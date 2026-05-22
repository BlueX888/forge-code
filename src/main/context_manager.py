"""3-layer context window management.

Replaces the previous 4-tier system with a progressive compression pipeline:

  Layer 1 (Truncation) — per-tool output truncation, sync on every tool result
  Layer 2 (Pruning)    — reverse-scan replacement of old tool results, zero LLM cost
  Layer 3 (Compaction) — LLM-based semantic summarisation of older turns
"""

from __future__ import annotations

import concurrent.futures
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
# TokenBudget — token estimation with real API usage support
# ---------------------------------------------------------------------------


class TokenBudget:
    """Token usage tracker.

    Uses the real ``input_tokens`` value returned by the API when available;
    falls back to a char-length estimate otherwise.
    """

    # Rough overhead per message (role markers, metadata, API protobuf framing)
    _PER_MESSAGE_OVERHEAD = 12  # tokens

    def __init__(self, max_tokens: int, *, overhead_estimate: int = 4000) -> None:
        self._max_tokens = max(max_tokens, 1)
        self._overhead = overhead_estimate
        self._actual_usage: int | None = None  # real value from last API response

    def set_actual_usage(self, tokens: int) -> None:
        """Store the real input_tokens value returned by the API."""
        self._actual_usage = tokens

    def _estimate_message_tokens(self, msg: Message) -> int:
        """Estimate token count for a single message including hidden fields."""
        total = 0

        # Main content
        if msg.content:
            total += len(msg.content) // 4

        # Reasoning / thinking content
        if msg.reasoning_content:
            total += len(msg.reasoning_content) // 4
        if msg.thinking_blocks:
            for block in msg.thinking_blocks:
                thinking_text = block.get("thinking", "")
                if isinstance(thinking_text, str):
                    total += len(thinking_text) // 4

        # Tool call metadata (function names, arguments JSON, IDs)
        if msg.tool_calls:
            for tc in msg.tool_calls:
                total += len(tc.tool_name) // 4  # function name
                total += len(str(tc.arguments)) // 4  # arguments JSON
                if tc.id:
                    total += len(tc.id) // 4  # tool call id

        # Per-message API overhead
        total += self._PER_MESSAGE_OVERHEAD

        return total

    def estimate_usage(self, messages: list[Message]) -> int:
        """Estimate token count from messages, ignoring any cached API value."""
        total_tokens = sum(self._estimate_message_tokens(m) for m in messages)
        return self._overhead + total_tokens

    def compute_usage(self, messages: deque[Message]) -> int:
        # Prefer the real value from the last API response
        if self._actual_usage is not None:
            return self._actual_usage
        # Fall back to char-based estimation
        return self.estimate_usage(list(messages))

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
        self._budget = TokenBudget(
            config.max_context_tokens,
            overhead_estimate=config.token_overhead_estimate,
        )

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
            protected_tools=config.prune_protected_tools,
        )
        self._prune_enabled = config.prune_enabled

        # Layer 3: Compaction
        self._compactor = Compactor(
            compaction_buffer=config.compaction_buffer_tokens,
            tail_budget_ratio=config.tail_budget_ratio,
            tail_clamp_min=config.tail_clamp_min,
            tail_clamp_max=config.tail_clamp_max,
            tail_min_turns=config.tail_min_turns,
            tool_output_max_chars=config.tool_output_max_chars,
        )
        if summarize_fn is not None:
            self._summarize_fn = self._make_timeout_summarizer(
                summarize_fn, config.compaction_timeout,
            )
        else:
            self._summarize_fn = None
        self._max_context_tokens = config.max_context_tokens
        self._trigger_ratio = config.compaction_trigger_ratio
        self._lightweight_turn_threshold = config.lightweight_turn_threshold
        # Deferred compaction state
        self._compaction_pending: bool = False

    # -- real token usage ---------------------------------------------------

    def set_actual_usage(self, input_tokens: int) -> None:
        """Forward real API token usage to the TokenBudget."""
        self._budget.set_actual_usage(input_tokens)

    def estimate_context_usage(self, messages: list[Message]) -> int:
        """Estimate token count of a message list, always from content (no cache)."""
        return self._budget.estimate_usage(messages)

    # -- Layer 1: Truncation (synchronous, every tool result) ---------------

    def process_before_add(
        self,
        content: str,
        tool_name: str,
        _history: deque[Message],
        tool_call_id: str | None = None,
    ) -> str:
        """Apply Layer 1 truncation before content enters the deque."""
        return self._truncator.truncate(content, tool_name, tool_call_id)

    # -- Layer 2 & 3: Pruning + Compaction (after message is added) ---------

    def after_add(
        self,
        history: deque[Message],
        replace_fn: Callable[[list[Message]], None],
    ) -> None:
        """Apply Layer 2 pruning; defer Layer 3 compaction if overflow persists."""
        # Lightweight mode: skip Layer 2/3 for short conversations.
        # Layer 1 (truncation) remains active regardless.
        turn_count = sum(1 for m in history if m.role == "user")
        if turn_count <= self._lightweight_turn_threshold:
            return

        ratio = self._budget.usage_ratio(history)
        if ratio <= self._trigger_ratio:
            return

        # Layer 2: try pruning first (zero LLM cost)
        if self._prune_enabled:
            pruned = self._pruner.prune(history)
        else:
            pruned = False
        if pruned:
            ratio = self._budget.usage_ratio(history)
            if ratio <= self._trigger_ratio:
                return

        # Layer 3: DEFER compaction — do NOT run synchronously here
        # Previously this was a blocking LLM call that froze the CLI.
        if self._summarize_fn is not None:
            self._compaction_pending = True

    # -- deferred compaction ------------------------------------------------

    def process_pending_compaction(
        self,
        history: deque[Message],
        replace_fn: Callable[[list[Message]], None],
    ) -> bool:
        """Process deferred compaction. Call this at the start of each turn.

        Returns True if compaction ran and replaced history.

        Handles recursive compaction when the summary itself exceeds the
        available budget (``"compact"`` status returned by
        :meth:`Compactor.compact`).
        """
        if not self._compaction_pending:
            return False

        if self._summarize_fn is None:
            self._compaction_pending = False
            return False

        # Re-check: maybe pruning already handled it during the interval
        ratio = self._budget.usage_ratio(history)
        if ratio <= self._trigger_ratio:
            self._compaction_pending = False
            return False

        result = self._compactor.compact(
            history,
            self._max_context_tokens,
            self._summarize_fn,
        )

        if result["status"] == "compact":
            # Summary too large — recursively condense with a shorten-prompt
            summary = result["summary"]
            tail = result.get("tail", [])
            max_retries = 3
            for _ in range(max_retries):
                condense_prompt = (
                    "Condense the following summary to be shorter while preserving "
                    "all key information. Remove redundancy.\n\n"
                    f"{summary}"
                )
                condensed = self._summarize_fn(condense_prompt)
                if condensed is None:
                    break
                summary = condensed
                condensed_tokens = len(summary) // 4
                tail_tokens = sum(len(m.content or "") // 4 for m in tail)
                available = self._max_context_tokens - self._compactor._compaction_buffer - tail_tokens
                if condensed_tokens <= available:
                    result = {
                        "status": "continue",
                        "summary": summary,
                        "head": [],
                        "tail": tail,
                    }
                    break
            else:
                # Exhausted retries — give up, keep current history
                self._compaction_pending = False
                return False

        if result["status"] != "continue":
            self._compaction_pending = False
            return False

        # Clear pending flag only on success
        self._compaction_pending = False
        from main.compaction import Compactor as CompactorCls
        CompactorCls.inject_summary(history, result["summary"], result["tail"], overflow=False)
        replace_fn(list(history))
        return True

    @staticmethod
    def _make_timeout_summarizer(
        summarize_fn: SummarizeFn, timeout: int,
    ) -> SummarizeFn:
        """Wrap summarize_fn with a timeout. Returns None on timeout or failure.

        A single ``ThreadPoolExecutor`` is created once and reused across
        calls to avoid thread leaks from repeated ``shutdown(wait=False)``.
        """
        executor = concurrent.futures.ThreadPoolExecutor(max_workers=1)

        def _with_timeout(text: str) -> str | None:
            try:
                future = executor.submit(summarize_fn, text)
                return future.result(timeout=timeout)
            except concurrent.futures.TimeoutError:
                return None
            except Exception:
                return None

        return _with_timeout
