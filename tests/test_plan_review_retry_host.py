"""Plan command retries preserve the existing waiter and accepted input owner."""

import asyncio
from contextlib import asynccontextmanager
from dataclasses import replace
from time import monotonic
from types import SimpleNamespace

from psycopg.rows import dict_row
import pytest

from pulsara_agent.conversation_kernel.direct_model import DirectKernelModelPort
from pulsara_agent.conversation_kernel.host_plans import _HostPlanOperations
from pulsara_agent.conversation_kernel.plan_runtime import (
    ContinuationAdmissionOwner,
    KernelPlanInteractionCoordinator,
)
from pulsara_agent.conversation_kernel.repository import (
    ConversationKernelConflict,
    PlanQuestionAnswer,
)
from pulsara_agent.conversation_kernel.tool_outputs import (
    read_accepted_tool_business_value,
)
from pulsara_agent.identity import derive_id, random_id
from pulsara_agent.llm.input import PromptContent
from pulsara_agent.primitives.permission import PermissionMode
from pulsara_agent.primitives.plan_workflow import (
    PlanDraftDecision,
    PlanQuestionAnswerKind,
)
from pulsara_agent.storage.postgres_connection_provider import PostgresConnectionLane
from tests.test_round4_plan_host import (
    _PreflightModel,
    _open_test_session,
    _runtime,
    _text,
    _tool_call,
)

pytestmark = pytest.mark.postgres


@asynccontextmanager
async def _open_review(
    tmp_path,
    monkeypatch,
    database,
    *,
    tool_name="exit_plan",
    arguments=None,
):
    class Model(_PreflightModel):
        def __init__(self):
            self._preparer = DirectKernelModelPort(
                model_runtime=_runtime(database.runtime_dsn)
            )
            self.requests = []

        async def stream(self, request):
            self.requests.append(request)
            if len(self.requests) == 1:
                payloads = _tool_call(
                    tool_name,
                    "review-call",
                    {"plan": "方案\n\n步骤"} if arguments is None else arguments,
                )
            else:
                assert len(self.requests) == 2
                payloads = _text("决定后继续")
            for payload in payloads:
                yield payload

    model = Model()
    core, session = await _open_test_session(
        tmp_path=tmp_path,
        model=model,
        postgres_dsn=database.runtime_dsn,
        monkeypatch=monkeypatch,
    )
    running = None
    try:
        await session.enter_plan(
            command_id=random_id("command"),
            entry_reason="planning",
            resume_permission_mode=PermissionMode.ACCEPT_EDITS,
        )
        running = asyncio.create_task(
            session.run_turn(
                PromptContent.text("提交方案"),
                command_id=random_id("command"),
                requested_permission_mode=PermissionMode.ACCEPT_EDITS,
            )
        )
        async with asyncio.timeout(10):
            while (opened := await session._plan_interactions.current_open()) is None:
                if running.done():
                    await running
                    pytest.fail("origin ended before review opened")
                await asyncio.sleep(0.01)
        with _read(session) as connection:
            row = connection.execute(
                """SELECT i.*,w.workflow_revision FROM pulsara_v3.plan_interactions i
                   JOIN pulsara_v3.plan_workflows w ON w.id=i.plan_workflow_id WHERE i.id=%s""",
                (opened.interaction_id,),
            ).fetchone()
        yield SimpleNamespace(
            session=session,
            model=model,
            running=running,
            params=dict(
                command_id=random_id("command"),
                workflow_id=row["plan_workflow_id"],
                expected_workflow_revision=row["workflow_revision"],
                interaction_id=opened.interaction_id,
            ),
        )
    finally:
        await core.close_session(session.host_session_id)
        if running is not None:
            await asyncio.gather(running, return_exceptions=True)
        await core.shutdown()


def _read(session):
    return session.repository.connection_provider.connection(
        lane=PostgresConnectionLane.INSPECTOR,
        row_factory=dict_row,
        deadline_monotonic=monotonic() + 10,
    )


def _assert_resumed(review, decision, resolution):
    assert len(review.model.requests) == 2
    initial, resumed = review.model.requests
    if decision is PlanDraftDecision.APPROVE:
        assert resumed.turn_id == resolution.continuation_turn_id != initial.turn_id
        mode = PermissionMode.ACCEPT_EDITS.value
    else:
        assert (
            resumed.turn_id == initial.turn_id
            and resolution.continuation_turn_id is None
        )
        mode = PermissionMode.READ_ONLY.value
        before, after = initial.compiled_input, resumed.compiled_input
        assert after.system_prompt == before.system_prompt
        assert after.tools == before.tools
        assert after.messages[: len(before.messages)] == before.messages
        messages = after.messages
        result_positions = [
            i
            for i, message in enumerate(messages)
            if message.tool_call_id == "review-call"
        ]
        feedback_positions = [
            i
            for i, message in enumerate(messages)
            if any(
                "修订\r\n反馈" in getattr(part, "text", "") for part in message.content
            )
        ]
        assert len(result_positions) == len(feedback_positions) == 1
        assert result_positions[0] < feedback_positions[0]
    with _read(review.session) as connection:
        assert (
            connection.execute(
                "SELECT effective_permission_mode FROM pulsara_v3.turns WHERE id=%s",
                (resumed.turn_id,),
            ).fetchone()["effective_permission_mode"]
            == mode
        )
        assert (
            connection.execute(
                "SELECT count(*) AS n FROM pulsara_v3.tool_results WHERE session_id=%s AND tool_call_id='review-call'",
                (review.session.session_id,),
            ).fetchone()["n"]
            == 1
        )


def _observe_retry_join(monkeypatch, command_id):
    joined = asyncio.Event()
    original = ContinuationAdmissionOwner.start
    attempts = []

    def start(self, **kwargs):
        attempt = original(self, **kwargs)
        if kwargs["attempt_id"] == ("plan-review-continuation", command_id):
            attempts.append(attempt)
            if len(attempts) == 2:
                assert attempts[0] is attempts[1]
                joined.set()
        return attempt

    monkeypatch.setattr(ContinuationAdmissionOwner, "start", start)
    return joined


def test_concurrent_approval_retry_joins_handoff_before_waking_origin(
    tmp_path,
    monkeypatch,
    stage2_migrated_postgres_database,
):
    async def run():
        async with _open_review(
            tmp_path, monkeypatch, stage2_migrated_postgres_database
        ) as review:
            session = review.session
            entered, release = asyncio.Event(), asyncio.Event()
            joined = _observe_retry_join(monkeypatch, review.params["command_id"])
            original_inspect = _HostPlanOperations._inspect_plan_continuation

            async def inspect(self, **kwargs):
                entered.set()
                await release.wait()
                return await original_inspect(self, **kwargs)

            monkeypatch.setattr(
                _HostPlanOperations, "_inspect_plan_continuation", inspect
            )
            params = dict(review.params, decision=PlanDraftDecision.APPROVE)
            first = asyncio.create_task(session.resolve_plan_draft_review(**params))
            second = None
            try:
                await asyncio.wait_for(entered.wait(), 10)
                with pytest.raises(ConversationKernelConflict):
                    await session.resolve_plan_draft_review(
                        **{**params, "decision": PlanDraftDecision.CANCEL}
                    )
                second = asyncio.create_task(
                    session.resolve_plan_draft_review(**params)
                )
                await asyncio.wait_for(joined.wait(), 10)
                assert not second.done()
                assert not review.running.done()
                assert len(review.model.requests) == 1
            finally:
                release.set()
                tasks = [first] if second is None else [first, second]
                resolutions = await asyncio.wait_for(asyncio.gather(*tasks), 10)
            assert resolutions[0] == resolutions[1]
            await asyncio.wait_for(asyncio.shield(review.running), 10)
            _assert_resumed(review, PlanDraftDecision.APPROVE, resolutions[0])
            assert await session.resolve_plan_draft_review(**params) == replace(
                resolutions[0],
                handoff_created_at_commit=False,
            )

    asyncio.run(run())


def test_queued_revision_retries_read_transferred_and_consumed_feedback(
    tmp_path,
    monkeypatch,
    stage2_migrated_postgres_database,
):
    async def run():
        async with _open_review(
            tmp_path, monkeypatch, stage2_migrated_postgres_database
        ) as review:
            session = review.session
            queued = await session.submit_prompt(
                command_id=random_id("command"),
                content=PromptContent.text("修订\r\n反馈"),
            )
            params = dict(
                review.params,
                decision=PlanDraftDecision.REVISE,
                source_queue_item_id=queued.target_id,
            )
            feedback_id = derive_id(
                "queue",
                "plan-review-feedback",
                params["interaction_id"],
                params["command_id"],
            )
            entered, release = asyncio.Event(), asyncio.Event()
            joined = _observe_retry_join(monkeypatch, review.params["command_id"])
            original_settle = KernelPlanInteractionCoordinator.settle

            async def settle(self, **kwargs):
                if kwargs["interaction_id"] == params["interaction_id"]:
                    entered.set()
                    await release.wait()
                return await original_settle(self, **kwargs)

            monkeypatch.setattr(KernelPlanInteractionCoordinator, "settle", settle)
            first = asyncio.create_task(session.resolve_plan_draft_review(**params))
            second = None
            try:
                await asyncio.wait_for(entered.wait(), 10)
                with _read(session) as connection:
                    assert (
                        connection.execute(
                            "SELECT status FROM pulsara_v3.prompt_queue_items WHERE id=%s",
                            (feedback_id,),
                        ).fetchone()["status"]
                        == "PENDING"
                    )
                other_source = await session.submit_prompt(
                    command_id=random_id("command"),
                    content=PromptContent.text("修订\r\n反馈"),
                )
                with pytest.raises(ConversationKernelConflict):
                    await session.resolve_plan_draft_review(
                        **{**params, "source_queue_item_id": other_source.target_id}
                    )
                await session.cancel_queued_prompt(
                    command_id=random_id("command"),
                    source_queue_item_id=other_source.target_id,
                )
                second = asyncio.create_task(
                    session.resolve_plan_draft_review(**params)
                )
                await asyncio.wait_for(joined.wait(), 10)
                assert not second.done()
                assert not review.running.done()
            finally:
                release.set()
                tasks = [first] if second is None else [first, second]
                resolutions = await asyncio.wait_for(asyncio.gather(*tasks), 10)
            assert resolutions[0] == resolutions[1]
            await asyncio.wait_for(asyncio.shield(review.running), 10)
            _assert_resumed(review, PlanDraftDecision.REVISE, resolutions[0])
            with _read(session) as connection:
                assert (
                    connection.execute(
                        "SELECT status FROM pulsara_v3.prompt_queue_items WHERE id=%s",
                        (feedback_id,),
                    ).fetchone()["status"]
                    == "CONSUMED"
                )
            assert await session.resolve_plan_draft_review(**params) == resolutions[0]
            for changed in (
                {"command_id": random_id("command")},
                {
                    "expected_workflow_revision": params["expected_workflow_revision"]
                    + 1
                },
                {
                    "source_queue_item_id": None,
                    "revision_prompt": PromptContent.text("不同反馈"),
                },
            ):
                with pytest.raises(ConversationKernelConflict):
                    await session.resolve_plan_draft_review(**{**params, **changed})

    asyncio.run(run())


@pytest.mark.parametrize(
    "decision", [PlanDraftDecision.APPROVE, PlanDraftDecision.REVISE]
)
def test_retry_finishes_confirmed_decision_after_write_confirmation_detaches(
    tmp_path,
    monkeypatch,
    stage2_migrated_postgres_database,
    decision,
):
    async def run():
        async with _open_review(
            tmp_path, monkeypatch, stage2_migrated_postgres_database
        ) as review:
            session = review.session
            original_resolve = type(session.repository).resolve_plan_draft_review
            calls = []

            def commit_then_unavailable(self, *args, **kwargs):
                calls.append(kwargs)
                if len(calls) == 1:
                    original_resolve(self, *args, **kwargs)
                raise ConnectionError(
                    "injected lost commit acknowledgement / unavailable confirmation"
                )

            params = dict(review.params, decision=decision)
            if decision is PlanDraftDecision.REVISE:
                params["revision_prompt"] = PromptContent.text("修订\r\n反馈")
            with monkeypatch.context() as context:
                context.setattr(
                    type(session.repository),
                    "resolve_plan_draft_review",
                    commit_then_unavailable,
                )
                with pytest.raises(ConnectionError):
                    await session.resolve_plan_draft_review(**params)
            assert len(calls) == 2 and not review.running.done()

            def no_new_write(*args, **kwargs):
                pytest.fail(
                    "confirmed decision retry must not write the decision again"
                )

            with monkeypatch.context() as context:
                context.setattr(
                    type(session.repository), "resolve_plan_draft_review", no_new_write
                )
                resolution = await session.resolve_plan_draft_review(**params)
            await asyncio.wait_for(asyncio.shield(review.running), 10)
            _assert_resumed(review, decision, resolution)

    asyncio.run(run())


@pytest.mark.parametrize(
    "answer_kind", [PlanQuestionAnswerKind.OPTION, PlanQuestionAnswerKind.FREE_TEXT]
)
def test_question_answer_prospective_input_matches_saved_result_and_resumes(
    tmp_path,
    monkeypatch,
    stage2_migrated_postgres_database,
    answer_kind,
):
    async def run():
        async with _open_review(
            tmp_path,
            monkeypatch,
            stage2_migrated_postgres_database,
            tool_name="ask_plan_question",
            arguments={
                "question": "Which path?",
                "options": [{"label": "Safe"}, {"label": "Fast"}],
                "allow_free_text": True,
            },
        ) as review:
            session = review.session
            answer = (
                PlanQuestionAnswer(answer_kind, option_ordinal=0)
                if answer_kind is PlanQuestionAnswerKind.OPTION
                else PlanQuestionAnswer(
                    answer_kind, free_text="Use the full answer\n  保留正文"
                )
            )
            resolution = await session.resolve_plan_question(
                **review.params, answer=answer
            )
            await asyncio.wait_for(asyncio.shield(review.running), 10)
            assert resolution.interaction_status == "ANSWERED"
            assert len(review.model.requests) == 2
            first, resumed = review.model.requests
            assert resumed.turn_id == first.turn_id
            before, after = first.compiled_input, resumed.compiled_input
            assert after.system_prompt == before.system_prompt
            assert after.tools == before.tools
            assert after.messages[: len(before.messages)] == before.messages
            results = [
                message
                for message in after.messages
                if message.tool_call_id == "review-call"
            ]
            assert len(results) == 1
            received = "".join(getattr(part, "text", "") for part in results[0].content)
            assert (
                "Safe"
                if answer_kind is PlanQuestionAnswerKind.OPTION
                else "Use the full answer"
            ) in received
            with _read(session) as connection:
                rows = connection.execute(
                    "SELECT * FROM pulsara_v3.tool_results WHERE session_id=%s AND tool_call_id='review-call'",
                    (session.session_id,),
                ).fetchall()
                assert len(rows) == 1
                value = read_accepted_tool_business_value(connection, rows[0])
                expected_answer = (
                    {"kind": "OPTION", "ordinal": 0, "label": "Safe"}
                    if answer_kind is PlanQuestionAnswerKind.OPTION
                    else {"kind": "FREE_TEXT", "text": answer.free_text}
                )
                assert value == {
                    "meta": {"status": "SUCCESS"},
                    "content": [
                        {
                            "type": "object",
                            "value": {
                                "plan_control": "QUESTION_ANSWERED",
                                "answer": expected_answer,
                            },
                        }
                    ],
                }
                assert (
                    connection.execute(
                        "SELECT effective_permission_mode FROM pulsara_v3.turns WHERE id=%s",
                        (resumed.turn_id,),
                    ).fetchone()["effective_permission_mode"]
                    == PermissionMode.READ_ONLY.value
                )

    asyncio.run(run())
