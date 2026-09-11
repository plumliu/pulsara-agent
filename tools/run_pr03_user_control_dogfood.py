"""Real-provider PR03 user-control and background-command dogfood.

The probe reads one exact saved model connection, owns a verified disposable
clean-v0 PostgreSQL database and controlled temporary processes, and records
the concrete product identities needed to audit PR03.  Saved settings remain
read-only and configured API-key values are scrubbed from the report.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
from dataclasses import replace
from datetime import datetime, timezone
import json
from pathlib import Path
import shlex
import sys
from tempfile import TemporaryDirectory
from time import monotonic
from typing import Any, Callable

from psycopg.rows import dict_row

from pulsara_agent.conversation_kernel.host import KernelHostCore
from pulsara_agent.conversation_kernel.subagents import SubagentTaskStatus
from pulsara_agent.conversation_kernel.user_control import (
    FeedbackCanonicalStatus,
    FeedbackInclusionStatus,
    UserControlOperation,
    UserControlRequest,
    UserControlTarget,
    UserControlTargetKind,
)
from pulsara_agent.llm.model_catalog import ModelCatalogOwner, ModelsDevCatalogClient
from pulsara_agent.llm.runtime import ModelRuntime
from pulsara_agent.primitives.context import thaw_json
from pulsara_agent.primitives.permission import PermissionMode
from pulsara_agent.settings import LocalPostgresConfig, LocalSettingsStore
from pulsara_agent.storage.postgres_connection_provider import PostgresConnectionLane
from pulsara_agent.workspace_identity import HostWorkspaceInput

from run_content_revision_line_edit_dogfood import _scrub
from run_model_switch_handover_dogfood import (
    _ReadOnlySettingsStore,
    _RecordingModelRuntime,
    _RecordingTransport,
    _binding,
    _create_database,
    _drop_database,
)


SCHEMA_VERSION = "pr03-user-control-dogfood.v1"


class _Pr03RecordingTransport(_RecordingTransport):
    def open_stream(self, *, call, context):
        execution = super().open_stream(call=call, context=context)
        plan = context.provider_wire_input_plan
        if plan is None:
            raise RuntimeError("real provider request lacks a final wire plan")
        self._records[-1]["wire_input"] = thaw_json(
            plan.materialization.context_bearing_projection
        )
        return execution


class _Pr03RecordingRuntime(_RecordingModelRuntime):
    def resolve_target(self, binding, *, timeout_policy):
        target = self._delegate.resolve_target(binding, timeout_policy=timeout_policy)
        return replace(
            target,
            transport=_Pr03RecordingTransport(target.transport, self._records, None),
        )


class _DelayedTurnOutcomeIO:
    def __init__(self, delegate: Any, turn_id: str) -> None:
        self._delegate = delegate
        self._turn_id = turn_id
        self.entered = asyncio.Event()
        self.release = asyncio.Event()

    async def run(self, operation, /, *args, deadline_monotonic, **kwargs):
        if (
            getattr(operation, "__name__", "") == "read_turn_terminal_outcome"
            and kwargs.get("turn_id") == self._turn_id
            and not self.release.is_set()
        ):
            self.entered.set()
            await self.release.wait()
        return await self._delegate.run(
            operation, *args, deadline_monotonic=deadline_monotonic, **kwargs
        )

    async def aclose(self, *args, **kwargs):
        return await self._delegate.aclose(*args, **kwargs)


def _saved_connection(saved, connection_id: str):
    matches = tuple(
        item for item in saved.model_connections if item.id.value == connection_id
    )
    if len(matches) != 1:
        raise RuntimeError(f"saved connection {connection_id!r} is unavailable")
    connection = matches[0]
    if saved.model_api_key(connection.id) is None:
        raise RuntimeError(f"saved connection {connection_id!r} has no API key")
    return connection


def _json_default(value: object) -> object:
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, bytes):
        try:
            return value.decode("utf-8")
        except UnicodeDecodeError:
            return {
                "encoding": "base64",
                "data": base64.b64encode(value).decode("ascii"),
            }
    raise TypeError(f"Object of type {type(value).__name__} is not JSON serializable")


def _shell_python(source: str) -> str:
    return f"{shlex.quote(sys.executable)} -u -c {shlex.quote(source)}"


def _rows(session, statement: str, params: tuple[object, ...]) -> list[dict[str, Any]]:
    with session.repository.connection_provider.connection(
        lane=PostgresConnectionLane.INSPECTOR,
        row_factory=dict_row,
        deadline_monotonic=monotonic() + 30,
    ) as connection:
        return [dict(row) for row in connection.execute(statement, params).fetchall()]


async def _wait_until(
    description: str,
    read: Callable[[], Any],
    accept: Callable[[Any], bool],
    *,
    timeout_seconds: float = 90,
) -> Any:
    deadline = monotonic() + timeout_seconds
    latest = None
    while monotonic() < deadline:
        latest = await read()
        if accept(latest):
            return latest
        await asyncio.sleep(0.05)
    raise TimeoutError(f"{description} did not settle; latest={latest!r}")


async def _wait_active_turn(session, *, different_from: str | None = None) -> str:
    return await _wait_until(
        "active ROOT turn",
        lambda: asyncio.sleep(0, result=session.active_root_turn_id()),
        lambda value: isinstance(value, str) and value != different_from,
    )


async def _wait_tool_command(session, command: str) -> dict[str, Any]:
    async def read():
        return await asyncio.to_thread(
            _rows,
            session,
            """
            SELECT a.id AS attempt_id, b.tool_call_id, b.tool_arguments,
                   r.id AS result_entry_id
            FROM pulsara_v3.tool_execution_attempts AS a
            JOIN pulsara_v3.assistant_message_blocks AS b
              ON b.session_id = a.session_id
             AND b.assistant_entry_id = a.assistant_entry_id
             AND b.tool_call_id = a.tool_call_id
            LEFT JOIN pulsara_v3.tool_results AS r
              ON r.session_id = a.session_id AND r.attempt_id = a.id
            WHERE a.session_id = %s AND b.tool_name = 'terminal'
              AND b.tool_arguments->>'command' = %s
            ORDER BY a.started_at DESC LIMIT 1
            """,
            (session.session_id, command),
        )

    rows = await _wait_until(
        f"terminal attempt for {command!r}", read, lambda value: bool(value)
    )
    return rows[0]


async def _wait_processes(session, commands: set[str]):
    async def read():
        return session.list_background_processes(
            expected_session_id=session.session_id,
            expected_host_session_id=session.host_session_id,
        )

    return await _wait_until(
        "background process adoption",
        read,
        lambda values: commands <= {item.command for item in values},
    )


def _control_request(
    session,
    *,
    operation: UserControlOperation,
    target_kind: UserControlTargetKind,
    target_id: str,
    suffix: str,
) -> UserControlRequest:
    return UserControlRequest(
        operation,
        f"command:control:{session.control_admission_deadline_ms()}:{suffix}",
        session.session_id,
        session.host_session_id,
        UserControlTarget(target_kind, target_id),
    )


async def _wait_control(session, request: UserControlRequest, *, feedback: bool):
    async def read():
        return await session.query_control_command(request)

    def settled(query) -> bool:
        outcome = query.outcome
        if outcome is None or outcome.status == "PENDING":
            return False
        control = outcome.user_control
        if not feedback or control is None or control.feedback is None:
            return True
        state = control.feedback
        if state.canonical_status is FeedbackCanonicalStatus.PENDING:
            return False
        if state.inclusion_status is FeedbackInclusionStatus.PENDING:
            return False
        return not (
            state.inclusion_status is FeedbackInclusionStatus.INCLUDED
            and not state.transport.invocation_attempted
        )

    query = await _wait_until("control result", read, settled, timeout_seconds=120)
    if query.outcome is None:
        raise RuntimeError(f"control result unavailable: {query.status.value}")
    return query.outcome


def _process_fact(info) -> dict[str, object]:
    return {
        "process_id": info.process_id,
        "command": info.command,
        "cwd": info.cwd,
        "status": info.status,
        "exit_code": info.exit_code,
        "physical_state": info.physical_state,
        "background_adopted": info.background_adopted,
        "origin_turn_id": info.origin.turn_id,
        "origin_subagent_task_id": info.origin.scope_subagent_task_id,
        "stream_id": info.stream_id,
        "output_cursor": info.output_cursor,
        "retained_from_cursor": info.retained_from_cursor,
    }


def _control_fact(outcome) -> dict[str, object]:
    control = outcome.user_control
    if control is None:
        raise RuntimeError("control outcome lacks its typed result")
    feedback = control.feedback
    return {
        "command_id": outcome.command_id,
        "status": outcome.status,
        "public_code": outcome.public_code,
        "target_kind": control.target.kind.value,
        "target_id": control.target.target_id,
        "accepted": control.accepted,
        "execution": control.execution.value,
        "root": None
        if control.root is None
        else {"status": control.root.status, "reason": control.root.reason},
        "subagent": None
        if control.subagent is None
        else {
            "disposition": control.subagent.disposition,
            "status": control.subagent.status,
            "reason": control.subagent.reason,
        },
        "process": None
        if control.process is None
        else {
            "disposition": control.process.disposition,
            "status": control.process.status,
            "exit_code": control.process.exit_code,
            "physical_state": control.process.physical_state,
            "group_alive": control.process.group_alive,
        },
        "monitor": None
        if control.monitor is None
        else {
            "monitor_id": control.monitor.monitor_id,
            "outcome": control.monitor.outcome,
            "in_flight_observation_ids": list(
                control.monitor.in_flight_observation_ids
            ),
            "detail": control.monitor.detail,
        },
        "feedback": None
        if feedback is None
        else {
            "canonical_status": feedback.canonical_status.value,
            "inclusion_status": feedback.inclusion_status.value,
            "owner_availability": feedback.owner_availability.value,
            "reason": feedback.reason,
            "target_root_turn_id": feedback.target_root_turn_id,
            "entry_id": feedback.entry_id,
            "context_binding_revision_id": feedback.context_binding_revision_id,
            "model_call_index": feedback.model_call_index,
            "transport_invocation_attempted": feedback.transport.invocation_attempted,
            "transport_invocation_succeeded": feedback.transport.invocation_succeeded,
            "transport_detail": feedback.transport.detail,
        },
    }


async def _run_stop_race(session) -> dict[str, object]:
    hold = _shell_python("import time; print('STOP_A_STARTED', flush=True); time.sleep(60)")
    turn_a_task = asyncio.create_task(
        session.run_turn(
            "Call terminal exactly once with command "
            f"{hold!r}, yield_time_ms 30000 and max_output_chars 2000. "
            "Wait for that tool call and do not call anything else.",
            command_id="command:pr03:stop-race-a",
            requested_permission_mode=PermissionMode.BYPASS_PERMISSIONS,
        )
    )
    turn_a = await _wait_active_turn(session)
    await _wait_tool_command(session, hold)
    queued = await session.submit_prompt(
        command_id="command:pr03:stop-race-b",
        text="Reply exactly B_CONTINUED. Do not call tools.",
        requested_permission_mode=PermissionMode.BYPASS_PERMISSIONS,
    )
    delayed_io = _DelayedTurnOutcomeIO(session._io, turn_a)  # noqa: SLF001
    session._io = delayed_io  # noqa: SLF001
    request = _control_request(
        session,
        operation=UserControlOperation.STOP_ACTIVE_TURN,
        target_kind=UserControlTargetKind.ROOT_TURN,
        target_id=turn_a,
        suffix="stop-race-a",
    )
    accepted = await session.request_stop_turn(
        command_id=request.command_id,
        expected_session_id=request.session_id,
        expected_host_session_id=request.host_session_id,
        target_turn_id=turn_a,
    )
    if accepted.status != "PENDING":
        raise RuntimeError("STOP A was not accepted asynchronously")
    await delayed_io.entered.wait()
    turn_b = await _wait_active_turn(session, different_from=turn_a)
    delayed_io.release.set()
    stop = await _wait_control(session, request, feedback=False)
    await asyncio.gather(turn_a_task, return_exceptions=True)
    await _wait_until(
        "queued ROOT B completion",
        lambda: asyncio.sleep(0, result=session.active_root_turn_id()),
        lambda value: value is None,
        timeout_seconds=120,
    )
    turn_rows = await asyncio.to_thread(
        _rows,
        session,
        "SELECT id, status, terminal_reason FROM pulsara_v3.turns "
        "WHERE session_id=%s AND id=ANY(%s) ORDER BY accepted_at",
        (session.session_id, [turn_a, turn_b]),
    )
    by_id = {str(row["id"]): row for row in turn_rows}
    if by_id[turn_a]["status"] != "INTERRUPTED":
        raise RuntimeError("STOP A did not interrupt exact ROOT A")
    if by_id[turn_b]["status"] != "COMPLETED":
        raise RuntimeError("late STOP A changed ROOT B")
    return {
        "turn_a": turn_a,
        "turn_b": turn_b,
        "queued_delivery": queued.prompt_delivery.queue_status
        if queued.prompt_delivery
        else None,
        "control": _control_fact(stop),
        "turns": turn_rows,
    }


async def _run_background_controls(session) -> dict[str, object]:
    command_a = _shell_python(
        "import time; print('BACKGROUND_NO_MONITOR', flush=True); time.sleep(120)"
    )
    command_b = _shell_python(
        "import time; print('BACKGROUND_WITH_MONITOR', flush=True); time.sleep(120)"
    )
    start = await session.run_turn(
        "Run this controlled sequence exactly. First call terminal with command "
        f"{command_a!r}, yield_time_ms 50, max_output_chars 2000. Then call terminal "
        f"with command {command_b!r}, yield_time_ms 50, max_output_chars 2000. "
        "Both must return running. Register terminal_monitor for only the second "
        "process with action register and otherwise default settings. Do not poll or "
        "kill either process. Finish with one short sentence.",
        command_id="command:pr03:background-start",
        requested_permission_mode=PermissionMode.BYPASS_PERMISSIONS,
    )
    processes = await _wait_processes(session, {command_a, command_b})
    by_command = {item.command: item for item in processes}
    process_a = by_command[command_a]
    process_b = by_command[command_b]
    monitors = session._tools.terminal_monitor_coordinator.list_current()  # noqa: SLF001
    if len(monitors) != 1 or monitors[0]["process_id"] != process_b.process_id:
        raise RuntimeError("real provider did not register the exact second monitor")

    original_before_prepare = session._runner._before_provider_preparation  # noqa: SLF001
    safe_point_entered = asyncio.Event()
    release_safe_point = asyncio.Event()
    preparation_count = 0

    async def hold_second_provider_preparation() -> bool:
        nonlocal preparation_count
        preparation_count += 1
        changed = False
        if original_before_prepare is not None:
            changed = await original_before_prepare()
        if preparation_count == 2:
            safe_point_entered.set()
            await release_safe_point.wait()
        return changed

    session._runner._before_provider_preparation = hold_second_provider_preparation  # noqa: SLF001
    delay = _shell_python("import time; print('FEEDBACK_GATE', flush=True); time.sleep(4)")
    try:
        active = asyncio.create_task(
            session.run_turn(
                "Call terminal exactly once with command "
                f"{delay!r}, yield_time_ms 10000 and max_output_chars 2000. "
                "After it returns, summarize any typed user-control feedback visible in "
                "your next request. Do not call other tools.",
                command_id="command:pr03:feedback-active-root",
                requested_permission_mode=PermissionMode.BYPASS_PERMISSIONS,
            )
        )
        target_turn = await _wait_active_turn(session)
        await _wait_tool_command(session, delay)
        requests: list[UserControlRequest] = []
        accepted_controls = []
        for label, process in (
            ("with-monitor", process_b),
            ("without-monitor", process_a),
        ):
            request = _control_request(
                session,
                operation=UserControlOperation.TERMINATE_BACKGROUND_PROCESS,
                target_kind=UserControlTargetKind.BACKGROUND_PROCESS,
                target_id=process.process_id,
                suffix=label,
            )
            accepted = await session.request_terminate_background_process(
                command_id=request.command_id,
                expected_session_id=request.session_id,
                expected_host_session_id=session.host_session_id,
                process_id=process.process_id,
            )
            if accepted.status != "PENDING":
                raise RuntimeError(f"background control {label} was not accepted")
            requests.append(request)
            accepted_controls.append(_control_fact(accepted))
        await asyncio.wait_for(safe_point_entered.wait(), timeout=30)

        async def read_feedback_states():
            return [await session.query_control_command(request) for request in requests]

        canonical_queries = await _wait_until(
            "canonical user-control feedback at the injected safe point",
            read_feedback_states,
            lambda values: any(
                query.outcome is not None
                and query.outcome.user_control is not None
                and query.outcome.user_control.feedback is not None
                and query.outcome.user_control.feedback.canonical_status
                is FeedbackCanonicalStatus.ACCEPTED
                for query in values
            ),
        )
        canonical_at_gate = [
            _control_fact(query.outcome)
            for query in canonical_queries
            if query.outcome is not None
        ]
        release_safe_point.set()
        active_result = await active
    finally:
        release_safe_point.set()
        session._runner._before_provider_preparation = original_before_prepare  # noqa: SLF001
    settled = [
        _control_fact(await _wait_control(session, request, feedback=True))
        for request in requests
    ]
    if any(
        item["feedback"] is None
        or item["feedback"]["target_root_turn_id"] != target_turn  # type: ignore[index]
        for item in settled
    ):
        raise RuntimeError("active feedback did not stay bound to its exact ROOT")
    if not any(
        isinstance(item["feedback"], dict)
        and item["feedback"]["canonical_status"] == "ACCEPTED"  # type: ignore[index]
        and item["feedback"]["inclusion_status"] == "INCLUDED"  # type: ignore[index]
        and item["feedback"]["transport_invocation_attempted"] is True  # type: ignore[index]
        for item in settled
    ):
        raise RuntimeError("active ROOT did not receive a provider-bound control fact")

    idle_long = _shell_python(
        "import time; print('IDLE_LONG', flush=True); time.sleep(120)"
    )
    natural = _shell_python(
        "import time; print('NATURAL_SHORT', flush=True); time.sleep(1)"
    )
    idle_start = await session.run_turn(
        "Call terminal for each command exactly once and do not register monitors. "
        f"First command {idle_long!r}, yield_time_ms 50. Second command {natural!r}, "
        "yield_time_ms 50. Use max_output_chars 2000 for both, then stop.",
        command_id="command:pr03:idle-processes",
        requested_permission_mode=PermissionMode.BYPASS_PERMISSIONS,
    )
    idle_processes = await _wait_processes(session, {idle_long, natural})
    idle_by_command = {item.command: item for item in idle_processes}
    natural_process = idle_by_command[natural]

    async def natural_read():
        values = session.list_background_processes(
            expected_session_id=session.session_id,
            expected_host_session_id=session.host_session_id,
        )
        return next(item for item in values if item.process_id == natural_process.process_id)

    natural_done = await _wait_until(
        "natural process completion",
        natural_read,
        lambda item: item.status != "running",
    )
    before_idle = await asyncio.to_thread(
        _rows,
        session,
        "SELECT (SELECT count(*) FROM pulsara_v3.turns WHERE session_id=%s) AS turns, "
        "(SELECT count(*) FROM pulsara_v3.transcript_entries WHERE session_id=%s "
        "AND entry_kind='USER_CONTROL_FEEDBACK') AS feedback",
        (session.session_id, session.session_id),
    )
    idle_controls = []
    for label, process in (
        ("idle-long", idle_by_command[idle_long]),
        ("natural-late", natural_done),
    ):
        request = _control_request(
            session,
            operation=UserControlOperation.TERMINATE_BACKGROUND_PROCESS,
            target_kind=UserControlTargetKind.BACKGROUND_PROCESS,
            target_id=process.process_id,
            suffix=label,
        )
        await session.request_terminate_background_process(
            command_id=request.command_id,
            expected_session_id=request.session_id,
            expected_host_session_id=request.host_session_id,
            process_id=process.process_id,
        )
        idle_controls.append(
            _control_fact(await _wait_control(session, request, feedback=False))
        )
    after_idle = await asyncio.to_thread(
        _rows,
        session,
        "SELECT (SELECT count(*) FROM pulsara_v3.turns WHERE session_id=%s) AS turns, "
        "(SELECT count(*) FROM pulsara_v3.transcript_entries WHERE session_id=%s "
        "AND entry_kind='USER_CONTROL_FEEDBACK') AS feedback",
        (session.session_id, session.session_id),
    )
    if before_idle != after_idle:
        raise RuntimeError("idle process control created a ROOT or feedback entry")
    natural_control = idle_controls[1]["process"]
    if (
        not isinstance(natural_control, dict)
        or natural_control["disposition"] != "ALREADY_TERMINAL"
        or natural_control["status"] != "success"
    ):
        raise RuntimeError("late natural-process control rewrote its terminal status")

    return {
        "start_reply": start.final_text,
        "active_reply": active_result.final_text,
        "idle_start_reply": idle_start.final_text,
        "initial_processes": [_process_fact(process_a), _process_fact(process_b)],
        "monitor_before_control": list(monitors),
        "safe_point_gate": {
            "preparation_count": preparation_count,
            "canonical_at_gate": canonical_at_gate,
        },
        "accepted_controls": accepted_controls,
        "settled_controls": settled,
        "idle_controls": idle_controls,
        "idle_counts_before": before_idle[0],
        "idle_counts_after": after_idle[0],
    }


async def _task_rows(session) -> list[dict[str, Any]]:
    return await session._io.run(  # noqa: SLF001
        session.repository.list_subagent_tasks,
        session_id=session.session_id,
        maximum_items=50,
        deadline_monotonic=monotonic() + 30,
    )


async def _run_task_controls(session, workspace: Path) -> dict[str, object]:
    release = workspace / "pr03-subagent-release"
    hold = f"while [ ! -f {shlex.quote(str(release))} ]; do sleep 0.1; done"
    arguments = {
        "tasks": [
            {
                "task_key": "slow",
                "label": "Slow worker",
                "profile": "general_worker",
                "task": (
                    f"Call terminal once with command {hold!r}, yield_time_ms "
                    "30000, then call report_agent_result alone with summary SLOW_DONE."
                ),
            },
            {
                "task_key": "waiting",
                "label": "Waiting worker",
                "profile": "general_worker",
                "depends_on": ["slow"],
                "task": "Call report_agent_result alone with summary WAITING_DONE.",
            },
            {
                "task_key": "unrelated",
                "label": "Unrelated worker",
                "profile": "general_worker",
                "task": (
                    "Call report_agent_result alone with summary UNRELATED_OK."
                ),
            },
        ]
    }
    prompt = (
        "Call create_agent_tasks exactly once with this exact JSON argument, then "
        "return immediately without list_agents or wait_agent: "
        + json.dumps(arguments, ensure_ascii=False, separators=(",", ":"))
    )
    root_result = await session.run_turn(
        prompt,
        command_id="command:pr03:task-group",
        requested_permission_mode=PermissionMode.BYPASS_PERMISSIONS,
    )

    async def read_three():
        return await _task_rows(session)

    rows = await _wait_until(
        "three-task group",
        read_three,
        lambda values: {row.get("task_key") for row in values}
        >= {"slow", "waiting", "unrelated"},
        timeout_seconds=120,
    )
    by_key = {str(row["task_key"]): row for row in rows if row.get("task_key")}
    cancel_results = []
    for key in ("waiting", "slow"):
        task_id = str(by_key[key]["id"])
        request = _control_request(
            session,
            operation=UserControlOperation.CANCEL_SUBAGENT_TASK,
            target_kind=UserControlTargetKind.SUBAGENT_TASK,
            target_id=task_id,
            suffix=f"task-{key}",
        )
        accepted = await session.request_cancel_subagent(
            command_id=request.command_id,
            expected_session_id=request.session_id,
            expected_host_session_id=request.host_session_id,
            task_id=task_id,
        )
        if accepted.status == "PENDING":
            outcome = await _wait_control(session, request, feedback=False)
        else:
            outcome = accepted
        cancel_results.append(_control_fact(outcome))
    release.touch()

    async def read_terminal_tasks():
        return await _task_rows(session)

    final_group = await _wait_until(
        "task group terminal settlement",
        read_terminal_tasks,
        lambda values: all(
            SubagentTaskStatus(str(row["status"])).terminal
            for row in values
            if row.get("task_key") in {"slow", "waiting", "unrelated"}
        ),
        timeout_seconds=180,
    )
    final_by_key = {
        str(row["task_key"]): row
        for row in final_group
        if row.get("task_key") in {"slow", "waiting", "unrelated"}
    }
    if final_by_key["unrelated"]["status"] != "COMPLETED":
        raise RuntimeError("unrelated task did not continue after sibling cancellation")

    single_result = await session.run_turn(
        "Call spawn_agent exactly once with task_name single_worker and objective "
        "'Call report_agent_result alone with summary SINGLE_OK'. Return immediately.",
        command_id="command:pr03:single-task",
        requested_permission_mode=PermissionMode.BYPASS_PERMISSIONS,
    )

    async def read_single():
        return await _task_rows(session)

    single_rows = await _wait_until(
        "single spawn completion",
        read_single,
        lambda values: any(
            row.get("task_key") == "single_worker"
            and row.get("status") == "COMPLETED"
            for row in values
        ),
        timeout_seconds=180,
    )
    single = next(row for row in single_rows if row.get("task_key") == "single_worker")

    completion_command = "command:pr03:accept-completion"
    completion = await session.accept_subagent_completion(
        command_id=completion_command,
        target_turn_id=None,
        requested_permission_mode=PermissionMode.BYPASS_PERMISSIONS,
        task_id=str(final_by_key["unrelated"]["id"]),
        actor_id="pr03-dogfood-controller",
    )
    if completion.status != "SUCCEEDED":
        raise RuntimeError("completed task result was not accepted into a new ROOT")
    await _wait_until(
        "completion continuation ROOT",
        lambda: asyncio.sleep(0, result=session.active_root_turn_id()),
        lambda value: value is None,
        timeout_seconds=180,
    )

    groups = await session._io.run(  # noqa: SLF001
        session.repository.list_subagent_task_groups,
        session_id=session.session_id,
        after_first_accepted_at=None,
        after_batch_id=None,
        maximum_items=50,
        deadline_monotonic=monotonic() + 30,
    )
    activities = await session._io.run(  # noqa: SLF001
        session.repository.list_subagent_task_activities,
        session_id=session.session_id,
        task_id=str(final_by_key["unrelated"]["id"]),
        after_entry_sequence=0,
        maximum_items=50,
        deadline_monotonic=monotonic() + 30,
    )
    return {
        "group_root_reply": root_result.final_text,
        "single_root_reply": single_result.final_text,
        "cancellations": cancel_results,
        "group_tasks": [dict(row) for row in final_by_key.values()],
        "single_task": dict(single),
        "completion_command": {
            "command_id": completion_command,
            "status": completion.status,
            "public_code": completion.public_code,
            "target_id": completion.target_id,
        },
        "task_groups": [dict(row) for row in groups],
        "unrelated_task_activities": {
            "entries": [dict(row) for row in activities[0]],
            "blocks": [dict(row) for row in activities[1]],
            "tool_results": [dict(row) for row in activities[2]],
            "has_more": activities[3],
        },
    }


async def _run(connection_id: str) -> dict[str, object]:
    saved = LocalSettingsStore().read()
    connection = _saved_connection(saved, connection_id)
    database_name, _admin_root, ephemeral_admin, ephemeral_runtime = _create_database(
        saved
    )
    try:
        with TemporaryDirectory(prefix="pulsara-pr03-dogfood-") as directory:
            workspace = Path(directory).resolve()
            runtime_settings = replace(
                saved,
                postgres=LocalPostgresConfig(ephemeral_runtime, ephemeral_admin),
            )
            settings_store = _ReadOnlySettingsStore(runtime_settings)
            catalog = ModelCatalogOwner(ModelsDevCatalogClient())
            await catalog.refresh()
            delegate = ModelRuntime.production(settings=settings_store, catalog=catalog)
            provider_calls: list[dict[str, object]] = []
            runtime = _Pr03RecordingRuntime(delegate, provider_calls, None)
            core = KernelHostCore.production(model_runtime=runtime)
            session = None
            installed_inputs: list[dict[str, object]] = []
            try:
                session = await core.open_session(
                    HostWorkspaceInput(
                        workspace_kind="project",
                        workspace_root=workspace,
                        trust_workspace_mcp_config=False,
                    ),
                    system_prompt=(
                        "This is a controlled Pulsara PR03 product test. Execute only "
                        "the exact tools requested by each human message, preserve exact "
                        "IDs and commands, never operate on any unlisted process, and keep "
                        "final prose short."
                    ),
                )
                await session.update_model_call_binding(_binding(delegate, connection))
                original_observer = session._runner._provider_input_installed_observer  # noqa: SLF001

                def observe_input(request) -> None:
                    installed_inputs.append(
                        {
                            "turn_id": request.turn_id,
                            "scope": request.compiled_input.canonical_input_identity.conversation_scope_kind.value,
                            "context_binding_revision_id": request.cut.context_binding_revision_id,
                            "model_call_index": request.model_call_index,
                            "messages": [
                                {
                                    "role": message.role.value,
                                    "content": list(message.content),
                                    "origin_entry_id": placement.origin_entry_id,
                                }
                                for message, placement in zip(
                                    request.compiled_input.messages,
                                    request.compiled_input.message_placements,
                                    strict=True,
                                )
                            ],
                        }
                    )
                    if original_observer is not None:
                        original_observer(request)

                session._runner._provider_input_installed_observer = observe_input  # noqa: SLF001
                stop_race = await _run_stop_race(session)
                background = await _run_background_controls(session)
                tasks = await _run_task_controls(session, workspace)
                canonical = await asyncio.to_thread(
                    _rows,
                    session,
                    """
                    SELECT e.id AS entry_id, e.entry_sequence, e.turn_id,
                           convert_from(e.inline_content, 'UTF8') AS content,
                           ev.event_id, ev.event_type
                    FROM pulsara_v3.transcript_entries AS e
                    JOIN pulsara_v3.agent_events AS ev
                      ON ev.session_id=e.session_id
                     AND ev.subject_entry_id=e.id
                    WHERE e.session_id=%s AND e.entry_kind='USER_CONTROL_FEEDBACK'
                    ORDER BY e.entry_sequence
                    """,
                    (session.session_id,),
                )
                if not canonical or any(
                    row["event_type"] != "UserControlFeedbackAccepted"
                    for row in canonical
                ):
                    raise RuntimeError("active controls did not create an exact feedback pair")
                return {
                    "schema_version": SCHEMA_VERSION,
                    "status": "passed",
                    "completed_at_utc": datetime.now(timezone.utc).isoformat(),
                    "connection_id": connection.id.value,
                    "model_id": connection.target.model_id,
                    "wire_api": connection.target.wire_api,
                    "session_id": session.session_id,
                    "host_session_id": session.host_session_id,
                    "clean_v0_ephemeral_database": True,
                    "stop_race": stop_race,
                    "background_controls": background,
                    "task_controls": tasks,
                    "canonical_feedback": canonical,
                    "provider_input_installs": installed_inputs,
                    "provider_calls": provider_calls,
                }
            finally:
                if session is not None:
                    await core.close_session(
                        session.host_session_id, close_conversation=True
                    )
                await core.shutdown()
    finally:
        _drop_database(saved, database_name)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--connection-id", required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()
    saved = LocalSettingsStore().read()
    secrets = tuple(item.value for item in saved.model_api_keys)
    try:
        report = asyncio.run(_run(args.connection_id))
    except BaseException as exc:
        report = {
            "schema_version": SCHEMA_VERSION,
            "status": "failed",
            "completed_at_utc": datetime.now(timezone.utc).isoformat(),
            "failure_type": type(exc).__name__,
            "failure_message": str(exc),
        }
    scrubbed = _scrub(report, secrets)
    encoded = json.dumps(
        scrubbed,
        default=_json_default,
        ensure_ascii=False,
        indent=2,
        sort_keys=True,
    )
    if any(secret and secret in encoded for secret in secrets):
        raise RuntimeError("PR03 dogfood report retained a configured API key")
    args.output_dir.mkdir(parents=True, exist_ok=True)
    (args.output_dir / "real-provider.json").write_text(
        encoded + "\n", encoding="utf-8"
    )
    print(encoded)
    return 0 if report.get("status") == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
