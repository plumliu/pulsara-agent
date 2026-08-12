"""Deterministic structured model-input allocation.

The compiler accepts only immutable provider-neutral facts.  It performs no
I/O and has no authority outside the duration of one function call.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import IntEnum
import heapq

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
    ContextSourceKind,
    ContextTrustClass,
    FrozenCompiledModelInput,
    FrozenProviderInputItem,
    FrozenProviderInputItemKind,
    ModelInputCompileFailureKind,
    StructuredModelInputCompileError,
    StructuredModelInputCompileRequest,
    StructuredModelInputLimits,
    STRUCTURED_MODEL_INPUT_LIMITS,
    ToolResultProviderRenderMode,
    frozen_compiled_model_input_fingerprint,
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


COMPILER_CONTRACT_VERSION = "pulsara.structured-model-input-compiler.v1"


class _SacrificeRank(IntEnum):
    DEBUG = 0
    OPTIONAL = 1
    IMPORTANT = 2
    MUST_KEEP = 3


_SOURCE_POLICY = {
    ContextSourceKind.BASE_SYSTEM: (
        "pulsara.base-system.v1",
        ContextChannel.SYSTEM,
        ContextTrustClass.ROOT_INSTRUCTION,
        ContextBudgetClass.MUST_KEEP,
        0,
        0,
        (ContextRenderMode.FULL,),
    ),
    ContextSourceKind.RUNTIME_ENVIRONMENT: (
        "pulsara.runtime-environment.v1",
        ContextChannel.SYSTEM,
        ContextTrustClass.TRUSTED_RUNTIME_FACT,
        ContextBudgetClass.MUST_KEEP,
        10,
        10,
        (ContextRenderMode.FULL, ContextRenderMode.COMPACT),
    ),
    ContextSourceKind.RUN_PERMISSION: (
        "pulsara.run-permission.v1",
        ContextChannel.SYSTEM,
        ContextTrustClass.AUTHORIZED_CAPABILITY_CONTEXT,
        ContextBudgetClass.MUST_KEEP,
        14,
        12,
        (ContextRenderMode.FULL, ContextRenderMode.COMPACT),
    ),
    ContextSourceKind.PLAN_HANDOFF: (
        "pulsara.plan-handoff.v1",
        ContextChannel.SYSTEM,
        ContextTrustClass.TRUSTED_RUNTIME_FACT,
        ContextBudgetClass.MUST_KEEP,
        15,
        11,
        (ContextRenderMode.FULL, ContextRenderMode.COMPACT),
    ),
    ContextSourceKind.PLAN_WORKFLOW: (
        "pulsara.plan-workflow.v1",
        ContextChannel.SYSTEM,
        ContextTrustClass.ROOT_INSTRUCTION,
        ContextBudgetClass.MUST_KEEP,
        16,
        10,
        (ContextRenderMode.FULL, ContextRenderMode.COMPACT),
    ),
    ContextSourceKind.CAPABILITY_CATALOG: (
        "pulsara.capability-catalog.v1",
        ContextChannel.SYSTEM,
        ContextTrustClass.AUTHORIZED_CAPABILITY_CONTEXT,
        ContextBudgetClass.IMPORTANT,
        20,
        30,
        (
            ContextRenderMode.FULL,
            ContextRenderMode.COMPACT,
            ContextRenderMode.REF_ONLY,
        ),
    ),
    ContextSourceKind.ACTIVE_SKILL: (
        "pulsara.active-skill.v1",
        ContextChannel.SYSTEM,
        ContextTrustClass.AUTHORIZED_CAPABILITY_CONTEXT,
        ContextBudgetClass.MUST_KEEP,
        30,
        20,
        (ContextRenderMode.FULL,),
    ),
    ContextSourceKind.RUNTIME_CLOCK: (
        "pulsara.runtime-clock.v1",
        ContextChannel.LEADING_OBSERVATION,
        ContextTrustClass.TRUSTED_RUNTIME_FACT,
        ContextBudgetClass.OPTIONAL,
        0,
        80,
        (ContextRenderMode.FULL, ContextRenderMode.COMPACT),
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
        # KernelSessionIO owns the deadline.  Accepting the injected value keeps
        # the pure function compatible with that one execution seam.
        del deadline_monotonic
        self._validate_sources(request)
        surface = request.compile_binding.tool_surface
        self._validate_tool_surface(request)
        artifact_read_available = any(
            tool.name == "artifact_read" for tool in surface.tool_specs
        )
        canonical_items, materialized_plan_bytes = self._materialize_approved_plan(
            request
        )
        lowered = tuple(
            lower_canonical_item(
                item,
                artifact_read_available=artifact_read_available,
                limits=self._limits,
            )
            for item in canonical_items
        )
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
        )
        if layout.estimate.total_input_tokens != current_total:
            raise StructuredModelInputCompileError(
                ModelInputCompileFailureKind.FINAL_ESTIMATE_MISMATCH
            )

        full = estimator.estimate_frozen_input(
            system_prompt=layout.system_prompt,
            messages=layout.messages,
            tools=surface.tool_specs,
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
        protected_tokens = sum(
            estimator.estimate_message(item.fixed_message)
            for item in lowered
            if item.fixed_message is not None
        )
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
            protected_transcript_tokens=protected_tokens,
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
        if not {
            ContextSourceKind.BASE_SYSTEM,
            ContextSourceKind.RUNTIME_ENVIRONMENT,
            ContextSourceKind.RUN_PERMISSION,
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
        }
        if any(
            (kind in present) != expected_presence
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
        materialized_text = (
            original.text
            + "\n[UNTRUSTED_APPROVED_PLAN exact=true digest="
            + approved.content_identity.plan_utf8_digest
            + "]\n"
            + exact_plan
            + "\n[/UNTRUSTED_APPROVED_PLAN]"
        )
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
    ) -> int:
        original = (state.selected, state.omitted)
        before = self._source_component_tokens(request, state, all_states)
        while state.advance():
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
    ) -> int:
        original = state.selected
        estimator = request.compile_binding.estimator
        before = estimator.estimate_message(state.message())
        while state.advance():
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
    ) -> _Layout:
        system_fragments = [
            text
            for state in sorted(
                sources, key=lambda item: self._placement_key(item.candidate)
            )
            if state.candidate.channel is ContextChannel.SYSTEM
            and (text := state.text()) is not None
        ]
        system_prompt = "\n\n".join(system_fragments)
        leading = tuple(
            source_variant_message(state.candidate, text)
            for state in sorted(
                sources, key=lambda item: self._placement_key(item.candidate)
            )
            if state.candidate.channel is ContextChannel.LEADING_OBSERVATION
            and (text := state.text()) is not None
        )
        trailing = tuple(
            source_variant_message(state.candidate, text)
            for state in sorted(
                sources, key=lambda item: self._placement_key(item.candidate)
            )
            if state.candidate.channel is ContextChannel.TRAILING_OBSERVATION
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
        messages = (*leading, *transcript, *trailing)
        estimate = request.compile_binding.estimator.estimate_frozen_input(
            system_prompt=system_prompt,
            messages=messages,
            tools=request.compile_binding.tool_surface.tool_specs,
        )
        return _Layout(system_prompt, messages, estimate)

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


__all__ = ["COMPILER_CONTRACT_VERSION", "StructuredModelInputCompiler"]
