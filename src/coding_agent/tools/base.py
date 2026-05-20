"""Tool protocol and shared data types."""

from __future__ import annotations

import dataclasses
from typing import Any, Protocol, runtime_checkable

from coding_agent.config import AgentConfig
from coding_agent.permissions import SafetyLabel


# ---------------------------------------------------------------------------
# Data types
# ---------------------------------------------------------------------------

@dataclasses.dataclass(frozen=True)
class ToolResult:
    """Outcome of a single tool execution."""

    success: bool
    output: str
    error: str | None = None


@dataclasses.dataclass(frozen=True)
class ToolCall:
    """A request to invoke a tool with specific arguments."""

    tool_name: str
    arguments: dict[str, Any]
    id: str | None = None


@dataclasses.dataclass(frozen=True)
class ModelResponse:
    """Response from a model client."""

    text: str
    tool_calls: list[ToolCall] = dataclasses.field(default_factory=list)
    stop_reason: str | None = None
    reasoning_content: str | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None


# ---------------------------------------------------------------------------
# Tool protocol
# ---------------------------------------------------------------------------

@runtime_checkable
class Tool(Protocol):
    """Structural interface every tool must satisfy."""

    @property
    def name(self) -> str: ...

    @property
    def description(self) -> str: ...

    @property
    def parameters_schema(self) -> dict[str, Any]: ...

    @property
    def safety_label(self) -> SafetyLabel: ...

    def run(self, *, arguments: dict[str, Any], config: AgentConfig) -> ToolResult: ...
