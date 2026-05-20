"""Session-level token usage tracking."""

from __future__ import annotations

import dataclasses
from typing import Any

from coding_agent.tools.base import ModelResponse


@dataclasses.dataclass
class TurnUsage:
    """Token usage for a single agent turn (may include multiple API calls)."""

    input_tokens: int = 0
    output_tokens: int = 0

    @property
    def total(self) -> int:
        return self.input_tokens + self.output_tokens


class TokenUsageTracker:
    """Accumulates token usage across an entire session."""

    def __init__(self, max_context_tokens: int) -> None:
        self._max_context = max_context_tokens
        self._session_input: int = 0
        self._session_output: int = 0
        self._current_turn = TurnUsage()
        self._turn_count: int = 0
        self._last_context_used: int = 0

    # -- turn lifecycle -----------------------------------------------------

    def begin_turn(self) -> None:
        """Mark the start of a new agent turn."""
        self._current_turn = TurnUsage()

    def record(self, response: ModelResponse) -> None:
        """Record token counts from a single API response."""
        inp = response.input_tokens or 0
        out = response.output_tokens or 0
        self._current_turn.input_tokens += inp
        self._current_turn.output_tokens += out
        self._session_input += inp
        self._session_output += out
        if response.input_tokens is not None:
            self._last_context_used = response.input_tokens

    def end_turn(self) -> TurnUsage:
        """Finalise the current turn and return its usage."""
        self._turn_count += 1
        return self._current_turn

    # -- accessors ----------------------------------------------------------

    @property
    def current_turn(self) -> TurnUsage:
        return self._current_turn

    @property
    def session_input(self) -> int:
        return self._session_input

    @property
    def session_output(self) -> int:
        return self._session_output

    @property
    def session_total(self) -> int:
        return self._session_input + self._session_output

    @property
    def turn_count(self) -> int:
        return self._turn_count

    @property
    def context_used(self) -> int:
        return self._last_context_used

    @property
    def context_remaining(self) -> int:
        return max(0, self._max_context - self._last_context_used)

    @property
    def context_usage_ratio(self) -> float:
        if self._max_context <= 0:
            return 0.0
        return min(self._last_context_used / self._max_context, 1.0)

    @property
    def max_context(self) -> int:
        return self._max_context

    # -- serialisation (for session persistence) ----------------------------

    def to_dict(self) -> dict[str, Any]:
        return {
            "session_input": self._session_input,
            "session_output": self._session_output,
            "turn_count": self._turn_count,
            "last_context_used": self._last_context_used,
        }

    def load_from_dict(self, d: dict[str, Any]) -> None:
        """Restore accumulated stats (e.g. when resuming a session)."""
        self._session_input = d.get("session_input", 0)
        self._session_output = d.get("session_output", 0)
        self._turn_count = d.get("turn_count", 0)
        self._last_context_used = d.get("last_context_used", 0)
