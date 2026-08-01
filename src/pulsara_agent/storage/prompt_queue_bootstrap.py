"""Canonical PostgreSQL prompt-queue genesis installation and verification."""

from __future__ import annotations

from dataclasses import asdict
from typing import Any

from psycopg import Connection
from psycopg.types.json import Jsonb

from pulsara_agent.primitives.stored_event import (
    RawRuntimeProjectionCheckpoint,
    RawTranscriptDomainPrefixFact,
)
from pulsara_agent.primitives.context import context_fingerprint
from pulsara_agent.primitives.prompt_queue import (
    PROMPT_QUEUE_EVENT_TYPE_VALUES,
    PromptQueueAccountProjectionFact,
    PromptQueueDomainCheckpointFact,
    build_prompt_queue_genesis_account,
    build_prompt_queue_genesis_checkpoint,
)
from pulsara_agent.primitives.transcript_accumulators import (
    EMPTY_LEDGER_CONTINUITY_ACCUMULATOR,
    EMPTY_TRANSCRIPT_SEMANTIC_ACCUMULATOR,
)

PROMPT_QUEUE_PROJECTION_KIND = "prompt_queue.v1"
PROMPT_QUEUE_PROJECTION_SCHEMA_VERSION = "prompt_queue_domain_checkpoint.v1"


def build_prompt_queue_genesis_raw_checkpoint(
    runtime_session_id: str,
) -> RawRuntimeProjectionCheckpoint:
    checkpoint = build_prompt_queue_genesis_checkpoint(runtime_session_id)
    prefix = RawTranscriptDomainPrefixFact(
        through_sequence=0,
        ledger_payload_bytes=0,
        semantic_event_count=0,
        semantic_accumulator=EMPTY_TRANSCRIPT_SEMANTIC_ACCUMULATOR,
        ledger_continuity_accumulator=EMPTY_LEDGER_CONTINUITY_ACCUMULATOR,
    )
    state_payload = {
        "checkpoint": checkpoint.model_dump(mode="json"),
        "items": [],
        "head_event_type": None,
    }
    payload = {
        "projection_kind": PROMPT_QUEUE_PROJECTION_KIND,
        "through_sequence": 0,
        "projection_schema_version": PROMPT_QUEUE_PROJECTION_SCHEMA_VERSION,
        "ledger_prefix": asdict(prefix),
        "validation_base_through_sequence": 0,
        "validation_base_state_payload": {},
        "state_payload": state_payload,
    }
    return RawRuntimeProjectionCheckpoint(
        projection_kind=PROMPT_QUEUE_PROJECTION_KIND,
        through_sequence=0,
        projection_schema_version=PROMPT_QUEUE_PROJECTION_SCHEMA_VERSION,
        ledger_prefix=prefix,
        validation_base_through_sequence=0,
        validation_base_state_payload={},
        state_payload=state_payload,
        payload_fingerprint=context_fingerprint(
            "prompt-queue-runtime-checkpoint-row:v1", payload
        ),
    )


def install_prompt_queue_genesis(
    connection: Connection[Any],
    *,
    runtime_session_id: str,
    require_empty_queue_chain: bool = True,
) -> tuple[PromptQueueAccountProjectionFact, PromptQueueDomainCheckpointFact]:
    """Install or exact-confirm the complete generation-zero queue bundle.

    The caller owns the surrounding transaction and runtime-write admission
    guard.  This function never repairs a non-genesis row in place.
    """

    observed_account, observed_checkpoint = read_prompt_queue_genesis(
        connection, runtime_session_id=runtime_session_id
    )
    if (observed_account is None) != (observed_checkpoint is None):
        raise ValueError("prompt queue bootstrap bundle is partial")
    if observed_account is not None and observed_checkpoint is not None:
        if (
            observed_account.runtime_session_id != runtime_session_id
            or observed_checkpoint.runtime_session_id != runtime_session_id
            or observed_account.checkpoint_generation
            != observed_checkpoint.checkpoint_generation
            or observed_account.checkpoint_through_sequence
            != observed_checkpoint.through_sequence
            or observed_account.checkpoint_fingerprint
            != observed_checkpoint.checkpoint_fingerprint
        ):
            raise ValueError("prompt queue account/checkpoint join mismatch")
        return observed_account, observed_checkpoint
    if require_empty_queue_chain:
        existing = connection.execute(
            """
            SELECT count(*)
            FROM public.agent_events
            WHERE session_id = %s AND event_type = ANY(%s)
            """,
            (runtime_session_id, list(PROMPT_QUEUE_EVENT_TYPE_VALUES)),
        ).fetchone()
        if existing is None or int(existing[0]) != 0:
            raise ValueError(
                "prompt queue genesis cannot be installed after queue transitions"
            )
    account = build_prompt_queue_genesis_account(runtime_session_id)
    raw = build_prompt_queue_genesis_raw_checkpoint(runtime_session_id)
    checkpoint = PromptQueueDomainCheckpointFact.model_validate(
        raw.state_payload["checkpoint"]
    )
    connection.execute(
        """
        INSERT INTO public.runtime_projection_checkpoints (
            session_id, projection_kind, through_sequence,
            projection_schema_version, ledger_prefix,
            validation_base_through_sequence,
            validation_base_state_payload, payload_fingerprint,
            state_payload, updated_at
        ) VALUES (%s, %s, 0, %s, %s, 0, %s, %s, %s, now())
        ON CONFLICT (session_id, projection_kind) DO NOTHING
        """,
        (
            runtime_session_id,
            raw.projection_kind,
            raw.projection_schema_version,
            Jsonb(asdict(raw.ledger_prefix)),
            Jsonb({}),
            raw.payload_fingerprint,
            Jsonb(raw.state_payload),
        ),
    )
    connection.execute(
        """
        INSERT INTO public.prompt_queue_accounts (
            session_id, next_accepted_ordinal, queue_chain_head_event_id,
            queue_chain_head_sequence, queue_chain_head_payload_fingerprint,
            account_revision, checkpoint_generation,
            checkpoint_through_sequence, checkpoint_fingerprint,
            transition_count, transition_accumulator,
            bounded_tail_first_sequence, bounded_tail_count,
            bounded_tail_payload_bytes, bounded_tail_accumulator,
            pending_item_count, reserved_item_count, artifact_bytes,
            pending_item_head_set_accumulator, row_set_accumulator,
            reducer_contract_fingerprint, event_registry_fingerprint,
            account_fingerprint, updated_at
        ) VALUES (
            %s, %s, NULL, 0, NULL, 0, 0, 0, %s, 0, %s,
            0, 0, 0, %s, 0, 0, 0, %s, %s, %s, %s, %s, now()
        )
        ON CONFLICT (session_id) DO NOTHING
        """,
        (
            runtime_session_id,
            account.next_accepted_ordinal,
            account.checkpoint_fingerprint,
            account.transition_accumulator,
            account.bounded_tail_accumulator,
            account.pending_item_head_set_accumulator,
            account.row_set_accumulator,
            account.reducer_contract_fingerprint,
            account.event_registry_fingerprint,
            account.account_fingerprint,
        ),
    )
    observed_account, observed_checkpoint = read_prompt_queue_genesis(
        connection, runtime_session_id=runtime_session_id
    )
    if observed_account != account or observed_checkpoint != checkpoint:
        raise ValueError("prompt queue genesis bundle conflicts with durable state")
    return account, checkpoint


def read_prompt_queue_genesis(
    connection: Connection[Any], *, runtime_session_id: str
) -> tuple[
    PromptQueueAccountProjectionFact | None,
    PromptQueueDomainCheckpointFact | None,
]:
    account_row = connection.execute(
        """
        SELECT session_id, next_accepted_ordinal, queue_chain_head_event_id,
               queue_chain_head_sequence, queue_chain_head_payload_fingerprint,
               account_revision, checkpoint_generation,
               checkpoint_through_sequence, checkpoint_fingerprint,
               transition_count, transition_accumulator,
               bounded_tail_first_sequence, bounded_tail_count,
               bounded_tail_payload_bytes, bounded_tail_accumulator,
               pending_item_count, reserved_item_count, artifact_bytes,
               pending_item_head_set_accumulator, row_set_accumulator,
               reducer_contract_fingerprint, event_registry_fingerprint,
               account_fingerprint
        FROM public.prompt_queue_accounts
        WHERE session_id = %s
        """,
        (runtime_session_id,),
    ).fetchone()
    checkpoint_row = connection.execute(
        """
        SELECT state_payload
        FROM public.runtime_projection_checkpoints
        WHERE session_id = %s AND projection_kind = %s
        """,
        (runtime_session_id, PROMPT_QUEUE_PROJECTION_KIND),
    ).fetchone()
    account = None
    if account_row is not None:
        account = prompt_queue_account_from_values(tuple(account_row))
    checkpoint = None
    if checkpoint_row is not None:
        state_payload = dict(checkpoint_row[0])
        checkpoint = PromptQueueDomainCheckpointFact.model_validate(
            state_payload["checkpoint"]
        )
    return account, checkpoint


def prompt_queue_account_from_values(
    values: tuple[object, ...],
) -> PromptQueueAccountProjectionFact:
    if len(values) != 23:
        raise ValueError("prompt queue account row shape mismatch")
    return PromptQueueAccountProjectionFact.model_validate(
        {
            "schema_version": "prompt_queue_account_projection.v1",
            "runtime_session_id": str(values[0]),
            "next_accepted_ordinal": int(values[1]),
            "queue_chain_head_event_id": values[2],
            "queue_chain_head_sequence": int(values[3]),
            "queue_chain_head_payload_fingerprint": values[4],
            "account_revision": int(values[5]),
            "checkpoint_generation": int(values[6]),
            "checkpoint_through_sequence": int(values[7]),
            "checkpoint_fingerprint": str(values[8]),
            "transition_count": int(values[9]),
            "transition_accumulator": str(values[10]),
            "bounded_tail_first_sequence": int(values[11]),
            "bounded_tail_count": int(values[12]),
            "bounded_tail_payload_bytes": int(values[13]),
            "bounded_tail_accumulator": str(values[14]),
            "pending_item_count": int(values[15]),
            "reserved_item_count": int(values[16]),
            "artifact_bytes": int(values[17]),
            "pending_item_head_set_accumulator": str(values[18]),
            "row_set_accumulator": str(values[19]),
            "reducer_contract_fingerprint": str(values[20]),
            "event_registry_fingerprint": str(values[21]),
            "account_fingerprint": str(values[22]),
        }
    )


__all__ = [
    "PROMPT_QUEUE_PROJECTION_KIND",
    "PROMPT_QUEUE_PROJECTION_SCHEMA_VERSION",
    "build_prompt_queue_genesis_raw_checkpoint",
    "install_prompt_queue_genesis",
    "prompt_queue_account_from_values",
    "read_prompt_queue_genesis",
]
