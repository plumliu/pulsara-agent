from __future__ import annotations

from pulsara_agent.identity import random_id

from datetime import datetime, timezone
from hashlib import sha256
from time import monotonic

import pytest
from psycopg.rows import dict_row

from pulsara_agent.llm.input import FrozenPromptContent

from pulsara_agent.conversation_kernel.contracts import (
    InlineContent,
    PromptDeliveryMode,
)
from pulsara_agent.conversation_kernel.prompt_content import freeze_canonical_prompt
from pulsara_agent.conversation_kernel.compaction.contracts import (
    CONTEXT_SNAPSHOT_CODEC,
    CONTEXT_SNAPSHOT_MEDIA_TYPE,
    CompactionActiveRequestLocation,
    CompactionCanonicalAdoptionFactoryInput,
    CompactionCanonicalWritePreconditions,
    CompactionContinuationMode,
    CompactionScope,
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
)
from pulsara_agent.conversation_kernel.reader import (
    CanonicalProviderContinuityError,
    CanonicalProviderContinuityFailureKind,
    CanonicalProviderInputReader,
    ProviderInputItemKind,
    ProviderToolResultClosureKind,
    _RemainingReadBudget,
)
from pulsara_agent.conversation_kernel.query import CanonicalConversationQuery
from pulsara_agent.conversation_kernel.execution_watchdogs import (
    KernelExecutionWatchdogPolicy,
)
from pulsara_agent.conversation_kernel.repository import (
    AssistantTextBlock,
    AssistantToolCallBlock,
    ConversationKernelConflict,
    ConversationKernelRepository,
    build_prepared_tool_result_acceptance,
)
from pulsara_agent.ports.artifact import (
    ToolOutputArtifactDisposition,
    ToolResultDisplayKind,
)
from pulsara_agent.ports.tool_execution import ToolOutputSourceCoverage
from pulsara_agent.conversation_kernel.safe_point import (
    ProviderSafePointCoordinator,
)
from pulsara_agent.conversation_kernel.steer import (
    PreparedRootProviderInputAdmission,
    PreparedRootProviderInputCandidate,
)
from pulsara_agent.model_input.contracts import (
    CanonicalInputOriginKind,
    FrozenProviderInputItemKind,
    ModelInputScopeKind,
    provider_input_item_text,
)
from pulsara_agent.storage.postgres_connection_provider import PostgresConnectionLane
from pulsara_agent.terminal_protocol.canonical_v3 import (
    CanonicalProtocolReader,
    CanonicalQueueContentNotPending,
)
from pulsara_agent.primitives.context import freeze_json
from pulsara_agent.primitives.permission import DEFAULT_PERMISSION_MODE
from pulsara_agent.primitives.tool_observation import ToolObservationOrigin
from pulsara_agent.primitives.tool_result_projection import (
    ToolResultDeliveryRequirement,
    ToolResultFullDeliveryReason,
)
from tests.support.postgres import verified_postgres_provider
from tests.support.model_config import (
    bind_test_session,
    enqueue_test_prompt,
    start_test_root_turn,
    test_model_binding,
    test_model_runtime,
)


pytestmark = pytest.mark.postgres


def _root_provider_input_admission(
    candidate: PreparedRootProviderInputCandidate,
) -> PreparedRootProviderInputAdmission:
    admission = object.__new__(PreparedRootProviderInputAdmission)
    object.__setattr__(admission, "candidate", candidate)
    return admission


def _id(prefix: str) -> str:
    return random_id(prefix)


def _start_turn(repository, lease, text: bytes):
    turn_id = _id("turn")
    model_call_binding = bind_test_session(repository, lease)
    start_test_root_turn(
        repository,
        lease.guard,
        command_id=_id("command"),
        turn_id=turn_id,
        entry_id=_id("entry"),
        context_binding_revision_id=_id("binding"),
        permission_snapshot_id=_id("permission"),
        requested_permission_mode=DEFAULT_PERMISSION_MODE,
        model_call_binding=model_call_binding,
        content=FrozenPromptContent.text(text.decode("utf-8")),
        occurred_at=datetime.now(timezone.utc),
        deadline_monotonic=monotonic() + 30,
    )
    return turn_id


def _permission_fingerprint(repository, lease, turn_id: str) -> str:
    with repository.connection_provider.connection(
        lane=PostgresConnectionLane.INSPECTOR,
        deadline_monotonic=monotonic() + 30,
    ) as connection:
        row = connection.execute(
            """SELECT permission_snapshot_fingerprint
               FROM pulsara_v3.turns WHERE session_id = %s AND id = %s""",
            (lease.guard.session_id, turn_id),
        ).fetchone()
    assert row is not None
    return str(row[0])


def test_reader_dispatch_carrier_requires_real_read_and_is_one_shot(
    stage2_migrated_postgres_database,
) -> None:
    provider = verified_postgres_provider(stage2_migrated_postgres_database.runtime_dsn)
    repository = ConversationKernelRepository(provider)
    lease = repository.acquire_host_writer(
        intent="NEW",
        session_id=_id("session"),
        workspace_id=_id("workspace"),
        writer_owner_id=_id("host"),
        lease_seconds=30,
        deadline_monotonic=monotonic() + 30,
    )
    turn_id = _start_turn(repository, lease, b"owner-issued read")
    safe_point = ProviderSafePointCoordinator(repository=repository, guard=lease.guard)
    handle = safe_point.freeze_provider_input(
        turn_id=turn_id, deadline_monotonic=monotonic() + 30
    )
    try:
        reader = CanonicalProviderInputReader(provider)
        observation = reader.read_owner_issued_dispatch_observation(
            handle.cut, deadline_monotonic=monotonic() + 30
        )
        same_shape = reader.read_frozen_dispatch(
            handle.cut, deadline_monotonic=monotonic() + 30
        )
        assert observation.canonical_read == same_shape
        assert observation.canonical_read is not same_shape
        with pytest.raises(TypeError, match="physical owners"):
            safe_point.seal_provider_input_preparation_basis(
                handle=handle,
                canonical_observation=same_shape,  # type: ignore[arg-type]
                capability_observation=object(),  # type: ignore[arg-type]
                call_target=object(),  # type: ignore[arg-type]
            )
        assert observation.consume() is observation.canonical_read
        with pytest.raises(RuntimeError, match="already consumed"):
            observation.consume()
    finally:
        handle.close()


def test_protocol_reader_reauthorizes_only_pending_queue_content_in_session(
    stage2_migrated_postgres_database,
) -> None:
    provider = verified_postgres_provider(stage2_migrated_postgres_database.runtime_dsn)
    repository = ConversationKernelRepository(provider)
    deadline = monotonic() + 30
    workspace_id = _id("workspace")
    lease = repository.acquire_host_writer(
        intent="NEW",
        session_id=_id("session"),
        workspace_id=workspace_id,
        writer_owner_id=_id("host"),
        lease_seconds=30,
        deadline_monotonic=deadline,
    )
    queue_item_id = _id("queue")
    content = FrozenPromptContent.text("同文队列🙂")
    canonical_prompt = freeze_canonical_prompt(content)
    enqueue_test_prompt(
        repository,
        lease.guard,
        command_id=_id("command"),
        queue_item_id=queue_item_id,
        client_submission_id=random_id("command"),
        delivery_mode=PromptDeliveryMode.NEW_TURN,
        target_turn_id=None,
        permission_snapshot_id=_id("permission"),
        requested_permission_mode=DEFAULT_PERMISSION_MODE,
        model_call_binding=test_model_binding(test_model_runtime()),
        content=content,
        occurred_at=datetime.now(timezone.utc),
        actor_id="user",
        deadline_monotonic=deadline,
    )
    reader = CanonicalProtocolReader(provider)
    reference = reader.resolve_content_reference(
        session_id=lease.guard.session_id,
        queue_item_id=queue_item_id,
        deadline_monotonic=deadline,
    )
    assert bytes(reference["inline_content"]) == canonical_prompt.body
    assert reference["blob_id"] is None
    assert (
        reference["content_digest"]
        == "sha256:" + sha256(canonical_prompt.body).hexdigest()
    )
    assert reference["content_size"] == len(canonical_prompt.body)

    other = repository.acquire_host_writer(
        intent="NEW",
        session_id=_id("session"),
        workspace_id=_id("workspace"),
        writer_owner_id=_id("host"),
        lease_seconds=30,
        deadline_monotonic=deadline,
    )
    for session_id, target in (
        (other.guard.session_id, queue_item_id),
        (lease.guard.session_id, _id("queue")),
    ):
        with pytest.raises(KeyError):
            reader.resolve_content_reference(
                session_id=session_id,
                queue_item_id=target,
                deadline_monotonic=deadline,
            )
    with pytest.raises(ValueError, match="queue content has no block target"):
        reader.resolve_content_reference(
            session_id=lease.guard.session_id,
            queue_item_id=queue_item_id,
            block_id=_id("block"),
            deadline_monotonic=deadline,
        )

    assert (
        repository.cancel_prompt(
            lease.guard,
            queue_item_id=queue_item_id,
            occurred_at=datetime.now(timezone.utc),
            actor_id="user",
            deadline_monotonic=deadline,
        )
        == "CANCELLED"
    )
    with pytest.raises(CanonicalQueueContentNotPending) as terminal:
        reader.resolve_content_reference(
            session_id=lease.guard.session_id,
            queue_item_id=queue_item_id,
            deadline_monotonic=deadline,
        )
    assert terminal.value.queue_item_id == queue_item_id
    assert terminal.value.status == "CANCELLED"
    assert terminal.value.consumed_entry_id is None


def test_protocol_reader_rebinds_a_large_queue_blob_to_its_exact_consumed_entry(
    stage2_migrated_postgres_database,
) -> None:
    provider = verified_postgres_provider(stage2_migrated_postgres_database.runtime_dsn)
    repository = ConversationKernelRepository(provider)
    deadline = monotonic() + 30
    workspace_id = _id("workspace")
    lease = repository.acquire_host_writer(
        intent="NEW",
        session_id=_id("session"),
        workspace_id=workspace_id,
        writer_owner_id=_id("host"),
        lease_seconds=30,
        deadline_monotonic=deadline,
    )
    queue_item_id = _id("queue")
    content_bytes = ("大正文🙂\n" * 12_000).encode()
    content = FrozenPromptContent.text(content_bytes.decode("utf-8"))
    canonical_prompt = freeze_canonical_prompt(content)
    assert len(canonical_prompt.body) > 64 << 10
    enqueue_test_prompt(
        repository,
        lease.guard,
        command_id=_id("command"),
        queue_item_id=queue_item_id,
        client_submission_id=random_id("command"),
        delivery_mode=PromptDeliveryMode.NEW_TURN,
        target_turn_id=None,
        permission_snapshot_id=_id("permission"),
        requested_permission_mode=DEFAULT_PERMISSION_MODE,
        model_call_binding=test_model_binding(test_model_runtime()),
        content=content,
        occurred_at=datetime.now(timezone.utc),
        actor_id="user",
        deadline_monotonic=deadline,
    )
    reader = CanonicalProtocolReader(provider)
    pending = reader.resolve_content_reference(
        session_id=lease.guard.session_id,
        queue_item_id=queue_item_id,
        deadline_monotonic=deadline,
    )
    assert pending["inline_content"] is None
    assert pending["blob_id"] is not None
    assert (
        pending["content_digest"]
        == "sha256:" + sha256(canonical_prompt.body).hexdigest()
    )

    candidate = repository.prepare_prompt_head_consumption(
        session_id=lease.guard.session_id,
        occurred_at=datetime.now(timezone.utc),
        actor_id="runtime:test",
        deadline_monotonic=deadline,
    )
    assert candidate is not None and candidate.queue_item_id == queue_item_id
    consumed = repository.consume_prepared_prompt_head(
        lease.guard,
        candidate=candidate,
        provider_input_admission=_root_provider_input_admission(
            candidate.provider_input_candidate
        ),
        deadline_monotonic=deadline,
    )
    assert consumed is not None and consumed.accepted is not None

    with pytest.raises(CanonicalQueueContentNotPending) as terminal:
        reader.resolve_content_reference(
            session_id=lease.guard.session_id,
            queue_item_id=queue_item_id,
            deadline_monotonic=deadline,
        )
    assert terminal.value.status == "CONSUMED"
    assert terminal.value.consumed_entry_id == candidate.exact_initial_entry_id
    entry = reader.resolve_content_reference(
        session_id=lease.guard.session_id,
        entry_id=terminal.value.consumed_entry_id,
        deadline_monotonic=deadline,
    )
    assert entry["inline_content"] is None
    assert entry["blob_id"] is not None
    assert (
        entry["content_digest"] == "sha256:" + sha256(canonical_prompt.body).hexdigest()
    )
    assert entry["content_size"] == len(canonical_prompt.body)


class _BrokenBlobReader:
    def read_exact(self, **kwargs):
        del kwargs
        raise KeyError("missing")


def test_provider_reader_fails_closed_on_canonical_blob_and_utf8_integrity() -> None:
    reader = object.__new__(CanonicalProviderInputReader)
    reader._blob_reader = _BrokenBlobReader()  # type: ignore[attr-defined]
    blob_row = {
        "inline_content": None,
        "blob_id": "blob:missing",
        "content_size": 4,
        "content_digest": "sha256:" + "0" * 64,
    }
    with pytest.raises(CanonicalProviderContinuityError) as unavailable:
        reader._read_content(blob_row, deadline_monotonic=monotonic() + 1)
    assert unavailable.value.kind is (
        CanonicalProviderContinuityFailureKind.BLOB_UNAVAILABLE_OR_CORRUPT
    )

    from hashlib import sha256

    invalid_bytes = b"\xf0\x9f"
    invalid_utf8_row = {
        "inline_content": invalid_bytes,
        "blob_id": None,
        "content_size": len(invalid_bytes),
        "content_digest": "sha256:" + sha256(invalid_bytes).hexdigest(),
        "content_codec": "utf-8",
    }
    with pytest.raises(CanonicalProviderContinuityError) as invalid:
        reader._block_text(
            invalid_utf8_row,
            deadline_monotonic=monotonic() + 1,
        )
    assert invalid.value.kind is CanonicalProviderContinuityFailureKind.INVALID_UTF8


def test_provider_blob_hydration_consumes_one_monotonic_remaining_budget() -> None:
    from hashlib import sha256

    class CountingBlobReader:
        calls = 0

        def read_exact(self, **kwargs):
            del kwargs
            self.calls += 1
            return b"data"

    blob_reader = CountingBlobReader()
    reader = object.__new__(CanonicalProviderInputReader)
    reader._blob_reader = blob_reader  # type: ignore[attr-defined]
    row = {
        "inline_content": None,
        "blob_id": "blob:test",
        "content_size": 4,
        "content_digest": "sha256:" + sha256(b"data").hexdigest(),
    }
    budget = _RemainingReadBudget(4)
    assert (
        reader._read_content(
            row,
            deadline_monotonic=monotonic() + 1,
            remaining_bytes=budget,
        )
        == b"data"
    )
    with pytest.raises(
        ConversationKernelConflict, match="physical byte bound exceeded"
    ):
        reader._read_content(
            row,
            deadline_monotonic=monotonic() + 1,
            remaining_bytes=budget,
        )
    assert blob_reader.calls == 1


def test_reader_uses_exact_scope_and_lowers_late_result_without_replay(
    stage2_migrated_postgres_database,
) -> None:
    provider = verified_postgres_provider(stage2_migrated_postgres_database.runtime_dsn)
    repository = ConversationKernelRepository(provider)
    workspace_id = _id("workspace")
    lease = repository.acquire_host_writer(
        intent="NEW",
        session_id=_id("session"),
        workspace_id=workspace_id,
        writer_owner_id=_id("host"),
        lease_seconds=30,
        deadline_monotonic=monotonic() + 30,
    )
    old_turn = _start_turn(repository, lease, b"old request")
    old_cut = repository.prepare_provider_input_cut(
        lease.guard,
        turn_id=old_turn,
        deadline_monotonic=monotonic() + 30,
    )
    request_entry_id = _id("entry")
    call_id = _id("call")
    repository.commit_assistant_message(
        lease.guard,
        cut=old_cut,
        entry_id=request_entry_id,
        parent_content=InlineContent.from_bytes(b"tool request"),
        blocks=(
            AssistantToolCallBlock(
                block_id=_id("block"),
                tool_call_id=call_id,
                tool_name="terminal",
                arguments=freeze_json({"command": "true"}),
            ),
        ),
        occurred_at=datetime.now(timezone.utc),
        actor_id="model:test",
        deadline_monotonic=monotonic() + 30,
    )
    attempt_id = _id("attempt")
    repository.accept_tool_attempt(
        lease.guard,
        attempt_id=attempt_id,
        assistant_entry_id=request_entry_id,
        tool_call_id=call_id,
        authorization_kind="policy",
        authorization_reference="allow",
        actor_kind="runtime",
        actor_id="executor",
        remote_idempotency_key=None,
        retry_of_attempt_id=None,
        permission_snapshot_fingerprint=_permission_fingerprint(
            repository, lease, old_turn
        ),
        occurred_at=datetime.now(timezone.utc),
        deadline_monotonic=monotonic() + 30,
    )
    repository.interrupt_turn(
        lease.guard,
        turn_id=old_turn,
        reason="HOST_CRASH",
        occurred_at=datetime.now(timezone.utc),
        actor_id="runtime",
        deadline_monotonic=monotonic() + 30,
    )
    # Freeze a real provider cut before the late result exists.  The accepted
    # assistant attributes that exact cut to the old request, so later reads
    # must retain its provider-only closure and append the physical outcome as
    # a late observation instead of rewriting history.
    bridge_turn = _start_turn(repository, lease, b"bridge")
    bridge_cut = repository.prepare_provider_input_cut(
        lease.guard,
        turn_id=bridge_turn,
        deadline_monotonic=monotonic() + 30,
    )
    repository.commit_assistant_message(
        lease.guard,
        cut=bridge_cut,
        entry_id=_id("entry"),
        parent_content=InlineContent.from_bytes(b"bridge answer"),
        blocks=(
            AssistantTextBlock(
                block_id=_id("block"),
                text=InlineContent.from_bytes(b"bridge answer"),
            ),
        ),
        complete_turn=True,
        occurred_at=datetime.now(timezone.utc),
        actor_id="model:test",
        deadline_monotonic=monotonic() + 30,
    )
    result_entry_id = _id("entry")
    occurred_at = datetime.now(timezone.utc)
    candidate = build_prepared_tool_result_acceptance(
        guard=lease.guard,
        workspace_id=workspace_id,
        result_id=_id("result"),
        result_entry_id=result_entry_id,
        turn_id=old_turn,
        assistant_entry_id=request_entry_id,
        tool_call_id=call_id,
        attempt_id=attempt_id,
        result_state="SUCCESS",
        canonical_preview_content=InlineContent.from_bytes(b"late-success"),
        artifact_disposition=ToolOutputArtifactDisposition.NOT_REQUIRED,
        artifact_id=None,
        artifact_blob_descriptor=None,
        source_coverage=ToolOutputSourceCoverage.COMPLETE,
        display_kind=ToolResultDisplayKind.COMPLETE,
        source_coverage_reason=None,
        artifact_unavailability_reason=None,
        observed_at=occurred_at,
        observation_duration_microseconds=None,
        observation_origin_kind=ToolObservationOrigin.TERMINAL_PROCESS,
        trusted_tool_reported_duration_microseconds=None,
        actor_id="terminal",
    )
    repository.accept_tool_result(
        lease.guard,
        candidate=candidate,
        deadline_monotonic=monotonic() + 30,
    )
    current_turn = _start_turn(repository, lease, b"continue")
    cut = repository.prepare_provider_input_cut(
        lease.guard,
        turn_id=current_turn,
        deadline_monotonic=monotonic() + 30,
    )
    materialized = CanonicalProviderInputReader(provider).read_frozen_snapshot(
        cut, deadline_monotonic=monotonic() + 30
    )
    assert materialized.identity.conversation_scope_kind.value == "ROOT"
    assert [item.item_kind for item in materialized.items] == [
        ProviderInputItemKind.USER,
        ProviderInputItemKind.ASSISTANT_TOOL_REQUEST,
        ProviderInputItemKind.TOOL_RESULT_CLOSURE,
        ProviderInputItemKind.USER,
        ProviderInputItemKind.ASSISTANT,
        ProviderInputItemKind.LATE_TOOL_OUTCOME,
        ProviderInputItemKind.USER,
    ]
    assert materialized.closures[0].closure_kind is (
        ProviderToolResultClosureKind.INTERRUPTED_MAY_HAVE_PARTIALLY_EXECUTED
    )
    assert materialized.late_outcomes[0].result_entry_id == result_entry_id
    assert "late-success" in provider_input_item_text(materialized.items[5])


def test_reader_lowers_no_attempt_as_interrupted_before_dispatch(
    stage2_migrated_postgres_database,
) -> None:
    provider = verified_postgres_provider(stage2_migrated_postgres_database.runtime_dsn)
    repository = ConversationKernelRepository(provider)
    lease = repository.acquire_host_writer(
        intent="NEW",
        session_id=_id("session"),
        workspace_id=_id("workspace"),
        writer_owner_id=_id("host"),
        lease_seconds=30,
        deadline_monotonic=monotonic() + 30,
    )

    old_turn = _start_turn(repository, lease, b"old")
    cut = repository.prepare_provider_input_cut(
        lease.guard, turn_id=old_turn, deadline_monotonic=monotonic() + 30
    )
    repository.commit_assistant_message(
        lease.guard,
        cut=cut,
        entry_id=_id("entry"),
        parent_content=InlineContent.from_bytes(b"request"),
        blocks=(
            AssistantToolCallBlock(
                block_id=_id("block"),
                tool_call_id=_id("call"),
                tool_name="terminal",
                arguments=freeze_json({"command": "true"}),
            ),
        ),
        occurred_at=datetime.now(timezone.utc),
        actor_id="model:test",
        deadline_monotonic=monotonic() + 30,
    )
    repository.interrupt_turn(
        lease.guard,
        turn_id=old_turn,
        reason="HOST_CRASH",
        occurred_at=datetime.now(timezone.utc),
        actor_id="runtime",
        deadline_monotonic=monotonic() + 30,
    )
    new_turn = _start_turn(repository, lease, b"continue")
    materialized = CanonicalProviderInputReader(provider).read_frozen_snapshot(
        repository.prepare_provider_input_cut(
            lease.guard,
            turn_id=new_turn,
            deadline_monotonic=monotonic() + 30,
        ),
        deadline_monotonic=monotonic() + 30,
    )
    assert materialized.closures[0].closure_kind is (
        ProviderToolResultClosureKind.INTERRUPTED_BEFORE_DISPATCH
    )
    assert materialized.late_outcomes == ()


def test_round7_1_reader_rebuilds_artifact_page_full_requirement_from_exact_rows(
    stage2_migrated_postgres_database,
) -> None:
    provider = verified_postgres_provider(stage2_migrated_postgres_database.runtime_dsn)
    repository = ConversationKernelRepository(provider)
    workspace_id = _id("workspace")
    lease = repository.acquire_host_writer(
        intent="NEW",
        session_id=_id("session"),
        workspace_id=workspace_id,
        writer_owner_id=_id("host"),
        lease_seconds=30,
        deadline_monotonic=monotonic() + 30,
    )
    turn_id = _start_turn(repository, lease, b"read an artifact page")
    cut = repository.prepare_provider_input_cut(
        lease.guard,
        turn_id=turn_id,
        deadline_monotonic=monotonic() + 30,
    )
    assistant_entry_id = _id("entry")
    call_id = _id("call")
    repository.commit_assistant_message(
        lease.guard,
        cut=cut,
        entry_id=assistant_entry_id,
        parent_content=InlineContent.from_bytes(b"artifact request"),
        blocks=(
            AssistantToolCallBlock(
                block_id=_id("block"),
                tool_call_id=call_id,
                tool_name="artifact_read",
                arguments=freeze_json(
                    {
                        "target": {
                            "kind": "ARTIFACT",
                            "artifact_id": "artifact:3uxyc2nlsokzkts3xmlji4zimy",
                        },
                        "offset_chars": 0,
                        "max_chars": 20_000,
                    }
                ),
            ),
        ),
        occurred_at=datetime.now(timezone.utc),
        actor_id="model:test",
        deadline_monotonic=monotonic() + 30,
    )
    attempt_id = _id("attempt")
    repository.accept_tool_attempt(
        lease.guard,
        attempt_id=attempt_id,
        assistant_entry_id=assistant_entry_id,
        tool_call_id=call_id,
        authorization_kind="policy",
        authorization_reference="allow",
        actor_kind="runtime",
        actor_id="executor",
        remote_idempotency_key=None,
        retry_of_attempt_id=None,
        permission_snapshot_fingerprint=_permission_fingerprint(
            repository, lease, turn_id
        ),
        occurred_at=datetime.now(timezone.utc),
        deadline_monotonic=monotonic() + 30,
    )
    observed_at = datetime.now(timezone.utc)
    result = build_prepared_tool_result_acceptance(
        guard=lease.guard,
        workspace_id=workspace_id,
        result_id=_id("result"),
        result_entry_id=_id("entry"),
        turn_id=turn_id,
        assistant_entry_id=assistant_entry_id,
        tool_call_id=call_id,
        attempt_id=attempt_id,
        result_state="SUCCESS",
        canonical_preview_content=InlineContent.from_bytes(
            b'{"has_more":true,"text":"page"}'
        ),
        artifact_disposition=ToolOutputArtifactDisposition.NOT_REQUIRED,
        artifact_id=None,
        artifact_blob_descriptor=None,
        source_coverage=ToolOutputSourceCoverage.COMPLETE,
        display_kind=ToolResultDisplayKind.COMPLETE,
        source_coverage_reason=None,
        artifact_unavailability_reason=None,
        observed_at=observed_at,
        observation_duration_microseconds=1,
        observation_origin_kind=ToolObservationOrigin.BUILTIN,
        trusted_tool_reported_duration_microseconds=None,
        actor_id="runtime",
    )
    repository.accept_tool_result(
        lease.guard,
        candidate=result,
        deadline_monotonic=monotonic() + 30,
    )
    read_cut = repository.prepare_provider_input_cut(
        lease.guard,
        turn_id=turn_id,
        deadline_monotonic=monotonic() + 30,
    )
    snapshot = CanonicalProviderInputReader(provider).read_frozen_snapshot(
        read_cut,
        deadline_monotonic=monotonic() + 30,
    )
    page = next(
        item
        for item in snapshot.items
        if item.item_kind is ProviderInputItemKind.TOOL_RESULT
    )
    assert page.tool_result_delivery.requirement is (
        ToolResultDeliveryRequirement.FULL_REQUIRED
    )
    assert (
        page.tool_result_delivery.reason is ToolResultFullDeliveryReason.ARTIFACT_PAGE
    )


def test_reader_rejects_declared_bytes_before_loading_any_payload(
    stage2_migrated_postgres_database,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = verified_postgres_provider(stage2_migrated_postgres_database.runtime_dsn)
    repository = ConversationKernelRepository(provider)
    lease = repository.acquire_host_writer(
        intent="NEW",
        session_id=_id("session"),
        workspace_id=_id("workspace"),
        writer_owner_id=_id("host"),
        lease_seconds=30,
        deadline_monotonic=monotonic() + 30,
    )
    turn_id = _start_turn(repository, lease, b"five!")
    reader = CanonicalProviderInputReader(provider, maximum_canonical_bytes=4)

    def forbidden_payload_load(*args, **kwargs):
        del args, kwargs
        raise AssertionError("payload loaded before metadata size admission")

    monkeypatch.setattr(reader, "_load_entry_payloads", forbidden_payload_load)
    with pytest.raises(
        ConversationKernelConflict, match="physical byte bound exceeded"
    ):
        reader.read_frozen_snapshot(
            repository.prepare_provider_input_cut(
                lease.guard,
                turn_id=turn_id,
                deadline_monotonic=monotonic() + 30,
            ),
            deadline_monotonic=monotonic() + 30,
        )


def test_reader_preflights_plan_continuation_before_loading_any_payload(
    stage2_migrated_postgres_database,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from tests.test_round4_plan_postgres import (
        _commit_plan_batch,
        _fresh_plan_provider_input_admission,
    )

    provider = verified_postgres_provider(stage2_migrated_postgres_database.runtime_dsn)
    repository = ConversationKernelRepository(provider)
    workspace_id = _id("workspace")
    lease = repository.acquire_host_writer(
        intent="NEW",
        session_id=_id("session"),
        workspace_id=workspace_id,
        writer_owner_id=_id("host"),
        lease_seconds=30,
        deadline_monotonic=monotonic() + 30,
    )
    origin_turn_id = _start_turn(repository, lease, b"enter plan")
    prepared = _commit_plan_batch(
        repository,
        lease,
        workspace_id=workspace_id,
        turn_id=origin_turn_id,
        selected_tool_name="enter_plan",
        selected_arguments={"reason": "preflight plan continuation"},
        workflow_id=_id("plan"),
        expected_workflow_revision=None,
    )
    accepted = repository.accept_plan_tool_batch(
        lease.guard,
        candidate=prepared.candidate,
        continuation_provider_input_admission=(
            _fresh_plan_provider_input_admission(repository, lease, prepared.candidate)
        ),
        deadline_monotonic=monotonic() + 30,
    )
    assert accepted.continuation_turn_id is not None
    assert accepted.continuation_entry_id is not None
    cut = repository.prepare_provider_input_cut(
        lease.guard,
        turn_id=accepted.continuation_turn_id,
        deadline_monotonic=monotonic() + 30,
    )

    probe = CanonicalProviderInputReader(provider)
    with provider.connection(
        lane=PostgresConnectionLane.INSPECTOR,
        row_factory=dict_row,
        deadline_monotonic=monotonic() + 30,
    ) as connection:
        entries = tuple(
            connection.execute(
                """SELECT id, entry_kind, content_size
                   FROM pulsara_v3.transcript_entries
                   WHERE session_id = %s
                     AND entry_sequence > 0
                     AND entry_sequence <= %s
                   ORDER BY entry_sequence""",
                (lease.guard.session_id, cut.provider_input_through_sequence),
            ).fetchall()
        )
        blocks = probe._load_block_metadata(  # noqa: SLF001
            connection,
            lease.guard.session_id,
            tuple(str(row["id"]) for row in entries),
        )
    plan_rows = tuple(
        row for row in entries if str(row["entry_kind"]) == "PLAN_CONTINUATION"
    )
    assert tuple(str(row["id"]) for row in plan_rows) == (
        accepted.continuation_entry_id,
    )
    bytes_without_plan = sum(
        int(row["content_size"])
        for row in entries
        if row["entry_kind"]
        in (
            "USER_MESSAGE",
            "USER_STEER",
            "TERMINAL_OBSERVATION",
            "USER_CONTROL_FEEDBACK",
            "INTER_AGENT_MESSAGE",
            "TOOL_RESULT",
        )
    ) + sum(
        (
            max(int(row["content_size"]), int(row["inline_content_size"]))
            if row["block_kind"] in ("TEXT", "DATA")
            else int(row["tool_arguments_size"])
        )
        for row in blocks
    )
    assert bytes_without_plan > 0
    assert int(plan_rows[0]["content_size"]) > 0
    reader = CanonicalProviderInputReader(
        provider,
        maximum_canonical_bytes=bytes_without_plan,
    )

    def forbidden_payload_load(*args, **kwargs):
        del args, kwargs
        raise AssertionError("plan payload loaded before metadata size admission")

    monkeypatch.setattr(reader, "_load_entry_payloads", forbidden_payload_load)
    with pytest.raises(
        ConversationKernelConflict, match="physical byte bound exceeded"
    ):
        reader.read_frozen_snapshot(
            cut,
            deadline_monotonic=monotonic() + 30,
        )


def test_reader_has_an_independent_bounded_assistant_block_query(
    stage2_migrated_postgres_database,
) -> None:
    provider = verified_postgres_provider(stage2_migrated_postgres_database.runtime_dsn)
    repository = ConversationKernelRepository(provider)
    lease = repository.acquire_host_writer(
        intent="NEW",
        session_id=_id("session"),
        workspace_id=_id("workspace"),
        writer_owner_id=_id("host"),
        lease_seconds=30,
        deadline_monotonic=monotonic() + 30,
    )
    turn_id = _start_turn(repository, lease, b"bounded blocks")
    cut = repository.prepare_provider_input_cut(
        lease.guard,
        turn_id=turn_id,
        deadline_monotonic=monotonic() + 30,
    )
    repository.commit_assistant_message(
        lease.guard,
        cut=cut,
        entry_id=_id("entry"),
        parent_content=InlineContent.from_bytes(b"storage manifest"),
        blocks=tuple(
            AssistantTextBlock(
                block_id=_id("block"),
                text=InlineContent.from_bytes(f"part-{index}".encode()),
            )
            for index in range(3)
        ),
        occurred_at=datetime.now(timezone.utc),
        actor_id="model:test",
        deadline_monotonic=monotonic() + 30,
    )
    reader = CanonicalProviderInputReader(provider, maximum_items=2)
    with pytest.raises(ConversationKernelConflict, match="block bound exceeded"):
        reader.read_frozen_snapshot(
            repository.prepare_provider_input_cut(
                lease.guard,
                turn_id=turn_id,
                deadline_monotonic=monotonic() + 30,
            ),
            deadline_monotonic=monotonic() + 30,
        )


@pytest.mark.parametrize("include_text", [False, True])
def test_round3_reader_uses_ordered_semantic_blocks_not_parent_manifest(
    stage2_migrated_postgres_database,
    include_text: bool,
) -> None:
    provider = verified_postgres_provider(stage2_migrated_postgres_database.runtime_dsn)
    repository = ConversationKernelRepository(provider)
    lease = repository.acquire_host_writer(
        intent="NEW",
        session_id=_id("session"),
        workspace_id=_id("workspace"),
        writer_owner_id=_id("host"),
        lease_seconds=30,
        deadline_monotonic=monotonic() + 30,
    )
    turn_id = _start_turn(repository, lease, b"run tools")
    cut = repository.prepare_provider_input_cut(
        lease.guard, turn_id=turn_id, deadline_monotonic=monotonic() + 30
    )
    blocks = []
    if include_text:
        blocks.append(
            AssistantTextBlock(
                block_id=_id("block"),
                text=InlineContent.from_bytes(b"semantic answer"),
            )
        )
    blocks.extend(
        (
            AssistantToolCallBlock(
                block_id=_id("block"),
                tool_call_id="call:first",
                tool_name="terminal",
                arguments=freeze_json({"command": "first"}),
            ),
            AssistantToolCallBlock(
                block_id=_id("block"),
                tool_call_id="call:second",
                tool_name="terminal",
                arguments=freeze_json({"command": "second"}),
            ),
        )
    )
    repository.commit_assistant_message(
        lease.guard,
        cut=cut,
        entry_id=_id("entry"),
        parent_content=InlineContent.from_bytes(
            b'{"draft_identity":"MUST_NOT_REACH_PROVIDER","blocks":["'
            + (b"storage-carrier-only" * 512)
            + b'"]}'
        ),
        blocks=tuple(blocks),
        occurred_at=datetime.now(timezone.utc),
        actor_id="model:test",
        deadline_monotonic=monotonic() + 30,
    )
    materialized = CanonicalProviderInputReader(
        provider, maximum_canonical_bytes=4_096
    ).read_frozen_snapshot(
        repository.prepare_provider_input_cut(
            lease.guard,
            turn_id=turn_id,
            deadline_monotonic=monotonic() + 30,
        ),
        deadline_monotonic=monotonic() + 30,
    )
    assistant = next(
        item
        for item in materialized.items
        if item.item_kind is ProviderInputItemKind.ASSISTANT_TOOL_REQUEST
    )
    assert assistant.item_kind is ProviderInputItemKind.ASSISTANT_TOOL_REQUEST
    assistant_text = provider_input_item_text(assistant)
    assert assistant_text == ("semantic answer" if include_text else "")
    assert "MUST_NOT_REACH_PROVIDER" not in assistant_text
    assert [call.tool_call_id for call in assistant.tool_calls] == [
        "call:first",
        "call:second",
    ]
    assert [call.arguments for call in assistant.tool_calls] == [
        freeze_json({"command": "first"}),
        freeze_json({"command": "second"}),
    ]


def test_mid_turn_snapshot_revision_keeps_current_user_as_exact_delta(
    stage2_migrated_postgres_database,
) -> None:
    provider = verified_postgres_provider(stage2_migrated_postgres_database.runtime_dsn)
    repository = ConversationKernelRepository(provider)
    lease = repository.acquire_host_writer(
        intent="NEW",
        session_id=_id("session"),
        workspace_id=_id("workspace"),
        writer_owner_id=_id("host"),
        lease_seconds=30,
        deadline_monotonic=monotonic() + 30,
    )
    first_turn = _start_turn(repository, lease, b"old history")
    first_cut = repository.prepare_provider_input_cut(
        lease.guard, turn_id=first_turn, deadline_monotonic=monotonic() + 30
    )
    first_answer = repository.commit_assistant_message(
        lease.guard,
        cut=first_cut,
        entry_id=_id("entry"),
        parent_content=InlineContent.from_bytes(b"old answer"),
        blocks=(
            AssistantTextBlock(
                block_id=_id("block"), text=InlineContent.from_bytes(b"old answer")
            ),
        ),
        complete_turn=True,
        occurred_at=datetime.now(timezone.utc),
        actor_id="model:test",
        deadline_monotonic=monotonic() + 30,
    )
    assert first_answer.turn_id == first_turn
    current_turn = _start_turn(repository, lease, b"current question")
    safe_point = ProviderSafePointCoordinator(repository=repository, guard=lease.guard)
    with safe_point.exclusive_safe_mutation():
        compaction_cut = repository.prepare_compaction_input_cut(
            lease.guard,
            turn_id=current_turn,
            allow_terminal=False,
            deadline_monotonic=monotonic() + 30,
        )
        compaction_read = CanonicalProviderInputReader(
            provider
        ).read_frozen_compaction_cut(
            compaction_cut,
            deadline_monotonic=monotonic() + 30,
        )
        scope = CompactionScope(
            session_id=lease.guard.session_id,
            workspace_id=repository.read_session_workspace_id(
                lease.guard,
                deadline_monotonic=monotonic() + 30,
            ),
            turn_id=current_turn,
            scope_kind=ModelInputScopeKind.ROOT,
        )
        source_range = freeze_compaction_canonical_range(
            scope=scope,
            effective_materialization_lineage_floor=(
                compaction_read.lineage_base.effective_materialization_lineage_floor
            ),
            source_through_sequence=first_answer.entry_sequence,
            ordered_items=compaction_read.safe_head_range.ordered_items,
            closures=compaction_read.safe_head_range.closures,
            late_outcomes=compaction_read.safe_head_range.late_outcomes,
        )
        active_item = next(
            item
            for item in compaction_read.safe_head_range.ordered_items
            if item.source_entry_id
            == compaction_read.dispatch_read.compile_snapshot.canonical_input.identity.initial_entry_id
        )
        assert active_item.source_entry_sequence is not None
        snapshot_carrier = build_compaction_snapshot_carrier(
            summary=freeze_compaction_summary_output(
                "summary of old history", maximum_utf8_bytes=100
            ),
            recent_human_requests=(),
            continuation_mode=CompactionContinuationMode.RESUME_ACTIVE_TURN,
            active_request=FrozenCompactionActiveRequest(
                entry_id=str(active_item.source_entry_id),
                entry_sequence=active_item.source_entry_sequence,
                location=CompactionActiveRequestLocation.CANONICAL_SUFFIX,
                item_kind=FrozenProviderInputItemKind.USER,
                input_origin=CanonicalInputOriginKind.HUMAN_MESSAGE,
                content=None,
            ),
        )
        adoption = build_prepared_compaction_canonical_adoption(
            CompactionCanonicalAdoptionFactoryInput(
                scope=scope,
                target_branch=CompactionTargetBranch.ACTIVE_INSTALLATION,
                expected_turn_status="RUNNING",
                predecessor=ExpectedCompactionPredecessorRevision(
                    binding_revision_id=(
                        compaction_read.lineage_base.binding_revision_id
                    ),
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
                snapshot_id=_id("snapshot"),
                binding_revision_id=_id("binding"),
                event_id=_id("event"),
                source_through_sequence=first_answer.entry_sequence,
                source_digest=canonical_compaction_range_digest(
                    compaction_read.lineage_base,
                    source_range,
                ),
                snapshot_content=InlineContent.from_bytes(
                    snapshot_carrier.body,
                    media_type=CONTEXT_SNAPSHOT_MEDIA_TYPE,
                    codec=CONTEXT_SNAPSHOT_CODEC,
                ),
                snapshot_carrier=snapshot_carrier,
                compiler_contract="compiler.v1",
                prompt_contract="prompt.v1",
                model_contract="model.v1",
                occurred_at=datetime.now(timezone.utc),
                actor_id="compactor",
            )
        )
        winner = repository.adopt_context_snapshot(
            lease.guard,
            candidate=adoption,
            preconditions=CompactionCanonicalWritePreconditions(
                scope=scope,
                expected_turn_status="RUNNING",
                expected_safe_head=(
                    compaction_read.safe_head_range.source_through_sequence
                ),
                provider_safe=True,
            ),
            deadline_monotonic=monotonic() + 30,
        )
    assert winner.revision_ordinal == 1
    protocol_snapshot = CanonicalProtocolReader(provider).snapshot(
        session_id=lease.guard.session_id,
        maximum_entries=32,
        maximum_control_items=32,
        deadline_monotonic=monotonic() + 30,
    )
    assert protocol_snapshot.control.HasField("latest_context_compaction")
    compaction_control = protocol_snapshot.control.latest_context_compaction
    assert compaction_control.turn_id == current_turn
    assert (
        compaction_control.context_binding_revision_id
        == adoption.binding.binding_revision_id
    )
    assert compaction_control.source_through_sequence == first_answer.entry_sequence
    assert (
        compaction_control.adopted_after_entry_sequence
        == active_item.source_entry_sequence
    )
    assert compaction_control.accepted_at_utc
    prepared = safe_point.freeze_provider_input(
        turn_id=current_turn, deadline_monotonic=monotonic() + 30
    )
    try:
        materialized = CanonicalProviderInputReader(provider).read_frozen_snapshot(
            prepared.cut, deadline_monotonic=monotonic() + 30
        )
    finally:
        prepared.close()
    assert [item.item_kind for item in materialized.items] == [
        ProviderInputItemKind.CONTEXT_SNAPSHOT,
        ProviderInputItemKind.USER,
    ]
    assert materialized.items[0].content == snapshot_carrier
    assert provider_input_item_text(materialized.items[1]) == "current question"


def test_inspector_reads_canonical_rows_and_selective_events_from_one_kernel(
    stage2_migrated_postgres_database,
) -> None:
    provider = verified_postgres_provider(stage2_migrated_postgres_database.runtime_dsn)
    repository = ConversationKernelRepository(provider)
    lease = repository.acquire_host_writer(
        intent="NEW",
        session_id=_id("session"),
        workspace_id=_id("workspace"),
        writer_owner_id=_id("host"),
        lease_seconds=30,
        deadline_monotonic=monotonic() + 30,
    )
    turn_id = _start_turn(repository, lease, b"inspect canonical truth")
    query = CanonicalConversationQuery(provider)
    view = query.inspect(
        session_id=lease.guard.session_id,
        maximum_entries=32,
        maximum_events=32,
        deadline_monotonic=monotonic() + 30,
    ).to_dict()
    assert view["inspect_kind"] == "canonical_conversation_kernel.v3"
    assert view["conversation"]["entries"][0]["turn_id"] == turn_id
    assert view["turns"][0]["status"] == "RUNNING"
    assert view["selective_events"][0]["event_type"] == "UserMessageAccepted"
    health = query.inspect_health(deadline_monotonic=monotonic() + 30)
    assert health["conversation_authority"] == "pulsara_v3"
    assert health["runtime_limit_contract"] == "stage2_runtime_limits.v1"
    assert "host_close_hard_ms" not in health["runtime_limits"]
    assert "model_calls_per_turn_hard" not in health["runtime_limits"]
    assert all(value > 0 for value in health["runtime_limits"].values())
    assert health["execution_watchdog_contract"] == "kernel_execution_watchdogs.v1"
    watchdogs = health["execution_watchdogs"]
    assert watchdogs["host_session_close_join_seconds"] == 120.0
    assert watchdogs["foreground_provider_total_seconds"] is None
    assert watchdogs["model_calls_per_turn"] is None
    assert watchdogs["tool_calls_per_turn"] is None
    assert watchdogs["turn_total_seconds"] is None
    injected_health = CanonicalConversationQuery(
        provider,
        watchdog_policy=KernelExecutionWatchdogPolicy(
            host_session_close_join_seconds=41.0
        ),
    ).inspect_health(deadline_monotonic=monotonic() + 30)
    assert (
        injected_health["execution_watchdogs"]["host_session_close_join_seconds"]
        == 41.0
    )
    assert "legacy_event_replay" not in health
    assert "oxigraph_enabled" not in health
