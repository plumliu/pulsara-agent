"""Fork copies canonical retained references instead of summary-owned text."""

from datetime import datetime, timezone
from io import BytesIO
from time import monotonic

from PIL import Image
from psycopg.rows import dict_row
import pytest

from pulsara_agent.identity import random_id
from pulsara_agent.conversation_kernel.compaction.contracts import (
    COMPACTION_MODEL_CONTRACT,
    COMPACTION_SNAPSHOT_COMPILER_CONTRACT,
    COMPACTION_SUMMARY_PROMPT_CONTRACT,
    CONTEXT_SNAPSHOT_CODEC,
    CONTEXT_SNAPSHOT_MEDIA_TYPE,
    CompactionCanonicalAdoptionFactoryInput,
    CompactionCanonicalWritePreconditions,
    CompactionContinuationMode,
    CompactionTargetBranch,
    ExpectedCompactionPredecessorRevision,
    build_prepared_compaction_canonical_adoption,
    canonical_compaction_range_digest,
    freeze_compaction_canonical_range,
)
from pulsara_agent.conversation_kernel.compaction.prompt import (
    build_compaction_snapshot_carrier,
    freeze_compaction_summary_output,
)
from pulsara_agent.conversation_kernel.contracts import InlineContent
from pulsara_agent.conversation_kernel.prompt_storage import (
    hydrate_canonical_snapshot_owner,
)
from pulsara_agent.conversation_kernel.reader import CanonicalProviderInputReader
from pulsara_agent.conversation_kernel.repository import (
    AssistantTextBlock,
    ConversationKernelRepository,
)
from pulsara_agent.llm.input import FrozenPromptContent, LLMImagePart, LLMTextPart
from pulsara_agent.model_input.contracts import CompactionSnapshotCarrier
from pulsara_agent.primitives.permission import DEFAULT_PERMISSION_MODE
from pulsara_agent.storage.postgres_connection_provider import PostgresConnectionLane
from tests.support.images import bound_test_image
from tests.support.model_config import (
    start_test_root_turn,
    test_model_binding as model_binding,
    test_model_runtime as model_runtime,
)
from tests.support.postgres import verified_postgres_provider


@pytest.fixture
def repo(stage2_migrated_postgres_database):
    return ConversationKernelRepository(
        verified_postgres_provider(stage2_migrated_postgres_database.runtime_dsn)
    )


def _rows(repo, query, args=()):
    with repo.connection_provider.connection(
        lane=PostgresConnectionLane.INSPECTOR,
        row_factory=dict_row,
        deadline_monotonic=monotonic() + 30,
    ) as connection:
        return connection.execute(query, args).fetchall()


def _lease(repo, session_id=None):
    workspace = (
        random_id("workspace")
        if session_id is None
        else _rows(
            repo,
            "SELECT workspace_id FROM pulsara_v3.sessions WHERE id=%s",
            (session_id,),
        )[0]["workspace_id"]
    )
    return repo.acquire_host_writer(
        intent="NEW" if session_id is None else "EXISTING",
        session_id=session_id or random_id("session"),
        workspace_id=workspace,
        writer_owner_id=random_id("host"),
        lease_seconds=300,
        deadline_monotonic=monotonic() + 30,
    )


def _start(repo, guard, content):
    turn_id, entry_id = random_id("turn"), random_id("entry")
    start_test_root_turn(
        repo,
        guard,
        command_id=random_id("command"),
        turn_id=turn_id,
        permission_snapshot_id=random_id("permission"),
        requested_permission_mode=DEFAULT_PERMISSION_MODE,
        entry_id=entry_id,
        context_binding_revision_id=random_id("binding"),
        content=content,
        occurred_at=datetime.now(timezone.utc),
        deadline_monotonic=monotonic() + 30,
        model_call_binding=model_binding(model_runtime()),
    )
    return turn_id, entry_id


def _finish(repo, guard, turn_id, text="final"):
    cut = repo.prepare_provider_input_cut(
        guard, turn_id=turn_id, deadline_monotonic=monotonic() + 30
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
        complete_turn=True,
        occurred_at=datetime.now(timezone.utc),
        actor_id="model:test",
        deadline_monotonic=monotonic() + 30,
    )
    return entry_id


def _compact(repo, guard, turn_id, boundary, retained):
    read = CanonicalProviderInputReader(
        repo.connection_provider
    ).read_frozen_compaction_cut(
        repo.prepare_compaction_input_cut(
            guard,
            turn_id=turn_id,
            allow_terminal=False,
            deadline_monotonic=monotonic() + 30,
        ),
        deadline_monotonic=monotonic() + 30,
    )
    lineage = read.lineage_base
    canonical = read.dispatch_read.compile_snapshot.canonical_input
    initial = _rows(
        repo, "SELECT initial_entry_id FROM pulsara_v3.turns WHERE id=%s", (turn_id,)
    )[0]["initial_entry_id"]
    carrier = build_compaction_snapshot_carrier(
        summary=freeze_compaction_summary_output(
            "Earlier work is complete.", maximum_utf8_bytes=65536
        ),
        retained_entry_ids=retained,
        continuation_mode=CompactionContinuationMode.RESUME_ACTIVE_TURN,
        active_request_entry_id=initial,
    )
    scope_range = freeze_compaction_canonical_range(
        scope=read.scope,
        effective_materialization_lineage_floor=lineage.effective_materialization_lineage_floor,
        source_through_sequence=boundary,
        ordered_items=canonical.items,
        closures=canonical.closures,
        late_outcomes=canonical.late_outcomes,
    )
    candidate = build_prepared_compaction_canonical_adoption(
        CompactionCanonicalAdoptionFactoryInput(
            scope=read.scope,
            target_branch=CompactionTargetBranch.ACTIVE_INSTALLATION,
            expected_turn_status="RUNNING",
            predecessor=ExpectedCompactionPredecessorRevision(
                binding_revision_id=lineage.binding_revision_id,
                revision_ordinal=lineage.binding_revision_ordinal,
                base_kind="SNAPSHOT"
                if lineage.persisted_context_snapshot_id
                else "FULL_HISTORY",
                context_snapshot_id=lineage.persisted_context_snapshot_id,
                source_through_sequence=lineage.persisted_revision_genesis_marker,
            ),
            snapshot_id=random_id("snapshot"),
            binding_revision_id=random_id("binding"),
            event_id=random_id("event"),
            source_through_sequence=boundary,
            source_digest=canonical_compaction_range_digest(lineage, scope_range),
            snapshot_content=InlineContent.from_bytes(
                carrier.body,
                media_type=CONTEXT_SNAPSHOT_MEDIA_TYPE,
                codec=CONTEXT_SNAPSHOT_CODEC,
            ),
            snapshot_carrier=carrier,
            compiler_contract=COMPACTION_SNAPSHOT_COMPILER_CONTRACT,
            prompt_contract=COMPACTION_SUMMARY_PROMPT_CONTRACT,
            model_contract=COMPACTION_MODEL_CONTRACT,
            occurred_at=datetime.now(timezone.utc),
            actor_id="runtime:test",
        )
    )
    result = repo.adopt_context_snapshot(
        guard,
        candidate=candidate,
        preconditions=CompactionCanonicalWritePreconditions(
            scope=read.scope,
            expected_turn_status="RUNNING",
            expected_safe_head=read.safe_head_range.source_through_sequence,
            provider_safe=True,
        ),
        deadline_monotonic=monotonic() + 30,
    )
    assert result.kind.value == "FULL"


def _fork(repo, source, anchor):
    outcome = repo.fork_conversation(
        source_session_id=source,
        anchor_entry_id=anchor,
        child_session_id=random_id("session"),
        memory_domain_id="u_local",
        deadline_monotonic=monotonic() + 30,
    )
    assert outcome.created, outcome.public_code
    return outcome.child_session_id


def _snapshot(repo, session_id):
    with repo.connection_provider.connection(
        lane=PostgresConnectionLane.INSPECTOR,
        row_factory=dict_row,
        deadline_monotonic=monotonic() + 30,
    ) as connection:
        row = connection.execute(
            "SELECT * FROM pulsara_v3.context_snapshots WHERE session_id=%s",
            (session_id,),
        ).fetchone()
        return row, hydrate_canonical_snapshot_owner(
            connection, row=row, context_snapshot_id=row["id"]
        )


def test_fork_retained_prefix_is_local_dense_and_self_contained(repo):
    parent = _lease(repo).guard
    old_turn, discarded_request = _start(
        repo, parent, FrozenPromptContent.text("summarized request")
    )
    discarded_answer = _finish(repo, parent, old_turn, "summarized answer")
    payload = BytesIO()
    Image.new("RGB", (7, 5), (11, 23, 41)).save(payload, "PNG")
    image = bound_test_image(
        "image/png",
        payload.getvalue(),
        7,
        5,
        workspace_id=repo.read_session_workspace_id(
            parent, deadline_monotonic=monotonic() + 30
        ),
    )
    # Identical content in distinct requests must remain distinct canonical rows.
    content = FrozenPromptContent((LLMTextPart("keep exact"), image, image))
    earlier_turn, earlier_request = _start(repo, parent, content)
    discarded_middle = _finish(
        repo, parent, earlier_turn, "intermediate answer summarized"
    )
    active_turn, active_request = _start(repo, parent, content)
    _compact(repo, parent, active_turn, 5, (earlier_request, active_request))
    anchor = _finish(repo, parent, active_turn)
    child = _fork(repo, parent.session_id, anchor)
    entries = _rows(
        repo,
        "SELECT * FROM pulsara_v3.transcript_entries WHERE session_id=%s ORDER BY entry_sequence",
        (child,),
    )
    assert [row["entry_sequence"] for row in entries] == [1, 2, 3]
    assert [row["entry_kind"] for row in entries] == [
        "USER_MESSAGE",
        "USER_MESSAGE",
        "ASSISTANT_MESSAGE",
    ]
    assert all(row["entry_owner_kind"] == "IMPORTED_HISTORY" for row in entries)
    assert all(row["turn_id"] is None for row in entries)
    assert {row["id"] for row in entries}.isdisjoint(
        {
            discarded_request,
            discarded_answer,
            discarded_middle,
            earlier_request,
            active_request,
            anchor,
        }
    )
    snapshot, carrier = _snapshot(repo, child)
    assert snapshot["source_through_sequence"] == 2
    assert carrier.retained_entry_ids == tuple(row["id"] for row in entries[:2])
    assert carrier.continuation_mode is CompactionContinuationMode.AWAIT_NEXT_USER
    assert carrier.active_request_entry_id is None
    assert not _rows(
        repo, "SELECT 1 FROM pulsara_v3.turns WHERE session_id=%s", (child,)
    )
    assert not _rows(
        repo,
        "SELECT 1 FROM information_schema.columns WHERE table_schema='pulsara_v3' AND table_name='canonical_image_refs' AND column_name='context_snapshot_id'",
    )
    refs = _rows(
        repo,
        "SELECT transcript_entry_id, ref_ordinal, blob_id FROM pulsara_v3.canonical_image_refs WHERE session_id=%s ORDER BY transcript_entry_id, ref_ordinal",
        (child,),
    )
    assert len(refs) == 4
    assert {row["transcript_entry_id"] for row in refs} == set(
        carrier.retained_entry_ids
    )
    grandchild = _fork(repo, child, entries[-1]["id"])
    grandchild_entries = _rows(
        repo,
        "SELECT * FROM pulsara_v3.transcript_entries WHERE session_id=%s ORDER BY entry_sequence",
        (grandchild,),
    )
    grandchild_snapshot, grandchild_carrier = _snapshot(repo, grandchild)
    assert len(grandchild_entries) == 3
    assert grandchild_snapshot["source_through_sequence"] == 2
    assert grandchild_carrier.retained_entry_ids == tuple(
        row["id"] for row in grandchild_entries[:2]
    )
    assert set(grandchild_carrier.retained_entry_ids).isdisjoint(
        carrier.retained_entry_ids
    )
    assert (
        repo.delete_session(
            required_lifecycle=None,
            session_id=parent.session_id,
            memory_domain_id="u_local",
            closed_writer=parent,
            deadline_monotonic=monotonic() + 30,
        )
        == "DELETED"
    )
    child_guard = _lease(repo, child).guard
    next_turn, next_request = _start(
        repo, child_guard, FrozenPromptContent.text("continue")
    )
    cut = repo.prepare_provider_input_cut(
        child_guard, turn_id=next_turn, deadline_monotonic=monotonic() + 30
    )
    read = CanonicalProviderInputReader(repo.connection_provider).read_frozen_snapshot(
        cut, deadline_monotonic=monotonic() + 30
    )
    assert isinstance(read.items[0].content, CompactionSnapshotCarrier)
    assert [item.source_entry_id for item in read.items[1:]] == [
        *carrier.retained_entry_ids,
        entries[-1]["id"],
        next_request,
    ]
    for item in read.items[1:3]:
        assert tuple(
            part for part in item.content if isinstance(part, LLMImagePart)
        ) == (image, image)
    assert (
        len(
            _rows(
                repo,
                "SELECT 1 FROM pulsara_v3.transcript_entries WHERE session_id=%s",
                (child,),
            )
        )
        == 4
    )


@pytest.mark.parametrize(
    "invalid",
    [
        "missing",
        "other_session",
        "after_cut",
        "assistant",
        "unsorted",
        "active_missing",
    ],
)
def test_fork_rejects_invalid_prefix_references_without_creating_child(
    repo, monkeypatch, invalid
):
    from pulsara_agent.conversation_kernel import fork_history

    guard = _lease(repo).guard
    old_turn, earlier = _start(repo, guard, FrozenPromptContent.text("earlier"))
    old_answer = _finish(repo, guard, old_turn)
    active_turn, active = _start(repo, guard, FrozenPromptContent.text("active"))
    _compact(repo, guard, active_turn, 3, (earlier, active))
    anchor = _finish(repo, guard, active_turn)
    other = _lease(repo).guard
    _, foreign = _start(repo, other, FrozenPromptContent.text("foreign"))
    refs = {
        "missing": (random_id("entry"),),
        "other_session": (foreign,),
        "after_cut": (anchor,),
        "assistant": (old_answer,),
        "unsorted": (active, earlier),
        "active_missing": (earlier,),
    }[invalid]
    forged = build_compaction_snapshot_carrier(
        summary=freeze_compaction_summary_output("summary", maximum_utf8_bytes=65536),
        retained_entry_ids=refs,
        continuation_mode=(
            CompactionContinuationMode.RESUME_ACTIVE_TURN
            if invalid == "active_missing"
            else CompactionContinuationMode.AWAIT_NEXT_USER
        ),
        active_request_entry_id=active if invalid == "active_missing" else None,
    )
    monkeypatch.setattr(
        fork_history, "hydrate_canonical_snapshot_owner", lambda *args, **kwargs: forged
    )
    child = random_id("session")
    outcome = repo.fork_conversation(
        source_session_id=guard.session_id,
        anchor_entry_id=anchor,
        child_session_id=child,
        memory_domain_id="u_local",
        deadline_monotonic=monotonic() + 30,
    )
    assert not outcome.created
    assert outcome.public_code.startswith("FORK_NOT_CREATED:")
    assert not _rows(repo, "SELECT 1 FROM pulsara_v3.sessions WHERE id=%s", (child,))
