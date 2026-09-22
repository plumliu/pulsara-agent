from __future__ import annotations

from datetime import datetime, timezone
from io import BytesIO
from time import monotonic
from types import SimpleNamespace
from typing import cast
from uuid import uuid4

from PIL import Image
import psycopg
from psycopg import sql
from psycopg.rows import dict_row
import pytest

from pulsara_agent.conversation_kernel.contracts import InlineContent
from pulsara_agent.conversation_kernel.fork_history import read_fork_anchor
from pulsara_agent.conversation_kernel.memory.recall import PostgresMemoryQuery
from pulsara_agent.conversation_kernel.memory.management import (
    MemoryManagementSelection,
)
from pulsara_agent.conversation_kernel.prompt_content import (
    hydrate_canonical_prompt_body,
)
from pulsara_agent.conversation_kernel.prompt_storage import (
    CanonicalImageReferenceUnavailable,
    PostgresCanonicalImageReferenceReadPort,
)
from pulsara_agent.conversation_kernel.reader import CanonicalProviderInputReader
from pulsara_agent.llm.input import (
    FrozenPromptContent,
    LLMImagePart,
    LLMTextPart,
    join_text_content,
)
from pulsara_agent.model_input.contracts import (
    CanonicalInputOriginKind,
    CompactionSnapshotCarrier,
    FrozenProviderInputItemKind,
    StructuredModelInputLimits,
    provider_input_item_text,
)
from pulsara_agent.model_input.lowering import lower_canonical_item
from pulsara_agent.conversation_kernel.repository import (
    AssistantTextBlock,
    ConversationKernelRepository,
)
from pulsara_agent.conversation_kernel.steer import PreparedActiveRootInputAdmission
from pulsara_agent.storage.postgres_connection_provider import PostgresConnectionLane
from pulsara_agent.primitives.permission import DEFAULT_PERMISSION_MODE
from pulsara_agent.memory.scope import (
    MemoryDomainContext,
    freeze_memory_read_context_binding,
    workspace_context_id,
)
from tests.support.model_config import (
    start_test_root_turn,
    test_model_binding as model_binding,
    test_model_runtime as model_runtime,
    build_test_provider_replay_target,
)
from tests.support.postgres import verified_postgres_provider
from pulsara_agent.conversation_kernel.compaction.contracts import (
    COMPACTION_MODEL_CONTRACT,
    COMPACTION_SNAPSHOT_COMPILER_CONTRACT,
    COMPACTION_SUMMARY_PROMPT_CONTRACT,
    CONTEXT_SNAPSHOT_CODEC,
    CONTEXT_SNAPSHOT_MEDIA_TYPE,
    CompactionActiveRequestLocation,
    CompactionCanonicalAdoptionFactoryInput,
    CompactionCanonicalWritePreconditions,
    CompactionContinuationMode,
    CompactionTargetBranch,
    ExpectedCompactionPredecessorRevision,
    FrozenCompactionActiveRequest,
    build_prepared_compaction_canonical_adoption,
    canonical_compaction_range_digest,
    freeze_compaction_canonical_range,
)
from pulsara_agent.conversation_kernel.compaction.prompt import (
    build_compaction_snapshot_carrier,
    freeze_compaction_summary_output,
    parse_compaction_snapshot_carrier,
)


def identity(prefix):
    if prefix == "workspace":
        return f"ctx:workspace/{uuid4().hex}"
    return f"{prefix}:{uuid4().hex}"


@pytest.fixture
def repo(stage2_migrated_postgres_database):
    return ConversationKernelRepository(
        verified_postgres_provider(stage2_migrated_postgres_database.runtime_dsn)
    )


def new_session(repo):
    return repo.acquire_host_writer(
        session_id=identity("session"),
        workspace_id=identity("workspace"),
        writer_owner_id=identity("host"),
        lease_seconds=300,
        deadline_monotonic=monotonic() + 30,
    )


def turn(repo, guard, text, *, finish=True, answer="answer", complete=True):
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
        content=FrozenPromptContent.text(text),
        occurred_at=datetime.now(timezone.utc),
        deadline_monotonic=monotonic() + 30,
        model_call_binding=model_binding(model_runtime()),
    )
    cut = repo.prepare_provider_input_cut(
        guard, turn_id=turn_id, deadline_monotonic=monotonic() + 30
    )
    if not finish:
        return turn_id, cut, None
    final_id = identity("entry")
    repo.commit_assistant_message(
        guard,
        cut=cut,
        entry_id=final_id,
        parent_content=InlineContent.from_bytes(b"manifest"),
        blocks=(
            AssistantTextBlock(
                identity("block"), InlineContent.from_bytes(answer.encode())
            ),
        ),
        complete_turn=complete,
        occurred_at=datetime.now(timezone.utc),
        actor_id="model:test",
        deadline_monotonic=monotonic() + 30,
    )
    return turn_id, cut, final_id


def rows(repo, query, args=()):
    with repo.connection_provider.connection(
        lane=PostgresConnectionLane.INSPECTOR,
        row_factory=dict_row,
        deadline_monotonic=monotonic() + 30,
    ) as conn:
        return conn.execute(query, args).fetchall()


def fork(repo, source, final, child=None):
    return repo.fork_conversation(
        source_session_id=source,
        anchor_entry_id=final,
        child_session_id=child or identity("session"),
        memory_domain_id="u_local",
        deadline_monotonic=monotonic() + 30,
    )


def final(repo, guard, turn_id, text="final"):
    cut = repo.prepare_provider_input_cut(
        guard, turn_id=turn_id, deadline_monotonic=monotonic() + 30
    )
    entry = identity("entry")
    repo.commit_assistant_message(
        guard,
        cut=cut,
        entry_id=entry,
        parent_content=InlineContent.from_bytes(b"manifest"),
        blocks=(
            AssistantTextBlock(
                identity("block"), InlineContent.from_bytes(text.encode())
            ),
        ),
        complete_turn=True,
        occurred_at=datetime.now(timezone.utc),
        actor_id="model:test",
        deadline_monotonic=monotonic() + 30,
    )
    return entry


def compact(
    repo,
    guard,
    turn_id,
    boundary,
    summary="historical summary",
    *,
    idle=False,
    retained_historical_requests=(),
):
    read = CanonicalProviderInputReader(
        repo.connection_provider
    ).read_frozen_compaction_cut(
        repo.prepare_compaction_input_cut(
            guard,
            turn_id=turn_id,
            allow_terminal=idle,
            deadline_monotonic=monotonic() + 30,
        ),
        deadline_monotonic=monotonic() + 30,
    )
    lineage = read.lineage_base
    canonical = read.dispatch_read.compile_snapshot.canonical_input
    initial = rows(
        repo,
        "SELECT e.* FROM pulsara_v3.turns t JOIN pulsara_v3.transcript_entries e ON e.id=t.initial_entry_id WHERE t.id=%s",
        (turn_id,),
    )[0]
    canonical_initial = tuple(
        item for item in canonical.items if item.source_entry_id == initial["id"]
    )
    active_content = None
    if initial["entry_sequence"] <= boundary:
        if len(canonical_initial) == 1 and isinstance(
            canonical_initial[0].content, tuple
        ):
            active_content = FrozenPromptContent(canonical_initial[0].content)
        else:
            active_content = hydrate_canonical_prompt_body(
                body=bytes(initial["inline_content"]), image_payloads=()
            ).content
    active = (
        None
        if idle
        else FrozenCompactionActiveRequest(
            entry_id=initial["id"],
            entry_sequence=initial["entry_sequence"],
            location=CompactionActiveRequestLocation.SNAPSHOT_EXACT
            if initial["entry_sequence"] <= boundary
            else CompactionActiveRequestLocation.CANONICAL_SUFFIX,
            item_kind=FrozenProviderInputItemKind.USER,
            input_origin=CanonicalInputOriginKind.HUMAN_MESSAGE,
            content=active_content,
        )
    )
    carrier = build_compaction_snapshot_carrier(
        summary=freeze_compaction_summary_output(summary, maximum_utf8_bytes=65536),
        recent_human_requests=(),
        continuation_mode=CompactionContinuationMode.AWAIT_NEXT_USER
        if idle
        else CompactionContinuationMode.RESUME_ACTIVE_TURN,
        active_request=active,
        retained_historical_requests=retained_historical_requests,
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
            target_branch=CompactionTargetBranch.IDLE_BASE_ONLY
            if idle
            else CompactionTargetBranch.ACTIVE_INSTALLATION,
            expected_turn_status="COMPLETED" if idle else "RUNNING",
            predecessor=ExpectedCompactionPredecessorRevision(
                binding_revision_id=lineage.binding_revision_id,
                revision_ordinal=lineage.binding_revision_ordinal,
                base_kind="SNAPSHOT" if lineage.snapshot_id else "FULL_HISTORY",
                context_snapshot_id=lineage.snapshot_id,
                source_through_sequence=lineage.persisted_revision_genesis_marker,
            ),
            snapshot_id=identity("snapshot"),
            binding_revision_id=identity("revision"),
            event_id=identity("event"),
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
            expected_turn_status=candidate.expected_turn_status,
            expected_safe_head=read.safe_head_range.source_through_sequence,
            provider_safe=True,
        ),
        deadline_monotonic=monotonic() + 30,
    )
    assert result.kind.value == "FULL"
    return carrier


def child_lease(repo, child):
    row = rows(
        repo, "SELECT workspace_id FROM pulsara_v3.sessions WHERE id=%s", (child,)
    )[0]
    return repo.acquire_host_writer(
        session_id=child,
        workspace_id=row["workspace_id"],
        writer_owner_id=identity("host"),
        lease_seconds=300,
        deadline_monotonic=monotonic() + 30,
    )


def test_historical_snapshot_ignores_future_idle_compaction_and_retains_exact_request(
    repo,
):
    lease = new_session(repo)
    turn(repo, lease.guard, "covered old text", answer="covered answer")
    running, _, _ = turn(repo, lease.guard, "precise active request", finish=False)
    compact(repo, lease.guard, running, 3, "C1 summary")
    anchor = final(repo, lease.guard, running, "C1 final")
    compact(repo, lease.guard, running, 4, "C2 future summary", idle=True)
    created = fork(repo, lease.guard.session_id, anchor)
    assert created.created, created.public_code
    child = created.child_session_id
    copied = rows(
        repo,
        "SELECT * FROM pulsara_v3.transcript_entries WHERE session_id=%s",
        (child,),
    )
    assert (
        len(copied) == 1
        and copied[0]["entry_sequence"] == 1
        and copied[0]["provider_input_through_sequence"] == 0
    )
    snapshot = rows(
        repo, "SELECT * FROM pulsara_v3.context_snapshots WHERE session_id=%s", (child,)
    )[0]
    carrier = parse_compaction_snapshot_carrier(bytes(snapshot["inline_content"]))
    assert carrier.earlier_context_summary == "C1 summary"
    assert (
        carrier.continuation_mode is CompactionContinuationMode.AWAIT_NEXT_USER
        and carrier.active_request is None
    )
    assert len(carrier.retained_historical_requests) == 1
    assert join_text_content(
        carrier.retained_historical_requests[0].content.parts
    ) == "precise active request"
    assert carrier.retained_historical_requests[0].item_kind.value == "USER"
    child_guard = child_lease(repo, child).guard
    _, cut, _ = turn(repo, child_guard, "new child request", finish=False)
    actual = CanonicalProviderInputReader(
        repo.connection_provider
    ).read_frozen_snapshot(cut, deadline_monotonic=monotonic() + 30)
    assert [provider_input_item_text(item) for item in actual.items[1:]] == [
        "C1 final",
        "new child request",
    ]
    snapshot_projection = lower_canonical_item(
        actual.items[0],
        artifact_read_available=False,
        limits=StructuredModelInputLimits(),
    )
    assert snapshot_projection.fixed_message is not None
    assert "C2 future summary" not in join_text_content(
        snapshot_projection.fixed_message.content
    )
    nested = fork(repo, child, copied[0]["id"])
    assert nested.created, nested.public_code
    nested_snapshot = rows(
        repo,
        "SELECT inline_content FROM pulsara_v3.context_snapshots WHERE session_id=%s",
        (nested.child_session_id,),
    )[0]
    assert bytes(nested_snapshot["inline_content"]) == carrier.body


def test_midturn_multiple_compactions_copy_only_last_adopted_base_and_retained_suffix(
    repo,
):
    lease = new_session(repo)
    turn_id, _, _ = turn(repo, lease.guard, "long horizon objective", finish=False)
    compact(repo, lease.guard, turn_id, 1, "C1")
    # A second adoption of the same effective cut still has a new historical binding.
    compact(repo, lease.guard, turn_id, 1, "C2")
    anchor = final(repo, lease.guard, turn_id)
    created = fork(repo, lease.guard.session_id, anchor)
    assert created.created, created.public_code
    carriers = rows(
        repo,
        "SELECT inline_content FROM pulsara_v3.context_snapshots WHERE session_id=%s",
        (created.child_session_id,),
    )
    assert len(carriers) == 1
    parsed = parse_compaction_snapshot_carrier(bytes(carriers[0]["inline_content"]))
    assert (
        parsed.earlier_context_summary == "C2"
        and join_text_content(
            parsed.retained_historical_requests[0].content.parts
        )
        == "long horizon objective"
    )


def test_fork_from_closed_source_or_while_later_turn_runs(repo):
    lease = new_session(repo)
    _, _, anchor = turn(repo, lease.guard, "past")
    turn(repo, lease.guard, "running future", finish=False)
    assert fork(repo, lease.guard.session_id, anchor).created
    repo.close_session(lease.guard, deadline_monotonic=monotonic() + 30)
    assert fork(repo, lease.guard.session_id, anchor).created


@pytest.mark.parametrize("second_request", ["new exact request", "first exact request"])
def test_repeated_fork_appends_exact_requests_without_overwrite_or_dedup(
    repo, second_request
):
    lease = new_session(repo)
    running, _, _ = turn(repo, lease.guard, "first exact request", finish=False)
    compact(repo, lease.guard, running, 1)
    parent_anchor = final(repo, lease.guard, running)
    child = fork(repo, lease.guard.session_id, parent_anchor)
    assert child.created, child.public_code
    child_guard = child_lease(repo, child.child_session_id).guard
    snapshot = rows(
        repo,
        "SELECT inline_content FROM pulsara_v3.context_snapshots WHERE session_id=%s",
        (child.child_session_id,),
    )[0]
    inherited = parse_compaction_snapshot_carrier(bytes(snapshot["inline_content"]))
    running, cut, _ = turn(repo, child_guard, second_request, finish=False)
    # Tier-3 destination projection can omit the predecessor base: its exact
    # typed requests remain separate from the new summary and active request.
    compact(
        repo,
        child_guard,
        running,
        cut.provider_input_through_sequence,
        "destination summary without predecessor base",
        retained_historical_requests=inherited.retained_historical_requests,
    )
    child_anchor = final(repo, child_guard, running)
    nested = fork(repo, child.child_session_id, child_anchor)
    assert nested.created, nested.public_code
    snapshot = rows(
        repo,
        "SELECT inline_content FROM pulsara_v3.context_snapshots WHERE session_id=%s",
        (nested.child_session_id,),
    )[0]
    settled = parse_compaction_snapshot_carrier(bytes(snapshot["inline_content"]))
    assert [
        join_text_content(request.content.parts)
        for request in settled.retained_historical_requests
    ] == [
        "first exact request",
        second_request,
    ]
    assert [
        request.input_origin.value for request in settled.retained_historical_requests
    ] == ["HUMAN_MESSAGE", "HUMAN_MESSAGE"]
    assert settled.active_request is None
    assert settled.continuation_mode is CompactionContinuationMode.AWAIT_NEXT_USER
    anchor = rows(
        repo,
        "SELECT anchor_entry_id FROM pulsara_v3.session_context_genesis WHERE session_id=%s",
        (nested.child_session_id,),
    )[0]["anchor_entry_id"]
    repeated = fork(repo, nested.child_session_id, anchor)
    assert repeated.created, repeated.public_code
    copied = rows(
        repo,
        "SELECT inline_content FROM pulsara_v3.context_snapshots WHERE session_id=%s",
        (repeated.child_session_id,),
    )[0]
    assert bytes(copied["inline_content"]) == settled.body


def test_fork_atomic_rollback_and_sealed_prefix(repo, monkeypatch):
    import pulsara_agent.conversation_kernel._repository.fork as writer
    import psycopg

    lease = new_session(repo)
    _, _, anchor = turn(repo, lease.guard, "original")
    original = writer._insert

    def fail(connection, table, values):
        if table == "session_context_genesis":
            raise RuntimeError("injected before commit")
        return original(connection, table, values)

    with monkeypatch.context() as local:
        local.setattr(writer, "_insert", fail)
        creation = fork(repo, lease.guard.session_id, anchor)
    assert not creation.created
    assert (
        rows(
            repo,
            "SELECT 1 FROM pulsara_v3.sessions WHERE id=%s",
            (creation.child_session_id,),
        )
        == []
    )
    created = fork(repo, lease.guard.session_id, anchor)
    assert created.created
    child = created.child_session_id
    turn(repo, child_lease(repo, child).guard, "local execution")
    copied = rows(
        repo,
        "SELECT * FROM pulsara_v3.transcript_entries WHERE session_id=%s AND entry_sequence=1",
        (child,),
    )[0]
    copied.update(id=identity("entry"), entry_sequence=5)
    with pytest.raises(psycopg.errors.CheckViolation):
        with repo.connection_provider.connection(
            lane=PostgresConnectionLane.HOST_CONTROL,
            row_factory=dict_row,
            deadline_monotonic=monotonic() + 30,
        ) as connection:
            original(connection, "transcript_entries", copied)


@pytest.mark.parametrize("late", [False, True])
def test_fork_tool_result_artifact_and_late_closure_are_history_only(repo, late):
    from tests.test_round1_tool_output_artifact import _install_tool_call
    from pulsara_agent.conversation_kernel.tool_artifacts import (
        ToolOutputArtifactProcessor,
        PostgresToolArtifactReadPort,
    )
    from pulsara_agent.conversation_kernel.repository import (
        build_prepared_tool_result_acceptance,
        ConversationKernelConflict,
    )
    from pulsara_agent.primitives.tool_observation import ToolObservationOrigin
    from pulsara_agent.conversation_kernel.blob import PostgresCanonicalBlobStore

    workspace = identity("workspace")
    lease, old_turn, request, call, attempt = _install_tool_call(repo, workspace)
    if late:
        repo.interrupt_turn(
            lease.guard,
            turn_id=old_turn,
            reason="HOST_CRASH",
            occurred_at=datetime.now(timezone.utc),
            actor_id="runtime",
            deadline_monotonic=monotonic() + 30,
        )
        turn(repo, lease.guard, "bridge before late result")
    result_id = identity("entry")
    source = "large historical artifact\n" * 4000
    projection = ToolOutputArtifactProcessor(repo.connection_provider).prepare(
        workspace_id=workspace,
        result_entry_id=result_id,
        public_output=source,
        candidate=None,
        artifact_source_read=False,
        deadline_monotonic=monotonic() + 30,
    )
    candidate = build_prepared_tool_result_acceptance(
        guard=lease.guard,
        workspace_id=workspace,
        result_id=identity("result"),
        result_entry_id=result_id,
        turn_id=old_turn,
        assistant_entry_id=request,
        tool_call_id=call,
        attempt_id=attempt,
        result_state="SUCCESS",
        canonical_preview_content=projection.canonical_preview,
        artifact_disposition=projection.artifact_disposition,
        artifact_id=projection.artifact_id,
        artifact_blob_descriptor=projection.artifact_blob,
        source_coverage=projection.source_coverage,
        display_kind=projection.display_kind,
        source_coverage_reason=projection.source_coverage_reason,
        artifact_unavailability_reason=projection.artifact_unavailability_reason,
        actor_id="terminal",
        observed_at=datetime.now(timezone.utc),
        observation_duration_microseconds=None,
        observation_origin_kind=ToolObservationOrigin.TERMINAL_PROCESS,
        trusted_tool_reported_duration_microseconds=None,
    )
    repo.accept_tool_result(
        lease.guard, candidate=candidate, deadline_monotonic=monotonic() + 30
    )
    anchor = (
        turn(repo, lease.guard, "after late result")[2]
        if late
        else final(repo, lease.guard, old_turn)
    )
    created = fork(repo, lease.guard.session_id, anchor)
    assert created.created, created.public_code
    child = created.child_session_id
    imported = rows(
        repo, "SELECT * FROM pulsara_v3.tool_results WHERE session_id=%s", (child,)
    )[0]
    assert (
        imported["result_record_kind"] == "IMPORTED_HISTORY"
        and imported["result_origin_kind"] == "PHYSICAL_ATTEMPT"
    )
    assert (
        imported["attempt_id"] is None
        and imported["permission_snapshot_fingerprint"] is None
    )
    assert (
        imported["output_artifact_id"] == projection.artifact_id
        and imported["output_artifact_blob_id"] == projection.artifact_blob.blob_id
    )
    closure = rows(
        repo,
        "SELECT * FROM pulsara_v3.imported_tool_call_closures WHERE session_id=%s",
        (child,),
    )
    assert len(closure) == int(late)
    if late:
        assert closure[0]["closure_kind"] == "INTERRUPTED_MAY_HAVE_PARTIALLY_EXECUTED"
    guard = child_lease(repo, child).guard
    _, cut, _ = turn(repo, guard, "continue imported tools", finish=False)
    snapshot = CanonicalProviderInputReader(
        repo.connection_provider
    ).read_frozen_snapshot(cut, deadline_monotonic=monotonic() + 30)
    assert len(snapshot.late_outcomes) == int(late) and len(snapshot.closures) == int(
        late
    )
    assert (
        rows(
            repo,
            "SELECT 1 FROM pulsara_v3.tool_execution_attempts WHERE session_id=%s",
            (child,),
        )
        == []
    )
    with pytest.raises(ConversationKernelConflict, match="not active"):
        repo.accept_tool_attempt(
            guard,
            attempt_id=identity("attempt"),
            assistant_entry_id=imported["tool_call_entry_id"],
            tool_call_id=call,
            authorization_kind="policy",
            authorization_reference="allow",
            actor_kind="runtime",
            actor_id="test",
            remote_idempotency_key=None,
            retry_of_attempt_id=None,
            permission_snapshot_fingerprint="sha256:" + "a" * 64,
            occurred_at=datetime.now(timezone.utc),
            deadline_monotonic=monotonic() + 30,
        )
    repo.close_session(lease.guard, deadline_monotonic=monotonic() + 30)
    PostgresCanonicalBlobStore(repo.connection_provider).delete_orphans(
        grace_seconds=1, maximum_items=100, deadline_monotonic=monotonic() + 30
    )
    view = PostgresToolArtifactReadPort(
        repo.connection_provider, session_id=child, workspace_id=workspace
    ).read_text(projection.artifact_id, offset_chars=0, max_chars=1000)
    assert view.text == source[:1000]
    nested = fork(
        repo,
        child,
        rows(
            repo,
            "SELECT anchor_entry_id FROM pulsara_v3.session_context_genesis WHERE session_id=%s",
            (child,),
        )[0]["anchor_entry_id"],
    )
    assert nested.created, nested.public_code


@pytest.mark.parametrize("wire_api", ["openai_chat_completions", "openai_responses"])
def test_fork_native_replay_rebinds_local_metadata_preserving_opaque_payload(
    repo, wire_api
):
    from pulsara_agent.llm.provider_replay import (
        build_prepared_durable_provider_assistant_replay,
    )
    from pulsara_agent.llm.request import (
        provider_assistant_public_projection_fingerprint,
    )
    from pulsara_agent.primitives.context import freeze_json

    lease = new_session(repo)
    turn_id, cut, _ = turn(repo, lease.guard, "question", finish=False)
    entry = identity("entry")
    target = build_test_provider_replay_target(
        wire_api=wire_api,
        model_id="model",
        transport_binding_id="test-wire",
    )
    payload = (
        (
            {
                "role": "assistant",
                "content": "answer",
                "reasoning_content": "visible thought",
            },
        )
        if wire_api == "openai_chat_completions"
        else (
            {
                "id": "opaque-reasoning",
                "type": "reasoning",
                "summary": [{"type": "summary_text", "text": "visible thought"}],
                "encrypted_content": "opaque-signed-data",
            },
            {
                "id": "opaque-message",
                "type": "message",
                "role": "assistant",
                "status": "completed",
                "content": [
                    {"type": "output_text", "text": "answer", "annotations": []}
                ],
            },
        )
    )
    replay = build_prepared_durable_provider_assistant_replay(
        session_id=lease.guard.session_id,
        workspace_id=repo.read_session_workspace_id(
            lease.guard, deadline_monotonic=monotonic() + 30
        ),
        assistant_entry_id=entry,
        target=target,
        public_projection_fingerprint=provider_assistant_public_projection_fingerprint(
            text="answer", tool_calls=()
        ),
        ordered_items=tuple(freeze_json(item) for item in payload),
    )
    repo.commit_assistant_message(
        lease.guard,
        cut=cut,
        entry_id=entry,
        parent_content=InlineContent.from_bytes(b"manifest"),
        blocks=(
            AssistantTextBlock(identity("block"), InlineContent.from_bytes(b"answer")),
        ),
        complete_turn=True,
        provider_replay=replay,
        occurred_at=datetime.now(timezone.utc),
        actor_id="model:test",
        deadline_monotonic=monotonic() + 30,
    )
    created = fork(repo, lease.guard.session_id, entry)
    assert created.created, created.public_code
    copied = rows(
        repo,
        "SELECT * FROM pulsara_v3.provider_assistant_replay_fragments WHERE session_id=%s",
        (created.child_session_id,),
    )[0]
    assert bytes(copied["payload_bytes"]) == replay.payload_bytes
    assert copied["id"] != replay.replay_id and copied["assistant_entry_id"] != entry
    assert copied["fragment_fingerprint"] != replay.fragment_fingerprint
    assert (
        copied["public_projection_fingerprint"] == replay.public_projection_fingerprint
    )
    _, next_cut, _ = turn(
        repo, child_lease(repo, created.child_session_id).guard, "next", finish=False
    )
    read = CanonicalProviderInputReader(repo.connection_provider).read_frozen_dispatch(
        next_cut, deadline_monotonic=monotonic() + 30
    )
    assert len(read.replay_manifest_cut.manifests) == 1


def test_fork_paged_repeatable_read_cannot_mix_in_concurrent_source_appends(
    repo, monkeypatch
):
    lease = new_session(repo)
    for index in range(130):
        _, _, anchor = turn(repo, lease.guard, f"history {index}")
    original = CanonicalProviderInputReader._read_content
    changed = False

    def append_during_read(reader, row, **kwargs):
        nonlocal changed
        if not changed:
            changed = True
            turn(repo, lease.guard, "CONCURRENT_FUTURE_ONLY")
        return original(reader, row, **kwargs)

    monkeypatch.setattr(
        CanonicalProviderInputReader, "_read_content", append_during_read
    )
    created = fork(repo, lease.guard.session_id, anchor)
    assert created.created, created.public_code
    copied = rows(
        repo,
        "SELECT entry_sequence,inline_content FROM pulsara_v3.transcript_entries WHERE session_id=%s ORDER BY entry_sequence",
        (created.child_session_id,),
    )
    assert [row["entry_sequence"] for row in copied] == list(range(1, 261))
    assert all(
        b"CONCURRENT_FUTURE_ONLY" not in bytes(row["inline_content"]) for row in copied
    )


def test_fork_commit_ack_loss_confirms_preselected_child_without_recopy(repo):
    from contextlib import contextmanager

    lease = new_session(repo)
    _, _, anchor = turn(repo, lease.guard, "ack source")
    original = repo.connection_provider

    class LostAck:
        fired = False

        @contextmanager
        def connection(self, **kwargs):
            with original.connection(**kwargs) as connection:
                yield connection
            if kwargs["lane"] == PostgresConnectionLane.HOST_CONTROL and not self.fired:
                self.fired = True
                raise ConnectionError("lost successful commit acknowledgement")

    repo._provider = LostAck()
    try:
        created = fork(repo, lease.guard.session_id, anchor)
        assert created.created, created.public_code
        assert (
            len(
                rows(
                    repo,
                    "SELECT * FROM pulsara_v3.session_context_genesis WHERE session_id=%s",
                    (created.child_session_id,),
                )
            )
            == 1
        )
        assert not fork(
            repo, lease.guard.session_id, anchor, child=created.child_session_id
        ).created
    finally:
        repo._provider = original


def test_protocol_projects_only_anchor_eligibility_and_genesis_without_occurrence(repo):
    from pulsara_agent.terminal_protocol.canonical_v3 import CanonicalProtocolReader

    lease = new_session(repo)
    _, _, old = turn(repo, lease.guard, "first")
    _, _, anchor = turn(repo, lease.guard, "second")
    created = fork(repo, lease.guard.session_id, anchor)
    assert created.created
    protocol = CanonicalProtocolReader(repo.connection_provider)
    with protocol._connection(monotonic() + 30) as connection:
        copied = connection.execute(
            "SELECT * FROM pulsara_v3.transcript_entries WHERE session_id=%s ORDER BY entry_sequence",
            (created.child_session_id,),
        ).fetchall()
        projected = protocol._entries(connection, copied)
        assert [row.fork_eligible for row in projected] == [False, False, False, True]
        assert all(row.entry_owner_kind == "IMPORTED_HISTORY" for row in projected)
        assert all(row.turn_id.startswith("history-group:") for row in projected)
        control = protocol._control(
            connection,
            session_id=created.child_session_id,
            lifecycle="OPEN",
            maximum_items=256,
            entry_sequence_floor=1,
        )
        assert control.initial_context_base.base_kind == "FULL_HISTORY"
        assert not control.HasField("latest_context_compaction")


@pytest.mark.parametrize(
    "created,open_failure,expected",
    [
        (False, False, "NOT_CREATED"),
        (True, False, "CREATED_AND_OPENED"),
        (True, True, "CREATED_OPEN_DEFERRED"),
    ],
)
def test_web_fork_outcomes_are_closed_and_open_is_post_commit(
    created, open_failure, expected, tmp_path
):
    import asyncio

    async def check():
        import asyncio
        from types import SimpleNamespace
        from unittest.mock import AsyncMock
        from pulsara_agent.web_app.session_controller import LocalSessionController
        from pulsara_agent.workspace_identity import HostWorkspaceInput
        from pulsara_agent.conversation_kernel._repository.fork import (
            CanonicalForkCreation,
        )

        child = identity("session")
        core = SimpleNamespace(
            mcp_management=SimpleNamespace(lane=asyncio.Lock()),
            fork_conversation=AsyncMock(
                return_value=CanonicalForkCreation(child, created, "test")
            ),
        )
        controller = LocalSessionController(
            core=core,
            workspace_input=HostWorkspaceInput(
                workspace_kind="project", workspace_root=tmp_path
            ),
            permission_policy=None,
            active_skill_names=frozenset(),
        )
        controller.resume_session = AsyncMock(
            side_effect=RuntimeError("workspace unavailable") if open_failure else None
        )
        result = await controller.fork_conversation(
            "parent", anchor_entry_id="anchor", child_session_id=child
        )
        assert result["outcome"] == expected and result["child_session_id"] == child
        assert controller.resume_session.await_count == int(created)

    asyncio.run(check())


def test_web_fork_disconnected_waiter_does_not_cancel_creation(tmp_path):
    import asyncio

    async def check():
        from types import SimpleNamespace
        from unittest.mock import AsyncMock
        from pulsara_agent.web_app.session_controller import LocalSessionController
        from pulsara_agent.workspace_identity import HostWorkspaceInput
        from pulsara_agent.conversation_kernel._repository.fork import (
            CanonicalForkCreation,
        )

        entered, release = asyncio.Event(), asyncio.Event()
        child = identity("session")

        async def create(**kwargs):
            entered.set()
            await release.wait()
            return CanonicalForkCreation(child, True, "created")

        core = SimpleNamespace(
            mcp_management=SimpleNamespace(lane=asyncio.Lock()),
            fork_conversation=create,
        )
        controller = LocalSessionController(
            core=core,
            workspace_input=HostWorkspaceInput(
                workspace_kind="project", workspace_root=tmp_path
            ),
            permission_policy=None,
            active_skill_names=frozenset(),
        )
        controller.resume_session = AsyncMock()
        waiter = asyncio.create_task(
            controller.fork_conversation(
                "parent", anchor_entry_id="anchor", child_session_id=child
            )
        )
        await entered.wait()
        owner = next(iter(controller._forks))
        waiter.cancel()
        with pytest.raises(asyncio.CancelledError):
            await waiter
        assert not owner.cancelled()
        release.set()
        assert (await owner)["outcome"] == "CREATED_AND_OPENED"
        controller.resume_session.assert_awaited_once_with(child)

    asyncio.run(check())


def test_fork_copies_exact_prefix_with_no_execution_and_continues(repo):
    lease = new_session(repo)
    _, _, anchor = turn(repo, lease.guard, "first prompt", answer="first final")
    turn(repo, lease.guard, "future secret", answer="future final")
    created = fork(repo, lease.guard.session_id, anchor)
    assert created.created, created.public_code
    child = created.child_session_id
    entries = rows(
        repo,
        "SELECT * FROM pulsara_v3.transcript_entries WHERE session_id=%s ORDER BY entry_sequence",
        (child,),
    )
    assert [r["entry_kind"] for r in entries] == ["USER_MESSAGE", "ASSISTANT_MESSAGE"]
    assert [r["entry_sequence"] for r in entries] == [1, 2]
    assert all(
        r["entry_owner_kind"] == "IMPORTED_HISTORY" and r["turn_id"] is None
        for r in entries
    )
    for table in (
        "turns",
        "agent_events",
        "session_commands",
        "tool_execution_attempts",
        "prompt_queue_items",
        "subagent_tasks",
        "plan_workflows",
    ):
        assert (
            rows(
                repo,
                sql.SQL("SELECT 1 FROM pulsara_v3.{} WHERE session_id=%s").format(
                    sql.Identifier(table)
                ),
                (child,),
            )
            == []
        )
    assert rows(
        repo,
        "SELECT 1 FROM pulsara_v3.memory_facts AS f "
        "JOIN pulsara_v3.tool_results AS r "
        "ON r.id=f.created_by_tool_result_id WHERE r.session_id=%s",
        (child,),
    ) == []
    source = rows(
        repo, "SELECT workspace_id FROM pulsara_v3.sessions WHERE id=%s", (child,)
    )[0]
    child_lease = repo.acquire_host_writer(
        session_id=child,
        workspace_id=source["workspace_id"],
        writer_owner_id=identity("host"),
        lease_seconds=300,
        deadline_monotonic=monotonic() + 30,
    )
    _, cut, _ = turn(repo, child_lease.guard, "continue child", finish=False)
    snapshot = CanonicalProviderInputReader(
        repo.connection_provider
    ).read_frozen_snapshot(cut, deadline_monotonic=monotonic() + 30)
    assert [provider_input_item_text(item) for item in snapshot.items] == [
        "first prompt",
        "first final",
        "continue child",
    ]


def test_fork_reuses_canonical_workspace_and_does_not_reown_shared_memory(
    repo, request: pytest.FixtureRequest
):
    from tests.test_direct_advisory_memory import _open_direct_memory_session

    workspace_root = f"/tmp/{identity('fork-memory-root')}"
    workspace_id = workspace_context_id(workspace_root)
    lease, _invoke, remember = _open_direct_memory_session(
        repo, workspace_root=workspace_root
    )
    _, saved, _ = remember(
        "The forked project uses the shared release checklist.", project=True
    )

    def cleanup_memory() -> None:
        selection = MemoryManagementSelection("project", workspace_id)
        preview = repo.memory_deletion_preview(
            memory_domain_id="u_local",
            selection=selection,
            fact_id=saved["memory_id"],
            deadline_monotonic=monotonic() + 30,
        )
        repo.execute_memory_deletion(
            memory_domain_id="u_local",
            selection=selection,
            fact_id=saved["memory_id"],
            additional=(),
            expected_records=lambda: iter(preview),
            deadline_monotonic=monotonic() + 30,
        )

    request.addfinalizer(cleanup_memory)
    active_turn = rows(
        repo,
        "SELECT id FROM pulsara_v3.turns WHERE session_id=%s "
        "ORDER BY accepted_at DESC LIMIT 1",
        (lease.guard.session_id,),
    )[0]["id"]
    anchor = final(repo, lease.guard, str(active_turn), "Memory saved.")
    created = fork(repo, lease.guard.session_id, anchor)
    assert created.created, created.public_code

    workspace_rows = rows(
        repo,
        "SELECT w.workspace_root,w.workspace_label,s.id AS session_id "
        "FROM pulsara_v3.workspaces w JOIN pulsara_v3.sessions s "
        "ON s.memory_domain_id=w.memory_domain_id AND s.workspace_id=w.id "
        "WHERE w.memory_domain_id='u_local' AND w.id=%s ORDER BY s.id",
        (workspace_id,),
    )
    assert {row["session_id"] for row in workspace_rows} == {
        lease.guard.session_id,
        created.child_session_id,
    }
    assert {
        (row["workspace_root"], row["workspace_label"])
        for row in workspace_rows
    } == {(workspace_root, workspace_root.rsplit("/", 1)[-1])}

    owners = rows(
        repo,
        "SELECT r.session_id FROM pulsara_v3.memory_facts f "
        "JOIN pulsara_v3.tool_results r ON r.id=f.created_by_tool_result_id "
        "WHERE f.id=%s",
        (saved["memory_id"],),
    )
    assert [row["session_id"] for row in owners] == [lease.guard.session_id]
    read_binding = freeze_memory_read_context_binding(
        domain=MemoryDomainContext(
            "u_local", "project", stable_project_key=workspace_root
        ),
        host_workspace_id=workspace_id,
    )
    memory_query = PostgresMemoryQuery(repo.connection_provider)
    assert memory_query.get(
        read_binding=read_binding,
        fact_id=saved["memory_id"],
        deadline_monotonic=monotonic() + 30,
    ) is not None
    provenance = memory_query.provenance(
        read_binding=read_binding,
        fact_id=saved["memory_id"],
        deadline_monotonic=monotonic() + 30,
    )
    assert provenance is not None
    assert provenance.provenance_disposition == "SAME_ORIGIN"
    assert provenance.producer_session_id == lease.guard.session_id
    assert provenance.producer_session_id != created.child_session_id


def test_fork_republishes_child_local_image_refs_and_preserves_typed_history(repo):
    lease = new_session(repo)
    payload = BytesIO()
    Image.new("RGB", (7, 5), (11, 23, 41)).save(payload, "PNG")
    image = LLMImagePart("image/png", payload.getvalue(), 7, 5)
    content = FrozenPromptContent(
        (LLMTextPart("before"), image, LLMTextPart("after"), image)
    )
    turn_id = identity("turn")
    start_test_root_turn(
        repo,
        lease.guard,
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
    anchor = final(repo, lease.guard, turn_id)

    created = fork(repo, lease.guard.session_id, anchor)
    assert created.created, created.public_code
    child = created.child_session_id
    copied_user = rows(
        repo,
        """SELECT id FROM pulsara_v3.transcript_entries
           WHERE session_id=%s AND entry_kind='USER_MESSAGE'""",
        (child,),
    )[0]["id"]
    source_refs = rows(
        repo,
        """SELECT ref_ordinal, blob_id FROM pulsara_v3.canonical_image_refs
           WHERE session_id=%s AND transcript_entry_id IS NOT NULL
           ORDER BY ref_ordinal""",
        (lease.guard.session_id,),
    )
    child_refs = rows(
        repo,
        """SELECT ref_ordinal, blob_id FROM pulsara_v3.canonical_image_refs
           WHERE session_id=%s AND transcript_entry_id=%s ORDER BY ref_ordinal""",
        (child, copied_user),
    )
    assert [(row["ref_ordinal"], row["blob_id"]) for row in child_refs] == [
        (row["ref_ordinal"], row["blob_id"]) for row in source_refs
    ]
    assert [row["ref_ordinal"] for row in child_refs] == [0, 1]

    workspace_id = str(
        rows(
            repo,
            "SELECT workspace_id FROM pulsara_v3.sessions WHERE id=%s",
            (child,),
        )[0]["workspace_id"]
    )
    assert PostgresCanonicalImageReferenceReadPort(
        repo.connection_provider,
        session_id=child,
        workspace_id=workspace_id,
    ).read_image(
        session_id=child,
        workspace_id=workspace_id,
        image_ref=image.content_digest,
        maximum_encoded_bytes=1 << 20,
        deadline_monotonic=monotonic() + 30,
    ) == image

    guard = child_lease(repo, child).guard
    _, cut, _ = turn(repo, guard, "continue child", finish=False)
    snapshot = CanonicalProviderInputReader(
        repo.connection_provider
    ).read_frozen_snapshot(cut, deadline_monotonic=monotonic() + 30)
    assert snapshot.items[0].content == content.parts
    assert provider_input_item_text(snapshot.items[-1]) == "continue child"


def test_fork_cannot_reread_image_outside_its_anchor_cut(repo):
    lease = new_session(repo)
    _, _, anchor = turn(repo, lease.guard, "before image")
    assert anchor is not None
    payload = BytesIO()
    Image.new("RGB", (7, 5), (11, 23, 41)).save(payload, "PNG")
    image = LLMImagePart("image/png", payload.getvalue(), 7, 5)
    turn_id = identity("turn")
    start_test_root_turn(
        repo,
        lease.guard,
        command_id=identity("command"),
        turn_id=turn_id,
        permission_snapshot_id=identity("permission"),
        requested_permission_mode=DEFAULT_PERMISSION_MODE,
        entry_id=identity("entry"),
        context_binding_revision_id=identity("revision"),
        content=FrozenPromptContent((LLMTextPart("later"), image)),
        occurred_at=datetime.now(timezone.utc),
        deadline_monotonic=monotonic() + 30,
        model_call_binding=model_binding(model_runtime()),
    )
    final(repo, lease.guard, turn_id)

    created = fork(repo, lease.guard.session_id, anchor)
    assert created.created, created.public_code
    child = created.child_session_id
    workspace_id = str(
        rows(
            repo,
            "SELECT workspace_id FROM pulsara_v3.sessions WHERE id=%s",
            (child,),
        )[0]["workspace_id"]
    )
    child_reader = PostgresCanonicalImageReferenceReadPort(
        repo.connection_provider,
        session_id=child,
        workspace_id=workspace_id,
    )
    with pytest.raises(CanonicalImageReferenceUnavailable):
        child_reader.read_image(
            session_id=child,
            workspace_id=workspace_id,
            image_ref=image.content_digest,
            maximum_encoded_bytes=1 << 20,
            deadline_monotonic=monotonic() + 30,
        )


def test_image_snapshot_adoption_and_fork_keep_child_readable_after_old_owner_delete(
    repo,
    stage2_migrated_postgres_database,
):
    lease = new_session(repo)
    payload = BytesIO()
    Image.new("RGB", (7, 5), (11, 23, 41)).save(payload, "PNG")
    image = LLMImagePart("image/png", payload.getvalue(), 7, 5)
    content = FrozenPromptContent((LLMTextPart("before"), image, image))
    turn_id = identity("turn")
    start_test_root_turn(
        repo,
        lease.guard,
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
    carrier = compact(repo, lease.guard, turn_id, 1, "image snapshot")
    parent_snapshot = rows(
        repo,
        """SELECT s.id
           FROM pulsara_v3.context_snapshots s
           WHERE s.session_id=%s""",
        (lease.guard.session_id,),
    )[0]["id"]
    parent_refs = rows(
        repo,
        """SELECT ref_ordinal, blob_id
           FROM pulsara_v3.canonical_image_refs
           WHERE session_id=%s AND context_snapshot_id=%s
           ORDER BY ref_ordinal""",
        (lease.guard.session_id, parent_snapshot),
    )
    assert [row["ref_ordinal"] for row in parent_refs] == [0, 1]
    assert parent_refs[0]["blob_id"] == parent_refs[1]["blob_id"]
    assert carrier.active_request is not None
    assert carrier.active_request.content == content

    anchor = final(repo, lease.guard, turn_id)
    created = fork(repo, lease.guard.session_id, anchor)
    assert created.created, created.public_code
    child = created.child_session_id
    child_snapshot = rows(
        repo,
        "SELECT id FROM pulsara_v3.context_snapshots WHERE session_id=%s",
        (child,),
    )[0]["id"]
    child_refs = rows(
        repo,
        """SELECT ref_ordinal, blob_id
           FROM pulsara_v3.canonical_image_refs
           WHERE session_id=%s AND context_snapshot_id=%s
           ORDER BY ref_ordinal""",
        (child, child_snapshot),
    )
    assert child_refs == parent_refs

    # Remove the actual adopted parent snapshot owner after rewinding its
    # completed turn to the still-valid revision-zero binding. The owner-local
    # cascade must not affect the fork's independent snapshot refs.
    with psycopg.connect(stage2_migrated_postgres_database.admin_dsn) as connection:
        revision_zero = connection.execute(
            """SELECT id FROM pulsara_v3.turn_context_binding_revisions
               WHERE session_id=%s AND turn_id=%s AND revision_ordinal=0""",
            (lease.guard.session_id, turn_id),
        ).fetchone()[0]
        connection.execute(
            """UPDATE pulsara_v3.turns
               SET current_context_binding_revision_id=%s
               WHERE session_id=%s AND id=%s""",
            (revision_zero, lease.guard.session_id, turn_id),
        )
        connection.execute(
            """UPDATE pulsara_v3.transcript_entries
               SET context_binding_revision_id=%s
               WHERE session_id=%s AND turn_id=%s
                 AND context_binding_revision_id<>%s""",
            (revision_zero, lease.guard.session_id, turn_id, revision_zero),
        )
        connection.execute(
            """DELETE FROM pulsara_v3.agent_events
               WHERE session_id=%s AND subject_context_binding_revision_id IN (
                   SELECT id FROM pulsara_v3.turn_context_binding_revisions
                   WHERE session_id=%s AND turn_id=%s AND revision_ordinal>0
               )""",
            (lease.guard.session_id, lease.guard.session_id, turn_id),
        )
        connection.execute(
            """DELETE FROM pulsara_v3.turn_context_binding_revisions
               WHERE session_id=%s AND turn_id=%s AND revision_ordinal>0""",
            (lease.guard.session_id, turn_id),
        )
        connection.execute(
            "DELETE FROM pulsara_v3.context_snapshots WHERE session_id=%s AND id=%s",
            (lease.guard.session_id, parent_snapshot),
        )
    assert rows(
        repo,
        """SELECT 1 FROM pulsara_v3.canonical_image_refs
           WHERE session_id=%s AND context_snapshot_id=%s""",
        (lease.guard.session_id, parent_snapshot),
    ) == []
    assert rows(
        repo,
        """SELECT ref_ordinal, blob_id FROM pulsara_v3.canonical_image_refs
           WHERE session_id=%s AND context_snapshot_id=%s ORDER BY ref_ordinal""",
        (child, child_snapshot),
    ) == child_refs
    child_workspace_id = str(
        rows(
            repo,
            "SELECT workspace_id FROM pulsara_v3.sessions WHERE id=%s",
            (child,),
        )[0]["workspace_id"]
    )
    assert PostgresCanonicalImageReferenceReadPort(
        repo.connection_provider,
        session_id=child,
        workspace_id=child_workspace_id,
    ).read_image(
        session_id=child,
        workspace_id=child_workspace_id,
        image_ref=image.content_digest,
        maximum_encoded_bytes=1 << 20,
        deadline_monotonic=monotonic() + 30,
    ) == image

    guard = child_lease(repo, child).guard
    _, cut, _ = turn(repo, guard, "continue child", finish=False)
    hydrated = CanonicalProviderInputReader(
        repo.connection_provider
    ).read_frozen_snapshot(cut, deadline_monotonic=monotonic() + 30)
    snapshot_item = hydrated.items[0]
    assert isinstance(snapshot_item.content, CompactionSnapshotCarrier)
    lowered = lower_canonical_item(
        snapshot_item,
        artifact_read_available=False,
        limits=StructuredModelInputLimits(),
    )
    assert lowered.fixed_message is not None
    assert tuple(
        part
        for part in lowered.fixed_message.content
        if isinstance(part, LLMImagePart)
    ) == (image, image)


def test_fork_imported_anchor_is_only_entry_and_stays_fixed_after_local_turn(repo):
    lease = new_session(repo)
    turn(repo, lease.guard, "one", answer="answer one")
    _, _, final = turn(repo, lease.guard, "two", answer="answer two")
    created = fork(repo, lease.guard.session_id, final)
    assert created.created, created.public_code
    child = created.child_session_id
    copied = rows(
        repo,
        "SELECT * FROM pulsara_v3.transcript_entries WHERE session_id=%s ORDER BY entry_sequence",
        (child,),
    )
    assert not fork(repo, child, copied[1]["id"]).created
    child_lease = repo.acquire_host_writer(
        session_id=child,
        workspace_id=copied[0]["workspace_id"],
        writer_owner_id=identity("host"),
        lease_seconds=300,
        deadline_monotonic=monotonic() + 30,
    )
    turn(repo, child_lease.guard, "child future", answer="local final")
    nested = fork(repo, child, copied[-1]["id"])
    assert nested.created, nested.public_code
    assert (
        len(
            rows(
                repo,
                "SELECT 1 FROM pulsara_v3.transcript_entries WHERE session_id=%s",
                (nested.child_session_id,),
            )
        )
        == 4
    )


@pytest.mark.parametrize(
    "answer,complete", [("", True), ("   \n\t", True), ("commentary", False)]
)
def test_fork_eligibility_requires_canonical_nonempty_final(repo, answer, complete):
    lease = new_session(repo)
    _, _, anchor = turn(repo, lease.guard, "prompt", answer=answer, complete=complete)
    rejected = fork(repo, lease.guard.session_id, anchor)
    assert not rejected.created
    assert (
        rows(
            repo,
            "SELECT 1 FROM pulsara_v3.sessions WHERE id=%s",
            (rejected.child_session_id,),
        )
        == []
    )
    with repo.connection_provider.connection(
        lane=PostgresConnectionLane.INSPECTOR,
        row_factory=dict_row,
        deadline_monotonic=monotonic() + 30,
    ) as conn:
        assert read_fork_anchor(conn, lease.guard.session_id, anchor) is None


@pytest.mark.parametrize("aborted", [False, True])
def test_fork_plan_history_retains_origin_and_attribution_without_live_authority(
    repo, aborted
):
    from tests.test_round4_plan_postgres import (
        _open_user_plan,
        _start_root,
        _commit_plan_batch,
    )
    from pulsara_agent.conversation_kernel.repository import PlanQuestionAnswer
    from pulsara_agent.primitives.plan_workflow import PlanQuestionAnswerKind
    from pulsara_agent.model_input.contracts import ProviderToolResultClosureKind

    lease = new_session(repo)
    workflow, _ = _open_user_plan(repo, lease)
    planning, _ = _start_root(repo, lease)
    workspace = repo.read_session_workspace_id(
        lease.guard, deadline_monotonic=monotonic() + 30
    )
    batch = _commit_plan_batch(
        repo,
        lease,
        workspace_id=workspace,
        turn_id=planning,
        selected_tool_name="ask_plan_question",
        selected_arguments={
            "question": "Proceed?",
            "options": [],
            "allow_free_text": True,
        },
        workflow_id=workflow,
        expected_workflow_revision=1,
    )
    opened = repo.accept_plan_tool_batch(
        lease.guard, candidate=batch.candidate, deadline_monotonic=monotonic() + 30
    )
    if aborted:
        repo.interrupt_turn(
            lease.guard,
            turn_id=planning,
            reason="FORCE_EXIT_TEST",
            occurred_at=datetime.now(timezone.utc),
            actor_id="runtime",
            deadline_monotonic=monotonic() + 30,
        )
        repo.exit_plan_by_user(
            lease.guard,
            command_id=identity("command"),
            command_kind="FORCE_EXIT_PLAN",
            workflow_id=workflow,
            expected_workflow_revision=2,
            occurred_at=datetime.now(timezone.utc),
            actor_id="user",
            deadline_monotonic=monotonic() + 30,
        )
        continued, _ = _start_root(repo, lease, text=b"continue outside Plan")
        anchor = final(repo, lease.guard, continued)
    else:
        answer = PlanQuestionAnswer(
            PlanQuestionAnswerKind.FREE_TEXT,
            free_text="Proceed exactly as discussed",
        )
        result_id = identity("result")
        result_entry_id = identity("entry")
        occurred_at = datetime.now(timezone.utc)
        provider_candidate = (
            repo.prepare_plan_question_resolution_provider_input_candidate(
                lease.guard,
                workflow_id=workflow,
                expected_workflow_revision=2,
                interaction_id=opened.interaction_id,
                answer=answer,
                result_id=result_id,
                result_entry_id=result_entry_id,
                occurred_at=occurred_at,
                deadline_monotonic=monotonic() + 30,
            )
        )
        repo.resolve_plan_question(
            lease.guard,
            command_id=identity("command"),
            workflow_id=workflow,
            expected_workflow_revision=2,
            interaction_id=opened.interaction_id,
            answer=answer,
            result_id=result_id,
            result_entry_id=result_entry_id,
            provider_input_admission=cast(
                PreparedActiveRootInputAdmission,
                SimpleNamespace(candidate=provider_candidate),
            ),
            occurred_at=occurred_at,
            actor_id="user",
            deadline_monotonic=monotonic() + 30,
        )
        anchor = final(repo, lease.guard, planning)
    created = fork(repo, lease.guard.session_id, anchor)
    assert created.created, created.public_code
    child = created.child_session_id
    assert (
        rows(
            repo,
            "SELECT 1 FROM pulsara_v3.plan_workflows WHERE session_id=%s",
            (child,),
        )
        == []
    )
    assert (
        rows(
            repo,
            "SELECT 1 FROM pulsara_v3.plan_interactions WHERE session_id=%s",
            (child,),
        )
        == []
    )
    _, cut, _ = turn(
        repo, child_lease(repo, child).guard, "new independent work", finish=False
    )
    canonical = CanonicalProviderInputReader(
        repo.connection_provider
    ).read_frozen_compile_snapshot(cut, deadline_monotonic=monotonic() + 30)
    if aborted:
        assert (
            canonical.canonical_input.closures[0].closure_kind
            is ProviderToolResultClosureKind.PLAN_INTERACTION_ABORTED
        )
        handoffs = rows(
            repo,
            "SELECT * FROM pulsara_v3.transcript_entries WHERE session_id=%s AND imported_source_plan_handoff_kind IS NOT NULL",
            (child,),
        )
        assert (
            len(handoffs) == 1
            and handoffs[0]["imported_source_plan_workflow_id"] == workflow
        )
        assert handoffs[0]["source_plan_workflow_id"] is None
    else:
        result = rows(
            repo,
            "SELECT * FROM pulsara_v3.tool_results WHERE session_id=%s AND tool_call_id=%s",
            (child, batch.selected_tool_call_id),
        )[0]
        assert result["result_origin_kind"] == "PLAN_CONTROL"
        assert (
            result["control_plan_workflow_id"] is None
            and result["control_plan_interaction_id"] is None
        )
        assert (
            result["attempt_id"] is None
            and result["result_record_kind"] == "IMPORTED_HISTORY"
        )
    nested = fork(
        repo,
        child,
        rows(
            repo,
            "SELECT anchor_entry_id FROM pulsara_v3.session_context_genesis WHERE session_id=%s",
            (child,),
        )[0]["anchor_entry_id"],
    )
    assert nested.created, nested.public_code


def test_imported_anchor_binding_survives_child_compaction_and_model_selection(repo):
    from pulsara_agent.llm.model_connections import ModelCallBinding, ModelConnectionId

    lease = new_session(repo)
    _, _, anchor = turn(repo, lease.guard, "original model")
    created = fork(repo, lease.guard.session_id, anchor)
    child = created.child_session_id
    child_guard = child_lease(repo, child).guard
    inherited = rows(
        repo,
        "SELECT * FROM pulsara_v3.session_context_genesis WHERE session_id=%s",
        (child,),
    )[0]
    local_turn, _, _ = turn(repo, child_guard, "child future")
    compact(repo, child_guard, local_turn, 4, "child-only future summary", idle=True)
    repo.update_session_model_call_binding(
        child_guard,
        binding=ModelCallBinding(ModelConnectionId.new(), None),
        deadline_monotonic=monotonic() + 30,
    )
    nested = fork(repo, child, inherited["anchor_entry_id"])
    assert nested.created, nested.public_code
    genesis = rows(
        repo,
        "SELECT * FROM pulsara_v3.session_context_genesis WHERE session_id=%s",
        (nested.child_session_id,),
    )[0]
    assert genesis["model_call_binding"] == inherited["model_call_binding"]
    assert genesis["base_kind"] == "FULL_HISTORY"
    assert (
        rows(
            repo,
            "SELECT 1 FROM pulsara_v3.context_snapshots WHERE session_id=%s",
            (nested.child_session_id,),
        )
        == []
    )
    assert (
        len(
            rows(
                repo,
                "SELECT 1 FROM pulsara_v3.transcript_entries WHERE session_id=%s",
                (nested.child_session_id,),
            )
        )
        == 2
    )


def test_database_rejects_imported_execution_targets(repo):
    import psycopg

    lease = new_session(repo)
    _, _, anchor = turn(repo, lease.guard, "settled")
    created = fork(repo, lease.guard.session_id, anchor)
    child = created.child_session_id
    inherited = rows(
        repo,
        "SELECT anchor_entry_id FROM pulsara_v3.session_context_genesis WHERE session_id=%s",
        (child,),
    )[0]["anchor_entry_id"]
    executed, _, local_final = turn(repo, child_lease(repo, child).guard, "local")
    with pytest.raises(psycopg.errors.CheckViolation, match="exact executed turn"):
        with repo.connection_provider.connection(
            lane=PostgresConnectionLane.HOST_CONTROL,
            row_factory=dict_row,
            deadline_monotonic=monotonic() + 30,
        ) as connection:
            connection.execute(
                "UPDATE pulsara_v3.turns SET final_entry_id=%s WHERE session_id=%s AND id=%s",
                (inherited, child, executed),
            )
    assert (
        rows(
            repo, "SELECT final_entry_id FROM pulsara_v3.turns WHERE id=%s", (executed,)
        )[0]["final_entry_id"]
        == local_final
    )
    event = rows(
        repo,
        "SELECT * FROM pulsara_v3.agent_events WHERE session_id=%s AND subject_entry_id IS NOT NULL LIMIT 1",
        (child,),
    )[0]
    event.update(
        event_id=identity("event"), event_sequence=100, subject_entry_id=inherited
    )
    from psycopg.types.json import Jsonb

    event = {
        key: Jsonb(value) if isinstance(value, dict) else value
        for key, value in event.items()
    }
    from pulsara_agent.conversation_kernel._repository.fork import _insert

    with pytest.raises(
        psycopg.errors.CheckViolation,
        match="execution occurrence cannot target imported history",
    ):
        with repo.connection_provider.connection(
            lane=PostgresConnectionLane.HOST_CONTROL,
            row_factory=dict_row,
            deadline_monotonic=monotonic() + 30,
        ) as connection:
            _insert(connection, "agent_events", event)


def test_fork_new_child_activity_does_not_inherit_old_sort_position(repo):
    from pulsara_agent.conversation_kernel.host import (
        _list_resumable_session_rows_across_workspaces,
    )

    lease = new_session(repo)
    _, _, anchor = turn(repo, lease.guard, "older source")
    created = fork(repo, lease.guard.session_id, anchor)
    activity = _list_resumable_session_rows_across_workspaces(
        repo, "u_local", False, monotonic() + 30
    )
    assert activity[0]["id"] == created.child_session_id
    child = rows(
        repo,
        "SELECT created_at FROM pulsara_v3.sessions WHERE id=%s",
        (created.child_session_id,),
    )[0]
    assert activity[0]["updated_at"] == child["created_at"]


@pytest.mark.parametrize("block_kind", ["data", "invalid_utf8", "tool"])
def test_manifest_data_or_tool_without_final_natural_language_is_not_forkable(
    repo, block_kind
):
    from pulsara_agent.conversation_kernel.repository import (
        AssistantDataBlock,
        AssistantToolCallBlock,
    )
    from pulsara_agent.primitives.context import freeze_json

    lease = new_session(repo)
    running, cut, _ = turn(repo, lease.guard, "question", finish=False)
    entry = identity("entry")
    if block_kind == "data":
        block = AssistantDataBlock(
            identity("block"), InlineContent.from_bytes(b"answer-looking data")
        )
    elif block_kind == "invalid_utf8":
        block = AssistantTextBlock(identity("block"), InlineContent.from_bytes(b"\xff"))
    else:
        block = AssistantToolCallBlock(
            identity("block"), identity("call"), "removed_tool", freeze_json({})
        )
    repo.commit_assistant_message(
        lease.guard,
        cut=cut,
        entry_id=entry,
        parent_content=InlineContent.from_bytes(b"a final-looking manifest"),
        blocks=(block,),
        complete_turn=block_kind != "tool",
        occurred_at=datetime.now(timezone.utc),
        actor_id="model",
        deadline_monotonic=monotonic() + 30,
    )
    if block_kind == "tool":
        repo.interrupt_turn(
            lease.guard,
            turn_id=running,
            reason="HOST_CRASH",
            occurred_at=datetime.now(timezone.utc),
            actor_id="runtime",
            deadline_monotonic=monotonic() + 30,
        )
    assert not fork(repo, lease.guard.session_id, entry).created


def test_fork_http_closed_request_scoped_lookup_and_cross_site_rejection(tmp_path):
    import asyncio
    from types import SimpleNamespace
    from unittest.mock import AsyncMock
    from aiohttp import ClientSession
    from tests.test_local_web_http_surface import _model_server_dependencies, _Bridge
    from pulsara_agent.web_app.http_server import LocalHttpServer
    from pulsara_agent.web_app.application import packaged_static_root

    async def check():
        child = identity("session")
        sessions = SimpleNamespace(
            fork_conversation=AsyncMock(
                return_value={
                    "outcome": "CREATED_OPEN_DEFERRED",
                    "child_session_id": child,
                }
            ),
            read_session=AsyncMock(return_value={"id": child}),
        )
        server = LocalHttpServer(
            sessions=sessions,
            bridge=_Bridge(),
            static_root=packaged_static_root(),
            requested_port=0,
            is_ready=lambda: True,
            is_draining=lambda: False,
            **_model_server_dependencies(),
        )
        await server.start()
        try:
            async with ClientSession() as client:
                url = f"{server.origin}/api/sessions/parent/fork"
                body = {"anchor_entry_id": "anchor", "child_session_id": child}
                async with client.post(url, json=body) as response:
                    assert response.status == 200
                    assert (await response.json())["outcome"] == "CREATED_OPEN_DEFERRED"
                async with client.post(
                    url, json={**body, "source_cut": 123}
                ) as response:
                    assert response.status == 400
                async with client.post(
                    url, json=body, headers={"Origin": "https://untrusted.example"}
                ) as response:
                    assert response.status == 403
                async with client.get(
                    f"{server.origin}/api/sessions/{child}"
                ) as response:
                    assert response.status == 200
                    assert (await response.json())["session"]["id"] == child
                sessions.fork_conversation.assert_awaited_once_with(
                    "parent", anchor_entry_id="anchor", child_session_id=child
                )
        finally:
            await server.aclose()

    asyncio.run(check())
