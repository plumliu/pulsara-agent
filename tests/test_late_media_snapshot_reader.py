"""Real PostgreSQL reproduction; no production mutation or user DB reset."""

from datetime import datetime, timezone
import json
from time import monotonic

import pytest

from tests.test_stage2_canonical_reader import _start_turn, _permission_fingerprint
from tests.test_kernel_image_input_k2_postgres import (
    _repository,
    _bound_session,
    _workspace,
    _image,
    _rows,
    _id,
)
from pulsara_agent.conversation_kernel.contracts import InlineContent
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
from pulsara_agent.conversation_kernel.prompt_content import (
    freeze_canonical_prompt,
    PROMPT_BODY_MEDIA_TYPE,
    PROMPT_BODY_CODEC,
)
from pulsara_agent.conversation_kernel.reader import (
    CanonicalProviderInputReader,
    ProviderInputItemKind,
)
from pulsara_agent.conversation_kernel.repository import (
    AssistantTextBlock,
    AssistantToolCallBlock,
    ConversationKernelConflict,
    build_prepared_tool_result_acceptance,
)
from pulsara_agent.conversation_kernel.safe_point import ProviderSafePointCoordinator
from pulsara_agent.llm.input import FrozenPromptContent, LLMTextPart, LLMImagePart
from pulsara_agent.model_input.contracts import (
    CanonicalInputOriginKind,
    FrozenProviderInputItemKind,
    ModelInputScopeKind,
    ToolAttachmentSource,
    ToolAttachmentSourceMember,
)
from pulsara_agent.model_input.compiler import _tool_attachment_message
from pulsara_agent.llm.adapters.openai.chat_completions import chat_semantic_wire_group
from pulsara_agent.llm.adapters.openai.responses import responses_semantic_wire_group
from pulsara_agent.ports.tool_execution import ToolOutputSourceCoverage
from pulsara_agent.primitives.context import freeze_json
from pulsara_agent.primitives.tool_observation import ToolObservationOrigin

pytestmark = pytest.mark.postgres


def adoption(repository, lease, turn_id, source_cut):
    provider = repository.connection_provider
    cut = repository.prepare_compaction_input_cut(
        lease.guard,
        turn_id=turn_id,
        allow_terminal=False,
        deadline_monotonic=monotonic() + 30,
    )
    read = CanonicalProviderInputReader(provider).read_frozen_compaction_cut(
        cut, deadline_monotonic=monotonic() + 30
    )
    scope = CompactionScope(
        session_id=lease.guard.session_id,
        workspace_id=_workspace(repository, lease),
        turn_id=turn_id,
        scope_kind=ModelInputScopeKind.ROOT,
    )
    source_range = freeze_compaction_canonical_range(
        scope=scope,
        effective_materialization_lineage_floor=read.lineage_base.effective_materialization_lineage_floor,
        source_through_sequence=source_cut,
        ordered_items=read.safe_head_range.ordered_items,
        closures=read.safe_head_range.closures,
        late_outcomes=read.safe_head_range.late_outcomes,
    )
    active = next(
        item
        for item in read.safe_head_range.ordered_items
        if item.source_entry_id
        == read.dispatch_read.compile_snapshot.canonical_input.identity.initial_entry_id
    )
    carrier = build_compaction_snapshot_carrier(
        summary=freeze_compaction_summary_output(
            "The earlier tool request was interrupted; its provider closure was observed.",
            maximum_utf8_bytes=200,
        ),
        recent_human_requests=(),
        continuation_mode=CompactionContinuationMode.RESUME_ACTIVE_TURN,
        active_request=FrozenCompactionActiveRequest(
            entry_id=active.source_entry_id,
            entry_sequence=active.source_entry_sequence,
            location=CompactionActiveRequestLocation.CANONICAL_SUFFIX,
            item_kind=FrozenProviderInputItemKind.USER,
            input_origin=CanonicalInputOriginKind.HUMAN_MESSAGE,
            content=None,
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
                base_kind="FULL_HISTORY"
                if read.lineage_base.snapshot_id is None
                else "SNAPSHOT",
                context_snapshot_id=read.lineage_base.snapshot_id,
                source_through_sequence=read.lineage_base.persisted_revision_genesis_marker,
            ),
            snapshot_id=_id("snapshot"),
            binding_revision_id=_id("binding"),
            event_id=_id("event"),
            source_through_sequence=source_cut,
            source_digest=canonical_compaction_range_digest(
                read.lineage_base, source_range
            ),
            snapshot_content=InlineContent.from_bytes(
                carrier.body,
                media_type=CONTEXT_SNAPSHOT_MEDIA_TYPE,
                codec=CONTEXT_SNAPSHOT_CODEC,
            ),
            snapshot_carrier=carrier,
            compiler_contract="compiler.v1",
            prompt_contract="prompt.v1",
            model_contract="model.v1",
            occurred_at=datetime.now(timezone.utc),
            actor_id="compactor",
        )
    )
    preconditions = CompactionCanonicalWritePreconditions(
        scope=scope,
        expected_turn_status="RUNNING",
        expected_safe_head=read.safe_head_range.source_through_sequence,
        provider_safe=True,
    )
    return candidate, preconditions


def setup(database, *, summarize_request=True, result_before_bridge=False):
    repository = _repository(database)
    lease, runtime, _ = _bound_session(repository)
    old = _start_turn(repository, lease, b"old image request")
    cut = repository.prepare_provider_input_cut(
        lease.guard, turn_id=old, deadline_monotonic=monotonic() + 30
    )
    request, call, attempt = _id("entry"), _id("call"), _id("attempt")
    arguments = {
        "target": {"kind": "PATH", "path": "/tmp/late.mp4"},
        "mode": "motion",
        "from": 0,
        "to": 1,
    }
    repository.commit_assistant_message(
        lease.guard,
        cut=cut,
        entry_id=request,
        parent_content=InlineContent.from_bytes(b"inspect image"),
        blocks=(
            AssistantToolCallBlock(
                block_id=_id("block"),
                tool_call_id=call,
                tool_name="read_video_frames",
                arguments=freeze_json(arguments),
            ),
        ),
        occurred_at=datetime.now(timezone.utc),
        actor_id="model:test",
        deadline_monotonic=monotonic() + 30,
    )
    repository.accept_tool_attempt(
        lease.guard,
        attempt_id=attempt,
        assistant_entry_id=request,
        tool_call_id=call,
        authorization_kind="policy",
        authorization_reference="allow",
        actor_kind="runtime",
        actor_id="read_video_frames",
        remote_idempotency_key=None,
        retry_of_attempt_id=None,
        permission_snapshot_fingerprint=_permission_fingerprint(repository, lease, old),
        occurred_at=datetime.now(timezone.utc),
        deadline_monotonic=monotonic() + 30,
    )

    def accept_result():
        image = _image(workspace_id=_workspace(repository, lease))
        body = json.dumps(
            {
                "video_frames": True,
                "frames": [{"n": 1, "image": 1, "t": 0.125}],
                "next": None,
            }
        )
        prompt = freeze_canonical_prompt(
            FrozenPromptContent((LLMTextPart(body), image)),
            workspace_id=_workspace(repository, lease),
        )
        result_entry = _id("entry")
        prepared = build_prepared_tool_result_acceptance(
            guard=lease.guard,
            workspace_id=_workspace(repository, lease),
            result_id=_id("result"),
            result_entry_id=result_entry,
            turn_id=old,
            assistant_entry_id=request,
            tool_call_id=call,
            attempt_id=attempt,
            result_state="SUCCESS",
            canonical_preview_content=InlineContent.from_bytes(
                prompt.body, media_type=PROMPT_BODY_MEDIA_TYPE, codec=PROMPT_BODY_CODEC
            ),
            canonical_prompt=prompt,
            output_body=None,
            source_coverage=ToolOutputSourceCoverage.COMPLETE,
            output_omitted=False,
            source_coverage_reason=None,
            output_unavailability_reason=None,
            observed_at=datetime.now(timezone.utc),
            observation_duration_microseconds=1,
            observation_origin_kind=ToolObservationOrigin.BUILTIN,
            trusted_tool_reported_duration_microseconds=None,
            actor_id="read_video_frames",
        )
        repository.accept_tool_result(
            lease.guard, candidate=prepared, deadline_monotonic=monotonic() + 30
        )
        return result_entry, image, body

    repository.interrupt_turn(
        lease.guard,
        turn_id=old,
        reason="HOST_CRASH",
        occurred_at=datetime.now(timezone.utc),
        actor_id="runtime",
        deadline_monotonic=monotonic() + 30,
    )
    if result_before_bridge:
        result_entry, image, body = accept_result()
    bridge = _start_turn(repository, lease, b"bridge observes original closure")
    bridge_cut = repository.prepare_provider_input_cut(
        lease.guard, turn_id=bridge, deadline_monotonic=monotonic() + 30
    )
    answer = repository.commit_assistant_message(
        lease.guard,
        cut=bridge_cut,
        entry_id=_id("entry"),
        parent_content=InlineContent.from_bytes(b"bridge answer"),
        blocks=(
            AssistantTextBlock(
                block_id=_id("block"), text=InlineContent.from_bytes(b"bridge answer")
            ),
        ),
        complete_turn=True,
        occurred_at=datetime.now(timezone.utc),
        actor_id="model:test",
        deadline_monotonic=monotonic() + 30,
    )
    current = _start_turn(repository, lease, b"current user request")
    if summarize_request:
        safe = ProviderSafePointCoordinator(repository=repository, guard=lease.guard)
        with safe.exclusive_safe_mutation():
            candidate, preconditions = adoption(
                repository, lease, current, answer.entry_sequence
            )
            winner = repository.adopt_context_snapshot(
                lease.guard,
                candidate=candidate,
                preconditions=preconditions,
                deadline_monotonic=monotonic() + 30,
            )
        assert winner.revision_ordinal == 1
    if not result_before_bridge:
        result_entry, image, body = accept_result()
    cut = repository.prepare_provider_input_cut(
        lease.guard, turn_id=current, deadline_monotonic=monotonic() + 30
    )
    frozen = CanonicalProviderInputReader(
        repository.connection_provider
    ).read_frozen_snapshot(cut, deadline_monotonic=monotonic() + 30)
    rows = _rows(
        repository,
        "SELECT id,entry_sequence,entry_kind FROM pulsara_v3.transcript_entries WHERE session_id=%s ORDER BY entry_sequence",
        (lease.guard.session_id,),
    )
    return (
        repository,
        lease,
        current,
        answer.entry_sequence,
        result_entry,
        image,
        body,
        frozen,
        rows,
    )


def test_normal_reader_retains_late_image_after_request_snapshot(
    isolated_migrated_postgres_database,
):
    repository, lease, current, floor, result, image, body, frozen, rows = setup(
        isolated_migrated_postgres_database
    )
    evidence = {
        "database": isolated_migrated_postgres_database.database_name,
        "snapshot_floor": floor,
        "rows": [dict(row) for row in rows],
        "item_kinds": [item.item_kind.value for item in frozen.items],
        "late_outcomes": [str(item.result_entry_id) for item in frozen.late_outcomes],
        "image_count": sum(
            isinstance(part, LLMImagePart)
            for item in frozen.items
            if isinstance(item.content, tuple)
            for part in item.content
        ),
        "expected_late_entry": result,
    }
    late = [
        item
        for item in frozen.items
        if item.item_kind is ProviderInputItemKind.LATE_TOOL_OUTCOME
    ]
    assert len(late) == 1, evidence
    assert late[0].source_entry_id == result
    assert late[0].tool_result_body_text == body
    assert json.loads(late[0].content[0].text)["result"] == body
    assert [part for part in late[0].content if isinstance(part, LLMImagePart)] == [
        image
    ]
    assert not any(
        item.item_kind is ProviderInputItemKind.ASSISTANT_TOOL_REQUEST
        for item in frozen.items
    )
    carrier = _tool_attachment_message(
        ToolAttachmentSource(
            late[0].tool_request_entry_id,
            (ToolAttachmentSourceMember(late[0].tool_call_ordinal, late[0]),),
        )
    )
    assert [part for part in carrier.content if isinstance(part, LLMImagePart)] == [
        image
    ]
    assert any(
        isinstance(part, LLMTextPart)
        and '"tool_image_source"' in part.text
        and "/tmp/late.mp4" in part.text
        for part in carrier.content
    )
    for wire in (
        chat_semantic_wire_group(carrier),
        responses_semantic_wire_group(carrier),
    ):
        assert str(wire).count("base64,") == 1


def test_new_compaction_can_keep_independent_late_image_in_tail(
    isolated_migrated_postgres_database,
):
    repository, lease, current, floor, result, image, body, frozen, rows = setup(
        isolated_migrated_postgres_database
    )
    safe = ProviderSafePointCoordinator(repository=repository, guard=lease.guard)
    with safe.exclusive_safe_mutation():
        candidate, preconditions = adoption(repository, lease, current, floor)
        evidence = {
            "database": isolated_migrated_postgres_database.database_name,
            "predecessor_floor": candidate.predecessor.effective_materialization_lineage_floor,
            "new_source_cut": candidate.snapshot.source_through_sequence,
            "safe_head": preconditions.expected_safe_head,
            "expected_late_entry": result,
        }
        try:
            winner = repository.adopt_context_snapshot(
                lease.guard,
                candidate=candidate,
                preconditions=preconditions,
                deadline_monotonic=monotonic() + 30,
            )
        except ConversationKernelConflict as error:
            evidence["conflict"] = str(error)
            raise

    assert winner.revision_ordinal == 2
    cut = repository.prepare_provider_input_cut(
        lease.guard, turn_id=current, deadline_monotonic=monotonic() + 30
    )
    after = CanonicalProviderInputReader(
        repository.connection_provider
    ).read_frozen_snapshot(cut, deadline_monotonic=monotonic() + 30)
    late = [
        item
        for item in after.items
        if item.item_kind is ProviderInputItemKind.LATE_TOOL_OUTCOME
    ]
    assert len(late) == 1 and late[0].source_entry_id == result
    assert late[0].tool_result_body_text == body
    assert [part for part in late[0].content if isinstance(part, LLMImagePart)] == [
        image
    ]
    assert not any(
        item.item_kind is ProviderInputItemKind.ASSISTANT_TOOL_REQUEST
        for item in after.items
    )


def test_first_snapshot_can_leave_an_already_closed_late_outcome_in_tail(
    isolated_migrated_postgres_database,
):
    repository, lease, current, floor, result, image, body, frozen, rows = setup(
        isolated_migrated_postgres_database, summarize_request=False
    )
    safe = ProviderSafePointCoordinator(repository=repository, guard=lease.guard)
    with safe.exclusive_safe_mutation():
        candidate, preconditions = adoption(repository, lease, current, floor)
        assert candidate.predecessor.base_kind == "FULL_HISTORY"
        assert candidate.predecessor.effective_materialization_lineage_floor == 0
        winner = repository.adopt_context_snapshot(
            lease.guard,
            candidate=candidate,
            preconditions=preconditions,
            deadline_monotonic=monotonic() + 30,
        )
    assert winner.revision_ordinal == 1
    cut = repository.prepare_provider_input_cut(
        lease.guard, turn_id=current, deadline_monotonic=monotonic() + 30
    )
    after = CanonicalProviderInputReader(
        repository.connection_provider
    ).read_frozen_snapshot(cut, deadline_monotonic=monotonic() + 30)
    late = [
        item
        for item in after.items
        if item.item_kind is ProviderInputItemKind.LATE_TOOL_OUTCOME
    ]
    assert len(late) == 1 and late[0].source_entry_id == result
    assert late[0].tool_result_body_text == body
    assert [part for part in late[0].content if isinstance(part, LLMImagePart)] == [
        image
    ]
    assert not any(
        item.item_kind is ProviderInputItemKind.ASSISTANT_TOOL_REQUEST
        for item in after.items
    )


def test_first_snapshot_still_rejects_splitting_an_ordinary_tool_result(
    isolated_migrated_postgres_database,
):
    repository, lease, current, floor, result, image, body, frozen, rows = setup(
        isolated_migrated_postgres_database,
        summarize_request=False,
        result_before_bridge=True,
    )
    request_sequence = next(
        row["entry_sequence"]
        for row in rows
        if row["entry_kind"] == "ASSISTANT_TOOL_REQUEST"
    )
    ordinary = [
        item
        for item in frozen.items
        if item.item_kind is ProviderInputItemKind.TOOL_RESULT
    ]
    assert len(ordinary) == 1 and ordinary[0].source_entry_id == result
    assert ordinary[0].tool_result_body_text == body
    safe = ProviderSafePointCoordinator(repository=repository, guard=lease.guard)
    with safe.exclusive_safe_mutation():
        candidate, preconditions = adoption(
            repository, lease, current, request_sequence
        )
        # FULL_HISTORY's turn-local stored genesis lies after this historical
        # request; it must not be mistaken for the semantic materialization floor.
        assert candidate.predecessor.source_through_sequence > request_sequence
        assert candidate.predecessor.effective_materialization_lineage_floor == 0
        with pytest.raises(ConversationKernelConflict, match="splits a tool group"):
            repository.adopt_context_snapshot(
                lease.guard,
                candidate=candidate,
                preconditions=preconditions,
                deadline_monotonic=monotonic() + 30,
            )
    assert (
        _rows(
            repository,
            "SELECT id FROM pulsara_v3.context_snapshots WHERE session_id=%s",
            (lease.guard.session_id,),
        )
        == []
    )
