"""Persistent, project-isolated memory system with semantic recall."""

from __future__ import annotations

import concurrent.futures
import dataclasses
import json
import logging
import time
from enum import Enum
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from main.config import AgentConfig

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

MAX_INDEX_LINES = 200
MAX_INDEX_BYTES = 25 * 1024       # 25 KB
MAX_SESSION_MEMORY_BYTES = 60 * 1024  # 60 KB
MAX_SINGLE_MEMORY_BYTES = 4 * 1024    # 4 KB
MAX_RECALLED_MEMORIES = 5
HEADER_SCAN_LINES = 30
PREFETCH_TIMEOUT = 5.0  # seconds
MAX_MEMORY_FILES = 200


@dataclasses.dataclass
class MemoryPrefetchHandle:
    """Non-blocking prefetch handle with settled/consumed flags."""

    future: concurrent.futures.Future  # type: ignore[type-arg]
    consumed: bool = False

    @property
    def settled(self) -> bool:
        """True when the background prefetch has finished (success or error)."""
        return self.future.done()

    def result(self) -> str:
        """Return the prefetch result without blocking.

        Only call this when ``settled`` is True.  Returns an empty string if
        the future is not yet done or if the worker raised an exception.
        """
        if not self.future.done():
            return ""
        try:
            return self.future.result(timeout=0) or ""
        except Exception:
            return ""


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


def scan_memory_headers(config: AgentConfig) -> list[dict[str, str]]:
    """Read only the first HEADER_SCAN_LINES of each memory file, parse frontmatter."""
    md = _memory_dir(config)
    if md is None:
        return []
    files = _memory_files(md)
    if len(files) > MAX_MEMORY_FILES:
        logger.warning(
            "Memory directory has %d files, limiting scan to %d (MAX_MEMORY_FILES).",
            len(files), MAX_MEMORY_FILES,
        )
        files = files[:MAX_MEMORY_FILES]
    headers: list[dict[str, str]] = []
    for f in files:
        try:
            text = f.read_text(encoding="utf-8", errors="replace")
            # Only take first N lines for parsing
            head = "\n".join(text.split("\n")[:HEADER_SCAN_LINES])
            meta = _parse_frontmatter(head)
            if meta:
                meta["filename"] = f.name
                headers.append(meta)
        except OSError:
            continue
    return headers


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


def recall_memories(
    query: str,
    headers: list[dict[str, str]],
    model_client: Any,
    already_surfaced: set[str] | None = None,
) -> list[str]:
    """Call LLM side query to select relevant memories. Returns filenames.

    Args:
        query: The user's current query text.
        headers: Parsed frontmatter dicts from all memory files.
        model_client: LLM client used to rank relevance.
        already_surfaced: Mutable set of filenames already injected this
            session.  Newly selected filenames are added to the set before
            returning so callers automatically get deduplication across turns.
    """
    if not headers:
        return []

    formatted_headers = "\n".join(
        f"- {h.get('filename', '?')}: name={h.get('name', '?')}, "
        f"description={h.get('description', '?')}, type={h.get('type', '?')}"
        for h in headers
    )

    prompt = (
        "You are a memory selection assistant. Given a user query and a list of "
        "available memories, select the most relevant memories that would help "
        "answer the query.\n\n"
        f"Available memories:\n{formatted_headers}\n\n"
        f"User query: {query}\n\n"
        "Return a JSON object with a single key \"selected_memories\" containing "
        "an array of filenames (max 5) that are most relevant. If no memories are "
        "relevant, return an empty array.\n"
        "Example: {\"selected_memories\": [\"project_standards.md\", \"user_preferences.md\"]}"
    )

    try:
        from main.context import Message
        response = model_client.complete(
            [Message(role="user", content=prompt)], []
        )
        text = response.text.strip()
        # Extract JSON from response (handle markdown code blocks)
        if "```" in text:
            start = text.find("{")
            end = text.rfind("}") + 1
            if start >= 0 and end > start:
                text = text[start:end]
        data = json.loads(text)
        selected = data.get("selected_memories", [])
        if isinstance(selected, list):
            candidates = [
                s for s in selected[:MAX_RECALLED_MEMORIES] if isinstance(s, str)
            ]
            # Deduplicate: remove filenames already injected this session
            if already_surfaced is not None:
                candidates = [s for s in candidates if s not in already_surfaced]
                already_surfaced.update(candidates)
            return candidates
    except Exception as exc:
        logger.debug("Memory recall failed: %s", exc)
    return []


def load_memory_content(config: AgentConfig, filename: str) -> str:
    """Read a single memory file, truncated to MAX_SINGLE_MEMORY_BYTES."""
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
    if len(text.encode("utf-8")) > MAX_SINGLE_MEMORY_BYTES:
        text = text.encode("utf-8")[:MAX_SINGLE_MEMORY_BYTES].decode("utf-8", errors="ignore")
        text += "\n... (truncated)"
    return text


def format_memories_for_injection(memories: list[dict[str, Any]]) -> str:
    """Format recalled memories as a <system-reminder> injection block."""
    parts: list[str] = []
    for mem in memories:
        section = f"## Recalled Memory: {mem['filename']}\n"
        created_at = mem.get("created_at")
        if created_at is not None:
            age_days = (time.time() - created_at) / 86400
            if age_days > 1:
                section += (
                    f"> This memory is {int(age_days)} days old. "
                    "Memories are point-in-time observations, not live state. "
                    "Verify against current code before asserting as fact.\n\n"
                )
        section += mem["content"]
        parts.append(section)
    return "<system-reminder>\n" + "\n\n".join(parts) + "\n</system-reminder>"


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
        "### Writing Memories\n"
        "When you learn important information worth remembering across sessions,\n"
        "create memory files using the write_file tool in the memory directory with this format:\n\n"
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


# ---------------------------------------------------------------------------
# Prefetch (thread-based for sync architecture)
# ---------------------------------------------------------------------------

_prefetch_executor = concurrent.futures.ThreadPoolExecutor(max_workers=1)


def start_memory_prefetch(
    query: str,
    config: AgentConfig,
    model_client: Any,
    session_memory_bytes: int,
    already_surfaced: set[str] | None = None,
) -> MemoryPrefetchHandle | None:
    """Start memory prefetch in background thread.

    Returns a :class:`MemoryPrefetchHandle` that can be polled non-blocking
    via ``handle.settled`` / ``handle.result()``, or ``None`` if the prefetch
    was skipped due to gate conditions.
    """
    # Gate 1: multi-word input
    if " " not in query.strip():
        return None
    # Gate 2: memory dir exists and has files
    md = _memory_dir(config)
    if md is None or not md.exists():
        return None
    md_files = _memory_files(md)
    if not md_files:
        return None
    # Gate 3: session budget
    if session_memory_bytes >= MAX_SESSION_MEMORY_BYTES:
        return None

    surfaced: set[str] = already_surfaced if already_surfaced is not None else set()
    future = _prefetch_executor.submit(
        _do_memory_prefetch, query, config, model_client, surfaced,
    )
    return MemoryPrefetchHandle(future=future)


def _do_memory_prefetch(
    query: str,
    config: AgentConfig,
    model_client: Any,
    already_surfaced: set[str],
) -> str:
    """Synchronous prefetch worker (runs in thread)."""
    headers = scan_memory_headers(config)
    if not headers:
        return ""
    selected = recall_memories(query, headers, model_client, already_surfaced)
    if not selected:
        return ""
    memories: list[dict[str, Any]] = []
    md = _memory_dir(config)
    if md is None:
        return ""
    for filename in selected:
        content = load_memory_content(config, filename)
        if content:
            try:
                resolved_md = md.resolve()
                filepath = (resolved_md / filename).resolve()
                if not filepath.is_relative_to(resolved_md):
                    continue
                created_at: float | None = None
                if filepath.exists():
                    created_at = filepath.stat().st_ctime
            except Exception:
                continue
            memories.append({
                "filename": filename,
                "content": content,
                "created_at": created_at,
            })
    return format_memories_for_injection(memories) if memories else ""
