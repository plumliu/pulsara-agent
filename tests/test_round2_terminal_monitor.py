from __future__ import annotations
from pulsara_agent.identity import derive_id

from pulsara_agent.identity import random_id

import asyncio
import json
import os
from pathlib import Path
import sys
from threading import Event, Lock, Thread
from time import monotonic, sleep

import pytest

from pulsara_agent.conversation_kernel.live import LiveAgentEventBus
from pulsara_agent.conversation_kernel.vocabulary import LiveEventType
from pulsara_agent.conversation_kernel.tool_contracts import (
    KernelToolLiveSink,
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
    prepare_test_direct_tool_surface,
)


def _name(prefix: str) -> str:
    return random_id(prefix)


def _spawn_sleeping_process(registry: ProcessRegistry, owner: str, tmp_path: Path):
    state, yielded = registry.exec_with_yield(
        command="sleep",
        cwd=tmp_path,
        yield_time_ms=0,
        tty=False,
        max_lifetime_seconds=None,
        owner_host_session_id=owner,
        shell_argv=(sys.executable, "-c", "import time; time.sleep(30)"),
        decision_deadline_monotonic=monotonic() + 5,
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
        assert "shell_diagnostic" not in payload
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
            host_owner_id='host:yxs2scqfk5edwydkur4vtl4y7a',
            session_id='session:pr52nocqxglhwexlvqa2agq3bi',
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
                session_id='session:pr52nocqxglhwexlvqa2agq3bi',
                tool_name="terminal",
                arguments={
                    "command": (
                        f'{sys.executable} -u -c "import time; '
                        "print('BEFORE-SLEEP', flush=True); time.sleep(30)\""
                    ),
                    "yield_time_ms": 10_000,
                },
                tool_call_id="call:close-order",
                attempt_id='attempt:kgm43uulgdp5vvsyzreo42cka4',
                turn_id='turn:rafljvcia7jgtxrwdf5kwiqmvu',
                assistant_entry_id='entry:zf2juceggf5fnzpoxsxecoagx4',
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


def test_round2_foreground_and_yielded_cd_leave_workspace_default(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        (tmp_path / "foreground").mkdir()
        (tmp_path / "background").mkdir()
        port = DirectKernelToolPort(
            workspace_root=tmp_path,
            host_owner_id='host:dugldp6ni2dxvuywv5a3utbnii',
            session_id='session:yb3rgzdbtfst4jirqaeox4wo6a',
            live_bus=LiveAgentEventBus(),
            authorization_policy=DefaultToolDispatchAuthorizationPolicy(),
        )
        port._terminal.environment_owner.config = TerminalEnvConfig(  # noqa: SLF001
            enable_shell_snapshot=False
        )

        async def invoke(command: str, *, yield_time_ms: int = 2_000):
            result = await invoke_direct_tool(
                port,
                session_id='session:yb3rgzdbtfst4jirqaeox4wo6a',
                tool_name="terminal",
                arguments={"command": command, "yield_time_ms": yield_time_ms},
                tool_call_id=_name("call"),
                attempt_id=_name("attempt"),
                turn_id=_name("turn"),
                assistant_entry_id=_name("entry"),
            )
            return json.loads(result.content)

        first = await invoke("cd foreground")
        assert first["cwd"] == str(tmp_path)
        yielded = await invoke("cd background; sleep 0.4", yield_time_ms=0)
        assert yielded["status"] == "running"
        await invoke_direct_tool(
            port,
            session_id='session:yb3rgzdbtfst4jirqaeox4wo6a',
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
        assert after["cwd"] == str(tmp_path)
        assert after["output"].strip() == str(tmp_path)
        await port.aclose(timeout_seconds=5)

    asyncio.run(scenario())


def test_round2_monitor_uses_exact_cursor_and_single_pass_head_tail(
    tmp_path: Path,
) -> None:
    owner = 'host:cokgwd42wr3sz45pia23zomsjm'
    registry = ProcessRegistry()
    registry.activate_owner(owner)
    state, yielded = registry.exec_with_yield(
        command="sleep",
        cwd=tmp_path,
        yield_time_ms=0,
        tty=False,
        max_lifetime_seconds=None,
        owner_host_session_id=owner,
        shell_argv=(sys.executable, "-c", "import time; time.sleep(30)"),
        decision_deadline_monotonic=monotonic() + 5,
        env=dict(os.environ),
    )
    assert yielded is True
    wakes: list[None] = []
    coordinator = TerminalMonitorCoordinator(
        session_id='session:r3tnljzotfqlsqf6x7oyutgjcq',
        owner_epoch=owner,
        registry=registry,
        live_bus=LiveAgentEventBus(),
        wake_scheduler=lambda: wakes.append(None),
    )
    prepared = coordinator.prepare_registration(
        process_id=state.process_id,
        origin_turn_id='turn:qb63tb3eshpbkryzmq6wzhzt2u',
        origin_attempt_id='attempt:k5lkisrtjrfo2x4h4sp54sznha',
        origin_result_entry_id='entry:jjhxgegvfzt7fe53ocfbndr2q4',
        writer_generation=1,
        authorization_reference="policy:test",
        policy=TerminalMonitorPolicy(
            min_new_output_chars=1,
            quiet_period_ms=0,
            max_output_chars=512,
            minimum_progress_interval_seconds=1800,
        ),
    )
    coordinator.settle_registration(prepared, committed=True)
    state.output.append_raw(("HEAD-" + "🙂" * 1_000 + "-TAIL ").encode())
    coordinator._evaluate(prepared.monitor_id, monotonic() + 2_000)  # noqa: SLF001
    target = NewTurnInstallation(
        'turn:aq53dqzrwqpuwmn4wsjba34uzq', "revision:observation:0", 'entry:n25d5ijed3stgnebhpwtxwytma'
    )
    attempt = coordinator.freeze(
        monitor_id=prepared.monitor_id,
        target=target,
        workspace_id='workspace:wdl6b3mtrkghwnvjo4an3ekbau',
        writer_generation=1,
        actor_id='host:yinyxklzunar54s7objjdbphcy',
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
        workspace_id='workspace:wdl6b3mtrkghwnvjo4an3ekbau',
        writer_generation=1,
        actor_id='host:yinyxklzunar54s7objjdbphcy',
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
    owner = 'host:2rq7lgzlvfcbo5qch7mkux3ukm'
    registry = ProcessRegistry()
    registry.activate_owner(owner)
    state, yielded = registry.exec_with_yield(
        command="race",
        cwd=tmp_path,
        yield_time_ms=0,
        tty=False,
        max_lifetime_seconds=None,
        owner_host_session_id=owner,
        shell_argv=(sys.executable, "-c", "import time; time.sleep(30)"),
        decision_deadline_monotonic=monotonic() + 5,
        env=dict(os.environ),
    )
    assert yielded is True
    coordinator = TerminalMonitorCoordinator(
        session_id='session:zzjwyhle2djc4mhh3onktt3ywe',
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
        origin_turn_id='turn:gxzq5vngv5b5jiz4coppto54yi',
        origin_attempt_id='attempt:kzdvb63t626wuo5prljni4enzu',
        origin_result_entry_id='entry:egjr7a5ft7ouzg3xc3zxfmr5xa',
        writer_generation=1,
        authorization_reference="policy:race",
        policy=TerminalMonitorPolicy(),
    )
    coordinator.settle_registration(prepared, committed=True)
    deadline = monotonic() + 1
    while prepared.monitor_id not in coordinator.pending_monitor_ids():
        assert monotonic() < deadline
        sleep(0.01)
    attempt = coordinator.freeze(
        monitor_id=prepared.monitor_id,
        target=NewTurnInstallation('turn:tgnq76so2tupmonbxpxhivfuya', "revision:new", 'entry:ljj6eunt5vctvos4pk3swztw5i'),
        workspace_id='workspace:faare2dinqcrxzuvzmkoamb2a4',
        writer_generation=1,
        actor_id='host:i5raubz73g443vpmydtsch6b2m',
    )
    assert attempt is not None
    assert attempt.content.observation_kind.value == "COMPLETION"
    coordinator.settle_installation(attempt, accepted=True)
    coordinator.stop_admission_and_close(timeout_seconds=2)
    registry.release_owner(owner, timeout_seconds=2)


def test_round2_monitor_cancel_after_freeze_preserves_exact_attempt_until_settlement(
    tmp_path: Path,
) -> None:
    owner = 'host:yhq4qzylnijd3tjplcxzszk3se'
    registry = ProcessRegistry()
    registry.activate_owner(owner)
    state, yielded = registry.exec_with_yield(
        command="cancel-race",
        cwd=tmp_path,
        yield_time_ms=0,
        tty=False,
        max_lifetime_seconds=None,
        owner_host_session_id=owner,
        shell_argv=(sys.executable, "-c", "import time; time.sleep(30)"),
        decision_deadline_monotonic=monotonic() + 5,
        env=dict(os.environ),
    )
    assert yielded is True
    coordinator = TerminalMonitorCoordinator(
        session_id='session:wdojzy7ejlduqcyw2tocwl5tri',
        owner_epoch=owner,
        registry=registry,
        live_bus=LiveAgentEventBus(),
        wake_scheduler=lambda: None,
    )
    prepared = coordinator.prepare_registration(
        process_id=state.process_id,
        origin_turn_id='turn:ttyfwr7awvyjfl5hpwjdj55dpa',
        origin_attempt_id='attempt:rz3wb4hricxqouo4rkiouiwlwu',
        origin_result_entry_id='entry:km64byuv5pqjmcrqyqrpnxt5iq',
        writer_generation=1,
        authorization_reference="policy:cancel-race",
        policy=TerminalMonitorPolicy(
            min_new_output_chars=1,
            quiet_period_ms=0,
            minimum_progress_interval_seconds=1800,
        ),
    )
    coordinator.settle_registration(prepared, committed=True)
    state.output.append_raw(b"freeze-wins ")
    coordinator._evaluate(prepared.monitor_id, monotonic() + 2_000)  # noqa: SLF001
    attempt = coordinator.freeze(
        monitor_id=prepared.monitor_id,
        target=NewTurnInstallation(
            'turn:c4cx4qeidvlnwg7lnfmi7rz6ge', "revision:cancel-winner", 'entry:iorpmxn257efsrpqg5ixbrw64a'
        ),
        workspace_id='workspace:q65um77jkwmxqhccpnn7bvy7oe',
        writer_generation=1,
        actor_id='host:ry64k4v3wug7nqvhws6mh4hflu',
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
    owner = 'host:yyf7ssb3dzpgtdttqszjbjt26i'
    registry = ProcessRegistry()
    registry.activate_owner(owner)
    heartbeat_process = _spawn_sleeping_process(registry, owner, tmp_path)
    expiry_process = _spawn_sleeping_process(registry, owner, tmp_path)
    coordinator = TerminalMonitorCoordinator(
        session_id='session:kkmydvqiftka4o5cg5djcb4mwe',
        owner_epoch=owner,
        registry=registry,
        live_bus=LiveAgentEventBus(),
        wake_scheduler=lambda: None,
    )

    heartbeat = coordinator.prepare_registration(
        process_id=heartbeat_process.process_id,
        origin_turn_id='turn:3oh4ib6u76lfoebttfo6pfuaby',
        origin_attempt_id='attempt:yqcm5p7b33ymazbqy5xd7fp65u',
        origin_result_entry_id='entry:tdnm3zimgdqdkmbvjfgcoadjoe',
        writer_generation=1,
        authorization_reference="policy:heartbeat",
        policy=TerminalMonitorPolicy(
            min_new_output_chars=None,
            heartbeat_interval_seconds=5,
        ),
    )
    coordinator.settle_registration(heartbeat, committed=True)
    heartbeat_process.output.append_raw(b"output-does-not-enable-progress ")
    coordinator._evaluate(heartbeat.monitor_id, monotonic() + 4)  # noqa: SLF001
    assert coordinator.pending_monitor_ids() == ()
    coordinator._evaluate(heartbeat.monitor_id, monotonic() + 6)  # noqa: SLF001
    heartbeat_attempt = coordinator.freeze(
        monitor_id=heartbeat.monitor_id,
        target=NewTurnInstallation(
            'turn:w374ncj3rfior26ccteoe5euty', "revision:heartbeat-wake", 'entry:b6x4z57xcpip2aamgu3wmy4kre'
        ),
        workspace_id='workspace:pjzzplieywc4j55n6nzst3pbe4',
        writer_generation=1,
        actor_id='host:yyf7ssb3dzpgtdttqszjbjt26i',
    )
    assert heartbeat_attempt is not None
    assert heartbeat_attempt.content.observation_kind.value == "HEARTBEAT"
    coordinator.settle_installation(heartbeat_attempt, accepted=True)

    expiry = coordinator.prepare_registration(
        process_id=expiry_process.process_id,
        origin_turn_id='turn:baysdzt3jvqcn4lufvfcpby26a',
        origin_attempt_id='attempt:33sugxz5xzstob5nzjpskexmxe',
        origin_result_entry_id='entry:rmkat6sjcymmy5u2gb6mzbit64',
        writer_generation=1,
        authorization_reference="policy:expiry",
        policy=TerminalMonitorPolicy(maximum_duration_seconds=1),
    )
    coordinator.settle_registration(expiry, committed=True)
    coordinator._evaluate(expiry.monitor_id, monotonic() + 2)  # noqa: SLF001
    expiry_attempt = coordinator.freeze(
        monitor_id=expiry.monitor_id,
        target=NewTurnInstallation(
            'turn:4dui4i4bb5x6lz6n2drmq3eu3q', "revision:expiry-wake", 'entry:jmod6h5lpearhs6ozenwgzvkt4'
        ),
        workspace_id='workspace:pjzzplieywc4j55n6nzst3pbe4',
        writer_generation=1,
        actor_id='host:yyf7ssb3dzpgtdttqszjbjt26i',
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
    owner = 'host:rsyfierba4zhgi2ck5syindorm'
    registry = ProcessRegistry()
    registry.activate_owner(owner)
    state = _spawn_sleeping_process(registry, owner, tmp_path)
    coordinator = TerminalMonitorCoordinator(
        session_id='session:ijyvukpiquoyner3bnx2vu27uy',
        owner_epoch=owner,
        registry=registry,
        live_bus=LiveAgentEventBus(),
        wake_scheduler=lambda: None,
    )
    prepared = coordinator.prepare_registration(
        process_id=state.process_id,
        origin_turn_id='turn:klcsjq3hftfxbhjldvoxc7cuca',
        origin_attempt_id='attempt:nzfq4svsvrd634knzpiziabmja',
        origin_result_entry_id='entry:bo6jk6vkknnuuguqspt3pv7ysi',
        writer_generation=1,
        authorization_reference="policy:coalesce",
        policy=TerminalMonitorPolicy(
            min_new_output_chars=1,
            quiet_period_ms=0,
            minimum_progress_interval_seconds=5,
        ),
    )
    coordinator.settle_registration(prepared, committed=True)
    state.output.append_raw(b"first ")
    coordinator._evaluate(prepared.monitor_id, monotonic() + 10)  # noqa: SLF001
    state.output.append_raw(b"second ")
    coordinator._evaluate(prepared.monitor_id, monotonic() + 20)  # noqa: SLF001
    coordinator.process_completed(state.process_id, status="success", exit_code=0)
    coordinator._evaluate(prepared.monitor_id, monotonic() + 21)  # noqa: SLF001
    completion = coordinator.freeze(
        monitor_id=prepared.monitor_id,
        target=NewTurnInstallation(
            'turn:io4iuecj6cxmnrg6b6vjfk4iv4', "revision:coalesce-wake", 'entry:h2o4xscctd2o53zdue7433h5ni'
        ),
        workspace_id='workspace:4suocxnymvtfebnb7rn74tu6pq',
        writer_generation=1,
        actor_id='host:rsyfierba4zhgi2ck5syindorm',
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
        session_id = 'session:mrypghgj5rp3itbezpgr3qqoru'
        live_bus = LiveAgentEventBus()
        port = DirectKernelToolPort(
            workspace_root=tmp_path,
            host_owner_id='host:zwkyqhfmjvf5idqiykvxlsqeeq',
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
            attempt_id='attempt:g4r6guogbojouzzzss6dh5ipxm',
            turn_id='turn:via4gs2quwbuwtie6oncitgyhe',
            assistant_entry_id='entry:z55igztunmp7kmov6htjk6vw2y',
        )
        process_id = json.loads(started.content)["process_id"]

        async def invoke_root(label: str, tool_name: str, arguments: dict[str, object]):
            return await invoke_direct_tool(
                port,
                session_id=session_id,
                workspace_id='workspace:n6yg6xfimeb4eiul4gaa43wgmu',
                tool_name=tool_name,
                arguments=arguments,
                tool_call_id=f"call:{label}",
                attempt_id=derive_id("attempt", "test-fixture", f"attempt:{label}"),
                turn_id=derive_id("turn", "test-fixture", f"turn:{label}"),
                assistant_entry_id=derive_id("entry", "test-fixture", f"entry:{label}"),
            )

        mismatch_borrow, mismatch_context = direct_tool_invocation_context(
            port,
            session_id=session_id,
            workspace_id='workspace:n6yg6xfimeb4eiul4gaa43wgmu',
            tool_name="terminal_monitor",
            tool_call_id="call:different",
            attempt_id='attempt:a4vormydvcekppxzkllhhenat4',
            turn_id='turn:pbibxkwjanwckkd7eyjqxpwfhq',
            assistant_entry_id='entry:xsgvyzr547ysitrg3t2aih4ake',
        )
        with pytest.raises(RuntimeError, match="exact-join"):
            try:
                await port.invoke(
                    tool_name="terminal_monitor",
                    arguments={"action": "register", "process_id": process_id},
                    tool_call_id="call:mismatch",
                    attempt_id='attempt:fk2b4bgdvgqqfszf3rakbgz6tm',
                    turn_id='turn:ygjjkr3ma6p3qedf4csngaglcu',
                    assistant_entry_id='entry:kzjvt467ssozzajgmswdrummim',
                    invocation_context=mismatch_context,
                )
            finally:
                mismatch_borrow.close()
        assert port.terminal_monitor_coordinator.list_current() == ()

        child_surface = prepare_test_direct_tool_surface(
            port,
            conversation_scope_kind=ModelInputScopeKind.SUBAGENT_TASK,
            scope_subagent_task_id='task:t2ptfli5koch6nfrihpkwl3kvi',
        )
        assert "terminal_monitor" not in {
            spec.name for spec in child_surface.model_surface.tool_specs
        }
        with pytest.raises(RuntimeError, match="not advertised"):
            await invoke_direct_tool(
                port,
                session_id=session_id,
                workspace_id='workspace:n6yg6xfimeb4eiul4gaa43wgmu',
                tool_name="terminal_monitor",
                arguments={"action": "register", "process_id": process_id},
                tool_call_id="call:SUBAGENT_TASK",
                attempt_id='attempt:scc6tomtfah2vnphmqnf4npzjy',
                turn_id='turn:evue3iiyois6caa2g3in2f7rnu',
                assistant_entry_id='entry:magflyy6cksxnaojtolyvzzpcu',
                conversation_scope_kind=ModelInputScopeKind.SUBAGENT_TASK,
                scope_subagent_task_id='task:t2ptfli5koch6nfrihpkwl3kvi',
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
            "prepared",
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
            {"action": "register", "process_id": 'process:vxwpzdmzto7uhw6jrpqmtojroy'},
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
        process = json.loads(processes.content)["processes"][0]
        assert process["status"] == "running"
        assert set(process) == {
            "command",
            "cwd",
            "duration_seconds",
            "exit_code",
            "output_cursor",
            "process_id",
            "retained_from_cursor",
            "status",
            "stdin_closed",
            "timed_out",
        }
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


@pytest.mark.parametrize("arguments, expected_detail", [
    ({"action": "malformed"}, "invalid tool arguments"),
    ({"action": "cancel", "monitor_id": "terminal-monitor:" + "0" * 32}, "INVALID_ID"),
    ({"action": "cancel", "monitor_id": "process:" + "a" * 26}, "INVALID_ID"),
])
def test_round2_malformed_monitor_input_uses_common_invalid_arguments_contract(
    tmp_path: Path, arguments, expected_detail,
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
                arguments=arguments,
                tool_call_id=_name("call"),
                turn_id=_name("turn"),
                assistant_entry_id=_name("entry"),
            )
            assert decision.kind is KernelToolAuthorizationKind.INVALID_ARGUMENTS
            assert expected_detail in decision.public_message
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
            host_owner_id='host:ivbbigu37u2kewe4z64qwvbhx4',
            session_id='session:p4dfhpwhrtwwusdq4xpm5xludi',
            live_bus=live_bus,
            authorization_policy=DefaultToolDispatchAuthorizationPolicy(),
        )
        port._terminal.environment_owner.config = TerminalEnvConfig(  # noqa: SLF001
            enable_shell_snapshot=False
        )
        started = await invoke_direct_tool(
            port,
            session_id='session:p4dfhpwhrtwwusdq4xpm5xludi',
            workspace_id='workspace:nwvy5eoruiyt7xwudvbidfgonu',
            tool_name="terminal",
            arguments={"command": "sleep 0.15", "yield_time_ms": 0},
            tool_call_id="call:child-origin",
            attempt_id='attempt:bvznwpxsjqpdc7yav4lgfvvbkq',
            turn_id='turn:yndpt43qq5exwddahqtsuhjxd4',
            assistant_entry_id='entry:cbgpjjjc35xgohf2jvhwqnrvoa',
            conversation_scope_kind=ModelInputScopeKind.SUBAGENT_TASK,
            scope_subagent_task_id='task:twul7hv3qpuxipu3qslwr755ci',
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

        assert completion.turn_id == 'turn:yndpt43qq5exwddahqtsuhjxd4'
        assert completion.scope_kind == "SUBAGENT_TASK"
        assert completion.scope_subagent_task_id == 'task:twul7hv3qpuxipu3qslwr755ci'
        await port.aclose(timeout_seconds=3)

    asyncio.run(scenario())


def test_round2_monitor_dormant_capture_requires_commit_and_discard_is_terminal(
    tmp_path: Path,
) -> None:
    owner = 'host:d6bqlydirt6gy7dbhqnnfyiymy'
    registry = ProcessRegistry()
    registry.activate_owner(owner)
    committed_process = _spawn_sleeping_process(registry, owner, tmp_path)
    discarded_process = _spawn_sleeping_process(registry, owner, tmp_path)
    wake_count = 0

    def wake() -> None:
        nonlocal wake_count
        wake_count += 1

    coordinator = TerminalMonitorCoordinator(
        session_id='session:ctgwah63ij24i3we2zwe7rikvi',
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
        origin_turn_id='turn:7ema7xndzglosw36knwdly6ibq',
        origin_attempt_id='attempt:fprwlchdox7w2s7gpmq2pwi7g4',
        origin_result_entry_id='entry:6kcvbcimcpstleycvftyjojw5e',
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

    coordinator.settle_registration(committed, committed=True)
    coordinator._evaluate(committed.monitor_id, monotonic() + 11)  # noqa: SLF001
    assert coordinator.pending_monitor_ids() == (committed.monitor_id,)
    assert wake_count == 1

    discarded = coordinator.prepare_registration(
        process_id=discarded_process.process_id,
        origin_turn_id='turn:epvrwrjidzvbqtrcpyjyv5kmqa',
        origin_attempt_id='attempt:mqspbxmc6idx45bx42dytvhsoi',
        origin_result_entry_id='entry:4iw44wl6ys5zfrcqrhegx6c72q',
        writer_generation=1,
        authorization_reference="policy:dormant-discard",
        policy=policy,
    )
    discarded_process.output.append_raw(b"must-never-deliver ")
    coordinator.settle_registration(discarded, committed=False)
    coordinator._evaluate(discarded.monitor_id, monotonic() + 20)  # noqa: SLF001
    assert coordinator.cancel(discarded.monitor_id) == "already_terminal"
    assert coordinator.pending_monitor_ids() == (committed.monitor_id,)

    coordinator.cancel(committed.monitor_id)
    coordinator.stop_admission_and_close(timeout_seconds=2)
    registry.release_owner(owner, timeout_seconds=3)


def test_round2_monitor_successor_is_exactly_after_inflight_and_rejection_retries_first(
    tmp_path: Path,
) -> None:
    owner = 'host:cb6ht25cdbugx4bfda52nmnc5e'
    registry = ProcessRegistry()
    registry.activate_owner(owner)
    state = _spawn_sleeping_process(registry, owner, tmp_path)
    coordinator = TerminalMonitorCoordinator(
        session_id='session:q6hr3drvgcvgbrg6beh653ji4i',
        owner_epoch=owner,
        registry=registry,
        live_bus=LiveAgentEventBus(),
        wake_scheduler=lambda: None,
    )
    prepared = coordinator.prepare_registration(
        process_id=state.process_id,
        origin_turn_id='turn:ip4gd6wolprut4h3uvxxn7kmhu',
        origin_attempt_id='attempt:ddiikzp7clpi4fyk64qppcpnou',
        origin_result_entry_id='entry:xv6rdvfkakwi6kuhvgmwnlhewy',
        writer_generation=1,
        authorization_reference="policy:successor",
        policy=TerminalMonitorPolicy(
            min_new_output_chars=1,
            quiet_period_ms=0,
            minimum_progress_interval_seconds=5,
        ),
    )
    coordinator.settle_registration(prepared, committed=True)
    target = NewTurnInstallation(
        'turn:rjqkuhuyeu2wmtozb24wkcx3gm', "revision:successor-wake", 'entry:x4oaahruysarkhbq5xhdm6wwxa'
    )
    state.output.append_raw(b"first-range ")
    coordinator._evaluate(prepared.monitor_id, monotonic() + 10)  # noqa: SLF001
    first = coordinator.freeze(
        monitor_id=prepared.monitor_id,
        target=target,
        workspace_id='workspace:yy5txz6mcdyrpc6oakif3eqhfq',
        writer_generation=1,
        actor_id='host:gqf32vb3ovziao6wjcguav5rhe',
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
        workspace_id='workspace:yy5txz6mcdyrpc6oakif3eqhfq',
        writer_generation=1,
        actor_id='host:gqf32vb3ovziao6wjcguav5rhe',
    )
    assert retried is not None
    assert retried.content == first.content
    assert retried.content_digest == first.content_digest
    assert retried.retained_from_cursor == first.retained_from_cursor
    assert retried.through_cursor == first.through_cursor
    assert retried.target == first.target
    coordinator.settle_installation(retried, accepted=True)

    second = coordinator.freeze(
        monitor_id=prepared.monitor_id,
        target=target,
        workspace_id='workspace:yy5txz6mcdyrpc6oakif3eqhfq',
        writer_generation=1,
        actor_id='host:gqf32vb3ovziao6wjcguav5rhe',
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
    owner = 'host:4p73ncvy7wp3g5zfpr762xhwvm'
    registry = ProcessRegistry()
    registry.activate_owner(owner)
    state = _spawn_sleeping_process(registry, owner, tmp_path)
    coordinator = TerminalMonitorCoordinator(
        session_id='session:4xyc6yp7z74tq4fnxtejc4r34m',
        owner_epoch=owner,
        registry=registry,
        live_bus=LiveAgentEventBus(),
        wake_scheduler=lambda: None,
    )
    prepared = coordinator.prepare_registration(
        process_id=state.process_id,
        origin_turn_id='turn:yk4w7yxiz3hlzzdiyqsp4z6v3e',
        origin_attempt_id='attempt:c723flspisov2t5lx7id6rho2y',
        origin_result_entry_id='entry:4t35k5hamvwn4p4zvwa4icbhz4',
        writer_generation=1,
        authorization_reference="policy:successor-race",
        policy=TerminalMonitorPolicy(
            min_new_output_chars=1,
            quiet_period_ms=0,
            minimum_progress_interval_seconds=1800,
        ),
    )
    coordinator.settle_registration(prepared, committed=True)
    target = NewTurnInstallation(
        'turn:2tzdh5ejoqyvmtoe3mvohg2nbq',
        "revision:successor-race-wake",
        'entry:j3bjjy27qb3tqh5fkftxdkucgu',
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
        workspace_id='workspace:vwkqufrluqzfdxqjobht5yozum',
        writer_generation=1,
        actor_id='host:4p73ncvy7wp3g5zfpr762xhwvm',
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
        workspace_id='workspace:vwkqufrluqzfdxqjobht5yozum',
        writer_generation=1,
        actor_id='host:4p73ncvy7wp3g5zfpr762xhwvm',
    )
    assert second is not None and second.content.output == "second-range "
    coordinator.settle_installation(second, accepted=True)
    coordinator.cancel(prepared.monitor_id)
    coordinator.stop_admission_and_close(timeout_seconds=2)
    registry.release_owner(owner, timeout_seconds=3)


def test_round2_monitor_capacity_and_autonomy_are_finite_and_idle_observer_is_nonblocking(
    tmp_path: Path,
) -> None:
    owner = 'host:nyj47kkcs43wnpg36mh7g7culu'
    registry = ProcessRegistry(max_live_processes=MAXIMUM_ACTIVE_MONITORS + 1)
    registry.activate_owner(owner)
    processes = [
        _spawn_sleeping_process(registry, owner, tmp_path)
        for _ in range(MAXIMUM_ACTIVE_MONITORS + 1)
    ]
    live_bus = LiveAgentEventBus()
    live_bus.subscribe()
    coordinator = TerminalMonitorCoordinator(
        session_id='session:7z3gmps6ggs7fhot2tnauzbaxu',
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
            origin_turn_id=derive_id("turn", "test-fixture", f"turn:bound:{index}"),
            origin_attempt_id=derive_id("attempt", "test-fixture", f"attempt:bound:{index}"),
            origin_result_entry_id=derive_id("entry", "test-fixture", f"entry:bound:{index}"),
            writer_generation=1,
            authorization_reference=f"policy:bound:{index}",
            policy=policy,
        )
        coordinator.settle_registration(prepared, committed=True)
        registrations.append(prepared)
    with pytest.raises(TerminalMonitorRejected) as rejection:
        coordinator.prepare_registration(
            process_id=processes[-1].process_id,
            origin_turn_id='turn:zrshqszzjr6rp3oyitw2ji65ta',
            origin_attempt_id='attempt:yovppsmvbuifwy7mbdmgq5crzy',
            origin_result_entry_id='entry:yi3s67dplsbzka6rmsztqztska',
            writer_generation=1,
            authorization_reference="policy:over-capacity",
            policy=policy,
        )
    assert rejection.value.reason is TerminalMonitorRejectionReason.CAPACITY_EXHAUSTED

    chosen = registrations[0]
    chosen_process = processes[0]
    target = NewTurnInstallation('turn:uw3p7nnjmja4zacdnbh3dsui4e', "revision:autonomy", 'entry:b3az3zpbdeibmxo6j3jdw532iu')
    started = monotonic()
    for index in range(MAXIMUM_AUTONOMOUS_CONTINUATIONS):
        chosen_process.output.append_raw(f"progress-{index} ".encode())
        coordinator._evaluate(chosen.monitor_id, started + 10 * (index + 1))  # noqa: SLF001
        attempt = coordinator.freeze(
            monitor_id=chosen.monitor_id,
            target=target,
            workspace_id='workspace:wtvuka2vhd5qwvkv2zu2rlr6ma',
            writer_generation=1,
            actor_id='host:7o3cxf3c7bxdczzkmcrbmhmbcm',
        )
        assert attempt is not None
        coordinator.settle_installation(attempt, accepted=True)
    assert coordinator.cancel(chosen.monitor_id) == "already_terminal"
    assert monotonic() - started < 1

    coordinator.stop_admission_and_close(timeout_seconds=2)
    registry.release_owner(owner, timeout_seconds=5)
