from __future__ import annotations
from pulsara_agent.identity import derive_id

import asyncio
from dataclasses import replace
from types import SimpleNamespace

from pulsara_agent.conversation_kernel.cancellation import (
    ActiveTurnCancellationIntent,
)
from pulsara_agent.conversation_kernel.compaction.runtime import (
    HostCompactionRuntimeOwner,
)
from pulsara_agent.conversation_kernel.host import KernelHostSession
from pulsara_agent.conversation_kernel.user_control import (
    ControlQueryStatus,
    FeedbackCanonicalStatus,
    FeedbackInclusionStatus,
    FeedbackOwnerAvailability,
    UserControlFeedbackState,
    UserControlOperation,
    UserControlRequest,
    UserControlTarget,
    UserControlTargetKind,
)
from pulsara_agent.model_input.contracts import ModelInputScopeKind
from pulsara_agent.terminal_process.monitor import (
    TerminalMonitorCancelOutcome,
    TerminalMonitorCancelResult,
)


class _Clock:
    def __init__(self, seconds: float = 100.0) -> None:
        self.seconds = seconds

    def __call__(self) -> float:
        return self.seconds


class _IO:
    async def run(self, operation, /, *args, deadline_monotonic, **kwargs):
        del deadline_monotonic
        return operation(*args, **kwargs)


class _Repository:
    def __init__(self) -> None:
        self.turns: dict[str, dict[str, object]] = {}

    def read_turn_terminal_outcome(self, *, session_id, turn_id):
        assert session_id == 'session:5dyf7lfzp7z22pphjtostkgsjm'
        return self.turns.get(turn_id)


def _host(clock: _Clock) -> KernelHostSession:
    host = object.__new__(KernelHostSession)
    host._writer_renewal_failure = None
    host.session_id = 'session:5dyf7lfzp7z22pphjtostkgsjm'
    host.host_session_id = 'host:gubylb5v44ganghpuxoqfvlrji'
    host._control_monotonic = clock
    host.root_session_id = None
    host._session_scope_kind = ModelInputScopeKind.ROOT
    host._code_mode = SimpleNamespace(stop_turn=lambda _turn: asyncio.sleep(0), index=lambda: ())
    host._user_control_attempts = {}
    host._lock = asyncio.Lock()
    host._active_task = None
    host._active_turn_id = None
    host._control_completion_sealed_turn_id = None
    host._control_completion_write_reservation = None
    host._control_completion_seal_changed = asyncio.Event()
    host._control_completion_seal_changed.set()
    host._compaction = HostCompactionRuntimeOwner()
    host._compaction_write_reservations = {}
    host._root_compaction_write_changed = asyncio.Event()
    host._root_compaction_write_changed.set()
    host._active_cancellation_intent = None
    host._pending_root_successor = None
    host._closing = False
    host._closed = False
    host._io = _IO()
    host.repository = _Repository()
    host._canonical_deadline = lambda: 10_000.0
    return host


def _command(host: KernelHostSession, suffix: str) -> str:
    return derive_id("command", "test-fixture", str(host.control_admission_deadline_ms()), suffix)


def test_pr03_exact_stop_never_cancels_a_successor_that_takes_the_root_slot() -> None:
    async def scenario() -> None:
        clock = _Clock()
        host = _host(clock)
        successor_release = asyncio.Event()
        successor = asyncio.create_task(successor_release.wait())

        async def root_a() -> None:
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                host.repository.turns['turn:74y74rvr2pht5lydgxvy57nxgq'] = {
                    "status": "INTERRUPTED",
                    "terminal_reason": "USER_STOPPED",
                }
                host._active_task = successor
                host._active_turn_id = 'turn:paqqn75nkrrptym5iw4zxx6u6e'
                host._active_cancellation_intent = ActiveTurnCancellationIntent(
                    'turn:paqqn75nkrrptym5iw4zxx6u6e', ModelInputScopeKind.ROOT
                )
                raise

        root = asyncio.create_task(root_a())
        host._active_task = root
        host._active_turn_id = 'turn:74y74rvr2pht5lydgxvy57nxgq'
        host._active_cancellation_intent = ActiveTurnCancellationIntent(
            'turn:74y74rvr2pht5lydgxvy57nxgq', ModelInputScopeKind.ROOT
        )

        async def settle_exact(_task) -> None:
            return None

        host._settle_active_root_task = settle_exact
        command_id = _command(host, "stop-a")
        accepted = await host.request_stop_turn(
            command_id=command_id,
            expected_session_id=host.session_id,
            expected_host_session_id=host.host_session_id,
            target_turn_id='turn:74y74rvr2pht5lydgxvy57nxgq',
         admission_deadline_ms=host.control_admission_deadline_ms())
        assert accepted.status == "PENDING"
        attempt = host._user_control_attempts[command_id]
        assert attempt.task is not None
        await attempt.task
        query = await host.query_control_command(attempt.request)
        assert query.status is ControlQueryStatus.FOUND
        assert query.outcome is not None
        assert query.outcome.public_code == "ROOT_STOPPED"
        assert host._active_turn_id == 'turn:paqqn75nkrrptym5iw4zxx6u6e'
        assert not successor.done()
        successor_release.set()
        await successor

    asyncio.run(scenario())






def test_pr03_background_control_closes_monitor_before_physical_termination() -> None:
    async def scenario() -> None:
        clock = _Clock()
        host = _host(clock)
        order: list[str] = []

        class _Monitor:
            def cancel_for_process(self, process_id: str):
                order.append(f"monitor:{process_id}")
                return TerminalMonitorCancelResult(
                    'monitor:4jgfcphmbriq5w4bko5mhb2qri',
                    TerminalMonitorCancelOutcome.CANCELLED,
                    ('entry:sl7dldallpsjyohxno62xsfnoy',),
                )

        class _Tools:
            terminal_monitor_coordinator = _Monitor()

            def list_background_terminal_processes(self):
                return [SimpleNamespace(process_id='process:34npgaljkboupr4n456vrmou6a')]

            def terminate_background_terminal_process(self, process_id: str):
                order.append(f"process:{process_id}")
                return SimpleNamespace(
                    disposition=SimpleNamespace(value="TERMINATION_COMPLETED"),
                    result=SimpleNamespace(
                        status=SimpleNamespace(value="killed"), exit_code=-15
                    ),
                    physical_state="TERMINAL",
                    group_alive=False,
                )

        host._tools = _Tools()
        command_id = _command(host, "terminate-one")
        accepted = await host.request_terminate_background_process(
            command_id=command_id,
            expected_session_id=host.session_id,
            expected_host_session_id=host.host_session_id,
            process_id='process:34npgaljkboupr4n456vrmou6a',
         admission_deadline_ms=host.control_admission_deadline_ms())
        assert accepted.status == "PENDING"
        assert accepted.user_control is not None
        assert accepted.user_control.feedback is not None
        assert accepted.user_control.feedback.canonical_status == "NOT_REQUIRED"
        attempt = host._user_control_attempts[command_id]
        assert attempt.task is not None
        await attempt.task
        assert order == ['monitor:process:34npgaljkboupr4n456vrmou6a', 'process:process:34npgaljkboupr4n456vrmou6a']
        assert attempt.outcome.status == "SUCCEEDED"
        assert attempt.outcome.user_control is not None
        assert attempt.outcome.user_control.monitor is not None
        assert attempt.outcome.user_control.monitor.in_flight_observation_ids == (
            'entry:sl7dldallpsjyohxno62xsfnoy',
        )

    asyncio.run(scenario())


def test_pr03_root_completion_seal_prevents_late_feedback_binding() -> None:
    async def scenario() -> None:
        host = _host(_Clock())
        active_release = asyncio.Event()
        active = asyncio.create_task(active_release.wait())
        host._active_task = active
        host._active_turn_id = 'turn:74y74rvr2pht5lydgxvy57nxgq'

        class _Monitor:
            def cancel_for_process(self, _process_id: str):
                return None

        class _Tools:
            terminal_monitor_coordinator = _Monitor()

            def list_background_terminal_processes(self):
                return [SimpleNamespace(process_id='process:34npgaljkboupr4n456vrmou6a')]

            def terminate_background_terminal_process(self, _process_id: str):
                return SimpleNamespace(
                    disposition=SimpleNamespace(value="TERMINATION_COMPLETED"),
                    result=SimpleNamespace(
                        status=SimpleNamespace(value="killed"), exit_code=-15
                    ),
                    physical_state="PHYSICALLY_JOINED",
                    group_alive=False,
                )

        host._tools = _Tools()
        assert not await host._coordinate_user_control_feedback_at_root_completion(
            'turn:74y74rvr2pht5lydgxvy57nxgq'
        )
        assert host._control_completion_sealed_turn_id == 'turn:74y74rvr2pht5lydgxvy57nxgq'

        command_id = _command(host, "sealed-root")
        accepted = await host.request_terminate_background_process(
            command_id=command_id,
            expected_session_id=host.session_id,
            expected_host_session_id=host.host_session_id,
            process_id='process:34npgaljkboupr4n456vrmou6a',
         admission_deadline_ms=host.control_admission_deadline_ms())
        assert accepted.user_control is not None
        assert accepted.user_control.feedback is not None
        assert accepted.user_control.feedback.target_root_turn_id is None
        assert accepted.user_control.feedback.reason == "TARGET_CLOSED"
        attempt = host._user_control_attempts[command_id]
        assert attempt.task is not None
        await attempt.task
        assert attempt.outcome.user_control is not None
        assert attempt.outcome.user_control.feedback is not None
        assert attempt.outcome.user_control.feedback.target_root_turn_id is None
        assert attempt.outcome.user_control.feedback.reason == "TARGET_CLOSED"

        await host._settle_user_control_feedback_root_completion(
            'turn:74y74rvr2pht5lydgxvy57nxgq', turn_completed=False
        )
        assert host._control_completion_sealed_turn_id is None
        active_release.set()
        await active

    asyncio.run(scenario())


def test_pr03_already_terminal_background_control_is_a_queryable_no_op() -> None:
    async def scenario() -> None:
        host = _host(_Clock())

        class _Monitor:
            def cancel_for_process(self, _process_id: str):
                return None

        class _Tools:
            terminal_monitor_coordinator = _Monitor()

            def list_background_terminal_processes(self):
                return [SimpleNamespace(process_id='process:qmi3ybigp4h4iv3tmaqmlm64am')]

            def terminate_background_terminal_process(self, _process_id: str):
                return SimpleNamespace(
                    disposition=SimpleNamespace(value="ALREADY_TERMINAL"),
                    result=SimpleNamespace(
                        status=SimpleNamespace(value="success"), exit_code=0
                    ),
                    physical_state="TERMINAL",
                    group_alive=False,
                )

        host._tools = _Tools()
        command_id = _command(host, "already-terminal")
        first = await host.request_terminate_background_process(
            command_id=command_id,
            expected_session_id=host.session_id,
            expected_host_session_id=host.host_session_id,
            process_id='process:qmi3ybigp4h4iv3tmaqmlm64am',
         admission_deadline_ms=host.control_admission_deadline_ms())
        assert first.status == "PENDING"
        attempt = host._user_control_attempts[command_id]
        assert attempt.task is not None
        await attempt.task
        assert attempt.outcome.public_code == "CONTROL_ALREADY_TERMINAL"
        assert attempt.outcome.user_control is not None
        assert attempt.outcome.user_control.accepted is False
        assert attempt.outcome.user_control.feedback is not None
        assert attempt.outcome.user_control.feedback.reason == "NO_NEW_CONTROL_EFFECT"
        assert (
            await host.query_control_command(attempt.request)
        ).status is ControlQueryStatus.FOUND

    asyncio.run(scenario())


def test_pr03_attempt_retention_waits_for_feedback_owner_and_restarts_at_terminal() -> (
    None
):
    async def scenario() -> None:
        clock = _Clock()
        host = _host(clock)
        request = UserControlRequest(
            UserControlOperation.TERMINATE_BACKGROUND_PROCESS,
            _command(host, "feedback-retention"),
            host.session_id,
            host.host_session_id,
            UserControlTarget(UserControlTargetKind.BACKGROUND_PROCESS, 'process:34npgaljkboupr4n456vrmou6a'),
         admission_deadline_ms=host.control_admission_deadline_ms())
        feedback = UserControlFeedbackState(
            FeedbackCanonicalStatus.PENDING,
            FeedbackInclusionStatus.PENDING,
            FeedbackOwnerAvailability.AVAILABLE,
            target_root_turn_id='turn:74y74rvr2pht5lydgxvy57nxgq',
        )
        outcome = host._pending_control_outcome(request, feedback=feedback)
        attempt = host._install_control_attempt_locked(request, outcome)
        host._finish_control_attempt_locked(
            attempt, replace(outcome, status="SUCCEEDED")
        )
        clock.seconds += 7_200
        retained = await host.query_control_command(request)
        assert retained.status is ControlQueryStatus.FOUND
        async with host._lock:
            host._replace_control_feedback_locked(
                attempt,
                replace(
                    feedback,
                    canonical_status=FeedbackCanonicalStatus.FAILED,
                    inclusion_status=FeedbackInclusionStatus.NOT_APPLICABLE,
                    reason="TARGET_CLOSED",
                ),
            )
        assert attempt.retain_until_monotonic_ms is not None
        assert attempt.retain_until_monotonic_ms > int(clock.seconds * 1000)
        assert (
            await host.query_control_command(request)
        ).status is ControlQueryStatus.FOUND

    asyncio.run(scenario())






def test_pr03_query_requires_the_exact_control_value() -> None:
    async def scenario() -> None:
        host = _host(_Clock())
        request = UserControlRequest(
            UserControlOperation.STOP_ACTIVE_TURN,
            _command(host, "unknown"),
            host.session_id,
            host.host_session_id,
            UserControlTarget(UserControlTargetKind.ROOT_TURN, 'turn:74y74rvr2pht5lydgxvy57nxgq'),
         admission_deadline_ms=host.control_admission_deadline_ms())
        unavailable = await host.query_control_command(request)
        assert unavailable.status is ControlQueryStatus.RESULT_UNAVAILABLE
        old_owner = await host.query_control_command(
            UserControlRequest(
                request.operation,
                request.command_id,
                request.session_id,
                'host:bdrf3raz6vcwmrrowx6l566uc4',
                request.target,
             admission_deadline_ms=host.control_admission_deadline_ms())
        )
        assert old_owner.status is ControlQueryStatus.OWNER_UNAVAILABLE

    asyncio.run(scenario())
