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

from pulsara_agent.code_mode import (
    CodeModeError,
    CodeModeHost,
    CodeModeLimits,
    CodeModeToolResult,
)
from pulsara_agent.code_mode.contracts import (
    exec_cell_parameters_schema,
    manage_cell_parameters_schema,
    query_cells_parameters_schema,
)
from pulsara_agent.code_mode.targets import cell_target, parse_cell_target
from pulsara_agent.conversation_kernel.tool_runtime import (
    DirectKernelToolPort,
    _validate_builtin_call_schema,
)
from pulsara_agent.identity import is_valid_id, random_id


@pytest.mark.parametrize(
    "value",
    [
        "cell_" + "a" * 32,
        "cell:" + "A" * 26,
        "cell:" + "a" * 25,
        "cell:" + "a" * 25 + "b",
        "process:" + "a" * 26,
    ],
)
def test_cell_target_rejects_noncanonical_ids_and_padding(value):
    with pytest.raises(ValueError):
        parse_cell_target({"kind": "CELL", "cell_id": value})


@pytest.mark.parametrize(
    "name,schema,valid,invalid",
    [
        (
            "exec_cell",
            exec_cell_parameters_schema,
            [{"code": ""}, {"target": {"kind": "PATH", "path": "code.js"}}],
            [
                {},
                {"code": "", "target": {"kind": "PATH", "path": "code.js"}},
                {"code": None},
                {"target": {"kind": "PATH", "path": ""}},
                {"code": "", "extra": True},
            ],
        ),
        (
            "query_cells",
            query_cells_parameters_schema,
            [{}, {"target": cell_target("cell:" + "a" * 26)}],
            [{"target": None}, {"target": {"kind": "PATH", "path": "a"}}, {"extra": 1}],
        ),
        (
            "manage_cell",
            manage_cell_parameters_schema,
            [
                {
                    "action": "wait",
                    "target": cell_target("cell:" + "a" * 26),
                    "yield_time_ms": 30000,
                },
                {"action": "stop", "target": cell_target("cell:" + "a" * 26)},
            ],
            [
                {
                    "action": "stop",
                    "target": cell_target("cell:" + "a" * 26),
                    "yield_time_ms": 0,
                },
                {
                    "action": "wait",
                    "target": cell_target("cell:" + "a" * 26),
                    "yield_time_ms": True,
                },
                {"action": "list"},
            ],
        ),
    ],
)
def test_cell_canonical_schema_and_dispatch_validation_agree(
    name, schema, valid, invalid
):
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

    return await host.start(
        source=source,
        source_exec_attempt_id=random_id("attempt"),
        turn_id=random_id("turn"),
        snapshot=object(),
        tool_catalog={"decide": {}, "read_file": {}},
        invoke=invoke or empty,
        decision_available=False,
    )


async def _terminal(host, cell_id):
    async with asyncio.timeout(15):
        while host.capture_cell(cell_id).state not in {
            "completed",
            "failed",
            "terminated",
        }:
            await asyncio.sleep(0.001)


def _context():
    return SimpleNamespace(session_id=random_id("session"))


def objects(value):
    return [part.value for part in value.content if part.type == "object"]


def test_real_host_complete_inventory_and_internal_cursor_survive_released_row():
    async def run():
        host, foreign = (
            CodeModeHost(limits=replace(CodeModeLimits(), vm_slots=1)),
            CodeModeHost(),
        )
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
            ids = [first] + [
                await _start(host, "console.log('queued');") for _ in range(56)
            ]
            foreign_id = await _start(foreign, "console.log('other Host');")
            assert len(set(ids)) == 57 and all(
                is_valid_id(value, "cell") for value in ids
            )
            value = await port._query_cells({}, _context())
            assert [row["target"]["cell_id"] for row in objects(value)] == sorted(ids)
            assert all(
                row["pending_calls"] == (1 if row["target"]["cell_id"] == first else 0)
                for row in objects(value)
            )
            with pytest.raises(CodeModeError) as missing:
                host.capture_cell(foreign_id)
            assert missing.value.code == "unavailable"
            # The UI owner's keyset still works after a queued boundary retires;
            # model queries have already returned their entire current scope.
            index = next(i for i, value in enumerate(sorted(ids)) if value != first)
            before = host.list_cell_snapshots(max_items=index + 1)
            boundary = before["next_cursor"]
            expected = host.list_cell_snapshots(cursor=boundary)
            owned = host._cells[boundary]
            await host.stop(boundary)
            await owned.vm_released.wait()
            async with asyncio.timeout(5):
                while boundary in host._cells:
                    await asyncio.sleep(0.001)
            assert host.list_cell_snapshots(cursor=boundary) == expected
            assert [
                row["target"]["cell_id"]
                for row in objects(await port._query_cells({}, _context()))
            ] == sorted(set(ids) - {boundary})
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
            cell_id = await _start(
                host, "console.log(await tools.read_file({}));", invoke
            )
            await _terminal(host, cell_id)
            lease = await host.lease_output(cell_id)
            captured = host.capture_cell(cell_id)
            assert captured.has_unread_output and captured.public_value()[
                "target"
            ] == cell_target(cell_id)
            assert host.list_cell_snapshots()["cells"]
            assert (await host.lease_output(cell_id)) is lease
            stopped = await host.stop(cell_id)
            assert stopped["target"] == cell_target(cell_id) and "output" not in stopped
            assert not host.output_delivery_allowed(lease)
            assert lease.record == {
                "kind": "log",
                "values": [{"type": "value", "value": 1}],
            }
            owned = host._cells[cell_id]
            await host.acknowledge_output(lease, accepted=False)
            await owned.vm_released.wait()
            async with asyncio.timeout(5):
                while cell_id in host._cells:
                    await asyncio.sleep(0.001)
            assert captured.public_value()["has_unread_output"] is True
            assert len(calls) == 1
        finally:
            await host.aclose()

    asyncio.run(run())


@pytest.mark.parametrize("source", ["", "\\" * 20000, "// 冻结源码😀\n" * 5000])
def test_query_source_is_complete_and_capture_survives_retirement_without_consuming_output(
    source,
):
    async def run():
        host = CodeModeHost()
        port = object.__new__(DirectKernelToolPort)
        port._code_mode = host
        started, release = asyncio.Event(), asyncio.Event()

        async def read_source(**_):
            started.set()
            await release.wait()
            return source

        port._code_mode_read_source = read_source
        context = _context()
        try:
            cell = await _start(host, "console.log('unread output');")
            await _terminal(host, cell)
            lease = await host.lease_output(cell)
            target = cell_target(cell)
            release.set()
            value = await port._query_cells({"target": target}, context)
            assert value.public_fields == {
                "target": target,
                "meta": {"cell_status": "completed", "pending_calls": 0},
            }
            assert "".join(part.value for part in value.content) == source
            assert await host.lease_output(cell) is lease
            started.clear()
            release.clear()
            query = asyncio.create_task(port._query_cells({"target": target}, context))
            await started.wait()
            owned = host._cells[cell]
            await owned.vm_released.wait()
            await host.acknowledge_output(lease)
            assert cell not in host._cells
            release.set()
            assert "".join(part.value for part in (await query).content) == source
            with pytest.raises(CodeModeError) as missing:
                await port._query_cells({"target": target}, context)
            assert missing.value.code == "unavailable"
        finally:
            release.set()
            await host.aclose()

    asyncio.run(run())


@pytest.mark.parametrize("failure", [None, OSError("frozen source blob is missing")])
def test_missing_source_is_error_and_preserves_unconsumed_output(failure):
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
            cell = await _start(host, "console.log('retained');")
            await _terminal(host, cell)
            result = await port._invoke_code_mode_control(
                "query_cells", {"target": cell_target(cell)}, _context()
            )
            value = result.public_value()
            assert value["meta"] == {"status": "ERROR"}
            assert ("blob is missing" if failure else "frozen source") in value[
                "content"
            ][0]["value"]
            lease = await host.lease_output(cell)
            assert lease.record == {
                "kind": "log",
                "values": [{"type": "text", "value": "retained"}],
            }
            assert await host.lease_output(cell) is lease
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
        _repository,
        _start_root_turn,
        _assistant_permission_fingerprint,
    )

    repository = _repository(database)
    deadline = monotonic() + 30
    now = datetime.now(timezone.utc)
    workspace_id = random_id("workspace")
    lease = repository.acquire_host_writer(
        intent="NEW",
        session_id=random_id("session"),
        workspace_id=workspace_id,
        writer_owner_id=random_id("host"),
        lease_seconds=30,
        deadline_monotonic=deadline,
    )
    turn_id, entry_id, call_id, attempt_id = (
        random_id(kind) for kind in ("turn", "entry", "call", "attempt")
    )
    _start_root_turn(
        repository,
        lease.guard,
        command_id=random_id("command"),
        turn_id=turn_id,
        entry_id=random_id("entry"),
        context_binding_revision_id=random_id("binding"),
        content=FrozenPromptContent.text("Run the source"),
        occurred_at=now,
        deadline_monotonic=deadline,
    )
    cut = repository.prepare_provider_input_cut(
        lease.guard, turn_id=turn_id, deadline_monotonic=deadline
    )
    repository.commit_assistant_message(
        lease.guard,
        cut=cut,
        entry_id=entry_id,
        blocks=(
            AssistantToolCallBlock(
                block_id=random_id("block"),
                tool_call_id=call_id,
                tool_name="exec_cell",
                arguments=freeze_json(
                    {"target": {"kind": "PATH", "path": "changed-after-exec.js"}}
                ),
            ),
        ),
        occurred_at=now,
        actor_id="model:test",
        deadline_monotonic=deadline,
    )
    permission = _assistant_permission_fingerprint(
        repository, lease.guard, entry_id, deadline
    )
    repository.accept_tool_attempt(
        lease.guard,
        attempt_id=attempt_id,
        assistant_entry_id=entry_id,
        tool_call_id=call_id,
        authorization_kind="policy",
        authorization_reference="test",
        actor_kind="runtime",
        actor_id="test",
        remote_idempotency_key=None,
        retry_of_attempt_id=None,
        permission_snapshot_fingerprint=permission,
        occurred_at=now,
        deadline_monotonic=deadline,
    )
    raw = text.encode("utf-8")
    content = (
        PostgresCanonicalBlobStore(repository.connection_provider).publish(
            workspace_id=workspace_id,
            content=raw,
            media_type="text/javascript",
            codec="utf-8",
            deadline_monotonic=deadline,
        )
        if blob
        else InlineContent.from_bytes(raw, media_type="text/javascript")
    )
    repository.freeze_code_mode_source(
        lease.guard, attempt_id=attempt_id, content=content, deadline_monotonic=deadline
    )
    return repository, lease, attempt_id, content


@pytest.mark.postgres
@pytest.mark.parametrize("blob", [False, True])
def test_frozen_source_read_uses_canonical_inline_and_blob_owner(
    stage2_migrated_postgres_database, blob
):
    text = "// frozen source 😀\n" * (4000 if blob else 2)
    repository, lease, attempt_id, _ = _freeze_source(
        stage2_migrated_postgres_database, text, blob=blob
    )
    assert (
        repository.read_code_mode_source(
            session_id=lease.guard.session_id,
            attempt_id=attempt_id,
            deadline_monotonic=monotonic() + 30,
        )
        == text
    )
    assert (
        repository.read_code_mode_source(
            session_id=random_id("session"),
            attempt_id=attempt_id,
            deadline_monotonic=monotonic() + 30,
        )
        is None
    )


@pytest.mark.postgres
def test_corrupt_blob_source_is_not_successful_empty_source(
    stage2_migrated_postgres_database,
):
    from pulsara_agent.conversation_kernel.reader import (
        CanonicalProviderContinuityError,
    )
    from tests.support.postgres import connect_postgres_test_database

    repository, lease, attempt_id, content = _freeze_source(
        stage2_migrated_postgres_database, "// frozen source\n" * 5000, blob=True
    )
    # The fixture's admin deliberately damages content behind the read-only
    # runtime grant while preserving the physical size constraint.
    with connect_postgres_test_database(
        stage2_migrated_postgres_database.admin_dsn
    ) as connection:
        connection.execute(
            "UPDATE pulsara_v3.blobs SET body=%s WHERE id=%s",
            (b"x" * content.size, content.blob_id),
        )
    with pytest.raises(
        CanonicalProviderContinuityError, match="CONTENT_DIGEST_MISMATCH"
    ):
        repository.read_code_mode_source(
            session_id=lease.guard.session_id,
            attempt_id=attempt_id,
            deadline_monotonic=monotonic() + 30,
        )


@pytest.mark.postgres
def test_real_host_path_source_and_ingress_retry_preserve_original_record(
    tmp_path,
    monkeypatch,
    stage2_migrated_postgres_database,
):
    """Source stays frozen after file changes while an immutable output retries admission."""
    import threading
    import pulsara_agent.conversation_kernel.host as kernel_host
    from pulsara_agent.conversation_kernel.host import KernelHostCore
    from pulsara_agent.conversation_kernel.repository import (
        ConversationKernelRepository,
    )
    from pulsara_agent.llm.input import MessageRole, PromptContent, text_part_values
    from tests.test_agent_code_mode_execution import _native_fields
    from pulsara_agent.primitives.permission import PermissionMode
    from pulsara_agent.workspace_identity import HostWorkspaceInput
    from tests.support.model_config import test_model_binding, test_model_runtime
    from tests.support.round3 import CallbackScriptedKernelModel
    from tests.support.model_stream import _text, _tool_call
    from tests.test_round9_2_hook_integration_postgres import _install_trusted_sources

    home, workspace = tmp_path / "home", tmp_path / "workspace"
    workspace.mkdir()
    executed = "// frozen source 😀\nconsole.log('ACTUAL_FROZEN_EXECUTED');\n"
    changed = "throw new Error('modified file must never execute');\n"
    source_path = workspace / "executed.js"
    source_path.write_text(executed)
    hook_log, hook = tmp_path / "hook-input.json", tmp_path / "review-hook.py"
    hook.write_text(
        "import json,pathlib,sys\n"
        + f"pathlib.Path({str(hook_log)!r}).write_text(sys.stdin.read())\n"
        + "print(json.dumps({'hookSpecificOutput':{'hookEventName':'PreToolUse','additionalContext':'Review the execution output.'}}))\n"
    )
    _install_trusted_sources(
        home=home,
        workspace_root=workspace,
        workspace_config=None,
        user_config={
            "hooks": {
                "PreToolUse": [
                    {
                        "matcher": "^exec_cell$",
                        "hooks": [
                            {
                                "type": "command",
                                "command": shlex.join([sys.executable, str(hook)]),
                                "timeout": 5,
                            }
                        ],
                    }
                ]
            }
        },
    )
    monkeypatch.setenv("PULSARA_HOME", str(home))
    release_ingress = threading.Event()
    attempted = threading.Event()
    candidates, observed = [], []
    original_enqueue = ConversationKernelRepository.enqueue_cell_output

    def flaky_enqueue(owner, guard, *, candidate, deadline_monotonic):
        candidates.append(candidate)
        attempted.set()
        if len(candidates) == 1:
            raise OSError("injected pre-commit output failure")
        assert release_ingress.wait(10), (
            "test failed to release original output admission"
        )
        return original_enqueue(
            owner, guard, candidate=candidate, deadline_monotonic=deadline_monotonic
        )

    monkeypatch.setattr(
        ConversationKernelRepository, "enqueue_cell_output", flaky_enqueue
    )

    async def stream(request):
        index = len(model.requests)
        if index == 1:
            payloads = _tool_call(
                "exec_cell",
                random_id("call"),
                {"target": {"kind": "PATH", "path": "executed.js"}},
            )
        elif index in {2, 3}:
            message = next(
                m
                for m in reversed(request.compiled_input.messages)
                if m.role is MessageRole.TOOL_RESULT
            )
            native_text = text_part_values(message.content)[0]
            body = _native_fields(native_text)
            if index == 2:
                assert body["meta"]["status"] == "SUCCESS"
                source_path.write_text(changed)
                async with asyncio.timeout(5):
                    while not attempted.is_set():
                        await asyncio.sleep(0.001)
                payloads = _tool_call(
                    "query_cells", random_id("call"), {"target": body["target"]}
                )
            else:
                observed.append(native_text)
                assert body["meta"]["status"] == "SUCCESS"
                assert executed in native_text and changed not in native_text
                assert candidates and not release_ingress.is_set()
                release_ingress.set()
                payloads = _text(random_id("block"), "FROZEN_SOURCE_CONFIRMED")
        else:
            payloads = _text(random_id("block"), "FROZEN_SOURCE_CONFIRMED")
        for payload in payloads:
            yield payload

    model = CallbackScriptedKernelModel(stream)
    monkeypatch.setattr(kernel_host, "DirectKernelModelPort", lambda **_: model)
    monkeypatch.setattr(
        kernel_host.LocalMcpManagementService,
        "load_configs",
        lambda *_args, **_kwargs: (),
    )
    runtime = test_model_runtime(
        model_id="test-pro",
        wire_api="openai_chat_completions",
        postgres_dsn=stage2_migrated_postgres_database.runtime_dsn,
    )

    async def scenario():
        core = KernelHostCore.production(model_runtime=runtime)
        try:
            session = await core.open_session(
                HostWorkspaceInput(workspace_kind="project", workspace_root=workspace)
            )
            await session.update_model_call_binding(test_model_binding(runtime))
            result = await session.run_turn(
                PromptContent.text("Run requested path"),
                requested_permission_mode=PermissionMode.BYPASS_PERMISSIONS,
            )
            assert result.final_text == "FROZEN_SOURCE_CONFIRMED"
            async with asyncio.timeout(5):
                while session._code_mode.snapshot_cell_index():
                    await asyncio.sleep(0.001)
            assert len(candidates) == 2
            assert candidates[0].content == candidates[1].content
            assert candidates[0].content.storage_parts == (
                {"type": "text", "value": "ACTUAL_FROZEN_EXECUTED"},
            )
            assert (
                candidates[0].content.storage_kind == "log"
                and candidates[0].content.record_sequence == 1
            )
            assert source_path.read_text() == changed
            hook_input = json.loads(hook_log.read_text())
            assert hook_input["tool_name"] == "exec_cell" and hook_input[
                "tool_input"
            ] == {"target": {"kind": "PATH", "path": "executed.js"}}
            assert "ACTUAL_FROZEN_EXECUTED" in observed[0]
        finally:
            release_ingress.set()
            await core.shutdown()

    asyncio.run(scenario())
