"""Actual producer values and QuickJS failures satisfy the visible SDK contract."""

import asyncio

from jsonschema import Draft202012Validator

from pulsara_agent.code_mode import CodeModeHost, CodeModeToolResult
from pulsara_agent.conversation_kernel.tool_runtime_results import _kernel_tool_state
from pulsara_agent.identity import random_id
from pulsara_agent.ports.tool_execution import ToolCall, thaw_tool_json_object
from pulsara_agent.tools.builtins.filesystem import ReadFileTool
from pulsara_agent.tools.builtins.filesystem_contracts import (
    file_tool_result_schema, read_file_parameters_schema,
)
from tests.support.tool_result_schemas import validate_business_result
from tests.test_query_runtime_integration import runtime, dispatch


def test_actual_file_result_and_error_survive_the_cell_bridge_whole(tmp_path):
    (tmp_path / "source.txt").write_bytes(b"\xef\xbb\xbfline one\r\nline two\n")
    tool = ReadFileTool(tmp_path)
    schema = file_tool_result_schema("read_file")

    async def run():
        host = CodeModeHost()
        produced = []

        async def invoke(call):
            result = tool.execute(ToolCall("provider-call", "read_file", call.arguments))
            state = _kernel_tool_state(result.status)
            value = validate_business_result(schema, result.business_value, state)
            produced.append(value)
            return CodeModeToolResult(value=value, state=state,
                error_code="APPLICATION_ERROR" if state != "SUCCESS" else None,
                error_message="actual file read failed" if state != "SUCCESS" else None)

        try:
            cell = await host.start(
                source='console.log(await tools.read_file({target:{kind:"PATH",path:"source.txt"}}));'
                    'try{await tools.read_file({target:{kind:"PATH",path:"missing.txt"}});}'
                    'catch(err){console.log(err.result);}',
                source_exec_attempt_id=random_id("attempt"), turn_id=random_id("turn"),
                snapshot=object(), decision_available=False,
                tool_catalog={"read_file": {"parameters": read_file_parameters_schema(native=False)},
                    "decide": {"parameters": {}}},
                invoke=invoke,
            )
            await host.wait_state(cell, 30_000)
            observed = []
            while lease := await host.lease_output(cell):
                record = thaw_tool_json_object(lease.record)
                assert record["kind"] == "log"
                value = record["values"][0]["value"]
                Draft202012Validator(schema).validate(value)
                observed.append(value)
                await host.acknowledge_output(lease)
                if cell not in host._cells:
                    break
            assert observed == produced and len(observed) == 2
            assert [value["meta"]["status"] for value in observed] == ["SUCCESS", "ERROR"]
        finally:
            await host.aclose()

    asyncio.run(run())


def test_actual_terminal_running_exited_query_and_failure_business_values(tmp_path):
    async def run():
        port = runtime(tmp_path)
        try:
            started = await dispatch(port, "exec_process", {
                "command": "read value; printf '%s\\n' \"$value\"; exit 9", "yield_time_ms": 0,
            })
            target = started.content.public_fields["target"]
            assert started.content.public_fields["meta"]["process_status"] == "RUNNING"
            await dispatch(port, "query_processes", {"target": target})
            await dispatch(port, "manage_process", {"target": target, "action": "wait", "timeout_seconds": 0})
            written = await dispatch(port, "manage_process", {
                "target": target, "action": "write", "data": "received 中文\n", "yield_time_ms": 1000,
            })
            assert written.state == "SUCCESS" and written.content.public_fields["meta"]["exit_code"] == 9
            await dispatch(port, "query_processes", {})
            missing = await dispatch(port, "query_processes", {
                "target": {"kind": "PROCESS", "process_id": random_id("process")},
            })
            assert missing.state == "APPLICATION_ERROR"
            failed = await dispatch(port, "exec_process", {"command": "true", "workdir": "missing-directory"})
            assert failed.state == "APPLICATION_ERROR"
        finally:
            await port.aclose()

    asyncio.run(run())
