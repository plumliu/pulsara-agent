"""Rejected direct memory writes are attempted ToolResults, not turn failures."""

import asyncio
import json
from types import SimpleNamespace
from time import monotonic

import pytest

from pulsara_agent.conversation_kernel.io import KernelSessionIO
from pulsara_agent.conversation_kernel.memory import contracts as memory
from pulsara_agent.conversation_kernel.memory_tools import KernelMemoryToolPort
from pulsara_agent.memory.scope import (
    MemoryDomainContext,
    freeze_memory_read_context_binding,
)
from pulsara_agent.retrieval.config import EmbeddingBackendConfig
from pulsara_agent.settings import LocalSettingsStore


@pytest.mark.parametrize(
    "case,expected_state,error",
    [
        ("old_handle", "APPLICATION_ERROR", "does not accept ToolResult citations"),
        ("old_kind", "APPLICATION_ERROR", "kind_hint is not supported"),
        ("missing_basis", "APPLICATION_ERROR", "based_on memory is absent"),
        ("closed", "SYSTEM_ERROR", "memory owner is closed"),
    ],
)
def test_memory_rejections_after_admission_have_attempted_states(
    tmp_path, case, expected_state, error
):
    async def exercise():
        io = KernelSessionIO()
        port = KernelMemoryToolPort(
            repository=SimpleNamespace(connection_provider=object()),
            session_id="session:test",
            read_binding=freeze_memory_read_context_binding(
                domain=MemoryDomainContext("test", "transient"),
                host_workspace_id="workspace:test",
            ),
            embedding_config=EmbeddingBackendConfig(),
            io_owner=io,
            settings=LocalSettingsStore(tmp_path / "settings.yaml"),
        )
        port._query = SimpleNamespace(get=lambda **kwargs: None)
        context = SimpleNamespace(
            session_id="session:test",
            workspace_id="workspace:test",
            assistant_entry_id="entry:test",
            tool_call_id="call:test",
            conversation_scope_kind="ROOT",
            memory_context=memory.FrozenModelCallMemoryContext(),
        )
        args = {
            "statement": "A test decision",
            "context_target": "GLOBAL",
            "kind": "DECISION",
        }
        if case == "old_handle":
            args["cited_tool_result_handles"] = ["tool:2"]
        if case == "old_kind":
            args["kind_hint"] = "AUTO"
        if case == "missing_basis":
            args["based_on_memory_ids"] = ["memory:missing"]
        try:
            if case == "closed":
                await port.aclose()
            result = await port.invoke(
                tool_name="remember", arguments=args, invocation_context=context
            )
            assert result.state == expected_state
            assert error in json.loads(result.content)["error"]
            assert result.memory_mutation is None
        finally:
            await port.aclose()
            await io.aclose(deadline_monotonic=monotonic() + 10)

    asyncio.run(exercise())


def test_detached_root_failure_is_logged_but_normal_cancellation_is_not(caplog):
    from pulsara_agent.conversation_kernel.host import KernelHostSession

    async def exercise():
        async def fail():
            raise ValueError("exact regression failure")

        task = asyncio.create_task(fail(), name="root-regression")
        try:
            await task
        except ValueError:
            pass
        KernelHostSession._observe_active_root_task_done(task)
        cancelled = asyncio.create_task(asyncio.sleep(0))
        cancelled.cancel()
        try:
            await cancelled
        except asyncio.CancelledError:
            pass
        KernelHostSession._observe_active_root_task_done(cancelled)

    asyncio.run(exercise())
    records = [r for r in caplog.records if "ROOT execution failed" in r.message]
    assert len(records) == 1
    assert records[0].exc_info[0] is ValueError
    assert str(records[0].exc_info[1]) == "exact regression failure"
