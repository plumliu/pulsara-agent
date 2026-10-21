"""Publication uses real canonical builds, transactions and competing backends."""

from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from copy import deepcopy
from dataclasses import replace
from datetime import datetime, timezone
from threading import Event
from time import monotonic, sleep
from uuid import uuid4

import psycopg
from psycopg import sql
import pytest

from pulsara_agent.conversation_kernel.contracts import InlineContent
from pulsara_agent.conversation_kernel.history_projection import (
    HistoryBuildFailure,
    HistoryBuildSuccess,
)
from pulsara_agent.conversation_kernel.history_publication import (
    HistoryProjectionRepository,
    HistoryQuestionKey,
)
from pulsara_agent.conversation_kernel.repository import (
    AssistantTextBlock,
    ConversationKernelRepository,
    StaleHostWriter,
)
from pulsara_agent.storage.postgres_connection_provider import PostgresConnectionLane
from tests.test_conversation_fork import identity, new_session, repo as repo, rows, turn


def _deadline():
    return monotonic() + 20


class _ConnectionObserver:
    def __init__(self, connection, before, after):
        self.connection = connection
        self.before = before
        self.after = after

    def __getattr__(self, name):
        return getattr(self.connection, name)

    def execute(self, query, params=None, **kwargs):
        text = query if isinstance(query, str) else query.as_string(self.connection)
        if self.before:
            self.before(text, self.connection, params)
        cursor = self.connection.execute(query, params, **kwargs)
        if self.after:
            self.after(text, self.connection, params)
        return cursor


class _ObservedProvider:
    def __init__(self, provider, *, before=None, after=None):
        self.provider, self.before, self.after = provider, before, after

    def __getattr__(self, name):
        return getattr(self.provider, name)

    @contextmanager
    def connection(self, **kwargs):
        with self.provider.connection(**kwargs) as connection:
            yield _ConnectionObserver(connection, self.before, self.after)


def _append(repo, guard, owner, text, *, complete=False):
    cut = repo.prepare_provider_input_cut(
        guard, turn_id=owner, deadline_monotonic=_deadline()
    )
    entry_id = identity("entry")
    repo.commit_assistant_message(
        guard, cut=cut, entry_id=entry_id,
        parent_content=InlineContent.from_bytes(b"manifest"),
        blocks=(AssistantTextBlock(identity("block"), InlineContent.from_bytes(text.encode())),),
        complete_turn=complete, occurred_at=datetime.now(timezone.utc),
        actor_id="model:publication-test", deadline_monotonic=_deadline(),
    )
    return entry_id


def _q1(repo, sid, owner):
    return rows(repo, "SELECT id FROM pulsara_v3.transcript_entries WHERE session_id=%s AND turn_id=%s AND entry_kind='USER_MESSAGE'", (sid, owner))[0]["id"]


def _group(repo, sid, qid):
    return rows(repo, "SELECT * FROM pulsara_v3.history_search_documents WHERE session_id=%s AND q1_entry_id=%s ORDER BY variant,include_tools", (sid, qid))


def _blocked(repo, waiter, holder):
    deadline = _deadline()
    while monotonic() < deadline:
        state = rows(repo, "SELECT wait_event_type,wait_event,pg_blocking_pids(pid) AS blockers FROM pg_stat_activity WHERE pid=%s", (waiter,))
        if state and holder in state[0]["blockers"]:
            assert state[0]["wait_event_type"] == "Lock"
            return state[0]
        sleep(0.005)  # Observation pacing, never a substitute for a lock edge.
    pytest.fail(f"no server blocking edge {waiter} -> {holder}; state={state}")


def _parent_gate(provider):
    held, release = Event(), Event()
    state = {}

    def after(sql, connection, _params):
        if "FROM pulsara_v3.sessions" in sql and "FOR KEY SHARE" in sql:
            state["pid"] = connection.info.backend_pid
            held.set()
            assert release.wait(20), "test did not release parent gate"

    return _ObservedProvider(provider, after=after), held, release, state


def test_publication_statuses_monotonicity_same_cut_repair_and_zero(repo):
    guard = new_session(repo).guard
    owner, _, _ = turn(repo, guard, "Q1中文🪐", finish=False)
    sid, qid = guard.session_id, _q1(repo, guard.session_id, owner)
    service = HistoryProjectionRepository(repo.connection_provider)
    zero = service.build_question(sid, qid, deadline_monotonic=_deadline())
    assert isinstance(zero, HistoryBuildSuccess) and not zero.documents
    assert service.publish(zero, deadline_monotonic=_deadline()) == "published"
    _append(repo, guard, owner, "old accepted needle")
    old = service.build_question(sid, qid, deadline_monotonic=_deadline())
    _append(repo, guard, owner, "new accepted needle", complete=True)
    new = service.build_question(sid, qid, deadline_monotonic=_deadline())
    assert service.publish(old, deadline_monotonic=_deadline()) == "published"
    assert service.publish(new, deadline_monotonic=_deadline()) == "published"
    complete = _group(repo, sid, qid)
    assert service.publish(old, deadline_monotonic=_deadline()) == "stale_skipped"
    assert service.publish(zero, deadline_monotonic=_deadline()) == "stale_skipped"
    assert _group(repo, sid, qid) == complete
    with repo.connection_provider.connection(lane=PostgresConnectionLane.BACKGROUND_WORK, deadline_monotonic=_deadline()) as c:
        c.execute("DELETE FROM pulsara_v3.history_search_documents WHERE session_id=%s AND q1_entry_id=%s AND include_tools=false", (sid, qid))
    assert len(_group(repo, sid, qid)) == 1
    assert service.publish(new, deadline_monotonic=_deadline()) == "published"
    assert _group(repo, sid, qid) == complete
    missing = service.build_question(sid, identity("entry"), deadline_monotonic=_deadline())
    assert service.publish(missing, deadline_monotonic=_deadline()) == "question_missing"
    absent = service.build_question(identity("session"), qid, deadline_monotonic=_deadline())
    assert service.publish(absent, deadline_monotonic=_deadline()) == "parent_missing"


def test_second_variant_failure_rolls_back_exact_group_and_releases_admission(repo):
    guard = new_session(repo).guard
    owner, _, _ = turn(repo, guard, "rollback Q1", finish=False)
    qid = _q1(repo, guard.session_id, owner)
    _append(repo, guard, owner, "old body")
    service = HistoryProjectionRepository(repo.connection_provider)
    old = service.build_question(guard.session_id, qid, deadline_monotonic=_deadline())
    assert service.publish(old, deadline_monotonic=_deadline()) == "published"
    previous = _group(repo, guard.session_id, qid)
    _append(repo, guard, owner, "new body", complete=True)
    new = service.build_question(guard.session_id, qid, deadline_monotonic=_deadline())
    inserted = 0

    def fail_second(sql, _c, _params):
        nonlocal inserted
        if "INSERT INTO pulsara_v3.history_search_documents" in sql:
            inserted += 1
            if inserted == 2:
                raise RuntimeError("injected second variant failure")

    failing = HistoryProjectionRepository(_ObservedProvider(repo.connection_provider, after=fail_second))
    with pytest.raises(RuntimeError, match="second variant"):
        failing.publish(new, deadline_monotonic=_deadline())
    assert inserted == 2 and _group(repo, guard.session_id, qid) == previous
    assert service.publish(new, deadline_monotonic=_deadline()) == "published"


def test_four_variant_partial_group_is_invisible_until_commit(repo):
    guard = new_session(repo).guard
    owner, _, _ = turn(repo, guard, "atomic Q1", finish=False)
    qid = _q1(repo, guard.session_id, owner)
    _append(repo, guard, owner, "old answer")
    service = HistoryProjectionRepository(repo.connection_provider)
    assert service.maintain_question(guard.session_id, qid, deadline_monotonic=_deadline()) == "published"
    old = _group(repo, guard.session_id, qid)
    _append(repo, guard, owner, "new final answer", complete=True)
    turn(repo, guard, "raw Q2 qualifies QAQ", finish=False)
    build = service.build_question(guard.session_id, qid, deadline_monotonic=_deadline())
    assert isinstance(build, HistoryBuildSuccess) and len(build.documents) == 4
    partial, release = Event(), Event()
    inserted = 0

    def after(query, _connection, _params):
        nonlocal inserted
        if "INSERT INTO pulsara_v3.history_search_documents" in query:
            inserted += 1
            if inserted == 2:
                partial.set()
                assert release.wait(20)

    observed = HistoryProjectionRepository(_ObservedProvider(repo.connection_provider, after=after))
    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(observed.publish, build, deadline_monotonic=_deadline())
        try:
            assert partial.wait(10)
            assert _group(repo, guard.session_id, qid) == old
        finally:
            release.set()
        assert future.result(10) == "published"
    current = _group(repo, guard.session_id, qid)
    assert len(current) == 4 and {r["covered_entry_sequence"] for r in current} == {build.covered_entry_sequence}
    assert all({tuple(v) for v in r["expected_variants"]} == build.expected_variants for r in current)


def test_actual_deferred_commit_failure_preserves_group_and_releases_locks(repo, stage2_migrated_postgres_database):
    guard = new_session(repo).guard
    owner, _, _ = turn(repo, guard, "commit failure", finish=False)
    qid = _q1(repo, guard.session_id, owner)
    _append(repo, guard, owner, "old accepted body")
    service = HistoryProjectionRepository(repo.connection_provider)
    assert service.maintain_question(guard.session_id, qid, deadline_monotonic=_deadline()) == "published"
    old = _group(repo, guard.session_id, qid)
    _append(repo, guard, owner, "new accepted body", complete=True)
    new = service.build_question(guard.session_id, qid, deadline_monotonic=_deadline())
    name = "test_history_commit_" + uuid4().hex
    function = sql.Identifier("pulsara_v3", name)
    trigger = sql.Identifier(name)
    with psycopg.connect(stage2_migrated_postgres_database.admin_dsn, autocommit=True) as admin:
        admin.execute(sql.SQL("""CREATE FUNCTION {}() RETURNS trigger LANGUAGE plpgsql AS $$
          BEGIN RAISE EXCEPTION 'injected deferred history commit failure' USING ERRCODE='23514';
          RETURN NEW; END $$""").format(function))
        admin.execute(sql.SQL("""CREATE CONSTRAINT TRIGGER {} AFTER INSERT
          ON pulsara_v3.history_search_documents DEFERRABLE INITIALLY DEFERRED
          FOR EACH ROW EXECUTE FUNCTION {}()""").format(trigger, function))
        try:
            with pytest.raises(psycopg.errors.CheckViolation, match="deferred history commit"):
                service.publish(new, deadline_monotonic=_deadline())
            assert _group(repo, guard.session_id, qid) == old
        finally:
            admin.execute(sql.SQL("DROP TRIGGER {} ON pulsara_v3.history_search_documents").format(trigger))
            admin.execute(sql.SQL("DROP FUNCTION {}()").format(function))
    assert service.publish(new, deadline_monotonic=_deadline()) == "published"


@pytest.mark.parametrize("same_question", [True, False])
@pytest.mark.parametrize("control", ["takeover", "delete"])
def test_waiting_publishers_hold_no_parent_and_queue_after_control(repo, same_question, control):
    guard = new_session(repo).guard
    first_owner, _, _ = turn(repo, guard, "first question", answer="first answer")
    second_owner, _, _ = turn(repo, guard, "second question", answer="second answer")
    sid = guard.session_id
    first_qid, second_qid = _q1(repo, sid, first_owner), _q1(repo, sid, second_owner)
    service = HistoryProjectionRepository(repo.connection_provider)
    first = service.build_question(sid, first_qid, deadline_monotonic=_deadline())
    second = service.build_question(sid, first_qid if same_question else second_qid, deadline_monotonic=_deadline())
    if control == "delete":
        # No Host/worker exists in this fixture. Release the exact fixture owner
        # before calling the real deletion owner with its closed guard.
        repo.release_host_writer(guard, deadline_monotonic=_deadline())
    gated, held, release, holder = _parent_gate(repo.connection_provider)
    first_service = HistoryProjectionRepository(gated)
    control_wait, publisher_wait = Event(), Event()
    control_state, publisher_state = {}, {"parent_queries": 0}

    def before_control(sql, c, _params):
        if "pg_advisory_xact_lock" in sql:
            control_state["pid"] = c.info.backend_pid
            control_wait.set()

    def before_second(sql, c, _params):
        if "pg_advisory_xact_lock" in sql:
            publisher_state["pid"] = c.info.backend_pid
            publisher_wait.set()
        if "FROM pulsara_v3.sessions" in sql and "FOR KEY SHARE" in sql:
            publisher_state["parent_queries"] += 1

    def after_second(sql, c, _params):
        if "pg_advisory_xact_lock" in sql:
            publisher_state["generation_after_admission"] = c.execute("SELECT writer_generation FROM pulsara_v3.sessions WHERE id=%s", (sid,)).fetchone()

    controlling_repo = ConversationKernelRepository(_ObservedProvider(repo.connection_provider, before=before_control))
    second_service = HistoryProjectionRepository(_ObservedProvider(repo.connection_provider, before=before_second, after=after_second))

    def control_operation():
        if control == "delete":
            return controlling_repo.delete_session(session_id=sid, memory_domain_id="u_local", closed_writer=guard, required_lifecycle=None, deadline_monotonic=_deadline())
        workspace = rows(repo, "SELECT workspace_id FROM pulsara_v3.sessions WHERE id=%s", (sid,))[0]["workspace_id"]
        return controlling_repo.acquire_host_writer(intent="EXISTING", session_id=sid, workspace_id=workspace, writer_owner_id=identity("host"), lease_seconds=300, deadline_monotonic=_deadline())

    with ThreadPoolExecutor(max_workers=3) as executor:
        one = executor.submit(first_service.publish, first, deadline_monotonic=_deadline())
        try:
            assert held.wait(10)
            strong = executor.submit(control_operation)
            assert control_wait.wait(10)
            assert _blocked(repo, control_state["pid"], holder["pid"])["wait_event"] == "advisory"
            two = executor.submit(second_service.publish, second, deadline_monotonic=_deadline())
            assert publisher_wait.wait(10)
            assert _blocked(repo, publisher_state["pid"], holder["pid"])["wait_event"] == "advisory"
            assert publisher_state["parent_queries"] == 0
            release.set()
            assert one.result(10) == "published"
            result = strong.result(10)
            if control == "delete":
                assert result == "DELETED" and two.result(10) == "parent_missing"
                assert publisher_state["generation_after_admission"] is None
                assert not rows(repo, "SELECT id FROM pulsara_v3.transcript_entries WHERE session_id=%s", (sid,))
                assert not rows(repo, "SELECT id FROM pulsara_v3.history_search_documents WHERE session_id=%s", (sid,))
            else:
                assert result.guard.writer_generation == guard.writer_generation + 1
                assert two.result(10) == "published"
                assert publisher_state["generation_after_admission"]["writer_generation"] == result.guard.writer_generation
            with pytest.raises(StaleHostWriter):
                repo.validate_host_writer(guard, deadline_monotonic=_deadline())
        finally:
            release.set()


def test_cross_session_publisher_finishes_while_other_session_holds_parent(repo):
    builds = []
    for text in ("session one", "session two"):
        guard = new_session(repo).guard
        owner, _, _ = turn(repo, guard, text, answer="complete answer")
        service = HistoryProjectionRepository(repo.connection_provider)
        builds.append(service.build_question(guard.session_id, _q1(repo, guard.session_id, owner), deadline_monotonic=_deadline()))
    gated, held, release, _ = _parent_gate(repo.connection_provider)
    with ThreadPoolExecutor(max_workers=2) as executor:
        pending = executor.submit(HistoryProjectionRepository(gated).publish, builds[0], deadline_monotonic=_deadline())
        try:
            assert held.wait(10)
            other = executor.submit(HistoryProjectionRepository(repo.connection_provider).publish, builds[1], deadline_monotonic=_deadline())
            assert other.result(10) == "published"
            assert not release.is_set()
        finally:
            release.set()
        assert pending.result(10) == "published"


def test_composite_fk_rejects_foreign_q2_and_publisher_rechecks_q1_session(repo):
    service = HistoryProjectionRepository(repo.connection_provider)
    fixtures = []
    for text in ("own Q1", "foreign Q1"):
        guard = new_session(repo).guard
        owner, _, _ = turn(repo, guard, text, answer="accepted answer")
        fixtures.append((guard.session_id, _q1(repo, guard.session_id, owner)))
    (sid, qid), (_foreign_sid, foreign_qid) = fixtures
    good = service.build_question(sid, qid, deadline_monotonic=_deadline())
    assert isinstance(good, HistoryBuildSuccess)
    assert service.publish(good, deadline_monotonic=_deadline()) == "published"
    old = _group(repo, sid, qid)
    # A trusted builder never emits this forged value. Exercise the independent
    # database boundary, without pretending a single-column FK checks domain.
    bad_documents = deepcopy(good.documents)
    for document in bad_documents.values():
        document["q2_entry_id"] = foreign_qid
    with pytest.raises(psycopg.errors.ForeignKeyViolation):
        service.publish(replace(good, documents=bad_documents), deadline_monotonic=_deadline())
    assert _group(repo, sid, qid) == old
    assert service.publish(replace(good, q1_entry_id=foreign_qid), deadline_monotonic=_deadline()) == "question_missing"
    assert _group(repo, sid, qid) == old


def test_strong_scheduled_admission_precedes_first_writer_parent_lock(repo):
    guard = new_session(repo).guard
    owner, _, _ = turn(repo, guard, "scheduled owner", answer="complete answer")
    build = HistoryProjectionRepository(repo.connection_provider).build_question(guard.session_id, _q1(repo, guard.session_id, owner), deadline_monotonic=_deadline())
    gated, held, release, holder = _parent_gate(repo.connection_provider)
    waiting, state, sql_calls = Event(), {}, []

    def before(sql, c, _params):
        sql_calls.append(sql)
        if "pg_advisory_xact_lock" in sql:
            state["pid"] = c.info.backend_pid
            waiting.set()

    scheduled = ConversationKernelRepository(_ObservedProvider(repo.connection_provider, before=before))

    def operation():
        with scheduled._scheduled_transaction(session_id=guard.session_id, memory_domain_id="u_local", guard=guard, deadline_monotonic=_deadline()) as (_c, row):
            return row["id"]

    with ThreadPoolExecutor(max_workers=2) as executor:
        publish = executor.submit(HistoryProjectionRepository(gated).publish, build, deadline_monotonic=_deadline())
        try:
            assert held.wait(10)
            work = executor.submit(operation)
            assert waiting.wait(10)
            _blocked(repo, state["pid"], holder["pid"])
            assert not any("FOR NO KEY UPDATE" in sql for sql in sql_calls)
            release.set()
            assert publish.result(10) == "published" and work.result(10) == guard.session_id
        finally:
            release.set()
    assert "pg_advisory_xact_lock" in sql_calls[0]
    assert next(i for i, s in enumerate(sql_calls) if "FOR NO KEY UPDATE" in s) < next(i for i, s in enumerate(sql_calls) if "FOR UPDATE" in s)


def test_resolve_owner_and_new_root_predecessor_and_dependency_scan(repo):
    guard = new_session(repo).guard
    first_owner, _, first_a = turn(repo, guard, "first question", answer="first answer")
    sid, q1 = guard.session_id, _q1(repo, guard.session_id, first_owner)
    service = HistoryProjectionRepository(repo.connection_provider)
    assert service.resolve_history_questions(sid, (first_a,), deadline_monotonic=_deadline()) == (HistoryQuestionKey(sid, q1),)
    assert service.maintain_question(sid, q1, deadline_monotonic=_deadline()) == "published"
    second_owner, _, _ = turn(repo, guard, "second question", finish=False)
    q2 = _q1(repo, sid, second_owner)
    assert set(service.resolve_history_questions(sid, (q2,), deadline_monotonic=_deadline())) == {HistoryQuestionKey(sid, q1), HistoryQuestionKey(sid, q2)}
    # A new raw Q2 makes Q1 stale even if its A owner did not advance.
    assert HistoryQuestionKey(sid, q1) in _scan_all(service)
    assert service.maintain_question(sid, q1, deadline_monotonic=_deadline()) == "published"
    second_a = _append(repo, guard, second_owner, "second only answer")
    assert service.resolve_history_questions(sid, (second_a,), deadline_monotonic=_deadline()) == (HistoryQuestionKey(sid, q2),)
    # Unrelated current-owner growth must not dirty historical Q1.
    assert HistoryQuestionKey(sid, q1) not in _scan_all(service)
    assert HistoryQuestionKey(sid, q2) in _scan_all(service)
    assert service.maintain_question(sid, q2, deadline_monotonic=_deadline()) == "published"
    with repo.connection_provider.connection(lane=PostgresConnectionLane.BACKGROUND_WORK, deadline_monotonic=_deadline()) as c:
        c.execute("DELETE FROM pulsara_v3.history_search_documents WHERE session_id=%s AND q1_entry_id=%s AND include_tools=false", (sid, q1))
    assert HistoryQuestionKey(sid, q1) in _scan_all(service)


def _scan_all(service):
    cursor, found = "", set()
    while True:
        page = service.scan_history_questions(cursor, 2, deadline_monotonic=_deadline())
        found.update(page.questions)
        if page.done:
            return found
        assert page.next_cursor > cursor
        cursor = page.next_cursor


def test_required_build_failure_never_replaces_positive_group(repo, monkeypatch):
    guard = new_session(repo).guard
    owner, _, _ = turn(repo, guard, "failure question", answer="accepted answer")
    sid, qid = guard.session_id, _q1(repo, guard.session_id, owner)
    service = HistoryProjectionRepository(repo.connection_provider)
    assert service.maintain_question(sid, qid, deadline_monotonic=_deadline()) == "published"
    old = _group(repo, sid, qid)
    failure = HistoryBuildFailure(sid, qid, 2, "A", old[0]["entry_ids"][-1], RuntimeError("canonical corrupt"))
    monkeypatch.setattr(service, "build_question", lambda *_a, **_kw: failure)
    assert service.maintain_question(sid, qid, deadline_monotonic=_deadline()) == "build_failed"
    with pytest.raises(TypeError, match="successful canonical build"):
        service.publish(failure, deadline_monotonic=_deadline())
    assert _group(repo, sid, qid) == old
