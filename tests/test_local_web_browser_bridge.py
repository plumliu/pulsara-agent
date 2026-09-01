from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import cast

import pytest

import pulsara_agent.web_app.browser_bridge as browser_bridge_module
from pulsara_agent.terminal_protocol.v3_gateway import TerminalKernelProtocolServer
from pulsara_agent.web_app.browser_bridge import LocalBrowserBridge
from pulsara_agent.web_app.session_controller import LocalSessionController

BROWSER_ONE = "00000000-0000-4000-8000-000000000001"
BROWSER_TWO = "00000000-0000-4000-8000-000000000002"
BROWSER_THREE = "00000000-0000-4000-8000-000000000003"


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
