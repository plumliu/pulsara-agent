"""Windows client to the live G0 GUI's Linux service; no production mutation."""

import http.client
import json
from pathlib import Path
import socket
import ssl
import subprocess
import time
from urllib.parse import urlsplit

OUT = Path(__file__).parent
ready = next(
    x
    for x in json.loads((OUT / "files-results.json").read_text(encoding="utf-8"))[
        "events"
    ]
    if x["event"] == "ready"
)
endpoint = urlsplit(ready["origin"])
certificate = ssl.PEM_cert_to_DER_cert(ready["certificate"])
context = ssl.create_default_context(cadata=ready["certificate"])
context.check_hostname = (
    False  # Exact full DER is checked before sending any HTTP bytes.
)
ROOT = "/home/plumwsl/workspaces/中文 工程 G0-R5"
results = []


def connection():
    conn = http.client.HTTPSConnection(
        endpoint.hostname, endpoint.port, context=context, timeout=20
    )
    conn.connect()
    assert conn.sock.getpeercert(binary_form=True) == certificate
    return conn


def call(value):
    conn = connection()
    try:
        conn.request(
            "POST",
            "/directories",
            json.dumps(value).encode(),
            {"Content-Type": "application/json", "Origin": ready["origin"]},
        )
        reply = conn.getresponse()
        return reply.status, json.loads(reply.read())
    finally:
        conn.close()


def linux(code):
    p = subprocess.run(
        [
            "C:/Windows/System32/wsl.exe",
            "-d",
            "Ubuntu",
            "-u",
            "plumwsl",
            "--exec",
            "/home/plumwsl/workspaces/pulsara_agent/.venv/bin/python",
            "-c",
            code,
        ],
        capture_output=True,
    )
    assert p.returncode == 0, p.stderr.decode("utf-8", errors="replace")
    return p.stdout.decode()


setup = f"""from pathlib import Path
import os,json
r=Path({ROOT!r}); assert r.is_dir() and not r.is_symlink()
(r/'symlink-ext4').symlink_to('/tmp',target_is_directory=True)
(r/'symlink-drvfs').symlink_to('/mnt/c',target_is_directory=True)
(r/'no-access').mkdir(mode=0)
(r/'duplicate').mkdir()
print(json.dumps({{'fixture':str(r),'uid':os.getuid()}}))
"""
results.append({"fixture": json.loads(linux(setup))})
cases = [
    ("canonical", {"op": "select", "path": ROOT}, 200),
    (
        "UNC-localhost",
        {"op": "select", "path": "\\\\wsl.localhost\\Ubuntu" + ROOT.replace("/", "\\")},
        200,
    ),
    (
        "UNC-dollar",
        {"op": "select", "path": "\\\\wsl$\\Ubuntu" + ROOT.replace("/", "\\")},
        200,
    ),
    (
        "wrong-distro",
        {"op": "select", "path": "\\\\wsl.localhost\\Other" + ROOT.replace("/", "\\")},
        400,
    ),
    ("windows-drive", {"op": "select", "path": "C:\\python_workspace"}, 400),
    ("network-share", {"op": "select", "path": "\\\\server\\share"}, 400),
    ("drvfs", {"op": "select", "path": "/mnt/c"}, 400),
    ("escape", {"op": "select", "path": ROOT + "/../pulsara_agent"}, 400),
    ("ext4-symlink", {"op": "select", "path": ROOT + "/symlink-ext4"}, 400),
    ("drvfs-symlink", {"op": "select", "path": ROOT + "/symlink-drvfs"}, 400),
    ("permissions", {"op": "select", "path": ROOT + "/no-access"}, 400),
    ("missing", {"op": "select", "path": ROOT + "/does-not-exist"}, 400),
    ("duplicate", {"op": "mkdir", "path": ROOT, "name": "duplicate"}, 400),
    ("separator", {"op": "mkdir", "path": ROOT, "name": "a/b"}, 400),
    ("reserved", {"op": "mkdir", "path": ROOT, "name": "CON.txt"}, 400),
]
for name, value, expected in cases:
    status, reply = call(value)
    results.append(
        {"name": name, "status": status, "reply": reply, "pass": status == expected}
    )
    assert status == expected, (name, status, reply)

# Full side-effect request, then deliberately discard response. Never replay mkdir.
conn = connection()
payload = json.dumps({"op": "mkdir", "path": ROOT, "name": "lost-reply"}).encode()
conn.request(
    "POST",
    "/directories",
    payload,
    {"Content-Type": "application/json", "Origin": ready["origin"]},
)
conn.close()
deadline = (
    time.monotonic() + 10
)  # Experiment observation deadline, not product lifetime.
while True:
    status, observed = call({"op": "list", "path": ROOT})
    if "lost-reply" in observed.get("names", []):
        break
    assert time.monotonic() < deadline
    time.sleep(0.05)
results.append(
    {
        "name": "mkdir-response-discarded",
        "pass": True,
        "callerOutcome": "unknown",
        "refreshedDirectoryPresent": True,
        "replayed": False,
    }
)

# A partial real upload is disconnected before the declared body has arrived.
conn = connection()
conn.putrequest("POST", "/import")
conn.putheader("Content-Type", "application/octet-stream")
conn.putheader("X-Pulsara-Filename", json.dumps("partial.bin"))
conn.putheader("Content-Length", "4096")
conn.endheaders()
conn.send(b"partial-body")
conn.sock.shutdown(socket.SHUT_RDWR)
conn.close()
time.sleep(0.3)
observation = json.loads(
    linux(
        f"from pathlib import Path; import json; p=Path({ready['home']!r})/'imports'; print(json.dumps({{'stages':[x.name for x in p.glob('.import-*')],'partialFiles':[str(x) for x in p.glob('*/partial.bin')]}}))"
    )
)
assert observation == {"stages": [], "partialFiles": []}, observation
results.append({"name": "partial-upload-disconnect", "pass": True, **observation})
(OUT / "files-matrix.json").write_text(
    json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8"
)
print(
    json.dumps({"passed": sum(x.get("pass", False) for x in results), "fixture": ROOT})
)
