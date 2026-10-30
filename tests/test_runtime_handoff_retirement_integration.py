"""Adopted mid-turn successors through real Host, owners, storage and wires."""
from __future__ import annotations

import asyncio
from dataclasses import replace
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
from time import monotonic
from uuid import uuid4

import psycopg
from psycopg import sql
from psycopg.conninfo import make_conninfo
import pytest

from pulsara_agent.code_mode import CodeModeToolResult
from pulsara_agent.conversation_kernel.compaction.contracts import (
    CompactionDisposition, CompactionTrigger, ResolvedCompactionPolicy,
)
from pulsara_agent.conversation_kernel.compaction.runtime_handoff import (
    HANDOFF_BUDGET_OMISSION, HANDOFF_CONTINUATION, PMCA_KEYS,
)
from pulsara_agent.conversation_kernel.direct_model import DirectKernelModelPort
from pulsara_agent.conversation_kernel.host import KernelHostCore
from pulsara_agent.identity import random_id
from pulsara_agent.llm.input import PromptContent, text_part_values
from pulsara_agent.llm.provider_replay import ProviderAssistantReplayCodecKind
from pulsara_agent.model_input.compiler import estimate_runtime_handoff_pmca_tokens
from pulsara_agent.model_input.continuity import decode_runtime_observation
from pulsara_agent.model_input.contracts import ModelInputScopeKind
from pulsara_agent.primitives.context import canonical_json_bytes, freeze_json, thaw_json
from pulsara_agent.ports.live_agent_event import TextEndPayload, ToolCallEndPayload
from pulsara_agent.ports.provider_stream import freeze_provider_adapter_completed_replay_payload
from pulsara_agent.primitives.permission import PermissionMode
from pulsara_agent.capability.pulsara_home import require_pulsara_home
from pulsara_agent.settings import LocalPostgresConfig, LocalSettingsStore
from pulsara_agent.desktop_runtime import validate_managed_postgres
from pulsara_agent.storage.migrations.runner import PostgresMigrationRunner
from pulsara_agent.terminal_process.models import TerminalProcessOrigin, TerminalRequest
from pulsara_agent.terminal_process.monitor import TerminalMonitorPolicy
from pulsara_agent.tools.builtins.todo import parse_todo_replacement
from pulsara_agent.workspace_identity import HostWorkspaceInput
from tests.dogfood.run_model_switch_handover_dogfood import _require_disposable_local_root
from tests.support.model_config import test_model_binding, test_model_limits, test_model_runtime
from tests.support.round3 import CallbackScriptedKernelModel
from tests.test_round2_terminal_host import _text, _tool_call
from tests.test_stage2_conversation_runner import _CompactionSummaryTransport, _context_snapshot_payload


pytestmark = pytest.mark.postgres


@pytest.fixture(scope="module")
def handoff_database():
    # Read the ordinary saved configuration before individual tests select an
    # isolated home. Validate both configured endpoints before creating the
    # uniquely named disposable database; unknown/remote endpoints are errors.
    saved = LocalSettingsStore(require_pulsara_home() / "local-settings.yaml").read()
    postgres = saved.postgres
    if postgres is None:
        # The desktop normally supplies its managed private-socket database as
        # an in-memory production override. Ask that same owner for the value;
        # an absent YAML postgres section is not a credential fallback.
        code = """
const {BundledPostgres}=require('./desktop/dist/postgres.js');
const {createPosixPostgresOperations}=require('./desktop/dist/postgres-posix.js');
(async()=>{
 const pg=new BundledPostgres(createPosixPostgresOperations('',process.argv[1]));
 const instance=await pg.operations.readInstance('resolved');
 if(instance.directory!==pg.directory||instance.socket!==pg.socket)throw new Error('managed instance mismatch');
 console.log(JSON.stringify(pg.settings()));
})().catch(e=>{console.error(e);process.exit(1)});
"""
        postgres = LocalPostgresConfig(**json.loads(subprocess.check_output(
            ["node", "-e", code, os.fspath(require_pulsara_home())], text=True,
            cwd=Path(__file__).resolve().parents[1])))
        validate_managed_postgres(postgres)
        with psycopg.connect(postgres.admin_dsn) as connection:
            actual = connection.execute("SELECT current_database(), current_user, current_setting('data_directory'), inet_server_addr()").fetchone()
        assert actual == ("pulsara", "pulsara_admin", os.fspath(require_pulsara_home() / "desktop" / "postgres"), None)
    else:
        if postgres.admin_dsn is None:
            raise RuntimeError("saved local PostgreSQL admin DSN is required")
        for label, dsn in (("admin DSN", postgres.admin_dsn), ("runtime DSN", postgres.runtime_dsn)):
            _require_disposable_local_root(dsn, label=label)
        for dsn in (postgres.admin_dsn, postgres.runtime_dsn):
            with psycopg.connect(dsn) as connection:
                actual = connection.execute("SELECT current_database(), inet_server_addr()::text").fetchone()
            assert actual is not None and actual[0] == "pulsara" and actual[1] in {"127.0.0.1", "::1"}, "configured PostgreSQL did not resolve to local pulsara"
    assert postgres.admin_dsn
    name = "pulsara_handoff_integration_" + uuid4().hex[:12]
    admin = make_conninfo(postgres.admin_dsn, dbname=name)
    runtime = make_conninfo(postgres.runtime_dsn, dbname=name)
    with psycopg.connect(postgres.admin_dsn, autocommit=True) as connection:
        connection.execute(sql.SQL("CREATE DATABASE {} TEMPLATE template0").format(sql.Identifier(name)))
    try:
        migration = PostgresMigrationRunner(admin_dsn=admin, runtime_dsn=runtime).migrate(deadline_monotonic=monotonic() + 240)
        assert migration.applied_versions == (0,)
        yield runtime, admin
    finally:
        with psycopg.connect(postgres.admin_dsn, autocommit=True) as connection:
            connection.execute(sql.SQL("DROP DATABASE {} WITH (FORCE)").format(sql.Identifier(name)))


def _observations(request):
    found = {}
    for message in request.compiled_input.messages:
        try:
            observation = decode_runtime_observation(message)
        except ValueError:
            continue
        found[observation.source_kind.value] = observation
    return found


def _payload(body):
    return json.loads(body.split("\n", 1)[0])


def _wire_observations(request):
    """Read the actual Chat/Responses serializer output used by preflight."""
    def strings(value):
        if isinstance(value, str):
            yield value
        elif isinstance(value, dict):
            for nested in value.values():
                yield from strings(nested)
        elif isinstance(value, list):
            for nested in value:
                yield from strings(nested)
    found = {}
    for item in request.wire_input_plan.materialization.ordered_input_items:
        for text in strings(thaw_json(item)):
            try:
                value = json.loads(text)
            except (ValueError, TypeError):
                continue
            if isinstance(value, dict) and "pulsara_runtime_observation" in value:
                observation = value["pulsara_runtime_observation"]
                found[observation["source"]] = observation["body"]
    return found


def _assert_wire_prefix(before, after):
    installed = before.wire_input_plan.materialization
    following = after.wire_input_plan.materialization
    assert following.root_policy_value == installed.root_policy_value
    assert following.tool_items == installed.tool_items
    assert following.ordered_input_items[:len(installed.ordered_input_items)] == installed.ordered_input_items


async def _eventually(predicate):
    async with asyncio.timeout(20):
        while not predicate():
            await asyncio.sleep(0.005)


async def _finish_agent_work_before_normal_close(core, root):
    # The separately tested close/notification race preserves its original
    # startup exception. These normal-compaction scenarios finish the admitted
    # Work and its real completion delivery before asking for a normal close.
    for child in tuple(core._sessions.values()):
        if child is root:
            continue
        task = child._active_task
        if task is not None:
            await asyncio.wait_for(asyncio.shield(task), 20)
    # Idle ROOT intentionally retains best-effort advisory references until a
    # future active turn; they are not an execution/settlement obligation.
    await _eventually(lambda: not root._external_new_turn_accepting
        and (root._active_task is None or root._active_task.done()))


class _HostCompactionModel(CallbackScriptedKernelModel):
    def __init__(self, runtime, *, variant):
        super().__init__(self._stream)
        self._model_runtime = runtime
        self._preparer = DirectKernelModelPort(model_runtime=runtime)
        self.summary_transport = _CompactionSummaryTransport("Continue the existing work and its targets.")
        self.variant = variant
        self.session = None
        self.root_calls = []
        self.resources = {}
        self.child_started = asyncio.Event()
        self.child_release = asyncio.Event()
        self.child_calls = 0
        self.todo_before = None
        self.todo_record = None
        self.todo_after_adoption = None
        self.todo_record_after_adoption = None
        self.idle_agent_id = None
        self.todo_arguments = {"items": [
            {"text": f"{index:02d}" + '"' * 104, "status": "in_progress" if index == 0 else "pending"}
            for index in range(16)
        ]}
        # This is the production parser's legal quote-heavy aggregate, not an
        # invalid arbitrary FrozenTodoSnapshot manufactured by the test.
        self.todo_candidate = parse_todo_replacement(self.todo_arguments)

    def preflight_execution(self, request, **kwargs):
        execution = super().preflight_execution(request, **kwargs)
        if self.replay_target_for_resolved_call(request.prepared_call.call).wire_api != "openai_responses":
            return execution
        # The controlled Responses provider supplies exact completed native
        # output items, so production durable-replay admission remains active.
        # Preserve the exact execution object to which the real install permit
        # is bound; only its controlled provider's completed payload changes.
        items = []
        open_once = execution.open_once
        take_completion = execution.take_completed_result_once
        async def open_responses(permit):
            async for event in open_once(permit):
                if isinstance(event, TextEndPayload):
                    items.append(freeze_json({"type": "message", "role": "assistant",
                        "content": [{"type": "output_text", "text": event.final_text}]}))
                elif isinstance(event, ToolCallEndPayload):
                    items.append(freeze_json({"type": "function_call", "call_id": event.tool_call_id,
                        "name": event.tool_name, "arguments": event.arguments_json}))
                yield event
        def take_responses_completion():
            completion = take_completion()
            return replace(completion, replay_payload=freeze_provider_adapter_completed_replay_payload(
                codec_kind=ProviderAssistantReplayCodecKind.RESPONSES_EXACT_OUTPUT_ITEMS,
                ordered_items=tuple(items)))
        execution.open_once = open_responses
        execution.take_completed_result_once = take_responses_completion
        return execution

    def resolve_compaction_summary_call(self, **kwargs):
        call = super().resolve_compaction_summary_call(**kwargs)
        self.summary_transport.binding_id = call.target.transport.binding_id
        self.summary_transport.contract_version = call.target.transport.contract_version
        resolved = replace(call, target=replace(call.target, transport=self.summary_transport))
        self._model_runtime.register_test_call_override(resolved)
        return resolved

    async def _seed(self, request):
        session = self.session
        self.idle_agent_id = random_id("session")
        def add_idle_agent(*, deadline_monotonic):
            with session.repository._writer_transaction(session._lease.guard, deadline_monotonic=deadline_monotonic) as connection:
                connection.execute("""INSERT INTO pulsara_v3.sessions
                    (id,root_session_id,workspace_id,memory_domain_id,lifecycle,writer_generation,title,model_call_binding,idle_since)
                    SELECT %s,id,workspace_id,memory_domain_id,'OPEN',1,'Retained idle Agent',model_call_binding,now()
                    FROM pulsara_v3.sessions WHERE id=%s""", (self.idle_agent_id, session.session_id))
        await session._io.run(add_idle_agent, deadline_monotonic=monotonic()+30)
        terminal = session._tools._terminal
        owner = session._tools._host_owner_id
        origin = TerminalProcessOrigin(request.turn_id, "ROOT", session.session_id)
        release_path = session.workspace.workspace_root / "release-processes"
        script = f"from pathlib import Path; import time\np=Path({str(release_path)!r})\nwhile not p.exists(): time.sleep(.01)"
        command = f"{shlex.quote(sys.executable)} -c {shlex.quote(script)}"
        running = []
        for _ in range(2):
            result = await asyncio.to_thread(terminal.execute, TerminalRequest(command, yield_time_ms=0),
                owner_host_session_id=owner, origin=origin, decision_deadline_monotonic=monotonic()+30)
            assert result.process_id
            running.append(result.process_id)
        ended = await asyncio.to_thread(terminal.execute,
            TerminalRequest("printf HANDOFF_ENDED_PROCESS", yield_time_ms=1000),
            owner_host_session_id=owner, origin=origin, decision_deadline_monotonic=monotonic()+30)
        assert ended.process_id and ended.exit_code == 0
        coordinator = session._tools.terminal_monitor_coordinator
        tokens = []
        for process_id in running:
            tokens.append(coordinator.prepare_registration(process_id=process_id,
                origin_turn_id=request.turn_id, origin_attempt_id=random_id("attempt"),
                origin_result_entry_id=random_id("entry"), writer_generation=session._lease.guard.writer_generation,
                authorization_reference="integration-owner-setup", policy=TerminalMonitorPolicy()))
        coordinator.settle_registration(tokens[0], committed=True)
        coordinator.cancel(tokens[1].monitor_id)
        assert tokens[1].monitor_id in coordinator._registrations
        assert coordinator._registrations[tokens[1].monitor_id].state.value == "CLOSED"
        cell_started = asyncio.Event()
        cell_release = asyncio.Event()
        async def invoke(_):
            cell_started.set()
            await cell_release.wait()
            return CodeModeToolResult(value={})
        cells = session._code_mode
        active = await cells.start(source="await tools.read_file({}); console.log('ACTIVE_CELL_ONCE');",
            source_exec_attempt_id=random_id("attempt"), turn_id=request.turn_id,
            snapshot=object(), tool_catalog={"read_file": {}}, invoke=invoke)
        await asyncio.wait_for(cell_started.wait(), 10)
        terminal_cell = await cells.start(source="console.log('UNCLAIMED_CELL_ONCE');",
            source_exec_attempt_id=random_id("attempt"), turn_id=request.turn_id,
            snapshot=object(), tool_catalog={}, invoke=invoke)
        await _eventually(lambda: cells.capture_cell(terminal_cell).state == "completed")
        # Freeze real owner facts without acquiring an output observation.
        before = cells.capture_cell(terminal_cell)
        assert before.has_unread_output
        cleanup_gate, cleanup_blocked = asyncio.Event(), asyncio.Event()
        collect = cells._collect
        cleanup_id = None
        async def held_collect(cell):
            if cell.cell_id == cleanup_id:
                if cell.terminal_delivered:
                    cleanup_blocked.set()
                await cleanup_gate.wait()
            await collect(cell)
        cells._collect = held_collect
        cleanup_id = await cells.start(source="void 0;", source_exec_attempt_id=random_id("attempt"),
            turn_id=request.turn_id, snapshot=object(), tool_catalog={}, invoke=invoke)
        await _eventually(lambda: cells.capture_cell(cleanup_id).state == "completed")
        cleanup_observation = await cells.observe(cleanup_id, 0)
        cleanup_ack = asyncio.create_task(cells.acknowledge(cleanup_observation))
        await asyncio.wait_for(cleanup_blocked.wait(), 10)
        assert cells._cells[cleanup_id].terminal_delivered
        assert not cells._cells[cleanup_id].output and not cells._cells[cleanup_id].pending
        self.resources.update(running=running, ended=ended.process_id, tokens=tokens,
            active=active, terminal_cell=terminal_cell, cell_release=cell_release,
            terminal_before=before, release_path=release_path, cleanup_id=cleanup_id,
            cleanup_gate=cleanup_gate, cleanup_ack=cleanup_ack,
            process_states={key: terminal.process_registry._states[key] for key in running},
            active_cell_owner=cells._cells[active],
            monitor_owner=coordinator._registrations[tokens[0].monitor_id])
        if self.variant == "large_todo":
            self._seed_todo(request)

    def _seed_todo(self, request):
        owner = self.session._tools.todo_owner
        prepared = owner.prepare_replace(scope_kind=ModelInputScopeKind.ROOT,
            exact_turn_id=request.turn_id, attempt_id=random_id("attempt"),
            proposed_result_entry_id=random_id("entry"), candidate=self.todo_candidate,
            acknowledgement=b'{"status":"updated"}')
        owner.commit(prepared)
        self.todo_record = owner._root
        self.todo_before = owner.freeze_compaction_handoff(scope_kind=ModelInputScopeKind.ROOT)

    async def _stream(self, request):
        if request.session_id != self.session.session_id:
            self.child_calls += 1
            self.child_started.set()
            await self.child_release.wait()
            for event in _text("text:child", "CHILD_EXISTING_WORK_FINISHED"):
                yield event
            return
        self.root_calls.append(request)
        index = len(self.root_calls)
        if index == 1 and self.variant == "large_todo":
            events = _tool_call("run_agent", "call:existing-agent", {"instruction": "Keep this existing Work active."})
        elif index == (2 if self.variant == "large_todo" else 1):
            if self.variant == "large_todo":
                await asyncio.wait_for(self.child_started.wait(), 20)
                await self._seed(request)
            if self.variant == "todo_only":
                self._seed_todo(request)
            events = _text("text:large", "accepted pre-compaction material " + "a" * 920_000)
            events += _tool_call("read_file", "call:trigger", {"target": {"kind": "PATH", "path": "input.txt"}})
        elif index == (3 if self.variant == "large_todo" else 2):
            if self.variant in {"large_todo", "todo_only"}:
                self.todo_record_after_adoption = self.session._tools.todo_owner._root
                self.todo_after_adoption = self.session._tools.todo_owner.freeze_compaction_handoff(
                    scope_kind=ModelInputScopeKind.ROOT)
            if self.variant == "plan":
                events = _tool_call("write_file", "call:denied-write", {
                    "target": {"kind": "PATH", "path": "must-not-exist.txt"}, "content": "forbidden"})
            elif self.variant == "large_todo":
                events = _tool_call("query_cells", "call:query-original-cells", {})
            else:
                events = _tool_call("read_file", "call:post-successor-read", {"target": {"kind": "PATH", "path": "input.txt"}})
        elif self.variant == "large_todo" and index == 4:
            # Metadata/source projection has not consumed the original output.
            assert self.session._code_mode.capture_cell(self.resources["terminal_cell"]) == self.resources["terminal_before"]
            events = _tool_call("manage_cell", "call:wait-original-cell", {"action": "wait",
                "target": {"kind": "CELL", "cell_id": self.resources["terminal_cell"]}, "yield_time_ms": 0})
        else:
            events = _text("text:finished", "HOST_HANDOFF_FINISHED")
        for event in events:
            yield event


@pytest.mark.parametrize("wire", ["openai_chat_completions", "openai_responses"])
@pytest.mark.parametrize("variant", ["large_todo", "todo_only", "empty", "plan"])
def test_real_host_adopts_midturn_handoff_and_sends_exact_owner_state(tmp_path, monkeypatch, handoff_database, wire, variant, record_property):
    import pulsara_agent.conversation_kernel.host as host_module
    runtime_dsn, _ = handoff_database
    monkeypatch.setenv("PULSARA_HOME", os.fspath(tmp_path / "home"))
    (tmp_path / "input.txt").write_text("real read-only input", encoding="utf-8")
    runtime = test_model_runtime(model_id="test-pro", wire_api=wire, postgres_dsn=runtime_dsn,
        limits=test_model_limits(total_context_tokens=360_000, max_input_tokens=330_000,
            max_output_tokens=1000, default_output_tokens=1000, input_safety_margin_tokens=0))
    model = _HostCompactionModel(runtime, variant=variant)
    monkeypatch.setattr(host_module, "DirectKernelModelPort", lambda **_: model)
    monkeypatch.setattr(host_module.LocalMcpManagementService, "load_configs", lambda *_, **__: ())

    async def scenario():
        core = KernelHostCore.production(model_runtime=runtime)
        session = await core.open_session(HostWorkspaceInput(workspace_kind="project", workspace_root=tmp_path,
            trust_workspace_mcp_config=False))
        model.session = session
        outcomes = []
        frozen_sources = []
        try:
            assert await session.attach_controller("handoff-integration")
            await session.update_model_call_binding(test_model_binding(runtime))
            session._compaction.policy = ResolvedCompactionPolicy(auto_trigger_ratio=.75,
                post_compaction_target_ratio=.60, minimum_reclaim_tokens=1,
                maximum_retained_tail_utf8_bytes=1)
            execute = session._runner.compaction.execute_active
            async def record(**kwargs):
                result = await execute(**kwargs)
                outcomes.append((kwargs["trigger"], result.outcome))
                return result
            session._runner.compaction.execute_active = record
            freeze_source = session._runner.compaction._freeze_compaction_runtime_source
            async def record_source(**kwargs):
                value = await freeze_source(**kwargs)
                frozen_sources.append(value[0])
                return value
            session._runner.compaction._freeze_compaction_runtime_source = record_source
            if variant == "plan":
                entered = await session.enter_plan(command_id=random_id("command"), entry_reason="Inspect safely",
                    resume_permission_mode=PermissionMode.BYPASS_PERMISSIONS)
                assert entered.status == "SUCCEEDED"
            result = await asyncio.wait_for(session.run_turn(PromptContent.text("p" * 40_000),
                command_id=random_id("command"), requested_permission_mode=PermissionMode.BYPASS_PERMISSIONS), 90)
            assert result.final_text == "HOST_HANDOFF_FINISHED"
            assert len(outcomes) == 1, outcomes
            assert outcomes[0][0] is CompactionTrigger.MID_TURN_FOLLOWUP
            assert outcomes[0][1].disposition is CompactionDisposition.COMPACTED
            successor_index = 2 if variant == "large_todo" else 1
            successor = model.root_calls[successor_index]
            assert _context_snapshot_payload(successor)["continuation"]["mode"] == "RESUME_ACTIVE_TURN"
            observations = _observations(successor)
            wire_observations = _wire_observations(successor)
            record_property("wire", wire)
            record_property("variant", variant)
            handoff = observations.get("COMPACTION_RUNTIME_HANDOFF")
            environment = _payload(observations["RUNTIME_ENVIRONMENT"].body)
            assert environment["workspace_kind"] == "project"
            assert "timezone" in environment
            assert not ({"cell_index", "omitted_cell_index_entries", "code_cells", "omitted_code_cells"} & environment.keys())
            # Every ordinary environment sample, including the pre-compaction
            # epoch with live Cells, carries facts without inventory injection.
            for request in model.root_calls:
                for observation in _observations(request).values():
                    if observation.source_kind.value == "RUNTIME_ENVIRONMENT":
                        assert "cell_index" not in observation.body
            if variant in {"large_todo", "todo_only"}:
                # Inspect at the first post-adoption call, before ordinary turn
                # completion legitimately settles the live TODO state.
                assert model.todo_record_after_adoption is model.todo_record
                assert model.todo_after_adoption == model.todo_before
                assert handoff is not None
                assert handoff.presence.value == "VALUE"
                payload = _payload(handoff.body)
                assert payload["todos"] == model.todo_arguments["items"]
                assert payload["todo_counts"] == {"pending": 15, "in_progress": 1, "completed": 0, "total": 16}
                assert HANDOFF_CONTINUATION in handoff.body
                assert HANDOFF_BUDGET_OMISSION not in handoff.body
                assert "omitted" not in payload
                pmca = canonical_json_bytes({key: payload[key] for key in PMCA_KEYS}).decode() + "\n" + HANDOFF_CONTINUATION
                assert estimate_runtime_handoff_pmca_tokens(pmca) <= 3200
                assert estimate_runtime_handoff_pmca_tokens(handoff.body) > 3200
                assert wire_observations["COMPACTION_RUNTIME_HANDOFF"] == handoff.body
                record_property("pmca_tokens", estimate_runtime_handoff_pmca_tokens(pmca))
                record_property("full_handoff_tokens", estimate_runtime_handoff_pmca_tokens(handoff.body))
                record_property("actual_wire_handoff_body", wire_observations["COMPACTION_RUNTIME_HANDOFF"])
            if variant == "large_todo":
                for process_id, process_owner in model.resources["process_states"].items():
                    assert session._tools._terminal.process_registry._states[process_id] is process_owner
                    assert process_owner.process.poll() is None
                assert session._code_mode._cells[model.resources["active"]] is model.resources["active_cell_owner"]
                assert session._tools.terminal_monitor_coordinator._registrations[
                    model.resources["tokens"][0].monitor_id] is model.resources["monitor_owner"]
                assert model.child_calls == 1 and not model.child_release.is_set()
                record_property("background_owners_unchanged", True)
                record_property("todo_owner_and_snapshot_unchanged", True)
                assert {row["target"]["process_id"] for row in payload["terminal_processes"]} == set(model.resources["running"])
                assert model.resources["ended"] not in handoff.body
                assert [row["target"]["monitor_id"] for row in payload["terminal_monitors"]] == [model.resources["tokens"][0].monitor_id]
                assert model.resources["tokens"][1].monitor_id not in handoff.body
                assert {row["target"]["cell_id"] for row in payload["code_cells"]} == {model.resources["active"], model.resources["terminal_cell"]}
                assert model.resources["cleanup_id"] not in handoff.body
                assert model.idle_agent_id not in handoff.body
                assert len(payload["agents"]) == 1 and payload["agents"][0]["current_work"]["kind"] == "WORK"
                rendered = "\n".join(text for request in model.root_calls[successor_index + 1:]
                    for message in request.compiled_input.messages for text in text_part_values(message.content))
                assert "UNCLAIMED_CELL_ONCE" in rendered
                # UI reads and precise stop retain the existing object identity.
                assert any(row["cell_id"] == model.resources["active"] for row in session.code_mode_cells())
                command = random_id("command")
                admission_deadline = session.control_admission_deadline_ms()
                stopped = await session.request_stop_code_mode_cell(command_id=command,
                    admission_deadline_ms=admission_deadline,
                    expected_session_id=session.session_id, expected_host_session_id=session.host_session_id,
                    cell_id=model.resources["active"])
                if stopped.status == "PENDING":
                    async with asyncio.timeout(10):
                        while stopped.status == "PENDING":
                            await asyncio.sleep(.005)
                            stopped = await session.request_stop_code_mode_cell(command_id=command,
                                admission_deadline_ms=admission_deadline, expected_session_id=session.session_id,
                                expected_host_session_id=session.host_session_id, cell_id=model.resources["active"])
                assert stopped.status == "SUCCEEDED" and stopped.user_control.cell.cell_id == model.resources["active"]
                assert session._code_mode.capture_cell(model.resources["active"]).state == "terminated"
            elif variant == "todo_only":
                assert all(payload[key] == [] for key in PMCA_KEYS)
            else:
                assert handoff is None
                assert "COMPACTION_RUNTIME_HANDOFF" not in wire_observations
                assert frozen_sources and all(source.absence_kind.value == "EXPLICIT_EMPTY" for source in frozen_sources)
                if variant == "plan":
                    assert "Planning is active." in observations["PLAN_WORKFLOW"].body
                    assert "Planning applies read-only permissions." in observations["RUN_PERMISSION"].body
                    assert not (tmp_path / "must-not-exist.txt").exists()
                    transcript = "\n".join(text for request in model.root_calls[successor_index + 1:]
                        for message in request.compiled_input.messages for text in text_part_values(message.content))
                    assert "PERMISSION_DENIED" in transcript or "permission_denied" in transcript
                    assert not {"query_agents", "query_cells"} & {tool.name for tool in successor.compiled_input.tools}
            # The first suffix after adoption keeps the successor's installed
            # SYSTEM/tools and its entire messages prefix, across actual calls.
            following = model.root_calls[successor_index + 1].compiled_input
            installed = successor.compiled_input
            assert following.system_prompt == installed.system_prompt
            assert following.tools == installed.tools
            assert following.messages[:len(installed.messages)] == installed.messages
            _assert_wire_prefix(successor, model.root_calls[successor_index+1])
            if variant == "large_todo":
                before, after = model.root_calls[:2]
                assert before.compiled_input.system_prompt == after.compiled_input.system_prompt
                assert before.compiled_input.tools == after.compiled_input.tools
                assert after.compiled_input.messages[:len(before.compiled_input.messages)] == before.compiled_input.messages
                _assert_wire_prefix(before, after)
        finally:
            if model.resources:
                for token in model.resources["tokens"]:
                    session._tools.terminal_monitor_coordinator.cancel(token.monitor_id)
                model.resources["cleanup_gate"].set()
                model.resources["cell_release"].set()
                model.resources["release_path"].touch()
                await model.resources["cleanup_ack"]
                session._tools.terminal_monitor_coordinator.settle_registration(model.resources["tokens"][1], committed=False)
            model.child_release.set()
            try:
                await _finish_agent_work_before_normal_close(core, session)
            finally:
                await core.shutdown()
    asyncio.run(scenario())


class _SubagentCompactionModel(_HostCompactionModel):
    def __init__(self, runtime):
        super().__init__(runtime, variant="subagent")
        self.core = None
        self.calls_by_session = {}
        self.sibling_ready = asyncio.Event()
        self.target_finished = asyncio.Event()
        self.target_id = None
        self.sibling_id = None
        self.target_requests = []
        self.scoped_resources = {}
        self.compactions = []

    async def _monitor_for(self, session, request):
        path = self.session.workspace.workspace_root / "release-scoped-processes"
        script = f"from pathlib import Path; import time\np=Path({str(path)!r})\nwhile not p.exists(): time.sleep(.01)"
        command = f"{shlex.quote(sys.executable)} -c {shlex.quote(script)}"
        is_root = session is self.session
        process = await asyncio.to_thread(session._tools._terminal.execute,
            TerminalRequest(command, yield_time_ms=0), owner_host_session_id=session._tools._terminal_owner_id,
            origin=TerminalProcessOrigin(request.turn_id, "ROOT" if is_root else "SUBAGENT", session.session_id),
            decision_deadline_monotonic=monotonic()+30)
        assert process.process_id
        coordinator = session._tools.terminal_monitor_coordinator
        token = coordinator.prepare_registration(process_id=process.process_id, origin_turn_id=request.turn_id,
            origin_attempt_id=random_id("attempt"), origin_result_entry_id=random_id("entry"),
            writer_generation=session._lease.guard.writer_generation, authorization_reference="integration-scoped-setup",
            policy=TerminalMonitorPolicy(), recipient_session_id=session.session_id,
            registering_agent_id=None if is_root else session.session_id,
            work_turn_id=None if is_root else request.turn_id)
        coordinator.settle_registration(token, committed=True)
        self.scoped_resources[session.session_id] = (process.process_id, token.monitor_id, request.turn_id)

    async def _stream(self, request):
        index = self.calls_by_session.get(request.session_id, 0) + 1
        self.calls_by_session[request.session_id] = index
        root = request.session_id == self.session.session_id
        if root:
            if index == 1:
                events = _tool_call("run_agent", "call:sibling", {"instruction": "OTHER_WORK: remain active until released."})
            elif index == 2:
                await asyncio.wait_for(self.sibling_ready.wait(), 20)
                await self._monitor_for(self.session, request)
                events = _tool_call("run_agent", "call:compact-child", {"instruction": "CURRENT_WORK: inspect with mid-turn compaction."})
            else:
                await asyncio.wait_for(self.target_finished.wait(), 60)
                events = _text("text:root-finished", "ROOT_SCOPE_FINISHED")
        elif self.sibling_id is None or request.session_id == self.sibling_id:
            self.sibling_id = request.session_id
            sibling = self.core._live_agent_session(request.session_id)
            await self._monitor_for(sibling, request)
            self.sibling_ready.set()
            await self.child_release.wait()
            events = _text("text:sibling-finished", "SIBLING_FINISHED")
        else:
            self.target_id = request.session_id
            self.target_requests.append(request)
            if index == 1:
                target = self.core._live_agent_session(request.session_id)
                await self._monitor_for(target, request)
                target._compaction.policy = ResolvedCompactionPolicy(auto_trigger_ratio=.75,
                    post_compaction_target_ratio=.60, minimum_reclaim_tokens=1, maximum_retained_tail_utf8_bytes=1)
                execute = target._runner.compaction.execute_active
                async def record(**kwargs):
                    result = await execute(**kwargs)
                    self.compactions.append((kwargs["trigger"], result.outcome))
                    return result
                target._runner.compaction.execute_active = record
                events = _text("text:child-large", "accepted child context " + "a" * 1_080_000)
                events += _tool_call("read_file", "call:child-trigger", {"target": {"kind": "PATH", "path": "input.txt"}})
            elif index == 2:
                events = _tool_call("query_monitors", "call:child-query", {})
            else:
                self.target_finished.set()
                events = _text("text:target-finished", "CURRENT_WORK_FINISHED")
        for event in events:
            yield event


@pytest.mark.parametrize("wire", ["openai_chat_completions", "openai_responses"])
def test_real_subagent_work_compaction_excludes_root_and_other_work_monitors(tmp_path, monkeypatch, handoff_database, wire, record_property):
    import pulsara_agent.conversation_kernel.host as host_module
    runtime_dsn, _ = handoff_database
    monkeypatch.setenv("PULSARA_HOME", os.fspath(tmp_path / "home"))
    (tmp_path / "input.txt").write_text("child scoped input", encoding="utf-8")
    runtime = test_model_runtime(model_id="test-pro", wire_api=wire, postgres_dsn=runtime_dsn,
        limits=test_model_limits(total_context_tokens=360_000, max_input_tokens=330_000,
            max_output_tokens=1000, default_output_tokens=1000, input_safety_margin_tokens=0))
    model = _SubagentCompactionModel(runtime)
    monkeypatch.setattr(host_module, "DirectKernelModelPort", lambda **_: model)
    monkeypatch.setattr(host_module.LocalMcpManagementService, "load_configs", lambda *_, **__: ())

    async def scenario():
        core = KernelHostCore.production(model_runtime=runtime)
        model.core = core
        root = await core.open_session(HostWorkspaceInput(workspace_kind="project", workspace_root=tmp_path,
            trust_workspace_mcp_config=False))
        model.session = root
        try:
            await root.update_model_call_binding(test_model_binding(runtime))
            result = await asyncio.wait_for(root.run_turn(PromptContent.text("Delegate two independent investigations"),
                command_id=random_id("command"), requested_permission_mode=PermissionMode.BYPASS_PERMISSIONS), 90)
            assert result.final_text == "ROOT_SCOPE_FINISHED"
            assert len(model.compactions) == 1
            trigger, outcome = model.compactions[0]
            assert trigger is CompactionTrigger.MID_TURN_FOLLOWUP
            assert outcome.disposition is CompactionDisposition.COMPACTED
            successor, following = model.target_requests[1:3]
            assert _context_snapshot_payload(successor)["continuation"]["mode"] == "RESUME_ACTIVE_TURN"
            assert successor.session_id == model.target_id and successor.turn_id == model.target_requests[0].turn_id
            handoff = _observations(successor)["COMPACTION_RUNTIME_HANDOFF"]
            payload = _payload(handoff.body)
            assert _wire_observations(successor)["COMPACTION_RUNTIME_HANDOFF"] == handoff.body
            record_property("wire", wire)
            record_property("actual_wire_handoff_body", handoff.body)
            process_id, monitor_id, work_turn_id = model.scoped_resources[model.target_id]
            assert work_turn_id == successor.turn_id
            assert payload["terminal_processes"] == [{"target": {"kind": "PROCESS", "process_id": process_id}, "status": "running"}]
            assert [row["target"]["monitor_id"] for row in payload["terminal_monitors"]] == [monitor_id]
            assert payload["agents"] == [] and payload["code_cells"] == []
            assert len(model.scoped_resources) == 3
            for session_id, (foreign_process, foreign_monitor, _) in model.scoped_resources.items():
                if session_id != model.target_id:
                    assert foreign_process not in handoff.body and foreign_monitor not in handoff.body
            assert not {"query_agents", "query_cells", "exec_cell", "manage_cell"} & {tool.name for tool in successor.compiled_input.tools}
            assert following.compiled_input.system_prompt == successor.compiled_input.system_prompt
            assert following.compiled_input.tools == successor.compiled_input.tools
            assert following.compiled_input.messages[:len(successor.compiled_input.messages)] == successor.compiled_input.messages
            _assert_wire_prefix(successor, following)
            rendered = "\n".join(text for message in following.compiled_input.messages for text in text_part_values(message.content))
            assert monitor_id in rendered
            assert len([call for call in model.target_requests if call.model_call_index == 1]) == 1
        finally:
            for _process, monitor, _turn in model.scoped_resources.values():
                root._tools.terminal_monitor_coordinator.cancel(monitor)
            model.child_release.set()
            (tmp_path / "release-scoped-processes").touch()
            try:
                await _finish_agent_work_before_normal_close(core, root)
            finally:
                await core.shutdown()
    asyncio.run(scenario())
