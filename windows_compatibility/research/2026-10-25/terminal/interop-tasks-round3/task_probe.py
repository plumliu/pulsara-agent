"""Windows orchestration only; the original Linux terminal owner is unchanged."""

import ctypes as c
import importlib.util
import json
import msvcrt
import os
from pathlib import Path
import subprocess
import sys
import time
import traceback
import threading
import uuid

BASE = Path(__file__).resolve().parent
ROOT = BASE.parents[3]
spec = importlib.util.spec_from_file_location(
    "job_fixture", ROOT / "output/windows-g0/interop_jobs/round2/job_probe.py"
)
J = importlib.util.module_from_spec(spec)
spec.loader.exec_module(J)
WSL = os.environ["SystemRoot"] + r"\System32\wsl.exe"
LINUX_ROOT = "/home/plumwsl/workspaces/pulsara_agent"
ENV = {key: value for key, value in os.environ.items() if key.upper() != "WSLENV"}


def lp(path):
    return str(path).replace("\\", "/").replace("C:", "/mnt/c")


def command(*args):
    return [
        WSL,
        "-d",
        "Ubuntu",
        "-u",
        "plumwsl",
        "--cd",
        LINUX_ROOT,
        "--exec",
        LINUX_ROOT + "/.venv/bin/python",
        "-u",
        *args,
    ]


def wait_event(path, event, seconds=20):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        found = [row for row in J.rows(Path(path)) if row["event"] == event]
        if found:
            return found[-1]
        time.sleep(0.02)
    raise TimeoutError((str(path), event))


def pe(record, job_name, role, scenario):
    job = J.check(J.K.OpenJobObjectW(4, False, job_name))
    in_job = J.member(J.K.GetCurrentProcess(), job)
    J.K.CloseHandle(job)
    J.emit(
        record,
        "pe-start",
        role=role,
        creation=J.creation(J.K.GetCurrentProcess()),
        inJob=in_job,
    )
    if role == "parent":
        args = [sys.executable, __file__, "pe", record, job_name, "child", scenario]
        child = subprocess.Popen(
            args,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=0x08000000,
        )
        wait_event(record, "child-ready")
        if scenario == "normal":
            J.emit(
                record, "parent-early-exit", childPid=child.pid, childStdio="DEVNULL"
            )
            return
        if scenario == "input":
            print("READY", flush=True)
            for line in sys.stdin:
                print("ACK:" + line.strip(), flush=True)
            print("EOF", flush=True)
            J.emit(record, "input-eof")
            child.wait()
        else:
            child.wait()
    else:
        J.emit(record, "child-ready")
        time.sleep(7 if scenario != "input" else 0.5)
    J.emit(record, "pe-finished", role=role)


def run_case(label, scenario, tty, patched=False, ab=None):
    prefix = BASE / label
    anchor_record = Path(str(prefix) + ".anchor.jsonl")
    relay_record = Path(str(prefix) + ".relay.jsonl")
    pe_record = Path(str(prefix) + ".pe.jsonl")
    manager_record = Path(str(prefix) + ".manager.jsonl")
    config_path = Path(str(prefix) + ".config.json")
    release = Path(str(prefix) + ".release")
    directory = "/home/plumwsl/.g0tasks/" + label
    job_name = "Local\\PulsaraG0Task-" + label
    job = J.check(J.K.CreateJobObjectW(None, job_name))
    limits = J.Extended()
    limits.BasicLimitInformation.LimitFlags = 0x2000
    J.check(J.K.SetInformationJobObject(job, 9, c.byref(limits), c.sizeof(limits)))
    rfd, wfd = os.pipe()
    os.set_handle_inheritable(msvcrt.get_osfhandle(rfd), True)
    carrier_log = open(str(prefix) + ".anchor-output", "wb")
    os.set_handle_inheritable(msvcrt.get_osfhandle(carrier_log.fileno()), True)
    si = J.SI()
    si.cb = c.sizeof(si)
    si.dwFlags = 0x100
    si.hStdInput = msvcrt.get_osfhandle(rfd)
    si.hStdOutput = si.hStdError = msvcrt.get_osfhandle(carrier_log.fileno())
    pi = J.PI()
    anchor_code = "import json,os,sys; f=open(sys.argv[1],'a',buffering=1); f.write(json.dumps(dict(event='anchor-ready',pid=os.getpid(),starttime=open('/proc/self/stat').read().rsplit(')',1)[1].split()[19],socket=os.environ['WSL_INTEROP']))+'\\n'); sys.stdin.buffer.read(); f.write(json.dumps(dict(event='anchor-eof'))+'\\n')"
    args = command("-c", anchor_code, lp(anchor_record))
    relay_process = manager_process = None
    held = []
    result = dict(label=label, scenario=scenario, tty=tty, patched=patched)
    scope_prefix = Path(str(prefix) + ".scope")
    broker_stop = threading.Event()
    broker = None
    pipe_lock = threading.Lock()

    def close_anchor():
        nonlocal wfd
        with pipe_lock:
            if wfd is not None:
                os.close(wfd)
                wfd = None

    def scope_broker():
        sealed = False
        cancelled = False
        exact = {}
        try:
            while not broker_stop.is_set():
                for fact in J.job_facts(job):
                    if (
                        fact.get("exists")
                        and (fact["pid"], fact["creation"]) not in exact
                    ):
                        handle = J.K.OpenProcess(0x1000 | 0x100000, False, fact["pid"])
                        if handle:
                            if J.creation(handle) == fact["creation"]:
                                exact[(fact["pid"], fact["creation"])] = handle
                            else:
                                J.K.CloseHandle(handle)
                normal = Path(str(scope_prefix) + ".normal").exists()
                cancel = Path(str(scope_prefix) + ".cancel").exists()
                if (
                    Path(str(scope_prefix) + ".observe").exists()
                    and not Path(str(scope_prefix) + ".observed.json").exists()
                ):
                    observed = dict(
                        time=time.time(),
                        members=J.job_facts(job),
                        accounting=J.accounting(job),
                        held=[
                            dict(
                                pid=pid,
                                creation=created,
                                waitResult=J.K.WaitForSingleObject(handle, 0),
                            )
                            for (pid, created), handle in exact.items()
                        ],
                    )
                    temporary = Path(str(scope_prefix) + ".observed.tmp")
                    temporary.write_text(json.dumps(observed), encoding="utf-8")
                    temporary.replace(Path(str(scope_prefix) + ".observed.json"))
                if normal or cancel:
                    if not sealed:
                        result["brokerBeforeSeal"] = dict(
                            members=J.job_facts(job),
                            accounting=J.accounting(job),
                            time=time.time(),
                        )
                        subprocess.run(
                            command(
                                "-c",
                                "from pathlib import Path; import sys; Path(sys.argv[1],'sealed').touch()",
                                directory,
                            ),
                            env=ENV,
                            check=True,
                            timeout=10,
                        )
                        Path(str(scope_prefix) + ".sealed").touch()
                        close_anchor()
                        sealed = True
                        result["brokerAfterAnchorEof"] = dict(
                            members=J.job_facts(job),
                            accounting=J.accounting(job),
                            time=time.time(),
                        )
                    if cancel and not cancelled:
                        J.check(J.K.TerminateJobObject(job, 93))
                        cancelled = True
                    if J.accounting(job)["ActiveProcesses"] == 0 and all(
                        J.K.WaitForSingleObject(handle, 0) == 0
                        for handle in exact.values()
                    ):
                        if not Path(str(scope_prefix) + ".joined").exists():
                            result["brokerJoinedHandles"] = [
                                dict(
                                    pid=pid,
                                    creation=created,
                                    waitResult=J.K.WaitForSingleObject(handle, 0),
                                )
                                for (pid, created), handle in exact.items()
                            ]
                            result["brokerTerminatedJob"] = cancelled
                            Path(str(scope_prefix) + ".joined").touch()
                time.sleep(0.005)
        finally:
            for handle in exact.values():
                J.K.CloseHandle(handle)

    try:
        os.environ.pop("WSLENV", None)
        J.check(
            J.K.CreateProcessW(
                args[0],
                c.create_unicode_buffer(subprocess.list2cmdline(args)),
                None,
                None,
                True,
                0x08000004,
                None,
                None,
                c.byref(si),
                c.byref(pi),
            )
        )
        os.close(rfd)
        rfd = None
        os.set_handle_inheritable(msvcrt.get_osfhandle(carrier_log.fileno()), False)
        J.check(J.K.AssignProcessToJobObject(job, pi.hProcess))
        assert not anchor_record.exists()
        assert J.K.ResumeThread(pi.hThread) != 0xFFFFFFFF
        anchor = wait_event(anchor_record, "anchor-ready")
        subprocess.run(
            command(
                "-c",
                "import os,sys; os.makedirs(sys.argv[1],mode=0o700,exist_ok=True)",
                str(Path(directory).parent).replace("\\", "/"),
            ),
            env=ENV,
            check=True,
            timeout=10,
        )
        relay_process = subprocess.Popen(
            command(
                lp(BASE / "guest_probe.py"),
                "relay",
                anchor["socket"],
                directory,
                lp(relay_record),
            ),
            env=ENV,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        relay = wait_event(relay_record, "relay-ready")
        assert relay["mode"] == "0o700"
        config = dict(
            record=lp(manager_record),
            socket=relay["socket"],
            pe_record=lp(pe_record),
            release=lp(release),
            scenario=scenario,
            tty=tty,
            patched=patched,
            scope_prefix=lp(scope_prefix),
            pe_args=[
                lp(sys.executable),
                str(BASE / "task_probe.py"),
                "pe",
                str(pe_record),
                job_name,
                "parent",
                scenario,
            ],
        )
        if ab:
            ab["configs"][scenario] = config
            ab["barrier"].wait(timeout=20)
            if scenario == "cancel":
                config["peer"] = ab["configs"]["normal"]
        config_path.write_text(json.dumps(config), encoding="utf-8")
        if patched:
            broker = threading.Thread(target=scope_broker)
            broker.start()
        if not ab or scenario == "cancel":
            manager_process = subprocess.Popen(
                command(lp(BASE / "guest_probe.py"), "manager", lp(config_path)),
                env=ENV,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
        wait_event(manager_record, "observation-ready", seconds=25)
        starts = [row for row in J.rows(pe_record) if row["event"] == "pe-start"]
        assert len(starts) == 2 and all(row["inJob"] for row in starts)
        for row in J.job_facts(job):
            handle = J.check(J.K.OpenProcess(0x1000 | 0x100000, False, row["pid"]))
            assert J.creation(handle) == row["creation"]
            held.append((row, handle))
        result["ownerObservation"] = J.rows(manager_record)
        result["jobAtOwnerObservation"] = J.accounting(job)
        result["exactPeAtOwnerObservation"] = [
            dict(role=row["role"], fact=J.own_fact(row["pid"], row["creation"]))
            for row in starts
        ]
        # Seal retains the socket pathname until no Linux request source remains.
        subprocess.run(
            command(
                "-c",
                "from pathlib import Path; import sys; Path(sys.argv[1],'sealed').touch()",
                directory,
            ),
            env=ENV,
            check=True,
            timeout=10,
        )
        close_anchor()
        if scenario == "cancel" and not patched:
            J.check(J.K.TerminateJobObject(job, 93))
            result["explicitResearchCleanupTermination"] = True
        deadline = time.monotonic() + 15
        while J.accounting(job)["ActiveProcesses"] and time.monotonic() < deadline:
            time.sleep(0.02)
        result["jobAfterWindowsJoin"] = J.accounting(job)
        result["heldWaits"] = [
            dict(**row, waitResult=J.K.WaitForSingleObject(handle, 5000))
            for row, handle in held
        ]
        result["peEvents"] = J.rows(pe_record)
        assert result["jobAfterWindowsJoin"]["ActiveProcesses"] == 0
        assert all(row["waitResult"] == 0 for row in result["heldWaits"])
        release.touch()
        if manager_process:
            stdout, stderr = manager_process.communicate(timeout=15)
            result["managerExit"] = dict(
                code=manager_process.returncode,
                stdout=stdout.decode(errors="replace"),
                stderr=stderr.decode(errors="replace"),
            )
            assert manager_process.returncode == 0
        elif ab:
            wait_event(manager_record, "manager-cleanup-done")
        result["completedObservation"] = True
    except BaseException:
        result["failure"] = traceback.format_exc()
    finally:
        release.touch()
        close_anchor()
        if rfd is not None:
            os.close(rfd)
        if J.accounting(job)["ActiveProcesses"]:
            J.check(J.K.TerminateJobObject(job, 94))
        Path(str(scope_prefix) + ".joined").touch()
        if manager_process:
            try:
                stdout, stderr = manager_process.communicate(timeout=15)
                result["managerCleanupExit"] = dict(
                    code=manager_process.returncode,
                    stdout=stdout.decode(errors="replace"),
                    stderr=stderr.decode(errors="replace"),
                )
            except subprocess.TimeoutExpired:
                result["managerCleanupTimeout"] = True
        if relay_process:
            ready = [
                row for row in J.rows(relay_record) if row["event"] == "relay-ready"
            ]
            if ready:
                rr = ready[-1]
                code = "import json,os,signal,sys; pid=int(sys.argv[1]); expected=sys.argv[2]; fd=os.pidfd_open(pid); actual=open('/proc/'+str(pid)+'/stat').read().rsplit(')',1)[1].split()[19]; assert actual==expected; signal.pidfd_send_signal(fd,signal.SIGKILL); os.close(fd)"
                clean = subprocess.run(
                    command("-c", code, str(rr["pid"]), rr["starttime"]),
                    env=ENV,
                    capture_output=True,
                    text=True,
                    timeout=10,
                )
                result["relayExactPidfdCleanup"] = dict(
                    code=clean.returncode, stderr=clean.stderr
                )
                relay_process.communicate(timeout=10)
                code = "from pathlib import Path; import sys; d=Path(sys.argv[1]); root=Path(sys.argv[2]); assert d.parent==root and d.name==sys.argv[3]; (d/'interop').unlink(); (d/'sealed').unlink(missing_ok=True); d.rmdir(); print('removed owned private socket after manager joined')"
                clean = subprocess.run(
                    command("-c", code, directory, "/home/plumwsl/.g0tasks", label),
                    env=ENV,
                    capture_output=True,
                    text=True,
                    timeout=10,
                )
                result["privateSocketCleanup"] = dict(
                    code=clean.returncode, stdout=clean.stdout, stderr=clean.stderr
                )
        broker_stop.set()
        if broker:
            broker.join(timeout=15)
        J.K.WaitForSingleObject(pi.hProcess, 5000)
        result["cleanupAccounting"] = J.accounting(job)
        result["cleanupMembers"] = J.job_facts(job)
        for _, handle in held:
            J.K.CloseHandle(handle)
        for handle in (pi.hThread, pi.hProcess, job):
            if handle:
                J.K.CloseHandle(handle)
        carrier_log.close()
        Path(str(prefix) + "-summary.json").write_text(
            json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    return result


def run(patched=False):
    label = ("patched-" if patched else "baseline-") + uuid.uuid4().hex[:8]
    results = []
    for scenario in ("normal", "cancel", "input"):
        for tty in (False, True):
            result = run_case(
                f"{label}-{scenario}-{'pty' if tty else 'pipe'}", scenario, tty, patched
            )
            results.append(result)
            print(
                json.dumps(
                    {
                        key: result.get(key)
                        for key in (
                            "label",
                            "completedObservation",
                            "failure",
                            "cleanupAccounting",
                        )
                    }
                ),
                flush=True,
            )
    (BASE / (label + "-matrix.json")).write_text(
        json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def run_ab():
    from concurrent.futures import ThreadPoolExecutor

    label = "shared-ab-" + uuid.uuid4().hex[:8]
    ab = dict(configs={}, barrier=threading.Barrier(2))
    with ThreadPoolExecutor(max_workers=2) as pool:
        a = pool.submit(run_case, label + "-A", "cancel", False, True, ab)
        b = pool.submit(run_case, label + "-B", "normal", True, True, ab)
        results = [a.result(), b.result()]
    (BASE / (label + "-matrix.json")).write_text(
        json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(
        json.dumps(
            [
                dict(
                    label=item["label"],
                    completed=item.get("completedObservation"),
                    failure=item.get("failure"),
                )
                for item in results
            ]
        )
    )


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "pe":
        pe(*sys.argv[2:])
    else:
        if "--ab" in sys.argv:
            run_ab()
        else:
            run(patched="--patched" in sys.argv)
