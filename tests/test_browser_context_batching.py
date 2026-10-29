"""Set-based context reads retain exact groups, cuts and streamed creator budgets."""

from contextlib import contextmanager
import json
from time import monotonic

import pytest
import psycopg
from psycopg.types.json import Jsonb

from pulsara_agent.terminal_protocol.canonical_v3 import (
    CanonicalProtocolReader,
    CanonicalProtocolResourceExhausted,
)
from tests.test_conversation_fork import repo as repo, new_session, rows, turn, fork, compact, final
from tests.test_turn_interruption_history import stop


pytestmark = pytest.mark.postgres


def context(reader, session, ids, **kwargs):
    return reader.browser_history_context(
        session_id=session, memory_domain_id="u_local", entry_ids=ids,
        deadline_monotonic=monotonic() + 30, **kwargs,
    )


class _SplitCreators:
    def __init__(self, cursor):
        object.__setattr__(self, "native", cursor)

    def __getattr__(self, name):
        return getattr(self.native, name)

    def __setattr__(self, name, value):
        setattr(self.native, name, value)

    def __enter__(self):
        self.native.__enter__()
        return self

    def __exit__(self, *args):
        return self.native.__exit__(*args)

    def fetchmany(self, size):
        # Exercise multiple physical deliveries without changing the product's
        # entry admission or imposing a total attempt limit.
        return self.native.fetchmany(1)


def traced_reader(repo, monkeypatch, *, split_creators=False):
    reader = CanonicalProtocolReader(repo.connection_provider)
    original = reader._connection
    statements = []

    class Connection:
        def __init__(self, native):
            self.native = native

        def execute(self, sql, *args, **kwargs):
            statements.append(str(sql))
            return self.native.execute(sql, *args, **kwargs)

        def cursor(self, *args, **kwargs):
            cursor = self.native.cursor(*args, **kwargs)
            if split_creators and kwargs.get("name") == "browser_history_creators":
                return _SplitCreators(cursor)
            return cursor

        def __getattr__(self, name):
            return getattr(self.native, name)

    @contextmanager
    def connection(deadline):
        with original(deadline) as native:
            yield Connection(native)

    monkeypatch.setattr(reader, "_connection", connection)
    return reader, statements


def test_root_group_query_count_does_not_grow_with_the_exact_entry_set(repo, monkeypatch):
    lease = new_session(repo)
    answers = [turn(repo, lease.guard, str(index))[2] for index in range(24)]
    reader, statements = traced_reader(repo, monkeypatch)
    first = context(reader, lease.guard.session_id, answers[:1])
    first_queries = len(statements)
    statements.clear()
    value = context(reader, lease.guard.session_id, list(reversed(answers)))
    assert len(statements) == first_queries
    assert len(value["groups"]) == len(answers)
    assert value["entry_contexts"][0] == first["entry_contexts"][0]
    assert [group["owner_id"] for group in value["groups"]] == [
        item["owner_id"] for item in value["entry_contexts"]
    ]
    assert [item["entry_id"] for item in value["entry_contexts"]] == answers
    size = len(json.dumps(value).encode())
    assert context(reader, lease.guard.session_id, answers, maximum_serialized_bytes=size) == value
    with pytest.raises(CanonicalProtocolResourceExhausted):
        context(reader, lease.guard.session_id, answers, maximum_serialized_bytes=size - 1)


def test_sparse_latest_entry_keeps_segment_mounts_lazy_after_many_legal_adoptions(repo):
    lease = new_session(repo)
    session = lease.guard.session_id
    owner, _, _ = turn(repo, lease.guard, "long running ROOT", finish=False)
    for index in range(32):
        compact(repo, lease.guard, owner, 1, f"summary {index}")
    answer = final(repo, lease.guard, owner)
    reader = CanonicalProtocolReader(repo.connection_provider)
    value = context(reader, session, [answer])
    latest = rows(repo, """SELECT event_id FROM pulsara_v3.agent_events
        WHERE session_id=%s AND event_type='CompactionAdopted' ORDER BY event_sequence DESC LIMIT 1""", (session,))[0]
    assert value["entry_contexts"][0]["segment_id"] == f"EXECUTED_TURN:{owner}:{latest['event_id']}"
    initial = str(rows(repo, "SELECT initial_entry_id FROM pulsara_v3.turns WHERE session_id=%s AND id=%s",
        (session, owner))[0]["initial_entry_id"])
    before = context(reader, session, [initial])
    assert before["entry_contexts"][0]["segment_id"] == f"EXECUTED_TURN:{owner}:initial"
    first_adoption = int(rows(repo, """SELECT min(event_sequence) AS sequence FROM pulsara_v3.agent_events
        WHERE session_id=%s AND event_type='CompactionAdopted'""", (session,))[0]["sequence"])
    frozen = context(reader, session, [initial], cut_sequence=1, event_sequence_cut=first_adoption - 1)
    assert frozen["entry_contexts"][0]["segment_id"] == f"EXECUTED_TURN:{owner}:initial"

    class Capture:
        def execute(self, sql, parameters):
            self.sql, self.parameters = sql, parameters
            return self

        def fetchall(self):
            return []

    capture = Capture()
    reader._browser_context_entry_rows(capture, session, [answer],
        int(value["cut_sequence"]), int(value["event_sequence_cut"]))
    with reader._connection(monotonic() + 30) as connection:
        plan = connection.execute("EXPLAIN (ANALYZE, FORMAT JSON) " + capture.sql,
            capture.parameters).fetchone()["QUERY PLAN"][0]["Plan"]
    # The candidate events may be read once, but resolving their canonical
    # entry boundary must stop at the latest eligible event for this entry.
    def nodes(node):
        yield node
        for child in node.get("Plans", ()):
            yield from nodes(child)

    mounts = [node for node in nodes(plan) if node.get("Alias") == "mounted"]
    assert mounts, plan
    assert all(node["Actual Loops"] == 1 for node in mounts), mounts
    candidates = [node for node in nodes(plan) if node.get("Alias") == "ev"]
    assert candidates, plan
    assert all(node["Actual Loops"] == 1 and node["Actual Rows"] == 1 for node in candidates), candidates




def test_later_malformed_imported_group_cannot_preempt_an_earlier_byte_rejection(
    repo, stage2_migrated_postgres_database,
):
    lease = new_session(repo)
    for index in range(10):
        turn(repo, lease.guard, str(index))
    interrupted, _, _ = turn(repo, lease.guard, "interrupted", finish=False)
    stop(repo, lease.guard, interrupted)
    _, _, answer = turn(repo, lease.guard, "anchor")
    child = fork(repo, lease.guard.session_id, answer)
    assert child.created
    session = child.child_session_id
    reader = CanonicalProtocolReader(repo.connection_provider)
    ids = [str(item["id"]) for item in rows(repo,
        "SELECT id FROM pulsara_v3.transcript_entries WHERE session_id=%s ORDER BY entry_sequence", (session,))]
    value = context(reader, session, ids)
    envelope = {**value, **{name: [] for name in (
        "groups", "entry_contexts", "tasks", "batches", "interruption_notices", "compactions", "boundary_entries",
    )}}
    budget = len(json.dumps(envelope).encode()) + len(json.dumps(value["groups"][0]).encode()) - 1
    assert budget >= 1024
    damaged = rows(repo, """SELECT id, interruption_outcome FROM pulsara_v3.imported_history_groups
        WHERE session_id=%s AND status='INTERRUPTED'""", (session,))[0]
    with psycopg.connect(stage2_migrated_postgres_database.admin_dsn, autocommit=True) as admin:
        try:
            for outcome, error in (
                ({"public_detail": "large diagnostic " * 10000}, KeyError),
                ({"display_after_entry_sequence": "invalid integer"}, ValueError),
                ({"display_after_entry_sequence": None}, TypeError),
                ({"display_after_entry_sequence": {"unbounded": "diagnostic " * 10000}}, TypeError),
                ({"display_after_entry_sequence": "9" * 5000}, ValueError),
            ):
                admin.execute("UPDATE pulsara_v3.imported_history_groups SET interruption_outcome=%s WHERE session_id=%s AND id=%s",
                    (Jsonb(outcome), session, damaged["id"]))
                with pytest.raises(CanonicalProtocolResourceExhausted):
                    context(reader, session, ids, maximum_serialized_bytes=budget)
                with pytest.raises(error):
                    context(reader, session, ids)
        finally:
            admin.execute("UPDATE pulsara_v3.imported_history_groups SET interruption_outcome=%s WHERE session_id=%s AND id=%s",
                (Jsonb(damaged["interruption_outcome"]), session, damaged["id"]))
