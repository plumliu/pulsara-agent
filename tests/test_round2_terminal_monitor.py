from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
import sys
from threading import Event, Lock, Thread
from time import monotonic, sleep
from uuid import uuid4

import pytest

from pulsara_agent.conversation_kernel.live import LiveAgentEventBus
from pulsara_agent.conversation_kernel.vocabulary import LiveEventType
from pulsara_agent.conversation_kernel.runner import KernelToolLiveSink
from pulsara_agent.conversation_kernel.runner import (
    KernelToolAuthorizationKind,
    ProcessLocalEffectSettlementDisposition,
)
from pulsara_agent.conversation_kernel.tool_policy import (
    DefaultToolDispatchAuthorizationPolicy,
)
from pulsara_agent.conversation_kernel.tool_runtime import DirectKernelToolPort
from pulsara_agent.ports.terminal_observation import NewTurnInstallation
from pulsara_agent.terminal_process.manager import ProcessRegistry
from pulsara_agent.terminal_process.environment import TerminalEnvConfig
from pulsara_agent.terminal_process.monitor import (
    MAXIMUM_ACTIVE_MONITORS,
    MAXIMUM_AUTONOMOUS_CONTINUATIONS,
    TerminalDeliveryCoverage,
    TerminalMonitorCoordinator,
    TerminalMonitorPolicy,
    TerminalMonitorRejected,
    TerminalMonitorRejectionReason,
)
from pulsara_agent.model_input.contracts import ModelInputScopeKind
from tests.support.round3 import (
    authorize_direct_tool,
    direct_tool_invocation_context,
    invoke_direct_tool,
)


def _name(prefix: str) -> str:
    return f"{prefix}:{uuid4().hex}"


def _spawn_sleeping_process(registry: ProcessRegistry, owner: str, tmp_path: Path):
    state, yielded, _cwd = registry.exec_with_yield(
        terminal_session_id="default",
        command="sleep",
        cwd=tmp_path,
        yield_time_ms=0,
        tty=False,
        max_lifetime_seconds=None,
        owner_host_session_id=owner,
        shell_argv=(sys.executable, "-c", "import time; time.sleep(30)"),
        env=dict(os.environ),
    )
    assert yielded is True
    return state


class _LiveSink(KernelToolLiveSink):
    def __init__(self) -> None:
        self.values: list[str] = []
        self.seen = Event()
        self.lock = Lock()

    def offer_text(self, text: str) -> None:
        with self.lock:
            self.values.append(text)
        if "BEFORE-SLEEP" in text:
            self.seen.set()


def test_round2_terminal_streams_before_physical_completion_for_pipe_and_pty(
    tmp_path: Path,
) -> None:
    async def scenario(tty: bool) -> None:
        session_id = _name("session")
        port = DirectKernelToolPort(
            workspace_root=tmp_path,
            host_owner_id=_name("host"),
            session_id=session_id,
            live_bus=LiveAgentEventBus(),
            authorization_policy=DefaultToolDispatchAuthorizationPolicy(),
        )
        port._terminal.environment_owner.config = TerminalEnvConfig(  # noqa: SLF001
            enable_shell_snapshot=False
        )
        sink = _LiveSink()
        invocation = asyncio.create_task(
            invoke_direct_tool(
                port,
                session_id=session_id,
                tool_name="terminal",
                arguments={
                    "command": (
                        f'{sys.executable} -u -c "import time; '
                        "print('BEFORE-SLEEP', flush=True); time.sleep(0.5); "
                        "print('AFTER-SLEEP', flush=True)\""
                    ),
                    "yield_time_ms": 2_000,
                    "tty": tty,
                },
                tool_call_id=_name("call"),
                attempt_id=_name("attempt"),
                turn_id=_name("turn"),
                assistant_entry_id=_name("entry"),
                live_sink=sink,
            )
        )
        assert await asyncio.to_thread(sink.seen.wait, 1.5)
        assert not invocation.done()
        result = await invocation
        payload = json.loads(result.content)
        assert payload["status"] == "success"
        assert "BEFORE-SLEEP" in "".join(sink.values)
        assert "AFTER-SLEEP" in payload["output"]
        await port.aclose(timeout_seconds=5)

    asyncio.run(scenario(False))
    asyncio.run(scenario(True))


def test_round2_tool_close_terminates_process_before_draining_terminal_thread(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        port = DirectKernelToolPort(
            workspace_root=tmp_path,
            host_owner_id="host:close-order",
            session_id="session:close-order",
            live_bus=LiveAgentEventBus(),
            authorization_policy=DefaultToolDispatchAuthorizationPolicy(),
        )
        port._terminal.environment_owner.config = TerminalEnvConfig(  # noqa: SLF001
            enable_shell_snapshot=False
        )
        sink = _LiveSink()
        invocation = asyncio.create_task(
            invoke_direct_tool(
                port,
                session_id="session:close-order",
                tool_name="terminal",
                arguments={
                    "command": (
                        f'{sys.executable} -u -c "import time; '
                        "print('BEFORE-SLEEP', flush=True); time.sleep(30)\""
                    ),
                    "yield_time_ms": 10_000,
                },
                tool_call_id="call:close-order",
                attempt_id="attempt:close-order",
                turn_id="turn:close-order",
                assistant_entry_id="entry:close-order",
                live_sink=sink,
            )
        )
        assert await asyncio.to_thread(sink.seen.wait, 1.5)
        started = monotonic()
        await port.aclose(timeout_seconds=3)
        assert monotonic() - started < 3
        result = await asyncio.wait_for(invocation, timeout=1)
        payload = json.loads(result.content)
        assert payload["status"] == "killed"
        assert port._physical_io._active == set()  # noqa: SLF001

    asyncio.run(scenario())


def test_round2_foreground_updates_cwd_but_yielded_process_never_does(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        (tmp_path / "foreground").mkdir()
        (tmp_path / "background").mkdir()
        port = DirectKernelToolPort(
            workspace_root=tmp_path,
            host_owner_id="host:cwd",
            session_id="session:cwd",
            live_bus=LiveAgentEventBus(),
            authorization_policy=DefaultToolDispatchAuthorizationPolicy(),
        )
        port._terminal.environment_owner.config = TerminalEnvConfig(  # noqa: SLF001
            enable_shell_snapshot=False
        )

        async def invoke(command: str, *, yield_time_ms: int = 2_000):
            result = await invoke_direct_tool(
                port,
                session_id="session:cwd",
                tool_name="terminal",
                arguments={"command": command, "yield_time_ms": yield_time_ms},
                tool_call_id=_name("call"),
                attempt_id=_name("attempt"),
                turn_id=_name("turn"),
                assistant_entry_id=_name("entry"),
            )
            return json.loads(result.content)

        first = await invoke("cd foreground")
        assert first["cwd"] == str(tmp_path / "foreground")
        yielded = await invoke("cd ../background; sleep 0.4", yield_time_ms=0)
        assert yielded["status"] == "running"
        await invoke_direct_tool(
            port,
            session_id="session:cwd",
            tool_name="terminal_process",
            arguments={
                "action": "wait",
                "process_id": yielded["process_id"],
                "timeout_seconds": 2,
            },
            tool_call_id=_name("call"),
            attempt_id=_name("attempt"),
            turn_id=_name("turn"),
            assistant_entry_id=_name("entry"),
        )
        after = await invoke("pwd")
        assert after["cwd"] == str(tmp_path / "foreground")
        assert str(tmp_path / "foreground") in after["output"]
        await port.aclose(timeout_seconds=5)

    asyncio.run(scenario())


def test_round2_monitor_uses_exact_cursor_and_single_pass_head_tail(
    tmp_path: Path,
) -> None:
    owner = "host:monitor"
    registry = ProcessRegistry()
    registry.activate_owner(owner)
    state, yielded, _cwd = registry.exec_with_yield(
        terminal_session_id="default",
        command="sleep",
        cwd=tmp_path,
        yield_time_ms=0,
        tty=False,
        max_lifetime_seconds=None,
        owner_host_session_id=owner,
        shell_argv=(sys.executable, "-c", "import time; time.sleep(30)"),
        env=dict(os.environ),
    )
    assert yielded is True
    wakes: list[None] = []
    coordinator = TerminalMonitorCoordinator(
        session_id="session:monitor",
        owner_epoch=owner,
        registry=registry,
        live_bus=LiveAgentEventBus(),
        wake_scheduler=lambda: wakes.append(None),
    )
    prepared = coordinator.prepare_registration(
        process_id=state.process_id,
        origin_turn_id="turn:origin",
        origin_attempt_id="attempt:origin",
        origin_result_entry_id="entry:origin-result",
        writer_generation=1,
        authorization_reference="policy:test",
        policy=TerminalMonitorPolicy(
            min_new_output_chars=1,
            quiet_period_ms=0,
            max_output_chars=512,
            minimum_progress_interval_seconds=1800,
        ),
    )
    coordinator.settle_registration(
        prepared.token_id, prepared.token_fingerprint, committed=True
    )
    state.output.append_raw(("HEAD-" + "🙂" * 1_000 + "-TAIL ").encode())
    coordinator._evaluate(prepared.monitor_id, monotonic() + 2_000)  # noqa: SLF001
    target = NewTurnInstallation(
        "turn:observation", "revision:observation:0", "entry:observation"
    )
    attempt = coordinator.freeze(
        monitor_id=prepared.monitor_id,
        target=target,
        workspace_id="workspace:test",
        writer_generation=1,
        actor_id="host:test",
    )
    assert attempt is not None
    assert attempt.content.delivery_coverage is TerminalDeliveryCoverage.HEAD_TAIL
    assert attempt.content.output.startswith("HEAD-")
    assert attempt.content.output.endswith("-TAIL ")
    assert len(attempt.content.output) <= 512
    assert len(attempt.content.canonical_bytes()) <= 32_000
    assert (
        attempt.content.included_source_utf8_bytes
        + attempt.content.omitted_by_delivery_bound_utf8_bytes
        == attempt.content.available_source_utf8_bytes
    )
    coordinator.settle_installation(attempt, accepted=True)

    state.output.append_raw(b"next-range ")
    coordinator._evaluate(prepared.monitor_id, monotonic() + 4_000)  # noqa: SLF001
    next_attempt = coordinator.freeze(
        monitor_id=prepared.monitor_id,
        target=target,
        workspace_id="workspace:test",
        writer_generation=1,
        actor_id="host:test",
    )
    assert next_attempt is not None
    assert next_attempt.content.delivery_coverage is TerminalDeliveryCoverage.COMPLETE
    assert next_attempt.content.output == "next-range "
    assert "HEAD-" not in next_attempt.content.output

    coordinator.settle_installation(next_attempt, accepted=True)
    coordinator.stop_admission_and_close(timeout_seconds=2)
    results = registry.release_owner(owner, timeout_seconds=5)
    assert results[0].status.value == "killed"
    assert wakes


def test_round2_monitor_registration_cannot_miss_completion_between_snapshot_and_publish(
    tmp_path: Path,
) -> None:
    owner = "host:registration-race"
    registry = ProcessRegistry()
    registry.activate_owner(owner)
    state, yielded, _cwd = registry.exec_with_yield(
        terminal_session_id="default",
        command="race",
        cwd=tmp_path,
        yield_time_ms=0,
        tty=False,
        max_lifetime_seconds=None,
        owner_host_session_id=owner,
        shell_argv=(sys.executable, "-c", "import time; time.sleep(30)"),
        env=dict(os.environ),
    )
    assert yielded is True
    coordinator = TerminalMonitorCoordinator(
        session_id="session:registration-race",
        owner_epoch=owner,
        registry=registry,
        live_bus=LiveAgentEventBus(),
        wake_scheduler=lambda: None,
    )
    original = registry.snapshot_and_subscribe

    def complete_after_snapshot(*args, **kwargs):
        snapshot, subscription = original(*args, **kwargs)
        registry.kill(
            state.process_id,
            max_output_chars=512,
            owner_host_session_id=owner,
        )
        return snapshot, subscription

    registry.snapshot_and_subscribe = complete_after_snapshot  # type: ignore[method-assign]
    prepared = coordinator.prepare_registration(
        process_id=state.process_id,
        origin_turn_id="turn:race",
        origin_attempt_id="attempt:race",
        origin_result_entry_id="entry:race",
        writer_generation=1,
        authorization_reference="policy:race",
        policy=TerminalMonitorPolicy(),
    )
    coordinator.settle_registration(
        prepared.token_id, prepared.token_fingerprint, committed=True
    )
    deadline = monotonic() + 1
    while prepared.monitor_id not in coordinator.pending_monitor_ids():
        assert monotonic() < deadline
        sleep(0.01)
    attempt = coordinator.freeze(
        monitor_id=prepared.monitor_id,
        target=NewTurnInstallation("turn:new", "revision:new", "entry:new"),
        workspace_id="workspace:race",
        writer_generation=1,
        actor_id="host:race",
    )
    assert attempt is not None
    assert attempt.content.observation_kind.value == "COMPLETION"
    coordinator.settle_installation(attempt, accepted=True)
    coordinator.stop_admission_and_close(timeout_seconds=2)
    registry.release_owner(owner, timeout_seconds=2)


def test_round2_monitor_cancel_after_freeze_preserves_exact_attempt_until_settlement(
    tmp_path: Path,
) -> None:
    owner = "host:cancel-after-freeze"
    registry = ProcessRegistry()
    registry.activate_owner(owner)
    state, yielded, _cwd = registry.exec_with_yield(
        terminal_session_id="default",
        command="cancel-race",
        cwd=tmp_path,
        yield_time_ms=0,
        tty=False,
        max_lifetime_seconds=None,
        owner_host_session_id=owner,
        shell_argv=(sys.executable, "-c", "import time; time.sleep(30)"),
        env=dict(os.environ),
    )
    assert yielded is True
    coordinator = TerminalMonitorCoordinator(
        session_id="session:cancel-after-freeze",
        owner_epoch=owner,
        registry=registry,
        live_bus=LiveAgentEventBus(),
        wake_scheduler=lambda: None,
    )
    prepared = coordinator.prepare_registration(
        process_id=state.process_id,
        origin_turn_id="turn:cancel-race",
        origin_attempt_id="attempt:cancel-race",
        origin_result_entry_id="entry:cancel-race",
        writer_generation=1,
        authorization_reference="policy:cancel-race",
        policy=TerminalMonitorPolicy(
            min_new_output_chars=1,
            quiet_period_ms=0,
            minimum_progress_interval_seconds=1800,
        ),
    )
    coordinator.settle_registration(
        prepared.token_id, prepared.token_fingerprint, committed=True
    )
    state.output.append_raw(b"freeze-wins ")
    coordinator._evaluate(prepared.monitor_id, monotonic() + 2_000)  # noqa: SLF001
    attempt = coordinator.freeze(
        monitor_id=prepared.monitor_id,
        target=NewTurnInstallation(
            "turn:cancel-winner", "revision:cancel-winner", "entry:cancel-winner"
        ),
        workspace_id="workspace:cancel-race",
        writer_generation=1,
        actor_id="host:cancel-race",
    )
    assert attempt is not None

    assert coordinator.cancel(prepared.monitor_id) == "cancelled"
    assert coordinator.cancel(prepared.monitor_id) == "already_terminal"
    assert coordinator.current_installation_attempt(prepared.monitor_id) == attempt
    assert coordinator.pending_monitor_ids() == (prepared.monitor_id,)
    assert (
        coordinator.pending_observation_id(prepared.monitor_id)
        == attempt.content.observation_id
    )

    # A proven NONE/conflict retires the cancelled candidate without
    # resurrecting its draft or future observations.
    coordinator.settle_installation(attempt, accepted=False)
    assert coordinator.current_installation_attempt(prepared.monitor_id) is None
    assert coordinator.pending_monitor_ids() == ()

    coordinator.stop_admission_and_close(timeout_seconds=2)
    registry.release_owner(owner, timeout_seconds=2)


def test_round2_monitor_optional_output_heartbeat_expiry_and_cancel_do_not_kill(
    tmp_path: Path,
) -> None:
    owner = "host:monitor-policy"
    registry = ProcessRegistry()
    registry.activate_owner(owner)
    heartbeat_process = _spawn_sleeping_process(registry, owner, tmp_path)
    expiry_process = _spawn_sleeping_process(registry, owner, tmp_path)
    coordinator = TerminalMonitorCoordinator(
        session_id="session:monitor-policy",
        owner_epoch=owner,
        registry=registry,
        live_bus=LiveAgentEventBus(),
        wake_scheduler=lambda: None,
    )

    heartbeat = coordinator.prepare_registration(
        process_id=heartbeat_process.process_id,
        origin_turn_id="turn:heartbeat",
        origin_attempt_id="attempt:heartbeat",
        origin_result_entry_id="entry:heartbeat",
        writer_generation=1,
        authorization_reference="policy:heartbeat",
        policy=TerminalMonitorPolicy(
            min_new_output_chars=None,
            heartbeat_interval_seconds=5,
        ),
    )
    coordinator.settle_registration(
        heartbeat.token_id, heartbeat.token_fingerprint, committed=True
    )
    heartbeat_process.output.append_raw(b"output-does-not-enable-progress ")
    coordinator._evaluate(heartbeat.monitor_id, monotonic() + 4)  # noqa: SLF001
    assert coordinator.pending_monitor_ids() == ()
    coordinator._evaluate(heartbeat.monitor_id, monotonic() + 6)  # noqa: SLF001
    heartbeat_attempt = coordinator.freeze(
        monitor_id=heartbeat.monitor_id,
        target=NewTurnInstallation(
            "turn:heartbeat-wake", "revision:heartbeat-wake", "entry:heartbeat-wake"
        ),
        workspace_id="workspace:monitor-policy",
        writer_generation=1,
        actor_id="host:monitor-policy",
    )
    assert heartbeat_attempt is not None
    assert heartbeat_attempt.content.observation_kind.value == "HEARTBEAT"
    coordinator.settle_installation(heartbeat_attempt, accepted=True)

    expiry = coordinator.prepare_registration(
        process_id=expiry_process.process_id,
        origin_turn_id="turn:expiry",
        origin_attempt_id="attempt:expiry",
        origin_result_entry_id="entry:expiry",
        writer_generation=1,
        authorization_reference="policy:expiry",
        policy=TerminalMonitorPolicy(maximum_duration_seconds=1),
    )
    coordinator.settle_registration(
        expiry.token_id, expiry.token_fingerprint, committed=True
    )
    coordinator._evaluate(expiry.monitor_id, monotonic() + 2)  # noqa: SLF001
    expiry_attempt = coordinator.freeze(
        monitor_id=expiry.monitor_id,
        target=NewTurnInstallation(
            "turn:expiry-wake", "revision:expiry-wake", "entry:expiry-wake"
        ),
        workspace_id="workspace:monitor-policy",
        writer_generation=1,
        actor_id="host:monitor-policy",
    )
    assert expiry_attempt is not None
    assert expiry_attempt.content.observation_kind.value == "EXPIRY"
    coordinator.settle_installation(expiry_attempt, accepted=True)
    assert coordinator.cancel(expiry.monitor_id) == "already_terminal"

    assert coordinator.cancel(heartbeat.monitor_id) == "cancelled"
    assert heartbeat_process.process.poll() is None
    coordinator.stop_admission_and_close(timeout_seconds=2)
    registry.release_owner(owner, timeout_seconds=3)


def test_round2_monitor_completion_coalesces_pending_progress_and_cursor_advances(
    tmp_path: Path,
) -> None:
    owner = "host:monitor-coalesce"
    registry = ProcessRegistry()
    registry.activate_owner(owner)
    state = _spawn_sleeping_process(registry, owner, tmp_path)
    coordinator = TerminalMonitorCoordinator(
        session_id="session:monitor-coalesce",
        owner_epoch=owner,
        registry=registry,
        live_bus=LiveAgentEventBus(),
        wake_scheduler=lambda: None,
    )
    prepared = coordinator.prepare_registration(
        process_id=state.process_id,
        origin_turn_id="turn:coalesce",
        origin_attempt_id="attempt:coalesce",
        origin_result_entry_id="entry:coalesce",
        writer_generation=1,
        authorization_reference="policy:coalesce",
        policy=TerminalMonitorPolicy(
            min_new_output_chars=1,
            quiet_period_ms=0,
            minimum_progress_interval_seconds=5,
        ),
    )
    coordinator.settle_registration(
        prepared.token_id, prepared.token_fingerprint, committed=True
    )
    state.output.append_raw(b"first ")
    coordinator._evaluate(prepared.monitor_id, monotonic() + 10)  # noqa: SLF001
    state.output.append_raw(b"second ")
    coordinator._evaluate(prepared.monitor_id, monotonic() + 20)  # noqa: SLF001
    coordinator.process_completed(state.process_id, status="success", exit_code=0)
    coordinator._evaluate(prepared.monitor_id, monotonic() + 21)  # noqa: SLF001
    completion = coordinator.freeze(
        monitor_id=prepared.monitor_id,
        target=NewTurnInstallation(
            "turn:coalesce-wake", "revision:coalesce-wake", "entry:coalesce-wake"
        ),
        workspace_id="workspace:monitor-coalesce",
        writer_generation=1,
        actor_id="host:monitor-coalesce",
    )
    assert completion is not None
    assert completion.content.observation_kind.value == "COMPLETION"
    assert completion.content.output == "first second "
    coordinator.settle_installation(completion, accepted=True)
    assert coordinator.pending_monitor_ids() == ()
    coordinator.stop_admission_and_close(timeout_seconds=2)
    registry.release_owner(owner, timeout_seconds=3)


def test_round2_terminal_monitor_tool_root_settlement_and_subagent_rejection(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        session_id = "session:monitor-tool"
        live_bus = LiveAgentEventBus()
        port = DirectKernelToolPort(
            workspace_root=tmp_path,
            host_owner_id="host:monitor-tool",
            session_id=session_id,
            live_bus=live_bus,
            authorization_policy=DefaultToolDispatchAuthorizationPolicy(),
        )
        port._terminal.environment_owner.config = TerminalEnvConfig(  # noqa: SLF001
            enable_shell_snapshot=False
        )
        started = await invoke_direct_tool(
            port,
            session_id=session_id,
            tool_name="terminal",
            arguments={"command": "sleep 30", "yield_time_ms": 0},
            tool_call_id="call:start-monitor-tool",
            attempt_id="attempt:start-monitor-tool",
            turn_id="turn:start-monitor-tool",
            assistant_entry_id="entry:start-monitor-tool",
        )
        process_id = json.loads(started.content)["process_id"]

        async def invoke_root(label: str, tool_name: str, arguments: dict[str, object]):
            return await invoke_direct_tool(
                port,
                session_id=session_id,
                workspace_id="workspace:monitor-tool",
                tool_name=tool_name,
                arguments=arguments,
                tool_call_id=f"call:{label}",
                attempt_id=f"attempt:{label}",
                turn_id=f"turn:{label}",
                assistant_entry_id=f"entry:{label}",
            )

        mismatch_borrow, mismatch_context = direct_tool_invocation_context(
            port,
            session_id=session_id,
            workspace_id="workspace:monitor-tool",
            tool_name="terminal_monitor",
            tool_call_id="call:different",
            attempt_id="attempt:different",
            turn_id="turn:different",
            assistant_entry_id="entry:different",
        )
        with pytest.raises(RuntimeError, match="exact-join"):
            try:
                await port.invoke(
                    tool_name="terminal_monitor",
                    arguments={"action": "register", "process_id": process_id},
                    tool_call_id="call:mismatch",
                    attempt_id="attempt:mismatch",
                    turn_id="turn:mismatch",
                    assistant_entry_id="entry:mismatch",
                    invocation_context=mismatch_context,
                )
            finally:
                mismatch_borrow.close()
        assert port.terminal_monitor_coordinator.list_current() == ()

        child_surface = port.snapshot_tool_surface(
            conversation_scope_kind=ModelInputScopeKind.SUBAGENT_TASK,
            scope_subagent_task_id="task:child",
        )
        assert "terminal_monitor" not in {
            spec.name for spec in child_surface.model_surface.tool_specs
        }
        with pytest.raises(RuntimeError, match="not advertised"):
            await invoke_direct_tool(
                port,
                session_id=session_id,
                workspace_id="workspace:monitor-tool",
                tool_name="terminal_monitor",
                arguments={"action": "register", "process_id": process_id},
                tool_call_id="call:SUBAGENT_TASK",
                attempt_id="attempt:SUBAGENT_TASK",
                turn_id="turn:SUBAGENT_TASK",
                assistant_entry_id="entry:SUBAGENT_TASK",
                conversation_scope_kind=ModelInputScopeKind.SUBAGENT_TASK,
                scope_subagent_task_id="task:child",
            )

        registered = await invoke_root(
            "ROOT",
            "terminal_monitor",
            {"action": "register", "process_id": process_id},
        )
        token = registered.process_local_settlement
        assert token is not None
        assert tuple(token.__dataclass_fields__) == (  # type: ignore[attr-defined]
            "token_id",
            "token_fingerprint",
        )
        await port.settle_process_local_effect(
            token, ProcessLocalEffectSettlementDisposition.COMMITTED
        )
        assert port.terminal_monitor_coordinator.list_current()[0]["state"] == "ACTIVE"

        duplicate = await invoke_root(
            "duplicate",
            "terminal_monitor",
            {"action": "register", "process_id": process_id},
        )
        assert json.loads(duplicate.content) == {
            "reason": "DUPLICATE_PROCESS_MONITOR",
            "status": "REJECTED",
        }
        unknown = await invoke_root(
            "missing",
            "terminal_monitor",
            {"action": "register", "process_id": "process:missing"},
        )
        assert json.loads(unknown.content) == {
            "reason": "PROCESS_NOT_FOUND",
            "status": "REJECTED",
        }
        inventory = await invoke_root("list", "terminal_monitor", {"action": "list"})
        monitors = json.loads(inventory.content)["monitors"]
        assert len(monitors) == 1 and monitors[0]["state"] == "ACTIVE"
        cancelled = await invoke_root(
            "cancel",
            "terminal_monitor",
            {"action": "cancel", "monitor_id": monitors[0]["monitor_id"]},
        )
        assert json.loads(cancelled.content)["cancellation_outcome"] == "cancelled"
        processes = await invoke_root(
            "list-processes", "terminal_process", {"action": "list"}
        )
        assert json.loads(processes.content)["processes"][0]["status"] == "running"
        await invoke_root(
            "kill-process",
            "terminal_process",
            {"action": "kill", "process_id": process_id},
        )
        terminal = await invoke_root(
            "terminal-process",
            "terminal_monitor",
            {"action": "register", "process_id": process_id},
        )
        assert json.loads(terminal.content) == {
            "reason": "PROCESS_ALREADY_TERMINAL",
            "status": "REJECTED",
        }
        await port.aclose(timeout_seconds=3)

    asyncio.run(scenario())


def test_round2_malformed_monitor_input_uses_common_invalid_arguments_contract(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        session_id = _name("session")
        port = DirectKernelToolPort(
            workspace_root=tmp_path,
            host_owner_id=_name("host"),
            session_id=session_id,
            live_bus=LiveAgentEventBus(),
            authorization_policy=DefaultToolDispatchAuthorizationPolicy(),
        )
        try:
            decision = await authorize_direct_tool(
                port,
                session_id=session_id,
                tool_name="terminal_monitor",
                arguments={"action": "malformed"},
                tool_call_id=_name("call"),
                turn_id=_name("turn"),
                assistant_entry_id=_name("entry"),
            )
            assert decision.kind is KernelToolAuthorizationKind.INVALID_ARGUMENTS
            assert "invalid tool arguments" in decision.public_message
        finally:
            await port.aclose(timeout_seconds=3)

    asyncio.run(scenario())


def test_round2_subagent_terminal_completion_preserves_exact_process_origin(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        live_bus = LiveAgentEventBus()
        observer, _generation, revision = live_bus.subscribe()
        port = DirectKernelToolPort(
            workspace_root=tmp_path,
            host_owner_id="host:subagent-terminal-origin",
            session_id="session:subagent-terminal-origin",
            live_bus=live_bus,
            authorization_policy=DefaultToolDispatchAuthorizationPolicy(),
        )
        port._terminal.environment_owner.config = TerminalEnvConfig(  # noqa: SLF001
            enable_shell_snapshot=False
        )
        started = await invoke_direct_tool(
            port,
            session_id="session:subagent-terminal-origin",
            workspace_id="workspace:subagent-terminal-origin",
            tool_name="terminal",
            arguments={"command": "sleep 0.15", "yield_time_ms": 0},
            tool_call_id="call:child-origin",
            attempt_id="attempt:child-origin",
            turn_id="turn:child-origin",
            assistant_entry_id="entry:child-origin",
            conversation_scope_kind=ModelInputScopeKind.SUBAGENT_TASK,
            scope_subagent_task_id="task:child-origin",
        )
        assert json.loads(started.content)["status"] == "running"

        completion = None
        deadline = monotonic() + 2
        while completion is None:
            observed = live_bus.observe(
                observer, after_revision=revision, maximum_events=32
            )
            revision = observed.latest_revision
            completion = next(
                (
                    event
                    for event in observed.events
                    if event.event_type is LiveEventType.TERMINAL_PROCESS_COMPLETED
                ),
                None,
            )
            if completion is None:
                assert monotonic() < deadline
                await asyncio.sleep(0.01)

        assert completion.turn_id == "turn:child-origin"
        assert completion.scope_kind == "SUBAGENT_TASK"
        assert completion.scope_subagent_task_id == "task:child-origin"
        await port.aclose(timeout_seconds=3)

    asyncio.run(scenario())


def test_round2_monitor_dormant_capture_requires_commit_and_discard_is_terminal(
    tmp_path: Path,
) -> None:
    owner = "host:dormant-monitor"
    registry = ProcessRegistry()
    registry.activate_owner(owner)
    committed_process = _spawn_sleeping_process(registry, owner, tmp_path)
    discarded_process = _spawn_sleeping_process(registry, owner, tmp_path)
    wake_count = 0

    def wake() -> None:
        nonlocal wake_count
        wake_count += 1

    coordinator = TerminalMonitorCoordinator(
        session_id="session:dormant-monitor",
        owner_epoch=owner,
        registry=registry,
        live_bus=LiveAgentEventBus(),
        wake_scheduler=wake,
    )
    policy = TerminalMonitorPolicy(
        min_new_output_chars=1,
        quiet_period_ms=0,
        minimum_progress_interval_seconds=5,
    )
    committed = coordinator.prepare_registration(
        process_id=committed_process.process_id,
        origin_turn_id="turn:dormant-commit",
        origin_attempt_id="attempt:dormant-commit",
        origin_result_entry_id="entry:dormant-commit",
        writer_generation=1,
        authorization_reference="policy:dormant-commit",
        policy=policy,
    )
    committed_process.output.append_raw(b"captured-before-commit ")
    coordinator.process_completed(
        committed_process.process_id, status="success", exit_code=0
    )
    coordinator._evaluate(committed.monitor_id, monotonic() + 10)  # noqa: SLF001
    assert coordinator.pending_monitor_ids() == ()
    assert wake_count == 0

    coordinator.settle_registration(
        committed.token_id, committed.token_fingerprint, committed=True
    )
    coordinator._evaluate(committed.monitor_id, monotonic() + 11)  # noqa: SLF001
    assert coordinator.pending_monitor_ids() == (committed.monitor_id,)
    assert wake_count == 1

    discarded = coordinator.prepare_registration(
        process_id=discarded_process.process_id,
        origin_turn_id="turn:dormant-discard",
        origin_attempt_id="attempt:dormant-discard",
        origin_result_entry_id="entry:dormant-discard",
        writer_generation=1,
        authorization_reference="policy:dormant-discard",
        policy=policy,
    )
    discarded_process.output.append_raw(b"must-never-deliver ")
    coordinator.settle_registration(
        discarded.token_id, discarded.token_fingerprint, committed=False
    )
    coordinator._evaluate(discarded.monitor_id, monotonic() + 20)  # noqa: SLF001
    assert coordinator.cancel(discarded.monitor_id) == "already_terminal"
    assert coordinator.pending_monitor_ids() == (committed.monitor_id,)

    coordinator.cancel(committed.monitor_id)
    coordinator.stop_admission_and_close(timeout_seconds=2)
    registry.release_owner(owner, timeout_seconds=3)


def test_round2_monitor_successor_is_exactly_after_inflight_and_rejection_retries_first(
    tmp_path: Path,
) -> None:
    owner = "host:successor-monitor"
    registry = ProcessRegistry()
    registry.activate_owner(owner)
    state = _spawn_sleeping_process(registry, owner, tmp_path)
    coordinator = TerminalMonitorCoordinator(
        session_id="session:successor-monitor",
        owner_epoch=owner,
        registry=registry,
        live_bus=LiveAgentEventBus(),
        wake_scheduler=lambda: None,
    )
    prepared = coordinator.prepare_registration(
        process_id=state.process_id,
        origin_turn_id="turn:successor",
        origin_attempt_id="attempt:successor",
        origin_result_entry_id="entry:successor",
        writer_generation=1,
        authorization_reference="policy:successor",
        policy=TerminalMonitorPolicy(
            min_new_output_chars=1,
            quiet_period_ms=0,
            minimum_progress_interval_seconds=5,
        ),
    )
    coordinator.settle_registration(
        prepared.token_id, prepared.token_fingerprint, committed=True
    )
    target = NewTurnInstallation(
        "turn:successor-wake", "revision:successor-wake", "entry:successor-wake"
    )
    state.output.append_raw(b"first-range ")
    coordinator._evaluate(prepared.monitor_id, monotonic() + 10)  # noqa: SLF001
    first = coordinator.freeze(
        monitor_id=prepared.monitor_id,
        target=target,
        workspace_id="workspace:successor",
        writer_generation=1,
        actor_id="host:successor",
    )
    assert first is not None and first.content.output == "first-range "

    state.output.append_raw(b"second-range ")
    coordinator._evaluate(prepared.monitor_id, monotonic() + 20)  # noqa: SLF001
    assert coordinator.current_installation_attempt(prepared.monitor_id) == first
    with coordinator._lock:  # noqa: SLF001
        successor = coordinator._registrations[prepared.monitor_id].successor  # noqa: SLF001
    assert successor is not None
    assert successor.content.output == "second-range "
    assert successor.content.observation_ordinal == 2

    coordinator.settle_installation(first, accepted=False)
    retried = coordinator.freeze(
        monitor_id=prepared.monitor_id,
        target=target,
        workspace_id="workspace:successor",
        writer_generation=1,
        actor_id="host:successor",
    )
    assert retried is not None
    assert retried.content == first.content
    assert retried.candidate_fingerprint == first.candidate_fingerprint
    coordinator.settle_installation(retried, accepted=True)

    second = coordinator.freeze(
        monitor_id=prepared.monitor_id,
        target=target,
        workspace_id="workspace:successor",
        writer_generation=1,
        actor_id="host:successor",
    )
    assert second is not None
    assert second.content.output == "second-range "
    assert second.content.observation_ordinal == 2
    coordinator.settle_installation(second, accepted=True)
    coordinator.cancel(prepared.monitor_id)
    coordinator.stop_admission_and_close(timeout_seconds=2)
    registry.release_owner(owner, timeout_seconds=3)


def test_round2_monitor_discards_stale_lock_free_read_after_draft_freeze(
    tmp_path: Path,
) -> None:
    owner = "host:successor-race"
    registry = ProcessRegistry()
    registry.activate_owner(owner)
    state = _spawn_sleeping_process(registry, owner, tmp_path)
    coordinator = TerminalMonitorCoordinator(
        session_id="session:successor-race",
        owner_epoch=owner,
        registry=registry,
        live_bus=LiveAgentEventBus(),
        wake_scheduler=lambda: None,
    )
    prepared = coordinator.prepare_registration(
        process_id=state.process_id,
        origin_turn_id="turn:successor-race",
        origin_attempt_id="attempt:successor-race",
        origin_result_entry_id="entry:successor-race",
        writer_generation=1,
        authorization_reference="policy:successor-race",
        policy=TerminalMonitorPolicy(
            min_new_output_chars=1,
            quiet_period_ms=0,
            minimum_progress_interval_seconds=1800,
        ),
    )
    coordinator.settle_registration(
        prepared.token_id, prepared.token_fingerprint, committed=True
    )
    target = NewTurnInstallation(
        "turn:successor-race-wake",
        "revision:successor-race-wake",
        "entry:successor-race-wake",
    )
    state.output.append_raw(b"first-range ")
    coordinator._evaluate(prepared.monitor_id, monotonic() + 2_000)  # noqa: SLF001

    entered_read = Event()
    release_read = Event()
    original_slice = registry.observation_slice

    def blocked_slice(*args, **kwargs):
        entered_read.set()
        assert release_read.wait(2)
        return original_slice(*args, **kwargs)

    registry.observation_slice = blocked_slice  # type: ignore[method-assign]
    state.output.append_raw(b"second-range ")
    evaluation = Thread(
        target=coordinator._evaluate,  # noqa: SLF001
        args=(prepared.monitor_id, monotonic() + 4_000),
    )
    evaluation.start()
    assert entered_read.wait(1)
    first = coordinator.freeze(
        monitor_id=prepared.monitor_id,
        target=target,
        workspace_id="workspace:successor-race",
        writer_generation=1,
        actor_id="host:successor-race",
    )
    assert first is not None and first.content.output == "first-range "
    release_read.set()
    evaluation.join(timeout=2)
    assert evaluation.is_alive() is False

    # The stale read selected first+second from the pre-freeze base and must
    # have been discarded.  A new evaluation starts exactly after in-flight.
    registry.observation_slice = original_slice  # type: ignore[method-assign]
    coordinator._evaluate(prepared.monitor_id, monotonic() + 6_000)  # noqa: SLF001
    with coordinator._lock:  # noqa: SLF001
        successor = coordinator._registrations[prepared.monitor_id].successor  # noqa: SLF001
    assert successor is not None
    assert successor.content.output == "second-range "
    assert "first-range" not in successor.content.output

    coordinator.settle_installation(first, accepted=True)
    second = coordinator.freeze(
        monitor_id=prepared.monitor_id,
        target=target,
        workspace_id="workspace:successor-race",
        writer_generation=1,
        actor_id="host:successor-race",
    )
    assert second is not None and second.content.output == "second-range "
    coordinator.settle_installation(second, accepted=True)
    coordinator.cancel(prepared.monitor_id)
    coordinator.stop_admission_and_close(timeout_seconds=2)
    registry.release_owner(owner, timeout_seconds=3)


def test_round2_monitor_capacity_and_autonomy_are_finite_and_idle_observer_is_nonblocking(
    tmp_path: Path,
) -> None:
    owner = "host:monitor-bounds"
    registry = ProcessRegistry(max_live_processes=MAXIMUM_ACTIVE_MONITORS + 1)
    registry.activate_owner(owner)
    processes = [
        _spawn_sleeping_process(registry, owner, tmp_path)
        for _ in range(MAXIMUM_ACTIVE_MONITORS + 1)
    ]
    live_bus = LiveAgentEventBus()
    live_bus.subscribe()
    coordinator = TerminalMonitorCoordinator(
        session_id="session:monitor-bounds",
        owner_epoch=owner,
        registry=registry,
        live_bus=live_bus,
        wake_scheduler=lambda: None,
    )
    registrations = []
    policy = TerminalMonitorPolicy(
        min_new_output_chars=1,
        quiet_period_ms=0,
        minimum_progress_interval_seconds=5,
    )
    for index, process in enumerate(processes[:MAXIMUM_ACTIVE_MONITORS]):
        prepared = coordinator.prepare_registration(
            process_id=process.process_id,
            origin_turn_id=f"turn:bound:{index}",
            origin_attempt_id=f"attempt:bound:{index}",
            origin_result_entry_id=f"entry:bound:{index}",
            writer_generation=1,
            authorization_reference=f"policy:bound:{index}",
            policy=policy,
        )
        coordinator.settle_registration(
            prepared.token_id, prepared.token_fingerprint, committed=True
        )
        registrations.append(prepared)
    with pytest.raises(TerminalMonitorRejected) as rejection:
        coordinator.prepare_registration(
            process_id=processes[-1].process_id,
            origin_turn_id="turn:over-capacity",
            origin_attempt_id="attempt:over-capacity",
            origin_result_entry_id="entry:over-capacity",
            writer_generation=1,
            authorization_reference="policy:over-capacity",
            policy=policy,
        )
    assert rejection.value.reason is TerminalMonitorRejectionReason.CAPACITY_EXHAUSTED

    chosen = registrations[0]
    chosen_process = processes[0]
    target = NewTurnInstallation("turn:autonomy", "revision:autonomy", "entry:autonomy")
    started = monotonic()
    for index in range(MAXIMUM_AUTONOMOUS_CONTINUATIONS):
        chosen_process.output.append_raw(f"progress-{index} ".encode())
        coordinator._evaluate(chosen.monitor_id, started + 10 * (index + 1))  # noqa: SLF001
        attempt = coordinator.freeze(
            monitor_id=chosen.monitor_id,
            target=target,
            workspace_id="workspace:autonomy",
            writer_generation=1,
            actor_id="host:autonomy",
        )
        assert attempt is not None
        coordinator.settle_installation(attempt, accepted=True)
    assert coordinator.cancel(chosen.monitor_id) == "already_terminal"
    assert monotonic() - started < 1

    coordinator.stop_admission_and_close(timeout_seconds=2)
    registry.release_owner(owner, timeout_seconds=5)
