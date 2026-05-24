"""Core Skill module for Forge-Code."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)

@dataclass
class SkillDefinition:
    name: str
    description: str
    when_to_use: str
    allowed_tools: list[str]
    user_invocable: bool
    context: str
    prompt_template: str
    path: Path


_skills_cache: dict[str, SkillDefinition] | None = None


def invalidate_skills_cache() -> None:
    """Invalidate the module-level skills cache."""
    global _skills_cache
    _skills_cache = None


def _parse_skill_file(path: Path) -> SkillDefinition | None:
    """Parse a Markdown skill file with YAML frontmatter."""
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        logger.warning("Could not read skill file %s: %s", path, exc)
        return None

    if not text.startswith("---"):
        return None
    end = text.find("---", 3)
    if end == -1:
        return None

    yaml_block = text[3:end].strip()
    prompt_template = text[end + 3:].strip()

    meta: dict[str, str] = {}
    for line in yaml_block.split("\n"):
        if ":" in line:
            key, _, value = line.partition(":")
            meta[key.strip()] = value.strip()

    name = meta.get("name", path.stem).strip()
    description = meta.get("description", "").strip()
    when_to_use = meta.get("when_to_use", "").strip()

    # context
    context = meta.get("context", "inline").strip().lower()
    if context not in ("inline", "fork"):
        context = "inline"

    # allowed-tools
    allowed_tools_str = meta.get("allowed-tools", meta.get("allowed_tools", "")).strip()
    allowed_tools = [t.strip() for t in allowed_tools_str.split(",") if t.strip()] if allowed_tools_str else []

    # user-invocable
    user_invocable_str = meta.get("user-invocable", meta.get("user_invocable", "true")).strip().lower()
    user_invocable = user_invocable_str != "false"

    return SkillDefinition(
        name=name,
        description=description,
        when_to_use=when_to_use,
        allowed_tools=allowed_tools,
        user_invocable=user_invocable,
        context=context,
        prompt_template=prompt_template,
        path=path,
    )


def discover_skills(working_directory: Path) -> dict[str, SkillDefinition]:
    """Discover all user-level and project-level skills with dict-based merge."""
    global _skills_cache
    if _skills_cache is not None:
        return _skills_cache

    skills: dict[str, SkillDefinition] = {}

    # 1. User-level: ~/.forgecode/skills/**/*.md (lower priority)
    try:
        user_dir = Path.home() / ".forgecode" / "skills"
        if user_dir.exists():
            for path in user_dir.glob("**/*.md"):
                if path.is_file():
                    skill = _parse_skill_file(path)
                    if skill:
                        skills[skill.name] = skill
    except Exception as exc:
        logger.warning("Error discovering user-level skills: %s", exc)

    # 2. Project-level: .forgecode/skills/**/*.md (higher priority, overrides by name)
    try:
        project_dir = working_directory / ".forgecode" / "skills"
        if project_dir.exists():
            for path in project_dir.glob("**/*.md"):
                if path.is_file():
                    skill = _parse_skill_file(path)
                    if skill:
                        skills[skill.name] = skill
    except Exception as exc:
        logger.warning("Error discovering project-level skills: %s", exc)

    _skills_cache = skills
    return skills


def resolve_prompt(skill: SkillDefinition, arguments: str) -> str:
    """Resolve skill prompt template replacing variables."""
    prompt = skill.prompt_template
    # Replace $ARGUMENTS
    prompt = prompt.replace("$ARGUMENTS", arguments)
    # Replace ${FORGE_SKILL_DIR}
    prompt = prompt.replace("${FORGE_SKILL_DIR}", skill.path.parent.resolve().as_posix())
    return prompt


def build_skill_descriptions(working_directory: Path) -> str:
    """Build the skill section for system prompt injection."""
    skills = discover_skills(working_directory)
    if not skills:
        return ""

    sections = [
        "## Skills",
        "",
        "You can run custom prompt-based workflows by calling the `Skill` tool.",
        "Here are the available skills:",
    ]
    for name, skill in sorted(skills.items()):
        sections.append(f"\n### {skill.name}")
        if skill.description:
            sections.append(f"- **Description**: {skill.description}")
        if skill.when_to_use:
            sections.append(f"- **When to use**: {skill.when_to_use}")
        sections.append(f"- **Invocation syntax**: Skill(skill=\"{skill.name}\", args=\"<arguments>\")")
    
    return "\n".join(sections)
