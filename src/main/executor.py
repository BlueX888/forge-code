"""Tool-call grouping for parallel execution of read-only tools."""

from __future__ import annotations

import dataclasses

from safety.permissions import SafetyLabel
from tools.base import ToolCall
from tools.registry import ToolRegistry


@dataclasses.dataclass(frozen=True)
class ToolCallGroup:
    """A batch of tool calls that share the same execution strategy."""

    parallel: bool
    tool_calls: list[ToolCall]


def group_tool_calls(
    tool_calls: list[ToolCall],
    registry: ToolRegistry,
) -> list[ToolCallGroup]:
    """Partition *tool_calls* into sequential groups for execution.

    Consecutive READONLY tools are grouped into a single ``parallel=True``
    group.  Any non-READONLY tool (DESTRUCTIVE, CONCURRENT_SAFE, or unknown)
    flushes the current READONLY batch and forms its own ``parallel=False``
    group.
    """
    if not tool_calls:
        return []

    groups: list[ToolCallGroup] = []
    readonly_batch: list[ToolCall] = []

    def _flush_readonly() -> None:
        if readonly_batch:
            groups.append(ToolCallGroup(parallel=True, tool_calls=list(readonly_batch)))
            readonly_batch.clear()

    for tc in tool_calls:
        tool = registry.get(tc.tool_name)
        if tool is not None and tool.safety_label == SafetyLabel.READONLY:
            readonly_batch.append(tc)
        else:
            _flush_readonly()
            groups.append(ToolCallGroup(parallel=False, tool_calls=[tc]))

    _flush_readonly()
    return groups
