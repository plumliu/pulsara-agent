"""Focused exact-handle, no-pipe, nested Node, and A/B isolation research."""

import ctypes as c
import json
import os  # noqa: F401 -- preserve the executed research snapshot
from pathlib import Path
import subprocess
import sys
import time
import uuid
from job_probe import K, check, creation, own_fact, job_facts, rows, emit

base = Path(__file__).resolve().parent
node = r"C:\Users\plumlocal\AppData\Local\mise\installs\node\22.23.3\node.exe"


def start(label, nested=False, root_exit=False):
    label += "-" + uuid.uuid4().hex[:8]
    record = base / (label + ".jsonl")
    job = "Local\\PulsaraG0Research-" + label
    args = [
        sys.executable,
        str(base / "job_probe.py"),
        "owner",
        str(record),
        job,
        "carrier",
        "1" if root_exit else "0",
    ]
    log = open(str(record) + ".host-output", "wb")
    wrapped = (
        [
            str(
                base.parents[3]
                / "desktop"
                / "node_modules"
                / "electron"
                / "dist"
                / "electron.exe"
            ),
            str(base / "electron_owner.cjs"),
            *args,
        ]
        if nested == "electron"
        else [node, str(base / "node_owner.cjs"), *args]
    )
    host = subprocess.Popen((wrapped if nested else args), stdout=log, stderr=log)
    host_identity = own_fact(host.pid)["creation"]
    deadline = time.monotonic() + 30
    while not any(x.get("role") == "child" for x in rows(record)):
        if host.poll() is not None:
            raise RuntimeError(f"host died {record}")
        if time.monotonic() > deadline:
            raise TimeoutError(str(record))
        time.sleep(0.02)
    if root_exit:
        time.sleep(0.3)
    handle = check(K.OpenJobObjectW(4, False, job))
    members = job_facts(handle)
    K.CloseHandle(handle)
    # Keep exact process objects, not just their PIDs, across termination.
    held = []
    for m in members:
        ph = check(K.OpenProcess(0x1000 | 0x100000, False, m["pid"]))
        assert creation(ph) == m["creation"]
        held.append((m, ph))
    emit(
        str(record),
        "held-exact-job-member-handles",
        members=members,
        hostIdentity=host_identity,
        nestedNode=nested,
    )
    return dict(
        record=record,
        job=job,
        host=host,
        host_identity=host_identity,
        held=held,
        log=log,
    )


def finish(state, action):
    record = state["record"]
    host = state["host"]
    if action == "cancel":
        Path(str(record) + ".cancel").touch()
    elif action == "owner-crash":
        actual = next(x for x in rows(record) if x["event"] == "carrier-assigned")
        emit(
            str(record),
            "exact-job-handle-owner-crash",
            fact=own_fact(actual["pid"], actual["ownerCreation"], True),
        )
    else:
        emit(
            str(record),
            "exact-host-crash",
            fact=own_fact(host.pid, state["host_identity"], True),
        )
    host.wait(timeout=10)
    waits = []
    for fact, ph in state["held"]:
        result = K.WaitForSingleObject(ph, 5000)
        waits.append(dict(**fact, waitResult=result))
        K.CloseHandle(ph)
    handle = K.OpenJobObjectW(4, False, state["job"])
    last_error = c.get_last_error()
    if handle:
        K.CloseHandle(handle)
    emit(
        str(record),
        "after-fault-exact-handle-waits",
        waits=waits,
        jobCanReopen=bool(handle),
        openJobError=last_error,
        hostExit=host.returncode,
    )
    assert all(x["waitResult"] == 0 for x in waits), waits
    assert not handle and last_error == 2, (handle, last_error)
    for x in rows(record):
        if x["event"] == "pe-start":
            assert own_fact(x["pid"], x["creation"])["exists"] is False
    state["log"].close()
    return dict(
        record=record.name,
        action=action,
        membersWaited=len(waits),
        allSignaled=True,
        jobDestroyed=True,
    )


results = []
for nested, root_exit, action in [
    (False, False, "cancel"),
    (False, False, "crash"),
    (False, True, "owner-crash"),
    (False, True, "crash"),
    (True, True, "crash"),
    ("electron", True, "crash"),
]:
    state = start(
        "scope-"
        + (("electron-" if nested == "electron" else "node-") if nested else "")
        + ("nopipe-" if root_exit else "")
        + action,
        nested,
        root_exit,
    )
    results.append(finish(state, action))
a = start("isolation-A")
b = start("isolation-B")
results.append(finish(a, "cancel"))
b_alive = [
    dict(**fact, waitResult=K.WaitForSingleObject(ph, 0)) for fact, ph in b["held"]
]
emit(str(b["record"]), "B-after-A-cancel", members=b_alive)
assert all(x["waitResult"] == 258 for x in b_alive), b_alive
results.append(dict(isolationBStillAliveAfterACancel=True, members=len(b_alive)))
results.append(finish(b, "cancel"))
(base / "scope-summary.json").write_text(
    json.dumps(results, indent=2), encoding="utf-8"
)
print(json.dumps(results, indent=2))
