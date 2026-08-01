"""Incremental transcript stable/live state over committed typed facts."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from threading import RLock
from typing import Iterable, Literal

from pulsara_agent.event_log.historical_decoder import decode_raw_stored_event_envelope

from pulsara_agent.event import (
    AgentEvent,
    ExternalExecutionResultEvent,
    ModelCallStartEvent,
    ModelCallControlDispositionResolvedEvent,
    ModelCallTerminalProjectionCommittedEvent,
    RequireExternalExecutionEvent,
    RunEndEvent,
    RunStartEvent,
    ToolExecutionSuspendedEvent,
    ToolResultTerminalProjectionCommittedEvent,
    UserSteerCommittedEvent,
)

from pulsara_agent.event_log.protocol import (
    RawTranscriptDomainDeltaSnapshot,
)
from pulsara_agent.event_log.serialization import DEFAULT_EVENT_SCHEMA_REGISTRY
from pulsara_agent.event_log.transcript_prefix import (
    EMPTY_LEDGER_CONTINUITY_ACCUMULATOR,
    EMPTY_TRANSCRIPT_SEMANTIC_ACCUMULATOR,
    advance_ledger_continuity_accumulator,
    advance_transcript_semantic_accumulator,
    classify_transcript_event_type,
)
from pulsara_agent.primitives import context_fingerprint
from pulsara_agent.primitives.context import ContextEventReferenceFact
from pulsara_agent.primitives.model_call import ModelCallControlDisposition
from pulsara_agent.primitives.stored_event import RawStoredEventEnvelope
from pulsara_agent.primitives.terminal_presentation import (
    CanonicalTranscriptFoldDeltaFact,
    CanonicalTranscriptLeafChangeFact,
    CanonicalTranscriptPlacementAnchorReferenceFact,
    CanonicalTranscriptPlacementAnchorTombstoneFact,
    CanonicalTranscriptPlacementTransitionProofFact,
    LiveCommittedFoldResult,
    PRESENTATION_PLACEMENT_KEY_CONTRACT_FINGERPRINT,
    PRESENTATION_PLACEMENT_KEY_CONTRACT_ID,
    PRESENTATION_PLACEMENT_KEY_CONTRACT_VERSION,
    RestoredRangeFoldResult,
    TranscriptAuditDispositionFact,
)
from pulsara_agent.primitives.authority_materialization import (
    TranscriptProjectionLiveAssemblyState,
    TranscriptProjectionStableSemanticStateFact,
)
from pulsara_agent.primitives.frozen import build_frozen_fact
from pulsara_agent.llm.user_carrier import (
    canonical_runtime_observation_wire_from_semantic,
)
from pulsara_agent.primitives.terminal_projection import (
    ModelTerminalProjectionPayloadFact,
    ModelToolCallBlockSemanticFact,
    TerminalProjectionDocumentFact,
    TerminalProjectionReferenceFact,
)
from pulsara_agent.primitives.transcript_projection import (
    InlineNormalizedMessageContentFact,
    TerminalProjectionMessageContentRefFact,
    TranscriptInlineBlockAttributionFact,
    TranscriptInlineBlockFact,
    TranscriptMessageAttributionFact,
    TranscriptMessageLeafEntryFact,
    TranscriptMessageLeafSemanticFact,
    TranscriptMessageProviderSemanticFact,
    TranscriptProjectionLeafEntryFact,
    TranscriptProjectionOrdinalFact,
    TranscriptProviderTextBlockSemanticFact,
    TranscriptToolPairLeafEntryFact,
    TranscriptToolPairLeafSemanticFact,
    TranscriptToolResultLeafEntryFact,
    TranscriptToolResultLeafSemanticFact,
)
from pulsara_agent.primitives.transcript_message_semantics import (
    build_inline_text_message_semantics,
    build_transcript_message_provider_semantic,
)
from pulsara_agent.runtime.recovery import (
    FAILURE_NOTE_TEXT,
    HOST_TEARDOWN_NOTE_TEXT,
    INTERRUPTED_NOTE_TEXT,
)
from pulsara_agent.runtime.authority_materialization.evidence_cursor import (
    TranscriptProjectionReducerEvidenceSnapshot,
    VerifiedTranscriptProjectionDocumentView,
    VerifiedTranscriptProjectionDocumentViewEntry,
)
from pulsara_agent.ports.stored_event import (
    JoinedRawStoredEventRangeProof,
    StoredEventBatchCommitReceipt,
)


TRANSCRIPT_PROJECTION_REDUCER_CONTRACT_FINGERPRINT = context_fingerprint(
    "transcript-projection-reducer-contract:v1",
    {
        "model": "terminal-projection+durable-control-disposition:v1",
        "tools": "terminal-projection+pairing-by-tool-call-id:v1",
        "pending": "model+disposition+tool-pair+suspension+external:v1",
        "checkpoint": "transcript-acceleration-deterministic-noop:v1",
    },
)
TRANSCRIPT_PROJECTION_REDUCER_ID = "canonical-transcript-projection"
TRANSCRIPT_PROJECTION_REDUCER_VERSION = "v1"
TRANSCRIPT_EVENT_REGISTRY_CONTRACT_FINGERPRINT = context_fingerprint(
    "transcript-event-schema-registry-contract:v1",
    tuple(
        item.domain_contract_fingerprint
        for item in DEFAULT_EVENT_SCHEMA_REGISTRY.contracts()
    ),
)


@dataclass(frozen=True, slots=True)
class _ProjectionRecord:
    reference: TerminalProjectionReferenceFact
    document: TerminalProjectionDocumentFact
    committed_sequence: int
    committed_event: (
        ModelCallTerminalProjectionCommittedEvent
        | ToolResultTerminalProjectionCommittedEvent
    )
    raw_stored_envelope: RawStoredEventEnvelope


@dataclass(slots=True)
class _AcceptedModelAssembly:
    record: _ProjectionRecord
    disposition_event: ModelCallControlDispositionResolvedEvent
    disposition_raw_stored_envelope: RawStoredEventEnvelope
    tool_calls: tuple[ModelToolCallBlockSemanticFact, ...]
    results: dict[str, _ProjectionRecord]


@dataclass(frozen=True, slots=True)
class GovernanceTranscriptAuthoritySnapshot:
    """One reducer-owned governance authority view frozen at a single H."""

    reducer_evidence_snapshot: TranscriptProjectionReducerEvidenceSnapshot
    document_view: VerifiedTranscriptProjectionDocumentView
    ledger_through_sequence: int
    ledger_continuity_accumulator: str
    transcript_semantic_event_count: int
    transcript_semantic_accumulator: str
    snapshot_fingerprint: str


@dataclass(frozen=True, slots=True)
class CanonicalRunFinalAssistantProjection:
    """One accepted, canonical assistant message at an exact ledger horizon."""

    entry: TranscriptMessageLeafEntryFact
    document: TerminalProjectionDocumentFact


@dataclass(frozen=True, slots=True)
class CanonicalTranscriptPlacementSpineSnapshot:
    """Process-local exact pairing of stable leaves and their placement anchors."""

    runtime_session_id: str
    through_sequence: int
    stable_entries: tuple[TranscriptProjectionLeafEntryFact, ...]
    ordered_anchor_references: tuple[
        CanonicalTranscriptPlacementAnchorReferenceFact, ...
    ]
    canonical_spine_fingerprint: str
    snapshot_fingerprint: str


class TranscriptProjectionDocumentRegistry:
    """Hydrated immutable documents prepared before the pure committed fold."""

    def __init__(self) -> None:
        self._lock = RLock()
        self._documents: dict[str, TerminalProjectionDocumentFact] = {}

    def register(
        self,
        reference: TerminalProjectionReferenceFact,
        document: TerminalProjectionDocumentFact,
    ) -> None:
        _validate_document_reference(reference, document)
        with self._lock:
            existing = self._documents.get(reference.reference_fingerprint)
            if existing is not None and existing != document:
                raise ValueError("terminal projection document registry conflict")
            self._documents[reference.reference_fingerprint] = document

    def resolve(
        self,
        reference: TerminalProjectionReferenceFact,
    ) -> TerminalProjectionDocumentFact:
        with self._lock:
            try:
                document = self._documents[reference.reference_fingerprint]
            except KeyError as exc:
                raise ValueError(
                    "terminal projection document was not prepared before fold"
                ) from exc
        _validate_document_reference(reference, document)
        return document

    def contains(self, reference: TerminalProjectionReferenceFact) -> bool:
        with self._lock:
            return reference.reference_fingerprint in self._documents

    def freeze_references(
        self,
        references: tuple[TerminalProjectionReferenceFact, ...],
    ) -> VerifiedTranscriptProjectionDocumentView:
        """Freeze one exact immutable subset for a provider-visible preparation."""

        by_fingerprint: dict[str, TerminalProjectionReferenceFact] = {}
        with self._lock:
            for reference in references:
                existing = by_fingerprint.get(reference.reference_fingerprint)
                if existing is not None and existing != reference:
                    raise ValueError(
                        "terminal projection frozen-view reference conflict"
                    )
                by_fingerprint[reference.reference_fingerprint] = reference
            entries: list[VerifiedTranscriptProjectionDocumentViewEntry] = []
            for fingerprint in sorted(by_fingerprint):
                reference = by_fingerprint[fingerprint]
                try:
                    document = self._documents[fingerprint]
                except KeyError as exc:
                    raise ValueError(
                        "terminal projection document was not prepared before freeze"
                    ) from exc
                _validate_document_reference(reference, document)
                entries.append(
                    VerifiedTranscriptProjectionDocumentViewEntry(
                        reference=reference,
                        document=document,
                    )
                )
        frozen_entries = tuple(entries)
        return VerifiedTranscriptProjectionDocumentView(
            entries=frozen_entries,
            reference_fingerprints=tuple(
                item.reference.reference_fingerprint for item in frozen_entries
            ),
            view_fingerprint=context_fingerprint(
                "verified-transcript-projection-document-view:v1",
                {
                    "ordered_entries": tuple(
                        (
                            item.reference.reference_fingerprint,
                            item.document.fact_fingerprint,
                        )
                        for item in frozen_entries
                    )
                },
            ),
        )


class TranscriptProjectionStateStore:
    """Pure incremental reducer with an explicit hydrated-document input port."""

    def __init__(
        self,
        *,
        runtime_session_id: str,
        documents: TranscriptProjectionDocumentRegistry,
    ) -> None:
        self.runtime_session_id = runtime_session_id
        self.documents = documents
        self._lock = RLock()
        self._reset()

    @property
    def through_sequence(self) -> int:
        with self._lock:
            return self._through_sequence

    def snapshot(self) -> TranscriptProjectionLiveAssemblyState:
        with self._lock:
            return self._snapshot_unlocked()

    def stable_entries(self) -> tuple[TranscriptProjectionLeafEntryFact, ...]:
        with self._lock:
            return tuple(self._stable_entries)

    def placement_spine_snapshot(self) -> CanonicalTranscriptPlacementSpineSnapshot:
        with self._lock:
            entries = tuple(self._stable_entries)
            anchors = tuple(self._placement_anchors)
            if len(entries) != len(anchors):
                raise ValueError(
                    "transcript placement spine is not aligned with leaves"
                )
            for entry, anchor in zip(entries, anchors, strict=True):
                expected = _build_anchor_reference(
                    runtime_session_id=self.runtime_session_id,
                    entry=entry,
                    first_coordinate=anchor.stable_first_spine_coordinate,
                    last_coordinate=anchor.stable_last_spine_coordinate,
                    stable_anchor_slot_key=anchor.stable_anchor_slot_key,
                    transcript_anchor_id=anchor.transcript_anchor_id,
                )
                if expected != anchor:
                    raise ValueError("transcript placement anchor/leaf join drifted")
            spine = _canonical_spine_fingerprint(anchors=anchors)
            return CanonicalTranscriptPlacementSpineSnapshot(
                runtime_session_id=self.runtime_session_id,
                through_sequence=self._through_sequence,
                stable_entries=entries,
                ordered_anchor_references=anchors,
                canonical_spine_fingerprint=spine,
                snapshot_fingerprint=context_fingerprint(
                    "canonical-transcript-placement-spine-snapshot:v1",
                    {
                        "runtime_session_id": self.runtime_session_id,
                        "through_sequence": self._through_sequence,
                        "ordered_entry_fact_fingerprints": tuple(
                            item.fact_fingerprint for item in entries
                        ),
                        "ordered_anchor_reference_fingerprints": tuple(
                            item.anchor_reference_fingerprint for item in anchors
                        ),
                        "canonical_spine_fingerprint": spine,
                    },
                ),
            )

    def restore_placement_spine(
        self,
        anchors: tuple[CanonicalTranscriptPlacementAnchorReferenceFact, ...],
    ) -> None:
        """Install a checkpoint-proved stable spine after semantic restore."""

        with self._lock:
            entries = tuple(self._stable_entries)
            if len(entries) != len(anchors):
                raise ValueError("restored transcript placement spine length mismatch")
            for entry, anchor in zip(entries, anchors, strict=True):
                expected = _build_anchor_reference(
                    runtime_session_id=self.runtime_session_id,
                    entry=entry,
                    first_coordinate=anchor.stable_first_spine_coordinate,
                    last_coordinate=anchor.stable_last_spine_coordinate,
                    stable_anchor_slot_key=anchor.stable_anchor_slot_key,
                    transcript_anchor_id=anchor.transcript_anchor_id,
                )
                if expected != anchor:
                    raise ValueError("restored transcript placement anchor mismatch")
            _canonical_spine_fingerprint(anchors=anchors)
            self._placement_anchors = list(anchors)

    def final_assistant_projection(
        self,
        *,
        run_id: str,
        through_sequence: int,
    ) -> CanonicalRunFinalAssistantProjection | None:
        """Return the latest accepted non-tool assistant message for one run.

        The resident store is itself restored from a canonical checkpoint plus a
        bounded semantic delta.  This query therefore never reconstructs a run
        from its physical EventLog range.
        """

        with self._lock:
            if through_sequence > self._through_sequence:
                raise ValueError(
                    "transcript projection has not reached final-output horizon"
                )
            for entry in reversed(self._stable_entries):
                if not isinstance(entry, TranscriptMessageLeafEntryFact):
                    continue
                attribution = entry.attribution
                if (
                    attribution.run_id != run_id
                    or attribution.segment != "current_run_tail"
                    or entry.semantic_identity.message_provider_semantic_identity.role
                    != "assistant"
                    or any(
                        reference.sequence > through_sequence
                        for reference in entry.source_event_refs
                    )
                ):
                    continue
                content = entry.content
                if not isinstance(content, TerminalProjectionMessageContentRefFact):
                    raise ValueError(
                        "canonical assistant entry lacks terminal projection authority"
                    )
                document = self.documents.resolve(content.projection_reference)
                payload = document.payload
                if not isinstance(payload, ModelTerminalProjectionPayloadFact):
                    raise ValueError(
                        "canonical assistant entry points at non-model projection"
                    )
                selected = frozenset(content.selected_projection_orders)
                if any(
                    isinstance(item.semantic_identity, ModelToolCallBlockSemanticFact)
                    for item in payload.items
                    if item.semantic_identity.projection_order in selected
                ):
                    continue
                return CanonicalRunFinalAssistantProjection(
                    entry=entry,
                    document=document,
                )
        return None

    def evidence_snapshot(self) -> TranscriptProjectionReducerEvidenceSnapshot:
        """Freeze live state, stable entries and required documents under one lock."""

        with self._lock:
            live_state = self._snapshot_unlocked()
            stable_entries = tuple(self._stable_entries)
            references: list[TerminalProjectionReferenceFact] = []
            seen: set[str] = set()
            for reference in stable_entry_projection_references(stable_entries):
                if reference.reference_fingerprint in seen:
                    continue
                seen.add(reference.reference_fingerprint)
                references.append(reference)
            required_references = tuple(references)
            return TranscriptProjectionReducerEvidenceSnapshot(
                live_state=live_state,
                stable_entries=stable_entries,
                required_projection_references=required_references,
                snapshot_fingerprint=context_fingerprint(
                    "transcript-projection-reducer-evidence-snapshot:v1",
                    {
                        "live_assembly_fingerprint": live_state.assembly_fingerprint,
                        "ordered_stable_entry_fact_fingerprints": tuple(
                            entry.fact_fingerprint for entry in stable_entries
                        ),
                        "ordered_required_projection_reference_fingerprints": tuple(
                            reference.reference_fingerprint
                            for reference in required_references
                        ),
                    },
                ),
            )

    def capture_governance_authority_snapshot(
        self,
    ) -> GovernanceTranscriptAuthoritySnapshot:
        """Freeze reducer evidence, hydrated documents, and H under one lock."""

        with self._lock:
            live_state = self._snapshot_unlocked()
            stable_entries = tuple(self._stable_entries)
            references: list[TerminalProjectionReferenceFact] = []
            seen: set[str] = set()
            for reference in stable_entry_projection_references(stable_entries):
                if reference.reference_fingerprint in seen:
                    continue
                seen.add(reference.reference_fingerprint)
                references.append(reference)
            required_references = tuple(references)
            reducer_snapshot = TranscriptProjectionReducerEvidenceSnapshot(
                live_state=live_state,
                stable_entries=stable_entries,
                required_projection_references=required_references,
                snapshot_fingerprint=context_fingerprint(
                    "transcript-projection-reducer-evidence-snapshot:v1",
                    {
                        "live_assembly_fingerprint": live_state.assembly_fingerprint,
                        "ordered_stable_entry_fact_fingerprints": tuple(
                            entry.fact_fingerprint for entry in stable_entries
                        ),
                        "ordered_required_projection_reference_fingerprints": tuple(
                            reference.reference_fingerprint
                            for reference in required_references
                        ),
                    },
                ),
            )
            document_view = self.documents.freeze_references(required_references)
            payload = {
                "reducer_evidence_snapshot_fingerprint": reducer_snapshot.snapshot_fingerprint,
                "document_view_fingerprint": document_view.view_fingerprint,
                "ledger_through_sequence": self._through_sequence,
                "ledger_continuity_accumulator": self._ledger_continuity_accumulator,
                "transcript_semantic_event_count": self._semantic_event_count,
                "transcript_semantic_accumulator": self._semantic_accumulator,
            }
            return GovernanceTranscriptAuthoritySnapshot(
                reducer_evidence_snapshot=reducer_snapshot,
                document_view=document_view,
                ledger_through_sequence=self._through_sequence,
                ledger_continuity_accumulator=self._ledger_continuity_accumulator,
                transcript_semantic_event_count=self._semantic_event_count,
                transcript_semantic_accumulator=self._semantic_accumulator,
                snapshot_fingerprint=context_fingerprint(
                    "governance-transcript-authority-snapshot:v1",
                    payload,
                ),
            )

    def unresolved_completed_call_ids(self, run_id: str) -> tuple[str, ...]:
        """Return completed projections that still lack their durable disposition."""

        with self._lock:
            return tuple(
                sorted(
                    call_id
                    for call_id, record in self._pending_models.items()
                    if record.committed_event.run_id == run_id
                    and call_id in self._pending_dispositions
                )
            )

    def apply_live_committed(
        self,
        receipt: StoredEventBatchCommitReceipt,
    ) -> LiveCommittedFoldResult:
        """Fold one exact physical FULL receipt without re-encoding events."""

        receipt.__post_init__()
        with self._lock:
            fold_delta, source_accumulator = self._fold_joined_pairs(
                owned_events=receipt.owned_stored_events,
                raw_envelopes=receipt.raw_stored_envelopes,
            )
        first = receipt.raw_stored_envelopes[0].sequence
        last = receipt.raw_stored_envelopes[-1].sequence
        payload = {
            "source_stored_batch_ordered_join_fingerprint": (
                receipt.ordered_join_fingerprint
            ),
            "source_first_sequence": first,
            "source_last_sequence": last,
            "source_envelope_accumulator": source_accumulator,
            "fold_delta_fingerprint": fold_delta.fold_delta_fingerprint,
        }
        return LiveCommittedFoldResult(
            source_stored_batch_ordered_join_fingerprint=(
                receipt.ordered_join_fingerprint
            ),
            source_first_sequence=first,
            source_last_sequence=last,
            source_envelope_accumulator=source_accumulator,
            fold_delta=fold_delta,
            live_result_fingerprint=context_fingerprint(
                "live-committed-transcript-fold-result:v1", payload
            ),
        )

    def fold_restored_range(
        self,
        range_proof: JoinedRawStoredEventRangeProof,
    ) -> RestoredRangeFoldResult:
        """Fold one contiguous historical range without inventing a batch receipt."""

        range_proof.__post_init__()
        with self._lock:
            fold_delta, source_accumulator = self._fold_joined_pairs(
                owned_events=range_proof.owned_stored_events,
                raw_envelopes=range_proof.raw_stored_envelopes,
            )
        payload = {
            "source_range_proof_fingerprint": range_proof.range_proof_fingerprint,
            "source_first_sequence": range_proof.from_sequence_exclusive + 1,
            "source_last_sequence": range_proof.through_sequence,
            "source_envelope_accumulator": source_accumulator,
            "fold_delta_fingerprint": fold_delta.fold_delta_fingerprint,
        }
        return RestoredRangeFoldResult(
            source_range_proof_fingerprint=range_proof.range_proof_fingerprint,
            source_first_sequence=range_proof.from_sequence_exclusive + 1,
            source_last_sequence=range_proof.through_sequence,
            source_envelope_accumulator=source_accumulator,
            fold_delta=fold_delta,
            restored_result_fingerprint=context_fingerprint(
                "restored-range-transcript-fold-result:v1", payload
            ),
        )

    def reset_for_rebuild(self) -> None:
        """Reset only for an explicit bounded repair/doctor range replay."""

        with self._lock:
            self._reset()

    def _fold_joined_pairs(
        self,
        *,
        owned_events: tuple[AgentEvent, ...],
        raw_envelopes: tuple[RawStoredEventEnvelope, ...],
    ) -> tuple[CanonicalTranscriptFoldDeltaFact, str]:
        if not owned_events or len(owned_events) != len(raw_envelopes):
            raise ValueError("transcript fold requires a non-empty joined range")
        first = raw_envelopes[0].sequence
        last = raw_envelopes[-1].sequence
        if first != self._through_sequence + 1:
            raise ValueError("transcript fold source does not start at H + 1")
        if tuple(item.sequence for item in raw_envelopes) != tuple(
            range(first, last + 1)
        ):
            raise ValueError("transcript fold source is not contiguous")
        before_mutable = self._capture_mutable_state()
        before_snapshot = self._snapshot_unlocked()
        before_entries = tuple(self._stable_entries)
        before_anchors = tuple(self._placement_anchors)
        self._active_fold_audit_dispositions: list[TranscriptAuditDispositionFact] = []
        try:
            for event, raw in zip(owned_events, raw_envelopes, strict=True):
                self._apply_contiguous(event, raw)
            after_snapshot = self._snapshot_unlocked()
            after_entries = tuple(self._stable_entries)
            leaf_changes, placement_proofs, after_anchors = self._derive_fold_changes(
                before_entries=before_entries,
                before_anchors=before_anchors,
                after_entries=after_entries,
            )
            self._placement_anchors = list(after_anchors)
            audit_dispositions = tuple(self._active_fold_audit_dispositions)
            source_accumulator = _fold_source_accumulator(
                runtime_session_id=self.runtime_session_id,
                raw_envelopes=raw_envelopes,
            )
            before_spine = _canonical_spine_fingerprint(
                anchors=before_anchors,
            )
            after_spine = _canonical_spine_fingerprint(
                anchors=after_anchors,
            )
            resulting_state_fingerprint = context_fingerprint(
                "canonical-transcript-resulting-state:v1",
                {
                    "runtime_session_id": self.runtime_session_id,
                    "through_sequence": last,
                    "live_assembly_fingerprint": after_snapshot.assembly_fingerprint,
                    "stable_state_fingerprint": (
                        after_snapshot.stable_semantic_state.state_semantic_fingerprint
                    ),
                    "canonical_spine_fingerprint": after_spine,
                },
            )
            delta_payload = {
                "runtime_session_id": self.runtime_session_id,
                "from_sequence_exclusive": first - 1,
                "through_sequence": last,
                "reducer_contract_fingerprint": (
                    TRANSCRIPT_PROJECTION_REDUCER_CONTRACT_FINGERPRINT
                ),
                "event_registry_contract_fingerprint": (
                    TRANSCRIPT_EVENT_REGISTRY_CONTRACT_FINGERPRINT
                ),
                "before_live_assembly_fingerprint": (
                    before_snapshot.assembly_fingerprint
                ),
                "after_live_assembly_fingerprint": after_snapshot.assembly_fingerprint,
                "before_stable_state_fingerprint": (
                    before_snapshot.stable_semantic_state.state_semantic_fingerprint
                ),
                "after_stable_state_fingerprint": (
                    after_snapshot.stable_semantic_state.state_semantic_fingerprint
                ),
                "before_canonical_spine_fingerprint": before_spine,
                "after_canonical_spine_fingerprint": after_spine,
                "ordered_leaf_change_fingerprints": tuple(
                    item.change_fingerprint for item in leaf_changes
                ),
                "ordered_placement_transition_fingerprints": tuple(
                    item.transition_proof_fingerprint for item in placement_proofs
                ),
                "ordered_audit_disposition_fingerprints": tuple(
                    item.disposition_fingerprint for item in audit_dispositions
                ),
                "resulting_canonical_state_fingerprint": (resulting_state_fingerprint),
            }
            fold_delta = CanonicalTranscriptFoldDeltaFact(
                schema_version="canonical_transcript_fold_delta.v1",
                runtime_session_id=self.runtime_session_id,
                from_sequence_exclusive=first - 1,
                through_sequence=last,
                reducer_contract_fingerprint=(
                    TRANSCRIPT_PROJECTION_REDUCER_CONTRACT_FINGERPRINT
                ),
                event_registry_contract_fingerprint=(
                    TRANSCRIPT_EVENT_REGISTRY_CONTRACT_FINGERPRINT
                ),
                before_live_assembly_fingerprint=(before_snapshot.assembly_fingerprint),
                after_live_assembly_fingerprint=after_snapshot.assembly_fingerprint,
                before_stable_state_fingerprint=(
                    before_snapshot.stable_semantic_state.state_semantic_fingerprint
                ),
                after_stable_state_fingerprint=(
                    after_snapshot.stable_semantic_state.state_semantic_fingerprint
                ),
                before_canonical_spine_fingerprint=before_spine,
                after_canonical_spine_fingerprint=after_spine,
                ordered_leaf_changes=leaf_changes,
                ordered_placement_transition_proofs=placement_proofs,
                ordered_audit_dispositions=audit_dispositions,
                resulting_canonical_state_fingerprint=resulting_state_fingerprint,
                fold_delta_fingerprint=context_fingerprint(
                    "canonical-transcript-fold-delta:v1", delta_payload
                ),
            )
        except BaseException:
            self._restore_mutable_state(before_mutable)
            raise
        finally:
            del self._active_fold_audit_dispositions
        return fold_delta, source_accumulator

    def restore_sparse(self, snapshot: RawTranscriptDomainDeltaSnapshot) -> None:
        if snapshot.runtime_session_id != self.runtime_session_id:
            raise ValueError("transcript sparse restore session mismatch")
        if snapshot.before.through_sequence != 0:
            raise ValueError("seedless AP2 sparse restore must begin at ledger genesis")
        with self._lock:
            lifecycle_kinds = dict(self._model_lifecycle_kinds)
            self._reset()
            self._model_lifecycle_kinds.update(lifecycle_kinds)
            for raw in snapshot.semantic_events:
                self._apply_semantic(
                    decode_raw_stored_event_envelope(
                        raw, DEFAULT_EVENT_SCHEMA_REGISTRY
                    ),
                    raw,
                )
            self._through_sequence = snapshot.after.through_sequence
            self._ledger_continuity_accumulator = (
                snapshot.after.ledger_continuity_accumulator
            )
            self._semantic_event_count = snapshot.after.semantic_event_count
            self._semantic_accumulator = snapshot.after.semantic_accumulator
            self._placement_anchors = list(
                _initial_anchor_references(
                    runtime_session_id=self.runtime_session_id,
                    entries=tuple(self._stable_entries),
                )
            )

    def restore_from_stable_base(
        self,
        *,
        stable_state: TranscriptProjectionStableSemanticStateFact,
        stable_entries: tuple[TranscriptProjectionLeafEntryFact, ...],
        ledger_through_sequence: int,
        ledger_continuity_accumulator: str,
        delta: RawTranscriptDomainDeltaSnapshot,
        model_start_events: tuple[ModelCallStartEvent, ...],
    ) -> None:
        """Restore a verified stable projection and fold only its semantic delta."""

        if delta.runtime_session_id != self.runtime_session_id:
            raise ValueError("transcript stable restore session mismatch")
        if delta.before.through_sequence != ledger_through_sequence:
            raise ValueError("transcript stable restore high-water mismatch")
        if delta.before.ledger_continuity_accumulator != ledger_continuity_accumulator:
            raise ValueError("transcript stable restore continuity mismatch")
        if (
            delta.before.semantic_event_count
            != stable_state.semantic_source_event_count
            or delta.before.semantic_accumulator
            != stable_state.semantic_source_accumulator
        ):
            raise ValueError("transcript stable restore semantic prefix mismatch")
        normalized = context_fingerprint(
            "normalized-transcript-semantic:v1",
            tuple(
                entry.semantic_identity.semantic_fingerprint for entry in stable_entries
            ),
        )
        if normalized != stable_state.normalized_transcript_fingerprint:
            raise ValueError("transcript stable restore entry fingerprint mismatch")
        expected_state = build_frozen_fact(
            TranscriptProjectionStableSemanticStateFact,
            schema_version="transcript_projection_stable_semantic_state.v1",
            semantic_source_event_count=stable_state.semantic_source_event_count,
            semantic_source_accumulator=stable_state.semantic_source_accumulator,
            normalized_transcript_fingerprint=normalized,
        )
        if expected_state != stable_state:
            raise ValueError("transcript stable restore state fingerprint mismatch")

        with self._lock:
            self._reset()
            self._stable_entries.extend(stable_entries)
            self._through_sequence = ledger_through_sequence
            self._ledger_continuity_accumulator = ledger_continuity_accumulator
            self._semantic_event_count = stable_state.semantic_source_event_count
            self._semantic_accumulator = stable_state.semantic_source_accumulator
            for event in model_start_events:
                self.register_model_start(event)
            for raw in delta.semantic_events:
                self._apply_semantic(
                    decode_raw_stored_event_envelope(
                        raw, DEFAULT_EVENT_SCHEMA_REGISTRY
                    ),
                    raw,
                )
            self._through_sequence = delta.after.through_sequence
            self._ledger_continuity_accumulator = (
                delta.after.ledger_continuity_accumulator
            )
            self._semantic_event_count = delta.after.semantic_event_count
            self._semantic_accumulator = delta.after.semantic_accumulator
            self._placement_anchors = list(
                _initial_anchor_references(
                    runtime_session_id=self.runtime_session_id,
                    entries=tuple(self._stable_entries),
                )
            )

    def _reset(self) -> None:
        self._through_sequence = 0
        self._ledger_continuity_accumulator = EMPTY_LEDGER_CONTINUITY_ACCUMULATOR
        self._semantic_event_count = 0
        self._semantic_accumulator = EMPTY_TRANSCRIPT_SEMANTIC_ACCUMULATOR
        self._stable_components: list[str] = []
        self._stable_entries: list[TranscriptProjectionLeafEntryFact] = []
        self._placement_anchors: list[
            CanonicalTranscriptPlacementAnchorReferenceFact
        ] = []
        self._pending_models: dict[str, _ProjectionRecord] = {}
        self._model_lifecycle_kinds: dict[str, str] = {}
        self._pending_dispositions: set[str] = set()
        self._accepted_model_assemblies: dict[str, _AcceptedModelAssembly] = {}
        self._tool_call_owners: dict[str, str] = {}
        self._pending_tool_results: dict[str, _ProjectionRecord] = {}
        self._suspended_tool_calls: set[str] = set()
        self._pending_external: set[str] = set()

    def _capture_mutable_state(self) -> tuple[object, ...]:
        return (
            self._through_sequence,
            self._ledger_continuity_accumulator,
            self._semantic_event_count,
            self._semantic_accumulator,
            list(self._stable_components),
            list(self._stable_entries),
            list(self._placement_anchors),
            dict(self._pending_models),
            dict(self._model_lifecycle_kinds),
            set(self._pending_dispositions),
            deepcopy(self._accepted_model_assemblies),
            dict(self._tool_call_owners),
            dict(self._pending_tool_results),
            set(self._suspended_tool_calls),
            set(self._pending_external),
        )

    def _restore_mutable_state(self, state: tuple[object, ...]) -> None:
        (
            self._through_sequence,
            self._ledger_continuity_accumulator,
            self._semantic_event_count,
            self._semantic_accumulator,
            self._stable_components,
            self._stable_entries,
            self._placement_anchors,
            self._pending_models,
            self._model_lifecycle_kinds,
            self._pending_dispositions,
            self._accepted_model_assemblies,
            self._tool_call_owners,
            self._pending_tool_results,
            self._suspended_tool_calls,
            self._pending_external,
        ) = state

    def _record_audit_disposition(
        self,
        *,
        disposition_kind: Literal[
            "suppressed_model_output",
            "recovered_transcript",
            "rejected_transcript_candidate",
        ],
        event: AgentEvent,
        reason_code: str,
    ) -> None:
        target = getattr(self, "_active_fold_audit_dispositions", None)
        if target is None:
            return
        if event.sequence is None:
            raise ValueError("transcript audit disposition requires committed source")
        payload = {
            "disposition_kind": disposition_kind,
            "source_event_id": event.id,
            "source_sequence": event.sequence,
            "reason_code": reason_code,
        }
        target.append(
            TranscriptAuditDispositionFact(
                schema_version="transcript_audit_disposition.v1",
                disposition_kind=disposition_kind,
                source_event_id=event.id,
                source_sequence=event.sequence,
                reason_code=reason_code,
                disposition_fingerprint=context_fingerprint(
                    "transcript-audit-disposition:v1", payload
                ),
            )
        )

    def _derive_fold_changes(
        self,
        *,
        before_entries: tuple[TranscriptProjectionLeafEntryFact, ...],
        before_anchors: tuple[CanonicalTranscriptPlacementAnchorReferenceFact, ...],
        after_entries: tuple[TranscriptProjectionLeafEntryFact, ...],
    ) -> tuple[
        tuple[CanonicalTranscriptLeafChangeFact, ...],
        tuple[CanonicalTranscriptPlacementTransitionProofFact, ...],
        tuple[CanonicalTranscriptPlacementAnchorReferenceFact, ...],
    ]:
        if len(before_entries) != len(before_anchors):
            raise ValueError("transcript placement spine is not aligned with leaves")
        before_spine = _canonical_spine_fingerprint(anchors=before_anchors)
        prefix = 0
        while (
            prefix < len(before_entries)
            and prefix < len(after_entries)
            and before_entries[prefix] == after_entries[prefix]
        ):
            prefix += 1
        suffix = 0
        while (
            suffix < len(before_entries) - prefix
            and suffix < len(after_entries) - prefix
            and before_entries[-1 - suffix] == after_entries[-1 - suffix]
        ):
            suffix += 1
        old_middle_end = len(before_entries) - suffix
        new_middle_end = len(after_entries) - suffix
        old_middle = before_entries[prefix:old_middle_end]
        new_middle = after_entries[prefix:new_middle_end]
        old_middle_anchors = before_anchors[prefix:old_middle_end]

        if not old_middle and not new_middle:
            return (), (), before_anchors

        changes: list[CanonicalTranscriptLeafChangeFact] = []
        transitions: list[CanonicalTranscriptPlacementTransitionProofFact] = []
        resulting_anchors = list(before_anchors[:prefix])
        retained_suffix = before_anchors[old_middle_end:]

        if not old_middle and prefix == len(before_entries):
            next_coordinate = (
                before_anchors[-1].stable_last_spine_coordinate + 1
                if before_anchors
                else 1
            )
            for entry in new_middle:
                anchor = _build_anchor_reference(
                    runtime_session_id=self.runtime_session_id,
                    entry=entry,
                    first_coordinate=next_coordinate,
                    last_coordinate=next_coordinate,
                )
                resulting_anchors.append(anchor)
                next_coordinate += 1
                changes.append(_leaf_change("append", None, entry))
            after_anchors = tuple(resulting_anchors)
            after_spine = _canonical_spine_fingerprint(anchors=after_anchors)
            for anchor in after_anchors[len(before_anchors) :]:
                transitions.append(
                    _placement_transition(
                        transition_kind="append",
                        before_spine=before_spine,
                        after_spine=after_spine,
                        predecessors=(),
                        resulting=anchor,
                        tombstones=(),
                    )
                )
            return tuple(changes), tuple(transitions), after_anchors

        if len(old_middle) == len(new_middle):
            for old_entry, new_entry, old_anchor in zip(
                old_middle, new_middle, old_middle_anchors, strict=True
            ):
                if old_entry == new_entry:
                    resulting_anchors.append(old_anchor)
                    continue
                anchor = _build_anchor_reference(
                    runtime_session_id=self.runtime_session_id,
                    entry=new_entry,
                    first_coordinate=old_anchor.stable_first_spine_coordinate,
                    last_coordinate=old_anchor.stable_last_spine_coordinate,
                    stable_anchor_slot_key=old_anchor.stable_anchor_slot_key,
                    transcript_anchor_id=old_anchor.transcript_anchor_id,
                )
                resulting_anchors.append(anchor)
                changes.append(_leaf_change("replace", old_entry, new_entry))
            resulting_anchors.extend(retained_suffix)
            after_anchors = tuple(resulting_anchors)
            after_spine = _canonical_spine_fingerprint(anchors=after_anchors)
            for old_entry, new_entry, old_anchor, new_anchor in zip(
                old_middle,
                new_middle,
                old_middle_anchors,
                after_anchors[prefix:new_middle_end],
                strict=True,
            ):
                if old_entry == new_entry:
                    continue
                transitions.append(
                    _placement_transition(
                        transition_kind="single_replace",
                        before_spine=before_spine,
                        after_spine=after_spine,
                        predecessors=(old_anchor,),
                        resulting=new_anchor,
                        tombstones=(),
                    )
                )
            return tuple(changes), tuple(transitions), after_anchors

        if old_middle and len(new_middle) == 1:
            inherited_slot = context_fingerprint(
                "canonical-transcript-interval-anchor-slot:v1",
                tuple(item.stable_anchor_slot_key for item in old_middle_anchors),
            )
            anchor = _build_anchor_reference(
                runtime_session_id=self.runtime_session_id,
                entry=new_middle[0],
                first_coordinate=(old_middle_anchors[0].stable_first_spine_coordinate),
                last_coordinate=old_middle_anchors[-1].stable_last_spine_coordinate,
                stable_anchor_slot_key=inherited_slot,
            )
            resulting_anchors.append(anchor)
            resulting_anchors.extend(retained_suffix)
            for old_entry in old_middle:
                changes.append(_leaf_change("retire", old_entry, None))
            changes.append(_leaf_change("append", None, new_middle[0]))
            after_anchors = tuple(resulting_anchors)
            after_spine = _canonical_spine_fingerprint(anchors=after_anchors)
            transitions.append(
                _placement_transition(
                    transition_kind="interval_replace",
                    before_spine=before_spine,
                    after_spine=after_spine,
                    predecessors=old_middle_anchors,
                    resulting=anchor,
                    tombstones=(),
                )
            )
            return tuple(changes), tuple(transitions), after_anchors

        if old_middle and not new_middle:
            resulting_anchors.extend(retained_suffix)
            after_anchors = tuple(resulting_anchors)
            after_spine = _canonical_spine_fingerprint(anchors=after_anchors)
            tombstones = tuple(
                _anchor_tombstone(
                    item,
                    retired_by_source_reference_fingerprint=context_fingerprint(
                        "transcript-anchor-retirement-source:v1",
                        (before_spine, after_spine),
                    ),
                )
                for item in old_middle_anchors
            )
            for old_entry in old_middle:
                changes.append(_leaf_change("retire", old_entry, None))
            transitions.append(
                _placement_transition(
                    transition_kind="retire_to_tombstone",
                    before_spine=before_spine,
                    after_spine=after_spine,
                    predecessors=old_middle_anchors,
                    resulting=None,
                    tombstones=tombstones,
                )
            )
            return tuple(changes), tuple(transitions), after_anchors

        raise ValueError("unsupported canonical transcript spine mutation")

    def _apply_contiguous(
        self,
        event: AgentEvent,
        raw: RawStoredEventEnvelope,
    ) -> None:
        if event.sequence != self._through_sequence + 1:
            raise ValueError("transcript projection committed fold is not contiguous")
        _validate_event_raw_join(event, raw, self.runtime_session_id)
        self._ledger_continuity_accumulator = advance_ledger_continuity_accumulator(
            self._ledger_continuity_accumulator,
            envelope_fingerprint=raw.envelope_fingerprint,
        )
        if isinstance(event, ModelCallStartEvent):
            self.register_model_start(event)
        if classify_transcript_event_type(raw.event_type) == "transcript_semantic":
            self._semantic_event_count += 1
            self._semantic_accumulator = advance_transcript_semantic_accumulator(
                self._semantic_accumulator,
                event=event,
                event_schema_version=raw.event_schema_version,
                event_schema_fingerprint=raw.event_schema_fingerprint,
            )
            self._apply_semantic(event, raw)
        self._through_sequence = raw.sequence

    def _apply_semantic(
        self,
        event: AgentEvent,
        raw: RawStoredEventEnvelope,
    ) -> None:
        _validate_event_raw_join(event, raw, self.runtime_session_id)
        if isinstance(event, RunStartEvent):
            self._append_current_user(event, raw)
            self._append_run_ingress_notifications(event, raw)
            return
        if isinstance(event, UserSteerCommittedEvent):
            self._append_user_steer(event, raw)
            return
        if isinstance(event, RunEndEvent):
            self._discard_incomplete_run_assemblies(event.run_id)
            self._append_run_recovery_note(event, raw)
            return
        if isinstance(event, ModelCallTerminalProjectionCommittedEvent):
            if event.sequence is None:
                raise ValueError("model projection requires committed sequence")
            call_id = event.resolved_model_call_id
            try:
                lifecycle_kind = self._model_lifecycle_kinds.pop(call_id)
            except KeyError as exc:
                raise ValueError(
                    "model terminal projection has no exact Start lifecycle fact"
                ) from exc
            if lifecycle_kind != "main_assistant_reply":
                return
            document = self.documents.resolve(event.projection_reference)
            if document.semantic_identity.terminal_outcome != "completed":
                # Non-completed provider streams are durable audit facts only.
                # They never wait for control disposition and never enter the
                # canonical transcript.
                self._record_audit_disposition(
                    disposition_kind="suppressed_model_output",
                    event=event,
                    reason_code=(
                        f"model_terminal_{document.semantic_identity.terminal_outcome}"
                    ),
                )
                return
            if call_id in self._pending_models:
                raise ValueError("duplicate pending model terminal projection")
            self._pending_models[call_id] = _ProjectionRecord(
                event.projection_reference,
                document,
                event.sequence,
                event,
                raw,
            )
            self._pending_dispositions.add(call_id)
            return
        if isinstance(event, ModelCallControlDispositionResolvedEvent):
            self._resolve_model_disposition(event, raw)
            return
        if isinstance(event, ToolResultTerminalProjectionCommittedEvent):
            if event.sequence is None:
                raise ValueError("tool projection requires committed sequence")
            record = _ProjectionRecord(
                event.projection_reference,
                self.documents.resolve(event.projection_reference),
                event.sequence,
                event,
                raw,
            )
            self._accept_or_defer_tool_result(event.tool_call_id, record)
            self._suspended_tool_calls.discard(event.tool_call_id)
            return
        if isinstance(event, ToolExecutionSuspendedEvent):
            self._suspended_tool_calls.add(event.tool_call_id)
            return
        if isinstance(event, RequireExternalExecutionEvent):
            self._pending_external.update(
                item.tool_call_id for item in event.external_tool_calls
            )
            return
        if isinstance(event, ExternalExecutionResultEvent):
            self._pending_external.difference_update(
                item.result_block.tool_call_id for item in event.external_results
            )

    def register_model_start(self, event: ModelCallStartEvent) -> None:
        call_id = event.resolved_call.resolved_model_call_id
        lifecycle_kind = event.recovery_plan.lifecycle_kind
        existing = self._model_lifecycle_kinds.get(call_id)
        if existing is not None and existing != lifecycle_kind:
            raise ValueError("model Start lifecycle identity drifted")
        self._model_lifecycle_kinds[call_id] = lifecycle_kind

    def _resolve_model_disposition(
        self,
        event: ModelCallControlDispositionResolvedEvent,
        raw: RawStoredEventEnvelope,
    ) -> None:
        call_id = event.resolved_model_call_id
        record = self._pending_models.pop(call_id, None)
        if record is None or call_id not in self._pending_dispositions:
            raise ValueError("model control disposition has no pending projection")
        self._pending_dispositions.remove(call_id)
        if event.disposition is not ModelCallControlDisposition.ACCEPTED:
            self._record_audit_disposition(
                disposition_kind="suppressed_model_output",
                event=event,
                reason_code=f"control_disposition_{event.disposition.value}",
            )
            return
        semantic_join = record.reference.semantic_join
        if (
            semantic_join.projection_kind != "model_call"
            or semantic_join.terminal_outcome != "completed"
        ):
            raise ValueError("only completed model projection can be accepted")
        payload = record.document.payload
        if not isinstance(payload, ModelTerminalProjectionPayloadFact):
            raise ValueError("model projection document payload kind drifted")
        calls = tuple(
            item.semantic_identity
            for item in payload.items
            if isinstance(item.semantic_identity, ModelToolCallBlockSemanticFact)
        )
        if not calls:
            self._stable_components.append(semantic_join.semantic_fingerprint)
            self._append_model_message(
                record,
                disposition_event=event,
                disposition_raw=raw,
            )
            return
        if call_id in self._accepted_model_assemblies:
            raise ValueError("duplicate accepted model assembly")
        assembly = _AcceptedModelAssembly(
            record=record,
            disposition_event=event,
            disposition_raw_stored_envelope=raw,
            tool_calls=calls,
            results={},
        )
        self._accepted_model_assemblies[call_id] = assembly
        for semantic in calls:
            if semantic.completion_status != "completed":
                raise ValueError("accepted model tool call is interrupted")
            if semantic.tool_call_id in self._tool_call_owners:
                raise ValueError("accepted model projection duplicates tool call")
            self._tool_call_owners[semantic.tool_call_id] = call_id
            deferred = self._pending_tool_results.pop(semantic.tool_call_id, None)
            if deferred is not None:
                assembly.results[semantic.tool_call_id] = deferred
        self._finalize_model_assembly_if_complete(call_id)

    def _accept_or_defer_tool_result(
        self,
        tool_call_id: str,
        record: _ProjectionRecord,
    ) -> None:
        owner = self._tool_call_owners.get(tool_call_id)
        if owner is not None:
            assembly = self._accepted_model_assemblies[owner]
            if tool_call_id in assembly.results:
                raise ValueError("duplicate tool result in accepted model assembly")
            assembly.results[tool_call_id] = record
            self._finalize_model_assembly_if_complete(owner)
            return
        if tool_call_id in self._pending_tool_results:
            raise ValueError("duplicate pending tool result projection")
        self._pending_tool_results[tool_call_id] = record

    def _finalize_model_assembly_if_complete(self, call_id: str) -> None:
        assembly = self._accepted_model_assemblies[call_id]
        expected = tuple(item.tool_call_id for item in assembly.tool_calls)
        if any(tool_call_id not in assembly.results for tool_call_id in expected):
            return
        semantic_join = assembly.record.reference.semantic_join
        self._stable_components.append(semantic_join.semantic_fingerprint)
        assistant_entry = self._append_model_message(
            assembly.record,
            disposition_event=assembly.disposition_event,
            disposition_raw=assembly.disposition_raw_stored_envelope,
        )
        call_block_position = len(self._stable_entries) - 1
        for semantic in assembly.tool_calls:
            self._append_tool_pair(
                semantic=semantic,
                assistant_entry=assistant_entry,
                call_block_position=call_block_position,
                record=assembly.results[semantic.tool_call_id],
            )
            self._tool_call_owners.pop(semantic.tool_call_id, None)
        self._accepted_model_assemblies.pop(call_id, None)

    def _append_tool_pair(
        self,
        *,
        semantic: ModelToolCallBlockSemanticFact,
        assistant_entry: TranscriptMessageLeafEntryFact,
        call_block_position: int,
        record: _ProjectionRecord,
    ) -> None:
        tool_call_id = semantic.tool_call_id
        join = record.reference.semantic_join
        if (
            join.projection_kind != "tool_result"
            or join.tool_call_id != tool_call_id
            or join.model_tool_name != semantic.tool_name
        ):
            raise ValueError("tool result projection does not match accepted call")
        self._stable_components.append(
            context_fingerprint(
                "transcript-tool-pair-semantic:v1",
                {
                    "tool_call_id": tool_call_id,
                    "call_semantic_fingerprint": semantic.semantic_fingerprint,
                    "result_semantic_fingerprint": join.semantic_fingerprint,
                },
            )
        )
        result_position = len(self._stable_entries)
        tool_semantic = build_frozen_fact(
            TranscriptToolResultLeafSemanticFact,
            schema_version="transcript_tool_result_leaf_semantic.v2",
            semantic_kind="tool_result_projection_ref",
            tool_call_id=join.tool_call_id,
            tool_name=join.model_tool_name,
            projection_semantic_identity=record.document.semantic_identity,
        )
        result_entry = build_frozen_fact(
            TranscriptToolResultLeafEntryFact,
            schema_version="transcript_tool_result_leaf_entry.v3",
            entry_kind="tool_result_projection_ref",
            ordinal=_ordinal(result_position),
            semantic_identity=tool_semantic,
            projection_reference=record.reference,
            source_event_refs=(
                _event_ref(
                    self.runtime_session_id,
                    record.committed_event,
                    record.raw_stored_envelope,
                ),
            ),
        )
        self._stable_entries.append(result_entry)
        pair_semantic = build_frozen_fact(
            TranscriptToolPairLeafSemanticFact,
            schema_version="transcript_tool_pair_leaf_semantic.v2",
            semantic_kind="tool_pair",
            assistant_tool_call_id=tool_call_id,
            tool_name=semantic.tool_name,
            assistant_message_semantic_fingerprint=(
                assistant_entry.semantic_identity.semantic_fingerprint
            ),
            tool_result_semantic_fingerprint=join.semantic_fingerprint,
            call_block_position=call_block_position,
            result_block_position=result_position,
        )
        pair_entry = build_frozen_fact(
            TranscriptToolPairLeafEntryFact,
            schema_version="transcript_tool_pair_leaf_entry.v3",
            entry_kind="tool_pair",
            ordinal=_ordinal(len(self._stable_entries)),
            pair_id=(
                "tool-pair:"
                + context_fingerprint(
                    "transcript-tool-pair-identity:v1",
                    {
                        "assistant_entry_fact_fingerprint": (
                            assistant_entry.fact_fingerprint
                        ),
                        "tool_call_id": tool_call_id,
                        "result_entry_fact_fingerprint": result_entry.fact_fingerprint,
                    },
                )
            ),
            semantic_identity=pair_semantic,
            source_event_refs=(
                _event_ref(
                    self.runtime_session_id,
                    record.committed_event,
                    record.raw_stored_envelope,
                ),
            ),
        )
        self._stable_entries.append(pair_entry)

    def _snapshot_unlocked(self) -> TranscriptProjectionLiveAssemblyState:
        normalized = context_fingerprint(
            "normalized-transcript-semantic:v1",
            tuple(
                entry.semantic_identity.semantic_fingerprint
                for entry in self._stable_entries
            ),
        )
        stable = build_frozen_fact(
            TranscriptProjectionStableSemanticStateFact,
            schema_version="transcript_projection_stable_semantic_state.v1",
            semantic_source_event_count=self._semantic_event_count,
            semantic_source_accumulator=self._semantic_accumulator,
            normalized_transcript_fingerprint=normalized,
        )
        pending_model_ids = tuple(sorted(self._pending_models))
        pending_dispositions = tuple(sorted(self._pending_dispositions))
        pending_calls = tuple(sorted(self._tool_call_owners))
        pending_results = tuple(sorted(self._pending_tool_results))
        pending_pairs = tuple(sorted(set(pending_calls) | set(pending_results)))
        suspended = tuple(sorted(self._suspended_tool_calls))
        external = tuple(sorted(self._pending_external))
        values = {
            "schema_version": "transcript_projection_live_assembly.v1",
            "stable_semantic_state": stable,
            "pending_model_projection_ids": pending_model_ids,
            "pending_model_disposition_call_ids": pending_dispositions,
            "pending_assistant_tool_call_ids": pending_calls,
            "pending_tool_result_projection_ids": pending_results,
            "pending_tool_pair_ids": pending_pairs,
            "suspended_tool_call_ids": suspended,
            "pending_external_requirement_ids": external,
            "ledger_through_sequence": self._through_sequence,
            "ledger_continuity_accumulator": self._ledger_continuity_accumulator,
            "transcript_semantic_event_count": self._semantic_event_count,
            "transcript_semantic_accumulator": self._semantic_accumulator,
            "checkpointable": not any(
                (
                    pending_model_ids,
                    pending_dispositions,
                    pending_calls,
                    pending_results,
                    pending_pairs,
                    suspended,
                    external,
                )
            ),
        }
        return TranscriptProjectionLiveAssemblyState(
            **values,
            assembly_fingerprint=context_fingerprint(
                "transcript-projection-live-assembly:v1",
                values,
            ),
        )

    def _discard_incomplete_run_assemblies(self, run_id: str) -> None:
        unresolved = self.unresolved_completed_call_ids(run_id)
        if unresolved:
            raise ValueError(
                "RunEnd cannot cross unresolved completed model projections: "
                + ", ".join(unresolved)
            )
        discarded = tuple(
            call_id
            for call_id, assembly in self._accepted_model_assemblies.items()
            if assembly.record.committed_event.run_id == run_id
        )
        for call_id in discarded:
            assembly = self._accepted_model_assemblies.pop(call_id)
            for semantic in assembly.tool_calls:
                self._tool_call_owners.pop(semantic.tool_call_id, None)
                self._pending_tool_results.pop(semantic.tool_call_id, None)
                self._suspended_tool_calls.discard(semantic.tool_call_id)

    def _append_current_user(
        self,
        event: RunStartEvent,
        raw: RawStoredEventEnvelope,
    ) -> None:
        current = event.current_user_message
        block_semantic, provider, _leaf = build_inline_text_message_semantics(
            text=current.text,
            role=(
                "user"
                if current.source_kind == "host_user_input"
                else "runtime_request"
            ),
            name=(
                "user"
                if current.source_kind == "host_user_input"
                else "terminal_process_observation"
                if current.source_kind == "host_runtime_request"
                else "subagent_task"
            ),
            segment="current_user",
        )
        block_attribution = build_frozen_fact(
            TranscriptInlineBlockAttributionFact,
            schema_version="transcript_inline_block_attribution.v1",
            block_id=f"text:{current.message_id}",
            block_index=0,
            source_projection_order=None,
        )
        block = build_frozen_fact(
            TranscriptInlineBlockFact,
            schema_version="transcript_inline_block.v1",
            provider_semantic_identity=block_semantic,
            attribution=block_attribution,
        )
        content = build_frozen_fact(
            InlineNormalizedMessageContentFact,
            schema_version="inline_normalized_message_content.v3",
            content_kind="inline_normalized_message",
            provider_semantic_identity=provider,
            blocks=(block,),
        )
        self._append_message_entry(
            provider=provider,
            attribution=build_frozen_fact(
                TranscriptMessageAttributionFact,
                schema_version="transcript_message_attribution.v2",
                message_id=current.message_id,
                run_id=event.run_id,
                turn_id=event.turn_id,
                reply_id=event.reply_id,
                created_at_utc=current.observed_at_utc,
                finished_at_utc=current.observed_at_utc,
                segment="current_user",
            ),
            content=content,
            source_event=event,
            source_raw=raw,
        )

    def _append_user_steer(
        self,
        event: UserSteerCommittedEvent,
        raw: RawStoredEventEnvelope,
    ) -> None:
        steer = event.steer
        block_semantic, provider, _leaf = build_inline_text_message_semantics(
            text=steer.canonical_utf8_text,
            role="user",
            name="user",
            segment="current_run_tail",
        )
        block = build_frozen_fact(
            TranscriptInlineBlockFact,
            schema_version="transcript_inline_block.v1",
            provider_semantic_identity=block_semantic,
            attribution=build_frozen_fact(
                TranscriptInlineBlockAttributionFact,
                schema_version="transcript_inline_block_attribution.v1",
                block_id=f"text:{steer.message_id}",
                block_index=0,
                source_projection_order=None,
            ),
        )
        self._append_message_entry(
            provider=provider,
            attribution=build_frozen_fact(
                TranscriptMessageAttributionFact,
                schema_version="transcript_message_attribution.v2",
                message_id=steer.message_id,
                run_id=event.run_id,
                turn_id=event.turn_id,
                reply_id=event.reply_id,
                created_at_utc=steer.observed_at_utc,
                finished_at_utc=steer.observed_at_utc,
                segment="current_run_tail",
            ),
            content=build_frozen_fact(
                InlineNormalizedMessageContentFact,
                schema_version="inline_normalized_message_content.v3",
                content_kind="inline_normalized_message",
                provider_semantic_identity=provider,
                blocks=(block,),
            ),
            source_event=event,
            source_raw=raw,
        )

    def _append_run_ingress_notifications(
        self,
        event: RunStartEvent,
        raw: RawStoredEventEnvelope,
    ) -> None:
        ingress = event.host_run_ingress
        if ingress is None:
            return
        attachments = (
            ingress.attached_runtime_notifications
            if ingress.ingress_kind == "human"
            else ingress.source_notifications
        )
        for index, attachment in enumerate(attachments):
            wire = canonical_runtime_observation_wire_from_semantic(
                attachment.observation_wire_semantic
            )
            block_semantic = build_frozen_fact(
                TranscriptProviderTextBlockSemanticFact,
                schema_version="transcript_provider_text_block_semantic.v1",
                block_kind="text",
                text=wire,
            )
            message_id = (
                f"terminal-notification:{event.id}:{index}:"
                f"{attachment.attachment_fingerprint.removeprefix('sha256:')[:16]}"
            )
            block = build_frozen_fact(
                TranscriptInlineBlockFact,
                schema_version="transcript_inline_block.v1",
                provider_semantic_identity=block_semantic,
                attribution=build_frozen_fact(
                    TranscriptInlineBlockAttributionFact,
                    schema_version="transcript_inline_block_attribution.v1",
                    block_id=f"text:{message_id}",
                    block_index=0,
                    source_projection_order=None,
                ),
            )
            provider = build_transcript_message_provider_semantic(
                role="runtime_observation",
                name="terminal_process_monitor_observation",
                segment="current_user",
                ordered_block_fingerprints=(block_semantic.semantic_fingerprint,),
            )
            self._append_message_entry(
                provider=provider,
                attribution=build_frozen_fact(
                    TranscriptMessageAttributionFact,
                    schema_version="transcript_message_attribution.v2",
                    message_id=message_id,
                    run_id=event.run_id,
                    turn_id=event.turn_id,
                    reply_id=event.reply_id,
                    created_at_utc=event.current_user_message.observed_at_utc,
                    finished_at_utc=event.current_user_message.observed_at_utc,
                    segment="current_user",
                ),
                content=build_frozen_fact(
                    InlineNormalizedMessageContentFact,
                    schema_version="inline_normalized_message_content.v3",
                    content_kind="inline_normalized_message",
                    provider_semantic_identity=provider,
                    blocks=(block,),
                ),
                source_event=event,
                source_raw=raw,
            )

    def _append_run_recovery_note(
        self,
        event: RunEndEvent,
        raw: RawStoredEventEnvelope,
    ) -> None:
        if event.status == "finished":
            return
        text = (
            FAILURE_NOTE_TEXT
            if event.status == "failed"
            else HOST_TEARDOWN_NOTE_TEXT
            if event.abort_kind == "host_teardown"
            else INTERRUPTED_NOTE_TEXT
        )
        message_id = f"run-recovery-note:{event.id}"
        block_semantic = build_frozen_fact(
            TranscriptProviderTextBlockSemanticFact,
            schema_version="transcript_provider_text_block_semantic.v1",
            block_kind="text",
            text=text,
        )
        block = build_frozen_fact(
            TranscriptInlineBlockFact,
            schema_version="transcript_inline_block.v1",
            provider_semantic_identity=block_semantic,
            attribution=build_frozen_fact(
                TranscriptInlineBlockAttributionFact,
                schema_version="transcript_inline_block_attribution.v1",
                block_id=f"{message_id}:text",
                block_index=0,
                source_projection_order=None,
            ),
        )
        provider = build_transcript_message_provider_semantic(
            role="runtime_observation",
            name="pulsara",
            segment="prior_history",
            ordered_block_fingerprints=(block_semantic.semantic_fingerprint,),
        )
        self._append_message_entry(
            provider=provider,
            attribution=build_frozen_fact(
                TranscriptMessageAttributionFact,
                schema_version="transcript_message_attribution.v2",
                message_id=message_id,
                run_id=event.run_id,
                turn_id=event.turn_id,
                reply_id=event.reply_id,
                created_at_utc=event.created_at,
                finished_at_utc=event.created_at,
                segment="recovery_note",
            ),
            content=build_frozen_fact(
                InlineNormalizedMessageContentFact,
                schema_version="inline_normalized_message_content.v3",
                content_kind="inline_normalized_message",
                provider_semantic_identity=provider,
                blocks=(block,),
            ),
            source_event=event,
            source_raw=raw,
        )
        self._record_audit_disposition(
            disposition_kind="recovered_transcript",
            event=event,
            reason_code=f"run_end_{event.status}",
        )

    def _append_model_message(
        self,
        record: _ProjectionRecord,
        *,
        disposition_event: ModelCallControlDispositionResolvedEvent,
        disposition_raw: RawStoredEventEnvelope,
    ) -> TranscriptMessageLeafEntryFact:
        payload = record.document.payload
        assert isinstance(payload, ModelTerminalProjectionPayloadFact)
        ordered = tuple(
            item.semantic_identity.semantic_fingerprint for item in payload.items
        )
        provider = build_transcript_message_provider_semantic(
            role="assistant",
            name="assistant",
            segment="current_run_tail",
            ordered_block_fingerprints=ordered,
        )
        content = build_frozen_fact(
            TerminalProjectionMessageContentRefFact,
            schema_version="terminal_projection_message_content_ref.v3",
            content_kind="terminal_projection_ref",
            provider_semantic_identity=provider,
            projection_reference=record.reference,
            selected_projection_orders=tuple(
                item.semantic_identity.projection_order for item in payload.items
            ),
        )
        event = record.committed_event
        return self._append_message_entry(
            provider=provider,
            attribution=build_frozen_fact(
                TranscriptMessageAttributionFact,
                schema_version="transcript_message_attribution.v2",
                message_id=f"assistant:{event.reply_id}",
                run_id=event.run_id,
                turn_id=event.turn_id,
                reply_id=event.reply_id,
                created_at_utc=event.created_at,
                finished_at_utc=event.created_at,
                segment="current_run_tail",
            ),
            content=content,
            source_event=event,
            source_raw=record.raw_stored_envelope,
            additional_source_event_pairs=((disposition_event, disposition_raw),),
        )

    def _append_message_entry(
        self,
        *,
        provider: TranscriptMessageProviderSemanticFact,
        attribution: TranscriptMessageAttributionFact,
        content: InlineNormalizedMessageContentFact
        | TerminalProjectionMessageContentRefFact,
        source_event: AgentEvent,
        source_raw: RawStoredEventEnvelope,
        additional_source_event_pairs: tuple[
            tuple[AgentEvent, RawStoredEventEnvelope], ...
        ] = (),
    ) -> TranscriptMessageLeafEntryFact:
        semantic = build_frozen_fact(
            TranscriptMessageLeafSemanticFact,
            schema_version="transcript_message_leaf_semantic.v2",
            semantic_kind="message",
            message_provider_semantic_identity=provider,
        )
        entry = build_frozen_fact(
            TranscriptMessageLeafEntryFact,
            schema_version="transcript_message_leaf_entry.v4",
            entry_kind="message",
            ordinal=_ordinal(len(self._stable_entries)),
            semantic_identity=semantic,
            attribution=attribution,
            content=content,
            source_event_refs=(
                _event_ref(self.runtime_session_id, source_event, source_raw),
                *tuple(
                    _event_ref(self.runtime_session_id, event, raw)
                    for event, raw in additional_source_event_pairs
                ),
            ),
        )
        self._stable_entries.append(entry)
        return entry


def _fold_source_accumulator(
    *,
    runtime_session_id: str,
    raw_envelopes: tuple[RawStoredEventEnvelope, ...],
) -> str:
    return context_fingerprint(
        "canonical-transcript-fold-source:v1",
        {
            "runtime_session_id": runtime_session_id,
            "ordered_envelopes": tuple(
                (item.sequence, item.event_id, item.envelope_fingerprint)
                for item in raw_envelopes
            ),
        },
    )


def _validate_event_raw_join(
    event: AgentEvent,
    raw: RawStoredEventEnvelope,
    runtime_session_id: str,
) -> None:
    if event.sequence is None or (
        raw.runtime_session_id != runtime_session_id
        or raw.event_id != event.id
        or raw.run_id != event.run_id
        or raw.turn_id != event.turn_id
        or raw.reply_id != event.reply_id
        or raw.sequence != event.sequence
        or raw.event_type != str(event.type)
    ):
        raise ValueError("transcript owned event/raw envelope join mismatch")


def _leaf_change(
    change_kind: Literal["append", "replace", "retire"],
    previous: TranscriptProjectionLeafEntryFact | None,
    resulting: TranscriptProjectionLeafEntryFact | None,
) -> CanonicalTranscriptLeafChangeFact:
    payload = {
        "change_kind": change_kind,
        "previous_entry_fingerprint": (
            previous.fact_fingerprint if previous is not None else None
        ),
        "resulting_entry_fingerprint": (
            resulting.fact_fingerprint if resulting is not None else None
        ),
    }
    return CanonicalTranscriptLeafChangeFact(
        schema_version="canonical_transcript_leaf_change.v1",
        change_kind=change_kind,
        previous_entry=previous,
        resulting_entry=resulting,
        change_fingerprint=context_fingerprint(
            "canonical-transcript-leaf-change:v1", payload
        ),
    )


def _build_anchor_reference(
    *,
    runtime_session_id: str,
    entry: TranscriptProjectionLeafEntryFact,
    first_coordinate: int,
    last_coordinate: int,
    stable_anchor_slot_key: str | None = None,
    transcript_anchor_id: str | None = None,
) -> CanonicalTranscriptPlacementAnchorReferenceFact:
    if not (1 <= first_coordinate <= last_coordinate <= 2**64 - 2):
        raise ValueError("canonical transcript anchor coordinate is exhausted")
    slot = stable_anchor_slot_key or context_fingerprint(
        "canonical-transcript-anchor-slot:v1",
        {
            "runtime_session_id": runtime_session_id,
            "stable_first_spine_coordinate": first_coordinate,
            "stable_last_spine_coordinate": last_coordinate,
        },
    )
    anchor_id = transcript_anchor_id or (
        "transcript-anchor:"
        + context_fingerprint(
            "canonical-transcript-anchor-id:v1",
            {
                "runtime_session_id": runtime_session_id,
                "stable_anchor_slot_key": slot,
            },
        ).removeprefix("sha256:")
    )
    anchor_fingerprint = context_fingerprint(
        "canonical-transcript-placement-anchor:v1",
        {
            "transcript_anchor_id": anchor_id,
            "stable_anchor_slot_key": slot,
            "stable_first_spine_coordinate": first_coordinate,
            "stable_last_spine_coordinate": last_coordinate,
            "entry_fact_fingerprint": entry.fact_fingerprint,
        },
    )
    payload = {
        "placement_key_contract_id": PRESENTATION_PLACEMENT_KEY_CONTRACT_ID,
        "placement_key_contract_version": (PRESENTATION_PLACEMENT_KEY_CONTRACT_VERSION),
        "placement_key_contract_fingerprint": (
            PRESENTATION_PLACEMENT_KEY_CONTRACT_FINGERPRINT
        ),
        "transcript_anchor_id": anchor_id,
        "stable_anchor_slot_key": slot,
        "stable_first_spine_coordinate": first_coordinate,
        "stable_last_spine_coordinate": last_coordinate,
        "anchor_fingerprint": anchor_fingerprint,
    }
    return CanonicalTranscriptPlacementAnchorReferenceFact(
        schema_version="canonical_transcript_placement_anchor_reference.v1",
        **payload,
        anchor_reference_fingerprint=context_fingerprint(
            "canonical-transcript-placement-anchor-reference:v1", payload
        ),
    )


def _initial_anchor_references(
    *,
    runtime_session_id: str,
    entries: tuple[TranscriptProjectionLeafEntryFact, ...],
) -> tuple[CanonicalTranscriptPlacementAnchorReferenceFact, ...]:
    return tuple(
        _build_anchor_reference(
            runtime_session_id=runtime_session_id,
            entry=entry,
            first_coordinate=index + 1,
            last_coordinate=index + 1,
        )
        for index, entry in enumerate(entries)
    )


def _canonical_spine_fingerprint(
    *,
    anchors: tuple[CanonicalTranscriptPlacementAnchorReferenceFact, ...],
) -> str:
    previous_last = 0
    for anchor in anchors:
        if anchor.stable_first_spine_coordinate <= previous_last:
            raise ValueError("canonical transcript anchor spine overlaps")
        previous_last = anchor.stable_last_spine_coordinate
    return context_fingerprint(
        "canonical-transcript-placement-spine:v1",
        tuple(item.anchor_reference_fingerprint for item in anchors),
    )


def _anchor_tombstone(
    anchor: CanonicalTranscriptPlacementAnchorReferenceFact,
    *,
    retired_by_source_reference_fingerprint: str,
) -> CanonicalTranscriptPlacementAnchorTombstoneFact:
    payload = {
        "placement_key_contract_id": anchor.placement_key_contract_id,
        "placement_key_contract_version": anchor.placement_key_contract_version,
        "placement_key_contract_fingerprint": (
            anchor.placement_key_contract_fingerprint
        ),
        "retired_anchor_reference_fingerprint": anchor.anchor_reference_fingerprint,
        "stable_anchor_slot_key": anchor.stable_anchor_slot_key,
        "stable_first_spine_coordinate": anchor.stable_first_spine_coordinate,
        "stable_last_spine_coordinate": anchor.stable_last_spine_coordinate,
        "retired_by_source_reference_fingerprint": (
            retired_by_source_reference_fingerprint
        ),
        "replacement_anchor_reference_fingerprint": None,
    }
    return CanonicalTranscriptPlacementAnchorTombstoneFact(
        schema_version="canonical_transcript_placement_anchor_tombstone.v1",
        placement_key_contract_id=anchor.placement_key_contract_id,
        placement_key_contract_version=anchor.placement_key_contract_version,
        placement_key_contract_fingerprint=anchor.placement_key_contract_fingerprint,
        retired_anchor_reference=anchor,
        stable_anchor_slot_key=anchor.stable_anchor_slot_key,
        stable_first_spine_coordinate=anchor.stable_first_spine_coordinate,
        stable_last_spine_coordinate=anchor.stable_last_spine_coordinate,
        retired_by_source_reference_fingerprint=(
            retired_by_source_reference_fingerprint
        ),
        replacement_anchor_reference=None,
        tombstone_fingerprint=context_fingerprint(
            "canonical-transcript-placement-anchor-tombstone:v1", payload
        ),
    )


def _placement_transition(
    *,
    transition_kind: Literal[
        "append", "single_replace", "interval_replace", "retire_to_tombstone"
    ],
    before_spine: str,
    after_spine: str,
    predecessors: tuple[CanonicalTranscriptPlacementAnchorReferenceFact, ...],
    resulting: CanonicalTranscriptPlacementAnchorReferenceFact | None,
    tombstones: tuple[CanonicalTranscriptPlacementAnchorTombstoneFact, ...],
) -> CanonicalTranscriptPlacementTransitionProofFact:
    payload = {
        "placement_key_contract_id": PRESENTATION_PLACEMENT_KEY_CONTRACT_ID,
        "placement_key_contract_version": (PRESENTATION_PLACEMENT_KEY_CONTRACT_VERSION),
        "placement_key_contract_fingerprint": (
            PRESENTATION_PLACEMENT_KEY_CONTRACT_FINGERPRINT
        ),
        "transition_kind": transition_kind,
        "before_canonical_spine_fingerprint": before_spine,
        "after_canonical_spine_fingerprint": after_spine,
        "ordered_predecessor_anchor_reference_fingerprints": tuple(
            item.anchor_reference_fingerprint for item in predecessors
        ),
        "resulting_anchor_reference_fingerprint": (
            resulting.anchor_reference_fingerprint if resulting is not None else None
        ),
        "resulting_anchor_tombstone_fingerprints": tuple(
            item.tombstone_fingerprint for item in tombstones
        ),
        "reducer_id": TRANSCRIPT_PROJECTION_REDUCER_ID,
        "reducer_version": TRANSCRIPT_PROJECTION_REDUCER_VERSION,
        "reducer_contract_fingerprint": (
            TRANSCRIPT_PROJECTION_REDUCER_CONTRACT_FINGERPRINT
        ),
    }
    return CanonicalTranscriptPlacementTransitionProofFact(
        schema_version="canonical_transcript_placement_transition.v2",
        placement_key_contract_id=PRESENTATION_PLACEMENT_KEY_CONTRACT_ID,
        placement_key_contract_version=PRESENTATION_PLACEMENT_KEY_CONTRACT_VERSION,
        placement_key_contract_fingerprint=(
            PRESENTATION_PLACEMENT_KEY_CONTRACT_FINGERPRINT
        ),
        transition_kind=transition_kind,
        before_canonical_spine_fingerprint=before_spine,
        after_canonical_spine_fingerprint=after_spine,
        ordered_predecessor_anchor_references=predecessors,
        resulting_anchor_reference=resulting,
        resulting_anchor_tombstones=tombstones,
        reducer_id=TRANSCRIPT_PROJECTION_REDUCER_ID,
        reducer_version=TRANSCRIPT_PROJECTION_REDUCER_VERSION,
        reducer_contract_fingerprint=(
            TRANSCRIPT_PROJECTION_REDUCER_CONTRACT_FINGERPRINT
        ),
        transition_proof_fingerprint=context_fingerprint(
            "canonical-transcript-placement-transition:v2", payload
        ),
    )


def _validate_document_reference(
    reference: TerminalProjectionReferenceFact,
    document: TerminalProjectionDocumentFact,
) -> None:
    if (
        reference.document_fact_fingerprint != document.fact_fingerprint
        or reference.document_contract_fingerprint
        != document.document_contract_fingerprint
        or reference.projection_kind != document.semantic_identity.projection_kind
        or reference.semantic_join.semantic_fingerprint
        != document.semantic_identity.semantic_fingerprint
    ):
        raise ValueError("terminal projection reference/document mismatch")


def _ordinal(value: int) -> TranscriptProjectionOrdinalFact:
    if value < 0 or value > (2**64 - 1):
        raise ValueError("transcript projection ordinal is out of range")
    return TranscriptProjectionOrdinalFact(
        schema_version="transcript_projection_ordinal.v1",
        encoding="u64_be_hex16",
        value_hex=f"{value:016x}",
    )


def _event_ref(
    runtime_session_id: str,
    event: AgentEvent,
    raw: RawStoredEventEnvelope,
) -> ContextEventReferenceFact:
    if event.sequence is None:
        raise ValueError("transcript source reference requires committed event")
    _validate_event_raw_join(event, raw, runtime_session_id)
    return ContextEventReferenceFact(
        runtime_session_id=runtime_session_id,
        event_id=event.id,
        sequence=event.sequence,
        event_type=str(event.type),
        payload_fingerprint=raw.payload_fingerprint,
    )


def projection_references(
    events: Iterable[AgentEvent],
) -> tuple[TerminalProjectionReferenceFact, ...]:
    return tuple(
        event.projection_reference
        for event in events
        if isinstance(
            event,
            (
                ModelCallTerminalProjectionCommittedEvent,
                ToolResultTerminalProjectionCommittedEvent,
            ),
        )
    )


def stable_entry_projection_references(
    entries: Iterable[TranscriptProjectionLeafEntryFact],
) -> tuple[TerminalProjectionReferenceFact, ...]:
    references: list[TerminalProjectionReferenceFact] = []
    for entry in entries:
        if isinstance(entry, TranscriptMessageLeafEntryFact) and isinstance(
            entry.content,
            TerminalProjectionMessageContentRefFact,
        ):
            references.append(entry.content.projection_reference)
        elif isinstance(entry, TranscriptToolResultLeafEntryFact):
            references.append(entry.projection_reference)
    return tuple(references)


__all__ = [
    "CanonicalRunFinalAssistantProjection",
    "CanonicalTranscriptPlacementSpineSnapshot",
    "TRANSCRIPT_PROJECTION_REDUCER_CONTRACT_FINGERPRINT",
    "TranscriptProjectionDocumentRegistry",
    "TranscriptProjectionReducerEvidenceSnapshot",
    "TranscriptProjectionStateStore",
    "projection_references",
    "stable_entry_projection_references",
]
