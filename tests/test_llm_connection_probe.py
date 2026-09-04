from __future__ import annotations

import asyncio
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from threading import Thread

from pulsara_agent.llm.connection_probe import probe_model_connection
from pulsara_agent.llm.model_catalog import ReasoningProviderDefault, WireApi
from pulsara_agent.llm.model_connections import (
    ModelConnectionAuthentication,
    ModelConnectionId,
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
        for delta, finish_reason in (("OK", None), ("", "stop")):
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


def test_no_auth_probe_uses_generic_chat_without_authorization_header() -> None:
    server = ThreadingHTTPServer(("127.0.0.1", 0), _ProbeHandler)
    server.observed = None  # type: ignore[attr-defined]
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
        connection_id=ModelConnectionId("model-connection:" + "e" * 32),
    )
    try:
        asyncio.run(
            probe_model_connection(
                resolved=resolved,
                catalog=None,
                route_wires=route_wires,
                api_key=None,
            )
        )
        observed = server.observed  # type: ignore[attr-defined]
        assert observed["path"] == "/v1/chat/completions"
        assert observed["authorization"] is None
        assert observed["body"]["model"] == "local-model"
        assert observed["body"]["max_completion_tokens"] == 64
    finally:
        server.shutdown()
        server.server_close()
        thread.join()
