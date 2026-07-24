"""PostgreSQL-backed EventLog implementation."""

from __future__ import annotations

from dataclasses import dataclass, field
from hashlib import sha256
from pathlib import Path
from time import monotonic
from typing import Iterable
from threading import RLock

from psycopg import sql
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from pulsara_agent.event.events import (
    AgentEvent,
    ReplyStartEvent,
    RunEndEvent,
    RunStartEvent,
)
from pulsara_agent.event_log.serialization import (
    DEFAULT_EVENT_SCHEMA_REGISTRY,
    EventSchemaContractMismatch,
    canonical_event_payload_bytes,
    dump_agent_event,
)
from pulsara_agent.event_log.postgres_pool import (
    PostgresConnectionLane,
    postgres_event_connection,
)
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
    RawEventTypeSelectionSnapshot,
    RawLedgerUsageSnapshot,
    RawEventSelectionBounds,
    RawReplyEventGroup,
    RawReplySelectionSnapshot,
    RawRuntimeProjectionCheckpoint,
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
from pulsara_agent.primitives.context import (
    canonical_json_bytes,
    canonical_utc_timestamp,
    context_fingerprint,
)
from pulsara_agent.primitives.authority_materialization import (
    LedgerMaterializationAccountStateFact,
    PhysicalChargeContractFact,
)
from pulsara_agent.storage.postgres_connection_provider import (
    VerifiedPostgresConnectionProviderProtocol,
)
from pulsara_agent.message.message import AssistantMsg, Msg
from pulsara_agent.message.reducer import (
    MessageReducer,
    require_canonical_reply_control,
)


def _runtime_projection_prefix_payload(
    prefix: RawTranscriptDomainPrefixFact,
) -> dict[str, object]:
    return {
        "through_sequence": prefix.through_sequence,
        "ledger_payload_bytes": prefix.ledger_payload_bytes,
        "semantic_event_count": prefix.semantic_event_count,
        "semantic_accumulator": prefix.semantic_accumulator,
        "ledger_continuity_accumulator": prefix.ledger_continuity_accumulator,
    }


@dataclass(slots=True)
class PostgresEventLog:
    connection_provider: VerifiedPostgresConnectionProviderProtocol
    runtime_session_id: str
    workspace_root: str | Path | None = None
    write_timeout_seconds: float = 30.0
    read_timeout_seconds: float = 30.0
    _parent_cache_lock: RLock = field(default_factory=RLock, init=False, repr=False)
    _session_parent_confirmed: bool = field(default=False, init=False, repr=False)
    _confirmed_parent_run_ids: set[str] = field(
        default_factory=set, init=False, repr=False
    )
    _confirmed_parent_turn_runs: dict[str, str] = field(
        default_factory=dict, init=False, repr=False
    )

    def ensure_runtime_session_owner(self) -> None:
        """Create the session row needed by artifacts produced before RunStart."""

        with postgres_event_connection(self.connection_provider) as connection:
            with connection.cursor() as cursor:
                self._lock_session(cursor)
                self._ensure_session_row(cursor)
        with self._parent_cache_lock:
            self._session_parent_confirmed = True

    def append(
        self,
        event: AgentEvent,
        *,
        expected_last_sequence: int | None = None,
        deadline_monotonic: float | None = None,
    ) -> AgentEvent:
        _validate_live_batch([event])
        deadline = self._write_deadline(deadline_monotonic)
        with self._parent_cache_lock:
            with postgres_event_connection(
                self.connection_provider,
                lane=PostgresConnectionLane.CRITICAL_WRITE,
                deadline_monotonic=deadline,
            ) as connection:
                with connection.cursor() as cursor:
                    self._apply_transaction_deadline(
                        cursor, deadline, include_lock=True
                    )
                    self._lock_session(cursor)
                    existing = self._get_by_id(cursor, event.id)
                    if existing is not None:
                        if same_event_payload(event, existing):
                            return existing
                        raise EventIdConflict(event.id)
                    next_sequence = self._next_sequence(cursor)
                    actual_last_sequence = next_sequence - 1
                    if (
                        expected_last_sequence is not None
                        and expected_last_sequence != actual_last_sequence
                    ):
                        raise EventLogWriteConflict(
                            expected_last_sequence=expected_last_sequence,
                            actual_last_sequence=actual_last_sequence,
                        )
                    ensured_runs, ensured_turns = self._ensure_parent_rows_batch(
                        cursor, [event]
                    )
                    stored, _ = self._with_canonical_sequence(event, next_sequence)
                    self._insert_event(cursor, stored)
                    self._sync_run_projection(cursor, stored)
            self._session_parent_confirmed = True
            self._confirmed_parent_run_ids.update(ensured_runs)
            self._confirmed_parent_turn_runs.update(
                (turn_id, event.run_id) for turn_id, event in ensured_turns
            )
            return stored

    def extend(
        self,
        events: Iterable[AgentEvent],
        *,
        expected_last_sequence: int | None = None,
        deadline_monotonic: float | None = None,
    ) -> list[AgentEvent]:
        event_list = list(events)
        if not event_list:
            return []
        _validate_live_batch(event_list)
        deadline = self._write_deadline(deadline_monotonic)

        with self._parent_cache_lock:
            with postgres_event_connection(
                self.connection_provider,
                lane=PostgresConnectionLane.CRITICAL_WRITE,
                deadline_monotonic=deadline,
            ) as connection:
                with connection.cursor() as cursor:
                    self._apply_transaction_deadline(
                        cursor, deadline, include_lock=True
                    )
                    self._lock_session(cursor)
                    stored_events: list[AgentEvent] = []
                    next_sequence = self._next_sequence(cursor)
                    actual_last_sequence = next_sequence - 1
                    if (
                        expected_last_sequence is not None
                        and expected_last_sequence != actual_last_sequence
                    ):
                        raise EventLogWriteConflict(
                            expected_last_sequence=expected_last_sequence,
                            actual_last_sequence=actual_last_sequence,
                        )
                    self._ensure_event_ids_available(cursor, event_list)
                    ensured_runs, ensured_turns = self._ensure_parent_rows_batch(
                        cursor, event_list
                    )
                    for event in event_list:
                        stored, next_sequence = self._with_canonical_sequence(
                            event, next_sequence
                        )
                        stored_events.append(stored)
                    self._insert_events(cursor, stored_events)
                    for stored in stored_events:
                        self._sync_run_projection(cursor, stored)
            self._session_parent_confirmed = True
            self._confirmed_parent_run_ids.update(ensured_runs)
            self._confirmed_parent_turn_runs.update(
                (turn_id, event.run_id) for turn_id, event in ensured_turns
            )
            return stored_events

    def read_materialization_account_state(
        self,
        *,
        deadline_monotonic: float | None = None,
    ) -> LedgerMaterializationAccountStateFact | None:
        deadline = self._read_deadline(deadline_monotonic)
        with postgres_event_connection(
            self.connection_provider,
            lane=PostgresConnectionLane.BOUNDED_READ,
            deadline_monotonic=deadline,
        ) as connection:
            with connection.cursor(row_factory=dict_row) as cursor:
                self._apply_transaction_deadline(cursor, deadline, include_lock=False)
                cursor.execute(
                    """
                    select state_payload
                    from ledger_materialization_accounts
                    where session_id = %s
                    """,
                    (self.runtime_session_id,),
                )
                row = cursor.fetchone()
        if row is None:
            return None
        return LedgerMaterializationAccountStateFact.model_validate(
            row["state_payload"]
        )

    def read_runtime_projection_checkpoint(
        self,
        projection_kind: str,
        *,
        deadline_monotonic: float | None = None,
    ) -> RawRuntimeProjectionCheckpoint | None:
        if not projection_kind:
            raise ValueError("runtime projection kind is required")
        deadline = self._read_deadline(deadline_monotonic)
        with postgres_event_connection(
            self.connection_provider,
            lane=PostgresConnectionLane.BOUNDED_READ,
            deadline_monotonic=deadline,
        ) as connection:
            with connection.cursor(row_factory=dict_row) as cursor:
                self._apply_transaction_deadline(cursor, deadline, include_lock=False)
                cursor.execute(
                    """
                    select projection_kind, through_sequence,
                           projection_schema_version, ledger_prefix,
                           validation_base_through_sequence,
                           validation_base_state_payload, state_payload,
                           payload_fingerprint
                    from runtime_projection_checkpoints
                    where session_id = %s and projection_kind = %s
                    """,
                    (self.runtime_session_id, projection_kind),
                )
                row = cursor.fetchone()
        if row is None:
            return None
        return RawRuntimeProjectionCheckpoint(
            projection_kind=str(row["projection_kind"]),
            through_sequence=int(row["through_sequence"]),
            projection_schema_version=str(row["projection_schema_version"]),
            ledger_prefix=RawTranscriptDomainPrefixFact(
                **dict(row["ledger_prefix"])
            ),
            validation_base_through_sequence=int(
                row["validation_base_through_sequence"]
            ),
            validation_base_state_payload=dict(
                row["validation_base_state_payload"]
            ),
            state_payload=dict(row["state_payload"]),
            payload_fingerprint=str(row["payload_fingerprint"]),
        )

    def write_runtime_projection_checkpoint(
        self,
        checkpoint: RawRuntimeProjectionCheckpoint,
        *,
        deadline_monotonic: float | None = None,
    ) -> None:
        deadline = self._write_deadline(deadline_monotonic)
        with postgres_event_connection(
            self.connection_provider,
            lane=PostgresConnectionLane.CRITICAL_WRITE,
            deadline_monotonic=deadline,
        ) as connection:
            with connection.cursor(row_factory=dict_row) as cursor:
                self._apply_transaction_deadline(cursor, deadline, include_lock=True)
                self._lock_session(cursor)
                high_water = self._read_transcript_prefix(
                    cursor,
                    sequence=None,
                ).through_sequence
                if checkpoint.through_sequence > high_water:
                    raise ValueError(
                        "runtime projection checkpoint exceeds ledger high-water"
                    )
                committed_prefix = self._read_transcript_prefix(
                    cursor,
                    sequence=checkpoint.through_sequence,
                )
                if checkpoint.ledger_prefix != committed_prefix:
                    raise ValueError(
                        "runtime projection checkpoint ledger prefix is untrusted"
                    )
                cursor.execute(
                    """
                    select through_sequence, projection_schema_version,
                           ledger_prefix, validation_base_through_sequence,
                           validation_base_state_payload, state_payload,
                           payload_fingerprint
                    from runtime_projection_checkpoints
                    where session_id = %s and projection_kind = %s
                    for update
                    """,
                    (self.runtime_session_id, checkpoint.projection_kind),
                )
                existing = cursor.fetchone()
                if (
                    existing is not None
                    and int(existing["through_sequence"]) > checkpoint.through_sequence
                ):
                    raise ValueError(
                        "runtime projection checkpoint cannot move backwards"
                    )
                if existing is not None and int(
                    existing["through_sequence"]
                ) == checkpoint.through_sequence:
                    expected_prefix_payload = _runtime_projection_prefix_payload(
                        checkpoint.ledger_prefix
                    )
                    if (
                        str(existing["projection_schema_version"])
                        != checkpoint.projection_schema_version
                        or dict(existing["ledger_prefix"])
                        != expected_prefix_payload
                        or int(existing["validation_base_through_sequence"])
                        != checkpoint.validation_base_through_sequence
                        or dict(existing["validation_base_state_payload"])
                        != checkpoint.validation_base_state_payload
                        or dict(existing["state_payload"])
                        != checkpoint.state_payload
                        or str(existing["payload_fingerprint"])
                        != checkpoint.payload_fingerprint
                    ):
                        raise ValueError(
                            "runtime projection checkpoint conflicts at one high-water"
                        )
                if (
                    existing is not None
                    and checkpoint.through_sequence
                    > int(existing["through_sequence"])
                    and (
                        checkpoint.validation_base_through_sequence
                        != int(existing["through_sequence"])
                        or checkpoint.validation_base_state_payload
                        != dict(existing["state_payload"])
                        or checkpoint.projection_schema_version
                        != str(existing["projection_schema_version"])
                    )
                ):
                    raise ValueError(
                        "runtime projection checkpoint validation base drifted"
                    )
                if (
                    existing is None
                    and checkpoint.validation_base_through_sequence != 0
                ):
                    raise ValueError(
                        "initial runtime projection checkpoint must start at ledger genesis"
                    )
                cursor.execute(
                    """
                    insert into runtime_projection_checkpoints (
                        session_id, projection_kind, through_sequence,
                        projection_schema_version, ledger_prefix,
                        validation_base_through_sequence,
                        validation_base_state_payload, payload_fingerprint,
                        state_payload, updated_at
                    ) values (%s, %s, %s, %s, %s, %s, %s, %s, %s, now())
                    on conflict (session_id, projection_kind) do update set
                        through_sequence = excluded.through_sequence,
                        projection_schema_version = excluded.projection_schema_version,
                        ledger_prefix = excluded.ledger_prefix,
                        validation_base_through_sequence =
                            excluded.validation_base_through_sequence,
                        validation_base_state_payload =
                            excluded.validation_base_state_payload,
                        payload_fingerprint = excluded.payload_fingerprint,
                        state_payload = excluded.state_payload,
                        updated_at = now()
                    """,
                    (
                        self.runtime_session_id,
                        checkpoint.projection_kind,
                        checkpoint.through_sequence,
                        checkpoint.projection_schema_version,
                        Jsonb(
                            _runtime_projection_prefix_payload(
                                checkpoint.ledger_prefix
                            )
                        ),
                        checkpoint.validation_base_through_sequence,
                        Jsonb(checkpoint.validation_base_state_payload),
                        checkpoint.payload_fingerprint,
                        Jsonb(checkpoint.state_payload),
                    ),
                )

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
        event_list = list(events)
        if not event_list:
            raise ValueError("materialization state commit requires events")
        _validate_live_batch(event_list)
        deadline = self._write_deadline(deadline_monotonic)
        with self._parent_cache_lock:
            with postgres_event_connection(
                self.connection_provider,
                lane=PostgresConnectionLane.CRITICAL_WRITE,
                deadline_monotonic=deadline,
            ) as connection:
                with connection.cursor(row_factory=dict_row) as cursor:
                    self._apply_transaction_deadline(
                        cursor, deadline, include_lock=True
                    )
                    self._lock_session(cursor)
                    cursor.execute(
                        """
                        select account_state_fingerprint
                        from ledger_materialization_accounts
                        where session_id = %s
                        for update
                        """,
                        (self.runtime_session_id,),
                    )
                    account_row = cursor.fetchone()
                    actual_account_fingerprint = (
                        str(account_row["account_state_fingerprint"])
                        if account_row is not None
                        else None
                    )
                    if actual_account_fingerprint != expected_account_state_fingerprint:
                        raise MaterializationAccountStateConflict(
                            expected_state_fingerprint=(
                                expected_account_state_fingerprint
                            ),
                            actual_state_fingerprint=actual_account_fingerprint,
                        )

                    next_sequence = self._next_sequence(cursor)
                    actual_last_sequence = next_sequence - 1
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

                    self._ensure_event_ids_available(cursor, event_list)
                    ensured_runs, ensured_turns = self._ensure_parent_rows_batch(
                        cursor, event_list
                    )
                    stored_events: list[AgentEvent] = []
                    for event in event_list:
                        stored, next_sequence = self._with_canonical_sequence(
                            event, next_sequence
                        )
                        stored_events.append(stored)
                    self._validate_materialization_envelope_charge_bounds(
                        stored_events,
                        physical_charge_contract,
                    )
                    self._insert_events(cursor, stored_events)
                    for stored in stored_events:
                        self._sync_run_projection(cursor, stored)

                    generation = resulting_account_state.generation
                    cursor.execute(
                        """
                        insert into ledger_materialization_accounts (
                            session_id,
                            account_state_fingerprint,
                            ledger_materialization_generation,
                            consumer_horizon_revision,
                            ledger_through_sequence,
                            state_payload,
                            updated_at
                        ) values (%s, %s, %s, %s, %s, %s, now())
                        on conflict (session_id) do update set
                            account_state_fingerprint = excluded.account_state_fingerprint,
                            ledger_materialization_generation =
                                excluded.ledger_materialization_generation,
                            consumer_horizon_revision =
                                excluded.consumer_horizon_revision,
                            ledger_through_sequence = excluded.ledger_through_sequence,
                            state_payload = excluded.state_payload,
                            updated_at = now()
                        """,
                        (
                            self.runtime_session_id,
                            resulting_account_state.account_state_fingerprint,
                            generation.ledger_materialization_generation,
                            generation.consumer_horizon_revision,
                            resulting_account_state.ledger_through_sequence,
                            Jsonb(resulting_account_state.model_dump(mode="json")),
                        ),
                    )
                    if transaction_companion is not None:
                        transaction_companion.apply_postgres(
                            cursor,
                            stored_events,
                        )
            self._session_parent_confirmed = True
            self._confirmed_parent_run_ids.update(ensured_runs)
            self._confirmed_parent_turn_runs.update(
                (turn_id, event.run_id) for turn_id, event in ensured_turns
            )
            return stored_events

    def _validate_materialization_envelope_charge_bounds(
        self,
        stored_events: list[AgentEvent],
        contract: PhysicalChargeContractFact,
    ) -> None:
        bounds = {
            (item.event_type, item.event_schema_version): item
            for item in contract.bookkeeping_event_bounds
        }
        for stored in stored_events:
            binding = DEFAULT_EVENT_SCHEMA_REGISTRY.resolve_for_event(
                stored
            ).schema_contract
            bound = bounds.get((str(stored.type), binding.event_schema_version))
            if bound is None:
                continue
            actual = len(canonical_event_payload_bytes(stored)) + (
                contract.fixed_sequence_wrapper_charge_bytes_per_event
                + contract.fixed_schema_wrapper_charge_bytes_per_event
            )
            if (
                str(stored.type) == "PHYSICAL_OPERATION_CHARGE_APPLIED"
                and actual > stored.charge.charge_applied_event_charge_payload_bytes
            ):
                raise ValueError(
                    "stored charge-applied envelope exceeds its dynamic charge bound"
                )
            if actual > bound.max_stored_envelope_bytes:
                raise ValueError(
                    "stored bookkeeping envelope exceeds fixed charge bound"
                )

    def repair_run_projection(
        self,
        *,
        deadline_monotonic: float | None = None,
    ) -> int:
        """Rebuild this session's runs summary rows from canonical events."""

        deadline = self._write_deadline(deadline_monotonic)
        with postgres_event_connection(
            self.connection_provider,
            lane=PostgresConnectionLane.CRITICAL_WRITE,
            deadline_monotonic=deadline,
        ) as connection:
            with connection.cursor() as cursor:
                self._apply_transaction_deadline(cursor, deadline, include_lock=True)
                self._lock_session(cursor)
                cursor.execute(
                    """
                    with starts as (
                        select run_id, min(created_at) as started_at
                        from agent_events
                        where session_id = %s and event_type = 'RUN_START'
                        group by run_id
                    )
                    update runs r
                    set started_at = starts.started_at
                    from starts
                    where r.session_id = %s and r.id = starts.run_id
                    """,
                    (self.runtime_session_id, self.runtime_session_id),
                )
                updated = cursor.rowcount
                cursor.execute(
                    """
                    with latest_end as (
                        select distinct on (run_id)
                            run_id,
                            payload->>'status' as status,
                            payload->>'stop_reason' as stop_reason,
                            created_at as completed_at
                        from agent_events
                        where session_id = %s and event_type = 'RUN_END'
                        order by run_id, sequence desc
                    )
                    update runs r
                    set
                        status = latest_end.status,
                        stop_reason = latest_end.stop_reason,
                        completed_at = latest_end.completed_at
                    from latest_end
                    where r.session_id = %s and r.id = latest_end.run_id
                    """,
                    (self.runtime_session_id, self.runtime_session_id),
                )
                updated += cursor.rowcount
                cursor.execute(
                    """
                    update runs r
                    set status = 'running', stop_reason = null, completed_at = null
                    where r.session_id = %s
                      and not exists (
                        select 1
                        from agent_events e
                        where e.session_id = %s
                          and e.run_id = r.id
                          and e.event_type = 'RUN_END'
                      )
                    """,
                    (self.runtime_session_id, self.runtime_session_id),
                )
                updated += cursor.rowcount
                return updated

    def iter(
        self,
        *,
        run_id: str | None = None,
        turn_id: str | None = None,
        reply_id: str | None = None,
        after_sequence: int | None = None,
        deadline_monotonic: float | None = None,
    ) -> list[AgentEvent]:
        predicates = [sql.SQL("session_id = %s")]
        params: list[str] = [self.runtime_session_id]

        if after_sequence is not None:
            predicates.append(sql.SQL("sequence > %s"))
            params.append(after_sequence)
        if run_id is not None:
            predicates.append(sql.SQL("run_id = %s"))
            params.append(run_id)
        if turn_id is not None:
            predicates.append(sql.SQL("turn_id = %s"))
            params.append(turn_id)
        if reply_id is not None:
            predicates.append(sql.SQL("reply_id = %s"))
            params.append(reply_id)

        query = sql.SQL(
            """
            select id, session_id, run_id, turn_id, reply_id, sequence,
                   event_type, event_schema_version,
                   event_schema_fingerprint,
                   event_domain_contract_fingerprint,
                   created_at, payload
            from agent_events
            where {where}
            order by sequence asc
            """
        ).format(where=sql.SQL(" and ").join(predicates))

        deadline = self._read_deadline(deadline_monotonic)
        with postgres_event_connection(
            self.connection_provider,
            lane=PostgresConnectionLane.BOUNDED_READ,
            deadline_monotonic=deadline,
        ) as connection:
            with connection.cursor(row_factory=dict_row) as cursor:
                self._apply_transaction_deadline(cursor, deadline, include_lock=False)
                cursor.execute(query, params)
                return [
                    self._raw_from_row(row).decode_owned(DEFAULT_EVENT_SCHEMA_REGISTRY)
                    for row in cursor.fetchall()
                ]

    def get_by_id(
        self,
        event_id: str,
        *,
        deadline_monotonic: float | None = None,
    ) -> AgentEvent | None:
        deadline = self._read_deadline(deadline_monotonic)
        with postgres_event_connection(
            self.connection_provider,
            lane=PostgresConnectionLane.BOUNDED_READ,
            deadline_monotonic=deadline,
        ) as connection:
            with connection.cursor() as cursor:
                self._apply_transaction_deadline(cursor, deadline, include_lock=False)
                return self._get_by_id(cursor, event_id)

    def confirm_batch(
        self,
        candidates,
        *,
        deadline_monotonic: float | None = None,
    ) -> EventBatchConfirmation:
        candidate_list = list(candidates)
        ids = [event.id for event in candidate_list]
        if len(ids) != len(set(ids)):
            raise ValueError("Confirmed event ids must be unique within one batch")
        deadline = self._write_deadline(deadline_monotonic)
        with postgres_event_connection(
            self.connection_provider,
            lane=PostgresConnectionLane.CRITICAL_WRITE,
            deadline_monotonic=deadline,
        ) as connection:
            with connection.cursor() as cursor:
                self._apply_transaction_deadline(cursor, deadline, include_lock=True)
                self._lock_session(cursor)
                committed: list[AgentEvent] = []
                missing: list[str] = []
                for candidate in candidate_list:
                    raw = self._get_raw_by_id(cursor, candidate.id)
                    if raw is None:
                        missing.append(candidate.id)
                        continue
                    contract = DEFAULT_EVENT_SCHEMA_REGISTRY.resolve_for_event(
                        candidate
                    ).schema_contract
                    if (
                        contract.event_schema_version != raw.event_schema_version
                        or contract.event_schema_fingerprint
                        != raw.event_schema_fingerprint
                        or contract.domain_contract_fingerprint
                        != raw.event_domain_contract_fingerprint
                        or not same_event_raw_payload(candidate, raw)
                    ):
                        raise EventIdConflict(candidate.id)
                    committed.append(raw.decode_owned(DEFAULT_EVENT_SCHEMA_REGISTRY))
                return EventBatchConfirmation(
                    committed_events=tuple(committed),
                    missing_event_ids=tuple(missing),
                    actual_last_sequence=self._next_sequence(cursor) - 1,
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
                event.decode_owned(DEFAULT_EVENT_SCHEMA_REGISTRY)
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
        if minimum_sequence < 1:
            raise ValueError("minimum sequence must be positive")
        if max_events is not None and max_events < 1:
            raise ValueError("event range max_events must be positive")
        if max_payload_bytes is not None and max_payload_bytes < 1:
            raise ValueError("event range max_payload_bytes must be positive")
        deadline = self._read_deadline(deadline_monotonic)
        with postgres_event_connection(
            self.connection_provider,
            lane=PostgresConnectionLane.BOUNDED_READ,
            deadline_monotonic=deadline,
        ) as connection:
            with connection.cursor(row_factory=dict_row) as cursor:
                cursor.execute("set transaction isolation level repeatable read")
                self._apply_transaction_deadline(cursor, deadline, include_lock=False)
                cursor.execute(
                    "select coalesce(max(sequence), 0) as high_water "
                    "from agent_events where session_id = %s",
                    (self.runtime_session_id,),
                )
                current_high_water = int(cursor.fetchone()["high_water"])
                effective_through = (
                    current_high_water if through_sequence is None else through_sequence
                )
                if effective_through > current_high_water:
                    raise ValueError(
                        "requested event high-water has not been committed"
                    )
                if effective_through < minimum_sequence:
                    raise ValueError("event read range is empty or reversed")
                limit_clause = " limit %s" if max_events is not None else ""
                parameters: tuple[object, ...] = (
                    self.runtime_session_id,
                    minimum_sequence,
                    effective_through,
                    *((max_events + 1,) if max_events is not None else ()),
                )
                cursor.execute(
                    f"""
                    select id, session_id, run_id, turn_id, reply_id, sequence,
                           event_type, event_schema_version,
                           event_schema_fingerprint,
                           event_domain_contract_fingerprint,
                           created_at, payload
                    from agent_events
                    where session_id = %s
                      and sequence >= %s
                      and sequence <= %s
                    order by sequence asc
                    {limit_clause}
                    """,
                    parameters,
                )
                events = tuple(self._raw_from_row(row) for row in cursor.fetchall())
                if max_events is not None and len(events) > max_events:
                    raise ValueError("event range exceeds its event bound")
                if (
                    max_payload_bytes is not None
                    and sum(len(event.canonical_payload_bytes) for event in events)
                    > max_payload_bytes
                ):
                    raise ValueError("event range exceeds its payload-byte bound")
        return RawEventLogReadSnapshot(
            through_sequence=effective_through,
            events=events,
            snapshot_fingerprint=context_fingerprint(
                "raw-event-log-read-snapshot:v1",
                {
                    "through_sequence": effective_through,
                    "envelopes": tuple(event.envelope_fingerprint for event in events),
                },
            ),
        )

    def read_raw_events_by_id(
        self,
        event_ids: tuple[str, ...],
        *,
        deadline_monotonic: float | None = None,
    ) -> tuple[RawStoredEventEnvelope, ...]:
        if len(event_ids) != len(set(event_ids)):
            raise ValueError("raw event ids must be unique")
        if not event_ids:
            return ()
        deadline = self._read_deadline(deadline_monotonic)
        with postgres_event_connection(
            self.connection_provider,
            lane=PostgresConnectionLane.BOUNDED_READ,
            deadline_monotonic=deadline,
        ) as connection:
            with connection.cursor(row_factory=dict_row) as cursor:
                self._apply_transaction_deadline(cursor, deadline, include_lock=False)
                cursor.execute(
                    """
                    select id, session_id, run_id, turn_id, reply_id, sequence,
                           event_type, event_schema_version,
                           event_schema_fingerprint,
                           event_domain_contract_fingerprint,
                           created_at, payload
                    from agent_events
                    where session_id = %s and id = any(%s)
                    """,
                    (self.runtime_session_id, list(event_ids)),
                )
                by_id = {
                    row["id"]: self._raw_from_row(row) for row in cursor.fetchall()
                }
        return tuple(by_id[event_id] for event_id in event_ids if event_id in by_id)

    def read_raw_events_by_id_snapshot(
        self,
        event_ids: tuple[str, ...],
        *,
        deadline_monotonic: float | None = None,
    ) -> RawEventIdSelectionSnapshot:
        if len(event_ids) != len(set(event_ids)):
            raise ValueError("raw event ids must be unique")
        deadline = self._read_deadline(deadline_monotonic)
        with postgres_event_connection(
            self.connection_provider,
            lane=PostgresConnectionLane.BOUNDED_READ,
            deadline_monotonic=deadline,
        ) as connection:
            with connection.cursor(row_factory=dict_row) as cursor:
                cursor.execute("set transaction isolation level repeatable read")
                self._apply_transaction_deadline(cursor, deadline, include_lock=False)
                cursor.execute(
                    "select coalesce(max(sequence), 0) as high_water "
                    "from agent_events where session_id = %s",
                    (self.runtime_session_id,),
                )
                through_sequence = int(cursor.fetchone()["high_water"])
                by_id: dict[str, RawStoredEventEnvelope] = {}
                if event_ids:
                    cursor.execute(
                        """
                        select id, session_id, run_id, turn_id, reply_id, sequence,
                               event_type, event_schema_version,
                               event_schema_fingerprint,
                               event_domain_contract_fingerprint,
                               created_at, payload
                        from agent_events
                        where session_id = %s and id = any(%s)
                        """,
                        (self.runtime_session_id, list(event_ids)),
                    )
                    by_id = {
                        row["id"]: self._raw_from_row(row) for row in cursor.fetchall()
                    }
        return RawEventIdSelectionSnapshot(
            through_sequence=through_sequence,
            events=tuple(
                by_id[event_id] for event_id in event_ids if event_id in by_id
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
        if limit < 1:
            raise ValueError("raw event type read limit must be positive")
        if through_sequence is not None and through_sequence < 0:
            raise ValueError("raw event type high-water cannot be negative")
        deadline = self._read_deadline(deadline_monotonic)
        with postgres_event_connection(
            self.connection_provider,
            lane=PostgresConnectionLane.BOUNDED_READ,
            deadline_monotonic=deadline,
        ) as connection:
            with connection.cursor(row_factory=dict_row) as cursor:
                self._apply_transaction_deadline(cursor, deadline, include_lock=False)
                query = """
                    select id, session_id, run_id, turn_id, reply_id, sequence,
                           event_type, event_schema_version,
                           event_schema_fingerprint,
                           event_domain_contract_fingerprint,
                           created_at, payload
                    from agent_events
                    where session_id = %s and event_type = %s
                """
                parameters: list[object] = [self.runtime_session_id, event_type]
                if through_sequence is not None:
                    query += " and sequence <= %s"
                    parameters.append(through_sequence)
                query += " order by sequence desc limit %s"
                parameters.append(limit)
                cursor.execute(query, tuple(parameters))
                return tuple(self._raw_from_row(row) for row in cursor.fetchall())

    def read_raw_model_call_events(
        self,
        resolved_model_call_id: str,
        *,
        max_events: int,
        max_payload_bytes: int,
        deadline_monotonic: float | None = None,
    ) -> tuple[RawStoredEventEnvelope, ...]:
        if not resolved_model_call_id:
            raise ValueError("resolved model call id must be non-empty")
        if max_events < 1 or max_payload_bytes < 1:
            raise ValueError("model-call read bounds must be positive")
        deadline = self._read_deadline(deadline_monotonic)
        with postgres_event_connection(
            self.connection_provider,
            lane=PostgresConnectionLane.BOUNDED_READ,
            deadline_monotonic=deadline,
        ) as connection:
            with connection.cursor(row_factory=dict_row) as cursor:
                self._apply_transaction_deadline(cursor, deadline, include_lock=False)
                cursor.execute(
                    """
                    select id, session_id, run_id, turn_id, reply_id, sequence,
                           event_type, event_schema_version,
                           event_schema_fingerprint,
                           event_domain_contract_fingerprint,
                           created_at, payload
                    from agent_events
                    where session_id = %s
                      and coalesce(
                            payload #>> '{resolved_call,resolved_model_call_id}',
                            payload #>> '{resolved_model_call_id}',
                            payload #>> '{model_stream_attribution,resolved_model_call_id}'
                          ) = %s
                    order by sequence asc
                    limit %s
                    """,
                    (
                        self.runtime_session_id,
                        resolved_model_call_id,
                        max_events + 1,
                    ),
                )
                selected = tuple(self._raw_from_row(row) for row in cursor.fetchall())
                if len(selected) > max_events:
                    raise ValueError("model-call event count exceeds its read bound")
                if (
                    sum(len(event.canonical_payload_bytes) for event in selected)
                    > max_payload_bytes
                ):
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
        if not event_types or len(event_types) != len(set(event_types)):
            raise ValueError("raw event types must be non-empty and unique")
        if run_ids is not None and (not run_ids or len(run_ids) != len(set(run_ids))):
            raise ValueError("run id selection must be non-empty and unique")
        if minimum_sequence < 1 or max_events < 1 or max_payload_bytes < 1:
            raise ValueError("sparse event read bounds are invalid")
        deadline = self._read_deadline(deadline_monotonic)
        with postgres_event_connection(
            self.connection_provider,
            lane=PostgresConnectionLane.BOUNDED_READ,
            deadline_monotonic=deadline,
        ) as connection:
            with connection.cursor(row_factory=dict_row) as cursor:
                cursor.execute("set transaction isolation level repeatable read")
                self._apply_transaction_deadline(cursor, deadline, include_lock=False)
                cursor.execute(
                    "select coalesce(max(sequence), 0) as high_water "
                    "from agent_events where session_id = %s",
                    (self.runtime_session_id,),
                )
                current_high_water = int(cursor.fetchone()["high_water"])
                high_water = (
                    current_high_water if through_sequence is None else through_sequence
                )
                if high_water < 0 or high_water > current_high_water:
                    raise ValueError(
                        "requested sparse high-water has not been committed"
                    )
                active_run_clause = (
                    """
                      and run_id in (
                        select id from runs
                        where session_id = %s and status = 'running'
                      )
                    """
                    if active_runs_only
                    else ""
                )
                run_id_clause = " and run_id = any(%s)" if run_ids is not None else ""
                parameters: list[object] = [
                    self.runtime_session_id,
                    list(event_types),
                    minimum_sequence,
                    high_water,
                ]
                if run_ids is not None:
                    parameters.append(list(run_ids))
                if active_runs_only:
                    parameters.append(self.runtime_session_id)
                parameters.append(max_events + 1)
                cursor.execute(
                    f"""
                    select id, session_id, run_id, turn_id, reply_id, sequence,
                           event_type, event_schema_version,
                           event_schema_fingerprint,
                           event_domain_contract_fingerprint,
                           created_at, payload
                    from agent_events
                    where session_id = %s and event_type = any(%s)
                      and sequence >= %s
                      and sequence <= %s
                    {run_id_clause}
                    {active_run_clause}
                    order by sequence asc
                    limit %s
                    """,
                    tuple(parameters),
                )
                events = tuple(self._raw_from_row(row) for row in cursor.fetchall())
                if len(events) > max_events:
                    raise ValueError("sparse event selection exceeds its event bound")
                if (
                    sum(len(item.canonical_payload_bytes) for item in events)
                    > max_payload_bytes
                ):
                    raise ValueError("sparse event selection exceeds its byte bound")
                return RawEventTypeSelectionSnapshot(
                    through_sequence=high_water,
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
        if after_sequence < 0 or max_events < 1 or max_payload_bytes < 1:
            raise ValueError("transcript domain delta bounds are invalid")
        if not registry_contract_fingerprint:
            raise ValueError("transcript registry contract fingerprint is required")
        deadline = self._read_deadline(deadline_monotonic)
        with postgres_event_connection(
            self.connection_provider,
            lane=PostgresConnectionLane.BOUNDED_READ,
            deadline_monotonic=deadline,
        ) as connection:
            with connection.cursor(row_factory=dict_row) as control:
                control.execute("set transaction isolation level repeatable read")
                self._apply_transaction_deadline(
                    control,
                    deadline,
                    include_lock=False,
                )
                control.execute(
                    "select coalesce(max(sequence), 0) as high_water "
                    "from agent_events where session_id = %s",
                    (self.runtime_session_id,),
                )
                high_water = int(control.fetchone()["high_water"])
                effective_through = (
                    high_water if through_sequence is None else through_sequence
                )
                if effective_through > high_water or effective_through < after_sequence:
                    raise ValueError("transcript domain delta range is invalid")
                before = self._read_transcript_prefix(
                    control,
                    sequence=after_sequence,
                )
                after = self._read_transcript_prefix(
                    control,
                    sequence=effective_through,
                )
            events: list[RawStoredEventEnvelope] = []
            payload_bytes = 0
            with connection.cursor(
                name="pulsara_transcript_domain_delta",
                row_factory=dict_row,
            ) as cursor:
                cursor.execute(
                    """
                    select id, session_id, run_id, turn_id, reply_id, sequence,
                           event_type, event_schema_version,
                           event_schema_fingerprint,
                           event_domain_contract_fingerprint,
                           created_at, payload
                    from agent_events
                    where session_id = %s
                      and transcript_event_domain = 'transcript_semantic'
                      and sequence > %s
                      and sequence <= %s
                    order by sequence asc
                    """,
                    (
                        self.runtime_session_id,
                        after_sequence,
                        effective_through,
                    ),
                )
                while row := cursor.fetchone():
                    if len(events) == max_events:
                        raise ValueError(
                            "transcript semantic delta exceeds its event bound"
                        )
                    raw = self._raw_from_row(row)
                    payload_bytes += len(raw.canonical_payload_bytes)
                    if payload_bytes > max_payload_bytes:
                        raise ValueError(
                            "transcript semantic delta exceeds its byte bound"
                        )
                    events.append(raw)
        self._validate_transcript_semantic_delta(
            before=before,
            after=after,
            semantic_events=tuple(events),
        )
        return RawTranscriptDomainDeltaSnapshot.build(
            runtime_session_id=self.runtime_session_id,
            before=before,
            after=after,
            semantic_events=tuple(events),
            registry_contract_fingerprint=registry_contract_fingerprint,
        )

    @staticmethod
    def _validate_transcript_semantic_delta(
        *,
        before: RawTranscriptDomainPrefixFact,
        after: RawTranscriptDomainPrefixFact,
        semantic_events: tuple[RawStoredEventEnvelope, ...],
    ) -> None:
        count = before.semantic_event_count
        accumulator = before.semantic_accumulator
        for raw in semantic_events:
            event = raw.decode_owned(DEFAULT_EVENT_SCHEMA_REGISTRY)
            count += 1
            accumulator = advance_transcript_semantic_accumulator(
                accumulator,
                event=event,
                event_schema_version=raw.event_schema_version,
                event_schema_fingerprint=raw.event_schema_fingerprint,
            )
        if count != after.semantic_event_count:
            raise ValueError("transcript semantic prefix count is inconsistent")
        if accumulator != after.semantic_accumulator:
            raise ValueError("transcript semantic prefix accumulator is inconsistent")

    def read_context_authority_bundle(
        self,
        request: RawContextAuthorityBundleRequest,
        *,
        deadline_monotonic: float | None = None,
    ) -> RawContextAuthorityBundle:
        """Freeze one context high-water and return every local authority channel."""

        deadline = self._read_deadline(deadline_monotonic)
        channels: dict[str, list[RawStoredEventEnvelope]] = {
            "primary": [],
            "run_sparse": [],
            "session_sparse": [],
            "exact": [],
        }
        high_water: int | None = None
        ledger_prefix: RawTranscriptDomainPrefixFact | None = None
        with postgres_event_connection(
            self.connection_provider,
            lane=PostgresConnectionLane.BOUNDED_READ,
            deadline_monotonic=deadline,
        ) as connection:
            with connection.cursor(row_factory=dict_row) as control:
                control.execute("set transaction isolation level repeatable read")
                self._apply_transaction_deadline(control, deadline, include_lock=False)
            with connection.cursor(
                name="pulsara_context_authority_bundle",
                row_factory=dict_row,
            ) as cursor:
                cursor.execute(
                    """
                    with boundary as (
                        select coalesce(max(sequence), 0)::bigint as high_water
                        from agent_events
                        where session_id = %s
                    ),
                    primary_events as (
                        select 'primary'::text as channel, b.high_water,
                               e.id, e.session_id, e.run_id, e.turn_id, e.reply_id,
                               e.sequence, e.event_type, e.event_schema_version,
                               e.event_schema_fingerprint,
                               e.event_domain_contract_fingerprint,
                               e.created_at, e.payload
                        from boundary b
                        join lateral (
                            select * from agent_events
                            where session_id = %s
                              and sequence >= %s
                              and sequence <= b.high_water
                            order by sequence asc
                            limit %s
                        ) e on true
                    ),
                    run_sparse_events as (
                        select 'run_sparse'::text as channel, b.high_water,
                               e.id, e.session_id, e.run_id, e.turn_id, e.reply_id,
                               e.sequence, e.event_type, e.event_schema_version,
                               e.event_schema_fingerprint,
                               e.event_domain_contract_fingerprint,
                               e.created_at, e.payload
                        from boundary b
                        join lateral (
                            select * from agent_events
                            where session_id = %s
                              and run_id = %s
                              and event_type = any(%s)
                              and sequence <= b.high_water
                            order by sequence asc
                            limit %s
                        ) e on true
                    ),
                    session_sparse_events as (
                        select 'session_sparse'::text as channel, b.high_water,
                               e.id, e.session_id, e.run_id, e.turn_id, e.reply_id,
                               e.sequence, e.event_type, e.event_schema_version,
                               e.event_schema_fingerprint,
                               e.event_domain_contract_fingerprint,
                               e.created_at, e.payload
                        from boundary b
                        join lateral (
                            select * from agent_events
                            where session_id = %s
                              and event_type = any(%s)
                              and sequence <= b.high_water
                            order by sequence asc
                            limit %s
                        ) e on true
                    ),
                    exact_events as (
                        select 'exact'::text as channel, b.high_water,
                               e.id, e.session_id, e.run_id, e.turn_id, e.reply_id,
                               e.sequence, e.event_type, e.event_schema_version,
                               e.event_schema_fingerprint,
                               e.event_domain_contract_fingerprint,
                               e.created_at, e.payload
                        from boundary b
                        join lateral (
                            select * from agent_events
                            where session_id = %s
                              and id = any(%s)
                              and sequence <= b.high_water
                            order by sequence asc
                            limit %s
                        ) e on true
                    )
                    select * from primary_events
                    union all select * from run_sparse_events
                    union all select * from session_sparse_events
                    union all select * from exact_events
                    union all
                    select 'meta'::text, b.high_water,
                           null::text, null::text, null::text, null::text,
                           null::text, null::bigint, null::text, null::text,
                           null::text, null::text, null::timestamptz, null::jsonb
                    from boundary b
                    order by channel, sequence nulls first
                    """,
                    (
                        self.runtime_session_id,
                        self.runtime_session_id,
                        request.primary_minimum_sequence,
                        request.primary_bounds.max_events + 1,
                        self.runtime_session_id,
                        request.run_id,
                        list(request.run_sparse_event_types),
                        request.run_sparse_bounds.max_events + 1,
                        self.runtime_session_id,
                        list(request.session_sparse_event_types),
                        request.session_sparse_bounds.max_events + 1,
                        self.runtime_session_id,
                        list(request.exact_event_ids),
                        request.exact_bounds.max_events + 1,
                    ),
                )
                while rows := cursor.fetchmany(128):
                    for row in rows:
                        row_high_water = int(row["high_water"])
                        if high_water is None:
                            high_water = row_high_water
                        elif row_high_water != high_water:
                            raise ValueError("authority bundle high-water drifted")
                        channel = str(row["channel"])
                        if channel == "meta":
                            continue
                        if channel not in channels:
                            raise ValueError(
                                "authority bundle returned unknown channel"
                            )
                        channels[channel].append(self._raw_from_row(row))
            if high_water is None:
                raise ValueError("authority bundle did not return a high-water")
            with connection.cursor(row_factory=dict_row) as prefix_cursor:
                self._apply_transaction_deadline(
                    prefix_cursor, deadline, include_lock=False
                )
                ledger_prefix = self._read_transcript_prefix(
                    prefix_cursor,
                    sequence=high_water,
                )
        if high_water is None:
            raise ValueError("authority bundle did not return a high-water")
        if ledger_prefix is None:
            raise ValueError("authority bundle did not return a ledger prefix")
        primary = tuple(channels["primary"])
        run_sparse = tuple(channels["run_sparse"])
        session_sparse = tuple(channels["session_sparse"])
        exact = tuple(channels["exact"])
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
            ledger_prefix=ledger_prefix,
        )

    def read_raw_ledger_prefix(
        self,
        *,
        through_sequence: int | None = None,
        deadline_monotonic: float | None = None,
    ) -> RawTranscriptDomainPrefixFact:
        deadline = self._read_deadline(deadline_monotonic)
        with postgres_event_connection(
            self.connection_provider,
            lane=PostgresConnectionLane.BOUNDED_READ,
            deadline_monotonic=deadline,
        ) as connection:
            with connection.cursor(row_factory=dict_row) as cursor:
                self._apply_transaction_deadline(cursor, deadline, include_lock=False)
                return self._read_transcript_prefix(
                    cursor,
                    sequence=through_sequence,
                )

    def read_raw_reply_events(
        self,
        reply_id: str,
        *,
        max_events: int,
        max_payload_bytes: int,
        deadline_monotonic: float | None = None,
    ) -> tuple[RawStoredEventEnvelope, ...]:
        if not reply_id or max_events < 1 or max_payload_bytes < 1:
            raise ValueError("reply event read bounds are invalid")
        deadline = self._read_deadline(deadline_monotonic)
        with postgres_event_connection(
            self.connection_provider,
            lane=PostgresConnectionLane.BOUNDED_READ,
            deadline_monotonic=deadline,
        ) as connection:
            with connection.cursor(row_factory=dict_row) as cursor:
                self._apply_transaction_deadline(cursor, deadline, include_lock=False)
                cursor.execute(
                    """
                    select id, session_id, run_id, turn_id, reply_id, sequence,
                           event_type, event_schema_version,
                           event_schema_fingerprint,
                           event_domain_contract_fingerprint,
                           created_at, payload
                    from agent_events
                    where session_id = %s and reply_id = %s
                    order by sequence asc
                    limit %s
                    """,
                    (self.runtime_session_id, reply_id, max_events + 1),
                )
                selected = tuple(self._raw_from_row(row) for row in cursor.fetchall())
        if len(selected) > max_events:
            raise ValueError("reply event count exceeds its read bound")
        if (
            sum(len(item.canonical_payload_bytes) for item in selected)
            > max_payload_bytes
        ):
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
        _validate_reply_snapshot_request(
            reply_ids=reply_ids,
            through_sequence=through_sequence,
            max_total_events=max_total_events,
            max_total_payload_bytes=max_total_payload_bytes,
        )
        deadline = self._read_deadline(deadline_monotonic)
        selected: list[RawStoredEventEnvelope] = []
        payload_bytes = 0
        with postgres_event_connection(
            self.connection_provider,
            lane=PostgresConnectionLane.BOUNDED_READ,
            deadline_monotonic=deadline,
        ) as connection:
            with connection.cursor(row_factory=dict_row) as control:
                control.execute("set transaction isolation level repeatable read")
                self._apply_transaction_deadline(control, deadline, include_lock=False)
            with connection.cursor(
                name="pulsara_reply_snapshot",
                row_factory=dict_row,
            ) as cursor:
                cursor.execute(
                    """
                    select id, session_id, run_id, turn_id, reply_id, sequence,
                           event_type, event_schema_version,
                           event_schema_fingerprint,
                           event_domain_contract_fingerprint,
                           created_at, payload
                    from agent_events
                    where session_id = %s and reply_id = any(%s)
                      and sequence <= %s
                    order by sequence asc
                    limit %s
                    """,
                    (
                        self.runtime_session_id,
                        list(reply_ids),
                        through_sequence,
                        max_total_events + 1,
                    ),
                )
                while rows := cursor.fetchmany(128):
                    for row in rows:
                        item = self._raw_from_row(row)
                        selected.append(item)
                        payload_bytes += len(item.canonical_payload_bytes)
                        if len(selected) > max_total_events:
                            raise ValueError(
                                "reply snapshot event count exceeds its aggregate bound"
                            )
                        if payload_bytes > max_total_payload_bytes:
                            raise ValueError(
                                "reply snapshot payload exceeds its aggregate byte bound"
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
        if not run_id or max_events < 1 or max_payload_bytes < 1:
            raise ValueError("run event read bounds are invalid")
        deadline = self._read_deadline(deadline_monotonic)
        with postgres_event_connection(
            self.connection_provider,
            lane=PostgresConnectionLane.BOUNDED_READ,
            deadline_monotonic=deadline,
        ) as connection:
            with connection.cursor(row_factory=dict_row) as cursor:
                self._apply_transaction_deadline(cursor, deadline, include_lock=False)
                cursor.execute(
                    """
                    select id, session_id, run_id, turn_id, reply_id, sequence,
                           event_type, event_schema_version,
                           event_schema_fingerprint,
                           event_domain_contract_fingerprint,
                           created_at, payload
                    from agent_events
                    where session_id = %s and run_id = %s
                    order by sequence asc
                    limit %s
                    """,
                    (self.runtime_session_id, run_id, max_events + 1),
                )
                selected = tuple(self._raw_from_row(row) for row in cursor.fetchall())
        if len(selected) > max_events:
            raise ValueError("run event count exceeds its read bound")
        if (
            sum(len(item.canonical_payload_bytes) for item in selected)
            > max_payload_bytes
        ):
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
        if requested_through_sequence < 1:
            raise ValueError("checkpoint requested high-water must be positive")
        if max_delta_events < 0 or max_delta_bytes < 0 or max_checkpoint_candidates < 1:
            raise ValueError("checkpoint read bounds are invalid")
        deadline = self._read_deadline(deadline_monotonic)
        with postgres_event_connection(
            self.connection_provider,
            lane=PostgresConnectionLane.BOUNDED_READ,
            deadline_monotonic=deadline,
        ) as connection:
            with connection.cursor(row_factory=dict_row) as cursor:
                cursor.execute("set transaction isolation level repeatable read")
                self._apply_transaction_deadline(cursor, deadline, include_lock=False)
                cursor.execute(
                    "select coalesce(max(sequence), 0) as high_water "
                    "from agent_events where session_id = %s",
                    (self.runtime_session_id,),
                )
                high_water = int(cursor.fetchone()["high_water"])
                if requested_through_sequence > high_water:
                    raise ValueError(
                        "requested checkpoint high-water has not been committed"
                    )
                cursor.execute(
                    """
                    select count(*) as checkpoint_count
                    from agent_events
                    where session_id = %s and event_type = %s
                    """,
                    (self.runtime_session_id, checkpoint_event_type),
                )
                confirmed_count = int(cursor.fetchone()["checkpoint_count"])
                compatible_predicate = """
                    session_id = %s
                    and event_type = %s
                    and payload #>> '{checkpoint,graph_reducer_id}' = %s
                    and payload #>> '{checkpoint,graph_reducer_version}' = %s
                    and payload #>> '{checkpoint,graph_reducer_contract_fingerprint}' = %s
                """
                contract_params = (
                    self.runtime_session_id,
                    checkpoint_event_type,
                    graph_reducer_id,
                    graph_reducer_version,
                    graph_reducer_contract_fingerprint,
                )
                cursor.execute(
                    f"select count(*) as compatible_count from agent_events where {compatible_predicate}",
                    contract_params,
                )
                compatible_count = int(cursor.fetchone()["compatible_count"])
                cursor.execute(
                    f"""
                    select id, session_id, run_id, turn_id, reply_id, sequence,
                           event_type, event_schema_version,
                           event_schema_fingerprint,
                           event_domain_contract_fingerprint,
                           created_at, payload
                    from agent_events
                    where {compatible_predicate}
                      and (payload #>> '{{checkpoint,through_sequence}}')::bigint <= %s
                    order by
                      case when payload #>> '{{checkpoint,checkpoint_id}}' = %s
                           then 0 else 1 end,
                      (payload #>> '{{checkpoint,through_sequence}}')::bigint desc,
                      sequence desc
                    limit %s
                    """,
                    (
                        *contract_params,
                        requested_through_sequence,
                        preferred_checkpoint_id,
                        max_checkpoint_candidates,
                    ),
                )
                catalog_rows = tuple(cursor.fetchall())
                catalog: list[tuple[RawStoredEventEnvelope, str, int]] = []
                for row in catalog_rows:
                    raw = self._raw_from_row(row)
                    (
                        checkpoint_id,
                        checkpoint_through,
                        _reducer_id,
                        _reducer_version,
                        _reducer_fingerprint,
                    ) = raw_checkpoint_catalog_identity(raw)
                    catalog.append((raw, checkpoint_id, checkpoint_through))
                candidates: list[RawCheckpointLedgerCandidate] = []
                for checkpoint_event, checkpoint_id, checkpoint_through in catalog:
                    delta_count = requested_through_sequence - checkpoint_through
                    if delta_count > max_delta_events:
                        candidates.append(
                            RawCheckpointLedgerCandidate(
                                checkpoint_id=checkpoint_id,
                                checkpoint_through_sequence=checkpoint_through,
                                checkpoint_event=checkpoint_event,
                                delta_events=(),
                                delta_event_count=delta_count,
                                delta_payload_bytes=0,
                                event_bound_satisfied=False,
                                byte_bound_satisfied=False,
                            )
                        )
                        continue
                    cursor.execute(
                        """
                        select id, session_id, run_id, turn_id, reply_id, sequence,
                               event_type, event_schema_version,
                               event_schema_fingerprint,
                               event_domain_contract_fingerprint,
                               created_at, payload
                        from agent_events
                        where session_id = %s
                          and sequence > %s
                          and sequence <= %s
                        order by sequence asc
                        """,
                        (
                            self.runtime_session_id,
                            checkpoint_through,
                            requested_through_sequence,
                        ),
                    )
                    delta = tuple(self._raw_from_row(row) for row in cursor.fetchall())
                    delta_bytes = sum(
                        len(event.canonical_payload_bytes) for event in delta
                    )
                    candidates.append(
                        RawCheckpointLedgerCandidate(
                            checkpoint_id=checkpoint_id,
                            checkpoint_through_sequence=checkpoint_through,
                            checkpoint_event=checkpoint_event,
                            delta_events=delta,
                            delta_event_count=delta_count,
                            delta_payload_bytes=delta_bytes,
                            event_bound_satisfied=True,
                            byte_bound_satisfied=delta_bytes <= max_delta_bytes,
                        )
                    )
        nearest = max(catalog, key=lambda item: item[2], default=None)
        return RawCheckpointLedgerSnapshot.build(
            runtime_session_id=self.runtime_session_id,
            requested_through_sequence=requested_through_sequence,
            ledger_high_water_observed=high_water,
            candidates=tuple(candidates),
            confirmed_checkpoint_count=confirmed_count,
            contract_compatible_checkpoint_count=compatible_count,
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

    def next_sequence(self, *, deadline_monotonic: float | None = None) -> int:
        deadline = self._read_deadline(deadline_monotonic)
        with postgres_event_connection(
            self.connection_provider,
            lane=PostgresConnectionLane.BOUNDED_READ,
            deadline_monotonic=deadline,
        ) as connection:
            with connection.cursor() as cursor:
                self._apply_transaction_deadline(cursor, deadline, include_lock=False)
                return self._next_sequence(cursor)

    def read_ledger_usage_snapshot(
        self,
        *,
        deadline_monotonic: float | None = None,
    ) -> RawLedgerUsageSnapshot:
        """Read physical shadow bootstrap totals without decoding the ledger."""

        deadline = self._read_deadline(deadline_monotonic)
        with postgres_event_connection(
            self.connection_provider,
            lane=PostgresConnectionLane.BOUNDED_READ,
            deadline_monotonic=deadline,
        ) as connection:
            with connection.cursor(row_factory=dict_row) as cursor:
                self._apply_transaction_deadline(cursor, deadline, include_lock=False)
                cursor.execute(
                    """
                    select coalesce(max(sequence), 0) as through_sequence,
                           count(*) as event_count,
                           coalesce(sum(octet_length(payload::text)), 0)
                               as candidate_payload_bytes
                    from agent_events
                    where session_id = %s
                    """,
                    (self.runtime_session_id,),
                )
                row = cursor.fetchone()
        if row is None:  # pragma: no cover - aggregate always returns one row
            raise RuntimeError("ledger usage aggregate returned no row")
        return RawLedgerUsageSnapshot(
            through_sequence=int(row["through_sequence"]),
            event_count=int(row["event_count"]),
            candidate_payload_bytes=int(row["candidate_payload_bytes"]),
        )

    def _lock_session(self, cursor) -> None:
        cursor.execute(
            "select pg_advisory_xact_lock(hashtextextended(%s, 0))",
            (self.runtime_session_id,),
        )

    def _write_deadline(self, deadline_monotonic: float | None) -> float:
        if deadline_monotonic is not None:
            if deadline_monotonic <= monotonic():
                raise TimeoutError("event-write deadline exceeded")
            return deadline_monotonic
        if self.write_timeout_seconds <= 0:
            raise ValueError("event-write timeout must be positive")
        return monotonic() + self.write_timeout_seconds

    def _read_deadline(self, deadline_monotonic: float | None) -> float:
        if deadline_monotonic is not None:
            if deadline_monotonic <= monotonic():
                raise TimeoutError("event-read deadline exceeded")
            return deadline_monotonic
        if self.read_timeout_seconds <= 0:
            raise ValueError("event-read timeout must be positive")
        return monotonic() + self.read_timeout_seconds

    @staticmethod
    def _apply_transaction_deadline(
        cursor,
        deadline_monotonic: float,
        *,
        include_lock: bool,
    ) -> None:
        remaining_ms = int((deadline_monotonic - monotonic()) * 1000)
        if remaining_ms <= 0:
            raise TimeoutError("PostgreSQL operation deadline exceeded")
        timeout = str(max(1, remaining_ms))
        cursor.execute(
            "select set_config('statement_timeout', %s, true)",
            (timeout,),
        )
        if include_lock:
            cursor.execute(
                "select set_config('lock_timeout', %s, true)",
                (timeout,),
            )

    def _ensure_parent_rows_batch(
        self,
        cursor,
        events: list[AgentEvent],
    ) -> tuple[tuple[str, ...], tuple[tuple[str, AgentEvent], ...]]:
        """Validate each unique run/turn identity once per committed batch."""

        if not self._session_parent_confirmed:
            self._ensure_session_row(cursor)
        runs: dict[str, AgentEvent] = {}
        turns: dict[str, AgentEvent] = {}
        for event in events:
            prior_run = runs.setdefault(event.run_id, event)
            if prior_run.run_id != event.run_id:
                raise ValueError("event batch run identity drifted")
            prior_turn = turns.setdefault(event.turn_id, event)
            if prior_turn.run_id != event.run_id:
                raise ValueError("event batch reuses a turn across runs")
        ensured_runs = tuple(
            run_id for run_id in runs if run_id not in self._confirmed_parent_run_ids
        )
        ensured_turns = tuple(
            (turn_id, event)
            for turn_id, event in turns.items()
            if turn_id not in self._confirmed_parent_turn_runs
        )
        for turn_id, event in turns.items():
            confirmed_run_id = self._confirmed_parent_turn_runs.get(turn_id)
            if confirmed_run_id is not None and confirmed_run_id != event.run_id:
                raise ValueError(
                    f"turn {turn_id!r} already belongs to runtime session "
                    f"run {confirmed_run_id!r}, not {event.run_id!r}"
                )
        for run_id in ensured_runs:
            event = runs[run_id]
            cursor.execute(
                """
                insert into runs (id, session_id)
                values (%s, %s)
                on conflict (id) do nothing
                """,
                (event.run_id, self.runtime_session_id),
            )
            self._ensure_run_belongs_to_session(cursor, event)
        for turn_id, event in ensured_turns:
            cursor.execute(
                """
                insert into turns (id, session_id, run_id, turn_index)
                select %s, %s, %s, coalesce(max(turn_index), 0) + 1
                from turns
                where run_id = %s
                on conflict (id) do nothing
                """,
                (
                    event.turn_id,
                    self.runtime_session_id,
                    event.run_id,
                    event.run_id,
                ),
            )
            self._ensure_turn_belongs_to_run(cursor, event)
        return ensured_runs, ensured_turns

    def _ensure_session_row(self, cursor) -> None:
        cursor.execute(
            """
            insert into sessions (id, workspace_root)
            values (%s, %s)
            on conflict (id) do nothing
            """,
            (
                self.runtime_session_id,
                str(self.workspace_root) if self.workspace_root is not None else None,
            ),
        )

    def _ensure_run_belongs_to_session(self, cursor, event: AgentEvent) -> None:
        cursor.execute("select session_id from runs where id = %s", (event.run_id,))
        row = cursor.fetchone()
        if row is None:
            return
        session_id = row["session_id"] if isinstance(row, dict) else row[0]
        if session_id != self.runtime_session_id:
            raise ValueError(
                f"run_id {event.run_id!r} already belongs to runtime session {session_id!r}"
            )

    def _ensure_turn_belongs_to_run(self, cursor, event: AgentEvent) -> None:
        cursor.execute(
            "select session_id, run_id from turns where id = %s", (event.turn_id,)
        )
        row = cursor.fetchone()
        if row is None:
            return
        if isinstance(row, dict):
            session_id, run_id = row["session_id"], row["run_id"]
        else:
            session_id, run_id = row
        if session_id != self.runtime_session_id or run_id != event.run_id:
            raise ValueError(
                f"turn_id {event.turn_id!r} already belongs to runtime session {session_id!r} "
                f"and run {run_id!r}"
            )

    def _insert_event(self, cursor, stored: AgentEvent) -> None:
        prefix = self._transcript_prefix_rows(cursor, [stored])[0]
        cursor.execute(
            """
            insert into agent_events (
                id,
                session_id,
                run_id,
                turn_id,
                reply_id,
                sequence,
                event_type,
                event_schema_version,
                event_schema_fingerprint,
                event_domain_contract_fingerprint,
                transcript_event_domain,
                transcript_semantic_prefix_count,
                transcript_semantic_prefix_accumulator,
                ledger_continuity_accumulator,
                ledger_payload_prefix_bytes,
                created_at,
                payload
            )
            values (
                %s, %s, %s, %s, %s, %s, %s, %s,
                %s, %s, %s, %s, %s, %s, %s, %s::timestamptz, %s
            )
            """,
            self._event_insert_params(stored, prefix),
        )

    def _insert_events(self, cursor, stored_events: list[AgentEvent]) -> None:
        row_template = (
            "(%s, %s, %s, %s, %s, %s, %s, %s, "
            "%s, %s, %s, %s, %s, %s, %s, %s::timestamptz, %s)"
        )
        prefix_rows = self._transcript_prefix_rows(cursor, stored_events)
        # Keep well below PostgreSQL's parameter ceiling while preserving one
        # physical INSERT for the normal model-stream/event batch.
        for offset in range(0, len(stored_events), 1_000):
            chunk = stored_events[offset : offset + 1_000]
            prefix_chunk = prefix_rows[offset : offset + 1_000]
            parameters = tuple(
                value
                for stored, prefix in zip(chunk, prefix_chunk, strict=True)
                for value in self._event_insert_params(stored, prefix)
            )
            cursor.execute(
                """
                insert into agent_events (
                    id, session_id, run_id, turn_id, reply_id, sequence,
                    event_type, event_schema_version, event_schema_fingerprint,
                    event_domain_contract_fingerprint, transcript_event_domain,
                    transcript_semantic_prefix_count,
                    transcript_semantic_prefix_accumulator,
                    ledger_continuity_accumulator,
                    ledger_payload_prefix_bytes, created_at, payload
                )
                values
                """
                + ",".join(row_template for _ in chunk),
                parameters,
            )

    def _event_insert_params(
        self,
        stored: AgentEvent,
        prefix: tuple[str, RawTranscriptDomainPrefixFact],
    ) -> tuple[object, ...]:
        payload = dump_agent_event(stored)
        contract = DEFAULT_EVENT_SCHEMA_REGISTRY.resolve_for_event(
            stored
        ).schema_contract
        return (
            stored.id,
            self.runtime_session_id,
            stored.run_id,
            stored.turn_id,
            stored.reply_id,
            stored.sequence,
            str(stored.type),
            contract.event_schema_version,
            contract.event_schema_fingerprint,
            contract.domain_contract_fingerprint,
            prefix[0],
            prefix[1].semantic_event_count,
            prefix[1].semantic_accumulator,
            prefix[1].ledger_continuity_accumulator,
            prefix[1].ledger_payload_bytes,
            stored.created_at,
            Jsonb(payload),
        )

    def _transcript_prefix_rows(
        self,
        cursor,
        stored_events: list[AgentEvent],
    ) -> list[tuple[str, RawTranscriptDomainPrefixFact]]:
        previous = self._read_transcript_prefix(cursor, sequence=None)
        rows: list[tuple[str, RawTranscriptDomainPrefixFact]] = []
        for stored in stored_events:
            raw = RawStoredEventEnvelope.from_stored_event(
                event=stored,
                runtime_session_id=self.runtime_session_id,
                schema_registry=DEFAULT_EVENT_SCHEMA_REGISTRY,
            )
            domain = classify_transcript_event_type(raw.event_type)
            semantic_count = previous.semantic_event_count
            semantic_accumulator = previous.semantic_accumulator
            if domain == "transcript_semantic":
                semantic_count += 1
                semantic_accumulator = advance_transcript_semantic_accumulator(
                    previous.semantic_accumulator,
                    event=stored,
                    event_schema_version=raw.event_schema_version,
                    event_schema_fingerprint=raw.event_schema_fingerprint,
                )
            previous = RawTranscriptDomainPrefixFact(
                through_sequence=raw.sequence,
                ledger_payload_bytes=(
                    previous.ledger_payload_bytes + len(raw.canonical_payload_bytes)
                ),
                semantic_event_count=semantic_count,
                semantic_accumulator=semantic_accumulator,
                ledger_continuity_accumulator=advance_ledger_continuity_accumulator(
                    previous.ledger_continuity_accumulator,
                    envelope_fingerprint=raw.envelope_fingerprint,
                ),
            )
            rows.append((domain, previous))
        return rows

    def _read_transcript_prefix(
        self,
        cursor,
        *,
        sequence: int | None,
    ) -> RawTranscriptDomainPrefixFact:
        if sequence == 0:
            return RawTranscriptDomainPrefixFact(
                through_sequence=0,
                ledger_payload_bytes=0,
                semantic_event_count=0,
                semantic_accumulator=EMPTY_TRANSCRIPT_SEMANTIC_ACCUMULATOR,
                ledger_continuity_accumulator=EMPTY_LEDGER_CONTINUITY_ACCUMULATOR,
            )
        if sequence is None:
            cursor.execute(
                """
                select sequence, transcript_semantic_prefix_count,
                       transcript_semantic_prefix_accumulator,
                       ledger_continuity_accumulator,
                       ledger_payload_prefix_bytes
                from agent_events
                where session_id = %s
                order by sequence desc
                limit 1
                """,
                (self.runtime_session_id,),
            )
        else:
            cursor.execute(
                """
                select sequence, transcript_semantic_prefix_count,
                       transcript_semantic_prefix_accumulator,
                       ledger_continuity_accumulator,
                       ledger_payload_prefix_bytes
                from agent_events
                where session_id = %s and sequence = %s
                """,
                (self.runtime_session_id, sequence),
            )
        row = cursor.fetchone()
        if row is None:
            if sequence is None:
                return self._read_transcript_prefix(cursor, sequence=0)
            raise ValueError("transcript prefix sequence is not committed")
        if isinstance(row, dict):
            values = (
                row["sequence"],
                row["transcript_semantic_prefix_count"],
                row["transcript_semantic_prefix_accumulator"],
                row["ledger_continuity_accumulator"],
                row["ledger_payload_prefix_bytes"],
            )
        else:
            values = row
        return RawTranscriptDomainPrefixFact(
            through_sequence=int(values[0]),
            ledger_payload_bytes=int(values[4]),
            semantic_event_count=int(values[1]),
            semantic_accumulator=str(values[2]),
            ledger_continuity_accumulator=str(values[3]),
        )

    def _ensure_event_ids_available(self, cursor, events: list[AgentEvent]) -> None:
        ids = [event.id for event in events]
        cursor.execute(
            "select id from agent_events where session_id = %s and id = any(%s)",
            (self.runtime_session_id, ids),
        )
        row = cursor.fetchone()
        if row is not None:
            event_id = row["id"] if isinstance(row, dict) else row[0]
            raise ValueError(f"Event id already exists in this session: {event_id}")

    def _get_by_id(self, cursor, event_id: str) -> AgentEvent | None:
        raw = self._get_raw_by_id(cursor, event_id)
        if raw is None:
            return None
        return raw.decode_owned(DEFAULT_EVENT_SCHEMA_REGISTRY)

    def _get_raw_by_id(self, cursor, event_id: str) -> RawStoredEventEnvelope | None:
        cursor.execute(
            """
            select id, session_id, run_id, turn_id, reply_id, sequence,
                   event_type, event_schema_version,
                   event_schema_fingerprint,
                   event_domain_contract_fingerprint,
                   created_at, payload
            from agent_events
            where session_id = %s and id = %s
            """,
            (self.runtime_session_id, event_id),
        )
        row = cursor.fetchone()
        if row is None:
            return None
        if not isinstance(row, dict):
            columns = (
                "id",
                "session_id",
                "run_id",
                "turn_id",
                "reply_id",
                "sequence",
                "event_type",
                "event_schema_version",
                "event_schema_fingerprint",
                "event_domain_contract_fingerprint",
                "created_at",
                "payload",
            )
            row = dict(zip(columns, row, strict=True))
        return self._raw_from_row(row)

    def _raw_from_row(self, row) -> RawStoredEventEnvelope:
        schema_identity = (
            row["event_schema_version"],
            row["event_schema_fingerprint"],
            row["event_domain_contract_fingerprint"],
        )
        if any(value is None or not str(value) for value in schema_identity):
            raise EventSchemaContractMismatch(
                "stored event row lacks explicit per-event schema identity"
            )
        payload_bytes = canonical_json_bytes(row["payload"])
        values = {
            "stored_envelope_version": "stored-agent-event:v1",
            "event_id": str(row["id"]),
            "runtime_session_id": str(row["session_id"]),
            "run_id": str(row["run_id"]),
            "turn_id": str(row["turn_id"]),
            "reply_id": str(row["reply_id"]),
            "sequence": int(row["sequence"]),
            "created_at_utc": canonical_utc_timestamp(row["created_at"].isoformat()),
            "event_type": str(row["event_type"]),
            "event_schema_version": str(row["event_schema_version"]),
            "event_schema_fingerprint": str(row["event_schema_fingerprint"]),
            "event_domain_contract_fingerprint": str(
                row["event_domain_contract_fingerprint"]
            ),
            "canonical_payload_bytes": payload_bytes,
            "payload_fingerprint": f"sha256:{sha256(payload_bytes).hexdigest()}",
        }
        return RawStoredEventEnvelope(
            **values,
            envelope_fingerprint=context_fingerprint(
                "stored-agent-event-envelope:v1",
                {
                    key: value
                    for key, value in values.items()
                    if key != "canonical_payload_bytes"
                },
            ),
        )

    def _sync_run_projection(self, cursor, stored: AgentEvent) -> None:
        if isinstance(stored, RunStartEvent):
            cursor.execute(
                """
                update runs
                set status = 'running',
                    stop_reason = null,
                    started_at = %s::timestamptz,
                    completed_at = null
                where id = %s and session_id = %s
                """,
                (stored.created_at, stored.run_id, self.runtime_session_id),
            )
            return

        if isinstance(stored, RunEndEvent):
            cursor.execute(
                """
                update runs
                set status = %s,
                    stop_reason = %s,
                    completed_at = %s::timestamptz
                where id = %s and session_id = %s
                """,
                (
                    stored.status,
                    stored.stop_reason,
                    stored.created_at,
                    stored.run_id,
                    self.runtime_session_id,
                ),
            )

    def _next_sequence(self, cursor) -> int:
        cursor.execute(
            """
            select coalesce(max(sequence), 0) + 1 as next_sequence
            from agent_events
            where session_id = %s
            """,
            (self.runtime_session_id,),
        )
        row = cursor.fetchone()
        return int(row["next_sequence"] if isinstance(row, dict) else row[0])

    def _with_canonical_sequence(
        self, event: AgentEvent, next_sequence: int
    ) -> tuple[AgentEvent, int]:
        return event.model_copy(update={"sequence": next_sequence}), next_sequence + 1


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
