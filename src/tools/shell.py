"""Shell command execution tool."""

from __future__ import annotations

import os
import subprocess
import sys
from typing import Any

from main.config import AgentConfig
from safety.permissions import SafetyLabel
from tools.base import ToolResult

_MAX_OUTPUT = 102_400  # 100 KB

# Substrings in env-var names that indicate sensitive values.
_SENSITIVE_PATTERNS: tuple[str, ...] = (
    "API_KEY", "APIKEY", "SECRET", "PRIVATE_KEY",
    "TOKEN", "PASSWORD", "PASSWD", "CREDENTIAL", "AUTH",
)


def _make_sanitized_env() -> dict[str, str]:
    """Return a copy of os.environ with sensitive variables removed."""
    return {
        k: v
        for k, v in os.environ.items()
        if not any(pat in k.upper() for pat in _SENSITIVE_PATTERNS)
    }


class RunCommandTool:
    """Execute shell commands."""

    @property
    def name(self) -> str:
        return "run_command"

    @property
    def description(self) -> str:
        return (
            "Run a shell command and return its output (stdout + stderr). "
            "Commands execute in the agent's working directory. "
            "Output is truncated if it exceeds 100KB."
        )

    @property
    def parameters_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "command": {
                    "type": "string",
                    "description": "The shell command to execute",
                },
                "timeout": {
                    "type": "integer",
                    "description": "Timeout in seconds (default: from config, typically 120)",
                },
            },
            "required": ["command"],
        }

    @property
    def safety_label(self) -> SafetyLabel:
        return SafetyLabel.CONCURRENT_SAFE

    def run(self, *, arguments: dict[str, Any], config: AgentConfig) -> ToolResult:
        command = arguments.get("command", "")
        if not command:
            return ToolResult(False, "", "No command provided")

        timeout = arguments.get("timeout", config.command_timeout)

        try:
            result = subprocess.run(
                command,
                shell=True,
                capture_output=True,
                text=True,
                cwd=str(config.working_directory),
                timeout=timeout,
                env=_make_sanitized_env(),
                # Use the appropriate shell for the platform
                executable=None if sys.platform == "win32" else "/bin/bash",
            )
        except subprocess.TimeoutExpired:
            return ToolResult(False, "", f"Command timed out after {timeout}s: {command}")
        except OSError as exc:
            return ToolResult(False, "", f"Failed to execute command: {exc}")

        output_parts: list[str] = []
        if result.stdout:
            output_parts.append(result.stdout)
        if result.stderr:
            output_parts.append(f"[stderr]\n{result.stderr}")
        output_parts.append(f"[exit code: {result.returncode}]")

        output = "\n".join(output_parts)

        # Truncate if too large
        if len(output) > _MAX_OUTPUT:
            half = _MAX_OUTPUT // 2
            output = (
                output[:half]
                + f"\n\n... [truncated {len(output) - _MAX_OUTPUT} bytes] ...\n\n"
                + output[-half:]
            )

        return ToolResult(result.returncode == 0, output, None if result.returncode == 0 else f"Exit code {result.returncode}")
