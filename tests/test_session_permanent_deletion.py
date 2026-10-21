"""Session deletion hard-cut: real constraints, independent memory, and fences."""

from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeout
from time import monotonic

import psycopg
from psycopg import sql
import pytest

from pulsara_agent.conversation_kernel._repository.deletion import SessionDeletionBusy
from pulsara_agent.conversation_kernel.repository import ConversationKernelConflict
from pulsara_agent.conversation_kernel.memory.contracts import MemoryRelationKind
from pulsara_agent.conversation_kernel.memory.management import (
    MemoryManagementSelection,
)
from pulsara_agent.conversation_kernel.memory.writes import PreparedMemoryRelationWrite
from pulsara_agent.storage.migrations.manifest import CONVERSATION_KERNEL_RELATIONS
from tests.test_conversation_fork import (
    repo as repo,
    new_session,
    turn,
    rows,
    fork,
    child_lease,
    identity,
)
from tests.test_direct_advisory_memory import _open_direct_memory_session


DIRECT = {
    "history_search_documents",
    "scheduled_tasks",
    "context_snapshots",
    "subagent_tasks",
    "turns",
    "session_commands",
    "imported_history_groups",
    "transcript_entries",
    "session_context_genesis",
    "assistant_message_blocks",
    "provider_assistant_replay_fragments",
    "tool_results",
    "prompt_queue_items",
    "canonical_image_refs",
    "assistant_visualizations",
    "interaction_decisions",
    "plan_workflows",
    "plan_interactions",
    "agent_events",
}
INDIRECT = {
    "provider_call_usage": ("turns", "turn_id"),
    "turn_context_binding_revisions": ("turns", "turn_id"),
    "tool_execution_attempts": ("assistant_message_blocks", "tool_call_id"),
    "imported_tool_call_closures": ("assistant_message_blocks", "tool_call_id"),
    "subagent_task_dependencies": ("subagent_tasks", "task_id"),
    "subagent_task_children": ("subagent_tasks", "task_id"),
}


def delete(repo, sid, guard=None, domain="u_local", *, required_lifecycle=None):
    return repo.delete_session(
        session_id=sid,
        memory_domain_id=domain,
        closed_writer=guard,
        deadline_monotonic=monotonic() + 30,
        required_lifecycle=required_lifecycle,
    )


def exists(repo, sid, domain="u_local"):
    return repo.canonical_session_exists(
        session_id=sid, memory_domain_id=domain, deadline_monotonic=monotonic() + 30
    )


def test_archived_condition_preserves_open_writer_and_deletes_only_archived(repo):
    from tests.test_session_archive import archive
    lease = new_session(repo)
    sid = lease.guard.session_id
    turn(repo, lease.guard, 'keep OPEN data')
    before = rows(repo, 'SELECT * FROM pulsara_v3.sessions WHERE id=%s', (sid,))
    assert delete(repo, sid, required_lifecycle='ARCHIVED') == 'NOT_ARCHIVED'
    assert rows(repo, 'SELECT * FROM pulsara_v3.sessions WHERE id=%s', (sid,)) == before
    assert delete(repo, sid, domain='other-domain', required_lifecycle='ARCHIVED') == 'ABSENT'
    assert archive(repo, lease) == 'ARCHIVED'
    assert delete(repo, sid, required_lifecycle='ARCHIVED') == 'DELETED'
    assert delete(repo, sid, required_lifecycle='ARCHIVED') == 'ABSENT'


def test_condition_waiting_for_unarchive_observes_committed_open(repo, stage2_migrated_postgres_database):
    from tests.test_session_archive import archive
    from threading import Event
    lease = new_session(repo)
    sid = lease.guard.session_id
    archive(repo, lease)
    entered = Event()
    def condition_delete():
        entered.set()
        return delete(repo, sid, required_lifecycle='ARCHIVED')
    with ThreadPoolExecutor(max_workers=1) as pool:
        with psycopg.connect(stage2_migrated_postgres_database.admin_dsn) as connection:
            connection.execute("UPDATE pulsara_v3.sessions SET lifecycle='OPEN' WHERE id=%s", (sid,))
            future = pool.submit(condition_delete)
            assert entered.wait(5)
            with pytest.raises(FutureTimeout):
                future.result(timeout=0.1)
            connection.commit()
        assert future.result(timeout=10) == 'NOT_ARCHIVED'
    assert exists(repo, sid)


def test_condition_keeps_existing_live_writer_boundary(repo, stage2_migrated_postgres_database):
    from tests.test_session_archive import archive
    lease = new_session(repo)
    sid = lease.guard.session_id
    archive(repo, lease)
    with psycopg.connect(stage2_migrated_postgres_database.admin_dsn) as connection:
        connection.execute("UPDATE pulsara_v3.sessions SET writer_lease_owner_id=%s,writer_lease_expires_at=clock_timestamp()+interval '30 seconds' WHERE id=%s", (identity('host'), sid))
    with pytest.raises(SessionDeletionBusy, match='live writer'):
        delete(repo, sid, required_lifecycle='ARCHIVED')
    assert exists(repo, sid)


def test_unarchive_waiting_for_condition_delete_cannot_resurrect(repo, monkeypatch):
    from threading import Event
    from tests.test_session_archive import archive
    import pulsara_agent.conversation_kernel._repository.deletion as deletion
    lease = new_session(repo)
    sid = lease.guard.session_id
    archive(repo, lease)
    locked, release = Event(), Event()
    original = deletion._sources
    def pause_after_lock(connection, session_id, domain, workspace):
        if session_id == sid:
            locked.set()
            assert release.wait(10)
        return original(connection, session_id, domain, workspace)
    monkeypatch.setattr(deletion, '_sources', pause_after_lock)
    with ThreadPoolExecutor(max_workers=2) as pool:
        deleting = pool.submit(delete, repo, sid, required_lifecycle='ARCHIVED')
        assert locked.wait(5)
        restoring = pool.submit(repo.unarchive_session, session_id=sid, memory_domain_id='u_local', deadline_monotonic=monotonic()+30)
        try:
            with pytest.raises(FutureTimeout):
                restoring.result(timeout=0.1)
        finally:
            release.set()
        assert deleting.result(timeout=10) == 'DELETED'
        with pytest.raises(KeyError):
            restoring.result(timeout=10)
    assert not exists(repo, sid)


def test_condition_replan_checks_new_lifecycle(repo, monkeypatch):
    from contextlib import contextmanager
    from tests.test_session_archive import archive
    import pulsara_agent.conversation_kernel._repository.deletion as deletion
    from dataclasses import replace
    lease = new_session(repo)
    sid = lease.guard.session_id
    archive(repo, lease)
    source_reads = 0
    original_sources = deletion._sources
    original_connection = repo.connection_provider.connection
    def drift(*args):
        nonlocal source_reads
        source_reads += 1
        plan = original_sources(*args)
        return replace(plan, workspace_ids=()) if source_reads == 2 else plan
    @contextmanager
    def connection(**kwargs):
        with original_connection(**kwargs) as current:
            yield current
        if source_reads == 2:
            repo.unarchive_session(session_id=sid, memory_domain_id='u_local', deadline_monotonic=monotonic()+30)
    monkeypatch.setattr(deletion, '_sources', drift)
    # Unarchive uses the same provider, so only install the replan callback once.
    @contextmanager
    def once_connection(**kwargs):
        with connection(**kwargs) as current:
            monkeypatch.setattr(repo.connection_provider, 'connection', original_connection)
            yield current
    monkeypatch.setattr(repo.connection_provider, 'connection', once_connection)
    assert delete(repo, sid, required_lifecycle='ARCHIVED') == 'NOT_ARCHIVED'
    assert source_reads == 2 and exists(repo, sid)


def test_catalog_ownership_semantic_edges_grants_and_index_coverage(repo):
    assert len(CONVERSATION_KERNEL_RELATIONS) == 31
    fks = rows(
        repo,
        """SELECT c.conrelid::regclass::text AS child,
        c.confrelid::regclass::text AS parent,c.confdeltype AS action,
        c.condeferrable AS deferred,c.condeferred AS initially_deferred,
        ARRAY(SELECT a.attname FROM unnest(c.conkey) WITH ORDINALITY k(n,o)
          JOIN pg_attribute a ON a.attrelid=c.conrelid AND a.attnum=k.n ORDER BY k.o) AS columns,
        EXISTS(SELECT 1 FROM pg_index i WHERE i.indrelid=c.conrelid
          AND i.indisvalid AND i.indpred IS NULL
          AND i.indkey[0]=ANY(c.conkey)) AS indexed
        FROM pg_constraint c WHERE c.contype='f' AND c.connamespace='pulsara_v3'::regnamespace""",
    )
    direct = {
        f["child"].split(".")[1] for f in fks if f["parent"] == "pulsara_v3.sessions"
    }
    assert direct == DIRECT
    owned = []
    for f in fks:
        child, parent = f["child"].split(".")[1], f["parent"].split(".")[1]
        ownership = parent == "sessions" or (
            child in INDIRECT
            and parent == INDIRECT[child][0]
            and INDIRECT[child][1] in f["columns"]
        )
        if ownership:
            assert f["action"] == "c", f
            # Equality on an FK column can use an existing leading index
            # column: e.g. globally unique task_id, or the session aggregate.
            # It need not duplicate every composite FK column in another index.
            assert f["indexed"], f
            owned.append(child)
        elif child in DIRECT | INDIRECT.keys() and parent in DIRECT | INDIRECT.keys():
            if child == "history_search_documents":
                # Disposable documents disappear with their canonical questions;
                # this is not a durable semantic edge that restrains deletion.
                assert parent == "transcript_entries"
                assert f["columns"] in (
                    ["session_id", "q1_entry_id"],
                    ["session_id", "q2_entry_id"],
                )
                assert (f["action"], f["deferred"], f["initially_deferred"]) == ("c", False, False)
                continue
            if (
                child in {"canonical_image_refs", "assistant_visualizations"}
                and f["action"] == "c"
            ):
                continue
            assert (f["action"], f["deferred"], f["initially_deferred"]) == (
                "a",
                True,
                True,
            ), f
        elif child in {"memory_facts", "memory_relations"} and parent == "tool_results":
            assert (f["action"], f["deferred"], f["initially_deferred"]) == (
                "n",
                True,
                True,
            ), f
    assert set(owned) == DIRECT | INDIRECT.keys()
    assert rows(
        repo,
        "SELECT has_table_privilege(current_user,'pulsara_v3.sessions','DELETE') AS ok",
    )[0]["ok"]
    assert not rows(
        repo,
        "SELECT has_table_privilege(current_user,'pulsara_v3.tool_results','DELETE') AS ok",
    )[0]["ok"]


@pytest.mark.parametrize('archived_only', [False, True])
def test_deleted_parent_preserves_fork_and_existing_only_cannot_resurrect(repo, archived_only):
    lease = new_session(repo)
    _, _, anchor = turn(repo, lease.guard, "parent history")
    child = fork(repo, lease.guard.session_id, anchor)
    assert child.created
    if archived_only:
        from tests.test_session_archive import archive
        archive(repo, lease)
    assert delete(repo, lease.guard.session_id, lease.guard, required_lifecycle='ARCHIVED' if archived_only else None) == "DELETED"
    assert delete(repo, lease.guard.session_id) == "ABSENT"
    assert not exists(repo, lease.guard.session_id)
    continued = child_lease(repo, child.child_session_id)
    turn(repo, continued.guard, "child still works")
    with pytest.raises(ConversationKernelConflict, match="unavailable"):
        repo.acquire_host_writer(
            intent="EXISTING",
            session_id=lease.guard.session_id,
            workspace_id='workspace:djt2auc66j65kynztzzvggseme',
            writer_owner_id='host:yo7gabc43kdoxdyautdtmtzhqq',
            lease_seconds=30,
            deadline_monotonic=monotonic() + 30,
        )
    assert not rows(
        repo,
        "SELECT id FROM pulsara_v3.workspaces WHERE id='ctx:workspace/nonexistent'",
    )
    assert not fork(repo, lease.guard.session_id, anchor).created
    if archived_only:
        archive(repo, continued)
    assert delete(repo, child.child_session_id, continued.guard, required_lifecycle='ARCHIVED' if archived_only else None) == "DELETED"


def test_cold_domain_lease_closed_existence_and_exact_release(
    repo, stage2_migrated_postgres_database
):
    lease = new_session(repo)
    sid = lease.guard.session_id
    assert delete(repo, sid, domain="another-domain") == "ABSENT"
    with pytest.raises(SessionDeletionBusy):
        delete(repo, sid)
    repo.release_host_writer(lease.guard, deadline_monotonic=monotonic() + 30)
    assert delete(repo, sid) == "DELETED"
    lease = new_session(repo)
    sid = lease.guard.session_id
    with psycopg.connect(stage2_migrated_postgres_database.admin_dsn) as c:
        c.execute(
            "UPDATE pulsara_v3.sessions SET lifecycle='ARCHIVED',writer_lease_expires_at=clock_timestamp()-interval '1 second' WHERE id=%s",
            (sid,),
        )
    assert exists(repo, sid)
    assert not exists(repo, sid, "another-domain")
    assert delete(repo, sid) == "DELETED"


@pytest.mark.parametrize('archived_only', [False, True])
def test_memory_graph_sources_and_project_survive_with_user_edit(repo, tmp_path, archived_only):
    project = tmp_path / "preserved-project"
    project.mkdir()
    lease, invoke, remember = _open_direct_memory_session(
        repo, workspace_root=str(project)
    )
    _, old, _ = remember("Deletion test: review is Friday.", project=True)
    _, decision, _ = remember(
        "Deletion test: prepare slides Thursday.",
        project=True,
        based_on=(old["memory_id"],),
    )
    _, newer, _ = remember("Deletion test: review is Monday.", project=True)
    invoke(
        "mark_memory_relation",
        {
            "source_memory_id": newer["memory_id"],
            "target_memory_id": old["memory_id"],
            "relation_kind": "SUPERSEDES",
        },
        PreparedMemoryRelationWrite(
            memory_domain_id="u_local",
            source_memory_id=newer["memory_id"],
            target_memory_id=old["memory_id"],
            relation_kind=MemoryRelationKind.SUPERSEDES,
        ),
    )
    ids = [x["memory_id"] for x in (old, decision, newer)]
    before = rows(
        repo,
        "SELECT * FROM pulsara_v3.memory_facts WHERE id=ANY(%s) ORDER BY id",
        (ids,),
    )
    relations = rows(
        repo,
        "SELECT * FROM pulsara_v3.memory_relations WHERE source_fact_id=ANY(%s) ORDER BY id",
        (ids,),
    )
    if archived_only:
        from tests.test_session_archive import archive
        from datetime import datetime, timezone
        from pulsara_agent.conversation_kernel.contracts import InlineContent
        from pulsara_agent.conversation_kernel.repository import AssistantTextBlock
        sid = lease.guard.session_id
        turn_id = rows(repo, "SELECT id FROM pulsara_v3.turns WHERE session_id=%s AND status='RUNNING'", (sid,))[0]['id']
        repo.commit_assistant_message(lease.guard,
            cut=repo.prepare_provider_input_cut(lease.guard, turn_id=turn_id, deadline_monotonic=monotonic()+30),
            entry_id=identity('entry'), parent_content=InlineContent.from_bytes(b'done'),
            blocks=(AssistantTextBlock(identity('block'), InlineContent.from_bytes(b'done')),),
            complete_turn=True, occurred_at=datetime.now(timezone.utc), actor_id='model:test', deadline_monotonic=monotonic()+30)
        archive(repo, lease)
        workspace = rows(repo, 'SELECT workspace_id FROM pulsara_v3.sessions WHERE id=%s', (sid,))[0]['workspace_id']
        targets = [sid]
        for _ in range(5):
            sibling = repo.acquire_host_writer(intent='NEW', session_id=identity('session'), workspace_id=workspace,
                workspace_root=str(project), writer_owner_id=identity('host'), lease_seconds=30, deadline_monotonic=monotonic()+30)
            archive(repo, sibling)
            targets.append(sibling.guard.session_id)
        with ThreadPoolExecutor(max_workers=3) as pool:
            outcomes = list(pool.map(lambda target: delete(repo, target, required_lifecycle='ARCHIVED'), targets))
        assert outcomes == ['DELETED'] * len(targets)
        assert not rows(repo, 'SELECT id FROM pulsara_v3.sessions WHERE id=ANY(%s)', (targets,))
    else:
        assert delete(repo, lease.guard.session_id, lease.guard) == "DELETED"
    after = rows(
        repo,
        "SELECT * FROM pulsara_v3.memory_facts WHERE id=ANY(%s) ORDER BY id",
        (ids,),
    )
    for a, b in zip(after, before, strict=True):
        assert a == {**b, "created_by_tool_result_id": None}
    assert rows(
        repo,
        "SELECT * FROM pulsara_v3.memory_relations WHERE source_fact_id=ANY(%s) ORDER BY id",
        (ids,),
    ) == [{**r, "created_by_tool_result_id": None} for r in relations]
    assert project.is_dir()
    workspace = after[0]["context_id"]
    assert rows(repo, "SELECT id FROM pulsara_v3.workspaces WHERE id=%s", (workspace,))
    selection = MemoryManagementSelection(view="project", workspace_id=workspace)
    detail = repo.memory_management_detail(
        memory_domain_id="u_local",
        selection=selection,
        fact_id=newer["memory_id"],
        deadline_monotonic=monotonic() + 30,
    )
    changed = repo.memory_management_edit_statement(
        memory_domain_id="u_local",
        selection=selection,
        fact_id=newer["memory_id"],
        statement="Deletion test: review is Tuesday.",
        expected_updated_at=detail["fact"]["updated_at"],
        deadline_monotonic=monotonic() + 30,
    )
    assert changed["changed"]
    # Exercise the ordinary graph preview/delete even after source removal,
    # and leave the shared fixture's project directory clean for other tests.
    for fact in (old, newer):
        preview = repo.memory_deletion_preview(
            memory_domain_id="u_local",
            selection=selection,
            fact_id=fact["memory_id"],
            deadline_monotonic=monotonic() + 30,
        )
        repo.execute_memory_deletion(
            memory_domain_id="u_local",
            selection=selection,
            fact_id=fact["memory_id"],
            additional=(),
            expected_records=lambda: iter(preview),
            deadline_monotonic=monotonic() + 30,
        )
    assert not rows(
        repo, "SELECT id FROM pulsara_v3.workspaces WHERE id=%s", (workspace,)
    )


def test_delete_failure_rolls_back_entire_aggregate(
    repo, stage2_migrated_postgres_database
):
    lease = new_session(repo)
    turn(repo, lease.guard, "preserved after rollback")
    with psycopg.connect(stage2_migrated_postgres_database.admin_dsn) as c:
        c.execute(
            "CREATE FUNCTION pulsara_v3.test_reject_delete() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN RAISE EXCEPTION 'injected deletion failure'; END $$"
        )
        c.execute(
            sql.SQL(
                "CREATE TRIGGER test_reject_delete BEFORE DELETE ON pulsara_v3.transcript_entries FOR EACH ROW WHEN (OLD.session_id={}) EXECUTE FUNCTION pulsara_v3.test_reject_delete()"
            ).format(sql.Literal(lease.guard.session_id))
        )
    try:
        with pytest.raises(
            psycopg.errors.RaiseException, match="injected deletion failure"
        ):
            delete(repo, lease.guard.session_id, lease.guard)
        assert exists(repo, lease.guard.session_id)
        assert (
            len(
                rows(
                    repo,
                    "SELECT id FROM pulsara_v3.transcript_entries WHERE session_id=%s",
                    (lease.guard.session_id,),
                )
            )
            == 2
        )
    finally:
        with psycopg.connect(stage2_migrated_postgres_database.admin_dsn) as c:
            c.execute(
                "DROP TRIGGER test_reject_delete ON pulsara_v3.transcript_entries"
            )
            c.execute("DROP FUNCTION pulsara_v3.test_reject_delete()")


def test_fork_waiting_for_deleted_source_never_copies_cached_history(
    repo, stage2_migrated_postgres_database
):
    lease = new_session(repo)
    _, _, anchor = turn(repo, lease.guard, "source")
    with ThreadPoolExecutor(max_workers=1) as pool:
        with psycopg.connect(stage2_migrated_postgres_database.admin_dsn) as c:
            c.execute(
                "SELECT id FROM pulsara_v3.sessions WHERE id=%s FOR UPDATE",
                (lease.guard.session_id,),
            )
            pending = pool.submit(fork, repo, lease.guard.session_id, anchor)
            with pytest.raises(FutureTimeout):
                pending.result(timeout=0.1)
            c.execute(
                "DELETE FROM pulsara_v3.sessions WHERE id=%s", (lease.guard.session_id,)
            )
        assert not pending.result(timeout=30).created


def test_replan_revalidates_writer_after_takeover(repo, monkeypatch):
    from contextlib import contextmanager
    from dataclasses import replace
    from pulsara_agent.conversation_kernel._repository import deletion

    lease = new_session(repo)
    sid = lease.guard.session_id
    workspace = rows(
        repo, "SELECT workspace_id FROM pulsara_v3.sessions WHERE id=%s", (sid,)
    )[0]["workspace_id"]
    original_sources = deletion._sources
    original_connection = repo.connection_provider.connection
    reads = 0
    takeover_pending = False
    replacement = None

    def drift(*args):
        nonlocal reads, takeover_pending
        plan = original_sources(*args)
        reads += 1
        if reads == 2:
            takeover_pending = True
            return replace(plan, relation_ids=("changed-between-statements",))
        return plan

    @contextmanager
    def connection(**kwargs):
        nonlocal takeover_pending, replacement
        with original_connection(**kwargs) as current:
            yield current
        if takeover_pending:
            takeover_pending = False
            replacement = repo.acquire_host_writer(
                intent="EXISTING",
                session_id=sid,
                workspace_id=workspace,
                writer_owner_id='host:6sddnxpwvxb4nxp77ymevcqaxq',
                lease_seconds=30,
                deadline_monotonic=monotonic() + 30,
            )

    monkeypatch.setattr(deletion, "_sources", drift)
    monkeypatch.setattr(repo.connection_provider, "connection", connection)
    with pytest.raises(SessionDeletionBusy, match="writer changed"):
        delete(repo, sid, lease.guard)
    assert reads == 2  # The next attempt rejects before using any memory plan.
    assert replacement is not None and exists(repo, sid)
    repo.release_host_writer(lease.guard, deadline_monotonic=monotonic() + 30)
    repo.validate_host_writer(replacement.guard, deadline_monotonic=monotonic() + 30)
