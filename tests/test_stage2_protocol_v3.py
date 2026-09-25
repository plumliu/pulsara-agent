from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from pulsara_agent.conversation_kernel.host import (
    KernelCommandOutcome,
    KernelControlQueryResult,
    KernelHostSession,
)
from pulsara_agent.conversation_kernel.user_control import (
    ControlQueryStatus,
    ProcessControlResult,
    UserControlExecution,
    UserControlOperation,
    UserControlOutcome,
    UserControlRequest,
    UserControlTarget,
    UserControlTargetKind,
)
from pulsara_agent.conversation_kernel.execution_watchdogs import (
    KernelExecutionDeadlineFactory,
)
from pulsara_agent.conversation_kernel.repository import AcceptedEntry
from pulsara_agent.conversation_kernel.interaction import KernelInteractionCoordinator
from pulsara_agent.conversation_kernel.io import KernelSessionIO
from pulsara_agent.conversation_kernel.live import LiveAgentEventBus
from pulsara_agent.terminal_protocol.canonical_v3 import (
    COMMITTED_PROJECTION_BRANCH_BY_TYPE,
    CanonicalQueueContentNotPending,
)
from pulsara_agent.conversation_kernel.vocabulary import CommittedEventType
from pulsara_agent.conversation_kernel.live_control import (
    CurrentInteractionView,
    LiveControlObservationKind,
    SessionLiveControlOwner,
)
from pulsara_agent.conversation_kernel.contracts import HostWriterGuard
from pulsara_agent.conversation_kernel.repository import (
    AcceptedInteractionDecision,
    ConversationKernelConflict,
)
from pulsara_agent.primitives.permission import PermissionMode
from pulsara_agent.primitives.run_permission import (
    RunPermissionAdmissionSource,
    build_run_permission_snapshot,
)
from pulsara_agent.llm.input import (
    LLMTextPart,
    MAXIMUM_PROMPT_TEXT_UTF8_BYTES,
    PromptImagePart,
)
from pulsara_agent.terminal_protocol.generated_v3 import terminal_kernel_v3_pb2 as wire
from pulsara_agent.terminal_protocol.v3_gateway import (
    TerminalKernelProtocolServer,
    _Connection,
)
from pulsara_agent.terminal_process.models import (
    TerminalProcessInfo,
    TerminalProcessLog,
    TerminalProcessOrigin,
)
from pulsara_agent.terminal_process.output import (
    TerminalOutputReadDisposition,
    TerminalOutputSourceCoverage,
)


def _wire_text_prompt(text: str) -> wire.PromptContent:
    return wire.PromptContent(parts=[wire.PromptContentPart(text=text)])


class _CommandHost:
    session_id = "session:test"
    host_session_id = "host:test"

    def __init__(self) -> None:
        self.controller_id = "attachment:test"
        self.submitted: list[tuple[str, object]] = []
        self.steered: list[tuple[str, str, str]] = []
        self.resolved: list[dict[str, object]] = []
        self.accepted_subagent_completions: list[dict[str, object]] = []
        self.accepted_job_results: list[dict[str, object]] = []
        self.stop_requests: list[dict[str, str]] = []
        self.cancel_requests: list[dict[str, str]] = []
        self.terminate_requests: list[dict[str, str]] = []
        self.control_queries: list[UserControlRequest] = []
        self.background_reads: list[dict[str, object]] = []

    def has_controller_attachment(self, attachment_id):
        return attachment_id == self.controller_id

    async def controller_detached(self, attachment_id):
        if self.controller_id == attachment_id:
            self.controller_id = None

    async def submit_prompt(self, *, command_id: str, content, **_kwargs) -> KernelCommandOutcome:
        self.submitted.append((command_id, content))
        return KernelCommandOutcome(
            command_id, "PENDING", "queue:item", "PROMPT_QUEUED", "Queued."
        )

    def _control_outcome(
        self,
        *,
        command_id: str,
        operation: UserControlOperation,
        target_kind: UserControlTargetKind,
        target_id: str,
    ) -> KernelCommandOutcome:
        return KernelCommandOutcome(
            command_id,
            "PENDING",
            target_id,
            "CONTROL_ACCEPTED",
            "Control accepted.",
            user_control=UserControlOutcome(
                operation=operation,
                session_id=self.session_id,
                host_session_id=self.host_session_id,
                target=UserControlTarget(target_kind, target_id),
                accepted=True,
                execution=UserControlExecution.RUNNING,
            ),
        )

    async def request_stop_turn(self, **kwargs) -> KernelCommandOutcome:
        self.stop_requests.append(dict(kwargs))
        return self._control_outcome(
            command_id=str(kwargs["command_id"]),
            operation=UserControlOperation.STOP_ACTIVE_TURN,
            target_kind=UserControlTargetKind.ROOT_TURN,
            target_id=str(kwargs["target_turn_id"]),
        )

    async def request_cancel_subagent(self, **kwargs) -> KernelCommandOutcome:
        self.cancel_requests.append(dict(kwargs))
        return self._control_outcome(
            command_id=str(kwargs["command_id"]),
            operation=UserControlOperation.CANCEL_SUBAGENT_TASK,
            target_kind=UserControlTargetKind.SUBAGENT_TASK,
            target_id=str(kwargs["task_id"]),
        )

    async def request_terminate_background_process(
        self, **kwargs
    ) -> KernelCommandOutcome:
        self.terminate_requests.append(dict(kwargs))
        return self._control_outcome(
            command_id=str(kwargs["command_id"]),
            operation=UserControlOperation.TERMINATE_BACKGROUND_PROCESS,
            target_kind=UserControlTargetKind.BACKGROUND_PROCESS,
            target_id=str(kwargs["process_id"]),
        )

    async def query_control_command(
        self, request: UserControlRequest
    ) -> KernelControlQueryResult:
        self.control_queries.append(request)
        return KernelControlQueryResult(
            ControlQueryStatus.FOUND,
            KernelCommandOutcome(
                request.command_id,
                "SUCCEEDED",
                request.target.target_id,
                "BACKGROUND_CONTROL_COMPLETED",
                "Control completed.",
                user_control=UserControlOutcome(
                    operation=request.operation,
                    session_id=request.session_id,
                    host_session_id=request.host_session_id,
                    target=request.target,
                    accepted=True,
                    execution=UserControlExecution.FINISHED,
                    process=ProcessControlResult(
                        disposition="TERMINATION_COMPLETED",
                        status="killed",
                        exit_code=-15,
                        physical_state="PHYSICALLY_JOINED",
                        group_alive=False,
                    ),
                ),
            ),
        )

    def list_background_processes(self, **kwargs) -> tuple[TerminalProcessInfo, ...]:
        self.background_reads.append({"kind": "list", **kwargs})
        def process(process_id: str, started_at: float) -> TerminalProcessInfo:
            return TerminalProcessInfo(
                process_id=process_id,
                command="sleep 60",
                cwd="/tmp",
                backend_type="local",
                io_mode="pipe",
                status="running",
                exit_code=None,
                timed_out=False,
                stdin_closed=False,
                started_at_monotonic=started_at,
                ended_at_monotonic=None,
                duration_seconds=1.0,
                owner_host_session_id=self.host_session_id,
                origin=TerminalProcessOrigin("turn:root", "ROOT"),
                stream_id="stream:1",
                output_revision=2,
                output_cursor="cursor:2",
                retained_from_cursor="cursor:0",
                background_adopted=True,
            )

        return (
            process("process:2", 20.0),
            process("process:1", 10.0),
        )

    def read_background_process_log(self, **kwargs) -> TerminalProcessLog:
        self.background_reads.append({"kind": "log", **kwargs})
        process = self.list_background_processes(
            expected_session_id=kwargs["expected_session_id"],
            expected_host_session_id=kwargs["expected_host_session_id"],
        )[1]
        return TerminalProcessLog(
            process=process,
            output="exact output\n",
            truncated=False,
            output_disposition=TerminalOutputReadDisposition.EXACT_DELTA,
            output_cursor="cursor:2",
            retained_from_cursor="cursor:0",
            source_coverage=TerminalOutputSourceCoverage.COMPLETE,
        )

    async def steer_queued_prompt(
        self, *, command_id: str, source_queue_item_id: str, target_turn_id: str
    ) -> KernelCommandOutcome:
        self.steered.append((command_id, source_queue_item_id, target_turn_id))
        return KernelCommandOutcome(
            command_id,
            "PENDING",
            target_turn_id,
            "PROMPT_QUEUED",
            "Steer queued.",
        )

    async def cancel_queued_prompt(
        self, *, command_id: str, source_queue_item_id: str
    ) -> KernelCommandOutcome:
        self.steered.append((command_id, source_queue_item_id, ""))
        return KernelCommandOutcome(command_id, "SUCCEEDED", source_queue_item_id,
                                    "PROMPT_CANCELLED", "Cancelled.")

    async def query_command(self, command_id: str) -> None:
        del command_id
        return None

    async def resolve_tool_interaction(self, **kwargs) -> KernelCommandOutcome:
        self.resolved.append(dict(kwargs))
        return KernelCommandOutcome(
            str(kwargs["command_id"]),
            "SUCCEEDED",
            "decision:1",
            "INTERACTION_ALLOW",
            "Accepted.",
        )

    async def accept_subagent_completion(self, **kwargs) -> KernelCommandOutcome:
        self.accepted_subagent_completions.append(dict(kwargs))
        return KernelCommandOutcome(
            str(kwargs["command_id"]),
            "SUCCEEDED",
            "entry:accepted-child",
            "SUBAGENT_COMPLETION_DELIVERED",
            "Accepted.",
        )

    async def accept_job_result(self, **kwargs) -> KernelCommandOutcome:
        self.accepted_job_results.append(dict(kwargs))
        return KernelCommandOutcome(
            str(kwargs["command_id"]),
            "SUCCEEDED",
            "entry:accepted-job",
            "JOB_RESULT_ACCEPTED",
            "Accepted.",
        )


def _server() -> TerminalKernelProtocolServer:
    return TerminalKernelProtocolServer(
        socket_path=Path("/tmp/pulsara-stage2-protocol-test.sock"),
        session_provider=lambda _: (_ for _ in ()).throw(KeyError()),
    )


def _state(*, role: int) -> _Connection:
    return _Connection(
        attachment_id="attachment:test",
        attachment_generation=1,
        host_session=_CommandHost(),  # type: ignore[arg-type]
        granted_role=role,
        authenticated=True,
    )


def test_pr02_protocol_carries_exact_result_queue_and_prompt_delivery_identity() -> None:
    tool_result = wire.CanonicalToolResult.DESCRIPTOR.fields_by_name
    assert {
        "assistant_entry_id",
        "tool_call_id",
        "result_state",
        "artifact_disposition",
        "source_coverage",
        "display_kind",
        "source_coverage_reason",
        "artifact_unavailability_reason",
    } <= set(tool_result)

    entry = wire.CanonicalEntry.DESCRIPTOR.fields_by_name
    assert "input_source" in entry
    assert {
        "queue_item_id",
        "command_id",
        "delivery_mode",
    } <= set(entry["input_source"].message_type.fields_by_name)

    queue = wire.PromptQueueControl.DESCRIPTOR.fields_by_name
    assert "command_id" in queue
    outcome = wire.CommandOutcome.DESCRIPTOR.fields_by_name
    assert "prompt_delivery" in outcome
    assert {
        "queue_item_id",
        "queue_status",
        "consumed_entry_id",
        "delivery_mode",
    } <= set(outcome["prompt_delivery"].message_type.fields_by_name)

    read_content = wire.ReadContentRequest.DESCRIPTOR
    assert read_content.oneofs_by_name["target"].fields[0].name == "entry_id"
    assert read_content.oneofs_by_name["target"].fields[1].name == "queue_item_id"
    assert "read_tool_artifact" in wire.ClientFrame.DESCRIPTOR.fields_by_name
    assert "tool_artifact" in wire.ServerFrame.DESCRIPTOR.fields_by_name


def test_pr02_read_content_reports_an_exact_queue_transition_separately_from_missing() -> None:
    class _QueueTransitionReader:
        def resolve_content_reference(self, **kwargs):
            raise CanonicalQueueContentNotPending(
                queue_item_id=str(kwargs["queue_item_id"]),
                status="CONSUMED",
                consumed_entry_id="entry:consumed",
            )

    state = _state(role=wire.ATTACHMENT_ROLE_OBSERVER)
    state.protocol_reader = _QueueTransitionReader()  # type: ignore[assignment]
    response = asyncio.run(_server()._read_content(
        state,
        wire.ReadContentRequest(
            request_id="request:queue-transition",
            queue_item_id="queue:consumed",
            offset_bytes=0,
            limit_bytes=1024,
        ),
    ))

    assert response.error.stable_code == "CONTENT_QUEUE_NOT_PENDING"


class _PresentationNoticeInteractions:
    def __init__(self, controller_id: str | None) -> None:
        self.controller_id = controller_id

    def current_controller_id(self) -> str | None:
        return self.controller_id

    def is_current_controller(self, attachment_id: str) -> bool:
        return attachment_id == self.controller_id

    async def controller_detached(self, attachment_id: str) -> None:
        if self.controller_id == attachment_id:
            self.controller_id = None


def test_stage2_presentation_notice_is_ephemeral_and_controller_bound() -> None:
    host = object.__new__(KernelHostSession)
    interactions = _PresentationNoticeInteractions("attachment:old")
    host._interactions = interactions
    host._presentation_notices = {}

    host._offer_presentation_notice("one-shot notice")
    assert host.take_presentation_notices("attachment:observer") == ()
    assert host.take_presentation_notices("attachment:old") == ("one-shot notice",)
    assert host.take_presentation_notices("attachment:old") == ()

    host._offer_presentation_notice("must not replay after reconnect")
    asyncio.run(host.controller_detached("attachment:old"))
    interactions.controller_id = "attachment:new"
    assert host.take_presentation_notices("attachment:new") == ()
    assert host._presentation_notices == {}


def test_stage2_observer_cannot_mutate_but_can_detach() -> None:
    server = _server()
    observer = _state(role=wire.ATTACHMENT_ROLE_OBSERVER)
    rejected = asyncio.run(
        server._command(
            observer,
            wire.CommandRequest(
                request_id="request:submit",
                command_id="command:submit",
                client_submission_id="command:submit",
                command_kind=wire.SUBMIT_PROMPT,
                prompt_content=_wire_text_prompt("hello"),
                requested_permission_mode=wire.PERMISSION_MODE_READ_ONLY,
            ),
        )
    )
    assert rejected.error.stable_code == "CONTROLLER_REQUIRED"
    assert observer.host_session.submitted == []

    detached = asyncio.run(
        server._command(
            observer,
            wire.CommandRequest(
                request_id="request:detach",
                command_id="command:detach",
                client_submission_id="command:detach",
                command_kind=wire.DETACH,
            ),
        )
    )
    assert detached.command_outcome.status == wire.SUCCEEDED


@pytest.mark.parametrize(
    ("command_kind", "target_field", "target_id", "record_name"),
    (
        (wire.STOP_ACTIVE_TURN, "target_turn_id", "turn:A", "stop_requests"),
        (
            wire.CANCEL_SUBAGENT_TASK,
            "subagent_task_id",
            "task:A",
            "cancel_requests",
        ),
        (
            wire.TERMINATE_BACKGROUND_PROCESS,
            "target_process_id",
            "process:A",
            "terminate_requests",
        ),
    ),
)
def test_pr03_controller_commands_preserve_exact_owner_and_target(
    command_kind: int,
    target_field: str,
    target_id: str,
    record_name: str,
) -> None:
    server = _server()
    controller = _state(role=wire.ATTACHMENT_ROLE_CONTROLLER)
    request = wire.CommandRequest(
        request_id=f"request:{target_id}",
        command_id="command:control:9999999999999:11111111-1111-4111-8111-111111111111",
        command_kind=command_kind,
        expected_session_id="session:test",
        expected_host_session_id="host:test",
        **{target_field: target_id},
    )

    response = asyncio.run(server._command(controller, request))

    assert response.command_outcome.status == wire.PENDING
    assert response.command_outcome.user_control.target.target_id == target_id
    calls = getattr(controller.host_session, record_name)
    assert calls == [
        {
            "command_id": request.command_id,
            "expected_session_id": "session:test",
            "expected_host_session_id": "host:test",
            {
                "target_turn_id": "target_turn_id",
                "subagent_task_id": "task_id",
                "target_process_id": "process_id",
            }[target_field]: target_id,
        }
    ]


def test_pr03_control_rejects_a_missing_exact_target_without_calling_host() -> None:
    controller = _state(role=wire.ATTACHMENT_ROLE_CONTROLLER)
    response = asyncio.run(
        _server()._command(
            controller,
            wire.CommandRequest(
                request_id="request:stop-missing-target",
                command_id=(
                    "command:control:9999999999999:"
                    "22222222-2222-4222-8222-222222222222"
                ),
                command_kind=wire.STOP_ACTIVE_TURN,
                expected_session_id="session:test",
                expected_host_session_id="host:test",
            ),
        )
    )

    assert response.error.stable_code == "STOP_REQUEST_INVALID"
    assert controller.host_session.stop_requests == []


def test_pr03_control_query_requires_controller_and_exact_expectation() -> None:
    request = wire.QueryCommandRequest(
        request_id="request:query-control",
        command_id="command:control:9999999999999:33333333-3333-4333-8333-333333333333",
        expected_control=wire.ExpectedUserControl(
            operation=wire.USER_CONTROL_TERMINATE_BACKGROUND_PROCESS,
            session_id="session:test",
            host_session_id="host:test",
            target=wire.UserControlTarget(
                kind=wire.USER_CONTROL_BACKGROUND_PROCESS,
                target_id="process:1",
            ),
        ),
    )
    observer = _state(role=wire.ATTACHMENT_ROLE_OBSERVER)
    denied = asyncio.run(_server()._query_command(observer, request))
    assert denied.error.stable_code == "CONTROLLER_REQUIRED"
    assert observer.host_session.control_queries == []

    controller = _state(role=wire.ATTACHMENT_ROLE_CONTROLLER)
    response = asyncio.run(_server()._query_command(controller, request))
    assert response.query_command.control_query_status == wire.CONTROL_QUERY_FOUND
    assert response.query_command.outcome.status == wire.SUCCEEDED
    assert response.query_command.outcome.user_control.process.group_alive is False
    assert controller.host_session.control_queries == [
        UserControlRequest(
            UserControlOperation.TERMINATE_BACKGROUND_PROCESS,
            request.command_id,
            "session:test",
            "host:test",
            UserControlTarget(
                UserControlTargetKind.BACKGROUND_PROCESS,
                "process:1",
            ),
        )
    ]


def test_pr03_observer_can_page_background_processes_and_read_exact_log() -> None:
    observer = _state(role=wire.ATTACHMENT_ROLE_OBSERVER)
    server = _server()
    first = asyncio.run(
        server._list_background_processes(
            observer,
            wire.ListBackgroundProcessesRequest(
                request_id="request:background-first",
                expected_session_id="session:test",
                expected_host_session_id="host:test",
                maximum_items=1,
            ),
        )
    )
    assert [item.process_id for item in first.background_processes.processes] == [
        "process:1"
    ]
    assert first.background_processes.next_cursor

    second = asyncio.run(
        server._list_background_processes(
            observer,
            wire.ListBackgroundProcessesRequest(
                request_id="request:background-second",
                expected_session_id="session:test",
                expected_host_session_id="host:test",
                cursor=first.background_processes.next_cursor,
                maximum_items=1,
            ),
        )
    )
    assert [item.process_id for item in second.background_processes.processes] == [
        "process:2"
    ]
    assert not second.background_processes.next_cursor

    log = asyncio.run(
        server._read_background_process_log(
            observer,
            wire.ReadBackgroundProcessLogRequest(
                request_id="request:background-log",
                expected_session_id="session:test",
                expected_host_session_id="host:test",
                process_id="process:1",
                output_cursor="cursor:1",
                max_output_chars=512,
            ),
        )
    )
    assert log.background_process_log.process.process_id == "process:1"
    assert log.background_process_log.output == "exact output\n"
    assert log.background_process_log.output_cursor == "cursor:2"

    wrong_owner = asyncio.run(
        server._list_background_processes(
            observer,
            wire.ListBackgroundProcessesRequest(
                request_id="request:wrong-owner",
                expected_session_id="session:test",
                expected_host_session_id="host:other",
                maximum_items=1,
            ),
        )
    )
    assert wrong_owner.error.stable_code == "BACKGROUND_OWNER_MISMATCH"


def test_stage2_controller_prompt_bounds_are_authoritative() -> None:
    server = _server()
    controller = _state(role=wire.ATTACHMENT_ROLE_CONTROLLER)
    for text in (
        "",
        "bad\x00text",
        "x" * (MAXIMUM_PROMPT_TEXT_UTF8_BYTES + 1),
    ):
        result = asyncio.run(
            server._command(
                controller,
                wire.CommandRequest(
                    request_id="request:submit",
                    command_id="command:submit",
                    client_submission_id="command:submit",
                    command_kind=wire.SUBMIT_PROMPT,
                    prompt_content=_wire_text_prompt(text),
                    requested_permission_mode=wire.PERMISSION_MODE_READ_ONLY,
                ),
            )
        )
        assert result.error.stable_code == "PROMPT_INVALID"
    assert controller.host_session.submitted == []


def test_u2_controller_submits_one_ordered_typed_prompt_without_figure_projection() -> None:
    server = _server()
    controller = _state(role=wire.ATTACHMENT_ROLE_CONTROLLER)
    image = b"same-immutable-image"
    prompt = wire.PromptContent(
        parts=[
            wire.PromptContentPart(text="before\n[Figure 1]"),
            wire.PromptContentPart(
                image=wire.PromptImagePart(
                    content=image,
                    declared_media_type="image/png",
                )
            ),
            wire.PromptContentPart(text="after"),
            wire.PromptContentPart(
                image=wire.PromptImagePart(
                    content=image,
                    declared_media_type="image/png",
                )
            ),
        ]
    )
    result = asyncio.run(
        server._command(
            controller,
            wire.CommandRequest(
                request_id="request:typed",
                command_id="command:typed",
                client_submission_id="command:typed",
                command_kind=wire.SUBMIT_PROMPT,
                prompt_content=prompt,
                requested_permission_mode=wire.PERMISSION_MODE_READ_ONLY,
            ),
        )
    )
    assert result.command_outcome.status == wire.PENDING
    [(command_id, content)] = controller.host_session.submitted
    assert command_id == "command:typed"
    assert content.parts == (
        LLMTextPart("before\n[Figure 1]"),
        PromptImagePart(original_bytes=image, declared_mime="image/png"),
        LLMTextPart("after"),
        PromptImagePart(original_bytes=image, declared_mime="image/png"),
    )


def test_u2_submit_prompt_cannot_be_reinterpreted_as_direct_active_steer() -> None:
    server = _server()
    controller = _state(role=wire.ATTACHMENT_ROLE_CONTROLLER)
    result = asyncio.run(
        server._command(
            controller,
            wire.CommandRequest(
                request_id="request:no-active-steer",
                command_id="command:no-active-steer",
                client_submission_id="command:no-active-steer",
                command_kind=wire.SUBMIT_PROMPT,
                target_turn_id="turn:active",
                prompt_content=_wire_text_prompt("must stay a new turn"),
                requested_permission_mode=wire.PERMISSION_MODE_READ_ONLY,
            ),
        )
    )
    assert result.error.stable_code == "PROMPT_INVALID"
    assert controller.host_session.submitted == []


def test_stage2_controller_can_send_an_exact_active_turn_steer() -> None:
    server = _server()
    controller = _state(role=wire.ATTACHMENT_ROLE_CONTROLLER)
    result = asyncio.run(
        server._command(
            controller,
            wire.CommandRequest(
                request_id="request:steer",
                command_id="command:steer",
                client_submission_id="command:steer",
                command_kind=wire.STEER_QUEUED_PROMPT,
                target_queue_item_id="queue:source",
                target_turn_id="turn:active",
            ),
        )
    )
    assert result.command_outcome.status == wire.PENDING
    assert controller.host_session.steered == [
        ("command:steer", "queue:source", "turn:active")
    ]


def test_stage2_controller_can_deliver_exact_terminal_subagent_task() -> None:
    server = _server()
    controller = _state(role=wire.ATTACHMENT_ROLE_CONTROLLER)
    result = asyncio.run(
        server._command(
            controller,
            wire.CommandRequest(
                request_id="request:accept-child",
                command_id="command:accept-child",
                client_submission_id="command:accept-child",
                command_kind=wire.ACCEPT_SUBAGENT_COMPLETION,
                target_turn_id="turn:root",
                subagent_task_id="task:1",
            ),
        )
    )
    assert result.command_outcome.status == wire.SUCCEEDED
    assert controller.host_session.accepted_subagent_completions == [
        {
            "command_id": "command:accept-child",
            "target_turn_id": "turn:root",
            "requested_permission_mode": None,
            "task_id": "task:1",
            "actor_id": "attachment:test",
        }
    ]


def test_stage2_controller_can_continue_from_a_completion_in_a_new_root() -> None:
    server = _server()
    controller = _state(role=wire.ATTACHMENT_ROLE_CONTROLLER)
    subagent = asyncio.run(
        server._command(
            controller,
            wire.CommandRequest(
                request_id="request:accept-child-new-turn",
                command_id="command:accept-child-new-turn",
                client_submission_id="command:accept-child-new-turn",
                command_kind=wire.ACCEPT_SUBAGENT_COMPLETION,
                subagent_task_id="task:2",
                requested_permission_mode=wire.PERMISSION_MODE_ACCEPT_EDITS,
            ),
        )
    )
    assert subagent.command_outcome.status == wire.SUCCEEDED
    assert (
        controller.host_session.accepted_subagent_completions[-1]["target_turn_id"]
        is None
    )
    assert (
        controller.host_session.accepted_subagent_completions[-1][
            "requested_permission_mode"
        ]
        is PermissionMode.ACCEPT_EDITS
    )


def test_stage2_new_root_subagent_completion_requires_turn_permission() -> None:
    server = _server()
    controller = _state(role=wire.ATTACHMENT_ROLE_CONTROLLER)

    result = asyncio.run(
        server._command(
            controller,
            wire.CommandRequest(
                request_id="request:accept-child-without-permission",
                command_id="command:accept-child-without-permission",
                client_submission_id="command:accept-child-without-permission",
                command_kind=wire.ACCEPT_SUBAGENT_COMPLETION,
                subagent_task_id="task:3",
            ),
        )
    )

    assert result.error.stable_code == "SUBAGENT_COMPLETION_REQUEST_INVALID"
    assert controller.host_session.accepted_subagent_completions == []


def _removed_stage2_host_exposes_job_result_acceptance_to_production_protocol() -> None:
    class _Runner:
        def __init__(self) -> None:
            self.kwargs: dict[str, object] = {}

        async def accept_job_result(self, **kwargs) -> AcceptedEntry:
            self.kwargs = dict(kwargs)
            return AcceptedEntry("entry:job", "turn:root", 4, 7)

    host = object.__new__(KernelHostSession)
    host._closing = False
    host._deadlines = KernelExecutionDeadlineFactory()
    host._runner = _Runner()

    async def query_command(_: str) -> None:
        return None

    host.query_command = query_command  # type: ignore[method-assign]
    outcome = asyncio.run(
        host.accept_job_result(
            command_id="command:job",
            target_turn_id="turn:root",
            job_id="job:durable",
            actor_id="attachment:controller",
        )
    )
    assert outcome.public_code == "JOB_RESULT_ACCEPTED"
    assert host._runner.kwargs["job_id"] == "job:durable"


def test_stage2_interaction_resolution_is_controller_only_and_exact() -> None:
    server = _server()
    request = wire.ResolveInteractionRequest(
        request_id="request:resolve",
        command_id="command:resolve",
        expected_writer_generation=3,
        expected_owner_epoch=3,
        expected_live_revision=4,
        interaction_id="interaction:1",
        decision=wire.INTERACTION_ALLOW,
    )
    observer = _state(role=wire.ATTACHMENT_ROLE_OBSERVER)
    rejected = asyncio.run(server._resolve_interaction(observer, request))
    assert rejected.error.stable_code == "CONTROLLER_REQUIRED"
    assert observer.host_session.resolved == []

    controller = _state(role=wire.ATTACHMENT_ROLE_CONTROLLER)
    accepted = asyncio.run(server._resolve_interaction(controller, request))
    assert accepted.command_outcome.status == wire.SUCCEEDED
    assert controller.host_session.resolved == [
        {
            "expected_writer_generation": 3,
            "expected_owner_epoch": 3,
            "expected_live_revision": 4,
            "interaction_id": "interaction:1",
            "command_id": "command:resolve",
            "decision": "ALLOW",
            "actor_id": "attachment:test",
        }
    ]


class _InteractionRepository:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    def accept_tool_interaction_decision(self, guard, **kwargs):
        del guard
        self.calls.append(dict(kwargs))
        return AcceptedInteractionDecision(
            str(kwargs["decision_id"]),
            str(kwargs["command_id"]),
            str(kwargs["decision"]),
            str(kwargs["assistant_entry_id"]),
            str(kwargs["tool_call_id"]),
            kwargs["attempt_id"],
            kwargs["result_entry_id"],
            str(kwargs["permission_snapshot_fingerprint"]),
            kwargs.get("result_id"),
            1 if kwargs["result_entry_id"] is not None else None,
            kwargs["occurred_at"] if kwargs["result_entry_id"] is not None else None,
        )


def test_stage2_pending_interaction_is_same_host_ephemeral_and_stale_safe() -> None:
    async def exercise() -> None:
        repository = _InteractionRepository()
        guard = HostWriterGuard(
            session_id="session:test",
            writer_generation=7,
            writer_owner_id="host:test",
        )
        owner = SessionLiveControlOwner(session_id="session:test", owner_epoch=7)
        coordinator = KernelInteractionCoordinator(
            repository=repository,  # type: ignore[arg-type]
            guard=guard,
            live_control=owner,
            live_bus=LiveAgentEventBus(),
            io_owner=KernelSessionIO(),
        )
        assert await coordinator.attach_controller("attachment:1")
        waiter = asyncio.create_task(
            coordinator.request_tool_confirmation(
                turn_id="turn:1",
                assistant_entry_id="entry:assistant",
                tool_call_id="call:1",
                tool_name="terminal",
                permission_snapshot=build_run_permission_snapshot(
                    snapshot_id="permission:test",
                    requested_mode=PermissionMode.ASK_PERMISSIONS,
                    effective_mode=PermissionMode.ASK_PERMISSIONS,
                    admission_source=RunPermissionAdmissionSource.USER_SUBMISSION,
                ),
            )
        )
        await asyncio.sleep(0)
        snapshot = owner.current_snapshot()
        assert snapshot.owner_epoch == 7
        assert snapshot.current_interaction is not None
        with pytest.raises(ConversationKernelConflict):
            await coordinator.resolve_tool_interaction(
                expected_writer_generation=7,
                expected_owner_epoch=7,
                expected_live_revision=snapshot.revision + 1,
                interaction_id=snapshot.current_interaction.interaction_id,
                command_id="command:stale",
                decision="ALLOW",
                actor_id="attachment:1",
            )
        accepted = await coordinator.resolve_tool_interaction(
            expected_writer_generation=7,
            expected_owner_epoch=7,
            expected_live_revision=snapshot.revision,
            interaction_id=snapshot.current_interaction.interaction_id,
            command_id="command:allow",
            decision="ALLOW",
            actor_id="attachment:1",
        )
        resolution = await waiter
        assert accepted.attempt_id == resolution.attempt_id
        assert owner.current_snapshot().current_interaction is None
        assert len(repository.calls) == 1
        await coordinator.aclose()

    asyncio.run(exercise())


def test_stage2_live_control_snapshot_and_cursor_are_linearized() -> None:
    owner = SessionLiveControlOwner(
        session_id="session:test", maximum_events=2, maximum_public_bytes=4096
    )
    subscriber, snapshot = owner.snapshot_and_subscribe()
    assert snapshot.revision == 0 and snapshot.current_interaction is None
    first = CurrentInteractionView(
        "interaction:1", "APPROVAL", "Allow the tool?", ("allow", "deny"), ""
    )
    opened = owner.install_interaction(first)
    repeated = owner.observe(
        subscriber, owner_epoch=1, after_revision=0, maximum_events=2
    )
    assert repeated.events == (opened,)
    assert (
        owner.observe(subscriber, owner_epoch=1, after_revision=0, maximum_events=2)
        == repeated
    )
    second = CurrentInteractionView(
        "interaction:2", "PLAN", "Accept the plan?", ("yes", "no"), ""
    )
    owner.install_interaction(second, replace_expected_interaction_id="interaction:1")
    owner.close_interaction(expected_interaction_id="interaction:2")
    gap = owner.observe(subscriber, owner_epoch=1, after_revision=0, maximum_events=2)
    assert gap.kind is LiveControlObservationKind.GAP


def test_stage2_protocol_v3_closed_vocabularies_are_exact() -> None:
    committed = {
        item.name
        for item in wire.CommittedEventType.DESCRIPTOR.values
        if item.number != 0
    }
    live = {
        item.name for item in wire.LiveEventType.DESCRIPTOR.values if item.number != 0
    }
    assert len(committed) == 30
    assert len(live) == 24
    assert set(COMMITTED_PROJECTION_BRANCH_BY_TYPE) == {
        item.value for item in CommittedEventType
    }
    assert {
        key
        for key, value in COMMITTED_PROJECTION_BRANCH_BY_TYPE.items()
        if value == "IMMUTABLE_ENTRY"
    } == {
        "UserMessageAccepted",
        "AssistantMessageAccepted",
        "AssistantToolRequestAccepted",
        "ToolResultAccepted",
        "UserSteerAccepted",
        "TerminalObservationAccepted",
        "UserControlFeedbackAccepted",
        "InterAgentMessageAccepted",
        "PlanContinuationAccepted",
    }
    assert (
        sum(
            value == "CURRENT_CONTROL"
            for value in COMMITTED_PROJECTION_BRANCH_BY_TYPE.values()
        )
        == 19
    )
    assert (
        sum(
            value == "EVENT_ONLY"
            for value in COMMITTED_PROJECTION_BRANCH_BY_TYPE.values()
        )
        == 2
    )
    assert {item.name for item in wire.ObservationGapKind.DESCRIPTOR.values} == {
        "OBSERVATION_GAP_KIND_UNSPECIFIED",
        "COMMITTED_GAP",
        "LIVE_GAP",
        "LIVE_CONTROL_GAP",
    }


def test_pr04_cancel_queue_command_and_closed_field_matrix():
    server = _server()
    controller = _state(role=wire.ATTACHMENT_ROLE_CONTROLLER)
    observer = _state(role=wire.ATTACHMENT_ROLE_OBSERVER)
    valid = dict(command_id='command:cancel', command_kind=wire.CANCEL_QUEUED_PROMPT,
                 target_queue_item_id='queue:source')
    accepted = asyncio.run(server._command(controller, wire.CommandRequest(**valid)))
    assert accepted.command_outcome.status == wire.SUCCEEDED
    assert accepted.command_outcome.public_code == 'PROMPT_CANCELLED'
    assert asyncio.run(server._command(observer, wire.CommandRequest(**valid))).error.stable_code == 'CONTROLLER_REQUIRED'
    for changed, code in [
        ({'plan_reason': 'must not resend body'}, 'QUEUE_ACTION_INVALID'),
        ({'target_turn_id': 'turn:wrong'}, 'QUEUE_ACTION_INVALID'),
        ({'target_queue_item_id': ''}, 'QUEUE_ACTION_INVALID'),
        ({'requested_permission_mode': wire.PERMISSION_MODE_READ_ONLY}, 'PERMISSION_FIELD_NOT_ALLOWED'),
        ({'command_kind': wire.SUBMIT_PROMPT,
          'prompt_content': _wire_text_prompt('hello')}, 'QUEUE_TARGET_FIELD_NOT_ALLOWED'),
        ({'command_kind': wire.STEER_QUEUED_PROMPT}, 'QUEUE_ACTION_INVALID'),
        ({'expected_session_id': 'another-session'}, 'CONTROL_FIELDS_NOT_ALLOWED'),
    ]:
        result = asyncio.run(server._command(controller, wire.CommandRequest(**(valid | changed))))
        assert result.error.stable_code == code
    assert controller.host_session.steered == [('command:cancel', 'queue:source', '')]
    assert 'STEER_ACTIVE_TURN' not in wire.CommandKind.keys()
