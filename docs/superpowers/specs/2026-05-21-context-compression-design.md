# Context Compression Strategy Redesign

## Overview

Replace the current 4-tier context management with a 3-layer progressive compression system:

| Layer | Name | Trigger | Cost |
|-------|------|---------|------|
| 1 | Truncation | Every tool execution (sync) | Zero |
| 2 | Pruning | Context overflow check | Zero |
| 3 | Compaction | Overflow persists after pruning | 1 LLM call |

## Layer 1: Truncation -- Per-Tool Output Truncation

### Trigger

After every tool execution, before the result enters the message deque. Preventive, not reactive.

### Criteria (AND logic)

Both conditions must be true simultaneously:

```
lines > 2,000 AND bytes > 50,000 (UTF-8)
```

Single-dimension overflow does NOT trigger truncation.

### Direction

| Tool type | Direction | Rationale |
|-----------|-----------|-----------|
| All tools (default) | **head** | First part of output is most valuable (file reads, grep results, glob lists) |
| Shell | **tail** | Last N lines are results (compile errors, test failures, log tail) |

Shell implements its own `tail()` truncation; other tools go through the framework's default head truncation.

### Disk spill

Truncated full output is saved to:
```
{base_dir}/.forgecode/sessions/{session_id}/truncated/{tool_call_id}.txt
```

The truncated in-memory content references the disk path and includes a preview (first ~2KB).

### Cleanup

Lazy cleanup on directory access: if last cleanup was >1 hour ago, delete files older than 7 days. No background thread needed.

### Configuration

```toml
[agent]
truncate_line_threshold = 2000
truncate_byte_threshold = 50000
truncate_cleanup_interval = 3600
truncate_max_age = 604800
```

### Turn Definition

A "turn" is a user→assistant roundtrip: from a `role="user"` message up to (but not including) the next `role="user"` message. This same definition is used by both Pruning (Layer 2) and Compaction (Layer 3).

## Layer 2: Pruning -- Reverse Tool Output Pruning

### Concept

Zero LLM cost. Reverse-scan tool results and replace old ones with `[tool result pruned]` placeholder. Old tool output (e.g., file content from 20 turns ago) is no longer useful.

### Configuration

| Constant | Value | Meaning |
|----------|-------|---------|
| PRUNE_PROTECT | 40,000 | Keep most recent 40K tokens of tool output |
| PRUNE_MINIMUM | 20,000 | Must recover at least 20K tokens to execute |

### Algorithm (reverse scan)

```
For each message from last to first:
  ├─ Skip last 2 turns (always protected)
  ├─ Encounter summary=True message → STOP immediately
  ├─ Encounter role="tool", compacted=False:
  │   ├─ Cumulative tokens <= PRUNE_PROTECT → continue accumulating (protected)
  │   └─ Cumulative tokens > PRUNE_PROTECT → add to prune queue
  └─ After scan: if prune queue total > PRUNE_MINIMUM → execute replacement
      Otherwise: skip (not worth the churn)
```

### Replacement

Each pruned message:
- `content` → `"[tool result pruned]"`
- `compacted` → `True`

### Relationship to Compaction

- Pruning succeeds (recovered > 20K tokens) → compaction not needed
- Pruning insufficient → triggers Layer 3 compaction
- summary message acts as a stop marker: content before summary has already been semantically compressed

## Layer 3: Compaction -- LLM Semantic Compression

### Trigger Flow

```
User sends message
  → runLoop checks deque head for compaction message
    → Found: processCompaction() → inject summary → replay/continue
    → Not found: check context overflow
      → Overflow + pruning insufficient: create compaction user message, insert into deque
```

### Configuration

| Constant | Value | Meaning |
|----------|-------|---------|
| COMPACTION_BUFFER | 20,000 | Token space reserved for compaction LLM call itself |
| TAIL_BUDGET_RATIO | 0.25 | Tail gets 25% of usable context |
| TAIL_CLAMP_MIN | 2,000 | Minimum tail token budget |
| TAIL_CLAMP_MAX | 8,000 | Maximum tail token budget |
| TAIL_MIN_TURNS | 2 | Minimum turns to keep in tail |
| TOOL_OUTPUT_MAX_CHARS | 2,000 | Tool output truncated to this length during compaction |

### Five Steps

#### Step 1: select() -- Split head/tail

```
usable = max_context_tokens - COMPACTION_BUFFER
tail_budget = clamp(usable * 0.25, TAIL_CLAMP_MIN, TAIL_CLAMP_MAX)

Accumulate turns from newest backward:
  ├─ Fits in tail_budget → keep in tail
  ├─ Doesn't fit → try splitTurn() (cut within turn at message boundary)
  ├─ Fallback: keep at least 1 turn in tail
  └─ Rest → head (to be compressed)

Head must have >= 1 turn worth compressing, otherwise return "stop"
```

#### Step 2: buildPrompt()

**First-time compaction** (no previous summary):
```
Create a new anchored summary from the conversation history above.
Output in the following 7 sections:
## Goal
## Constraints
## Progress
## Key Decisions
## Next Steps
## Critical Context
## Relevant Files
```

**Incremental update** (has previous summary):
```
Below is the previous summary and new conversation history.
Preserve still-true details, remove stale details, merge in new facts.
Previous summary: {previous_summary}
Output the updated summary in the same 7 sections.
```

#### Step 3: Call Compaction LLM

- Uses the same model as the main conversation
- `stripMedia: true` -- remove all image/file attachments, text only
- `toolOutputMaxChars: 2000` -- truncate tool outputs to 2000 characters
- Returns summary text or None on failure

Three possible outcomes:
| Result | Meaning |
|--------|---------|
| `"continue"` | Compaction successful |
| `"stop"` | LLM error or head too small |
| `"compact"` | Summary itself is too large, recursive compaction needed |

#### Step 4: Inject Structured Summary

The LLM output is parsed into 7 fixed sections. The summary is injected as an **assistant** message with `summary=True`.

#### Step 5: Auto-continue

If `overflow == true`: replay the current user message (strip media attachments, replace with text placeholders like `[Attached image/png: photo.png]`).

If `overflow == false`: insert a synthetic continue message: `"Continue if you have next steps, or stop..."`

### Final Message Structure

```
[compaction-msg]          ← role="compaction", carries tail_start_id metadata
[summary assistant msg]   ← 7-section structured summary, summary=True
[tail turn 1: user]       ← preserved verbatim
[tail turn 1: assistant]  ← preserved verbatim
[tail turn 2: user]       ← preserved verbatim
[tail turn 2: assistant]  ← preserved verbatim
[current user msg]        ← user's current message (or replayed version)
```

## Message Model Changes

Two new fields added to `Message`:

```python
@dataclasses.dataclass
class Message:
    role: str
    content: str
    tool_calls: list[ToolCall] | None = None
    tool_call_id: str | None = None
    tool_name: str | None = None
    reasoning_content: str | None = None
    summary: bool = False        # NEW: marks summary messages
    compacted: bool = False      # NEW: marks processed-by-pruning/compaction messages
```

CompactionPart is represented as `role="compaction"` with metadata in content.

**Important**: `role="compaction"` messages must be intercepted before API calls. `ContextBuilder.build()` filters out compaction messages. `_to_anthropic_messages()` and `_to_openai_messages()` treat unknown roles as errors, so compaction messages must never reach them.

**Session serialization**: `summary` and `compacted` fields are persisted in session JSON via `_message_to_dict()` / `_message_from_dict()`.

## Full Flow Diagram

```
Tool execution completed
  │
  ├─ Layer 1: Truncation (sync, every tool)
  │   AND(>2000 lines, >50KB) → truncate + spill to disk
  │   Shell: independent tail truncation
  │
  ▼
Tool result added to deque, tokens accumulate
  │
  ▼
User sends next message
  │
  ├─ Deque head has compaction message?
  │   YES → processCompaction() → inject summary → replay/continue
  │   NO  → overflow check
  │
  ├─ Overflow: token >= usable(context)?
  │   │
  │   ├─ Layer 2: Pruning (no LLM)
  │   │   Reverse scan, protect 40K, recover >20K → done
  │   │   Recovery <20K → proceed to Layer 3
  │   │
  │   └─ Layer 3: Compaction (LLM call)
  │       select() → buildPrompt() → LLM → inject summary
  │       Status: "continue" / "stop" / "compact"
  │       Auto-continue or replay
  │
  ▼
Normal agent loop continues
```

## Files to Create

| File | Purpose |
|------|---------|
| `src/main/truncation.py` | `ToolOutputTruncator` class (Layer 1) |
| `src/main/pruning.py` | `ToolOutputPruner` class (Layer 2) |
| `src/main/compaction.py` | `Compactor` class (Layer 3) |

## Files to Modify

| File | Changes |
|------|---------|
| `src/main/context.py` | Add `summary`, `compacted` fields to `Message` |
| `src/main/context_manager.py` | Complete rewrite: replace 4-tier with 3-layer `ContextWindowManager` |
| `src/main/config.py` | Add Layer 1/2/3 configuration constants |
| `src/main/runtime.py` | Add compaction message check before processing user input |
| `src/tools/shell.py` | Implement `ShellTruncator` tail truncation |

## Files to Remove

| File / Code | Reason |
|-------------|--------|
| `context_manager.py::TokenBudget` | Replaced by simpler token estimation in pruning/compaction |
| `context_manager.py::BudgetTruncator` | Replaced by Layer 1 Truncation |
| `context_manager.py::SnipProcessor` | Replaced by Layer 2 Pruning |
| `context_manager.py::MicroCompactor` | Removed (no longer needed) |
| `context_manager.py::AutoCompactor` | Replaced by Layer 3 Compaction |
| `context_manager.py::PreEntryProcessor` | Replaced by Layer 1 Truncation |

## Testing Strategy

- **Layer 1**: Unit test `should_truncate()` with boundary conditions (<2000 lines, <50KB, exactly at thresholds, both exceeded). Test shell tail vs head truncation independently.
- **Layer 2**: Unit test pruning with mock message lists. Verify: last 2 turns protected, summary stops scan, token accounting correct, PRUNE_MINIMUM gate works.
- **Layer 3**: Mock LLM client to return known summary text. Test select() partitioning, prompt building, summary parsing, message structure after compaction. Test incremental (with prior summary) vs first-time modes.
- **Integration**: Test full 3-layer flow end-to-end with simulated context overflow scenarios.
