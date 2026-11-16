from tests.support.model_config import scheduled_task_service
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
from pulsara_agent.identity import derive_id

SESSION_ID = derive_id("session", "test-fixture", "open-in-pulsara")




@pytest.mark.parametrize('arguments,rejected', [
    ({'targets': []}, True), ({'targets': 'file'}, True), ({}, True),
    ({'targets': [{'kind': 'PANEL', 'name': 'tasks'}], 'session_id': 'other'}, True),
    ({'targets': [None, {'kind': 'PATH', 'path': 'missing #?.md'}, {'kind': 'URL', 'url': 'file:///tmp/a'}, {'kind': 'PROCESS', 'process_id': 'process:aaaaaaaaaaaaaaaaaaaaaaaaaa'}, {'kind': 'PANEL', 'name': 'tasks'}]}, False),
    ({'targets': [None, {'kind': 'PATH', 'path': ''}]}, False),
    ({'targets': [{'kind': 'PANEL', 'name': 'browser'}]}, False),
    ({'targets': [None], 'session_id': 'other'}, True),
])
def test_real_tool_gate_and_invocation(tmp_path, arguments, rejected):
    async def run():
        port = DirectKernelToolPort(scheduled_tasks=scheduled_task_service(), workspace_root=tmp_path, host_owner_id='host:aaaaaaaaaaaaaaaaaaaaaaaaaa', session_id=SESSION_ID, live_bus=LiveAgentEventBus(), authorization_policy=DefaultToolDispatchAuthorizationPolicy())
        ids = dict(session_id=SESSION_ID, tool_name='open_in_pulsara', tool_call_id='call:test', attempt_id='attempt:test', turn_id='turn:test', assistant_entry_id='entry:test')
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
            assert result.state == 'APPLICATION_ERROR'
            text = "\n".join(part.value for part in result.content.content)
            if len(arguments['targets']) == 5:
                assert result.result_facts["presentation_targets"] == (
                    {"target":{"kind":"PATH","path":str(tmp_path/'missing #?.md')}},)
                assert "已接受 targets[1]" in text
                assert all(f"targets[{index}] 未接受" in text for index in (0,2,3,4))
            else:
                assert result.result_facts == {}
                assert all(f"targets[{index}] 未接受" in text for index in range(len(arguments['targets'])))
        finally:
            borrow.close()
            await port.aclose(timeout_seconds=2)
    asyncio.run(run())


def test_root_surface_and_recovery(tmp_path):
    async def run():
        port = DirectKernelToolPort(scheduled_tasks=scheduled_task_service(), workspace_root=tmp_path, host_owner_id='host:aaaaaaaaaaaaaaaaaaaaaaaaaa', session_id=SESSION_ID, live_bus=LiveAgentEventBus(), authorization_policy=DefaultToolDispatchAuthorizationPolicy())
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
    tool = OpenInPulsaraTool(tmp_path, host_session_id='host:aaaaaaaaaaaaaaaaaaaaaaaaaa')
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
        port = DirectKernelToolPort(scheduled_tasks=scheduled_task_service(), workspace_root=tmp_path, host_owner_id='host:aaaaaaaaaaaaaaaaaaaaaaaaaa', session_id=SESSION_ID, live_bus=LiveAgentEventBus(), authorization_policy=DefaultToolDispatchAuthorizationPolicy())
        ids = dict(session_id=SESSION_ID, tool_name='open_in_pulsara', tool_call_id='call:test', attempt_id='attempt:test', turn_id='turn:test', assistant_entry_id='entry:test')
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
            assert result.state == "APPLICATION_ERROR"
            assert result.result_facts == {}
            text = "\n".join(part.value for part in result.content.content)
            assert all(f"targets[{index}] 未接受" in text for index in range(6))
        finally:
            borrow.close()
            await port.aclose(timeout_seconds=2)
    asyncio.run(run())


def test_schema_exposes_only_files_webpages_and_canonical_agents():
    from pulsara_agent.conversation_kernel.subagents.collaboration_contracts import target_schema
    schema = json.loads(json.dumps(builtin_tool_catalog_entry("open_in_pulsara").descriptor.input_schema))
    assert set(schema["properties"]) == {"targets"}
    assert schema["additionalProperties"] is False
    branches = schema["properties"]["targets"]["items"]["anyOf"]
    assert target_schema("AGENT") in branches
    assert "maxItems" not in schema["properties"]["targets"]
    assert {item["properties"]["kind"]["const"] for item in branches} == {
        "PATH", "URL", "AGENT",
    }


def test_object_requests_use_resolved_calling_context_and_keep_original_indexes(tmp_path):
    from pulsara_agent.conversation_kernel.presentation_targets import ResolvedPresentationObject
    from pulsara_agent.ports.tool_execution import ToolCall
    agent_id = "session:" + "a" * 26
    retired_id = "session:" + "b" * 25 + "a"
    cell_id = "cell:" + "a" * 26
    tool = OpenInPulsaraTool(tmp_path, root_session_id="session:ddddddddddddddddddddddddda", host_session_id="host:aaaaaaaaaaaaaaaaaaaaaaaaaa")
    resolved = []
    async def resolve(kind, identity):
        resolved.append((kind, identity))
        if identity == retired_id:
            raise LookupError("Agent retired")
        return ResolvedPresentationObject(kind, identity, "session:ddddddddddddddddddddddddda", "host:aaaaaaaaaaaaaaaaaaaaaaaaaa" if kind == "CELL" else None)
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
    from pulsara_agent.message import ToolResultState
    assert result.status is ToolResultState.ERROR
    text = "\n".join(part.value for part in result.business_value.content)
    assert all(f"targets[{index}] 未接受" in text for index in range(1,8))
    assert "已接受 targets[0]、targets[8]" in text
    assert resolved == [("AGENT",agent_id),("AGENT",retired_id)]
    assert result.result_facts["presentation_targets"] == (
        {"target":arguments["targets"][0],"root_session_id":tool.root_session_id},
        {"target":arguments["targets"][8]},
    )


def test_shared_object_reader_rejects_root_and_other_root_without_touching_lifecycle():
    from types import SimpleNamespace
    from unittest.mock import Mock
    from pulsara_agent.conversation_kernel.presentation_targets import resolve_visible_agent, resolve_visible_cell
    agent = "session:" + "a" * 26
    repository = SimpleNamespace(read_agent_runtime_state=Mock(return_value={"root_session_id": "session:ddddddddddddddddddddddddda"}))
    assert resolve_visible_agent(repository, root_session_id="session:ddddddddddddddddddddddddda", agent_session_id=agent, deadline_monotonic=123) == {"root_session_id": "session:ddddddddddddddddddddddddda"}
    repository.read_agent_runtime_state.assert_called_once_with(session_id=agent, deadline_monotonic=123)
    for root in (agent, "session:other"):
        with pytest.raises(LookupError):
            resolve_visible_agent(repository, root_session_id=root, agent_session_id=agent, deadline_monotonic=123)
    owner = SimpleNamespace(capture_code_cell=Mock(return_value="capture"))
    cell = "cell:" + "a" * 26
    assert resolve_visible_cell(owner, cell_id=cell) == "capture"
    owner.capture_code_cell.assert_called_once_with(cell)


@pytest.mark.parametrize("lowered", [False, True])
def test_public_targets_have_closed_shapes_and_exclude_internal_views(lowered):
    from jsonschema import Draft202012Validator
    from pulsara_agent.llm.adapters.openai.function_tools import lower_openai_function_parameters
    schema = json.loads(json.dumps(builtin_tool_catalog_entry("open_in_pulsara").descriptor.input_schema))
    validator = Draft202012Validator(lower_openai_function_parameters(schema) if lowered else schema)
    targets = [
        {"kind": "PATH", "path": "report.md"},
        {"kind": "URL", "url": "https://example.test"},
        {"kind": "AGENT", "agent_id": "session:" + "a" * 26},
    ]
    assert validator.is_valid({"targets": targets})
    for target in targets:
        assert validator.is_valid({"targets": [target]})
        assert not validator.is_valid({"targets": [{**target, "view": "graph"}]})
    excluded = [
        {"kind": "PROCESS", "process_id": "process:" + "a" * 26},
        {"kind": "MCP_SERVER", "runtime_server_id": "mcp-server:" + "a" * 26},
        {"kind": "CELL", "cell_id": "cell:" + "a" * 26},
        *({"kind": "PANEL", "name": name} for name in
          ["capabilities", "background", "subagents", "cells", "browser"]),
    ]
    for target in excluded:
        assert not validator.is_valid({"targets": [target]})
    assert not validator.is_valid({"targets": targets, "root_session_id": "other"})


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
        port = DirectKernelToolPort(scheduled_tasks=scheduled_task_service(), workspace_root=tmp_path, host_owner_id="host:aaaaaaaaaaaaaaaaaaaaaaaaaa", session_id=root, live_bus=LiveAgentEventBus(), authorization_policy=DefaultToolDispatchAuthorizationPolicy())
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
            assert result.state == "APPLICATION_ERROR"
            text = "\n".join(part.value for part in result.content.content)
            assert all(f"targets[{index}] 未接受" in text for index in range(1,6))
            assert result.result_facts["presentation_targets"] == (
                {"target":{"kind":"AGENT","agent_id":agent},"root_session_id":root},)
            assert [item["session_id"] for item in reads] == [agent,other]
            port.capture_code_cell.assert_not_called()
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
    tool = OpenInPulsaraTool(tmp_path, root_session_id="session:ddddddddddddddddddddddddda", host_session_id="host:aaaaaaaaaaaaaaaaaaaaaaaaaa")
    async def resolve(kind, identity):
        return resolve_visible_agent(repository, root_session_id=tool.root_session_id, agent_session_id=identity, deadline_monotonic=123)
    call = ToolCall(id="call:test", name=tool.name, arguments={"targets": [
        {"kind": "URL", "url": "https://one.example.test"},
        {"kind": "AGENT", "agent_id": agent},
        {"kind": "URL", "url": "https://two.example.test"},
    ]})
    result = asyncio.run(tool.execute_async(call,resolve_object=resolve))
    from pulsara_agent.message import ToolResultState
    assert result.status is ToolResultState.ERROR
    assert result.result_facts["presentation_targets"] == ({"target":call.arguments["targets"][0]},{"target":call.arguments["targets"][2]})
    text = "\n".join(part.value for part in result.business_value.content)
    assert "targets[1] 未接受：Agent reader is temporarily unavailable" in text
    reader.side_effect = TypeError("invalid repository row")
    with pytest.raises(TypeError, match="invalid repository row"):
        asyncio.run(tool.execute_async(call, resolve_object=resolve))


def test_source_target_schema_is_structural_and_parser_checks_shared_identity():
    from jsonschema import Draft202012Validator
    from pulsara_agent.ports.tool_output import parse_tool_output_target, session_entry_source_target_schema
    valid=derive_id("entry","test-schema","object")
    validator=Draft202012Validator(session_entry_source_target_schema())
    target={"kind":"SESSION_ENTRY","entry_id":valid}
    assert validator.is_valid(target)
    assert parse_tool_output_target(target)==target
    for identity in (valid+"\n",derive_id("memory","test-schema","object")):
        supplied={"kind":"SESSION_ENTRY","entry_id":identity}
        assert validator.is_valid(supplied)
        with pytest.raises(ValueError):
            parse_tool_output_target(supplied)
    assert not validator.is_valid({"kind":"SESSION_ENTRY","entry_id":None})


def test_object_parsers_check_exact_shared_identity_without_format_repair():
    from pulsara_agent.code_mode.targets import parse_cell_target
    from pulsara_agent.memory.targets import parse_memory_target
    for parser,kind,field,identity,wrong in (
        (parse_cell_target,"CELL","cell_id",derive_id("cell","test-target","valid"),derive_id("process","test-target","wrong")),
        (parse_memory_target,"MEMORY","memory_id",derive_id("memory","test-target","valid"),derive_id("entry","test-target","wrong")),
    ):
        assert parser({"kind":kind,field:identity})
        for value in (identity+"\n",wrong):
            with pytest.raises(ValueError):
                parser({"kind":kind,field:value})
