"""Synchronous JSON-RPC stdio connection for MCP servers."""

from __future__ import annotations

import json
import os
import queue
import subprocess
import sys
import threading
from pathlib import Path
from typing import Any


DEFAULT_TIMEOUT = 15.0
PROTOCOL_VERSION = "2024-11-05"


class McpError(RuntimeError):
    """Base error raised by the MCP client."""


class McpToolError(McpError):
    """Raised when an MCP tool reports an error result."""


class McpConnection:
    """Manage one MCP server process and JSON-RPC request routing."""

    def __init__(
        self,
        name: str,
        command: str,
        args: list[str] | tuple[str, ...] | None = None,
        env: dict[str, str] | None = None,
        *,
        cwd: Path | None = None,
        timeout: float = DEFAULT_TIMEOUT,
    ) -> None:
        self.name = name
        self.command = command
        self.args = list(args or [])
        self.env = dict(env or {})
        self.cwd = cwd
        self.timeout = timeout

        self._process: subprocess.Popen[str] | None = None
        self._reader_thread: threading.Thread | None = None
        self._stderr_thread: threading.Thread | None = None
        self._write_lock = threading.Lock()
        self._pending_lock = threading.Lock()
        self._pending: dict[int, queue.Queue[dict[str, Any]]] = {}
        self._next_id = 0
        self._closed = False

    @property
    def is_connected(self) -> bool:
        return self._process is not None and self._process.poll() is None

    def connect(self) -> None:
        """Spawn the configured server process and start reader threads."""
        if self.is_connected:
            return

        env = os.environ.copy()
        env.update({key: str(value) for key, value in self.env.items()})
        argv = [self.command, *self.args]
        self._process = subprocess.Popen(
            argv,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
            cwd=str(self.cwd) if self.cwd is not None else None,
            env=env,
        )
        self._closed = False

        self._reader_thread = threading.Thread(
            target=self._reader_loop,
            name=f"mcp-{self.name}-stdout",
            daemon=True,
        )
        self._reader_thread.start()

        self._stderr_thread = threading.Thread(
            target=self._stderr_loop,
            name=f"mcp-{self.name}-stderr",
            daemon=True,
        )
        self._stderr_thread.start()

    def initialize(self) -> dict[str, Any]:
        """Perform the MCP initialize handshake."""
        result = self._send_request(
            "initialize",
            {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": {
                    "name": "forge-code",
                    "version": "0.1.0",
                },
            },
        )
        self._send_notification("notifications/initialized", {})
        return result

    def list_tools(self) -> list[dict[str, Any]]:
        """Return raw MCP tool metadata from the server."""
        result = self._send_request("tools/list", {})
        tools = result.get("tools", [])
        if not isinstance(tools, list):
            raise McpError("Invalid tools/list response: 'tools' is not a list")
        return [tool for tool in tools if isinstance(tool, dict)]

    def call_tool(self, name: str, args: dict[str, Any] | None = None) -> str:
        """Call an MCP tool and return its textual result."""
        result = self._send_request(
            "tools/call",
            {
                "name": name,
                "arguments": args or {},
            },
        )
        text = self._extract_tool_text(result)
        if result.get("isError"):
            raise McpToolError(text or f"MCP tool '{name}' reported an error")
        return text

    def close(self) -> None:
        """Terminate the server process and wake any pending requests."""
        self._closed = True
        process = self._process
        if process is None:
            return

        if process.stdin is not None:
            try:
                process.stdin.close()
            except OSError:
                pass

        if process.poll() is None:
            try:
                process.terminate()
            except OSError:
                pass
            try:
                process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                try:
                    process.kill()
                except OSError:
                    pass
                try:
                    process.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    pass

        self._wake_pending("MCP connection closed")

        if self._reader_thread is not None:
            self._reader_thread.join(timeout=1)
        if self._stderr_thread is not None:
            self._stderr_thread.join(timeout=1)

    def _send_request(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        if not self.is_connected:
            raise McpError(f"MCP server '{self.name}' is not connected")

        with self._write_lock:
            self._next_id += 1
            request_id = self._next_id
            response_queue: queue.Queue[dict[str, Any]] = queue.Queue(maxsize=1)
            with self._pending_lock:
                self._pending[request_id] = response_queue
            try:
                self._write_message(
                    {
                        "jsonrpc": "2.0",
                        "id": request_id,
                        "method": method,
                        "params": params or {},
                    }
                )
            except Exception:
                with self._pending_lock:
                    self._pending.pop(request_id, None)
                raise

        try:
            response = response_queue.get(timeout=self.timeout)
        except queue.Empty as exc:
            raise TimeoutError(
                f"Timed out waiting for MCP response to '{method}' from '{self.name}'"
            ) from exc
        finally:
            with self._pending_lock:
                self._pending.pop(request_id, None)

        if "error" in response:
            error = response["error"]
            if isinstance(error, dict):
                message = error.get("message") or json.dumps(error, ensure_ascii=False)
            else:
                message = str(error)
            raise McpError(message)

        result = response.get("result", {})
        if not isinstance(result, dict):
            raise McpError(f"Invalid JSON-RPC response for '{method}': result is not an object")
        return result

    def _send_notification(self, method: str, params: dict[str, Any] | None = None) -> None:
        if not self.is_connected:
            raise McpError(f"MCP server '{self.name}' is not connected")
        with self._write_lock:
            self._write_message(
                {
                    "jsonrpc": "2.0",
                    "method": method,
                    "params": params or {},
                }
            )

    def _write_message(self, message: dict[str, Any]) -> None:
        process = self._process
        if process is None or process.stdin is None:
            raise McpError(f"MCP server '{self.name}' stdin is unavailable")
        process.stdin.write(json.dumps(message, ensure_ascii=False, separators=(",", ":")) + "\n")
        process.stdin.flush()

    def _reader_loop(self) -> None:
        process = self._process
        if process is None or process.stdout is None:
            return

        try:
            for raw_line in process.stdout:
                line = raw_line.strip()
                if not line:
                    continue
                try:
                    message = json.loads(line)
                except json.JSONDecodeError as exc:
                    print(
                        f"[mcp:{self.name}] Ignoring invalid JSON from stdout: {exc}",
                        file=sys.stderr,
                    )
                    continue
                if not isinstance(message, dict):
                    continue
                request_id = message.get("id")
                if not isinstance(request_id, int):
                    continue
                with self._pending_lock:
                    response_queue = self._pending.get(request_id)
                if response_queue is not None:
                    response_queue.put(message)
        finally:
            if not self._closed:
                self._wake_pending(f"MCP server '{self.name}' stdout closed")

    def _stderr_loop(self) -> None:
        process = self._process
        if process is None or process.stderr is None:
            return
        for raw_line in process.stderr:
            line = raw_line.rstrip()
            if line:
                print(f"[mcp:{self.name}:stderr] {line}", file=sys.stderr)

    def _wake_pending(self, message: str) -> None:
        response = {
            "jsonrpc": "2.0",
            "error": {
                "code": -32000,
                "message": message,
            },
        }
        with self._pending_lock:
            queues = list(self._pending.values())
        for response_queue in queues:
            try:
                response_queue.put_nowait(dict(response))
            except queue.Full:
                pass

    @staticmethod
    def _extract_tool_text(result: dict[str, Any]) -> str:
        content = result.get("content", [])
        parts: list[str] = []
        if isinstance(content, list):
            for item in content:
                if not isinstance(item, dict):
                    continue
                if item.get("type") == "text":
                    parts.append(str(item.get("text", "")))
                else:
                    parts.append(json.dumps(item, ensure_ascii=False))
        structured = result.get("structuredContent")
        if not parts and structured is not None:
            parts.append(json.dumps(structured, ensure_ascii=False, indent=2))
        return "\n".join(part for part in parts if part)
