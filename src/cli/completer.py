"""prompt_toolkit completer for slash commands."""

from __future__ import annotations

from prompt_toolkit.completion import Completer, Completion
from prompt_toolkit.document import Document

from cli.commands import SLASH_COMMANDS


class SlashCommandCompleter(Completer):
    """Offer completions only when the input starts with '/'."""

    def get_completions(self, document: Document, complete_event):  # type: ignore[override]
        text = document.text_before_cursor
        if not text.startswith("/"):
            return
        for cmd in SLASH_COMMANDS:
            if cmd.hidden:
                continue
            if cmd.name.startswith(text):
                display = f"{cmd.name} {cmd.argument}".strip()
                yield Completion(
                    cmd.name,
                    start_position=-len(text),
                    display=display,
                    display_meta=cmd.description,
                )
