"""CLI input/output abstraction."""

from __future__ import annotations

import os
import shutil
import sys
from typing import Any

from tools.base import ToolResult

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from main.config import AgentConfig
    from main.session import SessionData
    from main.token_tracker import TokenUsageTracker


_TOOL_ICONS: dict[str, str] = {
    "read_file": "\U0001f4d6",
    "edit_file": "\u270f\ufe0f",
    "write_file": "\U0001f4dd",
    "list_directory": "\U0001f4c1",
    "search": "\U0001f50d",
    "run_command": "\u26a1",
}
_DEFAULT_ICON = "\U0001f527"

_PRIMARY_ARG_KEY: dict[str, str] = {
    "read_file": "path",
    "write_file": "path",
    "edit_file": "path",
    "list_directory": "path",
    "search": "query",
    "run_command": "command",
}


def _supports_color(stream) -> bool:
    return hasattr(stream, "isatty") and stream.isatty()


def _detect_color_level(stream) -> int:
    """Detect the terminal color capability level.

    Returns:
        0 — not a TTY (piped / redirected)
        1 — basic 16-color TTY
        2 — 256-color terminal
        3 — true-color (24-bit) terminal
    """
    if not _supports_color(stream):
        return 0
    colorterm = os.environ.get("COLORTERM", "").lower()
    if colorterm in ("truecolor", "24bit"):
        return 3
    # Windows Terminal always supports true-color
    if os.environ.get("WT_SESSION"):
        return 3
    term = os.environ.get("TERM", "")
    if "256color" in term or colorterm:
        return 2
    return 1


class AgentIO:
    """Thin wrapper around stdin/stdout for agent I/O."""

    def __init__(
        self,
        input_stream=None,
        output_stream=None,
    ) -> None:
        self._in = input_stream or sys.stdin
        self._out = output_stream or sys.stdout
        self._color_level = _detect_color_level(self._out)
        self._color = self._color_level > 0
        self._prompt_session = None
        # Enable prompt_toolkit only for interactive tty without custom streams
        if input_stream is None and output_stream is None and _supports_color(self._out):
            try:
                from prompt_toolkit import PromptSession
                from cli.completer import SlashCommandCompleter
                self._prompt_session = PromptSession(
                    completer=SlashCommandCompleter(),
                    complete_while_typing=True,
                )
            except (ImportError, Exception):
                pass  # graceful fallback to input()

    # -- helpers -------------------------------------------------------------

    def _write(self, text: str) -> None:
        try:
            self._out.write(text)
        except UnicodeEncodeError:
            encoding = getattr(self._out, "encoding", None) or "utf-8"
            encoded = text.encode(encoding, errors="replace")
            self._out.write(encoded.decode(encoding))
        self._out.flush()

    def _c(self, code: str, text: str) -> str:
        if not self._color:
            return text
        return f"\033[{code}m{text}\033[0m"

    # -- public API ----------------------------------------------------------

    def prompt_user(self, prompt: str = "> ") -> str | None:
        """Return user input or ``None`` on EOF."""
        try:
            if self._in is not sys.stdin:
                return self._in.readline()
            # Apply green styling to the default prompt
            styled_prompt = prompt
            if prompt == "> " and self._color:
                styled_prompt = self._c("1;32", ">") + " "
            if self._prompt_session is not None:
                if self._color:
                    from prompt_toolkit.formatted_text import ANSI
                    prompt_text = ANSI("\033[1;32m>\033[0m " if prompt == "> " else prompt)
                    line = self._prompt_session.prompt(prompt_text)
                else:
                    line = self._prompt_session.prompt(prompt)
            else:
                line = input(styled_prompt)
            if line.strip().startswith("{{{"):
                return self._read_multiline(line)
            # prompt_toolkit handles paste natively; only drain for input()
            if self._prompt_session is None:
                extra = self._drain_paste_buffer()
                if extra:
                    return line + "\n" + "\n".join(extra)
            return line
        except EOFError:
            return None

    # -- multi-line helpers --------------------------------------------------

    @staticmethod
    def _has_buffered_input() -> bool:
        """Return ``True`` if stdin has characters waiting in the buffer.

        Uses platform-specific APIs to perform a non-blocking check.
        """
        try:
            if sys.platform == "win32":
                import msvcrt
                return bool(msvcrt.kbhit())
            else:
                import select
                return bool(select.select([sys.stdin], [], [], 0)[0])
        except (ImportError, OSError):
            return False

    def drain_input_buffer(self) -> None:
        """Clear any remaining keystrokes from stdin buffer."""
        if not hasattr(self._in, "isatty") or not self._in.isatty():
            return
        try:
            if sys.platform == "win32":
                import msvcrt
                while msvcrt.kbhit():
                    msvcrt.getch()
            else:
                import select
                import os
                while select.select([sys.stdin], [], [], 0)[0]:
                    os.read(sys.stdin.fileno(), 1024)
        except Exception:
            pass

    def _drain_paste_buffer(self) -> list[str]:
        """Consume any remaining lines from a multi-line paste operation.

        When the user pastes multi-line text, ``input()`` returns only the
        first line while the rest sits in the stdin buffer.  This method
        reads those extra lines so they are treated as a single input.
        """
        import time

        if not hasattr(self._in, "isatty") or not self._in.isatty():
            return []

        lines: list[str] = []
        # Brief pause so the OS finishes delivering the pasted text.
        time.sleep(0.03)
        while self._has_buffered_input():
            try:
                lines.append(input())
            except EOFError:
                break
        return lines

    def _read_multiline(self, first_line: str) -> str:
        """Collect lines until ``}}}`` is encountered and return joined content.

        Supports single-line shorthand: ``{{{ text }}}`` returns ``"text"``.
        """
        # Strip the opening delimiter
        body = first_line.strip().removeprefix("{{{")

        # Single-line shorthand: {{{ text }}}
        if body.rstrip().endswith("}}}"):
            return body.rstrip().removesuffix("}}}").strip()

        lines: list[str] = []
        if body.strip():
            lines.append(body.strip())

        while True:
            try:
                line = input("... ")
            except EOFError:
                break
            if line.rstrip().endswith("}}}"):
                before = line.rstrip().removesuffix("}}}").rstrip()
                if before:
                    lines.append(before)
                break
            lines.append(line)

        return "\n".join(lines)

    def print_banner(
        self,
        config: AgentConfig,
        session: SessionData | None,
        token_tracker: TokenUsageTracker,
    ) -> None:
        """Print the CRT retro startup banner."""
        from main import __version__
        from cli.banner import format_banner

        terminal_width = shutil.get_terminal_size((80, 24)).columns
        lines = format_banner(
            version=__version__,
            model_name=config.model_name,
            provider=config.provider,
            max_context_tokens=config.max_context_tokens,
            working_directory=str(config.working_directory),
            dangerous_mode=config.allow_dangerous_operations.value,
            session_id=(
                session.metadata.session_id if session is not None else None
            ),
            resumed_tokens=token_tracker.session_total,
            resumed_turns=token_tracker.turn_count,
            color_level=self._color_level,
            colorize=self._c,
            terminal_width=terminal_width,
            banner_width=config.banner_width,
        )
        for line in lines:
            self._write(line + "\n")

    def print_assistant(self, message: str) -> None:
        self._write(self._c("32", message) + "\n")

    def print_stream(self, token: str) -> None:
        """Write a streaming token in green without trailing newline."""
        self._write(self._c("32", token))

    def print_stream_end(self) -> None:
        """Write the final newline after a streaming response completes."""
        self._write("\n")

    def print_thinking_start(self) -> None:
        """Print a dim italic header to indicate thinking has begun."""
        self._write(self._c("2;3", "Thinking...") + "\n")

    def print_thinking_stream(self, token: str) -> None:
        """Write a streaming thinking token in dim italic without trailing newline."""
        self._write(self._c("2;3", token))

    def print_thinking_end(self) -> None:
        """Write a separator after thinking content finishes."""
        self._write("\n" + self._c("2", "---") + "\n")

    def print_thinking(self, content: str) -> None:
        """Print a complete thinking block (non-streaming path)."""
        self.print_thinking_start()
        self._write(self._c("2;3", content))
        self.print_thinking_end()

    def print_tool_call(self, tool_name: str, arguments: dict[str, Any]) -> None:
        icon = _TOOL_ICONS.get(tool_name, _DEFAULT_ICON)
        primary_key = _PRIMARY_ARG_KEY.get(tool_name)
        primary_val = arguments.get(primary_key, "") if primary_key else ""
        header = f"{icon} {tool_name}"
        if primary_val:
            header += f" {primary_val}"
        self._write(self._c("33", header) + "\n")

    def print_tool_result(
        self, tool_name: str, result: ToolResult, arguments: dict[str, Any] | None = None
    ) -> None:
        if not result.success:
            self._write(self._c("31", f"[Tool Error] {result.error}") + "\n")
            return

        formatter = getattr(self, f"_fmt_{tool_name}", None)
        if formatter:
            summary = formatter(result, arguments)
        else:
            summary = self._fmt_default(result)
        self._write(self._c("2", f"  {summary}") + "\n")

    # -- compact result formatters --------------------------------------------

    @staticmethod
    def _fmt_read_file(result: ToolResult, arguments: dict[str, Any] | None) -> str:
        output = result.output
        first_line = output.split("\n", 1)[0]
        if len(first_line) > 80:
            first_line = first_line[:77] + "..."
        return f"{first_line}  ... ({len(output)} chars total)"

    @staticmethod
    def _fmt_edit_file(result: ToolResult, arguments: dict[str, Any] | None) -> str:
        if not arguments:
            return result.output[:120]
        lines: list[str] = []
        old = arguments.get("old_text", "")
        new = arguments.get("new_text", "")
        for raw in old.split("\n")[:3]:
            lines.append(f"- {raw}")
        if old.count("\n") > 2:
            lines.append(f"  ... ({old.count(chr(10)) + 1} lines)")
        for raw in new.split("\n")[:3]:
            lines.append(f"+ {raw}")
        if new.count("\n") > 2:
            lines.append(f"  ... ({new.count(chr(10)) + 1} lines)")
        return "\n  ".join(lines)

    @staticmethod
    def _fmt_write_file(result: ToolResult, arguments: dict[str, Any] | None) -> str:
        path = (arguments or {}).get("path", "?")
        return f"Wrote {len(result.output)} bytes to {path}"

    @staticmethod
    def _fmt_list_directory(result: ToolResult, arguments: dict[str, Any] | None) -> str:
        entries = [e for e in result.output.split("\n") if e.strip()]
        preview = "  ".join(entries[:3])
        if len(entries) > 3:
            preview += f"  ... ({len(entries)} entries total)"
        return preview

    @staticmethod
    def _fmt_search(result: ToolResult, arguments: dict[str, Any] | None) -> str:
        matches = [m for m in result.output.split("\n") if m.strip()]
        preview = "  ".join(matches[:3])
        if len(matches) > 3:
            preview += f"  ... ({len(matches)} matches)"
        return preview

    @staticmethod
    def _fmt_run_command(result: ToolResult, arguments: dict[str, Any] | None) -> str:
        output = result.output
        first_line = output.split("\n", 1)[0]
        if len(first_line) > 80:
            first_line = first_line[:77] + "..."
        return f"{first_line}  ... ({len(output)} chars total)"

    @staticmethod
    def _fmt_default(result: ToolResult, arguments: dict[str, Any] | None = None) -> str:
        output = result.output
        if len(output) > 120:
            return output[:117] + "..."
        return output

    def print_error(self, message: str) -> None:
        self._write(self._c("31", f"Error: {message}") + "\n")

    def print_system(self, message: str) -> None:
        self._write(self._c("36", message) + "\n")

    def print_token_usage(
        self,
        turn_input: int,
        turn_output: int,
        session_total: int,
        context_used: int,
        max_context: int,
    ) -> None:
        """Display a one-line token usage summary after each API call."""
        turn_total = turn_input + turn_output
        ratio = context_used / max_context * 100 if max_context > 0 else 0

        # Color-code the context ratio
        if ratio > 85:
            ratio_color = "31"  # red
        elif ratio > 50:
            ratio_color = "33"  # yellow
        else:
            ratio_color = "32"  # green
        ratio_str = self._c(ratio_color, f"{ratio:.1f}%")

        line = (
            f"[Tokens] \u2191{turn_input:,} \u2193{turn_output:,} | "
            f"Turn: {turn_total:,} | "
            f"Session: {session_total:,} | "
            f"Context: {context_used:,}/{max_context:,} ({ratio_str})"
        )
        self._write(self._c("2;36", line) + "\n")

    def print_usage_detail(self, tracker: TokenUsageTracker) -> None:
        """Render the detailed /usage panel."""
        t = tracker
        ratio = t.context_usage_ratio * 100
        bar_width = 20
        filled = int(bar_width * t.context_usage_ratio)
        bar = "\u2588" * filled + "\u2591" * (bar_width - filled)

        if ratio > 85:
            bar_color = "31"
        elif ratio > 50:
            bar_color = "33"
        else:
            bar_color = "32"

        lines = [
            "\u256d\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500 Token Usage \u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u256e",
            "\u2502                                                   \u2502",
            "\u2502  Session Statistics:                              \u2502",
            f"\u2502    Total turns:     {t.turn_count:>8,}                       \u2502",
            f"\u2502    Input tokens:    {t.session_input:>8,}                       \u2502",
            f"\u2502    Output tokens:   {t.session_output:>8,}                       \u2502",
            f"\u2502    Total tokens:    {t.session_total:>8,}                       \u2502",
            "\u2502                                                   \u2502",
            "\u2502  Context Window:                                  \u2502",
            f"\u2502    Used:    {t.context_used:>8,} / {t.max_context:,}                \u2502",
            f"\u2502    Free:    {t.context_remaining:>8,}                             \u2502",
            f"\u2502    Usage:   {self._c(bar_color, bar)}  {ratio:.1f}%           \u2502",
            "\u2502                                                   \u2502",
            "\u2570\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u256f",
        ]
        self._write(self._c("36", "\n".join(lines)) + "\n")

    def confirm(self, message: str) -> bool:
        """Display a yes/no confirmation prompt. Returns True for y/yes, False otherwise."""
        full_prompt = (
            self._c("35", "\n[Confirm] ")
            + message
            + " "
            + self._c("35", "[y/N] ")
        )
        response = self.prompt_user(full_prompt)
        if response is None:
            return False
        return response.strip().lower() in ("y", "yes")
