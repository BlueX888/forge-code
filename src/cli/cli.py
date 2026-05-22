"""CLI entry point for ForgeCode."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from safety.command_policy import CommandPolicy, make_command_rule
from main.config import AgentConfig, DangerousMode, compute_session_dir
from cli.io import AgentIO
from safety.permissions import PermissionChecker
from tools.builtin import register_builtin_tools
from tools.registry import ToolRegistry


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        prog="forge-code",
        description="ForgeCode — An AI coding agent",
    )
    parser.add_argument(
        "--working-dir", "-d",
        type=Path,
        default=Path.cwd(),
        help="Working directory for the agent (default: current directory)",
    )
    parser.add_argument(
        "--allow-dangerous",
        action="store_true",
        help="Allow write and execute operations (equivalent to --dangerous-mode allow)",
    )
    parser.add_argument(
        "--dangerous-mode",
        choices=["deny", "ask", "allow"],
        nargs="?",
        const="allow",
        default=None,
        help="Dangerous operations policy: ask (default), deny, or allow. "
             "If flag is given without a value, defaults to allow.",
    )
    parser.add_argument(
        "--model",
        default=None,
        help="Model name (e.g. claude-sonnet-4-20250514, DeepSeek-V4-Flash). "
             "Can be set in config file.",
    )
    parser.add_argument(
        "--provider",
        default=None,
        choices=["openai", "anthropic"],
        help="API provider: 'openai' for OpenAI-compatible APIs (DeepSeek, OpenAI, etc.), "
             "'anthropic' for Anthropic API. Default: openai",
    )
    parser.add_argument(
        "--api-key",
        default=None,
        help="API key (can also set in config file or env var)",
    )
    parser.add_argument(
        "--base-url",
        default=None,
        help="API base URL (can also set in config file)",
    )
    parser.add_argument(
        "--show-thinking",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="Display model thinking/reasoning content (use --no-show-thinking to hide)",
    )
    parser.add_argument(
        "--thinking-budget",
        type=int,
        default=None,
        help="Token budget for model thinking/reasoning (default: 10000)",
    )
    parser.add_argument(
        "--prompt-file",
        type=Path,
        default=None,
        help="Read a prompt from a file, run one agent turn, and exit.",
    )

    # Session arguments
    session_group = parser.add_argument_group("session", "Session persistence options")
    session_group.add_argument(
        "--session",
        nargs="?",
        const="",
        default=None,
        metavar="ID",
        help="Start or resume a session. Without ID: create new session. "
             "With ID: resume the specified session.",
    )
    session_group.add_argument(
        "--resume",
        action="store_true",
        help="Resume the most recent session.",
    )
    session_group.add_argument(
        "--list-sessions",
        action="store_true",
        help="List all saved sessions and exit.",
    )
    session_group.add_argument(
        "--delete-session",
        metavar="ID",
        default=None,
        help="Delete the specified session and exit.",
    )
    session_group.add_argument(
        "--session-dir",
        type=Path,
        default=None,
        help="Session storage directory (default: ~/.forgecode/sessions/<hash>/sessions/).",
    )
    session_group.add_argument(
        "--no-session",
        action="store_true",
        help="Disable session creation, persistence, and loading for this run.",
    )
    parser.add_argument(
        "--plan",
        action="store_true",
        help="Start the session in plan mode for the initial task.",
    )

    args = parser.parse_args(argv)

    working_dir = args.working_dir.resolve()

    is_admin_cmd = args.list_sessions or (args.delete_session is not None)

    if not is_admin_cmd:
        from main.config import (
            has_effective_model_config,
            save_global_model_config,
        )
        if not has_effective_model_config(working_dir):
            if not (args.model and args.api_key and args.base_url):
                io = AgentIO()
                io.print_error(
                    "首次启动请使用 forge-code --model xxx --api-key xxx --base-url xxx"
                )
                sys.exit(1)
            else:
                save_global_model_config(
                    name=args.model,
                    provider=args.provider or "openai",
                    api_key=args.api_key,
                    base_url=args.base_url,
                )
        else:
            if args.model or args.api_key or args.base_url or args.provider:
                save_global_model_config(
                    name=args.model,
                    provider=args.provider,
                    api_key=args.api_key,
                    base_url=args.base_url,
                )

    # Resolve CLI dangerous mode: --dangerous-mode takes precedence over --allow-dangerous
    cli_dangerous_mode: DangerousMode | None = None
    if args.dangerous_mode:
        cli_dangerous_mode = DangerousMode(args.dangerous_mode)
    elif args.allow_dangerous:
        cli_dangerous_mode = DangerousMode.ALLOW

    config = AgentConfig.from_file_and_args(
        working_dir,
        cli_model=args.model,
        cli_provider=args.provider,
        cli_api_key=args.api_key,
        cli_base_url=args.base_url,
        cli_dangerous_mode=cli_dangerous_mode,
        cli_show_thinking=args.show_thinking,
        cli_thinking_budget=args.thinking_budget,
    )

    # Session setup must happen first (needed for session_id in DynamicPathConfig)
    from main.session import SessionManager, SessionData

    io = AgentIO()

    if args.no_session and (args.resume or args.session is not None):
        io.print_error("Cannot use --no-session when specifying --resume or --session.")
        sys.exit(1)

    session_dir = (args.session_dir or compute_session_dir(working_dir)).resolve()
    session_manager: SessionManager | None = None
    session: SessionData | None = None

    if args.list_sessions or args.delete_session or not args.no_session:
        session_manager = SessionManager(session_dir)

    if args.list_sessions:
        sessions = session_manager.list_sessions()  # type: ignore[union-attr]
        if not sessions:
            io.print_system("No saved sessions.")
        else:
            for meta in sessions:
                title = meta.title or "(untitled)"
                io.print_system(
                    f"  {meta.session_id}  {title}  "
                    f"(updated: {meta.updated_at})"
                )
        return

    if args.delete_session:
        deleted = session_manager.delete(args.delete_session)  # type: ignore[union-attr]
        if deleted:
            io.print_system(f"Deleted session: {args.delete_session}")
        else:
            io.print_error(f"Session not found: {args.delete_session}")
        return

    if not config.model_name or config.model_name == "placeholder":
        io.print_error(
            "Model name is not configured. Please specify a model name using "
            "the '--model' command-line argument, or set 'name' under the "
            "'[model]' section in your '.forgecode.toml' configuration file."
        )
        sys.exit(1)

    if not args.no_session:
        if args.resume:
            session = session_manager.load_latest_prefer_non_empty()  # type: ignore[union-attr]
            if session is None:
                io.print_error("No sessions to resume.")
                sys.exit(1)
            session.resumed = True
            io.print_system(
                f"Resuming session: {session.metadata.session_id} "
                f"({len(session.messages)} messages)"
            )
        elif args.session is not None:
            if args.session == "":
                # No ID given: create new session
                session = session_manager.create_session(  # type: ignore[union-attr]
                    model_name=config.model_name,
                    provider=config.provider,
                    working_directory=str(working_dir),
                )
                io.print_system(f"New session: {session.metadata.session_id}")
            else:
                # ID given: resume
                try:
                    session = session_manager.load(args.session)  # type: ignore[union-attr]
                    session.resumed = True
                    io.print_system(
                        f"Resuming session: {session.metadata.session_id} "
                        f"({len(session.messages)} messages)"
                    )
                except FileNotFoundError:
                    io.print_error(f"Session not found: {args.session}")
                    sys.exit(1)
                except ValueError as exc:
                    io.print_error(f"Corrupt session file: {exc}")
                    sys.exit(1)
        else:
            # Default behavior: always create a new session
            session = session_manager.create_session(  # type: ignore[union-attr]
                model_name=config.model_name,
                provider=config.provider,
                working_directory=str(working_dir),
            )
            io.print_system(f"New session: {session.metadata.session_id}")

    # Create DynamicPathConfig with session_id
    sid = session.metadata.session_id if session is not None else "no-session"
    from safety.permissions import DynamicPathConfig
    config = DynamicPathConfig(config, session_id=sid)

    # Inject approval callback for ExitPlanMode
    def _approval_callback(summary: str) -> str:
        io.print_system(f"\n--- Plan Ready for Review ---")
        io.print_system(summary)
        plan_file = config.plan_file
        if plan_file and plan_file.is_file():
            io.print_system(f"\nPlan file: {plan_file}\n")
            try:
                content = plan_file.read_text(encoding="utf-8")
                io.print_system(content)
            except OSError:
                io.print_system("(could not read plan file)")
        io.print_system("")
        choices = [
            ("1", "clear_execute", "Clear context and execute (auto-accept edits)"),
            ("2", "execute", "Execute with current context (auto-accept edits)"),
            ("3", "manual", "Execute with manual approval for each destructive action"),
            ("4", "continue", "Continue planning (provide feedback to revise)"),
        ]
        for key, _, desc in choices:
            io.print_system(f"  [{key}] {desc}")
        while True:
            response = io.prompt_user("\nChoose [1-4]: ")
            if response:
                for key, value, _ in choices:
                    if response.strip() == key:
                        return value
            io.print_error("Please choose 1, 2, 3, or 4")

    config._approval_callback = _approval_callback

    registry = ToolRegistry()
    register_builtin_tools(registry)

    command_policy = CommandPolicy(
        extra_safe_commands=frozenset(config.extra_safe_commands),
    )
    permissions = PermissionChecker(config, extra_rules=[make_command_rule(command_policy)])

    if config.provider == "anthropic":
        try:
            from main.runtime import AnthropicModelClient
            model_client = AnthropicModelClient(
                model=config.model_name,
                api_key=config.api_key,
                base_url=config.base_url,
                max_tokens=config.max_output_tokens,
                show_thinking=config.show_thinking,
                thinking_budget=config.thinking_budget,
                timeout=config.api_timeout,
                connect_timeout=config.api_connect_timeout,
            )
        except ImportError:
            io.print_error(
                "Provider SDK missing. Reinstall ForgeCode with the README pipx install command."
            )
            sys.exit(1)
    else:
        try:
            from main.runtime import OpenAIModelClient
            model_client = OpenAIModelClient(
                model=config.model_name,
                api_key=config.api_key,
                base_url=config.base_url,
                timeout=config.api_timeout,
                connect_timeout=config.api_connect_timeout,
            )
        except ImportError:
            io.print_error(
                "Provider SDK missing. Reinstall ForgeCode with the README pipx install command."
            )
            sys.exit(1)

    from main.runtime import AgentRuntime
    runtime = AgentRuntime(
        config, registry, permissions, io, model_client,
        session_manager=session_manager,
        session=session,
        plan_mode_startup=args.plan,
    )

    try:
        if args.prompt_file is not None:
            try:
                prompt = args.prompt_file.read_text(encoding="utf-8")
            except OSError as exc:
                io.print_error(f"Could not read prompt file: {exc}")
                sys.exit(1)
            if args.plan:
                runtime.enter_plan_mode(prompt)
            runtime.run_once(prompt)
        else:
            runtime.run()
    except KeyboardInterrupt:
        if session is not None and session_manager is not None:
            try:
                session_manager.save(session)
            except OSError:
                pass
        io.print_system("\nInterrupted. Goodbye.")


if __name__ == "__main__":
    main()
