"""Session-owned pure reducer for canonical provider-input generations."""

from __future__ import annotations

from dataclasses import dataclass
from threading import RLock

from pulsara_agent.event import (
    AgentEvent,
    ContextCompiledEvent,
    ExistingGenerationPreparationAbandonedEvent,
    ModelCallControlDispositionResolvedEvent,
    ModelCallEndEvent,
    ModelCallStartEvent,
    ModelCallTerminalProjectionCommittedEvent,
    ProviderInputAppendCommittedEvent,
    ProviderInputGenerationClosedEvent,
    ProviderInputGenerationRolloverResolvedEvent,
    ProviderInputGenerationStartedEvent,
    RunEndEvent,
    ScopedGenerationPreparationAbandonedEvent,
)
from pulsara_agent.event_log.serialization import freeze_event_write_candidate
from pulsara_agent.primitives._context_base import ContextEventReferenceFact
from pulsara_agent.primitives.context import context_fingerprint
from pulsara_agent.primitives.context_source import LedgerAuthorityHorizonFact
from pulsara_agent.primitives.frozen import StableEventIdentityFact, build_frozen_fact
from pulsara_agent.primitives.model_call import ModelCallControlDisposition
from pulsara_agent.primitives.provider_input import (
    CommittedProviderInputGenerationCoreStateFact,
    CommittedRuntimeObservationSourceHeadFact,
    InlineProviderInputUnitHydrationAttributionFact,
    OneShotGenerationScopeFact,
    ProviderInputAwaitingControlDispositionFact,
    ProviderInputContinuationMaterializationProofFact,
    ProviderInputUnitPlacementAttributionFact,
    ProviderInputGenerationAttributionStateFact,
    ProviderInputGenerationScopeBindingFact,
    ProviderInvocationContextFramePlacementFact,
    ProviderInputPendingContinuationFact,
    ProviderInputPreparationOwnershipFact,
    ProviderInputPreparationOwnershipAttributionFact,
    ProviderInputReplayBindingIdentityFact,
    ProviderTranscriptFrontierFact,
    InitialGenerationCommitGuardFact,
    ExistingAppendCommitGuardFact,
    RolloverGenerationCommitGuardFact,
    SessionProviderInputContinuityScopeFact,
    ProviderSourceDispositionRewriteAuthorityFact,
)
from pulsara_agent.primitives.runtime_observation import (
    PreparedRuntimeObservationProviderUnitFact,
    RuntimeObservationProjectionRewriteFact,
)
from pulsara_agent.runtime.provider_input.materialization import (
    RecursivelyImmutableProviderInputCarrier,
    build_provider_unit_semantic_document,
)
from pulsara_agent.runtime.provider_input.observation_rewrite import (
    RuntimeObservationLifecycleReducerState,
    advance_runtime_observation_lifecycle_state,
    validate_runtime_observation_rewrite_transition,
    validate_runtime_observation_source_head_transition,
)
from pulsara_agent.runtime.provider_input.resident import (
    DEFAULT_PROVIDER_INPUT_RESIDENT_MANAGER,
    ProviderInputResidentBudgetManager,
    ProviderInputResidentCacheKey,
)
from pulsara_agent.runtime.provider_input.vector import (
    PersistentProviderInputUnitSequence,
    ProviderInputVectorState,
    provider_input_artifact_namespace,
)


class ProviderInputGenerationReducerError(RuntimeError):
    pass


def _validate_continuation_materialization(
    *,
    pending: ProviderInputPendingContinuationFact | None,
    proof: ProviderInputContinuationMaterializationProofFact | None,
    predecessor_frontier: ProviderTranscriptFrontierFact,
    resulting_core: CommittedProviderInputGenerationCoreStateFact,
    append_event: ProviderInputAppendCommittedEvent,
    resident: "ProviderInputResidentGeneration | None",
    require_staged_resident: bool,
) -> None:
    if pending is None:
        if proof is not None:
            raise ProviderInputGenerationReducerError(
                "provider append supplied a continuation proof without pending authority"
            )
        return
    if proof is None:
        raise ProviderInputGenerationReducerError(
            "provider append consumed continuation without materialization proof"
        )
    if (
        proof.pending_continuation_fingerprint != pending.continuation_fingerprint
        or proof.terminal_projection_reference != pending.terminal_projection_reference
        or proof.predecessor_transcript_frontier_fingerprint
        != predecessor_frontier.provider_semantic_frontier_fingerprint
        or proof.resulting_transcript_frontier_fingerprint
        != resulting_core.transcript_frontier.provider_semantic_frontier_fingerprint
        or (
            append_event.expected_revision > 0
            and resulting_core.transcript_frontier.committed_transcript_unit_count
            <= predecessor_frontier.committed_transcript_unit_count
        )
        or (
            append_event.expected_revision == 0
            and resulting_core.transcript_frontier.committed_transcript_unit_count
            < len(proof.appended_unit_ordinals)
        )
    ):
        raise ProviderInputGenerationReducerError(
            "provider continuation proof authority join failed"
        )
    if resident is None:
        if require_staged_resident:
            raise ProviderInputGenerationReducerError(
                "provider continuation proof lacks staged resident materialization"
            )
        # Reopen intentionally has no process-local resident. The durable event
        # already joins the proof to the append semantic and resulting vector
        # root; the vector loader revalidates the content-addressed artifacts
        # before the generation may be dispatched again.
        return
    try:
        units = tuple(
            resident.units[ordinal] for ordinal in proof.appended_unit_ordinals
        )
    except IndexError as exc:
        raise ProviderInputGenerationReducerError(
            "provider continuation proof exceeds resident vector"
        ) from exc
    if (
        tuple(item.attribution.semantic.semantic_fingerprint for item in units)
        != proof.ordered_appended_unit_semantic_fingerprints
        or tuple(item.materialization_fingerprint for item in units)
        != proof.ordered_appended_unit_materialization_fingerprints
        or tuple(item.attribution.owner_semantic_fingerprint for item in units)
        != proof.ordered_appended_unit_owner_semantic_fingerprints
        or any(
            item.attribution.semantic.unit_kind != "transcript_message"
            or getattr(item.canonical_provider_fragment, "role", None) == "user"
            for item in units
        )
    ):
        raise ProviderInputGenerationReducerError(
            "provider continuation proof differs from staged transcript units"
        )
    if append_event.resulting_core_state != resulting_core:
        raise ProviderInputGenerationReducerError(
            "provider continuation proof resulting core drifted"
        )


@dataclass(frozen=True, slots=True, weakref_slot=True)
class ProviderInputResidentGeneration:
    units: PersistentProviderInputUnitSequence
    vector_state: ProviderInputVectorState
    carrier: RecursivelyImmutableProviderInputCarrier
    authority_horizons: tuple[LedgerAuthorityHorizonFact, ...]
    replay_bindings: tuple[ProviderInputReplayBindingIdentityFact, ...]
    reachable_artifact_ids: frozenset[str]

    def __post_init__(self) -> None:
        if self.units is not self.vector_state.units:
            raise ValueError("provider resident unit/vector state drifted")
        if self.carrier.input_unit_count != len(self.units):
            raise ValueError("provider resident carrier unit count drifted")
        horizon_owners = tuple(
            item.runtime_session_id for item in self.authority_horizons
        )
        if horizon_owners != tuple(sorted(set(horizon_owners))):
            raise ValueError("provider resident authority horizons are not canonical")
        replay_fingerprints = tuple(
            item.identity_fingerprint for item in self.replay_bindings
        )
        if replay_fingerprints != tuple(sorted(set(replay_fingerprints))):
            raise ValueError("provider resident replay bindings are not canonical")


@dataclass(frozen=True, slots=True)
class ProviderInputGenerationSnapshot:
    through_sequence: int
    scope_binding: ProviderInputGenerationScopeBindingFact
    core_state: CommittedProviderInputGenerationCoreStateFact | None
    attribution_state: ProviderInputGenerationAttributionStateFact | None
    preparation_attribution: ProviderInputPreparationOwnershipAttributionFact | None
    resident: ProviderInputResidentGeneration | None
    frame_placements: tuple[ProviderInvocationContextFramePlacementFact, ...]
    runtime_observation_units: tuple[
        PreparedRuntimeObservationProviderUnitFact, ...
    ]
    runtime_observation_rewrites: tuple[
        RuntimeObservationProjectionRewriteFact, ...
    ]
    runtime_observation_lifecycle_state: (
        RuntimeObservationLifecycleReducerState | None
    )


@dataclass(frozen=True, slots=True)
class ProviderInputPreparationRecoverySnapshot:
    through_sequence: int
    attribution: ProviderInputPreparationOwnershipAttributionFact
    scope_binding: ProviderInputGenerationScopeBindingFact
    committed_core: CommittedProviderInputGenerationCoreStateFact | None


class ProviderInputGenerationStore:
    """One deterministic state machine shared by live commit and reopen replay."""

    def __init__(
        self,
        events: tuple[AgentEvent, ...] = (),
        *,
        runtime_session_id: str,
        through_sequence: int = 0,
        resident_manager: ProviderInputResidentBudgetManager | None = None,
    ) -> None:
        self._runtime_session_id = runtime_session_id
        self._lock = RLock()
        self._through_sequence = 0
        self._cores: dict[str, CommittedProviderInputGenerationCoreStateFact] = {}
        self._attributions: dict[str, ProviderInputGenerationAttributionStateFact] = {}
        self._bindings: dict[str, ProviderInputGenerationScopeBindingFact] = {}
        self._preparations: dict[
            str, ProviderInputPreparationOwnershipAttributionFact
        ] = {}
        self._call_to_generation: dict[str, str] = {}
        self._append_identities_by_call: dict[str, StableEventIdentityFact] = {}
        self._frame_placements: dict[
            str, tuple[ProviderInvocationContextFramePlacementFact, ...]
        ] = {}
        self._runtime_observation_units: dict[
            str, tuple[PreparedRuntimeObservationProviderUnitFact, ...]
        ] = {}
        self._runtime_observation_rewrites: dict[
            str, tuple[RuntimeObservationProjectionRewriteFact, ...]
        ] = {}
        self._runtime_observation_lifecycle_states: dict[
            str, RuntimeObservationLifecycleReducerState
        ] = {}
        self._append_events: dict[
            str, tuple[ProviderInputAppendCommittedEvent, ...]
        ] = {}
        self._resident_manager = (
            resident_manager or DEFAULT_PROVIDER_INPUT_RESIDENT_MANAGER
        )
        self._staged_ownerships: dict[str, ProviderInputPreparationOwnershipFact] = {}
        if events:
            self._apply_committed(events, require_staged_resident=False)
        if through_sequence < self._through_sequence:
            raise ValueError("provider generation bootstrap high-water moved backwards")
        self._through_sequence = through_sequence

    @classmethod
    def from_sparse_bootstrap(
        cls,
        events: tuple[AgentEvent, ...],
        *,
        runtime_session_id: str,
        through_sequence: int,
    ) -> "ProviderInputGenerationStore":
        store = cls(runtime_session_id=runtime_session_id)
        ordered = sorted(events, key=lambda item: item.sequence or 0)
        groups: list[list[AgentEvent]] = []
        for event in ordered:
            if event.sequence is None:
                raise ValueError("provider generation bootstrap event is uncommitted")
            if (
                groups
                and groups[-1][-1].sequence is not None
                and event.sequence == groups[-1][-1].sequence + 1
            ):
                groups[-1].append(event)
            else:
                groups.append([event])
        for group in groups:
            first_sequence = group[0].sequence
            assert first_sequence is not None
            store._through_sequence = first_sequence - 1
            store._apply_committed(
                tuple(group),
                require_staged_resident=False,
            )
        if store._through_sequence > through_sequence:
            raise ValueError("provider generation bootstrap exceeds ledger high-water")
        store._through_sequence = through_sequence
        return store

    @property
    def through_sequence(self) -> int:
        with self._lock:
            return self._through_sequence

    def empty_scope_binding(
        self, scope_fingerprint: str
    ) -> ProviderInputGenerationScopeBindingFact:
        return build_frozen_fact(
            ProviderInputGenerationScopeBindingFact,
            schema_version="provider_input_generation_scope_binding.v1",
            scope_fingerprint=scope_fingerprint,
            active_generation_id=None,
            latest_closed_generation_id=None,
            active_preparation_id=None,
        )

    def snapshot(self, scope_fingerprint: str) -> ProviderInputGenerationSnapshot:
        with self._lock:
            binding = self._bindings.get(scope_fingerprint) or self.empty_scope_binding(
                scope_fingerprint
            )
            generation_id = binding.active_generation_id
            preparation = (
                self._preparations.get(binding.active_preparation_id)
                if binding.active_preparation_id is not None
                else None
            )
            return ProviderInputGenerationSnapshot(
                through_sequence=self._through_sequence,
                scope_binding=binding,
                core_state=(self._cores.get(generation_id) if generation_id else None),
                attribution_state=(
                    self._attributions.get(generation_id) if generation_id else None
                ),
                preparation_attribution=preparation,
                resident=(
                    self._resident_manager.get(
                        self._resident_key("generation", generation_id)
                    )
                    if generation_id
                    else None
                ),
                frame_placements=self._frame_placements.get(generation_id, ()),
                runtime_observation_units=(
                    self._runtime_observation_units.get(generation_id, ())
                ),
                runtime_observation_rewrites=(
                    self._runtime_observation_rewrites.get(generation_id, ())
                ),
                runtime_observation_lifecycle_state=(
                    self._runtime_observation_lifecycle_states.get(generation_id)
                    if generation_id
                    else None
                ),
            )

    def active_preparation_snapshots(
        self,
    ) -> tuple[ProviderInputPreparationRecoverySnapshot, ...]:
        with self._lock:
            snapshots = []
            for preparation_id in sorted(self._preparations):
                attribution = self._preparations[preparation_id]
                owner = attribution.ownership
                binding = self._bindings.get(
                    owner.scope_fingerprint
                ) or self.empty_scope_binding(owner.scope_fingerprint)
                snapshots.append(
                    ProviderInputPreparationRecoverySnapshot(
                        through_sequence=self._through_sequence,
                        attribution=attribution,
                        scope_binding=binding,
                        committed_core=(
                            next(
                                (
                                    core
                                    for core in self._cores.values()
                                    if core.core_state_fingerprint
                                    == owner.expected_committed_core_state_fingerprint
                                ),
                                None,
                            )
                            if owner.expected_committed_core_state_fingerprint
                            is not None
                            else None
                        ),
                    )
                )
            return tuple(snapshots)

    def preparation_snapshot(
        self, preparation_id: str
    ) -> ProviderInputPreparationRecoverySnapshot | None:
        return next(
            (
                item
                for item in self.active_preparation_snapshots()
                if item.attribution.ownership.preparation_id == preparation_id
            ),
            None,
        )

    def latest_open_session_continuity_snapshot(
        self,
        *,
        call_lane: str,
    ) -> ProviderInputGenerationSnapshot | None:
        """Return the open session-continuity generation for one call lane."""

        with self._lock:
            candidates = tuple(
                core
                for core in self._cores.values()
                if core.status == "open"
                and core.generation.call_lane == call_lane
                and isinstance(
                    core.generation.scope,
                    SessionProviderInputContinuityScopeFact,
                )
                and core.generation.scope.runtime_session_id == self._runtime_session_id
            )
            if not candidates:
                return None
            core = max(
                candidates,
                key=lambda item: (item.revision, item.generation.generation_id),
            )
            scope_fingerprint = core.generation.scope.scope_fingerprint
            binding = self._bindings.get(scope_fingerprint) or self.empty_scope_binding(
                scope_fingerprint
            )
            return ProviderInputGenerationSnapshot(
                through_sequence=self._through_sequence,
                scope_binding=binding,
                core_state=core,
                attribution_state=self._attributions.get(core.generation.generation_id),
                preparation_attribution=(
                    self._preparations.get(binding.active_preparation_id)
                    if binding.active_preparation_id is not None
                    else None
                ),
                resident=self._resident_manager.get(
                    self._resident_key("generation", core.generation.generation_id)
                ),
                frame_placements=self._frame_placements.get(
                    core.generation.generation_id, ()
                ),
                runtime_observation_units=self._runtime_observation_units.get(
                    core.generation.generation_id, ()
                ),
                runtime_observation_rewrites=(
                    self._runtime_observation_rewrites.get(
                        core.generation.generation_id, ()
                    )
                ),
                runtime_observation_lifecycle_state=(
                    self._runtime_observation_lifecycle_states.get(
                        core.generation.generation_id
                    )
                ),
            )

    def open_session_continuity_snapshots(
        self,
    ) -> tuple[ProviderInputGenerationSnapshot, ...]:
        """Freeze every live session-continuity generation for teardown."""

        with self._lock:
            cores = tuple(
                sorted(
                    (
                        core
                        for core in self._cores.values()
                        if core.status == "open"
                        and isinstance(
                            core.generation.scope,
                            SessionProviderInputContinuityScopeFact,
                        )
                        and core.generation.scope.runtime_session_id
                        == self._runtime_session_id
                    ),
                    key=lambda item: item.generation.generation_id,
                )
            )
            snapshots: list[ProviderInputGenerationSnapshot] = []
            for core in cores:
                scope_fingerprint = core.generation.scope.scope_fingerprint
                binding = self._bindings.get(
                    scope_fingerprint
                ) or self.empty_scope_binding(scope_fingerprint)
                snapshots.append(
                    ProviderInputGenerationSnapshot(
                        through_sequence=self._through_sequence,
                        scope_binding=binding,
                        core_state=core,
                        attribution_state=self._attributions.get(
                            core.generation.generation_id
                        ),
                        preparation_attribution=(
                            self._preparations.get(binding.active_preparation_id)
                            if binding.active_preparation_id is not None
                            else None
                        ),
                        resident=self._resident_manager.get(
                            self._resident_key(
                                "generation", core.generation.generation_id
                            )
                        ),
                        frame_placements=self._frame_placements.get(
                            core.generation.generation_id, ()
                        ),
                        runtime_observation_units=(
                            self._runtime_observation_units.get(
                                core.generation.generation_id, ()
                            )
                        ),
                        runtime_observation_rewrites=(
                            self._runtime_observation_rewrites.get(
                                core.generation.generation_id, ()
                            )
                        ),
                        runtime_observation_lifecycle_state=(
                            self._runtime_observation_lifecycle_states.get(
                                core.generation.generation_id
                            )
                        ),
                    )
                )
            return tuple(snapshots)

    def stage_prepared_resident(
        self,
        ownership: ProviderInputPreparationOwnershipFact,
        resident: ProviderInputResidentGeneration,
    ) -> None:
        preparation_id = ownership.preparation_id
        key = self._resident_key("preparation", preparation_id)
        with self._lock:
            existing_owner = self._staged_ownerships.get(
                ownership.ownership_fingerprint
            )
            if existing_owner is not None and existing_owner != ownership:
                raise ProviderInputGenerationReducerError(
                    "provider preparation ownership identity conflict"
                )
            if any(
                item.scope_fingerprint == ownership.scope_fingerprint
                and item.ownership_fingerprint != ownership.ownership_fingerprint
                for item in self._staged_ownerships.values()
            ):
                raise ProviderInputGenerationReducerError(
                    "provider preparation scope already has a staged owner"
                )
            existing = self._resident_manager.get(key)
            if existing is not None and existing != resident:
                raise ProviderInputGenerationReducerError(
                    "provider preparation resident identity conflict"
                )
            self._staged_ownerships[ownership.ownership_fingerprint] = ownership
            self._resident_manager.admit(key, resident)

    def has_staged_preparation_for_scope(self, scope_fingerprint: str) -> bool:
        with self._lock:
            return any(
                item.scope_fingerprint == scope_fingerprint
                for item in self._staged_ownerships.values()
            )

    def stage_ephemeral_preparation(
        self,
        ownership: ProviderInputPreparationOwnershipFact,
        resident: ProviderInputResidentGeneration,
    ) -> None:
        """Own a one-shot candidate until its atomic Start batch is committed."""

        with self._lock:
            existing = self._staged_ownerships.get(ownership.ownership_fingerprint)
            if existing is not None and existing != ownership:
                raise ProviderInputGenerationReducerError(
                    "provider ephemeral preparation identity conflict"
                )
            if any(
                item.scope_fingerprint == ownership.scope_fingerprint
                and item.ownership_fingerprint != ownership.ownership_fingerprint
                for item in self._staged_ownerships.values()
            ):
                raise ProviderInputGenerationReducerError(
                    "provider ephemeral scope already has a staged owner"
                )
            self._staged_ownerships[ownership.ownership_fingerprint] = ownership
            key = self._resident_key("preparation", ownership.preparation_id)
            current = self._resident_manager.get(key)
            if current is not None and current != resident:
                raise ProviderInputGenerationReducerError(
                    "provider ephemeral preparation resident conflict"
                )
            self._resident_manager.admit(key, resident)

    def install_restored_resident(
        self,
        generation_id: str,
        resident: ProviderInputResidentGeneration,
    ) -> None:
        with self._lock:
            core = self._cores.get(generation_id)
            if core is None:
                raise ProviderInputGenerationReducerError(
                    "cannot restore resident state for an unknown generation"
                )
            key = self._resident_key("generation", generation_id)
            existing = self._resident_manager.get(key)
            if existing is not None and existing != resident:
                raise ProviderInputGenerationReducerError(
                    "provider resident restore conflicts with live state"
                )
            self._resident_manager.admit(key, resident)
            attribution = self._attributions.get(generation_id)
            if attribution is not None:
                source_attributions = _source_head_attributions_from_resident(
                    core=core,
                    append_events=self._append_events.get(generation_id, ()),
                    resident=resident,
                    runtime_session_id=self._runtime_session_id,
                )
                self._attributions[generation_id] = build_frozen_fact(
                    ProviderInputGenerationAttributionStateFact,
                    schema_version="provider_input_generation_attribution_state.v3",
                    core_state=core,
                    source_head_attribution_status="complete",
                    source_head_attributions=source_attributions,
                    latest_model_start_event_ref=(
                        attribution.latest_model_start_event_ref
                    ),
                    latest_model_start_committed_core_fingerprint=(
                        attribution.latest_model_start_committed_core_fingerprint
                    ),
                    close_or_rollover_event_ref=(
                        attribution.close_or_rollover_event_ref
                    ),
                )

    def discard_staged_resident(self, preparation_id: str) -> None:
        with self._lock:
            if preparation_id not in self._preparations:
                self._resident_manager.discard(
                    self._resident_key("preparation", preparation_id)
                )
                self._staged_ownerships = {
                    fingerprint: owner
                    for fingerprint, owner in self._staged_ownerships.items()
                    if owner.preparation_id != preparation_id
                }

    def resident_cache_stats(self):
        return self._resident_manager.stats()

    def clear_resident_cache(self) -> None:
        self._resident_manager.discard_runtime_session(self._runtime_session_id)

    def _resident_key(
        self, owner_kind: str, owner_id: str
    ) -> ProviderInputResidentCacheKey:
        return ProviderInputResidentCacheKey(
            runtime_session_id=self._runtime_session_id,
            owner_kind=owner_kind,
            owner_id=owner_id,
        )

    def validate_start_guard(self, guard) -> None:
        """Validate preparation/core CAS while the RuntimeSession write lock is held."""

        with self._lock:
            preparation = next(
                (
                    item
                    for item in self._preparations.values()
                    if item.ownership.ownership_fingerprint
                    == guard.expected_preparation_ownership_fingerprint
                ),
                None,
            )
            staged_owner = self._staged_ownerships.get(
                guard.expected_preparation_ownership_fingerprint
            )
            if preparation is None and staged_owner is None:
                raise ProviderInputGenerationReducerError(
                    "provider start guard lacks preparation ownership"
                )
            owner = preparation.ownership if preparation is not None else staged_owner
            assert owner is not None
            binding = self._bindings.get(
                owner.scope_fingerprint
            ) or self.empty_scope_binding(owner.scope_fingerprint)
            expected_binding = binding
            if preparation is None:
                if (
                    binding.binding_fingerprint
                    != owner.expected_predecessor_scope_binding_fingerprint
                    or binding.active_preparation_id is not None
                ):
                    raise ProviderInputGenerationReducerError(
                        "ephemeral provider preparation predecessor CAS failed"
                    )
                expected_binding = build_frozen_fact(
                    ProviderInputGenerationScopeBindingFact,
                    schema_version="provider_input_generation_scope_binding.v1",
                    scope_fingerprint=binding.scope_fingerprint,
                    active_generation_id=binding.active_generation_id,
                    latest_closed_generation_id=binding.latest_closed_generation_id,
                    active_preparation_id=owner.preparation_id,
                )
            expected_guard_binding_fingerprint = (
                guard.expected_new_scope_binding_fingerprint
                if isinstance(guard, RolloverGenerationCommitGuardFact)
                else guard.expected_scope_binding_fingerprint
            )
            if (
                expected_binding.binding_fingerprint
                != expected_guard_binding_fingerprint
                or expected_binding.active_preparation_id != owner.preparation_id
                or owner.resolved_model_call_id != guard.resolved_model_call_id
            ):
                raise ProviderInputGenerationReducerError(
                    "provider start scope/preparation CAS failed"
                )
            if isinstance(guard, InitialGenerationCommitGuardFact):
                if (
                    binding.active_generation_id is not None
                    or owner.ownership_kind != "initial_start"
                    or owner.expected_committed_core_state_fingerprint is not None
                ):
                    raise ProviderInputGenerationReducerError(
                        "initial provider generation guard conflicts with live scope"
                    )
                return
            if isinstance(guard, ExistingAppendCommitGuardFact):
                core = self._cores.get(guard.generation_id)
                if (
                    core is None
                    or binding.active_generation_id != guard.generation_id
                    or owner.ownership_kind != "existing_append"
                    or core.core_state_fingerprint
                    != guard.expected_committed_core_state_fingerprint
                    or core.revision != guard.expected_revision
                    or core.committed_prefix_fingerprint
                    != guard.expected_committed_prefix_fingerprint
                    or core.transcript_frontier.provider_semantic_frontier_fingerprint
                    != guard.expected_transcript_frontier_fingerprint
                    or (
                        core.awaiting_control_disposition.awaiting_fingerprint
                        if core.awaiting_control_disposition is not None
                        else None
                    )
                    != guard.expected_awaiting_disposition_fingerprint
                    or (
                        core.accepted_but_not_appended_continuation.continuation_fingerprint
                        if core.accepted_but_not_appended_continuation is not None
                        else None
                    )
                    != guard.expected_pending_continuation_fingerprint
                ):
                    raise ProviderInputGenerationReducerError(
                        "existing provider append guard CAS failed"
                    )
                return
            if isinstance(guard, RolloverGenerationCommitGuardFact):
                core = self._cores.get(guard.old_generation_id)
                old_binding = self._bindings.get(
                    guard.old_scope_fingerprint
                ) or self.empty_scope_binding(guard.old_scope_fingerprint)
                if (
                    core is None
                    or owner.scope_fingerprint != guard.new_scope_fingerprint
                    or old_binding.binding_fingerprint
                    != guard.expected_old_scope_binding_fingerprint
                    or old_binding.active_generation_id != guard.old_generation_id
                    or owner.ownership_kind != "rollover_start"
                    or core.core_state_fingerprint
                    != guard.expected_old_core_state_fingerprint
                    or core.revision != guard.expected_old_revision
                    or core.committed_prefix_fingerprint
                    != guard.expected_old_prefix_fingerprint
                    or owner.expected_committed_core_state_fingerprint
                    != core.core_state_fingerprint
                ):
                    raise ProviderInputGenerationReducerError(
                        "provider rollover guard CAS failed"
                    )
                return
            raise ProviderInputGenerationReducerError(
                "unknown provider generation start guard"
            )

    def apply_committed(self, events: tuple[AgentEvent, ...]) -> None:
        """Fold one live committed batch with its staged physical owner."""

        self._apply_committed(events, require_staged_resident=True)

    def _apply_committed(
        self,
        events: tuple[AgentEvent, ...],
        *,
        require_staged_resident: bool,
    ) -> None:
        if not events:
            return
        with self._lock:
            expected = self._through_sequence + 1
            for event in events:
                if event.sequence is None or event.sequence != expected:
                    raise ProviderInputGenerationReducerError(
                        "provider generation reducer received a sequence gap"
                    )
                expected += 1
            # Validate on detached maps so a rejected batch cannot partially fold.
            cores = dict(self._cores)
            attributions = dict(self._attributions)
            bindings = dict(self._bindings)
            preparations = dict(self._preparations)
            call_to_generation = dict(self._call_to_generation)
            append_identities_by_call = dict(self._append_identities_by_call)
            frame_placements = dict(self._frame_placements)
            runtime_observation_units = dict(self._runtime_observation_units)
            runtime_observation_rewrites = dict(
                self._runtime_observation_rewrites
            )
            runtime_observation_lifecycle_states = dict(
                self._runtime_observation_lifecycle_states
            )
            append_events = dict(self._append_events)
            staged_ownerships = dict(self._staged_ownerships)
            resident_actions: list[
                tuple[
                    str,
                    ProviderInputResidentCacheKey,
                    ProviderInputResidentCacheKey | None,
                    ProviderInputResidentGeneration | None,
                ]
            ] = []
            rollover_pending_by_successor: dict[
                str, ProviderInputPendingContinuationFact | None
            ] = {}
            rollover_frontier_by_successor: dict[
                str, ProviderTranscriptFrontierFact
            ] = {}
            rollover_source_core_by_successor: dict[
                str, CommittedProviderInputGenerationCoreStateFact
            ] = {}

            for index, event in enumerate(events):
                if isinstance(event, ContextCompiledEvent):
                    prepared = event.prepared_provider_input
                    if prepared is None:
                        continue
                    owner = prepared.preparation_ownership
                    existing = preparations.get(owner.preparation_id)
                    attribution = build_frozen_fact(
                        ProviderInputPreparationOwnershipAttributionFact,
                        schema_version=(
                            "provider_input_preparation_ownership_attribution.v2"
                        ),
                        ownership=owner,
                        context_compiled_event_ref=_event_ref(
                            event, runtime_session_id=self._runtime_session_id
                        ),
                        prepared_candidate_fingerprint=(
                            prepared.candidate_fingerprint
                        ),
                        prepared_plan_fingerprint=(
                            prepared.prepared_plan.plan_fingerprint
                        ),
                        manifest_projection_reference_fingerprint=(
                            prepared.manifest_projection_reference.reference_fingerprint
                        ),
                        rollover_request_fingerprint=(
                            prepared.rollover_request.request_fingerprint
                            if prepared.rollover_request is not None
                            else None
                        ),
                    )
                    if existing is not None and existing != attribution:
                        raise ProviderInputGenerationReducerError(
                            "provider preparation ownership conflict"
                        )
                    binding = bindings.get(
                        owner.scope_fingerprint
                    ) or self.empty_scope_binding(owner.scope_fingerprint)
                    if (
                        binding.binding_fingerprint
                        != owner.expected_predecessor_scope_binding_fingerprint
                        or binding.active_preparation_id
                        not in {None, owner.preparation_id}
                    ):
                        raise ProviderInputGenerationReducerError(
                            "provider preparation scope CAS failed"
                        )
                    resulting_binding = build_frozen_fact(
                        ProviderInputGenerationScopeBindingFact,
                        schema_version="provider_input_generation_scope_binding.v1",
                        scope_fingerprint=binding.scope_fingerprint,
                        active_generation_id=binding.active_generation_id,
                        latest_closed_generation_id=binding.latest_closed_generation_id,
                        active_preparation_id=owner.preparation_id,
                    )
                    if (
                        resulting_binding.binding_fingerprint
                        != owner.resulting_scope_binding_fingerprint
                    ):
                        raise ProviderInputGenerationReducerError(
                            "provider preparation resulting scope drifted"
                        )
                    preparations[owner.preparation_id] = attribution
                    bindings[binding.scope_fingerprint] = resulting_binding
                    continue

                if isinstance(event, ProviderInputGenerationStartedEvent):
                    generation_id = event.generation.generation_id
                    if generation_id in cores:
                        if cores[generation_id] != event.genesis_core_state:
                            raise ProviderInputGenerationReducerError(
                                "provider generation genesis conflict"
                            )
                    else:
                        cores[generation_id] = event.genesis_core_state
                    continue

                if isinstance(event, ProviderInputAppendCommittedEvent):
                    generation_id = event.generation_id
                    owner_attribution = preparations.get(event.consumed_preparation_id)
                    staged_owner = staged_ownerships.get(
                        event.consumed_preparation_ownership_fingerprint
                    )
                    owner_fact = (
                        owner_attribution.ownership
                        if owner_attribution is not None
                        else staged_owner
                    )
                    predecessor = cores.get(generation_id)
                    if event.expected_revision == 0:
                        if predecessor is None:
                            raise ProviderInputGenerationReducerError(
                                "initial provider append lacks same-batch genesis"
                            )
                    elif predecessor is None:
                        raise ProviderInputGenerationReducerError(
                            "provider append lacks predecessor core"
                        )
                    assert predecessor is not None
                    recovered_one_shot = (
                        owner_fact is None
                        and event.expected_revision == 0
                        and isinstance(
                            predecessor.generation.scope,
                            OneShotGenerationScopeFact,
                        )
                        and any(
                            isinstance(candidate, ProviderInputGenerationStartedEvent)
                            and candidate.generation.generation_id == generation_id
                            and candidate.expected_initial_append_event_id == event.id
                            and candidate.expected_model_start_event_id
                            == event.expected_model_start_event_id
                            for candidate in events[:index]
                        )
                    )
                    if not recovered_one_shot and (
                        owner_fact is None
                        or owner_fact.ownership_fingerprint
                        != event.consumed_preparation_ownership_fingerprint
                    ):
                        raise ProviderInputGenerationReducerError(
                            "provider append lacks its prepared owner"
                        )
                    if owner_attribution is not None and (
                        event.append_kind != "compiled_manifest"
                        or event.prepared_provider_input_candidate_fingerprint
                        != owner_attribution.prepared_candidate_fingerprint
                        or event.manifest_projection_reference is None
                        or event.manifest_projection_reference.reference_fingerprint
                        != owner_attribution.manifest_projection_reference_fingerprint
                    ):
                        raise ProviderInputGenerationReducerError(
                            "provider append manifest/preparation join failed"
                        )
                    if (
                        predecessor.core_state_fingerprint
                        != event.predecessor_core_state_fingerprint
                        or predecessor.revision != event.expected_revision
                    ):
                        raise ProviderInputGenerationReducerError(
                            "provider append predecessor CAS failed"
                        )
                    pending = (
                        rollover_pending_by_successor.get(generation_id)
                        if event.expected_revision == 0
                        and generation_id in rollover_pending_by_successor
                        else predecessor.accepted_but_not_appended_continuation
                    )
                    if (pending.continuation_fingerprint if pending else None) != (
                        event.consumed_pending_continuation_fingerprint
                    ):
                        raise ProviderInputGenerationReducerError(
                            "provider append continuation CAS failed"
                        )
                    preparation_id = event.consumed_preparation_id
                    preparation_key = self._resident_key("preparation", preparation_id)
                    resident = self._resident_manager.get(preparation_key)
                    expected_frontier = (
                        rollover_frontier_by_successor[generation_id]
                        if generation_id in rollover_frontier_by_successor
                        else predecessor.transcript_frontier
                    )
                    _validate_continuation_materialization(
                        pending=pending,
                        proof=event.continuation_materialization_proof,
                        predecessor_frontier=expected_frontier,
                        resulting_core=event.resulting_core_state,
                        append_event=event,
                        resident=resident,
                        require_staged_resident=require_staged_resident,
                    )
                    source_predecessor = rollover_source_core_by_successor.get(
                        generation_id, predecessor
                    )
                    try:
                        validate_runtime_observation_source_head_transition(
                            predecessor_heads=(
                                source_predecessor.committed_source_heads
                            ),
                            source_dispositions=event.source_dispositions,
                            appended_observations=event.runtime_observation_units,
                            resulting_heads=(
                                event.resulting_core_state.committed_source_heads
                            ),
                            allow_rewrite_drop=(
                                generation_id in rollover_source_core_by_successor
                            ),
                        )
                    except ValueError as exc:
                        raise ProviderInputGenerationReducerError(
                            "provider append source-head transition is invalid"
                        ) from exc
                    if event.frame_placement is not None:
                        prior_frames = frame_placements.get(generation_id, ())
                        if any(
                            item.frame_id == event.frame_placement.frame_id
                            and item != event.frame_placement
                            for item in prior_frames
                        ):
                            raise ProviderInputGenerationReducerError(
                                "provider context-frame identity conflict"
                            )
                        if event.frame_placement not in prior_frames:
                            frame_placements[generation_id] = (
                                *prior_frames,
                                event.frame_placement,
                            )
                    prior_observations = runtime_observation_units.get(
                        generation_id, ()
                    )
                    known_observation_ids = {
                        item.wire_semantic.observation_semantic_id
                        for item in prior_observations
                    }
                    if any(
                        item.wire_semantic.observation_semantic_id
                        in known_observation_ids
                        for item in event.runtime_observation_units
                    ):
                        raise ProviderInputGenerationReducerError(
                            "runtime observation semantic identity was committed twice"
                        )
                    runtime_observation_units[generation_id] = (
                        *prior_observations,
                        *event.runtime_observation_units,
                    )
                    runtime_observation_lifecycle_states[generation_id] = (
                        advance_runtime_observation_lifecycle_state(
                            runtime_observation_lifecycle_states.get(generation_id),
                            appended_observations=event.runtime_observation_units,
                            effective_heads=(
                                event.resulting_core_state.committed_source_heads
                            ),
                        )
                    )
                    append_events[generation_id] = (
                        *append_events.get(generation_id, ()),
                        event,
                    )
                    cores[generation_id] = event.resulting_core_state
                    prior_attribution = attributions.get(generation_id)
                    if resident is None:
                        source_head_status = (
                            "pending_hydration"
                            if event.resulting_core_state.committed_source_heads
                            else "complete"
                        )
                        source_head_attributions = ()
                    else:
                        source_head_status = "complete"
                        source_head_attributions = (
                            _source_head_attributions_from_resident(
                                core=event.resulting_core_state,
                                append_events=append_events[generation_id],
                                resident=resident,
                                runtime_session_id=self._runtime_session_id,
                            )
                        )
                    attributions[generation_id] = build_frozen_fact(
                        ProviderInputGenerationAttributionStateFact,
                        schema_version="provider_input_generation_attribution_state.v3",
                        core_state=event.resulting_core_state,
                        source_head_attribution_status=source_head_status,
                        source_head_attributions=source_head_attributions,
                        latest_model_start_event_ref=(
                            prior_attribution.latest_model_start_event_ref
                            if prior_attribution is not None
                            else None
                        ),
                        latest_model_start_committed_core_fingerprint=(
                            prior_attribution.latest_model_start_committed_core_fingerprint
                            if prior_attribution is not None
                            else None
                        ),
                        close_or_rollover_event_ref=(
                            prior_attribution.close_or_rollover_event_ref
                            if prior_attribution is not None
                            else None
                        ),
                    )
                    resident_actions.append(
                        (
                            "move",
                            preparation_key,
                            self._resident_key("generation", generation_id),
                            resident,
                        )
                    )
                    preparations.pop(preparation_id, None)
                    staged_ownerships.pop(
                        event.consumed_preparation_ownership_fingerprint, None
                    )
                    scope_fingerprint = (
                        predecessor.generation.scope.scope_fingerprint
                        if recovered_one_shot
                        else owner_fact.scope_fingerprint
                    )
                    binding = bindings.get(
                        scope_fingerprint
                    ) or self.empty_scope_binding(scope_fingerprint)
                    if recovered_one_shot:
                        if (
                            binding.active_generation_id is not None
                            or binding.active_preparation_id is not None
                        ):
                            raise ProviderInputGenerationReducerError(
                                "recovered one-shot append scope is already occupied"
                            )
                    elif owner_attribution is None:
                        assert owner_fact is not None
                        if (
                            binding.binding_fingerprint
                            != owner_fact.expected_predecessor_scope_binding_fingerprint
                        ):
                            raise ProviderInputGenerationReducerError(
                                "ephemeral provider append scope CAS failed"
                            )
                    elif binding.active_preparation_id != preparation_id:
                        raise ProviderInputGenerationReducerError(
                            "durable provider append lost active preparation"
                        )
                    bindings[binding.scope_fingerprint] = build_frozen_fact(
                        ProviderInputGenerationScopeBindingFact,
                        schema_version="provider_input_generation_scope_binding.v1",
                        scope_fingerprint=binding.scope_fingerprint,
                        active_generation_id=generation_id,
                        latest_closed_generation_id=binding.latest_closed_generation_id,
                        active_preparation_id=None,
                    )
                    call_to_generation[event.resolved_model_call_id] = generation_id
                    append_identities_by_call[event.resolved_model_call_id] = (
                        _stable_event_identity(
                            event,
                            runtime_session_id=self._runtime_session_id,
                        )
                    )
                    continue

                if isinstance(event, ModelCallStartEvent):
                    reference = event.provider_input_reference
                    if reference is None:
                        continue
                    generation_id = reference.generation_id
                    core = cores.get(generation_id)
                    if (
                        core is None
                        or core.core_state_fingerprint
                        != reference.resulting_generation_core_state_fingerprint
                        or core.revision != reference.committed_generation_revision
                    ):
                        raise ProviderInputGenerationReducerError(
                            "ModelStart provider input core join failed"
                        )
                    append = next(
                        (
                            candidate
                            for candidate in events[:index]
                            if isinstance(candidate, ProviderInputAppendCommittedEvent)
                            and candidate.id
                            == reference.append_committed_event_identity.event_id
                        ),
                        None,
                    )
                    append_identity = (
                        _stable_event_identity(
                            append,
                            runtime_session_id=self._runtime_session_id,
                        )
                        if append is not None
                        else append_identities_by_call.get(
                            event.resolved_call.resolved_model_call_id
                        )
                    )
                    if append_identity is None:
                        raise ProviderInputGenerationReducerError(
                            "ModelStart provider append identity is unavailable"
                        )
                    if append_identity != reference.append_committed_event_identity:
                        raise ProviderInputGenerationReducerError(
                            "ModelStart provider append identity drifted"
                        )
                    if append is not None:
                        compiled = append.append_kind == "compiled_manifest"
                        if compiled != (reference.reference_kind == "compiled_manifest"):
                            raise ProviderInputGenerationReducerError(
                                "ModelStart provider reference kind drifted"
                            )
                        if compiled and (
                            append.manifest_projection_reference is None
                            or append.causal_validation is None
                            or reference.manifest_projection_reference_fingerprint
                            != append.manifest_projection_reference.reference_fingerprint
                            or reference.causal_validation_fingerprint
                            != append.causal_validation.result_fingerprint
                            or reference.transcript_frontier_fingerprint
                            != append.resulting_core_state.transcript_frontier.provider_semantic_frontier_fingerprint
                        ):
                            raise ProviderInputGenerationReducerError(
                                "ModelStart provider manifest proof drifted"
                            )
                    if append is not None and (
                        append.resolved_model_call_id
                        != event.resolved_call.resolved_model_call_id
                        or append.expected_model_start_event_id != event.id
                    ):
                        raise ProviderInputGenerationReducerError(
                            "ModelStart provider append control join drifted"
                        )
                    prior = attributions.get(generation_id)
                    attributions[generation_id] = build_frozen_fact(
                        ProviderInputGenerationAttributionStateFact,
                        schema_version="provider_input_generation_attribution_state.v3",
                        core_state=core,
                        source_head_attribution_status=(
                            prior.source_head_attribution_status
                            if prior is not None
                            else "complete"
                        ),
                        source_head_attributions=(
                            prior.source_head_attributions if prior is not None else ()
                        ),
                        latest_model_start_event_ref=_event_ref(
                            event, runtime_session_id=self._runtime_session_id
                        ),
                        latest_model_start_committed_core_fingerprint=(
                            core.core_state_fingerprint
                        ),
                        close_or_rollover_event_ref=(
                            prior.close_or_rollover_event_ref if prior else None
                        ),
                    )
                    continue

                if isinstance(event, ModelCallEndEvent):
                    generation_id = call_to_generation.get(event.resolved_model_call_id)
                    if generation_id is None or event.outcome != "completed":
                        continue
                    start = next(
                        (
                            candidate
                            for candidate in reversed(events[: index + 1])
                            if isinstance(candidate, ModelCallStartEvent)
                            and candidate.resolved_call.resolved_model_call_id
                            == event.resolved_model_call_id
                        ),
                        None,
                    )
                    if start is not None and start.model_call_index is None:
                        continue
                    if index == 0 or not isinstance(
                        events[index - 1], ModelCallTerminalProjectionCommittedEvent
                    ):
                        raise ProviderInputGenerationReducerError(
                            "completed model terminal lacks adjacent projection"
                        )
                    projection = events[index - 1]
                    assert isinstance(
                        projection, ModelCallTerminalProjectionCommittedEvent
                    )
                    core = cores[generation_id]
                    if core.generation.scope.scope_kind == "one_shot":
                        continue
                    if core.awaiting_control_disposition is not None:
                        raise ProviderInputGenerationReducerError(
                            "provider generation already awaits disposition"
                        )
                    awaiting = build_frozen_fact(
                        ProviderInputAwaitingControlDispositionFact,
                        schema_version=(
                            "provider_input_awaiting_control_disposition.v1"
                        ),
                        resolved_model_call_id=event.resolved_model_call_id,
                        terminal_projection_reference=projection.projection_reference,
                        model_terminal_event_ref=_event_ref(
                            event, runtime_session_id=self._runtime_session_id
                        ),
                        terminal_projection_committed_event_ref=_event_ref(
                            projection, runtime_session_id=self._runtime_session_id
                        ),
                        authority_horizon_set=(core.committed_authority_horizon_set),
                    )
                    cores[generation_id] = _copy_core(
                        core,
                        awaiting_control_disposition=awaiting,
                    )
                    continue

                if isinstance(event, ModelCallControlDispositionResolvedEvent):
                    generation_id = call_to_generation.get(event.resolved_model_call_id)
                    if generation_id is None:
                        continue
                    core = cores[generation_id]
                    # One-shot generations close atomically with ModelEnd and do
                    # not participate in the session-window continuation chain.
                    if isinstance(core.generation.scope, OneShotGenerationScopeFact):
                        continue
                    awaiting = core.awaiting_control_disposition
                    if (
                        awaiting is None
                        or awaiting.resolved_model_call_id
                        != event.resolved_model_call_id
                    ):
                        raise ProviderInputGenerationReducerError(
                            "control disposition lacks awaiting provider projection"
                        )
                    pending = None
                    if event.disposition is ModelCallControlDisposition.ACCEPTED:
                        pending = build_frozen_fact(
                            ProviderInputPendingContinuationFact,
                            schema_version="provider_input_pending_continuation.v1",
                            resolved_model_call_id=event.resolved_model_call_id,
                            terminal_projection_reference=(
                                awaiting.terminal_projection_reference
                            ),
                            accepted_disposition_event_ref=_event_ref(
                                event, runtime_session_id=self._runtime_session_id
                            ),
                            continuation_semantic_fingerprint=(
                                awaiting.terminal_projection_reference.semantic_join.semantic_fingerprint
                            ),
                            authority_horizon_set=(awaiting.authority_horizon_set),
                        )
                    cores[generation_id] = _copy_core(
                        core,
                        awaiting_control_disposition=None,
                        accepted_but_not_appended_continuation=pending,
                    )
                    continue

                if isinstance(event, ProviderInputGenerationClosedEvent):
                    predecessor = cores.get(event.generation_id)
                    if (
                        predecessor is None
                        or predecessor.core_state_fingerprint
                        != event.predecessor_core_state_fingerprint
                    ):
                        raise ProviderInputGenerationReducerError(
                            "provider generation close predecessor CAS failed"
                        )
                    pending_fingerprint = (
                        predecessor.accepted_but_not_appended_continuation.continuation_fingerprint
                        if predecessor.accepted_but_not_appended_continuation
                        is not None
                        else None
                    )
                    if event.unconsumed_continuation_fingerprint != pending_fingerprint:
                        raise ProviderInputGenerationReducerError(
                            "provider generation close continuation attribution drifted"
                        )
                    if (
                        event.close_reason == "rollover"
                        and event.successor_generation_id is not None
                    ):
                        rollover_source_core_by_successor[
                            event.successor_generation_id
                        ] = predecessor
                        rollover_pending_by_successor[event.successor_generation_id] = (
                            predecessor.accepted_but_not_appended_continuation
                        )
                        rollover_frontier_by_successor[
                            event.successor_generation_id
                        ] = predecessor.transcript_frontier
                    cores[event.generation_id] = event.resulting_closed_core_state
                    prior = attributions.get(event.generation_id)
                    if prior is not None:
                        attributions[event.generation_id] = build_frozen_fact(
                            ProviderInputGenerationAttributionStateFact,
                            schema_version=(
                                "provider_input_generation_attribution_state.v3"
                            ),
                            core_state=event.resulting_closed_core_state,
                            source_head_attribution_status=(
                                prior.source_head_attribution_status
                            ),
                            source_head_attributions=prior.source_head_attributions,
                            latest_model_start_event_ref=(
                                prior.latest_model_start_event_ref
                            ),
                            latest_model_start_committed_core_fingerprint=(
                                prior.latest_model_start_committed_core_fingerprint
                            ),
                            close_or_rollover_event_ref=_event_ref(
                                event, runtime_session_id=self._runtime_session_id
                            ),
                        )
                    scope_fingerprint = event.resulting_closed_core_state.generation.scope.scope_fingerprint
                    binding = bindings.get(
                        scope_fingerprint
                    ) or self.empty_scope_binding(scope_fingerprint)
                    if binding.active_generation_id not in {
                        None,
                        event.generation_id,
                    }:
                        raise ProviderInputGenerationReducerError(
                            "provider generation close conflicts with scope owner"
                        )
                    bindings[scope_fingerprint] = build_frozen_fact(
                        ProviderInputGenerationScopeBindingFact,
                        schema_version="provider_input_generation_scope_binding.v1",
                        scope_fingerprint=scope_fingerprint,
                        active_generation_id=None,
                        latest_closed_generation_id=event.generation_id,
                        active_preparation_id=(
                            binding.active_preparation_id
                            if event.close_reason == "rollover"
                            else None
                        ),
                    )
                    if event.close_reason in {"one_shot_terminal", "rollover"}:
                        resident_actions.append(
                            (
                                "discard",
                                self._resident_key("generation", event.generation_id),
                                None,
                                None,
                            )
                        )
                    continue

                if isinstance(event, ProviderInputGenerationRolloverResolvedEvent):
                    old = cores.get(event.old_generation_id)
                    request = event.rollover_request
                    initial_append = next(
                        (
                            candidate
                            for candidate in events
                            if isinstance(candidate, ProviderInputAppendCommittedEvent)
                            and candidate.id == event.expected_initial_append_event_id
                        ),
                        None,
                    )
                    if (
                        old is None
                        or old.status != "closed"
                        or old.core_state_fingerprint
                        != event.old_final_core_state_fingerprint
                        or event.new_generation.predecessor_generation_id
                        != event.old_generation_id
                        or event.new_generation.predecessor_generation_fingerprint
                        != event.old_generation_fingerprint
                        or event.new_root_reference.generation != event.new_generation
                        or event.new_root_reference.authority_horizon_set
                        != event.authority_horizon_set
                        or request.intent.predecessor_generation_id
                        != event.old_generation_id
                        or request.intent.reason != event.new_generation.rollover_reason
                        or initial_append is None
                        or initial_append.manifest_projection_reference
                        != request.manifest_projection_reference
                    ):
                        raise ProviderInputGenerationReducerError(
                            "provider rollover semantic join failed"
                        )
                    required_ids = {
                        event.expected_old_close_event_id,
                        event.expected_new_start_event_id,
                        event.expected_initial_append_event_id,
                        event.expected_model_start_event_id,
                    }
                    available_ids = {candidate.id for candidate in events}
                    if not required_ids.issubset(available_ids):
                        raise ProviderInputGenerationReducerError(
                            "provider rollover atomic event set is incomplete"
                        )
                    source_rewrite_authority = request.intent.authority
                    if isinstance(
                        source_rewrite_authority,
                        ProviderSourceDispositionRewriteAuthorityFact,
                    ):
                        source_core = rollover_source_core_by_successor.get(
                            event.new_generation.generation_id
                        )
                        if source_core is None:
                            raise ProviderInputGenerationReducerError(
                                "source-disposition rollover lacks predecessor core"
                            )
                        head_by_key = {
                            (
                                item.effective_snapshot.source_id,
                                item.effective_snapshot.source_instance_id,
                            ): item
                            for item in source_core.committed_source_heads
                        }
                        expected_head_fingerprints = tuple(
                            head_by_key[
                                (item.source_id, item.source_instance_id)
                            ].semantic_head_fingerprint
                            for item in source_rewrite_authority.rewrite_dispositions
                            if (item.source_id, item.source_instance_id) in head_by_key
                        )
                        if (
                            source_rewrite_authority.predecessor_core_state_fingerprint
                            != source_core.core_state_fingerprint
                            or source_rewrite_authority.ordered_projection_identity_fingerprint
                            != request.manifest_projection_reference.projection_identity.identity_fingerprint
                            or source_rewrite_authority.rewrite_dispositions
                            != tuple(
                                item
                                for item in initial_append.source_dispositions
                                if item.disposition == "rewrite_required"
                            )
                            or len(expected_head_fingerprints)
                            != len(source_rewrite_authority.rewrite_dispositions)
                            or expected_head_fingerprints
                            != source_rewrite_authority.rewritten_predecessor_source_head_fingerprints
                        ):
                            raise ProviderInputGenerationReducerError(
                                "source-disposition rollover authority drifted"
                            )
                    rewrite = event.runtime_observation_rewrite
                    if rewrite is not None:
                        close_event = next(
                            (
                                candidate
                                for candidate in events
                                if isinstance(
                                    candidate, ProviderInputGenerationClosedEvent
                                )
                                and candidate.id
                                == event.expected_old_close_event_id
                            ),
                            None,
                        )
                        lifecycle_state = runtime_observation_lifecycle_states.get(
                            event.old_generation_id
                        )
                        if close_event is None or lifecycle_state is None:
                            raise ProviderInputGenerationReducerError(
                                "runtime observation rewrite lacks source reducer state"
                            )
                        source_core = rollover_source_core_by_successor.get(
                            event.new_generation.generation_id
                        )
                        if (
                            source_core is None
                            or source_core.core_state_fingerprint
                            != close_event.predecessor_core_state_fingerprint
                        ):
                            raise ProviderInputGenerationReducerError(
                                "runtime observation rewrite source core drifted"
                            )
                        try:
                            validate_runtime_observation_rewrite_transition(
                                source_core=source_core,
                                source_observations=runtime_observation_units.get(
                                    event.old_generation_id, ()
                                ),
                                source_lifecycle_state=lifecycle_state,
                                resulting_core=initial_append.resulting_core_state,
                                resulting_observations=(
                                    initial_append.runtime_observation_units
                                ),
                                rewrite=rewrite,
                                current_run_protection_scope_semantic_id=(
                                    context_fingerprint(
                                        "runtime-observation-run-protection-scope:v1",
                                        (self._runtime_session_id, event.run_id),
                                    )
                                ),
                                artifact_namespace=provider_input_artifact_namespace(
                                    self._runtime_session_id
                                ),
                            )
                        except ValueError as exc:
                            raise ProviderInputGenerationReducerError(
                                "runtime observation rewrite transition is invalid"
                            ) from exc
                        prior_rewrites = runtime_observation_rewrites.get(
                            event.new_generation.generation_id, ()
                        )
                        if any(
                            item.rewrite_id == rewrite.rewrite_id and item != rewrite
                            for item in prior_rewrites
                        ):
                            raise ProviderInputGenerationReducerError(
                                "runtime observation rewrite identity conflict"
                            )
                        if rewrite not in prior_rewrites:
                            runtime_observation_rewrites[
                                event.new_generation.generation_id
                            ] = (*prior_rewrites, rewrite)
                    prior = attributions.get(event.old_generation_id)
                    if prior is not None:
                        attributions[event.old_generation_id] = build_frozen_fact(
                            ProviderInputGenerationAttributionStateFact,
                            schema_version=(
                                "provider_input_generation_attribution_state.v3"
                            ),
                            core_state=old,
                            source_head_attribution_status=(
                                prior.source_head_attribution_status
                            ),
                            source_head_attributions=prior.source_head_attributions,
                            latest_model_start_event_ref=(
                                prior.latest_model_start_event_ref
                            ),
                            latest_model_start_committed_core_fingerprint=(
                                prior.latest_model_start_committed_core_fingerprint
                            ),
                            close_or_rollover_event_ref=_event_ref(
                                event,
                                runtime_session_id=self._runtime_session_id,
                            ),
                        )
                    continue

                if isinstance(
                    event,
                    (
                        ExistingGenerationPreparationAbandonedEvent,
                        ScopedGenerationPreparationAbandonedEvent,
                    ),
                ):
                    preparation = preparations.pop(event.preparation_id, None)
                    if preparation is None or (
                        preparation.ownership.ownership_fingerprint
                        != event.preparation_ownership_fingerprint
                    ):
                        raise ProviderInputGenerationReducerError(
                            "provider preparation abandonment lost its owner"
                        )
                    owner = preparation.ownership
                    if (
                        event.preparation_ownership_fingerprint
                        != owner.ownership_fingerprint
                        or event.context_compiled_event_ref
                        != preparation.context_compiled_event_ref
                        or event.resolved_model_call_id != owner.resolved_model_call_id
                        or event.predecessor_preparation_attribution_fingerprint
                        != preparation.attribution_fingerprint
                    ):
                        raise ProviderInputGenerationReducerError(
                            "provider preparation abandonment attribution drifted"
                        )
                    resident_actions.append(
                        (
                            "discard",
                            self._resident_key("preparation", event.preparation_id),
                            None,
                            None,
                        )
                    )
                    staged_ownerships.pop(owner.ownership_fingerprint, None)
                    scope_fingerprint = owner.scope_fingerprint
                    binding = bindings[scope_fingerprint]
                    if (
                        binding.active_preparation_id != event.preparation_id
                        or binding.binding_fingerprint
                        != event.predecessor_scope_binding_fingerprint
                    ):
                        raise ProviderInputGenerationReducerError(
                            "provider preparation abandonment scope CAS failed"
                        )
                    if isinstance(event, ExistingGenerationPreparationAbandonedEvent):
                        core = cores.get(event.generation_id)
                        if (
                            owner.ownership_kind != "existing_append"
                            or event.generation_id != owner.generation_id
                            or event.expected_committed_core_state_fingerprint
                            != owner.expected_committed_core_state_fingerprint
                            or core is None
                            or core.core_state_fingerprint
                            != event.expected_committed_core_state_fingerprint
                        ):
                            raise ProviderInputGenerationReducerError(
                                "existing provider abandonment core join failed"
                            )
                    else:
                        if (
                            event.scope_fingerprint != owner.scope_fingerprint
                            or event.proposed_generation_id != owner.generation_id
                            or event.abandonment_kind != owner.ownership_kind
                        ):
                            raise ProviderInputGenerationReducerError(
                                "scoped provider abandonment identity drifted"
                            )
                        if owner.ownership_kind == "initial_start":
                            if (
                                event.old_generation_id is not None
                                or event.expected_old_core_state_fingerprint is not None
                            ):
                                raise ProviderInputGenerationReducerError(
                                    "initial provider abandonment has old generation"
                                )
                        elif (
                            event.old_generation_id is None
                            or event.expected_old_core_state_fingerprint
                            != owner.expected_committed_core_state_fingerprint
                        ):
                            raise ProviderInputGenerationReducerError(
                                "rollover provider abandonment old core drifted"
                            )
                    resulting = build_frozen_fact(
                        ProviderInputGenerationScopeBindingFact,
                        schema_version="provider_input_generation_scope_binding.v1",
                        scope_fingerprint=scope_fingerprint,
                        active_generation_id=binding.active_generation_id,
                        latest_closed_generation_id=binding.latest_closed_generation_id,
                        active_preparation_id=None,
                    )
                    if (
                        resulting.binding_fingerprint
                        != event.resulting_scope_binding_fingerprint
                    ):
                        raise ProviderInputGenerationReducerError(
                            "provider abandonment scope result drifted"
                        )
                    bindings[scope_fingerprint] = resulting
                    continue

                if isinstance(event, RunEndEvent):
                    unresolved = tuple(
                        state.generation.generation_id
                        for state in cores.values()
                        if state.generation.scope.scope_kind == "session_continuity"
                        and getattr(state.generation.scope, "runtime_session_id", None)
                        == self._runtime_session_id
                        and state.awaiting_control_disposition is not None
                    )
                    if unresolved:
                        raise ProviderInputGenerationReducerError(
                            "RunEnd cannot strand completed provider input: "
                            + ",".join(unresolved)
                        )

            self._cores = cores
            self._attributions = attributions
            self._bindings = bindings
            self._preparations = preparations
            self._call_to_generation = call_to_generation
            self._append_identities_by_call = append_identities_by_call
            self._frame_placements = frame_placements
            self._runtime_observation_units = runtime_observation_units
            self._runtime_observation_rewrites = runtime_observation_rewrites
            self._runtime_observation_lifecycle_states = (
                runtime_observation_lifecycle_states
            )
            self._append_events = append_events
            self._staged_ownerships = staged_ownerships
            self._through_sequence = events[-1].sequence or self._through_sequence

            # Resident state is disposable memoization. Apply cache actions only
            # after the semantic reducer has accepted the complete durable batch.
            for action, source_key, destination_key, resident in resident_actions:
                self._resident_manager.discard(source_key)
                if (
                    action == "move"
                    and destination_key is not None
                    and resident is not None
                ):
                    self._resident_manager.admit(destination_key, resident)

    def rebuild(self, events: tuple[AgentEvent, ...]) -> None:
        with self._lock:
            staged_ownerships = dict(self._staged_ownerships)
            self._through_sequence = 0
            self._cores = {}
            self._attributions = {}
            self._bindings = {}
            self._preparations = {}
            self._call_to_generation = {}
            self._append_identities_by_call = {}
            self._frame_placements = {}
            self._runtime_observation_units = {}
            self._runtime_observation_rewrites = {}
            self._runtime_observation_lifecycle_states = {}
            self._append_events = {}
            self._staged_ownerships = staged_ownerships
            self._resident_manager.discard_runtime_session(self._runtime_session_id)
        if events:
            self._apply_committed(events, require_staged_resident=False)


def _source_head_attributions_from_resident(
    *,
    core: CommittedProviderInputGenerationCoreStateFact,
    append_events: tuple[ProviderInputAppendCommittedEvent, ...],
    resident: ProviderInputResidentGeneration,
    runtime_session_id: str,
) -> tuple[ProviderInputUnitPlacementAttributionFact, ...]:
    result = []
    for head in core.committed_source_heads:
        snapshot = head.effective_snapshot
        matched_event = None
        matched_observation = None
        for event in reversed(append_events):
            matched_observation = next(
                (
                    item
                    for item in event.runtime_observation_units
                    if item.source_id == snapshot.source_id
                    and item.wire_semantic.source_instance_id
                    == snapshot.source_instance_id
                    and item.wire_semantic.observation_semantic_id
                    == snapshot.observation_semantic_id
                    and item.source_payload_semantic_fingerprint
                    == snapshot.snapshot_semantic_fingerprint
                    and item.wire_semantic.wire_semantic_fingerprint
                    == snapshot.canonical_wire_semantic_fingerprint
                    and item.causal_placement.placement_semantic_fingerprint
                    == snapshot.causal_placement_semantic_fingerprint
                    and item.unit_causal_semantic_fingerprint
                    == snapshot.unit_causal_semantic_fingerprint
                ),
                None,
            )
            if matched_observation is not None:
                matched_event = event
                break
        if matched_event is None or matched_observation is None:
            raise ProviderInputGenerationReducerError(
                "provider source head lacks its committed observation"
            )
        unit_matches = tuple(
            (ordinal, unit)
            for ordinal, unit in enumerate(resident.units)
            if unit.attribution.semantic.semantic_fingerprint
            == matched_observation.provider_unit_semantic_fingerprint
        )
        if len(unit_matches) != 1:
            raise ProviderInputGenerationReducerError(
                "provider source head does not uniquely locate its vector unit"
            )
        ordinal, unit = unit_matches[0]
        semantic_materialization, document_identity = (
            build_provider_unit_semantic_document(unit)
        )
        if document_identity != snapshot.unit_document_identity:
            raise ProviderInputGenerationReducerError(
                "provider source-head semantic document drifted"
            )
        closure_ref = (
            unit.attribution.source_event_refs[-1]
            if snapshot.effective_status == "source_closed"
            and unit.attribution.source_event_refs
            else None
        )
        if (snapshot.effective_status == "source_closed") != (
            closure_ref is not None
        ):
            raise ProviderInputGenerationReducerError(
                "provider source-head closure attribution is incomplete"
            )
        hydration = build_frozen_fact(
            InlineProviderInputUnitHydrationAttributionFact,
            schema_version=(
                "inline_provider_input_unit_hydration_attribution.v1"
            ),
            semantic_document_identity_fingerprint=(
                document_identity.document_semantic_fingerprint
            ),
            semantic_materialization=semantic_materialization,
        )
        placement = build_frozen_fact(
            ProviderInputUnitPlacementAttributionFact,
            schema_version="provider_input_unit_placement_attribution.v1",
            semantic_head_fingerprint=head.semantic_head_fingerprint,
            hydration_attribution=hydration,
            origin_generation_id=core.generation.generation_id,
            committed_append_event_reference=_event_ref(
                matched_event,
                runtime_session_id=runtime_session_id,
            ),
            committed_append_index=matched_event.resulting_revision,
            committed_vector_root_reference=(
                matched_event.resulting_core_state.unit_vector_root
            ),
            vector_ordinal=ordinal,
            source_event_references=unit.attribution.source_event_refs,
            source_artifact_references=unit.attribution.source_artifact_refs,
            authority_horizons=unit.attribution.authority_horizons,
            required_replay_bindings=unit.attribution.required_replay_bindings,
            closure_event_reference=closure_ref,
        )
        build_frozen_fact(
            CommittedRuntimeObservationSourceHeadFact,
            schema_version="committed_runtime_observation_source_head.v1",
            semantic_head=head,
            placement_attribution=placement,
        )
        result.append(placement)
    return tuple(sorted(result, key=lambda item: item.semantic_head_fingerprint))


def _copy_core(
    core: CommittedProviderInputGenerationCoreStateFact,
    **updates,
) -> CommittedProviderInputGenerationCoreStateFact:
    payload = {
        name: getattr(core, name)
        for name in core.__class__.model_fields
        if name not in {"core_state_fingerprint", "schema_version"}
    }
    payload.update(updates)
    return build_frozen_fact(
        CommittedProviderInputGenerationCoreStateFact,
        schema_version="committed_provider_input_generation_core_state.v3",
        **payload,
    )


def _event_ref(
    event: AgentEvent,
    *,
    runtime_session_id: str,
) -> ContextEventReferenceFact:
    if event.sequence is None:
        raise ProviderInputGenerationReducerError(
            "provider generation event reference requires committed sequence"
        )
    candidate = freeze_event_write_candidate(
        event.model_copy(update={"sequence": None})
    )
    return ContextEventReferenceFact(
        runtime_session_id=runtime_session_id,
        event_id=event.id,
        sequence=event.sequence,
        event_type=str(event.type),
        payload_fingerprint=candidate.payload_fingerprint,
    )


def _stable_event_identity(
    event: AgentEvent,
    *,
    runtime_session_id: str,
) -> StableEventIdentityFact:
    candidate = freeze_event_write_candidate(
        event.model_copy(update={"sequence": None})
    )
    return build_frozen_fact(
        StableEventIdentityFact,
        schema_version="stable_event_identity.v2",
        runtime_session_id=runtime_session_id,
        event_id=candidate.event_id,
        event_type=candidate.event_type,
        event_schema_version=candidate.event_schema_version,
        event_schema_fingerprint=candidate.event_schema_fingerprint,
        payload_fingerprint=candidate.payload_fingerprint,
    )


__all__ = [
    "ProviderInputGenerationReducerError",
    "ProviderInputGenerationSnapshot",
    "ProviderInputGenerationStore",
    "ProviderInputPreparationRecoverySnapshot",
    "ProviderInputResidentGeneration",
]
