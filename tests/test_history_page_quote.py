"""Incremental protobuf quotes preserve the full frame and rejection rollback."""
from time import monotonic

import pytest

from pulsara_agent.terminal_protocol.canonical_v3 import CanonicalProtocolReader
from pulsara_agent.terminal_protocol.generated_v3 import terminal_kernel_v3_pb2 as wire
from tests.test_conversation_fork import new_session, repo as repo, rows, turn
from tests.test_turn_interruption_history import stop


pytestmark = pytest.mark.postgres


def test_rejected_notice_and_cursor_do_not_mutate_the_last_accepted_page(repo):
    lease = new_session(repo)
    interrupted, _, _ = turn(repo, lease.guard, "old", complete=False)
    stop(repo, lease.guard, interrupted)
    turn(repo, lease.guard, "new", answer="large answer " * 200)
    head = rows(repo, "SELECT latest_entry_sequence, latest_event_sequence FROM pulsara_v3.sessions WHERE id=%s",
                (lease.guard.session_id,))[0]
    reader = CanonicalProtocolReader(repo.connection_provider)

    def read(maximum_entries, **kwargs):
        return reader.history_page(
            session_id=lease.guard.session_id, cut_sequence=int(head["latest_entry_sequence"]),
            event_sequence_cut=int(head["latest_event_sequence"]),
            before_entry_sequence=int(head["latest_entry_sequence"]) + 1,
            maximum_entries=maximum_entries, request_id="回声" * 150,
            deadline_monotonic=monotonic() + 30, **kwargs,
        )

    full = read(4)
    assert len(full.interruption_notices) == 1
    assert not full.has_more and not full.HasField("older_history_cursor")
    tail = read(2)
    assert not tail.interruption_notices and tail.has_more
    assert tail.HasField("older_history_cursor")
    size = wire.ServerFrame(history_page=tail).ByteSize()
    bounded = read(4, maximum_serialized_bytes=size)
    assert bounded.SerializeToString(deterministic=True) == tail.SerializeToString(deterministic=True)
    smaller = read(4, maximum_serialized_bytes=size - 1)
    assert len(smaller.entries) == 1 and not smaller.interruption_notices
    assert smaller.has_more and smaller.older_history_cursor.entry_sequence == 4
