"""Finite Windows HTTPS check: ext4 case/Unicode identities remain distinct."""

import http.client
import json
from pathlib import Path
import ssl
import subprocess
from urllib.parse import urlsplit

out = Path(__file__).parent
source = out.parent / "round5/files_guest.py"
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
stderr = (out / "directory-names.stderr.log").open("wb")
child = subprocess.Popen(
    wsl + ["-u", "-c", source.read_text(encoding="utf-8")],
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
            'import tempfile;print(tempfile.mkdtemp(prefix="g0-names-",dir="/home/plumwsl/workspaces"))',
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
        body = json.loads(response.read())
        results.append({"input": value, "status": response.status, "body": body})
        assert response.status == 200, results[-1]
        return body
    finally:
        conn.close()


try:
    names = ["Case", "case", "caf\u00e9 中文", "cafe\u0301 中文"]
    for name in names:
        call({"op": "mkdir", "path": root, "name": name})
    listed = call({"op": "list", "path": root})
    assert sorted(listed["names"]) == sorted(names)
    selected = [call({"op": "select", "path": root + "/" + name}) for name in names]
    assert [item["selected"] for item in selected] == [
        root + "/" + name for name in names
    ]
    assert len({item["key"] for item in selected}) == 4
    for name, expected in zip(names, selected, strict=True):
        unc = "\\\\wsl.localhost\\Ubuntu" + (root + "/" + name).replace("/", "\\")
        assert call({"op": "select", "path": unc}) == expected
finally:
    child.stdin.close()
    tail = child.stdout.read().decode()
    child.wait(timeout=20)
    stderr.close()
    cleanup = subprocess.run(
        wsl
        + [
            "-c",
            'import pathlib,sys,shutil,json; paths=[pathlib.Path(v) for v in sys.argv[1:]]; assert paths[0].parent==pathlib.Path("/home/plumwsl") and paths[0].name.startswith(".g0-files-"); assert paths[1].parent==pathlib.Path("/home/plumwsl/workspaces") and paths[1].name.startswith("g0-names-"); assert all(not p.is_symlink() for p in paths); [shutil.rmtree(p) for p in paths]; print(json.dumps({"remaining":[str(p) for p in paths if p.exists()]}))',
            ready["home"],
            root,
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    report = {
        "requests": results,
        "carrierExit": child.returncode,
        "guestTail": [json.loads(v) for v in tail.splitlines()],
        "home": ready["home"],
        "workspace": root,
        "cleanup": json.loads(cleanup.stdout),
    }
    (out / "directory-names.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
assert child.returncode == 0
print(
    json.dumps(
        {
            "passedRequests": len(results),
            "distinctCanonicalKeys": 4,
            "cleanup": report["cleanup"],
        }
    )
)
