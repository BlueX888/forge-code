"""Command classification policy for run_command safety."""

from __future__ import annotations

import enum
import re
import shlex
from pathlib import PurePosixPath, PureWindowsPath
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from coding_agent.config import AgentConfig
    from coding_agent.permissions import PermissionRequest, PermissionResult, Rule


class CommandRiskLevel(enum.Enum):
    """Risk classification for shell commands."""
    SAFE = "safe"
    NEEDS_APPROVAL = "needs_approval"


# ---------------------------------------------------------------------------
# Default safe-command definitions
# ---------------------------------------------------------------------------

_SAFE_COMMANDS: frozenset[str] = frozenset({
    # File inspection
    "ls", "dir", "cat", "type", "head", "tail", "less", "more",
    "wc", "file", "stat", "du", "df", "tree",
    # Checksums
    "md5sum", "sha256sum", "shasum", "cksum",
    # Search
    "grep", "egrep", "fgrep", "rg", "ag", "ack",
    "find", "fd", "locate", "which", "where", "whereis",
    # Text processing (read-only)
    "sort", "uniq", "cut", "tr", "diff", "cmp", "comm", "awk", "jq", "yq",
    # System info
    "pwd", "echo", "printf", "date", "cal", "whoami", "hostname",
    "uname", "arch", "id", "basename", "dirname", "realpath", "readlink",
    # Process info
    "ps", "top", "htop", "pgrep",
    # Help
    "man", "help", "info",
    # Linting / type checking (read-only)
    "mypy", "ruff", "eslint", "flake8", "pylint", "tsc",
})

_SAFE_SUBCOMMANDS: dict[str, frozenset[str]] = {
    "git": frozenset({
        "status", "log", "diff", "show", "branch", "remote", "tag",
        "blame", "stash list", "rev-parse", "ls-files", "describe",
        "shortlog", "version", "help",
    }),
    "docker": frozenset({
        "ps", "images", "inspect", "logs", "info", "version",
    }),
    "npm": frozenset({
        "list", "ls", "outdated", "audit", "view", "info", "version",
    }),
    "pip": frozenset({
        "list", "show", "freeze", "check", "--version",
    }),
    "pip3": frozenset({
        "list", "show", "freeze", "check", "--version",
    }),
    "cargo": frozenset({
        "--version", "version",
    }),
    "go": frozenset({
        "version", "env",
    }),
    "python": frozenset({"--version", "-V"}),
    "python3": frozenset({"--version", "-V"}),
    "node": frozenset({"--version", "-v"}),
}

# Regex to split compound commands on ; && || |
# Negative lookbehind avoids splitting on || inside quoted strings (best-effort).
_COMPOUND_SPLIT = re.compile(r"\s*(?:;|\&\&|\|\||\|)\s*")


# ---------------------------------------------------------------------------
# CommandPolicy
# ---------------------------------------------------------------------------

class CommandPolicy:
    """Classifies shell commands into risk levels."""

    def __init__(
        self,
        extra_safe_commands: frozenset[str] | None = None,
        extra_safe_subcommands: dict[str, frozenset[str]] | None = None,
    ) -> None:
        self._safe_commands = _SAFE_COMMANDS | (extra_safe_commands or frozenset())
        self._safe_subcommands: dict[str, frozenset[str]] = dict(_SAFE_SUBCOMMANDS)
        if extra_safe_subcommands:
            for cmd, subs in extra_safe_subcommands.items():
                existing = self._safe_subcommands.get(cmd, frozenset())
                self._safe_subcommands[cmd] = existing | subs

    def classify(self, command: str) -> CommandRiskLevel:
        """Classify a (possibly compound) shell command."""
        command = command.strip()
        if not command:
            return CommandRiskLevel.SAFE

        parts = _COMPOUND_SPLIT.split(command)
        for part in parts:
            part = part.strip()
            if part and self._classify_simple(part) == CommandRiskLevel.NEEDS_APPROVAL:
                return CommandRiskLevel.NEEDS_APPROVAL
        return CommandRiskLevel.SAFE

    def _classify_simple(self, command: str) -> CommandRiskLevel:
        """Classify a single (non-compound) command."""
        try:
            tokens = shlex.split(command)
        except ValueError:
            tokens = command.split()

        if not tokens:
            return CommandRiskLevel.SAFE

        base = self._extract_base_command(tokens[0])

        # Direct match against safe commands
        if base in self._safe_commands:
            return CommandRiskLevel.SAFE

        # Check safe subcommands
        if base in self._safe_subcommands and len(tokens) >= 2:
            sub = tokens[1]
            if sub in self._safe_subcommands[base]:
                return CommandRiskLevel.SAFE
            # Multi-word subcommand (e.g., "stash list")
            if len(tokens) >= 3:
                multi_sub = f"{tokens[1]} {tokens[2]}"
                if multi_sub in self._safe_subcommands[base]:
                    return CommandRiskLevel.SAFE

        return CommandRiskLevel.NEEDS_APPROVAL

    @staticmethod
    def _extract_base_command(token: str) -> str:
        """Extract the base command name, stripping path prefixes."""
        # Try POSIX path first, then Windows
        for path_cls in (PurePosixPath, PureWindowsPath):
            try:
                name = path_cls(token).name
                if name:
                    return name
            except (ValueError, TypeError):
                continue
        return token


# ---------------------------------------------------------------------------
# Rule factory
# ---------------------------------------------------------------------------

def make_command_rule(policy: CommandPolicy) -> "Rule":
    """Create a permission rule that classifies commands via *policy*."""
    from coding_agent.config import DangerousMode
    from coding_agent.permissions import PermissionResult

    def _command_rule(req: "PermissionRequest", cfg: "AgentConfig") -> "PermissionResult | None":
        if req.command is None:
            return None  # Not a command invocation — let other rules decide.

        level = policy.classify(req.command)

        if level == CommandRiskLevel.SAFE:
            return PermissionResult(True, "Safe command")

        # NEEDS_APPROVAL: respect DangerousMode
        if cfg.allow_dangerous_operations == DangerousMode.DENY:
            return PermissionResult(
                False,
                f"Command requires approval but dangerous mode is DENY: {req.command}",
            )
        if cfg.allow_dangerous_operations == DangerousMode.ASK:
            return PermissionResult(
                True,
                "Command requires user approval",
                requires_confirmation=True,
            )
        # ALLOW
        return PermissionResult(True, "Allowed")

    return _command_rule
