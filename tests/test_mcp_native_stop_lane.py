"""Native turn Stop must cancel lane waits without dispatching effects."""
from tests.support.mcp import finish_connection_attempts
import asyncio
import json
import re
import sys
from time import monotonic

import pytest
from psycopg.rows import dict_row

from pulsara_agent.identity import random_id
from pulsara_agent.llm.input import PromptContent, text_part_values
from pulsara_agent.mcp_config import load_mcp_server_configs
from pulsara_agent.primitives.permission import PermissionMode
from pulsara_agent.storage.postgres_connection_provider import PostgresConnectionLane
from tests.test_round4_plan_host import _DetachedDraftModel, _open_test_session, _tool_call

pytestmark = pytest.mark.postgres


@pytest.fixture(autouse=True)
def private_home(tmp_path, monkeypatch):
    monkeypatch.setenv("PULSARA_HOME", str(tmp_path / "home"))


@pytest.mark.parametrize("blocked_lane", ["server", "host"])
def test_native_stop_while_mcp_lane_full_does_not_dispatch(tmp_path, monkeypatch, stage2_migrated_postgres_database, blocked_lane):
    marker = tmp_path / "remote-effect.txt"
    server_file = tmp_path / "mcp_server.py"
    server_file.write_text('''from pathlib import Path
from mcp.server.mcpserver import MCPServer
import mcp_types as types
server = MCPServer("stop-regression")
@server.tool(annotations=types.ToolAnnotations(readOnlyHint=False, destructiveHint=True, openWorldHint=True))
def write_mark(value: str) -> str:
    Path(''' + repr(str(marker)) + ''').write_text(value)
    return value
if __name__ == "__main__":
    server.run()
''')
    config_path = tmp_path / "mcp.yaml"
    config_path.write_text('servers:\n  fixture:\n    enabled: true\n    catalog_refresh_interval_ms: DISABLED\n    transport: ' + json.dumps({
        "type": "stdio", "command": sys.executable, "args": [str(server_file)],
    }) + '\n')
    configs = load_mcp_server_configs(user_config_path=config_path)

    class Model(_DetachedDraftModel):
        async def stream(self, request):
            self.requests.append(request)
            if len(self.requests) == 1:
                assert "mcp__fixture__write_mark" not in {
                    spec.name for spec in request.compiled_input.tools
                }
                events = _tool_call("query_capabilities", "call:inspect", {
                    "kind": "MCP",
                    "target": {"kind": "MCP_TOOL", "server_id": "fixture", "remote_tool_name": "write_mark"},
                })
            else:
                assert len(self.requests) == 2
                bodies = [text for message in request.compiled_input.messages
                    for text in text_part_values(message.content)]
                token = next((match.group() for body in reversed(bodies)
                    if (match := re.search(r"mcpref:[a-z2-7]{26}", body)) is not None), None)
                assert token is not None
                events = _tool_call("call_mcp_tool", "call:effect", {
                    "target": {"kind": "MCP_TOOL_REF", "tool_ref": token},
                    "arguments": {"value": "dispatched"},
                })
            for event in events:
                yield event

    async def exercise():
        core, session = await _open_test_session(tmp_path=tmp_path, model=Model(),
            postgres_dsn=stage2_migrated_postgres_database.runtime_dsn, monkeypatch=monkeypatch)
        running = None
        held = 0
        lane = None
        try:
            await session.attach_controller("browser:stop-test")
            await session.reload_mcp_configs(configs, deadline_monotonic=monotonic() + 20)
            await finish_connection_attempts(session._mcp_supervisor)
            session._tools.prepare_tool_surface_safe_point()
            slot = session._mcp_supervisor._slots["fixture"]
            server_capacity, host_capacity = slot._lane._value, slot._host_lane._value
            lane = slot._lane if blocked_lane == "server" else slot._host_lane
            held = lane._value
            for _ in range(held):
                await lane.acquire()
            running = asyncio.create_task(session.run_turn(PromptContent.text("execute effect"),
                command_id=random_id("command"), requested_permission_mode=PermissionMode.ASK_PERMISSIONS))
            async with asyncio.timeout(10):
                while session.live_control.current_snapshot().current_interaction is None:
                    await asyncio.sleep(.005)
            snapshot = session.live_control.current_snapshot()
            await session.resolve_tool_interaction(
                expected_writer_generation=session._lease.guard.writer_generation,
                expected_owner_epoch=snapshot.owner_epoch, expected_live_revision=snapshot.revision,
                interaction_id=snapshot.current_interaction.interaction_id, command_id=random_id("command"),
                decision="ALLOW", actor_id="browser:stop-test")
            async with asyncio.timeout(10):
                while not lane._waiters:
                    await asyncio.sleep(.005)
            accepted = await session.request_stop_turn(command_id=random_id("command"),
                admission_deadline_ms=session.control_admission_deadline_ms(),
                expected_session_id=session.session_id, expected_host_session_id=session.host_session_id,
                target_turn_id=session._active_turn_id)
            assert accepted.status == "PENDING"
            done, _ = await asyncio.wait((running,), timeout=2)
            stopped_before_lane_release = bool(done)
            for _ in range(held):
                lane.release()
            held = 0
            await asyncio.gather(running, return_exceptions=True)
            assert stopped_before_lane_release, f"Stop hung on {blocked_lane} lane; remote dispatched={marker.exists()}"
            assert not marker.exists()
            assert not slot._permits
            assert slot._active_operation_count == 0
            assert slot._lane._value == server_capacity
            assert slot._host_lane._value == host_capacity
            with session.repository.connection_provider.connection(
                lane=PostgresConnectionLane.INSPECTOR, row_factory=dict_row,
                deadline_monotonic=monotonic() + 10,
            ) as connection:
                rows = connection.execute(
                    "SELECT tool_call_id, result_state, result_origin_kind "
                    "FROM pulsara_v3.tool_results WHERE session_id=%s ORDER BY tool_call_id",
                    (session.session_id,),
                ).fetchall()
            assert rows == [
                {"tool_call_id": "call:effect", "result_state": "CANCELLED_BEFORE_DISPATCH", "result_origin_kind": "PHYSICAL_ATTEMPT"},
                {"tool_call_id": "call:inspect", "result_state": "SUCCESS", "result_origin_kind": "PHYSICAL_ATTEMPT"},
            ]
        finally:
            if lane is not None:
                for _ in range(held):
                    lane.release()
            await core.close_session(session.host_session_id)
            if running is not None:
                await asyncio.gather(running, return_exceptions=True)
            await core.shutdown()

    asyncio.run(exercise())
