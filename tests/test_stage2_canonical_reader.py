from __future__ import annotations

from datetime import datetime, timezone
from hashlib import sha256
import json
from time import monotonic
from uuid import uuid4

import pytest

from pulsara_agent.conversation_kernel.contracts import (
    InlineContent,
)
from pulsara_agent.conversation_kernel.cancellation import stable_subagent_turn_id
from pulsara_agent.conversation_kernel.compaction.contracts import (
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
    SubagentCompletionDisposition,
    build_prepared_tool_result_acceptance,
)
from pulsara_agent.ports.artifact import (
    ToolOutputArtifactDisposition,
    ToolResultDisplayKind,
)
from pulsara_agent.ports.tool_execution import ToolOutputSourceCoverage
from pulsara_agent.conversation_kernel.safe_point import (
    ExternalSourceNotAtSafePoint,
    ProviderSafePointCoordinator,
)
from pulsara_agent.conversation_kernel.subagents.contracts import (
    SubagentResultSource,
    SubagentTaskStatus,
    build_subagent_result_public_fact,
    build_subagent_task_terminal_settlement,
)
from pulsara_agent.model_input.contracts import (
    CanonicalInputOriginKind,
    ModelInputScopeKind,
)
from pulsara_agent.storage.postgres_connection_provider import PostgresConnectionLane
from pulsara_agent.terminal_protocol.canonical_v3 import CanonicalProtocolReader
from pulsara_agent.primitives.context import freeze_json
from pulsara_agent.primitives.permission import DEFAULT_PERMISSION_MODE
from pulsara_agent.primitives.tool_observation import ToolObservationOrigin
from pulsara_agent.primitives.tool_result_projection import (
    ToolResultDeliveryRequirement,
    ToolResultFullDeliveryReason,
)
from tests.support.postgres import verified_postgres_provider
from tests.support.subagents import accept_active_subagent_fixture


pytestmark = pytest.mark.postgres


def _id(prefix: str) -> str:
    return f"{prefix}:{uuid4().hex}"


def _start_turn(repository, lease, text: bytes):
    turn_id = _id("turn")
    repository.start_root_turn(
        lease.guard,
        command_id=_id("command"),
        turn_id=turn_id,
        entry_id=_id("entry"),
        context_binding_revision_id=_id("revision"),
        permission_snapshot_id=_id("permission-snapshot"),
        requested_permission_mode=DEFAULT_PERMISSION_MODE,
        content=InlineContent.from_bytes(text),
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
    assert "late-success" in materialized.items[5].text


def test_reader_lowers_no_attempt_as_interrupted_before_dispatch(
    stage2_migrated_postgres_database,
) -> None:
    provider = verified_postgres_provider(stage2_migrated_postgres_database.runtime_dsn)
    repository = ConversationKernelRepository(provider)
    lease = repository.acquire_host_writer(
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
                        "artifact_id": "artifact:test",
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
    assert page.tool_result_delivery.reason is ToolResultFullDeliveryReason.ARTIFACT_PAGE


def test_reader_rejects_declared_bytes_before_loading_any_payload(
    stage2_migrated_postgres_database,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = verified_postgres_provider(stage2_migrated_postgres_database.runtime_dsn)
    repository = ConversationKernelRepository(provider)
    lease = repository.acquire_host_writer(
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


def test_reader_has_an_independent_bounded_assistant_block_query(
    stage2_migrated_postgres_database,
) -> None:
    provider = verified_postgres_provider(stage2_migrated_postgres_database.runtime_dsn)
    repository = ConversationKernelRepository(provider)
    lease = repository.acquire_host_writer(
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
    assert assistant.text == ("semantic answer" if include_text else "")
    assert "MUST_NOT_REACH_PROVIDER" not in assistant.text
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
            scope_subagent_task_id=None,
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
            recent_user_messages=(),
            continuation_mode=CompactionContinuationMode.RESUME_ACTIVE_TURN,
            active_request=FrozenCompactionActiveRequest(
                entry_id=str(active_item.source_entry_id),
                entry_sequence=active_item.source_entry_sequence,
                location=CompactionActiveRequestLocation.CANONICAL_SUFFIX,
                text=None,
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
                binding_revision_id=_id("revision"),
                event_id=_id("compaction-event"),
                source_through_sequence=first_answer.entry_sequence,
                source_digest=canonical_compaction_range_digest(
                    compaction_read.lineage_base,
                    source_range,
                ),
                snapshot_content=InlineContent.from_bytes(snapshot_carrier.body),
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
    assert materialized.items[0].text == snapshot_carrier.body.decode("utf-8")
    assert materialized.items[1].text == "current question"


def test_subagent_completion_linearizes_at_provider_safe_point(
    stage2_migrated_postgres_database,
) -> None:
    provider = verified_postgres_provider(stage2_migrated_postgres_database.runtime_dsn)
    repository = ConversationKernelRepository(provider)
    lease = repository.acquire_host_writer(
        session_id=_id("session"),
        workspace_id=_id("workspace"),
        writer_owner_id=_id("host"),
        lease_seconds=30,
        deadline_monotonic=monotonic() + 30,
    )
    root_turn = _start_turn(repository, lease, b"delegate this")
    task_id = accept_active_subagent_fixture(
        repository,
        lease,
        parent_turn_id=root_turn,
        objective="return one exact result",
    )
    child_turn = _id("turn")
    repository.start_subagent_turn(
        lease.guard,
        task_id=task_id,
        turn_id=child_turn,
        entry_id=_id("entry"),
        context_binding_revision_id=_id("revision"),
        task_start_event_id=task_id.launch.task_start.event_id,
        expected_parent_permission_snapshot=(
            task_id.launch.parent_permission_snapshot
        ),
        content=InlineContent.from_bytes(b"return one exact result"),
        occurred_at=datetime.now(timezone.utc),
        actor_id="subagent:test",
        deadline_monotonic=monotonic() + 30,
    )

    safe_point = ProviderSafePointCoordinator(repository=repository, guard=lease.guard)
    first_handle = safe_point.freeze_provider_input(
        turn_id=root_turn, deadline_monotonic=monotonic() + 30
    )
    child_cut = repository.prepare_provider_input_cut(
        lease.guard,
        turn_id=child_turn,
        deadline_monotonic=monotonic() + 30,
    )
    child_result_id = _id("subagent-result")
    child_reply_entry_id = _id("entry")
    child_result_text = "the exact child result"
    child_result = build_subagent_result_public_fact(
        task_id=task_id,
        result_id=child_result_id,
        source=SubagentResultSource.INFERRED,
        producer_entry_id=child_reply_entry_id,
        summary=child_result_text,
        source_assistant_content_digest=(
            "sha256:" + sha256(child_result_text.encode("utf-8")).hexdigest()
        ),
    )
    child_reply = repository.commit_assistant_message(
        lease.guard,
        cut=child_cut,
        entry_id=child_reply_entry_id,
        parent_content=InlineContent.from_bytes(child_result_text.encode("utf-8")),
        blocks=(
            AssistantTextBlock(
                block_id=_id("block"),
                text=InlineContent.from_bytes(child_result_text.encode("utf-8")),
            ),
        ),
        complete_turn=True,
        subagent_result=child_result,
        occurred_at=datetime.now(timezone.utc),
        actor_id="model:child",
        deadline_monotonic=monotonic() + 30,
    )
    assert child_reply.turn_id == child_turn

    # The source domain can finish while a provider handle is active, but it
    # cannot splice a new ROOT entry behind that handle's fixed cut.
    command_id = _id("command")
    with pytest.raises(ExternalSourceNotAtSafePoint):
        safe_point.accept_subagent_completion(
            turn_id=root_turn,
            task_id=task_id,
            command_id=command_id,
            actor_id="host:test",
            deadline_monotonic=monotonic() + 30,
        )
    with provider.connection(
        lane=PostgresConnectionLane.INSPECTOR,
        deadline_monotonic=monotonic() + 30,
    ) as connection:
        assert (
            connection.execute(
                """
            SELECT count(*) FROM pulsara_v3.transcript_entries
            WHERE session_id = %s AND source_subagent_task_id = %s
            """,
                (lease.guard.session_id, task_id),
            ).fetchone()[0]
            == 0
        )
    first_handle.close()

    assert repository.interrupt_turn(
        lease.guard,
        turn_id=root_turn,
        reason="PARENT_ALREADY_TERMINAL",
        occurred_at=datetime.now(timezone.utc),
        actor_id="host:test",
        deadline_monotonic=monotonic() + 30,
    )
    new_root_turn = _id("turn")
    new_revision = _id("revision")
    accepted = safe_point.accept_subagent_completion(
        turn_id=new_root_turn,
        new_context_binding_revision_id=new_revision,
        requested_permission_mode=DEFAULT_PERMISSION_MODE,
        task_id=task_id,
        command_id=command_id,
        actor_id="host:test",
        deadline_monotonic=monotonic() + 30,
    )
    assert accepted.entry is not None

    second_handle = safe_point.freeze_provider_input(
        turn_id=new_root_turn, deadline_monotonic=monotonic() + 30
    )
    try:
        materialized = CanonicalProviderInputReader(provider).read_frozen_snapshot(
            second_handle.cut, deadline_monotonic=monotonic() + 30
        )
        envelope = json.loads(materialized.items[-1].text)[
            "pulsara_inter_agent_message"
        ]
        assert envelope["message_type"] == "FINAL_ANSWER"
        assert envelope["sender"] == {"kind": "SUBAGENT_TASK", "task_id": task_id}
        assert envelope["content"]["result"]["summary"] == child_result_text
        assert (
            materialized.items[-1].input_origin
            is CanonicalInputOriginKind.INTER_AGENT_MESSAGE
        )
    finally:
        second_handle.close()

    compatible = safe_point.accept_subagent_completion(
        turn_id=new_root_turn,
        new_context_binding_revision_id=new_revision,
        requested_permission_mode=DEFAULT_PERMISSION_MODE,
        task_id=task_id,
        command_id=command_id,
        actor_id="host:test",
        deadline_monotonic=monotonic() + 30,
    )
    assert compatible == accepted
    protocol_snapshot = CanonicalProtocolReader(provider).snapshot(
        session_id=lease.guard.session_id,
        maximum_entries=32,
        maximum_control_items=32,
        deadline_monotonic=monotonic() + 30,
    )
    accepted_projection = next(
        entry
        for entry in protocol_snapshot.entries
        if entry.entry_id == accepted.entry.entry_id
    )
    assert accepted_projection.source_subagent_task_id == task_id
    with provider.connection(
        lane=PostgresConnectionLane.INSPECTOR,
        deadline_monotonic=monotonic() + 30,
    ) as connection:
        assert (
            connection.execute(
                """
            SELECT count(*) FROM pulsara_v3.transcript_entries
            WHERE session_id = %s AND source_subagent_task_id = %s
            """,
                (lease.guard.session_id, task_id),
            ).fetchone()[0]
            == 1
        )


def test_failed_completion_automatic_manual_and_ack_retry_share_one_writer(
    stage2_migrated_postgres_database,
) -> None:
    provider = verified_postgres_provider(stage2_migrated_postgres_database.runtime_dsn)
    repository = ConversationKernelRepository(provider)
    workspace_id = _id("workspace")
    lease = repository.acquire_host_writer(
        session_id=_id("session"),
        workspace_id=workspace_id,
        writer_owner_id=_id("host"),
        lease_seconds=30,
        deadline_monotonic=monotonic() + 30,
    )
    root_turn = _start_turn(repository, lease, b"delegate and keep working")

    def failed_task(detail: str) -> str:
        task_id = accept_active_subagent_fixture(
            repository,
            lease,
            parent_turn_id=root_turn,
            objective="fail in a controlled way",
        )
        candidate = build_subagent_task_terminal_settlement(
            session_id=lease.guard.session_id,
            workspace_id=workspace_id,
            writer_generation=lease.guard.writer_generation,
            task_id=task_id,
            expected_turn_id=stable_subagent_turn_id(
                session_id=lease.guard.session_id,
                task_id=task_id,
            ),
            status=SubagentTaskStatus.FAILED,
            reason="CHILD_EXECUTION_FAILED",
            public_detail=detail,
            require_absent_turn=True,
            occurred_at=datetime.now(timezone.utc),
            actor_id="runtime:test",
        )
        assert repository.accept_subagent_task_terminal_settlement(
            lease.guard,
            candidate=candidate,
            deadline_monotonic=monotonic() + 30,
        )
        return task_id

    task_id = failed_task("ProviderError: controlled worker failure")
    other_task_id = failed_task("ProviderError: another controlled failure")
    safe_point = ProviderSafePointCoordinator(repository=repository, guard=lease.guard)
    handle = safe_point.freeze_provider_input(
        turn_id=root_turn, deadline_monotonic=monotonic() + 30
    )
    automatic = safe_point.accept_queued_subagent_completion(
        handle,
        task_id=task_id,
        actor_id="runtime:test",
        deadline_monotonic=monotonic() + 30,
    )
    assert automatic.disposition is SubagentCompletionDisposition.CREATED
    rotated = safe_point.rotate_provider_input(
        handle,
        turn_id=root_turn,
        deadline_monotonic=monotonic() + 30,
    )
    try:
        snapshot = CanonicalProviderInputReader(provider).read_frozen_snapshot(
            rotated.cut, deadline_monotonic=monotonic() + 30
        )
        envelope = json.loads(snapshot.items[-1].text)[
            "pulsara_inter_agent_message"
        ]
        completion = envelope["content"]
        assert envelope["message_type"] == "FINAL_ANSWER"
        assert completion["status"] == "FAILED"
        assert completion["result"] is None
        assert completion["failure"] == {
            "code": "CHILD_EXECUTION_FAILED",
            "detail": "ProviderError: controlled worker failure",
            "failed_dependency_task_ids": [],
            "retryability": "NEW_TASK_ONLY",
            "next_action": (
                "Use the failure detail to recover locally or create a "
                "replacement task."
            ),
        }
        duplicate = safe_point.accept_queued_subagent_completion(
            rotated,
            task_id=task_id,
            actor_id="runtime:test",
            deadline_monotonic=monotonic() + 30,
        )
        assert (
            duplicate.disposition
            is SubagentCompletionDisposition.ALREADY_DELIVERED
        )
        assert duplicate.entry == automatic.entry
    finally:
        rotated.close()

    with provider.connection(
        lane=PostgresConnectionLane.INSPECTOR,
        deadline_monotonic=monotonic() + 30,
    ) as connection:
        assert connection.execute(
            "SELECT count(*) FROM pulsara_v3.session_commands WHERE session_id=%s",
            (lease.guard.session_id,),
        ).fetchone() == (1,)
        # The sole row is the original human prompt. Automatic completion did
        # not manufacture an internal command receipt.

    command_id = _id("command")
    manual_loser = safe_point.accept_subagent_completion(
        turn_id=root_turn,
        task_id=task_id,
        command_id=command_id,
        actor_id="user:test",
        deadline_monotonic=monotonic() + 30,
    )
    assert (
        manual_loser.disposition
        is SubagentCompletionDisposition.ALREADY_DELIVERED
    )
    assert manual_loser.entry == automatic.entry
    assert safe_point.accept_subagent_completion(
        turn_id=root_turn,
        task_id=task_id,
        command_id=command_id,
        actor_id="user:test",
        deadline_monotonic=monotonic() + 30,
    ) == manual_loser

    with pytest.raises(ConversationKernelConflict, match="command conflicts"):
        safe_point.accept_subagent_completion(
            turn_id=root_turn,
            task_id=other_task_id,
            command_id=command_id,
            actor_id="user:test",
            deadline_monotonic=monotonic() + 30,
        )
    with provider.connection(
        lane=PostgresConnectionLane.INSPECTOR,
        deadline_monotonic=monotonic() + 30,
    ) as connection:
        assert connection.execute(
            "SELECT count(*) FROM pulsara_v3.transcript_entries "
            "WHERE session_id=%s AND source_subagent_task_id=%s",
            (lease.guard.session_id, task_id),
        ).fetchone() == (1,)
        assert connection.execute(
            "SELECT count(*) FROM pulsara_v3.session_commands "
            "WHERE session_id=%s AND command_kind='ACCEPT_SUBAGENT_COMPLETION'",
            (lease.guard.session_id,),
        ).fetchone() == (1,)


def test_inspector_reads_canonical_rows_and_selective_events_from_one_kernel(
    stage2_migrated_postgres_database,
) -> None:
    provider = verified_postgres_provider(stage2_migrated_postgres_database.runtime_dsn)
    repository = ConversationKernelRepository(provider)
    lease = repository.acquire_host_writer(
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
    assert injected_health["execution_watchdogs"][
        "host_session_close_join_seconds"
    ] == 41.0
    assert "legacy_event_replay" not in health
    assert "oxigraph_enabled" not in health
