"""Physical cancellation retains the exact returned complete business value."""
import asyncio
from threading import Event
from time import monotonic
from types import SimpleNamespace
from pulsara_agent.conversation_kernel.content_parts import ResultPart
from pulsara_agent.conversation_kernel.execution_watchdogs import KernelExecutionDeadlineFactory
from pulsara_agent.conversation_kernel.io import KernelSessionIO
from pulsara_agent.conversation_kernel.tool_runtime import DirectKernelToolPort
from pulsara_agent.conversation_kernel.tool_surface import BuiltinExecutionPolicyRef
from pulsara_agent.identity import random_id
from pulsara_agent.message import ToolResultState
from pulsara_agent.model_input.contracts import ModelInputScopeKind
from pulsara_agent.ports.tool_execution import ToolExecutionResult, ToolBusinessValue
from pulsara_agent.primitives.permission import PermissionMode

def context():
    return SimpleNamespace(session_id=random_id("session"),tool_call_id=random_id("call"),
        workspace_id=random_id("workspace"),result_entry_id=random_id("entry"))


def test_runtime_cancellation_reaches_synchronous_tool_and_joins_cleanup():
    async def run():
        started, cleaned = Event(), Event()
        ctx = context()
        class CancellableTool:
            def execute(self, call):
                started.set()
                while not call.cancel_requested():
                    cleaned.wait(0.001)
                cleaned.set()
                return ToolExecutionResult(call.id, call.name, ToolResultState.SUCCESS,
                    business_value=ToolBusinessValue({},(ResultPart("text","finished"),)))
        port = object.__new__(DirectKernelToolPort)
        binding = SimpleNamespace(tool_name="find_files", execution_policy=BuiltinExecutionPolicyRef("find_files", "fixture"))
        borrow = SimpleNamespace(code_cell_id=None,
            prepared=SimpleNamespace(access=SimpleNamespace(conversation_scope_kind=ModelInputScopeKind.ROOT)),
            execution_binding=lambda name: binding)
        ctx.__dict__.update(turn_id=random_id("turn"), assistant_entry_id=random_id("entry"), attempt_id=random_id("attempt"),
            attempt_permission_snapshot_fingerprint="permission", permission_snapshot_fingerprint="permission",
            surface_borrow=borrow, conversation_scope_kind="ROOT", cell_cancel_event=None,
            code_cell_id=None, effective_permission_mode=PermissionMode.BYPASS_PERMISSIONS,
            permission_confirmation_granted=False)
        port._closed, port._session_id = False, ctx.session_id
        port._validate_surface_borrow = lambda *args: binding
        port._subagent, port._decision, port._memory = None, None, None
        port._tools = {"find_files": CancellableTool()}
        port._deadlines = KernelExecutionDeadlineFactory()
        io = port._physical_io = KernelSessionIO()
        try:
            task = asyncio.create_task(port.invoke(tool_name="find_files", arguments={}, tool_call_id=ctx.tool_call_id,
                attempt_id=ctx.attempt_id, turn_id=ctx.turn_id, assistant_entry_id=ctx.assistant_entry_id, invocation_context=ctx))
            assert await asyncio.to_thread(started.wait, 2)
            task.cancel()
            result = await asyncio.wait_for(task, 2)
            assert cleaned.is_set()
            assert result.caller_cancelled_while_running
            assert result.state == "SUCCESS"
            assert result.content.content == (ResultPart("text","finished"),)
        finally:
            await io.aclose(deadline_monotonic=monotonic() + 2)
    asyncio.run(run())
