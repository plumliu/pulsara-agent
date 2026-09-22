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
)
from tests.test_direct_advisory_memory import _open_direct_memory_session


DIRECT = {
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
    "turn_context_binding_revisions": ("turns", "turn_id"),
    "tool_execution_attempts": ("assistant_message_blocks", "tool_call_id"),
    "imported_tool_call_closures": ("assistant_message_blocks", "tool_call_id"),
    "subagent_task_dependencies": ("subagent_tasks", "task_id"),
    "subagent_task_children": ("subagent_tasks", "task_id"),
}


def delete(repo, sid, guard=None, domain="u_local"):
    return repo.delete_session(
        session_id=sid,
        memory_domain_id=domain,
        closed_writer=guard,
        deadline_monotonic=monotonic() + 30,
    )


def exists(repo, sid, domain="u_local"):
    return repo.canonical_session_exists(
        session_id=sid, memory_domain_id=domain, deadline_monotonic=monotonic() + 30
    )


def test_catalog_ownership_semantic_edges_grants_and_index_coverage(repo):
    assert len(CONVERSATION_KERNEL_RELATIONS) == 28
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


def test_deleted_parent_preserves_fork_and_existing_only_cannot_resurrect(repo):
    lease = new_session(repo)
    _, _, anchor = turn(repo, lease.guard, "parent history")
    child = fork(repo, lease.guard.session_id, anchor)
    assert child.created
    assert delete(repo, lease.guard.session_id, lease.guard) == "DELETED"
    assert delete(repo, lease.guard.session_id) == "ABSENT"
    assert not exists(repo, lease.guard.session_id)
    continued = child_lease(repo, child.child_session_id)
    turn(repo, continued.guard, "child still works")
    with pytest.raises(ConversationKernelConflict, match="unavailable"):
        repo.acquire_host_writer(
            intent="EXISTING",
            session_id=lease.guard.session_id,
            workspace_id="ctx:workspace/nonexistent",
            writer_owner_id="host:stale",
            lease_seconds=30,
            deadline_monotonic=monotonic() + 30,
        )
    assert not rows(
        repo,
        "SELECT id FROM pulsara_v3.workspaces WHERE id='ctx:workspace/nonexistent'",
    )
    assert not fork(repo, lease.guard.session_id, anchor).created
    assert delete(repo, child.child_session_id, continued.guard) == "DELETED"


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
            "UPDATE pulsara_v3.sessions SET lifecycle='CLOSED',writer_lease_expires_at=clock_timestamp()-interval '1 second' WHERE id=%s",
            (sid,),
        )
    assert exists(repo, sid)
    assert not exists(repo, sid, "another-domain")
    assert delete(repo, sid) == "DELETED"


def test_memory_graph_sources_and_project_survive_with_user_edit(repo, tmp_path):
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
                writer_owner_id="host:takeover-after-replan",
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
