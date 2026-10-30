"""Delegation keeps identity, lifecycle and content in the shared session queue."""

from datetime import datetime, timezone
from dataclasses import replace
from time import monotonic

import pytest
from psycopg.rows import dict_row

from pulsara_agent.identity import random_id, derive_id
from pulsara_agent.conversation_kernel.blob import CanonicalContentPublisher
from pulsara_agent.conversation_kernel.contracts import InlineContent
from pulsara_agent.conversation_kernel.repository import (
    ConversationKernelRepository,
    AssistantTextBlock,
    AssistantToolCallBlock,
)
from pulsara_agent.conversation_kernel.subagents.collaboration_contracts import (
    PreparedAgentRequest,
)
from pulsara_agent.llm.input import FrozenPromptContent
from pulsara_agent.primitives.context import freeze_json, canonical_json_bytes
from pulsara_agent.primitives.permission import PermissionMode
from pulsara_agent.storage.postgres_connection_provider import PostgresConnectionLane
from tests.support.model_config import (
    start_test_root_turn,
    test_model_binding,
    test_model_runtime,
)
from tests.support.postgres import verified_postgres_provider

pytestmark = pytest.mark.postgres


def deadline():
    return monotonic() + 30


def now():
    return datetime.now(timezone.utc)


@pytest.fixture
def source(stage2_migrated_postgres_database):
    repository = ConversationKernelRepository(
        verified_postgres_provider(stage2_migrated_postgres_database.runtime_dsn)
    )
    lease = repository.acquire_host_writer(
        intent="NEW",
        session_id=random_id("session"),
        workspace_id=random_id("workspace"),
        writer_owner_id=random_id("host"),
        lease_seconds=300,
        deadline_monotonic=deadline(),
    )
    binding = test_model_binding(test_model_runtime())
    turn_id = random_id("turn")
    start_test_root_turn(
        repository,
        lease.guard,
        command_id=random_id("command"),
        turn_id=turn_id,
        entry_id=random_id("entry"),
        context_binding_revision_id=random_id("binding"),
        permission_snapshot_id=random_id("permission"),
        requested_permission_mode=PermissionMode.BYPASS_PERMISSIONS,
        model_call_binding=binding,
        content=FrozenPromptContent.text("Delegate independent investigations."),
        occurred_at=now(),
        deadline_monotonic=deadline(),
    )
    cut = repository.prepare_provider_input_cut(
        lease.guard, turn_id=turn_id, deadline_monotonic=deadline()
    )
    entry_id, call_id, attempt_id = (
        random_id("entry"),
        random_id("call"),
        random_id("attempt"),
    )
    repository.commit_assistant_message(
        lease.guard,
        cut=cut,
        entry_id=entry_id,
        parent_content=InlineContent.from_bytes(b"delegate"),
        blocks=(
            AssistantToolCallBlock(
                random_id("block"),
                call_id,
                "run_agent",
                freeze_json({"instruction": "Investigate."}),
            ),
        ),
        occurred_at=now(),
        actor_id="test-model",
        deadline_monotonic=deadline(),
    )
    with repository._writer_transaction(
        lease.guard, deadline_monotonic=deadline()
    ) as c:
        permission = repository._permission_from_row(
            c.execute(
                "SELECT * FROM pulsara_v3.turns WHERE id=%s", (turn_id,)
            ).fetchone()
        )
        invocation_id = derive_id(
            "call", "model-invocation", lease.guard.session_id, entry_id, call_id
        )
        c.execute(
            "INSERT INTO pulsara_v3.tool_invocations (id,session_id,turn_id,source_kind,assistant_entry_id,tool_call_id) VALUES (%s,%s,%s,'MODEL_TOOL_CALL',%s,%s)",
            (invocation_id, lease.guard.session_id, turn_id, entry_id, call_id),
        )
        c.execute(
            """INSERT INTO pulsara_v3.tool_execution_attempts (id,session_id,invocation_id,assistant_entry_id,tool_call_id,
            authorization_kind,authorization_reference,permission_snapshot_fingerprint,actor_kind,actor_id)
            VALUES (%s,%s,%s,%s,%s,'BYPASS','test',%s,'runtime','test')""",
            (
                attempt_id,
                lease.guard.session_id,
                invocation_id,
                entry_id,
                call_id,
                permission.snapshot_fingerprint,
            ),
        )

    def admit(*requests):
        return repository.admit_agent_requests(
            lease.guard,
            requests=requests,
            source_turn_id=turn_id,
            source_tool_attempt_id=attempt_id,
            model_call_binding=binding,
            permission_snapshot=permission,
            occurred_at=now(),
            actor_id="test",
            deadline_monotonic=deadline(),
        )

    return repository, lease, admit


def request(*, agent_id=None, work_id=None, create=True, **kwargs):
    return PreparedAgentRequest(
        agent_id=agent_id or random_id("session"),
        work_id=work_id or random_id("queue"),
        instruction=kwargs.pop("instruction", "Investigate."),
        create_agent=create,
        **kwargs,
    )


def test_session_list_counts_subagent_sessions_once_when_work_is_reused(source):
    from pulsara_agent.conversation_kernel.host import _list_resumable_session_rows_across_workspaces

    repository, root, admit = source
    first, second = request(), request()
    admit(first, second)
    admit(request(agent_id=first.agent_id, create=False))
    rows = _list_resumable_session_rows_across_workspaces(repository, 'u_local', False, deadline())
    summary = next(row for row in rows if row['id'] == root.guard.session_id)
    assert summary['subagent_count'] == 2
    assert all(row['id'] not in {first.agent_id, second.agent_id} for row in rows)


def start(repository, req):
    lease = repository.acquire_host_writer(
        intent="EXISTING",
        session_id=req.agent_id,
        workspace_id=repository.read_agent_runtime_state(
            session_id=req.agent_id, deadline_monotonic=deadline()
        )["workspace_id"],
        writer_owner_id=random_id("host"),
        lease_seconds=300,
        deadline_monotonic=deadline(),
    )
    candidate = repository.prepare_prompt_head_consumption(
        session_id=req.agent_id,
        occurred_at=now(),
        actor_id="test",
        deadline_monotonic=deadline(),
    )
    assert candidate is not None
    result = repository.consume_prepared_prompt_head(
        lease.guard,
        candidate=candidate,
        deadline_monotonic=deadline(),
    )
    assert result.kind.value == "FULL"
    assert (
        repository.confirm_prepared_prompt_head_consumption(
            candidate=candidate, deadline_monotonic=deadline()
        ).kind.value
        == "FULL"
    )
    return lease, candidate


def finish(repository, lease, candidate, text):
    cut = repository.prepare_provider_input_cut(
        lease.guard, turn_id=candidate.exact_turn_id, deadline_monotonic=deadline()
    )
    content = CanonicalContentPublisher(repository.connection_provider).materialize(
        session_id=lease.guard.session_id,
        content=text.encode(),
        media_type="text/plain",
        codec="utf-8",
        deadline_monotonic=deadline(),
    )
    repository.commit_assistant_message(
        lease.guard,
        cut=cut,
        entry_id=random_id("entry"),
        parent_content=InlineContent.from_bytes(
            canonical_json_bytes(
                {
                    "blocks": [
                        {"digest": content.digest, "kind": "TEXT", "size": content.size}
                    ],
                    "draft_identity": random_id("entry"),
                }
            )
        ),
        blocks=(AssistantTextBlock(random_id("block"), content),),
        complete_turn=True,
        occurred_at=now(),
        actor_id="test-model",
        deadline_monotonic=deadline(),
    )


def test_batch_failure_has_no_partial_sessions_and_combined_fifo_cycle_is_rejected(
    source,
):
    repository, root, admit = source
    good = request()
    bad = request(materials=(random_id("queue"),))
    with pytest.raises(Exception):
        admit(good, bad)
    assert (
        repository.read_agent_runtime_state(
            session_id=good.agent_id, deadline_monotonic=deadline()
        )
        is None
    )
    later = random_id("queue")
    first = request(depends_on=(later,))
    second = request(agent_id=first.agent_id, work_id=later, create=False)
    with pytest.raises(ValueError, match="waiting cycle"):
        admit(first, second)
    assert (
        repository.list_root_agent_session_ids(
            root_session_id=root.guard.session_id, deadline_monotonic=deadline()
        )
        == ()
    )


def test_cancel_cascades_without_empty_turn_and_preserves_independent_fifo_work(source):
    repository, root, admit = source
    a = request()
    b = request(depends_on=(a.work_id,))
    c = request(depends_on=(b.work_id,))
    independent = request(agent_id=b.agent_id, create=False)
    admit(a, b, c, independent)
    assert (
        repository.prepare_prompt_head_consumption(
            session_id=b.agent_id,
            occurred_at=now(),
            actor_id="test",
            deadline_monotonic=deadline(),
        )
        is None
    )
    repository.cancel_agent_requests(
        root.guard,
        agent_id=a.agent_id,
        work_id=a.work_id,
        occurred_at=now(),
        actor_id="test",
        deadline_monotonic=deadline(),
    )
    for req in (a, b, c):
        work = repository.query_agent_work(
            root_session_id=root.guard.session_id,
            work_id=req.work_id,
            deadline_monotonic=deadline(),
        )
        assert work["status"] == "stopped" and work["turn_id"] is None
    assert repository.read_agent_runtime_state(
        session_id=b.agent_id, deadline_monotonic=deadline()
    )["head_ready"]
    lease, candidate = start(repository, independent)
    assert candidate.queue_item_id == independent.work_id


def test_full_results_are_owned_and_ordered_before_first_model_input(source):
    repository, root, admit = source
    a = request()
    b = request()
    receiver = request(
        depends_on=(a.work_id, b.work_id), instruction="Compare both results."
    )
    admit(a, b, receiver)
    # Complete in the opposite order to the dependency declaration.
    lb, cb = start(repository, b)
    finish(repository, lb, cb, "second-result")
    repository.resolve_agent_dependencies(
        session_id=receiver.agent_id, deadline_monotonic=deadline()
    )
    la, ca = start(repository, a)
    large = "first-result:" + ("x" * 100_000)
    finish(repository, la, ca, large)
    repository.resolve_agent_dependencies(
        session_id=receiver.agent_id, deadline_monotonic=deadline()
    )
    assert (
        repository.query_agent_work(
            root_session_id=root.guard.session_id,
            work_id=a.work_id,
            deadline_monotonic=deadline(),
        )["final"]
        == large
    )
    repository.release_host_writer(la.guard, deadline_monotonic=deadline())
    assert (
        repository.delete_session(
            session_id=a.agent_id,
            memory_domain_id="u_local",
            closed_writer=la.guard,
            deadline_monotonic=deadline(),
            required_lifecycle=None,
        )
        == "DELETED"
    )
    lr, candidate = start(repository, receiver)
    entries = candidate.ordered_input_entries
    texts = ["".join(p.text for p in e.canonical_prompt.content.parts) for e in entries]
    assert texts == ["Compare both results.", large, "second-result"]
    assert entries[0].entry_id == candidate.exact_initial_entry_id
    confirmation = repository.confirm_prepared_prompt_head_consumption(
        candidate=candidate, deadline_monotonic=deadline()
    )
    group = confirmation.accepted.started_input_group
    assert group.ordered_items[0].source_entry_sequence == 1
    assert group.provider_input_through_sequence == 3
    assert group.predecessor_through_sequence == 0
    assert group.initial_source_through_sequence == 0
    with repository.connection_provider.connection(
        lane=PostgresConnectionLane.INSPECTOR,
        row_factory=dict_row,
        deadline_monotonic=deadline(),
    ) as c:
        rows = c.execute(
            "SELECT input_origin,id FROM pulsara_v3.transcript_entries WHERE session_id=%s ORDER BY entry_sequence",
            (receiver.agent_id,),
        ).fetchall()
        assert [r["input_origin"] for r in rows] == [
            "AGENT_INSTRUCTION",
            "AGENT_MATERIAL",
            "AGENT_MATERIAL",
        ]
    assert (
        repository.query_agent_work(
            root_session_id=root.guard.session_id,
            work_id=receiver.work_id,
            deadline_monotonic=deadline(),
        )["status"]
        == "running"
    )
    facts = repository.read_agent_work_graph_facts(
        root_session_id=root.guard.session_id,
        work_id=receiver.work_id,
        deadline_monotonic=deadline(),
    )
    assert facts["status"] == "running"
    assert facts["consumed_entry_id"] == candidate.exact_initial_entry_id
    assert facts["turn_id"] == candidate.exact_turn_id
    assert facts["inputs"] == (
        {"kind": "dependency", "source_work_id": a.work_id, "ordinal": 0},
        {"kind": "dependency", "source_work_id": b.work_id, "ordinal": 1},
    )
    assert (
        repository.read_agent_work_graph_facts(
            root_session_id=root.guard.session_id,
            work_id=a.work_id,
            deadline_monotonic=deadline(),
        )
        is None
    )


def test_work_input_identities_are_confirmed_and_graph_does_not_hydrate(
    source, monkeypatch
):
    repository, root, admit = source
    first, second = request(), request()
    admit(first, second)
    lease, candidate = start(repository, second)
    finish(repository, lease, candidate, "material body")
    receiver = request(
        depends_on=(first.work_id,), materials=(first.work_id, second.work_id)
    )
    admit(receiver)
    with repository.connection_provider.connection(
        lane=PostgresConnectionLane.INSPECTOR,
        row_factory=dict_row,
        deadline_monotonic=deadline(),
    ) as connection:
        turn = connection.execute(
            "SELECT * FROM pulsara_v3.turns WHERE session_id=%s AND status='RUNNING'",
            (root.guard.session_id,),
        ).fetchone()
        attempt = connection.execute(
            "SELECT id FROM pulsara_v3.tool_execution_attempts WHERE session_id=%s",
            (root.guard.session_id,),
        ).fetchone()
        permission = repository._permission_from_row(turn)
    from pulsara_agent.llm.model_connections import model_call_binding_from_dict

    confirmation = dict(
        source_turn_id=turn["id"],
        source_tool_attempt_id=attempt["id"],
        model_call_binding=model_call_binding_from_dict(turn["model_call_binding"]),
        permission_snapshot=permission,
        deadline_monotonic=deadline(),
    )
    assert (
        repository.confirm_agent_requests(
            root.guard, requests=(receiver,), **confirmation
        )[0]["work_id"]
        == receiver.work_id
    )
    with pytest.raises(Exception, match="differs from its prepared"):
        repository.confirm_agent_requests(
            root.guard,
            requests=(replace(receiver, depends_on=(second.work_id,)),),
            **confirmation,
        )
    monkeypatch.setattr(
        repository,
        "_agent_prompt_text",
        lambda *args: pytest.fail("graph hydrated a prompt"),
    )
    facts = repository.read_agent_work_graph_facts(
        root_session_id=root.guard.session_id,
        work_id=receiver.work_id,
        deadline_monotonic=deadline(),
    )
    assert facts["consumed_entry_id"] is None and facts["turn_id"] is None
    assert facts["inputs"] == (
        {"kind": "dependency", "source_work_id": first.work_id, "ordinal": 0},
        {"kind": "material", "source_work_id": second.work_id, "ordinal": 1},
    )
    assert (
        repository.read_agent_work_graph_facts(
            root_session_id=random_id("session"),
            work_id=receiver.work_id,
            deadline_monotonic=deadline(),
        )
        is None
    )


@pytest.mark.parametrize("kind", ["dependency", "material"])
@pytest.mark.parametrize("body", ["", "  "])
def test_empty_work_material_is_stored_and_projected_once(source, kind, body):
    repository, root, admit = source
    parent = request()
    admit(parent)
    lease, candidate = start(repository, parent)
    finish(repository, lease, candidate, body)
    receiver = request(
        **{"depends_on" if kind == "dependency" else "materials": (parent.work_id,)}
    )
    admit(receiver)
    with repository.connection_provider.connection(
        lane=PostgresConnectionLane.INSPECTOR,
        row_factory=dict_row,
        deadline_monotonic=deadline(),
    ) as connection:
        inputs = connection.execute(
            "SELECT work_inputs FROM pulsara_v3.prompt_queue_items WHERE id=%s",
            (receiver.work_id,),
        ).fetchone()["work_inputs"]
    assert inputs == [
        {"kind": kind, "source_work_id": parent.work_id, "ordinal": 0, "body": body}
    ]
    _, candidate = start(repository, receiver)
    assert [
        part.text
        for entry in candidate.ordered_input_entries
        for part in entry.canonical_prompt.content.parts
    ] == [
        "Investigate.",
        "此来源 Work 的结果不含文本内容。" if body == "" else body,
    ]
    confirmation = repository.confirm_prepared_prompt_head_consumption(
        candidate=candidate, deadline_monotonic=deadline()
    )
    assert confirmation.kind.value == "FULL"


def test_source_deletion_before_dependency_scan_does_not_deadlock(source, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event

    repository, root, admit = source
    predecessor = request()
    source_work = request(depends_on=(predecessor.work_id,))
    receiver = request(depends_on=(source_work.work_id,))
    admit(predecessor, source_work, receiver)
    found_sources = Event()
    original = repository._work_input_source_sessions

    def observed(connection, **kwargs):
        result = original(connection, **kwargs)
        if source_work.agent_id in result.values():
            found_sources.set()
        return result

    monkeypatch.setattr(repository, "_work_input_source_sessions", observed)
    with repository.connection_provider.connection(
        lane=PostgresConnectionLane.HOST_CONTROL,
        deadline_monotonic=deadline(),
    ) as deleting:
        deleting.execute(
            "SELECT id FROM pulsara_v3.sessions WHERE id=%s FOR UPDATE",
            (source_work.agent_id,),
        )
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(
                repository.resolve_agent_dependencies,
                session_id=receiver.agent_id,
                deadline_monotonic=deadline(),
            )
            assert found_sources.wait(5)
            deleting.execute(
                "DELETE FROM pulsara_v3.sessions WHERE id=%s", (source_work.agent_id,)
            )
            deleting.execute("SET CONSTRAINTS ALL IMMEDIATE")
            deleting.commit()
            future.result(timeout=10)
    work = repository.query_agent_work(
        root_session_id=root.guard.session_id,
        work_id=receiver.work_id,
        deadline_monotonic=deadline(),
    )
    assert work["status"] == "stopped"
    assert work["terminal_reason"] == "DEPENDENCY_NOT_SUCCESSFUL:" + source_work.work_id


def test_materialization_wins_against_concurrent_source_deletion(source, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event
    from pulsara_agent.conversation_kernel._repository import (
        deletion as deletion_operations,
    )

    repository, root, admit = source
    previous = request()
    admit(previous)
    lease, candidate = start(repository, previous)
    finish(repository, lease, candidate, "independent snapshot")
    repository.release_host_writer(lease.guard, deadline_monotonic=deadline())
    next_work = request(depends_on=(previous.work_id,))
    reading, continue_read, deleting = Event(), Event(), Event()
    original_read = repository._agent_work_in_connection
    original_lock = deletion_operations.lock_session_admission

    def paused_read(connection, *, root_session_id, work_id):
        if work_id == previous.work_id:
            reading.set()
            assert continue_read.wait(10)
        return original_read(
            connection, root_session_id=root_session_id, work_id=work_id
        )

    def observed_delete_lock(connection, *, session_id):
        original_lock(connection, session_id=session_id)
        if session_id == previous.agent_id:
            deleting.set()

    monkeypatch.setattr(repository, "_agent_work_in_connection", paused_read)
    monkeypatch.setattr(
        deletion_operations, "lock_session_admission", observed_delete_lock
    )
    with ThreadPoolExecutor(max_workers=2) as pool:
        accepted = pool.submit(admit, next_work)
        assert reading.wait(5)
        retired = pool.submit(
            repository.delete_session,
            session_id=previous.agent_id,
            memory_domain_id="u_local",
            closed_writer=lease.guard,
            required_lifecycle=None,
            deadline_monotonic=deadline(),
        )
        assert deleting.wait(5)
        continue_read.set()
        assert accepted.result(timeout=10)[0]["work_id"] == next_work.work_id
        assert retired.result(timeout=10) == "DELETED"
    _, next_candidate = start(repository, next_work)
    assert (
        next_candidate.ordered_input_entries[-1].canonical_prompt.content.parts[0].text
        == "independent snapshot"
    )
    assert (
        repository.read_agent_work_graph_facts(
            root_session_id=root.guard.session_id,
            work_id=previous.work_id,
            deadline_monotonic=deadline(),
        )
        is None
    )


def test_reused_agent_source_delete_wins_before_admission(source, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event
    from pulsara_agent.conversation_kernel._repository import subagents as operations

    repository, root, admit = source
    previous = request()
    admit(previous)
    lease, candidate = start(repository, previous)
    finish(repository, lease, candidate, "completed")
    repository.release_host_writer(lease.guard, deadline_monotonic=deadline())
    next_work = request(
        agent_id=previous.agent_id, create=False, depends_on=(previous.work_id,)
    )
    target_lock = Event()
    original = operations.lock_session_admission

    def observed(connection, *, session_id):
        original(connection, session_id=session_id)
        if session_id == previous.agent_id:
            target_lock.set()

    monkeypatch.setattr(operations, "lock_session_admission", observed)
    with repository.connection_provider.connection(
        lane=PostgresConnectionLane.HOST_CONTROL,
        deadline_monotonic=deadline(),
    ) as deleting:
        deleting.execute(
            "SELECT id FROM pulsara_v3.sessions WHERE id=%s FOR UPDATE",
            (previous.agent_id,),
        )
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(admit, next_work)
            assert target_lock.wait(5)
            deleting.execute(
                "DELETE FROM pulsara_v3.sessions WHERE id=%s", (previous.agent_id,)
            )
            deleting.execute("SET CONSTRAINTS ALL IMMEDIATE")
            deleting.commit()
            with pytest.raises(Exception, match="unavailable"):
                future.result(timeout=10)
    assert (
        repository.query_agent_work(
            root_session_id=root.guard.session_id,
            work_id=next_work.work_id,
            deadline_monotonic=deadline(),
        )
        is None
    )


def test_input_lock_replan_rolls_back_before_retrying_admission(source, monkeypatch):
    from pulsara_agent.conversation_kernel._repository.subagents import (
        _WorkInputsReplan,
    )

    repository, _root, admit = source
    work = request()
    checks = []
    original = repository._verify_work_input_source_locks

    def replan_once(connection, **kwargs):
        checks.append(True)
        if len(checks) == 1:
            raise _WorkInputsReplan()
        return original(connection, **kwargs)

    monkeypatch.setattr(repository, "_verify_work_input_source_locks", replan_once)
    assert admit(work)[0]["work_id"] == work.work_id
    assert len(checks) >= 2
    with repository.connection_provider.connection(
        lane=PostgresConnectionLane.INSPECTOR,
        deadline_monotonic=deadline(),
    ) as connection:
        assert (
            connection.execute(
                "SELECT count(*) FROM pulsara_v3.prompt_queue_items WHERE id=%s",
                (work.work_id,),
            ).fetchone()[0]
            == 1
        )
        assert (
            connection.execute(
                "SELECT count(*) FROM pulsara_v3.agent_events WHERE subject_queue_item_id=%s AND event_type='PromptQueued'",
                (work.work_id,),
            ).fetchone()[0]
            == 1
        )


def test_waiting_work_is_not_idle_and_cancel_preserves_other_work(source):
    repository, root, admit = source
    predecessor = request()
    first = request(depends_on=(predecessor.work_id,))
    second = request(agent_id=first.agent_id, create=False)
    admit(predecessor, first, second)
    repository.cancel_agent_requests(
        root.guard,
        agent_id=first.agent_id,
        work_id=first.work_id,
        occurred_at=now(),
        actor_id="test",
        deadline_monotonic=deadline(),
    )
    work = repository.query_agent_work(
        root_session_id=root.guard.session_id,
        work_id=second.work_id,
        deadline_monotonic=deadline(),
    )
    assert work["status"] == "queued"
    assert "profile" not in work
    assert (
        repository.mark_agent_idle(
            session_id=predecessor.agent_id, at=now(), deadline_monotonic=deadline()
        )
        is None
    )
    rows = repository.cancel_agent_requests(
        root.guard,
        agent_id=first.agent_id,
        occurred_at=now(),
        actor_id="test",
        deadline_monotonic=deadline(),
    )
    assert [x["work_id"] for x in rows] == [second.work_id]


def test_foreign_root_cannot_dispatch_into_or_cancel_an_agent(
    source, stage2_migrated_postgres_database
):
    repository, root, admit = source
    other = repository.acquire_host_writer(
        intent="NEW",
        session_id=random_id("session"),
        workspace_id=random_id("workspace"),
        writer_owner_id=random_id("host"),
        lease_seconds=300,
        deadline_monotonic=deadline(),
    )
    child = request()
    admit(child)
    assert (
        repository.query_agent_work(
            root_session_id=other.guard.session_id,
            work_id=child.work_id,
            deadline_monotonic=deadline(),
        )
        is None
    )
    with pytest.raises(Exception, match="does not belong"):
        repository.cancel_agent_requests(
            other.guard,
            agent_id=child.agent_id,
            occurred_at=now(),
            actor_id="test",
            deadline_monotonic=deadline(),
        )
    # Even the real ROOT guard only authorizes queue admission/cancellation in its member session.
    from pulsara_agent.conversation_kernel.vocabulary import (
        CommittedEventType,
        SubjectSlot,
    )

    with pytest.raises(Exception, match="only accept or cancel"):
        with repository._writer_transaction(
            root.guard, deadline_monotonic=deadline()
        ) as c:
            repository._append_events(
                c,
                root.guard,
                workspace_id=repository.read_agent_runtime_state(
                    session_id=child.agent_id, deadline_monotonic=deadline()
                )["workspace_id"],
                owned_session_id=child.agent_id,
                drafts=(
                    repository._event(
                        CommittedEventType.TURN_COMPLETED,
                        SubjectSlot.TURN,
                        random_id("turn"),
                        session_id=child.agent_id,
                        occurrence_key="",
                        occurred_at=now(),
                        actor_kind="runtime",
                        actor_id="test",
                        payload={},
                    ),
                ),
            )
    assert (
        repository.query_agent_work(
            root_session_id=root.guard.session_id,
            work_id=child.work_id,
            deadline_monotonic=deadline(),
        )["status"]
        == "queued"
    )


def test_start_cancel_race_has_one_actual_stage(source):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier

    repository, root, admit = source
    child = request()
    admit(child)
    state = repository.read_agent_runtime_state(
        session_id=child.agent_id, deadline_monotonic=deadline()
    )
    lease = repository.acquire_host_writer(
        intent="EXISTING",
        session_id=child.agent_id,
        workspace_id=state["workspace_id"],
        writer_owner_id=random_id("host"),
        lease_seconds=300,
        deadline_monotonic=deadline(),
    )
    candidate = repository.prepare_prompt_head_consumption(
        session_id=child.agent_id,
        occurred_at=now(),
        actor_id="test",
        deadline_monotonic=deadline(),
    )
    gate = Barrier(2)

    def consume():
        gate.wait()
        return repository.consume_prepared_prompt_head(
            lease.guard,
            candidate=candidate,
            deadline_monotonic=deadline(),
        )

    def cancel():
        gate.wait()
        return repository.cancel_agent_requests(
            root.guard,
            agent_id=child.agent_id,
            work_id=child.work_id,
            occurred_at=now(),
            actor_id="test",
            deadline_monotonic=deadline(),
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        started = pool.submit(consume)
        cancelled = pool.submit(cancel)
        result = started.result()
        cancelled.result()
    actual = repository.query_agent_work(
        root_session_id=root.guard.session_id,
        work_id=child.work_id,
        deadline_monotonic=deadline(),
    )
    assert actual["status"] in {"running", "stopped"}
    assert (actual["turn_id"] is None) == (actual["status"] == "stopped")
    assert (result is None) == (actual["status"] == "stopped")
    if result is not None:
        assert result.kind.value == "FULL"
        assert (
            repository.confirm_prepared_prompt_head_consumption(
                candidate=candidate, deadline_monotonic=deadline()
            ).kind.value
            == "FULL"
        )


def test_root_deletion_cascades_only_owned_members(source):
    repository, root, admit = source
    a = request()
    b = request()
    admit(a, b)
    la, _ = start(repository, a)
    with pytest.raises(Exception, match="owned Agent has a live writer"):
        repository.delete_session(
            session_id=root.guard.session_id,
            memory_domain_id="u_local",
            closed_writer=root.guard,
            deadline_monotonic=deadline(),
            required_lifecycle=None,
        )
    repository.release_host_writer(la.guard, deadline_monotonic=deadline())
    assert (
        repository.delete_session(
            session_id=b.agent_id,
            memory_domain_id="u_local",
            closed_writer=None,
            deadline_monotonic=deadline(),
            required_lifecycle=None,
        )
        == "DELETED"
    )
    assert repository.canonical_session_exists(
        session_id=root.guard.session_id,
        memory_domain_id="u_local",
        deadline_monotonic=deadline(),
    )
    assert (
        repository.delete_session(
            session_id=root.guard.session_id,
            memory_domain_id="u_local",
            closed_writer=root.guard,
            deadline_monotonic=deadline(),
            required_lifecycle=None,
        )
        == "DELETED"
    )
    assert not repository.canonical_session_exists(
        session_id=a.agent_id, memory_domain_id="u_local", deadline_monotonic=deadline()
    )


def test_material_delivery_is_independent_of_deleted_sender_and_exactly_confirmed(
    source,
):
    repository, root, admit = source
    child = request()
    admit(child)
    lease, candidate = start(repository, child)
    finish(repository, lease, candidate, "Complete real final body.")
    final = repository.query_agent_work(
        root_session_id=root.guard.session_id,
        work_id=child.work_id,
        deadline_monotonic=deadline(),
    )["final"]
    assert final == "Complete real final body."
    delivery_id = derive_id(
        "queue", "agent-completion", root.guard.session_id, child.work_id
    )
    with repository.connection_provider.connection(
        lane=PostgresConnectionLane.INSPECTOR, deadline_monotonic=deadline()
    ) as c:
        root_turn = c.execute(
            "SELECT id FROM pulsara_v3.turns WHERE session_id=%s AND status='RUNNING'",
            (root.guard.session_id,),
        ).fetchone()[0]
    delivered = repository.enqueue_agent_steer(
        root.guard,
        target_turn_id=root_turn,
        queue_item_id=delivery_id,
        source_session_id=child.agent_id,
        source_work_id=child.work_id,
        text=final,
        input_origin="AGENT_MATERIAL",
        occurred_at=now(),
        actor_id="test",
        deadline_monotonic=deadline(),
    )
    assert delivered["status"] == "PENDING"
    repository.release_host_writer(lease.guard, deadline_monotonic=deadline())
    repository.delete_session(
        session_id=child.agent_id,
        memory_domain_id="u_local",
        closed_writer=lease.guard,
        deadline_monotonic=deadline(),
        required_lifecycle=None,
    )
    confirmed = repository.enqueue_agent_steer(
        root.guard,
        target_turn_id=random_id("turn"),
        queue_item_id=delivery_id,
        source_session_id=child.agent_id,
        source_work_id=child.work_id,
        text=final,
        input_origin="AGENT_MATERIAL",
        occurred_at=now(),
        actor_id="test",
        deadline_monotonic=deadline(),
    )
    assert (
        confirmed["id"] == delivered["id"] and confirmed["target_turn_id"] == root_turn
    )
    facts = repository.read_pending_prompt_steer_facts(
        session_id=root.guard.session_id,
        target_turn_id=root_turn,
        maximum_items=8,
        deadline_monotonic=deadline(),
    )
    fact = next(x for x in facts if x.queue_item_id == delivery_id)
    assert fact.command_id is None and fact.input_origin.value == "AGENT_MATERIAL"
    actual = repository.hydrate_pending_prompt_steer(
        fact=fact, deadline_monotonic=deadline()
    )
    assert "".join(p.text for p in actual.content.parts) == final


def test_automatic_retirement_rechecks_admission_under_the_same_postgres_lock(
    source, monkeypatch
):
    from concurrent.futures import ThreadPoolExecutor
    from datetime import timedelta
    from threading import Event
    from time import sleep

    repository, root, admit = source
    child = request()
    admit(child)
    repository.cancel_agent_requests(
        root.guard,
        agent_id=child.agent_id,
        work_id=child.work_id,
        occurred_at=now(),
        actor_id="test",
        deadline_monotonic=deadline(),
    )
    idle_at = now() - timedelta(days=4)
    assert (
        repository.mark_agent_idle(
            session_id=child.agent_id, at=idle_at, deadline_monotonic=deadline()
        )
        == idle_at
    )
    # The retirement scan observed eligibility before this new acceptance.
    observed = repository.read_agent_runtime_state(
        session_id=child.agent_id, deadline_monotonic=deadline()
    )
    assert observed["idle_since"] == idle_at and not observed["has_pending_work"]
    following = request(agent_id=child.agent_id, create=False)
    published_in_transaction, release_commit = Event(), Event()
    append = repository._agent_queue_event

    def pause_after_admission(*args, **kwargs):
        append(*args, **kwargs)
        if args[4] == following.work_id:
            published_in_transaction.set()
            assert release_commit.wait(10), (
                "test did not release the admitted request transaction"
            )

    monkeypatch.setattr(repository, "_agent_queue_event", pause_after_admission)
    with ThreadPoolExecutor(max_workers=2) as pool:
        accepted = pool.submit(admit, following)
        assert published_in_transaction.wait(10)
        retired = pool.submit(
            repository.delete_session,
            session_id=child.agent_id,
            memory_domain_id="u_local",
            closed_writer=None,
            required_lifecycle=None,
            idle_before=now() - timedelta(days=3),
            deadline_monotonic=deadline(),
        )
        try:
            # Verify real database contention while the old idle snapshot is
            # still visible and the acceptance transaction owns admission.
            wait_deadline = monotonic() + 5
            blocked = False
            while monotonic() < wait_deadline:
                with repository.connection_provider.connection(
                    lane=PostgresConnectionLane.INSPECTOR, deadline_monotonic=deadline()
                ) as c:
                    blocked = c.execute("""SELECT EXISTS(SELECT 1 FROM pg_locks l
                        JOIN pg_stat_activity a ON a.pid=l.pid
                        WHERE a.datname=current_database() AND l.locktype='advisory' AND NOT l.granted)""").fetchone()[
                        0
                    ]
                if blocked:
                    break
                sleep(0.01)
            assert blocked and not retired.done()
        finally:
            release_commit.set()
        assert accepted.result()[0]["work_id"] == following.work_id
        assert retired.result() == "NOT_IDLE"
    work = repository.query_agent_work(
        root_session_id=root.guard.session_id,
        work_id=following.work_id,
        deadline_monotonic=deadline(),
    )
    assert work["status"] == "queued" and work["turn_id"] is None
    assert (
        repository.read_agent_runtime_state(
            session_id=child.agent_id, deadline_monotonic=deadline()
        )["idle_since"]
        is None
    )


def test_automatic_retirement_requires_continuous_idle_but_explicit_delete_does_not(
    source,
):
    from datetime import timedelta

    repository, root, admit = source
    child = request()
    admit(child)
    repository.cancel_agent_requests(
        root.guard,
        agent_id=child.agent_id,
        work_id=child.work_id,
        occurred_at=now(),
        actor_id="test",
        deadline_monotonic=deadline(),
    )
    repository.mark_agent_idle(
        session_id=child.agent_id,
        at=now() - timedelta(hours=2),
        deadline_monotonic=deadline(),
    )
    assert (
        repository.delete_session(
            session_id=child.agent_id,
            memory_domain_id="u_local",
            closed_writer=None,
            required_lifecycle=None,
            idle_before=now() - timedelta(days=3),
            deadline_monotonic=deadline(),
        )
        == "NOT_IDLE"
    )
    repository.mark_agent_idle(
        session_id=child.agent_id, idle=False, deadline_monotonic=deadline()
    )
    repository.mark_agent_idle(
        session_id=child.agent_id,
        at=now() - timedelta(days=4),
        deadline_monotonic=deadline(),
    )
    assert (
        repository.delete_session(
            session_id=child.agent_id,
            memory_domain_id="u_local",
            closed_writer=None,
            required_lifecycle=None,
            idle_before=now() - timedelta(days=3),
            deadline_monotonic=deadline(),
        )
        == "DELETED"
    )
    # The user-deletion path retains its existing authority over pending work.
    explicit = request()
    admit(explicit)
    assert (
        repository.delete_session(
            session_id=explicit.agent_id,
            memory_domain_id="u_local",
            closed_writer=None,
            required_lifecycle=None,
            deadline_monotonic=deadline(),
        )
        == "DELETED"
    )
