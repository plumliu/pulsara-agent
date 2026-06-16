from __future__ import annotations

import asyncio
from uuid import uuid4

import psycopg
import pytest

from pulsara_agent.entities.memory import Claim, Preference
from pulsara_agent.event import EventContext, EventType, TextBlockDeltaEvent
from pulsara_agent.event.candidates import ClaimCandidate, PreferenceCandidate, ValidCandidatePayload
from pulsara_agent.event_log import InMemoryEventLog, PostgresEventLog
from pulsara_agent.graph import InMemoryGraphStore, PostgresGraphStore
from pulsara_agent.jsonld import utc_now
from pulsara_agent.memory import (
    InMemoryArchiveStore,
    InMemoryCandidatePool,
    MemoryGovernanceExecutor,
    MemoryWriteUnitOfWork,
    PostgresCandidatePool,
    PostgresMemoryQuery,
    SupersedeAndSubmitDecision,
    WriteFailedOutcome,
    WriteSucceededOutcome,
)
from pulsara_agent.memory.candidates.pool import CandidateOrigin, CorrectAndSubmitDecision, PooledMemoryCandidate
from pulsara_agent.memory.canonical.index_sync import MemorySearchIndexSync
from pulsara_agent.memory.canonical.ledger import ExecutionEvidenceLedger
from pulsara_agent.memory.canonical.write_gate import MemoryWriteGate
from pulsara_agent.memory.canonical.write_service import MemoryWriteService
from pulsara_agent.memory.governance.dedupe import already_exists
from pulsara_agent.memory.governance.executor import _SUPERSEDE_DOWNGRADE_SENTINEL, _jsonld_type_names
from pulsara_agent.memory.recall.explain import ClaimKind, explain_memory
from pulsara_agent.memory.recall.service import LexicalMemoryRecallService, RecallQuery, RecallStatus
from pulsara_agent.ontology import memory
from pulsara_agent.settings import StorageConfig


def test_supersede_decision_facade_export_and_round_trip() -> None:
    decision = SupersedeAndSubmitDecision(
        target_entry_id="pool:test",
        candidate=_preference_candidate("candidate:new", "The user prefers concise summaries."),
        superseded_memory_ids=("preference:old",),
        reason="User explicitly replaced the prior preference.",
    )

    assert decision.kind == "supersede_and_submit"


def test_postgres_governance_supersede_writes_new_retires_old_and_records_outcome(tmp_path) -> None:
    dsn = StorageConfig.from_env().postgres_dsn
    _connect_or_skip(dsn).close()
    runtime_session_id = f"runtime:test:{uuid4().hex}"
    graph_id = f"graph:test/{uuid4().hex}"
    batch_id = f"governance:test:supersede:{uuid4().hex}"
    source_ctx = _source_context("supersede")
    store = PostgresGraphStore(dsn=dsn)
    query = PostgresMemoryQuery(dsn=dsn)
    log = PostgresEventLog(dsn=dsn, runtime_session_id=runtime_session_id, workspace_root=tmp_path)
    log.append(TextBlockDeltaEvent(**source_ctx.event_fields(), block_id="text:seed", delta="seed"))
    pool = PostgresCandidatePool(dsn=dsn)
    old_id = "preference:test-supersede-old"
    try:
        store.put_jsonld(_preference_doc(old_id, "The user prefers verbose summaries."), graph_id=graph_id)
        pooled = pool.append_candidate(
            _pooled_valid(
                runtime_session_id=runtime_session_id,
                source_ctx=source_ctx,
                candidate=_preference_candidate("candidate:new", "The user prefers concise summaries."),
            )
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
            SupersedeAndSubmitDecision(
                target_entry_id=pooled.entry_id,
                candidate=_preference_candidate("candidate:new", "The user prefers concise summaries."),
                superseded_memory_ids=(old_id,),
                reason="The user explicitly changed the summary preference.",
            ),
            governance_batch_id=batch_id,
        )

        assert isinstance(result.decision_record.decision, SupersedeAndSubmitDecision)
        assert isinstance(result.decision_record.write_outcome, WriteSucceededOutcome)
        new_id = result.decision_record.write_outcome.memory_id
        assert result.decision_record.write_outcome.superseded_memory_ids == (old_id,)
        assert [event.type for event in result.events] == [
            EventType.MEMORY_CANDIDATE_PROPOSED,
            EventType.MEMORY_WRITE_RESULT,
            EventType.MEMORY_SUPERSEDED,
        ]
        old_doc = store.get_jsonld(old_id, graph_id=graph_id)
        new_doc = store.get_jsonld(new_id, graph_id=graph_id)
        assert old_doc[memory.STATUS.name] == memory.NodeStatus.SUPERSEDED.value
        assert {"@id": old_id} in new_doc[memory.SUPERSEDES.name]
        assert _governance_candidate_count(pool) == 0
        assert pool.list_pending() == []

        fetched = {view.id: view for view in query.fetch_nodes([old_id, new_id], graph_id=graph_id)}
        assert fetched[old_id].status is memory.NodeStatus.SUPERSEDED
        assert (memory.SUPERSEDES.name, old_id) in fetched[new_id].outgoing
        old_explanation = explain_memory(fetched[old_id])
        assert any(claim.kind is ClaimKind.SUPERSEDED_BY for claim in old_explanation.claims)
    finally:
        _cleanup_postgres(dsn, graph_id=graph_id, runtime_session_id=runtime_session_id, governance_batch_id=batch_id)


def test_postgres_superseded_old_memory_is_filtered_from_recall(tmp_path) -> None:
    dsn = StorageConfig.from_env().postgres_dsn
    _connect_or_skip(dsn).close()
    runtime_session_id = f"runtime:test:{uuid4().hex}"
    graph_id = f"graph:test/{uuid4().hex}"
    batch_id = f"governance:test:recall-supersede:{uuid4().hex}"
    source_ctx = _source_context("recall")
    store = PostgresGraphStore(dsn=dsn)
    pool = PostgresCandidatePool(dsn=dsn)
    log = PostgresEventLog(dsn=dsn, runtime_session_id=runtime_session_id, workspace_root=tmp_path)
    log.append(TextBlockDeltaEvent(**source_ctx.event_fields(), block_id="text:seed", delta="seed"))
    old_id = "preference:test-supersede-recall-old"
    try:
        store.put_jsonld(_preference_doc(old_id, "The user prefers verbose summaries."), graph_id=graph_id)
        pooled = pool.append_candidate(
            _pooled_valid(
                runtime_session_id=runtime_session_id,
                source_ctx=source_ctx,
                candidate=_preference_candidate("candidate:new", "The user prefers concise summaries."),
            )
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
            SupersedeAndSubmitDecision(
                target_entry_id=pooled.entry_id,
                candidate=_preference_candidate("candidate:new", "The user prefers concise summaries."),
                superseded_memory_ids=(old_id,),
                reason="Explicit replacement.",
            ),
            governance_batch_id=batch_id,
        )
        assert isinstance(result.decision_record.write_outcome, WriteSucceededOutcome)
        new_id = result.decision_record.write_outcome.memory_id
        MemorySearchIndexSync(dsn=dsn).rebuild(graph_id=graph_id)

        recall = asyncio.run(
            LexicalMemoryRecallService(PostgresMemoryQuery(dsn=dsn)).recall(
                RecallQuery(text="summaries preference", scopes=("ctx:user",)),
                graph_id=graph_id,
            )
        )

        assert recall.status is RecallStatus.OK
        assert [item.memory_id for item in recall.items] == [new_id]
        assert old_id not in [item.memory_id for item in recall.items]
    finally:
        _cleanup_postgres(dsn, graph_id=graph_id, runtime_session_id=runtime_session_id, governance_batch_id=batch_id)


def test_postgres_supersede_downgrades_on_scope_mismatch_and_skips_audit_candidate(tmp_path) -> None:
    dsn = StorageConfig.from_env().postgres_dsn
    _connect_or_skip(dsn).close()
    runtime_session_id = f"runtime:test:{uuid4().hex}"
    graph_id = f"graph:test/{uuid4().hex}"
    batch_id = f"governance:test:supersede-scope:{uuid4().hex}"
    source_ctx = _source_context("scope")
    store = PostgresGraphStore(dsn=dsn)
    pool = PostgresCandidatePool(dsn=dsn)
    log = PostgresEventLog(dsn=dsn, runtime_session_id=runtime_session_id, workspace_root=tmp_path)
    log.append(TextBlockDeltaEvent(**source_ctx.event_fields(), block_id="text:seed", delta="seed"))
    old_id = "preference:test-scope-old"
    try:
        store.put_jsonld(_preference_doc(old_id, "The user prefers verbose summaries.", scope="ctx:project"), graph_id=graph_id)
        pooled = pool.append_candidate(
            _pooled_valid(
                runtime_session_id=runtime_session_id,
                source_ctx=source_ctx,
                candidate=_preference_candidate("candidate:new", "The user prefers concise summaries.", scope="ctx:user"),
            )
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
            SupersedeAndSubmitDecision(
                target_entry_id=pooled.entry_id,
                candidate=_preference_candidate("candidate:new", "The user prefers concise summaries.", scope="ctx:user"),
                superseded_memory_ids=(old_id,),
                reason="Bad scope target.",
            ),
            governance_batch_id=batch_id,
        )

        assert isinstance(result.decision_record.decision, CorrectAndSubmitDecision)
        assert result.decision_record.decision.reason.startswith(_SUPERSEDE_DOWNGRADE_SENTINEL)
        assert isinstance(result.decision_record.write_outcome, WriteSucceededOutcome)
        assert result.decision_record.write_outcome.superseded_memory_ids == ()
        assert store.get_jsonld(old_id, graph_id=graph_id)[memory.STATUS.name] == memory.NodeStatus.ACTIVE.value
        new_doc = store.get_jsonld(result.decision_record.write_outcome.memory_id, graph_id=graph_id)
        assert memory.SUPERSEDES.name not in new_doc
        assert _governance_candidate_count(pool) == 0
    finally:
        _cleanup_postgres(dsn, graph_id=graph_id, runtime_session_id=runtime_session_id, governance_batch_id=batch_id)


def test_postgres_supersede_downgrades_non_preference_multi_target_missing_and_inactive(tmp_path) -> None:
    dsn = StorageConfig.from_env().postgres_dsn
    _connect_or_skip(dsn).close()
    runtime_session_id = f"runtime:test:{uuid4().hex}"
    graph_id = f"graph:test/{uuid4().hex}"
    source_ctx = _source_context("gates")
    store = PostgresGraphStore(dsn=dsn)
    pool = PostgresCandidatePool(dsn=dsn)
    log = PostgresEventLog(dsn=dsn, runtime_session_id=runtime_session_id, workspace_root=tmp_path)
    log.append(TextBlockDeltaEvent(**source_ctx.event_fields(), block_id="text:seed", delta="seed"))
    active_old = "preference:test-gates-active"
    inactive_old = "preference:test-gates-inactive"
    claim_old = "claim:test-gates-claim"
    try:
        store.put_jsonld(_preference_doc(active_old, "The user prefers verbose summaries."), graph_id=graph_id)
        store.put_jsonld(
            _preference_doc(
                inactive_old,
                "The user prefers very verbose summaries.",
                status=memory.NodeStatus.SUPERSEDED,
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
                _claim_candidate("candidate:claim", "The repository now uses Rust."),
                (claim_old,),
                "type_not_supersedable",
            ),
            (
                _preference_candidate("candidate:multi", "The user prefers compact summaries."),
                (active_old, inactive_old),
                "too_many_supersede_targets",
            ),
            (
                _preference_candidate("candidate:missing", "The user prefers short status updates."),
                ("preference:missing",),
                "supersede_target_missing",
            ),
            (
                _preference_candidate("candidate:inactive", "The user prefers brief plans."),
                (inactive_old,),
                "supersede_target_not_active",
            ),
            (
                _preference_candidate("candidate:claim-target", "The user prefers terse commit messages."),
                (claim_old,),
                "supersede_target_type_not_supersedable",
            ),
        ]
        for candidate, old_ids, reason_prefix in cases:
            batch_id = f"governance:test:gates:{uuid4().hex}"
            pooled = pool.append_candidate(
                _pooled_valid(runtime_session_id=runtime_session_id, source_ctx=source_ctx, candidate=candidate)
            )
            result = executor.apply_decision(
                SupersedeAndSubmitDecision(
                    target_entry_id=pooled.entry_id,
                    candidate=candidate,
                    superseded_memory_ids=old_ids,
                    reason="Proposed supersede should downgrade.",
                ),
                governance_batch_id=batch_id,
            )

            assert isinstance(result.decision_record.decision, CorrectAndSubmitDecision)
            assert reason_prefix in result.decision_record.decision.reason
            assert isinstance(result.decision_record.write_outcome, WriteSucceededOutcome)
            assert result.decision_record.write_outcome.superseded_memory_ids == ()
        assert store.get_jsonld(active_old, graph_id=graph_id)[memory.STATUS.name] == memory.NodeStatus.ACTIVE.value
        assert store.get_jsonld(inactive_old, graph_id=graph_id)[memory.STATUS.name] == memory.NodeStatus.SUPERSEDED.value
        assert store.get_jsonld(claim_old, graph_id=graph_id)[memory.STATUS.name] == memory.NodeStatus.ACTIVE.value
    finally:
        _cleanup_postgres(dsn, graph_id=graph_id, runtime_session_id=runtime_session_id, governance_batch_id=None)


def test_postgres_supersede_downgrades_when_new_node_is_not_active(tmp_path) -> None:
    dsn = StorageConfig.from_env().postgres_dsn
    _connect_or_skip(dsn).close()
    runtime_session_id = f"runtime:test:{uuid4().hex}"
    graph_id = f"graph:test/{uuid4().hex}"
    batch_id = f"governance:test:supersede-non-active:{uuid4().hex}"
    source_ctx = _source_context("non-active")
    store = PostgresGraphStore(dsn=dsn)
    pool = PostgresCandidatePool(dsn=dsn)
    log = PostgresEventLog(dsn=dsn, runtime_session_id=runtime_session_id, workspace_root=tmp_path)
    log.append(TextBlockDeltaEvent(**source_ctx.event_fields(), block_id="text:seed", delta="seed"))
    old_id = "preference:test-non-active-old"
    try:
        store.put_jsonld(_preference_doc(old_id, "The user prefers verbose summaries."), graph_id=graph_id)
        candidate = _preference_candidate(
            "candidate:needs-review",
            "The user might prefer concise summaries.",
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
            SupersedeAndSubmitDecision(
                target_entry_id=pooled.entry_id,
                candidate=candidate,
                superseded_memory_ids=(old_id,),
                reason="Should not retire with non-active replacement.",
            ),
            governance_batch_id=batch_id,
        )

        assert isinstance(result.decision_record.decision, CorrectAndSubmitDecision)
        assert isinstance(result.decision_record.write_outcome, WriteSucceededOutcome)
        assert result.decision_record.write_outcome.node_status is memory.NodeStatus.NEEDS_REVIEW
        assert result.decision_record.write_outcome.superseded_memory_ids == ()
        assert store.get_jsonld(old_id, graph_id=graph_id)[memory.STATUS.name] == memory.NodeStatus.ACTIVE.value
    finally:
        _cleanup_postgres(dsn, graph_id=graph_id, runtime_session_id=runtime_session_id, governance_batch_id=batch_id)


def test_postgres_supersede_write_failure_does_not_retire_old_or_record_supersede(tmp_path) -> None:
    dsn = StorageConfig.from_env().postgres_dsn
    _connect_or_skip(dsn).close()
    runtime_session_id = f"runtime:test:{uuid4().hex}"
    graph_id = f"graph:test/{uuid4().hex}"
    batch_id = f"governance:test:supersede-write-failed:{uuid4().hex}"
    source_ctx = _source_context("write-failed")
    store = PostgresGraphStore(dsn=dsn)
    pool = PostgresCandidatePool(dsn=dsn)
    log = PostgresEventLog(dsn=dsn, runtime_session_id=runtime_session_id, workspace_root=tmp_path)
    log.append(TextBlockDeltaEvent(**source_ctx.event_fields(), block_id="text:seed", delta="seed"))
    old_id = "preference:test-write-failed-old"
    try:
        store.put_jsonld(_preference_doc(old_id, "The user prefers verbose summaries."), graph_id=graph_id)
        candidate = _preference_candidate(
            "candidate:missing-evidence",
            "The user prefers concise summaries.",
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
            SupersedeAndSubmitDecision(
                target_entry_id=pooled.entry_id,
                candidate=candidate,
                superseded_memory_ids=(old_id,),
                reason="Write should fail before retirement.",
            ),
            governance_batch_id=batch_id,
        )

        assert isinstance(result.decision_record.decision, CorrectAndSubmitDecision)
        assert isinstance(result.decision_record.write_outcome, WriteFailedOutcome)
        assert store.get_jsonld(old_id, graph_id=graph_id)[memory.STATUS.name] == memory.NodeStatus.ACTIVE.value
        assert _governance_candidate_count(pool) == 0
    finally:
        _cleanup_postgres(dsn, graph_id=graph_id, runtime_session_id=runtime_session_id, governance_batch_id=batch_id)


def test_postgres_supersede_rolls_back_when_lifecycle_fails_before_commit(tmp_path) -> None:
    dsn = StorageConfig.from_env().postgres_dsn
    _connect_or_skip(dsn).close()
    runtime_session_id = f"runtime:test:{uuid4().hex}"
    graph_id = f"graph:test/{uuid4().hex}"
    batch_id = f"governance:test:supersede-rollback:{uuid4().hex}"
    source_ctx = _source_context("rollback")
    store = PostgresGraphStore(dsn=dsn)
    pool = PostgresCandidatePool(dsn=dsn)
    log = PostgresEventLog(dsn=dsn, runtime_session_id=runtime_session_id, workspace_root=tmp_path)
    log.append(TextBlockDeltaEvent(**source_ctx.event_fields(), block_id="text:seed", delta="seed"))
    old_id = "preference:test-rollback-old"
    try:
        store.put_jsonld(_preference_doc(old_id, "The user prefers verbose summaries."), graph_id=graph_id)
        candidate = _preference_candidate("candidate:rollback", "The user prefers concise summaries.")
        pooled = pool.append_candidate(
            _pooled_valid(runtime_session_id=runtime_session_id, source_ctx=source_ctx, candidate=candidate)
        )
        executor = MemoryGovernanceExecutor(
            candidate_pool=pool,
            memory_write_service=_service_on(InMemoryGraphStore()),
            event_log=log,
            graph=InMemoryGraphStore(),
            runtime_session_id=runtime_session_id,
            memory_write_uow_factory=lambda: _FailingLifecycleUow(
                dsn=dsn,
                runtime_session_id=runtime_session_id,
                graph_id=graph_id,
                workspace_root=tmp_path,
            ),
        )

        with pytest.raises(RuntimeError, match="boom after lifecycle"):
            executor.apply_decision(
                SupersedeAndSubmitDecision(
                    target_entry_id=pooled.entry_id,
                    candidate=candidate,
                    superseded_memory_ids=(old_id,),
                    reason="Inject lifecycle failure before commit.",
                ),
                governance_batch_id=batch_id,
            )

        assert store.get_jsonld(old_id, graph_id=graph_id)[memory.STATUS.name] == memory.NodeStatus.ACTIVE.value
        with _connect_or_skip(dsn) as connection:
            with connection.cursor() as cursor:
                cursor.execute("select count(*) from memory_nodes where graph_id = %s", (graph_id,))
                assert cursor.fetchone()[0] == 1
    finally:
        _cleanup_postgres(dsn, graph_id=graph_id, runtime_session_id=runtime_session_id, governance_batch_id=batch_id)


def test_postgres_supersede_dedupe_skip_happens_before_retirement(tmp_path) -> None:
    dsn = StorageConfig.from_env().postgres_dsn
    _connect_or_skip(dsn).close()
    runtime_session_id = f"runtime:test:{uuid4().hex}"
    graph_id = f"graph:test/{uuid4().hex}"
    batch_id = f"governance:test:supersede-dedupe:{uuid4().hex}"
    source_ctx = _source_context("dedupe")
    store = PostgresGraphStore(dsn=dsn)
    pool = PostgresCandidatePool(dsn=dsn)
    log = PostgresEventLog(dsn=dsn, runtime_session_id=runtime_session_id, workspace_root=tmp_path)
    log.append(TextBlockDeltaEvent(**source_ctx.event_fields(), block_id="text:seed", delta="seed"))
    old_id = "preference:test-dedupe-old"
    try:
        store.put_jsonld(_preference_doc(old_id, "The user prefers concise summaries."), graph_id=graph_id)
        candidate = _preference_candidate("candidate:duplicate", "The user prefers concise summaries.")
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
            SupersedeAndSubmitDecision(
                target_entry_id=pooled.entry_id,
                candidate=candidate,
                superseded_memory_ids=(old_id,),
                reason="Incorrectly proposed supersede for duplicate.",
            ),
            governance_batch_id=batch_id,
        )

        assert result.decision_record.decision.kind == "skip"
        assert result.decision_record.write_outcome.kind == "no_write"
        assert store.get_jsonld(old_id, graph_id=graph_id)[memory.STATUS.name] == memory.NodeStatus.ACTIVE.value
    finally:
        _cleanup_postgres(dsn, graph_id=graph_id, runtime_session_id=runtime_session_id, governance_batch_id=batch_id)


def test_legacy_supersede_downgrades_to_coexist_without_audit_candidate() -> None:
    graph = InMemoryGraphStore()
    pool = InMemoryCandidatePool()
    log = InMemoryEventLog()
    service = _service_on(graph)
    old_outcome = service.submit(
        _preference_candidate("candidate:old", "The user prefers verbose summaries."),
        event_context=EventContext(run_id="run:old", turn_id="turn:old", reply_id="reply:old"),
    )
    old_id = next(event.memory_id for event in old_outcome.events if hasattr(event, "memory_id"))
    pooled = pool.append_candidate(
        _pooled_valid(
            runtime_session_id="runtime:test",
            source_ctx=_source_context("legacy"),
            candidate=_preference_candidate("candidate:new", "The user prefers concise summaries."),
        )
    )
    executor = MemoryGovernanceExecutor(
        candidate_pool=pool,
        memory_write_service=service,
        event_log=log,
        graph=graph,
        runtime_session_id="runtime:test",
    )

    result = executor.apply_decision(
        SupersedeAndSubmitDecision(
            target_entry_id=pooled.entry_id,
            candidate=_preference_candidate("candidate:new", "The user prefers concise summaries."),
            superseded_memory_ids=(old_id,),
            reason="Legacy cannot atomically supersede.",
        ),
        governance_batch_id="governance:test:legacy-supersede",
    )

    assert isinstance(result.decision_record.decision, CorrectAndSubmitDecision)
    assert result.decision_record.decision.reason.startswith(_SUPERSEDE_DOWNGRADE_SENTINEL)
    assert isinstance(result.decision_record.write_outcome, WriteSucceededOutcome)
    assert result.decision_record.write_outcome.superseded_memory_ids == ()
    assert graph.get_jsonld(old_id)[memory.STATUS.name] == memory.NodeStatus.ACTIVE.value
    assert _governance_candidate_count(pool) == 0


def test_related_dedupe_authority_remains_statement_exact() -> None:
    graph = InMemoryGraphStore()
    service = _service_on(graph)
    service.submit(
        _preference_candidate("candidate:old", "The user prefers verbose summaries."),
        event_context=EventContext(run_id="run:old", turn_id="turn:old", reply_id="reply:old"),
    )

    assert already_exists(
        _preference_candidate("candidate:new", "The user prefers concise summaries."),
        graph,
    ) is False
    assert already_exists(
        _preference_candidate("candidate:dup", "The user prefers verbose summaries."),
        graph,
    ) is True


def test_jsonld_type_names_accepts_compact_and_iri_types() -> None:
    assert _jsonld_type_names({"@type": ["Preference", memory.CLAIM.value]}) == {"Preference", "Claim"}


class _FailingLifecycleUow(MemoryWriteUnitOfWork):
    def __enter__(self):
        uow = super().__enter__()
        uow.lifecycle = _FailingLifecycle(uow.lifecycle)
        return uow


class _FailingLifecycle:
    def __init__(self, wrapped) -> None:
        self._wrapped = wrapped

    def supersede(self, **kwargs):
        self._wrapped.supersede(**kwargs)
        raise RuntimeError("boom after lifecycle")


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
        graph=InMemoryGraphStore(),
        runtime_session_id=runtime_session_id,
        memory_write_uow_factory=lambda: MemoryWriteUnitOfWork(
            dsn=dsn,
            runtime_session_id=runtime_session_id,
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
                    ("governance:test:%",),
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
