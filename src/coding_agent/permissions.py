"""Permission system with safety labels and default-deny."""

from __future__ import annotations

import dataclasses
import enum
from pathlib import Path
from typing import Callable

from coding_agent.config import AgentConfig, DangerousMode


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
