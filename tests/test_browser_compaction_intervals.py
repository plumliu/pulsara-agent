"""Compaction edge intervals preserve actual canonical occurrence relations."""

from contextlib import contextmanager
from datetime import datetime, timezone
import json
from time import monotonic

import psycopg
import pytest

from pulsara_agent.conversation_kernel.contracts import InlineContent
from pulsara_agent.conversation_kernel.repository import AssistantTextBlock
from pulsara_agent.identity import random_id
from pulsara_agent.terminal_protocol.canonical_v3 import CanonicalProtocolReader, CanonicalProtocolResourceExhausted
from tests.test_conversation_fork import compact, final, new_session, repo as repo, rows, turn


pytestmark = pytest.mark.postgres


def _append(repository, guard, owner, text="partial"):
    cut = repository.prepare_provider_input_cut(guard, turn_id=owner, deadline_monotonic=monotonic()+30)
    entry_id = random_id("entry")
    repository.commit_assistant_message(
        guard, cut=cut, entry_id=entry_id, parent_content=InlineContent.from_bytes(b"manifest"),
        blocks=(AssistantTextBlock(random_id("block"), InlineContent.from_bytes(text.encode())),),
        complete_turn=False, occurred_at=datetime.now(timezone.utc), actor_id="model:test",
        deadline_monotonic=monotonic()+30,
    )
    return entry_id


def _context(reader, session_id, ids, **kwargs):
    return reader.browser_history_context(
        session_id=session_id, memory_domain_id="u_local", entry_ids=ids,
        deadline_monotonic=monotonic()+30, **kwargs,
    )


def _reference(repository, session_id, ids, event_cut, entry_cut):
    """The original definition: independently find each event's prior max entry.

    Test metadata only; this deliberately does not assume one acceptance per
    entry, or the same ordering of entry sequence and occurrence sequence.
    """
    entries = rows(repository, "SELECT * FROM pulsara_v3.transcript_entries WHERE session_id=%s ORDER BY entry_sequence", (session_id,))
    by_id = {entry["id"]: entry for entry in entries}
    accepted = rows(repository, "SELECT event_sequence, subject_entry_id FROM pulsara_v3.agent_events WHERE session_id=%s AND subject_entry_id IS NOT NULL", (session_id,))
    events = rows(repository, """SELECT ev.event_id, ev.event_sequence, ev.event_type,
        coalesce(r.turn_id,ev.subject_turn_id) AS owner, t.conversation_scope_kind
        FROM pulsara_v3.agent_events ev LEFT JOIN pulsara_v3.turn_context_binding_revisions r
          ON r.session_id=ev.session_id AND r.id=ev.subject_context_binding_revision_id
        LEFT JOIN pulsara_v3.turns t ON t.session_id=ev.session_id AND t.id=coalesce(r.turn_id,ev.subject_turn_id)
        WHERE ev.session_id=%s AND ev.event_type IN ('CompactionAdopted','TurnInterrupted')
          AND ev.event_sequence<=%s ORDER BY ev.event_sequence""", (session_id,event_cut))

    def mount(event, owner=None):
        candidates = [by_id[item["subject_entry_id"]] for item in accepted if item["event_sequence"]<event["event_sequence"]]
        if owner is None:
            candidates = [entry for entry in candidates if entry["entry_owner_kind"]=="EXECUTED_TURN" and entry["conversation_scope_kind"]=="ROOT"]
        else:
            candidates = [entry for entry in candidates if entry["turn_id"]==owner]
        return max((entry["entry_sequence"] for entry in candidates), default=0)

    selected = {by_id[entry_id]["entry_sequence"] for entry_id in ids}
    compactions = []
    for event in events:
        if event["event_type"]!="CompactionAdopted" or event["conversation_scope_kind"]!="ROOT":
            continue
        boundary = mount(event)
        if boundary in selected or (boundary==0 and entries[0]["id"] in ids):
            compactions.append((event["event_id"],str(boundary)))
    segments = {}
    for entry_id in ids:
        entry = by_id[entry_id]
        candidates = [event for event in events if event["owner"]==entry["turn_id"] and mount(event,entry["turn_id"])<entry["entry_sequence"]]
        if candidates:
            event = candidates[-1]
            boundary = mount(event,entry["turn_id"])
            start = next(item["id"] for item in entries if item["turn_id"]==entry["turn_id"] and boundary<item["entry_sequence"]<=entry_cut)
            segments[entry_id] = (f"EXECUTED_TURN:{entry['turn_id']}:{event['event_id']}",start)
        else:
            start = next(item["id"] for item in entries if item["turn_id"]==entry["turn_id"])
            segments[entry_id] = (f"EXECUTED_TURN:{entry['turn_id']}:initial",start)
    return compactions,segments


@pytest.mark.parametrize("acceptance", ["ordinary", "swapped", "duplicate"])
def test_compaction_intervals_preserve_repeated_edges_and_actual_acceptances(
    repo, stage2_migrated_postgres_database, acceptance,
):
    lease = new_session(repo)
    session_id = lease.guard.session_id
    owner,_,_ = turn(repo,lease.guard,"intervals",finish=False)
    compact(repo,lease.guard,owner,1,"first")
    compact(repo,lease.guard,owner,1,"same first edge")
    first = _append(repo,lease.guard,owner)
    compact(repo,lease.guard,owner,2,"second")
    compact(repo,lease.guard,owner,2,"same second edge")
    second = _append(repo,lease.guard,owner)
    compact(repo,lease.guard,owner,3,"third")
    last = final(repo,lease.guard,owner)
    if acceptance!="ordinary":
        # Both updates satisfy the existing FK, kind and owner constraints;
        # there is no unique/monotonic acceptance relation in clean-v0.
        with psycopg.connect(stage2_migrated_postgres_database.admin_dsn) as connection:
            if acceptance=="swapped":
                connection.execute("""UPDATE pulsara_v3.agent_events SET subject_entry_id=CASE
                    WHEN subject_entry_id=%s THEN %s ELSE %s END
                    WHERE session_id=%s AND event_type='AssistantMessageAccepted' AND subject_entry_id=ANY(%s)""",
                    (first,second,first,session_id,[first,second]))
            else:
                connection.execute("""UPDATE pulsara_v3.agent_events SET subject_entry_id=%s
                    WHERE session_id=%s AND event_type='AssistantMessageAccepted' AND subject_entry_id=%s""",
                    (second,session_id,first))
    reader = CanonicalProtocolReader(repo.connection_provider)
    all_ids = [item["id"] for item in rows(repo,"SELECT id FROM pulsara_v3.transcript_entries WHERE session_id=%s ORDER BY entry_sequence",(session_id,))]
    for ids in (all_ids,[first],[second,last],[all_ids[0],last]):
        value = _context(reader,session_id,ids)
        expected,segments = _reference(repo,session_id,ids,int(value["event_sequence_cut"]),int(value["cut_sequence"]))
        assert [(item["event_id"],item["adopted_after_entry_sequence"]) for item in value["compactions"]]==expected
        assert {item["entry_id"]:(item["segment_id"],item["segment_start_entry_id"]) for item in value["entry_contexts"]}==segments
    assert len(_context(reader,session_id,all_ids)["compactions"])==5
    size = len(json.dumps(_context(reader,session_id,all_ids)).encode())
    assert _context(reader,session_id,all_ids,maximum_serialized_bytes=size)["entry_ids"]==all_ids
    with pytest.raises(CanonicalProtocolResourceExhausted):
        _context(reader,session_id,all_ids,maximum_serialized_bytes=size-1)


def test_repeated_adoption_edges_keep_old_cuts_without_per_adoption_mounts(repo):
    lease = new_session(repo)
    session_id = lease.guard.session_id
    owner,_,_ = turn(repo,lease.guard,"long active ROOT",finish=False)
    compact(repo,lease.guard,owner,1,"initial summary")
    middle = _append(repo,lease.guard,owner)
    old_cut = rows(repo,"SELECT latest_entry_sequence, latest_event_sequence FROM pulsara_v3.sessions WHERE id=%s",(session_id,))[0]
    for index in range(32):
        compact(repo,lease.guard,owner,2,f"later summary {index}")
    answer = final(repo,lease.guard,owner)
    reader = CanonicalProtocolReader(repo.connection_provider)
    value = _context(reader,session_id,[middle])
    frozen = _context(reader,session_id,[middle],cut_sequence=old_cut["latest_entry_sequence"],event_sequence_cut=old_cut["latest_event_sequence"])
    assert len(value["compactions"])==32
    assert frozen["compactions"]==[]
    current_context,old_context = value["entry_contexts"][0],frozen["entry_contexts"][0]
    assert current_context["segment_id"]==old_context["segment_id"]
    assert current_context["segment_start_entry_id"]==old_context["segment_start_entry_id"]==middle
    assert current_context["next_entry_id"]==answer
    assert "next_entry_id" not in old_context

    captures = []

    class CompactionCursor:
        def __init__(self, cursor):
            self.cursor = cursor

        def __enter__(self):
            self.cursor.__enter__()
            return self

        def __exit__(self, *args):
            return self.cursor.__exit__(*args)

        def execute(self, sql, parameters):
            captures.append((sql, parameters))
            return self.cursor.execute(sql, parameters)

        def __iter__(self):
            return iter(self.cursor)

    class Connection:
        def __init__(self, native):
            self.native = native

        def cursor(self, *args, **kwargs):
            cursor = self.native.cursor(*args, **kwargs)
            return CompactionCursor(cursor) if kwargs.get("name")=="browser_history_compactions" else cursor

        def __getattr__(self, name):
            return getattr(self.native, name)

    with reader._connection(monotonic()+30) as connection:
        @contextmanager
        def fixed(_deadline):
            yield Connection(connection)

        reader._connection = fixed
        assert _context(reader,session_id,[middle])==value
        assert len(captures)==1
        sql,parameters = captures[0]
        plan = connection.execute("EXPLAIN (ANALYZE, FORMAT JSON) "+sql,parameters).fetchone()["QUERY PLAN"][0]["Plan"]

    def nodes(node):
        yield node
        for child in node.get("Plans",()):
            yield from nodes(child)

    # Compaction selection uses acceptance intervals; it must not calculate
    # a full ROOT mount for every adoption (including repeated same-edge ones).
    root_scans = [node for node in nodes(plan)
                  if node.get("Relation Name")=="transcript_entries" and node.get("Alias")=="e"]
    assert root_scans and all(node["Actual Loops"]<=1 for node in root_scans)
