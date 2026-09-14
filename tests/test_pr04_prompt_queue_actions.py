"""PR04 exact queue actions and their canonical settlement winner."""

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from time import monotonic
from types import SimpleNamespace
from typing import cast

import pytest
from psycopg.rows import dict_row

from pulsara_agent.conversation_kernel.contracts import (
    InlineContent,
    PromptDeliveryMode,
)
from pulsara_agent.conversation_kernel.prompt_content import freeze_canonical_prompt
from pulsara_agent.llm.input import FrozenPromptContent
from pulsara_agent.conversation_kernel.queued_prompt_actions import (
    QueuedPromptAction,
    QueuedPromptActionRejected,
)
from pulsara_agent.conversation_kernel.repository import AssistantTextBlock
from pulsara_agent.conversation_kernel.steer import PreparedRootProviderInputAdmission
from pulsara_agent.model_input.contracts import PreparedProviderInputCut
from pulsara_agent.storage.postgres_connection_provider import PostgresConnectionLane
from tests.test_stage2_conversation_kernel_postgres import (
    _enqueue_prompt,
    _name,
    _repository,
    _start_root_turn,
)

pytestmark = pytest.mark.postgres


@pytest.fixture
def queue_case(stage2_migrated_postgres_database):
    repository = _repository(stage2_migrated_postgres_database)
    guard = repository.acquire_host_writer(
        session_id=_name("session"),
        workspace_id=_name("workspace"),
        writer_owner_id=_name("host"),
        lease_seconds=300,
        deadline_monotonic=monotonic() + 30,
    ).guard
    turn_id, revision_id = _name("turn"), _name("revision")
    _start_root_turn(
        repository,
        guard,
        command_id=_name("initial-command"),
        turn_id=turn_id,
        entry_id=_name("initial-entry"),
        context_binding_revision_id=revision_id,
        content=FrozenPromptContent.text('initial'),
        occurred_at=datetime.now(timezone.utc),
        deadline_monotonic=monotonic() + 30,
    )
    source, submission = _name("source"), _name("submission")
    _enqueue_prompt(
        repository,
        guard,
        command_id=submission,
        queue_item_id=source,
        client_submission_id=submission,
        delivery_mode=PromptDeliveryMode.NEW_TURN,
        target_turn_id=None,
        content=FrozenPromptContent.text('exact queued\ninput'),
        occurred_at=datetime.now(timezone.utc),
        actor_id="user",
        deadline_monotonic=monotonic() + 30,
    )
    return repository, guard, turn_id, revision_id, source, submission


def action(case, *, command=None, steer=True, source=None):
    _, guard, turn, _, original, _ = case
    return QueuedPromptAction(
        session_id=guard.session_id,
        command_id=command or _name("action"),
        source_queue_item_id=source or original,
        target_turn_id=turn if steer else None,
    )


def apply(case, candidate):
    repository, guard, *_ = case
    return repository.apply_queued_prompt_action(
        guard,
        candidate=candidate,
        occurred_at=datetime.now(timezone.utc),
        actor_id="user",
        deadline_monotonic=monotonic() + 30,
    )


def rows(case):
    repository, guard, *_ = case
    with repository.connection_provider.connection(
        lane=PostgresConnectionLane.INSPECTOR,
        row_factory=dict_row,
        deadline_monotonic=monotonic() + 30,
    ) as connection:
        return connection.execute(
            "SELECT * FROM pulsara_v3.prompt_queue_items WHERE session_id=%s ORDER BY queue_sequence",
            (guard.session_id,),
        ).fetchall()


def complete(case):
    repository, guard, turn, revision, *_ = case
    return repository.commit_assistant_message(
        guard,
        cut=PreparedProviderInputCut(
            session_id=guard.session_id,
            turn_id=turn,
            context_binding_revision_id=revision,
            provider_input_through_sequence=1,
        ),
        entry_id=_name("assistant"),
        parent_content=InlineContent.from_bytes(b"terminal-looking answer"),
        blocks=(AssistantTextBlock(_name("block"), InlineContent.from_bytes(b"done")),),
        complete_turn=True,
        occurred_at=datetime.now(timezone.utc),
        actor_id="model",
        deadline_monotonic=monotonic() + 30,
    )


def test_two_request_cancel_then_steer_can_lose_input(queue_case):
    """Deterministic reproduction: cancellation commits before ROOT closes."""
    from pulsara_agent.conversation_kernel._repository.contracts import (
        PromptIngressRejected,
    )

    repository, guard, turn, _, source, _ = queue_case
    repository.cancel_prompt(
        guard,
        queue_item_id=source,
        occurred_at=datetime.now(timezone.utc),
        actor_id="user",
        deadline_monotonic=monotonic() + 30,
    )
    assert complete(queue_case).turn_completed
    with pytest.raises(PromptIngressRejected):
        _enqueue_prompt(
            repository,
            guard,
            command_id=_name("unsafe-command"),
            queue_item_id=_name("unsafe-replacement"),
            client_submission_id=_name("client"),
            delivery_mode=PromptDeliveryMode.STEER_ACTIVE_TURN,
            target_turn_id=turn,
            content=FrozenPromptContent.text('exact queued\ninput'),
            occurred_at=datetime.now(timezone.utc),
            actor_id="user",
            deadline_monotonic=monotonic() + 30,
        )
    assert [(row["status"], row["consumed_entry_id"]) for row in rows(queue_case)] == [
        ("CANCELLED", None)
    ]


def test_two_request_steer_then_cancel_can_duplicate_input(queue_case):
    """The second request may never arrive; both lanes then own the body."""
    repository, guard, turn, *_ = queue_case
    _enqueue_prompt(
        repository,
        guard,
        command_id=_name("unsafe-command"),
        queue_item_id=_name("unsafe-replacement"),
        client_submission_id=_name("client"),
        delivery_mode=PromptDeliveryMode.STEER_ACTIVE_TURN,
        target_turn_id=turn,
        content=FrozenPromptContent.text('exact queued\ninput'),
        occurred_at=datetime.now(timezone.utc),
        actor_id="user",
        deadline_monotonic=monotonic() + 30,
    )
    source, replacement = rows(queue_case)
    assert source["status"] == replacement["status"] == "PENDING"
    assert source["inline_content"] == replacement["inline_content"]
    assert source["delivery_mode"] == "NEW_TURN"
    assert replacement["delivery_mode"] == "STEER_ACTIVE_TURN"


def test_redirect_first_preserves_source_and_holds_same_root_open(queue_case):
    candidate = action(queue_case)
    before = rows(queue_case)[0]
    apply(queue_case, candidate)
    source, replacement = rows(queue_case)
    assert source["status"] == "CANCELLED"
    assert source["terminal_reason"] == "USER_REDIRECTED_TO_STEER"
    assert replacement["id"] == candidate.replacement_queue_item_id
    assert replacement["command_id"] == candidate.command_id
    assert replacement["target_turn_id"] == queue_case[2]
    assert replacement["delivery_mode"] == "STEER_ACTIVE_TURN"
    assert replacement["queue_sequence"] > source["queue_sequence"]
    for key in (
        "inline_content",
        "blob_id",
        "content_digest",
        "content_size",
        "content_media_type",
        "content_codec",
    ):
        assert replacement[key] == before[key]
    for key in before:
        if "permission" in key or "handoff" in key or key == "model_call_binding":
            assert source[key] == before[key]
            assert replacement[key] is None
    accepted = complete(queue_case)
    assert not accepted.turn_completed
    assert accepted.pending_steer_at_settlement
    assert accepted.turn_id == queue_case[2]


def test_completion_first_rejects_redirect_without_losing_fifo_source(queue_case):
    before = rows(queue_case)
    assert complete(queue_case).turn_completed
    with pytest.raises(QueuedPromptActionRejected, match="STEER_TARGET_CLOSED"):
        apply(queue_case, action(queue_case))
    assert rows(queue_case) == before


def test_action_ack_loss_recovers_exact_command_and_conflict_does_not_mutate(
    queue_case,
):
    candidate = action(queue_case)
    apply(queue_case, candidate)  # caller loses this acknowledgement
    before = rows(queue_case)
    apply(queue_case, candidate)
    repository, guard, *_ = queue_case
    observed = repository.query_command(
        session_id=guard.session_id,
        command_id=candidate.command_id,
        deadline_monotonic=monotonic() + 30,
    )
    assert observed["command_kind"] == "STEER_QUEUED_PROMPT"
    assert observed["target_queue_item_id"] == candidate.replacement_queue_item_id
    with pytest.raises(QueuedPromptActionRejected, match="COMMAND_CONFLICT"):
        apply(queue_case, action(queue_case, command=candidate.command_id, steer=False))
    assert rows(queue_case) == before


@pytest.mark.parametrize("steers", [(False, False), (False, True), (True, True)])
def test_concurrent_actions_have_one_exact_source_winner(queue_case, steers):
    candidates = [action(queue_case, steer=steer) for steer in steers]

    def attempt(candidate):
        try:
            apply(queue_case, candidate)
            return "ACCEPTED"
        except QueuedPromptActionRejected as error:
            return error.code

    with ThreadPoolExecutor(max_workers=2) as workers:
        outcomes = list(workers.map(attempt, candidates))
    assert sorted(outcomes) == ["ACCEPTED", "PROMPT_NOT_PENDING"]
    assert rows(queue_case)[0]["status"] == "CANCELLED"


def test_cancel_command_is_durably_idempotent(queue_case):
    candidate = action(queue_case, steer=False)
    apply(queue_case, candidate)
    before = rows(queue_case)
    apply(queue_case, candidate)
    assert rows(queue_case) == before
    repository, guard, *_ = queue_case
    command = repository.query_command(
        session_id=guard.session_id,
        command_id=candidate.command_id,
        deadline_monotonic=monotonic() + 30,
    )
    assert command["command_kind"] == "CANCEL_PROMPT"
    assert command["queue_status"] == "CANCELLED"
    assert command["target_queue_item_id"] == candidate.source_queue_item_id


def test_redirect_reuses_large_blob_without_materializing_another_copy(queue_case):
    repository, guard, _, _, _, _ = queue_case
    body = ("  原文\n" * 20000).encode("utf-8")
    assert len(body) > 64 << 10
    content = FrozenPromptContent.text(body.decode("utf-8"))
    canonical_prompt = freeze_canonical_prompt(content)
    source = _name("large-source")
    _enqueue_prompt(
        repository,
        guard,
        command_id=_name("large-command"),
        queue_item_id=source,
        client_submission_id=_name("large-submission"),
        delivery_mode=PromptDeliveryMode.NEW_TURN,
        target_turn_id=None,
        content=content,
        occurred_at=datetime.now(timezone.utc),
        actor_id="user",
        deadline_monotonic=monotonic() + 30,
    )
    candidate = action(queue_case, source=source)
    apply(queue_case, candidate)
    original, replacement = rows(queue_case)[1:]
    assert original["blob_id"] == replacement["blob_id"]
    assert original["blob_id"] is not None
    assert original["inline_content"] is replacement["inline_content"] is None
    assert repository._content_from_row(replacement) == repository._content_from_row(
        original
    )
    with repository.connection_provider.connection(
        lane=PostgresConnectionLane.INSPECTOR, deadline_monotonic=monotonic() + 30
    ) as connection:
        assert (
            connection.execute(
                "SELECT body FROM pulsara_v3.blobs WHERE id=%s",
                (original["blob_id"],),
            ).fetchone()[0]
            == canonical_prompt.body
        )


def test_fifo_consumption_wins_over_both_queue_actions(queue_case):
    repository, guard, *_ = queue_case
    assert complete(queue_case).turn_completed
    candidate = repository.prepare_prompt_head_consumption(
        session_id=guard.session_id,
        occurred_at=datetime.now(timezone.utc),
        actor_id="runtime",
        deadline_monotonic=monotonic() + 30,
    )
    assert candidate is not None
    repository.consume_prepared_prompt_head(
        guard,
        candidate=candidate,
        provider_input_admission=cast(
            PreparedRootProviderInputAdmission,
            SimpleNamespace(candidate=candidate.provider_input_candidate),
        ),
        deadline_monotonic=monotonic() + 30,
    )
    before = rows(queue_case)
    assert before[0]["status"] == "CONSUMED"
    for steer in (False, True):
        with pytest.raises(QueuedPromptActionRejected, match="PROMPT_ALREADY_CONSUMED"):
            apply(queue_case, action(queue_case, steer=steer))
    assert rows(queue_case) == before


def test_plan_handoff_redirect_rejects_without_changing_source_or_action(
    stage2_migrated_postgres_database,
):
    from tests.test_round4_plan_postgres import _lease, _open_user_plan
    from pulsara_agent.primitives.permission import PermissionMode

    repository = _repository(stage2_migrated_postgres_database)
    lease = _lease(repository)
    workflow, _ = _open_user_plan(repository, lease)
    repository.exit_plan_by_user(
        lease.guard,
        command_id=_name("cancel-plan"),
        command_kind="CANCEL_PLAN",
        workflow_id=workflow,
        expected_workflow_revision=1,
        occurred_at=datetime.now(timezone.utc),
        actor_id="user",
        deadline_monotonic=monotonic() + 30,
    )
    source = _name("handoff-source")
    _enqueue_prompt(
        repository,
        lease.guard,
        command_id=_name("handoff-command"),
        queue_item_id=source,
        client_submission_id=_name("submission"),
        delivery_mode=PromptDeliveryMode.NEW_TURN,
        target_turn_id=None,
        content=FrozenPromptContent.text('handoff'),
        requested_permission_mode=PermissionMode.READ_ONLY,
        occurred_at=datetime.now(timezone.utc),
        actor_id="user",
        deadline_monotonic=monotonic() + 30,
    )
    turn, revision = _name("target"), _name("revision")
    _start_root_turn(
        repository,
        lease.guard,
        command_id=_name("root-command"),
        turn_id=turn,
        entry_id=_name("entry"),
        context_binding_revision_id=revision,
        content=FrozenPromptContent.text('root'),
        occurred_at=datetime.now(timezone.utc),
        deadline_monotonic=monotonic() + 30,
    )
    case = (repository, lease.guard, turn, revision, source, "")
    before = rows(case)
    assert before[0]["pending_plan_handoff_workflow_id"] == workflow
    candidate = action(case)
    with pytest.raises(QueuedPromptActionRejected, match="PROMPT_HAS_PLAN_HANDOFF"):
        apply(case, candidate)
    assert rows(case) == before
    assert (
        repository.query_command(
            session_id=lease.guard.session_id,
            command_id=candidate.command_id,
            deadline_monotonic=monotonic() + 30,
        )
        is None
    )
