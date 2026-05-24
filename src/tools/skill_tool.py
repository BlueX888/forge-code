from __future__ import annotations

from typing import Any, Callable
from main.config import AgentConfig
from safety.permissions import SafetyLabel
from tools.base import ToolResult
from tools.names import ToolName
from skills.skills import discover_skills, resolve_prompt

class SkillTool:
    def __init__(self) -> None:
        self._fork_handler: Callable[[str, str], ToolResult] | None = None

    @property
    def name(self) -> str:
        return ToolName.SKILL

    @property
    def description(self) -> str:
        return "Run a predefined workflow/skill with the specified arguments."

    @property
    def parameters_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "skill": {"type": "string", "description": "The name of the skill to run"},
                "args": {"type": "string", "description": "Arguments to pass to the skill", "default": ""},
            },
            "required": ["skill"],
        }

    @property
    def safety_label(self) -> SafetyLabel:
        return SafetyLabel.READONLY

    def set_fork_handler(self, handler: Callable[[str, str], ToolResult]) -> None:
        self._fork_handler = handler

    def run(self, *, arguments: dict[str, Any], config: AgentConfig) -> ToolResult:
        skill_name = arguments.get("skill", "").strip()
        args_str = arguments.get("args", "").strip()

        if not skill_name:
            return ToolResult(False, "", "Skill name is required.")

        skills = discover_skills(config.working_directory)
        if skill_name not in skills:
            return ToolResult(False, "", f"Skill '{skill_name}' not found.")

        skill = skills[skill_name]

        if skill.context == "fork":
            if self._fork_handler is None:
                return ToolResult(False, "", "Fork handler not registered for Skill tool.")
            # Execute skill in fork mode
            return self._fork_handler(skill_name, args_str)
        else:
            # Inline execution mode: return the resolved prompt as tool output so the main model can act on it
            resolved = resolve_prompt(skill, args_str)
            return ToolResult(True, resolved)
