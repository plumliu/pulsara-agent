"""Window byte admission quotes immutable entry bodies once."""
from collections import Counter
import json

import pytest

from pulsara_agent.terminal_protocol import canonical_v3
from tests.test_browser_history_paging import window
from tests.test_conversation_fork import new_session, repo as repo, turn


pytestmark = pytest.mark.postgres


def test_growing_window_does_not_reencode_previously_quoted_bodies(repo, monkeypatch):
    lease = new_session(repo)
    answers = [turn(repo, lease.guard, str(index), answer='答案 "quoted"\\\n' * 120)[2]
               for index in range(6)]
    reader = canonical_v3.CanonicalProtocolReader(repo.connection_provider)
    quoted = Counter()
    original = canonical_v3._browser_json_size

    def measured(value):
        if isinstance(value, dict):
            if "entries" in value:
                assert not value["entries"], "an admission quote re-encoded all earlier entry bodies"
            if "entry_id" in value:
                quoted[value["entry_id"]] += 1
        return original(value)

    monkeypatch.setattr(canonical_v3, "_browser_json_size", measured)
    value = window(reader, lease.guard.session_id, anchor_entry_id=answers[2], maximum_entries=5)
    assert len(value["entries"]) == 5
    assert quoted == Counter({entry["entry_id"]: 1 for entry in value["entries"]})


def test_multi_entry_exact_cutoff_includes_both_cursor_directions(repo):
    lease = new_session(repo)
    answers = [turn(repo, lease.guard, str(index), answer="large answer " * 150)[2]
               for index in range(7)]
    reader = canonical_v3.CanonicalProtocolReader(repo.connection_provider)
    session = lease.guard.session_id
    middle = window(reader, session, anchor_entry_id=answers[3], maximum_entries=5)
    requests = [dict(anchor_entry_id=answers[3], maximum_entries=5)]
    requests.extend(dict(cursor=middle[direction + "_cursor"], maximum_entries=3)
                    for direction in ("older", "newer"))
    for request in requests:
        expected = window(reader, session, **request)
        size = len(json.dumps(expected).encode("utf-8"))
        assert size >= 1024
        assert window(reader, session, maximum_serialized_bytes=size, **request) == expected
        smaller = window(reader, session, maximum_serialized_bytes=size - 1, **request)
        assert len(smaller["entries"]) == len(expected["entries"]) - 1
        assert len(json.dumps(smaller).encode("utf-8")) <= size - 1
