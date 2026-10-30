"""Atomic Plan approval starts its real successor before first-call compaction."""
from __future__ import annotations

import asyncio
from dataclasses import replace

import pytest

from pulsara_agent.conversation_kernel.compaction.contracts import ResolvedCompactionPolicy
from pulsara_agent.identity import random_id
from pulsara_agent.llm.input import PromptContent
from pulsara_agent.primitives.permission import PermissionMode
from pulsara_agent.primitives.plan_workflow import PlanDraftDecision
from tests.test_compaction_request_start_integration import (
    _Model, _eventually, _render, _rows, _seed_history,
)
from tests.test_round4_plan_host import _open_test_session, _text, _tool_call

pytestmark = pytest.mark.postgres


class _PlanSummaryModel(_Model):
    async def _stream(self, request):
        self.root_requests.append(request)
        if len(self.root_requests) == 1:
            events = _tool_call("exit_plan", "call:plan-draft-for-summary", {
                "plan": "1. inspect source\n2. implement PLAN_APPROVAL_EXACT_SENTINEL\n3. verify",
                "summary": "prepare the approved implementation",
            })
        else:
            events = _text("PLAN_SUCCESSOR_FINISHED")
        for event in events:
            yield event


@pytest.mark.parametrize("with_steer", [False, True])
def test_plan_approval_starts_atomic_successor_before_summary_and_delivers_steer(
    tmp_path, monkeypatch, stage2_migrated_postgres_database, with_steer,
):
    async def scenario():
        monkeypatch.setenv("PULSARA_HOME", str(tmp_path / "home"))
        model = _PlanSummaryModel()
        core, root = await _open_test_session(tmp_path=tmp_path, model=model,
            postgres_dsn=stage2_migrated_postgres_database.runtime_dsn, monkeypatch=monkeypatch)
        try:
            root._compaction.policy = ResolvedCompactionPolicy(automatic_enabled=False)
            _seed_history(root)
            entered = await root.enter_plan(command_id=random_id("command"),
                entry_reason="prepare the implementation", resume_permission_mode=PermissionMode.ACCEPT_EDITS)
            draft = await root.run_turn(PromptContent.text("Draft a concrete implementation plan."),
                command_id=random_id("command"), requested_permission_mode=PermissionMode.ACCEPT_EDITS)
            assert draft.pending_plan_interaction_id is not None
            assert len(model.root_requests) == 1
            usage = await root.read_context_usage()
            ratio = usage["input_tokens"] / usage["input_budget_tokens"]
            root._compaction.policy = replace(root._compaction.policy,
                automatic_enabled=True, auto_trigger_ratio=ratio * 0.95,
                post_compaction_target_ratio=ratio * 0.85, minimum_reclaim_tokens=1)
            interaction = _rows(root,
                "SELECT w.workflow_revision FROM pulsara_v3.plan_interactions i "
                "JOIN pulsara_v3.plan_workflows w ON w.session_id=i.session_id AND w.id=i.plan_workflow_id "
                "WHERE i.session_id=%s AND i.id=%s", (root.session_id, draft.pending_plan_interaction_id))[0]
            resolution = await root.resolve_plan_draft_review(command_id=random_id("command"),
                workflow_id=entered.target_id, expected_workflow_revision=interaction["workflow_revision"],
                interaction_id=draft.pending_plan_interaction_id, decision=PlanDraftDecision.APPROVE,
                revision_prompt=None)
            await asyncio.wait_for(model.summary.started.wait(), 20)
            target = resolution.continuation_turn_id
            assert root.active_root_turn_id() == target and len(model.root_requests) == 1
            state = _rows(root,
                "SELECT t.status,t.initial_entry_id,e.entry_kind,e.entry_sequence,r.revision_ordinal, "
                "e.source_plan_workflow_id,e.source_plan_interaction_id,e.source_plan_handoff_kind "
                "FROM pulsara_v3.turns t JOIN pulsara_v3.transcript_entries e "
                "ON e.session_id=t.session_id AND e.id=t.initial_entry_id "
                "JOIN pulsara_v3.turn_context_binding_revisions r "
                "ON r.session_id=t.session_id AND r.id=t.current_context_binding_revision_id "
                "WHERE t.session_id=%s AND t.id=%s", (root.session_id, target))[0]
            assert state["status"] == "RUNNING" and state["revision_ordinal"] == 0
            assert state["entry_kind"] == "PLAN_CONTINUATION"
            assert state["source_plan_workflow_id"] == entered.target_id
            assert state["source_plan_interaction_id"] == draft.pending_plan_interaction_id
            assert state["source_plan_handoff_kind"] == "APPROVED_PLAN"
            assert _rows(root, "SELECT status FROM pulsara_v3.plan_workflows WHERE session_id=%s AND id=%s",
                (root.session_id, entered.target_id))[0]["status"] == "APPROVED"
            assert _rows(root, "SELECT status FROM pulsara_v3.turns WHERE session_id=%s AND id=%s",
                (root.session_id, draft.turn_id))[0]["status"] == "COMPLETED"
            if with_steer:
                steer = await root.steer_active_turn(command_id=random_id("command"), target_turn_id=target,
                    content=PromptContent.text("PLAN_SUCCESSOR_STEER_DURING_SUMMARY"))
                assert steer.status == "PENDING"
            model.summary.release.set()
            async def finished():
                return _rows(root, "SELECT status FROM pulsara_v3.turns WHERE session_id=%s AND id=%s",
                    (root.session_id, target))[0]["status"] == "COMPLETED"
            await _eventually(finished)
            assert len(model.summary.calls) == 1 and len(model.root_requests) == 2
            request = model.root_requests[1]
            rendered = "\n".join((
                request.compiled_input.system_prompt,
                _render(request.compiled_input.messages),
                *(call.arguments for message in request.compiled_input.messages
                  for call in message.tool_calls),
            ))
            assert "PLAN_APPROVAL_EXACT_SENTINEL" in rendered
            if with_steer:
                assert "PLAN_SUCCESSOR_STEER_DURING_SUMMARY" in rendered
            assert sum(placement.origin_entry_id == state["initial_entry_id"]
                for placement in request.compiled_input.message_placements) == 1
            adopted = _rows(root,
                "SELECT r.revision_ordinal,r.source_through_sequence FROM pulsara_v3.turns t "
                "JOIN pulsara_v3.turn_context_binding_revisions r "
                "ON r.session_id=t.session_id AND r.id=t.current_context_binding_revision_id "
                "WHERE t.session_id=%s AND t.id=%s", (root.session_id, target))[0]
            assert adopted["revision_ordinal"] == 1
            assert adopted["source_through_sequence"] < state["entry_sequence"]
        finally:
            model.summary.release.set()
            await core.shutdown()
    asyncio.run(scenario())
