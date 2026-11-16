"""Plan's storage boundary uses shared calls and one public content value."""

from time import monotonic
from io import BytesIO
import json

import psycopg
from psycopg.rows import dict_row
import pytest
from PIL import Image

from pulsara_agent.conversation_kernel.content_parts import read_content_parts
from pulsara_agent.conversation_kernel.repository import (
    AssistantToolCallBlock,
    AssistantTextBlock,
)
from pulsara_agent.conversation_kernel.contracts import (
    InlineContent,
    PromptDeliveryMode,
)
from pulsara_agent.conversation_kernel.prompt_content import freeze_canonical_prompt
from pulsara_agent.conversation_kernel.reader import CanonicalProviderInputReader
from pulsara_agent.conversation_kernel.steer import (
    build_steer_canonical_base_fence,
    build_steer_consumption_candidate,
)
from pulsara_agent.identity import derive_id
from pulsara_agent.llm.input import (
    FrozenPromptContent,
    LLMTextPart,
    PromptAnnotationPart,
    PromptAnnotationSource,
)
from pulsara_agent.conversation_kernel._repository.contracts import (
    PreparedPlanBatchCall,
    PreparedPlanToolBatch,
    PlanToolControlKind,
    PlanToolBatchDisposition,
    PlanQuestionAnswer,
)
from pulsara_agent.conversation_kernel.tool_outputs import (
    read_accepted_tool_business_value,
)
from pulsara_agent.primitives.context import freeze_json
from pulsara_agent.primitives.plan_workflow import (
    PlanQuestionAnswerKind,
    PlanDraftDecision,
)
from pulsara_agent.model_input.contracts import (
    ModelInputScopeKind,
    provider_input_item_fingerprint,
)
from pulsara_agent.model_input.continuity import (
    ProviderInputContinuityScope,
    ProcessLocalCanonicalFrontier,
    NoNewTriggerAnchor,
)
from tests.support.images import bound_test_image
from tests.support.round3 import new_test_provider_input_continuity_owner
from tests.support.model_config import (
    enqueue_test_prompt,
    test_model_binding as model_binding,
    test_model_runtime as model_runtime,
)
from tests.test_round4_plan_postgres import (
    _repository,
    _lease,
    _start_root,
    _permission,
    _open_user_plan,
    _now,
    _id,
    _active_provider_input_admission,
)


pytestmark = pytest.mark.postgres


def _batch(
    repository,
    lease,
    turn_id,
    *,
    control,
    arguments,
    workflow_id,
    revision=None,
    disposition=PlanToolBatchDisposition.APPLY,
):
    tool_name = {
        PlanToolControlKind.QUESTION: "ask_plan_question",
        PlanToolControlKind.DRAFT: "exit_plan",
        PlanToolControlKind.ENTER: "enter_plan",
    }[control]
    block = AssistantToolCallBlock(
        _id("block"), "provider-plan-call", tool_name, freeze_json(arguments)
    )
    assistant_id = _id("entry")
    repository.commit_assistant_message(
        lease.guard,
        cut=repository.prepare_provider_input_cut(
            lease.guard, turn_id=turn_id, deadline_monotonic=monotonic() + 30
        ),
        entry_id=assistant_id,
        blocks=(
            AssistantTextBlock(_id("block"), InlineContent.from_bytes(b"plan context")),
            block,
        ),
        occurred_at=_now(),
        actor_id="model:test",
        deadline_monotonic=monotonic() + 30,
    )
    rejected = disposition is not PlanToolBatchDisposition.APPLY
    waiting = (
        control in {PlanToolControlKind.QUESTION, PlanToolControlKind.DRAFT}
        and not rejected
    )
    fresh_enter = control is PlanToolControlKind.ENTER and not rejected
    workspace_id = repository.read_session_workspace_id(
        lease.guard, deadline_monotonic=monotonic() + 30
    )
    return PreparedPlanToolBatch(
        session_id=lease.guard.session_id,
        workspace_id=workspace_id,
        origin_turn_id=turn_id,
        assistant_entry_id=assistant_id,
        selected_call_ordinal=0,
        control_kind=control,
        selected_arguments=block.arguments,
        permission_snapshot=_permission(repository, lease, turn_id),
        workflow_id=workflow_id,
        expected_workflow_revision=revision,
        interaction_id=_id("plan-interaction") if waiting else None,
        continuation_turn_id=_id("turn") if fresh_enter else None,
        continuation_entry_id=_id("entry") if fresh_enter else None,
        continuation_context_binding_revision_id=_id("binding")
        if fresh_enter
        else None,
        calls=(
            PreparedPlanBatchCall(
                block.block_id,
                block.tool_call_id,
                block.tool_name,
                block.arguments,
                None if waiting else _id("result"),
                None if waiting else _id("entry"),
            ),
        ),
        occurred_at=_now(),
        actor_id="runtime:test",
        selected_disposition=disposition,
    )


def _assert_result(database, session_id, result_id, expected_text):
    with psycopg.connect(database.runtime_dsn, row_factory=dict_row) as connection:
        result = connection.execute(
            "SELECT * FROM pulsara_v3.tool_results WHERE id=%s", (result_id,)
        ).fetchone()
        parts = read_content_parts(
            connection, session_id=session_id, entry_id=result["result_entry_id"]
        )
        assert len(parts) == 1 and parts[0].type == "text"
        assert parts[0].value == expected_text
        assert result["result_facts"] == {}
        assert result["public_fields"] == {}
        value = read_accepted_tool_business_value(connection, result)
        assert value["content"][0]["value"] == expected_text
        return value


def test_plan_enter_and_rejected_results_save_only_public_parts(
    isolated_migrated_postgres_database,
):
    database = isolated_migrated_postgres_database
    repository = _repository(database)
    lease = _lease(repository)
    turn_id, _ = _start_root(repository, lease)
    candidate = _batch(
        repository,
        lease,
        turn_id,
        control=PlanToolControlKind.ENTER,
        arguments={},
        workflow_id=_id("plan"),
    )
    accepted = repository.accept_plan_tool_batch(
        lease.guard, candidate=candidate, deadline_monotonic=monotonic() + 30
    )
    assert (
        repository.confirm_plan_tool_batch_winner(
            candidate=candidate, deadline_monotonic=monotonic() + 30
        )
        == accepted
    )
    value = _assert_result(
        database, lease.guard.session_id, candidate.calls[0].result_id, "已进入计划模式。"
    )
    assert value["meta"]["status"] == "SUCCESS"
    rejected = _batch(
        repository,
        lease,
        accepted.continuation_turn_id,
        control=PlanToolControlKind.ENTER,
        arguments={},
        workflow_id=None,
        disposition=PlanToolBatchDisposition.HOOK_BLOCKED,
    )
    rejection = repository.accept_plan_tool_batch(
        lease.guard, candidate=rejected, deadline_monotonic=monotonic() + 30
    )
    assert (
        repository.confirm_plan_tool_batch_winner(
            candidate=rejected, deadline_monotonic=monotonic() + 30
        )
        == rejection
    )
    assert (
        _assert_result(
            database, lease.guard.session_id, rejected.calls[0].result_id, "这项 Plan 操作已被 Hook 阻止。"
        )["meta"]["status"]
        == "ERROR"
    )


def test_plan_question_arguments_and_answer_use_the_shared_content_path(
    isolated_migrated_postgres_database,
):
    database = isolated_migrated_postgres_database
    repository = _repository(database)
    lease = _lease(repository)
    workflow_id, _ = _open_user_plan(repository, lease)
    turn_id, _ = _start_root(repository, lease)
    arguments = {"question": "Which path?", "options": []}
    candidate = _batch(
        repository,
        lease,
        turn_id,
        control=PlanToolControlKind.QUESTION,
        arguments=arguments,
        workflow_id=workflow_id,
        revision=1,
    )
    opened = repository.accept_plan_tool_batch(
        lease.guard, candidate=candidate, deadline_monotonic=monotonic() + 30
    )
    question = repository.read_plan_question_content(
        session_id=lease.guard.session_id,
        interaction_id=opened.interaction_id,
        deadline_monotonic=monotonic() + 30,
    )
    assert question.question == "Which path?"
    answer = PlanQuestionAnswer(
        PlanQuestionAnswerKind.FREE_TEXT, free_text="Use the whole answer\n原文"
    )
    result_id, result_entry_id, occurred_at, command_id = (
        _id("result"),
        _id("entry"),
        _now(),
        _id("command"),
    )
    parameters = dict(
        workflow_id=workflow_id,
        expected_workflow_revision=opened.workflow_revision,
        interaction_id=opened.interaction_id,
        answer=answer,
        result_id=result_id,
        result_entry_id=result_entry_id,
        occurred_at=occurred_at,
    )
    provider_candidate = (
        repository.prepare_plan_question_resolution_provider_input_candidate(
            lease.guard, **parameters, deadline_monotonic=monotonic() + 30
        )
    )
    resolved = repository.resolve_plan_question(
        lease.guard,
        command_id=command_id,
        **parameters,
        actor_id="user:test",
        provider_input_admission=_active_provider_input_admission(provider_candidate),
        deadline_monotonic=monotonic() + 30,
    )
    assert resolved.interaction_status == "ANSWERED"
    value = _assert_result(
        database, lease.guard.session_id, result_id, answer.free_text
    )
    assert value["content"][0]["value"] == answer.free_text


def _open_draft(database):
    repository = _repository(database)
    lease = _lease(repository)
    workflow_id, _ = _open_user_plan(repository, lease)
    turn_id, _ = _start_root(repository, lease)
    candidate = _batch(
        repository,
        lease,
        turn_id,
        control=PlanToolControlKind.DRAFT,
        arguments={"plan": "# Plan\n\n1. Read the code\n2. Implement"},
        workflow_id=workflow_id,
        revision=1,
    )
    opened = repository.accept_plan_tool_batch(
        lease.guard, candidate=candidate, deadline_monotonic=monotonic() + 30
    )
    assert not opened.origin_turn_completed
    assert (
        opened.selected_result_entry_id is None and opened.tool_result_settlements == ()
    )
    with psycopg.connect(database.runtime_dsn, row_factory=dict_row) as connection:
        assert (
            connection.execute(
                "SELECT status FROM pulsara_v3.turns WHERE id=%s", (turn_id,)
            ).fetchone()["status"]
            == "RUNNING"
        )
        assert (
            connection.execute(
                "SELECT control_tool_result_id FROM pulsara_v3.plan_interactions WHERE id=%s",
                (opened.interaction_id,),
            ).fetchone()["control_tool_result_id"]
            is None
        )
    return repository, lease, workflow_id, turn_id, candidate, opened


@pytest.mark.parametrize(
    "queued,mixed,inject_failure",
    [
        (False, False, False),
        (True, False, False),
        (True, True, False),
        (True, True, True),
    ],
)
def test_plan_revision_transfers_full_feedback_and_stays_in_original_turn(
    isolated_migrated_postgres_database, queued, mixed, inject_failure, monkeypatch
):
    database = isolated_migrated_postgres_database
    repository, lease, workflow_id, turn_id, candidate, opened = _open_draft(database)
    previous_permission = _permission(repository, lease, turn_id)
    encoded = BytesIO()
    Image.new("RGB", (2, 2), (20, 40, 60)).save(encoded, format="PNG")
    image = bound_test_image("image/png", encoded.getvalue(), 2, 2)
    feedback_parts = (
        (
            LLMTextPart("before\n"),
            image,
            PromptAnnotationPart(
                "plan",
                PromptAnnotationSource(candidate.assistant_entry_id, 0, 4),
                "retain annotation",
            ),
            LLMTextPart("after"),
        )
        if mixed
        else (image,)
    )
    prompt = freeze_canonical_prompt(FrozenPromptContent(feedback_parts))
    source_id = None
    original_parts = None
    if queued:
        source_id = _id("queue")
        permission = repository.prepare_root_permission_snapshot(
            lease.guard,
            snapshot_id=_id("permission"),
            requested_mode=previous_permission.requested_mode,
            queued_user_observation=True,
            deadline_monotonic=monotonic() + 30,
        )
        enqueue_test_prompt(
            repository,
            lease.guard,
            command_id=_id("command"),
            queue_item_id=source_id,
            client_submission_id=_id("command"),
            delivery_mode=PromptDeliveryMode.NEW_TURN,
            target_turn_id=None,
            permission_snapshot_id=permission.snapshot_id,
            requested_permission_mode=previous_permission.requested_mode,
            _expected_permission_snapshot=permission,
            model_call_binding=model_binding(model_runtime()),
            content=prompt.content,
            occurred_at=_now(),
            actor_id="user:test",
            deadline_monotonic=monotonic() + 30,
        )
        with psycopg.connect(database.runtime_dsn, row_factory=dict_row) as connection:
            original_parts = read_content_parts(connection, queue_item_id=source_id)
    command_id = _id("command")
    parameters = dict(
        command_id=command_id,
        workflow_id=workflow_id,
        expected_workflow_revision=opened.workflow_revision,
        interaction_id=opened.interaction_id,
        decision=PlanDraftDecision.REVISE,
        canonical_prompt=prompt,
        continuation_turn_id=None,
        continuation_entry_id=None,
        continuation_context_binding_revision_id=None,
        source_queue_item_id=source_id,
    )
    if inject_failure:
        from pulsara_agent.conversation_kernel._repository import plans

        transfer = plans.move_queue_content_parts

        def moved_then_failed(*args, **kwargs):
            transfer(*args, **kwargs)
            raise RuntimeError("injected after content transfer")

        with monkeypatch.context() as context:
            context.setattr(plans, "move_queue_content_parts", moved_then_failed)
            with pytest.raises(RuntimeError, match="after content transfer"):
                repository.resolve_plan_draft_review(
                    lease.guard,
                    **parameters,
                    occurred_at=_now(),
                    actor_id="user:test",
                    deadline_monotonic=monotonic() + 30,
                )
        with psycopg.connect(database.runtime_dsn, row_factory=dict_row) as connection:
            assert (
                read_content_parts(connection, queue_item_id=source_id)
                == original_parts
            )
            assert (
                connection.execute(
                    "SELECT status FROM pulsara_v3.prompt_queue_items WHERE id=%s",
                    (source_id,),
                ).fetchone()["status"]
                == "PENDING"
            )
            assert (
                connection.execute(
                    "SELECT status FROM pulsara_v3.plan_interactions WHERE id=%s",
                    (opened.interaction_id,),
                ).fetchone()["status"]
                == "OPEN"
            )
            assert (
                connection.execute(
                    "SELECT count(*) AS n FROM pulsara_v3.tool_results WHERE control_plan_interaction_id=%s",
                    (opened.interaction_id,),
                ).fetchone()["n"]
                == 0
            )
    resolution = repository.resolve_plan_draft_review(
        lease.guard,
        **parameters,
        occurred_at=_now(),
        actor_id="user:test",
        deadline_monotonic=monotonic() + 30,
    )
    assert (
        resolution.continuation_turn_id is None
        and resolution.continuation_entry_id is None
    )
    value = json.loads(
        resolution.tool_result_settlement.public_projection.canonical_body
    )
    assert value == {
        "meta": {"status": "SUCCESS"},
        "content": [{"type": "text", "value": "用户要求修订方案。"}],
    }
    assert _permission(repository, lease, turn_id) == previous_permission
    with psycopg.connect(database.runtime_dsn, row_factory=dict_row) as connection:
        feedback_id = derive_id(
            "queue", "plan-review-feedback", opened.interaction_id, command_id
        )
        feedback = connection.execute(
            "SELECT * FROM pulsara_v3.prompt_queue_items WHERE id=%s", (feedback_id,)
        ).fetchone()
        actual = read_content_parts(connection, queue_item_id=feedback_id)
        assert (
            feedback["target_turn_id"] == turn_id
            and feedback["delivery_mode"] == "STEER_ACTIVE_TURN"
        )
        assert [part.type for part in actual] == (
            ["text", "image", "annotation", "text"] if mixed else ["image"]
        )
        if queued:
            assert actual == original_parts
            assert read_content_parts(connection, queue_item_id=source_id) == ()
            assert (
                connection.execute(
                    "SELECT status FROM pulsara_v3.prompt_queue_items WHERE id=%s",
                    (source_id,),
                ).fetchone()["status"]
                == "CANCELLED"
            )
        assert (
            connection.execute(
                "SELECT status FROM pulsara_v3.turns WHERE id=%s", (turn_id,)
            ).fetchone()["status"]
            == "RUNNING"
        )
        assert (
            connection.execute(
                "SELECT status FROM pulsara_v3.plan_workflows WHERE id=%s",
                (workflow_id,),
            ).fetchone()["status"]
            == "ACTIVE"
        )
        assert (
            connection.execute(
                "SELECT count(*) AS n FROM pulsara_v3.tool_results WHERE control_plan_interaction_id=%s",
                (opened.interaction_id,),
            ).fetchone()["n"]
            == 1
        )
    confirmed = repository.confirm_plan_draft_review_winner(
        session_id=lease.guard.session_id,
        **parameters,
        deadline_monotonic=monotonic() + 30,
    )
    assert confirmed == resolution
    # The same production steer consumer transfers this exact owner to an entry.
    cut = repository.prepare_provider_input_cut(
        lease.guard, turn_id=turn_id, deadline_monotonic=monotonic() + 30
    )
    reader = CanonicalProviderInputReader(repository.connection_provider)
    snapshot = reader.read_frozen_compile_snapshot(
        cut, deadline_monotonic=monotonic() + 30
    )
    context = snapshot.context_binding_fact
    owner = new_test_provider_input_continuity_owner(lease.guard.session_id)
    planning = owner.freeze_planning_input(
        scope=ProviderInputContinuityScope(
            lease.guard.session_id, ModelInputScopeKind.ROOT
        ),
        canonical_frontier=ProcessLocalCanonicalFrontier(
            context.binding_revision_id,
            context.context_base_semantic_identity,
            cut.provider_input_through_sequence,
            tuple(
                provider_input_item_fingerprint(item)
                for item in snapshot.canonical_input.items
            ),
        ),
        dispatch_anchor=NoNewTriggerAnchor(None),
    )
    fact = repository.read_pending_prompt_steer_facts(
        session_id=lease.guard.session_id,
        target_turn_id=turn_id,
        deadline_monotonic=monotonic() + 30,
    )[0]
    hydrated = repository.hydrate_pending_prompt_steer(
        fact=fact, deadline_monotonic=monotonic() + 30
    )
    assert hydrated == prompt
    consume = build_steer_consumption_candidate(
        fact=fact,
        canonical_prompt=hydrated,
        expected_entry_sequence=cut.provider_input_through_sequence + 1,
        predecessor=planning,
        canonical_base_fence=build_steer_canonical_base_fence(snapshot),
        occurred_at=_now(),
        actor_id=lease.guard.writer_owner_id,
    )
    accepted = repository.consume_prepared_prompt_steer(
        lease.guard, candidate=consume, deadline_monotonic=monotonic() + 30
    )
    with psycopg.connect(database.runtime_dsn, row_factory=dict_row) as connection:
        assert read_content_parts(connection, entry_id=accepted.entry_id) == actual
        assert read_content_parts(connection, queue_item_id=feedback_id) == ()
        row = connection.execute(
            "SELECT turn_id,entry_sequence,input_origin FROM pulsara_v3.transcript_entries WHERE id=%s",
            (accepted.entry_id,),
        ).fetchone()
        assert row["turn_id"] == turn_id and row["input_origin"] == "HUMAN_STEER"
        assert (
            row["entry_sequence"]
            > resolution.tool_result_settlement.accepted_entry_sequence
        )
    assert (
        repository.confirm_plan_draft_review_winner(
            session_id=lease.guard.session_id,
            **parameters,
            deadline_monotonic=monotonic() + 30,
        )
        == resolution
    )
    if queued and mixed and not inject_failure:
        replacement = _batch(
            repository,
            lease,
            turn_id,
            control=PlanToolControlKind.DRAFT,
            arguments={"plan": "# Replacement plan"},
            workflow_id=workflow_id,
            revision=resolution.workflow_revision,
        )
        reopened = repository.accept_plan_tool_batch(
            lease.guard, candidate=replacement, deadline_monotonic=monotonic() + 30
        )
        assert reopened.workflow_revision > resolution.workflow_revision
        assert (
            repository.confirm_plan_draft_review_winner(
                session_id=lease.guard.session_id,
                **parameters,
                deadline_monotonic=monotonic() + 30,
            )
            == resolution
        )


def test_plan_review_cancel_saves_one_final_result_and_keeps_planning_active(
    isolated_migrated_postgres_database,
):
    database = isolated_migrated_postgres_database
    repository, lease, workflow_id, turn_id, _, opened = _open_draft(database)
    resolution = repository.resolve_plan_draft_review(
        lease.guard,
        command_id=_id("command"),
        workflow_id=workflow_id,
        expected_workflow_revision=opened.workflow_revision,
        interaction_id=opened.interaction_id,
        decision=PlanDraftDecision.CANCEL,
        canonical_prompt=None,
        continuation_turn_id=None,
        continuation_entry_id=None,
        continuation_context_binding_revision_id=None,
        occurred_at=_now(),
        actor_id="user:test",
        deadline_monotonic=monotonic() + 30,
    )
    assert (
        resolution.workflow_status.value == "ACTIVE"
        and not resolution.handoff_created_at_commit
    )
    assert resolution.tool_result_settlement.result_state == "CANCELLED"
    with psycopg.connect(database.runtime_dsn, row_factory=dict_row) as connection:
        assert (
            connection.execute(
                "SELECT status FROM pulsara_v3.turns WHERE id=%s", (turn_id,)
            ).fetchone()["status"]
            == "RUNNING"
        )
        assert (
            connection.execute(
                "SELECT count(*) AS n FROM pulsara_v3.tool_results WHERE control_plan_interaction_id=%s",
                (opened.interaction_id,),
            ).fetchone()["n"]
            == 1
        )


def test_plan_approval_alone_finishes_origin_and_creates_execution_successor(
    isolated_migrated_postgres_database,
):
    database = isolated_migrated_postgres_database
    repository, lease, workflow_id, turn_id, _, opened = _open_draft(database)
    command_id = _id("command")
    parameters = dict(
        command_id=command_id,
        workflow_id=workflow_id,
        expected_workflow_revision=opened.workflow_revision,
        interaction_id=opened.interaction_id,
        decision=PlanDraftDecision.APPROVE,
        canonical_prompt=None,
        continuation_turn_id=_id("turn"),
        continuation_entry_id=_id("entry"),
        continuation_context_binding_revision_id=_id("binding"),
    )
    resolution = repository.resolve_plan_draft_review(
        lease.guard,
        **parameters,
        occurred_at=_now(),
        actor_id="user:test",
        deadline_monotonic=monotonic() + 30,
    )
    assert resolution.tool_result_settlement.result_state == "SUCCESS"
    assert resolution.continuation_turn_id == parameters["continuation_turn_id"]
    with psycopg.connect(database.runtime_dsn, row_factory=dict_row) as connection:
        assert (
            connection.execute(
                "SELECT status FROM pulsara_v3.turns WHERE id=%s", (turn_id,)
            ).fetchone()["status"]
            == "COMPLETED"
        )
        successor = connection.execute(
            "SELECT status,effective_permission_mode,permission_overlay FROM pulsara_v3.turns WHERE id=%s",
            (resolution.continuation_turn_id,),
        ).fetchone()
        assert successor == {
            "status": "RUNNING",
            "effective_permission_mode": "accept-edits",
            "permission_overlay": "NONE",
        }
        assert (
            connection.execute(
                "SELECT count(*) AS n FROM pulsara_v3.tool_results WHERE control_plan_interaction_id=%s",
                (opened.interaction_id,),
            ).fetchone()["n"]
            == 1
        )
    assert (
        repository.confirm_plan_draft_review_winner(
            session_id=lease.guard.session_id,
            **parameters,
            deadline_monotonic=monotonic() + 30,
        )
        == resolution
    )
