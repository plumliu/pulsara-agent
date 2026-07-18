"""Append-only in-memory EventLog implementation."""

from __future__ import annotations

import json
from dataclasses import dataclass, field, replace
from threading import Lock
from typing import Iterable

from pulsara_agent.event.events import AgentEvent, ReplyStartEvent
from pulsara_agent.event_log.protocol import (
    EventBatchConfirmation,
    EventIdConflict,
    EventLogReadSnapshot,
    RawCheckpointLedgerCandidate,
    RawCheckpointLedgerSnapshot,
    RawContextAuthorityBundle,
    RawContextAuthorityBundleRequest,
    RawEventLogReadSnapshot,
    RawEventIdSelectionSnapshot,
    RawEventSelectionBounds,
    RawEventTypeSelectionSnapshot,
    RawLedgerUsageSnapshot,
    RawReplyEventGroup,
    RawReplySelectionSnapshot,
    RawStoredEventEnvelope,
    RawTranscriptDomainDeltaSnapshot,
    RawTranscriptDomainPrefixFact,
    EventLogWriteConflict,
    EventLogTransactionCompanion,
    MaterializationAccountStateConflict,
    raw_checkpoint_catalog_identity,
    same_event_payload,
    same_event_raw_payload,
)
from pulsara_agent.event_log.transcript_prefix import (
    EMPTY_LEDGER_CONTINUITY_ACCUMULATOR,
    EMPTY_TRANSCRIPT_SEMANTIC_ACCUMULATOR,
    advance_ledger_continuity_accumulator,
    advance_transcript_semantic_accumulator,
    classify_transcript_event_type,
)
from pulsara_agent.event_log.serialization import (
    DEFAULT_EVENT_SCHEMA_REGISTRY,
    dump_agent_event,
    load_agent_event,
)
from pulsara_agent.primitives.context import context_fingerprint
from pulsara_agent.primitives.authority_materialization import (
    LedgerMaterializationAccountStateFact,
    PhysicalChargeContractFact,
)
from pulsara_agent.message.message import AssistantMsg, Msg
from pulsara_agent.message.reducer import (
    MessageReducer,
    require_canonical_reply_control,
)


@dataclass(slots=True)
class InMemoryEventLog:
    runtime_session_id: str = "in-memory"
    _raw_events: list[RawStoredEventEnvelope] = field(default_factory=list)
    _transcript_prefixes: list[RawTranscriptDomainPrefixFact] = field(
        default_factory=lambda: [
            RawTranscriptDomainPrefixFact(
                through_sequence=0,
                ledger_payload_bytes=0,
                semantic_event_count=0,
                semantic_accumulator=EMPTY_TRANSCRIPT_SEMANTIC_ACCUMULATOR,
                ledger_continuity_accumulator=EMPTY_LEDGER_CONTINUITY_ACCUMULATOR,
            )
        ]
    )
    _next_sequence: int = 1
    _materialization_account_state: LedgerMaterializationAccountStateFact | None = (
        None
    )
    _lock: Lock = field(default_factory=Lock, init=False, repr=False)

    def ensure_runtime_session_owner(self) -> None:
        """In-memory ledgers have no external ownership foreign key."""

    def bind_runtime_session_id(self, runtime_session_id: str) -> None:
        """Bind the test-double ledger before its first durable write."""

        if not runtime_session_id:
            raise ValueError("in-memory event log runtime session id is required")
        with self._lock:
            if self._raw_events and self.runtime_session_id != runtime_session_id:
                raise ValueError("cannot rebind a non-empty in-memory event ledger")
            self.runtime_session_id = runtime_session_id

    def append(
        self,
        event: AgentEvent,
        *,
        expected_last_sequence: int | None = None,
        deadline_monotonic: float | None = None,
    ) -> AgentEvent:
        del deadline_monotonic
        _validate_live_batch([event])
        with self._lock:
            existing = next(
                (stored for stored in self._raw_events if stored.event_id == event.id),
                None,
            )
            if existing is not None:
                decoded = existing.decode_owned(DEFAULT_EVENT_SCHEMA_REGISTRY)
                if same_event_payload(event, decoded):
                    return _owned_event(decoded)
                raise EventIdConflict(event.id)
            actual_last_sequence = self._next_sequence - 1
            if (
                expected_last_sequence is not None
                and expected_last_sequence != actual_last_sequence
            ):
                raise EventLogWriteConflict(
                    expected_last_sequence=expected_last_sequence,
                    actual_last_sequence=actual_last_sequence,
                )
            stored = _owned_event(
                event.model_copy(update={"sequence": self._next_sequence})
            )
            raw = _raw(stored, self.runtime_session_id)
            self._raw_events.append(raw)
            self._append_transcript_prefix(stored, raw)
            self._next_sequence += 1
            return _owned_event(stored)

    def extend(
        self,
        events: Iterable[AgentEvent],
        *,
        expected_last_sequence: int | None = None,
        deadline_monotonic: float | None = None,
    ) -> list[AgentEvent]:
        del deadline_monotonic
        event_list = list(events)
        if not event_list:
            return []
        _validate_live_batch(event_list)
        with self._lock:
            actual_last_sequence = self._next_sequence - 1
            if (
                expected_last_sequence is not None
                and expected_last_sequence != actual_last_sequence
            ):
                raise EventLogWriteConflict(
                    expected_last_sequence=expected_last_sequence,
                    actual_last_sequence=actual_last_sequence,
                )
            existing_ids = {event.event_id for event in self._raw_events}
            duplicate = next(
                (event.id for event in event_list if event.id in existing_ids), None
            )
            if duplicate is not None:
                raise ValueError(
                    f"Event id already exists in this session: {duplicate}"
                )
            stored_events = [
                _owned_event(
                    event.model_copy(update={"sequence": self._next_sequence + index})
                )
                for index, event in enumerate(event_list)
            ]
            raw_events = tuple(
                _raw(event, self.runtime_session_id) for event in stored_events
            )
            self._raw_events.extend(raw_events)
            for stored, raw in zip(stored_events, raw_events, strict=True):
                self._append_transcript_prefix(stored, raw)
            self._next_sequence += len(stored_events)
            return [_owned_event(event) for event in stored_events]

    def read_materialization_account_state(
        self,
        *,
        deadline_monotonic: float | None = None,
    ) -> LedgerMaterializationAccountStateFact | None:
        del deadline_monotonic
        with self._lock:
            state = self._materialization_account_state
            if state is None:
                return None
            return LedgerMaterializationAccountStateFact.model_validate(
                state.model_dump(mode="json")
            )

    def adopt_materialization_account_state_for_test(
        self,
        state: LedgerMaterializationAccountStateFact,
    ) -> None:
        """Install an explicit pytest-only account over legacy fixture events."""

        with self._lock:
            if self._materialization_account_state is not None:
                raise ValueError("in-memory materialization account already exists")
            if (
                state.runtime_session_id != self.runtime_session_id
                or state.ledger_through_sequence != self._next_sequence - 1
            ):
                raise ValueError("test account does not cover the in-memory ledger")
            self._materialization_account_state = state

    def extend_with_materialization_state(
        self,
        events: Iterable[AgentEvent],
        *,
        expected_account_state_fingerprint: str | None,
        resulting_account_state: LedgerMaterializationAccountStateFact,
        physical_charge_contract: PhysicalChargeContractFact,
        transaction_companion: EventLogTransactionCompanion | None = None,
        expected_last_sequence: int | None = None,
        deadline_monotonic: float | None = None,
    ) -> list[AgentEvent]:
        del deadline_monotonic
        event_list = list(events)
        if not event_list:
            raise ValueError("materialization state commit requires events")
        _validate_live_batch(event_list)
        with self._lock:
            current = self._materialization_account_state
            actual_fingerprint = (
                current.account_state_fingerprint if current is not None else None
            )
            if actual_fingerprint != expected_account_state_fingerprint:
                raise MaterializationAccountStateConflict(
                    expected_state_fingerprint=(
                        expected_account_state_fingerprint
                    ),
                    actual_state_fingerprint=actual_fingerprint,
                )
            actual_last_sequence = self._next_sequence - 1
            if (
                expected_last_sequence is not None
                and expected_last_sequence != actual_last_sequence
            ):
                raise EventLogWriteConflict(
                    expected_last_sequence=expected_last_sequence,
                    actual_last_sequence=actual_last_sequence,
                )
            expected_result_sequence = actual_last_sequence + len(event_list)
            if (
                resulting_account_state.runtime_session_id
                != self.runtime_session_id
                or resulting_account_state.ledger_through_sequence
                != expected_result_sequence
                or resulting_account_state.ledger_event_count_through
                != expected_result_sequence
            ):
                raise ValueError(
                    "materialization state does not cover the committed event batch"
                )
            existing_ids = {event.event_id for event in self._raw_events}
            if any(event.id in existing_ids for event in event_list):
                raise ValueError("materialization state batch cannot contain existing IDs")
            stored_events = [
                _owned_event(
                    event.model_copy(update={"sequence": self._next_sequence + index})
                )
                for index, event in enumerate(event_list)
            ]
            raw_events = tuple(
                _raw(event, self.runtime_session_id) for event in stored_events
            )
            self._validate_stored_envelope_charge_bounds(
                stored_events,
                raw_events,
                physical_charge_contract,
            )
            if transaction_companion is not None:
                transaction_companion.apply_in_memory(stored_events)
            self._raw_events.extend(raw_events)
            for stored, raw in zip(stored_events, raw_events, strict=True):
                self._append_transcript_prefix(stored, raw)
            self._next_sequence += len(stored_events)
            self._materialization_account_state = resulting_account_state
            return [_owned_event(event) for event in stored_events]

    @staticmethod
    def _validate_stored_envelope_charge_bounds(
        stored_events: list[AgentEvent],
        raw_events: tuple[RawStoredEventEnvelope, ...],
        contract: PhysicalChargeContractFact,
    ) -> None:
        bounds = {
            (item.event_type, item.event_schema_version): item
            for item in contract.bookkeeping_event_bounds
        }
        for stored, raw in zip(stored_events, raw_events, strict=True):
            binding = DEFAULT_EVENT_SCHEMA_REGISTRY.resolve_for_event(
                stored
            ).schema_contract
            bound = bounds.get((str(stored.type), binding.event_schema_version))
            actual = len(raw.canonical_payload_bytes) + (
                contract.fixed_sequence_wrapper_charge_bytes_per_event
                + contract.fixed_schema_wrapper_charge_bytes_per_event
            )
            if (
                str(stored.type) == "PHYSICAL_OPERATION_CHARGE_APPLIED"
                and actual
                > stored.charge.charge_applied_event_charge_payload_bytes
            ):
                raise ValueError(
                    "stored charge-applied envelope exceeds its dynamic charge bound"
                )
            if bound is not None and actual > bound.max_stored_envelope_bytes:
                raise ValueError(
                    "stored bookkeeping envelope exceeds fixed charge bound"
                )

    def iter(
        self,
        *,
        run_id: str | None = None,
        turn_id: str | None = None,
        reply_id: str | None = None,
        after_sequence: int | None = None,
    ) -> list[AgentEvent]:
        with self._lock:
            events = [
                raw.decode_owned(DEFAULT_EVENT_SCHEMA_REGISTRY)
                for raw in self._raw_events
            ]
        if after_sequence is not None:
            events = [
                event for event in events if (event.sequence or 0) > after_sequence
            ]
        if run_id is not None:
            events = [event for event in events if event.run_id == run_id]
        if turn_id is not None:
            events = [event for event in events if event.turn_id == turn_id]
        if reply_id is not None:
            events = [event for event in events if event.reply_id == reply_id]
        return [_owned_event(event) for event in events]

    def get_by_id(self, event_id: str) -> AgentEvent | None:
        with self._lock:
            raw = next(
                (event for event in self._raw_events if event.event_id == event_id),
                None,
            )
            if raw is None:
                return None
            return _owned_event(raw.decode_owned(DEFAULT_EVENT_SCHEMA_REGISTRY))

    def confirm_batch(
        self,
        candidates,
        *,
        deadline_monotonic: float | None = None,
    ) -> EventBatchConfirmation:
        del deadline_monotonic
        candidate_list = list(candidates)
        ids = [event.id for event in candidate_list]
        if len(ids) != len(set(ids)):
            raise ValueError("Confirmed event ids must be unique within one batch")
        with self._lock:
            by_id = {event.event_id: event for event in self._raw_events}
            committed: list[AgentEvent] = []
            missing: list[str] = []
            for candidate in candidate_list:
                existing = by_id.get(candidate.id)
                if existing is None:
                    missing.append(candidate.id)
                    continue
                candidate_binding = DEFAULT_EVENT_SCHEMA_REGISTRY.resolve_for_event(
                    candidate
                )
                if (
                    candidate_binding.schema_contract.event_schema_version
                    != existing.event_schema_version
                    or candidate_binding.schema_contract.event_schema_fingerprint
                    != existing.event_schema_fingerprint
                    or candidate_binding.schema_contract.domain_contract_fingerprint
                    != existing.event_domain_contract_fingerprint
                    or not same_event_raw_payload(candidate, existing)
                ):
                    raise EventIdConflict(candidate.id)
                decoded = existing.decode_owned(DEFAULT_EVENT_SCHEMA_REGISTRY)
                committed.append(_owned_event(decoded))
            return EventBatchConfirmation(
                committed_events=tuple(committed),
                missing_event_ids=tuple(missing),
                actual_last_sequence=self._next_sequence - 1,
            )

    def read_ledger_usage_snapshot(
        self,
        *,
        deadline_monotonic: float | None = None,
    ) -> RawLedgerUsageSnapshot:
        del deadline_monotonic
        with self._lock:
            return RawLedgerUsageSnapshot(
                through_sequence=self._next_sequence - 1,
                event_count=len(self._raw_events),
                candidate_payload_bytes=sum(
                    len(item.canonical_payload_bytes) for item in self._raw_events
                ),
            )

    def read_range_snapshot(
        self,
        *,
        minimum_sequence: int,
        through_sequence: int | None = None,
        deadline_monotonic: float | None = None,
    ) -> EventLogReadSnapshot:
        raw = self.read_raw_range_snapshot(
            minimum_sequence=minimum_sequence,
            through_sequence=through_sequence,
            deadline_monotonic=deadline_monotonic,
        )
        return EventLogReadSnapshot(
            through_sequence=raw.through_sequence,
            events=tuple(
                _owned_event(event.decode_owned(DEFAULT_EVENT_SCHEMA_REGISTRY))
                for event in raw.events
            ),
        )

    def read_raw_range_snapshot(
        self,
        *,
        minimum_sequence: int,
        through_sequence: int | None = None,
        max_events: int | None = None,
        max_payload_bytes: int | None = None,
        deadline_monotonic: float | None = None,
    ) -> RawEventLogReadSnapshot:
        del deadline_monotonic
        if minimum_sequence < 1:
            raise ValueError("minimum sequence must be positive")
        if max_events is not None and max_events < 1:
            raise ValueError("event range max_events must be positive")
        if max_payload_bytes is not None and max_payload_bytes < 1:
            raise ValueError("event range max_payload_bytes must be positive")
        with self._lock:
            current_high_water = self._next_sequence - 1
            effective_through = (
                current_high_water if through_sequence is None else through_sequence
            )
            if effective_through > current_high_water:
                raise ValueError("requested event high-water has not been committed")
            if effective_through < minimum_sequence:
                raise ValueError("event read range is empty or reversed")
            events = tuple(
                _owned_raw(event)
                for event in self._raw_events
                if minimum_sequence <= event.sequence <= effective_through
            )
        if max_events is not None and len(events) > max_events:
            raise ValueError("event range exceeds its event bound")
        if max_payload_bytes is not None and sum(
            len(event.canonical_payload_bytes) for event in events
        ) > max_payload_bytes:
            raise ValueError("event range exceeds its payload-byte bound")
        return RawEventLogReadSnapshot(
            through_sequence=effective_through,
            events=events,
            snapshot_fingerprint=context_fingerprint(
                "raw-event-log-read-snapshot:v1",
                {
                    "through_sequence": effective_through,
                    "envelopes": tuple(
                        event.envelope_fingerprint for event in events
                    ),
                },
            ),
        )

    def read_raw_events_by_id(
        self,
        event_ids: tuple[str, ...],
        *,
        deadline_monotonic: float | None = None,
    ) -> tuple[RawStoredEventEnvelope, ...]:
        del deadline_monotonic
        if len(event_ids) != len(set(event_ids)):
            raise ValueError("raw event ids must be unique")
        with self._lock:
            by_id = {event.event_id: event for event in self._raw_events}
            return tuple(
                _owned_raw(by_id[event_id])
                for event_id in event_ids
                if event_id in by_id
            )

    def read_raw_events_by_id_snapshot(
        self,
        event_ids: tuple[str, ...],
        *,
        deadline_monotonic: float | None = None,
    ) -> RawEventIdSelectionSnapshot:
        del deadline_monotonic
        if len(event_ids) != len(set(event_ids)):
            raise ValueError("raw event ids must be unique")
        with self._lock:
            by_id = {event.event_id: event for event in self._raw_events}
            return RawEventIdSelectionSnapshot(
                through_sequence=self._next_sequence - 1,
                events=tuple(
                    _owned_raw(by_id[event_id])
                    for event_id in event_ids
                    if event_id in by_id
                ),
            )

    def read_raw_events_by_type(
        self,
        event_type: str,
        *,
        limit: int,
        through_sequence: int | None = None,
        deadline_monotonic: float | None = None,
    ) -> tuple[RawStoredEventEnvelope, ...]:
        del deadline_monotonic
        if limit < 1:
            raise ValueError("raw event type read limit must be positive")
        if through_sequence is not None and through_sequence < 0:
            raise ValueError("raw event type high-water cannot be negative")
        with self._lock:
            matches = [
                event
                for event in self._raw_events
                if event.event_type == event_type
                and (
                    through_sequence is None
                    or event.sequence <= through_sequence
                )
            ]
            return tuple(_owned_raw(event) for event in reversed(matches[-limit:]))

    def read_raw_model_call_events(
        self,
        resolved_model_call_id: str,
        *,
        max_events: int,
        max_payload_bytes: int,
        deadline_monotonic: float | None = None,
    ) -> tuple[RawStoredEventEnvelope, ...]:
        del deadline_monotonic
        if not resolved_model_call_id:
            raise ValueError("resolved model call id must be non-empty")
        if max_events < 1 or max_payload_bytes < 1:
            raise ValueError("model-call read bounds must be positive")
        with self._lock:
            selected = tuple(
                _owned_raw(event)
                for event in self._raw_events
                if _raw_resolved_model_call_id(event) == resolved_model_call_id
            )
        if len(selected) > max_events:
            raise ValueError("model-call event count exceeds its read bound")
        if sum(len(event.canonical_payload_bytes) for event in selected) > max_payload_bytes:
            raise ValueError("model-call payload bytes exceed their read bound")
        return selected

    def read_raw_events_by_types(
        self,
        event_types: tuple[str, ...],
        *,
        active_runs_only: bool = False,
        run_ids: tuple[str, ...] | None = None,
        minimum_sequence: int = 1,
        through_sequence: int | None = None,
        max_events: int = 16_384,
        max_payload_bytes: int = 16 * 1024 * 1024,
        deadline_monotonic: float | None = None,
    ) -> RawEventTypeSelectionSnapshot:
        del deadline_monotonic
        if not event_types or len(event_types) != len(set(event_types)):
            raise ValueError("raw event types must be non-empty and unique")
        if run_ids is not None and (
            not run_ids or len(run_ids) != len(set(run_ids))
        ):
            raise ValueError("run id selection must be non-empty and unique")
        if minimum_sequence < 1 or max_events < 1 or max_payload_bytes < 1:
            raise ValueError("sparse event read bounds are invalid")
        selected = frozenset(event_types)
        with self._lock:
            current_high_water = self._next_sequence - 1
            effective_through = (
                current_high_water
                if through_sequence is None
                else through_sequence
            )
            if effective_through < 0 or effective_through > current_high_water:
                raise ValueError("requested sparse high-water has not been committed")
            active_run_ids: frozenset[str] | None = None
            if active_runs_only:
                started_run_ids = {
                    event.run_id
                    for event in self._raw_events
                    if event.event_type == "RUN_START"
                }
                ended_run_ids = {
                    event.run_id
                    for event in self._raw_events
                    if event.event_type == "RUN_END"
                }
                active_run_ids = frozenset(started_run_ids - ended_run_ids)
            events = tuple(
                _owned_raw(event)
                for event in self._raw_events
                if event.sequence >= minimum_sequence
                and event.sequence <= effective_through
                and event.event_type in selected
                and (active_run_ids is None or event.run_id in active_run_ids)
                and (run_ids is None or event.run_id in run_ids)
            )
            if len(events) > max_events:
                raise ValueError("sparse event selection exceeds its event bound")
            if sum(len(item.canonical_payload_bytes) for item in events) > max_payload_bytes:
                raise ValueError("sparse event selection exceeds its byte bound")
            return RawEventTypeSelectionSnapshot(
                through_sequence=effective_through,
                events=events,
            )

    def read_transcript_domain_delta(
        self,
        *,
        after_sequence: int,
        through_sequence: int | None = None,
        max_events: int = 16_384,
        max_payload_bytes: int = 16 * 1024 * 1024,
        registry_contract_fingerprint: str,
        deadline_monotonic: float | None = None,
    ) -> RawTranscriptDomainDeltaSnapshot:
        del deadline_monotonic
        if after_sequence < 0 or max_events < 1 or max_payload_bytes < 1:
            raise ValueError("transcript domain delta bounds are invalid")
        if not registry_contract_fingerprint:
            raise ValueError("transcript registry contract fingerprint is required")
        with self._lock:
            high_water = self._next_sequence - 1
            effective_through = high_water if through_sequence is None else through_sequence
            if effective_through > high_water or effective_through < after_sequence:
                raise ValueError("transcript domain delta range is invalid")
            before = self._transcript_prefixes[after_sequence]
            after = self._transcript_prefixes[effective_through]
            semantic_events = tuple(
                _owned_raw(event)
                for event in self._raw_events[after_sequence:effective_through]
                if classify_transcript_event_type(event.event_type)
                == "transcript_semantic"
            )
        if len(semantic_events) > max_events:
            raise ValueError("transcript semantic delta exceeds its event bound")
        if sum(
            len(item.canonical_payload_bytes) for item in semantic_events
        ) > max_payload_bytes:
            raise ValueError("transcript semantic delta exceeds its byte bound")
        return RawTranscriptDomainDeltaSnapshot.build(
            runtime_session_id=self.runtime_session_id,
            before=before,
            after=after,
            semantic_events=semantic_events,
            registry_contract_fingerprint=registry_contract_fingerprint,
        )

    def _append_transcript_prefix(
        self,
        stored: AgentEvent,
        raw: RawStoredEventEnvelope,
    ) -> None:
        previous = self._transcript_prefixes[-1]
        semantic_count = previous.semantic_event_count
        semantic_accumulator = previous.semantic_accumulator
        if classify_transcript_event_type(raw.event_type) == "transcript_semantic":
            semantic_count += 1
            semantic_accumulator = advance_transcript_semantic_accumulator(
                previous.semantic_accumulator,
                event=stored,
                event_schema_version=raw.event_schema_version,
                event_schema_fingerprint=raw.event_schema_fingerprint,
            )
        self._transcript_prefixes.append(
            RawTranscriptDomainPrefixFact(
                through_sequence=raw.sequence,
                ledger_payload_bytes=(
                    previous.ledger_payload_bytes
                    + len(raw.canonical_payload_bytes)
                ),
                semantic_event_count=semantic_count,
                semantic_accumulator=semantic_accumulator,
                ledger_continuity_accumulator=advance_ledger_continuity_accumulator(
                    previous.ledger_continuity_accumulator,
                    envelope_fingerprint=raw.envelope_fingerprint,
                ),
            )
        )

    def read_context_authority_bundle(
        self,
        request: RawContextAuthorityBundleRequest,
        *,
        deadline_monotonic: float | None = None,
    ) -> RawContextAuthorityBundle:
        del deadline_monotonic
        with self._lock:
            high_water = self._next_sequence - 1
            primary = tuple(
                _owned_raw(item)
                for item in self._raw_events
                if item.sequence >= request.primary_minimum_sequence
            )
            run_types = frozenset(request.run_sparse_event_types)
            run_sparse = tuple(
                _owned_raw(item)
                for item in self._raw_events
                if item.run_id == request.run_id and item.event_type in run_types
            )
            session_types = frozenset(request.session_sparse_event_types)
            session_sparse = tuple(
                _owned_raw(item)
                for item in self._raw_events
                if item.event_type in session_types
            )
            exact_ids = frozenset(request.exact_event_ids)
            exact = tuple(
                _owned_raw(item)
                for item in self._raw_events
                if item.event_id in exact_ids
            )
        _validate_bundle_channel(primary, request.primary_bounds, "primary")
        _validate_bundle_channel(run_sparse, request.run_sparse_bounds, "run sparse")
        _validate_bundle_channel(
            session_sparse,
            request.session_sparse_bounds,
            "session sparse",
        )
        _validate_bundle_channel(exact, request.exact_bounds, "exact")
        return RawContextAuthorityBundle.build(
            runtime_session_id=self.runtime_session_id,
            request=request,
            through_sequence=high_water,
            primary_events=primary,
            run_sparse_events=run_sparse,
            session_sparse_events=session_sparse,
            exact_events=exact,
        )

    def read_raw_reply_events(
        self,
        reply_id: str,
        *,
        max_events: int,
        max_payload_bytes: int,
        deadline_monotonic: float | None = None,
    ) -> tuple[RawStoredEventEnvelope, ...]:
        del deadline_monotonic
        if not reply_id or max_events < 1 or max_payload_bytes < 1:
            raise ValueError("reply event read bounds are invalid")
        with self._lock:
            selected = tuple(
                _owned_raw(item)
                for item in self._raw_events
                if item.reply_id == reply_id
            )
        if len(selected) > max_events:
            raise ValueError("reply event count exceeds its read bound")
        if sum(len(item.canonical_payload_bytes) for item in selected) > max_payload_bytes:
            raise ValueError("reply payload bytes exceed their read bound")
        return selected

    def read_raw_replies_snapshot(
        self,
        reply_ids: tuple[str, ...],
        *,
        through_sequence: int,
        max_total_events: int,
        max_total_payload_bytes: int,
        deadline_monotonic: float | None = None,
    ) -> RawReplySelectionSnapshot:
        del deadline_monotonic
        _validate_reply_snapshot_request(
            reply_ids=reply_ids,
            through_sequence=through_sequence,
            max_total_events=max_total_events,
            max_total_payload_bytes=max_total_payload_bytes,
        )
        selected_ids = frozenset(reply_ids)
        with self._lock:
            selected = tuple(
                _owned_raw(item)
                for item in self._raw_events
                if item.reply_id in selected_ids
                and item.sequence <= through_sequence
            )
        _require_reply_snapshot_bounds(
            selected,
            max_total_events=max_total_events,
            max_total_payload_bytes=max_total_payload_bytes,
        )
        by_reply = {reply_id: [] for reply_id in reply_ids}
        for item in selected:
            by_reply[item.reply_id].append(item)
        return RawReplySelectionSnapshot(
            through_sequence=through_sequence,
            groups=tuple(
                RawReplyEventGroup(reply_id=reply_id, events=tuple(by_reply[reply_id]))
                for reply_id in reply_ids
            ),
        )

    def read_raw_run_events(
        self,
        run_id: str,
        *,
        max_events: int,
        max_payload_bytes: int,
        deadline_monotonic: float | None = None,
    ) -> tuple[RawStoredEventEnvelope, ...]:
        del deadline_monotonic
        if not run_id or max_events < 1 or max_payload_bytes < 1:
            raise ValueError("run event read bounds are invalid")
        with self._lock:
            selected = tuple(
                _owned_raw(item) for item in self._raw_events if item.run_id == run_id
            )
        if len(selected) > max_events:
            raise ValueError("run event count exceeds its read bound")
        if sum(len(item.canonical_payload_bytes) for item in selected) > max_payload_bytes:
            raise ValueError("run payload bytes exceed their read bound")
        return selected

    def read_raw_checkpoint_ledger_snapshot(
        self,
        *,
        checkpoint_event_type: str,
        requested_through_sequence: int,
        graph_reducer_id: str,
        graph_reducer_version: str,
        graph_reducer_contract_fingerprint: str,
        preferred_checkpoint_id: str | None,
        max_delta_events: int,
        max_delta_bytes: int,
        max_checkpoint_candidates: int,
        deadline_monotonic: float | None = None,
    ) -> RawCheckpointLedgerSnapshot:
        del deadline_monotonic
        if requested_through_sequence < 1:
            raise ValueError("checkpoint requested high-water must be positive")
        if (
            max_delta_events < 0
            or max_delta_bytes < 0
            or max_checkpoint_candidates < 1
        ):
            raise ValueError("checkpoint read bounds are invalid")
        expected_contract = (
            graph_reducer_id,
            graph_reducer_version,
            graph_reducer_contract_fingerprint,
        )
        with self._lock:
            high_water = self._next_sequence - 1
            if requested_through_sequence > high_water:
                raise ValueError("requested checkpoint high-water is not committed")
            catalog = tuple(
                event
                for event in self._raw_events
                if event.event_type == checkpoint_event_type
            )
            compatible: list[tuple[RawStoredEventEnvelope, str, int]] = []
            compatible_total = 0
            for event in catalog:
                (
                    checkpoint_id,
                    through_sequence,
                    reducer_id,
                    reducer_version,
                    reducer_fingerprint,
                ) = raw_checkpoint_catalog_identity(event)
                if (
                    reducer_id,
                    reducer_version,
                    reducer_fingerprint,
                ) != expected_contract:
                    continue
                compatible_total += 1
                if through_sequence <= requested_through_sequence:
                    compatible.append((event, checkpoint_id, through_sequence))
            compatible.sort(
                key=lambda item: (
                    item[1] != preferred_checkpoint_id,
                    -item[2],
                    -item[0].sequence,
                )
            )
            selected = compatible[:max_checkpoint_candidates]
            candidates: list[RawCheckpointLedgerCandidate] = []
            for checkpoint_event, checkpoint_id, checkpoint_through in selected:
                delta_count = requested_through_sequence - checkpoint_through
                if delta_count > max_delta_events:
                    candidates.append(
                        RawCheckpointLedgerCandidate(
                            checkpoint_id=checkpoint_id,
                            checkpoint_through_sequence=checkpoint_through,
                            checkpoint_event=_owned_raw(checkpoint_event),
                            delta_events=(),
                            delta_event_count=delta_count,
                            delta_payload_bytes=0,
                            event_bound_satisfied=False,
                            byte_bound_satisfied=False,
                        )
                    )
                    continue
                delta = tuple(
                    _owned_raw(event)
                    for event in self._raw_events
                    if checkpoint_through
                    < event.sequence
                    <= requested_through_sequence
                )
                delta_bytes = sum(
                    len(event.canonical_payload_bytes) for event in delta
                )
                candidates.append(
                    RawCheckpointLedgerCandidate(
                        checkpoint_id=checkpoint_id,
                        checkpoint_through_sequence=checkpoint_through,
                        checkpoint_event=_owned_raw(checkpoint_event),
                        delta_events=delta,
                        delta_event_count=delta_count,
                        delta_payload_bytes=delta_bytes,
                        event_bound_satisfied=True,
                        byte_bound_satisfied=delta_bytes <= max_delta_bytes,
                    )
                )
        nearest = max(compatible, key=lambda item: item[2], default=None)
        return RawCheckpointLedgerSnapshot.build(
            runtime_session_id=self.runtime_session_id,
            requested_through_sequence=requested_through_sequence,
            ledger_high_water_observed=high_water,
            candidates=tuple(candidates),
            confirmed_checkpoint_count=len(catalog),
            contract_compatible_checkpoint_count=compatible_total,
            nearest_compatible_checkpoint_id=(nearest[1] if nearest else None),
            nearest_compatible_checkpoint_through_sequence=(
                nearest[2] if nearest else None
            ),
        )

    def replay(self, reply_id: str) -> Msg:
        events = self.iter(reply_id=reply_id)
        require_canonical_reply_control(events)
        start = next(
            (event for event in events if isinstance(event, ReplyStartEvent)), None
        )
        message = AssistantMsg(
            id=reply_id,
            name=start.name if start else "assistant",
            content=[],
            created_at=start.created_at if start else None,
        )
        reducer = MessageReducer(message)
        for event in events:
            reducer.append(event)
        return reducer.message

    def next_sequence(self) -> int:
        with self._lock:
            return self._next_sequence


def _validate_live_batch(events: list[AgentEvent]) -> None:
    if any(event.sequence is not None for event in events):
        raise ValueError("Live EventLog append requires sequence=None")
    ids = [event.id for event in events]
    if len(ids) != len(set(ids)):
        raise ValueError("Event ids must be unique within one batch")


def _validate_bundle_channel(
    events: tuple[RawStoredEventEnvelope, ...],
    bounds: RawEventSelectionBounds,
    label: str,
) -> None:
    if len(events) > bounds.max_events:
        raise ValueError(f"authority bundle {label} exceeds its event bound")
    if (
        sum(len(item.canonical_payload_bytes) for item in events)
        > bounds.max_payload_bytes
    ):
        raise ValueError(f"authority bundle {label} exceeds its byte bound")


def _owned_event(event: AgentEvent) -> AgentEvent:
    return load_agent_event(dump_agent_event(event))


def _raw(event: AgentEvent, runtime_session_id: str) -> RawStoredEventEnvelope:
    return RawStoredEventEnvelope.from_stored_event(
        event=event,
        runtime_session_id=runtime_session_id,
        schema_registry=DEFAULT_EVENT_SCHEMA_REGISTRY,
    )


def _owned_raw(event: RawStoredEventEnvelope) -> RawStoredEventEnvelope:
    return replace(event)


def _raw_resolved_model_call_id(event: RawStoredEventEnvelope) -> str | None:
    payload = json.loads(event.canonical_payload_bytes.decode("utf-8"))
    resolved = payload.get("resolved_call")
    if isinstance(resolved, dict):
        value = resolved.get("resolved_model_call_id")
        if isinstance(value, str):
            return value
    value = payload.get("resolved_model_call_id")
    if isinstance(value, str):
        return value
    attribution = payload.get("model_stream_attribution")
    if isinstance(attribution, dict):
        value = attribution.get("resolved_model_call_id")
        if isinstance(value, str):
            return value
    return None


def _validate_reply_snapshot_request(
    *,
    reply_ids: tuple[str, ...],
    through_sequence: int,
    max_total_events: int,
    max_total_payload_bytes: int,
) -> None:
    if (
        not reply_ids
        or any(not item for item in reply_ids)
        or len(reply_ids) != len(set(reply_ids))
    ):
        raise ValueError("reply snapshot ids must be non-empty and unique")
    if through_sequence < 0 or max_total_events < 1 or max_total_payload_bytes < 1:
        raise ValueError("reply snapshot bounds are invalid")


def _require_reply_snapshot_bounds(
    events: tuple[RawStoredEventEnvelope, ...],
    *,
    max_total_events: int,
    max_total_payload_bytes: int,
) -> None:
    if len(events) > max_total_events:
        raise ValueError("reply snapshot event count exceeds its aggregate bound")
    if sum(len(item.canonical_payload_bytes) for item in events) > max_total_payload_bytes:
        raise ValueError("reply snapshot payload exceeds its aggregate byte bound")
