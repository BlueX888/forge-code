"""Multi-tier context window management.

Tier flow:
  Pre-entry  →  Tier 1 (budget)  →  Tier 2 (snip)  →  Tier 3 (micro-compact)  →  Tier 4 (LLM summary)
"""

from __future__ import annotations

import json
import os
import re
import time
import uuid
from collections import deque
from pathlib import Path
from typing import TYPE_CHECKING, Callable

if TYPE_CHECKING:
    from coding_agent.config import AgentConfig
    from coding_agent.context import Message


# Type alias: summarize callback takes text, returns summary or None on failure.
SummarizeFn = Callable[[str], str | None]


# ---------------------------------------------------------------------------
# TokenBudget — anchor-based token usage estimation
# ---------------------------------------------------------------------------

class TokenBudget:
    """Token budget tracker using API anchor + chars/4 incremental estimation."""

    def __init__(self, max_tokens: int, *, overhead_estimate: int = 2000) -> None:
        self._max_tokens = max(max_tokens, 1)
        self._overhead = overhead_estimate
        self._anchor_tokens: int | None = None
        self._post_anchor_chars: int = 0

    def update_anchor(self, input_tokens: int) -> None:
        """Refresh anchor from the latest API response's input_tokens."""
        self._anchor_tokens = input_tokens
        self._post_anchor_chars = 0

    def notify_message_added(self, msg: Message) -> None:
        """Accumulate post-anchor character count when a message is enqueued."""
        self._post_anchor_chars += self._message_chars(msg)

    def invalidate_anchor(self) -> None:
        """Called when history is replaced (auto-compaction)."""
        self._anchor_tokens = None
        self._post_anchor_chars = 0

    def compute_usage(self, messages: deque[Message]) -> int:
        if self._anchor_tokens is not None:
            return self._anchor_tokens + self._post_anchor_chars // 4
        # Fallback: full char-based estimation
        total_chars = sum(self._message_chars(m) for m in messages)
        return self._overhead + total_chars // 4

    def usage_ratio(self, messages: deque[Message]) -> float:
        return min(self.compute_usage(messages) / self._max_tokens, 1.0)

    @staticmethod
    def _message_chars(msg: Message) -> int:
        chars = len(msg.content or "")
        if msg.reasoning_content:
            chars += len(msg.reasoning_content)
        if msg.tool_calls:
            for tc in msg.tool_calls:
                chars += len(tc.tool_name)
                chars += len(json.dumps(tc.arguments, default=str))
        return chars


# ---------------------------------------------------------------------------
# PreEntryProcessor — oversized result handling before entering deque
# ---------------------------------------------------------------------------

class PreEntryProcessor:
    """Handles oversized tool output before it enters the history deque."""

    _SPILL_THRESHOLD_BYTES = 30_000
    _TRUNCATE_THRESHOLD_CHARS = 50_000
    _HEAD_LINES = 50
    _TAIL_LINES = 20
    _CLEANUP_AGE_SECONDS = 86_400  # 24 hours

    def __init__(self, spill_dir: Path) -> None:
        self._spill_dir = spill_dir
        self._initialized = False

    def _ensure_dir(self) -> None:
        if not self._initialized:
            self._spill_dir.mkdir(parents=True, exist_ok=True)
            self._cleanup_old_files()
            self._initialized = True

    def _cleanup_old_files(self) -> None:
        if not self._spill_dir.exists():
            return
        cutoff = time.time() - self._CLEANUP_AGE_SECONDS
        for f in self._spill_dir.iterdir():
            if f.is_file() and f.stat().st_mtime < cutoff:
                f.unlink(missing_ok=True)

    def process(self, output: str, tool_name: str) -> str:
        encoded = output.encode("utf-8", errors="replace")

        # Disk spill for very large byte payloads
        if len(encoded) > self._SPILL_THRESHOLD_BYTES:
            self._ensure_dir()
            spill_path = self._spill_dir / f"{uuid.uuid4().hex}.txt"
            spill_path.write_bytes(encoded)
            lines = output.splitlines(keepends=True)
            preview = "".join(lines[: self._HEAD_LINES])
            return (
                f"{preview}\n"
                f"[output truncated — {len(encoded):,} bytes total]\n"
                f"[full output saved to: {spill_path}]"
            )

        # Head+tail truncation for large char payloads
        if len(output) > self._TRUNCATE_THRESHOLD_CHARS:
            lines = output.splitlines(keepends=True)
            head = "".join(lines[: self._HEAD_LINES])
            tail = "".join(lines[-self._TAIL_LINES :]) if len(lines) > self._HEAD_LINES + self._TAIL_LINES else ""
            marker = f"\n[truncated — {len(output):,} chars, {len(lines)} lines total]\n"
            return head + marker + tail

        return output


# ---------------------------------------------------------------------------
# BudgetTruncator (Tier 1) — dynamic per-result budget
# ---------------------------------------------------------------------------

class BudgetTruncator:
    """Caps tool output length based on current context usage ratio."""

    _THRESHOLDS = [
        (0.85, 5_000),
        (0.70, 15_000),
        (0.50, 30_000),
    ]

    def apply(self, output: str, usage_ratio: float) -> str:
        cap = None
        for threshold, limit in self._THRESHOLDS:
            if usage_ratio >= threshold:
                cap = limit
                break

        if cap is None or len(output) <= cap:
            return output

        lines = output.splitlines(keepends=True)
        head_lines = 40
        tail_lines = 10
        head = "".join(lines[:head_lines])
        tail = "".join(lines[-tail_lines:]) if len(lines) > head_lines + tail_lines else ""

        # Ensure we actually truncate to roughly the cap
        if len(head) + len(tail) > cap:
            head = output[: cap - 200]
            tail = ""

        marker = f"\n[budget-truncated from {len(output):,} to ~{cap:,} chars at {usage_ratio:.0%} usage]\n"
        return head + marker + tail


# ---------------------------------------------------------------------------
# SnipProcessor (Tier 2) — deduplicate stale content
# ---------------------------------------------------------------------------

class SnipProcessor:
    """Replaces duplicate/superseded content with short markers."""

    _MAX_SEARCH_RESULTS = 3

    def process(self, history: deque[Message]) -> int:
        snipped = 0
        snipped += self._snip_duplicate_reads(history)
        snipped += self._snip_old_searches(history)
        return snipped

    def _snip_duplicate_reads(self, history: deque[Message]) -> int:
        # Map tool_call_id → path from assistant tool_calls
        id_to_path: dict[str, str] = {}
        for msg in history:
            if msg.role == "assistant" and msg.tool_calls:
                for tc in msg.tool_calls:
                    if tc.tool_name == "read_file" and tc.id:
                        path = tc.arguments.get("path", "")
                        if path:
                            id_to_path[tc.id] = path

        # Group tool results by path
        path_to_msgs: dict[str, list[Message]] = {}
        for msg in history:
            if msg.role == "tool" and msg.tool_call_id and msg.tool_name == "read_file":
                path = id_to_path.get(msg.tool_call_id, "")
                if path:
                    path_to_msgs.setdefault(path, []).append(msg)

        # Keep only the latest read for each path
        snipped = 0
        for path, msgs in path_to_msgs.items():
            if len(msgs) <= 1:
                continue
            for older in msgs[:-1]:
                if not older.content.startswith("["):
                    older.content = f"[previously read {path} — see latest read below]"
                    snipped += 1
        return snipped

    def _snip_old_searches(self, history: deque[Message]) -> int:
        search_tools = {"search_text", "grep_search", "search_files"}
        search_msgs: list[Message] = []
        for msg in history:
            if msg.role == "tool" and msg.tool_name in search_tools:
                search_msgs.append(msg)

        snipped = 0
        if len(search_msgs) > self._MAX_SEARCH_RESULTS:
            for older in search_msgs[: -self._MAX_SEARCH_RESULTS]:
                if not older.content.startswith("["):
                    older.content = "[superseded search result]"
                    snipped += 1
        return snipped


# ---------------------------------------------------------------------------
# MicroCompactor (Tier 3) — whitespace compression for old messages
# ---------------------------------------------------------------------------

class MicroCompactor:
    """Compresses whitespace in older tool messages."""

    _MULTI_BLANK = re.compile(r"\n{3,}")
    _MULTI_SPACE = re.compile(r"[^\S\n]{2,}")

    def process(self, history: deque[Message], *, recent_count: int = 10) -> int:
        if len(history) <= recent_count:
            return 0

        compacted = 0
        cutoff = len(history) - recent_count
        for idx, msg in enumerate(history):
            if idx >= cutoff:
                break
            if msg.role != "tool":
                continue
            if msg.content.startswith("["):
                continue

            original = msg.content
            text = self._MULTI_BLANK.sub("\n\n", original)
            text = text.rstrip()
            # Collapse internal multi-spaces but preserve leading indentation
            lines = text.split("\n")
            for i, line in enumerate(lines):
                stripped = line.lstrip()
                if not stripped:
                    continue
                indent = line[: len(line) - len(stripped)]
                stripped = self._MULTI_SPACE.sub(" ", stripped)
                lines[i] = indent + stripped
            text = "\n".join(lines)

            if text != original:
                msg.content = text
                compacted += 1
        return compacted


# ---------------------------------------------------------------------------
# AutoCompactor (Tier 4) — LLM-based summarization
# ---------------------------------------------------------------------------

class AutoCompactor:
    """Summarizes old messages via LLM when context is critically full."""

    _COOLDOWN_SECONDS = 60
    _KEEP_RECENT = 10

    def __init__(self) -> None:
        self._last_compact_time: float = 0.0

    def process(
        self,
        history: deque[Message],
        summarize_fn: SummarizeFn | None,
        replace_fn: Callable[[list[Message]], None],
    ) -> bool:
        if summarize_fn is None:
            return False

        if len(history) <= self._KEEP_RECENT:
            return False

        now = time.time()
        if now - self._last_compact_time < self._COOLDOWN_SECONDS:
            return False

        # Build text from old messages
        old_count = len(history) - self._KEEP_RECENT
        old_msgs = list(history)[:old_count]
        recent_msgs = list(history)[old_count:]

        parts: list[str] = []
        for msg in old_msgs:
            prefix = f"[{msg.role}]"
            if msg.tool_name:
                prefix = f"[tool:{msg.tool_name}]"
            parts.append(f"{prefix} {msg.content[:2000]}")

        text_to_summarize = "\n---\n".join(parts)

        prompt = (
            "Summarize the following conversation history concisely. "
            "Preserve key facts, decisions, file paths, and tool outcomes. "
            "Omit redundant details.\n\n" + text_to_summarize
        )

        summary = summarize_fn(prompt)
        if summary is None:
            return False

        from coding_agent.context import Message as Msg

        summary_msg = Msg(
            role="user",
            content=f"[Context summary of {old_count} earlier messages]\n{summary}",
        )
        replace_fn([summary_msg] + recent_msgs)
        self._last_compact_time = now
        return True


# ---------------------------------------------------------------------------
# ContextWindowManager — orchestrator
# ---------------------------------------------------------------------------

class ContextWindowManager:
    """Coordinates all context management tiers."""

    def __init__(
        self,
        config: AgentConfig,
        spill_dir: Path,
        summarize_fn: SummarizeFn | None = None,
    ) -> None:
        self._budget = TokenBudget(config.max_context_tokens)
        self._pre_entry = PreEntryProcessor(spill_dir)
        self._truncator = BudgetTruncator()
        self._snip = SnipProcessor()
        self._micro = MicroCompactor()
        self._auto = AutoCompactor()
        self._summarize_fn = summarize_fn
        self._idle_timeout = config.context_idle_timeout
        self._last_activity: float = time.time()

    # -- anchor management --------------------------------------------------

    def update_anchor(self, input_tokens: int) -> None:
        self._budget.update_anchor(input_tokens)

    def notify_message_added(self, msg: Message) -> None:
        self._budget.notify_message_added(msg)

    def invalidate_anchor(self) -> None:
        self._budget.invalidate_anchor()

    # -- tier processing ----------------------------------------------------

    def process_before_add(
        self,
        content: str,
        tool_name: str,
        history: deque[Message],
    ) -> str:
        """Pre-entry + Tier 1: process content before it enters the deque."""
        content = self._pre_entry.process(content, tool_name)
        ratio = self._budget.usage_ratio(history)
        content = self._truncator.apply(content, ratio)
        return content

    def after_add(
        self,
        history: deque[Message],
        replace_fn: Callable[[list[Message]], None],
    ) -> None:
        """Tier 2 + Tier 4: run after a message is added."""
        self._touch()
        self._snip.process(history)
        ratio = self._budget.usage_ratio(history)
        if ratio > 0.85:
            self._auto.process(history, self._summarize_fn, replace_fn)

    def check_idle(self, history: deque[Message]) -> None:
        """Tier 3: compress whitespace if idle long enough."""
        if time.time() - self._last_activity >= self._idle_timeout:
            self._micro.process(history)

    def _touch(self) -> None:
        self._last_activity = time.time()
