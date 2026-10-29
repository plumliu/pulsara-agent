"""Assistant SQL batching preserves candidate-local adaptation and resources."""

from datetime import datetime, timezone
from random import Random
from time import monotonic
import json

import psycopg
import pytest
from psycopg.types.json import JsonbLoader

from pulsara_agent.conversation_kernel.contracts import InlineContent
from pulsara_agent.conversation_kernel.repository import AssistantToolCallBlock
from pulsara_agent.identity import random_id
from pulsara_agent.primitives.context import freeze_json
from pulsara_agent.terminal_protocol.canonical_v3 import CanonicalProtocolReader
from pulsara_agent.llm.provider_replay import MAXIMUM_PROVIDER_REPLAY_PAYLOAD_BYTES
from pulsara_agent.llm.provider_replay import build_prepared_durable_provider_assistant_replay
from pulsara_agent.llm.input import LLMToolCall
from pulsara_agent.llm.request import provider_assistant_public_projection_fingerprint
from pulsara_agent.primitives.context import canonical_json_bytes
from tests.support.model_config import build_test_provider_replay_target
from tests.test_conversation_fork import final, new_session, repo as repo
from tests.test_stage2_canonical_reader import _start_turn


pytestmark = pytest.mark.postgres


class ObservedConnection:
    def __init__(self, connection):
        self.connection = connection
        self.queries = []
        self.binary_row_bytes = []

    def execute(self, query, *args, **kwargs):
        self.queries.append(" ".join(str(query).split()))
        cursor = self.connection.execute(query, *args, **kwargs)
        if kwargs.get("binary"):
            result = cursor.pgresult
            assert result.nfields == 20
            self.binary_row_bytes.extend(
                1 + 4 + 2 + 4 * result.nfields + sum(
                    len(value) for column in range(result.nfields)
                    if (value := result.get_value(row, column)) is not None
                )
                for row in range(result.ntuples)
            )
        return cursor

    def __getattr__(self, name):
        return getattr(self.connection, name)


def _requests(repository, lease, commands, *, replay=False):
    turn_id = _start_turn(repository, lease, b"body window")
    entries = []
    for command in commands:
        cut = repository.prepare_provider_input_cut(
            lease.guard, turn_id=turn_id, deadline_monotonic=monotonic() + 30,
        )
        entry_id = random_id("entry")
        call_id = random_id("call")
        replay_body = None
        if replay:
            arguments = canonical_json_bytes({"command": command}).decode()
            replay_body = build_prepared_durable_provider_assistant_replay(
                session_id=lease.guard.session_id,
                workspace_id=repository.read_session_workspace_id(
                    lease.guard, deadline_monotonic=monotonic()+30,
                ), assistant_entry_id=entry_id,
                target=build_test_provider_replay_target(model_id="model", transport_binding_id="test-wire"),
                public_projection_fingerprint=provider_assistant_public_projection_fingerprint(
                    text=None, tool_calls=(LLMToolCall(call_id, "exec_process", arguments),),
                ),
                ordered_items=(freeze_json({
                    "role": "assistant", "content": None, "reasoning_content": "candidate reasoning",
                    "tool_calls": [{"id": call_id, "type": "function",
                                    "function": {"name": "exec_process", "arguments": arguments}}],
                }),),
            )
        repository.commit_assistant_message(
            lease.guard, cut=cut, entry_id=entry_id,
            parent_content=InlineContent.from_bytes(b"tool manifest"),
            blocks=(AssistantToolCallBlock(
                block_id=random_id("block"), tool_call_id=call_id,
                tool_name="exec_process", arguments=freeze_json({"command": command}),
            ),),
            provider_replay=replay_body,
            occurred_at=datetime.now(timezone.utc), actor_id="model:test",
            deadline_monotonic=monotonic() + 30,
        )
        entries.append(entry_id)
    return entries


def _rows(connection, session_id):
    return connection.execute(
        "SELECT * FROM pulsara_v3.transcript_entries WHERE session_id=%s ORDER BY entry_sequence",
        (session_id,),
    ).fetchall()


def test_small_assistant_bodies_share_one_query_and_keep_exact_wire(repo):
    lease = new_session(repo)
    _requests(repo, lease, ["echo 中文 " + str(index) for index in range(8)], replay=True)
    reader = CanonicalProtocolReader(repo.connection_provider)
    with reader._connection(monotonic() + 30) as connection:
        rows = _rows(connection, lease.guard.session_id)
        expected = tuple(reader._entry(connection, row) for row in rows)
        observed = ObservedConnection(connection)
        actual = reader._entries(observed, rows)
        assert [entry.SerializeToString(deterministic=True) for entry in actual] == [
            entry.SerializeToString(deterministic=True) for entry in expected
        ]
        assert len(observed.queries) == 4  # facts, physical quote, flat classification, raw body
        assert len(observed.binary_row_bytes) == 16


def test_body_window_quote_includes_actual_binary_protocol_row_framing(repo):
    lease = new_session(repo)
    entry_ids = _requests(repo, lease, ["quote \\\" 中文", "other"], replay=True)
    reader = CanonicalProtocolReader(repo.connection_provider)
    with reader._connection(monotonic() + 30) as connection:
        observed = ObservedConnection(connection)
        quotes = reader._assistant_part_sizes(observed, lease.guard.session_id, entry_ids)
        reader._assistant_parts_batch(observed, lease.guard.session_id, entry_ids)
        # libpq exposes exactly the binary cells used on the transport; this
        # assertion includes their framing and catches missing NULL-cell costs.
        assert sum(quotes.values()) >= sum(observed.binary_row_bytes)


def test_future_prefetched_replay_validation_remains_at_its_candidate(
    repo, stage2_migrated_postgres_database,
):
    lease = new_session(repo)
    ids = _requests(repo, lease, ["old", "middle " * 1400, "new " * 800], replay=True)
    reader = CanonicalProtocolReader(repo.connection_provider)
    common = dict(session_id=lease.guard.session_id, maximum_control_items=128)
    tail = reader.snapshot(**common, maximum_entries=1, deadline_monotonic=monotonic()+30)
    with psycopg.connect(stage2_migrated_postgres_database.admin_dsn) as connection:
        connection.execute(
            "UPDATE pulsara_v3.provider_assistant_replay_fragments SET payload_digest=%s "
            "WHERE session_id=%s AND assistant_entry_id=%s",
            ("sha256:" + "0" * 64, lease.guard.session_id, ids[0]),
        )
    bounded = reader.snapshot(**common, maximum_entries=256, maximum_serialized_bytes=tail.ByteSize(),
                              deadline_monotonic=monotonic()+30)
    assert bounded.SerializeToString(deterministic=True) == tail.SerializeToString(deterministic=True)
    with pytest.raises(RuntimeError, match="replay body is corrupt"):
        reader.snapshot(**common, maximum_entries=256, deadline_monotonic=monotonic()+30)


def test_connection_json_loader_runs_only_at_the_consumed_candidate(repo):
    lease = new_session(repo)
    entry_ids = _requests(repo, lease, ["first", "deferred failure", "last"])
    reader = CanonicalProtocolReader(repo.connection_provider)
    adapted = []

    class RejectDeferred(JsonbLoader):
        def load(self, data):
            raw = bytes(data)
            adapted.append(raw)
            if b"deferred failure" in raw:
                raise ValueError("connection-specific deferred adaptation")
            return super().load(data)

    with reader._connection(monotonic() + 30) as connection:
        rows = _rows(connection, lease.guard.session_id)
        connection.adapters.register_loader("jsonb", RejectDeferred)
        relations = reader._entry_relations(connection, rows)
        candidates = reader._entry_candidates(connection, rows, relations)
        assert next(candidates)[0]["entry_kind"] == "USER_MESSAGE"
        assert next(candidates)[1].entry_id == entry_ids[0]
        assert not any(b"deferred failure" in item for item in adapted)
        with pytest.raises(ValueError, match="connection-specific deferred adaptation"):
            next(candidates)


@pytest.mark.parametrize("custom_loader", [False, True])
def test_non_utf8_connections_keep_original_json_loader_bytes(repo, custom_loader):
    lease = new_session(repo)
    _requests(repo, lease, ["café one", "café two"])
    reader = CanonicalProtocolReader(repo.connection_provider)
    seen = []

    class Latin1Loader(JsonbLoader):
        def load(self, data):
            raw = bytes(data)
            seen.append(raw)
            return json.loads(raw.decode("latin-1"))

    with reader._connection(monotonic()+30) as connection:
        rows = _rows(connection, lease.guard.session_id)
        connection.execute("SET LOCAL client_encoding TO 'LATIN1'")
        assert connection.info.encoding != "utf-8"
        if custom_loader:
            connection.adapters.register_loader("jsonb", Latin1Loader)
            expected = tuple(reader._entry(connection, row) for row in rows)
            original_bytes = tuple(seen)
            seen.clear()
            observed = ObservedConnection(connection)
            actual = reader._entries(observed, rows)
            assert [entry.SerializeToString(deterministic=True) for entry in actual] == [
                entry.SerializeToString(deterministic=True) for entry in expected
            ]
            assert tuple(seen) == original_bytes
            assert b"\xe9" in b"".join(seen)
            assert not observed.binary_row_bytes
        else:
            with pytest.raises(UnicodeDecodeError) as expected:
                tuple(reader._entry(connection, row) for row in rows)
            with pytest.raises(UnicodeDecodeError) as actual:
                reader._entries(connection, rows)
            assert actual.value.args == expected.value.args


def test_carrier_sized_window_splits_without_rejecting_additional_entries(repo, monkeypatch):
    lease = new_session(repo)
    random = Random(491)
    _requests(repo, lease, [random.randbytes(200_000).hex() for _ in range(9)])
    reader = CanonicalProtocolReader(repo.connection_provider)
    native = reader._assistant_parts_batch
    windows = []

    def observe(connection, session_id, entry_ids):
        quote = reader._assistant_part_sizes(connection, session_id, entry_ids)
        assert sum(quote.values()) <= MAXIMUM_PROVIDER_REPLAY_PAYLOAD_BYTES
        windows.append(tuple(entry_ids))
        return native(connection, session_id, entry_ids)

    monkeypatch.setattr(reader, "_assistant_parts_batch", observe)
    with reader._connection(monotonic() + 30) as connection:
        rows = _rows(connection, lease.guard.session_id)
        expected = tuple(reader._entry(connection, row) for row in rows)
        actual = reader._entries(connection, rows)
        assert len(actual) == 10
        assert [entry.SerializeToString(deterministic=True) for entry in actual] == [
            entry.SerializeToString(deterministic=True) for entry in expected
        ]
        assert len(windows) >= 2
        assert sum(map(len, windows)) == 9


def test_entry_larger_than_raw_window_keeps_original_candidate_read(repo, monkeypatch):
    lease = new_session(repo)
    large = Random(13).randbytes(1_500_000).hex()
    ids = _requests(repo, lease, ["before one", "before two", large, "after one", "after two"])
    reader = CanonicalProtocolReader(repo.connection_provider)
    native = reader._assistant_parts
    ordinary_reads = []

    def ordinary(connection, session_id, entry_id):
        ordinary_reads.append(entry_id)
        return native(connection, session_id, entry_id)

    with reader._connection(monotonic() + 30) as connection:
        rows = _rows(connection, lease.guard.session_id)
        expected = tuple(reader._entry(connection, row) for row in rows)
        quote = reader._assistant_part_sizes(connection, lease.guard.session_id, ids)
        assert quote[ids[2]] > MAXIMUM_PROVIDER_REPLAY_PAYLOAD_BYTES
        monkeypatch.setattr(reader, "_assistant_parts", ordinary)
        actual = reader._entries(connection, rows)
        assert ordinary_reads == [ids[2]]
        assert [entry.SerializeToString(deterministic=True) for entry in actual] == [
            entry.SerializeToString(deterministic=True) for entry in expected
        ]


def test_root_fork_boundary_is_never_in_a_raw_body_batch(repo, monkeypatch):
    lease = new_session(repo)
    first = _requests(repo, lease, ["before one", "before two"])
    reader = CanonicalProtocolReader(repo.connection_provider)
    with reader._connection(monotonic()+30) as connection:
        turn_id = next(row["turn_id"] for row in _rows(connection, lease.guard.session_id)
                       if row["id"] == first[0])
    anchor = final(repo, lease.guard, turn_id)
    after = _requests(repo, lease, ["after one", "after two"])
    batches = []
    native = reader._assistant_parts_batch

    def observed(connection, session_id, entry_ids):
        batches.append(tuple(entry_ids))
        return native(connection, session_id, entry_ids)

    monkeypatch.setattr(reader, "_assistant_parts_batch", observed)
    with reader._connection(monotonic()+30) as connection:
        actual = reader._entries(connection, _rows(connection, lease.guard.session_id))
        assert next(entry for entry in actual if entry.entry_id == anchor).fork_eligible
    assert batches == [tuple(first), tuple(after)]


@pytest.mark.parametrize("body", ["huge_integer", "huge_scale", "compressed", "deep"])
def test_future_nonbatchable_body_cannot_preempt_byte_admission(
    repo, stage2_migrated_postgres_database, body,
):
    lease = new_session(repo)
    ids = _requests(repo, lease, ["old", "middle " * 1400, "new " * 800])
    reader = CanonicalProtocolReader(repo.connection_provider)
    common = dict(session_id=lease.guard.session_id, maximum_control_items=128)
    tail = reader.snapshot(**common, maximum_entries=1, deadline_monotonic=monotonic()+30)
    budget = tail.ByteSize()
    with psycopg.connect(stage2_migrated_postgres_database.admin_dsn) as connection:
        if body == "huge_integer":
            payload = '{"value":1e100000}'
        elif body == "huge_scale":
            payload = '{"value":1.' + '0' * 16000 + '}'
        elif body == "compressed":
            payload = '{"value":"' + 'x' * 3_000_000 + '"}'
        else:
            # Preserve a genuine nested JSONB datum without TOAST compression
            # so the flat classifier, rather than compression, excludes it.
            connection.execute("ALTER TABLE pulsara_v3.assistant_message_blocks ALTER COLUMN tool_arguments SET STORAGE EXTERNAL")
            payload = '{"nested":' * 1800 + '0' + '}' * 1800
        connection.execute(
            "UPDATE pulsara_v3.assistant_message_blocks SET tool_arguments=%s::jsonb "
            "WHERE session_id=%s AND assistant_entry_id=%s", (payload, lease.guard.session_id, ids[0]),
        )
    bounded = reader.snapshot(**common, maximum_entries=256, maximum_serialized_bytes=budget,
                              deadline_monotonic=monotonic()+30)
    assert bounded.SerializeToString(deterministic=True) == tail.SerializeToString(deterministic=True)
    with reader._connection(monotonic()+30) as connection:
        rows = _rows(connection, lease.guard.session_id)
        old = next(row for row in rows if row["id"] == ids[0])
        try:
            expected = reader._entry(connection, old)
        except (ValueError, RecursionError) as error:
            failure = type(error), str(error)
        else:
            failure = None
    if failure is None:
        full = reader.snapshot(**common, maximum_entries=256, deadline_monotonic=monotonic()+30)
        assert next(entry for entry in full.entries if entry.entry_id == ids[0]) == expected
    else:
        with pytest.raises(failure[0]) as caught:
            reader.snapshot(**common, maximum_entries=256, deadline_monotonic=monotonic()+30)
        assert str(caught.value) == failure[1]
