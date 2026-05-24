"""Slash-command registry — single source of truth for all CLI commands."""

from __future__ import annotations

import dataclasses


@dataclasses.dataclass(frozen=True)
class SlashCommand:
    name: str            # "/help"
    description: str     # "Show this help"
    argument: str = ""   # "<path>", "[path]", or ""
    hidden: bool = False # hidden from command list (e.g. /exit alias)


SLASH_COMMANDS: tuple[SlashCommand, ...] = (
    SlashCommand("/read",   "Read a file",                argument="<path>"),
    SlashCommand("/ls",     "List directory",             argument="[path]"),
    SlashCommand("/pwd",    "Current directory"),
    SlashCommand("/tools",  "List available tools"),
    SlashCommand("/mcp",    "List MCP servers and tools"),
    SlashCommand("/usage",  "Show token usage statistics"),
    SlashCommand("/memory", "List stored memories"),
    SlashCommand("/history", "Show saved conversation history", argument="[count|all]"),
    SlashCommand("/clear",  "Clear conversation context (keeps session log & memory)"),
    SlashCommand("/plan",   "Enter plan mode for complex tasks", argument="<task description>"),
    SlashCommand("/skills", "List available skills"),
    SlashCommand("/help",   "Show this help"),

    SlashCommand("/quit",   "Exit"),
    SlashCommand("/exit",   "Exit",                       hidden=True),
)


def get_command_names() -> list[str]:
    """Return visible command names."""
    return [cmd.name for cmd in SLASH_COMMANDS if not cmd.hidden]


def format_command_list() -> str:
    """Format a compact command list for the '/' shortcut."""
    lines: list[str] = []
    for cmd in SLASH_COMMANDS:
        if cmd.hidden:
            continue
        label = f"{cmd.name} {cmd.argument}".strip()
        lines.append(f"  {label:16s} — {cmd.description}")
    return "Commands:\n" + "\n".join(lines)


def format_help_text(session_help: str = "") -> str:
    """Format the full /help output."""
    lines: list[str] = []
    for cmd in SLASH_COMMANDS:
        if cmd.hidden:
            continue
        label = f"{cmd.name} {cmd.argument}".strip()
        lines.append(f"  {label:16s} — {cmd.description}")
    lines.append("  {{{ ... }}}      — Multi-line input with delimiters")
    return "Commands:\n" + "\n".join(lines) + session_help
