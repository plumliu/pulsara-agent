"""Minimal CLI entrypoint for the Pulsara backend."""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

from pulsara_agent import __version__
from pulsara_agent.capability import (
    BUNDLED_OPT_OUT_MARKER_NAME,
    CapabilityResolveContext,
    LocalSkillResolver,
    bundled_skills_status,
    reset_bundled_skill,
    sync_bundled_skills,
)
from pulsara_agent.graph import InMemoryGraphStore
from pulsara_agent.host import HostCore, HostWorkspaceInput, normalize_workspace_kind, resolve_workspace
from pulsara_agent.llm import ModelRole
from pulsara_agent.memory.artifacts.archive import InMemoryArchiveStore
from pulsara_agent.memory.canonical.ledger import ExecutionEvidenceLedger
from pulsara_agent.memory.canonical.write_gate import MemoryWriteGate
from pulsara_agent.ontology import memory, runtime as rt
from pulsara_agent.runtime import ApprovalResolution, RuntimeSession, ToolApprovalDecision
from pulsara_agent.runtime.permission import resolve_permission_policy
from pulsara_agent.settings import PulsaraSettings, load_env_file
from pulsara_agent.tools import build_core_tool_registry


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="pulsara")
    parser.add_argument("--version", action="store_true", help="Print Pulsara version.")

    subcommands = parser.add_subparsers(dest="command")
    subcommands.add_parser("demo-ledger", help="Create and print a demo evidence ledger.")
    host = subcommands.add_parser("host", help="Run the thin HostCore smoke driver.")
    host_subcommands = host.add_subparsers(dest="host_command")
    _add_host_common_args(
        host_subcommands.add_parser("run", help="Run one prompt through HostCore and close the session.")
    ).add_argument("prompt", help="Prompt to run.")
    _add_host_common_args(
        host_subcommands.add_parser("repl", help="Start a minimal HostCore REPL.")
    )
    inspect_cmd = _add_host_permission_args(
        _add_host_workspace_args(
            host_subcommands.add_parser("inspect", help="Print a HostCore diagnostics snapshot.")
        )
    )
    inspect_cmd.add_argument("--env-file", default=None, help="Load settings from a .env file before inspecting.")
    inspect_cmd.add_argument("--override-env", action="store_true", help="Let --env-file override existing env.")
    inspect_cmd.add_argument("--prefix", default="PULSARA", help="Environment variable prefix. Defaults to PULSARA.")
    skills = subcommands.add_parser("skills", help="Manage Pulsara local skills.")
    skills_subcommands = skills.add_subparsers(dest="skills_command")
    sync_bundled = _add_skills_common_args(
        skills_subcommands.add_parser("sync-bundled", help="Sync bundled Pulsara skills into PULSARA_HOME.")
    )
    sync_bundled.add_argument(
        "--override-opt-out",
        action="store_true",
        help=f"Run even when {BUNDLED_OPT_OUT_MARKER_NAME} exists.",
    )
    _add_skills_common_args(skills_subcommands.add_parser("status", help="Print bundled skill status."))
    reset = _add_skills_common_args(skills_subcommands.add_parser("reset", help="Reset a bundled skill from package source."))
    reset.add_argument("name", help="Bundled skill name to reset.")
    config_check = subcommands.add_parser(
        "config-check",
        help="Load Pulsara configuration from environment variables.",
    )
    config_check.add_argument(
        "--prefix",
        default="PULSARA",
        help="Environment variable prefix. Defaults to PULSARA.",
    )
    config_check.add_argument(
        "--env-file",
        default=None,
        help="Load configuration from a .env file before reading the environment.",
    )
    config_check.add_argument(
        "--override-env",
        action="store_true",
        help="Let values from --env-file override existing environment variables.",
    )
    return parser


def _add_host_common_args(parser: argparse.ArgumentParser) -> argparse.ArgumentParser:
    _add_host_workspace_args(parser)
    _add_host_permission_args(parser)
    parser.add_argument(
        "--skill",
        action="append",
        default=[],
        help="Activate a workspace skill by name for this turn. May be repeated.",
    )
    parser.add_argument("--durable", action="store_true", help="Use durable runtime wiring.")
    parser.add_argument("--env-file", default=None, help="Load settings from a .env file before running.")
    parser.add_argument("--override-env", action="store_true", help="Let --env-file override existing env.")
    parser.add_argument("--prefix", default="PULSARA", help="Environment variable prefix. Defaults to PULSARA.")
    parser.add_argument(
        "--model-role",
        default=ModelRole.PRO.value,
        choices=(ModelRole.PRO.value, ModelRole.FLASH.value),
        help="Model role to use.",
    )
    return parser


def _add_host_workspace_args(parser: argparse.ArgumentParser) -> argparse.ArgumentParser:
    parser.add_argument("--workspace", default=".", help="Workspace path. Defaults to current directory.")
    parser.add_argument(
        "--workspace-kind",
        default="project",
        choices=("project", "transient", "ephemeral"),
        help="Workspace kind. 'ephemeral' is accepted as an adapter alias for 'transient'.",
    )
    parser.add_argument("--display-label", default=None, help="Optional workspace display label.")
    parser.add_argument("--memory-domain-id", default="u_local", help="Memory domain id. Defaults to u_local.")
    return parser


def _add_host_permission_args(parser: argparse.ArgumentParser) -> argparse.ArgumentParser:
    parser.add_argument(
        "--permission-profile",
        default=None,
        choices=("trusted_host", "workspace_guarded", "read_only"),
        help="Permission profile. Defaults depend on the host command and workspace kind.",
    )
    parser.add_argument(
        "--approval-policy",
        default=None,
        choices=("never", "risky_only", "on_request"),
        help="Approval policy. Defaults depend on the effective permission profile.",
    )
    parser.add_argument(
        "--terminal-access",
        default=None,
        choices=("off", "ask", "allow"),
        help="Terminal access policy. ASK is defined but requires approval resume before practical use.",
    )
    return parser


def _add_skills_common_args(parser: argparse.ArgumentParser) -> argparse.ArgumentParser:
    parser.add_argument("--env-file", default=None, help="Load a .env file before resolving PULSARA_HOME.")
    parser.add_argument("--override-env", action="store_true", help="Let --env-file override existing env.")
    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()

    if args.version:
        print(__version__)
        return

    if args.command == "demo-ledger":
        graph = InMemoryGraphStore()
        archive = InMemoryArchiveStore()
        gate = MemoryWriteGate()
        ledger = ExecutionEvidenceLedger(graph=graph, archive=archive, gate=gate)
        result = ledger.record_tool_result(
            turn_id="turn:demo/001",
            tool_name="search_files",
            status=rt.ToolExecutionStatus.SUCCESS,
            input_summary="Search for JSON-LD flattening",
            output="Found JSON-LD flattening in memory graph conversion.",
            scope="ctx:demo",
        )
        evidence = ledger.create_evidence_from_tool_result(
            result.tool_result_id,
            statement="The tool result found a JSON-LD flattening concern.",
            scope="ctx:demo",
        )
        claim = ledger.submit_claim(
            statement="Pulsara should preserve JSON-LD semantics before optimizing recall.",
            scope="ctx:demo",
            evidence_ids=[evidence.evidence_id],
            source_authority=memory.SourceAuthority.TOOL_RESULT,
            verification_status=memory.VerificationStatus.TOOL_VERIFIED,
        )
        print(json.dumps({"tool_result": result.to_dict(), "evidence": evidence.to_dict(), "claim": claim.to_dict()}, indent=2))
        return

    if args.command == "config-check":
        try:
            if args.env_file:
                settings = PulsaraSettings.from_env_file(
                    args.env_file,
                    prefix=args.prefix,
                    override=args.override_env,
                )
            else:
                settings = PulsaraSettings.from_env(prefix=args.prefix)
        except ValueError as exc:
            parser.error(str(exc))
        print(json.dumps(settings.redacted_dict(), indent=2))
        return

    if args.command == "host":
        if args.host_command == "run":
            try:
                result = asyncio.run(_host_run(args))
            except ValueError as exc:
                parser.error(str(exc))
            if isinstance(result, dict) and result.get("pending_approval") is not None:
                print(json.dumps(result, indent=2))
            else:
                print(result.final_text)
            return
        if args.host_command == "repl":
            try:
                asyncio.run(_host_repl(args))
            except ValueError as exc:
                parser.error(str(exc))
            return
        if args.host_command == "inspect":
            try:
                snapshot = asyncio.run(_host_inspect(args))
            except ValueError as exc:
                parser.error(str(exc))
            print(json.dumps(snapshot, indent=2))
            return
        parser.error("host requires a subcommand")

    if args.command == "skills":
        if args.skills_command == "sync-bundled":
            result = _skills_sync_bundled(args)
            print(json.dumps(result.to_dict(), indent=2))
            return
        if args.skills_command == "status":
            result = _skills_status(args)
            print(json.dumps(result.to_dict(), indent=2))
            return
        if args.skills_command == "reset":
            result = _skills_reset(args)
            print(json.dumps(result.to_dict(), indent=2))
            return
        parser.error("skills requires a subcommand")

    parser.print_help()


async def _host_run(args) -> object:
    settings = _settings_from_host_args(args)
    permission_policy = _permission_policy_from_host_args(args, intent="run")
    _best_effort_sync_bundled_skills()
    core = HostCore(settings=settings, durable=bool(args.durable))
    session = await core.open_session(
        _workspace_input_from_args(args),
        model_role=ModelRole(args.model_role),
        permission_policy=permission_policy,
    )
    try:
        result = await session.run_turn(args.prompt, active_skill_names=_active_skill_names_from_args(args))
        pending = session.get_pending_approval()
        if pending is not None:
            return {
                "status": "waiting_user",
                "message": "This one-shot host run is waiting for approval. Use host repl or HostCore APIs to resolve it.",
                "pending_approval": pending.to_dict(),
            }
        return result
    finally:
        await core.close_session(session.host_session_id)


async def _host_repl(args) -> None:
    settings = _settings_from_host_args(args)
    permission_policy = _permission_policy_from_host_args(args, intent="run")
    _best_effort_sync_bundled_skills()
    core = HostCore(settings=settings, durable=bool(args.durable))
    session = await core.open_session(
        _workspace_input_from_args(args),
        model_role=ModelRole(args.model_role),
        permission_policy=permission_policy,
    )
    try:
        while True:
            try:
                prompt = input("> ")
            except EOFError:
                break
            if prompt.strip() in {"exit", "quit", ":q"}:
                break
            if prompt.strip() == ":approval":
                pending = session.get_pending_approval()
                print(json.dumps(pending.to_dict() if pending is not None else None, indent=2))
                continue
            if prompt.strip() == ":stop":
                result = await session.stop_current_turn()
                if result is None:
                    print("No active turn to stop.")
                else:
                    print(json.dumps({"status": result.status.value, "stop_reason": result.stop_reason}, indent=2))
                continue
            if prompt.strip() in {":approve", ":deny"}:
                pending = session.get_pending_approval()
                if pending is None:
                    print("No pending approval.")
                    continue
                confirmed = prompt.strip() == ":approve"
                resolution = ApprovalResolution(
                    approval_id=pending.approval_id,
                    decisions=tuple(
                        ToolApprovalDecision(tool_call_id=call.id, confirmed=confirmed)
                        for call in pending.tool_calls
                    ),
                )
                result = await session.resolve_approval(resolution)
                if result.final_text:
                    print(result.final_text)
                pending = session.get_pending_approval()
                if pending is not None:
                    print(json.dumps({"pending_approval": pending.to_dict()}, indent=2))
                continue
            result = await session.run_turn(prompt, active_skill_names=_active_skill_names_from_args(args))
            if result.final_text:
                print(result.final_text)
            pending = session.get_pending_approval()
            if pending is not None:
                print(json.dumps({"pending_approval": pending.to_dict()}, indent=2))
    finally:
        await core.close_session(session.host_session_id)


async def _host_inspect(args) -> dict[str, object]:
    _load_env_file_from_args(args)
    workspace = resolve_workspace(_workspace_input_from_args(args))
    permission_policy = _permission_policy_from_host_args(args, intent="inspect")
    runtime_session = RuntimeSession(workspace.workspace_root)
    try:
        registry = build_core_tool_registry(runtime_session, permission_policy=permission_policy)
        resolver = LocalSkillResolver()
        capabilities = resolver.resolve(
            CapabilityResolveContext(
                workspace_root=workspace.workspace_root,
                workspace_kind=workspace.workspace_kind,
                memory_domain=workspace.memory_domain,
                available_tool_names=frozenset(registry.names()),
                user_input="",
            )
        )
    finally:
        runtime_session.close()
    return {
        "sessions": [],
        "workspace_supervisors": [],
        "recovery_scope": "host_process",
        "workspace": {
            "workspace_kind": workspace.workspace_kind,
            "workspace_root": str(workspace.workspace_root),
            "display_label": workspace.display_label,
            "workspace_scope": workspace.workspace_scope,
            "workspace_key": workspace.workspace_key,
            "read_scopes": sorted(workspace.memory_domain.read_scopes),
            "allowed_write_scopes": sorted(workspace.memory_domain.allowed_write_scopes),
        },
        "tools": registry.names(),
        "permissions": permission_policy.to_dict(),
        "memory": {
            "graph_id": workspace.memory_domain.graph_id,
            "tools_enabled": sorted(
                name for name in registry.names() if name.startswith(("memory_", "remember_"))
            ),
            "read_scopes": sorted(workspace.memory_domain.read_scopes),
            "allowed_write_scopes": sorted(workspace.memory_domain.allowed_write_scopes),
        },
        "skills": [
            {
                "name": entry.name,
                "description": entry.description,
                "when_to_use": entry.when_to_use,
                "location": entry.location,
                "provides_tools": list(entry.provides_tools),
            }
            for entry in capabilities.catalog_entries
        ],
        "active_skills": [
            {
                "name": injection.name,
                "location": injection.location,
                "reason": injection.reason,
            }
            for injection in capabilities.active_injections
        ],
        "capability_diagnostics": [diagnostic.to_dict() for diagnostic in capabilities.diagnostics],
        "bundled_skills": bundled_skills_status().to_dict(),
    }


def _settings_from_host_args(args) -> PulsaraSettings:
    if args.env_file:
        return PulsaraSettings.from_env_file(
            args.env_file,
            prefix=args.prefix,
            override=args.override_env,
        )
    return PulsaraSettings.from_env(prefix=args.prefix)


def _load_env_file_from_args(args) -> None:
    env_file = getattr(args, "env_file", None)
    if env_file:
        load_env_file(env_file, override=bool(getattr(args, "override_env", False)))


def _best_effort_sync_bundled_skills() -> None:
    try:
        result = sync_bundled_skills()
    except Exception as exc:  # pragma: no cover - defensive best-effort boundary
        print(f"pulsara: bundled skill sync failed: {exc}", file=sys.stderr)
        return
    changed = [item for item in result.items if item.action in {"installed", "updated"}]
    if changed:
        names = ", ".join(item.name for item in changed)
        print(f"pulsara: bundled skills synced: {names}", file=sys.stderr)


def _skills_sync_bundled(args):
    _load_env_file_from_args(args)
    return sync_bundled_skills(override_opt_out=bool(args.override_opt_out))


def _skills_status(args):
    _load_env_file_from_args(args)
    return bundled_skills_status()


def _skills_reset(args):
    _load_env_file_from_args(args)
    try:
        return reset_bundled_skill(args.name)
    except ValueError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        raise SystemExit(2) from exc


def _workspace_input_from_args(args) -> HostWorkspaceInput:
    return HostWorkspaceInput(
        workspace_kind=normalize_workspace_kind(args.workspace_kind),
        workspace_root=Path(args.workspace),
        display_label=args.display_label,
        memory_domain_id=args.memory_domain_id,
    )


def _active_skill_names_from_args(args) -> frozenset[str]:
    return frozenset(name.strip() for name in getattr(args, "skill", ()) if name.strip())


def _permission_policy_from_host_args(args, *, intent: str):
    workspace_kind = normalize_workspace_kind(args.workspace_kind)
    prefix = getattr(args, "prefix", "PULSARA")
    return resolve_permission_policy(
        workspace_kind=workspace_kind,
        intent=intent,
        profile=getattr(args, "permission_profile", None),
        approval=getattr(args, "approval_policy", None),
        terminal=getattr(args, "terminal_access", None),
        prefix=prefix,
    )
