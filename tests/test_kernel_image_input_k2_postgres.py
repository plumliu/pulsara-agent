"""K2 PostgreSQL gates for canonical image owners, reads, and GC."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import datetime, timezone
from io import BytesIO
from time import monotonic
from threading import Event
from types import SimpleNamespace
from typing import cast
from uuid import uuid4

from PIL import Image
import psycopg
from psycopg.rows import dict_row
import pytest

from pulsara_agent.conversation_kernel.blob import (
    PostgresCanonicalBlobStore,
    _delete_orphans_in_connection,
)
from pulsara_agent.conversation_kernel.contracts import (
    InlineContent,
    PromptDeliveryMode,
)
from tests.support.round3 import new_test_provider_input_continuity_owner
from pulsara_agent.conversation_kernel.prompt_content import freeze_canonical_prompt
from pulsara_agent.conversation_kernel.prompt_content import (
    PROMPT_BODY_CODEC,
    PROMPT_BODY_MEDIA_TYPE,
)
from pulsara_agent.conversation_kernel.prompt_storage import (
    CanonicalImageReferenceResourceExceeded,
    CanonicalImageReferenceUnavailable,
    PostgresCanonicalImageReferenceReadPort,
    insert_canonical_prompt_refs,
)
from pulsara_agent.conversation_kernel.provider_dispatch import canonical_frontier
from pulsara_agent.conversation_kernel.queued_prompt_actions import QueuedPromptAction
from pulsara_agent.conversation_kernel.reader import CanonicalProviderInputReader
from pulsara_agent.conversation_kernel.repository import (
    AssistantToolCallBlock,
    ConversationKernelConflict,
    ConversationKernelRepository,
    build_prepared_tool_result_acceptance,
    build_prepared_root_turn_intent,
)
from pulsara_agent.conversation_kernel.steer import (
    PreparedRootProviderInputAdmission,
    PromptIngressConfirmationKind,
    QueuedRootTurnAdmissionConfirmationKind,
    SteerConsumptionConfirmationKind,
    build_prompt_ingress_command,
    build_steer_canonical_base_fence,
    build_steer_consumption_candidate,
)
from pulsara_agent.llm.input import FrozenPromptContent, LLMImagePart, LLMTextPart
from pulsara_agent.model_input.continuity import (
    NoNewTriggerAnchor,
    ProviderInputContinuityScope,
)
from pulsara_agent.model_input.contracts import (
    ModelInputScopeKind,
    StructuredModelInputLimits,
)
from pulsara_agent.model_input.lowering import (
    image_referenced_content,
    lower_canonical_item,
)
from pulsara_agent.primitives.permission import DEFAULT_PERMISSION_MODE
from pulsara_agent.primitives.context import freeze_json
from pulsara_agent.primitives.tool_observation import ToolObservationOrigin
from pulsara_agent.ports.artifact import (
    ToolOutputArtifactDisposition,
    ToolResultDisplayKind,
)
from pulsara_agent.ports.tool_execution import ToolOutputSourceCoverage
from pulsara_agent.storage.postgres_connection_provider import PostgresConnectionLane
from tests.support.model_config import test_model_binding, test_model_runtime
from tests.support.postgres import verified_postgres_provider


pytestmark = pytest.mark.postgres


def _id(prefix: str) -> str:
    if prefix == "workspace":
        return f"ctx:workspace/{uuid4().hex}"
    return f"{prefix}:{uuid4().hex}"


def _png(*, size: tuple[int, int] = (7, 5)) -> bytes:
    output = BytesIO()
    Image.new("RGB", size, (11, 23, 41)).save(output, "PNG")
    return output.getvalue()


def _image(payload: bytes | None = None) -> LLMImagePart:
    return LLMImagePart("image/png", payload or _png(), 7, 5)


def _repository(database) -> ConversationKernelRepository:
    return ConversationKernelRepository(verified_postgres_provider(database.runtime_dsn))


def _bound_session(repository, *, workspace_id: str | None = None):
    runtime = test_model_runtime()
    binding = test_model_binding(runtime)
    lease = repository.acquire_host_writer(
        intent="NEW",
        session_id=_id("session"),
        workspace_id=workspace_id or _id("workspace"),
        writer_owner_id=_id("host"),
        lease_seconds=300,
        deadline_monotonic=monotonic() + 30,
    )
    repository.update_session_model_call_binding(
        lease.guard,
        binding=binding,
        deadline_monotonic=monotonic() + 30,
    )
    return lease, runtime, binding


def _direct_intent(repository, lease, runtime, content: FrozenPromptContent):
    intent = build_prepared_root_turn_intent(
        session_id=lease.guard.session_id,
        command_id=_id("command"),
        turn_id=_id("turn"),
        entry_id=_id("entry"),
        context_binding_revision_id=_id("revision"),
        permission_snapshot_id=_id("permission"),
        requested_permission_mode=DEFAULT_PERMISSION_MODE,
        canonical_prompt=freeze_canonical_prompt(content),
        occurred_at=datetime.now(timezone.utc),
    )
    resolution_snapshot = runtime.freeze_resolution_snapshot()
    candidate = repository.prepare_root_provider_input_candidate(
        lease.guard,
        intent=intent,
        model_resolution_snapshot=resolution_snapshot,
        deadline_monotonic=monotonic() + 30,
    )
    admission = _fixture_root_admission(candidate)
    accepted = repository.accept_root_turn_intent(
        lease.guard,
        intent=intent,
        provider_input_admission=admission,
        model_resolution_snapshot=resolution_snapshot,
        deadline_monotonic=monotonic() + 30,
    )
    return intent, accepted, admission


def _fixture_root_admission(candidate) -> PreparedRootProviderInputAdmission:
    """Repository-only fixture; production admission is compiler/materializer-owned."""

    admission = object.__new__(PreparedRootProviderInputAdmission)
    object.__setattr__(admission, "candidate", candidate)
    return admission


def _rows(repository, query: str, args: tuple[object, ...] = ()):
    with repository.connection_provider.connection(
        lane=PostgresConnectionLane.INSPECTOR,
        row_factory=dict_row,
        deadline_monotonic=monotonic() + 30,
    ) as connection:
        return connection.execute(query, args).fetchall()


def test_direct_image_owner_confirms_and_reader_lowers_exact_typed_content(
    stage2_migrated_postgres_database,
) -> None:
    repository = _repository(stage2_migrated_postgres_database)
    workspace_id = _id("workspace")
    lease, runtime, _binding = _bound_session(
        repository, workspace_id=workspace_id
    )
    image = _image()
    content = FrozenPromptContent(
        (LLMTextPart("before"), image, LLMTextPart("after"), image)
    )
    intent, accepted, admission = _direct_intent(repository, lease, runtime, content)

    confirmation = repository.confirm_root_turn_intent(
        intent=intent,
        guard=lease.guard,
        deadline_monotonic=monotonic() + 30,
    )
    assert confirmation.kind.value == "FULL"
    refs = _rows(
        repository,
        """SELECT ref_ordinal, blob_id FROM pulsara_v3.canonical_image_refs
           WHERE session_id=%s AND transcript_entry_id=%s ORDER BY ref_ordinal""",
        (lease.guard.session_id, intent.entry_id),
    )
    assert [(row["ref_ordinal"], row["blob_id"]) for row in refs] == [
        (0, refs[0]["blob_id"]),
        (1, refs[0]["blob_id"]),
    ]
    entry = _rows(
        repository,
        "SELECT inline_content, blob_id FROM pulsara_v3.transcript_entries WHERE id=%s",
        (intent.entry_id,),
    )[0]
    assert entry["inline_content"] is not None and entry["blob_id"] is None
    image_blob = _rows(
        repository,
        "SELECT body, media_type, codec FROM pulsara_v3.blobs WHERE id=%s",
        (refs[0]["blob_id"],),
    )[0]
    assert bytes(image_blob["body"]) == image.immutable_bytes
    assert (image_blob["media_type"], image_blob["codec"]) == (
        "image/png",
        "binary",
    )

    cut = repository.prepare_provider_input_cut(
        lease.guard,
        turn_id=intent.turn_id,
        deadline_monotonic=monotonic() + 30,
    )
    snapshot = CanonicalProviderInputReader(
        repository.connection_provider
    ).read_frozen_snapshot(cut, deadline_monotonic=monotonic() + 30)
    assert snapshot.items[0].content == content.parts
    lowered = lower_canonical_item(
        snapshot.items[0],
        artifact_read_available=False,
        limits=StructuredModelInputLimits(),
    )
    assert lowered.fixed_message is not None
    assert lowered.fixed_message.content == image_referenced_content(content.parts)
    assert accepted.accepted.entry_id == intent.entry_id

    changed = replace(
        intent,
        canonical_prompt=freeze_canonical_prompt(
            FrozenPromptContent((image, LLMTextPart("before"), image))
        ),
    )
    assert (
        repository.confirm_root_turn_intent(
            intent=changed,
            deadline_monotonic=monotonic() + 30,
        ).kind.value
        == "CONFLICT"
    )
    with pytest.raises(ConversationKernelConflict, match="command identity conflict"):
        changed_admission = _fixture_root_admission(
            replace(
                admission.candidate,
                unpublished_items=(
                    replace(
                        admission.candidate.unpublished_items[0],
                        content=changed.canonical_prompt.content.parts,
                    ),
                ),
                unpublished_item_canonical_expanded_bytes=(
                    changed.canonical_prompt.resource_quote.canonical_expanded_bytes,
                ),
            ),
        )
        repository.accept_root_turn_intent(
            lease.guard,
            intent=changed,
            provider_input_admission=changed_admission,
            model_resolution_snapshot=runtime.freeze_resolution_snapshot(),
            deadline_monotonic=monotonic() + 30,
        )


def test_known_image_reference_requires_one_committed_owner_in_the_bound_session(
    stage2_migrated_postgres_database,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = _repository(stage2_migrated_postgres_database)
    workspace_id = _id("workspace")
    lease, runtime, _binding = _bound_session(repository, workspace_id=workspace_id)
    image = _image()
    _direct_intent(
        repository,
        lease,
        runtime,
        FrozenPromptContent((LLMTextPart("remember"), image)),
    )
    reader = PostgresCanonicalImageReferenceReadPort(
        repository.connection_provider,
        session_id=lease.guard.session_id,
        workspace_id=workspace_id,
    )
    reread = reader.read_image(
        session_id=lease.guard.session_id,
        workspace_id=workspace_id,
        image_ref=image.content_digest,
        maximum_encoded_bytes=1 << 20,
        deadline_monotonic=monotonic() + 30,
    )
    assert reread == image
    exact_reads = 0

    def unexpected_payload_read(*_args, **_kwargs):
        nonlocal exact_reads
        exact_reads += 1
        raise AssertionError("resource failure must precede target payload read")

    monkeypatch.setattr(
        PostgresCanonicalBlobStore,
        "read_exact_in_connection",
        staticmethod(unexpected_payload_read),
    )
    with pytest.raises(CanonicalImageReferenceResourceExceeded):
        reader.read_image(
            session_id=lease.guard.session_id,
            workspace_id=workspace_id,
            image_ref=image.content_digest,
            maximum_encoded_bytes=len(image.immutable_bytes) - 1,
            deadline_monotonic=monotonic() + 30,
        )
    assert exact_reads == 0

    other_lease, _other_runtime, _other_binding = _bound_session(
        repository, workspace_id=workspace_id
    )
    other_reader = PostgresCanonicalImageReferenceReadPort(
        repository.connection_provider,
        session_id=other_lease.guard.session_id,
        workspace_id=workspace_id,
    )
    with pytest.raises(CanonicalImageReferenceUnavailable):
        other_reader.read_image(
            session_id=other_lease.guard.session_id,
            workspace_id=workspace_id,
            image_ref=image.content_digest,
            maximum_encoded_bytes=1 << 20,
            deadline_monotonic=monotonic() + 30,
        )

    orphan = _image(_png(size=(8, 6)))
    with repository.connection_provider.connection(
        lane=PostgresConnectionLane.ARTIFACT,
        row_factory=dict_row,
        deadline_monotonic=monotonic() + 30,
    ) as connection:
        PostgresCanonicalBlobStore.publish_in_connection(
            connection,
            workspace_id=workspace_id,
            content=orphan.immutable_bytes,
            media_type=orphan.media_type,
            codec="binary",
        )
    with pytest.raises(CanonicalImageReferenceUnavailable):
        reader.read_image(
            session_id=lease.guard.session_id,
            workspace_id=workspace_id,
            image_ref=orphan.content_digest,
            maximum_encoded_bytes=1 << 20,
            deadline_monotonic=monotonic() + 30,
        )


def test_typed_tool_results_commit_confirm_reuse_and_rollback_atomically(
    stage2_migrated_postgres_database,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = _repository(stage2_migrated_postgres_database)
    workspace_id = _id("workspace")
    lease, runtime, _binding = _bound_session(
        repository, workspace_id=workspace_id
    )
    intent, _accepted, _admission = _direct_intent(
        repository,
        lease,
        runtime,
        FrozenPromptContent.text("inspect local images"),
    )
    assistant_entry_id = _id("entry")
    call_ids = tuple(_id("call") for _ in range(3))
    cut = repository.prepare_provider_input_cut(
        lease.guard,
        turn_id=intent.turn_id,
        deadline_monotonic=monotonic() + 30,
    )
    repository.commit_assistant_message(
        lease.guard,
        cut=cut,
        entry_id=assistant_entry_id,
        parent_content=InlineContent.from_bytes(b"inspect"),
        blocks=tuple(
            AssistantToolCallBlock(
                block_id=_id("block"),
                tool_call_id=call_id,
                tool_name="view_image",
                arguments=freeze_json({"path": f"/tmp/{ordinal}.png"}),
            )
            for ordinal, call_id in enumerate(call_ids)
        ),
        occurred_at=datetime.now(timezone.utc),
        actor_id="model:test",
        deadline_monotonic=monotonic() + 30,
    )
    permission_fingerprint = str(
        _rows(
            repository,
            "SELECT permission_snapshot_fingerprint FROM pulsara_v3.turns "
            "WHERE session_id=%s AND id=%s",
            (lease.guard.session_id, intent.turn_id),
        )[0]["permission_snapshot_fingerprint"]
    )

    attempt_ids = tuple(_id("attempt") for _ in call_ids)
    for call_id, attempt_id in zip(call_ids, attempt_ids, strict=True):
        repository.accept_tool_attempt(
            lease.guard,
            attempt_id=attempt_id,
            assistant_entry_id=assistant_entry_id,
            tool_call_id=call_id,
            authorization_kind="policy",
            authorization_reference="allow",
            actor_kind="runtime",
            actor_id="view_image",
            remote_idempotency_key=None,
            retry_of_attempt_id=None,
            permission_snapshot_fingerprint=permission_fingerprint,
            occurred_at=datetime.now(timezone.utc),
            deadline_monotonic=monotonic() + 30,
        )

    shared_image = _image()

    def candidate(
        *, ordinal: int, image: LLMImagePart, result_entry_id: str
    ):
        prompt = freeze_canonical_prompt(
            FrozenPromptContent((LLMTextPart("Image loaded."), image))
        )
        return build_prepared_tool_result_acceptance(
            guard=lease.guard,
            workspace_id=workspace_id,
            result_id=_id("result"),
            result_entry_id=result_entry_id,
            turn_id=intent.turn_id,
            assistant_entry_id=assistant_entry_id,
            tool_call_id=call_ids[ordinal],
            attempt_id=attempt_ids[ordinal],
            result_state="SUCCESS",
            canonical_preview_content=InlineContent.from_bytes(
                prompt.body,
                media_type=PROMPT_BODY_MEDIA_TYPE,
                codec=PROMPT_BODY_CODEC,
            ),
            canonical_prompt=prompt,
            artifact_disposition=ToolOutputArtifactDisposition.NOT_REQUIRED,
            artifact_id=None,
            artifact_blob_descriptor=None,
            source_coverage=ToolOutputSourceCoverage.COMPLETE,
            display_kind=ToolResultDisplayKind.COMPLETE,
            source_coverage_reason=None,
            artifact_unavailability_reason=None,
            observed_at=datetime.now(timezone.utc),
            observation_duration_microseconds=1,
            observation_origin_kind=ToolObservationOrigin.BUILTIN,
            trusted_tool_reported_duration_microseconds=None,
            actor_id="view_image",
        )

    accepted_candidates = tuple(
        candidate(
            ordinal=ordinal,
            image=shared_image,
            result_entry_id=_id("entry"),
        )
        for ordinal in range(2)
    )
    for prepared in accepted_candidates:
        repository.accept_tool_result(
            lease.guard,
            candidate=prepared,
            deadline_monotonic=monotonic() + 30,
        )
        assert repository.confirm_tool_result_winner(
            lease.guard,
            candidate=prepared,
            deadline_monotonic=monotonic() + 30,
        ) is not None

    refs = _rows(
        repository,
        "SELECT transcript_entry_id, blob_id FROM pulsara_v3.canonical_image_refs "
        "WHERE session_id=%s AND transcript_entry_id = ANY(%s) "
        "ORDER BY transcript_entry_id",
        (
            lease.guard.session_id,
            [item.result_entry_id for item in accepted_candidates],
        ),
    )
    assert len(refs) == 2
    assert len({str(row["transcript_entry_id"]) for row in refs}) == 2
    assert len({str(row["blob_id"]) for row in refs}) == 1

    final_cut = repository.prepare_provider_input_cut(
        lease.guard,
        turn_id=intent.turn_id,
        deadline_monotonic=monotonic() + 30,
    )
    snapshot = CanonicalProviderInputReader(
        repository.connection_provider
    ).read_frozen_snapshot(final_cut, deadline_monotonic=monotonic() + 30)
    tool_results = tuple(
        item
        for item in snapshot.items
        if item.item_kind.value == "TOOL_RESULT"
    )
    assert len(tool_results) == 2
    assert tuple(item.tool_call_ordinal for item in tool_results) == (0, 1)
    assert all(item.content[1] == shared_image for item in tool_results)

    rollback_image = _image(_png(size=(8, 6)))
    rollback_entry_id = _id("entry")
    rollback_candidate = candidate(
        ordinal=2,
        image=rollback_image,
        result_entry_id=rollback_entry_id,
    )
    original_publish = PostgresCanonicalBlobStore.publish_in_connection

    def fail_after_image_publish(connection, **kwargs):
        published = original_publish(connection, **kwargs)
        if kwargs["content"] == rollback_image.immutable_bytes:
            raise RuntimeError("injected image publication failure")
        return published

    monkeypatch.setattr(
        PostgresCanonicalBlobStore,
        "publish_in_connection",
        staticmethod(fail_after_image_publish),
    )
    with pytest.raises(RuntimeError, match="injected image publication failure"):
        repository.accept_tool_result(
            lease.guard,
            candidate=rollback_candidate,
            deadline_monotonic=monotonic() + 30,
        )
    assert _rows(
        repository,
        "SELECT id FROM pulsara_v3.transcript_entries WHERE session_id=%s AND id=%s",
        (lease.guard.session_id, rollback_entry_id),
    ) == []
    assert _rows(
        repository,
        "SELECT id FROM pulsara_v3.tool_results WHERE session_id=%s AND id=%s",
        (lease.guard.session_id, rollback_candidate.result_id),
    ) == []
    assert _rows(
        repository,
        "SELECT id FROM pulsara_v3.blobs WHERE workspace_id=%s AND logical_digest=%s",
        (workspace_id, rollback_image.content_digest),
    ) == []

    drifted_prompt = freeze_canonical_prompt(
        FrozenPromptContent((LLMTextPart("Image loaded."), rollback_image))
    )
    drifted = replace(
        accepted_candidates[0],
        canonical_preview_content=InlineContent.from_bytes(
            drifted_prompt.body,
            media_type=PROMPT_BODY_MEDIA_TYPE,
            codec=PROMPT_BODY_CODEC,
        ),
        canonical_prompt=drifted_prompt,
    )
    with pytest.raises(ConversationKernelConflict, match="different winner"):
        repository.confirm_tool_result_winner(
            lease.guard,
            candidate=drifted,
            deadline_monotonic=monotonic() + 30,
        )

    reference_reader = PostgresCanonicalImageReferenceReadPort(
        repository.connection_provider,
        session_id=lease.guard.session_id,
        workspace_id=workspace_id,
    )
    assert reference_reader.read_image(
        session_id=lease.guard.session_id,
        workspace_id=workspace_id,
        image_ref=shared_image.content_digest,
        maximum_encoded_bytes=1 << 20,
        deadline_monotonic=monotonic() + 30,
    ) == shared_image
    selected_owner_id = min(item.result_entry_id for item in accepted_candidates)
    with psycopg.connect(
        stage2_migrated_postgres_database.admin_dsn,
        autocommit=True,
        row_factory=dict_row,
    ) as connection:
        connection.execute("SET session_replication_role = replica")
        row = connection.execute(
            "SELECT content_size FROM pulsara_v3.transcript_entries WHERE id=%s",
            (selected_owner_id,),
        ).fetchone()
        assert row is not None
        try:
            connection.execute(
                "UPDATE pulsara_v3.transcript_entries SET inline_content=%s WHERE id=%s",
                (b"x" * int(row["content_size"]), selected_owner_id),
            )
        finally:
            connection.execute("SET session_replication_role = origin")
    with pytest.raises(ConversationKernelConflict):
        reference_reader.read_image(
            session_id=lease.guard.session_id,
            workspace_id=workspace_id,
            image_ref=shared_image.content_digest,
            maximum_encoded_bytes=1 << 20,
            deadline_monotonic=monotonic() + 30,
        )


def test_compaction_headroom_counts_one_snapshot_with_repeated_image_refs_once(
    stage2_migrated_postgres_database,
) -> None:
    repository = _repository(stage2_migrated_postgres_database)
    lease, runtime, _binding = _bound_session(repository)
    intent, _accepted, _admission = _direct_intent(
        repository,
        lease,
        runtime,
        FrozenPromptContent((LLMTextPart("request"),)),
    )
    session_id = lease.guard.session_id
    workspace_id = str(
        _rows(
            repository,
            "SELECT workspace_id FROM pulsara_v3.sessions WHERE id=%s",
            (session_id,),
        )[0]["workspace_id"]
    )
    snapshot_id = _id("snapshot")
    revision_id = _id("revision")
    body = InlineContent.from_bytes(
        b"{}",
        media_type="application/vnd.pulsara.context-snapshot+json",
        codec="utf-8",
    )
    with repository.connection_provider.connection(
        lane=PostgresConnectionLane.HOST_CONTROL,
        row_factory=dict_row,
        deadline_monotonic=monotonic() + 30,
    ) as connection:
        blob = PostgresCanonicalBlobStore.publish_in_connection(
            connection,
            workspace_id=workspace_id,
            content=_png(),
            media_type="image/png",
            codec="binary",
        )
        connection.execute(
            """INSERT INTO pulsara_v3.context_snapshots (
                 id, session_id, workspace_id, source_through_sequence,
                 source_digest, compiler_contract, prompt_contract, model_contract,
                 inline_content, blob_id, content_digest, content_size,
                 content_media_type, content_codec
               ) VALUES (%s,%s,%s,0,%s,'compiler','prompt','model',%s,NULL,%s,%s,%s,%s)""",
            (
                snapshot_id,
                session_id,
                workspace_id,
                "sha256:" + "7" * 64,
                body.canonical_bytes,
                body.digest,
                body.size,
                body.media_type,
                body.codec,
            ),
        )
        insert_canonical_prompt_refs(
            connection,
            session_id=session_id,
            workspace_id=workspace_id,
            image_blob_ids=(blob.blob_id, blob.blob_id),
            context_snapshot_id=snapshot_id,
        )
        connection.execute(
            """INSERT INTO pulsara_v3.turn_context_binding_revisions (
                 id, session_id, turn_id, revision_ordinal, base_kind,
                 context_snapshot_id, source_through_sequence
               ) VALUES (%s,%s,%s,1,'SNAPSHOT',%s,0)""",
            (revision_id, session_id, intent.turn_id, snapshot_id),
        )
        connection.execute(
            """UPDATE pulsara_v3.turns
               SET current_context_binding_revision_id=%s
               WHERE session_id=%s AND id=%s""",
            (revision_id, session_id, intent.turn_id),
        )

    cut = repository.prepare_provider_input_cut(
        lease.guard,
        turn_id=intent.turn_id,
        deadline_monotonic=monotonic() + 30,
    )
    quote = CanonicalProviderInputReader(
        repository.connection_provider
    ).read_compaction_headroom_preflight(
        cut,
        deadline_monotonic=monotonic() + 30,
    )

    # One snapshot base plus one USER entry. Repeated image occurrences charge
    # bytes twice, but they never multiply the snapshot's item cardinality.
    assert quote.selected_item_count == 2
    assert quote.selected_canonical_expanded_bytes >= (
        body.size + 2 * blob.size
    )


def test_full_confirmation_reads_body_blob_but_not_image_payload(
    stage2_migrated_postgres_database,
) -> None:
    repository = _repository(stage2_migrated_postgres_database)
    lease, runtime, _binding = _bound_session(repository)
    image = _image()
    content = FrozenPromptContent((LLMTextPart("x" * 70_000), image))
    intent, _accepted, _admission = _direct_intent(
        repository, lease, runtime, content
    )
    owner = _rows(
        repository,
        "SELECT blob_id FROM pulsara_v3.transcript_entries WHERE id=%s",
        (intent.entry_id,),
    )[0]
    image_ref = _rows(
        repository,
        "SELECT blob_id FROM pulsara_v3.canonical_image_refs WHERE transcript_entry_id=%s",
        (intent.entry_id,),
    )[0]
    assert owner["blob_id"] is not None and owner["blob_id"] != image_ref["blob_id"]

    with psycopg.connect(stage2_migrated_postgres_database.admin_dsn) as connection:
        payload = bytes(
            connection.execute(
                "SELECT body FROM pulsara_v3.blobs WHERE id=%s",
                (image_ref["blob_id"],),
            ).fetchone()[0]
        )
        connection.execute(
            "UPDATE pulsara_v3.blobs SET body=%s WHERE id=%s",
            (bytes([payload[0] ^ 0xFF]) + payload[1:], image_ref["blob_id"]),
        )
    assert (
        repository.confirm_root_turn_intent(
            intent=intent,
            deadline_monotonic=monotonic() + 30,
        ).kind.value
        == "FULL"
    )
    cut = repository.prepare_provider_input_cut(
        lease.guard,
        turn_id=intent.turn_id,
        deadline_monotonic=monotonic() + 30,
    )
    with pytest.raises(ConversationKernelConflict, match="integrity mismatch"):
        CanonicalProviderInputReader(
            repository.connection_provider
        ).read_frozen_snapshot(cut, deadline_monotonic=monotonic() + 30)

    with psycopg.connect(stage2_migrated_postgres_database.admin_dsn) as connection:
        body = bytes(
            connection.execute(
                "SELECT body FROM pulsara_v3.blobs WHERE id=%s",
                (owner["blob_id"],),
            ).fetchone()[0]
        )
        connection.execute(
            "UPDATE pulsara_v3.blobs SET body=%s WHERE id=%s",
            (body[:-1] + bytes([body[-1] ^ 0xFF]), owner["blob_id"]),
        )
    assert (
        repository.confirm_root_turn_intent(
            intent=intent,
            deadline_monotonic=monotonic() + 30,
        ).kind.value
        == "CONFLICT"
    )


def test_queue_redirect_and_steer_copy_independent_ordered_refs(
    stage2_migrated_postgres_database,
) -> None:
    repository = _repository(stage2_migrated_postgres_database)
    lease, runtime, binding = _bound_session(repository)
    workspace_id = str(
        _rows(
            repository,
            "SELECT workspace_id FROM pulsara_v3.sessions WHERE id=%s",
            (lease.guard.session_id,),
        )[0]["workspace_id"]
    )
    image = _image()
    initial_content = FrozenPromptContent((image,))
    permission_id = _id("permission")
    expected_permission = repository.prepare_root_permission_snapshot(
        lease.guard,
        snapshot_id=permission_id,
        requested_mode=DEFAULT_PERMISSION_MODE,
        deadline_monotonic=monotonic() + 30,
    )
    initial = build_prompt_ingress_command(
        session_id=lease.guard.session_id,
        command_id=_id("command"),
        queue_item_id=_id("queue"),
        client_submission_id=_id("submission"),
        delivery_mode=PromptDeliveryMode.NEW_TURN,
        target_turn_id=None,
        permission_snapshot_id=permission_id,
        requested_permission_mode=DEFAULT_PERMISSION_MODE,
        canonical_prompt=freeze_canonical_prompt(initial_content),
    )
    repository.enqueue_prompt(
        lease.guard,
        candidate=initial,
        model_resolution_snapshot=runtime.freeze_resolution_snapshot(),
        occurred_at=datetime.now(timezone.utc),
        actor_id="user",
        deadline_monotonic=monotonic() + 30,
        _expected_permission_snapshot=expected_permission,
    )
    assert (
        repository.confirm_prompt_ingress(
            candidate=initial,
            deadline_monotonic=monotonic() + 30,
        ).kind
        is PromptIngressConfirmationKind.FULL_COMPATIBLE
    )
    reference_reader = PostgresCanonicalImageReferenceReadPort(
        repository.connection_provider,
        session_id=lease.guard.session_id,
        workspace_id=workspace_id,
    )
    with pytest.raises(CanonicalImageReferenceUnavailable):
        reference_reader.read_image(
            session_id=lease.guard.session_id,
            workspace_id=workspace_id,
            image_ref=image.content_digest,
            maximum_encoded_bytes=1 << 20,
            deadline_monotonic=monotonic() + 30,
        )
    queued = repository.prepare_prompt_head_consumption(
        session_id=lease.guard.session_id,
        occurred_at=datetime.now(timezone.utc),
        actor_id=lease.guard.writer_owner_id,
        deadline_monotonic=monotonic() + 30,
    )
    assert queued is not None
    consumed = repository.consume_prepared_prompt_head(
        lease.guard,
        candidate=queued,
        provider_input_admission=cast(
            PreparedRootProviderInputAdmission,
            SimpleNamespace(candidate=queued.provider_input_candidate),
        ),
        deadline_monotonic=monotonic() + 30,
    )
    assert consumed is not None
    assert consumed.kind is QueuedRootTurnAdmissionConfirmationKind.FULL
    assert reference_reader.read_image(
        session_id=lease.guard.session_id,
        workspace_id=workspace_id,
        image_ref=image.content_digest,
        maximum_encoded_bytes=1 << 20,
        deadline_monotonic=monotonic() + 30,
    ) == image
    assert (
        repository.confirm_prompt_ingress(
            candidate=initial,
            deadline_monotonic=monotonic() + 30,
        ).status
        == "CONSUMED"
    )

    second_permission_id = _id("permission")
    second_permission = repository.prepare_root_permission_snapshot(
        lease.guard,
        snapshot_id=second_permission_id,
        requested_mode=DEFAULT_PERMISSION_MODE,
        deadline_monotonic=monotonic() + 30,
    )
    second_content = FrozenPromptContent((LLMTextPart("next"), image, image))
    second = build_prompt_ingress_command(
        session_id=lease.guard.session_id,
        command_id=_id("command"),
        queue_item_id=_id("queue"),
        client_submission_id=_id("submission"),
        delivery_mode=PromptDeliveryMode.NEW_TURN,
        target_turn_id=None,
        permission_snapshot_id=second_permission_id,
        requested_permission_mode=DEFAULT_PERMISSION_MODE,
        canonical_prompt=freeze_canonical_prompt(second_content),
    )
    repository.enqueue_prompt(
        lease.guard,
        candidate=second,
        model_resolution_snapshot=runtime.freeze_resolution_snapshot(),
        occurred_at=datetime.now(timezone.utc),
        actor_id="user",
        deadline_monotonic=monotonic() + 30,
        _expected_permission_snapshot=second_permission,
    )
    action = QueuedPromptAction(
        session_id=lease.guard.session_id,
        command_id=_id("redirect"),
        source_queue_item_id=second.queue_item_id,
        target_turn_id=queued.exact_turn_id,
    )
    repository.apply_queued_prompt_action(
        lease.guard,
        candidate=action,
        occurred_at=datetime.now(timezone.utc),
        actor_id="user",
        deadline_monotonic=monotonic() + 30,
    )
    replacement_id = action.replacement_queue_item_id
    assert replacement_id is not None
    owner_refs = _rows(
        repository,
        """SELECT queue_item_id, transcript_entry_id, ref_ordinal, blob_id
           FROM pulsara_v3.canonical_image_refs WHERE session_id=%s
           ORDER BY queue_item_id NULLS LAST, transcript_entry_id NULLS LAST, ref_ordinal""",
        (lease.guard.session_id,),
    )
    grouped: dict[tuple[str | None, str | None], list[tuple[int, str]]] = {}
    for row in owner_refs:
        grouped.setdefault(
            (row["queue_item_id"], row["transcript_entry_id"]), []
        ).append((int(row["ref_ordinal"]), str(row["blob_id"])))
    assert grouped[(initial.queue_item_id, None)] == grouped[
        (None, queued.exact_initial_entry_id)
    ]
    assert grouped[(second.queue_item_id, None)] == grouped[(replacement_id, None)]
    assert [ordinal for ordinal, _blob in grouped[(replacement_id, None)]] == [0, 1]

    fact = repository.read_pending_prompt_steer_facts(
        session_id=lease.guard.session_id,
        target_turn_id=queued.exact_turn_id,
        deadline_monotonic=monotonic() + 30,
    )[0]
    assert fact.canonical_expanded_bytes == (
        len(second.canonical_prompt.body) + 2 * len(image.immutable_bytes)
    )
    hydrated = repository.hydrate_pending_prompt_steer(
        fact=fact,
        deadline_monotonic=monotonic() + 30,
    )
    assert hydrated.content == second_content
    base_cut = repository.prepare_provider_input_cut(
        lease.guard,
        turn_id=queued.exact_turn_id,
        deadline_monotonic=monotonic() + 30,
    )
    base = CanonicalProviderInputReader(
        repository.connection_provider
    ).read_frozen_compile_snapshot(base_cut, deadline_monotonic=monotonic() + 30)
    continuity = new_test_provider_input_continuity_owner(lease.guard.session_id)
    planning = continuity.freeze_planning_input(
        scope=ProviderInputContinuityScope(
            session_id=lease.guard.session_id,
            scope_kind=ModelInputScopeKind.ROOT,
            scope_subagent_task_id=None,
        ),
        canonical_frontier=canonical_frontier(base.canonical_input, base),
        dispatch_anchor=NoNewTriggerAnchor(None),
    )
    steer = build_steer_consumption_candidate(
        fact=fact,
        canonical_prompt=hydrated,
        expected_entry_sequence=2,
        predecessor=planning,
        canonical_base_fence=build_steer_canonical_base_fence(base),
        occurred_at=datetime.now(timezone.utc),
        actor_id=lease.guard.writer_owner_id,
    )
    accepted = repository.consume_prepared_prompt_steer(
        lease.guard,
        candidate=steer,
        deadline_monotonic=monotonic() + 30,
    )
    assert (
        repository.confirm_prepared_prompt_steer(
            candidate=steer,
            deadline_monotonic=monotonic() + 30,
        ).kind
        is SteerConsumptionConfirmationKind.FULL
    )
    assert grouped[(replacement_id, None)] == [
        (int(row["ref_ordinal"]), str(row["blob_id"]))
        for row in _rows(
            repository,
            """SELECT ref_ordinal, blob_id FROM pulsara_v3.canonical_image_refs
               WHERE session_id=%s AND transcript_entry_id=%s ORDER BY ref_ordinal""",
            (lease.guard.session_id, accepted.entry_id),
        )
    ]


class _FailingEventRepository(ConversationKernelRepository):
    def _append_events(self, *_args, **_kwargs):
        raise RuntimeError("injected after canonical publication")


def test_direct_publication_rolls_back_body_image_refs_and_command_together(
    stage2_migrated_postgres_database,
) -> None:
    provider = verified_postgres_provider(stage2_migrated_postgres_database.runtime_dsn)
    repository = _FailingEventRepository(provider)
    lease, runtime, _binding = _bound_session(repository)
    intent = build_prepared_root_turn_intent(
        session_id=lease.guard.session_id,
        command_id=_id("command"),
        turn_id=_id("turn"),
        entry_id=_id("entry"),
        context_binding_revision_id=_id("revision"),
        permission_snapshot_id=_id("permission"),
        requested_permission_mode=DEFAULT_PERMISSION_MODE,
        canonical_prompt=freeze_canonical_prompt(FrozenPromptContent((_image(),))),
        occurred_at=datetime.now(timezone.utc),
    )
    resolution_snapshot = runtime.freeze_resolution_snapshot()
    provider_candidate = repository.prepare_root_provider_input_candidate(
        lease.guard,
        intent=intent,
        model_resolution_snapshot=resolution_snapshot,
        deadline_monotonic=monotonic() + 30,
    )
    with pytest.raises(RuntimeError, match="after canonical publication"):
        repository.accept_root_turn_intent(
            lease.guard,
            intent=intent,
            provider_input_admission=_fixture_root_admission(provider_candidate),
            model_resolution_snapshot=resolution_snapshot,
            deadline_monotonic=monotonic() + 30,
        )
    counts = _rows(
        repository,
        """SELECT
             (SELECT count(*) FROM pulsara_v3.session_commands WHERE session_id=%s) commands,
             (SELECT count(*) FROM pulsara_v3.transcript_entries WHERE session_id=%s) entries,
             (SELECT count(*) FROM pulsara_v3.canonical_image_refs WHERE session_id=%s) refs,
             (SELECT count(*) FROM pulsara_v3.blobs WHERE workspace_id=(
                 SELECT workspace_id FROM pulsara_v3.sessions WHERE id=%s
             )) blobs""",
        (lease.guard.session_id,) * 4,
    )[0]
    assert tuple(counts.values()) == (0, 0, 0, 0)


def test_image_ref_fk_gc_commit_orders_cascade_scope_and_runtime_grants(
    stage2_migrated_postgres_database,
) -> None:
    repository = _repository(stage2_migrated_postgres_database)
    lease, _runtime, _binding = _bound_session(repository)
    session_id = lease.guard.session_id
    workspace_id = _rows(
        repository,
        "SELECT workspace_id FROM pulsara_v3.sessions WHERE id=%s",
        (session_id,),
    )[0]["workspace_id"]
    child_lease, _child_runtime, _child_binding = _bound_session(
        repository, workspace_id=str(workspace_id)
    )
    provider = repository.connection_provider
    store = PostgresCanonicalBlobStore(provider)
    payload = _png()

    # Attach commits first on one caller-owned connection; GC on another
    # connection must retain the blob until deleting the real owner cascades.
    snapshot_id = _id("snapshot")
    child_snapshot_id = _id("snapshot")
    body = InlineContent.from_bytes(
        b"{}", media_type="application/vnd.pulsara.context-snapshot+json", codec="utf-8"
    )
    with provider.connection(
        lane=PostgresConnectionLane.HOST_CONTROL,
        row_factory=dict_row,
        deadline_monotonic=monotonic() + 30,
    ) as attach:
        blob = PostgresCanonicalBlobStore.publish_in_connection(
            attach,
            workspace_id=str(workspace_id),
            content=payload,
            media_type="image/png",
            codec="binary",
        )
        attach.execute(
            """INSERT INTO pulsara_v3.context_snapshots (
                 id, session_id, workspace_id, source_through_sequence,
                 source_digest, compiler_contract, prompt_contract, model_contract,
                 inline_content, blob_id, content_digest, content_size,
                 content_media_type, content_codec
               ) VALUES (%s,%s,%s,0,%s,'compiler','prompt','model',%s,NULL,%s,%s,%s,%s)""",
            (
                snapshot_id,
                session_id,
                workspace_id,
                "sha256:" + "1" * 64,
                body.canonical_bytes,
                body.digest,
                body.size,
                body.media_type,
                body.codec,
            ),
        )
        insert_canonical_prompt_refs(
            attach,
            session_id=session_id,
            workspace_id=str(workspace_id),
            image_blob_ids=(blob.blob_id,),
            context_snapshot_id=snapshot_id,
        )
        attach.execute(
            """INSERT INTO pulsara_v3.context_snapshots (
                 id, session_id, workspace_id, source_through_sequence,
                 source_digest, compiler_contract, prompt_contract, model_contract,
                 inline_content, blob_id, content_digest, content_size,
                 content_media_type, content_codec
               ) VALUES (%s,%s,%s,0,%s,'compiler','prompt','model',%s,NULL,%s,%s,%s,%s)""",
            (
                child_snapshot_id,
                child_lease.guard.session_id,
                workspace_id,
                "sha256:" + "3" * 64,
                body.canonical_bytes,
                body.digest,
                body.size,
                body.media_type,
                body.codec,
            ),
        )
        insert_canonical_prompt_refs(
            attach,
            session_id=child_lease.guard.session_id,
            workspace_id=str(workspace_id),
            image_blob_ids=(blob.blob_id,),
            context_snapshot_id=child_snapshot_id,
        )
    with psycopg.connect(stage2_migrated_postgres_database.admin_dsn) as admin:
        admin.execute(
            "UPDATE pulsara_v3.blobs SET created_at=clock_timestamp()-interval '1 hour' WHERE id=%s",
            (blob.blob_id,),
        )
    assert blob.blob_id not in store.delete_orphans(
        grace_seconds=1,
        deadline_monotonic=monotonic() + 30,
    )
    with psycopg.connect(stage2_migrated_postgres_database.admin_dsn) as admin:
        admin.execute("DELETE FROM pulsara_v3.context_snapshots WHERE id=%s", (snapshot_id,))
    assert _rows(
        repository,
        "SELECT 1 FROM pulsara_v3.canonical_image_refs WHERE context_snapshot_id=%s",
        (snapshot_id,),
    ) == []
    assert blob.blob_id not in store.delete_orphans(
        grace_seconds=1,
        deadline_monotonic=monotonic() + 30,
    )
    assert _rows(
        repository,
        "SELECT blob_id FROM pulsara_v3.canonical_image_refs WHERE context_snapshot_id=%s",
        (child_snapshot_id,),
    ) == [{"blob_id": blob.blob_id}]
    with psycopg.connect(stage2_migrated_postgres_database.admin_dsn) as admin:
        admin.execute(
            "DELETE FROM pulsara_v3.context_snapshots WHERE id=%s",
            (child_snapshot_id,),
        )
    assert blob.blob_id in store.delete_orphans(
        grace_seconds=1,
        deadline_monotonic=monotonic() + 30,
    )

    # Delete commits first; attaching the stale identity fails its transaction.
    with provider.connection(
        lane=PostgresConnectionLane.HOST_CONTROL,
        row_factory=dict_row,
        deadline_monotonic=monotonic() + 30,
    ) as publication:
        deleted = PostgresCanonicalBlobStore.publish_in_connection(
            publication,
            workspace_id=str(workspace_id),
            content=payload + b"different",
            media_type="image/png",
            codec="binary",
        )
    with psycopg.connect(stage2_migrated_postgres_database.admin_dsn) as admin:
        admin.execute(
            "UPDATE pulsara_v3.blobs SET created_at=clock_timestamp()-interval '1 hour' WHERE id=%s",
            (deleted.blob_id,),
        )
    assert deleted.blob_id in store.delete_orphans(
        grace_seconds=1,
        deadline_monotonic=monotonic() + 30,
    )
    failed_owner = _id("snapshot")
    with pytest.raises(psycopg.errors.ForeignKeyViolation):
        with provider.connection(
            lane=PostgresConnectionLane.HOST_CONTROL,
            row_factory=dict_row,
            deadline_monotonic=monotonic() + 30,
        ) as stale_attach:
            stale_attach.execute(
                """INSERT INTO pulsara_v3.context_snapshots (
                     id, session_id, workspace_id, source_through_sequence,
                     source_digest, compiler_contract, prompt_contract, model_contract,
                     inline_content, blob_id, content_digest, content_size,
                     content_media_type, content_codec
                   ) VALUES (%s,%s,%s,0,%s,'compiler','prompt','model',%s,NULL,%s,%s,%s,%s)""",
                (
                    failed_owner,
                    session_id,
                    workspace_id,
                    "sha256:" + "2" * 64,
                    body.canonical_bytes,
                    body.digest,
                    body.size,
                    body.media_type,
                    body.codec,
                ),
            )
            insert_canonical_prompt_refs(
                stale_attach,
                session_id=session_id,
                workspace_id=str(workspace_id),
                image_blob_ids=(deleted.blob_id,),
                context_snapshot_id=failed_owner,
            )
    assert _rows(
        repository,
        "SELECT 1 FROM pulsara_v3.context_snapshots WHERE id=%s",
        (failed_owner,),
    ) == []

    grants = _rows(
        repository,
        """SELECT
             has_table_privilege(current_user,'pulsara_v3.canonical_image_refs','SELECT') can_select,
             has_table_privilege(current_user,'pulsara_v3.canonical_image_refs','INSERT') can_insert,
             has_table_privilege(current_user,'pulsara_v3.canonical_image_refs','UPDATE') can_update,
             has_table_privilege(current_user,'pulsara_v3.canonical_image_refs','DELETE') can_delete""",
    )[0]
    assert grants == {
        "can_select": True,
        "can_insert": True,
        "can_update": False,
        "can_delete": False,
    }


def test_image_ref_gc_and_old_blob_attach_overlap_in_both_commit_orders(
    stage2_migrated_postgres_database,
) -> None:
    repository = _repository(stage2_migrated_postgres_database)
    lease, _runtime, _binding = _bound_session(repository)
    session_id = lease.guard.session_id
    workspace_id = str(
        _rows(
            repository,
            "SELECT workspace_id FROM pulsara_v3.sessions WHERE id=%s",
            (session_id,),
        )[0]["workspace_id"]
    )
    runtime_dsn = stage2_migrated_postgres_database.runtime_dsn
    admin_dsn = stage2_migrated_postgres_database.admin_dsn
    body = InlineContent.from_bytes(
        b"{}",
        media_type="application/vnd.pulsara.context-snapshot+json",
        codec="utf-8",
    )

    def insert_snapshot(connection, snapshot_id: str, source_digest: str) -> None:
        connection.execute(
            """INSERT INTO pulsara_v3.context_snapshots (
                 id, session_id, workspace_id, source_through_sequence,
                 source_digest, compiler_contract, prompt_contract, model_contract,
                 inline_content, blob_id, content_digest, content_size,
                 content_media_type, content_codec
               ) VALUES (%s,%s,%s,0,%s,'compiler','prompt','model',%s,NULL,%s,%s,%s,%s)""",
            (
                snapshot_id,
                session_id,
                workspace_id,
                source_digest,
                body.canonical_bytes,
                body.digest,
                body.size,
                body.media_type,
                body.codec,
            ),
        )

    def age(blob_id: str) -> None:
        with psycopg.connect(admin_dsn) as connection:
            connection.execute(
                """UPDATE pulsara_v3.blobs
                   SET created_at=clock_timestamp()-interval '1 hour'
                   WHERE id=%s""",
                (blob_id,),
            )

    def wait_for_lock(application_name: str) -> None:
        deadline = monotonic() + 10
        with psycopg.connect(admin_dsn, autocommit=True) as connection:
            while True:
                row = connection.execute(
                    """SELECT wait_event_type
                       FROM pg_catalog.pg_stat_activity
                       WHERE application_name=%s""",
                    (application_name,),
                ).fetchone()
                if row is not None and row[0] == "Lock":
                    return
                if monotonic() >= deadline:
                    raise AssertionError(
                        f"{application_name} did not reach its database lock wait"
                    )

    # The attach transaction exact-reuses an already committed orphan blob and
    # publishes its owner ref before commit. A concurrent GC statement sees the
    # old snapshot, blocks on the FK lock, and cannot commit the deletion after
    # attach wins.
    attach_first_payload = _png() + b"attach-first"
    with psycopg.connect(runtime_dsn, row_factory=dict_row) as publication:
        attach_first_blob = PostgresCanonicalBlobStore.publish_in_connection(
            publication,
            workspace_id=workspace_id,
            content=attach_first_payload,
            media_type="image/png",
            codec="binary",
        )
    age(attach_first_blob.blob_id)
    attach_first_owner = _id("snapshot")

    def competing_gc() -> tuple[str, ...]:
        with psycopg.connect(
            runtime_dsn,
            row_factory=dict_row,
            application_name="k2-gc-attach-first",
        ) as connection:
            return _delete_orphans_in_connection(
                connection,
                grace_seconds=1,
                maximum_items=100,
            )

    with psycopg.connect(runtime_dsn, row_factory=dict_row) as attach:
        reused = PostgresCanonicalBlobStore.publish_in_connection(
            attach,
            workspace_id=workspace_id,
            content=attach_first_payload,
            media_type="image/png",
            codec="binary",
        )
        assert reused.blob_id == attach_first_blob.blob_id
        insert_snapshot(attach, attach_first_owner, "sha256:" + "4" * 64)
        insert_canonical_prompt_refs(
            attach,
            session_id=session_id,
            workspace_id=workspace_id,
            image_blob_ids=(reused.blob_id,),
            context_snapshot_id=attach_first_owner,
        )
        with ThreadPoolExecutor(max_workers=1) as executor:
            future = executor.submit(competing_gc)
            wait_for_lock("k2-gc-attach-first")
            attach.commit()
            with pytest.raises(psycopg.errors.ForeignKeyViolation):
                future.result(timeout=10)
    assert _rows(
        repository,
        """SELECT r.blob_id, b.body
           FROM pulsara_v3.canonical_image_refs r
           JOIN pulsara_v3.blobs b ON b.id=r.blob_id
           WHERE r.context_snapshot_id=%s""",
        (attach_first_owner,),
    ) == [{"blob_id": attach_first_blob.blob_id, "body": attach_first_payload}]

    # GC now completes its DELETE but holds the transaction open. A stale
    # attach of that old blob identity blocks, then fails its whole owner write
    # after DELETE commits.
    delete_first_payload = _png() + b"delete-first"
    with psycopg.connect(runtime_dsn, row_factory=dict_row) as publication:
        delete_first_blob = PostgresCanonicalBlobStore.publish_in_connection(
            publication,
            workspace_id=workspace_id,
            content=delete_first_payload,
            media_type="image/png",
            codec="binary",
        )
    age(delete_first_blob.blob_id)
    gc_deleted = Event()
    allow_gc_commit = Event()
    delete_first_owner = _id("snapshot")

    def held_gc() -> tuple[str, ...]:
        with psycopg.connect(
            runtime_dsn,
            row_factory=dict_row,
            application_name="k2-gc-delete-first",
        ) as connection:
            deleted = _delete_orphans_in_connection(
                connection,
                grace_seconds=1,
                maximum_items=100,
            )
            assert delete_first_blob.blob_id in deleted
            gc_deleted.set()
            assert allow_gc_commit.wait(timeout=10)
            return deleted

    def stale_attach() -> None:
        with psycopg.connect(
            runtime_dsn,
            row_factory=dict_row,
            application_name="k2-stale-attach-delete-first",
        ) as connection:
            insert_snapshot(connection, delete_first_owner, "sha256:" + "5" * 64)
            insert_canonical_prompt_refs(
                connection,
                session_id=session_id,
                workspace_id=workspace_id,
                image_blob_ids=(delete_first_blob.blob_id,),
                context_snapshot_id=delete_first_owner,
            )

    with ThreadPoolExecutor(max_workers=2) as executor:
        gc_future = executor.submit(held_gc)
        assert gc_deleted.wait(timeout=10)
        attach_future = executor.submit(stale_attach)
        wait_for_lock("k2-stale-attach-delete-first")
        allow_gc_commit.set()
        assert delete_first_blob.blob_id in gc_future.result(timeout=10)
        with pytest.raises(psycopg.errors.ForeignKeyViolation):
            attach_future.result(timeout=10)
    assert _rows(
        repository,
        "SELECT 1 FROM pulsara_v3.context_snapshots WHERE id=%s",
        (delete_first_owner,),
    ) == []
    assert _rows(
        repository,
        "SELECT 1 FROM pulsara_v3.blobs WHERE id=%s",
        (delete_first_blob.blob_id,),
    ) == []


def test_scope_misbinding_and_extra_ref_fail_before_image_hydration(
    stage2_migrated_postgres_database,
) -> None:
    repository = _repository(stage2_migrated_postgres_database)
    lease, runtime, _binding = _bound_session(repository)
    image = _image()
    content = FrozenPromptContent((image,))
    intent, _accepted, _admission = _direct_intent(
        repository, lease, runtime, content
    )
    cut = repository.prepare_provider_input_cut(
        lease.guard,
        turn_id=intent.turn_id,
        deadline_monotonic=monotonic() + 30,
    )
    ref = _rows(
        repository,
        "SELECT workspace_id, blob_id FROM pulsara_v3.canonical_image_refs WHERE transcript_entry_id=%s",
        (intent.entry_id,),
    )[0]
    with psycopg.connect(stage2_migrated_postgres_database.admin_dsn) as admin:
        admin.execute(
            """INSERT INTO pulsara_v3.canonical_image_refs (
                 session_id, workspace_id, transcript_entry_id, ref_ordinal, blob_id
               ) VALUES (%s,%s,%s,1,%s)""",
            (lease.guard.session_id, ref["workspace_id"], intent.entry_id, ref["blob_id"]),
        )
    with pytest.raises(ConversationKernelConflict, match="refs do not exact-join"):
        CanonicalProviderInputReader(
            repository.connection_provider
        ).read_frozen_snapshot(cut, deadline_monotonic=monotonic() + 30)
    assert (
        repository.confirm_root_turn_intent(
            intent=intent,
            deadline_monotonic=monotonic() + 30,
        ).kind.value
        == "CONFLICT"
    )

    other_repository = _repository(stage2_migrated_postgres_database)
    other_lease, _other_runtime, _other_binding = _bound_session(other_repository)
    other_workspace = _rows(
        other_repository,
        "SELECT workspace_id FROM pulsara_v3.sessions WHERE id=%s",
        (other_lease.guard.session_id,),
    )[0]["workspace_id"]
    with pytest.raises(psycopg.errors.ForeignKeyViolation):
        with psycopg.connect(stage2_migrated_postgres_database.admin_dsn) as admin:
            admin.execute(
                """INSERT INTO pulsara_v3.canonical_image_refs (
                     session_id, workspace_id, transcript_entry_id, ref_ordinal, blob_id
                   ) VALUES (%s,%s,%s,2,%s)""",
                (
                    lease.guard.session_id,
                    other_workspace,
                    intent.entry_id,
                    ref["blob_id"],
                ),
            )
