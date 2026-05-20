"""Unified search tool: glob file search and regex content search."""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any

from main.config import AgentConfig
from safety.permissions import SafetyLabel
from tools.base import ToolResult

_MAX_RESULTS = 200
_MAX_FILE_SIZE = 1_048_576  # 1 MB
_MAX_OUTPUT_LINES = 500


def _is_binary(path: Path) -> bool:
    """Heuristic: read first 8KB and check for null bytes."""
    try:
        chunk = path.read_bytes()[:8192]
        return b"\x00" in chunk
    except OSError:
        return True


class SearchTool:
    """Search by file name pattern, file content regex, or both."""

    @property
    def name(self) -> str:
        return "search"

    @property
    def description(self) -> str:
        return (
            "Search for files by glob pattern, content regex, or both. "
            "Use 'pattern' to find files by name (e.g. '**/*.py'). "
            "Use 'content_pattern' to search file contents with a regex. "
            "When both are given, files are first filtered by glob, then searched by content. "
            "At least one of 'pattern' or 'content_pattern' must be provided."
        )

    @property
    def parameters_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "pattern": {
                    "type": "string",
                    "description": "Glob pattern to match file names (e.g. '**/*.py', 'src/**/*.ts')",
                },
                "content_pattern": {
                    "type": "string",
                    "description": "Regular expression to search file contents",
                },
                "path": {
                    "type": "string",
                    "description": "Directory to search in (default: working directory)",
                },
                "case_insensitive": {
                    "type": "boolean",
                    "description": "Ignore case for content_pattern (default: false)",
                },
            },
        }

    @property
    def safety_label(self) -> SafetyLabel:
        return SafetyLabel.READONLY

    def run(self, *, arguments: dict[str, Any], config: AgentConfig) -> ToolResult:
        glob_pattern = arguments.get("pattern")
        content_pattern = arguments.get("content_pattern")

        if not glob_pattern and not content_pattern:
            return ToolResult(False, "", "At least one of 'pattern' or 'content_pattern' must be provided")

        # Resolve base directory
        raw_path = arguments.get("path")
        if raw_path:
            base_dir = Path(raw_path) if Path(raw_path).is_absolute() else config.working_directory / raw_path
        else:
            base_dir = config.working_directory
        base_dir = base_dir.resolve()

        if not config.is_path_allowed(base_dir):
            return ToolResult(False, "", f"Path {base_dir} is outside allowed directories")

        # --- Glob-only mode ---
        if glob_pattern and not content_pattern:
            return self._glob_search(glob_pattern, base_dir, config)

        # --- Content-only mode ---
        if content_pattern and not glob_pattern:
            return self._content_search(content_pattern, base_dir, None, arguments, config)

        # --- Combined mode: glob first, then content search ---
        return self._content_search(content_pattern, base_dir, glob_pattern, arguments, config)

    def _glob_search(
        self, pattern: str, base_dir: Path, config: AgentConfig,
    ) -> ToolResult:
        """Find files matching a glob pattern."""
        try:
            matches = sorted(
                base_dir.glob(pattern),
                key=lambda p: p.stat().st_mtime,
                reverse=True,
            )
        except OSError as exc:
            return ToolResult(False, "", str(exc))

        matches = [p for p in matches if config.is_path_allowed(p)]
        total = len(matches)
        matches = matches[:_MAX_RESULTS]

        lines = [str(p) for p in matches]
        if total > _MAX_RESULTS:
            lines.append(f"\n... and {total - _MAX_RESULTS} more results")

        if not lines:
            return ToolResult(True, f"No files matching '{pattern}' found in {base_dir}")
        return ToolResult(True, "\n".join(lines))

    def _content_search(
        self,
        raw_pattern: str,
        base_dir: Path,
        glob_pattern: str | None,
        arguments: dict[str, Any],
        config: AgentConfig,
    ) -> ToolResult:
        """Search file contents with a regex, optionally pre-filtered by glob."""
        flags = re.IGNORECASE if arguments.get("case_insensitive", False) else 0
        try:
            regex = re.compile(raw_pattern, flags)
        except re.error as exc:
            return ToolResult(False, "", f"Invalid regex: {exc}")

        # Build file list
        if glob_pattern:
            try:
                files = sorted(
                    base_dir.glob(glob_pattern),
                    key=lambda p: p.stat().st_mtime,
                    reverse=True,
                )
            except OSError as exc:
                return ToolResult(False, "", str(exc))
            files = [p for p in files if p.is_file() and config.is_path_allowed(p)]
        elif base_dir.is_file():
            files = [base_dir]
        else:
            files = []
            for dirpath, _dirnames, filenames in os.walk(base_dir):
                for fname in filenames:
                    fpath = Path(dirpath) / fname
                    if config.is_path_allowed(fpath):
                        files.append(fpath)

        output_lines: list[str] = []
        match_count = 0

        for fpath in files:
            try:
                if fpath.stat().st_size > _MAX_FILE_SIZE:
                    continue
            except OSError:
                continue

            if _is_binary(fpath):
                continue

            try:
                text = fpath.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue

            for lineno, line in enumerate(text.splitlines(), 1):
                if regex.search(line):
                    output_lines.append(f"{fpath}:{lineno}: {line.rstrip()}")
                    match_count += 1
                    if len(output_lines) >= _MAX_OUTPUT_LINES:
                        output_lines.append(f"\n... truncated ({match_count}+ matches)")
                        return ToolResult(True, "\n".join(output_lines))

        if not output_lines:
            return ToolResult(True, f"No matches found for '{raw_pattern}'")
        return ToolResult(True, "\n".join(output_lines))
