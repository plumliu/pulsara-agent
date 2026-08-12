from __future__ import annotations

import asyncio
import json
from pathlib import Path
from time import monotonic
from typing import AsyncIterator

from psycopg.rows import dict_row
import pytest

from pulsara_agent.conversation_kernel.direct_model import DirectKernelModelPort
from pulsara_agent.conversation_kernel.host import KernelHostCore
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
from pulsara_agent.primitives.plan_workflow import (
    PlanDraftDecision,
    PlanQuestionAnswerKind,
)
from pulsara_agent.settings import PulsaraSettings, StorageConfig
from pulsara_agent.storage.postgres_connection_provider import PostgresConnectionLane
from pulsara_agent.workspace_identity import HostWorkspaceInput
from tests.support.model_config import test_llm_config


pytestmark = pytest.mark.postgres

_PLAN_SENTINEL = "ROUND4_APPROVED_PLAN_SENTINEL"


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


class _PlanHostModel:
    def __init__(self) -> None:
        self.requests: list[object] = []
        self.question_opened = asyncio.Event()
        self.implementation_seen = asyncio.Event()
        self._preparer = DirectKernelModelPort(
            config=test_llm_config(
                api_key="test",
                base_url="https://example.invalid/v1",
                pro_model="test-pro",
                flash_model="test-flash",
                api="openai_chat_completions",
            )
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
                    "plan": (
                        f"1. inspect\n2. implement {_PLAN_SENTINEL}\n3. verify"
                    ),
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
                        for part in message.content
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


class _EnterPlanThenTextModel:
    def __init__(self) -> None:
        self.requests: list[object] = []
        self.completed = asyncio.Event()
        self._preparer = DirectKernelModelPort(
            config=test_llm_config(
                api_key="test",
                base_url="https://example.invalid/v1",
                pro_model="test-pro",
                flash_model="test-flash",
                api="openai_chat_completions",
            )
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


class _DetachedDraftModel:
    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.implementation_seen = asyncio.Event()
        self.requests: list[object] = []
        self._preparer = DirectKernelModelPort(
            config=test_llm_config(
                api_key="test",
                base_url="https://example.invalid/v1",
                pro_model="test-pro",
                flash_model="test-flash",
                api="openai_chat_completions",
            )
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


class _ForceExitRaceModel:
    def __init__(self) -> None:
        self.requests: list[object] = []
        self._preparer = DirectKernelModelPort(
            config=test_llm_config(
                api_key="test",
                base_url="https://example.invalid/v1",
                pro_model="test-pro",
                flash_model="test-flash",
                api="openai_chat_completions",
            )
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


class _BlockingPlanTextModel:
    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self._preparer = DirectKernelModelPort(
            config=test_llm_config(
                api_key="test",
                base_url="https://example.invalid/v1",
                pro_model="test-pro",
                flash_model="test-flash",
                api="openai_chat_completions",
            )
        )

    def prepare_call(self, request):
        return self._preparer.prepare_call(request)

    async def stream(self, request: object) -> AsyncIterator[object]:
        del request
        self.started.set()
        await self.release.wait()
        for payload in _text("PLAN_TURN_REMAINED_LIVE"):
            yield payload


def _settings(postgres_dsn: str) -> PulsaraSettings:
    return PulsaraSettings(
        llm=test_llm_config(
            api_key="test",
            base_url="https://example.invalid/v1",
            pro_model="test-pro",
            flash_model="test-flash",
            api="openai_chat_completions",
        ),
        storage=StorageConfig(postgres_dsn=postgres_dsn),
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
    monkeypatch.setattr(kernel_host, "load_mcp_server_configs", lambda **_: ())
    core = KernelHostCore.production(settings=_settings(postgres_dsn))
    session = await core.open_session(
        HostWorkspaceInput(workspace_kind="project", workspace_root=tmp_path)
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
    monkeypatch.setattr(kernel_host, "load_mcp_server_configs", lambda **_: ())
    settings = PulsaraSettings(
        llm=test_llm_config(
            api_key="test",
            base_url="https://example.invalid/v1",
            pro_model="test-pro",
            flash_model="test-flash",
            api="openai_chat_completions",
        ),
        storage=StorageConfig(
            postgres_dsn=stage2_migrated_postgres_database.runtime_dsn,
        ),
    )

    async def scenario() -> None:
        core = KernelHostCore.production(settings=settings)
        session = await core.open_session(
            HostWorkspaceInput(
                workspace_kind="project",
                workspace_root=tmp_path,
            )
        )
        running = asyncio.create_task(
            session.run_turn(
                "Make a plan before editing.",
                command_id="command:round4-plan",
                requested_permission_mode=PermissionMode.ACCEPT_EDITS,
            )
        )
        await asyncio.wait_for(model.question_opened.wait(), timeout=5)
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
            command_id="command:round4-answer",
            workflow_id=workflow_id,
            expected_workflow_revision=2,
            interaction_id=opened.interaction_id,
            answer=PlanQuestionAnswer(
                PlanQuestionAnswerKind.OPTION, option_ordinal=0
            ),
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
            command_id="command:round4-approve",
            workflow_id=workflow_id,
            expected_workflow_revision=4,
            interaction_id=draft_run.pending_plan_interaction_id,
            decision=PlanDraftDecision.APPROVE,
            feedback=None,
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
        await core.close_session(session.host_session_id, close_conversation=True)
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
            command_id="command:queued-plan-chain",
            text="Plan this queued request.",
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
        await core.close_session(session.host_session_id, close_conversation=True)
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
            command_id="command:detached-enter",
            entry_reason="prepare detached review",
            resume_permission_mode=PermissionMode.ACCEPT_EDITS,
        )
        running = asyncio.create_task(
            session.run_turn(
                "Draft the plan.",
                command_id="command:detached-origin",
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
            command_id="command:detached-approve",
            workflow_id=entered.target_id,
            expected_workflow_revision=int(draft["workflow_revision"]),
            interaction_id=str(draft["id"]),
            decision=PlanDraftDecision.APPROVE,
            feedback=None,
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
                "SELECT status FROM pulsara_v3.turns "
                "WHERE session_id = %s AND id = %s",
                (session.session_id, approved.continuation_turn_id),
            ).fetchone()
        assert successor_row == {"status": "COMPLETED"}
        await core.close_session(session.host_session_id, close_conversation=True)
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
                "Enter Plan while force-exit races.",
                command_id="command:force-race-origin",
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
                command_id="command:force-race-exit",
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
                "SELECT status FROM pulsara_v3.turns "
                "WHERE session_id = %s AND id = %s",
                (session.session_id, successor["id"]),
            ).fetchone()
        assert successor_row == {"status": "INTERRUPTED"}
        assert len(model.requests) == 1
        await core.close_session(session.host_session_id, close_conversation=True)
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
            command_id="command:stale-force-enter",
            entry_reason="validate before cancelling",
            resume_permission_mode=PermissionMode.READ_ONLY,
        )
        running = asyncio.create_task(
            session.run_turn(
                "Keep this exact turn alive.",
                command_id="command:stale-force-origin",
            )
        )
        await asyncio.wait_for(model.started.wait(), timeout=5)
        with pytest.raises(ConversationKernelConflict, match="target drifted"):
            await session.force_exit_plan(
                command_id="command:stale-force",
                workflow_id="workflow:stale",
                expected_workflow_revision=entered.plan_workflow_revision,
            )
        assert not running.done()
        assert session._active_task is not None  # noqa: SLF001
        model.release.set()
        result = await asyncio.wait_for(running, timeout=5)
        assert result.final_text == "PLAN_TURN_REMAINED_LIVE"
        cancelled = await session.cancel_plan(
            command_id="command:stale-force-cleanup",
            workflow_id=entered.target_id,
            expected_workflow_revision=entered.plan_workflow_revision,
        )
        assert cancelled.public_code == "PLAN_CANCELLED"
        await core.close_session(session.host_session_id, close_conversation=True)
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
            command_id="command:terminalize-enter",
            entry_reason="exercise terminalization retry",
            resume_permission_mode=PermissionMode.ACCEPT_EDITS,
        )
        running = asyncio.create_task(
            session.run_turn(
                "Draft a bounded plan.",
                command_id="command:terminalize-origin",
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
                command_id="command:terminalize-approve",
                workflow_id=entered.target_id,
                expected_workflow_revision=int(workflow["workflow_revision"]),
                interaction_id=origin.pending_plan_interaction_id,
                decision=PlanDraftDecision.APPROVE,
                feedback=None,
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
                "SELECT status FROM pulsara_v3.turns "
                "WHERE session_id = %s AND id = %s",
                (session.session_id, approved.continuation_turn_id),
            ).fetchone()
        assert successor == {"status": "INTERRUPTED"}
        assert not model.implementation_seen.is_set()
        await core.close_session(session.host_session_id, close_conversation=True)
        await core.shutdown()

    asyncio.run(scenario())
