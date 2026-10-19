from __future__ import annotations
from pulsara_agent.identity import derive_id

from pulsara_agent.llm.input import PromptContent
import asyncio
import json
from pathlib import Path
from time import monotonic
from typing import AsyncIterator
from uuid import uuid4

from psycopg.rows import dict_row
import pytest

from pulsara_agent.conversation_kernel.direct_model import DirectKernelModelPort
from pulsara_agent.conversation_kernel.host import KernelHostCore, RootChainPhase
from pulsara_agent.conversation_kernel.repository import (
    ConversationKernelConflict,
    PlanQuestionAnswer,
)
from pulsara_agent.ports.live_agent_event import (
    TextDeltaPayload,
    TextEndPayload,
    TextStartPayload,
    ToolCallDeltaPayload,
    ToolCallEndPayload,
    ToolCallStartPayload,
    live_digest,
)
from pulsara_agent.primitives.permission import PermissionMode
from pulsara_agent.llm.input import text_part_values
from pulsara_agent.model_input.continuity import ProcessLocalProviderInputInstallPermit
from pulsara_agent.primitives.plan_workflow import (
    PlanDraftDecision,
    PlanQuestionAnswerKind,
)
from pulsara_agent.storage.postgres_connection_provider import PostgresConnectionLane
from pulsara_agent.workspace_identity import HostWorkspaceInput
from tests.support.model_config import test_model_binding, test_model_runtime
from tests.support.round3 import completed_provider_execution_for_test


pytestmark = pytest.mark.postgres

_PLAN_SENTINEL = "ROUND4_APPROVED_PLAN_SENTINEL"


class _PreparedTestExecution:
    def __init__(self, owner, request, candidate, install_authority) -> None:
        self._owner = owner
        self._request = request
        self._candidate = candidate
        self._install_authority = install_authority
        self._opened = False
        self._completion = None

    def discard(self) -> None:
        if self._opened:
            raise RuntimeError("test execution already opened")
        self._opened = True

    async def open_once(self, permit: ProcessLocalProviderInputInstallPermit):
        if self._opened:
            raise RuntimeError("test execution already opened")
        if (
            permit.epoch_nonce != self._candidate.epoch_nonce
            or permit.epoch_revision != self._candidate.expected_epoch_revision + 1
        ):
            raise RuntimeError("test execution permit mismatch")
        self._install_authority.consume(
            permit,
            candidate=self._candidate,
            execution=self,
        )
        self._opened = True
        async for item in self._owner.stream(self._request):
            yield item
        self._completion = completed_provider_execution_for_test(self._request)

    def take_completed_result_once(self):
        if self._completion is None:
            raise RuntimeError("test provider execution is not complete")
        result = self._completion
        self._completion = None
        return result


class _PreflightModel:
    def prepare_target(self, request):
        return self._preparer.prepare_target(request)

    def freeze_native_tool_eligibility(self, **kwargs):
        return self._preparer.freeze_native_tool_eligibility(**kwargs)

    def materialize_native_tool_projection_set(self, **kwargs):
        return self._preparer.materialize_native_tool_projection_set(**kwargs)

    def bind_tool_surface(self, **kwargs):
        return self._preparer.bind_tool_surface(**kwargs)

    def plan_wire_input(self, **kwargs):
        return self._preparer.plan_wire_input(**kwargs)

    def freeze_wire_measurement(self, **kwargs):
        return self._preparer.freeze_wire_measurement(**kwargs)

    def replay_target_for_resolved_call(self, call):
        return self._preparer.replay_target_for_resolved_call(call)

    def preflight_execution(
        self,
        request,
        *,
        append_candidate,
        install_authority,
    ) -> _PreparedTestExecution:
        return _PreparedTestExecution(
            self, request, append_candidate, install_authority
        )


def _tool_call(
    name: str, call_id: str, arguments: dict[str, object]
) -> tuple[object, ...]:
    encoded = json.dumps(arguments, ensure_ascii=False, separators=(",", ":"))
    return (
        ToolCallStartPayload(call_id, call_id, name),
        ToolCallDeltaPayload(call_id, call_id, encoded),
        ToolCallEndPayload(
            block_identity=call_id,
            tool_call_id=call_id,
            tool_name=name,
            arguments_json=encoded,
            utf8_bytes=len(encoded.encode("utf-8")),
            digest=live_digest(encoded),
        ),
    )


def _text(value: str) -> tuple[object, ...]:
    return (
        TextStartPayload("text:implementation"),
        TextDeltaPayload("text:implementation", value),
        TextEndPayload(
            "text:implementation",
            value,
            len(value.encode("utf-8")),
            live_digest(value),
        ),
    )


class _PlanHostModel(_PreflightModel):
    def __init__(self) -> None:
        self.requests: list[object] = []
        self.question_opened = asyncio.Event()
        self.implementation_seen = asyncio.Event()
        self._preparer = DirectKernelModelPort(
            model_runtime=test_model_runtime(
                api_key="sk-fixture-secret",
                base_url="https://example.invalid/v1",
                model_id="test-pro",
                wire_api="openai_chat_completions",
            ),
        )

    def prepare_call(self, request):
        return self._preparer.prepare_call(request)

    async def stream(self, request: object) -> AsyncIterator[object]:
        self.requests.append(request)
        call_index = len(self.requests)
        if call_index == 1:
            payloads = _tool_call(
                "enter_plan", "call:enter-plan", {"reason": "plan first"}
            )
        elif call_index == 2:
            payloads = _tool_call(
                "write_file",
                "call:blocked-write",
                {"path": "must-not-exist.txt", "content": "blocked"},
            )
        elif call_index == 3:
            self.question_opened.set()
            payloads = _tool_call(
                "ask_plan_question",
                "call:question",
                {
                    "question": "Which implementation path?",
                    "options": [
                        {
                            "label": "Safe",
                            "description": "Use the bounded path",
                            "recommended": True,
                        },
                        {
                            "label": "Fast",
                            "description": "Skip validation",
                            "recommended": False,
                        },
                    ],
                    "allow_free_text": True,
                },
            )
        elif call_index == 4:
            payloads = _tool_call(
                "exit_plan",
                "call:draft",
                {
                    "plan": (f"1. inspect\n2. implement {_PLAN_SENTINEL}\n3. verify"),
                    "summary": "bounded implementation",
                },
            )
        elif call_index == 5:
            compiled = request.compiled_input  # type: ignore[attr-defined]
            rendered = "\n".join(
                (
                    compiled.system_prompt,
                    *(
                        part
                        for message in compiled.messages
                        for part in text_part_values(message.content)
                    ),
                    *(
                        call.arguments
                        for message in compiled.messages
                        for call in message.tool_calls
                    ),
                )
            )
            assert rendered.count(_PLAN_SENTINEL) == 1
            self.implementation_seen.set()
            payloads = _text("ROUND4_IMPLEMENTATION_READY")
        else:  # pragma: no cover - an extra provider cycle is a contract failure
            raise AssertionError("unexpected Plan provider cycle")
        for payload in payloads:
            yield payload


class _EnterPlanThenTextModel(_PreflightModel):
    def __init__(self) -> None:
        self.requests: list[object] = []
        self.completed = asyncio.Event()
        self._preparer = DirectKernelModelPort(
            model_runtime=test_model_runtime(
                api_key="sk-fixture-secret",
                base_url="https://example.invalid/v1",
                model_id="test-pro",
                wire_api="openai_chat_completions",
            ),
        )

    def prepare_call(self, request):
        return self._preparer.prepare_call(request)

    async def stream(self, request: object) -> AsyncIterator[object]:
        self.requests.append(request)
        if len(self.requests) == 1:
            payloads = _tool_call(
                "enter_plan", "call:queued-enter-plan", {"reason": "queue plan"}
            )
        elif len(self.requests) == 2:
            self.completed.set()
            payloads = _text("QUEUED_PLAN_CONTINUATION_COMPLETED")
        else:  # pragma: no cover - an extra provider cycle is a contract failure
            raise AssertionError("unexpected queued Plan provider cycle")
        for payload in payloads:
            yield payload


class _DetachedDraftModel(_PreflightModel):
    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.implementation_seen = asyncio.Event()
        self.requests: list[object] = []
        self._preparer = DirectKernelModelPort(
            model_runtime=test_model_runtime(
                api_key="sk-fixture-secret",
                base_url="https://example.invalid/v1",
                model_id="test-pro",
                wire_api="openai_chat_completions",
            ),
        )

    def prepare_call(self, request):
        return self._preparer.prepare_call(request)

    async def stream(self, request: object) -> AsyncIterator[object]:
        self.requests.append(request)
        if len(self.requests) == 1:
            self.started.set()
            await self.release.wait()
            payloads = _tool_call(
                "exit_plan",
                "call:detached-draft",
                {"plan": "1. retain task ownership\n2. approve", "summary": "draft"},
            )
        elif len(self.requests) == 2:
            self.implementation_seen.set()
            payloads = _text("DETACHED_DRAFT_SUCCESSOR_COMPLETED")
        else:  # pragma: no cover - an extra provider cycle is a contract failure
            raise AssertionError("unexpected detached Plan provider cycle")
        for payload in payloads:
            yield payload


class _ForceExitRaceModel(_PreflightModel):
    def __init__(self) -> None:
        self.requests: list[object] = []
        self._preparer = DirectKernelModelPort(
            model_runtime=test_model_runtime(
                api_key="sk-fixture-secret",
                base_url="https://example.invalid/v1",
                model_id="test-pro",
                wire_api="openai_chat_completions",
            ),
        )

    def prepare_call(self, request):
        return self._preparer.prepare_call(request)

    async def stream(self, request: object) -> AsyncIterator[object]:
        self.requests.append(request)
        if len(self.requests) != 1:  # pragma: no cover - successor must not bind
            raise AssertionError("force-exit bound an automatic successor")
        for payload in _tool_call(
            "enter_plan", "call:force-race", {"reason": "force race"}
        ):
            yield payload


class _BlockingPlanTextModel(_PreflightModel):
    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self._preparer = DirectKernelModelPort(
            model_runtime=test_model_runtime(
                api_key="sk-fixture-secret",
                base_url="https://example.invalid/v1",
                model_id="test-pro",
                wire_api="openai_chat_completions",
            ),
        )

    def prepare_call(self, request):
        return self._preparer.prepare_call(request)

    async def stream(self, request: object) -> AsyncIterator[object]:
        del request
        self.started.set()
        await self.release.wait()
        for payload in _text("PLAN_TURN_REMAINED_LIVE"):
            yield payload


def _runtime(postgres_dsn: str):
    return test_model_runtime(
        api_key="sk-fixture-secret",
        base_url="https://example.invalid/v1",
        model_id="test-pro",
        wire_api="openai_chat_completions",
        postgres_dsn=postgres_dsn,
    )


async def _open_test_session(
    *,
    tmp_path: Path,
    model: object,
    postgres_dsn: str,
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[KernelHostCore, object]:
    import pulsara_agent.conversation_kernel.host as kernel_host

    monkeypatch.setattr(kernel_host, "DirectKernelModelPort", lambda **_: model)
    monkeypatch.setattr(kernel_host.LocalMcpManagementService, "load_configs", lambda *_args, **_kwargs: ())
    core = KernelHostCore.production(model_runtime=_runtime(postgres_dsn))
    session = await core.open_session(
        HostWorkspaceInput(workspace_kind="project", workspace_root=tmp_path)
    )
    await session.update_model_call_binding(
        test_model_binding(core._model_runtime)  # noqa: SLF001
    )
    return core, session


def test_round4_host_enter_question_approve_and_permission_happy_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    stage2_migrated_postgres_database,
) -> None:
    import pulsara_agent.conversation_kernel.host as kernel_host

    model = _PlanHostModel()
    monkeypatch.setattr(kernel_host, "DirectKernelModelPort", lambda **_: model)
    monkeypatch.setattr(kernel_host.LocalMcpManagementService, "load_configs", lambda *_args, **_kwargs: ())
    model_runtime = test_model_runtime(
        api_key="sk-fixture-secret",
        base_url="https://example.invalid/v1",
        model_id="test-pro",
        wire_api="openai_chat_completions",
        postgres_dsn=stage2_migrated_postgres_database.runtime_dsn,
    )

    async def scenario() -> None:
        core = KernelHostCore.production(model_runtime=model_runtime)
        session = await core.open_session(
            HostWorkspaceInput(
                workspace_kind="project",
                workspace_root=tmp_path,
            )
        )
        await session.update_model_call_binding(test_model_binding(model_runtime))
        running = asyncio.create_task(
            session.run_turn(
                PromptContent.text("Make a plan before editing."),
                command_id='command:sxoqk2ilnlrszpfj6i47qge33u',
                requested_permission_mode=PermissionMode.ACCEPT_EDITS,
            )
        )
        question_wait = asyncio.create_task(model.question_opened.wait())
        done, _pending = await asyncio.wait(
            {running, question_wait}, timeout=5, return_when=asyncio.FIRST_COMPLETED
        )
        if running in done:
            await running
        assert question_wait in done, "Plan question was not opened"
        deadline = monotonic() + 5
        opened = None
        while opened is None:
            opened = await session._plan_interactions.current_open()  # noqa: SLF001
            assert monotonic() < deadline
            if opened is None:
                await asyncio.sleep(0.01)

        with session.repository.connection_provider.connection(
            lane=PostgresConnectionLane.INSPECTOR,
            row_factory=dict_row,
            deadline_monotonic=monotonic() + 5,
        ) as connection:
            question = connection.execute(
                "SELECT i.plan_workflow_id, w.workflow_revision "
                "FROM pulsara_v3.plan_interactions AS i "
                "JOIN pulsara_v3.plan_workflows AS w "
                "ON w.session_id = i.session_id AND w.id = i.plan_workflow_id "
                "WHERE i.session_id = %s AND i.id = %s",
                (session.session_id, opened.interaction_id),
            ).fetchone()
        assert question is not None
        workflow_id = str(question["plan_workflow_id"])
        assert int(question["workflow_revision"]) == 2
        await session.resolve_plan_question(
            command_id='command:bksg34ixdxmlcrrrjqyljxmhmu',
            workflow_id=workflow_id,
            expected_workflow_revision=2,
            interaction_id=opened.interaction_id,
            answer=PlanQuestionAnswer(PlanQuestionAnswerKind.OPTION, option_ordinal=0),
        )
        draft_run = await asyncio.wait_for(running, timeout=5)
        assert draft_run.pending_plan_interaction_id is not None
        assert not (tmp_path / "must-not-exist.txt").exists()

        with session.repository.connection_provider.connection(
            lane=PostgresConnectionLane.INSPECTOR,
            row_factory=dict_row,
            deadline_monotonic=monotonic() + 5,
        ) as connection:
            blocked = connection.execute(
                "SELECT result_state FROM pulsara_v3.tool_results "
                "WHERE session_id = %s AND tool_call_id = 'call:blocked-write'",
                (session.session_id,),
            ).fetchone()
            draft = connection.execute(
                "SELECT w.workflow_revision "
                "FROM pulsara_v3.plan_interactions AS i "
                "JOIN pulsara_v3.plan_workflows AS w "
                "ON w.session_id = i.session_id AND w.id = i.plan_workflow_id "
                "WHERE i.session_id = %s AND i.id = %s",
                (session.session_id, draft_run.pending_plan_interaction_id),
            ).fetchone()
        assert blocked == {"result_state": "PERMISSION_DENIED"}
        assert int(draft["workflow_revision"]) == 4

        approved = await session.resolve_plan_draft_review(
            command_id='command:aoqobn3uzzf5rldcj6mn23grfu',
            workflow_id=workflow_id,
            expected_workflow_revision=4,
            interaction_id=draft_run.pending_plan_interaction_id,
            decision=PlanDraftDecision.APPROVE,
            revision_prompt=None,
        )
        assert approved.continuation_turn_id is not None
        successor_task = session._active_task  # noqa: SLF001
        assert successor_task is not None
        await asyncio.wait_for(asyncio.shield(successor_task), timeout=5)
        assert model.implementation_seen.is_set()

        with session.repository.connection_provider.connection(
            lane=PostgresConnectionLane.INSPECTOR,
            row_factory=dict_row,
            deadline_monotonic=monotonic() + 5,
        ) as connection:
            implementation = connection.execute(
                "SELECT status, effective_permission_mode, permission_overlay "
                "FROM pulsara_v3.turns WHERE session_id = %s AND id = %s",
                (session.session_id, approved.continuation_turn_id),
            ).fetchone()
        assert implementation == {
            "status": "COMPLETED",
            "effective_permission_mode": "accept-edits",
            "permission_overlay": "NONE",
        }
        assert len(model.requests) == 5
        await core.close_session(session.host_session_id)
        await core.shutdown()

    asyncio.run(scenario())


def test_round4_queued_root_turn_runs_automatic_plan_successor(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    stage2_migrated_postgres_database,
) -> None:
    model = _EnterPlanThenTextModel()

    async def scenario() -> None:
        core, session = await _open_test_session(
            tmp_path=tmp_path,
            model=model,
            postgres_dsn=stage2_migrated_postgres_database.runtime_dsn,
            monkeypatch=monkeypatch,
        )
        accepted = await session.submit_prompt(
            command_id='command:wrlpqzmssdyfr24hczvci4pxwm',
            content=PromptContent.text("Plan this queued request."),
        )
        assert accepted.status == "PENDING"
        await asyncio.wait_for(model.completed.wait(), timeout=5)
        deadline = monotonic() + 5
        while session._active_task is not None:  # noqa: SLF001
            assert monotonic() < deadline
            await asyncio.sleep(0.01)

        with session.repository.connection_provider.connection(
            lane=PostgresConnectionLane.INSPECTOR,
            row_factory=dict_row,
            deadline_monotonic=monotonic() + 5,
        ) as connection:
            statuses = connection.execute(
                "SELECT status FROM pulsara_v3.turns "
                "WHERE session_id = %s ORDER BY accepted_at, id",
                (session.session_id,),
            ).fetchall()
        assert statuses == [{"status": "COMPLETED"}, {"status": "COMPLETED"}]
        assert len(model.requests) == 2
        await core.close_session(session.host_session_id)
        await core.shutdown()

    asyncio.run(scenario())


def test_round4_detached_waiter_cannot_strand_root_slot_before_plan_review(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    stage2_migrated_postgres_database,
) -> None:
    model = _DetachedDraftModel()

    async def scenario() -> None:
        core, session = await _open_test_session(
            tmp_path=tmp_path,
            model=model,
            postgres_dsn=stage2_migrated_postgres_database.runtime_dsn,
            monkeypatch=monkeypatch,
        )
        entered = await session.enter_plan(
            command_id='command:xvmfuqrzeyxnjmngwmq6hnx6sa',
            entry_reason="prepare detached review",
            resume_permission_mode=PermissionMode.ACCEPT_EDITS,
        )
        running = asyncio.create_task(
            session.run_turn(
                PromptContent.text("Draft the plan."),
                command_id='command:lk5pmfotchgugdrpgzbeuaag74',
                requested_permission_mode=PermissionMode.ACCEPT_EDITS,
            )
        )
        await asyncio.wait_for(model.started.wait(), timeout=5)
        running.cancel()
        with pytest.raises(asyncio.CancelledError):
            await running
        assert session._active_task is not None  # noqa: SLF001
        model.release.set()

        deadline = monotonic() + 5
        while session._active_task is not None:  # noqa: SLF001
            assert monotonic() < deadline
            await asyncio.sleep(0.01)

        with session.repository.connection_provider.connection(
            lane=PostgresConnectionLane.INSPECTOR,
            row_factory=dict_row,
            deadline_monotonic=monotonic() + 5,
        ) as connection:
            draft = connection.execute(
                "SELECT i.id, w.workflow_revision "
                "FROM pulsara_v3.plan_interactions AS i "
                "JOIN pulsara_v3.plan_workflows AS w "
                "ON w.session_id = i.session_id AND w.id = i.plan_workflow_id "
                "WHERE i.session_id = %s AND i.kind = 'DRAFT_REVIEW' "
                "AND i.status = 'OPEN'",
                (session.session_id,),
            ).fetchone()
        assert draft is not None
        approved = await session.resolve_plan_draft_review(
            command_id='command:i4unsmhggqu6ko5alb2bx6zo7a',
            workflow_id=entered.target_id,
            expected_workflow_revision=int(draft["workflow_revision"]),
            interaction_id=str(draft["id"]),
            decision=PlanDraftDecision.APPROVE,
            revision_prompt=None,
        )
        assert approved.continuation_turn_id is not None
        successor = session._active_task  # noqa: SLF001
        assert successor is not None
        await asyncio.wait_for(asyncio.shield(successor), timeout=5)
        assert model.implementation_seen.is_set()

        with session.repository.connection_provider.connection(
            lane=PostgresConnectionLane.INSPECTOR,
            row_factory=dict_row,
            deadline_monotonic=monotonic() + 5,
        ) as connection:
            successor_row = connection.execute(
                "SELECT status FROM pulsara_v3.turns WHERE session_id = %s AND id = %s",
                (session.session_id, approved.continuation_turn_id),
            ).fetchone()
        assert successor_row == {"status": "COMPLETED"}
        await core.close_session(session.host_session_id)
        await core.shutdown()

    asyncio.run(scenario())


def test_round4_force_exit_fences_full_automatic_continuation_before_bind(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    stage2_migrated_postgres_database,
) -> None:
    model = _ForceExitRaceModel()

    async def scenario() -> None:
        core, session = await _open_test_session(
            tmp_path=tmp_path,
            model=model,
            postgres_dsn=stage2_migrated_postgres_database.runtime_dsn,
            monkeypatch=monkeypatch,
        )
        winner_ready = asyncio.Event()
        inspect_release = asyncio.Event()
        original_inspect = session._inspect_plan_continuation  # noqa: SLF001

        async def delayed_inspect(**kwargs):
            winner_ready.set()
            await inspect_release.wait()
            return await original_inspect(**kwargs)

        monkeypatch.setattr(session, "_inspect_plan_continuation", delayed_inspect)
        running = asyncio.create_task(
            session.run_turn(
                PromptContent.text("Enter Plan while force-exit races."),
                command_id='command:yicudqxalprq6bpetytvtuwa54',
            )
        )
        await asyncio.wait_for(winner_ready.wait(), timeout=5)
        with session.repository.connection_provider.connection(
            lane=PostgresConnectionLane.INSPECTOR,
            row_factory=dict_row,
            deadline_monotonic=monotonic() + 5,
        ) as connection:
            workflow = connection.execute(
                "SELECT id, workflow_revision FROM pulsara_v3.plan_workflows "
                "WHERE session_id = %s AND status = 'ACTIVE'",
                (session.session_id,),
            ).fetchone()
            successor = connection.execute(
                "SELECT t.id FROM pulsara_v3.turns AS t "
                "JOIN pulsara_v3.transcript_entries AS e "
                "ON e.session_id = t.session_id AND e.id = t.initial_entry_id "
                "WHERE t.session_id = %s "
                "AND e.source_plan_handoff_kind = 'ENTERED_PLAN'",
                (session.session_id,),
            ).fetchone()
        assert workflow is not None
        assert successor is not None
        exiting = asyncio.create_task(
            session.force_exit_plan(
                command_id='command:p2g5zrg4xhgszmle5oujw4dbny',
                workflow_id=str(workflow["id"]),
                expected_workflow_revision=int(workflow["workflow_revision"]),
            )
        )
        deadline = monotonic() + 5
        while not session._plan_exit_fence:  # noqa: SLF001
            assert monotonic() < deadline
            await asyncio.sleep(0.01)
        inspect_release.set()
        outcome = await asyncio.wait_for(exiting, timeout=5)
        assert outcome.public_code == "PLAN_FORCE_EXITED"
        with pytest.raises(asyncio.CancelledError):
            await running

        with session.repository.connection_provider.connection(
            lane=PostgresConnectionLane.INSPECTOR,
            row_factory=dict_row,
            deadline_monotonic=monotonic() + 5,
        ) as connection:
            successor_row = connection.execute(
                "SELECT status FROM pulsara_v3.turns WHERE session_id = %s AND id = %s",
                (session.session_id, successor["id"]),
            ).fetchone()
        assert successor_row == {"status": "INTERRUPTED"}
        assert len(model.requests) == 1
        await core.close_session(session.host_session_id)
        await core.shutdown()

    asyncio.run(scenario())


@pytest.mark.parametrize(
    ("operation", "terminal_reason"),
    (("stop", "USER_STOPPED"), ("close", "SESSION_CLOSED")),
)
def test_round7_cancel_during_automatic_plan_pending_handoff_interrupts_successor(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    stage2_migrated_postgres_database,
    operation: str,
    terminal_reason: str,
) -> None:
    model = _EnterPlanThenTextModel()

    async def scenario() -> None:
        core, session = await _open_test_session(
            tmp_path=tmp_path,
            model=model,
            postgres_dsn=stage2_migrated_postgres_database.runtime_dsn,
            monkeypatch=monkeypatch,
        )
        handoff_reached = asyncio.Event()
        original_finish = session._finish_root_chain  # noqa: SLF001

        async def hold_before_handoff(result, cancellation_intent):
            if result.continuation_turn_id is not None:
                handoff_reached.set()
                await asyncio.Event().wait()
            return await original_finish(result, cancellation_intent)

        monkeypatch.setattr(session, "_finish_root_chain", hold_before_handoff)
        running = asyncio.create_task(
            session.run_turn(
                PromptContent.text("Enter Plan and wait at the ownership handoff."),
                command_id='command:zgmtm6hm7qiube4algyfcyevae',
            )
        )
        await asyncio.wait_for(handoff_reached.wait(), timeout=5)
        pending = session._pending_root_successor  # noqa: SLF001
        assert pending is not None
        successor_turn_id = pending.successor_turn_id
        assert session._active_turn_id == pending.origin_turn_id  # noqa: SLF001
        assert session._active_root_phase is (  # noqa: SLF001
            RootChainPhase.SUCCESSOR_COMMITTED_PENDING_HANDOFF
        )

        if operation == "stop":
            command_id = (
                derive_id("command", "test-fixture", f"command:control:{session.control_admission_deadline_ms()}:"
                f"{uuid4()}")
            )
            accepted = await asyncio.wait_for(
                session.request_stop_turn(
                    command_id=command_id,
                    expected_session_id=session.session_id,
                    expected_host_session_id=session.host_session_id,
                    target_turn_id=pending.origin_turn_id,
                 admission_deadline_ms=session.control_admission_deadline_ms()),
                timeout=5,
            )
            assert accepted.status == "PENDING"
            attempt = session._user_control_attempts[command_id]  # noqa: SLF001
            assert attempt.task is not None
            await asyncio.wait_for(attempt.task, timeout=5)
        else:
            await asyncio.wait_for(
                core.close_session(
                    session.host_session_id,
                ),
                timeout=5,
            )
        with pytest.raises(asyncio.CancelledError):
            await running
        assert session._active_task is None  # noqa: SLF001
        assert session._pending_root_successor is None  # noqa: SLF001
        with session.repository.connection_provider.connection(
            lane=PostgresConnectionLane.INSPECTOR,
            row_factory=dict_row,
            deadline_monotonic=monotonic() + 5,
        ) as connection:
            successor = connection.execute(
                "SELECT status, terminal_reason FROM pulsara_v3.turns "
                "WHERE session_id = %s AND id = %s",
                (session.session_id, successor_turn_id),
            ).fetchone()
        assert successor == {
            "status": "INTERRUPTED",
            "terminal_reason": terminal_reason,
        }
        if operation == "stop":
            await core.close_session(session.host_session_id)
        await core.shutdown()

    asyncio.run(scenario())


def test_round4_stale_force_exit_does_not_cancel_valid_root_turn(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    stage2_migrated_postgres_database,
) -> None:
    model = _BlockingPlanTextModel()

    async def scenario() -> None:
        core, session = await _open_test_session(
            tmp_path=tmp_path,
            model=model,
            postgres_dsn=stage2_migrated_postgres_database.runtime_dsn,
            monkeypatch=monkeypatch,
        )
        entered = await session.enter_plan(
            command_id='command:lkhkapvefg7cn5vpcgi7ggdz7i',
            entry_reason="validate before cancelling",
            resume_permission_mode=PermissionMode.READ_ONLY,
        )
        running = asyncio.create_task(
            session.run_turn(
                PromptContent.text("Keep this exact turn alive."),
                command_id='command:zngiz6o6oniqp6cz3gsbbng56y',
            )
        )
        await asyncio.wait_for(model.started.wait(), timeout=5)
        with pytest.raises(ConversationKernelConflict, match="target drifted"):
            await session.force_exit_plan(
                command_id='command:gduj36krcv5ht7uvabkc4qq3oq',
                workflow_id='plan:exe6aubs2ra65q6wrb3vc5yvx4',
                expected_workflow_revision=entered.plan_workflow_revision,
            )
        assert not running.done()
        assert session._active_task is not None  # noqa: SLF001
        model.release.set()
        result = await asyncio.wait_for(running, timeout=5)
        assert result.final_text == "PLAN_TURN_REMAINED_LIVE"
        cancelled = await session.cancel_plan(
            command_id='command:2cnwd7gdysyp2hxr4j3eou6iye',
            workflow_id=entered.target_id,
            expected_workflow_revision=entered.plan_workflow_revision,
        )
        assert cancelled.public_code == "PLAN_CANCELLED"
        await core.close_session(session.host_session_id)
        await core.shutdown()

    asyncio.run(scenario())


def test_round4_unbound_successor_retries_terminalization_before_retirement(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    stage2_migrated_postgres_database,
) -> None:
    model = _DetachedDraftModel()

    async def scenario() -> None:
        core, session = await _open_test_session(
            tmp_path=tmp_path,
            model=model,
            postgres_dsn=stage2_migrated_postgres_database.runtime_dsn,
            monkeypatch=monkeypatch,
        )
        entered = await session.enter_plan(
            command_id='command:scry7cudtekborsan7kpgs6uqq',
            entry_reason="exercise terminalization retry",
            resume_permission_mode=PermissionMode.ACCEPT_EDITS,
        )
        running = asyncio.create_task(
            session.run_turn(
                PromptContent.text("Draft a bounded plan."),
                command_id='command:d5b4wigb4wy6mx2uui3zj3x5ne',
            )
        )
        await asyncio.wait_for(model.started.wait(), timeout=5)
        model.release.set()
        origin = await asyncio.wait_for(running, timeout=5)
        assert origin.pending_plan_interaction_id is not None

        with session.repository.connection_provider.connection(
            lane=PostgresConnectionLane.INSPECTOR,
            row_factory=dict_row,
            deadline_monotonic=monotonic() + 5,
        ) as connection:
            workflow = connection.execute(
                "SELECT workflow_revision FROM pulsara_v3.plan_workflows "
                "WHERE session_id = %s AND id = %s",
                (session.session_id, entered.target_id),
            ).fetchone()
        assert workflow is not None

        async def reject_bind(**_: object) -> bool:
            return False

        original_interrupt = session.repository.interrupt_turn
        interrupt_calls = 0

        def flaky_interrupt(*args: object, **kwargs: object) -> bool:
            nonlocal interrupt_calls
            interrupt_calls += 1
            if interrupt_calls == 1:
                raise RuntimeError("injected transient terminalization failure")
            return original_interrupt(*args, **kwargs)

        monkeypatch.setattr(session, "_bind_plan_review_successor", reject_bind)
        monkeypatch.setattr(session.repository, "interrupt_turn", flaky_interrupt)
        approved = await asyncio.wait_for(
            session.resolve_plan_draft_review(
                command_id='command:tw2yikvynynbh63vxu3kemqeqq',
                workflow_id=entered.target_id,
                expected_workflow_revision=int(workflow["workflow_revision"]),
                interaction_id=origin.pending_plan_interaction_id,
                decision=PlanDraftDecision.APPROVE,
                revision_prompt=None,
            ),
            timeout=5,
        )
        assert approved.continuation_turn_id is not None
        assert interrupt_calls >= 2
        with session.repository.connection_provider.connection(
            lane=PostgresConnectionLane.INSPECTOR,
            row_factory=dict_row,
            deadline_monotonic=monotonic() + 5,
        ) as connection:
            successor = connection.execute(
                "SELECT status FROM pulsara_v3.turns WHERE session_id = %s AND id = %s",
                (session.session_id, approved.continuation_turn_id),
            ).fetchone()
        assert successor == {"status": "INTERRUPTED"}
        assert not model.implementation_seen.is_set()
        await core.close_session(session.host_session_id)
        await core.shutdown()

    asyncio.run(scenario())


def test_pending_review_submission_queues_and_runs_after_cancel_without_prefix_conflict(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    stage2_migrated_postgres_database,
) -> None:
    from pulsara_agent.identity import random_id
    from pulsara_agent.terminal_protocol.generated_v3 import terminal_kernel_v3_pb2 as wire
    from pulsara_agent.terminal_protocol.v3_gateway import TerminalKernelProtocolServer, _Connection

    async def scenario() -> None:
        model = _DetachedDraftModel()
        model.release.set()
        core, session = await _open_test_session(
            tmp_path=tmp_path, model=model,
            postgres_dsn=stage2_migrated_postgres_database.runtime_dsn,
            monkeypatch=monkeypatch,
        )
        try:
            entered = await session.enter_plan(
                command_id=random_id('command'), entry_reason='review prompt admission',
                resume_permission_mode=PermissionMode.ACCEPT_EDITS,
            )
            await asyncio.wait_for(session.run_turn(
                PromptContent.text('prepare a plan'), command_id=random_id('command'),
                requested_permission_mode=PermissionMode.ACCEPT_EDITS,
            ), timeout=10)
            attachment = random_id('attachment')
            assert await session.attach_controller(attachment)
            state = _Connection(attachment_id=attachment, attachment_generation=1,
                host_session=session, granted_role=wire.ATTACHMENT_ROLE_CONTROLLER, authenticated=True)
            gateway = TerminalKernelProtocolServer(socket_path=tmp_path/'protocol.sock', session_provider=lambda _: session)
            command = random_id('command')
            request = wire.CommandRequest(request_id=random_id('request'), command_id=command,
                client_submission_id=command, command_kind=wire.SUBMIT_PROMPT,
                prompt_content=wire.PromptContent(parts=[wire.PromptContentPart(text='message after this plan')]),
                requested_permission_mode=wire.PERMISSION_MODE_ACCEPT_EDITS)
            queued = await gateway._command(state, request)
            assert queued.command_outcome.status == wire.PENDING
            assert queued.command_outcome.public_code == 'PROMPT_QUEUED'
            assert queued.command_outcome.command_id == command
            assert (await session.query_command(command)).prompt_delivery.queue_status == 'PENDING'
            assert session.has_controller_attachment(attachment)
            with session.repository.connection_provider.connection(
                lane=PostgresConnectionLane.INSPECTOR, row_factory=dict_row,
                deadline_monotonic=monotonic()+5,
            ) as connection:
                item = connection.execute('SELECT * FROM pulsara_v3.prompt_queue_items WHERE session_id=%s AND command_id=%s', (session.session_id, command)).fetchone()
                assert item['status'] == 'PENDING'
                assert item['permission_snapshot_id'] is None
                assert item['requested_permission_mode'] == 'accept-edits'
                review = connection.execute("SELECT i.id, w.workflow_revision FROM pulsara_v3.plan_interactions i JOIN pulsara_v3.plan_workflows w ON w.id=i.plan_workflow_id AND w.session_id=i.session_id WHERE i.session_id=%s AND i.kind='DRAFT_REVIEW' AND i.status='OPEN'", (session.session_id,)).fetchone()
            assert review is not None
            cancelled = await session.resolve_plan_draft_review(
                command_id=random_id('command'), workflow_id=entered.target_id,
                expected_workflow_revision=int(review['workflow_revision']),
                interaction_id=str(review['id']), decision=PlanDraftDecision.CANCEL, revision_prompt=None,
            )
            assert cancelled.draft_decision is PlanDraftDecision.CANCEL
            await asyncio.wait_for(model.implementation_seen.wait(), timeout=10)
            admitted = await gateway._command(state, request)
            assert admitted.command_outcome.status in (wire.PENDING, wire.SUCCEEDED)
            assert admitted.command_outcome.prompt_delivery.queue_status == 'CONSUMED'
            with session.repository.connection_provider.connection(
                lane=PostgresConnectionLane.INSPECTOR, row_factory=dict_row,
                deadline_monotonic=monotonic()+5,
            ) as connection:
                assert connection.execute('SELECT count(*) AS n FROM pulsara_v3.prompt_queue_items WHERE session_id=%s AND command_id=%s', (session.session_id, command)).fetchone()['n'] == 1
        finally:
            await core.close_session(session.host_session_id)
            await core.shutdown()

    asyncio.run(scenario())


class _QuestionSteerModel(_DetachedDraftModel):
    async def stream(self, request: object) -> AsyncIterator[object]:
        self.requests.append(request)
        if len(self.requests) == 1:
            payloads = _tool_call("ask_plan_question", "call:steer-question", {
                "question": "Which path?", "options": [], "allow_free_text": True,
            })
        elif len(self.requests) == 2:
            self.implementation_seen.set()
            payloads = _text("QUESTION_STEER_RESUMED")
        else:
            raise AssertionError("unexpected question Steer provider cycle")
        for payload in payloads:
            yield payload


def test_plan_question_steer_cancels_real_tool_then_resumes_same_readonly_turn(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, stage2_migrated_postgres_database,
) -> None:
    import pulsara_agent.conversation_kernel.host as kernel_host
    from pulsara_agent.identity import random_id

    model = _QuestionSteerModel()
    monkeypatch.setattr(kernel_host, "DirectKernelModelPort", lambda **_: model)
    monkeypatch.setattr(kernel_host.LocalMcpManagementService, "load_configs", lambda *_args, **_kwargs: ())
    runtime = test_model_runtime(postgres_dsn=stage2_migrated_postgres_database.runtime_dsn)

    async def scenario() -> None:
        core = KernelHostCore.production(model_runtime=runtime)
        session = await core.open_session(HostWorkspaceInput(workspace_kind="project", workspace_root=tmp_path))
        await session.update_model_call_binding(test_model_binding(runtime))
        try:
            await session.enter_plan(command_id=random_id("command"), entry_reason="steer question",
                                     resume_permission_mode=PermissionMode.ACCEPT_EDITS)
            running = asyncio.create_task(session.run_turn(PromptContent.text("prepare plan"),
                command_id=random_id("command"), requested_permission_mode=PermissionMode.ACCEPT_EDITS))
            deadline = monotonic() + 10
            opened = None
            while opened is None:
                if running.done():
                    await running
                    pytest.fail("turn ended before question")
                opened = await session._plan_interactions.current_open()
                assert monotonic() < deadline
                await asyncio.sleep(0.01)
            turn_id = session._active_turn_id
            assert turn_id is not None
            command = random_id("command")
            outcome = await session.steer_active_turn(command_id=command,
                content=PromptContent.text("Use the rollback path instead."), target_turn_id=turn_id)
            assert outcome.status in {"PENDING", "SUCCEEDED"}
            await asyncio.wait_for(running, timeout=10)
            assert len(model.requests) == 2
            assert model.requests[0].turn_id == model.requests[1].turn_id == turn_id
            original, current = (item.compiled_input for item in model.requests)
            assert current.system_prompt == original.system_prompt
            assert current.tools == original.tools
            assert current.messages[:len(original.messages)] == original.messages
            rendered = "\n".join(part for message in model.requests[1].compiled_input.messages
                                  for part in text_part_values(message.content))
            assert "QUESTION_CANCELLED" in rendered
            assert "被用户取消" in rendered
            assert rendered.index("QUESTION_CANCELLED") < rendered.index("Use the rollback path instead.")
            with session.repository.connection_provider.connection(
                lane=PostgresConnectionLane.INSPECTOR, row_factory=dict_row,
                deadline_monotonic=monotonic()+5,
            ) as connection:
                question = connection.execute("SELECT status, answer_kind, control_tool_result_id FROM pulsara_v3.plan_interactions WHERE session_id=%s AND kind='QUESTION'", (session.session_id,)).fetchone()
                assert question["status"] == "CANCELLED"
                assert question["answer_kind"] is None
                result = connection.execute("SELECT result_state, result_origin_kind FROM pulsara_v3.tool_results WHERE id=%s", (question["control_tool_result_id"],)).fetchone()
                assert result == {"result_state": "CANCELLED_BEFORE_DISPATCH", "result_origin_kind": "PLAN_CONTROL"}
                turns = connection.execute("SELECT id, effective_permission_mode FROM pulsara_v3.turns WHERE session_id=%s", (session.session_id,)).fetchall()
                assert turns == [{"id": turn_id, "effective_permission_mode": PermissionMode.READ_ONLY.value}]
            # Repeating the accepted command does not create a second cancellation.
            await session.cancel_plan_question_for_steer(command_id=command, target_turn_id=turn_id)
        finally:
            await core.close_session(session.host_session_id)
            await core.shutdown()

    asyncio.run(scenario())


@pytest.mark.parametrize("from_queue", [False, True])
@pytest.mark.parametrize("pure_image", [False, True])
def test_plan_revision_full_prompt_direct_and_queued_share_admission(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, stage2_migrated_postgres_database,
    from_queue: bool, pure_image: bool,
) -> None:
    import pulsara_agent.conversation_kernel.host as kernel_host
    from pulsara_agent.identity import random_id
    from pulsara_agent.llm.input import PromptImagePart, LLMImagePart
    from pulsara_agent.terminal_protocol.canonical_v3 import CanonicalProtocolReader
    from PIL import Image
    from io import BytesIO

    runtime = test_model_runtime(postgres_dsn=stage2_migrated_postgres_database.runtime_dsn,
                                 input_modalities=("text", "image"), wire_api="openai_chat_completions")
    model = _DetachedDraftModel()
    model.release.set()
    model._preparer = DirectKernelModelPort(model_runtime=runtime)
    monkeypatch.setattr(kernel_host, "DirectKernelModelPort", lambda **_: model)
    monkeypatch.setattr(kernel_host.LocalMcpManagementService, "load_configs", lambda *_args, **_kwargs: ())
    image_bytes = BytesIO()
    Image.new("RGB", (7, 5), (10, 20, 30)).save(image_bytes, "PNG")
    payload = image_bytes.getvalue()
    prompt = (PromptContent((PromptImagePart(payload, "image/png"),)) if pure_image
              else PromptContent.text("Add a rollback step."))

    async def scenario() -> None:
        core = KernelHostCore.production(model_runtime=runtime)
        session = await core.open_session(HostWorkspaceInput(workspace_kind="project", workspace_root=tmp_path))
        await session.update_model_call_binding(test_model_binding(runtime))
        try:
            await session.enter_plan(command_id=random_id("command"), entry_reason="review revisions",
                                     resume_permission_mode=PermissionMode.ACCEPT_EDITS)
            await session.run_turn(PromptContent.text("prepare plan"), command_id=random_id("command"),
                                   requested_permission_mode=PermissionMode.ACCEPT_EDITS)
            with session.repository.connection_provider.connection(
                lane=PostgresConnectionLane.INSPECTOR, row_factory=dict_row,
                deadline_monotonic=monotonic()+5,
            ) as connection:
                review = connection.execute("SELECT i.id, i.plan_workflow_id, w.workflow_revision FROM pulsara_v3.plan_interactions i JOIN pulsara_v3.plan_workflows w ON w.id=i.plan_workflow_id WHERE i.session_id=%s AND i.kind='DRAFT_REVIEW' AND i.status='OPEN'", (session.session_id,)).fetchone()
            source = None
            if from_queue:
                queued = await session.submit_prompt(command_id=random_id("command"), content=prompt,
                    requested_permission_mode=PermissionMode.BYPASS_PERMISSIONS)
                source = queued.target_id
            command = random_id("command")
            kwargs = dict(command_id=command, workflow_id=str(review["plan_workflow_id"]),
                expected_workflow_revision=int(review["workflow_revision"]), interaction_id=str(review["id"]),
                decision=PlanDraftDecision.REVISE, revision_prompt=None if from_queue else prompt,
                source_queue_item_id=source)
            if from_queue:
                with pytest.raises(ConversationKernelConflict):
                    await session.resolve_plan_draft_review(**{
                        **kwargs, "command_id": random_id("command"),
                        "expected_workflow_revision": int(review["workflow_revision"]) - 1,
                    })
                with session.repository.connection_provider.connection(
                    lane=PostgresConnectionLane.INSPECTOR, row_factory=dict_row,
                    deadline_monotonic=monotonic()+5,
                ) as connection:
                    pending = connection.execute("SELECT status FROM pulsara_v3.prompt_queue_items WHERE id=%s", (source,)).fetchone()
                    assert pending["status"] == "PENDING"
            resolution = await session.resolve_plan_draft_review(**kwargs)
            await asyncio.wait_for(model.implementation_seen.wait(), timeout=10)
            task = session._active_task
            if task is not None:
                await asyncio.wait_for(asyncio.shield(task), timeout=10)
            repeated = await session.resolve_plan_draft_review(**kwargs)
            assert repeated.continuation_turn_id == resolution.continuation_turn_id
            with pytest.raises(ConversationKernelConflict):
                await session.resolve_plan_draft_review(**{**kwargs, "command_id": random_id("command")})
            assert len(model.requests) == 2
            snapshot = CanonicalProtocolReader(session.repository.connection_provider).snapshot(
                session_id=session.session_id, maximum_entries=64, maximum_control_items=64,
                deadline_monotonic=monotonic()+5)
            entry = next(item for item in snapshot.entries if item.entry_id == resolution.continuation_entry_id)
            assert entry.input_source.command_id == command
            assert entry.input_source.queue_item_id == ""
            if pure_image:
                images = [part for message in model.requests[1].compiled_input.messages
                          for part in message.content if isinstance(part, LLMImagePart)]
                assert len(images) == 1 and images[0].immutable_bytes == payload
            else:
                text = [part for message in model.requests[1].compiled_input.messages
                        for part in text_part_values(message.content)]
                assert "Add a rollback step." in text
            with session.repository.connection_provider.connection(
                lane=PostgresConnectionLane.INSPECTOR, row_factory=dict_row,
                deadline_monotonic=monotonic()+5,
            ) as connection:
                revised = connection.execute("SELECT e.entry_kind, e.source_plan_handoff_kind, t.effective_permission_mode FROM pulsara_v3.transcript_entries e JOIN pulsara_v3.turns t ON t.id=e.turn_id WHERE e.id=%s", (resolution.continuation_entry_id,)).fetchone()
                assert revised == {"entry_kind": "USER_MESSAGE", "source_plan_handoff_kind": "REVISION_REQUESTED", "effective_permission_mode": PermissionMode.READ_ONLY.value}
                if source is not None:
                    queued = connection.execute("SELECT status, terminal_reason FROM pulsara_v3.prompt_queue_items WHERE id=%s", (source,)).fetchone()
                    assert queued == {"status": "CANCELLED", "terminal_reason": "USER_REDIRECTED_TO_STEER"}
        finally:
            await core.close_session(session.host_session_id)
            await core.shutdown()

    asyncio.run(scenario())


def test_plan_multiple_revisions_keep_distinct_user_entries(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, stage2_migrated_postgres_database,
) -> None:
    import pulsara_agent.conversation_kernel.host as kernel_host
    from pulsara_agent.identity import random_id

    class Model(_DetachedDraftModel):
        async def stream(self, request: object) -> AsyncIterator[object]:
            self.requests.append(request)
            index = len(self.requests)
            payloads = (_tool_call("exit_plan", f"call:review-{index}", {"plan": f"draft {index}"})
                        if index < 3 else _text("TWICE_REVISED"))
            for payload in payloads:
                yield payload

    model = Model()
    monkeypatch.setattr(kernel_host, "DirectKernelModelPort", lambda **_: model)
    monkeypatch.setattr(kernel_host.LocalMcpManagementService, "load_configs", lambda *_args, **_kwargs: ())
    runtime = test_model_runtime(postgres_dsn=stage2_migrated_postgres_database.runtime_dsn)

    async def scenario() -> None:
        core = KernelHostCore.production(model_runtime=runtime)
        session = await core.open_session(HostWorkspaceInput(workspace_kind="project", workspace_root=tmp_path))
        await session.update_model_call_binding(test_model_binding(runtime))
        try:
            await session.enter_plan(command_id=random_id("command"), entry_reason="repeat revision",
                                     resume_permission_mode=PermissionMode.ACCEPT_EDITS)
            await session.run_turn(PromptContent.text("plan"), command_id=random_id("command"),
                                   requested_permission_mode=PermissionMode.ACCEPT_EDITS)
            entries = []
            for ordinal in (1, 2):
                with session.repository.connection_provider.connection(
                    lane=PostgresConnectionLane.INSPECTOR, row_factory=dict_row,
                    deadline_monotonic=monotonic()+5,
                ) as connection:
                    review = connection.execute("SELECT i.id, i.plan_workflow_id, w.workflow_revision FROM pulsara_v3.plan_interactions i JOIN pulsara_v3.plan_workflows w ON w.id=i.plan_workflow_id WHERE i.session_id=%s AND i.kind='DRAFT_REVIEW' AND i.status='OPEN'", (session.session_id,)).fetchone()
                resolution = await session.resolve_plan_draft_review(
                    command_id=random_id("command"), workflow_id=str(review["plan_workflow_id"]),
                    expected_workflow_revision=int(review["workflow_revision"]), interaction_id=str(review["id"]),
                    decision=PlanDraftDecision.REVISE, revision_prompt=PromptContent.text(f"revision {ordinal}"))
                entries.append(resolution.continuation_entry_id)
                task = session._active_task
                assert task is not None
                await asyncio.wait_for(asyncio.shield(task), timeout=10)
            assert len(set(entries)) == 2
            assert len(model.requests) == 3
            final_texts = [part for message in model.requests[-1].compiled_input.messages
                           for part in text_part_values(message.content)]
            assert final_texts.count("revision 1") == final_texts.count("revision 2") == 1
        finally:
            await core.close_session(session.host_session_id)
            await core.shutdown()

    asyncio.run(scenario())


@pytest.mark.parametrize("failure_stage", ["measurement", "after_commit"])
def test_accepted_question_steer_failure_terminates_input_and_preserves_results(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, stage2_migrated_postgres_database,
    failure_stage: str,
) -> None:
    import pulsara_agent.conversation_kernel.host as kernel_host
    from pulsara_agent.identity import random_id
    from pulsara_agent.model_input.contracts import ModelInputCompileFailureKind, StructuredModelInputCompileError

    model = _QuestionSteerModel()
    monkeypatch.setattr(kernel_host, "DirectKernelModelPort", lambda **_: model)
    monkeypatch.setattr(kernel_host.LocalMcpManagementService, "load_configs", lambda *_args, **_kwargs: ())
    runtime = test_model_runtime(postgres_dsn=stage2_migrated_postgres_database.runtime_dsn)

    async def scenario() -> None:
        core = KernelHostCore.production(model_runtime=runtime)
        session = await core.open_session(HostWorkspaceInput(workspace_kind="project", workspace_root=tmp_path))
        await session.update_model_call_binding(test_model_binding(runtime))
        try:
            await session.enter_plan(command_id=random_id("command"), entry_reason="failure boundary",
                                     resume_permission_mode=PermissionMode.ACCEPT_EDITS)
            running = asyncio.create_task(session.run_turn(PromptContent.text("plan"),
                command_id=random_id("command"), requested_permission_mode=PermissionMode.ACCEPT_EDITS))
            deadline = monotonic()+10
            while await session._plan_interactions.current_open() is None:
                if running.done():
                    await running
                    pytest.fail("turn ended before question")
                assert monotonic() < deadline
                await asyncio.sleep(0.01)
            target = session._active_turn_id
            assert target is not None
            error = (StructuredModelInputCompileError(ModelInputCompileFailureKind.FULL_REQUIRED_TOOL_RESULT_EXCEEDS_INPUT_BUDGET)
                     if failure_stage == "measurement" else RuntimeError("injected confirmation failure"))
            async def fail(*_args, **_kwargs):
                raise error
            monkeypatch.setattr(session._runner,
                "prepare_plan_question_resolution_input" if failure_stage == "measurement" else "confirm_published_active_root_input", fail)
            command = random_id("command")
            outcome = await asyncio.wait_for(session.steer_active_turn(command_id=command,
                content=PromptContent.text("keep this accepted redirection"), target_turn_id=target), timeout=10)
            assert outcome.status == "REJECTED"
            assert outcome.prompt_delivery.queue_status == "REJECTED"
            with pytest.raises(type(error)):
                await running
            assert await session._plan_interactions.current_open() is None
            assert len(model.requests) == 1
            with session.repository.connection_provider.connection(
                lane=PostgresConnectionLane.INSPECTOR, row_factory=dict_row,
                deadline_monotonic=monotonic()+5,
            ) as connection:
                question = connection.execute("SELECT status, control_tool_result_id FROM pulsara_v3.plan_interactions WHERE session_id=%s AND kind='QUESTION'", (session.session_id,)).fetchone()
                assert question["status"] == ("ABORTED" if failure_stage == "measurement" else "CANCELLED")
                assert (question["control_tool_result_id"] is not None) == (failure_stage == "after_commit")
                turn = connection.execute("SELECT status, terminal_reason FROM pulsara_v3.turns WHERE id=%s", (target,)).fetchone()
                assert turn["status"] == "INTERRUPTED"
                assert turn["terminal_reason"] == ("PROVIDER_INPUT_RESOURCE_EXHAUSTED" if failure_stage == "measurement" else "FOREGROUND_EXECUTION_INTERRUPTED")
                queued = connection.execute("SELECT status, terminal_reason, inline_content FROM pulsara_v3.prompt_queue_items WHERE command_id=%s", (command,)).fetchone()
                assert queued["status"] == "REJECTED"
                assert queued["terminal_reason"] == "TARGET_TURN_TERMINAL"
                assert b"keep this accepted redirection" in bytes(queued["inline_content"])
            repeated = await session.query_command(command)
            assert repeated.status == "REJECTED"
        finally:
            await core.close_session(session.host_session_id)
            await core.shutdown()

    asyncio.run(scenario())


@pytest.mark.parametrize("tool_name", ["ask_plan_question", "exit_plan", "exit_plan_empty", "enter_plan"])
@pytest.mark.parametrize("boundary", ["provider", "plan_transaction"])
def test_accepted_steer_prevents_later_plan_batch_dispatch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, stage2_migrated_postgres_database,
    tool_name: str, boundary: str,
) -> None:
    import threading
    import pulsara_agent.conversation_kernel.host as kernel_host
    from pulsara_agent.identity import random_id

    class Model(_DetachedDraftModel):
        async def stream(self, request: object) -> AsyncIterator[object]:
            self.requests.append(request)
            if len(self.requests) == 1:
                self.started.set()
                if boundary == "provider":
                    await self.release.wait()
                args = {"ask_plan_question": {"question": "choose", "options": [], "allow_free_text": True},
                        "exit_plan": {"plan": "old plan"}, "exit_plan_empty": {}, "enter_plan": {"reason": "old direction"}}[tool_name]
                payloads = _tool_call("exit_plan" if tool_name == "exit_plan_empty" else tool_name, "call:late-plan", args)
            else:
                assert len(self.requests) == 2
                payloads = _text("REDIRECTED_WITHOUT_OLD_PLAN")
            for payload in payloads:
                yield payload

    model = Model()
    monkeypatch.setattr(kernel_host, "DirectKernelModelPort", lambda **_: model)
    monkeypatch.setattr(kernel_host.LocalMcpManagementService, "load_configs", lambda *_args, **_kwargs: ())
    runtime = test_model_runtime(postgres_dsn=stage2_migrated_postgres_database.runtime_dsn)
    entered, release = threading.Event(), threading.Event()

    async def scenario() -> None:
        core = KernelHostCore.production(model_runtime=runtime)
        session = await core.open_session(HostWorkspaceInput(workspace_kind="project", workspace_root=tmp_path))
        await session.update_model_call_binding(test_model_binding(runtime))
        try:
            if tool_name != "enter_plan":
                await session.enter_plan(command_id=random_id("command"), entry_reason="active plan",
                                         resume_permission_mode=PermissionMode.ACCEPT_EDITS)
            if boundary == "plan_transaction":
                original = session.repository.accept_plan_tool_batch
                def blocked(*args, **kwargs):
                    entered.set()
                    assert release.wait(10)
                    return original(*args, **kwargs)
                monkeypatch.setattr(session.repository, "accept_plan_tool_batch", blocked)
            running = asyncio.create_task(session.run_turn(PromptContent.text("old request"),
                command_id=random_id("command"), requested_permission_mode=PermissionMode.ACCEPT_EDITS))
            if boundary == "provider":
                await asyncio.wait_for(model.started.wait(), timeout=10)
            else:
                assert await asyncio.to_thread(entered.wait, 10)
            target = session._active_turn_id
            assert target is not None
            outcome = await session.steer_active_turn(command_id=random_id("command"),
                content=PromptContent.text("New direction replaces the pending plan call."), target_turn_id=target)
            assert outcome.status == "PENDING"
            release.set()
            model.release.set()
            await asyncio.wait_for(running, timeout=10)
            assert len(model.requests) == 2
            rendered = "\n".join(part for message in model.requests[-1].compiled_input.messages
                                  for part in text_part_values(message.content))
            assert "被用户取消" in rendered
            assert "New direction replaces the pending plan call." in rendered
            with session.repository.connection_provider.connection(
                lane=PostgresConnectionLane.INSPECTOR, row_factory=dict_row,
                deadline_monotonic=monotonic()+5,
            ) as connection:
                assert connection.execute("SELECT count(*) AS n FROM pulsara_v3.plan_interactions WHERE session_id=%s", (session.session_id,)).fetchone()["n"] == 0
                result = connection.execute("SELECT result_state, result_origin_kind FROM pulsara_v3.tool_results WHERE session_id=%s AND tool_call_id='call:late-plan'", (session.session_id,)).fetchone()
                assert result == {"result_state": "CANCELLED_BEFORE_DISPATCH", "result_origin_kind": "POLICY_NO_ATTEMPT"}
                assert connection.execute("SELECT count(*) AS n FROM pulsara_v3.turns WHERE session_id=%s", (session.session_id,)).fetchone()["n"] == 1
        finally:
            release.set()
            model.release.set()
            await core.close_session(session.host_session_id)
            await core.shutdown()

    asyncio.run(scenario())
