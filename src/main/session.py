"""Session persistence — stores conversation history as JSON files."""

from __future__ import annotations

import dataclasses
import json
import os
import secrets
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from main.context import Message
from tools.base import ToolCall


# ---------------------------------------------------------------------------
# Data models
# ---------------------------------------------------------------------------

@dataclasses.dataclass
class SessionMetadata:
    session_id: str
    title: str
    created_at: str  # ISO 8601
    updated_at: str  # ISO 8601
    model_name: str
    provider: str
    working_directory: str
    # Token usage (cumulative for session)
    total_input_tokens: int = 0
    total_output_tokens: int = 0
    turn_count: int = 0


@dataclasses.dataclass
class SessionData:
    metadata: SessionMetadata
    messages: list[Message]
    context_messages: list[Message] | None = None
    resumed: bool = False


# ---------------------------------------------------------------------------
# Serialization helpers
# ---------------------------------------------------------------------------

def _tool_call_to_dict(tc: ToolCall) -> dict[str, Any]:
    return {
        "tool_name": tc.tool_name,
        "arguments": tc.arguments,
        "id": tc.id,
    }


def _tool_call_from_dict(d: dict[str, Any]) -> ToolCall:
    return ToolCall(
        tool_name=d["tool_name"],
        arguments=d.get("arguments", {}),
        id=d.get("id"),
    )


def _message_to_dict(msg: Message) -> dict[str, Any]:
    d: dict[str, Any] = {
        "role": msg.role,
        "content": msg.content,
        "tool_calls": [_tool_call_to_dict(tc) for tc in msg.tool_calls] if msg.tool_calls else None,
        "tool_call_id": msg.tool_call_id,
        "tool_name": msg.tool_name,
        "reasoning_content": msg.reasoning_content,
    }
    return d


def _message_from_dict(d: dict[str, Any]) -> Message:
    raw_tool_calls = d.get("tool_calls")
    tool_calls = [_tool_call_from_dict(tc) for tc in raw_tool_calls] if raw_tool_calls else None
    return Message(
        role=d["role"],
        content=d.get("content", ""),
        tool_calls=tool_calls,
        tool_call_id=d.get("tool_call_id"),
        tool_name=d.get("tool_name"),
        reasoning_content=d.get("reasoning_content"),
    )


def _generate_session_id() -> str:
    """Generate a session ID: YYYYMMDD_HHMMSS_4hex."""
    now = datetime.now(timezone.utc)
    return f"{now.strftime('%Y%m%d_%H%M%S')}_{secrets.token_hex(2)}"


# ---------------------------------------------------------------------------
# SessionManager
# ---------------------------------------------------------------------------

class SessionManager:
    """Manages JSON session files in a directory."""

    def __init__(self, session_dir: Path) -> None:
        self._dir = session_dir
        self._dir.mkdir(parents=True, exist_ok=True)

    @property
    def session_dir(self) -> Path:
        return self._dir

    def create_session(
        self,
        *,
        model_name: str,
        provider: str,
        working_directory: str,
        title: str = "",
    ) -> SessionData:
        now = datetime.now(timezone.utc).isoformat()
        meta = SessionMetadata(
            session_id=_generate_session_id(),
            title=title,
            created_at=now,
            updated_at=now,
            model_name=model_name,
            provider=provider,
            working_directory=working_directory,
        )
        session = SessionData(metadata=meta, messages=[])
        self.save(session)
        return session

    def save(self, session: SessionData) -> None:
        """Atomically save session data to JSON."""
        session.metadata.updated_at = datetime.now(timezone.utc).isoformat()
        data = {
            "version": 1,
            "session_id": session.metadata.session_id,
            "metadata": dataclasses.asdict(session.metadata),
            "messages": [_message_to_dict(m) for m in session.messages],
            "context_messages": (
                [_message_to_dict(m) for m in session.context_messages]
                if session.context_messages is not None
                else None
            ),
        }
        target = self._dir / f"{session.metadata.session_id}.json"
        tmp = target.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        # Atomic replace
        tmp.replace(target)

    def load(self, session_id: str) -> SessionData:
        """Load a session by ID. Raises FileNotFoundError or ValueError."""
        path = self._dir / f"{session_id}.json"
        raw = json.loads(path.read_text(encoding="utf-8"))
        return self._parse(raw)

    def load_latest(self) -> SessionData | None:
        """Load the most recently updated session, or None."""
        sessions = self.list_sessions()
        if not sessions:
            return None
        latest = sessions[0]  # already sorted by updated_at desc
        return self.load(latest.session_id)

    def load_latest_prefer_non_empty(self) -> SessionData | None:
        """Load the latest session with messages, falling back to the latest session."""
        sessions = self.list_sessions()
        if not sessions:
            return None

        fallback: SessionData | None = None
        for meta in sessions:
            session = self.load(meta.session_id)
            if fallback is None:
                fallback = session
            if session.messages:
                return session
        return fallback

    def list_sessions(self) -> list[SessionMetadata]:
        """Return all session metadata sorted by updated_at descending."""
        result: list[SessionMetadata] = []
        for f in self._dir.glob("*.json"):
            try:
                raw = json.loads(f.read_text(encoding="utf-8"))
                meta = self._parse_metadata(raw)
                result.append(meta)
            except (json.JSONDecodeError, KeyError, ValueError):
                continue
        result.sort(key=lambda m: m.updated_at, reverse=True)
        return result

    def delete(self, session_id: str) -> bool:
        """Delete a session file. Returns True if deleted."""
        path = self._dir / f"{session_id}.json"
        try:
            path.unlink()
            return True
        except FileNotFoundError:
            return False

    # -- internal -----------------------------------------------------------

    @staticmethod
    def _parse_metadata(raw: dict[str, Any]) -> SessionMetadata:
        meta = raw["metadata"]
        return SessionMetadata(
            session_id=meta["session_id"],
            title=meta.get("title", ""),
            created_at=meta["created_at"],
            updated_at=meta["updated_at"],
            model_name=meta.get("model_name", ""),
            provider=meta.get("provider", ""),
            working_directory=meta.get("working_directory", ""),
            total_input_tokens=meta.get("total_input_tokens", 0),
            total_output_tokens=meta.get("total_output_tokens", 0),
            turn_count=meta.get("turn_count", 0),
        )

    @staticmethod
    def _parse(raw: dict[str, Any]) -> SessionData:
        meta = SessionManager._parse_metadata(raw)
        messages = [_message_from_dict(m) for m in raw.get("messages", [])]
        raw_context_messages = raw.get("context_messages")
        context_messages = (
            [_message_from_dict(m) for m in raw_context_messages]
            if raw_context_messages is not None
            else None
        )
        return SessionData(
            metadata=meta,
            messages=messages,
            context_messages=context_messages,
        )
