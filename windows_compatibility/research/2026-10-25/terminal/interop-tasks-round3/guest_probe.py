"""Output-only real terminal owner probe and opaque Unix byte relay."""

import asyncio
from dataclasses import asdict
import json
import os
from pathlib import Path
import shlex
import sys
import time


def emit(path, event, **values):
    with open(path, "a", encoding="utf-8") as stream:
        stream.write(json.dumps(dict(event=event, time=time.time(), **values)) + "\n")


def rows(path):
    path = Path(path)
    return (
        [json.loads(line) for line in path.read_text().splitlines()]
        if path.exists()
        else []
    )


def wait_for(path, predicate, seconds=15):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if predicate(rows(path)):
            return
        time.sleep(0.02)
    raise TimeoutError(path)


async def relay(upstream, directory, record):
    os.mkdir(directory, 0o700)
    path = directory + "/interop"
    fd = os.open(upstream, os.O_PATH | os.O_CLOEXEC)
    pinned = f"/proc/{os.getpid()}/fd/{fd}"

    async def connected(reader, writer):
        remote = None
        try:
            if Path(directory, "sealed").exists():
                emit(record, "relay-refused-after-seal")
                return
            other, remote = await asyncio.open_unix_connection(pinned)

            async def pump(source, target):
                while chunk := await source.read(65536):
                    target.write(chunk)
                    await target.drain()
                if target.can_write_eof():
                    target.write_eof()

            await asyncio.gather(pump(reader, remote), pump(other, writer))
            emit(record, "relay-request-done")
        except Exception as exc:
            emit(record, "relay-error", error=repr(exc))
        finally:
            for item in (writer, remote):
                if item:
                    item.close()

    server = await asyncio.start_unix_server(connected, path=path)
    emit(
        record,
        "relay-ready",
        pid=os.getpid(),
        starttime=Path("/proc/self/stat").read_text().rsplit(")", 1)[1].split()[19],
        socket=path,
        directory=directory,
        mode=oct(os.stat(directory).st_mode & 0o777),
        upstream=upstream,
    )
    async with server:
        await server.serve_forever()


def manager(config_path):
    from pulsara_agent.terminal_process.environment import TerminalEnvConfig
    from pulsara_agent.terminal_process.manager import TerminalManager
    from pulsara_agent.terminal_process.models import (
        TerminalRequest,
        TerminalProcessOrigin,
    )

    config = json.loads(Path(config_path).read_text())
    configurations = [config] + ([config["peer"]] if "peer" in config else [])
    record = config["record"]
    owner = "host:45aj6sixiaarr5ta455q3trmwi"
    instance = TerminalManager(Path.cwd())
    instance.activate_owner(owner)
    instance.environment_owner.config = TerminalEnvConfig(
        enable_shell_snapshot=False, passthrough_names=frozenset({"WSL_INTEROP"})
    )
    parent = dict(os.environ)
    parent["WSL_INTEROP"] = config["socket"]
    instance.environment_owner.parent_env = parent
    shared = dict(owner_host_session_id=owner, max_output_chars=20000)

    if config.get("patched"):
        import pulsara_agent.terminal_process.manager as module

        class Scope:
            def __init__(self, settings):
                self.settings = settings

            def request(self, action):
                Path(self.settings["scope_prefix"] + "." + action).touch()

            def join(self, timeout=None):
                self.request("normal")
                deadline = None if timeout is None else time.monotonic() + timeout
                while not Path(self.settings["scope_prefix"] + ".joined").exists():
                    if deadline is not None and time.monotonic() >= deadline:
                        return
                    time.sleep(0.005)

        scopes = {shlex.join(item["pe_args"]): Scope(item) for item in configurations}
        original_spawn = instance.process_registry._spawn
        original_terminate = module._terminate_process_group
        scoped_states = {}

        def spawn(**kwargs):
            state = original_spawn(**kwargs)
            scope = scopes.get(kwargs["command"])
            if scope is None:
                return state
            scoped_states[id(state)] = scope
            original_join = state.reader.join

            def reader_and_scope_join(timeout=None):
                before = time.monotonic()
                original_join(timeout=timeout)
                if not state.reader.is_alive():
                    deadline = None if timeout is None else before + timeout
                    while module._process_group_exists(state.process.pid):
                        if deadline is not None and time.monotonic() >= deadline:
                            return
                        time.sleep(0.005)
                    scope.join(
                        None
                        if deadline is None
                        else max(0, deadline - time.monotonic())
                    )

            state.reader.join = reader_and_scope_join
            return state

        def terminate(state):
            if id(state) in scoped_states:
                scope = scoped_states[id(state)]
                scope.request("cancel")
                while not Path(scope.settings["scope_prefix"] + ".sealed").exists():
                    time.sleep(0.005)
            return original_terminate(state)

        # Probe-only composition at existing physical-owner calls, not an API.
        instance.process_registry._spawn = spawn
        module._terminate_process_group = terminate

    def execute(command, tty=False, yield_ms=100):
        return instance.execute(
            TerminalRequest(command, tty=tty, yield_time_ms=yield_ms),
            owner_host_session_id=owner,
            origin=TerminalProcessOrigin("round3-task", "ROOT"),
            decision_deadline_monotonic=time.monotonic() + 15,
        )

    def snapshot(result):
        state = instance.process_registry._states[result.process_id]
        return dict(
            result=asdict(result),
            rootPid=state.process.pid,
            rootReturncode=state.process.poll(),
            readerAlive=state.reader.is_alive(),
            watcherAlive=state.watcher.is_alive(),
            physicalState=state.physical_state,
            completion=state.physical_completion.is_set(),
            liveCount=instance.live_process_count(owner_host_session_id=owner),
        )

    try:
        peer_result = None
        if "peer" in config:
            peer = config["peer"]
            parent["WSL_INTEROP"] = peer["socket"]
            peer_result = execute(shlex.join(peer["pe_args"]), tty=peer["tty"])
            emit(
                peer["record"],
                "started-in-shared-owner",
                managerPid=os.getpid(),
                registryOwner=instance.process_registry._owner_identity,
                result=asdict(peer_result),
            )
            wait_for(
                peer["pe_record"],
                lambda values: any(row["event"] == "child-ready" for row in values),
            )
            parent["WSL_INTEROP"] = config["socket"]
        command = shlex.join(config["pe_args"])
        result = execute(command, tty=config["tty"])
        emit(
            record,
            "started",
            managerPid=os.getpid(),
            registryOwner=instance.process_registry._owner_identity,
            result=asdict(result),
        )
        wait_for(
            config["pe_record"],
            lambda values: any(row["event"] == "child-ready" for row in values),
        )
        if config["scenario"] == "normal":
            if config.get("patched"):
                time.sleep(0.4)
                emit(record, "during-windows-tail", **snapshot(result))
                slots = [execute("sleep 30", yield_ms=0) for _ in range(8)]
                emit(
                    record,
                    "eight-slot-admission",
                    results=[asdict(item) for item in slots],
                    liveCount=instance.live_process_count(owner_host_session_id=owner),
                )
                for item in slots:
                    if item.process_id:
                        instance.kill_process(item.process_id, **shared)
                result = instance.wait_process(
                    result.process_id, timeout_seconds=12, **shared
                )
                emit(record, "normal-owner-result", **snapshot(result))
                emit(record, "observation-ready")
                while not Path(config["release"]).exists():
                    time.sleep(0.02)
                return
            result = instance.wait_process(
                result.process_id, timeout_seconds=5, **shared
            )
            emit(record, "normal-owner-result", **snapshot(result))
            slots = [execute("sleep 30", yield_ms=0) for _ in range(8)]
            emit(
                record,
                "eight-slot-admission",
                results=[asdict(item) for item in slots],
                liveCount=instance.live_process_count(owner_host_session_id=owner),
            )
            for item in slots:
                if item.process_id:
                    instance.kill_process(item.process_id, **shared)
        elif config["scenario"] == "cancel":
            try:
                result = instance.kill_process(result.process_id, **shared)
                emit(record, "cancel-owner-result", **snapshot(result))
            except Exception as exc:
                emit(record, "cancel-owner-error", error=repr(exc), **snapshot(result))
            if peer_result:
                Path(peer["scope_prefix"] + ".observe").touch()
                while not Path(peer["scope_prefix"] + ".observed.json").exists():
                    time.sleep(0.005)
                windows = json.loads(
                    Path(peer["scope_prefix"] + ".observed.json").read_text()
                )
                emit(
                    record,
                    "peer-after-A-cancel",
                    windows=windows,
                    **snapshot(peer_result),
                )
                emit(record, "observation-ready")
                peer_final = instance.wait_process(
                    peer_result.process_id, timeout_seconds=12, **shared
                )
                emit(peer["record"], "normal-owner-result", **snapshot(peer_final))
                emit(peer["record"], "observation-ready")
        else:
            written = instance.write_process(
                result.process_id,
                "héllo",
                append_newline=True,
                yield_time_ms=100,
                **shared,
            )
            emit(record, "input-write", result=asdict(written))
            instance.close_process_stdin(result.process_id, **shared)
            final = instance.wait_process(
                result.process_id, timeout_seconds=8, **shared
            )
            emit(record, "input-eof-result", **snapshot(final))
        emit(record, "observation-ready")
        while not all(Path(item["release"]).exists() for item in configurations):
            time.sleep(0.02)
    finally:
        instance.release_owner_and_join(owner)
        emit(record, "manager-cleanup-done")
        if "peer" in config:
            emit(config["peer"]["record"], "manager-cleanup-done")


if __name__ == "__main__":
    if sys.argv[1] == "relay":
        asyncio.run(relay(*sys.argv[2:]))
    else:
        manager(sys.argv[2])
