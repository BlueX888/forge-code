"""MCP Server Presets definition and resolution logic."""

from __future__ import annotations

import dataclasses
import os
import shutil
import sys
from typing import Any


@dataclasses.dataclass(frozen=True)
class McpPreset:
    name: str
    description: str
    command: str
    args: tuple[str, ...]
    required_env: tuple[str, ...]
    env_aliases: dict[str, tuple[str, ...]] = dataclasses.field(default_factory=dict)


PRESETS: dict[str, McpPreset] = {
    "github": McpPreset(
        name="github",
        description="GitHub MCP Server",
        command="npx",
        args=("@modelcontextprotocol/server-github",),
        required_env=("GITHUB_PERSONAL_ACCESS_TOKEN",),
        env_aliases={"GITHUB_PERSONAL_ACCESS_TOKEN": ("GITHUB_TOKEN",)},
    )
}


def resolve_npx_command() -> str:
    """Resolve the 'npx' executable command, fallback to 'npx.cmd' on Windows."""
    if sys.platform == "win32":
        npx = shutil.which("npx")
        return npx if npx else "npx.cmd"
    return "npx"


def resolve_preset(name: str, user_config: Any) -> dict[str, Any] | None:
    """Resolve preset configuration combined with user overrides into a raw server config."""
    preset = PRESETS.get(name)
    if not preset:
        print(f"[mcp] Warning: Unknown preset '{name}'", file=sys.stderr)
        return None

    if user_config is False:
        return None
    if isinstance(user_config, dict) and user_config.get("enabled") is False:
        return None
    if not (user_config is True or isinstance(user_config, dict)):
        return None

    resolved_command = resolve_npx_command() if preset.command == "npx" else preset.command
    resolved_env: dict[str, str] = {}

    if isinstance(user_config, dict) and isinstance(user_config.get("env"), dict):
        for k, v in user_config["env"].items():
            resolved_env[str(k)] = str(v)

    for req_var in preset.required_env:
        if req_var in resolved_env:
            continue

        alias_found_in_config = False
        for alias in preset.env_aliases.get(req_var, ()):
            if alias in resolved_env:
                resolved_env[req_var] = resolved_env[alias]
                alias_found_in_config = True
                break

        if alias_found_in_config:
            continue

        if req_var in os.environ:
            resolved_env[req_var] = os.environ[req_var]
            continue

        alias_found_in_system = False
        for alias in preset.env_aliases.get(req_var, ()):
            if alias in os.environ:
                resolved_env[req_var] = os.environ[alias]
                alias_found_in_system = True
                break

        if alias_found_in_system:
            continue

        print(
            f"[mcp] Warning: Preset '{name}' is enabled but missing required environment variable '{req_var}'",
            file=sys.stderr,
        )
        return None

    return {
        "command": resolved_command,
        "args": list(preset.args),
        "env": resolved_env,
    }
