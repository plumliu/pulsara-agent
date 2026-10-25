"""A finite TLS workload for the production Electron BackendSession smoke."""
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

root = Path(tempfile.mkdtemp(prefix='.pulsara-session-smoke-', dir=Path.home()))
lock = threading.RLock()
active = {}


def emit(event, **facts):
    with lock:
        print(json.dumps(dict(event=event, **facts)), flush=True)


class Handler(BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.1'

    def log_message(self, *args):
        pass

    def do_GET(self):
        token = str(id(self))
        kind = self.path
        with lock:
            active[token] = kind
        try:
            if kind == '/pending':
                self.connection.settimeout(.1)
                while True:
                    try:
                        if not self.connection.recv(1):
                            return
                    except TimeoutError:
                        continue
            self.send_response(200)
            if kind == '/':
                body = b'<meta charset="utf-8"><title>BackendSession smoke</title>'
                self.send_header('Content-Length', str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            self.send_header('Content-Type', 'text/event-stream' if kind == '/sse' else 'application/octet-stream')
            if kind == '/download':
                self.send_header('Content-Disposition', 'attachment; filename="session-smoke.bin"')
                self.send_header('Content-Length', str(64 * 1024 * 1024))
            self.end_headers()
            count = 0
            while True:
                self.wfile.write(b'data: same live guest\n\n' if kind == '/sse' else b'N' * 65536)
                self.wfile.flush()
                count += 65536
                if kind != '/sse' and count >= 64 * 1024 * 1024:
                    return
                time.sleep(.03)
        except (BrokenPipeError, ConnectionResetError, ssl.SSLError):
            pass
        finally:
            with lock:
                active.pop(token, None)
            emit('handler-closed', path=kind)


try:
    key, cert = root / 'key.pem', root / 'cert.pem'
    subprocess.run(['openssl', 'req', '-x509', '-newkey', 'rsa:2048', '-nodes', '-days', '1', '-subj', '/CN=127.0.0.1', '-keyout', str(key), '-out', str(cert)], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(cert, key)
    public = cert.read_text()
    key.unlink()
    cert.unlink()
    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    server.socket = context.wrap_socket(server.socket, server_side=True)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    emit('ready', origin=f'https://127.0.0.1:{server.server_port}', certificatePem=public, pid=os.getpid(), starttime=Path('/proc/self/stat').read_text().rsplit(')',1)[1].split()[19], home=str(root))
    for raw in sys.stdin:
        if raw.strip() == 'stop':
            break
        with lock:
            emit('inspect', active=list(active.values()))
    server.shutdown()
    server.server_close()
    thread.join()
    with lock:
        assert not active, active
finally:
    shutil.rmtree(root)
    emit('cleaned', exists=root.exists())
