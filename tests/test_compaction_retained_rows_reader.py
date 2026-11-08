"""Canonical compaction references read original rows in the same frozen cut."""

from datetime import datetime, timezone
from io import BytesIO
from time import monotonic

from PIL import Image
import pytest

from pulsara_agent.conversation_kernel.contracts import InlineContent
from pulsara_agent.conversation_kernel.reader import (
    CanonicalProviderInputReader,
    _read_retained_prefix_rows,
    _require_snapshot_active_request,
)
from pulsara_agent.conversation_kernel.repository import AssistantTextBlock
from pulsara_agent.conversation_kernel.repository_errors import (
    ConversationKernelConflict,
)
from pulsara_agent.identity import random_id
from pulsara_agent.llm.input import FrozenPromptContent, LLMImagePart, LLMTextPart
from pulsara_agent.model_input.contracts import (
    FrozenProviderInputItemKind,
)
from tests.support.images import bound_test_image
from tests.test_compaction_refs_fork import (
    _compact,
    _lease,
    _rows,
    _start,
    _finish,
)


@pytest.fixture
def repo(stage2_migrated_postgres_database):
    from pulsara_agent.conversation_kernel.repository import (
        ConversationKernelRepository,
    )
    from tests.support.postgres import verified_postgres_provider

    return ConversationKernelRepository(
        verified_postgres_provider(stage2_migrated_postgres_database.runtime_dsn)
    )


def _answer(repo, guard, turn, text):
    cut = repo.prepare_provider_input_cut(
        guard, turn_id=turn, deadline_monotonic=monotonic() + 30
    )
    entry_id = random_id("entry")
    repo.commit_assistant_message(
        guard,
        cut=cut,
        entry_id=entry_id,
        parent_content=InlineContent.from_bytes(b"manifest"),
        blocks=(
            AssistantTextBlock(
                random_id("block"), InlineContent.from_bytes(text.encode())
            ),
        ),
        complete_turn=False,
        occurred_at=datetime.now(timezone.utc),
        actor_id="model:test",
        deadline_monotonic=monotonic() + 30,
    )
    return entry_id


@pytest.mark.postgres
def test_reader_keeps_sparse_prefix_images_and_effective_q_across_compactions(repo):
    guard = _lease(repo).guard
    omitted_turn, omitted_request = _start(
        repo, guard, FrozenPromptContent.text("summarize")
    )
    omitted_answer = _finish(repo, guard, omitted_turn, "done")
    image_body = BytesIO()
    Image.new("RGB", (8, 6), (43, 17, 5)).save(image_body, "PNG")
    image = bound_test_image(
        "image/png",
        image_body.getvalue(),
        8,
        6,
        workspace_id=repo.read_session_workspace_id(
            guard, deadline_monotonic=monotonic() + 30
        ),
    )
    content = FrozenPromptContent(
        (LLMTextPart("preserve this exact request " * 150), image)
    )
    prior_turn, prior_request = _start(repo, guard, content)
    _finish(repo, guard, prior_turn, "prior work")
    current_turn, active_request = _start(repo, guard, content)
    before = _rows(
        repo,
        "SELECT count(*) AS n FROM pulsara_v3.transcript_entries WHERE session_id=%s",
        (guard.session_id,),
    )[0]["n"]
    _compact(repo, guard, current_turn, 5, (prior_request, active_request))
    suffix = _answer(repo, guard, current_turn, "continuing")
    reader = CanonicalProviderInputReader(repo.connection_provider)
    cut = repo.prepare_compaction_input_cut(
        guard,
        turn_id=current_turn,
        allow_terminal=False,
        deadline_monotonic=monotonic() + 30,
    )
    dispatch = reader.read_frozen_dispatch(cut, deadline_monotonic=monotonic() + 30)
    canonical = dispatch.compile_snapshot.canonical_input
    assert [item.source_entry_id for item in canonical.items] == [
        None,
        prior_request,
        active_request,
        suffix,
    ]
    assert [item.source_entry_sequence for item in canonical.items] == [5, 3, 5, 6]
    assert [item.item_kind for item in canonical.items] == [
        FrozenProviderInputItemKind.CONTEXT_SNAPSHOT,
        FrozenProviderInputItemKind.USER,
        FrozenProviderInputItemKind.USER,
        FrozenProviderInputItemKind.ASSISTANT,
    ]
    for item in canonical.items[1:3]:
        assert item.content == content.parts
        assert isinstance(item.content[1], LLMImagePart)
    assert omitted_request not in {item.source_entry_id for item in canonical.items}
    assert omitted_answer not in {item.source_entry_id for item in canonical.items}
    assert canonical.canonical_expanded_bytes > len(
        canonical.items[0].content.body
    ) + 2 * len(image.immutable_bytes)
    preflight = reader.read_compaction_headroom_preflight(
        cut, deadline_monotonic=monotonic() + 30
    )
    assert preflight.selected_item_count >= 4
    assert (
        preflight.selected_canonical_expanded_bytes
        >= canonical.canonical_expanded_bytes
    )
    frozen = reader.read_frozen_compaction_cut(cut, deadline_monotonic=monotonic() + 30)
    assert [item.source_entry_id for item in frozen.safe_head_range.ordered_items] == [
        prior_request,
        active_request,
        suffix,
    ]
    assert frozen.safe_head_range.retained_entry_ids == (prior_request, active_request)
    assert frozen.lineage_base.effective_materialization_lineage_floor == 5
    # A second cut retains the same original rows without expanding summarized H.
    _compact(repo, guard, current_turn, 6, (prior_request, active_request))
    second_cut = repo.prepare_compaction_input_cut(
        guard,
        turn_id=current_turn,
        allow_terminal=False,
        deadline_monotonic=monotonic() + 30,
    )
    second = reader.read_frozen_compaction_cut(
        second_cut, deadline_monotonic=monotonic() + 30
    )
    assert [
        item.source_entry_id
        for item in second.dispatch_read.compile_snapshot.canonical_input.items
    ] == [None, prior_request, active_request]
    assert second.safe_head_range.retained_entry_ids == (prior_request, active_request)
    assert second.lineage_base.effective_materialization_lineage_floor == 6
    assert (
        _rows(
            repo,
            "SELECT count(*) AS n FROM pulsara_v3.transcript_entries WHERE session_id=%s",
            (guard.session_id,),
        )[0]["n"]
        == before + 1
    )
    assert not _rows(
        repo,
        "SELECT 1 FROM information_schema.columns WHERE table_schema='pulsara_v3' "
        "AND table_name='canonical_image_refs' AND column_name='context_snapshot_id'",
    )


class _RowsConnection:
    def __init__(self, rows):
        self.rows = rows
        self.calls = []

    def execute(self, query, params):
        self.calls.append((query, params))
        return self

    def fetchall(self):
        return self.rows


def _original_row(**overrides):
    row = dict(
        id=random_id("entry"),
        session_id=random_id("session"),
        workspace_id=random_id("workspace"),
        entry_sequence=2,
        entry_kind="USER_MESSAGE",
        conversation_scope_kind="ROOT",
        input_origin="HUMAN_MESSAGE",
        scheduled_input=None,
        owning_turn_status="RUNNING",
    )
    row.update(overrides)
    return row


def _read_prefix(row, *, rows=None, ids=None, **kwargs):
    connection = _RowsConnection([row] if rows is None else rows)
    result = _read_retained_prefix_rows(
        connection,
        session_id=kwargs.pop("session_id", row["session_id"]),
        workspace_id=kwargs.pop("workspace_id", row["workspace_id"]),
        retained_entry_ids=(row["id"],) if ids is None else ids,
        source_through_sequence=kwargs.pop("source_through_sequence", 4),
        provider_input_through_sequence=kwargs.pop(
            "provider_input_through_sequence", 6
        ),
        scope_kind="ROOT",
        **kwargs,
    )
    return result, connection


def test_prefix_exact_batch_uses_identity_and_preserves_distinct_equal_content():
    first = _original_row()
    second = {**first, "id": random_id("entry"), "entry_sequence": 3}
    rows, connection = _read_prefix(
        first, rows=[first, second], ids=(first["id"], second["id"])
    )
    assert rows == (first, second)
    assert len(connection.calls) == 1
    assert connection.calls[0][1] == (first["session_id"], [first["id"], second["id"]])


@pytest.mark.parametrize(
    "changes",
    [
        {"entry_sequence": 0},
        {"entry_sequence": 5},
        {"workspace_id": random_id("workspace")},
        {"session_id": random_id("session")},
        {"entry_kind": "ASSISTANT_MESSAGE"},
        {"entry_kind": "TOOL_RESULT"},
        {"entry_kind": "USER_MESSAGE", "input_origin": "HUMAN_STEER"},
        {"entry_kind": "USER_STEER", "input_origin": "HUMAN_MESSAGE"},
        {"owning_turn_status": None},
        {"conversation_scope_kind": "SUBAGENT"},
        {"input_origin": "BAD_ORIGIN"},
        {"entry_kind": "TERMINAL_OBSERVATION", "input_origin": "HUMAN_MESSAGE"},
        {"entry_kind": "PLAN_CONTINUATION", "input_origin": "HUMAN_MESSAGE"},
    ],
)
def test_prefix_rejects_invalid_canonical_sources(changes):
    row = _original_row()
    with pytest.raises(ConversationKernelConflict):
        _read_prefix(row, rows=[{**row, **changes}])


def test_prefix_rejects_missing_duplicate_out_of_order_and_invalid_ids():
    row = _original_row()
    second = {**row, "id": random_id("entry"), "entry_sequence": 3}
    for rows, ids in [
        ([], (row["id"],)),
        ([row], (row["id"], row["id"])),
        ([second, row], (row["id"], second["id"])),
        ([row], (random_id("turn"),)),
    ]:
        with pytest.raises(ConversationKernelConflict):
            _read_prefix(row, rows=rows, ids=ids)


def test_prefix_never_moves_floor_to_recover_missing_active_request():
    from types import SimpleNamespace

    row = _original_row()
    with pytest.raises(ConversationKernelConflict, match="active request is absent"):
        _require_snapshot_active_request(
            SimpleNamespace(active_request_entry_id=row["id"]),
            entries=(),
            scope_kind="ROOT",
        )
    with pytest.raises(ConversationKernelConflict, match="floor exceeds"):
        _read_prefix(row, source_through_sequence=7)


def test_snapshot_hydrates_only_body_and_exact_joins_its_owner():
    from pulsara_agent.conversation_kernel.compaction.contracts import (
        CONTEXT_SNAPSHOT_MEDIA_TYPE,
        CONTEXT_SNAPSHOT_CODEC,
        CompactionContinuationMode,
    )
    from pulsara_agent.conversation_kernel.compaction.prompt import (
        build_compaction_snapshot_carrier,
        freeze_compaction_summary_output,
    )
    from pulsara_agent.conversation_kernel.prompt_storage import (
        hydrate_canonical_snapshot_owner,
    )

    carrier = build_compaction_snapshot_carrier(
        summary=freeze_compaction_summary_output(
            "Earlier history.", maximum_utf8_bytes=100
        ),
        retained_entry_ids=(),
        continuation_mode=CompactionContinuationMode.AWAIT_NEXT_USER,
        active_request_entry_id=None,
    )
    row = dict(
        id=random_id("snapshot"),
        workspace_id=random_id("workspace"),
        session_id=random_id("session"),
        inline_content=carrier.body,
        blob_id=None,
        content_digest=carrier.content_digest,
        content_size=len(carrier.body),
        content_media_type=CONTEXT_SNAPSHOT_MEDIA_TYPE,
        content_codec=CONTEXT_SNAPSHOT_CODEC,
    )
    connection = _RowsConnection([])
    assert (
        hydrate_canonical_snapshot_owner(
            connection, row=row, context_snapshot_id=row["id"]
        )
        == carrier
    )
    assert connection.calls == []
    with pytest.raises(ConversationKernelConflict, match="owner does not exact-join"):
        hydrate_canonical_snapshot_owner(
            connection, row=row, context_snapshot_id=random_id("snapshot")
        )


@pytest.mark.parametrize(
    "kind,origin",
    [
        ("PLAN_CONTINUATION", None),
        ("TERMINAL_OBSERVATION", "CELL_OUTPUT"),
        ("USER_STEER", "HUMAN_STEER"),
        ("USER_MESSAGE", "AGENT_INSTRUCTION"),
    ],
)
def test_prefix_permits_existing_request_kind_origin_union(kind, origin):
    row = _original_row(entry_kind=kind, input_origin=origin)
    assert _read_prefix(row)[0] == (row,)
