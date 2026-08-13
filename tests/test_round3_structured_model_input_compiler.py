"""Round 3 structured model-input compiler product and architecture gates."""

from __future__ import annotations

import ast
import asyncio
from dataclasses import fields, replace
from datetime import datetime, timedelta, timezone, tzinfo
import json
from pathlib import Path
import shlex
from time import monotonic, sleep
from types import SimpleNamespace
from typing import get_type_hints
from zoneinfo import ZoneInfo

import pytest

from pulsara_agent.capability.provider import CapabilityProjectionOutput
from pulsara_agent.capability.builtin_catalog import builtin_tool_catalog_entry
from pulsara_agent.capability.types import (
    CapabilityDiagnostic,
    ResolvedSkillCatalogEntry,
)
from pulsara_agent.conversation_kernel.assembler import CompletedToolCallBlock
from pulsara_agent.conversation_kernel.context_sources import (
    ContextSourceRegistry,
    KernelContextSourceCollector,
)
from pulsara_agent.conversation_kernel.direct_model import (
    DirectKernelModelPort,
    KernelModelPreparationRequest,
)
from pulsara_agent.conversation_kernel.input_continuity import (
    HostProviderInputContinuityOwner,
    ProviderInputContinuityConflict,
)
from pulsara_agent.conversation_kernel.execution_watchdogs import (
    KernelWatchdogOwner,
)
from pulsara_agent.conversation_kernel.io import KernelSessionIO
from pulsara_agent.conversation_kernel.extensions import OperationalHookType
from pulsara_agent.conversation_kernel.repository import AssistantToolCallBlock
from pulsara_agent.conversation_kernel.runner import (
    ConversationKernelRunner,
    KernelToolAuthorizationKind,
    KernelToolInvocationContext,
    _prepared_append_candidate,
)
from pulsara_agent.conversation_kernel.tool_policy import (
    DefaultToolDispatchAuthorizationPolicy,
)
from pulsara_agent.conversation_kernel.tool_runtime import DirectKernelToolPort
from pulsara_agent.conversation_kernel.live import LiveAgentEventBus
from pulsara_agent.llm.input import MessageRole
from pulsara_agent.model_input.compiler import (
    COMPILER_CONTRACT_VERSION,
    StructuredModelInputCompiler,
    _message_logical_utf8_bytes,
)
from pulsara_agent.model_input.contracts import (
    ApprovedPlanMaterializationFact,
    CapabilityActivationSubjectKind,
    CanonicalInputOriginKind,
    CanonicalModelInputIdentity,
    CanonicalModelInputSnapshot,
    CollectedContextSources,
    ContextBudgetClass,
    ContextChannel,
    ContextPublicDiagnosticCode,
    ContextRenderMode,
    ContextRenderVariant,
    ContextSourceCandidate,
    ContextSourceAbsentFact,
    ContextSourceAbsenceKind,
    ContextSourceKind,
    ContextSourceLifecycle,
    ContextTrustClass,
    ContextBindingBaseKind,
    FrozenContextBindingCompileFact,
    FrozenCanonicalCompileSnapshot,
    FrozenPlanHandoffCompileFact,
    FrozenProviderInputItem,
    FrozenProviderInputItemKind,
    ModelInputCompileFailureKind,
    ModelInputScopeKind,
    ModelInputTokenEstimator,
    ProviderToolCall,
    ProviderToolResultContextMetadata,
    StructuredModelInputCompileError,
    StructuredModelInputCompileRequest,
    StructuredModelInputLimits,
    ToolResultProviderRenderMode,
    canonical_model_input_identity_fingerprint,
    canonical_model_input_snapshot_fingerprint,
    canonical_compile_snapshot_fingerprint,
    context_binding_compile_fact_fingerprint,
    approved_plan_materialization_fingerprint,
    plan_handoff_compile_fact_fingerprint,
    provider_input_item_fingerprint,
    model_input_compile_binding_fingerprint,
)
from pulsara_agent.model_input.continuity import (
    FULL_HISTORY_CONTEXT_BASE_IDENTITY,
    NewTriggerAnchor,
    NoNewTriggerAnchor,
    ProcessLocalCanonicalFrontier,
    ProviderInputContinuityScope,
    ProviderInputEpochCompatibility,
    ProviderInputEpochResetReason,
    PROVIDER_MESSAGE_LOWERING_CONTRACT,
    decode_runtime_observation,
)
from pulsara_agent.model_input.lowering import (
    lower_canonical_item,
    source_variant_message,
)
from pulsara_agent.model_input.diagnostics import (
    CompileDecisionSampleKind,
    ModelInputCompileOperationalProjection,
    project_model_input_compile_observation,
)
from pulsara_agent.ports.artifact import (
    ToolOutputArtifactDisposition,
    ToolOutputArtifactUnavailabilityReason,
    ToolResultDisplayKind,
)
from pulsara_agent.ports.tool_execution import (
    ToolOutputSourceCoverage,
    ToolOutputSourceCoverageReason,
)
from pulsara_agent.primitives.context import (
    FrozenJsonObjectFact,
    context_fingerprint,
    freeze_json,
)
from pulsara_agent.primitives.model_call import ModelCallPurpose
from pulsara_agent.primitives.permission import PermissionMode
from pulsara_agent.primitives.plan_workflow import (
    PlanApprovedMaterializationDisposition,
    PlanHandoffKind,
    PlanInteractionBinding,
    PlanWorkflowStatus,
    extract_plan_draft,
)
from pulsara_agent.primitives.run_permission import (
    RunPermissionAdmissionSource,
    build_run_permission_snapshot,
)
from pulsara_agent.terminal_process.models import TerminalRequest, TerminalStatus
from tests.support.model_config import test_llm_config
from tests.support.round3 import StructuredToolPort


_SOURCE_FACTS = {
    ContextSourceKind.BASE_SYSTEM: (
        "pulsara.base-system.prefix-continuity.v2",
        ContextChannel.SYSTEM,
        ContextTrustClass.ROOT_INSTRUCTION,
        ContextBudgetClass.MUST_KEEP,
        0,
        0,
        (ContextRenderMode.FULL,),
        ContextSourceLifecycle.EPOCH_ROOT,
    ),
    ContextSourceKind.RUNTIME_ENVIRONMENT: (
        "pulsara.runtime-environment.v1",
        ContextChannel.RUNTIME_OBSERVATION,
        ContextTrustClass.TRUSTED_RUNTIME_FACT,
        ContextBudgetClass.MUST_KEEP,
        10,
        10,
        (ContextRenderMode.FULL, ContextRenderMode.COMPACT),
        ContextSourceLifecycle.SNAPSHOT_ON_CHANGE,
    ),
    ContextSourceKind.RUNTIME_CLOCK: (
        "pulsara.runtime-clock.v1",
        ContextChannel.RUNTIME_OBSERVATION,
        ContextTrustClass.TRUSTED_RUNTIME_FACT,
        ContextBudgetClass.OPTIONAL,
        90,
        80,
        (ContextRenderMode.FULL, ContextRenderMode.COMPACT),
        ContextSourceLifecycle.CALL_APPEND,
    ),
    ContextSourceKind.RUN_PERMISSION: (
        "pulsara.run-permission.v1",
        ContextChannel.RUNTIME_OBSERVATION,
        ContextTrustClass.AUTHORIZED_RUNTIME_GUIDANCE,
        ContextBudgetClass.MUST_KEEP,
        20,
        12,
        (ContextRenderMode.FULL, ContextRenderMode.COMPACT),
        ContextSourceLifecycle.TURN_APPEND,
    ),
    ContextSourceKind.CAPABILITY_CATALOG: (
        "pulsara.capability-catalog.v1",
        ContextChannel.RUNTIME_OBSERVATION,
        ContextTrustClass.AUTHORIZED_CAPABILITY_CONTEXT,
        ContextBudgetClass.IMPORTANT,
        50,
        30,
        (
            ContextRenderMode.FULL,
            ContextRenderMode.COMPACT,
            ContextRenderMode.REF_ONLY,
        ),
        ContextSourceLifecycle.SNAPSHOT_ON_CHANGE,
    ),
    ContextSourceKind.PLAN_HANDOFF: (
        "pulsara.plan-handoff.v1",
        ContextChannel.RUNTIME_OBSERVATION,
        ContextTrustClass.AUTHORIZED_RUNTIME_GUIDANCE,
        ContextBudgetClass.MUST_KEEP,
        30,
        11,
        (ContextRenderMode.FULL, ContextRenderMode.COMPACT),
        ContextSourceLifecycle.ONE_SHOT,
    ),
    ContextSourceKind.PLAN_WORKFLOW: (
        "pulsara.plan-workflow.v1",
        ContextChannel.RUNTIME_OBSERVATION,
        ContextTrustClass.AUTHORIZED_RUNTIME_GUIDANCE,
        ContextBudgetClass.MUST_KEEP,
        40,
        10,
        (ContextRenderMode.FULL, ContextRenderMode.COMPACT),
        ContextSourceLifecycle.SNAPSHOT_ON_CHANGE,
    ),
    ContextSourceKind.ACTIVE_SKILL: (
        "pulsara.active-skill.v1",
        ContextChannel.RUNTIME_OBSERVATION,
        ContextTrustClass.AUTHORIZED_CAPABILITY_CONTEXT,
        ContextBudgetClass.MUST_KEEP,
        60,
        20,
        (ContextRenderMode.FULL,),
        ContextSourceLifecycle.ACTIVATION_SNAPSHOT,
    ),
}


def _candidate(
    kind: ContextSourceKind,
    texts: tuple[str, ...],
    *,
    trust: ContextTrustClass | None = None,
    channel: ContextChannel | None = None,
) -> ContextSourceCandidate:
    (
        version,
        expected_channel,
        expected_trust,
        budget,
        placement,
        degradation,
        modes,
        lifecycle,
    ) = _SOURCE_FACTS[kind]
    selected_channel = channel or expected_channel
    selected_trust = trust or expected_trust
    assert len(texts) == len(modes)
    variants = tuple(
        ContextRenderVariant(
            mode=mode,
            text=text,
            utf8_bytes=len(text.encode("utf-8")),
            semantic_fingerprint=context_fingerprint(
                "context-render-variant:v1", {"mode": mode.value, "text": text}
            ),
        )
        for mode, text in zip(modes, texts, strict=True)
    )
    contract = context_fingerprint(
        "context-source-contract:v1",
        {
            "kind": kind.value,
            "version": version,
            "channel": selected_channel.value,
            "trust": selected_trust.value,
            "budget": budget.value,
            "placement": placement,
            "degradation": degradation,
            "modes": tuple(mode.value for mode in modes),
            "lifecycle": lifecycle.value,
        },
    )
    instance = f"source:{kind.value.lower()}"
    semantic = context_fingerprint(
        "context-source-candidate:v1",
        {
            "source_kind": kind.value,
            "source_instance_id": instance,
            "source_contract_fingerprint": contract,
            "variants": tuple(item.semantic_fingerprint for item in variants),
        },
    )
    return ContextSourceCandidate(
        source_kind=kind,
        source_instance_id=instance,
        source_contract_version=version,
        source_contract_fingerprint=contract,
        source_semantic_fingerprint=semantic,
        channel=selected_channel,
        trust_class=selected_trust,
        budget_class=budget,
        placement_ordinal=placement,
        degradation_priority=degradation,
        variants=variants,
        lifecycle=lifecycle,
        domain_semantic_fingerprint=semantic,
    )


def _sources(
    *candidates: ContextSourceCandidate,
    absent_facts: tuple[ContextSourceAbsentFact, ...] = (),
) -> CollectedContextSources:
    kinds = {candidate.source_kind for candidate in candidates}
    required: list[ContextSourceCandidate] = []
    if ContextSourceKind.BASE_SYSTEM not in kinds:
        required.append(_candidate(ContextSourceKind.BASE_SYSTEM, ("BASE",)))
    if ContextSourceKind.RUNTIME_ENVIRONMENT not in kinds:
        required.append(
            _candidate(ContextSourceKind.RUNTIME_ENVIRONMENT, ("runtime", "runtime"))
        )
    if ContextSourceKind.RUN_PERMISSION not in kinds:
        required.append(
            _candidate(
                ContextSourceKind.RUN_PERMISSION,
                ("permission=bypass-permissions", "permission=bypass"),
            )
        )
    candidates = (*required, *candidates)
    candidate_kinds = {candidate.source_kind for candidate in candidates}
    absent_by_kind = {item.source_kind: item for item in absent_facts}
    default_absences = {
        ContextSourceKind.RUNTIME_CLOCK: ContextSourceAbsenceKind.UNAVAILABLE,
        ContextSourceKind.PLAN_HANDOFF: ContextSourceAbsenceKind.NOT_APPLICABLE,
        ContextSourceKind.PLAN_WORKFLOW: ContextSourceAbsenceKind.EXPLICIT_EMPTY,
        ContextSourceKind.CAPABILITY_CATALOG: ContextSourceAbsenceKind.EXPLICIT_EMPTY,
        ContextSourceKind.ACTIVE_SKILL: ContextSourceAbsenceKind.EXPLICIT_EMPTY,
    }
    for kind, absence_kind in default_absences.items():
        if kind not in candidate_kinds and kind not in absent_by_kind:
            absent_by_kind[kind] = _absent(kind, absence_kind)
    absent_facts = tuple(absent_by_kind.values())
    registry = ContextSourceRegistry().fingerprint
    fingerprint = context_fingerprint(
        "collected-context-sources:v1",
        {
            "registry_fingerprint": registry,
            "candidates": tuple(
                candidate.source_semantic_fingerprint for candidate in candidates
            ),
            "diagnostics": (),
            "absent": tuple(
                (
                    item.source_kind.value,
                    item.lifecycle.value,
                    item.absence_kind.value,
                    item.domain_semantic_fingerprint,
                )
                for item in absent_facts
            ),
        },
    )
    return CollectedContextSources(
        tuple(candidates), (), registry, fingerprint, absent_facts
    )


def _absent(
    kind: ContextSourceKind,
    absence_kind: ContextSourceAbsenceKind,
) -> ContextSourceAbsentFact:
    (
        version,
        channel,
        trust,
        budget,
        placement,
        degradation,
        modes,
        lifecycle,
    ) = _SOURCE_FACTS[kind]
    contract = context_fingerprint(
        "context-source-contract:v1",
        {
            "kind": kind.value,
            "version": version,
            "channel": channel.value,
            "trust": trust.value,
            "budget": budget.value,
            "placement": placement,
            "degradation": degradation,
            "modes": tuple(mode.value for mode in modes),
            "lifecycle": lifecycle.value,
        },
    )
    return ContextSourceAbsentFact(
        source_kind=kind,
        lifecycle=lifecycle,
        absence_kind=absence_kind,
        source_contract_version=version,
        source_contract_fingerprint=contract,
        trust_class=trust,
        budget_class=budget,
        placement_ordinal=placement,
        degradation_priority=degradation,
        domain_semantic_fingerprint=context_fingerprint(
            "pulsara:context-source-absence:v1",
            {"kind": kind.value, "absence": absence_kind.value, "contract": contract},
        ),
    )


def _snapshot(
    *items: FrozenProviderInputItem,
    scope: ModelInputScopeKind = ModelInputScopeKind.ROOT,
    turn_id: str = "turn:test",
    scope_subagent_task_id: str | None = None,
    canonical_utf8_bytes: int | None = None,
) -> CanonicalModelInputSnapshot:
    scope_task = (
        None
        if scope is ModelInputScopeKind.ROOT
        else scope_subagent_task_id or "task:test"
    )
    initial_entry_id = next(
        (
            item.source_entry_id
            for item in items
            if item.source_turn_id == turn_id and item.source_entry_id is not None
        ),
        "entry:initial",
    )
    identity = CanonicalModelInputIdentity(
        session_id="session:test",
        turn_id=turn_id,
        initial_entry_id=initial_entry_id,
        context_binding_revision_id="revision:test",
        provider_input_through_sequence=max(
            (item.source_entry_sequence or 0 for item in items), default=0
        ),
        conversation_scope_kind=scope,
        scope_subagent_task_id=scope_task,
        identity_fingerprint=canonical_model_input_identity_fingerprint(
            session_id="session:test",
            turn_id=turn_id,
            initial_entry_id=initial_entry_id,
            context_binding_revision_id="revision:test",
            provider_input_through_sequence=max(
                (item.source_entry_sequence or 0 for item in items), default=0
            ),
            conversation_scope_kind=scope,
            scope_subagent_task_id=scope_task,
        ),
    )
    logical_bytes = (
        sum(len(item.text.encode("utf-8")) for item in items)
        if canonical_utf8_bytes is None
        else canonical_utf8_bytes
    )
    fingerprint = canonical_model_input_snapshot_fingerprint(
        identity=identity,
        items=tuple(items),
        canonical_utf8_bytes=logical_bytes,
        closures=(),
        late_outcomes=(),
    )
    return CanonicalModelInputSnapshot(
        identity=identity,
        items=tuple(items),
        canonical_utf8_bytes=logical_bytes,
        snapshot_fingerprint=fingerprint,
    )


def _user(
    text: str,
    *,
    sequence: int = 1,
    turn_id: str = "turn:test",
    origin: CanonicalInputOriginKind = CanonicalInputOriginKind.HUMAN_MESSAGE,
):
    return FrozenProviderInputItem(
        FrozenProviderInputItemKind.USER,
        f"entry:{sequence}",
        sequence,
        turn_id,
        text,
        input_origin=origin,
    )


def _tool_result(
    body: str,
    *,
    sequence: int,
    turn_id: str,
    artifact: bool = True,
) -> FrozenProviderInputItem:
    return FrozenProviderInputItem(
        FrozenProviderInputItemKind.TOOL_RESULT,
        f"entry:{sequence}",
        sequence,
        turn_id,
        body,
        tool_call_id=f"call:{sequence}",
        tool_result_context=ProviderToolResultContextMetadata(
            result_state="SUCCESS",
            display_kind=ToolResultDisplayKind.COMPLETE,
            artifact_disposition=(
                ToolOutputArtifactDisposition.AVAILABLE
                if artifact
                else ToolOutputArtifactDisposition.NOT_REQUIRED
            ),
            artifact_id=f"artifact:{sequence}" if artifact else None,
            source_coverage=ToolOutputSourceCoverage.COMPLETE,
            source_coverage_reason=None,
            artifact_unavailability_reason=None,
        ),
        tool_result_body_text=body,
    )


def _prepared_request(
    snapshot: CanonicalModelInputSnapshot,
    sources: CollectedContextSources,
    *,
    budget: int = 100_000,
    tool_names: tuple[str, ...] = (),
    canonical_facts: FrozenCanonicalCompileSnapshot | None = None,
) -> StructuredModelInputCompileRequest:
    tools = StructuredToolPort(object(), tool_names=tool_names)
    prepared_surface = tools.snapshot_tool_surface(
        conversation_scope_kind=snapshot.identity.conversation_scope_kind,
        scope_subagent_task_id=snapshot.identity.scope_subagent_task_id,
    )
    model = DirectKernelModelPort(
        config=test_llm_config(
            api_key="test",
            base_url="https://example.invalid/v1",
            pro_model="test-pro",
            flash_model="test-flash",
            api="openai_chat_completions",
        )
    )
    prepared = model.prepare_call(
        KernelModelPreparationRequest(
            session_id=snapshot.identity.session_id,
            turn_id=snapshot.identity.turn_id,
            model_call_index=1,
            purpose=ModelCallPurpose.AGENT_MODEL_LOOP,
            maximum_input_tokens=max(budget, 1),
            maximum_output_tokens=16_384,
            tool_surface=prepared_surface,
        )
    )
    binding = prepared.compile_binding
    effective = min(budget, binding.effective_input_budget_tokens)
    binding = replace(
        binding,
        effective_input_budget_tokens=effective,
        binding_fingerprint=model_input_compile_binding_fingerprint(
            call_fact=binding.call_fact,
            target_fact=binding.target_fact,
            estimator_fingerprint=binding.estimator_fingerprint,
            effective_input_budget_tokens=effective,
            effective_output_tokens=binding.effective_output_tokens,
            tool_surface=binding.tool_surface,
        ),
    )
    canonical_facts = canonical_facts or _canonical_facts(snapshot)
    return StructuredModelInputCompileRequest(
        context_id="context:test",
        model_call_index=1,
        canonical_input=snapshot,
        canonical_facts=canonical_facts,
        compile_binding=binding,
        sources=sources,
    )


def _canonical_facts(
    snapshot: CanonicalModelInputSnapshot | None = None,
) -> FrozenCanonicalCompileSnapshot:
    canonical = snapshot or _snapshot()
    binding = _context_binding_fact(canonical)
    permission = build_run_permission_snapshot(
        snapshot_id="permission:test",
        requested_mode=PermissionMode.BYPASS_PERMISSIONS,
        effective_mode=PermissionMode.BYPASS_PERMISSIONS,
        admission_source=RunPermissionAdmissionSource.USER_SUBMISSION,
    )
    provisional = FrozenCanonicalCompileSnapshot.__new__(FrozenCanonicalCompileSnapshot)
    object.__setattr__(provisional, "canonical_input", canonical)
    object.__setattr__(provisional, "context_binding_fact", binding)
    object.__setattr__(provisional, "run_permission_snapshot", permission)
    object.__setattr__(provisional, "plan_workflow_fact", None)
    object.__setattr__(provisional, "plan_handoff_fact", None)
    object.__setattr__(provisional, "approved_plan_materialization_fact", None)
    object.__setattr__(provisional, "canonical_read_cut_fingerprint", "")
    return FrozenCanonicalCompileSnapshot(
        canonical_input=canonical,
        context_binding_fact=binding,
        run_permission_snapshot=permission,
        plan_workflow_fact=None,
        plan_handoff_fact=None,
        approved_plan_materialization_fact=None,
        canonical_read_cut_fingerprint=canonical_compile_snapshot_fingerprint(
            provisional
        ),
    )


def _context_binding_fact(
    snapshot: CanonicalModelInputSnapshot,
) -> FrozenContextBindingCompileFact:
    values = {
        "binding_revision_id": snapshot.identity.context_binding_revision_id,
        "revision_ordinal": 0,
        "base_kind": ContextBindingBaseKind.FULL_HISTORY,
        "context_snapshot_id": None,
        "source_through_sequence": 0,
        "context_base_semantic_identity": FULL_HISTORY_CONTEXT_BASE_IDENTITY,
    }
    provisional = FrozenContextBindingCompileFact.__new__(
        FrozenContextBindingCompileFact
    )
    for name, value in values.items():
        object.__setattr__(provisional, name, value)
    object.__setattr__(provisional, "fact_fingerprint", "")
    return FrozenContextBindingCompileFact(
        **values,
        fact_fingerprint=context_binding_compile_fact_fingerprint(provisional),
    )


def _append_compatibility(
    request: StructuredModelInputCompileRequest,
) -> ProviderInputEpochCompatibility:
    base = next(
        item
        for item in request.sources.candidates
        if item.source_kind is ContextSourceKind.BASE_SYSTEM
    )
    binding = request.compile_binding
    return ProviderInputEpochCompatibility(
        compiler_contract_version=COMPILER_CONTRACT_VERSION,
        base_system_semantic_fingerprint=base.source_semantic_fingerprint,
        tool_surface_fingerprint=binding.tool_surface.surface_fingerprint,
        model_target_fingerprint=binding.target_fact.target_fingerprint,
        estimator_fingerprint=binding.estimator_fingerprint,
        provider_message_lowering_contract=PROVIDER_MESSAGE_LOWERING_CONTRACT,
        context_base_semantic_identity=(
            request.canonical_facts.context_binding_fact.context_base_semantic_identity
        ),
    )


def _append_frontier(
    request: StructuredModelInputCompileRequest,
) -> ProcessLocalCanonicalFrontier:
    snapshot = request.canonical_input
    return ProcessLocalCanonicalFrontier(
        latest_context_binding_revision_id=(
            request.canonical_facts.context_binding_fact.binding_revision_id
        ),
        context_base_semantic_identity=(
            request.canonical_facts.context_binding_fact.context_base_semantic_identity
        ),
        through_sequence=snapshot.identity.provider_input_through_sequence,
        ordered_item_fingerprints=tuple(
            provider_input_item_fingerprint(item) for item in snapshot.items
        ),
    )


def _append_anchor(
    request: StructuredModelInputCompileRequest,
) -> NewTriggerAnchor:
    item = request.canonical_input.items[-1]
    fingerprint = provider_input_item_fingerprint(item)
    return NewTriggerAnchor(
        source_entry_id=item.source_entry_id or "",
        provider_input_item_fingerprint=fingerprint,
        provider_group_boundary_fingerprint=context_fingerprint(
            "pulsara:provider-group-boundary:v1",
            {
                "entry_id": item.source_entry_id,
                "item": fingerprint,
                "sequence": item.source_entry_sequence,
            },
        ),
    )


def _compile_and_install_append(
    *,
    compiler: StructuredModelInputCompiler,
    owner: HostProviderInputContinuityOwner,
    request: StructuredModelInputCompileRequest,
    dispatch_anchor=None,
):
    scope = ProviderInputContinuityScope(
        session_id=request.canonical_input.identity.session_id,
        scope_kind=request.canonical_input.identity.conversation_scope_kind,
        scope_subagent_task_id=(
            request.canonical_input.identity.scope_subagent_task_id
        ),
    )
    planning = owner.freeze_planning_input(
        scope=scope,
        canonical_frontier=_append_frontier(request),
        dispatch_anchor=(
            _append_anchor(request) if dispatch_anchor is None else dispatch_anchor
        ),
    )
    compatibility = _append_compatibility(request)
    result = compiler.compile_append(
        request,
        planning=planning,
        compatibility=compatibility,
    )
    candidate = _prepared_append_candidate(
        planning=planning,
        compatibility=compatibility,
        compiled_result=result,
    )
    owner.register(candidate)
    owner.install(
        candidate_fingerprint=candidate.candidate_fingerprint,
        execution_fingerprint=context_fingerprint(
            "test:provider-execution:v1", candidate.candidate_fingerprint
        ),
    )
    view = owner.current_view(scope)
    assert view is not None
    return result, view


def _permission_snapshot():
    return build_run_permission_snapshot(
        snapshot_id="permission:test",
        requested_mode=PermissionMode.BYPASS_PERMISSIONS,
        effective_mode=PermissionMode.BYPASS_PERMISSIONS,
        admission_source=RunPermissionAdmissionSource.USER_SUBMISSION,
    )


def _approved_plan_compile_facts(
    *,
    disposition: PlanApprovedMaterializationDisposition,
    workflow_id: str = "workflow:test",
    interaction_id: str = "interaction:draft",
    transition_digest: str = "sha256:" + "2" * 64,
) -> tuple[FrozenCanonicalCompileSnapshot, str]:
    plan = "PLAN_SENTINEL_EXACTLY_ONCE"
    binding_contract = builtin_tool_catalog_entry("exit_plan").binding_contract.base
    binding = PlanInteractionBinding(
        binding_contract.contract_id,
        binding_contract.contract_version,
        binding_contract.binding_fingerprint,
    )
    arguments = freeze_json({"plan": plan})
    assert isinstance(arguments, FrozenJsonObjectFact)
    assistant = FrozenProviderInputItem(
        FrozenProviderInputItemKind.ASSISTANT_TOOL_REQUEST,
        "entry:draft",
        1,
        "turn:origin",
        "",
        tool_calls=(ProviderToolCall("call:exit", "exit_plan", arguments),),
    )
    continuation = FrozenProviderInputItem(
        FrozenProviderInputItemKind.PLAN_CONTINUATION,
        "entry:continuation",
        2,
        "turn:implementation",
        json.dumps(
            {"handoff": "APPROVED_PLAN", "plan_reference": interaction_id},
            sort_keys=True,
            separators=(",", ":"),
        ),
        input_origin=CanonicalInputOriginKind.PLAN_CONTINUATION,
    )
    items = (
        (assistant, continuation)
        if disposition
        is PlanApprovedMaterializationDisposition.PIN_EXISTING_CANONICAL_BLOCK
        else (continuation,)
    )
    snapshot = _snapshot(*items, turn_id="turn:implementation")
    handoff_values = {
        "session_id": "session:test",
        "workspace_id": "workspace:test",
        "target_turn_id": "turn:implementation",
        "carrier_entry_id": "entry:continuation",
        "carrier_entry_sequence": 2,
        "workflow_id": workflow_id,
        "workflow_ordinal": 1,
        "workflow_revision_at_transition": 4,
        "interaction_id": interaction_id,
        "handoff_kind": PlanHandoffKind.APPROVED_PLAN,
        "workflow_status": PlanWorkflowStatus.APPROVED,
        "resume_permission_mode": PermissionMode.ACCEPT_EDITS,
        "transition_semantic_digest": transition_digest,
    }
    provisional_handoff = FrozenPlanHandoffCompileFact.__new__(
        FrozenPlanHandoffCompileFact
    )
    for name, value in handoff_values.items():
        object.__setattr__(provisional_handoff, name, value)
    object.__setattr__(provisional_handoff, "fact_fingerprint", "")
    handoff = FrozenPlanHandoffCompileFact(
        **handoff_values,
        fact_fingerprint=plan_handoff_compile_fact_fingerprint(provisional_handoff),
    )
    extracted = extract_plan_draft(
        interaction_id=interaction_id,
        assistant_entry_id="entry:draft",
        tool_call_id="call:exit",
        binding=binding,
        request_semantic_digest="sha256:" + "3" * 64,
        arguments=arguments,
    )
    approved_values = {
        "session_id": "session:test",
        "workspace_id": "workspace:test",
        "target_turn_id": "turn:implementation",
        "workflow_id": workflow_id,
        "interaction_id": interaction_id,
        "assistant_entry_id": "entry:draft",
        "tool_call_id": "call:exit",
        "request_contract_id": binding.contract_id,
        "request_contract_version": binding.contract_version,
        "request_contract_fingerprint": binding.contract_fingerprint,
        "request_semantic_digest": "sha256:" + "3" * 64,
        "content_identity": extracted.identity,
        "exact_plan_utf8": extracted.exact_plan_utf8,
        "disposition": disposition,
        "pinned_canonical_item_fingerprint": (
            provider_input_item_fingerprint(assistant)
            if disposition
            is PlanApprovedMaterializationDisposition.PIN_EXISTING_CANONICAL_BLOCK
            else None
        ),
    }
    provisional_approved = ApprovedPlanMaterializationFact.__new__(
        ApprovedPlanMaterializationFact
    )
    for name, value in approved_values.items():
        object.__setattr__(provisional_approved, name, value)
    object.__setattr__(provisional_approved, "fact_fingerprint", "")
    approved = ApprovedPlanMaterializationFact(
        **approved_values,
        fact_fingerprint=approved_plan_materialization_fingerprint(
            provisional_approved
        ),
    )
    permission = build_run_permission_snapshot(
        snapshot_id="permission:implementation",
        requested_mode=PermissionMode.ACCEPT_EDITS,
        effective_mode=PermissionMode.ACCEPT_EDITS,
        admission_source=RunPermissionAdmissionSource.RUNTIME_PLAN_CONTINUATION,
        inherited_from_turn_id="turn:origin",
    )
    provisional = FrozenCanonicalCompileSnapshot.__new__(FrozenCanonicalCompileSnapshot)
    binding = _context_binding_fact(snapshot)
    object.__setattr__(provisional, "canonical_input", snapshot)
    object.__setattr__(provisional, "context_binding_fact", binding)
    object.__setattr__(provisional, "run_permission_snapshot", permission)
    object.__setattr__(provisional, "plan_workflow_fact", None)
    object.__setattr__(provisional, "plan_handoff_fact", handoff)
    object.__setattr__(provisional, "approved_plan_materialization_fact", approved)
    object.__setattr__(provisional, "canonical_read_cut_fingerprint", "")
    facts = FrozenCanonicalCompileSnapshot(
        canonical_input=snapshot,
        context_binding_fact=binding,
        run_permission_snapshot=permission,
        plan_workflow_fact=None,
        plan_handoff_fact=handoff,
        approved_plan_materialization_fact=approved,
        canonical_read_cut_fingerprint=canonical_compile_snapshot_fingerprint(
            provisional
        ),
    )
    return facts, plan


@pytest.mark.parametrize(
    "disposition",
    tuple(PlanApprovedMaterializationDisposition),
)
def test_round4_approved_plan_is_materialized_exactly_once(
    disposition: PlanApprovedMaterializationDisposition,
) -> None:
    facts, plan = _approved_plan_compile_facts(disposition=disposition)
    request = _prepared_request(
        facts.canonical_input,
        _sources(
            _candidate(
                ContextSourceKind.PLAN_HANDOFF,
                ("approved handoff", "approved"),
            )
        ),
        canonical_facts=facts,
    )
    compiled = StructuredModelInputCompiler().compile(request)
    carriers = "\n".join(
        value
        for message in compiled.messages
        for value in (
            *message.content,
            *(call.arguments for call in message.tool_calls),
        )
    )
    assert carriers.count(plan) == 1


def test_round3_1_plan_handoff_occurrence_uses_canonical_transition_identity(
    tmp_path: Path,
) -> None:
    collector = KernelContextSourceCollector(
        workspace_kind="project",
        workspace_root=tmp_path,
        terminal_cwd=_TerminalCwd(tmp_path),
        capability_composer=_Capability(),  # type: ignore[arg-type]
        base_system_prompt="BASE",
        display_timezone=timezone.utc,
        clock=lambda: datetime(2026, 8, 13, tzinfo=timezone.utc),
    )
    surface = (
        StructuredToolPort(object(), tool_names=())
        .snapshot_tool_surface(
            conversation_scope_kind=ModelInputScopeKind.ROOT,
            scope_subagent_task_id=None,
        )
        .model_surface
    )

    def collect(facts: FrozenCanonicalCompileSnapshot) -> CollectedContextSources:
        return collector.collect(
            activation_subject=None,
            activation_text="",
            tool_surface=surface,
            canonical_facts=facts,
        )

    first_facts, plan = _approved_plan_compile_facts(
        disposition=(
            PlanApprovedMaterializationDisposition.MATERIALIZE_REFERENCED_BLOCK
        ),
        workflow_id="workflow:first",
        interaction_id="interaction:first",
        transition_digest="sha256:" + "4" * 64,
    )
    second_facts, _ = _approved_plan_compile_facts(
        disposition=(
            PlanApprovedMaterializationDisposition.MATERIALIZE_REFERENCED_BLOCK
        ),
        workflow_id="workflow:second",
        interaction_id="interaction:first",
        transition_digest="sha256:" + "5" * 64,
    )
    first_sources, second_sources = collect(first_facts), collect(second_facts)
    first_handoff = next(
        item
        for item in first_sources.candidates
        if item.source_kind is ContextSourceKind.PLAN_HANDOFF
    )
    second_handoff = next(
        item
        for item in second_sources.candidates
        if item.source_kind is ContextSourceKind.PLAN_HANDOFF
    )
    assert tuple(item.text for item in first_handoff.variants) == tuple(
        item.text for item in second_handoff.variants
    )
    assert first_handoff.source_semantic_fingerprint == (
        second_handoff.source_semantic_fingerprint
    )
    assert first_handoff.domain_semantic_fingerprint != (
        second_handoff.domain_semantic_fingerprint
    )

    compiler = StructuredModelInputCompiler()
    owner = HostProviderInputContinuityOwner(session_id="session:test")
    first_request = _prepared_request(
        first_facts.canonical_input,
        first_sources,
        canonical_facts=first_facts,
    )
    _first, first_view = _compile_and_install_append(
        compiler=compiler,
        owner=owner,
        request=first_request,
        dispatch_anchor=NoNewTriggerAnchor(
            predecessor_frontier_fingerprint=None
        ),
    )
    second_request = replace(
        _prepared_request(
            second_facts.canonical_input,
            second_sources,
            canonical_facts=second_facts,
        ),
        context_id="context:second-plan-workflow",
        model_call_index=2,
    )
    _second, second_view = _compile_and_install_append(
        compiler=compiler,
        owner=owner,
        request=second_request,
        dispatch_anchor=NoNewTriggerAnchor(
            predecessor_frontier_fingerprint=None
        ),
    )
    appended = second_view.messages[len(first_view.messages) :]
    observations = tuple(
        decode_runtime_observation(message)
        for message in appended
        if message.role is MessageRole.USER
        and message.content
        and "pulsara_runtime_observation" in message.content[0]
    )
    handoffs = tuple(
        item
        for item in observations
        if item.source_kind is ContextSourceKind.PLAN_HANDOFF
    )
    assert len(handoffs) == 1
    assert plan in handoffs[0].body


def test_round3_1_two_plan_revisions_in_one_epoch_have_distinct_occurrences(
    tmp_path: Path,
) -> None:
    snapshot = _snapshot(
        FrozenProviderInputItem(
            FrozenProviderInputItemKind.PLAN_CONTINUATION,
            "entry:1",
            1,
            "turn:test",
            "continue plan",
            input_origin=CanonicalInputOriginKind.PLAN_CONTINUATION,
        )
    )
    base = _canonical_facts(snapshot)

    def facts(revision: int, digest_digit: str) -> FrozenCanonicalCompileSnapshot:
        values = {
            "session_id": "session:test",
            "workspace_id": "workspace:test",
            "target_turn_id": "turn:test",
            "carrier_entry_id": "entry:1",
            "carrier_entry_sequence": 1,
            "workflow_id": "workflow:repeat-revision",
            "workflow_ordinal": 1,
            "workflow_revision_at_transition": revision,
            "interaction_id": "interaction:review",
            "handoff_kind": PlanHandoffKind.REVISION_REQUESTED,
            "workflow_status": PlanWorkflowStatus.ACTIVE,
            "resume_permission_mode": PermissionMode.ACCEPT_EDITS,
            "transition_semantic_digest": "sha256:" + digest_digit * 64,
        }
        provisional_handoff = FrozenPlanHandoffCompileFact.__new__(
            FrozenPlanHandoffCompileFact
        )
        for name, value in values.items():
            object.__setattr__(provisional_handoff, name, value)
        object.__setattr__(provisional_handoff, "fact_fingerprint", "")
        handoff = FrozenPlanHandoffCompileFact(
            **values,
            fact_fingerprint=plan_handoff_compile_fact_fingerprint(
                provisional_handoff
            ),
        )
        compiled_values = {
            "canonical_input": base.canonical_input,
            "context_binding_fact": base.context_binding_fact,
            "run_permission_snapshot": base.run_permission_snapshot,
            "plan_workflow_fact": None,
            "plan_handoff_fact": handoff,
            "approved_plan_materialization_fact": None,
        }
        provisional = FrozenCanonicalCompileSnapshot.__new__(
            FrozenCanonicalCompileSnapshot
        )
        for name, value in compiled_values.items():
            object.__setattr__(provisional, name, value)
        object.__setattr__(provisional, "canonical_read_cut_fingerprint", "")
        return FrozenCanonicalCompileSnapshot(
            **compiled_values,
            canonical_read_cut_fingerprint=canonical_compile_snapshot_fingerprint(
                provisional
            ),
        )

    collector = KernelContextSourceCollector(
        workspace_kind="project",
        workspace_root=tmp_path,
        terminal_cwd=_TerminalCwd(tmp_path),
        capability_composer=_Capability(),  # type: ignore[arg-type]
        base_system_prompt="BASE",
        display_timezone=timezone.utc,
        clock=lambda: datetime(2026, 8, 13, tzinfo=timezone.utc),
    )
    surface = (
        StructuredToolPort(object(), tool_names=())
        .snapshot_tool_surface(
            conversation_scope_kind=ModelInputScopeKind.ROOT,
            scope_subagent_task_id=None,
        )
        .model_surface
    )
    candidates = []
    for item in (facts(2, "6"), facts(3, "7")):
        collected = collector.collect(
            activation_subject=None,
            activation_text="",
            tool_surface=surface,
            canonical_facts=item,
        )
        candidates.append(
            next(
                candidate
                for candidate in collected.candidates
                if candidate.source_kind is ContextSourceKind.PLAN_HANDOFF
            )
        )
    assert tuple(item.text for item in candidates[0].variants) == tuple(
        item.text for item in candidates[1].variants
    )
    assert candidates[0].domain_semantic_fingerprint != (
        candidates[1].domain_semantic_fingerprint
    )


def test_round3_source_registry_is_exact_and_rejects_self_certified_wrong_trust() -> (
    None
):
    registry = ContextSourceRegistry()
    assert {registry.binding(kind).source_kind for kind in ContextSourceKind} == set(
        ContextSourceKind
    )
    with pytest.raises(ValueError, match="root instruction must use SYSTEM"):
        _sources(
            _candidate(
                ContextSourceKind.RUNTIME_ENVIRONMENT,
                ("runtime full", "runtime compact"),
                trust=ContextTrustClass.ROOT_INSTRUCTION,
            )
        )

    registry = ContextSourceRegistry().fingerprint
    empty = CollectedContextSources(
        (),
        (),
        registry,
        context_fingerprint(
            "collected-context-sources:v1",
            {
                "registry_fingerprint": registry,
                "candidates": (),
                "diagnostics": (),
                "absent": (),
            },
        ),
    )
    with pytest.raises(StructuredModelInputCompileError) as failure:
        StructuredModelInputCompiler().compile(_prepared_request(_snapshot(), empty))
    assert failure.value.kind is ModelInputCompileFailureKind.SOURCE_CONTRACT_INVALID


def test_round3_1_compiler_requires_exact_value_or_absent_for_every_source() -> None:
    valid = _sources()
    absent = tuple(
        item
        for item in valid.absent_facts
        if item.source_kind is not ContextSourceKind.CAPABILITY_CATALOG
    )
    collection = context_fingerprint(
        "collected-context-sources:v1",
        {
            "registry_fingerprint": valid.registry_fingerprint,
            "candidates": tuple(
                item.source_semantic_fingerprint for item in valid.candidates
            ),
            "diagnostics": (),
            "absent": tuple(
                (
                    item.source_kind.value,
                    item.lifecycle.value,
                    item.absence_kind.value,
                    item.domain_semantic_fingerprint,
                )
                for item in absent
            ),
        },
    )
    missing = CollectedContextSources(
        valid.candidates,
        (),
        valid.registry_fingerprint,
        collection,
        absent,
    )

    with pytest.raises(StructuredModelInputCompileError) as failure:
        StructuredModelInputCompiler().compile(
            _prepared_request(_snapshot(), missing)
        )
    assert failure.value.kind is ModelInputCompileFailureKind.SOURCE_CONTRACT_INVALID


def test_round3_1_compiler_rejects_self_certified_absent_policy() -> None:
    valid = _sources()
    absent = tuple(
        replace(item, trust_class=ContextTrustClass.AUTHORIZED_RUNTIME_GUIDANCE)
        if item.source_kind is ContextSourceKind.RUNTIME_CLOCK
        else item
        for item in valid.absent_facts
    )
    malformed = CollectedContextSources(
        valid.candidates,
        valid.diagnostics,
        valid.registry_fingerprint,
        valid.collection_fingerprint,
        absent,
    )

    with pytest.raises(StructuredModelInputCompileError) as failure:
        StructuredModelInputCompiler().compile(
            _prepared_request(_snapshot(), malformed)
        )
    assert failure.value.kind is ModelInputCompileFailureKind.SOURCE_CONTRACT_INVALID


def test_round3_source_identity_duplicate_and_variant_order_fail_closed() -> None:
    item = _candidate(ContextSourceKind.BASE_SYSTEM, ("base",))
    with pytest.raises(ValueError, match="duplicated"):
        _sources(item, item)
    full = item.variants[0]
    with pytest.raises(ValueError, match="duplicated or unordered"):
        replace(item, variants=(full, full))
    with pytest.raises(UnicodeEncodeError):
        _candidate(ContextSourceKind.BASE_SYSTEM, ("\ud800",))


def test_round3_source_variants_must_not_increase_exact_estimator_cost() -> None:
    environment = _candidate(
        ContextSourceKind.RUNTIME_ENVIRONMENT,
        ("x", "this compact variant is larger than full"),
    )
    with pytest.raises(StructuredModelInputCompileError) as failure:
        StructuredModelInputCompiler().compile(
            _prepared_request(_snapshot(), _sources(environment))
        )
    assert failure.value.kind is ModelInputCompileFailureKind.SOURCE_CONTRACT_INVALID


def test_round3_system_placement_is_independent_of_input_order() -> None:
    candidates = (
        _candidate(ContextSourceKind.ACTIVE_SKILL, ("ACTIVE",)),
        _candidate(
            ContextSourceKind.CAPABILITY_CATALOG,
            ("CATALOG FULL LONG", "CATALOG", "CAT"),
        ),
        _candidate(
            ContextSourceKind.RUNTIME_ENVIRONMENT,
            ("RUNTIME ENVIRONMENT FULL", "RUNTIME"),
        ),
        _candidate(ContextSourceKind.BASE_SYSTEM, ("BASE",)),
    )
    compiled = StructuredModelInputCompiler().compile(
        _prepared_request(_snapshot(_user("hello")), _sources(*candidates))
    )
    assert compiled.system_prompt == "BASE"
    assert compiled.messages[0].content == ("hello",)
    observations = tuple(
        decode_runtime_observation(message) for message in compiled.messages[1:]
    )
    assert tuple(item.source_kind for item in observations) == (
        ContextSourceKind.RUNTIME_ENVIRONMENT,
        ContextSourceKind.RUN_PERMISSION,
        ContextSourceKind.CAPABILITY_CATALOG,
        ContextSourceKind.ACTIVE_SKILL,
    )


def test_round3_optional_clock_degrades_before_required_sources() -> None:
    sources = _sources(
        _candidate(ContextSourceKind.BASE_SYSTEM, ("BASE",)),
        _candidate(
            ContextSourceKind.RUNTIME_ENVIRONMENT,
            ("runtime " * 20, "runtime"),
        ),
        _candidate(ContextSourceKind.RUNTIME_CLOCK, ("clock " * 100, "clock")),
    )
    snapshot = _snapshot(_user("hello"))
    full_request = _prepared_request(snapshot, sources)
    full = StructuredModelInputCompiler().compile(full_request)
    constrained = _prepared_request(
        snapshot, sources, budget=full.final_estimate.total_input_tokens - 1
    )
    compiled = StructuredModelInputCompiler().compile(constrained)
    decisions = {item.source_kind: item for item in compiled.source_decisions}
    assert decisions[ContextSourceKind.RUNTIME_CLOCK].selected_mode in {
        ContextRenderMode.COMPACT,
        None,
    }
    assert decisions[ContextSourceKind.RUNTIME_ENVIRONMENT].selected_mode is (
        ContextRenderMode.FULL
    )


def test_round3_must_keep_source_never_omits_and_fails_before_provider() -> None:
    request = _prepared_request(
        _snapshot(),
        _sources(_candidate(ContextSourceKind.BASE_SYSTEM, ("required " * 100,))),
        budget=4,
    )
    with pytest.raises(StructuredModelInputCompileError) as failure:
        StructuredModelInputCompiler().compile(request)
    assert (
        failure.value.kind
        is ModelInputCompileFailureKind.REQUIRED_CONTEXT_EXCEEDS_BUDGET
    )


def test_round3_active_skill_and_tool_schema_fail_with_closed_budget_kind() -> None:
    active = _candidate(ContextSourceKind.ACTIVE_SKILL, ("active " * 100,))
    with pytest.raises(StructuredModelInputCompileError) as failure:
        StructuredModelInputCompiler().compile(
            _prepared_request(_snapshot(), _sources(active), budget=4)
        )
    assert failure.value.kind is (
        ModelInputCompileFailureKind.REQUIRED_CONTEXT_EXCEEDS_BUDGET
    )

    with pytest.raises(StructuredModelInputCompileError) as failure:
        StructuredModelInputCompiler().compile(
            _prepared_request(
                _snapshot(),
                _sources(),
                budget=4,
                tool_names=("artifact_read",),
            )
        )
    assert failure.value.kind is ModelInputCompileFailureKind.TOOL_SCHEMA_EXCEEDS_BUDGET


def test_round3_assistant_semantic_text_never_uses_parent_manifest() -> None:
    mutable_arguments = {"nested": {"value": 1}}
    frozen = freeze_json(mutable_arguments)
    call = ProviderToolCall("call:1", "terminal", frozen)  # type: ignore[arg-type]
    tool_only = FrozenProviderInputItem(
        FrozenProviderInputItemKind.ASSISTANT_TOOL_REQUEST,
        "entry:1",
        1,
        "turn:test",
        "",
        tool_calls=(call,),
    )
    lowered = lower_canonical_item(
        tool_only,
        artifact_read_available=False,
        limits=StructuredModelInputLimits(),
    )
    assert lowered.fixed_message is not None
    assert lowered.fixed_message.role is MessageRole.ASSISTANT
    assert lowered.fixed_message.content == ()
    assert lowered.fixed_message.tool_calls[0].arguments == '{"nested":{"value":1}}'
    mutable_arguments["nested"]["value"] = 2  # type: ignore[index]
    assert lowered.fixed_message.tool_calls[0].arguments == '{"nested":{"value":1}}'

    mixed = replace(tool_only, text="semantic assistant text")
    mixed_lowered = lower_canonical_item(
        mixed,
        artifact_read_available=False,
        limits=StructuredModelInputLimits(),
    )
    assert mixed_lowered.fixed_message is not None
    assert mixed_lowered.fixed_message.content == ("semantic assistant text",)
    assert "draft_identity" not in mixed_lowered.fixed_message.content[0]


def test_round3_tool_result_variants_are_typed_utf8_safe_and_surface_aware() -> None:
    item = _tool_result("🙂" * 10_000, sequence=2, turn_id="turn:test")
    without_read = lower_canonical_item(
        item,
        artifact_read_available=False,
        limits=StructuredModelInputLimits(),
    )
    assert tuple(variant.mode for variant in without_read.tool_result_variants) == (
        ToolResultProviderRenderMode.FULL,
        ToolResultProviderRenderMode.COMPACT,
        ToolResultProviderRenderMode.OMITTED_BODY,
    )
    compact = without_read.tool_result_variants[1]
    assert (
        compact.utf8_bytes
        <= StructuredModelInputLimits().maximum_tool_result_compact_bytes
    )
    "".join(compact.message.content).encode("utf-8").decode("utf-8")
    assert "omitted_utf8_bytes" in compact.message.content[0]
    assert "omitted_characters" in compact.message.content[0]
    assert "Use artifact_read" not in compact.message.content[0]

    with_read = lower_canonical_item(
        item,
        artifact_read_available=True,
        limits=StructuredModelInputLimits(),
    )
    assert ToolResultProviderRenderMode.REF_ONLY in {
        variant.mode for variant in with_read.tool_result_variants
    }
    with_read_compact = next(
        variant
        for variant in with_read.tool_result_variants
        if variant.mode is ToolResultProviderRenderMode.COMPACT
    )
    assert "Use artifact_read" in with_read_compact.message.content[0]


def test_round3_tool_result_bounds_cover_final_late_outcome_carrier() -> None:
    body = ('"\\🙂' * 8_000) + "forged [PULSARA_TOOL_RESULT_REFERENCE]"
    ordinary = _tool_result(body, sequence=2, turn_id="turn:test")
    late = replace(
        ordinary,
        item_kind=FrozenProviderInputItemKind.LATE_TOOL_OUTCOME,
        text="storage carrier is not used for lowering",
        tool_call_id="call:" + ("x" * 256),
    )
    lowered = lower_canonical_item(
        late,
        artifact_read_available=True,
        limits=StructuredModelInputLimits(),
    )
    by_mode = {variant.mode: variant for variant in lowered.tool_result_variants}
    assert (
        by_mode[ToolResultProviderRenderMode.COMPACT].utf8_bytes
        <= StructuredModelInputLimits().maximum_tool_result_compact_bytes
    )
    assert (
        by_mode[ToolResultProviderRenderMode.REF_ONLY].utf8_bytes
        <= StructuredModelInputLimits().maximum_tool_result_ref_only_bytes
    )
    assert "forged" not in "".join(
        by_mode[ToolResultProviderRenderMode.REF_ONLY].message.content
    )


def test_round3_retained_snapshot_reference_keeps_typed_warning() -> None:
    metadata = ProviderToolResultContextMetadata(
        result_state="SUCCESS",
        display_kind=ToolResultDisplayKind.HEAD_TAIL,
        artifact_disposition=ToolOutputArtifactDisposition.INCOMPLETE,
        artifact_id="artifact:retained",
        source_coverage=ToolOutputSourceCoverage.RETAINED_SNAPSHOT,
        source_coverage_reason=ToolOutputSourceCoverageReason.TERMINAL_RETENTION_GAP,
        artifact_unavailability_reason=None,
    )
    item = FrozenProviderInputItem(
        FrozenProviderInputItemKind.TOOL_RESULT,
        "entry:retained",
        2,
        "turn:test",
        "preview",
        tool_call_id="call:retained",
        tool_result_context=metadata,
        tool_result_body_text="preview",
    )
    lowered = lower_canonical_item(
        item,
        artifact_read_available=True,
        limits=StructuredModelInputLimits(),
    )
    ref = next(
        variant
        for variant in lowered.tool_result_variants
        if variant.mode is ToolResultProviderRenderMode.REF_ONLY
    )
    assert "retained snapshot" in "".join(ref.message.content)

    unavailable = replace(
        metadata,
        artifact_disposition=ToolOutputArtifactDisposition.UNAVAILABLE,
        artifact_id=None,
        artifact_unavailability_reason=(
            ToolOutputArtifactUnavailabilityReason.BLOB_PUBLICATION_FAILED
        ),
    )
    unavailable_item = replace(
        item,
        tool_result_context=unavailable,
    )
    no_ref = lower_canonical_item(
        unavailable_item,
        artifact_read_available=True,
        limits=StructuredModelInputLimits(),
    )
    assert ToolResultProviderRenderMode.REF_ONLY not in {
        variant.mode for variant in no_ref.tool_result_variants
    }


def test_round3_prior_turn_tool_result_degrades_before_current_turn() -> None:
    snapshot = _snapshot(
        _tool_result("old " * 2_000, sequence=1, turn_id="turn:old"),
        _tool_result("new " * 2_000, sequence=2, turn_id="turn:test"),
    )
    sources = _sources(_candidate(ContextSourceKind.BASE_SYSTEM, ("BASE",)))
    full = StructuredModelInputCompiler().compile(
        _prepared_request(snapshot, sources, tool_names=("artifact_read",))
    )
    compiled = StructuredModelInputCompiler().compile(
        _prepared_request(
            snapshot,
            sources,
            budget=full.final_estimate.total_input_tokens - 1,
            tool_names=("artifact_read",),
        )
    )
    assert compiled.tool_result_decisions[0].current_turn is False
    assert compiled.tool_result_decisions[0].selected_mode is not (
        ToolResultProviderRenderMode.FULL
    )
    assert compiled.tool_result_decisions[1].current_turn is True
    assert compiled.tool_result_decisions[1].selected_mode is (
        ToolResultProviderRenderMode.FULL
    )


def test_round3_aggregate_variant_and_total_working_set_exact_boundaries() -> None:
    snapshot = _snapshot(canonical_utf8_bytes=100)
    request = _prepared_request(snapshot, _sources())
    candidates = request.sources.candidates
    expected_working_bytes = (
        snapshot.canonical_utf8_bytes
        + sum(
            variant.utf8_bytes
            for candidate in candidates
            for variant in candidate.variants
        )
        + sum(
            _message_logical_utf8_bytes(source_variant_message(candidate, variant.text))
            - variant.utf8_bytes
            for candidate in candidates
            if candidate.channel is ContextChannel.RUNTIME_OBSERVATION
            for variant in candidate.variants
        )
        + max(
            0,
            sum(candidate.channel is ContextChannel.SYSTEM for candidate in candidates)
            - 1,
        )
        * len("\n\n".encode())
    )
    exact = StructuredModelInputLimits(
        maximum_compile_working_set_bytes=expected_working_bytes
    )
    StructuredModelInputCompiler(limits=exact).compile(request)
    too_small = replace(
        exact, maximum_compile_working_set_bytes=expected_working_bytes - 1
    )
    with pytest.raises(StructuredModelInputCompileError) as failure:
        StructuredModelInputCompiler(limits=too_small).compile(request)
    assert (
        failure.value.kind is ModelInputCompileFailureKind.COMPILE_WORKING_SET_EXCEEDED
    )

    call = ProviderToolCall(
        "call:large",
        "terminal",
        freeze_json({"command": "x" * 256}),
    )
    tool_request = FrozenProviderInputItem(
        FrozenProviderInputItemKind.ASSISTANT_TOOL_REQUEST,
        "entry:tool",
        1,
        "turn:test",
        "",
        tool_calls=(call,),
    )
    argument_bound = replace(
        StructuredModelInputLimits(), maximum_compile_working_set_bytes=128
    )
    with pytest.raises(StructuredModelInputCompileError) as failure:
        StructuredModelInputCompiler(limits=argument_bound).compile(
            _prepared_request(
                _snapshot(tool_request, canonical_utf8_bytes=0),
                _sources(),
            )
        )
    assert (
        failure.value.kind is ModelInputCompileFailureKind.COMPILE_WORKING_SET_EXCEEDED
    )


def test_round3_nonprogress_variant_is_bounded_and_then_omitted() -> None:
    clock = _candidate(ContextSourceKind.RUNTIME_CLOCK, ("aaaa", "bbbb"))
    full_request = _prepared_request(_snapshot(), _sources(clock))
    full = StructuredModelInputCompiler().compile(full_request)
    compiled = StructuredModelInputCompiler().compile(
        _prepared_request(
            _snapshot(),
            _sources(clock),
            budget=full.final_estimate.total_input_tokens - 1,
        )
    )
    decision = next(
        item
        for item in compiled.source_decisions
        if item.source_kind is ContextSourceKind.RUNTIME_CLOCK
    )
    assert decision.included is False
    assert ContextPublicDiagnosticCode.SOURCE_VARIANT_NON_PROGRESS in (
        compiled.diagnostic_codes
    )


def test_round3_catalog_walks_full_compact_reference_then_omitted() -> None:
    catalog = _candidate(
        ContextSourceKind.CAPABILITY_CATALOG,
        ("FULL " * 800, "COMPACT " * 180, "REF " * 20),
    )

    def compile_at(budget: int):
        return StructuredModelInputCompiler().compile(
            _prepared_request(
                _snapshot(_user("hello")), _sources(catalog), budget=budget
            )
        )

    full = compile_at(100_000)
    decisions = []
    current = full
    for _ in range(4):
        decision = next(
            item
            for item in current.source_decisions
            if item.source_kind is ContextSourceKind.CAPABILITY_CATALOG
        )
        decisions.append(decision.selected_mode)
        if decision.selected_mode is None:
            break
        current = compile_at(current.final_estimate.total_input_tokens - 1)
    assert decisions == [
        ContextRenderMode.FULL,
        ContextRenderMode.COMPACT,
        ContextRenderMode.REF_ONLY,
        None,
    ]


def test_round3_aggregate_source_variant_exact_boundaries() -> None:
    environment = _candidate(ContextSourceKind.RUNTIME_ENVIRONMENT, ("12345", "123"))
    source_request = _prepared_request(_snapshot(), _sources(environment))
    full_bytes = sum(
        candidate.variants[0].utf8_bytes
        for candidate in source_request.sources.candidates
    )
    all_bytes = sum(
        variant.utf8_bytes
        for candidate in source_request.sources.candidates
        for variant in candidate.variants
    )
    aggregate_exact = replace(
        StructuredModelInputLimits(),
        maximum_aggregate_full_source_bytes=full_bytes,
        maximum_aggregate_source_variant_bytes=all_bytes,
    )
    StructuredModelInputCompiler(limits=aggregate_exact).compile(source_request)
    aggregate_too_small = replace(
        aggregate_exact, maximum_aggregate_source_variant_bytes=all_bytes - 1
    )
    with pytest.raises(StructuredModelInputCompileError) as failure:
        StructuredModelInputCompiler(limits=aggregate_too_small).compile(source_request)
    assert (
        failure.value.kind is ModelInputCompileFailureKind.COMPILE_WORKING_SET_EXCEEDED
    )


def test_round3_single_source_variant_exact_boundary() -> None:
    environment = _candidate(ContextSourceKind.RUNTIME_ENVIRONMENT, ("12345", "123"))
    request = _prepared_request(_snapshot(), _sources(environment))
    maximum_variant_bytes = max(
        variant.utf8_bytes
        for candidate in request.sources.candidates
        for variant in candidate.variants
    )
    exact = replace(
        StructuredModelInputLimits(),
        maximum_single_source_variant_bytes=maximum_variant_bytes,
    )
    StructuredModelInputCompiler(limits=exact).compile(request)
    with pytest.raises(StructuredModelInputCompileError) as failure:
        StructuredModelInputCompiler(
            limits=replace(
                exact,
                maximum_single_source_variant_bytes=maximum_variant_bytes - 1,
            )
        ).compile(request)
    assert (
        failure.value.kind
        is ModelInputCompileFailureKind.SOURCE_PHYSICAL_BOUND_EXCEEDED
    )


def test_round3_tool_schema_canonical_bytes_exact_boundary() -> None:
    request = _prepared_request(
        _snapshot(),
        _sources(),
        tool_names=("artifact_read", "terminal"),
    )
    canonical_bytes = sum(
        len(tool.canonical_bytes)
        for tool in request.compile_binding.tool_surface.tool_specs
    )
    exact = replace(
        StructuredModelInputLimits(),
        maximum_tool_spec_canonical_bytes=canonical_bytes,
    )
    StructuredModelInputCompiler(limits=exact).compile(request)
    with pytest.raises(StructuredModelInputCompileError) as failure:
        StructuredModelInputCompiler(
            limits=replace(
                exact,
                maximum_tool_spec_canonical_bytes=canonical_bytes - 1,
            )
        ).compile(request)
    assert failure.value.kind is ModelInputCompileFailureKind.TOOL_SURFACE_INVALID


class _CountingEstimator:
    def __init__(self, delegate: ModelInputTokenEstimator) -> None:
        self._delegate = delegate
        self.fact = delegate.fact
        self.full_calls = 0
        self.message_calls = 0

    def estimate_text(self, text: str) -> int:
        return self._delegate.estimate_text(text)

    def estimate_message(self, message):
        self.message_calls += 1
        return self._delegate.estimate_message(message)

    def estimate_frozen_tool_spec(self, tool):
        return self._delegate.estimate_frozen_tool_spec(tool)

    def estimate_frozen_input(self, **kwargs):
        self.full_calls += 1
        return self._delegate.estimate_frozen_input(**kwargs)


class _SlowCooperativeEstimator(_CountingEstimator):
    def estimate_frozen_input_cooperative(self, *, checkpoint, **kwargs):
        for _ in range(1_000):
            sleep(0.001)
            checkpoint()
        return self._delegate.estimate_frozen_input(**kwargs)


def test_round3_4096_item_allocation_does_not_full_reestimate_per_item() -> None:
    items = tuple(_user("x", sequence=index + 1) for index in range(4_096))
    request = _prepared_request(_snapshot(*items), _sources())
    counting = _CountingEstimator(request.compile_binding.estimator)
    request = replace(
        request,
        compile_binding=replace(request.compile_binding, estimator=counting),
    )
    compiled = StructuredModelInputCompiler().compile(request)
    assert compiled.final_estimate.total_input_tokens > 0
    assert counting.full_calls <= 4
    # Constant first-party runtime-observation carriers add a bounded number
    # of estimates; transcript growth must remain linear, never per-item full
    # re-estimation.
    assert counting.message_calls <= 4_096 + 16


def test_round3_compiler_rejects_expired_deadline_before_allocation() -> None:
    request = _prepared_request(_snapshot(_user("deadline")), _sources())
    compiler = StructuredModelInputCompiler()
    with pytest.raises(StructuredModelInputCompileError) as failure:
        compiler.compile(request, deadline_monotonic=monotonic() - 1)
    assert failure.value.kind is ModelInputCompileFailureKind.DEADLINE_EXPIRED

    owner = HostProviderInputContinuityOwner(session_id="session:test")
    scope = ProviderInputContinuityScope(
        session_id="session:test",
        scope_kind=ModelInputScopeKind.ROOT,
        scope_subagent_task_id=None,
    )
    planning = owner.freeze_planning_input(
        scope=scope,
        canonical_frontier=_append_frontier(request),
        dispatch_anchor=_append_anchor(request),
    )
    with pytest.raises(StructuredModelInputCompileError) as append_failure:
        compiler.compile_append(
            request,
            planning=planning,
            compatibility=_append_compatibility(request),
            deadline_monotonic=monotonic() - 1,
        )
    assert append_failure.value.kind is ModelInputCompileFailureKind.DEADLINE_EXPIRED


def test_round3_compiler_deadline_physically_exits_before_io_close() -> None:
    async def exercise() -> None:
        request = _prepared_request(_snapshot(_user("deadline")), _sources())
        slow = _SlowCooperativeEstimator(request.compile_binding.estimator)
        request = replace(
            request,
            compile_binding=replace(request.compile_binding, estimator=slow),
        )
        io_owner = KernelSessionIO()
        started = monotonic()
        deadline = started + 0.03
        with pytest.raises((TimeoutError, StructuredModelInputCompileError)):
            await io_owner.run(
                StructuredModelInputCompiler().compile,
                request,
                deadline_monotonic=deadline,
            )
        assert monotonic() - started < 0.5
        await io_owner.aclose(deadline_monotonic=monotonic() + 0.2)

    asyncio.run(exercise())


def test_round3_4096_tool_result_degradation_uses_bounded_heap_work() -> None:
    items = tuple(
        _tool_result("x" * 1_024, sequence=index + 1, turn_id="turn:old")
        for index in range(4_096)
    )
    request = _prepared_request(
        _snapshot(*items),
        _sources(),
        budget=128,
        tool_names=("artifact_read",),
    )
    counting = _CountingEstimator(request.compile_binding.estimator)
    request = replace(
        request,
        compile_binding=replace(request.compile_binding, estimator=counting),
    )
    with pytest.raises(StructuredModelInputCompileError) as failure:
        StructuredModelInputCompiler().compile(request)
    assert failure.value.kind is (
        ModelInputCompileFailureKind.PROTECTED_TRANSCRIPT_EXCEEDS_BUDGET
    )
    assert counting.full_calls <= 8
    assert counting.message_calls < 50_000


class _TerminalCwd:
    def __init__(self, value: Path) -> None:
        self.value = value
        self.calls = 0

    def snapshot_terminal_cwd(self) -> Path:
        self.calls += 1
        return self.value


class _Capability:
    def __init__(self) -> None:
        self.inputs: list[tuple[str, frozenset[str]]] = []

    def resolve_projection(self, *, user_input: str, available_tool_names):
        self.inputs.append((user_input, available_tool_names))
        return CapabilityProjectionOutput()

    def freeze_projection_input(self, *, available_tool_names):
        return SimpleNamespace(
            available_tool_names=available_tool_names,
            snapshot_fingerprint=context_fingerprint(
                "test:frozen-capability-input:v1",
                {"tools": tuple(sorted(available_tool_names))},
            ),
        )

    def resolve_projection_from_frozen(self, frozen, *, user_input: str):
        return self.resolve_projection(
            user_input=user_input,
            available_tool_names=frozen.available_tool_names,
        )


class _SensitiveCapability(_Capability):
    def resolve_projection(self, *, user_input: str, available_tool_names):
        self.inputs.append((user_input, available_tool_names))
        return CapabilityProjectionOutput(
            catalog_entries=(
                ResolvedSkillCatalogEntry(
                    name="demo",
                    description="Demo capability",
                    location="private/path/SKILL.md",
                ),
            ),
            diagnostics=(
                CapabilityDiagnostic(
                    severity="error",
                    code="unknown_private_failure",
                    message="secret diagnostic detail",
                    path=Path("/private/skill/path"),
                ),
            ),
            catalog_prompt="CATALOG FULL",
            active_skill_prompt="ACTIVE FULL",
        )


class _LargeCatalogCapability(_Capability):
    def resolve_projection(self, *, user_input: str, available_tool_names):
        self.inputs.append((user_input, available_tool_names))
        entries = tuple(
            ResolvedSkillCatalogEntry(
                name=f"catalog-{index:02d}",
                description="descriptive context " * 30,
                location=f".agents/skills/catalog-{index:02d}/SKILL.md",
            )
            for index in range(40)
        )
        return CapabilityProjectionOutput(
            catalog_entries=entries,
            catalog_prompt="FULL-CATALOG\n" + ("catalog context " * 560),
        )


def test_round3_temporal_capture_is_single_and_dst_consistent(tmp_path: Path) -> None:
    clock_calls = 0

    def clock() -> datetime:
        nonlocal clock_calls
        clock_calls += 1
        return datetime(2024, 11, 3, 5, 30, tzinfo=timezone.utc)

    terminal = _TerminalCwd(tmp_path)
    capability = _Capability()
    collector = KernelContextSourceCollector(
        workspace_kind="project",
        workspace_root=tmp_path,
        terminal_cwd=terminal,
        capability_composer=capability,  # type: ignore[arg-type]
        base_system_prompt="BASE",
        display_timezone=ZoneInfo("America/New_York"),
        clock=clock,
    )
    surface = (
        StructuredToolPort(object(), tool_names=("terminal",))
        .snapshot_tool_surface(
            conversation_scope_kind=ModelInputScopeKind.ROOT,
            scope_subagent_task_id=None,
        )
        .model_surface
    )
    collected = collector.collect(
        activation_subject=CapabilityActivationSubjectKind.ROOT_HUMAN_PROMPT,
        activation_text="$skill demo",
        tool_surface=surface,
        canonical_facts=_canonical_facts(),
    )
    assert clock_calls == 1
    assert terminal.calls == 1
    assert capability.inputs == [("$skill demo", frozenset({"terminal"}))]
    by_kind = {candidate.source_kind: candidate for candidate in collected.candidates}
    environment = by_kind[ContextSourceKind.RUNTIME_ENVIRONMENT].variants[0].text
    clock_text = by_kind[ContextSourceKind.RUNTIME_CLOCK].variants[0].text
    assert "utc_offset_minutes=-240" in environment
    assert '"utc_offset_minutes":-240' in clock_text
    assert '"local_date":"2024-11-03"' in clock_text


class _MutableUnkeyedTimezone(tzinfo):
    def __init__(self, offset: timedelta) -> None:
        self.offset = offset

    def utcoffset(self, _value: datetime | None) -> timedelta:
        return self.offset

    def dst(self, _value: datetime | None) -> timedelta:
        return timedelta(0)

    def tzname(self, _value: datetime | None) -> str:
        return "dynamic-unkeyed"


def test_round3_unkeyed_timezone_is_frozen_to_opening_offset(tmp_path: Path) -> None:
    dynamic = _MutableUnkeyedTimezone(timedelta(hours=-4))
    collector = KernelContextSourceCollector(
        workspace_kind="project",
        workspace_root=tmp_path,
        terminal_cwd=_TerminalCwd(tmp_path),
        capability_composer=_Capability(),  # type: ignore[arg-type]
        base_system_prompt="BASE",
        display_timezone=dynamic,
        clock=lambda: datetime(2026, 12, 1, 12, tzinfo=timezone.utc),
    )
    # A no-key timezone may change its rules after Host open.  The collector
    # must keep both the display name and the exact opening offset together.
    dynamic.offset = timedelta(hours=-5)
    surface = (
        StructuredToolPort(object(), tool_names=())
        .snapshot_tool_surface(
            conversation_scope_kind=ModelInputScopeKind.ROOT,
            scope_subagent_task_id=None,
        )
        .model_surface
    )
    collected = collector.collect(
        activation_subject=CapabilityActivationSubjectKind.ROOT_HUMAN_PROMPT,
        activation_text="hello",
        tool_surface=surface,
        canonical_facts=_canonical_facts(),
    )
    by_kind = {candidate.source_kind: candidate for candidate in collected.candidates}
    environment = by_kind[ContextSourceKind.RUNTIME_ENVIRONMENT].variants[0].text
    clock_text = by_kind[ContextSourceKind.RUNTIME_CLOCK].variants[0].text
    assert 'timezone="UTC-04:00"' in environment
    assert "utc_offset_minutes=-240" in environment
    assert '"timezone":"UTC-04:00"' in clock_text
    assert '"utc_offset_minutes":-240' in clock_text


def test_round3_temporal_failure_samples_once_and_omits_clock(tmp_path: Path) -> None:
    calls = 0

    def broken_clock() -> datetime:
        nonlocal calls
        calls += 1
        raise RuntimeError("private clock detail")

    collector = KernelContextSourceCollector(
        workspace_kind="project",
        workspace_root=tmp_path,
        terminal_cwd=_TerminalCwd(tmp_path),
        capability_composer=_Capability(),  # type: ignore[arg-type]
        base_system_prompt="BASE",
        display_timezone=timezone.utc,
        clock=broken_clock,
    )
    surface = (
        StructuredToolPort(object(), tool_names=())
        .snapshot_tool_surface(
            conversation_scope_kind=ModelInputScopeKind.ROOT,
            scope_subagent_task_id=None,
        )
        .model_surface
    )
    collected = collector.collect(
        activation_subject=CapabilityActivationSubjectKind.ROOT_HUMAN_PROMPT,
        activation_text="hello",
        tool_surface=surface,
        canonical_facts=_canonical_facts(),
    )
    assert calls == 1
    assert ContextSourceKind.RUNTIME_CLOCK not in {
        candidate.source_kind for candidate in collected.candidates
    }
    assert collected.diagnostics[0].code is (
        ContextPublicDiagnosticCode.RUNTIME_CLOCK_UNAVAILABLE
    )
    assert not hasattr(collected.diagnostics[0], "message")
    environment = next(
        candidate
        for candidate in collected.candidates
        if candidate.source_kind is ContextSourceKind.RUNTIME_ENVIRONMENT
    )
    assert "utc_offset_minutes=null" in environment.variants[0].text


def test_round3_capability_sources_and_public_diagnostics_are_separate(
    tmp_path: Path,
) -> None:
    capability = _SensitiveCapability()
    collector = KernelContextSourceCollector(
        workspace_kind="transient",
        workspace_root=tmp_path,
        terminal_cwd=_TerminalCwd(tmp_path),
        capability_composer=capability,  # type: ignore[arg-type]
        base_system_prompt="BASE",
        display_timezone=timezone.utc,
        clock=lambda: datetime(2026, 8, 12, tzinfo=timezone.utc),
    )
    surface = (
        StructuredToolPort(object(), tool_names=("read_file",))
        .snapshot_tool_surface(
            conversation_scope_kind=ModelInputScopeKind.ROOT,
            scope_subagent_task_id=None,
        )
        .model_surface
    )
    collected = collector.collect(
        activation_subject=CapabilityActivationSubjectKind.ROOT_HUMAN_PROMPT,
        activation_text="skill:demo",
        tool_surface=surface,
        canonical_facts=_canonical_facts(),
    )
    by_kind = {candidate.source_kind: candidate for candidate in collected.candidates}
    assert by_kind[ContextSourceKind.CAPABILITY_CATALOG].variants[0].text == (
        "CATALOG FULL"
    )
    assert by_kind[ContextSourceKind.ACTIVE_SKILL].variants[0].text == "ACTIVE FULL"
    assert collected.registry_fingerprint == collector.registry_fingerprint
    assert collected.diagnostics[0].code is (
        ContextPublicDiagnosticCode.CAPABILITY_DISCOVERY_INCOMPLETE
    )
    assert not hasattr(collected.diagnostics[0], "message")
    assert "/private/skill/path" not in repr(collected.diagnostics)


def test_round3_large_catalog_renderer_never_inverts_declared_variants(
    tmp_path: Path,
) -> None:
    collector = KernelContextSourceCollector(
        workspace_kind="project",
        workspace_root=tmp_path,
        terminal_cwd=_TerminalCwd(tmp_path),
        capability_composer=_LargeCatalogCapability(),  # type: ignore[arg-type]
        base_system_prompt="BASE",
        display_timezone=timezone.utc,
        clock=lambda: datetime(2026, 8, 12, tzinfo=timezone.utc),
    )
    surface = (
        StructuredToolPort(object(), tool_names=())
        .snapshot_tool_surface(
            conversation_scope_kind=ModelInputScopeKind.ROOT,
            scope_subagent_task_id=None,
        )
        .model_surface
    )
    collected = collector.collect(
        activation_subject=CapabilityActivationSubjectKind.ROOT_HUMAN_PROMPT,
        activation_text="hello",
        tool_surface=surface,
        canonical_facts=_canonical_facts(),
    )
    request = _prepared_request(_snapshot(_user("hello")), collected)
    compiled = StructuredModelInputCompiler().compile(request)
    catalog = next(
        item
        for item in collected.candidates
        if item.source_kind is ContextSourceKind.CAPABILITY_CATALOG
    )
    costs = tuple(
        request.compile_binding.estimator.estimate_text(variant.text)
        for variant in catalog.variants
    )
    assert costs == tuple(sorted(costs, reverse=True))
    assert (
        next(
            item
            for item in compiled.source_decisions
            if item.source_kind is ContextSourceKind.CAPABILITY_CATALOG
        ).selected_mode
        is ContextRenderMode.FULL
    )


def test_round3_runtime_path_is_fixed_escaped_and_cannot_leave_workspace(
    tmp_path: Path,
) -> None:
    inside = tmp_path / "目录 with space\nand-newline"
    inside.mkdir()
    terminal = _TerminalCwd(inside)
    collector = KernelContextSourceCollector(
        workspace_kind="project",
        workspace_root=tmp_path,
        terminal_cwd=terminal,
        capability_composer=_Capability(),  # type: ignore[arg-type]
        base_system_prompt="BASE",
        display_timezone=timezone.utc,
        clock=lambda: datetime(2026, 8, 12, tzinfo=timezone.utc),
    )
    surface = (
        StructuredToolPort(object(), tool_names=())
        .snapshot_tool_surface(
            conversation_scope_kind=ModelInputScopeKind.ROOT,
            scope_subagent_task_id=None,
        )
        .model_surface
    )
    collected = collector.collect(
        activation_subject=CapabilityActivationSubjectKind.ROOT_HUMAN_PROMPT,
        activation_text="hello",
        tool_surface=surface,
        canonical_facts=_canonical_facts(),
    )
    environment = (
        next(
            candidate
            for candidate in collected.candidates
            if candidate.source_kind is ContextSourceKind.RUNTIME_ENVIRONMENT
        )
        .variants[0]
        .text
    )
    assert "\\n" in environment
    assert str(inside).split("\n", 1)[0] in environment

    terminal.value = tmp_path.parent
    with pytest.raises(ValueError, match="outside"):
        collector.collect(
            activation_subject=CapabilityActivationSubjectKind.ROOT_HUMAN_PROMPT,
            activation_text="hello",
            tool_surface=surface,
            canonical_facts=_canonical_facts(),
        )


def test_round3_runtime_source_tracks_foreground_cwd_but_not_yielded_cwd(
    tmp_path: Path,
) -> None:
    foreground = tmp_path / "foreground cwd"
    yielded = tmp_path / "yielded cwd"
    foreground.mkdir()
    yielded.mkdir()
    port = DirectKernelToolPort(
        workspace_root=tmp_path,
        host_owner_id="host:cwd",
        session_id="session:cwd",
        live_bus=LiveAgentEventBus(),
        authorization_policy=DefaultToolDispatchAuthorizationPolicy(),
    )
    try:
        session = port._terminal.get_or_create(  # noqa: SLF001
            owner_host_session_id="host:cwd"
        )
        completed = session.execute(
            TerminalRequest(
                command=f"cd {shlex.quote(str(foreground))}",
                yield_time_ms=2_000,
            ),
            decision_deadline_monotonic=port._deadlines.deadline(  # noqa: SLF001
                KernelWatchdogOwner.TERMINAL_FOREGROUND_DECISION
            ),
        )
        assert completed.status is TerminalStatus.SUCCESS
        assert port.snapshot_terminal_cwd() == foreground.resolve()
        background = session.execute(
            TerminalRequest(
                command=f"cd {shlex.quote(str(yielded))}; sleep 5",
                yield_time_ms=5,
                max_lifetime_seconds=10,
            ),
            decision_deadline_monotonic=port._deadlines.deadline(  # noqa: SLF001
                KernelWatchdogOwner.TERMINAL_FOREGROUND_DECISION
            ),
        )
        assert background.status is TerminalStatus.RUNNING
        assert port.snapshot_terminal_cwd() == foreground.resolve()

        collector = KernelContextSourceCollector(
            workspace_kind="project",
            workspace_root=tmp_path,
            terminal_cwd=port,
            capability_composer=_Capability(),  # type: ignore[arg-type]
            base_system_prompt="BASE",
            display_timezone=timezone.utc,
            clock=lambda: datetime(2026, 8, 12, tzinfo=timezone.utc),
        )
        collected = collector.collect(
            activation_subject=CapabilityActivationSubjectKind.ROOT_HUMAN_PROMPT,
            activation_text="hello",
            tool_surface=port.snapshot_tool_surface(
                conversation_scope_kind=ModelInputScopeKind.ROOT,
                scope_subagent_task_id=None,
            ).model_surface,
            canonical_facts=_canonical_facts(),
        )
        environment = next(
            item
            for item in collected.candidates
            if item.source_kind is ContextSourceKind.RUNTIME_ENVIRONMENT
        )
        assert json.dumps(str(foreground.resolve()), ensure_ascii=False) in (
            environment.variants[0].text
        )
        assert str(yielded.resolve()) not in environment.variants[0].text
    finally:
        asyncio.run(port.aclose(timeout_seconds=2))


def test_round3_tool_invocation_rejects_other_subagent_access() -> None:
    tools = StructuredToolPort(object(), tool_names=("read_file",))
    prepared = tools.snapshot_tool_surface(
        conversation_scope_kind=ModelInputScopeKind.SUBAGENT_TASK,
        scope_subagent_task_id="subagent-task:a",
    )
    borrow = tools.borrow_tool_surface(prepared)
    try:
        with pytest.raises(ValueError, match="scope access does not exact-join"):
            KernelToolInvocationContext(
                session_id="session:test",
                workspace_id="workspace:test",
                turn_id="turn:test",
                assistant_entry_id="entry:assistant",
                tool_call_id="call:test",
                attempt_id="attempt:test",
                result_entry_id="entry:result",
                conversation_scope_kind=ModelInputScopeKind.SUBAGENT_TASK.value,
                scope_subagent_task_id="subagent-task:b",
                host_owner_epoch=1,
                authorization_reference="authorization:test",
                permission_snapshot_fingerprint=(
                    _permission_snapshot().snapshot_fingerprint
                ),
                attempt_permission_snapshot_fingerprint=(
                    _permission_snapshot().snapshot_fingerprint
                ),
                tool_surface_fingerprint=prepared.model_surface.surface_fingerprint,
                executor_binding_fingerprint=borrow.binding_fingerprint("read_file"),
                surface_borrow=borrow,
            )
    finally:
        borrow.close()
def test_round3_tool_owner_rejects_foreign_host_surface_borrow(tmp_path: Path) -> None:
    async def scenario() -> None:
        ports = tuple(
            DirectKernelToolPort(
                workspace_root=tmp_path,
                host_owner_id=f"host:{name}",
                session_id="session:test",
                live_bus=LiveAgentEventBus(),
                authorization_policy=DefaultToolDispatchAuthorizationPolicy(),
            )
            for name in ("a", "b")
        )
        owner, foreign = ports
        owner_surface = owner.snapshot_tool_surface(
            conversation_scope_kind=ModelInputScopeKind.ROOT,
            scope_subagent_task_id=None,
        )
        foreign_surface = foreign.snapshot_tool_surface(
            conversation_scope_kind=ModelInputScopeKind.ROOT,
            scope_subagent_task_id=None,
        )
        assert owner_surface != foreign_surface
        borrow = foreign.borrow_tool_surface(foreign_surface)
        try:
            authorization = await owner.authorize(
                tool_name="read_file",
                arguments={"path": "README.md"},
                tool_call_id="call:test",
                turn_id="turn:test",
                assistant_entry_id="entry:assistant",
                surface_borrow=borrow,
                permission_snapshot=_permission_snapshot(),
            )
            assert authorization.kind is KernelToolAuthorizationKind.TOOL_UNAVAILABLE
            invocation = KernelToolInvocationContext(
                session_id="session:test",
                workspace_id="workspace:test",
                turn_id="turn:test",
                assistant_entry_id="entry:assistant",
                tool_call_id="call:test",
                attempt_id="attempt:test",
                result_entry_id="entry:result",
                conversation_scope_kind=ModelInputScopeKind.ROOT.value,
                scope_subagent_task_id=None,
                host_owner_epoch=1,
                authorization_reference="authorization:test",
                permission_snapshot_fingerprint=(
                    _permission_snapshot().snapshot_fingerprint
                ),
                attempt_permission_snapshot_fingerprint=(
                    _permission_snapshot().snapshot_fingerprint
                ),
                tool_surface_fingerprint=(
                    foreign_surface.model_surface.surface_fingerprint
                ),
                executor_binding_fingerprint=borrow.binding_fingerprint("read_file"),
                surface_borrow=borrow,
            )
            with pytest.raises(RuntimeError, match="surface borrow is not active"):
                await owner.invoke(
                    tool_name="read_file",
                    arguments={"path": "README.md"},
                    tool_call_id="call:test",
                    attempt_id="attempt:test",
                    turn_id="turn:test",
                    assistant_entry_id="entry:assistant",
                    invocation_context=invocation,
                )
        finally:
            borrow.close()
            await owner.aclose(timeout_seconds=2)
            await foreign.aclose(timeout_seconds=2)

    asyncio.run(scenario())


def test_round3_tool_surface_excludes_root_only_monitor_and_schema_is_frozen(
    tmp_path: Path,
) -> None:
    port = DirectKernelToolPort(
        workspace_root=tmp_path,
        host_owner_id="host:test",
        session_id="session:test",
        live_bus=LiveAgentEventBus(),
        authorization_policy=DefaultToolDispatchAuthorizationPolicy(),
    )
    root = port.snapshot_tool_surface(
        conversation_scope_kind=ModelInputScopeKind.ROOT,
        scope_subagent_task_id=None,
    )
    child = port.snapshot_tool_surface(
        conversation_scope_kind=ModelInputScopeKind.SUBAGENT_TASK,
        scope_subagent_task_id="task:test",
    )
    assert "terminal_monitor" in {tool.name for tool in root.model_surface.tool_specs}
    assert "terminal_monitor" not in {
        tool.name for tool in child.model_surface.tool_specs
    }
    assert port._terminal._sessions == {}  # noqa: SLF001
    assert port.snapshot_terminal_cwd() == tmp_path.resolve()
    assert port._terminal._sessions == {}  # noqa: SLF001
    terminal = next(
        tool for tool in root.model_surface.tool_specs if tool.name == "terminal"
    )
    with pytest.raises((AttributeError, TypeError)):
        terminal.parameters["type"] = "array"  # type: ignore[index]
    borrow = port.borrow_tool_surface(root)
    with pytest.raises(RuntimeError, match="active borrow"):
        port.bind_subagent_port(SimpleNamespace(tool_names=frozenset({"spawn_agent"})))
    borrow.close()
    asyncio.run(port.aclose(timeout_seconds=2))


def test_round3_tool_call_argument_owners_are_strictly_frozen() -> None:
    assert get_type_hints(CompletedToolCallBlock)["arguments"] is FrozenJsonObjectFact
    assert get_type_hints(AssistantToolCallBlock)["arguments"] is FrozenJsonObjectFact
    with pytest.raises(TypeError, match="recursively frozen"):
        CompletedToolCallBlock("block", "call", "terminal", {})  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="recursively frozen"):
        AssistantToolCallBlock("block", "call", "terminal", {})  # type: ignore[arg-type]


def test_round3_report_projection_is_bounded_and_contains_no_prompt() -> None:
    secret = "secret-user-prompt-never-project"
    items = tuple(
        _tool_result(f"{secret}:{index}", sequence=index + 1, turn_id="turn:test")
        for index in range(70)
    )
    compiled = StructuredModelInputCompiler().compile(
        _prepared_request(
            _snapshot(*items),
            _sources(_candidate(ContextSourceKind.BASE_SYSTEM, (secret,))),
            tool_names=("artifact_read",),
        )
    )
    assert ContextPublicDiagnosticCode.DECISION_SAMPLE_TRUNCATED in (
        compiled.diagnostic_codes
    )
    assert secret not in repr(compiled)

    offers: list[object] = []
    owner = SimpleNamespace(
        _extensions=SimpleNamespace(offer_operational_nowait=offers.append),
        _writer_lease=SimpleNamespace(guard=SimpleNamespace(session_id="session:test")),
    )
    ConversationKernelRunner._offer_compile_observation(
        owner,  # type: ignore[arg-type]
        turn_id="turn:test",
        model_call_index=1,
        compiled=compiled,
    )
    assert len(offers) == 1
    offer = offers[0]
    assert offer.event_type is OperationalHookType.MODEL_INPUT_COMPILE_OBSERVED
    assert offer.public_payload["decision_sample_count"] == 64
    assert offer.public_payload["decision_omitted_count"] == 9
    assert secret not in repr(offer.public_payload)

    failing_owner = SimpleNamespace(
        _extensions=SimpleNamespace(
            offer_operational_nowait=lambda _offer: (_ for _ in ()).throw(
                RuntimeError("isolated observer failure")
            )
        ),
        _writer_lease=owner._writer_lease,
    )
    ConversationKernelRunner._offer_compile_observation(
        failing_owner,  # type: ignore[arg-type]
        turn_id="turn:test",
        model_call_index=1,
        compiled=compiled,
    )


def test_round3_diagnostic_projector_is_the_bounded_public_owner() -> None:
    assert (
        get_type_hints(ModelInputCompileOperationalProjection)["diagnostic_codes"]
        == tuple[ContextPublicDiagnosticCode, ...]
    )
    compiled = StructuredModelInputCompiler().compile(
        _prepared_request(
            _snapshot(
                *(
                    _tool_result(
                        f"private-body-{index}", sequence=index + 1, turn_id="turn:test"
                    )
                    for index in range(70)
                )
            ),
            _sources(_candidate(ContextSourceKind.BASE_SYSTEM, ("private-system",))),
            tool_names=("artifact_read",),
        )
    )
    projection = project_model_input_compile_observation(
        model_call_index=1,
        compiled=compiled,
    )
    assert len(projection.decision_samples) == 64
    assert projection.decision_omitted_count == 9
    assert {item.decision_kind for item in projection.decision_samples} <= {
        CompileDecisionSampleKind.SOURCE,
        CompileDecisionSampleKind.TOOL_RESULT,
    }
    payload = projection.public_payload()
    assert "private-system" not in repr(payload)
    assert "private-body" not in repr(payload)
    assert not any(
        key in repr(payload).lower()
        for key in ("public_detail", "filesystem_path", "tool_arguments")
    )


def test_round3_compile_binding_cannot_exceed_or_rewrite_target_budgets() -> None:
    request = _prepared_request(
        _snapshot(_user("hello")),
        _sources(_candidate(ContextSourceKind.BASE_SYSTEM, ("base",))),
    )
    binding = request.compile_binding
    with pytest.raises(ValueError, match="input budget exceeds"):
        replace(
            binding,
            effective_input_budget_tokens=(
                binding.target_fact.context_budget.input_budget_tokens + 1
            ),
            binding_fingerprint=model_input_compile_binding_fingerprint(
                call_fact=binding.call_fact,
                target_fact=binding.target_fact,
                estimator_fingerprint=binding.estimator_fingerprint,
                effective_input_budget_tokens=(
                    binding.target_fact.context_budget.input_budget_tokens + 1
                ),
                effective_output_tokens=binding.effective_output_tokens,
                tool_surface=binding.tool_surface,
            ),
        )
    with pytest.raises(ValueError, match="output budget differs"):
        replace(
            binding,
            effective_output_tokens=binding.effective_output_tokens - 1,
            binding_fingerprint=model_input_compile_binding_fingerprint(
                call_fact=binding.call_fact,
                target_fact=binding.target_fact,
                estimator_fingerprint=binding.estimator_fingerprint,
                effective_input_budget_tokens=binding.effective_input_budget_tokens,
                effective_output_tokens=binding.effective_output_tokens - 1,
                tool_surface=binding.tool_surface,
            ),
        )


def test_round3_source_decision_and_compiled_fingerprints_are_golden() -> None:
    request = _prepared_request(
        _snapshot(_user("golden input")),
        _sources(
            _candidate(
                ContextSourceKind.RUNTIME_CLOCK,
                ("clock full", "clock"),
            )
        ),
        tool_names=("artifact_read",),
    )
    binding = request.compile_binding
    call_fact = binding.call_fact.model_copy(
        update={"resolved_model_call_id": "model_call:golden"}
    )
    request = replace(
        request,
        compile_binding=replace(
            binding,
            call_fact=call_fact,
            binding_fingerprint=model_input_compile_binding_fingerprint(
                call_fact=call_fact,
                target_fact=binding.target_fact,
                estimator_fingerprint=binding.estimator_fingerprint,
                effective_input_budget_tokens=binding.effective_input_budget_tokens,
                effective_output_tokens=binding.effective_output_tokens,
                tool_surface=binding.tool_surface,
            ),
        ),
    )
    compiled = StructuredModelInputCompiler().compile(request)
    assert compiled.source_collection_fingerprint == (
        "sha256:771d620c4b06950280b5cb0a35c124ac01bcc1ce38a265a3e1acaf12a889fc29"
    )
    assert compiled.budget_report.decision_digest == (
        "sha256:0c70198d1d2a90d1b8e4271d266561102f671daf01c5edc46a436973a4d70fa9"
    )
    assert compiled.compiled_semantic_fingerprint == (
        "sha256:e19ae6df5f20692859be0bcb3c9e2458c81d31ca17d1f28623741d7cfadf0661"
    )
    assert compiled.final_estimate.total_input_tokens == 248


def test_round3_1_compatible_epoch_appends_clock_without_rewriting_prefix() -> None:
    compiler = StructuredModelInputCompiler()
    owner = HostProviderInputContinuityOwner(session_id="session:test")
    initial = _user("first", sequence=1)
    first_request = _prepared_request(
        _snapshot(initial),
        _sources(_candidate(ContextSourceKind.RUNTIME_CLOCK, ("clock=A", "A"))),
    )
    _first, installed = _compile_and_install_append(
        compiler=compiler, owner=owner, request=first_request
    )

    assistant = FrozenProviderInputItem(
        FrozenProviderInputItemKind.ASSISTANT,
        "entry:2",
        2,
        "turn:test",
        "answer",
    )
    second_request = replace(
        _prepared_request(
            _snapshot(initial, assistant),
            _sources(_candidate(ContextSourceKind.RUNTIME_CLOCK, ("clock=B", "B"))),
        ),
        context_id="context:second",
        model_call_index=2,
    )
    second, successor = _compile_and_install_append(
        compiler=compiler, owner=owner, request=second_request
    )

    assert successor.system_prompt == installed.system_prompt
    assert successor.tools == installed.tools
    assert successor.messages[: len(installed.messages)] == installed.messages
    assert second.appended_message_count == len(successor.messages) - len(
        installed.messages
    )
    observations = [
        decode_runtime_observation(message)
        for message in successor.messages
        if message.role is MessageRole.USER
        and message.content
        and "pulsara_runtime_observation" in message.content[0]
    ]
    assert [
        item.body
        for item in observations
        if item.source_kind is ContextSourceKind.RUNTIME_CLOCK
    ] == [
        "clock=A",
        "clock=B",
    ]


def test_round3_1_active_skill_no_change_and_clear_are_causal_once() -> None:
    compiler = StructuredModelInputCompiler()
    owner = HostProviderInputContinuityOwner(session_id="session:test")
    initial = _user("$skill:alpha", sequence=1)
    first_request = _prepared_request(
        _snapshot(initial),
        _sources(_candidate(ContextSourceKind.ACTIVE_SKILL, ("skill=alpha",))),
    )
    _first, first_view = _compile_and_install_append(
        compiler=compiler, owner=owner, request=first_request
    )

    assistant = FrozenProviderInputItem(
        FrozenProviderInputItemKind.ASSISTANT,
        "entry:2",
        2,
        "turn:test",
        "tool loop result",
    )
    no_change_request = replace(
        _prepared_request(
            _snapshot(initial, assistant),
            _sources(
                absent_facts=(
                    _absent(
                        ContextSourceKind.ACTIVE_SKILL,
                        ContextSourceAbsenceKind.NOT_APPLICABLE,
                    ),
                )
            ),
        ),
        context_id="context:no-change",
        model_call_index=2,
    )
    _no_change, second_view = _compile_and_install_append(
        compiler=compiler, owner=owner, request=no_change_request
    )
    assert second_view.messages[: len(first_view.messages)] == first_view.messages
    assert not any(
        observation.source_kind is ContextSourceKind.ACTIVE_SKILL
        for observation in (
            decode_runtime_observation(message)
            for message in second_view.messages[len(first_view.messages) :]
            if message.role is MessageRole.USER
            and message.content
            and "pulsara_runtime_observation" in message.content[0]
        )
    )

    next_user = _user("ordinary follow-up", sequence=3)
    clear_request = replace(
        _prepared_request(
            _snapshot(initial, assistant, next_user),
            _sources(
                absent_facts=(
                    _absent(
                        ContextSourceKind.ACTIVE_SKILL,
                        ContextSourceAbsenceKind.EXPLICIT_EMPTY,
                    ),
                )
            ),
        ),
        context_id="context:clear",
        model_call_index=3,
    )
    _clear, third_view = _compile_and_install_append(
        compiler=compiler, owner=owner, request=clear_request
    )
    clear_count = sum(
        observation.source_kind is ContextSourceKind.ACTIVE_SKILL
        and observation.presence.value == "CLEARED"
        for observation in (
            decode_runtime_observation(message)
            for message in third_view.messages
            if message.role is MessageRole.USER
            and message.content
            and "pulsara_runtime_observation" in message.content[0]
        )
    )
    assert clear_count == 1

    final_assistant = FrozenProviderInputItem(
        FrozenProviderInputItemKind.ASSISTANT,
        "entry:4",
        4,
        "turn:test",
        "final",
    )
    repeated_clear_request = replace(
        _prepared_request(
            _snapshot(initial, assistant, next_user, final_assistant),
            _sources(
                absent_facts=(
                    _absent(
                        ContextSourceKind.ACTIVE_SKILL,
                        ContextSourceAbsenceKind.NOT_APPLICABLE,
                    ),
                )
            ),
        ),
        context_id="context:repeat-clear",
        model_call_index=4,
    )
    _repeat, fourth_view = _compile_and_install_append(
        compiler=compiler, owner=owner, request=repeated_clear_request
    )
    assert (
        sum(
            observation.source_kind is ContextSourceKind.ACTIVE_SKILL
            and observation.presence.value == "CLEARED"
            for observation in (
                decode_runtime_observation(message)
                for message in fourth_view.messages
                if message.role is MessageRole.USER
                and message.content
                and "pulsara_runtime_observation" in message.content[0]
            )
        )
        == 1
    )


@pytest.mark.parametrize(
    ("previous_presence", "current_presence", "semantic_changed", "expected_append"),
    tuple(
        (previous, current, False, previous != current)
        for previous in ("VALUE", "CLEARED", "UNAVAILABLE")
        for current in ("VALUE", "CLEARED", "UNAVAILABLE")
    )
    + tuple(
        (presence, presence, True, True)
        for presence in ("VALUE", "CLEARED", "UNAVAILABLE")
    ),
)
def test_round3_1_stateful_source_presence_matrix_is_exact(
    previous_presence: str,
    current_presence: str,
    semantic_changed: bool,
    expected_append: bool,
) -> None:
    """Cover the complete VALUE/CLEARED/UNAVAILABLE replacement matrix."""

    kind = ContextSourceKind.CAPABILITY_CATALOG

    def source_state(presence: str, semantic: str):
        if presence == "VALUE":
            return _sources(
                _candidate(
                    kind,
                    (
                        f"catalog={semantic}:" + "full-detail " * 20,
                        f"catalog={semantic}:compact",
                        f"ref={semantic}",
                    ),
                )
            )
        absence_kind = (
            ContextSourceAbsenceKind.EXPLICIT_EMPTY
            if presence == "CLEARED"
            else ContextSourceAbsenceKind.UNAVAILABLE
        )
        absence = _absent(kind, absence_kind)
        if semantic != "A":
            absence = replace(
                absence,
                domain_semantic_fingerprint=context_fingerprint(
                    "test:round3-1-source-absence:v1",
                    {
                        "kind": kind.value,
                        "presence": presence,
                        "semantic": semantic,
                    },
                ),
            )
        return _sources(absent_facts=(absence,))

    compiler = StructuredModelInputCompiler()
    owner = HostProviderInputContinuityOwner(session_id="session:test")
    items: list[FrozenProviderInputItem] = [_user("initial", sequence=1)]
    first = _prepared_request(
        _snapshot(*items),
        source_state("VALUE", "A"),
    )
    _compile_and_install_append(compiler=compiler, owner=owner, request=first)

    call_index = 2
    if previous_presence != "VALUE":
        items.append(
            FrozenProviderInputItem(
                FrozenProviderInputItemKind.ASSISTANT,
                f"entry:{call_index}",
                call_index,
                "turn:test",
                f"settle {previous_presence}",
            )
        )
        previous_request = replace(
            _prepared_request(_snapshot(*items), source_state(previous_presence, "A")),
            context_id=f"context:previous:{previous_presence}",
            model_call_index=call_index,
        )
        _compile_and_install_append(
            compiler=compiler, owner=owner, request=previous_request
        )
        call_index += 1

    before = owner.current_view(
        ProviderInputContinuityScope(
            session_id="session:test",
            scope_kind=ModelInputScopeKind.ROOT,
            scope_subagent_task_id=None,
        )
    )
    assert before is not None
    items.append(
        FrozenProviderInputItem(
            FrozenProviderInputItemKind.ASSISTANT,
            f"entry:{call_index}",
            call_index,
            "turn:test",
            "matrix transition",
        )
    )
    current_request = replace(
        _prepared_request(
            _snapshot(*items),
            source_state(current_presence, "B" if semantic_changed else "A"),
        ),
        context_id=(
            f"context:matrix:{previous_presence}:{current_presence}:{semantic_changed}"
        ),
        model_call_index=call_index,
    )
    _result, after = _compile_and_install_append(
        compiler=compiler, owner=owner, request=current_request
    )
    observations = tuple(
        decode_runtime_observation(message)
        for message in after.messages[len(before.messages) :]
        if message.role is MessageRole.USER
        and message.content
        and "pulsara_runtime_observation" in message.content[0]
    )
    matching = tuple(item for item in observations if item.source_kind is kind)
    assert len(matching) == int(expected_append)
    head = next(item for item in after.source_heads if item.source_kind is kind)
    assert head.presence.value == current_presence


def test_round3_1_compatible_epoch_rejects_old_canonical_rewrite() -> None:
    compiler = StructuredModelInputCompiler()
    owner = HostProviderInputContinuityOwner(session_id="session:test")
    first = _prepared_request(_snapshot(_user("original")), _sources())
    _compile_and_install_append(compiler=compiler, owner=owner, request=first)
    rewritten = _prepared_request(_snapshot(_user("rewritten")), _sources())

    scope = ProviderInputContinuityScope(
        session_id="session:test",
        scope_kind=ModelInputScopeKind.ROOT,
        scope_subagent_task_id=None,
    )
    planning = owner.freeze_planning_input(
        scope=scope,
        canonical_frontier=_append_frontier(rewritten),
        dispatch_anchor=_append_anchor(rewritten),
    )
    with pytest.raises(StructuredModelInputCompileError) as failure:
        compiler.compile_append(
            rewritten,
            planning=planning,
            compatibility=_append_compatibility(rewritten),
        )
    assert failure.value.kind is ModelInputCompileFailureKind.CANONICAL_PREFIX_CONFLICT


def test_round3_1_root_epoch_spans_turns_and_host_replacement_is_cold() -> None:
    compiler = StructuredModelInputCompiler()
    owner = HostProviderInputContinuityOwner(session_id="session:test")
    first_user = _user("turn one", sequence=1, turn_id="turn:one")
    first_request = _prepared_request(
        _snapshot(first_user, turn_id="turn:one"), _sources()
    )
    _first, first_view = _compile_and_install_append(
        compiler=compiler, owner=owner, request=first_request
    )

    answer = FrozenProviderInputItem(
        FrozenProviderInputItemKind.ASSISTANT,
        "entry:2",
        2,
        "turn:one",
        "answer one",
    )
    second_user = _user("turn two", sequence=3, turn_id="turn:two")
    second_request = replace(
        _prepared_request(
            _snapshot(first_user, answer, second_user, turn_id="turn:two"),
            _sources(),
        ),
        context_id="context:turn-two",
        model_call_index=2,
    )
    _second, second_view = _compile_and_install_append(
        compiler=compiler, owner=owner, request=second_request
    )
    assert second_view.epoch_nonce == first_view.epoch_nonce
    assert second_view.epoch_revision == first_view.epoch_revision + 1
    assert second_view.messages[: len(first_view.messages)] == first_view.messages

    replacement = HostProviderInputContinuityOwner(session_id="session:test")
    scope = ProviderInputContinuityScope(
        session_id="session:test",
        scope_kind=ModelInputScopeKind.ROOT,
        scope_subagent_task_id=None,
    )
    assert replacement.current_view(scope) is None
    planning = replacement.freeze_planning_input(
        scope=scope,
        canonical_frontier=_append_frontier(second_request),
        dispatch_anchor=_append_anchor(second_request),
    )
    assert planning.predecessor.value == "EMPTY"
    assert planning.predecessor_view is None


def test_round3_1_child_epochs_are_exactly_scoped_and_released() -> None:
    compiler = StructuredModelInputCompiler()
    owner = HostProviderInputContinuityOwner(
        session_id="session:test", maximum_child_scopes=2
    )

    def install_child(task_id: str, sequence: int):
        objective = _user(
            f"objective {task_id}",
            sequence=sequence,
            turn_id=f"turn:{task_id}",
            origin=CanonicalInputOriginKind.SUBAGENT_OBJECTIVE,
        )
        request = _prepared_request(
            _snapshot(
                objective,
                scope=ModelInputScopeKind.SUBAGENT_TASK,
                turn_id=f"turn:{task_id}",
                scope_subagent_task_id=task_id,
            ),
            _sources(),
        )
        return _compile_and_install_append(
            compiler=compiler, owner=owner, request=request
        )[1]

    first = install_child("task:a", 1)
    second = install_child("task:b", 2)
    assert first.epoch_revision == second.epoch_revision == 1
    assert first.scope != second.scope
    with pytest.raises(ProviderInputContinuityConflict, match="capacity"):
        scope = ProviderInputContinuityScope(
            session_id="session:test",
            scope_kind=ModelInputScopeKind.SUBAGENT_TASK,
            scope_subagent_task_id="task:c",
        )
        owner.freeze_planning_input(
            scope=scope,
            canonical_frontier=first.canonical_frontier,
            dispatch_anchor=NoNewTriggerAnchor(None),
        )

    owner.discard_scope(first.scope)
    third = install_child("task:c", 3)
    assert third.epoch_revision == 1
    assert owner.current_view(first.scope) is None

    owner.close()
    assert owner.current_view(second.scope) is None
    with pytest.raises(ProviderInputContinuityConflict, match="closed"):
        owner.freeze_planning_input(
            scope=second.scope,
            canonical_frontier=second.canonical_frontier,
            dispatch_anchor=NoNewTriggerAnchor(None),
        )
    with pytest.raises(ProviderInputContinuityConflict, match="another session"):
        HostProviderInputContinuityOwner(
            session_id="session:other"
        ).freeze_planning_input(
            scope=second.scope,
            canonical_frontier=second.canonical_frontier,
            dispatch_anchor=NoNewTriggerAnchor(None),
        )


def test_round3_1_compatibility_reset_starts_a_new_epoch_without_prefix_join() -> None:
    compiler = StructuredModelInputCompiler()
    owner = HostProviderInputContinuityOwner(session_id="session:test")
    initial = _user("initial", sequence=1)
    first = _prepared_request(_snapshot(initial), _sources())
    _first, first_view = _compile_and_install_append(
        compiler=compiler, owner=owner, request=first
    )

    changed_base = _candidate(ContextSourceKind.BASE_SYSTEM, ("BASE v2",))
    successor = replace(
        _prepared_request(
            _snapshot(initial, _user("next", sequence=2)),
            _sources(changed_base),
        ),
        context_id="context:reset",
        model_call_index=2,
    )
    result, reset_view = _compile_and_install_append(
        compiler=compiler, owner=owner, request=successor
    )
    assert result.reset_reason is ProviderInputEpochResetReason.BASE_SYSTEM_CHANGED
    assert reset_view.epoch_nonce != first_view.epoch_nonce
    assert reset_view.epoch_revision == first_view.epoch_revision + 1
    assert reset_view.system_prompt == "BASE v2"


def test_round3_1_append_quotes_canonical_item_and_snapshot_bounds_before_install() -> None:
    request = _prepared_request(
        _snapshot(_user("one", sequence=1), _user("two", sequence=2)),
        _sources(),
    )

    item_limited = StructuredModelInputCompiler(
        limits=replace(
            StructuredModelInputLimits(),
            maximum_canonical_input_items=1,
        )
    )
    with pytest.raises(StructuredModelInputCompileError) as failure:
        item_limited.compile(request)
    assert failure.value.kind is ModelInputCompileFailureKind.COMPILE_WORKING_SET_EXCEEDED

    byte_limited = StructuredModelInputCompiler(
        limits=replace(
            StructuredModelInputLimits(),
            maximum_canonical_input_bytes=request.canonical_input.canonical_utf8_bytes
            - 1,
        )
    )
    with pytest.raises(StructuredModelInputCompileError) as failure:
        byte_limited.compile(request)
    assert failure.value.kind is ModelInputCompileFailureKind.COMPILE_WORKING_SET_EXCEEDED


def test_round3_pure_compiler_import_graph_has_no_kernel_transport_or_io() -> None:
    package = Path(__file__).parents[1] / "src/pulsara_agent/model_input"
    forbidden_modules = (
        "conversation_kernel",
        "postgres",
        "normalized_transport",
        "provider_stream",
        "event_log",
    )
    for path in package.glob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        imports = [
            node.module or ""
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom)
        ]
        imports.extend(
            alias.name
            for node in ast.walk(tree)
            if isinstance(node, ast.Import)
            for alias in node.names
        )
        assert not any(
            forbidden in module for module in imports for forbidden in forbidden_modules
        ), (path, imports)

    compiler_source = (package / "compiler.py").read_text(encoding="utf-8")
    assert "ResolvedModelCall" not in compiler_source
    assert "LLMContext" not in compiler_source
    assert "transport" not in compiler_source.lower()
    for carrier in (
        StructuredModelInputCompileRequest,
        ContextSourceCandidate,
        FrozenProviderInputItem,
    ):
        annotations = tuple(str(field.type) for field in fields(carrier))
        assert not any(
            "dict[" in annotation or "list[" in annotation for annotation in annotations
        )

    kernel = package.parent / "conversation_kernel"
    direct_tree = ast.parse((kernel / "direct_model.py").read_text(encoding="utf-8"))
    direct_imports = {
        node.module or ""
        for node in ast.walk(direct_tree)
        if isinstance(node, ast.ImportFrom)
    }
    assert not any(
        name in module
        for module in direct_imports
        for name in ("repository", "reader", "context_sources")
    )
    assert "_to_llm_message" not in (kernel / "direct_model.py").read_text(
        encoding="utf-8"
    )
    assert "KernelModelRequest" not in (kernel / "runner.py").read_text(
        encoding="utf-8"
    )
    assert "direct_model" not in (kernel / "context_sources.py").read_text(
        encoding="utf-8"
    )
    assert "project_model_input_compile_observation" in (
        kernel / "runner.py"
    ).read_text(encoding="utf-8")

    reader_source = (kernel / "reader.py").read_text(encoding="utf-8")
    assert "SELECT e.*" not in reader_source
    assert "SELECT * FROM pulsara_v3.assistant_message_blocks" not in reader_source
    assert reader_source.count("LIMIT %s") >= 5
    assert reader_source.index("_preflight_physical_bytes(") < reader_source.index(
        "_load_entry_payloads("
    )
