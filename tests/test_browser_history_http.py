"""Browser history HTTP uses cold authorization, exact cuts and byte budgets."""

import asyncio
from time import monotonic
from types import SimpleNamespace
from unittest.mock import AsyncMock

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from pulsara_agent.terminal_protocol.canonical_v3 import (
    CanonicalProtocolGap, CanonicalProtocolResourceExhausted,
)
from pulsara_agent.terminal_protocol.generated_v3 import terminal_kernel_v3_pb2 as wire
from pulsara_agent.desktop_app.http_server import LocalHttpServer
from pulsara_agent.desktop_app.session_controller import LocalSessionController
from pulsara_agent.desktop_app import session_controller as controller_module


def test_cold_history_http_precise_budget_authorization_and_resource_outcomes(monkeypatch):
    calls = []

    class Reader:
        def __init__(self, provider):
            assert provider is sentinel

        def authorize_session(self, **kwargs):
            calls.append(("authorize", kwargs))
            assert kwargs["memory_domain_id"] == "test-domain"
            if kwargs["session_id"] == "denied":
                raise KeyError("denied")

        def history_page(self, **kwargs):
            calls.append(("history", kwargs))
            return wire.HistoryPageResponse()

        def browser_history_context(self, **kwargs):
            calls.append(("context", kwargs))
            if kwargs["entry_ids"] == ["large"]:
                raise CanonicalProtocolResourceExhausted("context cannot fit")
            if kwargs["entry_ids"] == ["ahead"]:
                raise CanonicalProtocolGap("target exceeds cut")
            return {"entry_ids": kwargs["entry_ids"]}

        def browser_history_window(self, **kwargs):
            calls.append(("window", kwargs))
            return {"anchor_entry_id": kwargs["anchor_entry_id"]}

    sentinel = object()
    monkeypatch.setattr(controller_module, "CanonicalProtocolReader", Reader)

    async def exercise():
        controller = object.__new__(LocalSessionController)
        controller.memory_domain_id = "test-domain"
        controller.core = SimpleNamespace(
            _ensure_resources=AsyncMock(return_value=SimpleNamespace(connection_provider=sentinel)),
            _canonical_deadline=lambda: monotonic() + 30,
        )
        surface = object.__new__(LocalHttpServer)
        surface.sessions = controller
        surface.bridge = SimpleNamespace(require_chat_controller=AsyncMock(side_effect=AssertionError("history must not require attachment")))
        app = web.Application(middlewares=[surface._errors, surface._chat_write_admission])
        for operation in ("history", "window", "context"):
            app.router.add_post("/api/sessions/{session_id}/history/" + operation, surface._cold_history_action)
        async with TestClient(TestServer(app)) as client:
            base = "/api/sessions/current/history/"
            for event_cut in ("0", str(2**53 + 3)):
                cursor = {"session_id": "current", "cut_sequence": str(2**53 + 1),
                          "entry_sequence": str(2**53), "event_sequence_cut": event_cut}
                response = await client.post(base + "history", json={"cursor": cursor, "maximum_entries": 7, "maximum_serialized_bytes": 2048})
                assert response.status == 200
                sent = calls[-1][1]
                assert sent["event_sequence_cut"] == int(event_cut)
                assert sent["cut_sequence"] == 2**53 + 1
                assert sent["before_entry_sequence"] == 2**53
                assert sent["maximum_serialized_bytes"] == 2048
                assert sent["maximum_entries"] == 7
            response = await client.post(base + "context", json={"entry_ids": ["one"], "cut_sequence": "2", "event_sequence_cut": "0", "maximum_serialized_bytes": 4096})
            assert response.status == 200
            assert calls[-1][1]["event_sequence_cut"] == 0
            assert calls[-1][1]["maximum_serialized_bytes"] == 4096
            response = await client.post(base + "context", json={"entry_ids": ["one"]})
            assert response.status == 200
            assert "event_sequence_cut" not in calls[-1][1]  # Let the reader choose the paired current cut.
            response = await client.post(base + "context", json={"entry_ids": [], "boundary_event_ids": ["event-one", "event-two"]})
            assert response.status == 200
            assert calls[-1][1]["boundary_event_ids"] == ["event-one", "event-two"]
            for invalid_ids in (None, "event-one", 7):
                response = await client.post(base + "context", json={"entry_ids": [], "boundary_event_ids": invalid_ids})
                assert response.status == 400
            response = await client.post(base + "window", json={"anchor_entry_id": "one"})
            assert response.status == 200
            assert await response.json() == {"anchor_entry_id": "one"}
            for body in ({"entry_ids": [], "cut_sequence": "1"}, {"entry_ids": [], "cut_sequence": "1", "event_sequence_cut": None}):
                response = await client.post(base + "context", json=body)
                assert response.status == 400
            for entry, status, code in (("large", 413, "RESOURCE_EXHAUSTED"), ("ahead", 409, "HISTORY_CUT_CONFLICT")):
                response = await client.post(base + "context", json={"entry_ids": [entry]})
                assert response.status == status
                assert (await response.json())["error"]["code"] == code
            prior_reads = len([call for call in calls if call[0] != "authorize"])
            response = await client.post("/api/sessions/denied/history/context", json={"entry_ids": ["one"]})
            assert response.status == 404
            assert len([call for call in calls if call[0] != "authorize"]) == prior_reads
        surface.bridge.require_chat_controller.assert_not_awaited()
        for index, (kind, _) in enumerate(calls):
            if kind != "authorize":
                assert calls[index - 1][0] == "authorize"

    asyncio.run(exercise())
