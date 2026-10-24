"""Keep one actual bundled kernel/PG ready while root coordinates human sleep."""

import json
from pathlib import Path
import subprocess
import time
import traceback
import uuid

import windows_probe as P

BASE = P.BASE
label = "sleep-" + uuid.uuid4().hex[:8]
prefix = BASE / label
home = P.control("setup")["home"]
config = dict(
    home=home,
    scenario="sleep",
    candidate=False,
    closeGate=False,
    guestControl=P.W.lp(BASE / "guest_control.py"),
    researchDirectory=P.W.lp(BASE),
    kernelRecord=P.W.lp(Path(str(prefix) + ".kernel.jsonl")),
    kernelRecordWindows=str(prefix) + ".kernel.jsonl",
    hostRecord=str(prefix) + ".host.jsonl",
    resultPath=str(prefix) + ".host-result.json",
    profilePath=str(prefix) + ".profile",
    sleepResumePath=str(prefix) + ".resume",
)
config_path = Path(str(prefix) + ".config.json")
config_path.write_text(json.dumps(config), encoding="utf-8")
out = open(str(prefix) + ".stdout", "wb")
err = open(str(prefix) + ".stderr", "wb")
host = subprocess.Popen(
    [str(P.NODE), str(BASE / "sleep-owner.cjs"), str(config_path)],
    stdout=out,
    stderr=err,
    env=P.W.ENV,
    creationflags=0x08000000,
)
creation = P.W.J.creation(host._handle)
result = dict(label=label, home=home, hostPid=host.pid, hostCreation=creation)


def facts():
    completed = subprocess.run(
        P.W.guest(P.W.lp(BASE / "sleep_facts.py"), home, config["kernelRecord"]),
        capture_output=True,
        text=True,
        check=True,
        timeout=20,
    )
    return json.loads(completed.stdout)


try:
    deadline = time.monotonic() + 60
    while not any(
        row.get("event") == "actual-kernel-sleep-ready"
        for row in P.rows(Path(config["hostRecord"]))
    ):
        assert host.poll() is None and time.monotonic() < deadline
        time.sleep(0.02)
    result["beforeSleep"] = facts()
    assert (
        not result["beforeSleep"]["kernelLock"]["acquired"]
        and not result["beforeSleep"]["pgLock"]["acquired"]
    )
    ready = dict(
        result,
        phase="ready-for-root-human-sleep",
        inspectPath=str(prefix) + ".inspect",
        resumePath=config["sleepResumePath"],
    )
    (BASE / "sleep-ready.json").write_text(
        json.dumps(ready, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(
        json.dumps(
            dict(
                label=label,
                phase=ready["phase"],
                hostPid=host.pid,
                beforeSleep=result["beforeSleep"],
            )
        ),
        flush=True,
    )
    # Human coordination has no automatic sleep, failure injection, or lifetime cutoff.
    while not Path(ready["inspectPath"]).exists():
        assert host.poll() is None
        time.sleep(0.1)
    result["afterSleep"] = facts()
    assert P.W.J.creation(host._handle) == creation and host.poll() is None
    for key in ("kernel", "postgres", "kernelLock", "pgLock", "pidNamespace", "query"):
        assert result["beforeSleep"][key] == result["afterSleep"][key], key
    print(
        json.dumps(
            dict(label=label, phase="same-kernel-pg-and-query-after-human-resume")
        ),
        flush=True,
    )
    Path(config["sleepResumePath"]).touch()
    host.wait(timeout=60)
    assert host.returncode == 0
    result["hostExit"] = host.returncode
    result["hostResult"] = json.loads(
        Path(config["resultPath"]).read_text(encoding="utf-8")
    )
    result["hostResult"].pop("rows", None)
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
    result["cleanup"] = P.control("cleanup", home, config["kernelRecord"])
    out.close()
    err.close()
    result["hostRows"] = P.rows(Path(config["hostRecord"]))
    result["kernelRows"] = P.rows(Path(config["kernelRecordWindows"]))
    (BASE / (label + "-result.json")).write_text(
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
