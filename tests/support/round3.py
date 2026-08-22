"""Formal structured-input test consumers; production never imports this module."""

from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
import json
from pathlib import Path
from typing import AsyncIterator, Callable

from pulsara_agent.conversation_kernel.direct_model import (
    CompletedProviderModelExecution,
    DirectKernelModelPort,
    KernelModelExecutionRequest,
    KernelModelPreparationRequest,
    KernelModelTargetPreparationRequest,
    PreparedKernelModelCall,
    PreparedKernelModelTarget,
)
from pulsara_agent.capability.contracts import (
    CapabilityKind,
    CapabilitySourceKind,
    CapabilitySourceRefreshMode,
    CapabilitySourceSnapshotDisposition,
    EmptyCapabilityEpochPredecessor,
    FrozenMcpCapabilityProjectionInput,
    FrozenSkillProjectionInput,
    FrozenToolCapabilityExposurePlan,
    ToolCapabilityOrigin,
    capability_identity,
    capability_source_ref,
    capability_source_registration,
    freeze_capability_source_snapshot,
    freeze_tool_capability_fact,
    tool_capability_version_ref,
)
from pulsara_agent.capability.local_skills import LocalSkillDiscovery, LocalSkillProvider
from pulsara_agent.capability.planner import KernelToolCapabilityPlanner
from pulsara_agent.capability.registry import (
    freeze_capability_dispatch_cut_and_views,
    freeze_tool_planning_input,
)
from pulsara_agent.conversation_kernel.capability_composition import (
    freeze_capability_registry_from_owner_snapshots,
    issue_local_skill_catalog_source_snapshot,
    issue_mcp_capability_source_snapshot_set,
    issue_sealed_builtin_capability_snapshot,
)
from pulsara_agent.conversation_kernel.mcp.contracts import build_catalog_snapshot
from pulsara_agent.conversation_kernel.input_continuity import (
    FrozenProviderInputEpochView,
    ProcessLocalProviderInputInstallAuthority,
)
from pulsara_agent.conversation_kernel.memory.contracts import (
    FrozenModelCallMemoryContext,
    FrozenModelVisibleMemoryProvenance,
    ModelVisibleMemoryProvenanceDisposition,
)
from pulsara_agent.conversation_kernel.context_sources import (
    ContextSourceRegistry,
    FrozenNonTriggerContextSources,
    build_subagent_context_source,
)
from pulsara_agent.conversation_kernel.cold_epoch import (
    SubagentInitialSeed,
    build_subagent_initial_seed,
)
from pulsara_agent.conversation_kernel.subagents.contracts import (
    SubagentContextMode,
    SubagentProfileKind,
    SubagentResultSource,
    build_parent_context_call_subject,
    build_parent_context_selection,
    build_subagent_result_public_fact,
    parent_context_call_subject_identity_digest,
    parent_context_selection_identity_digest,
    parent_context_source_identity_digest,
)
from pulsara_agent.conversation_kernel.runner import (
    KernelToolInvocationContext,
    KernelToolResult,
)
from pulsara_agent.conversation_kernel.tool_surface import (
    BuiltinExecutionPolicyRef,
    PreparedKernelToolSurface,
    PreparedToolExecutionBinding,
    ProcessLocalToolSurfaceAccess,
    ProcessLocalToolSurfaceBorrow,
)
from pulsara_agent.conversation_kernel.tool_runtime import DirectKernelToolPort
from pulsara_agent.model_input.contracts import (
    CanonicalModelInputSnapshot,
    CollectedContextSources,
    ContextBudgetClass,
    ContextChannel,
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
    FrozenModelToolSurface,
    FrozenCanonicalCompileSnapshot,
    FrozenToolSpec,
    ModelInputScopeKind,
    canonical_compile_snapshot_fingerprint,
    build_tool_observation_freshness_fact,
    context_binding_compile_fact_fingerprint,
    model_tool_surface_fingerprint,
)
from pulsara_agent.model_input.provider_replay import (
    FrozenCanonicalProviderDispatchRead,
)
from pulsara_agent.model_input.continuity import FULL_HISTORY_CONTEXT_BASE_IDENTITY
from pulsara_agent.model_input.continuity import ProcessLocalProviderInputInstallPermit
from pulsara_agent.primitives.context import context_fingerprint, freeze_json
from pulsara_agent.llm.adapters.openai.function_tools import (
    freeze_openai_native_tool_eligibility,
    materialize_openai_native_tool_projection_set,
)
from pulsara_agent.llm.result import TransportUsageReport
from pulsara_agent.ports.provider_stream import (
    ProviderNormalizedTerminalKind,
    ProviderStreamTerminal,
)
from pulsara_agent.primitives.permission import DEFAULT_PERMISSION_MODE
from pulsara_agent.primitives.run_permission import (
    FrozenRunPermissionSnapshot,
    RunPermissionAdmissionSource,
    build_run_permission_snapshot,
)
from tests.support.model_config import test_llm_config


class ScriptedKernelModel:
    def __init__(self, calls: list[list[object]]) -> None:
        self._calls = calls
        self.requests: list[KernelModelExecutionRequest] = []
        self.preparation_requests: list[KernelModelPreparationRequest] = []
        self._preparer = DirectKernelModelPort(
            config=test_llm_config(
                api_key="test",
                base_url="https://example.invalid/v1",
                pro_model="test-pro",
                flash_model="test-flash",
                api="openai_chat_completions",
            )
        )

    def prepare_target(
        self, request: KernelModelTargetPreparationRequest
    ) -> PreparedKernelModelTarget:
        self.preparation_requests.append(request)  # type: ignore[arg-type]
        return self._preparer.prepare_target(request)

    def freeze_native_tool_eligibility(self, **kwargs):
        return self._preparer.freeze_native_tool_eligibility(**kwargs)

    def materialize_native_tool_projection_set(self, **kwargs):
        return self._preparer.materialize_native_tool_projection_set(**kwargs)

    def bind_tool_surface(self, **kwargs):
        return self._preparer.bind_tool_surface(**kwargs)

    def bind_semantic_tool_surface(self, **kwargs):
        return self._preparer.bind_semantic_tool_surface(**kwargs)

    def resolve_compaction_summary_call(self, **kwargs):
        return self._preparer.resolve_compaction_summary_call(**kwargs)

    def replay_target_for_resolved_call(self, call):
        return self._preparer.replay_target_for_resolved_call(call)

    def prepare_call(
        self, request: KernelModelPreparationRequest
    ) -> PreparedKernelModelCall:
        self.preparation_requests.append(request)
        return prepare_test_model_call(self._preparer, request)

    def plan_wire_input(self, **kwargs):
        return self._preparer.plan_wire_input(**kwargs)

    def preflight_execution(
        self,
        request: KernelModelExecutionRequest,
        *,
        append_candidate,
        install_authority: ProcessLocalProviderInputInstallAuthority,
    ) -> "_ScriptedPreparedExecution":
        for tool in request.compiled_input.tools:
            binding = request.surface_borrow.execution_binding(tool.name)
            if binding.descriptor_fingerprint != tool.descriptor_fingerprint:
                raise RuntimeError("scripted tool binding was revoked")
        self.requests.append(request)
        return _ScriptedPreparedExecution(
            request=request,
            items=self._calls.pop(0),
            append_candidate=append_candidate,
            install_authority=install_authority,
        )


class CallbackScriptedKernelModel:
    """Round 3.1 model double with the production preflight/open boundary."""

    def __init__(
        self,
        stream_factory: Callable[
            [KernelModelExecutionRequest], AsyncIterator[object]
        ],
    ) -> None:
        self._stream_factory = stream_factory
        self.requests: list[KernelModelExecutionRequest] = []
        self._preparer = DirectKernelModelPort(
            config=test_llm_config(
                api_key="test",
                base_url="https://example.invalid/v1",
                pro_model="test-pro",
                flash_model="test-flash",
                api="openai_chat_completions",
            )
        )

    def prepare_target(self, request):
        return self._preparer.prepare_target(request)

    def freeze_native_tool_eligibility(self, **kwargs):
        return self._preparer.freeze_native_tool_eligibility(**kwargs)

    def materialize_native_tool_projection_set(self, **kwargs):
        return self._preparer.materialize_native_tool_projection_set(**kwargs)

    def bind_tool_surface(self, **kwargs):
        return self._preparer.bind_tool_surface(**kwargs)

    def bind_semantic_tool_surface(self, **kwargs):
        return self._preparer.bind_semantic_tool_surface(**kwargs)

    def resolve_compaction_summary_call(self, **kwargs):
        return self._preparer.resolve_compaction_summary_call(**kwargs)

    def replay_target_for_resolved_call(self, call):
        return self._preparer.replay_target_for_resolved_call(call)

    def prepare_call(
        self, request: KernelModelPreparationRequest
    ) -> PreparedKernelModelCall:
        return prepare_test_model_call(self._preparer, request)

    def plan_wire_input(self, **kwargs):
        return self._preparer.plan_wire_input(**kwargs)

    def preflight_execution(
        self,
        request: KernelModelExecutionRequest,
        *,
        append_candidate,
        install_authority: ProcessLocalProviderInputInstallAuthority,
    ) -> "_CallbackPreparedExecution":
        self.requests.append(request)
        return _CallbackPreparedExecution(
            request=request,
            stream_factory=self._stream_factory,
            append_candidate=append_candidate,
            install_authority=install_authority,
        )


class _CallbackPreparedExecution:
    def __init__(
        self,
        *,
        request: KernelModelExecutionRequest,
        stream_factory: Callable[
            [KernelModelExecutionRequest], AsyncIterator[object]
        ],
        append_candidate,
        install_authority: ProcessLocalProviderInputInstallAuthority,
    ) -> None:
        self._request = request
        self._stream_factory = stream_factory
        self._append_candidate = append_candidate
        self._install_authority = install_authority
        self._settled = False
        self._completion = None

    def discard(self) -> None:
        if self._settled:
            raise RuntimeError("callback execution already settled")
        self._settled = True

    async def open_once(
        self, permit: ProcessLocalProviderInputInstallPermit
    ) -> AsyncIterator[object]:
        if self._settled:
            raise RuntimeError("callback execution already settled")
        if (
            permit.epoch_nonce != self._append_candidate.epoch_nonce
            or permit.epoch_revision
            != self._append_candidate.expected_epoch_revision + 1
        ):
            raise RuntimeError("callback execution permit mismatch")
        self._install_authority.consume(
            permit,
            candidate=self._append_candidate,
            execution=self,
        )
        self._settled = True
        async for item in self._stream_factory(self._request):
            yield item
        self._completion = completed_provider_execution_for_test(self._request)

    def take_completed_result_once(self):
        if self._completion is None:
            raise RuntimeError("test provider execution is not completed")
        result = self._completion
        self._completion = None
        return result


class _ScriptedPreparedExecution:
    def __init__(
        self,
        *,
        request: KernelModelExecutionRequest,
        items: list[object],
        append_candidate,
        install_authority: ProcessLocalProviderInputInstallAuthority,
    ) -> None:
        self._request = request
        self._items = items
        self._append_candidate = append_candidate
        self._install_authority = install_authority
        self._opened = False
        self._completion = None

    def discard(self) -> None:
        if self._opened:
            raise RuntimeError("scripted execution already opened")
        self._opened = True

    async def open_once(
        self, permit: ProcessLocalProviderInputInstallPermit
    ) -> AsyncIterator[object]:
        if self._opened:
            raise RuntimeError("scripted execution already opened")
        if (
            permit.epoch_nonce != self._append_candidate.epoch_nonce
            or permit.epoch_revision
            != self._append_candidate.expected_epoch_revision + 1
        ):
            raise RuntimeError("scripted execution permit mismatch")
        self._install_authority.consume(
            permit,
            candidate=self._append_candidate,
            execution=self,
        )
        self._opened = True
        for item in self._items:
            yield item
        self._completion = completed_provider_execution_for_test(self._request)

    def take_completed_result_once(self):
        if self._completion is None:
            raise RuntimeError("test provider execution is not completed")
        result = self._completion
        self._completion = None
        return result


def completed_provider_execution_for_test(
    request: KernelModelExecutionRequest,
) -> CompletedProviderModelExecution:
    terminal = ProviderStreamTerminal(
        terminal_kind=ProviderNormalizedTerminalKind.COMPLETED,
        usage=TransportUsageReport(usage_status="missing", usage=None),
    )
    return CompletedProviderModelExecution(
        terminal=terminal,
        replay_payload=None,
        replay_target=DirectKernelModelPort.replay_target(request.prepared_call),
    )


@dataclass(frozen=True, slots=True)
class _Cwd:
    value: str = "/test/workspace"


class StaticContextSourceCollector:
    """Required first-party facts with the production contracts."""

    @property
    def registry_fingerprint(self) -> str:
        return ContextSourceRegistry().fingerprint

    def freeze_skill_capability_source_snapshot(
        self,
        *,
        conversation_scope_kind,
        scope_subagent_task_id,
        deadline_monotonic=None,
    ):
        del deadline_monotonic
        source = capability_source_ref(
            CapabilitySourceKind.LOCAL_SKILL_CATALOG, "test-skill-catalog"
        )
        registration = capability_source_registration(
            source=source,
            refresh_mode=CapabilitySourceRefreshMode.SAFE_POINT_REFRESHABLE,
            source_contract_fingerprint=context_fingerprint(
                "test:skill-source-contract:v1", "empty"
            ),
        )
        snapshot = freeze_capability_source_snapshot(
            registration=registration,
            conversation_scope_kind=conversation_scope_kind,
            scope_subagent_task_id=scope_subagent_task_id,
            disposition=CapabilitySourceSnapshotDisposition.COMPLETE,
            facts=(),
        )
        root_policy = LocalSkillProvider(include_user_skills=False).prepare_root_policy(
            Path.cwd(),
            conversation_scope_kind=conversation_scope_kind,
            scope_subagent_task_id=scope_subagent_task_id,
        )
        return issue_local_skill_catalog_source_snapshot(
            conversation_scope_kind=conversation_scope_kind,
            scope_subagent_task_id=scope_subagent_task_id,
            source_snapshot=snapshot,
            discovery=LocalSkillDiscovery((), (), root_policy=root_policy),
            owner_authenticity=self,
        )

    def freeze_skill_capability_projection_input(self, owner):
        from pulsara_agent.conversation_kernel.capability import (
            skill_discovery_semantic_fingerprint,
        )

        discovery_fingerprint = skill_discovery_semantic_fingerprint(owner.discovery)
        return FrozenSkillProjectionInput(
            discovery_semantic_fingerprint=discovery_fingerprint,
            source_snapshot=owner.source_snapshot,
        )

    def collect(self, **_kwargs: object) -> CollectedContextSources:
        canonical_facts = _kwargs["canonical_facts"]
        permission = canonical_facts.run_permission_snapshot  # type: ignore[union-attr]
        candidates: tuple[ContextSourceCandidate, ...] = (
            _candidate(
                kind=ContextSourceKind.BASE_SYSTEM,
                version="pulsara.base-system.prefix-continuity.v8-hierarchical-subagents",
                channel=ContextChannel.SYSTEM,
                trust=ContextTrustClass.ROOT_INSTRUCTION,
                budget=ContextBudgetClass.MUST_KEEP,
                placement=0,
                degradation=0,
                variants=((ContextRenderMode.FULL, "ROOT SYSTEM"),),
            ),
            _candidate(
                kind=ContextSourceKind.RUNTIME_ENVIRONMENT,
                version="pulsara.runtime-environment.v2",
                channel=ContextChannel.RUNTIME_OBSERVATION,
                trust=ContextTrustClass.TRUSTED_RUNTIME_FACT,
                budget=ContextBudgetClass.MUST_KEEP,
                placement=10,
                degradation=10,
                variants=(
                    (ContextRenderMode.FULL, "runtime workspace=/test/workspace"),
                    (ContextRenderMode.COMPACT, "runtime=/test/workspace"),
                ),
            ),
            _candidate(
                kind=ContextSourceKind.RUN_PERMISSION,
                version="pulsara.run-permission.v2",
                channel=ContextChannel.RUNTIME_OBSERVATION,
                trust=ContextTrustClass.AUTHORIZED_RUNTIME_GUIDANCE,
                budget=ContextBudgetClass.MUST_KEEP,
                placement=20,
                degradation=12,
                variants=(
                    (
                        ContextRenderMode.FULL,
                        f"permission={permission.effective_mode.value}",
                    ),
                    (
                        ContextRenderMode.COMPACT,
                        f"permission={permission.effective_mode.value}",
                    ),
                ),
            ),
        )
        if canonical_facts.plan_handoff_fact is not None:  # type: ignore[union-attr]
            candidates += (
                _candidate(
                    kind=ContextSourceKind.PLAN_HANDOFF,
                    version="pulsara.plan-handoff.v2",
                    channel=ContextChannel.RUNTIME_OBSERVATION,
                    trust=ContextTrustClass.AUTHORIZED_RUNTIME_GUIDANCE,
                    budget=ContextBudgetClass.MUST_KEEP,
                    placement=30,
                    degradation=11,
                    variants=(
                        (ContextRenderMode.FULL, "plan handoff full"),
                        (ContextRenderMode.COMPACT, "plan handoff"),
                    ),
                ),
            )
        if canonical_facts.plan_workflow_fact is not None:  # type: ignore[union-attr]
            candidates += (
                _candidate(
                    kind=ContextSourceKind.PLAN_WORKFLOW,
                    version="pulsara.plan-workflow.v2",
                    channel=ContextChannel.RUNTIME_OBSERVATION,
                    trust=ContextTrustClass.AUTHORIZED_RUNTIME_GUIDANCE,
                    budget=ContextBudgetClass.MUST_KEEP,
                    placement=40,
                    degradation=10,
                    variants=(
                        (ContextRenderMode.FULL, "plan workflow full"),
                        (ContextRenderMode.COMPACT, "plan workflow"),
                    ),
                ),
            )
        previous = canonical_facts.previous_turn_outcome_fact  # type: ignore[union-attr]
        if previous is not None:
            body = json.dumps(
                {
                    "kind": previous.outcome_kind.value,
                    "accepted_assistant_entry_count": (
                        previous.accepted_assistant_entry_count
                    ),
                    "definitely_not_dispatched_tool_count": (
                        previous.definitely_not_dispatched_tool_count
                    ),
                    "outcome_unknown_tool_count": (
                        previous.outcome_unknown_tool_count
                    ),
                },
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            candidates += (
                _candidate(
                    kind=ContextSourceKind.PREVIOUS_TURN_OUTCOME,
                    version="pulsara.previous-turn-outcome.v1",
                    channel=ContextChannel.RUNTIME_OBSERVATION,
                    trust=ContextTrustClass.AUTHORIZED_RUNTIME_GUIDANCE,
                    budget=ContextBudgetClass.MUST_KEEP,
                    placement=45,
                    degradation=10,
                    variants=(
                        (ContextRenderMode.FULL, body),
                        (ContextRenderMode.COMPACT, body),
                    ),
                ),
            )
        freshness = canonical_facts.tool_observation_freshness_fact  # type: ignore[union-attr]
        candidates += (
            _candidate(
                kind=ContextSourceKind.TOOL_OBSERVATION_FRESHNESS,
                version="pulsara.tool-observation-freshness.v1",
                channel=ContextChannel.RUNTIME_OBSERVATION,
                trust=ContextTrustClass.TRUSTED_RUNTIME_FACT,
                budget=ContextBudgetClass.MUST_KEEP,
                placement=70,
                degradation=10,
                variants=((ContextRenderMode.FULL, freshness.current_turn_ref),),
            ),
        )
        present = {item.source_kind for item in candidates}
        absence_kinds = {
            ContextSourceKind.RUNTIME_CLOCK: ContextSourceAbsenceKind.UNAVAILABLE,
            ContextSourceKind.PLAN_HANDOFF: ContextSourceAbsenceKind.NOT_APPLICABLE,
            ContextSourceKind.PLAN_WORKFLOW: ContextSourceAbsenceKind.EXPLICIT_EMPTY,
            ContextSourceKind.SKILL_CATALOG: ContextSourceAbsenceKind.EXPLICIT_EMPTY,
            ContextSourceKind.MCP_CATALOG: ContextSourceAbsenceKind.NOT_APPLICABLE,
            ContextSourceKind.ACTIVE_SKILL: ContextSourceAbsenceKind.EXPLICIT_EMPTY,
            ContextSourceKind.PREVIOUS_TURN_OUTCOME: (
                ContextSourceAbsenceKind.EXPLICIT_EMPTY
            ),
            ContextSourceKind.MEMORY_RESPONSE_PREFERENCE_HEAD: (
                ContextSourceAbsenceKind.NOT_APPLICABLE
            ),
            ContextSourceKind.MEMORY_RECALL: (
                ContextSourceAbsenceKind.NOT_APPLICABLE
            ),
            ContextSourceKind.COMPACTION_RUNTIME_HANDOFF: (
                ContextSourceAbsenceKind.NOT_APPLICABLE
            ),
            ContextSourceKind.RETAINED_SKILL_CONTEXT: (
                ContextSourceAbsenceKind.NOT_APPLICABLE
            ),
            ContextSourceKind.PARENT_CONTEXT: (
                ContextSourceAbsenceKind.NOT_APPLICABLE
            ),
            ContextSourceKind.DEPENDENCY_RESULTS: (
                ContextSourceAbsenceKind.NOT_APPLICABLE
            ),
        }
        absent = tuple(
            _absent_source(kind, absence)
            for kind, absence in absence_kinds.items()
            if kind not in present
        )
        registry = self.registry_fingerprint
        collection = context_fingerprint(
            "collected-context-sources:v1",
            {
                "registry_fingerprint": registry,
                "candidates": tuple(
                    item.source_semantic_fingerprint for item in candidates
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
        return CollectedContextSources(candidates, (), registry, collection, absent)

    def freeze_non_trigger_sources(
        self, **kwargs: object
    ) -> FrozenNonTriggerContextSources:
        collection = self.collect(
            activation_subject=None,
            activation_text="",
            **kwargs,
        )
        return FrozenNonTriggerContextSources(
            candidates=collection.candidates,
            absent_facts=collection.absent_facts,
            diagnostics=collection.diagnostics,
            registry_fingerprint=collection.registry_fingerprint,
            tool_exposure_plan=kwargs["tool_exposure_plan"],  # type: ignore[arg-type]
            skill_dispatch_view=kwargs["skill_dispatch_view"],  # type: ignore[arg-type]
            skill_owner_snapshot=kwargs["skill_owner_snapshot"],  # type: ignore[arg-type]
        )

    def complete_frozen_sources(
        self,
        frozen: FrozenNonTriggerContextSources,
        **_kwargs: object,
    ) -> CollectedContextSources:
        registry = self.registry_fingerprint
        if registry != frozen.registry_fingerprint:
            raise ValueError("context source registry changed after source freeze")
        collection = context_fingerprint(
            "collected-context-sources:v1",
            {
                "registry_fingerprint": registry,
                "candidates": tuple(
                    item.source_semantic_fingerprint for item in frozen.candidates
                ),
                "diagnostics": (),
                "absent": tuple(
                    (
                        item.source_kind.value,
                        item.lifecycle.value,
                        item.absence_kind.value,
                        item.domain_semantic_fingerprint,
                    )
                    for item in frozen.absent_facts
                ),
            },
        )
        return CollectedContextSources(
            frozen.candidates,
            frozen.diagnostics,
            registry,
            collection,
            frozen.absent_facts,
        )

    def freeze_compaction_active_skill_source(
        self,
        predecessor_epoch: FrozenProviderInputEpochView | None,
    ) -> ContextSourceAbsentFact:
        """Static fixtures never synthesize inherited active Skill bodies."""

        if predecessor_epoch is not None and any(
            head.source_kind is ContextSourceKind.ACTIVE_SKILL
            for head in predecessor_epoch.source_heads
        ):
            raise ValueError(
                "static source collector cannot inherit an installed active Skill"
            )
        return _absent_source(
            ContextSourceKind.ACTIVE_SKILL,
            ContextSourceAbsenceKind.NOT_APPLICABLE,
        )


def _absent_source(
    kind: ContextSourceKind,
    absence: ContextSourceAbsenceKind,
) -> ContextSourceAbsentFact:
    binding = ContextSourceRegistry().binding(kind)
    return ContextSourceAbsentFact(
        source_kind=kind,
        lifecycle=binding.lifecycle,
        absence_kind=absence,
        source_contract_version=binding.contract_version,
        source_contract_fingerprint=binding.contract_fingerprint,
        trust_class=binding.trust,
        budget_class=binding.budget,
        placement_ordinal=binding.placement,
        degradation_priority=binding.degradation,
        domain_semantic_fingerprint=context_fingerprint(
            "pulsara:context-source-absence:v1",
            {
                "kind": kind.value,
                "absence": absence.value,
                "contract": binding.contract_fingerprint,
            },
        ),
    )


def static_canonical_compile_facts(
    canonical_input: CanonicalModelInputSnapshot,
) -> FrozenCanonicalCompileSnapshot:
    """Build the final no-Plan compile fact for isolated adapter tests."""

    permission = build_run_permission_snapshot(
        snapshot_id="permission:test",
        requested_mode=DEFAULT_PERMISSION_MODE,
        effective_mode=DEFAULT_PERMISSION_MODE,
        admission_source=RunPermissionAdmissionSource.USER_SUBMISSION,
    )
    binding_values = {
        "binding_revision_id": canonical_input.identity.context_binding_revision_id,
        "revision_ordinal": 0,
        "base_kind": ContextBindingBaseKind.FULL_HISTORY,
        "context_snapshot_id": None,
        "source_through_sequence": 0,
        "context_base_semantic_identity": FULL_HISTORY_CONTEXT_BASE_IDENTITY,
    }
    provisional_binding = FrozenContextBindingCompileFact.__new__(
        FrozenContextBindingCompileFact
    )
    for name, value in binding_values.items():
        object.__setattr__(provisional_binding, name, value)
    object.__setattr__(provisional_binding, "fact_fingerprint", "")
    binding = FrozenContextBindingCompileFact(
        **binding_values,
        fact_fingerprint=context_binding_compile_fact_fingerprint(
            provisional_binding
        ),
    )
    provisional = FrozenCanonicalCompileSnapshot.__new__(
        FrozenCanonicalCompileSnapshot
    )
    object.__setattr__(provisional, "canonical_input", canonical_input)
    object.__setattr__(provisional, "context_binding_fact", binding)
    object.__setattr__(provisional, "run_permission_snapshot", permission)
    object.__setattr__(provisional, "plan_workflow_fact", None)
    object.__setattr__(provisional, "plan_handoff_fact", None)
    object.__setattr__(provisional, "approved_plan_materialization_fact", None)
    freshness = build_tool_observation_freshness_fact(
        session_id=canonical_input.identity.session_id,
        workspace_id="workspace:test",
        current_turn_id=canonical_input.identity.turn_id,
        current_scope_kind=canonical_input.identity.conversation_scope_kind,
        scope_subagent_task_id=canonical_input.identity.scope_subagent_task_id,
        current_initial_entry_sequence=1,
        immediate_predecessor_turn_id=None,
    )
    object.__setattr__(provisional, "previous_turn_outcome_fact", None)
    object.__setattr__(provisional, "tool_observation_freshness_fact", freshness)
    object.__setattr__(provisional, "canonical_read_cut_fingerprint", "")
    return FrozenCanonicalCompileSnapshot(
        canonical_input=canonical_input,
        context_binding_fact=binding,
        run_permission_snapshot=permission,
        plan_workflow_fact=None,
        plan_handoff_fact=None,
        approved_plan_materialization_fact=None,
        previous_turn_outcome_fact=None,
        tool_observation_freshness_fact=freshness,
        canonical_read_cut_fingerprint=canonical_compile_snapshot_fingerprint(
            provisional
        ),
    )


class StructuredToolPort:
    def __init__(
        self,
        delegate: object,
        *,
        tool_names: tuple[str, ...] = ("terminal", "test_tool"),
    ) -> None:
        self.delegate = delegate
        self._authority = object()
        self._active: set[str] = set()
        self._mcp_owner = object()
        specs = tuple(
            FrozenToolSpec(
                name=name,
                description=f"Test tool {name}",
                parameters=freeze_json(
                    {"type": "object", "additionalProperties": True}
                ),
                descriptor_fingerprint=context_fingerprint(
                    "test-tool-descriptor:v1", name
                ),
            )
            for name in sorted(tool_names)
        )
        self._surfaces = {
            scope: FrozenModelToolSurface(
                scope,
                specs,
                model_tool_surface_fingerprint(scope, specs),
            )
            for scope in ModelInputScopeKind
        }

    def sealed_builtin_capability_snapshot(
        self, *, conversation_scope_kind, scope_subagent_task_id
    ):
        surface = self._surfaces[conversation_scope_kind]
        source = capability_source_ref(
            CapabilitySourceKind.BUILTIN_REGISTRY, "test-builtin-registry"
        )
        registration = capability_source_registration(
            source=source,
            refresh_mode=CapabilitySourceRefreshMode.IMMUTABLE,
            source_contract_fingerprint=context_fingerprint(
                "test:builtin-source-contract:v1", "sealed"
            ),
        )
        facts = tuple(
            freeze_tool_capability_fact(
                identity=capability_identity(
                    kind=CapabilityKind.TOOL,
                    source=source,
                    stable_name=spec.name,
                ),
                origin=ToolCapabilityOrigin.BUILTIN,
                canonical_tool_spec=spec,
            )
            for spec in surface.tool_specs
        )
        snapshot = freeze_capability_source_snapshot(
            registration=registration,
            conversation_scope_kind=conversation_scope_kind,
            scope_subagent_task_id=scope_subagent_task_id,
            disposition=CapabilitySourceSnapshotDisposition.COMPLETE,
            facts=facts,
        )
        return issue_sealed_builtin_capability_snapshot(
            conversation_scope_kind=conversation_scope_kind,
            scope_subagent_task_id=scope_subagent_task_id,
            source_snapshot=snapshot,
            executor_bindings=(),
            builtin_composition_seal=self._authority,
        )

    def freeze_mcp_capability_source_snapshot_set(
        self, *, conversation_scope_kind, scope_subagent_task_id
    ):
        return issue_mcp_capability_source_snapshot_set(
            conversation_scope_kind=conversation_scope_kind,
            scope_subagent_task_id=scope_subagent_task_id,
            source_snapshots=(),
            catalog_snapshot=build_catalog_snapshot(
                owner_epoch=1, catalog_revision=1, entries=()
            ).for_scope(conversation_scope_kind),
            inspection_inputs=(),
            owner_authenticity=self._mcp_owner,
        )

    def freeze_mcp_capability_projection_input(self, owner):
        return FrozenMcpCapabilityProjectionInput(
            conversation_scope_kind=owner.conversation_scope_kind,
            scope_subagent_task_id=owner.scope_subagent_task_id,
            source_snapshots=owner.source_snapshots,
            catalog_semantic_fingerprint=owner.catalog_snapshot.semantic_fingerprint,
            inspectability_facts=(),
        )

    def prepare_planned_tool_surface(
        self, *, plan: FrozenToolCapabilityExposurePlan, builtin
    ) -> PreparedKernelToolSurface:
        prepared = self.snapshot_tool_surface(
            conversation_scope_kind=plan.direct_tool_surface.conversation_scope_kind,
            scope_subagent_task_id=builtin.scope_subagent_task_id,
        )
        if prepared.model_surface != plan.direct_tool_surface:
            raise RuntimeError("test planned surface drifted")
        return PreparedKernelToolSurface(
            model_surface=prepared.model_surface,
            execution_bindings=prepared.execution_bindings,
            access=prepared.access,
            capability_exposure_plan=plan,
        )

    def snapshot_tool_surface(
        self,
        *,
        conversation_scope_kind: ModelInputScopeKind,
        scope_subagent_task_id: str | None,
    ) -> PreparedKernelToolSurface:
        surface = self._surfaces[conversation_scope_kind]
        bindings = tuple(
            PreparedToolExecutionBinding(
                tool_name=item.name,
                descriptor_fingerprint=item.descriptor_fingerprint,
                executor_binding_fingerprint=context_fingerprint(
                    "test-tool-binding:v1", item.name
                ),
                execution_policy=BuiltinExecutionPolicyRef(
                    tool_name=item.name,
                    catalog_entry_fingerprint=context_fingerprint(
                        "test-tool-catalog:v1", item.name
                    ),
                ),
            )
            for item in surface.tool_specs
        )
        access = ProcessLocalToolSurfaceAccess(
            owner_epoch=1,
            surface_generation=1,
            conversation_scope_kind=conversation_scope_kind,
            scope_subagent_task_id=scope_subagent_task_id,
            _authority=self._authority,
        )
        return PreparedKernelToolSurface(
            model_surface=surface,
            execution_bindings=bindings,
            access=access,
        )

    def borrow_tool_surface(
        self, prepared: PreparedKernelToolSurface
    ) -> ProcessLocalToolSurfaceBorrow:
        borrow_id = f"test-borrow:{len(self._active) + 1}"
        self._active.add(borrow_id)

        def validate(
            borrow: ProcessLocalToolSurfaceBorrow, tool_name: str
        ) -> PreparedToolExecutionBinding:
            if borrow.borrow_id not in self._active:
                raise RuntimeError("test tool surface borrow is inactive")
            for item in prepared.execution_bindings:
                if item.tool_name == tool_name:
                    return item
            raise RuntimeError("test tool was not advertised")

        def release(borrow: ProcessLocalToolSurfaceBorrow) -> None:
            self._active.discard(borrow.borrow_id)

        return ProcessLocalToolSurfaceBorrow(
            prepared=prepared,
            borrow_id=borrow_id,
            _authority=self._authority,
            _validate=validate,
            _release=release,
        )

    def validate_tool_surface_borrow(
        self,
        borrow: ProcessLocalToolSurfaceBorrow,
        prepared: PreparedKernelToolSurface,
    ) -> None:
        if (
            borrow._closed
            or borrow.borrow_id not in self._active
            or not borrow.exactly_joins(prepared)
        ):
            raise RuntimeError("test tool surface borrow is inactive")
        if prepared.model_surface.tool_specs:
            borrow.binding_fingerprint(prepared.model_surface.tool_specs[0].name)

    def install_provider_input_tool_result_deliveries(self, **kwargs: object) -> None:
        permit = kwargs["permit"]
        borrow = kwargs["surface_borrow"]
        if not isinstance(permit, ProcessLocalProviderInputInstallPermit):
            raise TypeError("test provider-input install permit is invalid")
        if not isinstance(borrow, ProcessLocalToolSurfaceBorrow):
            raise TypeError("test provider-input install borrow is invalid")
        self.validate_tool_surface_borrow(borrow, borrow.prepared)

    async def freeze_compaction_runtime_handoff(self, **_kwargs: object):
        return None

    async def authorize(self, **kwargs: object):
        kwargs.pop("surface_borrow")
        permission = kwargs.pop("permission_snapshot")
        kwargs.pop("memory_context")
        if not isinstance(permission, FrozenRunPermissionSnapshot):
            raise TypeError("test tool authorization lacks a permission snapshot")
        return await self.delegate.authorize(**kwargs)

    async def request_confirmation(self, **kwargs: object):
        permission = kwargs.pop("permission_snapshot")
        if not isinstance(permission, FrozenRunPermissionSnapshot):
            raise TypeError("test confirmation lacks a permission snapshot")
        return await self.delegate.request_confirmation(**kwargs)

    async def invoke(self, **kwargs: object):
        return await self.delegate.invoke(**kwargs)

    async def settle_process_local_effect(self, *args: object):
        method = getattr(self.delegate, "settle_process_local_effect", None)
        if method is not None:
            return await method(*args)
        raise RuntimeError("test tool emitted an unowned process-local settlement")


class _EmptyTestMcpCapabilityOwner:
    """Zero-server MCP owner used only to complete real-port test composition."""

    def __init__(self) -> None:
        self._authority = object()
        self._catalog = build_catalog_snapshot(
            owner_epoch=1, catalog_revision=1, entries=()
        )

    def freeze_capability_source_snapshot_set(
        self, *, conversation_scope_kind, scope_subagent_task_id
    ):
        return issue_mcp_capability_source_snapshot_set(
            conversation_scope_kind=conversation_scope_kind,
            scope_subagent_task_id=scope_subagent_task_id,
            source_snapshots=(),
            catalog_snapshot=self._catalog.for_scope(conversation_scope_kind),
            inspection_inputs=(),
            owner_authenticity=self._authority,
        )

    def install_pending_at_safe_point(self):
        return None

    def freeze_capability_projection_input(self, owner):
        return FrozenMcpCapabilityProjectionInput(
            conversation_scope_kind=owner.conversation_scope_kind,
            scope_subagent_task_id=owner.scope_subagent_task_id,
            source_snapshots=owner.source_snapshots,
            catalog_semantic_fingerprint=owner.catalog_snapshot.semantic_fingerprint,
            inspectability_facts=(),
        )


def seal_test_direct_tool_port(port: DirectKernelToolPort) -> None:
    """Complete the production composition boundary for a standalone test port."""

    port.bind_interaction_port(object())  # type: ignore[arg-type]
    port.bind_subagent_port(  # type: ignore[arg-type]
        type("_EmptySubagentPort", (), {"tool_names": ()})()
    )
    port.bind_memory_port(  # type: ignore[arg-type]
        type("_EmptyMemoryPort", (), {"tool_names": ()})()
    )
    port.bind_mcp_supervisor(_EmptyTestMcpCapabilityOwner())  # type: ignore[arg-type]
    port.seal_builtin_composition()


def prepare_test_model_call(
    port: DirectKernelModelPort,
    request: KernelModelPreparationRequest,
) -> PreparedKernelModelCall:
    """Bind a component-test surface through the split Round 9 model API."""

    target = port.prepare_target(
        KernelModelTargetPreparationRequest(
            session_id=request.session_id,
            turn_id=request.turn_id,
            model_call_index=request.model_call_index,
            purpose=request.purpose,
            maximum_input_tokens=request.maximum_input_tokens,
            maximum_output_tokens=request.maximum_output_tokens,
        )
    )
    exposure_plan = request.tool_surface.capability_exposure_plan
    if exposure_plan is not None:
        projection_set = exposure_plan.direct_projection_set
    else:
        source = capability_source_ref(
            CapabilitySourceKind.BUILTIN_REGISTRY,
            "direct-model-component-test",
        )
        facts = tuple(
            freeze_tool_capability_fact(
                identity=capability_identity(
                    kind=CapabilityKind.TOOL,
                    source=source,
                    stable_name=spec.name,
                ),
                origin=ToolCapabilityOrigin.BUILTIN,
                canonical_tool_spec=spec,
            )
            for spec in request.tool_surface.model_surface.tool_specs
        )
        eligibility = freeze_openai_native_tool_eligibility(
            conversation_scope_kind=(
                request.tool_surface.model_surface.conversation_scope_kind
            ),
            scope_subagent_task_id=request.tool_surface.access.scope_subagent_task_id,
            wire_api=target.target.model_profile.provider_profile.wire_api,
            tool_facts=facts,
        )
        ordered = tuple(
            sorted(facts, key=lambda item: item.canonical_tool_spec.name)
        )
        projection_set = materialize_openai_native_tool_projection_set(
            conversation_scope_kind=(
                request.tool_surface.model_surface.conversation_scope_kind
            ),
            scope_subagent_task_id=request.tool_surface.access.scope_subagent_task_id,
            wire_api=target.target.model_profile.provider_profile.wire_api,
            tool_versions=tuple(
                tool_capability_version_ref(item) for item in ordered
            ),
            tool_specs=tuple(item.canonical_tool_spec for item in ordered),
            eligibility=eligibility,
        )
    return port.bind_tool_surface(
        prepared_target=target,
        tool_surface=request.tool_surface,
        native_projection_set=projection_set,
    )


def prepare_test_direct_tool_surface(
    port: DirectKernelToolPort,
    *,
    conversation_scope_kind: ModelInputScopeKind = ModelInputScopeKind.ROOT,
    scope_subagent_task_id: str | None = None,
    wire_api: str = "openai_chat_completions",
) -> PreparedKernelToolSurface:
    """Plan a real port through the Round 9 authority path for component tests."""

    # Standalone tool tests do not construct a Host. Complete only missing
    # support owners, then seal exactly once; tests with real owners already
    # arrive sealed and are left untouched.
    if (
        hasattr(port, "_builtin_composition_state")
        and port._builtin_composition_state.value == "PREPARING"  # noqa: SLF001
    ):
        if port._interaction is None:  # noqa: SLF001
            port.bind_interaction_port(object())  # type: ignore[arg-type]
        if port._subagent is None:  # noqa: SLF001
            port.bind_subagent_port(  # type: ignore[arg-type]
                type("_EmptySubagentPort", (), {"tool_names": ()})()
            )
        if port._memory is None:  # noqa: SLF001
            port.bind_memory_port(  # type: ignore[arg-type]
                type("_EmptyMemoryPort", (), {"tool_names": ()})()
            )
        if port._mcp_supervisor is None:  # noqa: SLF001
            port.bind_mcp_supervisor(  # type: ignore[arg-type]
                _EmptyTestMcpCapabilityOwner()
            )
        port.seal_builtin_composition()

    builtin = port.sealed_builtin_capability_snapshot(
        conversation_scope_kind=conversation_scope_kind,
        scope_subagent_task_id=scope_subagent_task_id,
    )
    mcp = port.freeze_mcp_capability_source_snapshot_set(
        conversation_scope_kind=conversation_scope_kind,
        scope_subagent_task_id=scope_subagent_task_id,
    )
    skill_source = capability_source_ref(
        CapabilitySourceKind.LOCAL_SKILL_CATALOG,
        "pulsara-local-skill-catalog",
    )
    skill_registration = capability_source_registration(
        source=skill_source,
        refresh_mode=CapabilitySourceRefreshMode.SAFE_POINT_REFRESHABLE,
        source_contract_fingerprint=context_fingerprint(
            "test:local-skill-source-contract:v1", ()
        ),
    )
    skill_snapshot = freeze_capability_source_snapshot(
        registration=skill_registration,
        conversation_scope_kind=conversation_scope_kind,
        scope_subagent_task_id=scope_subagent_task_id,
        disposition=CapabilitySourceSnapshotDisposition.COMPLETE,
        facts=(),
    )
    root_policy = LocalSkillProvider(include_user_skills=False).prepare_root_policy(
        Path.cwd(),
        conversation_scope_kind=conversation_scope_kind,
        scope_subagent_task_id=scope_subagent_task_id,
    )
    discovery = LocalSkillDiscovery(
        skills=(),
        diagnostics=(),
        root_policy=root_policy,
    )
    skill_owner = issue_local_skill_catalog_source_snapshot(
        conversation_scope_kind=conversation_scope_kind,
        scope_subagent_task_id=scope_subagent_task_id,
        source_snapshot=skill_snapshot,
        discovery=discovery,
        owner_authenticity=object(),
    )
    registry = freeze_capability_registry_from_owner_snapshots(
        builtin=builtin,
        mcp=mcp,
        skills=skill_owner,
    )
    native = freeze_openai_native_tool_eligibility(
        conversation_scope_kind=conversation_scope_kind,
        scope_subagent_task_id=scope_subagent_task_id,
        wire_api=wire_api,
        tool_facts=registry.tool_facts,
    )
    mcp_input = port.freeze_mcp_capability_projection_input(mcp)
    tool_input = freeze_tool_planning_input(
        predecessor=EmptyCapabilityEpochPredecessor(0),
        native_wire=native,
        mcp=mcp_input,
    )
    discovery_fingerprint = context_fingerprint(
        "test:local-skill-discovery:v1", ()
    )
    skill_input = FrozenSkillProjectionInput(
        discovery_semantic_fingerprint=discovery_fingerprint,
        source_snapshot=skill_snapshot,
    )
    _parent, tool_view, _skill_view = freeze_capability_dispatch_cut_and_views(
        conversation_scope_kind=conversation_scope_kind,
        scope_subagent_task_id=scope_subagent_task_id,
        registry=registry,
        tools=tool_input,
        skills=skill_input,
    )
    planner = KernelToolCapabilityPlanner()
    selection = planner.select(view=tool_view)
    projection_set = materialize_openai_native_tool_projection_set(
        conversation_scope_kind=conversation_scope_kind,
        scope_subagent_task_id=scope_subagent_task_id,
        wire_api=wire_api,
        tool_versions=selection.direct_tool_versions,
        tool_specs=selection.direct_tool_surface.tool_specs,
        eligibility=native,
    )
    plan = planner.finalize(
        selection=selection,
        direct_projection_set=projection_set,
    )
    return port.prepare_planned_tool_surface(plan=plan, builtin=builtin)


def direct_tool_invocation_context(
    port: DirectKernelToolPort,
    *,
    session_id: str,
    tool_name: str,
    tool_call_id: str,
    attempt_id: str,
    turn_id: str,
    assistant_entry_id: str,
    workspace_id: str = "workspace:test",
    conversation_scope_kind: ModelInputScopeKind = ModelInputScopeKind.ROOT,
    scope_subagent_task_id: str | None = None,
    permission_snapshot: FrozenRunPermissionSnapshot | None = None,
    memory_context: FrozenModelCallMemoryContext | None = None,
) -> tuple[ProcessLocalToolSurfaceBorrow, KernelToolInvocationContext]:
    """Acquire the same narrow surface authority used by the runner."""

    prepared = prepare_test_direct_tool_surface(
        port,
        conversation_scope_kind=conversation_scope_kind,
        scope_subagent_task_id=scope_subagent_task_id,
    )
    borrow = port.borrow_tool_surface(prepared)
    try:
        permission = permission_snapshot or build_run_permission_snapshot(
            snapshot_id=f"permission:{turn_id}",
            requested_mode=DEFAULT_PERMISSION_MODE,
            effective_mode=DEFAULT_PERMISSION_MODE,
            admission_source=RunPermissionAdmissionSource.USER_SUBMISSION,
        )
        borrow.execution_binding(tool_name)
        return borrow, KernelToolInvocationContext(
            session_id=session_id,
            workspace_id=workspace_id,
            turn_id=turn_id,
            assistant_entry_id=assistant_entry_id,
            tool_call_id=tool_call_id,
            attempt_id=attempt_id,
            result_entry_id=f"result:{attempt_id}",
            conversation_scope_kind=conversation_scope_kind.value,
            scope_subagent_task_id=scope_subagent_task_id,
            host_owner_epoch=1,
            authorization_reference="test:authorized",
            effective_permission_mode=permission.effective_mode,
            permission_snapshot_fingerprint=permission.snapshot_fingerprint,
            attempt_permission_snapshot_fingerprint=(
                permission.snapshot_fingerprint
            ),
            surface_borrow=borrow,
            memory_context=memory_context or _enabled_memory_context(),
        )
    except BaseException:
        borrow.close()
        raise


class Round10TestSubagentRuntime:
    """Exact sealed child seed/result seam for retained runner tests.

    Production child execution is always owned by ``KernelSubagentManager``.
    Older runner tests exercise admission/transport failure matrices directly,
    so this test-only carrier supplies the same exact-object cold seed without
    reintroducing the removed flat prompt path.
    """

    def __init__(
        self,
        *,
        session_id: str,
        task_id: str,
        parent_turn_id: str,
        objective: str,
        profile_kind: SubagentProfileKind = SubagentProfileKind.GENERAL_WORKER,
    ) -> None:
        self._task_id = task_id
        self._parent_turn_id = parent_turn_id
        self._objective = objective
        self._profile_kind = profile_kind
        self._subject = build_parent_context_call_subject(
            session_id=session_id,
            caller_turn_id=parent_turn_id,
            provider_input_cut_fingerprint="sha256:test-child-parent-cut",
            continuity_epoch_nonce="epoch:test-child-parent",
            continuity_epoch_revision=0,
            compiled_semantic_input_fingerprint="sha256:test-child-parent-semantic",
            compiled_message_placements_fingerprint=(
                "sha256:test-child-parent-placements"
            ),
            ordered_eligible_units=(),
        )
        self._selection = build_parent_context_selection(
            self._subject,
            mode=SubagentContextMode.NONE,
            last_n_turns=None,
        )

    def profile_kind(self, *, task_id: str) -> SubagentProfileKind:
        self._require_task(task_id)
        return self._profile_kind

    def initial_context_sources(
        self, *, task_id: str
    ) -> tuple[ContextSourceCandidate | ContextSourceAbsentFact, ...]:
        self._require_task(task_id)
        return (
            build_subagent_context_source(
                kind=ContextSourceKind.PARENT_CONTEXT,
                text=self._selection.rendered_body,
                domain_identity={
                    "subject": parent_context_call_subject_identity_digest(
                        self._subject
                    ),
                    "selection": parent_context_selection_identity_digest(
                        self._subject, self._selection
                    ),
                    "source": parent_context_source_identity_digest(
                        self._subject, self._selection
                    ),
                },
            ),
            build_subagent_context_source(
                kind=ContextSourceKind.DEPENDENCY_RESULTS,
                text=None,
                domain_identity=None,
            ),
        )

    def build_initial_seed(
        self,
        *,
        task_id: str,
        dispatch_read: FrozenCanonicalProviderDispatchRead,
    ) -> SubagentInitialSeed:
        self._require_task(task_id)
        return build_subagent_initial_seed(
            dispatch_read=dispatch_read,
            task_id=task_id,
            parent_turn_id=self._parent_turn_id,
            profile_kind=self._profile_kind,
            objective=self._objective,
            parent_call_subject=self._subject,
            parent_context_selection=self._selection,
            dependency_context=None,
        )

    async def consume_mailbox_safe_point(self, task_id: str) -> bool:
        self._require_task(task_id)
        return False

    async def prepare_inferred_completion(
        self, *, task_id: str, entry_id: str, public_text: str
    ):
        self._require_task(task_id)
        digest = "sha256:" + sha256(public_text.encode("utf-8")).hexdigest()
        result = build_subagent_result_public_fact(
            task_id=task_id,
            result_id=f"subagent-result:{sha256(entry_id.encode()).hexdigest()}",
            source=SubagentResultSource.INFERRED,
            producer_entry_id=entry_id,
            summary=public_text or "Task completed without public text.",
            source_assistant_content_digest=digest,
        )
        return object(), result

    async def prepare_explicit_completion(
        self,
        *,
        task_id: str,
        result_entry_id: str,
        arguments: dict[str, object],
    ):
        self._require_task(task_id)
        summary = arguments.get("summary")
        preview = arguments.get("output_preview")
        diagnostics = arguments.get("diagnostics", [])
        if not isinstance(summary, str) or not summary:
            return None
        result = build_subagent_result_public_fact(
            task_id=task_id,
            result_id=(
                "subagent-result:"
                + sha256(f"{task_id}:{result_entry_id}".encode()).hexdigest()
            ),
            source=SubagentResultSource.EXPLICIT,
            producer_entry_id=result_entry_id,
            summary=summary,
            output_preview=preview if isinstance(preview, str) else None,
            diagnostics=diagnostics if isinstance(diagnostics, list) else (),
        )
        acknowledgement = KernelToolResult(
            state="SUCCESS",
            content=json.dumps(
                {"status": "accepted", "task_id": task_id},
                sort_keys=True,
                separators=(",", ":"),
            ).encode(),
        )
        return object(), result, acknowledgement

    async def finish_completion(self, _permit: object, *, committed: bool) -> None:
        if not committed:
            return

    def _require_task(self, task_id: str) -> None:
        if task_id != self._task_id:
            raise ValueError("test subagent runtime received another task")


async def invoke_direct_tool(
    port: DirectKernelToolPort,
    *,
    session_id: str,
    tool_name: str,
    arguments: dict[str, object],
    tool_call_id: str,
    attempt_id: str,
    turn_id: str,
    assistant_entry_id: str,
    workspace_id: str = "workspace:test",
    conversation_scope_kind: ModelInputScopeKind = ModelInputScopeKind.ROOT,
    scope_subagent_task_id: str | None = None,
    memory_context: FrozenModelCallMemoryContext | None = None,
    **kwargs: object,
):
    """Invoke through the same short-lived binding borrow as production."""

    borrow, invocation_context = direct_tool_invocation_context(
        port,
        session_id=session_id,
        tool_name=tool_name,
        tool_call_id=tool_call_id,
        attempt_id=attempt_id,
        turn_id=turn_id,
        assistant_entry_id=assistant_entry_id,
        workspace_id=workspace_id,
        conversation_scope_kind=conversation_scope_kind,
        scope_subagent_task_id=scope_subagent_task_id,
        memory_context=memory_context,
    )
    try:
        return await port.invoke(
            tool_name=tool_name,
            arguments=arguments,
            tool_call_id=tool_call_id,
            attempt_id=attempt_id,
            turn_id=turn_id,
            assistant_entry_id=assistant_entry_id,
            invocation_context=invocation_context,
            **kwargs,
        )
    finally:
        borrow.close()


async def authorize_direct_tool(
    port: DirectKernelToolPort,
    *,
    session_id: str,
    tool_name: str,
    arguments: dict[str, object],
    tool_call_id: str,
    turn_id: str,
    assistant_entry_id: str,
    conversation_scope_kind: ModelInputScopeKind = ModelInputScopeKind.ROOT,
    scope_subagent_task_id: str | None = None,
    permission_snapshot: FrozenRunPermissionSnapshot | None = None,
    memory_context: FrozenModelCallMemoryContext | None = None,
):
    """Authorize under a formally acquired immutable surface borrow."""

    prepared = prepare_test_direct_tool_surface(
        port,
        conversation_scope_kind=conversation_scope_kind,
        scope_subagent_task_id=scope_subagent_task_id,
    )
    borrow = port.borrow_tool_surface(prepared)
    try:
        permission = permission_snapshot or build_run_permission_snapshot(
            snapshot_id=f"permission:{turn_id}",
            requested_mode=DEFAULT_PERMISSION_MODE,
            effective_mode=DEFAULT_PERMISSION_MODE,
            admission_source=RunPermissionAdmissionSource.USER_SUBMISSION,
        )
        return await port.authorize(
            tool_name=tool_name,
            arguments=arguments,
            tool_call_id=tool_call_id,
            turn_id=turn_id,
            assistant_entry_id=assistant_entry_id,
            surface_borrow=borrow,
            permission_snapshot=permission,
            memory_context=memory_context or _enabled_memory_context(),
        )
    finally:
        borrow.close()


def _enabled_memory_context() -> FrozenModelCallMemoryContext:
    return FrozenModelCallMemoryContext(
        FrozenModelVisibleMemoryProvenance(
            ModelVisibleMemoryProvenanceDisposition.COMPLETE,
            (),
        )
    )


def _candidate(
    *,
    kind: ContextSourceKind,
    version: str,
    channel: ContextChannel,
    trust: ContextTrustClass,
    budget: ContextBudgetClass,
    placement: int,
    degradation: int,
    variants: tuple[tuple[ContextRenderMode, str], ...],
) -> ContextSourceCandidate:
    lifecycle = {
        ContextSourceKind.BASE_SYSTEM: ContextSourceLifecycle.EPOCH_ROOT,
        ContextSourceKind.RUNTIME_ENVIRONMENT: ContextSourceLifecycle.SNAPSHOT_ON_CHANGE,
        ContextSourceKind.RUNTIME_CLOCK: ContextSourceLifecycle.CALL_APPEND,
        ContextSourceKind.RUN_PERMISSION: ContextSourceLifecycle.TURN_APPEND,
        ContextSourceKind.PLAN_HANDOFF: ContextSourceLifecycle.ONE_SHOT,
        ContextSourceKind.PLAN_WORKFLOW: ContextSourceLifecycle.SNAPSHOT_ON_CHANGE,
        ContextSourceKind.SKILL_CATALOG: ContextSourceLifecycle.SNAPSHOT_ON_CHANGE,
        ContextSourceKind.MCP_CATALOG: ContextSourceLifecycle.SNAPSHOT_ON_CHANGE,
        ContextSourceKind.ACTIVE_SKILL: ContextSourceLifecycle.ACTIVATION_SNAPSHOT,
        ContextSourceKind.PREVIOUS_TURN_OUTCOME: ContextSourceLifecycle.TURN_APPEND,
        ContextSourceKind.TOOL_OBSERVATION_FRESHNESS: ContextSourceLifecycle.TURN_APPEND,
    }[kind]
    modes = tuple(mode for mode, _text in variants)
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
    rendered = tuple(
        ContextRenderVariant(
            mode,
            text,
            len(text.encode("utf-8")),
            context_fingerprint(
                "context-render-variant:v1", {"mode": mode.value, "text": text}
            ),
        )
        for mode, text in variants
    )
    instance = f"context-source:{kind.value.lower()}"
    semantic = context_fingerprint(
        "context-source-candidate:v1",
        {
            "source_kind": kind.value,
            "source_instance_id": instance,
            "source_contract_fingerprint": contract,
            "variants": tuple(item.semantic_fingerprint for item in rendered),
        },
    )
    return ContextSourceCandidate(
        kind,
        instance,
        version,
        contract,
        semantic,
        channel,
        trust,
        budget,
        placement,
        degradation,
        rendered,
        lifecycle,
        semantic,
    )


__all__ = [
    "Round10TestSubagentRuntime",
    "ScriptedKernelModel",
    "StaticContextSourceCollector",
    "StructuredToolPort",
    "direct_tool_invocation_context",
    "static_canonical_compile_facts",
]
