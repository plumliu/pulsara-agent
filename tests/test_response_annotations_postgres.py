"""Exercise source admission and child-owned annotation bodies in real PostgreSQL."""

from datetime import datetime, timezone
from dataclasses import replace
from time import monotonic
import pytest
from psycopg.rows import dict_row

from tests import test_conversation_fork as fork_support
from tests.test_conversation_fork import (
    new_session,
    turn,
    final,
    fork,
    rows,
    identity,
    compact,
)
from tests.support.model_config import (
    start_test_root_turn,
    test_model_binding as model_binding,
    test_model_runtime as model_runtime,
)
from pulsara_agent.llm.input import (
    FrozenPromptContent,
    PromptAnnotationPart,
    PromptAnnotationSource,
    LLMTextPart,
    prompt_provider_parts,
)
from pulsara_agent.conversation_kernel.annotations import AnnotationSourceInvalid
from pulsara_agent.conversation_kernel.prompt_storage import (
    hydrate_canonical_prompt_owner,
    hydrate_canonical_snapshot_owner,
)
from pulsara_agent.primitives.permission import DEFAULT_PERMISSION_MODE
from pulsara_agent.storage.postgres_connection_provider import PostgresConnectionLane
from pulsara_agent.model_input.contracts import (
    FrozenRetainedHistoricalRequest,
    FrozenProviderInputItemKind,
    CanonicalInputOriginKind,
)

repo = fork_support.repo
pytestmark = pytest.mark.postgres


def submit(repo, guard, content):
    turn_id = identity("turn")
    start_test_root_turn(
        repo,
        guard,
        command_id=identity("command"),
        turn_id=turn_id,
        permission_snapshot_id=identity("permission"),
        requested_permission_mode=DEFAULT_PERMISSION_MODE,
        entry_id=identity("entry"),
        context_binding_revision_id=identity("revision"),
        content=content,
        occurred_at=datetime.now(timezone.utc),
        deadline_monotonic=monotonic() + 30,
        model_call_binding=model_binding(model_runtime()),
    )
    return turn_id


def read_content(repo, session_id):
    with repo.connection_provider.connection(
        lane=PostgresConnectionLane.INSPECTOR,
        row_factory=dict_row,
        deadline_monotonic=monotonic() + 30,
    ) as connection:
        entries = connection.execute(
            "SELECT * FROM pulsara_v3.transcript_entries WHERE session_id=%s AND entry_kind='USER_MESSAGE' ORDER BY entry_sequence",
            (session_id,),
        ).fetchall()
        return [
            (
                row,
                hydrate_canonical_prompt_owner(
                    connection, row=row, transcript_entry_id=row["id"]
                ).content,
            )
            for row in entries
        ]


@pytest.mark.parametrize("large", [False, True])
def test_source_validation_and_two_forks_keep_child_owned_anchors(repo, large):
    guard = new_session(repo).guard
    _, _, source = turn(repo, guard, "start", answer="甲😀乙")
    a = PromptAnnotationPart("😀", PromptAnnotationSource(source, 1, 3), "解释")
    for invalid in (
        replace(a, quote="不同"),
        replace(a, source=PromptAnnotationSource(source, 1, 2)),
        replace(a, source=PromptAnnotationSource("entry:missing", 0, 2)),
    ):
        with pytest.raises(AnnotationSourceInvalid):
            submit(repo, guard, FrozenPromptContent((invalid,)))
    other = new_session(repo).guard
    with pytest.raises(AnnotationSourceInvalid):
        submit(repo, other, FrozenPromptContent((a,)))
    content = FrozenPromptContent((a, LLMTextPart("正文" * (20000 if large else 1))))
    turn_id = submit(repo, guard, content)
    anchor = final(repo, guard, turn_id)
    current = guard.session_id
    original_model = prompt_provider_parts(content.parts)
    for _ in range(2):
        result = fork(repo, current, anchor)
        assert result.created, result
        current = result.child_session_id
        entries = read_content(repo, current)
        inherited = entries[-1][1]
        assert prompt_provider_parts(inherited.parts) == original_model
        child_source = inherited.parts[0].source.entry_id
        assert child_source != source
        assert rows(
            repo,
            "SELECT id FROM pulsara_v3.transcript_entries WHERE session_id=%s AND id=%s",
            (current, child_source),
        )
        anchor = rows(
            repo,
            "SELECT id FROM pulsara_v3.transcript_entries WHERE session_id=%s AND entry_kind='ASSISTANT_MESSAGE' ORDER BY entry_sequence DESC LIMIT 1",
            (current,),
        )[0]["id"]
    assert read_content(repo, guard.session_id)[-1][1] == content


def test_compacted_fork_preserves_quote_with_absent_source(repo):
    guard = new_session(repo).guard
    _, _, source = turn(repo, guard, "start", answer="旧回复")
    content = FrozenPromptContent(
        (PromptAnnotationPart("旧回复", PromptAnnotationSource(source, 0, 3), "解释"),)
    )
    turn_id = submit(repo, guard, content)
    anchor = final(repo, guard, turn_id)
    sequence = rows(
        repo,
        "SELECT entry_sequence FROM pulsara_v3.transcript_entries WHERE id=%s",
        (anchor,),
    )[0]["entry_sequence"]
    retained = FrozenRetainedHistoricalRequest(
        FrozenProviderInputItemKind.USER,
        CanonicalInputOriginKind.HUMAN_MESSAGE,
        content,
    )
    compact(
        repo,
        guard,
        turn_id,
        sequence,
        idle=True,
        retained_historical_requests=(retained,),
    )
    # A final after the compacted base is the fork anchor; earlier originals aren't imported.
    _, _, anchor = turn(repo, guard, "继续")
    result = fork(repo, guard.session_id, anchor)
    assert result.created, result
    with repo.connection_provider.connection(
        lane=PostgresConnectionLane.INSPECTOR,
        row_factory=dict_row,
        deadline_monotonic=monotonic() + 30,
    ) as connection:
        snapshot = connection.execute(
            "SELECT * FROM pulsara_v3.context_snapshots WHERE session_id=%s",
            (result.child_session_id,),
        ).fetchone()
        carrier = hydrate_canonical_snapshot_owner(
            connection, row=snapshot, context_snapshot_id=snapshot["id"]
        )
        part = carrier.retained_historical_requests[0].content.parts[0]
        assert part.source is None and part.quote == "旧回复"


@pytest.mark.parametrize("mode", ["NEW_TURN", "STEER"])
def test_multiblock_source_and_queued_annotation_roundtrip(repo, mode):
    from pulsara_agent.conversation_kernel.contracts import (
        InlineContent,
        PromptDeliveryMode,
    )
    from pulsara_agent.conversation_kernel.repository import AssistantTextBlock
    from pulsara_agent.conversation_kernel.prompt_content import freeze_canonical_prompt
    from pulsara_agent.conversation_kernel.steer import build_prompt_ingress_command

    guard = new_session(repo).guard
    turn_id, cut, _ = turn(repo, guard, "开始", finish=False)
    source = identity("entry")
    blocks = ("甲😀", "", "乙\r\n尾")
    repo.commit_assistant_message(
        guard,
        cut=cut,
        entry_id=source,
        parent_content=InlineContent.from_bytes(b"manifest"),
        blocks=tuple(
            AssistantTextBlock(
                identity("block"), InlineContent.from_bytes(text.encode())
            )
            for text in blocks
        ),
        complete_turn=mode == "NEW_TURN",
        occurred_at=datetime.now(timezone.utc),
        actor_id="model:test",
        deadline_monotonic=monotonic() + 30,
    )
    # Same projection as the frontend: nonempty TEXT values joined by two LF.
    quote = "😀\n\n乙\r\n尾"
    content = FrozenPromptContent(
        (
            PromptAnnotationPart(
                quote, PromptAnnotationSource(source, 1, 9), "$pulsara-docs 解释"
            ),
        )
    )
    permission_id = identity("permission")
    permission = repo.prepare_root_permission_snapshot(
        guard,
        snapshot_id=permission_id,
        requested_mode=DEFAULT_PERMISSION_MODE,
        deadline_monotonic=monotonic() + 30,
    )

    def candidate(prompt):
        return build_prompt_ingress_command(
            session_id=guard.session_id,
            command_id=identity("command"),
            queue_item_id=identity("queue"),
            client_submission_id=identity("submission"),
            delivery_mode=PromptDeliveryMode.NEW_TURN
            if mode == "NEW_TURN"
            else PromptDeliveryMode.STEER_ACTIVE_TURN,
            target_turn_id=turn_id if mode == "STEER" else None,
            permission_snapshot_id=permission_id if mode == "NEW_TURN" else None,
            requested_permission_mode=DEFAULT_PERMISSION_MODE
            if mode == "NEW_TURN"
            else None,
            canonical_prompt=freeze_canonical_prompt(prompt),
        )

    def enqueue(value):
        return repo.enqueue_prompt(
            guard,
            candidate=value,
            model_resolution_snapshot=model_runtime().freeze_resolution_snapshot()
            if mode == "NEW_TURN"
            else None,
            occurred_at=datetime.now(timezone.utc),
            actor_id="user",
            deadline_monotonic=monotonic() + 30,
            _expected_permission_snapshot=permission if mode == "NEW_TURN" else None,
        )

    with pytest.raises(AnnotationSourceInvalid):
        enqueue(
            candidate(FrozenPromptContent((replace(content.parts[0], quote="不匹配"),)))
        )
    queued = candidate(content)
    enqueue(queued)
    with repo.connection_provider.connection(
        lane=PostgresConnectionLane.INSPECTOR,
        row_factory=dict_row,
        deadline_monotonic=monotonic() + 30,
    ) as connection:
        row = connection.execute(
            "SELECT * FROM pulsara_v3.prompt_queue_items WHERE id=%s",
            (queued.queue_item_id,),
        ).fetchone()
        restored = hydrate_canonical_prompt_owner(
            connection, row=row, queue_item_id=queued.queue_item_id
        ).content
        assert restored == content
