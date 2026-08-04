"""Atomic PostgreSQL restore/checkpoint operations for the durable prompt queue."""

from __future__ import annotations

from dataclasses import asdict
from time import monotonic
from typing import TYPE_CHECKING

from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from pulsara_agent.primitives.stored_event import (
    RawRuntimeProjectionCheckpoint,
    RawTranscriptDomainPrefixFact,
)
from pulsara_agent.event_log.serialization import (
    hydrate_raw_stored_event_envelope_from_row,
)
from pulsara_agent.event_log.postgres_pool import (
    PostgresConnectionLane,
    postgres_event_connection,
)
from pulsara_agent.ports.prompt_queue import (
    PromptQueueCheckpointCommitGuard,
    PromptQueueCheckpointCommitOutcome,
    PromptQueueRestoreBundle,
)
from pulsara_agent.primitives.context import context_fingerprint
from pulsara_agent.primitives.prompt_queue import (
    PROMPT_QUEUE_EVENT_TYPE_VALUES,
    PromptQueueDomainCheckpointFact,
    PromptQueueHeadReceiptFact,
    build_prompt_queue_account_projection,
    build_prompt_queue_head_receipt,
)
from pulsara_agent.storage.prompt_queue_bootstrap import (
    PROMPT_QUEUE_PROJECTION_KIND,
    PROMPT_QUEUE_PROJECTION_SCHEMA_VERSION,
    prompt_queue_account_from_values,
)

if TYPE_CHECKING:
    from pulsara_agent.event_log.postgres import PostgresEventLog


_ACCOUNT_COLUMNS = """
    session_id, next_accepted_ordinal, queue_chain_head_event_id,
    queue_chain_head_sequence, queue_chain_head_payload_fingerprint,
    account_revision, checkpoint_generation,
    checkpoint_through_sequence, checkpoint_fingerprint,
    transition_count, transition_accumulator,
    bounded_tail_first_sequence, bounded_tail_count,
    bounded_tail_payload_bytes, bounded_tail_accumulator,
    pending_item_count, reserved_item_count, artifact_bytes,
    pending_item_head_set_accumulator,
    active_client_item_count, active_client_item_accumulator,
    row_set_accumulator,
    reducer_contract_fingerprint, event_registry_fingerprint,
    account_fingerprint
"""


def read_prompt_queue_restore_bundle(
    event_log: PostgresEventLog,
    *,
    max_delta_events: int,
    max_delta_payload_bytes: int,
    deadline_monotonic: float | None,
) -> PromptQueueRestoreBundle:
    if max_delta_events < 1 or max_delta_payload_bytes < 1:
        raise ValueError("prompt queue restore bounds must be positive")
    deadline = event_log._read_deadline(deadline_monotonic)
    with postgres_event_connection(
        event_log.connection_provider,
        lane=PostgresConnectionLane.BOUNDED_READ,
        deadline_monotonic=deadline,
    ) as connection:
        connection.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY")
        with connection.cursor(row_factory=dict_row) as cursor:
            event_log._apply_transaction_deadline(cursor, deadline, include_lock=False)
            ledger_prefix = event_log._read_transcript_prefix(cursor, sequence=None)
            checkpoint_row = cursor.execute(
                """
                SELECT projection_kind, through_sequence,
                       projection_schema_version, ledger_prefix,
                       validation_base_through_sequence,
                       validation_base_state_payload, state_payload,
                       payload_fingerprint
                FROM runtime_projection_checkpoints
                WHERE session_id = %s AND projection_kind = %s
                """,
                (event_log.runtime_session_id, PROMPT_QUEUE_PROJECTION_KIND),
            ).fetchone()
            account_row = cursor.execute(
                f"""
                SELECT {_ACCOUNT_COLUMNS}
                FROM prompt_queue_accounts
                WHERE session_id = %s
                """,
                (event_log.runtime_session_id,),
            ).fetchone()
            if checkpoint_row is None or account_row is None:
                raise ValueError("prompt queue durable genesis is missing")
            raw_checkpoint = _raw_checkpoint_from_row(checkpoint_row)
            checkpoint = PromptQueueDomainCheckpointFact.model_validate(
                raw_checkpoint.state_payload["checkpoint"]
            )
            account = prompt_queue_account_from_values(
                tuple(account_row[name] for name in _account_column_names())
            )
            item_rows = tuple(
                cursor.execute(
                    """
                    SELECT state_payload
                    FROM prompt_queue_items
                    WHERE session_id = %s
                    ORDER BY accepted_ordinal
                    LIMIT 257
                    """,
                    (event_log.runtime_session_id,),
                ).fetchall()
            )
            if len(item_rows) > 256:
                raise ValueError("prompt queue live row set exceeds its bound")
            event_rows = tuple(
                cursor.execute(
                    """
                    SELECT id, session_id, run_id, turn_id, reply_id, sequence,
                           event_type, event_schema_version,
                           event_schema_fingerprint,
                           event_domain_contract_fingerprint,
                           created_at, payload
                    FROM agent_events
                    WHERE session_id = %s
                      AND event_type = ANY(%s)
                      AND sequence > %s
                      AND sequence <= %s
                    ORDER BY sequence
                    LIMIT %s
                    """,
                    (
                        event_log.runtime_session_id,
                        list(PROMPT_QUEUE_EVENT_TYPE_VALUES),
                        checkpoint.through_sequence,
                        ledger_prefix.through_sequence,
                        max_delta_events + 1,
                    ),
                ).fetchall()
            )
    if len(event_rows) > max_delta_events:
        raise ValueError("prompt queue restore delta exceeds its event bound")
    events = tuple(
        hydrate_raw_stored_event_envelope_from_row(row) for row in event_rows
    )
    if (
        sum(len(item.canonical_payload_bytes) for item in events)
        > max_delta_payload_bytes
    ):
        raise ValueError("prompt queue restore delta exceeds its byte bound")
    item_payloads = tuple(dict(row["state_payload"]) for row in item_rows)
    checkpoint_item_payloads = tuple(
        dict(item) for item in raw_checkpoint.state_payload.get("items", ())
    )
    checkpoint_head_event_type_value = raw_checkpoint.state_payload.get(
        "head_event_type"
    )
    checkpoint_head_event_type = (
        str(checkpoint_head_event_type_value)
        if checkpoint_head_event_type_value is not None
        else None
    )
    payload = {
        "runtime_session_id": event_log.runtime_session_id,
        "ledger_high_water": ledger_prefix.through_sequence,
        "ledger_prefix": asdict(ledger_prefix),
        "raw_checkpoint_fingerprint": raw_checkpoint.payload_fingerprint,
        "checkpoint_fingerprint": checkpoint.checkpoint_fingerprint,
        "account_fingerprint": account.account_fingerprint,
        "item_row_fingerprints": tuple(
            str(item["row_fingerprint"]) for item in item_payloads
        ),
        "delta_envelopes": tuple(item.envelope_fingerprint for item in events),
    }
    return PromptQueueRestoreBundle(
        runtime_session_id=event_log.runtime_session_id,
        ledger_high_water=ledger_prefix.through_sequence,
        ledger_prefix=ledger_prefix,
        raw_checkpoint=raw_checkpoint,
        checkpoint=checkpoint,
        account=account,
        checkpoint_item_payloads=checkpoint_item_payloads,
        checkpoint_head_event_type=checkpoint_head_event_type,
        current_item_payloads=item_payloads,
        bounded_delta_events=events,
        bundle_fingerprint=context_fingerprint(
            "prompt-queue-restore-bundle:v1", payload
        ),
    )


def commit_prompt_queue_checkpoint(
    event_log: PostgresEventLog,
    *,
    candidate: RawRuntimeProjectionCheckpoint,
    checkpoint: PromptQueueDomainCheckpointFact,
    guard: PromptQueueCheckpointCommitGuard,
    deadline_monotonic: float | None,
) -> PromptQueueCheckpointCommitOutcome:
    _validate_candidate(
        event_log, candidate=candidate, checkpoint=checkpoint, guard=guard
    )
    deadline = event_log._write_deadline(deadline_monotonic)
    try:
        with postgres_event_connection(
            event_log.connection_provider,
            lane=PostgresConnectionLane.CRITICAL_WRITE,
            deadline_monotonic=deadline,
        ) as connection:
            with connection.cursor(row_factory=dict_row) as cursor:
                event_log._apply_transaction_deadline(
                    cursor, deadline, include_lock=True
                )
                event_log._lock_session(cursor)
                predecessor_row = cursor.execute(
                    """
                    SELECT projection_kind, through_sequence,
                           projection_schema_version, ledger_prefix,
                           validation_base_through_sequence,
                           validation_base_state_payload, state_payload,
                           payload_fingerprint
                    FROM runtime_projection_checkpoints
                    WHERE session_id = %s AND projection_kind = %s
                    FOR UPDATE
                    """,
                    (event_log.runtime_session_id, PROMPT_QUEUE_PROJECTION_KIND),
                ).fetchone()
                account_row = cursor.execute(
                    f"""
                    SELECT {_ACCOUNT_COLUMNS}
                    FROM prompt_queue_accounts
                    WHERE session_id = %s
                    FOR UPDATE
                    """,
                    (event_log.runtime_session_id,),
                ).fetchone()
                if predecessor_row is None or account_row is None:
                    raise ValueError("prompt queue checkpoint durable owner is missing")
                predecessor = _raw_checkpoint_from_row(predecessor_row)
                account = prompt_queue_account_from_values(
                    tuple(account_row[name] for name in _account_column_names())
                )
                _validate_guard(predecessor=predecessor, account=account, guard=guard)
                committed_prefix = event_log._read_transcript_prefix(
                    cursor, sequence=checkpoint.through_sequence
                )
                if committed_prefix != candidate.ledger_prefix:
                    raise ValueError("prompt queue checkpoint ledger prefix drifted")
                resulting_account = build_prompt_queue_account_projection(
                    runtime_session_id=account.runtime_session_id,
                    next_accepted_ordinal=account.next_accepted_ordinal,
                    queue_chain_head_event_id=account.queue_chain_head_event_id,
                    queue_chain_head_sequence=account.queue_chain_head_sequence,
                    queue_chain_head_payload_fingerprint=(
                        account.queue_chain_head_payload_fingerprint
                    ),
                    account_revision=account.account_revision,
                    checkpoint_generation=checkpoint.checkpoint_generation,
                    checkpoint_through_sequence=checkpoint.through_sequence,
                    checkpoint_fingerprint=checkpoint.checkpoint_fingerprint,
                    transition_count=account.transition_count,
                    transition_accumulator=account.transition_accumulator,
                    bounded_tail_first_sequence=0,
                    bounded_tail_count=0,
                    bounded_tail_payload_bytes=0,
                    bounded_tail_accumulator=context_fingerprint(
                        "prompt-queue-bounded-tail:v1", ()
                    ),
                    pending_item_count=account.pending_item_count,
                    reserved_item_count=account.reserved_item_count,
                    artifact_bytes=account.artifact_bytes,
                    pending_item_head_set_accumulator=(
                        account.pending_item_head_set_accumulator
                    ),
                    active_client_item_count=account.active_client_item_count,
                    active_client_item_accumulator=(
                        account.active_client_item_accumulator
                    ),
                    row_set_accumulator=account.row_set_accumulator,
                    reducer_contract_fingerprint=(account.reducer_contract_fingerprint),
                    event_registry_fingerprint=account.event_registry_fingerprint,
                )
                cursor.execute(
                    """
                    UPDATE runtime_projection_checkpoints
                    SET through_sequence = %s,
                        projection_schema_version = %s,
                        ledger_prefix = %s,
                        validation_base_through_sequence = %s,
                        validation_base_state_payload = %s,
                        state_payload = %s,
                        payload_fingerprint = %s,
                        updated_at = now()
                    WHERE session_id = %s AND projection_kind = %s
                      AND through_sequence = %s AND payload_fingerprint = %s
                    """,
                    (
                        candidate.through_sequence,
                        candidate.projection_schema_version,
                        Jsonb(asdict(candidate.ledger_prefix)),
                        candidate.validation_base_through_sequence,
                        Jsonb(candidate.validation_base_state_payload),
                        Jsonb(candidate.state_payload),
                        candidate.payload_fingerprint,
                        event_log.runtime_session_id,
                        PROMPT_QUEUE_PROJECTION_KIND,
                        guard.expected_previous_through_sequence,
                        guard.expected_previous_payload_fingerprint,
                    ),
                )
                if cursor.rowcount != 1:
                    raise ValueError("prompt queue checkpoint predecessor CAS failed")
                cursor.execute(
                    """
                    UPDATE prompt_queue_accounts
                    SET checkpoint_generation = %s,
                        checkpoint_through_sequence = %s,
                        checkpoint_fingerprint = %s,
                        bounded_tail_first_sequence = 0,
                        bounded_tail_count = 0,
                        bounded_tail_payload_bytes = 0,
                        bounded_tail_accumulator = %s,
                        account_fingerprint = %s,
                        updated_at = now()
                    WHERE session_id = %s AND account_revision = %s
                      AND checkpoint_generation = %s
                      AND checkpoint_fingerprint = %s
                    """,
                    (
                        resulting_account.checkpoint_generation,
                        resulting_account.checkpoint_through_sequence,
                        resulting_account.checkpoint_fingerprint,
                        resulting_account.bounded_tail_accumulator,
                        resulting_account.account_fingerprint,
                        event_log.runtime_session_id,
                        guard.expected_account_revision,
                        checkpoint.checkpoint_generation - 1,
                        PromptQueueDomainCheckpointFact.model_validate(
                            predecessor.state_payload["checkpoint"]
                        ).checkpoint_fingerprint,
                    ),
                )
                if cursor.rowcount != 1:
                    raise ValueError("prompt queue account checkpoint CAS failed")
        receipt = _head_receipt(checkpoint=checkpoint, account=resulting_account)
        return _outcome(
            disposition="full",
            candidate_fingerprint=checkpoint.checkpoint_fingerprint,
            installed_checkpoint=checkpoint,
            head_receipt=receipt,
        )
    except BaseException:
        return _confirm_after_failure(
            event_log,
            candidate=candidate,
            checkpoint=checkpoint,
            guard=guard,
            deadline_monotonic=deadline,
        )


def _confirm_after_failure(
    event_log: PostgresEventLog,
    *,
    candidate: RawRuntimeProjectionCheckpoint,
    checkpoint: PromptQueueDomainCheckpointFact,
    guard: PromptQueueCheckpointCommitGuard,
    deadline_monotonic: float,
) -> PromptQueueCheckpointCommitOutcome:
    if monotonic() >= deadline_monotonic:
        return _outcome(
            disposition="reconciliation_required",
            candidate_fingerprint=checkpoint.checkpoint_fingerprint,
            installed_checkpoint=None,
            head_receipt=None,
        )
    try:
        bundle = read_prompt_queue_restore_bundle(
            event_log,
            max_delta_events=256,
            max_delta_payload_bytes=8 * 1024 * 1024,
            deadline_monotonic=deadline_monotonic,
        )
    except BaseException:
        return _outcome(
            disposition="reconciliation_required",
            candidate_fingerprint=checkpoint.checkpoint_fingerprint,
            installed_checkpoint=None,
            head_receipt=None,
        )
    if bundle.raw_checkpoint == candidate and bundle.checkpoint == checkpoint:
        return _outcome(
            disposition="full",
            candidate_fingerprint=checkpoint.checkpoint_fingerprint,
            installed_checkpoint=checkpoint,
            head_receipt=_head_receipt(
                checkpoint=bundle.checkpoint, account=bundle.account
            ),
        )
    if (
        bundle.raw_checkpoint.through_sequence
        == guard.expected_previous_through_sequence
        and bundle.raw_checkpoint.payload_fingerprint
        == guard.expected_previous_payload_fingerprint
        and bundle.account.account_revision == guard.expected_account_revision
    ):
        return _outcome(
            disposition="none",
            candidate_fingerprint=checkpoint.checkpoint_fingerprint,
            installed_checkpoint=None,
            head_receipt=None,
        )
    if (
        bundle.checkpoint.checkpoint_generation > checkpoint.checkpoint_generation
        and bundle.checkpoint.through_sequence >= checkpoint.through_sequence
        and bundle.checkpoint.transition_count == checkpoint.transition_count
        and bundle.checkpoint.transition_accumulator
        == checkpoint.transition_accumulator
        and bundle.checkpoint.queue_row_set_accumulator
        == checkpoint.queue_row_set_accumulator
        and bundle.checkpoint.active_client_item_count
        == checkpoint.active_client_item_count
        and bundle.checkpoint.active_client_item_accumulator
        == checkpoint.active_client_item_accumulator
    ):
        return _outcome(
            disposition="superseded_by_compatible_winner",
            candidate_fingerprint=checkpoint.checkpoint_fingerprint,
            installed_checkpoint=bundle.checkpoint,
            head_receipt=_head_receipt(
                checkpoint=bundle.checkpoint, account=bundle.account
            ),
        )
    return _outcome(
        disposition="reconciliation_required",
        candidate_fingerprint=checkpoint.checkpoint_fingerprint,
        installed_checkpoint=None,
        head_receipt=None,
    )


def _validate_candidate(
    event_log: PostgresEventLog,
    *,
    candidate: RawRuntimeProjectionCheckpoint,
    checkpoint: PromptQueueDomainCheckpointFact,
    guard: PromptQueueCheckpointCommitGuard,
) -> None:
    if (
        guard.runtime_session_id != event_log.runtime_session_id
        or checkpoint.runtime_session_id != event_log.runtime_session_id
        or candidate.projection_kind != PROMPT_QUEUE_PROJECTION_KIND
        or candidate.projection_schema_version != PROMPT_QUEUE_PROJECTION_SCHEMA_VERSION
        or candidate.through_sequence != checkpoint.through_sequence
        or candidate.state_payload.get("checkpoint")
        != checkpoint.model_dump(mode="json")
        or set(candidate.state_payload)
        != {
            "checkpoint",
            "items",
            "head_event_type",
        }
        or candidate.validation_base_through_sequence
        != guard.expected_previous_through_sequence
        or checkpoint.active_client_item_count
        != guard.expected_active_client_item_count
        or checkpoint.active_client_item_accumulator
        != guard.expected_active_client_item_accumulator
    ):
        raise ValueError("prompt queue checkpoint candidate/guard mismatch")


def _validate_guard(
    *,
    predecessor: RawRuntimeProjectionCheckpoint,
    account,
    guard: PromptQueueCheckpointCommitGuard,
) -> None:
    if (
        predecessor.through_sequence != guard.expected_previous_through_sequence
        or predecessor.payload_fingerprint
        != guard.expected_previous_payload_fingerprint
        or account.account_revision != guard.expected_account_revision
        or account.queue_chain_head_event_id != guard.expected_queue_head_event_id
        or account.queue_chain_head_payload_fingerprint
        != guard.expected_queue_head_payload_fingerprint
        or account.row_set_accumulator != guard.expected_row_set_accumulator
        or account.pending_item_head_set_accumulator
        != guard.expected_pending_item_head_set_accumulator
        or account.active_client_item_count != guard.expected_active_client_item_count
        or account.active_client_item_accumulator
        != guard.expected_active_client_item_accumulator
    ):
        raise ValueError("prompt queue checkpoint guard no longer matches")


def _raw_checkpoint_from_row(row) -> RawRuntimeProjectionCheckpoint:
    return RawRuntimeProjectionCheckpoint(
        projection_kind=str(row["projection_kind"]),
        through_sequence=int(row["through_sequence"]),
        projection_schema_version=str(row["projection_schema_version"]),
        ledger_prefix=RawTranscriptDomainPrefixFact(**dict(row["ledger_prefix"])),
        validation_base_through_sequence=int(row["validation_base_through_sequence"]),
        validation_base_state_payload=dict(row["validation_base_state_payload"]),
        state_payload=dict(row["state_payload"]),
        payload_fingerprint=str(row["payload_fingerprint"]),
    )


def _account_column_names() -> tuple[str, ...]:
    return tuple(part.strip() for part in _ACCOUNT_COLUMNS.split(",") if part.strip())


def _head_receipt(*, checkpoint, account) -> PromptQueueHeadReceiptFact:
    return build_prompt_queue_head_receipt(
        checkpoint=checkpoint,
        bounded_tail_first_sequence=account.bounded_tail_first_sequence,
        bounded_tail_last_sequence=(
            account.queue_chain_head_sequence if account.bounded_tail_count else 0
        ),
        bounded_tail_count=account.bounded_tail_count,
        bounded_tail_accumulator=account.bounded_tail_accumulator,
        resulting_queue_head_event_id=account.queue_chain_head_event_id,
        resulting_queue_head_payload_fingerprint=(
            account.queue_chain_head_payload_fingerprint
        ),
        resulting_account_revision=account.account_revision,
        resulting_active_client_item_count=account.active_client_item_count,
        resulting_active_client_item_accumulator=(
            account.active_client_item_accumulator
        ),
        resulting_row_set_accumulator=account.row_set_accumulator,
    )


def _outcome(
    *, disposition, candidate_fingerprint, installed_checkpoint, head_receipt
) -> PromptQueueCheckpointCommitOutcome:
    payload = {
        "disposition": disposition,
        "candidate_fingerprint": candidate_fingerprint,
        "installed_checkpoint_fingerprint": (
            installed_checkpoint.checkpoint_fingerprint
            if installed_checkpoint is not None
            else None
        ),
        "head_receipt_fingerprint": (
            head_receipt.receipt_fingerprint if head_receipt is not None else None
        ),
    }
    return PromptQueueCheckpointCommitOutcome(
        disposition=disposition,
        candidate_fingerprint=candidate_fingerprint,
        installed_checkpoint=installed_checkpoint,
        head_receipt=head_receipt,
        outcome_fingerprint=context_fingerprint(
            "prompt-queue-checkpoint-commit-outcome:v1", payload
        ),
    )


__all__ = [
    "commit_prompt_queue_checkpoint",
    "read_prompt_queue_restore_bundle",
]
