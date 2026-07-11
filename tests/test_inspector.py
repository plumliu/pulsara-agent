from __future__ import annotations

from pathlib import Path
from uuid import uuid4

import psycopg
import pytest
from psycopg.types.json import Jsonb

from tests.conftest import run_start_permission_fields
from tests.support import (
    compaction_completed_contract_fields,
    compaction_started_contract_fields,
    context_compiled_contract_fields,
    model_call_end_fields,
    model_call_start_fields,
    test_resolved_call_fact,
)

from pulsara_agent.event import (
    CapabilityGateDecisionEvent,
    ContextCompiledEvent,
    ContextCompactionCompletedEvent,
    ContextCompactionMemoryCandidatesProposedEvent,
    ContextCompactionStartedEvent,
    CustomEvent,
    EventContext,
    McpCapabilitySnapshotInstalledEvent,
    ModelCallStartEvent,
    ModelCallEndEvent,
    ModelCallRejectedEvent,
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
from pulsara_agent.inspector.service import (
    _context_compilation_projection,
    _model_contract_projection,
)
from pulsara_agent.memory.artifacts.postgres_archive import PostgresArtifactStore
from pulsara_agent.primitives.model_call import (
    CompactionTargetEstimateFact,
    ModelCallPurpose,
)
from pulsara_agent.primitives.mcp import (
    McpInstalledServerSnapshotFact,
    McpReconcileAttemptSummaryFact,
    McpServerLifecycleTimingFact,
)
from pulsara_agent.memory.candidates.pool import CANDIDATE_POOL_SCHEMA_SQL
from pulsara_agent.message import ToolResultArtifactRef, ToolResultState
from pulsara_agent.runtime.permission import PermissionMode, preset_to_policy
from pulsara_agent.runtime.subagent.facts import subagent_dependency_generation
from pulsara_agent.settings import StorageConfig
from pulsara_agent.storage import MEMORY_SUBSTRATE_SCHEMA_SQL


def _stored(event, sequence: int):
    return event.model_copy(update={"sequence": sequence})


def _compiled_call_events(*, reported_usage: bool = True):
    ctx = _ctx("model-contract")
    call = test_resolved_call_fact()
    permission = run_start_permission_fields(ctx.run_id)
    permission["model_target"] = call.target
    usage_fields = model_call_end_fields(
        input_tokens=10,
        output_tokens=5,
        estimated_input_tokens=12,
        resolved_call=call,
    )
    if not reported_usage:
        usage_fields["usage_status"] = "missing"
        usage_fields["usage"] = None
    return (
        ctx,
        call,
        [
            _stored(
                RunStartEvent(
                    **ctx.event_fields(),
                    **permission,
                    user_input_chars=1,
                ),
                1,
            ),
            _stored(
                ContextCompiledEvent(
                    **ctx.event_fields(),
                    **context_compiled_contract_fields(
                        resolved_call=call,
                        estimated_tokens=12,
                        tools_estimated_tokens=0,
                    ),
                    context_id="context:model-contract",
                    model_call_index=1,
                ),
                2,
            ),
            _stored(
                ModelCallStartEvent(
                    **ctx.event_fields(),
                    **model_call_start_fields(
                        context_id="context:model-contract",
                        model_call_index=1,
                        resolved_call=call,
                    ),
                ),
                3,
            ),
            _stored(ModelCallEndEvent(**ctx.event_fields(), **usage_fields), 4),
        ],
    )


def test_inspector_joins_compiled_call_by_resolved_id() -> None:
    _ctx_value, call, events = _compiled_call_events()
    projection = _model_contract_projection(events)
    model_call = projection["model_calls"][0]
    assert model_call["resolved_model_call_id"] == call.resolved_model_call_id
    assert model_call["join_status"] == "compiled_started_completed"
    assert model_call["compile_context_ids"] == ["context:model-contract"]
    assert model_call["requested_model_id"] == call.target.model_id
    assert model_call["reported_model_id"] == call.target.model_id
    assert model_call["model_identity_policy"] == "accept_reported"
    assert model_call["model_identity_relation"] == "exact"


def test_inspector_projects_accepted_reported_model_alias() -> None:
    _ctx_value, call, events = _compiled_call_events()
    end = events[-1].model_copy(update={"reported_model_id": "provider-snapshot"})
    projection = _model_contract_projection([*events[:-1], end])
    model_call = projection["model_calls"][0]

    assert model_call["requested_model_id"] == call.target.model_id
    assert model_call["reported_model_id"] == "provider-snapshot"
    assert model_call["model_identity_relation"] == "different"
    assert not any(
        diagnostic["code"] == "reported_model_identity_policy_violation"
        for diagnostic in projection["diagnostics"]
    )


def test_inspector_reports_start_without_end() -> None:
    _ctx_value, _call, events = _compiled_call_events()
    projection = _model_contract_projection(events[:-1])
    assert projection["model_calls"][0]["join_status"] == "started_missing_end"


def test_inspector_reports_rejected_without_start() -> None:
    ctx, call, events = _compiled_call_events()
    rejected = _stored(
        ModelCallRejectedEvent(
            **ctx.event_fields(),
            resolved_call=call,
            context_id="context:model-contract",
            model_call_index=1,
            reason_code="model_input_budget_exceeded",
            estimated_input_tokens=999,
            input_budget_tokens=call.target.context_budget.input_budget_tokens,
        ),
        3,
    )
    projection = _model_contract_projection([*events[:2], rejected])
    assert projection["model_calls"][0]["join_status"] == "compiled_rejected"


def test_inspector_reports_call_fact_mismatch() -> None:
    _ctx_value, _call, events = _compiled_call_events()
    end = events[-1].model_copy(update={"target_fingerprint": "sha256:mismatch"})
    projection = _model_contract_projection([*events[:-1], end])
    assert projection["model_calls"][0]["join_status"] == "fact_mismatch"
    assert any(
        diagnostic["code"] == "model_target_fingerprint_mismatch"
        for diagnostic in projection["diagnostics"]
    )


def test_inspector_projects_run_target() -> None:
    _ctx_value, call, events = _compiled_call_events()
    projection = _model_contract_projection(events)
    target = projection["model_targets"][0]
    assert target["target_fingerprint"] == call.target.target_fingerprint
    assert any(source["source"] == "run_start" for source in target["sources"])


def test_inspector_projects_per_call_usage() -> None:
    _ctx_value, _call, events = _compiled_call_events()
    projection = _model_contract_projection(events)
    assert projection["model_calls"][0]["usage"] == {
        "input_tokens": 10,
        "cached_input_tokens": None,
        "output_tokens": 5,
        "reasoning_output_tokens": None,
        "total_tokens": 15,
    }


def test_inspector_aggregates_run_usage_by_call_purpose() -> None:
    ctx, _call, events = _compiled_call_events()
    aggregate = _model_contract_projection(events)["usage_by_run"][0]
    assert aggregate["run_id"] == ctx.run_id
    assert aggregate["total_tokens"] == 15
    assert aggregate["by_purpose"] == [
        {
            "purpose": "agent_model_loop",
            "input_tokens": 10,
            "output_tokens": 5,
            "total_tokens": 15,
            "reported_call_count": 1,
            "missing_usage_call_count": 0,
        }
    ]


def test_inspector_does_not_treat_missing_cached_breakdown_as_zero() -> None:
    _ctx_value, _call, events = _compiled_call_events()
    aggregate = _model_contract_projection(events)["usage_by_run"][0]
    assert aggregate["cached_input_tokens"] is None
    assert aggregate["cached_input_tokens_complete"] is False
    assert aggregate["reasoning_output_tokens"] is None


def test_inspector_does_not_recompute_historical_limits() -> None:
    _ctx_value, call, events = _compiled_call_events()
    projection = _model_contract_projection(events)
    projected = projection["model_targets"][0]["fact"]
    assert projected["limits"] == call.target.limits.model_dump(mode="json")


def test_inspector_projects_compaction_target_and_summarizer() -> None:
    ctx = _ctx("inspector-compaction-contract")
    fields = compaction_started_contract_fields()
    event = _stored(
        ContextCompactionStartedEvent(
            **ctx.event_fields(),
            **fields,
            compaction_id="compaction:inspector-contract",
            trigger="manual",
            reason="inspection",
            window_number=1,
            window_id="window:inspector-contract",
            threshold_tokens=100,
            through_sequence=10,
            keep_after_sequence=8,
        ),
        1,
    )

    projection = _model_contract_projection([event])

    contract = projection["compaction_model_contracts"][0]
    assert (
        contract["target_fingerprint"]
        == fields["target_model_target"].target_fingerprint
    )
    assert (
        contract["resolved_model_call_id"]
        == fields["summarizer_call"].resolved_model_call_id
    )
    call = projection["model_calls"][0]
    assert call["purpose"] == "context_compaction_summary"
    assert (
        call["target_fingerprint"]
        == fields["summarizer_call"].target.target_fingerprint
    )
    target_fingerprints = {
        item["target_fingerprint"] for item in projection["model_targets"]
    }
    assert fields["target_model_target"].target_fingerprint in target_fingerprints
    assert fields["summarizer_call"].target.target_fingerprint in target_fingerprints


def test_inspector_displays_compaction_estimate_scope_and_baseline() -> None:
    ctx = _ctx("inspector-compaction-baseline")
    fields = compaction_started_contract_fields()
    target = fields["target_model_target"]
    fields["target_estimate"] = CompactionTargetEstimateFact(
        estimate_scope="compiled_context_baseline",
        basis_context_id="context:basis",
        basis_context_compiled_sequence=7,
        target_fingerprint=target.target_fingerprint,
        non_transcript_baseline_tokens=75,
        transcript_tokens_before=225,
        estimated_tokens_before=300,
        summary_tokens_reserved=50,
        retained_transcript_tokens=0,
        protected_transcript_tokens=0,
        summary_tokens_actual=None,
        transcript_tokens_after=None,
        estimated_tokens_after=None,
        predicted_post_target_reached=None,
    )
    event = _stored(
        ContextCompactionStartedEvent(
            **ctx.event_fields(),
            **fields,
            compaction_id="compaction:inspector-baseline",
            trigger="auto",
            reason="threshold",
            window_number=1,
            window_id="window:inspector-baseline",
            threshold_tokens=100,
            through_sequence=10,
            keep_after_sequence=8,
        ),
        1,
    )

    estimate = _model_contract_projection([event])["compaction_model_contracts"][0][
        "target_estimate"
    ]

    assert estimate["estimate_scope"] == "compiled_context_baseline"
    assert estimate["basis_context_id"] == "context:basis"
    assert estimate["basis_context_compiled_sequence"] == 7
    assert estimate["non_transcript_baseline_tokens"] == 75


def test_inspector_does_not_claim_historical_governance_usage() -> None:
    _ctx_value, _call, events = _compiled_call_events()
    projection = _model_contract_projection(events)

    assert all(
        call["purpose"] != "memory_governance" for call in projection["model_calls"]
    )
    assert "governance_model_contracts" not in projection


def test_inspector_allows_direct_call_without_compiled_context() -> None:
    ctx = _ctx("direct-model-contract")
    call = test_resolved_call_fact(purpose=ModelCallPurpose.MEMORY_REFLECTION)
    start = _stored(
        ModelCallStartEvent(
            **ctx.event_fields(),
            resolved_call=call,
            context_id="context:direct",
            model_call_index=None,
        ),
        1,
    )
    projection = _context_compilation_projection([start])
    assert (
        projection["model_call_joins"][0]["join_status"]
        == "direct_context_not_applicable"
    )
    assert projection["diagnostics"] == []


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


def _subagent_context_snapshot() -> dict[str, object]:
    return {
        "mode": "isolated",
        "include_parent_summary": False,
        "include_parent_current_task": True,
        "include_parent_memory_projection": False,
        "include_parent_artifact_refs": False,
        "max_parent_context_chars": None,
        "fork_source_context_id": None,
    }


def _subagent_capability_snapshot() -> dict[str, object]:
    return {
        "profile_id": "subagent_capability_profile:test",
        "profile_name": "review_worker",
        "inherited_from_parent_context_id": "context:parent",
        "permission_mode": PermissionMode.READ_ONLY.value,
        "permission_policy": preset_to_policy(PermissionMode.READ_ONLY).to_dict(),
        "allowed_tool_names": [
            "artifact_read",
            "read_file",
            "report_agent_phase",
            "report_agent_result",
        ],
        "allowed_descriptor_ids": [],
        "allowed_skill_names": [],
        "allowed_mcp_server_ids": [],
        "can_spawn_subagents": False,
        "max_spawn_depth_from_root": 0,
        "memory_enabled": False,
        "computed_from_parent_exposure_generation": None,
        "diagnostics": [],
    }


def _subagent_budget_snapshot() -> dict[str, object]:
    return {
        "max_concurrent_children_per_parent_run": 4,
        "max_concurrent_children_per_host_session": 8,
        "max_spawn_depth_from_root": 0,
        "child_timeout_seconds": None,
        "max_total_child_runs_per_parent_run": 16,
        "max_result_summary_chars_per_child": 4_000,
        "max_subagent_results_per_parent_compile": 8,
    }


def _simple_run_events(ctx: EventContext, *, user_input: str, text: str):
    return [
        RunStartEvent(
            **ctx.event_fields(),
            **run_start_permission_fields(ctx.run_id),
            user_input_chars=len(user_input),
            metadata={"user_input": user_input},
        ),
        ReplyStartEvent(**ctx.event_fields(), name="assistant"),
        TextBlockStartEvent(**ctx.event_fields(), block_id=f"text:{ctx.run_id}"),
        TextBlockDeltaEvent(
            **ctx.event_fields(), block_id=f"text:{ctx.run_id}", delta=text
        ),
        TextBlockEndEvent(**ctx.event_fields(), block_id=f"text:{ctx.run_id}"),
        ReplyEndEvent(**ctx.event_fields()),
        RunEndEvent(**ctx.event_fields(), status="finished", stop_reason="final"),
    ]


def _mcp_installed_event(ctx: EventContext) -> McpCapabilitySnapshotInstalledEvent:
    timing = McpServerLifecycleTimingFact(
        queued_at_utc="2026-01-01T00:00:00Z",
        connect_started_at_utc="2026-01-01T00:00:00Z",
        connect_ended_at_utc="2026-01-01T00:00:00.003000Z",
        discovery_started_at_utc="2026-01-01T00:00:00.003000Z",
        discovery_ended_at_utc="2026-01-01T00:00:00.010000Z",
        completed_at_utc="2026-01-01T00:00:00.010000Z",
        connect_duration_seconds=0.003,
        discovery_duration_seconds=0.007,
        total_duration_seconds=0.01,
    )
    attempt = McpReconcileAttemptSummaryFact(
        server_id="docs",
        reconcile_attempt_id="mcp_attempt:inspect",
        reconcile_trigger="initial",
        attempt_status="ready",
        retry_attempt=1,
        request_count=2,
        page_count=2,
        cache_outcome="miss",
        stale_candidates_discarded_since_previous_install=1,
    )
    snapshot = McpInstalledServerSnapshotFact(
        server_id="docs",
        status="ready",
        required=False,
        changed_in_this_installation=True,
        attempt=attempt,
        snapshot_id="mcp_snapshot:inspect",
        discovery_generation=2,
        event_safe_config_fingerprint="sha256:server",
        snapshot_semantic_fingerprint="sha256:catalog",
        tool_count=3,
        lifecycle_timing=timing,
        catalog_artifact_id=None,
    )
    return McpCapabilitySnapshotInstalledEvent(
        **ctx.event_fields(),
        installation_id="mcp_installation:inspect",
        config_epoch=1,
        event_safe_config_set_fingerprint="sha256:set",
        installation_triggers=("initial",),
        server_snapshots=(snapshot,),
        total_installed_tool_count=3,
        added_tool_count=3,
        revoked_tool_count=0,
        changed_tool_names_bounded=("mcp__docs__a", "mcp__docs__b"),
    )


def test_inspect_run_rebuilds_timeline_and_assistant_reply(tmp_path: Path) -> None:
    dsn = StorageConfig.from_env().postgres_dsn
    runtime_session_id = _runtime_session_id()
    _connect_or_skip(dsn).close()
    try:
        log = PostgresEventLog(
            dsn=dsn, runtime_session_id=runtime_session_id, workspace_root=tmp_path
        )
        ctx = _ctx("basic")
        log.extend(
            _simple_run_events(ctx, user_input="hello", text="PULSARA_INSPECTOR_TEXT")
        )

        report = _service(dsn).inspect_run(ctx.run_id)

        assert report["inspect_kind"] == "run"
        assert report["session"]["id"] == runtime_session_id
        assert report["run"]["status"] == "finished"
        assert report["canonical"]["current_user_input"] == "hello"
        assert (
            report["canonical"]["permission_snapshot"]["permission_snapshot_id"]
            == f"permission_snapshot:{ctx.run_id}"
        )
        assert (
            report["canonical"]["permission_snapshot"]["permission_mode"]
            == "bypass-permissions"
        )
        assert (
            report["canonical"]["permission_snapshot"]["permission_snapshot_source"]
            == "session_default"
        )
        assert report["timeline"]["status"] == "completed"
        assert (
            "PULSARA_INSPECTOR_TEXT"
            in report["assistant_replies"][0]["content"][0]["text"]
        )
        assert report["diagnostics"] == []
    finally:
        _cleanup_session(dsn, runtime_session_id)


def test_inspect_run_reports_stale_run_projection_without_repairing(
    tmp_path: Path,
) -> None:
    dsn = StorageConfig.from_env().postgres_dsn
    runtime_session_id = _runtime_session_id()
    _connect_or_skip(dsn).close()
    try:
        log = PostgresEventLog(
            dsn=dsn, runtime_session_id=runtime_session_id, workspace_root=tmp_path
        )
        ctx = _ctx("stale")
        log.extend(_simple_run_events(ctx, user_input="hello", text="done"))
        with psycopg.connect(dsn, connect_timeout=2) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    "update runs set status = 'running', stop_reason = null, completed_at = null where id = %s",
                    (ctx.run_id,),
                )

        report = _service(dsn).inspect_run(ctx.run_id)

        assert any(
            diagnostic["code"] == "run_projection_stale"
            for diagnostic in report["diagnostics"]
        )
        with psycopg.connect(dsn, connect_timeout=2) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    "select status, completed_at from runs where id = %s", (ctx.run_id,)
                )
                status, completed_at = cursor.fetchone()
        assert status == "running"
        assert completed_at is None
    finally:
        _cleanup_session(dsn, runtime_session_id)


def test_inspect_run_prior_messages_are_bounded_to_target_run_start(
    tmp_path: Path,
) -> None:
    dsn = StorageConfig.from_env().postgres_dsn
    runtime_session_id = _runtime_session_id()
    _connect_or_skip(dsn).close()
    try:
        log = PostgresEventLog(
            dsn=dsn, runtime_session_id=runtime_session_id, workspace_root=tmp_path
        )
        first = _ctx("first")
        target = _ctx("target")
        future = _ctx("future")
        log.extend(
            _simple_run_events(first, user_input="first user", text="FIRST_ASSISTANT")
        )
        log.extend(
            _simple_run_events(
                target, user_input="target user", text="TARGET_ASSISTANT"
            )
        )
        log.extend(
            _simple_run_events(
                future, user_input="future user", text="FUTURE_ASSISTANT"
            )
        )

        report = _service(dsn).inspect_run(target.run_id)
        prior_text = str(report["prior_messages_as_seen"])

        assert "first user" in prior_text
        assert "FIRST_ASSISTANT" in prior_text
        assert "target user" not in prior_text
        assert "FUTURE_ASSISTANT" not in prior_text
    finally:
        _cleanup_session(dsn, runtime_session_id)


def test_inspect_run_prior_messages_use_context_compaction_boundary(
    tmp_path: Path,
) -> None:
    dsn = StorageConfig.from_env().postgres_dsn
    runtime_session_id = _runtime_session_id()
    _connect_or_skip(dsn).close()
    try:
        log = PostgresEventLog(
            dsn=dsn, runtime_session_id=runtime_session_id, workspace_root=tmp_path
        )
        old = _ctx("compacted-old")
        target = _ctx("compacted-target")
        log.extend(
            _simple_run_events(
                old, user_input="old user text", text="OLD_ASSISTANT_TEXT"
            )
        )
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
                **compaction_completed_contract_fields(
                    estimated_tokens_before=10_000,
                    estimated_tokens_after=100,
                ),
                compaction_id=f"context_compaction:{uuid4().hex}",
                trigger="auto",
                reason="mid_turn_context_threshold",
                window_number=1,
                window_id="context_window:test",
                summary_artifact_id=summary_artifact_id,
                summary_chars=len("COMPACTED_OLD_CONTEXT_SUMMARY"),
                threshold_tokens=200_000,
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
        log.extend(
            _simple_run_events(
                target, user_input="target user", text="TARGET_ASSISTANT"
            )
        )

        report = _service(dsn).inspect_run(target.run_id)
        prior_text = str(report["prior_messages_as_seen"])

        assert (
            report["compaction_boundary_as_seen"]["summary_artifact_id"]
            == summary_artifact_id
        )
        assert report["compaction_boundary_as_seen"]["phase"] == "mid_turn"
        assert (
            report["compaction_boundary_as_seen"]["safe_point"]
            == "before_followup_model_call"
        )
        assert report["compaction_boundary_as_seen"]["current_run_id"] == target.run_id
        assert report["compaction_boundary_as_seen"]["max_compactable_sequence"] == 7
        assert report["compaction_boundary_as_seen"]["tail_message_count"] == 3
        assert "COMPACTED_OLD_CONTEXT_SUMMARY" in prior_text
        assert "<context-compaction-summary" in prior_text
        assert "old user text" not in prior_text
        assert "OLD_ASSISTANT_TEXT" not in prior_text
    finally:
        _cleanup_session(dsn, runtime_session_id)


def test_inspect_run_reports_context_compilation_and_model_call_join(
    tmp_path: Path,
) -> None:
    dsn = StorageConfig.from_env().postgres_dsn
    runtime_session_id = _runtime_session_id()
    _connect_or_skip(dsn).close()
    try:
        log = PostgresEventLog(
            dsn=dsn, runtime_session_id=runtime_session_id, workspace_root=tmp_path
        )
        ctx = _ctx("context-compiled")
        context_id = f"context:{uuid4().hex}"
        compiled_fields = context_compiled_contract_fields(
            estimated_tokens=321,
            tools_estimated_tokens=42,
        )
        log.extend(
            [
                RunStartEvent(
                    **ctx.event_fields(),
                    **run_start_permission_fields(ctx.run_id),
                    user_input_chars=5,
                    metadata={"user_input": "hello"},
                ),
                ContextCompiledEvent(
                    **ctx.event_fields(),
                    **compiled_fields,
                    context_id=context_id,
                    model_call_index=1,
                    sections=[
                        {
                            "id": "transcript:current_user",
                            "source_id": "current_user",
                            "channel": "current_user",
                            "included": True,
                            "render_mode": "full",
                            "estimated_tokens": 2,
                            "metadata": {
                                "timing": {
                                    "compiled_at_utc": "2026-07-09T01:02:03+00:00",
                                    "source": {
                                        "freshness": "current_turn",
                                        "source_started_at": "2026-07-09T01:02:00+00:00",
                                        "source_ended_at": "2026-07-09T01:02:00+00:00",
                                    },
                                    "age_seconds": 3,
                                }
                            },
                        },
                        {
                            "id": "component:memory",
                            "source_id": "memory",
                            "channel": "leading_user",
                            "included": True,
                            "render_mode": "full",
                            "estimated_tokens": 4,
                            "metadata": {
                                "timing": {
                                    "compiled_at_utc": "2026-07-09T01:02:03+00:00",
                                    "source": {
                                        "freshness": "memory_projection",
                                        "observed_at": "2026-07-09T01:01:59+00:00",
                                    },
                                    "age_seconds": 4,
                                }
                            },
                        },
                    ],
                    tool_specs=[
                        {"name": "read_file", "estimated_tokens": 42, "included": True}
                    ],
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
                            "tool_name": "terminal",
                            "model_tool_name": "terminal",
                            "tool_timing": {
                                "observed_at": "2026-07-09T01:02:03Z",
                                "freshness": "current_tool_observation",
                            },
                            "timing_policy": "minimal",
                            "rendered_timing_chars": 92,
                            "diagnostics": [],
                        },
                        {
                            "tool_call_id": "call:old",
                            "tool_name": "read_file",
                            "model_tool_name": "read_file",
                            "timing_policy": "not_applicable",
                            "rendered_timing_chars": 0,
                            "diagnostics": [],
                        },
                    ],
                ),
                ModelCallStartEvent(
                    **ctx.event_fields(),
                    **model_call_start_fields(
                        context_id=context_id,
                        resolved_call=compiled_fields["resolved_call"],
                    ),
                ),
                ReplyStartEvent(**ctx.event_fields(), name="assistant"),
                TextBlockStartEvent(
                    **ctx.event_fields(), block_id=f"text:{ctx.run_id}"
                ),
                TextBlockDeltaEvent(
                    **ctx.event_fields(), block_id=f"text:{ctx.run_id}", delta="done"
                ),
                TextBlockEndEvent(**ctx.event_fields(), block_id=f"text:{ctx.run_id}"),
                ReplyEndEvent(**ctx.event_fields()),
                RunEndEvent(
                    **ctx.event_fields(), status="finished", stop_reason="final"
                ),
            ]
        )

        report = _service(dsn).inspect_run(ctx.run_id)

        contexts = report["contexts_as_seen"]
        assert contexts["latest"]["context_id"] == context_id
        assert contexts["latest"]["tools_estimated_tokens"] == 42
        assert contexts["latest"]["sections"][0]["channel"] == "current_user"
        assert contexts["latest"]["section_timings"][0]["status"] == "present"
        assert contexts["latest"]["section_timings"][0]["freshness"] == "current_turn"
        assert (
            contexts["latest"]["section_timings"][0]["source_started_at"]
            == "2026-07-09T01:02:00+00:00"
        )
        assert (
            contexts["latest"]["section_timings"][0]["source_ended_at"]
            == "2026-07-09T01:02:00+00:00"
        )
        assert contexts["latest"]["section_timings"][0]["age_seconds"] == 3
        assert (
            contexts["latest"]["section_timings"][1]["freshness"] == "memory_projection"
        )
        assert (
            contexts["latest"]["section_timings"][1]["observed_at"]
            == "2026-07-09T01:01:59+00:00"
        )
        assert contexts["latest"]["section_timings"][1]["source_started_at"] is None
        assert contexts["latest"]["section_timings"][1]["source_ended_at"] is None
        assert contexts["latest"]["tool_result_timings"][0]["status"] == "present"
        assert (
            contexts["latest"]["tool_result_timings"][0]["observed_at"]
            == "2026-07-09T01:02:03Z"
        )
        assert (
            contexts["latest"]["tool_result_timings"][0]["timing_policy"] == "minimal"
        )
        assert (
            contexts["latest"]["tool_result_timings"][1]["status"] == "not_applicable"
        )
        assert contexts["latest"]["lifecycle_decisions"][0]["decision"] == "invalidated"
        assert contexts["model_call_joins"][0]["join_status"] == "matched"
        assert contexts["model_call_joins"][0]["context_compiled_sequence"] is not None
        assert contexts["diagnostics"] == []
    finally:
        _cleanup_session(dsn, runtime_session_id)


def test_inspect_session_reports_missing_context_compaction_summary_artifact(
    tmp_path: Path,
) -> None:
    dsn = StorageConfig.from_env().postgres_dsn
    runtime_session_id = _runtime_session_id()
    _connect_or_skip(dsn).close()
    try:
        log = PostgresEventLog(
            dsn=dsn, runtime_session_id=runtime_session_id, workspace_root=tmp_path
        )
        ctx = _ctx("missing-compaction-summary")
        log.extend(_simple_run_events(ctx, user_input="old", text="done"))
        log.append(
            ContextCompactionCompletedEvent(
                **ctx.event_fields(),
                **compaction_completed_contract_fields(
                    estimated_tokens_before=200_001,
                    estimated_tokens_after=1_000,
                ),
                compaction_id=f"context_compaction:{uuid4().hex}",
                trigger="auto",
                reason="context_threshold",
                window_number=1,
                window_id="context_window:missing",
                summary_artifact_id=f"artifact:missing:{uuid4().hex}",
                summary_chars=10,
                threshold_tokens=200_000,
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


def test_inspect_session_links_context_compaction_memory_candidates(
    tmp_path: Path,
) -> None:
    dsn = StorageConfig.from_env().postgres_dsn
    runtime_session_id = _runtime_session_id()
    _connect_or_skip(dsn).close()
    ctx = _ctx("compaction-candidates")
    candidate_entry_id = f"pool:inspector:{uuid4().hex}"
    decision_id = f"decision:inspector:{uuid4().hex}"
    governance_batch_id = f"governance:inspector:{uuid4().hex}"
    try:
        log = PostgresEventLog(
            dsn=dsn, runtime_session_id=runtime_session_id, workspace_root=tmp_path
        )
        log.extend(_simple_run_events(ctx, user_input="compact me", text="done"))
        archive = PostgresArtifactStore(dsn)
        summary_artifact_id = f"context_compaction_summary:{uuid4().hex}"
        archive.put_text(
            summary_artifact_id,
            "Compaction summary mentions release sync workflow.",
            session_id=runtime_session_id,
            run_id=ctx.run_id,
            metadata={"kind": "context_compaction_summary", "do_not_write_back": True},
        )
        completed = log.append(
            ContextCompactionCompletedEvent(
                **ctx.event_fields(),
                **compaction_completed_contract_fields(
                    estimated_tokens_before=200_001,
                    estimated_tokens_after=4_000,
                ),
                compaction_id=f"context_compaction:{uuid4().hex}",
                trigger="manual",
                reason="user_requested",
                window_number=1,
                window_id=f"context_window:{uuid4().hex}",
                summary_artifact_id=summary_artifact_id,
                summary_chars=48,
                threshold_tokens=200_000,
                through_sequence=10,
                keep_after_sequence=10,
                included_run_ids=[ctx.run_id],
            )
        )
        log.append(
            ContextCompactionMemoryCandidatesProposedEvent(
                **ctx.event_fields(),
                compaction_id=completed.compaction_id,
                source_event_id=completed.id,
                source_event_sequence=completed.sequence or 0,
                summary_artifact_id=summary_artifact_id,
                candidate_entry_ids=[candidate_entry_id],
                attempted_count=1,
                proposed_count=1,
            )
        )
        with psycopg.connect(dsn) as connection:
            with connection.cursor() as cursor:
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
                        source_reply_id,
                        source_event_id,
                        source_artifact_id,
                        intent_fingerprint,
                        metadata
                    )
                    values (%s, %s, 'compaction', %s, %s, %s, %s, %s, %s, %s, %s)
                    """,
                    (
                        candidate_entry_id,
                        Jsonb(
                            {
                                "payload_kind": "valid",
                                "candidate": {
                                    "kind": "Preference",
                                    "candidate_id": "candidate:compaction-inspector",
                                    "statement": "The user prefers syncing release before pushing GitHub.",
                                    "scope": "ctx:workspace/test",
                                    "source_authority": "conversation_evidence",
                                    "verification_status": "inferred",
                                    "evidence_ids": [],
                                },
                            }
                        ),
                        runtime_session_id,
                        ctx.run_id,
                        ctx.turn_id,
                        ctx.reply_id,
                        completed.id,
                        summary_artifact_id,
                        "sha256:inspector",
                        Jsonb(
                            {
                                "source": "context_compaction",
                                "compaction_id": completed.compaction_id,
                                "summary_artifact_id": summary_artifact_id,
                                "summary_excerpt": "Compaction summary mentions release sync workflow.",
                            }
                        ),
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
                                "kind": "skip",
                                "target_entry_ids": [candidate_entry_id],
                                "reason": "weak",
                                "skip_reason": "not_durable",
                            }
                        ),
                        Jsonb({"kind": "no_write"}),
                    ),
                )

        report = _service(dsn).inspect_session(runtime_session_id)

        window = next(
            item
            for item in report["compaction_windows"]
            if item["compaction_id"] == completed.compaction_id
        )
        assert window["candidate_proposals"][0]["candidate_entry_ids"] == [
            candidate_entry_id
        ]
        assert window["candidate_proposals"][0]["proposed_count"] == 1
        assert window["memory_candidates"][0]["entry_id"] == candidate_entry_id
        assert window["memory_candidates"][0]["origin"] == "compaction"
        assert window["memory_candidates"][0]["source_event_id"] == completed.id
        assert window["memory_candidates"][0]["metadata"]["summary_excerpt"].startswith(
            "Compaction summary"
        )
        assert (
            window["memory_candidates"][0]["governance_decisions"][0]["decision_id"]
            == decision_id
        )
    finally:
        with _connect_or_skip(dsn) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    "delete from memory_governance_decisions where decision_id = %s",
                    (decision_id,),
                )
                cursor.execute(
                    "delete from memory_candidates where entry_id = %s",
                    (candidate_entry_id,),
                )
        _cleanup_session(dsn, runtime_session_id)


def test_inspect_run_reports_only_projections_seen_by_that_run(tmp_path: Path) -> None:
    dsn = StorageConfig.from_env().postgres_dsn
    runtime_session_id = _runtime_session_id()
    _connect_or_skip(dsn).close()
    try:
        log = PostgresEventLog(
            dsn=dsn, runtime_session_id=runtime_session_id, workspace_root=tmp_path
        )
        target = _ctx("target-projection")
        future = _ctx("future-projection")
        log.extend(
            [
                RunStartEvent(
                    **target.event_fields(),
                    **run_start_permission_fields(target.run_id),
                    user_input_chars=6,
                    metadata={"user_input": "target"},
                ),
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
                TextBlockDeltaEvent(
                    **target.event_fields(), block_id="text:target", delta="target done"
                ),
                TextBlockEndEvent(**target.event_fields(), block_id="text:target"),
                ReplyEndEvent(**target.event_fields()),
                RunEndEvent(
                    **target.event_fields(), status="finished", stop_reason="final"
                ),
            ]
        )
        log.extend(
            [
                RunStartEvent(
                    **future.event_fields(),
                    **run_start_permission_fields(future.run_id),
                    user_input_chars=6,
                    metadata={"user_input": "future"},
                ),
                ProjectionReadyEvent(
                    **future.event_fields(),
                    projection_id="projection:future",
                    role="pro",
                    scope="session",
                    token_budget=100,
                    summary="FUTURE_PROJECTION_NOT_SEEN",
                ),
                RunEndEvent(
                    **future.event_fields(), status="finished", stop_reason="final"
                ),
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
        log = PostgresEventLog(
            dsn=dsn, runtime_session_id=runtime_session_id, workspace_root=tmp_path
        )
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
                    policy_mode="bypass-permissions",
                    permission_policy=run_start_permission_fields(ctx.run_id)[
                        "permission_policy"
                    ],
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
                "policy_mode": "bypass-permissions",
                "permission_policy": run_start_permission_fields(ctx.run_id)[
                    "permission_policy"
                ],
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


def test_inspector_projects_bounded_mcp_installation_facts(tmp_path: Path) -> None:
    dsn = StorageConfig.from_env().postgres_dsn
    runtime_session_id = _runtime_session_id()
    _connect_or_skip(dsn).close()
    try:
        log = PostgresEventLog(
            dsn=dsn, runtime_session_id=runtime_session_id, workspace_root=tmp_path
        )
        ctx = _ctx("mcp-installation")
        permission = run_start_permission_fields(ctx.run_id)
        permission.update(
            {
                "mcp_installation_id": "mcp_installation:inspect",
                "mcp_installation_owner_runtime_session_id": runtime_session_id,
            }
        )
        log.extend(
            [
                RunStartEvent(
                    **ctx.event_fields(),
                    **permission,
                    user_input_chars=3,
                    metadata={"user_input": "mcp"},
                ),
                _mcp_installed_event(ctx),
                RunEndEvent(
                    **ctx.event_fields(), status="finished", stop_reason="final"
                ),
            ]
        )

        session_report = _service(dsn).inspect_session(runtime_session_id)
        installation = session_report["mcp_installations"][0]
        assert installation["installation_id"] == "mcp_installation:inspect"
        assert installation["server_snapshots"][0]["attempt"] == {
            "server_id": "docs",
            "reconcile_attempt_id": "mcp_attempt:inspect",
            "reconcile_trigger": "initial",
            "attempt_status": "ready",
            "retry_attempt": 1,
            "request_count": 2,
            "page_count": 2,
            "cache_outcome": "miss",
            "stale_candidates_discarded_since_previous_install": 1,
        }
        assert installation["server_snapshots"][0]["catalog_artifact_id"] is None

        run_report = _service(dsn).inspect_run(ctx.run_id)
        run_installation = run_report["canonical"]["mcp_installation"]
        assert run_installation["status"] == "durable"
        assert run_installation["owner_is_current_session"] is True
    finally:
        _cleanup_session(dsn, runtime_session_id)


def test_inspector_joins_child_mcp_installation_through_owner_session(
    tmp_path: Path,
) -> None:
    dsn = StorageConfig.from_env().postgres_dsn
    parent_session_id = _runtime_session_id()
    child_session_id = _runtime_session_id()
    _connect_or_skip(dsn).close()
    try:
        parent_log = PostgresEventLog(
            dsn=dsn,
            runtime_session_id=parent_session_id,
            workspace_root=tmp_path,
        )
        parent_ctx = _ctx("mcp-parent-owner")
        parent_fields = run_start_permission_fields(parent_ctx.run_id)
        parent_fields.update(
            {
                "mcp_installation_id": "mcp_installation:inspect",
                "mcp_installation_owner_runtime_session_id": parent_session_id,
            }
        )
        parent_log.extend(
            [
                RunStartEvent(
                    **parent_ctx.event_fields(),
                    **parent_fields,
                    user_input_chars=6,
                ),
                _mcp_installed_event(parent_ctx),
                RunEndEvent(
                    **parent_ctx.event_fields(),
                    status="finished",
                    stop_reason="final",
                ),
            ]
        )

        child_log = PostgresEventLog(
            dsn=dsn,
            runtime_session_id=child_session_id,
            workspace_root=tmp_path,
        )
        child_ctx = _ctx("mcp-child-owner")
        child_fields = run_start_permission_fields(child_ctx.run_id)
        child_fields.update(
            {
                "mcp_installation_id": "mcp_installation:inspect",
                "mcp_installation_owner_runtime_session_id": parent_session_id,
            }
        )
        child_log.extend(
            [
                RunStartEvent(
                    **child_ctx.event_fields(),
                    **child_fields,
                    user_input_chars=5,
                ),
                RunEndEvent(
                    **child_ctx.event_fields(),
                    status="finished",
                    stop_reason="final",
                ),
            ]
        )

        report = _service(dsn).inspect_run(child_ctx.run_id)
        installation = report["canonical"]["mcp_installation"]
        assert installation["status"] == "durable"
        assert installation["owner_runtime_session_id"] == parent_session_id
        assert installation["owner_is_current_session"] is False
        assert installation["audit"]["installation_id"] == "mcp_installation:inspect"
    finally:
        _cleanup_session(dsn, child_session_id)
        _cleanup_session(dsn, parent_session_id)


def test_inspector_reports_missing_mcp_installation_audit(tmp_path: Path) -> None:
    dsn = StorageConfig.from_env().postgres_dsn
    runtime_session_id = _runtime_session_id()
    _connect_or_skip(dsn).close()
    try:
        log = PostgresEventLog(
            dsn=dsn,
            runtime_session_id=runtime_session_id,
            workspace_root=tmp_path,
        )
        ctx = _ctx("mcp-missing-audit")
        fields = run_start_permission_fields(ctx.run_id)
        fields.update(
            {
                "mcp_installation_id": "mcp_installation:missing",
                "mcp_installation_owner_runtime_session_id": runtime_session_id,
            }
        )
        log.extend(
            [
                RunStartEvent(
                    **ctx.event_fields(),
                    **fields,
                    user_input_chars=1,
                ),
                RunEndEvent(
                    **ctx.event_fields(), status="finished", stop_reason="final"
                ),
            ]
        )

        report = _service(dsn).inspect_run(ctx.run_id)
        assert report["canonical"]["mcp_installation"]["status"] == "missing"
        assert any(
            diagnostic["code"] == "mcp_installation_audit_missing"
            for diagnostic in report["diagnostics"]
        )
    finally:
        _cleanup_session(dsn, runtime_session_id)


def test_inspect_run_reports_gate_permission_snapshot_mismatch(tmp_path: Path) -> None:
    dsn = StorageConfig.from_env().postgres_dsn
    runtime_session_id = _runtime_session_id()
    _connect_or_skip(dsn).close()
    try:
        log = PostgresEventLog(
            dsn=dsn, runtime_session_id=runtime_session_id, workspace_root=tmp_path
        )
        ctx = _ctx("gate-permission-mismatch")
        events = _simple_run_events(ctx, user_input="hello", text="done")
        log.extend(
            [
                events[0],
                CapabilityGateDecisionEvent(
                    **ctx.event_fields(),
                    tool_call_id="call:terminal",
                    tool_name="terminal",
                    descriptor_id="builtin:terminal",
                    decision="deny",
                    reason_code="hardline_terminal_command_blocked",
                    policy_mode="read-only",
                    permission_policy=run_start_permission_fields(
                        ctx.run_id, mode="read-only"
                    )["permission_policy"],
                    availability="available",
                    permission_category="terminal",
                    effective_permission_category="terminal",
                    effective_read_only=False,
                ),
                *events[1:],
            ]
        )

        report = _service(dsn).inspect_run(ctx.run_id)

        diagnostic = next(
            diagnostic
            for diagnostic in report["diagnostics"]
            if diagnostic["code"] == "capability_gate_permission_snapshot_mismatch"
        )
        assert diagnostic["details"]["tool_call_id"] == "call:terminal"
        assert diagnostic["details"]["run_permission_mode"] == "bypass-permissions"
        assert diagnostic["details"]["gate_policy_mode"] == "read-only"
        assert diagnostic["details"]["mode_matches"] is False
        assert diagnostic["details"]["policy_matches"] is False
    finally:
        _cleanup_session(dsn, runtime_session_id)


def test_inspect_projects_subagent_graph(tmp_path: Path) -> None:
    dsn = StorageConfig.from_env().postgres_dsn
    runtime_session_id = _runtime_session_id()
    _connect_or_skip(dsn).close()
    try:
        log = PostgresEventLog(
            dsn=dsn, runtime_session_id=runtime_session_id, workspace_root=tmp_path
        )
        ctx = _ctx("subagent-graph")
        child_runtime_session_id = f"runtime:subagent:{uuid4().hex}"
        subagent_run_id = f"subagent_run:{uuid4().hex}"
        review_task_id = f"subagent_task:{uuid4().hex}"
        prepare_task_id = f"subagent_task:{uuid4().hex}"
        verify_task_id = f"subagent_task:{uuid4().hex}"
        result_id = f"subagent_result:{uuid4().hex}"
        result_artifact_id = f"{subagent_run_id}:result"
        edge_id = f"subagent_edge:{subagent_run_id}:spawn"
        prepare_failed_event = SubagentTaskFailedEvent(
            **ctx.event_fields(),
            task_id=prepare_task_id,
            subagent_run_id=None,
            batch_id="subagent_batch:test",
            create_tool_call_id="tool:create-tasks",
            reason_code="synthetic_prepare_failed",
            reason_message="prepare failed",
        )
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
                    objective_artifact_id=f"{review_task_id}:objective",
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
                    objective_artifact_id=f"{prepare_task_id}:objective",
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
                    objective_artifact_id=f"{verify_task_id}:objective",
                    depends_on=[prepare_task_id],
                ),
                prepare_failed_event,
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
                    spawning_tool_name="spawn_agent",
                    spawn_initiator_kind="tool_call",
                    spawn_initiator_id="tool:spawn",
                    child_runtime_session_id=child_runtime_session_id,
                    label="worker",
                    role="worker",
                    task_preview="child task",
                    task_id=review_task_id,
                    batch_id="subagent_batch:test",
                    create_tool_call_id="tool:create-tasks",
                    run_index=1,
                    context_policy=_subagent_context_snapshot(),
                    capability_profile=_subagent_capability_snapshot(),
                    budget_snapshot=_subagent_budget_snapshot(),
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
                    message_artifact_id=f"{review_task_id}:objective",
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
                    dependency_terminal_event_ids={
                        prepare_task_id: prepare_failed_event.id
                    },
                    dependency_generation=subagent_dependency_generation(
                        {prepare_task_id: prepare_failed_event.id}
                    ),
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

        assert (
            run_report["subagent_graph"]["nodes"]
            == session_report["subagent_graph"]["nodes"]
        )
        [node] = run_report["subagent_graph"]["nodes"]
        assert node["subagent_run_id"] == subagent_run_id
        assert node["status"] == "completed"
        assert node["delivered"] is True
        assert node["consumed_by_wait"] is True
        edge_kinds = {
            edge["edge_kind"] for edge in run_report["subagent_graph"]["edges"]
        }
        assert {"spawn", "wait"}.issubset(edge_kinds)
        assert (
            run_report["subagent_graph"]["tasks"]
            == session_report["subagent_graph"]["tasks"]
        )
        tasks_by_key = {
            task["task_key"]: task for task in run_report["subagent_graph"]["tasks"]
        }
        assert tasks_by_key["review"]["current_run_id"] == subagent_run_id
        assert tasks_by_key["review"]["result_id"] == result_id
        assert tasks_by_key["prepare"]["status"] == "failed"
        assert tasks_by_key["verify"]["current_run_id"] is None
        assert tasks_by_key["verify"]["status"] == "blocked_dependency_failed"
        assert tasks_by_key["verify"]["blocked_by_task_ids"] == [prepare_task_id]
        assert tasks_by_key["verify"]["dependency_terminal_event_ids"] == {
            prepare_task_id: prepare_failed_event.id
        }
    finally:
        _cleanup_session(dsn, runtime_session_id)


def test_inspect_run_reports_missing_artifact_ref(tmp_path: Path) -> None:
    dsn = StorageConfig.from_env().postgres_dsn
    runtime_session_id = _runtime_session_id()
    _connect_or_skip(dsn).close()
    try:
        log = PostgresEventLog(
            dsn=dsn, runtime_session_id=runtime_session_id, workspace_root=tmp_path
        )
        ctx = _ctx("missing-artifact")
        call_id = "call:missing-artifact"
        log.extend(
            [
                RunStartEvent(
                    **ctx.event_fields(),
                    **run_start_permission_fields(ctx.run_id),
                    user_input_chars=5,
                    metadata={"user_input": "tool"},
                ),
                ToolCallStartEvent(
                    **ctx.event_fields(),
                    tool_call_id=call_id,
                    tool_call_name="read_file",
                ),
                ToolCallEndEvent(**ctx.event_fields(), tool_call_id=call_id),
                ToolResultStartEvent(
                    **ctx.event_fields(),
                    tool_call_id=call_id,
                    tool_call_name="read_file",
                ),
                ToolResultTextDeltaEvent(
                    **ctx.event_fields(), tool_call_id=call_id, delta="preview"
                ),
                ToolResultEndEvent(
                    **ctx.event_fields(),
                    tool_call_id=call_id,
                    state=ToolResultState.SUCCESS,
                    metadata={
                        "tool_observation_timing": {
                            "observed_at": "2026-01-01T00:00:00Z"
                        }
                    },
                    artifacts=[
                        ToolResultArtifactRef(
                            artifact_id="artifact:missing",
                            role="output",
                            media_type="text/plain",
                            size_bytes=100,
                        )
                    ],
                ),
                RunEndEvent(
                    **ctx.event_fields(), status="finished", stop_reason="final"
                ),
            ]
        )

        report = _service(dsn).inspect_run(ctx.run_id)

        assert any(
            diagnostic["code"] == "missing_artifact"
            for diagnostic in report["diagnostics"]
        )
    finally:
        _cleanup_session(dsn, runtime_session_id)


def test_inspect_run_outbox_uses_structured_lineage_not_payload_text(
    tmp_path: Path,
) -> None:
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
        log = PostgresEventLog(
            dsn=dsn, runtime_session_id=runtime_session_id, workspace_root=tmp_path
        )
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
                                "documents": [
                                    {
                                        "statement": f"mentions {target.run_id} only as text"
                                    }
                                ],
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
                    (
                        [
                            runtime_outbox_id,
                            governed_outbox_id,
                            false_positive_outbox_id,
                        ],
                    ),
                )
                cursor.execute(
                    "delete from memory_governance_decisions where decision_id = %s",
                    (decision_id,),
                )
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


def test_inspect_health_reports_failed_outbox(tmp_path: Path, monkeypatch) -> None:
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

        service = _service(dsn)
        # This assertion concerns the outbox only. Keep unrelated hard-cut
        # legacy session rows in a developer database out of its evidence set.
        monkeypatch.setattr(
            PostgresInspectorStore, "recent_session_ids", lambda self: []
        )
        report = service.inspect_health()

        assert any(row["status"] == "failed" for row in report["outbox"])
    finally:
        with _connect_or_skip(dsn) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    "delete from memory_write_outbox where outbox_id = %s", (outbox_id,)
                )
