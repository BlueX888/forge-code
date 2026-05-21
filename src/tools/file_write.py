"""File write and edit tools."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from main.config import AgentConfig
from safety.permissions import SafetyLabel
from tools.base import ToolResult


def _maybe_update_memory_index(target: Path, config: AgentConfig) -> None:
    """If target is in the memory directory, rebuild MEMORY.md."""
    if not config.memory_enabled or config.memory_dir is None:
        return
    try:
        target.resolve().relative_to(config.memory_dir.resolve())
    except ValueError:
        return  # Not in memory directory
    if target.suffix != ".md" or target.name == "MEMORY.md":
        return
    from memory.memory import auto_update_memory_index
    auto_update_memory_index(config)


class WriteFileTool:
    """Write content to a file, creating parent directories as needed."""

    @property
    def name(self) -> str:
        return "write_file"

    @property
    def description(self) -> str:
        return (
            "Write content to a file. Creates the file if it does not exist, "
            "or overwrites it if it does. Parent directories are created automatically."
        )

    @property
    def parameters_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Path to the file to write (relative to working directory or absolute)",
                },
                "content": {
                    "type": "string",
                    "description": "The content to write to the file",
                },
            },
            "required": ["path", "content"],
        }

    @property
    def safety_label(self) -> SafetyLabel:
        return SafetyLabel.DESTRUCTIVE

    def run(self, *, arguments: dict[str, Any], config: AgentConfig) -> ToolResult:
        raw_path = arguments.get("path", "")
        content = arguments.get("content", "")

        if not raw_path:
            return ToolResult(False, "", "No path provided")

        target = Path(raw_path)
        if not target.is_absolute():
            target = config.working_directory / target
        target = target.resolve()

        if not config.is_path_allowed(target):
            return ToolResult(False, "", f"Path {target} is outside allowed directories")

        try:
            # Detect if target exists and is binary
            if target.exists():
                try:
                    target.read_text(encoding="utf-8")
                except UnicodeDecodeError:
                    return ToolResult(False, "", f"Refusing to overwrite binary file: {target}")

            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8")
            size = target.stat().st_size
            _maybe_update_memory_index(target, config)
            return ToolResult(True, f"Wrote {size} bytes to {target}")
        except OSError as exc:
            return ToolResult(False, "", str(exc))


class EditFileTool:
    """Edit a file by replacing exact text matches."""

    @property
    def name(self) -> str:
        return "edit_file"

    @property
    def description(self) -> str:
        return (
            "Edit a file by replacing an exact text match. Provide enough surrounding "
            "context in old_text to uniquely identify the location. "
            "If old_text is not found, an error is returned."
        )

    @property
    def parameters_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Path to the file to edit",
                },
                "old_text": {
                    "type": "string",
                    "description": "The exact text to find and replace",
                },
                "new_text": {
                    "type": "string",
                    "description": "The replacement text",
                },
            },
            "required": ["path", "old_text", "new_text"],
        }

    @property
    def safety_label(self) -> SafetyLabel:
        return SafetyLabel.DESTRUCTIVE

    def run(self, *, arguments: dict[str, Any], config: AgentConfig) -> ToolResult:
        raw_path = arguments.get("path", "")
        old_text = arguments.get("old_text", "")
        new_text = arguments.get("new_text", "")

        if not raw_path:
            return ToolResult(False, "", "No path provided")
        if not old_text:
            return ToolResult(False, "", "old_text cannot be empty")

        target = Path(raw_path)
        if not target.is_absolute():
            target = config.working_directory / target
        target = target.resolve()

        if not config.is_path_allowed(target):
            return ToolResult(False, "", f"Path {target} is outside allowed directories")

        try:
            content = target.read_text(encoding="utf-8")
        except FileNotFoundError:
            return ToolResult(False, "", f"File not found: {target}")
        except ValueError as exc:
            return ToolResult(False, "", f"Unable to read file: {exc} (file might be binary or has non-UTF-8 encoding)")
        except OSError as exc:
            return ToolResult(False, "", str(exc))

        count = content.count(old_text)
        if count == 0:
            # Show a snippet of the file to help the model find the right text
            preview = content[:500] + ("..." if len(content) > 500 else "")
            return ToolResult(
                False, "",
                f"old_text not found in {target}. File starts with:\n{preview}",
            )

        new_content = content.replace(old_text, new_text, 1)

        try:
            target.write_text(new_content, encoding="utf-8")
        except OSError as exc:
            return ToolResult(False, "", str(exc))

        _maybe_update_memory_index(target, config)

        msg = f"Replaced text in {target}"
        if count > 1:
            msg += f" (found {count} matches, replaced first occurrence only)"
        return ToolResult(True, msg)
