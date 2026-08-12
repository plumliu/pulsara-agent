from __future__ import annotations

from datetime import datetime, timedelta, timezone
from time import monotonic
from uuid import uuid4

import pytest

from pulsara_agent.conversation_kernel.contracts import (
    InlineContent,
    JobSafetyClass,
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
    ExternalSourceNotAtSafePoint,
    ProviderSafePointCoordinator,
)
from pulsara_agent.model_input.contracts import CanonicalInputOriginKind
from pulsara_agent.storage.postgres_connection_provider import PostgresConnectionLane
from pulsara_agent.primitives.context import freeze_json
from tests.support.postgres import verified_postgres_provider


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
        content=InlineContent.from_bytes(text),
        occurred_at=datetime.now(timezone.utc),
        deadline_monotonic=monotonic() + 30,
    )
    return turn_id


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
        occurred_at=occurred_at,
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
        ProviderInputItemKind.LATE_TOOL_OUTCOME,
        ProviderInputItemKind.USER,
    ]
    assert materialized.closures[0].closure_kind is (
        ProviderToolResultClosureKind.INTERRUPTED_MAY_HAVE_PARTIALLY_EXECUTED
    )
    assert materialized.late_outcomes[0].result_entry_id == result_entry_id
    assert "late-success" in materialized.items[3].text


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
        ordinal = repository.adopt_context_snapshot(
            lease.guard,
            turn_id=current_turn,
            snapshot_id=_id("snapshot"),
            context_binding_revision_id=_id("revision"),
            source_through_sequence=first_answer.entry_sequence,
            source_digest="sha256:" + "1" * 64,
            compiler_contract="compiler.v1",
            prompt_contract="prompt.v1",
            model_contract="model.v1",
            content=InlineContent.from_bytes(b"summary of old history"),
            occurred_at=datetime.now(timezone.utc),
            actor_id="compactor",
            deadline_monotonic=monotonic() + 30,
        )
    assert ordinal == 1
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
    assert materialized.items[0].text == "summary of old history"
    assert materialized.items[1].text == "current question"


def test_subagent_result_acceptance_linearizes_at_provider_safe_point(
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
    task_id = _id("subagent-task")
    repository.accept_subagent_task(
        lease.guard,
        task_id=task_id,
        parent_turn_id=root_turn,
        objective="return one exact result",
        occurred_at=datetime.now(timezone.utc),
        actor_id="host:test",
        deadline_monotonic=monotonic() + 30,
    )
    assert repository.set_subagent_task_status(
        lease.guard,
        task_id=task_id,
        status="ACTIVE",
        reason=None,
        occurred_at=datetime.now(timezone.utc),
        actor_id="host:test",
        deadline_monotonic=monotonic() + 30,
    )
    child_turn = _id("turn")
    repository.start_subagent_turn(
        lease.guard,
        task_id=task_id,
        turn_id=child_turn,
        entry_id=_id("entry"),
        context_binding_revision_id=_id("revision"),
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
    child_reply = repository.commit_assistant_message(
        lease.guard,
        cut=child_cut,
        entry_id=_id("entry"),
        parent_content=InlineContent.from_bytes(b"the exact child result"),
        blocks=(
            AssistantTextBlock(
                block_id=_id("block"),
                text=InlineContent.from_bytes(b"the exact child result"),
            ),
        ),
        complete_turn=True,
        occurred_at=datetime.now(timezone.utc),
        actor_id="model:child",
        deadline_monotonic=monotonic() + 30,
    )
    assert child_reply.turn_id == child_turn
    child_result_id = _id("subagent-result")
    repository.accept_subagent_child(
        lease.guard,
        child_id=child_result_id,
        task_id=task_id,
        child_kind="RESULT",
        child_ordinal=1,
        entry_id=child_reply.entry_id,
        occurred_at=datetime.now(timezone.utc),
        actor_id="subagent:test",
        deadline_monotonic=monotonic() + 30,
    )
    assert repository.set_subagent_task_status(
        lease.guard,
        task_id=task_id,
        status="COMPLETED",
        reason=None,
        occurred_at=datetime.now(timezone.utc),
        actor_id="host:test",
        deadline_monotonic=monotonic() + 30,
    )

    # The source domain can finish while a provider handle is active, but it
    # cannot splice a new ROOT entry behind that handle's fixed cut.
    command_id = _id("command")
    with pytest.raises(ExternalSourceNotAtSafePoint):
        safe_point.accept_subagent_result(
            turn_id=root_turn,
            child_result_id=child_result_id,
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
            WHERE session_id = %s AND source_subagent_result_id = %s
            """,
                (lease.guard.session_id, child_result_id),
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
    accepted = safe_point.accept_subagent_result(
        turn_id=new_root_turn,
        new_context_binding_revision_id=new_revision,
        child_result_id=child_result_id,
        command_id=command_id,
        actor_id="host:test",
        deadline_monotonic=monotonic() + 30,
    )
    assert accepted is not None

    second_handle = safe_point.freeze_provider_input(
        turn_id=new_root_turn, deadline_monotonic=monotonic() + 30
    )
    try:
        materialized = CanonicalProviderInputReader(provider).read_frozen_snapshot(
            second_handle.cut, deadline_monotonic=monotonic() + 30
        )
        assert materialized.items[-1].text == "the exact child result"
        assert (
            materialized.items[-1].input_origin
            is CanonicalInputOriginKind.SUBAGENT_RESULT
        )
    finally:
        second_handle.close()

    compatible = safe_point.accept_subagent_result(
        turn_id=new_root_turn,
        new_context_binding_revision_id=new_revision,
        child_result_id=child_result_id,
        command_id=command_id,
        actor_id="host:test",
        deadline_monotonic=monotonic() + 30,
    )
    assert compatible == accepted
    with provider.connection(
        lane=PostgresConnectionLane.INSPECTOR,
        deadline_monotonic=monotonic() + 30,
    ) as connection:
        assert (
            connection.execute(
                """
            SELECT count(*) FROM pulsara_v3.transcript_entries
            WHERE session_id = %s AND source_subagent_result_id = %s
            """,
                (lease.guard.session_id, child_result_id),
            ).fetchone()[0]
            == 1
        )


def test_job_result_acceptance_is_explicit_idempotent_and_safe_point_bound(
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
    root_turn = _start_turn(repository, lease, b"wait for durable work")
    job_id = _id("job")
    repository.enqueue_job(
        lease.guard,
        job_id=job_id,
        handler_type="MEMORY_GOVERNANCE",
        intent_schema_version="memory_governance.v1",
        intent_payload={"candidate_id": _id("candidate")},
        automatic_intent_key=None,
        safety_class=JobSafetyClass.RETRY_SAFE,
        retry_policy_id="bounded-exponential",
        retry_policy_version=1,
        maximum_attempts=3,
        attempt_timeout_ms=30_000,
        provider_input_token_limit_per_attempt=16_000,
        provider_output_token_limit_per_attempt=1_024,
        next_eligible_at=datetime.now(timezone.utc) - timedelta(seconds=1),
        occurred_at=datetime.now(timezone.utc),
        deadline_monotonic=monotonic() + 30,
    )
    attempt = repository.claim_due_job(
        handler_type="MEMORY_GOVERNANCE",
        claim_owner_id=_id("worker"),
        lease_seconds=15,
        deadline_monotonic=monotonic() + 30,
    )
    assert attempt is not None and attempt.guard.job_id == job_id
    repository.settle_job_attempt(
        attempt.guard,
        terminal_status="SUCCEEDED",
        result_payload={"answer": "durable job result"},
        error_code=None,
        retryable=False,
        occurred_at=datetime.now(timezone.utc),
        deadline_monotonic=monotonic() + 30,
    )

    safe_point = ProviderSafePointCoordinator(repository=repository, guard=lease.guard)
    frozen = safe_point.freeze_provider_input(
        turn_id=root_turn, deadline_monotonic=monotonic() + 30
    )
    command_id = _id("command")
    with pytest.raises(ExternalSourceNotAtSafePoint):
        safe_point.accept_job_result(
            turn_id=root_turn,
            job_id=job_id,
            command_id=command_id,
            actor_id="host:test",
            deadline_monotonic=monotonic() + 30,
        )
    frozen.close()
    accepted = safe_point.accept_job_result(
        turn_id=root_turn,
        job_id=job_id,
        command_id=command_id,
        actor_id="host:test",
        deadline_monotonic=monotonic() + 30,
    )
    assert accepted is not None
    assert (
        safe_point.accept_job_result(
            turn_id=root_turn,
            job_id=job_id,
            command_id=command_id,
            actor_id="host:test",
            deadline_monotonic=monotonic() + 30,
        )
        == accepted
    )
    next_input = safe_point.freeze_provider_input(
        turn_id=root_turn, deadline_monotonic=monotonic() + 30
    )
    try:
        materialized = CanonicalProviderInputReader(provider).read_frozen_snapshot(
            next_input.cut, deadline_monotonic=monotonic() + 30
        )
        assert materialized.items[-1].text == '{"answer":"durable job result"}'
        assert (
            materialized.items[-1].input_origin is CanonicalInputOriginKind.JOB_RESULT
        )
    finally:
        next_input.close()


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
    assert health["runtime_limits"]["host_close_hard_ms"] == 5_000
    assert all(value > 0 for value in health["runtime_limits"].values())
    assert "legacy_event_replay" not in health
    assert "oxigraph_enabled" not in health
