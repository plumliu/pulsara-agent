"""Bounded Long-Horizon rewrite for committed runtime observations.

This module is deliberately pure.  It classifies only committed provider-input
observation facts, prepares content-addressed proof artifacts, and returns a
stable rollover projection.  The provider-input coordinator remains the sole
artifact writer and LLMRuntime remains the sole ModelStart writer.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

from pulsara_agent.llm.input import LLMMessage
from pulsara_agent.llm.user_carrier import (
    decode_runtime_observation_wire_semantic,
    runtime_observation_rewrite_projection_payload,
)
from pulsara_agent.primitives._context_base import ContextEventReferenceFact
from pulsara_agent.primitives.context import canonical_json_bytes, context_fingerprint
from pulsara_agent.primitives.context_source import (
    ContextSourceDispositionFact,
    ContextSourceId,
    LedgerAuthorityHorizonFact,
)
from pulsara_agent.primitives.frozen import build_frozen_fact
from pulsara_agent.primitives.provider_input import (
    CommittedProviderInputGenerationCoreStateFact,
    CompactionReplacementSummarySourceAttributionFact,
    ProviderInputUnitMaterializationFact,
    ProviderInputReplayBindingIdentityFact,
)
from pulsara_agent.primitives.runtime_observation import (
    ContextSourceReplacementObservationPayloadFact,
    PreparedRuntimeObservationProviderUnitFact,
    PreparedRuntimeObservationRewriteProjectionReferenceFact,
    PreparedRuntimeObservationRewriteProjectionUnitFact,
    RuntimeObservationCausalPlacementSemanticFact,
    RuntimeObservationEffectiveHeadSetReferenceFact,
    RuntimeObservationProjectionPartitionProofFact,
    RuntimeObservationProjectionPhysicalPolicyFact,
    RuntimeObservationProjectionRewriteFact,
    RuntimeObservationProjectionSetNodeReferenceFact,
    RuntimeObservationProjectionSetReferenceFact,
    RuntimeObservationProjectionStableStateFact,
    RuntimeObservationRewriteCoverageSemanticFact,
    RuntimeObservationRewriteProjectionPayloadFact,
    RuntimeObservationRewriteUnitAttributionFact,
    RuntimeObservationRewriteUnitSemanticFact,
    RuntimeObservationSourceAttributionFact,
)
from pulsara_agent.runtime.context_input.sources.lifecycle import (
    context_source_lifecycle_entry,
    runtime_observation_kind_contract,
    runtime_observation_derived_producer,
)
from pulsara_agent.runtime.provider_input.materialization import freeze_message_unit
from pulsara_agent.runtime.provider_input.vector import (
    PreparedProviderInputArtifact,
    prepared_json_artifact,
)


OBSERVATION_SET_CONTRACT_FINGERPRINT = context_fingerprint(
    "runtime-observation-projection-set-contract:v1",
    {
        "ordering": "committed-causal-order",
        "leaf": "bounded-canonical-json",
        "internal": "bounded-fanout-merkle",
    },
)
OBSERVATION_PARTITION_CONTRACT_FINGERPRINT = context_fingerprint(
    "runtime-observation-projection-partition-contract:v1",
    "protected+retained+rewritten=active;rewritten<=eligible",
)
OBSERVATION_CLASSIFICATION_CONTRACT_FINGERPRINT = context_fingerprint(
    "runtime-observation-projection-classification-contract:v1",
    {
        "protected": (
            "effective-replacement-head",
            "latest-clock",
            "open-or-unproven-causal-lifecycle",
            "non-rewriteable-kind",
        ),
        "eligible": (
            "superseded-replacement",
            "historical-clock",
            "prior-rewrite-projection",
        ),
    },
)
OBSERVATION_COVERAGE_CONTRACT_FINGERPRINT = context_fingerprint(
    "runtime-observation-rewrite-coverage-contract:v1",
    "ordered-segment-merkle-with-transitive-count",
)
OBSERVATION_REWRITE_PROJECTION_CONTRACT_FINGERPRINT = context_fingerprint(
    "runtime-observation-rewrite-projection-contract:v1",
    "causal-boundary-preserving-runtime-observation-units",
)
OBSERVATION_UNIFIED_ORDER_CONTRACT_FINGERPRINT = context_fingerprint(
    "runtime-observation-unified-ordered-projection-contract:v1",
    "root+transcript+compaction+observation-rewrite+retained+tail",
)
OBSERVATION_REWRITE_POLICY_FINGERPRINT = context_fingerprint(
    "runtime-observation-rewrite-policy:v1",
    "rewrite-all-proven-eligible;protect-effective-and-open",
)


def default_runtime_observation_physical_policy() -> (
    RuntimeObservationProjectionPhysicalPolicyFact
):
    return build_frozen_fact(
        RuntimeObservationProjectionPhysicalPolicyFact,
        schema_version="runtime_observation_projection_physical_policy.v1",
        leaf_max_entries=64,
        leaf_max_canonical_bytes=262_144,
        internal_max_fanout=64,
        maximum_tree_height=8,
        maximum_event_root_bytes=65_536,
        maximum_changed_nodes_per_rewrite=16_384,
        maximum_artifact_batches_per_rewrite=16_384,
        operation_deadline_seconds=30,
    )


@dataclass(frozen=True, slots=True)
class PreparedRuntimeObservationRewritePlan:
    rewrite_id: str
    parent_event_reference: ContextEventReferenceFact
    source_stable_state: RuntimeObservationProjectionStableStateFact
    partition_proof: RuntimeObservationProjectionPartitionProofFact
    prepared_projection: PreparedRuntimeObservationRewriteProjectionReferenceFact
    source_effective_heads: RuntimeObservationEffectiveHeadSetReferenceFact
    projected_units: tuple[ProviderInputUnitMaterializationFact, ...]
    projected_observations: tuple[PreparedRuntimeObservationProviderUnitFact, ...]
    rewrite_units: tuple[PreparedRuntimeObservationRewriteProjectionUnitFact, ...]
    artifacts: tuple[PreparedProviderInputArtifact, ...]
    physical_policy: RuntimeObservationProjectionPhysicalPolicyFact

    def finalize(
        self,
        *,
        resulting_ordered_provider_projection_fingerprint: str,
        resulting_effective_heads: (
            RuntimeObservationEffectiveHeadSetReferenceFact | None
        ) = None,
    ) -> RuntimeObservationProjectionRewriteFact:
        fact = build_frozen_fact(
            RuntimeObservationProjectionRewriteFact,
            schema_version="runtime_observation_projection_rewrite.v3",
            rewrite_id=self.rewrite_id,
            parent_long_horizon_rewrite_event_reference=self.parent_event_reference,
            source_stable_state=self.source_stable_state,
            partition_proof=self.partition_proof,
            prepared_replacement_projection=self.prepared_projection,
            resulting_effective_heads=(
                resulting_effective_heads or self.source_effective_heads
            ),
            coverage_lineage_contract_fingerprint=(
                OBSERVATION_COVERAGE_CONTRACT_FINGERPRINT
            ),
            unified_ordered_projection_contract_fingerprint=(
                OBSERVATION_UNIFIED_ORDER_CONTRACT_FINGERPRINT
            ),
            resulting_ordered_provider_projection_fingerprint=(
                resulting_ordered_provider_projection_fingerprint
            ),
            physical_policy_fingerprint=self.physical_policy.policy_fingerprint,
            rewrite_policy_id="pulsara.runtime-observation.long-horizon",
            rewrite_policy_version="1",
            rewrite_policy_fingerprint=OBSERVATION_REWRITE_POLICY_FINGERPRINT,
        )
        if len(canonical_json_bytes(fact.model_dump(mode="json"))) > (
            self.physical_policy.maximum_event_root_bytes
        ):
            raise ValueError("runtime observation rewrite exceeds event-root bound")
        return fact


@dataclass(frozen=True, slots=True)
class _PreparedSet:
    reference: RuntimeObservationProjectionSetReferenceFact
    artifacts: tuple[PreparedProviderInputArtifact, ...]


@dataclass(frozen=True, slots=True)
class _PreparedHeadSet:
    reference: RuntimeObservationEffectiveHeadSetReferenceFact
    artifacts: tuple[PreparedProviderInputArtifact, ...]


@dataclass(frozen=True, slots=True)
class _CoverageSegment:
    count: int
    semantic_accumulator: str
    causal_accumulator: str
    coverage_root: str


@dataclass(frozen=True, slots=True)
class RuntimeObservationLifecycleReducerState:
    """Process-local incremental classification basis rebuilt from durable appends."""

    ordered_observation_semantic_ids: tuple[str, ...]
    effective_head_observation_semantic_ids: frozenset[str]
    latest_clock_observation_semantic_id: str | None
    closed_protection_scope_semantic_ids: frozenset[str]
    pending_dependency_observation_semantic_ids: frozenset[str]
    state_fingerprint: str


@dataclass(frozen=True, slots=True)
class RuntimeObservationLifecycleSnapshot:
    active: tuple[PreparedRuntimeObservationProviderUnitFact, ...]
    protected: tuple[PreparedRuntimeObservationProviderUnitFact, ...]
    eligible: tuple[PreparedRuntimeObservationProviderUnitFact, ...]
    open_lifecycle: tuple[PreparedRuntimeObservationProviderUnitFact, ...]
    pending_dependency: tuple[PreparedRuntimeObservationProviderUnitFact, ...]
    snapshot_fingerprint: str


def advance_runtime_observation_lifecycle_state(
    previous: RuntimeObservationLifecycleReducerState | None,
    *,
    appended_observations: tuple[PreparedRuntimeObservationProviderUnitFact, ...],
    effective_heads,
) -> RuntimeObservationLifecycleReducerState:
    previous_ids = (
        previous.ordered_observation_semantic_ids if previous is not None else ()
    )
    appended_ids = tuple(
        item.wire_semantic.observation_semantic_id for item in appended_observations
    )
    ordered_ids = (*previous_ids, *appended_ids)
    if len(ordered_ids) != len(set(ordered_ids)):
        raise ValueError("runtime observation lifecycle reducer saw duplicate identity")
    closed = set(
        previous.closed_protection_scope_semantic_ids if previous is not None else ()
    )
    latest_clock = (
        previous.latest_clock_observation_semantic_id
        if previous is not None
        else None
    )
    for observation in appended_observations:
        if observation.wire_semantic.observation_kind == "runtime_clock":
            latest_clock = observation.wire_semantic.observation_semantic_id
        transition = observation.source_attribution.transition_kind
        if transition in {"terminal", "delivery"}:
            closed.add(
                observation.source_attribution.protection_scope_semantic_id
            )
    effective_ids = frozenset(
        item.effective_snapshot.observation_semantic_id for item in effective_heads
    )
    payload = {
        "ordered_observation_semantic_ids": ordered_ids,
        "effective_head_observation_semantic_ids": tuple(sorted(effective_ids)),
        "latest_clock_observation_semantic_id": latest_clock,
        "closed_protection_scope_semantic_ids": tuple(sorted(closed)),
        "pending_dependency_observation_semantic_ids": (),
    }
    return RuntimeObservationLifecycleReducerState(
        ordered_observation_semantic_ids=ordered_ids,
        effective_head_observation_semantic_ids=effective_ids,
        latest_clock_observation_semantic_id=latest_clock,
        closed_protection_scope_semantic_ids=frozenset(closed),
        pending_dependency_observation_semantic_ids=frozenset(),
        state_fingerprint=context_fingerprint(
            "runtime-observation-lifecycle-reducer-state:v1", payload
        ),
    )


def classify_runtime_observation_lifecycle(
    *,
    state: RuntimeObservationLifecycleReducerState,
    observations: tuple[PreparedRuntimeObservationProviderUnitFact, ...],
    current_run_protection_scope_semantic_id: str | None,
) -> RuntimeObservationLifecycleSnapshot:
    ids = tuple(item.wire_semantic.observation_semantic_id for item in observations)
    if ids != state.ordered_observation_semantic_ids:
        raise ValueError("runtime observation lifecycle state/vector mismatch")
    protected = []
    eligible = []
    open_lifecycle = []
    pending = []
    for observation in observations:
        contract = runtime_observation_kind_contract(
            observation.wire_semantic.observation_kind
        )
        semantic_id = observation.wire_semantic.observation_semantic_id
        attribution = observation.source_attribution
        belongs_to_current_run = (
            current_run_protection_scope_semantic_id is not None
            and attribution.owning_run_protection_scope_semantic_id
            == current_run_protection_scope_semantic_id
        )
        causally_closed = (
            attribution.protection_scope_semantic_id
            in state.closed_protection_scope_semantic_ids
            or (
                attribution.protection_scope_kind == "run"
                and current_run_protection_scope_semantic_id is not None
                and attribution.protection_scope_semantic_id
                != current_run_protection_scope_semantic_id
            )
        )
        is_effective = (
            semantic_id in state.effective_head_observation_semantic_ids
        )
        is_latest_clock = (
            semantic_id == state.latest_clock_observation_semantic_id
        )
        is_pending = (
            semantic_id in state.pending_dependency_observation_semantic_ids
        )
        is_open = (
            contract.rewrite_eligibility == "after_causal_close"
            and not causally_closed
        )
        if is_open:
            open_lifecycle.append(observation)
        if is_pending:
            pending.append(observation)
        rewriteable = (
            contract.rewrite_eligibility == "superseded_only" and not is_effective
        ) or (
            contract.rewrite_eligibility == "after_causal_close" and causally_closed
        ) or (
            contract.rewrite_eligibility == "long_horizon_rewrite"
            and not belongs_to_current_run
        )
        must_protect = (
            contract.rewrite_eligibility == "never"
            or is_effective
            or is_latest_clock
            or belongs_to_current_run
            or is_open
            or is_pending
            or contract.protection_policy == "always"
        )
        if rewriteable and not must_protect:
            eligible.append(observation)
        else:
            protected.append(observation)
    snapshot_payload = {
        "state_fingerprint": state.state_fingerprint,
        "protected": tuple(
            item.wire_semantic.observation_semantic_id for item in protected
        ),
        "eligible": tuple(
            item.wire_semantic.observation_semantic_id for item in eligible
        ),
        "open": tuple(
            item.wire_semantic.observation_semantic_id for item in open_lifecycle
        ),
        "pending": tuple(
            item.wire_semantic.observation_semantic_id for item in pending
        ),
        "current_run": current_run_protection_scope_semantic_id,
    }
    return RuntimeObservationLifecycleSnapshot(
        active=observations,
        protected=tuple(protected),
        eligible=tuple(eligible),
        open_lifecycle=tuple(open_lifecycle),
        pending_dependency=tuple(pending),
        snapshot_fingerprint=context_fingerprint(
            "runtime-observation-lifecycle-snapshot:v1", snapshot_payload
        ),
    )


def validate_runtime_observation_source_head_transition(
    *,
    predecessor_heads,
    source_dispositions: tuple[ContextSourceDispositionFact, ...],
    appended_observations: tuple[PreparedRuntimeObservationProviderUnitFact, ...],
    resulting_heads,
    allow_rewrite_drop: bool = False,
) -> None:
    """Prove that replacement observations are the sole source-head writers."""

    def head_map(heads):
        result = {
            (
                item.effective_snapshot.source_id,
                item.effective_snapshot.source_instance_id,
            ): item
            for item in heads
        }
        if len(result) != len(tuple(heads)):
            raise ValueError("runtime observation source heads are duplicated")
        return result

    previous = head_map(predecessor_heads)
    resulting = head_map(resulting_heads)
    dispositions = {
        (item.source_id, item.source_instance_id): item
        for item in source_dispositions
    }
    if len(dispositions) != len(source_dispositions):
        raise ValueError("runtime observation source dispositions are duplicated")
    if not set(previous).issubset(dispositions):
        raise ValueError("historical runtime observation head lacks disposition")

    expected = dict(previous)
    transitioned: set[tuple[ContextSourceId, str]] = set()
    carried: set[tuple[ContextSourceId, str]] = set()
    for observation in appended_observations:
        source_id = observation.source_id
        if source_id in {None, ContextSourceId.RUNTIME_CLOCK}:
            continue
        contract = runtime_observation_kind_contract(
            observation.wire_semantic.observation_kind
        )
        if contract.lifecycle_class != "replacement_snapshot":
            continue
        key = (source_id, observation.wire_semantic.source_instance_id)
        prior = previous.get(key)
        if (
            prior is not None
            and prior.effective_snapshot.observation_semantic_id
            == observation.wire_semantic.observation_semantic_id
        ):
            if key in carried:
                raise ValueError("runtime observation source head was carried twice")
            if dispositions[key].disposition != "retain":
                raise ValueError("carried runtime observation lacks retain disposition")
            carried.add(key)
            continue
        if key in transitioned:
            raise ValueError("runtime observation source head changed twice in one append")
        disposition = dispositions.get(key)
        if disposition is None or disposition.disposition not in {
            "replace",
            "explicit_empty",
            "terminal",
        }:
            raise ValueError("replacement observation lacks a changing disposition")
        if (
            disposition.candidate_semantic_fingerprint
            != observation.owner_semantic_fingerprint
            or disposition.candidate_payload_semantic_fingerprint
            != observation.source_payload_semantic_fingerprint
        ):
            raise ValueError("replacement observation/disposition semantic join drifted")
        payload = observation.wire_semantic.payload
        if not isinstance(payload, ContextSourceReplacementObservationPayloadFact):
            raise ValueError("replacement observation lacks typed lineage payload")
        expected_predecessor_id = (
            prior.effective_snapshot.observation_semantic_id
            if prior is not None
            else "genesis"
        )
        expected_revision = (
            prior.effective_snapshot.committed_revision + 1
            if prior is not None
            else 1
        )
        transition = observation.source_attribution.transition_kind
        lifecycle = context_source_lifecycle_entry(source_id)
        binding = (transition, observation.wire_semantic.observation_kind)
        registered_bindings = {
            (item.transition_kind, item.observation_kind)
            for item in lifecycle.observation_kind_bindings
        }
        disposition_transition_valid = (
            disposition.disposition == "replace"
            and transition not in {"explicit_empty", "terminal"}
        ) or (
            disposition.disposition == "explicit_empty"
            and transition == "explicit_empty"
        ) or (
            disposition.disposition == "terminal" and transition == "terminal"
        )
        if (
            payload.predecessor_observation_semantic_id != expected_predecessor_id
            or payload.replacement_revision != expected_revision
            or binding not in registered_bindings
            or not disposition_transition_valid
        ):
            raise ValueError("replacement observation revision/transition drifted")
        head = resulting.get(key)
        if head is None:
            raise ValueError("replacement observation did not produce a source head")
        snapshot = head.effective_snapshot
        expected_status = {
            "replace": "active_snapshot",
            "explicit_empty": "explicit_empty_snapshot",
            "terminal": "source_closed",
        }[disposition.disposition]
        if (
            snapshot.source_id != source_id
            or snapshot.source_instance_id
            != observation.wire_semantic.source_instance_id
            or snapshot.committed_revision != expected_revision
            or snapshot.observation_semantic_id
            != observation.wire_semantic.observation_semantic_id
            or snapshot.predecessor_observation_semantic_id
            != (None if expected_predecessor_id == "genesis" else expected_predecessor_id)
            or snapshot.snapshot_semantic_fingerprint
            != observation.source_payload_semantic_fingerprint
            or snapshot.canonical_wire_semantic_fingerprint
            != observation.wire_semantic.wire_semantic_fingerprint
            or snapshot.causal_placement_semantic_fingerprint
            != observation.causal_placement.placement_semantic_fingerprint
            or snapshot.unit_causal_semantic_fingerprint
            != observation.unit_causal_semantic_fingerprint
            or snapshot.effective_status != expected_status
        ):
            raise ValueError("replacement observation/resulting source head drifted")
        expected[key] = head
        transitioned.add(key)

    for key, disposition in dispositions.items():
        prior = previous.get(key)
        if disposition.disposition == "retain":
            if prior is None or resulting.get(key) != prior or key in transitioned:
                raise ValueError("retain disposition changed its source head")
            if disposition.reason == "semantic_noop" and (
                disposition.candidate_payload_semantic_fingerprint
                != prior.effective_snapshot.snapshot_semantic_fingerprint
            ):
                raise ValueError("semantic-noop disposition differs from effective head")
            continue
        if disposition.disposition == "rewrite_required":
            if not allow_rewrite_drop or prior is None or key in resulting:
                raise ValueError("source rewrite did not remove exactly one old head")
            expected.pop(key, None)
            continue
        if key in transitioned:
            continue
        if disposition.disposition == "explicit_empty" and prior is None:
            if key in resulting:
                raise ValueError("genesis explicit-empty created a phantom source head")
            continue
        raise ValueError("changing source disposition lacks an appended observation")
    if resulting != expected:
        raise ValueError("runtime observation resulting source-head set is not exact")


def prepare_runtime_observation_rewrite(
    *,
    generation_snapshot,
    ordered_projection,
    parent_event_reference: ContextEventReferenceFact,
    authority_horizons: tuple[LedgerAuthorityHorizonFact, ...],
    artifact_namespace: str,
    required_replay_bindings: tuple[ProviderInputReplayBindingIdentityFact, ...],
    current_run_protection_scope_semantic_id: str | None = None,
) -> PreparedRuntimeObservationRewritePlan:
    """Prepare one exact observation rewrite under confirmed transcript authority."""

    core = generation_snapshot.core_state
    resident = generation_snapshot.resident
    if core is None or resident is None or core.status != "open":
        raise ValueError("runtime observation rewrite lacks an open resident generation")
    active = tuple(generation_snapshot.runtime_observation_units)
    if len({item.wire_semantic.observation_semantic_id for item in active}) != len(
        active
    ):
        raise ValueError("runtime observation stable state contains duplicate identities")
    unit_by_semantic = {
        item.attribution.semantic.semantic_fingerprint: item for item in resident.units
    }
    for observation in active:
        if observation.provider_unit_semantic_fingerprint not in unit_by_semantic:
            raise ValueError("runtime observation stable state cannot hydrate a unit")

    lifecycle_state = getattr(
        generation_snapshot, "runtime_observation_lifecycle_state", None
    )
    if lifecycle_state is None:
        lifecycle_state = advance_runtime_observation_lifecycle_state(
            None,
            appended_observations=active,
            effective_heads=core.committed_source_heads,
        )
    lifecycle_snapshot = classify_runtime_observation_lifecycle(
        state=lifecycle_state,
        observations=active,
        current_run_protection_scope_semantic_id=(
            current_run_protection_scope_semantic_id
        ),
    )
    eligible_tuple = lifecycle_snapshot.eligible
    protected_tuple = lifecycle_snapshot.protected
    retained_tuple: tuple[PreparedRuntimeObservationProviderUnitFact, ...] = ()
    rewritten_tuple = eligible_tuple
    pending_tuple = lifecycle_snapshot.pending_dependency
    effective_ids = set(lifecycle_state.effective_head_observation_semantic_ids)
    _validate_observation_partition(
        active=active,
        protected=protected_tuple,
        retained=retained_tuple,
        rewritten=rewritten_tuple,
        eligible=eligible_tuple,
        open_lifecycle=lifecycle_snapshot.open_lifecycle,
        pending_dependency=pending_tuple,
        effective_ids=effective_ids,
    )

    policy = default_runtime_observation_physical_policy()
    artifacts: list[PreparedProviderInputArtifact] = []
    active_set = _prepare_observation_set(
        "active", active, artifact_namespace=artifact_namespace, policy=policy
    )
    protected_set = _prepare_observation_set(
        "protected",
        protected_tuple,
        artifact_namespace=artifact_namespace,
        policy=policy,
    )
    eligible_set = _prepare_observation_set(
        "eligible",
        eligible_tuple,
        artifact_namespace=artifact_namespace,
        policy=policy,
    )
    retained_set = _prepare_observation_set(
        "retained",
        retained_tuple,
        artifact_namespace=artifact_namespace,
        policy=policy,
    )
    rewritten_set = _prepare_observation_set(
        "rewritten",
        rewritten_tuple,
        artifact_namespace=artifact_namespace,
        policy=policy,
    )
    open_set = _prepare_observation_set(
        "open_lifecycle",
        lifecycle_snapshot.open_lifecycle,
        artifact_namespace=artifact_namespace,
        policy=policy,
    )
    pending_set = _prepare_observation_set(
        "pending_dependency",
        pending_tuple,
        artifact_namespace=artifact_namespace,
        policy=policy,
    )
    for prepared in (
        active_set,
        protected_set,
        eligible_set,
        retained_set,
        rewritten_set,
        open_set,
        pending_set,
    ):
        artifacts.extend(prepared.artifacts)

    head_set = _prepare_effective_head_set(
        core.committed_source_heads,
        artifact_namespace=artifact_namespace,
        policy=policy,
    )
    artifacts.extend(head_set.artifacts)
    stable = build_frozen_fact(
        RuntimeObservationProjectionStableStateFact,
        schema_version="runtime_observation_projection_stable_state.v1",
        state_revision=core.revision,
        source_generation_id=core.generation.generation_id,
        source_generation_core_fingerprint=core.core_state_fingerprint,
        authority_horizon_set_reference=core.committed_authority_horizon_set,
        active_observations=active_set.reference,
        protected_observations=protected_set.reference,
        eligible_observations=eligible_set.reference,
        open_lifecycle_observations=open_set.reference,
        pending_dependency_observations=pending_set.reference,
        effective_heads=head_set.reference,
        classification_contract_fingerprint=(
            OBSERVATION_CLASSIFICATION_CONTRACT_FINGERPRINT
        ),
        physical_policy_fingerprint=policy.policy_fingerprint,
    )
    proof_artifact = prepared_json_artifact(
        "runtime-observation-partition-proof",
        {
            "schema_version": "runtime_observation_partition_proof_artifact.v1",
            "source_stable_state_fingerprint": stable.stable_state_fingerprint,
            "active": active_set.reference.model_dump(mode="json"),
            "protected": protected_set.reference.model_dump(mode="json"),
            "retained": retained_set.reference.model_dump(mode="json"),
            "rewritten": rewritten_set.reference.model_dump(mode="json"),
            "eligible": eligible_set.reference.model_dump(mode="json"),
        },
        artifact_namespace=artifact_namespace,
        contract_fingerprint=OBSERVATION_PARTITION_CONTRACT_FINGERPRINT,
        metadata_kind="runtime_observation_partition_proof",
    )
    artifacts.append(proof_artifact)
    proof = build_frozen_fact(
        RuntimeObservationProjectionPartitionProofFact,
        schema_version="runtime_observation_projection_partition_proof.v1",
        source_stable_state_fingerprint=stable.stable_state_fingerprint,
        active_set_reference=active_set.reference,
        protected_set_reference=protected_set.reference,
        retained_set_reference=retained_set.reference,
        rewritten_set_reference=rewritten_set.reference,
        eligible_set_reference=eligible_set.reference,
        merkle_partition_proof_reference=proof_artifact.artifact_reference,
        partition_contract_fingerprint=OBSERVATION_PARTITION_CONTRACT_FINGERPRINT,
    )

    rewrite_id = "runtime-observation-rewrite:" + context_fingerprint(
        "runtime-observation-rewrite-id:v1",
        (
            parent_event_reference.payload_fingerprint,
            stable.stable_state_fingerprint,
            proof.proof_fingerprint,
        ),
    ).removeprefix("sha256:")
    projected_units, projected_observations, rewrite_units, group_artifacts = (
        _project_active_observations(
            active=active,
            eligible=eligible_tuple,
            unit_by_semantic=unit_by_semantic,
            ordered_projection=ordered_projection,
            parent_event_reference=parent_event_reference,
            authority_horizons=authority_horizons,
            required_replay_bindings=required_replay_bindings,
            stable=stable,
            proof=proof,
            rewrite_id=rewrite_id,
            artifact_namespace=artifact_namespace,
            policy=policy,
            current_run_protection_scope_semantic_id=(
                current_run_protection_scope_semantic_id
            ),
        )
    )
    artifacts.extend(group_artifacts)
    projection_artifact = None
    if rewrite_units:
        projection_root, projection_pages = _prepare_merkle_tree(
            namespace="runtime-observation-rewrite-projection",
            records=tuple(item.model_dump(mode="json") for item in rewrite_units),
            semantic_fingerprints=tuple(
                item.semantic.unit_semantic_fingerprint for item in rewrite_units
            ),
            causal_keys=tuple(
                item.semantic.causal_placement.placement_semantic_fingerprint
                for item in rewrite_units
            ),
            artifact_namespace=artifact_namespace,
            policy=policy,
            contract_fingerprint=(
                OBSERVATION_REWRITE_PROJECTION_CONTRACT_FINGERPRINT
            ),
            leaf_metadata_kind="runtime_observation_rewrite_projection_leaf",
            internal_metadata_kind=(
                "runtime_observation_rewrite_projection_internal"
            ),
            leaf_schema_version=(
                "runtime_observation_rewrite_projection_leaf.v1"
            ),
            internal_schema_version=(
                "runtime_observation_rewrite_projection_internal.v1"
            ),
        )
        if projection_root is None:
            raise ValueError("non-empty rewrite projection lacks a tree root")
        artifacts.extend(projection_pages)
        projection_artifact = prepared_json_artifact(
            "runtime-observation-rewrite-projection",
            {
                "schema_version": (
                    "prepared_runtime_observation_rewrite_projection_root.v1"
                ),
                "rewrite_id": rewrite_id,
                "unit_count": len(rewrite_units),
                "ordered_unit_semantic_accumulator": context_fingerprint(
                    "runtime-observation-rewrite-unit-semantics:v1",
                    tuple(
                        item.semantic.unit_semantic_fingerprint
                        for item in rewrite_units
                    ),
                ),
                "ordered_causal_placement_accumulator": context_fingerprint(
                    "runtime-observation-rewrite-unit-placements:v1",
                    tuple(
                        item.semantic.causal_placement.placement_semantic_fingerprint
                        for item in rewrite_units
                    ),
                ),
                "root_node_reference": projection_root.model_dump(mode="json"),
            },
            artifact_namespace=artifact_namespace,
            contract_fingerprint=(
                OBSERVATION_REWRITE_PROJECTION_CONTRACT_FINGERPRINT
            ),
            metadata_kind="runtime_observation_rewrite_projection",
        )
        artifacts.append(projection_artifact)
    prepared_projection = build_frozen_fact(
        PreparedRuntimeObservationRewriteProjectionReferenceFact,
        schema_version=(
            "prepared_runtime_observation_rewrite_projection_reference.v1"
        ),
        unit_count=len(rewrite_units),
        ordered_unit_semantic_accumulator=context_fingerprint(
            "runtime-observation-rewrite-unit-semantics:v1",
            tuple(item.semantic.unit_semantic_fingerprint for item in rewrite_units),
        ),
        ordered_causal_placement_accumulator=context_fingerprint(
            "runtime-observation-rewrite-unit-placements:v1",
            tuple(
                item.semantic.causal_placement.placement_semantic_fingerprint
                for item in rewrite_units
            ),
        ),
        root_artifact_reference=(
            projection_artifact.artifact_reference
            if projection_artifact is not None
            else None
        ),
        projection_contract_fingerprint=(
            OBSERVATION_REWRITE_PROJECTION_CONTRACT_FINGERPRINT
        ),
    )
    unique = _unique_artifacts(artifacts)
    changed_node_count = sum(
        artifact.semantic_metadata.get("artifact_kind")
        in {
            "runtime_observation_projection_set_leaf",
            "runtime_observation_projection_set_internal",
            "runtime_observation_rewrite_projection_leaf",
            "runtime_observation_rewrite_projection_internal",
        }
        for artifact in unique
    )
    if changed_node_count > policy.maximum_changed_nodes_per_rewrite:
        raise ValueError("runtime observation rewrite exceeds total changed-node bound")
    if len(unique) > policy.maximum_artifact_batches_per_rewrite:
        raise ValueError("runtime observation rewrite exceeds artifact operation bound")
    if any(
        artifact.artifact_reference.content_bytes
        > policy.leaf_max_canonical_bytes
        for artifact in unique
    ):
        raise ValueError("runtime observation rewrite artifact exceeds byte bound")
    return PreparedRuntimeObservationRewritePlan(
        rewrite_id=rewrite_id,
        parent_event_reference=parent_event_reference,
        source_stable_state=stable,
        partition_proof=proof,
        prepared_projection=prepared_projection,
        source_effective_heads=head_set.reference,
        projected_units=projected_units,
        projected_observations=projected_observations,
        rewrite_units=rewrite_units,
        artifacts=unique,
        physical_policy=policy,
    )


def _project_active_observations(
    *,
    active: tuple[PreparedRuntimeObservationProviderUnitFact, ...],
    eligible: tuple[PreparedRuntimeObservationProviderUnitFact, ...],
    unit_by_semantic: dict[str, ProviderInputUnitMaterializationFact],
    ordered_projection,
    parent_event_reference: ContextEventReferenceFact,
    authority_horizons: tuple[LedgerAuthorityHorizonFact, ...],
    required_replay_bindings: tuple[ProviderInputReplayBindingIdentityFact, ...],
    stable: RuntimeObservationProjectionStableStateFact,
    proof: RuntimeObservationProjectionPartitionProofFact,
    rewrite_id: str,
    artifact_namespace: str,
    policy: RuntimeObservationProjectionPhysicalPolicyFact,
    current_run_protection_scope_semantic_id: str | None,
) -> tuple[
    tuple[ProviderInputUnitMaterializationFact, ...],
    tuple[PreparedRuntimeObservationProviderUnitFact, ...],
    tuple[PreparedRuntimeObservationRewriteProjectionUnitFact, ...],
    tuple[PreparedProviderInputArtifact, ...],
]:
    eligible_ids = {item.wire_semantic.observation_semantic_id for item in eligible}
    projection_nodes = {
        item.causal_placement.node_identity.node_identity_fingerprint: (
            item.causal_placement.node_identity
        )
        for item in ordered_projection.projection.ordered_units
    }
    summaries = tuple(
        item.causal_placement.node_identity
        for item in ordered_projection.projection.ordered_units
        if isinstance(
            item.source_attribution,
            CompactionReplacementSummarySourceAttributionFact,
        )
    )
    summary_node = summaries[0] if len(summaries) == 1 else None

    output_units: list[ProviderInputUnitMaterializationFact] = []
    output_observations: list[PreparedRuntimeObservationProviderUnitFact] = []
    rewrite_facts: list[PreparedRuntimeObservationRewriteProjectionUnitFact] = []
    artifacts: list[PreparedProviderInputArtifact] = []
    index = 0
    group_index = 0
    while index < len(active):
        observation = active[index]
        semantic_id = observation.wire_semantic.observation_semantic_id
        if semantic_id not in eligible_ids:
            output_units.append(
                unit_by_semantic[observation.provider_unit_semantic_fingerprint]
            )
            output_observations.append(
                _rebind_retained_observation(
                    observation,
                    projection_nodes=projection_nodes,
                    summary_node=summary_node,
                )
            )
            index += 1
            continue
        first_key = _resolved_group_key(
            observation,
            projection_nodes=projection_nodes,
            summary_node=summary_node,
        )
        group = [observation]
        cursor = index + 1
        while cursor < len(active):
            next_observation = active[cursor]
            if next_observation.wire_semantic.observation_semantic_id not in eligible_ids:
                break
            if (
                _resolved_group_key(
                    next_observation,
                    projection_nodes=projection_nodes,
                    summary_node=summary_node,
                )
                != first_key
            ):
                break
            group.append(next_observation)
            cursor += 1
        group_set = _prepare_observation_set(
            "rewritten",
            tuple(group),
            artifact_namespace=artifact_namespace,
            policy=policy,
            namespace_suffix=f"group-{group_index}",
        )
        artifacts.extend(group_set.artifacts)
        unit, observation_fact, rewrite_fact = _rewrite_group(
            group=tuple(group),
            unit_by_semantic=unit_by_semantic,
            group_set=group_set.reference,
            group_key=first_key,
            parent_event_reference=parent_event_reference,
            authority_horizons=authority_horizons,
            required_replay_bindings=required_replay_bindings,
            stable=stable,
            proof=proof,
            rewrite_id=rewrite_id,
            group_index=group_index,
            current_run_protection_scope_semantic_id=(
                current_run_protection_scope_semantic_id
            ),
        )
        output_units.append(unit)
        output_observations.append(observation_fact)
        rewrite_facts.append(rewrite_fact)
        group_index += 1
        index = cursor
    return (
        tuple(output_units),
        tuple(output_observations),
        tuple(rewrite_facts),
        _unique_artifacts(artifacts),
    )


def _rebind_retained_observation(
    observation: PreparedRuntimeObservationProviderUnitFact,
    *,
    projection_nodes,
    summary_node,
) -> PreparedRuntimeObservationProviderUnitFact:
    placement = observation.causal_placement
    predecessor = placement.stable_predecessor_transcript_node
    if predecessor is None or (
        predecessor.node_identity_fingerprint in projection_nodes
    ):
        return observation
    if summary_node is None:
        raise ValueError(
            "retained observation predecessor has no compaction replacement"
        )
    rebound_placement = build_frozen_fact(
        RuntimeObservationCausalPlacementSemanticFact,
        schema_version="runtime_observation_causal_placement_semantic.v1",
        causal_scope_kind=placement.causal_scope_kind,
        causal_scope_semantic_id=placement.causal_scope_semantic_id,
        placement_phase=placement.placement_phase,
        stable_predecessor_transcript_node=summary_node,
        source_occurrence_semantic_fingerprint=(
            placement.source_occurrence_semantic_fingerprint
        ),
        intra_boundary_order=placement.intra_boundary_order,
        placement_contract_fingerprint=context_fingerprint(
            "runtime-observation-rewrite-placement-contract:v1",
            "map-rewritten-predecessor-to-compaction-summary",
        ),
    )
    unit_causal = context_fingerprint(
        "runtime-observation-provider-unit-causal:v1",
        (
            observation.wire_semantic.wire_semantic_fingerprint,
            rebound_placement.placement_semantic_fingerprint,
            observation.provider_fragment_semantic_fingerprint,
            observation.provider_unit_semantic_fingerprint,
        ),
    )
    return build_frozen_fact(
        PreparedRuntimeObservationProviderUnitFact,
        schema_version="prepared_runtime_observation_provider_unit.v1",
        wire_semantic=observation.wire_semantic,
        causal_placement=rebound_placement,
        source_attribution=observation.source_attribution,
        source_id=observation.source_id,
        source_candidate_key=observation.source_candidate_key,
        source_payload_semantic_fingerprint=(
            observation.source_payload_semantic_fingerprint
        ),
        owner_semantic_fingerprint=observation.owner_semantic_fingerprint,
        provider_fragment_semantic_fingerprint=(
            observation.provider_fragment_semantic_fingerprint
        ),
        provider_unit_semantic_fingerprint=(
            observation.provider_unit_semantic_fingerprint
        ),
        unit_causal_semantic_fingerprint=unit_causal,
    )


def _resolved_group_key(observation, *, projection_nodes, summary_node):
    placement = observation.causal_placement
    predecessor = placement.stable_predecessor_transcript_node
    predecessor_id = predecessor.node_identity_fingerprint if predecessor else None
    if predecessor_id is None:
        resolved = None
    else:
        resolved = projection_nodes.get(predecessor_id)
        if resolved is None:
            if summary_node is None:
                raise ValueError(
                    "rewritten observation predecessor has no compaction replacement"
                )
            resolved = summary_node
    return (
        resolved,
        placement.placement_phase,
        placement.causal_scope_kind,
        placement.causal_scope_semantic_id,
        observation.source_attribution.protection_scope_kind,
        observation.source_attribution.protection_scope_semantic_id,
    )


def _rewrite_group(
    *,
    group,
    unit_by_semantic,
    group_set,
    group_key,
    parent_event_reference,
    authority_horizons,
    required_replay_bindings,
    stable,
    proof,
    rewrite_id,
    group_index,
    current_run_protection_scope_semantic_id,
):
    segments = tuple(
        _coverage_segment(
            item,
            unit_by_semantic[item.provider_unit_semantic_fingerprint],
        )
        for item in group
    )
    coverage = build_frozen_fact(
        RuntimeObservationRewriteCoverageSemanticFact,
        schema_version="runtime_observation_rewrite_coverage_semantic.v1",
        direct_member_count=len(group),
        transitive_original_observation_count=sum(item.count for item in segments),
        ordered_original_semantic_accumulator=context_fingerprint(
            "runtime-observation-coverage-ordered-semantic:v1",
            tuple((item.count, item.semantic_accumulator) for item in segments),
        ),
        ordered_original_causal_accumulator=context_fingerprint(
            "runtime-observation-coverage-ordered-causal:v1",
            tuple((item.count, item.causal_accumulator) for item in segments),
        ),
        transitive_coverage_root_fingerprint=context_fingerprint(
            "runtime-observation-transitive-coverage-root:v1",
            tuple((item.count, item.coverage_root) for item in segments),
        ),
        coverage_contract_fingerprint=OBSERVATION_COVERAGE_CONTRACT_FINGERPRINT,
    )
    kind_counts: dict[str, int] = {}
    for item in group:
        kind = item.wire_semantic.observation_kind
        kind_counts[kind] = kind_counts.get(kind, 0) + 1
    payload = runtime_observation_rewrite_projection_payload(
        covered_direct_member_count=len(group),
        covered_kind_counts=tuple(sorted(kind_counts.items())),
        covered_original_observation_count=(
            coverage.transitive_original_observation_count
        ),
        coverage_semantic_fingerprint=coverage.coverage_semantic_fingerprint,
        ordered_original_causal_accumulator=(
            coverage.ordered_original_causal_accumulator
        ),
        ordered_original_semantic_accumulator=(
            coverage.ordered_original_semantic_accumulator
        ),
        transitive_coverage_root_fingerprint=(
            coverage.transitive_coverage_root_fingerprint
        ),
        summary=(
            "Earlier superseded runtime snapshots and historical runtime clock "
            "observations were compacted under confirmed Long-Horizon authority. "
            "Current effective snapshots and protected observations remain separate."
        ),
    )
    source_instance_id = f"{rewrite_id}:group:{group_index}"
    message = LLMMessage.runtime_observation(
        payload,
        observation_kind="runtime_observation_rewrite_projection",
        source_instance_id=source_instance_id,
        lifecycle_class="immutable_append_once",
        authority_class="runtime_fact",
        causal_occurrence_semantic_fingerprint=(
            coverage.coverage_semantic_fingerprint
        ),
    )
    fragment_text = message.content[0]
    wire = decode_runtime_observation_wire_semantic(
        fragment_text,
        causal_occurrence_semantic_fingerprint=(
            coverage.coverage_semantic_fingerprint
        ),
    )
    (
        predecessor,
        phase,
        scope_kind,
        scope_id,
        protection_scope_kind,
        protection_scope_semantic_id,
    ) = group_key
    placement = build_frozen_fact(
        RuntimeObservationCausalPlacementSemanticFact,
        schema_version="runtime_observation_causal_placement_semantic.v1",
        causal_scope_kind=scope_kind,
        causal_scope_semantic_id=scope_id,
        placement_phase=phase,
        stable_predecessor_transcript_node=predecessor,
        source_occurrence_semantic_fingerprint=(
            coverage.coverage_semantic_fingerprint
        ),
        intra_boundary_order=min(
            item.causal_placement.intra_boundary_order for item in group
        ),
        placement_contract_fingerprint=context_fingerprint(
            "runtime-observation-rewrite-placement-contract:v1",
            "derive-from-contiguous-committed-member-placements",
        ),
    )
    source_attribution = build_frozen_fact(
        RuntimeObservationSourceAttributionFact,
        schema_version="runtime_observation_source_attribution.v3",
        observation_semantic_fingerprint=wire.wire_semantic_fingerprint,
        producer=runtime_observation_derived_producer(
            observation_kind="runtime_observation_rewrite_projection",
            producer_kind="long_horizon_rewrite",
        ),
        transition_kind=None,
        protection_scope_kind=protection_scope_kind,
        protection_scope_semantic_id=protection_scope_semantic_id,
        owning_run_protection_scope_semantic_id=(
            current_run_protection_scope_semantic_id
        ),
        source_event_references=(parent_event_reference,),
        source_artifact_references=(),
        authority_horizons=authority_horizons,
    )
    materialized = freeze_message_unit(
        message,
        unit_kind="runtime_observation_rewrite",
        owner_semantic_fingerprint=coverage.coverage_semantic_fingerprint,
        authority_horizons=authority_horizons,
        estimated_tokens=max(1, len(fragment_text.encode("utf-8")) // 4),
        source_event_refs=(parent_event_reference,),
        required_replay_bindings=required_replay_bindings,
    )
    unit_semantic = build_frozen_fact(
        RuntimeObservationRewriteUnitSemanticFact,
        schema_version="runtime_observation_rewrite_unit_semantic.v1",
        observation_semantic_id=wire.observation_semantic_id,
        canonical_provider_fragment=materialized.canonical_provider_fragment,
        lowering_lane="runtime_observation",
        causal_placement=placement,
        coverage_semantic=coverage,
    )
    attribution = build_frozen_fact(
        RuntimeObservationRewriteUnitAttributionFact,
        schema_version="runtime_observation_rewrite_unit_attribution.v1",
        unit_semantic_fingerprint=unit_semantic.unit_semantic_fingerprint,
        rewritten_source_set_reference=group_set,
        source_stable_state_fingerprint=stable.stable_state_fingerprint,
        partition_proof_fingerprint=proof.proof_fingerprint,
    )
    rewrite_fact = build_frozen_fact(
        PreparedRuntimeObservationRewriteProjectionUnitFact,
        schema_version="prepared_runtime_observation_rewrite_projection_unit.v2",
        semantic=unit_semantic,
        attribution=attribution,
    )
    unit_causal = context_fingerprint(
        "runtime-observation-provider-unit-causal:v1",
        (
            wire.wire_semantic_fingerprint,
            placement.placement_semantic_fingerprint,
            materialized.canonical_provider_fragment.semantic_fingerprint,
            materialized.attribution.semantic.semantic_fingerprint,
        ),
    )
    observation_fact = build_frozen_fact(
        PreparedRuntimeObservationProviderUnitFact,
        schema_version="prepared_runtime_observation_provider_unit.v1",
        wire_semantic=wire,
        causal_placement=placement,
        source_attribution=source_attribution,
        source_id=None,
        source_candidate_key=None,
        source_payload_semantic_fingerprint=None,
        owner_semantic_fingerprint=coverage.coverage_semantic_fingerprint,
        provider_fragment_semantic_fingerprint=(
            materialized.canonical_provider_fragment.semantic_fingerprint
        ),
        provider_unit_semantic_fingerprint=(
            materialized.attribution.semantic.semantic_fingerprint
        ),
        unit_causal_semantic_fingerprint=unit_causal,
    )
    return materialized, observation_fact, rewrite_fact


def _coverage_segment(observation, unit) -> _CoverageSegment:
    if observation.wire_semantic.observation_kind == (
        "runtime_observation_rewrite_projection"
    ):
        payload = observation.wire_semantic.payload
        if not isinstance(payload, RuntimeObservationRewriteProjectionPayloadFact):
            raise ValueError("prior observation rewrite payload is not typed")
        coverage = build_frozen_fact(
            RuntimeObservationRewriteCoverageSemanticFact,
            schema_version="runtime_observation_rewrite_coverage_semantic.v1",
            direct_member_count=payload.covered_direct_member_count,
            transitive_original_observation_count=(
                payload.covered_original_observation_count
            ),
            ordered_original_semantic_accumulator=(
                payload.ordered_original_semantic_accumulator
            ),
            ordered_original_causal_accumulator=(
                payload.ordered_original_causal_accumulator
            ),
            transitive_coverage_root_fingerprint=(
                payload.transitive_coverage_root_fingerprint
            ),
            coverage_contract_fingerprint=(
                OBSERVATION_COVERAGE_CONTRACT_FINGERPRINT
            ),
        )
        if (
            coverage.coverage_semantic_fingerprint
            != payload.coverage_semantic_fingerprint
        ):
            raise ValueError("prior observation rewrite coverage identity drifted")
        return _CoverageSegment(
            count=coverage.transitive_original_observation_count,
            semantic_accumulator=coverage.ordered_original_semantic_accumulator,
            causal_accumulator=coverage.ordered_original_causal_accumulator,
            coverage_root=coverage.transitive_coverage_root_fingerprint,
        )
    semantic = context_fingerprint(
        "runtime-observation-original-semantic-leaf:v1",
        observation.wire_semantic.wire_semantic_fingerprint,
    )
    causal = context_fingerprint(
        "runtime-observation-original-causal-leaf:v1",
        observation.causal_placement.placement_semantic_fingerprint,
    )
    return _CoverageSegment(
        count=1,
        semantic_accumulator=semantic,
        causal_accumulator=causal,
        coverage_root=context_fingerprint(
            "runtime-observation-original-coverage-leaf:v1", (semantic, causal)
        ),
    )


def _prepare_observation_set(
    set_kind,
    observations,
    *,
    artifact_namespace,
    policy,
    namespace_suffix="set",
) -> _PreparedSet:
    records = tuple(item.model_dump(mode="json") for item in observations)
    semantics = tuple(item.unit_causal_semantic_fingerprint for item in observations)
    causal_keys = tuple(_causal_key(item) for item in observations)
    root, artifacts = _prepare_merkle_tree(
        namespace=f"runtime-observation-{namespace_suffix}-{set_kind}",
        records=records,
        semantic_fingerprints=semantics,
        causal_keys=causal_keys,
        artifact_namespace=artifact_namespace,
        policy=policy,
    )
    reference = build_frozen_fact(
        RuntimeObservationProjectionSetReferenceFact,
        schema_version="runtime_observation_projection_set_reference.v1",
        set_kind=set_kind,
        member_count=len(observations),
        ordered_semantic_accumulator=context_fingerprint(
            "runtime-observation-set-ordered-semantic:v1", semantics
        ),
        ordered_causal_accumulator=context_fingerprint(
            "runtime-observation-set-ordered-causal:v1", causal_keys
        ),
        root_node_reference=root,
        set_contract_fingerprint=OBSERVATION_SET_CONTRACT_FINGERPRINT,
    )
    return _PreparedSet(reference=reference, artifacts=artifacts)


def _prepare_effective_head_set(heads, *, artifact_namespace, policy):
    records = tuple(item.model_dump(mode="json") for item in heads)
    semantics = tuple(item.semantic_head_fingerprint for item in heads)
    causal_keys = tuple(
        context_fingerprint(
            "runtime-observation-effective-head-key:v1",
            (
                item.effective_snapshot.source_id.value,
                item.effective_snapshot.source_instance_id,
            ),
        )
        for item in heads
    )
    root, artifacts = _prepare_merkle_tree(
        namespace="runtime-observation-effective-heads",
        records=records,
        semantic_fingerprints=semantics,
        causal_keys=causal_keys,
        artifact_namespace=artifact_namespace,
        policy=policy,
    )
    reference = build_frozen_fact(
        RuntimeObservationEffectiveHeadSetReferenceFact,
        schema_version="runtime_observation_effective_head_set_reference.v1",
        head_count=len(heads),
        ordered_head_accumulator=context_fingerprint(
            "runtime-observation-effective-heads:v1", semantics
        ),
        root_node_reference=root,
        set_contract_fingerprint=OBSERVATION_SET_CONTRACT_FINGERPRINT,
    )
    return _PreparedHeadSet(reference=reference, artifacts=artifacts)


def prepare_runtime_observation_effective_head_set(
    heads,
    *,
    artifact_namespace: str,
    policy: RuntimeObservationProjectionPhysicalPolicyFact | None = None,
) -> tuple[
    RuntimeObservationEffectiveHeadSetReferenceFact,
    tuple[PreparedProviderInputArtifact, ...],
]:
    prepared = _prepare_effective_head_set(
        heads,
        artifact_namespace=artifact_namespace,
        policy=policy or default_runtime_observation_physical_policy(),
    )
    return prepared.reference, prepared.artifacts


def validate_runtime_observation_rewrite_transition(
    *,
    source_core: CommittedProviderInputGenerationCoreStateFact,
    source_observations: tuple[PreparedRuntimeObservationProviderUnitFact, ...],
    source_lifecycle_state: RuntimeObservationLifecycleReducerState,
    resulting_core: CommittedProviderInputGenerationCoreStateFact,
    resulting_observations: tuple[PreparedRuntimeObservationProviderUnitFact, ...],
    rewrite: RuntimeObservationProjectionRewriteFact,
    current_run_protection_scope_semantic_id: str | None,
    artifact_namespace: str,
) -> None:
    """Recompute the bounded rewrite authority at durable rollover fold time."""

    policy = default_runtime_observation_physical_policy()
    lifecycle = classify_runtime_observation_lifecycle(
        state=source_lifecycle_state,
        observations=source_observations,
        current_run_protection_scope_semantic_id=(
            current_run_protection_scope_semantic_id
        ),
    )
    active = _prepare_observation_set(
        "active", source_observations, artifact_namespace=artifact_namespace, policy=policy
    )
    protected = _prepare_observation_set(
        "protected",
        lifecycle.protected,
        artifact_namespace=artifact_namespace,
        policy=policy,
    )
    eligible = _prepare_observation_set(
        "eligible",
        lifecycle.eligible,
        artifact_namespace=artifact_namespace,
        policy=policy,
    )
    retained = _prepare_observation_set(
        "retained", (), artifact_namespace=artifact_namespace, policy=policy
    )
    rewritten = _prepare_observation_set(
        "rewritten",
        lifecycle.eligible,
        artifact_namespace=artifact_namespace,
        policy=policy,
    )
    open_lifecycle = _prepare_observation_set(
        "open_lifecycle",
        lifecycle.open_lifecycle,
        artifact_namespace=artifact_namespace,
        policy=policy,
    )
    pending = _prepare_observation_set(
        "pending_dependency",
        lifecycle.pending_dependency,
        artifact_namespace=artifact_namespace,
        policy=policy,
    )
    source_heads = _prepare_effective_head_set(
        source_core.committed_source_heads,
        artifact_namespace=artifact_namespace,
        policy=policy,
    )
    expected_stable = build_frozen_fact(
        RuntimeObservationProjectionStableStateFact,
        schema_version="runtime_observation_projection_stable_state.v1",
        state_revision=source_core.revision,
        source_generation_id=source_core.generation.generation_id,
        source_generation_core_fingerprint=source_core.core_state_fingerprint,
        authority_horizon_set_reference=(
            source_core.committed_authority_horizon_set
        ),
        active_observations=active.reference,
        protected_observations=protected.reference,
        eligible_observations=eligible.reference,
        open_lifecycle_observations=open_lifecycle.reference,
        pending_dependency_observations=pending.reference,
        effective_heads=source_heads.reference,
        classification_contract_fingerprint=(
            OBSERVATION_CLASSIFICATION_CONTRACT_FINGERPRINT
        ),
        physical_policy_fingerprint=policy.policy_fingerprint,
    )
    if rewrite.source_stable_state != expected_stable:
        raise ValueError("runtime observation rewrite source state is not canonical")
    proof_artifact = prepared_json_artifact(
        "runtime-observation-partition-proof",
        {
            "schema_version": "runtime_observation_partition_proof_artifact.v1",
            "source_stable_state_fingerprint": expected_stable.stable_state_fingerprint,
            "active": active.reference.model_dump(mode="json"),
            "protected": protected.reference.model_dump(mode="json"),
            "retained": retained.reference.model_dump(mode="json"),
            "rewritten": rewritten.reference.model_dump(mode="json"),
            "eligible": eligible.reference.model_dump(mode="json"),
        },
        artifact_namespace=artifact_namespace,
        contract_fingerprint=OBSERVATION_PARTITION_CONTRACT_FINGERPRINT,
        metadata_kind="runtime_observation_partition_proof",
    )
    expected_proof = build_frozen_fact(
        RuntimeObservationProjectionPartitionProofFact,
        schema_version="runtime_observation_projection_partition_proof.v1",
        source_stable_state_fingerprint=expected_stable.stable_state_fingerprint,
        active_set_reference=active.reference,
        protected_set_reference=protected.reference,
        retained_set_reference=retained.reference,
        rewritten_set_reference=rewritten.reference,
        eligible_set_reference=eligible.reference,
        merkle_partition_proof_reference=proof_artifact.artifact_reference,
        partition_contract_fingerprint=OBSERVATION_PARTITION_CONTRACT_FINGERPRINT,
    )
    if rewrite.partition_proof != expected_proof:
        raise ValueError("runtime observation rewrite partition proof is not canonical")
    resulting_heads = _prepare_effective_head_set(
        resulting_core.committed_source_heads,
        artifact_namespace=artifact_namespace,
        policy=policy,
    )
    if rewrite.resulting_effective_heads != resulting_heads.reference:
        raise ValueError("runtime observation rewrite resulting heads drifted")
    _validate_rewrite_projection_coverage(
        source_observations=source_observations,
        protected_observation_ids={
            item.wire_semantic.observation_semantic_id
            for item in lifecycle.protected
        },
        eligible_observation_ids={
            item.wire_semantic.observation_semantic_id for item in lifecycle.eligible
        },
        resulting_observations=resulting_observations,
    )


def _validate_rewrite_projection_coverage(
    *,
    source_observations,
    protected_observation_ids,
    eligible_observation_ids,
    resulting_observations,
) -> None:
    source_index = 0
    resulting_index = 0
    while source_index < len(source_observations):
        source = source_observations[source_index]
        semantic_id = source.wire_semantic.observation_semantic_id
        if semantic_id in protected_observation_ids:
            if resulting_index >= len(resulting_observations):
                raise ValueError("runtime observation rewrite dropped a protected unit")
            resulting = resulting_observations[resulting_index]
            if resulting.wire_semantic != source.wire_semantic:
                raise ValueError("runtime observation rewrite changed protected wire semantic")
            source_index += 1
            resulting_index += 1
            continue
        if semantic_id not in eligible_observation_ids:
            raise ValueError("runtime observation rewrite source is unclassified")
        if resulting_index >= len(resulting_observations):
            raise ValueError("runtime observation rewrite omitted eligible coverage")
        resulting = resulting_observations[resulting_index]
        if resulting.wire_semantic.observation_kind != (
            "runtime_observation_rewrite_projection"
        ):
            raise ValueError("eligible observations lack a typed rewrite projection")
        payload = resulting.wire_semantic.payload
        if not isinstance(payload, RuntimeObservationRewriteProjectionPayloadFact):
            raise ValueError("runtime observation rewrite projection payload is untyped")
        direct_count = payload.covered_direct_member_count
        group = source_observations[source_index : source_index + direct_count]
        if len(group) != direct_count or any(
            item.wire_semantic.observation_semantic_id
            not in eligible_observation_ids
            for item in group
        ):
            raise ValueError("runtime observation rewrite coverage crosses a protected unit")
        segments = tuple(_coverage_segment(item, None) for item in group)
        expected = build_frozen_fact(
            RuntimeObservationRewriteCoverageSemanticFact,
            schema_version="runtime_observation_rewrite_coverage_semantic.v1",
            direct_member_count=direct_count,
            transitive_original_observation_count=sum(
                item.count for item in segments
            ),
            ordered_original_semantic_accumulator=context_fingerprint(
                "runtime-observation-coverage-ordered-semantic:v1",
                tuple((item.count, item.semantic_accumulator) for item in segments),
            ),
            ordered_original_causal_accumulator=context_fingerprint(
                "runtime-observation-coverage-ordered-causal:v1",
                tuple((item.count, item.causal_accumulator) for item in segments),
            ),
            transitive_coverage_root_fingerprint=context_fingerprint(
                "runtime-observation-transitive-coverage-root:v1",
                tuple((item.count, item.coverage_root) for item in segments),
            ),
            coverage_contract_fingerprint=OBSERVATION_COVERAGE_CONTRACT_FINGERPRINT,
        )
        if (
            payload.covered_original_observation_count
            != expected.transitive_original_observation_count
            or payload.coverage_semantic_fingerprint
            != expected.coverage_semantic_fingerprint
            or payload.ordered_original_semantic_accumulator
            != expected.ordered_original_semantic_accumulator
            or payload.ordered_original_causal_accumulator
            != expected.ordered_original_causal_accumulator
            or payload.transitive_coverage_root_fingerprint
            != expected.transitive_coverage_root_fingerprint
        ):
            raise ValueError("runtime observation rewrite coverage drifted")
        source_index += direct_count
        resulting_index += 1
    old_ids = {
        item.wire_semantic.observation_semantic_id for item in source_observations
    }
    if any(
        item.wire_semantic.observation_semantic_id in old_ids
        for item in resulting_observations[resulting_index:]
    ):
        raise ValueError("runtime observation rewrite duplicated an old unit")


def _prepare_merkle_tree(
    *,
    namespace,
    records,
    semantic_fingerprints,
    causal_keys,
    artifact_namespace,
    policy,
    contract_fingerprint=OBSERVATION_SET_CONTRACT_FINGERPRINT,
    leaf_metadata_kind="runtime_observation_projection_set_leaf",
    internal_metadata_kind="runtime_observation_projection_set_internal",
    leaf_schema_version="runtime_observation_projection_set_leaf.v1",
    internal_schema_version="runtime_observation_projection_set_internal.v1",
):
    if not records:
        return None, ()
    if not (len(records) == len(semantic_fingerprints) == len(causal_keys)):
        raise ValueError("runtime observation set vector lengths differ")
    chunks = _bounded_chunks(
        records,
        policy=policy,
        leaf_schema_version=leaf_schema_version,
    )
    artifacts: list[PreparedProviderInputArtifact] = []
    leaves = []
    offset = 0
    for leaf_index, chunk in enumerate(chunks):
        count = len(chunk)
        leaf_semantics = semantic_fingerprints[offset : offset + count]
        leaf_keys = causal_keys[offset : offset + count]
        artifact = prepared_json_artifact(
            f"{namespace}-leaf",
            {
                "schema_version": leaf_schema_version,
                "leaf_index": leaf_index,
                "records": chunk,
            },
            artifact_namespace=artifact_namespace,
            contract_fingerprint=contract_fingerprint,
            metadata_kind=leaf_metadata_kind,
        )
        if artifact.artifact_reference.content_bytes > policy.leaf_max_canonical_bytes:
            raise ValueError("runtime observation leaf exceeds canonical byte bound")
        artifacts.append(artifact)
        leaves.append(
            build_frozen_fact(
                RuntimeObservationProjectionSetNodeReferenceFact,
                schema_version=(
                    "runtime_observation_projection_set_node_reference.v1"
                ),
                node_kind="leaf",
                height=1,
                member_count=count,
                first_causal_key=leaf_keys[0],
                last_causal_key=leaf_keys[-1],
                ordered_semantic_accumulator=context_fingerprint(
                    "runtime-observation-set-node-semantic:v1", leaf_semantics
                ),
                ordered_causal_accumulator=context_fingerprint(
                    "runtime-observation-set-node-causal:v1", leaf_keys
                ),
                artifact_reference=artifact.artifact_reference,
            )
        )
        offset += count
    level = tuple(leaves)
    height = 1
    while len(level) > 1:
        height += 1
        if height > policy.maximum_tree_height:
            raise ValueError("runtime observation set exceeds tree height bound")
        next_level = []
        for group_index in range(0, len(level), policy.internal_max_fanout):
            children = level[
                group_index : group_index + policy.internal_max_fanout
            ]
            artifact = prepared_json_artifact(
                f"{namespace}-internal",
                {
                    "schema_version": internal_schema_version,
                    "height": height,
                    "children": tuple(
                        item.model_dump(mode="json") for item in children
                    ),
                },
                artifact_namespace=artifact_namespace,
                contract_fingerprint=contract_fingerprint,
                metadata_kind=internal_metadata_kind,
            )
            if artifact.artifact_reference.content_bytes > (
                policy.leaf_max_canonical_bytes
            ):
                raise ValueError(
                    "runtime observation internal node exceeds canonical byte bound"
                )
            artifacts.append(artifact)
            next_level.append(
                build_frozen_fact(
                    RuntimeObservationProjectionSetNodeReferenceFact,
                    schema_version=(
                        "runtime_observation_projection_set_node_reference.v1"
                    ),
                    node_kind="internal",
                    height=height,
                    member_count=sum(item.member_count for item in children),
                    first_causal_key=children[0].first_causal_key,
                    last_causal_key=children[-1].last_causal_key,
                    ordered_semantic_accumulator=context_fingerprint(
                        "runtime-observation-set-node-semantic:v1",
                        tuple(
                            (item.member_count, item.ordered_semantic_accumulator)
                            for item in children
                        ),
                    ),
                    ordered_causal_accumulator=context_fingerprint(
                        "runtime-observation-set-node-causal:v1",
                        tuple(
                            (item.member_count, item.ordered_causal_accumulator)
                            for item in children
                        ),
                    ),
                    artifact_reference=artifact.artifact_reference,
                )
            )
        level = tuple(next_level)
    if len(artifacts) > policy.maximum_changed_nodes_per_rewrite:
        raise ValueError("runtime observation rewrite exceeds changed-node bound")
    return level[0], tuple(artifacts)


def _bounded_chunks(records, *, policy, leaf_schema_version):
    chunks = []
    current = []
    for record in records:
        prospective = (*current, record)
        leaf_index = len(chunks)
        encoded_bytes = len(
            canonical_json_bytes(
                {
                    "schema_version": leaf_schema_version,
                    "leaf_index": leaf_index,
                    "records": prospective,
                }
            )
        )
        if current and (
            len(prospective) > policy.leaf_max_entries
            or encoded_bytes > policy.leaf_max_canonical_bytes
        ):
            chunks.append(tuple(current))
            current = [record]
            encoded_single = len(
                canonical_json_bytes(
                    {
                        "schema_version": leaf_schema_version,
                        "leaf_index": len(chunks),
                        "records": tuple(current),
                    }
                )
            )
            if encoded_single > policy.leaf_max_canonical_bytes:
                raise ValueError("one runtime observation exceeds leaf byte bound")
        else:
            current.append(record)
            if (
                len(current) > policy.leaf_max_entries
                or encoded_bytes > policy.leaf_max_canonical_bytes
            ):
                raise ValueError("one runtime observation exceeds leaf bound")
    if current:
        chunks.append(tuple(current))
    return tuple(chunks)


def _validate_observation_partition(
    *,
    active,
    protected,
    retained,
    rewritten,
    eligible,
    open_lifecycle,
    pending_dependency,
    effective_ids,
) -> None:
    def identities(values):
        result = tuple(
            item.wire_semantic.observation_semantic_id for item in values
        )
        if len(result) != len(set(result)):
            raise ValueError("runtime observation partition contains duplicates")
        return result

    active_ids = identities(active)
    protected_ids = identities(protected)
    retained_ids = identities(retained)
    rewritten_ids = identities(rewritten)
    eligible_ids = identities(eligible)
    open_ids = identities(open_lifecycle)
    pending_ids = identities(pending_dependency)
    partitions = (set(protected_ids), set(retained_ids), set(rewritten_ids))
    if any(partitions[left] & partitions[right] for left, right in ((0, 1), (0, 2), (1, 2))):
        raise ValueError("runtime observation partition sets overlap")
    if set(active_ids) != set().union(*partitions):
        raise ValueError("runtime observation partition does not cover active state")
    if not set(rewritten_ids).issubset(eligible_ids):
        raise ValueError("runtime observation rewritten set exceeds eligible state")
    if not set(open_ids).issubset(protected_ids):
        raise ValueError("open lifecycle observation is not protected")
    if not set(pending_ids).issubset(protected_ids):
        raise ValueError("pending observation dependency is not protected")
    if not set(effective_ids).issubset(active_ids):
        raise ValueError("effective observation head is absent from active state")


def _causal_key(observation) -> str:
    placement = observation.causal_placement
    predecessor = placement.stable_predecessor_transcript_node
    return context_fingerprint(
        "runtime-observation-causal-key:v1",
        (
            predecessor.node_identity_fingerprint if predecessor else None,
            placement.placement_phase,
            placement.causal_scope_kind,
            placement.causal_scope_semantic_id,
            placement.intra_boundary_order,
            observation.wire_semantic.observation_semantic_id,
        ),
    )


def _unique_artifacts(
    artifacts: Iterable[PreparedProviderInputArtifact],
) -> tuple[PreparedProviderInputArtifact, ...]:
    by_id: dict[str, PreparedProviderInputArtifact] = {}
    for artifact in artifacts:
        key = artifact.artifact_reference.artifact_id
        existing = by_id.get(key)
        if existing is not None and existing != artifact:
            raise ValueError("runtime observation artifact identity conflict")
        by_id[key] = artifact
    return tuple(by_id[key] for key in sorted(by_id))


__all__ = [
    "RuntimeObservationLifecycleReducerState",
    "RuntimeObservationLifecycleSnapshot",
    "PreparedRuntimeObservationRewritePlan",
    "advance_runtime_observation_lifecycle_state",
    "classify_runtime_observation_lifecycle",
    "default_runtime_observation_physical_policy",
    "prepare_runtime_observation_effective_head_set",
    "prepare_runtime_observation_rewrite",
    "validate_runtime_observation_rewrite_transition",
]
