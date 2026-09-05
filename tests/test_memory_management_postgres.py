import json
from time import monotonic
from uuid import uuid4

import pytest

from pulsara_agent.conversation_kernel.memory.contracts import (
    FrozenMemoryGovernanceDecision,
    MemoryDecisionKind,
    MemoryFactKind,
    MemoryKindHint,
    MemorySupersedeMode,
)
from pulsara_agent.conversation_kernel.memory.management import (
    MemoryManagementSelection,
    MemoryManagementError,
)
from pulsara_agent.storage.postgres_connection_provider import PostgresConnectionLane
from tests.test_round8_advisory_memory import (
    _repository,
    _lease,
    _claim_candidate,
    _settle,
)
from tests.support.model_config import acquire_bound_test_writer

pytestmark = pytest.mark.postgres


@pytest.fixture
def memory_graph(stage2_migrated_postgres_database):
    repository = _repository(stage2_migrated_postgres_database)
    domain = "memory_management_" + uuid4().hex
    lease = acquire_bound_test_writer(
        repository,
        session_id="session:" + uuid4().hex,
        workspace_id="transient:management",
        workspace_kind="transient",
        workspace_root="/tmp/pulsara-memory-management",
        workspace_label="快速开始",
        memory_domain_id=domain,
        writer_owner_id="host:" + uuid4().hex,
        lease_seconds=30,
        deadline_monotonic=monotonic() + 30,
    )

    def accept(
        text,
        *,
        kind="FACT",
        based_on=(),
        target=None,
        relation=None,
        context="ctx:global",
        mode=None,
    ):
        active_lease = (
            lease
            if context == "ctx:global"
            else _lease(repository, workspace_id=context, domain=domain)
        )
        repository.renew_host_writer(
            active_lease.guard, lease_seconds=30, deadline_monotonic=monotonic() + 30
        )
        candidate = _claim_candidate(
            repository,
            active_lease,
            statement=text,
            kind_hint=MemoryKindHint(kind),
            based_on=tuple(based_on),
            domain=domain,
            context_id=context,
        )
        decision = FrozenMemoryGovernanceDecision(
            decision_kind=MemoryDecisionKind(
                "ACCEPT" if relation is None else "ACCEPT_AND_" + relation
            ),
            final_kind=MemoryFactKind(kind),
            related_target_fact_id=target,
            supersede_mode=MemorySupersedeMode(mode or "SAME_KIND_REPLACEMENT")
            if relation == "SUPERSEDE"
            else None,
            public_summary="根据对话中表达的背景整理。",
        )
        _, result = _settle(repository, active_lease, candidate, decision)
        return result.fact_id or result.duplicate_winner_fact_id

    return repository, domain, accept


def preview(repository, domain, root, additional=()):
    return repository.memory_deletion_preview(
        memory_domain_id=domain,
        selection=MemoryManagementSelection(),
        fact_id=root,
        additional=additional,
        deadline_monotonic=monotonic() + 30,
    )


def execute(repository, domain, root, confirmation, additional=()):
    return repository.execute_memory_deletion(
        memory_domain_id=domain,
        selection=MemoryManagementSelection(),
        fact_id=root,
        additional=additional,
        expected_records=lambda: iter(confirmation),
        deadline_monotonic=monotonic() + 30,
    )


@pytest.mark.parametrize(
    "relation,delete_new",
    [
        ("BASIS", True),
        ("BASIS", False),
        ("SUPERSEDE", True),
        ("SUPERSEDE", False),
        ("CONTRADICT", True),
        ("CONTRADICT", False),
    ],
)
def test_real_single_edge_deletion_and_transcript_preservation(
    memory_graph, relation, delete_new
):
    repo, domain, accept = memory_graph
    old = accept("之前的背景 " + uuid4().hex)
    new = accept(
        "新的背景 " + uuid4().hex,
        based_on=(old,) if relation == "BASIS" else (),
        target=None if relation == "BASIS" else old,
        relation=None if relation == "BASIS" else relation,
    )
    root = new if delete_new else old
    confirmation = preview(repo, domain, root)
    assert json.loads(confirmation[0])["disposition"] == "READY"
    with repo.connection_provider.connection(
        lane=PostgresConnectionLane.MEMORY_QUERY, deadline_monotonic=monotonic() + 30
    ) as c:
        before = c.execute(
            "SELECT count(*) FROM pulsara_v3.transcript_entries"
        ).fetchone()[0]
    result = execute(repo, domain, root, confirmation)
    assert json.loads(result[0])["result"] == "DELETED"
    with repo.connection_provider.connection(
        lane=PostgresConnectionLane.MEMORY_QUERY, deadline_monotonic=monotonic() + 30
    ) as c:
        rows = c.execute(
            "SELECT id,lifecycle FROM pulsara_v3.memory_facts WHERE memory_domain_id=%s",
            (domain,),
        ).fetchall()
        assert (
            c.execute("SELECT count(*) FROM pulsara_v3.transcript_entries").fetchone()[
                0
            ]
            == before
        )
        assert (
            c.execute(
                "SELECT count(*) FROM pulsara_v3.memory_relations WHERE memory_domain_id=%s",
                (domain,),
            ).fetchone()[0]
            == 0
        )
    expected = (
        {}
        if relation == "BASIS" and not delete_new
        else {(old if delete_new else new): "ACTIVE"}
    )
    assert dict(rows) == expected


def test_chain_middle_requires_explicit_resolution(memory_graph):
    repo, domain, accept = memory_graph
    c = accept("第一版")
    b = accept("第二版", target=c, relation="SUPERSEDE")
    a = accept("第三版", target=b, relation="SUPERSEDE")
    confirmation = preview(repo, domain, b)
    records = [json.loads(r) for r in confirmation]
    assert records[0]["disposition"] == "NEEDS_RESOLUTION"
    assert any(r.get("reason") == "SURVIVING_SUPERSEDE_ANCESTRY" for r in records)
    with pytest.raises(MemoryManagementError) as error:
        execute(repo, domain, b, confirmation)
    assert error.value.status == 409
    confirmation = preview(repo, domain, b, (c,))
    execute(repo, domain, b, confirmation, (c,))
    assert (
        repo.memory_management_catalog(
            memory_domain_id=domain,
            selection=MemoryManagementSelection(),
            deadline_monotonic=monotonic() + 30,
        )["items"][0]["fact_id"]
        == a
    )


def test_preview_drift_includes_new_dependent_and_never_partially_deletes(memory_graph):
    repo, domain, accept = memory_graph
    basis = accept("共同背景")
    confirmation = preview(repo, domain, basis)
    dependent = accept("后来的相关决定", based_on=(basis,))
    with pytest.raises(MemoryManagementError) as error:
        execute(repo, domain, basis, confirmation)
    assert error.value.code == "MEMORY_DELETION_PLAN_DRIFTED"
    assert {
        r["fact"]["fact_id"]
        for r in map(json.loads, error.value.preview)
        if r["type"] == "FACT_DELETE"
    } == {basis, dependent}
    page = repo.memory_management_catalog(
        memory_domain_id=domain,
        selection=MemoryManagementSelection(),
        deadline_monotonic=monotonic() + 30,
    )
    assert len(page["items"]) == 2


def test_detail_bidirectional_conflict_and_literal_search(memory_graph):
    repo, domain, accept = memory_graph
    a = accept("literal %_\\ 中文")
    b = accept("另一条内容", target=a, relation="CONTRADICT")
    for selected, companion in ((a, b), (b, a)):
        detail = repo.memory_management_detail(
            memory_domain_id=domain,
            selection=MemoryManagementSelection(),
            fact_id=selected,
            provenance_workspace_id="transient:management",
            deadline_monotonic=monotonic() + 30,
        )
        assert detail["fact"]["needs_confirmation"]
        assert detail["relations"][0]["relative_role"] == "CONFLICTS_WITH"
        assert detail["relations"][0]["companion"]["fact_id"] == companion
        assert detail["source"] is not None
    page = repo.memory_management_catalog(
        memory_domain_id=domain,
        selection=MemoryManagementSelection(),
        search=" %_\\ ",
        deadline_monotonic=monotonic() + 30,
    )
    assert [r["fact_id"] for r in page["items"]] == [a]


def test_concurrent_double_delete_commits_only_once(memory_graph):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier

    repo, domain, accept = memory_graph
    root = accept("并发删除同一条")
    confirmation = preview(repo, domain, root)
    barrier = Barrier(2)

    def attempt():
        barrier.wait(timeout=10)
        try:
            return execute(repo, domain, root, confirmation)
        except MemoryManagementError as exc:
            return exc

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(lambda _: attempt(), range(2)))
    assert sum(isinstance(outcome, tuple) for outcome in outcomes) == 1
    failure = next(
        outcome for outcome in outcomes if isinstance(outcome, MemoryManagementError)
    )
    assert failure.code == "MEMORY_NOT_FOUND"


def test_dependency_inserted_after_snapshot_is_replanned_not_silently_lost(
    memory_graph, monkeypatch
):
    repo, domain, accept = memory_graph
    root = accept("正在被引用的依据")
    confirmation = preview(repo, domain, root)
    original = repo._memory_deletion_plan
    inserted = []

    def plan(*args, **kwargs):
        result = original(*args, **kwargs)
        if not inserted:
            inserted.append(accept("在删除 snapshot 之后新增的依赖", based_on=(root,)))
        return result

    monkeypatch.setattr(repo, "_memory_deletion_plan", plan)
    with pytest.raises(MemoryManagementError) as error:
        execute(repo, domain, root, confirmation)
    assert error.value.code == "MEMORY_DELETION_PLAN_DRIFTED"
    assert {
        r["fact"]["fact_id"]
        for r in map(json.loads, error.value.preview)
        if r["type"] == "FACT_DELETE"
    } == {root, inserted[0]}


def test_deleted_processing_basis_candidate_cannot_be_accepted_later(memory_graph):
    from datetime import datetime, timezone
    from pulsara_agent.conversation_kernel.memory.contracts import (
        prepare_memory_governance_acceptance,
    )
    from pulsara_agent.conversation_kernel.repository import ConversationKernelConflict

    repo, domain, accept = memory_graph
    root = accept("可撤回的依据")
    lease = _lease(repo, workspace_id="transient:pending", domain=domain)
    candidate = _claim_candidate(
        repo,
        lease,
        statement="尚未治理完的依赖",
        kind_hint=MemoryKindHint.FACT,
        based_on=(root,),
        domain=domain,
    )
    head = repo.read_memory_candidate_for_governance(
        lease.guard,
        candidate_id=candidate.candidate_id,
        deadline_monotonic=monotonic() + 30,
    )
    evidence = repo.read_memory_governance_evidence(
        lease.guard, candidate=head, deadline_monotonic=monotonic() + 30
    )
    prepared = prepare_memory_governance_acceptance(
        candidate=candidate,
        decision=FrozenMemoryGovernanceDecision(
            MemoryDecisionKind.ACCEPT,
            final_kind=MemoryFactKind.FACT,
            public_summary="依据已经明确",
        ),
        basis_items=evidence.basis_items,
        relation_targets=(),
    )
    execute(repo, domain, root, preview(repo, domain, root))
    with pytest.raises(ConversationKernelConflict):
        repo.accept_memory_governance(
            lease.guard,
            prepared=prepared,
            decided_at=datetime.now(timezone.utc),
            deadline_monotonic=monotonic() + 30,
        )
    assert not repo.memory_management_catalog(
        memory_domain_id=domain,
        selection=MemoryManagementSelection(),
        deadline_monotonic=monotonic() + 30,
    )["items"]


def test_expired_deadline_never_mutates(memory_graph):
    repo, domain, accept = memory_graph
    root = accept("保持存在")
    confirmation = preview(repo, domain, root)
    with pytest.raises(MemoryManagementError) as error:
        repo.execute_memory_deletion(
            memory_domain_id=domain,
            selection=MemoryManagementSelection(),
            fact_id=root,
            additional=(),
            expected_records=lambda: iter(confirmation),
            deadline_monotonic=monotonic() - 1,
        )
    assert error.value.status == 504
    assert preview(repo, domain, root) == confirmation


def test_cross_project_basis_diamond_cascade(memory_graph):
    repo, domain, accept = memory_graph
    root = accept("跨对话共同背景")
    left = accept("项目甲的约定", context="ctx:workspace/alpha", based_on=(root,))
    right = accept("项目乙的决定", context="ctx:workspace/beta", based_on=(root,))
    leaf = accept(
        "甲的进一步决定", context="ctx:workspace/alpha", based_on=(root, left)
    )
    independent = accept("不相关背景")
    confirmation = preview(repo, domain, root)
    assert {
        r["fact"]["fact_id"]
        for r in map(json.loads, confirmation)
        if r["type"] == "FACT_DELETE"
    } == {root, left, right, leaf}
    execute(repo, domain, root, confirmation)
    page = repo.memory_management_catalog(
        memory_domain_id=domain,
        selection=MemoryManagementSelection(),
        deadline_monotonic=monotonic() + 30,
    )
    assert [r["fact_id"] for r in page["items"]] == [independent]


def test_duplicate_restoration_requires_explicit_old_fact_deletion(memory_graph):
    repo, domain, accept = memory_graph
    old = accept("相同背景")
    new = accept("更新的背景", relation="SUPERSEDE", target=old)
    winner = accept("相同背景")
    confirmation = preview(repo, domain, new)
    assert any(
        r.get("reason") == "ACTIVE_SEMANTIC_COLLISION"
        for r in map(json.loads, confirmation)
    )
    confirmation = preview(repo, domain, new, (old,))
    execute(repo, domain, new, confirmation, (old,))
    assert (
        repo.memory_management_catalog(
            memory_domain_id=domain,
            selection=MemoryManagementSelection(),
            deadline_monotonic=monotonic() + 30,
        )["items"][0]["fact_id"]
        == winner
    )


def test_applied_to_existing_owner_deleted_without_deleting_source(memory_graph):
    repo, domain, accept = memory_graph
    old = accept("四川菜爱好", kind="RESPONSE_PREFERENCE")
    actual = accept("四川菜爱好", kind="USER_PROFILE")
    applied = accept(
        "四川菜爱好",
        kind="USER_PROFILE",
        relation="SUPERSEDE",
        target=old,
        mode="TAXONOMY_CORRECTION",
    )
    assert applied == actual
    execute(repo, domain, old, preview(repo, domain, old))
    with repo.connection_provider.connection(
        lane=PostgresConnectionLane.MEMORY_QUERY, deadline_monotonic=monotonic() + 30
    ) as c:
        assert c.execute(
            "SELECT id FROM pulsara_v3.memory_facts WHERE memory_domain_id=%s",
            (domain,),
        ).fetchall() == [(actual,)]
        assert c.execute(
            "SELECT status FROM pulsara_v3.memory_candidates WHERE memory_domain_id=%s",
            (domain,),
        ).fetchall() == [("ACCEPTED",)]


def test_restore_response_preference_capacity_is_not_silent_eviction(memory_graph):
    repo, domain, accept = memory_graph
    old = accept("曾经的答复偏好", kind="RESPONSE_PREFERENCE")
    new = accept(
        "这是人物爱好而非答复格式",
        kind="USER_PROFILE",
        target=old,
        relation="SUPERSEDE",
        mode="TAXONOMY_CORRECTION",
    )
    for i in range(16):
        assert accept(f"回答格式偏好{i}", kind="RESPONSE_PREFERENCE")
    confirmation = preview(repo, domain, new)
    assert any(
        r.get("reason") == "RESPONSE_PREFERENCE_CAPACITY"
        for r in map(json.loads, confirmation)
    )
    with pytest.raises(MemoryManagementError):
        execute(repo, domain, new, confirmation)
    assert preview(repo, domain, new) == confirmation


def test_catalog_keyset_reads_beyond_model_recall_without_duplicate_rows(memory_graph):
    repo, domain, accept = memory_graph
    root = accept("可管理背景 0")
    expected = {root} | {
        accept(f"可管理背景 {i}", based_on=(root,)) for i in range(1, 105)
    }
    seen = []
    cursor = None
    while True:
        page = repo.memory_management_catalog(
            memory_domain_id=domain,
            selection=MemoryManagementSelection(),
            limit=17,
            cursor=cursor,
            deadline_monotonic=monotonic() + 30,
        )
        seen.extend(f["fact_id"] for f in page["items"])
        cursor = page["next_cursor"]
        if cursor is None:
            break
    assert len(seen) == len(set(seen)) == 105
    assert set(seen) == expected
    companions = []
    cursor = None
    while True:
        detail = repo.memory_management_detail(
            memory_domain_id=domain,
            selection=MemoryManagementSelection(),
            fact_id=root,
            provenance_workspace_id="transient:management",
            limit=31,
            cursor=cursor,
            deadline_monotonic=monotonic() + 30,
        )
        companions.extend(r["companion"]["fact_id"] for r in detail["relations"])
        cursor = detail["next_cursor"]
        if cursor is None:
            break
    assert len(companions) == len(set(companions)) == 104
    assert set(companions) == expected - {root}


def test_next_preference_freeze_observes_deletion_without_mutating_old_frozen_source(
    memory_graph,
):
    import asyncio
    from types import SimpleNamespace
    from tests.test_round8_advisory_memory import (
        KernelSessionIO,
        freeze_memory_read_context_binding,
        MemoryDomainContext,
        KernelMemoryToolPort,
        EmbeddingBackendConfig,
        AdvisoryMemoryFeatureConfig,
        LocalSettings,
    )

    repo, domain, accept = memory_graph
    root = accept("先给简短结论", kind="RESPONSE_PREFERENCE")
    io = KernelSessionIO()
    port = KernelMemoryToolPort(
        repository=repo,
        session_id="management-observer",
        read_binding=freeze_memory_read_context_binding(
            domain=MemoryDomainContext(domain, "transient"),
            host_workspace_id="transient:management",
        ),
        embedding_config=EmbeddingBackendConfig(),
        feature_config=AdvisoryMemoryFeatureConfig(
            automatic_dense=False, explicit_rerank=False
        ),
        io_owner=io,
        settings=SimpleNamespace(read=lambda: LocalSettings()),
    )

    async def run():
        try:
            before = await port.freeze_response_preference_source()
            before_text = before.variants[0].text
            execute(repo, domain, root, preview(repo, domain, root))
            after = await port.freeze_response_preference_source()
            assert before.model_visible_memory_fact_ids == (root,)
            assert before.variants[0].text == before_text
            assert after.absence_kind.value == "EXPLICIT_EMPTY"
        finally:
            await port.aclose()
            await io.aclose(deadline_monotonic=monotonic() + 30)

    asyncio.run(run())


def test_source_fence_and_project_catalog_does_not_resolve_removed_directory(
    memory_graph,
):
    repo, domain, accept = memory_graph
    root = accept("项目背景", context="ctx:workspace/removed-project")
    project = repo.memory_management_projects(
        memory_domain_id=domain, deadline_monotonic=monotonic() + 30
    )
    assert "ctx:workspace/removed-project" in {
        p["workspace_id"] for p in project["items"]
    }
    detail = repo.memory_management_detail(
        memory_domain_id=domain,
        selection=MemoryManagementSelection("project", "ctx:workspace/removed-project"),
        fact_id=root,
        provenance_workspace_id="other-workspace",
        deadline_monotonic=monotonic() + 30,
    )
    assert detail["source"] is not None
    global_fact = accept("全局背景")
    detail = repo.memory_management_detail(
        memory_domain_id=domain,
        selection=MemoryManagementSelection(),
        fact_id=global_fact,
        provenance_workspace_id="other-workspace",
        deadline_monotonic=monotonic() + 30,
    )
    assert detail["source"] is None
    with pytest.raises(MemoryManagementError):
        repo.memory_management_detail(
            memory_domain_id="other-domain",
            selection=MemoryManagementSelection(),
            fact_id=global_fact,
            provenance_workspace_id="transient:management",
            deadline_monotonic=monotonic() + 30,
        )
