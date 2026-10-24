"""Exact Windows research-host faults with independent Linux kernel observations."""

import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import time
import traceback
import uuid

BASE = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location(
    "win_fixture", BASE.parent / "round4/windows_probe.py"
)
W = importlib.util.module_from_spec(spec)
spec.loader.exec_module(W)
NODE = Path("C:/Users/plumlocal/AppData/Local/mise/installs/node/22.23.3/node.exe")
ELECTRON = BASE.parents[3] / "desktop/node_modules/electron/dist/electron.exe"


def rows(path):
    return W.J.rows(Path(path))


def control(action, *arguments):
    completed = subprocess.run(
        W.guest(W.lp(BASE / "guest_control.py"), action, *arguments),
        env=W.ENV,
        capture_output=True,
        text=True,
        timeout=40,
    )
    assert completed.returncode == 0, (completed.stdout, completed.stderr)
    return json.loads(completed.stdout) if completed.stdout.strip() else None


def run_case(scenario, candidate=False, electron=False, owner="owner.cjs", extra=None):
    label = (
        scenario
        + ("-candidate" if candidate else "-original")
        + ("-electron" if electron else "-node")
        + "-"
        + uuid.uuid4().hex[:8]
    )
    prefix = BASE / label
    home = control("setup")["home"]
    kernel_record = Path(str(prefix) + ".kernel.jsonl")
    host_record = Path(str(prefix) + ".host.jsonl")
    config_path = Path(str(prefix) + ".config.json")
    result_path = Path(str(prefix) + ".host-result.json")
    config = dict(
        home=home,
        scenario=scenario,
        candidate=candidate,
        closeGate=scenario in {"carrier-loss", "host-loss", "ready-eof-gated"},
        guestControl=W.lp(BASE / "guest_control.py"),
        researchDirectory=W.lp(BASE),
        kernelRecord=W.lp(kernel_record),
        kernelRecordWindows=str(kernel_record),
        hostRecord=str(host_record),
        resultPath=str(result_path),
        profilePath=str(prefix) + ".electron-profile",
    )
    if extra:
        config.update(extra)
    config_path.write_text(json.dumps(config), encoding="utf-8")
    stdout_file = open(str(prefix) + ".stdout", "wb")
    stderr_file = open(str(prefix) + ".stderr", "wb")
    executable = ELECTRON if electron else NODE
    environment = dict(W.ENV)
    environment.pop("ELECTRON_RUN_AS_NODE", None)
    process = subprocess.Popen(
        [str(executable), str(BASE / owner), str(config_path)],
        env=environment,
        cwd=str(BASE.parents[3]),
        stdout=stdout_file,
        stderr=stderr_file,
        creationflags=0x08000000,
    )
    host_creation = W.J.creation(process._handle)
    result = dict(
        label=label,
        scenario=scenario,
        candidate=candidate,
        electron=electron,
        home=home,
        hostPid=process.pid,
        hostCreation=host_creation,
    )
    if extra:
        result["researchExtra"] = extra
    observer = None
    try:
        deadline = time.monotonic() + 100
        injected = False
        while process.poll() is None:
            if (
                scenario in {"host-loss", "host-before-start"}
                and not injected
                and any(
                    row["event"] == "research-host-kill-ready"
                    for row in rows(host_record)
                )
            ):
                watch_record = Path(str(prefix) + ".pidfd.jsonl")
                observer = subprocess.Popen(
                    W.guest(
                        W.lp(BASE / "guest_control.py"),
                        "watch",
                        home,
                        W.lp(kernel_record),
                        W.lp(watch_record),
                    ),
                    env=W.ENV,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                )
                watch_deadline = time.monotonic() + 10
                while not any(
                    row["event"] == "pidfd-watch-ready" for row in rows(watch_record)
                ):
                    assert time.monotonic() < watch_deadline
                    time.sleep(0.01)
                assert W.J.creation(process._handle) == host_creation
                W.J.check(W.J.K.TerminateProcess(process._handle, 93))
                result["hostKilledThroughExactHandle"] = True
                if scenario == "host-loss":
                    result["lossClassification"] = control(
                        "observe-loss", home, W.lp(kernel_record)
                    )
                    result["afterHostDeath"] = control(
                        "state", home, W.lp(kernel_record)
                    )
                    control("release-close", home)
                output, error = observer.communicate(timeout=45)
                result["pidfdObserver"] = dict(
                    exit=observer.returncode,
                    stdout=output.decode(errors="replace"),
                    stderr=error.decode(errors="replace"),
                    rows=rows(watch_record),
                )
                assert observer.returncode == 0
                injected = True
            if time.monotonic() >= deadline:
                raise TimeoutError("research host observation deadline")
            time.sleep(0.02)
        result["hostExit"] = process.returncode
        result["hostRows"] = rows(host_record)
        result["kernelRows"] = rows(kernel_record)
        if result_path.exists():
            result["hostResult"] = json.loads(result_path.read_text(encoding="utf-8"))
            assert "failure" not in result["hostResult"]
        if scenario not in {"host-loss", "host-before-start"}:
            assert process.returncode == 0
        result["observed"] = True
    except BaseException:
        result["failure"] = traceback.format_exc()
        if process.poll() is None:
            W.J.check(W.J.K.TerminateProcess(process._handle, 94))
            process.wait(timeout=10)
    finally:
        result["beforeCleanup"] = control("state", home, W.lp(kernel_record))
        result["cleanup"] = control("cleanup", home, W.lp(kernel_record))
        stdout_file.close()
        stderr_file.close()
        result["hostStdout"] = Path(str(prefix) + ".stdout").read_text(
            encoding="utf-8", errors="replace"
        )
        result["hostStderr"] = Path(str(prefix) + ".stderr").read_text(
            encoding="utf-8", errors="replace"
        )
        (BASE / (label + "-summary.json")).write_text(
            json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        print(
            json.dumps(
                {
                    key: result.get(key)
                    for key in (
                        "label",
                        "observed",
                        "failure",
                        "hostExit",
                        "hostStderr",
                    )
                }
            ),
            flush=True,
        )
    return result


if __name__ == "__main__":
    group = sys.argv[1] if len(sys.argv) > 1 else "smoke"
    if group == "smoke":
        cases = [("fragmented", False, False)]
    elif group == "child-clear":
        cases = [
            ("kernel-crash", False, False),
            ("kernel-crash", True, False),
            ("carrier-loss", True, False),
        ]
    elif group == "protocol":
        cases = [
            (name, False, False)
            for name in (
                "before-start-eof",
                "after-start-eof",
                "malformed-output",
                "partial-output",
                "unicode-failure",
                "close-failure",
                "carrier-before-start",
            )
        ]
    elif group == "hosts":
        cases = [
            ("normal", False, True),
            ("host-before-start", False, False),
            ("host-loss", True, False),
            ("host-loss", True, True),
        ]
    elif group == "loss-recheck":
        cases = [
            ("carrier-loss", False, False),
            ("carrier-loss", True, False),
            ("carrier-loss", True, True),
            ("host-loss", True, False),
            ("host-loss", True, True),
            ("ready-eof-gated", False, False),
        ]
    elif group == "clean-eof":
        cases = [("ready-eof-gated", False, False)]
    else:
        raise ValueError(group)
    results = [run_case(*case) for case in cases]
    (BASE / (group + "-" + uuid.uuid4().hex[:8] + "-results.json")).write_text(
        json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8"
    )
