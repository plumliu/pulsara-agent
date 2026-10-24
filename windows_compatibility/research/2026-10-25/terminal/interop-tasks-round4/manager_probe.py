"""Research-only cross-domain scope composition around the real terminal owner."""

from dataclasses import asdict
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
import threading
import time
import traceback

import pulsara_agent.terminal_process.manager as M
from pulsara_agent.terminal_process.environment import TerminalEnvConfig
from pulsara_agent.terminal_process.models import TerminalProcessOrigin, TerminalRequest

LOG_LOCK = threading.Lock()


def append(path, event, **facts):
    with LOG_LOCK:
        with open(path, "a", encoding="utf-8") as stream:
            stream.write(
                json.dumps(dict(event=event, time=time.time(), **facts)) + "\n"
            )


def rows(path):
    source = Path(path)
    if not source.exists():
        return []
    return [json.loads(line) for line in source.read_text().splitlines() if line]


def wait_until(predicate, diagnostic_seconds=25):
    deadline = time.monotonic() + diagnostic_seconds
    while not predicate():
        if time.monotonic() >= deadline:
            raise TimeoutError("research observation deadline")
        time.sleep(0.005)


class ScopeCancelled(RuntimeError):
    pass


class Scope:
    def __init__(self, config, record):
        self.config = config
        self.record = record
        self.prefix = config["prefix"]
        self.receiver = None
        self.receiver_pidfd = None
        self.diagnostic = None
        self.cancelled = threading.Event()
        self.prepared = threading.Event()
        self.state = None
        self.receiver_output = None
        self.audit_lock = threading.Lock()
        self.audited_modes = set()

    def path(self, suffix):
        return Path(self.prefix + suffix)

    def log(self, event, **facts):
        append(self.record, event, scope=self.config["name"], **facts)

    def prepare(self):
        self.receiver_output = open(self.prefix + ".receiver-output", "ab", buffering=0)
        self.receiver = subprocess.Popen(
            [
                sys.executable,
                self.config["fixture"],
                "receiver",
                self.config["config_path"],
            ],
            stdin=subprocess.DEVNULL,
            stdout=self.receiver_output,
            stderr=subprocess.STDOUT,
            close_fds=True,
        )
        self.receiver_pidfd = os.pidfd_open(self.receiver.pid)
        self.log("scope-prepare-entered", receiverPid=self.receiver.pid)
        wait_until(
            lambda: (
                self.cancelled.is_set()
                or any(
                    row["event"] == "receiver-ready"
                    for row in rows(self.config["record"])
                )
            )
        )
        if self.cancelled.is_set():
            raise ScopeCancelled("cancelled before receiver admission")
        self.path(".need-anchor").touch()
        wait_until(
            lambda: (
                self.cancelled.is_set()
                or any(
                    row["event"] == "binding-complete"
                    for row in rows(self.config["record"])
                )
            )
        )
        if self.cancelled.is_set():
            raise ScopeCancelled("cancelled before binding")
        self.log("scope-fd-bound")
        environment = dict(
            os.environ, WSL_INTEROP=self.config["directory"] + "/interop"
        )
        self.diagnostic = subprocess.Popen(
            self.config["diagnostic_args"],
            env=environment,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
        )
        wait_until(
            lambda: (
                self.cancelled.is_set() or self.path(".diagnostic-accepted").exists()
            )
        )
        if self.cancelled.is_set():
            raise ScopeCancelled("cancelled during diagnostic binding")
        output, error = self.diagnostic.communicate(timeout=15)
        if self.diagnostic.returncode != 0:
            raise RuntimeError((self.diagnostic.returncode, output, error))
        self.path(".operation-admit").touch()
        self.prepared.set()
        self.log("scope-operation-admitted")

    def request_cancel(self):
        self.cancelled.set()
        self.path(".cancel").touch()
        self.log("scope-cancel-requested")
        wait_until(lambda: self.path(".sealed.json").exists())
        sealed = json.loads(self.path(".sealed.json").read_text())
        if sealed["mode"] != "cancel":
            wait_until(
                lambda: (
                    json.loads(self.path(".sealed.json").read_text())["mode"]
                    == "cancel"
                )
            )
        self.audit_seal("cancel")
        self.path(".job-cancel").touch()

    def audit_seal(self, mode):
        with self.audit_lock:
            if mode in self.audited_modes:
                return True
            sealed = json.loads(self.path(".sealed.json").read_text())
            if mode == "normal" and sealed["mode"] == "cancel":
                return False
            assert (
                sealed["mode"] == mode
                and sealed["taskCount"] == sealed["writerCount"] == 0
            )
            self.log(
                "relay-ack-observed",
                snapshot=sealed,
                crossDomainOutcome="unknown"
                if sealed["operationAnyForwarded"]
                else "no-operation-bytes-forwarded",
            )
            for gate in self.config["gates"]:
                self.path(".gate." + gate + ".release").touch()
            if sealed["socketExists"]:
                environment = dict(
                    os.environ, WSL_INTEROP=self.config["directory"] + "/interop"
                )
                late = subprocess.Popen(
                    self.config["late_args"],
                    env=environment,
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    start_new_session=True,
                )
                pidfd = os.pidfd_open(late.pid)
                try:
                    output, error = late.communicate(timeout=10)
                    wait_until(lambda: not M._process_group_exists(late.pid))
                    assert late.returncode != 0 and not rows(self.config["late_record"])
                    self.log(
                        "retained-socket-late-caller-rejected",
                        pid=late.pid,
                        exit=late.returncode,
                        stdout=output.decode(errors="replace"),
                        stderr=error.decode(errors="replace"),
                        socketStillExists=Path(
                            self.config["directory"], "interop"
                        ).exists(),
                        groupAlive=False,
                    )
                finally:
                    os.close(pidfd)
            seals = [
                row
                for row in rows(self.config["record"])
                if row["event"] == "relay-sealed"
            ]
            acknowledged = next(row for row in reversed(seals) if row["mode"] == mode)
            assert not any(
                row["event"] == "bytes-forwarded" and row["time"] > acknowledged["time"]
                for row in rows(self.config["record"])
            )
            current = json.loads(self.path(".sealed.json").read_text())
            assert (
                current["forwardedBytes"] == sealed["forwardedBytes"]
                and current["operationForwardedBytes"]
                == sealed["operationForwardedBytes"]
            )
            self.log(
                "relay-ack-remains-stable",
                mode=mode,
                operationForwardedBytes=current["operationForwardedBytes"],
            )
            self.audited_modes.add(mode)
            return True

    def join_windows(self, timeout=None):
        deadline = None if timeout is None else time.monotonic() + timeout
        while not self.path(".job-joined.json").exists():
            if deadline is not None and time.monotonic() >= deadline:
                return False
            time.sleep(0.005)
        return True

    def normal_join(self, timeout=None):
        if not self.cancelled.is_set():
            self.path(".seal").touch()
            deadline = None if timeout is None else time.monotonic() + timeout
            while not self.path(".sealed.json").exists():
                if deadline is not None and time.monotonic() >= deadline:
                    return False
                time.sleep(0.005)
            if not self.audit_seal("normal"):
                return self.join_windows(timeout)
            self.path(".job-eof").touch()
        return self.join_windows(timeout)

    def cancel_and_join(self):
        self.request_cancel()
        wait_until(lambda: self.join_windows(0))
        if self.diagnostic and self.diagnostic.poll() is None:
            try:
                os.killpg(self.diagnostic.pid, 15)
            except ProcessLookupError:
                pass
            self.diagnostic.communicate(timeout=10)
        if self.diagnostic:
            wait_until(lambda: not M._process_group_exists(self.diagnostic.pid))
            self.log(
                "diagnostic-group-joined", pgid=self.diagnostic.pid, groupAlive=False
            )

    def retire(self):
        if self.diagnostic:
            wait_until(lambda: not M._process_group_exists(self.diagnostic.pid))
        self.path(".retire").touch()
        if self.receiver:
            self.receiver.wait(timeout=15)
            self.log(
                "scope-receiver-retired",
                exit=self.receiver.returncode,
                output=Path(self.prefix + ".receiver-output").read_text(),
                directoryExists=Path(self.config["directory"]).exists(),
            )
            if self.receiver.returncode:
                raise RuntimeError("receiver retirement failed")
            self.receiver_output.close()
        if self.receiver_pidfd is not None:
            os.close(self.receiver_pidfd)
            self.receiver_pidfd = None


def run(path):
    config = json.loads(Path(path).read_text())
    record = config["manager_record"]
    append(
        record,
        "manager-started",
        pid=os.getpid(),
        starttime=Path("/proc/self/stat").read_text().rsplit(")", 1)[1].split()[19],
    )
    owner = "host:45aj6sixiaarr5ta455q3trmwi"
    completions = []

    def completed(info, snapshot):
        completions.append(info.process_id)
        append(
            record,
            "original-completion-subscriber",
            processId=info.process_id,
            physicalState=info.physical_state,
            status=info.status,
        )

    manager = M.TerminalManager(Path.cwd(), completion_subscriber=completed)
    manager.activate_owner(owner)
    manager.environment_owner.config = TerminalEnvConfig(
        enable_shell_snapshot=False, passthrough_names=frozenset({"WSL_INTEROP"})
    )
    shared = dict(owner_host_session_id=owner, max_output_chars=20000)
    scopes = [Scope(item, record) for item in config["scopes"]]
    by_command = {shlex.join(scope.config["task_args"]): scope for scope in scopes}
    by_state = {}
    by_decision = {}
    states = []
    result = {}
    original_spawn = manager.process_registry._spawn
    original_abort = manager.process_registry._abort_foreground_decision
    original_terminate = M._terminate_process_group
    original_join = M._join_physical
    original_is_live = M._process_is_live
    original_thread_start = threading.Thread.start
    fault_fired = threading.Event()

    def spawn(**arguments):
        scope = by_command.get(arguments["command"])
        if scope is None:
            return original_spawn(**arguments)
        decision = arguments["decision_attempt_id"]
        by_decision[decision] = scope
        append(
            record,
            "preparing-under-real-reservation",
            decision=decision,
            launching=dict(manager.process_registry._launching_by_owner),
            decisionState=manager.process_registry.foreground_decision_state(decision),
        )
        try:
            scope.prepare()
            if (
                scope.cancelled.is_set()
                or manager.process_registry._foreground_decision_abort_requested(
                    decision
                )
            ):
                raise ScopeCancelled("decision cancelled before task spawn")
            arguments["env"] = dict(
                arguments["env"], WSL_INTEROP=scope.config["directory"] + "/interop"
            )
            state = original_spawn(**arguments)
            scope.state = state
            states.append(state)
            by_state[id(state)] = scope
            reader_join = state.reader.join

            def joined_reader(timeout=None):
                before = time.monotonic()
                reader_join(timeout=timeout)
                if state.reader.is_alive():
                    return
                deadline = None if timeout is None else before + timeout
                while M._process_group_exists(state.process.pid):
                    if deadline is not None and time.monotonic() >= deadline:
                        return
                    time.sleep(0.005)
                scope.normal_join(
                    None if deadline is None else max(0, deadline - time.monotonic())
                )

            state.reader.join = joined_reader
            append(
                record,
                "scope-installed-before-thread-start",
                processId=state.process_id,
                readerStarted=state.reader_started,
                watcherStarted=state.watcher_started,
            )
            return state
        except BaseException:
            scope.cancel_and_join()
            append(
                record,
                "spawn-error-scope-joined-before-reservation-release",
                decision=decision,
                launching=dict(manager.process_registry._launching_by_owner),
                windows=json.loads(scope.path(".job-joined.json").read_text()),
            )
            raise

    def abort(decision):
        original_abort(decision)
        scope = by_decision.get(decision)
        if scope is not None and scope.state is None:
            scope.cancel_and_join()

    def terminate(state):
        scope = by_state.get(id(state))
        if scope is not None and not scope.path(".job-joined.json").exists():
            scope.request_cancel()
        original_terminate(state)

    def join(state, *, timeout):
        scope = by_state.get(id(state))
        before = time.monotonic()
        if scope is not None and scope.cancelled.is_set():
            if not scope.join_windows(timeout):
                return False
            append(
                record,
                "rollback-or-cancel-extra-scope-joined",
                processId=state.process_id,
                readerStarted=state.reader_started,
                watcherStarted=state.watcher_started,
                completionBeforeOriginalJoin=state.physical_completion.is_set(),
            )
        return original_join(
            state, timeout=max(0, timeout - (time.monotonic() - before))
        )

    def is_live(state):
        scope = by_state.get(id(state))
        return bool(
            scope is not None and not scope.path(".job-joined.json").exists()
        ) or original_is_live(state)

    def thread_start(thread):
        fault = config.get("fault")
        matches = (
            fault == "reader-start"
            and thread.name.startswith("terminal-reader:")
            or fault == "watcher-start"
            and thread.name.startswith("terminal-watcher:")
        )
        if matches and not fault_fired.is_set():
            fault_fired.set()
            append(
                record, "actual-thread-start-injected-failure", threadName=thread.name
            )
            raise RuntimeError("round4 injected " + fault)
        return original_thread_start(thread)

    manager.process_registry._spawn = spawn
    manager.process_registry._abort_foreground_decision = abort
    M._terminate_process_group = terminate
    M._join_physical = join
    M._process_is_live = is_live
    threading.Thread.start = thread_start

    def execute(scope, yield_ms=100):
        decision = scope.config["decision"]
        return manager.execute(
            TerminalRequest(
                shlex.join(scope.config["task_args"]),
                tty=scope.config["tty"],
                yield_time_ms=yield_ms,
            ),
            owner_host_session_id=owner,
            decision_attempt_id=decision,
            origin=TerminalProcessOrigin("round4-task", "ROOT"),
            decision_deadline_monotonic=time.monotonic() + 60,
        )

    def snapshot(state):
        return dict(
            processId=state.process_id,
            rootPid=state.process.pid,
            rootReturncode=state.process.poll(),
            readerAlive=state.reader.is_alive(),
            watcherAlive=bool(state.watcher and state.watcher.is_alive()),
            readerStarted=state.reader_started,
            watcherStarted=state.watcher_started,
            completion=state.physical_completion.is_set(),
            physicalState=state.physical_state,
            liveCount=manager.live_process_count(owner_host_session_id=owner),
            groupAlive=M._process_group_exists(state.process.pid),
        )

    stop_driver = threading.Event()

    def driver():
        try:
            request = Path(config["driver_prefix"] + ".cancel")
            close = Path(config["driver_prefix"] + ".close")
            while not stop_driver.is_set():
                if request.exists() or close.exists():
                    target = scopes[-1]
                    if close.exists():
                        manager.release_owner_and_join(owner)
                        append(record, "real-owner-close-returned")
                    elif target.state is not None:
                        killed = manager.kill_process(target.state.process_id, **shared)
                        append(
                            record,
                            "real-kill-returned",
                            result=asdict(killed),
                            **snapshot(target.state),
                        )
                    else:
                        changed = manager.process_registry.abort_foreground_decision(
                            target.config["decision"]
                        )
                        append(
                            record,
                            "real-abort-returned",
                            changed=changed,
                            launching=dict(
                                manager.process_registry._launching_by_owner
                            ),
                        )
                    return
                time.sleep(0.005)
        except BaseException:
            append(record, "driver-failure", traceback=traceback.format_exc())

    driver_thread = threading.Thread(target=driver, name="round4-driver")
    driver_thread.start()
    try:
        first = scopes[0]
        if config["scenario"] == "ab":
            peer = execute(first)
            append(
                record,
                "shared-B-started",
                managerPid=os.getpid(),
                registryOwner=manager.process_registry._owner_identity,
                result=asdict(peer),
            )
            target = scopes[1]
        else:
            target = first
        started = execute(target)
        append(
            record,
            "execute-returned",
            managerPid=os.getpid(),
            registryOwner=manager.process_registry._owner_identity,
            result=asdict(started),
        )
        if config["scenario"] in {"normal", "ab"}:
            if config["scenario"] == "normal":
                wait_until(
                    lambda: any(
                        row["event"] == "parent-early-exit"
                        for row in rows(target.config["pe_record"])
                    )
                )
                time.sleep(0.15)
                append(record, "during-windows-tail", **snapshot(target.state))
                polled = manager.poll_process(started.process_id, **shared)
                append(
                    record,
                    "fresh-poll-during-windows-tail",
                    result=asdict(polled),
                    **snapshot(target.state),
                )
                assert (
                    polled.status.value == "running"
                    and not target.state.physical_completion.is_set()
                )
                admitted = [
                    manager.execute(
                        TerminalRequest("sleep 30", yield_time_ms=0),
                        owner_host_session_id=owner,
                        origin=TerminalProcessOrigin("round4-slots", "ROOT"),
                        decision_deadline_monotonic=time.monotonic() + 15,
                    )
                    for _ in range(8)
                ]
                append(
                    record,
                    "eight-slot-results",
                    statuses=[item.status for item in admitted],
                    liveCount=manager.live_process_count(owner_host_session_id=owner),
                )
                for item in admitted:
                    if item.process_id:
                        manager.kill_process(item.process_id, **shared)
            else:
                wait_until(
                    lambda: any(
                        row["event"] == "real-kill-returned" for row in rows(record)
                    )
                )
                append(record, "shared-B-after-A-cancel", **snapshot(first.state))
                first.path(".observe").touch()
                wait_until(lambda: first.path(".observed.json").exists())
                append(
                    record,
                    "shared-B-exact-windows-after-A-cancel",
                    facts=json.loads(first.path(".observed.json").read_text()),
                )
                target = first
                started = peer
            final = manager.wait_process(
                started.process_id, timeout_seconds=15, **shared
            )
            append(
                record, "normal-final", result=asdict(final), **snapshot(target.state)
            )
        elif config["scenario"] == "input":
            manager.write_process(
                started.process_id,
                "héllo",
                append_newline=True,
                yield_time_ms=100,
                **shared,
            )
            manager.close_process_stdin(started.process_id, **shared)
            final = manager.wait_process(
                started.process_id, timeout_seconds=15, **shared
            )
            append(
                record, "input-final", result=asdict(final), **snapshot(target.state)
            )
        else:
            if config["scenario"] == "tail-cancel":
                wait_until(
                    lambda: (
                        target.state.process.poll() is not None
                        and not M._process_group_exists(target.state.process.pid)
                    )
                )
                polled = manager.poll_process(started.process_id, **shared)
                append(
                    record,
                    "fresh-poll-before-tail-cancel",
                    result=asdict(polled),
                    **snapshot(target.state),
                )
                assert (
                    polled.status.value == "running"
                    and not target.state.physical_completion.is_set()
                )
                Path(config["driver_prefix"] + ".tail-ready").touch()
            wait_until(
                lambda: any(
                    row["event"]
                    in {
                        "real-kill-returned",
                        "real-owner-close-returned",
                        "real-abort-returned",
                        "driver-failure",
                    }
                    for row in rows(record)
                )
            )
    except BaseException:
        result["executionException"] = traceback.format_exc()
        append(record, "execute-exception", traceback=result["executionException"])
    finally:
        stop_driver.set()
        driver_thread.join(timeout=30)
        try:
            manager.release_owner_and_join(owner)
            append(
                record,
                "original-owner-release-and-join-returned",
                states=[snapshot(state) for state in states],
                launching=dict(manager.process_registry._launching_by_owner),
                decisionCount=len(manager.process_registry._decision_attempts),
                completionPublications=completions,
            )
        finally:
            for scope in scopes:
                if scope.receiver is None:
                    continue
                if not scope.path(".job-joined.json").exists():
                    scope.cancel_and_join()
                assert not manager.process_registry._launching_by_owner
                if scope.state is not None:
                    assert not M._process_group_exists(scope.state.process.pid)
                    assert not scope.state.reader.is_alive()
                    assert not scope.state.watcher or not scope.state.watcher.is_alive()
                append(
                    record,
                    "retirement-authorized-after-original-owner-join",
                    scope=scope.config["name"],
                    launching=dict(manager.process_registry._launching_by_owner),
                    state=None if scope.state is None else snapshot(scope.state),
                )
                scope.retire()
            append(
                record,
                "manager-finished",
                result=result,
                faultFired=fault_fired.is_set(),
            )


if __name__ == "__main__":
    run(sys.argv[1])
