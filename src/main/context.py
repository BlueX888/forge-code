"""Context builder — assembles messages for model calls."""

from __future__ import annotations

import dataclasses
import copy
from collections import deque
from typing import TYPE_CHECKING, Any, Callable

from main.config import AgentConfig
from main.prompts import SystemPromptBuilder
from tools.base import ToolCall, ToolResult
from tools.registry import ToolRegistry

if TYPE_CHECKING:
    from main.context_manager import ContextWindowManager


@dataclasses.dataclass
class Message:
    role: str  # "system" | "user" | "assistant" | "tool" | "compaction"
    content: str
    tool_calls: list[ToolCall] | None = None   # assistant messages with tool invocations
    tool_call_id: str | None = None            # tool result correlation id
    tool_name: str | None = None
    reasoning_content: str | None = None        # thinking/reasoning content (OpenAI-compatible APIs)
    summary: bool = False                       # marks summary messages from compaction
    compacted: bool = False                     # marks processed-by-pruning/compaction messages


class ContextBuilder:
    """Maintains conversation history and builds the full message list."""

    def __init__(
        self,
        config: AgentConfig,
        registry: ToolRegistry,
        *,
        on_message: Callable[[Message], None] | None = None,
        on_replace: Callable[[list[Message]], None] | None = None,
        window_manager: ContextWindowManager | None = None,
    ) -> None:
        self._config = config
        self._registry = registry
        self._history: deque[Message] = deque(maxlen=config.max_history_messages)
        self._on_message = on_message
        self._on_replace = on_replace
        self._window_manager = window_manager

    # -- mutators -----------------------------------------------------------

    def _append(self, msg: Message) -> None:
        self._history.append(msg)
        if self._window_manager is not None:
            self._window_manager.notify_message_added(msg)
        if self._on_message is not None:
            self._on_message(msg)

    def add_user_message(self, content: str) -> None:
        self._append(Message(role="user", content=content))

    def add_assistant_message(self, content: str, tool_calls: list[ToolCall] | None = None, reasoning_content: str | None = None) -> None:
        self._append(Message(role="assistant", content=content, tool_calls=tool_calls, reasoning_content=reasoning_content))

    def add_tool_result(self, tool_name: str, result: ToolResult, tool_call_id: str | None = None) -> None:
        content = result.output if result.success else f"Error: {result.error}"
        if self._window_manager is not None:
            content = self._window_manager.process_before_add(content, tool_name, self._history, tool_call_id=tool_call_id)
        self._append(Message(role="tool", content=content, tool_name=tool_name, tool_call_id=tool_call_id))
        if self._window_manager is not None:
            self._window_manager.after_add(self._history, self._replace_history)

    def load_history(self, messages: list[Message]) -> None:
        """Load persisted messages into the deque without triggering the callback."""
        for msg in messages:
            self._history.append(copy.deepcopy(msg))

    def history_snapshot(self) -> list[Message]:
        """Return a detached copy of the current model context history."""
        return [copy.deepcopy(msg) for msg in self._history]

    def _replace_history(self, messages: list[Message]) -> None:
        """Replace the entire history deque (used by Tier 4 compaction)."""
        self._history.clear()
        for msg in messages:
            self._history.append(msg)
        if self._window_manager is not None:
            self._window_manager.invalidate_anchor()
        if self._on_replace is not None:
            self._on_replace(messages)

    def check_idle_compression(self) -> None:
        """Delegate idle-based compression to the window manager (Tier 3)."""
        if self._window_manager is not None:
            self._window_manager.check_idle(self._history)

    def update_token_anchor(self, input_tokens: int) -> None:
        """Forward API usage anchor to the window manager."""
        if self._window_manager is not None:
            self._window_manager.update_anchor(input_tokens)

    # -- builders -----------------------------------------------------------

    def build(self) -> list[Message]:
        """Return ``[system_message, *history]``, filtering out compaction messages."""
        system = Message(role="system", content=self._build_system_prompt())
        # Filter out compaction messages — they are internal metadata, not API-visible
        visible = [m for m in self._history if m.role != "compaction"]
        return [system, *visible]

    def _build_system_prompt(self) -> str:
        builder = SystemPromptBuilder(self._config, self._registry)
        return builder.build()
