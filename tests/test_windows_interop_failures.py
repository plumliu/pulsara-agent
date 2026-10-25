"""Unobservable Windows joins must remain owned; these are failure-path units."""

import asyncio
import os
import shlex
import sys
from threading import Event
from time import monotonic

import pytest

from pulsara_agent.windows_interop.scope import InteropScopeError, WindowsInteropScope


class UnjoinedScope:
    def __init__(self):
        self.physically_joined = Event()
        self.cancelled = Event()
        self.failed = Event()
        self.failure = "lost exact Windows settlement"
        self.finish_calls = 0

    def prepare(self):
        raise InteropScopeError(self.failure)

    async def prepare_async(self):
        self.prepare()

    def request_cancel(self):
        pass

    def finish(self, **kwargs):
        self.finish_calls += 1
        raise InteropScopeError(self.failure)

    async def finish_async(self, **kwargs):
        self.finish(**kwargs)

    def retire(self):
        pytest.fail("an unjoined scope was retired")


def test_environment_retains_failed_physical_attempt_and_refuses_clean_close(
    tmp_path, monkeypatch
):
    from pulsara_agent.terminal_process import environment as E

    scope = UnjoinedScope()
    created = []

    def create():
        created.append(scope)
        return scope

    monkeypatch.setattr(E, "create_scope", create)
    owner = E.TerminalEnvironmentOwner(tmp_path)
    first = owner.build(cwd=tmp_path)
    assert first.diagnostic["shell_snapshot_error_code"] == "PROBE_FAILED"
    assert len(owner._attempts) == 1 and not owner._cache
    second = owner.build(cwd=tmp_path)
    assert (
        second.diagnostic["shell_snapshot_error_code"] == "PROBE_PHYSICAL_JOIN_FAILED"
    )
    assert len(created) == 1
    with pytest.raises(RuntimeError, match="physical join"):
        owner.close_and_join()
    with pytest.raises(RuntimeError, match="physical join"):
        owner.close(timeout_seconds=1)
    assert len(owner._attempts) == 1


def test_terminal_caller_cannot_settle_unjoined_bootstrap_out_of_inventory(
    tmp_path, monkeypatch
):
    from pulsara_agent.identity import derive_id
    from pulsara_agent.terminal_process import manager as M

    scope = UnjoinedScope()
    monkeypatch.setattr(M, "create_scope", lambda: scope)
    registry = M.ProcessRegistry(max_live_processes=1)
    owner = derive_id("host", "interop-failure-unit", "owner")
    decision = derive_id("terminal-decision", "interop-failure-unit", "decision")
    registry.activate_owner(owner)
    args = dict(
        command="must not spawn",
        cwd=tmp_path,
        yield_time_ms=0,
        tty=False,
        max_lifetime_seconds=None,
        owner_host_session_id=owner,
        shell_argv=("/bin/false",),
        env={},
        decision_deadline_monotonic=monotonic() + 5,
    )
    with pytest.raises(M.ProcessPhysicalJoinError):
        registry.exec_with_yield(**args, decision_attempt_id=decision)
    registry.settle_foreground_decision(decision)
    assert registry.foreground_decision_state(decision) == "ABORT_REQUESTED"
    assert not registry._launching_by_owner and not registry._states
    with pytest.raises(M.ProcessLimitError):
        registry.exec_with_yield(**args)
    with pytest.raises(InteropScopeError, match="lost exact"):
        registry.release_owner(owner, timeout_seconds=1)
    assert decision in registry._decision_attempts


def test_stdio_repeated_close_preserves_same_physical_failure(tmp_path):
    from pulsara_agent.conversation_kernel.mcp.sdk_facade import _BoundedStdioTransport
    from pulsara_agent.conversation_kernel.mcp.wire import DEFAULT_MCP_WIRE_BOUNDS
    from pulsara_agent.mcp_config import _parse_server
    from pulsara_agent.process_credential_boundary import ProcessCredentialBoundary

    async def scenario():
        config = _parse_server(
            "unit", {"transport": {"type": "stdio", "command": "/bin/false"}}
        )
        transport = _BoundedStdioTransport(
            config.transport,
            workspace_root=tmp_path,
            credential_boundary=ProcessCredentialBoundary(),
            bounds=DEFAULT_MCP_WIRE_BOUNDS,
        )
        scope = UnjoinedScope()
        transport._interop_scope = scope
        for _ in range(2):
            with pytest.raises(InteropScopeError, match="lost exact"):
                await transport.aclose()
        assert scope.finish_calls == 1
        assert transport._close_task.done()
        assert transport._close_task.exception() is not None
        await transport.read_writer.aclose()

    asyncio.run(scenario())


def test_hook_retains_unjoined_attempt_and_its_original_permit(tmp_path, monkeypatch):
    from tests.test_round9_2_hook_subsystem import _definition, _scope
    from pulsara_agent.hooks import executor as H
    from pulsara_agent.hooks.contracts import HookEventType
    from pulsara_agent.process_credential_boundary import ProcessCredentialBoundary

    async def scenario():
        scope = UnjoinedScope()
        monkeypatch.setattr(H, "create_scope", lambda: scope)
        executor = H.HookCommandExecutor(
            credential_boundary=ProcessCredentialBoundary()
        )
        definition = _definition(
            tmp_path, HookEventType.USER_PROMPT_SUBMIT_EVENT, "false"
        )
        request = H.HookExecutionRequest(
            definition, {}, _scope(), 0, 0, tmp_path, None, None
        )
        result = await executor.execute(request)
        assert result.failure_code == "HOOK_EXECUTOR_FAILURE"
        assert len(executor._attempts) == 1
        assert executor._sync_slots._value == H.SYNCHRONOUS_COMMAND_SLOTS - 1
        with pytest.raises(InteropScopeError, match="lost exact"):
            await executor.aclose()

    asyncio.run(scenario())


def test_complete_json_without_lf_is_not_a_join_frame():
    async def scenario():
        scope = WindowsInteropScope(None)
        loop = asyncio.get_running_loop()
        scope._ready, scope._verified, scope._joined = (
            loop.create_future() for _ in range(3)
        )
        stream = asyncio.StreamReader()
        stream.feed_data(b'{"type":"joined","reason":"settled"}')
        stream.feed_eof()

        class Helper:
            stdout = stream

        scope.helper = Helper()
        await scope._read_helper()
        assert scope.failed.is_set() and not scope.physically_joined.is_set()
        for future in (scope._ready, scope._verified, scope._joined):
            with pytest.raises(InteropScopeError, match="partial"):
                future.result()

    asyncio.run(scenario())


@pytest.mark.parametrize("owner_kind", ["hook", "mcp"])
def test_early_drain_task_creation_failure_joins_actual_linux_child(
    tmp_path, monkeypatch, owner_kind
):
    from tests.test_round9_2_hook_subsystem import _definition, _scope
    from pulsara_agent.hooks import executor as H
    from pulsara_agent.hooks.contracts import HookEventType
    from pulsara_agent.conversation_kernel.mcp import sdk_facade as S
    from pulsara_agent.conversation_kernel.mcp.wire import DEFAULT_MCP_WIRE_BOUNDS
    from pulsara_agent.mcp_config import _parse_server
    from pulsara_agent.process_credential_boundary import ProcessCredentialBoundary

    async def scenario():
        processes = []
        original_task = asyncio.create_task
        original_spawn = (
            asyncio.create_subprocess_shell
            if owner_kind == "hook"
            else asyncio.create_subprocess_exec
        )

        async def spawn(*args, **kwargs):
            process = await original_spawn(*args, **kwargs)
            processes.append(process)
            return process

        def task(coroutine, *, name=None, **kwargs):
            if name == (
                "hook-stderr-drain" if owner_kind == "hook" else "mcp-stdio-writer"
            ):
                raise RuntimeError("drain task could not start")
            return original_task(coroutine, name=name, **kwargs)

        monkeypatch.setattr(asyncio, "create_task", task)
        monkeypatch.setattr(
            asyncio,
            "create_subprocess_shell"
            if owner_kind == "hook"
            else "create_subprocess_exec",
            spawn,
        )
        argv = [sys.executable, "-c", "import time; time.sleep(30)"]
        if owner_kind == "hook":
            monkeypatch.setattr(H, "create_scope", lambda: None)
            executor = H.HookCommandExecutor(
                credential_boundary=ProcessCredentialBoundary()
            )
            definition = _definition(
                tmp_path, HookEventType.USER_PROMPT_SUBMIT_EVENT, shlex.join(argv)
            )
            request = H.HookExecutionRequest(
                definition, {}, _scope(), 0, 0, tmp_path, None, None
            )
            result = await executor.execute(request)
            assert result.failure_code == "HOOK_EXECUTOR_FAILURE"
            assert not executor._attempts
            assert executor._sync_slots._value == H.SYNCHRONOUS_COMMAND_SLOTS
            await executor.aclose()
        else:
            monkeypatch.setattr(S, "create_scope", lambda: None)
            config = _parse_server(
                "unit",
                {"transport": {"type": "stdio", "command": argv[0], "args": argv[1:]}},
            )
            transport = S._BoundedStdioTransport(
                config.transport,
                workspace_root=tmp_path,
                credential_boundary=ProcessCredentialBoundary(),
                bounds=DEFAULT_MCP_WIRE_BOUNDS,
            )
            with pytest.raises(RuntimeError, match="drain task"):
                await transport.start()
            await transport.aclose()
            assert transport._tasks and all(task.done() for task in transport._tasks)
        assert len(processes) == 1 and processes[0].returncode is not None
        with pytest.raises(ProcessLookupError):
            os.killpg(processes[0].pid, 0)

    asyncio.run(scenario())


def test_supervisor_host_close_retains_failed_slot_and_repeats_error(tmp_path):
    from pulsara_agent.conversation_kernel.mcp.supervisor import (
        McpHostSupervisor,
        McpConnectionSlot,
        McpPhysicalConcurrencyKind,
    )
    from pulsara_agent.mcp_config import _parse_server
    from pulsara_agent.process_credential_boundary import ProcessCredentialBoundary

    async def scenario():
        failure = InteropScopeError("exact stdio scope did not join")

        class Client:
            calls = 0

            async def aclose(self):
                self.calls += 1
                raise failure

        class Anchor:
            closed = False

            def close(self):
                self.closed = True

        config = _parse_server(
            "unit", {"transport": {"type": "stdio", "command": "/bin/false"}}
        )
        supervisor = McpHostSupervisor(
            session_id="session:unit",
            workspace_root=tmp_path,
            configs=(config,),
            credential_boundary=ProcessCredentialBoundary(),
        )
        client, anchor = Client(), Anchor()
        slot = McpConnectionSlot(
            server_id="unit",
            supervisor_epoch=supervisor.owner_epoch,
            connection_generation=1,
            runtime_config_fingerprint=config.runtime_config_fingerprint,
            client=client,
            concurrency_kind=McpPhysicalConcurrencyKind.SERIAL_SESSION,
            maximum_in_flight=1,
            host_lane=asyncio.Semaphore(1),
            failure_reporter=lambda *args, **kwargs: None,
            physical_lifetime_anchor=anchor,
        )
        supervisor._all_slots.add(slot)
        supervisor._slots["unit"] = slot
        for _ in range(2):
            with pytest.raises(InteropScopeError) as caught:
                await supervisor.aclose()
            assert caught.value is failure
        assert client.calls == 1
        assert slot in supervisor._all_slots
        assert slot._physical_lifetime_anchor is anchor and not anchor.closed
        assert supervisor.configs == (config,)

    asyncio.run(scenario())


def test_supervisor_owns_failed_cleanup_before_slot_publication(tmp_path):
    from pulsara_agent.conversation_kernel.mcp.supervisor import McpHostSupervisor
    from pulsara_agent.mcp_config import _parse_server
    from pulsara_agent.process_credential_boundary import ProcessCredentialBoundary

    async def scenario():
        failure = InteropScopeError("unpublished stdio scope did not join")

        class Client:
            def __init__(self, *args, **kwargs):
                pass

            async def open(self):
                raise RuntimeError("handshake failed after process creation")

            async def aclose(self):
                raise failure

        config = _parse_server(
            "unit",
            {
                "required": False,
                "transport": {"type": "stdio", "command": "/bin/false"},
            },
        )
        supervisor = McpHostSupervisor(
            session_id="session:unit",
            workspace_root=tmp_path,
            configs=(config,),
            credential_boundary=ProcessCredentialBoundary(),
            client_factory=Client,
        )
        await supervisor.start()
        await asyncio.gather(*supervisor._tasks.values(), return_exceptions=True)
        assert not supervisor._slots
        assert len(supervisor._all_slots) == 1
        for _ in range(2):
            with pytest.raises(InteropScopeError) as caught:
                await supervisor.aclose()
            assert caught.value is failure
        assert len(supervisor._all_slots) == 1

    asyncio.run(scenario())
