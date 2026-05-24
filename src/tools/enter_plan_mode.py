"""EnterPlanMode tool — transition into a read-only planning phase."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Callable

from main.config import AgentConfig
from main.plan_mode import create_plan_state, PlanModeState
from safety.permissions import SafetyLabel
from tools.base import ToolResult


class EnterPlanModeTool:
    """Enter Plan Mode — a mandatory read-only planning phase.

    Use this tool when a task is complex enough to warrant exploration,
    design, and user review before any code changes are made.
    """

    @property
    def name(self) -> str:
        return "EnterPlanMode"

    @property
    def description(self) -> str:
        return (
            "Enter plan mode — a read-only planning phase. Use this BEFORE making code "
            "changes for complex tasks. In plan mode you can read/search/explore freely "
            "and write only to the plan file. Call ExitPlanMode when your plan is ready "
            "for user review. Simple fixes (typos, single-line bugs) do NOT need this."
        )

    @property
    def parameters_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "description": {
                    "type": "string",
                    "description": "A short (3-5 word) description of the task to plan",
                },
            },
            "required": ["description"],
        }

    @property
    def safety_label(self) -> SafetyLabel:
        return SafetyLabel.READONLY

    def run(self, *, arguments: dict[str, Any], config: AgentConfig) -> ToolResult:
        description = arguments.get("description", "").strip()

        # Idempotent: already in plan mode
        if getattr(config, "plan_mode", False):
            plan_file = getattr(config, "plan_file", None)
            return ToolResult(
                True,
                f"Already in plan mode. Plan file: {plan_file}\n"
                f"Continue planning or call ExitPlanMode when ready.",
            )

        session_id = getattr(config, "session_id", "no-session")

        # Create plan state
        state = create_plan_state(config, session_id, description)

        # Enter plan mode on the dynamic config
        if hasattr(config, "enter_plan_mode"):
            config.enter_plan_mode(state.plan_file)

        # Store the state on the config for the runtime to access
        if hasattr(config, "_plan_state_raw"):
            config._plan_state_raw = state

        # Initialize the plan file with a structured template
        try:
            state.plan_file.parent.mkdir(parents=True, exist_ok=True)
            state.plan_file.write_text(
                f"# Plan: {description}\n\n"
                f"> Task: {state.task_description}\n\n"
                f"## Context\n\n<!-- What problem does this solve? -->\n\n"
                f"## Design\n\n<!-- Chosen approach and why -->\n\n"
                f"## Tasks\n\n"
                f"- [ ] #1 <description> [pending]\n"
                f"- [ ] #2 <description> [pending]\n\n"
                f"<!-- Status: [pending] → [in_progress] → [done]. "
                f"Mark [x] when complete. -->\n\n"
                f"## Files to Modify\n\n<!-- List specific file paths -->\n\n"
                f"## Verification\n\n<!-- How to test the changes -->\n",
                encoding="utf-8",
            )
        except OSError as exc:
            return ToolResult(False, "", f"Failed to create plan file: {exc}")

        return ToolResult(
            True,
            f"Plan mode activated.\n"
            f"Plan file: {state.plan_file}\n"
            f"Previous dangerous mode ({state.previous_dangerous_mode}) saved — will be restored on exit.\n\n"
            f"You are now in a read-only phase. Explore the codebase, design your approach, "
            f"then write your plan to the plan file using Write. "
            f"Call ExitPlanMode when ready for user review.",
        )
