"""A post-acceptance DB read failure keeps the real business value in JS."""

import asyncio
import pytest
from tests.test_agent_code_mode_execution import _settled_formal_records
from pulsara_agent.identity import random_id


@pytest.mark.postgres
def test_real_saved_effect_then_reread_timeout(
    tmp_path, monkeypatch, stage2_migrated_postgres_database
):
    from pulsara_agent.conversation_kernel.host import KernelHostCore
    from pulsara_agent.conversation_kernel._repository.tools import _ToolOperations
    from pulsara_agent.llm.input import PromptContent
    from pulsara_agent.primitives.permission import PermissionMode
    from pulsara_agent.workspace_identity import HostWorkspaceInput
    from tests.support.model_config import test_model_runtime, test_model_binding
    from tests.support.round3 import CallbackScriptedKernelModel
    from tests.support.model_stream import _tool_call, _text
    import pulsara_agent.conversation_kernel.host as kernel_host

    original = _ToolOperations.read_accepted_tool_result_observation
    saved = []

    def fail_after_real_read(self, **kwargs):
        value = original(self, **kwargs)
        from pulsara_agent.conversation_kernel.tool_invocations import (
            current_tool_invocation,
        )

        source = current_tool_invocation.get()
        if source is not None and source.tool_name == "write_file":
            saved.append(value)
            if len(saved) == 2:
                raise TimeoutError("post-commit result reread deadline")
        return value

    monkeypatch.setattr(
        _ToolOperations, "read_accepted_tool_result_observation", fail_after_real_read
    )
    code = """try {await tools.write_file({target:{kind:"PATH",path:"effect.txt"},content:"accepted-effect"});} catch(e) {console.log(JSON.stringify({name:e.name,code:e.code,message:e.message,hasResult:Object.hasOwn(e,"result"),result:e.result}));}"""

    async def stream(request):
        payloads = (
            _tool_call("exec_cell", random_id("call"), {"code": code})
            if len(model.requests) == 1
            else _text(random_id("block"), "ROOT_DONE")
        )
        for payload in payloads:
            yield payload

    model = CallbackScriptedKernelModel(stream)
    monkeypatch.setattr(kernel_host, "DirectKernelModelPort", lambda **_: model)
    monkeypatch.setattr(
        kernel_host.LocalMcpManagementService, "load_configs", lambda *_a, **_k: ()
    )
    runtime = test_model_runtime(
        model_id="test-pro",
        wire_api="openai_chat_completions",
        postgres_dsn=stage2_migrated_postgres_database.runtime_dsn,
    )

    async def scenario():
        core = KernelHostCore.production(model_runtime=runtime)
        session = await core.open_session(
            HostWorkspaceInput(workspace_kind="project", workspace_root=tmp_path)
        )
        try:
            await session.update_model_call_binding(test_model_binding(runtime))
            await session.run_turn(
                PromptContent.text("execute"),
                requested_permission_mode=PermissionMode.BYPASS_PERMISSIONS,
            )
            records = await _settled_formal_records(session, expected=1)
            assert (tmp_path / "effect.txt").read_text() == "accepted-effect"
            assert saved and all(item["state"] == "SUCCESS" for item in saved)
            import json

            observed = json.loads(records[0]["text"])
            assert observed["name"] == "ToolError"
            assert observed["code"] == "RESULT_PROJECTION_FAILED"
            assert observed["hasResult"] is True
            assert observed["result"] == saved[-1]["value"]
            assert observed["result"]["meta"]["status"] == "SUCCESS"
            assert saved[-1]["result_entry_id"] in observed["message"]
            assert (
                "完整结果已保存" in observed["message"]
                and "不应重做业务操作" in observed["message"]
            )
            assert (
                "foreground physical I/O exited after its deadline"
                in observed["message"]
            )
            assert len({item["result_id"] for item in saved}) == 1
        finally:
            await core.shutdown()

    asyncio.run(scenario())


def test_delivered_err_result_advances_wait_agent_message_observation(monkeypatch):
    """The actual error RPC delivery owns the same cursor as normal delivery."""
    from types import SimpleNamespace

    from pulsara_agent.conversation_kernel import tool_execution
    from pulsara_agent.conversation_kernel.tool_execution import ToolBatchExecutor
    from pulsara_agent.conversation_kernel.subagents.collaboration_contracts import (
        agent_target,
    )
    from tests.test_agent_wait_until import harness
    from tests.test_code_mode_complete_values import run

    class Snapshot(SimpleNamespace):
        pass

    class InlineIO:
        async def run(self, operation, *args, **kwargs):
            return operation(*args, **kwargs)

    monkeypatch.setattr(tool_execution, "CodeModeExecutionSnapshot", Snapshot)
    h = harness()
    agent, _work = h.add_work(status="completed")
    h.runtime.messages[agent] = 1
    arguments = {"agents": [agent_target(agent)], "timeout_seconds": 0}
    execution = object.__new__(ToolBatchExecutor)
    execution._tools = SimpleNamespace(
        borrow_code_mode_surface=lambda *_a, **_k: SimpleNamespace(close=lambda: None)
    )
    execution._io = InlineIO()
    execution._canonical_deadline = lambda: float("inf")
    settled = []

    async def execute(**_kwargs):
        result = await h.api.invoke(
            tool_name="wait_agent", arguments=arguments, invocation_context=h.context
        )
        settled.append(result)
        settlement = SimpleNamespace(
            result_id=random_id("result"),
            result_entry_id=random_id("entry"),
            result_state=result.state,
        )
        settlement.public_projection = SimpleNamespace(
            metadata=SimpleNamespace(
                session_id=h.root,
                result_id=settlement.result_id,
            )
        )
        tool_execution._code_result.get().append((result, settlement))

    def read(**_kwargs):
        if len(settled) == 1:
            # The message has not reached JS merely because the result exists.
            assert h.context.agent_message_cursors == {}
            raise TimeoutError("post-acceptance read unavailable")
        return {"value": settled[-1].public_value()}

    execution.execute = execute
    execution._repository = SimpleNamespace(read_accepted_tool_result_observation=read)

    async def invoke(invocation):
        local = SimpleNamespace(
            **{
                field: getattr(invocation, field)
                for field in (
                    "arguments",
                    "tool_name",
                    "sequence",
                    "source_exec_attempt_id",
                    "cell_id",
                )
            }
        )
        local.snapshot = Snapshot(
            turn_id=h.context.turn_id,
            tool_directory=object(),
            canonical_facts=object(),
            canonical_identity=object(),
            request=SimpleNamespace(session_id=h.root),
            agent_invocation_snapshot=None,
            continuity_scope=object(),
            hook_scope=None,
        )
        return await execution.invoke_code_mode(local)

    records = run(
        """
try { await tools.sample({}); }
catch (error) { console.log(error.result.meta.wait_reason); }
return (await tools.sample({})).meta.wait_reason;
""",
        invoke,
    )
    assert records == [
        {"kind": "log", "values": [{"type": "text", "value": "message_available"}]},
        {"kind": "return", "values": [{"type": "text", "value": "condition_met"}]},
    ]
    assert h.context.agent_message_cursors == {agent: 1}
