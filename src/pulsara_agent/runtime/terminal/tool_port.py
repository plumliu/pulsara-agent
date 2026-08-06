"""Owner-scoped terminal command, process, and monitor port implementations."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Callable, Literal

from pulsara_agent.capability.terminal_risk import is_hardline_terminal_command
from pulsara_agent.event import AgentEvent
from pulsara_agent.ports.event_write import RuntimeThreadEventSettlementReceipt
from pulsara_agent.ports.terminal import (
    DEFAULT_MAX_OUTPUT_CHARS,
    TerminalBackendType,
    TerminalCommandCompletedOutcome,
    TerminalCommandOutcome,
    TerminalCommandRejectedOutcome,
    TerminalCommandRequest,
    TerminalIOMode,
    TerminalMonitorCancelInput,
    TerminalMonitorCancelledOutcome,
    TerminalMonitorInventoryItem,
    TerminalMonitorInventoryOutcome,
    TerminalMonitorListInput,
    TerminalMonitorOutcome,
    TerminalMonitorRegisterInput,
    TerminalMonitorRegisteredOutcome,
    TerminalMonitorRejectedOutcome,
    TerminalPortInvocationOwner,
    TerminalPortRejectCode,
    TerminalProcessCloseStdinInput,
    TerminalProcessInfo,
    TerminalProcessInventoryOutcome,
    TerminalProcessKillInput,
    TerminalProcessKilledOutcome,
    TerminalProcessListInput,
    TerminalProcessLog,
    TerminalProcessLogInput,
    TerminalProcessLogOutcome,
    TerminalProcessObservationOutcome,
    TerminalProcessOutcome,
    TerminalProcessPollInput,
    TerminalProcessRejectedOutcome,
    TerminalProcessRequest,
    TerminalProcessSubmitInput,
    TerminalProcessWaitInput,
    TerminalProcessWriteInput,
    TerminalResult,
    TerminalStatus,
    resolve_terminal_monitor_public_policy,
)
from pulsara_agent.ports.tool_execution import ToolRuntimeContext
from pulsara_agent.primitives.context import (
    FrozenJsonObjectFact,
    context_fingerprint,
    freeze_json,
)
from pulsara_agent.primitives.frozen import build_frozen_fact
from pulsara_agent.primitives.terminal_observation import (
    TerminalProcessLifecycleOutcomeFact,
    TerminalProcessObservationReceiptFact,
)
from pulsara_agent.runtime.terminal.manager import TerminalSessionManager
from pulsara_agent.runtime.terminal.models import (
    TerminalExecutionOwner,
    TerminalRequest,
)
from pulsara_agent.runtime.terminal.monitor import TerminalMonitorCoordinator
from pulsara_agent.runtime.terminal.notification import (
    TerminalNotificationAccountCoordinator,
    TerminalNotificationCapacityError,
)
from pulsara_agent.runtime.terminal.process import ProcessInputError


@dataclass(slots=True)
class RuntimeTerminalCommandPort:
    workspace_root: Path
    terminal_sessions: TerminalSessionManager
    owner_host_session_id: str | None
    owner_conversation_id: str | None
    terminal_notification_account: TerminalNotificationAccountCoordinator | None
    record_event: Callable[[AgentEvent], RuntimeThreadEventSettlementReceipt] | None = (
        None
    )

    def execute(
        self,
        *,
        request: TerminalCommandRequest,
        owner: TerminalPortInvocationOwner,
        output_sink=None,
    ) -> TerminalCommandOutcome:
        _require_owner(owner, tool_name="terminal")
        if owner.permission.terminal_access == "off":
            return _command_rejected(
                request=request,
                stage="permission",
                code=TerminalPortRejectCode.ACCESS_OFF,
                message="terminal is disabled by permission policy",
            )
        try:
            terminal_session = self.terminal_sessions.get_or_create(
                request.terminal_session_id,
                owner_host_session_id=self.owner_host_session_id,
                owner_conversation_id=self.owner_conversation_id,
            )
        except ValueError as exc:
            return _command_rejected(
                request=request,
                stage="adapter_initialization",
                code=TerminalPortRejectCode.CONTRACT_MISMATCH,
                message=str(exc),
            )
        requires_completion_reservation = bool(
            self.terminal_notification_account is not None
            and self.owner_host_session_id is not None
            and owner.owner_kind.value == "host_main_run"
        )
        execution_owner = TerminalExecutionOwner(
            origin_event_context=owner.event_context,
            origin_tool_call_id=owner.tool_call_id,
            origin_runtime_session_id=owner.runtime_session_id,
            origin_run_entry_kind=owner.owner_kind.value,
            output_callback=(output_sink.emit if output_sink is not None else None),
            record_event=self.record_event,
            require_completion_notification_reservation=(
                requires_completion_reservation
            ),
        )
        result = terminal_session.execute(
            TerminalRequest(
                command=request.command,
                workdir=request.workdir,
                yield_time_ms=request.yield_time_ms,
                max_output_chars=request.max_output_chars,
                tty=request.tty,
                max_lifetime_seconds=request.max_lifetime_seconds,
            ),
            execution_owner=execution_owner,
        )
        prepared_reservation = None
        if (
            result.status.value == "running"
            and result.process_id is not None
            and self.terminal_notification_account is not None
            and self.owner_host_session_id is not None
            and owner.owner_kind.value == "host_main_run"
        ):
            try:
                process = self.terminal_sessions.monitorable_process(
                    result.process_id,
                    owner_host_session_id=self.owner_host_session_id,
                    origin_runtime_session_id=owner.runtime_session_id,
                )
                prepared_reservation = (
                    self.terminal_notification_account.prepare_completion_reservation(
                        process=process,
                        tool_result_end_event_id=(
                            f"tool_result_end:{owner.run_id}:{owner.tool_call_id}"
                        ),
                    )
                )
            except TerminalNotificationCapacityError as exc:
                self.terminal_sessions.abort_unreserved_yielded_process(
                    result.process_id,
                    owner_host_session_id=self.owner_host_session_id,
                )
                return _command_rejected(
                    request=request,
                    stage="completion_reservation",
                    code=TerminalPortRejectCode.PROCESS_CAPACITY_EXHAUSTED,
                    message=str(exc),
                )
            except Exception:
                self.terminal_sessions.abort_unreserved_yielded_process(
                    result.process_id,
                    owner_host_session_id=self.owner_host_session_id,
                )
                raise
        port_result = _terminal_result(result)
        payload = {
            "outcome_kind": "completed",
            "result": asdict(port_result),
            "terminal_session_id": terminal_session.session_id,
            "backend_type": terminal_session.state.backend_type.value,
            "prepared_completion_reservation": (
                asdict(prepared_reservation)
                if prepared_reservation is not None
                else None
            ),
        }
        return TerminalCommandCompletedOutcome(
            outcome_kind="completed",
            result=port_result,
            terminal_session_id=terminal_session.session_id,
            backend_type=TerminalBackendType(terminal_session.state.backend_type.value),
            prepared_completion_reservation=prepared_reservation,
            outcome_fingerprint=context_fingerprint(
                "terminal-command-completed-outcome:v1", payload
            ),
        )


@dataclass(slots=True)
class RuntimeTerminalProcessPort:
    workspace_root: Path
    terminal_sessions: TerminalSessionManager
    owner_host_session_id: str | None

    def execute(
        self,
        *,
        request: TerminalProcessRequest,
        owner: TerminalPortInvocationOwner,
    ) -> TerminalProcessOutcome:
        _require_owner(owner, tool_name="terminal_process")
        process_id = getattr(request, "process_id", None)
        if owner.permission.terminal_access == "off":
            return _process_rejected(
                request=request,
                process_id=process_id,
                status="blocked",
                code=TerminalPortRejectCode.ACCESS_OFF,
                message="terminal_process is disabled by permission policy",
            )
        if isinstance(request, TerminalProcessWriteInput | TerminalProcessSubmitInput):
            if is_hardline_terminal_command(request.data):
                return _process_rejected(
                    request=request,
                    process_id=request.process_id,
                    status="blocked",
                    code=TerminalPortRejectCode.HARDLINE_PROCESS_INPUT,
                    message=(
                        "terminal process input blocked by hardline permission policy"
                    ),
                )
        try:
            if isinstance(request, TerminalProcessListInput):
                processes = self.terminal_sessions.list_processes(
                    owner_host_session_id=self.owner_host_session_id,
                    include_finished=request.include_finished,
                    include_running=request.include_running,
                )
                items = tuple(_process_info(item) for item in processes)
                live = self.terminal_sessions.live_process_count(
                    owner_host_session_id=self.owner_host_session_id
                )
                finished = self.terminal_sessions.finished_process_count(
                    owner_host_session_id=self.owner_host_session_id
                )
                payload = {
                    "outcome_kind": "inventory",
                    "processes": tuple(asdict(item) for item in items),
                    "live_process_count": live,
                    "finished_process_count": finished,
                }
                return TerminalProcessInventoryOutcome(
                    outcome_kind="inventory",
                    processes=items,
                    live_process_count=live,
                    finished_process_count=finished,
                    outcome_fingerprint=context_fingerprint(
                        "terminal-process-inventory-outcome:v1", payload
                    ),
                )
            if isinstance(request, TerminalProcessLogInput):
                log = self.terminal_sessions.log_process(
                    request.process_id,
                    max_output_chars=request.max_output_chars,
                    owner_host_session_id=self.owner_host_session_id,
                )
                port_log = _process_log(log)
                receipt = _observation_receipt(
                    tool_call_id=owner.tool_call_id,
                    action="log",
                    observation_semantic=log.observation_semantic,
                    completion_event_reference=log.completion_event_reference,
                )
                if receipt is None:
                    raise ValueError("terminal log lacks exact observation receipt")
                payload = {
                    "outcome_kind": "log",
                    "log": asdict(port_log),
                    "observation_receipt": receipt.model_dump(mode="json"),
                }
                return TerminalProcessLogOutcome(
                    outcome_kind="log",
                    log=port_log,
                    observation_receipt=receipt,
                    outcome_fingerprint=context_fingerprint(
                        "terminal-process-log-outcome:v1", payload
                    ),
                )
            result, action = self._execute_process_action(request)
        except KeyError as exc:
            return _process_rejected(
                request=request,
                process_id=process_id,
                status="not_found",
                code=TerminalPortRejectCode.PROCESS_NOT_FOUND,
                message=str(exc),
            )
        except ProcessInputError as exc:
            return _process_rejected(
                request=request,
                process_id=process_id,
                status="blocked",
                code=TerminalPortRejectCode.PROCESS_INPUT_REJECTED,
                message=str(exc),
            )
        port_result = _terminal_result(result)
        receipt = _observation_receipt(
            tool_call_id=owner.tool_call_id,
            action=action,
            observation_semantic=result.observation_semantic,
            completion_event_reference=result.completion_event_reference,
        )
        payload = {
            "action": action,
            "result": asdict(port_result),
            "receipt": receipt.model_dump(mode="json") if receipt is not None else None,
        }
        if isinstance(request, TerminalProcessKillInput):
            if receipt is None:
                raise ValueError("terminal kill lacks completion observation receipt")
            return TerminalProcessKilledOutcome(
                outcome_kind="killed",
                action="kill",
                result=port_result,
                completion_observation_receipt=receipt,
                outcome_fingerprint=context_fingerprint(
                    "terminal-process-killed-outcome:v1", payload
                ),
            )
        return TerminalProcessObservationOutcome(
            outcome_kind="observation",
            action=action,  # type: ignore[arg-type]
            result=port_result,
            observation_receipt=receipt,
            outcome_fingerprint=context_fingerprint(
                "terminal-process-observation-outcome:v1", payload
            ),
        )

    def _execute_process_action(self, request: TerminalProcessRequest):
        if isinstance(request, TerminalProcessPollInput):
            return (
                self.terminal_sessions.poll_process(
                    request.process_id,
                    max_output_chars=request.max_output_chars,
                    owner_host_session_id=self.owner_host_session_id,
                ),
                "poll",
            )
        if isinstance(request, TerminalProcessWaitInput):
            return (
                self.terminal_sessions.wait_process(
                    request.process_id,
                    timeout_seconds=request.timeout_seconds,
                    max_output_chars=request.max_output_chars,
                    owner_host_session_id=self.owner_host_session_id,
                ),
                "wait",
            )
        if isinstance(request, TerminalProcessKillInput):
            return (
                self.terminal_sessions.kill_process(
                    request.process_id,
                    max_output_chars=DEFAULT_MAX_OUTPUT_CHARS,
                    owner_host_session_id=self.owner_host_session_id,
                ),
                "kill",
            )
        if isinstance(request, TerminalProcessWriteInput | TerminalProcessSubmitInput):
            return (
                self.terminal_sessions.write_process(
                    request.process_id,
                    request.data,
                    append_newline=isinstance(request, TerminalProcessSubmitInput),
                    max_output_chars=DEFAULT_MAX_OUTPUT_CHARS,
                    owner_host_session_id=self.owner_host_session_id,
                ),
                "submit"
                if isinstance(request, TerminalProcessSubmitInput)
                else "write",
            )
        if isinstance(request, TerminalProcessCloseStdinInput):
            return (
                self.terminal_sessions.close_process_stdin(
                    request.process_id,
                    max_output_chars=DEFAULT_MAX_OUTPUT_CHARS,
                    owner_host_session_id=self.owner_host_session_id,
                ),
                "close_stdin",
            )
        raise AssertionError(type(request))


@dataclass(slots=True)
class RuntimeTerminalMonitorPort:
    workspace_root: Path
    terminal_monitor_coordinator: TerminalMonitorCoordinator | None

    def execute(
        self,
        *,
        request,
        owner: TerminalPortInvocationOwner,
    ) -> TerminalMonitorOutcome:
        _require_owner(owner, tool_name="terminal_monitor")
        process_id = (
            request.process_id
            if isinstance(request, TerminalMonitorRegisterInput)
            else None
        )
        monitor_id = (
            request.monitor_id
            if isinstance(request, TerminalMonitorCancelInput)
            else None
        )
        if owner.permission.terminal_access == "off":
            return _monitor_rejected(
                request=request,
                process_id=process_id,
                monitor_id=monitor_id,
                status="blocked",
                code=TerminalPortRejectCode.ACCESS_OFF,
                message="terminal_monitor is disabled by permission policy",
            )
        coordinator = self.terminal_monitor_coordinator
        if coordinator is None:
            return _monitor_rejected(
                request=request,
                process_id=process_id,
                monitor_id=monitor_id,
                status="blocked",
                code=TerminalPortRejectCode.MONITOR_OWNER_UNAVAILABLE,
                message="terminal monitor owner is unavailable",
            )
        runtime_context = ToolRuntimeContext(
            runtime_session_id=owner.runtime_session_id,
            event_context=owner.event_context,
            context_id=None,
            model_call_index=None,
            permission=owner.permission,
            owner_kind=owner.owner_kind,
        )
        try:
            if isinstance(request, TerminalMonitorRegisterInput):
                policy = resolve_terminal_monitor_public_policy(request)
                prepared = coordinator.prepare_registration(
                    process_id=request.process_id,
                    origin_tool_call_id=owner.tool_call_id,
                    runtime_context=runtime_context,
                    conditions=policy.conditions,
                    delivery=policy.delivery,
                    lifetime=policy.lifetime,
                )
                payload = {
                    "outcome_kind": "registered",
                    "registration_fingerprint": (
                        prepared.registration_semantic.registration_semantic_fingerprint
                    ),
                }
                return TerminalMonitorRegisteredOutcome(
                    outcome_kind="registered",
                    prepared_registration=prepared,
                    outcome_fingerprint=context_fingerprint(
                        "terminal-monitor-registered-outcome:v1", payload
                    ),
                )
            if isinstance(request, TerminalMonitorListInput):
                snapshots, omitted = coordinator.list_current_snapshots(maximum_items=8)
                items = tuple(_monitor_inventory_item(item) for item in snapshots)
                payload = {
                    "outcome_kind": "inventory",
                    "items": tuple(asdict(item) for item in items),
                    "omitted_monitor_count": omitted,
                }
                return TerminalMonitorInventoryOutcome(
                    outcome_kind="inventory",
                    items=items,
                    omitted_monitor_count=omitted,
                    outcome_fingerprint=context_fingerprint(
                        "terminal-monitor-inventory-outcome:v1", payload
                    ),
                )
            if isinstance(request, TerminalMonitorCancelInput):
                prepared = coordinator.prepare_cancellation(
                    monitor_id=request.monitor_id,
                    origin_tool_call_id=owner.tool_call_id,
                    runtime_context=runtime_context,
                )
                payload = {
                    "outcome_kind": "cancelled",
                    "monitor_id": prepared.monitor_id,
                    "outcome": prepared.outcome,
                    "cancellation_fingerprint": (
                        prepared.cancellation_semantic.cancellation_semantic_fingerprint
                        if prepared.cancellation_semantic is not None
                        else None
                    ),
                }
                return TerminalMonitorCancelledOutcome(
                    outcome_kind="cancelled",
                    prepared_cancellation=prepared,
                    outcome_fingerprint=context_fingerprint(
                        "terminal-monitor-cancelled-outcome:v1", payload
                    ),
                )
        except TerminalNotificationCapacityError as exc:
            return _monitor_rejected(
                request=request,
                process_id=process_id,
                monitor_id=monitor_id,
                status="blocked",
                code=(
                    TerminalPortRejectCode.MONITOR_DUPLICATE
                    if exc.reason_code == "terminal_monitor_already_active_for_process"
                    else TerminalPortRejectCode.MONITOR_CAPACITY_EXHAUSTED
                ),
                message=str(exc),
            )
        except KeyError as exc:
            return _monitor_rejected(
                request=request,
                process_id=process_id,
                monitor_id=monitor_id,
                status="not_found",
                code=TerminalPortRejectCode.MONITOR_NOT_FOUND,
                message=str(exc),
            )
        except ValueError as exc:
            return _monitor_rejected(
                request=request,
                process_id=process_id,
                monitor_id=monitor_id,
                status="blocked",
                code=(
                    TerminalPortRejectCode.MONITOR_DUPLICATE
                    if "already" in str(exc).lower()
                    else TerminalPortRejectCode.CONTRACT_MISMATCH
                ),
                message=str(exc),
            )
        raise AssertionError(type(request))


def _require_owner(
    owner: TerminalPortInvocationOwner,
    *,
    tool_name: Literal["terminal", "terminal_process", "terminal_monitor"],
) -> None:
    if owner.tool_name != tool_name:
        raise ValueError("terminal port invocation owner tool mismatch")


def _terminal_result(result) -> TerminalResult:
    metadata = _freeze_public_metadata(result.metadata)
    return TerminalResult(
        status=TerminalStatus(result.status.value),
        output=str(result.output),
        exit_code=int(result.exit_code),
        cwd=str(result.cwd),
        timed_out=bool(result.timed_out),
        truncated=bool(result.truncated),
        error=str(result.error) if result.error is not None else None,
        process_id=str(result.process_id) if result.process_id is not None else None,
        full_output_text=(
            str(result.full_output_text)
            if result.full_output_text is not None
            else None
        ),
        metadata=metadata,
        observation_semantic=result.observation_semantic,
        completion_event_reference=result.completion_event_reference,
    )


def _freeze_public_metadata(metadata: object) -> FrozenJsonObjectFact:
    source = metadata if isinstance(metadata, dict) else {}
    public: dict[str, object] = {}
    for key in (
        "command",
        "duration_seconds",
        "terminal_session_id",
        "backend_type",
        "io_mode",
        "stdin_closed",
        "policy_code",
        "suggested_args",
        "shell",
        "env",
        "started_at_monotonic",
        "ended_at_monotonic",
    ):
        value = source.get(key)
        if value is not None:
            public[key] = value.value if hasattr(value, "value") else value
    frozen = freeze_json(public)
    if not isinstance(frozen, FrozenJsonObjectFact):
        raise AssertionError("terminal result metadata must freeze as an object")
    return frozen


def _process_info(process) -> TerminalProcessInfo:
    return TerminalProcessInfo(
        process_id=process.process_id,
        terminal_session_id=process.terminal_session_id,
        command=process.command,
        cwd=process.cwd,
        backend_type=TerminalBackendType(process.backend_type),
        io_mode=TerminalIOMode(process.io_mode),
        status=TerminalStatus(process.status),
        exit_code=process.exit_code,
        timed_out=process.timed_out,
        stdin_closed=process.stdin_closed,
        started_at_monotonic=process.started_at_monotonic,
        ended_at_monotonic=process.ended_at_monotonic,
        duration_seconds=process.duration_seconds,
    )


def _process_log(log) -> TerminalProcessLog:
    return TerminalProcessLog(
        process=_process_info(log.process),
        output=log.output,
        truncated=log.truncated,
        full_output_text=log.full_output_text,
        observation_semantic=log.observation_semantic,
        completion_event_reference=log.completion_event_reference,
    )


def _observation_receipt(
    *,
    tool_call_id: str,
    action: str,
    observation_semantic,
    completion_event_reference,
) -> TerminalProcessObservationReceiptFact | None:
    if action not in {"poll", "log", "wait", "kill"} or observation_semantic is None:
        return None
    terminal = isinstance(
        observation_semantic.observed_state,
        TerminalProcessLifecycleOutcomeFact,
    )
    if terminal and completion_event_reference is None:
        return None
    return build_frozen_fact(
        TerminalProcessObservationReceiptFact,
        schema_version="terminal_process_observation_receipt.v1",
        observation_semantic=observation_semantic,
        action_kind=action,
        origin_tool_call_id=tool_call_id,
        completion_event_reference=completion_event_reference,
    )


def _command_rejected(
    *,
    request: TerminalCommandRequest,
    stage,
    code: TerminalPortRejectCode,
    message: str,
) -> TerminalCommandRejectedOutcome:
    payload = {
        "outcome_kind": "rejected",
        "command": request.command,
        "terminal_session_id": request.terminal_session_id,
        "failure_stage": stage,
        "reject_code": code.value,
        "sanitized_message": message,
    }
    return TerminalCommandRejectedOutcome(
        outcome_kind="rejected",
        command=request.command,
        terminal_session_id=request.terminal_session_id,
        failure_stage=stage,
        reject_code=code,
        sanitized_message=message,
        outcome_fingerprint=context_fingerprint(
            "terminal-command-rejected-outcome:v1", payload
        ),
    )


def _process_rejected(
    *,
    request: TerminalProcessRequest,
    process_id: str | None,
    status,
    code: TerminalPortRejectCode,
    message: str,
) -> TerminalProcessRejectedOutcome:
    payload = {
        "outcome_kind": "rejected",
        "requested_action": request.action,
        "process_id": process_id,
        "status": status,
        "reject_code": code.value,
        "sanitized_message": message,
    }
    return TerminalProcessRejectedOutcome(
        outcome_kind="rejected",
        requested_action=request.action,
        process_id=process_id,
        status=status,
        reject_code=code,
        sanitized_message=message,
        outcome_fingerprint=context_fingerprint(
            "terminal-process-rejected-outcome:v1", payload
        ),
    )


def _monitor_rejected(
    *,
    request,
    process_id: str | None,
    monitor_id: str | None,
    status,
    code: TerminalPortRejectCode,
    message: str,
) -> TerminalMonitorRejectedOutcome:
    payload = {
        "outcome_kind": "rejected",
        "requested_action": request.action,
        "process_id": process_id,
        "monitor_id": monitor_id,
        "status": status,
        "reject_code": code.value,
        "sanitized_message": message,
    }
    return TerminalMonitorRejectedOutcome(
        outcome_kind="rejected",
        requested_action=request.action,
        process_id=process_id,
        monitor_id=monitor_id,
        status=status,
        reject_code=code,
        sanitized_message=message,
        outcome_fingerprint=context_fingerprint(
            "terminal-monitor-rejected-outcome:v1", payload
        ),
    )


def _monitor_inventory_item(snapshot) -> TerminalMonitorInventoryItem:
    registration = snapshot.registration_event.registration_semantic
    payload = {
        "monitor_id": registration.monitor_id,
        "process_id": registration.initial_baseline_cursor.stream_identity.process_id,
        "lifecycle_state": snapshot.core_state.lifecycle_state,
        "observation_ordinal": snapshot.core_state.last_committed_observation_ordinal,
        "has_pending_observation": snapshot.pending_observation_event is not None,
    }
    return TerminalMonitorInventoryItem(
        **payload,
        item_fingerprint=context_fingerprint(
            "terminal-monitor-inventory-item:v1", payload
        ),
    )


__all__ = [
    "RuntimeTerminalCommandPort",
    "RuntimeTerminalMonitorPort",
    "RuntimeTerminalProcessPort",
]
