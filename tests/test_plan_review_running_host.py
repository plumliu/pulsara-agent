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
@pytest.mark.parametrize("file_failure", [False, True, "late_success", "late_failure"])
def test_running_review_final_result_precedes_same_turn_resume_or_approved_successor(
    tmp_path: Path, monkeypatch, stage2_migrated_postgres_database, decision, file_failure,
):
    import pulsara_agent.conversation_kernel.host as host_module

    if file_failure is True:
        def fail_publication(*args, **kwargs):
            raise PermissionError("test folder is read-only")
        monkeypatch.setattr("pulsara_agent.tools.builtins.filesystem._atomic_create_bytes", fail_publication)
    elif file_failure in {"late_success", "late_failure"}:
        from time import sleep
        from pulsara_agent.conversation_kernel.io import KernelSessionIO
        from pulsara_agent.tools.builtins import filesystem

        original_write = filesystem._atomic_create_bytes
        original_run = KernelSessionIO.run_tool_invocation

        def slow_write(path, content):
            sleep(0.05)
            if file_failure == "late_failure":
                raise PermissionError("test folder is read-only")
            original_write(path, content)

        async def short_file_deadline(self, operation, /, *args, deadline_monotonic, **kwargs):
            if operation.__name__ == "publish_plan_markdown":
                deadline_monotonic = min(deadline_monotonic, monotonic() + 0.02)
            return await original_run(self, operation, *args, deadline_monotonic=deadline_monotonic, **kwargs)

        monkeypatch.setattr(filesystem, "_atomic_create_bytes", slow_write)
        monkeypatch.setattr(KernelSessionIO, "run_tool_invocation", short_file_deadline)
    file_failed = file_failure is True or file_failure == "late_failure"
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
            await session.enter_plan(command_id=random_id("command"),  resume_permission_mode=PermissionMode.ACCEPT_EDITS)
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
            assert (opened.expires_at - review["accepted_at"]).total_seconds() == 600
            if file_failed:
                assert review["plan_file_path"] is None and "test folder is read-only" in review["plan_file_error"]
            else:
                assert review["plan_file_path"] == ".pulsara/plans/方案.md" and review["plan_file_error"] is None
                assert (tmp_path / review["plan_file_path"]).read_bytes() == "方案\n\n步骤".encode()
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
                import json
                observations = []
                for message in resumed.compiled_input.messages:
                    for part in message.content:
                        try:
                            envelope = json.loads(getattr(part,"text",""))
                        except json.JSONDecodeError:
                            continue
                        if isinstance(envelope,dict) and "pulsara_runtime_observation" in envelope:
                            observations.append(envelope["pulsara_runtime_observation"])
                approved = [item for item in observations if item["source"] == "APPROVED_PLAN"]
                assert len(approved) == 1
                assert approved[0]["body"][-1] == {"text":"方案\n\n步骤"}
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
                final = connection.execute("SELECT public_fields,result_entry_id FROM pulsara_v3.tool_results WHERE session_id=%s AND tool_call_id='provider-plan-call'", (session.session_id,)).fetchone()
                from pulsara_agent.conversation_kernel.content_parts import read_content_parts
                parts = read_content_parts(connection, entry_id=final["result_entry_id"])
                if file_failed:
                    assert final["public_fields"] == {} and "Markdown 文件保存失败：" in parts[0].value
                    assert "test folder is read-only" in parts[0].value
                else:
                    assert final["public_fields"] == {"target":{"kind":"PATH","path":".pulsara/plans/方案.md"}}
            assert count == 1
        finally:
            await core.close_session(session.host_session_id)
            if running is not None:
                await asyncio.gather(running, return_exceptions=True)
            await core.shutdown()
    asyncio.run(run())


@pytest.mark.parametrize("control", ["stop", "close", "force_exit"])
def test_cancel_during_plan_file_publication_drains_without_opening_review(
    tmp_path, monkeypatch, stage2_migrated_postgres_database, control,
):
    from threading import Event
    from pulsara_agent.conversation_kernel.io import KernelSessionIO
    from pulsara_agent.tools.builtins import filesystem
    from tests.test_round4_plan_host import _DetachedDraftModel, _open_test_session

    entered_file, release_file = Event(), Event()
    original_write = filesystem._atomic_create_bytes
    original_run = KernelSessionIO.run_tool_invocation

    def held_write(path, content):
        entered_file.set()
        assert release_file.wait(10)
        original_write(path, content)

    class Model(_DetachedDraftModel):
        async def stream(self, request):
            self.requests.append(request)
            assert len(self.requests) == 1
            for payload in _tool_call("exit_plan", "file-cancel-call", {"plan": "preserve exact plan"}):
                yield payload

    async def scenario():
        cancelled = asyncio.Event()

        async def observe_file_cancel(self, operation, /, *args, **kwargs):
            if operation.__name__ == "publish_plan_markdown":
                kwargs["on_caller_cancelled"] = cancelled.set
            return await original_run(self, operation, *args, **kwargs)

        monkeypatch.setattr(filesystem, "_atomic_create_bytes", held_write)
        monkeypatch.setattr(KernelSessionIO, "run_tool_invocation", observe_file_cancel)
        core, session = await _open_test_session(tmp_path=tmp_path, model=Model(),
            postgres_dsn=stage2_migrated_postgres_database.runtime_dsn, monkeypatch=monkeypatch)
        running = action = None
        try:
            entered = await session.enter_plan(command_id=random_id("command"), resume_permission_mode=PermissionMode.ACCEPT_EDITS)
            running = asyncio.create_task(session.run_turn(PromptContent.text("prepare a plan")))
            assert await asyncio.wait_for(asyncio.to_thread(entered_file.wait, 5), 6)
            if control == "stop":
                action = asyncio.create_task(session.request_stop_turn(command_id=random_id("command"),
                    expected_session_id=session.session_id, expected_host_session_id=session.host_session_id,
                    target_turn_id=session._active_turn_id, admission_deadline_ms=session.control_admission_deadline_ms()))
            elif control == "close":
                action = asyncio.create_task(core.close_session(session.host_session_id))
            else:
                action = asyncio.create_task(session.force_exit_plan(command_id=random_id("command"),
                    workflow_id=entered.target_id, expected_workflow_revision=entered.plan_workflow_revision))
            await asyncio.wait_for(cancelled.wait(), 5)
            assert not running.done()
            assert await session._plan_interactions.current_open() is None
            release_file.set()
            await asyncio.wait_for(action, 5)
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(running, 5)
            assert (tmp_path / ".pulsara/plans/方案.md").read_bytes() == b"preserve exact plan"
            with session.repository.connection_provider.connection(lane=PostgresConnectionLane.INSPECTOR,
                    deadline_monotonic=monotonic()+10) as connection:
                assert connection.execute("SELECT count(*) FROM pulsara_v3.plan_interactions WHERE session_id=%s",
                    (session.session_id,)).fetchone()[0] == 0
        finally:
            release_file.set()
            await asyncio.gather(*(task for task in (action, running) if task is not None), return_exceptions=True)
            await core.shutdown()

    asyncio.run(scenario())


@pytest.mark.parametrize("tool_name,arguments,expected_text", [
    ("ask_plan_question", {"question":"如何继续？","options":[{"label":"A"},{"label":"B"}]}, "问题等待回答超时（10 分钟），未获得用户回答。"),
    ("exit_plan", {"plan":"超时方案\r\n保持原文"}, "方案审批等待超时（10 分钟），未获得用户批准。"),
])
def test_expired_interaction_delivers_one_cancelled_result_to_original_readonly_turn(
    tmp_path, monkeypatch, stage2_migrated_postgres_database, tool_name, arguments, expected_text,
):
    from datetime import datetime, timedelta
    import pulsara_agent.conversation_kernel.host as host_module
    import pulsara_agent.conversation_kernel.plan_runtime as plan_runtime
    from pulsara_agent.conversation_kernel.repository_errors import ConversationKernelConflict
    from pulsara_agent.conversation_kernel.tool_outputs import read_accepted_tool_business_value
    runtime = model_runtime(postgres_dsn=stage2_migrated_postgres_database.runtime_dsn, wire_api="openai_chat_completions")
    class Model(_PreflightModel):
        def __init__(self):
            self._preparer = DirectKernelModelPort(model_runtime=runtime)
            self.requests = []
        async def stream(self, request):
            self.requests.append(request)
            for item in (_tool_call(tool_name, "deadline-call", arguments) if len(self.requests)==1 else _text("超时后继续规划")):
                yield item
    class AfterDeadline(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime.now(tz) + timedelta(seconds=601)
    # Advance only the waiting owner's clock. Accepted timestamps and the
    # fixed accepted_at + 600 deadline remain real database facts.
    monkeypatch.setattr(plan_runtime, "datetime", AfterDeadline)
    model = Model()
    monkeypatch.setattr(host_module, "DirectKernelModelPort", lambda **_: model)
    monkeypatch.setattr(host_module.LocalMcpManagementService, "load_configs", lambda *_args, **_kwargs: ())
    async def run():
        core = KernelHostCore.production(model_runtime=runtime)
        session = await core.open_session(HostWorkspaceInput(workspace_kind="project", workspace_root=tmp_path))
        await session.update_model_call_binding(model_binding(runtime))
        try:
            await session.enter_plan(command_id=random_id("command"), resume_permission_mode=PermissionMode.ACCEPT_EDITS)
            await asyncio.wait_for(session.run_turn(PromptContent.text("继续规划"), command_id=random_id("command"), requested_permission_mode=PermissionMode.ACCEPT_EDITS), timeout=10)
            assert len(model.requests)==2
            assert model.requests[0].turn_id == model.requests[1].turn_id
            assert any(expected_text in getattr(part,"text","") for message in model.requests[1].compiled_input.messages for part in message.content)
            assert await session._plan_interactions.current_open() is None
            with session.repository.connection_provider.connection(lane=PostgresConnectionLane.INSPECTOR,
                    row_factory=dict_row, deadline_monotonic=monotonic()+10) as connection:
                rows = connection.execute("SELECT * FROM pulsara_v3.tool_results WHERE session_id=%s AND tool_call_id='deadline-call'", (session.session_id,)).fetchall()
                assert len(rows)==1
                value = read_accepted_tool_business_value(connection, rows[0])
                assert value["meta"]["status"] == "CANCELLED"
                assert value["content"][0]["value"] == expected_text
                interaction = connection.execute("SELECT i.*, w.workflow_revision,w.status AS workflow_status FROM pulsara_v3.plan_interactions i JOIN pulsara_v3.plan_workflows w ON w.id=i.plan_workflow_id WHERE i.id=%s", (rows[0]["control_plan_interaction_id"],)).fetchone()
                assert interaction["status"] == "CANCELLED" and interaction["workflow_status"] == "ACTIVE"
                assert interaction["decision_continuation_entry_id"] is None
                assert connection.execute("SELECT effective_permission_mode FROM pulsara_v3.turns WHERE id=%s", (model.requests[1].turn_id,)).fetchone()["effective_permission_mode"] == "read-only"
                if tool_name == "exit_plan":
                    assert value["target"] == {"kind":"PATH","path":".pulsara/plans/方案.md"}
                    assert (tmp_path/interaction["plan_file_path"]).read_bytes() == arguments["plan"].encode()
            if tool_name == "exit_plan":
                with pytest.raises(ConversationKernelConflict):
                    await session.resolve_plan_draft_review(command_id=random_id("command"), workflow_id=interaction["plan_workflow_id"], expected_workflow_revision=interaction["workflow_revision"]-1,
                        interaction_id=interaction["id"], decision=PlanDraftDecision.APPROVE)
        finally:
            await core.close_session(session.host_session_id)
            await core.shutdown()
    asyncio.run(run())
