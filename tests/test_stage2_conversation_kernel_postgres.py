from __future__ import annotations

from datetime import datetime, timedelta, timezone
from hashlib import sha256
from time import monotonic
from uuid import uuid4

import psycopg
import pytest

from pulsara_agent.llm.input import FrozenPromptContent
from pulsara_agent.conversation_kernel.prompt_content import freeze_canonical_prompt

from pulsara_agent.conversation_kernel.contracts import (
    HostWriterAcquisitionKind,
    InlineContent,
    PromptDeliveryMode,
)
from pulsara_agent.conversation_kernel.activation import (
    require_stage2_runtime_privilege_boundary,
)
from pulsara_agent.conversation_kernel.blob import (
    MAXIMUM_BLOB_BYTES,
    MAXIMUM_CONTENT_CHUNK_BYTES,
    PostgresCanonicalBlobStore,
)
from pulsara_agent.conversation_kernel.memory import PostgresMemoryQuery
from pulsara_agent.conversation_kernel.memory.contracts import (
    FrozenMemoryProposal,
    MemoryKindHint,
    prepare_memory_candidate,
)
from tests.support.round3 import new_test_provider_input_continuity_owner
from pulsara_agent.conversation_kernel.compaction.contracts import (
    COMPACTION_MODEL_CONTRACT,
    COMPACTION_SNAPSHOT_COMPILER_CONTRACT,
    COMPACTION_SUMMARY_PROMPT_CONTRACT,
    CONTEXT_SNAPSHOT_CODEC,
    CONTEXT_SNAPSHOT_MEDIA_TYPE,
    CompactionActiveRequestLocation,
    CompactionCanonicalAdoptionFactoryInput,
    CompactionCanonicalWritePreconditions,
    CompactionConfirmationKind,
    CompactionContinuationMode,
    CompactionScope,
    CompactionTargetBranch,
    ExpectedCompactionPredecessorRevision,
    FrozenCompactionActiveRequest,
    build_prepared_manual_compaction_command,
    build_prepared_compaction_canonical_adoption,
    canonical_compaction_range_digest,
    freeze_compaction_canonical_range,
)
from pulsara_agent.conversation_kernel.compaction.prompt import (
    build_compaction_snapshot_carrier,
    freeze_compaction_summary_output,
)
from pulsara_agent.conversation_kernel.reader import CanonicalProviderInputReader
from pulsara_agent.terminal_protocol.canonical_v3 import CanonicalProtocolReader
from pulsara_agent.conversation_kernel.repository import (
    AssistantTextBlock,
    AssistantToolCallBlock,
    ConversationKernelConflict,
    ConversationKernelRepository,
    StaleHostWriter,
    ToolRemoteIdentityConfirmationKind,
    build_prepared_root_turn_intent,
    build_prepared_tool_remote_identity_publication,
    build_prepared_tool_result_acceptance,
)
from pulsara_agent.llm.model_connections import (
    ModelCallBinding,
    ModelConnectionId,
    model_call_binding_from_dict,
)
from pulsara_agent.llm.model_target import FrozenModelResolutionSnapshot
from pulsara_agent.conversation_kernel.steer import (
    PreparedRootProviderInputAdmission,
    PromptIngressConfirmationKind,
    QueuedRootTurnAdmissionConfirmationKind,
    SteerConsumptionConfirmationKind,
    SteerResourceRejectionConfirmationKind,
    build_prompt_ingress_command,
    build_steer_canonical_base_fence,
    build_steer_consumption_candidate,
    build_steer_resource_rejection,
)
from pulsara_agent.ports.artifact import (
    ToolOutputArtifactDisposition,
    ToolResultDisplayKind,
)
from pulsara_agent.ports.tool_execution import ToolOutputSourceCoverage
from pulsara_agent.primitives.context import freeze_json
from pulsara_agent.model_input.continuity import (
    FULL_HISTORY_CONTEXT_BASE_IDENTITY,
    NoNewTriggerAnchor,
    ProcessLocalCanonicalFrontier,
    ProviderInputContinuityScope,
)
from pulsara_agent.model_input.contracts import (
    CanonicalInputOriginKind,
    ContextBindingBaseKind,
    FrozenProviderInputItemKind,
    ModelInputScopeKind,
    PreparedProviderInputCut,
    provider_input_item_text,
)
from pulsara_agent.primitives.permission import DEFAULT_PERMISSION_MODE
from pulsara_agent.primitives.tool_observation import ToolObservationOrigin
from pulsara_agent.memory.scope import CTX_GLOBAL
from pulsara_agent.conversation_kernel.vocabulary import (
    APPEND_GUARDS,
    COMMITTED_EVENT_DESCRIPTORS,
    LIVE_EVENT_TYPES,
    SUBJECT_SLOTS,
)
from pulsara_agent.storage.migrations.manifest import CONVERSATION_KERNEL_RELATIONS
from pulsara_agent.storage.postgres_connection_provider import PostgresConnectionLane
from tests.support.postgres import verified_postgres_provider
from tests.support.model_config import (
    enqueue_test_prompt,
    start_test_root_turn,
    test_model_binding,
    test_model_runtime,
)


pytestmark = pytest.mark.postgres


def _root_provider_input_admission(candidate) -> PreparedRootProviderInputAdmission:
    admission = object.__new__(PreparedRootProviderInputAdmission)
    object.__setattr__(admission, "candidate", candidate)
    return admission


def _two_connection_resolution_cut():
    first_id = ModelConnectionId("model-connection:" + "1" * 32)
    second_id = ModelConnectionId("model-connection:" + "2" * 32)
    first = test_model_runtime(connection_id=first_id, model_id="model-first")
    second = test_model_runtime(connection_id=second_id, model_id="model-second")
    first_cut = first.freeze_resolution_snapshot()
    second_cut = second.freeze_resolution_snapshot()
    return (
        FrozenModelResolutionSnapshot(
            {**first_cut.resolved, **second_cut.resolved},
            {**first_cut.unavailable, **second_cut.unavailable},
        ),
        ModelCallBinding(first_id, None),
        ModelCallBinding(second_id, None),
    )


def _enqueue_binding_candidate(
    repository,
    guard,
    *,
    cut,
    command_id: str,
    queue_item_id: str,
    text: bytes,
):
    permission_snapshot_id = _name("permission")
    permission = repository.prepare_root_permission_snapshot(
        guard,
        snapshot_id=permission_snapshot_id,
        requested_mode=DEFAULT_PERMISSION_MODE,
        deadline_monotonic=monotonic() + 30,
    )
    candidate = build_prompt_ingress_command(
        session_id=guard.session_id,
        command_id=command_id,
        queue_item_id=queue_item_id,
        client_submission_id=_name("submission"),
        delivery_mode=PromptDeliveryMode.NEW_TURN,
        target_turn_id=None,
        permission_snapshot_id=permission_snapshot_id,
        requested_permission_mode=DEFAULT_PERMISSION_MODE,
        canonical_prompt=freeze_canonical_prompt(
            FrozenPromptContent.text(text.decode("utf-8"))
        ),
    )
    accepted = repository.enqueue_prompt(
        guard,
        candidate=candidate,
        model_resolution_snapshot=cut,
        occurred_at=datetime.now(timezone.utc),
        actor_id="test",
        deadline_monotonic=monotonic() + 30,
        _expected_permission_snapshot=permission,
    )
    return candidate, accepted


def test_model_binding_freezes_per_queue_command_and_idempotent_retry(
    stage2_migrated_postgres_database,
) -> None:
    repository = _repository(stage2_migrated_postgres_database)
    lease = repository.acquire_host_writer(
        session_id=_name("session"),
        workspace_id=_name("workspace"),
        writer_owner_id=_name("host"),
        lease_seconds=30,
        deadline_monotonic=monotonic() + 30,
    )
    cut, first_binding, second_binding = _two_connection_resolution_cut()
    repository.update_session_model_call_binding(
        lease.guard,
        binding=first_binding,
        deadline_monotonic=monotonic() + 30,
    )
    first, first_accepted = _enqueue_binding_candidate(
        repository,
        lease.guard,
        cut=cut,
        command_id=_name("command"),
        queue_item_id=_name("queue"),
        text=b"first binding",
    )
    repository.update_session_model_call_binding(
        lease.guard,
        binding=second_binding,
        deadline_monotonic=monotonic() + 30,
    )
    second, second_accepted = _enqueue_binding_candidate(
        repository,
        lease.guard,
        cut=cut,
        command_id=_name("command"),
        queue_item_id=_name("queue"),
        text=b"second binding",
    )

    assert first_accepted.model_call_binding == first_binding
    assert second_accepted.model_call_binding == second_binding
    replay_permission = repository.prepare_root_permission_snapshot(
        lease.guard,
        snapshot_id=first.permission_snapshot_id,
        requested_mode=DEFAULT_PERMISSION_MODE,
        deadline_monotonic=monotonic() + 30,
    )
    assert (
        repository.enqueue_prompt(
            lease.guard,
            candidate=first,
            model_resolution_snapshot=cut,
            occurred_at=datetime.now(timezone.utc),
            actor_id="test",
            deadline_monotonic=monotonic() + 30,
            _expected_permission_snapshot=replay_permission,
        ).model_call_binding
        == first_binding
    )
    assert (
        repository.confirm_prompt_ingress(
            candidate=first,
            deadline_monotonic=monotonic() + 30,
        ).kind
        is PromptIngressConfirmationKind.FULL_COMPATIBLE
    )
    with repository.connection_provider.connection(
        lane=PostgresConnectionLane.INSPECTOR,
        deadline_monotonic=monotonic() + 30,
    ) as connection:
        rows = connection.execute(
            "SELECT id, model_call_binding FROM pulsara_v3.prompt_queue_items "
            "WHERE session_id=%s ORDER BY queue_sequence",
            (lease.guard.session_id,),
        ).fetchall()
    assert [model_call_binding_from_dict(row[1]) for row in rows] == [
        first_binding,
        second_binding,
    ]
    assert (
        repository.read_session_model_call_binding(
            lease.guard,
            deadline_monotonic=monotonic() + 30,
        )
        == second_binding
    )


def test_direct_root_retry_confirms_turn_binding_not_later_session_choice(
    stage2_migrated_postgres_database,
) -> None:
    repository = _repository(stage2_migrated_postgres_database)
    lease = repository.acquire_host_writer(
        session_id=_name("session"),
        workspace_id=_name("workspace"),
        writer_owner_id=_name("host"),
        lease_seconds=30,
        deadline_monotonic=monotonic() + 30,
    )
    cut, first_binding, second_binding = _two_connection_resolution_cut()
    repository.update_session_model_call_binding(
        lease.guard,
        binding=first_binding,
        deadline_monotonic=monotonic() + 30,
    )
    intent = build_prepared_root_turn_intent(
        session_id=lease.guard.session_id,
        command_id=_name("command"),
        turn_id=_name("turn"),
        entry_id=_name("entry"),
        context_binding_revision_id=_name("context"),
        permission_snapshot_id=_name("permission"),
        requested_permission_mode=DEFAULT_PERMISSION_MODE,
        canonical_prompt=freeze_canonical_prompt(
            FrozenPromptContent.text("direct binding")
        ),
        occurred_at=datetime.now(timezone.utc),
    )
    provider_candidate = repository.prepare_root_provider_input_candidate(
        lease.guard,
        intent=intent,
        model_resolution_snapshot=cut,
        deadline_monotonic=monotonic() + 30,
    )
    provider_admission = _root_provider_input_admission(provider_candidate)
    accepted = repository.accept_root_turn_intent(
        lease.guard,
        intent=intent,
        provider_input_admission=provider_admission,
        model_resolution_snapshot=cut,
        deadline_monotonic=monotonic() + 30,
    )
    repository.update_session_model_call_binding(
        lease.guard,
        binding=second_binding,
        deadline_monotonic=monotonic() + 30,
    )
    confirmation = repository.confirm_root_turn_intent(
        intent=intent,
        guard=lease.guard,
        deadline_monotonic=monotonic() + 30,
    )
    assert confirmation.kind.value == "FULL"
    assert (
        repository.accept_root_turn_intent(
            lease.guard,
            intent=intent,
            provider_input_admission=provider_admission,
            model_resolution_snapshot=cut,
            deadline_monotonic=monotonic() + 30,
        ).accepted
        == accepted.accepted
    )
    assert (
        repository.read_turn_model_call_binding(
            lease.guard,
            turn_id=intent.turn_id,
            deadline_monotonic=monotonic() + 30,
        )
        == first_binding
    )
    assert (
        repository.read_session_model_call_binding(
            lease.guard,
            deadline_monotonic=monotonic() + 30,
        )
        == second_binding
    )


def test_permanently_invalid_queued_binding_rejects_without_turn(
    stage2_migrated_postgres_database,
) -> None:
    repository = _repository(stage2_migrated_postgres_database)
    lease = repository.acquire_host_writer(
        session_id=_name("session"),
        workspace_id=_name("workspace"),
        writer_owner_id=_name("host"),
        lease_seconds=30,
        deadline_monotonic=monotonic() + 30,
    )
    cut, first_binding, _second_binding = _two_connection_resolution_cut()
    repository.update_session_model_call_binding(
        lease.guard,
        binding=first_binding,
        deadline_monotonic=monotonic() + 30,
    )
    _command, _accepted = _enqueue_binding_candidate(
        repository,
        lease.guard,
        cut=cut,
        command_id=_name("command"),
        queue_item_id=_name("queue"),
        text=b"removed target",
    )
    candidate = repository.prepare_prompt_head_consumption(
        session_id=lease.guard.session_id,
        occurred_at=datetime.now(timezone.utc),
        actor_id="host:test",
        deadline_monotonic=monotonic() + 30,
    )
    assert candidate is not None
    unavailable = FrozenModelResolutionSnapshot(
        {}, {first_binding.connection_id: "catalog target was removed"}
    )
    with pytest.raises(ValueError):
        unavailable.validate(candidate.model_call_binding)
    assert repository.reject_prepared_prompt_head_model_unavailable(
        lease.guard,
        candidate=candidate,
        deadline_monotonic=monotonic() + 30,
    )
    assert repository.reject_prepared_prompt_head_model_unavailable(
        lease.guard,
        candidate=candidate,
        deadline_monotonic=monotonic() + 30,
    )
    with repository.connection_provider.connection(
        lane=PostgresConnectionLane.INSPECTOR,
        deadline_monotonic=monotonic() + 30,
    ) as connection:
        queue = connection.execute(
            "SELECT status, terminal_reason FROM pulsara_v3.prompt_queue_items "
            "WHERE id=%s",
            (candidate.queue_item_id,),
        ).fetchone()
        turn = connection.execute(
            "SELECT 1 FROM pulsara_v3.turns WHERE id=%s",
            (candidate.exact_turn_id,),
        ).fetchone()
    assert queue == (
        "REJECTED",
        "MODEL_CONFIGURATION_UNAVAILABLE_BEFORE_DELIVERY",
    )
    assert turn is None


def test_lightweight_todo_queued_root_admission_has_exact_confirmation(
    stage2_migrated_postgres_database,
) -> None:
    provider = verified_postgres_provider(stage2_migrated_postgres_database.runtime_dsn)
    repository = ConversationKernelRepository(provider)
    session_id = f"session:{uuid4().hex}"
    workspace_id = f"workspace:{uuid4().hex}"
    lease = repository.acquire_host_writer(
        session_id=session_id,
        workspace_id=workspace_id,
        writer_owner_id=f"host:{uuid4().hex}",
        lease_seconds=30,
        deadline_monotonic=monotonic() + 30,
    )
    queue_item_id = f"queue:{uuid4().hex}"
    command_id = f"command:{uuid4().hex}"
    model_call_binding = test_model_binding(test_model_runtime())
    repository.update_session_model_call_binding(
        lease.guard,
        binding=model_call_binding,
        deadline_monotonic=monotonic() + 30,
    )
    enqueue_test_prompt(
        repository,
        lease.guard,
        command_id=command_id,
        queue_item_id=queue_item_id,
        client_submission_id=f"submission:{uuid4().hex}",
        delivery_mode=PromptDeliveryMode.NEW_TURN,
        target_turn_id=None,
        permission_snapshot_id=f"permission:{uuid4().hex}",
        requested_permission_mode=DEFAULT_PERMISSION_MODE,
        model_call_binding=model_call_binding,
        content=FrozenPromptContent.text('queued TODO run'),
        occurred_at=datetime.now(timezone.utc),
        actor_id="test",
        deadline_monotonic=monotonic() + 30,
    )
    candidate = repository.prepare_prompt_head_consumption(
        session_id=session_id,
        occurred_at=datetime.now(timezone.utc),
        actor_id="host:test",
        deadline_monotonic=monotonic() + 30,
    )
    assert candidate is not None and candidate.queue_item_id == queue_item_id
    before = repository.confirm_prepared_prompt_head_consumption(
        candidate=candidate,
        deadline_monotonic=monotonic() + 30,
    )
    assert before.kind is QueuedRootTurnAdmissionConfirmationKind.NONE
    consumed = repository.consume_prepared_prompt_head(
        lease.guard,
        candidate=candidate,
        provider_input_admission=_root_provider_input_admission(
            candidate.provider_input_candidate
        ),
        deadline_monotonic=monotonic() + 30,
    )
    assert consumed is not None
    assert consumed.kind is QueuedRootTurnAdmissionConfirmationKind.FULL
    confirmed = repository.confirm_prepared_prompt_head_consumption(
        candidate=candidate,
        deadline_monotonic=monotonic() + 30,
    )
    assert confirmed == consumed
    assert confirmed.accepted is not None
    assert confirmed.accepted.turn_id == candidate.exact_turn_id


def _name(prefix: str) -> str:
    return f"{prefix}:{uuid4().hex}"


def _snapshot_content(carrier) -> InlineContent:
    return InlineContent.from_bytes(
        carrier.body,
        media_type=CONTEXT_SNAPSHOT_MEDIA_TYPE,
        codec=CONTEXT_SNAPSHOT_CODEC,
    )


def _repository(stage2_migrated_postgres_database) -> ConversationKernelRepository:
    return ConversationKernelRepository(
        verified_postgres_provider(stage2_migrated_postgres_database.runtime_dsn)
    )


class _FailingSteerRejectionRepository(ConversationKernelRepository):
    fail_event_append = False

    def _append_events(self, *args, **kwargs):
        if self.fail_event_append:
            raise RuntimeError("injected event append failure")
        return super()._append_events(*args, **kwargs)


def _start_root_turn(repository: ConversationKernelRepository, *args, **kwargs):
    """Retained Stage 2 fixture expressed through the Round 4 admission API."""

    model_call_binding = test_model_binding(test_model_runtime())
    repository.update_session_model_call_binding(
        args[0],
        binding=model_call_binding,
        deadline_monotonic=kwargs["deadline_monotonic"],
    )
    kwargs.setdefault("permission_snapshot_id", _name("permission-snapshot"))
    kwargs.setdefault("requested_permission_mode", DEFAULT_PERMISSION_MODE)
    kwargs.setdefault("model_call_binding", model_call_binding)
    return start_test_root_turn(repository, *args, **kwargs)


def _enqueue_prompt(repository: ConversationKernelRepository, *args, **kwargs):
    if kwargs["delivery_mode"] is PromptDeliveryMode.NEW_TURN:
        model_call_binding = test_model_binding(test_model_runtime())
        repository.update_session_model_call_binding(
            args[0],
            binding=model_call_binding,
            deadline_monotonic=kwargs["deadline_monotonic"],
        )
        kwargs.setdefault("permission_snapshot_id", _name("permission-snapshot"))
        kwargs.setdefault("requested_permission_mode", DEFAULT_PERMISSION_MODE)
        kwargs.setdefault("model_call_binding", model_call_binding)
    else:
        kwargs.setdefault("permission_snapshot_id", None)
        kwargs.setdefault("requested_permission_mode", None)
        kwargs.setdefault("model_call_binding", None)
    return enqueue_test_prompt(repository, *args, **kwargs)


def _assistant_permission_fingerprint(
    repository: ConversationKernelRepository,
    guard,
    assistant_entry_id: str,
    deadline: float,
) -> str:
    with repository.connection_provider.connection(
        lane=PostgresConnectionLane.INSPECTOR,
        deadline_monotonic=deadline,
    ) as connection:
        row = connection.execute(
            """
            SELECT t.permission_snapshot_fingerprint
            FROM pulsara_v3.transcript_entries AS e
            JOIN pulsara_v3.turns AS t
              ON t.session_id = e.session_id AND t.id = e.turn_id
            WHERE e.session_id = %s AND e.id = %s
            """,
            (guard.session_id, assistant_entry_id),
        ).fetchone()
    assert row is not None
    return str(row[0])


def _accept_tool_attempt(repository, guard, *args, **kwargs):
    kwargs.setdefault(
        "permission_snapshot_fingerprint",
        _assistant_permission_fingerprint(
            repository,
            guard,
            kwargs["assistant_entry_id"],
            kwargs["deadline_monotonic"],
        ),
    )
    return repository.accept_tool_attempt(guard, *args, **kwargs)


def _accept_tool_interaction_decision(repository, guard, *args, **kwargs):
    kwargs.setdefault(
        "permission_snapshot_fingerprint",
        _assistant_permission_fingerprint(
            repository,
            guard,
            kwargs["assistant_entry_id"],
            kwargs["deadline_monotonic"],
        ),
    )
    return repository.accept_tool_interaction_decision(guard, *args, **kwargs)


def test_stage2_orphan_blob_gc_deletes_only_unreferenced_content_after_grace(
    stage2_migrated_postgres_database,
) -> None:
    provider = verified_postgres_provider(stage2_migrated_postgres_database.runtime_dsn)
    repository = ConversationKernelRepository(provider)
    session_id = _name("session")
    workspace_id = _name("workspace")
    lease = repository.acquire_host_writer(
        session_id=session_id,
        workspace_id=workspace_id,
        writer_owner_id=_name("host"),
        lease_seconds=30,
        deadline_monotonic=monotonic() + 30,
    )
    store = PostgresCanonicalBlobStore(provider)
    orphan = store.publish(
        workspace_id=workspace_id,
        content=b"orphan" * 20_000,
        media_type="application/octet-stream",
        codec="binary",
        deadline_monotonic=monotonic() + 30,
    )
    prompt = FrozenPromptContent.text("referenced" * 20_000)
    canonical_prompt = freeze_canonical_prompt(prompt)
    entry_id = _name("entry")
    _start_root_turn(
        repository,
        lease.guard,
        command_id=_name("command"),
        turn_id=_name("turn"),
        entry_id=entry_id,
        context_binding_revision_id=_name("revision"),
        content=prompt,
        occurred_at=datetime.now(timezone.utc),
        deadline_monotonic=monotonic() + 30,
    )
    with psycopg.connect(stage2_migrated_postgres_database.admin_dsn) as connection:
        referenced = connection.execute(
            "SELECT blob_id, content_digest, content_size "
            "FROM pulsara_v3.transcript_entries WHERE id = %s",
            (entry_id,),
        ).fetchone()
        assert referenced is not None and referenced[0] is not None
        referenced_blob_id = str(referenced[0])
        # Test-only clock aging: production never mutates immutable blob
        # metadata.  The admin fixture temporarily disables the generic
        # runtime-write trigger in the same transaction and restores it
        # before commit.
        connection.execute("ALTER TABLE pulsara_v3.blobs DISABLE TRIGGER USER")
        connection.execute(
            """
            UPDATE pulsara_v3.blobs
            SET created_at = clock_timestamp() - interval '25 hours'
            WHERE id = ANY(%s)
            """,
            ([orphan.blob_id, referenced_blob_id],),
        )
        connection.execute("ALTER TABLE pulsara_v3.blobs ENABLE TRIGGER USER")
    deleted = store.delete_orphans(
        grace_seconds=24 * 60 * 60,
        maximum_items=128,
        deadline_monotonic=monotonic() + 30,
    )
    assert deleted == (orphan.blob_id,)
    with pytest.raises(KeyError):
        store.read_exact(
            blob_id=orphan.blob_id,
            expected_digest=orphan.digest,
            expected_size=orphan.size,
            deadline_monotonic=monotonic() + 30,
        )
    assert (
        store.read_exact(
            blob_id=referenced_blob_id,
            expected_digest="sha256:" + sha256(canonical_prompt.body).hexdigest(),
            expected_size=len(canonical_prompt.body),
            deadline_monotonic=monotonic() + 30,
        )
        == canonical_prompt.body
    )


def test_stage2_blob_chunk_exactly_joins_complete_canonical_descriptor(
    stage2_migrated_postgres_database,
) -> None:
    provider = verified_postgres_provider(stage2_migrated_postgres_database.runtime_dsn)
    store = PostgresCanonicalBlobStore(provider)
    content = b"descriptor-bound-content"
    reference = store.publish(
        workspace_id=_name("workspace"),
        content=content,
        media_type="text/plain",
        codec="utf-8",
        deadline_monotonic=monotonic() + 30,
    )
    with psycopg.connect(stage2_migrated_postgres_database.admin_dsn) as connection:
        connection.execute(
            "UPDATE pulsara_v3.blobs SET media_type = 'application/octet-stream' "
            "WHERE id = %s",
            (reference.blob_id,),
        )
        connection.commit()
    with pytest.raises(ConversationKernelConflict):
        store.read_chunk(
            blob_id=reference.blob_id,
            expected_digest=reference.digest,
            expected_size=reference.size,
            expected_media_type=reference.media_type,
            expected_codec=reference.codec,
            offset=0,
            maximum_bytes=1024,
            deadline_monotonic=monotonic() + 30,
        )


def test_stage2_maximum_blob_is_read_as_exact_bounded_storage_ranges(
    stage2_migrated_postgres_database,
) -> None:
    provider = verified_postgres_provider(stage2_migrated_postgres_database.runtime_dsn)
    store = PostgresCanonicalBlobStore(provider)
    content = b"0123456789abcdef" * (MAXIMUM_BLOB_BYTES // 16)
    reference = store.publish(
        workspace_id=_name("workspace"),
        content=content,
        media_type="text/plain",
        codec="utf-8",
        deadline_monotonic=monotonic() + 30,
    )
    offsets = (0, MAXIMUM_CONTENT_CHUNK_BYTES, len(content) - 97, len(content))
    for offset in offsets:
        chunk = store.read_chunk(
            blob_id=reference.blob_id,
            expected_digest=reference.digest,
            expected_size=reference.size,
            expected_media_type=reference.media_type,
            expected_codec=reference.codec,
            offset=offset,
            maximum_bytes=MAXIMUM_CONTENT_CHUNK_BYTES,
            deadline_monotonic=monotonic() + 30,
        )
        assert chunk.content == content[offset : offset + MAXIMUM_CONTENT_CHUNK_BYTES]
        assert chunk.has_more is (offset + len(chunk.content) < len(content))


def test_stage2_schema_and_descriptor_oracles_are_exact(
    stage2_migrated_postgres_database,
) -> None:
    # Fork spec §7.6 adds groups, genesis, and irreducible historical closures.
    assert len(CONVERSATION_KERNEL_RELATIONS) == 29
    assert len(set(CONVERSATION_KERNEL_RELATIONS)) == 29
    assert len(COMMITTED_EVENT_DESCRIPTORS) == 30
    assert len(LIVE_EVENT_TYPES) == 24
    assert len(SUBJECT_SLOTS) == 11
    assert len(APPEND_GUARDS) == 1

    with psycopg.connect(stage2_migrated_postgres_database.admin_dsn) as connection:
        observed = tuple(
            row[0]
            for row in connection.execute(
                """
                SELECT c.relname
                FROM pg_catalog.pg_class c
                JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
                WHERE n.nspname = 'pulsara_v3' AND c.relkind = 'r'
                ORDER BY c.relname
                """
            ).fetchall()
        )
        assert observed == tuple(sorted(CONVERSATION_KERNEL_RELATIONS))
        assert connection.execute(
            "SELECT to_regclass('public.sessions'), to_regclass('pulsara_v3.sessions')"
        ).fetchone() == (None, "pulsara_v3.sessions")


def test_stage2_snapshot_and_history_page_are_bounded_by_final_wire_bytes(
    stage2_migrated_postgres_database,
) -> None:
    repository = _repository(stage2_migrated_postgres_database)
    lease = repository.acquire_host_writer(
        session_id=_name("session"),
        workspace_id=_name("workspace"),
        writer_owner_id=_name("host"),
        lease_seconds=30,
        deadline_monotonic=monotonic() + 30,
    )
    # Leave room for the typed canonical body envelope so the entry remains
    # inline and this gate still exercises the final protocol wire bound.
    payload = b"x" * (60 * 1024)
    for _ in range(6):
        turn_id = _name("turn")
        _start_root_turn(
            repository,
            lease.guard,
            command_id=_name("command"),
            turn_id=turn_id,
            entry_id=_name("entry"),
            context_binding_revision_id=_name("revision"),
            content=FrozenPromptContent.text(payload.decode("utf-8")),
            occurred_at=datetime.now(timezone.utc),
            deadline_monotonic=monotonic() + 30,
        )
        repository.interrupt_turn(
            lease.guard,
            turn_id=turn_id,
            reason="TEST_BOUNDARY",
            occurred_at=datetime.now(timezone.utc),
            actor_id="test",
            deadline_monotonic=monotonic() + 30,
        )
    reader = CanonicalProtocolReader(repository.connection_provider)
    snapshot = reader.snapshot(
        session_id=lease.guard.session_id,
        maximum_entries=256,
        maximum_control_items=128,
        maximum_serialized_bytes=140_000,
        deadline_monotonic=monotonic() + 30,
    )
    assert 0 < len(snapshot.entries) < 6
    assert snapshot.control.latest_root_turn.turn_id == turn_id
    assert snapshot.control.latest_root_turn.status == "INTERRUPTED"
    assert snapshot.control.latest_root_turn.terminal_reason == "TEST_BOUNDARY"
    assert not snapshot.control.active_turns
    assert len(snapshot.SerializeToString(deterministic=True)) <= 140_000
    assert snapshot.HasField("older_history_cursor")
    entries, cursor, has_more = reader.history_page(
        session_id=lease.guard.session_id,
        cut_sequence=snapshot.older_history_cursor.cut_sequence,
        before_entry_sequence=snapshot.older_history_cursor.entry_sequence,
        maximum_entries=256,
        maximum_serialized_bytes=140_000,
        deadline_monotonic=monotonic() + 30,
    )
    assert entries
    assert (
        sum(len(item.SerializeToString(deterministic=True)) + 8 for item in entries)
        + 512
        <= 140_000
    )
    assert has_more == (cursor is not None)


def test_stage2_text_turn_is_canonical_and_sequences_rollback_without_gaps(
    stage2_migrated_postgres_database,
) -> None:
    repository = _repository(stage2_migrated_postgres_database)
    deadline = monotonic() + 30
    session_id = _name("session")
    workspace_id = _name("workspace")
    lease = repository.acquire_host_writer(
        session_id=session_id,
        workspace_id=workspace_id,
        writer_owner_id=_name("host"),
        lease_seconds=30,
        deadline_monotonic=deadline,
    )
    turn_id = _name("turn")
    _start_root_turn(
        repository,
        lease.guard,
        command_id=_name("command"),
        turn_id=turn_id,
        entry_id=_name("entry"),
        context_binding_revision_id=_name("revision"),
        content=FrozenPromptContent.text('hello'),
        occurred_at=datetime.now(timezone.utc),
        deadline_monotonic=deadline,
    )
    cut = repository.prepare_provider_input_cut(
        lease.guard, turn_id=turn_id, deadline_monotonic=deadline
    )
    duplicate_block_id = _name("block")
    with pytest.raises(Exception):
        repository.commit_assistant_message(
            lease.guard,
            cut=cut,
            entry_id=_name("entry"),
            parent_content=InlineContent.from_bytes(b"bad"),
            blocks=(
                AssistantTextBlock(
                    duplicate_block_id, InlineContent.from_bytes(b"one")
                ),
                AssistantTextBlock(
                    duplicate_block_id, InlineContent.from_bytes(b"two")
                ),
            ),
            occurred_at=datetime.now(timezone.utc),
            actor_id="model:test",
            deadline_monotonic=deadline,
        )
    accepted = repository.commit_assistant_message(
        lease.guard,
        cut=cut,
        entry_id=_name("entry"),
        parent_content=InlineContent.from_bytes(b"ok"),
        blocks=(AssistantTextBlock(_name("block"), InlineContent.from_bytes(b"ok")),),
        occurred_at=datetime.now(timezone.utc),
        actor_id="model:test",
        deadline_monotonic=deadline,
    )
    assert accepted.entry_sequence == 2
    assert accepted.event_sequence == 2


def test_stage2_stale_writer_cannot_mutate_after_takeover(
    stage2_migrated_postgres_database,
) -> None:
    repository = _repository(stage2_migrated_postgres_database)
    deadline = monotonic() + 30
    session_id = _name("session")
    workspace_id = _name("workspace")
    first = repository.acquire_host_writer(
        session_id=session_id,
        workspace_id=workspace_id,
        writer_owner_id=_name("host"),
        lease_seconds=30,
        deadline_monotonic=deadline,
    )
    second = repository.acquire_host_writer(
        session_id=session_id,
        workspace_id=workspace_id,
        writer_owner_id=_name("host"),
        lease_seconds=30,
        deadline_monotonic=deadline,
    )
    assert first.acquisition_kind is HostWriterAcquisitionKind.NEW_SESSION
    assert second.acquisition_kind is HostWriterAcquisitionKind.HOST_TAKEOVER
    assert second.guard.writer_generation == first.guard.writer_generation + 1
    with pytest.raises(StaleHostWriter):
        _start_root_turn(
            repository,
            first.guard,
            command_id=_name("command"),
            turn_id=_name("turn"),
            entry_id=_name("entry"),
            context_binding_revision_id=_name("revision"),
            content=FrozenPromptContent.text('stale'),
            occurred_at=datetime.now(timezone.utc),
            deadline_monotonic=deadline,
        )


def test_round9_host_writer_renewal_accepts_unconstrained_memory_domain(
    stage2_migrated_postgres_database,
) -> None:
    repository = _repository(stage2_migrated_postgres_database)
    deadline = monotonic() + 30
    lease = repository.acquire_host_writer(
        session_id=_name("session"),
        workspace_id=_name("workspace"),
        writer_owner_id=_name("host"),
        lease_seconds=30,
        deadline_monotonic=deadline,
    )

    renewed = repository.renew_host_writer(
        lease.guard,
        lease_seconds=30,
        memory_domain_id=None,
        deadline_monotonic=deadline,
    )

    assert renewed.guard == lease.guard
    assert renewed.expires_at > lease.expires_at


def test_stage2_host_takeover_rejects_pending_exact_turn_steer(
    stage2_migrated_postgres_database,
) -> None:
    repository = _repository(stage2_migrated_postgres_database)
    deadline = monotonic() + 30
    session_id = _name("session")
    workspace_id = _name("workspace")
    first = repository.acquire_host_writer(
        session_id=session_id,
        workspace_id=workspace_id,
        writer_owner_id=_name("host"),
        lease_seconds=30,
        deadline_monotonic=deadline,
    )
    turn_id = _name("turn")
    _start_root_turn(
        repository,
        first.guard,
        command_id=_name("command"),
        turn_id=turn_id,
        entry_id=_name("entry"),
        context_binding_revision_id=_name("revision"),
        content=FrozenPromptContent.text('initial'),
        occurred_at=datetime.now(timezone.utc),
        deadline_monotonic=deadline,
    )
    queue_item_id = _name("queue")
    _enqueue_prompt(
        repository,
        first.guard,
        command_id=_name("command"),
        queue_item_id=queue_item_id,
        client_submission_id=_name("client"),
        delivery_mode=PromptDeliveryMode.STEER_ACTIVE_TURN,
        target_turn_id=turn_id,
        content=FrozenPromptContent.text('new direction'),
        occurred_at=datetime.now(timezone.utc),
        actor_id="user",
        deadline_monotonic=deadline,
    )

    second = repository.acquire_host_writer(
        session_id=session_id,
        workspace_id=workspace_id,
        writer_owner_id=_name("host"),
        lease_seconds=30,
        deadline_monotonic=deadline,
    )
    assert second.guard.writer_generation == first.guard.writer_generation + 1
    with verified_postgres_provider(
        stage2_migrated_postgres_database.runtime_dsn
    ).connection(
        lane=PostgresConnectionLane.INSPECTOR,
        deadline_monotonic=deadline,
    ) as connection:
        assert connection.execute(
            """
            SELECT status, terminal_reason
            FROM pulsara_v3.prompt_queue_items
            WHERE session_id = %s AND id = %s
            """,
            (session_id, queue_item_id),
        ).fetchone() == ("REJECTED", "TARGET_TURN_INTERRUPTED")
        assert connection.execute(
            """
            SELECT event_type, subject_queue_item_id
            FROM pulsara_v3.agent_events
            WHERE session_id = %s AND subject_queue_item_id = %s
            ORDER BY event_sequence
            """,
            (session_id, queue_item_id),
        ).fetchall() == [
            ("PromptQueued", queue_item_id),
            ("PromptRejected", queue_item_id),
        ]


def test_stage2_tool_message_precedes_attempt_and_remote_identity_is_set_once(
    stage2_migrated_postgres_database,
) -> None:
    repository = _repository(stage2_migrated_postgres_database)
    deadline = monotonic() + 30
    session_id = _name("session")
    workspace_id = _name("workspace")
    lease = repository.acquire_host_writer(
        session_id=session_id,
        workspace_id=workspace_id,
        writer_owner_id=_name("host"),
        lease_seconds=30,
        deadline_monotonic=deadline,
    )
    turn_id = _name("turn")
    _start_root_turn(
        repository,
        lease.guard,
        command_id=_name("command"),
        turn_id=turn_id,
        entry_id=_name("entry"),
        context_binding_revision_id=_name("revision"),
        content=FrozenPromptContent.text('use tool'),
        occurred_at=datetime.now(timezone.utc),
        deadline_monotonic=deadline,
    )
    cut = repository.prepare_provider_input_cut(
        lease.guard, turn_id=turn_id, deadline_monotonic=deadline
    )
    request_entry_id = _name("entry")
    call_id = _name("call")
    repository.commit_assistant_message(
        lease.guard,
        cut=cut,
        entry_id=request_entry_id,
        parent_content=InlineContent.from_bytes(b"tool request"),
        blocks=(
            AssistantToolCallBlock(
                block_id=_name("block"),
                tool_call_id=call_id,
                tool_name="terminal",
                arguments=freeze_json({"command": "true"}),
            ),
        ),
        occurred_at=datetime.now(timezone.utc),
        actor_id="model:test",
        deadline_monotonic=deadline,
    )
    attempt = _accept_tool_attempt(
        repository,
        lease.guard,
        attempt_id=_name("attempt"),
        assistant_entry_id=request_entry_id,
        tool_call_id=call_id,
        authorization_kind="policy",
        authorization_reference="trusted",
        actor_kind="runtime",
        actor_id="tool-executor",
        remote_idempotency_key=None,
        retry_of_attempt_id=None,
        occurred_at=datetime.now(timezone.utc),
        deadline_monotonic=deadline,
    )
    assert attempt.tool_call_id == call_id
    remote_identity = _name("process")
    remote_identity_candidate = build_prepared_tool_remote_identity_publication(
        session_id=session_id,
        attempt_id=attempt.attempt_id,
        remote_identity=remote_identity,
        occurred_at=datetime.now(timezone.utc),
        actor_id="tool-executor",
    )
    repository.publish_tool_remote_identity(
        lease.guard,
        candidate=remote_identity_candidate,
        deadline_monotonic=deadline,
    )
    # The set-once publication is idempotent for the compatible winner and
    # owns the previously missing committed occurrence.
    repository.publish_tool_remote_identity(
        lease.guard,
        candidate=remote_identity_candidate,
        deadline_monotonic=deadline,
    )
    assert (
        repository.confirm_tool_remote_identity(
            lease.guard,
            candidate=remote_identity_candidate,
            deadline_monotonic=deadline,
        )
        is ToolRemoteIdentityConfirmationKind.FULL
    )
    with repository.connection_provider.connection(
        lane=PostgresConnectionLane.INSPECTOR,
        deadline_monotonic=deadline,
    ) as connection:
        assert connection.execute(
            """
            SELECT remote_identity FROM pulsara_v3.tool_execution_attempts
            WHERE session_id = %s AND id = %s
            """,
            (session_id, attempt.attempt_id),
        ).fetchone() == (remote_identity,)
        assert connection.execute(
            """
            SELECT count(*) FROM pulsara_v3.agent_events
            WHERE session_id = %s
              AND event_type = 'ToolRemoteIdentityPublished'
              AND subject_tool_attempt_id = %s
            """,
            (session_id, attempt.attempt_id),
        ).fetchone() == (1,)
        occurrence_payload = connection.execute(
            """
            SELECT payload FROM pulsara_v3.agent_events
            WHERE session_id = %s
              AND event_type = 'ToolRemoteIdentityPublished'
              AND subject_tool_attempt_id = %s
            """,
            (session_id, attempt.attempt_id),
        ).fetchone()[0]
        assert remote_identity not in repr(occurrence_payload)
        assert occurrence_payload == {
            "remote_identity_utf8_bytes": len(remote_identity.encode("utf-8")),
            "remote_identity_digest": "sha256:"
            + sha256(remote_identity.encode("utf-8")).hexdigest(),
        }


@pytest.mark.parametrize("decision", ["ALLOW", "DENY"])
def test_stage2_human_tool_decision_atomically_installs_exact_effect_boundary(
    stage2_migrated_postgres_database,
    decision: str,
) -> None:
    repository = _repository(stage2_migrated_postgres_database)
    deadline = monotonic() + 30
    session_id = _name("session")
    workspace_id = _name("workspace")
    lease = repository.acquire_host_writer(
        session_id=session_id,
        workspace_id=workspace_id,
        writer_owner_id=_name("host"),
        lease_seconds=30,
        deadline_monotonic=deadline,
    )
    turn_id = _name("turn")
    _start_root_turn(
        repository,
        lease.guard,
        command_id=_name("command"),
        turn_id=turn_id,
        entry_id=_name("entry"),
        context_binding_revision_id=_name("revision"),
        content=FrozenPromptContent.text('use tool'),
        occurred_at=datetime.now(timezone.utc),
        deadline_monotonic=deadline,
    )
    cut = repository.prepare_provider_input_cut(
        lease.guard, turn_id=turn_id, deadline_monotonic=deadline
    )
    assistant_entry_id = _name("entry")
    tool_call_id = _name("call")
    repository.commit_assistant_message(
        lease.guard,
        cut=cut,
        entry_id=assistant_entry_id,
        parent_content=InlineContent.from_bytes(b"tool request"),
        blocks=(
            AssistantToolCallBlock(
                block_id=_name("block"),
                tool_call_id=tool_call_id,
                tool_name="terminal",
                arguments=freeze_json({"command": "true"}),
            ),
        ),
        occurred_at=datetime.now(timezone.utc),
        actor_id="model:test",
        deadline_monotonic=deadline,
    )
    command_id = _name("interaction-command")
    decision_id = _name("interaction-decision")
    attempt_id = _name("attempt") if decision == "ALLOW" else None
    result_id = _name("result") if decision == "DENY" else None
    result_entry_id = _name("entry") if decision == "DENY" else None
    accepted = _accept_tool_interaction_decision(
        repository,
        lease.guard,
        command_id=command_id,
        decision_id=decision_id,
        assistant_entry_id=assistant_entry_id,
        tool_call_id=tool_call_id,
        decision=decision,
        attempt_id=attempt_id,
        result_id=result_id,
        result_entry_id=result_entry_id,
        denial_content=(
            InlineContent.from_bytes(b"tool execution denied by user")
            if decision == "DENY"
            else None
        ),
        redacted_subject="tool:terminal",
        actor_id="attachment:test",
        occurred_at=datetime.now(timezone.utc),
        deadline_monotonic=deadline,
    )
    assert accepted.decision == decision
    # Lost response is resolved by exact command-winner confirmation, not by
    # creating a second decision or physical effect.
    confirmed = _accept_tool_interaction_decision(
        repository,
        lease.guard,
        command_id=command_id,
        decision_id=decision_id,
        assistant_entry_id=assistant_entry_id,
        tool_call_id=tool_call_id,
        decision=decision,
        attempt_id=attempt_id,
        result_id=result_id,
        result_entry_id=result_entry_id,
        denial_content=(
            InlineContent.from_bytes(b"tool execution denied by user")
            if decision == "DENY"
            else None
        ),
        redacted_subject="tool:terminal",
        actor_id="attachment:test",
        occurred_at=datetime.now(timezone.utc),
        deadline_monotonic=deadline,
    )
    assert confirmed == accepted
    with repository.connection_provider.connection(
        lane=PostgresConnectionLane.INSPECTOR,
        deadline_monotonic=deadline,
    ) as connection:
        counts = connection.execute(
            """
            SELECT
              (SELECT count(*) FROM pulsara_v3.interaction_decisions
               WHERE session_id = %s AND id = %s),
              (SELECT count(*) FROM pulsara_v3.session_commands
               WHERE session_id = %s AND command_id = %s),
              (SELECT count(*) FROM pulsara_v3.tool_execution_attempts
               WHERE session_id = %s AND assistant_entry_id = %s
                 AND tool_call_id = %s),
              (SELECT count(*) FROM pulsara_v3.tool_results
               WHERE session_id = %s AND tool_call_entry_id = %s
                 AND tool_call_id = %s)
            """,
            (
                session_id,
                decision_id,
                session_id,
                command_id,
                session_id,
                assistant_entry_id,
                tool_call_id,
                session_id,
                assistant_entry_id,
                tool_call_id,
            ),
        ).fetchone()
        assert counts == ((1, 1, 1, 0) if decision == "ALLOW" else (1, 1, 0, 1))
        event_types = tuple(
            row[0]
            for row in connection.execute(
                """
                SELECT event_type FROM pulsara_v3.agent_events
                WHERE session_id = %s
                  AND event_type IN (
                    'InteractionDecisionAccepted', 'ToolAttemptAccepted',
                    'ToolResultAccepted'
                  )
                ORDER BY event_sequence
                """,
                (session_id,),
            ).fetchall()
        )
        assert event_types == (
            ("InteractionDecisionAccepted", "ToolAttemptAccepted")
            if decision == "ALLOW"
            else ("InteractionDecisionAccepted", "ToolResultAccepted")
        )


def test_stage2_unqualified_product_sql_cannot_resolve_a_product_relation(
    stage2_migrated_postgres_database,
) -> None:
    repository = _repository(stage2_migrated_postgres_database)
    session_id = _name("session")
    repository.acquire_host_writer(
        session_id=session_id,
        workspace_id=_name("workspace"),
        writer_owner_id=_name("host"),
        lease_seconds=30,
        deadline_monotonic=monotonic() + 30,
    )
    with psycopg.connect(stage2_migrated_postgres_database.runtime_dsn) as connection:
        assert connection.execute("SHOW search_path").fetchone() == ('"$user", public',)
        with pytest.raises(psycopg.errors.UndefinedTable):
            connection.execute(
                "SELECT count(*) FROM sessions WHERE id = %s",
                (session_id,),
            )
        connection.rollback()
        assert connection.execute(
            "SELECT count(*) FROM pulsara_v3.sessions WHERE id = %s",
            (session_id,),
        ).fetchone() == (1,)
    require_stage2_runtime_privilege_boundary(
        repository.connection_provider,
        deadline_monotonic=monotonic() + 30,
    )


# Round 5B removes the entire durable job family; its canonical successor is
# the Host-owned compaction settlement coverage in the Round 5B suite.


def test_stage2_memory_governance_is_async_and_postgres_only(
    stage2_migrated_postgres_database,
) -> None:
    """Round 8 successor: governance owns rows but no durable execution."""

    repository = _repository(stage2_migrated_postgres_database)
    with repository.connection_provider.connection(
        lane=PostgresConnectionLane.INSPECTOR,
        deadline_monotonic=monotonic() + 30,
    ) as connection:
        relations = {
            row[0]
            for row in connection.execute(
                """
                SELECT table_name FROM information_schema.tables
                WHERE table_schema='pulsara_v3' AND table_name LIKE 'memory_%'
                """
            ).fetchall()
        }
    assert relations == {
        "memory_candidates",
        "memory_candidate_tool_result_refs",
        "memory_candidate_basis_refs",
        "memory_facts",
        "memory_relations",
        "memory_embeddings",
    }


def test_stage2_memory_lifecycle_change_has_one_canonical_occurrence(
    stage2_migrated_postgres_database,
) -> None:
    """Round 8 successor: memory lifecycle is relational, not an occurrence."""

    del stage2_migrated_postgres_database
    assert {item.event_type.value for item in COMMITTED_EVENT_DESCRIPTORS}.isdisjoint(
        {"MemoryFactAccepted", "MemoryFactLifecycleChanged", "MemoryRelationAccepted"}
    )
    assert all("memory" not in slot.lower() for slot in SUBJECT_SLOTS)


def test_stage2_postgres_memory_preserves_bounded_direct_and_two_hop_paths(
    stage2_migrated_postgres_database,
) -> None:
    """Round 8 successor: only exact rows and direct relations remain public."""

    del stage2_migrated_postgres_database
    assert "max_hops" not in PostgresMemoryQuery.search.__code__.co_varnames
    source = (
        __import__("pathlib").Path(__file__).resolve().parents[1]
        / "src/pulsara_agent/conversation_kernel/memory/recall.py"
    ).read_text(encoding="utf-8")
    assert "WITH RECURSIVE" not in source
    assert "max_hops" not in source


def test_stage2_prompt_queue_has_stable_fifo_and_frozen_terminal_steer_target(
    stage2_migrated_postgres_database,
) -> None:
    repository = _repository(stage2_migrated_postgres_database)
    deadline = monotonic() + 30
    session_id = _name("session")
    lease = repository.acquire_host_writer(
        session_id=session_id,
        workspace_id=_name("workspace"),
        writer_owner_id=_name("host"),
        lease_seconds=30,
        deadline_monotonic=deadline,
    )
    root_turn = _name("turn")
    initial = _start_root_turn(
        repository,
        lease.guard,
        command_id=_name("command"),
        turn_id=root_turn,
        entry_id=_name("entry"),
        context_binding_revision_id=_name("revision"),
        content=FrozenPromptContent.text('first'),
        occurred_at=datetime.now(timezone.utc),
        deadline_monotonic=deadline,
    )
    first_item = _name("queue")
    second_item = _name("queue")
    third_item = _name("queue")
    assert (
        _enqueue_prompt(
            repository,
            lease.guard,
            command_id=_name("command"),
            queue_item_id=first_item,
            client_submission_id=_name("client"),
            delivery_mode=PromptDeliveryMode.STEER_ACTIVE_TURN,
            target_turn_id=root_turn,
            content=FrozenPromptContent.text('steer'),
            occurred_at=datetime.now(timezone.utc),
            actor_id="user",
            deadline_monotonic=deadline,
        )
        == 1
    )
    assert (
        _enqueue_prompt(
            repository,
            lease.guard,
            command_id=_name("command"),
            queue_item_id=second_item,
            client_submission_id=_name("client"),
            delivery_mode=PromptDeliveryMode.STEER_ACTIVE_TURN,
            target_turn_id=root_turn,
            content=FrozenPromptContent.text('second steer'),
            occurred_at=datetime.now(timezone.utc),
            actor_id="user",
            deadline_monotonic=deadline,
        )
        == 2
    )
    assert (
        _enqueue_prompt(
            repository,
            lease.guard,
            command_id=_name("command"),
            queue_item_id=third_item,
            client_submission_id=_name("client"),
            delivery_mode=PromptDeliveryMode.NEW_TURN,
            target_turn_id=None,
            content=FrozenPromptContent.text('next'),
            occurred_at=datetime.now(timezone.utc),
            actor_id="user",
            deadline_monotonic=deadline,
        )
        == 3
    )
    assert initial.turn_id == root_turn
    assert (
        repository.prepare_prompt_head_consumption(
            session_id=session_id,
            occurred_at=datetime.now(timezone.utc),
            actor_id="runtime",
            deadline_monotonic=deadline,
        )
        is None
    )
    assert repository.interrupt_turn(
        lease.guard,
        turn_id=root_turn,
        reason="TEST_TARGET_TERMINAL",
        occurred_at=datetime.now(timezone.utc),
        actor_id="runtime:test",
        deadline_monotonic=deadline,
    )
    # The terminal transition has already rejected every steer bound to the
    # interrupted ROOT; the next global NEW_TURN remains independently consumable.
    candidate = repository.prepare_prompt_head_consumption(
        session_id=session_id,
        occurred_at=datetime.now(timezone.utc),
        actor_id="runtime",
        deadline_monotonic=deadline,
    )
    assert candidate is not None
    consumed = repository.consume_prepared_prompt_head(
        lease.guard,
        candidate=candidate,
        provider_input_admission=_root_provider_input_admission(
            candidate.provider_input_candidate
        ),
        deadline_monotonic=deadline,
    )
    assert consumed is not None
    assert consumed.kind is QueuedRootTurnAdmissionConfirmationKind.FULL
    assert consumed.accepted is not None
    assert consumed.accepted.turn_id == candidate.exact_turn_id
    with verified_postgres_provider(
        stage2_migrated_postgres_database.runtime_dsn
    ).connection(
        lane=PostgresConnectionLane.INSPECTOR,
        deadline_monotonic=deadline,
    ) as connection:
        assert connection.execute(
            """
            SELECT id, status, terminal_reason
            FROM pulsara_v3.prompt_queue_items
            WHERE session_id = %s ORDER BY queue_sequence
            """,
            (session_id,),
        ).fetchall() == [
            (first_item, "REJECTED", "TARGET_TURN_TERMINAL"),
            (second_item, "REJECTED", "TARGET_TURN_TERMINAL"),
            (third_item, "CONSUMED", "CONSUMED"),
        ]


def test_stage2_interrupt_turn_immediately_rejects_steer_without_future_turn(
    stage2_migrated_postgres_database,
) -> None:
    repository = _repository(stage2_migrated_postgres_database)
    deadline = monotonic() + 30
    session_id = _name("session")
    lease = repository.acquire_host_writer(
        session_id=session_id,
        workspace_id=_name("workspace"),
        writer_owner_id=_name("host"),
        lease_seconds=30,
        deadline_monotonic=deadline,
    )
    root_turn = _name("turn")
    _start_root_turn(
        repository,
        lease.guard,
        command_id=_name("command"),
        turn_id=root_turn,
        entry_id=_name("entry"),
        context_binding_revision_id=_name("revision"),
        content=FrozenPromptContent.text('first'),
        occurred_at=datetime.now(timezone.utc),
        deadline_monotonic=deadline,
    )
    queue_item_id = _name("queue")
    _enqueue_prompt(
        repository,
        lease.guard,
        command_id=_name("command"),
        queue_item_id=queue_item_id,
        client_submission_id=_name("client"),
        delivery_mode=PromptDeliveryMode.STEER_ACTIVE_TURN,
        target_turn_id=root_turn,
        content=FrozenPromptContent.text('steer'),
        occurred_at=datetime.now(timezone.utc),
        actor_id="user",
        deadline_monotonic=deadline,
    )

    assert repository.interrupt_turn(
        lease.guard,
        turn_id=root_turn,
        reason="TEST_TARGET_TERMINAL",
        occurred_at=datetime.now(timezone.utc),
        actor_id="runtime:test",
        deadline_monotonic=deadline,
    )

    with verified_postgres_provider(
        stage2_migrated_postgres_database.runtime_dsn
    ).connection(
        lane=PostgresConnectionLane.INSPECTOR,
        deadline_monotonic=deadline,
    ) as connection:
        queue_row = connection.execute(
            """
            SELECT status, terminal_reason
            FROM pulsara_v3.prompt_queue_items
            WHERE session_id = %s AND id = %s
            """,
            (session_id, queue_item_id),
        ).fetchone()
        rejected_event = connection.execute(
            """
            SELECT payload->>'reason'
            FROM pulsara_v3.agent_events
            WHERE session_id = %s AND subject_queue_item_id = %s
              AND event_type = 'PromptRejected'
            """,
            (session_id, queue_item_id),
        ).fetchone()
    assert queue_row == ("REJECTED", "TARGET_TURN_TERMINAL")
    assert rejected_event == ("TARGET_TURN_TERMINAL",)


def test_round3_1_future_new_turn_lane_does_not_block_active_steer_cut(
    stage2_migrated_postgres_database,
) -> None:
    repository = _repository(stage2_migrated_postgres_database)
    deadline = monotonic() + 30
    session_id = _name("session")
    lease = repository.acquire_host_writer(
        session_id=session_id,
        workspace_id=_name("workspace"),
        writer_owner_id=_name("host"),
        lease_seconds=30,
        deadline_monotonic=deadline,
    )
    root_turn = _name("turn")
    _start_root_turn(
        repository,
        lease.guard,
        command_id=_name("command"),
        turn_id=root_turn,
        entry_id=_name("entry"),
        context_binding_revision_id=_name("revision"),
        content=FrozenPromptContent.text('initial'),
        occurred_at=datetime.now(timezone.utc),
        deadline_monotonic=deadline,
    )
    new_item = _name("queue")
    steer_item = _name("queue")
    _enqueue_prompt(
        repository,
        lease.guard,
        command_id=_name("command"),
        queue_item_id=new_item,
        client_submission_id=_name("client"),
        delivery_mode=PromptDeliveryMode.NEW_TURN,
        target_turn_id=None,
        content=FrozenPromptContent.text('next'),
        occurred_at=datetime.now(timezone.utc),
        actor_id="user",
        deadline_monotonic=deadline,
    )
    _enqueue_prompt(
        repository,
        lease.guard,
        command_id=_name("command"),
        queue_item_id=steer_item,
        client_submission_id=_name("client"),
        delivery_mode=PromptDeliveryMode.STEER_ACTIVE_TURN,
        target_turn_id=root_turn,
        content=FrozenPromptContent.text('steer'),
        occurred_at=datetime.now(timezone.utc),
        actor_id="user",
        deadline_monotonic=deadline,
    )
    steers = repository.read_pending_prompt_steer_facts(
        session_id=session_id,
        target_turn_id=root_turn,
        deadline_monotonic=deadline,
    )
    assert tuple(item.queue_item_id for item in steers) == (steer_item,)
    assert (
        repository.pending_prompt_head_mode(
            session_id=session_id, deadline_monotonic=deadline
        )
        is PromptDeliveryMode.NEW_TURN
    )
    assert steers[0].exact_target_turn_id == root_turn


def test_round3_1_prompt_ingress_confirmation_is_semantically_exact_and_stable(
    stage2_migrated_postgres_database,
) -> None:
    repository = _repository(stage2_migrated_postgres_database)
    deadline = monotonic() + 30
    session_id = _name("session")
    workspace_id = _name("workspace")
    lease = repository.acquire_host_writer(
        session_id=session_id,
        workspace_id=workspace_id,
        writer_owner_id=_name("host"),
        lease_seconds=30,
        deadline_monotonic=deadline,
    )
    root_turn = _name("turn")
    _start_root_turn(
        repository,
        lease.guard,
        command_id=_name("command"),
        turn_id=root_turn,
        entry_id=_name("entry"),
        context_binding_revision_id=_name("revision"),
        content=FrozenPromptContent.text('initial'),
        occurred_at=datetime.now(timezone.utc),
        deadline_monotonic=deadline,
    )
    command_id = _name("command-steer")
    queue_item_id = _name("queue-steer")
    client_submission_id = _name("submission")
    content = b"exact steer"
    candidate = build_prompt_ingress_command(
        session_id=session_id,
        command_id=command_id,
        queue_item_id=queue_item_id,
        client_submission_id=client_submission_id,
        delivery_mode=PromptDeliveryMode.STEER_ACTIVE_TURN,
        target_turn_id=root_turn,
        permission_snapshot_id=None,
        requested_permission_mode=None,
        canonical_prompt=freeze_canonical_prompt(
            FrozenPromptContent.text(content.decode("utf-8"))
        ),
    )
    _enqueue_prompt(
        repository,
        lease.guard,
        command_id=command_id,
        queue_item_id=queue_item_id,
        client_submission_id=client_submission_id,
        delivery_mode=PromptDeliveryMode.STEER_ACTIVE_TURN,
        target_turn_id=root_turn,
        content=FrozenPromptContent.text(content.decode("utf-8")),
        occurred_at=datetime.now(timezone.utc),
        actor_id="user",
        deadline_monotonic=deadline,
    )
    assert (
        repository.confirm_prompt_ingress(
            candidate=candidate, deadline_monotonic=deadline
        ).kind
        is PromptIngressConfirmationKind.FULL_COMPATIBLE
    )

    conflicting = build_prompt_ingress_command(
        session_id=session_id,
        command_id=command_id,
        queue_item_id=queue_item_id,
        client_submission_id=client_submission_id,
        delivery_mode=PromptDeliveryMode.STEER_ACTIVE_TURN,
        target_turn_id=root_turn,
        permission_snapshot_id=None,
        requested_permission_mode=None,
        canonical_prompt=freeze_canonical_prompt(
            FrozenPromptContent.text("different steer")
        ),
    )
    assert (
        repository.confirm_prompt_ingress(
            candidate=conflicting, deadline_monotonic=deadline
        ).kind
        is PromptIngressConfirmationKind.CONFLICT
    )

    repository.interrupt_turn(
        lease.guard,
        turn_id=root_turn,
        reason="TEST_TERMINAL_AFTER_ACK_LOSS",
        occurred_at=datetime.now(timezone.utc),
        actor_id="runtime:test",
        deadline_monotonic=deadline,
    )
    assert (
        repository.confirm_prompt_ingress(
            candidate=candidate, deadline_monotonic=deadline
        ).kind
        is PromptIngressConfirmationKind.FULL_COMPATIBLE
    )


def test_round3_1_steer_consumption_ack_confirmation_is_exact(
    stage2_migrated_postgres_database,
) -> None:
    provider = verified_postgres_provider(stage2_migrated_postgres_database.runtime_dsn)
    repository = ConversationKernelRepository(provider)
    deadline = monotonic() + 30
    session_id = _name("session")
    workspace_id = _name("workspace")
    lease = repository.acquire_host_writer(
        session_id=session_id,
        workspace_id=workspace_id,
        writer_owner_id=_name("host"),
        lease_seconds=30,
        deadline_monotonic=deadline,
    )
    turn_id = _name("turn")
    revision_id = _name("revision")
    _start_root_turn(
        repository,
        lease.guard,
        command_id=_name("command"),
        turn_id=turn_id,
        entry_id=_name("entry"),
        context_binding_revision_id=revision_id,
        content=FrozenPromptContent.text('initial'),
        occurred_at=datetime.now(timezone.utc),
        deadline_monotonic=deadline,
    )
    _enqueue_prompt(
        repository,
        lease.guard,
        command_id=_name("command-steer"),
        queue_item_id=_name("queue-steer"),
        client_submission_id=_name("submission"),
        delivery_mode=PromptDeliveryMode.STEER_ACTIVE_TURN,
        target_turn_id=turn_id,
        content=FrozenPromptContent.text('exact steer'),
        occurred_at=datetime.now(timezone.utc),
        actor_id="user",
        deadline_monotonic=deadline,
    )
    fact = repository.read_pending_prompt_steer_facts(
        session_id=session_id,
        target_turn_id=turn_id,
        deadline_monotonic=deadline,
    )[0]
    owner = new_test_provider_input_continuity_owner(session_id)
    planning = owner.freeze_planning_input(
        scope=ProviderInputContinuityScope(
            session_id=session_id,
            scope_kind=ModelInputScopeKind.ROOT,
            scope_subagent_task_id=None,
        ),
        canonical_frontier=ProcessLocalCanonicalFrontier(
            latest_context_binding_revision_id=revision_id,
            context_base_semantic_identity=FULL_HISTORY_CONTEXT_BASE_IDENTITY,
            through_sequence=1,
            ordered_item_fingerprints=("sha256:" + "1" * 64,),
        ),
        dispatch_anchor=NoNewTriggerAnchor(None),
    )
    occurred_at = datetime.now(timezone.utc)
    canonical_base_fence = build_steer_canonical_base_fence(
        CanonicalProviderInputReader(provider).read_frozen_compile_snapshot(
            PreparedProviderInputCut(
                session_id=session_id,
                turn_id=turn_id,
                context_binding_revision_id=revision_id,
                provider_input_through_sequence=1,
            ),
            deadline_monotonic=deadline,
        )
    )
    candidate = build_steer_consumption_candidate(
        fact=fact,
        canonical_prompt=freeze_canonical_prompt(
            FrozenPromptContent.text("exact steer")
        ),
        expected_entry_sequence=2,
        predecessor=planning,
        canonical_base_fence=canonical_base_fence,
        occurred_at=occurred_at,
        actor_id=lease.guard.writer_owner_id,
    )
    assert (
        repository.confirm_prepared_prompt_steer(
            candidate=candidate, deadline_monotonic=deadline
        ).kind
        is SteerConsumptionConfirmationKind.NONE
    )
    accepted = repository.consume_prepared_prompt_steer(
        lease.guard,
        candidate=candidate,
        deadline_monotonic=deadline,
    )
    assert accepted.user_steer_event_sequence == (
        accepted.prompt_consumed_event_sequence + 1
    )
    confirmation = repository.confirm_prepared_prompt_steer(
        candidate=candidate, deadline_monotonic=deadline
    )
    assert confirmation.kind is SteerConsumptionConfirmationKind.FULL
    assert confirmation.accepted == accepted

    conflicting = build_steer_consumption_candidate(
        fact=fact,
        canonical_prompt=freeze_canonical_prompt(
            FrozenPromptContent.text("exact steer")
        ),
        expected_entry_sequence=2,
        predecessor=planning,
        canonical_base_fence=canonical_base_fence,
        occurred_at=occurred_at + timedelta(microseconds=1),
        actor_id=lease.guard.writer_owner_id,
    )
    assert (
        repository.confirm_prepared_prompt_steer(
            candidate=conflicting, deadline_monotonic=deadline
        ).kind
        is SteerConsumptionConfirmationKind.CONFLICT
    )


def test_round3_1_steer_consume_rejects_canonical_base_drift_without_mutation(
    stage2_migrated_postgres_database,
) -> None:
    provider = verified_postgres_provider(stage2_migrated_postgres_database.runtime_dsn)
    repository = ConversationKernelRepository(provider)
    deadline = monotonic() + 30
    session_id = _name("session")
    workspace_id = _name("workspace")
    lease = repository.acquire_host_writer(
        session_id=session_id,
        workspace_id=workspace_id,
        writer_owner_id=_name("host"),
        lease_seconds=30,
        deadline_monotonic=deadline,
    )
    turn_id = _name("turn")
    revision_id = _name("revision")
    _start_root_turn(
        repository,
        lease.guard,
        command_id=_name("command"),
        turn_id=turn_id,
        entry_id=_name("entry"),
        context_binding_revision_id=revision_id,
        content=FrozenPromptContent.text('initial'),
        occurred_at=datetime.now(timezone.utc),
        deadline_monotonic=deadline,
    )
    _enqueue_prompt(
        repository,
        lease.guard,
        command_id=_name("command-steer"),
        queue_item_id=_name("queue-steer"),
        client_submission_id=_name("submission"),
        delivery_mode=PromptDeliveryMode.STEER_ACTIVE_TURN,
        target_turn_id=turn_id,
        content=FrozenPromptContent.text('must remain pending'),
        occurred_at=datetime.now(timezone.utc),
        actor_id="user",
        deadline_monotonic=deadline,
    )
    fact = repository.read_pending_prompt_steer_facts(
        session_id=session_id,
        target_turn_id=turn_id,
        deadline_monotonic=deadline,
    )[0]
    base = CanonicalProviderInputReader(provider).read_frozen_compile_snapshot(
        PreparedProviderInputCut(
            session_id=session_id,
            turn_id=turn_id,
            context_binding_revision_id=revision_id,
            provider_input_through_sequence=1,
        ),
        deadline_monotonic=deadline,
    )
    planning = new_test_provider_input_continuity_owner(
        session_id
    ).freeze_planning_input(
        scope=ProviderInputContinuityScope(
            session_id=session_id,
            scope_kind=ModelInputScopeKind.ROOT,
            scope_subagent_task_id=None,
        ),
        canonical_frontier=ProcessLocalCanonicalFrontier(
            latest_context_binding_revision_id=revision_id,
            context_base_semantic_identity=FULL_HISTORY_CONTEXT_BASE_IDENTITY,
            through_sequence=1,
            ordered_item_fingerprints=(base.canonical_input.snapshot_fingerprint,),
        ),
        dispatch_anchor=NoNewTriggerAnchor(None),
    )
    steer_candidate = build_steer_consumption_candidate(
        fact=fact,
        canonical_prompt=freeze_canonical_prompt(
            FrozenPromptContent.text("must remain pending")
        ),
        expected_entry_sequence=2,
        predecessor=planning,
        canonical_base_fence=build_steer_canonical_base_fence(base),
        occurred_at=datetime.now(timezone.utc),
        actor_id=lease.guard.writer_owner_id,
    )

    compaction_cut = repository.prepare_compaction_input_cut(
        lease.guard,
        turn_id=turn_id,
        allow_terminal=False,
        deadline_monotonic=deadline,
    )
    compaction_read = CanonicalProviderInputReader(provider).read_frozen_compaction_cut(
        compaction_cut,
        deadline_monotonic=deadline,
    )
    scope = CompactionScope(
        session_id=session_id,
        workspace_id=workspace_id,
        turn_id=turn_id,
        scope_kind=ModelInputScopeKind.ROOT,
        scope_subagent_task_id=None,
    )
    snapshot_carrier = build_compaction_snapshot_carrier(
        summary=freeze_compaction_summary_output(
            "summary", maximum_utf8_bytes=100
        ),
        recent_human_requests=(),
        continuation_mode=CompactionContinuationMode.RESUME_ACTIVE_TURN,
        active_request=FrozenCompactionActiveRequest(
            entry_id=(
                compaction_read.dispatch_read.compile_snapshot.canonical_input.identity.initial_entry_id
            ),
            entry_sequence=compaction_read.safe_head_range.source_through_sequence,
            location=CompactionActiveRequestLocation.SNAPSHOT_EXACT,
            item_kind=FrozenProviderInputItemKind.USER,
            input_origin=CanonicalInputOriginKind.HUMAN_MESSAGE,
            content=FrozenPromptContent.text("initial"),
        ),
    )
    compaction_candidate = build_prepared_compaction_canonical_adoption(
        CompactionCanonicalAdoptionFactoryInput(
            scope=scope,
            target_branch=CompactionTargetBranch.ACTIVE_INSTALLATION,
            expected_turn_status="RUNNING",
            predecessor=ExpectedCompactionPredecessorRevision(
                binding_revision_id=(compaction_read.lineage_base.binding_revision_id),
                revision_ordinal=(
                    compaction_read.lineage_base.binding_revision_ordinal
                ),
                base_kind=(
                    "FULL_HISTORY"
                    if compaction_read.lineage_base.snapshot_id is None
                    else "SNAPSHOT"
                ),
                context_snapshot_id=compaction_read.lineage_base.snapshot_id,
                source_through_sequence=(
                    compaction_read.lineage_base.persisted_revision_genesis_marker
                ),
            ),
            snapshot_id=_name("snapshot"),
            binding_revision_id=_name("replacement-revision"),
            event_id=_name("compaction-event"),
            source_through_sequence=(
                compaction_read.safe_head_range.source_through_sequence
            ),
            source_digest=canonical_compaction_range_digest(
                compaction_read.lineage_base,
                compaction_read.safe_head_range,
            ),
            snapshot_content=_snapshot_content(snapshot_carrier),
            snapshot_carrier=snapshot_carrier,
            compiler_contract=COMPACTION_SNAPSHOT_COMPILER_CONTRACT,
            prompt_contract=COMPACTION_SUMMARY_PROMPT_CONTRACT,
            model_contract=COMPACTION_MODEL_CONTRACT,
            occurred_at=datetime.now(timezone.utc),
            actor_id="runtime:test",
        )
    )
    winner = repository.adopt_context_snapshot(
        lease.guard,
        candidate=compaction_candidate,
        preconditions=CompactionCanonicalWritePreconditions(
            scope=scope,
            expected_turn_status="RUNNING",
            expected_safe_head=(
                compaction_read.safe_head_range.source_through_sequence
            ),
            provider_safe=True,
        ),
        deadline_monotonic=deadline,
    )
    assert winner.kind is CompactionConfirmationKind.FULL
    assert (
        repository.confirm_context_snapshot_adoption(
            candidate=compaction_candidate,
            deadline_monotonic=deadline,
        )
        == winner
    )
    conflicting_carrier = build_compaction_snapshot_carrier(
        summary=freeze_compaction_summary_output(
            "different summary", maximum_utf8_bytes=100
        ),
        recent_human_requests=(),
        continuation_mode=CompactionContinuationMode.RESUME_ACTIVE_TURN,
        active_request=snapshot_carrier.active_request,
    )
    conflicting_compaction = build_prepared_compaction_canonical_adoption(
        CompactionCanonicalAdoptionFactoryInput(
            scope=scope,
            target_branch=CompactionTargetBranch.ACTIVE_INSTALLATION,
            expected_turn_status="RUNNING",
            predecessor=ExpectedCompactionPredecessorRevision(
                binding_revision_id=compaction_read.lineage_base.binding_revision_id,
                revision_ordinal=compaction_read.lineage_base.binding_revision_ordinal,
                base_kind="FULL_HISTORY",
                context_snapshot_id=None,
                source_through_sequence=(
                    compaction_read.lineage_base.persisted_revision_genesis_marker
                ),
            ),
            snapshot_id=compaction_candidate.snapshot.snapshot_id,
            binding_revision_id=compaction_candidate.binding.binding_revision_id,
            event_id=compaction_candidate.event.event_id,
            source_through_sequence=(
                compaction_read.safe_head_range.source_through_sequence
            ),
            source_digest=canonical_compaction_range_digest(
                compaction_read.lineage_base,
                compaction_read.safe_head_range,
            ),
            snapshot_content=_snapshot_content(conflicting_carrier),
            snapshot_carrier=conflicting_carrier,
            compiler_contract=COMPACTION_SNAPSHOT_COMPILER_CONTRACT,
            prompt_contract=COMPACTION_SUMMARY_PROMPT_CONTRACT,
            model_contract=COMPACTION_MODEL_CONTRACT,
            occurred_at=compaction_candidate.event.occurred_at,
            actor_id="runtime:test",
        )
    )
    assert (
        repository.confirm_context_snapshot_adoption(
            candidate=conflicting_compaction,
            deadline_monotonic=deadline,
        ).kind
        is CompactionConfirmationKind.CONFLICT
    )
    with pytest.raises(ConversationKernelConflict, match="control base drifted"):
        repository.consume_prepared_prompt_steer(
            lease.guard,
            candidate=steer_candidate,
            deadline_monotonic=deadline,
        )
    assert (
        repository.confirm_prepared_prompt_steer(
            candidate=steer_candidate, deadline_monotonic=deadline
        ).kind
        is SteerConsumptionConfirmationKind.NONE
    )
    with provider.connection(
        lane=PostgresConnectionLane.INSPECTOR,
        deadline_monotonic=deadline,
    ) as connection:
        assert (
            connection.execute(
                "SELECT count(*) FROM pulsara_v3.context_snapshots "
                "WHERE session_id = %s",
                (session_id,),
            ).fetchone()[0]
            == 1
        )
        assert (
            connection.execute(
                "SELECT count(*) FROM pulsara_v3.turn_context_binding_revisions "
                "WHERE session_id = %s AND turn_id = %s",
                (session_id, turn_id),
            ).fetchone()[0]
            == 2
        )
        assert connection.execute(
            "SELECT status, consumed_entry_id FROM pulsara_v3.prompt_queue_items "
            "WHERE session_id = %s AND id = %s",
            (session_id, fact.queue_item_id),
        ).fetchone() == ("PENDING", None)
        assert connection.execute(
            "SELECT count(*) FROM pulsara_v3.transcript_entries "
            "WHERE session_id = %s AND entry_kind = 'USER_STEER'",
            (session_id,),
        ).fetchone() == (0,)


def test_round5b_first_compaction_can_cut_before_turn_genesis_marker(
    stage2_migrated_postgres_database,
) -> None:
    """A late ROOT turn may summarize earlier same-scope FULL_HISTORY."""

    provider = verified_postgres_provider(stage2_migrated_postgres_database.runtime_dsn)
    repository = ConversationKernelRepository(provider)
    deadline = monotonic() + 30
    session_id = _name("session")
    workspace_id = _name("workspace")
    lease = repository.acquire_host_writer(
        session_id=session_id,
        workspace_id=workspace_id,
        writer_owner_id=_name("host"),
        lease_seconds=30,
        deadline_monotonic=deadline,
    )
    prior_assistant_sequences: list[int] = []
    for index in range(3):
        turn_id = _name(f"prior-turn-{index}")
        _start_root_turn(
            repository,
            lease.guard,
            command_id=_name(f"prior-command-{index}"),
            turn_id=turn_id,
            entry_id=_name(f"prior-user-{index}"),
            context_binding_revision_id=_name(f"prior-revision-{index}"),
            content=FrozenPromptContent.text(f'prior user {index}'),
            occurred_at=datetime.now(timezone.utc),
            deadline_monotonic=deadline,
        )
        cut = repository.prepare_provider_input_cut(
            lease.guard, turn_id=turn_id, deadline_monotonic=deadline
        )
        accepted = repository.commit_assistant_message(
            lease.guard,
            cut=cut,
            entry_id=_name(f"prior-assistant-{index}"),
            parent_content=InlineContent.from_bytes(
                f"prior assistant {index}".encode()
            ),
            blocks=(
                AssistantTextBlock(
                    _name(f"prior-block-{index}"),
                    InlineContent.from_bytes(f"prior assistant {index}".encode()),
                ),
            ),
            complete_turn=True,
            occurred_at=datetime.now(timezone.utc),
            actor_id="model:test",
            deadline_monotonic=deadline,
        )
        prior_assistant_sequences.append(accepted.entry_sequence)

    turn_id = _name("current-turn")
    revision_id = _name("current-revision")
    current = _start_root_turn(
        repository,
        lease.guard,
        command_id=_name("current-command"),
        turn_id=turn_id,
        entry_id=_name("current-user"),
        context_binding_revision_id=revision_id,
        content=FrozenPromptContent.text('current user'),
        occurred_at=datetime.now(timezone.utc),
        deadline_monotonic=deadline,
    )
    current_read = CanonicalProviderInputReader(provider).read_frozen_compaction_cut(
        repository.prepare_compaction_input_cut(
            lease.guard,
            turn_id=turn_id,
            allow_terminal=False,
            deadline_monotonic=deadline,
        ),
        deadline_monotonic=deadline,
    )
    lineage = current_read.lineage_base
    boundary = prior_assistant_sequences[1]
    assert lineage.persisted_revision_genesis_marker == current.entry_sequence - 1
    assert boundary < lineage.persisted_revision_genesis_marker
    assert lineage.effective_materialization_lineage_floor == 0

    canonical = current_read.dispatch_read.compile_snapshot.canonical_input
    canonical_range = freeze_compaction_canonical_range(
        scope=current_read.scope,
        effective_materialization_lineage_floor=0,
        source_through_sequence=boundary,
        ordered_items=canonical.items,
        closures=canonical.closures,
        late_outcomes=canonical.late_outcomes,
    )
    snapshot_carrier = build_compaction_snapshot_carrier(
        summary=freeze_compaction_summary_output(
            "summary", maximum_utf8_bytes=100
        ),
        recent_human_requests=(),
        continuation_mode=CompactionContinuationMode.RESUME_ACTIVE_TURN,
        active_request=FrozenCompactionActiveRequest(
            entry_id=canonical.identity.initial_entry_id,
            entry_sequence=current.entry_sequence,
            location=CompactionActiveRequestLocation.CANONICAL_SUFFIX,
            item_kind=FrozenProviderInputItemKind.USER,
            input_origin=CanonicalInputOriginKind.HUMAN_MESSAGE,
            content=None,
        ),
    )
    candidate = build_prepared_compaction_canonical_adoption(
        CompactionCanonicalAdoptionFactoryInput(
            scope=current_read.scope,
            target_branch=CompactionTargetBranch.ACTIVE_INSTALLATION,
            expected_turn_status="RUNNING",
            predecessor=ExpectedCompactionPredecessorRevision(
                binding_revision_id=lineage.binding_revision_id,
                revision_ordinal=lineage.binding_revision_ordinal,
                base_kind="FULL_HISTORY",
                context_snapshot_id=None,
                source_through_sequence=lineage.persisted_revision_genesis_marker,
            ),
            snapshot_id=_name("snapshot"),
            binding_revision_id=_name("replacement-revision"),
            event_id=_name("compaction-event"),
            source_through_sequence=boundary,
            source_digest=canonical_compaction_range_digest(lineage, canonical_range),
            snapshot_content=_snapshot_content(snapshot_carrier),
            snapshot_carrier=snapshot_carrier,
            compiler_contract=COMPACTION_SNAPSHOT_COMPILER_CONTRACT,
            prompt_contract=COMPACTION_SUMMARY_PROMPT_CONTRACT,
            model_contract=COMPACTION_MODEL_CONTRACT,
            occurred_at=datetime.now(timezone.utc),
            actor_id="runtime:test",
        )
    )
    winner = repository.adopt_context_snapshot(
        lease.guard,
        candidate=candidate,
        preconditions=CompactionCanonicalWritePreconditions(
            scope=current_read.scope,
            expected_turn_status="RUNNING",
            expected_safe_head=(current_read.safe_head_range.source_through_sequence),
            provider_safe=True,
        ),
        deadline_monotonic=deadline,
    )
    assert winner.kind is CompactionConfirmationKind.FULL
    assert (
        repository.confirm_context_snapshot_adoption(
            candidate=candidate, deadline_monotonic=deadline
        )
        == winner
    )


def test_round5b_manual_compaction_command_is_exact_and_ack_confirmable(
    stage2_migrated_postgres_database,
) -> None:
    provider = verified_postgres_provider(stage2_migrated_postgres_database.runtime_dsn)
    repository = ConversationKernelRepository(provider)
    deadline = monotonic() + 30
    session_id = _name("session")
    lease = repository.acquire_host_writer(
        session_id=session_id,
        workspace_id=_name("workspace"),
        writer_owner_id=_name("host"),
        lease_seconds=30,
        deadline_monotonic=deadline,
    )
    turn_id = _name("turn")
    _start_root_turn(
        repository,
        lease.guard,
        command_id=_name("prompt-command"),
        turn_id=turn_id,
        entry_id=_name("entry"),
        context_binding_revision_id=_name("revision"),
        content=FrozenPromptContent.text('initial'),
        occurred_at=datetime.now(timezone.utc),
        deadline_monotonic=deadline,
    )
    command_id = _name("compact-command")
    candidate = build_prepared_manual_compaction_command(
        session_id=session_id,
        command_id=command_id,
        scope_kind=ModelInputScopeKind.ROOT,
        scope_subagent_task_id=None,
        target_turn_id=turn_id,
        expected_active_turn_id=turn_id,
        force=False,
    )
    assert (
        repository.confirm_manual_compaction_command(
            candidate=candidate,
            deadline_monotonic=deadline,
        )
        is CompactionConfirmationKind.NONE
    )
    assert (
        repository.accept_manual_compaction_command(
            lease.guard,
            candidate=candidate,
            deadline_monotonic=deadline,
        )
        is CompactionConfirmationKind.FULL
    )
    assert (
        repository.confirm_manual_compaction_command(
            candidate=candidate,
            deadline_monotonic=deadline,
        )
        is CompactionConfirmationKind.FULL
    )
    conflict = build_prepared_manual_compaction_command(
        session_id=session_id,
        command_id=command_id,
        scope_kind=ModelInputScopeKind.ROOT,
        scope_subagent_task_id=None,
        target_turn_id=turn_id,
        expected_active_turn_id=turn_id,
        force=True,
    )
    assert (
        repository.confirm_manual_compaction_command(
            candidate=conflict,
            deadline_monotonic=deadline,
        )
        is CompactionConfirmationKind.CONFLICT
    )


def test_round5b_next_exact_scope_turn_inherits_latest_snapshot_base(
    stage2_migrated_postgres_database,
) -> None:
    provider = verified_postgres_provider(stage2_migrated_postgres_database.runtime_dsn)
    repository = ConversationKernelRepository(provider)
    deadline = monotonic() + 30
    session_id = _name("session")
    workspace_id = _name("workspace")
    lease = repository.acquire_host_writer(
        session_id=session_id,
        workspace_id=workspace_id,
        writer_owner_id=_name("host"),
        lease_seconds=30,
        deadline_monotonic=deadline,
    )
    first_turn = _name("first-turn")
    first_revision = _name("first-revision")
    _start_root_turn(
        repository,
        lease.guard,
        command_id=_name("first-command"),
        turn_id=first_turn,
        entry_id=_name("first-entry"),
        context_binding_revision_id=first_revision,
        content=FrozenPromptContent.text('old prompt'),
        occurred_at=datetime.now(timezone.utc),
        deadline_monotonic=deadline,
    )
    cut = repository.prepare_compaction_input_cut(
        lease.guard,
        turn_id=first_turn,
        allow_terminal=False,
        deadline_monotonic=deadline,
    )
    read = CanonicalProviderInputReader(provider).read_frozen_compaction_cut(
        cut,
        deadline_monotonic=deadline,
    )
    scope = CompactionScope(
        session_id=session_id,
        workspace_id=workspace_id,
        turn_id=first_turn,
        scope_kind=ModelInputScopeKind.ROOT,
        scope_subagent_task_id=None,
    )
    snapshot_carrier = build_compaction_snapshot_carrier(
        summary=freeze_compaction_summary_output("old summary", maximum_utf8_bytes=100),
        recent_human_requests=(),
        continuation_mode=CompactionContinuationMode.RESUME_ACTIVE_TURN,
        active_request=FrozenCompactionActiveRequest(
            entry_id=read.dispatch_read.compile_snapshot.canonical_input.identity.initial_entry_id,
            entry_sequence=read.safe_head_range.source_through_sequence,
            location=CompactionActiveRequestLocation.SNAPSHOT_EXACT,
            item_kind=FrozenProviderInputItemKind.USER,
            input_origin=CanonicalInputOriginKind.HUMAN_MESSAGE,
            content=FrozenPromptContent.text("old prompt"),
        ),
    )
    candidate = build_prepared_compaction_canonical_adoption(
        CompactionCanonicalAdoptionFactoryInput(
            scope=scope,
            target_branch=CompactionTargetBranch.ACTIVE_INSTALLATION,
            expected_turn_status="RUNNING",
            predecessor=ExpectedCompactionPredecessorRevision(
                binding_revision_id=read.lineage_base.binding_revision_id,
                revision_ordinal=read.lineage_base.binding_revision_ordinal,
                base_kind="FULL_HISTORY",
                context_snapshot_id=None,
                source_through_sequence=(
                    read.lineage_base.persisted_revision_genesis_marker
                ),
            ),
            snapshot_id=_name("snapshot"),
            binding_revision_id=_name("snapshot-revision"),
            event_id=_name("snapshot-event"),
            source_through_sequence=read.safe_head_range.source_through_sequence,
            source_digest=canonical_compaction_range_digest(
                read.lineage_base,
                read.safe_head_range,
            ),
            snapshot_content=_snapshot_content(snapshot_carrier),
            snapshot_carrier=snapshot_carrier,
            compiler_contract=COMPACTION_SNAPSHOT_COMPILER_CONTRACT,
            prompt_contract=COMPACTION_SUMMARY_PROMPT_CONTRACT,
            model_contract=COMPACTION_MODEL_CONTRACT,
            occurred_at=datetime.now(timezone.utc),
            actor_id="runtime:test",
        )
    )
    assert (
        repository.adopt_context_snapshot(
            lease.guard,
            candidate=candidate,
            preconditions=CompactionCanonicalWritePreconditions(
                scope=scope,
                expected_turn_status="RUNNING",
                expected_safe_head=read.safe_head_range.source_through_sequence,
                provider_safe=True,
            ),
            deadline_monotonic=deadline,
        ).kind
        is CompactionConfirmationKind.FULL
    )
    assert repository.interrupt_turn(
        lease.guard,
        turn_id=first_turn,
        reason="TEST_COMPLETE",
        occurred_at=datetime.now(timezone.utc),
        actor_id="runtime:test",
        deadline_monotonic=deadline,
    )

    second_turn = _name("second-turn")
    second_revision = _name("second-revision")
    _start_root_turn(
        repository,
        lease.guard,
        command_id=_name("second-command"),
        turn_id=second_turn,
        entry_id=_name("second-entry"),
        context_binding_revision_id=second_revision,
        content=FrozenPromptContent.text('new prompt'),
        occurred_at=datetime.now(timezone.utc),
        deadline_monotonic=deadline,
    )
    second_cut = repository.prepare_provider_input_cut(
        lease.guard,
        turn_id=second_turn,
        deadline_monotonic=deadline,
    )
    second = CanonicalProviderInputReader(provider).read_frozen_compile_snapshot(
        second_cut,
        deadline_monotonic=deadline,
    )
    assert second.context_binding_fact.base_kind is ContextBindingBaseKind.SNAPSHOT
    assert (
        second.context_binding_fact.context_snapshot_id
        == candidate.snapshot.snapshot_id
    )
    assert second.canonical_input.items[0].item_kind is (
        FrozenProviderInputItemKind.CONTEXT_SNAPSHOT
    )
    assert second.canonical_input.items[0].content == snapshot_carrier
    assert provider_input_item_text(second.canonical_input.items[-1]) == "new prompt"


def test_round3_1_resource_rejection_is_atomic_and_exactly_confirmable(
    stage2_migrated_postgres_database,
) -> None:
    provider = verified_postgres_provider(stage2_migrated_postgres_database.runtime_dsn)
    repository = _FailingSteerRejectionRepository(provider)
    deadline = monotonic() + 30
    session_id = _name("session")
    lease = repository.acquire_host_writer(
        session_id=session_id,
        workspace_id=_name("workspace"),
        writer_owner_id=_name("host"),
        lease_seconds=30,
        deadline_monotonic=deadline,
    )
    turn_id = _name("turn")
    _start_root_turn(
        repository,
        lease.guard,
        command_id=_name("command"),
        turn_id=turn_id,
        entry_id=_name("entry"),
        context_binding_revision_id=_name("revision"),
        content=FrozenPromptContent.text('initial'),
        occurred_at=datetime.now(timezone.utc),
        deadline_monotonic=deadline,
    )
    _enqueue_prompt(
        repository,
        lease.guard,
        command_id=_name("command-steer"),
        queue_item_id=_name("queue-steer"),
        client_submission_id=_name("submission"),
        delivery_mode=PromptDeliveryMode.STEER_ACTIVE_TURN,
        target_turn_id=turn_id,
        content=FrozenPromptContent.text('too large for the fixed prefix'),
        occurred_at=datetime.now(timezone.utc),
        actor_id="user",
        deadline_monotonic=deadline,
    )
    fact = repository.read_pending_prompt_steer_facts(
        session_id=session_id,
        target_turn_id=turn_id,
        deadline_monotonic=deadline,
    )[0]
    candidate = build_steer_resource_rejection(
        fact=fact,
        occurred_at=datetime.now(timezone.utc),
        actor_id=lease.guard.writer_owner_id,
    )

    repository.fail_event_append = True
    with pytest.raises(RuntimeError, match="injected event append failure"):
        repository.reject_prepared_prompt_steer_resource_exhaustion(
            lease.guard,
            candidate=candidate,
            deadline_monotonic=deadline,
        )
    repository.fail_event_append = False
    assert (
        repository.confirm_prepared_prompt_steer_resource_rejection(
            session_id=session_id,
            candidate=candidate,
            deadline_monotonic=deadline,
        ).kind
        is SteerResourceRejectionConfirmationKind.NONE
    )

    repository.reject_prepared_prompt_steer_resource_exhaustion(
        lease.guard,
        candidate=candidate,
        deadline_monotonic=deadline,
    )
    assert (
        repository.confirm_prepared_prompt_steer_resource_rejection(
            session_id=session_id,
            candidate=candidate,
            deadline_monotonic=deadline,
        ).kind
        is SteerResourceRejectionConfirmationKind.FULL
    )


def test_stage2_prompt_cancel_is_single_terminal_cas(
    stage2_migrated_postgres_database,
) -> None:
    repository = _repository(stage2_migrated_postgres_database)
    deadline = monotonic() + 30
    session_id = _name("session")
    lease = repository.acquire_host_writer(
        session_id=session_id,
        workspace_id=_name("workspace"),
        writer_owner_id=_name("host"),
        lease_seconds=30,
        deadline_monotonic=deadline,
    )
    item_id = _name("queue")
    _enqueue_prompt(
        repository,
        lease.guard,
        command_id=_name("command"),
        queue_item_id=item_id,
        client_submission_id=_name("submission"),
        delivery_mode=PromptDeliveryMode.NEW_TURN,
        target_turn_id=None,
        content=FrozenPromptContent.text('cancel me'),
        occurred_at=datetime.now(timezone.utc),
        actor_id="user",
        deadline_monotonic=deadline,
    )
    assert (
        repository.cancel_prompt(
            lease.guard,
            queue_item_id=item_id,
            occurred_at=datetime.now(timezone.utc),
            actor_id="user",
            deadline_monotonic=deadline,
        )
        == "CANCELLED"
    )
    assert (
        repository.cancel_prompt(
            lease.guard,
            queue_item_id=item_id,
            occurred_at=datetime.now(timezone.utc),
            actor_id="user",
            deadline_monotonic=deadline,
        )
        == "CANCELLED"
    )
    assert not repository.has_pending_prompt(
        session_id=session_id, deadline_monotonic=deadline
    )
    with repository.connection_provider.connection(
        lane=PostgresConnectionLane.INSPECTOR,
        deadline_monotonic=deadline,
    ) as connection:
        assert connection.execute(
            """
            SELECT count(*) FROM pulsara_v3.agent_events
            WHERE session_id = %s AND event_type = 'PromptCancelled'
            """,
            (session_id,),
        ).fetchone() == (1,)


def test_stage2_memory_candidate_and_tool_result_are_one_transaction(
    stage2_migrated_postgres_database,
) -> None:
    repository = _repository(stage2_migrated_postgres_database)
    deadline = monotonic() + 30
    session_id = _name("session")
    workspace_id = _name("workspace")
    lease = repository.acquire_host_writer(
        session_id=session_id,
        workspace_id=workspace_id,
        writer_owner_id=_name("host"),
        lease_seconds=30,
        deadline_monotonic=deadline,
    )
    turn_id = _name("turn")
    _start_root_turn(
        repository,
        lease.guard,
        command_id=_name("command"),
        turn_id=turn_id,
        entry_id=_name("entry"),
        context_binding_revision_id=_name("revision"),
        content=FrozenPromptContent.text('remember this'),
        occurred_at=datetime.now(timezone.utc),
        deadline_monotonic=deadline,
    )
    cut = repository.prepare_provider_input_cut(
        lease.guard, turn_id=turn_id, deadline_monotonic=deadline
    )
    assistant_entry_id = _name("entry")
    first_call = _name("call")
    second_call = _name("call")
    repository.commit_assistant_message(
        lease.guard,
        cut=cut,
        entry_id=assistant_entry_id,
        parent_content=InlineContent.from_bytes(b"memory proposals"),
        blocks=(
            AssistantToolCallBlock(
                _name("block"),
                first_call,
                "remember_claim",
                freeze_json({"statement": "a"}),
            ),
            AssistantToolCallBlock(
                _name("block"),
                second_call,
                "remember_claim",
                freeze_json({"statement": "b"}),
            ),
        ),
        occurred_at=datetime.now(timezone.utc),
        actor_id="model:test",
        deadline_monotonic=deadline,
    )
    first_attempt = _accept_tool_attempt(
        repository,
        lease.guard,
        attempt_id=_name("attempt"),
        assistant_entry_id=assistant_entry_id,
        tool_call_id=first_call,
        authorization_kind="policy",
        authorization_reference="allow",
        actor_kind="runtime",
        actor_id="tool",
        remote_idempotency_key=None,
        retry_of_attempt_id=None,
        occurred_at=datetime.now(timezone.utc),
        deadline_monotonic=deadline,
    )
    candidate_id = _name("candidate")
    memory_candidate = prepare_memory_candidate(
        candidate_id=candidate_id,
        memory_domain_id="u_local",
        origin_workspace_id=workspace_id,
        origin_session_id=session_id,
        producer_entry_id=assistant_entry_id,
        producer_tool_call_id=first_call,
        proposal=FrozenMemoryProposal(
            statement="a",
            context_id=CTX_GLOBAL,
            kind_hint=MemoryKindHint.FACT,
        ),
    )
    first_result_entry_id = _name("entry")
    first_candidate = build_prepared_tool_result_acceptance(
        guard=lease.guard,
        workspace_id=workspace_id,
        result_id=_name("result"),
        result_entry_id=first_result_entry_id,
        turn_id=turn_id,
        assistant_entry_id=assistant_entry_id,
        tool_call_id=first_call,
        attempt_id=first_attempt.attempt_id,
        result_state="SUCCESS",
        canonical_preview_content=InlineContent.from_bytes(b"proposed"),
        artifact_disposition=ToolOutputArtifactDisposition.NOT_REQUIRED,
        artifact_id=None,
        artifact_blob_descriptor=None,
        source_coverage=ToolOutputSourceCoverage.COMPLETE,
        display_kind=ToolResultDisplayKind.COMPLETE,
        source_coverage_reason=None,
        artifact_unavailability_reason=None,
        observed_at=datetime.now(timezone.utc),
        observation_duration_microseconds=None,
        observation_origin_kind=ToolObservationOrigin.BUILTIN,
        trusted_tool_reported_duration_microseconds=None,
        actor_id="remember_claim",
        memory_candidate=memory_candidate,
    )
    repository.accept_tool_result(
        lease.guard,
        candidate=first_candidate,
        deadline_monotonic=deadline,
    )
    second_attempt = _accept_tool_attempt(
        repository,
        lease.guard,
        attempt_id=_name("attempt"),
        assistant_entry_id=assistant_entry_id,
        tool_call_id=second_call,
        authorization_kind="policy",
        authorization_reference="allow",
        actor_kind="runtime",
        actor_id="tool",
        remote_idempotency_key=None,
        retry_of_attempt_id=None,
        occurred_at=datetime.now(timezone.utc),
        deadline_monotonic=deadline,
    )
    rolled_back_candidate = candidate_id
    conflicting_memory_candidate = prepare_memory_candidate(
        candidate_id=rolled_back_candidate,
        memory_domain_id="u_local",
        origin_workspace_id=workspace_id,
        origin_session_id=session_id,
        producer_entry_id=assistant_entry_id,
        producer_tool_call_id=second_call,
        proposal=FrozenMemoryProposal(
            statement="b",
            context_id=CTX_GLOBAL,
            kind_hint=MemoryKindHint.FACT,
        ),
    )
    rolled_back_result = _name("result")
    rollback_candidate = build_prepared_tool_result_acceptance(
        guard=lease.guard,
        workspace_id=workspace_id,
        result_id=rolled_back_result,
        result_entry_id=_name("entry"),
        turn_id=turn_id,
        assistant_entry_id=assistant_entry_id,
        tool_call_id=second_call,
        attempt_id=second_attempt.attempt_id,
        result_state="SUCCESS",
        canonical_preview_content=InlineContent.from_bytes(b"must rollback"),
        artifact_disposition=ToolOutputArtifactDisposition.NOT_REQUIRED,
        artifact_id=None,
        artifact_blob_descriptor=None,
        source_coverage=ToolOutputSourceCoverage.COMPLETE,
        display_kind=ToolResultDisplayKind.COMPLETE,
        source_coverage_reason=None,
        artifact_unavailability_reason=None,
        observed_at=datetime.now(timezone.utc),
        observation_duration_microseconds=None,
        observation_origin_kind=ToolObservationOrigin.BUILTIN,
        trusted_tool_reported_duration_microseconds=None,
        actor_id="remember_claim",
        memory_candidate=conflicting_memory_candidate,
    )
    with pytest.raises(Exception):
        repository.accept_tool_result(
            lease.guard,
            candidate=rollback_candidate,
            deadline_monotonic=deadline,
        )
    with repository.connection_provider.connection(
        lane=PostgresConnectionLane.INSPECTOR,
        deadline_monotonic=deadline,
    ) as connection:
        assert connection.execute(
            "SELECT count(*) FROM pulsara_v3.tool_results WHERE id = %s",
            (rolled_back_result,),
        ).fetchone() == (0,)
        assert connection.execute(
            "SELECT count(*) FROM pulsara_v3.memory_candidates WHERE id = %s",
            (candidate_id,),
        ).fetchone() == (1,)
