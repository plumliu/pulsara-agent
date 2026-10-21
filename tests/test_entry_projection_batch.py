"""Bounded relation reads preserve the canonical per-entry projection owner."""

from datetime import datetime, timezone
from time import monotonic

import pytest

from pulsara_agent.terminal_protocol.canonical_v3 import CanonicalProtocolReader
from pulsara_agent.conversation_kernel.contracts import InlineContent
from pulsara_agent.conversation_kernel.repository import build_prepared_tool_result_acceptance
from pulsara_agent.identity import random_id
from pulsara_agent.ports.artifact import ToolOutputArtifactDisposition, ToolResultDisplayKind
from pulsara_agent.ports.tool_execution import ToolOutputSourceCoverage
from pulsara_agent.primitives.tool_observation import ToolObservationOrigin
from tests.test_conversation_fork import new_session, repo as repo, rows as read_rows, turn
from tests.test_session_snapshot_loading import _interrupted_attempt


pytestmark = pytest.mark.postgres


class RecordingConnection:
    def __init__(self, connection):
        self.connection = connection
        self.queries = []

    def execute(self, statement, *args, **kwargs):
        self.queries.append(" ".join(str(statement).split()))
        return self.connection.execute(statement, *args, **kwargs)


def _tool_outcome(repo, lease):
    attempt_id = _interrupted_attempt(repo, lease)
    attempt = read_rows(repo,
        """SELECT a.*, e.turn_id, e.workspace_id FROM pulsara_v3.tool_execution_attempts a
           JOIN pulsara_v3.transcript_entries e ON e.session_id=a.session_id AND e.id=a.assistant_entry_id
           WHERE a.session_id=%s AND a.id=%s""", (lease.guard.session_id, attempt_id),
    )[0]
    result_entry_id = random_id("entry")
    value = build_prepared_tool_result_acceptance(
        guard=lease.guard, workspace_id=attempt["workspace_id"], result_id=random_id("result"),
        result_entry_id=result_entry_id, turn_id=attempt["turn_id"],
        assistant_entry_id=attempt["assistant_entry_id"], tool_call_id=attempt["tool_call_id"], attempt_id=attempt_id,
        result_state="SUCCESS", canonical_preview_content=InlineContent.from_bytes(b"outcome"),
        artifact_disposition=ToolOutputArtifactDisposition.NOT_REQUIRED, artifact_id=None,
        artifact_blob_descriptor=None, source_coverage=ToolOutputSourceCoverage.COMPLETE,
        display_kind=ToolResultDisplayKind.COMPLETE, source_coverage_reason=None,
        artifact_unavailability_reason=None, observed_at=datetime.now(timezone.utc),
        observation_duration_microseconds=None, observation_origin_kind=ToolObservationOrigin.TERMINAL_PROCESS,
        trusted_tool_reported_duration_microseconds=None, actor_id="terminal",
    )
    repo.accept_tool_result(lease.guard, candidate=value, deadline_monotonic=monotonic() + 30)
    return result_entry_id


def test_bounded_entry_relations_keep_identical_wire_with_fewer_database_queries(repo):
    lease = new_session(repo)
    for index in range(8):
        turn(repo, lease.guard, str(index))
    reader = CanonicalProtocolReader(repo.connection_provider)
    with reader._connection(monotonic() + 30) as connection:
        rows = connection.execute(
            "SELECT * FROM pulsara_v3.transcript_entries WHERE session_id=%s ORDER BY entry_sequence",
            (lease.guard.session_id,),
        ).fetchall()
        measured = RecordingConnection(connection)
        individual = tuple(reader._entry(measured, row) for row in rows)
        before = len(measured.queries)
        measured.queries.clear()
        batched = reader._entries(measured, rows)
        after = len(measured.queries)
        assert [entry.SerializeToString(deterministic=True) for entry in batched] == [
            entry.SerializeToString(deterministic=True) for entry in individual
        ]
        # Every entry loses a redundant small-relation read, while ROOT fork
        # model/TEXT reads deliberately remain candidate-local.
        assert before - after >= len(rows)
        assert all("FETCH" not in query for query in measured.queries)
        assert all("payload_bytes" not in query for query in measured.queries if "relation_entry_id" in query)


def test_entry_relation_batch_rejects_mixed_session_inputs_before_reading(repo):
    first, other = new_session(repo), new_session(repo)
    turn(repo, first.guard, "first")
    turn(repo, other.guard, "other")
    reader = CanonicalProtocolReader(repo.connection_provider)
    with reader._connection(monotonic() + 30) as connection:
        rows = connection.execute(
            "SELECT * FROM pulsara_v3.transcript_entries WHERE session_id=ANY(%s)",
            ([first.guard.session_id, other.guard.session_id],),
        ).fetchall()
        measured = RecordingConnection(connection)
        with pytest.raises(ValueError, match="crosses canonical sessions"):
            reader._entry_relations(measured, rows)
        assert not measured.queries


def test_entry_relation_batch_preserves_missing_tool_result_error(repo):
    lease = new_session(repo)
    entry_id = _tool_outcome(repo, lease)
    reader = CanonicalProtocolReader(repo.connection_provider)
    with reader._connection(monotonic() + 30) as connection:
        row = connection.execute(
            "SELECT * FROM pulsara_v3.transcript_entries WHERE session_id=%s AND id=%s",
            (lease.guard.session_id, entry_id),
        ).fetchone()
        relations = reader._entry_relations(connection, (row,))
        # Exercise the same relational-edge guard with an absent result owner;
        # no foreign or synthetic result can be installed by metadata batching.
        relations[entry_id]["tool_result_id"] = None
        with pytest.raises(ValueError, match="missing its relational row"):
            reader._entry(connection, row, relations=relations)


def test_user_and_tool_result_projection_uses_one_small_relation_query(repo):
    lease = new_session(repo)
    for _ in range(3):
        _tool_outcome(repo, lease)
    reader = CanonicalProtocolReader(repo.connection_provider)
    with reader._connection(monotonic() + 30) as connection:
        rows = connection.execute(
            """SELECT * FROM pulsara_v3.transcript_entries WHERE session_id=%s
               AND entry_kind IN ('USER_MESSAGE', 'TOOL_RESULT') ORDER BY entry_sequence""",
            (lease.guard.session_id,),
        ).fetchall()
        individual = tuple(reader._entry(connection, row) for row in rows)
        measured = RecordingConnection(connection)
        batched = reader._entries(measured, rows)
        assert len(batched) == 6
        assert [value.SerializeToString(deterministic=True) for value in batched] == [
            value.SerializeToString(deterministic=True) for value in individual
        ]
        assert len(measured.queries) == 1
        assert "payload_bytes" not in measured.queries[0]


def test_fork_owner_is_called_only_when_its_root_candidate_is_projected(repo, monkeypatch):
    from pulsara_agent.conversation_kernel import fork_history

    lease = new_session(repo)
    for index in range(3):
        turn(repo, lease.guard, str(index))
    reader = CanonicalProtocolReader(repo.connection_provider)
    native = fork_history.read_fork_anchor
    touched = []

    def observe(connection, session_id, entry_id, *, candidate_blocks=None):
        touched.append(entry_id)
        return native(connection, session_id, entry_id, candidate_blocks=candidate_blocks)

    monkeypatch.setattr(fork_history, "read_fork_anchor", observe)
    with reader._connection(monotonic() + 30) as connection:
        rows = connection.execute(
            "SELECT * FROM pulsara_v3.transcript_entries WHERE session_id=%s ORDER BY entry_sequence",
            (lease.guard.session_id,),
        ).fetchall()
        measured = RecordingConnection(connection)
        relations = reader._entry_relations(measured, rows)
        assert not touched
        assert len(measured.queries) == 1
        assert "model_call_binding" not in measured.queries[0]
        latest = reader._entry(connection, rows[-1], relations=relations)
        assert latest.fork_eligible
        assert touched == [rows[-1]["id"]]
