"""Layer 3: LLM-based semantic compaction.

Triggered when context overflow persists after Layer 2 pruning.  Uses an
LLM call to produce a structured summary of older conversation turns while
preserving recent context verbatim.

Flow:
  1. *select()* — Partition history into head (to compact) and tail (keep).
  2. *buildPrompt()* — Construct the LLM prompt with or without prior summary.
  3. *compact()* — Call the LLM, parse the structured summary, inject it.
  4. Auto-continue or replay based on overflow state.
"""

from __future__ import annotations

from collections import deque
from typing import TYPE_CHECKING, Callable

if TYPE_CHECKING:
    from main.context import Message


# ---------------------------------------------------------------------------
# Compacter (Layer 3)
# ---------------------------------------------------------------------------

class Compactor:
    """Semantic compaction of conversation history via LLM.

    Compaction operates on *turns* (user → assistant roundtrips).  Older
    turns are compressed into a 7-section structured summary.
    """

    SUMMARY_SECTIONS = [
        "Goal",
        "Constraints",
        "Progress",
        "Key Decisions",
        "Next Steps",
        "Critical Context",
        "Relevant Files",
    ]

    def __init__(
        self,
        *,
        compaction_buffer: int = 20_000,
        tail_budget_ratio: float = 0.25,
        tail_clamp_min: int = 2_000,
        tail_clamp_max: int = 8_000,
        tail_min_turns: int = 2,
        tool_output_max_chars: int = 2_000,
    ) -> None:
        self._compaction_buffer = compaction_buffer
        self._tail_budget_ratio = tail_budget_ratio
        self._tail_clamp_min = tail_clamp_min
        self._tail_clamp_max = tail_clamp_max
        self._tail_min_turns = tail_min_turns
        self._tool_output_max_chars = tool_output_max_chars

    # -- Step 1: select head/tail split ------------------------------------

    def select(
        self,
        history: deque[Message],
        max_context_tokens: int,
    ) -> tuple[list[Message], list[Message]] | None:
        """Split history into head (to compact) and tail (to keep verbatim).

        Returns ``(head, tail)`` or ``None`` when head is too small to warrant
        compaction (the ``"stop"`` outcome).
        """
        usable = max_context_tokens - self._compaction_buffer
        tail_budget = max(self._tail_clamp_min, min(
            int(usable * self._tail_budget_ratio),
            self._tail_clamp_max,
        ))

        tail: list[Message] = []
        tail_tokens = 0
        turns_in_tail = 0

        # Accumulate from newest to oldest
        for msg in reversed(history):
            tokens = len(msg.content or "") // 4
            if tail_tokens + tokens <= tail_budget:
                tail.insert(0, msg)
                tail_tokens += tokens
                if msg.role == "user":
                    turns_in_tail += 1
            else:
                # Try to split within the current turn at message boundary
                if turns_in_tail < self._tail_min_turns and msg.role == "user":
                    tail.insert(0, msg)
                    tail_tokens += tokens
                    turns_in_tail += 1
                else:
                    break

        # Fallback: keep at least 1 turn
        if not tail and history:
            last = list(history)[-1]
            tail.append(last)

        # Head is everything before tail
        head_end = len(history) - len(tail)
        head = list(history)[:head_end]

        # Head must have at least 1 turn worth compressing
        if not head or not any(m.role == "user" for m in head):
            return None

        return head, tail

    # -- Step 2: build prompt ----------------------------------------------

    def build_prompt(self, head: list[Message], previous_summary: str | None = None) -> str:
        """Build the LLM prompt for compaction."""
        if previous_summary:
            return self._build_incremental_prompt(head, previous_summary)
        return self._build_first_time_prompt(head)

    def _build_first_time_prompt(self, head: list[Message]) -> str:
        sections_text = "\n".join(f"## {s}" for s in self.SUMMARY_SECTIONS)
        history_text = self._format_head(head)
        return (
            "You are a conversation summarizer. Your ONLY task is to create a "
            "structured summary. Do NOT greet. Do NOT engage in conversation. "
            "Do NOT ask questions. Output ONLY the summary content, nothing else.\n\n"
            "Create a new anchored summary from the conversation history below.\n\n"
            f"{history_text}\n\n"
            "Output in the following 7 sections:\n"
            f"{sections_text}"
        )

    def _build_incremental_prompt(self, head: list[Message], previous_summary: str) -> str:
        sections_text = "\n".join(f"## {s}" for s in self.SUMMARY_SECTIONS)
        history_text = self._format_head(head)
        return (
            "You are a conversation summarizer. Your ONLY task is to update a "
            "structured summary. Do NOT greet. Do NOT engage in conversation. "
            "Do NOT ask questions. Output ONLY the updated summary, nothing else.\n\n"
            "Below is the previous summary and new conversation history.\n"
            "Preserve still-true details, remove stale details, merge in new facts.\n\n"
            f"Previous summary:\n{previous_summary}\n\n"
            f"New conversation:\n{history_text}\n\n"
            "Output the updated summary in the same 7 sections:\n"
            f"{sections_text}"
        )

    def _format_head(self, head: list[Message]) -> str:
        """Format head messages into text, truncating tool outputs.

        Skips messages already replaced by pruning (``compacted=True``) to
        avoid injecting meaningless ``[tool result pruned]`` placeholders into
        the compaction prompt.
        """
        parts: list[str] = []
        for msg in head:
            if msg.compacted:
                continue  # skip pruned placeholders
            prefix = f"[{msg.role}]"
            if msg.tool_name:
                prefix = f"[tool:{msg.tool_name}]"
            content = msg.content or ""
            if msg.role == "tool" and len(content) > self._tool_output_max_chars:
                content = content[:self._tool_output_max_chars] + "..."
            if content.strip():
                parts.append(f"{prefix} {content}")
        return "\n---\n".join(parts)

    # -- Step 3: call compaction LLM ---------------------------------------

    def compact(
        self,
        history: deque[Message],
        max_context_tokens: int,
        summarize_fn: Callable[[str], str | None] | None,
    ) -> dict:
        """Run the full compaction flow.

        Returns a result dict:
          - ``"status"``: ``"continue"`` | ``"stop"`` | ``"compact"``
          - ``"summary"``: summary text (if successful)
          - ``"tail"``: tail messages (if successful)
          - ``"head"``: head messages (if ``"continue"``)
        """
        if summarize_fn is None:
            return {"status": "stop"}

        result = self.select(history, max_context_tokens)
        if result is None:
            return {"status": "stop"}

        head, tail = result

        # Find previous summary if one exists
        previous_summary: str | None = None
        for msg in reversed(head):
            if msg.summary:
                previous_summary = msg.content
                break

        prompt = self.build_prompt(head, previous_summary=previous_summary)
        summary = summarize_fn(prompt)

        if summary is None:
            return {"status": "stop"}

        # Validate summary quality — reject conversational or malformed output
        if not self._validate_summary(summary):
            return {"status": "stop"}

        # Check if summary itself is too large (recursive compaction needed)
        summary_tokens = len(summary) // 4
        available = max_context_tokens - self._compaction_buffer - (sum(len(m.content or "") // 4 for m in tail))
        if summary_tokens > available:
            return {"status": "compact", "summary": summary, "tail": tail, "head": head}

        return {
            "status": "continue",
            "summary": summary,
            "head": head,
            "tail": tail,
        }

    @staticmethod
    def _validate_summary(text: str) -> bool:
        """Return True only if *text* looks like a valid structured summary.

        Rejects:
        - Empty or whitespace-only output
        - Suspiciously short output (< 100 chars)
        - Conversational openers (greetings, offers to help, etc.)
        - Output that contains no Markdown section headers (``##``)
        """
        if not text or not text.strip():
            return False
        stripped = text.strip()
        if len(stripped) < 100:
            return False
        # Reject conversational openers
        conversational_prefixes = [
            "hello", "hi ", "hi,", "hi!", "hey", "greetings",
            "你好", "您好", "嗨",
            "i'll ", "i will ", "let me ", "sure,", "sure!",
            "okay", "ok,", "ok!", "of course",
        ]
        lower = stripped.lower()
        for prefix in conversational_prefixes:
            if lower.startswith(prefix):
                return False
        # Must contain at least one section header
        if "##" not in stripped:
            return False
        return True

    # -- Step 4: inject structured summary ---------------------------------

    @staticmethod
    def inject_summary(
        history: deque[Message],
        summary: str,
        tail: list[Message],
        *,
        overflow: bool = False,
    ) -> Message:
        """Inject a summary message and return the compaction metadata message.

        Replaces *history* content with::

            [compaction metadata msg] ← role="compaction"
            [summary assistant msg]   ← summary=True, content=summary
            [tail messages ...]       ← preserved verbatim

        Returns the compaction metadata message.
        """
        from main.context import Message as Msg

        # Build compaction metadata
        compaction_msg = Msg(
            role="compaction",
            content="compaction",
        )

        # Build summary message
        summary_msg = Msg(
            role="assistant",
            content=summary,
            summary=True,
        )

        # Clear and rebuild history
        history.clear()
        history.append(compaction_msg)
        history.append(summary_msg)
        for m in tail:
            history.append(m)

        return compaction_msg
