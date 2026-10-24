"""Final candidate HTTP acceptance/ambiguity checks, independent of the earlier GUI evidence."""

import http.client
import json
from pathlib import Path
import ssl
import subprocess
from urllib.parse import urlsplit

out = Path(__file__).parent
wsl = [
    "C:/Windows/System32/wsl.exe",
    "-d",
    "Ubuntu",
    "-u",
    "plumwsl",
    "--cd",
    "/home/plumwsl/workspaces/pulsara_agent",
    "--exec",
    "/home/plumwsl/workspaces/pulsara_agent/.venv/bin/python",
]
stderr = (out / "directory-final.stderr.log").open("wb")
child = subprocess.Popen(
    wsl + ["-u", "-c", (out / "files_guest.py").read_text(encoding="utf-8")],
    stdin=subprocess.PIPE,
    stdout=subprocess.PIPE,
    stderr=stderr,
)
ready = json.loads(child.stdout.readline())
assert ready["event"] == "ready"
root = (
    subprocess.check_output(
        wsl
        + [
            "-c",
            'import tempfile;print(tempfile.mkdtemp(prefix="g0-directory-final-",dir="/home/plumwsl/workspaces"))',
        ]
    )
    .decode()
    .strip()
)
ctx = ssl.create_default_context(cadata=ready["certificate"])
ctx.check_hostname = False
cert = ssl.PEM_cert_to_DER_cert(ready["certificate"])
endpoint = urlsplit(ready["origin"])
results = []


def call(value):
    conn = http.client.HTTPSConnection(
        endpoint.hostname, endpoint.port, context=ctx, timeout=15
    )
    try:
        conn.connect()
        assert conn.sock.getpeercert(binary_form=True) == cert
        conn.request(
            "POST",
            "/directories",
            json.dumps(value).encode(),
            {"Origin": ready["origin"], "Content-Type": "application/json"},
        )
        response = conn.getresponse()
        return response.status, json.loads(response.read())
    finally:
        conn.close()


try:
    cases = [
        ("list", {"op": "list", "path": root}, 200),
        ("select", {"op": "select", "path": root}, 200),
        ("mkdir", {"op": "mkdir", "path": root, "name": "中文 子目录"}, 200),
        ("select-new", {"op": "select", "path": root + "/中文 子目录"}, 200),
        ("duplicate", {"op": "mkdir", "path": root, "name": "中文 子目录"}, 400),
    ]
    for name, raw in [
        ("linux-dot", root + "/."),
        ("linux-empty", root.replace("/workspaces/", "/workspaces//")),
        ("unc-dot", "\\\\wsl.localhost\\Ubuntu" + root.replace("/", "\\") + "\\."),
        ("unc-empty", "\\\\wsl.localhost\\Ubuntu\\" + root.replace("/", "\\")),
    ]:
        cases.append((name, {"op": "select", "path": raw}, 400))
    for name, value, expected in cases:
        status, body = call(value)
        results.append(
            {"name": name, "status": status, "body": body, "pass": status == expected}
        )
        assert status == expected, (name, body)
finally:
    child.stdin.close()
    remaining = child.stdout.read().decode()
    child.wait(timeout=20)
    stderr.close()
    subprocess.run(
        wsl
        + [
            "-c",
            'import pathlib,sys,shutil; paths=[pathlib.Path(v) for v in sys.argv[1:]]; assert all(p.parent in (pathlib.Path("/home/plumwsl"),pathlib.Path("/home/plumwsl/workspaces")) and p.name.startswith((".g0-files-","g0-directory-final-")) and not p.is_symlink() for p in paths); [shutil.rmtree(p) for p in paths]',
            ready["home"],
            root,
        ],
        check=True,
    )
    report = {
        "cases": results,
        "carrierExit": child.returncode,
        "guestTail": [json.loads(v) for v in remaining.splitlines()],
        "home": ready["home"],
        "workspace": root,
        "cleanup": True,
    }
    (out / "directory-final.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
assert child.returncode == 0
print(json.dumps({"passed": len(results), "carrierExit": child.returncode}))
