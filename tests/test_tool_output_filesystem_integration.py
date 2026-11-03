from __future__ import annotations

import asyncio
import json

import pytest

from pulsara_agent.conversation_kernel.tool_policy import (
    DefaultToolDispatchAuthorizationPolicy,
)
from pulsara_agent.conversation_kernel.repository import ConversationKernelRepository
from tests.support.postgres import verified_postgres_provider
from tests.test_tool_output_filesystem import BodyPort


@pytest.mark.parametrize("child", [False, True])
def test_source_write_cancel_after_publication_keeps_exact_single_outcome(
    tmp_path, monkeypatch, child
):
    from threading import Event
    from pulsara_agent.conversation_kernel.live import LiveAgentEventBus
    from pulsara_agent.conversation_kernel.tool_runtime import DirectKernelToolPort
    from pulsara_agent.model_input.contracts import ModelInputScopeKind
    from pulsara_agent.tools.builtins import filesystem as owner
    from tests.support.round3 import invoke_direct_tool

    published = Event()
    release = Event()
    writes = []
    actual_create = owner._atomic_create_bytes

    def create(path, content):
        actual_create(path, content)
        writes.append(path)
        published.set()
        release.wait()

    monkeypatch.setattr(owner, "_atomic_create_bytes", create)

    source = BodyPort(session_id="session:yovkoffbw6sro643t4zfhazd24")

    async def run():
        port = DirectKernelToolPort(
            workspace_root=tmp_path,
            host_owner_id="host:m2lzworel4eoc2mmilyyzj7nxm",
            session_id="session:yovkoffbw6sro643t4zfhazd24",
            live_bus=LiveAgentEventBus(),
            authorization_policy=DefaultToolDispatchAuthorizationPolicy(),
            result_source_reader=source,
        )
        operation = asyncio.create_task(
            invoke_direct_tool(
                port,
                session_id="session:yovkoffbw6sro643t4zfhazd24",
                tool_name="write_file",
                arguments={
                    "target": {"kind": "PATH", "path": "output.txt"},
                    "source": source.target,
                },
                tool_call_id="call:export",
                attempt_id="attempt:u3xrx7rpcwqzvgtl24sz2q7u7y",
                turn_id="turn:kkdrrjecidt6ni3sgr7kc4p6ge",
                assistant_entry_id="entry:5nuy4yboonqd6fi6ppzl54b42i",
                conversation_scope_kind=ModelInputScopeKind.SUBAGENT
                if child
                else ModelInputScopeKind.ROOT,
            )
        )
        try:
            assert await asyncio.to_thread(published.wait, 5)
            operation.cancel()
            await asyncio.sleep(0)
            release.set()
            result = await operation
            assert result.state == "SUCCESS"
            assert result.caller_cancelled_while_running
            assert result.page_complete
            assert json.loads(result.content)["status"] == "ok"
            assert writes == [tmp_path / "output.txt"]
            assert (tmp_path / "output.txt").read_bytes() == b"retained text"
        finally:
            release.set()
            await port.aclose()

    asyncio.run(run())


def test_source_write_available_to_root_and_child_with_same_scoped_owner(tmp_path):
    from pulsara_agent.conversation_kernel.live import LiveAgentEventBus
    from pulsara_agent.conversation_kernel.tool_runtime import DirectKernelToolPort
    from pulsara_agent.model_input.contracts import ModelInputScopeKind
    from tests.support.round3 import prepare_test_direct_tool_surface

    source = BodyPort(session_id="session:yovkoffbw6sro643t4zfhazd24")

    async def run():
        port = DirectKernelToolPort(
            workspace_root=tmp_path,
            host_owner_id="host:m2lzworel4eoc2mmilyyzj7nxm",
            session_id="session:yovkoffbw6sro643t4zfhazd24",
            live_bus=LiveAgentEventBus(),
            authorization_policy=DefaultToolDispatchAuthorizationPolicy(),
            result_source_reader=source,
        )
        try:
            for scope, task in [
                (ModelInputScopeKind.ROOT, None),
                (ModelInputScopeKind.SUBAGENT, None),
            ]:
                surface = prepare_test_direct_tool_surface(
                    port, conversation_scope_kind=scope
                )
                assert {"read_file", "write_file"}.issubset(
                    {s.name for s in surface.model_surface.tool_specs}
                )
        finally:
            await port.aclose()

    asyncio.run(run())


@pytest.mark.parametrize("terminal_available", [True, False])
def test_source_write_runtime_dispatch_delivery_and_prefix_continuity(
    stage2_migrated_postgres_database, tmp_path, terminal_available
):
    from pulsara_agent.conversation_kernel.live import LiveAgentEventBus
    from pulsara_agent.conversation_kernel.runner import ConversationKernelRunner
    from pulsara_agent.conversation_kernel.tool_runtime import DirectKernelToolPort
    from pulsara_agent.llm.input import LLMTextPart, MessageRole
    from tests.support.model_config import (
        frozen_test_prompt,
        test_model_resolution_snapshot,
    )
    from tests.support.round3 import (
        ScriptedKernelModel,
        StaticContextSourceCollector,
        seal_test_direct_tool_port,
    )
    from tests.test_round5_long_horizon_postgres import (
        _lease,
        _tool_stream,
        _text_stream,
    )

    provider = verified_postgres_provider(stage2_migrated_postgres_database.runtime_dsn)
    repository = ConversationKernelRepository(provider)
    session_id, _, lease = _lease(repository)
    source = BodyPort(b"exact source", session_id=session_id)
    model = ScriptedKernelModel(
        [
            _tool_stream(
                1,
                tool_name="write_file",
                arguments=json.dumps(
                    {
                        "target": {"kind": "PATH", "path": "result.txt"},
                        "source": source.target,
                    }
                ),
            ),
            _tool_stream(
                2,
                tool_name="read_file",
                arguments='{"target":{"kind":"PATH","path":"result.txt"}}',
            ),
            _text_stream("source checked", block_id="answer"),
        ]
    )

    async def run():
        port = DirectKernelToolPort(
            workspace_root=tmp_path,
            host_owner_id="host:m2lzworel4eoc2mmilyyzj7nxm",
            session_id=session_id,
            live_bus=LiveAgentEventBus(),
            authorization_policy=DefaultToolDispatchAuthorizationPolicy(),
            result_source_reader=source,
        )
        if not terminal_available:
            # Model the unavailable capability before the first epoch installs
            # its surface; availability never mutates an installed prefix.
            for name in ("exec_process", "manage_process"):
                port._tools.pop(name, None)
        seal_test_direct_tool_port(port)
        runner = ConversationKernelRunner(
            model_resolution_snapshot_provider=test_model_resolution_snapshot,
            repository=repository,
            writer_lease=lease,
            model=model,
            tools=port,
            live_bus=LiveAgentEventBus(),
            context_source_collector=StaticContextSourceCollector(),
        )
        try:
            result = await runner.run_turn(
                frozen_test_prompt("export the saved result and inspect its file")
            )
            assert result.final_text == "source checked"
        finally:
            await port.aclose()

    asyncio.run(run())
    assert (tmp_path / "result.txt").read_bytes() == b"exact source"
    bodies = [
        json.loads(part.text)["pulsara_tool_result"]
        for message in model.requests[-1].compiled_input.messages
        if message.role is MessageRole.TOOL_RESULT
        for part in message.content
        if isinstance(part, LLMTextPart)
    ]
    exported = json.loads(bodies[0]["body"])
    assert exported["target"] == {"kind": "PATH", "path": "result.txt"}
    assert exported["source_coverage"] == "COMPLETE"
    assert "model_visible_memory_ids" not in bodies[0]
    assert "exact source" in bodies[1]["body"]
    for before, after in zip(model.requests, model.requests[1:]):
        assert after.compiled_input.tools == before.compiled_input.tools
        assert after.compiled_input.system_prompt == before.compiled_input.system_prompt
        assert (
            after.compiled_input.messages[: len(before.compiled_input.messages)]
            == before.compiled_input.messages
        )
