"""Imported native observations retain their child-owned complete source."""

from time import monotonic
import pytest
from pulsara_agent.conversation_kernel.content_parts import ResultPart
from pulsara_agent.ports.tool_execution import ToolBusinessValue
from tests.test_native_tool_delivery_postgres import (
    repo as repo,
    save,
    stored,
    admit_page,
    freeze_page,
)
from tests.test_hardcut_entry_parts import open_turn
from tests.test_conversation_fork import final, fork, rows

pytestmark = pytest.mark.postgres


@pytest.mark.parametrize("source_before_floor", [False, True])
def test_fork_maps_native_source_and_retains_frozen_page_without_copying_body(repo, source_before_floor):
    lease, intent, _, _ = open_turn(repo, text="native fork")
    source = save(
        repo,
        lease,
        intent.turn_id,
        value=ToolBusinessValue({}, (ResultPart("text", "body\n" * 20000),)),
    )
    first = admit_page(
        repo,
        lease.guard,
        workspace_id=source.workspace_id,
        result_entry_id=source.result_entry_id,
        deadline_monotonic=monotonic() + 30,
    )
    assert first.next is not None
    from pulsara_agent.conversation_kernel.tool_outputs import (
        PostgresToolOutputReadPort,
    )

    port = PostgresToolOutputReadPort(
        repo.connection_provider,
        session_id=source.session_id,
        workspace_id=source.workspace_id,
    )
    if source_before_floor:
        from tests.test_conversation_fork import compact
        boundary = rows(repo,
            "SELECT entry_sequence FROM pulsara_v3.transcript_entries WHERE id=%s",
            (source.result_entry_id,))[0]["entry_sequence"]
        compact(repo, lease.guard, intent.turn_id, boundary)
    continuation = port.read_native_page(first.next)
    page = port.select_native_page(continuation.source, position=continuation.position)
    observed = save(
        repo,
        lease,
        intent.turn_id,
        value=ToolBusinessValue(),
        arguments={"target": dict(first.next)},
        source=source.result_entry_id,
        page=page,
    )
    freeze_page(repo, lease.guard, result_entry_id=observed.result_entry_id, page=page)
    anchor = final(repo, lease.guard, intent.turn_id)
    child = fork(repo, source.session_id, anchor)
    assert child.created
    results = rows(
        repo,
        "SELECT * FROM pulsara_v3.tool_results WHERE session_id=%s ORDER BY source_result_entry_id=result_entry_id DESC",
        (child.child_session_id,),
    )
    assert len(results) == 2
    complete, observation = results
    assert complete["source_result_entry_id"] == complete["result_entry_id"]
    assert observation["source_result_entry_id"] == complete["result_entry_id"]
    assert complete["result_entry_id"] != source.result_entry_id
    assert (
        observation["native_projection"]
        == stored(repo, observed.result_entry_id)[0]["native_projection"]
    )
    _, parts, replayed, _ = stored(repo, observation["result_entry_id"])
    assert parts == () and replayed.body == page.body and replayed.next == page.next
    assert replayed.source_entry_id == complete["result_entry_id"]

    if source_before_floor:
        binding = rows(repo, "SELECT source_through_sequence FROM pulsara_v3.session_context_genesis WHERE session_id=%s", (child.child_session_id,))[0]
        hidden = rows(repo, "SELECT id,entry_sequence FROM pulsara_v3.transcript_entries WHERE session_id=%s AND id=ANY(%s)", (child.child_session_id, [complete["result_entry_id"], complete["tool_call_entry_id"]]))
        assert len(hidden) == 2
        assert all(row["entry_sequence"] <= binding["source_through_sequence"] for row in hidden)
        visible = rows(repo, "SELECT id FROM pulsara_v3.transcript_entries WHERE session_id=%s AND entry_sequence>%s", (child.child_session_id, binding["source_through_sequence"]))
        assert observation["result_entry_id"] in {row["id"] for row in visible}
        assert complete["result_entry_id"] not in {row["id"] for row in visible}
        # The complete source and frozen observations survive independently of
        # every parent-session row, including another fork of this child.
        nested = fork(repo, child.child_session_id, rows(repo, "SELECT id FROM pulsara_v3.transcript_entries WHERE session_id=%s AND entry_kind='ASSISTANT_MESSAGE' ORDER BY entry_sequence DESC LIMIT 1", (child.child_session_id,))[0]["id"])
        assert nested.created
        assert repo.delete_session(required_lifecycle=None, session_id=source.session_id,
            memory_domain_id="u_local",closed_writer=lease.guard,deadline_monotonic=monotonic()+30) == "DELETED"
        _, _, child_page, _ = stored(repo, observation["result_entry_id"])
        assert child_page.body == page.body
