"""Precompile compaction admission and complete-wire policy decisions."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from pulsara_agent.conversation_kernel.compaction.contracts import (
    CompactionScope,
    CompactionTrigger,
)
from pulsara_agent.conversation_kernel.compaction.planner import (
    compaction_effective_history_has_image,
    CompactionPlanningError,
    freeze_compaction_source_view,
    should_trigger_compaction,
)
from pulsara_agent.conversation_kernel.memory.contracts import MemoryUsePolicy
from pulsara_agent.conversation_kernel.repository import StaleHostWriter
from pulsara_agent.conversation_kernel.provider_dispatch import (
    PreparedCompactionSourceDispatch,
    PreparedProviderDispatch,
    PreparedProviderHeadroomAdmission,
    PreparedWireMeasurementDecision,
    HandleFreeProviderWireObservation,
)
from pulsara_agent.model_input.contracts import (
    ModelInputScopeKind,
    ModelInputCompileFailureKind,
    StructuredModelInputCompileError,
)
from pulsara_agent.model_input.continuity import ProviderInputContinuityScope
from pulsara_agent.llm.request import MAXIMUM_PROVIDER_WIRE_INPUT_BYTES
from pulsara_agent.llm.errors import ModelTargetCapabilityMismatch
from pulsara_agent.llm.input import content_has_image
from pulsara_agent.conversation_kernel.direct_model import PreparedKernelModelTarget
from pulsara_agent.llm.frozen_target import FrozenEpochModelTargetBundle
from pulsara_agent.conversation_kernel.compaction.execution_contracts import (
    AutomaticCompactionTriggerCandidate,
    ModelSwitchCompactionTriggerCandidate,
    ModelSwitchDirectPrecompileDecision,
    OrdinaryPrecompileDecision,
    PrecompileCompactionDecision,
)


def _target_is_known_text_only(target: PreparedKernelModelTarget) -> bool:
    modalities = target.target.fact.input_modalities
    return modalities is not None and "text" in modalities and "image" not in modalities


class CompactionAdmissionOperations:
    """Admission operations on the coordinator's existing owners and state."""

    async def prepare_precompile_admission(
        self,
        *,
        turn_id: str,
        model_call_index: int,
        deadline: float,
    ) -> PreparedProviderHeadroomAdmission:
        return await self._provider_dispatch.prepare_headroom_admission(
            turn_id=turn_id,
            model_call_index=model_call_index,
            deadline=deadline,
        )

    def automatic_allowed(
        self,
        *,
        scope_kind: ModelInputScopeKind,
    ) -> bool:
        return (
            self._compaction_owner is not None
            and self._compaction_owner.policy.enabled
            and self._compaction_owner.policy.automatic_enabled
            and (
                self._compaction_owner.automatic_allowed(
                    scope_kind=scope_kind,
                )
            )
        )

    def precompile_needed(
        self,
        *,
        scope_kind: ModelInputScopeKind,
        allow_model_switch: bool,
    ) -> bool:
        if self.automatic_allowed(scope_kind=scope_kind):
            return True
        if not allow_model_switch:
            return False
        # The first call must inspect canonical history even when no installed A
        # survives (cold restart/fork).  A known text-only B may need a destination projection.
        return True

    def context_allowance_change_pending(
        self,
        *,
        scope_kind: ModelInputScopeKind,
        destination: PreparedKernelModelTarget,
    ) -> bool:
        source = self._provider_dispatch.installed_source_target(
            scope=ProviderInputContinuityScope(
                session_id=self._writer_lease.guard.session_id,
                scope_kind=scope_kind,
            ),
            destination=destination,
        )
        if source is None:
            return False
        target_fact = destination.epoch_call_target.target_bundle.target_fact
        # Only the user allowance can grant a later-call handover. Physical
        # limits, identity, reasoning, projection and estimator must still match.
        return (
            replace(
                source,
                target_fact=source.target_fact.model_copy(
                    update={
                        "context_window_tokens": target_fact.context_window_tokens,
                        "context_budget": target_fact.context_budget,
                    }
                ),
            )
            == destination.epoch_call_target.target_bundle
        )

    async def prepare_precompile(
        self,
        *,
        turn_id: str,
        model_call_index: int,
        inherited_memory_use_policy: MemoryUsePolicy,
        trigger: CompactionTrigger,
        scope_kind: ModelInputScopeKind,
        headroom_admission: PreparedProviderHeadroomAdmission,
        deadline: float,
        allow_model_switch: bool = False,
        first_call_history_through_sequence: int | None = None,
    ) -> PrecompileCompactionDecision | None:
        owner = self._compaction_owner
        if owner is None:
            headroom_admission.close()
            return None
        try:
            scope = CompactionScope(
                session_id=self._writer_lease.guard.session_id,
                workspace_id=await self._resolved_workspace_id(deadline=deadline),
                turn_id=turn_id,
                scope_kind=scope_kind,
            )
        except BaseException:
            headroom_admission.close()
            raise

        dispatch: PreparedCompactionSourceDispatch | None = None
        source_target: FrozenEpochModelTargetBundle | None = None
        headroom_consumed = False
        try:
            preflight = headroom_admission.preflight
            prepared_target = headroom_admission.prepared_target
            if allow_model_switch:
                source_target = self._provider_dispatch.installed_source_target(
                    scope=ProviderInputContinuityScope(
                        session_id=scope.session_id,
                        scope_kind=scope.scope_kind,
                    ),
                    destination=prepared_target,
                )
                if _target_is_known_text_only(prepared_target):
                    history_read = await self._io.run(
                        self._input_reader.read_frozen_compaction_cut,
                        headroom_admission.cut,
                        deadline_monotonic=deadline,
                    )
                    if (
                        history_read.scope != scope
                        or history_read.turn_status != "RUNNING"
                        or preflight.effective_materialization_lineage_floor
                        != history_read.safe_head_range.effective_materialization_lineage_floor
                    ):
                        raise CompactionPlanningError(
                            "model-switch history preflight changed at admission"
                        )
                    if first_call_history_through_sequence is not None and any(
                        item.source_entry_sequence is not None
                        and item.source_entry_sequence
                        > first_call_history_through_sequence
                        and isinstance(item.content, tuple)
                        and content_has_image(item.content)
                        for item in history_read.dispatch_read.compile_snapshot.canonical_input.items
                    ):
                        raise ModelTargetCapabilityMismatch(
                            "the current input group contains images unsupported by the selected model"
                        )
                    if compaction_effective_history_has_image(
                        history_read,
                        first_call_history_through_sequence=first_call_history_through_sequence,
                    ):
                        if not owner.policy.enabled:
                            raise StructuredModelInputCompileError(
                                ModelInputCompileFailureKind.MODEL_SWITCH_REQUIRES_COMPACTION
                            )
                        headroom_admission.close()
                        return ModelSwitchCompactionTriggerCandidate(
                            destination_target=prepared_target,
                        )
            transferred_handle = headroom_admission.take_handle()
            headroom_consumed = True
            dispatch = await self._provider_dispatch.prepare_compaction_source(
                turn_id=turn_id,
                model_call_index=model_call_index,
                inherited_memory_use_policy=inherited_memory_use_policy,
                deadline=deadline,
                existing_handle=transferred_handle,
                headroom_preflight_override=preflight,
                prepared_target_override=prepared_target,
            )
            canonical_read = await self._io.run(
                self._input_reader.read_frozen_compaction_cut,
                dispatch.cut,
                deadline_monotonic=deadline,
            )
            if canonical_read.scope != scope or canonical_read.turn_status != "RUNNING":
                raise CompactionPlanningError(
                    "precompile compaction target changed at admission"
                )
            canonical_range = canonical_read.safe_head_range
            if (
                preflight.effective_materialization_lineage_floor
                != canonical_range.effective_materialization_lineage_floor
            ):
                raise CompactionPlanningError(
                    "compaction headroom quote differs from its frozen source"
                )
            if source_target is not None:
                quote = dispatch.wire_quote
                destination_trigger = int(
                    quote.effective_input_budget_tokens
                    * owner.policy.auto_trigger_ratio
                )
                if (
                    quote.budget_input_tokens < destination_trigger
                    and quote.final_wire_utf8_bytes <= MAXIMUM_PROVIDER_WIRE_INPUT_BYTES
                ):
                    handle, measurement = dispatch.take_below_trigger_ownership()
                    dispatch = None
                    measurement.discard_materialization_to_quote()
                    return ModelSwitchDirectPrecompileDecision(
                        ordinary_admission=PreparedProviderHeadroomAdmission(
                            handle, preflight, prepared_target
                        ),
                        switch_candidate=ModelSwitchCompactionTriggerCandidate(
                            destination_target=prepared_target,
                        ),
                    )
                dispatch.discard_wire_materialization_to_quote()
                dispatch.close()
                dispatch = None
                if not owner.policy.enabled:
                    raise StructuredModelInputCompileError(
                        ModelInputCompileFailureKind.MODEL_SWITCH_REQUIRES_COMPACTION
                    )
                return ModelSwitchCompactionTriggerCandidate(
                    destination_target=prepared_target,
                )
            source_view = freeze_compaction_source_view(
                canonical_read=canonical_read,
                compile_binding=dispatch.prepared_call.compile_binding,
                semantic_projection=dispatch.projection,
                predecessor_epoch_view=dispatch.planning.predecessor_view,
                provider_wire_quote=dispatch.wire_quote,
                producer_wire_api=(
                    dispatch.prepared_call.call.target.model_profile.route_wire_profile.wire_api
                ),
            )
            if not self.automatic_allowed(scope_kind=scope_kind):
                wire_candidate = dispatch.wire_candidate
                handle, measurement = dispatch.take_below_trigger_ownership()
                dispatch = None
                ordinary = PreparedProviderHeadroomAdmission(
                    handle, preflight, prepared_target
                )
                observation = HandleFreeProviderWireObservation(
                    candidate=wire_candidate,
                    measurement=measurement,
                )
                return OrdinaryPrecompileDecision(
                    ordinary_admission=ordinary,
                    reusable_wire_observation=observation,
                )
            if not should_trigger_compaction(
                source_view=source_view,
                policy=owner.policy,
                force=False,
            ):
                wire_candidate = dispatch.wire_candidate
                handle, measurement = dispatch.take_below_trigger_ownership()
                dispatch = None
                ordinary: PreparedProviderHeadroomAdmission | None = None
                observation: HandleFreeProviderWireObservation | None = None
                try:
                    ordinary = PreparedProviderHeadroomAdmission(
                        handle,
                        preflight,
                        prepared_target,
                    )
                    observation = HandleFreeProviderWireObservation(
                        candidate=wire_candidate,
                        measurement=measurement,
                    )
                    return OrdinaryPrecompileDecision(
                        ordinary_admission=ordinary,
                        reusable_wire_observation=observation,
                    )
                except BaseException:
                    if observation is None:
                        measurement.discard_materialization_to_quote()
                    else:
                        observation.discard()
                    if ordinary is None:
                        handle.close()
                    else:
                        ordinary.close()
                    raise
            dispatch.discard_wire_materialization_to_quote()
            dispatch.close()
            dispatch = None
            return AutomaticCompactionTriggerCandidate(trigger=trigger)
        except (asyncio.CancelledError, StaleHostWriter):
            if dispatch is not None:
                dispatch.close()
            raise
        except StructuredModelInputCompileError as error:
            if dispatch is not None:
                dispatch.close()
            elif not headroom_consumed:
                headroom_admission.close()
            if source_target is not None or error.kind is (
                ModelInputCompileFailureKind.MODEL_SWITCH_REQUIRES_COMPACTION
            ):
                raise
            owner.record_automatic_failure(
                scope_kind=scope_kind,
            )
            return None
        except BaseException:
            if dispatch is not None:
                dispatch.close()
            elif not headroom_consumed:
                headroom_admission.close()
            if source_target is not None:
                raise
            owner.record_automatic_failure(
                scope_kind=scope_kind,
            )
            return None

    async def measure_dispatch_wire(
        self,
        dispatch: PreparedProviderDispatch,
        *,
        deadline: float,
        reusable_observation: HandleFreeProviderWireObservation | None = None,
    ) -> PreparedWireMeasurementDecision:
        decision = await self._provider_dispatch.measure_prepared_wire_candidate(
            self._provider_dispatch.wire_candidate_for_dispatch(dispatch),
            deadline=deadline,
            reusable_observation=reusable_observation,
        )
        dispatch.select_wire_candidate(decision.candidate)
        return decision

    def direct_switch_wire_admitted(
        self, decision: PreparedWireMeasurementDecision
    ) -> bool:
        """Admit a complete direct-switch input below the automatic trigger."""
        owner = self._compaction_owner
        return (
            owner is not None
            and decision.wire_input_plan is not None
            and decision.quote.budget_input_tokens
            < int(
                decision.quote.effective_input_budget_tokens
                * owner.policy.auto_trigger_ratio
            )
        )

    def wire_decision_crosses_automatic_threshold(
        self,
        dispatch: PreparedProviderDispatch,
        decision: PreparedWireMeasurementDecision,
    ) -> bool:
        owner = self._compaction_owner
        if (
            owner is None
            or not owner.policy.enabled
            or not self.automatic_allowed(
                scope_kind=(
                    dispatch.canonical_read.compile_snapshot.canonical_input.identity.conversation_scope_kind
                )
            )
        ):
            return False
        if decision.candidate != self._provider_dispatch.wire_candidate_for_dispatch(
            dispatch
        ):
            raise RuntimeError("wire decision belongs to another provider dispatch")
        quote = decision.quote
        budget = quote.effective_input_budget_tokens
        if quote.budget_input_tokens >= int(budget * owner.policy.auto_trigger_ratio):
            return True
        return False
