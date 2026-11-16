"""Stop joins local media adaptation after the remote MCP outcome is known."""

import asyncio
import base64
import json
import re
import sys
from io import BytesIO
from time import monotonic
import pytest
from PIL import Image
from psycopg.rows import dict_row
from tests.test_round4_plan_host import (
    _DetachedDraftModel,
    _open_test_session,
    _tool_call,
    _text,
)
from pulsara_agent.identity import random_id
from pulsara_agent.llm.input import PromptContent, text_part_values
from pulsara_agent.mcp_config import load_mcp_server_configs
from pulsara_agent.primitives.permission import PermissionMode
from pulsara_agent.storage.postgres_connection_provider import PostgresConnectionLane


@pytest.mark.postgres
@pytest.mark.parametrize(
    "remote_kind,outcome_kind",
    [
        ("tool", "success"),
        ("tool", "remote_error"),
        ("tool", "image_error"),
        ("tool", "text_error"),
        ("resource", "success"),
        ("tool", "shutdown"),
        ("resource", "shutdown"),
    ],
)
def test_stop_after_remote_return_preserves_complete_result(
    tmp_path, monkeypatch, stage2_migrated_postgres_database, remote_kind, outcome_kind
):
    monkeypatch.setenv("PULSARA_HOME", str(tmp_path / "home"))
    marker = tmp_path / "remote-effect.txt"
    image = BytesIO()
    Image.new("RGB", (3, 3), "red").save(image, format="PNG")
    encoded = base64.b64encode(image.getvalue()).decode()
    remote_error = outcome_kind in {"remote_error", "text_error"}
    failed = outcome_kind != "success"
    has_images = outcome_kind != "text_error"
    content = [{"type": "text", "text": "actual effect completed"}]
    if has_images:
        content.append({"type": "image", "data": encoded, "mimeType": "image/png"})
    if outcome_kind == "image_error":
        content.append(
            {
                "type": "image",
                "data": base64.b64encode(b"broken image").decode(),
                "mimeType": "image/png",
            }
        )
    content.append({"type": "text", "text": "remaining business text"})
    payload = {
        "content": content,
        "structuredContent": {"effect": "completed", "count": 1},
        "isError": remote_error,
    }
    server_file = tmp_path / "mcp_server.py"
    server_file.write_text(f"""from pathlib import Path
import base64
from mcp.server.mcpserver import MCPServer
import mcp_types as types
server=MCPServer('known-result-stop')
@server.tool(annotations=types.ToolAnnotations(readOnlyHint=False,destructiveHint=True,openWorldHint=True))
def write_mark(value:str)->types.CallToolResult:
    with Path({str(marker)!r}).open('a') as output: output.write(value)
    return types.CallToolResult.model_validate({payload!r})
@server.resource('fixture://image',mime_type='image/png')
def image_resource()->bytes:
    with Path({str(marker)!r}).open('a') as output: output.write('dispatched-once')
    return base64.b64decode({encoded!r})
if __name__=='__main__': server.run()
""")
    config_path = tmp_path / "mcp.yaml"
    config_path.write_text(
        "servers:\n  fixture:\n    enabled: true\n    catalog_refresh_interval_ms: DISABLED\n    transport: "
        + json.dumps(
            {"type": "stdio", "command": sys.executable, "args": [str(server_file)]}
        )
        + "\n"
    )
    configs = load_mcp_server_configs(user_config_path=config_path)

    class Model(_DetachedDraftModel):
        async def stream(self, request):
            self.requests.append(request)
            if remote_kind == "resource":
                assert len(self.requests) == 1
                events = _tool_call(
                    "read_mcp_resource",
                    "call:effect",
                    {
                        "target": {
                            "kind": "MCP_RESOURCE",
                            "server_id": "fixture",
                            "uri": "fixture://image",
                        }
                    },
                )
            elif len(self.requests) == 3 and not has_images:
                events = _text("DONE")
            elif len(self.requests) == 1:
                events = _tool_call(
                    "query_capabilities",
                    "call:inspect",
                    {
                        "kind": "MCP",
                        "target": {
                            "kind": "MCP_TOOL",
                            "server_id": "fixture",
                            "remote_tool_name": "write_mark",
                        },
                    },
                )
            else:
                assert len(self.requests) == 2
                bodies = [
                    text
                    for message in request.compiled_input.messages
                    for text in text_part_values(message.content)
                ]
                token = next(
                    match.group()
                    for body in reversed(bodies)
                    if (match := re.search(r"mcpref:[a-z2-7]{26}", body))
                )
                events = _tool_call(
                    "call_mcp_tool",
                    "call:effect",
                    {
                        "target": {"kind": "MCP_TOOL_REF", "tool_ref": token},
                        "arguments": {"value": "dispatched-once"},
                    },
                )
            for event in events:
                yield event

    async def scenario():
        import pulsara_agent.conversation_kernel.tool_runtime as tool_runtime
        from pulsara_agent.conversation_kernel.image_validation import (
            HostPromptImageValidator,
        )

        entered = asyncio.Event()
        release = asyncio.Event()
        validator_closed = asyncio.Event()
        known = []
        original = tool_runtime._kernel_result_from_mcp_known

        async def capture(value, **kwargs):
            known.append(json.loads(value.content))
            return await original(value, **kwargs)

        monkeypatch.setattr(tool_runtime, "_kernel_result_from_mcp_known", capture)
        validate = HostPromptImageValidator.freeze_local_image

        async def delayed(self, payload, **kwargs):
            entered.set()
            await release.wait()
            return await validate(self, payload, **kwargs)

        monkeypatch.setattr(HostPromptImageValidator, "freeze_local_image", delayed)
        close_validator = HostPromptImageValidator.aclose

        async def observe_close(self):
            await close_validator(self)
            validator_closed.set()

        monkeypatch.setattr(HostPromptImageValidator, "aclose", observe_close)
        core, session = await _open_test_session(
            tmp_path=tmp_path,
            model=Model(),
            postgres_dsn=stage2_migrated_postgres_database.runtime_dsn,
            monkeypatch=monkeypatch,
        )
        running = None
        closing = None
        try:
            await session.reload_mcp_configs(
                configs, deadline_monotonic=monotonic() + 20
            )
            await session._mcp_supervisor.wait_initial_readiness()
            session._tools.prepare_tool_surface_safe_point()
            running = asyncio.create_task(
                session.run_turn(
                    PromptContent.text("execute effect"),
                    requested_permission_mode=PermissionMode.BYPASS_PERMISSIONS,
                )
            )
            if has_images:
                await asyncio.wait_for(entered.wait(), 15)
                assert marker.read_text() == "dispatched-once" and len(known) == 1
                if outcome_kind == "shutdown":
                    closing = asyncio.create_task(core.shutdown())
                    await asyncio.wait_for(validator_closed.wait(), 10)
                else:
                    accepted = await session.request_stop_turn(
                        command_id=random_id("command"),
                        admission_deadline_ms=session.control_admission_deadline_ms(),
                        expected_session_id=session.session_id,
                        expected_host_session_id=session.host_session_id,
                        target_turn_id=session._active_turn_id,
                    )
                    assert accepted.status == "PENDING"
                release.set()
            outcome = await asyncio.wait_for(
                asyncio.gather(running, return_exceptions=True), 10
            )
            if closing is not None:
                await asyncio.wait_for(closing, 10)
            from tests.support.postgres import verified_postgres_provider

            provider = verified_postgres_provider(
                stage2_migrated_postgres_database.runtime_dsn
            )
            with provider.connection(
                lane=PostgresConnectionLane.INSPECTOR,
                row_factory=dict_row,
                deadline_monotonic=monotonic() + 10,
            ) as conn:
                rows = conn.execute(
                    "SELECT tool_call_id,result_state,public_fields,result_entry_id FROM pulsara_v3.tool_results WHERE session_id=%s ORDER BY tool_call_id",
                    (session.session_id,),
                ).fetchall()
            assert len(known) == 1 and marker.read_text() == "dispatched-once"
            results = [row for row in rows if row["tool_call_id"] == "call:effect"]
            assert len(results) == 1
            assert results[0]["result_state"] == (
                "APPLICATION_ERROR" if failed else "SUCCESS"
            )
            from pulsara_agent.conversation_kernel.tool_outputs import (
                read_tool_result_source_in_connection,
            )
            from pulsara_agent.conversation_kernel.prompt_storage import (
                read_image_reference_in_connection,
            )

            with provider.connection(
                lane=PostgresConnectionLane.INSPECTOR,
                row_factory=dict_row,
                deadline_monotonic=monotonic() + 10,
            ) as conn:
                value = read_tool_result_source_in_connection(
                    conn,
                    session_id=session.session_id,
                    entry_id=results[0]["result_entry_id"],
                ).value
                assert value["meta"]["status"] == ("ERROR" if failed else "SUCCESS")
                if remote_kind == "tool":
                    assert value["structuredContent"] == {
                        "effect": "completed",
                        "count": 1,
                    }
                    assert value["content"][0] == {
                        "type": "text",
                        "value": "actual effect completed",
                    }
                    assert value["content"][-1] == {
                        "type": "text",
                        "value": "remaining business text",
                    }
                    if outcome_kind == "image_error":
                        assert [part["type"] for part in value["content"]] == [
                            "text",
                            "image",
                            "text",
                            "text",
                        ]
                        assert "未交付实际图片" in value["content"][2]["value"]
                if outcome_kind == "shutdown":
                    assert not any(part["type"] == "image" for part in value["content"])
                    assert any(
                        "validator is closed" in part["value"]
                        and "未交付实际图片" in part["value"]
                        for part in value["content"]
                    )
                elif has_images:
                    images = [
                        part for part in value["content"] if part["type"] == "image"
                    ]
                    assert len(images) == 1
                    saved_image = read_image_reference_in_connection(
                        conn, image_ref=images[0]["value"]["image_ref"]
                    )
                    assert saved_image.immutable_bytes == image.getvalue()
                else:
                    assert [part["type"] for part in value["content"]] == [
                        "text",
                        "text",
                    ]
            assert len(outcome) == 1
        finally:
            release.set()
            if running and not running.done():
                running.cancel()
            if running:
                await asyncio.gather(running, return_exceptions=True)
            if closing:
                await asyncio.gather(closing, return_exceptions=True)
            await core.shutdown()

    asyncio.run(scenario())
