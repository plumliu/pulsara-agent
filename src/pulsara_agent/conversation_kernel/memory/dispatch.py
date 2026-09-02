"""Advisory-memory planning support for provider dispatch."""

from __future__ import annotations

from dataclasses import replace
from typing import Protocol


from pulsara_agent.conversation_kernel.context_sources import (
    build_memory_context_source,
    replace_memory_context_sources,
)
from pulsara_agent.conversation_kernel.direct_model import (
    PreparedKernelModelCall,
)
from pulsara_agent.conversation_kernel.io import KernelSessionIO
from pulsara_agent.conversation_kernel.execution_watchdogs import (
    KernelExecutionDeadlineFactory,
    KernelWatchdogOwner,
)
from pulsara_agent.conversation_kernel.memory.contracts import (
    AutomaticMemoryTriggerDisposition,
    FrozenMemoryTriggerPolicy,
    FrozenModelCallMemoryContext,
    MemoryUsePolicy,
)
from pulsara_agent.conversation_kernel.memory.hints import MEMORY_WRITE_HINT_BODY
from pulsara_agent.conversation_kernel.memory.citations import (
    ProcessLocalMemoryCallContextOwner,
)
from pulsara_agent.conversation_kernel.reader import (
    CanonicalProviderInputReader,
)
from pulsara_agent.conversation_kernel.steer import (
    MemorySourceInvalidationReservation,
    build_memory_source_invalidation_reservation,
)
from pulsara_agent.model_input.compiler import (
    StructuredModelInputCompiler,
)
from pulsara_agent.model_input.contracts import (
    CapabilityActivationSubjectKind,
    CollectedContextSources,
    ContextSourceAbsentFact,
    ContextSourceAbsenceKind,
    ContextSourceCandidate,
    ContextSourceKind,
    ContextSourceLifecycle,
    FrozenCanonicalCompileSnapshot,
    PreparedProviderInputCut,
    FrozenCompiledModelInput,
    ModelInputCompileFailureKind,
    StructuredModelInputCompileError,
    StructuredModelInputCompileRequest,
)
from pulsara_agent.model_input.continuity import (
    FrozenProviderInputAppendCompileResult,
    FrozenProviderInputAppendPlanningInput,
    ProcessLocalSourceHead,
    ProviderInputContinuityScope,
    ProviderInputEpochCompatibility,
    provider_input_logical_utf8_bytes,
    encode_runtime_observation,
    SourceObservationLifecycle,
    SourceObservationPresence,
)

from pulsara_agent.primitives.context import (
    context_fingerprint,
)


class MemoryContextProjectionPort(Protocol):
    async def freeze_response_preference_source(
        self,
    ) -> ContextSourceCandidate | ContextSourceAbsentFact: ...

    async def freeze_automatic_recall_source(
        self, query: str
    ) -> ContextSourceCandidate | ContextSourceAbsentFact: ...

    def classify_automatic_trigger(
        self, text: str
    ) -> AutomaticMemoryTriggerDisposition: ...

    def classify_memory_trigger(self, text: str) -> FrozenMemoryTriggerPolicy: ...

    def offer_governance_wake(self) -> None: ...


class MemoryDispatchSupport:
    """Optional advisory-memory projection for one provider dispatch."""

    def __init__(
        self,
        *,
        compiler: StructuredModelInputCompiler,
        io_owner: KernelSessionIO,
        memory_context_owner: ProcessLocalMemoryCallContextOwner,
        memory_projection: MemoryContextProjectionPort | None,
        input_reader: CanonicalProviderInputReader,
        deadline_factory: KernelExecutionDeadlineFactory,
    ) -> None:
        self._compiler = compiler
        self._io = io_owner
        self._memory_contexts = memory_context_owner
        self._memory_projection = memory_projection
        self._input_reader = input_reader
        self._deadlines = deadline_factory

    def offer_governance_wake(self) -> None:
        projection = self._memory_projection
        if projection is not None:
            projection.offer_governance_wake()

    def _canonical_deadline(self) -> float:
        return self._deadlines.deadline(KernelWatchdogOwner.FOREGROUND_CANONICAL)

    @property
    def available(self) -> bool:
        return self._memory_projection is not None

    def classify_trigger(self, text: str) -> FrozenMemoryTriggerPolicy:
        if self._memory_projection is None:
            raise RuntimeError("memory projection is not installed")
        return self._memory_projection.classify_memory_trigger(text)

    async def freeze_response_preference_source(
        self,
    ) -> ContextSourceCandidate | ContextSourceAbsentFact:
        if self._memory_projection is None:
            raise RuntimeError("memory projection is not installed")
        return await self._memory_projection.freeze_response_preference_source()

    async def _read_compile_snapshot(
        self, cut: PreparedProviderInputCut, *, deadline: float
    ) -> FrozenCanonicalCompileSnapshot:
        return await self._io.run(
            self._input_reader.read_frozen_compile_snapshot,
            cut,
            deadline_monotonic=deadline,
        )

    def freeze_call_context(
        self,
        *,
        scope: ProviderInputContinuityScope,
        planning: FrozenProviderInputAppendPlanningInput,
        canonical_facts: FrozenCanonicalCompileSnapshot,
        sources: CollectedContextSources,
        memory_use_policy: MemoryUsePolicy,
    ) -> tuple[FrozenModelCallMemoryContext, tuple[tuple[str, str], ...]]:
        epoch_nonce = (
            planning.predecessor_view.epoch_nonce
            if planning.predecessor_view is not None
            else f"cold:{planning.planning_nonce}"
        )
        return self._memory_contexts.freeze_call(
            scope=scope,
            epoch_nonce=epoch_nonce,
            canonical_facts=canonical_facts,
            sources=sources.candidates,
            memory_use_policy=memory_use_policy,
        )

    @staticmethod
    def _memory_source_head(
        planning: FrozenProviderInputAppendPlanningInput,
        kind: ContextSourceKind,
    ) -> ProcessLocalSourceHead | None:
        predecessor = planning.predecessor_view
        if predecessor is None:
            return None
        return next(
            (item for item in predecessor.source_heads if item.source_kind is kind),
            None,
        )

    @staticmethod
    def _memory_source_presence(
        source: ContextSourceCandidate | ContextSourceAbsentFact,
    ) -> SourceObservationPresence:
        if isinstance(source, ContextSourceCandidate):
            return SourceObservationPresence.VALUE
        if source.absence_kind is ContextSourceAbsenceKind.UNAVAILABLE:
            return SourceObservationPresence.UNAVAILABLE
        return SourceObservationPresence.CLEARED

    @staticmethod
    def _memory_source_occurrence_fingerprint(
        source: ContextSourceCandidate | ContextSourceAbsentFact,
    ) -> str:
        # Both Round 8 sources are SNAPSHOT_ON_CHANGE.  Keep the occurrence
        # derivation identical to the pure compiler without importing its
        # private implementation.
        if source.lifecycle is not ContextSourceLifecycle.SNAPSHOT_ON_CHANGE:
            raise StructuredModelInputCompileError(
                ModelInputCompileFailureKind.SOURCE_CONTRACT_INVALID
            )
        return context_fingerprint(
            "pulsara:context-source-occurrence:v1",
            {
                "domain": source.domain_semantic_fingerprint,
                "lifecycle": source.lifecycle.value,
                "occurrence": None,
            },
        )

    def _memory_invalidation_reservation(
        self,
        *,
        source_kind: ContextSourceKind,
        prior: ProcessLocalSourceHead,
        desired: ContextSourceCandidate | ContextSourceAbsentFact,
        compiled: FrozenCompiledModelInput,
        prepared_call: PreparedKernelModelCall,
    ) -> MemorySourceInvalidationReservation:
        cleared = build_memory_context_source(
            kind=source_kind,
            texts=None,
            absence_kind=ContextSourceAbsenceKind.EXPLICIT_EMPTY,
        )
        unavailable = build_memory_context_source(
            kind=source_kind,
            texts=None,
            absence_kind=ContextSourceAbsenceKind.UNAVAILABLE,
        )
        assert isinstance(cleared, ContextSourceAbsentFact)
        assert isinstance(unavailable, ContextSourceAbsentFact)
        invalidation_messages = tuple(
            encode_runtime_observation(
                source_kind=source_kind,
                trust_class=item.trust_class,
                lifecycle=(
                    SourceObservationLifecycle.CLEARED
                    if item.absence_kind is ContextSourceAbsenceKind.EXPLICIT_EMPTY
                    else SourceObservationLifecycle.UNAVAILABLE
                ),
                presence=(
                    SourceObservationPresence.CLEARED
                    if item.absence_kind is ContextSourceAbsenceKind.EXPLICIT_EMPTY
                    else SourceObservationPresence.UNAVAILABLE
                ),
                contract_version=item.source_contract_version,
                body="",
            )
            for item in (cleared, unavailable)
        )
        estimator = prepared_call.compile_binding.estimator
        base_estimate = compiled.final_estimate
        token_deltas: list[int] = []
        byte_deltas: list[int] = []
        for message in invalidation_messages:
            estimate = estimator.estimate_frozen_input(
                system_prompt=compiled.system_prompt,
                messages=(*compiled.messages, message),
                tools=compiled.tools,
            )
            token_deltas.append(
                max(0, estimate.total_input_tokens - base_estimate.total_input_tokens)
            )
            byte_deltas.append(
                provider_input_logical_utf8_bytes(
                    system_prompt="",
                    tools=(),
                    messages=(message,),
                )
            )
        full_bytes = 0
        full_tokens = 0
        if isinstance(desired, ContextSourceCandidate):
            full_message = encode_runtime_observation(
                source_kind=source_kind,
                trust_class=desired.trust_class,
                lifecycle=SourceObservationLifecycle.SNAPSHOT,
                presence=SourceObservationPresence.VALUE,
                contract_version=desired.source_contract_version,
                body=desired.variants[0].text,
            )
            full_bytes = provider_input_logical_utf8_bytes(
                system_prompt="", tools=(), messages=(full_message,)
            )
            full_estimate = estimator.estimate_frozen_input(
                system_prompt=compiled.system_prompt,
                messages=(*compiled.messages, full_message),
                tools=compiled.tools,
            )
            full_tokens = max(
                0,
                full_estimate.total_input_tokens - base_estimate.total_input_tokens,
            )
        return build_memory_source_invalidation_reservation(
            source_kind=source_kind,
            prior_presence=prior.presence,
            prior_semantic_fingerprint=prior.semantic_fingerprint,
            desired_presence=self._memory_source_presence(desired),
            desired_semantic_fingerprint=(
                self._memory_source_occurrence_fingerprint(desired)
            ),
            source_contract_fingerprint=desired.source_contract_fingerprint,
            invalidation_encoded_utf8_bytes_ceiling=max(byte_deltas),
            invalidation_input_token_ceiling=max(token_deltas),
            invalidation_epoch_bytes_ceiling=max(byte_deltas),
            full_encoded_utf8_bytes=full_bytes,
            full_input_token_cost=full_tokens,
            estimator_fingerprint=(
                prepared_call.compile_binding.estimator.fact.estimator_fingerprint
            ),
        )

    def planning_reservations(
        self,
        *,
        planning: FrozenProviderInputAppendPlanningInput,
        prepared_preference: ContextSourceCandidate | ContextSourceAbsentFact,
        recall_desired: ContextSourceCandidate | ContextSourceAbsentFact,
        compiled: FrozenCompiledModelInput,
        prepared_call: PreparedKernelModelCall,
    ) -> tuple[
        MemorySourceInvalidationReservation | None,
        MemorySourceInvalidationReservation | None,
    ]:
        recall_prior = self._memory_source_head(
            planning, ContextSourceKind.MEMORY_RECALL
        )
        preference_prior = self._memory_source_head(
            planning, ContextSourceKind.MEMORY_RESPONSE_PREFERENCE_HEAD
        )
        recall = None
        if recall_prior is not None and recall_prior.presence in {
            SourceObservationPresence.VALUE,
            SourceObservationPresence.UNAVAILABLE,
        }:
            recall = self._memory_invalidation_reservation(
                source_kind=ContextSourceKind.MEMORY_RECALL,
                prior=recall_prior,
                desired=recall_desired,
                compiled=compiled,
                prepared_call=prepared_call,
            )
        preference = None
        desired_presence = self._memory_source_presence(prepared_preference)
        desired_semantic = self._memory_source_occurrence_fingerprint(
            prepared_preference
        )
        if (
            preference_prior is not None
            and preference_prior.presence is SourceObservationPresence.VALUE
            and (
                desired_presence is not SourceObservationPresence.VALUE
                or preference_prior.semantic_fingerprint != desired_semantic
            )
        ):
            preference = self._memory_invalidation_reservation(
                source_kind=ContextSourceKind.MEMORY_RESPONSE_PREFERENCE_HEAD,
                prior=preference_prior,
                desired=prepared_preference,
                compiled=compiled,
                prepared_call=prepared_call,
            )
        return recall, preference

    async def apply_sources(
        self,
        sources: CollectedContextSources,
        *,
        activation_subject: CapabilityActivationSubjectKind | None,
        activation_text: str,
        include_recall: bool,
        frozen_preference: ContextSourceCandidate
        | ContextSourceAbsentFact
        | None = None,
        trigger_disposition: str | None = None,
        write_hint: bool = False,
    ) -> CollectedContextSources:
        if (
            self._memory_projection is None
            or activation_subject
            is not CapabilityActivationSubjectKind.ROOT_HUMAN_PROMPT
        ):
            return sources
        disposition = trigger_disposition or str(
            self._memory_projection.classify_automatic_trigger(activation_text)
        )
        preference = frozen_preference or (
            await self._memory_projection.freeze_response_preference_source()
        )
        if disposition == "DISABLED_BY_EXPLICIT_USER_DIRECTIVE":
            preference = build_memory_context_source(
                kind=ContextSourceKind.MEMORY_RESPONSE_PREFERENCE_HEAD,
                texts=None,
                absence_kind=ContextSourceAbsenceKind.EXPLICIT_EMPTY,
            )
        replacements: list[ContextSourceCandidate | ContextSourceAbsentFact] = [
            preference,
            build_memory_context_source(
                kind=ContextSourceKind.MEMORY_WRITE_HINT,
                texts=(MEMORY_WRITE_HINT_BODY,) if write_hint else None,
                absence_kind=ContextSourceAbsenceKind.NOT_APPLICABLE,
            ),
        ]
        if include_recall:
            if disposition in {
                "DISABLED_BY_EXPLICIT_USER_DIRECTIVE",
                "SKIPPED_LOW_INFORMATION",
            }:
                replacements.append(
                    build_memory_context_source(
                        kind=ContextSourceKind.MEMORY_RECALL,
                        texts=None,
                        absence_kind=ContextSourceAbsenceKind.EXPLICIT_EMPTY,
                    )
                )
            else:
                replacements.append(
                    await self._memory_projection.freeze_automatic_recall_source(
                        activation_text
                    )
                )
        return replace_memory_context_sources(sources, tuple(replacements))

    async def compile_with_fallback(
        self,
        *,
        request: StructuredModelInputCompileRequest,
        planning: FrozenProviderInputAppendPlanningInput,
        compatibility: ProviderInputEpochCompatibility,
        canonical_facts: FrozenCanonicalCompileSnapshot,
        sources: CollectedContextSources,
        preference_source: ContextSourceCandidate | ContextSourceAbsentFact | None,
        recall_reservation: MemorySourceInvalidationReservation | None,
        preference_reservation: MemorySourceInvalidationReservation | None,
        scope: ProviderInputContinuityScope,
        memory_use_policy: MemoryUsePolicy,
        deadline: float,
    ) -> tuple[FrozenProviderInputAppendCompileResult, CollectedContextSources]:
        """Materialize optional memory without invalidating an accepted steer.

        Preference FULL gets the first optional allocation.  Recall then uses
        the ordinary FULL/COMPACT/REF_ONLY compiler degradation.  If either
        optional VALUE cannot fit, only the already-quoted stale-state carrier
        may remain.  No queue row is reconsidered and no remote operation is
        retried here.
        """

        budget_failures = {
            ModelInputCompileFailureKind.COMPILE_WORKING_SET_EXCEEDED,
            ModelInputCompileFailureKind.PROTECTED_TRANSCRIPT_EXCEEDS_BUDGET,
            ModelInputCompileFailureKind.REQUIRED_CONTEXT_EXCEEDS_BUDGET,
            ModelInputCompileFailureKind.PREFIX_EPOCH_BUDGET_EXHAUSTED,
            ModelInputCompileFailureKind.STATEFUL_SOURCE_REPLACEMENT_OVER_BUDGET,
            ModelInputCompileFailureKind.FULL_REQUIRED_TOOL_RESULT_EXCEEDS_INPUT_BUDGET,
        }

        def request_for(
            selected_sources: CollectedContextSources,
        ) -> StructuredModelInputCompileRequest:
            memory = self.freeze_call_context(
                scope=scope,
                planning=planning,
                canonical_facts=canonical_facts,
                sources=selected_sources,
                memory_use_policy=memory_use_policy,
            )
            return replace(
                request,
                sources=selected_sources,
                memory_citation_handles=memory[1],
            )

        async def compile_one(selected_sources: CollectedContextSources):
            selected_request = request_for(selected_sources)
            return await self._io.run(
                self._compiler.compile_append,
                selected_request,
                planning=planning,
                compatibility=compatibility,
                deadline_monotonic=deadline,
            )

        try:
            return await compile_one(sources), sources
        except StructuredModelInputCompileError as exc:
            if exc.kind not in budget_failures:
                raise

        def fallback_source(
            kind: ContextSourceKind,
            reservation: MemorySourceInvalidationReservation | None,
            desired: ContextSourceCandidate | ContextSourceAbsentFact | None,
        ) -> ContextSourceAbsentFact:
            absence = ContextSourceAbsenceKind.NOT_APPLICABLE
            if reservation is not None:
                if (
                    isinstance(desired, ContextSourceAbsentFact)
                    and desired.absence_kind is ContextSourceAbsenceKind.EXPLICIT_EMPTY
                ):
                    absence = ContextSourceAbsenceKind.EXPLICIT_EMPTY
                else:
                    absence = ContextSourceAbsenceKind.UNAVAILABLE
            value = build_memory_context_source(
                kind=kind,
                texts=None,
                absence_kind=absence,
            )
            assert isinstance(value, ContextSourceAbsentFact)
            return value

        recall_desired = next(
            (
                item
                for item in (*sources.candidates, *sources.absent_facts)
                if item.source_kind is ContextSourceKind.MEMORY_RECALL
            ),
            None,
        )
        without_recall = replace_memory_context_sources(
            sources,
            (
                fallback_source(
                    ContextSourceKind.MEMORY_RECALL,
                    recall_reservation,
                    recall_desired,
                ),
            ),
        )
        try:
            return await compile_one(without_recall), without_recall
        except StructuredModelInputCompileError as exc:
            if exc.kind not in budget_failures:
                raise

        without_optional_values = replace_memory_context_sources(
            without_recall,
            (
                fallback_source(
                    ContextSourceKind.MEMORY_RESPONSE_PREFERENCE_HEAD,
                    preference_reservation,
                    preference_source,
                ),
            ),
        )
        return await compile_one(without_optional_values), without_optional_values

__all__ = ["MemoryContextProjectionPort", "MemoryDispatchSupport"]
