from __future__ import annotations

import asyncio
from dataclasses import FrozenInstanceError
from pathlib import Path

import pytest

from pulsara_agent.capability.builtin_catalog import builtin_tool_catalog_entry
from pulsara_agent.conversation_kernel.live import LiveAgentEventBus
from pulsara_agent.conversation_kernel.execution_watchdogs import (
    KernelExecutionDeadlineFactory,
)
from pulsara_agent.conversation_kernel.host import (
    KernelCommandOutcome,
    KernelHostSession,
)
from pulsara_agent.conversation_kernel.plan_runtime import (
    ContinuationAdmissionOwner,
    KernelPlanInteractionCoordinator,
)
from pulsara_agent.conversation_kernel.repository import (
    AcceptedPlanResolution,
    ConversationKernelConflict,
    PlanQuestionAnswer,
    plan_draft_review_semantic_candidate,
    plan_question_resolution_semantic_fingerprint,
)
from pulsara_agent.conversation_kernel.tool_policy import (
    DefaultToolDispatchAuthorizationPolicy,
)
from pulsara_agent.conversation_kernel.tool_runtime import DirectKernelToolPort
from pulsara_agent.conversation_kernel.vocabulary import (
    APPEND_GUARDS,
    COMMITTED_EVENT_DESCRIPTORS,
    LIVE_EVENT_TYPES,
    SUBJECT_SLOTS,
)
from pulsara_agent.model_input.contracts import ModelInputScopeKind
from pulsara_agent.primitives.context import freeze_json
from pulsara_agent.primitives.permission import (
    PERMISSION_PRESET_CONTRACT_FINGERPRINT,
    PERMISSION_PRESET_CONTRACT_ID,
    PermissionMode,
    preset_permission_payload,
)
from pulsara_agent.primitives.plan_workflow import (
    PLAN_INTERACTION_CONTRACTS,
    PlanDraftDecision,
    PlanInteractionBinding,
    PlanQuestionAnswerKind,
    PlanWorkflowStatus,
    extract_plan_draft,
    extract_plan_question,
    read_plan_draft_chunk,
)
from pulsara_agent.primitives.run_permission import (
    RunPermissionAdmissionSource,
    RunPermissionOverlay,
    build_run_permission_snapshot,
)
from pulsara_agent.storage.migrations.manifest import CONVERSATION_KERNEL_RELATIONS
from pulsara_agent.terminal_protocol.generated_v3 import terminal_kernel_v3_pb2 as wire
from pulsara_agent.terminal_protocol.v3_gateway import (
    TerminalKernelProtocolServer,
    _Connection,
    _outcome_to_wire,
)


def _binding(tool_name: str) -> PlanInteractionBinding:
    contract = builtin_tool_catalog_entry(tool_name).binding_contract.base
    return PlanInteractionBinding(
        contract.contract_id,
        contract.contract_version,
        contract.binding_fingerprint,
    )


def _accepted_resolution(interaction_id: str) -> AcceptedPlanResolution:
    return AcceptedPlanResolution(
        command_id="command:answer",
        workflow_id="workflow:one",
        workflow_status=PlanWorkflowStatus.ACTIVE,
        interaction_id=interaction_id,
        interaction_status="ANSWERED",
        resume_permission_mode=PermissionMode.ACCEPT_EDITS,
        continuation_turn_id=None,
        continuation_entry_id=None,
        handoff_created_at_commit=False,
        workflow_revision=3,
        question_result_entry_id="entry:answer",
    )


def test_round4_closed_oracle_and_permission_presets() -> None:
    assert len(COMMITTED_EVENT_DESCRIPTORS) == 31
    assert len(LIVE_EVENT_TYPES) == 23
    assert len(SUBJECT_SLOTS) == 13
    assert len(APPEND_GUARDS) == 2
    assert len(CONVERSATION_KERNEL_RELATIONS) == 25
    assert set(PLAN_INTERACTION_CONTRACTS.values()) == {
        "ask_plan_question",
        "exit_plan",
    }
    assert PERMISSION_PRESET_CONTRACT_ID == "pulsara.permission-presets.v1"
    assert PERMISSION_PRESET_CONTRACT_FINGERPRINT.startswith("sha256:")
    for mode in PermissionMode:
        first = preset_permission_payload(mode)
        second = preset_permission_payload(mode)
        assert first == second
        assert first is not second


def test_round4_permission_snapshot_is_immutable_and_plan_only_narrows() -> None:
    snapshot = build_run_permission_snapshot(
        snapshot_id="permission:plan",
        requested_mode=PermissionMode.BYPASS_PERMISSIONS,
        effective_mode=PermissionMode.READ_ONLY,
        admission_source=RunPermissionAdmissionSource.USER_SUBMISSION,
        overlay=RunPermissionOverlay.PLAN_READ_ONLY,
        plan_context_ordinal_at_admission=2,
        plan_workflow_id="workflow:two",
        plan_workflow_revision_at_admission=5,
    )
    assert snapshot.requested_mode is PermissionMode.BYPASS_PERMISSIONS
    assert snapshot.effective_mode is PermissionMode.READ_ONLY
    with pytest.raises(FrozenInstanceError):
        snapshot.effective_mode = PermissionMode.BYPASS_PERMISSIONS  # type: ignore[misc]
    with pytest.raises(ValueError, match="force read-only"):
        build_run_permission_snapshot(
            snapshot_id="permission:invalid",
            requested_mode=PermissionMode.BYPASS_PERMISSIONS,
            effective_mode=PermissionMode.BYPASS_PERMISSIONS,
            admission_source=RunPermissionAdmissionSource.USER_SUBMISSION,
            overlay=RunPermissionOverlay.PLAN_READ_ONLY,
            plan_context_ordinal_at_admission=2,
            plan_workflow_id="workflow:two",
            plan_workflow_revision_at_admission=5,
        )
    with pytest.raises(ValueError, match="without an overlay"):
        build_run_permission_snapshot(
            snapshot_id="permission:amplified",
            requested_mode=PermissionMode.READ_ONLY,
            effective_mode=PermissionMode.BYPASS_PERMISSIONS,
            admission_source=RunPermissionAdmissionSource.USER_SUBMISSION,
            overlay=RunPermissionOverlay.NONE,
            plan_context_ordinal_at_admission=0,
        )


def test_round4_central_question_and_draft_extractor_owns_utf8_identity() -> None:
    question = extract_plan_question(
        interaction_id="interaction:question",
        binding=_binding("ask_plan_question"),
        arguments=freeze_json(
            {
                "allow_free_text": True,
                "options": [
                    {
                        "label": "保守",
                        "description": "保持兼容",
                        "recommended": True,
                    },
                    {
                        "label": "直接",
                        "description": "hard cut",
                        "recommended": False,
                    },
                ],
                "question": "请选择实现路径？",
            }
        ),  # type: ignore[arg-type]
    )
    assert question.options[0].ordinal == 0
    assert question.options[0].recommended
    assert question.typed_content_fingerprint.startswith("sha256:")

    body = "第一行🙂\n第二行\\n不是转义\n第三行"
    draft = extract_plan_draft(
        interaction_id="interaction:draft",
        assistant_entry_id="entry:assistant",
        tool_call_id="call:exit",
        binding=_binding("exit_plan"),
        request_semantic_digest="sha256:" + "1" * 64,
        arguments=freeze_json({"summary": "摘要", "plan": body}),  # type: ignore[arg-type]
    )
    assert draft.exact_plan_utf8 == body.encode("utf-8")
    chunk = read_plan_draft_chunk(draft, offset_utf8_bytes=0, limit_bytes=9)
    assert chunk.body.encode("utf-8") == draft.exact_plan_utf8[: len(chunk.body.encode("utf-8"))]
    assert chunk.next_offset_utf8_bytes > 0
    with pytest.raises(ValueError, match="boundary"):
        read_plan_draft_chunk(draft, offset_utf8_bytes=10, limit_bytes=8)


def test_round4_question_recommendation_is_optional_but_at_most_one() -> None:
    binding = _binding("ask_plan_question")
    arguments = {
        "question": "Choose one",
        "options": [
            {"label": "A", "recommended": False},
            {"label": "B"},
        ],
        "allow_free_text": True,
    }
    question = extract_plan_question(
        interaction_id="interaction:no-recommendation",
        binding=binding,
        arguments=freeze_json(arguments),  # type: ignore[arg-type]
    )
    assert not any(option.recommended for option in question.options)

    arguments["options"] = [
        {"label": "A", "recommended": True},
        {"label": "B", "recommended": True},
    ]
    with pytest.raises(ValueError, match="at most one recommendation"):
        extract_plan_question(
            interaction_id="interaction:multiple-recommendations",
            binding=binding,
            arguments=freeze_json(arguments),  # type: ignore[arg-type]
        )


def test_round4_question_waiter_accepts_resolution_before_publish_and_await() -> None:
    async def scenario() -> None:
        coordinator = KernelPlanInteractionCoordinator()
        waiter = await coordinator.prepare_question(
            interaction_id="interaction:question", origin_turn_id="turn:origin"
        )
        resolution = _accepted_resolution(waiter.interaction_id)
        assert await coordinator.settle(
            interaction_id=waiter.interaction_id, resolution=resolution
        )
        question = extract_plan_question(
            interaction_id=waiter.interaction_id,
            binding=_binding("ask_plan_question"),
            arguments=freeze_json(
                {"question": "继续吗？", "options": [], "allow_free_text": True}
            ),  # type: ignore[arg-type]
        )
        await coordinator.publish_open(waiter, question)
        assert await coordinator.wait(waiter) == resolution
        assert await coordinator.current_open() is None
        await coordinator.aclose()

    asyncio.run(scenario())


def test_round4_question_waiter_caller_cancel_only_detaches() -> None:
    async def scenario() -> None:
        coordinator = KernelPlanInteractionCoordinator()
        waiter = await coordinator.prepare_question(
            interaction_id="interaction:question", origin_turn_id="turn:origin"
        )
        waiting = asyncio.create_task(coordinator.wait(waiter))
        await asyncio.sleep(0)
        waiting.cancel()
        with pytest.raises(asyncio.CancelledError):
            await waiting
        resolution = _accepted_resolution(waiter.interaction_id)
        assert await coordinator.settle(
            interaction_id=waiter.interaction_id, resolution=resolution
        )
        assert await coordinator.wait(waiter) == resolution
        await coordinator.aclose()

    asyncio.run(scenario())


def test_round4_continuation_owner_survives_waiter_cancel_and_close_drains() -> None:
    async def scenario() -> None:
        owner = ContinuationAdmissionOwner()
        release = asyncio.Event()
        completed = asyncio.Event()

        async def admit() -> object:
            await release.wait()
            completed.set()
            return "FULL"

        attempt = owner.start(
            attempt_id="attempt:one",
            turn_id="turn:successor",
            semantic_candidate_fingerprint="sha256:" + "1" * 64,
            run=admit,
        )
        waiter = asyncio.ensure_future(asyncio.shield(attempt.task))
        waiter.cancel()
        with pytest.raises(asyncio.CancelledError):
            await waiter
        assert not attempt.task.done()
        closing = asyncio.create_task(owner.aclose())
        await asyncio.sleep(0)
        assert not closing.done()
        release.set()
        await closing
        assert completed.is_set()
        assert attempt.task.result() == "FULL"

    asyncio.run(scenario())


def test_round4_plan_resolution_attempts_reject_semantic_aliases() -> None:
    question_first = plan_question_resolution_semantic_fingerprint(
        workflow_id="workflow:one",
        expected_workflow_revision=3,
        interaction_id="interaction:question",
        answer=PlanQuestionAnswer(
            PlanQuestionAnswerKind.OPTION, option_ordinal=0
        ),
        result_id="result:question",
        result_entry_id="entry:question",
    )
    question_conflict = plan_question_resolution_semantic_fingerprint(
        workflow_id="workflow:one",
        expected_workflow_revision=3,
        interaction_id="interaction:question",
        answer=PlanQuestionAnswer(
            PlanQuestionAnswerKind.OPTION, option_ordinal=1
        ),
        result_id="result:question",
        result_entry_id="entry:question",
    )
    _, _, decision_first = plan_draft_review_semantic_candidate(
        workflow_id="workflow:one",
        expected_workflow_revision=4,
        interaction_id="interaction:draft",
        decision=PlanDraftDecision.APPROVE,
        feedback=None,
        continuation_turn_id="turn:review",
        continuation_entry_id="entry:review",
        continuation_context_binding_revision_id="revision:review",
    )
    _, _, decision_conflict = plan_draft_review_semantic_candidate(
        workflow_id="workflow:one",
        expected_workflow_revision=4,
        interaction_id="interaction:draft",
        decision=PlanDraftDecision.REVISE,
        feedback=None,
        continuation_turn_id="turn:review",
        continuation_entry_id="entry:review",
        continuation_context_binding_revision_id="revision:review",
    )
    _, _, feedback_first = plan_draft_review_semantic_candidate(
        workflow_id="workflow:one",
        expected_workflow_revision=4,
        interaction_id="interaction:draft",
        decision=PlanDraftDecision.REVISE,
        feedback="first",
        continuation_turn_id="turn:review",
        continuation_entry_id="entry:review",
        continuation_context_binding_revision_id="revision:review",
    )
    _, _, feedback_conflict = plan_draft_review_semantic_candidate(
        workflow_id="workflow:one",
        expected_workflow_revision=4,
        interaction_id="interaction:draft",
        decision=PlanDraftDecision.REVISE,
        feedback="second",
        continuation_turn_id="turn:review",
        continuation_entry_id="entry:review",
        continuation_context_binding_revision_id="revision:review",
    )

    async def scenario(first: str, conflict: str) -> None:
        owner = ContinuationAdmissionOwner()
        release = asyncio.Event()

        async def admit() -> object:
            await release.wait()
            return "FULL"

        attempt = owner.start(
            attempt_id="attempt:shared-command",
            turn_id="turn:target",
            semantic_candidate_fingerprint=first,
            run=admit,
        )
        with pytest.raises(ConversationKernelConflict, match="semantic identity"):
            owner.start(
                attempt_id="attempt:shared-command",
                turn_id="turn:target",
                semantic_candidate_fingerprint=conflict,
                run=admit,
            )
        release.set()
        await attempt.task
        await owner.aclose()

    for first, conflict in (
        (question_first, question_conflict),
        (decision_first, decision_conflict),
        (feedback_first, feedback_conflict),
    ):
        asyncio.run(scenario(first, conflict))


def test_round4_question_option_ordinal_rejects_negative_python_input() -> None:
    with pytest.raises(ValueError, match="union"):
        PlanQuestionAnswer(PlanQuestionAnswerKind.OPTION, option_ordinal=-1)


def test_round4_root_plan_tools_are_absent_from_subagent_surface(tmp_path: Path) -> None:
    async def scenario() -> None:
        port = DirectKernelToolPort(
            workspace_root=tmp_path,
            host_owner_id="host:round4",
            session_id="session:round4",
            live_bus=LiveAgentEventBus(),
            authorization_policy=DefaultToolDispatchAuthorizationPolicy(),
        )
        try:
            root = port.snapshot_tool_surface(
                conversation_scope_kind=ModelInputScopeKind.ROOT,
                scope_subagent_task_id=None,
            )
            child = port.snapshot_tool_surface(
                conversation_scope_kind=ModelInputScopeKind.SUBAGENT_TASK,
                scope_subagent_task_id="task:child",
            )
            plan_names = {"enter_plan", "ask_plan_question", "exit_plan"}
            assert plan_names <= {item.name for item in root.model_surface.tool_specs}
            assert not plan_names & {
                item.name for item in child.model_surface.tool_specs
            }
        finally:
            await port.aclose()

    asyncio.run(scenario())


def test_round4_protocol_uses_true_question_oneof_and_feedback_presence() -> None:
    option = wire.PlanQuestionAnswer(option_ordinal=0)
    assert option.WhichOneof("answer") == "option_ordinal"
    assert option.option_ordinal == 0
    free = wire.PlanQuestionAnswer(free_text="custom")
    assert free.WhichOneof("answer") == "free_text"
    empty = wire.PlanQuestionAnswer()
    assert empty.WhichOneof("answer") is None

    missing = wire.PlanDraftResolution(decision=wire.PLAN_DRAFT_REVISE)
    present_empty = wire.PlanDraftResolution(
        decision=wire.PLAN_DRAFT_REVISE, feedback=""
    )
    assert not missing.HasField("feedback")
    assert present_empty.HasField("feedback")
    assert present_empty.feedback == ""


class _PlanProtocolHost:
    writer_generation = 7

    def __init__(self) -> None:
        self.questions: list[object] = []
        self.drafts: list[tuple[PlanDraftDecision, str | None]] = []
        self._question_winners: dict[str, tuple[object, AcceptedPlanResolution]] = {}
        self._draft_winners: dict[
            str, tuple[tuple[PlanDraftDecision, str | None], AcceptedPlanResolution]
        ] = {}
        self.controller_attachment_id = "attachment:controller"

    def has_controller_attachment(self, attachment_id: str) -> bool:
        return attachment_id == self.controller_attachment_id

    async def resolve_plan_question(self, **kwargs: object) -> AcceptedPlanResolution:
        command_id = str(kwargs["command_id"])
        answer = kwargs["answer"]
        winner = self._question_winners.get(command_id)
        if winner is not None:
            if winner[0] != answer:
                raise ConversationKernelConflict("Plan question command conflicts")
            return winner[1]
        if kwargs["write_expected_writer_generation"] != self.writer_generation:
            raise ConversationKernelConflict("Plan resolution writer generation is stale")
        self.questions.append(answer)
        accepted = _accepted_resolution(str(kwargs["interaction_id"]))
        self._question_winners[command_id] = (answer, accepted)
        return accepted

    async def resolve_plan_draft_review(
        self, **kwargs: object
    ) -> AcceptedPlanResolution:
        decision = kwargs["decision"]
        feedback = kwargs["feedback"]
        assert isinstance(decision, PlanDraftDecision)
        assert feedback is None or isinstance(feedback, str)
        command_id = str(kwargs["command_id"])
        candidate = (decision, feedback)
        winner = self._draft_winners.get(command_id)
        if winner is not None:
            if winner[0] != candidate:
                raise ConversationKernelConflict("Plan draft command conflicts")
            return winner[1]
        if kwargs["write_expected_writer_generation"] != self.writer_generation:
            raise ConversationKernelConflict("Plan resolution writer generation is stale")
        self.drafts.append(candidate)
        accepted = AcceptedPlanResolution(
            command_id=str(kwargs["command_id"]),
            workflow_id=str(kwargs["workflow_id"]),
            workflow_status=PlanWorkflowStatus.ACTIVE,
            interaction_id=str(kwargs["interaction_id"]),
            interaction_status="REVISION_REQUESTED",
            resume_permission_mode=PermissionMode.ACCEPT_EDITS,
            continuation_turn_id="turn:revision",
            continuation_entry_id="entry:revision",
            handoff_created_at_commit=False,
            workflow_revision=4,
            draft_decision=PlanDraftDecision.REVISE,
        )
        self._draft_winners[command_id] = (candidate, accepted)
        return accepted


def _plan_protocol_state() -> tuple[TerminalKernelProtocolServer, _Connection]:
    host = _PlanProtocolHost()
    return (
        TerminalKernelProtocolServer(
            socket_path=Path("/tmp/pulsara-round4-protocol-test.sock"),
            session_provider=lambda _: host,  # type: ignore[arg-type]
        ),
        _Connection(
            attachment_id="attachment:controller",
            attachment_generation=1,
            host_session=host,  # type: ignore[arg-type]
            granted_role=wire.ATTACHMENT_ROLE_CONTROLLER,
            authenticated=True,
        ),
    )


def _plan_resolution_request(
    resolution: object,
    *,
    writer_generation: int = 7,
) -> wire.ResolvePlanInteractionRequest:
    request = wire.ResolvePlanInteractionRequest(
        request_id="request:plan",
        command_id="command:plan",
        attempt_expected_writer_generation=writer_generation,
        interaction_id="interaction:plan",
        workflow_id="workflow:one",
        expected_workflow_revision=3,
    )
    if isinstance(resolution, wire.PlanQuestionAnswer):
        request.question_answer.CopyFrom(resolution)
    elif isinstance(resolution, wire.PlanDraftResolution):
        request.draft.CopyFrom(resolution)
    else:
        raise TypeError("unsupported test resolution")
    return request


def test_round4_protocol_rejects_stale_writer_and_invalid_question_union() -> None:
    server, state = _plan_protocol_state()
    host = state.host_session
    assert isinstance(host, _PlanProtocolHost)

    stale = asyncio.run(
        server._resolve_plan_interaction(
            state,
            _plan_resolution_request(
                wire.PlanQuestionAnswer(option_ordinal=0), writer_generation=6
            ),
        )
    )
    assert stale.error.stable_code == "PLAN_RESOLUTION_CONFLICT"

    for answer in (wire.PlanQuestionAnswer(), wire.PlanQuestionAnswer(free_text="")):
        rejected = asyncio.run(
            server._resolve_plan_interaction(
                state, _plan_resolution_request(answer)
            )
        )
        assert rejected.error.stable_code == "PLAN_QUESTION_ANSWER_INVALID"
    assert host.questions == []


def test_round4_plan_resolution_requires_exact_current_controller_capability() -> None:
    server, state = _plan_protocol_state()
    host = state.host_session
    assert isinstance(host, _PlanProtocolHost)
    host.controller_attachment_id = "attachment:replacement"

    rejected = asyncio.run(
        server._resolve_plan_interaction(
            state,
            _plan_resolution_request(wire.PlanQuestionAnswer(option_ordinal=0)),
        )
    )
    assert rejected.error.stable_code == "CONTROLLER_REQUIRED"
    assert host.questions == []


def test_round4_protocol_feedback_presence_matrix_is_closed() -> None:
    server, state = _plan_protocol_state()
    host = state.host_session
    assert isinstance(host, _PlanProtocolHost)

    for decision in (wire.PLAN_DRAFT_APPROVE, wire.PLAN_DRAFT_CANCEL):
        rejected = asyncio.run(
            server._resolve_plan_interaction(
                state,
                _plan_resolution_request(
                    wire.PlanDraftResolution(decision=decision, feedback="")
                ),
            )
        )
        assert rejected.error.stable_code == "PLAN_DRAFT_FEEDBACK_NOT_ALLOWED"

    for draft in (
        wire.PlanDraftResolution(decision=wire.PLAN_DRAFT_REVISE),
        wire.PlanDraftResolution(decision=wire.PLAN_DRAFT_REVISE, feedback=""),
    ):
        accepted = asyncio.run(
            server._resolve_plan_interaction(
                state, _plan_resolution_request(draft)
            )
        )
        assert accepted.resolve_plan_interaction.draft_decision == wire.PLAN_DRAFT_REVISE
    assert host.drafts == [(PlanDraftDecision.REVISE, None)]


def test_round4_protocol_old_writer_can_query_exact_resolution_winner() -> None:
    server, state = _plan_protocol_state()
    host = state.host_session
    assert isinstance(host, _PlanProtocolHost)
    request = _plan_resolution_request(wire.PlanQuestionAnswer(option_ordinal=0))

    first = asyncio.run(server._resolve_plan_interaction(state, request))
    assert first.resolve_plan_interaction.interaction_id == "interaction:plan"
    host.writer_generation = 8
    retried = asyncio.run(server._resolve_plan_interaction(state, request))

    assert retried.resolve_plan_interaction == first.resolve_plan_interaction
    assert len(host.questions) == 1


def test_round4_ack_unknown_query_preserves_typed_plan_winner() -> None:
    async def scenario() -> KernelCommandOutcome:
        host = object.__new__(KernelHostSession)
        host._command_failures = {}  # type: ignore[attr-defined]

        async def query(_: str) -> dict[str, object]:
            return {
                "command_kind": "RESOLVE_PLAN_INTERACTION",
                "target_plan_interaction_id": "interaction:draft",
                "plan_interaction_kind": "DRAFT_REVIEW",
                "plan_interaction_status": "REVISION_REQUESTED",
                "interaction_workflow_status": "ACTIVE",
                "interaction_resume_permission_mode": "accept-edits",
                "interaction_workflow_revision": 9,
                "plan_continuation_turn_id": "turn:revision",
            }

        host._query_command_row = query  # type: ignore[method-assign]
        outcome = await KernelHostSession.query_command(host, "command:revision")
        assert outcome is not None
        return outcome

    outcome = asyncio.run(scenario())
    assert outcome.plan_draft_decision is PlanDraftDecision.REVISE
    assert outcome.plan_continuation_turn_id == "turn:revision"
    assert not outcome.handoff_created_at_commit
    response = _outcome_to_wire("request:query", outcome)
    assert response.plan_draft_decision == wire.PLAN_DRAFT_REVISE
    assert response.plan_continuation_turn_id == "turn:revision"
    assert response.plan_workflow_revision == 9


def test_round4_existing_draft_winner_precedes_root_slot_reservation() -> None:
    async def scenario() -> None:
        winner = AcceptedPlanResolution(
            command_id="command:approve",
            workflow_id="workflow:one",
            workflow_status=PlanWorkflowStatus.APPROVED,
            interaction_id="interaction:draft",
            interaction_status="APPROVED",
            resume_permission_mode=PermissionMode.ACCEPT_EDITS,
            continuation_turn_id="turn:implementation",
            continuation_entry_id="entry:implementation",
            handoff_created_at_commit=False,
            workflow_revision=4,
            draft_decision=PlanDraftDecision.APPROVE,
        )

        class Repository:
            @staticmethod
            def confirm_plan_draft_review_winner(**_: object) -> AcceptedPlanResolution:
                return winner

        class IO:
            @staticmethod
            async def run(function: object, *args: object, **kwargs: object) -> object:
                assert callable(function)
                return function(*args, **kwargs)  # type: ignore[operator]

        class Guard:
            writer_generation = 8

        class Lease:
            guard = Guard()

        host = object.__new__(KernelHostSession)
        host._closing = False  # type: ignore[attr-defined]
        host._deadlines = KernelExecutionDeadlineFactory()  # type: ignore[attr-defined]
        host._plan_exit_fence = True  # type: ignore[attr-defined]
        host.session_id = "session:one"  # type: ignore[attr-defined]
        host._lease = Lease()  # type: ignore[attr-defined]
        host.repository = Repository()  # type: ignore[attr-defined]
        host._io = IO()  # type: ignore[attr-defined]
        blocker = asyncio.Event()
        active = asyncio.create_task(blocker.wait())
        host._active_task = active  # type: ignore[attr-defined]
        try:
            observed = await KernelHostSession.resolve_plan_draft_review(
                host,
                command_id="command:approve",
                workflow_id="workflow:one",
                expected_workflow_revision=3,
                interaction_id="interaction:draft",
                decision=PlanDraftDecision.APPROVE,
                feedback=None,
                write_expected_writer_generation=7,
            )
            assert observed == winner
            assert not active.done()
        finally:
            active.cancel()
            with pytest.raises(asyncio.CancelledError):
                await active

    asyncio.run(scenario())


def test_round4_force_exit_fence_rejects_every_new_resolution_write() -> None:
    class Repository:
        writes = 0

        @staticmethod
        def confirm_plan_question_winner(**_: object) -> None:
            return None

        @staticmethod
        def confirm_plan_draft_review_winner(**_: object) -> None:
            return None

        @classmethod
        def resolve_plan_question(cls, *_: object, **__: object) -> None:
            cls.writes += 1

        @classmethod
        def resolve_plan_draft_review(cls, *_: object, **__: object) -> None:
            cls.writes += 1

    class IO:
        @staticmethod
        async def run(function: object, *args: object, **kwargs: object) -> object:
            assert callable(function)
            return function(*args, **kwargs)  # type: ignore[operator]

    async def new_host() -> KernelHostSession:
        host = object.__new__(KernelHostSession)
        host._closing = False  # type: ignore[attr-defined]
        host._deadlines = KernelExecutionDeadlineFactory()  # type: ignore[attr-defined]
        host._plan_exit_fence = True  # type: ignore[attr-defined]
        host._external_new_turn_accepting = False  # type: ignore[attr-defined]
        host._active_task = None  # type: ignore[attr-defined]
        host._active_turn_id = None  # type: ignore[attr-defined]
        host._active_command_id = None  # type: ignore[attr-defined]
        host._lock = asyncio.Lock()  # type: ignore[attr-defined]
        host._plan_continuations = ContinuationAdmissionOwner()  # type: ignore[attr-defined]
        host._io = IO()  # type: ignore[attr-defined]
        host.repository = Repository()  # type: ignore[attr-defined]
        host.session_id = "session:one"  # type: ignore[attr-defined]
        return host

    async def scenario() -> None:
        host = await new_host()
        with pytest.raises(ConversationKernelConflict, match="force exit"):
            await KernelHostSession.resolve_plan_question(
                host,
                command_id="command:question",
                workflow_id="workflow:one",
                expected_workflow_revision=3,
                interaction_id="interaction:question",
                answer=PlanQuestionAnswer(
                    PlanQuestionAnswerKind.OPTION, option_ordinal=0
                ),
            )
        await host._plan_continuations.aclose()  # noqa: SLF001

        for decision, feedback in (
            (PlanDraftDecision.APPROVE, None),
            (PlanDraftDecision.REVISE, "revise this"),
            (PlanDraftDecision.CANCEL, None),
        ):
            host = await new_host()
            with pytest.raises(ConversationKernelConflict, match="force exit"):
                await KernelHostSession.resolve_plan_draft_review(
                    host,
                    command_id=f"command:{decision.value}",
                    workflow_id="workflow:one",
                    expected_workflow_revision=4,
                    interaction_id="interaction:draft",
                    decision=decision,
                    feedback=feedback,
                )
            await host._plan_continuations.aclose()  # noqa: SLF001
        assert Repository.writes == 0

    asyncio.run(scenario())
