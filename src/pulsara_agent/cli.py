"""Minimal CLI entrypoint for the Pulsara backend."""

from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path

from pulsara_agent import __version__
from pulsara_agent.capability import CapabilityResolveContext, LocalSkillResolver
from pulsara_agent.graph import InMemoryGraphStore
from pulsara_agent.host import HostCore, HostWorkspaceInput, normalize_workspace_kind, resolve_workspace
from pulsara_agent.llm import ModelRole
from pulsara_agent.memory.artifacts.archive import InMemoryArchiveStore
from pulsara_agent.memory.canonical.ledger import ExecutionEvidenceLedger
from pulsara_agent.memory.canonical.write_gate import MemoryWriteGate
from pulsara_agent.ontology import memory, runtime as rt
from pulsara_agent.runtime import RuntimeSession
from pulsara_agent.settings import PulsaraSettings
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
    inspect_cmd = _add_host_workspace_args(
        host_subcommands.add_parser("inspect", help="Print a HostCore diagnostics snapshot.")
    )
    inspect_cmd.add_argument("--env-file", default=None, help="Load settings from a .env file before inspecting.")
    inspect_cmd.add_argument("--override-env", action="store_true", help="Let --env-file override existing env.")
    inspect_cmd.add_argument("--prefix", default="PULSARA", help="Environment variable prefix. Defaults to PULSARA.")
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
            result = asyncio.run(_host_run(args))
            print(result.final_text)
            return
        if args.host_command == "repl":
            asyncio.run(_host_repl(args))
            return
        if args.host_command == "inspect":
            snapshot = asyncio.run(_host_inspect(args))
            print(json.dumps(snapshot, indent=2))
            return
        parser.error("host requires a subcommand")

    parser.print_help()


async def _host_run(args) -> object:
    settings = _settings_from_host_args(args)
    core = HostCore(settings=settings, durable=bool(args.durable))
    session = await core.open_session(
        _workspace_input_from_args(args),
        model_role=ModelRole(args.model_role),
    )
    try:
        return await session.run_turn(args.prompt, active_skill_names=_active_skill_names_from_args(args))
    finally:
        await core.close_session(session.host_session_id)


async def _host_repl(args) -> None:
    settings = _settings_from_host_args(args)
    core = HostCore(settings=settings, durable=bool(args.durable))
    session = await core.open_session(
        _workspace_input_from_args(args),
        model_role=ModelRole(args.model_role),
    )
    try:
        while True:
            try:
                prompt = input("> ")
            except EOFError:
                break
            if prompt.strip() in {"exit", "quit", ":q"}:
                break
            result = await session.run_turn(prompt, active_skill_names=_active_skill_names_from_args(args))
            if result.final_text:
                print(result.final_text)
    finally:
        await core.close_session(session.host_session_id)


async def _host_inspect(args) -> dict[str, object]:
    workspace = resolve_workspace(_workspace_input_from_args(args))
    runtime_session = RuntimeSession(workspace.workspace_root)
    try:
        registry = build_core_tool_registry(runtime_session)
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
    }


def _settings_from_host_args(args) -> PulsaraSettings:
    if args.env_file:
        return PulsaraSettings.from_env_file(
            args.env_file,
            prefix=args.prefix,
            override=args.override_env,
        )
    return PulsaraSettings.from_env(prefix=args.prefix)


def _workspace_input_from_args(args) -> HostWorkspaceInput:
    return HostWorkspaceInput(
        workspace_kind=normalize_workspace_kind(args.workspace_kind),
        workspace_root=Path(args.workspace),
        display_label=args.display_label,
        memory_domain_id=args.memory_domain_id,
    )


def _active_skill_names_from_args(args) -> frozenset[str]:
    return frozenset(name.strip() for name in getattr(args, "skill", ()) if name.strip())
