# CLAUDE.md

This file provides guidance to ForgeCode (forge-code CLI) when working with code in this repository.

## Build & Development

```bash
# Install (editable, with dev deps)
pipx install --editable --include-deps ".[dev]"

# Run all tests (from any directory — package is flat under src/)
pytest

# Run a single test
pytest tests/test_config.py::test_project_config_path

# Verify no import errors
python -m compileall src
```

## Architecture

The source is at `src/`, installed as five top-level packages (no namespace prefix):

| Package | Role |
|---------|------|
| `main/` | Agent runtime, config, context builder, session, token tracking, context window management (3-layer compression: truncation → pruning → compaction) |
| `cli/` | argparse entry point (`cli/cli.py:main`), interactive I/O, slash commands, tab completion, banner, ESC-key cancellation |
| `tools/` | Tool protocol (`Tool`), registry (`ToolRegistry`), and built-in tools: `read_file`, `list_directory`, `write_file`, `edit_file`, `search`, `run_command` |
| `safety/` | Permission rule chain (`PermissionChecker`), safety labels (`READONLY`/`DESTRUCTIVE`/`CONCURRENT_SAFE`), path sandbox, shell command risk policy (`CommandPolicy`) |
| `memory/` | File-based persistent memory with semantic recall via LLM, frontmatter metadata, MEMORY.md index |

### Request flow

1. `cli/cli.py:main` parses args, builds `DynamicPathConfig` (wrapping `AgentConfig`), creates `ToolRegistry` + `PermissionChecker` + model client (`AnthropicModelClient` or `OpenAIModelClient`)
2. `AgentRuntime.run()` enters the interactive loop — on each turn it prefetches memories in a background thread, builds context via `ContextBuilder`, streams from the model, and executes tool calls
3. Tool execution groups consecutive `READONLY` tools for parallel execution via `ThreadPoolExecutor`; `DESTRUCTIVE` tools run sequentially with interactive confirmation
4. After each tool result, `ContextWindowManager.after_add()` checks token budget and may trigger Layer 2 pruning or defer Layer 3 compaction
5. `ContextWindowManager.process_pending_compaction()` runs at the start of each turn to process any deferred LLM-based compaction

### Key data types (in `tools/base.py`)

- `Message` — role, content, optional tool_calls/tool_call_id/reasoning_content/thinking_blocks
- `ToolCall` — tool_name, arguments dict, optional id (for parallel result correlation)
- `ToolResult` — success bool, output str, optional error
- `ModelResponse` — text, optional tool_calls, stop_reason, reasoning_content, token counts

### Config system

- **Priority**: CLI args > global `~/.forgecode/config.toml` `[model]` > project `.forgecode.toml` `[model]` (legacy fallback)
- For non-model sections (`[agent]`, `[commands]`): global is default, project overrides
- `DynamicPathConfig` wraps the frozen `AgentConfig` to add mutable session-approved directories at runtime

### Session persistence

Sessions are stored as JSON files in `~/.forgecode/sessions/<project-hash>/sessions/`. Each session file contains metadata, full message history, and an optional compacted context snapshot (`context_messages`). Default behavior creates a new session on every launch.

### Context compression layers (in `main/context_manager.py`)

1. **Truncation** (`main/truncation.py`) — sync, per-tool-output; cuts oversized results
2. **Pruning** (`main/pruning.py`) — reverse-scan replacement of old tool outputs with placeholders, zero LLM cost
3. **Compaction** (`main/compaction.py`) — LLM-based semantic summarization of older turns; **deferred** (not blocking on hot path), processed at start of next turn with a configurable timeout

### Dangerous mode

`DangerousMode` enum: `DENY` / `ASK` (default) / `ALLOW`. Controls `DESTRUCTIVE`-labeled tools (write_file, edit_file, run_command). The permission rule chain checks path sandbox first, then dangerous-mode policy.

## Important conventions

- Do NOT use `coding_agent.*` imports — packages are flat (`from main.config import ...`, `from tools.base import ...`, etc.)
- `AgentConfig` is a frozen dataclass — use `DynamicPathConfig` for mutable path approvals
- Tools implement the `Tool` protocol (structural, not inheritance) with `name`, `description`, `parameters_schema`, `safety_label` properties and a `run(*, arguments, config)` method
- Tool calls with safety label `READONLY` and `CONCURRENT_SAFE` may execute in parallel
- The `--no-session` flag disables all session persistence for that run
