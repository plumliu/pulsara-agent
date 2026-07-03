"""Minimal CLI entrypoint for the Pulsara backend."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path

from pulsara_agent import __version__
from pulsara_agent.capability import (
    BUNDLED_OPT_OUT_MARKER_NAME,
    CapabilityResolveContext,
    LocalSkillCapabilityProvider,
    SkillBinaryLookupPath,
    SkillHealthResolver,
    bundled_skills_status,
    reset_bundled_skill,
    sync_bundled_skills,
    default_pulsara_home,
)
from pulsara_agent.capability.runtime import CapabilityRuntime
from pulsara_agent.host import (
    HostCore,
    HostSessionBusyError,
    HostSessionPendingApprovalError,
    HostSessionPendingInteractionError,
    HostWorkspaceInput,
    normalize_workspace_kind,
    resolve_workspace,
)
from pulsara_agent.event import ContextCompactionCompletedEvent, ContextCompactionFailedEvent
from pulsara_agent.inspector import InspectorService, PostgresInspectorStore
from pulsara_agent.llm import ModelRole
from pulsara_agent.runtime import (
    ApprovalResolution,
    PendingPlanInteraction,
    PlanExitResolution,
    PlanQuestionResolution,
    ToolApprovalDecision,
    build_durable_runtime_wiring,
)
from pulsara_agent.runtime.permission import (
    PermissionMode,
    PermissionState,
    mode_for_policy,
    parse_permission_mode,
    preset_to_policy,
    resolve_permission_policy,
)
from pulsara_agent.repl import ReplPrompt, build_repl_prompt
from pulsara_agent.settings import PulsaraSettings, load_env_file
from pulsara_agent.tools import build_core_tool_registry


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="pulsara")
    parser.add_argument("--version", action="store_true", help="Print Pulsara version.")

    subcommands = parser.add_subparsers(dest="command")
    host = subcommands.add_parser("host", help="Run the thin HostCore smoke driver.")
    host_subcommands = host.add_subparsers(dest="host_command")
    _add_host_common_args(
        host_subcommands.add_parser("run", help="Run one prompt through HostCore and close the session.")
    ).add_argument("prompt", help="Prompt to run.")
    repl = _add_host_common_args(host_subcommands.add_parser("repl", help="Start a minimal HostCore REPL."))
    repl_resume = repl.add_mutually_exclusive_group()
    repl_resume.add_argument("--resume", default=None, help="Resume an existing runtime session id.")
    repl_resume.add_argument(
        "--continue",
        dest="continue_session",
        action="store_true",
        help="Resume the most recent resumable session for this workspace.",
    )
    repl.add_argument(
        "--list-sessions",
        action="store_true",
        help="List resumable sessions for this workspace and exit.",
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
    inspect = subcommands.add_parser("inspect", help="Read-only durable runtime inspector.")
    inspect_subcommands = inspect.add_subparsers(dest="inspect_command")
    inspect_run = _add_inspect_common_args(inspect_subcommands.add_parser("run", help="Inspect a durable run."))
    inspect_run.add_argument("run_id")
    inspect_session = _add_inspect_common_args(
        inspect_subcommands.add_parser("session", help="Inspect a durable session.")
    )
    inspect_session.add_argument("session_id")
    inspect_artifact = _add_inspect_common_args(
        inspect_subcommands.add_parser("artifact", help="Inspect an artifact.")
    )
    inspect_artifact.add_argument("artifact_id")
    inspect_memory = _add_inspect_common_args(inspect_subcommands.add_parser("memory", help="Inspect a memory node."))
    inspect_memory.add_argument("memory_id")
    _add_inspect_common_args(inspect_subcommands.add_parser("health", help="Inspect durable subsystem health."))
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
    parser.add_argument("--workspace", default=None, help="Workspace path. Defaults to current directory.")
    parser.add_argument(
        "--workspace-kind",
        default=None,
        choices=("project", "transient"),
        help="Workspace kind: 'project' or 'transient'.",
    )
    parser.add_argument("--display-label", default=None, help="Optional workspace display label.")
    parser.add_argument("--memory-domain-id", default=None, help="Memory domain id. Defaults to u_local.")
    return parser


def _add_host_permission_args(parser: argparse.ArgumentParser) -> argparse.ArgumentParser:
    parser.add_argument(
        "--permission-mode",
        default=None,
        choices=tuple(mode.value for mode in PermissionMode),
        help=(
            "Permission preset (main path). One of: read-only, ask-permissions, "
            "accept-edits, bypass-permissions. Defaults to bypass-permissions for run, "
            "read-only for inspect. Mutually exclusive with the advanced --permission-profile/"
            "--approval-policy/--terminal-access flags."
        ),
    )
    parser.add_argument(
        "--permission-profile",
        default=None,
        choices=("trusted_host", "workspace_guarded", "read_only"),
        help="[advanced/custom] Raw permission profile. Cannot be combined with --permission-mode.",
    )
    parser.add_argument(
        "--approval-policy",
        default=None,
        choices=("never", "risky_only", "on_request"),
        help="[advanced/custom] Raw approval policy. Cannot be combined with --permission-mode.",
    )
    parser.add_argument(
        "--terminal-access",
        default=None,
        choices=("off", "ask", "allow"),
        help="[advanced/custom] Raw terminal access policy. Cannot be combined with --permission-mode.",
    )
    return parser


def _add_skills_common_args(parser: argparse.ArgumentParser) -> argparse.ArgumentParser:
    parser.add_argument("--env-file", default=None, help="Load a .env file before resolving PULSARA_HOME.")
    parser.add_argument("--override-env", action="store_true", help="Let --env-file override existing env.")
    return parser


def _add_inspect_common_args(parser: argparse.ArgumentParser) -> argparse.ArgumentParser:
    parser.add_argument("--env-file", default=None, help="Load settings from a .env file before inspecting.")
    parser.add_argument("--override-env", action="store_true", help="Let --env-file override existing env.")
    parser.add_argument("--prefix", default="PULSARA", help="Environment variable prefix. Defaults to PULSARA.")
    parser.add_argument("--format", default="json", choices=("json",), help="Output format. Defaults to json.")
    parser.add_argument("--include-payload", action="store_true", help="Include raw event or artifact payloads.")
    parser.add_argument("--limit-events", type=int, default=200, help="Maximum event summaries to include.")
    parser.add_argument("--max-chars", type=int, default=2000, help="Maximum artifact preview characters.")
    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()

    if args.version:
        print(__version__)
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
            if isinstance(result, dict) and (
                result.get("pending_approval") is not None or result.get("pending_interaction") is not None
            ):
                print(json.dumps(result, indent=2))
            else:
                print(result.final_text)
            return
        if args.host_command == "repl":
            try:
                asyncio.run(_host_repl(args))
            except ValueError as exc:
                parser.error(str(exc))
            except KeyError as exc:
                parser.error(_format_not_found_error(exc))
            return
        if args.host_command == "inspect":
            try:
                snapshot = asyncio.run(_host_inspect(args))
            except ValueError as exc:
                parser.error(str(exc))
            print(json.dumps(snapshot, indent=2))
            return
        parser.error("host requires a subcommand")

    if args.command == "inspect":
        try:
            report = _inspect(args)
        except ValueError as exc:
            parser.error(str(exc))
        except KeyError as exc:
            parser.error(f"not found: {exc.args[0]}")
        print(json.dumps(report, indent=2, ensure_ascii=False, default=str))
        return

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
    core = HostCore(settings=settings)
    session = None
    try:
        session = await core.open_session(
            _workspace_input_from_args(args),
            model_role=ModelRole(args.model_role),
            permission_policy=permission_policy,
        )
        result = await session.run_turn(args.prompt, active_skill_names=_active_skill_names_from_args(args))
        pending = session.get_pending_approval()
        if pending is not None:
            return {
                "status": "waiting_user",
                "message": "This one-shot host run is waiting for approval. Use host repl or HostCore APIs to resolve it.",
                "pending_approval": pending.to_dict(),
            }
        pending_interaction = session.get_pending_interaction()
        if pending_interaction is not None:
            return {
                "status": "waiting_user",
                "message": "This one-shot host run is waiting for a user interaction. Use host repl or HostCore APIs to resolve it.",
                "pending_interaction": pending_interaction.to_dict(),
            }
        return result
    finally:
        close_error = None
        if session is not None:
            try:
                await core.close_session(session.host_session_id, close_conversation=True)
            except BaseException as exc:
                close_error = exc
        try:
            await core.shutdown()
        finally:
            if close_error is not None:
                raise close_error


def _inspect(args) -> dict[str, object]:
    if args.inspect_command is None:
        raise ValueError("inspect requires a subcommand")
    settings = _settings_from_inspect_args(args)
    service = InspectorService(
        PostgresInspectorStore(settings.storage.postgres_dsn),
        oxigraph_url=settings.storage.oxigraph_url,
    )
    if args.inspect_command == "run":
        return service.inspect_run(
            args.run_id,
            limit_events=args.limit_events,
            include_payload=args.include_payload,
        )
    if args.inspect_command == "session":
        return service.inspect_session(
            args.session_id,
            limit_events=args.limit_events,
            include_payload=args.include_payload,
        )
    if args.inspect_command == "artifact":
        return service.inspect_artifact(
            args.artifact_id,
            include_payload=args.include_payload,
            max_chars=args.max_chars,
        )
    if args.inspect_command == "memory":
        return service.inspect_memory(args.memory_id)
    if args.inspect_command == "health":
        return service.inspect_health()
    raise ValueError(f"unsupported inspect command: {args.inspect_command}")


PLAN_APPROVE_TOKENS = frozenset({"approve", "yes", "是", "好", "可以", "同意", "好的", "批准", "y", "Y"})


def _format_plan_question(pending: PendingPlanInteraction) -> str:
    lines = ["Plan question:", "", pending.question]
    if pending.options:
        lines.extend(["", "Options:"])
        for index, option in enumerate(pending.options, start=1):
            suffix = " (Recommended)" if option.recommended else ""
            lines.append(f"  {index}. {option.label}{suffix}")
            if option.description:
                lines.append(f"     {option.description}")
    if pending.allow_free_text:
        lines.extend(["", "Reply with :choose <n|label>, a number/label, or :answer <text>."])
    else:
        lines.extend(["", "Reply with :choose <n|label> or a number/label. Free text is disabled."])
    return "\n".join(lines)


def _format_plan_exit(pending: PendingPlanInteraction) -> str:
    lines = ["Plan ready for approval:"]
    if pending.summary:
        lines.extend(["", pending.summary])
    if pending.plan_text:
        lines.extend(["", pending.plan_text])
    lines.extend(
        [
            "",
            "Reply with:",
            "  :approve-plan    Accept and exit plan mode",
            "  :revise-plan ... Request a revision and stay read-only",
            "  :cancel-plan     Abandon this plan workflow and exit plan mode",
        ]
    )
    return "\n".join(lines)


def _format_pending_plan_interaction(pending: PendingPlanInteraction) -> str:
    if pending.kind == "question":
        return _format_plan_question(pending)
    return _format_plan_exit(pending)


def _print_pending_plan_interaction(pending: PendingPlanInteraction | None) -> None:
    if pending is not None:
        print(_format_pending_plan_interaction(pending))


def _select_plan_question_option(pending: PendingPlanInteraction, selector: str) -> str | None:
    value = selector.strip()
    if not value:
        return None
    if value.isdigit():
        index = int(value)
        if 1 <= index <= len(pending.options):
            return pending.options[index - 1].label
        return None
    for option in pending.options:
        if option.label == value:
            return option.label
    return None


async def _answer_plan_question(session, pending: PendingPlanInteraction, answer: str) -> None:
    selected_option = _select_plan_question_option(pending, answer)
    if selected_option is None and not pending.allow_free_text:
        print("Free text is disabled for this question. Use :choose <n|label>.", file=sys.stderr)
        return
    result = await session.resolve_plan_interaction(
        PlanQuestionResolution(
            interaction_id=pending.interaction_id,
            answer_text=selected_option or answer,
            selected_option=selected_option,
        )
    )
    if result.final_text:
        print(result.final_text)
    _print_pending_plan_interaction(session.get_pending_interaction())


async def _choose_plan_question_option(session, pending: PendingPlanInteraction, selector: str) -> None:
    selected_option = _select_plan_question_option(pending, selector)
    if selected_option is None:
        print("No matching plan question option. Use :interaction to see available choices.", file=sys.stderr)
        return
    await _answer_plan_question(session, pending, selected_option)


async def _approve_pending_plan(session, pending: PendingPlanInteraction) -> None:
    result = await session.resolve_plan_interaction(
        PlanExitResolution(interaction_id=pending.interaction_id, decision="approve")
    )
    if result.final_text:
        print(result.final_text)


def _attach_repl_compaction_notifications(session) -> None:
    add_listener = getattr(session, "add_compaction_listener", None)
    if not callable(add_listener):
        return
    add_listener(_print_context_compaction_event)


def _print_context_compaction_event(event) -> None:
    if isinstance(event, ContextCompactionCompletedEvent):
        print(
            "context compaction completed: "
            f"compaction_id={event.compaction_id} "
            f"summary_artifact_id={event.summary_artifact_id} "
            f"window_id={event.window_id}"
        )
        return
    if isinstance(event, ContextCompactionFailedEvent):
        print(
            "context compaction failed: "
            f"compaction_id={event.compaction_id} "
            f"{event.error_type}: {event.message}",
            file=sys.stderr,
        )


def _format_manual_compaction_result(result: dict[str, object]) -> str:
    if result.get("compacted"):
        return (
            "context compaction completed: "
            f"compaction_id={result.get('compaction_id')} "
            f"summary_artifact_id={result.get('summary_artifact_id')} "
            f"window_id={result.get('window_id')}"
        )
    return "context compaction skipped: no eligible compact window"


async def _host_repl(args) -> None:
    settings = _settings_from_host_args(args)
    permission_policy = _permission_policy_from_host_args(args, intent="run")
    _best_effort_sync_bundled_skills()
    core = HostCore(settings=settings)
    repl_prompt: ReplPrompt = build_repl_prompt(
        history_path=default_pulsara_home() / "repl_history",
    )
    try:
        workspace_input = _workspace_input_from_args(args)
        resume_workspace_input = workspace_input if _has_explicit_workspace_override(args) else None
        if getattr(args, "list_sessions", False):
            sessions = await core.list_resumable_sessions(workspace_input=workspace_input)
            print(json.dumps([summary.to_dict() for summary in sessions], indent=2, ensure_ascii=False))
            return
        session = await _open_initial_repl_session(
            core,
            args,
            workspace_input=workspace_input,
            resume_workspace_input=resume_workspace_input,
            permission_policy=permission_policy,
        )
        _attach_repl_compaction_notifications(session)
        print("Pulsara REPL · :help 查看命令 · Ctrl-D detach · :close 关闭对话")
        while True:
            try:
                prompt = await repl_prompt.read_line(_repl_prompt_message(session))
            except KeyboardInterrupt:
                # prompt_toolkit has already cleared the current input buffer.
                # Keep the session alive instead of turning Ctrl-C into teardown.
                print("^C")
                continue
            except EOFError:
                print()
                break
            command = prompt.strip()
            if not command:
                continue
            if command in {"exit", "quit", ":q"}:
                break
            if command in {":help", ":h", ":?"}:
                print(_REPL_HELP)
                continue
            if command == ":sessions":
                sessions = await core.list_resumable_sessions(workspace_input=workspace_input)
                print(json.dumps([summary.to_dict() for summary in sessions], indent=2, ensure_ascii=False))
                continue
            if command.startswith(":resume"):
                runtime_session_id = command[len(":resume"):].strip()
                if not runtime_session_id:
                    print("Usage: :resume <runtime_session_id>", file=sys.stderr)
                    continue
                if runtime_session_id == session.runtime_session_id:
                    print(f"Already attached to {session.runtime_session_id}")
                    continue
                try:
                    next_session = await core.resume_session(
                        runtime_session_id,
                        workspace_input=resume_workspace_input,
                        model_role=ModelRole(args.model_role),
                        permission_policy=permission_policy,
                    )
                    await core.detach_session(session.host_session_id)
                    session = next_session
                    _attach_repl_compaction_notifications(session)
                except Exception as exc:
                    print(f"ERROR: {exc}", file=sys.stderr)
                    continue
                print(f"Resumed {session.runtime_session_id}")
                continue
            if command == ":continue":
                try:
                    summaries = await core.list_resumable_sessions(workspace_input=workspace_input)
                    target = next(
                        (summary for summary in summaries if summary.runtime_session_id != session.runtime_session_id),
                        None,
                    )
                    if target is None:
                        print(f"Already attached to the latest session: {session.runtime_session_id}")
                        continue
                    next_session = await core.resume_session(
                        target.runtime_session_id,
                        workspace_input=workspace_input,
                        model_role=ModelRole(args.model_role),
                        permission_policy=permission_policy,
                    )
                    await core.detach_session(session.host_session_id)
                    session = next_session
                    _attach_repl_compaction_notifications(session)
                except Exception as exc:
                    print(f"ERROR: {exc}", file=sys.stderr)
                    continue
                print(f"Resumed {session.runtime_session_id}")
                continue
            if command == ":close":
                await core.close_session(session.host_session_id, close_conversation=True)
                print(f"Closed {session.runtime_session_id}")
                break
            if command == ":approval":
                pending = session.get_pending_approval()
                print(json.dumps(pending.to_dict() if pending is not None else None, indent=2))
                continue
            if command == ":interaction":
                pending = session.get_pending_interaction()
                if isinstance(pending, PendingPlanInteraction):
                    print(_format_pending_plan_interaction(pending))
                else:
                    print(json.dumps(pending.to_dict() if pending is not None else None, indent=2))
                continue
            if command.startswith(":plan"):
                reason = command[len(":plan"):].strip()
                try:
                    policy = session.enter_plan(reason=reason)
                except (HostSessionBusyError, HostSessionPendingApprovalError, HostSessionPendingInteractionError) as exc:
                    print(f"ERROR: {exc}", file=sys.stderr)
                    continue
                print(json.dumps({"plan": session.plan_state.to_dict(), "policy": policy.to_dict()}, indent=2))
                continue
            if command == ":status":
                mode = session.current_permission_mode
                print(
                    json.dumps(
                        {
                            "mode": mode.value if mode is not None else "custom",
                            "policy": session.current_permission_policy().to_dict(),
                        },
                        indent=2,
                    )
                )
                continue
            if command.startswith(":mode"):
                requested = command[len(":mode"):].strip()
                if not requested:
                    allowed = ", ".join(m.value for m in PermissionMode)
                    print(f"Usage: :mode <preset>  (one of: {allowed})", file=sys.stderr)
                    continue
                try:
                    policy = session.set_permission_mode(requested)
                except (
                    ValueError,
                    HostSessionBusyError,
                    HostSessionPendingApprovalError,
                    HostSessionPendingInteractionError,
                ) as exc:
                    print(f"ERROR: {exc}", file=sys.stderr)
                    continue
                print(json.dumps({"mode": requested, "policy": policy.to_dict()}, indent=2))
                continue
            if command == ":stop":
                result = await session.stop_current_turn()
                if result is None:
                    print("No active turn to stop.")
                else:
                    print(json.dumps({"status": result.status.value, "stop_reason": result.stop_reason}, indent=2))
                continue
            if command == ":compact":
                try:
                    result = await session.compact_now()
                except Exception as exc:
                    print(f"context compaction failed: {exc}", file=sys.stderr)
                    continue
                print(_format_manual_compaction_result(result))
                continue
            if command.startswith(":answer"):
                pending = session.get_pending_interaction()
                if not isinstance(pending, PendingPlanInteraction) or pending.kind != "question":
                    print("No pending plan question.")
                    continue
                answer = command[len(":answer"):].strip()
                if not answer:
                    print("Usage: :answer <text>", file=sys.stderr)
                    continue
                await _answer_plan_question(session, pending, answer)
                continue
            if command.startswith(":choose"):
                pending = session.get_pending_interaction()
                if not isinstance(pending, PendingPlanInteraction) or pending.kind != "question":
                    print("No pending plan question.")
                    continue
                selector = command[len(":choose"):].strip()
                if not selector:
                    print("Usage: :choose <n|label>", file=sys.stderr)
                    continue
                await _choose_plan_question_option(session, pending, selector)
                continue
            if command == ":approve-plan":
                pending = session.get_pending_interaction()
                if not isinstance(pending, PendingPlanInteraction) or pending.kind != "exit":
                    print("No pending plan exit request.")
                    continue
                await _approve_pending_plan(session, pending)
                continue
            if command.startswith(":revise-plan"):
                pending = session.get_pending_interaction()
                if not isinstance(pending, PendingPlanInteraction) or pending.kind != "exit":
                    print("No pending plan exit request.")
                    continue
                feedback = command[len(":revise-plan"):].strip()
                result = await session.resolve_plan_interaction(
                    PlanExitResolution(
                        interaction_id=pending.interaction_id,
                        decision="revise",
                        user_feedback=feedback,
                    )
                )
                if result.final_text:
                    print(result.final_text)
                _print_pending_plan_interaction(session.get_pending_interaction())
                continue
            if command == ":cancel-plan":
                pending = session.get_pending_interaction()
                if not isinstance(pending, PendingPlanInteraction) or pending.kind != "exit":
                    print("No pending plan exit request. Use :force-exit-plan to leave active plan mode.", file=sys.stderr)
                    continue
                await session.exit_plan_workflow(source="user_cancel")
                print("Plan workflow cancelled.")
                continue
            if command == ":force-exit-plan":
                if not session.plan_state.active:
                    print("Plan workflow is not active.")
                    continue
                try:
                    await session.exit_plan_workflow(source="user_force_exit")
                except (HostSessionBusyError, HostSessionPendingInteractionError, ValueError) as exc:
                    print(f"ERROR: {exc}", file=sys.stderr)
                    continue
                print("Plan workflow exited.")
                continue
            if command in {":approve", ":deny"}:
                pending = session.get_pending_approval()
                if pending is None:
                    print("No pending approval.")
                    continue
                confirmed = command == ":approve"
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
            pending_interaction = session.get_pending_interaction()
            if isinstance(pending_interaction, PendingPlanInteraction):
                if pending_interaction.kind == "question":
                    selected_option = _select_plan_question_option(pending_interaction, command)
                    if selected_option is not None:
                        await _choose_plan_question_option(session, pending_interaction, selected_option)
                    elif pending_interaction.allow_free_text:
                        await _answer_plan_question(session, pending_interaction, command)
                    else:
                        print(
                            "Pending plan question requires one of the listed options. Use :interaction to see choices.",
                            file=sys.stderr,
                        )
                    continue
                if pending_interaction.kind == "exit":
                    if command in PLAN_APPROVE_TOKENS:
                        await _approve_pending_plan(session, pending_interaction)
                    else:
                        print(
                            "Pending plan approval. Use :approve-plan, :revise-plan <feedback>, or :cancel-plan.",
                            file=sys.stderr,
                        )
                    continue
            result = await session.run_turn(prompt, active_skill_names=_active_skill_names_from_args(args))
            if result.final_text:
                print(result.final_text)
            pending = session.get_pending_approval()
            if pending is not None:
                print(json.dumps({"pending_approval": pending.to_dict()}, indent=2))
            pending_interaction = session.get_pending_interaction()
            if pending_interaction is not None:
                if isinstance(pending_interaction, PendingPlanInteraction):
                    print(_format_pending_plan_interaction(pending_interaction))
                else:
                    print(json.dumps({"pending_interaction": pending_interaction.to_dict()}, indent=2))
    finally:
        await core.shutdown()


async def _open_initial_repl_session(
    core: HostCore,
    args,
    *,
    workspace_input: HostWorkspaceInput,
    resume_workspace_input: HostWorkspaceInput | None,
    permission_policy,
):
    if getattr(args, "resume", None):
        return await core.resume_session(
            args.resume,
            workspace_input=resume_workspace_input,
            model_role=ModelRole(args.model_role),
            permission_policy=permission_policy,
        )
    if getattr(args, "continue_session", False):
        return await core.resume_most_recent_session(
            workspace_input,
            model_role=ModelRole(args.model_role),
            permission_policy=permission_policy,
        )
    return await core.open_session(
        workspace_input,
        model_role=ModelRole(args.model_role),
        permission_policy=permission_policy,
    )


def _repl_prompt_message(session) -> str:
    if session.get_pending_approval() is not None:
        return "approval> "
    pending = session.get_pending_interaction()
    if isinstance(pending, PendingPlanInteraction):
        return "plan> "
    if session.plan_state.active:
        return "plan> "
    return "pulsara> "


_REPL_HELP = """Commands:
  :sessions               List resumable sessions for this workspace
  :resume <session-id>    Detach current HostSession and resume a durable runtime session
  :continue               Detach current HostSession and resume the latest workspace session
  :close                  Explicitly close this durable conversation
  :status                 Show the current permission mode and policy
  :mode <preset>          Switch permission mode
  :plan [reason]          Enter plan mode
  :interaction            Show a pending plan interaction
  :choose <n|label>       Choose a pending plan question option
  :answer <text>          Answer a pending plan question
  :approve-plan           Approve plan exit
  :revise-plan <feedback> Request a plan revision
  :cancel-plan            Cancel the plan workflow from a pending plan draft
  :force-exit-plan        Exit active plan mode without approving a draft
  :approval               Show a pending tool approval
  :approve / :deny        Resolve a pending tool approval
  :stop                   Stop the current active or suspended turn
  :compact                Manually compact idle session context before the auto threshold
  :q / quit / exit        Detach from the conversation

Editing: Up/Down history · Ctrl-R search · Ctrl-C clear · Ctrl-Z suspend · Ctrl-D exit"""


async def _host_inspect(args) -> dict[str, object]:
    settings = _settings_from_host_args(args)
    workspace = resolve_workspace(_workspace_input_from_args(args))
    permission_policy = _permission_policy_from_host_args(args, intent="inspect")
    wiring = build_durable_runtime_wiring(
        settings,
        workspace.workspace_root,
        memory_domain=workspace.memory_domain,
    )
    runtime_session = wiring.runtime_session
    try:
        registry = build_core_tool_registry(
            runtime_session,
            permission_state=PermissionState.from_policy(permission_policy),
        )
        capability_runtime = CapabilityRuntime.with_default_providers(
            LocalSkillCapabilityProvider(
                skill_health_resolver=SkillHealthResolver(
                    path_supplier=_terminal_path_supplier(runtime_session),
                )
            )
        )
        exposure = capability_runtime.resolve_for_turn(
            CapabilityResolveContext(
                workspace_root=workspace.workspace_root,
                workspace_kind=workspace.workspace_kind,
                memory_domain=workspace.memory_domain,
                available_tool_names=frozenset(registry.names()),
                user_input="",
            ),
            tool_registry=registry,
            permission_policy=permission_policy,
            plan_active=False,
        )
    finally:
        runtime_session.close()
    return {
        "inspect_kind": "static_workspace_capability",
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
        "capability_surface": {
            "registry_generation": exposure.registry_generation,
            "direct_names": sorted(exposure.direct_names),
            "deferred_names": sorted(exposure.deferred_names),
            "hidden_names": sorted(exposure.hidden_names),
            "callable_names": sorted(exposure.callable_names),
            "descriptors": [
                descriptor.to_diagnostic_dict()
                for descriptor in sorted(exposure.descriptors_by_name.values(), key=lambda item: item.name)
            ],
            "diagnostics": [diagnostic.to_dict() for diagnostic in exposure.diagnostics],
        },
        "permissions": permission_policy.to_dict(),
        "current_mode": (
            mode_for_policy(permission_policy).value
            if mode_for_policy(permission_policy) is not None
            else "custom"
        ),
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
                **_skill_cli_hint_snapshot(entry),
            }
            for entry in exposure.catalog_entries
        ],
        "active_skills": [
            {
                "name": injection.name,
                "location": injection.location,
                "reason": injection.reason,
            }
            for injection in exposure.active_injections
        ],
        "capability_diagnostics": [diagnostic.to_dict() for diagnostic in exposure.diagnostics],
        "bundled_skills": bundled_skills_status().to_dict(),
    }


def _skill_cli_hint_snapshot(entry) -> dict[str, object]:
    snapshot: dict[str, object] = {}
    if entry.suggested_tools:
        snapshot["suggested_tools"] = list(entry.suggested_tools)
    if entry.required_binaries:
        snapshot["required_binaries"] = list(entry.required_binaries)
    if entry.optional_binaries:
        snapshot["optional_binaries"] = list(entry.optional_binaries)
    if entry.external_services:
        snapshot["external_services"] = list(entry.external_services)
    if entry.network_required:
        snapshot["network_required"] = True
    if entry.auth_required != "none":
        snapshot["auth_required"] = entry.auth_required
    if entry.cli_usage_kind != "none":
        snapshot["cli_usage_kind"] = entry.cli_usage_kind
    return snapshot


def _terminal_path_supplier(runtime_session):
    def supplier() -> SkillBinaryLookupPath:
        terminal_sessions = runtime_session.terminal_sessions
        shell = terminal_sessions.shell
        if shell is None:
            return SkillBinaryLookupPath(path=None, source="Pulsara process PATH")
        result = terminal_sessions.env_builder.build(
            cwd=runtime_session.workspace_root,
            workspace_root=runtime_session.workspace_root,
            shell=shell,
        )
        source = (
            "terminal PATH"
            if not result.diagnostics.get("shell_snapshot_error")
            else "terminal PATH with shell snapshot fallback"
        )
        return SkillBinaryLookupPath(path=result.env.get("PATH"), source=source)

    return supplier


def _settings_from_host_args(args) -> PulsaraSettings:
    if args.env_file:
        return PulsaraSettings.from_env_file(
            args.env_file,
            prefix=args.prefix,
            override=args.override_env,
        )
    return PulsaraSettings.from_env(prefix=args.prefix)


def _format_not_found_error(exc: KeyError) -> str:
    raw = exc.args[0] if exc.args else "not found"
    text = str(raw)
    if text == "no resumable runtime session found":
        return "no resumable runtime session found for this workspace. Start a new REPL without --continue, or use --resume <runtime_session_id>."
    return f"not found: {text}"


def _settings_from_inspect_args(args) -> PulsaraSettings:
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
        workspace_kind=normalize_workspace_kind(args.workspace_kind or "project"),
        workspace_root=Path(args.workspace or "."),
        display_label=args.display_label,
        memory_domain_id=args.memory_domain_id or "u_local",
    )


def _has_explicit_workspace_override(args) -> bool:
    return any(
        getattr(args, name, None) is not None
        for name in ("workspace", "workspace_kind", "display_label", "memory_domain_id")
    )


def _active_skill_names_from_args(args) -> frozenset[str]:
    return frozenset(name.strip() for name in getattr(args, "skill", ()) if name.strip())


def _env_permission_mode(prefix: str) -> str | None:
    value = os.environ.get(f"{prefix}_PERMISSION_MODE")
    if value is None:
        return None
    value = value.strip()
    return value or None


def _permission_policy_from_host_args(args, *, intent: str):
    prefix = getattr(args, "prefix", "PULSARA")
    raw_profile = getattr(args, "permission_profile", None)
    raw_approval = getattr(args, "approval_policy", None)
    raw_terminal = getattr(args, "terminal_access", None)
    mode = getattr(args, "permission_mode", None) or _env_permission_mode(prefix)

    if mode is not None:
        custom_flags = [
            flag
            for flag, value in (
                ("--permission-profile", raw_profile),
                ("--approval-policy", raw_approval),
                ("--terminal-access", raw_terminal),
            )
            if value is not None
        ]
        if custom_flags:
            print(
                "ERROR: --permission-mode cannot be combined with the advanced flag(s): "
                f"{', '.join(custom_flags)}. Use a preset OR the custom three-axis flags, not both.",
                file=sys.stderr,
            )
            raise SystemExit(2)
        try:
            return preset_to_policy(parse_permission_mode(mode))
        except ValueError as exc:
            print(f"ERROR: {exc}", file=sys.stderr)
            raise SystemExit(2) from exc

    return resolve_permission_policy(
        intent=intent,
        profile=raw_profile,
        approval=raw_approval,
        terminal=raw_terminal,
        prefix=prefix,
    )
