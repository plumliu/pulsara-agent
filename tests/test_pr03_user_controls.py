from __future__ import annotations
from pulsara_agent.identity import derive_id

import asyncio
from types import SimpleNamespace
from time import monotonic

from pulsara_agent.conversation_kernel.cancellation import (
    ActiveTurnCancellationIntent,
)
from pulsara_agent.conversation_kernel.compaction.runtime import (
    HostCompactionRuntimeOwner,
)
from pulsara_agent.conversation_kernel.host import KernelHostSession
from pulsara_agent.conversation_kernel.user_control import (
    ControlQueryStatus,
    UserControlOperation,
    UserControlRequest,
    UserControlTarget,
    UserControlTargetKind,
)
from pulsara_agent.model_input.contracts import ModelInputScopeKind
from pulsara_agent.process_credential_boundary import ProcessCredentialBoundary
from tests.support.model_config import test_model_runtime


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
    host._model_runtime = test_model_runtime()
    host._credential_boundary = ProcessCredentialBoundary()
    host.session_id = 'session:5dyf7lfzp7z22pphjtostkgsjm'
    host.host_session_id = 'host:gubylb5v44ganghpuxoqfvlrji'
    host._control_monotonic = clock
    host.root_session_id = None
    host._session_scope_kind = ModelInputScopeKind.ROOT
    host._code_mode = SimpleNamespace(mark_turn_stopped=lambda _turn: None,
                                     stop_turn=lambda _turn: asyncio.sleep(0), index=lambda: ())
    host._user_control_attempts = {}
    host._pending_workspace_recreated = None
    host._pending_process_controls = {}
    host.workspace = SimpleNamespace(workspace_root="/tmp/workspace")
    host._lock = asyncio.Lock()
    host._active_task = None
    host._active_turn_id = None
    host._compaction = HostCompactionRuntimeOwner()
    host._compaction_write_reservations = {}
    host._active_cancellation_intent = None
    host._pending_root_successor = None
    host._closing = False
    host._closed = False
    host._io = _IO()
    host.repository = _Repository()
    host._canonical_deadline = lambda: monotonic() + 10
    return host


def _command(host: KernelHostSession, suffix: str) -> str:
    return derive_id("command", "test-fixture", str(host.control_admission_deadline_ms()), suffix)


def test_starting_turn_stop_confirmation_failure_finishes_without_claiming_success():
    async def scenario():
        host = _host(_Clock())
        target = 'turn:74y74rvr2pht5lydgxvy57nxgq'
        host._external_new_turn_accepting = True
        host._external_new_turn_cancellation_intent = ActiveTurnCancellationIntent(
            target, ModelInputScopeKind.ROOT,
        )
        host._external_new_turn_settled = asyncio.Event()
        interrupted = []
        reads = []

        async def interrupt(*, turn_id, reason):
            interrupted.append((turn_id, reason))
            host.repository.turns[turn_id] = {
                'status': 'INTERRUPTED', 'terminal_reason': reason,
            }

        def uncertain_terminal(*, session_id, turn_id):
            reads.append((session_id, turn_id))
            raise OSError('terminal commit succeeded; confirmation is unavailable')

        host._interrupt_queued_admission_until_safe = interrupt
        host.repository.read_turn_terminal_outcome = uncertain_terminal
        accepted = await host.request_stop_turn(
            command_id=_command(host, 'stop-starting'), target_turn_id=target,
            admission_deadline_ms=host.control_admission_deadline_ms(),
            expected_session_id=host.session_id,
            expected_host_session_id=host.host_session_id,
        )
        assert accepted.status == 'PENDING'
        attempt = host._user_control_attempts[accepted.command_id]
        assert not attempt.task.done()
        host._external_new_turn_settled.set()
        await asyncio.wait_for(attempt.task, 1)
        outcome = (await host.query_control_command(attempt.request)).outcome
        assert interrupted == [(target, 'USER_STOPPED')]
        assert reads == [(host.session_id, target)]
        assert host.repository.turns[target]['status'] == 'INTERRUPTED'
        assert outcome.status == 'FAILED' and outcome.public_code == 'CONTROL_FAILED'
        assert outcome.user_control.execution.value == 'FINISHED'
        assert outcome.user_control.root is None

    asyncio.run(scenario())


def test_pr03_exact_stop_never_cancels_a_successor_that_takes_the_root_slot() -> None:
    async def scenario() -> None:
        clock = _Clock()
        host = _host(clock)
        successor_release = asyncio.Event()
        successor = asyncio.create_task(successor_release.wait())
        root_started = asyncio.Event()

        async def root_a() -> None:
            try:
                root_started.set()
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
        await root_started.wait()

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


def test_pr03_background_control_terminates_the_exact_process() -> None:
    async def scenario() -> None:
        clock = _Clock()
        host = _host(clock)
        order: list[str] = []


        class _Tools:

            def list_background_terminal_processes(self):
                return [SimpleNamespace(process_id='process:34npgaljkboupr4n456vrmou6a', origin=SimpleNamespace(conversation_scope_kind='ROOT', session_id=host.session_id))]

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
        attempt = host._user_control_attempts[command_id]
        assert attempt.task is not None
        await attempt.task
        assert order == ['process:process:34npgaljkboupr4n456vrmou6a']
        assert attempt.outcome.status == "SUCCEEDED"
        assert attempt.outcome.user_control is not None
        assert "process_control" in await host.take_user_actions()

    asyncio.run(scenario())




def test_pr03_already_terminal_background_control_is_a_queryable_no_op() -> None:
    async def scenario() -> None:
        host = _host(_Clock())


        class _Tools:

            def list_background_terminal_processes(self):
                return [SimpleNamespace(process_id='process:qmi3ybigp4h4iv3tmaqmlm64am', origin=SimpleNamespace(conversation_scope_kind='ROOT', session_id=host.session_id))]

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
        assert await host.take_user_actions() is None
        assert (
            await host.query_control_command(attempt.request)
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
