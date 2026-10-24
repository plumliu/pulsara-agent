"""Windows drives two real WSL pipe children; no payload copied into NTFS."""

import json
import os
from pathlib import Path
import subprocess
import time  # noqa: F401 -- preserve executed research snapshot
import uuid

BASE = Path(__file__).resolve().parent
WSL = os.environ["SystemRoot"] + r"\System32\wsl.exe"
LINUX = "/home/plumwsl/workspaces/pulsara_agent"
PYTHON = LINUX + "/.venv/bin/python"
SCRIPT = str(BASE / "payload_probe.py").replace("\\", "/").replace("C:", "/mnt/c")
ENV = {key: value for key, value in os.environ.items() if key.upper() != "WSLENV"}
root = "/home/plumwsl/.g0d6-" + uuid.uuid4().hex[:12]
records = []


def command(*args, offline=False):
    program = (
        ["unshare", "--user", "--map-current-user", "--net", PYTHON]
        if offline
        else [PYTHON]
    )
    return [
        WSL,
        "-d",
        "Ubuntu",
        "-u",
        "plumwsl",
        "--cd",
        LINUX,
        "--exec",
        *program,
        SCRIPT,
        *args,
    ]


def call(*args, offline=False):
    result = subprocess.run(
        command(*args, offline=offline),
        env=ENV,
        capture_output=True,
        text=True,
        timeout=180,
    )
    rows = [json.loads(line) for line in result.stdout.splitlines() if line]
    value = dict(
        args=list(args),
        offlineNetworkNamespace=offline,
        exit=result.returncode,
        rows=rows,
        stderr=result.stderr,
    )
    records.append(value)
    return value


try:
    packed = call("pack", root)
    assert packed["exit"] == 0, packed
    artifact = packed["rows"][-1]
    producer = subprocess.Popen(
        [
            WSL,
            "-d",
            "Ubuntu",
            "-u",
            "plumwsl",
            "--exec",
            "/bin/cat",
            artifact["archive"],
        ],
        env=ENV,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    receiver = subprocess.Popen(
        command("receive", root, artifact["sha256"]),
        env=ENV,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    sent = 0
    try:
        while chunk := producer.stdout.read(1024 * 1024):
            receiver.stdin.write(chunk)
            sent += len(chunk)
        receiver.stdin.close()
        receiver.stdin = None
        output, error = receiver.communicate(timeout=30)
        _, producer_error = producer.communicate(timeout=30)
        received = dict(
            event="windows-pipe-transfer",
            sent=sent,
            producerExit=producer.returncode,
            receiverExit=receiver.returncode,
            producerStderr=producer_error.decode(),
            receiverStderr=error.decode(),
            rows=[json.loads(line) for line in output.decode().splitlines()],
        )
        records.append(received)
        assert (
            producer.returncode == receiver.returncode == 0
            and sent == artifact["bytes"]
        )
    finally:
        for process in (producer, receiver):
            if process.poll() is None:
                process.kill()
                process.communicate()
    result = call("install", root, "offline", artifact["sha256"], "none", offline=True)
    assert result["exit"] == 0, result
    for case, fault, digest in [
        ("missing", "missing", artifact["sha256"]),
        ("bad-digest", "none", "0" * 64),
        ("enospc", "enospc", artifact["sha256"]),
    ]:
        result = call("install", root, case, digest, fault, offline=True)
        assert result["exit"] == 2, result
    process = subprocess.Popen(
        command(
            "install", root, "interrupt", artifact["sha256"], "interrupt", offline=True
        ),
        env=ENV,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    gate = json.loads(process.stdout.readline())
    assert gate["event"] == "real-member-written-before-interruption", gate
    records.append(dict(event="interrupted-at-real-extract-write", gate=gate))
    kill_code = "import json,os,signal,sys; p=int(sys.argv[1]); fd=os.pidfd_open(p); actual=open('/proc/'+str(p)+'/stat').read().rsplit(')',1)[1].split()[19]; assert actual==sys.argv[2]; signal.pidfd_send_signal(fd,signal.SIGKILL); os.close(fd); print(json.dumps(dict(pid=p,starttime=actual,pidfdKilled=True)))"
    killed = subprocess.run(
        [
            WSL,
            "-d",
            "Ubuntu",
            "-u",
            "plumwsl",
            "--exec",
            PYTHON,
            "-c",
            kill_code,
            str(gate["pid"]),
            gate["starttime"],
        ],
        env=ENV,
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert killed.returncode == 0, killed.stderr
    out, err = process.communicate(timeout=15)
    records.append(
        dict(
            event="extract-helper-exactly-killed",
            facts=json.loads(killed.stdout),
            carrierExit=process.returncode,
            stdout=out,
            stderr=err,
        )
    )
    reopened = call(
        "install", root, "interrupt", artifact["sha256"], "none", offline=True
    )
    assert reopened["exit"] == 2, reopened
    cleared = call("clear-interrupted-stage", root)
    assert cleared["exit"] == 0, cleared
    retried = call(
        "install", root, "interrupt", artifact["sha256"], "none", offline=True
    )
    assert retried["exit"] == 0, retried
    passed = True
finally:
    cleanup = call("cleanup", root)
    path = BASE / "payload-results.json"
    path.write_text(
        json.dumps(
            dict(
                passed=locals().get("passed", False),
                root=root,
                records=records,
                cleanup=cleanup,
            ),
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    print(path)
