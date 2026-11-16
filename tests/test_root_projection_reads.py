"""ROOT projection reuses admitted bytes without moving fork/replay authority."""

from datetime import datetime, timezone
from time import monotonic

import psycopg
import pytest

from pulsara_agent.conversation_kernel.blob import PostgresCanonicalBlobStore
from pulsara_agent.conversation_kernel.contracts import InlineContent
from pulsara_agent.conversation_kernel.fork_history import read_fork_anchor
from pulsara_agent.conversation_kernel.repository import AssistantTextBlock
from pulsara_agent.identity import random_id
from pulsara_agent.llm.provider_replay import (
    build_prepared_durable_provider_assistant_replay,
)
from pulsara_agent.llm.request import provider_assistant_public_projection_fingerprint
from pulsara_agent.primitives.context import freeze_json
from pulsara_agent.terminal_protocol.canonical_v3 import CanonicalProtocolReader
from tests.support.model_config import build_test_provider_replay_target
from tests.test_conversation_fork import new_session, repo as repo, turn


pytestmark = pytest.mark.postgres


class RecordingConnection:
    def __init__(self, connection):
        self.connection = connection
        self.queries = []

    def execute(self, statement, *args, **kwargs):
        self.queries.append(" ".join(str(statement).split()))
        return self.connection.execute(statement, *args, **kwargs)

    def __getattr__(self, name):
        return getattr(self.connection, name)


def blocks(connection, session_id, entry_id):
    return connection.execute(
        "SELECT * FROM pulsara_v3.content_parts WHERE session_id=%s "
        "AND entry_id=%s ORDER BY ordinal",
        (session_id, entry_id),
    ).fetchall()


def final_with_parts(repository, lease, *, blob=False, replay=False, first=b"first"):
    turn_id, cut, _ = turn(repository, lease.guard, "question", finish=False)
    entry_id = random_id("entry")
    workspace_id = repository.read_session_workspace_id(
        lease.guard, deadline_monotonic=monotonic() + 30
    )
    second = (
        PostgresCanonicalBlobStore(repository.connection_provider).publish(
            workspace_id=workspace_id,
            content=b"second" * 12000,
            media_type="text/plain",
            codec="utf-8",
            deadline_monotonic=monotonic() + 30,
        )
        if blob
        else InlineContent.from_bytes(b"second")
    )
    fragment = (
        build_prepared_durable_provider_assistant_replay(
            session_id=lease.guard.session_id,
            workspace_id=workspace_id,
            assistant_entry_id=entry_id,
            target=build_test_provider_replay_target(
                model_id="model", transport_binding_id="test-wire"
            ),
            public_projection_fingerprint=provider_assistant_public_projection_fingerprint(
                text="firstsecond", tool_calls=()
            ),
            ordered_items=(
                freeze_json(
                    {
                        "role": "assistant",
                        "content": "firstsecond",
                        "reasoning_content": "visible thought",
                    }
                ),
            ),
        )
        if replay
        else None
    )
    repository.commit_assistant_message(
        lease.guard,
        cut=cut,
        entry_id=entry_id,
        blocks=(
            AssistantTextBlock(random_id("block"), InlineContent.from_bytes(first)),
            AssistantTextBlock(random_id("block"), second),
        ),
        complete_turn=True,
        provider_replay=fragment,
        occurred_at=datetime.now(timezone.utc),
        actor_id="model:test",
        deadline_monotonic=monotonic() + 30,
    )
    return turn_id, entry_id


@pytest.mark.parametrize(
    ("blob", "first"), [(False, b"first"), (True, b"first"), (True, b" \n")]
)
def test_fork_owner_reuses_only_complete_inline_candidate_sets_in_its_own_order(
    repo, blob, first
):
    lease = new_session(repo)
    _, entry_id = final_with_parts(repo, lease, blob=blob, first=first)
    reader = CanonicalProtocolReader(repo.connection_provider)
    with reader._connection(monotonic() + 30) as connection:
        candidate = blocks(connection, lease.guard.session_id, entry_id)
        measured = RecordingConnection(connection)
        expected = read_fork_anchor(measured, lease.guard.session_id, entry_id)
        assert len(measured.queries) == 2 + 2 * int(blob and not first.strip())
        measured.queries.clear()
        actual = read_fork_anchor(
            measured, lease.guard.session_id, entry_id, candidate_blocks=candidate[::-1]
        )
        assert actual == expected
        assert actual.nonempty_text_block_ordinal == (0 if first.strip() else 1)
        assert len(measured.queries) == 1 + 2 * int(blob and not first.strip())
        assert sum(
            "pulsara_v3.blobs" in query for query in measured.queries
        ) == 2 * int(blob and not first.strip())


@pytest.mark.parametrize("damage", ["empty", "whitespace", "utf8", "digest", "size"])
def test_reused_fork_parts_keep_blob_integrity_utf8_and_nonempty_checks(
    repo, stage2_migrated_postgres_database, damage
):
    lease = new_session(repo)
    if damage in {"digest", "size"}:
        _, entry_id = final_with_parts(repo, lease, blob=True, first=b" ")
    else:
        _, _, entry_id = turn(
            repo,
            lease.guard,
            "question",
            answer={
                "empty": "",
                "whitespace": " \n\t",
                "utf8": "valid before corruption",
            }[damage],
        )
    with psycopg.connect(stage2_migrated_postgres_database.admin_dsn) as connection:
        if damage == "size":
            with pytest.raises(psycopg.errors.CheckViolation):
                with connection.transaction():
                    connection.execute(
                        "UPDATE pulsara_v3.blobs SET logical_size=logical_size+1 "
                        "WHERE id=(SELECT blob_id FROM pulsara_v3.content_parts WHERE entry_id=%s AND ordinal=1)",
                        (entry_id,),
                    )
        elif damage == "digest":
            connection.execute(
                "UPDATE pulsara_v3.blobs SET logical_digest=%s "
                "WHERE id=(SELECT blob_id FROM pulsara_v3.content_parts WHERE entry_id=%s AND ordinal=1)",
                ("sha256:" + "0" * 64, entry_id),
            )
        elif damage == "utf8":
            # Administrative storage corruption bypasses the application writer,
            # while retaining the relational row and immutable-update guard.
            row = connection.execute(
                "DELETE FROM pulsara_v3.content_parts WHERE entry_id=%s RETURNING id,session_id,workspace_id,entry_id,ordinal",
                (entry_id,),
            ).fetchone()
            connection.execute(
                "INSERT INTO pulsara_v3.content_parts (id,session_id,workspace_id,entry_id,ordinal,type,inline_value) "
                "VALUES (%s,%s,%s,%s,%s,'text',%s)",
                (*row, b"\xff"),
            )
    reader = CanonicalProtocolReader(repo.connection_provider)
    with reader._connection(monotonic() + 30) as connection:
        candidate = blocks(connection, lease.guard.session_id, entry_id)
        if damage in {"empty", "whitespace"}:
            assert (
                read_fork_anchor(connection, lease.guard.session_id, entry_id) is None
            )
            assert (
                read_fork_anchor(
                    connection,
                    lease.guard.session_id,
                    entry_id,
                    candidate_blocks=candidate,
                )
                is None
            )
        elif damage == "size":
            assert (
                read_fork_anchor(connection, lease.guard.session_id, entry_id)
                is not None
            )
            assert (
                read_fork_anchor(
                    connection,
                    lease.guard.session_id,
                    entry_id,
                    candidate_blocks=candidate,
                )
                is not None
            )
        else:
            errors = []
            for kwargs in ({}, {"candidate_blocks": candidate}):
                with pytest.raises((RuntimeError, ValueError)) as error:
                    read_fork_anchor(
                        connection, lease.guard.session_id, entry_id, **kwargs
                    )
                errors.append((type(error.value), str(error.value)))
            assert errors[0] == errors[1]


def test_empty_candidate_block_set_needs_no_second_fork_query(
    repo, stage2_migrated_postgres_database
):
    lease = new_session(repo)
    _, _, entry_id = turn(repo, lease.guard, "question")
    with psycopg.connect(stage2_migrated_postgres_database.admin_dsn) as connection:
        connection.execute(
            "DELETE FROM pulsara_v3.content_parts WHERE entry_id=%s", (entry_id,)
        )
    reader = CanonicalProtocolReader(repo.connection_provider)
    with reader._connection(monotonic() + 30) as connection:
        assert read_fork_anchor(connection, lease.guard.session_id, entry_id) is None
        measured = RecordingConnection(connection)
        assert (
            read_fork_anchor(
                measured, lease.guard.session_id, entry_id, candidate_blocks=()
            )
            is None
        )
        assert len(measured.queries) == 1


@pytest.mark.parametrize("replay", [False, True])
def test_root_replay_presence_saves_only_an_empty_read_and_keeps_exact_wire(
    repo, replay
):
    lease = new_session(repo)
    _, entry_id = final_with_parts(repo, lease, replay=replay)
    reader = CanonicalProtocolReader(repo.connection_provider)
    with reader._connection(monotonic() + 30) as connection:
        row = connection.execute(
            "SELECT * FROM pulsara_v3.transcript_entries WHERE id=%s", (entry_id,)
        ).fetchone()
        expected = reader._entry(connection, row)
        measured = RecordingConnection(connection)
        related = reader._entry_relations(measured, (row,))
        assert related[entry_id]["has_replay"] is replay
        assert "payload_bytes" not in measured.queries[0]
        measured.queries.clear()
        actual = reader._entry(measured, row, relations=related)
        assert actual.SerializeToString(
            deterministic=True
        ) == expected.SerializeToString(deterministic=True)
        assert len(measured.queries) == (
            3 if replay else 2
        )  # blocks, fork metadata, optional replay
        assert len(actual.reasoning_blocks) == int(replay)


@pytest.mark.parametrize("damage", ["replay", "model"])
def test_root_future_corruption_cannot_preempt_candidate_byte_admission(
    repo, stage2_migrated_postgres_database, damage
):
    lease = new_session(repo)
    old_turn, old_entry = final_with_parts(repo, lease, replay=damage == "replay")
    turn(repo, lease.guard, "newest", answer="newest answer " * 200)
    reader = CanonicalProtocolReader(repo.connection_provider)
    common = dict(session_id=lease.guard.session_id, maximum_control_items=128)
    tail = reader.snapshot(
        **common, maximum_entries=1, deadline_monotonic=monotonic() + 30
    )
    with psycopg.connect(stage2_migrated_postgres_database.admin_dsn) as connection:
        if damage == "replay":
            connection.execute(
                "UPDATE pulsara_v3.provider_assistant_replay_fragments SET payload_digest=%s WHERE assistant_entry_id=%s",
                ("sha256:" + "0" * 64, old_entry),
            )
        else:
            connection.execute(
                "UPDATE pulsara_v3.turns SET model_call_binding=jsonb_set(model_call_binding,'{reasoning}','{}'::jsonb) WHERE id=%s",
                (old_turn,),
            )
    bounded = reader.snapshot(
        **common,
        maximum_entries=256,
        maximum_serialized_bytes=tail.ByteSize(),
        deadline_monotonic=monotonic() + 30,
    )
    assert bounded.SerializeToString(deterministic=True) == tail.SerializeToString(
        deterministic=True
    )
    with reader._connection(monotonic() + 30) as connection:
        row = connection.execute(
            "SELECT * FROM pulsara_v3.transcript_entries WHERE id=%s", (old_entry,)
        ).fetchone()
        related = reader._entry_relations(connection, (row,))
        errors = []
        for relations in (None, related):
            with pytest.raises((RuntimeError, ValueError)) as error:
                reader._entry(connection, row, relations=relations)
            errors.append((type(error.value), str(error.value)))
        assert errors[0] == errors[1]
    with pytest.raises(
        errors[0][0],
        match="replay body is corrupt"
        if damage == "replay"
        else "reasoning selection must be a closed object or null",
    ):
        reader.snapshot(
            **common, maximum_entries=256, deadline_monotonic=monotonic() + 30
        )
