"""Process-local verified evidence cursor for transcript projection preparation.

The cursor is disposable memoization.  EventLog remains the durable authority and
the transcript state store remains the only live reducer.
"""

from __future__ import annotations

from bisect import bisect_left
from dataclasses import dataclass
from enum import StrEnum
from typing import Literal, Protocol

from pulsara_agent.event_log.protocol import (
    RawStoredEventEnvelope,
    RawTranscriptDomainDeltaSnapshot,
    RawTranscriptDomainPrefixFact,
)
from pulsara_agent.primitives import context_fingerprint
from pulsara_agent.primitives.authority_materialization import (
    TranscriptDomainPrefixFact,
    TranscriptDomainSparseReadProofFact,
    TranscriptProjectionLiveAssemblyState,
)
from pulsara_agent.primitives.frozen import StableEventIdentityFact
from pulsara_agent.primitives.terminal_projection import (
    TerminalProjectionDocumentFact,
    TerminalProjectionReferenceFact,
)
from pulsara_agent.primitives.transcript_projection import (
    InlineNormalizedMessageContentFact,
    NormalizedMessageContentArtifactFact,
    NormalizedMessageContentArtifactReferenceFact,
    TerminalProjectionMessageContentRefFact,
    TranscriptMessageLeafEntryFact,
    TranscriptProjectionBaseFact,
    TranscriptProjectionLeafEntryFact,
    TranscriptProjectionSemanticSourceFact,
    TranscriptToolPairLeafEntryFact,
    TranscriptToolResultLeafEntryFact,
)
from pulsara_agent.runtime.authority_materialization.contracts import (
    TranscriptEventDomainRegistryBinding,
    materialize_transcript_sparse_read_proof,
)


_CURSOR_CONSTRUCTION_GUARD = object()
_CURSOR_USE_GUARD = object()
_MAX_CHUNK_EVENTS = 256
EVIDENCE_CURSOR_IMPLEMENTATION_CONTRACT_FINGERPRINT = context_fingerprint(
    "verified-transcript-projection-cursor-implementation-contract:v1",
    {
        "chunking": "append-only-max-256-semantic-events",
        "deep_validation": "full-vector-and-canonical-sparse-proof",
        "fast_validation": "factory-guard+outer-fingerprint+anchor+reducer-high-water",
        "delta_composition": "validated-token+bounded-new-delta",
        "resident_authority": "process-local-disposable-memoization",
    },
)


class ProjectionEvidenceCursorOutcome(StrEnum):
    SAME_HIGH_WATER_HIT = "same_high_water_hit"
    DELTA_EXTENSION = "delta_extension"
    SEEDED_FROM_STARTUP_RESTORE = "seeded_from_startup_restore"
    EXACT_RESTORE_CURSOR_ABSENT = "exact_restore_cursor_absent"
    EXACT_RESTORE_REQUESTED_BEHIND = "exact_restore_requested_behind"
    EXACT_RESTORE_LIVE_STORE_AHEAD = "exact_restore_live_store_ahead"
    EXACT_RESTORE_ANCHOR_CHANGED = "exact_restore_anchor_changed"
    EXACT_RESTORE_CURSOR_MISMATCH = "exact_restore_cursor_mismatch"
    RESIDENT_ADMISSION_REJECTED = "resident_admission_rejected"


@dataclass(frozen=True, slots=True)
class TranscriptProjectionCursorMetricObservation:
    outcome: ProjectionEvidenceCursorOutcome
    previous_through_sequence: int | None
    requested_through_sequence: int
    new_ledger_events: int
    new_semantic_events: int
    new_semantic_payload_bytes: int
    new_semantic_stored_envelope_bytes: int
    prefix_rows_read: int
    prefix_logical_bytes_read: int
    total_logical_bytes_read: int
    full_semantic_events: int
    full_semantic_payload_bytes: int
    database_read_wall_seconds: float
    proof_compose_wall_seconds: float
    document_freeze_wall_seconds: float
    exact_restore_wall_seconds: float
    fast_validation_wall_seconds: float
    deep_validation_wall_seconds: float
    anchor_generation: int
    resident_charge_bytes: int
    process_resident_charge_bytes: int
    process_resident_chunk_count: int
    process_resident_cursor_count: int

    def __post_init__(self) -> None:
        integers = (
            self.requested_through_sequence,
            self.new_ledger_events,
            self.new_semantic_events,
            self.new_semantic_payload_bytes,
            self.new_semantic_stored_envelope_bytes,
            self.prefix_rows_read,
            self.prefix_logical_bytes_read,
            self.total_logical_bytes_read,
            self.full_semantic_events,
            self.full_semantic_payload_bytes,
            self.anchor_generation,
            self.resident_charge_bytes,
            self.process_resident_charge_bytes,
            self.process_resident_chunk_count,
            self.process_resident_cursor_count,
        )
        durations = (
            self.database_read_wall_seconds,
            self.proof_compose_wall_seconds,
            self.document_freeze_wall_seconds,
            self.exact_restore_wall_seconds,
            self.fast_validation_wall_seconds,
            self.deep_validation_wall_seconds,
        )
        if min(integers) < 0 or min(durations) < 0:
            raise ValueError("cursor metric observation cannot be negative")
        if (
            self.previous_through_sequence is not None
            and self.previous_through_sequence < 0
        ):
            raise ValueError("cursor metric previous high-water cannot be negative")


@dataclass(frozen=True, slots=True)
class CursorAnchorCarrierIdentity:
    stable_event_identity: StableEventIdentityFact
    committed_sequence: int
    carrier_kind: Literal["run_start", "transcript_checkpoint_committed"]

    def __post_init__(self) -> None:
        if self.committed_sequence < 1:
            raise ValueError("cursor anchor carrier must be committed")


@dataclass(frozen=True, slots=True)
class RunSeedCursorBaseIdentity:
    runtime_session_id: str
    base_kind: Literal["run_seed"]
    anchor_carrier: CursorAnchorCarrierIdentity
    anchor_available_from_sequence: int
    run_seed_semantic_fingerprint: str
    run_seed_reference_fingerprint: str
    stable_state_semantic_fingerprint: str
    base_ledger_through_sequence: int
    base_ledger_continuity_accumulator: str
    canonical_base_prefix_fingerprint: str
    event_domain_registry_contract_fingerprint: str
    reducer_contract_fingerprint: str
    identity_fingerprint: str


@dataclass(frozen=True, slots=True)
class CheckpointCursorBaseIdentity:
    runtime_session_id: str
    base_kind: Literal["checkpoint"]
    anchor_carrier: CursorAnchorCarrierIdentity
    anchor_available_from_sequence: int
    run_seed_semantic_fingerprint: str
    run_seed_reference_fingerprint: str
    stable_state_semantic_fingerprint: str
    checkpoint_id: str
    checkpoint_committed_event_id: str
    checkpoint_committed_event_sequence: int
    checkpoint_candidate_fingerprint: str
    checkpoint_candidate_ledger_through_sequence: int
    checkpoint_candidate_ledger_continuity_accumulator: str
    checkpoint_materialization_fingerprint: str
    previous_checkpoint_id: str | None
    ledger_materialization_generation: int
    consumer_horizon_revision: int
    checkpoint_build_contract_fingerprint: str
    base_ledger_through_sequence: int
    base_ledger_continuity_accumulator: str
    canonical_base_prefix_fingerprint: str
    event_domain_registry_contract_fingerprint: str
    reducer_contract_fingerprint: str
    identity_fingerprint: str


TranscriptProjectionCursorBaseIdentity = (
    RunSeedCursorBaseIdentity | CheckpointCursorBaseIdentity
)


@dataclass(frozen=True, slots=True)
class VerifiedTranscriptSemanticEnvelopeChunk:
    first_sequence: int
    last_sequence: int
    event_count: int
    canonical_payload_bytes: int
    envelopes: tuple[RawStoredEventEnvelope, ...]
    chunk_fingerprint: str


@dataclass(frozen=True, slots=True)
class PersistentTranscriptSemanticEnvelopeVector:
    chunks: tuple[VerifiedTranscriptSemanticEnvelopeChunk, ...]
    event_count: int
    canonical_payload_bytes: int
    first_sequence: int | None
    last_sequence: int | None
    vector_fingerprint: str

    @classmethod
    def empty(cls) -> "PersistentTranscriptSemanticEnvelopeVector":
        return cls(
            chunks=(),
            event_count=0,
            canonical_payload_bytes=0,
            first_sequence=None,
            last_sequence=None,
            vector_fingerprint=context_fingerprint(
                "persistent-transcript-semantic-envelope-vector-empty:v1", {}
            ),
        )

    @classmethod
    def build(
        cls,
        envelopes: tuple[RawStoredEventEnvelope, ...],
        *,
        max_payload_bytes: int,
    ) -> "PersistentTranscriptSemanticEnvelopeVector":
        return cls.empty()._append_envelopes(
            envelopes,
            max_payload_bytes=max_payload_bytes,
        )

    def materialize(self) -> tuple[RawStoredEventEnvelope, ...]:
        return tuple(item for chunk in self.chunks for item in chunk.envelopes)

    def _append_envelopes(
        self,
        envelopes: tuple[RawStoredEventEnvelope, ...],
        *,
        max_payload_bytes: int,
    ) -> "PersistentTranscriptSemanticEnvelopeVector":
        if not envelopes:
            return self
        chunks = _build_chunks(envelopes, max_payload_bytes=max_payload_bytes)
        if self.last_sequence is not None and chunks[0].first_sequence <= self.last_sequence:
            raise ValueError("cursor envelope append is not strictly ordered")
        next_chunks = (*self.chunks, *chunks)
        event_count = self.event_count + len(envelopes)
        payload_bytes = self.canonical_payload_bytes + sum(
            len(item.canonical_payload_bytes) for item in envelopes
        )
        first = self.first_sequence if self.first_sequence is not None else envelopes[0].sequence
        last = envelopes[-1].sequence
        return PersistentTranscriptSemanticEnvelopeVector(
            chunks=next_chunks,
            event_count=event_count,
            canonical_payload_bytes=payload_bytes,
            first_sequence=first,
            last_sequence=last,
            vector_fingerprint=context_fingerprint(
                "persistent-transcript-semantic-envelope-vector-append:v1",
                {
                    "previous_vector_fingerprint": self.vector_fingerprint,
                    "new_chunk_fingerprints": tuple(
                        item.chunk_fingerprint for item in chunks
                    ),
                    "event_count": event_count,
                    "canonical_payload_bytes": payload_bytes,
                    "first_sequence": first,
                    "last_sequence": last,
                },
            ),
        )


@dataclass(frozen=True, slots=True)
class TranscriptProjectionReducerEvidenceSnapshot:
    live_state: TranscriptProjectionLiveAssemblyState
    stable_entries: tuple[TranscriptProjectionLeafEntryFact, ...]
    required_projection_references: tuple[TerminalProjectionReferenceFact, ...]
    snapshot_fingerprint: str


class TranscriptProjectionDocumentResolver(Protocol):
    def resolve(
        self,
        reference: TerminalProjectionReferenceFact,
    ) -> TerminalProjectionDocumentFact: ...


@dataclass(frozen=True, slots=True)
class VerifiedTranscriptProjectionDocumentViewEntry:
    reference: TerminalProjectionReferenceFact
    document: TerminalProjectionDocumentFact


@dataclass(frozen=True, slots=True)
class VerifiedTranscriptProjectionDocumentView:
    entries: tuple[VerifiedTranscriptProjectionDocumentViewEntry, ...]
    reference_fingerprints: tuple[str, ...]
    view_fingerprint: str

    def __post_init__(self) -> None:
        expected = tuple(
            item.reference.reference_fingerprint for item in self.entries
        )
        if self.reference_fingerprints != expected:
            raise ValueError("terminal projection frozen-view index drifted")
        if expected != tuple(sorted(expected)) or len(expected) != len(set(expected)):
            raise ValueError("terminal projection frozen-view index is not unique")

    def resolve(
        self,
        reference: TerminalProjectionReferenceFact,
    ) -> TerminalProjectionDocumentFact:
        index = bisect_left(
            self.reference_fingerprints,
            reference.reference_fingerprint,
        )
        if (
            index < len(self.reference_fingerprints)
            and self.reference_fingerprints[index]
            == reference.reference_fingerprint
        ):
            entry = self.entries[index]
            if entry.reference != reference:
                raise ValueError("terminal projection frozen-view reference conflict")
            return entry.document
        raise ValueError("terminal projection document is absent from frozen view")


class TranscriptProjectionMaterializationMismatchCode(StrEnum):
    PROJECTION_AUTHORITY = "projection_authority"
    STABLE_ENTRY_COUNT = "stable_entry_count"
    STABLE_ENTRY_SEMANTIC = "stable_entry_semantic"
    NORMALIZED_MESSAGE_CONTENT = "normalized_message_content"
    TERMINAL_DOCUMENT = "terminal_document"
    PAIRING = "pairing"
    ATTRIBUTION = "attribution"
    SOURCE_REFERENCE = "source_reference"
    NORMALIZED_TRANSCRIPT = "normalized_transcript"


@dataclass(frozen=True, slots=True)
class TranscriptProjectionMaterializationEquivalenceContract:
    contract_id: Literal[
        "pulsara.transcript-projection-materialization-equivalence"
    ]
    contract_version: Literal["1"]
    inline_message_contract_fingerprint: str
    message_artifact_contract_fingerprint: str
    terminal_document_contract_fingerprint: str
    stable_entry_union_contract_fingerprint: str
    contract_fingerprint: str


@dataclass(frozen=True, slots=True)
class TranscriptProjectionMaterializationEquivalenceResult:
    equivalent: bool
    mismatch_code: TranscriptProjectionMaterializationMismatchCode | None
    left_normalized_transcript_fingerprint: str
    right_normalized_transcript_fingerprint: str
    compared_stable_entry_count: int
    compared_terminal_document_count: int
    contract_fingerprint: str

    def __post_init__(self) -> None:
        if self.equivalent != (self.mismatch_code is None):
            raise ValueError("materialization equivalence result is inconsistent")
        if min(
            self.compared_stable_entry_count,
            self.compared_terminal_document_count,
        ) < 0:
            raise ValueError("materialization equivalence counts cannot be negative")
        if self.equivalent and (
            self.left_normalized_transcript_fingerprint
            != self.right_normalized_transcript_fingerprint
        ):
            raise ValueError("equivalent transcript fingerprints differ")


class _PreparedTranscriptProjectionEvidence(Protocol):
    projection_base: TranscriptProjectionBaseFact
    semantic_source: TranscriptProjectionSemanticSourceFact
    domain_completeness_proof: TranscriptDomainSparseReadProofFact
    semantic_delta_events: tuple[RawStoredEventEnvelope, ...]
    stable_entries: tuple[TranscriptProjectionLeafEntryFact, ...]
    document_registry: TranscriptProjectionDocumentResolver
    hydrated_message_contents: tuple[NormalizedMessageContentArtifactFact, ...]


class TranscriptProjectionMaterializationEquivalenceBinding:
    def __init__(
        self,
        contract: TranscriptProjectionMaterializationEquivalenceContract,
    ) -> None:
        expected = _materialization_equivalence_contract_fingerprint(contract)
        if contract.contract_fingerprint != expected:
            raise ValueError("materialization equivalence contract fingerprint drifted")
        self.contract = contract

    def compare(
        self,
        *,
        left: _PreparedTranscriptProjectionEvidence,
        right: _PreparedTranscriptProjectionEvidence,
    ) -> TranscriptProjectionMaterializationEquivalenceResult:
        left_normalized = _normalized_entry_fingerprint(left.stable_entries)
        right_normalized = _normalized_entry_fingerprint(right.stable_entries)
        compared_documents = 0

        def result(
            mismatch: TranscriptProjectionMaterializationMismatchCode | None,
            *,
            compared_entries: int,
        ) -> TranscriptProjectionMaterializationEquivalenceResult:
            return TranscriptProjectionMaterializationEquivalenceResult(
                equivalent=mismatch is None,
                mismatch_code=mismatch,
                left_normalized_transcript_fingerprint=left_normalized,
                right_normalized_transcript_fingerprint=right_normalized,
                compared_stable_entry_count=compared_entries,
                compared_terminal_document_count=compared_documents,
                contract_fingerprint=self.contract.contract_fingerprint,
            )

        if (
            left.projection_base != right.projection_base
            or left.semantic_source != right.semantic_source
            or left.domain_completeness_proof != right.domain_completeness_proof
            or tuple(item.envelope_fingerprint for item in left.semantic_delta_events)
            != tuple(item.envelope_fingerprint for item in right.semantic_delta_events)
        ):
            return result(
                TranscriptProjectionMaterializationMismatchCode.PROJECTION_AUTHORITY,
                compared_entries=0,
            )
        if len(left.stable_entries) != len(right.stable_entries):
            return result(
                TranscriptProjectionMaterializationMismatchCode.STABLE_ENTRY_COUNT,
                compared_entries=0,
            )
        for index, (left_entry, right_entry) in enumerate(
            zip(left.stable_entries, right.stable_entries, strict=True)
        ):
            if type(left_entry) is not type(right_entry) or (
                left_entry.semantic_identity != right_entry.semantic_identity
            ):
                return result(
                    TranscriptProjectionMaterializationMismatchCode.STABLE_ENTRY_SEMANTIC,
                    compared_entries=index,
                )
            if left_entry.source_event_refs != right_entry.source_event_refs:
                return result(
                    TranscriptProjectionMaterializationMismatchCode.SOURCE_REFERENCE,
                    compared_entries=index,
                )
            if isinstance(left_entry, TranscriptMessageLeafEntryFact):
                if left_entry.attribution != right_entry.attribution:
                    return result(
                        TranscriptProjectionMaterializationMismatchCode.ATTRIBUTION,
                        compared_entries=index,
                    )
                if _message_block_semantics(
                    left_entry,
                    hydrated=left.hydrated_message_contents,
                    documents=left.document_registry,
                ) != _message_block_semantics(
                    right_entry,
                    hydrated=right.hydrated_message_contents,
                    documents=right.document_registry,
                ):
                    return result(
                        TranscriptProjectionMaterializationMismatchCode.NORMALIZED_MESSAGE_CONTENT,
                        compared_entries=index,
                    )
                if isinstance(
                    left_entry.content, TerminalProjectionMessageContentRefFact
                ):
                    compared_documents += 1
            elif isinstance(left_entry, TranscriptToolResultLeafEntryFact):
                left_document = left.document_registry.resolve(
                    left_entry.projection_reference
                )
                right_document = right.document_registry.resolve(
                    right_entry.projection_reference
                )
                compared_documents += 1
                if (
                    left_document.semantic_identity
                    != right_document.semantic_identity
                ):
                    return result(
                        TranscriptProjectionMaterializationMismatchCode.TERMINAL_DOCUMENT,
                        compared_entries=index,
                    )
        if left_normalized != right_normalized:
            return result(
                TranscriptProjectionMaterializationMismatchCode.NORMALIZED_TRANSCRIPT,
                compared_entries=len(left.stable_entries),
            )
        return result(None, compared_entries=len(left.stable_entries))


@dataclass(frozen=True, slots=True)
class VerifiedTranscriptProjectionCursorSnapshot:
    generation: int
    base_identity: TranscriptProjectionCursorBaseIdentity
    projection_base: TranscriptProjectionBaseFact
    verified_through_sequence: int
    delta_before: RawTranscriptDomainPrefixFact
    delta_after: RawTranscriptDomainPrefixFact
    semantic_envelopes: PersistentTranscriptSemanticEnvelopeVector
    semantic_source: TranscriptProjectionSemanticSourceFact
    domain_completeness_proof: TranscriptDomainSparseReadProofFact
    reducer_snapshot_fingerprint: str
    cursor_fingerprint: str
    _factory_guard: object


@dataclass(frozen=True, slots=True)
class ValidatedCursorUseToken:
    cursor: VerifiedTranscriptProjectionCursorSnapshot
    anchor_generation: int
    anchor_base_identity_fingerprint: str
    reducer_snapshot_fingerprint: str
    event_domain_registry_contract_fingerprint: str
    token_fingerprint: str
    _factory_guard: object


class ValidatedCursorSnapshotFactory:
    @classmethod
    def validate_base_identity(
        cls,
        identity: TranscriptProjectionCursorBaseIdentity,
    ) -> None:
        _validate_base_identity(identity)

    @classmethod
    def build(
        cls,
        *,
        generation: int,
        base_identity: TranscriptProjectionCursorBaseIdentity,
        projection_base: TranscriptProjectionBaseFact,
        base_prefix: RawTranscriptDomainPrefixFact,
        through_prefix: RawTranscriptDomainPrefixFact,
        semantic_envelopes: PersistentTranscriptSemanticEnvelopeVector,
        semantic_source: TranscriptProjectionSemanticSourceFact,
        domain_completeness_proof: TranscriptDomainSparseReadProofFact,
        reducer_snapshot: TranscriptProjectionReducerEvidenceSnapshot,
        event_domain_binding: TranscriptEventDomainRegistryBinding,
    ) -> VerifiedTranscriptProjectionCursorSnapshot:
        cursor = _construct_cursor(
            generation=generation,
            base_identity=base_identity,
            projection_base=projection_base,
            base_prefix=base_prefix,
            through_prefix=through_prefix,
            semantic_envelopes=semantic_envelopes,
            semantic_source=semantic_source,
            domain_completeness_proof=domain_completeness_proof,
            reducer_snapshot_fingerprint=reducer_snapshot.snapshot_fingerprint,
        )
        cls.deep_validate(
            cursor,
            active_base_identity=base_identity,
            event_domain_binding=event_domain_binding,
            reducer_snapshot=reducer_snapshot,
        )
        return cursor

    @classmethod
    def validate_for_use(
        cls,
        cursor: VerifiedTranscriptProjectionCursorSnapshot,
        *,
        active_generation: int,
        active_base_identity: TranscriptProjectionCursorBaseIdentity,
        event_domain_binding: TranscriptEventDomainRegistryBinding,
        reducer_snapshot: TranscriptProjectionReducerEvidenceSnapshot,
    ) -> ValidatedCursorUseToken:
        if cursor._factory_guard is not _CURSOR_CONSTRUCTION_GUARD:
            raise ValueError("cursor was not built by the validated factory")
        if cursor.cursor_fingerprint != _cursor_fingerprint(cursor):
            raise ValueError("cursor outer fingerprint drifted")
        if cursor.generation != active_generation:
            raise ValueError("cursor anchor generation drifted")
        if cursor.base_identity != active_base_identity:
            raise ValueError("cursor anchor identity drifted")
        if (
            cursor.base_identity.event_domain_registry_contract_fingerprint
            != event_domain_binding.contract.registry_contract_fingerprint
            or cursor.semantic_source.reducer_contract_fingerprint
            != cursor.base_identity.reducer_contract_fingerprint
        ):
            raise ValueError("cursor reducer/domain binding drifted")
        live = reducer_snapshot.live_state
        if live.ledger_through_sequence < cursor.delta_after.through_sequence:
            raise ValueError("cursor is ahead of the live reducer")
        if live.ledger_through_sequence == cursor.delta_after.through_sequence and (
            live.ledger_continuity_accumulator
            != cursor.delta_after.ledger_continuity_accumulator
            or live.transcript_semantic_event_count
            != cursor.delta_after.semantic_event_count
            or live.transcript_semantic_accumulator
            != cursor.delta_after.semantic_accumulator
            or reducer_snapshot.snapshot_fingerprint
            != cursor.reducer_snapshot_fingerprint
        ):
            raise ValueError("cursor/reducer evidence snapshot drifted")
        if (
            cursor.base_identity.anchor_available_from_sequence
            > cursor.verified_through_sequence
        ):
            raise ValueError("cursor anchor was not durable at requested high-water")
        values = {
            "cursor_fingerprint": cursor.cursor_fingerprint,
            "anchor_generation": active_generation,
            "anchor_base_identity_fingerprint": (
                active_base_identity.identity_fingerprint
            ),
            "reducer_snapshot_fingerprint": reducer_snapshot.snapshot_fingerprint,
            "event_domain_registry_contract_fingerprint": (
                event_domain_binding.contract.registry_contract_fingerprint
            ),
        }
        return ValidatedCursorUseToken(
            cursor=cursor,
            anchor_generation=active_generation,
            anchor_base_identity_fingerprint=active_base_identity.identity_fingerprint,
            reducer_snapshot_fingerprint=reducer_snapshot.snapshot_fingerprint,
            event_domain_registry_contract_fingerprint=(
                event_domain_binding.contract.registry_contract_fingerprint
            ),
            token_fingerprint=context_fingerprint(
                "validated-transcript-projection-cursor-use-token:v1",
                values,
            ),
            _factory_guard=_CURSOR_USE_GUARD,
        )

    @classmethod
    def build_from_validated_previous(
        cls,
        *,
        previous: ValidatedCursorUseToken,
        new_delta: RawTranscriptDomainDeltaSnapshot,
        next_projection_base: TranscriptProjectionBaseFact,
        reducer_snapshot: TranscriptProjectionReducerEvidenceSnapshot,
        event_domain_binding: TranscriptEventDomainRegistryBinding,
        max_payload_bytes: int,
    ) -> VerifiedTranscriptProjectionCursorSnapshot:
        _validate_use_token(
            previous,
            event_domain_binding=event_domain_binding,
        )
        if (
            reducer_snapshot.snapshot_fingerprint
            != previous.reducer_snapshot_fingerprint
        ):
            raise ValueError("cursor use token reducer snapshot binding drifted")
        cursor = previous.cursor
        if new_delta.before != cursor.delta_after:
            raise ValueError("cursor delta does not continue its verified prefix")
        vector = cursor.semantic_envelopes._append_envelopes(
            new_delta.semantic_events,
            max_payload_bytes=max_payload_bytes,
        )
        proof = compose_verified_transcript_sparse_read_proof(
            previous=previous,
            new_delta=new_delta,
            next_semantic_envelopes=vector,
            binding=event_domain_binding,
        )
        stable = reducer_snapshot.live_state.stable_semantic_state
        semantic_source = _semantic_source(
            through_prefix=new_delta.after,
            resulting_state_fingerprint=stable.state_semantic_fingerprint,
            prior=cursor.semantic_source,
        )
        candidate = _construct_cursor(
            generation=cursor.generation,
            base_identity=cursor.base_identity,
            projection_base=next_projection_base,
            base_prefix=cursor.delta_before,
            through_prefix=new_delta.after,
            semantic_envelopes=vector,
            semantic_source=semantic_source,
            domain_completeness_proof=proof,
            reducer_snapshot_fingerprint=reducer_snapshot.snapshot_fingerprint,
        )
        _validate_incremental_candidate(
            candidate,
            previous=previous,
            new_delta=new_delta,
            reducer_snapshot=reducer_snapshot,
            event_domain_binding=event_domain_binding,
        )
        return candidate

    @classmethod
    def deep_validate(
        cls,
        cursor: VerifiedTranscriptProjectionCursorSnapshot,
        *,
        active_base_identity: TranscriptProjectionCursorBaseIdentity,
        event_domain_binding: TranscriptEventDomainRegistryBinding,
        reducer_snapshot: TranscriptProjectionReducerEvidenceSnapshot,
    ) -> None:
        if cursor._factory_guard is not _CURSOR_CONSTRUCTION_GUARD:
            raise ValueError("cursor construction guard is invalid")
        _validate_base_identity(cursor.base_identity)
        if cursor.base_identity != active_base_identity:
            raise ValueError("cursor active base identity mismatch")
        if cursor.delta_before.through_sequence != (
            cursor.base_identity.base_ledger_through_sequence
        ):
            raise ValueError("cursor base prefix high-water mismatch")
        if cursor.delta_before.ledger_continuity_accumulator != (
            cursor.base_identity.base_ledger_continuity_accumulator
        ):
            raise ValueError("cursor base continuity mismatch")
        if raw_prefix_fingerprint(cursor.delta_before) != (
            cursor.base_identity.canonical_base_prefix_fingerprint
        ):
            raise ValueError("cursor base prefix fingerprint mismatch")
        _deep_validate_vector(cursor.semantic_envelopes)
        events = cursor.semantic_envelopes.materialize()
        proof = cursor.domain_completeness_proof
        reconstructed_delta = RawTranscriptDomainDeltaSnapshot.build(
            runtime_session_id=cursor.base_identity.runtime_session_id,
            before=cursor.delta_before,
            after=cursor.delta_after,
            semantic_events=events,
            registry_contract_fingerprint=(
                cursor.base_identity.event_domain_registry_contract_fingerprint
            ),
        )
        expected_proof = materialize_transcript_sparse_read_proof(
            reconstructed_delta,
            binding=event_domain_binding,
        )
        if expected_proof != proof:
            raise ValueError("cursor sparse proof is not canonical")
        if proof.prefix_before != _domain_prefix(
            cursor.base_identity.runtime_session_id,
            cursor.delta_before,
            cursor.base_identity.event_domain_registry_contract_fingerprint,
        ) or proof.prefix_through != _domain_prefix(
            cursor.base_identity.runtime_session_id,
            cursor.delta_after,
            cursor.base_identity.event_domain_registry_contract_fingerprint,
        ):
            raise ValueError("cursor sparse proof prefix mismatch")
        if proof.selected_transcript_semantic_event_count != len(events):
            raise ValueError("cursor sparse proof event count mismatch")
        if proof.selected_event_ids_fingerprint != context_fingerprint(
            "transcript-sparse-selected-event-ids:v1",
            tuple(item.event_id for item in events),
        ):
            raise ValueError("cursor sparse proof event identity mismatch")
        if events and (
            events[0].sequence <= cursor.delta_before.through_sequence
            or events[-1].sequence > cursor.delta_after.through_sequence
        ):
            raise ValueError("cursor semantic envelopes exceed the proven range")
        if (
            cursor.semantic_source.semantic_source_event_count
            != cursor.delta_after.semantic_event_count
            or cursor.semantic_source.semantic_source_accumulator
            != cursor.delta_after.semantic_accumulator
        ):
            raise ValueError("cursor semantic source prefix mismatch")
        base_state = cursor.projection_base.common.stable_semantic_state
        if (
            base_state.semantic_source_event_count
            != cursor.delta_before.semantic_event_count
            or base_state.semantic_source_accumulator
            != cursor.delta_before.semantic_accumulator
            or base_state.state_semantic_fingerprint
            != cursor.base_identity.stable_state_semantic_fingerprint
            or cursor.projection_base.common.run_seed_semantic.seed_semantic_fingerprint
            != cursor.base_identity.run_seed_semantic_fingerprint
            or cursor.projection_base.common.run_seed_reference.reference_fingerprint
            != cursor.base_identity.run_seed_reference_fingerprint
        ):
            raise ValueError("cursor projection-base semantic join failed")
        live = reducer_snapshot.live_state
        if (
            live.ledger_through_sequence != cursor.delta_after.through_sequence
            or live.ledger_continuity_accumulator
            != cursor.delta_after.ledger_continuity_accumulator
            or live.transcript_semantic_event_count
            != cursor.delta_after.semantic_event_count
            or live.transcript_semantic_accumulator
            != cursor.delta_after.semantic_accumulator
            or live.stable_semantic_state.state_semantic_fingerprint
            != cursor.semantic_source.resulting_state_fingerprint
        ):
            raise ValueError("cursor reducer/source join failed")
        if reducer_snapshot.snapshot_fingerprint != cursor.reducer_snapshot_fingerprint:
            raise ValueError("cursor reducer snapshot fingerprint mismatch")
        if cursor.cursor_fingerprint != _cursor_fingerprint(cursor):
            raise ValueError("cursor fingerprint mismatch")
        if cursor.verified_through_sequence != cursor.delta_after.through_sequence:
            raise ValueError("cursor verified high-water mismatch")
        if (
            cursor.base_identity.event_domain_registry_contract_fingerprint
            != event_domain_binding.contract.registry_contract_fingerprint
        ):
            raise ValueError("cursor event-domain registry mismatch")
        if (
            cursor.base_identity.anchor_available_from_sequence
            > cursor.verified_through_sequence
        ):
            raise ValueError("cursor anchor carrier lies after requested high-water")


def compose_verified_transcript_sparse_read_proof(
    *,
    previous: ValidatedCursorUseToken,
    new_delta: RawTranscriptDomainDeltaSnapshot,
    next_semantic_envelopes: PersistentTranscriptSemanticEnvelopeVector,
    binding: TranscriptEventDomainRegistryBinding,
) -> TranscriptDomainSparseReadProofFact:
    """Compose a proof after validating only the newly read semantic suffix."""

    _validate_use_token(previous, event_domain_binding=binding)
    cursor = previous.cursor
    if new_delta.before != cursor.delta_after:
        raise ValueError("proof composition delta prefix mismatch")
    materialize_transcript_sparse_read_proof(new_delta, binding=binding)
    events = next_semantic_envelopes.materialize()
    before = _domain_prefix(
        cursor.base_identity.runtime_session_id,
        cursor.delta_before,
        binding.contract.registry_contract_fingerprint,
    )
    through = _domain_prefix(
        cursor.base_identity.runtime_session_id,
        new_delta.after,
        binding.contract.registry_contract_fingerprint,
    )
    payload = {
        "schema_version": "transcript_domain_sparse_read_proof.v1",
        "range_kind": (
            "empty"
            if cursor.delta_before.through_sequence == new_delta.after.through_sequence
            else "non_empty"
        ),
        "from_sequence": cursor.delta_before.through_sequence + 1,
        "through_sequence": new_delta.after.through_sequence,
        "prefix_before": before,
        "prefix_through": through,
        "selected_transcript_semantic_event_count": len(events),
        "selected_transcript_semantic_accumulator": new_delta.after.semantic_accumulator,
        "selected_event_ids_fingerprint": context_fingerprint(
            "transcript-sparse-selected-event-ids:v1",
            tuple(item.event_id for item in events),
        ),
    }
    return TranscriptDomainSparseReadProofFact(
        **payload,
        completeness_fingerprint=context_fingerprint(
            "transcript-domain-sparse-read-proof:v1", payload
        ),
    )


def raw_prefix_fingerprint(prefix: RawTranscriptDomainPrefixFact) -> str:
    return context_fingerprint(
        "raw-transcript-domain-prefix:v1",
        {
            "through_sequence": prefix.through_sequence,
            "ledger_payload_bytes": prefix.ledger_payload_bytes,
            "semantic_event_count": prefix.semantic_event_count,
            "semantic_accumulator": prefix.semantic_accumulator,
            "ledger_continuity_accumulator": prefix.ledger_continuity_accumulator,
        },
    )


def build_materialization_equivalence_contract(
    *,
    message_artifact_contract_fingerprint: str,
    terminal_document_contract_fingerprint: str,
) -> TranscriptProjectionMaterializationEquivalenceContract:
    values = {
        "contract_id": (
            "pulsara.transcript-projection-materialization-equivalence"
        ),
        "contract_version": "1",
        "inline_message_contract_fingerprint": context_fingerprint(
            "inline-normalized-message-content-schema:v3",
            InlineNormalizedMessageContentFact.model_json_schema(),
        ),
        "message_artifact_contract_fingerprint": (
            message_artifact_contract_fingerprint
        ),
        "terminal_document_contract_fingerprint": (
            terminal_document_contract_fingerprint
        ),
        "stable_entry_union_contract_fingerprint": context_fingerprint(
            "transcript-projection-leaf-entry-union-schema:v3",
            tuple(
                item.model_json_schema()
                for item in (
                    TranscriptMessageLeafEntryFact,
                    TranscriptToolPairLeafEntryFact,
                    TranscriptToolResultLeafEntryFact,
                )
            ),
        ),
    }
    return TranscriptProjectionMaterializationEquivalenceContract(
        **values,
        contract_fingerprint=context_fingerprint(
            "transcript-projection-materialization-equivalence-contract:v1",
            values,
        ),
    )


def _materialization_equivalence_contract_fingerprint(
    contract: TranscriptProjectionMaterializationEquivalenceContract,
) -> str:
    return context_fingerprint(
        "transcript-projection-materialization-equivalence-contract:v1",
        {
            field: getattr(contract, field)
            for field in contract.__dataclass_fields__
            if field != "contract_fingerprint"
        },
    )


def _normalized_entry_fingerprint(
    entries: tuple[TranscriptProjectionLeafEntryFact, ...],
) -> str:
    return context_fingerprint(
        "normalized-transcript-semantic:v1",
        tuple(item.semantic_identity.semantic_fingerprint for item in entries),
    )


def _message_block_semantics(
    entry: TranscriptMessageLeafEntryFact,
    *,
    hydrated: tuple[NormalizedMessageContentArtifactFact, ...],
    documents: TranscriptProjectionDocumentResolver,
) -> tuple[str, ...]:
    content = entry.content
    if isinstance(content, InlineNormalizedMessageContentFact):
        blocks = content.blocks
        return tuple(
            item.provider_semantic_identity.semantic_fingerprint for item in blocks
        )
    if isinstance(content, NormalizedMessageContentArtifactReferenceFact):
        matches = tuple(
            item
            for item in hydrated
            if item.fact_fingerprint == content.document_fact_fingerprint
        )
        if len(matches) != 1:
            raise ValueError("normalized message artifact hydration is ambiguous")
        document = matches[0]
        if (
            document.provider_semantic_identity != content.provider_semantic_identity
            or document.artifact_contract_fingerprint
            != content.artifact_contract_fingerprint
        ):
            raise ValueError("normalized message artifact semantic join failed")
        return tuple(
            item.provider_semantic_identity.semantic_fingerprint
            for item in document.blocks
        )
    if isinstance(content, TerminalProjectionMessageContentRefFact):
        document = documents.resolve(content.projection_reference)
        payload = document.payload
        if payload.projection_kind != "model_call":
            raise ValueError("message terminal projection is not a model document")
        by_order = {
            item.semantic_identity.projection_order: (
                item.semantic_identity.semantic_fingerprint
            )
            for item in payload.items
        }
        try:
            return tuple(by_order[item] for item in content.selected_projection_orders)
        except KeyError as exc:
            raise ValueError("message terminal projection order is missing") from exc
    raise TypeError("unsupported transcript message content carrier")


def build_run_seed_cursor_base_identity(
    *,
    runtime_session_id: str,
    carrier: CursorAnchorCarrierIdentity,
    run_seed_semantic_fingerprint: str,
    run_seed_reference_fingerprint: str,
    stable_state_semantic_fingerprint: str,
    base_prefix: RawTranscriptDomainPrefixFact,
    event_domain_registry_contract_fingerprint: str,
    reducer_contract_fingerprint: str,
) -> RunSeedCursorBaseIdentity:
    values = {
        "runtime_session_id": runtime_session_id,
        "base_kind": "run_seed",
        "anchor_carrier": carrier,
        "anchor_available_from_sequence": carrier.committed_sequence,
        "run_seed_semantic_fingerprint": run_seed_semantic_fingerprint,
        "run_seed_reference_fingerprint": run_seed_reference_fingerprint,
        "stable_state_semantic_fingerprint": stable_state_semantic_fingerprint,
        "base_ledger_through_sequence": base_prefix.through_sequence,
        "base_ledger_continuity_accumulator": base_prefix.ledger_continuity_accumulator,
        "canonical_base_prefix_fingerprint": raw_prefix_fingerprint(base_prefix),
        "event_domain_registry_contract_fingerprint": (
            event_domain_registry_contract_fingerprint
        ),
        "reducer_contract_fingerprint": reducer_contract_fingerprint,
    }
    payload = {**values, "anchor_carrier": _carrier_payload(carrier)}
    return RunSeedCursorBaseIdentity(
        **values,
        identity_fingerprint=context_fingerprint(
            "run-seed-cursor-base-identity:v1", payload
        ),
    )


def build_checkpoint_cursor_base_identity(
    *,
    runtime_session_id: str,
    carrier: CursorAnchorCarrierIdentity,
    run_seed_semantic_fingerprint: str,
    run_seed_reference_fingerprint: str,
    stable_state_semantic_fingerprint: str,
    checkpoint_id: str,
    checkpoint_candidate_fingerprint: str,
    checkpoint_materialization_fingerprint: str,
    previous_checkpoint_id: str | None,
    ledger_materialization_generation: int,
    consumer_horizon_revision: int,
    checkpoint_build_contract_fingerprint: str,
    base_prefix: RawTranscriptDomainPrefixFact,
    event_domain_registry_contract_fingerprint: str,
    reducer_contract_fingerprint: str,
) -> CheckpointCursorBaseIdentity:
    values = {
        "runtime_session_id": runtime_session_id,
        "base_kind": "checkpoint",
        "anchor_carrier": carrier,
        "anchor_available_from_sequence": carrier.committed_sequence,
        "run_seed_semantic_fingerprint": run_seed_semantic_fingerprint,
        "run_seed_reference_fingerprint": run_seed_reference_fingerprint,
        "stable_state_semantic_fingerprint": stable_state_semantic_fingerprint,
        "checkpoint_id": checkpoint_id,
        "checkpoint_committed_event_id": carrier.stable_event_identity.event_id,
        "checkpoint_committed_event_sequence": carrier.committed_sequence,
        "checkpoint_candidate_fingerprint": checkpoint_candidate_fingerprint,
        "checkpoint_candidate_ledger_through_sequence": base_prefix.through_sequence,
        "checkpoint_candidate_ledger_continuity_accumulator": (
            base_prefix.ledger_continuity_accumulator
        ),
        "checkpoint_materialization_fingerprint": checkpoint_materialization_fingerprint,
        "previous_checkpoint_id": previous_checkpoint_id,
        "ledger_materialization_generation": ledger_materialization_generation,
        "consumer_horizon_revision": consumer_horizon_revision,
        "checkpoint_build_contract_fingerprint": checkpoint_build_contract_fingerprint,
        "base_ledger_through_sequence": base_prefix.through_sequence,
        "base_ledger_continuity_accumulator": base_prefix.ledger_continuity_accumulator,
        "canonical_base_prefix_fingerprint": raw_prefix_fingerprint(base_prefix),
        "event_domain_registry_contract_fingerprint": (
            event_domain_registry_contract_fingerprint
        ),
        "reducer_contract_fingerprint": reducer_contract_fingerprint,
    }
    payload = {**values, "anchor_carrier": _carrier_payload(carrier)}
    return CheckpointCursorBaseIdentity(
        **values,
        identity_fingerprint=context_fingerprint(
            "checkpoint-cursor-base-identity:v1", payload
        ),
    )


def _semantic_source(
    *,
    through_prefix: RawTranscriptDomainPrefixFact,
    resulting_state_fingerprint: str,
    prior: TranscriptProjectionSemanticSourceFact,
) -> TranscriptProjectionSemanticSourceFact:
    from pulsara_agent.primitives.frozen import build_frozen_fact

    return build_frozen_fact(
        TranscriptProjectionSemanticSourceFact,
        schema_version="transcript_projection_semantic_source.v1",
        reducer_id=prior.reducer_id,
        reducer_version=prior.reducer_version,
        reducer_contract_fingerprint=prior.reducer_contract_fingerprint,
        transcript_semantic_domain_contract_fingerprint=(
            prior.transcript_semantic_domain_contract_fingerprint
        ),
        semantic_source_event_count=through_prefix.semantic_event_count,
        semantic_source_accumulator=through_prefix.semantic_accumulator,
        resulting_state_fingerprint=resulting_state_fingerprint,
    )


def _build_chunks(
    envelopes: tuple[RawStoredEventEnvelope, ...],
    *,
    max_payload_bytes: int,
) -> tuple[VerifiedTranscriptSemanticEnvelopeChunk, ...]:
    output: list[VerifiedTranscriptSemanticEnvelopeChunk] = []
    current: list[RawStoredEventEnvelope] = []
    current_bytes = 0
    previous_sequence: int | None = None
    for envelope in envelopes:
        size = len(envelope.canonical_payload_bytes)
        if size > max_payload_bytes:
            raise ValueError("single transcript semantic envelope exceeds physical bound")
        if previous_sequence is not None and envelope.sequence <= previous_sequence:
            raise ValueError("transcript semantic envelopes are not strictly ordered")
        if current and (
            len(current) >= _MAX_CHUNK_EVENTS
            or current_bytes + size > max_payload_bytes
        ):
            output.append(_chunk(tuple(current)))
            current = []
            current_bytes = 0
        current.append(envelope)
        current_bytes += size
        previous_sequence = envelope.sequence
    if current:
        output.append(_chunk(tuple(current)))
    return tuple(output)


def _chunk(
    envelopes: tuple[RawStoredEventEnvelope, ...],
) -> VerifiedTranscriptSemanticEnvelopeChunk:
    payload_bytes = sum(len(item.canonical_payload_bytes) for item in envelopes)
    payload = {
        "first_sequence": envelopes[0].sequence,
        "last_sequence": envelopes[-1].sequence,
        "event_count": len(envelopes),
        "canonical_payload_bytes": payload_bytes,
        "envelope_fingerprints": tuple(
            item.envelope_fingerprint for item in envelopes
        ),
    }
    return VerifiedTranscriptSemanticEnvelopeChunk(
        first_sequence=envelopes[0].sequence,
        last_sequence=envelopes[-1].sequence,
        event_count=len(envelopes),
        canonical_payload_bytes=payload_bytes,
        envelopes=envelopes,
        chunk_fingerprint=context_fingerprint(
            "verified-transcript-semantic-envelope-chunk:v1", payload
        ),
    )


def _deep_validate_vector(
    vector: PersistentTranscriptSemanticEnvelopeVector,
) -> None:
    rebuilt = PersistentTranscriptSemanticEnvelopeVector.empty()
    for chunk in vector.chunks:
        expected = _chunk(chunk.envelopes)
        if expected != chunk:
            raise ValueError("cursor semantic envelope chunk drifted")
        if rebuilt.last_sequence is not None and chunk.first_sequence <= rebuilt.last_sequence:
            raise ValueError("cursor semantic envelope chunks overlap")
        event_count = rebuilt.event_count + chunk.event_count
        payload_bytes = rebuilt.canonical_payload_bytes + chunk.canonical_payload_bytes
        first = rebuilt.first_sequence or chunk.first_sequence
        rebuilt = PersistentTranscriptSemanticEnvelopeVector(
            chunks=(*rebuilt.chunks, chunk),
            event_count=event_count,
            canonical_payload_bytes=payload_bytes,
            first_sequence=first,
            last_sequence=chunk.last_sequence,
            vector_fingerprint=context_fingerprint(
                "persistent-transcript-semantic-envelope-vector-append:v1",
                {
                    "previous_vector_fingerprint": rebuilt.vector_fingerprint,
                    "new_chunk_fingerprints": (chunk.chunk_fingerprint,),
                    "event_count": event_count,
                    "canonical_payload_bytes": payload_bytes,
                    "first_sequence": first,
                    "last_sequence": chunk.last_sequence,
                },
            ),
        )
    if rebuilt != vector:
        raise ValueError("cursor semantic envelope vector drifted")


def _validate_base_identity(identity: TranscriptProjectionCursorBaseIdentity) -> None:
    if identity.anchor_available_from_sequence != identity.anchor_carrier.committed_sequence:
        raise ValueError("cursor anchor availability drifted")
    payload = {
        item: (
            _carrier_payload(value)
            if isinstance(value, CursorAnchorCarrierIdentity)
            else value
        )
        for item in identity.__dataclass_fields__
        if item != "identity_fingerprint"
        for value in (getattr(identity, item),)
    }
    namespace = (
        "run-seed-cursor-base-identity:v1"
        if isinstance(identity, RunSeedCursorBaseIdentity)
        else "checkpoint-cursor-base-identity:v1"
    )
    if identity.identity_fingerprint != context_fingerprint(namespace, payload):
        raise ValueError("cursor base identity fingerprint drifted")
    if isinstance(identity, RunSeedCursorBaseIdentity):
        if identity.anchor_carrier.carrier_kind != "run_start":
            raise ValueError("run-seed cursor has wrong carrier kind")
    elif (
        identity.anchor_carrier.carrier_kind != "transcript_checkpoint_committed"
        or identity.checkpoint_committed_event_id
        != identity.anchor_carrier.stable_event_identity.event_id
        or identity.checkpoint_committed_event_sequence
        != identity.anchor_carrier.committed_sequence
    ):
        raise ValueError("checkpoint cursor carrier identity drifted")


def _cursor_fingerprint(cursor: VerifiedTranscriptProjectionCursorSnapshot) -> str:
    return context_fingerprint(
        "verified-transcript-projection-cursor:v1",
        {
            "generation": cursor.generation,
            "base_identity_fingerprint": cursor.base_identity.identity_fingerprint,
            "projection_base_fact_fingerprint": cursor.projection_base.fact_fingerprint,
            "verified_through_sequence": cursor.verified_through_sequence,
            "delta_before_prefix_fingerprint": raw_prefix_fingerprint(
                cursor.delta_before
            ),
            "delta_after_prefix_fingerprint": raw_prefix_fingerprint(cursor.delta_after),
            "semantic_envelope_vector_fingerprint": (
                cursor.semantic_envelopes.vector_fingerprint
            ),
            "semantic_source_fingerprint": (
                cursor.semantic_source.semantic_source_fingerprint
            ),
            "domain_completeness_proof_fingerprint": (
                cursor.domain_completeness_proof.completeness_fingerprint
            ),
            "reducer_snapshot_fingerprint": cursor.reducer_snapshot_fingerprint,
            "implementation_contract_fingerprint": (
                EVIDENCE_CURSOR_IMPLEMENTATION_CONTRACT_FINGERPRINT
            ),
        },
    )


def _construct_cursor(
    *,
    generation: int,
    base_identity: TranscriptProjectionCursorBaseIdentity,
    projection_base: TranscriptProjectionBaseFact,
    base_prefix: RawTranscriptDomainPrefixFact,
    through_prefix: RawTranscriptDomainPrefixFact,
    semantic_envelopes: PersistentTranscriptSemanticEnvelopeVector,
    semantic_source: TranscriptProjectionSemanticSourceFact,
    domain_completeness_proof: TranscriptDomainSparseReadProofFact,
    reducer_snapshot_fingerprint: str,
) -> VerifiedTranscriptProjectionCursorSnapshot:
    cursor = VerifiedTranscriptProjectionCursorSnapshot(
        generation=generation,
        base_identity=base_identity,
        projection_base=projection_base,
        verified_through_sequence=through_prefix.through_sequence,
        delta_before=base_prefix,
        delta_after=through_prefix,
        semantic_envelopes=semantic_envelopes,
        semantic_source=semantic_source,
        domain_completeness_proof=domain_completeness_proof,
        reducer_snapshot_fingerprint=reducer_snapshot_fingerprint,
        cursor_fingerprint="",
        _factory_guard=_CURSOR_CONSTRUCTION_GUARD,
    )
    object.__setattr__(cursor, "cursor_fingerprint", _cursor_fingerprint(cursor))
    return cursor


def _validate_incremental_candidate(
    candidate: VerifiedTranscriptProjectionCursorSnapshot,
    *,
    previous: ValidatedCursorUseToken,
    new_delta: RawTranscriptDomainDeltaSnapshot,
    reducer_snapshot: TranscriptProjectionReducerEvidenceSnapshot,
    event_domain_binding: TranscriptEventDomainRegistryBinding,
) -> None:
    _validate_use_token(
        previous,
        event_domain_binding=event_domain_binding,
    )
    old = previous.cursor
    if (
        candidate.generation != old.generation
        or candidate.base_identity != old.base_identity
        or candidate.delta_before != old.delta_before
        or new_delta.before != old.delta_after
        or candidate.delta_after != new_delta.after
    ):
        raise ValueError("incremental cursor lineage drifted")
    if candidate.semantic_envelopes.chunks[: len(old.semantic_envelopes.chunks)] != (
        old.semantic_envelopes.chunks
    ):
        raise ValueError("incremental cursor replaced authenticated chunks")
    if (
        candidate.semantic_envelopes.event_count
        != old.semantic_envelopes.event_count + len(new_delta.semantic_events)
        or candidate.domain_completeness_proof.prefix_before
        != old.domain_completeness_proof.prefix_before
        or candidate.domain_completeness_proof.prefix_through
        != _domain_prefix(
            candidate.base_identity.runtime_session_id,
            new_delta.after,
            event_domain_binding.contract.registry_contract_fingerprint,
        )
    ):
        raise ValueError("incremental cursor proof composition drifted")
    live = reducer_snapshot.live_state
    if (
        live.ledger_through_sequence != new_delta.after.through_sequence
        or live.ledger_continuity_accumulator
        != new_delta.after.ledger_continuity_accumulator
        or live.transcript_semantic_event_count
        != new_delta.after.semantic_event_count
        or live.transcript_semantic_accumulator
        != new_delta.after.semantic_accumulator
        or live.stable_semantic_state.state_semantic_fingerprint
        != candidate.semantic_source.resulting_state_fingerprint
        or candidate.semantic_source.semantic_source_event_count
        != new_delta.after.semantic_event_count
        or candidate.semantic_source.semantic_source_accumulator
        != new_delta.after.semantic_accumulator
    ):
        raise ValueError("incremental cursor/reducer join failed")
    if candidate.cursor_fingerprint != _cursor_fingerprint(candidate):
        raise ValueError("incremental cursor fingerprint drifted")


def _carrier_payload(carrier: CursorAnchorCarrierIdentity) -> dict[str, object]:
    return {
        "stable_event_identity": carrier.stable_event_identity,
        "committed_sequence": carrier.committed_sequence,
        "carrier_kind": carrier.carrier_kind,
    }


def _domain_prefix(
    runtime_session_id: str,
    prefix: RawTranscriptDomainPrefixFact,
    registry_fingerprint: str,
) -> TranscriptDomainPrefixFact:
    payload = {
        "schema_version": "transcript_domain_prefix.v1",
        "runtime_session_id": runtime_session_id,
        "ledger_through_sequence": prefix.through_sequence,
        "ledger_event_count": prefix.through_sequence,
        "ledger_continuity_accumulator": prefix.ledger_continuity_accumulator,
        "transcript_semantic_event_count": prefix.semantic_event_count,
        "transcript_semantic_accumulator": prefix.semantic_accumulator,
        "event_domain_registry_contract_fingerprint": registry_fingerprint,
    }
    return TranscriptDomainPrefixFact(
        **payload,
        prefix_fingerprint=context_fingerprint(
            "transcript-domain-prefix:v1", payload
        ),
    )


def _validate_use_token(
    token: ValidatedCursorUseToken,
    *,
    event_domain_binding: TranscriptEventDomainRegistryBinding,
) -> None:
    if token._factory_guard is not _CURSOR_USE_GUARD:
        raise ValueError("cursor use token was not issued by the factory")
    cursor = token.cursor
    if cursor._factory_guard is not _CURSOR_CONSTRUCTION_GUARD:
        raise ValueError("cursor use token references an untrusted cursor")
    if token.anchor_generation != cursor.generation:
        raise ValueError("cursor use token generation drifted")
    if (
        token.anchor_base_identity_fingerprint
        != cursor.base_identity.identity_fingerprint
    ):
        raise ValueError("cursor use token anchor identity drifted")
    if (
        token.event_domain_registry_contract_fingerprint
        != cursor.base_identity.event_domain_registry_contract_fingerprint
        or token.event_domain_registry_contract_fingerprint
        != event_domain_binding.contract.registry_contract_fingerprint
    ):
        raise ValueError("cursor use token event-domain binding drifted")
    expected_token_fingerprint = context_fingerprint(
        "validated-transcript-projection-cursor-use-token:v1",
        {
            "cursor_fingerprint": cursor.cursor_fingerprint,
            "anchor_generation": token.anchor_generation,
            "anchor_base_identity_fingerprint": (
                token.anchor_base_identity_fingerprint
            ),
            "reducer_snapshot_fingerprint": token.reducer_snapshot_fingerprint,
            "event_domain_registry_contract_fingerprint": (
                token.event_domain_registry_contract_fingerprint
            ),
        },
    )
    if token.token_fingerprint != expected_token_fingerprint:
        raise ValueError("cursor use token frozen identity drifted")
    if cursor.cursor_fingerprint != _cursor_fingerprint(cursor):
        raise ValueError("cursor use token references a drifted cursor")


__all__ = [
    "CheckpointCursorBaseIdentity",
    "CursorAnchorCarrierIdentity",
    "EVIDENCE_CURSOR_IMPLEMENTATION_CONTRACT_FINGERPRINT",
    "PersistentTranscriptSemanticEnvelopeVector",
    "ProjectionEvidenceCursorOutcome",
    "RunSeedCursorBaseIdentity",
    "TranscriptProjectionCursorBaseIdentity",
    "TranscriptProjectionCursorMetricObservation",
    "TranscriptProjectionDocumentResolver",
    "TranscriptProjectionMaterializationEquivalenceBinding",
    "TranscriptProjectionMaterializationEquivalenceContract",
    "TranscriptProjectionMaterializationEquivalenceResult",
    "TranscriptProjectionMaterializationMismatchCode",
    "TranscriptProjectionReducerEvidenceSnapshot",
    "ValidatedCursorSnapshotFactory",
    "ValidatedCursorUseToken",
    "VerifiedTranscriptProjectionCursorSnapshot",
    "VerifiedTranscriptProjectionDocumentView",
    "VerifiedTranscriptProjectionDocumentViewEntry",
    "VerifiedTranscriptSemanticEnvelopeChunk",
    "build_checkpoint_cursor_base_identity",
    "build_run_seed_cursor_base_identity",
    "compose_verified_transcript_sparse_read_proof",
    "build_materialization_equivalence_contract",
    "raw_prefix_fingerprint",
]
