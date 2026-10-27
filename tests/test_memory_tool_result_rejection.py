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
from pulsara_agent.memory.targets import memory_target


@pytest.mark.parametrize(
    "case,expected_state,error",
    [
        ("old_handle", "APPLICATION_ERROR", "does not accept ToolResult citations"),
        ("old_kind", "APPLICATION_ERROR", "kind_hint is not supported"),
        ("old_basis", "APPLICATION_ERROR", "unsupported argument"),
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
            session_id='session:336wrfwkamrr3up6kjtsgvgcte',
            read_binding=freeze_memory_read_context_binding(
                domain=MemoryDomainContext("test", "transient"),
                host_workspace_id='workspace:wdl6b3mtrkghwnvjo4an3ekbau',
            ),
            embedding_config=EmbeddingBackendConfig(),
            io_owner=io,
            settings=LocalSettingsStore(tmp_path / "settings.yaml"),
        )
        port._query = SimpleNamespace(get=lambda **kwargs: None)
        context = SimpleNamespace(
            session_id='session:336wrfwkamrr3up6kjtsgvgcte',
            workspace_id='workspace:wdl6b3mtrkghwnvjo4an3ekbau',
            assistant_entry_id='entry:yizmduice57hcs52bz2usckgc4',
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
        if case == "old_basis":
            args["based_on_memory_ids"] = ['memory:b7oykog3jr6iuswihsj3h5x66e']
        if case == "missing_basis":
            args["based_on"] = [memory_target('memory:b7oykog3jr6iuswihsj3h5x66e')]
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


def test_returned_memory_target_is_reused_for_read_basis_and_relation(tmp_path):
    from pulsara_agent.conversation_kernel.memory.recall import MemoryQueryRow
    from pulsara_agent.memory.scope import CTX_GLOBAL

    async def exercise():
        io = KernelSessionIO()
        binding = freeze_memory_read_context_binding(
            domain=MemoryDomainContext("test", "transient"),
            host_workspace_id='workspace:wdl6b3mtrkghwnvjo4an3ekbau',
        )
        port = KernelMemoryToolPort(
            repository=SimpleNamespace(connection_provider=object()),
            session_id='session:336wrfwkamrr3up6kjtsgvgcte',
            read_binding=binding, embedding_config=EmbeddingBackendConfig(),
            io_owner=io, settings=LocalSettingsStore(tmp_path / "settings.yaml"),
        )
        fact_id = 'memory:b7oykog3jr6iuswihsj3h5x66e'
        row = MemoryQueryRow(
            fact_id=fact_id, memory_domain_id="test", context_id=CTX_GLOBAL,
            fact_kind="FACT", lifecycle="ACTIVE", statement="The room is A.",
            recorded_at="2026-01-01T00:00:00Z", fact_semantic_digest="sha256:" + "0" * 64,
        )
        reads = []
        def get(**kwargs):
            assert kwargs["read_binding"] is binding
            reads.append(kwargs["fact_id"])
            return row if kwargs["fact_id"] == fact_id else None
        port._query = SimpleNamespace(get=get, direct_relations=lambda **_: ())
        async def related(*args, **kwargs):
            return (), {"disposition": "UNAVAILABLE"}
        port._related_for_remember = related
        context = SimpleNamespace(
            session_id=port._session_id, conversation_scope_kind="ROOT",
            memory_context=memory.FrozenModelCallMemoryContext(),
        )
        try:
            result = await port.invoke(tool_name="memory_get", arguments={"target": memory_target(fact_id)}, invocation_context=context)
            target = json.loads(result.content)["target"]
            assert result.model_visible_memory_fact_ids == (fact_id,)
            assert (await port.invoke(tool_name="memory_get", arguments={"target": target}, invocation_context=context)).content == result.content
            written = await port.invoke(tool_name="remember", arguments={
                "statement": "The meeting uses room A.", "context_target": "GLOBAL",
                "kind": "DECISION", "based_on": [target],
            }, invocation_context=context)
            assert written.state == "SUCCESS"
            assert written.memory_mutation.basis_refs == (memory.PreparedMemoryBasisReference(fact_id, CTX_GLOBAL, 0),)
            replacement = memory_target('memory:f4ekao4ik3kbehvawlzrh6b7ke')
            relation = await port.invoke(tool_name="mark_memory_relation", arguments={
                "source": replacement, "target": target, "relation_kind": "SUPERSEDES",
            }, invocation_context=context)
            assert relation.memory_mutation.source_memory_id == replacement["memory_id"]
            assert relation.memory_mutation.target_memory_id == fact_id
            assert reads == [fact_id, fact_id, fact_id]
            for arguments in (
                {"memory_id": fact_id},
                {"target": {**target, "context_target": "GLOBAL"}},
                {"target": {**target, "kind": "SESSION"}},
            ):
                rejected = await port.invoke(tool_name="memory_get", arguments=arguments, invocation_context=context)
                assert rejected.state == "APPLICATION_ERROR"
                assert rejected.memory_mutation is None
            assert reads == [fact_id, fact_id, fact_id]
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
