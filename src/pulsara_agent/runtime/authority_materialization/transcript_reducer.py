"""Incremental transcript stable/live state over committed typed facts."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from threading import RLock
from typing import Iterable

from pulsara_agent.event import (
    AgentEvent,
    ExternalExecutionResultEvent,
    ModelCallStartEvent,
    ModelCallControlDispositionResolvedEvent,
    ModelCallTerminalProjectionCommittedEvent,
    RequireExternalExecutionEvent,
    RunEndEvent,
    RunStartEvent,
    TerminalProcessCompletedEvent,
    ToolExecutionSuspendedEvent,
    ToolResultTerminalProjectionCommittedEvent,
)
from pulsara_agent.event_log.protocol import (
    RawStoredEventEnvelope,
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
from pulsara_agent.primitives.authority_materialization import (
    TranscriptProjectionLiveAssemblyState,
    TranscriptProjectionStableSemanticStateFact,
)
from pulsara_agent.primitives.frozen import build_frozen_fact
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
    TranscriptMessageProviderPlacementSemanticFact,
    TranscriptMessageProviderSemanticFact,
    TranscriptProjectionLeafEntryFact,
    TranscriptProjectionOrdinalFact,
    TranscriptProviderTextBlockSemanticFact,
    TranscriptToolPairLeafEntryFact,
    TranscriptToolPairLeafSemanticFact,
    TranscriptToolResultLeafEntryFact,
    TranscriptToolResultLeafSemanticFact,
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


TRANSCRIPT_PROJECTION_REDUCER_CONTRACT_FINGERPRINT = context_fingerprint(
    "transcript-projection-reducer-contract:v1",
    {
        "model": "terminal-projection+durable-control-disposition:v1",
        "tools": "terminal-projection+pairing-by-tool-call-id:v1",
        "pending": "model+disposition+tool-pair+suspension+external:v1",
        "checkpoint": "transcript-acceleration-deterministic-noop:v1",
    },
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


@dataclass(slots=True)
class _AcceptedModelAssembly:
    record: _ProjectionRecord
    disposition_event: ModelCallControlDispositionResolvedEvent
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
                    raise ValueError("terminal projection frozen-view reference conflict")
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

    def apply_committed(self, events: tuple[AgentEvent, ...]) -> None:
        with self._lock:
            before = self._capture_mutable_state()
            try:
                for event in events:
                    self._apply_contiguous(event)
            except BaseException:
                self._restore_mutable_state(before)
                raise

    def rebuild(self, events: tuple[AgentEvent, ...]) -> None:
        with self._lock:
            self._reset()
            for event in events:
                self._apply_contiguous(event)

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
                self._apply_semantic(raw.decode_owned(DEFAULT_EVENT_SCHEMA_REGISTRY))
            self._through_sequence = snapshot.after.through_sequence
            self._ledger_continuity_accumulator = (
                snapshot.after.ledger_continuity_accumulator
            )
            self._semantic_event_count = snapshot.after.semantic_event_count
            self._semantic_accumulator = snapshot.after.semantic_accumulator

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
        if (
            delta.before.ledger_continuity_accumulator
            != ledger_continuity_accumulator
        ):
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
                entry.semantic_identity.semantic_fingerprint
                for entry in stable_entries
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
                self._apply_semantic(raw.decode_owned(DEFAULT_EVENT_SCHEMA_REGISTRY))
            self._through_sequence = delta.after.through_sequence
            self._ledger_continuity_accumulator = (
                delta.after.ledger_continuity_accumulator
            )
            self._semantic_event_count = delta.after.semantic_event_count
            self._semantic_accumulator = delta.after.semantic_accumulator

    def _reset(self) -> None:
        self._through_sequence = 0
        self._ledger_continuity_accumulator = EMPTY_LEDGER_CONTINUITY_ACCUMULATOR
        self._semantic_event_count = 0
        self._semantic_accumulator = EMPTY_TRANSCRIPT_SEMANTIC_ACCUMULATOR
        self._stable_components: list[str] = []
        self._stable_entries: list[TranscriptProjectionLeafEntryFact] = []
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
            self._pending_models,
            self._model_lifecycle_kinds,
            self._pending_dispositions,
            self._accepted_model_assemblies,
            self._tool_call_owners,
            self._pending_tool_results,
            self._suspended_tool_calls,
            self._pending_external,
        ) = state

    def _apply_contiguous(self, event: AgentEvent) -> None:
        if event.sequence != self._through_sequence + 1:
            raise ValueError("transcript projection committed fold is not contiguous")
        raw = RawStoredEventEnvelope.from_stored_event(
            event=event,
            runtime_session_id=self.runtime_session_id,
            schema_registry=DEFAULT_EVENT_SCHEMA_REGISTRY,
        )
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
            self._apply_semantic(event)
        self._through_sequence = raw.sequence

    def _apply_semantic(self, event: AgentEvent) -> None:
        if isinstance(event, RunStartEvent):
            self._append_current_user(event)
            return
        if isinstance(event, RunEndEvent):
            self._discard_incomplete_run_assemblies(event.run_id)
            self._append_run_recovery_note(event)
            return
        if isinstance(event, TerminalProcessCompletedEvent):
            self._append_terminal_completion_note(event)
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
                return
            if call_id in self._pending_models:
                raise ValueError("duplicate pending model terminal projection")
            self._pending_models[call_id] = _ProjectionRecord(
                event.projection_reference,
                document,
                event.sequence,
                event,
            )
            self._pending_dispositions.add(call_id)
            return
        if isinstance(event, ModelCallControlDispositionResolvedEvent):
            self._resolve_model_disposition(event)
            return
        if isinstance(event, ToolResultTerminalProjectionCommittedEvent):
            if event.sequence is None:
                raise ValueError("tool projection requires committed sequence")
            record = _ProjectionRecord(
                event.projection_reference,
                self.documents.resolve(event.projection_reference),
                event.sequence,
                event,
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
    ) -> None:
        call_id = event.resolved_model_call_id
        record = self._pending_models.pop(call_id, None)
        if record is None or call_id not in self._pending_dispositions:
            raise ValueError("model control disposition has no pending projection")
        self._pending_dispositions.remove(call_id)
        if event.disposition is not ModelCallControlDisposition.ACCEPTED:
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
            self._append_model_message(record, disposition_event=event)
            return
        if call_id in self._accepted_model_assemblies:
            raise ValueError("duplicate accepted model assembly")
        assembly = _AcceptedModelAssembly(
            record=record,
            disposition_event=event,
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
            source_event_refs=(_event_ref(self.runtime_session_id, record.committed_event),),
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
            source_event_refs=(_event_ref(self.runtime_session_id, record.committed_event),),
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

    def _append_current_user(self, event: RunStartEvent) -> None:
        current = event.current_user_message
        block_semantic = build_frozen_fact(
            TranscriptProviderTextBlockSemanticFact,
            schema_version="transcript_provider_text_block_semantic.v1",
            block_kind="text",
            text=current.text,
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
        provider_role = (
            "user" if current.source_kind == "host_user_input" else "runtime_request"
        )
        provider = _message_provider_semantic(
            role=provider_role,
            name="user" if provider_role == "user" else "pulsara_runtime",
            segment="current_user",
            ordered_block_fingerprints=(block_semantic.semantic_fingerprint,),
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
        )

    def _append_run_recovery_note(self, event: RunEndEvent) -> None:
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
        provider = _message_provider_semantic(
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
        )

    def _append_terminal_completion_note(
        self,
        event: TerminalProcessCompletedEvent,
    ) -> None:
        text = (
            "Pulsara note: terminal background task update. "
            f"Process {event.process_id} completed with status {event.status} "
            f"and exit code {event.exit_code}. This note is lifecycle-only, not "
            "the full output; if still retained, inspect retained output with "
            "terminal_process log."
        )
        message_id = f"terminal-completion-note:{event.id}"
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
        provider = _message_provider_semantic(
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
                segment="terminal_lifecycle_note",
            ),
            content=build_frozen_fact(
                InlineNormalizedMessageContentFact,
                schema_version="inline_normalized_message_content.v3",
                content_kind="inline_normalized_message",
                provider_semantic_identity=provider,
                blocks=(block,),
            ),
            source_event=event,
        )

    def _append_model_message(
        self,
        record: _ProjectionRecord,
        *,
        disposition_event: ModelCallControlDispositionResolvedEvent,
    ) -> TranscriptMessageLeafEntryFact:
        payload = record.document.payload
        assert isinstance(payload, ModelTerminalProjectionPayloadFact)
        ordered = tuple(
            item.semantic_identity.semantic_fingerprint for item in payload.items
        )
        provider = _message_provider_semantic(
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
            additional_source_events=(disposition_event,),
        )

    def _append_message_entry(
        self,
        *,
        provider: TranscriptMessageProviderSemanticFact,
        attribution: TranscriptMessageAttributionFact,
        content: InlineNormalizedMessageContentFact
        | TerminalProjectionMessageContentRefFact,
        source_event: AgentEvent,
        additional_source_events: tuple[AgentEvent, ...] = (),
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
            source_event_refs=tuple(
                _event_ref(self.runtime_session_id, item)
                for item in (source_event, *additional_source_events)
            ),
        )
        self._stable_entries.append(entry)
        return entry


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
) -> ContextEventReferenceFact:
    if event.sequence is None:
        raise ValueError("transcript source reference requires committed event")
    raw = RawStoredEventEnvelope.from_stored_event(
        event=event,
        runtime_session_id=runtime_session_id,
        schema_registry=DEFAULT_EVENT_SCHEMA_REGISTRY,
    )
    return ContextEventReferenceFact(
        runtime_session_id=runtime_session_id,
        event_id=event.id,
        sequence=event.sequence,
        event_type=str(event.type),
        payload_fingerprint=raw.payload_fingerprint,
    )


def _message_provider_semantic(
    *,
    role: str,
    name: str,
    segment: str,
    ordered_block_fingerprints: tuple[str, ...],
) -> TranscriptMessageProviderSemanticFact:
    if segment == "current_user":
        lane = "current_user"
        scope = "leading_user"
        timing = "current_user"
    elif segment == "current_run_tail":
        lane = "current_run_tail"
        scope = "transcript_current_run"
        timing = "current_run_observation"
    else:
        lane = "prior_history"
        scope = "transcript_prior"
        timing = "historical_replay"
    placement = build_frozen_fact(
        TranscriptMessageProviderPlacementSemanticFact,
        schema_version="transcript_message_provider_placement_semantic.v2",
        normalized_lane=lane,
        lowering_scope=scope,
        timing_overlay_kind=timing,
        timing_policy_semantic_fingerprint=context_fingerprint(
            "transcript-timing-policy-semantic:v1", timing
        ),
        placement_contract_id="pulsara.transcript-message-placement",
        placement_contract_version="2",
        placement_contract_fingerprint=context_fingerprint(
            "transcript-message-placement-contract:v2",
            "current-user+current-run-tail+prior-history",
        ),
    )
    return build_frozen_fact(
        TranscriptMessageProviderSemanticFact,
        schema_version="transcript_message_provider_semantic.v4",
        role=role,
        name=name,
        placement_semantic=placement,
        ordered_block_semantic_fingerprints=ordered_block_fingerprints,
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
    "TRANSCRIPT_PROJECTION_REDUCER_CONTRACT_FINGERPRINT",
    "TranscriptProjectionDocumentRegistry",
    "TranscriptProjectionReducerEvidenceSnapshot",
    "TranscriptProjectionStateStore",
    "projection_references",
    "stable_entry_projection_references",
]
