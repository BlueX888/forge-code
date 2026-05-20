"""Agent configuration."""

from __future__ import annotations

import dataclasses
import enum
import hashlib
import sys
import tomllib
from pathlib import Path


# ---------------------------------------------------------------------------
# Config file locations
# ---------------------------------------------------------------------------

_GLOBAL_CONFIG_PATH = Path.home() / ".forgecode" / "config.toml"
_PROJECT_CONFIG_NAME = ".forgecode.toml"


def _load_toml(path: Path) -> dict:
    if not path.is_file():
        return {}
    try:
        raw = path.read_bytes()
        if raw.startswith(b"\xef\xbb\xbf"):  # strip UTF-8 BOM
            raw = raw[3:]
        return tomllib.loads(raw.decode("utf-8"))
    except (tomllib.TOMLDecodeError, UnicodeDecodeError) as exc:
        print(f"Warning: skipping invalid config {path}: {exc}", file=sys.stderr)
        return {}


def load_config_file(working_directory: Path) -> dict:
    """Merge global + project config files. Project values win."""
    global_cfg = _load_toml(_GLOBAL_CONFIG_PATH)
    project_cfg = _load_toml(working_directory / _PROJECT_CONFIG_NAME)

    merged: dict = {}
    for cfg in (global_cfg, project_cfg):
        for section_key, section_val in cfg.items():
            if isinstance(section_val, dict):
                merged.setdefault(section_key, {}).update(section_val)
            else:
                merged[section_key] = section_val
    return merged


# ---------------------------------------------------------------------------
# DangerousMode
# ---------------------------------------------------------------------------

class DangerousMode(enum.Enum):
    """Policy for dangerous operations (write / execute)."""
    DENY = "deny"
    ASK = "ask"
    ALLOW = "allow"


def _normalize_dangerous_mode(raw: object, default: DangerousMode) -> DangerousMode:
    """Coerce a config value to DangerousMode, with backward compat for bool."""
    if isinstance(raw, DangerousMode):
        return raw
    if isinstance(raw, bool):
        return DangerousMode.ALLOW if raw else DangerousMode.ASK
    if isinstance(raw, str):
        try:
            return DangerousMode(raw.lower())
        except ValueError:
            pass
    return default


# ---------------------------------------------------------------------------
# AgentConfig
# ---------------------------------------------------------------------------

@dataclasses.dataclass(frozen=True)
class AgentConfig:
    """Immutable agent configuration."""

    working_directory: Path
    allow_dangerous_operations: DangerousMode = DangerousMode.ASK
    model_name: str = "placeholder"
    provider: str = "openai"  # "openai" | "anthropic"
    api_key: str | None = None
    base_url: str | None = None
    max_history_messages: int = 50
    max_tool_iterations: int = 25
    max_output_tokens: int = 4096
    command_timeout: int = 120
    permitted_directories: tuple[Path, ...] = ()
    parallel_tool_execution: bool = True
    extra_safe_commands: tuple[str, ...] = ()
    max_context_tokens: int = 258_000
    enable_context_management: bool = True
    context_idle_timeout: int = 300
    context_spill_dir: Path | None = None
    auto_compact_enabled: bool = True
    memory_enabled: bool = True
    memory_dir: Path | None = None
    show_thinking: bool = True
    thinking_budget: int = 10_000
    banner_width: int = 80

    @classmethod
    def from_file_and_args(
        cls,
        working_directory: Path,
        *,
        cli_model: str | None = None,
        cli_provider: str | None = None,
        cli_api_key: str | None = None,
        cli_base_url: str | None = None,
        cli_dangerous_mode: DangerousMode | None = None,
        cli_show_thinking: bool | None = None,
        cli_thinking_budget: int | None = None,
    ) -> AgentConfig:
        """Build config with priority: CLI args > config file > defaults."""
        file_cfg = load_config_file(working_directory)
        model_section = file_cfg.get("model", {})
        agent_section = file_cfg.get("agent", {})
        commands_section = file_cfg.get("commands", {})

        memory_enabled = agent_section.get("memory_enabled", True)
        memory_dir = cls._compute_memory_dir(working_directory)

        extra_permitted = tuple(Path(p) for p in agent_section.get("permitted_directories", []))
        permitted_dirs = extra_permitted + ((memory_dir,) if memory_enabled else ())

        return cls(
            working_directory=working_directory,
            model_name=cli_model or model_section.get("name", "placeholder"),
            provider=cli_provider or model_section.get("provider", "openai"),
            api_key=cli_api_key or model_section.get("api_key"),
            base_url=cli_base_url or model_section.get("base_url"),
            allow_dangerous_operations=cls._resolve_dangerous_mode(
                cli_dangerous_mode,
                agent_section.get("dangerous_mode"),
                agent_section.get("allow_dangerous"),
            ),
            max_history_messages=agent_section.get("max_history_messages", 50),
            max_output_tokens=agent_section.get("max_output_tokens", 4096),
            parallel_tool_execution=agent_section.get("parallel_tool_execution", True),
            extra_safe_commands=tuple(commands_section.get("safe", [])),
            max_context_tokens=cls._resolve_max_context_tokens(agent_section),
            enable_context_management=agent_section.get("enable_context_management", True),
            context_idle_timeout=agent_section.get("context_idle_timeout", 300),
            auto_compact_enabled=agent_section.get("auto_compact_enabled", True),
            memory_enabled=memory_enabled,
            memory_dir=memory_dir,
            permitted_directories=permitted_dirs,
            show_thinking=cli_show_thinking if cli_show_thinking is not None else agent_section.get("show_thinking", True),
            thinking_budget=cli_thinking_budget if cli_thinking_budget is not None else agent_section.get("thinking_budget", 10_000),
            banner_width=int(agent_section.get("banner_width", agent_section.get("width", 80))),
        )

    @staticmethod
    def _resolve_max_context_tokens(agent_section: dict) -> int:
        if "max_context_tokens" in agent_section:
            return int(agent_section["max_context_tokens"])
        if "max_context_chars" in agent_section:
            return int(agent_section["max_context_chars"]) // 4
        return 258_000

    @staticmethod
    def _compute_memory_dir(working_directory: Path) -> Path:
        """Compute project-isolated memory directory: ~/.forgecode/projects/{hash}/memory/"""
        project_hash = hashlib.sha256(str(working_directory).encode()).hexdigest()[:16]
        return Path.home() / ".forgecode" / "projects" / project_hash / "memory"

    @staticmethod
    def _resolve_dangerous_mode(
        cli_value: DangerousMode | None,
        file_dangerous_mode: object,
        file_allow_dangerous: object,
    ) -> DangerousMode:
        """CLI > config file 'dangerous_mode' > config file 'allow_dangerous' > ASK."""
        if cli_value is not None:
            return cli_value
        if file_dangerous_mode is not None:
            return _normalize_dangerous_mode(file_dangerous_mode, DangerousMode.DENY)
        if file_allow_dangerous is not None:
            return _normalize_dangerous_mode(file_allow_dangerous, DangerousMode.DENY)
        return DangerousMode.ASK

    def is_path_allowed(self, path: Path) -> bool:
        """Check whether *path* is inside the sandbox.

        The sandbox consists of ``working_directory`` plus every entry in
        ``permitted_directories``.  Symlinks are resolved before checking.
        """
        resolved = path.resolve()
        allowed_roots = (self.working_directory.resolve(),) + tuple(
            p.resolve() for p in self.permitted_directories
        )
        return any(
            resolved == root or resolved.is_relative_to(root)
            for root in allowed_roots
        )
