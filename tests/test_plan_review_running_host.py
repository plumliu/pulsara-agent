"""A live Host keeps draft review in the original read-only runner."""
import asyncio
from pathlib import Path
from time import monotonic

from psycopg.rows import dict_row
import pytest

from pulsara_agent.conversation_kernel.direct_model import DirectKernelModelPort
from pulsara_agent.conversation_kernel.host import KernelHostCore
from pulsara_agent.identity import random_id
from pulsara_agent.llm.input import PromptContent
from pulsara_agent.primitives.permission import PermissionMode
from pulsara_agent.primitives.plan_workflow import PlanDraftDecision
from pulsara_agent.storage.postgres_connection_provider import PostgresConnectionLane
from pulsara_agent.workspace_identity import HostWorkspaceInput
from tests.support.model_config import test_model_binding as model_binding, test_model_runtime as model_runtime
from tests.test_round4_plan_host import _PreflightModel, _tool_call, _text

pytestmark = pytest.mark.postgres


@pytest.mark.parametrize("decision", [PlanDraftDecision.REVISE, PlanDraftDecision.CANCEL, PlanDraftDecision.APPROVE])
def test_running_review_final_result_precedes_same_turn_resume_or_approved_successor(
    tmp_path: Path, monkeypatch, stage2_migrated_postgres_database, decision,
):
    import pulsara_agent.conversation_kernel.host as host_module

    runtime = model_runtime(postgres_dsn=stage2_migrated_postgres_database.runtime_dsn, wire_api="openai_chat_completions")
    class Model(_PreflightModel):
        def __init__(self):
            self._preparer = DirectKernelModelPort(model_runtime=runtime)
            self.requests = []
        async def stream(self, request):
            self.requests.append(request)
            if len(self.requests) == 1:
                for item in _tool_call("exit_plan", "provider-plan-call", {"plan": "方案\n\n步骤"}):
                    yield item
            else:
                assert len(self.requests) == 2
                for item in _text("决定后继续"):
                    yield item
    model = Model()
    monkeypatch.setattr(host_module, "DirectKernelModelPort", lambda **_: model)
    monkeypatch.setattr(host_module.LocalMcpManagementService, "load_configs", lambda *_args, **_kwargs: ())

    async def run():
        core = KernelHostCore.production(model_runtime=runtime)
        session = await core.open_session(HostWorkspaceInput(workspace_kind="project", workspace_root=tmp_path))
        await session.update_model_call_binding(model_binding(runtime))
        running = None
        try:
            await session.enter_plan(command_id=random_id("command"), entry_reason="planning", resume_permission_mode=PermissionMode.ACCEPT_EDITS)
            running = asyncio.create_task(session.run_turn(
                PromptContent.text("提交方案"), command_id=random_id("command"), requested_permission_mode=PermissionMode.ACCEPT_EDITS,
            ))
            opened = None
            deadline = monotonic() + 10
            while opened is None:
                if running.done():
                    await running
                    pytest.fail("origin runner finished before draft review was decided")
                opened = await session._plan_interactions.current_open()
                assert monotonic() < deadline
                if opened is None:
                    await asyncio.sleep(0.01)
            assert opened.draft is not None and opened.question is None
            with session.repository.connection_provider.connection(
                lane=PostgresConnectionLane.INSPECTOR, row_factory=dict_row, deadline_monotonic=monotonic()+10,
            ) as connection:
                review = connection.execute(
                    "SELECT i.*,w.workflow_revision,t.status AS turn_status FROM pulsara_v3.plan_interactions i JOIN pulsara_v3.plan_workflows w ON w.id=i.plan_workflow_id JOIN pulsara_v3.turns t ON t.id=i.origin_turn_id WHERE i.id=%s",
                    (opened.interaction_id,),
                ).fetchone()
                assert review["turn_status"] == "RUNNING" and review["control_tool_result_id"] is None
            origin = model.requests[0].turn_id
            accepted = await session.resolve_plan_draft_review(
                command_id=random_id("command"), workflow_id=review["plan_workflow_id"],
                expected_workflow_revision=review["workflow_revision"], interaction_id=opened.interaction_id,
                decision=decision, revision_prompt=PromptContent.text("修订\r\n反馈") if decision is PlanDraftDecision.REVISE else None,
            )
            await asyncio.wait_for(asyncio.shield(running), timeout=10)
            active = session._active_task
            if active is not None and active is not asyncio.current_task():
                await asyncio.wait_for(asyncio.shield(active), timeout=10)
            assert len(model.requests) == 2
            resumed = model.requests[1]
            if decision is PlanDraftDecision.APPROVE:
                assert resumed.turn_id == accepted.continuation_turn_id != origin
                expected_mode = PermissionMode.ACCEPT_EDITS.value
            else:
                assert resumed.turn_id == origin and accepted.continuation_turn_id is None
                expected_mode = PermissionMode.READ_ONLY.value
                original_input = model.requests[0].compiled_input
                assert resumed.compiled_input.system_prompt == original_input.system_prompt
                assert resumed.compiled_input.tools == original_input.tools
                assert resumed.compiled_input.messages[:len(original_input.messages)] == original_input.messages
            if decision is PlanDraftDecision.REVISE:
                messages = resumed.compiled_input.messages
                results = [index for index, item in enumerate(messages) if item.tool_call_id == "provider-plan-call"]
                feedback = [index for index, item in enumerate(messages) if any(
                    "修订\r\n反馈" in getattr(part, "text", "") for part in item.content
                )]
                assert len(results) == 1 and len(feedback) == 1
                assert results[0] < feedback[0]
            with session.repository.connection_provider.connection(
                lane=PostgresConnectionLane.INSPECTOR, row_factory=dict_row, deadline_monotonic=monotonic()+10,
            ) as connection:
                mode = connection.execute("SELECT effective_permission_mode FROM pulsara_v3.turns WHERE id=%s", (resumed.turn_id,)).fetchone()["effective_permission_mode"]
                assert mode == expected_mode
                count = connection.execute("SELECT count(*) AS n FROM pulsara_v3.tool_results WHERE session_id=%s AND tool_call_id='provider-plan-call'", (session.session_id,)).fetchone()["n"]
            assert count == 1
        finally:
            await core.close_session(session.host_session_id)
            if running is not None:
                await asyncio.gather(running, return_exceptions=True)
            await core.shutdown()
    asyncio.run(run())
