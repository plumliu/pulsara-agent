"""One fresh-distro positive control and one actual whole-distro loss."""

import json
from pathlib import Path
import subprocess
import sys
import time
import traceback
import uuid

import windows_probe as P

BASE = P.BASE
STATE = json.loads((BASE / "distro-state.json").read_text(encoding="utf-8"))
REPO = "/home/plumwsl/workspaces/pulsara_agent"
PYTHON = REPO + "/desktop/resources/python/bin/python3"


def guest(script, *args):
    return [
        str(P.W.WSL),
        "-d",
        STATE["name"],
        "-u",
        "plumwsl",
        "--cd",
        REPO,
        "--exec",
        PYTHON,
        script,
        *args,
    ]


def control(script, *args):
    result = subprocess.run(
        guest(P.W.lp(BASE / script), *args), capture_output=True, text=True, timeout=45
    )
    assert result.returncode == 0, (result.stdout, result.stderr)
    return json.loads(result.stdout)


def management(action):
    result = subprocess.run(
        [sys.executable, str(BASE / "distro_manage.py"), action],
        capture_output=True,
        text=True,
        timeout=90,
    )
    assert result.returncode == 0, (result.stdout, result.stderr)
    return [json.loads(line) for line in result.stdout.splitlines()]


def until(predicate, seconds=60):
    deadline = time.monotonic() + seconds
    while not predicate():
        assert time.monotonic() < deadline, "research observation deadline"
        time.sleep(0.02)


def run(scenario):
    label = scenario + "-" + uuid.uuid4().hex[:8]
    prefix = BASE / label
    home = control("distro_guest_control.py", "setup")["home"]
    config = dict(
        home=home,
        scenario=scenario,
        candidate=scenario == "distro-loss",
        closeGate=False,
        guestControl=P.W.lp(BASE / "distro_guest_control.py"),
        researchDirectory=P.W.lp(BASE / "distro-site"),
        kernelRecord=P.W.lp(Path(str(prefix) + ".kernel.jsonl")),
        kernelRecordWindows=str(prefix) + ".kernel.jsonl",
        hostRecord=str(prefix) + ".host.jsonl",
        resultPath=str(prefix) + ".host-result.json",
        profilePath=str(prefix) + ".electron-profile",
        resumeObservationPath=str(prefix) + ".resume",
    )
    config_path = Path(str(prefix) + ".config.json")
    config_path.write_text(json.dumps(config), encoding="utf-8")
    out = open(str(prefix) + ".stdout", "wb")
    err = open(str(prefix) + ".stderr", "wb")
    host = subprocess.Popen(
        [str(P.NODE), str(BASE / "distro-owner.cjs"), str(config_path)],
        stdout=out,
        stderr=err,
        creationflags=0x08000000,
        env=P.W.ENV,
    )
    creation = P.W.J.creation(host._handle)
    result = dict(
        label=label,
        scenario=scenario,
        home=home,
        name=STATE["name"],
        hostPid=host.pid,
        hostCreation=creation,
    )
    carrier_handle = None

    def host_rows():
        return P.rows(Path(config["hostRecord"]))

    def event(name):
        return [row for row in host_rows() if row.get("event") == name]

    try:
        if scenario == "distro-loss":
            until(lambda: event("temporary-distro-terminate-ready"))
            result["beforeTerminate"] = control("distro_recovery.py", "snapshot", home)
            carrier = event("actual-wsl-carrier-spawned")[0]
            carrier_handle = P.W.J.check(
                P.W.J.K.OpenProcess(0x1000 | 0x100000, False, carrier["pid"])
            )
            result["heldCarrier"] = dict(
                pid=carrier["pid"], creation=P.W.J.creation(carrier_handle)
            )
            result["terminateManagement"] = management("terminate")
            until(lambda: event("temporary-distro-carrier-closed-before-restart"))
            result["heldCarrier"]["waitAfterTerminate"] = P.W.J.K.WaitForSingleObject(
                carrier_handle, 10000
            )
            assert result["heldCarrier"]["waitAfterTerminate"] == 0
            # This explicit query restarts the isolated distro, in a fresh namespace.
            result["afterExplicitRestart"] = control(
                "distro_recovery.py", "snapshot", home
            )
            assert (
                result["beforeTerminate"]["pidNamespace"]
                != result["afterExplicitRestart"]["pidNamespace"]
            )
            assert not result["beforeTerminate"]["kernelLock"]["acquired"]
            assert not result["beforeTerminate"]["pgLock"]["acquired"]
            assert result["afterExplicitRestart"]["kernelLock"]["acquired"]
            assert result["afterExplicitRestart"]["pgLock"]["acquired"]
            assert not result["afterExplicitRestart"]["ownedPgProcesses"]
            assert (
                result["beforeTerminate"]["pgDataInode"]
                == result["afterExplicitRestart"]["pgDataInode"]
            )
            Path(config["resumeObservationPath"]).touch()
        host.wait(timeout=90)
        result["hostExit"] = host.returncode
        assert host.returncode == 0
        result["hostResult"] = json.loads(
            Path(config["resultPath"]).read_text(encoding="utf-8")
        )
        result["hostResult"].pop("rows", None)
        assert "failure" not in result["hostResult"]
        if scenario == "distro-loss":
            assert result["hostResult"]["pgStopCalls"] == 0
            assert result["hostResult"]["appExit"] is None
            assert result["hostResult"]["quitError"]
        else:
            assert (
                result["hostResult"]["pgStopCalls"] == 1
                and result["hostResult"]["appExit"] == 0
            )
        result["observed"] = True
    except BaseException:
        result["failure"] = traceback.format_exc()
        if host.poll() is None:
            assert P.W.J.creation(host._handle) == creation
            P.W.J.check(P.W.J.K.TerminateProcess(host._handle, 94))
            host.wait(timeout=10)
    finally:
        if carrier_handle:
            P.W.J.K.CloseHandle(carrier_handle)
        if scenario == "distro-loss":
            result["staleFileCleanup"] = control(
                "distro_recovery.py", "remove-stale-pid-for-cleanup", home
            )
        result["cleanup"] = control(
            "distro_guest_control.py", "cleanup", home, config["kernelRecord"]
        )
        out.close()
        err.close()
        result["hostRows"] = host_rows()
        result["kernelRows"] = P.rows(Path(config["kernelRecordWindows"]))
        result["stderr"] = Path(str(prefix) + ".stderr").read_text(
            encoding="utf-8", errors="replace"
        )
        (BASE / (label + "-result.json")).write_text(
            json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        print(
            json.dumps(
                dict(
                    label=label,
                    observed=result.get("observed"),
                    failure=result.get("failure"),
                    stderr=result["stderr"],
                )
            )
        )
    assert result.get("observed"), result.get("failure")


if __name__ == "__main__":
    run(sys.argv[1])
