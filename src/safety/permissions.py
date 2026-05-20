"""Permission system with safety labels and default-deny."""

from __future__ import annotations

import dataclasses
import enum
from pathlib import Path
from typing import Callable

from main.config import AgentConfig, DangerousMode


class SafetyLabel(enum.Enum):
    """Semantic safety classification for tools."""
    READONLY = "readonly"
    DESTRUCTIVE = "destructive"
    CONCURRENT_SAFE = "concurrent_safe"


@dataclasses.dataclass(frozen=True)
class PermissionRequest:
    safety_label: SafetyLabel
    path: Path | None = None
    command: str | None = None


@dataclasses.dataclass(frozen=True)
class PermissionResult:
    allowed: bool
    reason: str
    requires_confirmation: bool = False


# ---------------------------------------------------------------------------
# Rule type
# ---------------------------------------------------------------------------

Rule = Callable[[PermissionRequest, AgentConfig], PermissionResult | None]


# ---------------------------------------------------------------------------
# Built-in rules
# ---------------------------------------------------------------------------

def _rule_path_sandbox(req: PermissionRequest, cfg: AgentConfig) -> PermissionResult | None:
    """Deny if path is outside the sandbox."""
    if req.path is not None and not cfg.is_path_allowed(req.path):
        return PermissionResult(False, f"Path {req.path} is outside allowed directories")
    return None


def _rule_deny_destructive(req: PermissionRequest, cfg: AgentConfig) -> PermissionResult | None:
    """Block destructive ops when DangerousMode is DENY; abstain otherwise."""
    if req.safety_label == SafetyLabel.DESTRUCTIVE:
        if cfg.allow_dangerous_operations == DangerousMode.DENY:
            return PermissionResult(False, "Destructive operations are disabled")
    return None


def _rule_default_allow(req: PermissionRequest, cfg: AgentConfig) -> PermissionResult | None:
    """Terminal catch-all: allow everything that reaches this point."""
    return PermissionResult(True, "Allowed")


# ---------------------------------------------------------------------------
# Checker
# ---------------------------------------------------------------------------

_EARLY_RULES: list[Rule] = [
    _rule_path_sandbox,
    _rule_deny_destructive,
]


class PermissionChecker:
    """Rule-chain permission checker with default-deny."""

    def __init__(self, config: AgentConfig, extra_rules: list[Rule] | None = None) -> None:
        self._config = config
        self._rules: list[Rule] = list(_EARLY_RULES)
        if extra_rules:
            self._rules.extend(extra_rules)
        self._rules.append(_rule_default_allow)

    def check(self, request: PermissionRequest) -> PermissionResult:
        for rule in self._rules:
            result = rule(request, self._config)
            if result is not None:
                return result
        return PermissionResult(False, "Denied by default policy")


class DynamicPathConfig:
    """Wraps an immutable AgentConfig with a mutable set of session-approved directories."""

    def __init__(self, config: AgentConfig) -> None:
        self._config = config
        self._approved_directories: set[Path] = set()

    def __getattr__(self, name: str):
        return getattr(self._config, name)

    def approve_directory(self, directory: Path) -> None:
        self._approved_directories.add(directory.resolve())

    def needs_path_approval(self, path: Path) -> bool:
        """True if path is outside both the static sandbox and dynamic approvals."""
        resolved = Path(path).resolve()
        if self._config.is_path_allowed(resolved):
            return False
        return not any(
            resolved == d or resolved.is_relative_to(d)
            for d in self._approved_directories
        )

    def is_path_allowed(self, path: Path) -> bool:
        """Override: check static sandbox + dynamic approvals."""
        if self._config.is_path_allowed(path):
            return True
        resolved = Path(path).resolve()
        return any(
            resolved == d or resolved.is_relative_to(d)
            for d in self._approved_directories
        )

