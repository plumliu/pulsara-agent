from __future__ import annotations

from tests.support.postgres import verified_postgres_provider

from dataclasses import dataclass
from uuid import uuid4

import pytest

from tests.support.postgres import connect_postgres_test_database as _connect_or_skip

from tests.support.model_stream import (
    make_text_block_segment_event,
)
from tests.support.governance import make_test_governance_decision_record

from pulsara_agent.event import EventContext
from pulsara_agent.event.candidates import (
    InvalidAttemptPayload,
    PreferenceCandidate,
    ValidCandidatePayload,
)
from pulsara_agent.event_log import PostgresEventLog
from pulsara_agent.memory import (
    MemoryWriteUnitOfWork,
    NoWriteOutcome,
    PooledMemoryCandidate,
    PostgresArtifactStore,
    PostgresCandidatePool,
    SkipDecision,
    SubmitAsIsDecision,
    WriteFailedOutcome,
    WriteSucceededOutcome,
)
from pulsara_agent.memory.candidates.pool import (
    CandidateOrigin,
    CandidatePool,
    CandidatePoolProposal,
)
from pulsara_agent.ontology import memory
from pulsara_agent.settings import StorageConfig


@dataclass(frozen=True, slots=True)
class _PoolCase:
    pool: CandidatePool
    session_id: str
    ctx: EventContext


@pytest.fixture
def pool_case(request, tmp_path) -> _PoolCase:
    dsn = StorageConfig.from_env().postgres_dsn
    session_id = f"runtime:test:{uuid4().hex}"
    ctx = _ctx("postgres")
    _connect_or_skip(dsn).close()
    log = PostgresEventLog(
        connection_provider=verified_postgres_provider(dsn),
        runtime_session_id=session_id,
        workspace_root=tmp_path,
    )
    log.append(
        make_text_block_segment_event(
            **ctx.event_fields(), block_id="text:parent", delta="seed"
        )
    )
    pool = PostgresCandidatePool(connection_provider=verified_postgres_provider(dsn))

    def cleanup() -> None:
        with _connect_or_skip(dsn) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    "delete from memory_governance_decisions where governance_batch_id like %s",
                    ("governance:test:%",),
                )
                cursor.execute("delete from sessions where id = %s", (session_id,))

    request.addfinalizer(cleanup)
    return _PoolCase(pool=pool, session_id=session_id, ctx=ctx)


def test_candidate_pool_round_trips_valid_and_invalid_payloads(
    pool_case: _PoolCase,
) -> None:
    valid = _pooled_valid(pool_case, entry_id=f"pool:test:{uuid4().hex}")
    invalid = _pooled_invalid(pool_case, entry_id=f"pool:test:{uuid4().hex}")

    pool_case.pool.append_candidate(valid)
    pool_case.pool.append_candidate(invalid)

    pending = _pending_for_case(pool_case)
    assert [candidate.entry_id for candidate in pending] == [
        valid.entry_id,
        invalid.entry_id,
    ]
    assert isinstance(
        pool_case.pool.get_candidate(valid.entry_id).payload, ValidCandidatePayload
    )
    assert isinstance(
        pool_case.pool.get_candidate(invalid.entry_id).payload, InvalidAttemptPayload
    )


def test_skip_decision_terminally_removes_candidate_from_pending(
    pool_case: _PoolCase,
) -> None:
    candidate = pool_case.pool.append_candidate(
        _pooled_valid(pool_case, entry_id=f"pool:test:{uuid4().hex}")
    )
    governance_batch_id = f"governance:test:{uuid4().hex}"
    decision = SkipDecision(
        target_entry_ids=(candidate.entry_id,),
        reason="not durable",
        skip_reason="not_durable",
    )
    pool_case.pool.append_decision(
        make_test_governance_decision_record(
            governance_batch_id=governance_batch_id,
            decision=decision,
            write_outcome=NoWriteOutcome(),
            candidates=(candidate,),
        )
    )

    assert _pending_for_case(pool_case) == []


def test_system_evidence_rejection_terminally_removes_candidate_from_pending(
    pool_case: _PoolCase,
) -> None:
    candidate = pool_case.pool.append_candidate(
        _pooled_valid(pool_case, entry_id=f"pool:test:{uuid4().hex}")
    )
    event_id = f"memory_candidate:{candidate.entry_id}:evidence_rejected:1"

    pool_case.pool.mark_evidence_rejected(
        entry_id=candidate.entry_id,
        rejection_event_id=event_id,
    )
    pool_case.pool.mark_evidence_rejected(
        entry_id=candidate.entry_id,
        rejection_event_id=event_id,
    )

    assert pool_case.pool.evidence_rejection_event_id(candidate.entry_id) == event_id
    assert _pending_for_case(pool_case) == []


def test_write_failed_decision_does_not_terminally_remove_candidate(
    pool_case: _PoolCase,
) -> None:
    candidate = pool_case.pool.append_candidate(
        _pooled_valid(pool_case, entry_id=f"pool:test:{uuid4().hex}")
    )
    governance_batch_id = f"governance:test:{uuid4().hex}"
    decision = SubmitAsIsDecision(
        target_entry_id=candidate.entry_id,
        reason="try write",
    )
    pool_case.pool.append_decision(
        make_test_governance_decision_record(
            governance_batch_id=governance_batch_id,
            decision=decision,
            write_outcome=WriteFailedOutcome(
                error_type="RuntimeError",
                message="temporary store failure",
                write_event_ids=("event:failed",),
            ),
            candidates=(candidate,),
        )
    )

    assert [pending.entry_id for pending in _pending_for_case(pool_case)] == [
        candidate.entry_id
    ]


def test_write_succeeded_decision_terminally_removes_candidate(
    pool_case: _PoolCase,
) -> None:
    candidate = pool_case.pool.append_candidate(
        _pooled_valid(pool_case, entry_id=f"pool:test:{uuid4().hex}")
    )
    governance_batch_id = f"governance:test:{uuid4().hex}"
    decision = SubmitAsIsDecision(
        target_entry_id=candidate.entry_id,
        reason="write",
    )
    pool_case.pool.append_decision(
        make_test_governance_decision_record(
            governance_batch_id=governance_batch_id,
            decision=decision,
            write_outcome=WriteSucceededOutcome(
                memory_id="preference:test",
                memory_type="Preference",
                node_status=memory.NodeStatus.ACTIVE,
                confidence_level=memory.ConfidenceLevel.HIGH,
                verification_status=memory.VerificationStatus.USER_CONFIRMED,
                gate_reason="ok",
                write_event_ids=("event:result",),
            ),
            candidates=(candidate,),
        )
    )

    assert _pending_for_case(pool_case) == []


def test_governance_origin_candidates_are_audit_rows_not_pending(
    pool_case: _PoolCase,
) -> None:
    candidate = _pooled_valid(
        pool_case,
        entry_id=f"pool:test:{uuid4().hex}",
        origin=CandidateOrigin.GOVERNANCE,
    )

    pool_case.pool.append_candidate(candidate)

    assert [
        item.entry_id
        for item in pool_case.pool.list_candidates()
        if item.source_session_id == pool_case.session_id
    ] == [candidate.entry_id]
    assert _pending_for_case(pool_case) == []


def test_compaction_origin_candidate_round_trips_provenance(
    pool_case: _PoolCase,
) -> None:
    candidate = _pooled_valid(
        pool_case,
        entry_id=f"pool:test:{uuid4().hex}",
        origin=CandidateOrigin.COMPACTION,
    ).model_copy(
        update={
            "source_event_id": "event:context-compaction-completed",
            "source_artifact_id": "context_compaction:test:summary",
            "intent_fingerprint": "sha256:test-intent",
            "metadata": {
                "source": "context_compaction",
                "compaction_id": "context_compaction:test",
                "summary_artifact_id": "context_compaction:test:summary",
                "summary_excerpt": "The user repeatedly asks to sync release before pushing.",
                "included_run_ids": [pool_case.ctx.run_id],
                "included_run_count": 1,
            },
        }
    )

    pool_case.pool.append_candidate(candidate)

    fetched = pool_case.pool.get_candidate(candidate.entry_id)
    assert fetched.origin is CandidateOrigin.COMPACTION
    assert fetched.source_event_id == "event:context-compaction-completed"
    assert fetched.source_artifact_id == "context_compaction:test:summary"
    assert fetched.intent_fingerprint == "sha256:test-intent"
    assert fetched.metadata["compaction_id"] == "context_compaction:test"
    assert fetched.metadata["summary_excerpt"].startswith("The user repeatedly asks")
    assert [pending.entry_id for pending in _pending_for_case(pool_case)] == [
        candidate.entry_id
    ]


def test_candidate_pool_proposal_preserves_provenance_metadata(
    pool_case: _PoolCase,
) -> None:
    proposal = CandidatePoolProposal(
        payload=ValidCandidatePayload(
            candidate=_preference(f"candidate:test:{uuid4().hex}")
        ),
        origin=CandidateOrigin.COMPACTION,
        source_event_id="event:context-compaction-completed",
        source_artifact_id="context_compaction:test:summary",
        intent_fingerprint="sha256:test-intent",
        metadata={
            "compaction_id": "context_compaction:test",
            "summary_excerpt": "bounded",
        },
    )

    pooled = proposal.to_pooled(
        source_session_id=pool_case.session_id,
        source_run_id=pool_case.ctx.run_id,
        source_turn_id=pool_case.ctx.turn_id,
        source_reply_id=pool_case.ctx.reply_id,
    )

    assert pooled.origin is CandidateOrigin.COMPACTION
    assert pooled.source_event_id == proposal.source_event_id
    assert pooled.source_artifact_id == proposal.source_artifact_id
    assert pooled.intent_fingerprint == proposal.intent_fingerprint
    assert pooled.metadata == proposal.metadata


def test_memory_write_unit_of_work_preserves_compaction_candidate_metadata(
    tmp_path,
) -> None:
    dsn = StorageConfig.from_env().postgres_dsn
    runtime_session_id = f"runtime:test:{uuid4().hex}"
    ctx = _ctx("uow")
    _connect_or_skip(dsn).close()
    log = PostgresEventLog(
        connection_provider=verified_postgres_provider(dsn),
        runtime_session_id=runtime_session_id,
        workspace_root=tmp_path,
    )
    log.append(
        make_text_block_segment_event(
            **ctx.event_fields(), block_id="text:seed", delta="seed"
        )
    )
    candidate = _pooled_valid(
        _PoolCase(
            pool=PostgresCandidatePool(verified_postgres_provider(dsn)),
            session_id=runtime_session_id,
            ctx=ctx,
        ),
        entry_id=f"pool:test:{uuid4().hex}",
        origin=CandidateOrigin.COMPACTION,
    ).model_copy(
        update={
            "source_event_id": "event:context-compaction-completed",
            "source_artifact_id": "context_compaction:test:summary",
            "intent_fingerprint": "sha256:uow-test",
            "metadata": {
                "compaction_id": "context_compaction:uow",
                "summary_excerpt": "bounded",
            },
        }
    )
    try:
        with MemoryWriteUnitOfWork(
            connection_provider=verified_postgres_provider(dsn),
            runtime_session_id=runtime_session_id,
            archive=PostgresArtifactStore(verified_postgres_provider(dsn)),
        ) as uow:
            uow.decisions.append_candidate(candidate)

        fetched = PostgresCandidatePool(verified_postgres_provider(dsn)).get_candidate(
            candidate.entry_id
        )

        assert fetched.origin is CandidateOrigin.COMPACTION
        assert fetched.source_event_id == candidate.source_event_id
        assert fetched.source_artifact_id == candidate.source_artifact_id
        assert fetched.intent_fingerprint == candidate.intent_fingerprint
        assert fetched.metadata == candidate.metadata
    finally:
        with _connect_or_skip(dsn) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    "delete from memory_candidates where entry_id = %s",
                    (candidate.entry_id,),
                )
                cursor.execute(
                    "delete from sessions where id = %s", (runtime_session_id,)
                )


def _preference(candidate_id: str) -> PreferenceCandidate:
    return PreferenceCandidate(
        candidate_id=candidate_id,
        statement="The user prefers concise summaries.",
        scope="ctx:user",
        source_authority=memory.SourceAuthority.EXPLICIT_USER_INSTRUCTION,
        verification_status=memory.VerificationStatus.USER_CONFIRMED,
    )


def _pending_for_case(pool_case: _PoolCase) -> list[PooledMemoryCandidate]:
    return [
        candidate
        for candidate in pool_case.pool.list_pending()
        if candidate.source_session_id == pool_case.session_id
    ]


def _pooled_valid(
    pool_case: _PoolCase,
    *,
    entry_id: str,
    origin: CandidateOrigin = CandidateOrigin.MAIN_AGENT_TOOL,
) -> PooledMemoryCandidate:
    return PooledMemoryCandidate(
        entry_id=entry_id,
        payload=ValidCandidatePayload(
            candidate=_preference(f"candidate:test:{uuid4().hex}")
        ),
        origin=origin,
        source_session_id=pool_case.session_id,
        source_run_id=pool_case.ctx.run_id,
        source_turn_id=pool_case.ctx.turn_id,
        source_reply_id=pool_case.ctx.reply_id,
    )


def _pooled_invalid(pool_case: _PoolCase, *, entry_id: str) -> PooledMemoryCandidate:
    return PooledMemoryCandidate(
        entry_id=entry_id,
        payload=InvalidAttemptPayload(
            attempted_tool_name="remember_action_boundary",
            attempted_kind="ActionBoundary",
            raw_arguments={"statement": "Do not commit unless asked."},
            validation_error="missing do_not_apply_when",
        ),
        origin=CandidateOrigin.MAIN_AGENT_TOOL,
        source_session_id=pool_case.session_id,
        source_run_id=pool_case.ctx.run_id,
        source_turn_id=pool_case.ctx.turn_id,
        source_reply_id=pool_case.ctx.reply_id,
        source_tool_call_id="call:bad",
    )


def _ctx(label: str) -> EventContext:
    suffix = uuid4().hex
    return EventContext(
        run_id=f"run:candidate-pool:{label}:{suffix}",
        turn_id=f"turn:candidate-pool:{label}:{suffix}",
        reply_id=f"reply:candidate-pool:{label}:{suffix}",
    )
