"""Browser output reads use the original entry and the shared text positions."""

import asyncio
from types import SimpleNamespace
import pytest

from pulsara_agent.identity import random_id
from pulsara_agent.ports.tool_execution import ToolOutputSourceCoverage
from pulsara_agent.ports.tool_output import ToolOutputBodyView
from pulsara_agent.terminal_protocol.generated_v3 import terminal_kernel_v3_pb2 as wire
from pulsara_agent.terminal_protocol.v3_gateway import TerminalKernelProtocolServer
from pulsara_agent.terminal_protocol.canonical_v3 import browser_tool_output_request


def test_browser_pages_exact_entry_body_without_reexecuting_or_reidentifying(monkeypatch):
    session_id, workspace_id, entry_id = random_id("session"), random_id("workspace"), random_id("entry")
    content = "你好🙂\r\n" + "中文🙂" * 15 + "\nend"
    reads = []

    class Port:
        def __init__(self, provider, **scope):
            assert scope == {"session_id": session_id, "workspace_id": workspace_id}

        def read_body(self, target):
            reads.append(target)
            return ToolOutputBodyView(target, content.encode(), "text/plain", "utf-8", ToolOutputSourceCoverage.COMPLETE)

    monkeypatch.setattr("pulsara_agent.terminal_protocol.v3_gateway.PostgresToolOutputReadPort", Port)
    reader = SimpleNamespace(tool_output_workspace=lambda **kwargs: workspace_id)

    async def scenario():
        position = wire.ToolOutputTextPosition(line=1)
        fragments = []
        while True:
            response = await TerminalKernelProtocolServer.read_canonical_tool_output(
                connection_provider=object(), protocol_reader=reader, session_id=session_id,
                request=wire.ReadToolOutputRequest(result_entry_id=entry_id, position=position, max_chars=7),
            )
            assert response.WhichOneof("response") == "tool_output"
            page = response.tool_output
            assert page.result_entry_id == entry_id
            assert page.position == position
            fragments.append(page.text)
            if not page.HasField("next"):
                break
            assert (page.next.line, page.next.char_offset) > (position.line, position.char_offset)
            position = page.next
        assert "".join(fragments) == content

    asyncio.run(scenario())
    assert reads and all(target == {"kind": "SESSION_ENTRY", "entry_id": entry_id} for target in reads)


def test_browser_rejects_invalid_entry_before_reading_any_body():
    response = asyncio.run(TerminalKernelProtocolServer.read_canonical_tool_output(
        connection_provider=object(), protocol_reader=object(), session_id=random_id("session"),
        request=wire.ReadToolOutputRequest(result_entry_id=random_id("result"), max_chars=7),
    ))
    assert response.error.stable_code == "TOOL_OUTPUT_RANGE_INVALID"


@pytest.mark.parametrize("extra", [{"offset_chars": 0}, {"position": None},
    {"position": {"line": 0}}, {"position": {"offset": 1}}, {"max_chars": True}])
def test_browser_request_accepts_only_the_single_new_position_contract(extra):
    with pytest.raises(ValueError):
        browser_tool_output_request({"result_entry_id": random_id("entry"), **extra})


def test_browser_missing_or_inaccessible_source_is_not_an_empty_body(monkeypatch):
    class Port:
        def __init__(self, *_args, **_kwargs):
            pass

        def read_body(self, target):
            raise KeyError(target["entry_id"])

    monkeypatch.setattr("pulsara_agent.terminal_protocol.v3_gateway.PostgresToolOutputReadPort", Port)
    response = asyncio.run(TerminalKernelProtocolServer.read_canonical_tool_output(
        connection_provider=object(),
        protocol_reader=SimpleNamespace(tool_output_workspace=lambda **kwargs: random_id("workspace")),
        session_id=random_id("session"), request=wire.ReadToolOutputRequest(result_entry_id=random_id("entry"), max_chars=7),
    ))
    assert response.error.stable_code == "TOOL_OUTPUT_MISSING"
