"""Pulsara command line for the canonical conversation kernel."""

from __future__ import annotations

import argparse
import asyncio
from contextlib import contextmanager
import json
import os
from pathlib import Path
import signal
import sys
from time import monotonic

from pulsara_agent import __version__
from pulsara_agent.capability import (
    ConflictingSkillCandidateIssue,
    EventLocalSkillCancellationProbe,
    InspectEffectiveSkillCatalogRequest,
    InstallLooseLocalSkillRequest,
    InvalidSkillCandidateIssue,
    LocalSkillInstallDisposition,
    LocalSkillInstallScope,
    LocalSkillManagementService,
    LocalSkillValidationDisposition,
    ShadowedSkillCandidateIssue,
    EffectiveSkillCatalogDisposition,
    ProducerUnavailableCause,
    ResolutionUnavailableCause,
    ValidateLocalSkillSourceRequest,
    skill_origin_label,
)
from pulsara_agent.capability.pulsara_home import (
    PulsaraHomeDisposition,
    require_pulsara_home,
    resolve_pulsara_home,
    resolve_user_home,
)
from pulsara_agent.conversation_kernel.host import KernelHostCore
from pulsara_agent.conversation_kernel.execution_watchdogs import (
    DEFAULT_KERNEL_WATCHDOG_POLICY,
)
from pulsara_agent.llm.model_catalog import ModelCatalogOwner, ModelsDevCatalogClient
from pulsara_agent.llm.model_connections import ModelCallBinding
from pulsara_agent.llm.model_target import (
    default_reasoning_selection,
    resolve_model_target_contract,
)
from pulsara_agent.llm.runtime import ModelRuntime
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
from pulsara_agent.tool_permission import preset_to_policy
from pulsara_agent.settings import (
    LocalSettings,
    LocalSettingsStore,
    LocalSettingsUnavailable,
)
from pulsara_agent.workspace_identity import (
    HostWorkspaceInput,
    normalize_workspace_kind,
    resolve_workspace,
)
from pulsara_agent.hooks.contracts import (
    HookSourceKind,
    PluginHookSourceIdentity,
)
from pulsara_agent.hooks.executor import HookSecretScrubSet
from pulsara_agent.hooks.source import LocalHookSourceProvider
from pulsara_agent.plugins.contracts import (
    CleanupUnavailablePluginInstallOutcome,
    ExternalProcessAcceptance,
    GcLocalPluginPackagesRequest,
    InspectLocalPluginsRequest,
    InstallLocalPluginRequest,
    PluginInspectionAbort,
    PluginInspectionDisposition,
    PluginInspectionOutcome,
    PluginScopeKind,
    EnabledPluginViewDisposition,
    NeverCancelPluginOperation,
    PluginDiagnostic,
    PluginDiagnosticCode,
    RemoveLocalPluginRequest,
    SetLocalPluginEnabledRequest,
    ValidateLocalPluginSourceRequest as ValidateLocalPluginPackageRequest,
)
from pulsara_agent.plugins.hook_adapter import compose_hook_definition_view
from pulsara_agent.plugins.management import (
    EventPluginCancellationPort,
    PluginManagementService,
)
from pulsara_agent.plugins.package_store import ManagedPluginStore
from pulsara_agent.plugins.skill_producer import PluginSkillDefinitionProducer
from pulsara_agent.plugins.view import EnabledPluginViewOwner, FrozenEnabledPluginView
from pulsara_agent.process_credential_boundary import (
    ProcessCredentialBoundary,
    ProcessCredentialScrubSet,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="pulsara")
    parser.add_argument("--version", action="store_true")
    commands = parser.add_subparsers(dest="command")

    app = _add_host_common_args(
        commands.add_parser("app", help="Run the complete local Pulsara Web app.")
    )
    app.add_argument("--port", type=int, default=0)
    app.add_argument(
        "--no-open",
        action="store_true",
        help="Print the local app URL without opening the default browser.",
    )
    app.add_argument(
        "--static-root",
        default=None,
        help=argparse.SUPPRESS,
    )

    host = commands.add_parser("host", help="Run the canonical conversation kernel.")
    host_commands = host.add_subparsers(dest="host_command")
    run = _add_host_common_args(host_commands.add_parser("run"))
    run.add_argument("prompt")
    repl = _add_host_common_args(host_commands.add_parser("repl"))
    resume = repl.add_mutually_exclusive_group()
    resume.add_argument("--resume", default=None)
    resume.add_argument("--continue", dest="continue_session", action="store_true")
    repl.add_argument("--list-sessions", action="store_true")
    skills = commands.add_parser("skills")
    skill_commands = skills.add_subparsers(dest="skills_command")
    validate = skill_commands.add_parser("validate")
    validate.add_argument("path")
    validate.add_argument("--json", action="store_true")
    install = skill_commands.add_parser("install")
    install.add_argument("path")
    install.add_argument("--scope", choices=("workspace", "user"), required=True)
    install.add_argument("--workspace", default=None)
    install.add_argument("--json", action="store_true")
    for name in ("list", "doctor"):
        command = skill_commands.add_parser(name)
        command.add_argument("--workspace", default=None)
        command.add_argument("--json", action="store_true")

    plugins = commands.add_parser(
        "plugins", help="Manage local Agent Plugins 1.0 packages."
    )
    plugin_commands = plugins.add_subparsers(dest="plugins_command")
    plugin_validate = plugin_commands.add_parser("validate")
    plugin_validate.add_argument("path")
    plugin_validate.add_argument("--json", action="store_true")
    plugin_add = _add_plugin_scope_args(plugin_commands.add_parser("add"))
    plugin_add.add_argument("--replace", action="store_true")
    plugin_add.add_argument("path")
    plugin_add.add_argument("--json", action="store_true")
    plugin_enable = _add_plugin_scope_args(plugin_commands.add_parser("enable"))
    plugin_enable.add_argument("--yes", action="store_true")
    plugin_enable.add_argument("plugin_id")
    plugin_enable.add_argument("--json", action="store_true")
    for name in ("disable", "remove"):
        command = _add_plugin_scope_args(plugin_commands.add_parser(name))
        command.add_argument("plugin_id")
        command.add_argument("--json", action="store_true")
    for name in ("list", "doctor", "gc"):
        command = plugin_commands.add_parser(name)
        command.add_argument("--workspace", default=None)
        command.add_argument("--json", action="store_true")

    mcp = commands.add_parser("mcp", help="Manage MCP server configuration.")
    mcp_commands = mcp.add_subparsers(dest="mcp_command")
    for name in ("list", "doctor"):
        command = mcp_commands.add_parser(name)
        command.add_argument("--workspace", default=None)
        if name == "doctor":
            command.add_argument("server_id", nargs="?")
    add = mcp_commands.add_parser("add")
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
        command = mcp_commands.add_parser(name)
        command.add_argument("server_id")
        command.add_argument("--workspace", default=None)

    hooks = commands.add_parser("hooks", help="Inspect and trust command Hooks.")
    hook_commands = hooks.add_subparsers(dest="hooks_command")
    for name in ("list", "inspect", "trust", "revoke", "enable", "disable", "doctor"):
        command = hook_commands.add_parser(name)
        command.add_argument("--workspace", default=None)
        command.add_argument(
            "--scope",
            choices=("user", "workspace"),
            required=name not in {"list", "doctor"},
        )
        command.add_argument(
            "--source",
            default=None,
            help="local or plugin:<plugin-id>; omitted list/doctor shows all",
        )
        if name == "trust":
            command.add_argument("--expected-definition-digest", required=True)

    database = commands.add_parser("db")
    database_commands = database.add_subparsers(dest="db_command")
    for name, deadline in (("status", 10.0), ("migrate", 300.0), ("verify", 30.0)):
        command = _add_database_args(database_commands.add_parser(name), deadline)
        if name == "verify":
            command.add_argument("--deep", action="store_true")

    commands.add_parser("config-check")
    return parser


def _add_plugin_scope_args(
    parser: argparse.ArgumentParser,
) -> argparse.ArgumentParser:
    parser.add_argument("--scope", choices=("user", "workspace"), required=True)
    parser.add_argument("--workspace", default=None)
    return parser


def _add_database_args(
    parser: argparse.ArgumentParser, deadline: float
) -> argparse.ArgumentParser:
    parser.add_argument("--deadline-seconds", type=float, default=deadline)
    return parser


def _add_host_common_args(parser: argparse.ArgumentParser) -> argparse.ArgumentParser:
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
    if args.command == "app":
        try:
            asyncio.run(_local_web_app(args))
            return
        except (ValueError, KeyError, RuntimeError, OSError) as exc:
            parser.error(_public_error(exc))
    if args.command == "host":
        try:
            if args.host_command == "run":
                result = asyncio.run(_kernel_host_run(args))
                _print_agent_run_result(result)
                return
            if args.host_command == "repl":
                asyncio.run(_kernel_host_repl(args))
                return
        except (ValueError, KeyError) as exc:
            parser.error(_public_error(exc))
        parser.error("host requires a subcommand")
    if args.command == "skills":
        try:
            output, exit_status = _skills_command(args)
        except _SkillCliUsageError as exc:
            parser.error(str(exc))
        except ValueError as exc:
            parser.error(_public_error(exc))
        print(output)
        if exit_status:
            raise SystemExit(exit_status)
        return
    if args.command == "plugins":
        credential_boundary = ProcessCredentialBoundary()
        try:
            output, exit_status = _plugins_command(
                args, credential_boundary=credential_boundary
            )
        except _PluginCliUsageError as exc:
            _plugin_cli_parser_error(parser, str(exc), credential_boundary)
        except ValueError as exc:
            _plugin_cli_parser_error(
                parser, _public_error(exc), credential_boundary
            )
        _plugin_cli_print(output, credential_boundary)
        if exit_status:
            raise SystemExit(exit_status)
        return
    if args.command == "mcp":
        try:
            result = asyncio.run(_mcp_command(args))
        except (ValueError, KeyError, RuntimeError) as exc:
            parser.error(_public_error(exc))
        print(json.dumps(result, indent=2, ensure_ascii=False))
        return
    if args.command == "hooks":
        try:
            result = _hooks_command(args)
        except (ValueError, KeyError, RuntimeError) as exc:
            parser.error(_public_error(exc))
        scrub = HookSecretScrubSet.capture()
        safe = scrub.scrub_json(result)
        print(json.dumps(safe, indent=2, ensure_ascii=False))
        return
    if args.command == "config-check":
        try:
            report = asyncio.run(_config_check())
        except (ValueError, RuntimeError) as exc:
            parser.error(str(exc))
        print(json.dumps(report, indent=2, ensure_ascii=False))
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
    _settings, catalog, runtime = _runtime_services()
    await catalog.refresh()
    core = KernelHostCore.production(model_runtime=runtime)
    session = None
    try:
        session = await core.open_session(
            _workspace_input_from_args(args),
            permission_policy=_permission_policy(args),
            active_skill_names=_active_skill_names_from_args(args),
        )
        return await session.run_turn(args.prompt)
    finally:
        if session is not None:
            await core.close_session(session.host_session_id, close_conversation=True)
        await core.shutdown()


async def _local_web_app(args) -> None:
    from pulsara_agent.web_app import (
        LocalWebApplication,
        run_local_web_application,
    )

    settings, catalog, runtime = _runtime_services()
    application = LocalWebApplication(
        settings=settings,
        catalog=catalog,
        model_runtime=runtime,
        workspace_input=_workspace_input_from_args(args),
        permission_policy=_permission_policy(args),
        active_skill_names=_active_skill_names_from_args(args),
        port=args.port,
        static_root=(
            None if args.static_root is None else Path(args.static_root)
        ),
    )
    await run_local_web_application(
        application,
        open_browser=not args.no_open,
    )


async def _open_initial_session(core: KernelHostCore, args):
    common = {
        "permission_policy": _permission_policy(args),
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
    _settings, catalog, runtime = _runtime_services()
    await catalog.refresh()
    core = KernelHostCore.production(model_runtime=runtime)
    repl_prompt: ReplPrompt = build_repl_prompt(
        history_path=require_pulsara_home() / "repl_history"
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
                    permission_policy=_permission_policy(args),
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
                    permission_policy=_permission_policy(args),
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


class _SkillCliUsageError(ValueError):
    pass


def _skills_command(args: argparse.Namespace) -> tuple[str, int]:
    command = args.skills_command
    if command is None:
        raise _SkillCliUsageError("skills requires a subcommand")

    service = LocalSkillManagementService()
    if command == "validate":
        result = service.validate_local_skill_source(
            ValidateLocalSkillSourceRequest(_resolved_local_source(args.path))
        )
        payload = _skill_validation_payload(result)
        rendered = (
            json.dumps(payload, indent=2, ensure_ascii=False)
            if args.json
            else _skill_validation_text(payload)
        )
        status = {
            LocalSkillValidationDisposition.VALID: 0,
            LocalSkillValidationDisposition.INVALID: 1,
            LocalSkillValidationDisposition.UNAVAILABLE: 2,
        }[result.disposition]
        return rendered, status

    if command == "install":
        scope = LocalSkillInstallScope(args.scope)
        if scope is LocalSkillInstallScope.USER and args.workspace is not None:
            raise _SkillCliUsageError("--workspace is not valid with --scope user")
        workspace = (
            _resolved_skill_workspace(args.workspace)
            if scope is LocalSkillInstallScope.WORKSPACE
            else None
        )
        probe = EventLocalSkillCancellationProbe()
        with _bridge_skill_sigint(probe):
            result = service.install_loose_local_skill(
                InstallLooseLocalSkillRequest(
                    source_path=_resolved_local_source(args.path),
                    scope=scope,
                    workspace_root=workspace,
                ),
                cancellation=probe,
            )
        payload = _skill_install_payload(result)
        rendered = (
            json.dumps(payload, indent=2, ensure_ascii=False)
            if args.json
            else _skill_install_text(payload)
        )
        status = _SKILL_INSTALL_EXIT_STATUS[result.disposition]
        return rendered, status

    if command in {"list", "doctor"}:
        workspace = _resolved_skill_workspace(args.workspace)
        plugin_definitions, plugin_view = _cli_plugin_skill_definitions(workspace)
        try:
            inspection = service.inspect_effective_skill_catalog(
                InspectEffectiveSkillCatalogRequest(workspace, plugin_definitions)
            )
            payload = _skill_inspection_payload(
                inspection, doctor=command == "doctor"
            )
            rendered = (
                json.dumps(payload, indent=2, ensure_ascii=False)
                if args.json
                else _skill_inspection_text(payload, doctor=command == "doctor")
            )
            status = (
                0
                if inspection.disposition
                is EffectiveSkillCatalogDisposition.COMPLETE
                else 2
            )
            return rendered, status
        finally:
            plugin_view.close()
    raise _SkillCliUsageError("unknown skills command")


def _resolved_skill_workspace(raw: str | None) -> Path:
    root = Path(raw) if raw is not None else Path.cwd()
    return resolve_workspace(
        HostWorkspaceInput(workspace_kind="project", workspace_root=root)
    ).workspace_root


def _cli_plugin_skill_definitions(
    workspace: Path,
):
    boundary = ProcessCredentialBoundary()
    user_home = resolve_user_home()
    home = resolve_pulsara_home(user_home_resolution=user_home)
    if home.disposition is not PulsaraHomeDisposition.RESOLVED:
        view = FrozenEnabledPluginView(
            EnabledPluginViewDisposition.UNAVAILABLE,
            diagnostics=(
                PluginDiagnostic(
                    PluginDiagnosticCode.HOME_CONFIGURATION_INVALID,
                    "Plugin home configuration is invalid",
                ),
            ),
        )
    else:
        view = EnabledPluginViewOwner(
            store=ManagedPluginStore(
                pulsara_home=home, credential_boundary=boundary
            ),
            credential_boundary=boundary,
        ).observe(
            workspace_root=workspace,
            deadline_monotonic=float("inf"),
            cancellation=NeverCancelPluginOperation(),
        )
    return PluginSkillDefinitionProducer().observe(view), view


def _resolved_local_source(raw: str) -> Path:
    source = Path(raw).expanduser()
    if not source.is_absolute():
        source = Path.cwd() / source
    return Path(os.path.normpath(os.fspath(source)))


@contextmanager
def _bridge_skill_sigint(probe: EventLocalSkillCancellationProbe):
    previous = signal.getsignal(signal.SIGINT)

    def request_cancel(_signum, _frame) -> None:
        probe.cancel()

    signal.signal(signal.SIGINT, request_cancel)
    try:
        yield
    finally:
        signal.signal(signal.SIGINT, previous)


def _skill_validation_payload(result) -> dict[str, object]:
    payload: dict[str, object] = {
        "operation": "validate_local_skill_source",
        "disposition": result.disposition.value,
        "source_path": str(result.source_path),
    }
    if result.parsed is not None:
        payload["manifest"] = {
            "name": result.parsed.name,
            "description": result.parsed.description,
            "license": result.parsed.license,
            "compatibility": result.parsed.compatibility,
            "metadata": dict(result.parsed.metadata),
            "body": result.parsed.body,
            "raw_document_digest": result.parsed.raw_document_digest,
            "manifest_semantic_fingerprint": (
                result.parsed.manifest_semantic_fingerprint
            ),
            "authoring_diagnostic_codes": [
                item.value for item in result.parsed.authoring_diagnostic_codes
            ],
        }
    if result.diagnostics:
        payload["diagnostics"] = [item.to_dict() for item in result.diagnostics]
    if result.unavailable_reason is not None:
        payload["unavailable_reason"] = result.unavailable_reason.value
    return payload


def _skill_validation_text(payload: dict[str, object]) -> str:
    lines = [
        f"Validation: {payload['disposition']}",
        f"Source: {payload['source_path']}",
    ]
    manifest = payload.get("manifest")
    if isinstance(manifest, dict):
        lines.append(f"Skill: {manifest['name']} — {manifest['description']}")
    for diagnostic in payload.get("diagnostics", []):
        if isinstance(diagnostic, dict):
            lines.append(f"- {diagnostic['code']}: {diagnostic['message']}")
    if "unavailable_reason" in payload:
        lines.append(f"Reason: {payload['unavailable_reason']}")
    return "\n".join(lines)


def _skill_install_payload(result) -> dict[str, object]:
    payload: dict[str, object] = {
        "operation": "install_loose_local_skill",
        "disposition": result.disposition.value,
        "source_path": str(result.source_path),
    }
    if result.destination_path is not None:
        payload["destination_path"] = str(result.destination_path)
        payload["discovery"] = "next_legal_provider_safe_point"
    if result.diagnostics:
        payload["diagnostics"] = [item.to_dict() for item in result.diagnostics]
    if result.target_configuration_reason is not None:
        payload["target_configuration_reason"] = (
            result.target_configuration_reason.value
        )
    if result.publish_unavailable_reason is not None:
        payload["publish_unavailable_reason"] = result.publish_unavailable_reason.value
    if result.entry_path is not None:
        payload["entry_path"] = result.entry_path.as_posix()
    if result.prior_disposition is not None:
        payload["prior_disposition"] = result.prior_disposition.value
    if result.attempted_staging_path is not None:
        payload["attempted_staging_path"] = str(result.attempted_staging_path)
    if result.cleanup_location_status is not None:
        payload["cleanup_location_status"] = result.cleanup_location_status.value
    return payload


def _skill_install_text(payload: dict[str, object]) -> str:
    lines = [
        f"Installation: {payload['disposition']}",
        f"Source: {payload['source_path']}",
    ]
    if "destination_path" in payload:
        lines.extend(
            (
                f"Destination: {payload['destination_path']}",
                "Discovery: the next legal provider safe point.",
            )
        )
    for name in (
        "target_configuration_reason",
        "publish_unavailable_reason",
        "entry_path",
        "prior_disposition",
        "attempted_staging_path",
        "cleanup_location_status",
    ):
        if name in payload:
            lines.append(f"{name}: {payload[name]}")
    for diagnostic in payload.get("diagnostics", []):
        if isinstance(diagnostic, dict):
            lines.append(f"- {diagnostic['code']}: {diagnostic['message']}")
    return "\n".join(lines)


def _skill_inspection_payload(inspection, *, doctor: bool) -> dict[str, object]:
    payload: dict[str, object] = {
        "operation": "inspect_effective_skill_catalog",
        "projection": "doctor" if doctor else "list",
        "disposition": inspection.disposition.value,
        "skills": [
            {
                "name": item.name,
                "description": item.description,
                "location": item.location,
                "source": item.source.value,
                "origin_label": item.origin_label,
            }
            for item in inspection.winners
        ],
    }
    if doctor:
        payload["roots"] = [
            {
                "root_kind": item.root_kind.value,
                "path": str(item.path),
                "location_prefix": item.location_prefix,
                "precedence_ordinal": item.precedence_ordinal,
            }
            for item in inspection.root_policy.roots
        ]
    if inspection.disposition is EffectiveSkillCatalogDisposition.UNAVAILABLE:
        causes: list[dict[str, object]] = []
        for cause in inspection.unavailable_causes:
            if isinstance(cause, ProducerUnavailableCause):
                causes.append(
                    {
                        "kind": "PRODUCER",
                        "producer_kind": cause.producer_kind.value,
                        "reason": cause.reason.value,
                        "diagnostics": [item.to_dict() for item in cause.diagnostics],
                    }
                )
            elif isinstance(cause, ResolutionUnavailableCause):
                causes.append(
                    {
                        "kind": "RESOLUTION",
                        "reason": cause.reason.value,
                        "diagnostics": [cause.diagnostic.to_dict()],
                    }
                )
            else:  # pragma: no cover - closed cause union
                raise TypeError("Skill catalog unavailable cause is open")
        payload["unavailable_causes"] = causes
    if doctor and inspection.disposition is EffectiveSkillCatalogDisposition.COMPLETE:
        issues: list[dict[str, object]] = []
        for item in inspection.candidate_issues:
            if isinstance(item, InvalidSkillCandidateIssue):
                issues.append(
                    {
                        "kind": item.kind.value,
                        "path": str(item.path),
                        "origin_label": skill_origin_label(item.origin),
                        "declared_name": item.declared_name,
                        "diagnostics": [
                            diagnostic.to_dict() for diagnostic in item.diagnostics
                        ],
                    }
                )
            elif isinstance(item, ShadowedSkillCandidateIssue):
                issues.append(
                    {
                        "kind": item.kind.value,
                        "path": str(item.path),
                        "origin_label": skill_origin_label(item.origin),
                        "name": item.name,
                        "winner_origin_label": skill_origin_label(
                            item.winner_origin
                        ),
                        "winner_path": str(item.winner_path),
                        "diagnostic_codes": [
                            code.value for code in item.diagnostic_codes
                        ],
                    }
                )
            elif isinstance(item, ConflictingSkillCandidateIssue):
                issues.append(
                    {
                        "kind": item.kind.value,
                        "name": item.name,
                        "tier": item.tier.value,
                        "candidates": [
                            {
                                "path": str(candidate.path),
                                "origin_label": skill_origin_label(candidate.origin),
                            }
                            for candidate in item.candidates
                        ],
                        "diagnostic_codes": [item.diagnostic_code.value],
                    }
                )
            else:  # pragma: no cover - closed issue union
                raise TypeError("Skill candidate issue is open")
        payload["candidate_issues"] = issues
    return payload


def _skill_inspection_text(payload: dict[str, object], *, doctor: bool) -> str:
    lines = [f"Catalog: {payload['disposition']}"]
    skills = payload.get("skills", [])
    if (
        not skills
        and payload["disposition"]
        == EffectiveSkillCatalogDisposition.COMPLETE.value
    ):
        lines.append("No effective Skills.")
    for item in skills:
        if isinstance(item, dict):
            lines.append(
                f"- {item['name']}: {item['description']} "
                f"({item['location']}; {item['origin_label']})"
            )
    for cause in payload.get("unavailable_causes", []):
        if isinstance(cause, dict):
            lines.append(f"Unavailable: {cause['reason']}")
    if doctor:
        for root in payload.get("roots", []):
            if isinstance(root, dict):
                lines.append(
                    f"Root {root['precedence_ordinal']}: "
                    f"{root['root_kind']} {root['path']}"
                )
        for issue in payload.get("candidate_issues", []):
            if isinstance(issue, dict):
                location = issue.get("path", issue.get("name", "unknown"))
                lines.append(f"Issue {issue['kind']}: {location}")
                if issue["kind"] == "SHADOWED":
                    lines.append(
                        "  winner: "
                        f"{issue['winner_origin_label']} {issue['winner_path']}"
                    )
                    if issue["origin_label"] == "bundled:pulsara-agent":
                        lines.append(
                            "  bundled fallback is shadowed by this loose definition; "
                            "deleting the loose winner allows fallback at the next "
                            "complete safe point"
                        )
                if issue["kind"] == "CONFLICTING":
                    for candidate in issue.get("candidates", []):
                        if isinstance(candidate, dict):
                            lines.append(
                                "  candidate: "
                                f"{candidate['origin_label']} {candidate['path']}"
                            )
                for diagnostic in issue.get("diagnostics", []):
                    if isinstance(diagnostic, dict):
                        lines.append(
                            f"  - {diagnostic['code']}: {diagnostic['message']}"
                        )
        for cause in payload.get("unavailable_causes", []):
            if isinstance(cause, dict):
                for diagnostic in cause.get("diagnostics", []):
                    if isinstance(diagnostic, dict):
                        lines.append(
                            f"- {diagnostic['code']}: {diagnostic['message']}"
                        )
    return "\n".join(lines)


_SKILL_INSTALL_EXIT_STATUS = {
    LocalSkillInstallDisposition.INSTALLED: 0,
    LocalSkillInstallDisposition.SOURCE_INVALID: 1,
    LocalSkillInstallDisposition.UNSUPPORTED_ENTRY: 1,
    LocalSkillInstallDisposition.DESTINATION_EXISTS: 1,
    LocalSkillInstallDisposition.CANCELLED: 1,
    LocalSkillInstallDisposition.SOURCE_UNAVAILABLE: 2,
    LocalSkillInstallDisposition.SOURCE_RACED: 2,
    LocalSkillInstallDisposition.TARGET_CONFIGURATION_UNAVAILABLE: 2,
    LocalSkillInstallDisposition.STAGING_UNAVAILABLE: 2,
    LocalSkillInstallDisposition.PUBLISH_UNAVAILABLE: 2,
    LocalSkillInstallDisposition.CLEANUP_UNAVAILABLE: 2,
}


class _PluginCliUsageError(ValueError):
    pass


def _plugins_command(
    args: argparse.Namespace,
    *,
    credential_boundary: ProcessCredentialBoundary | None = None,
) -> tuple[str, int]:
    command = args.plugins_command
    if command is None:
        raise _PluginCliUsageError("plugins requires a subcommand")
    boundary = credential_boundary or ProcessCredentialBoundary()
    service = PluginManagementService(credential_boundary=boundary)
    cancellation = EventPluginCancellationPort()
    deadline = float("inf")

    if command == "validate":
        with _bridge_skill_sigint(cancellation):
            result = service.validate_local_plugin_source(
                ValidateLocalPluginPackageRequest(
                    _resolved_local_source(args.path), deadline, cancellation
                )
            )
        payload = _plugin_validation_payload(result)
        return _render_plugin_payload(payload, args.json), _plugin_exit_status(
            result.disposition
        )

    if command == "add":
        scope, workspace = _plugin_scope_and_workspace(args)
        with _bridge_skill_sigint(cancellation):
            result = service.install_local_plugin(
                InstallLocalPluginRequest(
                    _resolved_local_source(args.path),
                    scope,
                    deadline,
                    workspace,
                    args.replace,
                    cancellation,
                )
            )
        payload = _plugin_install_payload(result)
        return _render_plugin_payload(payload, args.json), _plugin_exit_status(
            result.disposition
        )

    if command in {"enable", "disable"}:
        scope, workspace = _plugin_scope_and_workspace(args)
        with _bridge_skill_sigint(cancellation):
            inspection = service.inspect_local_plugins(
                InspectLocalPluginsRequest(deadline, workspace, cancellation)
            )
        if not isinstance(inspection, PluginInspectionOutcome) or (
            inspection.disposition is not PluginInspectionDisposition.COMPLETE
        ):
            payload = _plugin_inspection_payload(
                inspection, doctor=True, projection="enable-preflight"
            )
            return _render_plugin_payload(payload, args.json), 2
        try:
            target = next(
                (
                    item
                    for item in inspection.instances
                    if item.identity.scope is scope
                    and item.identity.plugin_id == args.plugin_id
                ),
                None,
            )
            if target is None:
                payload = {
                    "operation": "set_local_plugin_enabled",
                    "disposition": "NOT_FOUND",
                    "scope": scope.value,
                    "plugin_id": args.plugin_id,
                }
                return _render_plugin_payload(payload, args.json), 1
            review = _plugin_instance_payload(target, include_diagnostics=True)
            if command == "enable" and not args.yes:
                _plugin_cli_stderr(_plugin_enable_review_text(review), boundary)
                _plugin_cli_stderr(
                    "Enable this exact package and accept future local process/HTTP "
                    "startup? [y/N] ",
                    boundary,
                    end="",
                )
                try:
                    accepted = input()
                except EOFError:
                    accepted = ""
                if accepted.strip().lower() not in {"y", "yes"}:
                    payload = {
                        "operation": "set_local_plugin_enabled",
                        "disposition": "DECLINED",
                        "reviewed_package": review,
                    }
                    return _render_plugin_payload(payload, args.json), 1
            with _bridge_skill_sigint(cancellation):
                result = service.set_local_plugin_enabled(
                    SetLocalPluginEnabledRequest(
                        scope,
                        args.plugin_id,
                        command == "enable",
                        target.package_install_id,
                        deadline,
                        workspace,
                        (
                            ExternalProcessAcceptance.ACCEPTED
                            if command == "enable"
                            else None
                        ),
                        cancellation,
                    )
                )
            payload = _plugin_enablement_payload(result, reviewed=review)
            return _render_plugin_payload(payload, args.json), _plugin_exit_status(
                result.disposition
            )
        finally:
            _close_inspection_anchors(inspection)

    if command == "remove":
        scope, workspace = _plugin_scope_and_workspace(args)
        with _bridge_skill_sigint(cancellation):
            result = service.remove_local_plugin(
                RemoveLocalPluginRequest(
                    scope, args.plugin_id, deadline, workspace, cancellation
                )
            )
        payload = _plugin_removal_payload(result)
        return _render_plugin_payload(payload, args.json), _plugin_exit_status(
            result.disposition
        )

    workspace = _resolved_skill_workspace(args.workspace)
    if command in {"list", "doctor"}:
        with _bridge_skill_sigint(cancellation):
            inspection = service.inspect_local_plugins(
                InspectLocalPluginsRequest(deadline, workspace, cancellation)
            )
        try:
            payload = _plugin_inspection_payload(
                inspection, doctor=command == "doctor", projection=command
            )
            status = (
                0
                if isinstance(inspection, PluginInspectionOutcome)
                and inspection.disposition is PluginInspectionDisposition.COMPLETE
                else 2
            )
            return _render_plugin_payload(payload, args.json), status
        finally:
            if isinstance(inspection, PluginInspectionOutcome):
                _close_inspection_anchors(inspection)

    if command == "gc":
        with _bridge_skill_sigint(cancellation):
            result = service.gc_local_plugin_packages(
                GcLocalPluginPackagesRequest(deadline, workspace, cancellation)
            )
        payload = _plugin_gc_payload(result)
        return _render_plugin_payload(payload, args.json), _plugin_exit_status(
            result.disposition
        )
    raise _PluginCliUsageError("unknown plugins command")


def _plugin_scope_and_workspace(
    args: argparse.Namespace,
) -> tuple[PluginScopeKind, Path | None]:
    scope = PluginScopeKind(args.scope.upper())
    if scope is PluginScopeKind.USER:
        if args.workspace is not None:
            raise _PluginCliUsageError(
                "--workspace is not valid with --scope user"
            )
        return scope, None
    return scope, _resolved_skill_workspace(args.workspace)


def _plugin_validation_payload(result) -> dict[str, object]:
    payload: dict[str, object] = {
        "operation": "validate_local_plugin_source",
        "disposition": result.disposition.value,
        "source_path": str(result.source_path),
    }
    if hasattr(result, "summary"):
        payload["summary"] = _plugin_summary_payload(result.summary)
    diagnostics = getattr(result, "diagnostics", ())
    if diagnostics:
        payload["diagnostics"] = [_diagnostic_payload(item) for item in diagnostics]
    return payload


def _plugin_install_payload(result) -> dict[str, object]:
    if isinstance(result, CleanupUnavailablePluginInstallOutcome):
        return {
            "operation": "install_local_plugin",
            "disposition": result.disposition.value,
            "prior": _plugin_install_payload(result.prior),
            "attempted_path": str(result.attempted_path),
            "location_status": result.location_status.value,
            "diagnostic": _diagnostic_payload(result.diagnostic),
        }
    payload: dict[str, object] = {
        "operation": "install_local_plugin",
        "disposition": result.disposition.value,
    }
    identity = getattr(result, "identity", None)
    if identity is not None:
        payload["identity"] = _plugin_identity_payload(identity)
    package_id = getattr(result, "package_install_id", None)
    if package_id is not None:
        payload["package_install_id"] = package_id
    if hasattr(result, "enabled"):
        payload["enabled"] = result.enabled
    if hasattr(result, "summary"):
        payload["summary"] = _plugin_summary_payload(result.summary)
    for name in (
        "attempted_package_install_id",
        "intended_enabled",
        "last_known_cut",
    ):
        value = getattr(result, name, None)
        if value is not None:
            payload[name] = value
    diagnostics = getattr(result, "diagnostics", ())
    if diagnostics:
        payload["diagnostics"] = [_diagnostic_payload(item) for item in diagnostics]
    return payload


def _plugin_enablement_payload(result, *, reviewed: dict[str, object]):
    payload: dict[str, object] = {
        "operation": "set_local_plugin_enabled",
        "disposition": result.disposition.value,
        "identity": _plugin_identity_payload(result.identity),
        "reviewed_package": reviewed,
    }
    for name in (
        "package_install_id",
        "enabled",
        "desired_enabled",
        "expected_package_install_id",
        "observed_package_install_id",
        "last_known_cut",
    ):
        value = getattr(result, name, None)
        if value is not None:
            payload[name] = value
    diagnostics = getattr(result, "diagnostics", ())
    if diagnostics:
        payload["diagnostics"] = [_diagnostic_payload(item) for item in diagnostics]
    return payload


def _plugin_removal_payload(result) -> dict[str, object]:
    payload: dict[str, object] = {
        "operation": "remove_local_plugin",
        "disposition": result.disposition.value,
        "identity": _plugin_identity_payload(result.identity),
    }
    for name in ("prior_package_install_id", "last_known_cut"):
        value = getattr(result, name, None)
        if value is not None:
            payload[name] = value
    diagnostics = getattr(result, "diagnostics", ())
    if diagnostics:
        payload["diagnostics"] = [_diagnostic_payload(item) for item in diagnostics]
    return payload


def _plugin_inspection_payload(
    inspection, *, doctor: bool, projection: str
) -> dict[str, object]:
    if isinstance(inspection, PluginInspectionAbort):
        return {
            "operation": "inspect_local_plugins",
            "projection": projection,
            "disposition": inspection.reason.value,
        }
    payload: dict[str, object] = {
        "operation": "inspect_local_plugins",
        "projection": projection,
        "disposition": inspection.disposition.value,
        "instances": [
            (
                _plugin_instance_payload(item, include_diagnostics=True)
                if doctor
                else _plugin_list_instance_payload(item)
            )
            for item in inspection.instances
        ],
        "effective_composition": {
            "skills": inspection.skill_composition_disposition.value,
            "mcp": inspection.mcp_composition_disposition.value,
            "hooks": inspection.hook_composition_disposition.value,
        },
    }
    if doctor:
        payload["versions"] = [
            {
                "identity": _plugin_identity_payload(item.identity),
                "package_install_id": item.package_install_id,
                "package_root": str(item.package_root),
                "referenced": item.referenced,
                "in_use": item.in_use,
            }
            for item in inspection.versions
        ]
    if inspection.diagnostics:
        payload["diagnostics"] = [
            _diagnostic_payload(item) for item in inspection.diagnostics
        ]
    return payload


def _plugin_instance_payload(
    item, *, include_diagnostics: bool
) -> dict[str, object]:
    payload: dict[str, object] = {
        "identity": _plugin_identity_payload(item.identity),
        "package_install_id": item.package_install_id,
        "enabled": item.enabled,
        "package_root": str(item.package_root),
        "data_root": str(item.data_root),
        "package_in_use": item.package_in_use,
        "summary": _plugin_summary_payload(item.summary),
        "effective_skill_names": list(item.effective_skill_names),
        "effective_mcp_server_ids": list(item.effective_mcp_server_ids),
        "effective_hook": item.effective_hook,
        "effective_hook_definition_count": item.effective_hook_definition_count,
        "effective_hook_trust_disposition": (
            None
            if item.effective_hook_trust_disposition is None
            else item.effective_hook_trust_disposition.value
        ),
    }
    if include_diagnostics and item.diagnostics:
        payload["diagnostics"] = [
            _diagnostic_payload(value) for value in item.diagnostics
        ]
    return payload


def _plugin_list_instance_payload(item) -> dict[str, object]:
    """The intentionally narrow current/effective ``list`` projection."""

    return {
        "identity": _plugin_identity_payload(item.identity),
        "package_install_id": item.package_install_id,
        "enabled": item.enabled,
        "effective_skill_names": list(item.effective_skill_names),
        "effective_mcp_server_ids": list(item.effective_mcp_server_ids),
        "effective_hook": item.effective_hook,
        "effective_hook_definition_count": item.effective_hook_definition_count,
        "effective_hook_trust_disposition": (
            None
            if item.effective_hook_trust_disposition is None
            else item.effective_hook_trust_disposition.value
        ),
    }


def _plugin_summary_payload(summary) -> dict[str, object]:
    manifest = summary.manifest
    return {
        "manifest": {
            "schema": manifest.schema,
            "name": manifest.name,
            "version": manifest.version,
            "description": manifest.description,
            "author": (
                None
                if manifest.author is None
                else {
                    "name": manifest.author.name,
                    "email": manifest.author.email,
                    "url": manifest.author.url,
                }
            ),
            "homepage": manifest.homepage,
            "repository": manifest.repository,
            "license": manifest.license,
            "keywords": list(manifest.keywords),
            "extensions": list(manifest.extension_names),
        },
        "skills": {
            "disposition": summary.skills.disposition.value,
            "definitions": [
                {
                    "name": item.name,
                    "description": item.description,
                    "location": item.location,
                }
                for item in summary.skills.skills
            ],
            "diagnostics": [
                _diagnostic_payload(item) for item in summary.skills.diagnostics
            ],
        },
        "mcp": {
            "disposition": summary.mcp.disposition.value,
            "servers": [_plugin_mcp_summary(item) for item in summary.mcp.mcp_servers],
            "diagnostics": [
                _diagnostic_payload(item) for item in summary.mcp.diagnostics
            ],
        },
        "hooks": {
            "disposition": summary.hooks.disposition.value,
            "definitions": [
                {
                    "event": item.event,
                    "matcher": item.matcher,
                    "command": item.command,
                    "commandWindows": item.command_windows,
                    "timeout": item.timeout_seconds,
                    "async": item.asynchronous,
                    "statusMessage": item.status_message,
                }
                for item in summary.hooks.hook_definitions
            ],
            "diagnostics": [
                _diagnostic_payload(item) for item in summary.hooks.diagnostics
            ],
        },
    }


def _plugin_mcp_summary(item) -> dict[str, object]:
    payload: dict[str, object] = {
        "server_id": item.local_server_id,
        "transport": item.kind.value,
    }
    if item.kind.value == "stdio":
        payload.update(
            {
                "command": item.command,
                "args": list(item.args),
                "cwd": item.cwd,
                "env": dict(item.environment),
            }
        )
    else:
        payload.update(
            {
                "endpoint": item.endpoint,
                "public_headers": dict(item.public_headers),
            }
        )
    return payload


def _plugin_gc_payload(result) -> dict[str, object]:
    progress = result.progress
    return {
        "operation": "gc_local_plugin_packages",
        "disposition": result.disposition.value,
        "progress": {
            "ordered_removed": [_plugin_gc_ref(item) for item in progress.ordered_removed],
            "ordered_in_use": [_plugin_gc_ref(item) for item in progress.ordered_in_use],
            "current_attempted_ref": (
                None
                if progress.current_attempted_ref is None
                else _plugin_gc_ref(progress.current_attempted_ref)
            ),
            "current_location_status": (
                None
                if progress.current_location_status is None
                else progress.current_location_status.value
            ),
            "unvisited_suffix": progress.unvisited_suffix,
        },
        "diagnostics": [_diagnostic_payload(item) for item in result.diagnostics],
    }


def _plugin_gc_ref(item) -> dict[str, object]:
    return {
        "kind": item.kind.value,
        "path": str(item.path),
        "package_install_id": item.package_install_id,
    }


def _plugin_identity_payload(identity) -> dict[str, object]:
    return {
        "scope": identity.scope.value,
        "plugin_id": identity.plugin_id,
        "workspace_state_key": identity.workspace_state_key,
    }


def _diagnostic_payload(item) -> dict[str, object]:
    if isinstance(item, ProducerUnavailableCause):
        return {
            "kind": "SKILL_PRODUCER_UNAVAILABLE",
            "producer_kind": item.producer_kind.value,
            "reason": item.reason.value,
            "diagnostics": [value.to_dict() for value in item.diagnostics],
        }
    if isinstance(item, ResolutionUnavailableCause):
        return {
            "kind": "SKILL_RESOLUTION_UNAVAILABLE",
            "reason": item.reason.value,
            "diagnostics": [item.diagnostic.to_dict()],
        }
    if isinstance(item, InvalidSkillCandidateIssue):
        return {
            "kind": item.kind.value,
            "path": str(item.path),
            "origin_label": skill_origin_label(item.origin),
            "declared_name": item.declared_name,
            "diagnostics": [value.to_dict() for value in item.diagnostics],
        }
    if isinstance(item, ShadowedSkillCandidateIssue):
        return {
            "kind": item.kind.value,
            "path": str(item.path),
            "origin_label": skill_origin_label(item.origin),
            "name": item.name,
            "winner_origin_label": skill_origin_label(item.winner_origin),
            "winner_path": str(item.winner_path),
            "diagnostic_codes": [value.value for value in item.diagnostic_codes],
        }
    if isinstance(item, ConflictingSkillCandidateIssue):
        return {
            "kind": item.kind.value,
            "name": item.name,
            "tier": item.tier.value,
            "candidates": [
                {
                    "path": str(candidate.path),
                    "origin_label": skill_origin_label(candidate.origin),
                }
                for candidate in item.candidates
            ],
            "diagnostic_codes": [item.diagnostic_code.value],
        }
    if hasattr(item, "to_dict"):
        return item.to_dict()
    value: dict[str, object] = {
        "code": getattr(getattr(item, "code", None), "value", getattr(item, "code", type(item).__name__)),
        "message": getattr(item, "message", type(item).__name__),
    }
    for name in ("severity", "path", "component", "source_label"):
        field = getattr(item, name, None)
        if field is not None:
            value[name] = getattr(field, "value", field)
    return value


def _plugin_enable_review_text(review: dict[str, object]) -> str:
    return "Exact package review (Hook trust remains separate):\n" + json.dumps(
        review, indent=2, ensure_ascii=False
    )


def _render_plugin_payload(payload: dict[str, object], json_output: bool) -> str:
    if json_output:
        return json.dumps(payload, indent=2, ensure_ascii=False)
    lines = [
        f"Plugin operation: {payload.get('operation')}",
        f"Disposition: {payload.get('disposition')}",
    ]
    identity = payload.get("identity")
    if isinstance(identity, dict):
        lines.append(
            f"Instance: {identity.get('scope')}:{identity.get('plugin_id')}"
        )
    if "package_install_id" in payload:
        lines.append(f"Package install id: {payload['package_install_id']}")
    instances = payload.get("instances")
    if isinstance(instances, list):
        if not instances:
            lines.append("No current Plugin instances.")
        for item in instances:
            if isinstance(item, dict):
                item_identity = item["identity"]
                lines.append(
                    f"- {item_identity['scope']}:{item_identity['plugin_id']} "
                    f"{item['package_install_id']} enabled={item['enabled']}"
                )
                skills = ", ".join(item.get("effective_skill_names", [])) or "none"
                servers = ", ".join(
                    item.get("effective_mcp_server_ids", [])
                ) or "none"
                hook_count = item.get("effective_hook_definition_count", 0)
                hook_trust = item.get("effective_hook_trust_disposition") or "none"
                lines.append(f"  effective Skills: {skills}")
                lines.append(f"  effective MCP servers: {servers}")
                lines.append(
                    f"  effective Hooks: {hook_count} trust={hook_trust}"
                )
    for diagnostic in payload.get("diagnostics", []):
        if isinstance(diagnostic, dict):
            lines.append(f"- {diagnostic['code']}: {diagnostic['message']}")
    if payload.get("projection") == "doctor" or payload.get("disposition") in {
        "VALID",
        "INSTALLED",
        "REPLACED",
        "ALREADY_PRESENT",
    }:
        lines.append(json.dumps(payload, indent=2, ensure_ascii=False))
    return "\n".join(lines)


def _plugin_cli_print(
    output: str, credential_boundary: ProcessCredentialBoundary
) -> None:
    """Scrub and emit while supported rotation is excluded from stdout."""

    with credential_boundary.sync_guard() as guard:
        scrub_set = ProcessCredentialScrubSet()
        scrub_set.observe(guard.value)
        print(scrub_set.scrub_text(output))


def _plugin_cli_stderr(
    output: str,
    credential_boundary: ProcessCredentialBoundary,
    *,
    end: str = "\n",
) -> None:
    """Keep preflight review/prompt off JSON stdout and inside the sink gate."""

    with credential_boundary.sync_guard() as guard:
        scrub_set = ProcessCredentialScrubSet()
        scrub_set.observe(guard.value)
        print(scrub_set.scrub_text(output), end=end, file=sys.stderr, flush=True)


def _plugin_cli_parser_error(
    parser: argparse.ArgumentParser,
    message: str,
    credential_boundary: ProcessCredentialBoundary,
) -> None:
    """Keep argparse's irreversible stderr write inside the shared gate."""

    with credential_boundary.sync_guard() as guard:
        scrub_set = ProcessCredentialScrubSet()
        scrub_set.observe(guard.value)
        parser.error(scrub_set.scrub_text(message))


def _close_inspection_anchors(inspection: PluginInspectionOutcome) -> None:
    for anchor in inspection.physical_lifetime_anchors:
        close = getattr(anchor, "close", None)
        if close is not None:
            close()


def _plugin_exit_status(disposition) -> int:
    value = disposition.value
    if value in {
        "VALID",
        "INSTALLED",
        "REPLACED",
        "ALREADY_PRESENT",
        "ENABLED",
        "DISABLED",
        "ALREADY_ENABLED",
        "ALREADY_DISABLED",
        "REMOVED",
        "COMPLETE",
    }:
        return 0
    if value in {"INVALID", "NOT_FOUND", "STALE", "CANCELLED", "TIMED_OUT"}:
        return 1
    return 2


async def _mcp_command(
    args: argparse.Namespace,
    *,
    credential_boundary: ProcessCredentialBoundary | None = None,
) -> dict[str, object]:
    boundary = credential_boundary or ProcessCredentialBoundary()
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
                    "ALLOW_PRIVATE" if args.allow_private_network else "PUBLIC_ONLY"
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
                results.append({"server_id": config.server_id, "status": "disabled"})
                continue
            supervisor = McpHostSupervisor(
                session_id=f"mcp-doctor:{config.server_id}",
                workspace_root=workspace_root or Path.cwd(),
                configs=(config,),
                credential_boundary=boundary,
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


def _hooks_command(args: argparse.Namespace) -> dict[str, object]:
    workspace_root = Path(args.workspace or Path.cwd()).expanduser().resolve()
    workspace = resolve_workspace(
        HostWorkspaceInput(workspace_kind="project", workspace_root=workspace_root)
    )
    provider = LocalHookSourceProvider(
        workspace_root=workspace.workspace_root,
        workspace_kind=workspace.workspace_kind,
        workspace_state_key=workspace.workspace_key,
    )
    boundary = ProcessCredentialBoundary()
    user_home = resolve_user_home()
    home = resolve_pulsara_home(user_home_resolution=user_home)

    def observe_view():
        local = provider.discover()
        if home.disposition is not PulsaraHomeDisposition.RESOLVED:
            plugin_view = FrozenEnabledPluginView(
                EnabledPluginViewDisposition.UNAVAILABLE,
                diagnostics=(
                    PluginDiagnostic(
                        PluginDiagnosticCode.HOME_CONFIGURATION_INVALID,
                        "Plugin home configuration is invalid",
                    ),
                ),
            )
        else:
            plugin_view = EnabledPluginViewOwner(
                store=ManagedPluginStore(
                    pulsara_home=home, credential_boundary=boundary
                ),
                credential_boundary=boundary,
            ).observe(
                workspace_root=workspace.workspace_root,
                deadline_monotonic=float("inf"),
                cancellation=NeverCancelPluginOperation(),
            )
        try:
            view = compose_hook_definition_view(
                local_view=local,
                plugin_view=plugin_view,
                trust_store=provider.trust_store,
            )
        except BaseException:
            plugin_view.close()
            raise
        return view, plugin_view

    requested_scope = getattr(args, "scope", None)
    requested_source = getattr(args, "source", None)

    def selected_snapshots(view):
        snapshots = view.source_snapshots
        if requested_scope is not None:
            snapshots = tuple(
                item
                for item in snapshots
                if item.provenance.identity.visibility_scope.value.lower()
                == requested_scope
            )
        if requested_source in {None, "local"}:
            if requested_source == "local" or requested_scope is not None:
                snapshots = tuple(
                    item
                    for item in snapshots
                    if item.provenance.identity.kind
                    in {HookSourceKind.USER_FILE, HookSourceKind.WORKSPACE_FILE}
                )
            return snapshots
        if not requested_source.startswith("plugin:"):
            raise ValueError("Hook --source must be local or plugin:<plugin-id>")
        plugin_id = requested_source.removeprefix("plugin:")
        if not plugin_id:
            raise ValueError("Plugin Hook source id is empty")
        return tuple(
            item
            for item in snapshots
            if isinstance(item.provenance.identity, PluginHookSourceIdentity)
            and item.provenance.identity.plugin_id == plugin_id
        )

    def current_snapshot():
        view, plugin_view = observe_view()
        try:
            snapshots = selected_snapshots(view)
            if len(snapshots) != 1:
                raise KeyError("Hook source is absent or ambiguous")
            return snapshots[0]
        finally:
            plugin_view.close()

    command = args.hooks_command
    if command in {"list", "inspect", "doctor"}:
        view, plugin_view = observe_view()
        try:
            snapshots = selected_snapshots(view)
            values = [
                _hook_snapshot_public(item, inspect=command != "list")
                for item in snapshots
            ]
        finally:
            plugin_view.close()
        result: dict[str, object] = {"status": "ok", "sources": values}
        if command == "doctor":
            result["compatibility_profile"] = {
                "events": 11,
                "clear_producer": False,
                "transcript_path": None,
                "supported_handlers": ["command"],
                "unsupported_handlers": ["prompt", "agent", "http", "mcp_tool"],
                "output_spill": False,
                "argument_rewrite": False,
                "tool_result_rewrite_or_suppression": False,
                "filesystem_alias": "edit_file/write_file expose apply_patch matcher aliases but retain Pulsara arguments",
                "terminal_transport": "only terminal exposes Bash; terminal_process and terminal_monitor remain independent",
                "permission_allow": "PermissionRequest only",
                "post_tool_exit_2_replacement": False,
                "context_channel": "untrusted user-role append-only suffix",
                "trust_boundary": (
                    "exact normalized command definitions only; scripts, PATH, "
                    "imports and dependencies are not recursively verified"
                ),
                "running_host_refresh": "reload_hooks or restart required",
            }
        return result
    if command is None:
        raise ValueError("hooks requires a subcommand")
    snapshot = current_snapshot()
    subject = snapshot.provenance.trust_subject
    if command == "trust":
        expected = args.expected_definition_digest

        def read_current_digest() -> str:
            current = current_snapshot()
            digest = current.trust.current_definition_digest
            if digest is None:
                raise ValueError("current Hook source is unavailable")
            return digest

        provider.trust_store.trust_after_revalidation(
            subject,
            expected_digest=expected,
            current_digest_reader=read_current_digest,
        )
    elif command == "revoke":
        provider.trust_store.revoke(subject)
    elif command == "enable":
        provider.trust_store.set_enabled(subject, enabled=True)
    elif command == "disable":
        provider.trust_store.set_enabled(subject, enabled=False)
    else:
        raise ValueError("unknown hooks command")
    snapshot = current_snapshot()
    return {
        "status": "ok",
        "source": _hook_snapshot_public(snapshot, inspect=False),
        "notice": "running Hosts require reload_hooks or restart",
    }


def _hook_snapshot_public(snapshot, *, inspect: bool) -> dict[str, object]:
    value: dict[str, object] = {
        "scope": snapshot.provenance.identity.visibility_scope.value.lower(),
        "path": str(snapshot.provenance.identity.canonical_path),
        "description": snapshot.provenance.description,
        "source_disposition": snapshot.disposition.value,
        "trust_disposition": snapshot.trust.disposition.value,
        "enabled": snapshot.trust.enabled,
        "definition_digest": snapshot.trust.current_definition_digest,
        "trusted_definition_digest": snapshot.trust.trusted_definition_digest,
        "trusted_at": snapshot.trust.trusted_at,
        "runnable_handler_count": len(snapshot.definitions) if snapshot.runnable else 0,
        "declaration_environment": dict(
            snapshot.provenance.declaration_environment
        ),
        "diagnostics": [
            {"code": item.code, "message": item.message}
            for item in snapshot.diagnostics
        ],
    }
    identity = snapshot.provenance.identity
    if isinstance(identity, PluginHookSourceIdentity):
        value["plugin_id"] = identity.plugin_id
        value["package_install_id"] = identity.package_install_id
    if inspect:
        value["definitions"] = [
            {
                "ordinal": item.source_local_definition_ordinal,
                "event": item.event_type.external_name,
                "matcher": item.matcher.pattern,
                "command": item.command,
                "commandWindows": item.command_windows,
                "timeout": item.timeout_seconds,
                "async": item.asynchronous,
                "statusMessage": item.status_message,
                "additionalContextLimit": item.additional_context_limit,
            }
            for item in snapshot.definitions
        ]
    return value


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
    postgres = LocalSettingsStore().read().postgres
    if postgres is None:
        raise ValueError("PostgreSQL is not configured in local settings")
    runtime_dsn = postgres.runtime_dsn
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
        admin_dsn = postgres.admin_dsn
        if admin_dsn is None:
            raise ValueError("PostgreSQL admin DSN is not configured")
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


def _runtime_services():
    settings = LocalSettingsStore()
    catalog = ModelCatalogOwner(ModelsDevCatalogClient())
    runtime = ModelRuntime.production(
        settings=settings,
        catalog=catalog,
    )
    return settings, catalog, runtime


async def _config_check() -> dict[str, object]:
    settings_store, catalog, runtime = _runtime_services()
    settings_status = "ready"
    settings_error: str | None = None
    try:
        settings = settings_store.read()
    except LocalSettingsUnavailable as exc:
        settings = LocalSettings()
        settings_status = "unavailable"
        settings_error = str(exc)
    catalog_status = "ready"
    catalog_error: str | None = None
    try:
        await catalog.refresh()
    except Exception as exc:
        catalog_status = "unavailable"
        catalog_error = type(exc).__name__
    connections: list[dict[str, object]] = []
    for connection in settings.model_connections:
        status = "ready"
        detail: str | None = None
        if catalog_status != "ready" and connection.user_declared is None:
            status = "catalog_unavailable"
        else:
            try:
                contract = resolve_model_target_contract(
                    catalog=catalog.selectable(),
                    connection=connection,
                    route_wires=runtime.route_wires,
                )
                runtime.resolve_target(
                    ModelCallBinding(
                        connection.id,
                        default_reasoning_selection(contract.reasoning),
                    ),
                    timeout_policy=(
                        DEFAULT_KERNEL_WATCHDOG_POLICY.foreground_transport
                    ),
                )
            except Exception as exc:
                status = "unavailable"
                detail = str(exc)
        connections.append(
            {
                "id": connection.id.value,
                "route_id": connection.target.route_id,
                "wire_api": connection.target.wire_api.value,
                "model_id": connection.target.model_id,
                "base_url": connection.base_url,
                "source": (
                    "models_dev"
                    if connection.user_declared is None
                    else "user_declared"
                ),
                "authentication": connection.authentication.value,
                "credential_configured": (
                    settings.model_api_key(connection.id) is not None
                ),
                "status": status,
                "detail": detail,
            }
        )
    database: dict[str, object]
    if settings.postgres is None:
        database = {"status": "database_not_configured"}
    else:
        from pulsara_agent.storage.postgres_connection_provider import (
            PostgresRuntimeConnectionFactory,
        )

        try:
            bundle = await asyncio.to_thread(
                PostgresRuntimeConnectionFactory(settings.postgres.runtime_dsn).verify,
                deadline_monotonic=monotonic() + 30.0,
            )
        except Exception as exc:
            database = {"status": "unavailable", "detail": str(exc)}
        else:
            database = {
                "status": "ready",
                "database_name": bundle.binding.database_name,
                "runtime_role": bundle.binding.runtime_role,
                "migration_head_version": bundle.binding.migration_head_version,
            }
    return {
        "local_settings": {"status": settings_status, "detail": settings_error},
        "catalog": {"status": catalog_status, "detail": catalog_error},
        "database": database,
        "dashscope_credentials": {
            "embedding_configured": (
                settings.dashscope_api_key("embedding") is not None
            ),
            "rerank_configured": (
                settings.dashscope_api_key("rerank") is not None
            ),
        },
        "model_connections": connections,
    }


def _workspace_input_from_args(args) -> HostWorkspaceInput:
    return HostWorkspaceInput(
        workspace_kind=normalize_workspace_kind(args.workspace_kind or "project"),
        workspace_root=Path(args.workspace or "."),
        display_label=args.display_label,
        memory_domain_id=args.memory_domain_id or "u_local",
        trust_workspace_mcp_config=bool(getattr(args, "trust_workspace_mcp", False)),
    )


def _active_skill_names_from_args(args) -> frozenset[str]:
    return frozenset(item.strip() for item in args.skill if item.strip())


def _permission_policy(args):
    raw = args.permission_mode
    if raw:
        return preset_to_policy(parse_permission_mode(raw.strip()))
    return preset_to_policy(DEFAULT_PERMISSION_MODE)


def _public_error(exc: BaseException) -> str:
    if isinstance(exc, KeyError):
        return str(exc.args[0] if exc.args else "not found")
    return str(exc)


__all__ = ["build_parser", "main"]
