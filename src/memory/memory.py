"""Persistent, project-isolated memory system."""

from __future__ import annotations

import logging
from enum import Enum
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from main.config import AgentConfig

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

MAX_INDEX_LINES = 200
MAX_INDEX_BYTES = 25 * 1024       # 25 KB
MAX_MEMORY_FILES = 200

# Maximum bytes returned by load_memory_content (kept as a safety guard).
_MAX_SINGLE_MEMORY_BYTES = 4 * 1024    # 4 KB


class MemoryType(str, Enum):
    USER = "user"
    FEEDBACK = "feedback"
    PROJECT = "project"
    REFERENCE = "reference"


# ---------------------------------------------------------------------------
# YAML frontmatter parser (no external deps)
# ---------------------------------------------------------------------------

def _parse_frontmatter(text: str) -> dict[str, str]:
    """Parse simple YAML frontmatter without external dependencies."""
    if not text.startswith("---"):
        return {}
    end = text.find("---", 3)
    if end == -1:
        return {}
    yaml_block = text[3:end].strip()
    result: dict[str, str] = {}
    for line in yaml_block.split("\n"):
        if ":" in line:
            key, _, value = line.partition(":")
            result[key.strip()] = value.strip()
    return result


# ---------------------------------------------------------------------------
# Core functions
# ---------------------------------------------------------------------------

def _memory_dir(config: AgentConfig) -> Path | None:
    """Return the memory directory, or None if disabled."""
    if not config.memory_enabled or config.memory_dir is None:
        return None
    return config.memory_dir


def _memory_files(memory_dir: Path) -> list[Path]:
    """Return sorted list of .md files excluding MEMORY.md."""
    if not memory_dir.exists():
        return []
    return sorted(
        f for f in memory_dir.glob("*.md")
        if f.name != "MEMORY.md"
    )


def load_memory_index(config: AgentConfig) -> str:
    """Read MEMORY.md, truncating to MAX_INDEX_LINES / MAX_INDEX_BYTES."""
    md = _memory_dir(config)
    if md is None:
        return ""
    index_path = md / "MEMORY.md"
    if not index_path.is_file():
        return ""
    try:
        text = index_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""

    lines = text.split("\n")
    if len(lines) > MAX_INDEX_LINES:
        lines = lines[:MAX_INDEX_LINES]
        lines.append("... (truncated)")
    result = "\n".join(lines)
    if len(result.encode("utf-8")) > MAX_INDEX_BYTES:
        result = result.encode("utf-8")[:MAX_INDEX_BYTES].decode("utf-8", errors="ignore")
        result += "\n... (truncated)"
    return result


def list_memories(config: AgentConfig) -> list[dict[str, str]]:
    """Full scan of all memory file metadata, for /memory command display."""
    md = _memory_dir(config)
    if md is None:
        return []
    result: list[dict[str, str]] = []
    for f in _memory_files(md):
        try:
            text = f.read_text(encoding="utf-8", errors="replace")
            meta = _parse_frontmatter(text)
            result.append({
                "filename": f.name,
                "name": meta.get("name", f.stem),
                "description": meta.get("description", ""),
                "type": meta.get("type", "unknown"),
            })
        except OSError:
            continue
    return result


def load_memory_content(config: AgentConfig, filename: str) -> str:
    """Read a single memory file (path-traversal safe)."""
    md = _memory_dir(config)
    if md is None:
        return ""
    try:
        resolved_md = md.resolve()
        filepath = (resolved_md / filename).resolve()
        if not filepath.is_relative_to(resolved_md):
            logger.warning("Blocked path traversal attempt in memory load: %s", filename)
            return ""
    except Exception:
        return ""
    if not filepath.is_file():
        return ""
    try:
        text = filepath.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    if len(text.encode("utf-8")) > _MAX_SINGLE_MEMORY_BYTES:
        text = text.encode("utf-8")[:_MAX_SINGLE_MEMORY_BYTES].decode("utf-8", errors="ignore")
        text += "\n... (truncated)"
    return text


def build_memory_prompt_section(config: AgentConfig) -> str:
    """Build the memory guidance section for the system prompt."""
    md = _memory_dir(config)
    if md is None:
        return ""

    index_content = load_memory_index(config)
    if not index_content:
        index_content = "(empty -- no memories yet)"

    return (
        "## Persistent Memory\n\n"
        f"You have access to a persistent, file-based memory system stored at:\n"
        f"`{md}`\n\n"
        "### Memory Index\n"
        f"{index_content}\n\n"
        "### Reading Memories\n"
        "The index above lists available memory files with a one-line summary each.\n"
        "To read the **full content** of a memory file, use the `Read` tool:\n"
        "```\n"
        f"Read(path=\"{md}/<filename>.md\")\n"
        "```\n"
        "Fetch the full content whenever you need detailed information from a memory file.\n\n"
        "### Writing Memories\n"
        "When you learn important information worth remembering across sessions,\n"
        "create memory files using the Write tool in the memory directory with this format:\n\n"
        "```\n"
        "---\n"
        "name: <descriptive name>\n"
        "description: <one-line summary>\n"
        "type: <user|feedback|project|reference>\n"
        "---\n"
        "Content...\n"
        "```\n\n"
        "**Naming convention**: Use `{type}_{brief_name}.md` for the filename.\n"
        "Examples: `user_preferences.md`, `feedback_code_style.md`, `project_goals.md`\n\n"
        "After creating or updating a memory file, also update MEMORY.md in the same directory.\n"
        "MEMORY.md is an index -- each entry should be one line: `- [Title](file.md) -- one-line hook`.\n\n"
        "### Memory Types\n"
        "- **user**: User preferences, role, habits, knowledge level\n"
        "- **feedback**: User corrections (must include Why + How to apply)\n"
        "- **project**: Work goals, deadlines, tech decisions, ongoing tasks\n"
        "- **reference**: External resource links (URLs, tools, dashboards)\n\n"
        "### Do NOT Memorize\n"
        "- Code implementation details or architecture (read the code directly)\n"
        "- Git commit history\n"
        "- Content already in CLAUDE.md or project docs\n"
        "- Temporary task steps or branch-specific work"
    )


def auto_update_memory_index(config: AgentConfig) -> None:
    """Scan all memory files and rebuild MEMORY.md."""
    md = _memory_dir(config)
    if md is None:
        return
    files = _memory_files(md)
    if len(files) > MAX_MEMORY_FILES:
        logger.warning(
            "Memory directory has %d files which exceeds MAX_MEMORY_FILES (%d). "
            "Consider archiving old memories. Index will include the first %d files.",
            len(files), MAX_MEMORY_FILES, MAX_MEMORY_FILES,
        )
        files = files[:MAX_MEMORY_FILES]
    if not files:
        # Remove MEMORY.md if no memories exist
        index_path = md / "MEMORY.md"
        if index_path.exists():
            try:
                index_path.unlink()
            except OSError:
                pass
        return

    lines: list[str] = []
    for f in files:
        try:
            text = f.read_text(encoding="utf-8", errors="replace")
            meta = _parse_frontmatter(text)
            name = meta.get("name", f.stem)
            desc = meta.get("description", "")
            line = f"- [{name}]({f.name})"
            if desc:
                line += f" -- {desc}"
            lines.append(line)
        except OSError:
            continue

    content = "\n".join(lines[:MAX_INDEX_LINES]) + "\n"
    index_path = md / "MEMORY.md"
    try:
        index_path.write_text(content, encoding="utf-8")
    except OSError as exc:
        logger.debug("Failed to update MEMORY.md: %s", exc)
