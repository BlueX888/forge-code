"""Layer 1: Per-tool output truncation with disk spill.

Triggers on every tool execution (synchronous), before the result enters
the message deque.  Combines line-count and byte-size thresholds using AND
logic: both must be exceeded for truncation to fire.

Default direction is *head* (beginning of output).  Shell tool output uses
*tail* (end of output).
"""

from __future__ import annotations

import time
import uuid
from pathlib import Path


# ---------------------------------------------------------------------------
# Token estimation helper (chars / 4 ≈ tokens)
# ---------------------------------------------------------------------------

def estimate_tokens(text: str) -> int:
    """Rough token estimate: chars // 4."""
    return len(text) // 4


# ---------------------------------------------------------------------------
# ShellTruncator — tail-based truncation for shell tool output
# ---------------------------------------------------------------------------

class ShellTruncator:
    """Truncates shell output by keeping the tail (last N lines).

    Used by :class:`ToolOutputTruncator` when the tool name matches a shell
    command.
    """

    def __init__(self, max_lines: int = 500, max_chars: int = 50_000) -> None:
        self._max_lines = max_lines
        self._max_chars = max_chars

    def truncate(self, content: str) -> str:
        lines = content.splitlines(keepends=True)
        if len(lines) <= self._max_lines and len(content) <= self._max_chars:
            return content

        # Keep tail lines up to max_chars
        tail_lines = lines[-self._max_lines:]
        result = "".join(tail_lines)
        if len(result) > self._max_chars:
            result = result[-self._max_chars:]

        marker = (
            f"\n[shell output truncated "
            f"from {len(lines)} lines / {len(content)} chars "
            f"to {len(tail_lines)} lines / {len(result)} chars]\n"
        )
        return marker + result.lstrip("\n")


# ---------------------------------------------------------------------------
# ToolOutputTruncator (Layer 1)
# ---------------------------------------------------------------------------

class ToolOutputTruncator:
    """Per-tool output truncation applied before the result enters the deque.

    Truncation fires only when **both** conditions are met:
      - lines > ``line_threshold``
      - bytes > ``byte_threshold``

    Truncated full output is spilled to disk.  The in-memory content is
    replaced with a short preview and a reference to the spill file.
    """

    PREVIEW_CHARS = 2048  # ~2 KB preview kept in memory

    # Tools whose output is truncated from the *tail* (end).
    # Only "Bash" is registered in builtin.py — keep single source of truth.
    _TAIL_TOOLS = frozenset({"Bash"})

    def __init__(
        self,
        spill_base: Path,
        session_id: str,
        *,
        line_threshold: int = 2_000,
        byte_threshold: int = 50_000,
        cleanup_interval: int = 3_600,
        max_age: int = 604_800,
    ) -> None:
        self._spill_dir = spill_base / ".forgecode" / "sessions" / session_id / "truncated"
        self._line_threshold = line_threshold
        self._byte_threshold = byte_threshold
        self._cleanup_interval = cleanup_interval
        self._max_age = max_age
        self._last_cleanup: float = 0.0

    # -- public API ---------------------------------------------------------

    def should_truncate(self, content: str) -> bool:
        """Check if content exceeds both line and byte thresholds (AND)."""
        lines = content.splitlines()
        byte_size = len(content.encode("utf-8", errors="replace"))
        return len(lines) > self._line_threshold and byte_size > self._byte_threshold

    def truncate(self, content: str, tool_name: str, tool_call_id: str | None = None) -> str:
        """Truncate *content*, spilling full output to disk.

        Returns the in-memory replacement string (preview + disk path).
        """
        if not self.should_truncate(content):
            return content

        # Determine truncation direction
        if tool_name in self._TAIL_TOOLS:
            return self._truncate_tail(content, tool_call_id)
        return self._truncate_head(content, tool_call_id)

    # -- head truncation (default) ------------------------------------------

    def _truncate_head(self, content: str, tool_call_id: str | None) -> str:
        """Keep the first ~2 KB as a preview, spill the rest."""
        return self._do_truncate(content, tool_call_id, tail=False)

    # -- tail truncation (shell tools) --------------------------------------

    def _truncate_tail(self, content: str, tool_call_id: str | None) -> str:
        """Keep the last portion as a preview, spill the rest."""
        return self._do_truncate(content, tool_call_id, tail=True)

    # -- common -------------------------------------------------------------

    def _do_truncate(self, content: str, tool_call_id: str | None, *, tail: bool) -> str:
        self._lazy_cleanup()

        # Write full output to disk
        file_id = tool_call_id if tool_call_id else uuid.uuid4().hex
        spill_path = self._spill_dir / f"{file_id}.txt"
        self._spill_dir.mkdir(parents=True, exist_ok=True)
        try:
            spill_path.write_text(content, encoding="utf-8", errors="replace")
        except OSError:
            return content  # can't spill, keep original

        spill_ref = spill_path.resolve().as_posix()
        lines = content.splitlines()
        byte_size = len(content.encode("utf-8", errors="replace"))

        # Build in-memory preview
        if tail:
            # Take last PREVIEW_CHARS for tail truncation
            preview = content[-self.PREVIEW_CHARS:].lstrip("\n")
        else:
            preview = content[:self.PREVIEW_CHARS]

        direction = "tail" if tail else "head"
        return (
            f"<persisted-output>\n"
            f"Output truncated ({direction}). "
            f"{len(lines)} lines, {byte_size:,} bytes. "
            f"Full output:\n{spill_ref}\n\n"
            f"Preview:\n{preview}\n"
            f"</persisted-output>"
        )

    # -- lazy cleanup -------------------------------------------------------

    def _lazy_cleanup(self) -> None:
        """Delete spilled files older than *max_age*, at most once per interval."""
        now = time.time()
        if now - self._last_cleanup < self._cleanup_interval:
            return
        self._last_cleanup = now
        if not self._spill_dir.exists():
            return
        cutoff = now - self._max_age
        for f in self._spill_dir.iterdir():
            if f.is_file() and f.stat().st_mtime < cutoff:
                f.unlink(missing_ok=True)
