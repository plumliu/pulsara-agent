"""Historical prompt-queue genesis owned exclusively by migration 0011."""

from __future__ import annotations

from dataclasses import asdict
from typing import Any

from psycopg import Connection
from psycopg.types.json import Jsonb

from pulsara_agent.primitives.context import context_fingerprint
from pulsara_agent.primitives.prompt_queue import (
    EMPTY_PROMPT_QUEUE_PENDING_HEAD_SET_ACCUMULATOR,
    EMPTY_PROMPT_QUEUE_ROW_SET_ACCUMULATOR,
    PROMPT_QUEUE_EVENT_REGISTRY_FINGERPRINT,
    PROMPT_QUEUE_EVENT_TYPE_VALUES,
    PROMPT_QUEUE_TRANSITION_ACCUMULATOR_REDUCER_FINGERPRINT,
    prompt_queue_transition_genesis_accumulator,
)
from pulsara_agent.primitives.stored_event import RawTranscriptDomainPrefixFact
from pulsara_agent.primitives.transcript_accumulators import (
    EMPTY_LEDGER_CONTINUITY_ACCUMULATOR,
    EMPTY_TRANSCRIPT_SEMANTIC_ACCUMULATOR,
)

_PROJECTION_KIND = "prompt_queue.v1"
_PROJECTION_SCHEMA_VERSION = "prompt_queue_domain_checkpoint.v1"
_REDUCER_ID = "pulsara.prompt_queue.reducer"
_REDUCER_VERSION = "1"
_EVENT_REGISTRY_ID = "pulsara.prompt_queue.event_registry"
_EVENT_REGISTRY_VERSION = "1"


def install_prompt_queue_v11_genesis_for_migration(
    connection: Connection[Any],
    *,
    runtime_session_id: str,
) -> None:
    """Install or exact-confirm the frozen v11 generation-zero bundle.

    Migration 0012 upgrades this historical carrier to the current v2
    checkpoint and account shape. Runtime code must use the current bootstrap
    owner instead of this migration-only seam.
    """

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
            "prompt queue v11 genesis cannot be installed after queue transitions"
        )

    checkpoint = _build_v11_checkpoint(runtime_session_id)
    prefix = RawTranscriptDomainPrefixFact(
        through_sequence=0,
        ledger_payload_bytes=0,
        semantic_event_count=0,
        semantic_accumulator=EMPTY_TRANSCRIPT_SEMANTIC_ACCUMULATOR,
        ledger_continuity_accumulator=EMPTY_LEDGER_CONTINUITY_ACCUMULATOR,
    )
    state_payload = {
        "checkpoint": checkpoint,
        "items": [],
        "head_event_type": None,
    }
    raw_payload = {
        "projection_kind": _PROJECTION_KIND,
        "through_sequence": 0,
        "projection_schema_version": _PROJECTION_SCHEMA_VERSION,
        "ledger_prefix": asdict(prefix),
        "validation_base_through_sequence": 0,
        "validation_base_state_payload": {},
        "state_payload": state_payload,
    }
    raw_fingerprint = context_fingerprint(
        "prompt-queue-runtime-checkpoint-row:v1", raw_payload
    )
    account = _build_v11_account(runtime_session_id, checkpoint=checkpoint)

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
            _PROJECTION_KIND,
            _PROJECTION_SCHEMA_VERSION,
            Jsonb(asdict(prefix)),
            Jsonb({}),
            raw_fingerprint,
            Jsonb(state_payload),
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
            %s, 1, NULL, 0, NULL, 0, 0, 0, %s, 0, %s,
            0, 0, 0, %s, 0, 0, 0, %s, %s, %s, %s, %s, now()
        )
        ON CONFLICT (session_id) DO NOTHING
        """,
        (
            runtime_session_id,
            checkpoint["checkpoint_fingerprint"],
            account["transition_accumulator"],
            account["bounded_tail_accumulator"],
            account["pending_item_head_set_accumulator"],
            account["row_set_accumulator"],
            account["reducer_contract_fingerprint"],
            account["event_registry_fingerprint"],
            account["account_fingerprint"],
        ),
    )
    _confirm_v11_bundle(
        connection,
        runtime_session_id=runtime_session_id,
        state_payload=state_payload,
        raw_fingerprint=raw_fingerprint,
        account=account,
    )


def _build_v11_checkpoint(runtime_session_id: str) -> dict[str, object]:
    payload: dict[str, object] = {
        "schema_version": "prompt_queue_domain_checkpoint.v1",
        "runtime_session_id": runtime_session_id,
        "reducer_id": _REDUCER_ID,
        "reducer_version": _REDUCER_VERSION,
        "reducer_contract_fingerprint": (
            PROMPT_QUEUE_TRANSITION_ACCUMULATOR_REDUCER_FINGERPRINT
        ),
        "event_registry_id": _EVENT_REGISTRY_ID,
        "event_registry_version": _EVENT_REGISTRY_VERSION,
        "event_registry_fingerprint": PROMPT_QUEUE_EVENT_REGISTRY_FINGERPRINT,
        "checkpoint_generation": 0,
        "through_sequence": 0,
        "transition_count": 0,
        "transition_accumulator": prompt_queue_transition_genesis_accumulator(
            runtime_session_id
        ),
        "account_revision": 0,
        "next_accepted_ordinal": 1,
        "pending_item_head_set_accumulator": (
            EMPTY_PROMPT_QUEUE_PENDING_HEAD_SET_ACCUMULATOR
        ),
        "queue_row_set_accumulator": EMPTY_PROMPT_QUEUE_ROW_SET_ACCUMULATOR,
        "resulting_queue_head_event_id": None,
        "resulting_queue_head_payload_fingerprint": None,
    }
    payload["checkpoint_fingerprint"] = context_fingerprint(
        "prompt-queue-domain-checkpoint:v1", payload
    )
    return payload


def _build_v11_account(
    runtime_session_id: str,
    *,
    checkpoint: dict[str, object],
) -> dict[str, object]:
    payload: dict[str, object] = {
        "schema_version": "prompt_queue_account_projection.v1",
        "runtime_session_id": runtime_session_id,
        "next_accepted_ordinal": 1,
        "queue_chain_head_event_id": None,
        "queue_chain_head_sequence": 0,
        "queue_chain_head_payload_fingerprint": None,
        "account_revision": 0,
        "checkpoint_generation": 0,
        "checkpoint_through_sequence": 0,
        "checkpoint_fingerprint": checkpoint["checkpoint_fingerprint"],
        "transition_count": 0,
        "transition_accumulator": checkpoint["transition_accumulator"],
        "bounded_tail_first_sequence": 0,
        "bounded_tail_count": 0,
        "bounded_tail_payload_bytes": 0,
        "bounded_tail_accumulator": context_fingerprint(
            "prompt-queue-bounded-tail:v1", ()
        ),
        "pending_item_count": 0,
        "reserved_item_count": 0,
        "artifact_bytes": 0,
        "pending_item_head_set_accumulator": (
            EMPTY_PROMPT_QUEUE_PENDING_HEAD_SET_ACCUMULATOR
        ),
        "row_set_accumulator": EMPTY_PROMPT_QUEUE_ROW_SET_ACCUMULATOR,
        "reducer_contract_fingerprint": (
            PROMPT_QUEUE_TRANSITION_ACCUMULATOR_REDUCER_FINGERPRINT
        ),
        "event_registry_fingerprint": PROMPT_QUEUE_EVENT_REGISTRY_FINGERPRINT,
    }
    payload["account_fingerprint"] = context_fingerprint(
        "prompt-queue-account-row:v1", payload
    )
    return payload


def _confirm_v11_bundle(
    connection: Connection[Any],
    *,
    runtime_session_id: str,
    state_payload: dict[str, object],
    raw_fingerprint: str,
    account: dict[str, object],
) -> None:
    checkpoint_row = connection.execute(
        """
        SELECT projection_schema_version, through_sequence,
               validation_base_through_sequence,
               validation_base_state_payload, state_payload,
               payload_fingerprint
        FROM public.runtime_projection_checkpoints
        WHERE session_id = %s AND projection_kind = %s
        """,
        (runtime_session_id, _PROJECTION_KIND),
    ).fetchone()
    if checkpoint_row is None or (
        str(checkpoint_row[0]) != _PROJECTION_SCHEMA_VERSION
        or int(checkpoint_row[1]) != 0
        or int(checkpoint_row[2]) != 0
        or dict(checkpoint_row[3]) != {}
        or dict(checkpoint_row[4]) != state_payload
        or str(checkpoint_row[5]) != raw_fingerprint
    ):
        raise ValueError("prompt queue v11 checkpoint conflicts with durable state")

    account_row = connection.execute(
        """
        SELECT next_accepted_ordinal, queue_chain_head_event_id,
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
    expected = (
        account["next_accepted_ordinal"],
        account["queue_chain_head_event_id"],
        account["queue_chain_head_sequence"],
        account["queue_chain_head_payload_fingerprint"],
        account["account_revision"],
        account["checkpoint_generation"],
        account["checkpoint_through_sequence"],
        account["checkpoint_fingerprint"],
        account["transition_count"],
        account["transition_accumulator"],
        account["bounded_tail_first_sequence"],
        account["bounded_tail_count"],
        account["bounded_tail_payload_bytes"],
        account["bounded_tail_accumulator"],
        account["pending_item_count"],
        account["reserved_item_count"],
        account["artifact_bytes"],
        account["pending_item_head_set_accumulator"],
        account["row_set_accumulator"],
        account["reducer_contract_fingerprint"],
        account["event_registry_fingerprint"],
        account["account_fingerprint"],
    )
    if account_row is None or tuple(account_row) != expected:
        raise ValueError("prompt queue v11 account conflicts with durable state")


__all__ = ["install_prompt_queue_v11_genesis_for_migration"]
