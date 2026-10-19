from __future__ import annotations
from pulsara_agent.identity import derive_id

import asyncio
from pathlib import Path
from types import SimpleNamespace

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
from pulsara_agent.conversation_kernel.workspace import WorkspaceExecutionGate
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
    StaleHostWriter,
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
    session_id = 'session:336wrfwkamrr3up6kjtsgvgcte'
    host_session_id = 'host:yinyxklzunar54s7objjdbphcy'

    def __init__(self) -> None:
        self.workspace_gate = WorkspaceExecutionGate(Path.cwd())
        self.repository = SimpleNamespace(connection_provider=None)
        self.controller_id = 'attachment:hmfv6qyb4msxxwadjyltzzsdse'
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
            command_id, "PENDING", 'queue:rtmcfbzvd2gwmqy5gyvysailji', "PROMPT_QUEUED", "Queued."
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
                origin=TerminalProcessOrigin('turn:b565wrncq4ezfqthehjqni5ilm', "ROOT"),
                stream_id='stream:uyxriew7zy36vqbrqxysa67wzq',
                output_revision=2,
                output_cursor="cursor:2",
                retained_from_cursor="cursor:0",
                background_adopted=True,
            )

        return (
            process('process:evw46zjzwyi55v7ihcmnj7p5vm', 20.0),
            process('process:72qov6ocdngiw5hv6gjo4g2o7i', 10.0),
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
            'decision:msm3yvp3o6enegbfcovfriproi',
            "INTERACTION_ALLOW",
            "Accepted.",
        )

    async def accept_subagent_completion(self, **kwargs) -> KernelCommandOutcome:
        self.accepted_subagent_completions.append(dict(kwargs))
        return KernelCommandOutcome(
            str(kwargs["command_id"]),
            "SUCCEEDED",
            'entry:biaf2pixvqeeyhyvvfxvrss2lq',
            "SUBAGENT_COMPLETION_DELIVERED",
            "Accepted.",
        )

    async def accept_job_result(self, **kwargs) -> KernelCommandOutcome:
        self.accepted_job_results.append(dict(kwargs))
        return KernelCommandOutcome(
            str(kwargs["command_id"]),
            "SUCCEEDED",
            'entry:a6gxugngkq34rwijeporafg5wa',
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
        attachment_id='attachment:hmfv6qyb4msxxwadjyltzzsdse',
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
                consumed_entry_id='entry:33fywnuzce5p2tabisbr3jpo54',
            )

    state = _state(role=wire.ATTACHMENT_ROLE_OBSERVER)
    state.protocol_reader = _QueueTransitionReader()  # type: ignore[assignment]
    response = asyncio.run(_server()._read_content(
        state,
        wire.ReadContentRequest(
            request_id='request:rubfgsxs65b5kypojab732lzpi',
            queue_item_id='queue:q4uhehaen5a547mkh7z2blvzcy',
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
    interactions = _PresentationNoticeInteractions('attachment:7cx2vyhggdz6wnr5qmqinptwwq')
    host._interactions = interactions
    host._presentation_notices = {}

    host._offer_presentation_notice("one-shot notice")
    assert host.take_presentation_notices('attachment:3h2oezl4vkvtofmr7q26bpnzhe') == ()
    assert host.take_presentation_notices('attachment:7cx2vyhggdz6wnr5qmqinptwwq') == ("one-shot notice",)
    assert host.take_presentation_notices('attachment:7cx2vyhggdz6wnr5qmqinptwwq') == ()

    host._offer_presentation_notice("must not replay after reconnect")
    asyncio.run(host.controller_detached('attachment:7cx2vyhggdz6wnr5qmqinptwwq'))
    interactions.controller_id = 'attachment:yuo3m5zj6thkehrqhokqso6hiq'
    assert host.take_presentation_notices('attachment:yuo3m5zj6thkehrqhokqso6hiq') == ()
    assert host._presentation_notices == {}


@pytest.mark.parametrize("owner", [None, 'attachment:uwzuz6lym7rdkwiyxdh6riocgq'])
def test_capacity_rejects_detached_or_replaced_controller(owner) -> None:
    async def exercise() -> None:
        server = _server()
        state = _state(role=wire.ATTACHMENT_ROLE_CONTROLLER)
        await server._command(state, wire.CommandRequest(
            request_id="detach", command_id='command:w2uvqwrrr7pmlv5xu3a4sj47h4', command_kind=wire.DETACH,
        ))
        state.host_session.controller_id = owner
        response = await server._subagent_capacity(state, wire.SubagentCapacityRequest(
            request_id="capacity", expected_session_id='session:336wrfwkamrr3up6kjtsgvgcte',
            expected_host_session_id='host:yinyxklzunar54s7objjdbphcy', target=2,
        ))
        # The fake host deliberately has no setter: revoked authority must never reach it.
        assert response.error.stable_code == "CONTROL_UNAVAILABLE"

    asyncio.run(exercise())


def test_capacity_reports_stale_writer_as_owner_unavailable() -> None:
    state = _state(role=wire.ATTACHMENT_ROLE_CONTROLLER)

    async def stale(target):
        raise StaleHostWriter("writer changed")

    state.host_session.set_subagent_capacity = stale
    response = asyncio.run(_server()._subagent_capacity(state, wire.SubagentCapacityRequest(
        request_id="capacity", expected_session_id='session:336wrfwkamrr3up6kjtsgvgcte',
        expected_host_session_id='host:yinyxklzunar54s7objjdbphcy', target=2,
    )))
    assert response.error.stable_code == "OWNER_UNAVAILABLE"


def test_stage2_observer_cannot_mutate_but_can_detach() -> None:
    server = _server()
    observer = _state(role=wire.ATTACHMENT_ROLE_OBSERVER)
    rejected = asyncio.run(
        server._command(
            observer,
            wire.CommandRequest(
                request_id='request:ada6trh7kkouspbvtb5xziozey',
                command_id='command:fatsxsbdgdroqnbx53epulaaoy',
                client_submission_id='command:fatsxsbdgdroqnbx53epulaaoy',
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
                request_id='request:43gvauqn3ak5wx6fxhxwqetrye',
                command_id='command:k5dgmaolc3lyo4a6zo344ax2ey',
                client_submission_id='command:k5dgmaolc3lyo4a6zo344ax2ey',
                command_kind=wire.DETACH,
            ),
        )
    )
    assert detached.command_outcome.status == wire.SUCCEEDED


@pytest.mark.parametrize(
    ("command_kind", "target_field", "target_id", "record_name"),
    (
        (wire.STOP_ACTIVE_TURN, "target_turn_id", 'turn:74y74rvr2pht5lydgxvy57nxgq', "stop_requests"),
        (
            wire.CANCEL_SUBAGENT_TASK,
            "subagent_task_id",
            'task:r3npg4w7oybo2df2s2il3y5nay',
            "cancel_requests",
        ),
        (
            wire.TERMINATE_BACKGROUND_PROCESS,
            "target_process_id",
            'process:ntcpufb7iult4c2nzqn4ksxvc4',
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
        request_id=derive_id("request", "test-fixture", f"request:{target_id}"),
        command_id='command:wm7mqtvoxp5oecp2gof4o25zna',
        command_kind=command_kind,
        control_admission_deadline_ms=9000,
        expected_session_id='session:336wrfwkamrr3up6kjtsgvgcte',
        expected_host_session_id='host:yinyxklzunar54s7objjdbphcy',
        **{target_field: target_id},
    )

    response = asyncio.run(server._command(controller, request))

    assert response.command_outcome.status == wire.PENDING
    assert response.command_outcome.user_control.target.target_id == target_id
    calls = getattr(controller.host_session, record_name)
    assert calls == [
        {
            "command_id": request.command_id,
            "admission_deadline_ms": request.control_admission_deadline_ms,
            "expected_session_id": 'session:336wrfwkamrr3up6kjtsgvgcte',
            "expected_host_session_id": 'host:yinyxklzunar54s7objjdbphcy',
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
                request_id='request:xqx7ysciebctelkreuupwfgcw4',
                command_id=(
                    'command:efiyh3atcokabxjkzxktpveilm'
                ),
                command_kind=wire.STOP_ACTIVE_TURN,
                expected_session_id='session:336wrfwkamrr3up6kjtsgvgcte',
                expected_host_session_id='host:yinyxklzunar54s7objjdbphcy',
            ),
        )
    )

    assert response.error.stable_code == "STOP_REQUEST_INVALID"
    assert controller.host_session.stop_requests == []


def test_pr03_control_query_requires_controller_and_exact_expectation() -> None:
    request = wire.QueryCommandRequest(
        request_id='request:hfvmrq7rc5ckhytozvtakc7xdm',
        command_id='command:lcdc7gb7ey5hf4qnilnjxgz4nq',
        expected_control=wire.ExpectedUserControl(
            operation=wire.USER_CONTROL_TERMINATE_BACKGROUND_PROCESS,
            session_id='session:336wrfwkamrr3up6kjtsgvgcte',
            host_session_id='host:yinyxklzunar54s7objjdbphcy',
            target=wire.UserControlTarget(
                kind=wire.USER_CONTROL_BACKGROUND_PROCESS,
                target_id='process:72qov6ocdngiw5hv6gjo4g2o7i',
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
            'session:336wrfwkamrr3up6kjtsgvgcte',
            'host:yinyxklzunar54s7objjdbphcy',
            UserControlTarget(
                UserControlTargetKind.BACKGROUND_PROCESS,
                'process:72qov6ocdngiw5hv6gjo4g2o7i',
            ),
         admission_deadline_ms=request.expected_control.admission_deadline_ms)
    ]


def test_pr03_observer_can_page_background_processes_and_read_exact_log() -> None:
    observer = _state(role=wire.ATTACHMENT_ROLE_OBSERVER)
    server = _server()
    first = asyncio.run(
        server._list_background_processes(
            observer,
            wire.ListBackgroundProcessesRequest(
                request_id='request:egjiwtdjb33g2hz5eieldm2r6q',
                expected_session_id='session:336wrfwkamrr3up6kjtsgvgcte',
                expected_host_session_id='host:yinyxklzunar54s7objjdbphcy',
                maximum_items=1,
            ),
        )
    )
    assert [item.process_id for item in first.background_processes.processes] == [
        'process:72qov6ocdngiw5hv6gjo4g2o7i'
    ]
    assert first.background_processes.next_cursor

    second = asyncio.run(
        server._list_background_processes(
            observer,
            wire.ListBackgroundProcessesRequest(
                request_id='request:ryqptzn7xuverjxj7dz7qxj5ou',
                expected_session_id='session:336wrfwkamrr3up6kjtsgvgcte',
                expected_host_session_id='host:yinyxklzunar54s7objjdbphcy',
                cursor=first.background_processes.next_cursor,
                maximum_items=1,
            ),
        )
    )
    assert [item.process_id for item in second.background_processes.processes] == [
        'process:evw46zjzwyi55v7ihcmnj7p5vm'
    ]
    assert not second.background_processes.next_cursor

    log = asyncio.run(
        server._read_background_process_log(
            observer,
            wire.ReadBackgroundProcessLogRequest(
                request_id='request:nimz2bgt6inzle5b4bit2rjyyu',
                expected_session_id='session:336wrfwkamrr3up6kjtsgvgcte',
                expected_host_session_id='host:yinyxklzunar54s7objjdbphcy',
                process_id='process:72qov6ocdngiw5hv6gjo4g2o7i',
                output_cursor="cursor:1",
                max_output_chars=512,
            ),
        )
    )
    assert log.background_process_log.process.process_id == 'process:72qov6ocdngiw5hv6gjo4g2o7i'
    assert log.background_process_log.output == "exact output\n"
    assert log.background_process_log.output_cursor == "cursor:2"

    wrong_owner = asyncio.run(
        server._list_background_processes(
            observer,
            wire.ListBackgroundProcessesRequest(
                request_id='request:6crypxm42qesk2gpwb5igsxvh4',
                expected_session_id='session:336wrfwkamrr3up6kjtsgvgcte',
                expected_host_session_id='host:r77neyfksldterpdnoirnf7zxu',
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
                    request_id='request:ada6trh7kkouspbvtb5xziozey',
                    command_id='command:fatsxsbdgdroqnbx53epulaaoy',
                    client_submission_id='command:fatsxsbdgdroqnbx53epulaaoy',
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
                request_id='request:yd45emjdqejd7k4o4grb45xi3m',
                command_id='command:phj426c6i6nucnncfbxhn4rqki',
                client_submission_id='command:phj426c6i6nucnncfbxhn4rqki',
                command_kind=wire.SUBMIT_PROMPT,
                prompt_content=prompt,
                requested_permission_mode=wire.PERMISSION_MODE_READ_ONLY,
            ),
        )
    )
    assert result.command_outcome.status == wire.PENDING
    [(command_id, content)] = controller.host_session.submitted
    assert command_id == 'command:phj426c6i6nucnncfbxhn4rqki'
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
                request_id='request:dm4o2yxgunao5ha7enfiyq4fgm',
                command_id='command:m4pqugrlircvu5iglncjlv6f54',
                client_submission_id='command:m4pqugrlircvu5iglncjlv6f54',
                command_kind=wire.SUBMIT_PROMPT,
                target_turn_id='turn:kezb35l4prynpikloa5x3helji',
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
                request_id='request:b7wr7donnyr772cpotkodjbscu',
                command_id='command:qy6x2pry2q3ac6dixx4eq43ewy',
                client_submission_id='command:qy6x2pry2q3ac6dixx4eq43ewy',
                command_kind=wire.STEER_QUEUED_PROMPT,
                target_queue_item_id='queue:ny2hkezkbzwknov5sviuukewba',
                target_turn_id='turn:kezb35l4prynpikloa5x3helji',
            ),
        )
    )
    assert result.command_outcome.status == wire.PENDING
    assert controller.host_session.steered == [
        ('command:qy6x2pry2q3ac6dixx4eq43ewy', 'queue:ny2hkezkbzwknov5sviuukewba', 'turn:kezb35l4prynpikloa5x3helji')
    ]


def test_stage2_controller_can_deliver_exact_terminal_subagent_task() -> None:
    server = _server()
    controller = _state(role=wire.ATTACHMENT_ROLE_CONTROLLER)
    result = asyncio.run(
        server._command(
            controller,
            wire.CommandRequest(
                request_id='request:25wmzvp5w5rexajepu55jo2vba',
                command_id='command:nvkv7ut5z2kfaz3oalvclv3otm',
                client_submission_id='command:nvkv7ut5z2kfaz3oalvclv3otm',
                command_kind=wire.ACCEPT_SUBAGENT_COMPLETION,
                target_turn_id='turn:b565wrncq4ezfqthehjqni5ilm',
                subagent_task_id='task:itnsbirlmxzeu74pn7keq6zuzi',
            ),
        )
    )
    assert result.command_outcome.status == wire.SUCCEEDED
    assert controller.host_session.accepted_subagent_completions == [
        {
            "command_id": 'command:nvkv7ut5z2kfaz3oalvclv3otm',
            "target_turn_id": 'turn:b565wrncq4ezfqthehjqni5ilm',
            "requested_permission_mode": None,
            "task_id": 'task:itnsbirlmxzeu74pn7keq6zuzi',
            "actor_id": 'attachment:hmfv6qyb4msxxwadjyltzzsdse',
        }
    ]


def test_stage2_controller_can_continue_from_a_completion_in_a_new_root() -> None:
    server = _server()
    controller = _state(role=wire.ATTACHMENT_ROLE_CONTROLLER)
    subagent = asyncio.run(
        server._command(
            controller,
            wire.CommandRequest(
                request_id='request:wwz4inql72b6bri4l3wxvewrea',
                command_id='command:rqy7ytzg4sa5lihnple4riaqje',
                client_submission_id='command:rqy7ytzg4sa5lihnple4riaqje',
                command_kind=wire.ACCEPT_SUBAGENT_COMPLETION,
                subagent_task_id='task:r464bjh2j4nauh7bhw7gjnegqa',
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
                request_id='request:gar3dr2lmgzosopwiz6jozi474',
                command_id='command:k3la3nqi4bh5sumovjn525fbvm',
                client_submission_id='command:k3la3nqi4bh5sumovjn525fbvm',
                command_kind=wire.ACCEPT_SUBAGENT_COMPLETION,
                subagent_task_id='task:qu46oowryrqxxvcctvx3a7jqxm',
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
            return AcceptedEntry('entry:vpv7agjg6bk6d3zu3bspq54g4q', 'turn:b565wrncq4ezfqthehjqni5ilm', 4, 7)

    host = object.__new__(KernelHostSession)
    host._closing = False
    host._deadlines = KernelExecutionDeadlineFactory()
    host._runner = _Runner()

    async def query_command(_: str) -> None:
        return None

    host.query_command = query_command  # type: ignore[method-assign]
    outcome = asyncio.run(
        host.accept_job_result(
            command_id='command:mbelilcycsnqxtuawobumejzw4',
            target_turn_id='turn:b565wrncq4ezfqthehjqni5ilm',
            job_id="job:durable",
            actor_id='attachment:5l6o6wokl63aqnv5llnyf34m6i',
        )
    )
    assert outcome.public_code == "JOB_RESULT_ACCEPTED"
    assert host._runner.kwargs["job_id"] == "job:durable"


def test_stage2_interaction_resolution_is_controller_only_and_exact() -> None:
    server = _server()
    request = wire.ResolveInteractionRequest(
        request_id='request:tbnpz7felib564z7bp36aozwk4',
        command_id='command:b3zfpydtkfbij6o73rwicaqhgi',
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
            "command_id": 'command:b3zfpydtkfbij6o73rwicaqhgi',
            "decision": "ALLOW",
            "actor_id": 'attachment:hmfv6qyb4msxxwadjyltzzsdse',
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
            session_id='session:336wrfwkamrr3up6kjtsgvgcte',
            writer_generation=7,
            writer_owner_id='host:yinyxklzunar54s7objjdbphcy',
        )
        owner = SessionLiveControlOwner(session_id='session:336wrfwkamrr3up6kjtsgvgcte', owner_epoch=7)
        coordinator = KernelInteractionCoordinator(
            repository=repository,  # type: ignore[arg-type]
            guard=guard,
            live_control=owner,
            live_bus=LiveAgentEventBus(),
            io_owner=KernelSessionIO(),
        )
        assert await coordinator.attach_controller('attachment:dqozinnq4ir4wm3cz5udiaqdoa')
        waiter = asyncio.create_task(
            coordinator.request_tool_confirmation(
                turn_id='turn:nzc36cttgo7xxki7bcjc7ops7a',
                assistant_entry_id='entry:5qidalqvkujk5ykzzzfpwie2qy',
                tool_call_id="call:1",
                tool_name="terminal",
                permission_snapshot=build_run_permission_snapshot(
                    snapshot_id='permission:53mvfpeaityebpvslz3hltgfuy',
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
                command_id='command:l6ug6fwppwxcmubad4kn33wfpq',
                decision="ALLOW",
                actor_id='attachment:dqozinnq4ir4wm3cz5udiaqdoa',
            )
        accepted = await coordinator.resolve_tool_interaction(
            expected_writer_generation=7,
            expected_owner_epoch=7,
            expected_live_revision=snapshot.revision,
            interaction_id=snapshot.current_interaction.interaction_id,
            command_id='command:4yfrkxxcf4kswqgydasrcvmrxq',
            decision="ALLOW",
            actor_id='attachment:dqozinnq4ir4wm3cz5udiaqdoa',
        )
        resolution = await waiter
        assert accepted.attempt_id == resolution.attempt_id
        assert owner.current_snapshot().current_interaction is None
        assert len(repository.calls) == 1
        await coordinator.aclose()

    asyncio.run(exercise())


def test_stage2_live_control_snapshot_and_cursor_are_linearized() -> None:
    owner = SessionLiveControlOwner(
        session_id='session:336wrfwkamrr3up6kjtsgvgcte', maximum_events=2, maximum_public_bytes=4096
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
    valid = dict(command_id='command:xbla77mpykdtojsv4k2diu32o4', command_kind=wire.CANCEL_QUEUED_PROMPT,
                 target_queue_item_id='queue:ny2hkezkbzwknov5sviuukewba')
    accepted = asyncio.run(server._command(controller, wire.CommandRequest(**valid)))
    assert accepted.command_outcome.status == wire.SUCCEEDED
    assert accepted.command_outcome.public_code == 'PROMPT_CANCELLED'
    assert asyncio.run(server._command(observer, wire.CommandRequest(**valid))).error.stable_code == 'CONTROLLER_REQUIRED'
    for changed, code in [
        ({'plan_reason': 'must not resend body'}, 'QUEUE_ACTION_INVALID'),
        ({'target_turn_id': 'turn:c3fxjnnljiejzzrzacfi4f3jkq'}, 'QUEUE_ACTION_INVALID'),
        ({'target_queue_item_id': ''}, 'QUEUE_ACTION_INVALID'),
        ({'requested_permission_mode': wire.PERMISSION_MODE_READ_ONLY}, 'PERMISSION_FIELD_NOT_ALLOWED'),
        ({'command_kind': wire.SUBMIT_PROMPT,
          'prompt_content': _wire_text_prompt('hello')}, 'QUEUE_TARGET_FIELD_NOT_ALLOWED'),
        ({'command_kind': wire.STEER_QUEUED_PROMPT}, 'QUEUE_ACTION_INVALID'),
        ({'expected_session_id': 'another-session'}, 'CONTROL_FIELDS_NOT_ALLOWED'),
    ]:
        result = asyncio.run(server._command(controller, wire.CommandRequest(**(valid | changed))))
        assert result.error.stable_code == code
    assert controller.host_session.steered == [('command:xbla77mpykdtojsv4k2diu32o4', 'queue:ny2hkezkbzwknov5sviuukewba', '')]
    assert wire.STEER_ACTIVE_TURN == 16


def test_direct_steer_is_exact_targeted_and_cannot_carry_permission() -> None:
    async def scenario() -> None:
        server = _server()
        state = _state(role=wire.ATTACHMENT_ROLE_CONTROLLER)
        received = []

        async def steer(**kwargs):
            received.append(kwargs)
            return KernelCommandOutcome(kwargs['command_id'], 'PENDING', '', 'PROMPT_QUEUED', 'Queued.')

        state.host_session.steer_active_turn = steer
        request = wire.CommandRequest(
            request_id='request:ada6trh7kkouspbvtb5xziozey',
            command_id='command:fatsxsbdgdroqnbx53epulaaoy',
            command_kind=wire.STEER_ACTIVE_TURN,
            target_turn_id='turn:qc5obgzujf23avn6iwnfufogvq',
            prompt_content=_wire_text_prompt('keep this input'),
        )
        result = await server._command(state, request)
        assert result.command_outcome.status == wire.PENDING
        assert received[0]['target_turn_id'] == request.target_turn_id
        assert received[0]['content'].parts[0].text == 'keep this input'
        request.requested_permission_mode = wire.PERMISSION_MODE_BYPASS_PERMISSIONS
        rejected = await server._command(state, request)
        assert rejected.error.stable_code == 'PERMISSION_FIELD_NOT_ALLOWED'
        assert len(received) == 1
        request.requested_permission_mode = wire.PERMISSION_MODE_UNSPECIFIED
        request.target_turn_id = ''
        rejected = await server._command(state, request)
        assert rejected.error.stable_code == 'STEER_INVALID'
        assert len(received) == 1
        assert state.host_session.has_controller_attachment(state.attachment_id)
    asyncio.run(scenario())
