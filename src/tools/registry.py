"""Tool registry — stores, finds, and lists tools."""

from __future__ import annotations

from typing import Any

from tools.base import Tool


class ToolRegistry:
    """Simple dict-backed tool registry."""

    # Aliases handle model-training mismatches (e.g. Claude often calls
    # "Grep"/"Glob" when the actual tool is named "Search").
    _ALIASES: dict[str, str] = {
        "Grep": "Search",
        "Glob": "Search",
        "grep": "Search",
        "glob": "Search",
        "search": "Search",
        "read_file": "Read",
        "write_file": "Write",
        "edit_file": "Edit",
        "list_directory": "ListDir",
        "run_command": "Bash",
    }

    def __init__(self) -> None:
        self._tools: dict[str, Tool] = {}

    def register(self, tool: Tool) -> None:
        if tool.name in self._tools:
            raise ValueError(f"Tool already registered: {tool.name}")
        self._tools[tool.name] = tool

    def get(self, name: str) -> Tool | None:
        tool = self._tools.get(name)
        if tool is None:
            canonical = self._ALIASES.get(name)
            if canonical:
                tool = self._tools.get(canonical)
        return tool

    def list_tools(self) -> list[dict[str, Any]]:
        """Return tool descriptions suitable for context building."""
        return [
            {
                "name": t.name,
                "description": t.description,
                "parameters": t.parameters_schema,
            }
            for t in self._tools.values()
        ]
