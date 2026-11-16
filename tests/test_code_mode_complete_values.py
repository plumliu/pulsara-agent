"""Real QuickJS bridge full values and explicit-output boundaries."""

import asyncio

from pulsara_agent.code_mode.contracts import CodeModeToolResult
from pulsara_agent.code_mode.host import CodeModeHost
from pulsara_agent.code_mode.output import project_output_record, project_output_values
from pulsara_agent.identity import random_id


def run(source, invoke):
    async def scenario():
        host = CodeModeHost()
        try:
            cell = await host.start(
                source=source,
                source_exec_attempt_id=random_id("attempt"),
                turn_id=random_id("turn"),
                snapshot=object(),
                decision_available=False,
                tool_catalog={
                    name: {"parameters": {}} for name in ("decide", "sample")
                },
                invoke=invoke,
            )
            await host.wait_state(cell, 30_000)
            records = []
            while lease := await host.lease_output(cell):
                records.append(dict(lease.record))
                await host.acknowledge_output(lease)
                if cell not in host._cells:
                    break
            return records
        finally:
            await host.aclose()

    return asyncio.run(scenario())


def test_js_receives_value_larger_than_old_single_value_boundary_without_disclosure():
    value = {
        "meta": {"status": "SUCCESS"},
        "content": [{"type": "text", "value": "x" * (5 << 20)}],
    }

    async def invoke(_):
        return CodeModeToolResult(value=value)

    records = run(
        "const r=await tools.sample({}); console.log(r.content[0].value.length); return 'done';",
        invoke,
    )
    assert records == [
        {"kind": "log", "values": [{"type": "value", "value": 5 << 20}]},
        {"kind": "return", "values": [{"type": "text", "value": "done"}]},
    ]


def test_actual_failed_business_value_is_available_as_err_result():
    value = {
        "meta": {"status": "ERROR"},
        "content": [{"type": "object", "value": {"partial": [None, False, 3]}}],
    }

    async def invoke(_):
        return CodeModeToolResult(
            value=value,
            state="APPLICATION_ERROR",
            error_code="APPLICATION_ERROR",
            error_message="actual failure",
        )

    records = run(
        "try{await tools.sample({});}catch(e){console.log(e.result);}", invoke
    )
    assert records == [{"kind": "log", "values": [{"type": "value", "value": value}]}]


def test_physical_failure_keeps_actual_effect_and_honest_err_result(tmp_path):
    from pulsara_agent.conversation_kernel.tool_execution import _tool_invocation_failure

    path = tmp_path / "effect.txt"
    calls = 0
    observed = []

    async def invoke(_):
        nonlocal calls
        calls += 1
        path.write_text("effect happened")
        try:
            raise OSError("result inspection failed after write")
        except OSError as error:
            failure = _tool_invocation_failure(error, "bounded_write")
        value = failure.public_value()
        observed.append(value)
        return CodeModeToolResult(value=value, state=failure.state,
            error_code=failure.state, error_message="physical result unavailable")

    records = run("try{await tools.sample({});}catch(e){console.log(e.result);}", invoke)
    assert calls == 1 and path.read_text() == "effect happened"
    assert records == [{"kind": "log", "values": [{"type": "value", "value": observed[0]}]}]
    assert observed[0]["meta"]["status"] == "ERROR"
    text = "\n".join(part["value"] for part in observed[0]["content"])
    assert "OSError: result inspection failed after write" in text
    assert "操作可能已部分或全部执行，实际结果未确认" in text
    assert "未自动重试" in text


def test_failure_without_business_value_does_not_invent_err_result():
    async def invoke(_):
        return CodeModeToolResult(
            has_result=False,
            state="FAILED",
            error_code="transport",
            error_message="not dispatched",
        )

    records = run(
        "try{await tools.sample({});}catch(e){console.log(Object.hasOwn(e,'result'));}",
        invoke,
    )
    assert records == [{"kind": "log", "values": [{"type": "value", "value": False}]}]


def test_output_recognizes_nested_image_occurrences_but_never_reference_strings():
    image = {
        "type": "image",
        "value": {"kind": "IMAGE_REF", "image_ref": random_id("image")},
    }
    values = [
        {
            "type": "value",
            "value": {
                "nested": [image, {"type": "object", "value": {"again": image}}],
                "bare": image["value"],
                "string": '{"type":"image"}',
            },
        }
    ]
    parts = project_output_values(values)
    assert [part["node"] for part in parts if part["type"] == "image"] == [image, image]
    text = "".join(part["value"] for part in parts if part["type"] == "text")
    assert (
        '"nested"' in text
        and '"again"' in text
        and '"bare"' in text
        and image["value"]["image_ref"] in text
    )


def test_content_occurrences_and_log_arguments_preserve_exact_order_and_separators():
    kind, parts = project_output_record(
        {
            "kind": "log",
            "values": [
                {
                    "type": "value",
                    "value": {
                        "content": [
                            {"type": "text", "value": "123"},
                            {"type": "text", "value": "456"},
                        ]
                    },
                },
                {"type": "text", "value": "tail"},
            ],
        }
    )
    assert kind == "log"
    assert "".join(part["value"] for part in parts) == "123456 tail"


def test_only_reported_values_are_scanned_and_getters_run_in_guest_once():
    image = {
        "type": "image",
        "value": {"kind": "IMAGE_REF", "image_ref": random_id("image")},
    }

    async def invoke(_):
        return CodeModeToolResult(value={"content": [image]})

    records = run(
        "const hidden=await tools.sample({});let seen=0;console.log({get count(){return ++seen}});return seen;",
        invoke,
    )
    assert records == [
        {"kind": "log", "values": [{"type": "value", "value": {"count": 1}}]},
        {"kind": "return", "values": [{"type": "value", "value": 1}]},
    ]
    assert all(
        part["type"] == "text"
        for record in records
        for part in project_output_record(record)[1]
    )


def test_nested_actual_image_is_not_lost_under_an_ordinary_target_field():
    image = {
        "type": "image",
        "value": {"kind": "IMAGE_REF", "image_ref": random_id("image")},
    }
    parts = project_output_values(
        [
            {
                "type": "value",
                "value": {
                    "type": "target",
                    "value": {"kind": "PATH", "comparison": image},
                },
            }
        ]
    )
    assert [part["node"] for part in parts if part["type"] == "image"] == [image]


def test_explicit_output_larger_than_old_bridge_cap_is_delivered_as_one_record():
    async def invoke(_):
        raise AssertionError("no tool call expected")

    records = run('return "x".repeat(5<<20);', invoke)
    assert len(records) == 1 and records[0]["kind"] == "return"
    assert records[0]["values"] == [{"type": "text", "value": "x" * (5 << 20)}]


def test_abnormal_exit_preserves_undelivered_full_result_without_reexecution():
    async def scenario():
        host = CodeModeHost()
        entered, release = asyncio.Event(), asyncio.Event()
        value = {
            "meta": {"status": "SUCCESS"},
            "content": [{"type": "text", "value": "complete late result"}],
        }
        calls = []

        async def invoke(_):
            calls.append(None)
            entered.set()
            await release.wait()
            return CodeModeToolResult(value=value)

        try:
            cell = await host.start(
                source="const pending=tools.sample({});throw Error('script failed');",
                source_exec_attempt_id=random_id("attempt"),
                turn_id=random_id("turn"),
                snapshot=object(),
                decision_available=False,
                tool_catalog={"decide": {}, "sample": {}},
                invoke=invoke,
            )
            await entered.wait()
            owned = host._cell(cell)
            await host.wait_state(cell, 30_000)
            tasks = [item.task for item in owned.pending.values()]
            release.set()
            await asyncio.gather(*tasks)
            records = []
            while lease := await host.lease_output(cell):
                records.append(dict(lease.record))
                await host.acknowledge_output(lease)
                if cell not in host._cells:
                    break
            assert len(calls) == 1
            assert any(
                record.get("error", {}).get("message") == "script failed"
                for record in records
            )
            assert [
                record["value"] for record in records if record["kind"] == "tool_result"
            ] == [value]
        finally:
            release.set()
            await host.aclose()

    asyncio.run(scenario())


def test_explicit_stop_suppresses_late_output_but_preserves_effect_settlement():
    async def scenario():
        host = CodeModeHost()
        entered, release, settled = asyncio.Event(), asyncio.Event(), asyncio.Event()

        async def invoke(_):
            entered.set()
            await release.wait()
            settled.set()
            return CodeModeToolResult(
                value={"meta": {"status": "SUCCESS"}, "content": []}
            )

        try:
            cell = await host.start(
                source='await tools.sample({});console.log("must not resume");',
                source_exec_attempt_id=random_id("attempt"),
                turn_id=random_id("turn"),
                snapshot=object(),
                decision_available=False,
                tool_catalog={"decide": {}, "sample": {}},
                invoke=invoke,
            )
            await entered.wait()
            tasks = [item.task for item in host._cell(cell).pending.values()]
            stopped = await host.stop(cell)
            assert stopped["state"] == "terminated"
            release.set()
            await asyncio.gather(*tasks)
            assert settled.is_set() and host.pending_output_cells() == ()
        finally:
            release.set()
            await host.aclose()

    asyncio.run(scenario())


def test_prepared_image_data_uses_existing_output_reservation_and_releases_once():
    from dataclasses import replace
    from pulsara_agent.code_mode.contracts import CodeModeLimits
    from pulsara_agent.llm.input import LLMImagePart
    from pulsara_agent.ports.terminal_observation import CellOutputContent

    async def scenario():
        host = CodeModeHost(
            limits=replace(
                CodeModeLimits(),
                cell_bytes=256 << 10,
                host_bytes=512 << 10,
                control_bytes=4096,
            )
        )
        image = LLMImagePart("image/png", b"x" * (96 << 10), 1, 1, random_id("image"))
        sent = []
        send = host._send

        async def observed(message):
            sent.append(dict(message))
            await send(message)

        host._send = observed

        async def prepare(lease):
            return CellOutputContent(
                lease.cell_id,
                lease.source_turn_id,
                lease.source_exec_attempt_id,
                lease.record_sequence,
                {
                    "kind": "log",
                    "content": [
                        {
                            "type": "image",
                            "value": {
                                "kind": "IMAGE_REF",
                                "image_ref": image.image_ref,
                            },
                        }
                    ],
                },
                (image,),
            )

        host.set_output_preparer(prepare)

        async def invoke(_):
            return CodeModeToolResult(value=None)

        try:
            cell = await host.start(
                source='console.log("image");',
                source_exec_attempt_id=random_id("attempt"),
                turn_id=random_id("turn"),
                snapshot=None,
                decision_available=False,
                tool_catalog={"decide": {"parameters": {}}},
                invoke=invoke,
            )
            await host.wait_state(cell, 30000)
            lease = await host.lease_output(cell)
            assert lease.content.images == (image,)
            retained = [m["bytes"] for m in sent if m["type"] == "retain"]
            assert len(retained) == 1 and retained[0] >= len(image.immutable_bytes)
            assert await host.lease_output(cell) is lease
            assert len([m for m in sent if m["type"] == "retain"]) == 1
            charge = host._cell(cell).output[0].charge
            await host.acknowledge_output(lease)
            assert any(m["type"] == "release" and m["bytes"] == charge for m in sent)
            async with asyncio.timeout(5):
                while cell in host._cells:
                    await asyncio.sleep(0.001)
            assert not host.pending_output_cells()
        finally:
            await host.aclose()

    asyncio.run(scenario())


def test_prepared_image_resource_rejection_settles_without_retrying_output():
    from dataclasses import replace
    from pulsara_agent.code_mode.contracts import CodeModeLimits
    from pulsara_agent.llm.input import LLMImagePart
    from pulsara_agent.ports.terminal_observation import CellOutputContent

    async def scenario():
        host = CodeModeHost(
            limits=replace(
                CodeModeLimits(),
                cell_bytes=128 << 10,
                host_bytes=256 << 10,
                control_bytes=4096,
            )
        )
        image = LLMImagePart("image/png", b"x" * (192 << 10), 1, 1, random_id("image"))
        prepared = []

        async def prepare(lease):
            prepared.append(lease.record_sequence)
            return CellOutputContent(
                lease.cell_id,
                lease.source_turn_id,
                lease.source_exec_attempt_id,
                lease.record_sequence,
                {
                    "kind": "log",
                    "content": [
                        {
                            "type": "image",
                            "value": {
                                "kind": "IMAGE_REF",
                                "image_ref": image.image_ref,
                            },
                        }
                    ],
                },
                (image,),
            )

        async def quote(_):
            return len(image.immutable_bytes) + 500

        host.set_output_preparer(prepare, quote=quote)

        async def invoke(_):
            return CodeModeToolResult(value=None)

        try:
            cell = await host.start(
                source='console.log("image");',
                source_exec_attempt_id=random_id("attempt"),
                turn_id=random_id("turn"),
                snapshot=None,
                decision_available=False,
                tool_catalog={"decide": {"parameters": {}}},
                invoke=invoke,
            )
            await host.wait_state(cell, 30000)
            owned = host._cell(cell)
            assert await host.lease_output(cell) is None
            assert prepared == []
            assert owned.state == "failed" and owned.error["code"] == "data_boundary"
            assert not owned.output and owned.output_lease is None
            assert cell not in host.pending_output_cells()
            async with asyncio.timeout(5):
                while cell in host._cells:
                    await asyncio.sleep(0.001)
        finally:
            await host.aclose()

    asyncio.run(scenario())


def test_save_failure_rejects_js_call_with_actual_success_business_value(monkeypatch):
    from pulsara_agent.ports.tool_output import ToolOutputUnavailableReason, ToolResultSaveError
    from tests.test_hardcut_source_consumers import invoke_code_mode_failure

    known = {"target": {"kind": "PATH", "path": "effect.txt"},
             "meta": {"status": "SUCCESS", "bytes_written": 15},
             "content": [{"type": "text", "value": "已创建文件。"}]}
    failure = ToolResultSaveError(reason=ToolOutputUnavailableReason.BODY_PUBLICATION_FAILED,
        known_value=known, result_state="SUCCESS", cause=OSError("storage write failed"))
    # Actual invoke_code_mode error adapter, then the actual Host/QuickJS bridge.
    result = invoke_code_mode_failure(monkeypatch, failure)
    invoked = []

    async def invoke(_):
        invoked.append(None)
        return result

    records = run("try {await tools.sample({}); throw Error('unexpected resolve');} "
                  "catch(e) {console.log({name:e.name,message:e.message,result:e.result});}", invoke)
    assert len(invoked) == 1
    actual = records[0]["values"][0]["value"]
    assert actual["name"] == "ToolError" and actual["result"] == known
    assert "不应重做业务操作" in actual["message"]
    assert result.result_id is None and result.result_entry_id is None
