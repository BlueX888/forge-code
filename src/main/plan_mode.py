"""Plan Mode state manager — read-only planning phase enforced by permissions."""

from __future__ import annotations

import hashlib
import sys
from dataclasses import dataclass, field
from pathlib import Path

from main.config import AgentConfig


@dataclass
class PlanModeState:
    """Mutable state for the plan-mode lifecycle.

    Tracks the plan directory/file and remembers the pre-plan dangerous-mode
    setting so it can be **symmetrically restored** on exit.
    """

    plan_dir: Path
    plan_file: Path
    previous_dangerous_mode: str  # "deny" | "ask" | "allow"
    is_active: bool = False
    plan_approved: bool = False
    task_description: str = ""
    approval_choice: str | None = None  # set by the approval callback


def compute_plan_dir(working_directory: Path) -> Path:
    """Compute project-isolated plan directory: ``~/.forgecode/plans/{hash}/``."""
    path_str = str(working_directory.resolve())
    if sys.platform == "win32":
        path_str = path_str.casefold()
    project_hash = hashlib.sha256(path_str.encode()).hexdigest()[:16]
    return Path.home() / ".forgecode" / "plans" / project_hash


def create_plan_state(
    config: AgentConfig,
    session_id: str,
    task_description: str,
) -> PlanModeState:
    """Create a new PlanModeState, ensuring the plan directory exists.

    Plan files are named ``plan_{session_id}_{N}.md`` where *N* is an
    auto-incremented counter so that multiple plans per session never
    overwrite each other.
    """
    plan_dir = compute_plan_dir(config.working_directory)
    plan_dir.mkdir(parents=True, exist_ok=True)

    # Auto-increment counter: scan for plan_{session_id}_*.md
    counter = 1
    for existing in plan_dir.glob(f"plan_{session_id}_*.md"):
        try:
            stem = existing.stem  # plan_{session_id}_{N}
            num = int(stem.rsplit("_", 1)[-1])
            if num >= counter:
                counter = num + 1
        except (ValueError, IndexError):
            continue

    plan_file = plan_dir / f"plan_{session_id}_{counter}.md"

    return PlanModeState(
        plan_dir=plan_dir,
        plan_file=plan_file,
        previous_dangerous_mode=config.allow_dangerous_operations.value,
        is_active=True,
        task_description=task_description,
    )
