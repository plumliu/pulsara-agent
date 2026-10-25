"""Explicit Windows/WSL integration matrix; run with the built helper/fixture."""

import asyncio
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
import json
import os
from pathlib import Path
import subprocess
import signal
import sys
from threading import Event
from time import monotonic
import time

import pytest

from pulsara_agent.identity import derive_id
from pulsara_agent.terminal_process import manager as M
from pulsara_agent.windows_interop import configure_interop


@pytest.fixture(scope="module", autouse=True)
def configured():
    configure_interop(
        helper_path=os.environ["PULSARA_TEST_INTEROP_HELPER"],
        distribution="Ubuntu",
        user="plumwsl",
    )


@pytest.fixture(autouse=True)
def exact_scope_trace(monkeypatch, request):
    from pulsara_agent.windows_interop.scope import WindowsInteropScope

    scopes = []
    original = WindowsInteropScope.__init__

    def construct(scope, configuration):
        original(scope, configuration)
        scopes.append(scope)

    monkeypatch.setattr(WindowsInteropScope, "__init__", construct)
    yield
    for scope in scopes:
        joined = None
        if (
            scope._joined is not None
            and scope._joined.done()
            and not scope._joined.cancelled()
        ):
            if scope._joined.exception() is None:
                joined = scope._joined.result()
        print(
            json.dumps(
                dict(
                    case=request.node.name,
                    helper=scope.helper_ready,
                    joined=joined,
                    failure=scope.failure,
                    physicalJoin=scope.physically_joined.is_set(),
                    cancelled=scope.cancelled.is_set(),
                    retired=scope.directory is None,
                    helperExit=None
                    if scope.helper is None
                    else scope.helper.returncode,
                    diagnosticExit=None
                    if scope.diagnostic is None
                    else scope.diagnostic.returncode,
                    workerAlive=scope._thread is not None and scope._thread.is_alive(),
                    relay=None
                    if scope.relay is None
                    else dict(
                        tasks=len(scope.relay.tasks),
                        writers=len(scope.relay.writers),
                        peer=scope.relay.peer_identity,
                        path=str(scope.relay.path),
                    ),
                ),
                ensure_ascii=False,
            ),
            flush=True,
        )
        assert scope.physically_joined.is_set() and scope.directory is None
        assert scope._thread is None or not scope._thread.is_alive()


@pytest.fixture
def child():
    return os.environ["PULSARA_TEST_INTEROP_CHILD"]


def launch(registry, tmp_path, argv, *, tty=False, owner=None):
    owner = owner or derive_id("host", "interop-integration", "owner")
    registry.activate_owner(owner)
    state, yielded = registry.exec_with_yield(
        command="interop integration fixture",
        cwd=tmp_path,
        yield_time_ms=0,
        tty=tty,
        max_lifetime_seconds=None,
        owner_host_session_id=owner,
        shell_argv=tuple(argv),
        env=dict(os.environ),
        decision_deadline_monotonic=monotonic() + 15,
    )
    return state, owner


@pytest.mark.parametrize("tty", [False, True])
def test_normal_no_pipe_descendant_retains_live_slot(tmp_path, child, tty):
    registry = M.ProcessRegistry(max_live_processes=1)
    started = monotonic()
    state, owner = launch(registry, tmp_path, [child, "tail", "1600"], tty=tty)
    try:
        state.process.wait(timeout=1)
        assert not state.physical_completion.is_set()
        assert M._status(state) is M.TerminalStatus.RUNNING
        assert registry.live_count(owner_host_session_id=owner) == 1
        assert state.physical_completion.wait(5)
        assert monotonic() - started >= 1.5
        assert M._snapshot(state, 1000).status is M.TerminalStatus.SUCCESS
        assert not state.interop_scope.cancelled.is_set()
        assert state.interop_scope.failure is None
    finally:
        registry.release_owner(owner, timeout_seconds=10)
    assert state.interop_scope.directory is None


@pytest.mark.parametrize("tty", [False, True])
def test_cancel_after_linux_root_exits(tmp_path, child, tty):
    registry = M.ProcessRegistry()
    state, owner = launch(registry, tmp_path, [child, "tail", "30000"], tty=tty)
    try:
        state.process.wait(timeout=5)
        assert M._status(state) is M.TerminalStatus.RUNNING
        result = registry.terminate_if_running(
            state.process_id,
            max_output_chars=1000,
            owner_host_session_id=owner,
            join_timeout_seconds=10,
        )
        assert result.result.status is M.TerminalStatus.KILLED
        assert state.physical_completion.is_set()
        assert state.interop_scope.physically_joined.is_set()
    finally:
        registry.release_owner(owner, timeout_seconds=10)


@pytest.mark.parametrize("tty", [False, True])
def test_input_and_eof(tmp_path, child, tty):
    registry = M.ProcessRegistry()
    state, owner = launch(registry, tmp_path, [child, "echo"], tty=tty)
    try:
        registry.write(
            state.process_id,
            "hello\n",
            append_newline=False,
            yield_time_ms=0,
            max_output_chars=1000,
            owner_host_session_id=owner,
        )
        registry.close_stdin(
            state.process_id, max_output_chars=1000, owner_host_session_id=owner
        )
        assert state.physical_completion.wait(5)
        assert M._snapshot(state, 1000).status is M.TerminalStatus.SUCCESS
        assert "hello" in M._snapshot(state, 1000).output
    finally:
        registry.release_owner(owner, timeout_seconds=10)


def test_same_registry_ab_isolation(tmp_path, child):
    registry = M.ProcessRegistry()
    a, owner = launch(registry, tmp_path, [child, "tail", "30000"])
    b, _ = launch(registry, tmp_path, [child, "tail", "2000"], owner=owner)
    try:
        a.process.wait(timeout=5)
        registry.terminate_if_running(
            a.process_id,
            max_output_chars=1000,
            owner_host_session_id=owner,
            join_timeout_seconds=10,
        )
        assert registry.live_count(owner_host_session_id=owner) == 1
        assert not b.interop_scope.cancelled.is_set()
        assert b.physical_completion.wait(5)
        assert M._snapshot(b, 1000).status is M.TerminalStatus.SUCCESS
    finally:
        registry.release_owner(owner, timeout_seconds=10)


def test_hook_normal_and_timeout(tmp_path, child):
    from tests.test_round9_2_hook_subsystem import _definition, _scope
    from pulsara_agent.hooks.executor import HookCommandExecutor, HookExecutionRequest
    from pulsara_agent.hooks.contracts import HookEventType
    from pulsara_agent.process_credential_boundary import ProcessCredentialBoundary

    async def scenario():
        executor = HookCommandExecutor(credential_boundary=ProcessCredentialBoundary())
        for timeout, duration, expected in [
            (10, 800, None),
            (2, 30000, "HOOK_TIMEOUT"),
        ]:
            definition = replace(
                _definition(
                    tmp_path,
                    HookEventType.USER_PROMPT_SUBMIT_EVENT,
                    f"{child} tail {duration}",
                ),
                timeout_seconds=timeout,
            )
            request = HookExecutionRequest(
                definition, {}, _scope(), 0, 0, tmp_path, None, None
            )
            started = monotonic()
            result = await executor.execute(request)
            assert result.failure_code == expected
            assert (
                monotonic() - started >= duration / 1000 if expected is None else True
            )
            assert not executor._attempts
        await executor.aclose()

    asyncio.run(scenario())


def test_mcp_sdk_call_cancel_preserves_session_then_close(tmp_path, child):
    from tests.test_mcp_transport_lifecycle import _client

    async def scenario():
        server = Path(__file__).parent / "fixtures" / "round6_mcp_server.py"
        # The original SDK fixture remains the server. This Linux launcher only
        # creates a Windows no-pipe tail in the same connection scope first.
        script = "import subprocess,os,sys;subprocess.run([sys.argv[1],'tail','30000'],stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,check=True);os.execv(sys.executable,[sys.executable,sys.argv[2]])"
        client = _client(
            tmp_path,
            dict(
                type="stdio",
                command=sys.executable,
                args=["-c", script, child, str(server)],
            ),
        )
        try:
            await client.open()
            scope = client._transport._interop_scope
            call = asyncio.create_task(
                client.session.call_tool("fixture_delay", {"delay_ms": 1000})
            )
            await asyncio.sleep(0.05)
            call.cancel()
            await asyncio.gather(call, return_exceptions=True)
            assert not scope.cancelled.is_set()
            result = await client.session.call_tool(
                "fixture_echo", {"text": "still-ready"}
            )
            assert not result.is_error
        finally:
            await client.aclose()
        assert scope.physically_joined.is_set() and scope.directory is None

    asyncio.run(scenario())


@pytest.mark.parametrize("worker", ["reader", "watcher"])
@pytest.mark.parametrize("tty", [False, True])
def test_physical_thread_start_failure_joins_both_domains(
    tmp_path, child, monkeypatch, worker, tty
):
    registry = M.ProcessRegistry()
    scopes = []
    original_scope = M.create_scope
    original_start = M.Thread.start

    def capture():
        scope = original_scope()
        scopes.append(scope)
        return scope

    def fail(thread):
        if thread.name.startswith("terminal-" + worker + ":"):
            raise RuntimeError("actual " + worker + " start failure")
        return original_start(thread)

    monkeypatch.setattr(M, "create_scope", capture)
    monkeypatch.setattr(M.Thread, "start", fail)
    with pytest.raises(RuntimeError, match="start failure"):
        launch(registry, tmp_path, [child, "sleep", "30000"], tty=tty)
    assert scopes[0].physically_joined.is_set()
    assert scopes[0].directory is None
    assert registry._states == {} and registry._launching_by_owner == {}


@pytest.mark.parametrize("role", ["owner", "monitor", "control-eof", "anchor"])
def test_helper_critical_loss_has_exact_join_and_unknown_result(tmp_path, child, role):
    registry = M.ProcessRegistry()
    state, owner = launch(registry, tmp_path, [child, "sleep", "30000"])
    scope = state.interop_scope
    try:
        ready = scope.helper_ready
        if role == "control-eof":
            scope._loop.call_soon_threadsafe(scope.helper.stdin.close)
        elif role == "anchor":
            signal.pidfd_send_signal(scope.relay.peer_pidfd, signal.SIGKILL)
        else:
            pid, creation = (
                (ready["pid"], ready["creation"])
                if role == "owner"
                else (ready["monitorPid"], ready["monitorCreation"])
            )
            subprocess.run(
                [child, "terminate", str(pid), creation], check=True, timeout=10
            )
        assert state.physical_completion.wait(10)
        assert scope.physically_joined.is_set()
        assert scope.failure and "unknown" in scope.failure
        assert M._snapshot(state, 1000).status is M.TerminalStatus.ERROR
    finally:
        registry.release_owner(owner, timeout_seconds=10)


@pytest.mark.parametrize(
    "timeout,duration,expected", [(5, 800, True), (1, 30000, False)]
)
def test_environment_cache_waits_both_domains(
    tmp_path, child, monkeypatch, timeout, duration, expected
):
    from pulsara_agent.terminal_process import environment as E

    script = tmp_path / "login-shell"
    script.write_text(
        f"#!/bin/sh\n{child} tail {duration} >/dev/null 2>&1\nprintf 'PULSARA_TERMINAL_ENV_V1\\0'\nenv -0\n"
    )
    script.chmod(0o700)
    monkeypatch.setattr(
        E, "detect_terminal_shell", lambda env: E.TerminalShellConfig(script, "sh")
    )
    owner = E.TerminalEnvironmentOwner(
        tmp_path, E.TerminalEnvConfig(shell_snapshot_timeout_seconds=timeout)
    )
    started = monotonic()
    result = owner.build(cwd=tmp_path)
    assert result.diagnostic["shell_snapshot_used"] is expected
    if expected:
        assert monotonic() - started >= 0.8
        assert len(owner._cache) == 1
    else:
        assert result.diagnostic["shell_snapshot_error_code"] == "PROBE_TIMEOUT"
        assert not owner._cache
    assert not owner._attempts
    owner.close_and_join()


def test_all_eight_slots_are_physical_and_reusable_after_join(tmp_path, child):
    registry = M.ProcessRegistry(max_live_processes=8)
    states = []
    owner = derive_id("host", "interop-integration", "eight-slots")
    try:
        for _ in range(8):
            state, _ = launch(registry, tmp_path, [child, "tail", "30000"], owner=owner)
            states.append(state)
        assert registry.live_count(owner_host_session_id=owner) == 8
        with pytest.raises(M.ProcessLimitError):
            launch(registry, tmp_path, ["/bin/true"], owner=owner)
        registry.terminate_if_running(
            states[0].process_id,
            max_output_chars=1000,
            owner_host_session_id=owner,
            join_timeout_seconds=10,
        )
        assert registry.live_count(owner_host_session_id=owner) == 7
        replacement, _ = launch(registry, tmp_path, ["/bin/true"], owner=owner)
        states.append(replacement)
        assert replacement.physical_completion.wait(5)
    finally:
        registry.release_owner(owner, timeout_seconds=30)
    assert all(state.interop_scope.directory is None for state in states)


@pytest.mark.parametrize(
    "stage", ["before-bootstrap", "descriptor-receive", "before-command"]
)
def test_cancel_inside_original_launch_reservation(tmp_path, child, monkeypatch, stage):
    from pulsara_agent.windows_interop.scope import WindowsInteropScope
    from pulsara_agent.windows_interop.relay import InteropRelay

    entered, release = Event(), Event()
    scopes = []
    original_create = M.create_scope
    original_prepare = WindowsInteropScope._prepare
    original_receive = InteropRelay._receive_descriptor
    original_environment = WindowsInteropScope.environment

    def capture():
        scope = original_create()
        scopes.append(scope)
        return scope

    async def before_bootstrap(scope):
        entered.set()
        while not release.is_set():
            await asyncio.sleep(0.005)
        return await original_prepare(scope)

    async def descriptor_receive(relay, connection):
        entered.set()
        await asyncio.Event().wait()
        return await original_receive(relay, connection)

    def before_command(scope, environment):
        entered.set()
        assert release.wait(10)
        return original_environment(scope, environment)

    monkeypatch.setattr(M, "create_scope", capture)
    if stage == "before-bootstrap":
        monkeypatch.setattr(WindowsInteropScope, "_prepare", before_bootstrap)
    elif stage == "descriptor-receive":
        monkeypatch.setattr(InteropRelay, "_receive_descriptor", descriptor_receive)
    else:
        monkeypatch.setattr(WindowsInteropScope, "environment", before_command)
    registry = M.ProcessRegistry(max_live_processes=1)
    owner = derive_id("host", "interop-integration", stage)
    with ThreadPoolExecutor(max_workers=1) as pool:
        pending = pool.submit(
            launch, registry, tmp_path, [child, "sleep", "30000"], owner=owner
        )
        try:
            assert entered.wait(10)
            assert registry._launching_by_owner[owner] == 1
            attempt_id = next(iter(registry._decision_attempts))
            assert registry.abort_foreground_decision(attempt_id)
            release.set()
            with pytest.raises((RuntimeError, asyncio.CancelledError)):
                pending.result(timeout=15)
            assert scopes[0].physically_joined.is_set()
            assert scopes[0].directory is None
            assert not registry._states and not registry._launching_by_owner
        finally:
            release.set()
            registry.release_owner(owner, timeout_seconds=15)


@pytest.mark.parametrize(
    "stage", ["before-upstream", "partial-forward", "after-forward"]
)
def test_real_owner_cancel_settles_inflight_relay(tmp_path, child, monkeypatch, stage):
    from pulsara_agent.windows_interop.scope import WindowsInteropScope

    entered = Event()
    active_paths, operation_writers = set(), set()
    forwarded = []
    original_environment = WindowsInteropScope.environment
    original_open = asyncio.open_unix_connection
    original_write, original_drain = (
        asyncio.StreamWriter.write,
        asyncio.StreamWriter.drain,
    )

    def environment(scope, values):
        values = original_environment(scope, values)
        active_paths.add(f"/proc/self/fd/{scope.relay.descriptor}")
        return values

    async def opening(path, *args, **kwargs):
        operation = str(path) in active_paths
        if operation and stage == "before-upstream":
            entered.set()
            await asyncio.Future()
        pair = await original_open(path, *args, **kwargs)
        if operation:
            operation_writers.add(pair[1])
        return pair

    def write(writer, data):
        if writer in operation_writers:
            selected = data[:1] if stage == "partial-forward" else data
            forwarded.append(len(selected))
            return original_write(writer, selected)
        return original_write(writer, data)

    async def drain(writer):
        await original_drain(writer)
        if writer in operation_writers:
            entered.set()
            await asyncio.Future()

    monkeypatch.setattr(WindowsInteropScope, "environment", environment)
    monkeypatch.setattr(asyncio, "open_unix_connection", opening)
    monkeypatch.setattr(asyncio.StreamWriter, "write", write)
    monkeypatch.setattr(asyncio.StreamWriter, "drain", drain)
    registry = M.ProcessRegistry()
    state, owner = launch(registry, tmp_path, [child, "sleep", "30000"])
    try:
        assert entered.wait(10)
        registry.terminate_if_running(
            state.process_id,
            max_output_chars=1000,
            owner_host_session_id=owner,
            join_timeout_seconds=15,
        )
        scope = state.interop_scope
        assert state.physical_completion.is_set() and scope.physically_joined.is_set()
        assert not scope.relay.tasks and not scope.relay.writers
        assert M._snapshot(state, 1000).status is M.TerminalStatus.KILLED
        amount = sum(forwarded)
        print(json.dumps(dict(gate=stage, forwardedBytes=amount)), flush=True)
        assert (
            amount == 0
            if stage == "before-upstream"
            else amount == 1
            if stage == "partial-forward"
            else amount > 1
        )
        assert scope.directory is None
    finally:
        registry.release_owner(owner, timeout_seconds=15)


def test_monitor_releases_exited_member_handles_during_live_scope(child):
    from pulsara_agent.windows_interop import create_scope

    scope = create_scope()
    scope.prepare()
    ready = scope.helper_ready

    def handles():
        done = subprocess.run(
            [child, "handles", str(ready["monitorPid"]), ready["monitorCreation"]],
            capture_output=True,
            text=True,
            check=True,
            timeout=10,
        )
        return int(done.stdout.strip())

    try:
        time.sleep(0.03)
        counts = [handles()]
        for index in range(40):
            subprocess.run(
                [child, "sleep", "30"],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                env=scope.environment(dict(os.environ)),
                check=True,
                timeout=10,
            )
            if index % 8 == 7:
                time.sleep(0.03)
                counts.append(handles())
        assert max(counts[1:]) <= counts[0] + 2, counts
        print(
            json.dumps(dict(shortProcesses=40, monitorHandleCounts=counts)), flush=True
        )
        scope.finish()
        assert scope.failure is None
    finally:
        if not scope.physically_joined.is_set():
            scope.finish(cancel=True)
        scope.retire()


def test_real_mcp_supervisor_closes_its_stdio_scope(tmp_path, child):
    from pulsara_agent.conversation_kernel.mcp.supervisor import (
        McpHostSupervisor,
        McpServerState,
    )
    from pulsara_agent.mcp_config import _parse_server
    from pulsara_agent.process_credential_boundary import ProcessCredentialBoundary

    async def scenario():
        server = Path(__file__).parent / "fixtures" / "round6_mcp_server.py"
        script = "import subprocess,os,sys;subprocess.run([sys.argv[1],'tail','30000'],stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,check=True);os.execv(sys.executable,[sys.executable,sys.argv[2]])"
        config = _parse_server(
            "real",
            {
                "transport": {
                    "type": "stdio",
                    "command": sys.executable,
                    "args": ["-c", script, child, str(server)],
                }
            },
        )
        supervisor = McpHostSupervisor(
            session_id="session:interop",
            workspace_root=tmp_path,
            configs=(config,),
            credential_boundary=ProcessCredentialBoundary(),
        )
        try:
            await supervisor.start()
            assert (
                await supervisor.wait_for_server_settlement("real", timeout_seconds=10)
                is McpServerState.READY
            )
            slot = supervisor._slots["real"]
            scope = slot.client._transport._interop_scope
            result = await slot.client.session.call_tool(
                "fixture_echo", {"text": "supervised"}
            )
            assert not result.is_error
        finally:
            await supervisor.aclose()
        assert scope.physically_joined.is_set() and scope.directory is None
        assert not supervisor._all_slots

    asyncio.run(scenario())
