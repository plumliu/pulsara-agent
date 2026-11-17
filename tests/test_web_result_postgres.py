"""Actual web execution is accepted once; all delivery reads the frozen result."""

import asyncio
from time import monotonic

import pytest

from pulsara_agent.conversation_kernel.repository import ConversationKernelRepository
from pulsara_agent.conversation_kernel.tool_outputs import PostgresToolOutputReadPort, project_tool_result_export
from pulsara_agent.ports.tool_execution import thaw_tool_json_object
from tests.support.postgres import verified_postgres_provider
from tests.test_hardcut_entry_parts import open_turn
from tests.test_native_tool_delivery_postgres import save, admit_page, stored
from tests.test_web_runtime import TARGET, FETCH, SEARCH, MockWebService, port_for, dispatch, dispatch_read_file

pytestmark = pytest.mark.postgres


@pytest.mark.parametrize("name,arguments", [
    ("web_search", {"query": "sources"}), ("web_fetch", {"target": TARGET}),
])
def test_web_complete_acceptance_next_history_and_export_never_reexecute(stage2_migrated_postgres_database, tmp_path, name, arguments):
    repo = ConversationKernelRepository(verified_postgres_provider(stage2_migrated_postgres_database.runtime_dsn))
    raw = "# Actual source\r\n2|literal source line\n" * 14000
    expected = ({**FETCH, "content": [{"type": "text", "value": raw}]} if name == "web_fetch" else
        {**SEARCH, "content": [{"type": "object", "value": {**SEARCH["content"][0]["value"], "snippet": raw}}]})
    service = MockWebService({name: expected})
    async def execute():
        port = port_for(tmp_path, service)
        try:
            return await dispatch(port, name, arguments)
        finally:
            await port.aclose()
    result = asyncio.run(execute())
    lease, intent, _, _ = open_turn(repo, text="research\n")
    source = save(repo, lease, intent.turn_id, value=result.content, arguments=arguments,
        state=result.state, tool_name=name)
    first = admit_page(repo, lease.guard, workspace_id=source.workspace_id,
        result_entry_id=source.result_entry_id, deadline_monotonic=monotonic()+30)
    assert first.next is not None
    _, _, restored, _ = stored(repo, source.result_entry_id)
    assert restored.body == first.body and restored.next == first.next
    reader = PostgresToolOutputReadPort(repo.connection_provider, session_id=source.session_id, workspace_id=source.workspace_id)
    async def next_pages():
        port = port_for(tmp_path, service, session_id=source.session_id)
        port._tools["read_file"].result_source_reader = reader
        try:
            pages = [first]
            while pages[-1].next is not None:
                continuation = await dispatch_read_file(port, {"target": dict(pages[-1].next)})
                pages.append(reader.select_native_page(continuation.source, position=continuation.position))
            return pages
        finally:
            await port.aclose()
    pages = asyncio.run(next_pages())
    complete = thaw_tool_json_object(reader.read_result({"kind": "SESSION_ENTRY", "entry_id": source.result_entry_id}).value)
    assert complete == expected
    assert "".join(page.body.replace("[JSON 正文片段]\n", "") for page in pages).encode() == project_tool_result_export(expected)
    assert all(page.estimated_tokens <= 12000 for page in pages)
    assert len(service.calls) == 1
