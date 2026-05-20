"""Agent runtime — main loop, model clients, tool execution."""

from __future__ import annotations

import concurrent.futures
import copy
import dataclasses
import json
import re
import uuid
from pathlib import Path
from typing import Any, Callable, Generator, Protocol


# ---------------------------------------------------------------------------
# Streaming chunk type
# ---------------------------------------------------------------------------

@dataclasses.dataclass(frozen=True)
class StreamChunk:
    """A single chunk from a streaming model response.

    chunk_type is "text" for regular content or "thinking" for reasoning tokens.
    """
    text: str
    chunk_type: str = "text"  # "text" | "thinking"

from main.config import AgentConfig, DangerousMode
from main.context import ContextBuilder, Message
from main.context_manager import ContextWindowManager
from main.executor import group_tool_calls
from cli.io import AgentIO
from safety.permissions import SafetyLabel, PermissionChecker, PermissionRequest
from main.session import SessionData, SessionManager
from main.token_tracker import TokenUsageTracker
from tools.base import ModelResponse, ToolCall, ToolResult
from tools.registry import ToolRegistry


# ---------------------------------------------------------------------------
# Model client protocol
# ---------------------------------------------------------------------------

class ModelClient(Protocol):
    def complete(self, messages: list[Message], tools: list[dict[str, Any]]) -> ModelResponse: ...


# ---------------------------------------------------------------------------
# Placeholder client  (parses /read, /ls, /pwd from the *last user message*)
# ---------------------------------------------------------------------------

class PlaceholderModelClient:
    """Simulates model responses by parsing slash commands."""

    def complete(self, messages: list[Message], tools: list[dict[str, Any]]) -> ModelResponse:
        user_msgs = [m for m in messages if m.role == "user"]
        if not user_msgs:
            return ModelResponse(text="(no user input)")
        text = user_msgs[-1].content.strip()

        if text.startswith("/read "):
            path = text[6:].strip()
            return ModelResponse(
                text="",
                tool_calls=[ToolCall("read_file", {"path": path}, id=str(uuid.uuid4()))],
                stop_reason="tool_use",
            )

        if text == "/ls" or text.startswith("/ls "):
            parts = text.split(maxsplit=1)
            args: dict[str, Any] = {"path": parts[1]} if len(parts) > 1 else {}
            return ModelResponse(
                text="",
                tool_calls=[ToolCall("list_directory", args, id=str(uuid.uuid4()))],
                stop_reason="tool_use",
            )

        if text == "/pwd":
            return ModelResponse(
                text="",
                tool_calls=[ToolCall("list_directory", {}, id=str(uuid.uuid4()))],
                stop_reason="tool_use",
            )

        return ModelResponse(
            text=f"[Echo] {text}\n(Placeholder model — use /read, /ls, /pwd to test tools, "
                 "or --model claude for real AI)",
            stop_reason="end_turn",
        )

    def complete_stream(
        self, messages: list[Message], tools: list[dict[str, Any]]
    ) -> Generator[str, None, ModelResponse]:
        response = self.complete(messages, tools)
        if response.text:
            yield response.text
        return response


# ---------------------------------------------------------------------------
# Anthropic client (optional)
# ---------------------------------------------------------------------------

class AnthropicModelClient:
    """Uses the Anthropic SDK for real Claude completions with tool use."""

    def __init__(
        self,
        model: str = "claude-sonnet-4-20250514",
        api_key: str | None = None,
        base_url: str | None = None,
        max_tokens: int = 4096,
        show_thinking: bool = False,
        thinking_budget: int = 10_000,
    ) -> None:
        import anthropic
        kwargs: dict[str, Any] = {}
        if api_key:
            kwargs["api_key"] = api_key
        if base_url:
            kwargs["base_url"] = base_url
        self._client = anthropic.Anthropic(**kwargs)
        self._model = model
        self._show_thinking = show_thinking
        self._thinking_budget = thinking_budget
        # When thinking is enabled, max_tokens must exceed thinking_budget
        if show_thinking and max_tokens <= thinking_budget:
            self._max_tokens = thinking_budget + max_tokens
        else:
            self._max_tokens = max_tokens
        # Cache for thinking blocks needed in multi-turn conversations
        self._cached_thinking_blocks: list[dict[str, Any]] = []

    def complete(self, messages: list[Message], tools: list[dict[str, Any]]) -> ModelResponse:
        api_messages = _to_anthropic_messages(messages)
        system_prompt = ""
        if api_messages and api_messages[0]["role"] == "system":
            system_prompt = api_messages.pop(0)["content"]

        # Inject cached thinking blocks into the last assistant message
        if self._cached_thinking_blocks and api_messages:
            api_messages = self._inject_thinking_blocks(api_messages)

        api_tools = _to_anthropic_tools(tools)

        kwargs: dict[str, Any] = {
            "model": self._model,
            "max_tokens": self._max_tokens,
            "messages": api_messages,
        }
        if system_prompt:
            kwargs["system"] = system_prompt
        if api_tools:
            kwargs["tools"] = api_tools
        if self._show_thinking:
            kwargs["thinking"] = {
                "type": "enabled",
                "budget_tokens": self._thinking_budget,
            }

        response = self._client.messages.create(**kwargs)

        text_parts: list[str] = []
        thinking_parts: list[str] = []
        tool_calls: list[ToolCall] = []
        new_thinking_blocks: list[dict[str, Any]] = []

        for block in response.content:
            if block.type == "thinking":
                thinking_parts.append(block.thinking)
                new_thinking_blocks.append({
                    "type": "thinking",
                    "thinking": block.thinking,
                    "signature": block.signature,
                })
            elif block.type == "text":
                text_parts.append(block.text)
            elif block.type == "tool_use":
                tool_calls.append(ToolCall(
                    tool_name=block.name,
                    arguments=block.input if isinstance(block.input, dict) else {},
                    id=block.id,
                ))

        # Cache thinking blocks for multi-turn
        self._cached_thinking_blocks = new_thinking_blocks

        reasoning_content = "\n".join(thinking_parts) if thinking_parts else None

        return ModelResponse(
            text="\n".join(text_parts),
            tool_calls=tool_calls,
            stop_reason=response.stop_reason,
            reasoning_content=reasoning_content,
            input_tokens=response.usage.input_tokens,
            output_tokens=response.usage.output_tokens,
        )

    def complete_stream(
        self, messages: list[Message], tools: list[dict[str, Any]]
    ) -> Generator[str | StreamChunk, None, ModelResponse]:
        api_messages = _to_anthropic_messages(messages)
        system_prompt = ""
        if api_messages and api_messages[0]["role"] == "system":
            system_prompt = api_messages.pop(0)["content"]

        # Inject cached thinking blocks into the last assistant message
        if self._cached_thinking_blocks and api_messages:
            api_messages = self._inject_thinking_blocks(api_messages)

        api_tools = _to_anthropic_tools(tools)

        kwargs: dict[str, Any] = {
            "model": self._model,
            "max_tokens": self._max_tokens,
            "messages": api_messages,
        }
        if system_prompt:
            kwargs["system"] = system_prompt
        if api_tools:
            kwargs["tools"] = api_tools
        if self._show_thinking:
            kwargs["thinking"] = {
                "type": "enabled",
                "budget_tokens": self._thinking_budget,
            }

        text_parts: list[str] = []
        thinking_parts: list[str] = []
        tool_calls: list[ToolCall] = []
        current_tool_call: ToolCall | None = None
        current_block_type: str | None = None
        current_signature: str = ""
        stop_reason: str | None = None
        input_tokens: int | None = None
        output_tokens: int | None = None
        new_thinking_blocks: list[dict[str, Any]] = []

        with self._client.messages.create(stream=True, **kwargs) as stream:
            for event in stream:
                if event.type == "message_start":
                    if hasattr(event.message, "usage") and event.message.usage:
                        input_tokens = event.message.usage.input_tokens

                elif event.type == "content_block_start":
                    block = event.content_block
                    if block.type == "tool_use":
                        current_block_type = "tool_use"
                        current_tool_call = ToolCall(
                            tool_name=block.name,
                            arguments=block.input if isinstance(block.input, dict) else {},
                            id=block.id,
                        )
                    elif block.type == "thinking":
                        current_block_type = "thinking"
                        current_signature = ""
                    elif block.type == "text":
                        current_block_type = "text"

                elif event.type == "content_block_delta":
                    if event.delta.type == "text_delta":
                        text_parts.append(event.delta.text)
                        yield event.delta.text
                    elif event.delta.type == "thinking_delta":
                        thinking_parts.append(event.delta.thinking)
                        yield StreamChunk(text=event.delta.thinking, chunk_type="thinking")
                    elif event.delta.type == "signature_delta":
                        current_signature += event.delta.signature

                elif event.type == "content_block_stop":
                    if current_tool_call is not None:
                        tool_calls.append(current_tool_call)
                        current_tool_call = None
                    if current_block_type == "thinking":
                        thinking_text = "".join(thinking_parts)
                        new_thinking_blocks.append({
                            "type": "thinking",
                            "thinking": thinking_text,
                            "signature": current_signature,
                        })
                    current_block_type = None

                elif event.type == "message_delta":
                    stop_reason = event.delta.stop_reason
                    usage = getattr(event, "usage", None)
                    if usage is not None:
                        output_tokens = getattr(usage, "output_tokens", None)

        # Cache thinking blocks for multi-turn
        self._cached_thinking_blocks = new_thinking_blocks

        reasoning_content = "".join(thinking_parts) if thinking_parts else None

        return ModelResponse(
            text="".join(text_parts),
            tool_calls=tool_calls,
            stop_reason=stop_reason,
            reasoning_content=reasoning_content,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
        )

    def _inject_thinking_blocks(
        self, api_messages: list[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        """Inject cached thinking blocks into the last assistant message.

        The Anthropic API requires thinking blocks with signatures to be present
        in assistant messages for multi-turn conversations with extended thinking.
        """
        for i in range(len(api_messages) - 1, -1, -1):
            if api_messages[i]["role"] == "assistant":
                content = api_messages[i]["content"]
                if isinstance(content, str):
                    content = [{"type": "text", "text": content}]
                # Prepend thinking blocks before text/tool_use blocks
                api_messages[i]["content"] = self._cached_thinking_blocks + content
                break
        self._cached_thinking_blocks = []
        return api_messages


def _to_anthropic_messages(messages: list[Message]) -> list[dict[str, Any]]:
    """Convert internal Messages to Anthropic API format with native tool blocks."""
    result: list[dict[str, Any]] = []
    for m in messages:
        if m.role == "system":
            result.append({"role": "system", "content": m.content})
        elif m.role == "user":
            result.append({"role": "user", "content": m.content})
        elif m.role == "assistant":
            if m.tool_calls:
                content: list[dict[str, Any]] = []
                if m.content:
                    content.append({"type": "text", "text": m.content})
                for tc in m.tool_calls:
                    content.append({
                        "type": "tool_use",
                        "id": tc.id or str(uuid.uuid4()),
                        "name": tc.tool_name,
                        "input": tc.arguments,
                    })
                result.append({"role": "assistant", "content": content})
            else:
                result.append({"role": "assistant", "content": m.content})
        elif m.role == "tool":
            # Anthropic requires tool results as user messages with tool_result blocks.
            # Consecutive tool results should be merged into one user message.
            tool_result_block = {
                "type": "tool_result",
                "tool_use_id": m.tool_call_id or "",
                "content": m.content,
            }
            if result and result[-1]["role"] == "user" and isinstance(result[-1]["content"], list):
                result[-1]["content"].append(tool_result_block)
            else:
                result.append({"role": "user", "content": [tool_result_block]})
    return result


def _to_anthropic_tools(tools: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        {
            "name": t["name"],
            "description": t["description"],
            "input_schema": t["parameters"],
        }
        for t in tools
    ]


# ---------------------------------------------------------------------------
# OpenAI-compatible client (DeepSeek, OpenAI, etc.)
# ---------------------------------------------------------------------------

class OpenAIModelClient:
    """Uses the OpenAI SDK for any OpenAI-compatible API (DeepSeek, OpenAI, etc.)."""

    def __init__(
        self,
        model: str,
        api_key: str | None = None,
        base_url: str | None = None,
    ) -> None:
        from openai import OpenAI
        kwargs: dict[str, Any] = {}
        if api_key:
            kwargs["api_key"] = api_key
        if base_url:
            kwargs["base_url"] = base_url
        self._client = OpenAI(**kwargs)
        self._model = model

    def complete(self, messages: list[Message], tools: list[dict[str, Any]]) -> ModelResponse:
        api_messages = _to_openai_messages(messages)
        api_tools = _to_openai_tools(tools) or None

        kwargs: dict[str, Any] = {
            "model": self._model,
            "messages": api_messages,
        }
        if api_tools:
            kwargs["tools"] = api_tools

        response = self._client.chat.completions.create(**kwargs)

        choice = response.choices[0]
        text = choice.message.content or ""
        # Capture reasoning/thinking content (e.g. DeepSeek R1, QwQ, etc.)
        reasoning_content: str | None = getattr(choice.message, "reasoning_content", None)
        tool_calls: list[ToolCall] = []

        if choice.message.tool_calls:
            for tc in choice.message.tool_calls:
                try:
                    arguments = json.loads(tc.function.arguments)
                except json.JSONDecodeError:
                    arguments = {}
                tool_calls.append(ToolCall(
                    tool_name=tc.function.name,
                    arguments=arguments,
                    id=tc.id,
                ))

        finish = choice.finish_reason or ""
        stop_reason = "tool_use" if finish == "tool_calls" else finish

        usage = response.usage
        return ModelResponse(
            text=text,
            tool_calls=tool_calls,
            stop_reason=stop_reason,
            reasoning_content=reasoning_content,
            input_tokens=usage.prompt_tokens if usage else None,
            output_tokens=usage.completion_tokens if usage else None,
        )

    def complete_stream(
        self, messages: list[Message], tools: list[dict[str, Any]]
    ) -> Generator[str | StreamChunk, None, ModelResponse]:
        api_messages = _to_openai_messages(messages)
        api_tools = _to_openai_tools(tools) or None

        kwargs: dict[str, Any] = {
            "model": self._model,
            "messages": api_messages,
            "stream": True,
            "stream_options": {"include_usage": True},
        }
        if api_tools:
            kwargs["tools"] = api_tools

        stream = self._client.chat.completions.create(**kwargs)

        text_parts: list[str] = []
        tool_call_builders: dict[int, dict[str, str]] = {}
        reasoning_parts: list[str] = []
        finish_reason: str = ""
        input_tokens: int | None = None
        output_tokens: int | None = None

        for chunk in stream:
            if chunk.usage is not None:
                input_tokens = chunk.usage.prompt_tokens
                output_tokens = chunk.usage.completion_tokens
            if not chunk.choices:
                continue
            delta = chunk.choices[0].delta
            finish = chunk.choices[0].finish_reason
            if finish:
                finish_reason = finish

            if delta is None:
                continue

            # Text content
            if delta.content:
                text_parts.append(delta.content)
                yield delta.content

            # Reasoning content (e.g. DeepSeek R1)
            reasoning = getattr(delta, "reasoning_content", None)
            if reasoning:
                reasoning_parts.append(reasoning)
                yield StreamChunk(text=reasoning, chunk_type="thinking")

            # Tool calls (arrive incrementally across chunks)
            if delta.tool_calls:
                for tc_delta in delta.tool_calls:
                    idx = tc_delta.index
                    if idx not in tool_call_builders:
                        tool_call_builders[idx] = {"name": "", "arguments": "", "id": ""}
                    builder = tool_call_builders[idx]
                    if tc_delta.id:
                        builder["id"] = tc_delta.id
                    if tc_delta.function:
                        if tc_delta.function.name:
                            builder["name"] = tc_delta.function.name
                        if tc_delta.function.arguments:
                            builder["arguments"] += tc_delta.function.arguments

        # Build final tool calls from accumulated partial data
        tool_calls: list[ToolCall] = []
        for idx in sorted(tool_call_builders):
            b = tool_call_builders[idx]
            try:
                arguments = json.loads(b["arguments"])
            except json.JSONDecodeError:
                arguments = {}
            tool_calls.append(ToolCall(
                tool_name=b["name"],
                arguments=arguments,
                id=b["id"],
            ))

        stop_reason = "tool_use" if finish_reason == "tool_calls" else finish_reason
        reasoning_content = "".join(reasoning_parts) if reasoning_parts else None

        return ModelResponse(
            text="".join(text_parts),
            tool_calls=tool_calls,
            stop_reason=stop_reason,
            reasoning_content=reasoning_content,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
        )


def _to_openai_messages(messages: list[Message]) -> list[dict[str, Any]]:
    """Convert internal Messages to OpenAI API format with native tool_calls."""
    result: list[dict[str, Any]] = []
    for m in messages:
        if m.role == "system":
            result.append({"role": "system", "content": m.content})
        elif m.role == "user":
            result.append({"role": "user", "content": m.content})
        elif m.role == "assistant":
            msg: dict[str, Any] = {"role": "assistant", "content": m.content or None}
            if m.reasoning_content:
                msg["reasoning_content"] = m.reasoning_content
            if m.tool_calls:
                msg["tool_calls"] = [
                    {
                        "id": tc.id or str(uuid.uuid4()),
                        "type": "function",
                        "function": {
                            "name": tc.tool_name,
                            "arguments": json.dumps(tc.arguments),
                        },
                    }
                    for tc in m.tool_calls
                ]
            result.append(msg)
        elif m.role == "tool":
            result.append({
                "role": "tool",
                "tool_call_id": m.tool_call_id or "",
                "content": m.content,
            })
    return _sanitize_openai_tool_message_groups(result)


def _sanitize_openai_tool_message_groups(
    messages: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Drop incomplete tool-call groups that can appear after history truncation."""
    sanitized: list[dict[str, Any]] = []
    i = 0
    while i < len(messages):
        message = messages[i]
        if message.get("role") == "tool":
            i += 1
            continue

        tool_calls = message.get("tool_calls")
        if message.get("role") == "assistant" and tool_calls:
            expected_ids = [
                tc.get("id")
                for tc in tool_calls
                if isinstance(tc, dict) and tc.get("id")
            ]
            group = [message]
            seen_ids: list[str] = []
            j = i + 1
            while j < len(messages) and messages[j].get("role") == "tool":
                tool_message = messages[j]
                group.append(tool_message)
                tool_call_id = tool_message.get("tool_call_id")
                if isinstance(tool_call_id, str) and tool_call_id:
                    seen_ids.append(tool_call_id)
                j += 1

            if (
                len(expected_ids) == len(seen_ids)
                and set(expected_ids) == set(seen_ids)
            ):
                sanitized.extend(group)
            i = j
            continue

        sanitized.append(message)
        i += 1
    return sanitized


def _to_openai_tools(tools: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        {
            "type": "function",
            "function": {
                "name": t["name"],
                "description": t["description"],
                "parameters": t["parameters"],
            },
        }
        for t in tools
    ]


# ---------------------------------------------------------------------------
# Tool call parser (for text-based model output)
# ---------------------------------------------------------------------------

_TOOL_CALL_RE = re.compile(
    r"\[TOOL_CALL\]\s*name:\s*(\S+)\s*arguments:\s*(\{.*?\})\s*\[/TOOL_CALL\]",
    re.DOTALL,
)


def parse_tool_call(text: str) -> ToolCall | None:
    m = _TOOL_CALL_RE.search(text)
    if not m:
        return None
    try:
        args = json.loads(m.group(2))
    except json.JSONDecodeError:
        return None
    return ToolCall(tool_name=m.group(1), arguments=args, id=str(uuid.uuid4()))


# ---------------------------------------------------------------------------
# Agent Runtime
# ---------------------------------------------------------------------------

class AgentRuntime:
    """Interactive agent loop with multi-step tool chaining."""

    def __init__(
        self,
        config: AgentConfig,
        registry: ToolRegistry,
        permissions: PermissionChecker,
        io: AgentIO,
        model_client: ModelClient,
        *,
        session_manager: SessionManager | None = None,
        session: SessionData | None = None,
    ) -> None:
        self._config = config
        self._registry = registry
        self._permissions = permissions
        self._io = io
        self._model = model_client
        self._session_manager = session_manager
        self._session = session

        # Context window management
        window_manager: ContextWindowManager | None = None
        on_replace = None
        if config.enable_context_management:
            spill_dir = config.context_spill_dir or (config.working_directory / ".forgecode" / "context_spill")
            summarize_fn: Callable[[str], str | None] | None = None
            if config.auto_compact_enabled and not isinstance(model_client, PlaceholderModelClient):
                def _summarize(text: str) -> str | None:
                    try:
                        resp = model_client.complete(
                            [Message(role="user", content=text)], []
                        )
                        return resp.text or None
                    except Exception:
                        return None
                summarize_fn = _summarize
            window_manager = ContextWindowManager(config, spill_dir, summarize_fn)

        if session is not None:
            def _on_replace(messages: list[Message]) -> None:
                session.context_messages = [copy.deepcopy(m) for m in messages]
            on_replace = _on_replace

        def _record_session_message(msg: Message) -> None:
            if session is not None:
                session.messages.append(copy.deepcopy(msg))

        on_message = _record_session_message if session is not None else None
        self._context = ContextBuilder(
            config, registry, on_message=on_message,
            on_replace=on_replace, window_manager=window_manager,
        )

        if session is not None:
            context_messages = (
                session.context_messages
                if session.context_messages is not None
                else session.messages
            )
            if context_messages:
                self._context.load_history(context_messages)

        # Token usage tracking
        self._token_tracker = TokenUsageTracker(config.max_context_tokens)
        # Memory system state
        self._session_memory_bytes = 0
        if session is not None:
            self._token_tracker.load_from_dict({
                "session_input": session.metadata.total_input_tokens,
                "session_output": session.metadata.total_output_tokens,
                "turn_count": session.metadata.turn_count,
            })

    def run(self) -> None:
        self._io.print_banner(self._config, self._session, self._token_tracker)

        if self._session is not None and getattr(self._session, "resumed", False):
            messages = [
                m for m in self._session.messages
                if m.role in ("user", "assistant") and (m.content or "").strip()
            ]
            if not messages:
                self._io.print_system("已恢复会话，但暂无对话记录")
            else:
                shown = messages[-20:]
                self._io.print_system(f"Loaded {len(messages)} messages. Showing latest 20.")
                for msg in shown:
                    content = self._format_history_content(msg.content)
                    if msg.role == "user":
                        self._io.print_system(f"[user]\n{content}")
                    else:
                        self._io.print_assistant(f"[assistant]\n{content}")

        while True:
            user_input = self._io.prompt_user()
            if user_input is None:
                break
            user_input = user_input.strip()
            if not user_input:
                continue

            if user_input in ("/quit", "/exit"):
                break
            if user_input == "/":
                self._print_commands_list()
                continue
            if user_input == "/help":
                self._print_help()
                continue
            if user_input == "/tools":
                self._print_tools()
                continue
            if user_input == "/usage":
                self._io.print_usage_detail(self._token_tracker)
                continue
            if user_input == "/memory":
                from memory.memory import list_memories
                memories = list_memories(self._config)
                if not memories:
                    self._io.print_system("No memories found for this project.")
                else:
                    self._io.print_system(f"Found {len(memories)} memories:\n")
                    for m in memories:
                        type_badge = f"[{m['type']}]"
                        self._io.print_system(f"  {type_badge:14s} {m['name']} -- {m['description']}")
                continue
            if user_input == "/history" or user_input.startswith("/history "):
                self._print_history(user_input.removeprefix("/history").strip())
                continue

            self._run_agent_turn(user_input)
            self._auto_save()

        self._auto_save()
        self._io.print_system("Goodbye.")

    def run_once(self, user_input: str) -> None:
        """Execute a single non-interactive user turn."""
        self._run_agent_turn(user_input)
        self._auto_save()

    # -- agentic loop -------------------------------------------------------

    def _run_agent_turn(self, user_input: str) -> None:
        """Execute one user turn: call the model in a loop until it stops using tools."""
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

        self._context.add_user_message(user_input)
        self._context.check_idle_compression()
        tools = self._registry.list_tools()

        # Memory injection (consume prefetch result)
        if memory_future is not None:
            try:
                memory_content = memory_future.result(timeout=PREFETCH_TIMEOUT)
                if memory_content:
                    last_msg = self._context._history[-1]
                    last_msg.content += "\n\n" + memory_content
                    self._session_memory_bytes += len(memory_content.encode("utf-8"))
            except Exception:
                pass  # Skip memory on timeout or error

        self._token_tracker.begin_turn()

        for _iteration in range(self._config.max_tool_iterations):
            messages = self._context.build()

            # Check for streaming support
            stream_method = getattr(self._model, "complete_stream", None)
            try:
                if stream_method is not None:
                    try:
                        response = self._consume_stream(stream_method(messages, tools))
                    except Exception:
                        response = self._model.complete(messages, tools)
                        stream_method = None  # treat as non-streaming after fallback
                else:
                    response = self._model.complete(messages, tools)
            except Exception as exc:
                self._io.print_error(f"API Error: {exc}")
                break

            # Record token usage
            if response.input_tokens is not None:
                self._token_tracker.record(response)

            # Update token anchor from API usage
            if response.input_tokens is not None:
                self._context.update_token_anchor(response.input_tokens)

            # Display token summary
            self._print_token_summary()

            # Try text-based fallback parsing if no native tool calls
            tool_calls = response.tool_calls
            if not tool_calls and response.text:
                parsed = parse_tool_call(response.text)
                if parsed:
                    tool_calls = [parsed]

            if not tool_calls:
                # Model is done reasoning — emit final response
                if response.text:
                    self._context.add_assistant_message(response.text, reasoning_content=response.reasoning_content)
                    if stream_method is None:
                        if response.reasoning_content and self._config.show_thinking:
                            self._io.print_thinking(response.reasoning_content)
                        self._io.print_assistant(response.text)
                break

            # Model wants to use tools — record the assistant message with tool calls
            self._context.add_assistant_message(response.text, tool_calls=tool_calls, reasoning_content=response.reasoning_content)
            if response.text and stream_method is None:
                if response.reasoning_content and self._config.show_thinking:
                    self._io.print_thinking(response.reasoning_content)
                self._io.print_assistant(response.text)

            # Execute each tool call
            self._execute_tool_calls(tool_calls)

            # Loop back: the model will see tool results and continue reasoning
        else:
            self._io.print_system(
                f"Reached maximum tool iterations ({self._config.max_tool_iterations}). "
                "Stopping agent loop."
            )

        self._token_tracker.end_turn()

    # -- tool execution -----------------------------------------------------

    def _execute_tool(self, tool_call: ToolCall) -> None:
        """Look up, permission-check, and run a single tool call."""
        tool = self._registry.get(tool_call.tool_name)
        if tool is None:
            msg = f"Unknown tool: {tool_call.tool_name}"
            self._io.print_error(msg)
            self._context.add_tool_result(
                tool_call.tool_name, ToolResult(False, "", msg),
                tool_call_id=tool_call.id,
            )
            return

        # Single permission check (path sandbox + safety label + command policy)
        path_val = tool_call.arguments.get("path")
        path = Path(path_val) if path_val else None

        if path and hasattr(self._config, "needs_path_approval"):
            if self._config.needs_path_approval(path):
                resolved_path = path.resolve()
                approval_dir = resolved_path.parent if not resolved_path.is_dir() else resolved_path
                if not self._confirm_path_access(tool_call, approval_dir):
                    self._io.print_error("Permission denied: Path access denied by user")
                    self._context.add_tool_result(
                        tool_call.tool_name,
                        ToolResult(False, "", "Permission denied: Path access denied by user"),
                        tool_call_id=tool_call.id,
                    )
                    return
                self._config.approve_directory(approval_dir)

        command = tool_call.arguments.get("command")
        req = PermissionRequest(safety_label=tool.safety_label, path=path, command=command)
        result = self._permissions.check(req)
        if not result.allowed:
            self._io.print_error(f"Permission denied: {result.reason}")
            self._context.add_tool_result(
                tool_call.tool_name,
                ToolResult(False, "", f"Permission denied: {result.reason}"),
                tool_call_id=tool_call.id,
            )
            return

        # Interactive confirmation for destructive tools or commands needing approval
        needs_confirm = (
            result.requires_confirmation
            or (tool.safety_label == SafetyLabel.DESTRUCTIVE
                and self._config.allow_dangerous_operations == DangerousMode.ASK)
        )
        if needs_confirm:
            if not self._confirm_dangerous_operation(tool_call):
                self._io.print_error("Operation cancelled by user.")
                self._context.add_tool_result(
                    tool_call.tool_name,
                    ToolResult(False, "", "Operation cancelled by user"),
                    tool_call_id=tool_call.id,
                )
                return

        self._io.print_tool_call(tool_call.tool_name, tool_call.arguments)
        tool_result = tool.run(arguments=tool_call.arguments, config=self._config)
        self._context.add_tool_result(tool_call.tool_name, tool_result, tool_call_id=tool_call.id)
        self._io.print_tool_result(tool_call.tool_name, tool_result, tool_call.arguments)

    # -- parallel execution -------------------------------------------------

    def _execute_tool_calls(self, tool_calls: list[ToolCall]) -> None:
        """Execute a batch of tool calls, parallelising consecutive READONLY ones."""
        if not self._config.parallel_tool_execution or len(tool_calls) <= 1:
            for tc in tool_calls:
                self._execute_tool(tc)
            return

        groups = group_tool_calls(tool_calls, self._registry)
        for group in groups:
            if group.parallel and len(group.tool_calls) > 1:
                self._execute_parallel_batch(group.tool_calls)
            else:
                for tc in group.tool_calls:
                    self._execute_tool(tc)

    def _execute_parallel_batch(self, tool_calls: list[ToolCall]) -> None:
        """Run a batch of READONLY tool calls concurrently via a thread pool.

        Three phases:
        1. Prepare — look up tools and check permissions (serial, fast).
        2. Execute — submit ``tool.run()`` to a thread pool (parallel).
        3. Finalise — record results in original order (serial).
        """

        # Phase 1: prepare -------------------------------------------------
        prepared: list[tuple[ToolCall, Any]] = []  # (tc, tool) pairs
        for tc in tool_calls:
            tool = self._registry.get(tc.tool_name)
            if tool is None:
                msg = f"Unknown tool: {tc.tool_name}"
                self._io.print_error(msg)
                self._context.add_tool_result(
                    tc.tool_name, ToolResult(False, "", msg), tool_call_id=tc.id,
                )
                continue

            path_val = tc.arguments.get("path")
            path = Path(path_val) if path_val else None

            if path and hasattr(self._config, "needs_path_approval"):
                if self._config.needs_path_approval(path):
                    resolved_path = path.resolve()
                    approval_dir = resolved_path.parent if not resolved_path.is_dir() else resolved_path
                    if not self._confirm_path_access(tc, approval_dir):
                        self._io.print_error("Permission denied: Path access denied by user")
                        self._context.add_tool_result(
                            tc.tool_name,
                            ToolResult(False, "", "Permission denied: Path access denied by user"),
                            tool_call_id=tc.id,
                        )
                        continue
                    self._config.approve_directory(approval_dir)

            req = PermissionRequest(safety_label=tool.safety_label, path=path)
            result = self._permissions.check(req)
            if not result.allowed:
                self._io.print_error(f"Permission denied: {result.reason}")
                self._context.add_tool_result(
                    tc.tool_name,
                    ToolResult(False, "", f"Permission denied: {result.reason}"),
                    tool_call_id=tc.id,
                )
                continue

            self._io.print_tool_call(tc.tool_name, tc.arguments)
            prepared.append((tc, tool))

        if not prepared:
            return

        # Phase 2: execute in parallel -------------------------------------
        futures: dict[str, concurrent.futures.Future[ToolResult]] = {}
        max_workers = min(len(prepared), 4)
        with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as pool:
            for tc, tool in prepared:
                key = tc.id or tc.tool_name
                futures[key] = pool.submit(
                    tool.run, arguments=tc.arguments, config=self._config,
                )

        # Phase 3: finalise in original order ------------------------------
        for tc, _tool in prepared:
            key = tc.id or tc.tool_name
            future = futures[key]
            try:
                tool_result = future.result()
            except Exception as exc:
                tool_result = ToolResult(False, "", str(exc))
            self._context.add_tool_result(
                tc.tool_name, tool_result, tool_call_id=tc.id,
            )
            self._io.print_tool_result(tc.tool_name, tool_result, tc.arguments)

    def _confirm_dangerous_operation(self, tool_call: ToolCall) -> bool:
        """Build a confirmation prompt for destructive operations and ask the user."""
        parts: list[str] = [f"Allow {tool_call.tool_name}? (destructive)"]
        path = tool_call.arguments.get("path")
        if path:
            parts.append(f"  Path: {path}")
        command = tool_call.arguments.get("command")
        if command:
            parts.append(f"  Command: {command}")
        return self._io.confirm("\n".join(parts))

    def _confirm_path_access(self, tool_call: ToolCall, directory: Path) -> bool:
        """Prompt the user to approve access to a directory outside the sandbox."""
        parts = [
            f"Allow {tool_call.tool_name} to access a path outside the working directory?",
            f"  Path: {tool_call.arguments.get('path', '?')}",
            f"  Directory to approve: {directory}",
            f"  (This directory will be permitted for the rest of the session)",
        ]
        return self._io.confirm("\n".join(parts))


    def _consume_stream(
        self, stream_gen: Generator[str | StreamChunk, None, ModelResponse]
    ) -> ModelResponse:
        """Iterate a streaming generator, writing tokens to output in real time."""
        in_thinking = False
        has_text = False
        show_thinking = self._config.show_thinking
        try:
            while True:
                token = next(stream_gen)
                if isinstance(token, StreamChunk) and token.chunk_type == "thinking":
                    if show_thinking:
                        if not in_thinking:
                            self._io.print_thinking_start()
                            in_thinking = True
                        self._io.print_thinking_stream(token.text)
                else:
                    if in_thinking:
                        self._io.print_thinking_end()
                        in_thinking = False
                    text = token.text if isinstance(token, StreamChunk) else token
                    self._io.print_stream(text)
                    has_text = True
        except StopIteration as e:
            response = e.value
        if in_thinking:
            self._io.print_thinking_end()
        if has_text:
            self._io.print_stream_end()
        return response

    # -- helpers ------------------------------------------------------------

    def _print_token_summary(self) -> None:
        """Display a one-line token usage summary after each API call."""
        t = self._token_tracker
        self._io.print_token_usage(
            turn_input=t.current_turn.input_tokens,
            turn_output=t.current_turn.output_tokens,
            session_total=t.session_total,
            context_used=t.context_used,
            max_context=t.max_context,
        )

    def _auto_save(self) -> None:
        """Save session data if a session is active. Errors are printed but not raised."""
        if self._session is not None and self._session_manager is not None:
            # Sync token stats to session metadata
            self._session.metadata.total_input_tokens = self._token_tracker.session_input
            self._session.metadata.total_output_tokens = self._token_tracker.session_output
            self._session.metadata.turn_count = self._token_tracker.turn_count
            self._session.context_messages = self._context.history_snapshot()
            try:
                self._session_manager.save(self._session)
            except OSError as exc:
                self._io.print_error(f"Failed to save session: {exc}")

    def _print_help(self) -> None:
        from cli.commands import format_help_text
        session_help = ""
        if self._session is not None:
            session_help = (
                "\n\nSession:\n"
                f"  Active session: {self._session.metadata.session_id}\n"
                "  Use --list-sessions to list all sessions\n"
                "  Use /history [count|all] to view the saved transcript\n"
                "  Use --session <id> to resume a session\n"
                "  Use --resume to resume the latest session"
            )
        self._io.print_system(format_help_text(session_help))

    def _print_commands_list(self) -> None:
        from cli.commands import format_command_list
        self._io.print_system(format_command_list())

    def _print_tools(self) -> None:
        tools = self._registry.list_tools()
        if not tools:
            self._io.print_system("No tools registered.")
            return
        lines = [f"  {t['name']} — {t['description']}" for t in tools]
        self._io.print_system("Available tools:\n" + "\n".join(lines))

    def _print_history(self, raw_limit: str = "") -> None:
        if self._session is None:
            self._io.print_system("No active session.")
            return

        messages = [
            m for m in self._session.messages
            if m.role in ("user", "assistant") and (m.content or "").strip()
        ]
        if not messages:
            self._io.print_system("No conversation history yet.")
            return

        limit = 20
        show_all = False
        raw_limit = raw_limit.strip().lower()
        if raw_limit:
            if raw_limit == "all":
                show_all = True
            else:
                try:
                    limit = max(1, int(raw_limit))
                except ValueError:
                    self._io.print_error("Usage: /history [count|all]")
                    return

        if show_all:
            shown = messages
            self._io.print_system(f"Showing all {len(messages)} conversation messages.")
        else:
            shown = messages[-limit:]
            if len(shown) < len(messages):
                self._io.print_system(
                    f"Showing last {len(shown)} of {len(messages)} conversation messages."
                )
            else:
                self._io.print_system(f"Showing {len(shown)} conversation messages.")

        for msg in shown:
            content = self._format_history_content(msg.content)
            if msg.role == "user":
                self._io.print_system(f"[user]\n{content}")
            else:
                self._io.print_assistant(f"[assistant]\n{content}")

    @staticmethod
    def _format_history_content(content: str) -> str:
        content = content.strip()
        max_chars = 4000
        if len(content) <= max_chars:
            return content
        omitted = len(content) - max_chars
        return f"{content[:max_chars].rstrip()}\n[truncated {omitted:,} chars]"
