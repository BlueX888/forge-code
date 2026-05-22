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


def global_config_path() -> Path:
    """Return the Path to the global configuration file."""
    return _GLOBAL_CONFIG_PATH


def has_global_model_config() -> bool:
    """Check if the global config has a valid model configuration."""
    path = global_config_path()
    if not path.is_file():
        return False
    cfg = _load_toml(path)
    model = cfg.get("model", {})
    if not isinstance(model, dict):
        return False
    return bool(
        model.get("name")
        and model.get("api_key")
        and model.get("base_url")
    )


def has_effective_model_config(working_directory: Path) -> bool:
    """Check if there is an effective model configuration (global or project fallback)."""
    return has_global_model_config() or has_project_model_config(working_directory)


def save_global_model_config(
    *,
    name: str | None = None,
    provider: str | None = None,
    api_key: str | None = None,
    base_url: str | None = None,
) -> None:
    """Save or update the global model configuration in ~/.forgecode/config.toml."""
    path = global_config_path()
    cfg = _load_toml(path)
    
    if "model" not in cfg or not isinstance(cfg["model"], dict):
        cfg["model"] = {}
        
    if name is not None:
        cfg["model"]["name"] = name
    if provider is not None:
        cfg["model"]["provider"] = provider
    if api_key is not None:
        cfg["model"]["api_key"] = api_key
    if base_url is not None:
        cfg["model"]["base_url"] = base_url
        
    content = _serialize_toml(cfg)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def migrate_project_model_config_to_global(working_directory: Path) -> None:
    """Migrate a project's model config to global if global has no valid model config and project has one."""
    if not has_global_model_config() and has_project_model_config(working_directory):
        project_cfg = _load_toml(project_config_path(working_directory))
        model_cfg = project_cfg.get("model", {})
        save_global_model_config(
            name=model_cfg.get("name"),
            provider=model_cfg.get("provider"),
            api_key=model_cfg.get("api_key"),
            base_url=model_cfg.get("base_url"),
        )


def load_config_file(working_directory: Path) -> dict:
    """Merge global + project config files.
    
    Rules:
    - For general sections (e.g. [agent], [commands]): global default, project overrides global.
    - For [model]: global model config has priority over legacy project config.
    - If global has no complete model config, but project does, we automatically migrate the project's model config to global, then load.
    """
    # 1. Automatic migration from project model config to global if applicable
    if not has_global_model_config() and has_project_model_config(working_directory):
        migrate_project_model_config_to_global(working_directory)

    global_cfg = _load_toml(_GLOBAL_CONFIG_PATH)
    project_cfg = _load_toml(working_directory / _PROJECT_CONFIG_NAME)

    merged: dict = {}
    
    # 2. General config merging (global + project, project wins)
    for cfg in (global_cfg, project_cfg):
        for section_key, section_val in cfg.items():
            if section_key == "model":
                continue
            if isinstance(section_val, dict):
                merged.setdefault(section_key, {}).update(section_val)
            else:
                merged[section_key] = section_val

    # 3. Model config merging
    global_model = global_cfg.get("model", {})
    project_model = project_cfg.get("model", {})

    def is_complete(m: dict) -> bool:
        return bool(
            isinstance(m, dict)
            and m.get("name")
            and m.get("api_key")
            and m.get("base_url")
        )

    if is_complete(global_model):
        merged["model"] = global_model
    elif is_complete(project_model):
        merged["model"] = project_model
    else:
        # Fallback/partial merge: start with project_model, override with global_model
        m = {}
        if isinstance(project_model, dict):
            m.update(project_model)
        if isinstance(global_model, dict):
            m.update(global_model)
        merged["model"] = m

    return merged


def project_config_path(working_directory: Path) -> Path:
    """Return the Path to the project configuration file."""
    return working_directory / _PROJECT_CONFIG_NAME


def _serialize_val(val: object) -> str:
    if isinstance(val, bool):
        return "true" if val else "false"
    elif isinstance(val, (int, float)):
        return str(val)
    elif isinstance(val, str):
        escaped = val.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n").replace("\r", "\\r")
        return f'"{escaped}"'
    elif isinstance(val, list):
        items = [_serialize_val(item) for item in val]
        return f"[{', '.join(items)}]"
    else:
        return f'"{val}"'


def _serialize_toml(cfg: dict) -> str:
    """Serialize a dictionary with section dictionaries to TOML format."""
    lines = []
    # Write top-level key-values first
    for k, v in cfg.items():
        if not isinstance(v, dict):
            lines.append(f"{k} = {_serialize_val(v)}")
            
    # Write sections (tables)
    for section_key, section_val in cfg.items():
        if isinstance(section_val, dict):
            if lines:
                lines.append("")
            lines.append(f"[{section_key}]")
            for k, v in section_val.items():
                lines.append(f"{k} = {_serialize_val(v)}")
                
    return "\n".join(lines) + "\n"


def has_project_model_config(working_directory: Path) -> bool:
    """Check if the project has a valid model configuration."""
    path = project_config_path(working_directory)
    if not path.is_file():
        return False
    cfg = _load_toml(path)
    model = cfg.get("model", {})
    if not isinstance(model, dict):
        return False
    return bool(
        model.get("name")
        and model.get("api_key")
        and model.get("base_url")
    )


def save_project_model_config(
    working_directory: Path,
    *,
    name: str | None = None,
    provider: str | None = None,
    api_key: str | None = None,
    base_url: str | None = None,
) -> None:
    """Save or update the project model configuration in .forgecode.toml."""
    path = project_config_path(working_directory)
    cfg = _load_toml(path)
    
    if "model" not in cfg or not isinstance(cfg["model"], dict):
        cfg["model"] = {}
        
    if name is not None:
        cfg["model"]["name"] = name
    if provider is not None:
        cfg["model"]["provider"] = provider
    if api_key is not None:
        cfg["model"]["api_key"] = api_key
    if base_url is not None:
        cfg["model"]["base_url"] = base_url
        
    content = _serialize_toml(cfg)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


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
    model_name: str | None = None
    provider: str = "openai"  # "openai" | "anthropic"
    api_key: str | None = None
    base_url: str | None = None
    max_history_messages: int = 100
    max_tool_iterations: int = 100
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
    # Layer 1: Truncation
    truncate_line_threshold: int = 2_000
    truncate_byte_threshold: int = 50_000
    truncate_cleanup_interval: int = 3_600
    truncate_max_age: int = 604_800
    # Layer 2: Pruning
    prune_protect_tokens: int = 40_000
    prune_minimum_tokens: int = 10_000
    # Layer 3: Compaction
    compaction_buffer_tokens: int = 20_000
    # Layer 3: Compaction — trigger control
    compaction_trigger_ratio: float = 0.95   # only compact when usage > 95% of capacity
    compaction_timeout: int = 30              # timeout (seconds) for compaction LLM call
    # API client timeouts
    api_timeout: int = 120                    # HTTP read timeout for model API calls
    api_connect_timeout: int = 15             # HTTP connect timeout for model API calls
    tail_budget_ratio: float = 0.25
    tail_clamp_min: int = 2_000
    tail_clamp_max: int = 8_000
    tail_min_turns: int = 5
    tool_output_max_chars: int = 2_000
    default_max_result_chars: int = 50_000
    tool_result_budget: dict[str, int] = dataclasses.field(default_factory=dict)
    # Layer 3: Compaction — summarize call token budget
    compaction_max_output_tokens: int = 16_000  # dedicated output cap for summarize LLM calls
    # Layer 2: Pruning — tool whitelist and on/off switch
    prune_protected_tools: tuple[str, ...] = ()  # tool names whose results are never pruned
    prune_enabled: bool = True                    # set False to disable Layer 2 entirely
    # Lightweight context mode
    token_overhead_estimate: int = 4_000          # system prompt overhead estimate (tokens)
    lightweight_turn_threshold: int = 10          # skip Layer 2/3 when turn count <= this

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

        spill_dir_raw = agent_section.get("context_spill_dir")
        context_spill_dir = Path(spill_dir_raw) if spill_dir_raw is not None else None

        default_max_result_chars = int(agent_section.get("default_max_result_chars", 50_000))
        tool_result_budget = {k: int(v) for k, v in agent_section.get("tool_result_budget", {}).items()}

        return cls(
            working_directory=working_directory,
            model_name=cli_model or model_section.get("name"),
            provider=cli_provider or model_section.get("provider", "openai"),
            api_key=cli_api_key or model_section.get("api_key"),
            base_url=cli_base_url or model_section.get("base_url"),
            allow_dangerous_operations=cls._resolve_dangerous_mode(
                cli_dangerous_mode,
                agent_section.get("dangerous_mode"),
                agent_section.get("allow_dangerous"),
            ),
            max_history_messages=agent_section.get("max_history_messages", 100),
            max_tool_iterations=agent_section.get("max_tool_iterations", 100),
            max_output_tokens=agent_section.get("max_output_tokens", 4096),
            command_timeout=agent_section.get("command_timeout", 120),
            permitted_directories=permitted_dirs,
            parallel_tool_execution=agent_section.get("parallel_tool_execution", True),
            extra_safe_commands=tuple(commands_section.get("safe", [])),
            max_context_tokens=cls._resolve_max_context_tokens(agent_section),
            enable_context_management=agent_section.get("enable_context_management", True),
            context_idle_timeout=agent_section.get("context_idle_timeout", 300),
            context_spill_dir=context_spill_dir,
            auto_compact_enabled=agent_section.get("auto_compact_enabled", True),
            memory_enabled=memory_enabled,
            memory_dir=memory_dir,
            show_thinking=cli_show_thinking if cli_show_thinking is not None else agent_section.get("show_thinking", True),
            thinking_budget=cli_thinking_budget if cli_thinking_budget is not None else agent_section.get("thinking_budget", 10_000),
            banner_width=int(agent_section.get("banner_width", agent_section.get("width", 80))),
            default_max_result_chars=default_max_result_chars,
            tool_result_budget=tool_result_budget,
            # Layer 1: Truncation
            truncate_line_threshold=int(agent_section.get("truncate_line_threshold", 2_000)),
            truncate_byte_threshold=int(agent_section.get("truncate_byte_threshold", 50_000)),
            truncate_cleanup_interval=int(agent_section.get("truncate_cleanup_interval", 3_600)),
            truncate_max_age=int(agent_section.get("truncate_max_age", 604_800)),
            # Layer 2: Pruning
            prune_protect_tokens=int(agent_section.get("prune_protect_tokens", 40_000)),
            prune_minimum_tokens=int(agent_section.get("prune_minimum_tokens", 10_000)),
            # Layer 3: Compaction
            compaction_buffer_tokens=int(agent_section.get("compaction_buffer_tokens", 20_000)),
            compaction_trigger_ratio=float(agent_section.get("compaction_trigger_ratio", 0.95)),
            compaction_timeout=int(agent_section.get("compaction_timeout", 30)),
            api_timeout=int(agent_section.get("api_timeout", 120)),
            api_connect_timeout=int(agent_section.get("api_connect_timeout", 15)),
            tail_budget_ratio=float(agent_section.get("tail_budget_ratio", 0.25)),
            tail_clamp_min=int(agent_section.get("tail_clamp_min", 2_000)),
            tail_clamp_max=int(agent_section.get("tail_clamp_max", 8_000)),
            tail_min_turns=int(agent_section.get("tail_min_turns", 5)),
            tool_output_max_chars=int(agent_section.get("tool_output_max_chars", 2_000)),
            compaction_max_output_tokens=int(agent_section.get("compaction_max_output_tokens", 16_000)),
            prune_protected_tools=tuple(agent_section.get("prune_protected_tools", [])),
            prune_enabled=bool(agent_section.get("prune_enabled", True)),
            token_overhead_estimate=int(agent_section.get("token_overhead_estimate", 4_000)),
            lightweight_turn_threshold=int(agent_section.get("lightweight_turn_threshold", 10)),
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


def compute_session_dir(working_directory: Path) -> Path:
    """Compute project-isolated session directory: ~/.forgecode/sessions/{hash}/sessions/"""
    project_hash = hashlib.sha256(str(working_directory).encode()).hexdigest()[:16]
    return Path.home() / ".forgecode" / "sessions" / project_hash / "sessions"

