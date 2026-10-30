"""Real transaction and range contracts for accepted Agent introductions."""

from concurrent.futures import ThreadPoolExecutor
from threading import Barrier, Event
from time import monotonic, sleep

import pytest
from psycopg.errors import DeadlockDetected, SerializationFailure
from psycopg.rows import dict_row

from pulsara_agent.identity import random_id
from pulsara_agent.storage.postgres_connection_provider import PostgresConnectionLane
from tests.test_agent_requests_persistence import deadline, finish, now, request, start
from tests.test_agent_requests_persistence import source as source_fixture

pytest_plugins = ["tests.test_agent_requests_persistence"]
pytestmark = pytest.mark.postgres


def relations(repository, root_id):
    with repository.connection_provider.connection(
        lane=PostgresConnectionLane.INSPECTOR, row_factory=dict_row,
        deadline_monotonic=deadline(),
    ) as connection:
        return {(r["observer_session_id"], r["known_session_id"]) for r in
                connection.execute("SELECT k.observer_session_id,k.known_session_id FROM pulsara_v3.agent_known_sessions k JOIN pulsara_v3.sessions s ON s.id=k.observer_session_id WHERE s.root_session_id=%s", (root_id,)).fetchall()}


def query_work(repository, root, observer, work):
    return repository.query_visible_agent_work(
        root_session_id=root, observer_session_id=observer, work_id=work,
        deadline_monotonic=deadline(),
    )


def test_batch_introductions_and_trusted_scope_are_transactional(source):
    repository, root, admit = source
    a, b, c, d = (request() for _ in range(4))
    missing = random_id("session")
    a = request(agent_id=a.agent_id, work_id=a.work_id,
                instruction=f"`{b.agent_id}` {b.agent_id}, {missing} {root.guard.session_id} {a.agent_id} {c.agent_id}extra",
                initial_context=(f"selected context: {c.agent_id}",))
    admit(a, b, c, d)
    assert relations(repository, root.guard.session_id) == {(a.agent_id, b.agent_id), (a.agent_id, c.agent_id)}
    root_id = root.guard.session_id
    own = repository.list_query_agent_sessions(root_session_id=root_id, observer_session_id=a.agent_id,
                                                limit=20, deadline_monotonic=deadline())
    assert {r["agent_id"] for r in own} == {a.agent_id, b.agent_id, c.agent_id}
    all_rows = repository.list_query_agent_sessions(root_session_id=root_id, observer_session_id=root_id,
                                                     limit=20, deadline_monotonic=deadline())
    assert {r["agent_id"] for r in all_rows} == {a.agent_id, b.agent_id, c.agent_id, d.agent_id}
    assert query_work(repository, root_id, a.agent_id, a.work_id)["work_id"] == a.work_id
    assert query_work(repository, root_id, a.agent_id, b.work_id) == query_work(repository, root_id, root_id, b.work_id)
    assert query_work(repository, root_id, a.agent_id, d.work_id) is None
    with pytest.raises(ValueError, match="unavailable"):
        repository.list_query_agent_works(root_session_id=root_id, observer_session_id=a.agent_id,
                                          agent_id=d.agent_id, deadline_monotonic=deadline())
    # Discovering or reading an unrelated Work does not mutate the accepted set.
    assert relations(repository, root.guard.session_id) == {(a.agent_id, b.agent_id), (a.agent_id, c.agent_id)}


def test_dependency_source_and_material_text_are_introduced_at_distinct_times(source):
    repository, root, admit = source
    producer, mentioned, receiver = request(), request(), request()
    receiver = request(agent_id=receiver.agent_id, work_id=receiver.work_id, depends_on=(producer.work_id,))
    admit(producer, mentioned, receiver)
    assert relations(repository, root.guard.session_id) == {(receiver.agent_id, producer.agent_id)}
    lease, candidate = start(repository, producer)
    finish(repository, lease, candidate, f"completed result mentions {mentioned.agent_id}")
    assert relations(repository, root.guard.session_id) == {(receiver.agent_id, producer.agent_id)}
    repository.resolve_agent_dependencies(session_id=receiver.agent_id, deadline_monotonic=deadline())
    assert relations(repository, root.guard.session_id) == {(receiver.agent_id, producer.agent_id), (receiver.agent_id, mentioned.agent_id)}
    # Material is a newly admitted input, even if the same text was a Work result.
    consumer = request(materials=(producer.work_id,))
    admit(consumer)
    assert (consumer.agent_id, producer.agent_id) in relations(repository, root.guard.session_id)
    assert (consumer.agent_id, mentioned.agent_id) in relations(repository, root.guard.session_id)


def test_foreign_root_candidate_and_observer_never_enter_query_scope(source, stage2_migrated_postgres_database):
    repository, root, admit = source
    foreign_repository, foreign_root, foreign_admit = source_fixture.__wrapped__(stage2_migrated_postgres_database)
    foreign = request()
    foreign_admit(foreign)
    own = request(instruction=f'Ignore unrelated {foreign.agent_id}')
    admit(own)
    assert relations(repository, root.guard.session_id) == set()
    assert query_work(repository, root.guard.session_id, foreign.agent_id, own.work_id) is None
    assert query_work(foreign_repository, foreign_root.guard.session_id, own.agent_id, foreign.work_id) is None
    assert not repository.list_query_agent_sessions(root_session_id=root.guard.session_id,
        observer_session_id=foreign.agent_id, limit=20, deadline_monotonic=deadline())
    assert not foreign_repository.list_query_agent_sessions(root_session_id=foreign_root.guard.session_id,
        observer_session_id=own.agent_id, limit=20, deadline_monotonic=deadline())


def test_active_message_is_bidirectional_only_for_endpoints_and_rolls_back_on_failure(source, monkeypatch):
    repository, root, admit = source
    a, b, c = request(), request(), request()
    admit(a, b, c)
    la, ca = start(repository, a)
    lb, cb = start(repository, b)
    queue_id = random_id("queue")
    args = dict(target_turn_id=cb.exact_turn_id, queue_item_id=queue_id,
                source_session_id=a.agent_id, source_work_id=a.work_id,
                text=f"please see {c.agent_id}", occurred_at=now(), actor_id=a.agent_id,
                deadline_monotonic=deadline())
    assert repository.enqueue_agent_steer(lb.guard, **args) is not None
    assert repository.enqueue_agent_steer(lb.guard, **args) is not None
    expected = {(a.agent_id, b.agent_id), (b.agent_id, a.agent_id), (b.agent_id, c.agent_id)}
    assert relations(repository, root.guard.session_id) == expected
    assert repository.enqueue_agent_steer(lb.guard, **{**args, "queue_item_id": random_id("queue"), "target_turn_id": random_id("turn")}) is None
    assert relations(repository, root.guard.session_id) == expected
    original = repository._merge_known_agents
    def fail_after_write(connection, **kwargs):
        original(connection, **kwargs)
        raise RuntimeError("injected relation failure")
    monkeypatch.setattr(repository, "_merge_known_agents", fail_after_write)
    failed = random_id("queue")
    with pytest.raises(RuntimeError, match="injected relation failure"):
        repository.enqueue_agent_steer(la.guard, **{**args, "target_turn_id": ca.exact_turn_id,
            "queue_item_id": failed, "source_session_id": c.agent_id, "source_work_id": c.work_id})
    assert relations(repository, root.guard.session_id) == expected
    with repository.connection_provider.connection(lane=PostgresConnectionLane.INSPECTOR,
            deadline_monotonic=deadline()) as connection:
        assert connection.execute("SELECT 1 FROM pulsara_v3.prompt_queue_items WHERE id=%s", (failed,)).fetchone() is None


def test_simultaneous_opposite_messages_keep_both_accepted_relations(source):
    repository, root, admit = source
    a, b = request(), request()
    admit(a, b)
    la, ca = start(repository, a)
    lb, cb = start(repository, b)
    barrier = Barrier(2)
    def send(lease, target, sender):
        barrier.wait(timeout=10)
        return repository.enqueue_agent_steer(lease.guard, target_turn_id=target.exact_turn_id,
            queue_item_id=random_id("queue"), source_session_id=sender.agent_id,
            source_work_id=sender.work_id, text="hello", occurred_at=now(), actor_id=sender.agent_id,
            deadline_monotonic=deadline())
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(send, la, ca, b)
        second = pool.submit(send, lb, cb, a)
        assert first.result(timeout=30) is not None
        assert second.result(timeout=30) is not None
    assert relations(repository, root.guard.session_id) == {(a.agent_id, b.agent_id), (b.agent_id, a.agent_id)}


def test_scope_filters_before_pagination_and_unchanged_root_inventory(source):
    repository, root, admit = source
    members = sorted((request() for _ in range(121)), key=lambda r: r.agent_id)
    for offset in range(0, len(members), 16):
        admit(*members[offset:offset + 16])
    observer = members[0]
    introduced = members[1::2]
    admit(request(agent_id=observer.agent_id, create=False,
                  instruction=" ".join(member.agent_id for member in introduced)))
    expected = sorted([observer.agent_id, *(member.agent_id for member in introduced)])
    pages, cursor = [], None
    while True:
        page = repository.list_query_agent_sessions(
            root_session_id=root.guard.session_id, observer_session_id=observer.agent_id,
            cursor=cursor, limit=50, deadline_monotonic=deadline(),
        )
        pages.append(page)
        if len(page) < 50:
            break
        cursor = page[-1]["agent_id"]
    assert [len(page) for page in pages] == [50, len(expected) - 50]
    assert [item["agent_id"] for page in pages for item in page] == expected
    root_page = repository.list_query_agent_sessions(
        root_session_id=root.guard.session_id, observer_session_id=root.guard.session_id,
        limit=50, deadline_monotonic=deadline(),
    )
    assert [row["agent_id"] for row in root_page] == [r.agent_id for r in members[:50]]
    active_page = repository.list_query_agent_sessions(
        root_session_id=root.guard.session_id, observer_session_id=observer.agent_id,
        limit=50, active_only=True, deadline_monotonic=deadline(),
    )
    assert [row["agent_id"] for row in active_page] == expected[:50]


def test_batch_and_active_message_interleave_without_losing_either_admission(source, monkeypatch):
    repository, root, admit = source
    a, b = request(), request()
    admit(a, b)
    _, _ = start(repository, a)
    lb, cb = start(repository, b)
    next_a = request(agent_id=a.agent_id, create=False, instruction=f"Review {b.agent_id}")
    gate = Barrier(2)
    original = repository._merge_known_agents
    def interleave(connection, **kwargs):
        gate.wait(timeout=10)
        return original(connection, **kwargs)
    monkeypatch.setattr(repository, "_merge_known_agents", interleave)
    message_id = random_id("queue")
    with ThreadPoolExecutor(max_workers=2) as pool:
        admitted = pool.submit(admit, next_a)
        delivered = pool.submit(repository.enqueue_agent_steer, lb.guard,
            target_turn_id=cb.exact_turn_id, queue_item_id=message_id,
            source_session_id=a.agent_id, source_work_id=a.work_id, text="hello",
            occurred_at=now(), actor_id=a.agent_id, deadline_monotonic=deadline())
        assert admitted.result(timeout=30)[0]["work_id"] == next_a.work_id
        assert delivered.result(timeout=30)["id"] == message_id
    assert relations(repository, root.guard.session_id) == {(a.agent_id, b.agent_id), (b.agent_id, a.agent_id)}


def test_candidate_deleted_before_merge_is_ignored_without_rejecting_work(source, monkeypatch):
    repository, root, admit = source
    a, target = request(), request()
    admit(a, target)
    next_a = request(agent_id=a.agent_id, create=False, instruction=f"Review {target.agent_id}")
    merge_reached, resume = Event(), Event()
    original = repository._merge_known_agents
    def paused(connection, **kwargs):
        merge_reached.set()
        assert resume.wait(10)
        return original(connection, **kwargs)
    monkeypatch.setattr(repository, "_merge_known_agents", paused)
    with ThreadPoolExecutor(max_workers=1) as pool:
        accepted = pool.submit(admit, next_a)
        assert merge_reached.wait(10)
        assert repository.delete_session(session_id=target.agent_id, memory_domain_id="u_local",
            closed_writer=None, required_lifecycle=None, deadline_monotonic=deadline()) == "DELETED"
        resume.set()
        assert accepted.result(timeout=15)[0]["work_id"] == next_a.work_id
    assert relations(repository, root.guard.session_id) == set()
    assert query_work(repository, root.guard.session_id, a.agent_id, next_a.work_id) is not None


def test_candidate_deleted_after_merge_is_cascaded_after_accepted_work(source, monkeypatch):
    repository, root, admit = source
    a, target = request(), request()
    admit(a, target)
    next_a = request(agent_id=a.agent_id, create=False, instruction=f"Review {target.agent_id}")
    locked, resume, delete_started = Event(), Event(), Event()
    original = repository._merge_known_agents
    def paused(connection, **kwargs):
        original(connection, **kwargs)
        locked.set()
        assert resume.wait(10)
    def remove():
        delete_started.set()
        return repository.delete_session(session_id=target.agent_id, memory_domain_id="u_local",
            closed_writer=None, required_lifecycle=None, deadline_monotonic=deadline())
    monkeypatch.setattr(repository, "_merge_known_agents", paused)
    with ThreadPoolExecutor(max_workers=2) as pool:
        accepted = pool.submit(admit, next_a)
        assert locked.wait(10)
        deleted = pool.submit(remove)
        assert delete_started.wait(5)
        resume.set()
        assert accepted.result(timeout=15)[0]["work_id"] == next_a.work_id
        assert deleted.result(timeout=15) == "DELETED"
    assert relations(repository, root.guard.session_id) == set()
    assert query_work(repository, root.guard.session_id, a.agent_id, next_a.work_id) is not None


@pytest.mark.parametrize("failure", [DeadlockDetected, SerializationFailure])
def test_retry_rolls_back_whole_admission_and_preserves_identity(source, monkeypatch, failure):
    repository, root, admit = source
    a, b = request(), request()
    admit(a, b)
    next_a = request(agent_id=a.agent_id, create=False, instruction=f"Review {b.agent_id}")
    calls = []
    original = repository._merge_known_agents
    def fail_once(connection, **kwargs):
        calls.append(1)
        original(connection, **kwargs)
        if len(calls) == 1:
            raise failure("injected transaction conflict after relationship insert")
    monkeypatch.setattr(repository, "_merge_known_agents", fail_once)
    assert admit(next_a)[0]["work_id"] == next_a.work_id
    assert len(calls) == 2
    assert relations(repository, root.guard.session_id) == {(a.agent_id, b.agent_id)}
    with repository.connection_provider.connection(lane=PostgresConnectionLane.INSPECTOR,
            deadline_monotonic=deadline()) as connection:
        assert connection.execute("SELECT count(*) FROM pulsara_v3.prompt_queue_items WHERE id=%s",
                                  (next_a.work_id,)).fetchone()[0] == 1


def test_transaction_retry_deadline_expires_without_partial_message(source, monkeypatch):
    from pulsara_agent.conversation_kernel._repository import subagents as owner

    repository, root, admit = source
    a, b = request(), request()
    admit(a, b)
    lb, cb = start(repository, b)
    message_id = random_id("queue")
    original = repository._merge_known_agents
    deadline_at = deadline()
    calls = []
    def conflict(connection, **kwargs):
        original(connection, **kwargs)
        calls.append(1)
        raise DeadlockDetected("injected conflict after relationship insert")
    monkeypatch.setattr(repository, "_merge_known_agents", conflict)
    monkeypatch.setattr(owner, "monotonic", lambda: deadline_at + 1 if calls else monotonic())
    with pytest.raises(TimeoutError, match="deadline"):
        repository.enqueue_agent_steer(lb.guard, target_turn_id=cb.exact_turn_id,
            queue_item_id=message_id, source_session_id=a.agent_id, source_work_id=a.work_id,
            text="hello", occurred_at=now(), actor_id=a.agent_id, deadline_monotonic=deadline_at)
    assert len(calls) == 1
    assert relations(repository, root.guard.session_id) == set()
    with repository.connection_provider.connection(lane=PostgresConnectionLane.INSPECTOR,
            deadline_monotonic=deadline()) as connection:
        assert connection.execute("SELECT 1 FROM pulsara_v3.prompt_queue_items WHERE id=%s", (message_id,)).fetchone() is None


def test_dependency_materialization_retries_real_deadlock_against_deletion(source, monkeypatch):
    from pulsara_agent.conversation_kernel._repository import deletion as deletion_owner

    repository, root, admit = source
    producer = request()
    candidate = request(depends_on=(producer.work_id,))
    receiver = request(depends_on=(producer.work_id,))
    admit(producer, candidate, receiver)
    lease, turn = start(repository, producer)
    finish(repository, lease, turn, f"result introduces {candidate.agent_id}")
    reached, resume, row_locked = Event(), Event(), Event()
    original = repository._merge_known_agents
    merge_calls = []
    def gated(connection, **kwargs):
        if (receiver.agent_id, candidate.agent_id) in kwargs['pairs']:
            merge_calls.append(1)
            if len(merge_calls) == 1:
                reached.set()
                assert resume.wait(15)
        return original(connection, **kwargs)
    monkeypatch.setattr(repository, '_merge_known_agents', gated)
    backend, delete_calls = [], []
    original_lock = deletion_owner.lock_canonical_identities
    def after_session_lock(connection, **kwargs):
        original_lock(connection, **kwargs)
        backend[:] = [connection.info.backend_pid]
        delete_calls.append(1)
        row_locked.set()
    monkeypatch.setattr(deletion_owner, 'lock_canonical_identities', after_session_lock)
    def delete_candidate():
        return repository.delete_session(session_id=candidate.agent_id, memory_domain_id='u_local',
            closed_writer=None, required_lifecycle=None, deadline_monotonic=deadline())
    with ThreadPoolExecutor(max_workers=2) as pool:
        materialize = pool.submit(repository.resolve_agent_dependencies,
                                  session_id=receiver.agent_id, deadline_monotonic=deadline())
        assert reached.wait(10)
        deletion = pool.submit(delete_candidate)
        assert row_locked.wait(10)
        with repository.connection_provider.connection(lane=PostgresConnectionLane.INSPECTOR,
                deadline_monotonic=deadline()) as observer:
            limit = monotonic() + 10
            while monotonic() < limit:
                wait = observer.execute('SELECT wait_event_type FROM pg_catalog.pg_stat_activity WHERE pid=%s', (backend[0],)).fetchone()
                if wait and wait[0] == 'Lock':
                    break
                sleep(0.01)
            else:
                pytest.fail('deletion never blocked on the resolver-owned queue row')
        resume.set()
        assert deletion.result(timeout=20) == 'DELETED'
        materialize.result(timeout=20)
    assert len(merge_calls) >= 2 or len(delete_calls) >= 2  # PostgreSQL retried its chosen victim.
    assert (receiver.agent_id, candidate.agent_id) not in relations(repository, root.guard.session_id)
    _, consumed = start(repository, receiver)
    assert consumed.ordered_input_entries[-1].canonical_prompt.content.parts[0].text == f"result introduces {candidate.agent_id}"
