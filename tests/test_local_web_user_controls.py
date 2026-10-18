"""Frontend control bodies traverse HTTP, bridge, gateway, and Host guards."""
from __future__ import annotations

import asyncio
from typing import cast

from aiohttp import ClientSession, DummyCookieJar
import pytest

from pulsara_agent.identity import derive_id
from pulsara_agent.terminal_protocol.generated_v3 import terminal_kernel_v3_pb2 as wire
from pulsara_agent.web_app.browser_bridge import LocalBrowserBridge
from pulsara_agent.web_app.http_server import LocalHttpServer
from pulsara_agent.web_app.session_controller import LocalSessionController
from tests.test_local_web_browser_bridge import _RuntimeConnection, _Sessions
from tests.test_local_web_http_surface import _model_server_dependencies
from tests.test_pr03_user_controls import _Clock, _host
from tests.test_stage2_protocol_v3 import _server, _state


@pytest.mark.parametrize("action", ["STOP_ACTIVE_TURN", "SUBMIT_PROMPT", "import-file"])
def test_observer_writes_report_chat_control_ownership_before_dispatch(tmp_path, action):
    async def exercise():
        bridge = LocalBrowserBridge(
            sessions=cast(LocalSessionController, _Sessions()), protocol_server=_server(),
        )
        observer = _RuntimeConnection(
            session_id="session:5dyf7lfzp7z22pphjtostkgsjm", generation=1, role="observer",
        )
        bridge._connections[observer.connection_id] = observer
        (tmp_path / "index.html").write_text("Pulsara", encoding="utf-8")
        server = LocalHttpServer(
            sessions=bridge.sessions, bridge=bridge, static_root=tmp_path,
            requested_port=0, is_ready=lambda: True, is_draining=lambda: False,
            **_model_server_dependencies(),
        )
        body = {
            "command_id": derive_id("command", "observer-write", action),
            "command_kind": action,
        }
        if action == "STOP_ACTIVE_TURN":
            body.update(
                expected_session_id=observer.session_id,
                expected_host_session_id="host:7r7elstdigup6xfbo3hd3d5jfu",
                target_turn_id="turn:74y74rvr2pht5lydgxvy57nxgq",
                control_admission_deadline_ms=9000,
            )
        elif action == "SUBMIT_PROMPT":
            body["prompt_content"] = {"parts": [{"type": "text", "text": "Prepare handoff"}]}
        await server.start()
        try:
            async with ClientSession(cookie_jar=DummyCookieJar()) as client:
                suffix = "import-file" if action == "import-file" else "command"
                url = f"{server.origin}/api/connections/{observer.connection_id}/{suffix}"
                async with client.post(
                    url, json=body,
                    headers={"X-Pulsara-Connection-Generation": str(observer.generation)},
                ) as response:
                    result = await response.json()
                    assert response.status == 409
                    assert result["error"]["code"] == "CHAT_CONTROLLER_REQUIRED"
                    assert result["error"]["message"] == "当前窗口没有会话控制权，请重新连接。"
                assert observer.requests == []
        finally:
            await server.aclose()
            await bridge.aclose()

    asyncio.run(exercise())


@pytest.mark.parametrize(
    ("kind", "target_field", "target_id", "record_name", "missing_target_code"),
    [
        ("STOP_ACTIVE_TURN", "target_turn_id", "turn:74y74rvr2pht5lydgxvy57nxgq", "stop_requests", "STOP_REQUEST_INVALID"),
        ("CANCEL_SUBAGENT_TASK", "subagent_task_id", "task:r3npg4w7oybo2df2s2il3y5nay", "cancel_requests", "SUBAGENT_CANCEL_REQUEST_INVALID"),
        ("TERMINATE_BACKGROUND_PROCESS", "target_process_id", "process:ntcpufb7iult4c2nzqn4ksxvc4", "terminate_requests", "PROCESS_CONTROL_REQUEST_INVALID"),
    ],
)
def test_frontend_controls_cross_http_bridge_without_bypassing_host_guards(
    tmp_path, kind, target_field, target_id, record_name, missing_target_code,
):
    async def exercise():
        gateway = _server()
        state = _state(role=wire.ATTACHMENT_ROLE_CONTROLLER)
        command_host = state.host_session
        clock = _Clock()
        guarded_host = _host(clock)
        guarded_host.session_id = command_host.session_id
        guarded_host.host_session_id = command_host.host_session_id
        guarded_host.workspace_gate = command_host.workspace_gate
        guarded_host.has_controller_attachment = command_host.has_controller_attachment
        deadline = guarded_host.control_admission_deadline_ms()
        sessions = _Sessions()
        sessions._lock = asyncio.Lock()
        sessions._operations = {}

        async def workspace_availability(_session_id):
            return {"path": str(tmp_path)}

        sessions.workspace_availability = workspace_availability
        bridge = LocalBrowserBridge(
            sessions=cast(LocalSessionController, sessions),
            protocol_server=gateway,
        )
        connection = _RuntimeConnection(
            session_id=command_host.session_id, generation=1, role="controller",
        )
        bridge._connections[connection.connection_id] = connection
        bridge._controller_by_session[connection.session_id] = connection.connection_id

        async def dispatch(operation, request):
            # Replace only the socket transport; retain production bridge decoding,
            # gateway dispatch/target validation, and actual Host admission guards.
            assert operation == "command"
            connection.requests.append((operation, request))
            return await gateway._command(state, request)

        connection.request = dispatch
        (tmp_path / "index.html").write_text("Pulsara", encoding="utf-8")
        server = LocalHttpServer(
            sessions=bridge.sessions,
            bridge=bridge,
            static_root=tmp_path,
            requested_port=0,
            is_ready=lambda: True,
            is_draining=lambda: False,
            **_model_server_dependencies(),
        )
        body = {
            "command_id": derive_id("command", "http-user-control", kind),
            "command_kind": kind,
            "client_submission_id": "",
            "expected_session_id": command_host.session_id,
            "expected_host_session_id": command_host.host_session_id,
            "control_admission_deadline_ms": deadline,
            target_field: target_id,
        }
        await server.start()
        try:
            async with ClientSession(cookie_jar=DummyCookieJar()) as client:
                url = f"{server.origin}/api/connections/{connection.connection_id}/command"

                async def send(payload, status=200):
                    async with client.post(url, json=payload) as response:
                        result = await response.json()
                        assert response.status == status, result
                        return result

                response = await send(body)
                assert response["command_outcome"]["status"] == "PENDING"
                assert response["command_outcome"]["user_control"]["target"]["target_id"] == target_id
                assert len(connection.requests) == 1
                request = connection.requests[0][1]
                assert request.control_admission_deadline_ms == deadline
                assert request.client_submission_id == ""
                assert request.expected_session_id == command_host.session_id
                assert request.expected_host_session_id == command_host.host_session_id
                assert getattr(request, target_field) == target_id
                accepted = getattr(command_host, record_name)
                assert len(accepted) == 1
                assert accepted[0]["admission_deadline_ms"] == deadline

                # A valid frontend deadline reaches the real Host; it must not
                # become a bypass for an expired deadline or a different owner.
                state.host_session = guarded_host
                for changed, code in [
                    ({"control_admission_deadline_ms": int(clock.seconds * 1000) - 1}, "CONTROL_REQUEST_EXPIRED"),
                    ({"expected_host_session_id": "host:r77neyfksldterpdnoirnf7zxu"}, "OWNER_UNAVAILABLE"),
                    ({"expected_session_id": "session:5dyf7lfzp7z22pphjtostkgsjm"}, "OWNER_UNAVAILABLE"),
                ]:
                    response = await send(body | changed)
                    assert response["command_outcome"]["status"] == "REJECTED"
                    assert response["command_outcome"]["public_code"] == code
                    assert guarded_host._user_control_attempts == {}

                response = await send(body | {target_field: ""})
                assert response["error"]["stable_code"] == missing_target_code
                assert guarded_host._user_control_attempts == {}

                count = len(connection.requests)
                for bad_deadline in (-1, True, "9000"):
                    response = await send(body | {"control_admission_deadline_ms": bad_deadline}, status=400)
                    assert response["error"]["code"] == "BAD_REQUEST"
                response = await send(body | {"unknown_control_field": True}, status=400)
                assert response["error"]["code"] == "BAD_REQUEST"
                assert len(connection.requests) == count
        finally:
            await server.aclose()
            await bridge.aclose()

    asyncio.run(exercise())
