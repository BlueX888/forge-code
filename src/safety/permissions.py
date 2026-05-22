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
    """Block or require confirmation for destructive ops based on DangerousMode."""
    if req.safety_label == SafetyLabel.DESTRUCTIVE:
        if cfg.allow_dangerous_operations == DangerousMode.DENY:
            return PermissionResult(False, "Destructive operations are disabled")
        if cfg.allow_dangerous_operations == DangerousMode.ASK:
            return PermissionResult(
                True,
                "Destructive operation requires confirmation",
                requires_confirmation=True,
            )
    return None


def _rule_plan_mode_readonly(req: PermissionRequest, cfg: AgentConfig) -> PermissionResult | None:
    """In plan mode, deny all destructive operations except plan file writes."""
    if not getattr(cfg, "plan_mode", False):
        return None  # Not in plan mode, pass through

    if req.safety_label != SafetyLabel.DESTRUCTIVE:
        return None  # Read-only tools pass through

    # Allow writes ONLY to the active plan file
    plan_file: Path | None = getattr(cfg, "plan_file", None)
    if req.path is not None and plan_file is not None:
        try:
            if req.path.resolve() == plan_file.resolve():
                return PermissionResult(True, "Plan file write allowed in plan mode")
        except OSError:
            pass

    return PermissionResult(
        False,
        "Plan mode is active: only read operations and plan file writes are allowed. "
        "Call ExitPlanMode to submit your plan for approval.",
    )


def _rule_default_allow(req: PermissionRequest, cfg: AgentConfig) -> PermissionResult | None:
    """Terminal catch-all: allow everything that reaches this point."""
    return PermissionResult(True, "Allowed")


# ---------------------------------------------------------------------------
# Checker
# ---------------------------------------------------------------------------

_EARLY_RULES: list[Rule] = [
    _rule_path_sandbox,
    _rule_plan_mode_readonly,  # must precede _rule_deny_destructive
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
    """Wraps an immutable AgentConfig with mutable plan-mode and path-approval state."""

    def __init__(self, config: AgentConfig, *, session_id: str = "no-session") -> None:
        self._config = config
        self._approved_directories: set[Path] = set()
        # Plan mode mutable state
        self._plan_mode: bool = False
        self._plan_file: Path | None = None
        self._plan_choice: str | None = None
        # Stored for tools to access
        self._plan_state_raw: Any = None  # PlanModeState | None
        self._approval_callback: Any = None  # Callable | None
        self._session_id: str = session_id

    def __getattr__(self, name: str):
        # Expose mutable plan-mode attributes
        if name == "plan_mode":
            return self._plan_mode
        if name == "plan_file":
            return self._plan_file
        if name == "session_id":
            return self._session_id
        if name == "_plan_state_raw":
            return self._plan_state_raw
        if name == "_approval_callback":
            return self._approval_callback
        if name == "_plan_choice":
            return self._plan_choice
        return getattr(self._config, name)

    # -- plan mode ----------------------------------------------------------

    def enter_plan_mode(self, plan_file: Path) -> None:
        self._plan_mode = True
        self._plan_file = plan_file
        self._plan_choice = None
        # Auto-approve the plan directory so write_file won't prompt for path access
        self.approve_directory(plan_file.parent)

    def exit_plan_mode(self) -> None:
        self._plan_mode = False
        self._plan_file = None
        self._plan_state_raw = None
        self._plan_choice = None

    # -- path approval ------------------------------------------------------

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

