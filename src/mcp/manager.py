"""MCP configuration loading and runtime tool routing."""

from __future__ import annotations

import dataclasses
import json
import sys
import tomllib
from pathlib import Path
from typing import Any

from main.config import global_config_path, project_config_path
from mcp.connection import DEFAULT_TIMEOUT, McpConnection


@dataclasses.dataclass(frozen=True)
class McpServerConfig:
    name: str
    command: str
    args: tuple[str, ...] = ()
    env: dict[str, str] = dataclasses.field(default_factory=dict)


class McpManager:
    """Load MCP server configs, own connections, and route tool calls."""

    def __init__(self, working_directory: Path, *, timeout: float = DEFAULT_TIMEOUT) -> None:
        self._working_directory = working_directory
        self._timeout = timeout
        self._loaded = False
        self._configs: dict[str, McpServerConfig] = {}
        self._connections: dict[str, McpConnection] = {}
        self._tools: dict[str, list[dict[str, Any]]] = {}

    def load_and_connect(self) -> None:
        """Load config and connect each configured server once."""
        if self._loaded:
            return
        self._loaded = True
        self._configs = self._load_configs()

        for name, config in self._configs.items():
            connection = McpConnection(
                name=config.name,
                command=config.command,
                args=config.args,
                env=config.env,
                cwd=self._working_directory,
                timeout=self._timeout,
            )
            try:
                connection.connect()
                connection.initialize()
                self._tools[name] = connection.list_tools()
            except Exception as exc:
                print(f"[mcp] Failed to connect '{name}': {exc}", file=sys.stderr)
                connection.close()
                continue
            self._connections[name] = connection

    def get_tool_definitions(self) -> list[dict[str, Any]]:
        """Return connected MCP tools in ForgeCode's model-tool schema."""
        definitions: list[dict[str, Any]] = []
        for server_name, tools in self._tools.items():
            for tool in tools:
                tool_name = tool.get("name")
                if not isinstance(tool_name, str) or not tool_name:
                    continue
                parameters = tool.get("inputSchema") or tool.get("parameters")
                if not isinstance(parameters, dict):
                    parameters = {"type": "object", "properties": {}}
                description = tool.get("description")
                if not isinstance(description, str) or not description:
                    description = f"MCP tool '{tool_name}' from server '{server_name}'."
                definitions.append(
                    {
                        "name": f"mcp__{server_name}__{tool_name}",
                        "description": f"[MCP:{server_name}] {description}",
                        "parameters": parameters,
                    }
                )
        return definitions

    def is_mcp_tool(self, name: str) -> bool:
        return name.startswith("mcp__")

    def call_tool(self, prefixed_name: str, args: dict[str, Any] | None = None) -> str:
        parts = prefixed_name.split("__", 2)
        if len(parts) != 3 or parts[0] != "mcp":
            raise ValueError(f"Invalid MCP tool name: {prefixed_name}")
        _prefix, server_name, tool_name = parts
        connection = self._connections.get(server_name)
        if connection is None:
            raise RuntimeError(f"MCP server '{server_name}' is not connected")
        return connection.call_tool(tool_name, args or {})

    def close_all(self) -> None:
        for connection in list(self._connections.values()):
            connection.close()
        self._connections.clear()
        self._tools.clear()

    def server_summaries(self) -> list[dict[str, Any]]:
        """Return connected server names and tool counts for /mcp."""
        return [
            {
                "name": name,
                "tool_count": len(self._tools.get(name, [])),
            }
            for name in sorted(self._connections)
        ]

    def configured_count(self) -> int:
        return len(self._configs)

    def _load_configs(self) -> dict[str, McpServerConfig]:
        merged: dict[str, dict[str, Any]] = {}
        self._merge_servers(merged, self._load_toml_servers(global_config_path()))
        self._merge_servers(merged, self._load_toml_servers(project_config_path(self._working_directory)))
        self._merge_servers(merged, self._load_json_servers(self._working_directory / ".mcp.json"))

        configs: dict[str, McpServerConfig] = {}
        for name, raw in merged.items():
            config = self._coerce_server_config(name, raw)
            if config is not None:
                configs[name] = config
        return configs

    @staticmethod
    def _merge_servers(target: dict[str, dict[str, Any]], source: dict[str, dict[str, Any]]) -> None:
        for name, raw in source.items():
            if not isinstance(raw, dict):
                continue
            current = dict(target.get(name, {}))
            for key, value in raw.items():
                if key == "env" and isinstance(value, dict):
                    env = dict(current.get("env", {})) if isinstance(current.get("env"), dict) else {}
                    env.update(value)
                    current["env"] = env
                else:
                    current[key] = value
            target[name] = current

    @staticmethod
    def _load_toml_servers(path: Path) -> dict[str, dict[str, Any]]:
        data = _load_toml(path)
        mcp = data.get("mcp", {})
        if not isinstance(mcp, dict):
            return {}
        servers = mcp.get("servers", {})
        if not isinstance(servers, dict):
            return {}
        return {str(name): raw for name, raw in servers.items() if isinstance(raw, dict)}

    @staticmethod
    def _load_json_servers(path: Path) -> dict[str, dict[str, Any]]:
        if not path.is_file():
            return {}
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError, UnicodeDecodeError) as exc:
            print(f"[mcp] Warning: skipping invalid config {path}: {exc}", file=sys.stderr)
            return {}
        if not isinstance(data, dict):
            return {}
        servers = data.get("mcpServers", {})
        if not isinstance(servers, dict):
            return {}
        return {str(name): raw for name, raw in servers.items() if isinstance(raw, dict)}

    @staticmethod
    def _coerce_server_config(name: str, raw: dict[str, Any]) -> McpServerConfig | None:
        if raw.get("disabled") is True:
            return None
        if "__" in name:
            print(f"[mcp] Skipping server '{name}': server names cannot contain '__'", file=sys.stderr)
            return None

        command = raw.get("command")
        if not isinstance(command, str) or not command.strip():
            print(f"[mcp] Skipping server '{name}': missing command", file=sys.stderr)
            return None

        raw_args = raw.get("args", [])
        if not isinstance(raw_args, list):
            print(f"[mcp] Skipping server '{name}': args must be a list", file=sys.stderr)
            return None
        args = tuple(str(arg) for arg in raw_args)

        raw_env = raw.get("env", {})
        if not isinstance(raw_env, dict):
            print(f"[mcp] Skipping server '{name}': env must be a table/object", file=sys.stderr)
            return None
        env = {str(key): str(value) for key, value in raw_env.items()}

        return McpServerConfig(name=name, command=command, args=args, env=env)


def _load_toml(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    try:
        raw = path.read_bytes()
        if raw.startswith(b"\xef\xbb\xbf"):
            raw = raw[3:]
        data = tomllib.loads(raw.decode("utf-8"))
    except (OSError, tomllib.TOMLDecodeError, UnicodeDecodeError) as exc:
        print(f"[mcp] Warning: skipping invalid config {path}: {exc}", file=sys.stderr)
        return {}
    return data if isinstance(data, dict) else {}
