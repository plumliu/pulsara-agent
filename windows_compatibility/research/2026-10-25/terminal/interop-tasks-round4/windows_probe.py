"""Output-only Win32 scope owner and deterministic real-manager experiment driver."""

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
import uuid

BASE = Path(__file__).resolve().parent
REPO = BASE.parents[3]
spec = importlib.util.spec_from_file_location(
    "job_fixture", REPO / "output/windows-g0/interop_jobs/round2/job_probe.py"
)
J = importlib.util.module_from_spec(spec)
spec.loader.exec_module(J)
LINUX_REPO = "/home/plumwsl/workspaces/pulsara_agent"
WSL = os.environ["SystemRoot"] + r"\System32\wsl.exe"
ENV = {key: value for key, value in os.environ.items() if key.upper() != "WSLENV"}
FIXTURE = REPO / "output/windows-g0/interop_races_r3/round4/relay_fixture.py"


def lp(path):
    return str(path).replace("\\", "/").replace("C:", "/mnt/c")


def guest(*args):
    return [
        WSL,
        "-d",
        "Ubuntu",
        "-u",
        "plumwsl",
        "--cd",
        LINUX_REPO,
        "--exec",
        LINUX_REPO + "/.venv/bin/python",
        "-u",
        *args,
    ]


def atomic_json(path, value):
    path = Path(path)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value), encoding="utf-8")
    temporary.replace(path)


def wait_until(predicate, seconds=35):
    deadline = time.monotonic() + seconds
    while not predicate():
        if time.monotonic() >= deadline:
            raise TimeoutError("round4 driver observation deadline")
        time.sleep(0.01)


def pe(record, job_name, role, scenario, prefix):
    job = J.check(J.K.OpenJobObjectW(4, False, job_name))
    in_job = J.member(J.K.GetCurrentProcess(), job)
    J.K.CloseHandle(job)
    J.emit(
        record,
        "pe-start",
        role=role,
        scenario=scenario,
        creation=J.creation(J.K.GetCurrentProcess()),
        inJob=in_job,
    )
    if role == "diagnostic":
        wait_until(lambda: Path(prefix + ".diagnostic-accepted").exists())
        J.emit(record, "diagnostic-finished")
        return
    if role == "late":
        return
    if role == "parent":
        child = subprocess.Popen(
            [
                sys.executable,
                __file__,
                "pe",
                record,
                job_name,
                "child",
                scenario,
                prefix,
            ],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=0x08000000,
        )
        wait_until(
            lambda: any(row["event"] == "child-ready" for row in J.rows(Path(record)))
        )
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
        J.emit(record, "child-ready")
        time.sleep(6 if scenario != "input" else 0.3)
    J.emit(record, "pe-finished", role=role)


class WindowsScope:
    def __init__(self, config):
        self.config = config
        self.prefix = config["windows_prefix"]
        self.job = J.check(J.K.CreateJobObjectW(None, config["job_name"]))
        limits = J.Extended()
        limits.BasicLimitInformation.LimitFlags = 0x2000
        J.check(
            J.K.SetInformationJobObject(self.job, 9, c.byref(limits), c.sizeof(limits))
        )
        self.pi = None
        self.stdin_fd = None
        self.output = None
        self.held = {}
        self.events = []
        self.started = False
        self.joined = False
        self.terminated = False
        self.diagnostic_accepted = False

    def path(self, suffix):
        return Path(self.prefix + suffix)

    def log(self, event, **facts):
        item = dict(event=event, time=time.time(), **facts)
        self.events.append(item)
        with open(self.config["windows_record"], "a", encoding="utf-8") as stream:
            stream.write(json.dumps(item) + "\n")

    def start_anchor(self):
        args = guest(lp(FIXTURE), "anchor", self.config["config_path"])
        rfd, self.stdin_fd = os.pipe()
        os.set_handle_inheritable(msvcrt.get_osfhandle(rfd), True)
        self.output = open(self.prefix + ".anchor-output", "wb")
        os.set_handle_inheritable(msvcrt.get_osfhandle(self.output.fileno()), True)
        si = J.SI()
        si.cb = c.sizeof(si)
        si.dwFlags = 0x100
        si.hStdInput = msvcrt.get_osfhandle(rfd)
        si.hStdOutput = si.hStdError = msvcrt.get_osfhandle(self.output.fileno())
        self.pi = J.PI()
        os.environ.pop("WSLENV", None)
        try:
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
                    c.byref(self.pi),
                )
            )
        finally:
            os.close(rfd)
            os.set_handle_inheritable(msvcrt.get_osfhandle(self.output.fileno()), False)
        J.check(J.K.AssignProcessToJobObject(self.job, self.pi.hProcess))
        self.log(
            "anchor-assigned-before-resume",
            carrierPid=self.pi.dwProcessId,
            carrierCreation=J.creation(self.pi.hProcess),
            carrierInJob=J.member(self.pi.hProcess, self.job),
            guestReadyExists=Path(self.config["windows_anchor_record"]).exists(),
        )
        assert not Path(self.config["windows_anchor_record"]).exists()
        assert J.K.ResumeThread(self.pi.hThread) != 0xFFFFFFFF
        self.started = True

    def close_stdin(self):
        if self.stdin_fd is not None:
            os.close(self.stdin_fd)
            self.stdin_fd = None

    def hold_current(self):
        for fact in J.job_facts(self.job):
            identity = (fact.get("pid"), fact.get("creation"))
            if fact.get("exists") and identity not in self.held:
                handle = J.K.OpenProcess(0x1000 | 0x100000, False, fact["pid"])
                if handle:
                    if J.creation(handle) == fact["creation"]:
                        self.held[identity] = handle
                    else:
                        J.K.CloseHandle(handle)

    def facts(self):
        return dict(
            accounting=J.accounting(self.job),
            members=J.job_facts(self.job),
            held=[
                dict(
                    pid=pid,
                    creation=created,
                    waitResult=J.K.WaitForSingleObject(handle, 0),
                )
                for (pid, created), handle in self.held.items()
            ],
            terminateJobCalled=self.terminated,
        )

    def tick(self):
        if (
            self.path(".need-anchor").exists()
            and not self.started
            and not self.path(".job-cancel").exists()
        ):
            self.start_anchor()
        self.hold_current()
        diagnostic_rows = [
            row
            for row in J.rows(Path(self.config["windows_diagnostic_record"]))
            if row["event"] == "pe-start"
        ]
        if (
            diagnostic_rows
            and not self.diagnostic_accepted
            and not self.path(".job-cancel").exists()
        ):
            row = diagnostic_rows[0]
            handle = J.check(J.K.OpenProcess(0x1000 | 0x100000, False, row["pid"]))
            try:
                fact = dict(
                    pid=row["pid"],
                    creation=row["creation"],
                    sameCreation=J.creation(handle) == row["creation"],
                    member=J.member(handle, self.job),
                    waitResult=J.K.WaitForSingleObject(handle, 0),
                )
                assert (
                    fact["sameCreation"]
                    and fact["member"]
                    and fact["waitResult"] == 258
                )
                self.log("host-verified-transferred-fd-job-binding", **fact)
            finally:
                J.K.CloseHandle(handle)
            self.path(".diagnostic-accepted").touch()
            self.diagnostic_accepted = True
        if self.path(".job-cancel").exists() and not self.joined:
            self.close_stdin()
            if not self.terminated:
                self.log("before-job-cancel", facts=self.facts())
                J.check(J.K.TerminateJobObject(self.job, 94))
                self.terminated = True
        elif self.path(".job-eof").exists() and self.stdin_fd is not None:
            self.log("before-normal-anchor-eof", facts=self.facts())
            self.close_stdin()
        if self.path(".observe").exists() and not self.path(".observed.json").exists():
            atomic_json(self.path(".observed.json"), self.facts())
        finishing = self.path(".job-cancel").exists() or self.path(".job-eof").exists()
        if finishing and not self.joined:
            facts = self.facts()
            if facts["accounting"]["ActiveProcesses"] == 0 and all(
                row["waitResult"] == 0 for row in facts["held"]
            ):
                atomic_json(self.path(".job-joined.json"), facts)
                self.log("windows-scope-joined", facts=facts)
                self.joined = True

    def cleanup(self):
        self.close_stdin()
        if J.accounting(self.job)["ActiveProcesses"]:
            J.check(J.K.TerminateJobObject(self.job, 95))
            self.terminated = True
        wait_until(lambda: J.accounting(self.job)["ActiveProcesses"] == 0, seconds=10)
        for handle in self.held.values():
            J.K.WaitForSingleObject(handle, 5000)
        facts = self.facts()
        if facts["accounting"]["ActiveProcesses"] == 0 and all(
            row["waitResult"] == 0 for row in facts["held"]
        ):
            atomic_json(self.path(".job-joined.json"), facts)
        for handle in self.held.values():
            J.K.CloseHandle(handle)
        if self.pi:
            J.K.CloseHandle(self.pi.hThread)
            J.K.CloseHandle(self.pi.hProcess)
        J.K.CloseHandle(self.job)
        if self.output:
            self.output.close()
        return facts


def run_case(name, scenario, tty=False, gate=None, fault=None, close=False):
    label = name + "-" + uuid.uuid4().hex[:8]
    manager_record = BASE / (label + ".manager.jsonl")
    configuration = dict(
        scenario=scenario,
        fault=fault,
        manager_record=lp(manager_record),
        driver_prefix=lp(BASE / label),
        scopes=[],
    )
    tasks = (
        [("B", "normal", True), ("A", "cancel", False)]
        if scenario == "ab"
        else [("A", "normal" if scenario == "tail-cancel" else scenario, tty)]
    )
    scopes = []
    for role, workload, terminal in tasks:
        prefix = BASE / (label + "-" + role)
        pe_record = Path(str(prefix) + ".pe.jsonl")
        diagnostic_record = Path(str(prefix) + ".diagnostic.jsonl")
        late_record = Path(str(prefix) + ".late-pe.jsonl")
        relay_record = Path(str(prefix) + ".relay.jsonl")
        anchor_record = Path(str(prefix) + ".anchor.jsonl")
        scope_config_path = Path(str(prefix) + ".config.json")
        job_name = "Local\\PulsaraG0Round4-" + label + "-" + role
        args = [lp(sys.executable), str(BASE / "windows_probe.py"), "pe"]
        scope = dict(
            name=role,
            directory="/home/plumwsl/.g0t4-" + label + "-" + role,
            prefix=lp(prefix),
            windows_prefix=str(prefix),
            record=lp(relay_record),
            windows_relay_record=str(relay_record),
            anchor_record=lp(anchor_record),
            windows_anchor_record=str(anchor_record),
            fixture=lp(FIXTURE),
            config_path=lp(scope_config_path),
            gates=[gate] if gate and role == "A" else [],
            tty=terminal,
            job_name=job_name,
            windows_record=str(prefix) + ".windows.jsonl",
            pe_record=lp(pe_record),
            windows_pe_record=str(pe_record),
            windows_diagnostic_record=str(diagnostic_record),
            decision="terminal-decision:" + uuid.uuid4().hex[:24],
            diagnostic_args=args
            + [str(diagnostic_record), job_name, "diagnostic", workload, str(prefix)],
            late_record=lp(late_record),
            late_args=args
            + [str(late_record), job_name, "late", workload, str(prefix)],
            task_args=args
            + [str(pe_record), job_name, "parent", workload, str(prefix)],
        )
        scope_config_path.write_text(json.dumps(scope), encoding="utf-8")
        configuration["scopes"].append(scope)
        scopes.append(WindowsScope(scope))
    manager_config = BASE / (label + ".manager-config.json")
    manager_config.write_text(json.dumps(configuration), encoding="utf-8")
    result = dict(label=label, scenario=scenario, tty=tty, gate=gate, fault=fault)
    process = subprocess.Popen(
        guest(lp(BASE / "manager_probe.py"), lp(manager_config)),
        env=ENV,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    try:
        cancelled = False
        deadline = time.monotonic() + 70
        while process.poll() is None:
            for scope in scopes:
                scope.tick()
            target = scopes[-1]
            if not cancelled and (gate or scenario in {"cancel", "ab", "tail-cancel"}):
                if gate:
                    gated = any(
                        row.get("gate") == gate
                        or row["event"] == gate
                        or row.get("name") == gate
                        for row in J.rows(Path(target.config["windows_relay_record"]))
                        + J.rows(Path(target.config["windows_anchor_record"]))
                    )
                elif scenario == "tail-cancel":
                    gated = Path(str(BASE / label) + ".tail-ready").exists()
                else:
                    gated = any(
                        row["event"] == "child-ready"
                        for row in J.rows(Path(target.config["windows_pe_record"]))
                    )
                if gated:
                    Path(str(BASE / label) + (".close" if close else ".cancel")).touch()
                    result["cancelTriggeredAtGate"] = gate or "child-ready"
                    cancelled = True
            if time.monotonic() >= deadline:
                raise TimeoutError("whole research experiment observation deadline")
            time.sleep(0.005)
        output, error = process.communicate(timeout=10)
        result["managerProcess"] = dict(
            exit=process.returncode,
            stdout=output.decode(errors="replace"),
            stderr=error.decode(errors="replace"),
        )
        result["managerRows"] = J.rows(manager_record)
        result["scopes"] = [
            dict(
                config=scope.config,
                windowsEvents=scope.events,
                relayRows=J.rows(Path(scope.config["windows_relay_record"])),
                anchorRows=J.rows(Path(scope.config["windows_anchor_record"])),
                peRows=J.rows(Path(scope.config["windows_pe_record"])),
                diagnosticRows=J.rows(Path(scope.config["windows_diagnostic_record"])),
            )
            for scope in scopes
        ]
        assert process.returncode == 0, result["managerProcess"]
        assert any(row["event"] == "manager-finished" for row in result["managerRows"])
        result["completedObservation"] = True
    except BaseException:
        result["failure"] = traceback.format_exc()
        for scope in scopes:
            scope.path(".cancel").touch()
            scope.path(".job-cancel").touch()
        cleanup_deadline = time.monotonic() + 20
        while process.poll() is None and time.monotonic() < cleanup_deadline:
            for scope in scopes:
                scope.tick()
            time.sleep(0.01)
        if process.poll() is None:
            result["managerStillRunning"] = True
        else:
            output, error = process.communicate()
            result["failureManagerProcess"] = dict(
                exit=process.returncode,
                stdout=output.decode(errors="replace"),
                stderr=error.decode(errors="replace"),
            )
    finally:
        result["cleanupWindows"] = [scope.cleanup() for scope in scopes]
        (BASE / (label + "-summary.json")).write_text(
            json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        print(
            json.dumps(
                {
                    key: result.get(key)
                    for key in (
                        "label",
                        "completedObservation",
                        "failure",
                        "managerProcess",
                        "managerStillRunning",
                    )
                }
            ),
            flush=True,
        )
    return result


def run(group):
    if group == "smoke":
        cases = [("normal-pty", "normal", True, None, None, False)]
    elif group == "startup":
        cases = [
            (stage, "cancel", False, stage, None, stage == "anchor-after-pin")
            for stage in (
                "anchor-before-pin",
                "anchor-after-pin",
                "before-bind",
                "after-bind",
            )
        ]
    elif group == "forward":
        cases = [
            (stage, "cancel", stage == "half-forwarded", stage, None, False)
            for stage in ("before-forward", "half-forwarded", "after-forwarded")
        ]
    elif group == "faults":
        cases = [
            (fault, "cancel", fault == "watcher-start", None, fault, False)
            for fault in ("reader-start", "watcher-start")
        ]
    elif group == "watcher-fault":
        cases = [("watcher-start", "cancel", True, None, "watcher-start", False)]
    elif group == "upstream":
        cases = [
            (stage, "cancel", False, stage, None, False)
            for stage in ("before-upstream", "after-upstream")
        ]
    elif group == "half-only":
        cases = [("half-forwarded", "cancel", True, "half-forwarded", None, False)]
    elif group == "tail-cancel":
        cases = [("tail-cancel", "tail-cancel", False, None, None, False)]
    elif group == "minimum":
        cases = [
            ("input-pipe", "input", False, None, None, False),
            ("input-pty", "input", True, None, None, False),
            ("shared-ab", "ab", False, None, None, False),
        ]
    else:
        raise ValueError(group)
    results = [run_case(*case) for case in cases]
    path = BASE / (group + "-" + uuid.uuid4().hex[:8] + "-results.json")
    path.write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
    print(path)


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "pe":
        pe(*sys.argv[2:])
    else:
        run(sys.argv[1] if len(sys.argv) > 1 else "smoke")
