"""ExitPlanMode tool — submit a plan for user approval and transition out of plan mode."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Callable

from main.config import AgentConfig
from safety.permissions import SafetyLabel, DangerousMode
from tools.base import ToolResult


class ExitPlanModeTool:
    """Submit your plan for user approval and exit plan mode.

    Do NOT ask the user "is this plan okay?" — this tool handles the approval
    flow. The user will be presented with options: clear context & execute,
    execute, manual approval, or continue planning.
    """

    @property
    def name(self) -> str:
        return "ExitPlanMode"

    @property
    def description(self) -> str:
        return (
            "Submit your plan for user approval and exit plan mode. "
            "The user will review your plan and choose how to proceed. "
            "Returns the user's decision. IMPORTANT: call this instead of "
            "asking the user if the plan is acceptable."
        )

    @property
    def parameters_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "summary": {
                    "type": "string",
                    "description": "A brief (1-3 sentence) summary of the plan for the user to review",
                },
            },
            "required": ["summary"],
        }

    @property
    def safety_label(self) -> SafetyLabel:
        return SafetyLabel.READONLY

    def run(self, *, arguments: dict[str, Any], config: AgentConfig) -> ToolResult:
        summary = arguments.get("summary", "").strip()

        # Idempotent: not in plan mode
        if not getattr(config, "plan_mode", False):
            return ToolResult(
                True,
                "Not currently in plan mode. No action needed.",
            )

        # Get the approval callback (injected by CLI layer; None for child agents)
        callback: Callable | None = getattr(config, "_approval_callback", None)

        if callback is None:
            # Child agent / no callback: just exit plan mode silently
            if hasattr(config, "exit_plan_mode"):
                config.exit_plan_mode()
            return ToolResult(
                True,
                "Exited plan mode (no approval callback available — child agent context).",
            )

        # Invoke the callback — this blocks until the user chooses
        choice = callback(summary)
        plan_state = getattr(config, "_plan_state_raw", None)

        if choice == "continue":
            # Stay in plan mode, return feedback to the model
            return ToolResult(
                True,
                "User chose to continue planning. Provide feedback or ask the user "
                "what to revise, then update the plan file and call ExitPlanMode again.",
            )

        # Exit plan mode — choice is "clear_execute", "execute", or "manual"
        previous_mode = plan_state.previous_dangerous_mode if plan_state else "ask"

        if hasattr(config, "exit_plan_mode"):
            config.exit_plan_mode()

        # Store the choice so the runtime can act on it after the tool returns
        if hasattr(config, "_plan_choice"):
            config._plan_choice = choice  # type: ignore[attr-defined]

        return ToolResult(
            True,
            f"Plan approved by user (choice: {choice}). "
            f"Exiting plan mode. Previous dangerous mode ({previous_mode}) restored.\n\n"
            f"Now begin implementation. Read the plan file, work through tasks in order:\n"
            f"- Before starting a task: Edit to set [in_progress]\n"
            f"- After completing a task: Edit to set [x] and [done]\n"
            f"Start with task #1.",
        )
