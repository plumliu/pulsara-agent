from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import cast

import pytest

import pulsara_agent.web_app.browser_bridge as browser_bridge_module
from pulsara_agent.terminal_protocol.generated_v3 import terminal_kernel_v3_pb2 as wire
from pulsara_agent.terminal_protocol.v3_gateway import TerminalKernelProtocolServer
from pulsara_agent.web_app.browser_bridge import LocalBrowserBridge
from pulsara_agent.web_app.protocol_client import _ATTACHED_FIELDS
from pulsara_agent.web_app.session_controller import LocalSessionController

BROWSER_ONE = "00000000-0000-4000-8000-000000000001"
BROWSER_TWO = "00000000-0000-4000-8000-000000000002"
BROWSER_THREE = "00000000-0000-4000-8000-000000000003"


def test_pr03_background_reads_use_attached_protocol_requests() -> None:
    assert "list_background_processes" in _ATTACHED_FIELDS
    assert "read_background_process_log" in _ATTACHED_FIELDS


class _Sessions:
    async def resume_session(self, session_id: str) -> SimpleNamespace:
        return SimpleNamespace(
            session_id=session_id,
            host_session_id=f"host:{session_id}",
        )


class _RuntimeConnection:
    next_id = 0

    def __init__(self, *, session_id: str, generation: int, role: str) -> None:
        type(self).next_id += 1
        self.connection_id = f"connection:{type(self).next_id}"
        self.session_id = session_id
        self.generation = generation
        self.role = role
        self.closed = False
        self.requests: list[tuple[str, object]] = []
        self.controller = self
        self.observer = self
        self.attachment_id = f"attachment:{generation}"

    @classmethod
    async def open(
        cls,
        *,
        server: object,
        session_id: str,
        host_session_id: str,
        generation: int,
        role: str,
    ) -> "_RuntimeConnection":
        assert server is not None
        assert host_session_id == f"host:{session_id}"
        return cls(session_id=session_id, generation=generation, role=role)

    async def aclose(self) -> None:
        self.closed = True

    async def request(self, kind: str, request: object) -> wire.ServerFrame:
        self.requests.append((kind, request))
        request_id = str(getattr(request, "request_id", ""))
        if kind == "command":
            return wire.ServerFrame(
                command_outcome=wire.CommandOutcome(
                    request_id=request_id,
                    command_id=str(getattr(request, "command_id")),
                    status=wire.PENDING,
                )
            )
        if kind == "query_command":
            return wire.ServerFrame(
                query_command=wire.QueryCommandResponse(
                    request_id=request_id,
                    found=False,
                    control_query_status=wire.CONTROL_QUERY_RESULT_UNAVAILABLE,
                )
            )
        if kind == "list_background_processes":
            return wire.ServerFrame(
                background_processes=wire.BackgroundProcessPage(
                    request_id=request_id,
                    session_id=self.session_id,
                    host_session_id=f"host:{self.session_id}",
                )
            )
        if kind == "read_background_process_log":
            return wire.ServerFrame(
                background_process_log=wire.BackgroundProcessLog(
                    request_id=request_id,
                    session_id=self.session_id,
                    host_session_id=f"host:{self.session_id}",
                    output="exact output",
                )
            )
        raise AssertionError(f"unexpected request kind: {kind}")

    @property
    def is_open(self) -> bool:
        return not self.closed


def test_browser_connections_share_one_controller_without_reconnect_ping_pong(
    monkeypatch,
) -> None:
    asyncio.run(_exercise_browser_connection_roles(monkeypatch))


async def _exercise_browser_connection_roles(monkeypatch) -> None:
    _RuntimeConnection.next_id = 0
    monkeypatch.setattr(
        browser_bridge_module,
        "BrowserRuntimeConnection",
        _RuntimeConnection,
    )
    monkeypatch.setattr(
        LocalBrowserBridge,
        "_connection_payload",
        staticmethod(
            lambda connection: {
                "connection_id": connection.connection_id,
                "session_id": connection.session_id,
                "connection_generation": connection.generation,
                "role": connection.role,
            }
        ),
    )
    bridge = LocalBrowserBridge(
        sessions=cast(LocalSessionController, _Sessions()),
        protocol_server=cast(TerminalKernelProtocolServer, object()),
    )

    first = await bridge.connect("session:one", browser_instance_id=BROWSER_ONE)
    second = await bridge.connect("session:one", browser_instance_id=BROWSER_TWO)
    other_session = await bridge.connect("session:two", browser_instance_id=BROWSER_ONE)

    assert first["role"] == "controller"
    assert second["role"] == "observer"
    assert other_session["role"] == "controller"
    first_connection = bridge._connections[str(first["connection_id"])]
    second_connection = bridge._connections[str(second["connection_id"])]
    other_connection = bridge._connections[str(other_session["connection_id"])]
    assert not first_connection.closed
    assert not second_connection.closed
    assert not other_connection.closed
    assert bridge._controller_by_session["session:one"] == first["connection_id"]
    assert (
        bridge._controller_by_session["session:two"] == other_session["connection_id"]
    )

    refreshed = await bridge.connect("session:one", browser_instance_id=BROWSER_ONE)

    assert refreshed["role"] == "controller"
    assert first_connection.closed
    assert not second_connection.closed
    assert not other_connection.closed
    assert bridge._controller_by_session["session:one"] == refreshed["connection_id"]
    await bridge.disconnect(str(first["connection_id"]))
    assert bridge._controller_by_session["session:one"] == refreshed["connection_id"]

    refreshed_connection = bridge._connections[str(refreshed["connection_id"])]
    third = await bridge.connect(
        "session:one",
        browser_instance_id=BROWSER_THREE,
        takeover=True,
    )

    assert third["role"] == "controller"
    assert refreshed_connection.closed
    assert not second_connection.closed
    assert not other_connection.closed
    assert str(second["connection_id"]) in bridge._connections
    assert bridge._controller_by_session["session:one"] == third["connection_id"]
    assert (
        bridge._controller_by_session["session:two"] == other_session["connection_id"]
    )

    await bridge.aclose()


def test_dead_attachment_expires_the_whole_browser_generation(monkeypatch) -> None:
    asyncio.run(_exercise_dead_attachment_expiry(monkeypatch))


async def _exercise_dead_attachment_expiry(monkeypatch) -> None:
    _RuntimeConnection.next_id = 0
    monkeypatch.setattr(
        browser_bridge_module,
        "BrowserRuntimeConnection",
        _RuntimeConnection,
    )
    monkeypatch.setattr(
        LocalBrowserBridge,
        "_connection_payload",
        staticmethod(
            lambda connection: {
                "connection_id": connection.connection_id,
                "session_id": connection.session_id,
                "connection_generation": connection.generation,
                "role": connection.role,
            }
        ),
    )
    bridge = LocalBrowserBridge(
        sessions=cast(LocalSessionController, _Sessions()),
        protocol_server=cast(TerminalKernelProtocolServer, object()),
    )
    payload = await bridge.connect("session:one", browser_instance_id=BROWSER_ONE)
    connection_id = str(payload["connection_id"])
    connection = bridge._connections[connection_id]
    connection.closed = True

    with pytest.raises(browser_bridge_module.ProtocolTransportClosed):
        await bridge._connection(connection_id)

    assert connection_id not in bridge._connections
    assert "session:one" not in bridge._controller_by_session
    replacement = await bridge.connect("session:one", browser_instance_id=BROWSER_ONE)
    assert replacement["role"] == "controller"
    await bridge.aclose()


def test_pr03_browser_bridge_preserves_exact_control_and_background_read_fields(
    monkeypatch,
) -> None:
    asyncio.run(_exercise_pr03_browser_bridge_fields(monkeypatch))


async def _exercise_pr03_browser_bridge_fields(monkeypatch) -> None:
    _RuntimeConnection.next_id = 0
    monkeypatch.setattr(browser_bridge_module, "BrowserRuntimeConnection", _RuntimeConnection)
    monkeypatch.setattr(
        LocalBrowserBridge,
        "_connection_payload",
        staticmethod(
            lambda connection: {
                "connection_id": connection.connection_id,
                "session_id": connection.session_id,
                "connection_generation": connection.generation,
                "role": connection.role,
            }
        ),
    )
    bridge = LocalBrowserBridge(
        sessions=cast(LocalSessionController, _Sessions()),
        protocol_server=cast(TerminalKernelProtocolServer, object()),
    )
    payload = await bridge.connect("session:one", browser_instance_id=BROWSER_ONE)
    connection_id = str(payload["connection_id"])
    connection = bridge._connections[connection_id]
    command_id = (
        "command:control:9999999999999:44444444-4444-4444-8444-444444444444"
    )

    await bridge.command(
        connection_id,
        {
            "command_id": command_id,
            "command_kind": "TERMINATE_BACKGROUND_PROCESS",
            "expected_session_id": "session:one",
            "expected_host_session_id": "host:session:one",
            "target_process_id": "process:exact",
        },
    )
    kind, request = connection.requests[-1]
    assert kind == "command"
    assert isinstance(request, wire.CommandRequest)
    assert request.client_submission_id == ""
    assert request.expected_session_id == "session:one"
    assert request.expected_host_session_id == "host:session:one"
    assert request.target_process_id == "process:exact"
    assert not request.target_turn_id
    assert not request.subagent_task_id

    await bridge.query_command(
        connection_id,
        {
            "command_id": command_id,
            "expected_control": {
                "operation": "USER_CONTROL_TERMINATE_BACKGROUND_PROCESS",
                "session_id": "session:one",
                "host_session_id": "host:session:one",
                "target_kind": "USER_CONTROL_BACKGROUND_PROCESS",
                "target_id": "process:exact",
            },
        },
    )
    kind, query = connection.requests[-1]
    assert kind == "query_command"
    assert isinstance(query, wire.QueryCommandRequest)
    assert query.expected_control.target.target_id == "process:exact"
    assert query.expected_control.host_session_id == "host:session:one"

    await bridge.list_background_processes(
        connection_id,
        {
            "expected_session_id": "session:one",
            "expected_host_session_id": "host:session:one",
            "cursor": "cursor:page",
            "maximum_items": 7,
        },
    )
    kind, listing = connection.requests[-1]
    assert kind == "list_background_processes"
    assert isinstance(listing, wire.ListBackgroundProcessesRequest)
    assert listing.cursor == "cursor:page"
    assert listing.maximum_items == 7

    await bridge.read_background_process_log(
        connection_id,
        {
            "expected_session_id": "session:one",
            "expected_host_session_id": "host:session:one",
            "process_id": "process:exact",
            "output_cursor": "output:17",
            "max_output_chars": 4096,
        },
    )
    kind, log = connection.requests[-1]
    assert kind == "read_background_process_log"
    assert isinstance(log, wire.ReadBackgroundProcessLogRequest)
    assert log.process_id == "process:exact"
    assert log.output_cursor == "output:17"
    assert log.max_output_chars == 4096

    await bridge.aclose()
