"""Deterministic structured model-input allocation.

The compiler accepts only immutable provider-neutral facts.  It performs no
I/O and has no authority outside the duration of one function call.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from enum import IntEnum
import heapq
import json
from time import monotonic

from pulsara_agent.llm.estimator import TokenEstimate
from pulsara_agent.llm.input import LLMMessage
from pulsara_agent.model_input.contracts import (
    CompiledSourceDecision,
    CompiledToolResultDecision,
    ContextBudgetClass,
    ContextChannel,
    ContextCompileBudgetReport,
    ContextPublicDiagnosticCode,
    ContextRenderMode,
    ContextSourceCandidate,
    ContextSourceAbsenceKind,
    ContextSourceKind,
    ContextSourceLifecycle,
    ContextTrustClass,
    FrozenCompiledModelInput,
    FrozenToolSpec,
    FrozenProviderInputItem,
    FrozenProviderInputItemKind,
    ModelInputCompileFailureKind,
    StructuredModelInputCompileError,
    StructuredModelInputCompileRequest,
    StructuredModelInputLimits,
    STRUCTURED_MODEL_INPUT_LIMITS,
    ToolResultProviderRenderMode,
    frozen_compiled_model_input_fingerprint,
    provider_input_item_fingerprint,
)
from pulsara_agent.model_input.continuity import (
    FrozenProviderInputAppendCompileResult,
    FrozenProviderInputAppendPlanningInput,
    NewTriggerAnchor,
    ProcessLocalCanonicalFrontier,
    ProcessLocalSourceHead,
    ProviderInputEpochCompatibility,
    ProviderInputEpochResetReason,
    SourceObservationLifecycle,
    SourceObservationPresence,
    encode_runtime_observation,
    provider_input_dispatch_anchor_value,
    provider_input_logical_utf8_bytes,
)
from pulsara_agent.model_input.lowering import (
    LoweredCanonicalItem,
    lower_canonical_item,
    source_variant_message,
)
from pulsara_agent.primitives.context import canonical_json_bytes, context_fingerprint
from pulsara_agent.primitives.plan_workflow import (
    PlanApprovedMaterializationDisposition,
)


COMPILER_CONTRACT_VERSION = (
    "pulsara.structured-model-input-compiler.prefix-continuity.v4"
)


class _SacrificeRank(IntEnum):
    DEBUG = 0
    OPTIONAL = 1
    IMPORTANT = 2
    MUST_KEEP = 3


_SOURCE_POLICY = {
    ContextSourceKind.BASE_SYSTEM: (
        "pulsara.base-system.prefix-continuity.v4",
        ContextChannel.SYSTEM,
        ContextTrustClass.ROOT_INSTRUCTION,
        ContextBudgetClass.MUST_KEEP,
        0,
        0,
        (ContextRenderMode.FULL,),
        ContextSourceLifecycle.EPOCH_ROOT,
    ),
    ContextSourceKind.RUNTIME_ENVIRONMENT: (
        "pulsara.runtime-environment.v2",
        ContextChannel.RUNTIME_OBSERVATION,
        ContextTrustClass.TRUSTED_RUNTIME_FACT,
        ContextBudgetClass.MUST_KEEP,
        10,
        10,
        (ContextRenderMode.FULL, ContextRenderMode.COMPACT),
        ContextSourceLifecycle.SNAPSHOT_ON_CHANGE,
    ),
    ContextSourceKind.RUN_PERMISSION: (
        "pulsara.run-permission.v2",
        ContextChannel.RUNTIME_OBSERVATION,
        ContextTrustClass.AUTHORIZED_RUNTIME_GUIDANCE,
        ContextBudgetClass.MUST_KEEP,
        20,
        12,
        (ContextRenderMode.FULL, ContextRenderMode.COMPACT),
        ContextSourceLifecycle.TURN_APPEND,
    ),
    ContextSourceKind.PLAN_HANDOFF: (
        "pulsara.plan-handoff.v2",
        ContextChannel.RUNTIME_OBSERVATION,
        ContextTrustClass.AUTHORIZED_RUNTIME_GUIDANCE,
        ContextBudgetClass.MUST_KEEP,
        30,
        11,
        (ContextRenderMode.FULL, ContextRenderMode.COMPACT),
        ContextSourceLifecycle.ONE_SHOT,
    ),
    ContextSourceKind.PLAN_WORKFLOW: (
        "pulsara.plan-workflow.v2",
        ContextChannel.RUNTIME_OBSERVATION,
        ContextTrustClass.AUTHORIZED_RUNTIME_GUIDANCE,
        ContextBudgetClass.MUST_KEEP,
        40,
        10,
        (ContextRenderMode.FULL, ContextRenderMode.COMPACT),
        ContextSourceLifecycle.SNAPSHOT_ON_CHANGE,
    ),
    ContextSourceKind.CAPABILITY_CATALOG: (
        "pulsara.capability-catalog.v2",
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
    ContextSourceKind.MCP_CATALOG: (
        "pulsara.mcp-catalog.v2",
        ContextChannel.RUNTIME_OBSERVATION,
        ContextTrustClass.UNTRUSTED_OBSERVATION,
        ContextBudgetClass.IMPORTANT,
        55,
        35,
        (
            ContextRenderMode.FULL,
            ContextRenderMode.COMPACT,
            ContextRenderMode.REF_ONLY,
        ),
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
    ContextSourceKind.PREVIOUS_TURN_OUTCOME: (
        "pulsara.previous-turn-outcome.v1",
        ContextChannel.RUNTIME_OBSERVATION,
        ContextTrustClass.AUTHORIZED_RUNTIME_GUIDANCE,
        ContextBudgetClass.MUST_KEEP,
        45,
        10,
        (ContextRenderMode.FULL, ContextRenderMode.COMPACT),
        ContextSourceLifecycle.TURN_APPEND,
    ),
    ContextSourceKind.TOOL_OBSERVATION_FRESHNESS: (
        "pulsara.tool-observation-freshness.v1",
        ContextChannel.RUNTIME_OBSERVATION,
        ContextTrustClass.TRUSTED_RUNTIME_FACT,
        ContextBudgetClass.MUST_KEEP,
        70,
        10,
        (ContextRenderMode.FULL,),
        ContextSourceLifecycle.TURN_APPEND,
    ),
    ContextSourceKind.RUNTIME_CLOCK: (
        "pulsara.runtime-clock.v2",
        ContextChannel.RUNTIME_OBSERVATION,
        ContextTrustClass.TRUSTED_RUNTIME_FACT,
        ContextBudgetClass.OPTIONAL,
        90,
        80,
        (ContextRenderMode.FULL, ContextRenderMode.COMPACT),
        ContextSourceLifecycle.CALL_APPEND,
    ),
    ContextSourceKind.MEMORY_RESPONSE_PREFERENCE_HEAD: (
        "pulsara.memory-response-preference-head.v1",
        ContextChannel.RUNTIME_OBSERVATION,
        ContextTrustClass.UNTRUSTED_OBSERVATION,
        ContextBudgetClass.IMPORTANT,
        62,
        42,
        (ContextRenderMode.FULL,),
        ContextSourceLifecycle.SNAPSHOT_ON_CHANGE,
    ),
    ContextSourceKind.MEMORY_RECALL: (
        "pulsara.memory-recall.v1",
        ContextChannel.RUNTIME_OBSERVATION,
        ContextTrustClass.UNTRUSTED_OBSERVATION,
        ContextBudgetClass.IMPORTANT,
        65,
        48,
        (
            ContextRenderMode.FULL,
            ContextRenderMode.COMPACT,
            ContextRenderMode.REF_ONLY,
        ),
        ContextSourceLifecycle.SNAPSHOT_ON_CHANGE,
    ),
}

_SOURCE_ABSENCE_POLICY = {
    ContextSourceKind.BASE_SYSTEM: frozenset(),
    ContextSourceKind.RUNTIME_ENVIRONMENT: frozenset(),
    ContextSourceKind.RUNTIME_CLOCK: frozenset(
        {ContextSourceAbsenceKind.UNAVAILABLE}
    ),
    ContextSourceKind.RUN_PERMISSION: frozenset(),
    ContextSourceKind.PLAN_HANDOFF: frozenset(
        {ContextSourceAbsenceKind.NOT_APPLICABLE}
    ),
    ContextSourceKind.PLAN_WORKFLOW: frozenset(
        {ContextSourceAbsenceKind.EXPLICIT_EMPTY}
    ),
    ContextSourceKind.CAPABILITY_CATALOG: frozenset(
        {
            ContextSourceAbsenceKind.EXPLICIT_EMPTY,
            ContextSourceAbsenceKind.UNAVAILABLE,
        }
    ),
    ContextSourceKind.MCP_CATALOG: frozenset(
        {
            ContextSourceAbsenceKind.NOT_APPLICABLE,
            ContextSourceAbsenceKind.UNAVAILABLE,
        }
    ),
    ContextSourceKind.ACTIVE_SKILL: frozenset(
        {
            ContextSourceAbsenceKind.NOT_APPLICABLE,
            ContextSourceAbsenceKind.EXPLICIT_EMPTY,
            ContextSourceAbsenceKind.UNAVAILABLE,
        }
    ),
    ContextSourceKind.PREVIOUS_TURN_OUTCOME: frozenset(
        {ContextSourceAbsenceKind.EXPLICIT_EMPTY}
    ),
    ContextSourceKind.TOOL_OBSERVATION_FRESHNESS: frozenset(),
    ContextSourceKind.MEMORY_RESPONSE_PREFERENCE_HEAD: frozenset(
        {
            ContextSourceAbsenceKind.NOT_APPLICABLE,
            ContextSourceAbsenceKind.EXPLICIT_EMPTY,
            ContextSourceAbsenceKind.UNAVAILABLE,
        }
    ),
    ContextSourceKind.MEMORY_RECALL: frozenset(
        {
            ContextSourceAbsenceKind.NOT_APPLICABLE,
            ContextSourceAbsenceKind.EXPLICIT_EMPTY,
            ContextSourceAbsenceKind.UNAVAILABLE,
        }
    ),
}


@dataclass(slots=True)
class _SourceState:
    candidate: ContextSourceCandidate
    selected: int = 0
    omitted: bool = False
    exhausted: bool = False

    def text(self) -> str | None:
        return None if self.omitted else self.candidate.variants[self.selected].text

    def mode(self) -> ContextRenderMode | None:
        return None if self.omitted else self.candidate.variants[self.selected].mode

    def advance(self) -> bool:
        if self.selected + 1 < len(self.candidate.variants):
            self.selected += 1
            return True
        if (
            self.candidate.budget_class is not ContextBudgetClass.MUST_KEEP
            and not self.omitted
        ):
            self.omitted = True
            return True
        return False


@dataclass(slots=True)
class _ToolState:
    lowered: LoweredCanonicalItem
    current_turn: bool
    selected: int = 0
    exhausted: bool = False

    @property
    def item(self):
        return self.lowered.source

    def message(self) -> LLMMessage:
        return self.lowered.tool_result_variants[self.selected].message

    def mode(self) -> ToolResultProviderRenderMode:
        return self.lowered.tool_result_variants[self.selected].mode

    def advance(self) -> bool:
        if self.selected + 1 >= len(self.lowered.tool_result_variants):
            return False
        self.selected += 1
        return True


@dataclass(frozen=True, slots=True)
class _Layout:
    system_prompt: str
    messages: tuple[LLMMessage, ...]
    estimate: TokenEstimate


@dataclass(frozen=True, slots=True)
class _CompileDeadline:
    value: float | None

    def check(self) -> None:
        if self.value is not None and monotonic() >= self.value:
            raise StructuredModelInputCompileError(
                ModelInputCompileFailureKind.DEADLINE_EXPIRED
            )


@dataclass(frozen=True, slots=True)
class _AppendSourceEmission:
    source_kind: ContextSourceKind
    placement_ordinal: int
    presence: SourceObservationPresence
    lifecycle: SourceObservationLifecycle
    semantic_fingerprint: str
    contract_version: str
    trust_class: ContextTrustClass
    state: _SourceState | None = None
    fixed_message: LLMMessage | None = None
    requires_installed_replacement: bool = False

    def __post_init__(self) -> None:
        if (self.state is None) == (self.fixed_message is None):
            raise ValueError("append source emission union is invalid")


class StructuredModelInputCompiler:
    def __init__(
        self,
        *,
        limits: StructuredModelInputLimits = STRUCTURED_MODEL_INPUT_LIMITS,
    ) -> None:
        self._limits = limits

    def compile(
        self,
        request: StructuredModelInputCompileRequest,
        *,
        deadline_monotonic: float | None = None,
    ) -> FrozenCompiledModelInput:
        deadline = _CompileDeadline(deadline_monotonic)
        deadline.check()
        self._validate_canonical_input_bound(request)
        deadline.check()
        self._validate_sources(request)
        surface = request.compile_binding.tool_surface
        self._validate_tool_surface(request)
        deadline.check()
        artifact_read_available = any(
            tool.name == "artifact_read" for tool in surface.tool_specs
        )
        canonical_items, materialized_plan_bytes = self._materialize_approved_plan(
            request
        )
        citation_handles = dict(request.memory_citation_handles)
        lowered_items: list[LoweredCanonicalItem] = []
        for item in canonical_items:
            deadline.check()
            lowered_items.append(
                lower_canonical_item(
                    item,
                    artifact_read_available=artifact_read_available,
                    limits=self._limits,
                    memory_citation_handles=citation_handles,
                )
            )
        lowered = tuple(lowered_items)
        deadline.check()
        source_states = [_SourceState(item) for item in request.sources.candidates]
        tool_states = [
            _ToolState(
                item,
                current_turn=(
                    item.source.source_turn_id
                    == request.canonical_input.identity.turn_id
                ),
            )
            for item in lowered
            if item.tool_result_variants
        ]
        if len(tool_states) > self._limits.maximum_tool_result_decisions:
            raise StructuredModelInputCompileError(
                ModelInputCompileFailureKind.COMPILE_WORKING_SET_EXCEEDED
            )
        self._validate_physical_bounds(
            request,
            lowered,
            materialized_plan_bytes=materialized_plan_bytes,
        )
        estimator = request.compile_binding.estimator
        diagnostics = [item.code for item in request.sources.diagnostics]

        layout = self._layout(
            request,
            lowered=lowered,
            sources=source_states,
            tools=tool_states,
            deadline=deadline,
        )
        budget = request.compile_binding.effective_input_budget_tokens
        degraded_source_ids: set[str] = set()
        degraded_tool_ids: set[str] = set()

        current_total = layout.estimate.total_input_tokens
        candidates: list[tuple[tuple[object, ...], int, str, object]] = []
        serial = 0

        def offer(kind: str, unit: _SourceState | _ToolState) -> None:
            nonlocal serial
            if kind == "source":
                assert isinstance(unit, _SourceState)
                key = self._source_degradation_key(unit.candidate)
            else:
                assert isinstance(unit, _ToolState)
                key = self._tool_degradation_key(unit)
            heapq.heappush(candidates, (key, serial, kind, unit))
            serial += 1

        for state in source_states:
            if self._source_can_advance(state):
                offer("source", state)
        for state in tool_states:
            if self._tool_can_advance(state):
                offer("tool", state)

        while current_total > budget:
            deadline.check()
            if not candidates:
                raise StructuredModelInputCompileError(
                    self._minimum_budget_failure(
                        request, lowered, source_states, tool_states
                    )
                )
            _key, _serial, kind, unit = heapq.heappop(candidates)
            if kind == "source":
                state = unit
                assert isinstance(state, _SourceState)
                reduction = self._advance_source_to_progress(
                    request,
                    state=state,
                    all_states=source_states,
                    diagnostics=diagnostics,
                    deadline=deadline,
                )
                if reduction > 0:
                    current_total -= reduction
                    degraded_source_ids.add(state.candidate.source_instance_id)
                    if self._source_can_advance(state):
                        offer("source", state)
            else:
                state = unit
                assert isinstance(state, _ToolState)
                reduction = self._advance_tool_to_progress(
                    request,
                    state=state,
                    diagnostics=diagnostics,
                    deadline=deadline,
                )
                if reduction > 0:
                    current_total -= reduction
                    degraded_tool_ids.add(state.item.source_entry_id or "")
                    if self._tool_can_advance(state):
                        offer("tool", state)

        layout = self._layout(
            request,
            lowered=lowered,
            sources=source_states,
            tools=tool_states,
            deadline=deadline,
        )
        if layout.estimate.total_input_tokens != current_total:
            raise StructuredModelInputCompileError(
                ModelInputCompileFailureKind.FINAL_ESTIMATE_MISMATCH
            )

        full = self._estimate_frozen_input(
            request,
            system_prompt=layout.system_prompt,
            messages=layout.messages,
            tools=surface.tool_specs,
            deadline=deadline,
        )
        if full != layout.estimate:
            raise StructuredModelInputCompileError(
                ModelInputCompileFailureKind.FINAL_ESTIMATE_MISMATCH
            )
        source_decisions = tuple(
            CompiledSourceDecision(
                source_kind=state.candidate.source_kind,
                source_instance_fingerprint=state.candidate.source_semantic_fingerprint,
                channel=state.candidate.channel,
                selected_mode=state.mode(),
                included=not state.omitted,
                estimated_tokens=self._selected_source_tokens(
                    request, state, source_states
                ),
                reason_code=(
                    "OMITTED_FOR_BUDGET"
                    if state.omitted
                    else "SELECTED_FULL"
                    if state.selected == 0
                    else "DEGRADED_FOR_BUDGET"
                ),
            )
            for state in sorted(
                source_states, key=lambda item: self._placement_key(item.candidate)
            )
        )
        tool_decisions = tuple(
            CompiledToolResultDecision(
                source_entry_fingerprint=context_fingerprint(
                    "compiled-tool-result-source:v1",
                    {
                        "entry_id": state.item.source_entry_id,
                        "sequence": state.item.source_entry_sequence,
                        "tool_call_id": state.item.tool_call_id,
                    },
                ),
                current_turn=state.item.source_turn_id
                == request.canonical_input.identity.turn_id,
                selected_mode=state.mode(),
                estimated_tokens=estimator.estimate_message(state.message()),
                reason_code=(
                    "SELECTED_FULL" if state.selected == 0 else "DEGRADED_FOR_BUDGET"
                ),
            )
            for state in tool_states
        )
        omitted_sources = sum(state.omitted for state in source_states)
        omitted_tools = sum(
            state.mode() is ToolResultProviderRenderMode.OMITTED_BODY
            for state in tool_states
        )
        if degraded_source_ids:
            self._add_diagnostic(
                diagnostics, ContextPublicDiagnosticCode.SOURCE_DEGRADED
            )
        if omitted_sources:
            self._add_diagnostic(
                diagnostics, ContextPublicDiagnosticCode.SOURCE_OMITTED
            )
        if degraded_tool_ids:
            self._add_diagnostic(
                diagnostics, ContextPublicDiagnosticCode.TOOL_RESULT_DEGRADED
            )
        if omitted_tools:
            self._add_diagnostic(
                diagnostics, ContextPublicDiagnosticCode.TOOL_RESULT_BODY_OMITTED
            )
        if (
            len(source_decisions) + len(tool_decisions)
            > self._limits.maximum_decision_samples
        ):
            self._add_diagnostic(
                diagnostics, ContextPublicDiagnosticCode.DECISION_SAMPLE_TRUNCATED
            )
        diagnostic_codes = tuple(dict.fromkeys(diagnostics))[
            : self._limits.maximum_diagnostics
        ]
        if (
            len(canonical_json_bytes(tuple(item.value for item in diagnostic_codes)))
            > self._limits.maximum_public_diagnostic_bytes
        ):
            raise StructuredModelInputCompileError(
                ModelInputCompileFailureKind.COMPILE_WORKING_SET_EXCEEDED
            )
        decision_digest = context_fingerprint(
            "model-input-decisions-public:v1",
            {
                "sources": tuple(
                    (
                        d.source_kind.value,
                        None if d.selected_mode is None else d.selected_mode.value,
                        d.included,
                        d.reason_code,
                    )
                    for d in source_decisions
                ),
                "tool_results": tuple(
                    (d.current_turn, d.selected_mode.value, d.reason_code)
                    for d in tool_decisions
                ),
            },
        )
        source_tokens = self._context_source_tokens(request, layout, source_states)
        report = ContextCompileBudgetReport(
            compiler_contract_version=COMPILER_CONTRACT_VERSION,
            estimator_fingerprint=request.compile_binding.estimator_fingerprint,
            target_fingerprint=request.compile_binding.target_fact.target_fingerprint,
            tool_surface_fingerprint=surface.surface_fingerprint,
            effective_input_budget_tokens=budget,
            system_tokens=full.system_tokens,
            message_tokens=full.message_tokens,
            tool_tokens=full.tool_tokens,
            envelope_tokens=full.envelope_tokens,
            total_input_tokens=full.total_input_tokens,
            protected_transcript_tokens=0,
            protected_prefix_message_count=0,
            protected_prefix_logical_utf8_bytes=0,
            protected_prefix_fingerprint=None,
            context_source_tokens=source_tokens,
            degraded_source_count=len(degraded_source_ids),
            omitted_source_count=omitted_sources,
            degraded_tool_result_count=len(degraded_tool_ids),
            omitted_tool_result_body_count=omitted_tools,
            decision_digest=decision_digest,
        )
        compiled_fingerprint = frozen_compiled_model_input_fingerprint(
            context_id=request.context_id,
            canonical_input_identity=request.canonical_input.identity,
            system_prompt=layout.system_prompt,
            messages=layout.messages,
            tools=surface.tool_specs,
            final_estimate=full,
            source_decisions=source_decisions,
            tool_result_decisions=tool_decisions,
            budget_report=report,
            diagnostic_codes=diagnostic_codes,
            source_collection_fingerprint=request.sources.collection_fingerprint,
            compile_binding_fingerprint=request.compile_binding.binding_fingerprint,
        )
        deadline.check()
        return FrozenCompiledModelInput(
            context_id=request.context_id,
            canonical_input_identity=request.canonical_input.identity,
            system_prompt=layout.system_prompt,
            messages=layout.messages,
            tools=surface.tool_specs,
            final_estimate=full,
            source_decisions=source_decisions,
            tool_result_decisions=tool_decisions,
            budget_report=report,
            diagnostic_codes=diagnostic_codes,
            source_collection_fingerprint=request.sources.collection_fingerprint,
            compiled_semantic_fingerprint=compiled_fingerprint,
            compile_binding_fingerprint=request.compile_binding.binding_fingerprint,
        )

    def compile_append(
        self,
        request: StructuredModelInputCompileRequest,
        *,
        planning: FrozenProviderInputAppendPlanningInput,
        compatibility: ProviderInputEpochCompatibility,
        deadline_monotonic: float | None = None,
    ) -> FrozenProviderInputAppendCompileResult:
        """Compile one causally appended input without relowering old messages."""

        deadline = _CompileDeadline(deadline_monotonic)
        deadline.check()
        self._validate_canonical_input_bound(request)
        deadline.check()
        identity = request.canonical_input.identity
        if (
            planning.scope.session_id != identity.session_id
            or planning.scope.scope_kind is not identity.conversation_scope_kind
            or planning.scope.scope_subagent_task_id != identity.scope_subagent_task_id
            or compatibility.context_base_semantic_identity
            != request.canonical_facts.context_binding_fact.context_base_semantic_identity
        ):
            raise StructuredModelInputCompileError(
                ModelInputCompileFailureKind.SOURCE_CONTRACT_INVALID
            )
        fingerprints_list: list[str] = []
        for item in request.canonical_input.items:
            deadline.check()
            fingerprints_list.append(provider_input_item_fingerprint(item))
        fingerprints = tuple(fingerprints_list)
        predecessor = planning.predecessor_view
        predecessor_count = 0 if predecessor is None else len(
            predecessor.canonical_frontier.ordered_item_fingerprints
        )
        reset_reason = _compatibility_reset_reason(predecessor, compatibility)
        same_base = predecessor is None or (
            predecessor.canonical_frontier.context_base_semantic_identity
            == request.canonical_facts.context_binding_fact.context_base_semantic_identity
        )
        prefix_matches = predecessor is None or (
            fingerprints[:predecessor_count]
            == predecessor.canonical_frontier.ordered_item_fingerprints
        )
        # A provider/tool/compiler reset may rematerialize the provider view,
        # but it never authorizes rewriting canonical items inside the same
        # context base.  Only an explicit context-base replacement has no
        # item-prefix relationship to the old frontier.
        if predecessor is not None and same_base and not prefix_matches:
            raise StructuredModelInputCompileError(
                ModelInputCompileFailureKind.CANONICAL_PREFIX_CONFLICT
            )
        expected_planning_delta = (
            fingerprints[predecessor_count:]
            if predecessor is not None and same_base and prefix_matches
            else fingerprints
        )
        if expected_planning_delta != planning.canonical_delta_fingerprints:
            raise StructuredModelInputCompileError(
                ModelInputCompileFailureKind.SOURCE_CONTRACT_INVALID
            )
        frontier = ProcessLocalCanonicalFrontier(
            latest_context_binding_revision_id=(
                request.canonical_facts.context_binding_fact.binding_revision_id
            ),
            context_base_semantic_identity=(
                request.canonical_facts.context_binding_fact.context_base_semantic_identity
            ),
            through_sequence=identity.provider_input_through_sequence,
            ordered_item_fingerprints=fingerprints,
        )
        if predecessor is not None and reset_reason is None:
            try:
                predecessor.canonical_frontier.require_prefix_of(frontier)
            except ValueError as exc:
                raise StructuredModelInputCompileError(
                    ModelInputCompileFailureKind.CANONICAL_PREFIX_CONFLICT
                ) from exc
            return self._compile_compatible_append(
                request,
                planning=planning,
                predecessor=predecessor,
                frontier=frontier,
                deadline=deadline,
            )

        # The ordinary compiler remains the unique suffix allocator.  Its
        # decisions are applied only to new canonical items and newly emitted
        # source observations; installed messages are never taken from it.
        fresh = self.compile(request, deadline_monotonic=deadline.value)
        deadline.check()
        all_materialized, _ = self._materialize_approved_plan(request)
        old_count = 0 if reset_reason is not None else predecessor_count
        delta_items = all_materialized[old_count:]
        artifact_read_available = any(
            tool.name == "artifact_read"
            for tool in request.compile_binding.tool_surface.tool_specs
        )
        lowered_delta_values: list[LoweredCanonicalItem] = []
        citation_handles = dict(request.memory_citation_handles)
        for item in delta_items:
            deadline.check()
            lowered_delta_values.append(
                lower_canonical_item(
                    item,
                    artifact_read_available=artifact_read_available,
                    limits=self._limits,
                    memory_citation_handles=citation_handles,
                )
            )
        lowered_delta = tuple(lowered_delta_values)
        tool_modes = {
            decision.source_entry_fingerprint: decision.selected_mode
            for decision in fresh.tool_result_decisions
        }
        delta_messages = tuple(
            _selected_lowered_message(item, tool_modes=tool_modes)
            for item in lowered_delta
        )

        previous_heads = {
            item.source_kind: item
            for item in (
                ()
                if predecessor is None or reset_reason is not None
                else predecessor.source_heads
            )
        }
        source_decisions = {
            item.source_kind: item for item in fresh.source_decisions
        }
        observation_messages: list[tuple[int, str, LLMMessage]] = []
        resulting_heads = dict(previous_heads)
        candidates = {
            item.source_kind: item for item in request.sources.candidates
        }
        absent = {item.source_kind: item for item in request.sources.absent_facts}
        for kind in ContextSourceKind:
            if kind is ContextSourceKind.BASE_SYSTEM:
                continue
            candidate = candidates.get(kind)
            absence = absent.get(kind)
            previous = previous_heads.get(kind)
            decision = source_decisions.get(kind)
            presence: SourceObservationPresence | None = None
            lifecycle: SourceObservationLifecycle | None = None
            body = ""
            contract_version: str
            trust: ContextTrustClass
            semantic: str
            placement: int
            if candidate is not None:
                if decision is None or not decision.included:
                    continue
                selected = next(
                    (
                        variant
                        for variant in candidate.variants
                        if variant.mode is decision.selected_mode
                    ),
                    None,
                )
                if selected is None:
                    raise StructuredModelInputCompileError(
                        ModelInputCompileFailureKind.SOURCE_CONTRACT_INVALID
                    )
                presence = SourceObservationPresence.VALUE
                lifecycle = _observation_lifecycle(candidate.lifecycle)
                body = selected.text
                contract_version = candidate.source_contract_version
                trust = candidate.trust_class
                placement = candidate.placement_ordinal
                semantic = _source_occurrence_fingerprint(
                    candidate.domain_semantic_fingerprint,
                    lifecycle=candidate.lifecycle,
                    turn_id=identity.turn_id,
                    model_call_index=request.model_call_index,
                    dispatch_anchor=planning.dispatch_anchor,
                )
                if candidate.lifecycle is ContextSourceLifecycle.CALL_APPEND:
                    should_append = True
                else:
                    should_append = previous is None or (
                        previous.presence is not presence
                        or previous.semantic_fingerprint != semantic
                    )
            elif absence is not None:
                contract_version = absence.source_contract_version
                trust = absence.trust_class
                placement = absence.placement_ordinal
                semantic = _source_occurrence_fingerprint(
                    absence.domain_semantic_fingerprint,
                    lifecycle=absence.lifecycle,
                    turn_id=identity.turn_id,
                    model_call_index=request.model_call_index,
                    dispatch_anchor=planning.dispatch_anchor,
                )
                if absence.absence_kind is ContextSourceAbsenceKind.NOT_APPLICABLE:
                    continue
                if absence.absence_kind is ContextSourceAbsenceKind.UNAVAILABLE:
                    # RUNTIME_CLOCK is the only currently optional unavailable
                    # source and is intentionally omitted with its diagnostic.
                    if absence.lifecycle is ContextSourceLifecycle.CALL_APPEND:
                        continue
                    presence = SourceObservationPresence.UNAVAILABLE
                    lifecycle = SourceObservationLifecycle.UNAVAILABLE
                else:
                    presence = SourceObservationPresence.CLEARED
                    lifecycle = SourceObservationLifecycle.CLEARED
                # Empty/unavailable state has no provider-visible meaning until
                # it invalidates an installed value.  Repeating the same closed
                # absence is a no-op even across a later turn/activation.
                should_append = previous is not None and previous.presence is not presence
            else:
                continue
            if not should_append or presence is None or lifecycle is None:
                continue
            message = encode_runtime_observation(
                source_kind=kind,
                trust_class=trust,
                lifecycle=lifecycle,
                presence=presence,
                contract_version=contract_version,
                body=body,
            )
            observation_messages.append((placement, kind.value, message))
            observation_fingerprint = context_fingerprint(
                "pulsara:installed-runtime-observation:v1",
                {"message": _llm_message_value(message)},
            )
            resulting_heads[kind] = ProcessLocalSourceHead(
                source_kind=kind,
                presence=presence,
                semantic_fingerprint=semantic,
                installed_observation_fingerprint=observation_fingerprint,
                last_emitted_turn_id=identity.turn_id,
                last_emitted_model_call_index=request.model_call_index,
            )
        ordered_observations = tuple(
            item[2] for item in sorted(observation_messages, key=lambda item: item[:2])
        )

        prefix_messages = () if predecessor is None or reset_reason is not None else (
            predecessor.messages
        )
        system_prompt = (
            fresh.system_prompt
            if predecessor is None or reset_reason is not None
            else predecessor.system_prompt
        )
        tools = (
            fresh.tools
            if predecessor is None or reset_reason is not None
            else predecessor.tools
        )
        if isinstance(planning.dispatch_anchor, NewTriggerAnchor):
            indexes = tuple(
                index
                for index, item in enumerate(delta_items)
                if item.source_entry_id == planning.dispatch_anchor.source_entry_id
                and provider_input_item_fingerprint(item)
                == planning.dispatch_anchor.provider_input_item_fingerprint
            )
            if len(indexes) != 1:
                raise StructuredModelInputCompileError(
                    ModelInputCompileFailureKind.SOURCE_CONTRACT_INVALID
                )
            trigger_index = indexes[0]
            suffix_messages = (
                *delta_messages[:trigger_index],
                *ordered_observations,
                *delta_messages[trigger_index:],
            )
        else:
            suffix_messages = (*delta_messages, *ordered_observations)
        messages = (*prefix_messages, *suffix_messages)
        estimate = self._estimate_frozen_input(
            request,
            system_prompt=system_prompt,
            messages=messages,
            tools=tools,
            deadline=deadline,
        )
        if estimate.total_input_tokens > request.compile_binding.effective_input_budget_tokens:
            raise StructuredModelInputCompileError(
                ModelInputCompileFailureKind.PROTECTED_TRANSCRIPT_EXCEEDS_BUDGET
            )
        if provider_input_logical_utf8_bytes(
            system_prompt=system_prompt, tools=tools, messages=messages
        ) > (64 << 20):
            raise StructuredModelInputCompileError(
                ModelInputCompileFailureKind.COMPILE_WORKING_SET_EXCEEDED
            )
        decision_digest = context_fingerprint(
            "pulsara:model-input-append-decisions:v1",
            {
                "predecessor": (
                    None
                    if predecessor is None
                    else predecessor.semantic_prefix_fingerprint
                ),
                "observations": tuple(
                    item.installed_observation_fingerprint
                    for item in resulting_heads.values()
                ),
                "delta": planning.canonical_delta_fingerprints,
            },
        )
        report = replace(
            fresh.budget_report,
            system_tokens=estimate.system_tokens,
            message_tokens=estimate.message_tokens,
            tool_tokens=estimate.tool_tokens,
            envelope_tokens=estimate.envelope_tokens,
            total_input_tokens=estimate.total_input_tokens,
            protected_transcript_tokens=estimate.message_tokens,
            context_source_tokens=sum(
                request.compile_binding.estimator.estimate_message(item)
                for item in ordered_observations
            ),
            decision_digest=decision_digest,
        )
        compiled_fingerprint = frozen_compiled_model_input_fingerprint(
            context_id=request.context_id,
            canonical_input_identity=identity,
            system_prompt=system_prompt,
            messages=messages,
            tools=tools,
            final_estimate=estimate,
            source_decisions=fresh.source_decisions,
            tool_result_decisions=fresh.tool_result_decisions,
            budget_report=report,
            diagnostic_codes=fresh.diagnostic_codes,
            source_collection_fingerprint=request.sources.collection_fingerprint,
            compile_binding_fingerprint=request.compile_binding.binding_fingerprint,
        )
        deadline.check()
        compiled = FrozenCompiledModelInput(
            context_id=request.context_id,
            canonical_input_identity=identity,
            system_prompt=system_prompt,
            messages=messages,
            tools=tools,
            final_estimate=estimate,
            source_decisions=fresh.source_decisions,
            tool_result_decisions=fresh.tool_result_decisions,
            budget_report=report,
            diagnostic_codes=fresh.diagnostic_codes,
            source_collection_fingerprint=request.sources.collection_fingerprint,
            compiled_semantic_fingerprint=compiled_fingerprint,
            compile_binding_fingerprint=request.compile_binding.binding_fingerprint,
        )
        return FrozenProviderInputAppendCompileResult(
            compiled_input=compiled,
            canonical_frontier=frontier,
            source_heads=tuple(
                resulting_heads[kind]
                for kind in sorted(resulting_heads, key=lambda item: item.value)
            ),
            appended_message_count=len(suffix_messages),
            reset_reason=reset_reason,
        )

    def _compile_compatible_append(
        self,
        request: StructuredModelInputCompileRequest,
        *,
        planning: FrozenProviderInputAppendPlanningInput,
        predecessor: object,
        frontier: ProcessLocalCanonicalFrontier,
        deadline: _CompileDeadline,
    ) -> FrozenProviderInputAppendCompileResult:
        """Allocate only the not-yet-installed suffix of a compatible epoch."""

        deadline.check()
        self._validate_sources(request)
        self._validate_tool_surface(request)
        previous_messages = predecessor.messages
        previous_heads = {item.source_kind: item for item in predecessor.source_heads}
        old_count = len(predecessor.canonical_frontier.ordered_item_fingerprints)
        delta_items = request.canonical_input.items[old_count:]
        self._require_provider_safe_delta(delta_items)
        artifact_read_available = any(
            tool.name == "artifact_read"
            for tool in request.compile_binding.tool_surface.tool_specs
        )
        lowered_delta_values: list[LoweredCanonicalItem] = []
        citation_handles = dict(request.memory_citation_handles)
        for item in delta_items:
            deadline.check()
            lowered_delta_values.append(
                lower_canonical_item(
                    item,
                    artifact_read_available=artifact_read_available,
                    limits=self._limits,
                    memory_citation_handles=citation_handles,
                )
            )
        lowered_delta = tuple(lowered_delta_values)
        tool_states = [
            _ToolState(
                item,
                current_turn=(
                    item.source.source_turn_id
                    == request.canonical_input.identity.turn_id
                ),
            )
            for item in lowered_delta
            if item.tool_result_variants
        ]
        if len(tool_states) > self._limits.maximum_tool_result_decisions:
            raise StructuredModelInputCompileError(
                ModelInputCompileFailureKind.COMPILE_WORKING_SET_EXCEEDED
            )

        emissions: list[_AppendSourceEmission] = []
        for candidate in request.sources.candidates:
            deadline.check()
            if candidate.source_kind is ContextSourceKind.BASE_SYSTEM:
                continue
            previous = previous_heads.get(candidate.source_kind)
            semantic = _source_occurrence_fingerprint(
                candidate.domain_semantic_fingerprint,
                lifecycle=candidate.lifecycle,
                turn_id=request.canonical_input.identity.turn_id,
                model_call_index=request.model_call_index,
                dispatch_anchor=planning.dispatch_anchor,
            )
            presence = SourceObservationPresence.VALUE
            should_append = (
                candidate.lifecycle is ContextSourceLifecycle.CALL_APPEND
                or previous is None
                or previous.presence is not presence
                or previous.semantic_fingerprint != semantic
            )
            if not should_append:
                continue
            state = _SourceState(candidate)
            emissions.append(
                _AppendSourceEmission(
                    source_kind=candidate.source_kind,
                    placement_ordinal=candidate.placement_ordinal,
                    presence=presence,
                    lifecycle=_observation_lifecycle(candidate.lifecycle),
                    semantic_fingerprint=semantic,
                    contract_version=candidate.source_contract_version,
                    trust_class=candidate.trust_class,
                    state=state,
                    requires_installed_replacement=(
                        previous is not None
                        and candidate.lifecycle
                        in {
                            ContextSourceLifecycle.SNAPSHOT_ON_CHANGE,
                            ContextSourceLifecycle.TURN_APPEND,
                            ContextSourceLifecycle.TURN_SNAPSHOT,
                            ContextSourceLifecycle.ACTIVATION_SNAPSHOT,
                        }
                    ),
                )
            )

        for absence in request.sources.absent_facts:
            deadline.check()
            if absence.source_kind is ContextSourceKind.BASE_SYSTEM:
                raise StructuredModelInputCompileError(
                    ModelInputCompileFailureKind.REQUIRED_SOURCE_UNAVAILABLE
                )
            previous = previous_heads.get(absence.source_kind)
            if (
                absence.lifecycle is ContextSourceLifecycle.CALL_APPEND
                and absence.absence_kind is ContextSourceAbsenceKind.UNAVAILABLE
            ):
                continue
            if absence.absence_kind is ContextSourceAbsenceKind.NOT_APPLICABLE:
                continue
            if absence.absence_kind is ContextSourceAbsenceKind.UNAVAILABLE:
                presence = SourceObservationPresence.UNAVAILABLE
                lifecycle = SourceObservationLifecycle.UNAVAILABLE
            else:
                presence = SourceObservationPresence.CLEARED
                lifecycle = SourceObservationLifecycle.CLEARED
            semantic = _source_occurrence_fingerprint(
                absence.domain_semantic_fingerprint,
                lifecycle=absence.lifecycle,
                turn_id=request.canonical_input.identity.turn_id,
                model_call_index=request.model_call_index,
                dispatch_anchor=planning.dispatch_anchor,
            )
            # An absent source that has never been installed has no stale state
            # to invalidate.  In particular, an empty catalog/skill does not
            # create a fake initial head merely to say nothing.
            if previous is None:
                continue
            if (
                previous.presence is presence
                and previous.semantic_fingerprint == semantic
            ):
                continue
            message = encode_runtime_observation(
                source_kind=absence.source_kind,
                trust_class=absence.trust_class,
                lifecycle=lifecycle,
                presence=presence,
                contract_version=absence.source_contract_version,
                body="",
            )
            emissions.append(
                _AppendSourceEmission(
                    source_kind=absence.source_kind,
                    placement_ordinal=absence.placement_ordinal,
                    presence=presence,
                    lifecycle=lifecycle,
                    semantic_fingerprint=semantic,
                    contract_version=absence.source_contract_version,
                    trust_class=absence.trust_class,
                    fixed_message=message,
                    requires_installed_replacement=True,
                )
            )

        source_states = [item.state for item in emissions if item.state is not None]
        typed_source_states = [item for item in source_states if item is not None]
        aggregate_source_variants = sum(
            variant.utf8_bytes
            for state in typed_source_states
            for variant in state.candidate.variants
        )
        aggregate_tool_variants = sum(
            variant.utf8_bytes
            for state in tool_states
            for variant in state.lowered.tool_result_variants
        )
        fixed_observation_bytes = sum(
            _message_logical_utf8_bytes(item.fixed_message)
            for item in emissions
            if item.fixed_message is not None
        )
        delta_carrier_bytes = sum(
            _message_logical_utf8_bytes(item.fixed_message)
            for item in lowered_delta
            if item.fixed_message is not None
        )
        if (
            request.canonical_input.canonical_utf8_bytes
            > self._limits.maximum_canonical_input_bytes
            or predecessor.logical_utf8_bytes
            + aggregate_source_variants
            + aggregate_tool_variants
            + fixed_observation_bytes
            + delta_carrier_bytes
            > self._limits.maximum_compile_working_set_bytes
        ):
            raise StructuredModelInputCompileError(
                ModelInputCompileFailureKind.COMPILE_WORKING_SET_EXCEEDED
            )

        diagnostics = [item.code for item in request.sources.diagnostics]
        layout = self._append_layout(
            request,
            planning=planning,
            predecessor=predecessor,
            lowered_delta=lowered_delta,
            emissions=emissions,
            tools=tool_states,
            deadline=deadline,
        )
        budget = request.compile_binding.effective_input_budget_tokens
        current_total = layout.estimate.total_input_tokens
        degraded_source_ids: set[str] = set()
        degraded_tool_ids: set[str] = set()
        heap: list[tuple[tuple[object, ...], int, str, object]] = []
        serial = 0

        def source_can_advance(state: _SourceState) -> bool:
            emission = next(item for item in emissions if item.state is state)
            if state.selected + 1 < len(state.candidate.variants):
                return not state.exhausted
            return (
                not state.exhausted
                and not state.omitted
                and state.candidate.budget_class is not ContextBudgetClass.MUST_KEEP
                and not emission.requires_installed_replacement
            )

        def offer(kind: str, unit: _SourceState | _ToolState) -> None:
            nonlocal serial
            key = (
                self._source_degradation_key(unit.candidate)
                if isinstance(unit, _SourceState)
                else self._tool_degradation_key(unit)
            )
            heapq.heappush(heap, (key, serial, kind, unit))
            serial += 1

        for state in typed_source_states:
            if source_can_advance(state):
                offer("source", state)
        for state in tool_states:
            if self._tool_can_advance(state):
                offer("tool", state)

        while current_total > budget:
            deadline.check()
            if not heap:
                failure = (
                    ModelInputCompileFailureKind.STATEFUL_SOURCE_REPLACEMENT_OVER_BUDGET
                    if any(item.requires_installed_replacement for item in emissions)
                    else ModelInputCompileFailureKind.PREFIX_EPOCH_BUDGET_EXHAUSTED
                )
                raise StructuredModelInputCompileError(failure)
            _key, _serial, kind, unit = heapq.heappop(heap)
            if kind == "source":
                state = unit
                assert isinstance(state, _SourceState)
                before = current_total
                original = (state.selected, state.omitted)
                if not state.advance():
                    state.exhausted = True
                    continue
                trial = self._append_layout(
                    request,
                    planning=planning,
                    predecessor=predecessor,
                    lowered_delta=lowered_delta,
                    emissions=emissions,
                    tools=tool_states,
                    deadline=deadline,
                )
                if trial.estimate.total_input_tokens >= before:
                    state.selected, state.omitted = original
                    state.exhausted = True
                    self._add_diagnostic(
                        diagnostics,
                        ContextPublicDiagnosticCode.SOURCE_VARIANT_NON_PROGRESS,
                    )
                    continue
                current_total = trial.estimate.total_input_tokens
                degraded_source_ids.add(state.candidate.source_instance_id)
                if source_can_advance(state):
                    offer("source", state)
            else:
                state = unit
                assert isinstance(state, _ToolState)
                before = current_total
                original = state.selected
                if not state.advance():
                    state.exhausted = True
                    continue
                trial = self._append_layout(
                    request,
                    planning=planning,
                    predecessor=predecessor,
                    lowered_delta=lowered_delta,
                    emissions=emissions,
                    tools=tool_states,
                    deadline=deadline,
                )
                if trial.estimate.total_input_tokens >= before:
                    state.selected = original
                    state.exhausted = True
                    self._add_diagnostic(
                        diagnostics,
                        ContextPublicDiagnosticCode.SOURCE_VARIANT_NON_PROGRESS,
                    )
                    continue
                current_total = trial.estimate.total_input_tokens
                degraded_tool_ids.add(state.item.source_entry_id or "")
                if self._tool_can_advance(state):
                    offer("tool", state)

        layout = self._append_layout(
            request,
            planning=planning,
            predecessor=predecessor,
            lowered_delta=lowered_delta,
            emissions=emissions,
            tools=tool_states,
            deadline=deadline,
        )
        if layout.estimate.total_input_tokens != current_total:
            raise StructuredModelInputCompileError(
                ModelInputCompileFailureKind.FINAL_ESTIMATE_MISMATCH
            )
        suffix_logical_bytes = sum(
            _message_logical_utf8_bytes(item)
            for item in layout.messages[len(previous_messages) :]
        )
        if predecessor.logical_utf8_bytes + suffix_logical_bytes > (
            self._limits.maximum_compile_working_set_bytes
        ):
            raise StructuredModelInputCompileError(
                ModelInputCompileFailureKind.COMPILE_WORKING_SET_EXCEEDED
            )

        source_decisions = tuple(
            CompiledSourceDecision(
                source_kind=item.source_kind,
                source_instance_fingerprint=(
                    item.semantic_fingerprint
                    if item.state is None
                    else item.state.candidate.source_semantic_fingerprint
                ),
                channel=ContextChannel.RUNTIME_OBSERVATION,
                selected_mode=(
                    ContextRenderMode.FULL
                    if item.state is None
                    else item.state.mode()
                ),
                included=(item.state is None or not item.state.omitted),
                estimated_tokens=(
                    request.compile_binding.estimator.estimate_message(
                        item.fixed_message
                    )
                    if item.state is None
                    else 0
                    if item.state.omitted
                    else request.compile_binding.estimator.estimate_message(
                        source_variant_message(
                            item.state.candidate, item.state.text() or ""
                        )
                    )
                ),
                reason_code=(
                    "OMITTED_FOR_BUDGET"
                    if item.state is not None and item.state.omitted
                    else "SELECTED_FULL"
                    if item.state is None or item.state.selected == 0
                    else "DEGRADED_FOR_BUDGET"
                ),
            )
            for item in sorted(
                emissions, key=lambda value: (value.placement_ordinal, value.source_kind.value)
            )
        )
        tool_decisions = tuple(
            CompiledToolResultDecision(
                source_entry_fingerprint=_tool_result_source_fingerprint(state.item),
                current_turn=state.current_turn,
                selected_mode=state.mode(),
                estimated_tokens=request.compile_binding.estimator.estimate_message(
                    state.message()
                ),
                reason_code=(
                    "SELECTED_FULL"
                    if state.selected == 0
                    else "DEGRADED_FOR_BUDGET"
                ),
            )
            for state in tool_states
        )
        omitted_sources = sum(
            item.state is not None and item.state.omitted for item in emissions
        )
        omitted_tools = sum(
            state.mode() is ToolResultProviderRenderMode.OMITTED_BODY
            for state in tool_states
        )
        if degraded_source_ids:
            self._add_diagnostic(
                diagnostics, ContextPublicDiagnosticCode.SOURCE_DEGRADED
            )
        if omitted_sources:
            self._add_diagnostic(
                diagnostics, ContextPublicDiagnosticCode.SOURCE_OMITTED
            )
        if degraded_tool_ids:
            self._add_diagnostic(
                diagnostics, ContextPublicDiagnosticCode.TOOL_RESULT_DEGRADED
            )
        if omitted_tools:
            self._add_diagnostic(
                diagnostics, ContextPublicDiagnosticCode.TOOL_RESULT_BODY_OMITTED
            )
        diagnostic_codes = tuple(dict.fromkeys(diagnostics))[
            : self._limits.maximum_diagnostics
        ]
        prefix_message_tokens = predecessor.final_estimate.message_tokens
        prefix_fingerprint = predecessor.semantic_prefix_fingerprint
        source_tokens = sum(decision.estimated_tokens for decision in source_decisions)
        decision_digest = context_fingerprint(
            "pulsara:model-input-append-decisions:v2",
            {
                "protected_prefix": prefix_fingerprint,
                "sources": tuple(
                    (
                        item.source_kind.value,
                        None if item.selected_mode is None else item.selected_mode.value,
                        item.included,
                        item.reason_code,
                    )
                    for item in source_decisions
                ),
                "tool_results": tuple(
                    (
                        item.current_turn,
                        item.selected_mode.value,
                        item.reason_code,
                    )
                    for item in tool_decisions
                ),
            },
        )
        report = ContextCompileBudgetReport(
            compiler_contract_version=COMPILER_CONTRACT_VERSION,
            estimator_fingerprint=request.compile_binding.estimator_fingerprint,
            target_fingerprint=request.compile_binding.target_fact.target_fingerprint,
            tool_surface_fingerprint=predecessor.compatibility.tool_surface_fingerprint,
            effective_input_budget_tokens=budget,
            system_tokens=layout.estimate.system_tokens,
            message_tokens=layout.estimate.message_tokens,
            tool_tokens=layout.estimate.tool_tokens,
            envelope_tokens=layout.estimate.envelope_tokens,
            total_input_tokens=layout.estimate.total_input_tokens,
            protected_transcript_tokens=prefix_message_tokens,
            protected_prefix_message_count=len(previous_messages),
            protected_prefix_logical_utf8_bytes=predecessor.logical_utf8_bytes,
            protected_prefix_fingerprint=prefix_fingerprint,
            context_source_tokens=source_tokens,
            degraded_source_count=len(degraded_source_ids),
            omitted_source_count=omitted_sources,
            degraded_tool_result_count=len(degraded_tool_ids),
            omitted_tool_result_body_count=omitted_tools,
            decision_digest=decision_digest,
        )
        compiled_fingerprint = frozen_compiled_model_input_fingerprint(
            context_id=request.context_id,
            canonical_input_identity=request.canonical_input.identity,
            system_prompt=layout.system_prompt,
            messages=layout.messages,
            tools=predecessor.tools,
            final_estimate=layout.estimate,
            source_decisions=source_decisions,
            tool_result_decisions=tool_decisions,
            budget_report=report,
            diagnostic_codes=diagnostic_codes,
            source_collection_fingerprint=request.sources.collection_fingerprint,
            compile_binding_fingerprint=request.compile_binding.binding_fingerprint,
        )
        deadline.check()
        compiled = FrozenCompiledModelInput(
            context_id=request.context_id,
            canonical_input_identity=request.canonical_input.identity,
            system_prompt=layout.system_prompt,
            messages=layout.messages,
            tools=predecessor.tools,
            final_estimate=layout.estimate,
            source_decisions=source_decisions,
            tool_result_decisions=tool_decisions,
            budget_report=report,
            diagnostic_codes=diagnostic_codes,
            source_collection_fingerprint=request.sources.collection_fingerprint,
            compiled_semantic_fingerprint=compiled_fingerprint,
            compile_binding_fingerprint=request.compile_binding.binding_fingerprint,
        )
        resulting_heads = dict(previous_heads)
        for item in emissions:
            if item.state is not None and item.state.omitted:
                continue
            message = (
                item.fixed_message
                if item.fixed_message is not None
                else source_variant_message(item.state.candidate, item.state.text() or "")
            )
            assert message is not None
            resulting_heads[item.source_kind] = ProcessLocalSourceHead(
                source_kind=item.source_kind,
                presence=item.presence,
                semantic_fingerprint=item.semantic_fingerprint,
                installed_observation_fingerprint=context_fingerprint(
                    "pulsara:installed-runtime-observation:v1",
                    {"message": _llm_message_value(message)},
                ),
                last_emitted_turn_id=request.canonical_input.identity.turn_id,
                last_emitted_model_call_index=request.model_call_index,
            )
        return FrozenProviderInputAppendCompileResult(
            compiled_input=compiled,
            canonical_frontier=frontier,
            source_heads=tuple(
                resulting_heads[kind]
                for kind in sorted(resulting_heads, key=lambda value: value.value)
            ),
            appended_message_count=len(layout.messages) - len(previous_messages),
            reset_reason=None,
        )

    def _append_layout(
        self,
        request: StructuredModelInputCompileRequest,
        *,
        planning: FrozenProviderInputAppendPlanningInput,
        predecessor: object,
        lowered_delta: tuple[LoweredCanonicalItem, ...],
        emissions: list[_AppendSourceEmission],
        tools: list[_ToolState],
        deadline: _CompileDeadline,
    ) -> _Layout:
        deadline.check()
        tool_by_identity = {id(state.lowered): state for state in tools}
        delta_messages = tuple(
            item.fixed_message
            if item.fixed_message is not None
            else tool_by_identity[id(item)].message()
            for item in lowered_delta
        )
        ordered_observations = tuple(
            message
            for _placement, _kind, message in sorted(
                (
                    (
                        item.placement_ordinal,
                        item.source_kind.value,
                        item.fixed_message
                        if item.fixed_message is not None
                        else None
                        if item.state is None or item.state.omitted
                        else source_variant_message(
                            item.state.candidate, item.state.text() or ""
                        ),
                    )
                    for item in emissions
                ),
                key=lambda value: value[:2],
            )
            if message is not None
        )
        if isinstance(planning.dispatch_anchor, NewTriggerAnchor):
            indexes = tuple(
                index
                for index, item in enumerate(lowered_delta)
                if item.source.source_entry_id
                == planning.dispatch_anchor.source_entry_id
                and provider_input_item_fingerprint(item.source)
                == planning.dispatch_anchor.provider_input_item_fingerprint
            )
            if len(indexes) != 1:
                raise StructuredModelInputCompileError(
                    ModelInputCompileFailureKind.SOURCE_CONTRACT_INVALID
                )
            trigger_index = indexes[0]
            suffix = (
                *delta_messages[:trigger_index],
                *ordered_observations,
                *delta_messages[trigger_index:],
            )
        else:
            suffix = (*delta_messages, *ordered_observations)
        suffix_token_values: list[int] = []
        for message in suffix:
            deadline.check()
            suffix_token_values.append(
                request.compile_binding.estimator.estimate_message(message)
            )
        previous_estimate = predecessor.final_estimate
        message_tokens_by_index = (
            *previous_estimate.message_tokens_by_index,
            *suffix_token_values,
        )
        message_tokens = sum(message_tokens_by_index)
        estimate = TokenEstimate(
            system_tokens=previous_estimate.system_tokens,
            message_tokens=message_tokens,
            message_tokens_by_index=message_tokens_by_index,
            tool_tokens=previous_estimate.tool_tokens,
            envelope_tokens=previous_estimate.envelope_tokens,
            total_input_tokens=(
                previous_estimate.system_tokens
                + message_tokens
                + previous_estimate.tool_tokens
                + previous_estimate.envelope_tokens
            ),
        )
        messages = (*predecessor.messages, *suffix)
        deadline.check()
        return _Layout(predecessor.system_prompt, messages, estimate)

    @staticmethod
    def _require_provider_safe_delta(
        delta: tuple[FrozenProviderInputItem, ...],
    ) -> None:
        for index, item in enumerate(delta):
            if item.item_kind is not FrozenProviderInputItemKind.ASSISTANT_TOOL_REQUEST:
                continue
            expected = tuple(call.tool_call_id for call in item.tool_calls)
            observed: list[str] = []
            cursor = index + 1
            while cursor < len(delta):
                successor = delta[cursor]
                if successor.item_kind not in {
                    FrozenProviderInputItemKind.TOOL_RESULT,
                    FrozenProviderInputItemKind.TOOL_RESULT_CLOSURE,
                }:
                    break
                if successor.tool_call_id is not None:
                    observed.append(successor.tool_call_id)
                cursor += 1
            if tuple(observed) != expected:
                raise StructuredModelInputCompileError(
                    ModelInputCompileFailureKind.CANONICAL_DELTA_NOT_PROVIDER_SAFE
                )

    def _validate_sources(self, request: StructuredModelInputCompileRequest) -> None:
        sources = request.sources
        if len(sources.candidates) > self._limits.maximum_source_candidates:
            raise StructuredModelInputCompileError(
                ModelInputCompileFailureKind.SOURCE_PHYSICAL_BOUND_EXCEEDED
            )
        if len(sources.diagnostics) > self._limits.maximum_diagnostics:
            raise StructuredModelInputCompileError(
                ModelInputCompileFailureKind.SOURCE_PHYSICAL_BOUND_EXCEEDED
            )
        present = {candidate.source_kind for candidate in sources.candidates}
        absent = {fact.source_kind for fact in sources.absent_facts}
        if present | absent != set(ContextSourceKind):
            raise StructuredModelInputCompileError(
                ModelInputCompileFailureKind.SOURCE_CONTRACT_INVALID
            )
        if not {
            ContextSourceKind.BASE_SYSTEM,
            ContextSourceKind.RUNTIME_ENVIRONMENT,
            ContextSourceKind.RUN_PERMISSION,
            ContextSourceKind.TOOL_OBSERVATION_FRESHNESS,
        }.issubset(present):
            raise StructuredModelInputCompileError(
                ModelInputCompileFailureKind.REQUIRED_SOURCE_UNAVAILABLE
            )
        expected_optional = {
            ContextSourceKind.PLAN_HANDOFF: (
                request.canonical_facts.plan_handoff_fact is not None
            ),
            ContextSourceKind.PLAN_WORKFLOW: (
                request.canonical_facts.plan_workflow_fact is not None
            ),
            ContextSourceKind.PREVIOUS_TURN_OUTCOME: (
                request.canonical_facts.previous_turn_outcome_fact is not None
            ),
        }
        if any(
            ((kind in present) != expected_presence)
            for kind, expected_presence in expected_optional.items()
        ):
            raise StructuredModelInputCompileError(
                ModelInputCompileFailureKind.SOURCE_CONTRACT_INVALID
            )
        for candidate in sources.candidates:
            expected = _SOURCE_POLICY.get(candidate.source_kind)
            if (
                expected is None
                or (
                    candidate.source_contract_version,
                    candidate.channel,
                    candidate.trust_class,
                    candidate.budget_class,
                    candidate.placement_ordinal,
                    candidate.degradation_priority,
                    tuple(variant.mode for variant in candidate.variants),
                    candidate.lifecycle,
                )
                != expected
            ):
                raise StructuredModelInputCompileError(
                    ModelInputCompileFailureKind.SOURCE_CONTRACT_INVALID
                )
            if len(candidate.variants) > self._limits.maximum_variants_per_source:
                raise StructuredModelInputCompileError(
                    ModelInputCompileFailureKind.SOURCE_PHYSICAL_BOUND_EXCEEDED
                )
            if any(
                variant.utf8_bytes > self._limits.maximum_single_source_variant_bytes
                for variant in candidate.variants
            ):
                raise StructuredModelInputCompileError(
                    ModelInputCompileFailureKind.SOURCE_PHYSICAL_BOUND_EXCEEDED
                )
            costs = tuple(
                self._source_variant_tokens(request, candidate, variant.text)
                for variant in candidate.variants
            )
            if any(after > before for before, after in zip(costs, costs[1:])):
                raise StructuredModelInputCompileError(
                    ModelInputCompileFailureKind.SOURCE_CONTRACT_INVALID
                )
        for fact in sources.absent_facts:
            expected = _SOURCE_POLICY.get(fact.source_kind)
            if expected is None:
                raise StructuredModelInputCompileError(
                    ModelInputCompileFailureKind.SOURCE_CONTRACT_INVALID
                )
            (
                version,
                channel,
                trust,
                budget,
                placement,
                degradation,
                modes,
                lifecycle,
            ) = expected
            expected_contract = context_fingerprint(
                "context-source-contract:v1",
                {
                    "kind": fact.source_kind.value,
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
            if (
                fact.source_contract_version != version
                or fact.source_contract_fingerprint != expected_contract
                or fact.trust_class is not trust
                or fact.budget_class is not budget
                or fact.placement_ordinal != placement
                or fact.degradation_priority != degradation
                or fact.lifecycle is not lifecycle
                or fact.absence_kind
                not in _SOURCE_ABSENCE_POLICY[fact.source_kind]
            ):
                raise StructuredModelInputCompileError(
                    ModelInputCompileFailureKind.SOURCE_CONTRACT_INVALID
                )

    def _validate_tool_surface(
        self, request: StructuredModelInputCompileRequest
    ) -> None:
        surface = request.compile_binding.tool_surface
        if len(surface.tool_specs) > self._limits.maximum_tool_specs:
            raise StructuredModelInputCompileError(
                ModelInputCompileFailureKind.TOOL_SURFACE_INVALID
            )
        if (
            sum(len(tool.canonical_bytes) for tool in surface.tool_specs)
            > self._limits.maximum_tool_spec_canonical_bytes
        ):
            raise StructuredModelInputCompileError(
                ModelInputCompileFailureKind.TOOL_SURFACE_INVALID
            )

    def _validate_canonical_input_bound(
        self, request: StructuredModelInputCompileRequest
    ) -> None:
        if (
            len(request.canonical_input.items)
            > self._limits.maximum_canonical_input_items
            or request.canonical_input.canonical_utf8_bytes
            > self._limits.maximum_canonical_input_bytes
        ):
            raise StructuredModelInputCompileError(
                ModelInputCompileFailureKind.COMPILE_WORKING_SET_EXCEEDED
            )

    def _validate_physical_bounds(
        self,
        request: StructuredModelInputCompileRequest,
        lowered: tuple[LoweredCanonicalItem, ...],
        *,
        materialized_plan_bytes: int,
    ) -> None:
        candidates = request.sources.candidates
        aggregate_full = sum(item.variants[0].utf8_bytes for item in candidates)
        aggregate_all = sum(
            variant.utf8_bytes for item in candidates for variant in item.variants
        )
        if aggregate_full > self._limits.maximum_aggregate_full_source_bytes:
            raise StructuredModelInputCompileError(
                ModelInputCompileFailureKind.SOURCE_PHYSICAL_BOUND_EXCEEDED
            )
        if aggregate_all > self._limits.maximum_aggregate_source_variant_bytes:
            raise StructuredModelInputCompileError(
                ModelInputCompileFailureKind.COMPILE_WORKING_SET_EXCEEDED
            )
        tool_variant_bytes = sum(
            variant.utf8_bytes
            for item in lowered
            for variant in item.tool_result_variants
        )
        schema_bytes = sum(
            len(tool.canonical_bytes)
            for tool in request.compile_binding.tool_surface.tool_specs
        )
        source_carrier_bytes = sum(
            max(
                0,
                _message_logical_utf8_bytes(
                    source_variant_message(candidate, variant.text)
                )
                - variant.utf8_bytes,
            )
            for candidate in candidates
            if candidate.channel is not ContextChannel.SYSTEM
            for variant in candidate.variants
        )
        system_source_count = sum(
            candidate.channel is ContextChannel.SYSTEM for candidate in candidates
        )
        system_join_bytes = max(0, system_source_count - 1) * len(
            "\n\n".encode("utf-8")
        )
        fixed_envelopes = sum(
            _fixed_message_envelope_utf8_bytes(item)
            for item in lowered
            if item.fixed_message is not None
        )
        working = (
            request.canonical_input.canonical_utf8_bytes
            + materialized_plan_bytes
            + aggregate_all
            + source_carrier_bytes
            + system_join_bytes
            + tool_variant_bytes
            + schema_bytes
            + fixed_envelopes
        )
        if working > self._limits.maximum_compile_working_set_bytes:
            raise StructuredModelInputCompileError(
                ModelInputCompileFailureKind.COMPILE_WORKING_SET_EXCEEDED
            )

    @staticmethod
    def _materialize_approved_plan(
        request: StructuredModelInputCompileRequest,
    ) -> tuple[tuple[FrozenProviderInputItem, ...], int]:
        approved = request.canonical_facts.approved_plan_materialization_fact
        if (
            approved is None
            or approved.disposition
            is PlanApprovedMaterializationDisposition.PIN_EXISTING_CANONICAL_BLOCK
        ):
            return request.canonical_input.items, 0
        handoff = request.canonical_facts.plan_handoff_fact
        if handoff is None:
            raise StructuredModelInputCompileError(
                ModelInputCompileFailureKind.SOURCE_CONTRACT_INVALID
            )
        try:
            exact_plan = approved.exact_plan_utf8.decode("utf-8")
        except UnicodeDecodeError as exc:  # central extraction normally proves this
            raise StructuredModelInputCompileError(
                ModelInputCompileFailureKind.SOURCE_CONTRACT_INVALID
            ) from exc
        indexes = tuple(
            index
            for index, item in enumerate(request.canonical_input.items)
            if item.item_kind is FrozenProviderInputItemKind.PLAN_CONTINUATION
            and item.source_entry_id == handoff.carrier_entry_id
            and item.source_turn_id == handoff.target_turn_id
        )
        if len(indexes) != 1:
            raise StructuredModelInputCompileError(
                ModelInputCompileFailureKind.SOURCE_CONTRACT_INVALID
            )
        index = indexes[0]
        original = request.canonical_input.items[index]
        try:
            storage_value = json.loads(original.text)
        except (TypeError, ValueError) as exc:
            raise StructuredModelInputCompileError(
                ModelInputCompileFailureKind.SOURCE_CONTRACT_INVALID
            ) from exc
        if not isinstance(storage_value, dict) or set(storage_value) != {
            "pulsara_plan_continuation"
        }:
            raise StructuredModelInputCompileError(
                ModelInputCompileFailureKind.SOURCE_CONTRACT_INVALID
            )
        projection = storage_value["pulsara_plan_continuation"]
        if not isinstance(projection, dict) or projection != {
            "status": "APPROVED",
            "transition": "APPROVED_PLAN",
        }:
            raise StructuredModelInputCompileError(
                ModelInputCompileFailureKind.SOURCE_CONTRACT_INVALID
            )
        # Add the exact centrally extracted Plan to the already-validated
        # provider DTO.  No storage identity descriptor or delimiter reaches
        # the prepared model call.
        storage_value = {
            "pulsara_plan_continuation": {
                **projection,
                "approved_plan": exact_plan,
            }
        }
        materialized_text = canonical_json_bytes(storage_value).decode("utf-8")
        replacement = FrozenProviderInputItem(
            item_kind=original.item_kind,
            source_entry_id=original.source_entry_id,
            source_entry_sequence=original.source_entry_sequence,
            source_turn_id=original.source_turn_id,
            text=materialized_text,
            input_origin=original.input_origin,
            tool_calls=original.tool_calls,
            tool_call_id=original.tool_call_id,
            tool_result_context=original.tool_result_context,
            tool_result_body_text=original.tool_result_body_text,
        )
        items = list(request.canonical_input.items)
        items[index] = replacement
        added = len(materialized_text.encode("utf-8")) - len(
            original.text.encode("utf-8")
        )
        return tuple(items), added

    def _advance_source_to_progress(
        self,
        request: StructuredModelInputCompileRequest,
        *,
        state: _SourceState,
        all_states: list[_SourceState],
        diagnostics: list[ContextPublicDiagnosticCode],
        deadline: _CompileDeadline,
    ) -> int:
        original = (state.selected, state.omitted)
        before = self._source_component_tokens(request, state, all_states)
        while state.advance():
            deadline.check()
            after = self._source_component_tokens(request, state, all_states)
            if after < before:
                return before - after
            self._add_diagnostic(
                diagnostics,
                ContextPublicDiagnosticCode.SOURCE_VARIANT_NON_PROGRESS,
            )
        state.selected, state.omitted = original
        state.exhausted = True
        return 0

    def _advance_tool_to_progress(
        self,
        request: StructuredModelInputCompileRequest,
        *,
        state: _ToolState,
        diagnostics: list[ContextPublicDiagnosticCode],
        deadline: _CompileDeadline,
    ) -> int:
        original = state.selected
        estimator = request.compile_binding.estimator
        before = estimator.estimate_message(state.message())
        while state.advance():
            deadline.check()
            after = estimator.estimate_message(state.message())
            if after < before:
                return before - after
            self._add_diagnostic(
                diagnostics,
                ContextPublicDiagnosticCode.SOURCE_VARIANT_NON_PROGRESS,
            )
        state.selected = original
        state.exhausted = True
        return 0

    def _source_component_tokens(
        self,
        request: StructuredModelInputCompileRequest,
        state: _SourceState,
        all_states: list[_SourceState],
    ) -> int:
        estimator = request.compile_binding.estimator
        if state.candidate.channel is not ContextChannel.SYSTEM:
            text = state.text()
            return (
                0
                if text is None
                else estimator.estimate_message(
                    source_variant_message(state.candidate, text)
                )
            )
        prompt = "\n\n".join(
            text
            for item in sorted(
                all_states, key=lambda item: self._placement_key(item.candidate)
            )
            if item.candidate.channel is ContextChannel.SYSTEM
            and (text := item.text()) is not None
        )
        return estimator.estimate_frozen_input(
            system_prompt=prompt,
            messages=(),
            tools=(),
        ).system_tokens

    @staticmethod
    def _source_variant_tokens(
        request: StructuredModelInputCompileRequest,
        candidate: ContextSourceCandidate,
        text: str,
    ) -> int:
        estimator = request.compile_binding.estimator
        if candidate.channel is ContextChannel.SYSTEM:
            return estimator.estimate_text(text)
        return estimator.estimate_message(source_variant_message(candidate, text))

    def _layout(
        self,
        request: StructuredModelInputCompileRequest,
        *,
        lowered: tuple[LoweredCanonicalItem, ...],
        sources: list[_SourceState],
        tools: list[_ToolState],
        deadline: _CompileDeadline | None = None,
    ) -> _Layout:
        active_deadline = deadline or _CompileDeadline(None)
        active_deadline.check()
        system_fragments = [
            text
            for state in sorted(
                sources, key=lambda item: self._placement_key(item.candidate)
            )
            if state.candidate.channel is ContextChannel.SYSTEM
            and (text := state.text()) is not None
        ]
        system_prompt = "\n\n".join(system_fragments)
        observations = tuple(
            source_variant_message(state.candidate, text)
            for state in sorted(
                sources, key=lambda item: self._placement_key(item.candidate)
            )
            if state.candidate.channel is ContextChannel.RUNTIME_OBSERVATION
            and (text := state.text()) is not None
        )
        tool_by_identity = {id(state.lowered): state for state in tools}
        transcript = tuple(
            (
                item.fixed_message
                if item.fixed_message is not None
                else tool_by_identity[id(item)].message()
            )
            for item in lowered
        )
        if request.dispatch_anchor_entry_id is None:
            messages = (*transcript, *observations)
        else:
            indexes = tuple(
                index
                for index, item in enumerate(lowered)
                if item.source.source_entry_id == request.dispatch_anchor_entry_id
            )
            if len(indexes) != 1:
                raise StructuredModelInputCompileError(
                    ModelInputCompileFailureKind.SOURCE_CONTRACT_INVALID
                )
            index = indexes[0]
            messages = (*transcript[:index], *observations, *transcript[index:])
        estimate = self._estimate_frozen_input(
            request,
            system_prompt=system_prompt,
            messages=messages,
            tools=request.compile_binding.tool_surface.tool_specs,
            deadline=active_deadline,
        )
        active_deadline.check()
        return _Layout(system_prompt, messages, estimate)

    @staticmethod
    def _estimate_frozen_input(
        request: StructuredModelInputCompileRequest,
        *,
        system_prompt: str,
        messages: tuple[LLMMessage, ...],
        tools: tuple[FrozenToolSpec, ...],
        deadline: _CompileDeadline,
    ) -> TokenEstimate:
        """Run one bounded estimator call with cooperative unit checkpoints.

        The estimator remains the sole token authority.  The per-unit calls do
        not supply a competing estimate; they only ensure a deadline can stop
        work between already-bounded messages and tool schemas.
        """

        estimator = request.compile_binding.estimator
        deadline.check()
        cooperative = getattr(estimator, "estimate_frozen_input_cooperative", None)
        if callable(cooperative):
            result = cooperative(
                system_prompt=system_prompt,
                messages=messages,
                tools=tools,
                checkpoint=deadline.check,
            )
        else:
            # Third-party/test estimators keep the frozen estimator protocol;
            # their whole call is bracketed by the same absolute deadline.
            result = estimator.estimate_frozen_input(
                system_prompt=system_prompt,
                messages=messages,
                tools=tools,
            )
        deadline.check()
        return result

    @staticmethod
    def _placement_key(candidate: ContextSourceCandidate) -> tuple[object, ...]:
        return (
            candidate.channel.value,
            candidate.placement_ordinal,
            candidate.source_kind.value,
            candidate.source_instance_id,
        )

    @staticmethod
    def _source_degradation_key(
        candidate: ContextSourceCandidate,
    ) -> tuple[object, ...]:
        rank = _SacrificeRank[candidate.budget_class.name]
        return (
            int(rank),
            -candidate.degradation_priority,
            1,
            "SOURCE",
            candidate.source_kind.value,
            candidate.source_instance_id,
        )

    @staticmethod
    def _source_can_advance(state: _SourceState) -> bool:
        return (
            not state.omitted
            and not state.exhausted
            and (
                state.selected + 1 < len(state.candidate.variants)
                or state.candidate.budget_class is not ContextBudgetClass.MUST_KEEP
            )
        )

    @staticmethod
    def _tool_can_advance(state: _ToolState) -> bool:
        return not state.exhausted and state.selected + 1 < len(
            state.lowered.tool_result_variants
        )

    def _tool_degradation_key(self, state: _ToolState) -> tuple[object, ...]:
        current = state.current_turn
        budget = (
            ContextBudgetClass.IMPORTANT if current else ContextBudgetClass.OPTIONAL
        )
        rank = _SacrificeRank[budget.name]
        sequence = state.item.source_entry_sequence or 0
        return (
            int(rank),
            -10 if current else -50,
            0 if not current else 1,
            "TOOL_RESULT",
            f"{sequence:020d}",
            state.item.source_entry_id or "",
        )

    def _minimum_budget_failure(
        self,
        request: StructuredModelInputCompileRequest,
        lowered: tuple[LoweredCanonicalItem, ...],
        sources: list[_SourceState],
        tools: list[_ToolState],
    ) -> ModelInputCompileFailureKind:
        estimator = request.compile_binding.estimator
        schema_only = estimator.estimate_frozen_input(
            system_prompt="",
            messages=(),
            tools=request.compile_binding.tool_surface.tool_specs,
        )
        if (
            schema_only.total_input_tokens
            > request.compile_binding.effective_input_budget_tokens
        ):
            return ModelInputCompileFailureKind.TOOL_SCHEMA_EXCEEDS_BUDGET
        tool_by_identity = {id(state.lowered): state for state in tools}
        protected = tuple(
            item.fixed_message
            if item.fixed_message is not None
            else tool_by_identity[id(item)].message()
            for item in lowered
        )
        protected_estimate = estimator.estimate_frozen_input(
            system_prompt="",
            messages=protected,
            tools=request.compile_binding.tool_surface.tool_specs,
        )
        if (
            protected_estimate.total_input_tokens
            > request.compile_binding.effective_input_budget_tokens
        ):
            return ModelInputCompileFailureKind.PROTECTED_TRANSCRIPT_EXCEEDS_BUDGET
        minimum = self._layout(
            request,
            lowered=lowered,
            sources=sources,
            tools=tools,
        )
        if (
            minimum.estimate.total_input_tokens
            > request.compile_binding.effective_input_budget_tokens
        ):
            return ModelInputCompileFailureKind.REQUIRED_CONTEXT_EXCEEDS_BUDGET
        return ModelInputCompileFailureKind.REQUIRED_CONTEXT_EXCEEDS_BUDGET

    def _selected_source_tokens(
        self,
        request: StructuredModelInputCompileRequest,
        state: _SourceState,
        all_states: list[_SourceState],
    ) -> int:
        text = state.text()
        if text is None:
            return 0
        estimator = request.compile_binding.estimator
        if state.candidate.channel is not ContextChannel.SYSTEM:
            return estimator.estimate_message(
                source_variant_message(state.candidate, text)
            )
        # System fragments are not individually additive because joining them
        # can change text token rounding.  Attribute the deterministic marginal
        # cost in provider placement order.
        ordered = [
            item
            for item in sorted(
                all_states, key=lambda item: self._placement_key(item.candidate)
            )
            if item.candidate.channel is ContextChannel.SYSTEM
            and item.text() is not None
        ]
        index = ordered.index(state)
        before = "\n\n".join(item.text() or "" for item in ordered[:index])
        through = "\n\n".join(item.text() or "" for item in ordered[: index + 1])
        return max(
            0, estimator.estimate_text(through) - estimator.estimate_text(before)
        )

    def _context_source_tokens(
        self,
        request: StructuredModelInputCompileRequest,
        layout: _Layout,
        states: list[_SourceState],
    ) -> int:
        estimator = request.compile_binding.estimator
        observation = sum(
            estimator.estimate_message(source_variant_message(state.candidate, text))
            for state in states
            if state.candidate.channel is not ContextChannel.SYSTEM
            and (text := state.text()) is not None
        )
        return layout.estimate.system_tokens + observation

    def _add_diagnostic(
        self,
        diagnostics: list[ContextPublicDiagnosticCode],
        code: ContextPublicDiagnosticCode,
    ) -> None:
        if code in diagnostics or len(diagnostics) >= self._limits.maximum_diagnostics:
            return
        diagnostics.append(code)


def _message_logical_utf8_bytes(message: LLMMessage) -> int:
    values = [*message.content, *message.thinking]
    for call in message.tool_calls:
        values.extend((call.id, call.name, call.arguments))
    values.extend(
        value
        for value in (message.tool_call_id, message.name, message.arguments)
        if value is not None
    )
    return sum(len(value.encode("utf-8")) for value in values)


def _fixed_message_envelope_utf8_bytes(item: LoweredCanonicalItem) -> int:
    message = item.fixed_message
    assert message is not None
    source = item.source
    source_bytes = len(source.text.encode("utf-8"))
    # The reader's canonical byte charge is content-oriented and does not
    # promise to include JSONB-backed tool arguments.  Count every non-body
    # provider carrier byte here, even when a parent manifest conservatively
    # causes some identity bytes to be charged twice.
    return max(0, _message_logical_utf8_bytes(message) - source_bytes)


def _llm_message_value(message: LLMMessage) -> object:
    return {
        "role": message.role.value,
        "content": message.content,
        "thinking": message.thinking,
        "tool_calls": tuple(
            (item.id, item.name, item.arguments) for item in message.tool_calls
        ),
        "tool_call_id": message.tool_call_id,
        "name": message.name,
        "arguments": message.arguments,
    }


def _tool_result_source_fingerprint(item: FrozenProviderInputItem) -> str:
    return context_fingerprint(
        "compiled-tool-result-source:v1",
        {
            "entry_id": item.source_entry_id,
            "sequence": item.source_entry_sequence,
            "tool_call_id": item.tool_call_id,
        },
    )


def _selected_lowered_message(
    item: LoweredCanonicalItem,
    *,
    tool_modes: dict[str, ToolResultProviderRenderMode],
) -> LLMMessage:
    if item.fixed_message is not None:
        return item.fixed_message
    mode = tool_modes.get(_tool_result_source_fingerprint(item.source))
    if mode is None:
        raise StructuredModelInputCompileError(
            ModelInputCompileFailureKind.SOURCE_CONTRACT_INVALID
        )
    matches = tuple(
        variant.message for variant in item.tool_result_variants if variant.mode is mode
    )
    if len(matches) != 1:
        raise StructuredModelInputCompileError(
            ModelInputCompileFailureKind.SOURCE_CONTRACT_INVALID
        )
    return matches[0]


def _observation_lifecycle(
    lifecycle: ContextSourceLifecycle,
) -> SourceObservationLifecycle:
    mapping = {
        ContextSourceLifecycle.SNAPSHOT_ON_CHANGE: SourceObservationLifecycle.SNAPSHOT,
        ContextSourceLifecycle.CALL_APPEND: SourceObservationLifecycle.CALL,
        ContextSourceLifecycle.TURN_APPEND: SourceObservationLifecycle.TURN,
        ContextSourceLifecycle.TURN_SNAPSHOT: SourceObservationLifecycle.TURN,
        ContextSourceLifecycle.ACTIVATION_SNAPSHOT: (
            SourceObservationLifecycle.ACTIVATION
        ),
        ContextSourceLifecycle.ONE_SHOT: SourceObservationLifecycle.ONE_SHOT,
    }
    try:
        return mapping[lifecycle]
    except KeyError as exc:
        raise StructuredModelInputCompileError(
            ModelInputCompileFailureKind.SOURCE_CONTRACT_INVALID
        ) from exc


def _source_occurrence_fingerprint(
    domain_semantic_fingerprint: str,
    *,
    lifecycle: ContextSourceLifecycle,
    turn_id: str,
    model_call_index: int,
    dispatch_anchor: object,
) -> str:
    occurrence: object = None
    if lifecycle in {
        ContextSourceLifecycle.TURN_APPEND,
        ContextSourceLifecycle.TURN_SNAPSHOT,
    }:
        occurrence = ("turn", turn_id)
    elif lifecycle is ContextSourceLifecycle.ACTIVATION_SNAPSHOT:
        occurrence = (
            "activation",
            provider_input_dispatch_anchor_value(dispatch_anchor),
        )
    elif lifecycle is ContextSourceLifecycle.CALL_APPEND:
        occurrence = ("call", turn_id, model_call_index)
    elif lifecycle is ContextSourceLifecycle.ONE_SHOT:
        occurrence = ("one-shot", domain_semantic_fingerprint)
    return context_fingerprint(
        "pulsara:context-source-occurrence:v1",
        {
            "domain": domain_semantic_fingerprint,
            "lifecycle": lifecycle.value,
            "occurrence": occurrence,
        },
    )


def _compatibility_reset_reason(
    predecessor: object,
    compatibility: ProviderInputEpochCompatibility,
) -> ProviderInputEpochResetReason | None:
    if predecessor is None:
        return ProviderInputEpochResetReason.COLD_HOST_BOOTSTRAP
    previous = predecessor.compatibility
    if (
        previous.base_system_semantic_fingerprint
        != compatibility.base_system_semantic_fingerprint
    ):
        return ProviderInputEpochResetReason.BASE_SYSTEM_CHANGED
    if previous.tool_surface_fingerprint != compatibility.tool_surface_fingerprint:
        return ProviderInputEpochResetReason.TOOL_SURFACE_CHANGED
    if (
        previous.model_target_fingerprint != compatibility.model_target_fingerprint
        or previous.estimator_fingerprint != compatibility.estimator_fingerprint
    ):
        return ProviderInputEpochResetReason.MODEL_TARGET_CHANGED
    if (
        previous.compiler_contract_version != compatibility.compiler_contract_version
        or previous.provider_message_lowering_contract
        != compatibility.provider_message_lowering_contract
    ):
        return ProviderInputEpochResetReason.PROVIDER_LOWERING_CHANGED
    if (
        previous.context_base_semantic_identity
        != compatibility.context_base_semantic_identity
    ):
        return ProviderInputEpochResetReason.CONTEXT_BINDING_REWRITE
    return None


__all__ = ["COMPILER_CONTRACT_VERSION", "StructuredModelInputCompiler"]
