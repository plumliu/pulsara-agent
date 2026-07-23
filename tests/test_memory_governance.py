from __future__ import annotations

from tests.support.postgres import verified_postgres_provider

from concurrent.futures import ThreadPoolExecutor
from uuid import uuid4

import psycopg
import pytest

from tests.support.postgres import connect_postgres_test_database as _connect_or_skip

from tests.support.model_stream import (
    make_text_block_segment_event,
)

from pulsara_agent.event import EventContext, EventType
from pulsara_agent.event.candidates import (
    InvalidAttemptPayload,
    PreferenceCandidate,
    ValidCandidatePayload,
)
from pulsara_agent.event_log import InMemoryEventLog, PostgresEventLog
from pulsara_agent.graph import InMemoryGraphStore
from pulsara_agent.message.assembler import BlockAssembler
from pulsara_agent.message.reducer import MessageReplayControlError
from pulsara_agent.memory import (
    InMemoryArchiveStore,
    InMemoryCandidatePool,
    MemoryGovernanceExecutor,
    MemoryWriteUnitOfWork,
    PostgresArtifactStore,
    PooledMemoryCandidate,
    PostgresCandidatePool,
    PostgresMemoryQuery,
    ContradictAndSubmitDecision,
    SubmitAsIsDecision,
    SupersedeAndSubmitDecision,
    CorrectAndSubmitDecision,
)
from pulsara_agent.memory.candidates.pool import (
    CandidateOrigin,
    WriteFailedOutcome,
    WriteSucceededOutcome,
)
from pulsara_agent.memory.candidates.pool import decision_target_entry_ids
from pulsara_agent.memory.governance.executor import (
    GovernanceDecisionExecutionIdentity,
)
from pulsara_agent.memory.governance.claims import (
    InMemoryMemoryGovernanceCandidateClaimRepository,
    PostgresMemoryGovernanceCandidateClaimRepository,
)
from pulsara_agent.memory.governance.dedupe import already_exists
from pulsara_agent.memory.governance.event_outbox import (
    GovernanceEventOutboxDispatcher,
    PostgresGovernanceEventOutboxStore,
)
from pulsara_agent.memory.canonical.ledger import ExecutionEvidenceLedger
from pulsara_agent.memory.canonical.write_gate import MemoryWriteGate
from pulsara_agent.memory.canonical.write_service import MemoryWriteService
from pulsara_agent.ontology import memory
from pulsara_agent.settings import StorageConfig
from tests.support.memory_uow import fake_memory_uow_factory


def test_governance_executor_requires_explicit_uow_factory() -> None:
    graph = InMemoryGraphStore()
    pool = InMemoryCandidatePool()
    service = _service_on(graph)
    event_log = InMemoryEventLog()
    common = {
        "candidate_pool": pool,
        "memory_write_service": service,
        "event_log": event_log,
        "event_commit_port": event_log.extend,
        "graph": graph,
        "runtime_session_id": "runtime:test",
    }

    with pytest.raises(TypeError, match="memory_write_uow_factory"):
        MemoryGovernanceExecutor(**common)
    with pytest.raises(ValueError, match="required; no storage fallback"):
        MemoryGovernanceExecutor(
            **common,
            memory_write_uow_factory=None,  # type: ignore[arg-type]
        )


def test_postgres_uow_requires_explicit_artifact_store() -> None:
    with pytest.raises(TypeError, match="archive"):
        MemoryWriteUnitOfWork(  # type: ignore[call-arg]
            connection_provider=object(),  # type: ignore[arg-type]
            runtime_session_id="runtime:test",
        )


def test_governance_submit_as_is_writes_with_synthetic_context_and_resolves_pending() -> (
    None
):
    graph = InMemoryGraphStore()
    pool = InMemoryCandidatePool()
    log = InMemoryEventLog()
    candidate = pool.append_candidate(_pooled_valid())
    executor = _executor(pool=pool, graph=graph, log=log)

    result = _apply_decision(
        executor,
        SubmitAsIsDecision(
            target_entry_id=candidate.entry_id, reason="valid durable preference"
        ),
        governance_batch_id="governance:test:submit",
    )

    assert [event.type for event in result.events] == [
        EventType.MEMORY_CANDIDATE_PROPOSED,
        EventType.MEMORY_WRITE_RESULT,
    ]
    assert all(
        event.run_id == "run:governance/governance:test:submit"
        for event in result.events
    )
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

    result = _apply_decision(
        executor,
        SubmitAsIsDecision(
            target_entry_id=candidate.entry_id, reason="valid durable preference"
        ),
        governance_batch_id="governance:test:replay",
    )

    assembler = BlockAssembler()
    for event in result.events:
        update = assembler.append(event)
        assert update.started == []
        assert update.completed == []
    assert assembler.active_count() == 0

    with pytest.raises(MessageReplayControlError):
        log.replay("reply:governance/governance:test:replay")
    assert log.iter(run_id=candidate.source_run_id) == []


def test_governance_duplicate_existing_memory_records_skip_without_write_events() -> (
    None
):
    graph = InMemoryGraphStore()
    pool = InMemoryCandidatePool()
    log = InMemoryEventLog()
    service = _service_on(graph)
    service.submit(
        _preference("candidate:existing"),
        event_context=EventContext(
            run_id="run:old", turn_id="turn:old", reply_id="reply:old"
        ),
    )
    candidate = pool.append_candidate(_pooled_valid())
    executor = _executor(pool=pool, graph=graph, log=log)

    result = _apply_decision(
        executor,
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
            candidate=_preference(
                "candidate:missing-evidence", evidence_ids=("evidence:missing",)
            ),
        )
    )
    executor = _executor(pool=pool, graph=graph, log=log)

    result = _apply_decision(
        executor,
        SubmitAsIsDecision(
            target_entry_id=candidate.entry_id, reason="try missing evidence"
        ),
        governance_batch_id="governance:test:failed",
    )

    assert [event.type for event in result.events] == [
        EventType.MEMORY_CANDIDATE_PROPOSED,
        EventType.MEMORY_WRITE_FAILED,
    ]
    assert isinstance(result.decision_record.write_outcome, WriteFailedOutcome)
    assert [pending.entry_id for pending in pool.list_pending()] == [candidate.entry_id]
    assert graph.find_by_type(memory.PREFERENCE) == []


def test_governance_compaction_origin_supersede_fails_closed_without_write() -> None:
    graph = InMemoryGraphStore()
    pool = InMemoryCandidatePool()
    log = InMemoryEventLog()
    candidate = pool.append_candidate(
        _pooled_valid().model_copy(
            update={
                "origin": CandidateOrigin.COMPACTION,
                "metadata": {"compaction_id": "context_compaction:test"},
            }
        )
    )
    executor = _executor(pool=pool, graph=graph, log=log)

    result = _apply_decision(
        executor,
        SupersedeAndSubmitDecision(
            target_entry_id=candidate.entry_id,
            candidate=_preference("candidate:new"),
            superseded_memory_ids=("preference:old",),
            replacement_evidence_refs=("event:source",),
            reason="bad lifecycle decision for compaction candidate",
        ),
        governance_batch_id="governance:test:compaction-supersede",
    )

    assert result.events == []
    assert result.diagnostics == ("compaction_origin_replacement_evidence_unsupported",)
    assert result.decision_record.decision.kind == "skip"
    assert result.decision_record.write_outcome.kind == "no_write"
    assert pool.list_pending() == []
    assert graph.find_by_type(memory.PREFERENCE) == []


def test_governance_compaction_origin_contradict_fails_closed_without_write() -> None:
    graph = InMemoryGraphStore()
    pool = InMemoryCandidatePool()
    log = InMemoryEventLog()
    candidate = pool.append_candidate(
        _pooled_valid().model_copy(
            update={
                "origin": CandidateOrigin.COMPACTION,
                "metadata": {"compaction_id": "context_compaction:test"},
            }
        )
    )
    executor = _executor(pool=pool, graph=graph, log=log)

    result = _apply_decision(
        executor,
        ContradictAndSubmitDecision(
            target_entry_id=candidate.entry_id,
            candidate=_preference("candidate:new"),
            contradicted_memory_ids=("preference:old",),
            reason="bad contradiction decision for compaction candidate",
        ),
        governance_batch_id="governance:test:compaction-contradict",
    )

    assert result.events == []
    assert result.diagnostics == ("compaction_origin_replacement_evidence_unsupported",)
    assert result.decision_record.decision.kind == "skip"
    assert result.decision_record.write_outcome.kind == "no_write"
    assert pool.list_pending() == []
    assert graph.find_by_type(memory.PREFERENCE) == []


def test_governance_validates_correct_target_before_writing() -> None:
    graph = InMemoryGraphStore()
    pool = InMemoryCandidatePool()
    log = InMemoryEventLog()
    executor = _executor(pool=pool, graph=graph, log=log)

    with pytest.raises(KeyError):
        _apply_decision(
            executor,
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


def test_governance_claim_limit_applies_after_runtime_session_filter() -> None:
    pool = InMemoryCandidatePool()
    other_session = _pooled_valid()
    other_session = other_session.model_copy(
        update={"source_session_id": "runtime:other"}
    )
    pool.append_candidate(other_session)
    current_session = pool.append_candidate(_pooled_valid())
    claims = InMemoryMemoryGovernanceCandidateClaimRepository(candidate_pool=pool)

    batch = claims.claim_pending_batch(
        runtime_session_id="runtime:test",
        governance_batch_id="governance:test:limit",
        limit=1,
    )

    assert tuple(item.entry_id for item in batch.candidates) == (
        current_session.entry_id,
    )
    assert {item.source_session_id for item in pool.list_pending()} == {
        "runtime:test",
        "runtime:other",
    }


def test_postgres_governance_claims_are_all_or_none_under_concurrency() -> None:
    dsn = StorageConfig.from_env().postgres_dsn
    _connect_or_skip(dsn).close()
    suffix = uuid4().hex
    runtime_session_id = f"runtime:governance-claim:{suffix}"
    run_id = f"run:governance-claim:{suffix}"
    turn_id = f"turn:governance-claim:{suffix}"
    reply_id = f"reply:governance-claim:{suffix}"
    candidate = _pooled_valid().model_copy(
        update={
            "source_session_id": runtime_session_id,
            "source_run_id": run_id,
            "source_turn_id": turn_id,
            "source_reply_id": reply_id,
        }
    )

    with psycopg.connect(dsn) as connection:
        connection.execute(
            "insert into sessions(id, workspace_root) values (%s, %s)",
            (runtime_session_id, "."),
        )
        connection.execute(
            "insert into runs(id, session_id) values (%s, %s)",
            (run_id, runtime_session_id),
        )
        connection.execute(
            """
            insert into turns(id, session_id, run_id, turn_index)
            values (%s, %s, %s, 0)
            """,
            (turn_id, runtime_session_id, run_id),
        )

    pool = PostgresCandidatePool(connection_provider=verified_postgres_provider(dsn))
    pool.append_candidate(candidate)
    repositories = (
        PostgresMemoryGovernanceCandidateClaimRepository(
            connection_provider=verified_postgres_provider(dsn)
        ),
        PostgresMemoryGovernanceCandidateClaimRepository(
            connection_provider=verified_postgres_provider(dsn)
        ),
    )

    def claim(index: int):
        return repositories[index].claim_pending_batch(
            runtime_session_id=runtime_session_id,
            governance_batch_id=f"governance:claim-race:{suffix}:{index}",
            limit=1,
        )

    try:
        with ThreadPoolExecutor(max_workers=2) as executor:
            claimed = tuple(executor.map(claim, (0, 1)))

        assert sorted(len(batch.claims) for batch in claimed) == [0, 1]
        assert (
            sum(
                claim.candidate_entry_id == candidate.entry_id
                for batch in claimed
                for claim in batch.claims
            )
            == 1
        )
    finally:
        with psycopg.connect(dsn) as connection:
            connection.execute(
                "delete from sessions where id = %s", (runtime_session_id,)
            )


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
    executor = _executor(
        pool=pool, graph=graph, log=log, allowed_write_scopes=frozenset({"ctx:user"})
    )

    result = _apply_decision(
        executor,
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
        _apply_decision(
            executor,
            SubmitAsIsDecision(
                target_entry_id=candidate.entry_id, reason="wrong runtime"
            ),
            governance_batch_id="governance:test:wrong-runtime",
        )

    assert graph.find_by_type(memory.PREFERENCE) == []
    assert pool.list_decisions() == []


def test_governance_correct_and_submit_adds_governance_audit_candidate_without_pending_residue() -> (
    None
):
    graph = InMemoryGraphStore()
    pool = InMemoryCandidatePool()
    log = InMemoryEventLog()
    invalid = pool.append_candidate(_pooled_invalid())
    corrected = _preference("candidate:corrected")
    executor = _executor(pool=pool, graph=graph, log=log)

    result = _apply_decision(
        executor,
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
    governance_candidates = [
        candidate
        for candidate in all_candidates
        if candidate.origin == CandidateOrigin.GOVERNANCE
    ]
    assert len(governance_candidates) == 1
    assert pool.list_pending() == []
    assert len(graph.find_by_type(memory.PREFERENCE)) == 1


def test_postgres_governance_correct_and_submit_has_valid_governance_candidate_fk_chain(
    tmp_path,
) -> None:
    dsn = StorageConfig.from_env().postgres_dsn
    _connect_or_skip(dsn).close()
    runtime_session_id = f"runtime:test:{uuid4().hex}"
    source_ctx = EventContext(
        run_id=f"run:source:{uuid4().hex}",
        turn_id=f"turn:source:{uuid4().hex}",
        reply_id=f"reply:source:{uuid4().hex}",
    )
    batch_id = f"governance:test:postgres-correct:{uuid4().hex}"
    graph_id = f"graph:test/{uuid4().hex}"
    log = PostgresEventLog(
        connection_provider=verified_postgres_provider(dsn),
        runtime_session_id=runtime_session_id,
        workspace_root=tmp_path,
    )
    log.append(
        make_text_block_segment_event(
            **source_ctx.event_fields(), block_id="text:seed", delta="seed"
        )
    )
    pool = PostgresCandidatePool(connection_provider=verified_postgres_provider(dsn))
    graph = InMemoryGraphStore()
    try:
        invalid = pool.append_candidate(
            PooledMemoryCandidate(
                payload=InvalidAttemptPayload(
                    attempted_tool_name="remember_action_boundary",
                    attempted_kind="ActionBoundary",
                    raw_arguments={
                        "statement": "Never commit unless explicitly asked."
                    },
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
            event_commit_port=log.extend,
            graph=graph,
            runtime_session_id=runtime_session_id,
            event_outbox_dispatcher=_postgres_governance_event_dispatcher(
                dsn=dsn,
                runtime_session_id=runtime_session_id,
                event_commit_port=log.extend,
            ),
            memory_write_uow_factory=lambda: MemoryWriteUnitOfWork(
                connection_provider=verified_postgres_provider(dsn),
                runtime_session_id=runtime_session_id,
                archive=PostgresArtifactStore(
                    connection_provider=verified_postgres_provider(dsn)
                ),
                graph_id=graph_id,
                workspace_root=tmp_path,
            ),
        )

        result = _apply_decision(
            executor,
            CorrectAndSubmitDecision(
                target_entry_id=invalid.entry_id,
                candidate=_preference("candidate:postgres-corrected"),
                reason="Correct invalid attempt from user quote.",
            ),
            governance_batch_id=batch_id,
        )

        assert isinstance(result.decision_record.write_outcome, WriteSucceededOutcome)
        governance_candidates = [
            candidate
            for candidate in pool.list_candidates()
            if candidate.origin is CandidateOrigin.GOVERNANCE
            and candidate.source_session_id == runtime_session_id
        ]
        assert len(governance_candidates) == 1
        assert governance_candidates[0].source_session_id == runtime_session_id
        assert governance_candidates[0].source_run_id == f"run:governance/{batch_id}"
        assert governance_candidates[0].source_turn_id == f"turn:governance/{batch_id}"
        assert len(log.iter(run_id=f"run:governance/{batch_id}")) == 2
        assert [
            candidate
            for candidate in pool.list_pending()
            if candidate.source_session_id == runtime_session_id
        ] == []
    finally:
        with _connect_or_skip(dsn) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    "delete from memory_write_outbox where governance_batch_id = %s",
                    (batch_id,),
                )
                cursor.execute(
                    "delete from memory_governance_decisions where governance_batch_id = %s",
                    (batch_id,),
                )
                cursor.execute(
                    "delete from memory_governance_event_outbox where governance_batch_id = %s",
                    (batch_id,),
                )
                cursor.execute(
                    "delete from graph_documents where graph_id = %s", (graph_id,)
                )
                cursor.execute(
                    "delete from memory_nodes where graph_id = %s", (graph_id,)
                )
                cursor.execute(
                    "delete from memory_relations where graph_id = %s", (graph_id,)
                )
                cursor.execute(
                    "delete from sessions where id = %s", (runtime_session_id,)
                )


def test_postgres_governance_uow_writes_graph_decision_outbox_and_audit_candidate(
    tmp_path,
) -> None:
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
    log = PostgresEventLog(
        connection_provider=verified_postgres_provider(dsn),
        runtime_session_id=runtime_session_id,
        workspace_root=tmp_path,
    )
    log.append(
        make_text_block_segment_event(
            **source_ctx.event_fields(), block_id="text:seed", delta="seed"
        )
    )
    pool = PostgresCandidatePool(connection_provider=verified_postgres_provider(dsn))
    query = PostgresMemoryQuery(connection_provider=verified_postgres_provider(dsn))
    try:
        invalid = pool.append_candidate(
            PooledMemoryCandidate(
                payload=InvalidAttemptPayload(
                    attempted_tool_name="remember_action_boundary",
                    attempted_kind="ActionBoundary",
                    raw_arguments={
                        "statement": "Never commit unless explicitly asked."
                    },
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
            event_commit_port=log.extend,
            graph=InMemoryGraphStore(),
            runtime_session_id=runtime_session_id,
            event_outbox_dispatcher=_postgres_governance_event_dispatcher(
                dsn=dsn,
                runtime_session_id=runtime_session_id,
                event_commit_port=log.extend,
            ),
            memory_write_uow_factory=lambda: MemoryWriteUnitOfWork(
                connection_provider=verified_postgres_provider(dsn),
                runtime_session_id=runtime_session_id,
                archive=PostgresArtifactStore(
                    connection_provider=verified_postgres_provider(dsn)
                ),
                graph_id=graph_id,
                workspace_root=tmp_path,
            ),
        )

        result = _apply_decision(
            executor,
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
            candidate
            for candidate in pool.list_candidates()
            if candidate.origin is CandidateOrigin.GOVERNANCE
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
        (
            decision_id,
            target_entry_key,
            status,
            mutation_lane,
            sequence_key,
            dirty_memory_ids,
            payload,
        ) = rows[0]
        assert decision_id == result.decision_record.decision_id
        assert target_entry_key == invalid.entry_id
        assert status == "pending"
        assert mutation_lane == "governed_memory"
        assert sequence_key == graph_id
        assert isinstance(dirty_memory_ids, list) and len(dirty_memory_ids) == 1
        assert payload["kind"] == "canonical_mutation"
        assert payload["mutation_lane"] == "governed_memory"
        assert payload["surface_apply_status"] == {
            "search_index": "pending",
            "oxigraph": "pending",
        }
        assert isinstance(payload["documents"], list) and len(payload["documents"]) == 1
    finally:
        with _connect_or_skip(dsn) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    "delete from memory_write_outbox where governance_batch_id = %s",
                    (batch_id,),
                )
                cursor.execute(
                    "delete from memory_governance_decisions where governance_batch_id = %s",
                    (batch_id,),
                )
                cursor.execute(
                    "delete from memory_governance_event_outbox where governance_batch_id = %s",
                    (batch_id,),
                )
                cursor.execute(
                    "delete from graph_documents where graph_id = %s", (graph_id,)
                )
                cursor.execute(
                    "delete from memory_nodes where graph_id = %s", (graph_id,)
                )
                cursor.execute(
                    "delete from memory_relations where graph_id = %s", (graph_id,)
                )
                cursor.execute(
                    "delete from sessions where id = %s", (runtime_session_id,)
                )


def test_postgres_governance_uow_failed_write_records_decision_but_not_mutation_outbox(
    tmp_path,
) -> None:
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
    log = PostgresEventLog(
        connection_provider=verified_postgres_provider(dsn),
        runtime_session_id=runtime_session_id,
        workspace_root=tmp_path,
    )
    pool = PostgresCandidatePool(connection_provider=verified_postgres_provider(dsn))
    try:
        log.append(
            make_text_block_segment_event(
                **source_ctx.event_fields(), block_id="text:seed", delta="seed"
            )
        )
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
            event_commit_port=log.extend,
            graph=InMemoryGraphStore(),
            runtime_session_id=runtime_session_id,
            event_outbox_dispatcher=_postgres_governance_event_dispatcher(
                dsn=dsn,
                runtime_session_id=runtime_session_id,
                event_commit_port=log.extend,
            ),
            memory_write_uow_factory=lambda: MemoryWriteUnitOfWork(
                connection_provider=verified_postgres_provider(dsn),
                runtime_session_id=runtime_session_id,
                archive=PostgresArtifactStore(
                    connection_provider=verified_postgres_provider(dsn)
                ),
                graph_id=graph_id,
                workspace_root=tmp_path,
            ),
        )

        result = _apply_decision(
            executor,
            SubmitAsIsDecision(
                target_entry_id=candidate.entry_id,
                reason="missing evidence should fail",
            ),
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
                cursor.execute(
                    "delete from memory_governance_event_outbox where governance_batch_id = %s",
                    (batch_id,),
                )
                cursor.execute(
                    "delete from graph_documents where graph_id = %s", (graph_id,)
                )
                cursor.execute(
                    "delete from memory_nodes where graph_id = %s", (graph_id,)
                )
                cursor.execute(
                    "delete from memory_relations where graph_id = %s", (graph_id,)
                )
                cursor.execute(
                    "delete from sessions where id = %s", (runtime_session_id,)
                )


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
            connection_provider=verified_postgres_provider(dsn),
            runtime_session_id=runtime_session_id,
            archive=PostgresArtifactStore(
                connection_provider=verified_postgres_provider(dsn)
            ),
            graph_id=graph_id,
            workspace_root=tmp_path,
        ) as uow:
            outcome = uow.memory_write_service.submit(
                _preference("candidate:uow-dedupe-original"),
                event_context=context,
            )
            duplicate = _preference("candidate:uow-dedupe-duplicate")

            assert outcome.record is not None
            assert (
                already_exists(duplicate, uow.graph, graph_id=uow.resolved_graph_id)
                is True
            )
    finally:
        with _connect_or_skip(dsn) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    "delete from graph_documents where graph_id = %s", (graph_id,)
                )
                cursor.execute(
                    "delete from memory_nodes where graph_id = %s", (graph_id,)
                )
                cursor.execute(
                    "delete from memory_relations where graph_id = %s", (graph_id,)
                )
                cursor.execute(
                    "delete from sessions where id = %s", (runtime_session_id,)
                )


def test_postgres_governance_event_outbox_retries_after_memory_uow_commit(
    tmp_path,
) -> None:
    dsn = StorageConfig.from_env().postgres_dsn
    _connect_or_skip(dsn).close()
    runtime_session_id = f"runtime:test:{uuid4().hex}"
    graph_id = f"graph:test/{uuid4().hex}"
    batch_id = f"governance:test:event-outbox:{uuid4().hex}"
    source_ctx = EventContext(
        run_id=f"run:source:{uuid4().hex}",
        turn_id=f"turn:source:{uuid4().hex}",
        reply_id=f"reply:source:{uuid4().hex}",
    )
    log = PostgresEventLog(
        connection_provider=verified_postgres_provider(dsn),
        runtime_session_id=runtime_session_id,
        workspace_root=tmp_path,
    )
    pool = PostgresCandidatePool(connection_provider=verified_postgres_provider(dsn))
    attempts = 0

    def fail_once_then_commit(events):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise RuntimeError("synthetic ledger outage after memory commit")
        return log.extend(events)

    try:
        log.append(
            make_text_block_segment_event(
                **source_ctx.event_fields(),
                block_id="text:seed",
                delta="seed",
            )
        )
        candidate = pool.append_candidate(
            PooledMemoryCandidate(
                payload=ValidCandidatePayload(
                    candidate=_preference(f"candidate:event-outbox:{uuid4().hex}")
                ),
                origin=CandidateOrigin.MAIN_AGENT_TOOL,
                source_session_id=runtime_session_id,
                source_run_id=source_ctx.run_id,
                source_turn_id=source_ctx.turn_id,
                source_reply_id=source_ctx.reply_id,
            )
        )
        dispatcher = _postgres_governance_event_dispatcher(
            dsn=dsn,
            runtime_session_id=runtime_session_id,
            event_commit_port=fail_once_then_commit,
        )
        executor = MemoryGovernanceExecutor(
            candidate_pool=pool,
            memory_write_service=_service_on(InMemoryGraphStore()),
            event_log=log,
            event_commit_port=fail_once_then_commit,
            event_outbox_dispatcher=dispatcher,
            graph=InMemoryGraphStore(),
            runtime_session_id=runtime_session_id,
            memory_write_uow_factory=lambda: MemoryWriteUnitOfWork(
                connection_provider=verified_postgres_provider(dsn),
                runtime_session_id=runtime_session_id,
                archive=PostgresArtifactStore(
                    connection_provider=verified_postgres_provider(dsn)
                ),
                graph_id=graph_id,
                workspace_root=tmp_path,
            ),
        )

        with pytest.raises(RuntimeError, match="synthetic ledger outage"):
            _apply_decision(
                executor,
                SubmitAsIsDecision(
                    target_entry_id=candidate.entry_id,
                    reason="Exercise durable event handoff.",
                ),
                governance_batch_id=batch_id,
            )

        assert [
            item
            for item in pool.list_pending()
            if item.source_session_id == runtime_session_id
        ] == []
        assert not log.iter(run_id=f"run:governance/{batch_id}")
        with _connect_or_skip(dsn) as connection:
            decision_count = connection.execute(
                "select count(*) from memory_governance_decisions where governance_batch_id = %s",
                (batch_id,),
            ).fetchone()
            outbox_status = connection.execute(
                "select status from memory_governance_event_outbox where governance_batch_id = %s",
                (batch_id,),
            ).fetchone()
        assert decision_count == (1,)
        assert outbox_status == ("pending",)

        committed = executor.flush_pending_event_outbox()

        assert len(committed) == 2
        assert len(log.iter(run_id=f"run:governance/{batch_id}")) == 2
        with _connect_or_skip(dsn) as connection:
            outbox_status = connection.execute(
                "select status from memory_governance_event_outbox where governance_batch_id = %s",
                (batch_id,),
            ).fetchone()
        assert outbox_status == ("applied",)
        assert executor.event_dispatch_retry_required is False
    finally:
        with _connect_or_skip(dsn) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    "delete from memory_write_outbox where governance_batch_id = %s",
                    (batch_id,),
                )
                cursor.execute(
                    "delete from memory_governance_event_outbox where governance_batch_id = %s",
                    (batch_id,),
                )
                cursor.execute(
                    "delete from memory_governance_decisions where governance_batch_id = %s",
                    (batch_id,),
                )
                cursor.execute(
                    "delete from graph_documents where graph_id = %s", (graph_id,)
                )
                cursor.execute(
                    "delete from memory_nodes where graph_id = %s", (graph_id,)
                )
                cursor.execute(
                    "delete from memory_relations where graph_id = %s", (graph_id,)
                )
                cursor.execute(
                    "delete from sessions where id = %s", (runtime_session_id,)
                )


def _executor(
    *,
    pool: InMemoryCandidatePool,
    graph: InMemoryGraphStore,
    log: InMemoryEventLog,
    allowed_write_scopes: frozenset[str] = frozenset({"ctx:user"}),
) -> MemoryGovernanceExecutor:
    service = _service_on(graph)
    return MemoryGovernanceExecutor(
        candidate_pool=pool,
        memory_write_service=service,
        event_log=log,
        event_commit_port=log.extend,
        graph=graph,
        runtime_session_id="runtime:test",
        memory_write_uow_factory=fake_memory_uow_factory(
            graph=graph,
            candidate_pool=pool,
            memory_write_service=service,
        ),
        allowed_write_scopes=allowed_write_scopes,
    )


def _apply_decision(
    executor: MemoryGovernanceExecutor,
    decision,
    *,
    governance_batch_id: str,
):
    return executor.apply_decision(
        decision,
        governance_batch_id=governance_batch_id,
        execution_identity=GovernanceDecisionExecutionIdentity(
            batch_input_fingerprint=f"batch-input:{governance_batch_id}",
            batch_input_reference_fingerprint=(
                f"batch-input-ref:{governance_batch_id}"
            ),
            governance_model_call_id=f"model-call:{governance_batch_id}",
            decision_index=0,
            allowed_candidate_entry_ids=frozenset(decision_target_entry_ids(decision)),
            allowed_scopes=executor.allowed_write_scopes,
        ),
    )


def _postgres_governance_event_dispatcher(
    *,
    dsn: str,
    runtime_session_id: str,
    event_commit_port,
) -> GovernanceEventOutboxDispatcher:
    return GovernanceEventOutboxDispatcher(
        store=PostgresGovernanceEventOutboxStore(
            connection_provider=verified_postgres_provider(dsn),
            runtime_session_id=runtime_session_id,
        ),
        event_commit_port=event_commit_port,
    )


def _service_on(graph: InMemoryGraphStore) -> MemoryWriteService:
    ledger = ExecutionEvidenceLedger(
        graph=graph,
        archive=InMemoryArchiveStore(),
        gate=MemoryWriteGate(),
    )
    return MemoryWriteService(ledger=ledger)


def _pooled_valid(
    candidate: PreferenceCandidate | None = None,
) -> PooledMemoryCandidate:
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
