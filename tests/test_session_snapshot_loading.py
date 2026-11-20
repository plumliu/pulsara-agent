"""Session opening preserves exact bounded snapshots while avoiding repeat reads."""

from datetime import datetime, timezone
from hashlib import sha256
from time import monotonic

import pytest

from pulsara_agent.conversation_kernel.content_parts import ResultPart
from pulsara_agent.conversation_kernel.repository import (
    AssistantToolCallBlock,
    build_prepared_tool_result_acceptance,
)
from pulsara_agent.identity import random_id
from pulsara_agent.primitives.context import freeze_json
from pulsara_agent.primitives.tool_observation import ToolObservationOrigin
from pulsara_agent.terminal_protocol.canonical_v3 import CanonicalProtocolReader
from pulsara_agent.terminal_protocol.generated_v3 import terminal_kernel_v3_pb2 as wire
from tests.test_conversation_fork import final, new_session, repo as repo, turn
from tests.test_history_projection_cut import append, question
from tests.test_stage2_canonical_reader import _permission_fingerprint, _start_turn
from tests.test_turn_interruption_history import stop


pytestmark = pytest.mark.postgres


def _tool_attempt(repository, lease, turn_id):
    cut = repository.prepare_provider_input_cut(
        lease.guard, turn_id=turn_id, deadline_monotonic=monotonic() + 30,
    )
    entry_id, call_id, attempt_id = (
        random_id("entry"), random_id("call"), random_id("attempt")
    )
    repository.commit_assistant_message(
        lease.guard, cut=cut, entry_id=entry_id,

        blocks=(AssistantToolCallBlock(
            block_id=random_id("block"), tool_call_id=call_id,
            tool_name="exec_process", arguments=freeze_json({"command": "true"}),
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
    return entry_id, call_id, attempt_id


def _interrupted_attempt(repository, lease):
    turn_id = _start_turn(repository, lease, b"tool request")
    _, _, attempt_id = _tool_attempt(repository, lease, turn_id)
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


def test_interactive_snapshot_only_widens_for_the_running_turn_and_latest_reply(repo):
    lease = new_session(repo)
    for index in range(13):
        turn(repo, lease.guard, f"history {index}")
    _, _, previous_reply = turn(repo, lease.guard, "previous prompt")
    reader = CanonicalProtocolReader(repo.connection_provider)
    session_id = lease.guard.session_id
    idle = _snapshot(reader, session_id, preferred_history_entries=24)
    assert idle == _snapshot(reader, session_id, maximum_entries=24)

    active, initial = question(repo, lease.guard, "new prompt")
    short = _snapshot(reader, session_id, preferred_history_entries=24)
    assert short == _snapshot(reader, session_id, maximum_entries=24)
    for index in range(26):
        append(repo, lease.guard, active, "progress " * 100 + str(index))
    value = _snapshot(reader, session_id, preferred_history_entries=24)
    assert value == _snapshot(reader, session_id, maximum_entries=28)
    assert value.entries[0].entry_id == previous_reply
    assert value.entries[1].entry_id == initial
    assert [entry.turn_id for entry in value.entries[1:]] == [active] * 27
    assert [observed.turn_id for observed in value.control.active_turns] == [active]
    _assert_exact_fingerprint(value)

    # A turn extending beyond the entry budget keeps a recent suffix.
    assert _snapshot(reader, session_id, maximum_entries=25, preferred_history_entries=24) == (
        _snapshot(reader, session_id, maximum_entries=24)
    )
    assert _snapshot(reader, session_id, preferred_history_entries=24, maximum_serialized_bytes=4096) == (
        _snapshot(reader, session_id, maximum_serialized_bytes=4096)
    )

    # The stored cursor remains at the initial cut even as the live run
    # commits, and the older page abuts the first readable reply.
    final(repo, lease.guard, active)
    cursor = value.older_history_cursor
    page = reader.history_page(
        session_id=session_id, cut_sequence=cursor.cut_sequence,
        event_sequence_cut=cursor.event_sequence_cut,
        before_entry_sequence=cursor.entry_sequence, maximum_entries=128,
        deadline_monotonic=monotonic() + 30,
    )
    assert cursor.cut_sequence == value.entry_sequence_cut
    assert cursor.event_sequence_cut == value.event_sequence_cut
    assert page.entries[-1].entry_sequence + 1 == value.entries[0].entry_sequence


def test_oversized_turn_keeps_latest_text_and_names_unsettled_tools(repo, monkeypatch):
    lease = new_session(repo)
    active, _ = question(repo, lease.guard, "running")
    append(repo, lease.guard, active, "earlier process")
    source, call, older_attempt = _tool_attempt(repo, lease, active)
    result_entry_id = random_id("entry")
    result = build_prepared_tool_result_acceptance(
        guard=lease.guard,
        workspace_id=repo.read_session_workspace_id(lease.guard, deadline_monotonic=monotonic() + 30),
        result_id=random_id("result"), result_entry_id=result_entry_id,
        turn_id=active, assistant_entry_id=source, tool_call_id=call, attempt_id=older_attempt,
        result_state="SUCCESS", public_fields={}, content_parts=(ResultPart("text", "done"),),
        observed_at=datetime.now(timezone.utc), observation_duration_microseconds=None,
        observation_origin_kind=ToolObservationOrigin.TERMINAL_PROCESS,
        trusted_tool_reported_duration_microseconds=None, actor_id="terminal",
    )
    repo.accept_tool_result(lease.guard, candidate=result, deadline_monotonic=monotonic() + 30)
    latest_text, _ = append(repo, lease.guard, active, "latest text")
    _, _, attempt_id = _tool_attempt(repo, lease, active)
    reader = CanonicalProtocolReader(repo.connection_provider)
    session_id = lease.guard.session_id
    floors = []
    set_attempts = reader._set_tool_attempt_control

    def record_attempt_read(*args, **kwargs):
        floors.append(kwargs["entry_sequence_floor"])
        return set_attempts(*args, **kwargs)

    monkeypatch.setattr(reader, "_set_tool_attempt_control", record_attempt_read)

    value = _snapshot(reader, session_id, maximum_entries=5, preferred_history_entries=1)
    assert floors == [value.entry_sequence_cut + 1]
    assert value == _snapshot(reader, session_id, maximum_entries=2)
    # The preferred page begins at a result whose source is earlier. Both
    # stay behind the same cursor when reading the recent process.
    assert _snapshot(reader, session_id, maximum_entries=5, preferred_history_entries=3) == value
    assert value.entries[0].entry_id == latest_text
    assert value.entries[1].entry_kind == wire.ASSISTANT_TOOL_REQUEST
    [attempt] = value.control.tool_attempts
    assert (attempt.attempt_id, attempt.turn_id, attempt.tool_name) == (attempt_id, active, "exec_process")
    cursor = value.older_history_cursor
    assert cursor.entry_sequence == value.entries[0].entry_sequence
    page = reader.history_page(
        session_id=session_id, cut_sequence=cursor.cut_sequence,
        event_sequence_cut=cursor.event_sequence_cut, before_entry_sequence=cursor.entry_sequence,
        maximum_entries=128, deadline_monotonic=monotonic() + 30,
    )
    assert page.entries[-2].entry_id == source
    assert page.entries[-1].entry_id == result_entry_id
    assert page.entries[-1].tool_result.assistant_entry_id == source
    _assert_exact_fingerprint(value)


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
