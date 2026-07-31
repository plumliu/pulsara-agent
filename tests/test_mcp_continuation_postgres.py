from __future__ import annotations

import pytest

from pulsara_agent.event_log import PostgresEventLog, freeze_event_write_candidate
from pulsara_agent.primitives.mcp_continuation import (
    McpContinuationCompanionKind,
    McpContinuationCarrierState,
    mcp_continuation_charge_contract_fingerprint,
)
from pulsara_agent.primitives.mcp_continuation_storage import (
    McpContinuationCarrierControlFact,
    build_mcp_continuation_storage_fact,
)
from pulsara_agent.primitives.mcp import McpBindingIdentityFact
from pulsara_agent.runtime.authority_materialization import (
    account_with_committed_usage,
    build_default_authority_materialization_contract_bundle,
    canonical_empty_account,
)
from pulsara_agent.runtime.mcp.continuation_store import (
    McpContinuationAuthorityConflict,
    McpContinuationMutationKind,
    PostgresMcpContinuationSecretStore,
    build_mcp_continuation_transaction_intent,
)
from tests.support.events import typed_non_transcript_event
from tests.support.mcp import prepare_test_mcp_input_required_suspension
from tests.support.postgres import (
    connect_postgres_test_database,
    verified_postgres_provider,
)


def test_mcp_continuation_companion_commits_and_rolls_back_atomically(
    migrated_postgres_database,
) -> None:
    runtime_session_id = "runtime:mcp-continuation-atomic"
    provider = verified_postgres_provider(migrated_postgres_database.runtime_dsn)
    event_log = PostgresEventLog(
        provider,
        runtime_session_id=runtime_session_id,
    )
    event_log.ensure_runtime_session_owner()
    repository = PostgresMcpContinuationSecretStore(provider)
    binding = McpBindingIdentityFact(
        server_id="docs",
        slot_id="slot:docs",
        snapshot_id="snapshot:docs",
        discovery_generation=1,
    )
    prepared = prepare_test_mcp_input_required_suspension(
        interaction_id="interaction:postgres-atomic",
        runtime_session_id=runtime_session_id,
        run_id="run:postgres-atomic",
        turn_id="turn:postgres-atomic",
        reply_id="reply:postgres-atomic",
        tool_call_id="call:postgres-atomic",
        tool_name="mcp__docs__lookup",
        server_id="docs",
        binding_identity=binding,
        pending_lease_reservation_id="lease:postgres-atomic",
        request_state="secret-state-canary",
    )
    contracts = build_default_authority_materialization_contract_bundle()
    first_event = typed_non_transcript_event(
        label="mcp-continuation-insert",
        event_id=prepared.suspension_event_id,
        run_id="run:postgres-atomic",
        turn_id="turn:postgres-atomic",
        reply_id="reply:postgres-atomic",
    )
    source_account = canonical_empty_account(
        runtime_session_id=runtime_session_id,
        charge_contract_fingerprint=contracts.charge_contract.contract_fingerprint,
    )
    first_account = account_with_committed_usage(
        source_account,
        events=(first_event,),
        charge_contract=contracts.charge_contract,
    )
    charge_contract_fingerprint = mcp_continuation_charge_contract_fingerprint(
        prepared.continuation.durable_fact.bounds
    )
    insert_intent = build_mcp_continuation_transaction_intent(
        companion_kind=McpContinuationCompanionKind.SUSPENSION_INSERT,
        mutation_kind=McpContinuationMutationKind.INSERT_AWAITING,
        runtime_session_id=runtime_session_id,
        interaction_id=prepared.interaction.interaction_id,
        round_ordinal=prepared.interaction.round_count,
        source_event_id=first_event.id,
        repository=repository,
        issuer_id="test:mcp-continuation-postgres",
        issuer_generation=1,
        charge_contract_fingerprint=charge_contract_fingerprint,
        resulting_record=prepared.continuation.stored_record,
    )
    insert_companion = insert_intent.bind_candidate_batch(
        (freeze_event_write_candidate(first_event),)
    )

    stored = event_log.extend_with_materialization_state(
        (first_event,),
        expected_account_state_fingerprint=None,
        resulting_account_state=first_account,
        physical_charge_contract=contracts.charge_contract,
        transaction_companion=insert_companion,
        expected_last_sequence=0,
    )

    assert stored[0].sequence == 1
    stored_record = repository.read(
        prepared.continuation.durable_fact.continuation_carrier_id
    )
    assert stored_record == prepared.continuation.stored_record
    assert event_log.read_materialization_account_state() == first_account
    with connect_postgres_test_database(
        migrated_postgres_database.admin_dsn
    ) as connection:
        ciphertext = connection.execute(
            """
            select ciphertext_bytes
            from mcp_continuation_secret_carriers
            where continuation_carrier_id = %s
            """,
            (prepared.continuation.durable_fact.continuation_carrier_id,),
        ).fetchone()[0]
    assert b"secret-state-canary" not in bytes(ciphertext)

    second_event = typed_non_transcript_event(
        label="mcp-continuation-conflicting-delete",
        event_id="event:mcp-continuation-conflicting-delete",
        run_id="run:postgres-atomic",
        turn_id="turn:postgres-atomic",
        reply_id="reply:postgres-atomic",
    )
    second_account = account_with_committed_usage(
        first_account,
        events=(second_event,),
        charge_contract=contracts.charge_contract,
    )
    wrong_control = build_mcp_continuation_storage_fact(
        McpContinuationCarrierControlFact,
        schema_version="mcp_continuation_carrier_control.v1",
        continuation_carrier_id=stored_record.envelope.continuation_carrier_id,
        carrier_state=McpContinuationCarrierState.AWAITING_CLIENT_INPUT,
        control_revision=stored_record.control.control_revision + 1,
        source_event_id="event:wrong-control",
        stored_envelope_fingerprint=(
            stored_record.envelope.stored_envelope_fingerprint
        ),
    )
    delete_intent = build_mcp_continuation_transaction_intent(
        companion_kind=McpContinuationCompanionKind.TERMINAL_DELETE,
        mutation_kind=McpContinuationMutationKind.DELETE_TERMINAL,
        runtime_session_id=runtime_session_id,
        interaction_id=prepared.interaction.interaction_id,
        round_ordinal=prepared.interaction.round_count,
        source_event_id=second_event.id,
        repository=repository,
        issuer_id="test:mcp-continuation-postgres",
        issuer_generation=2,
        charge_contract_fingerprint=charge_contract_fingerprint,
        source_carrier_id=stored_record.envelope.continuation_carrier_id,
        expected_control=wrong_control,
    )
    delete_companion = delete_intent.bind_candidate_batch(
        (freeze_event_write_candidate(second_event),)
    )

    with pytest.raises(McpContinuationAuthorityConflict, match="control CAS"):
        event_log.extend_with_materialization_state(
            (second_event,),
            expected_account_state_fingerprint=(
                first_account.account_state_fingerprint
            ),
            resulting_account_state=second_account,
            physical_charge_contract=contracts.charge_contract,
            transaction_companion=delete_companion,
            expected_last_sequence=1,
        )

    assert event_log.get_by_id(second_event.id) is None
    assert event_log.read_materialization_account_state() == first_account
    assert repository.read(stored_record.envelope.continuation_carrier_id) == (
        stored_record
    )
