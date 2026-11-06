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
     [{"code": ""}, {"path": "code.js"}],
     [{}, {"code": "", "path": "code.js"}, {"code": None}, {"code": "", "yield_time_ms": True}, {"path": "code.js", "yield_time_ms": 0}]),
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
        turn_id=random_id("turn"), snapshot=object(), tool_catalog={"decide": {}, "read_file": {}},
        invoke=invoke or empty, decision_available=False)


async def _terminal(host, cell_id):
    async with asyncio.timeout(15):
        while host.capture_cell(cell_id).state not in {"completed", "failed", "terminated"}:
            await asyncio.sleep(.001)


def _context(*, maximum=None):
    from pulsara_agent.primitives.tool_result_budget import ToolResultPageBudget
    from pulsara_agent.primitives.tool_result_projection import conservative_tool_result_page_logical_utf8_bytes
    call_id = random_id("call")
    def accepts(body, memory_ids):
        return maximum is None or conservative_tool_result_page_logical_utf8_bytes(
            tool_call_id=call_id, body=body, model_visible_memory_ids=memory_ids) <= maximum
    return SimpleNamespace(tool_call_id=call_id, session_id=random_id("session"),
                           page_budget=ToolResultPageBudget(call_id, accepts_provider_body=accepts))


def test_real_host_inventory_pages_no_total_limit_and_released_cursor_remains_usable():
    async def run():
        host, foreign = CodeModeHost(limits=replace(CodeModeLimits(), vm_slots=1)), CodeModeHost()
        port = object.__new__(DirectKernelToolPort)
        port._code_mode = host
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
            context = _context()
            initial = await port._query_cells({}, context)
            assert len(initial["cells"]) == 50 and initial["next"]["tool"] == "query_cells"
            following = await port._query_cells(initial["next"]["arguments"], context)
            assert [row["target"]["cell_id"] for row in initial["cells"] + following["cells"]] == sorted(ids)
            assert following["next"] is None
            assert len((await port._query_cells({"max_items": 10**6}, context))["cells"]) == 57
            small_context = _context(maximum=1200)
            small = await port._query_cells({"max_items": 10**6}, small_context)
            assert 0 < len(small["cells"]) < 50 and small["next"]
            assert small_context.page_budget.fits(encode_tool_public_json(small).decode())
            with pytest.raises(CodeModeError, match="inventory row"):
                await port._query_cells({}, _context(maximum=100))
            with pytest.raises(CodeModeError) as missing:
                host.capture_cell(foreign_id)
            assert missing.value.code == "unavailable"
            # Select a queued object as the keyset boundary, then retire it.
            boundary_index = next(index for index, value in enumerate(sorted(ids)) if value != first)
            before = await port._query_cells({"max_items": boundary_index + 1}, context)
            boundary = before["next"]["arguments"]["cursor"]
            expected = await port._query_cells(before["next"]["arguments"], context)
            owned = host._cells[boundary]
            await host.stop(boundary)
            await owned.vm_released.wait()
            async with asyncio.timeout(5):
                while boundary in host._cells:
                    await asyncio.sleep(.001)
            assert await port._query_cells(before["next"]["arguments"], context) == expected
        finally:
            release.set()
            await host.aclose()
            await foreign.aclose()
    asyncio.run(run())


def test_live_query_does_not_reserve_or_consume_formal_output():
    async def run():
        host = CodeModeHost()
        calls = []
        async def invoke(call):
            calls.append(call)
            return CodeModeToolResult(value=1)
        try:
            cell_id = await _start(host, "console.log(await tools.read_file({}));", invoke)
            await _terminal(host, cell_id)
            lease = await host.lease_output(cell_id)
            captured = host.capture_cell(cell_id)
            assert captured.has_unread_output and captured.public_value()["target"] == cell_target(cell_id)
            assert host.list_cell_snapshots()["cells"]
            assert (await host.lease_output(cell_id)) is lease
            stopped = await host.stop(cell_id)
            assert stopped["target"] == cell_target(cell_id) and "output" not in stopped
            assert not host.output_delivery_allowed(lease)
            assert lease.record == {"kind":"log", "text":"1"}
            owned = host._cells[cell_id]
            await host.acknowledge_output(lease, accepted=False)
            await owned.vm_released.wait()
            async with asyncio.timeout(5):
                while cell_id in host._cells:
                    await asyncio.sleep(.001)
            assert captured.public_value()["has_unread_output"] is True
            assert len(calls) == 1
        finally:
            await host.aclose()
    asyncio.run(run())


@pytest.mark.parametrize("source", ["\\" * 20000, "// Hook后的冻结源码😀\n" * 5000])
def test_query_source_uses_complete_quote_and_copyable_next_without_consuming_output(source):
    async def run():
        host = CodeModeHost()
        port = object.__new__(DirectKernelToolPort)
        port._code_mode = host
        read_started, read_release = asyncio.Event(), asyncio.Event()
        async def read_source(**_):
            read_started.set()
            await read_release.wait()
            return source
        port._code_mode_read_source = read_source
        context = _context()
        try:
            cell_id = await _start(host, "console.log('unread output');")
            await _terminal(host, cell_id)
            target = cell_target(cell_id)
            lease = await host.lease_output(cell_id)
            read_release.set()
            arguments, pieces, previous_offset = {"target":target,"max_chars":32000}, [], -1
            while True:
                value = await port._query_cells(arguments, context)
                assert context.page_budget.fits(encode_tool_public_json(value).decode())
                assert value["cell"]["has_unread_output"]
                page = value["source"]
                assert page["returned_chars"] == len(page["text"]) > 0
                assert page["offset_chars"] > previous_offset
                previous_offset = page["offset_chars"]
                pieces.append(page["text"])
                assert (await host.lease_output(cell_id)) is lease
                if value["next"] is None:
                    break
                assert value["next"]["tool"] == "query_cells"
                arguments = value["next"]["arguments"]
            assert "".join(pieces) == source
            for offset in (len(source),len(source)+10):
                value = await port._query_cells({"target":target,"offset_chars":offset}, context)
                assert value["source"]["text"] == "" and value["next"] is None
            with pytest.raises(CodeModeError, match="cannot fit"):
                await port._query_cells({"target":target}, _context(maximum=100))
            assert (await host.lease_output(cell_id)) is lease
            read_started.clear()
            read_release.clear()
            query = asyncio.create_task(port._query_cells({"target":target}, context))
            await read_started.wait()
            owned = host._cells[cell_id]
            await owned.vm_released.wait()
            await host.acknowledge_output(lease)
            assert cell_id not in host._cells
            read_release.set()
            page = (await query)["source"]
            assert page["text"] == source[:page["returned_chars"]]
            with pytest.raises(CodeModeError) as missing:
                await port._query_cells({"target":target}, context)
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
            value = await port._query_cells({"target":cell_target(cell_id)}, _context())
            assert value["cell"]["has_unread_output"] and value["next"] is None
            assert value["source"]["status"] == "UNAVAILABLE" and value["source"]["reason"]
            assert "text" not in value["source"]
            lease = await host.lease_output(cell_id)
            assert lease.record == {"kind":"log", "text":"retained"}
            assert (await host.lease_output(cell_id)) is lease
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
def test_real_host_hook_path_source_and_ingress_retry_preserve_original_record(
    tmp_path, monkeypatch, stage2_migrated_postgres_database,
):
    """Real Hook→source freeze→query, while an immutable output retries admission."""
    import threading
    import pulsara_agent.conversation_kernel.host as kernel_host
    from pulsara_agent.conversation_kernel.host import KernelHostCore
    from pulsara_agent.conversation_kernel.repository import ConversationKernelRepository
    from pulsara_agent.llm.input import MessageRole, PromptContent, text_part_values
    from pulsara_agent.model_input.lowering import decode_tool_result_observation
    from pulsara_agent.primitives.permission import PermissionMode
    from pulsara_agent.workspace_identity import HostWorkspaceInput
    from tests.support.model_config import test_model_binding, test_model_runtime
    from tests.support.round3 import CallbackScriptedKernelModel
    from tests.support.model_stream import _text, _tool_call
    from tests.test_round9_2_hook_integration_postgres import _install_trusted_sources

    home, workspace = tmp_path / "home", tmp_path / "workspace"
    workspace.mkdir()
    original = "throw new Error('original request must never execute');\n"
    executed = "// Hook-selected frozen source 😀\nconsole.log('ACTUAL_FROZEN_EXECUTED');\n"
    changed = "throw new Error('modified file must never execute');\n"
    (workspace / "original.js").write_text(original)
    source_path = workspace / "executed.js"
    source_path.write_text(executed)
    hook_log, hook = tmp_path / "hook-input.json", tmp_path / "rewrite-hook.py"
    hook.write_text("import json,pathlib,sys\n" +
        f"pathlib.Path({str(hook_log)!r}).write_text(sys.stdin.read())\n" +
        "print(json.dumps({'hookSpecificOutput':{'hookEventName':'PreToolUse','updatedInput':{'path':'executed.js'}}}))\n")
    _install_trusted_sources(home=home, workspace_root=workspace, workspace_config=None,
        user_config={"hooks":{"PreToolUse":[{"matcher":"^exec_cell$","hooks":[{
            "type":"command","command":shlex.join([sys.executable,str(hook)]),"timeout":5}]}]}})
    monkeypatch.setenv("PULSARA_HOME",str(home))
    release_ingress = threading.Event()
    attempted = threading.Event()
    candidates, observed = [], []
    original_enqueue = ConversationKernelRepository.enqueue_cell_output
    def flaky_enqueue(owner, guard, *, candidate, deadline_monotonic):
        candidates.append(candidate)
        attempted.set()
        if len(candidates) == 1:
            raise OSError("injected pre-commit output failure")
        assert release_ingress.wait(10), "test failed to release original output admission"
        return original_enqueue(owner,guard,candidate=candidate,deadline_monotonic=deadline_monotonic)
    monkeypatch.setattr(ConversationKernelRepository,"enqueue_cell_output",flaky_enqueue)

    async def stream(request):
        index = len(model.requests)
        if index == 1:
            payloads = _tool_call("exec_cell",random_id("call"),{"path":"original.js"})
        elif index in {2,3}:
            message = next(m for m in reversed(request.compiled_input.messages) if m.role is MessageRole.TOOL_RESULT)
            body = json.loads(str(decode_tool_result_observation(text_part_values(message.content)[0])["body"]))
            if index == 2:
                assert body["accepted"]
                source_path.write_text(changed)
                async with asyncio.timeout(5):
                    while not attempted.is_set():
                        await asyncio.sleep(.001)
                payloads = _tool_call("query_cells",random_id("call"),{"target":body["target"]})
            else:
                observed.append(body)
                assert body["source"]["text"] == executed and body["cell"]["has_unread_output"]
                release_ingress.set()
                payloads = _text(random_id("block"),"FROZEN_SOURCE_CONFIRMED")
        else:
            payloads = _text(random_id("block"),"FROZEN_SOURCE_CONFIRMED")
        for payload in payloads:
            yield payload
    model = CallbackScriptedKernelModel(stream)
    monkeypatch.setattr(kernel_host,"DirectKernelModelPort",lambda **_:model)
    monkeypatch.setattr(kernel_host.LocalMcpManagementService,"load_configs",lambda *_args,**_kwargs:())
    runtime = test_model_runtime(model_id="test-pro",wire_api="openai_chat_completions",
                                 postgres_dsn=stage2_migrated_postgres_database.runtime_dsn)
    async def scenario():
        core = KernelHostCore.production(model_runtime=runtime)
        try:
            session = await core.open_session(HostWorkspaceInput(workspace_kind="project",workspace_root=workspace))
            await session.update_model_call_binding(test_model_binding(runtime))
            result = await session.run_turn(PromptContent.text("Run requested path"),requested_permission_mode=PermissionMode.BYPASS_PERMISSIONS)
            assert result.final_text == "FROZEN_SOURCE_CONFIRMED"
            async with asyncio.timeout(5):
                while session._code_mode.snapshot_cell_index():
                    await asyncio.sleep(.001)
            assert len(candidates) == 2
            assert candidates[0].content == candidates[1].content
            assert candidates[0].content.record == {"kind":"log","text":"ACTUAL_FROZEN_EXECUTED"}
            assert source_path.read_text() == changed
            hook_input = json.loads(hook_log.read_text())
            assert hook_input["tool_name"] == "exec_cell" and hook_input["tool_input"] == {"path":"original.js"}
            assert observed[0]["next"] is None
        finally:
            release_ingress.set()
            await core.shutdown()
    asyncio.run(scenario())
