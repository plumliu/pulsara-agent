"""Production Host approval results, provider delivery, and pre-dispatch cleanup."""

from __future__ import annotations

import asyncio
import re
from time import monotonic

import pytest
from psycopg.rows import dict_row

from pulsara_agent.conversation_kernel import interaction
from pulsara_agent.conversation_kernel.tool_outputs import PostgresToolOutputReadPort
from pulsara_agent.conversation_kernel.tool_surface import ProcessLocalToolSurfaceBorrow
from pulsara_agent.identity import random_id
from pulsara_agent.llm.input import MessageRole, PromptContent, join_text_content
from pulsara_agent.primitives.permission import PermissionMode
from pulsara_agent.storage.postgres_connection_provider import PostgresConnectionLane
from tests.support.mcp import finish_connection_attempts
from tests.test_blocking_steer_host import _wait_for_card
from tests.test_round4_plan_host import _DetachedDraftModel, _open_test_session, _text, _tool_call
from tests.test_round6_mcp_production import _config


pytestmark = pytest.mark.postgres
UNAUTHORIZED_TEXT = "本次工具调用未获授权，操作未执行。"
WORKSPACE_UNAVAILABLE_TEXT = "工作目录不可用，此工具未执行。"


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    monkeypatch.setenv("PULSARA_HOME", str(tmp_path / "home"))
    root = tmp_path / "workspace"
    root.mkdir()
    return root


class ApprovalModel(_DetachedDraftModel):
    def __init__(self, tool_name, arguments):
        super().__init__()
        self.tool_name = tool_name
        self.arguments = arguments

    async def stream(self, request):
        self.requests.append(request)
        cycle = len(self.requests)
        if self.tool_name == "call_mcp_tool" and cycle == 1:
            events = _tool_call(
                "query_capabilities", "call:inspect-effect",
                {"kind": "MCP", "target": {"kind": "MCP_TOOL", "server_id": "fixture", "remote_tool_name": "fixture_effect"}},
            )
        elif cycle == (2 if self.tool_name == "call_mcp_tool" else 1):
            arguments = self.arguments
            if self.tool_name == "call_mcp_tool":
                result = next(message for message in reversed(request.compiled_input.messages)
                              if message.role is MessageRole.TOOL_RESULT)
                reference = re.search(r"mcpref:[a-z2-7]{26}", join_text_content(result.content))
                assert reference is not None
                arguments = {"target": {"kind": "MCP_TOOL_REF", "tool_ref": reference.group()}, "arguments": arguments}
            events = _tool_call(self.tool_name, "call:permission", arguments)
        else:
            events = _text("finished")
        for event in events:
            yield event


async def _decide(session, controller, decision):
    snapshot = session.live_control.current_snapshot()
    return await session.resolve_tool_interaction(
        expected_writer_generation=session._lease.guard.writer_generation,
        expected_owner_epoch=snapshot.owner_epoch,
        expected_live_revision=snapshot.revision,
        interaction_id=snapshot.current_interaction.interaction_id,
        command_id=random_id("command"),
        decision=decision,
        actor_id=controller,
    )


async def _accepted_result(session):
    def read():
        with session.repository.connection_provider.connection(
            lane=PostgresConnectionLane.INSPECTOR, row_factory=dict_row,
            deadline_monotonic=monotonic() + 5,
        ) as connection:
            return connection.execute(
                """SELECT r.result_entry_id, r.result_state, r.result_origin_kind, r.attempt_id,
                       ARRAY(SELECT d.decision FROM pulsara_v3.interaction_decisions AS d
                             WHERE d.session_id = r.session_id AND d.subject_invocation_id = r.invocation_id
                               AND d.actor_kind = 'human') AS decisions,
                       ARRAY(SELECT a.id FROM pulsara_v3.tool_execution_attempts AS a
                             WHERE a.session_id = r.session_id AND a.invocation_id = r.invocation_id) AS attempts
                   FROM pulsara_v3.tool_results AS r
                   WHERE r.session_id = %s AND r.tool_call_id = 'call:permission'""",
                (session.session_id,),
            ).fetchall()

    async with asyncio.timeout(5):
        while not (results := await asyncio.to_thread(read)):
            await asyncio.sleep(0.01)
    assert len(results) == 1
    return results[0]


def _assert_delivery(session, model, result, status, text):
    result_entry_id = result["result_entry_id"]
    reader = PostgresToolOutputReadPort(
        session.repository.connection_provider,
        session_id=session.session_id, workspace_id=session.workspace.workspace_key,
    )
    value = reader.read_result({"kind": "SESSION_ENTRY", "entry_id": result_entry_id}).value
    assert value == {"meta": {"status": status}, "content": ({"type": "text", "value": text},)}
    messages = model.requests[-1].compiled_input.messages
    results = [message for message in messages
               if message.role is MessageRole.TOOL_RESULT and message.tool_call_id == "call:permission"]
    assert len(results) == 1
    message = join_text_content(results[0].content)
    assert f"\nmeta:\n  status: {status}\n\ncontent:\n" in message
    assert f"\n{text}\ntool_result:" in message
    assert f'"entry_id":"{result_entry_id}"' in message
    assert message.endswith("next: null")


@pytest.mark.parametrize(("tool_name", "decision", "workspace_missing"), [
    ("write_file", "DENY", False),
    ("exec_process", "DENY", False),
    ("write_file", "EXPIRE", True),
    ("exec_process", "EXPIRE", False),
])
def test_denial_and_expiry_use_same_value_without_an_attempt(
    workspace, monkeypatch, stage2_migrated_postgres_database, tool_name, decision, workspace_missing,
):
    if decision == "EXPIRE":
        monkeypatch.setattr(interaction, "INTERACTION_TIMEOUT_SECONDS", 0.3)
    outside_workspace = tool_name == "write_file" and decision == "DENY"
    target = (workspace.parent if outside_workspace else workspace) / "should-not-exist.txt"
    permission_mode = PermissionMode.ACCEPT_EDITS if outside_workspace else PermissionMode.ASK_PERMISSIONS
    arguments = ({"target": {"kind": "PATH", "path": str(target)}, "content": "unexpected write"}
                 if tool_name == "write_file" else {"command": "touch should-not-exist.txt", "yield_time_ms": 0})
    command_directory = workspace
    if tool_name == "exec_process" and decision == "DENY":
        command_directory = workspace / "project-tools"
        command_directory.mkdir()
        arguments["workdir"] = command_directory.name
        target = command_directory / "should-not-exist.txt"

    async def exercise():
        model = ApprovalModel(tool_name, arguments)
        core, session = await _open_test_session(
            tmp_path=workspace, model=model, monkeypatch=monkeypatch,
            postgres_dsn=stage2_migrated_postgres_database.runtime_dsn,
        )
        running = None
        moved_workspace = workspace.with_name("workspace-moved")
        try:
            controller = random_id("attachment")
            assert await session.attach_controller(controller)
            running = asyncio.create_task(session.run_turn(
                PromptContent.text("Run the operation once"), requested_permission_mode=permission_mode,
            ))
            await _wait_for_card(session)
            card = session.live_control.current_snapshot().current_interaction
            assert card.interaction_kind == "TOOL_CONFIRMATION"
            if tool_name == "write_file":
                assert str(target.resolve()) in card.public_prompt
                assert ("工作区外" if outside_workspace else "工作区内") in card.public_prompt
                assert arguments["content"] not in card.public_prompt
            else:
                assert str(command_directory.resolve()) in card.public_prompt
                assert arguments["command"] in card.public_prompt
            if workspace_missing:
                workspace.rename(moved_workspace)
            if decision == "DENY":
                assert (await _decide(session, controller, "DENY")).status == "SUCCEEDED"
            row = await _accepted_result(session)
            assert session.live_control.current_snapshot().current_interaction is None
            assert row["result_state"] == ("PERMISSION_DENIED" if decision == "DENY" else "CANCELLED_BEFORE_DISPATCH")
            assert row["result_origin_kind"] == "POLICY_NO_ATTEMPT"
            assert row["attempt_id"] is None and row["attempts"] == []
            assert row["decisions"] == (["DENY"] if decision == "DENY" else [])
            if workspace_missing:
                assert not running.done() and len(model.requests) == 1
                moved_workspace.rename(workspace)
                session.workspace_gate.wake()
            await asyncio.wait_for(running, 5)
            assert not target.exists()
            _assert_delivery(session, model, row, "ERROR" if decision == "DENY" else "CANCELLED", UNAUTHORIZED_TEXT)
        finally:
            if moved_workspace.exists():
                moved_workspace.rename(workspace)
            await core.shutdown()
            if running is not None:
                await asyncio.gather(running, return_exceptions=True)

    asyncio.run(exercise())


@pytest.mark.parametrize("outcome", ["workspace", "binding", "deny", "expire"])
def test_mcp_permission_settlement_releases_the_undispatched_permit(
    workspace, tmp_path, monkeypatch, stage2_migrated_postgres_database, outcome,
):
    if outcome == "expire":
        monkeypatch.setattr(interaction, "INTERACTION_TIMEOUT_SECONDS", 0.3)
    config = _config(tmp_path)

    async def exercise():
        model = ApprovalModel("call_mcp_tool", {"value": "unexpected remote effect"})
        core, session = await _open_test_session(
            tmp_path=workspace, model=model, monkeypatch=monkeypatch,
            postgres_dsn=stage2_migrated_postgres_database.runtime_dsn,
        )
        running = None
        moved_workspace = workspace.with_name("workspace-moved")
        active_mcp_borrow = None
        revoked_mcp_borrow = None
        original_request = session._tools.request_confirmation
        original_binding = ProcessLocalToolSurfaceBorrow.execution_binding
        original_invoke = session._tools.invoke
        calls = []

        async def request_confirmation(**kwargs):
            nonlocal revoked_mcp_borrow
            authorization = await original_request(**kwargs)
            if outcome in {"workspace", "binding"}:
                assert authorization.accepted_attempt_id is not None
                if outcome == "workspace":
                    workspace.rename(moved_workspace)
                else:
                    assert active_mcp_borrow is not None
                    revoked_mcp_borrow = active_mcp_borrow
            return authorization

        def execution_binding(borrow, tool_name):
            nonlocal active_mcp_borrow
            if tool_name == "call_mcp_tool":
                if borrow is revoked_mcp_borrow:
                    raise RuntimeError("test revoked the exact execution binding")
                active_mcp_borrow = borrow
            return original_binding(borrow, tool_name)

        async def invoke(**kwargs):
            calls.append(kwargs["tool_name"])
            return await original_invoke(**kwargs)

        try:
            monkeypatch.setattr(session._tools, "request_confirmation", request_confirmation)
            monkeypatch.setattr(session._tools, "invoke", invoke)
            monkeypatch.setattr(ProcessLocalToolSurfaceBorrow, "execution_binding", execution_binding)
            await session.reload_mcp_configs((config,), deadline_monotonic=monotonic() + 20)
            await finish_connection_attempts(session._mcp_supervisor)
            controller = random_id("attachment")
            assert await session.attach_controller(controller)
            running = asyncio.create_task(session.run_turn(
                PromptContent.text("Call the fixture once"), requested_permission_mode=PermissionMode.ASK_PERMISSIONS,
            ))
            await _wait_for_card(session)
            card = session.live_control.current_snapshot().current_interaction
            assert card.interaction_kind == "TOOL_CONFIRMATION"
            assert card.public_prompt == "调用 MCP 服务器 fixture 上的工具 fixture_effect。"
            assert len(session._tools._mcp_dispatch_permits) == 1
            permit = next(iter(session._tools._mcp_dispatch_permits.values()))
            assert permit.state.value == "ADMITTED"
            if outcome != "expire":
                assert (await _decide(session, controller, "DENY" if outcome == "deny" else "ALLOW")).status == "SUCCEEDED"
            row = await _accepted_result(session)
            assert calls == ["query_capabilities"]
            assert permit.state.value == "RELEASED"
            assert not session._tools._mcp_dispatch_permits
            assert not session._tools._mcp_dispatch_resources
            assert not session._tools._mcp_meta_invocations
            assert not session._tools._mcp_confirmation_admissions
            assert session.live_control.current_snapshot().current_interaction is None
            if outcome in {"workspace", "binding"}:
                assert row["result_state"] == "APPLICATION_ERROR"
                assert row["result_origin_kind"] == "PHYSICAL_ATTEMPT"
                assert row["attempt_id"] is not None and row["attempts"] == [row["attempt_id"]]
                assert row["decisions"] == ["ALLOW"]
                if outcome == "workspace":
                    assert not running.done() and len(model.requests) == 2
                    moved_workspace.rename(workspace)
                    session.workspace_gate.wake()
                expected_text = (WORKSPACE_UNAVAILABLE_TEXT if outcome == "workspace"
                                 else "工具 call_mcp_tool 已不可用，本次调用未执行。")
            else:
                assert row["result_state"] == ("PERMISSION_DENIED" if outcome == "deny" else "CANCELLED_BEFORE_DISPATCH")
                assert row["result_origin_kind"] == "POLICY_NO_ATTEMPT"
                assert row["attempt_id"] is None and row["attempts"] == []
                assert row["decisions"] == (["DENY"] if outcome == "deny" else [])
                expected_text = UNAUTHORIZED_TEXT
            await asyncio.wait_for(running, 5)
            _assert_delivery(session, model, row, "CANCELLED" if outcome == "expire" else "ERROR", expected_text)
        finally:
            if moved_workspace.exists():
                moved_workspace.rename(workspace)
            await core.shutdown()
            if running is not None:
                await asyncio.gather(running, return_exceptions=True)

    asyncio.run(exercise())
