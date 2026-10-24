"""One canonical commit -> distro loss -> untouched PG recovery -> original Quit."""

import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import time
import traceback
import urllib.request
import uuid

BASE = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location(
    "r6_windows", BASE.parent / "round6/windows_probe.py"
)
P = importlib.util.module_from_spec(spec)
spec.loader.exec_module(P)
STATE = json.loads((BASE / "distro-state.json").read_text(encoding="utf-8"))
REPO = "/home/plumwsl/workspaces/pulsara_agent"
PYTHON = REPO + "/desktop/resources/python/bin/python3"
label = "recovery-" + uuid.uuid4().hex[:8]
result = dict(label=label, target=STATE["name"], phases=[])
hosts = []
carrier_handle = None


def guest(script, *args):
    return [
        P.W.WSL,
        "-d",
        STATE["name"],
        "-u",
        "plumwsl",
        "--cd",
        REPO,
        "--exec",
        PYTHON,
        P.W.lp(BASE / script),
        *args,
    ]


def read_control(script, *args):
    value = subprocess.run(
        guest(script, *args), capture_output=True, text=True, timeout=45
    )
    assert value.returncode == 0, (value.stdout, value.stderr)
    return json.loads(value.stdout)


def management(action):
    value = subprocess.run(
        [sys.executable, str(BASE / "distro_manage.py"), action],
        capture_output=True,
        text=True,
        timeout=90,
    )
    assert value.returncode == 0, (value.stdout, value.stderr)
    return [json.loads(line) for line in value.stdout.splitlines()]


def rows(config):
    return P.rows(Path(config["hostRecord"]))


def events(config, event):
    return [row for row in rows(config) if row.get("event") == event]


def until(predicate, seconds=90):
    deadline = time.monotonic() + seconds
    while not predicate():
        assert time.monotonic() < deadline, "research observation deadline"
        time.sleep(0.02)


def start(home, phase, initialize):
    prefix = BASE / (label + "-" + phase)
    config = dict(
        home=home,
        scenario="distro-loss" if initialize else "recovery-after",
        candidate=True,
        initialize=initialize,
        closeGate=False,
        guestControl=P.W.lp(BASE / "distro_guest_control.py"),
        researchDirectory=P.W.lp(BASE / "distro-site"),
        kernelRecord=P.W.lp(Path(str(prefix) + ".kernel.jsonl")),
        kernelRecordWindows=str(prefix) + ".kernel.jsonl",
        hostRecord=str(prefix) + ".host.jsonl",
        resultPath=str(prefix) + ".host-result.json",
        profilePath=str(prefix) + ".profile",
        finishPath=str(prefix) + ".finish",
    )
    config_path = Path(str(prefix) + ".config.json")
    config_path.write_text(json.dumps(config), encoding="utf-8")
    out = open(str(prefix) + ".stdout", "wb")
    err = open(str(prefix) + ".stderr", "wb")
    host = subprocess.Popen(
        [str(P.NODE), str(BASE / "recovery-owner.cjs"), str(config_path)],
        stdout=out,
        stderr=err,
        env=P.W.ENV,
        creationflags=0x08000000,
    )
    fact = dict(
        phase=phase,
        config=config,
        hostPid=host.pid,
        hostCreation=P.W.J.creation(host._handle),
    )
    result["phases"].append(fact)
    hosts.append((host, fact, out, err))
    until(
        lambda: (
            events(
                config,
                "temporary-distro-terminate-ready"
                if initialize
                else "recovery-new-attempt-ready",
            )
            or host.poll() is not None
        )
    )
    assert host.poll() is None, rows(config)
    origin = events(config, "actual-kernel-ready")[0]["origin"]
    return host, fact, origin


http = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def request(origin, path, payload=None):
    data = None if payload is None else json.dumps(payload).encode()
    call = urllib.request.Request(
        origin + path,
        data=data,
        headers={
            "Origin": origin,
            "Sec-Fetch-Site": "same-origin",
            "Content-Type": "application/json",
        },
    )
    with http.open(call, timeout=45) as reply:
        return dict(status=reply.status, body=json.loads(reply.read()))


home = read_control("distro_guest_control.py", "setup")["home"]
result["home"] = home
try:
    first, first_fact, origin = start(home, "before", True)
    result["canonicalCreate"] = request(
        origin, "/api/sessions", {"workspace_kind": "quick"}
    )
    assert result["canonicalCreate"]["status"] == 201
    session_id = result["canonicalCreate"]["body"]["session"]["id"]
    result["sessionId"] = session_id
    result["canonicalReadBefore"] = request(origin, "/api/sessions/" + session_id)
    before = result["beforeTerminate"] = read_control(
        "recovery_facts.py", "query", home, session_id
    )
    assert before["durabilitySettings"] == dict(fsync="on", synchronous_commit="on")
    assert not before["kernelLock"]["acquired"] and not before["pgLock"]["acquired"]
    assert "in production" in before["control"]["stdout"]
    carrier = events(first_fact["config"], "actual-wsl-carrier-spawned")[0]
    carrier_handle = P.W.J.check(
        P.W.J.K.OpenProcess(0x1000 | 0x100000, False, carrier["pid"])
    )
    result["heldCarrier"] = dict(
        pid=carrier["pid"], creation=P.W.J.creation(carrier_handle)
    )
    result["terminateManagement"] = management("terminate")
    first.wait(timeout=60)
    assert first.returncode == 0
    result["heldCarrier"]["waitAfterTerminate"] = P.W.J.K.WaitForSingleObject(
        carrier_handle, 10000
    )
    assert result["heldCarrier"]["waitAfterTerminate"] == 0
    after_loss = result["untouchedAfterExplicitRestart"] = read_control(
        "recovery_facts.py", "snapshot", home
    )
    assert before["pidNamespace"] != after_loss["pidNamespace"]
    assert before["pidfileHex"] == after_loss["pidfileHex"]
    assert before["pgDataInode"] == after_loss["pgDataInode"]
    assert after_loss["kernelLock"]["acquired"] and after_loss["pgLock"]["acquired"]
    assert "in production" in after_loss["control"]["stdout"]
    second, second_fact, recovered_origin = start(home, "after", False)
    result["canonicalReadAfter"] = request(
        recovered_origin, "/api/sessions/" + session_id
    )
    recovered = result["recovered"] = read_control(
        "recovery_facts.py", "query", home, session_id
    )
    assert result["canonicalReadAfter"]["status"] == 200
    assert before["canonicalRow"] == recovered["canonicalRow"]
    assert before["systemIdentifier"] == recovered["systemIdentifier"]
    assert before["pgDataInode"] == recovered["pgDataInode"]
    new_log = recovered["postgresLog"][len(after_loss["postgresLog"]) :]
    result["postgresRecoveryLog"] = new_log
    assert "database system was interrupted" in new_log
    assert "automatic recovery in progress" in new_log
    assert "database system is ready to accept connections" in new_log
    Path(second_fact["config"]["finishPath"]).touch()
    second.wait(timeout=60)
    assert second.returncode == 0
    quit_result = json.loads(
        Path(second_fact["config"]["resultPath"]).read_text(encoding="utf-8")
    )
    assert quit_result["pgStopCalls"] == 1 and quit_result["appExit"] == 0
    result["observed"] = True
    result["cleanup"] = read_control(
        "distro_guest_control.py",
        "cleanup",
        home,
        second_fact["config"]["kernelRecord"],
    )
except BaseException:
    result["failure"] = traceback.format_exc()
finally:
    for host, fact, out, err in hosts:
        if host.poll() is None:
            assert P.W.J.creation(host._handle) == fact["hostCreation"]
            P.W.J.check(P.W.J.K.TerminateProcess(host._handle, 94))
            host.wait(timeout=10)
        fact["hostExit"] = host.returncode
        fact["hostRows"] = rows(fact["config"])
        fact["kernelRows"] = P.rows(Path(fact["config"]["kernelRecordWindows"]))
        path = Path(fact["config"]["resultPath"])
        if path.exists():
            fact["hostResult"] = json.loads(path.read_text(encoding="utf-8"))
            fact["hostResult"].pop("rows", None)
        out.close()
        err.close()
    if carrier_handle:
        P.W.J.K.CloseHandle(carrier_handle)
    (BASE / "final-result.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(
        json.dumps(
            dict(
                label=label,
                observed=result.get("observed"),
                failure=result.get("failure"),
            )
        ),
        flush=True,
    )
assert result.get("observed"), result.get("failure")
