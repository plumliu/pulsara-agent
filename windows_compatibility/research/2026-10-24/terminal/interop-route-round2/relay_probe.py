"""Research-only opaque Unix byte forwarding, not an interop implementation."""

import json
from pathlib import Path
import subprocess
import time
import uuid
import socket_probe as S

BASE = Path(__file__).resolve().parent
GUEST_ROOT = (
    "/home/plumwsl/workspaces/pulsara_agent/output/windows-g0/interop_review/round2"
)
RELAY = r"""import asyncio,json,os,sys,tempfile
async def main():
 upstream,record,root=sys.argv[1:4]
 os.makedirs(root,exist_ok=True)
 directory=tempfile.mkdtemp(prefix='byte-relay-',dir=root)
 path=directory+'/interop'
 fd=os.open(upstream,os.O_PATH|os.O_CLOEXEC)
 pinned='/proc/'+str(os.getpid())+'/fd/'+str(fd)
 f=open(record,'a',buffering=1)
 def log(event,**kw): f.write(json.dumps(dict(event=event,**kw))+'\n')
 async def connected(reader,writer):
  remote=None
  try:
   other,remote=await asyncio.open_unix_connection(pinned)
   async def pump(source,destination):
    total=0
    while True:
     chunk=await source.read(65536)
     if not chunk: break
     total+=len(chunk); destination.write(chunk); await destination.drain()
    if destination.can_write_eof(): destination.write_eof()
    return total
   counts=await asyncio.gather(pump(reader,remote),pump(other,writer))
   log('relay-connection-complete',counts=counts)
  except Exception as e: log('relay-connection-error',type=type(e).__name__,message=str(e))
  finally:
   for item in [writer,remote]:
    if item: item.close()
 server=await asyncio.start_unix_server(connected,path=path)
 log('relay-ready',pid=os.getpid(),starttime=open('/proc/self/stat').read().split()[21],socket=path,directory=directory,mode=oct(os.stat(directory).st_mode&0o777),upstream=upstream,pinned=pinned,inode=os.stat(pinned).st_ino)
 async with server: await server.serve_forever()
asyncio.run(main())
"""


def relay(label, anchor):
    record = BASE / (label + ".relay.jsonl")
    process = subprocess.Popen(
        S.command(RELAY, anchor["ready"]["socket"], S.linux_path(record), GUEST_ROOT),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    ready = S.wait_row(record, "relay-ready")
    return dict(process=process, ready=ready, record=record)


def exact_kill(ready):
    code = "import json,os,signal,sys; pid=int(sys.argv[1]); expected=sys.argv[2]; p='/proc/'+str(pid)+'/stat'; actual=open(p).read().split()[21] if os.path.exists(p) else None; same=actual==expected; print(json.dumps(dict(pid=pid,starttime=actual,sameIdentity=same))); os.kill(pid,signal.SIGKILL) if same else None"
    return subprocess.run(
        S.command(code, str(ready["pid"]), ready["starttime"]),
        capture_output=True,
        text=True,
        timeout=10,
    )


def path_fact(path):
    code = "import json,os,stat,sys; p=sys.argv[1]; print(json.dumps(dict(path=p,exists=os.path.exists(p),isSocket=stat.S_ISSOCK(os.stat(p).st_mode) if os.path.exists(p) else False)))"
    return json.loads(
        subprocess.check_output(S.command(code, path), text=True, timeout=10)
    )


def run():
    label = "relay-" + uuid.uuid4().hex[:8]
    a = S.anchor(label + "-A")
    b = S.anchor(label + "-B")
    relays = []
    records = []
    runners = []
    summary = {"label": label}
    try:
        ra = relay(label + "-A", a)
        relays.append(ra)
        rb = relay(label + "-B", b)
        relays.append(rb)
        summary["relayReady"] = {"A": ra["ready"], "B": rb["ready"]}
        for key, rr in [("A", ra), ("B", rb)]:
            record = BASE / (label + "-live-" + key + ".jsonl")
            records.append(record)
            runners.append(
                S.launch(
                    record,
                    rr["ready"]["socket"],
                    a,
                    b,
                    root_exit=key == "A",
                    seconds=25,
                )
            )
            S.wait_row(record, "child-ready")
        summary["beforeAnchorDeath"] = {
            "A": S.facts(records[0]),
            "B": S.facts(records[1]),
        }
        assert all(
            x["memberships"][a["name"]] and not x["memberships"][b["name"]]
            for x in summary["beforeAnchorDeath"]["A"]
        )
        assert all(
            x["memberships"][b["name"]] and not x["memberships"][a["name"]]
            for x in summary["beforeAnchorDeath"]["B"]
        )
        S.J.check(S.J.K.TerminateJobObject(a["job"], 99))
        assert S.J.K.WaitForSingleObject(a["pi"].hProcess, 5000) == 0
        time.sleep(0.3)
        summary["afterAnchorDeath"] = {
            "A": S.facts(records[0]),
            "B": S.facts(records[1]),
            "relaySocket": path_fact(ra["ready"]["socket"]),
        }
        assert all(not x["fact"]["exists"] for x in summary["afterAnchorDeath"]["A"])
        assert all(x["fact"]["exists"] for x in summary["afterAnchorDeath"]["B"])
        dead = BASE / (label + "-dead-anchor.jsonl")
        records.append(dead)
        runner = S.launch(dead, ra["ready"]["socket"], a, b, seconds=0.2)
        runners.append(runner)
        out, err = runner.communicate(timeout=15)
        summary["deadAnchorAttempt"] = {
            "linux": S.J.rows(Path(str(dead) + ".linux")),
            "stdout": out.decode(errors="replace"),
            "stderr": err.decode(errors="replace"),
            "facts": S.facts(dead),
        }
        assert not summary["deadAnchorAttempt"]["facts"]
        assert [
            x
            for x in summary["deadAnchorAttempt"]["linux"]
            if x["event"] == "runner-exit"
        ][-1]["code"] != 0
        summary["BBeforeRelayKill"] = S.facts(records[1])
        assert all(x["fact"]["exists"] for x in summary["BBeforeRelayKill"])
        killed = exact_kill(rb["ready"])
        rb["process"].communicate(timeout=10)
        summary["relayKilled"] = {
            "output": killed.stdout,
            "stderr": killed.stderr,
            "code": killed.returncode,
            "path": path_fact(rb["ready"]["socket"]),
        }
        assert summary["relayKilled"]["path"]["isSocket"]
        summary["BAfterRelayKill"] = S.facts(records[1])
        assert all(x["fact"]["exists"] for x in summary["BAfterRelayKill"])
        dead = BASE / (label + "-dead-relay.jsonl")
        records.append(dead)
        runner = S.launch(dead, rb["ready"]["socket"], a, b, seconds=0.2)
        runners.append(runner)
        out, err = runner.communicate(timeout=10)
        summary["deadRelayAttempt"] = {
            "linux": S.J.rows(Path(str(dead) + ".linux")),
            "stdout": out.decode(errors="replace"),
            "stderr": err.decode(errors="replace"),
            "facts": S.facts(dead),
        }
        assert not summary["deadRelayAttempt"]["facts"]
        assert [
            x
            for x in summary["deadRelayAttempt"]["linux"]
            if x["event"] == "runner-exit"
        ][-1]["code"] != 0
        for runner in runners:
            if runner.poll() is None:
                runner.communicate(timeout=15)
        summary["BFinished"] = [
            x for x in S.J.rows(records[1]) if x["event"] == "pe-finished"
        ]
        assert len(summary["BFinished"]) == 2
        summary["passedObservedBoundaries"] = True
    finally:
        for record in records:
            for row in S.J.rows(record):
                if row["event"] == "pe-start":
                    S.J.emit(
                        record,
                        "cleanup-exact-pe",
                        fact=S.J.own_fact(row["pid"], row["creation"], True),
                    )
        for rr in relays:
            exact_kill(rr["ready"])
            rr["process"].communicate(timeout=10)
            code = "import os,sys; p=sys.argv[1]; d=sys.argv[2]; root=sys.argv[3]; assert d.startswith(root+'/byte-relay-') and p==d+'/interop'; os.unlink(p); os.rmdir(d); print('removed exact private socket and directory after runners completed')"
            cleanup = subprocess.run(
                S.command(
                    code, rr["ready"]["socket"], rr["ready"]["directory"], GUEST_ROOT
                ),
                capture_output=True,
                text=True,
                timeout=10,
            )
            S.J.emit(
                rr["record"],
                "cleanup-private-socket",
                code=cleanup.returncode,
                output=cleanup.stdout,
                stderr=cleanup.stderr,
            )
            assert cleanup.returncode == 0
        S.cleanup_anchor(a)
        S.cleanup_anchor(b)
        path = BASE / (label + "-summary.json")
        path.write_text(
            json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    print(path)


if __name__ == "__main__":
    run()
