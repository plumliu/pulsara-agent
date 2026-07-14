import asyncio
import json
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path
from threading import Barrier
from uuid import uuid4

import psycopg
import pytest
from psycopg.rows import dict_row

from tests.conftest import run_end_contract_fields, run_start_permission_fields
from tests.support import (
    compaction_completed_contract_fields,
    context_compiled_contract_fields,
    model_call_end_fields,
    model_call_start_fields,
    test_resolved_call_fact,
)
from tests.support.runtime_session import in_memory_runtime_session

from pulsara_agent.event import (
    CapabilityGateDecisionEvent,
    ContextCompiledEvent,
    ContextCompactionCompletedEvent,
    EventContext,
    PlanExitRequestedEvent,
    PlanExitResolvedEvent,
    PlanModeEnteredEvent,
    PlanModeExitedEvent,
    PlanQuestionAnsweredEvent,
    PlanQuestionAskedEvent,
    ModelCallEndEvent,
    ModelCallStartEvent,
    ReplyEndEvent,
    ReplyStartEvent,
    RunEndEvent,
    RunStartEvent,
    TextBlockDeltaEvent,
    TextBlockEndEvent,
    TextBlockStartEvent,
)
from pulsara_agent.event import TerminalProcessCompletedEvent
from pulsara_agent.event_log import (
    EventIdConflict,
    EventLog,
    EventLogWriteConflict,
    PostgresEventLog,
    RawContextAuthorityBundleRequest,
    RawEventSelectionBounds,
    dump_agent_event,
    load_agent_event,
)
from pulsara_agent.settings import StorageConfig
from pulsara_agent.primitives.model_call import ModelCallPurpose, ModelTokenUsageFact
from pulsara_agent.llm.control import build_model_call_control_disposition_event
from pulsara_agent.llm.materialize import materialize_committed_model_call_result
from pulsara_agent.primitives.model_call import ModelCallControlDisposition
from pulsara_agent.runtime.tool_action import (
    builtin_tool_action_policy,
    default_tool_action_classifier_registry,
)
from pulsara_agent.tools.base import ToolCall


def _connect_or_skip(dsn: str):
    try:
        return psycopg.connect(dsn, connect_timeout=2)
    except psycopg.OperationalError as exc:
        pytest.skip(f"Postgres is not available at configured DSN: {exc}")


def _cleanup_session(dsn: str, runtime_session_id: str) -> None:
    with _connect_or_skip(dsn) as connection:
        with connection.cursor() as cursor:
            cursor.execute("delete from sessions where id = %s", (runtime_session_id,))


def _fetch_run_row(dsn: str, run_id: str):
    with psycopg.connect(dsn, row_factory=dict_row, connect_timeout=2) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                "select id, status, stop_reason, started_at, completed_at from runs where id = %s",
                (run_id,),
            )
            return cursor.fetchone()


def _runtime_session_id() -> str:
    return f"runtime:test:{uuid4().hex}"


def _ctx(label: str) -> EventContext:
    return EventContext(
        run_id=f"run:{label}",
        turn_id=f"turn:{label}",
        reply_id=f"reply:{label}",
    )


def _reply_events(ctx: EventContext):
    return [
        ReplyStartEvent(**ctx.event_fields(), name="assistant"),
        TextBlockStartEvent(**ctx.event_fields(), block_id="text:1"),
        TextBlockDeltaEvent(**ctx.event_fields(), block_id="text:1", delta="hello "),
        TextBlockDeltaEvent(**ctx.event_fields(), block_id="text:1", delta="world"),
        TextBlockEndEvent(**ctx.event_fields(), block_id="text:1"),
        ReplyEndEvent(**ctx.event_fields(), model_terminal_outcome="completed"),
    ]


def _append_canonical_reply_events(event_log: EventLog, ctx: EventContext) -> None:
    call = test_resolved_call_fact()
    start = ModelCallStartEvent(
        **ctx.event_fields(),
        **model_call_start_fields(resolved_call=call),
    )
    events = (
        ReplyStartEvent(
            id=start.recovery_plan.reply_start_event_id,
            **ctx.event_fields(),
            name="assistant",
        ),
        start,
        TextBlockStartEvent(**ctx.event_fields(), block_id="text:1"),
        TextBlockDeltaEvent(
            **ctx.event_fields(), block_id="text:1", delta="hello "
        ),
        TextBlockDeltaEvent(
            **ctx.event_fields(), block_id="text:1", delta="world"
        ),
        TextBlockEndEvent(**ctx.event_fields(), block_id="text:1"),
        ModelCallEndEvent(
            id=start.recovery_plan.stable_model_call_end_event_id,
            **ctx.event_fields(),
            **model_call_end_fields(resolved_call=call),
        ),
        ReplyEndEvent(
            id=start.recovery_plan.stable_reply_end_event_id,
            **ctx.event_fields(),
            model_terminal_outcome="completed",
        ),
    )
    event_log.extend(events)
    result = materialize_committed_model_call_result(
        event_log,
        resolved_model_call_id=call.resolved_model_call_id,
    )
    activation = start.recovery_plan.run_execution_activation
    assert activation is not None
    event_log.append(
        build_model_call_control_disposition_event(
            result=result,
            model_call_index=1,
            event_context=ctx,
            activation=activation,
            disposition=ModelCallControlDisposition.ACCEPTED,
            termination_intent=None,
            recovery_reason_code=None,
        )
    )


@pytest.fixture
def event_log(request, tmp_path) -> EventLog:
    dsn = StorageConfig.from_env().postgres_dsn
    _connect_or_skip(dsn).close()
    runtime_session_id = _runtime_session_id()
    log = PostgresEventLog(
        dsn=dsn,
        runtime_session_id=runtime_session_id,
        workspace_root=tmp_path,
    )
    request.addfinalizer(lambda: _cleanup_session(dsn, runtime_session_id))
    return log


def test_event_log_assigns_sequences_and_filters_events(event_log: EventLog) -> None:
    first = _ctx("contract:first")
    second = _ctx("contract:second")

    event_log.extend(_reply_events(first))
    stored = event_log.append(
        TextBlockDeltaEvent(**second.event_fields(), block_id="text:2", delta="other")
    )

    assert stored.sequence == 7
    assert [event.sequence for event in event_log.iter()] == list(range(1, 8))
    assert [event.reply_id for event in event_log.iter(run_id=first.run_id)] == [
        first.reply_id
    ] * 6
    assert [event.run_id for event in event_log.iter(reply_id=second.reply_id)] == [
        second.run_id
    ]
    assert [event.sequence for event in event_log.iter(after_sequence=5)] == [6, 7]


def test_event_log_single_append_is_idempotent_by_exact_event_payload(
    event_log: EventLog,
) -> None:
    ctx = _ctx("contract:idempotent-event-id")
    candidate = TextBlockDeltaEvent(
        **ctx.event_fields(),
        block_id="text:idempotent",
        delta="stable",
    )

    first = event_log.append(candidate)
    confirmed = event_log.append(candidate)

    assert confirmed == first
    assert event_log.get_by_id(candidate.id) == first
    assert len(event_log.iter()) == 1

    conflicting = candidate.model_copy(update={"delta": "different"})
    with pytest.raises(EventIdConflict):
        event_log.append(conflicting)


def test_event_log_replay_rebuilds_assistant_message(event_log: EventLog) -> None:
    ctx = _ctx("contract:replay")
    _append_canonical_reply_events(event_log, ctx)

    message = event_log.replay(ctx.reply_id)

    assert message.id == ctx.reply_id
    assert message.name == "assistant"
    assert message.content[0].type == "text"
    assert message.content[0].text == "hello world"


def test_run_lifecycle_events_round_trip_through_agent_event_serialization() -> None:
    ctx = _ctx("contract:lifecycle")
    started = RunStartEvent(
        **ctx.event_fields(),
        **run_start_permission_fields(ctx.run_id, user_input="x" * 7),
        user_input_chars=7,
    )
    ended = RunEndEvent(
        **run_end_contract_fields(ctx.run_id, status="aborted", abort_kind="user_stop"),
        **ctx.event_fields(),
        status="aborted",
        stop_reason="aborted",
        abort_kind="user_stop",
    )

    assert load_agent_event(dump_agent_event(started)) == started
    assert load_agent_event(dump_agent_event(ended)) == ended


def test_raw_event_type_selection_can_limit_to_active_runs(
    event_log: EventLog,
) -> None:
    ended_ctx = _ctx("contract:active-selection:ended")
    active_ctx = _ctx("contract:active-selection:active")
    ended_start = RunStartEvent(
        **ended_ctx.event_fields(),
        **run_start_permission_fields(ended_ctx.run_id, user_input="ended"),
        user_input_chars=5,
    )
    active_start = RunStartEvent(
        **active_ctx.event_fields(),
        **run_start_permission_fields(active_ctx.run_id, user_input="active"),
        user_input_chars=6,
    )
    ended = RunEndEvent(
        **run_end_contract_fields(
            ended_ctx.run_id,
            status="aborted",
            abort_kind="user_stop",
        ),
        **ended_ctx.event_fields(),
        status="aborted",
        stop_reason="aborted",
        abort_kind="user_stop",
    )
    event_log.extend((ended_start, active_start, ended))

    snapshot = event_log.read_raw_events_by_types(
        ("RUN_START", "RUN_END"),
        active_runs_only=True,
    )

    assert snapshot.through_sequence == 3
    assert tuple(event.run_id for event in snapshot.events) == (active_ctx.run_id,)
    assert tuple(event.event_type for event in snapshot.events) == ("RUN_START",)


def test_context_authority_bundle_freezes_all_channels_at_one_high_water(
    event_log: EventLog,
) -> None:
    ctx = _ctx("contract:authority-bundle")
    stored = event_log.extend(
        (
            RunStartEvent(
                **ctx.event_fields(),
                **run_start_permission_fields(ctx.run_id, user_input="bundle"),
                user_input_chars=6,
            ),
            TextBlockDeltaEvent(
                **ctx.event_fields(),
                block_id="text:bundle",
                delta="payload",
            ),
            RunEndEvent(
                **run_end_contract_fields(
                    ctx.run_id,
                    status="aborted",
                    abort_kind="user_stop",
                ),
                **ctx.event_fields(),
                status="aborted",
                stop_reason="aborted",
                abort_kind="user_stop",
            ),
        )
    )
    bounds = RawEventSelectionBounds(
        max_events=8,
        max_payload_bytes=1024 * 1024,
    )
    request = RawContextAuthorityBundleRequest(
        primary_minimum_sequence=2,
        run_id=ctx.run_id,
        run_sparse_event_types=("RUN_START",),
        session_sparse_event_types=("RUN_END",),
        exact_event_ids=(stored[1].id,),
        primary_bounds=bounds,
        run_sparse_bounds=bounds,
        session_sparse_bounds=bounds,
        exact_bounds=bounds,
    )

    bundle = event_log.read_context_authority_bundle(request)

    assert bundle.through_sequence == 3
    assert tuple(item.sequence for item in bundle.primary_events) == (2, 3)
    assert tuple(item.event_type for item in bundle.run_sparse_events) == (
        "RUN_START",
    )
    assert tuple(item.event_type for item in bundle.session_sparse_events) == (
        "RUN_END",
    )
    assert tuple(item.event_id for item in bundle.exact_events) == (stored[1].id,)
    assert all(
        item.sequence <= bundle.through_sequence
        for channel in (
            bundle.primary_events,
            bundle.run_sparse_events,
            bundle.session_sparse_events,
            bundle.exact_events,
        )
        for item in channel
    )

    empty_delta = event_log.read_context_authority_bundle(
        RawContextAuthorityBundleRequest(
            primary_minimum_sequence=4,
            run_id=ctx.run_id,
            run_sparse_event_types=(),
            session_sparse_event_types=(),
            exact_event_ids=(),
            primary_bounds=bounds,
            run_sparse_bounds=bounds,
            session_sparse_bounds=bounds,
            exact_bounds=bounds,
        )
    )
    assert empty_delta.through_sequence == 3
    assert empty_delta.primary_events == ()


def test_raw_range_and_run_reads_enforce_physical_bounds(
    event_log: EventLog,
) -> None:
    ctx = _ctx("contract:bounded-range")
    event_log.extend(
        (
            RunStartEvent(
                **ctx.event_fields(),
                **run_start_permission_fields(ctx.run_id, user_input="bounded"),
                user_input_chars=7,
            ),
            RunEndEvent(
                **run_end_contract_fields(
                    ctx.run_id,
                    status="aborted",
                    abort_kind="user_stop",
                ),
                **ctx.event_fields(),
                status="aborted",
                stop_reason="aborted",
                abort_kind="user_stop",
            ),
        )
    )

    with pytest.raises(ValueError, match="event bound"):
        event_log.read_raw_range_snapshot(minimum_sequence=1, max_events=1)
    with pytest.raises(ValueError, match="payload-byte bound"):
        event_log.read_raw_range_snapshot(
            minimum_sequence=1,
            max_payload_bytes=1,
        )
    with pytest.raises(ValueError, match="event count"):
        event_log.read_raw_run_events(
            ctx.run_id,
            max_events=1,
            max_payload_bytes=1024 * 1024,
        )


def test_run_start_permission_policy_equals_preset_expansion() -> None:
    ctx = _ctx("contract:lifecycle-permission-preset")
    fields = run_start_permission_fields(
        ctx.run_id, mode="accept-edits", user_input="x" * 7
    )

    event = RunStartEvent(**ctx.event_fields(), **fields, user_input_chars=7)

    assert event.permission_mode == "accept-edits"
    assert event.permission_policy == fields["permission_policy"]


def test_run_start_rejects_missing_or_custom_permission_mode() -> None:
    ctx = _ctx("contract:lifecycle-permission-required")

    with pytest.raises(ValueError):
        RunStartEvent(**ctx.event_fields(), user_input_chars=7)

    fields = run_start_permission_fields(ctx.run_id, user_input="x" * 7)
    with pytest.raises(ValueError):
        RunStartEvent(
            **ctx.event_fields(),
            **{
                **fields,
                "permission_policy": {
                    **fields["permission_policy"],
                    "terminal_access": "ask",
                },
            },
            user_input_chars=7,
        )


def test_run_start_permission_fields_are_required_and_preset_only() -> None:
    test_run_start_rejects_missing_or_custom_permission_mode()


def test_plan_workflow_permission_facts_are_required_preset_expansions() -> None:
    entered_ctx = _ctx("contract:plan-permission-entered")
    exited_ctx = _ctx("contract:plan-permission-exited")
    policy = run_start_permission_fields("run:contract:plan-permission")[
        "permission_policy"
    ]

    with pytest.raises(ValueError):
        PlanModeEnteredEvent(
            **entered_ctx.event_fields(),
            source="user",
            previous_permission_mode="bypass-permissions",
            reason="plan",
        )

    with pytest.raises(ValueError):
        PlanModeEnteredEvent(
            **entered_ctx.event_fields(),
            source="user",
            previous_permission_mode="bypass-permissions",
            previous_permission_policy={**policy, "terminal_access": "ask"},
            reason="plan",
        )

    with pytest.raises(ValueError):
        PlanModeExitedEvent(
            **exited_ctx.event_fields(),
            source="approved_exit_plan",
            exit_request_id="plan_exit:test",
            restored_permission_mode="bypass-permissions",
            restored_permission_policy={**policy, "terminal_access": "ask"},
            transition_owner="agent_run",
        )


@pytest.mark.parametrize(
    ("status", "stop_reason"),
    [
        ("finished", "final"),
        ("failed", "model_error"),
        ("aborted", "aborted"),
    ],
)
def test_postgres_event_log_updates_runs_projection_on_run_lifecycle(
    tmp_path: Path,
    status: str,
    stop_reason: str,
) -> None:
    dsn = StorageConfig.from_env().postgres_dsn
    runtime_session_id = _runtime_session_id()
    _connect_or_skip(dsn).close()

    try:
        event_log = PostgresEventLog(
            dsn=dsn, runtime_session_id=runtime_session_id, workspace_root=tmp_path
        )
        ctx = _ctx(f"postgres:run-projection:{status}:{uuid4().hex}")
        started = RunStartEvent(
            **ctx.event_fields(),
            **run_start_permission_fields(ctx.run_id, user_input="x" * 12),
            user_input_chars=12,
            created_at="2026-01-02T03:04:05+00:00",
        )
        event_log.append(started)

        row = _fetch_run_row(dsn, ctx.run_id)
        assert row["status"] == "running"
        assert row["stop_reason"] is None
        assert row["completed_at"] is None
        assert row["started_at"] == datetime.fromisoformat(started.created_at)

        ended = RunEndEvent(
            **run_end_contract_fields(
                ctx.run_id,
                status=status,
                abort_kind="user_stop" if status == "aborted" else None,
            ),
            **ctx.event_fields(),
            status=status,
            stop_reason=stop_reason,
            abort_kind="user_stop" if status == "aborted" else None,
            created_at="2026-01-02T03:05:06+00:00",
        )
        event_log.append(ended)

        row = _fetch_run_row(dsn, ctx.run_id)
        assert row["status"] == status
        assert row["stop_reason"] == stop_reason
        assert row["completed_at"] == datetime.fromisoformat(ended.created_at)
    finally:
        _cleanup_session(dsn, runtime_session_id)


def test_postgres_event_log_repairs_stale_runs_projection(tmp_path: Path) -> None:
    dsn = StorageConfig.from_env().postgres_dsn
    runtime_session_id = _runtime_session_id()
    _connect_or_skip(dsn).close()

    try:
        event_log = PostgresEventLog(
            dsn=dsn, runtime_session_id=runtime_session_id, workspace_root=tmp_path
        )
        ended_ctx = _ctx(f"postgres:run-repair-ended:{uuid4().hex}")
        running_ctx = _ctx(f"postgres:run-repair-running:{uuid4().hex}")
        ended = RunEndEvent(
            **run_end_contract_fields(ended_ctx.run_id, status="failed"),
            **ended_ctx.event_fields(),
            status="failed",
            stop_reason="model_error",
            created_at="2026-01-02T03:05:06+00:00",
        )
        event_log.extend(
            [
                RunStartEvent(
                    **ended_ctx.event_fields(),
                    **run_start_permission_fields(ended_ctx.run_id, user_input="x" * 8),
                    user_input_chars=8,
                    created_at="2026-01-02T03:04:05+00:00",
                ),
                ended,
                RunStartEvent(
                    **running_ctx.event_fields(),
                    **run_start_permission_fields(
                        running_ctx.run_id, user_input="x" * 9
                    ),
                    user_input_chars=9,
                    created_at="2026-01-03T04:05:06+00:00",
                ),
            ]
        )

        with psycopg.connect(dsn, connect_timeout=2) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    update runs
                    set status = 'running', stop_reason = null, completed_at = null
                    where id = %s
                    """,
                    (ended_ctx.run_id,),
                )
                cursor.execute(
                    """
                    update runs
                    set status = 'failed', stop_reason = 'stale', completed_at = now()
                    where id = %s
                    """,
                    (running_ctx.run_id,),
                )

        assert event_log.repair_run_projection() >= 2

        ended_row = _fetch_run_row(dsn, ended_ctx.run_id)
        assert ended_row["status"] == "failed"
        assert ended_row["stop_reason"] == "model_error"
        assert ended_row["completed_at"] == datetime.fromisoformat(ended.created_at)

        running_row = _fetch_run_row(dsn, running_ctx.run_id)
        assert running_row["status"] == "running"
        assert running_row["stop_reason"] is None
        assert running_row["completed_at"] is None
    finally:
        _cleanup_session(dsn, runtime_session_id)


def test_terminal_process_completed_event_round_trips_through_agent_event_serialization() -> (
    None
):
    event = TerminalProcessCompletedEvent(
        run_id="run:terminal",
        turn_id="turn:terminal",
        reply_id="reply:terminal",
        process_id="proc_123",
        terminal_session_id="default",
        command="pytest -q",
        status="success",
        exit_code=0,
        cwd="/workspace",
        duration_seconds=1.25,
        output_preview="ok",
        tool_call_id="call:terminal",
        completion_reason="user_tool_kill",
    )

    assert load_agent_event(dump_agent_event(event)) == event


def test_context_compiled_event_round_trips_through_agent_event_serialization() -> None:
    event = ContextCompiledEvent(
        **_ctx("contract:context-compiled").event_fields(),
        **context_compiled_contract_fields(status="failed"),
        context_id="context:1",
        model_call_index=1,
        sections=[
            {
                "id": "transcript:current_user",
                "source_id": "current_user",
                "channel": "current_user",
                "included": True,
            }
        ],
        tool_specs=[{"name": "read_file", "estimated_tokens": 10, "included": True}],
        diagnostics=[],
        lifecycle_decisions=[
            {
                "source_id": "transcript",
                "section_id": "transcript:prior_history",
                "decision": "invalidated",
                "reason": "dependency_fingerprint_changed",
            }
        ],
        tool_result_render_decisions=[
            {
                "tool_call_id": "call:terminal",
                "segment": "current_run_tail",
                "latest_reserved_applied": True,
                "body_policy": "full_visible",
            }
        ],
        tool_result_budget_report={
            "caps": {"tool_result_total_context_chars": 36_000},
            "used_by_scope": {"current_run_tail": {"body": 128}},
        },
    )

    assert load_agent_event(dump_agent_event(event)) == event


def test_context_compiled_pressure_event_round_trips_through_agent_event_serialization() -> (
    None
):
    event = ContextCompiledEvent(
        **_ctx("contract:context-pressure").event_fields(),
        **context_compiled_contract_fields(
            status="pressure",
            estimated_tokens=0,
            tools_estimated_tokens=0,
            model_call_index=2,
        ),
        context_id="context:pressure",
        model_call_index=2,
        diagnostics=[
            {
                "severity": "error",
                "code": "tool_result_total_budget_unsatisfied",
                "message": "context pressure",
            }
        ],
        tool_result_render_decisions=[
            {
                "tool_call_id": "call:terminal",
                "unit_fingerprint": "sha256:abc",
                "body_policy": "metadata_only",
            }
        ],
        tool_result_budget_report={
            "caps": {"tool_result_total_context_chars": 36_000},
            "used": {"total": 37_000},
            "diagnostics": [{"code": "tool_result_total_budget_unsatisfied"}],
        },
    )

    assert load_agent_event(dump_agent_event(event)) == event


def test_capability_gate_decision_event_round_trips_through_agent_event_serialization() -> (
    None
):
    action_classification = default_tool_action_classifier_registry().classify(
        call=ToolCall(
            id="call:terminal",
            name="terminal",
            arguments={"command": "pwd"},
        ),
        descriptor_id="builtin:terminal",
        descriptor_fingerprint="descriptor:test:terminal",
        policy=builtin_tool_action_policy("terminal"),
    )
    event = CapabilityGateDecisionEvent(
        **_ctx("contract:capability-gate").event_fields(),
        tool_call_id="call:terminal",
        tool_name="terminal",
        descriptor_id="builtin:terminal",
        decision="wait_for_user",
        reason_code="permission_wait_for_user",
        reason_message="terminal access requires user confirmation by permission policy",
        suggested_rules=[{"tool": "terminal", "reason": "terminal_access_ask"}],
        policy_mode="ask-permissions",
        permission_policy={"profile": "trusted_host", "terminal_access": "ask"},
        exposure_generation=7,
        availability="available",
        permission_category="terminal",
        effective_permission_category="terminal",
        effective_read_only=False,
        capability_context={
            "context_kind": "active_skill_present",
            "active_skill_names": ["hf-cli"],
        },
        action_classification=action_classification,
    )

    assert load_agent_event(dump_agent_event(event)) == event


@pytest.mark.parametrize(
    "event",
    [
        PlanModeEnteredEvent(
            **_ctx("contract:plan-entered").event_fields(),
            source="user",
            previous_permission_mode="bypass-permissions",
            previous_permission_policy=run_start_permission_fields(
                "run:contract:plan-entered"
            )["permission_policy"],
            reason="plan first",
        ),
        PlanQuestionAskedEvent(
            **_ctx("contract:plan-question").event_fields(),
            question_id="plan_question:1",
            tool_call_id="call:question",
            question="Which path?",
            options=["A", "B"],
            allow_free_text=True,
            reason="need scope",
        ),
        PlanQuestionAnsweredEvent(
            **_ctx("contract:plan-answer").event_fields(),
            question_id="plan_question:1",
            answer_text="A",
            selected_option="A",
        ),
        PlanExitRequestedEvent(
            **_ctx("contract:plan-exit-request").event_fields(),
            exit_request_id="plan_exit:1",
            tool_call_id="call:exit",
            plan_text="Do the thing.",
            summary="Thing plan",
        ),
        PlanExitResolvedEvent(
            **_ctx("contract:plan-exit-resolved").event_fields(),
            exit_request_id="plan_exit:1",
            tool_call_id="call:exit",
            decision="approve",
            user_feedback="ok",
        ),
        PlanModeExitedEvent(
            **_ctx("contract:plan-exited").event_fields(),
            source="approved_exit_plan",
            exit_request_id="plan_exit:1",
            restored_permission_mode="bypass-permissions",
            restored_permission_policy=run_start_permission_fields(
                "run:contract:plan-exited"
            )["permission_policy"],
            accepted_plan_summary="Thing plan",
            transition_owner="agent_run",
        ),
    ],
)
def test_plan_workflow_events_round_trip_through_agent_event_serialization(
    event,
) -> None:
    assert load_agent_event(dump_agent_event(event)) == event


def test_event_log_live_append_rejects_presequenced_event(event_log: EventLog) -> None:
    ctx = _ctx("contract:preset")
    with pytest.raises(ValueError, match="sequence=None"):
        event_log.append(
            TextBlockDeltaEvent(
                **ctx.event_fields(),
                block_id="text:10",
                delta="preset",
                sequence=10,
            )
        )
    assert event_log.next_sequence() == 1


def test_postgres_event_log_reloads_persisted_events(tmp_path: Path) -> None:
    dsn = StorageConfig.from_env().postgres_dsn
    runtime_session_id = _runtime_session_id()
    _connect_or_skip(dsn).close()

    try:
        first_log = PostgresEventLog(
            dsn=dsn, runtime_session_id=runtime_session_id, workspace_root=tmp_path
        )
        ctx = _ctx("postgres:reload")
        _append_canonical_reply_events(first_log, ctx)

        second_log = PostgresEventLog(
            dsn=dsn, runtime_session_id=runtime_session_id, workspace_root=tmp_path
        )
        reply_events = second_log.iter(reply_id=ctx.reply_id)
        assert [event.sequence for event in reply_events] == list(
            range(1, len(reply_events) + 1)
        )
        assert second_log.replay(ctx.reply_id).content[0].text == "hello world"
    finally:
        _cleanup_session(dsn, runtime_session_id)


def test_postgres_event_log_can_reserve_session_owner_before_first_event(
    tmp_path: Path,
) -> None:
    dsn = StorageConfig.from_env().postgres_dsn
    runtime_session_id = _runtime_session_id()
    _connect_or_skip(dsn).close()

    try:
        event_log = PostgresEventLog(
            dsn=dsn,
            runtime_session_id=runtime_session_id,
            workspace_root=tmp_path,
        )
        event_log.ensure_runtime_session_owner()

        with psycopg.connect(dsn, row_factory=dict_row) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    "select id, workspace_root from sessions where id = %s",
                    (runtime_session_id,),
                )
                row = cursor.fetchone()
                cursor.execute(
                    "select count(*) as count from agent_events where session_id = %s",
                    (runtime_session_id,),
                )
                event_count = cursor.fetchone()["count"]

        assert row == {"id": runtime_session_id, "workspace_root": str(tmp_path)}
        assert event_count == 0
        assert event_log.next_sequence() == 1
    finally:
        _cleanup_session(dsn, runtime_session_id)


def test_postgres_event_log_concurrent_append_keeps_unique_sequences(
    tmp_path: Path,
) -> None:
    dsn = StorageConfig.from_env().postgres_dsn
    runtime_session_id = _runtime_session_id()
    _connect_or_skip(dsn).close()

    try:
        event_log = PostgresEventLog(
            dsn=dsn, runtime_session_id=runtime_session_id, workspace_root=tmp_path
        )
        ctx = _ctx("postgres:concurrent")
        events = [
            TextBlockDeltaEvent(
                **ctx.event_fields(), block_id=f"text:{index}", delta=str(index)
            )
            for index in range(12)
        ]

        with ThreadPoolExecutor(max_workers=4) as executor:
            stored = list(executor.map(event_log.append, events))

        assert sorted(event.sequence for event in stored) == list(range(1, 13))
        assert [
            event.sequence for event in event_log.iter(reply_id=ctx.reply_id)
        ] == list(range(1, 13))
    finally:
        _cleanup_session(dsn, runtime_session_id)


def test_postgres_event_log_extend_allocates_contiguous_atomic_batch(
    tmp_path: Path,
) -> None:
    dsn = StorageConfig.from_env().postgres_dsn
    runtime_session_id = _runtime_session_id()
    _connect_or_skip(dsn).close()
    try:
        log = PostgresEventLog(
            dsn=dsn, runtime_session_id=runtime_session_id, workspace_root=tmp_path
        )
        ctx = _ctx("postgres:atomic-batch")
        stored = log.extend(
            [
                TextBlockDeltaEvent(
                    **ctx.event_fields(),
                    block_id=f"text:{index}",
                    delta=str(index),
                )
                for index in range(4)
            ]
        )
        assert [event.sequence for event in stored] == [1, 2, 3, 4]
    finally:
        _cleanup_session(dsn, runtime_session_id)


def test_postgres_event_log_concurrent_batches_never_interleave(tmp_path: Path) -> None:
    dsn = StorageConfig.from_env().postgres_dsn
    runtime_session_id = _runtime_session_id()
    barrier = Barrier(2)
    _connect_or_skip(dsn).close()
    log = PostgresEventLog(
        dsn=dsn, runtime_session_id=runtime_session_id, workspace_root=tmp_path
    )

    def write(prefix: str):
        ctx = _ctx(f"postgres:batch:{prefix}")
        barrier.wait(timeout=2)
        return log.extend(
            [
                TextBlockDeltaEvent(
                    **ctx.event_fields(),
                    block_id=f"{prefix}:{index}",
                    delta=prefix,
                )
                for index in range(3)
            ]
        )

    try:
        with ThreadPoolExecutor(max_workers=2) as executor:
            futures = (executor.submit(write, "a"), executor.submit(write, "b"))
            batches = tuple(future.result() for future in futures)
        for batch in batches:
            sequences = [event.sequence for event in batch]
            assert sequences == list(range(sequences[0], sequences[0] + 3))
        assert [event.sequence for event in log.iter()] == list(range(1, 7))
    finally:
        _cleanup_session(dsn, runtime_session_id)


def test_postgres_event_log_conditional_extend_conflict_writes_nothing(
    tmp_path: Path,
) -> None:
    dsn = StorageConfig.from_env().postgres_dsn
    runtime_session_id = _runtime_session_id()
    _connect_or_skip(dsn).close()
    try:
        log = PostgresEventLog(
            dsn=dsn, runtime_session_id=runtime_session_id, workspace_root=tmp_path
        )
        ctx = _ctx("postgres:cas")
        log.append(
            TextBlockDeltaEvent(**ctx.event_fields(), block_id="seed", delta="seed")
        )
        with pytest.raises(EventLogWriteConflict) as captured:
            log.extend(
                [
                    TextBlockDeltaEvent(
                        **ctx.event_fields(), block_id="stale", delta="stale"
                    )
                ],
                expected_last_sequence=0,
            )
        assert captured.value.actual_last_sequence == 1
        assert [event.sequence for event in log.iter()] == [1]
    finally:
        _cleanup_session(dsn, runtime_session_id)


def test_postgres_event_log_batch_failure_rolls_back_prior_event_and_projection(
    tmp_path: Path,
) -> None:
    dsn = StorageConfig.from_env().postgres_dsn
    owner_session_id = _runtime_session_id()
    batch_session_id = _runtime_session_id()
    _connect_or_skip(dsn).close()
    owner = PostgresEventLog(
        dsn=dsn,
        runtime_session_id=owner_session_id,
        workspace_root=tmp_path,
    )
    batch = PostgresEventLog(
        dsn=dsn,
        runtime_session_id=batch_session_id,
        workspace_root=tmp_path,
    )
    shared_run_id = f"run:owned:{uuid4().hex}"
    owner_context = EventContext(
        run_id=shared_run_id,
        turn_id=f"turn:owned:{uuid4().hex}",
        reply_id=f"reply:owned:{uuid4().hex}",
    )
    first_context = _ctx(f"postgres:rollback:first:{uuid4().hex}")
    conflicting_context = EventContext(
        run_id=shared_run_id,
        turn_id=f"turn:conflict:{uuid4().hex}",
        reply_id=f"reply:conflict:{uuid4().hex}",
    )
    try:
        owner.append(
            TextBlockDeltaEvent(
                **owner_context.event_fields(),
                block_id="owner",
                delta="owner",
            )
        )

        with pytest.raises(ValueError, match="already belongs to runtime session"):
            batch.extend(
                (
                    RunStartEvent(
                        **first_context.event_fields(),
                        **run_start_permission_fields(
                            first_context.run_id, user_input="x"
                        ),
                        user_input_chars=1,
                    ),
                    TextBlockDeltaEvent(
                        **conflicting_context.event_fields(),
                        block_id="conflict",
                        delta="conflict",
                    ),
                )
            )

        assert batch.iter() == []
        assert batch.next_sequence() == 1
        assert _fetch_run_row(dsn, first_context.run_id) is None
        assert [event.sequence for event in owner.iter()] == [1]
    finally:
        _cleanup_session(dsn, batch_session_id)
        _cleanup_session(dsn, owner_session_id)


def test_postgres_event_log_rejects_cross_session_run_id_reuse(tmp_path: Path) -> None:
    dsn = StorageConfig.from_env().postgres_dsn
    first_session_id = _runtime_session_id()
    second_session_id = _runtime_session_id()
    _connect_or_skip(dsn).close()

    try:
        first_log = PostgresEventLog(
            dsn=dsn, runtime_session_id=first_session_id, workspace_root=tmp_path
        )
        second_log = PostgresEventLog(
            dsn=dsn, runtime_session_id=second_session_id, workspace_root=tmp_path
        )
        first_ctx = EventContext(
            run_id="run:shared",
            turn_id=f"turn:{uuid4().hex}",
            reply_id=f"reply:{uuid4().hex}",
        )
        second_ctx = EventContext(
            run_id="run:shared",
            turn_id=f"turn:{uuid4().hex}",
            reply_id=f"reply:{uuid4().hex}",
        )

        first_log.append(
            TextBlockDeltaEvent(
                **first_ctx.event_fields(), block_id="text:1", delta="first"
            )
        )

        with pytest.raises(ValueError, match="already belongs to runtime session"):
            second_log.append(
                TextBlockDeltaEvent(
                    **second_ctx.event_fields(), block_id="text:2", delta="second"
                )
            )
    finally:
        _cleanup_session(dsn, first_session_id)
        _cleanup_session(dsn, second_session_id)


def test_postgres_event_log_rejects_cross_run_turn_id_reuse(tmp_path: Path) -> None:
    dsn = StorageConfig.from_env().postgres_dsn
    runtime_session_id = _runtime_session_id()
    _connect_or_skip(dsn).close()

    try:
        event_log = PostgresEventLog(
            dsn=dsn, runtime_session_id=runtime_session_id, workspace_root=tmp_path
        )
        turn_id = f"turn:shared:{uuid4().hex}"
        first_ctx = EventContext(
            run_id=f"run:first:{uuid4().hex}",
            turn_id=turn_id,
            reply_id=f"reply:{uuid4().hex}",
        )
        second_ctx = EventContext(
            run_id=f"run:second:{uuid4().hex}",
            turn_id=turn_id,
            reply_id=f"reply:{uuid4().hex}",
        )

        event_log.append(
            TextBlockDeltaEvent(
                **first_ctx.event_fields(), block_id="text:1", delta="first"
            )
        )

        with pytest.raises(ValueError, match="already belongs to runtime session"):
            event_log.append(
                TextBlockDeltaEvent(
                    **second_ctx.event_fields(), block_id="text:2", delta="second"
                )
            )
    finally:
        _cleanup_session(dsn, runtime_session_id)


def test_postgres_event_log_rejects_concurrent_cross_session_run_id_reuse(
    tmp_path: Path,
) -> None:
    dsn = StorageConfig.from_env().postgres_dsn
    first_session_id = _runtime_session_id()
    second_session_id = _runtime_session_id()
    shared_run_id = f"run:shared:{uuid4().hex}"
    barrier = Barrier(2)
    _connect_or_skip(dsn).close()

    def append_with(log: PostgresEventLog, ctx: EventContext) -> str:
        barrier.wait(timeout=2)
        try:
            log.append(
                TextBlockDeltaEvent(
                    **ctx.event_fields(), block_id=f"text:{uuid4().hex}", delta="x"
                )
            )
        except ValueError:
            return "error"
        return "ok"

    try:
        first_log = PostgresEventLog(
            dsn=dsn, runtime_session_id=first_session_id, workspace_root=tmp_path
        )
        second_log = PostgresEventLog(
            dsn=dsn, runtime_session_id=second_session_id, workspace_root=tmp_path
        )
        first_ctx = EventContext(
            run_id=shared_run_id,
            turn_id=f"turn:{uuid4().hex}",
            reply_id=f"reply:{uuid4().hex}",
        )
        second_ctx = EventContext(
            run_id=shared_run_id,
            turn_id=f"turn:{uuid4().hex}",
            reply_id=f"reply:{uuid4().hex}",
        )

        with ThreadPoolExecutor(max_workers=2) as executor:
            futures = [
                executor.submit(append_with, first_log, first_ctx),
                executor.submit(append_with, second_log, second_ctx),
            ]
            outcomes = sorted(future.result() for future in futures)

        assert outcomes == ["error", "ok"]
    finally:
        _cleanup_session(dsn, first_session_id)
        _cleanup_session(dsn, second_session_id)


def test_postgres_event_log_rejects_concurrent_cross_session_turn_id_reuse(
    tmp_path: Path,
) -> None:
    dsn = StorageConfig.from_env().postgres_dsn
    first_session_id = _runtime_session_id()
    second_session_id = _runtime_session_id()
    shared_turn_id = f"turn:shared:{uuid4().hex}"
    barrier = Barrier(2)
    _connect_or_skip(dsn).close()

    def append_with(log: PostgresEventLog, ctx: EventContext) -> str:
        barrier.wait(timeout=2)
        try:
            log.append(
                TextBlockDeltaEvent(
                    **ctx.event_fields(), block_id=f"text:{uuid4().hex}", delta="x"
                )
            )
        except ValueError:
            return "error"
        return "ok"

    try:
        first_log = PostgresEventLog(
            dsn=dsn, runtime_session_id=first_session_id, workspace_root=tmp_path
        )
        second_log = PostgresEventLog(
            dsn=dsn, runtime_session_id=second_session_id, workspace_root=tmp_path
        )
        first_ctx = EventContext(
            run_id=f"run:first:{uuid4().hex}",
            turn_id=shared_turn_id,
            reply_id=f"reply:{uuid4().hex}",
        )
        second_ctx = EventContext(
            run_id=f"run:second:{uuid4().hex}",
            turn_id=shared_turn_id,
            reply_id=f"reply:{uuid4().hex}",
        )

        with ThreadPoolExecutor(max_workers=2) as executor:
            futures = [
                executor.submit(append_with, first_log, first_ctx),
                executor.submit(append_with, second_log, second_ctx),
            ]
            outcomes = sorted(future.result() for future in futures)

        assert outcomes == ["error", "ok"]
    finally:
        _cleanup_session(dsn, first_session_id)
        _cleanup_session(dsn, second_session_id)


def test_postgres_event_log_transaction_failure_leaves_no_partial_events(
    tmp_path: Path,
) -> None:
    dsn = StorageConfig.from_env().postgres_dsn
    runtime_session_id = _runtime_session_id()
    conflicting_session_id = _runtime_session_id()
    _connect_or_skip(dsn).close()

    try:
        event_log = PostgresEventLog(
            dsn=dsn, runtime_session_id=runtime_session_id, workspace_root=tmp_path
        )
        conflicting_log = PostgresEventLog(
            dsn=dsn, runtime_session_id=conflicting_session_id, workspace_root=tmp_path
        )
        conflicting_ctx = EventContext(
            run_id="run:batch-conflict",
            turn_id=f"turn:{uuid4().hex}",
            reply_id=f"reply:{uuid4().hex}",
        )
        valid_ctx = EventContext(
            run_id=f"run:valid:{uuid4().hex}",
            turn_id=f"turn:{uuid4().hex}",
            reply_id=f"reply:{uuid4().hex}",
        )
        invalid_ctx = EventContext(
            run_id=conflicting_ctx.run_id,
            turn_id=f"turn:{uuid4().hex}",
            reply_id=f"reply:{uuid4().hex}",
        )
        conflicting_log.append(
            TextBlockDeltaEvent(
                **conflicting_ctx.event_fields(), block_id="text:seed", delta="seed"
            )
        )

        with pytest.raises(ValueError, match="already belongs to runtime session"):
            event_log.extend(
                [
                    TextBlockDeltaEvent(
                        **valid_ctx.event_fields(), block_id="text:valid", delta="valid"
                    ),
                    TextBlockDeltaEvent(
                        **invalid_ctx.event_fields(),
                        block_id="text:invalid",
                        delta="invalid",
                    ),
                ]
            )

        assert event_log.iter(reply_id=valid_ctx.reply_id) == []
        assert event_log.iter(reply_id=invalid_ctx.reply_id) == []
    finally:
        _cleanup_session(dsn, runtime_session_id)
        _cleanup_session(dsn, conflicting_session_id)


def test_runtime_session_can_emit_with_postgres_event_log(tmp_path: Path) -> None:

    dsn = StorageConfig.from_env().postgres_dsn
    runtime_session_id = _runtime_session_id()
    _connect_or_skip(dsn).close()

    try:
        event_log = PostgresEventLog(
            dsn=dsn, runtime_session_id=runtime_session_id, workspace_root=tmp_path
        )
        runtime = in_memory_runtime_session(
            tmp_path,
            runtime_session_id=runtime_session_id,
            event_log=event_log,
        )
        ctx = _ctx("postgres:runtime")

        async def run() -> None:
            first = await runtime.emit(
                TextBlockDeltaEvent(
                    **ctx.event_fields(), block_id="text:1", delta="hello"
                )
            )
            second = await runtime.emit(
                TextBlockDeltaEvent(
                    **ctx.event_fields(), block_id="text:1", delta=" world"
                )
            )
            assert [first.sequence, second.sequence] == [1, 2]

        asyncio.run(run())

        reloaded = PostgresEventLog(
            dsn=dsn, runtime_session_id=runtime_session_id, workspace_root=tmp_path
        )
        assert [event.sequence for event in reloaded.iter(reply_id=ctx.reply_id)] == [
            1,
            2,
        ]
    finally:
        _cleanup_session(dsn, runtime_session_id)


def test_model_call_end_usage_breakdown_round_trips_postgres(
    event_log: EventLog,
) -> None:
    ctx = _ctx(f"postgres:model-usage:{uuid4().hex}")
    call = test_resolved_call_fact()
    event_log.extend(
        (
            ModelCallStartEvent(
                **ctx.event_fields(),
                **model_call_start_fields(
                    resolved_call=call,
                    context_id="context:postgres:model-usage",
                    model_call_index=1,
                ),
            ),
            ModelCallEndEvent(
                **ctx.event_fields(),
                resolved_model_call_id=call.resolved_model_call_id,
                target_fingerprint=call.target.target_fingerprint,
                reported_model_id="provider-snapshot",
                outcome="completed",
                provider_dispatch_status="dispatched",
                usage_status="reported",
                usage=ModelTokenUsageFact(
                    input_tokens=120,
                    cached_input_tokens=40,
                    output_tokens=30,
                    reasoning_output_tokens=10,
                    total_tokens=150,
                ),
                estimated_input_tokens=128,
            ),
        )
    )

    reloaded = event_log.iter()
    end = next(event for event in reloaded if isinstance(event, ModelCallEndEvent))
    assert end.reported_model_id == "provider-snapshot"
    assert end.usage is not None
    assert end.usage.cached_input_tokens == 40
    assert end.usage.reasoning_output_tokens == 10
    assert end.usage.total_tokens == 150


def test_compaction_double_limits_round_trip() -> None:
    ctx = _ctx(f"contract:compaction-double-limits:{uuid4().hex}")
    fields = compaction_completed_contract_fields()
    event = ContextCompactionCompletedEvent(
        **ctx.event_fields(),
        **fields,
        compaction_id=f"compaction:{uuid4().hex}",
        trigger="manual",
        reason="contract-test",
        window_number=1,
        window_id=f"window:{uuid4().hex}",
        summary_artifact_id=f"artifact:{uuid4().hex}",
        summary_chars=12,
        threshold_tokens=100,
        through_sequence=10,
        keep_after_sequence=8,
    )

    restored = load_agent_event(dump_agent_event(event))

    assert isinstance(restored, ContextCompactionCompletedEvent)
    assert restored.target_model_target.model_role == "pro"
    assert (
        restored.summarizer_call.purpose is ModelCallPurpose.CONTEXT_COMPACTION_SUMMARY
    )
    assert (
        restored.target_model_target.target_fingerprint
        != restored.summarizer_call.target.target_fingerprint
    )


def test_postgres_model_call_facts_round_trip(event_log: EventLog) -> None:
    ctx = _ctx(f"postgres:model-facts:{uuid4().hex}")
    call = test_resolved_call_fact()
    permission = run_start_permission_fields(
        ctx.run_id, user_input="x" * 3, model_target=call.target
    )
    event_log.extend(
        (
            RunStartEvent(
                **ctx.event_fields(),
                **permission,
                user_input_chars=3,
            ),
            ContextCompiledEvent(
                **ctx.event_fields(),
                **context_compiled_contract_fields(
                    resolved_call=call,
                    estimated_tokens=22,
                    tools_estimated_tokens=0,
                ),
                context_id="context:postgres:model-facts",
                model_call_index=1,
            ),
            ModelCallStartEvent(
                **ctx.event_fields(),
                **model_call_start_fields(
                    resolved_call=call,
                    context_id="context:postgres:model-facts",
                    model_call_index=1,
                ),
            ),
        )
    )

    restored = event_log.iter()
    run_start = next(event for event in restored if isinstance(event, RunStartEvent))
    compiled = next(
        event for event in restored if isinstance(event, ContextCompiledEvent)
    )
    started = next(
        event for event in restored if isinstance(event, ModelCallStartEvent)
    )
    assert run_start.model_target == call.target
    assert compiled.resolved_call == call
    assert started.resolved_call == call
    assert compiled.budget.target_fingerprint == call.target.target_fingerprint


def test_postgres_json_payload_contains_no_secret(
    event_log: EventLog,
) -> None:
    ctx = _ctx(f"postgres:model-secret:{uuid4().hex}")
    call = test_resolved_call_fact()
    permission = run_start_permission_fields(
        ctx.run_id, user_input="x", model_target=call.target
    )
    event_log.append(
        RunStartEvent(
            **ctx.event_fields(),
            **permission,
            user_input_chars=1,
        )
    )

    payload = json.dumps(dump_agent_event(event_log.iter()[0]), sort_keys=True)
    assert "sk-test" not in payload
    assert "authorization" not in payload.lower()
    assert "request_defaults" not in payload
