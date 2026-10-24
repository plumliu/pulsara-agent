"""Disposable stdlib TLS fixture; not a Pulsara server or production adapter."""
import atexit
import base64
import json
import os
from pathlib import Path
import shutil
import ssl
import subprocess
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

os.umask(0o077)
root = Path(tempfile.mkdtemp(prefix="pulsara-g0-tls-"))
atexit.register(lambda: shutil.rmtree(root, ignore_errors=True))
contexts, certificates = {}, {}
for kind in ("valid", "other", "expired"):
    key, csr, cert = [root / (kind + suffix) for suffix in (".key", ".csr", ".crt")]
    subprocess.run(["openssl", "req", "-new", "-newkey", "rsa:2048", "-nodes", "-keyout", str(key), "-out", str(csr), "-subj", "/CN=127.0.0.1"], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    validity = ["-not_before", "20200101000000Z", "-not_after", "20200102000000Z"] if kind == "expired" else ["-days", "1"]
    subprocess.run(["openssl", "x509", "-req", "-in", str(csr), "-signkey", str(key), "-out", str(cert), *validity], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(cert, key)
    contexts[kind], certificates[kind] = context, cert.read_text()
shutil.rmtree(root)
server, thread, kind, port = None, None, "valid", 0

def emit(value):
    print(json.dumps(value, ensure_ascii=False), flush=True)

class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.0"

    def log_message(self, *_):
        pass

    def do_POST(self):
        self.do_GET()

    def do_GET(self):
        origin = f"https://127.0.0.1:{port}"
        headers = {name: self.headers.get(name) for name in ("Host", "Origin", "Sec-Fetch-Site")}
        emit({"event": "request", "kind": self.server.kind, "path": self.path, "method": self.command, "headers": headers})
        status, content_type, body = 200, "text/plain; charset=utf-8", (self.server.kind + ":中文 TLS fixture").encode()
        if headers["Host"] != f"127.0.0.1:{port}":
            status, body = 421, b"HOST_REJECTED"
        elif self.path.startswith("/api/") and self.command not in ("GET", "HEAD", "OPTIONS") and headers["Origin"] not in (None, origin):
            status, body = 403, b"ORIGIN_REJECTED"
        elif self.path.startswith("/api/") and headers["Sec-Fetch-Site"] not in (None, "none", "same-origin"):
            status, body = 403, b"FETCH_SITE_REJECTED"
        elif self.path == "/":
            content_type, body = "text/html; charset=utf-8", b"<!doctype html><title>G0 TLS fixture</title><main>fixture</main>"
        elif self.path == "/preview":
            content_type, body = "text/html; charset=utf-8", b"<!doctype html><title>isolated preview</title><main>preview</main>"
        elif self.path == "/image":
            content_type = "image/png"
            body = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+aElQAAAAASUVORK5CYII=")
        elif self.path == "/pdf":
            content_type, body = "application/pdf", b"%PDF-1.4\n1 0 obj<</Type/Catalog>>endobj\n%%EOF\n"
        elif self.path == "/download":
            content_type, body = "application/octet-stream", "Windows下载 中文\n".encode() + bytes(range(256))
        elif self.path == "/sse":
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            try:
                for value in ("一", "二"):
                    self.wfile.write(f"data: {value}\n\n".encode())
                    self.wfile.flush()
                    time.sleep(0.1)
            except (BrokenPipeError, ConnectionResetError):
                pass
            return
        emit({"event": "response", "kind": self.server.kind, "path": self.path, "status": status, "body": body.decode("utf-8", errors="replace") if status != 200 else None})
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        if self.path == "/download":
            self.send_header("Content-Disposition", 'attachment; filename="g0.bin"')
        self.end_headers()
        self.wfile.write(body)

def bind(next_kind):
    global server, thread, kind, port
    if server:
        server.shutdown()
        server.server_close()
        thread.join()
    kind = next_kind
    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    server.daemon_threads = True
    server.kind = kind
    port = server.server_address[1]
    server.socket = contexts[kind].wrap_socket(server.socket, server_side=True)
    thread = threading.Thread(target=server.serve_forever)
    thread.start()
    emit({"event": "ready", "kind": kind, "origin": f"https://127.0.0.1:{port}", "certificate": certificates[kind], "pid": os.getpid(), "uid": os.getuid(), "temporary_keys_removed": not root.exists()})

try:
    bind("valid")
    for line in sys.stdin:
        request = json.loads(line)
        if request["op"] == "stop":
            break
        if request["op"] == "rebind":
            bind(request["kind"])
finally:
    if server:
        server.shutdown()
        server.server_close()
        thread.join()
    emit({"event": "stopped", "pid": os.getpid(), "temporary_keys_removed": not root.exists()})
