import asyncio
import json

import pytest

from pulsara_agent.primitives.permission import PermissionMode
from pulsara_agent.primitives.run_permission import RunPermissionAdmissionSource, build_run_permission_snapshot
from pulsara_agent.conversation_kernel.memory.contracts import FrozenModelCallMemoryContext
from pulsara_agent.conversation_kernel.tool_contracts import KernelToolAuthorizationKind

from pulsara_agent.capability.builtin_catalog import builtin_tool_catalog_entry
from pulsara_agent.conversation_kernel.live import LiveAgentEventBus
from pulsara_agent.conversation_kernel.tool_policy import DefaultToolDispatchAuthorizationPolicy
from pulsara_agent.conversation_kernel.tool_runtime import DirectKernelToolPort
from pulsara_agent.conversation_kernel.tool_contracts import PreparedToolPreparationRejection
from pulsara_agent.model_input.contracts import ModelInputScopeKind
from pulsara_agent.tools.builtins.presentation import OpenInPulsaraTool
from tests.support.round3 import direct_tool_invocation_context, prepare_test_direct_tool_surface


@pytest.mark.parametrize('arguments,rejected', [
    ({'targets': []}, True), ({'targets': 'file'}, True), ({}, True),
    ({'targets': [{'kind': 'PANEL', 'name': 'tasks'}], 'session_id': 'other'}, True),
    ({'targets': [None, {'kind': 'PATH', 'path': 'missing #?.md'}, {'kind': 'URL', 'url': 'file:///tmp/a'}, {'kind': 'PROCESS', 'process_id': 'p1'}, {'kind': 'PANEL', 'name': 'tasks'}]}, False),
    ({'targets': [None, {'kind': 'PATH', 'path': ''}]}, False),
    ({'targets': [{'kind': 'PANEL', 'name': 'browser'}]}, False),
    ({'targets': [None], 'session_id': 'other'}, True),
])
def test_real_tool_gate_and_invocation(tmp_path, arguments, rejected):
    async def run():
        port = DirectKernelToolPort(workspace_root=tmp_path, host_owner_id='host:test', session_id='session:test', live_bus=LiveAgentEventBus(), authorization_policy=DefaultToolDispatchAuthorizationPolicy())
        ids = dict(session_id='session:test', tool_name='open_in_pulsara', tool_call_id='call:test', attempt_id='attempt:test', turn_id='turn:test', assistant_entry_id='entry:test')
        borrow, context = direct_tool_invocation_context(port, **ids)
        try:
            prepared = port.prepare_resolved_invocation(tool_name='open_in_pulsara', arguments=arguments, surface_borrow=borrow)
            assert isinstance(prepared, PreparedToolPreparationRejection) is rejected
            permission = build_run_permission_snapshot(
                snapshot_id='permission:test', requested_mode=PermissionMode.BYPASS_PERMISSIONS,
                effective_mode=PermissionMode.BYPASS_PERMISSIONS,
                admission_source=RunPermissionAdmissionSource.USER_SUBMISSION,
            )
            authorization = await port.authorize(
                tool_name=ids['tool_name'], arguments=arguments, tool_call_id=ids['tool_call_id'],
                turn_id=ids['turn_id'], assistant_entry_id=ids['assistant_entry_id'],
                permission_snapshot=permission, surface_borrow=borrow,
                memory_context=FrozenModelCallMemoryContext(),
            )
            assert authorization.kind is (KernelToolAuthorizationKind.INVALID_ARGUMENTS if rejected else KernelToolAuthorizationKind.ALLOW)
            if rejected:
                return
            result = await port.invoke(**{k: v for k, v in ids.items() if k != 'session_id'}, arguments=arguments, invocation_context=context)
            assert result.state == 'SUCCESS'
            value = json.loads(result.content)
            if arguments['targets'] == [{'kind': 'PANEL', 'name': 'browser'}]:
                assert value == {'targets': [{'target': {'kind': 'PANEL', 'name': 'browser'}}]}
            elif len(arguments['targets']) == 2:
                assert value['targets'] == []
                assert [e['index'] for e in value['errors']] == [0, 1]
            else:
                assert [e['index'] for e in value['errors']] == [0, 2, 4]
                assert value['targets'] == [
                    {'target': {'kind': 'PATH', 'path': str(tmp_path / 'missing #?.md')}},
                    {'target': {'kind': 'PROCESS', 'process_id': 'p1'}, 'host_session_id': 'host:test'},
                ]
        finally:
            borrow.close()
            await port.aclose(timeout_seconds=2)
    asyncio.run(run())


def test_root_surface_and_recovery(tmp_path):
    async def run():
        port = DirectKernelToolPort(workspace_root=tmp_path, host_owner_id='host:test', session_id='session:test', live_bus=LiveAgentEventBus(), authorization_policy=DefaultToolDispatchAuthorizationPolicy())
        try:
            prepared = prepare_test_direct_tool_surface(port, conversation_scope_kind=ModelInputScopeKind.SUBAGENT)
            borrow = port.borrow_tool_surface(prepared)
            try:
                with pytest.raises((RuntimeError, KeyError)):
                    borrow.execution_binding('open_in_pulsara')
            finally:
                borrow.close()
            assert builtin_tool_catalog_entry('open_in_pulsara').recovery_contract.severity == 'read_only'
        finally:
            await port.aclose(timeout_seconds=2)
    asyncio.run(run())


def test_normalization_does_not_read_or_resolve_links(tmp_path):
    tool = OpenInPulsaraTool(tmp_path, host_session_id='host:test')
    (tmp_path / 'link').symlink_to(tmp_path / 'absent')
    assert tool.normalize_target({'kind': 'PATH', 'path': 'link/a.md'})['target']['path'] == str(tmp_path / 'link/a.md')
    with pytest.raises(ValueError) as failure:
        tool.normalize_target({'kind': 'URL', 'url': 'https://username:secret@example.test/'})
    assert 'secret' not in str(failure.value)
    assert tool.normalize_target({'kind': 'URL', 'url': ' https://example.test/a#b '})['target']['url'] == 'https://example.test/a#b'
    with pytest.raises(ValueError):
        tool.normalize_target({'kind': 'PANEL', 'name': 'tasks', 'session_id': 'other'})


def test_clean_import_order():
    import subprocess
    import sys
    subprocess.run([sys.executable, "-c", "from pulsara_agent.tools.builtins.presentation import OpenInPulsaraTool"], check=True)


def test_real_tool_rejects_removed_wrappers_without_aliases(tmp_path):
    async def run():
        task_id = 'task:6d7i5mg4ymfcckogzrqiser5km'
        port = DirectKernelToolPort(workspace_root=tmp_path, host_owner_id='host:test', session_id='session:test', live_bus=LiveAgentEventBus(), authorization_policy=DefaultToolDispatchAuthorizationPolicy())
        ids = dict(session_id='session:test', tool_name='open_in_pulsara', tool_call_id='call:test', attempt_id='attempt:test', turn_id='turn:test', assistant_entry_id='entry:test')
        borrow, context = direct_tool_invocation_context(port, **ids)
        arguments = {'targets': [
            {'type': 'task', 'task_id': task_id},
            {'type': 'task', 'task_id': task_id, 'session_id': 'other'},
            {'type': 'task', 'task_id': 'invented'},
            {'type': 'task'},
            {'type': 'unknown', 'name': 'tasks'},
            {'kind': 'PANEL', 'name': 'tasks'},
        ]}
        try:
            assert not isinstance(port.prepare_resolved_invocation(tool_name='open_in_pulsara', arguments=arguments, surface_borrow=borrow), PreparedToolPreparationRejection)
            result = await port.invoke(**{k: v for k, v in ids.items() if k != 'session_id'}, arguments=arguments, invocation_context=context)
            value = json.loads(result.content)
            assert value['targets'] == []
            assert [error['index'] for error in value['errors']] == [0, 1, 2, 3, 4, 5]
        finally:
            borrow.close()
            await port.aclose(timeout_seconds=2)
    asyncio.run(run())


def test_object_schema_reuses_canonical_targets_without_view_or_work():
    from pulsara_agent.code_mode.targets import cell_target_schema
    from pulsara_agent.conversation_kernel.subagents.collaboration_contracts import target_schema
    schema = json.loads(json.dumps(builtin_tool_catalog_entry("open_in_pulsara").descriptor.input_schema))
    assert set(schema["properties"]) == {"targets"}
    assert schema["additionalProperties"] is False
    branches = schema["properties"]["targets"]["items"]["anyOf"]
    assert target_schema("AGENT") in branches
    assert cell_target_schema() in branches
    assert "maxItems" not in schema["properties"]["targets"]
    assert {item["properties"]["kind"]["const"] for item in branches} == {
        "AGENT", "CELL", "PATH", "URL", "PROCESS", "MCP_SERVER", "PANEL",
    }


def test_object_requests_use_resolved_calling_context_and_keep_original_indexes(tmp_path):
    from pulsara_agent.conversation_kernel.presentation_targets import ResolvedPresentationObject
    from pulsara_agent.ports.tool_execution import ToolCall
    agent_id = "session:" + "a" * 26
    retired_id = "session:" + "b" * 25 + "a"
    cell_id = "cell:" + "a" * 26
    tool = OpenInPulsaraTool(tmp_path, root_session_id="session:root", host_session_id="host:original")
    resolved = []
    async def resolve(kind, identity):
        resolved.append((kind, identity))
        if identity == retired_id:
            raise LookupError("Agent retired")
        return ResolvedPresentationObject(kind, identity, "session:root", "host:original" if kind == "CELL" else None)
    arguments = {"targets": [
        {"kind": "AGENT", "agent_id": agent_id},
        {"kind": "AGENT", "agent_id": retired_id},
        {"kind": "CELL", "cell_id": cell_id, "view": "graph"},
        {"kind": "CELL", "cell_id": cell_id},
        {"kind": "PANEL", "name": "subagents"},
        {"kind": "PANEL", "name": "cells"},
        {"kind": "WORK", "work_id": "queue:" + "a" * 26},
        {"kind": "PANEL", "name": "cell_graphs"},
        {"kind": "URL", "url": "https://example.test"},
    ]}
    result = asyncio.run(tool.execute_async(ToolCall(id="call:test", name=tool.name, arguments=arguments), resolve_object=resolve))
    value = json.loads(result.output)
    assert [error["index"] for error in value["errors"]] == [1, 2, 6, 7]
    assert resolved == [("AGENT", agent_id), ("AGENT", retired_id), ("CELL", cell_id)]
    assert value["targets"] == [
        {"target": arguments["targets"][0], "root_session_id": "session:root"},
        {"target": arguments["targets"][3], "root_session_id": "session:root", "host_session_id": "host:original"},
        {"target": arguments["targets"][4], "root_session_id": "session:root"},
        {"target": arguments["targets"][5], "root_session_id": "session:root", "host_session_id": "host:original"},
        {"target": arguments["targets"][8]},
    ]


def test_shared_object_reader_rejects_root_and_other_root_without_touching_lifecycle():
    from types import SimpleNamespace
    from unittest.mock import Mock
    from pulsara_agent.conversation_kernel.presentation_targets import resolve_visible_agent, resolve_visible_cell
    agent = "session:" + "a" * 26
    repository = SimpleNamespace(read_agent_runtime_state=Mock(return_value={"root_session_id": "session:root"}))
    assert resolve_visible_agent(repository, root_session_id="session:root", agent_session_id=agent, deadline_monotonic=123) == {"root_session_id": "session:root"}
    repository.read_agent_runtime_state.assert_called_once_with(session_id=agent, deadline_monotonic=123)
    for root in (agent, "session:other"):
        with pytest.raises(LookupError):
            resolve_visible_agent(repository, root_session_id=root, agent_session_id=agent, deadline_monotonic=123)
    owner = SimpleNamespace(capture_code_cell=Mock(return_value="capture"))
    cell = "cell:" + "a" * 26
    assert resolve_visible_cell(owner, cell_id=cell) == "capture"
    owner.capture_code_cell.assert_called_once_with(cell)


def test_new_targets_reach_provider_lowering_with_closed_shapes():
    from jsonschema import Draft202012Validator
    from pulsara_agent.llm.adapters.openai.function_tools import lower_openai_function_parameters
    schema = json.loads(json.dumps(builtin_tool_catalog_entry("open_in_pulsara").descriptor.input_schema))
    validator = Draft202012Validator(lower_openai_function_parameters(schema))
    agent = {"kind": "AGENT", "agent_id": "session:" + "a" * 26}
    cell = {"kind": "CELL", "cell_id": "cell:" + "a" * 26}
    for target in [agent, cell, {"kind": "PANEL", "name": "subagents"}, {"kind": "PANEL", "name": "cells"}]:
        assert validator.is_valid({"targets": [target]})
        assert not validator.is_valid({"targets": [{**target, "view": "graph"}]})
    assert not validator.is_valid({"targets": [cell], "root_session_id": "other"})


def test_real_dispatch_resolves_objects_through_common_read_owners(tmp_path):
    from types import SimpleNamespace
    from unittest.mock import Mock
    from pulsara_agent.conversation_kernel.subagents.collaboration_api import CollaborationToolPort
    from pulsara_agent.code_mode.contracts import CodeModeError

    async def run():
        root = "session:" + "a" * 26
        agent = "session:" + "b" * 25 + "a"
        other = "session:" + "c" * 25 + "a"
        cell = "cell:" + "a" * 26
        retired = "cell:" + "b" * 25 + "a"
        reads = []
        def read_agent(**kwargs):
            reads.append(kwargs)
            return {"root_session_id": root if kwargs["session_id"] == agent else other}
        async def read(method, **kwargs):
            return method(**kwargs)
        port = DirectKernelToolPort(workspace_root=tmp_path, host_owner_id="host:original", session_id=root, live_bus=LiveAgentEventBus(), authorization_policy=DefaultToolDispatchAuthorizationPolicy())
        collaboration = CollaborationToolPort(repository=SimpleNamespace(read_agent_runtime_state=read_agent), root_session_id=root, session_id=root, runtime=SimpleNamespace(read_deadline=lambda: 123), admission_facts=lambda *_: {}, io=SimpleNamespace(run=read))
        port.bind_subagent_port(collaboration)
        def capture(identity):
            if identity == retired:
                raise CodeModeError("cell_not_found", "Cell retired")
            return object()
        port.capture_code_cell = Mock(side_effect=capture)
        ids = dict(session_id=root, tool_name="open_in_pulsara", tool_call_id="call:test", attempt_id="attempt:test", turn_id="turn:test", assistant_entry_id="entry:test")
        borrow, context = direct_tool_invocation_context(port, **ids)
        try:
            result = await port.invoke(**{k: v for k, v in ids.items() if k != "session_id"}, arguments={"targets": [
                {"kind": "AGENT", "agent_id": agent}, {"kind": "AGENT", "agent_id": other},
                {"kind": "AGENT", "agent_id": root}, {"kind": "CELL", "cell_id": retired},
                {"kind": "CELL", "cell_id": cell}, {"kind": "PANEL", "name": "cells"},
            ]}, invocation_context=context)
            value = json.loads(result.content)
            assert result.state == "SUCCESS"
            assert [error["index"] for error in value["errors"]] == [1, 2, 3]
            assert value["targets"] == [
                {"target": {"kind": "AGENT", "agent_id": agent}, "root_session_id": root},
                {"target": {"kind": "CELL", "cell_id": cell}, "root_session_id": root, "host_session_id": "host:original"},
                {"target": {"kind": "PANEL", "name": "cells"}, "root_session_id": root, "host_session_id": "host:original"},
            ]
            assert [item["session_id"] for item in reads] == [agent, other]
            assert [call.args for call in port.capture_code_cell.call_args_list] == [(retired,), (cell,)]
        finally:
            borrow.close()
            await port.aclose(timeout_seconds=2)
    asyncio.run(run())


def test_known_reader_outage_fails_one_target_and_programming_error_still_raises(tmp_path):
    from types import SimpleNamespace
    from unittest.mock import Mock
    from psycopg import OperationalError
    from pulsara_agent.conversation_kernel.presentation_targets import resolve_visible_agent
    from pulsara_agent.ports.tool_execution import ToolCall
    agent = "session:" + "a" * 26
    reader = Mock(side_effect=OperationalError("connection closed"))
    repository = SimpleNamespace(read_agent_runtime_state=reader)
    tool = OpenInPulsaraTool(tmp_path, root_session_id="session:root", host_session_id="host:test")
    async def resolve(kind, identity):
        return resolve_visible_agent(repository, root_session_id=tool.root_session_id, agent_session_id=identity, deadline_monotonic=123)
    call = ToolCall(id="call:test", name=tool.name, arguments={"targets": [
        {"kind": "PANEL", "name": "capabilities"},
        {"kind": "AGENT", "agent_id": agent},
        {"kind": "PANEL", "name": "browser"},
    ]})
    value = json.loads(asyncio.run(tool.execute_async(call, resolve_object=resolve)).output)
    assert value["targets"] == [{"target": call.arguments["targets"][0]}, {"target": call.arguments["targets"][2]}]
    assert value["errors"] == [{"index": 1, "message": "Agent reader is temporarily unavailable"}]
    reader.side_effect = TypeError("invalid repository row")
    with pytest.raises(TypeError, match="invalid repository row"):
        asyncio.run(tool.execute_async(call, resolve_object=resolve))
