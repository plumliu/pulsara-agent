"""Pulsara command line for the canonical conversation kernel."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
from pathlib import Path
import sys
from time import monotonic

from pulsara_agent import __version__
from pulsara_agent.capability import (
    bundled_skills_status,
    default_pulsara_home,
    reset_bundled_skill,
    sync_bundled_skills,
)
from pulsara_agent.capability.builtin_catalog import builtin_tool_catalog_entry
from pulsara_agent.conversation_kernel.capability import KernelCapabilityComposer
from pulsara_agent.conversation_kernel.host import KernelHostCore
from pulsara_agent.conversation_kernel.memory_tools import MEMORY_TOOL_NAMES
from pulsara_agent.conversation_kernel.subagent import SUBAGENT_TOOL_NAMES
from pulsara_agent.conversation_kernel.tool_runtime import DIRECT_KERNEL_TOOL_NAMES
from pulsara_agent.llm.models import ModelRole
from pulsara_agent.mcp_config import (
    McpServerConfig,
    StdioTransportConfig,
    StreamableHttpTransportConfig,
    load_mcp_server_configs,
    set_mcp_server_enabled,
    write_mcp_server_config,
)
from pulsara_agent.primitives.permission import (
    DEFAULT_PERMISSION_MODE,
    PermissionMode,
    parse_permission_mode,
)
from pulsara_agent.repl import ReplPrompt, build_repl_prompt
from pulsara_agent.tool_permission import mode_for_policy, preset_to_policy
from pulsara_agent.settings import PulsaraSettings, load_env_file
from pulsara_agent.terminal_client import (
    TerminalClientBinaryError,
    TerminalClientLaunchError,
)
from pulsara_agent.terminal_client.v3_launcher import launch_terminal_kernel_client
from pulsara_agent.workspace_identity import (
    HostWorkspaceInput,
    normalize_workspace_kind,
    resolve_workspace,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="pulsara")
    parser.add_argument("--version", action="store_true")
    commands = parser.add_subparsers(dest="command")

    host = commands.add_parser("host", help="Run the canonical conversation kernel.")
    host_commands = host.add_subparsers(dest="host_command")
    run = _add_host_common_args(host_commands.add_parser("run"))
    run.add_argument("prompt")
    for name in ("repl", "tui"):
        command = _add_host_common_args(host_commands.add_parser(name))
        resume = command.add_mutually_exclusive_group()
        resume.add_argument("--resume", default=None)
        resume.add_argument("--continue", dest="continue_session", action="store_true")
        command.add_argument("--list-sessions", action="store_true")
        if name == "tui":
            command.add_argument("--tui-binary", default=None)
            command.add_argument(
                "--clear-scrollback",
                action="store_true",
                help="Irreversibly erase display and scrollback before launch.",
            )
    inspect_host = _add_host_common_args(host_commands.add_parser("inspect"))
    inspect_host.set_defaults(permission_mode=PermissionMode.READ_ONLY.value)

    skills = commands.add_parser("skills")
    skill_commands = skills.add_subparsers(dest="skills_command")
    sync = _add_env_args(skill_commands.add_parser("sync-bundled"))
    sync.add_argument("--override-opt-out", action="store_true")
    _add_env_args(skill_commands.add_parser("status"))
    reset = _add_env_args(skill_commands.add_parser("reset"))
    reset.add_argument("name")

    mcp = commands.add_parser("mcp", help="Manage MCP server configuration.")
    mcp_commands = mcp.add_subparsers(dest="mcp_command")
    for name in ("list", "doctor"):
        command = _add_env_args(mcp_commands.add_parser(name))
        command.add_argument("--workspace", default=None)
        if name == "doctor":
            command.add_argument("server_id", nargs="?")
    add = _add_env_args(mcp_commands.add_parser("add"))
    add.add_argument("server_id")
    add.add_argument("--workspace", default=None)
    transport = add.add_mutually_exclusive_group(required=True)
    transport.add_argument("--stdio-command")
    transport.add_argument("--url")
    add.add_argument("--arg", action="append", default=[])
    add.add_argument("--allow-http-localhost", action="store_true")
    add.add_argument("--allow-private-network", action="store_true")
    add.add_argument("--proved-stateless", action="store_true")
    add.add_argument("--required", action="store_true")
    add.add_argument("--disabled", action="store_true")
    add.add_argument(
        "--scope",
        choices=("ROOT_ONLY", "ROOT_AND_SUBAGENTS"),
        default="ROOT_ONLY",
    )
    add.add_argument(
        "--effect",
        choices=("AUTO", "READ_ONLY", "EXTERNAL_EFFECT"),
        default="AUTO",
    )
    for name in ("remove", "enable", "disable", "reconnect"):
        command = _add_env_args(mcp_commands.add_parser(name))
        command.add_argument("server_id")
        command.add_argument("--workspace", default=None)

    database = commands.add_parser("db")
    database_commands = database.add_subparsers(dest="db_command")
    for name, deadline in (("status", 10.0), ("migrate", 300.0), ("verify", 30.0)):
        command = _add_database_args(database_commands.add_parser(name), deadline)
        if name == "verify":
            command.add_argument("--deep", action="store_true")

    config = _add_env_args(commands.add_parser("config-check"))
    config.set_defaults(prefix="PULSARA")
    return parser


def _add_env_args(parser: argparse.ArgumentParser) -> argparse.ArgumentParser:
    parser.add_argument("--env-file", default=None)
    parser.add_argument("--override-env", action="store_true")
    parser.add_argument("--prefix", default="PULSARA")
    return parser


def _add_database_args(
    parser: argparse.ArgumentParser, deadline: float
) -> argparse.ArgumentParser:
    _add_env_args(parser)
    parser.add_argument("--deadline-seconds", type=float, default=deadline)
    return parser


def _add_host_common_args(parser: argparse.ArgumentParser) -> argparse.ArgumentParser:
    _add_env_args(parser)
    parser.add_argument("--workspace", default=None)
    parser.add_argument("--workspace-kind", choices=("project", "transient"))
    parser.add_argument("--display-label", default=None)
    parser.add_argument("--memory-domain-id", default=None)
    parser.add_argument("--skill", action="append", default=[])
    parser.add_argument(
        "--trust-workspace-mcp",
        action="store_true",
        help=(
            "Trust this workspace's .pulsara/mcp.yaml for the current Host "
            "open. Disabled by default because workspace MCP may launch code "
            "or resolve secret references."
        ),
    )
    parser.add_argument(
        "--model-role",
        choices=(ModelRole.PRO.value, ModelRole.FLASH.value),
        default=ModelRole.PRO.value,
    )
    parser.add_argument(
        "--permission-mode",
        choices=tuple(item.value for item in PermissionMode),
        default=None,
    )
    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    if args.version:
        print(__version__)
        return
    if args.command == "host":
        try:
            if args.host_command == "run":
                result = asyncio.run(_kernel_host_run(args))
                _print_agent_run_result(result)
                return
            if args.host_command == "repl":
                asyncio.run(_kernel_host_repl(args))
                return
            if args.host_command == "tui":
                asyncio.run(_kernel_host_tui(args))
                return
            if args.host_command == "inspect":
                print(json.dumps(_host_inspect(args), indent=2, ensure_ascii=False))
                return
        except (
            ValueError,
            KeyError,
            TerminalClientBinaryError,
            TerminalClientLaunchError,
        ) as exc:
            parser.error(_public_error(exc))
        parser.error("host requires a subcommand")
    if args.command == "skills":
        _load_env_file_from_args(args)
        if args.skills_command == "sync-bundled":
            result = sync_bundled_skills(override_opt_out=args.override_opt_out)
        elif args.skills_command == "status":
            result = bundled_skills_status()
        elif args.skills_command == "reset":
            result = reset_bundled_skill(args.name)
        else:
            parser.error("skills requires a subcommand")
        print(json.dumps(result.to_dict(), indent=2, ensure_ascii=False))
        return
    if args.command == "mcp":
        try:
            result = asyncio.run(_mcp_command(args))
        except (ValueError, KeyError, RuntimeError) as exc:
            parser.error(_public_error(exc))
        print(json.dumps(result, indent=2, ensure_ascii=False))
        return
    if args.command == "config-check":
        try:
            settings = _settings_from_args(args)
        except ValueError as exc:
            parser.error(str(exc))
        print(json.dumps(settings.redacted_dict(), indent=2, ensure_ascii=False))
        return
    if args.command == "db":
        try:
            report = _database_command(args)
        except Exception as exc:
            from pulsara_agent.storage.migrations.errors import PostgresSchemaError

            if not isinstance(exc, PostgresSchemaError):
                raise
            print(
                json.dumps(
                    {
                        "status": "error",
                        "error_code": exc.code.value,
                        "detail": exc.detail,
                        "retryable": exc.retryable,
                    },
                    indent=2,
                    ensure_ascii=False,
                )
            )
            raise SystemExit(2) from exc
        print(json.dumps(report, indent=2, ensure_ascii=False, default=str))
        return
    parser.print_help()


async def _kernel_host_run(args) -> object:
    settings = _settings_from_args(args)
    _best_effort_sync_bundled_skills()
    core = KernelHostCore.production(settings=settings)
    session = None
    try:
        session = await core.open_session(
            _workspace_input_from_args(args),
            model_role=ModelRole(args.model_role),
            permission_policy=_permission_policy(args, intent="run"),
            active_skill_names=_active_skill_names_from_args(args),
        )
        return await session.run_turn(args.prompt)
    finally:
        if session is not None:
            await core.close_session(session.host_session_id, close_conversation=True)
        await core.shutdown()


async def _open_initial_session(core: KernelHostCore, args):
    common = {
        "model_role": ModelRole(args.model_role),
        "permission_policy": _permission_policy(args, intent="run"),
        "active_skill_names": _active_skill_names_from_args(args),
    }
    workspace = _workspace_input_from_args(args)
    if getattr(args, "resume", None):
        return await core.resume_session(
            args.resume, workspace_input=workspace, **common
        )
    if getattr(args, "continue_session", False):
        return await core.resume_most_recent_session(workspace, **common)
    return await core.open_session(workspace, **common)


async def _kernel_host_repl(args) -> None:
    settings = _settings_from_args(args)
    _best_effort_sync_bundled_skills()
    core = KernelHostCore.production(settings=settings)
    repl_prompt: ReplPrompt = build_repl_prompt(
        history_path=default_pulsara_home() / "repl_history"
    )
    try:
        workspace = _workspace_input_from_args(args)
        if args.list_sessions:
            summaries = await core.list_resumable_sessions(workspace_input=workspace)
            print(json.dumps([item.to_dict() for item in summaries], indent=2))
            return
        session = await _open_initial_session(core, args)
        print("Pulsara kernel REPL · :help · Ctrl-D detach · :close conversation")
        while True:
            try:
                prompt = await repl_prompt.read_line("pulsara> ")
            except KeyboardInterrupt:
                print("^C")
                continue
            except EOFError:
                print()
                return
            command = prompt.strip()
            if not command:
                continue
            if command in {"exit", "quit", ":q"}:
                return
            if command in {":help", ":h", ":?"}:
                print(":sessions · :resume ID · :continue · :stop · :close")
                continue
            if command == ":sessions":
                summaries = await core.list_resumable_sessions(
                    workspace_input=workspace
                )
                print(json.dumps([item.to_dict() for item in summaries], indent=2))
                continue
            if command == ":stop":
                print(
                    "Stopped."
                    if await session.stop_current_turn()
                    else "No active turn."
                )
                continue
            if command == ":close":
                await core.close_session(
                    session.host_session_id, close_conversation=True
                )
                print(f"Closed {session.session_id}")
                return
            if command.startswith(":resume "):
                next_session = await core.resume_session(
                    command.removeprefix(":resume ").strip(),
                    workspace_input=workspace,
                    model_role=ModelRole(args.model_role),
                    permission_policy=_permission_policy(args, intent="run"),
                    active_skill_names=_active_skill_names_from_args(args),
                )
                await core.close_session(
                    session.host_session_id, close_conversation=False
                )
                session = next_session
                continue
            if command == ":continue":
                next_session = await core.resume_most_recent_session(
                    workspace,
                    model_role=ModelRole(args.model_role),
                    permission_policy=_permission_policy(args, intent="run"),
                    active_skill_names=_active_skill_names_from_args(args),
                )
                if next_session.host_session_id != session.host_session_id:
                    await core.close_session(
                        session.host_session_id, close_conversation=False
                    )
                    session = next_session
                continue
            _print_agent_run_result(await session.run_turn(prompt))
    finally:
        await core.shutdown()


async def _kernel_host_tui(args) -> None:
    if not (sys.stdin.isatty() and sys.stdout.isatty()):
        raise ValueError("host tui requires an interactive terminal")
    settings = _settings_from_args(args)
    _best_effort_sync_bundled_skills()
    core = KernelHostCore.production(settings=settings)
    try:
        workspace = _workspace_input_from_args(args)
        if args.list_sessions:
            summaries = await core.list_resumable_sessions(workspace_input=workspace)
            print(json.dumps([item.to_dict() for item in summaries], indent=2))
            return
        session = await _open_initial_session(core, args)
        await launch_terminal_kernel_client(
            host_session=session,
            binary_path=args.tui_binary,
            clear_scrollback=args.clear_scrollback,
        )
    finally:
        await core.shutdown()


def _host_inspect(args) -> dict[str, object]:
    workspace = resolve_workspace(_workspace_input_from_args(args))
    permission = _permission_policy(args, intent="inspect")
    names = sorted(DIRECT_KERNEL_TOOL_NAMES | SUBAGENT_TOOL_NAMES | MEMORY_TOOL_NAMES)
    capability_composer = KernelCapabilityComposer(
        workspace_root=workspace.workspace_root,
        workspace_kind=workspace.workspace_kind,
        memory_domain=workspace.memory_domain,
        available_tool_names=frozenset(names),
        configured_active_skill_names=_active_skill_names_from_args(args),
    )
    capability = capability_composer.resolve_projection(
        user_input="", available_tool_names=frozenset(names)
    )
    enabled_mcp = [
        item.server_id
        for item in load_mcp_server_configs(
            workspace_root=workspace.workspace_root,
            trust_workspace_config=workspace.trust_workspace_mcp_config,
        )
        if item.enabled
    ]
    return {
        "inspect_kind": "canonical_kernel_static_workspace_capability.v3",
        "conversation_authority": "pulsara_v3",
        "protocol_major": 3,
        "workspace": {
            "workspace_kind": workspace.workspace_kind,
            "workspace_root": str(workspace.workspace_root),
            "workspace_key": workspace.workspace_key,
        },
        "tools": names,
        "descriptors": [
            builtin_tool_catalog_entry(name).descriptor.to_diagnostic_dict()
            for name in names
        ],
        "permissions": permission.to_dict(),
        "current_mode": (
            mode_for_policy(permission).value
            if mode_for_policy(permission) is not None
            else "custom"
        ),
        "skills": [item.name for item in capability.catalog_entries],
        "active_skills": [item.name for item in capability.active_injections],
        "mcp": {
            "composition_status": ("CONFIGURED" if enabled_mcp else "NOT_CONFIGURED"),
            "configured_enabled_servers": enabled_mcp,
        },
    }


async def _mcp_command(args: argparse.Namespace) -> dict[str, object]:
    _load_env_file_from_args(args)
    workspace_root = (
        Path(args.workspace).expanduser().resolve()
        if getattr(args, "workspace", None)
        else None
    )
    command = args.mcp_command
    if command == "list":
        configs = load_mcp_server_configs(
            workspace_root=workspace_root,
            trust_workspace_config=workspace_root is not None,
        )
        return {
            "status": "ok",
            "servers": [_mcp_config_public(item) for item in configs],
        }
    if command == "add":
        transport: dict[str, object]
        if args.stdio_command:
            transport = {
                "type": "stdio",
                "command": args.stdio_command,
                "args": args.arg,
            }
        else:
            if args.arg:
                raise ValueError("--arg is only valid with --stdio-command")
            transport = {
                "type": "streamable_http",
                "endpoint": args.url,
                "allow_http_localhost": args.allow_http_localhost,
                "network_policy": (
                    "ALLOW_PRIVATE"
                    if args.allow_private_network
                    else "PUBLIC_ONLY"
                ),
                "proved_stateless": args.proved_stateless,
            }
        entry = {
            "enabled": not args.disabled,
            "required": args.required,
            "transport": transport,
            "scope_policy": args.scope,
            "effect_policy": {"default_effect": args.effect},
            "catalog_refresh_interval_ms": 300_000,
        }
        path = write_mcp_server_config(
            server_id=args.server_id,
            entry=entry,
            workspace_root=workspace_root,
        )
        return {"status": "ok", "server_id": args.server_id, "path": str(path)}
    if command == "remove":
        path = write_mcp_server_config(
            server_id=args.server_id,
            entry=None,
            workspace_root=workspace_root,
        )
        return {"status": "ok", "server_id": args.server_id, "path": str(path)}
    if command in {"enable", "disable"}:
        path = set_mcp_server_enabled(
            server_id=args.server_id,
            enabled=command == "enable",
            workspace_root=workspace_root,
        )
        return {"status": "ok", "server_id": args.server_id, "path": str(path)}
    if command == "doctor":
        configs = load_mcp_server_configs(
            workspace_root=workspace_root,
            trust_workspace_config=workspace_root is not None,
        )
        if args.server_id is not None:
            configs = tuple(
                item for item in configs if item.server_id == args.server_id
            )
            if not configs:
                raise KeyError(args.server_id)
        from pulsara_agent.conversation_kernel.mcp.supervisor import (
            McpHostSupervisor,
        )

        results: list[dict[str, object]] = []
        for config in configs:
            if not config.enabled:
                results.append(
                    {"server_id": config.server_id, "status": "disabled"}
                )
                continue
            supervisor = McpHostSupervisor(
                session_id=f"mcp-doctor:{config.server_id}",
                workspace_root=workspace_root or Path.cwd(),
                configs=(config,),
            )
            try:
                await supervisor.start()
                state = await supervisor.wait_for_server_settlement(
                    config.server_id,
                    timeout_seconds=30,
                )
                runtime = supervisor.install_pending_at_safe_point()
                catalog = supervisor.catalog_snapshot()
                entry = catalog.servers[0]
                results.append(
                    {
                        "server_id": config.server_id,
                        "status": state.value.lower(),
                        "tool_count": entry.discovered_tool_count,
                        "exposed_tool_count": entry.exposed_tool_count,
                        "resource_count": entry.resource_count,
                        "resource_template_count": entry.resource_template_count,
                        "prompt_count": entry.prompt_count,
                    }
                )
                if runtime is not None:
                    runtime.release()
            except Exception as exc:
                results.append(
                    {
                        "server_id": config.server_id,
                        "status": "failed",
                        "failure_category": type(exc).__name__,
                    }
                )
            finally:
                await supervisor.aclose()
        return {"status": "ok", "servers": results}
    if command == "reconnect":
        raise RuntimeError(
            "mcp reconnect requires an active Host-owned supervisor; "
            "a standalone CLI process cannot control another Host"
        )
    raise ValueError("mcp requires a subcommand")


def _mcp_config_public(config: McpServerConfig) -> dict[str, object]:
    transport = config.transport
    if isinstance(transport, StdioTransportConfig):
        transport_kind = "stdio"
    elif isinstance(transport, StreamableHttpTransportConfig):
        transport_kind = "streamable_http"
    else:  # pragma: no cover - closed transport union
        raise TypeError("unknown MCP transport")
    return {
        "server_id": config.server_id,
        "display_name": config.display_name,
        "enabled": config.enabled,
        "required": config.required,
        "transport": transport_kind,
        "scope_policy": config.scope_policy.value,
        "effect_policy": config.effect_policy.default_effect.value,
        "semantic_config_fingerprint": config.semantic_config_fingerprint,
    }


def _database_command(args: argparse.Namespace) -> dict[str, object]:
    if args.db_command is None:
        raise ValueError("db requires a subcommand")
    if not 1 <= args.deadline_seconds <= 3600:
        raise ValueError("--deadline-seconds must be between 1 and 3600")
    _load_env_file_from_args(args)
    runtime_dsn = os.getenv(f"{args.prefix}_POSTGRES_DSN", "").strip()
    if not runtime_dsn:
        raise ValueError(f"{args.prefix}_POSTGRES_DSN is required")
    deadline = monotonic() + args.deadline_seconds
    from pulsara_agent.storage.migrations.registry import POSTGRES_MIGRATION_REGISTRY
    from pulsara_agent.storage.migrations.runner import (
        PostgresMigrationRunner,
        _read_identity_from_connection,
        read_migration_ledger,
    )
    from pulsara_agent.storage.migrations.verifier import classify_migration_history
    from pulsara_agent.storage.postgres_connection_provider import (
        PostgresRuntimeConnectionFactory,
    )

    factory = PostgresRuntimeConnectionFactory(runtime_dsn)
    if args.db_command == "status":
        with factory.connect(
            deadline_monotonic=deadline, autocommit=False
        ) as connection:
            with connection.transaction():
                connection.execute(
                    "SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY"
                )
                identity = _read_identity_from_connection(connection)
                rows = read_migration_ledger(connection)
        return {
            "status": classify_migration_history(rows).value,
            "database_name": identity.database_name,
            "runtime_role": identity.runtime_role,
            "migration_head_version": rows[-1].version if rows else None,
            "expected_head_version": POSTGRES_MIGRATION_REGISTRY.latest_version,
            "observed_registry_prefix_fingerprint": (
                rows[-1].registry_prefix_fingerprint if rows else None
            ),
            "expected_registry_prefix_fingerprint": (
                POSTGRES_MIGRATION_REGISTRY.registry_fingerprint
            ),
        }
    if args.db_command == "migrate":
        admin_dsn = os.getenv(f"{args.prefix}_POSTGRES_ADMIN_DSN", "").strip()
        if not admin_dsn:
            raise ValueError(f"{args.prefix}_POSTGRES_ADMIN_DSN is required")
        report = PostgresMigrationRunner(
            admin_dsn=admin_dsn,
            runtime_dsn=runtime_dsn,
        ).migrate(deadline_monotonic=deadline)
        return report.to_dict()
    if args.deep:
        return factory.verify_deep(deadline_monotonic=deadline).result.to_dict()
    bundle = factory.verify(deadline_monotonic=deadline)
    return {
        "status": "verified",
        "database_name": bundle.binding.database_name,
        "runtime_role": bundle.binding.runtime_role,
        "migration_head_version": bundle.binding.migration_head_version,
        "result_fingerprint": bundle.result.result_fingerprint,
    }


def _print_agent_run_result(result) -> None:
    if result.final_text:
        print(result.final_text)


def _settings_from_args(args) -> PulsaraSettings:
    if args.env_file:
        return PulsaraSettings.from_env_file(
            args.env_file, prefix=args.prefix, override=args.override_env
        )
    return PulsaraSettings.from_env(prefix=args.prefix)


def _load_env_file_from_args(args) -> None:
    if getattr(args, "env_file", None):
        load_env_file(args.env_file, override=bool(args.override_env))


def _workspace_input_from_args(args) -> HostWorkspaceInput:
    return HostWorkspaceInput(
        workspace_kind=normalize_workspace_kind(args.workspace_kind or "project"),
        workspace_root=Path(args.workspace or "."),
        display_label=args.display_label,
        memory_domain_id=args.memory_domain_id or "u_local",
        trust_workspace_mcp_config=bool(
            getattr(args, "trust_workspace_mcp", False)
        ),
    )


def _active_skill_names_from_args(args) -> frozenset[str]:
    return frozenset(item.strip() for item in args.skill if item.strip())


def _permission_policy(args, *, intent: str):
    raw = args.permission_mode or os.getenv(f"{args.prefix}_PERMISSION_MODE")
    if raw:
        return preset_to_policy(parse_permission_mode(raw.strip()))
    return preset_to_policy(
        PermissionMode.READ_ONLY if intent == "inspect" else DEFAULT_PERMISSION_MODE
    )


def _best_effort_sync_bundled_skills() -> None:
    try:
        result = sync_bundled_skills()
    except Exception as exc:
        print(f"pulsara: bundled skill sync failed: {exc}", file=sys.stderr)
        return
    changed = [
        item.name for item in result.items if item.action in {"installed", "updated"}
    ]
    if changed:
        print("pulsara: bundled skills synced: " + ", ".join(changed), file=sys.stderr)


def _public_error(exc: BaseException) -> str:
    if isinstance(exc, KeyError):
        return str(exc.args[0] if exc.args else "not found")
    return str(exc)


__all__ = ["build_parser", "main"]
