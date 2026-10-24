"""Research-only anchor-owned O_PATH transfer before operation admission."""

import ctypes as c
import json
import os  # noqa: F401 -- preserve executed research snapshot
from pathlib import Path  # noqa: F401 -- preserve executed research snapshot
import subprocess
import time  # noqa: F401 -- preserve executed research snapshot
import uuid
import race_probe as R

BASE = R.BASE
J = R.J
S = R.S
CODE = (BASE / "fd_relay.py").read_text()


def start_anchor(label, control, stage):
    record = BASE / (label + ".anchor.jsonl")
    gate = BASE / (label + ".anchor-release")
    name = "Local\\PulsaraG0Fd-" + label
    job = J.check(J.K.CreateJobObjectW(None, name))
    limits = J.Extended()
    limits.BasicLimitInformation.LimitFlags = 0x2000
    J.check(J.K.SetInformationJobObject(job, 9, c.byref(limits), c.sizeof(limits)))
    args = S.command(
        CODE, "anchor", S.linux_path(record), control, stage, S.linux_path(gate), ""
    )
    si = J.SI()
    si.cb = c.sizeof(si)
    pi = J.PI()
    J.check(
        J.K.CreateProcessW(
            args[0],
            c.create_unicode_buffer(subprocess.list2cmdline(args)),
            None,
            None,
            False,
            0x08000000 | 4,
            None,
            None,
            c.byref(si),
            c.byref(pi),
        )
    )
    J.check(J.K.AssignProcessToJobObject(job, pi.hProcess))
    J.emit(
        record,
        "anchor-assigned-before-resume",
        carrierPid=pi.dwProcessId,
        carrierCreation=J.creation(pi.hProcess),
        carrierInJob=J.member(pi.hProcess, job),
        guestReadyExists=any(x["event"] == "anchor-ready" for x in J.rows(record)),
    )
    assert not any(x["event"] == "anchor-ready" for x in J.rows(record))
    assert J.K.ResumeThread(pi.hThread) != 0xFFFFFFFF
    ready = S.wait_row(record, "anchor-ready")
    return dict(record=record, gate=gate, job=job, name=name, pi=pi, ready=ready)


def run():
    label = "binding-" + uuid.uuid4().hex[:8]
    summary = dict(label=label, scenarios={})
    receivers = []
    anchors = []
    callers = []
    try:
        for stage in ("before-pin", "after-pin", "after-send", "live"):
            name = label + "-" + stage
            record = BASE / (name + ".receiver.jsonl")
            gate = BASE / (name + ".bind-release")
            expected = BASE / (name + ".anchor.jsonl")
            process = subprocess.Popen(
                S.command(
                    CODE,
                    "receiver",
                    S.linux_path(record),
                    R.GUEST_ROOT,
                    stage,
                    S.linux_path(gate),
                    S.linux_path(expected),
                ),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            ready = S.wait_row(record, "receiver-ready")
            rr = dict(process=process, record=record, ready=ready, gate=gate)
            receivers.append(rr)
            anchor = start_anchor(name, ready["control"], stage)
            anchors.append(anchor)
            result = {}
            if stage in ("before-pin", "after-pin"):
                S.wait_row(anchor["record"], stage + "-gate")
                result["jobCancel"] = R.terminate_job(anchor)
                result["anchorPidfdKill"] = R.pidfd_kill(anchor["ready"])
                assert not any(x["event"] == "fd-received" for x in J.rows(record))
                result["operationAdmitted"] = False
            elif stage == "after-send":
                S.wait_row(record, "fd-received")
                result["jobCancel"] = R.terminate_job(anchor)
                result["anchorPidfdKill"] = R.pidfd_kill(anchor["ready"])
                gate.touch()
                S.wait_row(record, "binding-rejected-anchor-dead")
                out, err = process.communicate(timeout=10)
                result.update(
                    operationAdmitted=False,
                    receiverCode=process.returncode,
                    stdout=out.decode(),
                    stderr=err.decode(),
                )
            else:
                S.wait_row(record, "fd-received")
                gate.touch()
                bound = S.wait_row(record, "receiver-bound")
                caller = R.launch(name, bound["socket"], anchor, anchors[0], seconds=2)
                callers.append(caller)
                pe = S.wait_row(caller["record"], "pe-start")
                handle = J.check(J.K.OpenProcess(0x1000 | 0x100000, False, pe["pid"]))
                try:
                    result["windowsOwnerBinding"] = dict(
                        sameCreation=J.creation(handle) == pe["creation"],
                        memberOfExactJob=J.member(handle, anchor["job"]),
                        notOtherJob=not J.member(handle, anchors[0]["job"]),
                    )
                finally:
                    J.K.CloseHandle(handle)
                assert all(result["windowsOwnerBinding"].values())
                result["caller"] = R.finish(caller)
                result["operationAdmitted"] = True
                result["cancelAfterBinding"] = R.terminate_job(anchor)
                late = R.launch(
                    name + "-after-cancel", bound["socket"], anchor, anchors[0]
                )
                callers.append(late)
                result["attemptAfterCancel"] = R.finish(late)
                assert not result["attemptAfterCancel"]["pe"]
            result["anchorRows"] = J.rows(anchor["record"])
            result["receiverRows"] = J.rows(record)
            summary["scenarios"][stage] = result
        summary["passedObservedGates"] = True
    finally:
        summary["cleanup"] = dict(receivers=[], anchors=[], paths=[], windows=[])
        for rr in receivers:
            if rr["process"].poll() is None:
                summary["cleanup"]["receivers"].append(R.pidfd_kill(rr["ready"]))
            out, err = rr["process"].communicate(timeout=10)
            summary["cleanup"]["receivers"].append(
                dict(
                    record=str(rr["record"]),
                    code=rr["process"].returncode,
                    stdout=out.decode(),
                    stderr=err.decode(),
                )
            )
        for anchor in anchors:
            summary["cleanup"]["anchors"].append(R.terminate_job(anchor))
            fact = R.guest(
                "import json,os,sys; p='/proc/'+sys.argv[1]+'/stat'; actual=open(p).read().split()[21] if os.path.exists(p) else None; print(json.dumps(dict(same=actual==sys.argv[2])))",
                str(anchor["ready"]["pid"]),
                anchor["ready"]["starttime"],
            )
            if fact["same"]:
                R.pidfd_kill(anchor["ready"])
            for handle in (anchor["pi"].hThread, anchor["pi"].hProcess, anchor["job"]):
                J.K.CloseHandle(handle)
        for caller in callers:
            for row in J.rows(caller["record"]):
                if row["event"] == "pe-start":
                    summary["cleanup"]["windows"].append(
                        J.own_fact(row["pid"], row["creation"], True)
                    )
        for rr in receivers:
            summary["cleanup"]["paths"].append(
                R.guest(
                    "import json,os,sys; d=sys.argv[1]; root=sys.argv[2]; assert d.startswith(root+'/fd-relay-'); paths=[d+'/interop',d+'/control']; exec(\"for p in paths:\\n if os.path.lexists(p): os.unlink(p)\"); os.rmdir(d); print(json.dumps(dict(directory=d,exists=os.path.exists(d))))",
                    rr["ready"]["directory"],
                    R.GUEST_ROOT,
                )
            )
        path = BASE / (label + "-summary.json")
        path.write_text(
            json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        print(path)


if __name__ == "__main__":
    run()
