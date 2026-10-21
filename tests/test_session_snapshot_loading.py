"""Session opening preserves exact bounded snapshots while avoiding repeat reads."""

from datetime import datetime, timezone
from hashlib import sha256
from time import monotonic

import pytest

from pulsara_agent.conversation_kernel.contracts import InlineContent
from pulsara_agent.conversation_kernel.repository import AssistantToolCallBlock
from pulsara_agent.identity import random_id
from pulsara_agent.primitives.context import freeze_json
from pulsara_agent.terminal_protocol.canonical_v3 import CanonicalProtocolReader
from pulsara_agent.terminal_protocol.generated_v3 import terminal_kernel_v3_pb2 as wire
from tests.test_conversation_fork import final, new_session, repo as repo, turn
from tests.test_stage2_canonical_reader import _permission_fingerprint, _start_turn
from tests.test_turn_interruption_history import stop


pytestmark = pytest.mark.postgres


def _interrupted_attempt(repository, lease):
    turn_id = _start_turn(repository, lease, b"tool request")
    cut = repository.prepare_provider_input_cut(
        lease.guard, turn_id=turn_id, deadline_monotonic=monotonic() + 30,
    )
    entry_id, call_id, attempt_id = (
        random_id("entry"), random_id("call"), random_id("attempt")
    )
    repository.commit_assistant_message(
        lease.guard, cut=cut, entry_id=entry_id,
        parent_content=InlineContent.from_bytes(b"tool manifest"),
        blocks=(AssistantToolCallBlock(
            block_id=random_id("block"), tool_call_id=call_id,
            tool_name="terminal", arguments=freeze_json({"command": "true"}),
        ),),
        occurred_at=datetime.now(timezone.utc), actor_id="model:test",
        deadline_monotonic=monotonic() + 30,
    )
    repository.accept_tool_attempt(
        lease.guard, attempt_id=attempt_id, assistant_entry_id=entry_id,
        tool_call_id=call_id, authorization_kind="policy",
        authorization_reference="allow", actor_kind="runtime", actor_id="executor",
        remote_idempotency_key=None, retry_of_attempt_id=None,
        permission_snapshot_fingerprint=_permission_fingerprint(repository, lease, turn_id),
        occurred_at=datetime.now(timezone.utc), deadline_monotonic=monotonic() + 30,
    )
    stop(repository, lease.guard, turn_id)
    return attempt_id


def _snapshot(reader, session_id, **kwargs):
    return reader.snapshot(
        session_id=session_id, maximum_entries=kwargs.pop("maximum_entries", 256),
        maximum_control_items=kwargs.pop("maximum_control_items", 128),
        deadline_monotonic=monotonic() + 30, **kwargs,
    )


def _assert_exact_fingerprint(snapshot):
    unsigned = wire.CanonicalSessionSnapshot()
    unsigned.CopyFrom(snapshot)
    unsigned.ClearField("snapshot_fingerprint")
    expected = "sha256:" + sha256(
        b"terminal-canonical-snapshot:v3\0"
        + unsigned.SerializeToString(deterministic=True)
    ).hexdigest()
    assert snapshot.snapshot_fingerprint == expected


def test_opening_byte_boundary_preserves_attempt_window_and_exact_fingerprint(repo):
    lease = new_session(repo)
    attempt_id = _interrupted_attempt(repo, lease)
    turn(repo, lease.guard, "next", answer="answer " * 200)
    reader = CanonicalProtocolReader(repo.connection_provider)
    session_id = lease.guard.session_id
    full = _snapshot(reader, session_id)
    assert full.control.tool_attempt_total_count == 1
    assert [a.attempt_id for a in full.control.tool_attempts] == [attempt_id]
    tail = _snapshot(reader, session_id, maximum_entries=2)
    assert tail.control.tool_attempt_total_count == 0
    assert not tail.control.tool_attempts
    assert tail.ByteSize() >= 1024
    bounded = _snapshot(reader, session_id, maximum_serialized_bytes=tail.ByteSize())
    # The next candidate includes the old attempt and notice. Its rejection
    # must preserve both the exact accepted tail and its original control.
    assert bounded.SerializeToString(deterministic=True) == tail.SerializeToString(deterministic=True)
    smaller = _snapshot(reader, session_id, maximum_serialized_bytes=tail.ByteSize() - 1)
    assert len(smaller.entries) == 1
    assert smaller.control.tool_attempt_total_count == 0
    assert not smaller.control.tool_attempts
    for value in (full, tail, bounded, smaller):
        _assert_exact_fingerprint(value)


def test_opening_attempt_total_is_complete_when_control_display_is_bounded(repo):
    lease = new_session(repo)
    first = _interrupted_attempt(repo, lease)
    _interrupted_attempt(repo, lease)
    turn(repo, lease.guard, "done")
    reader = CanonicalProtocolReader(repo.connection_provider)
    snapshot = _snapshot(reader, lease.guard.session_id, maximum_control_items=1)
    assert snapshot.control.tool_attempt_total_count == 2
    assert [a.attempt_id for a in snapshot.control.tool_attempts] == [first]
    _assert_exact_fingerprint(snapshot)


def test_opening_reads_session_wide_control_only_once(repo, monkeypatch):
    lease = new_session(repo)
    for index in range(4):
        turn(repo, lease.guard, str(index))
    reader = CanonicalProtocolReader(repo.connection_provider)
    original = reader._control
    reads = 0

    def counted(*args, **kwargs):
        nonlocal reads
        reads += 1
        return original(*args, **kwargs)

    monkeypatch.setattr(reader, "_control", counted)
    snapshot = _snapshot(reader, lease.guard.session_id)
    assert len(snapshot.entries) == 8
    assert reads == 1  # One RR transaction fixes all session-wide fields.
    _assert_exact_fingerprint(snapshot)


def test_opening_quotes_growing_transcript_without_rebuilding_it(repo, monkeypatch):
    lease = new_session(repo)
    for index in range(4):
        turn(repo, lease.guard, str(index))
    reader = CanonicalProtocolReader(repo.connection_provider)
    original = reader._snapshot_value
    assemblies = 0

    def counted(*args, **kwargs):
        nonlocal assemblies
        assemblies += 1
        return original(*args, **kwargs)

    monkeypatch.setattr(reader, "_snapshot_value", counted)
    value = _snapshot(reader, lease.guard.session_id)
    assert [entry.entry_sequence for entry in value.entries] == list(range(1, 9))
    assert not value.HasField("older_history_cursor")
    assert assemblies == 1
    _assert_exact_fingerprint(value)


def test_history_context_batched_neighbours_obey_the_selected_cut(repo):
    lease = new_session(repo)
    turn(repo, lease.guard, "previous")
    active, _, partial = turn(repo, lease.guard, "working", complete=False)
    reader = CanonicalProtocolReader(repo.connection_provider)
    before = _snapshot(reader, lease.guard.session_id)
    initial = next(entry.entry_id for entry in before.entries if entry.turn_id == active)
    answer = final(repo, lease.guard, active)

    def context(**cuts):
        return reader.browser_history_context(
            session_id=lease.guard.session_id, memory_domain_id="u_local",
            entry_ids=[partial], deadline_monotonic=monotonic() + 30, **cuts,
        )["entry_contexts"][0]

    frozen = context(cut_sequence=before.entry_sequence_cut, event_sequence_cut=before.event_sequence_cut)
    assert frozen["previous_entry_id"] == initial
    assert "next_entry_id" not in frozen
    current = context()
    assert current["previous_entry_id"] == initial
    assert current["next_entry_id"] == answer


def test_history_context_rejects_unowned_entries_before_relation_batch(repo, monkeypatch):
    first, other = new_session(repo), new_session(repo)
    _, _, foreign = turn(repo, other.guard, "other session")
    reader = CanonicalProtocolReader(repo.connection_provider)

    def unexpected(*_args, **_kwargs):
        raise AssertionError("entry relations read before exact-set authorization")

    monkeypatch.setattr(reader, "_browser_context_entry_rows", unexpected)
    with pytest.raises(KeyError, match="does not belong"):
        reader.browser_history_context(
            session_id=first.guard.session_id, memory_domain_id="u_local",
            entry_ids=[foreign], deadline_monotonic=monotonic() + 30,
        )
