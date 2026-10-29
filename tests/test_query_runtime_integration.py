"""Query dispatch uses live execution owners without acknowledging their output."""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from pulsara_agent.code_mode import CodeModeError, CodeModeHost, CodeModeToolResult
from pulsara_agent.conversation_kernel.tool_runtime import DirectKernelToolPort
from pulsara_agent.conversation_kernel.tool_surface import BuiltinExecutionPolicyRef
from pulsara_agent.identity import random_id
from pulsara_agent.model_input.contracts import ModelInputScopeKind
from pulsara_agent.ports.tool_execution import encode_tool_public_json
from pulsara_agent.primitives.tool_observation import MODEL_VISIBLE_TOOL_RESULT_MAX_LOGICAL_UTF8_BYTES
from pulsara_agent.primitives.tool_result_projection import conservative_artifact_page_logical_utf8_bytes


def _runtime(host, read_source):
    port = object.__new__(DirectKernelToolPort)
    port._closed = False
    port._session_id = random_id("session")
    port._code_mode = host
    port._code_mode_read_source = read_source
    port._code_mode_archive_output = None
    port._code_observation_settlements = {}
    port._validate_surface_borrow = lambda *args: None
    borrow = SimpleNamespace(
        prepared=SimpleNamespace(access=SimpleNamespace(conversation_scope_kind=ModelInputScopeKind.ROOT)),
        execution_binding=lambda name: SimpleNamespace(tool_name=name, execution_policy=BuiltinExecutionPolicyRef(name, "test-bound-catalog")),
    )
    context = SimpleNamespace(
        session_id=port._session_id, turn_id=random_id("turn"),
        assistant_entry_id=random_id("entry"), tool_call_id="query_test", attempt_id=random_id("attempt"),
        attempt_permission_snapshot_fingerprint="permission", permission_snapshot_fingerprint="permission",
        conversation_scope_kind="ROOT", surface_borrow=borrow, steer_event=None, code_cell_id=None,
    )
    return port, context


async def _dispatch(port, context, name, arguments):
    return await port.invoke(
        tool_name=name, arguments=arguments, tool_call_id=context.tool_call_id,
        attempt_id=context.attempt_id, turn_id=context.turn_id,
        assistant_entry_id=context.assistant_entry_id, invocation_context=context,
    )


def test_cell_source_query_dispatch_pages_unicode_without_taking_output_owner():
    async def run():
        host = CodeModeHost()
        frozen_source = "console.log('retained output');\n/*" + "冻结🙂\n" * 15000 + "*/"
        source_attempt = random_id("attempt")
        reads = []
        async def read_source(**values):
            reads.append(values)
            return frozen_source
        async def no_tools(_):
            return CodeModeToolResult(value={})
        port, context = _runtime(host, read_source)
        try:
            cell_id = await host.start(
                source=frozen_source, source_exec_attempt_id=source_attempt,
                turn_id=context.turn_id, snapshot=object(), tool_catalog={}, invoke=no_tools,
            )
            async with asyncio.timeout(5):
                while host.capture_cell(cell_id).state not in {"completed", "failed", "terminated"}:
                    await asyncio.sleep(.001)
            observation = await host.observe(cell_id, 0)
            assert observation.output
            target = {"kind": "CELL", "cell_id": cell_id}
            inventory = (await _dispatch(port, context, "query_cells", {})).public_value()
            assert inventory["cells"][0]["target"] == target
            assert inventory["cells"][0]["has_unread_output"] is True
            parts, offset = [], 0
            while True:
                result = await _dispatch(port, context, "query_cells", {
                    "target": target, "offset_chars": offset, "max_chars": 32000,
                })
                assert result.state == "SUCCESS" and result.process_local_settlement is None
                value = result.public_value()
                assert value["cell"]["target"] == target
                assert value["cell"]["has_unread_output"] is True
                assert len(encode_tool_public_json(value)) + conservative_artifact_page_logical_utf8_bytes(
                    tool_call_id=context.tool_call_id, body="", model_visible_memory_ids=(),
                ) <= MODEL_VISIBLE_TOOL_RESULT_MAX_LOGICAL_UTF8_BYTES
                page = value["source"]
                parts.append(page["text"])
                assert page["returned_chars"] == len(page["text"])
                if not page["has_more"]:
                    break
                assert page["next_offset_chars"] > offset
                offset = page["next_offset_chars"]
            assert "".join(parts) == frozen_source
            assert reads and all(values == {"session_id": context.session_id, "attempt_id": source_attempt}
                                 for values in reads)
            # The existing reserved output page remains owned and unchanged.
            with pytest.raises(CodeModeError, match="Another reader owns"):
                await host.observe(cell_id, 0)
            await host.acknowledge(observation)
            async with asyncio.timeout(5):
                while host.index():
                    await asyncio.sleep(.001)
            unavailable = await _dispatch(port, context, "query_cells", {"target": target})
            assert unavailable.state == "APPLICATION_ERROR"
            assert unavailable.public_value()["error"] == "unavailable"
        finally:
            await host.aclose()
    asyncio.run(run())


def test_source_read_failure_preserves_live_metadata_and_manage_stop_does_not_read_output():
    async def run():
        host = CodeModeHost()
        started, finish = asyncio.Event(), asyncio.Event()
        async def invoke(_):
            started.set()
            await finish.wait()
            return CodeModeToolResult(value={})
        async def read_source(**_):
            raise ValueError("canonical content digest mismatch")
        port, context = _runtime(host, read_source)
        try:
            cell_id = await host.start(
                source="console.log('keep'); await tools.read_file({});",
                source_exec_attempt_id=random_id("attempt"), turn_id=context.turn_id,
                snapshot=object(), tool_catalog={"read_file": {}}, invoke=invoke,
            )
            await asyncio.wait_for(started.wait(), 5)
            target = {"kind": "CELL", "cell_id": cell_id}
            result = await _dispatch(port, context, "query_cells", {"target": target})
            assert result.public_value()["source"] == {
                "status": "UNAVAILABLE", "reason": "canonical content digest mismatch"}
            assert result.public_value()["cell"]["pending_calls"] == 1
            stopped = await _dispatch(port, context, "manage_cell", {"action": "stop", "target": target})
            assert stopped.state == "SUCCESS" and stopped.process_local_settlement is None
            assert stopped.public_value()["target"] == target
            assert host.capture_cell(cell_id).has_unread_output is True
            finish.set()
        finally:
            finish.set()
            await host.aclose()
    asyncio.run(run())


def test_process_and_monitor_query_dispatch_reuses_live_owners(tmp_path):
    from pulsara_agent.conversation_kernel.live import LiveAgentEventBus
    from pulsara_agent.conversation_kernel.tool_contracts import ProcessLocalEffectSettlementDisposition
    from pulsara_agent.primitives.permission import PermissionMode

    async def run():
        session_id, host_id = random_id("session"), random_id("host")
        port = DirectKernelToolPort(
            workspace_root=tmp_path, host_owner_id=host_id, session_id=session_id,
            authorization_policy=SimpleNamespace(), live_bus=LiveAgentEventBus(),
        )
        _, context = _runtime(None, None)
        context.session_id = session_id
        context.effective_permission_mode = PermissionMode.BYPASS_PERMISSIONS
        context.permission_confirmation_granted = False
        context.result_entry_id = random_id("entry")
        context.host_owner_epoch = host_id
        context.authorization_reference = "test:query-runtime"
        port._validate_surface_borrow = lambda *args: None
        try:
            started = await _dispatch(port, context, "exec_process", {
                "command": "printf 'kept log'; read query_input", "yield_time_ms": 0,
            })
            target = started.public_value()["target"]
            detail = await _dispatch(port, context, "query_processes", {"target": target})
            process = detail.public_value()["process"]
            assert process["target"] == target and process["command"] == "printf 'kept log'; read query_input"
            assert "output" not in process and "process_id" not in process
            inventory = (await _dispatch(port, context, "query_processes", {})).public_value()
            assert inventory["terminal_process_action"] == "list"
            assert [item["target"] for item in inventory["processes"]] == [target]
            registered = await _dispatch(port, context, "manage_monitor", {
                "action": "register", "target": target,
                "conditions": {"output": {"min_new_output_chars": 19, "quiet_period_ms": 77}},
            })
            assert registered.process_local_settlement is not None
            await port.settle_process_local_effect(
                registered.process_local_settlement, ProcessLocalEffectSettlementDisposition.COMMITTED)
            monitor_target = registered.public_value()["target"]
            registration = port._terminal_monitor._registrations[monitor_target["monitor_id"]]
            before = (registration.expires_at_monotonic, registration.last_accepted_cursor,
                      registration.observation_ordinal)
            monitor = (await _dispatch(port, context, "query_monitors", {"target": monitor_target})).public_value()["monitor"]
            assert monitor["target"] == monitor_target and monitor["process_target"] == target
            assert monitor["conditions"]["output"] == {"min_new_output_chars": 19, "quiet_period_ms": 77}
            assert before == (registration.expires_at_monotonic, registration.last_accepted_cursor,
                              registration.observation_ordinal)
            cancelled = await _dispatch(port, context, "manage_monitor", {"action": "cancel", "target": monitor_target})
            assert cancelled.public_value()["status"] == "CANCELLED"
            missing = await _dispatch(port, context, "query_monitors", {"target": monitor_target})
            assert missing.state == "APPLICATION_ERROR"
            assert (await _dispatch(port, context, "query_processes", {"target": target})).public_value()["process"]["status"] == "running"
        finally:
            await port.aclose()
    asyncio.run(run())
