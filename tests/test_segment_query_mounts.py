"""A page mounts each selected canonical segment once, without eager history mounts."""

from time import monotonic

import pytest

from pulsara_agent.terminal_protocol.canonical_v3 import CanonicalProtocolReader
from tests.test_browser_compaction_intervals import _append, _context
from tests.test_conversation_fork import compact, final, new_session, repo as repo, rows, turn


pytestmark = pytest.mark.postgres


@pytest.mark.parametrize("page", ["shared", "latest"])
def test_selected_segment_mount_is_shared_and_not_eager_for_older_adoptions(repo, page):
    lease = new_session(repo)
    session_id = lease.guard.session_id
    owner, _, _ = turn(repo, lease.guard, "long active ROOT", finish=False)
    compact(repo, lease.guard, owner, 1, "first boundary")
    shared = [_append(repo, lease.guard, owner) for _ in range(8)]
    for index in range(32):
        compact(repo, lease.guard, owner, 9, f"same edge {index}")
    answer = final(repo, lease.guard, owner)
    reader = CanonicalProtocolReader(repo.connection_provider)
    selected = shared if page == "shared" else [answer]
    value = _context(reader, session_id, selected)
    assert len({item["segment_id"] for item in value["entry_contexts"]}) == 1
    assert {item["segment_start_entry_id"] for item in value["entry_contexts"]} == {
        shared[0] if page == "shared" else answer
    }

    class Capture:
        def execute(self, sql, parameters):
            self.sql, self.parameters = sql, parameters
            return self

        def fetchall(self):
            return []

    capture = Capture()
    head = rows(repo, "SELECT latest_entry_sequence, latest_event_sequence FROM pulsara_v3.sessions WHERE id=%s", (session_id,))[0]
    reader._browser_context_entry_rows(capture, session_id, selected, head["latest_entry_sequence"], head["latest_event_sequence"])
    with reader._connection(monotonic() + 30) as connection:
        plan = connection.execute("EXPLAIN (ANALYZE, FORMAT JSON) " + capture.sql, capture.parameters).fetchone()["QUERY PLAN"][0]["Plan"]

    def nodes(node):
        yield node
        for child in node.get("Plans", ()):
            yield from nodes(child)

    mounts = [node for node in nodes(plan) if node.get("Alias") == "mounted"]
    assert mounts and all(node["Actual Loops"] <= 1 for node in mounts)
