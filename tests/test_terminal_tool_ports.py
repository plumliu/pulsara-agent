from __future__ import annotations

from dataclasses import dataclass
import inspect

import pytest

from pulsara_agent.event import EventContext
from pulsara_agent.ports.terminal import (
    TerminalCommandCompletedOutcome,
    TerminalCommandRejectedOutcome,
    TerminalMonitorRegisterInput,
    TerminalMonitorRejectedOutcome,
    TerminalPortRejectCode,
    TerminalProcessInventoryOutcome,
    TerminalProcessKilledOutcome,
    TerminalProcessListInput,
    TerminalProcessPollInput,
    TerminalStatus,
    build_terminal_command_request,
    build_terminal_port_invocation_owner,
)
from pulsara_agent.runtime.terminal.manager import TerminalSessionManager
from pulsara_agent.runtime.terminal.models import TerminalRequest
from pulsara_agent.runtime.terminal.session import TerminalSession
from pulsara_agent.runtime.terminal.notification import (
    TerminalNotificationCapacityError,
)
from pulsara_agent.runtime.terminal.tool_port import (
    RuntimeTerminalCommandPort,
    RuntimeTerminalMonitorPort,
    RuntimeTerminalProcessPort,
)
from tests.support.capability import tool_runtime_context
from tests.support.events import settled_test_event


CTX = EventContext(
    run_id="run:terminal-port",
    turn_id="turn:terminal-port",
    reply_id="reply:terminal-port",
)


def _owner(tool_name: str):
    runtime_context = tool_runtime_context(
        runtime_session_id="runtime:terminal-port",
        event_context=CTX,
    )
    return build_terminal_port_invocation_owner(
        runtime_session_id=runtime_context.runtime_session_id,
        tool_call_id=f"call:{tool_name}",
        tool_name=tool_name,  # type: ignore[arg-type]
        event_context=CTX,
        owner_kind=runtime_context.owner_kind,
        permission=runtime_context.permission,
    )


def test_command_and_process_ports_share_exact_process_owner(tmp_path) -> None:
    manager = TerminalSessionManager(tmp_path)
    stored = []

    def record_event(event):
        receipt = settled_test_event(event, sequence=len(stored) + 1)
        stored.append(receipt.committed_event)
        return receipt

    command_port = RuntimeTerminalCommandPort(
        workspace_root=tmp_path,
        terminal_sessions=manager,
        owner_host_session_id="host:terminal-port",
        owner_conversation_id="conversation:terminal-port",
        terminal_notification_account=None,
        record_event=record_event,
    )
    process_port = RuntimeTerminalProcessPort(
        workspace_root=tmp_path,
        terminal_sessions=manager,
        owner_host_session_id="host:terminal-port",
    )
    started = command_port.execute(
        request=build_terminal_command_request(
            command=(
                "python -c 'import time; print(\"ready\", flush=True); time.sleep(30)'"
            ),
            workdir=None,
            terminal_session_id="default",
            yield_time_ms=100,
            max_output_chars=4_000,
            tty=False,
        ),
        owner=_owner("terminal"),
        output_sink=None,
    )
    assert isinstance(started, TerminalCommandCompletedOutcome)
    assert started.result.status is TerminalStatus.RUNNING
    assert started.result.process_id is not None
    with pytest.raises(TypeError):
        started.result.metadata["late"] = True

    inventory = process_port.execute(
        request=TerminalProcessListInput(
            action="list",
            include_finished=True,
            include_running=True,
        ),
        owner=_owner("terminal_process"),
    )
    assert isinstance(inventory, TerminalProcessInventoryOutcome)
    assert tuple(item.process_id for item in inventory.processes) == (
        started.result.process_id,
    )

    observed = process_port.execute(
        request=TerminalProcessPollInput(
            action="poll",
            process_id=started.result.process_id,
            max_output_chars=4_000,
        ),
        owner=_owner("terminal_process"),
    )
    assert observed.outcome_kind == "observation"
    assert observed.observation_receipt is not None
    assert observed.observation_receipt.origin_tool_call_id == "call:terminal_process"

    from pulsara_agent.ports.terminal import TerminalProcessKillInput

    killed = process_port.execute(
        request=TerminalProcessKillInput(
            action="kill",
            process_id=started.result.process_id,
        ),
        owner=_owner("terminal_process"),
    )
    assert isinstance(killed, TerminalProcessKilledOutcome)
    assert killed.result.status is TerminalStatus.KILLED
    assert killed.completion_observation_receipt.completion_event_reference is not None


def test_terminal_live_owner_is_not_carried_in_request_metadata() -> None:
    assert "metadata" not in TerminalRequest.__dataclass_fields__
    assert "request.metadata" not in inspect.getsource(TerminalSession.execute)


def test_terminal_ports_reject_wrong_owner_and_missing_monitor_owner(tmp_path) -> None:
    manager = TerminalSessionManager(tmp_path)
    process_port = RuntimeTerminalProcessPort(
        workspace_root=tmp_path,
        terminal_sessions=manager,
        owner_host_session_id="host:terminal-port",
    )
    with pytest.raises(ValueError, match="owner tool mismatch"):
        process_port.execute(
            request=TerminalProcessListInput(
                action="list", include_finished=True, include_running=True
            ),
            owner=_owner("terminal"),
        )

    monitor_port = RuntimeTerminalMonitorPort(
        workspace_root=tmp_path,
        terminal_monitor_coordinator=None,
    )
    rejected = monitor_port.execute(
        request=TerminalMonitorRegisterInput(
            action="register", process_id="process:missing"
        ),
        owner=_owner("terminal_monitor"),
    )
    assert isinstance(rejected, TerminalMonitorRejectedOutcome)
    assert rejected.reject_code is TerminalPortRejectCode.MONITOR_OWNER_UNAVAILABLE


@dataclass
class _CapacityCoordinator:
    reason_code: str

    def prepare_registration(self, **_kwargs):
        raise TerminalNotificationCapacityError(
            "expected capacity rejection",
            reason_code=self.reason_code,
        )


@dataclass
class _CompletionCapacityAccount:
    def prepare_completion_reservation(self, **_kwargs):
        raise TerminalNotificationCapacityError(
            "expected completion capacity rejection",
            reason_code="terminal_notification_capacity_exhausted",
        )


def test_command_capacity_rejection_terminates_and_retires_spawned_process(
    tmp_path,
) -> None:
    manager = TerminalSessionManager(tmp_path)
    port = RuntimeTerminalCommandPort(
        workspace_root=tmp_path,
        terminal_sessions=manager,
        owner_host_session_id="host:terminal-port",
        owner_conversation_id="conversation:terminal-port",
        terminal_notification_account=_CompletionCapacityAccount(),  # type: ignore[arg-type]
    )

    outcome = port.execute(
        request=build_terminal_command_request(
            command="python -c 'import time; time.sleep(30)'",
            workdir=None,
            terminal_session_id="default",
            yield_time_ms=0,
            max_output_chars=4_000,
            tty=False,
        ),
        owner=_owner("terminal"),
    )

    assert isinstance(outcome, TerminalCommandRejectedOutcome)
    assert outcome.reject_code is TerminalPortRejectCode.PROCESS_CAPACITY_EXHAUSTED
    assert manager.list_processes(owner_host_session_id="host:terminal-port") == []


@pytest.mark.parametrize(
    ("reason_code", "expected"),
    (
        (
            "terminal_notification_capacity_exhausted",
            TerminalPortRejectCode.MONITOR_CAPACITY_EXHAUSTED,
        ),
        (
            "terminal_monitor_already_active_for_process",
            TerminalPortRejectCode.MONITOR_DUPLICATE,
        ),
    ),
)
def test_monitor_expected_rejections_keep_closed_reason(
    tmp_path, reason_code, expected
) -> None:
    port = RuntimeTerminalMonitorPort(
        workspace_root=tmp_path,
        terminal_monitor_coordinator=_CapacityCoordinator(reason_code),  # type: ignore[arg-type]
    )
    outcome = port.execute(
        request=TerminalMonitorRegisterInput(
            action="register", process_id="process:test"
        ),
        owner=_owner("terminal_monitor"),
    )
    assert isinstance(outcome, TerminalMonitorRejectedOutcome)
    assert outcome.reject_code is expected
