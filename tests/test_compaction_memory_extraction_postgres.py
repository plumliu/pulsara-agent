from __future__ import annotations

import asyncio
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
from time import monotonic

import pytest

from pulsara_agent.event import (
    ContextCompactionCompletedEvent,
    ContextCompactionMemoryExtractionCompletedEvent,
    ContextCompactionMemoryExtractionRequestedEvent,
    ContextCompactionStartedEvent,
    EventContext,
    ModelCallEndEvent,
    ModelCallStartEvent,
    RunStartEvent,
)
from pulsara_agent.event_log.postgres import PostgresEventLog
from pulsara_agent.event_log.serialization import DEFAULT_EVENT_SCHEMA_REGISTRY
from pulsara_agent.llm.terminal_projection import stable_event_identity
from pulsara_agent.llm.result import TransportUsageReport
from pulsara_agent.llm.terminal_projection import (
    ModelTerminalProjectionReducer,
    bind_model_terminal_projection_to_session,
    build_default_terminal_projection_contract_bundle,
    persist_model_terminal_projection,
)
from pulsara_agent.inspector import InspectorService, PostgresInspectorStore
from pulsara_agent.memory.candidates.projection_outbox import (
    PostgresCandidateProjectionOutbox,
)
from pulsara_agent.memory.artifacts.archive import InMemoryArchiveStore
from pulsara_agent.memory.compaction.extension import (
    build_compaction_memory_extraction_contract,
)
from pulsara_agent.runtime.projection_jobs.compaction_memory_driver import (
    _INPUT_ARTIFACT_CONTRACT_FINGERPRINT,
)
from pulsara_agent.projection_jobs.compaction_memory_policy import (
    build_default_compaction_memory_extraction_policy,
)
from pulsara_agent.memory.compaction.result_candidate import (
    build_extraction_completed_event,
    build_result_candidate,
)
from pulsara_agent.memory.compaction.contracts import (
    build_settlement_write_attempt,
)
from pulsara_agent.primitives._context_base import context_fingerprint
from pulsara_agent.primitives.compaction import (
    CANONICAL_EMPTY_COMPACTION_MEMORY_EVIDENCE_SET_FINGERPRINT,
    CompactionHumanEvidenceManifestReferenceFact,
    CompactionMemoryExtractionModelInputAttributionFact,
    CompactionPostCompletionExtensionLinkFact,
    CompactionPostCompletionExtensionRequestedFact,
    ContentAddressedArtifactReferenceFact,
    ResolvedExtractionInputBudgetAttributionFact,
)
from pulsara_agent.primitives.frozen import build_frozen_fact
from pulsara_agent.primitives.governance_evidence import (
    GovernanceEvidenceArtifactReferenceFact,
    GovernanceStoredEventReferenceFact,
)
from pulsara_agent.primitives.long_horizon import (
    calculate_model_call_reservation,
    default_rollout_budget_policy,
)
from pulsara_agent.primitives.model_call import (
    DEFAULT_MODEL_STREAM_SEGMENT_POLICY_CONTRACT,
    ModelCallPurpose,
    ModelTokenUsageFact,
)
from pulsara_agent.primitives.runtime_event_vocabulary import (
    build_bounded_runtime_failure_diagnostic,
)
from pulsara_agent.projection_jobs.compaction_memory import (
    CompactionMemoryModelResultAttributionFact,
    CompactionMemoryExtractionResultCandidateFact,
    CompactionMemoryExtractionOccurrenceAttributionFact,
    CompactionMemoryNoEligibleEvidenceAttributionFact,
    CompactionMemoryNoEligibleEvidenceResultSemanticFact,
    CompactionMemoryValidEmptyResultSemanticFact,
)
from pulsara_agent.projection_jobs.contracts import (
    DurableProjectionCommitConfirmation,
    DurableProjectionFailureKind,
    DurableProjectionJobCandidateFact,
    DurableProjectionJobStatus,
    DurableProjectionKind,
    LeasedDurableProjectionJob,
)
from pulsara_agent.runtime.projection_jobs.postgres_repository import (
    PostgresDurableProjectionRepository,
)
from pulsara_agent.runtime.projection_jobs.model_lifecycle import (
    build_model_lifecycle_companions,
)
from pulsara_agent.runtime.projection_jobs.compaction_memory_settlement import (
    RuntimeSessionCompactionMemoryExtractionSettlementPort,
)
from pulsara_agent.runtime.projection_jobs.compaction_memory_driver_registry import (
    ProcessCompactionMemoryExtractionDriverRegistry,
)
from pulsara_agent.runtime.projection_jobs.registry import (
    DurableProjectionExecutableRegistry,
)
from pulsara_agent.runtime.projection_jobs.service import DurableProjectionJobService
from pulsara_agent.runtime.projection_jobs.source import exact_stored_event
from pulsara_agent.runtime.provider_input.coordinator import (
    ProviderInputPreparationStale,
)
from pulsara_agent.runtime.provider_input.planner import (
    build_one_shot_generation_close_event,
)
from pulsara_agent.runtime.session import RuntimeSession
from pulsara_agent.storage.postgres_connection_provider import (
    VerifiedPostgresConnectionProviderProtocol,
)
from pulsara_agent.storage.session_bootstrap import (
    PostgresRuntimeSessionOwnerBootstrapPort,
)
from tests.support.artifacts import FakeToolResultArtifactIndex
from tests.support.model_call import (
    compaction_completed_contract_fields,
    compaction_started_contract_fields,
    model_call_end_fields,
    model_call_start_fields,
    prepared_provider_input_bundle_fixture,
    test_resolved_call_fact,
)
from tests.conftest import (
    persist_test_run_transcript_seed,
    run_start_permission_fields,
)
from tests.support.postgres import (
    connect_postgres_test_database,
    verified_postgres_provider,
)
from tests.support.postgres_database import MigratedPostgresTestDatabase


@dataclass(slots=True)
class _TestBackgroundAdmissionLease:
    resolved_model_call_id: str
    state: str = "issued"

    def begin_model_start(self) -> None:
        if self.state != "issued":
            raise RuntimeError("background admission lease is not issued")
        self.state = "in_flight"

    def validate_model_start(self, *, resolved_model_call_id: str) -> None:
        if (
            self.state != "in_flight"
            or resolved_model_call_id != self.resolved_model_call_id
        ):
            raise RuntimeError("background admission lease validation failed")

    def confirm_model_start_full(self) -> None:
        self.state = "consumed"

    def mark_reconciliation_required(self) -> None:
        self.state = "reconciliation_required"

    def release(self) -> None:
        self.state = "released"


@dataclass(frozen=True, slots=True)
class _NoCallSettlementFixture:
    runtime: RuntimeSession
    repository: PostgresDurableProjectionRepository
    job: DurableProjectionJobCandidateFact
    lease: LeasedDurableProjectionJob
    result_candidate: CompactionMemoryExtractionResultCandidateFact
    result_event: ContextCompactionMemoryExtractionCompletedEvent
    request: ContextCompactionMemoryExtractionRequestedEvent


@dataclass(slots=True)
class _FailingExtractionDriver:
    runtime_session_id: str
    driver_generation: int
    binding_fingerprint: str = "sha256:test-failing-extraction-driver"

    async def execute_leased_job(self, job, *, deadline_monotonic: float) -> None:
        del job, deadline_monotonic
        raise ProviderInputPreparationStale("foreground provider input won the race")

    async def settle_result_candidate(
        self,
        result_candidate,
        *,
        settlement_generation: int,
        deadline_monotonic: float,
    ) -> None:
        del result_candidate, settlement_generation, deadline_monotonic
        raise AssertionError("unexpected settlement")

    def stop_admission(self) -> None:
        pass

    async def close(self, *, deadline_monotonic: float) -> None:
        del deadline_monotonic


def _request_pair(runtime_session_id: str):
    suffix = sha256(runtime_session_id.encode("utf-8")).hexdigest()[:12]
    context = EventContext(
        run_id=f"run:compaction-memory-postgres:{suffix}",
        turn_id=f"turn:compaction-memory-postgres:{suffix}",
        reply_id=f"reply:compaction-memory-postgres:{suffix}",
    )
    completed_id = f"context_compaction_completed:memory-postgres:{suffix}"
    request_id = (
        f"context_compaction_memory_extraction_requested:memory-postgres:{suffix}"
    )
    link = build_frozen_fact(
        CompactionPostCompletionExtensionLinkFact,
        schema_version="compaction_post_completion_extension_link.v1",
        compaction_id=f"context_compaction:memory-postgres:{suffix}",
        completed_event_id=completed_id,
        request_event_id=request_id,
        extension_contract_fingerprint=context_fingerprint(
            "test-compaction-extension:v1", runtime_session_id
        ),
    )
    requested = build_frozen_fact(
        CompactionPostCompletionExtensionRequestedFact,
        schema_version="compaction_post_completion_extension_requested.v1",
        disposition_kind="requested",
        extension_link=link,
    )
    started_fields = compaction_started_contract_fields()
    started_fields["terminal_event_id"] = completed_id
    started = ContextCompactionStartedEvent(
        id="context_compaction_started:test:" + suffix,
        **context.event_fields(),
        **started_fields,
        compaction_id=link.compaction_id,
        trigger="auto",
        reason="context_threshold",
        window_number=1,
        window_id=f"context_window:memory-postgres:{suffix}",
        threshold_tokens=8_000,
        through_sequence=1,
        keep_after_sequence=1,
    )
    completed_fields = compaction_completed_contract_fields()
    completed_fields["started_event_id"] = started.id
    completed = ContextCompactionCompletedEvent(
        id=completed_id,
        **context.event_fields(),
        **completed_fields,
        compaction_id=link.compaction_id,
        trigger="auto",
        reason="context_threshold",
        window_number=1,
        window_id="context_window:memory-postgres",
        summary_artifact_id="artifact:compaction-summary",
        summary_chars=16,
        threshold_tokens=8_000,
        through_sequence=1,
        keep_after_sequence=1,
        post_completion_extension_dispositions=(requested,),
    )
    root_bytes = b"{}"
    root = build_frozen_fact(
        ContentAddressedArtifactReferenceFact,
        schema_version="content_addressed_artifact_reference.v1",
        artifact_id="compaction-human-evidence-manifest-root:test",
        artifact_kind="compaction-human-evidence-manifest-root",
        media_type=("application/vnd.pulsara.compaction-human-evidence-root+json"),
        content_sha256=sha256(root_bytes).hexdigest(),
        content_bytes=len(root_bytes),
        artifact_contract_fingerprint="sha256:test-manifest-contract",
    )
    manifest = build_frozen_fact(
        CompactionHumanEvidenceManifestReferenceFact,
        schema_version="compaction_human_evidence_manifest_reference.v1",
        manifest_semantic_fingerprint="sha256:test-manifest-semantic",
        manifest_attribution_fingerprint="sha256:test-manifest-attribution",
        paged_manifest_root_reference=root,
    )
    extraction_contract = build_compaction_memory_extraction_contract()
    target = test_resolved_call_fact(
        purpose=ModelCallPurpose.COMPACTION_MEMORY_EXTRACTION
    ).target
    policy = build_default_compaction_memory_extraction_policy(model_target=target)
    request = ContextCompactionMemoryExtractionRequestedEvent(
        id=request_id,
        **context.event_fields(),
        extension_link=link,
        human_evidence_manifest_reference=manifest,
        memory_domain_id="memory-domain:test",
        resolved_scope="ctx:user",
        extraction_contract=extraction_contract,
        extraction_policy=policy,
        business_occurrence_fingerprint=context_fingerprint(
            "compaction-memory-extraction-request-occurrence:v1",
            (runtime_session_id, link.compaction_id, completed.id, request_id),
        ),
        event_semantic_fingerprint=context_fingerprint(
            "context-compaction-memory-extraction-request-semantic:v1",
            {
                "manifest_semantic": manifest.manifest_semantic_fingerprint,
                "memory_domain_id": "memory-domain:test",
                "resolved_scope": "ctx:user",
                "extraction_contract": extraction_contract.contract_fingerprint,
            },
        ),
    )
    return context, started, completed, request


def _seed_one_request(
    *,
    event_log: PostgresEventLog,
    repository: PostgresDurableProjectionRepository,
):
    candidate = repository.prepare_next_seed_candidate(
        runtime_session_id=event_log.runtime_session_id,
        projection_kind=DurableProjectionKind.COMPACTION_MEMORY_EXTRACTION,
        deadline_monotonic=monotonic() + 20.0,
    )
    assert candidate is not None
    jobs = candidate.ordered_job_candidates
    assert len(jobs) == 1
    job = jobs[0]
    assert job.job_semantic.trigger_horizon.through_sequence == (
        job.job_semantic.source_event_reference.sequence
    )
    outcome = repository.commit(
        candidate=candidate,
        deadline_monotonic=monotonic() + 20.0,
    )
    assert outcome.confirmation is DurableProjectionCommitConfirmation.FULL
    return job


def _governance_event_reference(
    *,
    event_log: PostgresEventLog,
    runtime_session_id: str,
    event_id: str,
) -> GovernanceStoredEventReferenceFact:
    source = exact_stored_event(
        event_log=event_log,
        event_id=event_id,
        deadline_monotonic=monotonic() + 20.0,
    )
    return build_frozen_fact(
        GovernanceStoredEventReferenceFact,
        schema_version="governance_stored_event_reference.v1",
        stable_identity=stable_event_identity(
            source.envelope.decode_owned(DEFAULT_EVENT_SCHEMA_REGISTRY),
            runtime_session_id=runtime_session_id,
        ),
        sequence=source.envelope.sequence,
        stored_envelope_fingerprint=source.envelope.envelope_fingerprint,
    )


def _prepare_no_call_settlement_fixture(
    *,
    tmp_path: Path,
    provider: VerifiedPostgresConnectionProviderProtocol,
    runtime_session_id: str,
    install_result_candidate: bool = True,
) -> _NoCallSettlementFixture:
    event_log = PostgresEventLog(
        connection_provider=provider,
        runtime_session_id=runtime_session_id,
        workspace_root=str(tmp_path),
    )
    event_log.ensure_runtime_session_owner()
    runtime = RuntimeSession(
        tmp_path,
        event_log=event_log,
        archive=InMemoryArchiveStore(),
        tool_result_artifacts=FakeToolResultArtifactIndex(),
        runtime_session_id=runtime_session_id,
    )
    context, started, completed, request = _request_pair(runtime_session_id)
    seed = persist_test_run_transcript_seed(runtime, run_id=context.run_id)
    user_input = "remember this"
    run_start_fields = run_start_permission_fields(
        context.run_id,
        user_input=user_input,
        turn_id=context.turn_id,
        reply_id=context.reply_id,
        mcp_installation_owner_runtime_session_id=runtime_session_id,
    )
    run_start_fields.update(
        run_transcript_seed_semantic=seed.seed_semantic,
        run_transcript_seed_reference=seed.seed_reference,
    )

    async def _commit_source_events() -> None:
        await runtime.write_event(
            RunStartEvent(
                **context.event_fields(),
                **run_start_fields,
                user_input_chars=len(user_input),
                metadata={"user_input": user_input},
            )
        )
        await runtime.write_events((started, completed, request))

    asyncio.run(_commit_source_events())
    repository = PostgresDurableProjectionRepository(provider)
    job = _seed_one_request(event_log=event_log, repository=repository)
    lease = repository.claim_due_session_model(
        owner_id=f"worker:no-call:{runtime_session_id}",
        runtime_session_ids=(runtime_session_id,),
        limit=1,
        deadline_monotonic=monotonic() + 20.0,
    )[0]
    request_source = exact_stored_event(
        event_log=event_log,
        event_id=request.id,
        deadline_monotonic=monotonic() + 20.0,
    )
    request_reference = build_frozen_fact(
        GovernanceStoredEventReferenceFact,
        schema_version="governance_stored_event_reference.v1",
        stable_identity=stable_event_identity(
            request_source.envelope.decode_owned(DEFAULT_EVENT_SCHEMA_REGISTRY),
            runtime_session_id=runtime_session_id,
        ),
        sequence=request_source.envelope.sequence,
        stored_envelope_fingerprint=request_source.envelope.envelope_fingerprint,
    )
    semantic = build_frozen_fact(
        CompactionMemoryNoEligibleEvidenceResultSemanticFact,
        schema_version="compaction_memory_no_eligible_result_semantic.v1",
        outcome_kind="no_eligible_evidence",
        evidence_set_semantic_fingerprint=(
            CANONICAL_EMPTY_COMPACTION_MEMORY_EVIDENCE_SET_FINGERPRINT
        ),
        extraction_semantic_contract_fingerprint="sha256:test-extraction-semantic",
    )
    no_evidence = build_frozen_fact(
        CompactionMemoryNoEligibleEvidenceAttributionFact,
        schema_version="compaction_memory_no_eligible_attribution.v1",
        outcome_kind="no_eligible_evidence",
    )
    occurrence = build_frozen_fact(
        CompactionMemoryExtractionOccurrenceAttributionFact,
        schema_version="compaction_memory_extraction_occurrence_attribution.v1",
        compaction_id=request.extension_link.compaction_id,
        extension_link=request.extension_link,
        request_event_reference=request_reference,
        durable_job_id=lease.job.job_id,
        durable_job_source_reference=lease.job.source_event_reference,
        human_evidence_manifest_reference=request.human_evidence_manifest_reference,
        outcome_attribution=no_evidence,
    )
    result_event = build_extraction_completed_event(
        runtime_session_id=runtime_session_id,
        event_context=context,
        created_at_utc=request.created_at,
        lease=lease,
        result_semantic=semantic,
        occurrence_attribution=occurrence,
        candidate_attributions=(),
    )
    empty_accumulator = context_fingerprint(
        "compaction-memory-permanent-omission:v1:empty", ()
    )
    result_candidate = build_result_candidate(
        runtime_session_id=runtime_session_id,
        lease=lease,
        event=result_event,
        intended_target_head_revision=1,
        expected_target_head_fingerprint=None,
        permanent_automatic_omission_count=0,
        permanent_automatic_omission_semantic_accumulator=empty_accumulator,
        permanent_automatic_omission_attribution_accumulator=empty_accumulator,
    )
    if install_result_candidate:
        installation_guard = (
            repository.prepare_compaction_memory_result_installation_guard(
                lease=lease,
                result_candidate=result_candidate,
                deadline_monotonic=monotonic() + 20.0,
            )
        )
        assert (
            installation_guard.source_job_state_revision
            == lease.expected_state_revision
        )
        assert (
            installation_guard.source_job_lease_fingerprint
            == lease.lease_fingerprint
        )
        assert installation_guard.target_lease_fingerprint
        assert (
            repository.install_compaction_memory_result_candidate(
                lease=lease,
                result_candidate=result_candidate,
                installation_guard=installation_guard,
                deadline_monotonic=monotonic() + 20.0,
            )
            is DurableProjectionCommitConfirmation.FULL
        )
    return _NoCallSettlementFixture(
        runtime=runtime,
        repository=repository,
        job=job,
        lease=lease,
        result_candidate=result_candidate,
        result_event=result_event,
        request=request,
    )


def test_result_candidate_installation_guard_rejects_stale_job_state(
    tmp_path: Path,
    migrated_postgres_database: MigratedPostgresTestDatabase,
) -> None:
    provider = verified_postgres_provider(migrated_postgres_database.runtime_dsn)
    fixture = _prepare_no_call_settlement_fixture(
        tmp_path=tmp_path,
        provider=provider,
        runtime_session_id="runtime:extraction-stale-installation-guard",
        install_result_candidate=False,
    )
    try:
        guard = fixture.repository.prepare_compaction_memory_result_installation_guard(
            lease=fixture.lease,
            result_candidate=fixture.result_candidate,
            deadline_monotonic=monotonic() + 20.0,
        )
        assert (
            fixture.repository.release_session_model_lease_without_attempt(
                fixture.lease,
                reason="driver_busy",
                deadline_monotonic=monotonic() + 20.0,
            )
            is DurableProjectionCommitConfirmation.FULL
        )
        assert (
            fixture.repository.install_compaction_memory_result_candidate(
                lease=fixture.lease,
                result_candidate=fixture.result_candidate,
                installation_guard=guard,
                deadline_monotonic=monotonic() + 20.0,
            )
            is DurableProjectionCommitConfirmation.NONE
        )
    finally:
        fixture.runtime.close()


def test_v9_bootstrap_seed_claim_and_busy_deferral(
    migrated_postgres_database: MigratedPostgresTestDatabase,
) -> None:
    provider = verified_postgres_provider(migrated_postgres_database.runtime_dsn)
    runtime_session_id = "runtime:compaction-memory-seed"
    event_log = PostgresEventLog(
        connection_provider=provider,
        runtime_session_id=runtime_session_id,
        workspace_root="/tmp/compaction-memory-seed",
    )
    event_log.ensure_runtime_session_owner()
    _context, started, completed, request = _request_pair(runtime_session_id)
    stored = event_log.extend((started, completed, request))
    repository = PostgresDurableProjectionRepository(provider)

    with connect_postgres_test_database(
        migrated_postgres_database.admin_dsn
    ) as connection:
        cutover = connection.execute(
            """
            SELECT 1 FROM durable_projection_session_cutovers
            WHERE runtime_session_id = %s AND projection_kind = %s
            """,
            (
                runtime_session_id,
                DurableProjectionKind.COMPACTION_MEMORY_EXTRACTION.value,
            ),
        ).fetchone()
        account = connection.execute(
            """
            SELECT account_revision, account_payload
            FROM background_derived_work_budget_accounts
            WHERE runtime_session_id = %s
            """,
            (runtime_session_id,),
        ).fetchone()
    assert cutover is not None
    assert account is not None
    assert account[0] == 0
    assert account[1]["open_reservation_count"] == 0

    job = _seed_one_request(event_log=event_log, repository=repository)
    assert job.job_semantic.source_event_reference.event_id == request.id
    assert job.job_semantic.source_event_reference.sequence == stored[-1].sequence
    leases = repository.claim_due_session_model(
        owner_id="worker:compaction-memory",
        runtime_session_ids=(runtime_session_id,),
        limit=1,
        deadline_monotonic=monotonic() + 20.0,
    )
    assert len(leases) == 1
    lease = leases[0]
    assert lease.attempt_count == 0
    assert lease.dispatch_attempt_count == 0
    assert lease.lease_generation == 1
    assert (
        repository.release_session_model_lease_without_attempt(
            lease,
            reason="driver_busy",
            deadline_monotonic=monotonic() + 20.0,
        )
        is DurableProjectionCommitConfirmation.FULL
    )
    record = repository.read_job(job.job_semantic.job_id)
    assert record is not None
    assert record.state.status is DurableProjectionJobStatus.PENDING
    assert record.state.attempt_count == 0
    assert record.state.dispatch_attempt_count == 0
    assert record.state.compaction_memory_deferral is not None
    assert record.state.compaction_memory_deferral.reason == "driver_busy"
    assert (
        repository.claim_due_session_model(
            owner_id="worker:too-soon",
            runtime_session_ids=(runtime_session_id,),
            limit=1,
            deadline_monotonic=monotonic() + 20.0,
        )
        == ()
    )


def test_driver_failure_before_model_start_releases_durable_lease(
    migrated_postgres_database: MigratedPostgresTestDatabase,
) -> None:
    provider = verified_postgres_provider(migrated_postgres_database.runtime_dsn)
    runtime_session_id = "runtime:compaction-memory-driver-pre-start-failure"
    event_log = PostgresEventLog(
        connection_provider=provider,
        runtime_session_id=runtime_session_id,
        workspace_root="/tmp/compaction-memory-driver-pre-start-failure",
    )
    event_log.ensure_runtime_session_owner()
    _context, started, completed, request = _request_pair(runtime_session_id)
    event_log.extend((started, completed, request))
    repository = PostgresDurableProjectionRepository(provider)
    job = _seed_one_request(event_log=event_log, repository=repository)
    lease = repository.claim_due_session_model(
        owner_id="worker:pre-start-failure",
        runtime_session_ids=(runtime_session_id,),
        limit=1,
        deadline_monotonic=monotonic() + 20.0,
    )[0]
    registry = ProcessCompactionMemoryExtractionDriverRegistry()
    generation = registry.next_driver_generation(runtime_session_id)
    registration = registry.register(
        _FailingExtractionDriver(
            runtime_session_id=runtime_session_id,
            driver_generation=generation,
        )
    )
    service = DurableProjectionJobService(
        connection_provider=provider,
        executable_registry=DurableProjectionExecutableRegistry(()),
        session_driver_registry=registry,
    )

    with pytest.raises(ProviderInputPreparationStale):
        asyncio.run(service._execute_session_model_lease(lease))

    record = repository.read_job(job.job_semantic.job_id)
    assert record is not None
    assert record.state.status is DurableProjectionJobStatus.PENDING
    assert record.state.dispatch_attempt_count == 0
    assert record.state.compaction_memory_deferral is not None
    assert record.state.compaction_memory_deferral.reason == "safe_point_stale"
    assert registry.active_borrow_count(runtime_session_id) == 0
    registration.revoke()


def test_deterministic_pre_dispatch_failure_dead_letters_without_attempt(
    migrated_postgres_database: MigratedPostgresTestDatabase,
) -> None:
    provider = verified_postgres_provider(migrated_postgres_database.runtime_dsn)
    runtime_session_id = "runtime:compaction-memory-pre-dispatch-dead-letter"
    event_log = PostgresEventLog(
        connection_provider=provider,
        runtime_session_id=runtime_session_id,
        workspace_root="/tmp/compaction-memory-pre-dispatch-dead-letter",
    )
    event_log.ensure_runtime_session_owner()
    _context, started, completed, request = _request_pair(runtime_session_id)
    event_log.extend((started, completed, request))
    repository = PostgresDurableProjectionRepository(provider)
    job = _seed_one_request(event_log=event_log, repository=repository)
    lease = repository.claim_due_session_model(
        owner_id="worker:pre-dispatch-dead-letter",
        runtime_session_ids=(runtime_session_id,),
        limit=1,
        deadline_monotonic=monotonic() + 20.0,
    )[0]
    failure = ValueError("manifest root authority mismatch")

    confirmation = repository.dead_letter_session_model_job_before_attempt(
        lease,
        failure_kind=DurableProjectionFailureKind.SOURCE_AUTHORITY_CONFLICT,
        error=failure,
        deadline_monotonic=monotonic() + 20.0,
    )

    assert confirmation is DurableProjectionCommitConfirmation.FULL
    record = repository.read_job(job.job_semantic.job_id)
    assert record is not None
    assert record.state.status is DurableProjectionJobStatus.DEAD_LETTER
    assert record.state.dispatch_attempt_count == 0
    assert record.state.last_failure is not None
    assert (
        repository.claim_due_session_model(
            owner_id="worker:must-not-reclaim-dead-letter",
            runtime_session_ids=(runtime_session_id,),
            limit=1,
            deadline_monotonic=monotonic() + 20.0,
        )
        == ()
    )
    inspection = InspectorService(PostgresInspectorStore(provider)).inspect_session(
        runtime_session_id
    )
    extraction = inspection["compaction_memory_extraction_durable_status"]
    assert len(extraction) == 1
    assert extraction[0]["status"] == "dead_letter", inspection["durable_projections"][
        "diagnostics"
    ]
    assert extraction[0]["job"]["authority_status"] == "trusted"


def test_no_call_result_settlement_is_one_atomic_runtime_write(
    tmp_path,
    migrated_postgres_database: MigratedPostgresTestDatabase,
) -> None:
    provider = verified_postgres_provider(migrated_postgres_database.runtime_dsn)
    runtime_session_id = "runtime:compaction-memory-settlement"
    fixture = _prepare_no_call_settlement_fixture(
        tmp_path=tmp_path,
        provider=provider,
        runtime_session_id=runtime_session_id,
    )
    claimed = fixture.repository.claim_compaction_memory_settlements(
        runtime_session_ids=(runtime_session_id,),
        limit=1,
        deadline_monotonic=monotonic() + 20.0,
    )
    assert len(claimed) == 1

    wake_count = 0

    def _on_result_full() -> None:
        nonlocal wake_count
        wake_count += 1

    port = RuntimeSessionCompactionMemoryExtractionSettlementPort(
        runtime_session=fixture.runtime,
        repository=fixture.repository,
        outbox=PostgresCandidateProjectionOutbox(provider),
        on_result_full=_on_result_full,
    )
    try:
        outcome = asyncio.run(
            port.commit_result(
                result_candidate=fixture.result_candidate,
                write_attempt=build_settlement_write_attempt(
                    result_candidate=fixture.result_candidate,
                    settlement_generation=claimed[0].state.settlement_generation,
                    deadline_monotonic=monotonic() + 20.0,
                ),
            )
        )
        assert outcome.confirmation == "full"
        assert outcome.producer_event_identity.event_id == fixture.result_event.id
        assert outcome.result_receipt_reference is not None
        assert wake_count == 1
    finally:
        fixture.runtime.close()

    record = fixture.repository.read_job(fixture.job.job_semantic.job_id)
    assert record is not None
    assert record.state.status is DurableProjectionJobStatus.SUCCEEDED
    confirmation, receipt, head_revision = (
        fixture.repository.confirm_compaction_memory_settlement(
            fixture.result_candidate,
            deadline_monotonic=monotonic() + 20.0,
        )
    )
    assert confirmation is DurableProjectionCommitConfirmation.FULL
    assert receipt is not None
    assert outcome.result_receipt_reference == receipt
    assert head_revision == 1
    assert fixture.runtime.event_log.get_by_id(fixture.result_event.id) is not None
    with connect_postgres_test_database(
        migrated_postgres_database.admin_dsn
    ) as connection:
        budget_rows = connection.execute(
            """
            SELECT count(*) FROM background_derived_work_budget_reservations
            WHERE extraction_job_id = %s
            """,
            (fixture.job.job_semantic.job_id,),
        ).fetchone()[0]
        outbox_rows = connection.execute(
            """
            SELECT count(*) FROM memory_candidate_projection_outbox
            WHERE producer_event_id = %s
            """,
            (fixture.result_event.id,),
        ).fetchone()[0]
    assert budget_rows == 0
    assert outbox_rows == 0

    inspection = InspectorService(PostgresInspectorStore(provider)).inspect_session(
        runtime_session_id
    )
    extraction = inspection["compaction_memory_extraction_durable_status"]
    assert len(extraction) == 1
    assert extraction[0]["status"] == "governed_no_write"
    assert extraction[0]["request"]["event_id"] == fixture.request.id
    assert extraction[0]["result"]["event_id"] == fixture.result_event.id
    assert extraction[0]["result_candidate"]["job_id"] == (
        fixture.job.job_semantic.job_id
    )
    assert extraction[0]["human_evidence_manifest"] == (
        fixture.request.human_evidence_manifest_reference.model_dump(mode="json")
    )
    assert (
        extraction[0]["background_budget_account"]["account_payload"][
            "open_reservation_count"
        ]
        == 0
    )
    assert extraction[0]["model_lifecycle"] == []


def test_result_ready_reclaims_interrupted_settlement_without_rebuilding_candidate(
    tmp_path,
    migrated_postgres_database: MigratedPostgresTestDatabase,
) -> None:
    provider = verified_postgres_provider(migrated_postgres_database.runtime_dsn)
    runtime_session_id = "runtime:compaction-memory-settlement-reclaim"
    fixture = _prepare_no_call_settlement_fixture(
        tmp_path=tmp_path,
        provider=provider,
        runtime_session_id=runtime_session_id,
    )
    original_payload = fixture.result_candidate.model_dump_json()
    first = fixture.repository.claim_compaction_memory_settlements(
        runtime_session_ids=(runtime_session_id,),
        limit=1,
        settlement_attempt_seconds=120.0,
        deadline_monotonic=monotonic() + 20.0,
    )
    assert len(first) == 1
    assert first[0].state.settlement_generation == 1

    reclaimed = fixture.repository.claim_compaction_memory_settlements(
        runtime_session_ids=(runtime_session_id,),
        limit=1,
        reclaim_active_writing=True,
        deadline_monotonic=monotonic() + 20.0,
    )
    assert len(reclaimed) == 1
    assert reclaimed[0].state.settlement_generation == 2
    assert reclaimed[0].result_candidate.model_dump_json() == original_payload

    port = RuntimeSessionCompactionMemoryExtractionSettlementPort(
        runtime_session=fixture.runtime,
        repository=fixture.repository,
        outbox=PostgresCandidateProjectionOutbox(provider),
    )
    try:
        outcome = asyncio.run(
            port.commit_result(
                result_candidate=reclaimed[0].result_candidate,
                write_attempt=build_settlement_write_attempt(
                    result_candidate=reclaimed[0].result_candidate,
                    settlement_generation=reclaimed[0].state.settlement_generation,
                    deadline_monotonic=monotonic() + 20.0,
                ),
            )
        )
        assert outcome.confirmation == "full"
    finally:
        fixture.runtime.close()

    record = fixture.repository.read_job(fixture.job.job_semantic.job_id)
    assert record is not None
    assert record.state.status is DurableProjectionJobStatus.SUCCEEDED
    assert record.state.settlement_generation == 2


def test_close_maintenance_bypasses_settlement_retry_delay_with_same_candidate(
    tmp_path,
    migrated_postgres_database: MigratedPostgresTestDatabase,
) -> None:
    provider = verified_postgres_provider(migrated_postgres_database.runtime_dsn)
    runtime_session_id = "runtime:compaction-memory-settlement-close-retry"
    fixture = _prepare_no_call_settlement_fixture(
        tmp_path=tmp_path,
        provider=provider,
        runtime_session_id=runtime_session_id,
    )
    original_payload = fixture.result_candidate.model_dump_json()
    first = fixture.repository.claim_compaction_memory_settlements(
        runtime_session_ids=(runtime_session_id,),
        limit=1,
        deadline_monotonic=monotonic() + 20.0,
    )
    assert len(first) == 1
    fixture.repository.defer_compaction_memory_settlement(
        result_candidate=fixture.result_candidate,
        settlement_generation=first[0].state.settlement_generation,
        failure=build_bounded_runtime_failure_diagnostic(
            error=RuntimeError("transient settlement failure"),
            redaction_profile_id="durable_projection_job_error.v1",
        ),
        delay_seconds=30.0,
        reconciliation_required=False,
        deadline_monotonic=monotonic() + 20.0,
    )
    assert (
        fixture.repository.claim_compaction_memory_settlements(
            runtime_session_ids=(runtime_session_id,),
            limit=1,
            deadline_monotonic=monotonic() + 20.0,
        )
        == ()
    )

    close_claim = fixture.repository.claim_compaction_memory_settlements(
        runtime_session_ids=(runtime_session_id,),
        limit=1,
        bypass_retry_not_before=True,
        deadline_monotonic=monotonic() + 20.0,
    )
    assert len(close_claim) == 1
    assert close_claim[0].state.settlement_generation == 2
    assert close_claim[0].result_candidate.model_dump_json() == original_payload

    port = RuntimeSessionCompactionMemoryExtractionSettlementPort(
        runtime_session=fixture.runtime,
        repository=fixture.repository,
        outbox=PostgresCandidateProjectionOutbox(provider),
    )
    try:
        outcome = asyncio.run(
            port.commit_result(
                result_candidate=close_claim[0].result_candidate,
                write_attempt=build_settlement_write_attempt(
                    result_candidate=close_claim[0].result_candidate,
                    settlement_generation=close_claim[0].state.settlement_generation,
                    deadline_monotonic=monotonic() + 20.0,
                ),
            )
        )
        assert outcome.confirmation == "full"
    finally:
        fixture.runtime.close()

    record = fixture.repository.read_job(fixture.job.job_semantic.job_id)
    assert record is not None
    assert record.state.status is DurableProjectionJobStatus.SUCCEEDED
    assert record.state.settlement_generation == 2


def test_model_start_and_end_atomically_reserve_and_settle_background_budget(
    tmp_path,
    migrated_postgres_database: MigratedPostgresTestDatabase,
) -> None:
    provider = verified_postgres_provider(migrated_postgres_database.runtime_dsn)
    runtime_session_id = "runtime:compaction-memory-model-lifecycle"
    event_log = PostgresEventLog(
        connection_provider=provider,
        runtime_session_id=runtime_session_id,
        workspace_root=str(tmp_path),
    )
    event_log.ensure_runtime_session_owner()
    runtime = RuntimeSession(
        tmp_path,
        event_log=event_log,
        archive=InMemoryArchiveStore(),
        tool_result_artifacts=FakeToolResultArtifactIndex(),
        runtime_session_id=runtime_session_id,
    )
    context, started, completed, request = _request_pair(runtime_session_id)
    seed = persist_test_run_transcript_seed(runtime, run_id=context.run_id)
    run_start_fields = run_start_permission_fields(
        context.run_id,
        user_input="Remember one stable preference.",
        turn_id=context.turn_id,
        reply_id=context.reply_id,
        mcp_installation_owner_runtime_session_id=runtime_session_id,
    )
    run_start_fields.update(
        run_transcript_seed_semantic=seed.seed_semantic,
        run_transcript_seed_reference=seed.seed_reference,
    )

    async def _commit_source_events() -> None:
        await runtime.write_event(
            RunStartEvent(
                **context.event_fields(),
                **run_start_fields,
                user_input_chars=len("Remember one stable preference."),
                metadata={"user_input": "Remember one stable preference."},
            )
        )
        await runtime.write_events((started, completed, request))

    asyncio.run(_commit_source_events())
    repository = PostgresDurableProjectionRepository(provider)
    job = _seed_one_request(event_log=event_log, repository=repository)
    lease = repository.claim_due_session_model(
        owner_id="worker:model-lifecycle",
        runtime_session_ids=(runtime_session_id,),
        limit=1,
        deadline_monotonic=monotonic() + 20.0,
    )[0]
    call = test_resolved_call_fact(
        purpose=ModelCallPurpose.COMPACTION_MEMORY_EXTRACTION
    )
    quote = calculate_model_call_reservation(
        target=call.target,
        resolved_model_call_id=call.resolved_model_call_id,
        policy=default_rollout_budget_policy(),
    )
    reserve = repository.prepare_background_budget_reservation(
        runtime_session_id=runtime_session_id,
        reservation_id="background-reservation:model-lifecycle",
        extraction_job_id=job.job_semantic.job_id,
        operation_id="operation:model-lifecycle",
        dispatch_attempt_ordinal=1,
        quote=quote,
        deadline_monotonic=monotonic() + 20.0,
    )
    assert reserve.reservation is not None and reserve.failure is None
    reservation = reserve.reservation
    request_source = exact_stored_event(
        event_log=event_log,
        event_id=request.id,
        deadline_monotonic=monotonic() + 20.0,
    )
    request_reference = build_frozen_fact(
        GovernanceStoredEventReferenceFact,
        schema_version="governance_stored_event_reference.v1",
        stable_identity=stable_event_identity(
            request_source.envelope.decode_owned(DEFAULT_EVENT_SCHEMA_REGISTRY),
            runtime_session_id=runtime_session_id,
        ),
        sequence=request_source.envelope.sequence,
        stored_envelope_fingerprint=request_source.envelope.envelope_fingerprint,
    )
    input_reference = build_frozen_fact(
        GovernanceEvidenceArtifactReferenceFact,
        schema_version="governance_evidence_artifact_reference.v1",
        artifact_kind="compaction_memory_extraction_input",
        artifact_id="compaction-memory-extraction-input:model-lifecycle",
        media_type="application/json",
        content_sha256=sha256(b"{}").hexdigest(),
        content_bytes=2,
        artifact_contract_id="pulsara.compaction-memory-extraction.input",
        artifact_contract_version="1",
        artifact_contract_fingerprint=_INPUT_ARTIFACT_CONTRACT_FINGERPRINT,
    )
    input_attribution = build_frozen_fact(
        CompactionMemoryExtractionModelInputAttributionFact,
        schema_version="compaction_memory_extraction_model_input_attribution.v1",
        extraction_job_id=job.job_semantic.job_id,
        dispatch_attempt_ordinal=1,
        request_event_reference=request_reference,
        input_artifact_reference=input_reference,
        input_semantic_fingerprint="sha256:model-lifecycle-input-semantic",
        input_document_fingerprint="sha256:model-lifecycle-input-document",
        resolved_input_budget_attribution_fingerprint=(
            "sha256:model-lifecycle-input-budget"
        ),
        background_budget_reservation=reservation,
        extraction_contract_fingerprint=(
            request.extraction_contract.contract_fingerprint
        ),
    )
    start_fields = model_call_start_fields(
        event_id=f"model_call_start:{call.resolved_model_call_id}",
        context_id="context:compaction-memory-model-lifecycle",
        model_call_index=None,
        resolved_call=call,
        lifecycle_kind="direct_internal_call",
    )
    provider_input = prepared_provider_input_bundle_fixture(
        call,
        context_id="context:compaction-memory-model-lifecycle",
        model_call_index=None,
        event_context=context,
        runtime_session_id=runtime_session_id,
    )
    start_fields["provider_input_reference"] = provider_input.committed_reference
    start = ModelCallStartEvent(
        **context.event_fields(),
        **start_fields,
        compaction_memory_extraction_input_attribution=input_attribution,
    )
    admission = _TestBackgroundAdmissionLease(call.resolved_model_call_id)
    admission.begin_model_start()
    start_companion, terminal_companion = build_model_lifecycle_companions(
        lease=lease,
        reservation=reservation,
        admission_lease=admission,
        model_call_start_event_id=start.id,
        model_call_end_event_id=start.recovery_plan.stable_model_call_end_event_id,
    )
    settlement_outcomes = []

    async def _commit_lifecycle() -> None:
        start_outcome = await runtime.write_events(
            (*provider_input.companion_events, start),
            transaction_companion=start_companion,
        )
        assert start_outcome.committed_events
        admission.confirm_model_start_full()
        stored_start = event_log.get_by_id(start.id)
        assert isinstance(stored_start, ModelCallStartEvent)
        projection_reducer = ModelTerminalProjectionReducer(
            runtime_session_id=runtime_session_id,
            start_event=stored_start,
            contracts=build_default_terminal_projection_contract_bundle(),
            model_stream_semantic_domain_contract_fingerprint=context_fingerprint(
                "test-compaction-memory-model-stream-domain:v1",
                call.resolved_model_call_id,
            ),
            segment_policy_contract_fingerprint=(
                DEFAULT_MODEL_STREAM_SEGMENT_POLICY_CONTRACT.contract_fingerprint
            ),
        )
        usage = ModelTokenUsageFact(
            input_tokens=32,
            output_tokens=16,
            total_tokens=48,
        )
        terminal_projection = bind_model_terminal_projection_to_session(
            runtime,
            projection_reducer.prepare_terminal(
                event_context=context,
                terminal_outcome="completed",
                usage_report=TransportUsageReport(
                    usage_status="reported",
                    usage=usage,
                    reported_model_id=call.target.model_id,
                ),
                physical_accounting_mode="accounted",
            ),
        )
        await persist_model_terminal_projection(
            runtime,
            terminal_projection,
            run_id=context.run_id,
        )
        runtime.transcript_projection_document_registry.register(
            terminal_projection.projection_reference,
            terminal_projection.document,
        )
        end_fields = model_call_end_fields(
            input_tokens=32,
            output_tokens=16,
            resolved_call=call,
        )
        end_fields["terminal_projection"] = terminal_projection.end_reference
        end = ModelCallEndEvent(
            id=start.recovery_plan.stable_model_call_end_event_id,
            **context.event_fields(),
            **end_fields,
        )
        end_outcome = await runtime.write_events(
            (
                terminal_projection.committed_event,
                end,
                build_one_shot_generation_close_event(
                    bundle=provider_input,
                    event_context=context,
                    created_at=end.created_at,
                ),
            ),
            transaction_companion=terminal_companion,
        )
        assert end_outcome.committed_events
        assert terminal_companion.settlement is not None

        stored_start = event_log.get_by_id(start.id)
        stored_end = event_log.get_by_id(end.id)
        assert isinstance(stored_start, ModelCallStartEvent)
        assert isinstance(stored_end, ModelCallEndEvent)
        resolved_budget = build_frozen_fact(
            ResolvedExtractionInputBudgetAttributionFact,
            schema_version="resolved_extraction_input_budget_attribution.v1",
            resolved_model_target_fingerprint=call.target.target_fingerprint,
            target_input_limit_tokens=4_096,
            static_prompt_tokens=128,
            carrier_and_framing_reserve_tokens=64,
            output_reserve_tokens=256,
            safety_margin_tokens=32,
            usable_evidence_tokens=3_616,
            maximum_physical_input_utf8_bytes=512 * 1024,
            token_estimator_contract_fingerprint="sha256:test-token-estimator",
            budget_selection_contract_fingerprint="sha256:test-budget-selection",
        )
        result_semantic = build_frozen_fact(
            CompactionMemoryValidEmptyResultSemanticFact,
            schema_version="compaction_memory_valid_empty_result_semantic.v1",
            outcome_kind="valid_empty",
            input_semantic_fingerprint=input_attribution.input_semantic_fingerprint,
            evidence_set_semantic_fingerprint=(
                CANONICAL_EMPTY_COMPACTION_MEMORY_EVIDENCE_SET_FINGERPRINT
            ),
            terminal_projection_semantic_fingerprint=(
                stored_end.terminal_projection.projection_reference.semantic_join.semantic_fingerprint
            ),
            parser_contract_fingerprint="sha256:test-parser-contract",
            extraction_semantic_contract_fingerprint=(
                "sha256:test-extraction-semantic-contract"
            ),
        )
        result_attribution = build_frozen_fact(
            CompactionMemoryModelResultAttributionFact,
            schema_version="compaction_memory_model_result_attribution.v1",
            outcome_kind="valid_empty",
            input_artifact_reference=input_reference,
            input_semantic_fingerprint=input_attribution.input_semantic_fingerprint,
            resolved_input_budget=resolved_budget,
            model_call_start_event_reference=_governance_event_reference(
                event_log=event_log,
                runtime_session_id=runtime_session_id,
                event_id=stored_start.id,
            ),
            model_call_end_event_reference=_governance_event_reference(
                event_log=event_log,
                runtime_session_id=runtime_session_id,
                event_id=stored_end.id,
            ),
            model_terminal_projection_reference=(
                stored_end.terminal_projection.projection_reference
            ),
            parsed_output_semantic_fingerprint="sha256:test-parsed-output",
            dispatch_attempt_ordinal=1,
            background_budget_reservation=reservation,
            background_budget_settlement=terminal_companion.settlement,
        )
        occurrence = build_frozen_fact(
            CompactionMemoryExtractionOccurrenceAttributionFact,
            schema_version="compaction_memory_extraction_occurrence_attribution.v1",
            compaction_id=request.extension_link.compaction_id,
            extension_link=request.extension_link,
            request_event_reference=request_reference,
            durable_job_id=lease.job.job_id,
            durable_job_source_reference=lease.job.source_event_reference,
            human_evidence_manifest_reference=(
                request.human_evidence_manifest_reference
            ),
            outcome_attribution=result_attribution,
        )
        result_event = build_extraction_completed_event(
            runtime_session_id=runtime_session_id,
            event_context=context,
            created_at_utc=stored_end.created_at,
            lease=lease,
            result_semantic=result_semantic,
            occurrence_attribution=occurrence,
            candidate_attributions=(),
        )
        empty_accumulator = context_fingerprint(
            "compaction-memory-permanent-omission:v1:empty", ()
        )
        result_candidate = build_result_candidate(
            runtime_session_id=runtime_session_id,
            lease=lease,
            event=result_event,
            intended_target_head_revision=1,
            expected_target_head_fingerprint=None,
            permanent_automatic_omission_count=0,
            permanent_automatic_omission_semantic_accumulator=empty_accumulator,
            permanent_automatic_omission_attribution_accumulator=empty_accumulator,
        )
        installation_guard = (
            repository.prepare_compaction_memory_result_installation_guard(
                lease=lease,
                result_candidate=result_candidate,
                deadline_monotonic=monotonic() + 20.0,
            )
        )
        assert (
            repository.install_compaction_memory_result_candidate(
                lease=lease,
                result_candidate=result_candidate,
                installation_guard=installation_guard,
                deadline_monotonic=monotonic() + 20.0,
            )
            is DurableProjectionCommitConfirmation.FULL
        )
        claimed = repository.claim_compaction_memory_settlements(
            runtime_session_ids=(runtime_session_id,),
            limit=1,
            deadline_monotonic=monotonic() + 20.0,
        )
        assert len(claimed) == 1
        settlement_port = RuntimeSessionCompactionMemoryExtractionSettlementPort(
            runtime_session=runtime,
            repository=repository,
            outbox=PostgresCandidateProjectionOutbox(provider),
        )
        settlement_outcomes.append(
            await settlement_port.commit_result(
                result_candidate=result_candidate,
                write_attempt=build_settlement_write_attempt(
                    result_candidate=result_candidate,
                    settlement_generation=claimed[0].state.settlement_generation,
                    deadline_monotonic=monotonic() + 20.0,
                ),
            )
        )

    try:
        asyncio.run(_commit_lifecycle())
    finally:
        runtime.close()

    state = repository.read_job(job.job_semantic.job_id)
    assert state is not None
    assert state.state.dispatch_attempt_count == 1
    assert terminal_companion.settlement is not None
    assert len(settlement_outcomes) == 1
    assert settlement_outcomes[0].confirmation == "full"
    with connect_postgres_test_database(
        migrated_postgres_database.admin_dsn
    ) as connection:
        account = connection.execute(
            """
            SELECT account_revision, account_payload
            FROM background_derived_work_budget_accounts
            WHERE runtime_session_id = %s
            """,
            (runtime_session_id,),
        ).fetchone()
        reservation_row = connection.execute(
            """
            SELECT status, reservation_fingerprint
            FROM background_derived_work_budget_reservations
            WHERE reservation_id = %s
            """,
            (reservation.reservation_id,),
        ).fetchone()
        settlement_row = connection.execute(
            """
            SELECT settlement_fingerprint
            FROM background_derived_work_budget_settlements
            WHERE reservation_id = %s
            """,
            (reservation.reservation_id,),
        ).fetchone()
    assert account is not None
    assert account[0] == 2
    assert account[1]["open_reservation_count"] == 0
    assert account[1]["settled_call_count"] == 1
    assert reservation_row == ("settled", reservation.reservation_fingerprint)
    assert settlement_row == (terminal_companion.settlement.settlement_fingerprint,)
    bootstrap = PostgresRuntimeSessionOwnerBootstrapPort(provider)
    bootstrap_outcome = bootstrap.bootstrap(
        candidate=bootstrap.candidate(
            runtime_session_id=runtime_session_id,
            workspace_root=str(tmp_path),
        ),
        deadline_monotonic=monotonic() + 20.0,
    )
    assert (
        bootstrap_outcome.confirmation
        is DurableProjectionCommitConfirmation.FULL
    )
    assert bootstrap_outcome.resulting_state is not None
    assert (
        bootstrap_outcome.resulting_state.background_budget_account_fingerprint
        == account[1]["account_fingerprint"]
    )
