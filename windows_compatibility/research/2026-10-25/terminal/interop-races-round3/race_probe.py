"""G0 research only: deterministic startup, byte forwarding and retirement gates."""

import ctypes as c  # noqa: F401 -- preserve executed research snapshot
import importlib.util
import json
import os  # noqa: F401 -- preserve executed research snapshot
from pathlib import Path
import subprocess
import sys
import time
import uuid

BASE = Path(__file__).resolve().parent
ROOT = next(
    parent
    for parent in BASE.parents
    if (parent / "pyproject.toml").exists() and (parent / ".git").exists()
)
source = (
    ROOT
    / "windows_compatibility/research/2026-10-24/terminal/interop-route-round2/socket_probe.py"
)
# The archived fixture's Job import is relative to its old output layout.
job_source = (
    ROOT
    / "windows_compatibility/research/2026-10-24/terminal/interop-jobs-round2/job_probe.py"
)
spec = importlib.util.spec_from_file_location("race_job", job_source)
J = importlib.util.module_from_spec(spec)
spec.loader.exec_module(J)
namespace = {"__file__": str(source), "__name__": "race_socket"}
text = source.read_text(encoding="utf-8")
start = text.index("SPEC = importlib.util.spec_from_file_location(")
end = text.index("WSL = os.environ", start)
namespace["supplied_job"] = J
exec(text[:start] + "J = supplied_job\n" + text[end:], namespace)
namespace["BASE"] = BASE
S = type("SocketFixture", (), namespace)
GUEST_ROOT = (
    "/home/plumwsl/workspaces/pulsara_agent/output/windows-g0/interop_races_r3/round3"
)


def guest(code, *args):
    result = subprocess.run(
        S.command(code, *args), capture_output=True, text=True, timeout=15
    )
    assert result.returncode == 0, (result.returncode, result.stdout, result.stderr)
    return json.loads(result.stdout)


def pidfd_kill(ready):
    return guest(
        """import json,os,signal,sys
p=int(sys.argv[1]); expected=sys.argv[2]
try:
 fd=os.pidfd_open(p)
except ProcessLookupError:
 print(json.dumps(dict(pid=p,exists=False,pidfdSignal=False)))
else:
 try:
  actual=open('/proc/'+str(p)+'/stat').read().split()[21]
  assert actual==expected
  try:
   signal.pidfd_send_signal(fd,signal.SIGKILL)
  except ProcessLookupError:
   sent=False
  else:
   sent=True
  print(json.dumps(dict(pid=p,starttime=actual,pidfdSignal=sent)))
 finally:
  os.close(fd)
""",
        str(ready["pid"]),
        ready["starttime"],
    )


def relay(label, anchor, mode="normal", upstream=None):
    record = BASE / (label + ".relay.jsonl")
    gate = BASE / (label + ".release")
    process = subprocess.Popen(
        S.command(
            (BASE / "opaque_gate.py").read_text(),
            upstream or anchor["ready"]["socket"],
            S.linux_path(record),
            GUEST_ROOT,
            mode,
            S.linux_path(gate),
        ),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    ready = S.wait_row(record, "relay-ready")
    return dict(record=record, gate=gate, process=process, ready=ready)


def settle_caller(ready):
    return guest(
        """import json,os,signal,sys
p=int(sys.argv[1]); expected=sys.argv[2]; facts=[]
try:
 parent=os.pidfd_open(p)
except ProcessLookupError:
 print(json.dumps(dict(pid=p,exists=False)))
else:
 actual=open('/proc/'+str(p)+'/stat').read().split()[21]
 assert actual==expected
 children=open('/proc/'+str(p)+'/task/'+str(p)+'/children').read().split()
 for child in children:
  try:
   fd=os.pidfd_open(int(child))
  except ProcessLookupError:
   continue
  try:
   text=open('/proc/'+child+'/stat').read(); tail=text[text.rindex(')')+2:].split()
   assert int(tail[1])==p
   facts.append(dict(pid=int(child),starttime=tail[19],cmdline=open('/proc/'+child+'/cmdline','rb').read().decode().replace(chr(0),' ')))
   try: signal.pidfd_send_signal(fd,signal.SIGKILL)
   except ProcessLookupError: pass
  finally: os.close(fd)
 try: signal.pidfd_send_signal(parent,signal.SIGKILL)
 except ProcessLookupError: pass
 os.close(parent)
 print(json.dumps(dict(pid=p,starttime=actual,pidfdSignal=True,directChildren=facts)))
""",
        str(ready["pid"]),
        ready["starttime"],
    )


def launch(label, socket, a, b, paused=False, seconds=0.2, owned_group=False):
    record = BASE / (label + ".pe.jsonl")
    gate = BASE / (label + ".caller-release")
    pe_args = [
        S.linux_path(sys.executable),
        str(Path(__file__).resolve()),
        "pe",
        str(record),
        a["name"],
        b["name"],
        str(seconds),
    ]
    code = "import json,os,subprocess,sys,time; f=open(sys.argv[1],'a',buffering=1); f.write(json.dumps(dict(event='caller-ready',pid=os.getpid(),starttime=open('/proc/self/stat').read().split()[21],originalSocket=os.environ.get('WSL_INTEROP')))+'\\n'); gate=sys.argv[3]; exec(\"while gate and not os.path.exists(gate): time.sleep(.01)\"); e=os.environ.copy(); e['WSL_INTEROP']=sys.argv[2]; p=subprocess.Popen(sys.argv[4:],env=e,stdin=subprocess.DEVNULL); rc=p.wait(); f.write(json.dumps(dict(event='caller-exit',code=rc))+'\\n')"
    if owned_group:
        code = code.replace(
            "f=open(sys.argv[1]",
            "exec('if os.getpgrp()!=os.getpid(): os.setsid()'); f=open(sys.argv[1]",
            1,
        ).replace(
            "event='caller-ready',pid=os.getpid()",
            "event='caller-ready',pid=os.getpid(),pgid=os.getpgrp(),sid=os.getsid(0)",
            1,
        )
    process = subprocess.Popen(
        S.command(
            code,
            S.linux_path(str(record) + ".linux"),
            socket,
            S.linux_path(gate) if paused else "",
            *pe_args,
        ),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    S.wait_row(Path(str(record) + ".linux"), "caller-ready")
    return dict(record=record, process=process, gate=gate)


def finish(caller):
    out, err = caller["process"].communicate(timeout=20)
    return dict(
        code=caller["process"].returncode,
        stdout=out.decode(errors="replace"),
        stderr=err.decode(errors="replace"),
        pe=J.rows(caller["record"]),
        linux=J.rows(Path(str(caller["record"]) + ".linux")),
    )


def cancel_group(ready):
    return guest(
        """import json,os,select,signal,sys,time
p=int(sys.argv[1]); expected=sys.argv[2]
root=os.pidfd_open(p)
text=open('/proc/'+str(p)+'/stat').read(); tail=text[text.rindex(')')+2:].split()
assert tail[19]==expected and int(tail[2])==p and int(tail[3])==p
def group():
 facts=[]
 for name in os.listdir('/proc'):
  if name.isdigit():
   try:
    text=open('/proc/'+name+'/stat').read(); tail=text[text.rindex(')')+2:].split()
   except FileNotFoundError: continue
   if int(tail[2])==p and tail[0]!='Z': facts.append(dict(pid=int(name),starttime=tail[19],state=tail[0]))
 return facts
before=group(); held=[]
for fact in before:
 try: fd=os.pidfd_open(fact['pid'])
 except ProcessLookupError: continue
 text=open('/proc/'+str(fact['pid'])+'/stat').read(); tail=text[text.rindex(')')+2:].split()
 assert tail[19]==fact['starttime']
 held.append((fd,fact))
os.killpg(p,signal.SIGTERM)
deadline=time.monotonic()+2
while group() and time.monotonic()<deadline: time.sleep(.02)
kill_sent=bool(group())
if kill_sent: os.killpg(p,signal.SIGKILL)
deadline=time.monotonic()+2
while group() and time.monotonic()<deadline: time.sleep(.02)
after=group(); waits=[dict(**fact,pidfdReadable=bool(select.select([fd],[],[],0)[0])) for fd,fact in held]
for fd,_ in held: os.close(fd)
os.close(root)
print(json.dumps(dict(pgid=p,before=before,after=after,exactWaits=waits,sigterm=True,sigkill=kill_sent)))
assert not after and all(fact['pidfdReadable'] for fact in waits)
""",
        str(ready["pid"]),
        ready["starttime"],
    )


def terminate_job(anchor):
    before = J.job_facts(anchor["job"])
    handles = []
    for fact in before:
        handle = J.check(J.K.OpenProcess(0x1000 | 0x100000, False, fact["pid"]))
        assert J.creation(handle) == fact["creation"]
        handles.append(handle)
    J.check(J.K.TerminateJobObject(anchor["job"], 99))
    waits = [J.K.WaitForSingleObject(handle, 5000) for handle in handles]
    for handle in handles:
        J.K.CloseHandle(handle)
    accounting = J.accounting(anchor["job"])
    assert all(value == 0 for value in waits) and accounting["ActiveProcesses"] == 0
    return dict(
        before=before,
        waits=waits,
        accounting=accounting,
        members=J.job_facts(anchor["job"]),
    )


def run():
    label = "races-" + uuid.uuid4().hex[:8]
    summary = dict(
        label=label,
        commit=subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip(),
        windowsPython=sys.version,
    )
    guest(
        "import json,os,sys; os.makedirs(sys.argv[1],exist_ok=True); print(json.dumps(dict(uid=os.getuid(),root=sys.argv[1],python=sys.version)))",
        GUEST_ROOT,
    )
    a, b = S.anchor(label + "-A"), S.anchor(label + "-B")
    relays, callers = [], []
    try:
        # Deliberately pin a live but wrong anchor: inode/path are not authority.
        wrong = relay(label + "-wrong", a, upstream=b["ready"]["socket"])
        relays.append(wrong)
        caller = launch(label + "-wrong", wrong["ready"]["socket"], a, b)
        callers.append(caller)
        S.wait_row(caller["record"], "pe-start")
        row = J.rows(caller["record"])[0]
        handle = J.check(J.K.OpenProcess(0x1000 | 0x100000, False, row["pid"]))
        try:
            same = J.creation(handle) == row["creation"]
            actual_a, actual_b = J.member(handle, a["job"]), J.member(handle, b["job"])
        finally:
            J.K.CloseHandle(handle)
        summary["wrongPinBinding"] = dict(
            sameIdentity=same,
            expectedJob=actual_a,
            otherJob=actual_b,
            admission=False,
            result=finish(caller),
        )
        assert same and not actual_a and actual_b
        # A correctly pinned socket is approved only by the owner's actual Job handle.
        good = relay(label + "-good", a)
        relays.append(good)
        caller = launch(label + "-good", good["ready"]["socket"], a, b, seconds=1)
        callers.append(caller)
        row = S.wait_row(caller["record"], "pe-start")
        handle = J.check(J.K.OpenProcess(0x1000 | 0x100000, False, row["pid"]))
        try:
            summary["positiveBinding"] = dict(
                sameIdentity=J.creation(handle) == row["creation"],
                member=J.member(handle, a["job"]),
                noOtherMember=not J.member(handle, b["job"]),
            )
        finally:
            J.K.CloseHandle(handle)
        assert all(summary["positiveBinding"].values())
        summary["positiveBinding"]["result"] = finish(caller)
        for mode, event in [
            ("before", "before-forward"),
            ("half", "half-forwarded"),
            ("after", "after-forwarded"),
        ]:
            rr = relay(label + "-" + mode, a, mode)
            relays.append(rr)
            caller = launch(label + "-" + mode, rr["ready"]["socket"], a, b, seconds=3)
            callers.append(caller)
            gate = S.wait_row(rr["record"], event)
            if mode == "after":
                S.wait_row(caller["record"], "pe-start")
            pe_before = J.rows(caller["record"])
            held_pe = []
            for row in pe_before:
                if row["event"] == "pe-start":
                    handle = J.check(
                        J.K.OpenProcess(0x1000 | 0x100000, False, row["pid"])
                    )
                    assert J.creation(handle) == row["creation"] and J.member(
                        handle, a["job"]
                    )
                    held_pe.append((handle, row["pid"], row["creation"]))
            killed = pidfd_kill(rr["ready"])
            rr["process"].communicate(timeout=10)
            if mode == "after":
                # No Unix-stream reply exists in this WSL path; lose the actual caller instead.
                ready = S.wait_row(
                    Path(str(caller["record"]) + ".linux"), "caller-ready"
                )
                caller_lost = pidfd_kill(ready)
                cancellation = terminate_job(a)
            result = finish(caller)
            summary[mode] = dict(
                gate=gate,
                relayKill=killed,
                peBeforeReply=pe_before,
                result=result,
                classification="unknown"
                if mode != "before"
                else "fixture-proves-no-forward",
            )
            if mode == "before":
                assert not result["pe"]
            if mode == "half":
                assert gate["total"] == 1
            if mode == "after":
                summary[mode]["callerLost"] = caller_lost
                summary[mode]["cancellation"] = cancellation
                summary[mode]["exactPeWaits"] = [
                    dict(
                        pid=pid,
                        creation=created,
                        wait=J.K.WaitForSingleObject(handle, 5000),
                    )
                    for handle, pid, created in held_pe
                ]
                assert all(fact["wait"] == 0 for fact in summary[mode]["exactPeWaits"])
                assert pe_before and not any(
                    x["event"] == "caller-exit" for x in result["linux"]
                )
            for handle, _, _ in held_pe:
                J.K.CloseHandle(handle)
        # Kill A before its first pin, preserving the raw pin failure.
        summary["cancelA"] = terminate_job(a)
        record = BASE / (label + "-dead-before-pin.relay.jsonl")
        process = subprocess.run(
            S.command(
                (BASE / "opaque_gate.py").read_text(),
                a["ready"]["socket"],
                S.linux_path(record),
                GUEST_ROOT,
                "normal",
                S.linux_path(BASE / (label + "-unused")),
            ),
            capture_output=True,
            text=True,
            timeout=10,
        )
        summary["deadBeforePin"] = dict(
            code=process.returncode,
            stdout=process.stdout,
            stderr=process.stderr,
            rows=J.rows(record),
        )
        assert any(x["event"] == "pin-failed" for x in J.rows(record))
        # Caller has retained a path but has not invoked PE yet.
        retained = launch(
            label + "-retained", good["ready"]["socket"], a, b, paused=True
        )
        callers.append(retained)
        summary["retireRelay"] = pidfd_kill(good["ready"])
        good["process"].communicate(timeout=10)
        retained["gate"].touch()
        summary["retainedSocket"] = finish(retained)
        assert not summary["retainedSocket"]["pe"]
        unlinked = launch(
            label + "-unlinked", good["ready"]["socket"], a, b, paused=True
        )
        callers.append(unlinked)
        summary["unlink"] = guest(
            "import json,os,sys; os.unlink(sys.argv[1]); print(json.dumps(dict(unlinked=sys.argv[1])))",
            good["ready"]["socket"],
        )
        unlinked["gate"].touch()
        summary["unlinkedSocket"] = finish(unlinked)
        assert summary["unlinkedSocket"]["pe"] and all(
            not any(row["memberships"].values())
            for row in summary["unlinkedSocket"]["pe"]
            if row["event"] == "pe-start"
        )
        summary["passedObservedGates"] = True
    finally:
        summary["cleanup"] = dict(relays=[], windows=[], anchors=[])
        for rr in relays:
            if rr["process"].poll() is None:
                summary["cleanup"]["relays"].append(pidfd_kill(rr["ready"]))
                rr["process"].communicate(timeout=10)
        for caller in callers:
            caller["gate"].touch()
            if caller["process"].poll() is None:
                try:
                    finish(caller)
                except subprocess.TimeoutExpired:
                    ready = S.wait_row(
                        Path(str(caller["record"]) + ".linux"), "caller-ready"
                    )
                    summary["cleanup"]["relays"].append(pidfd_kill(ready))
            for row in J.rows(caller["record"]):
                if row["event"] == "pe-start":
                    summary["cleanup"]["windows"].append(
                        J.own_fact(row["pid"], row["creation"], True)
                    )
        for anchor in (a, b):
            summary["cleanup"]["anchors"].append(terminate_job(anchor))
            ready = anchor["ready"]
            check = guest(
                "import json,os,sys; p='/proc/'+sys.argv[1]+'/stat'; actual=open(p).read().split()[21] if os.path.exists(p) else None; print(json.dumps(dict(pid=int(sys.argv[1]),starttime=actual,expected=sys.argv[2],same=actual==sys.argv[2])))",
                str(ready["pid"]),
                ready["starttime"],
            )
            if check["same"]:
                pidfd_kill(ready)
            for handle in (anchor["pi"].hThread, anchor["pi"].hProcess, anchor["job"]):
                J.K.CloseHandle(handle)
        summary["cleanup"]["privatePaths"] = guest(
            "import json,os,sys; root=sys.argv[1]; dirs=json.loads(sys.argv[2]); result=[]; exec(\"for d in dirs:\\n assert d.startswith(root+'/relay-')\\n p=d+'/interop'\\n if os.path.lexists(p): os.unlink(p)\\n os.rmdir(d)\\n result.append(dict(directory=d,exists=os.path.exists(d)))\"); print(json.dumps(result))",
            GUEST_ROOT,
            json.dumps([rr["ready"]["directory"] for rr in relays]),
        )
        path = BASE / (label + "-summary.json")
        path.write_text(
            json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        print(path)


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "pe":
        record, a_name, b_name, seconds = sys.argv[2:]
        memberships = {}
        for name in (a_name, b_name):
            job = J.check(J.K.OpenJobObjectW(4, False, name))
            memberships[name] = J.member(J.K.GetCurrentProcess(), job)
            J.K.CloseHandle(job)
        J.emit(
            record,
            "pe-start",
            creation=J.creation(J.K.GetCurrentProcess()),
            memberships=memberships,
        )
        time.sleep(float(seconds))
        J.emit(record, "pe-finished")
    else:
        run()
