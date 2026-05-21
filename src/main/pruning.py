"""Layer 2: Reverse tool output pruning.

Zero LLM cost.  Scans messages from newest to oldest, replacing old tool
results with ``[tool result pruned]`` placeholders.  The most recent tool
output tokens (up to ``PRUNE_PROTECT``) are kept.  Pruning only executes
when at least ``PRUNE_MINIMUM`` tokens can be recovered.
"""

from __future__ import annotations

from collections import deque
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from main.context import Message


# ---------------------------------------------------------------------------
# Token estimation helper
# ---------------------------------------------------------------------------

def _message_tokens(msg: Message) -> int:
    """Rough token count for a message (chars // 4)."""
    return len(msg.content or "") // 4


# ---------------------------------------------------------------------------
# ToolOutputPruner (Layer 2)
# ---------------------------------------------------------------------------

class ToolOutputPruner:
    """Prunes old tool results from conversation history.

    Algorithm (reverse scan):
      1. Iterate messages from last to first.
      2. Skip the last 2 turns (always protected).
      3. Encounter ``summary=True`` → STOP immediately.
      4. Encounter ``role="tool"`` with ``compacted=False``:
         a. Cumulative tokens ≤ PRUNE_PROTECT → continue accumulating.
         b. Cumulative tokens > PRUNE_PROTECT → add to prune queue.
      5. After scan: if prune queue total > PRUNE_MINIMUM → execute.
         Otherwise → skip (not worth the churn).
    """

    def __init__(
        self,
        *,
        protect_tokens: int = 40_000,
        minimum_tokens: int = 20_000,
        turns_to_protect: int = 2,
    ) -> None:
        self._protect_tokens = protect_tokens
        self._minimum_tokens = minimum_tokens
        self._turns_to_protect = turns_to_protect

    def prune(self, history: deque[Message]) -> bool:
        """Run the pruning algorithm.

        Returns ``True`` if at least one message was pruned, ``False`` otherwise.
        """
        if len(history) < 2:
            return False

        prune_queue: list[Message] = []
        cumulative_tokens = 0
        turns_seen = 0
        scan_reversed = list(reversed(history))

        for msg in scan_reversed:
            # Track turns in reverse
            if msg.role == "user":
                turns_seen += 1

            # Always protect the last N turns
            if turns_seen <= self._turns_to_protect:
                # Still accumulate tokens for the protected region size check
                if msg.role == "tool" and not msg.compacted:
                    cumulative_tokens += _message_tokens(msg)
                continue

            # Summary message → stop scanning (content already compressed)
            if msg.summary:
                break

            # Candidate for pruning
            if msg.role == "tool" and not msg.compacted:
                tokens = _message_tokens(msg)
                if cumulative_tokens + tokens <= self._protect_tokens:
                    cumulative_tokens += tokens
                else:
                    prune_queue.append(msg)

        # Check minimum recovery threshold
        recovered = sum(_message_tokens(m) for m in prune_queue)
        if recovered < self._minimum_tokens:
            return False

        # Execute pruning
        for msg in prune_queue:
            msg.content = "[tool result pruned]"
            msg.compacted = True

        return True
