"""G0-only cancel after seal, at opaque byte boundaries already reached."""

import json
from pathlib import Path
import subprocess
import uuid
import race_probe as R
import sys

J = R.J
S = R.S
BASE = R.BASE


def run():
    label = "jobcancel-" + uuid.uuid4().hex[:8]
    combined = "--combined" in sys.argv
    summary = dict(label=label, combinedLinuxGroup=combined, scenarios={})
    anchors = []
    relays = []
    callers = []
    try:
        for mode, event in (
            ("before", "before-forward"),
            ("half", "half-forwarded"),
            ("after", "after-forwarded"),
        ):
            a = S.anchor(label + "-" + mode)
            anchors.append(a)
            rr = R.relay(label + "-" + mode, a, mode)
            relays.append(rr)
            caller = R.launch(
                label + "-" + mode,
                rr["ready"]["socket"],
                a,
                a,
                seconds=10,
                owned_group=combined,
            )
            callers.append(caller)
            gate = S.wait_row(rr["record"], event)
            held = None
            if mode == "after":
                row = S.wait_row(caller["record"], "pe-start")
                held = J.check(J.K.OpenProcess(0x1000 | 0x100000, False, row["pid"]))
                assert J.creation(held) == row["creation"] and J.member(held, a["job"])
            Path(str(rr["gate"]) + ".seal").touch()
            sealed = S.wait_row(rr["record"], "admission-sealed")
            assert sealed["socketStillExists"]
            cancellation = R.terminate_job(a)
            rr["gate"].touch()
            result = dict(
                gate=gate,
                sealed=sealed,
                cancellation=cancellation,
                classification="unknown"
                if mode != "before"
                else "fixture-proves-no-forward-at-cancel",
            )
            summary["scenarios"][mode] = result
            if combined:
                ready = S.wait_row(
                    Path(str(caller["record"]) + ".linux"), "caller-ready"
                )
                result["linuxGroupCancel"] = R.cancel_group(ready)
            try:
                result["caller"] = R.finish(caller)
            except subprocess.TimeoutExpired:
                result["callerJoin"] = "probe-20s-timeout-unknown"
                ready = S.wait_row(
                    Path(str(caller["record"]) + ".linux"), "caller-ready"
                )
                result["linuxOwnerCleanup"] = R.settle_caller(ready)
                result["caller"] = R.finish(caller)
            if held:
                result["exactPeWait"] = J.K.WaitForSingleObject(held, 5000)
                J.K.CloseHandle(held)
                assert result["exactPeWait"] == 0
            if mode in ("before", "half"):
                assert not result["caller"]["pe"]
            # An old caller retaining the sealed pathname must fail, never fallback.
            late = R.launch(label + "-" + mode + "-sealed", rr["ready"]["socket"], a, a)
            callers.append(late)
            result["sealedAttempt"] = R.finish(late)
            assert not result["sealedAttempt"]["pe"]
            summary["scenarios"][mode] = result
        summary["passedWindowsCancelAndSealGates"] = True
        summary["linuxCallerJoinedWithoutExplicitCleanup"] = all(
            "callerJoin" not in case for case in summary["scenarios"].values()
        )
    finally:
        summary["cleanup"] = dict(relays=[], jobs=[], paths=[], windows=[])
        for rr in relays:
            if rr["process"].poll() is None:
                summary["cleanup"]["relays"].append(R.pidfd_kill(rr["ready"]))
                rr["process"].communicate(timeout=10)
        for a in anchors:
            summary["cleanup"]["jobs"].append(R.terminate_job(a))
            R.pidfd_kill(a["ready"])
            for handle in (a["pi"].hThread, a["pi"].hProcess, a["job"]):
                J.K.CloseHandle(handle)
        for caller in callers:
            if caller["process"].poll() is None:
                ready = S.wait_row(
                    Path(str(caller["record"]) + ".linux"), "caller-ready"
                )
                R.settle_caller(ready)
                caller["process"].communicate(timeout=10)
            for row in J.rows(caller["record"]):
                if row["event"] == "pe-start":
                    summary["cleanup"]["windows"].append(
                        J.own_fact(row["pid"], row["creation"], True)
                    )
        for rr in relays:
            summary["cleanup"]["paths"].append(
                R.guest(
                    "import json,os,sys; d=sys.argv[1]; root=sys.argv[2]; assert d.startswith(root+'/relay-'); os.unlink(d+'/interop'); os.rmdir(d); print(json.dumps(dict(directory=d,exists=os.path.exists(d))))",
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
