from __future__ import annotations

import asyncio
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from threading import Thread

import pytest

from pulsara_agent.llm.connection_probe import (
    ModelConnectionProbeFailure,
    probe_model_connection,
)
from pulsara_agent.llm.model_catalog import ReasoningProviderDefault, WireApi
from pulsara_agent.llm.model_connections import (
    ModelConnectionAuthentication,
    ModelConnectionId,
    ReasoningWireProfile,
    UserDeclaredModelTarget,
)
from pulsara_agent.llm.model_target import create_user_declared_model_connection
from pulsara_agent.llm.route_wires import production_route_wire_registry


class _ProbeHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, _format: str, *_args: object) -> None:
        return

    def do_POST(self) -> None:  # noqa: N802 - stdlib handler API
        size = int(self.headers.get("content-length", "0"))
        body = json.loads(self.rfile.read(size))
        self.server.observed = {  # type: ignore[attr-defined]
            "path": self.path,
            "authorization": self.headers.get("authorization"),
            "body": body,
        }
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Connection", "close")
        self.end_headers()
        chunks = self.server.response_chunks  # type: ignore[attr-defined]
        for delta, finish_reason in (*((chunk, None) for chunk in chunks), ("", "stop")):
            payload = {
                "id": "chatcmpl_probe",
                "object": "chat.completion.chunk",
                "created": 1,
                "model": "local-model",
                "choices": [
                    {
                        "index": 0,
                        "delta": {"content": delta},
                        "finish_reason": finish_reason,
                    }
                ],
            }
            self.wfile.write(f"data: {json.dumps(payload)}\n\n".encode())
        self.wfile.write(b"data: [DONE]\n\n")
        self.wfile.flush()
        self.close_connection = True


@pytest.mark.parametrize(
    ("response_chunks", "succeeds"),
    ((("OK",), True), (("x" * 8,), True), (("x" * 9,), False), (("xxx", "xxx", "xxx"), False)),
)
def test_probe_content_bound_is_independent_of_fragments_and_closes_transport(
    monkeypatch: pytest.MonkeyPatch, response_chunks: tuple[str, ...], succeeds: bool,
) -> None:
    # Probe-only content protection survives removal of transport envelopes;
    # end snapshots are not charged again. Exercise the real SDK/HTTP close.
    monkeypatch.setattr(
        "pulsara_agent.llm.connection_probe.MAX_COMPLETED_PROVIDER_RESPONSE_AGGREGATE_BYTES",
        8,
    )
    server = ThreadingHTTPServer(("127.0.0.1", 0), _ProbeHandler)
    server.observed = None  # type: ignore[attr-defined]
    server.response_chunks = response_chunks  # type: ignore[attr-defined]
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    route_wires = production_route_wire_registry()
    resolved = create_user_declared_model_connection(
        model_id="local-model",
        wire_api=WireApi.OPENAI_CHAT_COMPLETIONS,
        base_url=f"http://127.0.0.1:{server.server_port}/v1",
        declaration=UserDeclaredModelTarget(
            "Local No Auth",
            256_000,
            8_192,
            True,
            ReasoningProviderDefault(),
            ModelConnectionAuthentication.NONE,
        ),
        route_wires=route_wires,
        reasoning_wire_profile=ReasoningWireProfile.PROVIDER_DEFAULT,
        connection_id=ModelConnectionId("model-connection:" + "e" * 32),
    )
    try:
        async def probe():
            await probe_model_connection(
                resolved=resolved,
                catalog=None,
                route_wires=route_wires,
                api_key=None,
            )
        if succeeds:
            asyncio.run(probe())
        else:
            with pytest.raises(ModelConnectionProbeFailure) as failure:
                asyncio.run(probe())
            # A failed physical close would instead report protocol_error.
            assert failure.value.code == "transport_source_payload_limit_exceeded"
        observed = server.observed  # type: ignore[attr-defined]
        assert observed["path"] == "/v1/chat/completions"
        assert observed["authorization"] is None
        assert observed["body"]["model"] == "local-model"
        assert observed["body"]["max_completion_tokens"] == 64
    finally:
        server.shutdown()
        server.server_close()
        thread.join()
