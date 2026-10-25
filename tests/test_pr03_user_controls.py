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
from pulsara_agent.conversation_kernel.repository import ConversationKernelConflict
from pulsara_agent.conversation_kernel.subagent import (
    KernelSubagentManager,
    SubagentCancelDisposition,
    SubagentCancelResult,
)
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
                    'turn:paqqn75nkrrptym5iw4zxx6u6e', ModelInputScopeKind.ROOT, None
                )
                raise

        root = asyncio.create_task(root_a())
        host._active_task = root
        host._active_turn_id = 'turn:74y74rvr2pht5lydgxvy57nxgq'
        host._active_cancellation_intent = ActiveTurnCancellationIntent(
            'turn:74y74rvr2pht5lydgxvy57nxgq', ModelInputScopeKind.ROOT, None
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


def test_pr03_control_identity_deduplicates_exact_values_and_rejects_conflicts() -> (
    None
):
    async def scenario() -> None:
        clock = _Clock()
        host = _host(clock)
        calls = 0

        class _Subagents:
            async def preflight_cancel_task(self, _task_id: str):
                return None

            async def cancel_task(self, task_id: str) -> SubagentCancelResult:
                nonlocal calls
                calls += 1
                return SubagentCancelResult(
                    SubagentCancelDisposition.CANCELLED,
                    task_id,
                    "CANCELLED",
                    "USER_CANCELLED",
                )

        host._subagents = _Subagents()
        command_id = _command(host, "cancel-one")
        first = await host.request_cancel_subagent(
            command_id=command_id,
            expected_session_id=host.session_id,
            expected_host_session_id=host.host_session_id,
            task_id='task:74xe3yvbpqgx6s4jejk25ed6c4',
         admission_deadline_ms=host.control_admission_deadline_ms())
        duplicate = await host.request_cancel_subagent(
            command_id=command_id,
            expected_session_id=host.session_id,
            expected_host_session_id=host.host_session_id,
            task_id='task:74xe3yvbpqgx6s4jejk25ed6c4',
         admission_deadline_ms=host.control_admission_deadline_ms())
        conflict = await host.request_cancel_subagent(
            command_id=command_id,
            expected_session_id=host.session_id,
            expected_host_session_id=host.host_session_id,
            task_id='task:b3pqjvjpgab3uwzucbs6fg2rmq',
         admission_deadline_ms=host.control_admission_deadline_ms())
        assert first is duplicate
        assert conflict.public_code == "CONTROL_COMMAND_CONFLICT"
        attempt = host._user_control_attempts[command_id]
        assert attempt.task is not None
        await attempt.task
        assert calls == 1

        clock.seconds += 3_599
        retained = await host.query_control_command(attempt.request)
        assert retained.status is ControlQueryStatus.FOUND
        clock.seconds += 2
        retired = await host.query_control_command(attempt.request)
        assert retired.status is ControlQueryStatus.RESULT_UNAVAILABLE
        expired = await host.request_cancel_subagent(
            command_id=command_id,
            expected_session_id=host.session_id,
            expected_host_session_id=host.host_session_id,
            task_id='task:74xe3yvbpqgx6s4jejk25ed6c4',
         admission_deadline_ms=attempt.request.admission_deadline_ms)
        assert expired.public_code == "CONTROL_REQUEST_EXPIRED"
        assert calls == 1

    asyncio.run(scenario())


def test_pr03_subagent_target_is_validated_before_control_acceptance() -> None:
    async def scenario() -> None:
        host = _host(_Clock())
        cancel_calls = 0

        class _Subagents:
            result = SubagentCancelResult(
                SubagentCancelDisposition.TARGET_UNAVAILABLE,
                'task:i5c7unweqbacv3f2tlydxnoeui',
                None,
                "TARGET_UNAVAILABLE",
            )

            async def preflight_cancel_task(self, _task_id: str):
                return self.result

            async def cancel_task(self, _task_id: str):
                nonlocal cancel_calls
                cancel_calls += 1
                raise AssertionError("an unavailable target must not be cancelled")

        subagents = _Subagents()
        host._subagents = subagents
        missing = await host.request_cancel_subagent(
            command_id=_command(host, "missing-target"),
            expected_session_id=host.session_id,
            expected_host_session_id=host.host_session_id,
            task_id='task:i5c7unweqbacv3f2tlydxnoeui',
         admission_deadline_ms=host.control_admission_deadline_ms())
        assert missing.status == "REJECTED"
        assert missing.public_code == "CONTROL_TARGET_UNAVAILABLE"
        assert host._user_control_attempts == {}

        subagents.result = SubagentCancelResult(
            SubagentCancelDisposition.ALREADY_TERMINAL,
            'task:y5fpcmjc3oxo42gmcrrczbqcdq',
            "COMPLETED",
            "SUCCEEDED",
        )
        command_id = _command(host, "terminal-target")
        terminal = await host.request_cancel_subagent(
            command_id=command_id,
            expected_session_id=host.session_id,
            expected_host_session_id=host.host_session_id,
            task_id='task:y5fpcmjc3oxo42gmcrrczbqcdq',
         admission_deadline_ms=host.control_admission_deadline_ms())
        assert terminal.status == "SUCCEEDED"
        assert terminal.public_code == "CONTROL_ALREADY_TERMINAL"
        assert terminal.user_control is not None
        assert terminal.user_control.accepted is False
        assert cancel_calls == 0
        request = host._user_control_attempts[command_id].request
        assert (
            await host.query_control_command(request)
        ).status is ControlQueryStatus.FOUND

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


def test_pr03_missing_target_and_wrong_owner_never_install_control() -> None:
    async def scenario() -> None:
        host = _host(_Clock())
        host._subagents = SimpleNamespace()
        missing = await host.request_cancel_subagent(
            command_id=_command(host, "missing"),
            expected_session_id=host.session_id,
            expected_host_session_id=host.host_session_id,
            task_id="",
         admission_deadline_ms=host.control_admission_deadline_ms())
        wrong_owner = await host.request_cancel_subagent(
            command_id=_command(host, "wrong-owner"),
            expected_session_id=host.session_id,
            expected_host_session_id='host:r77neyfksldterpdnoirnf7zxu',
            task_id='task:74xe3yvbpqgx6s4jejk25ed6c4',
         admission_deadline_ms=host.control_admission_deadline_ms())
        assert missing.public_code == "CONTROL_REQUEST_INVALID"
        assert wrong_owner.public_code == "OWNER_UNAVAILABLE"
        assert host._user_control_attempts == {}

    asyncio.run(scenario())


def test_pr03_closing_host_rejects_new_control_before_and_after_preflight() -> None:
    async def scenario() -> None:
        host = _host(_Clock())
        preflight_started = asyncio.Event()
        release_preflight = asyncio.Event()

        class _Subagents:
            async def preflight_cancel_task(self, _task_id: str):
                preflight_started.set()
                await release_preflight.wait()
                return None

            async def cancel_task(self, _task_id: str):
                raise AssertionError("a closing Host must not accept new control")

        host._subagents = _Subagents()
        host._closing = True
        rejected = await host.request_cancel_subagent(
            command_id=_command(host, "already-closing"),
            expected_session_id=host.session_id,
            expected_host_session_id=host.host_session_id,
            task_id='task:74xe3yvbpqgx6s4jejk25ed6c4',
         admission_deadline_ms=host.control_admission_deadline_ms())
        assert rejected.public_code == "OWNER_UNAVAILABLE"
        assert not preflight_started.is_set()

        host._closing = False
        operation = asyncio.create_task(
            host.request_cancel_subagent(
                command_id=_command(host, "closing-after-preflight"),
                expected_session_id=host.session_id,
                expected_host_session_id=host.host_session_id,
                task_id='task:r7vf2gydnecjw6x22sqtkceg6u',
             admission_deadline_ms=host.control_admission_deadline_ms())
        )
        await preflight_started.wait()
        async with host._lock:
            host._closing = True
        release_preflight.set()
        rejected_after_preflight = await operation
        assert rejected_after_preflight.public_code == "OWNER_UNAVAILABLE"
        assert host._user_control_attempts == {}

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


def test_pr03_accepted_control_owner_failure_settles_instead_of_staying_pending() -> (
    None
):
    async def scenario() -> None:
        host = _host(_Clock())

        class _Subagents:
            async def preflight_cancel_task(self, _task_id: str):
                return None

            async def cancel_task(self, _task_id: str):
                raise RuntimeError("cancel owner failed")

        host._subagents = _Subagents()
        command_id = _command(host, "cancel-owner-failure")
        accepted = await host.request_cancel_subagent(
            command_id=command_id,
            expected_session_id=host.session_id,
            expected_host_session_id=host.host_session_id,
            task_id='task:74xe3yvbpqgx6s4jejk25ed6c4',
         admission_deadline_ms=host.control_admission_deadline_ms())
        assert accepted.status == "PENDING"
        attempt = host._user_control_attempts[command_id]
        assert attempt.task is not None
        await attempt.task
        assert attempt.outcome.status == "FAILED"
        assert attempt.outcome.public_code == "CONTROL_FAILED"
        assert attempt.outcome.user_control is not None
        assert attempt.outcome.user_control.execution == "FINISHED"

    asyncio.run(scenario())


def test_pr03_subagent_cancel_reports_a_naturally_completed_race_as_terminal() -> None:
    async def scenario() -> None:
        manager = object.__new__(KernelSubagentManager)
        completed = asyncio.create_task(asyncio.sleep(0))
        await completed
        manager._lock = asyncio.Lock()
        manager._tasks = {
            'task:y5fpcmjc3oxo42gmcrrczbqcdq': SimpleNamespace(
                task=completed,
                status="COMPLETED",
                cancellation_reason=None,
            )
        }
        manager._launch_permits = {}
        manager._guard = SimpleNamespace(session_id='session:5dyf7lfzp7z22pphjtostkgsjm')
        manager._canonical_deadline = lambda: 10_000.0
        manager._io = _IO()

        class _SubagentRepository:
            @staticmethod
            def query_subagent_task(*, session_id: str, task_id: str):
                assert (session_id, task_id) == ('session:5dyf7lfzp7z22pphjtostkgsjm', 'task:y5fpcmjc3oxo42gmcrrczbqcdq')
                return {
                    "status": "COMPLETED",
                    "terminal_reason": "SUCCEEDED",
                }

        manager._repository = _SubagentRepository()
        result = await manager.cancel_task('task:y5fpcmjc3oxo42gmcrrczbqcdq')
        assert result.disposition is SubagentCancelDisposition.ALREADY_TERMINAL
        assert result.status == "COMPLETED"
        assert result.reason == "SUCCEEDED"

    asyncio.run(scenario())


def test_pr03_durable_subagent_cancel_loser_reports_the_actual_terminal_winner() -> (
    None
):
    async def scenario() -> None:
        manager = object.__new__(KernelSubagentManager)
        manager._lock = asyncio.Lock()
        manager._tasks = {}
        manager._launch_permits = {}
        manager._guard = SimpleNamespace(session_id='session:5dyf7lfzp7z22pphjtostkgsjm')
        manager._canonical_deadline = lambda: 10_000.0
        manager._io = _IO()
        state = {
            "status": "PENDING_START",
            "terminal_reason": None,
            "workspace_id": 'workspace:scvzq3m6ilgcdfi5qvvqwjwvla',
        }

        class _SubagentRepository:
            @staticmethod
            def query_subagent_task(*, session_id: str, task_id: str):
                assert (session_id, task_id) == ('session:5dyf7lfzp7z22pphjtostkgsjm', 'task:y5fpcmjc3oxo42gmcrrczbqcdq')
                return dict(state)

        async def lose_cancellation_to_natural_completion(*_args, **_kwargs):
            state["status"] = "COMPLETED"
            state["terminal_reason"] = "SUCCEEDED"
            raise ConversationKernelConflict(
                "subagent task already has another terminal winner"
            )

        async def unexpected_dependency_settlement(_task_id: str) -> None:
            raise AssertionError("natural completion owns dependency settlement")

        manager._repository = _SubagentRepository()
        manager._settle_task_terminal_exact = lose_cancellation_to_natural_completion
        manager._settle_dependency_frontier = unexpected_dependency_settlement

        result = await manager.cancel_task('task:y5fpcmjc3oxo42gmcrrczbqcdq')
        assert result.disposition is SubagentCancelDisposition.ALREADY_TERMINAL
        assert result.status == "COMPLETED"
        assert result.reason == "SUCCEEDED"

    asyncio.run(scenario())
