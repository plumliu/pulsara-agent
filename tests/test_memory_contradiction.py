from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from types import MappingProxyType
from uuid import uuid4

import psycopg
import pytest

from tests.support.model_stream import (
    make_text_block_segment_event,
)

from pulsara_agent.entities.memory import Claim, Preference
from pulsara_agent.event import EventContext, EventType
from pulsara_agent.event.candidates import ClaimCandidate, PreferenceCandidate, ValidCandidatePayload
from pulsara_agent.event_log import InMemoryEventLog, PostgresEventLog
from pulsara_agent.graph import InMemoryGraphStore, PostgresGraphStore
from pulsara_agent.jsonld import utc_now
from pulsara_agent.memory import (
    ContradictAndSubmitDecision,
    CorrectAndSubmitDecision,
    InMemoryArchiveStore,
    InMemoryCandidatePool,
    MemoryGovernanceExecutor,
    MemoryWriteUnitOfWork,
    PostgresArtifactStore,
    PostgresCandidatePool,
    PostgresMemoryQuery,
    WriteFailedOutcome,
    WriteSucceededOutcome,
)
from pulsara_agent.memory.candidates.pool import CandidateOrigin, PooledMemoryCandidate
from pulsara_agent.memory.canonical.index_sync import MemorySearchIndexSync, _outbox_memory_ids
from pulsara_agent.memory.canonical.ledger import ExecutionEvidenceLedger
from pulsara_agent.memory.canonical.lifecycle import MemoryLifecycle
from pulsara_agent.memory.canonical.query import CanonicalNodeView
from pulsara_agent.memory.canonical.write_gate import MemoryWriteGate
from pulsara_agent.memory.canonical.write_service import MemoryWriteService
from pulsara_agent.memory.governance.executor import _CONTRADICTION_DOWNGRADE_SENTINEL
from pulsara_agent.memory.governance.relatedness import (
    RelatednessAvailability,
    RelatednessExecutionContext,
)
from pulsara_agent.memory.hooks.durable import _merge_projections
from pulsara_agent.memory.recall.graph import GraphCandidateService
from pulsara_agent.memory.recall.projection import ProjectionBuilder
from pulsara_agent.memory.recall.service import (
    LexicalMemoryRecallService,
    RecallItem,
    RecallQuery,
    RecallResult,
    RecallStatus,
    RecallTrigger,
)
from pulsara_agent.ontology import memory
from pulsara_agent.settings import StorageConfig
from tests.support.memory_uow import fake_memory_uow_factory


def test_contradict_decision_facade_export_and_round_trip() -> None:
    decision = ContradictAndSubmitDecision(
        target_entry_id="pool:test",
        candidate=_preference_candidate("candidate:new", "The user dislikes egg tarts."),
        contradicted_memory_ids=("preference:old",),
        reason="New durable preference conflicts with an existing preference.",
    )

    assert decision.kind == "contradict_and_submit"
    assert decision.contradicted_memory_ids == ("preference:old",)


def test_postgres_governance_contradiction_writes_new_links_old_keeps_active_and_records_outcome(tmp_path) -> None:
    dsn = StorageConfig.from_env().postgres_dsn
    _connect_or_skip(dsn).close()
    runtime_session_id = f"runtime:test:{uuid4().hex}"
    graph_id = f"graph:test/{uuid4().hex}"
    batch_id = f"governance:test:contradiction:{uuid4().hex}"
    source_ctx = _source_context("contradiction")
    store = PostgresGraphStore(dsn=dsn)
    query = PostgresMemoryQuery(dsn=dsn)
    pool = PostgresCandidatePool(dsn=dsn)
    log = PostgresEventLog(dsn=dsn, runtime_session_id=runtime_session_id, workspace_root=tmp_path)
    log.append(make_text_block_segment_event(**source_ctx.event_fields(), block_id="text:seed", delta="seed"))
    old_id = "preference:test-contradiction-old"
    try:
        store.put_jsonld(_preference_doc(old_id, "The user likes egg tarts."), graph_id=graph_id)
        candidate = _preference_candidate("candidate:hate-egg-tarts", "The user hates egg tarts.")
        pooled = pool.append_candidate(
            _pooled_valid(runtime_session_id=runtime_session_id, source_ctx=source_ctx, candidate=candidate)
        )
        executor = _postgres_executor(
            dsn=dsn,
            pool=pool,
            log=log,
            runtime_session_id=runtime_session_id,
            graph_id=graph_id,
            workspace_root=tmp_path,
        )

        result = executor.apply_decision(
            ContradictAndSubmitDecision(
                target_entry_id=pooled.entry_id,
                candidate=candidate,
                contradicted_memory_ids=(old_id,),
                reason="Same-scope preference conflict without explicit replacement.",
            ),
            governance_batch_id=batch_id,
            relatedness_context=_relatedness_context(batch_id, pooled.entry_id, (old_id,)),
        )

        assert isinstance(result.decision_record.decision, ContradictAndSubmitDecision)
        assert isinstance(result.decision_record.write_outcome, WriteSucceededOutcome)
        new_id = result.decision_record.write_outcome.memory_id
        assert result.decision_record.write_outcome.contradicted_memory_ids == (old_id,)
        assert result.decision_record.write_outcome.superseded_memory_ids == ()
        assert [event.type for event in result.events] == [
            EventType.MEMORY_CANDIDATE_PROPOSED,
            EventType.MEMORY_WRITE_RESULT,
            EventType.MEMORY_CONTRADICTION_LINKED,
            EventType.MEMORY_CONTRADICTION_LINKED,
        ]
        assert _governance_candidate_count(pool) == 0

        old_doc = store.get_jsonld(old_id, graph_id=graph_id)
        new_doc = store.get_jsonld(new_id, graph_id=graph_id)
        assert old_doc[memory.STATUS.name] == memory.NodeStatus.ACTIVE.value
        assert new_doc[memory.STATUS.name] == memory.NodeStatus.ACTIVE.value
        assert {"@id": new_id} in old_doc[memory.CONTRADICTS.name]
        assert {"@id": old_id} in new_doc[memory.CONTRADICTS.name]

        fetched = {view.id: view for view in query.fetch_nodes([old_id, new_id], graph_id=graph_id)}
        assert fetched[old_id].status is memory.NodeStatus.ACTIVE
        assert fetched[new_id].status is memory.NodeStatus.ACTIVE
        assert (memory.CONTRADICTS.name, new_id) in fetched[old_id].outgoing
        assert (memory.CONTRADICTS.name, old_id) in fetched[new_id].outgoing
        with _connect_or_skip(dsn) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    "select payload from memory_write_outbox where governance_batch_id = %s",
                    (batch_id,),
                )
                payload = cursor.fetchone()[0]
        documents = {item["node_id"]: item["document"] for item in payload["documents"]}
        assert set(documents) == {old_id, new_id}
        assert {"@id": new_id} in documents[old_id][memory.CONTRADICTS.name]
        assert {"@id": old_id} in documents[new_id][memory.CONTRADICTS.name]
    finally:
        _cleanup_postgres(dsn, graph_id=graph_id, runtime_session_id=runtime_session_id, governance_batch_id=batch_id)


def test_uow_contradiction_links_old_new_in_memory_without_audit_candidate() -> None:
    graph = InMemoryGraphStore()
    pool = InMemoryCandidatePool()
    log = InMemoryEventLog()
    runtime_session_id = "runtime:test"
    old_id = "preference:test-uow-contradiction-old"
    graph.put_jsonld(_preference_doc(old_id, "The user likes egg tarts."))
    candidate = _preference_candidate("candidate:uow-hate-egg-tarts", "The user hates egg tarts.")
    pooled = pool.append_candidate(
        _pooled_valid(runtime_session_id=runtime_session_id, source_ctx=_source_context("uow"), candidate=candidate)
    )
    service = _service_on(graph)
    executor = MemoryGovernanceExecutor(
        candidate_pool=pool,
        memory_write_service=service,
        event_log=log,
        event_commit_port=log.extend,
        graph=graph,
        runtime_session_id=runtime_session_id,
        memory_write_uow_factory=fake_memory_uow_factory(
            graph=graph,
            candidate_pool=pool,
            memory_write_service=service,
        ),
    )

    result = executor.apply_decision(
        ContradictAndSubmitDecision(
            target_entry_id=pooled.entry_id,
            candidate=candidate,
            contradicted_memory_ids=(old_id,),
            reason="Same-scope preference conflict without explicit replacement.",
        ),
        governance_batch_id="governance:test:uow-contradiction",
        relatedness_context=_relatedness_context(
            "governance:test:uow-contradiction", pooled.entry_id, (old_id,)
        ),
    )

    assert isinstance(result.decision_record.decision, ContradictAndSubmitDecision)
    assert isinstance(result.decision_record.write_outcome, WriteSucceededOutcome)
    new_id = result.decision_record.write_outcome.memory_id
    assert result.decision_record.write_outcome.contradicted_memory_ids == (old_id,)
    assert [event.type for event in result.events] == [
        EventType.MEMORY_CANDIDATE_PROPOSED,
        EventType.MEMORY_WRITE_RESULT,
        EventType.MEMORY_CONTRADICTION_LINKED,
        EventType.MEMORY_CONTRADICTION_LINKED,
    ]
    assert graph.get_jsonld(old_id)[memory.STATUS.name] == memory.NodeStatus.ACTIVE.value
    assert graph.get_jsonld(new_id)[memory.STATUS.name] == memory.NodeStatus.ACTIVE.value
    assert {"@id": new_id} in graph.get_jsonld(old_id)[memory.CONTRADICTS.name]
    assert {"@id": old_id} in graph.get_jsonld(new_id)[memory.CONTRADICTS.name]
    assert _governance_candidate_count(pool) == 0


def test_postgres_contradiction_downgrades_gate_failures_without_audit_candidate(tmp_path) -> None:
    dsn = StorageConfig.from_env().postgres_dsn
    _connect_or_skip(dsn).close()
    runtime_session_id = f"runtime:test:{uuid4().hex}"
    graph_id = f"graph:test/{uuid4().hex}"
    source_ctx = _source_context("contradiction-gates")
    store = PostgresGraphStore(dsn=dsn)
    pool = PostgresCandidatePool(dsn=dsn)
    log = PostgresEventLog(dsn=dsn, runtime_session_id=runtime_session_id, workspace_root=tmp_path)
    log.append(make_text_block_segment_event(**source_ctx.event_fields(), block_id="text:seed", delta="seed"))
    active_old = "preference:test-contradiction-gates-active"
    inactive_old = "preference:test-contradiction-gates-inactive"
    workspace_old = "preference:test-contradiction-gates-workspace"
    claim_old = "claim:test-contradiction-gates-claim"
    try:
        store.put_jsonld(_preference_doc(active_old, "The user likes egg tarts."), graph_id=graph_id)
        store.put_jsonld(
            _preference_doc(
                inactive_old,
                "The user likes Portuguese custard tarts.",
                status=memory.NodeStatus.SUPERSEDED,
            ),
            graph_id=graph_id,
        )
        store.put_jsonld(
            _preference_doc(
                workspace_old,
                "The user likes egg tarts for this project.",
                scope="ctx:workspace/test_project",
            ),
            graph_id=graph_id,
        )
        store.put_jsonld(_claim_doc(claim_old, "The repository uses Python."), graph_id=graph_id)
        executor = _postgres_executor(
            dsn=dsn,
            pool=pool,
            log=log,
            runtime_session_id=runtime_session_id,
            graph_id=graph_id,
            workspace_root=tmp_path,
        )

        cases = [
            (
                _claim_candidate("candidate:claim", "The repository does not use Python."),
                (claim_old,),
                "type_not_contradictable",
            ),
            (
                _preference_candidate("candidate:multi", "The user hates egg tarts."),
                (active_old, inactive_old),
                "too_many_contradiction_targets",
            ),
            (
                _preference_candidate("candidate:missing", "The user hates custard pastries."),
                ("preference:missing",),
                "contradiction_target_missing",
            ),
            (
                _preference_candidate("candidate:inactive", "The user hates Portuguese custard tarts."),
                (inactive_old,),
                "contradiction_target_not_active",
            ),
            (
                _preference_candidate("candidate:scope", "The user hates egg tarts for this project."),
                (workspace_old,),
                "contradiction_target_scope_mismatch",
            ),
            (
                _preference_candidate("candidate:claim-target", "The user dislikes Python repositories."),
                (claim_old,),
                "contradiction_target_type_not_contradictable",
            ),
        ]
        for candidate, contradicted_ids, reason_prefix in cases:
            batch_id = f"governance:test:contradiction-gates:{uuid4().hex}"
            pooled = pool.append_candidate(
                _pooled_valid(runtime_session_id=runtime_session_id, source_ctx=source_ctx, candidate=candidate)
            )

            result = executor.apply_decision(
                ContradictAndSubmitDecision(
                    target_entry_id=pooled.entry_id,
                    candidate=candidate,
                    contradicted_memory_ids=contradicted_ids,
                    reason="Proposed contradiction should downgrade.",
                ),
                governance_batch_id=batch_id,
                relatedness_context=_relatedness_context(
                    batch_id, pooled.entry_id, contradicted_ids
                ),
            )

            assert isinstance(result.decision_record.decision, CorrectAndSubmitDecision)
            assert result.decision_record.decision.reason.startswith(_CONTRADICTION_DOWNGRADE_SENTINEL)
            assert reason_prefix in result.decision_record.decision.reason
            assert isinstance(result.decision_record.write_outcome, WriteSucceededOutcome)
            assert result.decision_record.write_outcome.contradicted_memory_ids == ()
            assert _governance_candidate_count(pool) == 0

        assert store.get_jsonld(active_old, graph_id=graph_id)[memory.STATUS.name] == memory.NodeStatus.ACTIVE.value
        assert store.get_jsonld(inactive_old, graph_id=graph_id)[memory.STATUS.name] == memory.NodeStatus.SUPERSEDED.value
        assert store.get_jsonld(workspace_old, graph_id=graph_id)[memory.STATUS.name] == memory.NodeStatus.ACTIVE.value
        assert store.get_jsonld(claim_old, graph_id=graph_id)[memory.STATUS.name] == memory.NodeStatus.ACTIVE.value
    finally:
        _cleanup_postgres(dsn, graph_id=graph_id, runtime_session_id=runtime_session_id, governance_batch_id=None)


def test_postgres_contradiction_downgrades_when_new_node_is_not_active(tmp_path) -> None:
    dsn = StorageConfig.from_env().postgres_dsn
    _connect_or_skip(dsn).close()
    runtime_session_id = f"runtime:test:{uuid4().hex}"
    graph_id = f"graph:test/{uuid4().hex}"
    batch_id = f"governance:test:contradiction-non-active:{uuid4().hex}"
    source_ctx = _source_context("contradiction-non-active")
    store = PostgresGraphStore(dsn=dsn)
    pool = PostgresCandidatePool(dsn=dsn)
    log = PostgresEventLog(dsn=dsn, runtime_session_id=runtime_session_id, workspace_root=tmp_path)
    log.append(make_text_block_segment_event(**source_ctx.event_fields(), block_id="text:seed", delta="seed"))
    old_id = "preference:test-contradiction-non-active-old"
    try:
        store.put_jsonld(_preference_doc(old_id, "The user likes egg tarts."), graph_id=graph_id)
        candidate = _preference_candidate(
            "candidate:inferred-hate",
            "The user might dislike egg tarts.",
            source_authority=memory.SourceAuthority.MODEL_INFERENCE,
            verification_status=memory.VerificationStatus.INFERRED,
        )
        pooled = pool.append_candidate(
            _pooled_valid(runtime_session_id=runtime_session_id, source_ctx=source_ctx, candidate=candidate)
        )
        executor = _postgres_executor(
            dsn=dsn,
            pool=pool,
            log=log,
            runtime_session_id=runtime_session_id,
            graph_id=graph_id,
            workspace_root=tmp_path,
        )

        result = executor.apply_decision(
            ContradictAndSubmitDecision(
                target_entry_id=pooled.entry_id,
                candidate=candidate,
                contradicted_memory_ids=(old_id,),
                reason="Should not link contradiction if the new node is not ACTIVE.",
            ),
            governance_batch_id=batch_id,
        )

        assert isinstance(result.decision_record.decision, CorrectAndSubmitDecision)
        assert isinstance(result.decision_record.write_outcome, WriteSucceededOutcome)
        assert result.decision_record.write_outcome.node_status is memory.NodeStatus.NEEDS_REVIEW
        assert result.decision_record.write_outcome.contradicted_memory_ids == ()
        old_doc = store.get_jsonld(old_id, graph_id=graph_id)
        assert old_doc[memory.STATUS.name] == memory.NodeStatus.ACTIVE.value
        assert memory.CONTRADICTS.name not in old_doc
    finally:
        _cleanup_postgres(dsn, graph_id=graph_id, runtime_session_id=runtime_session_id, governance_batch_id=batch_id)


def test_postgres_contradiction_write_failure_does_not_link_or_record_contradiction(tmp_path) -> None:
    dsn = StorageConfig.from_env().postgres_dsn
    _connect_or_skip(dsn).close()
    runtime_session_id = f"runtime:test:{uuid4().hex}"
    graph_id = f"graph:test/{uuid4().hex}"
    batch_id = f"governance:test:contradiction-write-failed:{uuid4().hex}"
    source_ctx = _source_context("contradiction-write-failed")
    store = PostgresGraphStore(dsn=dsn)
    pool = PostgresCandidatePool(dsn=dsn)
    log = PostgresEventLog(dsn=dsn, runtime_session_id=runtime_session_id, workspace_root=tmp_path)
    log.append(make_text_block_segment_event(**source_ctx.event_fields(), block_id="text:seed", delta="seed"))
    old_id = "preference:test-contradiction-write-failed-old"
    try:
        store.put_jsonld(_preference_doc(old_id, "The user likes egg tarts."), graph_id=graph_id)
        candidate = _preference_candidate(
            "candidate:missing-evidence",
            "The user hates egg tarts.",
            evidence_ids=("evidence:missing",),
        )
        pooled = pool.append_candidate(
            _pooled_valid(runtime_session_id=runtime_session_id, source_ctx=source_ctx, candidate=candidate)
        )
        executor = _postgres_executor(
            dsn=dsn,
            pool=pool,
            log=log,
            runtime_session_id=runtime_session_id,
            graph_id=graph_id,
            workspace_root=tmp_path,
        )

        result = executor.apply_decision(
            ContradictAndSubmitDecision(
                target_entry_id=pooled.entry_id,
                candidate=candidate,
                contradicted_memory_ids=(old_id,),
                reason="Write should fail before linking contradiction.",
            ),
            governance_batch_id=batch_id,
        )

        assert isinstance(result.decision_record.decision, CorrectAndSubmitDecision)
        assert isinstance(result.decision_record.write_outcome, WriteFailedOutcome)
        old_doc = store.get_jsonld(old_id, graph_id=graph_id)
        assert old_doc[memory.STATUS.name] == memory.NodeStatus.ACTIVE.value
        assert memory.CONTRADICTS.name not in old_doc
        assert _governance_candidate_count(pool) == 0
        with _connect_or_skip(dsn) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    "select count(*) from memory_write_outbox where governance_batch_id = %s",
                    (batch_id,),
                )
                assert cursor.fetchone() == (0,)
    finally:
        _cleanup_postgres(dsn, graph_id=graph_id, runtime_session_id=runtime_session_id, governance_batch_id=batch_id)


def test_postgres_contradiction_rolls_back_when_lifecycle_fails_after_first_edge(tmp_path) -> None:
    dsn = StorageConfig.from_env().postgres_dsn
    _connect_or_skip(dsn).close()
    runtime_session_id = f"runtime:test:{uuid4().hex}"
    graph_id = f"graph:test/{uuid4().hex}"
    batch_id = f"governance:test:contradiction-rollback:{uuid4().hex}"
    source_ctx = _source_context("contradiction-rollback")
    store = PostgresGraphStore(dsn=dsn)
    pool = PostgresCandidatePool(dsn=dsn)
    log = PostgresEventLog(dsn=dsn, runtime_session_id=runtime_session_id, workspace_root=tmp_path)
    log.append(make_text_block_segment_event(**source_ctx.event_fields(), block_id="text:seed", delta="seed"))
    old_id = "preference:test-contradiction-rollback-old"
    try:
        store.put_jsonld(_preference_doc(old_id, "The user likes egg tarts."), graph_id=graph_id)
        candidate = _preference_candidate("candidate:rollback-hate-egg", "The user hates egg tarts.")
        pooled = pool.append_candidate(
            _pooled_valid(runtime_session_id=runtime_session_id, source_ctx=source_ctx, candidate=candidate)
        )
        executor = MemoryGovernanceExecutor(
            candidate_pool=pool,
            memory_write_service=_service_on(InMemoryGraphStore()),
            event_log=log,
            event_commit_port=log.extend,
            graph=InMemoryGraphStore(),
            runtime_session_id=runtime_session_id,
            memory_write_uow_factory=lambda: _FailingContradictionLifecycleUow(
                dsn=dsn,
                runtime_session_id=runtime_session_id,
                archive=PostgresArtifactStore(dsn=dsn),
                graph_id=graph_id,
                workspace_root=tmp_path,
            ),
        )

        with pytest.raises(RuntimeError, match="boom after first contradiction edge"):
            executor.apply_decision(
                ContradictAndSubmitDecision(
                    target_entry_id=pooled.entry_id,
                    candidate=candidate,
                    contradicted_memory_ids=(old_id,),
                    reason="Inject lifecycle failure after the first edge.",
                ),
                governance_batch_id=batch_id,
                relatedness_context=_relatedness_context(batch_id, pooled.entry_id, (old_id,)),
            )

        old_doc = store.get_jsonld(old_id, graph_id=graph_id)
        assert old_doc[memory.STATUS.name] == memory.NodeStatus.ACTIVE.value
        assert memory.CONTRADICTS.name not in old_doc
        with _connect_or_skip(dsn) as connection:
            with connection.cursor() as cursor:
                cursor.execute("select count(*) from memory_nodes where graph_id = %s", (graph_id,))
                assert cursor.fetchone()[0] == 1
                cursor.execute("select count(*) from memory_relations where graph_id = %s", (graph_id,))
                assert cursor.fetchone()[0] == 0
    finally:
        _cleanup_postgres(dsn, graph_id=graph_id, runtime_session_id=runtime_session_id, governance_batch_id=batch_id)


def test_contradiction_without_relatedness_context_is_blocked_and_downgraded() -> None:
    # The explicit fake takes the same executor decision path as durable.
    # Contradiction without relatedness evidence is safely blocked and
    # downgraded with the real relatedness-gate reason.
    graph = InMemoryGraphStore()
    pool = InMemoryCandidatePool()
    log = InMemoryEventLog()
    service = _service_on(graph)
    old_outcome = service.submit(
        _preference_candidate("candidate:old", "The user likes egg tarts."),
        event_context=EventContext(run_id="run:old", turn_id="turn:old", reply_id="reply:old"),
    )
    old_id = next(event.memory_id for event in old_outcome.events if hasattr(event, "memory_id"))
    candidate = _preference_candidate("candidate:new", "The user hates egg tarts.")
    pooled = pool.append_candidate(
        _pooled_valid(runtime_session_id="runtime:test", source_ctx=_source_context("legacy"), candidate=candidate)
    )
    executor = MemoryGovernanceExecutor(
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
    )

    result = executor.apply_decision(
        ContradictAndSubmitDecision(
            target_entry_id=pooled.entry_id,
            candidate=candidate,
            contradicted_memory_ids=(old_id,),
            reason="Contradiction attempted without relatedness context.",
        ),
        governance_batch_id="governance:test:contradiction-no-context",
    )

    assert isinstance(result.decision_record.decision, CorrectAndSubmitDecision)
    reason = result.decision_record.decision.reason
    assert reason.startswith(_CONTRADICTION_DOWNGRADE_SENTINEL)
    assert "relatedness_context_missing" in reason
    assert result.diagnostics == ("relatedness_context_missing",)
    assert isinstance(result.decision_record.write_outcome, WriteSucceededOutcome)
    assert result.decision_record.write_outcome.contradicted_memory_ids == ()
    old_doc = graph.get_jsonld(old_id)
    assert old_doc[memory.STATUS.name] == memory.NodeStatus.ACTIVE.value
    assert memory.CONTRADICTS.name not in old_doc
    assert _governance_candidate_count(pool) == 0


def test_recall_surfaces_contradiction_companion_at_zero_hop_and_grounds_path_at_one_hop() -> None:
    dsn = StorageConfig.from_env().postgres_dsn
    _connect_or_skip(dsn).close()
    graph_id = f"graph:test/{uuid4().hex}"
    old_id = "preference:test-recall-like-egg"
    new_id = "preference:test-recall-avoid-custard"
    store = PostgresGraphStore(dsn=dsn)
    query = PostgresMemoryQuery(dsn=dsn)
    lifecycle = MemoryLifecycle(graph=store, mutable=store)
    try:
        store.put_jsonld(_preference_doc(old_id, "The user likes egg tarts."), graph_id=graph_id)
        store.put_jsonld(_preference_doc(new_id, "The user avoids Portuguese custard pastries."), graph_id=graph_id)
        lifecycle.link_contradiction(
            left_id=old_id,
            right_id=new_id,
            governance_batch_id=f"governance:test:recall-contradiction:{uuid4().hex}",
            graph_id=graph_id,
        )
        MemorySearchIndexSync(dsn=dsn).rebuild(graph_id=graph_id)

        service = LexicalMemoryRecallService(query, graph_candidates=GraphCandidateService(query))
        zero_hop = asyncio.run(
            service.recall(
                RecallQuery(
                    text="likes egg",
                    scopes=("ctx:user",),
                    limit=2,
                    trigger=RecallTrigger.EXPLICIT_SEARCH,
                ),
                graph_id=graph_id,
            )
        )
        result = asyncio.run(
            service.recall(
                RecallQuery(
                    text="likes egg",
                    scopes=("ctx:user",),
                    limit=2,
                    max_hops=1,
                    trigger=RecallTrigger.EXPLICIT_SEARCH,
                ),
                graph_id=graph_id,
            )
        )
        projection = ProjectionBuilder().build(result, token_budget=300)

        # 0-hop already surfaces the contradiction companion (special case), but
        # without a grounded graph path — that trail only appears at hops>=1.
        zero_by_id = {item.memory_id: item for item in zero_hop.items}
        assert set(zero_by_id) == {old_id, new_id}
        assert zero_by_id[new_id].direct_match is False
        assert zero_by_id[new_id].paths == ()
        assert "contradiction_companion" in zero_by_id[new_id].why
        assert zero_by_id[old_id].conflicts_with == (new_id,)
        assert result.status is RecallStatus.OK
        by_id = {item.memory_id: item for item in result.items}
        assert set(by_id) == {old_id, new_id}
        assert by_id[old_id].conflicts_with == (new_id,)
        assert by_id[new_id].conflicts_with == (old_id,)
        assert "contradiction_warning" in by_id[old_id].why
        assert by_id[new_id].direct_match is False
        assert by_id[new_id].hop_count == 1
        assert by_id[new_id].paths[0].steps[0].predicate == memory.CONTRADICTS.name
        assert projection["conflict_groups"] == [
            {"kind": "contradiction", "memory_ids": sorted([old_id, new_id])}
        ]
        assert "Conflicting recalled memories" in projection["summary"]
    finally:
        store.delete_graph(graph_id)


def test_zero_hop_recall_does_not_follow_hidden_contradiction_edges() -> None:
    matched_id = "preference:visible-like-egg"
    cross_scope_id = "preference:hidden-workspace-egg"
    inactive_id = "preference:hidden-superseded-egg"
    views = {
        matched_id: _view(
            matched_id,
            "The user likes egg tarts.",
            outgoing=(
                (memory.CONTRADICTS.name, cross_scope_id),
                (memory.CONTRADICTS.name, inactive_id),
            ),
        ),
        cross_scope_id: _view(
            cross_scope_id,
            "The user hates egg tarts for another workspace.",
            scope="ctx:workspace/other",
        ),
        inactive_id: _view(
            inactive_id,
            "The user hates egg tarts.",
            status=memory.NodeStatus.SUPERSEDED,
        ),
    }

    result = asyncio.run(
        LexicalMemoryRecallService(_FakeMemoryQuery(views, ranked_ids=(matched_id,))).recall(
            RecallQuery(text="egg tarts", scopes=("ctx:user",), limit=3)
        )
    )

    assert result.status is RecallStatus.OK
    assert [item.memory_id for item in result.items] == [matched_id]
    assert result.filtered_ids == ()
    assert result.items[0].conflicts_with == ()
    assert "contradiction_warning" not in result.items[0].why


def test_recall_surfaces_contradiction_companion_beyond_limit() -> None:
    # Contradiction is a 0-hop special case: even when limit=1 is filled by the
    # matched memory, the active CONTRADICTS partner is surfaced as a companion
    # (exempt from limit) so the model never acts on half a known conflict.
    matched_id = "preference:visible-like-egg"
    companion_id = "preference:visible-hate-egg"
    views = {
        matched_id: _view(
            matched_id,
            "The user likes egg tarts.",
            outgoing=((memory.CONTRADICTS.name, companion_id),),
        ),
        companion_id: _view(
            companion_id,
            "The user hates egg tarts.",
            outgoing=((memory.CONTRADICTS.name, matched_id),),
        ),
    }

    result = asyncio.run(
        LexicalMemoryRecallService(_FakeMemoryQuery(views, ranked_ids=(matched_id,))).recall(
            RecallQuery(text="likes egg", scopes=("ctx:user",), limit=1)
        )
    )
    projection = ProjectionBuilder().build(result, token_budget=200)

    assert result.status is RecallStatus.OK
    by_id = {item.memory_id: item for item in result.items}
    assert set(by_id) == {matched_id, companion_id}
    assert by_id[matched_id].conflicts_with == (companion_id,)
    assert "contradiction_warning" in by_id[matched_id].why
    assert by_id[companion_id].direct_match is False
    assert "contradiction_companion" in by_id[companion_id].why
    assert projection["conflict_groups"] == [
        {"kind": "contradiction", "memory_ids": sorted([matched_id, companion_id])}
    ]


def _recall_item(memory_id: str, *, snippet: str, conflicts_with: tuple[str, ...] = ()) -> RecallItem:
    why = ("contradiction_warning",) if conflicts_with else ("recall_match",)
    return RecallItem(
        memory_id=memory_id,
        memory_type=memory.PREFERENCE.name,
        scope="ctx:user",
        status=memory.NodeStatus.ACTIVE,
        snippet=snippet,
        score=0.5,
        why=why,
        deep_recall=f"memory_get {memory_id}",
        conflicts_with=conflicts_with,
        direct_match=not conflicts_with,
    )


def test_projection_truncation_does_not_overclaim_included_or_conflicts() -> None:
    # Tight token_budget must clip the tail; the metadata (included_memory_ids /
    # conflict_groups) must reflect ONLY what survived in the summary text. The
    # contradiction pair has SMALL snippets and renders first, so it (and the
    # conflict block) survives while the large non-conflict tail is dropped.
    result = RecallResult(
        status=RecallStatus.OK,
        items=(
            _recall_item("preference:left", snippet="likes tabs", conflicts_with=("preference:right",)),
            _recall_item("preference:right", snippet="likes spaces", conflicts_with=("preference:left",)),
            _recall_item("preference:c", snippet="c " + "x" * 400),
            _recall_item("preference:d", snippet="d " + "x" * 400),
            _recall_item("preference:e", snippet="e " + "x" * 400),
        ),
    )

    projection = ProjectionBuilder().build(result, token_budget=150)

    included = projection["included_memory_ids"]
    # Honesty invariant: every claimed id actually appears in the summary text.
    assert all(f"[{mid}]" in projection["summary"] for mid in included)
    # Truncation actually happened (not all 5 items fit).
    assert len(included) < len(result.items)
    # The contradiction pair is the safety signal — it must survive the clip.
    assert "preference:left" in included
    assert "preference:right" in included
    # A large non-conflict tail item was dropped (the over-claim guard).
    assert "preference:e" not in included
    # conflict_groups must never reference an id absent from included_memory_ids.
    for group in projection["conflict_groups"]:
        assert set(group["memory_ids"]) <= set(included)
    assert projection["conflict_groups"] == [
        {"kind": "contradiction", "memory_ids": ["preference:left", "preference:right"]}
    ]


def test_projection_generous_budget_includes_all_items_and_conflict() -> None:
    result = RecallResult(
        status=RecallStatus.OK,
        items=(
            _recall_item("preference:left", snippet="likes tabs", conflicts_with=("preference:right",)),
            _recall_item("preference:right", snippet="likes spaces", conflicts_with=("preference:left",)),
            _recall_item("preference:c", snippet="unrelated"),
        ),
    )

    projection = ProjectionBuilder().build(result, token_budget=500)

    assert set(projection["included_memory_ids"]) == {
        "preference:left",
        "preference:right",
        "preference:c",
    }
    assert projection["conflict_groups"] == [
        {"kind": "contradiction", "memory_ids": ["preference:left", "preference:right"]}
    ]


def test_projection_minimum_budget_keeps_long_conflict_pair_atomic() -> None:
    result = RecallResult(
        status=RecallStatus.OK,
        items=(
            _recall_item(
                "preference:left-with-long-id",
                snippet="left " + "x" * 500,
                conflicts_with=("preference:right-with-long-id",),
            ),
            _recall_item(
                "preference:right-with-long-id",
                snippet="right " + "y" * 500,
                conflicts_with=("preference:left-with-long-id",),
            ),
            _recall_item("preference:ordinary-tail", snippet="ordinary tail"),
        ),
    )

    projection = ProjectionBuilder().build(result, token_budget=1)

    assert projection["included_memory_ids"] == [
        "preference:left-with-long-id",
        "preference:right-with-long-id",
    ]
    assert "[preference:left-with-long-id]" in projection["summary"]
    assert "[preference:right-with-long-id]" in projection["summary"]
    assert " <-> " in projection["summary"]
    assert "left=" in projection["summary"]
    assert "right=" in projection["summary"]
    assert "preference:ordinary-tail" not in projection["summary"]
    assert projection["summary"].endswith("</recalled-memory-projection>")
    assert all(f"- {unit}" in projection["summary"] for unit in projection["items"])
    assert projection["conflict_groups"] == [
        {
            "kind": "contradiction",
            "memory_ids": [
                "preference:left-with-long-id",
                "preference:right-with-long-id",
            ],
        }
    ]


def test_merge_projection_preserves_conflict_groups() -> None:
    working_context = {
        "summary": '<working-context-projection do_not_write_back="true">recent work</working-context-projection>',
        "items": ["recent work"],
        "included_memory_ids": [],
        "filtered_memory_ids": [],
        "do_not_write_back": True,
        "projection_kind": "working_context",
    }
    recalled = {
        "summary": '<recalled-memory-projection do_not_write_back="true">conflict</recalled-memory-projection>',
        "items": ["conflict"],
        "included_memory_ids": ["preference:a", "preference:b"],
        "filtered_memory_ids": [],
        "conflict_groups": [{"kind": "contradiction", "memory_ids": ["preference:b", "preference:a"]}],
        "do_not_write_back": True,
    }

    projection = _merge_projections(working_context, recalled)

    assert projection is not None
    assert projection["projection_kind"] == "mixed"
    assert projection["conflict_groups"] == [
        {"kind": "contradiction", "memory_ids": ["preference:a", "preference:b"]}
    ]


def test_outbox_memory_ids_ignores_contradicted_memory_ids() -> None:
    assert _outbox_memory_ids(
        {
            "kind": "canonical_mutation",
            "mutation_lane": "governed_memory",
            "dirty_memory_ids": ["preference:new", "preference:old"],
        }
    ) == ("preference:new", "preference:old")


def _postgres_executor(
    *,
    dsn: str,
    pool: PostgresCandidatePool,
    log: PostgresEventLog,
    runtime_session_id: str,
    graph_id: str,
    workspace_root,
) -> MemoryGovernanceExecutor:
    return MemoryGovernanceExecutor(
        candidate_pool=pool,
        memory_write_service=_service_on(InMemoryGraphStore()),
        event_log=log,
        event_commit_port=log.extend,
        graph=InMemoryGraphStore(),
        runtime_session_id=runtime_session_id,
        memory_write_uow_factory=lambda: MemoryWriteUnitOfWork(
            dsn=dsn,
            runtime_session_id=runtime_session_id,
            archive=PostgresArtifactStore(dsn=dsn),
            graph_id=graph_id,
            workspace_root=workspace_root,
        ),
    )


def _service_on(graph: InMemoryGraphStore) -> MemoryWriteService:
    return MemoryWriteService(
        ledger=ExecutionEvidenceLedger(
            graph=graph,
            archive=InMemoryArchiveStore(),
            gate=MemoryWriteGate(),
        )
    )


class _FailingContradictionLifecycleUow(MemoryWriteUnitOfWork):
    def __enter__(self):
        uow = super().__enter__()
        uow.lifecycle = _FailingAfterFirstContradictionEdgeLifecycle(uow.lifecycle)
        return uow


class _FailingAfterFirstContradictionEdgeLifecycle:
    def __init__(self, wrapped: MemoryLifecycle) -> None:
        self._wrapped = wrapped

    def link_contradiction(
        self,
        *,
        left_id: str,
        right_id: str,
        governance_batch_id: str,
        graph_id: str | None = None,
    ):
        left_doc = self._wrapped.graph.get_jsonld(left_id, graph_id=graph_id)
        _append_jsonld_ref(left_doc, memory.CONTRADICTS.name, right_id)
        self._wrapped.graph.put_jsonld(left_doc, graph_id=graph_id)
        raise RuntimeError("boom after first contradiction edge")


class _FakeMemoryQuery:
    def __init__(self, views: dict[str, CanonicalNodeView], *, ranked_ids: tuple[str, ...]) -> None:
        self._views = views
        self._ranked_ids = ranked_ids

    def lexical_candidates(self, *, terms, scopes, types, limit, graph_id=None):
        return [(memory_id, 1.0) for memory_id in self._ranked_ids[:limit]]

    def fts_candidates(self, *, query_text, scopes, types, limit, graph_id=None):
        return []

    def fetch_nodes(self, ids, *, graph_id=None):
        return [self._views[memory_id] for memory_id in ids if memory_id in self._views]


def _view(
    memory_id: str,
    statement: str,
    *,
    scope: str = "ctx:user",
    status: memory.NodeStatus = memory.NodeStatus.ACTIVE,
    outgoing: tuple[tuple[str, str], ...] = (),
    incoming: tuple[tuple[str, str], ...] = (),
) -> CanonicalNodeView:
    now = datetime.now(timezone.utc)
    return CanonicalNodeView(
        id=memory_id,
        memory_type="Preference",
        scope=scope,
        status=status,
        statement=statement,
        summary=None,
        source_authority=memory.SourceAuthority.EXPLICIT_USER_INSTRUCTION,
        verification_status=memory.VerificationStatus.USER_CONFIRMED,
        confidence_level=memory.ConfidenceLevel.HIGH,
        applies_when=None,
        do_not_apply_when=None,
        created_at=now,
        updated_at=now,
        evidence_ids=(),
        outgoing=outgoing,
        incoming=incoming,
    )


def _append_jsonld_ref(document: dict, predicate: str, target_id: str) -> None:
    values = list(document.get(predicate) or [])
    node_ref = {"@id": target_id}
    if node_ref not in values:
        values.append(node_ref)
    document[predicate] = values


def _pooled_valid(
    *,
    runtime_session_id: str,
    source_ctx: EventContext,
    candidate: PreferenceCandidate | ClaimCandidate,
) -> PooledMemoryCandidate:
    return PooledMemoryCandidate(
        entry_id=f"pool:test:{uuid4().hex}",
        payload=ValidCandidatePayload(candidate=candidate),
        origin=CandidateOrigin.MAIN_AGENT_TOOL,
        source_session_id=runtime_session_id,
        source_run_id=source_ctx.run_id,
        source_turn_id=source_ctx.turn_id,
        source_reply_id=source_ctx.reply_id,
    )


def _preference_candidate(
    candidate_id: str,
    statement: str,
    *,
    scope: str = "ctx:user",
    evidence_ids: tuple[str, ...] = (),
    source_authority: memory.SourceAuthority = memory.SourceAuthority.EXPLICIT_USER_INSTRUCTION,
    verification_status: memory.VerificationStatus = memory.VerificationStatus.USER_CONFIRMED,
) -> PreferenceCandidate:
    return PreferenceCandidate(
        candidate_id=candidate_id,
        statement=statement,
        scope=scope,
        evidence_ids=evidence_ids,
        source_authority=source_authority,
        verification_status=verification_status,
    )


def _claim_candidate(candidate_id: str, statement: str, *, scope: str = "ctx:user") -> ClaimCandidate:
    return ClaimCandidate(
        candidate_id=candidate_id,
        statement=statement,
        scope=scope,
        source_authority=memory.SourceAuthority.EXPLICIT_USER_INSTRUCTION,
        verification_status=memory.VerificationStatus.USER_CONFIRMED,
    )


def _preference_doc(
    memory_id: str,
    statement: str,
    *,
    scope: str = "ctx:user",
    status: memory.NodeStatus = memory.NodeStatus.ACTIVE,
) -> dict:
    now = utc_now()
    return Preference(
        id=memory_id,
        statement=statement,
        scope=scope,
        status=status,
        confidence_level=memory.ConfidenceLevel.HIGH,
        verification_status=memory.VerificationStatus.USER_CONFIRMED,
        source_authority=memory.SourceAuthority.EXPLICIT_USER_INSTRUCTION,
        created_at=now,
        updated_at=now,
        gate_reason="test",
    ).to_jsonld()


def _claim_doc(memory_id: str, statement: str, *, scope: str = "ctx:user") -> dict:
    now = utc_now()
    return Claim(
        id=memory_id,
        statement=statement,
        scope=scope,
        status=memory.NodeStatus.ACTIVE,
        confidence_level=memory.ConfidenceLevel.HIGH,
        verification_status=memory.VerificationStatus.USER_CONFIRMED,
        source_authority=memory.SourceAuthority.EXPLICIT_USER_INSTRUCTION,
        created_at=now,
        updated_at=now,
        gate_reason="test",
    ).to_jsonld()


def _source_context(label: str) -> EventContext:
    suffix = uuid4().hex
    return EventContext(
        run_id=f"run:source:{label}:{suffix}",
        turn_id=f"turn:source:{label}:{suffix}",
        reply_id=f"reply:source:{label}:{suffix}",
    )


def _relatedness_context(
    batch_id: str,
    entry_id: str,
    memory_ids: tuple[str, ...],
) -> RelatednessExecutionContext:
    return RelatednessExecutionContext(
        governance_batch_id=batch_id,
        allowlists=MappingProxyType({entry_id: frozenset(memory_ids)}),
        availability=MappingProxyType({entry_id: RelatednessAvailability.FULL}),
    )


def _governance_candidate_count(pool) -> int:
    return sum(1 for candidate in pool.list_candidates() if candidate.origin is CandidateOrigin.GOVERNANCE)


def _connect_or_skip(dsn: str):
    try:
        return psycopg.connect(dsn, connect_timeout=2)
    except psycopg.OperationalError as exc:
        pytest.skip(f"Postgres is not available at configured DSN: {exc}")


def _cleanup_postgres(
    dsn: str,
    *,
    graph_id: str,
    runtime_session_id: str,
    governance_batch_id: str | None,
) -> None:
    with _connect_or_skip(dsn) as connection:
        with connection.cursor() as cursor:
            cursor.execute("delete from memory_write_outbox where graph_id = %s", (graph_id,))
            if governance_batch_id is None:
                cursor.execute(
                    "delete from memory_governance_decisions where governance_batch_id like %s",
                    ("governance:test:contradiction%",),
                )
            else:
                cursor.execute(
                    "delete from memory_governance_decisions where governance_batch_id = %s",
                    (governance_batch_id,),
                )
            cursor.execute("delete from graph_documents where graph_id = %s", (graph_id,))
            cursor.execute("delete from memory_nodes where graph_id = %s", (graph_id,))
            cursor.execute("delete from memory_relations where graph_id = %s", (graph_id,))
            cursor.execute("delete from sessions where id = %s", (runtime_session_id,))
