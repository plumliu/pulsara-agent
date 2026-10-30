"""Read-only browser windows over real canonical rows, with no running Host."""

from time import monotonic
import json

import pytest

from pulsara_agent.terminal_protocol.canonical_v3 import (
    CanonicalProtocolGap,
    CanonicalProtocolReader,
    CanonicalProtocolResourceExhausted,
    browser_history_uint64,
)
from tests.test_conversation_fork import (
    repo as repo, new_session, turn, rows, fork, compact, final,
)
from tests.test_turn_interruption_history import stop, snapshot


pytestmark = pytest.mark.postgres


def window(reader, session, **kwargs):
    return reader.browser_history_window(
        session_id=session, memory_domain_id="u_local",
        deadline_monotonic=monotonic() + 30, **kwargs,
    )


def context(reader, session, ids, **kwargs):
    return reader.browser_history_context(
        session_id=session, memory_domain_id="u_local", entry_ids=ids,
        deadline_monotonic=monotonic() + 30, **kwargs,
    )


def test_anchor_and_bidirectional_keysets_cover_all_history_without_writes(repo):
    lease = new_session(repo)
    session = lease.guard.session_id
    turns = [turn(repo, lease.guard, str(index)) for index in range(7)]
    reader = CanonicalProtocolReader(repo.connection_provider)
    before = rows(repo, "SELECT latest_entry_sequence, latest_event_sequence FROM pulsara_v3.sessions WHERE id=%s", (session,))
    center = window(reader, session, anchor_entry_id=turns[3][2], maximum_entries=3)
    assert [entry["entry_sequence"] for entry in center["entries"]] == ["7", "8", "9"]
    assert center["anchor_entry_id"] == turns[3][2]
    seen = {entry["entry_id"] for entry in center["entries"]}
    for direction in ("older", "newer"):
        cursor = center[direction + "_cursor"]
        while cursor:
            page = window(reader, session, cursor=cursor, maximum_entries=3)
            assert page["cut_sequence"] == center["cut_sequence"]
            assert page["event_sequence_cut"] == center["event_sequence_cut"]
            assert len(page["entries"]) <= 3
            new_ids = {entry["entry_id"] for entry in page["entries"]}
            assert not new_ids.intersection(seen)
            seen.update(new_ids)
            cursor = page.get(direction + "_cursor")
    assert len(seen) == 14
    assert rows(repo, "SELECT latest_entry_sequence, latest_event_sequence FROM pulsara_v3.sessions WHERE id=%s", (session,)) == before
    with pytest.raises(KeyError):
        reader.browser_history_window(session_id=session, memory_domain_id="wrong-domain",
            anchor_entry_id=turns[3][2], deadline_monotonic=monotonic() + 30)
    with pytest.raises(ValueError, match="session conflicts"):
        window(reader, session, cursor={**center["older_cursor"], "session_id": "other"})
    with pytest.raises(ValueError, match="exact uint64"):
        window(reader, session, cursor={key: value for key, value in center["older_cursor"].items() if key != "event_sequence_cut"})


def test_context_exact_sets_middle_turn_and_canonical_root_neighbours(repo):
    lease = new_session(repo)
    session = lease.guard.session_id
    first = turn(repo, lease.guard, "first")
    second = turn(repo, lease.guard, "second")
    third = turn(repo, lease.guard, "third")
    reader = CanonicalProtocolReader(repo.connection_provider)
    value = context(reader, session, [third[2], first[2]])
    assert value["entry_ids"] == [first[2], third[2]]
    groups = {group["owner_id"]: group for group in value["groups"]}
    assert groups[first[0]]["next_owner_id"] == second[0]
    assert groups[third[0]]["previous_owner_id"] == second[0]
    assert second[0] not in groups  # Never synthesize continuity across the gap.
    one = context(reader, session, [third[2]])
    assert one["entry_contexts"][0]["segment_id"] == value["entry_contexts"][-1]["segment_id"]
    assert one["entry_contexts"][0]["segment_start_entry_id"] != third[2]
    assert one["entry_contexts"][0]["previous_entry_id"] == groups[third[0]]["initial_entry_id"]
    assert one["entry_contexts"][0]["owner_id"] == third[0]
    with pytest.raises(KeyError):
        context(reader, session, ["entry:missing"])
    with pytest.raises(CanonicalProtocolGap):
        context(reader, session, [third[2]], cut_sequence=2, event_sequence_cut=int(value["event_sequence_cut"]))


def test_context_refresh_finds_later_interruption_and_each_real_compaction(repo):
    lease = new_session(repo)
    session = lease.guard.session_id
    turn(repo, lease.guard, "old")
    active, _, partial = turn(repo, lease.guard, "working", complete=False)
    reader = CanonicalProtocolReader(repo.connection_provider)
    before = snapshot(reader, session)
    ids = [entry.entry_id for entry in before.entries]
    compact(repo, lease.guard, active, 2)
    compact(repo, lease.guard, active, 3)
    stop(repo, lease.guard, active, detail="retained detail")
    frozen = context(reader, session, ids, cut_sequence=before.entry_sequence_cut, event_sequence_cut=before.event_sequence_cut)
    assert frozen["compactions"] == []
    assert frozen["interruption_notices"] == []
    refreshed = context(reader, session, [partial])
    assert len(refreshed["compactions"]) == 2
    assert len({item["event_id"] for item in refreshed["compactions"]}) == 2
    assert {item["adopted_after_entry_sequence"] for item in refreshed["compactions"]} == {"4"}
    assert refreshed["interruption_notices"][0]["owner_id"] == active
    assert refreshed["interruption_notices"][0]["public_detail"] == "retained detail"
    assert context(reader, session, ids[:2])["compactions"] == []


def test_segments_follow_compaction_without_moving_preceding_entries(repo):
    lease = new_session(repo)
    session = lease.guard.session_id
    turn(repo, lease.guard, "old")
    active, _, partial = turn(repo, lease.guard, "work", complete=False)
    reader = CanonicalProtocolReader(repo.connection_provider)
    old_segment = context(reader, session, [partial])["entry_contexts"][0]["segment_id"]
    compact(repo, lease.guard, active, 2)
    answer = final(repo, lease.guard, active)
    value = context(reader, session, [partial, answer])
    assert value["entry_contexts"][0]["segment_id"] == old_segment
    assert value["entry_contexts"][1]["segment_id"] != old_segment
    assert value["entry_contexts"][1]["segment_start_entry_id"] == answer


def test_exact_boundary_occurrences_locate_all_affected_pages_without_latest_control(repo, monkeypatch):
    lease = new_session(repo)
    session = lease.guard.session_id
    turn(repo, lease.guard, "old")
    first, _, first_edge = turn(repo, lease.guard, "first work", complete=False)
    reader = CanonicalProtocolReader(repo.connection_provider)
    before = snapshot(reader, session)
    compact(repo, lease.guard, first, 2)
    compact(repo, lease.guard, first, 3)
    compact(repo, lease.guard, first, 4)
    final(repo, lease.guard, first)
    second, _, second_edge = turn(repo, lease.guard, "second work", complete=False)
    compact(repo, lease.guard, second, 5)
    compact(repo, lease.guard, second, 6)
    compact(repo, lease.guard, second, 7)
    stop(repo, lease.guard, second)
    events = rows(repo, """SELECT event_id, event_sequence, event_type FROM pulsara_v3.agent_events
        WHERE session_id=%s AND event_type IN ('CompactionAdopted', 'TurnInterrupted')
        ORDER BY event_sequence""", (session,))
    assert len(events) == 7
    ids = [event["event_id"] for event in reversed(events)]
    heads = rows(repo, "SELECT latest_entry_sequence, latest_event_sequence FROM pulsara_v3.sessions WHERE id=%s", (session,))
    # These separate pages contain different true boundaries. Latest control
    # reports only the second adoption and cannot invalidate the first page.
    first_page = context(reader, session, [first_edge])
    second_page = context(reader, session, [second_edge])
    assert len(first_page["compactions"]) == 3
    assert len(second_page["compactions"]) == 3
    monkeypatch.setattr(reader, "_entry", lambda *_: pytest.fail("boundary lookup must not read content"))
    monkeypatch.setattr(reader, "_browser_context_groups", lambda *_: pytest.fail("boundary lookup must not enrich unrelated pages"))
    value = context(reader, session, [], boundary_event_ids=ids)
    assert value["entry_ids"] == [] and value["groups"] == [] and value["compactions"] == []
    assert value["boundary_entries"] == [
        {"event_id": event["event_id"], "event_sequence": str(event["event_sequence"]),
         "entry_id": first_edge if index < 3 else second_edge,
         "entry_sequence": "4" if index < 3 else "7"}
        for index, event in enumerate(events)
    ]
    assert rows(repo, "SELECT latest_entry_sequence, latest_event_sequence FROM pulsara_v3.sessions WHERE id=%s", (session,)) == heads
    with pytest.raises(CanonicalProtocolGap, match="beyond its cut"):
        context(reader, session, [], boundary_event_ids=ids,
                cut_sequence=before.entry_sequence_cut, event_sequence_cut=before.event_sequence_cut)
    size = len(json.dumps(value).encode())
    assert size > 1024
    assert context(reader, session, [], boundary_event_ids=ids, maximum_serialized_bytes=size) == value
    with pytest.raises(CanonicalProtocolResourceExhausted):
        context(reader, session, [], boundary_event_ids=ids, maximum_serialized_bytes=size - 1)
    other = new_session(repo)
    with pytest.raises(KeyError):
        context(reader, other.guard.session_id, [], boundary_event_ids=ids)
    wrong_kind = rows(repo, "SELECT event_id FROM pulsara_v3.agent_events WHERE session_id=%s AND event_type='UserMessageAccepted' ORDER BY event_sequence LIMIT 1", (session,))[0]["event_id"]
    with pytest.raises(ValueError, match="not a boundary"):
        context(reader, session, [], boundary_event_ids=[wrong_kind])


@pytest.mark.parametrize("entry_ids,event_ids", [
    ([], ["one", "one"]), ([], [""]), ([], [1]), ([], "one"),
    ([f"entry-{index}" for index in range(256)], ["event-one"]),
])
def test_boundary_request_shares_existing_entry_budget_and_validates_ids(entry_ids, event_ids):
    reader = CanonicalProtocolReader(None)
    with pytest.raises(ValueError):
        context(reader, "session", entry_ids, boundary_event_ids=event_ids)


def test_imported_history_zero_event_cut_and_no_invented_tasks(repo):
    lease = new_session(repo)
    first, _, _ = turn(repo, lease.guard, "interrupted", finish=False)
    stop(repo, lease.guard, first)
    _, _, answer = turn(repo, lease.guard, "copy", answer='{"task_id":"old-task","batch_id":"old-batch"}')
    child = fork(repo, lease.guard.session_id, answer)
    assert child.created
    reader = CanonicalProtocolReader(repo.connection_provider)
    snap = snapshot(reader, child.child_session_id)
    page = window(reader, child.child_session_id, anchor_entry_id=snap.entries[-1].entry_id, maximum_entries=1)
    assert page["event_sequence_cut"] == "0"
    assert page["older_cursor"]["event_sequence_cut"] == "0"
    ids = [entry.entry_id for entry in snap.entries]
    value = context(reader, child.child_session_id, ids, cut_sequence=snap.entry_sequence_cut, event_sequence_cut=0)
    assert all(group["owner_kind"] == "IMPORTED_HISTORY" for group in value["groups"])
    assert all(entry["owner_id"] for entry in value["entry_contexts"])
    assert len(value["interruption_notices"]) == 1
    assert "tasks" not in value and "batches" not in value and value["compactions"] == []
    with pytest.raises(KeyError):
        window(reader, child.child_session_id, anchor_entry_id=answer)




def test_json_budgets_include_envelopes_and_context_is_never_truncated(repo):
    lease = new_session(repo)
    session = lease.guard.session_id
    _, _, answer = turn(repo, lease.guard, "long" * 1000)
    reader = CanonicalProtocolReader(repo.connection_provider)
    page = window(reader, session, anchor_entry_id=answer, maximum_entries=1)
    size = len(json.dumps(page).encode())
    assert size >= 1024
    assert window(reader, session, anchor_entry_id=answer, maximum_entries=1, maximum_serialized_bytes=size) == page
    with pytest.raises(CanonicalProtocolResourceExhausted):
        window(reader, session, anchor_entry_id=answer, maximum_entries=1, maximum_serialized_bytes=size - 1)
    ids = [entry.entry_id for entry in snapshot(reader, session).entries]
    value = context(reader, session, ids)
    size = len(json.dumps(value).encode())
    assert size >= 1024
    assert context(reader, session, ids, maximum_serialized_bytes=size) == value
    with pytest.raises(CanonicalProtocolResourceExhausted):
        context(reader, session, ids, maximum_serialized_bytes=size - 1)


@pytest.mark.parametrize("value", [True, 1.0, None, "-1", "1.0", 2**64])
def test_browser_uint64_rejects_lossy_or_invalid_inputs(value):
    with pytest.raises(ValueError):
        browser_history_uint64(value, "cut")


def test_browser_uint64_retains_wire_precision_and_explicit_zero():
    assert browser_history_uint64(str(2**53 + 3), "cut") == 2**53 + 3
    assert browser_history_uint64("0", "cut") == 0
