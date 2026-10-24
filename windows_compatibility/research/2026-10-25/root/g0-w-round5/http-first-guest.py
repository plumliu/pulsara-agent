"""Real production HTTP/import/preview owners, with output-only TLS site injection."""

import asyncio
import base64
import functools
import json
import os
from pathlib import Path
import ssl
import subprocess
import sys
import tempfile
from types import SimpleNamespace
from unittest.mock import patch

from aiohttp import web

REPO = Path("/home/plumwsl/workspaces/pulsara_agent")
sys.path[:0] = [str(REPO), str(REPO / "src")]
os.umask(0o077)
HOME = Path(tempfile.mkdtemp(prefix=".g0-http-", dir=Path.home()))
os.environ["PULSARA_HOME"] = str(HOME)
from pulsara_agent.desktop_app.renderer_bridge import RendererBridge  # noqa: E402 -- preserve executed research snapshot
from pulsara_agent.desktop_app.http_server import LocalHttpServer  # noqa: E402 -- preserve executed research snapshot
from tests.test_desktop_http_surface import _model_server_dependencies  # noqa: E402 -- preserve executed research snapshot


def emit(event, **values):
    print(json.dumps({"event": event, **values}, ensure_ascii=False), flush=True)


class TlsServer(LocalHttpServer):
    @property
    def origin(self):
        return f"https://127.0.0.1:{self.port}"


async def main():
    workspace = HOME / "workspace"
    workspace.mkdir()
    static = HOME / "static"
    static.mkdir()
    (static / "index.html").write_text(
        '<!doctype html><meta charset="utf-8"><title>G0 production HTTP owners</title><h1>Windows Electron / Linux production HTTP owners</h1><div id="results"></div>',
        encoding="utf-8",
    )
    (workspace / "中文.txt").write_text("中文预览 / Windows WSL2\n", encoding="utf-8")
    (workspace / "index.html").write_text(
        """<!doctype html><meta charset="utf-8"><title>隔离 HTML</title><h1>隔离 HTML 中文</h1><script>parent.postMessage({g0:true,node:typeof require,bridge:typeof pulsaraDesktop},'*');fetch('./data.json').then(r=>r.json()).then(v=>parent.postMessage({g0data:v},'*'))</script>""",
        encoding="utf-8",
    )
    (workspace / "data.json").write_text('{"value":"中文资源"}', encoding="utf-8")
    (workspace / "image.png").write_bytes(
        base64.b64decode(
            "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+aElQAAAAASUVORK5CYII="
        )
    )
    # A small valid PDF, including exact byte offsets in its standard xref table.
    stream = b"BT /F1 20 Tf 50 700 Td (G0 Windows WSL2 PDF) Tj ET"
    objects = [
        b"<</Type /Catalog /Pages 2 0 R>>",
        b"<</Type /Pages /Kids [3 0 R] /Count 1>>",
        b"<</Type /Page /Parent 2 0 R /MediaBox [0 0 595 842] /Resources <</Font <</F1 4 0 R>>>> /Contents 5 0 R>>",
        b"<</Type /Font /Subtype /Type1 /BaseFont /Helvetica>>",
        b"<</Length "
        + str(len(stream)).encode()
        + b">>\nstream\n"
        + stream
        + b"\nendstream",
    ]
    pdf = b"%PDF-1.4\n"
    offsets = []
    for n, obj in enumerate(objects, 1):
        offsets.append(len(pdf))
        pdf += f"{n} 0 obj\n".encode() + obj + b"\nendobj\n"
    xref = len(pdf)
    pdf += (
        b"xref\n0 6\n0000000000 65535 f \n"
        + b"".join(f"{offset:010} 00000 n \n".encode() for offset in offsets)
        + f"trailer\n<</Size 6 /Root 1 0 R>>\nstartxref\n{xref}\n%%EOF\n".encode()
    )
    (workspace / "sample.pdf").write_bytes(pdf)

    async def availability(_session):
        return {"path": str(workspace), "outcome": "AVAILABLE"}

    async def summary(_session, **_kwargs):
        return SimpleNamespace(workspace_root=str(workspace))

    sessions = SimpleNamespace(
        workspace_availability=availability,
        core=SimpleNamespace(read_resumable_session=summary),
        memory_domain_id="g0",
        _lock=asyncio.Lock(),
        _operations={},
    )
    bridge = RendererBridge(sessions=sessions, protocol_server=object())
    sid = "session:tpsqggcbte7fwanzpf642ljwli"
    for name, role, generation in [
        ("owner", "controller", 7),
        ("observer", "observer", 8),
    ]:
        bridge._connections[name] = SimpleNamespace(
            connection_id=name,
            session_id=sid,
            role=role,
            generation=generation,
            is_open=True,
        )
    bridge._controller_by_session[sid] = "owner"
    server = TlsServer(
        sessions=sessions,
        bridge=bridge,
        static_root=static,
        requested_port=0,
        is_ready=lambda: True,
        is_draining=lambda: False,
        **_model_server_dependencies(),
    )
    key, cert = HOME / "key.pem", HOME / "cert.pem"
    subprocess.run(
        [
            "openssl",
            "req",
            "-x509",
            "-newkey",
            "rsa:2048",
            "-nodes",
            "-days",
            "1",
            "-subj",
            "/CN=127.0.0.1",
            "-keyout",
            str(key),
            "-out",
            str(cert),
        ],
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(cert, key)
    public = cert.read_text()
    key.unlink()
    cert.unlink()
    with patch.object(
        web, "TCPSite", functools.partial(web.TCPSite, ssl_context=context)
    ):
        await server.start()
    emit(
        "ready",
        origin=server.origin,
        certificate=public,
        home=str(HOME),
        workspace=str(workspace),
        uid=os.getuid(),
        pid=os.getpid(),
        owners=[
            "LocalHttpServer",
            "RendererBridge",
            "LocalImport",
            "FilePreviewHttp",
            "FilePreviews",
        ],
    )
    while raw := await asyncio.to_thread(sys.stdin.buffer.readline):
        value = json.loads(raw)
        if value["op"] == "stop":
            break
        if value["op"] == "revoke":
            bridge.file_previews.revoke("owner")
            emit(
                "revoked",
                slots=len(bridge.file_previews.slots),
                pending=len(bridge.file_previews.pending),
            )
        elif value["op"] == "replace":
            p = workspace / "中文.txt"
            p.rename(workspace / "original.txt")
            p.write_text("replacement", encoding="utf-8")
            emit("replaced")
        elif value["op"] == "inspect":
            imported = []
            for p in (HOME / "imports").glob("*/*"):
                imported.append(
                    {
                        "name": p.name,
                        "bytes": len(p.read_bytes()),
                        "contentBase64": base64.b64encode(p.read_bytes()).decode(),
                        "mode": oct(p.stat().st_mode & 0o777),
                        "uid": p.stat().st_uid,
                    }
                )
            emit(
                "inspect",
                imports=imported,
                stages=[p.name for p in (HOME / "imports").glob(".import-*")],
                slots=len(bridge.file_previews.slots),
                pending=len(bridge.file_previews.pending),
            )
    for name in tuple(bridge.file_previews.slots):
        bridge.file_previews.revoke(name)
    await server.aclose()
    emit(
        "stopped",
        slots=len(bridge.file_previews.slots),
        pending=len(bridge.file_previews.pending),
        home=str(HOME),
    )


asyncio.run(main())
