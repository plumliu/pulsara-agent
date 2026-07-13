"""Central model-target fixtures for hard-cut runtime tests."""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import AsyncIterator
from uuid import uuid4

from pulsara_agent.event import AgentEvent, EventContext
from pulsara_agent.llm.config import LLMConfig, ModelSlotConfig
from pulsara_agent.llm.adapters.mock import MockTransport
from pulsara_agent.llm.models import ModelRole
from pulsara_agent.llm.provider import ProviderProfile
from pulsara_agent.llm.registry import LLMTransportRegistry
from pulsara_agent.llm.request import LLMContext, LLMOptions
from pulsara_agent.llm.retry import LLMRetryConfig
from pulsara_agent.llm.resolution import resolve_model_call, resolve_model_target
from pulsara_agent.primitives.model_call import (
    ContextBudgetReportEvent,
    CompactionTargetEstimateFact,
    ModelCallPurpose,
    ModelContextLimits,
    ResolvedModelCallFact,
    ResolvedModelTargetFact,
    ModelTokenUsageFact,
)
from pulsara_agent.primitives.context import ContextCompileInputAuditFact


def compaction_completed_contract_fields(
    *,
    estimated_tokens_before: int = 10_000,
    estimated_tokens_after: int = 100,
) -> dict[str, object]:
    target = test_resolved_target_fact()
    summarizer = test_resolved_call_fact(
        purpose=ModelCallPurpose.CONTEXT_COMPACTION_SUMMARY
    )
    summary_actual = min(estimated_tokens_after, 50)
    target_estimate = CompactionTargetEstimateFact(
        estimate_scope="transcript_only",
        basis_context_id=None,
        target_fingerprint=target.target_fingerprint,
        non_transcript_baseline_tokens=None,
        transcript_tokens_before=estimated_tokens_before,
        estimated_tokens_before=estimated_tokens_before,
        summary_tokens_reserved=max(summary_actual, 256),
        retained_transcript_tokens=estimated_tokens_after - summary_actual,
        protected_transcript_tokens=0,
        summary_tokens_actual=summary_actual,
        transcript_tokens_after=estimated_tokens_after,
        estimated_tokens_after=estimated_tokens_after,
        predicted_post_target_reached=None,
    )
    return {
        "target_model_target": target,
        "target_input_budget_tokens": target.context_budget.input_budget_tokens,
        "post_compaction_target_tokens": max(
            1, target.context_budget.input_budget_tokens // 2
        ),
        "target_estimate": target_estimate,
        "summarizer_call": summarizer,
        "summarizer_context_id": "context:test-compaction",
        "summarizer_input_estimated_tokens": 64,
        "summarizer_input_budget_tokens": summarizer.target.context_budget.input_budget_tokens,
        "summarizer_usage_status": "missing",
        "summarizer_usage": None,
        "summarizer_estimated_input_tokens": 64,
        "summarizer_reported_model_id": None,
        "predicted_post_target_reached": None,
        "started_event_id": "context_compaction_started:test",
    }


def compaction_started_contract_fields(
    *,
    estimated_tokens_before: int = 10_000,
) -> dict[str, object]:
    target = test_resolved_target_fact()
    summarizer = test_resolved_call_fact(
        purpose=ModelCallPurpose.CONTEXT_COMPACTION_SUMMARY
    )
    target_estimate = CompactionTargetEstimateFact(
        estimate_scope="transcript_only",
        basis_context_id=None,
        target_fingerprint=target.target_fingerprint,
        non_transcript_baseline_tokens=None,
        transcript_tokens_before=estimated_tokens_before,
        estimated_tokens_before=estimated_tokens_before,
        summary_tokens_reserved=256,
        retained_transcript_tokens=0,
        protected_transcript_tokens=0,
        summary_tokens_actual=None,
        transcript_tokens_after=None,
        estimated_tokens_after=None,
        predicted_post_target_reached=None,
    )
    return {
        "target_model_target": target,
        "target_input_budget_tokens": target.context_budget.input_budget_tokens,
        "post_compaction_target_tokens": max(
            1, target.context_budget.input_budget_tokens // 2
        ),
        "target_estimate": target_estimate,
        "summarizer_call": summarizer,
        "summarizer_context_id": "context:test-compaction",
        "summarizer_input_estimated_tokens": 64,
        "summarizer_input_budget_tokens": summarizer.target.context_budget.input_budget_tokens,
        "terminal_event_id": "context_compaction_terminal:test",
    }


def compaction_failed_contract_fields() -> dict[str, object]:
    target = test_resolved_target_fact()
    return {
        "target_model_target": target,
        "target_input_budget_tokens": target.context_budget.input_budget_tokens,
        "post_compaction_target_tokens": max(
            1, target.context_budget.input_budget_tokens // 2
        ),
        "failure_stage": "planning",
        "termination_kind": "failed",
    }


def context_compiled_contract_fields(
    *,
    estimated_tokens: int = 123,
    tools_estimated_tokens: int = 42,
    status: str = "compiled",
    non_transcript_baseline_tokens: int | None = None,
    resolved_call: ResolvedModelCallFact | None = None,
    model_call_index: int = 1,
) -> dict[str, object]:
    call = resolved_call or test_resolved_call_fact()
    target = call.target
    baseline = (
        estimated_tokens - max(0, estimated_tokens // 3)
        if non_transcript_baseline_tokens is None
        else non_transcript_baseline_tokens
    )
    transcript = estimated_tokens - baseline
    if transcript < 0:
        raise ValueError("non-transcript baseline exceeds estimated token total")
    sections = max(0, estimated_tokens - tools_estimated_tokens)
    budget = ContextBudgetReportEvent(
        target_fingerprint=target.target_fingerprint,
        resolved_model_call_id=call.resolved_model_call_id,
        measurement_stage="final_payload",
        total_context_tokens=target.limits.total_context_tokens,
        max_input_tokens=target.limits.max_input_tokens,
        max_output_tokens=target.limits.max_output_tokens,
        effective_output_tokens=target.context_budget.effective_output_tokens,
        safety_margin_tokens=target.context_budget.safety_margin_tokens,
        input_budget_tokens=target.context_budget.input_budget_tokens,
        sections_estimated_tokens=sections,
        tools_estimated_tokens=tools_estimated_tokens,
        envelope_estimated_tokens=3,
        allocation_estimated_tokens=sections + tools_estimated_tokens,
        final_payload_estimated_tokens=estimated_tokens,
        non_transcript_baseline_tokens=baseline,
        transcript_estimated_tokens=transcript,
        estimator=target.token_estimator,
    )
    return {
        "status": status,
        "failure_stage": "context_compile" if status == "failed" else None,
        "compile_attempt_index": 1,
        "context_retry_index": 0,
        "resolved_call": call,
        "budget": budget,
        "input_audit": ContextCompileInputAuditFact(
            snapshot_id="context_snapshot:test",
            snapshot_semantic_fingerprint="sha256:" + "1" * 64,
            snapshot_fact_fingerprint="sha256:" + "2" * 64,
            snapshot_schema_version="context-snapshot:v1",
            compiler_contract_version="context-compiler-input:v1",
            source_runtime_session_id="runtime:test",
            authority_from_sequence=1,
            source_through_sequence=1,
            authority_slice_plan_fingerprint="sha256:" + "3" * 64,
            transcript_projection_window_fingerprint="sha256:" + "4" * 64,
            run_start_event_id="run-start:test",
            run_start_sequence=1,
            continuation_event_id=None,
            continuation_sequence=None,
            continuation_count=0,
            resolved_model_call_id=call.resolved_model_call_id,
            model_call_index=model_call_index,
            compile_attempt_index=1,
            context_retry_index=0,
            transcript_fingerprint="sha256:" + "5" * 64,
            transcript_message_count=1,
            transcript_pair_count=0,
            tool_result_units_fingerprint="sha256:" + "6" * 64,
            tool_result_unit_count=0,
            tool_result_render_policy_fingerprint="sha256:" + "7" * 64,
            tool_result_render_input_fingerprint="sha256:" + "8" * 64,
            prepared_candidate_set_fingerprint="sha256:" + "9" * 64,
            section_candidate_count=1,
            input_aggregate_fingerprint="sha256:" + "a" * 64,
            input_manifest_artifact_id="context-input-manifest:test",
            input_manifest_fingerprint="sha256:" + "b" * 64,
            input_manifest_write_outcome="stored",
        ),
        "provider_neutral_payload_fingerprint": (
            "sha256:" + "c" * 64 if status == "compiled" else None
        ),
        "canonical_render_decisions_fingerprint": (
            "sha256:" + "d" * 64 if status == "compiled" else None
        ),
    }


def model_call_start_fields(
    *,
    context_id: str = "context:test",
    model_call_index: int = 1,
    resolved_call: ResolvedModelCallFact | None = None,
) -> dict[str, object]:
    return {
        "resolved_call": resolved_call or test_resolved_call_fact(),
        "context_id": context_id,
        "model_call_index": model_call_index,
    }


def model_call_end_fields(
    *,
    input_tokens: int = 0,
    output_tokens: int = 0,
    estimated_input_tokens: int | None = None,
    resolved_call: ResolvedModelCallFact | None = None,
) -> dict[str, object]:
    call = resolved_call or test_resolved_call_fact()
    usage = ModelTokenUsageFact(
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        total_tokens=input_tokens + output_tokens,
    )
    return {
        "resolved_model_call_id": call.resolved_model_call_id,
        "target_fingerprint": call.target.target_fingerprint,
        "reported_model_id": call.target.model_id,
        "outcome": "completed",
        "usage_status": "reported",
        "usage": usage,
        "estimated_input_tokens": (
            input_tokens if estimated_input_tokens is None else estimated_input_tokens
        ),
    }


async def run_agent_task(agent, user_input: str, **kwargs):
    """Commit a test-owned Host entry, then invoke the production committed API."""

    _prepare_test_host_run_entry(agent, user_input, kwargs)
    draft, committed, _stored = await _commit_test_host_run_entry(
        agent, user_input, kwargs
    )
    return await agent.run_committed_entry(draft, committed)


def stream_agent_task(agent, user_input: str, **kwargs):
    """Return a test-owned entry stream feeding the committed production API."""

    _prepare_test_host_run_entry(agent, user_input, kwargs)

    async def _stream():
        draft, committed, stored = await _commit_test_host_run_entry(
            agent, user_input, kwargs
        )
        for event in stored:
            yield event
        async for event in agent.stream_committed_entry(draft, committed):
            yield event

    return _stream()


async def _commit_test_host_run_entry(agent, user_input: str, kwargs: dict):
    from pulsara_agent.event import EventContext
    from pulsara_agent.runtime.run_entry import (
        CommittedHostRunEntry,
        install_run_working_set,
        prepare_agent_run_draft,
    )
    from pulsara_agent.runtime.session import EventPublicationAfterCommitError

    state = kwargs["state"]
    target = kwargs["run_model_target"]
    if (
        agent._subagent_parent_features_enabled
        and agent.subagent_runtime is not None
        and not agent._subagent_dangling_repair_done
    ):
        await agent.subagent_runtime.repair_dangling_children()
        agent._subagent_dangling_repair_done = True
    draft = await prepare_agent_run_draft(
        agent,
        state,
        run_model_target=target,
        permission_snapshot=state.permission_snapshot,
        current_user_message=state.scratchpad["current_user_message_fact"],
        run_start_event_id=f"run_start:test:{uuid4().hex}",
        terminal_run_end_event_id=state.scratchpad["terminal_run_end_event_id"],
        capability_basis=state.scratchpad["capability_resolve_basis"].fact,
        frozen_execution_surface=state.scratchpad[
            "frozen_capability_execution_surface"
        ],
        new_run_boundary=state.scratchpad["new_run_boundary_fact"],
        subagent_run_entry=None,
        prior_messages=kwargs.get("prior_messages"),
    )
    audits = agent.runtime_session.pending_mcp_installation_audit_events(
        EventContext(
            run_id=state.run_id,
            turn_id=state.turn_id,
            reply_id=state.reply_id,
        )
    )
    try:
        stored = tuple(
            await agent.runtime_session.emit_many(
                (draft.run_start_event, *audits),
                state=state,
            )
        )
    except EventPublicationAfterCommitError as exc:
        agent.runtime_session.acknowledge_committed_mcp_installation_audits(
            exc.result.committed_events
        )
        raise
    agent.runtime_session.acknowledge_committed_mcp_installation_audits(stored)
    run_start = stored[0]
    assert run_start.sequence is not None
    assert draft.run_start_event.new_run_boundary is not None
    committed = CommittedHostRunEntry(
        run_start_event=run_start,
        run_start_sequence=run_start.sequence,
        committed_through_sequence=stored[-1].sequence or run_start.sequence,
        publication_status="completed",
        boundary_id=draft.run_start_event.new_run_boundary.identity.boundary_id,
        committed_audit_event_ids=tuple(event.id for event in stored[1:]),
    )
    install_run_working_set(
        state,
        committed,
        plan_snapshot=state.scratchpad["host_run_boundary_plan"],
        capability_resolve_basis=state.scratchpad["capability_resolve_basis"],
        frozen_execution_surface=state.scratchpad[
            "frozen_capability_execution_surface"
        ],
    )
    return draft, committed, stored


def _prepare_test_host_run_entry(agent, user_input: str, kwargs: dict) -> None:
    """Provide the typed Host run-entry contract for direct component tests."""

    from pulsara_agent.event.events import utc_now
    from pulsara_agent.capability.types import (
        CapabilityExecutionSurfaceSnapshotContext,
    )
    from pulsara_agent.primitives.capability import build_capability_resolve_basis
    from pulsara_agent.primitives.model_call import sha256_fingerprint
    from pulsara_agent.primitives.run_boundary import (
        BoundaryTranscriptSnapshotFact,
        NewRunBoundaryFact,
        PlanWorkflowStateFact,
    )
    from pulsara_agent.primitives.run_entry import (
        CapabilityExposureOwnerFact,
        CurrentUserMessageFact,
        HostRunBoundaryIdentityFact,
        text_sha256,
    )
    from pulsara_agent.tools.registry import build_tool_binding_contract
    from pulsara_agent.primitives.permission import preset_permission_policy_fact
    from pulsara_agent.runtime.run_entry import CapabilityResolveBasis

    _ensure_test_postgres_runtime_owner(agent)
    state = kwargs.setdefault("state", agent.new_state())
    target = kwargs.setdefault("run_model_target", agent.resolve_run_model_target())
    permission = agent._capture_run_permission_snapshot(state)
    observed_at = utc_now()
    boundary = HostRunBoundaryIdentityFact(
        boundary_id=f"run_boundary:test:{uuid4().hex}",
        kind="pre_run",
        runtime_session_id=agent.runtime_session.runtime_session_id,
        run_id=state.run_id,
        turn_id=state.turn_id,
        reply_id=state.reply_id,
        attempt_number=1,
        observed_at_utc=observed_at,
    )
    owner = CapabilityExposureOwnerFact(
        owner_kind="host_boundary",
        owner_id=boundary.boundary_id,
        host_boundary_kind="pre_run",
        runtime_session_id=boundary.runtime_session_id,
        run_id=boundary.run_id,
    )
    for tool_name in agent.tool_executor.registry.names():
        if agent.tool_executor.registry.binding_contract(tool_name) is None:
            agent.tool_executor.registry.bind_contract(
                build_tool_binding_contract(
                    tool_name=tool_name,
                    origin="custom",
                    contract_id=f"test.direct.{tool_name}",
                    contract_version="v1",
                )
            )
    frozen_surface = agent.capability_runtime.freeze_execution_surface(
        CapabilityExecutionSurfaceSnapshotContext(
            workspace_root=agent.runtime_session.workspace_root,
            workspace_kind=agent.workspace_kind,
            available_tool_names=frozenset(agent.tool_executor.registry.names()),
            mcp_installation_id=agent.runtime_session.mcp_installation_id,
        ),
        tool_registry=agent.tool_executor.registry,
        archive=agent.runtime_session.archive,
        runtime_session_id=agent.runtime_session.runtime_session_id,
        owner_id=boundary.boundary_id,
    )
    surface = frozen_surface.identity
    basis = build_capability_resolve_basis(
        basis_id=f"capability_basis:test:{uuid4().hex}",
        basis_kind="initial",
        source_basis_id=None,
        source_basis_fingerprint=None,
        owner=owner,
        workspace_identity_fingerprint=sha256_fingerprint(
            "test-workspace:v1", str(agent.runtime_session.workspace_root)
        ),
        memory_domain_id="memory_domain:test",
        permission_snapshot_id=permission.snapshot_id,
        plan_active=False,
        active_skill_names=tuple(sorted(kwargs.get("active_skill_names") or ())),
        user_intent_fingerprint=sha256_fingerprint("test-user-intent:v1", user_input),
        prior_transcript_fingerprint=sha256_fingerprint(
            "test-prior-transcript:v1",
            [
                message.model_dump(mode="json")
                for message in (kwargs.get("prior_messages") or ())
            ],
        ),
        mcp_installation_id=surface.mcp_installation_id,
        execution_surface_identity=surface,
    )
    transcript = BoundaryTranscriptSnapshotFact(
        source_through_sequence=0,
        source_event_count=0,
        compacted_window_id=None,
        preflight_compaction_id=None,
        preflight_compaction_terminal_event_id=None,
        preflight_compaction_terminal_sequence=None,
    )
    current_user = CurrentUserMessageFact(
        message_id=f"user-message:{state.run_id}",
        source_kind="host_user_input",
        text=user_input,
        observed_at_utc=observed_at,
        content_sha256=text_sha256(user_input),
        source_artifact_id=None,
    )
    state.permission_snapshot = permission
    state.run_model_target = target
    state.scratchpad.update(
        {
            "current_user_message_fact": current_user,
            "terminal_run_end_event_id": f"run_end:test:{uuid4().hex}",
            "new_run_boundary_fact": NewRunBoundaryFact(
                identity=boundary,
                transcript=transcript,
                model_target_fingerprint=target.fact.target_fingerprint,
                permission_snapshot_id=permission.snapshot_id,
                mcp_installation_id=surface.mcp_installation_id,
                capability_basis=basis,
                degraded_reason_codes=(),
            ),
            "frozen_capability_execution_surface": frozen_surface,
            "capability_resolve_basis": CapabilityResolveBasis(
                fact=basis,
                user_input=user_input,
                prior_messages=tuple(
                    message.model_copy(deep=True)
                    for message in (kwargs.get("prior_messages") or ())
                ),
                active_skill_names=frozenset(kwargs.get("active_skill_names") or ()),
                workspace_root=agent.runtime_session.workspace_root,
                memory_domain_id="memory_domain:test",
            ),
            "host_run_boundary_plan": PlanWorkflowStateFact(
                workflow_id=None,
                active=False,
                revision=0,
                entered_event_id=None,
                entered_event_sequence=None,
                entry_run_id=None,
                entry_turn_id=None,
                entry_reply_id=None,
                stored_default_permission=preset_permission_policy_fact(
                    permission.permission_mode
                ),
                accepted_plan_artifact_id=None,
            ),
        }
    )


def _ensure_test_postgres_runtime_owner(agent) -> None:
    """Mirror the production Host's durable session-owner precondition.

    Direct component tests intentionally bypass HostCore/SessionManifestStore,
    but PostgreSQL artifacts still require their runtime session owner to exist
    before the pre-RunStart capability surface is frozen.
    """

    from pulsara_agent.memory import PostgresArtifactStore

    archive = agent.runtime_session.archive
    if not isinstance(archive, PostgresArtifactStore):
        return

    import psycopg

    with psycopg.connect(archive.dsn) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                insert into sessions (id, workspace_root)
                values (%s, %s)
                on conflict (id) do nothing
                """,
                (
                    agent.runtime_session.runtime_session_id,
                    str(agent.runtime_session.workspace_root),
                ),
            )


@dataclass(frozen=True, slots=True)
class _ContractOnlyTransport:
    api: str
    binding_id: str = "test.contract_only"
    contract_version: str = "v1"

    async def stream(
        self,
        *,
        call,
        context: LLMContext,
        event_context: EventContext,
    ) -> AsyncIterator[AgentEvent]:
        if False:
            yield  # pragma: no cover


def test_model_limits(
    *,
    total_context_tokens: int = 256_000,
    max_input_tokens: int = 256_000,
    max_output_tokens: int = 8_192,
    default_output_tokens: int = 8_000,
    input_safety_margin_tokens: int = 64_000,
) -> ModelContextLimits:
    return ModelContextLimits(
        total_context_tokens=total_context_tokens,
        max_input_tokens=max_input_tokens,
        max_output_tokens=max_output_tokens,
        default_output_tokens=default_output_tokens,
        input_safety_margin_tokens=input_safety_margin_tokens,
    )


def test_model_slot(
    model_id: str,
    *,
    limits: ModelContextLimits | None = None,
) -> ModelSlotConfig:
    return ModelSlotConfig(model_id=model_id, limits=limits or test_model_limits())


def test_llm_config(
    *,
    api_key: str,
    base_url: str,
    pro_model: str,
    flash_model: str,
    api: str = "openai_responses",
    provider: str = "custom",
    provider_profile: ProviderProfile | None = None,
    retry: LLMRetryConfig = LLMRetryConfig(),
    openai_sdk_max_retries: int | None = None,
    pro_limits: ModelContextLimits | None = None,
    flash_limits: ModelContextLimits | None = None,
) -> LLMConfig:
    """Build a production-shaped config while keeping terse test call sites."""

    return LLMConfig(
        api_key=api_key,
        base_url=base_url,
        pro=test_model_slot(pro_model, limits=pro_limits),
        flash=test_model_slot(flash_model, limits=flash_limits),
        api=api,
        provider=provider,
        provider_profile=provider_profile,
        retry=retry,
        openai_sdk_max_retries=openai_sdk_max_retries,
    )


def test_resolved_target_fact(
    *,
    model_id: str = "test-pro",
    role: ModelRole = ModelRole.PRO,
    limits: ModelContextLimits | None = None,
) -> ResolvedModelTargetFact:
    config = test_llm_config(
        api_key="test-key",
        base_url="https://example.test/v1",
        pro_model=model_id if role is ModelRole.PRO else "test-pro",
        flash_model=model_id if role is ModelRole.FLASH else "test-flash",
        api="mock",
        pro_limits=limits,
        flash_limits=limits,
    )
    registry = LLMTransportRegistry()
    registry.register(MockTransport(text="test"))
    return resolve_model_target(
        config=config,
        registry=registry,
        role=role,
        requested_options=None,
    ).fact


def test_resolved_call_fact(
    *,
    purpose: ModelCallPurpose = ModelCallPurpose.AGENT_MODEL_LOOP,
) -> ResolvedModelCallFact:
    config = test_llm_config(
        api_key="test-key",
        base_url="https://example.test/v1",
        pro_model="test-pro",
        flash_model="test-flash",
        api="mock",
    )
    registry = LLMTransportRegistry()
    registry.register(MockTransport(text="test"))
    role = (
        ModelRole.PRO
        if purpose is ModelCallPurpose.AGENT_MODEL_LOOP
        else ModelRole.FLASH
    )
    target = resolve_model_target(
        config=config,
        registry=registry,
        role=role,
        requested_options=None,
    )
    return resolve_model_call(target=target, purpose=purpose).fact


def test_resolved_call(
    *,
    purpose: ModelCallPurpose = ModelCallPurpose.AGENT_MODEL_LOOP,
    limits: ModelContextLimits | None = None,
    options: LLMOptions | None = None,
    provider_profile: ProviderProfile | None = None,
):
    """Return a runtime call for component tests that do not own an LLM runtime."""

    config = test_llm_config(
        api_key="test-key",
        base_url="https://example.test/v1",
        pro_model="test-pro",
        flash_model="test-flash",
        api="mock",
        provider_profile=provider_profile,
        pro_limits=limits,
        flash_limits=limits,
    )
    role = (
        ModelRole.PRO
        if purpose is ModelCallPurpose.AGENT_MODEL_LOOP
        else ModelRole.FLASH
    )
    return resolve_test_call(config, role=role, purpose=purpose, options=options)


def resolve_test_call(
    config: LLMConfig,
    *,
    role: ModelRole = ModelRole.PRO,
    options: LLMOptions | None = None,
    transport=None,
    purpose: ModelCallPurpose = ModelCallPurpose.AGENT_MODEL_LOOP,
):
    registry = LLMTransportRegistry()
    registry.register(transport or _ContractOnlyTransport(api=config.api))
    target = resolve_model_target(
        config=config,
        registry=registry,
        role=role,
        requested_options=options,
    )
    return resolve_model_call(target=target, purpose=purpose)


def bind_test_context(
    call,
    context: LLMContext,
    *,
    context_id: str | None = None,
    model_call_index: int | None = None,
) -> LLMContext:
    index = model_call_index
    if index is None and call.fact.context_mode == "compiled":
        index = context.model_call_index if context.model_call_index is not None else 1
    bound = replace(
        context,
        context_id=context_id or context.context_id or "context:test",
        resolved_model_call_id=call.fact.resolved_model_call_id,
        target_fingerprint=call.target.fact.target_fingerprint,
        model_call_index=index,
    )
    if (
        call.fact.context_mode == "compiled"
        and bound.compiler_estimated_input_tokens is None
    ):
        bound = replace(
            bound,
            compiler_estimated_input_tokens=(
                call.target.token_estimator.estimate_context(bound).total_input_tokens
            ),
        )
    return bound


def test_llm_context(**kwargs) -> LLMContext:
    """Build a structurally complete context before a test binds its real call."""

    kwargs.setdefault("context_id", "context:test-unbound")
    kwargs.setdefault("resolved_model_call_id", f"model_call:{'0' * 32}")
    kwargs.setdefault("target_fingerprint", f"sha256:{'0' * 64}")
    kwargs.setdefault("model_call_index", None)
    return LLMContext(**kwargs)


test_model_limits.__test__ = False
test_model_slot.__test__ = False
test_llm_config.__test__ = False
test_resolved_target_fact.__test__ = False
test_resolved_call_fact.__test__ = False
test_resolved_call.__test__ = False
resolve_test_call.__test__ = False
bind_test_context.__test__ = False
test_llm_context.__test__ = False
model_call_start_fields.__test__ = False
model_call_end_fields.__test__ = False
context_compiled_contract_fields.__test__ = False
compaction_completed_contract_fields.__test__ = False
compaction_started_contract_fields.__test__ = False
compaction_failed_contract_fields.__test__ = False
run_agent_task.__test__ = False
stream_agent_task.__test__ = False
