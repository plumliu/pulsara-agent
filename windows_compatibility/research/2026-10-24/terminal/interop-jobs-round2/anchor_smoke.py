import json
from pathlib import Path
import subprocess
import sys
import time
import uuid
from job_probe import K, check, creation, own_fact, job_facts, emit

base = Path(__file__).resolve().parent
results = []
for action in ("eof", "cancel", "crash"):
    label = "anchor-" + action + "-" + uuid.uuid4().hex[:8]
    record = str(base / (label + ".jsonl"))
    ready = record + ".ready.json"
    job = "Local\\PulsaraG0Research-" + label
    log = open(record + ".host-output", "wb")
    host = subprocess.Popen(
        [sys.executable, str(base / "job_anchor.py"), record, ready, job],
        stdout=log,
        stderr=log,
    )
    host_creation = own_fact(host.pid)["creation"]
    deadline = time.monotonic() + 20
    while not Path(ready).exists():
        assert host.poll() is None
        assert time.monotonic() < deadline
        time.sleep(0.02)
    guest = json.loads(Path(ready).read_text())
    assert guest["socket"] and guest["uid"] == 1000
    query = check(K.OpenJobObjectW(4, False, job))
    members = job_facts(query)
    K.CloseHandle(query)
    held = []
    for fact in members:
        ph = check(K.OpenProcess(0x1000 | 0x100000, False, fact["pid"]))
        assert creation(ph) == fact["creation"]
        held.append((fact, ph))
    emit(record, "smoke-ready", guest=guest, members=members)
    if action == "eof":
        Path(record + ".eof").touch()
        for _, ph in held:
            assert K.WaitForSingleObject(ph, 5000) == 0
        Path(record + ".snapshot").touch()
        time.sleep(0.1)
        Path(record + ".cancel").touch()
    elif action == "cancel":
        Path(record + ".cancel").touch()
    else:
        emit(record, "exact-owner-crash", fact=own_fact(host.pid, host_creation, True))
    host.wait(timeout=10)
    waits = []
    for fact, ph in held:
        result = K.WaitForSingleObject(ph, 5000)
        assert result == 0
        waits.append(dict(**fact, waitResult=result))
        K.CloseHandle(ph)
    emit(record, "smoke-finished", action=action, waits=waits)
    results.append(
        dict(
            record=Path(record).name,
            action=action,
            guest=guest,
            membersWaited=len(waits),
        )
    )
    log.close()
(base / "anchor-summary.json").write_text(json.dumps(results, indent=2))
print(json.dumps(results, indent=2))
