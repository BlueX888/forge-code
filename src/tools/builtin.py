"""Built-in tools: Read, ListDir."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from main.config import AgentConfig
from safety.permissions import SafetyLabel
from tools.base import ToolResult
from tools.names import ToolName
from tools.registry import ToolRegistry


# ---------------------------------------------------------------------------
# Read
# ---------------------------------------------------------------------------

class ReadFileTool:
    @property
    def name(self) -> str:
        return ToolName.READ

    @property
    def description(self) -> str:
        return "Read the contents of a file with optional line offset and limit."

    @property
    def parameters_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "File path to read"},
                "offset": {"type": "integer", "description": "Starting line (0-based)", "default": 0},
                "limit": {"type": "integer", "description": "Max lines to return", "default": 200},
            },
            "required": ["path"],
        }

    @property
    def safety_label(self) -> SafetyLabel:
        return SafetyLabel.READONLY

    def run(self, *, arguments: dict[str, Any], config: AgentConfig) -> ToolResult:
        raw_path = arguments.get("path", "").strip()
        if not raw_path:
            return ToolResult(False, "", "Path is empty. Provide a file path to read.")
        path = Path(raw_path) if Path(raw_path).is_absolute() else config.working_directory / raw_path
        path = path.resolve()

        if not config.is_path_allowed(path):
            return ToolResult(False, "", f"Path {path} is outside allowed directories")

        if path.is_dir():
            return ToolResult(False, "", f"'{path}' is a directory, not a file. Use ListDir to view its contents.")

        if not path.is_file():
            return ToolResult(False, "", f"File not found: {path}")

        offset = max(int(arguments.get("offset", 0)), 0)
        limit = min(max(int(arguments.get("limit", 200)), 1), 500)

        try:
            lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError as exc:
            return ToolResult(False, "", str(exc))

        selected = lines[offset : offset + limit]
        numbered = "\n".join(
            f"  {offset + i + 1:>4} | {line}" for i, line in enumerate(selected)
        )
        return ToolResult(True, numbered)


# ---------------------------------------------------------------------------
# ListDir
# ---------------------------------------------------------------------------

class ListDirectoryTool:
    @property
    def name(self) -> str:
        return ToolName.LIST_DIR

    @property
    def description(self) -> str:
        return "List the contents of a directory and show its absolute path."

    @property
    def parameters_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Directory path (default: working directory)"},
            },
        }

    @property
    def safety_label(self) -> SafetyLabel:
        return SafetyLabel.READONLY

    def run(self, *, arguments: dict[str, Any], config: AgentConfig) -> ToolResult:
        raw_path = arguments.get("path", "")
        if raw_path:
            path = Path(raw_path) if Path(raw_path).is_absolute() else config.working_directory / raw_path
        else:
            path = config.working_directory
        path = path.resolve()

        if not config.is_path_allowed(path):
            return ToolResult(False, "", f"Path {path} is outside allowed directories")

        if not path.is_dir():
            return ToolResult(False, "", f"Not a directory: {path}")

        try:
            entries = sorted(path.iterdir(), key=lambda p: (not p.is_dir(), p.name.lower()))
        except OSError as exc:
            return ToolResult(False, "", str(exc))

        lines: list[str] = []
        for entry in entries:
            if entry.is_dir():
                lines.append(f"  {entry.name}/")
            else:
                try:
                    size = entry.stat().st_size
                except OSError:
                    size = 0
                lines.append(f"  {entry.name}  ({_human_size(size)})")

        return ToolResult(True, f"[directory: {path}]\n" + ("\n".join(lines) if lines else "(empty directory)"))


def _human_size(nbytes: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if nbytes < 1024:
            return f"{nbytes:.0f} {unit}" if unit == "B" else f"{nbytes:.1f} {unit}"
        nbytes /= 1024
    return f"{nbytes:.1f} TB"


# ---------------------------------------------------------------------------
# Registration helper
# ---------------------------------------------------------------------------

def register_builtin_tools(registry: ToolRegistry) -> None:
    """Register all built-in tools into *registry*."""
    from tools.enter_plan_mode import EnterPlanModeTool
    from tools.exit_plan_mode import ExitPlanModeTool
    from tools.file_write import EditFileTool, WriteFileTool
    from tools.search import SearchTool
    from tools.shell import RunCommandTool
    from tools.skill_tool import SkillTool
    from tools.web_search import WebSearchTool
    from tools.web_fetch import WebFetchTool

    registry.register(ReadFileTool())
    registry.register(ListDirectoryTool())
    registry.register(WriteFileTool())
    registry.register(EditFileTool())
    registry.register(SearchTool())
    registry.register(RunCommandTool())
    registry.register(EnterPlanModeTool())
    registry.register(ExitPlanModeTool())
    registry.register(SkillTool())
    registry.register(WebSearchTool())
    registry.register(WebFetchTool())

