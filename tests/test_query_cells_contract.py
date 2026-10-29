"""Cell addresses, live queries and canonical frozen-source contract."""
from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import datetime, timezone
import json
import shlex
import sys
from time import monotonic
from types import SimpleNamespace

import pytest
from jsonschema import Draft202012Validator, ValidationError

from pulsara_agent.code_mode import CodeModeError, CodeModeHost, CodeModeLimits, CodeModeToolResult
from pulsara_agent.code_mode.contracts import (
    exec_cell_parameters_schema, manage_cell_parameters_schema, query_cells_parameters_schema,
)
from pulsara_agent.code_mode.targets import cell_target, parse_cell_target
from pulsara_agent.conversation_kernel.tool_runtime import DirectKernelToolPort, _validate_builtin_call_schema
from pulsara_agent.identity import is_valid_id, random_id
from pulsara_agent.ports.tool_execution import encode_tool_public_json


@pytest.mark.parametrize("value", [
    "cell_" + "a" * 32, "cell:" + "A" * 26, "cell:" + "a" * 25,
    "cell:" + "a" * 25 + "b", "process:" + "a" * 26,
])
def test_cell_target_rejects_noncanonical_ids_and_padding(value):
    with pytest.raises(ValueError):
        parse_cell_target({"kind": "CELL", "cell_id": value})


@pytest.mark.parametrize("name,schema,valid,invalid", [
    ("exec_cell", exec_cell_parameters_schema,
     [{"code": ""}, {"path": "code.js", "yield_time_ms": 0}],
     [{}, {"code": "", "path": "code.js"}, {"code": None}, {"code": "", "yield_time_ms": True}]),
    ("query_cells", query_cells_parameters_schema,
     [{}, {"max_items": 100000}, {"cursor": "cell:" + "a" * 26},
      {"target": cell_target("cell:" + "a" * 26), "offset_chars": 0, "max_chars": 32000}],
     [{"target": None}, {"max_items": 0}, {"max_items": True}, {"cursor": None},
      {"offset_chars": 0}, {"target": cell_target("cell:" + "a" * 26), "max_items": 50},
      {"target": cell_target("cell:" + "a" * 26), "max_chars": 32001}]),
    ("manage_cell", manage_cell_parameters_schema,
     [{"action": "wait", "target": cell_target("cell:" + "a" * 26), "yield_time_ms": 30000},
      {"action": "stop", "target": cell_target("cell:" + "a" * 26)}],
     [{"action": "stop", "target": cell_target("cell:" + "a" * 26), "yield_time_ms": 0},
      {"action": "wait", "cell_id": "cell:" + "a" * 26},
      {"action": "list"}]),
])
def test_cell_canonical_schema_and_dispatch_validation_agree(name, schema, valid, invalid):
    validator = Draft202012Validator(schema())
    for value in valid:
        validator.validate(value)
        _validate_builtin_call_schema(name, schema(), value)
    for value in invalid:
        assert list(validator.iter_errors(value)), value
        with pytest.raises(ValidationError):
            _validate_builtin_call_schema(name, schema(), value)


async def _start(host, source, invoke=None):
    async def empty(_):
        return CodeModeToolResult(value={})
    return await host.start(source=source, source_exec_attempt_id=random_id("attempt"),
        turn_id=random_id("turn"), snapshot=object(), tool_catalog={"read_file": {}},
        invoke=invoke or empty)


async def _terminal(host, cell_id):
    async with asyncio.timeout(15):
        while host.capture_cell(cell_id).state not in {"completed", "failed", "terminated"}:
            await asyncio.sleep(.001)


def test_real_host_inventory_pages_no_total_limit_and_released_cursor_remains_usable():
    async def run():
        host = CodeModeHost(limits=replace(CodeModeLimits(), vm_slots=1))
        foreign = CodeModeHost()
        entered, release = asyncio.Event(), asyncio.Event()
        async def wait(_):
            entered.set()
            await release.wait()
            return CodeModeToolResult(value={})
        try:
            first = await _start(host, "await tools.read_file({});", wait)
            await entered.wait()
            ids = [first] + [await _start(host, "console.log('queued');") for _ in range(56)]
            foreign_id = await _start(foreign, "console.log('other Host');")
            assert len(set(ids)) == 57 and all(is_valid_id(value, "cell") for value in ids)
            initial = host.query_cells()
            assert len(initial["cells"]) == 50 and initial["has_more"]
            boundary = initial["next_cursor"]
            assert boundary == initial["cells"][-1]["target"]["cell_id"]
            following = host.query_cells(cursor=boundary)
            all_ids = [row["target"]["cell_id"] for row in initial["cells"] + following["cells"]]
            assert all_ids == sorted(ids) and following["next_cursor"] is None
            assert len(host.query_cells(max_items=10**6)["cells"]) == 57
            small = host.query_cells(max_items=10**6, maximum_page_bytes=500)
            assert 0 < len(small["cells"]) < 50 and small["has_more"]
            assert len(encode_tool_public_json(small)) <= 500
            assert small["next_cursor"] == small["cells"][-1]["target"]["cell_id"]
            with pytest.raises(CodeModeError, match="row exceeds"):
                host.query_cells(maximum_page_bytes=100)
            with pytest.raises(CodeModeError) as missing:
                host.capture_cell(foreign_id)
            assert missing.value.code == "unavailable"
            # Stopping and draining a queued boundary does not invalidate its
            # string position for a subsequent live inventory page.
            if boundary != first:
                await host.stop(boundary)
                await host._cells[boundary].vm_released.wait()
                page = await host.observe(boundary, 0)
                await host.acknowledge(page)
                assert boundary not in host._cells
                assert host.query_cells(cursor=boundary) == following
        finally:
            release.set()
            await host.aclose()
            await foreign.aclose()
    asyncio.run(run())


def test_live_query_does_not_reserve_or_consume_output_and_node_uses_same_canonical_id():
    async def run():
        host = CodeModeHost()
        calls = []
        async def invoke(call):
            calls.append(call)
            return CodeModeToolResult(value=1)
        try:
            source = "console.log(await tools.read_file({}));"
            cell_id = await _start(host, source, invoke)
            second = await _start(host, source, invoke)
            await _terminal(host, cell_id)
            await _terminal(host, second)
            assert second != cell_id
            assert {call.cell_id for call in calls} == {cell_id, second}
            reserved = await host.observe(cell_id, 0)
            captured = host.capture_cell(cell_id)
            assert captured.has_unread_output and captured.public_value()["target"] == cell_target(cell_id)
            assert host.query_cells()["cells"]
            stopped = await host.stop(cell_id)
            assert stopped["target"] == cell_target(cell_id) and stopped["has_more"]
            assert not host._cells[cell_id].terminal_delivered
            host.abandon(reserved)
            again = await host.observe(cell_id, 0)
            assert again.output == reserved.output
            await host._cells[cell_id].vm_released.wait()
            await host.acknowledge(again)
            assert captured.public_value()["has_unread_output"] is True
            with pytest.raises(CodeModeError) as missing:
                host.capture_cell(cell_id)
            assert missing.value.code == "unavailable"
        finally:
            await host.aclose()
    asyncio.run(run())


def test_complete_target_output_envelope_utf8_boundary_and_failed_quote_preserve_output():
    async def run():
        host = CodeModeHost()
        try:
            cell_id = await _start(host, "console.log('中文😀'.repeat(100));")
            await _terminal(host, cell_id)
            cell = host._cells[cell_id]
            expected = host._public_page(cell, [cell.output[0].value], False)
            size = len(encode_tool_public_json(expected))
            with pytest.raises(CodeModeError) as failure:
                await host.observe(cell_id, 0, maximum_page_bytes=size - 1)
            assert failure.value.code == "output_resource_error"
            assert cell.observation is None and cell.output and not cell.terminal_delivered
            page = await host.observe(cell_id, 0, maximum_page_bytes=size)
            assert page.public_value() == expected
            assert len(encode_tool_public_json(page.public_value())) == size
            assert "cell_id" not in page.public_value()
            host.abandon(page)
            repeated = await host.observe(cell_id, 0, maximum_page_bytes=size)
            assert repeated.output == page.output
        finally:
            await host.aclose()
    asyncio.run(run())


def test_query_source_unicode_pages_read_only_and_captured_source_survives_release():
    async def run():
        host = CodeModeHost()
        # Exercise the actual query adapter and source callback without another
        # durable source store; PostgreSQL hydration is covered below.
        port = object.__new__(DirectKernelToolPort)
        port._code_mode = host
        source = "// Hook后的冻结源码😀\n" * 5000
        read_started, read_release = asyncio.Event(), asyncio.Event()
        async def read_source(**_):
            read_started.set()
            await read_release.wait()
            return source
        port._code_mode_read_source = read_source
        context = SimpleNamespace(tool_call_id=random_id("call"), session_id=random_id("session"))
        try:
            cell_id = await _start(host, "console.log('unread output');")
            await _terminal(host, cell_id)
            target = cell_target(cell_id)
            reserved = await host.observe(cell_id, 0)
            read_release.set()
            offset, pieces = 0, []
            while True:
                value = await port._query_cells({"target": target, "offset_chars": offset,
                                               "max_chars": 32000}, context)
                assert len(encode_tool_public_json(value)) <= 40000
                assert value["cell"]["has_unread_output"]
                page = value["source"]
                assert page["returned_chars"] == len(page["text"])
                pieces.append(page["text"])
                if not page["has_more"]:
                    assert page["next_offset_chars"] is None
                    break
                assert page["next_offset_chars"] == offset + len(page["text"])
                offset = page["next_offset_chars"]
            assert "".join(pieces) == source
            for offset in (len(source), len(source) + 10):
                page = (await port._query_cells({"target": target, "offset_chars": offset}, context))["source"]
                assert page["text"] == "" and not page["has_more"] and page["returned_chars"] == 0
            read_started.clear()
            read_release.clear()
            query = asyncio.create_task(port._query_cells({"target": target}, context))
            await read_started.wait()
            await host._cells[cell_id].vm_released.wait()
            await host.acknowledge(reserved)
            assert cell_id not in host._cells
            read_release.set()
            captured_page = (await query)["source"]
            assert captured_page["text"] == source[:captured_page["returned_chars"]]
            with pytest.raises(CodeModeError) as missing:
                await port._query_cells({"target": target}, context)
            assert missing.value.code == "unavailable"
        finally:
            read_release.set()
            await host.aclose()
    asyncio.run(run())


@pytest.mark.parametrize("failure", [None, OSError("frozen source blob is missing")])
def test_missing_source_preserves_cell_metadata_and_does_not_consume_output(failure):
    async def run():
        host = CodeModeHost()
        port = object.__new__(DirectKernelToolPort)
        port._code_mode = host
        async def read_source(**_):
            if failure is not None:
                raise failure
            return None
        port._code_mode_read_source = read_source
        try:
            cell_id = await _start(host, "console.log('retained');")
            await _terminal(host, cell_id)
            value = await port._query_cells({"target": cell_target(cell_id)}, SimpleNamespace(
                tool_call_id=random_id("call"), session_id=random_id("session")))
            assert value["cell"]["target"] == cell_target(cell_id)
            assert value["cell"]["has_unread_output"]
            assert value["source"]["status"] == "UNAVAILABLE"
            assert value["source"]["reason"] and "text" not in value["source"]
            page = await host.observe(cell_id, 0)
            assert page.output == ({"kind": "log", "text": "retained"},)
            host.abandon(page)
        finally:
            await host.aclose()
    asyncio.run(run())


def _freeze_source(database, text, *, blob):
    from pulsara_agent.conversation_kernel.blob import PostgresCanonicalBlobStore
    from pulsara_agent.conversation_kernel.contracts import InlineContent
    from pulsara_agent.conversation_kernel.repository import AssistantToolCallBlock
    from pulsara_agent.llm.input import FrozenPromptContent
    from pulsara_agent.primitives.context import freeze_json
    from tests.test_stage2_conversation_kernel_postgres import (
        _repository, _start_root_turn, _assistant_permission_fingerprint,
    )
    repository = _repository(database)
    deadline = monotonic() + 30
    now = datetime.now(timezone.utc)
    workspace_id = random_id("workspace")
    lease = repository.acquire_host_writer(intent="NEW", session_id=random_id("session"),
        workspace_id=workspace_id, writer_owner_id=random_id("host"),
        lease_seconds=30, deadline_monotonic=deadline)
    turn_id, entry_id, call_id, attempt_id = (random_id(kind) for kind in ("turn", "entry", "call", "attempt"))
    _start_root_turn(repository, lease.guard, command_id=random_id("command"), turn_id=turn_id,
        entry_id=random_id("entry"), context_binding_revision_id=random_id("binding"),
        content=FrozenPromptContent.text("Run the source"), occurred_at=now, deadline_monotonic=deadline)
    cut = repository.prepare_provider_input_cut(lease.guard, turn_id=turn_id, deadline_monotonic=deadline)
    repository.commit_assistant_message(lease.guard, cut=cut, entry_id=entry_id,
        parent_content=InlineContent.from_bytes(b"execute"), blocks=(AssistantToolCallBlock(
            block_id=random_id("block"), tool_call_id=call_id, tool_name="exec_cell",
            arguments=freeze_json({"path": "changed-after-exec.js"})),),
        occurred_at=now, actor_id="model:test", deadline_monotonic=deadline)
    permission = _assistant_permission_fingerprint(repository, lease.guard, entry_id, deadline)
    repository.accept_tool_attempt(lease.guard, attempt_id=attempt_id, assistant_entry_id=entry_id,
        tool_call_id=call_id, authorization_kind="policy", authorization_reference="test", actor_kind="runtime",
        actor_id="test", remote_idempotency_key=None, retry_of_attempt_id=None,
        permission_snapshot_fingerprint=permission, occurred_at=now, deadline_monotonic=deadline)
    raw = text.encode("utf-8")
    content = (PostgresCanonicalBlobStore(repository.connection_provider).publish(
        workspace_id=workspace_id, content=raw, media_type="text/javascript", codec="utf-8",
        deadline_monotonic=deadline) if blob else InlineContent.from_bytes(raw, media_type="text/javascript"))
    repository.freeze_code_mode_source(lease.guard, attempt_id=attempt_id, content=content,
                                       deadline_monotonic=deadline)
    return repository, lease, attempt_id, content


@pytest.mark.postgres
@pytest.mark.parametrize("blob", [False, True])
def test_frozen_source_read_uses_canonical_inline_and_blob_owner(stage2_migrated_postgres_database, blob):
    text = "// frozen Hook source 😀\n" * (4000 if blob else 2)
    repository, lease, attempt_id, _ = _freeze_source(stage2_migrated_postgres_database, text, blob=blob)
    assert repository.read_code_mode_source(session_id=lease.guard.session_id, attempt_id=attempt_id,
                                           deadline_monotonic=monotonic() + 30) == text
    assert repository.read_code_mode_source(session_id=random_id("session"), attempt_id=attempt_id,
                                           deadline_monotonic=monotonic() + 30) is None


@pytest.mark.postgres
def test_corrupt_blob_source_is_not_successful_empty_source(stage2_migrated_postgres_database):
    from pulsara_agent.conversation_kernel.reader import CanonicalProviderContinuityError
    from tests.support.postgres import connect_postgres_test_database
    repository, lease, attempt_id, content = _freeze_source(
        stage2_migrated_postgres_database, "// frozen source\n" * 5000, blob=True)
    # The fixture's admin deliberately damages content behind the read-only
    # runtime grant while preserving the physical size constraint.
    with connect_postgres_test_database(stage2_migrated_postgres_database.admin_dsn) as connection:
        connection.execute("UPDATE pulsara_v3.blobs SET body=%s WHERE id=%s", (b"x" * content.size, content.blob_id))
    with pytest.raises(CanonicalProviderContinuityError, match="CONTENT_DIGEST_MISMATCH"):
        repository.read_code_mode_source(session_id=lease.guard.session_id, attempt_id=attempt_id,
                                         deadline_monotonic=monotonic() + 30)


@pytest.mark.postgres
def test_real_host_hook_path_source_and_full_projection_failure_keep_original_output(
    tmp_path, monkeypatch, stage2_migrated_postgres_database,
):
    """Actual Hook→file read→freeze→VM→query and failed outer-page settlement."""
    import pulsara_agent.conversation_kernel.host as kernel_host
    from pulsara_agent.conversation_kernel.host import KernelHostCore
    from pulsara_agent.conversation_kernel.tool_artifacts import ToolOutputArtifactProcessor
    from pulsara_agent.conversation_kernel.tool_contracts import ProcessLocalEffectSettlementDisposition
    from pulsara_agent.llm.input import MessageRole, PromptContent, text_part_values
    from pulsara_agent.model_input.contracts import ModelInputCompileFailureKind, StructuredModelInputCompileError
    from pulsara_agent.model_input.lowering import decode_tool_result_observation
    from pulsara_agent.primitives.permission import PermissionMode
    from pulsara_agent.primitives.tool_result_projection import (
        ToolResultDeliveryRequirement, classify_tool_result_delivery,
    )
    from pulsara_agent.workspace_identity import HostWorkspaceInput
    from tests.support.model_config import test_model_binding, test_model_runtime
    from tests.support.round3 import CallbackScriptedKernelModel
    from tests.test_round2_terminal_host import _text, _tool_call
    from tests.test_round9_2_hook_integration_postgres import _install_trusted_sources

    home, workspace = tmp_path / "home", tmp_path / "workspace"
    workspace.mkdir()
    original = "throw new Error('original request must never execute');\n"
    executed = "// Hook-selected frozen source 😀\nconsole.log('ACTUAL_FROZEN_EXECUTED');\n"
    changed = "throw new Error('modified file must never execute');\n"
    (workspace / "original.js").write_text(original)
    source_path = workspace / "executed.js"
    source_path.write_text(executed)
    hook_log = tmp_path / "hook-input.json"
    hook = tmp_path / "rewrite-hook.py"
    hook.write_text(
        "import json, pathlib, sys\n"
        f"pathlib.Path({str(hook_log)!r}).write_text(sys.stdin.read())\n"
        "print(json.dumps({'hookSpecificOutput': {'hookEventName': 'PreToolUse', "
        "'updatedInput': {'path': 'executed.js', 'yield_time_ms': 1000}}}))\n"
    )
    _install_trusted_sources(home=home, workspace_root=workspace, workspace_config=None,
        user_config={"hooks": {"PreToolUse": [{"matcher": "^exec_cell$", "hooks": [{
            "type": "command", "command": shlex.join([sys.executable, str(hook)]), "timeout": 5,
        }]}]}})
    monkeypatch.setenv("PULSARA_HOME", str(home))
    failed_pages, dispositions, observed = [], [], []
    original_prepare = ToolOutputArtifactProcessor.prepare
    original_settle = DirectKernelToolPort.settle_process_local_effect

    def fail_first_full_page(owner, **values):
        if not failed_pages and "ACTUAL_FROZEN_EXECUTED" in values["public_output"]:
            page = json.loads(values["public_output"])
            if "output" in page:
                assert page["output"] == [{"kind": "log", "text": "ACTUAL_FROZEN_EXECUTED"}]
                assert classify_tool_result_delivery(tool_name="exec_cell", result_state="SUCCESS").requirement is (
                    ToolResultDeliveryRequirement.FULL_REQUIRED)
                failed_pages.append(page)
                raise StructuredModelInputCompileError(
                    ModelInputCompileFailureKind.FULL_REQUIRED_TOOL_RESULT_NOT_INLINEABLE)
        return original_prepare(owner, **values)

    async def record_real_settlement(owner, token, disposition):
        if hasattr(token.prepared, "observation") and token.prepared.observation.output:
            dispositions.append(disposition)
        return await original_settle(owner, token, disposition)

    monkeypatch.setattr(ToolOutputArtifactProcessor, "prepare", fail_first_full_page)
    monkeypatch.setattr(DirectKernelToolPort, "settle_process_local_effect", record_real_settlement)
    target = None
    recovering = False
    recovery_calls = 0
    active_session = None

    async def stream(request):
        nonlocal target, recovery_calls
        index = len(model.requests)
        if not recovering:
            if index == 1:
                payloads = _tool_call("exec_cell", random_id("call"), {"path": "original.js"})
            else:
                # The first observation can end at the queued→running change.
                # Wait for actual VM completion before selecting its full page.
                message = next(item for item in reversed(request.compiled_input.messages)
                               if item.role is MessageRole.TOOL_RESULT)
                body = json.loads(str(decode_tool_result_observation(text_part_values(message.content)[0])["body"]))
                target = body["target"]
                await _terminal(active_session._code_mode, target["cell_id"])
                payloads = _tool_call("manage_cell", random_id("call"), {"action": "wait", "target": target})
        else:
            recovery_calls += 1
            if recovery_calls == 1:
                for payload in _tool_call("query_cells", random_id("call"), {}):
                    yield payload
                return
            message = next(item for item in reversed(request.compiled_input.messages)
                           if item.role is MessageRole.TOOL_RESULT)
            body = json.loads(str(decode_tool_result_observation(text_part_values(message.content)[0])["body"]))
            observed.append(body)
            if recovery_calls == 2:
                assert len(body["cells"]) == 1 and body["cells"][0]["has_unread_output"]
                target = body["cells"][0]["target"]
                payloads = _tool_call("query_cells", random_id("call"), {"target": target})
            elif recovery_calls == 3:
                assert body["source"]["status"] == "AVAILABLE"
                assert body["source"]["text"] == executed
                assert body["source"]["text"] not in (original, changed)
                assert body["cell"]["has_unread_output"]
                payloads = _tool_call("manage_cell", random_id("call"), {"action": "wait", "target": target})
            else:
                assert recovery_calls == 4
                assert body["target"] == target == failed_pages[0]["target"]
                assert body["output"] == failed_pages[0]["output"]
                payloads = _text(random_id("block"), "FROZEN_SOURCE_AND_REDELIVERY_CONFIRMED")
        for payload in payloads:
            yield payload

    model = CallbackScriptedKernelModel(stream)
    monkeypatch.setattr(kernel_host, "DirectKernelModelPort", lambda **_: model)
    monkeypatch.setattr(kernel_host.LocalMcpManagementService, "load_configs", lambda *_args, **_kwargs: ())
    runtime = test_model_runtime(model_id="test-pro", wire_api="openai_chat_completions",
                                 postgres_dsn=stage2_migrated_postgres_database.runtime_dsn)

    async def scenario():
        nonlocal active_session, recovering
        core = KernelHostCore.production(model_runtime=runtime)
        session = await core.open_session(HostWorkspaceInput(workspace_kind="project", workspace_root=workspace))
        active_session = session
        try:
            await session.update_model_call_binding(test_model_binding(runtime))
            with pytest.raises(StructuredModelInputCompileError) as failure:
                await session.run_turn(PromptContent.text("Run the requested path"),
                                       requested_permission_mode=PermissionMode.BYPASS_PERMISSIONS)
            assert failure.value.kind is ModelInputCompileFailureKind.FULL_REQUIRED_TOOL_RESULT_NOT_INLINEABLE
            assert len(failed_pages) == 1
            assert dispositions == [ProcessLocalEffectSettlementDisposition.DISCARDED]
            cell_id = failed_pages[0]["target"]["cell_id"]
            cell = session._code_mode._cells[cell_id]
            assert cell.observation is None and cell.output and not cell.terminal_delivered
            hook_input = json.loads(hook_log.read_text())
            assert hook_input["tool_name"] == "exec_cell" and hook_input["tool_input"] == {"path": "original.js"}
            source_path.write_text(changed)
            recovering = True
            result = await session.run_turn(PromptContent.text("Inspect the existing Cell and collect its original output"),
                                            requested_permission_mode=PermissionMode.BYPASS_PERMISSIONS)
            assert result.final_text == "FROZEN_SOURCE_AND_REDELIVERY_CONFIRMED"
            assert dispositions == [ProcessLocalEffectSettlementDisposition.DISCARDED,
                                    ProcessLocalEffectSettlementDisposition.COMMITTED]
            assert source_path.read_text() == changed
            async with asyncio.timeout(5):
                while session._code_mode.snapshot_cell_index():
                    await asyncio.sleep(.001)
        finally:
            await core.shutdown()
    asyncio.run(scenario())
