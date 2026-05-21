# Context Compression Strategy Redesign — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the current 4-tier context management with a 3-layer progressive compression system: Truncation (tool output) → Pruning (reverse scan) → Compaction (LLM semantic compression).

**Architecture:** Three new modules (`truncation.py`, `pruning.py`, `compaction.py`) each encapsulate one layer. `ContextWindowManager` is rewritten as a thin orchestrator. `ContextBuilder` gains compaction-aware message filtering. `Message` gets `summary` and `compacted` flags.

**Tech Stack:** Python 3.11+, dataclasses, pytest, existing `Message`/`ToolCall`/`ToolResult` types.

---

## File Map

| File | Action | Responsibility |
|------|--------|---------------|
| `src/main/context.py` | Modify | Add `summary`, `compacted` to `Message`; filter compaction in `build()` |
| `src/main/config.py` | Modify | Add Layer 1/2/3 config constants to `AgentConfig` |
| `src/main/truncation.py` | Create | `ToolOutputTruncator` — Layer 1 head truncation + disk spill + cleanup |
| `src/main/pruning.py` | Create | `ToolOutputPruner` — Layer 2 reverse-scan tool output replacement |
| `src/main/compaction.py` | Create | `Compactor` — Layer 3 LLM semantic compression (select/buildPrompt/LLM/inject) |
| `src/main/context_manager.py` | Rewrite | `ContextWindowManager` — thin orchestrator for all 3 layers |
| `src/main/runtime.py` | Modify | Add compaction message check at top of `_run_agent_turn` |
| `src/tools/shell.py` | Modify | `ShellTruncator` tail truncation in `RunCommandTool.run()` |
| `src/main/session.py` | Modify | Serialize `summary`/`compacted` fields in `_message_to_dict`/`_message_from_dict` |
| `tests/test_truncation.py` | Create | Unit tests for Layer 1 |
| `tests/test_pruning.py` | Create | Unit tests for Layer 2 |
| `tests/test_compaction.py` | Create | Unit tests for Layer 3 |
| `tests/test_context_manager.py` | Create | Integration tests for orchestrator |

---

### Task 1: Message model and config foundation

**Files:**
- Modify: `src/main/context.py:19-27`
- Modify: `src/main/config.py:270-298`
- Modify: `src/main/session.py:66-88`

- [ ] **Step 1: Add `summary` and `compacted` fields to `Message`**

```python
# src/main/context.py — replace the Message dataclass
@dataclasses.dataclass
class Message:
    role: str  # "system" | "user" | "assistant" | "tool" | "compaction"
    content: str
    tool_calls: list[ToolCall] | None = None
    tool_call_id: str | None = None
    tool_name: str | None = None
    reasoning_content: str | None = None
    summary: bool = False
    compacted: bool = False
```

- [ ] **Step 2: Add config constants to `AgentConfig`**

```python
# src/main/config.py — add to AgentConfig dataclass fields (after default_max_result_chars)
    # Layer 1: Truncation
    truncate_line_threshold: int = 2_000
    truncate_byte_threshold: int = 50_000
    truncate_cleanup_interval: int = 3_600
    truncate_max_age: int = 604_800
    # Layer 2: Pruning
    prune_protect: int = 40_000
    prune_minimum: int = 20_000
    # Layer 3: Compaction
    compaction_buffer: int = 20_000
    tail_budget_ratio: float = 0.25
    tail_clamp_min: int = 2_000
    tail_clamp_max: int = 8_000
    tail_min_turns: int = 2
    tool_output_max_chars: int = 2_000
```

Also update `from_file_and_args()` to read these from `agent_section`:

```python
# In from_file_and_args(), after tool_result_budget line:
truncate_line_threshold = int(agent_section.get("truncate_line_threshold", 2_000))
truncate_byte_threshold = int(agent_section.get("truncate_byte_threshold", 50_000))
truncate_cleanup_interval = int(agent_section.get("truncate_cleanup_interval", 3_600))
truncate_max_age = int(agent_section.get("truncate_max_age", 604_800))
prune_protect = int(agent_section.get("prune_protect", 40_000))
prune_minimum = int(agent_section.get("prune_minimum", 20_000))
compaction_buffer = int(agent_section.get("compaction_buffer", 20_000))
tail_budget_ratio = float(agent_section.get("tail_budget_ratio", 0.25))
tail_clamp_min = int(agent_section.get("tail_clamp_min", 2_000))
tail_clamp_max = int(agent_section.get("tail_clamp_max", 8_000))
tail_min_turns = int(agent_section.get("tail_min_turns", 2))
tool_output_max_chars = int(agent_section.get("tool_output_max_chars", 2_000))
```

Pass all to the `cls(...)` constructor call.

- [ ] **Step 3: Update session serialization for new Message fields**

```python
# src/main/session.py — _message_to_dict, add after "reasoning_content":
    d["summary"] = msg.summary
    d["compacted"] = msg.compacted

# _message_from_dict, add after reasoning_content:
    summary=d.get("summary", False),
    compacted=d.get("compacted", False),
```

- [ ] **Step 4: Run existing tests to verify no regressions**

```bash
cd F:/Forge-Code && python -m pytest tests/ -x -q
```

- [ ] **Step 5: Commit**

```bash
git add src/main/context.py src/main/config.py src/main/session.py
git commit -m "feat: add summary/compacted fields to Message and config constants for 3-layer compression"
```

---

### Task 2: Layer 1 — ToolOutputTruncator

**Files:**
- Create: `src/main/truncation.py`
- Create: `tests/test_truncation.py`

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_truncation.py
from pathlib import Path
from main.truncation import ToolOutputTruncator


def make_output(lines: int, bytes_per_line: int = 80) -> str:
    """Build output with exactly `lines` lines, each ~`bytes_per_line` bytes."""
    line = "x" * max(1, bytes_per_line)
    return "\n".join(line for _ in range(lines))


def test_should_not_truncate_when_under_both_thresholds(tmp_path):
    truncator = ToolOutputTruncator(tmp_path, "s1")
    output = make_output(lines=100, bytes_per_line=100)  # 100 lines, ~10KB
    assert not truncator.should_truncate(output)


def test_should_not_truncate_when_only_lines_exceeded(tmp_path):
    truncator = ToolOutputTruncator(tmp_path, "s1")
    output = make_output(lines=3000, bytes_per_line=10)  # 3000 lines, ~30KB
    assert not truncator.should_truncate(output)


def test_should_not_truncate_when_only_bytes_exceeded(tmp_path):
    truncator = ToolOutputTruncator(tmp_path, "s1")
    output = "x" * 60_000 + "\n" + "x" * 60_000  # 2 lines, ~120KB
    assert not truncator.should_truncate(output)


def test_should_truncate_when_both_exceeded(tmp_path):
    truncator = ToolOutputTruncator(tmp_path, "s1")
    output = make_output(lines=3000, bytes_per_line=100)  # 3000 lines, ~300KB
    assert truncator.should_truncate(output)


def test_truncate_head_saves_to_disk_and_returns_preview(tmp_path):
    truncator = ToolOutputTruncator(tmp_path, "s1")
    output = make_output(lines=3000, bytes_per_line=100)
    result = truncator.truncate(output, "read_file", "call_001")

    assert "<truncated-output>" in result
    assert "truncated/call_001.txt" in result

    spill_file = tmp_path / ".forgecode" / "sessions" / "s1" / "truncated" / "call_001.txt"
    assert spill_file.exists()
    assert spill_file.read_text(encoding="utf-8") == output


def test_truncate_head_keeps_beginning_content(tmp_path):
    truncator = ToolOutputTruncator(tmp_path, "s1")
    lines = [f"line_{i:04d}" for i in range(3000)]
    output = "\n".join(lines)
    result = truncator.truncate(output, "read_file", "call_002")

    # Head truncation: first lines should be in the result
    assert "line_0000" in result
    assert "line_0001" in result
    # Later lines should not be in the preview
    assert "line_2999" not in result


def test_cleanup_removes_old_files(tmp_path):
    import time
    truncator = ToolOutputTruncator(tmp_path, "s1")
    output = make_output(lines=3000, bytes_per_line=100)
    truncator.truncate(output, "test_tool", "call_old")
    spill_dir = tmp_path / ".forgecode" / "sessions" / "s1" / "truncated"
    spill_file = spill_dir / "call_old.txt"

    # Manually set mtime to 8 days ago
    old_time = time.time() - 8 * 86400
    os.utime(spill_file, (old_time, old_time))

    # Force cleanup by setting _last_cleanup to 0
    truncator._last_cleanup = 0.0
    # Trigger cleanup via truncate (which calls _ensure_dir)
    truncator.truncate(output, "test_tool", "call_new")
    assert not spill_file.exists()
    assert (spill_dir / "call_new.txt").exists()
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
cd F:/Forge-Code && python -m pytest tests/test_truncation.py -v
```
Expected: all FAIL with `ModuleNotFoundError` or `ImportError`

- [ ] **Step 3: Implement `ToolOutputTruncator`**

```python
# src/main/truncation.py
"""Layer 1: Per-tool output truncation with disk spill."""

from __future__ import annotations

import time
import uuid
from pathlib import Path


class ToolOutputTruncator:
    """Preventive tool output truncation (AND logic: lines + bytes)."""

    _PREVIEW_CHARS = 2_048

    def __init__(self, base_dir: Path, session_id: str) -> None:
        self._spill_dir = base_dir / ".forgecode" / "sessions" / session_id / "truncated"
        self._last_cleanup: float = 0.0
        self._initialized = False

    def _ensure_dir(self) -> None:
        if not self._initialized:
            self._spill_dir.mkdir(parents=True, exist_ok=True)
            self._initialized = True
        self._maybe_cleanup()

    def _maybe_cleanup(self) -> None:
        now = time.time()
        if now - self._last_cleanup < 3_600:  # 1 hour
            return
        self._last_cleanup = now
        if not self._spill_dir.exists():
            return
        cutoff = now - 604_800  # 7 days
        for f in self._spill_dir.iterdir():
            if f.is_file() and f.stat().st_mtime < cutoff:
                f.unlink(missing_ok=True)

    @staticmethod
    def should_truncate(output: str, *,
                        line_threshold: int = 2_000,
                        byte_threshold: int = 50_000) -> bool:
        """AND: both lines > line_threshold AND bytes > byte_threshold."""
        if len(output.encode("utf-8", errors="replace")) <= byte_threshold:
            return False
        line_count = output.count("\n") + (0 if output.endswith("\n") else 1)
        return line_count > line_threshold

    def truncate(self, output: str, tool_name: str,
                 tool_call_id: str | None = None,
                 *,
                 line_threshold: int = 2_000,
                 byte_threshold: int = 50_000) -> str:
        """Head truncation: keep beginning, spill to disk, return preview."""
        if not self.should_truncate(output,
                                    line_threshold=line_threshold,
                                    byte_threshold=byte_threshold):
            return output

        self._ensure_dir()
        file_id = tool_call_id if tool_call_id else uuid.uuid4().hex
        spill_path = self._spill_dir / f"{file_id}.txt"
        try:
            spill_path.write_text(output, encoding="utf-8", errors="replace")
        except OSError:
            pass

        size_bytes = len(output.encode("utf-8", errors="replace"))
        if size_bytes >= 1_048_576:
            size_str = f"{size_bytes / 1_048_576:.1f} MB"
        elif size_bytes >= 1024:
            size_str = f"{size_bytes / 1024:.1f} KB"
        else:
            size_str = f"{size_bytes} B"

        preview = output[:self._PREVIEW_CHARS]
        spill_path_str = spill_path.resolve().as_posix()
        return (
            f"<truncated-output>\n"
            f"Output too large ({size_str}). Full output saved to:\n"
            f"{spill_path_str}\n\n"
            f"Preview (first {self._PREVIEW_CHARS // 1024:.0f} KB):\n"
            f"{preview}\n"
            f"...\n"
            f"</truncated-output>"
        )
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
cd F:/Forge-Code && python -m pytest tests/test_truncation.py -v
```
Expected: all PASS

- [ ] **Step 5: Commit**

```bash
git add src/main/truncation.py tests/test_truncation.py
git commit -m "feat: add Layer 1 ToolOutputTruncator with AND-logic head truncation"
```

---

### Task 3: Layer 2 — ToolOutputPruner

**Files:**
- Create: `src/main/pruning.py`
- Create: `tests/test_pruning.py`

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_pruning.py
from main.context import Message
from main.pruning import ToolOutputPruner


def _tool_msg(content: str, compacted: bool = False) -> Message:
    return Message(role="tool", content=content, tool_name="read_file",
                   tool_call_id="id1", compacted=compacted)


def _user_msg(content: str = "hello") -> Message:
    return Message(role="user", content=content)


def _assistant_msg(content: str = "ok") -> Message:
    return Message(role="assistant", content=content)


def _summary_msg(content: str = "summary") -> Message:
    return Message(role="assistant", content=content, summary=True)


def test_prune_skips_last_2_turns():
    pruner = ToolOutputPruner()
    # 3 turns, each: user → assistant → tool (big output)
    messages = [
        _user_msg("q1"),
        _assistant_msg("a1"),
        _tool_msg("x" * 20_000),   # turn 1 tool, 5K tokens
        _user_msg("q2"),
        _assistant_msg("a2"),
        _tool_msg("x" * 20_000),   # turn 2 tool, 5K tokens
        _user_msg("q3"),
        _assistant_msg("a3"),
        _tool_msg("x" * 20_000),   # turn 3 tool, 5K tokens (protected)
    ]
    pruned = pruner.prune(messages)
    assert pruned > 0
    # Turn 3 tools protected (last 2 turns: turn 2 and turn 3)
    assert messages[8].compacted is False
    assert messages[8].content != "[tool result pruned]"
    assert messages[5].compacted is False  # turn 2 protected
    # Turn 1 pruned
    assert messages[2].compacted is True
    assert messages[2].content == "[tool result pruned]"


def test_prune_skips_when_below_minimum():
    pruner = ToolOutputPruner()
    messages = [
        _user_msg("q1"),
        _assistant_msg("a1"),
        _tool_msg("x" * 100),  # tiny tool output
        _user_msg("q2"),
        _assistant_msg("a2"),
        _tool_msg("x" * 100),
    ]
    pruned = pruner.prune(messages)
    assert pruned == 0  # Not enough to recover


def test_prune_stops_at_summary():
    pruner = ToolOutputPruner()
    messages = [
        _user_msg("old"),
        _assistant_msg("old"),
        _tool_msg("x" * 50_000),  # old big tool output
        _summary_msg("previous compaction summary"),
        _user_msg("recent q"),
        _assistant_msg("recent a"),
        _tool_msg("y" * 10_000),
    ]
    pruned = pruner.prune(messages)
    # Should stop at summary, so old tool output before summary is NOT pruned
    assert messages[2].compacted is False
    assert messages[2].content != "[tool result pruned]"


def test_prune_skips_already_compacted():
    pruner = ToolOutputPruner()
    messages = [
        _user_msg("q1"),
        _assistant_msg("a1"),
        _tool_msg("x" * 50_000, compacted=True),  # already compacted
        _user_msg("q2"),
        _assistant_msg("a2"),
        _tool_msg("z" * 50_000),  # fresh big output
    ]
    pruned = pruner.prune(messages)
    assert pruned == 0  # already-compacted messages count as stop point
    assert messages[2].compacted is True  # unchanged
    assert messages[5].compacted is False  # protected (within recent 2 turns)


def test_pruned_messages_marked_compacted():
    pruner = ToolOutputPruner()
    messages = [
        _user_msg("q1"),
        _assistant_msg("a1"),
        _tool_msg("x" * 50_000),  # old, will be pruned
        _user_msg("q2"),
        _assistant_msg("a2"),
        _tool_msg("y" * 50_000),  # old, will be pruned
        _user_msg("q3"),
        _assistant_msg("a3"),
        _tool_msg("z" * 10_000),  # protected
    ]
    pruner.prune(messages)
    assert messages[2].compacted is True
    assert messages[5].compacted is True
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
cd F:/Forge-Code && python -m pytest tests/test_pruning.py -v
```

- [ ] **Step 3: Implement `ToolOutputPruner`**

```python
# src/main/pruning.py
"""Layer 2: Reverse-scan tool output pruning (zero LLM cost)."""

from __future__ import annotations

from main.context import Message


class ToolOutputPruner:
    """Replace old tool outputs with [tool result pruned] placeholder."""

    PRUNE_PROTECT = 40_000
    PRUNE_MINIMUM = 20_000
    _PROTECTED_TURNS = 2

    def prune(self, messages: list[Message], *,
              prune_protect: int = PRUNE_PROTECT,
              prune_minimum: int = PRUNE_MINIMUM) -> int:
        """Reverse-scan and prune old tool results. Returns estimated tokens recovered."""
        n = len(messages)

        # Find the cutoff: skip last N turns
        turns_skipped = 0
        scan_start = n - 1
        for i in range(n - 1, -1, -1):
            if messages[i].role == "user":
                turns_skipped += 1
                if turns_skipped >= self._PROTECTED_TURNS:
                    scan_start = i - 1
                    break

        # Reverse scan from scan_start
        cumulative_tokens = 0
        prune_queue: list[int] = []  # indices to prune
        stopped_by_summary = False

        for i in range(scan_start, -1, -1):
            msg = messages[i]

            # Stop at summary message
            if msg.summary:
                stopped_by_summary = True
                break

            # Stop at already-compacted tool results
            if msg.role == "tool" and msg.compacted:
                break

            # Accumulate tool tokens
            if msg.role == "tool" and not msg.compacted:
                msg_tokens = len(msg.content) // 4
                if cumulative_tokens + msg_tokens <= prune_protect:
                    cumulative_tokens += msg_tokens
                else:
                    prune_queue.append(i)

        # Check minimum recovery
        total_prune_tokens = sum(len(messages[idx].content) // 4 for idx in prune_queue)
        if total_prune_tokens < prune_minimum:
            return 0

        # Execute replacement
        for idx in prune_queue:
            messages[idx].content = "[tool result pruned]"
            messages[idx].compacted = True

        return total_prune_tokens
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
cd F:/Forge-Code && python -m pytest tests/test_pruning.py -v
```
Expected: all PASS

- [ ] **Step 5: Commit**

```bash
git add src/main/pruning.py tests/test_pruning.py
git commit -m "feat: add Layer 2 ToolOutputPruner with reverse-scan algorithm"
```

---

### Task 4: Layer 3 — Compactor

**Files:**
- Create: `src/main/compaction.py`
- Create: `tests/test_compaction.py`

- [ ] **Step 1: Write failing tests for `select()` message split**

```python
# tests/test_compaction.py
import pytest
from main.context import Message
from main.compaction import Compactor, CompactionResult


def _user(content: str = "hello") -> Message:
    return Message(role="user", content=content)


def _assistant(content: str = "ok") -> Message:
    return Message(role="assistant", content=content)


def _tool(content: str, tool_name: str = "read_file") -> Message:
    return Message(role="tool", content=content, tool_name=tool_name)


class _FakeModel:
    """Fake model client that returns a canned response."""
    def __init__(self, response_text: str = "## Goal\nTest goal\n"):
        self.response_text = response_text
        self.last_messages = None

    def complete(self, messages, tools):
        self.last_messages = messages
        from src.tools.base import ModelResponse
        return ModelResponse(text=self.response_text, input_tokens=100, output_tokens=50)


def test_select_splits_head_and_tail():
    compactor = Compactor(_FakeModel(), max_context_tokens=100_000)
    messages = [
        _user("old q1"), _assistant("old a1"), _tool("x" * 1000),
        _user("old q2"), _assistant("old a2"), _tool("y" * 1000),
        _user("recent q"), _assistant("recent a"),
    ]
    head, tail = compactor._select(messages)
    # tail should contain at least the last turn
    assert len(tail) >= 2
    assert tail[-1].role == "assistant"
    # head should contain the rest
    assert len(head) > 0
    assert len(head) + len(tail) == len(messages)


def test_select_returns_stop_when_no_head():
    compactor = Compactor(_FakeModel(), max_context_tokens=100_000)
    messages = [
        _user("only q"), _assistant("only a"),
    ]
    head, tail = compactor._select(messages)
    assert len(head) == 0  # nothing to compress


def test_build_prompt_first_time():
    compactor = Compactor(_FakeModel(), max_context_tokens=100_000)
    head = [_user("q1"), _assistant("a1"), _tool("some output")]
    prompt = compactor._build_prompt(head)
    assert "Create a new anchored summary" in prompt
    assert "## Goal" in prompt
    assert "## Constraints" in prompt
    assert "## Progress" in prompt
    assert "## Key Decisions" in prompt
    assert "## Next Steps" in prompt
    assert "## Critical Context" in prompt
    assert "## Relevant Files" in prompt


def test_build_prompt_incremental():
    compactor = Compactor(_FakeModel(), max_context_tokens=100_000)
    compactor._previous_summary = "Previous: did X, decided Y"
    head = [_user("new q"), _assistant("new a")]
    prompt = compactor._build_prompt(head)
    assert "Previous summary:" in prompt
    assert "Previous: did X, decided Y" in prompt
    assert "Preserve still-true details" in prompt


def test_compact_returns_continue_on_success():
    model = _FakeModel(
        "## Goal\nBuild feature X\n"
        "## Constraints\nMust use Python\n"
        "## Progress\nIn Progress\n"
        "## Key Decisions\nUsing pytest\n"
        "## Next Steps\nWrite tests\n"
        "## Critical Context\nAPI key configured\n"
        "## Relevant Files\nsrc/main.py: main entry\n"
    )
    compactor = Compactor(model, max_context_tokens=100_000)
    messages = [
        _user("build feature X"),
        _assistant("ok let me do that"),
        _tool("file contents here" * 100),
    ]
    result = compactor.compact(messages)
    assert result.status == "continue"
    assert result.summary_msg is not None
    assert result.summary_msg.summary is True
    assert result.summary_msg.role == "assistant"
    assert "## Goal" in result.summary_msg.content


def test_compact_returns_stop_on_llm_error():
    class _ErrorModel:
        def complete(self, messages, tools):
            raise RuntimeError("API down")
    compactor = Compactor(_ErrorModel(), max_context_tokens=100_000)
    messages = [
        _user("q1"), _assistant("a1"), _tool("x" * 1000),
        _user("q2"), _assistant("a2"),
    ]
    result = compactor.compact(messages)
    assert result.status == "stop"


def test_compaction_result_structure():
    result = CompactionResult(
        status="continue",
        summary_msg=Message(role="assistant", content="summary", summary=True),
        tail_messages=[_user("q"), _assistant("a")],
        overflow=False,
    )
    assert result.summary_msg.summary is True
    assert len(result.tail_messages) == 2
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
cd F:/Forge-Code && python -m pytest tests/test_compaction.py -v
```

- [ ] **Step 3: Implement `Compactor` and `CompactionResult`**

```python
# src/main/compaction.py
"""Layer 3: LLM semantic compression with 7-section structured summaries."""

from __future__ import annotations

import dataclasses
from typing import TYPE_CHECKING

from main.context import Message

if TYPE_CHECKING:
    from main.runtime import ModelClient


@dataclasses.dataclass
class CompactionResult:
    status: str  # "continue" | "stop" | "compact"
    summary_msg: Message | None = None
    tail_messages: list[Message] = dataclasses.field(default_factory=list)
    overflow: bool = False


class Compactor:
    """LLM-based semantic compression producing 7-section structured summary."""

    _COMPACTION_BUFFER = 20_000
    _TAIL_BUDGET_RATIO = 0.25
    _TAIL_CLAMP_MIN = 2_000
    _TAIL_CLAMP_MAX = 8_000
    _TOOL_OUTPUT_MAX_CHARS = 2_000

    _FIRST_TIME_PROMPT = (
        "Create a new anchored summary from the conversation history above. "
        "Output in the following 7 sections exactly:\n"
        "## Goal\n"
        "## Constraints\n"
        "## Progress\n"
        "## Key Decisions\n"
        "## Next Steps\n"
        "## Critical Context\n"
        "## Relevant Files\n"
    )

    _INCREMENTAL_PROMPT = (
        "Below is the previous summary and new conversation history. "
        "Preserve still-true details, remove stale details, merge in new facts.\n"
        "Previous summary:\n{previous_summary}\n\n"
        "Output the updated summary in the same 7 sections exactly:\n"
        "## Goal\n"
        "## Constraints\n"
        "## Progress\n"
        "## Key Decisions\n"
        "## Next Steps\n"
        "## Critical Context\n"
        "## Relevant Files\n"
    )

    def __init__(self, model_client, *,
                 max_context_tokens: int = 258_000) -> None:
        self._model = model_client
        self._max_context = max_context_tokens
        self._previous_summary: str | None = None

    # -- public API ---------------------------------------------------------

    def compact(self, messages: list[Message]) -> CompactionResult:
        """Run the 5-step compaction workflow."""
        head, tail = self._select(messages)
        if len(head) == 0:
            return CompactionResult(status="stop", tail_messages=tail)

        prompt = self._build_prompt(head)
        summary_text = self._call_llm(prompt, head)
        if summary_text is None:
            return CompactionResult(status="stop", tail_messages=tail)

        self._previous_summary = summary_text
        summary_msg = Message(
            role="assistant",
            content=summary_text.strip(),
            summary=True,
        )

        # Check if summary itself overflows (recursive compact needed)
        summary_tokens = len(summary_text) // 4
        usable = self._max_context - self._COMPACTION_BUFFER
        tail_tokens = sum(len(m.content) // 4 for m in tail)
        overflow = (summary_tokens + tail_tokens) > usable

        if overflow and summary_tokens > usable * 0.5:
            # Summary is too large, recursive compact
            return CompactionResult(status="compact", summary_msg=summary_msg,
                                    tail_messages=tail, overflow=True)

        return CompactionResult(
            status="continue",
            summary_msg=summary_msg,
            tail_messages=tail,
            overflow=overflow,
        )

    # -- internal -----------------------------------------------------------

    def _select(self, messages: list[Message]) -> tuple[list[Message], list[Message]]:
        """Split messages into head (to compress) and tail (preserve verbatim)."""
        usable = self._max_context - self._COMPACTION_BUFFER
        tail_budget = max(self._TAIL_CLAMP_MIN,
                          min(self._TAIL_CLAMP_MAX,
                              int(usable * self._TAIL_BUDGET_RATIO)))

        # Group messages into turns
        turns: list[list[Message]] = []
        current_turn: list[Message] = []
        for msg in messages:
            if msg.role == "user" and current_turn:
                turns.append(current_turn)
                current_turn = []
            current_turn.append(msg)
        if current_turn:
            turns.append(current_turn)

        # Accumulate turns from newest
        tail_turns: list[list[Message]] = []
        tail_token_count = 0
        for turn in reversed(turns):
            turn_tokens = sum(len(m.content) // 4 for m in turn)
            if tail_token_count + turn_tokens <= tail_budget:
                tail_turns.insert(0, turn)
                tail_token_count += turn_tokens
            else:
                # Try splitTurn: cut within turn at message boundary
                partial, partial_tokens = self._split_turn(turn, tail_budget - tail_token_count)
                if partial:
                    tail_turns.insert(0, partial)
                break

        # Fallback: keep at least 1 turn
        if not tail_turns and turns:
            tail_turns = [turns[-1]]

        tail = [msg for turn in tail_turns for msg in turn]
        head = [msg for turn in turns if turn not in tail_turns for msg in turn]
        return head, tail

    @staticmethod
    def _split_turn(turn: list[Message], budget: int) -> tuple[list[Message] | None, int]:
        """Try to cut within a turn at message boundary to fit budget."""
        partial: list[Message] = []
        tokens = 0
        for msg in reversed(turn):
            msg_tokens = len(msg.content) // 4
            if tokens + msg_tokens <= budget:
                partial.insert(0, msg)
                tokens += msg_tokens
            else:
                break
        return (partial, tokens) if partial else (None, 0)

    def _build_prompt(self, head: list[Message]) -> str:
        """Build the compaction prompt (first-time or incremental)."""
        # Format head messages for the prompt
        parts: list[str] = []
        for msg in head:
            prefix = f"[{msg.role}]"
            if msg.tool_name:
                prefix = f"[tool:{msg.tool_name}]"
            content = msg.content
            if msg.role == "tool" and len(content) > self._TOOL_OUTPUT_MAX_CHARS:
                content = content[:self._TOOL_OUTPUT_MAX_CHARS] + "..."
            parts.append(f"{prefix} {content}")

        history_text = "\n---\n".join(parts)

        if self._previous_summary:
            return self._INCREMENTAL_PROMPT.format(
                previous_summary=self._previous_summary
            ) + "\n\n" + history_text
        else:
            return self._FIRST_TIME_PROMPT + "\n\n" + history_text

    def _call_llm(self, prompt: str, head: list[Message]) -> str | None:
        """Call the compaction LLM, stripping media."""
        try:
            resp = self._model.complete(
                [Message(role="user", content=prompt)], []
            )
            return resp.text or None
        except Exception:
            return None
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
cd F:/Forge-Code && python -m pytest tests/test_compaction.py -v
```
Expected: all PASS

- [ ] **Step 5: Commit**

```bash
git add src/main/compaction.py tests/test_compaction.py
git commit -m "feat: add Layer 3 Compactor with 7-section structured LLM summary"
```

---

### Task 5: Shell tool tail truncation

**Files:**
- Modify: `src/tools/shell.py`
- Modify: `src/main/truncation.py` (add `ShellTruncator`)

- [ ] **Step 1: Add `ShellTruncator` to truncation module**

```python
# Add to src/main/truncation.py, after ToolOutputTruncator

class ShellTruncator:
    """Tail-biased truncation for shell command output."""

    _PREVIEW_CHARS = 2_048

    def __init__(self, base_dir: Path, session_id: str) -> None:
        self._spill_dir = base_dir / ".forgecode" / "sessions" / session_id / "truncated"
        self._last_cleanup: float = 0.0
        self._initialized = False

    def _ensure_dir(self) -> None:
        if not self._initialized:
            self._spill_dir.mkdir(parents=True, exist_ok=True)
            self._initialized = True
        # Reuse cleanup logic from ToolOutputTruncator
        now = time.time()
        if now - self._last_cleanup >= 3_600:
            self._last_cleanup = now
            cutoff = now - 604_800
            if self._spill_dir.exists():
                for f in self._spill_dir.iterdir():
                    if f.is_file() and f.stat().st_mtime < cutoff:
                        f.unlink(missing_ok=True)

    def should_truncate(self, output: str, *,
                        line_threshold: int = 2_000,
                        byte_threshold: int = 50_000) -> bool:
        return ToolOutputTruncator.should_truncate(
            output, line_threshold=line_threshold, byte_threshold=byte_threshold
        )

    def truncate(self, output: str,
                 tool_call_id: str | None = None) -> str:
        """Tail truncation: keep end of output, spill to disk."""
        if not self.should_truncate(output):
            return output

        self._ensure_dir()
        file_id = tool_call_id if tool_call_id else uuid.uuid4().hex
        spill_path = self._spill_dir / f"{file_id}.txt"
        try:
            spill_path.write_text(output, encoding="utf-8", errors="replace")
        except OSError:
            pass

        size_bytes = len(output.encode("utf-8", errors="replace"))
        if size_bytes >= 1_048_576:
            size_str = f"{size_bytes / 1_048_576:.1f} MB"
        elif size_bytes >= 1024:
            size_str = f"{size_bytes / 1024:.1f} KB"
        else:
            size_str = f"{size_bytes} B"

        tail_preview = output[-self._PREVIEW_CHARS:]
        spill_path_str = spill_path.resolve().as_posix()
        return (
            f"<truncated-output>\n"
            f"Shell output too large ({size_str}). Full output saved to:\n"
            f"{spill_path_str}\n\n"
            f"Tail preview (last {self._PREVIEW_CHARS // 1024:.0f} KB):\n"
            f"{tail_preview}\n"
            f"</truncated-output>"
        )
```

- [ ] **Step 2: Modify `RunCommandTool.run()` to use tail truncation**

In `src/tools/shell.py`, replace the manual truncation logic with delegation to `ShellTruncator`. The shell tool needs access to a truncator instance — pass it through `config` or add it as a module-level concern. Since `ShellTruncator` needs `base_dir` and `session_id`, and the tool only receives `config`, we handle this by passing a `shell_truncator` attribute:

```python
# src/tools/shell.py — modify RunCommandTool class

class RunCommandTool:
    """Execute shell commands."""

    def __init__(self, shell_truncator=None):
        self._shell_truncator = shell_truncator

    # ... name, description, parameters_schema, safety_label unchanged ...

    def run(self, *, arguments: dict[str, Any], config: AgentConfig) -> ToolResult:
        command = arguments.get("command", "")
        if not command:
            return ToolResult(False, "", "No command provided")

        timeout = arguments.get("timeout", config.command_timeout)

        try:
            result = subprocess.run(
                command,
                shell=True,
                capture_output=True,
                text=True,
                cwd=str(config.working_directory),
                timeout=timeout,
                env=_make_sanitized_env(),
                executable=None if sys.platform == "win32" else "/bin/bash",
            )
        except subprocess.TimeoutExpired:
            return ToolResult(False, "", f"Command timed out after {timeout}s: {command}")
        except OSError as exc:
            return ToolResult(False, "", f"Failed to execute command: {exc}")

        output_parts: list[str] = []
        if result.stdout:
            output_parts.append(result.stdout)
        if result.stderr:
            output_parts.append(f"[stderr]\n{result.stderr}")
        output_parts.append(f"[exit code: {result.returncode}]")

        output = "\n".join(output_parts)

        # Layer 1: Shell tail truncation
        if self._shell_truncator is not None:
            output = self._shell_truncator.truncate(output)

        return ToolResult(
            result.returncode == 0, output,
            None if result.returncode == 0 else f"Exit code {result.returncode}"
        )
```

Remove the old `_MAX_OUTPUT = 102_400` constant and the head+tail truncation block (lines 102-108 in the original).

- [ ] **Step 3: Write test for shell tail truncation**

```python
# Add to tests/test_truncation.py

from main.truncation import ShellTruncator


def test_shell_truncator_tail_keeps_end_content(tmp_path):
    truncator = ShellTruncator(tmp_path, "s1")
    lines = [f"line_{i:04d}" for i in range(3000)]
    output = "\n".join(lines)
    result = truncator.truncate(output, tool_call_id="shell_001")

    # Tail truncation: last lines should be in the result
    assert "line_2999" in result
    assert "line_2998" in result
    # First lines should not be in the preview
    assert "line_0000" not in result

    spill_file = tmp_path / ".forgecode" / "sessions" / "s1" / "truncated" / "shell_001.txt"
    assert spill_file.exists()
    assert spill_file.read_text(encoding="utf-8") == output


def test_shell_truncator_respects_and_thresholds(tmp_path):
    truncator = ShellTruncator(tmp_path, "s1")
    output = "short\noutput\n"  # 2 lines, small
    result = truncator.truncate(output)
    assert result == output  # No truncation
```

- [ ] **Step 4: Run all truncation tests**

```bash
cd F:/Forge-Code && python -m pytest tests/test_truncation.py -v
```
Expected: all PASS

- [ ] **Step 5: Commit**

```bash
git add src/main/truncation.py src/tools/shell.py tests/test_truncation.py
git commit -m "feat: add ShellTruncator tail truncation for shell command output"
```

---

### Task 6: ContextWindowManager rewrite

**Files:**
- Rewrite: `src/main/context_manager.py`
- Create: `tests/test_context_manager.py`

- [ ] **Step 1: Rewrite `ContextWindowManager` as 3-layer orchestrator**

```python
# src/main/context_manager.py
"""Three-layer context window management.

Layer flow:
  Tool output → Layer 1 (Truncation) → deque
  User message → overflow check → Layer 2 (Pruning) → Layer 3 (Compaction)
"""

from __future__ import annotations

from collections import deque
from pathlib import Path
from typing import TYPE_CHECKING, Callable

from main.truncation import ToolOutputTruncator, ShellTruncator
from main.pruning import ToolOutputPruner
from main.compaction import Compactor, CompactionResult

if TYPE_CHECKING:
    from main.config import AgentConfig
    from main.context import Message
    from main.runtime import ModelClient


class ContextWindowManager:
    """Coordinates all three context management layers."""

    def __init__(
        self,
        config: AgentConfig,
        working_directory: Path,
        session_id: str,
        model_client=None,
    ) -> None:
        # Layer 1
        self._session_id = session_id
        self._truncator = ToolOutputTruncator(
            base_dir=working_directory,
            session_id=session_id,
        )
        # Layer 2
        self._pruner = ToolOutputPruner()
        # Layer 3
        self._compactor: Compactor | None = None
        if model_client is not None:
            self._compactor = Compactor(
                model_client=model_client,
                max_context_tokens=config.max_context_tokens,
            )
        # Config
        self._config = config
        # Simple token estimation (chars/4)
        self._token_count = 0

    # -- Layer 1: Truncation ------------------------------------------------

    def process_tool_output(self, output: str, tool_name: str,
                            tool_call_id: str | None = None) -> str:
        """Run Layer 1 truncation on tool output before it enters deque."""
        if tool_name == "run_command":
            return output  # Shell handles its own truncation
        return self._truncator.truncate(
            output, tool_name, tool_call_id=tool_call_id,
            line_threshold=self._config.truncate_line_threshold,
            byte_threshold=self._config.truncate_byte_threshold,
        )

    def create_shell_truncator(self) -> ShellTruncator:
        """Create a ShellTruncator for use by the shell tool."""
        return ShellTruncator(
            base_dir=self._config.working_directory,
            session_id=self._session_id,
        )

    # -- Layer 2+3: Pruning + Compaction ------------------------------------

    def check_and_compact(
        self,
        messages: list[Message],
        current_user_msg: Message,
    ) -> CompactionResult | None:
        """Check overflow, run pruning then compaction if needed.
        Returns CompactionResult if compaction was triggered, None otherwise.
        """
        # Estimate current token usage
        total_tokens = sum(len(m.content) // 4 for m in messages)
        total_tokens += len(current_user_msg.content) // 4
        usable = self._config.max_context_tokens - self._config.compaction_buffer

        if total_tokens < usable:
            return None  # No overflow

        # Layer 2: Pruning first
        pruned = self._pruner.prune(
            messages,
            prune_protect=self._config.prune_protect,
            prune_minimum=self._config.prune_minimum,
        )

        # Re-estimate after pruning
        new_total = sum(len(m.content) // 4 for m in messages)
        new_total += len(current_user_msg.content) // 4
        if new_total < usable:
            return None  # Pruning was sufficient

        # Layer 3: Compaction
        if self._compactor is not None:
            return self._compactor.compact(messages)
        return None

    def process_compaction_result(
        self,
        messages: list[Message],
        result: CompactionResult,
        current_user_msg: Message,
    ) -> list[Message]:
        """Build the new message deque after compaction."""
        if result.status == "stop":
            return messages  # Compaction failed, return unchanged

        new_messages: list[Message] = []

        # 1. Compaction metadata message
        compaction_meta = Message(
            role="compaction",
            content="context compaction",
        )
        new_messages.append(compaction_meta)

        # 2. Summary
        if result.summary_msg is not None:
            new_messages.append(result.summary_msg)

        # 3. Tail (preserved verbatim)
        new_messages.extend(result.tail_messages)

        # 4. Continue or replay
        if result.overflow:
            # Replay: strip media from current user message
            replayed = Message(
                role="user",
                content=_strip_media(current_user_msg.content),
            )
            new_messages.append(replayed)
        else:
            new_messages.append(Message(
                role="user",
                content="Continue if you have next steps, or stop...",
            ))

        return new_messages

    @property
    def content_replacement_state(self) -> dict[str, str]:
        # Maintained for session persistence compatibility
        return {}


def _strip_media(content: str) -> str:
    """Replace image/file attachment references with text placeholders."""
    import re
    # Replace [Attached ...] patterns
    return re.sub(r'\[Attached[^\]]*\]', '[media attachment]', content)
```

- [ ] **Step 2: Write integration tests**

```python
# tests/test_context_manager.py
from pathlib import Path
from main.config import AgentConfig
from main.context import Message
from main.context_manager import ContextWindowManager


class _FakeModel:
    def __init__(self, response_text="## Goal\nTest\n"):
        self.response_text = response_text
    def complete(self, messages, tools):
        from src.tools.base import ModelResponse
        return ModelResponse(text=self.response_text, input_tokens=100, output_tokens=50)


def test_process_tool_output_under_thresholds(tmp_path):
    config = AgentConfig(working_directory=tmp_path)
    wm = ContextWindowManager(config, tmp_path, "s1")
    output = "short output"
    result = wm.process_tool_output(output, "read_file", "call_1")
    assert result == output


def test_process_tool_output_over_thresholds(tmp_path):
    config = AgentConfig(
        working_directory=tmp_path,
        truncate_line_threshold=10,
        truncate_byte_threshold=100,
    )
    wm = ContextWindowManager(config, tmp_path, "s1")
    output = "x" * 200 + "\n"  # creates many lines, big bytes
    output = output * 50  # enough to trigger
    result = wm.process_tool_output(output, "read_file", "call_big")
    assert "<truncated-output>" in result


def test_shell_tool_skips_truncation(tmp_path):
    config = AgentConfig(working_directory=tmp_path)
    wm = ContextWindowManager(config, tmp_path, "s1")
    output = "x" * 100_000
    result = wm.process_tool_output(output, "run_command")
    assert result == output  # Shell bypasses framework truncation


def test_check_and_compact_no_overflow(tmp_path):
    config = AgentConfig(working_directory=tmp_path, max_context_tokens=1_000_000)
    wm = ContextWindowManager(config, tmp_path, "s1", model_client=_FakeModel())
    messages = [Message(role="user", content="hello")]
    current = Message(role="user", content="world")
    result = wm.check_and_compact(messages, current)
    assert result is None


def test_check_and_compact_with_overflow(tmp_path):
    config = AgentConfig(
        working_directory=tmp_path,
        max_context_tokens=5_000,
        prune_protect=1_000,
        prune_minimum=500,
        compaction_buffer=1_000,
    )
    wm = ContextWindowManager(config, tmp_path, "s1", model_client=_FakeModel())
    # Create messages that exceed usable context
    messages = [
        Message(role="user", content="q1"),
        Message(role="assistant", content="a1"),
        Message(role="tool", content="x" * 10_000, tool_name="read_file"),
    ]
    current = Message(role="user", content="big question")
    result = wm.check_and_compact(messages, current)
    # Should trigger compaction since pruning alone won't be enough
    assert result is not None
    assert result.status in ("continue", "stop", "compact")


def test_process_compaction_result_builds_structure(tmp_path):
    config = AgentConfig(working_directory=tmp_path)
    wm = ContextWindowManager(config, tmp_path, "s1")
    from main.compaction import CompactionResult
    summary = Message(role="assistant", content="## Goal\nBuild it\n", summary=True)
    tail = [Message(role="user", content="recent"), Message(role="assistant", content="ok")]
    result = CompactionResult(status="continue", summary_msg=summary, tail_messages=tail, overflow=False)
    current = Message(role="user", content="do something")
    new_messages = wm.process_compaction_result([], result, current)
    assert new_messages[0].role == "compaction"
    assert new_messages[1].summary is True
    assert new_messages[-1].content == "Continue if you have next steps, or stop..."
```

- [ ] **Step 3: Run context manager tests**

```bash
cd F:/Forge-Code && python -m pytest tests/test_context_manager.py -v
```
Expected: all PASS

- [ ] **Step 4: Commit**

```bash
git add src/main/context_manager.py tests/test_context_manager.py
git commit -m "refactor: rewrite ContextWindowManager as 3-layer orchestrator"
```

---

### Task 7: ContextBuilder and Runtime integration

**Files:**
- Modify: `src/main/context.py:51-129`
- Modify: `src/main/runtime.py:652-707, 800-820`

- [ ] **Step 1: Add compaction message filtering to `ContextBuilder.build()`**

In `src/main/context.py`, modify `build()` to filter out `role="compaction"` messages:

```python
def build(self) -> list[Message]:
    """Return ``[system_message, *history]``, filtering compaction messages."""
    system = Message(role="system", content=self._build_system_prompt())
    visible = [m for m in self._history if m.role != "compaction"]
    return [system, *visible]
```

- [ ] **Step 2: Update `ContextBuilder` constructor to accept `model_client` and wire compaction**

Modify the `__init__` signature to optionally accept a model_client for compaction. The `_window_manager` is already injected; it now handles all 3 layers internally.

No constructor change needed — the `ContextWindowManager` already receives `model_client` during construction (Task 6). The `ContextBuilder` methods just delegate.

- [ ] **Step 3: Update `add_tool_result` to use new truncation path**

```python
# In ContextBuilder.add_tool_result:
def add_tool_result(self, tool_name: str, result: ToolResult, tool_call_id: str | None = None) -> None:
    content = result.output if result.success else f"Error: {result.error}"
    if self._window_manager is not None:
        content = self._window_manager.process_tool_output(content, tool_name, tool_call_id=tool_call_id)
    self._append(Message(role="tool", content=content, tool_name=tool_name, tool_call_id=tool_call_id))
    # Note: after_add is removed — overflow/pruning/compaction only runs on user message
```

Remove the `after_add` call and the `_replace_history` delegation (compaction now uses `process_compaction_result` which returns a modified list rather than calling replace_fn).

- [ ] **Step 4: Add compaction check to `AgentRuntime._run_agent_turn`**

In `src/main/runtime.py`, at the top of `_run_agent_turn`, after adding the user message, add:

```python
def _run_agent_turn(self, user_input: str) -> None:
    # Auto-set session title from first user message
    if self._session is not None and not self._session.metadata.title:
        self._session.metadata.title = user_input[:50]

    # Memory prefetch (background thread)
    memory_future = None
    if self._config.memory_enabled:
        from memory.memory import start_memory_prefetch, PREFETCH_TIMEOUT
        memory_future = start_memory_prefetch(
            query=user_input,
            config=self._config,
            model_client=self._model,
            session_memory_bytes=self._session_memory_bytes,
        )

    # --- NEW: Compaction check ---
    wm = self._context._window_manager
    if wm is not None:
        # Check if deque head has a pending compaction message
        history = list(self._context._history)
        if history and history[0].role == "compaction":
            # Process it: the compaction meta message is at head, skip it and resume
            pass  # Already processed — the replay/continue message is the actual user input
        else:
            # Create a temporary user message for overflow check
            temp_msg = Message(role="user", content=user_input)
            result = wm.check_and_compact(history, temp_msg)
            if result is not None:
                new_messages = wm.process_compaction_result(history, result, temp_msg)
                self._context._replace_history(new_messages)
                # The last message in new_messages is the replay/continue
                # Skip adding user_input since it's already handled
                user_input = None
    # --- END compaction check ---

    if user_input is not None:
        self._context.add_user_message(user_input)
    # ... rest of the method
```

But wait — the compaction check runs BEFORE `add_user_message`. The logic needs refinement. Let me restructure:

```python
def _run_agent_turn(self, user_input: str) -> None:
    """Execute one user turn."""
    if self._session is not None and not self._session.metadata.title:
        self._session.metadata.title = user_input[:50]

    # Memory prefetch
    memory_future = None
    if self._config.memory_enabled:
        from memory.memory import start_memory_prefetch, PREFETCH_TIMEOUT
        memory_future = start_memory_prefetch(
            query=user_input, config=self._config,
            model_client=self._model, session_memory_bytes=self._session_memory_bytes,
        )

    # Compaction check — before adding user message
    wm = self._context._window_manager
    if wm is not None:
        history = list(self._context._history)
        if history and history[0].role == "compaction":
            # Compaction already processed last turn, dequeue the meta message
            self._context._history.popleft()
        else:
            current_msg = Message(role="user", content=user_input)
            result = wm.check_and_compact(history, current_msg)
            if result is not None and result.status != "stop":
                new_messages = wm.process_compaction_result(history, result, current_msg)
                self._context._replace_history(new_messages)
                # user_input was incorporated as replay/continue; skip add_user_message
                user_input = ""

    if user_input:
        self._context.add_user_message(user_input)

    self._context.check_idle_compression()
    # ... rest unchanged: tools = ..., token_tracker.begin_turn(), etc.
```

- [ ] **Step 5: Update `AgentRuntime.__init__` to pass `model_client` to `ContextWindowManager`**

In `src/main/runtime.py`, modify the `ContextWindowManager` construction in `__init__`:

```python
# Replace the existing window_manager construction block
window_manager = ContextWindowManager(
    config=config,
    working_directory=config.working_directory,
    session_id=session_id,
    model_client=model_client if not isinstance(model_client, PlaceholderModelClient) else None,
)
```

Remove the old `summarize_fn` lambda and `auto_compact_enabled` check — the new `ContextWindowManager` doesn't need them.

- [ ] **Step 6: Run all existing tests to verify no regressions**

```bash
cd F:/Forge-Code && python -m pytest tests/ -x -q
```

- [ ] **Step 7: Commit**

```bash
git add src/main/context.py src/main/runtime.py
git commit -m "feat: integrate 3-layer compression into ContextBuilder and AgentRuntime"
```

---

### Task 8: Shell tool wiring in registry

**Files:**
- Modify: `src/tools/builtin.py` or wherever tools are registered

- [ ] **Step 1: Wire ShellTruncator to RunCommandTool**

The `RunCommandTool` now accepts an optional `shell_truncator` parameter. We need to pass it during tool construction. This requires `ContextWindowManager` to be accessible at tool creation time.

Since tools are created before `ContextWindowManager` exists (they're created in `ToolRegistry` first), we defer: add a method to set it after construction, or use a lazy approach.

Simplest approach — add a setter:

```python
# In RunCommandTool
def set_shell_truncator(self, truncator):
    self._shell_truncator = truncator
```

Wire it in `AgentRuntime.__init__` after creating `window_manager`:

```python
# In AgentRuntime.__init__, after creating window_manager:
if window_manager is not None:
    shell_truncator = window_manager.create_shell_truncator()
    shell_tool = self._registry.get("run_command")
    if shell_tool is not None and hasattr(shell_tool, 'set_shell_truncator'):
        shell_tool.set_shell_truncator(shell_truncator)
```

- [ ] **Step 2: Verify shell tests pass**

```bash
cd F:/Forge-Code && python -m pytest tests/ -x -q
```

- [ ] **Step 3: Commit**

```bash
git add src/tools/shell.py src/tools/builtin.py src/main/runtime.py
git commit -m "feat: wire ShellTruncator to RunCommandTool via ContextWindowManager"
```

---

### Task 9: Integration tests and final cleanup

**Files:**
- Create: `tests/test_integration_compression.py`

- [ ] **Step 1: Write end-to-end integration test**

```python
# tests/test_integration_compression.py
"""End-to-end tests for the 3-layer compression flow."""
from pathlib import Path
from main.config import AgentConfig
from main.context import Message, ContextBuilder
from main.context_manager import ContextWindowManager
from src.tools.registry import ToolRegistry
from src.tools.base import ToolResult


class _FakeModel:
    def __init__(self, response_text="## Goal\nIntegration test\n"):
        self.response_text = response_text
    def complete(self, messages, tools):
        from src.tools.base import ModelResponse
        return ModelResponse(text=self.response_text, input_tokens=100, output_tokens=50)


def test_full_three_layer_flow(tmp_path):
    """Simulate context overflow triggering all three layers."""
    config = AgentConfig(
        working_directory=tmp_path,
        max_context_tokens=5_000,
        truncate_line_threshold=2_000,
        truncate_byte_threshold=50_000,
        prune_protect=1_000,
        prune_minimum=500,
        compaction_buffer=1_000,
    )
    wm = ContextWindowManager(config, tmp_path, "test-session",
                              model_client=_FakeModel())
    builder = ContextBuilder(config, ToolRegistry(), window_manager=wm)

    # Step 1: Add a tool result that exceeds thresholds
    big_output = ("x" * 100 + "\n") * 3_000  # 3000 lines
    result = ToolResult(success=True, output=big_output)
    builder.add_tool_result("read_file", result, tool_call_id="big_read")

    # Verify truncation happened
    history = builder.history_snapshot()
    assert "<truncated-output>" in history[0].content

    # Step 2: Simulate conversation growth until overflow
    for i in range(20):
        builder.add_user_message(f"question {i}")
        builder.add_assistant_message(f"answer {i}")
        builder.add_tool_result("read_file", ToolResult(success=True, output=f"file_{i}" * 500),
                                tool_call_id=f"call_{i}")

    # Step 3: Trigger compaction via check
    current = Message(role="user", content="final question")
    messages = builder.history_snapshot()
    result = wm.check_and_compact(messages, current)

    # Should have triggered compaction
    assert result is not None
    assert result.status == "continue"
    assert result.summary_msg is not None
    assert result.summary_msg.summary is True


def test_build_filters_compaction_messages(tmp_path):
    config = AgentConfig(working_directory=tmp_path)
    builder = ContextBuilder(config, ToolRegistry())
    builder._history.extend([
        Message(role="compaction", content="meta"),
        Message(role="user", content="hello"),
        Message(role="assistant", content="hi"),
    ])
    built = builder.build()
    roles = [m.role for m in built]
    assert "compaction" not in roles
    assert "user" in roles


def test_pruning_before_compaction(tmp_path):
    """Verify pruning runs and reduces tokens before compaction is considered."""
    config = AgentConfig(
        working_directory=tmp_path,
        max_context_tokens=100_000,
        prune_protect=5_000,
        prune_minimum=500,
        compaction_buffer=10_000,
    )
    wm = ContextWindowManager(config, tmp_path, "test-prune-first",
                              model_client=_FakeModel())

    messages = []
    # Create many old tool outputs that can be pruned
    for i in range(10):
        messages.append(Message(role="user", content=f"old q{i}"))
        messages.append(Message(role="assistant", content=f"old a{i}"))
        messages.append(Message(role="tool", content="x" * 10_000, tool_name="read_file"))

    # Add recent messages (protected)
    messages.append(Message(role="user", content="recent q"))
    messages.append(Message(role="assistant", content="recent a"))

    # With abundant prunable content, pruning alone should suffice
    current = Message(role="user", content="new question")
    result = wm.check_and_compact(messages, current)

    # Check that old messages were pruned
    assert messages[2].content == "[tool result pruned]"
    assert messages[2].compacted is True
```

- [ ] **Step 2: Run integration tests**

```bash
cd F:/Forge-Code && python -m pytest tests/test_integration_compression.py -v
```
Expected: all PASS

- [ ] **Step 3: Run full test suite**

```bash
cd F:/Forge-Code && python -m pytest tests/ -v
```
Expected: all 30+ tests PASS

- [ ] **Step 4: Commit**

```bash
git add tests/test_integration_compression.py
git commit -m "test: add integration tests for 3-layer compression flow"
```

---

### Summary of Commits

1. `feat: add summary/compacted fields to Message and config constants for 3-layer compression`
2. `feat: add Layer 1 ToolOutputTruncator with AND-logic head truncation`
3. `feat: add Layer 2 ToolOutputPruner with reverse-scan algorithm`
4. `feat: add Layer 3 Compactor with 7-section structured LLM summary`
5. `feat: add ShellTruncator tail truncation for shell command output`
6. `refactor: rewrite ContextWindowManager as 3-layer orchestrator`
7. `feat: integrate 3-layer compression into ContextBuilder and AgentRuntime`
8. `feat: wire ShellTruncator to RunCommandTool via ContextWindowManager`
9. `test: add integration tests for 3-layer compression flow`
