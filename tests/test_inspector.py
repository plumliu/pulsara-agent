from __future__ import annotations

import hashlib
from pathlib import Path
from uuid import uuid4

import psycopg
import pytest
from psycopg.types.json import Jsonb

from tests.conftest import (
    run_end_contract_fields,
    run_start_permission_fields,
    subagent_result_handoff_fields,
    tool_result_end_contract_fields,
)
from tests.support import (
    compaction_completed_contract_fields,
    compaction_started_contract_fields,
    context_compiled_contract_fields,
    model_call_end_fields,
    model_call_start_fields,
    test_resolved_call_fact,
)
from tests.support.model_stream import (
    make_text_block_end_event,
    make_text_block_segment_event,
    make_text_block_start_event,
    make_tool_call_end_event,
    make_tool_call_start_event,
)

from pulsara_agent.primitives.long_horizon import default_child_rollout_policy

from pulsara_agent.event import (
    CapabilityExposureResolvedEvent,
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
    RolloutBudgetAccountOpenedEvent,
    RunEndEvent,
    RunInteractionResumeBoundaryEvent,
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
    ToolResultEndEvent,
    ToolResultStartEvent,
    ToolResultTextDeltaEvent,
)
from pulsara_agent.event_log import PostgresEventLog
from pulsara_agent.event.candidates import PreferenceCandidate, ValidCandidatePayload
from pulsara_agent.inspector import InspectorService, PostgresInspectorStore
from pulsara_agent.inspector.service import (
    _context_compilation_projection,
    _long_horizon_run_projection,
    _memory_governance_projection,
    _model_contract_projection,
    _rollout_status_shadow_projection,
)
from pulsara_agent.memory.artifacts.postgres_archive import PostgresArtifactStore
from pulsara_agent.primitives.model_call import (
    CompactionTargetEstimateFact,
    ModelCallPurpose,
)
from pulsara_agent.primitives.capability import (
    build_capability_resolve_basis,
    build_capability_exposure_semantic,
    build_capability_exposure_snapshot,
    capability_authorization_fingerprint,
    empty_capability_projection,
)
from pulsara_agent.primitives.run_boundary import InteractionResumeBoundaryFact
from pulsara_agent.primitives.run_entry import (
    CapabilityExposureOwnerFact,
    HostRunBoundaryIdentityFact,
)
from pulsara_agent.primitives.mcp import (
    McpInstalledServerSnapshotFact,
    McpReconcileAttemptSummaryFact,
    McpServerLifecycleTimingFact,
)
from pulsara_agent.primitives.context import context_fingerprint
from pulsara_agent.primitives.long_horizon import (
    RolloutBudgetAccountFact,
    calculate_model_call_reservation,
)
from pulsara_agent.memory.candidates.pool import CANDIDATE_POOL_SCHEMA_SQL
from pulsara_agent.memory.candidates.pool import candidate_payload_fingerprint
from pulsara_agent.llm.terminal_projection import stable_event_identity
from pulsara_agent.primitives.frozen import build_frozen_fact
from pulsara_agent.primitives.governance_evidence import (
    CompactionCandidateAttributionFact,
)
from pulsara_agent.message import ToolResultArtifactRef, ToolResultState
from pulsara_agent.primitives.permission import PermissionMode
from pulsara_agent.runtime.permission import preset_to_policy
from pulsara_agent.runtime.tool_action import (
    builtin_tool_action_policy,
    default_tool_action_classifier_registry,
)
from pulsara_agent.tools import ToolCall
from pulsara_agent.runtime.subagent.facts import subagent_dependency_generation
from pulsara_agent.runtime.compaction.candidates import (
    ContextCompactionMemoryCandidatePolicy,
    compaction_extractor_contract,
)
from pulsara_agent.settings import StorageConfig
from pulsara_agent.storage import MEMORY_SUBSTRATE_SCHEMA_SQL


def _stored(event, sequence: int):
    return event.model_copy(update={"sequence": sequence})


def test_memory_governance_projection_reports_durable_owner_state() -> None:
    class Store:
        def governance_batches_for_session(self, session_id):
            assert session_id == "runtime:test"
            return [{"governance_batch_id": "governance:one", "status": "prepared"}]

        def governance_claims_for_session(self, session_id):
            assert session_id == "runtime:test"
            return [
                {"candidate_entry_id": "pool:one", "status": "prepared"},
                {"candidate_entry_id": "pool:two", "status": "terminal"},
            ]

        def governance_evidence_rejections_for_session(self, session_id):
            assert session_id == "runtime:test"
            return [{"candidate_entry_id": "pool:invalid"}]

        def candidate_projection_outbox_for_session(self, session_id):
            assert session_id == "runtime:test"
            return [
                {"candidate_entry_id": "pool:pending", "status": "pending"},
                {"candidate_entry_id": "pool:applied", "status": "applied"},
            ]

    projection = _memory_governance_projection(Store(), session_id="runtime:test")

    assert projection["counts"] == {
        "batches": 1,
        "open_claims": 1,
        "evidence_rejections": 1,
        "pending_candidate_projections": 1,
    }
    assert projection["batches"][0]["status"] == "prepared"
    assert projection["claims"][1]["status"] == "terminal"


def _action_classification(
    *,
    tool_call_id: str,
    tool_name: str,
    descriptor_id: str,
):
    return default_tool_action_classifier_registry().classify(
        call=ToolCall(id=tool_call_id, name=tool_name, arguments={}),
        descriptor_id=descriptor_id,
        descriptor_fingerprint=f"descriptor-fingerprint:{descriptor_id}",
        policy=builtin_tool_action_policy(tool_name),
    )


def _compiled_call_events(*, reported_usage: bool = True):
    ctx = _ctx("model-contract")
    call = test_resolved_call_fact()
    permission = run_start_permission_fields(
        ctx.run_id, user_input="x", model_target=call.target
    )
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
                        context_id="context:model-contract",
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
            **model_call_start_fields(
                resolved_call=call,
                context_id="context:direct",
                model_call_index=None,
                lifecycle_kind="direct_internal_call",
            ),
        ),
        1,
    )
    projection = _context_compilation_projection([start])
    assert (
        projection["model_call_joins"][0]["join_status"]
        == "direct_context_not_applicable"
    )
    assert projection["diagnostics"] == []


def test_inspector_can_show_counts_when_model_hint_is_not_injected() -> None:
    ctx = _ctx("rollout-status-shadow")
    permission = run_start_permission_fields(ctx.run_id, user_input="status")
    start = _stored(
        RunStartEvent(
            **ctx.event_fields(),
            **permission,
            user_input_chars=6,
        ),
        1,
    )
    contract = start.long_horizon
    target = start.model_target
    policy = contract.rollout_policy
    primary = calculate_model_call_reservation(
        target=target,
        resolved_model_call_id=None,
        policy=policy,
    )
    final_agent = primary.reserved_milliunits * policy.finalization_reserved_model_calls
    final_compaction = (
        primary.reserved_milliunits
        * policy.finalization_reserved_window_compactions
    )
    final_tool = (
        policy.finalization_reserved_tool_cost_units
        * policy.tool_cost_unit_weight_milli
    )
    reserve = final_agent + final_compaction + final_tool
    total = (
        target.context_budget.input_budget_tokens
        * policy.total_input_budget_multiplier_milli
    )
    account_payload = {
        "account_id": contract.rollout_account_id,
        "owner_runtime_session_id": contract.rollout_account_owner_runtime_session_id,
        "root_run_id": ctx.run_id,
        "policy": policy,
        "total_budget_milliunits": total,
        "finalization_reserve_milliunits": reserve,
        "finalization_agent_reserve_milliunits": final_agent,
        "finalization_compaction_reserve_milliunits": final_compaction,
        "finalization_tool_reserve_milliunits": final_tool,
        "exploration_allowance_milliunits": total - reserve,
    }
    opened = _stored(
        RolloutBudgetAccountOpenedEvent(
            **ctx.event_fields(),
            account=RolloutBudgetAccountFact(
                **account_payload,
                semantic_fingerprint=context_fingerprint(
                    "rollout-budget-account:v1", account_payload
                ),
            ),
        ),
        2,
    )

    projection = _rollout_status_shadow_projection(
        (start, opened),
        runtime_session_id=contract.rollout_account_owner_runtime_session_id,
    )

    assert projection["diagnostics"] == []
    assert len(projection["shadows"]) == 1
    shadow = projection["shadows"][0]
    assert shadow["settled_model_call_count"] == 0
    assert shadow["settled_tool_call_count"] == 0
    assert shadow["recurrence"] == []
    assert shadow["model_visible"] is False

    long_horizon = _long_horizon_run_projection(
        (start, opened),
        runtime_session_id=contract.rollout_account_owner_runtime_session_id,
    )
    assert long_horizon["diagnostics"] == []
    assert len(long_horizon["runs"]) == 1
    run = long_horizon["runs"][0]
    assert run["run_id"] == ctx.run_id
    assert run["rollout_phase"] == "exploration"
    assert run["rollout_charged_milliunits"] == 0
    assert run["rollout_total_milliunits"] == total
    assert run["latest_rollout_status_hint"] is None
    assert run["rollout_status_shadow"]["model_visible"] is False
    assert run["pending_owner_counts"]["total"] == 0


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
        "max_result_artifact_refs_per_child": 32,
        "max_subagent_results_per_parent_compile": 8,
        "child_rollout_policy": default_child_rollout_policy().model_dump(
            mode="json"
        ),
    }


def _simple_run_events(
    ctx: EventContext,
    *,
    user_input: str,
    text: str,
    include_exposure: bool = True,
):
    run_start = RunStartEvent(
        **ctx.event_fields(),
        **run_start_permission_fields(ctx.run_id, user_input=user_input),
        user_input_chars=len(user_input),
        metadata={"user_input": user_input},
    )
    assert run_start.new_run_boundary is not None
    basis = run_start.new_run_boundary.capability_basis
    projection = empty_capability_projection()
    semantic = build_capability_exposure_semantic(
        execution_surface=basis.execution_surface_identity,
        catalog_projection=projection,
        active_skill_projection=projection,
        authorization_fingerprint=capability_authorization_fingerprint(()),
    )
    exposure = build_capability_exposure_snapshot(
        exposure_id=f"exposure:{ctx.run_id}",
        owner=basis.owner,
        resolution_kind="initial",
        resolve_basis=basis,
        semantic=semantic,
        authorization_entries=(),
        source_exposure_id=None,
    )
    return [
        run_start,
        *(
            [
                CapabilityExposureResolvedEvent(
                    **ctx.event_fields(),
                    exposure=exposure,
                    exposure_revision=1,
                )
            ]
            if include_exposure
            else []
        ),
        ReplyStartEvent(**ctx.event_fields(), name="assistant"),
        make_text_block_start_event(**ctx.event_fields(), block_id=f"text:{ctx.run_id}"),
        make_text_block_segment_event(
            **ctx.event_fields(), block_id=f"text:{ctx.run_id}", delta=text
        ),
        make_text_block_end_event(**ctx.event_fields(), block_id=f"text:{ctx.run_id}"),
        ReplyEndEvent(**ctx.event_fields(), model_terminal_outcome="completed"),
        RunEndEvent(
            **run_end_contract_fields(ctx.run_id, status="finished"),
            **ctx.event_fields(),
            status="finished",
            stop_reason="final",
        ),
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
        assert report["canonical"]["current_user_input"]["chars"] == 5
        assert report["canonical"]["current_user_input"]["text_redacted"] is True
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
        assert report["run_boundary"]["run_entry_kind"] == "host"
        assert report["run_boundary"]["status"] == "committed"
        assert report["run_boundary"]["current_user_chars"] == 5
        assert report["run_boundary"]["current_user_content_sha256"]
        assert (
            "PULSARA_INSPECTOR_TEXT"
            in report["assistant_replies"][0]["content"][0]["text"]
        )
        assert report["diagnostics"] == []
    finally:
        _cleanup_session(dsn, runtime_session_id)


def test_inspector_projects_all_committed_resume_boundaries(tmp_path: Path) -> None:
    dsn = StorageConfig.from_env().postgres_dsn
    runtime_session_id = _runtime_session_id()
    _connect_or_skip(dsn).close()
    try:
        log = PostgresEventLog(
            dsn=dsn,
            runtime_session_id=runtime_session_id,
            workspace_root=tmp_path,
        )
        ctx = _ctx("resume-boundary")
        events = _simple_run_events(ctx, user_input="hello", text="done")
        run_start = events[0]
        source_event = events[1]
        assert isinstance(run_start, RunStartEvent)
        assert isinstance(source_event, CapabilityExposureResolvedEvent)
        assert run_start.sequence is None
        owner = CapabilityExposureOwnerFact(
            owner_kind="host_boundary",
            owner_id="boundary:resume:inspect",
            host_boundary_kind="pre_interaction_resume",
            runtime_session_id=runtime_session_id,
            run_id=ctx.run_id,
        )
        source_basis = source_event.exposure.resolve_basis
        basis = build_capability_resolve_basis(
            basis_id="basis:resume:inspect",
            basis_kind="continuation",
            source_basis_id=source_basis.basis_id,
            source_basis_fingerprint=source_basis.basis_fingerprint,
            owner=owner,
            workspace_identity_fingerprint=(
                source_basis.workspace_identity_fingerprint
            ),
            memory_domain_id=source_basis.memory_domain_id,
            permission_snapshot_id=source_basis.permission_snapshot_id,
            plan_active=source_basis.plan_active,
            active_skill_names=source_basis.active_skill_names,
            user_intent_fingerprint=source_basis.user_intent_fingerprint,
            prior_transcript_fingerprint=(source_basis.prior_transcript_fingerprint),
            mcp_installation_id=source_basis.mcp_installation_id,
            execution_surface_identity=source_basis.execution_surface_identity,
        )
        effective = build_capability_exposure_snapshot(
            exposure_id="exposure:resume:inspect",
            owner=owner,
            resolution_kind="continuation_reused",
            resolve_basis=basis,
            semantic=source_event.exposure.semantic,
            authorization_entries=source_event.exposure.authorization_entries,
            source_exposure_id=source_event.exposure.exposure_id,
        )
        identity = HostRunBoundaryIdentityFact(
            boundary_id=owner.owner_id,
            kind="pre_interaction_resume",
            runtime_session_id=runtime_session_id,
            run_id=ctx.run_id,
            turn_id=ctx.turn_id,
            reply_id=ctx.reply_id,
            attempt_number=1,
            observed_at_utc="2026-07-12T01:02:03Z",
        )
        exposure_event = CapabilityExposureResolvedEvent(
            **ctx.event_fields(),
            exposure=effective,
            exposure_revision=2,
        )
        resume_event = RunInteractionResumeBoundaryEvent(
            **ctx.event_fields(),
            boundary=InteractionResumeBoundaryFact(
                identity=identity,
                original_run_start_event_id=run_start.id,
                original_run_start_sequence=1,
                interaction_id="approval:inspect",
                interaction_kind="approval",
                suspended_state_token_fingerprint="token-fp",
                permission_snapshot_id=run_start.permission_snapshot_id,
                model_target_fingerprint=run_start.model_target.target_fingerprint,
                mcp_installation_id=run_start.mcp_installation_id,
                source_exposure_id=source_event.exposure.exposure_id,
                source_exposure_semantic_fingerprint=(
                    source_event.exposure.exposure_semantic_fingerprint
                ),
                source_exposure_fact_fingerprint=(
                    source_event.exposure.exposure_fact_fingerprint
                ),
                effective_exposure_id=effective.exposure_id,
                effective_exposure_semantic_fingerprint=(
                    effective.exposure_semantic_fingerprint
                ),
                effective_exposure_fact_fingerprint=(
                    effective.exposure_fact_fingerprint
                ),
                exposure_transition="reused",
                committed_mcp_audit_event_ids=(),
            ),
        )
        log.extend([*events[:2], exposure_event, resume_event, *events[2:]])

        report = _service(dsn).inspect_run(ctx.run_id)
        assert len(report["continuation_boundaries"]) == 1
        projected = report["continuation_boundaries"][0]
        assert projected["boundary_id"] == identity.boundary_id
        assert projected["exposure_transition"] == "reused"
        assert projected["status"] == "committed"
    finally:
        _cleanup_session(dsn, runtime_session_id)


def test_inspector_projects_primitive_child_entry_with_nullable_task(
    tmp_path: Path,
) -> None:
    dsn = StorageConfig.from_env().postgres_dsn
    child_runtime_session_id = _runtime_session_id()
    parent_runtime_session_id = _runtime_session_id()
    _connect_or_skip(dsn).close()
    try:
        log = PostgresEventLog(
            dsn=dsn,
            runtime_session_id=child_runtime_session_id,
            workspace_root=tmp_path,
        )
        ctx = _ctx("primitive-child-boundary")
        fields = run_start_permission_fields(
            ctx.run_id,
            source="child_profile",
            user_input="primitive objective",
            turn_id=ctx.turn_id,
            reply_id=ctx.reply_id,
            mcp_installation_owner_runtime_session_id=(parent_runtime_session_id),
        )
        entry = fields["subagent_run_entry"]
        current_user = fields["current_user_message"]
        assert entry is not None
        fields["subagent_run_entry"] = entry.model_copy(
            update={"subagent_task_id": None}
        )
        fields["current_user_message"] = current_user.model_copy(
            update={"source_kind": "subagent_primitive_objective"}
        )
        run_start = RunStartEvent(
            **ctx.event_fields(),
            **fields,
            user_input_chars=len("primitive objective"),
        )
        log.extend(
            [
                run_start,
                RunEndEvent(
                    **ctx.event_fields(),
                    **run_end_contract_fields(ctx.run_id, status="finished"),
                    status="finished",
                    stop_reason="final",
                ),
            ]
        )

        report = _service(dsn).inspect_run(ctx.run_id)
        assert report["run_boundary"]["run_entry_kind"] == "subagent_child"
        child = report["child_run_entry"]
        assert child["subagent_task_id"] is None
        assert child["entry_mode"] == "primitive_run"
        assert child["child_result_render_policy"]["renderer_version"] == "test:v1"
        assert child["child_terminal_reference"] is None
    finally:
        _cleanup_session(dsn, child_runtime_session_id)


def test_inspector_joins_preflight_compaction_to_host_boundary(tmp_path: Path) -> None:
    dsn = StorageConfig.from_env().postgres_dsn
    runtime_session_id = _runtime_session_id()
    _connect_or_skip(dsn).close()
    try:
        log = PostgresEventLog(
            dsn=dsn,
            runtime_session_id=runtime_session_id,
            workspace_root=tmp_path,
        )
        ctx = _ctx("boundary-compaction")
        run_events = _simple_run_events(ctx, user_input="next", text="done")
        run_start = run_events[0]
        assert isinstance(run_start, RunStartEvent)
        boundary = run_start.new_run_boundary
        assert boundary is not None
        compaction_id = f"context_compaction:{uuid4().hex}"
        started_fields = compaction_started_contract_fields()
        started = log.append(
            ContextCompactionStartedEvent(
                id="context_compaction_started:test-boundary",
                **ctx.event_fields(),
                **started_fields,
                compaction_id=compaction_id,
                trigger="auto",
                reason="context_threshold",
                window_number=1,
                window_id="context_window:boundary",
                threshold_tokens=200_000,
                through_sequence=0,
                keep_after_sequence=0,
                host_boundary_id=boundary.identity.boundary_id,
                host_boundary_kind="pre_run",
            )
        )
        completed_fields = compaction_completed_contract_fields(
            estimated_tokens_before=200_001,
            estimated_tokens_after=1_000,
        )
        completed_fields["started_event_id"] = started.id
        completed = log.append(
            ContextCompactionCompletedEvent(
                id=started.terminal_event_id,
                **ctx.event_fields(),
                **completed_fields,
                compaction_id=compaction_id,
                trigger="auto",
                reason="context_threshold",
                window_number=1,
                window_id="context_window:boundary",
                summary_artifact_id=f"artifact:missing:{uuid4().hex}",
                summary_chars=10,
                threshold_tokens=200_000,
                through_sequence=0,
                keep_after_sequence=0,
                included_run_ids=[],
                host_boundary_id=boundary.identity.boundary_id,
                host_boundary_kind="pre_run",
            )
        )
        assert completed.sequence is not None
        transcript = boundary.transcript.model_copy(
            update={
                "preflight_compaction_id": compaction_id,
                "preflight_compaction_terminal_event_id": completed.id,
                "preflight_compaction_terminal_sequence": completed.sequence,
            }
        )
        run_events[0] = run_start.model_copy(
            update={
                "new_run_boundary": boundary.model_copy(
                    update={"transcript": transcript}
                )
            }
        )
        log.extend(run_events)

        report = _service(dsn).inspect_run(ctx.run_id)
        joined = report["run_boundary"]["preflight_compaction"]
        assert joined["compaction_id"] == compaction_id
        assert joined["terminal_event_id"] == completed.id
        assert [item["event_id"] for item in joined["events"]] == [
            started.id,
            completed.id,
        ]
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
            context_id=context_id,
        )
        log.extend(
            [
                RunStartEvent(
                    **ctx.event_fields(),
                    **run_start_permission_fields(ctx.run_id, user_input="hello"),
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
                make_text_block_start_event(
                    **ctx.event_fields(), block_id=f"text:{ctx.run_id}"
                ),
                make_text_block_segment_event(
                    **ctx.event_fields(), block_id=f"text:{ctx.run_id}", delta="done"
                ),
                make_text_block_end_event(**ctx.event_fields(), block_id=f"text:{ctx.run_id}"),
                ReplyEndEvent(**ctx.event_fields(), model_terminal_outcome="completed"),
                RunEndEvent(
                    **run_end_contract_fields(ctx.run_id, status="finished"),
                    **ctx.event_fields(),
                    status="finished",
                    stop_reason="final",
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
        assert contexts["latest"]["input_status"] == "audited"
        assert contexts["latest"]["input_audit"] == compiled_fields[
            "input_audit"
        ].model_dump(mode="json")
        assert contexts["latest"]["input_failure"] is None
        assert contexts["latest"]["input_replay"]["status"] == "artifact_missing"
        assert contexts["latest"]["input_replay"]["diagnostics"][0]["code"] == (
            "context_input_manifest_missing"
        )
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
        candidate_payload = ValidCandidatePayload(
            candidate=PreferenceCandidate(
                candidate_id="candidate:compaction-inspector",
                statement=(
                    "The user prefers syncing release before pushing GitHub."
                ),
                scope="ctx:workspace/test",
                source_authority="conversation_evidence",
                verification_status="inferred",
                evidence_ids=[],
            )
        )
        extractor_contract = compaction_extractor_contract(
            ContextCompactionMemoryCandidatePolicy()
        )
        summary_text = "Compaction summary mentions release sync workflow."
        candidate_attribution = build_frozen_fact(
            CompactionCandidateAttributionFact,
            schema_version="compaction_candidate_attribution.v1",
            candidate_entry_id=candidate_entry_id,
            raw_candidate_index=0,
            candidate_payload=candidate_payload,
            candidate_payload_fingerprint=candidate_payload_fingerprint(
                candidate_payload
            ),
            intent_fingerprint="sha256:inspector",
        )
        proposed = log.append(
            ContextCompactionMemoryCandidatesProposedEvent(
                **ctx.event_fields(),
                compaction_id=completed.compaction_id,
                source_event_id=completed.id,
                source_event_sequence=completed.sequence or 0,
                summary_artifact_id=summary_artifact_id,
                candidate_entry_ids=[candidate_entry_id],
                attempted_count=1,
                proposed_count=1,
                extractor_version=extractor_contract.extractor_version,
                summary_content_sha256=hashlib.sha256(
                    summary_text.encode("utf-8")
                ).hexdigest(),
                summary_content_bytes=len(summary_text.encode("utf-8")),
                extractor_contract=extractor_contract,
                ordered_candidate_attributions=(candidate_attribution,),
                completed_compaction_event_identity=stable_event_identity(
                    completed,
                    runtime_session_id=runtime_session_id,
                ),
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
                                **candidate_payload.model_dump(mode="json"),
                            }
                        ),
                        runtime_session_id,
                        ctx.run_id,
                        ctx.turn_id,
                        ctx.reply_id,
                        proposed.id,
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
                        batch_input_fingerprint,
                        batch_input_reference_fingerprint,
                        governance_model_call_id,
                        decision_index,
                        requested_decision_payload_fingerprint,
                        decision_payload_fingerprint,
                        decision,
                        write_outcome
                    )
                    values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    """,
                    (
                        decision_id,
                        governance_batch_id,
                        f"sha256:batch:{governance_batch_id}",
                        f"sha256:reference:{governance_batch_id}",
                        f"model_call:{governance_batch_id}",
                        0,
                        f"sha256:requested:{decision_id}",
                        f"sha256:effective:{decision_id}",
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
        assert window["memory_candidates"][0]["source_event_id"] == proposed.id
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
                    **run_start_permission_fields(target.run_id, user_input="target"),
                    user_input_chars=6,
                    metadata={"user_input": "target"},
                ),
                ProjectionReadyEvent(
                    **target.event_fields(),
                    projection_id="projection:target",
                    role="pro",
                    scope="session",
                    token_budget=100,
                    projection_kind="memory",
                    summary="TARGET_PROJECTION_AS_SEEN",
                ),
                ReplyStartEvent(**target.event_fields(), name="assistant"),
                make_text_block_start_event(**target.event_fields(), block_id="text:target"),
                make_text_block_segment_event(
                    **target.event_fields(), block_id="text:target", delta="target done"
                ),
                make_text_block_end_event(**target.event_fields(), block_id="text:target"),
                ReplyEndEvent(**target.event_fields(), model_terminal_outcome="completed"),
                RunEndEvent(
                    **run_end_contract_fields(target.run_id, status="finished"),
                    **target.event_fields(),
                    status="finished",
                    stop_reason="final",
                ),
            ]
        )
        log.extend(
            [
                RunStartEvent(
                    **future.event_fields(),
                    **run_start_permission_fields(future.run_id, user_input="future"),
                    user_input_chars=6,
                    metadata={"user_input": "future"},
                ),
                ProjectionReadyEvent(
                    **future.event_fields(),
                    projection_id="projection:future",
                    role="pro",
                    scope="session",
                    token_budget=100,
                    projection_kind="memory",
                    summary="FUTURE_PROJECTION_NOT_SEEN",
                ),
                RunEndEvent(
                    **run_end_contract_fields(future.run_id, status="finished"),
                    **future.event_fields(),
                    status="finished",
                    stop_reason="final",
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
        events = _simple_run_events(
            ctx,
            user_input="hello",
            text="done",
            include_exposure=False,
        )
        run_start = events[0]
        assert isinstance(run_start, RunStartEvent)
        assert run_start.new_run_boundary is not None
        basis = run_start.new_run_boundary.capability_basis
        projection = empty_capability_projection()
        semantic = build_capability_exposure_semantic(
            execution_surface=basis.execution_surface_identity,
            catalog_projection=projection,
            active_skill_projection=projection,
            authorization_fingerprint=capability_authorization_fingerprint(()),
        )
        exposure = build_capability_exposure_snapshot(
            exposure_id="exposure:inspect",
            owner=basis.owner,
            resolution_kind="initial",
            resolve_basis=basis,
            semantic=semantic,
            authorization_entries=(),
            source_exposure_id=None,
        )
        log.extend(
            [
                events[0],
                CapabilityExposureResolvedEvent(
                    **ctx.event_fields(),
                    exposure=exposure,
                    exposure_revision=1,
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
                    action_classification=_action_classification(
                        tool_call_id="call:read",
                        tool_name="read_file",
                        descriptor_id="builtin:read_file",
                    ),
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
        assert capability["latest_exposure"]["direct_names"] == []
        assert capability["latest_exposure"]["callable_names"] == []
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
        permission = run_start_permission_fields(
            ctx.run_id,
            user_input="mcp",
            mcp_installation_id="mcp_installation:inspect",
            mcp_installation_owner_runtime_session_id=runtime_session_id,
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
                    **run_end_contract_fields(ctx.run_id, status="finished"),
                    **ctx.event_fields(),
                    status="finished",
                    stop_reason="final",
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
        parent_fields = run_start_permission_fields(
            parent_ctx.run_id,
            user_input="x" * 6,
            mcp_installation_id="mcp_installation:inspect",
            mcp_installation_owner_runtime_session_id=parent_session_id,
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
                    **run_end_contract_fields(parent_ctx.run_id, status="finished"),
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
        child_fields = run_start_permission_fields(
            child_ctx.run_id,
            source="child_profile",
            user_input="x" * 5,
            mcp_installation_id="mcp_installation:inspect",
            mcp_installation_owner_runtime_session_id=parent_session_id,
        )
        child_log.extend(
            [
                RunStartEvent(
                    **child_ctx.event_fields(),
                    **child_fields,
                    user_input_chars=5,
                ),
                RunEndEvent(
                    **run_end_contract_fields(child_ctx.run_id, status="finished"),
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
        fields = run_start_permission_fields(
            ctx.run_id,
            user_input="x",
            mcp_installation_id="mcp_installation:missing",
            mcp_installation_owner_runtime_session_id=runtime_session_id,
        )
        log.extend(
            [
                RunStartEvent(
                    **ctx.event_fields(),
                    **fields,
                    user_input_chars=1,
                ),
                RunEndEvent(
                    **run_end_contract_fields(ctx.run_id, status="finished"),
                    **ctx.event_fields(),
                    status="finished",
                    stop_reason="final",
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
                        action_classification=_action_classification(
                            tool_call_id="call:terminal",
                            tool_name="terminal",
                            descriptor_id="builtin:terminal",
                        ),
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
                    **subagent_result_handoff_fields(
                        subagent_run_id=subagent_run_id,
                        child_runtime_session_id=child_runtime_session_id,
                        child_run_id=f"child-run:{subagent_run_id}",
                        result_id=result_id,
                        summary="child summary",
                        result_artifact_id=result_artifact_id,
                        artifact_ids=(result_artifact_id,),
                    ),
                    **ctx.event_fields(),
                    subagent_run_id=subagent_run_id,
                    parent_runtime_session_id=runtime_session_id,
                    child_runtime_session_id=child_runtime_session_id,
                    child_run_id=f"child-run:{subagent_run_id}",
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
                    **run_start_permission_fields(ctx.run_id, user_input="tool"),
                    user_input_chars=len("tool"),
                    metadata={"user_input": "tool"},
                ),
                make_tool_call_start_event(
                    **ctx.event_fields(),
                    tool_call_id=call_id,
                    tool_call_name="read_file",
                ),
                make_tool_call_end_event(**ctx.event_fields(), tool_call_id=call_id),
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
                    **tool_result_end_contract_fields(call_id, tool_name="read_file"),
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
                    **run_end_contract_fields(ctx.run_id, status="finished"),
                    **ctx.event_fields(),
                    status="finished",
                    stop_reason="final",
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
                        batch_input_fingerprint,
                        batch_input_reference_fingerprint,
                        governance_model_call_id,
                        decision_index,
                        requested_decision_payload_fingerprint,
                        decision_payload_fingerprint,
                        decision,
                        write_outcome
                    )
                    values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    """,
                    (
                        decision_id,
                        governance_batch_id,
                        f"sha256:batch:{governance_batch_id}",
                        f"sha256:reference:{governance_batch_id}",
                        f"model_call:{governance_batch_id}",
                        0,
                        f"sha256:requested:{decision_id}",
                        f"sha256:effective:{decision_id}",
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
