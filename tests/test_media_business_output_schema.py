"""Generated SDK declarations describe complete producer values through QuickJS."""

import asyncio
import json

from jsonschema import Draft202012Validator

from pulsara_agent.code_mode.contracts import CodeModeToolResult
from pulsara_agent.code_mode.host import CodeModeHost
from pulsara_agent.identity import random_id
from pulsara_agent.ports.tool_execution import thaw_tool_json_object
from pulsara_agent.tools.builtins.media import media_business_output_schema


async def validate_media_js_value(name, value, state):
    value = thaw_tool_json_object(value)
    schema = media_business_output_schema(name)
    Draft202012Validator.check_schema(schema)
    validator = Draft202012Validator(schema)
    validator.validate(value)

    async def invoke(_):
        return CodeModeToolResult(
            value=value,
            state=state,
            error_code=None if state == "SUCCESS" else state,
            error_message=None if state == "SUCCESS" else "Actual media failure",
        )

    host = CodeModeHost()
    try:
        cell = await host.start(
            source=(
                f"let value; try {{value=await tools[{json.dumps(name)}]({{}});}}"
                "catch(err){value=err.result;} console.log(value);"
            ),
            source_exec_attempt_id=random_id("attempt"),
            turn_id=random_id("turn"),
            snapshot=object(),
            decision_available=False,
            tool_catalog={name: {"parameters": {}}, "decide": {"parameters": {}}},
            invoke=invoke,
        )
        await host.wait_state(cell, 30_000)
        lease = await host.lease_output(cell)
        assert lease is not None
        returned = lease.record["values"][0]["value"]
        assert returned == value
        validator.validate(returned)
        await host.acknowledge_output(lease)
    finally:
        await host.aclose()


def test_actual_subtitle_business_value_and_cell_preserve_boundary_wording(tmp_path):
    from tests.test_hardcut_media_contract import _execute

    (tmp_path / "captions.srt").write_text(
        "1\n00:00:00,000 --> 00:00:02,000\nOriginal 完整文字\n\n"
        "2\n00:00:02,000 --> 00:00:04,000\nNext wording\n",
        encoding="utf-8",
    )
    result = asyncio.run(
        _execute(
            tmp_path,
            "read_transcript",
            {"target": {"kind": "PATH", "path": "captions.srt"}, "from": 1, "to": 3},
        )
    )
    assert result.state == "SUCCESS"
    segments = [p.value for p in result.content.content if p.type == "object"]
    assert [item["text"] for item in segments] == ["Original 完整文字", "Next wording"]
    assert segments[0]["visible_range"] == {"from": 1, "to": 2}


def test_actual_transcript_failure_and_cell_have_no_invented_source(tmp_path):
    from tests.test_hardcut_media_contract import _execute

    result = asyncio.run(
        _execute(
            tmp_path,
            "read_transcript",
            {"target": {"kind": "PATH", "path": "missing.wav"}},
        )
    )
    assert result.state == "APPLICATION_ERROR"
    assert result.content.content[0].type == "text"
