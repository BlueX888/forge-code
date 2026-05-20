"""Interactive TUI launcher and configuration wizard for ForgeCode."""

from __future__ import annotations

import getpass
import os
import shutil
import sys
from pathlib import Path
from typing import TYPE_CHECKING

from coding_agent.config import AgentConfig, DangerousMode
from coding_agent.session import SessionManager

if TYPE_CHECKING:
    from coding_agent.session import SessionData, SessionMetadata


# ---------------------------------------------------------------------------
# EMOJI Fallbacks and Safe Printing for Windows/GBK Compatibility
# ---------------------------------------------------------------------------

EMOJI_FALLBACKS = {
    "🚀": "=>",
    "💾": "[SAVE]",
    "🔄": "[RESUME]",
    "📂": "[LIST]",
    "⚙️": "[CONFIG]",
    "❌": "[EXIT]",
    "⚠️": "WARNING:",
}


def safe_print(text: str) -> None:
    """Print text safely, replacing unencodable emojis/chars to prevent UnicodeEncodeError."""
    encoding = sys.stdout.encoding or "utf-8"
    try:
        # Test if the string can be encoded in the stdout encoding
        text.encode(encoding)
        sys.stdout.write(text + "\n")
        sys.stdout.flush()
    except UnicodeEncodeError:
        # Fallback 1: Replace known emojis with safe text representations
        cleaned = text
        for emoji, replacement in EMOJI_FALLBACKS.items():
            cleaned = cleaned.replace(emoji, replacement)
        
        try:
            # Fallback 2: Encode with 'replace' to prevent crashing on remaining unencodable chars
            encoded = cleaned.encode(encoding, errors="replace")
            sys.stdout.write(encoded.decode(encoding) + "\n")
            sys.stdout.flush()
        except Exception:
            # Fallback 3: Strict ASCII fallback
            try:
                sys.stdout.write(text.encode("ascii", errors="replace").decode("ascii") + "\n")
                sys.stdout.flush()
            except Exception:
                pass  # Absolute silent fallback to avoid crashing under any circumstances


# ---------------------------------------------------------------------------
# ANSI Color Helpers
# ---------------------------------------------------------------------------

def _supports_color() -> bool:
    return hasattr(sys.stdout, "isatty") and sys.stdout.isatty()


COLOR_SUPPORTED = _supports_color()


def c(code: str, text: str) -> str:
    """Wrap text in ANSI escape code if color is supported."""
    if not COLOR_SUPPORTED:
        return text
    return f"\033[{code}m{text}\033[0m"


# Color palettes matching the project's CRT retro aesthetic
STYLE = {
    "GLOW": "1;38;2;57;255;20",       # Bright phosphor green
    "BORDER": "38;2;0;120;0",         # Dark green border
    "TITLE": "1;32",                  # Standard bright green
    "LABEL": "36",                    # Cyan label
    "VALUE": "37",                    # White value
    "WARN": "1;31",                   # Red warning
    "HIGHLIGHT": "1;33",              # Yellow highlight
    "DIM": "2;37",                    # Dim white
}


# ---------------------------------------------------------------------------
# ASCII Art
# ---------------------------------------------------------------------------

_BANNER_ART = [
    r"███████╗ ██████╗ ██████╗  ██████╗ ███████╗",
    r"██╔════╝██╔═══██╗██╔══██╗██╔════╝ ██╔════╝",
    r"█████╗  ██║   ██║██████╔╝██║  ███╗█████╗  ",
    r"██╔══╝  ██║   ██║██╔══██╗██║   ██║██╔══╝  ",
    r"██║     ╚██████╔╝██║  ██║╚██████╔╝███████╗",
    r"╚═╝      ╚═════╝ ╚═╝  ╚═╝ ╚═════╝ ╚══════╝",
]


# ---------------------------------------------------------------------------
# TOML Writer
# ---------------------------------------------------------------------------

def write_toml_config(
    path: Path,
    provider: str,
    model_name: str,
    api_key: str,
    base_url: str | None = None,
) -> None:
    """Atomically write model configuration in TOML format."""
    path.parent.mkdir(parents=True, exist_ok=True)
    
    lines = [
        "[model]",
        f'provider = "{provider}"',
        f'name = "{model_name}"',
        f'api_key = "{api_key}"',
    ]
    if base_url:
        lines.append(f'base_url = "{base_url}"')
    
    content = "\n".join(lines) + "\n"
    
    tmp = path.with_suffix(".tmp")
    tmp.write_text(content, encoding="utf-8")
    tmp.replace(path)


def save_cli_config_permanently(
    working_dir: Path,
    model: str | None = None,
    provider: str | None = None,
    api_key: str | None = None,
    base_url: str | None = None,
) -> None:
    """Merge and save the provided API flags into the project's local .forgecode.toml."""
    config_path = working_dir / ".forgecode.toml"
    
    # Load existing local TOML configs first if they exist to prevent wiping out other configurations
    from coding_agent.config import load_config_file
    existing = load_config_file(working_dir).get("model", {})
    
    # Merge values: CLI flags win over existing values
    p = provider or existing.get("provider", "openai")
    m = model or existing.get("name", "placeholder")
    k = api_key or existing.get("api_key", "")
    u = base_url or existing.get("base_url", "")
    
    write_toml_config(
        config_path,
        provider=p,
        model_name=m,
        api_key=k,
        base_url=u or None,
    )


# ---------------------------------------------------------------------------
# TUI Views
# ---------------------------------------------------------------------------

def clear_screen() -> None:
    """Clear terminal screen."""
    os.system("cls" if os.name == "nt" else "clear")


def draw_dashboard(
    config: AgentConfig,
    latest_session: SessionMetadata | None,
    width: int = 76,
) -> None:
    """Draw a beautiful CRT-styled dashboard."""
    h_char = "═"
    tl, tr = "╔", "╗"
    bl, br = "╚", "╝"
    v_char = "║"
    divider = "╠" + "═" * (width - 2) + "╣"

    # Print Top Border
    safe_print(c(STYLE["BORDER"], tl + h_char * (width - 2) + tr))

    # Print Centered Banner Art
    for line in _BANNER_ART:
        line_len = len(line)
        left_pad = (width - 2 - line_len) // 2
        right_pad = width - 2 - line_len - left_pad
        padded = " " * left_pad + line + " " * right_pad
        safe_print(c(STYLE["BORDER"], v_char) + c(STYLE["GLOW"], padded) + c(STYLE["BORDER"], v_char))

    # Subtitle
    sub = "L A U N C H E R  &  C O N F I G  P A N E L"
    left_pad = (width - 2 - len(sub)) // 2
    right_pad = width - 2 - len(sub) - left_pad
    sub_line = " " * left_pad + sub + " " * right_pad
    safe_print(c(STYLE["BORDER"], v_char) + c(STYLE["GLOW"], sub_line) + c(STYLE["BORDER"], v_char))

    # Divider
    safe_print(c(STYLE["BORDER"], divider))

    # System Status Section
    safe_print(c(STYLE["BORDER"], v_char) + c(STYLE["TITLE"], "  [SYSTEM STATUS]").ljust(width - 2 + len(c(STYLE["TITLE"], ""))) + c(STYLE["BORDER"], v_char))
    
    ws_str = str(config.working_directory)
    if len(ws_str) > width - 20:
        ws_str = "..." + ws_str[-(width - 23):]
    
    print_field(v_char, "Workspace", ws_str, width)
    
    provider = config.provider
    model = config.model_name
    print_field(v_char, "API Model", f"{model} ({provider})", width)

    # API key check
    if config.model_name == "placeholder":
        key_status = c(STYLE["WARN"], "Placeholder Mode (No Real AI)")
    elif config.api_key:
        masked = config.api_key[:6] + "..." + config.api_key[-4:] if len(config.api_key) > 10 else "******"
        key_status = f"{masked} " + c(STYLE["GLOW"], "(Configured)")
    else:
        key_status = c(STYLE["WARN"], "Not Configured (Will fail for real models)")
    
    print_field(v_char, "API Key", key_status, width)
    print_field(v_char, "Safety Mode", config.allow_dangerous_operations.value, width)

    # Divider
    safe_print(c(STYLE["BORDER"], divider))

    # Latest Session Section
    safe_print(c(STYLE["BORDER"], v_char) + c(STYLE["TITLE"], "  [LATEST PERSISTENT SESSION]").ljust(width - 2 + len(c(STYLE["TITLE"], ""))) + c(STYLE["BORDER"], v_char))
    
    if latest_session:
        print_field(v_char, "Session ID", latest_session.session_id, width)
        title_str = latest_session.title or "(untitled)"
        if len(title_str) > width - 20:
            title_str = title_str[:width - 23] + "..."
        print_field(v_char, "Title", title_str, width)
        
        # Format updated_at: YYYY-MM-DD HH:MM:SS
        try:
            from datetime import datetime
            dt = datetime.fromisoformat(latest_session.updated_at.replace("Z", "+00:00"))
            time_str = dt.strftime("%Y-%m-%d %H:%M:%S Local")
        except Exception:
            time_str = latest_session.updated_at
            
        print_field(v_char, "Last Active", time_str, width)
    else:
        empty_msg = c(STYLE["DIM"], "No saved sessions found in this workspace.")
        empty_line = "    " + empty_msg
        raw_len = 4 + len("No saved sessions found in this workspace.")
        safe_print(c(STYLE["BORDER"], v_char) + empty_line + " " * (width - 2 - raw_len) + c(STYLE["BORDER"], v_char))

    # Divider
    safe_print(c(STYLE["BORDER"], divider))

    # Interactive Menu Options
    safe_print(c(STYLE["BORDER"], v_char) + c(STYLE["TITLE"], "  [MENU OPTIONS]").ljust(width - 2 + len(c(STYLE["TITLE"], ""))) + c(STYLE["BORDER"], v_char))
    
    menu_items = [
        ("1", "🚀 Start New Standard Session (No persistence)"),
        ("2", "💾 Start New Persistent Session (Auto-saves history)"),
    ]
    if latest_session:
        menu_items.append(("3", f"🔄 Resume Latest Session ({latest_session.session_id[:15]}...)"))
    else:
        menu_items.append(("3", c(STYLE["DIM"], "🔄 Resume Latest Session (No saved sessions)")))
        
    menu_items.extend([
        ("4", "📂 List & Load Saved Sessions"),
        ("5", "⚙️  Configure API Provider & Model Wizard"),
        ("6", "❌ Exit"),
    ])

    for key, text in menu_items:
        raw_text = f"  [{key}] {text}"
        # adjust padding by ignoring color codes inside the text
        clean_text = raw_text.replace(STYLE["DIM"], "").replace("\033[0m", "")
        pad = width - 2 - len(clean_text)
        safe_print(c(STYLE["BORDER"], v_char) + c(STYLE["HIGHLIGHT"], f"  [{key}] ") + text + " " * pad + c(STYLE["BORDER"], v_char))

    # Bottom Border
    safe_print(c(STYLE["BORDER"], bl + h_char * (width - 2) + br))


def print_field(v_char: str, label: str, val_with_ansi: str, width: int) -> None:
    """Print formatted label: value field inside the TUI card borders."""
    lbl = f"    {label:<11s}: "
    # strip ANSI to calculate true print length of value
    ansi_escapes = [STYLE[k] for k in STYLE] + ["0m", "1;32m", "32m", "31m", "33m", "35m", "36m", "37m", "2;37m", "1;38;2;57;255;20m", "38;2;0;120m"]
    val_clean = val_with_ansi
    for esc in ansi_escapes:
        val_clean = val_clean.replace(f"\033[{esc}", "").replace("\033[", "")
    val_clean = val_clean.replace("\033[0m", "")
    
    content = c(STYLE["LABEL"], lbl) + val_with_ansi
    raw_len = len(lbl) + len(val_clean)
    pad = width - 2 - raw_len
    safe_print(c(STYLE["BORDER"], v_char) + content + " " * pad + c(STYLE["BORDER"], v_char))


# ---------------------------------------------------------------------------
# API Configuration Wizard
# ---------------------------------------------------------------------------

def configure_api_wizard(working_dir: Path) -> bool:
    """Interactively guide the user through setting up model config.
    
    Returns True if config was successfully written.
    """
    clear_screen()
    safe_print(c(STYLE["HIGHLIGHT"], "=== ForgeCode API Configuration Wizard ===\n"))

    safe_print("Please select your model provider:")
    safe_print("  [1] openai-compatible (DeepSeek, OpenAI, Ollama, SiliconFlow, etc.)")
    safe_print("  [2] anthropic (Claude)")
    
    provider_choice = ""
    while provider_choice not in ("1", "2"):
        provider_choice = input(c(STYLE["HIGHLIGHT"], "Choose provider [1-2] (default: 1): ")).strip()
        if not provider_choice:
            provider_choice = "1"
            
    provider = "openai" if provider_choice == "1" else "anthropic"
    safe_print(f"Selected Provider: {c(STYLE['GLOW'], provider)}\n")

    # Ask for Model Name with recommendations
    if provider == "openai":
        safe_print("Popular OpenAI-compatible Models:")
        safe_print("  - deepseek-chat (DeepSeek-V3 / R1)")
        safe_print("  - gpt-4o")
        safe_print("  - gpt-4o-mini")
        safe_print("  - qwen-max")
        default_model = "deepseek-chat"
    else:
        safe_print("Popular Anthropic Models:")
        safe_print("  - claude-3-5-sonnet-20241022")
        safe_print("  - claude-3-5-haiku-20241022")
        default_model = "claude-3-5-sonnet-20241022"

    model_name = input(c(STYLE["HIGHLIGHT"], f"Enter Model Name (default: {default_model}): ")).strip()
    if not model_name:
        model_name = default_model
    safe_print(f"Selected Model: {c(STYLE['GLOW'], model_name)}\n")

    # Ask for API Key secretly
    api_key = ""
    while not api_key:
        api_key = getpass.getpass(c(STYLE["HIGHLIGHT"], "Enter your API Key (input is hidden): ")).strip()
        if not api_key:
            safe_print(c(STYLE["WARN"], "API Key is required to connect to real models."))
            
    # Ask for Base URL (only relevant for openai or customized anthropic endpoints)
    default_url = "https://api.deepseek.com/v1" if provider == "openai" and "deepseek" in model_name else ""
    url_prompt = f"Enter Base URL (Press Enter for default{f' [{default_url}]' if default_url else ''}): "
    base_url = input(c(STYLE["HIGHLIGHT"], url_prompt)).strip()
    if not base_url and default_url:
        base_url = default_url

    # Ask for Save Location
    safe_print("\nWhere would you like to save this configuration?")
    safe_print("  [1] Project local (.forgecode.toml) - Applies only inside this workspace")
    safe_print("  [2] Global (~/.forgecode/config.toml) - Applies to all your workspaces")
    
    save_choice = ""
    while save_choice not in ("1", "2"):
        save_choice = input(c(STYLE["HIGHLIGHT"], "Select save location [1-2] (default: 1): ")).strip()
        if not save_choice:
            save_choice = "1"

    if save_choice == "1":
        config_path = working_dir / ".forgecode.toml"
        loc_desc = "project-local (.forgecode.toml)"
    else:
        config_path = Path.home() / ".forgecode" / "config.toml"
        loc_desc = "global (~/.forgecode/config.toml)"

    safe_print(f"\nWriting configuration to {c(STYLE['GLOW'], str(config_path))}...")
    try:
        write_toml_config(
            config_path,
            provider=provider,
            model_name=model_name,
            api_key=api_key,
            base_url=base_url or None,
        )
        safe_print(c(STYLE["GLOW"], "Success! Configuration saved successfully."))
        input("\nPress Enter to return to launcher...")
        return True
    except Exception as exc:
        safe_print(c(STYLE["WARN"], f"Failed to write configuration: {exc}"))
        input("\nPress Enter to return to launcher...")
        return False


# ---------------------------------------------------------------------------
# Sessions List & Manage View
# ---------------------------------------------------------------------------

def list_and_load_sessions(
    session_manager: SessionManager,
    width: int = 76,
) -> str | None:
    """Show an interactive list of all saved sessions.
    
    Returns the chosen session ID, or None if the user pressed Back.
    """
    while True:
        clear_screen()
        safe_print(c(STYLE["HIGHLIGHT"], "=== Saved persistent sessions ===\n"))
        
        sessions = session_manager.list_sessions()
        if not sessions:
            safe_print(c(STYLE["DIM"], "  No saved sessions found."))
            input("\nPress Enter to return to launcher...")
            return None

        # Display sessions table
        safe_print(f"  {'#':<3s}  {'Session ID':<20s}  {'Title':<30s}  {'Last Active'}")
        safe_print("  " + "-" * (width - 4))
        
        for idx, meta in enumerate(sessions, 1):
            title = meta.title or "(untitled)"
            if len(title) > 28:
                title = title[:25] + "..."
            
            try:
                from datetime import datetime
                dt = datetime.fromisoformat(meta.updated_at.replace("Z", "+00:00"))
                time_str = dt.strftime("%Y-%m-%d %H:%M:%S")
            except Exception:
                time_str = meta.updated_at
                
            safe_print(f"  {c(STYLE['HIGHLIGHT'], f'[{idx}]'):<12s}  {meta.session_id:<20s}  {title:<30s}  {time_str}")

        safe_print("\nOptions:")
        safe_print(f"  Enter {c(STYLE['HIGHLIGHT'], 'number')} to load session")
        safe_print(f"  Type {c(STYLE['WARN'], 'd <number>')} to delete a session")
        safe_print(f"  Type {c(STYLE['HIGHLIGHT'], 'b')} to go back")
        
        choice = input(c(STYLE["HIGHLIGHT"], "\nYour choice: ")).strip().lower()
        if choice == "b":
            return None
            
        if choice.startswith("d "):
            # Delete flow
            try:
                idx = int(choice[2:].strip())
                if 1 <= idx <= len(sessions):
                    target = sessions[idx - 1]
                    confirm = input(c(STYLE["WARN"], f"Are you sure you want to delete session {target.session_id}? [y/N]: ")).strip().lower()
                    if confirm in ("y", "yes"):
                        if session_manager.delete(target.session_id):
                            safe_print(c(STYLE["GLOW"], "Session deleted successfully."))
                        else:
                            safe_print(c(STYLE["WARN"], "Failed to delete session file."))
                        import time
                        time.sleep(1)
                else:
                    safe_print(c(STYLE["WARN"], "Invalid session index."))
                    import time
                    time.sleep(1)
            except ValueError:
                safe_print(c(STYLE["WARN"], "Invalid index format."))
                import time
                time.sleep(1)
            continue
            
        try:
            idx = int(choice)
            if 1 <= idx <= len(sessions):
                return sessions[idx - 1].session_id
            else:
                safe_print(c(STYLE["WARN"], "Invalid session index."))
                import time
                time.sleep(1)
        except ValueError:
            safe_print(c(STYLE["WARN"], "Unknown option. Enter a number, 'd <number>', or 'b'."))
            import time
            time.sleep(1)


# ---------------------------------------------------------------------------
# Main Launcher Control Loop
# ---------------------------------------------------------------------------

def run_launcher(
    working_dir: Path,
    session_dir: Path,
    cli_dangerous_mode: DangerousMode | None = None,
) -> tuple[AgentConfig, SessionData | None, SessionManager | None, bool]:
    """Run the TUI startup launcher loop.
    
    Returns:
        (config, session, session_manager, should_exit)
    """
    session_manager = SessionManager(session_dir)
    
    while True:
        # Load newest config
        config = AgentConfig.from_file_and_args(
            working_dir,
            cli_dangerous_mode=cli_dangerous_mode,
        )
        
        # Load newest latest session
        latest_sessions = session_manager.list_sessions()
        latest_session = latest_sessions[0] if latest_sessions else None
        
        clear_screen()
        draw_dashboard(config, latest_session)
        
        choice = input(c(STYLE["HIGHLIGHT"], "\nEnter option [1-6]: ")).strip()
        if not choice:
            continue
            
        if choice == "1":
            # Start standard non-persistent session
            safe_print(c(STYLE["GLOW"], "\nStarting standard session (No persistence)..."))
            import time
            time.sleep(0.5)
            return config, None, None, False
            
        elif choice == "2":
            # Start new persistent session
            safe_print(c(STYLE["GLOW"], "\nCreating new persistent session..."))
            session = session_manager.create_session(
                model_name=config.model_name,
                provider=config.provider,
                working_directory=str(working_dir),
            )
            time.sleep(0.5)
            return config, session, session_manager, False
            
        elif choice == "3" and latest_session:
            # Resume latest session
            safe_print(c(STYLE["GLOW"], f"\nResuming latest session: {latest_session.session_id}..."))
            session = session_manager.load(latest_session.session_id)
            time.sleep(0.5)
            return config, session, session_manager, False
            
        elif choice == "4":
            # List & load saved sessions
            session_id = list_and_load_sessions(session_manager)
            if session_id:
                safe_print(c(STYLE["GLOW"], f"\nLoading session {session_id}..."))
                session = session_manager.load(session_id)
                time.sleep(0.5)
                return config, session, session_manager, False
                
        elif choice == "5":
            # Run API Setup Wizard
            configure_api_wizard(working_dir)
            
        elif choice == "6":
            # Exit
            safe_print(c(STYLE["GLOW"], "\nGoodbye!"))
            return config, None, None, True
            
        else:
            safe_print(c(STYLE["WARN"], "Invalid choice. Please pick an option from the menu."))
            time.sleep(1)
