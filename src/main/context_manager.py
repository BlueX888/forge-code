"""3-layer context window management.

Replaces the previous 4-tier system with a progressive compression pipeline:

  Layer 1 (Truncation) — per-tool output truncation, sync on every tool result
  Layer 2 (Pruning)    — reverse-scan replacement of old tool results, zero LLM cost
  Layer 3 (Compaction) — LLM-based semantic summarisation of older turns
"""

from __future__ import annotations

import time
from collections import deque
from pathlib import Path
from typing import TYPE_CHECKING, Callable

from main.truncation import ToolOutputTruncator
from main.pruning import ToolOutputPruner
from main.compaction import Compactor

if TYPE_CHECKING:
    from main.config import AgentConfig
    from main.context import Message


# Type alias: summarize callback takes text, returns summary or None on failure.
SummarizeFn = Callable[[str], str | None]


# ---------------------------------------------------------------------------
# TokenBudget — simple token estimation
# ---------------------------------------------------------------------------

class TokenBudget:
    """Rough token usage tracker based on char-length estimation."""

    def __init__(self, max_tokens: int, *, overhead_estimate: int = 2000) -> None:
        self._max_tokens = max(max_tokens, 1)
        self._overhead = overhead_estimate

    def compute_usage(self, messages: deque[Message]) -> int:
        total_chars = sum(
            len(m.content or "")
            for m in messages
        )
        return self._overhead + total_chars // 4

    def usage_ratio(self, messages: deque[Message]) -> float:
        return min(self.compute_usage(messages) / self._max_tokens, 1.0)


# ---------------------------------------------------------------------------
# ContextWindowManager — 3-layer orchestrator
# ---------------------------------------------------------------------------

class ContextWindowManager:
    """Coordinates the 3-layer context compression pipeline."""

    def __init__(
        self,
        config: AgentConfig,
        working_directory: Path,
        session_id: str,
        summarize_fn: SummarizeFn | None = None,
    ) -> None:
        self._budget = TokenBudget(config.max_context_tokens)

        # Layer 1: Truncation
        self._truncator = ToolOutputTruncator(
            spill_base=working_directory,
            session_id=session_id,
            line_threshold=config.truncate_line_threshold,
            byte_threshold=config.truncate_byte_threshold,
            cleanup_interval=config.truncate_cleanup_interval,
            max_age=config.truncate_max_age,
        )

        # Layer 2: Pruning
        self._pruner = ToolOutputPruner(
            protect_tokens=config.prune_protect_tokens,
            minimum_tokens=config.prune_minimum_tokens,
        )

        # Layer 3: Compaction
        self._compactor = Compactor(
            compaction_buffer=config.compaction_buffer_tokens,
            tail_budget_ratio=config.tail_budget_ratio,
            tail_clamp_min=config.tail_clamp_min,
            tail_clamp_max=config.tail_clamp_max,
            tail_min_turns=config.tail_min_turns,
            tool_output_max_chars=config.tool_output_max_chars,
        )

        self._summarize_fn = summarize_fn
        self._max_context_tokens = config.max_context_tokens
        self._idle_timeout = config.context_idle_timeout
        self._last_activity: float = time.time()

    # -- Layer 1: Truncation (synchronous, every tool result) ---------------

    def process_before_add(
        self,
        content: str,
        tool_name: str,
        _history: deque[Message],
        tool_call_id: str | None = None,
    ) -> str:
        """Apply Layer 1 truncation before content enters the deque."""
        content = self._truncator.truncate(content, tool_name, tool_call_id)
        return content

    # -- Layer 2 & 3: Pruning + Compaction (after message is added) ---------

    def after_add(
        self,
        history: deque[Message],
        replace_fn: Callable[[list[Message]], None],
    ) -> None:
        """Apply Layer 2 pruning and trigger Layer 3 compaction if needed."""
        self._touch()

        # Check whether we are overflowing
        ratio = self._budget.usage_ratio(history)
        if ratio <= 0.85:
            return

        # Layer 2: try pruning first (zero LLM cost)
        pruned = self._pruner.prune(history)
        if pruned:
            # Re-check after pruning
            ratio = self._budget.usage_ratio(history)
            if ratio <= 0.85:
                return

        # Layer 3: semantic compaction
        if self._summarize_fn is None:
            return

        result = self._compactor.compact(
            history,
            self._max_context_tokens,
            self._summarize_fn,
        )

        if result["status"] != "continue":
            return

        summary = result["summary"]
        tail = result["tail"]

        # Inject the summary into history
        from main.compaction import Compactor as CompactorCls
        CompactorCls.inject_summary(history, summary, tail, overflow=False)

        # Notify the runtime via replace callback
        replace_fn(list(history))

    # -- idle compression ---------------------------------------------------

    def check_idle(self, history: deque[Message]) -> None:
        """No-op in the new design; idle checks are handled by Layer 2/3 flow."""
        pass

    # -- anchor management (deprecated, kept for backward compat) -----------

    def update_anchor(self, input_tokens: int) -> None:
        """No-op: anchor-based estimation is replaced by simple char estimation."""
        pass

    def notify_message_added(self, msg: Message) -> None:
        """No-op: cost tracking is handled at the runtime level."""
        pass

    def invalidate_anchor(self) -> None:
        """No-op: anchor-based estimation is replaced by simple char estimation."""
        pass

    # -- helpers ------------------------------------------------------------

    def _touch(self) -> None:
        self._last_activity = time.time()
