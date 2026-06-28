from __future__ import annotations

from uuid import uuid4

import psycopg
import pytest

from pulsara_agent.event import EventContext, EventType, TextBlockDeltaEvent
from pulsara_agent.event.candidates import InvalidAttemptPayload, PreferenceCandidate, ValidCandidatePayload
from pulsara_agent.event_log import InMemoryEventLog, PostgresEventLog
from pulsara_agent.graph import InMemoryGraphStore
from pulsara_agent.message.assembler import BlockAssembler
from pulsara_agent.memory import (
    InMemoryArchiveStore,
    InMemoryCandidatePool,
    MemoryGovernanceExecutor,
    MemoryWriteUnitOfWork,
    PooledMemoryCandidate,
    PostgresCandidatePool,
    PostgresMemoryQuery,
    SubmitAsIsDecision,
    CorrectAndSubmitDecision,
)
from pulsara_agent.memory.candidates.pool import CandidateOrigin, WriteFailedOutcome, WriteSucceededOutcome
from pulsara_agent.memory.governance.dedupe import already_exists
from pulsara_agent.memory.canonical.ledger import ExecutionEvidenceLedger
from pulsara_agent.memory.canonical.write_gate import MemoryWriteGate
from pulsara_agent.memory.canonical.write_service import MemoryWriteService
from pulsara_agent.ontology import memory
from pulsara_agent.settings import StorageConfig


def test_governance_submit_as_is_writes_with_synthetic_context_and_resolves_pending() -> None:
    graph = InMemoryGraphStore()
    pool = InMemoryCandidatePool()
    log = InMemoryEventLog()
    candidate = pool.append_candidate(_pooled_valid())
    executor = _executor(pool=pool, graph=graph, log=log)

    result = executor.apply_decision(
        SubmitAsIsDecision(target_entry_id=candidate.entry_id, reason="valid durable preference"),
        governance_batch_id="governance:test:submit",
    )

    assert [event.type for event in result.events] == [
        EventType.MEMORY_CANDIDATE_PROPOSED,
        EventType.MEMORY_WRITE_RESULT,
    ]
    assert all(event.run_id == "run:governance/governance:test:submit" for event in result.events)
    assert log.iter(run_id=candidate.source_run_id) == []
    assert len(log.iter(run_id="run:governance/governance:test:submit")) == 2
    assert isinstance(result.decision_record.write_outcome, WriteSucceededOutcome)
    assert graph.has_jsonld(result.decision_record.write_outcome.memory_id)
    assert pool.list_pending() == []


def test_governance_synthetic_reply_does_not_replay_as_assistant_content() -> None:
    graph = InMemoryGraphStore()
    pool = InMemoryCandidatePool()
    log = InMemoryEventLog()
    candidate = pool.append_candidate(_pooled_valid())
    executor = _executor(pool=pool, graph=graph, log=log)

    result = executor.apply_decision(
        SubmitAsIsDecision(target_entry_id=candidate.entry_id, reason="valid durable preference"),
        governance_batch_id="governance:test:replay",
    )

    assembler = BlockAssembler()
    for event in result.events:
        update = assembler.append(event)
        assert update.started == []
        assert update.completed == []
    assert assembler.active_count() == 0

    replayed = log.replay("reply:governance/governance:test:replay")
    assert replayed.content == []
    assert log.iter(run_id=candidate.source_run_id) == []


def test_governance_duplicate_existing_memory_records_skip_without_write_events() -> None:
    graph = InMemoryGraphStore()
    pool = InMemoryCandidatePool()
    log = InMemoryEventLog()
    service = _service_on(graph)
    service.submit(
        _preference("candidate:existing"),
        event_context=EventContext(run_id="run:old", turn_id="turn:old", reply_id="reply:old"),
    )
    candidate = pool.append_candidate(_pooled_valid())
    executor = _executor(pool=pool, graph=graph, log=log)

    result = executor.apply_decision(
        SubmitAsIsDecision(target_entry_id=candidate.entry_id, reason="try duplicate"),
        governance_batch_id="governance:test:duplicate",
    )

    assert result.events == []
    assert result.decision_record.decision.kind == "skip"
    assert result.decision_record.write_outcome.kind == "no_write"
    assert pool.list_pending() == []
    assert len(graph.find_by_type(memory.PREFERENCE)) == 1


def test_governance_write_failed_does_not_terminally_remove_candidate() -> None:
    graph = InMemoryGraphStore()
    pool = InMemoryCandidatePool()
    log = InMemoryEventLog()
    candidate = pool.append_candidate(
        _pooled_valid(
            candidate=_preference("candidate:missing-evidence", evidence_ids=("evidence:missing",)),
        )
    )
    executor = _executor(pool=pool, graph=graph, log=log)

    result = executor.apply_decision(
        SubmitAsIsDecision(target_entry_id=candidate.entry_id, reason="try missing evidence"),
        governance_batch_id="governance:test:failed",
    )

    assert [event.type for event in result.events] == [
        EventType.MEMORY_CANDIDATE_PROPOSED,
        EventType.MEMORY_WRITE_FAILED,
    ]
    assert isinstance(result.decision_record.write_outcome, WriteFailedOutcome)
    assert [pending.entry_id for pending in pool.list_pending()] == [candidate.entry_id]
    assert graph.find_by_type(memory.PREFERENCE) == []


def test_governance_validates_correct_target_before_writing() -> None:
    graph = InMemoryGraphStore()
    pool = InMemoryCandidatePool()
    log = InMemoryEventLog()
    executor = _executor(pool=pool, graph=graph, log=log)

    with pytest.raises(KeyError):
        executor.apply_decision(
            CorrectAndSubmitDecision(
                target_entry_id="pool:missing",
                candidate=_preference("candidate:corrected"),
                reason="bad governance output",
            ),
            governance_batch_id="governance:test:missing-target",
        )

    assert log.iter(run_id="run:governance/governance:test:missing-target") == []
    assert graph.find_by_type(memory.PREFERENCE) == []
    assert pool.list_decisions() == []


def test_governance_invalid_attempt_is_skipped_by_submit_pending_as_is() -> None:
    graph = InMemoryGraphStore()
    pool = InMemoryCandidatePool()
    log = InMemoryEventLog()
    candidate = pool.append_candidate(_pooled_invalid())
    executor = _executor(pool=pool, graph=graph, log=log)

    results = executor.submit_pending_as_is(governance_batch_id="governance:test:invalid")

    assert len(results) == 1
    assert results[0].events == []
    assert results[0].decision_record.decision.kind == "skip"
    assert results[0].decision_record.decision.target_entry_ids == (candidate.entry_id,)
    assert pool.list_pending() == []


def test_governance_limit_applies_after_runtime_session_filter() -> None:
    graph = InMemoryGraphStore()
    pool = InMemoryCandidatePool()
    log = InMemoryEventLog()
    other_session = _pooled_valid()
    other_session = other_session.model_copy(update={"source_session_id": "runtime:other"})
    pool.append_candidate(other_session)
    current_session = pool.append_candidate(_pooled_valid())
    executor = _executor(pool=pool, graph=graph, log=log)

    results = executor.submit_pending_as_is(limit=1, governance_batch_id="governance:test:limit")

    assert len(results) == 1
    assert results[0].decision_record.decision.target_entry_id == current_session.entry_id
    assert [candidate.source_session_id for candidate in pool.list_pending()] == ["runtime:other"]


def test_governance_skips_candidate_outside_allowed_write_scopes() -> None:
    graph = InMemoryGraphStore()
    pool = InMemoryCandidatePool()
    log = InMemoryEventLog()
    candidate = pool.append_candidate(
        _pooled_valid(
            candidate=_preference(
                "candidate:workspace-out-of-scope",
                scope="ctx:workspace/test_project",
            )
        )
    )
    executor = _executor(pool=pool, graph=graph, log=log, allowed_write_scopes=frozenset({"ctx:user"}))

    result = executor.apply_decision(
        SubmitAsIsDecision(target_entry_id=candidate.entry_id, reason="try workspace"),
        governance_batch_id="governance:test:out-of-scope",
    )

    assert result.events == []
    assert result.decision_record.decision.kind == "skip"
    assert result.decision_record.decision.skip_reason == "scope_not_allowed"
    assert "ctx:workspace/test_project" in result.decision_record.decision.reason
    assert graph.find_by_type(memory.PREFERENCE) == []


def test_governance_rejects_cross_runtime_target_entry() -> None:
    graph = InMemoryGraphStore()
    pool = InMemoryCandidatePool()
    log = InMemoryEventLog()
    candidate = pool.append_candidate(
        _pooled_valid().model_copy(update={"source_session_id": "runtime:other"})
    )
    executor = _executor(pool=pool, graph=graph, log=log)

    with pytest.raises(ValueError, match="another runtime"):
        executor.apply_decision(
            SubmitAsIsDecision(target_entry_id=candidate.entry_id, reason="wrong runtime"),
            governance_batch_id="governance:test:wrong-runtime",
        )

    assert graph.find_by_type(memory.PREFERENCE) == []
    assert pool.list_decisions() == []


def test_governance_correct_and_submit_adds_governance_audit_candidate_without_pending_residue() -> None:
    graph = InMemoryGraphStore()
    pool = InMemoryCandidatePool()
    log = InMemoryEventLog()
    invalid = pool.append_candidate(_pooled_invalid())
    corrected = _preference("candidate:corrected")
    executor = _executor(pool=pool, graph=graph, log=log)

    result = executor.apply_decision(
        CorrectAndSubmitDecision(
            target_entry_id=invalid.entry_id,
            candidate=corrected,
            reason="Correct invalid main-agent attempt.",
        ),
        governance_batch_id="governance:test:correct",
    )

    assert isinstance(result.decision_record.write_outcome, WriteSucceededOutcome)
    all_candidates = pool.list_candidates()
    assert len(all_candidates) == 2
    governance_candidates = [candidate for candidate in all_candidates if candidate.origin == CandidateOrigin.GOVERNANCE]
    assert len(governance_candidates) == 1
    assert pool.list_pending() == []
    assert len(graph.find_by_type(memory.PREFERENCE)) == 1


def test_postgres_governance_correct_and_submit_has_valid_governance_candidate_fk_chain(tmp_path) -> None:
    dsn = StorageConfig.from_env().postgres_dsn
    _connect_or_skip(dsn).close()
    runtime_session_id = f"runtime:test:{uuid4().hex}"
    source_ctx = EventContext(
        run_id=f"run:source:{uuid4().hex}",
        turn_id=f"turn:source:{uuid4().hex}",
        reply_id=f"reply:source:{uuid4().hex}",
    )
    batch_id = f"governance:test:postgres-correct:{uuid4().hex}"
    log = PostgresEventLog(dsn=dsn, runtime_session_id=runtime_session_id, workspace_root=tmp_path)
    log.append(TextBlockDeltaEvent(**source_ctx.event_fields(), block_id="text:seed", delta="seed"))
    pool = PostgresCandidatePool(dsn=dsn)
    graph = InMemoryGraphStore()
    try:
        invalid = pool.append_candidate(
            PooledMemoryCandidate(
                payload=InvalidAttemptPayload(
                    attempted_tool_name="remember_action_boundary",
                    attempted_kind="ActionBoundary",
                    raw_arguments={"statement": "Never commit unless explicitly asked."},
                    validation_error="missing do_not_apply_when",
                ),
                origin=CandidateOrigin.MAIN_AGENT_TOOL,
                source_session_id=runtime_session_id,
                source_run_id=source_ctx.run_id,
                source_turn_id=source_ctx.turn_id,
                source_reply_id=source_ctx.reply_id,
            )
        )
        executor = MemoryGovernanceExecutor(
            candidate_pool=pool,
            memory_write_service=_service_on(graph),
            event_log=log,
            graph=graph,
            runtime_session_id=runtime_session_id,
        )

        result = executor.apply_decision(
            CorrectAndSubmitDecision(
                target_entry_id=invalid.entry_id,
                candidate=_preference("candidate:postgres-corrected"),
                reason="Correct invalid attempt from user quote.",
            ),
            governance_batch_id=batch_id,
        )

        assert isinstance(result.decision_record.write_outcome, WriteSucceededOutcome)
        governance_candidates = [
            candidate for candidate in pool.list_candidates() if candidate.origin is CandidateOrigin.GOVERNANCE
        ]
        assert len(governance_candidates) == 1
        assert governance_candidates[0].source_session_id == runtime_session_id
        assert governance_candidates[0].source_run_id == f"run:governance/{batch_id}"
        assert governance_candidates[0].source_turn_id == f"turn:governance/{batch_id}"
        assert len(log.iter(run_id=f"run:governance/{batch_id}")) == 2
        assert pool.list_pending() == []
    finally:
        with _connect_or_skip(dsn) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    "delete from memory_governance_decisions where governance_batch_id = %s",
                    (batch_id,),
                )
                cursor.execute("delete from sessions where id = %s", (runtime_session_id,))


def test_postgres_governance_uow_writes_graph_decision_outbox_and_audit_candidate(tmp_path) -> None:
    dsn = StorageConfig.from_env().postgres_dsn
    _connect_or_skip(dsn).close()
    runtime_session_id = f"runtime:test:{uuid4().hex}"
    graph_id = f"graph:test/{uuid4().hex}"
    source_ctx = EventContext(
        run_id=f"run:source:{uuid4().hex}",
        turn_id=f"turn:source:{uuid4().hex}",
        reply_id=f"reply:source:{uuid4().hex}",
    )
    batch_id = f"governance:test:uow:{uuid4().hex}"
    log = PostgresEventLog(dsn=dsn, runtime_session_id=runtime_session_id, workspace_root=tmp_path)
    log.append(TextBlockDeltaEvent(**source_ctx.event_fields(), block_id="text:seed", delta="seed"))
    pool = PostgresCandidatePool(dsn=dsn)
    query = PostgresMemoryQuery(dsn=dsn)
    try:
        invalid = pool.append_candidate(
            PooledMemoryCandidate(
                payload=InvalidAttemptPayload(
                    attempted_tool_name="remember_action_boundary",
                    attempted_kind="ActionBoundary",
                    raw_arguments={"statement": "Never commit unless explicitly asked."},
                    validation_error="missing do_not_apply_when",
                ),
                origin=CandidateOrigin.MAIN_AGENT_TOOL,
                source_session_id=runtime_session_id,
                source_run_id=source_ctx.run_id,
                source_turn_id=source_ctx.turn_id,
                source_reply_id=source_ctx.reply_id,
            )
        )
        executor = MemoryGovernanceExecutor(
            candidate_pool=pool,
            memory_write_service=_service_on(InMemoryGraphStore()),
            event_log=log,
            graph=InMemoryGraphStore(),
            runtime_session_id=runtime_session_id,
            memory_write_uow_factory=lambda: MemoryWriteUnitOfWork(
                dsn=dsn,
                runtime_session_id=runtime_session_id,
                graph_id=graph_id,
                workspace_root=tmp_path,
            ),
        )

        result = executor.apply_decision(
            CorrectAndSubmitDecision(
                target_entry_id=invalid.entry_id,
                candidate=_preference("candidate:uow-corrected"),
                reason="Correct invalid attempt.",
            ),
            governance_batch_id=batch_id,
        )

        assert isinstance(result.decision_record.write_outcome, WriteSucceededOutcome)
        memory_id = result.decision_record.write_outcome.memory_id
        fetched = query.fetch_nodes([memory_id], graph_id=graph_id)
        assert len(fetched) == 1
        assert fetched[0].statement == "The user prefers concise summaries."
        governance_candidates = [
            candidate for candidate in pool.list_candidates() if candidate.origin is CandidateOrigin.GOVERNANCE
        ]
        assert len(governance_candidates) == 1
        assert governance_candidates[0].source_run_id == f"run:governance/{batch_id}"
        assert len(pool.list_decisions()) == 1
        assert len(log.iter(run_id=f"run:governance/{batch_id}")) == 2
        with _connect_or_skip(dsn) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    select decision_id, target_entry_key, status, mutation_lane, sequence_key, dirty_memory_ids, payload
                    from memory_write_outbox
                    where governance_batch_id = %s
                    """,
                    (batch_id,),
                )
                rows = cursor.fetchall()
        assert len(rows) == 1
        decision_id, target_entry_key, status, mutation_lane, sequence_key, dirty_memory_ids, payload = rows[0]
        assert decision_id == result.decision_record.decision_id
        assert target_entry_key == invalid.entry_id
        assert status == "pending"
        assert mutation_lane == "governed_memory"
        assert sequence_key == graph_id
        assert isinstance(dirty_memory_ids, list) and len(dirty_memory_ids) == 1
        assert payload["kind"] == "canonical_mutation"
        assert payload["mutation_lane"] == "governed_memory"
        assert payload["surface_apply_status"] == {"search_index": "pending", "oxigraph": "pending"}
        assert isinstance(payload["documents"], list) and len(payload["documents"]) == 1
    finally:
        with _connect_or_skip(dsn) as connection:
            with connection.cursor() as cursor:
                cursor.execute("delete from memory_write_outbox where governance_batch_id = %s", (batch_id,))
                cursor.execute(
                    "delete from memory_governance_decisions where governance_batch_id = %s",
                    (batch_id,),
                )
                cursor.execute("delete from graph_documents where graph_id = %s", (graph_id,))
                cursor.execute("delete from memory_nodes where graph_id = %s", (graph_id,))
                cursor.execute("delete from memory_relations where graph_id = %s", (graph_id,))
                cursor.execute("delete from sessions where id = %s", (runtime_session_id,))


def test_postgres_governance_uow_failed_write_records_decision_but_not_mutation_outbox(tmp_path) -> None:
    dsn = StorageConfig.from_env().postgres_dsn
    _connect_or_skip(dsn).close()
    runtime_session_id = f"runtime:test:{uuid4().hex}"
    graph_id = f"graph:test/{uuid4().hex}"
    batch_id = f"governance:test:uow-failed:{uuid4().hex}"
    source_ctx = EventContext(
        run_id=f"run:source:{uuid4().hex}",
        turn_id=f"turn:source:{uuid4().hex}",
        reply_id=f"reply:source:{uuid4().hex}",
    )
    log = PostgresEventLog(dsn=dsn, runtime_session_id=runtime_session_id, workspace_root=tmp_path)
    pool = PostgresCandidatePool(dsn=dsn)
    try:
        log.append(TextBlockDeltaEvent(**source_ctx.event_fields(), block_id="text:seed", delta="seed"))
        candidate = pool.append_candidate(
            PooledMemoryCandidate(
                payload=ValidCandidatePayload(
                    candidate=_preference(
                        f"candidate:failed:{uuid4().hex}",
                        evidence_ids=("evidence:missing",),
                    )
                ),
                origin=CandidateOrigin.MAIN_AGENT_TOOL,
                source_session_id=runtime_session_id,
                source_run_id=source_ctx.run_id,
                source_turn_id=source_ctx.turn_id,
                source_reply_id=source_ctx.reply_id,
            )
        )
        executor = MemoryGovernanceExecutor(
            candidate_pool=pool,
            memory_write_service=_service_on(InMemoryGraphStore()),
            event_log=log,
            graph=InMemoryGraphStore(),
            runtime_session_id=runtime_session_id,
            memory_write_uow_factory=lambda: MemoryWriteUnitOfWork(
                dsn=dsn,
                runtime_session_id=runtime_session_id,
                graph_id=graph_id,
                workspace_root=tmp_path,
            ),
        )

        result = executor.apply_decision(
            SubmitAsIsDecision(target_entry_id=candidate.entry_id, reason="missing evidence should fail"),
            governance_batch_id=batch_id,
        )

        assert isinstance(result.decision_record.write_outcome, WriteFailedOutcome)
        with _connect_or_skip(dsn) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    "select count(*) from memory_governance_decisions where governance_batch_id = %s",
                    (batch_id,),
                )
                assert cursor.fetchone() == (1,)
                cursor.execute(
                    "select count(*) from memory_write_outbox where governance_batch_id = %s",
                    (batch_id,),
                )
                assert cursor.fetchone() == (0,)
    finally:
        with _connect_or_skip(dsn) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    "delete from memory_governance_decisions where governance_batch_id = %s",
                    (batch_id,),
                )
                cursor.execute("delete from graph_documents where graph_id = %s", (graph_id,))
                cursor.execute("delete from memory_nodes where graph_id = %s", (graph_id,))
                cursor.execute("delete from memory_relations where graph_id = %s", (graph_id,))
                cursor.execute("delete from sessions where id = %s", (runtime_session_id,))


def test_postgres_uow_dedupe_sees_uncommitted_same_transaction_node(tmp_path) -> None:
    dsn = StorageConfig.from_env().postgres_dsn
    _connect_or_skip(dsn).close()
    runtime_session_id = f"runtime:test:{uuid4().hex}"
    graph_id = f"graph:test/{uuid4().hex}"
    context = EventContext(
        run_id=f"run:governance/test-uow-dedupe/{uuid4().hex}",
        turn_id=f"turn:governance/test-uow-dedupe/{uuid4().hex}",
        reply_id=f"reply:governance/test-uow-dedupe/{uuid4().hex}",
    )
    try:
        with MemoryWriteUnitOfWork(
            dsn=dsn,
            runtime_session_id=runtime_session_id,
            graph_id=graph_id,
            workspace_root=tmp_path,
        ) as uow:
            outcome = uow.memory_write_service.submit(
                _preference("candidate:uow-dedupe-original"),
                event_context=context,
            )
            duplicate = _preference("candidate:uow-dedupe-duplicate")

            assert outcome.record is not None
            assert already_exists(duplicate, uow.graph, graph_id=uow.resolved_graph_id) is True
    finally:
        with _connect_or_skip(dsn) as connection:
            with connection.cursor() as cursor:
                cursor.execute("delete from graph_documents where graph_id = %s", (graph_id,))
                cursor.execute("delete from memory_nodes where graph_id = %s", (graph_id,))
                cursor.execute("delete from memory_relations where graph_id = %s", (graph_id,))
                cursor.execute("delete from sessions where id = %s", (runtime_session_id,))


def _executor(
    *,
    pool: InMemoryCandidatePool,
    graph: InMemoryGraphStore,
    log: InMemoryEventLog,
    allowed_write_scopes: frozenset[str] = frozenset({"ctx:user"}),
) -> MemoryGovernanceExecutor:
    return MemoryGovernanceExecutor(
        candidate_pool=pool,
        memory_write_service=_service_on(graph),
        event_log=log,
        graph=graph,
        runtime_session_id="runtime:test",
        allowed_write_scopes=allowed_write_scopes,
    )


def _service_on(graph: InMemoryGraphStore) -> MemoryWriteService:
    ledger = ExecutionEvidenceLedger(
        graph=graph,
        archive=InMemoryArchiveStore(),
        gate=MemoryWriteGate(),
    )
    return MemoryWriteService(ledger=ledger)


def _pooled_valid(candidate: PreferenceCandidate | None = None) -> PooledMemoryCandidate:
    candidate = candidate or _preference(f"candidate:test:{uuid4().hex}")
    return PooledMemoryCandidate(
        entry_id=f"pool:test:{uuid4().hex}",
        payload=ValidCandidatePayload(candidate=candidate),
        origin=CandidateOrigin.MAIN_AGENT_TOOL,
        source_session_id="runtime:test",
        source_run_id=f"run:source:{uuid4().hex}",
        source_turn_id=f"turn:source:{uuid4().hex}",
        source_reply_id=f"reply:source:{uuid4().hex}",
    )


def _pooled_invalid() -> PooledMemoryCandidate:
    return PooledMemoryCandidate(
        entry_id=f"pool:test:{uuid4().hex}",
        payload=InvalidAttemptPayload(
            attempted_tool_name="remember_action_boundary",
            attempted_kind="ActionBoundary",
            raw_arguments={"statement": "Do not commit unless asked."},
            validation_error="missing do_not_apply_when",
        ),
        origin=CandidateOrigin.MAIN_AGENT_TOOL,
        source_session_id="runtime:test",
        source_run_id=f"run:source:{uuid4().hex}",
        source_turn_id=f"turn:source:{uuid4().hex}",
        source_reply_id=f"reply:source:{uuid4().hex}",
    )


def _preference(
    candidate_id: str,
    evidence_ids: tuple[str, ...] = (),
    *,
    scope: str = "ctx:user",
) -> PreferenceCandidate:
    return PreferenceCandidate(
        candidate_id=candidate_id,
        statement="The user prefers concise summaries.",
        scope=scope,
        evidence_ids=evidence_ids,
        source_authority=memory.SourceAuthority.EXPLICIT_USER_INSTRUCTION,
        verification_status=memory.VerificationStatus.USER_CONFIRMED,
    )


def _connect_or_skip(dsn: str):
    try:
        return psycopg.connect(dsn, connect_timeout=2)
    except psycopg.OperationalError as exc:
        pytest.skip(f"Postgres is not available at configured DSN: {exc}")
