from __future__ import annotations

import asyncio
import json
from time import monotonic
from typing import cast

import pytest

from pulsara_agent.event import (
    EventContext,
    ReplyEndEvent,
    ReplyStartEvent,
    ToolResultEndEvent,
    ToolResultStartEvent,
    ToolResultTextDeltaEvent,
)
from pulsara_agent.event_log.postgres import PostgresEventLog
from pulsara_agent.entities.memory import Preference
from pulsara_agent.graph import PostgresGraphStore
from pulsara_agent.inspector.store import PostgresInspectorStore
from pulsara_agent.jsonld import utc_now
from pulsara_agent.memory.artifacts.postgres_archive import (
    PostgresArtifactStore,
)
from pulsara_agent.memory.foundation.run_timeline_query import (
    RunTimelineExportLimitExceeded,
    load_run_timeline,
    load_run_timeline_page,
)
from pulsara_agent.message import ToolResultState
from pulsara_agent.ontology import memory
from pulsara_agent.ontology import runtime as runtime_ontology
from pulsara_agent.projection_jobs.contracts import (
    CanonicalMemoryMutationOperationKind,
    CanonicalMutationSurface,
    DurableProjectionDeliveryPolicyFact,
    DurableProjectionCommitConfirmation,
    DurableProjectionFailureKind,
    DurableProjectionKind,
    DurableProjectionKindActivationFact,
    DurableProjectionRepairReason,
    DurableProjectionRetryPolicyFact,
    DurableProjectionResultSemanticFact,
    DurableProjectionSessionCutoverFact,
    DurableProjectionJobStatus,
    PreparedDurableProjectionResultFact,
    ProjectionJobResultOwnerFact,
    build_projection_fact,
    projection_target_key,
)
from pulsara_agent.runtime.projection_jobs.mutation_writer import (
    CanonicalMutationV2Writer,
    build_surface_plan,
)
from pulsara_agent.runtime.projection_jobs.inspection import (
    inspect_durable_projection_state,
)
from pulsara_agent.runtime.projection_jobs.postgres_repository import (
    DurableProjectionSeedBlockedError,
    PostgresDurableProjectionRepository,
)
from pulsara_agent.runtime.projection_jobs.projection_handlers import (
    PostgresRunTimelineProjectionHandler,
    PostgresToolResultEvidenceProjectionHandler,
)
from pulsara_agent.runtime.projection_jobs.registry import (
    DURABLE_PROJECTION_TRIGGER_REGISTRY,
    default_projection_delivery_policy,
)
from pulsara_agent.runtime.projection_jobs.seeder import (
    build_seed_commit_candidate,
    build_seed_failure_commit_candidate,
    canonical_seed_state,
)
from pulsara_agent.runtime.projection_jobs.source import (
    build_job_candidate,
    exact_stored_event,
)
from pulsara_agent.runtime.projection_jobs.surface import (
    CanonicalMutationSurfaceWorker,
    PostgresCanonicalMutationSurfaceRepository,
)
from pulsara_agent.runtime.projection_jobs.surface_handlers import (
    PostgresSearchIndexSurfaceHandler,
)
from pulsara_agent.storage.postgres_connection_provider import (
    PostgresConnectionLane,
)
from pulsara_agent.storage.session_bootstrap import (
    PostgresRuntimeSessionOwnerBootstrapPort,
)
from tests.support.postgres import verified_postgres_provider
from tests.support.postgres import connect_postgres_test_database
from tests.support.postgres_database import MigratedPostgresTestDatabase
from tests.support.runtime_session import in_memory_runtime_session
from tests.conftest import tool_result_end_candidate
from tests.support.model_stream import (
    ToolCallArgumentsSegmentEvent,
    ToolCallEndEvent,
    ToolCallStartEvent,
    make_text_block_segment_event,
)


def _read_production_authority(
    *,
    event_log: PostgresEventLog,
    kind: DurableProjectionKind,
    admin_dsn: str,
) -> tuple[DurableProjectionKindActivationFact, DurableProjectionSessionCutoverFact]:
    with connect_postgres_test_database(admin_dsn) as connection:
        stored_activation = connection.execute(
            """
            SELECT activation_payload, activation_fingerprint
            FROM durable_projection_kind_activations
            WHERE projection_kind = %s
            """,
            (kind.value,),
        ).fetchone()
        assert stored_activation is not None
        activation = DurableProjectionKindActivationFact.model_validate(
            stored_activation[0]
        )
        assert stored_activation[1] == activation.activation_fingerprint
        stored_cutover = connection.execute(
            """
            SELECT cutover_payload, cutover_fingerprint
            FROM durable_projection_session_cutovers
            WHERE runtime_session_id = %s AND projection_kind = %s
            """,
            (event_log.runtime_session_id, kind.value),
        ).fetchone()
        assert stored_cutover is not None
        cutover = DurableProjectionSessionCutoverFact.model_validate(stored_cutover[0])
        assert stored_cutover[1] == cutover.cutover_fingerprint
    return activation, cutover


def _prepared_result(job) -> PreparedDurableProjectionResultFact:
    owner = cast(
        ProjectionJobResultOwnerFact,
        build_projection_fact(
            ProjectionJobResultOwnerFact,
            schema_version="projection_job_result_owner.v1",
            owner_kind="durable_projection_job",
            job_id=job.job_semantic.job_id,
            job_semantic_fingerprint=(job.job_semantic.job_semantic_fingerprint),
            job_candidate_fingerprint=job.candidate_fingerprint,
            source_event_reference_fingerprint=(
                job.job_semantic.source_event_reference.reference_fingerprint
            ),
        ),
    )
    semantic = cast(
        DurableProjectionResultSemanticFact,
        build_projection_fact(
            DurableProjectionResultSemanticFact,
            schema_version="durable_projection_result_semantic.v1",
            projection_kind=job.job_semantic.projection_kind,
            source_projection_fingerprint=(job.job_semantic.job_semantic_fingerprint),
            ordered_document_semantic_fingerprints=(),
            ordered_canonical_mutation_semantic_fingerprints=(),
        ),
    )
    return cast(
        PreparedDurableProjectionResultFact,
        build_projection_fact(
            PreparedDurableProjectionResultFact,
            schema_version="prepared_durable_projection_result.v1",
            result_owner=owner,
            result_semantic=semantic,
            ordered_documents=(),
            canonical_mutation_candidates=(),
        ),
    )


def test_postgres_seed_commit_is_atomic_and_exact_confirmed(
    migrated_postgres_database: MigratedPostgresTestDatabase,
) -> None:
    provider = verified_postgres_provider(migrated_postgres_database.runtime_dsn)
    event_log = PostgresEventLog(
        connection_provider=provider,
        runtime_session_id="runtime:projection-seed",
        workspace_root="/tmp/projection-seed",
    )
    event_log.ensure_runtime_session_owner()
    activation, cutover = _read_production_authority(
        event_log=event_log,
        kind=DurableProjectionKind.RUN_TIMELINE,
        admin_dsn=migrated_postgres_database.admin_dsn,
    )
    stored = event_log.append(
        ReplyEndEvent(
            **EventContext(
                "run:projection-seed",
                "turn:projection-seed",
                "reply:projection-seed",
            ).event_fields(),
            model_terminal_outcome="completed",
        )
    )
    source = exact_stored_event(event_log=event_log, event_id=stored.id)
    job = build_job_candidate(
        stored=source,
        projection_kind=DurableProjectionKind.RUN_TIMELINE,
        activation_fingerprint=activation.activation_fingerprint,
        trigger_registry=DURABLE_PROJECTION_TRIGGER_REGISTRY,
    )
    initial = canonical_seed_state(cutover)
    candidate = build_seed_commit_candidate(
        expected_state=initial,
        scan_horizon=source.trigger_horizon,
        ordered_job_candidates=(job,),
        source_event_count=1,
        source_payload_bytes=(
            source.trigger_horizon.ledger_payload_prefix_bytes
            - initial.ledger_payload_prefix_bytes
        ),
    )
    repository = PostgresDurableProjectionRepository(provider)
    first = repository.commit(
        candidate=candidate,
        deadline_monotonic=monotonic() + 20.0,
    )
    assert first.confirmation is DurableProjectionCommitConfirmation.FULL, first.failure
    assert first.committed_job_ids == (job.job_semantic.job_id,)
    second = repository.commit(
        candidate=candidate,
        deadline_monotonic=monotonic() + 20.0,
    )
    assert second.confirmation is DurableProjectionCommitConfirmation.FULL
    assert repository.read_job(job.job_semantic.job_id) is not None
    leases = repository.claim_due(owner_id="worker:seed", limit=1)
    assert len(leases) == 1
    assert (
        repository.settle_success(
            lease=leases[0],
            prepared=_prepared_result(job),
        ).confirmation
        is DurableProjectionCommitConfirmation.FULL
    )


def test_session_bootstrap_rejects_same_kind_with_different_cutover_fact(
    migrated_postgres_database: MigratedPostgresTestDatabase,
) -> None:
    provider = verified_postgres_provider(migrated_postgres_database.runtime_dsn)
    port = PostgresRuntimeSessionOwnerBootstrapPort(provider)
    candidate = port.candidate(
        runtime_session_id="runtime:bootstrap-exact-cutover",
        workspace_root="/tmp/bootstrap-exact-cutover",
    )
    assert (
        port.bootstrap(
            candidate=candidate,
            deadline_monotonic=monotonic() + 20.0,
        ).confirmation
        is DurableProjectionCommitConfirmation.FULL
    )
    with connect_postgres_test_database(
        migrated_postgres_database.admin_dsn
    ) as connection:
        # Fault injection deliberately bypasses the production write fence.
        connection.execute("SET LOCAL session_replication_role = replica")
        row = connection.execute(
            """
            SELECT cutover_payload
            FROM durable_projection_session_cutovers
            WHERE runtime_session_id = %s
              AND projection_kind = %s
            FOR UPDATE
            """,
            (
                candidate.session_owner.runtime_session_id,
                DurableProjectionKind.RUN_TIMELINE.value,
            ),
        ).fetchone()
        assert row is not None
        original = DurableProjectionSessionCutoverFact.model_validate(row[0])
        drifted = cast(
            DurableProjectionSessionCutoverFact,
            build_projection_fact(
                DurableProjectionSessionCutoverFact,
                schema_version="durable_projection_session_cutover.v1",
                runtime_session_id=original.runtime_session_id,
                projection_kind=original.projection_kind,
                cutover_through_sequence=original.cutover_through_sequence,
                cutover_ledger_continuity_accumulator=(
                    original.cutover_ledger_continuity_accumulator
                ),
                cutover_ledger_payload_prefix_bytes=(
                    original.cutover_ledger_payload_prefix_bytes
                ),
                cutover_transcript_semantic_prefix_count=(
                    original.cutover_transcript_semantic_prefix_count
                ),
                cutover_transcript_semantic_prefix_accumulator=(
                    original.cutover_transcript_semantic_prefix_accumulator
                ),
                migration_version=original.migration_version + 1,
                migration_registry_prefix_fingerprint=(
                    original.migration_registry_prefix_fingerprint
                ),
                activation_fingerprint=original.activation_fingerprint,
                seed_contract_fingerprint=(original.seed_contract_fingerprint),
                cutover_policy_id=original.cutover_policy_id,
            ),
        )
        connection.execute(
            """
            UPDATE durable_projection_session_cutovers
            SET cutover_payload = %s::jsonb, cutover_fingerprint = %s
            WHERE runtime_session_id = %s AND projection_kind = %s
            """,
            (
                drifted.model_dump_json(),
                drifted.cutover_fingerprint,
                drifted.runtime_session_id,
                drifted.projection_kind.value,
            ),
        )
    assert (
        port.bootstrap(
            candidate=candidate,
            deadline_monotonic=monotonic() + 20.0,
        ).confirmation
        is DurableProjectionCommitConfirmation.CONFLICT
    )


def test_postgres_job_claim_and_settlement_are_exact(
    migrated_postgres_database: MigratedPostgresTestDatabase,
) -> None:
    provider = verified_postgres_provider(migrated_postgres_database.runtime_dsn)
    event_log = PostgresEventLog(
        connection_provider=provider,
        runtime_session_id="runtime:projection-settlement",
        workspace_root="/tmp/projection-settlement",
    )
    event_log.ensure_runtime_session_owner()
    activation, cutover = _read_production_authority(
        event_log=event_log,
        kind=DurableProjectionKind.RUN_TIMELINE,
        admin_dsn=migrated_postgres_database.admin_dsn,
    )
    stored = event_log.append(
        ReplyEndEvent(
            **EventContext(
                "run:projection-settlement",
                "turn:projection-settlement",
                "reply:projection-settlement",
            ).event_fields(),
            model_terminal_outcome="completed",
        )
    )
    source = exact_stored_event(event_log=event_log, event_id=stored.id)
    job = build_job_candidate(
        stored=source,
        projection_kind=DurableProjectionKind.RUN_TIMELINE,
        activation_fingerprint=activation.activation_fingerprint,
        trigger_registry=DURABLE_PROJECTION_TRIGGER_REGISTRY,
    )
    initial = canonical_seed_state(cutover)
    candidate = build_seed_commit_candidate(
        expected_state=initial,
        scan_horizon=source.trigger_horizon,
        ordered_job_candidates=(job,),
        source_event_count=1,
        source_payload_bytes=(
            source.trigger_horizon.ledger_payload_prefix_bytes
            - initial.ledger_payload_prefix_bytes
        ),
    )
    repository = PostgresDurableProjectionRepository(provider)
    assert (
        repository.commit(
            candidate=candidate,
            deadline_monotonic=monotonic() + 20.0,
        ).confirmation
        is DurableProjectionCommitConfirmation.FULL
    )
    leases = repository.claim_due(owner_id="worker:settlement", limit=2)
    assert len(leases) == 1
    assert repository.claim_due(owner_id="worker:other", limit=2) == ()
    outcome = repository.settle_success(
        lease=leases[0],
        prepared=_prepared_result(job),
    )
    assert outcome.confirmation is DurableProjectionCommitConfirmation.FULL
    assert outcome.resulting_status is DurableProjectionJobStatus.SUCCEEDED
    record = repository.read_job(job.job_semantic.job_id)
    assert record is not None
    assert record.state.status is DurableProjectionJobStatus.SUCCEEDED
    assert record.state.result_receipt_reference is not None
    receipt = repository.read_receipt(record.state.result_receipt_reference.receipt_id)
    assert (
        receipt.receipt_fingerprint
        == record.state.result_receipt_reference.receipt_fingerprint
    )
    snapshot = inspect_durable_projection_state(
        PostgresInspectorStore(provider),
        session_id=event_log.runtime_session_id,
        limit=16,
    )
    assert snapshot["status"] == "healthy"
    assert snapshot["jobs"][0]["job_id"] == job.job_semantic.job_id
    assert (
        snapshot["result_receipts"][0]["receipt_id"]
        == record.state.result_receipt_reference.receipt_id
    )
    assert snapshot["target_heads"][0]["target_key"] == (job.job_semantic.target_key)


def test_claim_due_does_not_starve_other_target_behind_hot_leased_target(
    migrated_postgres_database: MigratedPostgresTestDatabase,
) -> None:
    provider = verified_postgres_provider(migrated_postgres_database.runtime_dsn)
    event_log = PostgresEventLog(
        connection_provider=provider,
        runtime_session_id="runtime:projection-claim-fairness",
        workspace_root="/tmp/projection-claim-fairness",
    )
    event_log.ensure_runtime_session_owner()
    repository = PostgresDurableProjectionRepository(provider)
    for index in range(12):
        event_log.append(
            ReplyEndEvent(
                **EventContext(
                    "run:hot-target",
                    f"turn:hot:{index}",
                    f"reply:hot:{index}",
                ).event_fields(),
                model_terminal_outcome="completed",
            )
        )
    first_seed = repository.prepare_next_seed_candidate(
        runtime_session_id=event_log.runtime_session_id,
        projection_kind=DurableProjectionKind.RUN_TIMELINE,
    )
    assert first_seed is not None
    assert (
        repository.commit(
            candidate=first_seed,
            deadline_monotonic=monotonic() + 20.0,
        ).confirmation
        is DurableProjectionCommitConfirmation.FULL
    )
    hot_lease = repository.claim_due(
        owner_id="worker:hot-target",
        limit=1,
    )
    assert len(hot_lease) == 1
    assert hot_lease[0].job.source_event_reference.run_id == "run:hot-target"

    event_log.append(
        ReplyEndEvent(
            **EventContext(
                "run:independent-target",
                "turn:independent",
                "reply:independent",
            ).event_fields(),
            model_terminal_outcome="completed",
        )
    )
    second_seed = repository.prepare_next_seed_candidate(
        runtime_session_id=event_log.runtime_session_id,
        projection_kind=DurableProjectionKind.RUN_TIMELINE,
    )
    assert second_seed is not None
    assert (
        repository.commit(
            candidate=second_seed,
            deadline_monotonic=monotonic() + 20.0,
        ).confirmation
        is DurableProjectionCommitConfirmation.FULL
    )
    independent = repository.claim_due(
        owner_id="worker:independent-target",
        limit=1,
    )
    assert len(independent) == 1
    assert independent[0].job.source_event_reference.run_id == "run:independent-target"


def test_seed_page_uses_longest_nonempty_byte_bounded_prefix(
    migrated_postgres_database: MigratedPostgresTestDatabase,
) -> None:
    provider = verified_postgres_provider(migrated_postgres_database.runtime_dsn)
    event_log = PostgresEventLog(
        connection_provider=provider,
        runtime_session_id="runtime:projection-byte-pages",
        workspace_root="/tmp/projection-byte-pages",
    )
    event_log.ensure_runtime_session_owner()
    payload = "x" * (5 * 1024 * 1024)
    for index in range(2):
        event_log.append(
            ToolResultTextDeltaEvent(
                **EventContext(
                    "run:projection-byte-pages",
                    f"turn:projection-byte-pages:{index}",
                    f"reply:projection-byte-pages:{index}",
                ).event_fields(),
                tool_call_id=f"call:projection-byte-pages:{index}",
                delta=payload,
            )
        )
    repository = PostgresDurableProjectionRepository(provider)
    first = repository.prepare_next_seed_candidate(
        runtime_session_id=event_log.runtime_session_id,
        projection_kind=DurableProjectionKind.RUN_TIMELINE,
    )
    assert first is not None
    assert first.source_event_count == 1
    assert first.source_payload_bytes < 8 * 1024 * 1024
    assert (
        repository.commit(
            candidate=first,
            deadline_monotonic=monotonic() + 20.0,
        ).confirmation
        is DurableProjectionCommitConfirmation.FULL
    )
    second = repository.prepare_next_seed_candidate(
        runtime_session_id=event_log.runtime_session_id,
        projection_kind=DurableProjectionKind.RUN_TIMELINE,
    )
    assert second is not None
    assert second.source_event_count == 1
    assert second.expected_seed_state == first.resulting_seed_state


def test_seed_failure_latch_requires_typed_repair_and_atomic_resolution(
    migrated_postgres_database: MigratedPostgresTestDatabase,
) -> None:
    provider = verified_postgres_provider(migrated_postgres_database.runtime_dsn)
    event_log = PostgresEventLog(
        connection_provider=provider,
        runtime_session_id="runtime:projection-seed-repair",
        workspace_root="/tmp/projection-seed-repair",
    )
    event_log.ensure_runtime_session_owner()
    event_log.append(
        ToolResultTextDeltaEvent(
            **EventContext(
                "run:projection-seed-repair",
                "turn:projection-seed-repair",
                "reply:projection-seed-repair",
            ).event_fields(),
            tool_call_id="call:projection-seed-repair",
            delta="seed authority repair source",
        )
    )
    repository = PostgresDurableProjectionRepository(provider)
    activation, cutover = _read_production_authority(
        event_log=event_log,
        kind=DurableProjectionKind.RUN_TIMELINE,
        admin_dsn=migrated_postgres_database.admin_dsn,
    )
    stale = repository.prepare_next_seed_candidate(
        runtime_session_id=event_log.runtime_session_id,
        projection_kind=DurableProjectionKind.RUN_TIMELINE,
    )
    assert stale is not None
    initial = canonical_seed_state(cutover)
    failure_candidate = build_seed_failure_commit_candidate(
        cutover=cutover,
        activation_fingerprint=activation.activation_fingerprint,
        expected_state=initial,
        failure_kind="source_authority_conflict",
        error=ValueError("operator repair required"),
    )
    failure_outcome = repository.commit(
        candidate=failure_candidate,
        deadline_monotonic=monotonic() + 20.0,
    )
    assert failure_outcome.confirmation is DurableProjectionCommitConfirmation.FULL
    assert (
        repository.commit(
            candidate=stale,
            deadline_monotonic=monotonic() + 20.0,
        ).confirmation
        is DurableProjectionCommitConfirmation.CONFLICT
    )
    with pytest.raises(DurableProjectionSeedBlockedError):
        repository.prepare_next_seed_candidate(
            runtime_session_id=event_log.runtime_session_id,
            projection_kind=DurableProjectionKind.RUN_TIMELINE,
        )
    repair = repository.repair_seed_failure(
        failure_id=failure_candidate.failure.failure_id,
        action="retry_after_authority_repair",
        operator_authority_id="operator:seed-repair",
        deadline_monotonic=monotonic() + 20.0,
    )
    repaired = repository.prepare_next_seed_candidate(
        runtime_session_id=event_log.runtime_session_id,
        projection_kind=DurableProjectionKind.RUN_TIMELINE,
    )
    assert repaired is not None
    assert (
        repaired.repaired_seed_failure_fingerprint
        == failure_candidate.failure.failure_fingerprint
    )
    assert repaired.seed_repair_action_fingerprint == repair.action_fingerprint
    repaired_outcome = repository.commit(
        candidate=repaired,
        deadline_monotonic=monotonic() + 20.0,
    )
    assert repaired_outcome.confirmation is DurableProjectionCommitConfirmation.FULL
    assert repaired_outcome.committed_seed_failure_resolution_fingerprint is not None
    assert (
        repository.read_active_seed_failure(
            event_log.runtime_session_id,
            DurableProjectionKind.RUN_TIMELINE,
        )
        is None
    )


def test_full_replacement_claim_terminalizes_older_job_as_superseded(
    migrated_postgres_database: MigratedPostgresTestDatabase,
) -> None:
    provider = verified_postgres_provider(migrated_postgres_database.runtime_dsn)
    event_log = PostgresEventLog(
        connection_provider=provider,
        runtime_session_id="runtime:projection-claim-supersession",
        workspace_root="/tmp/projection-claim-supersession",
    )
    event_log.ensure_runtime_session_owner()
    activation, cutover = _read_production_authority(
        event_log=event_log,
        kind=DurableProjectionKind.RUN_TIMELINE,
        admin_dsn=migrated_postgres_database.admin_dsn,
    )
    context = EventContext(
        "run:projection-claim-supersession",
        "turn:projection-claim-supersession",
        "reply:projection-claim-supersession:1",
    )
    first_stored = event_log.append(
        ReplyEndEvent(
            **context.event_fields(),
            model_terminal_outcome="completed",
        )
    )
    second_stored = event_log.append(
        ReplyEndEvent(
            **EventContext(
                context.run_id,
                context.turn_id,
                "reply:projection-claim-supersession:2",
            ).event_fields(),
            model_terminal_outcome="completed",
        )
    )
    first_source = exact_stored_event(
        event_log=event_log,
        event_id=first_stored.id,
    )
    second_source = exact_stored_event(
        event_log=event_log,
        event_id=second_stored.id,
    )
    first_job = build_job_candidate(
        stored=first_source,
        projection_kind=DurableProjectionKind.RUN_TIMELINE,
        activation_fingerprint=activation.activation_fingerprint,
        trigger_registry=DURABLE_PROJECTION_TRIGGER_REGISTRY,
    )
    second_job = build_job_candidate(
        stored=second_source,
        projection_kind=DurableProjectionKind.RUN_TIMELINE,
        activation_fingerprint=activation.activation_fingerprint,
        trigger_registry=DURABLE_PROJECTION_TRIGGER_REGISTRY,
    )
    initial = canonical_seed_state(cutover)
    candidate = build_seed_commit_candidate(
        expected_state=initial,
        scan_horizon=second_source.trigger_horizon,
        ordered_job_candidates=(first_job, second_job),
        source_event_count=2,
        source_payload_bytes=(
            second_source.trigger_horizon.ledger_payload_prefix_bytes
            - initial.ledger_payload_prefix_bytes
        ),
    )
    repository = PostgresDurableProjectionRepository(provider)
    assert (
        repository.commit(
            candidate=candidate,
            deadline_monotonic=monotonic() + 20.0,
        ).confirmation
        is DurableProjectionCommitConfirmation.FULL
    )
    leases = repository.claim_due(owner_id="worker:newest", limit=4)
    assert len(leases) == 1
    assert leases[0].job.job_id == second_job.job_semantic.job_id
    assert (
        repository.settle_success(
            lease=leases[0],
            prepared=_prepared_result(second_job),
        ).confirmation
        is DurableProjectionCommitConfirmation.FULL
    )

    assert repository.claim_due(owner_id="worker:stale", limit=4) == ()
    stale = repository.read_job(first_job.job_semantic.job_id)
    assert stale is not None
    assert stale.state.status is DurableProjectionJobStatus.SUPERSEDED
    assert stale.state.result_receipt_reference is not None
    stale_receipt = repository.read_receipt(
        stale.state.result_receipt_reference.receipt_id
    )
    assert stale_receipt.receipt_kind == "superseded"
    assert (
        stale_receipt.effective_applied_result_receipt_reference.receipt_id
        == repository.read_job(
            second_job.job_semantic.job_id
        ).state.result_receipt_reference.receipt_id
    )


def test_dead_letter_repair_is_an_exact_typed_cas_action(
    migrated_postgres_database: MigratedPostgresTestDatabase,
) -> None:
    provider = verified_postgres_provider(migrated_postgres_database.runtime_dsn)
    event_log = PostgresEventLog(
        connection_provider=provider,
        runtime_session_id="runtime:projection-repair",
        workspace_root="/tmp/projection-repair",
    )
    event_log.ensure_runtime_session_owner()
    activation, cutover = _read_production_authority(
        event_log=event_log,
        kind=DurableProjectionKind.RUN_TIMELINE,
        admin_dsn=migrated_postgres_database.admin_dsn,
    )
    stored = event_log.append(
        ReplyEndEvent(
            **EventContext(
                "run:projection-repair",
                "turn:projection-repair",
                "reply:projection-repair",
            ).event_fields(),
            model_terminal_outcome="completed",
        )
    )
    source = exact_stored_event(event_log=event_log, event_id=stored.id)
    job = build_job_candidate(
        stored=source,
        projection_kind=DurableProjectionKind.RUN_TIMELINE,
        activation_fingerprint=activation.activation_fingerprint,
        trigger_registry=DURABLE_PROJECTION_TRIGGER_REGISTRY,
    )
    initial = canonical_seed_state(cutover)
    repository = PostgresDurableProjectionRepository(provider)
    assert (
        repository.commit(
            candidate=build_seed_commit_candidate(
                expected_state=initial,
                scan_horizon=source.trigger_horizon,
                ordered_job_candidates=(job,),
                source_event_count=1,
                source_payload_bytes=(
                    source.trigger_horizon.ledger_payload_prefix_bytes
                    - initial.ledger_payload_prefix_bytes
                ),
            ),
            deadline_monotonic=monotonic() + 20.0,
        ).confirmation
        is DurableProjectionCommitConfirmation.FULL
    )
    leases = repository.claim_due(owner_id="worker:repair", limit=1)
    assert len(leases) == 1
    repository.settle_failure(
        lease=leases[0],
        failure_kind=(DurableProjectionFailureKind.SOURCE_AUTHORITY_CONFLICT),
        error=ValueError("synthetic permanent projection failure"),
    )
    dead_letter = repository.read_job(job.job_semantic.job_id)
    assert dead_letter is not None
    assert dead_letter.state.status is DurableProjectionJobStatus.DEAD_LETTER

    action = repository.repair_dead_letter(
        job_id=job.job_semantic.job_id,
        reason=DurableProjectionRepairReason.SOURCE_AUTHORITY_REPAIRED,
        operator_authority_id="operator:projection-repair",
        deadline_monotonic=monotonic() + 20.0,
    )
    repeated = repository.repair_dead_letter(
        job_id=job.job_semantic.job_id,
        reason=DurableProjectionRepairReason.SOURCE_AUTHORITY_REPAIRED,
        operator_authority_id="operator:projection-repair",
        deadline_monotonic=monotonic() + 20.0,
    )
    assert repeated == action
    repaired = repository.read_job(job.job_semantic.job_id)
    assert repaired is not None
    assert repaired.state.status is DurableProjectionJobStatus.PENDING
    assert repaired.state.repair_generation == action.resulting_repair_generation
    with pytest.raises(ValueError, match="requires a dead-letter"):
        repository.repair_dead_letter(
            job_id=job.job_semantic.job_id,
            reason=DurableProjectionRepairReason.SOURCE_AUTHORITY_REPAIRED,
            operator_authority_id="operator:other",
            deadline_monotonic=monotonic() + 20.0,
        )

    snapshot = inspect_durable_projection_state(
        PostgresInspectorStore(provider),
        session_id=event_log.runtime_session_id,
        run_id=stored.run_id,
        limit=16,
    )
    assert snapshot["repair_actions"][0]["repair_action_id"] == (
        action.repair_action_id
    )
    assert snapshot["diagnostics"] == []


def test_surface_predecessor_uses_surface_sequence_not_global_mutation_gap(
    migrated_postgres_database: MigratedPostgresTestDatabase,
) -> None:
    provider = verified_postgres_provider(migrated_postgres_database.runtime_dsn)
    plan = build_surface_plan(
        (
            CanonicalMutationSurface.SEARCH_INDEX,
            CanonicalMutationSurface.VECTOR_INDEX,
        )
    )
    writer = CanonicalMutationV2Writer(
        surface_plan=plan,
        connection_provider=provider,
    )
    graph_id = "graph:surface-gap"
    first_search = writer.append_canonical_memory_write_mutation(
        payload={
            "mutation_lane": "runtime_semantic",
            "documents": [{"node_id": "node:one", "document": {"v": 1}}],
        },
        graph_id=graph_id,
        operation_id="surface-gap:node:one",
        operation_kind=(CanonicalMemoryMutationOperationKind.RUNTIME_SEMANTIC_DOCUMENT),
        requested_surfaces=(CanonicalMutationSurface.SEARCH_INDEX,),
    )
    writer.append_canonical_memory_write_mutation(
        payload={
            "mutation_lane": "runtime_semantic",
            "documents": [{"node_id": "node:two", "document": {"v": 2}}],
        },
        graph_id=graph_id,
        operation_id="surface-gap:node:two",
        operation_kind=(CanonicalMemoryMutationOperationKind.RUNTIME_SEMANTIC_DOCUMENT),
        requested_surfaces=(CanonicalMutationSurface.VECTOR_INDEX,),
    )
    second_search = writer.append_canonical_memory_write_mutation(
        payload={
            "mutation_lane": "runtime_semantic",
            "documents": [{"node_id": "node:three", "document": {"v": 3}}],
        },
        graph_id=graph_id,
        operation_id="surface-gap:node:three",
        operation_kind=(CanonicalMemoryMutationOperationKind.RUNTIME_SEMANTIC_DOCUMENT),
        requested_surfaces=(CanonicalMutationSurface.SEARCH_INDEX,),
    )
    repository = PostgresCanonicalMutationSurfaceRepository(provider)
    first = repository.claim_due(
        surface=CanonicalMutationSurface.SEARCH_INDEX,
        owner_id="surface-worker:first",
        limit=4,
        deadline_monotonic=monotonic() + 20.0,
    )
    assert tuple(item.mutation.candidate.mutation_id for item in first) == (
        first_search,
    )
    repository.settle_applied(
        delivery=first[0],
        target_semantic_identity="search-target:first",
        applied_document_semantic_fingerprint=(
            first[
                0
            ].mutation.candidate.mutation_semantic.graph_document_semantic_fingerprint
        ),
        deadline_monotonic=monotonic() + 20.0,
    )
    second = repository.claim_due(
        surface=CanonicalMutationSurface.SEARCH_INDEX,
        owner_id="surface-worker:second",
        limit=4,
        deadline_monotonic=monotonic() + 20.0,
    )
    assert tuple(item.mutation.candidate.mutation_id for item in second) == (
        second_search,
    )
    assert second[0].mutation.ordering.sequence_number == 3
    assert second[0].lease.delivery_identity.predecessor_surface_sequence_number == 1
    repository.settle_applied(
        delivery=second[0],
        target_semantic_identity="search-target:second",
        applied_document_semantic_fingerprint=(
            second[
                0
            ].mutation.candidate.mutation_semantic.graph_document_semantic_fingerprint
        ),
        deadline_monotonic=monotonic() + 20.0,
    )


def test_surface_dead_letter_retry_and_decommission_unblock_successor(
    migrated_postgres_database: MigratedPostgresTestDatabase,
) -> None:
    provider = verified_postgres_provider(migrated_postgres_database.runtime_dsn)
    default_policy = default_projection_delivery_policy()
    retry = cast(
        DurableProjectionRetryPolicyFact,
        build_projection_fact(
            DurableProjectionRetryPolicyFact,
            schema_version="durable_projection_retry_policy.v1",
            maximum_attempts=1,
            base_delay_milliseconds=1,
            maximum_delay_milliseconds=1,
            lease_duration_seconds=30,
            claim_batch_size=4,
        ),
    )
    policy = cast(
        DurableProjectionDeliveryPolicyFact,
        build_projection_fact(
            DurableProjectionDeliveryPolicyFact,
            schema_version="durable_projection_delivery_policy.v1",
            retry_policy=retry,
            physical_policy=default_policy.physical_policy,
        ),
    )
    writer = CanonicalMutationV2Writer(
        surface_plan=build_surface_plan(
            (CanonicalMutationSurface.SEARCH_INDEX,),
            delivery_policy=policy,
        ),
        connection_provider=provider,
    )
    first_id = writer.append_canonical_memory_write_mutation(
        payload={
            "mutation_lane": "runtime_semantic",
            "documents": [{"node_id": "node:blocked:one", "document": {"v": 1}}],
        },
        graph_id="graph:surface-repair",
        operation_id="surface-repair:one",
        operation_kind=(CanonicalMemoryMutationOperationKind.RUNTIME_SEMANTIC_DOCUMENT),
        requested_surfaces=(CanonicalMutationSurface.SEARCH_INDEX,),
    )
    second_id = writer.append_canonical_memory_write_mutation(
        payload={
            "mutation_lane": "runtime_semantic",
            "documents": [{"node_id": "node:blocked:two", "document": {"v": 2}}],
        },
        graph_id="graph:surface-repair",
        operation_id="surface-repair:two",
        operation_kind=(CanonicalMemoryMutationOperationKind.RUNTIME_SEMANTIC_DOCUMENT),
        requested_surfaces=(CanonicalMutationSurface.SEARCH_INDEX,),
    )
    repository = PostgresCanonicalMutationSurfaceRepository(provider)
    first = repository.claim_due(
        surface=CanonicalMutationSurface.SEARCH_INDEX,
        owner_id="surface-repair:first",
        limit=4,
        deadline_monotonic=monotonic() + 20.0,
    )
    assert tuple(item.mutation.candidate.mutation_id for item in first) == (first_id,)
    assert (
        repository.settle_failure(
            delivery=first[0],
            failure_kind=(
                DurableProjectionFailureKind.TRANSIENT_EXTERNAL_SURFACE_UNAVAILABLE
            ),
            error=RuntimeError("temporary surface outage"),
            deadline_monotonic=monotonic() + 20.0,
        ).status
        == "dead_letter"
    )
    assert (
        repository.claim_due(
            surface=CanonicalMutationSurface.SEARCH_INDEX,
            owner_id="surface-repair:blocked",
            limit=4,
            deadline_monotonic=monotonic() + 20.0,
        )
        == ()
    )
    with pytest.raises(
        ValueError,
        match="rebuild receipt is not durable FULL",
    ):
        repository.repair_dead_letter(
            mutation_id=first_id,
            surface=CanonicalMutationSurface.SEARCH_INDEX,
            action="decommission_after_rebuild",
            operator_authority_id="operator:forged-rebuild",
            rebuild_result_receipt_id="projection-result-receipt:forged",
            deadline_monotonic=monotonic() + 20.0,
        )

    retry_action = repository.repair_dead_letter(
        mutation_id=first_id,
        surface=CanonicalMutationSurface.SEARCH_INDEX,
        action="retry_same_contract",
        operator_authority_id="operator:surface-retry",
        deadline_monotonic=monotonic() + 20.0,
    )
    assert (
        repository.repair_dead_letter(
            mutation_id=first_id,
            surface=CanonicalMutationSurface.SEARCH_INDEX,
            action="retry_same_contract",
            operator_authority_id="operator:surface-retry",
            deadline_monotonic=monotonic() + 20.0,
        )
        == retry_action
    )
    retried = repository.claim_due(
        surface=CanonicalMutationSurface.SEARCH_INDEX,
        owner_id="surface-repair:retry",
        limit=4,
        deadline_monotonic=monotonic() + 20.0,
    )
    assert tuple(item.mutation.candidate.mutation_id for item in retried) == (first_id,)
    assert (
        repository.settle_failure(
            delivery=retried[0],
            failure_kind=(
                DurableProjectionFailureKind.TRANSIENT_EXTERNAL_SURFACE_UNAVAILABLE
            ),
            error=RuntimeError("persistent surface outage"),
            deadline_monotonic=monotonic() + 20.0,
        ).status
        == "dead_letter"
    )
    repository.repair_dead_letter(
        mutation_id=first_id,
        surface=CanonicalMutationSurface.SEARCH_INDEX,
        action="decommission_with_authority",
        operator_authority_id="operator:surface-decommission",
        deadline_monotonic=monotonic() + 20.0,
    )
    successor = repository.claim_due(
        surface=CanonicalMutationSurface.SEARCH_INDEX,
        owner_id="surface-repair:successor",
        limit=4,
        deadline_monotonic=monotonic() + 20.0,
    )
    assert tuple(item.mutation.candidate.mutation_id for item in successor) == (
        second_id,
    )


def test_search_surface_worker_applies_v2_delivery_without_legacy_outbox(
    migrated_postgres_database: MigratedPostgresTestDatabase,
) -> None:
    provider = verified_postgres_provider(migrated_postgres_database.runtime_dsn)
    graph_id = "graph:surface-worker"
    memory_id = "preference:surface-worker"
    now = utc_now()
    PostgresGraphStore(connection_provider=provider).put_jsonld(
        Preference(
            id=memory_id,
            statement="The user prefers deterministic projection workers.",
            scope="ctx:user",
            status=memory.NodeStatus.ACTIVE,
            confidence_level=memory.ConfidenceLevel.HIGH,
            verification_status=memory.VerificationStatus.USER_CONFIRMED,
            source_authority=memory.SourceAuthority.EXPLICIT_USER_INSTRUCTION,
            created_at=now,
            updated_at=now,
            gate_reason="projection integration test",
        ).to_jsonld(),
        graph_id=graph_id,
    )
    writer = CanonicalMutationV2Writer(
        surface_plan=build_surface_plan((CanonicalMutationSurface.SEARCH_INDEX,)),
        connection_provider=provider,
    )
    mutation_id = writer.append_canonical_memory_write_mutation(
        payload={
            "mutation_lane": "governed_memory",
            "dirty_memory_ids": [memory_id],
            "documents": [],
        },
        graph_id=graph_id,
        operation_id="surface-worker:search",
        operation_kind=CanonicalMemoryMutationOperationKind.PREFERENCE,
        requested_surfaces=(CanonicalMutationSurface.SEARCH_INDEX,),
    )
    worker = CanonicalMutationSurfaceWorker(
        repository=PostgresCanonicalMutationSurfaceRepository(provider),
        handler=PostgresSearchIndexSurfaceHandler(provider),
        owner_id="surface-worker:search",
    )

    assert (
        worker.run_once(
            limit=4,
            deadline_monotonic=monotonic() + 20.0,
        )
        == 1
    )
    with provider.connection(
        lane=PostgresConnectionLane.PROJECTION_MAINTENANCE,
        deadline_monotonic=monotonic() + 20.0,
    ) as connection:
        indexed = connection.execute(
            """
            SELECT memory_id FROM memory_search_index
            WHERE graph_id = %s AND memory_id = %s
            """,
            (graph_id, memory_id),
        ).fetchone()
        delivery = connection.execute(
            """
            SELECT status FROM canonical_mutation_surface_deliveries
            WHERE mutation_id = %s AND surface = %s
            """,
            (mutation_id, CanonicalMutationSurface.SEARCH_INDEX.value),
        ).fetchone()
    assert indexed == (memory_id,)
    assert delivery == ("applied",)


def test_run_timeline_job_incrementally_commits_receipt_head_and_outputs(
    migrated_postgres_database: MigratedPostgresTestDatabase,
) -> None:
    provider = verified_postgres_provider(migrated_postgres_database.runtime_dsn)
    event_log = PostgresEventLog(
        connection_provider=provider,
        runtime_session_id="runtime:timeline-handler",
        workspace_root="/tmp/timeline-handler",
    )
    event_log.ensure_runtime_session_owner()
    repository = PostgresDurableProjectionRepository(provider)
    run_id = "run:timeline-handler"
    turn_id = "turn:timeline-handler"
    reply_id = "reply:timeline-handler"
    target_key = projection_target_key(
        projection_kind=DurableProjectionKind.RUN_TIMELINE,
        runtime_session_id=event_log.runtime_session_id,
        run_id=run_id,
        tool_call_id=None,
    )

    event_log.append(
        ReplyStartEvent(
            **EventContext(run_id, turn_id, reply_id).event_fields(),
            name="assistant",
        )
    )
    event_log.append(
        make_text_block_segment_event(
            **EventContext(run_id, turn_id, reply_id).event_fields(),
            block_id="text:timeline:first",
            delta="first projected reply",
        )
    )
    first = event_log.append(
        ReplyEndEvent(
            **EventContext(run_id, turn_id, reply_id).event_fields(),
            model_terminal_outcome="completed",
        )
    )
    first_seed = repository.prepare_next_seed_candidate(
        runtime_session_id=event_log.runtime_session_id,
        projection_kind=DurableProjectionKind.RUN_TIMELINE,
    )
    assert first_seed is not None
    assert (
        repository.commit(
            candidate=first_seed,
            deadline_monotonic=monotonic() + 20.0,
        ).confirmation
        is DurableProjectionCommitConfirmation.FULL
    )
    first_lease = repository.claim_due(
        owner_id="worker:timeline:first",
        limit=1,
    )
    assert len(first_lease) == 1
    timeline_handler = PostgresRunTimelineProjectionHandler(provider)
    first_prepared = timeline_handler(
        first_lease[0],
        deadline_monotonic=monotonic() + 20.0,
    )
    first_outcome = repository.settle_success(
        lease=first_lease[0],
        prepared=first_prepared,
        deadline_monotonic=monotonic() + 20.0,
    )
    assert first_outcome.confirmation is DurableProjectionCommitConfirmation.FULL
    assert first_outcome.resulting_status is DurableProjectionJobStatus.SUCCEEDED, (
        first_outcome.failure
    )
    first_head = repository.read_head(
        DurableProjectionKind.RUN_TIMELINE,
        target_key,
    )
    assert first_head is not None
    assert first_head.applied_source_sequence == first.sequence
    first_receipt = repository.read_receipt(
        first_head.applied_result_receipt_reference.receipt_id
    )
    assert any(
        getattr(item, "media_type", None)
        == "application/vnd.pulsara.run-timeline-manifest+json"
        for item in first_receipt.result_document_references
    )

    second_reply_id = "reply:timeline-handler:second"
    event_log.append(
        ReplyStartEvent(
            **EventContext(run_id, turn_id, second_reply_id).event_fields(),
            name="assistant",
        )
    )
    event_log.append(
        make_text_block_segment_event(
            **EventContext(run_id, turn_id, second_reply_id).event_fields(),
            block_id="text:timeline:second",
            delta="second projected reply",
        )
    )
    second = event_log.append(
        ReplyEndEvent(
            **EventContext(run_id, turn_id, second_reply_id).event_fields(),
            model_terminal_outcome="completed",
        )
    )
    second_seed = repository.prepare_next_seed_candidate(
        runtime_session_id=event_log.runtime_session_id,
        projection_kind=DurableProjectionKind.RUN_TIMELINE,
    )
    assert second_seed is not None
    assert (
        repository.commit(
            candidate=second_seed,
            deadline_monotonic=monotonic() + 20.0,
        ).confirmation
        is DurableProjectionCommitConfirmation.FULL
    )
    second_lease = repository.claim_due(
        owner_id="worker:timeline:second",
        limit=1,
    )
    assert len(second_lease) == 1
    second_prepared = timeline_handler(
        second_lease[0],
        deadline_monotonic=monotonic() + 20.0,
    )
    second_outcome = repository.settle_success(
        lease=second_lease[0],
        prepared=second_prepared,
        deadline_monotonic=monotonic() + 20.0,
    )
    assert second_outcome.confirmation is DurableProjectionCommitConfirmation.FULL
    assert second_outcome.resulting_status is DurableProjectionJobStatus.SUCCEEDED, (
        second_outcome.failure
    )
    second_head = repository.read_head(
        DurableProjectionKind.RUN_TIMELINE,
        target_key,
    )
    assert second_head is not None
    assert second_head.head_revision == first_head.head_revision + 1
    assert second_head.applied_source_sequence == second.sequence

    with provider.connection(
        lane=PostgresConnectionLane.PROJECTION_MAINTENANCE,
        deadline_monotonic=monotonic() + 20.0,
    ) as connection:
        timeline_row = connection.execute(
            """
            SELECT payload
            FROM graph_documents
            WHERE graph_id = 'graph:default' AND id = %s
            """,
            (f"run-timeline:{event_log.runtime_session_id}:{run_id}",),
        ).fetchone()
        mutation_count = connection.execute(
            """
            SELECT count(*)
            FROM canonical_mutations_v2
            WHERE mutation_id = ANY(%s)
            """,
            (
                [
                    item.mutation_id
                    for item in (
                        *first_prepared.canonical_mutation_candidates,
                        *second_prepared.canonical_mutation_candidates,
                    )
                ],
            ),
        ).fetchone()
    assert timeline_row is not None
    assert timeline_row[0]["@id"] == (
        f"run-timeline:{event_log.runtime_session_id}:{run_id}"
    )
    assert mutation_count is not None
    assert mutation_count[0] == 2
    page = load_run_timeline_page(
        graph=PostgresGraphStore(provider),
        archive=PostgresArtifactStore(provider),
        run_id=run_id,
        runtime_session_id=event_log.runtime_session_id,
        graph_id="graph:default",
        max_items=2,
    )
    assert page.total_completed_items == 4
    assert tuple(item.kind for item in page.items) == (
        "reply",
        "assistant_text",
    )
    assert tuple(item.summary for item in page.items if item.summary) == (
        "second projected reply",
    )
    assert page.next_cursor is not None
    prior_page = load_run_timeline_page(
        graph=PostgresGraphStore(provider),
        archive=PostgresArtifactStore(provider),
        run_id=run_id,
        runtime_session_id=event_log.runtime_session_id,
        graph_id="graph:default",
        max_items=2,
        cursor=page.next_cursor,
    )
    assert tuple(item.kind for item in prior_page.items) == (
        "reply",
        "assistant_text",
    )
    assert tuple(item.summary for item in prior_page.items if item.summary) == (
        "first projected reply",
    )
    assert prior_page.next_cursor is None
    with pytest.raises(RunTimelineExportLimitExceeded):
        load_run_timeline(
            graph=PostgresGraphStore(provider),
            archive=PostgresArtifactStore(provider),
            run_id=run_id,
            runtime_session_id=event_log.runtime_session_id,
            graph_id="graph:default",
            max_items=3,
        )
    exported = load_run_timeline(
        graph=PostgresGraphStore(provider),
        archive=PostgresArtifactStore(provider),
        run_id=run_id,
        runtime_session_id=event_log.runtime_session_id,
        graph_id="graph:default",
        max_items=4,
    )
    assert tuple(item.summary for item in exported.items if item.summary) == (
        "first projected reply",
        "second projected reply",
    )


def test_tool_evidence_job_joins_terminal_artifact_and_immutable_relation(
    tmp_path,
    migrated_postgres_database: MigratedPostgresTestDatabase,
) -> None:
    provider = verified_postgres_provider(migrated_postgres_database.runtime_dsn)
    runtime_session_id = "runtime:evidence-handler"
    event_log = PostgresEventLog(
        connection_provider=provider,
        runtime_session_id=runtime_session_id,
        workspace_root=str(tmp_path),
    )
    event_log.ensure_runtime_session_owner()
    runtime = in_memory_runtime_session(
        tmp_path,
        runtime_session_id=runtime_session_id,
    )
    repository = PostgresDurableProjectionRepository(provider)
    run_id = "run:evidence-handler"
    turn_id = "turn:evidence-handler"
    reply_id = "reply:evidence-handler"
    tool_call_id = "call:evidence-handler"
    tool_name = "read"
    common = EventContext(run_id, turn_id, reply_id).event_fields()

    event_log.extend(
        (
            ToolCallStartEvent(
                id="event:evidence-call-start",
                **common,
                tool_call_id=tool_call_id,
                tool_call_name=tool_name,
                durable_semantic_event_index=0,
            ),
            ToolCallArgumentsSegmentEvent(
                id="event:evidence-call-arguments",
                **common,
                tool_call_id=tool_call_id,
                delta='{"path":',
                durable_semantic_event_index=1,
            ),
            ToolCallEndEvent(
                id="event:evidence-call-end",
                **common,
                tool_call_id=tool_call_id,
                durable_semantic_event_index=2,
            ),
        )
    )
    prepared_terminal = asyncio.run(
        runtime.tool_terminal_projection_service.prepare_batch(
            (
                ToolResultStartEvent(
                    id="event:evidence-result-start",
                    **common,
                    tool_call_id=tool_call_id,
                    tool_call_name=tool_name,
                ),
                ToolResultTextDeltaEvent(
                    id="event:evidence-result-text",
                    **common,
                    tool_call_id=tool_call_id,
                    delta="Pulsara evidence output",
                ),
                tool_result_end_candidate(
                    event_id="event:evidence-result-end",
                    run_id=run_id,
                    turn_id=turn_id,
                    reply_id=reply_id,
                    tool_call_id=tool_call_id,
                    tool_name=tool_name,
                    state=ToolResultState.SUCCESS,
                ),
            )
        )
    )
    postgres_archive = PostgresArtifactStore(provider)
    for blob in runtime.archive.blobs.values():
        assert blob.text_content is not None
        postgres_archive.put_text(
            blob.id,
            blob.text_content,
            session_id=runtime_session_id,
            run_id=run_id,
            media_type=blob.media_type,
            metadata=blob.metadata,
        )
    committed = event_log.extend(prepared_terminal)
    terminal = next(item for item in committed if isinstance(item, ToolResultEndEvent))
    source = exact_stored_event(event_log=event_log, event_id=terminal.id)
    assert source.source_reference.sequence == terminal.sequence

    seed = repository.prepare_next_seed_candidate(
        runtime_session_id=runtime_session_id,
        projection_kind=(DurableProjectionKind.TOOL_RESULT_EXECUTION_EVIDENCE),
    )
    assert seed is not None
    assert (
        repository.commit(
            candidate=seed,
            deadline_monotonic=monotonic() + 20.0,
        ).confirmation
        is DurableProjectionCommitConfirmation.FULL
    )
    leases = repository.claim_due(
        owner_id="worker:evidence",
        limit=8,
    )
    evidence_leases = tuple(
        item
        for item in leases
        if item.job.projection_kind
        is DurableProjectionKind.TOOL_RESULT_EXECUTION_EVIDENCE
    )
    assert len(evidence_leases) == 1
    handler = PostgresToolResultEvidenceProjectionHandler(provider)
    prepared = handler(
        evidence_leases[0],
        deadline_monotonic=monotonic() + 20.0,
    )
    outcome = repository.settle_success(
        lease=evidence_leases[0],
        prepared=prepared,
        deadline_monotonic=monotonic() + 20.0,
    )
    assert outcome.confirmation is DurableProjectionCommitConfirmation.FULL
    assert outcome.resulting_status is DurableProjectionJobStatus.SUCCEEDED
    assert outcome.result_receipt_reference is not None

    target_key = projection_target_key(
        projection_kind=(DurableProjectionKind.TOOL_RESULT_EXECUTION_EVIDENCE),
        runtime_session_id=runtime_session_id,
        run_id=run_id,
        tool_call_id=tool_call_id,
    )
    head = repository.read_head(
        DurableProjectionKind.TOOL_RESULT_EXECUTION_EVIDENCE,
        target_key,
    )
    assert head is not None
    receipt = repository.read_receipt(head.applied_result_receipt_reference.receipt_id)
    assert receipt.result_semantic.projection_kind is (
        DurableProjectionKind.TOOL_RESULT_EXECUTION_EVIDENCE
    )
    source_documents = tuple(
        item
        for item in receipt.result_document_references
        if getattr(item, "media_type", None)
        == "application/vnd.pulsara.tool-result-evidence-source+json"
    )
    assert len(source_documents) == 1
    source_payload = json.loads(
        postgres_archive.get_text(
            source_documents[0].artifact_reference.artifact_semantic_id
        )
    )
    assert source_payload["tool_call_arguments"]["raw_arguments_json"] == ('{"path":')
    assert source_payload["tool_call_arguments"]["parse_disposition"] == (
        "invalid_json"
    )
    assert source_payload["tool_call_arguments"]["parse_error_code"] == (
        "json_decode_error"
    )
    relations = tuple(
        item
        for item in receipt.result_document_references
        if getattr(item, "document_kind", None) == "graph_relation"
    )
    assert len(relations) == 1
    graph = PostgresGraphStore(connection_provider=provider)
    typed_view = graph.get_jsonld_read_view(
        turn_id,
        graph_id="graph:default",
    )
    assert typed_view.merged_relation_count == 1
    turn_view = graph.get_jsonld(turn_id, graph_id="graph:default")
    produced = turn_view[runtime_ontology.PRODUCED.name]
    assert len(produced) == 1
    assert produced[0]["@id"].startswith("tool-result:")
