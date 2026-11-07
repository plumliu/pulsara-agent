"""Work display titles follow their own queue identity through canonical reads."""
from __future__ import annotations

import pytest
from google.protobuf.json_format import MessageToDict

from pulsara_agent.terminal_protocol.canonical_v3 import CanonicalProtocolReader
from tests.test_agent_requests_persistence import (
    deadline, finish, now, request, source as source, start,
)

pytestmark = pytest.mark.postgres


def test_queue_history_and_unstarted_stop_read_each_works_own_title(source):
    repository, root, admit = source
    first = request(work_title="第一项：调查", instruction="同一正文")
    admit(first)
    child, first_input = start(repository, first)
    finish(repository, child, first_input, "FIRST_FINAL_BODY")
    repository.release_host_writer(child.guard, deadline_monotonic=deadline())
    second = request(agent_id=first.agent_id, create=False, work_title="第二项：复审",
                     instruction="同一正文", materials=(first.work_id,))
    stopped = request(agent_id=first.agent_id, create=False, work_title="第三项：取消",
                      instruction="未启动正文")
    unnamed = request(agent_id=first.agent_id, create=False, instruction="无标题正文")
    admit(second, stopped, unnamed)
    reader = CanonicalProtocolReader(repository.connection_provider)
    common = dict(session_id=first.agent_id, root_session_id=root.guard.session_id,
                  memory_domain_id="u_local", deadline_monotonic=deadline())
    queue, cursor = reader.queue_page(after_sequence=0, limit=10, **common)
    assert cursor is None
    assert [(item.queue_item_id, item.work_title) for item in queue] == [
        (second.work_id, "第二项：复审"), (stopped.work_id, "第三项：取消"), (unnamed.work_id, ""),
    ]
    assert queue[0].HasField("work_title")
    assert not queue[2].HasField("work_title")
    assert MessageToDict(queue[0], preserving_proto_field_name=True)["work_title"] == "第二项：复审"
    assert "work_title" not in MessageToDict(queue[2], preserving_proto_field_name=True)

    _, second_input = start(repository, second)
    snapshot = reader.snapshot(session_id=first.agent_id, maximum_entries=100,
                               maximum_control_items=100, deadline_monotonic=deadline())
    instructions = [entry for entry in snapshot.entries if entry.input_origin == "AGENT_INSTRUCTION"]
    assert [(entry.input_source.queue_item_id, entry.input_source.work_title) for entry in instructions] == [
        (first.work_id, "第一项：调查"), (second.work_id, "第二项：复审"),
    ]
    assert instructions[0].entry_id == first_input.ordered_input_entries[0].entry_id
    assert instructions[1].entry_id == second_input.ordered_input_entries[0].entry_id
    assert instructions[0].content.inline_content == instructions[1].content.inline_content
    assert all("项：".encode() not in entry.content.inline_content for entry in snapshot.entries)
    materials = [entry for entry in snapshot.entries if entry.input_origin == "AGENT_MATERIAL"]
    assert len(materials) == 1 and not materials[0].input_source.HasField("work_title")

    history = reader.history_page(
        session_id=first.agent_id, cut_sequence=snapshot.entry_sequence_cut,
        event_sequence_cut=snapshot.event_sequence_cut,
        before_entry_sequence=snapshot.entry_sequence_cut + 1,
        maximum_entries=100, deadline_monotonic=deadline(),
    )
    assert [(entry.input_source.queue_item_id, entry.input_source.work_title)
            for entry in history.entries if entry.input_origin == "AGENT_INSTRUCTION"] == [
        (first.work_id, "第一项：调查"), (second.work_id, "第二项：复审"),
    ]
    repository.cancel_agent_requests(root.guard, agent_id=first.agent_id, work_id=stopped.work_id,
                                     occurred_at=now(), actor_id="test", deadline_monotonic=deadline())
    terminal = reader.queue_item(queue_item_id=stopped.work_id, **common)
    assert terminal.status == "CANCELLED"
    assert terminal.work_title == "第三项：取消"
    assert terminal.consumed_entry_id == ""
    after = reader.snapshot(session_id=first.agent_id, maximum_entries=100,
                            maximum_control_items=100, deadline_monotonic=deadline())
    assert [entry.entry_id for entry in after.entries] == [entry.entry_id for entry in snapshot.entries]
