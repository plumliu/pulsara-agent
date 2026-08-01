"""Immutable presentation roots and exact mutable-checkpoint installation."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from hashlib import sha256
from typing import Literal

from pulsara_agent.event_log.protocol import EventLog
from pulsara_agent.memory.foundation.protocols import ArtifactStore
from pulsara_agent.primitives._context_base import canonical_json_bytes
from pulsara_agent.primitives.context import context_fingerprint
from pulsara_agent.primitives.frozen import build_frozen_fact
from pulsara_agent.primitives.presentation_history import (
    PresentationHistoryCheckpointCandidateCutFact,
    PresentationHistoryCheckpointStableCandidateFact,
    PresentationHistoryProjectionCheckpointFact,
    PresentationHistoryProjectionRootFact,
    PresentationHistoryProjectionRootReferenceFact,
    PresentationHistoryRootIdentityFact,
    PresentationHistorySourcePrefixTransitionProofFact,
    PresentationHistoryTailFoldSegmentFact,
    PresentationHistoryTreeNodeReferenceFact,
    PresentationHistoryMaterializationPolicyFact,
)
from pulsara_agent.primitives.presentation_checkpoint_storage import (
    PresentationHistoryCapacityCheckpointFact,
    PresentationHistorySpineAccelerationFact,
)
from pulsara_agent.primitives.storage_frozen import build_frozen_storage_fact
from pulsara_agent.primitives.stored_event import RawRuntimeProjectionCheckpoint
from pulsara_agent.runtime.authority_materialization.transcript_reducer import (
    TRANSCRIPT_EVENT_REGISTRY_CONTRACT_FINGERPRINT,
    TRANSCRIPT_PROJECTION_REDUCER_CONTRACT_FINGERPRINT,
)
from pulsara_agent.runtime.terminal_presentation.history_tree import (
    PreparedPresentationHistoryArtifact,
    PresentationHistoryPersistentTree,
)
from pulsara_agent.runtime.terminal_presentation.policy import (
    PresentationAuditExtractorBinding,
    PresentationPurposePolicyRegistry,
)
from pulsara_agent.runtime.terminal_presentation.projection import (
    PresentationProjectionSnapshot,
)


PRESENTATION_HISTORY_PROJECTION_KIND = "terminal_presentation_history.v1"
PRESENTATION_HISTORY_PROJECTION_SCHEMA_VERSION = (
    "terminal_presentation_history_checkpoint.v2"
)
PRESENTATION_HISTORY_ROOT_MEDIA_TYPE = (
    "application/vnd.pulsara.presentation-history-root+json;version=1"
)
PRESENTATION_HISTORY_ROOT_CODEC_ID = "pulsara.presentation-history-root-json"
PRESENTATION_HISTORY_ROOT_CODEC_VERSION = "1"
PRESENTATION_HISTORY_ROOT_CODEC_CONTRACT_FINGERPRINT = context_fingerprint(
    "presentation-history-root-codec-contract:v1",
    "canonical-json-utf8+registered-frozen-fact",
)
PRESENTATION_HISTORY_PROJECTION_ID = "pulsara.terminal-presentation-history"
PRESENTATION_HISTORY_PROJECTION_VERSION = "1"
PRESENTATION_HISTORY_PROJECTION_CONTRACT_FINGERPRINT = context_fingerprint(
    "presentation-history-projection-contract:v1",
    "canonical-transcript-spine+registered-durable-audit+stable-placement+path-copy-tree",
)
EMPTY_PRESENTATION_SOURCE_PREFIX_ACCUMULATOR = context_fingerprint(
    "presentation-history-source-prefix:v1", ()
)
EMPTY_PRESENTATION_ENTRY_ACCUMULATOR = context_fingerprint(
    "presentation-history-ordered-entries:v1", ()
)
EMPTY_PRESENTATION_SPINE_FINGERPRINT = context_fingerprint(
    "presentation-history-canonical-spine:v1", ()
)


@dataclass(frozen=True, slots=True)
class PreparedPresentationHistoryRootArtifact:
    reference: PresentationHistoryProjectionRootReferenceFact
    canonical_bytes: bytes
    semantic_metadata: dict[str, object]
    spine_acceleration: PresentationHistorySpineAccelerationFact


CheckpointCommitDisposition = Literal[
    "full", "none", "unknown", "conflict", "superseded_rebuild_required"
]


def _checkpoint_confirmation_payload(
    *,
    disposition: CheckpointCommitDisposition,
    candidate_fingerprint: str,
    installed_checkpoint: PresentationHistoryProjectionCheckpointFact | None,
    installed_root_identity: PresentationHistoryRootIdentityFact | None,
    confirmation_kind: str,
) -> dict[str, object]:
    return {
        "disposition": disposition,
        "candidate_fingerprint": candidate_fingerprint,
        "installed_checkpoint_fingerprint": (
            installed_checkpoint.checkpoint_fingerprint
            if installed_checkpoint is not None
            else None
        ),
        "installed_root_identity_fingerprint": (
            installed_root_identity.root_identity_fingerprint
            if installed_root_identity is not None
            else None
        ),
        "confirmation_kind": confirmation_kind,
    }


@dataclass(frozen=True, slots=True)
class PresentationHistoryCheckpointCommitReceipt:
    disposition: CheckpointCommitDisposition
    candidate_fingerprint: str
    installed_checkpoint: PresentationHistoryProjectionCheckpointFact | None
    installed_root_identity: PresentationHistoryRootIdentityFact | None
    confirmation_kind: Literal[
        "exact_candidate",
        "identical_concurrent_winner",
        "compatible_successor",
        "predecessor_unchanged",
        "unavailable",
        "conflict",
        "candidate_superseded",
    ]
    confirmation_fingerprint: str

    def __post_init__(self) -> None:
        expected_confirmation_kinds = {
            "full": {
                "exact_candidate",
                "identical_concurrent_winner",
                "compatible_successor",
            },
            "none": {"predecessor_unchanged"},
            "unknown": {"unavailable"},
            "conflict": {"conflict"},
            "superseded_rebuild_required": {"candidate_superseded"},
        }
        if self.confirmation_kind not in expected_confirmation_kinds[self.disposition]:
            raise ValueError("presentation checkpoint confirmation kind mismatch")
        if (self.disposition == "full") != (
            self.installed_checkpoint is not None
            and self.installed_root_identity is not None
        ):
            raise ValueError("presentation checkpoint FULL carrier mismatch")
        checkpoint = self.installed_checkpoint
        root_identity = self.installed_root_identity
        if checkpoint is not None and root_identity is not None:
            if (
                checkpoint.runtime_session_id != root_identity.runtime_session_id
                or checkpoint.checkpoint_generation
                != root_identity.checkpoint_generation
                or checkpoint.checkpoint_fingerprint
                != root_identity.checkpoint_fingerprint
                or checkpoint.projection_root_reference
                != root_identity.projection_root_reference
                or checkpoint.projection_root_fingerprint
                != root_identity.projection_root_fingerprint
                or checkpoint.through_authority_sequence
                != root_identity.through_authority_sequence
                or checkpoint.presentation_source_segment_count
                != root_identity.presentation_source_segment_count
                or checkpoint.presentation_source_prefix_accumulator
                != root_identity.presentation_source_prefix_accumulator
            ):
                raise ValueError("presentation checkpoint/root receipt join mismatch")
        expected_fingerprint = context_fingerprint(
            "presentation-history-checkpoint-confirmation:v1",
            _checkpoint_confirmation_payload(
                disposition=self.disposition,
                candidate_fingerprint=self.candidate_fingerprint,
                installed_checkpoint=checkpoint,
                installed_root_identity=root_identity,
                confirmation_kind=self.confirmation_kind,
            ),
        )
        if self.confirmation_fingerprint != expected_fingerprint:
            raise ValueError(
                "presentation checkpoint confirmation fingerprint mismatch"
            )


@dataclass(frozen=True, slots=True)
class PreparedPresentationHistoryCheckpointCommitAttempt:
    """Stable physical candidate retained across write/confirmation retries."""

    candidate: PresentationHistoryCheckpointStableCandidateFact
    tree_artifacts: tuple[PreparedPresentationHistoryArtifact, ...]
    root_artifact: PreparedPresentationHistoryRootArtifact
    capacity_checkpoint: PresentationHistoryCapacityCheckpointFact
    raw_candidate: RawRuntimeProjectionCheckpoint
    commit_candidate_fingerprint: str
    attempt_fingerprint: str

    def __post_init__(self) -> None:
        expected = context_fingerprint(
            "presentation-history-checkpoint-commit-attempt:v1",
            {
                "stable_candidate_fingerprint": (
                    self.candidate.stable_candidate_fingerprint
                ),
                "capacity_checkpoint_fingerprint": (
                    self.capacity_checkpoint.capacity_checkpoint_fingerprint
                ),
                "raw_candidate_fingerprint": self.raw_candidate.payload_fingerprint,
                "ordered_required_artifact_ids": (
                    self.candidate.ordered_required_artifact_ids
                ),
            },
        )
        if self.attempt_fingerprint != expected:
            raise ValueError("presentation checkpoint attempt fingerprint mismatch")


class PresentationHistoryCheckpointError(RuntimeError):
    pass


class PresentationHistoryProjectionCheckpointOwner:
    """Builds stable roots and classifies every mutable checkpoint write."""

    def __init__(
        self,
        *,
        runtime_session_id: str,
        event_log: EventLog,
        archive: ArtifactStore,
        materialization_policy: PresentationHistoryMaterializationPolicyFact,
        purpose_policy: PresentationPurposePolicyRegistry,
        audit_extractor: PresentationAuditExtractorBinding,
    ) -> None:
        self.runtime_session_id = runtime_session_id
        self.event_log = event_log
        self.archive = archive
        self.policy = materialization_policy
        self.purpose_policy = purpose_policy
        self.audit_extractor = audit_extractor
        self.tree = PresentationHistoryPersistentTree(
            runtime_session_id=runtime_session_id,
            archive=archive,
            contract=materialization_policy.tree_contract,
        )

    def read_checkpoint(
        self, *, deadline_monotonic: float | None = None
    ) -> PresentationHistoryProjectionCheckpointFact | None:
        raw = self.event_log.read_runtime_projection_checkpoint(
            PRESENTATION_HISTORY_PROJECTION_KIND,
            deadline_monotonic=deadline_monotonic,
        )
        if raw is None:
            return None
        self._validate_raw_checkpoint(
            raw,
            deadline_monotonic=deadline_monotonic,
        )
        return PresentationHistoryProjectionCheckpointFact.model_validate(
            raw.state_payload["checkpoint"]
        )

    def ensure_genesis(
        self, *, deadline_monotonic: float | None = None
    ) -> PresentationHistoryCheckpointCommitReceipt:
        existing = self.read_checkpoint(deadline_monotonic=deadline_monotonic)
        if existing is not None:
            root_identity = self.materialize_root_identity(
                existing, deadline_monotonic=deadline_monotonic
            )
            return _checkpoint_receipt(
                disposition="full",
                candidate_fingerprint=existing.checkpoint_fingerprint,
                installed_checkpoint=existing,
                installed_root_identity=root_identity,
                confirmation_kind="identical_concurrent_winner",
            )
        root = self._build_root(
            projection_generation=0,
            through_sequence=0,
            segment_count=0,
            source_prefix_accumulator=EMPTY_PRESENTATION_SOURCE_PREFIX_ACCUMULATOR,
            source_prefix_transition_proof=None,
            previous_root_reference=None,
            tree_root_reference=None,
            tree_height=0,
            canonical_spine_fingerprint=EMPTY_PRESENTATION_SPINE_FINGERPRINT,
            ordered_entry_accumulator=EMPTY_PRESENTATION_ENTRY_ACCUMULATOR,
        )
        prepared_root = _prepare_root_artifact(
            root,
            spine_acceleration=_empty_spine_acceleration(
                runtime_session_id=self.runtime_session_id,
                placement_contract=self.policy.tree_contract.placement_key_contract,
            ),
        )
        self._persist_artifacts((prepared_root,), deadline_monotonic=deadline_monotonic)
        checkpoint = build_frozen_fact(
            PresentationHistoryProjectionCheckpointFact,
            schema_version="presentation_history_projection_checkpoint.v1",
            runtime_session_id=self.runtime_session_id,
            checkpoint_kind="terminal_presentation_history",
            checkpoint_generation=0,
            previous_checkpoint_fingerprint=None,
            through_authority_sequence=0,
            presentation_source_segment_count=0,
            presentation_source_prefix_accumulator=(
                EMPTY_PRESENTATION_SOURCE_PREFIX_ACCUMULATOR
            ),
            projection_revision=0,
            projection_root_reference=prepared_root.reference,
            projection_root_fingerprint=root.projection_root_fingerprint,
        )
        raw = self._raw_checkpoint(
            checkpoint=checkpoint,
            spine_acceleration=prepared_root.spine_acceleration,
            capacity_checkpoint=_empty_capacity_checkpoint(
                runtime_session_id=self.runtime_session_id,
                quote_policy_fingerprint=(
                    self.policy.growth_quote_policy.quote_policy_fingerprint
                ),
            ),
            validation_base_through_sequence=0,
            validation_base_state_payload={},
            deadline_monotonic=deadline_monotonic,
        )
        try:
            self.event_log.write_runtime_projection_checkpoint(
                raw, deadline_monotonic=deadline_monotonic
            )
        except BaseException:
            return self._confirm_raw_candidate(
                raw,
                candidate_fingerprint=checkpoint.checkpoint_fingerprint,
                deadline_monotonic=deadline_monotonic,
            )
        return self._confirm_raw_candidate(
            raw,
            candidate_fingerprint=checkpoint.checkpoint_fingerprint,
            deadline_monotonic=deadline_monotonic,
        )

    def prepare_candidate(
        self,
        *,
        snapshot: PresentationProjectionSnapshot,
        predecessor: PresentationHistoryProjectionCheckpointFact,
        deadline_monotonic: float | None = None,
    ) -> tuple[
        PresentationHistoryCheckpointStableCandidateFact,
        tuple[PreparedPresentationHistoryArtifact, ...],
        PreparedPresentationHistoryRootArtifact,
    ]:
        if snapshot.runtime_session_id != self.runtime_session_id:
            raise PresentationHistoryCheckpointError(
                "presentation checkpoint snapshot crosses sessions"
            )
        if snapshot.through_authority_sequence < predecessor.through_authority_sequence:
            raise PresentationHistoryCheckpointError(
                "presentation checkpoint snapshot moved behind predecessor"
            )
        previous_root = self._read_root(
            predecessor.projection_root_reference,
            deadline_monotonic=deadline_monotonic,
        )
        segments = tuple(
            item
            for item in snapshot.ordered_tail_segments
            if item.through_sequence > predecessor.through_authority_sequence
            and item.through_sequence <= snapshot.through_authority_sequence
        )
        expected_sequences = tuple(
            range(
                predecessor.through_authority_sequence + 1,
                snapshot.through_authority_sequence + 1,
            )
        )
        if tuple(item.through_sequence for item in segments) != expected_sequences:
            raise PresentationHistoryCheckpointError(
                "presentation checkpoint candidate lacks a contiguous segment cut"
            )
        mutations = tuple(
            mutation for segment in segments for mutation in segment.ordered_mutations
        )
        prepared_tree = self.tree.prepare_update(
            base_root_node_reference=previous_root.tree_root_node_reference,
            base_tree_height=previous_root.tree_height,
            ordered_mutations=mutations,
            deadline_monotonic=deadline_monotonic,
        )
        if snapshot.ordered_entries_complete and (
            prepared_tree.resulting_entry_count != len(snapshot.ordered_entries)
            or prepared_tree.resulting_entry_accumulator != snapshot.entry_accumulator
        ):
            raise PresentationHistoryCheckpointError(
                "presentation checkpoint tree does not equal frozen projection state"
            )
        source_range_accumulator = _segment_source_accumulator(segments)
        segment_accumulator = _segment_accumulator(segments)
        mutation_accumulator = _mutation_accumulator(segments)
        resulting_prefix = _extend_source_prefix(
            predecessor.presentation_source_prefix_accumulator, segments
        )
        transition = build_frozen_fact(
            PresentationHistorySourcePrefixTransitionProofFact,
            schema_version="presentation_history_source_prefix_transition.v1",
            predecessor_through_sequence=predecessor.through_authority_sequence,
            predecessor_segment_count=predecessor.presentation_source_segment_count,
            predecessor_prefix_accumulator=(
                predecessor.presentation_source_prefix_accumulator
            ),
            ordered_added_segment_fingerprints=tuple(
                item.segment_fingerprint for item in segments
            ),
            added_segment_count=len(segments),
            resulting_through_sequence=snapshot.through_authority_sequence,
            resulting_segment_count=(
                predecessor.presentation_source_segment_count + len(segments)
            ),
            resulting_prefix_accumulator=resulting_prefix,
        )
        root = self._build_root(
            projection_generation=previous_root.projection_generation + 1,
            through_sequence=snapshot.through_authority_sequence,
            segment_count=transition.resulting_segment_count,
            source_prefix_accumulator=resulting_prefix,
            source_prefix_transition_proof=transition,
            previous_root_reference=predecessor.projection_root_reference,
            tree_root_reference=prepared_tree.resulting_root_node_reference,
            tree_height=prepared_tree.resulting_tree_height,
            canonical_spine_fingerprint=snapshot.canonical_spine_fingerprint,
            ordered_entry_accumulator=prepared_tree.resulting_entry_accumulator,
        )
        # A confirmed-root replacement is itself a client-visible projection
        # transition, including a checkpoint containing only no-op source
        # segments.  Keep this revision in the durable checkpoint acceleration
        # rather than deriving it from the mutable live batch grouping.
        checkpoint_projection_revision = predecessor.projection_revision + 1
        spine_acceleration = build_frozen_storage_fact(
            PresentationHistorySpineAccelerationFact,
            schema_version="presentation_history_spine_acceleration.v1",
            runtime_session_id=snapshot.spine_acceleration.runtime_session_id,
            placement_key_contract_id=(
                snapshot.spine_acceleration.placement_key_contract_id
            ),
            placement_key_contract_version=(
                snapshot.spine_acceleration.placement_key_contract_version
            ),
            placement_key_contract_fingerprint=(
                snapshot.spine_acceleration.placement_key_contract_fingerprint
            ),
            through_authority_sequence=(
                snapshot.spine_acceleration.through_authority_sequence
            ),
            projection_revision=checkpoint_projection_revision,
            canonical_spine_fingerprint=(
                snapshot.spine_acceleration.canonical_spine_fingerprint
            ),
            ordered_entries=snapshot.spine_acceleration.ordered_entries,
        )
        prepared_root = _prepare_root_artifact(
            root,
            spine_acceleration=spine_acceleration,
        )
        checkpoint = build_frozen_fact(
            PresentationHistoryProjectionCheckpointFact,
            schema_version="presentation_history_projection_checkpoint.v1",
            runtime_session_id=self.runtime_session_id,
            checkpoint_kind="terminal_presentation_history",
            checkpoint_generation=predecessor.checkpoint_generation + 1,
            previous_checkpoint_fingerprint=predecessor.checkpoint_fingerprint,
            through_authority_sequence=snapshot.through_authority_sequence,
            presentation_source_segment_count=transition.resulting_segment_count,
            presentation_source_prefix_accumulator=resulting_prefix,
            projection_revision=checkpoint_projection_revision,
            projection_root_reference=prepared_root.reference,
            projection_root_fingerprint=root.projection_root_fingerprint,
        )
        cut = build_frozen_fact(
            PresentationHistoryCheckpointCandidateCutFact,
            schema_version="presentation_history_checkpoint_candidate_cut.v1",
            source_active_head_fingerprint=snapshot.snapshot_fingerprint,
            source_confirmed_root_fingerprint=predecessor.projection_root_fingerprint,
            cut_from_sequence_exclusive=predecessor.through_authority_sequence,
            cut_through_sequence=snapshot.through_authority_sequence,
            ordered_segment_fingerprints=tuple(
                item.segment_fingerprint for item in segments
            ),
            segment_count=len(segments),
            source_range_accumulator=source_range_accumulator,
            segment_accumulator=segment_accumulator,
            mutation_count=len(mutations),
            mutation_accumulator=mutation_accumulator,
            resulting_source_prefix_accumulator=resulting_prefix,
            resulting_resident_entry_accumulator=(
                prepared_tree.resulting_entry_accumulator
            ),
        )
        artifact_ids = tuple(
            sorted(
                {
                    *(
                        item.artifact_id
                        for item in prepared_tree.newly_prepared_artifacts
                    ),
                    prepared_root.reference.root_artifact_id,
                }
            )
        )
        candidate_id_digest = context_fingerprint(
            "presentation-history-checkpoint-candidate-id:v1",
            {
                "runtime_session_id": self.runtime_session_id,
                "predecessor_checkpoint_fingerprint": predecessor.checkpoint_fingerprint,
                "candidate_cut_fingerprint": cut.candidate_cut_fingerprint,
                "resulting_checkpoint_fingerprint": checkpoint.checkpoint_fingerprint,
            },
        )
        candidate = build_frozen_fact(
            PresentationHistoryCheckpointStableCandidateFact,
            schema_version="presentation_history_checkpoint_stable_candidate.v1",
            checkpoint_candidate_id=(
                f"presentation-checkpoint:{candidate_id_digest.removeprefix('sha256:')}"
            ),
            runtime_session_id=self.runtime_session_id,
            expected_predecessor_checkpoint=predecessor,
            candidate_cut=cut,
            resulting_projection_root=root,
            resulting_projection_root_reference=prepared_root.reference,
            resulting_checkpoint=checkpoint,
            ordered_required_artifact_ids=artifact_ids,
        )
        return candidate, prepared_tree.newly_prepared_artifacts, prepared_root

    def commit_candidate(
        self,
        candidate: PresentationHistoryCheckpointStableCandidateFact,
        tree_artifacts: tuple[PreparedPresentationHistoryArtifact, ...],
        root_artifact: PreparedPresentationHistoryRootArtifact,
        capacity_checkpoint: PresentationHistoryCapacityCheckpointFact,
        *,
        deadline_monotonic: float | None = None,
    ) -> PresentationHistoryCheckpointCommitReceipt:
        attempt = self.freeze_commit_attempt(
            candidate,
            tree_artifacts,
            root_artifact,
            capacity_checkpoint,
            deadline_monotonic=deadline_monotonic,
        )
        return self.commit_prepared_attempt(
            attempt, deadline_monotonic=deadline_monotonic
        )

    def freeze_commit_attempt(
        self,
        candidate: PresentationHistoryCheckpointStableCandidateFact,
        tree_artifacts: tuple[PreparedPresentationHistoryArtifact, ...],
        root_artifact: PreparedPresentationHistoryRootArtifact,
        capacity_checkpoint: PresentationHistoryCapacityCheckpointFact,
        *,
        deadline_monotonic: float | None = None,
    ) -> PreparedPresentationHistoryCheckpointCommitAttempt:
        """Freeze one raw CAS candidate before any fallible physical write."""

        if candidate.runtime_session_id != self.runtime_session_id:
            raise PresentationHistoryCheckpointError(
                "presentation checkpoint candidate crosses sessions"
            )
        predecessor_raw = self.event_log.read_runtime_projection_checkpoint(
            PRESENTATION_HISTORY_PROJECTION_KIND,
            deadline_monotonic=deadline_monotonic,
        )
        if predecessor_raw is None:
            raise PresentationHistoryCheckpointError(
                "presentation checkpoint predecessor disappeared"
            )
        expected_predecessor = (
            PresentationHistoryProjectionCheckpointFact.model_validate(
                predecessor_raw.state_payload["checkpoint"]
            )
        )
        if expected_predecessor != candidate.expected_predecessor_checkpoint:
            raise PresentationHistoryCheckpointError(
                "presentation checkpoint predecessor was superseded before freeze"
            )
        if (
            capacity_checkpoint.runtime_session_id != self.runtime_session_id
            or capacity_checkpoint.through_authority_sequence
            != candidate.resulting_checkpoint.through_authority_sequence
            or capacity_checkpoint.quote_policy_fingerprint
            != self.policy.growth_quote_policy.quote_policy_fingerprint
        ):
            raise PresentationHistoryCheckpointError(
                "presentation checkpoint capacity projection mismatch"
            )
        commit_candidate_fingerprint = context_fingerprint(
            "presentation-history-checkpoint-commit-candidate:v2",
            {
                "history_candidate_fingerprint": candidate.stable_candidate_fingerprint,
                "capacity_checkpoint_fingerprint": (
                    capacity_checkpoint.capacity_checkpoint_fingerprint
                ),
            },
        )
        raw = self._raw_checkpoint(
            checkpoint=candidate.resulting_checkpoint,
            spine_acceleration=root_artifact.spine_acceleration,
            capacity_checkpoint=capacity_checkpoint,
            validation_base_through_sequence=predecessor_raw.through_sequence,
            validation_base_state_payload=predecessor_raw.state_payload,
            deadline_monotonic=deadline_monotonic,
        )
        attempt_payload = {
            "stable_candidate_fingerprint": candidate.stable_candidate_fingerprint,
            "capacity_checkpoint_fingerprint": (
                capacity_checkpoint.capacity_checkpoint_fingerprint
            ),
            "raw_candidate_fingerprint": raw.payload_fingerprint,
            "ordered_required_artifact_ids": candidate.ordered_required_artifact_ids,
        }
        return PreparedPresentationHistoryCheckpointCommitAttempt(
            candidate=candidate,
            tree_artifacts=tree_artifacts,
            root_artifact=root_artifact,
            capacity_checkpoint=capacity_checkpoint,
            raw_candidate=raw,
            commit_candidate_fingerprint=commit_candidate_fingerprint,
            attempt_fingerprint=context_fingerprint(
                "presentation-history-checkpoint-commit-attempt:v1", attempt_payload
            ),
        )

    def commit_prepared_attempt(
        self,
        attempt: PreparedPresentationHistoryCheckpointCommitAttempt,
        *,
        deadline_monotonic: float | None = None,
    ) -> PresentationHistoryCheckpointCommitReceipt:
        """Write and confirm the exact same frozen candidate on every retry."""

        attempt.__post_init__()
        self._persist_artifacts(
            attempt.tree_artifacts, deadline_monotonic=deadline_monotonic
        )
        self._persist_artifacts(
            (attempt.root_artifact,), deadline_monotonic=deadline_monotonic
        )
        try:
            self.event_log.write_runtime_projection_checkpoint(
                attempt.raw_candidate, deadline_monotonic=deadline_monotonic
            )
        except BaseException:
            pass
        return self.confirm_prepared_attempt(
            attempt, deadline_monotonic=deadline_monotonic
        )

    def confirm_prepared_attempt(
        self,
        attempt: PreparedPresentationHistoryCheckpointCommitAttempt,
        *,
        deadline_monotonic: float | None = None,
    ) -> PresentationHistoryCheckpointCommitReceipt:
        """Classify one frozen attempt without deriving a replacement candidate."""

        attempt.__post_init__()
        return self._confirm_raw_candidate(
            attempt.raw_candidate,
            candidate_fingerprint=attempt.commit_candidate_fingerprint,
            deadline_monotonic=deadline_monotonic,
        )

    def materialize_root_identity(
        self,
        checkpoint: PresentationHistoryProjectionCheckpointFact,
        *,
        deadline_monotonic: float | None = None,
    ) -> PresentationHistoryRootIdentityFact:
        root = self._read_root(
            checkpoint.projection_root_reference,
            deadline_monotonic=deadline_monotonic,
        )
        if (
            root.projection_root_fingerprint != checkpoint.projection_root_fingerprint
            or root.through_authority_sequence != checkpoint.through_authority_sequence
            or root.presentation_source_segment_count
            != checkpoint.presentation_source_segment_count
            or root.presentation_source_prefix_accumulator
            != checkpoint.presentation_source_prefix_accumulator
        ):
            raise PresentationHistoryCheckpointError(
                "presentation checkpoint/root authority mismatch"
            )
        return build_frozen_fact(
            PresentationHistoryRootIdentityFact,
            schema_version="presentation_history_root_identity.v1",
            runtime_session_id=self.runtime_session_id,
            history_projection_contract_fingerprint=(
                root.history_projection_contract_fingerprint
            ),
            materialization_policy_fingerprint=root.materialization_policy_fingerprint,
            tree_contract_fingerprint=root.tree_contract_fingerprint,
            placement_key_contract_id=root.placement_key_contract_id,
            placement_key_contract_version=root.placement_key_contract_version,
            placement_key_contract_fingerprint=(
                root.placement_key_contract_fingerprint
            ),
            checkpoint_generation=checkpoint.checkpoint_generation,
            checkpoint_fingerprint=checkpoint.checkpoint_fingerprint,
            projection_root_reference=checkpoint.projection_root_reference,
            projection_generation=root.projection_generation,
            projection_root_fingerprint=root.projection_root_fingerprint,
            through_authority_sequence=root.through_authority_sequence,
            presentation_source_segment_count=root.presentation_source_segment_count,
            presentation_source_prefix_accumulator=(
                root.presentation_source_prefix_accumulator
            ),
            presentation_policy_registry_contract_fingerprint=(
                root.presentation_policy_registry_contract_fingerprint
            ),
            audit_extractor_registry_contract_fingerprint=(
                root.audit_extractor_registry_contract_fingerprint
            ),
        )

    def read_root(
        self,
        reference: PresentationHistoryProjectionRootReferenceFact,
        *,
        deadline_monotonic: float | None = None,
    ) -> PresentationHistoryProjectionRootFact:
        return self._read_root(reference, deadline_monotonic=deadline_monotonic)

    def read_spine_acceleration(
        self, *, deadline_monotonic: float | None = None
    ) -> PresentationHistorySpineAccelerationFact | None:
        raw = self.event_log.read_runtime_projection_checkpoint(
            PRESENTATION_HISTORY_PROJECTION_KIND,
            deadline_monotonic=deadline_monotonic,
        )
        if raw is None:
            return None
        self._validate_raw_checkpoint(
            raw,
            deadline_monotonic=deadline_monotonic,
        )
        return PresentationHistorySpineAccelerationFact.model_validate(
            raw.state_payload["spine_acceleration"]
        )

    def read_capacity_checkpoint(
        self, *, deadline_monotonic: float | None = None
    ) -> PresentationHistoryCapacityCheckpointFact | None:
        raw = self.event_log.read_runtime_projection_checkpoint(
            PRESENTATION_HISTORY_PROJECTION_KIND,
            deadline_monotonic=deadline_monotonic,
        )
        if raw is None:
            return None
        self._validate_raw_checkpoint(
            raw,
            deadline_monotonic=deadline_monotonic,
        )
        return PresentationHistoryCapacityCheckpointFact.model_validate(
            raw.state_payload["capacity_checkpoint"]
        )

    def _build_root(
        self,
        *,
        projection_generation: int,
        through_sequence: int,
        segment_count: int,
        source_prefix_accumulator: str,
        source_prefix_transition_proof: PresentationHistorySourcePrefixTransitionProofFact
        | None,
        previous_root_reference: PresentationHistoryProjectionRootReferenceFact | None,
        tree_root_reference: PresentationHistoryTreeNodeReferenceFact | None,
        tree_height: int,
        canonical_spine_fingerprint: str,
        ordered_entry_accumulator: str,
    ) -> PresentationHistoryProjectionRootFact:
        placement = self.policy.tree_contract.placement_key_contract
        return build_frozen_fact(
            PresentationHistoryProjectionRootFact,
            schema_version="presentation_history_projection_root.v1",
            runtime_session_id=self.runtime_session_id,
            root_codec_id=PRESENTATION_HISTORY_ROOT_CODEC_ID,
            root_codec_version=PRESENTATION_HISTORY_ROOT_CODEC_VERSION,
            root_codec_contract_fingerprint=(
                PRESENTATION_HISTORY_ROOT_CODEC_CONTRACT_FINGERPRINT
            ),
            history_projection_id=PRESENTATION_HISTORY_PROJECTION_ID,
            history_projection_version=PRESENTATION_HISTORY_PROJECTION_VERSION,
            history_projection_contract_fingerprint=(
                PRESENTATION_HISTORY_PROJECTION_CONTRACT_FINGERPRINT
            ),
            materialization_policy_fingerprint=self.policy.policy_fingerprint,
            tree_contract_fingerprint=self.policy.tree_contract.tree_contract_fingerprint,
            placement_key_contract_id=placement.placement_key_contract_id,
            placement_key_contract_version=placement.placement_key_contract_version,
            placement_key_contract_fingerprint=placement.contract_fingerprint,
            canonical_transcript_reducer_contract_fingerprint=(
                TRANSCRIPT_PROJECTION_REDUCER_CONTRACT_FINGERPRINT
            ),
            event_domain_registry_contract_fingerprint=(
                TRANSCRIPT_EVENT_REGISTRY_CONTRACT_FINGERPRINT
            ),
            presentation_policy_registry_contract_fingerprint=(
                self.purpose_policy.contract.registry_fingerprint
            ),
            audit_extractor_registry_contract_fingerprint=(
                self.audit_extractor.contract.contract_fingerprint
            ),
            projection_generation=projection_generation,
            through_authority_sequence=through_sequence,
            presentation_source_segment_count=segment_count,
            presentation_source_prefix_accumulator=source_prefix_accumulator,
            source_prefix_transition_proof=source_prefix_transition_proof,
            previous_projection_root_reference=previous_root_reference,
            root_kind=("empty" if tree_root_reference is None else "non_empty"),
            tree_root_node_reference=tree_root_reference,
            tree_height=tree_height,
            entry_count=(
                0
                if tree_root_reference is None
                else tree_root_reference.subtree_entry_count
            ),
            first_placement_key=(
                None
                if tree_root_reference is None
                else tree_root_reference.first_placement_key
            ),
            last_placement_key=(
                None
                if tree_root_reference is None
                else tree_root_reference.last_placement_key
            ),
            canonical_transcript_spine_fingerprint=canonical_spine_fingerprint,
            ordered_history_entry_accumulator=ordered_entry_accumulator,
        )

    def _raw_checkpoint(
        self,
        *,
        checkpoint: PresentationHistoryProjectionCheckpointFact,
        spine_acceleration: PresentationHistorySpineAccelerationFact,
        capacity_checkpoint: PresentationHistoryCapacityCheckpointFact,
        validation_base_through_sequence: int,
        validation_base_state_payload: dict[str, object],
        deadline_monotonic: float | None,
    ) -> RawRuntimeProjectionCheckpoint:
        ledger_prefix = self.event_log.read_raw_ledger_prefix(
            through_sequence=checkpoint.through_authority_sequence,
            deadline_monotonic=deadline_monotonic,
        )
        if (
            spine_acceleration.runtime_session_id != self.runtime_session_id
            or spine_acceleration.through_authority_sequence
            != checkpoint.through_authority_sequence
            or spine_acceleration.projection_revision != checkpoint.projection_revision
        ):
            raise PresentationHistoryCheckpointError(
                "presentation checkpoint/spine acceleration mismatch"
            )
        if (
            capacity_checkpoint.runtime_session_id != self.runtime_session_id
            or capacity_checkpoint.through_authority_sequence
            != checkpoint.through_authority_sequence
            or capacity_checkpoint.quote_policy_fingerprint
            != self.policy.growth_quote_policy.quote_policy_fingerprint
        ):
            raise PresentationHistoryCheckpointError(
                "presentation checkpoint/capacity acceleration mismatch"
            )
        state_payload = {
            "checkpoint": checkpoint.model_dump(mode="json"),
            "spine_acceleration": spine_acceleration.model_dump(mode="json"),
            "capacity_checkpoint": capacity_checkpoint.model_dump(mode="json"),
        }
        payload_fingerprint = context_fingerprint(
            "presentation-history-runtime-checkpoint-row:v1",
            {
                "projection_kind": PRESENTATION_HISTORY_PROJECTION_KIND,
                "through_sequence": checkpoint.through_authority_sequence,
                "projection_schema_version": (
                    PRESENTATION_HISTORY_PROJECTION_SCHEMA_VERSION
                ),
                "ledger_prefix": asdict(ledger_prefix),
                "validation_base_through_sequence": validation_base_through_sequence,
                "validation_base_state_payload": validation_base_state_payload,
                "state_payload": state_payload,
            },
        )
        return RawRuntimeProjectionCheckpoint(
            projection_kind=PRESENTATION_HISTORY_PROJECTION_KIND,
            through_sequence=checkpoint.through_authority_sequence,
            projection_schema_version=PRESENTATION_HISTORY_PROJECTION_SCHEMA_VERSION,
            ledger_prefix=ledger_prefix,
            validation_base_through_sequence=validation_base_through_sequence,
            validation_base_state_payload=validation_base_state_payload,
            state_payload=state_payload,
            payload_fingerprint=payload_fingerprint,
        )

    def _confirm_raw_candidate(
        self,
        candidate: RawRuntimeProjectionCheckpoint,
        *,
        candidate_fingerprint: str,
        deadline_monotonic: float | None,
    ) -> PresentationHistoryCheckpointCommitReceipt:
        try:
            observed = self.event_log.read_runtime_projection_checkpoint(
                PRESENTATION_HISTORY_PROJECTION_KIND,
                deadline_monotonic=deadline_monotonic,
            )
        except BaseException:
            return _checkpoint_receipt(
                disposition="unknown",
                candidate_fingerprint=candidate_fingerprint,
                installed_checkpoint=None,
                installed_root_identity=None,
                confirmation_kind="unavailable",
            )
        if observed == candidate:
            checkpoint = PresentationHistoryProjectionCheckpointFact.model_validate(
                observed.state_payload["checkpoint"]
            )
            try:
                root_identity = self.materialize_root_identity(
                    checkpoint, deadline_monotonic=deadline_monotonic
                )
            except BaseException:
                return _checkpoint_receipt(
                    disposition="unknown",
                    candidate_fingerprint=candidate_fingerprint,
                    installed_checkpoint=None,
                    installed_root_identity=None,
                    confirmation_kind="unavailable",
                )
            return _checkpoint_receipt(
                disposition="full",
                candidate_fingerprint=candidate_fingerprint,
                installed_checkpoint=checkpoint,
                installed_root_identity=root_identity,
                confirmation_kind="exact_candidate",
            )
        if (
            observed is not None
            and observed.projection_schema_version
            == candidate.projection_schema_version
            and observed.validation_base_through_sequence == candidate.through_sequence
            and observed.validation_base_state_payload == candidate.state_payload
        ):
            checkpoint = PresentationHistoryProjectionCheckpointFact.model_validate(
                observed.state_payload["checkpoint"]
            )
            try:
                root_identity = self.materialize_root_identity(
                    checkpoint, deadline_monotonic=deadline_monotonic
                )
            except BaseException:
                return _checkpoint_receipt(
                    disposition="unknown",
                    candidate_fingerprint=candidate_fingerprint,
                    installed_checkpoint=None,
                    installed_root_identity=None,
                    confirmation_kind="unavailable",
                )
            return _checkpoint_receipt(
                disposition="full",
                candidate_fingerprint=candidate_fingerprint,
                installed_checkpoint=checkpoint,
                installed_root_identity=root_identity,
                confirmation_kind="compatible_successor",
            )
        if (
            observed is not None
            and observed.through_sequence == candidate.validation_base_through_sequence
            and observed.state_payload == candidate.validation_base_state_payload
        ):
            return _checkpoint_receipt(
                disposition="none",
                candidate_fingerprint=candidate_fingerprint,
                installed_checkpoint=None,
                installed_root_identity=None,
                confirmation_kind="predecessor_unchanged",
            )
        return _checkpoint_receipt(
            disposition=("unknown" if observed is None else "conflict"),
            candidate_fingerprint=candidate_fingerprint,
            installed_checkpoint=None,
            installed_root_identity=None,
            confirmation_kind=("unavailable" if observed is None else "conflict"),
        )

    def _validate_raw_checkpoint(
        self,
        raw: RawRuntimeProjectionCheckpoint,
        *,
        deadline_monotonic: float | None,
    ) -> None:
        if (
            raw.projection_kind != PRESENTATION_HISTORY_PROJECTION_KIND
            or raw.projection_schema_version
            != PRESENTATION_HISTORY_PROJECTION_SCHEMA_VERSION
        ):
            raise PresentationHistoryCheckpointError(
                "presentation history checkpoint binding mismatch"
            )
        checkpoint = PresentationHistoryProjectionCheckpointFact.model_validate(
            raw.state_payload["checkpoint"]
        )
        acceleration = PresentationHistorySpineAccelerationFact.model_validate(
            raw.state_payload["spine_acceleration"]
        )
        capacity = PresentationHistoryCapacityCheckpointFact.model_validate(
            raw.state_payload["capacity_checkpoint"]
        )
        expected = self._raw_checkpoint(
            checkpoint=checkpoint,
            spine_acceleration=acceleration,
            capacity_checkpoint=capacity,
            validation_base_through_sequence=raw.validation_base_through_sequence,
            validation_base_state_payload=raw.validation_base_state_payload,
            deadline_monotonic=None,
        )
        if expected != raw:
            raise PresentationHistoryCheckpointError(
                "presentation history checkpoint row fingerprint mismatch"
            )
        root = self._read_root(
            checkpoint.projection_root_reference,
            deadline_monotonic=deadline_monotonic,
        )
        if (
            acceleration.runtime_session_id != self.runtime_session_id
            or acceleration.through_authority_sequence
            != checkpoint.through_authority_sequence
            or acceleration.projection_revision != checkpoint.projection_revision
            or acceleration.canonical_spine_fingerprint
            != root.canonical_transcript_spine_fingerprint
            or acceleration.placement_key_contract_id != root.placement_key_contract_id
            or acceleration.placement_key_contract_version
            != root.placement_key_contract_version
            or acceleration.placement_key_contract_fingerprint
            != root.placement_key_contract_fingerprint
        ):
            raise PresentationHistoryCheckpointError(
                "presentation checkpoint spine acceleration join mismatch"
            )

    def _read_root(
        self,
        reference: PresentationHistoryProjectionRootReferenceFact,
        *,
        deadline_monotonic: float | None,
    ) -> PresentationHistoryProjectionRootFact:
        text = self.archive.get_text(
            reference.root_artifact_id,
            session_id=self.runtime_session_id,
            deadline_monotonic=deadline_monotonic,
        )
        encoded = text.encode("utf-8")
        if (
            len(encoded) != reference.root_byte_count
            or f"sha256:{sha256(encoded).hexdigest()}" != reference.root_sha256
        ):
            raise PresentationHistoryCheckpointError(
                "presentation root artifact identity mismatch"
            )
        root = PresentationHistoryProjectionRootFact.model_validate(json.loads(text))
        if (
            root.projection_root_fingerprint != reference.projection_root_fingerprint
            or root.materialization_policy_fingerprint
            != reference.materialization_policy_fingerprint
            or root.tree_contract_fingerprint != reference.tree_contract_fingerprint
        ):
            raise PresentationHistoryCheckpointError(
                "presentation root/reference contract mismatch"
            )
        return root

    def _persist_artifacts(
        self,
        artifacts: tuple[
            PreparedPresentationHistoryArtifact
            | PreparedPresentationHistoryRootArtifact,
            ...,
        ],
        *,
        deadline_monotonic: float | None,
    ) -> None:
        for artifact in artifacts:
            if isinstance(artifact, PreparedPresentationHistoryRootArtifact):
                artifact_id = artifact.reference.root_artifact_id
                encoded = artifact.canonical_bytes
                media_type = PRESENTATION_HISTORY_ROOT_MEDIA_TYPE
                metadata = artifact.semantic_metadata
            else:
                artifact_id = artifact.artifact_id
                encoded = artifact.canonical_bytes
                media_type = artifact.media_type
                metadata = artifact.semantic_metadata
            self.archive.put_text_if_absent_or_confirm_identical(
                artifact_id,
                encoded.decode("utf-8"),
                session_id=self.runtime_session_id,
                run_id=None,
                media_type=media_type,
                semantic_metadata=metadata,
                deadline_monotonic=deadline_monotonic,
            )


def _prepare_root_artifact(
    root: PresentationHistoryProjectionRootFact,
    *,
    spine_acceleration: PresentationHistorySpineAccelerationFact,
) -> PreparedPresentationHistoryRootArtifact:
    if (
        spine_acceleration.runtime_session_id != root.runtime_session_id
        or spine_acceleration.through_authority_sequence
        != root.through_authority_sequence
        or spine_acceleration.canonical_spine_fingerprint
        != root.canonical_transcript_spine_fingerprint
    ):
        raise PresentationHistoryCheckpointError(
            "presentation root/spine acceleration mismatch"
        )
    encoded = canonical_json_bytes(root.model_dump(mode="json"))
    digest = f"sha256:{sha256(encoded).hexdigest()}"
    artifact_digest = context_fingerprint(
        "presentation-history-root-artifact:v1", root.projection_root_fingerprint
    )
    reference = build_frozen_fact(
        PresentationHistoryProjectionRootReferenceFact,
        schema_version="presentation_history_projection_root_reference.v1",
        root_kind=root.root_kind,
        root_artifact_id=(
            f"artifact:presentation-history-root:{artifact_digest.removeprefix('sha256:')}"
        ),
        root_sha256=digest,
        root_byte_count=len(encoded),
        projection_root_fingerprint=root.projection_root_fingerprint,
        materialization_policy_fingerprint=root.materialization_policy_fingerprint,
        tree_contract_fingerprint=root.tree_contract_fingerprint,
    )
    return PreparedPresentationHistoryRootArtifact(
        reference=reference,
        canonical_bytes=encoded,
        semantic_metadata={
            "artifact_kind": "presentation_history_projection_root",
            "projection_root_fingerprint": root.projection_root_fingerprint,
            "materialization_policy_fingerprint": (
                root.materialization_policy_fingerprint
            ),
            "tree_contract_fingerprint": root.tree_contract_fingerprint,
        },
        spine_acceleration=spine_acceleration,
    )


def _empty_spine_acceleration(
    *,
    runtime_session_id: str,
    placement_contract,
) -> PresentationHistorySpineAccelerationFact:
    return build_frozen_storage_fact(
        PresentationHistorySpineAccelerationFact,
        schema_version="presentation_history_spine_acceleration.v1",
        runtime_session_id=runtime_session_id,
        placement_key_contract_id=placement_contract.placement_key_contract_id,
        placement_key_contract_version=(
            placement_contract.placement_key_contract_version
        ),
        placement_key_contract_fingerprint=placement_contract.contract_fingerprint,
        through_authority_sequence=0,
        projection_revision=0,
        canonical_spine_fingerprint=EMPTY_PRESENTATION_SPINE_FINGERPRINT,
        ordered_entries=(),
    )


def _empty_capacity_checkpoint(
    *, runtime_session_id: str, quote_policy_fingerprint: str
) -> PresentationHistoryCapacityCheckpointFact:
    return build_frozen_storage_fact(
        PresentationHistoryCapacityCheckpointFact,
        schema_version="presentation_history_capacity_checkpoint.v1",
        runtime_session_id=runtime_session_id,
        through_authority_sequence=0,
        quote_policy_fingerprint=quote_policy_fingerprint,
        ordered_active_reservations=(),
        fault=None,
    )


def _extend_source_prefix(
    predecessor_accumulator: str,
    segments: tuple[PresentationHistoryTailFoldSegmentFact, ...],
) -> str:
    accumulator = predecessor_accumulator
    for segment in segments:
        accumulator = context_fingerprint(
            "presentation-history-source-prefix-step:v1",
            {
                "previous_prefix_accumulator": accumulator,
                "through_sequence": segment.through_sequence,
                "segment_fingerprint": segment.segment_fingerprint,
            },
        )
    return accumulator


def _segment_source_accumulator(
    segments: tuple[PresentationHistoryTailFoldSegmentFact, ...],
) -> str:
    return context_fingerprint(
        "presentation-history-tail-source-range:v1",
        tuple(
            (item.through_sequence, item.source_range_fingerprint) for item in segments
        ),
    )


def _segment_accumulator(
    segments: tuple[PresentationHistoryTailFoldSegmentFact, ...],
) -> str:
    return context_fingerprint(
        "presentation-history-tail-segments:v1",
        tuple(item.segment_fingerprint for item in segments),
    )


def _mutation_accumulator(
    segments: tuple[PresentationHistoryTailFoldSegmentFact, ...],
) -> str:
    return context_fingerprint(
        "presentation-history-tail-mutations:v1",
        tuple(
            mutation.mutation_fingerprint
            for segment in segments
            for mutation in segment.ordered_mutations
        ),
    )


def _checkpoint_receipt(
    *,
    disposition: CheckpointCommitDisposition,
    candidate_fingerprint: str,
    installed_checkpoint: PresentationHistoryProjectionCheckpointFact | None,
    installed_root_identity: PresentationHistoryRootIdentityFact | None,
    confirmation_kind: str,
) -> PresentationHistoryCheckpointCommitReceipt:
    payload = _checkpoint_confirmation_payload(
        disposition=disposition,
        candidate_fingerprint=candidate_fingerprint,
        installed_checkpoint=installed_checkpoint,
        installed_root_identity=installed_root_identity,
        confirmation_kind=confirmation_kind,
    )
    return PresentationHistoryCheckpointCommitReceipt(
        disposition=disposition,
        candidate_fingerprint=candidate_fingerprint,
        installed_checkpoint=installed_checkpoint,
        installed_root_identity=installed_root_identity,
        confirmation_kind=confirmation_kind,
        confirmation_fingerprint=context_fingerprint(
            "presentation-history-checkpoint-confirmation:v1", payload
        ),
    )


__all__ = [
    "EMPTY_PRESENTATION_ENTRY_ACCUMULATOR",
    "EMPTY_PRESENTATION_SOURCE_PREFIX_ACCUMULATOR",
    "EMPTY_PRESENTATION_SPINE_FINGERPRINT",
    "PRESENTATION_HISTORY_PROJECTION_CONTRACT_FINGERPRINT",
    "PRESENTATION_HISTORY_PROJECTION_KIND",
    "PRESENTATION_HISTORY_PROJECTION_SCHEMA_VERSION",
    "CheckpointCommitDisposition",
    "PreparedPresentationHistoryCheckpointCommitAttempt",
    "PreparedPresentationHistoryRootArtifact",
    "PresentationHistoryCheckpointCommitReceipt",
    "PresentationHistoryCheckpointError",
    "PresentationHistoryProjectionCheckpointOwner",
]
