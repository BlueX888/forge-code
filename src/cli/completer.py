"""prompt_toolkit completer for slash commands."""

from __future__ import annotations

from prompt_toolkit.completion import Completer, Completion
from prompt_toolkit.document import Document

from pathlib import Path
from cli.commands import SLASH_COMMANDS


class SlashCommandCompleter(Completer):
    """Offer completions only when the input starts with '/'."""

    def __init__(self, working_directory: Path | None = None) -> None:
        self.working_directory = working_directory

    def get_completions(self, document: Document, complete_event):  # type: ignore[override]
        text = document.text_before_cursor
        if not text.startswith("/"):
            return
        
        # 1. Static commands
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

        # 2. Skill commands
        if self.working_directory is not None:
            from skills.skills import discover_skills
            try:
                skills = discover_skills(self.working_directory)
                for skill_name, skill in sorted(skills.items()):
                    if not skill.user_invocable:
                        continue
                    full_cmd = f"/{skill_name}"
                    if full_cmd.startswith(text):
                        yield Completion(
                            full_cmd,
                            start_position=-len(text),
                            display=full_cmd,
                            display_meta=skill.description or "Custom Skill",
                        )
            except Exception:
                pass

