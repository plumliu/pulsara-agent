"""Plan reviews wait on their running origin and settle only their final result."""
import asyncio
from dataclasses import replace
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from pulsara_agent.conversation_kernel._repository.contracts import (
    AcceptedPlanResolution, PreparedPlanBatchCall, PreparedPlanToolBatch,
    PlanContinuationDisposition, PlanContinuationInspection,
    PlanToolControlKind, plan_draft_review_semantic_candidate,
)
from pulsara_agent.conversation_kernel.host_contracts import RootChainPhase
from pulsara_agent.conversation_kernel.host_plans import _HostPlanOperations
from pulsara_agent.conversation_kernel.plan_runtime import (
    ContinuationAdmissionOwner, KernelPlanInteractionCoordinator,
)
from pulsara_agent.conversation_kernel.prompt_content import freeze_canonical_prompt
from pulsara_agent.conversation_kernel.tool_contracts import build_accepted_canonical_tool_result_settlement
from pulsara_agent.identity import random_id
from pulsara_agent.llm.input import FrozenPromptContent
from pulsara_agent.model_input.contracts import ModelInputScopeKind
from pulsara_agent.primitives.context import freeze_json
from pulsara_agent.primitives.permission import PermissionMode
from pulsara_agent.primitives.plan_workflow import (
    PlanDraftDecision, PlanHandoffKind, PlanWorkflowStatus, extract_plan_draft,
)
from pulsara_agent.primitives.run_permission import (
    RunPermissionAdmissionSource, build_run_permission_snapshot,
)
from pulsara_agent.primitives.tool_observation import ToolObservationOrigin


def settlement():
    return build_accepted_canonical_tool_result_settlement(
        session_id=random_id("session"), scope_kind=ModelInputScopeKind.ROOT,
        turn_id=random_id("turn"), assistant_entry_id=random_id("entry"), call_ordinal=0,
        tool_name="exit_plan", tool_call_id="provider-plan-call",
        public_arguments=freeze_json({"plan": "方案"}), result_id=random_id("result"),
        result_entry_id=random_id("entry"), accepted_entry_sequence=3, result_state="SUCCESS",
        result_origin_kind="PLAN_CONTROL", canonical_body="用户要求修订方案。",
        observed_at=datetime.now(timezone.utc), observation_origin=ToolObservationOrigin.PLAN_CONTROL,
    )


def resolution(decision):
    approved = decision is PlanDraftDecision.APPROVE
    return AcceptedPlanResolution(
        command_id=random_id("command"), workflow_id=random_id("plan"),
        workflow_status=PlanWorkflowStatus.APPROVED if approved else PlanWorkflowStatus.ACTIVE,
        interaction_id=random_id("interaction"),
        interaction_status={PlanDraftDecision.APPROVE: "APPROVED", PlanDraftDecision.REVISE: "REVISION_REQUESTED", PlanDraftDecision.CANCEL: "CANCELLED"}[decision],
        resume_permission_mode=PermissionMode.ACCEPT_EDITS,
        continuation_turn_id=random_id("turn") if approved else None,
        continuation_entry_id=random_id("entry") if approved else None,
        handoff_created_at_commit=False, workflow_revision=2, draft_decision=decision,
        tool_result_settlement=settlement(),
    )


@pytest.mark.parametrize("kind,arguments", [(PlanToolControlKind.QUESTION, {"question": "继续？"}), (PlanToolControlKind.DRAFT, {"plan": "方案"})])
def test_open_interactions_have_no_selected_result(kind, arguments):
    permission = build_run_permission_snapshot(
        snapshot_id=random_id("permission"), requested_mode=PermissionMode.READ_ONLY,
        effective_mode=PermissionMode.READ_ONLY, admission_source=RunPermissionAdmissionSource.USER_SUBMISSION,
    )
    call = PreparedPlanBatchCall(
        random_id("block"), "provider-call", "exit_plan" if kind is PlanToolControlKind.DRAFT else "ask_plan_question",
        freeze_json(arguments), None, None,
    )
    fields = dict(
        session_id=random_id("session"), workspace_id=random_id("workspace"),
        origin_turn_id=random_id("turn"), assistant_entry_id=random_id("entry"),
        selected_call_ordinal=0, control_kind=kind, selected_arguments=call.arguments,
        permission_snapshot=permission, workflow_id=random_id("plan"), expected_workflow_revision=1,
        interaction_id=random_id("interaction"), continuation_turn_id=None, continuation_entry_id=None,
        continuation_context_binding_revision_id=None, calls=(call,), occurred_at=datetime.now(timezone.utc), actor_id="test",
    )
    assert PreparedPlanToolBatch(**fields).calls[0].result_id is None
    with pytest.raises(ValueError, match="cannot preinstall"):
        PreparedPlanToolBatch(**{**fields, "calls": (replace(call, result_id=random_id("result"), result_entry_id=random_id("entry")),)})
    with pytest.raises(ValueError, match="unselected"):
        PreparedPlanToolBatch(**{**fields, "calls": (call, replace(call, tool_call_id="other-call"))})
    with pytest.raises(ValueError, match="continuation union"):
        PreparedPlanToolBatch(**{**fields, "continuation_entry_id": random_id("entry")})


def test_revision_stays_on_current_turn_and_approval_requires_complete_successor():
    fields = dict(
        workflow_id=random_id("plan"), expected_workflow_revision=1, interaction_id=random_id("interaction"),
        decision=PlanDraftDecision.REVISE, canonical_prompt=freeze_canonical_prompt(FrozenPromptContent.text("改这里\r\n")),
        continuation_turn_id=None, continuation_entry_id=None, continuation_context_binding_revision_id=None,
    )
    _, continuation, _ = plan_draft_review_semantic_candidate(**fields)
    assert continuation == (None, None, None)
    with pytest.raises(ValueError, match="continuation union"):
        plan_draft_review_semantic_candidate(**{**fields, "continuation_turn_id": random_id("turn")})
    with pytest.raises(ValueError, match="continuation union"):
        plan_draft_review_semantic_candidate(**{**fields, "decision": PlanDraftDecision.APPROVE, "canonical_prompt": None})


def test_draft_waiter_can_resolve_before_open_publication():
    async def run():
        coordinator = KernelPlanInteractionCoordinator()
        answer = resolution(PlanDraftDecision.REVISE)
        waiter = await coordinator.prepare_interaction(interaction_id=answer.interaction_id, origin_turn_id=answer.tool_result_settlement.turn_id)
        assert waiter.owner_task is asyncio.current_task()
        assert await coordinator.current_waiter(answer.interaction_id) is waiter
        assert await coordinator.settle(interaction_id=answer.interaction_id, resolution=answer)
        draft = extract_plan_draft(
            interaction_id=answer.interaction_id, assistant_entry_id=answer.tool_result_settlement.assistant_entry_id,
            tool_call_id=answer.tool_result_settlement.tool_call_id,
            request_semantic_digest="sha256:" + "1" * 64, arguments=freeze_json({"plan": "方案"}),
        )
        await coordinator.publish_open(waiter, draft=draft)
        assert await coordinator.current_open() is None
        assert await coordinator.wait(waiter) is answer
        assert await coordinator.current_waiter(answer.interaction_id) is None
        await coordinator.aclose()
    asyncio.run(run())


@pytest.mark.parametrize("decision", [PlanDraftDecision.REVISE, PlanDraftDecision.CANCEL, PlanDraftDecision.APPROVE])
def test_host_resolves_review_against_live_origin_and_only_approval_installs_successor(decision):
    async def run():
        accepted = resolution(decision)
        coordinator = KernelPlanInteractionCoordinator()
        origin_turn_id = accepted.tool_result_settlement.turn_id
        waiter = await coordinator.prepare_interaction(interaction_id=accepted.interaction_id, origin_turn_id=origin_turn_id)
        released = []
        writes = []
        class Repository:
            def confirm_plan_draft_review_winner(self, **kwargs):
                return None
            def resolve_plan_draft_review(self, guard, **kwargs):
                writes.append(kwargs)
                return accepted
            def classify_plan_continuation(self, inspection, guard):
                return PlanContinuationDisposition.RUNNING_CURRENT_WRITER
            def read_started_turn_input_group(self, guard, **kwargs):
                return None
        class Host(_HostPlanOperations):
            def _require_open(self):
                pass
            def _canonical_deadline(self):
                return 100000000000.0
            def _admit_plan_resolution_write_locked(self, *, creates_turn):
                assert not creates_turn
                return "reservation"
            async def _release_compaction_write_reservation(self, reservation):
                released.append(reservation)
            def _inspection_owned_by_current_host(self, inspection):
                return True
            async def _inspect_plan_continuation(self, **kwargs):
                return PlanContinuationInspection(
                    turn_id=accepted.continuation_turn_id, initial_entry_id=accepted.continuation_entry_id,
                    status="RUNNING", workflow_id=accepted.workflow_id, interaction_id=accepted.interaction_id,
                    handoff_kind=PlanHandoffKind.APPROVED_PLAN, session_lifecycle="OPEN",
                    writer_generation=1, writer_owner_id=random_id("host"),
                )
        async def io(function, *args, **kwargs):
            return function(*args, **kwargs)
        host = Host()
        host.session_id = random_id("session")
        host.writer_generation = 1
        host.host_session_id = random_id("host")
        host._lease = SimpleNamespace(guard=object())
        host.repository = Repository()
        host._io = SimpleNamespace(run=io)
        host._lock = asyncio.Lock()
        host._plan_continuations = ContinuationAdmissionOwner()
        host._plan_interactions = coordinator
        host._queue_wake = asyncio.Event()
        host._closing = False
        host._plan_exit_fence = False
        host._pending_root_successor = None
        host._active_task = asyncio.current_task()
        host._active_turn_id = origin_turn_id
        host._active_cancellation_intent = SimpleNamespace(cause=None)
        if decision is PlanDraftDecision.REVISE:
            async def freeze(prompt, **kwargs):
                return FrozenPromptContent.text("修改\r\n")
            host._image_validator = SimpleNamespace(freeze=freeze)
        observed = await host.resolve_plan_draft_review(
            command_id=accepted.command_id, workflow_id=accepted.workflow_id, expected_workflow_revision=1,
            interaction_id=accepted.interaction_id, decision=decision,
            revision_prompt=SimpleNamespace() if decision is PlanDraftDecision.REVISE else None,
        )
        assert observed is accepted and await coordinator.wait(waiter) is accepted
        assert released == ["reservation"] and len(writes) == 1
        if decision is PlanDraftDecision.APPROVE:
            assert writes[0]["continuation_turn_id"] is not None
            assert host._pending_root_successor.owner_task is asyncio.current_task()
            assert host._pending_root_successor.origin_turn_id == origin_turn_id
            assert host._active_root_phase is RootChainPhase.SUCCESSOR_COMMITTED_PENDING_HANDOFF
        else:
            assert writes[0]["continuation_turn_id"] is None
            assert host._pending_root_successor is None
            assert host._active_turn_id == origin_turn_id
        await host._plan_continuations.aclose()
        await coordinator.aclose()
    asyncio.run(run())
