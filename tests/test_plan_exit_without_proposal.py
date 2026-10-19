"""No-proposal exit through production Host, provider input admission, DB and real tools."""

from __future__ import annotations

import asyncio
import json
from time import monotonic

import pytest
from psycopg.rows import dict_row

from pulsara_agent.identity import random_id
from pulsara_agent.llm.input import PromptContent, text_part_values
from pulsara_agent.primitives.permission import PermissionMode
from pulsara_agent.storage.postgres_connection_provider import PostgresConnectionLane
from tests.test_round4_plan_host import (
    _PreflightModel,
    _open_test_session,
    _text,
    _tool_call,
)
from pulsara_agent.conversation_kernel.direct_model import DirectKernelModelPort
from tests.support.model_config import test_model_runtime as model_runtime

pytestmark = pytest.mark.postgres


class _ExitModel(_PreflightModel):
    def __init__(self, arguments, *, agent_enter=False):
        self.arguments = arguments
        self.agent_enter = agent_enter
        self.requests = []
        self.exit_started = asyncio.Event()
        self.allow_exit = asyncio.Event()
        self._preparer = DirectKernelModelPort(
            model_runtime=model_runtime(
                api_key="sk-fixture-secret",
                base_url="https://example.invalid/v1",
                model_id="test-pro",
                wire_api="openai_chat_completions",
            )
        )

    def prepare_call(self, request):
        return self._preparer.prepare_call(request)

    async def stream(self, request):
        self.requests.append(request)
        index = len(self.requests) - int(self.agent_enter)
        if self.agent_enter and index == 0:
            payloads = _tool_call("enter_plan", "call:enter", {})
        elif index == 1:
            self.exit_started.set()
            await self.allow_exit.wait()
            payloads = (
                *_tool_call("exit_plan", "call:exit", self.arguments),
                *_tool_call(
                    "write_file",
                    "call:sibling",
                    {"path": "sibling.txt", "content": "must not run"},
                ),
            )
        elif index == 2:
            payloads = _tool_call(
                "write_file",
                "call:write-after-exit",
                {"path": "after-exit.txt", "content": "resumed"},
            )
        elif index == 3:
            payloads = _text("EXIT_CONTINUED")
        else:
            # The ordinary queued message must run after the automatic successor.
            payloads = _text("FIFO_AFTER_SUCCESSOR")
        for payload in payloads:
            yield payload


@pytest.mark.parametrize(
    "arguments",
    [{}, {"plan": None}, {"plan": ""}, {"plan": " \n\t\u3000"}],
    ids=["omitted", "null", "empty", "whitespace"],
)
@pytest.mark.parametrize("mode", list(PermissionMode))
def test_no_proposal_exit_restores_saved_permission_and_automatically_runs_tools(
    tmp_path,
    monkeypatch,
    stage2_migrated_postgres_database,
    arguments,
    mode,
):
    model = _ExitModel(arguments, agent_enter=arguments == {"plan": ""})

    async def scenario():
        core, session = await _open_test_session(
            tmp_path=tmp_path,
            model=model,
            postgres_dsn=stage2_migrated_postgres_database.runtime_dsn,
            monkeypatch=monkeypatch,
        )
        try:
            lost_ack = []
            if arguments == {"plan": None}:
                original_accept = session.repository.accept_plan_tool_batch

                def lose_exit_ack(*args, **kwargs):
                    outcome = original_accept(*args, **kwargs)
                    if kwargs["candidate"].control_kind.value == "EXIT":
                        lost_ack.append(outcome)
                        raise OSError("exit committed but response lost")
                    return outcome

                monkeypatch.setattr(
                    session.repository, "accept_plan_tool_batch", lose_exit_ack
                )
            attachment = random_id("attachment")
            assert await session.attach_controller(attachment)
            if not model.agent_enter:
                await session.enter_plan(
                    command_id=random_id("command"),
                    entry_reason="exit regression",
                    resume_permission_mode=mode,
                )
            running = asyncio.create_task(
                session.run_turn(
                    PromptContent.text("Stop planning and continue."),
                    command_id=random_id("command"),
                    requested_permission_mode=mode
                    if model.agent_enter
                    else PermissionMode.BYPASS_PERMISSIONS,
                )
            )
            await asyncio.wait_for(model.exit_started.wait(), 10)
            queued = await session.submit_prompt(
                command_id=random_id("command"),
                content=PromptContent.text("ordinary queued message"),
                requested_permission_mode=PermissionMode.READ_ONLY,
            )
            assert queued.status == "PENDING"
            model.allow_exit.set()
            if mode is PermissionMode.ASK_PERMISSIONS:
                deadline = monotonic() + 15
                while (
                    session.live_control.current_snapshot().current_interaction is None
                ):
                    if running.done():
                        await running
                        pytest.fail("restored ASK mode did not request tool approval")
                    assert monotonic() < deadline
                    await asyncio.sleep(0.01)
                snapshot = session.live_control.current_snapshot()
                await session.resolve_tool_interaction(
                    expected_writer_generation=session._lease.guard.writer_generation,
                    expected_owner_epoch=snapshot.owner_epoch,
                    expected_live_revision=snapshot.revision,
                    interaction_id=snapshot.current_interaction.interaction_id,
                    command_id=random_id("command"),
                    decision="ALLOW",
                    actor_id=attachment,
                )
            await asyncio.wait_for(running, 20)
            deadline = monotonic() + 15
            while (
                len(model.requests) < 4 + int(model.agent_enter)
                or session._active_task is not None
            ):
                assert monotonic() < deadline
                await asyncio.sleep(0.01)
            if arguments == {"plan": None}:
                assert len(lost_ack) == 1
            assert not (tmp_path / "sibling.txt").exists()
            if mode is PermissionMode.READ_ONLY:
                assert not (tmp_path / "after-exit.txt").exists()
            else:
                assert (tmp_path / "after-exit.txt").read_text() == "resumed"
            with session.repository.connection_provider.connection(
                lane=PostgresConnectionLane.INSPECTOR,
                row_factory=dict_row,
                deadline_monotonic=monotonic() + 5,
            ) as conn:
                workflow = conn.execute(
                    "SELECT * FROM pulsara_v3.plan_workflows WHERE session_id=%s",
                    (session.session_id,),
                ).fetchone()
                assert workflow["status"] == "EXITED"
                assert workflow["resume_permission_mode"] == mode.value
                assert workflow["accepted_plan_interaction_id"] is None
                assert (
                    conn.execute(
                        "SELECT count(*) AS n FROM pulsara_v3.plan_interactions WHERE session_id=%s",
                        (session.session_id,),
                    ).fetchone()["n"]
                    == 0
                )
                entries = conn.execute(
                    "SELECT e.*, t.effective_permission_mode, t.permission_overlay FROM pulsara_v3.transcript_entries e JOIN pulsara_v3.turns t ON t.session_id=e.session_id AND t.id=e.turn_id WHERE e.session_id=%s ORDER BY e.entry_sequence",
                    (session.session_id,),
                ).fetchall()
                exits = [
                    e for e in entries if e["source_plan_handoff_kind"] == "EXITED_PLAN"
                ]
                assert len(exits) == 1
                successor = exits[0]
                assert successor["entry_kind"] == "PLAN_CONTINUATION"
                assert successor["source_plan_interaction_id"] is None
                assert successor["effective_permission_mode"] == mode.value
                assert successor["permission_overlay"] == "NONE"
                results = conn.execute(
                    "SELECT r.*, e.inline_content, e.turn_id FROM pulsara_v3.tool_results r JOIN pulsara_v3.transcript_entries e ON e.session_id=r.session_id AND e.id=r.result_entry_id WHERE r.session_id=%s",
                    (session.session_id,),
                ).fetchall()
                by_call = {r["tool_call_id"]: r for r in results}
                exit_result = by_call["call:exit"]
                assert exit_result["result_state"] == "SUCCESS"
                assert exit_result["turn_id"] != successor["turn_id"]
                payload = json.loads(bytes(exit_result["inline_content"]))
                assert payload["plan_control"] == "EXITED_PLAN"
                assert payload["resume_permission_mode"] == mode.value
                origin = next(
                    e for e in entries if e["id"] == exit_result["result_entry_id"]
                )
                assert origin["effective_permission_mode"] == "read-only"
                assert origin["permission_overlay"] == "PLAN_READ_ONLY"
                assert (
                    by_call["call:sibling"]["result_state"]
                    == "CANCELLED_BEFORE_DISPATCH"
                )
                assert by_call["call:write-after-exit"]["result_state"] == (
                    "PERMISSION_DENIED"
                    if mode is PermissionMode.READ_ONLY
                    else "SUCCESS"
                )
                queue_entry = next(
                    e
                    for e in entries
                    if e["entry_kind"] == "USER_MESSAGE"
                    and e["entry_sequence"] > successor["entry_sequence"]
                )
                assert queue_entry["source_plan_workflow_id"] is None
            resumed = model.requests[1 + int(model.agent_enter)].compiled_input
            rendered = "\n".join(
                part
                for message in resumed.messages
                for part in text_part_values(message.content)
            )
            assert "EXITED_PLAN" in rendered
            assert "resume_permission_mode" in rendered
            assert mode.value in rendered
            for before, after in zip(model.requests, model.requests[1:]):
                assert (
                    before.compiled_input.system_prompt
                    == after.compiled_input.system_prompt
                )
                assert before.compiled_input.tools == after.compiled_input.tools
                previous = before.compiled_input.messages
                assert after.compiled_input.messages[: len(previous)] == previous
            assert session.live_control.current_snapshot().current_interaction is None
            from pulsara_agent.terminal_protocol.canonical_v3 import (
                CanonicalProtocolReader,
            )

            snapshot = CanonicalProtocolReader(
                session.repository.connection_provider
            ).snapshot(
                session_id=session.session_id,
                maximum_entries=64,
                maximum_control_items=64,
                deadline_monotonic=monotonic() + 5,
            )
            assert not snapshot.control.HasField("active_plan_workflow")
            assert not snapshot.control.HasField("open_plan_interaction")
            from pulsara_agent.terminal_protocol.generated_v3 import (
                terminal_kernel_v3_pb2 as wire,
            )

            protocol_continuations = [
                entry
                for entry in snapshot.entries
                if entry.entry_kind == wire.PLAN_CONTINUATION
                and json.loads(entry.content.inline_content).get("transition")
                == "EXITED_PLAN"
            ]
            assert len(protocol_continuations) == 1
            assert protocol_continuations[0].turn_id == successor["turn_id"]

        finally:
            await core.shutdown()

    asyncio.run(scenario())


def test_exit_resource_admission_failure_does_not_publish_partial_exit(
    tmp_path,
    monkeypatch,
    stage2_migrated_postgres_database,
):
    from pulsara_agent.model_input.contracts import (
        ModelInputCompileFailureKind,
        StructuredModelInputCompileError,
    )

    model = _ExitModel({})
    model.allow_exit.set()

    async def scenario():
        core, session = await _open_test_session(
            tmp_path=tmp_path,
            model=model,
            postgres_dsn=stage2_migrated_postgres_database.runtime_dsn,
            monkeypatch=monkeypatch,
        )

        async def reject(_candidate):
            raise StructuredModelInputCompileError(
                ModelInputCompileFailureKind.FULL_REQUIRED_TOOL_RESULT_EXCEEDS_INPUT_BUDGET
            )

        monkeypatch.setattr(session._runner, "prepare_plan_continuation_input", reject)
        try:
            await session.enter_plan(
                command_id=random_id("command"),
                entry_reason="admission failure",
                resume_permission_mode=PermissionMode.ACCEPT_EDITS,
            )
            with pytest.raises(StructuredModelInputCompileError):
                await session.run_turn(
                    PromptContent.text("exit"), command_id=random_id("command")
                )
            with session.repository.connection_provider.connection(
                lane=PostgresConnectionLane.INSPECTOR,
                row_factory=dict_row,
                deadline_monotonic=monotonic() + 5,
            ) as conn:
                workflow = conn.execute(
                    "SELECT status, workflow_revision FROM pulsara_v3.plan_workflows WHERE session_id=%s",
                    (session.session_id,),
                ).fetchone()
                assert workflow == {"status": "ACTIVE", "workflow_revision": 1}
                assert (
                    conn.execute(
                        "SELECT count(*) AS n FROM pulsara_v3.transcript_entries WHERE session_id=%s AND source_plan_handoff_kind='EXITED_PLAN'",
                        (session.session_id,),
                    ).fetchone()["n"]
                    == 0
                )
                assert (
                    conn.execute(
                        "SELECT count(*) AS n FROM pulsara_v3.tool_results WHERE session_id=%s AND result_state='SUCCESS' AND tool_call_id='call:exit'",
                        (session.session_id,),
                    ).fetchone()["n"]
                    == 0
                )
            assert not (tmp_path / "after-exit.txt").exists()
            assert session._pending_root_successor is None
        finally:
            await core.shutdown()

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "operation,reason", [("stop", "USER_STOPPED"), ("close", "SESSION_CLOSED")]
)
def test_exit_committed_successor_obeys_stop_or_close_before_handoff(
    tmp_path,
    monkeypatch,
    stage2_migrated_postgres_database,
    operation,
    reason,
):
    model = _ExitModel({})
    model.allow_exit.set()

    async def scenario():
        core, session = await _open_test_session(
            tmp_path=tmp_path,
            model=model,
            postgres_dsn=stage2_migrated_postgres_database.runtime_dsn,
            monkeypatch=monkeypatch,
        )
        reached = asyncio.Event()
        finish = session._finish_root_chain

        async def hold_handoff(result, cancellation_intent):
            if result.continuation_turn_id is not None:
                reached.set()
                await asyncio.Event().wait()
            return await finish(result, cancellation_intent)

        monkeypatch.setattr(session, "_finish_root_chain", hold_handoff)
        try:
            await session.enter_plan(
                command_id=random_id("command"),
                entry_reason="exit cancellation",
                resume_permission_mode=PermissionMode.ACCEPT_EDITS,
            )
            running = asyncio.create_task(
                session.run_turn(
                    PromptContent.text("exit"), command_id=random_id("command")
                )
            )
            await asyncio.wait_for(reached.wait(), 10)
            pending = session._pending_root_successor
            assert pending is not None
            if operation == "stop":
                command = random_id("command")
                result = await session.request_stop_turn(
                    command_id=command,
                    expected_session_id=session.session_id,
                    expected_host_session_id=session.host_session_id,
                    target_turn_id=pending.origin_turn_id,
                    admission_deadline_ms=session.control_admission_deadline_ms(),
                )
                assert result.status == "PENDING"
                await asyncio.wait_for(session._user_control_attempts[command].task, 10)
            else:
                await asyncio.wait_for(core.close_session(session.host_session_id), 10)
            with pytest.raises(asyncio.CancelledError):
                await running
            with session.repository.connection_provider.connection(
                lane=PostgresConnectionLane.INSPECTOR,
                row_factory=dict_row,
                deadline_monotonic=monotonic() + 5,
            ) as conn:
                turn = conn.execute(
                    "SELECT status,terminal_reason FROM pulsara_v3.turns WHERE session_id=%s AND id=%s",
                    (session.session_id, pending.successor_turn_id),
                ).fetchone()
                assert turn == {"status": "INTERRUPTED", "terminal_reason": reason}
                assert (
                    conn.execute(
                        "SELECT status FROM pulsara_v3.plan_workflows WHERE session_id=%s",
                        (session.session_id,),
                    ).fetchone()["status"]
                    == "EXITED"
                )
            assert len(model.requests) == 1
            assert not (tmp_path / "after-exit.txt").exists()
            assert session._pending_root_successor is None
        finally:
            await core.shutdown()

    asyncio.run(scenario())
