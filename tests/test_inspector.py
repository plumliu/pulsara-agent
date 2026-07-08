from __future__ import annotations

from pathlib import Path
from uuid import uuid4

import psycopg
import pytest
from psycopg.types.json import Jsonb

from pulsara_agent.event import (
    CapabilityGateDecisionEvent,
    ContextCompiledEvent,
    ContextCompactionCompletedEvent,
    CustomEvent,
    EventContext,
    ModelCallStartEvent,
    ProjectionReadyEvent,
    ReplyEndEvent,
    ReplyStartEvent,
    RunEndEvent,
    RunStartEvent,
    SubagentEdgeRecordedEvent,
    SubagentMessageSentEvent,
    SubagentResultDeliveredEvent,
    SubagentRunCompletedEvent,
    SubagentRunStartedEvent,
    SubagentTaskBlockedEvent,
    SubagentTaskCompletedEvent,
    SubagentTaskCreatedEvent,
    SubagentTaskFailedEvent,
    SubagentTaskStartedEvent,
    TextBlockDeltaEvent,
    TextBlockEndEvent,
    TextBlockStartEvent,
    ToolCallEndEvent,
    ToolCallStartEvent,
    ToolResultEndEvent,
    ToolResultStartEvent,
    ToolResultTextDeltaEvent,
)
from pulsara_agent.event_log import PostgresEventLog
from pulsara_agent.inspector import InspectorService, PostgresInspectorStore
from pulsara_agent.memory.artifacts.postgres_archive import PostgresArtifactStore
from pulsara_agent.memory.candidates.pool import CANDIDATE_POOL_SCHEMA_SQL
from pulsara_agent.message import ToolResultArtifactRef, ToolResultState
from pulsara_agent.settings import StorageConfig
from pulsara_agent.storage import MEMORY_SUBSTRATE_SCHEMA_SQL


def _connect_or_skip(dsn: str):
    try:
        return psycopg.connect(dsn, connect_timeout=2)
    except psycopg.OperationalError as exc:
        pytest.skip(f"Postgres is not available at configured DSN: {exc}")


def _runtime_session_id() -> str:
    return f"runtime:inspector:{uuid4().hex}"


def _ctx(label: str) -> EventContext:
    unique = uuid4().hex
    return EventContext(
        run_id=f"run:{label}:{unique}",
        turn_id=f"turn:{label}:{unique}",
        reply_id=f"reply:{label}:{unique}",
    )


def _cleanup_session(dsn: str, runtime_session_id: str) -> None:
    with _connect_or_skip(dsn) as connection:
        with connection.cursor() as cursor:
            cursor.execute("delete from sessions where id = %s", (runtime_session_id,))


def _service(dsn: str) -> InspectorService:
    return InspectorService(PostgresInspectorStore(dsn), oxigraph_url=None)


def _simple_run_events(ctx: EventContext, *, user_input: str, text: str):
    return [
        RunStartEvent(**ctx.event_fields(), user_input_chars=len(user_input), metadata={"user_input": user_input}),
        ReplyStartEvent(**ctx.event_fields(), name="assistant"),
        TextBlockStartEvent(**ctx.event_fields(), block_id=f"text:{ctx.run_id}"),
        TextBlockDeltaEvent(**ctx.event_fields(), block_id=f"text:{ctx.run_id}", delta=text),
        TextBlockEndEvent(**ctx.event_fields(), block_id=f"text:{ctx.run_id}"),
        ReplyEndEvent(**ctx.event_fields()),
        RunEndEvent(**ctx.event_fields(), status="finished", stop_reason="final"),
    ]


def test_inspect_run_rebuilds_timeline_and_assistant_reply(tmp_path: Path) -> None:
    dsn = StorageConfig.from_env().postgres_dsn
    runtime_session_id = _runtime_session_id()
    _connect_or_skip(dsn).close()
    try:
        log = PostgresEventLog(dsn=dsn, runtime_session_id=runtime_session_id, workspace_root=tmp_path)
        ctx = _ctx("basic")
        log.extend(_simple_run_events(ctx, user_input="hello", text="PULSARA_INSPECTOR_TEXT"))

        report = _service(dsn).inspect_run(ctx.run_id)

        assert report["inspect_kind"] == "run"
        assert report["session"]["id"] == runtime_session_id
        assert report["run"]["status"] == "finished"
        assert report["canonical"]["current_user_input"] == "hello"
        assert report["timeline"]["status"] == "completed"
        assert "PULSARA_INSPECTOR_TEXT" in report["assistant_replies"][0]["content"][0]["text"]
        assert report["diagnostics"] == []
    finally:
        _cleanup_session(dsn, runtime_session_id)


def test_inspect_run_reports_stale_run_projection_without_repairing(tmp_path: Path) -> None:
    dsn = StorageConfig.from_env().postgres_dsn
    runtime_session_id = _runtime_session_id()
    _connect_or_skip(dsn).close()
    try:
        log = PostgresEventLog(dsn=dsn, runtime_session_id=runtime_session_id, workspace_root=tmp_path)
        ctx = _ctx("stale")
        log.extend(_simple_run_events(ctx, user_input="hello", text="done"))
        with psycopg.connect(dsn, connect_timeout=2) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    "update runs set status = 'running', stop_reason = null, completed_at = null where id = %s",
                    (ctx.run_id,),
                )

        report = _service(dsn).inspect_run(ctx.run_id)

        assert any(diagnostic["code"] == "run_projection_stale" for diagnostic in report["diagnostics"])
        with psycopg.connect(dsn, connect_timeout=2) as connection:
            with connection.cursor() as cursor:
                cursor.execute("select status, completed_at from runs where id = %s", (ctx.run_id,))
                status, completed_at = cursor.fetchone()
        assert status == "running"
        assert completed_at is None
    finally:
        _cleanup_session(dsn, runtime_session_id)


def test_inspect_run_prior_messages_are_bounded_to_target_run_start(tmp_path: Path) -> None:
    dsn = StorageConfig.from_env().postgres_dsn
    runtime_session_id = _runtime_session_id()
    _connect_or_skip(dsn).close()
    try:
        log = PostgresEventLog(dsn=dsn, runtime_session_id=runtime_session_id, workspace_root=tmp_path)
        first = _ctx("first")
        target = _ctx("target")
        future = _ctx("future")
        log.extend(_simple_run_events(first, user_input="first user", text="FIRST_ASSISTANT"))
        log.extend(_simple_run_events(target, user_input="target user", text="TARGET_ASSISTANT"))
        log.extend(_simple_run_events(future, user_input="future user", text="FUTURE_ASSISTANT"))

        report = _service(dsn).inspect_run(target.run_id)
        prior_text = str(report["prior_messages_as_seen"])

        assert "first user" in prior_text
        assert "FIRST_ASSISTANT" in prior_text
        assert "target user" not in prior_text
        assert "FUTURE_ASSISTANT" not in prior_text
    finally:
        _cleanup_session(dsn, runtime_session_id)


def test_inspect_run_prior_messages_use_context_compaction_boundary(tmp_path: Path) -> None:
    dsn = StorageConfig.from_env().postgres_dsn
    runtime_session_id = _runtime_session_id()
    _connect_or_skip(dsn).close()
    try:
        log = PostgresEventLog(dsn=dsn, runtime_session_id=runtime_session_id, workspace_root=tmp_path)
        old = _ctx("compacted-old")
        target = _ctx("compacted-target")
        log.extend(_simple_run_events(old, user_input="old user text", text="OLD_ASSISTANT_TEXT"))
        summary_artifact_id = f"context_compaction_summary:{uuid4().hex}"
        PostgresArtifactStore(dsn).put_text(
            summary_artifact_id,
            "COMPACTED_OLD_CONTEXT_SUMMARY",
            session_id=runtime_session_id,
            run_id=old.run_id,
            media_type="text/plain; charset=utf-8",
            metadata={"kind": "context_compaction_summary", "do_not_write_back": True},
        )
        log.append(
            ContextCompactionCompletedEvent(
                **old.event_fields(),
                compaction_id=f"context_compaction:{uuid4().hex}",
                trigger="auto",
                reason="mid_turn_context_threshold",
                window_number=1,
                window_id="context_window:test",
                summary_artifact_id=summary_artifact_id,
                summary_chars=len("COMPACTED_OLD_CONTEXT_SUMMARY"),
                estimated_tokens_before=10_000,
                estimated_tokens_after=100,
                threshold_tokens=200_000,
                context_window_tokens=256_000,
                through_sequence=7,
                keep_after_sequence=7,
                included_run_ids=[old.run_id],
                metadata={
                    "phase": "mid_turn",
                    "safe_point": "before_followup_model_call",
                    "current_run_id": target.run_id,
                    "max_compactable_sequence": 7,
                    "tail_message_count": 3,
                },
            )
        )
        log.extend(_simple_run_events(target, user_input="target user", text="TARGET_ASSISTANT"))

        report = _service(dsn).inspect_run(target.run_id)
        prior_text = str(report["prior_messages_as_seen"])

        assert report["compaction_boundary_as_seen"]["summary_artifact_id"] == summary_artifact_id
        assert report["compaction_boundary_as_seen"]["phase"] == "mid_turn"
        assert report["compaction_boundary_as_seen"]["safe_point"] == "before_followup_model_call"
        assert report["compaction_boundary_as_seen"]["current_run_id"] == target.run_id
        assert report["compaction_boundary_as_seen"]["max_compactable_sequence"] == 7
        assert report["compaction_boundary_as_seen"]["tail_message_count"] == 3
        assert "COMPACTED_OLD_CONTEXT_SUMMARY" in prior_text
        assert "<context-compaction-summary" in prior_text
        assert "old user text" not in prior_text
        assert "OLD_ASSISTANT_TEXT" not in prior_text
    finally:
        _cleanup_session(dsn, runtime_session_id)


def test_inspect_run_reports_context_compilation_and_model_call_join(tmp_path: Path) -> None:
    dsn = StorageConfig.from_env().postgres_dsn
    runtime_session_id = _runtime_session_id()
    _connect_or_skip(dsn).close()
    try:
        log = PostgresEventLog(dsn=dsn, runtime_session_id=runtime_session_id, workspace_root=tmp_path)
        ctx = _ctx("context-compiled")
        context_id = f"context:{uuid4().hex}"
        log.extend(
            [
                RunStartEvent(**ctx.event_fields(), user_input_chars=5, metadata={"user_input": "hello"}),
                ContextCompiledEvent(
                    **ctx.event_fields(),
                    context_id=context_id,
                    model_role="pro",
                    model_call_index=1,
                    estimated_tokens=321,
                    context_window_tokens=256_000,
                    reserved_output_tokens=8_000,
                    tools_estimated_tokens=42,
                    sections=[
                        {
                            "id": "transcript:current_user",
                            "source_id": "current_user",
                            "channel": "current_user",
                            "included": True,
                            "render_mode": "full",
                            "estimated_tokens": 2,
                        }
                    ],
                    tool_specs=[{"name": "read_file", "estimated_tokens": 42, "included": True}],
                    diagnostics=[],
                    lifecycle_decisions=[
                        {
                            "source_id": "transcript",
                            "section_id": "transcript:prior_history",
                            "decision": "invalidated",
                            "reason": "dependency_fingerprint_changed",
                        }
                    ],
                ),
                ModelCallStartEvent(
                    **ctx.event_fields(),
                    model_name="pro",
                    model_role="pro",
                    provider="scripted",
                    context_id=context_id,
                    model_call_index=1,
                ),
                ReplyStartEvent(**ctx.event_fields(), name="assistant"),
                TextBlockStartEvent(**ctx.event_fields(), block_id=f"text:{ctx.run_id}"),
                TextBlockDeltaEvent(**ctx.event_fields(), block_id=f"text:{ctx.run_id}", delta="done"),
                TextBlockEndEvent(**ctx.event_fields(), block_id=f"text:{ctx.run_id}"),
                ReplyEndEvent(**ctx.event_fields()),
                RunEndEvent(**ctx.event_fields(), status="finished", stop_reason="final"),
            ]
        )

        report = _service(dsn).inspect_run(ctx.run_id)

        contexts = report["contexts_as_seen"]
        assert contexts["latest"]["context_id"] == context_id
        assert contexts["latest"]["tools_estimated_tokens"] == 42
        assert contexts["latest"]["sections"][0]["channel"] == "current_user"
        assert contexts["latest"]["lifecycle_decisions"][0]["decision"] == "invalidated"
        assert contexts["model_call_joins"][0]["join_status"] == "matched"
        assert contexts["model_call_joins"][0]["context_compiled_sequence"] is not None
        assert contexts["diagnostics"] == []
    finally:
        _cleanup_session(dsn, runtime_session_id)


def test_inspect_session_reports_missing_context_compaction_summary_artifact(tmp_path: Path) -> None:
    dsn = StorageConfig.from_env().postgres_dsn
    runtime_session_id = _runtime_session_id()
    _connect_or_skip(dsn).close()
    try:
        log = PostgresEventLog(dsn=dsn, runtime_session_id=runtime_session_id, workspace_root=tmp_path)
        ctx = _ctx("missing-compaction-summary")
        log.extend(_simple_run_events(ctx, user_input="old", text="done"))
        log.append(
            ContextCompactionCompletedEvent(
                **ctx.event_fields(),
                compaction_id=f"context_compaction:{uuid4().hex}",
                trigger="auto",
                reason="context_threshold",
                window_number=1,
                window_id="context_window:missing",
                summary_artifact_id=f"artifact:missing:{uuid4().hex}",
                summary_chars=10,
                estimated_tokens_before=200_001,
                estimated_tokens_after=1_000,
                threshold_tokens=200_000,
                context_window_tokens=256_000,
                through_sequence=7,
                keep_after_sequence=7,
                included_run_ids=[ctx.run_id],
            )
        )

        report = _service(dsn).inspect_session(runtime_session_id)

        assert report["compaction_windows"][0]["summary_artifact_present"] is False
        assert any(
            diagnostic["code"] == "context_compaction_missing_summary_artifact"
            for diagnostic in report["diagnostics"]
        )
    finally:
        _cleanup_session(dsn, runtime_session_id)


def test_inspect_run_reports_only_projections_seen_by_that_run(tmp_path: Path) -> None:
    dsn = StorageConfig.from_env().postgres_dsn
    runtime_session_id = _runtime_session_id()
    _connect_or_skip(dsn).close()
    try:
        log = PostgresEventLog(dsn=dsn, runtime_session_id=runtime_session_id, workspace_root=tmp_path)
        target = _ctx("target-projection")
        future = _ctx("future-projection")
        log.extend(
            [
                RunStartEvent(**target.event_fields(), user_input_chars=6, metadata={"user_input": "target"}),
                ProjectionReadyEvent(
                    **target.event_fields(),
                    projection_id="projection:target",
                    role="pro",
                    scope="session",
                    token_budget=100,
                    summary="TARGET_PROJECTION_AS_SEEN",
                ),
                ReplyStartEvent(**target.event_fields(), name="assistant"),
                TextBlockStartEvent(**target.event_fields(), block_id="text:target"),
                TextBlockDeltaEvent(**target.event_fields(), block_id="text:target", delta="target done"),
                TextBlockEndEvent(**target.event_fields(), block_id="text:target"),
                ReplyEndEvent(**target.event_fields()),
                RunEndEvent(**target.event_fields(), status="finished", stop_reason="final"),
            ]
        )
        log.extend(
            [
                RunStartEvent(**future.event_fields(), user_input_chars=6, metadata={"user_input": "future"}),
                ProjectionReadyEvent(
                    **future.event_fields(),
                    projection_id="projection:future",
                    role="pro",
                    scope="session",
                    token_budget=100,
                    summary="FUTURE_PROJECTION_NOT_SEEN",
                ),
                RunEndEvent(**future.event_fields(), status="finished", stop_reason="final"),
            ]
        )

        report = _service(dsn).inspect_run(target.run_id)
        projection_text = str(report["projections_as_seen"])

        assert "TARGET_PROJECTION_AS_SEEN" in projection_text
        assert "FUTURE_PROJECTION_NOT_SEEN" not in projection_text
        assert "working_context" not in report
    finally:
        _cleanup_session(dsn, runtime_session_id)


def test_inspect_run_projects_capability_surface_events(tmp_path: Path) -> None:
    dsn = StorageConfig.from_env().postgres_dsn
    runtime_session_id = _runtime_session_id()
    _connect_or_skip(dsn).close()
    try:
        log = PostgresEventLog(dsn=dsn, runtime_session_id=runtime_session_id, workspace_root=tmp_path)
        ctx = _ctx("capability-surface")
        events = _simple_run_events(ctx, user_input="hello", text="done")
        log.extend(
            [
                events[0],
                CustomEvent(
                    **ctx.event_fields(),
                    name="capability_exposure_resolved",
                    value={
                        "registry_generation": 1,
                        "direct_names": ["read_file"],
                        "callable_names": ["read_file"],
                    },
                ),
                CapabilityGateDecisionEvent(
                    **ctx.event_fields(),
                    tool_call_id="call:read",
                    tool_name="read_file",
                    descriptor_id="builtin:read_file",
                    decision="allow",
                    reason_code=None,
                    permission_policy={"profile": "trusted_host"},
                    exposure_generation=1,
                    availability="available",
                    permission_category="filesystem_read",
                    effective_permission_category="filesystem_read",
                    effective_read_only=True,
                ),
                CustomEvent(
                    **ctx.event_fields(),
                    name="capability_gate_decision",
                    value={
                        "tool_call_id": "call:legacy",
                        "tool_name": "legacy_tool",
                        "decision": "allow",
                    },
                ),
                *events[1:],
            ]
        )

        report = _service(dsn).inspect_run(ctx.run_id)

        capability = report["capability_surface_as_seen"]
        assert capability["latest_exposure"]["direct_names"] == ["read_file"]
        assert capability["latest_exposure"]["callable_names"] == ["read_file"]
        assert capability["gate_decisions"] == [
            {
                "sequence": capability["gate_decisions"][0]["sequence"],
                "run_id": ctx.run_id,
                "turn_id": ctx.turn_id,
                "reply_id": ctx.reply_id,
                "tool_call_id": "call:read",
                "tool_name": "read_file",
                "descriptor_id": "builtin:read_file",
                "decision": "allow",
                "reason_code": None,
                "reason_message": None,
                "suggested_rules": [],
                "result_state": None,
                "policy_mode": None,
                "permission_policy": {"profile": "trusted_host"},
                "exposure_generation": 1,
                "availability": "available",
                "permission_category": "filesystem_read",
                "effective_permission_category": "filesystem_read",
                "effective_read_only": True,
                "capability_context": {},
            }
        ]
    finally:
        _cleanup_session(dsn, runtime_session_id)


def test_inspect_projects_subagent_graph(tmp_path: Path) -> None:
    dsn = StorageConfig.from_env().postgres_dsn
    runtime_session_id = _runtime_session_id()
    _connect_or_skip(dsn).close()
    try:
        log = PostgresEventLog(dsn=dsn, runtime_session_id=runtime_session_id, workspace_root=tmp_path)
        ctx = _ctx("subagent-graph")
        child_runtime_session_id = f"runtime:subagent:{uuid4().hex}"
        subagent_run_id = f"subagent_run:{uuid4().hex}"
        review_task_id = f"subagent_task:{uuid4().hex}"
        prepare_task_id = f"subagent_task:{uuid4().hex}"
        verify_task_id = f"subagent_task:{uuid4().hex}"
        result_id = f"subagent_result:{uuid4().hex}"
        result_artifact_id = f"{subagent_run_id}:result"
        edge_id = f"subagent_edge:{subagent_run_id}:spawn"
        log.extend(
            [
                *_simple_run_events(ctx, user_input="delegate", text="parent done"),
                SubagentTaskCreatedEvent(
                    **ctx.event_fields(),
                    task_id=review_task_id,
                    batch_id="subagent_batch:test",
                    create_tool_call_id="tool:create-tasks",
                    task_key="review",
                    label="Review",
                    profile_id="review_worker",
                    objective_preview="Review the change",
                    depends_on=[],
                ),
                SubagentTaskCreatedEvent(
                    **ctx.event_fields(),
                    task_id=prepare_task_id,
                    batch_id="subagent_batch:test",
                    create_tool_call_id="tool:create-tasks",
                    task_key="prepare",
                    label="Prepare",
                    profile_id="review_worker",
                    objective_preview="Prepare evidence",
                    depends_on=[],
                ),
                SubagentTaskCreatedEvent(
                    **ctx.event_fields(),
                    task_id=verify_task_id,
                    batch_id="subagent_batch:test",
                    create_tool_call_id="tool:create-tasks",
                    task_key="verify",
                    label="Verify",
                    profile_id="verification_worker",
                    objective_preview="Verify the review",
                    depends_on=[prepare_task_id],
                ),
                SubagentTaskFailedEvent(
                    **ctx.event_fields(),
                    task_id=prepare_task_id,
                    subagent_run_id=None,
                    reason_code="synthetic_prepare_failed",
                    reason_message="prepare failed",
                ),
                SubagentRunStartedEvent(
                    **ctx.event_fields(),
                    subagent_run_id=subagent_run_id,
                    edge_id=edge_id,
                    parent_runtime_session_id=runtime_session_id,
                    parent_run_id=ctx.run_id,
                    parent_turn_id=ctx.turn_id,
                    parent_reply_id=ctx.reply_id,
                    parent_context_id="context:parent",
                    parent_model_call_index=1,
                    spawning_tool_call_id="tool:spawn",
                    spawning_tool_name="spawn_agent",
                    child_runtime_session_id=child_runtime_session_id,
                    label="worker",
                    role="worker",
                    task_preview="child task",
                    task_id=review_task_id,
                    batch_id="subagent_batch:test",
                    create_tool_call_id="tool:create-tasks",
                ),
                SubagentTaskStartedEvent(
                    **ctx.event_fields(),
                    task_id=review_task_id,
                    subagent_run_id=subagent_run_id,
                    batch_id="subagent_batch:test",
                    create_tool_call_id="tool:create-tasks",
                    run_index=1,
                    spawn_initiator_kind="tool_call",
                    spawn_initiator_id="tool:create-tasks",
                ),
                SubagentMessageSentEvent(
                    **ctx.event_fields(),
                    edge_id=edge_id,
                    subagent_run_id=subagent_run_id,
                    parent_runtime_session_id=runtime_session_id,
                    parent_run_id=ctx.run_id,
                    child_runtime_session_id=child_runtime_session_id,
                    message_artifact_id=f"{subagent_run_id}:task",
                    message_preview="child task",
                    delivery_kind="spawn_task",
                ),
                SubagentRunCompletedEvent(
                    **ctx.event_fields(),
                    subagent_run_id=subagent_run_id,
                    parent_runtime_session_id=runtime_session_id,
                    child_runtime_session_id=child_runtime_session_id,
                    result_id=result_id,
                    summary="child summary",
                    result_artifact_id=result_artifact_id,
                    artifact_ids=[result_artifact_id],
                ),
                SubagentTaskCompletedEvent(
                    **ctx.event_fields(),
                    task_id=review_task_id,
                    subagent_run_id=subagent_run_id,
                    result_id=result_id,
                    primary_result_artifact_id=result_artifact_id,
                    result_source="inferred",
                ),
                SubagentTaskBlockedEvent(
                    **ctx.event_fields(),
                    task_id=verify_task_id,
                    status="blocked_dependency_failed",
                    blocked_reason="dependency_failed",
                    blocked_by_task_ids=[prepare_task_id],
                    dependency_status_snapshot={prepare_task_id: "failed"},
                    dependency_terminal_event_ids={prepare_task_id: "event_sequence:999"},
                    dependency_generation=999,
                ),
                SubagentEdgeRecordedEvent(
                    **ctx.event_fields(),
                    edge_id=f"subagent_edge:{subagent_run_id}:wait:{uuid4().hex}",
                    edge_kind="wait",
                    parent_runtime_session_id=runtime_session_id,
                    parent_run_id=ctx.run_id,
                    parent_turn_id=ctx.turn_id,
                    parent_reply_id=ctx.reply_id,
                    subagent_run_id=subagent_run_id,
                    child_runtime_session_id=child_runtime_session_id,
                    source_tool_call_id="tool:wait",
                    source_tool_name="wait_agent",
                    result_id=result_id,
                    result_artifact_id=result_artifact_id,
                    returned_to_tool_call_id="tool:wait",
                ),
                SubagentResultDeliveredEvent(
                    **ctx.event_fields(),
                    subagent_run_id=subagent_run_id,
                    parent_runtime_session_id=runtime_session_id,
                    parent_run_id=ctx.run_id,
                    parent_turn_id=ctx.turn_id,
                    parent_reply_id=ctx.reply_id,
                    context_id="context:parent",
                    model_call_index=2,
                    section_id="subagent:results",
                    result_id=result_id,
                    result_artifact_id=result_artifact_id,
                    summary="child summary",
                ),
            ]
        )

        run_report = _service(dsn).inspect_run(ctx.run_id)
        session_report = _service(dsn).inspect_session(runtime_session_id)

        assert run_report["subagent_graph"]["nodes"] == session_report["subagent_graph"]["nodes"]
        [node] = run_report["subagent_graph"]["nodes"]
        assert node["subagent_run_id"] == subagent_run_id
        assert node["status"] == "completed"
        assert node["delivered"] is True
        assert node["consumed_by_wait"] is True
        edge_kinds = {edge["edge_kind"] for edge in run_report["subagent_graph"]["edges"]}
        assert {"spawn", "wait"}.issubset(edge_kinds)
        assert run_report["subagent_graph"]["tasks"] == session_report["subagent_graph"]["tasks"]
        tasks_by_key = {
            task["task_key"]: task
            for task in run_report["subagent_graph"]["tasks"]
        }
        assert tasks_by_key["review"]["current_run_id"] == subagent_run_id
        assert tasks_by_key["review"]["result_id"] == result_id
        assert tasks_by_key["prepare"]["status"] == "failed"
        assert tasks_by_key["verify"]["current_run_id"] is None
        assert tasks_by_key["verify"]["status"] == "blocked_dependency_failed"
        assert tasks_by_key["verify"]["blocked_by_task_ids"] == [prepare_task_id]
        assert tasks_by_key["verify"]["dependency_terminal_event_ids"] == {
            prepare_task_id: "event_sequence:999"
        }
    finally:
        _cleanup_session(dsn, runtime_session_id)


def test_inspect_run_reports_missing_artifact_ref(tmp_path: Path) -> None:
    dsn = StorageConfig.from_env().postgres_dsn
    runtime_session_id = _runtime_session_id()
    _connect_or_skip(dsn).close()
    try:
        log = PostgresEventLog(dsn=dsn, runtime_session_id=runtime_session_id, workspace_root=tmp_path)
        ctx = _ctx("missing-artifact")
        call_id = "call:missing-artifact"
        log.extend(
            [
                RunStartEvent(**ctx.event_fields(), user_input_chars=5, metadata={"user_input": "tool"}),
                ToolCallStartEvent(**ctx.event_fields(), tool_call_id=call_id, tool_call_name="read_file"),
                ToolCallEndEvent(**ctx.event_fields(), tool_call_id=call_id),
                ToolResultStartEvent(**ctx.event_fields(), tool_call_id=call_id, tool_call_name="read_file"),
                ToolResultTextDeltaEvent(**ctx.event_fields(), tool_call_id=call_id, delta="preview"),
                ToolResultEndEvent(
                    **ctx.event_fields(),
                    tool_call_id=call_id,
                    state=ToolResultState.SUCCESS,
                    artifacts=[
                        ToolResultArtifactRef(
                            artifact_id="artifact:missing",
                            role="output",
                            media_type="text/plain",
                            size_bytes=100,
                        )
                    ],
                ),
                RunEndEvent(**ctx.event_fields(), status="finished", stop_reason="final"),
            ]
        )

        report = _service(dsn).inspect_run(ctx.run_id)

        assert any(diagnostic["code"] == "missing_artifact" for diagnostic in report["diagnostics"])
    finally:
        _cleanup_session(dsn, runtime_session_id)


def test_inspect_run_outbox_uses_structured_lineage_not_payload_text(tmp_path: Path) -> None:
    dsn = StorageConfig.from_env().postgres_dsn
    runtime_session_id = _runtime_session_id()
    _connect_or_skip(dsn).close()
    graph_id = f"graph:test/inspector:{uuid4().hex}"
    runtime_outbox_id = f"outbox:runtime:{uuid4().hex}"
    governed_outbox_id = f"outbox:governed:{uuid4().hex}"
    false_positive_outbox_id = f"outbox:false:{uuid4().hex}"
    governance_batch_id = f"governance:inspector:{uuid4().hex}"
    decision_id = f"decision:inspector:{uuid4().hex}"
    candidate_entry_id = f"pool:inspector:{uuid4().hex}"
    try:
        log = PostgresEventLog(dsn=dsn, runtime_session_id=runtime_session_id, workspace_root=tmp_path)
        target = _ctx("outbox-target")
        other = _ctx("outbox-other")
        log.extend(_simple_run_events(target, user_input="target", text="target done"))
        log.extend(_simple_run_events(other, user_input="other", text="other done"))
        with psycopg.connect(dsn) as connection:
            with connection.cursor() as cursor:
                cursor.execute(MEMORY_SUBSTRATE_SCHEMA_SQL)
                cursor.execute(CANDIDATE_POOL_SCHEMA_SQL)
                cursor.execute(
                    """
                    insert into memory_candidates (
                        entry_id,
                        payload,
                        origin,
                        source_session_id,
                        source_run_id,
                        source_turn_id,
                        source_reply_id
                    )
                    values (%s, %s, 'reflection', %s, %s, %s, %s)
                    """,
                    (
                        candidate_entry_id,
                        Jsonb({"statement": "durable preference from target run"}),
                        runtime_session_id,
                        target.run_id,
                        target.turn_id,
                        target.reply_id,
                    ),
                )
                cursor.execute(
                    """
                    insert into memory_governance_decisions (
                        decision_id,
                        governance_batch_id,
                        decision,
                        write_outcome
                    )
                    values (%s, %s, %s, %s)
                    """,
                    (
                        decision_id,
                        governance_batch_id,
                        Jsonb(
                            {
                                "kind": "submit_as_is",
                                "target_entry_id": candidate_entry_id,
                                "reason": "valid durable memory",
                            }
                        ),
                        Jsonb({"kind": "no_write"}),
                    ),
                )
                cursor.execute(
                    """
                    insert into memory_write_outbox (
                        outbox_id,
                        graph_id,
                        target_entry_key,
                        payload,
                        status,
                        mutation_lane,
                        sequence_key
                    )
                    values (%s, %s, %s, %s, 'applied', 'runtime_semantic', %s)
                    """,
                    (
                        runtime_outbox_id,
                        graph_id,
                        f"run-timeline:{target.run_id}",
                        Jsonb({"source_run_id": target.run_id, "documents": []}),
                        graph_id,
                    ),
                )
                cursor.execute(
                    """
                    insert into memory_write_outbox (
                        outbox_id,
                        graph_id,
                        governance_batch_id,
                        decision_id,
                        target_entry_key,
                        payload,
                        status,
                        mutation_lane,
                        sequence_key
                    )
                    values (%s, %s, %s, %s, %s, %s, 'applied', 'governed_memory', %s)
                    """,
                    (
                        governed_outbox_id,
                        graph_id,
                        governance_batch_id,
                        decision_id,
                        candidate_entry_id,
                        Jsonb({"documents": [{"node_id": "mem:governed"}]}),
                        graph_id,
                    ),
                )
                cursor.execute(
                    """
                    insert into memory_write_outbox (
                        outbox_id,
                        graph_id,
                        target_entry_key,
                        payload,
                        status,
                        mutation_lane,
                        sequence_key
                    )
                    values (%s, %s, %s, %s, 'applied', 'runtime_semantic', %s)
                    """,
                    (
                        false_positive_outbox_id,
                        graph_id,
                        "run-timeline:other",
                        Jsonb(
                            {
                                "source_run_id": other.run_id,
                                "documents": [{"statement": f"mentions {target.run_id} only as text"}],
                            }
                        ),
                        graph_id,
                    ),
                )

        report = _service(dsn).inspect_run(target.run_id)
        outbox_ids = {row["outbox_id"] for row in report["outbox"]}

        assert runtime_outbox_id in outbox_ids
        assert governed_outbox_id in outbox_ids
        assert false_positive_outbox_id not in outbox_ids
    finally:
        with _connect_or_skip(dsn) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    "delete from memory_write_outbox where outbox_id = any(%s)",
                    ([runtime_outbox_id, governed_outbox_id, false_positive_outbox_id],),
                )
                cursor.execute("delete from memory_governance_decisions where decision_id = %s", (decision_id,))
        _cleanup_session(dsn, runtime_session_id)


def test_inspect_memory_unknown_id_raises_not_found() -> None:
    dsn = StorageConfig.from_env().postgres_dsn
    _connect_or_skip(dsn).close()
    memory_id = f"mem:inspector-missing:{uuid4().hex}"
    with psycopg.connect(dsn) as connection:
        with connection.cursor() as cursor:
            cursor.execute(MEMORY_SUBSTRATE_SCHEMA_SQL)

    with pytest.raises(KeyError, match=memory_id):
        _service(dsn).inspect_memory(memory_id)


def test_inspect_health_reports_failed_outbox(tmp_path: Path) -> None:
    dsn = StorageConfig.from_env().postgres_dsn
    _connect_or_skip(dsn).close()
    outbox_id = f"outbox:inspector:{uuid4().hex}"
    graph_id = f"graph:test/inspector:{uuid4().hex}"
    try:
        with psycopg.connect(dsn, connect_timeout=2) as connection:
            with connection.cursor() as cursor:
                cursor.execute(MEMORY_SUBSTRATE_SCHEMA_SQL)
                cursor.execute(
                    """
                    insert into memory_write_outbox (
                        outbox_id,
                        graph_id,
                        target_entry_key,
                        payload,
                        status,
                        mutation_lane,
                        sequence_key,
                        last_error
                    )
                    values (%s, %s, %s, '{}'::jsonb, 'failed', 'runtime_semantic', %s, 'boom')
                    """,
                    (outbox_id, graph_id, f"target:{outbox_id}", graph_id),
                )

        report = _service(dsn).inspect_health()

        assert any(row["status"] == "failed" for row in report["outbox"])
    finally:
        with _connect_or_skip(dsn) as connection:
            with connection.cursor() as cursor:
                cursor.execute("delete from memory_write_outbox where outbox_id = %s", (outbox_id,))
