"""The first input group stays exact while only its predecessor is compacted."""

from __future__ import annotations

import asyncio
from hashlib import sha256
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from pulsara_agent.identity import derive_id
from pulsara_agent.conversation_kernel.compaction.contracts import (
    CompactionActiveRequestLocation,
    CompactionContinuationMode,
    CompactionDisposition,
    CompactionLineageBaseKind,
    CompactionOutcome,
    CompactionScope,
    CompactionSourceLineageBase,
    CompactionTargetBranch,
    FrozenCompactionCanonicalRead,
    freeze_compaction_canonical_range,
    provider_input_item_canonical_expanded_bytes,
)
from pulsara_agent.conversation_kernel.compaction.coordinator import (
    CompactionCoordinator,
    CompactionExecutionResult,
    _join_compaction_settlement,
)
from pulsara_agent.conversation_kernel.compaction.model_call import (
    _destination_source_messages_and_active_placement,
)
from pulsara_agent.conversation_kernel.compaction.planner import (
    build_synthetic_compaction_dispatch_read,
    enumerate_destination_backbone_projections,
    enumerate_safe_summary_prefixes,
    freeze_compaction_continuation,
    freeze_destination_dialogue_projection_plan,
    project_compaction_read_for_text_only_handover,
    provider_dispatch_effective_history_has_image,
)
from pulsara_agent.conversation_kernel.compaction.prompt import (
    build_compaction_snapshot_carrier,
    freeze_compaction_summary_output,
)
from pulsara_agent.llm.input import LLMMessage, LLMTextPart, MessageRole
from pulsara_agent.model_input.contracts import (
    CanonicalInputOriginKind,
    CanonicalModelInputIdentity,
    CanonicalModelInputSnapshot,
    FrozenCompiledMessagePlacement,
    FrozenProviderInputItem,
    FrozenProviderInputItemKind,
    ModelInputScopeKind,
    canonical_model_input_identity_fingerprint,
    canonical_model_input_snapshot_fingerprint,
    provider_input_item_fingerprint,
)
from pulsara_agent.model_input.continuity import ProviderInputContinuityScope
from pulsara_agent.model_input.provider_replay import (
    FrozenCanonicalProviderDispatchRead,
    freeze_provider_replay_manifest_cut,
)
from pulsara_agent.primitives.context import context_fingerprint
from tests.support.images import bound_test_image
from tests.support.round3 import static_canonical_compile_facts


def _first_call_read(
    *, historical_image: bool = False
) -> FrozenCompactionCanonicalRead:
    turn = derive_id("turn", "first-call-current")
    old_turn = derive_id("turn", "first-call-predecessor")
    image = bound_test_image("image/png", b"first-call-material", 2, 2)
    descriptions = (
        ("history", old_turn, CanonicalInputOriginKind.HUMAN_MESSAGE),
        ("old answer", old_turn, None),
        ("profile before instruction", turn, CanonicalInputOriginKind.AGENT_PROFILE),
        ("original instruction", turn, CanonicalInputOriginKind.AGENT_INSTRUCTION),
        (
            "dependency material after instruction",
            turn,
            CanonicalInputOriginKind.AGENT_MATERIAL,
        ),
    )
    items = tuple(
        FrozenProviderInputItem(
            item_kind=(
                FrozenProviderInputItemKind.ASSISTANT
                if origin is None
                else FrozenProviderInputItemKind.USER
            ),
            source_entry_id=derive_id("entry", "first-call", str(index)),
            source_entry_sequence=index,
            source_turn_id=owner,
            content=(LLMTextPart(body), image)
            if index == 5 or (index == 1 and historical_image)
            else (LLMTextPart(body),),
            input_origin=origin,
        )
        for index, (body, owner, origin) in enumerate(descriptions, 1)
    )
    identity_values = dict(
        session_id=derive_id("session", "first-call"),
        turn_id=turn,
        initial_entry_id=items[3].source_entry_id,
        context_binding_revision_id=derive_id("binding", "first-call", "zero"),
        provider_input_through_sequence=5,
        conversation_scope_kind=ModelInputScopeKind.SUBAGENT,
    )
    identity = CanonicalModelInputIdentity(
        **identity_values,
        identity_fingerprint=canonical_model_input_identity_fingerprint(
            **identity_values
        ),
    )
    expanded = sum(provider_input_item_canonical_expanded_bytes(item) for item in items)
    canonical = CanonicalModelInputSnapshot(
        identity=identity,
        items=items,
        canonical_expanded_bytes=expanded,
        snapshot_fingerprint=canonical_model_input_snapshot_fingerprint(
            identity=identity,
            items=items,
            canonical_expanded_bytes=expanded,
            closures=(),
            late_outcomes=(),
        ),
    )
    facts = static_canonical_compile_facts(canonical)
    manifest = freeze_provider_replay_manifest_cut(
        session_id=identity.session_id,
        scope=ProviderInputContinuityScope(
            identity.session_id, ModelInputScopeKind.SUBAGENT
        ),
        context_binding_revision_id=identity.context_binding_revision_id,
        provider_input_through_sequence=5,
        manifests=(),
    )
    dispatch = FrozenCanonicalProviderDispatchRead(
        compile_snapshot=facts,
        replay_manifest_cut=manifest,
        composite_fingerprint=context_fingerprint(
            "pulsara.canonical-provider-dispatch-read:v1",
            {
                "compile": facts.canonical_read_cut_fingerprint,
                "replay_manifest_cut": manifest.cut_fingerprint,
            },
        ),
    )
    scope = CompactionScope(
        identity.session_id,
        derive_id("workspace", "first-call"),
        turn,
        ModelInputScopeKind.SUBAGENT,
    )
    lineage = CompactionSourceLineageBase(
        kind=CompactionLineageBaseKind.FULL_HISTORY_GENESIS,
        scope=scope,
        binding_revision_id=identity.context_binding_revision_id,
        binding_revision_ordinal=0,
        persisted_revision_genesis_marker=2,
        effective_materialization_lineage_floor=0,
    )
    return FrozenCompactionCanonicalRead(
        scope=scope,
        turn_status="RUNNING",
        dispatch_read=dispatch,
        lineage_base=lineage,
        safe_head_range=freeze_compaction_canonical_range(
            scope=scope,
            effective_materialization_lineage_floor=0,
            source_through_sequence=5,
            ordered_items=items,
            closures=(),
            late_outcomes=(),
        ),
    )


def _source(read):
    canonical = read.dispatch_read.compile_snapshot.canonical_input
    messages = tuple(
        LLMMessage(
            role=MessageRole.ASSISTANT
            if item.item_kind is FrozenProviderInputItemKind.ASSISTANT
            else MessageRole.USER,
            content=item.content,
        )
        for item in canonical.items
    )
    placements = tuple(
        FrozenCompiledMessagePlacement(
            message_ordinal=index,
            origin_entry_id=item.source_entry_id,
            origin_item_fingerprint=provider_input_item_fingerprint(item),
            within_origin_ordinal=0,
            role=messages[index].role,
        )
        for index, item in enumerate(canonical.items)
    )
    binding = SimpleNamespace(
        binding_fingerprint=context_fingerprint("test:binding", "first-call"),
        tool_surface=SimpleNamespace(tool_specs=()),
    )
    projection = SimpleNamespace(
        system_prompt="frozen system",
        messages=messages,
        message_placements=placements,
        tools=(),
        compile_binding_fingerprint=binding.binding_fingerprint,
    )
    source = SimpleNamespace(
        source_view_fingerprint=context_fingerprint("test:source", "first-call"),
        materialized_messages=lambda: messages,
        materialized_system_prompt=lambda: "frozen system",
        predecessor_epoch_view=SimpleNamespace(messages=messages[:2]),
        normal_compile_binding=binding,
        canonical_dispatch_read=read.dispatch_read,
        exact_safe_canonical_head=5,
        snapshot_carrier=None,
    )
    return source, projection


def test_first_call_summary_ceiling_precedes_the_whole_initial_group() -> None:
    read = _first_call_read()
    source, projection = _source(read)
    candidates = enumerate_safe_summary_prefixes(
        source_view=source,
        complete_tool_groups=(),
        retained_group_count=0,
        source_projection=projection,
        maximum_source_through_sequence=2,
    )
    assert len(candidates) == 1
    tail, prefix = candidates[0]
    assert prefix.source_through_sequence == 2
    assert prefix.summary_prefix_message_count == 2
    assert source.exact_safe_canonical_head == 5
    assert (
        source.materialized_messages()[tail.protected_tail_message_start_index :]
        == projection.messages[2:]
    )
    _, active = freeze_compaction_continuation(
        source_view=source,
        target_branch=CompactionTargetBranch.ACTIVE_INSTALLATION,
        source_through_sequence=prefix.source_through_sequence,
    )
    assert active.entry_sequence == 4
    assert active.location is CompactionActiveRequestLocation.CANONICAL_SUFFIX
    assert active.content is None


def test_destination_summary_excludes_profile_instruction_and_dependency_material() -> (
    None
):
    read = _first_call_read()
    source, projection = _source(read)
    _, active = freeze_compaction_continuation(
        source_view=source,
        target_branch=CompactionTargetBranch.ACTIVE_INSTALLATION,
        source_through_sequence=2,
    )
    plan = freeze_destination_dialogue_projection_plan(
        canonical_read=read,
        active_request=active,
        source_through_sequence=2,
    )
    assert len(plan.units) == 1
    assert plan.units[0].source_entry_sequences == (1, 2)
    text = str(enumerate_destination_backbone_projections(plan)[0].content)
    assert "history" in text and "old answer" in text
    for item in read.dispatch_read.compile_snapshot.canonical_input.items[2:]:
        assert item.content[0].text not in text
    current_messages, current_placements, active_placement = (
        _destination_source_messages_and_active_placement(
            source_projection=projection,
            active_request=active,
        )
    )
    assert current_messages == current_placements == ()
    assert active_placement is None


def test_history_image_projection_keeps_every_current_group_image_exact() -> None:
    read = _first_call_read(historical_image=True)
    original = read.dispatch_read.compile_snapshot.canonical_input.items
    assert provider_dispatch_effective_history_has_image(
        read.dispatch_read, first_call_history_through_sequence=2
    )
    current_only = _first_call_read()
    assert not provider_dispatch_effective_history_has_image(
        current_only.dispatch_read, first_call_history_through_sequence=2
    )
    projected = project_compaction_read_for_text_only_handover(
        read, first_call_history_through_sequence=2
    )
    after = projected.dispatch_read.compile_snapshot.canonical_input.items
    assert after[0].content != original[0].content
    assert after[2:] == original[2:]
    assert after[4].content[1] is original[4].content[1]
    assert projected.safe_head_range.source_through_sequence == 5


def test_new_turn_adopted_base_keeps_exact_independent_initial_entries() -> None:
    read = _first_call_read()
    source, _projection = _source(read)
    mode, active = freeze_compaction_continuation(
        source_view=source,
        target_branch=CompactionTargetBranch.ACTIVE_INSTALLATION,
        source_through_sequence=2,
    )
    carrier = build_compaction_snapshot_carrier(
        summary=freeze_compaction_summary_output(
            "historical summary", maximum_utf8_bytes=200
        ),
        recent_human_requests=(),
        continuation_mode=mode,
        active_request=active,
    )
    rewritten = build_synthetic_compaction_dispatch_read(
        canonical_read=read,
        source_through_sequence=2,
        snapshot_id=derive_id("snapshot", "first-call"),
        binding_revision_id=derive_id("binding", "first-call", "adopted"),
        binding_revision_ordinal=1,
        snapshot_carrier=carrier,
        snapshot_content_digest="sha256:" + sha256(carrier.body).hexdigest(),
        snapshot_content_size=len(carrier.body),
        snapshot_content_media_type="application/vnd.pulsara.context-snapshot+json",
        snapshot_content_codec="utf-8",
        snapshot_blob_id=None,
    )
    canonical = rewritten.compile_snapshot.canonical_input
    assert canonical.identity.turn_id == read.scope.turn_id
    assert canonical.identity.initial_entry_id == active.entry_id
    assert canonical.identity.provider_input_through_sequence == 5
    assert (
        canonical.items[1:]
        == read.dispatch_read.compile_snapshot.canonical_input.items[2:]
    )
    assert tuple(item.source_entry_sequence for item in canonical.items[1:]) == (
        3,
        4,
        5,
    )
    assert carrier.continuation_mode is CompactionContinuationMode.RESUME_ACTIVE_TURN
    assert carrier.active_request.content is None

    # A previous adopted snapshot alone remains a compressible predecessor;
    # its origin has no entry ID, while the exact current group stays suffix.
    from dataclasses import replace

    _old_source, old_projection = _source(read)
    snapshot_message = LLMMessage.user("previous summary")
    messages = (snapshot_message, *old_projection.messages[2:])
    placements = (
        FrozenCompiledMessagePlacement(
            message_ordinal=0,
            origin_entry_id=None,
            origin_item_fingerprint=provider_input_item_fingerprint(canonical.items[0]),
            within_origin_ordinal=0,
            role=MessageRole.USER,
        ),
        *(
            replace(item, message_ordinal=index + 1)
            for index, item in enumerate(old_projection.message_placements[2:])
        ),
    )
    projection = SimpleNamespace(
        **{
            **vars(old_projection),
            "messages": messages,
            "message_placements": placements,
        }
    )
    snapshot_source = SimpleNamespace(
        **{
            **vars(source),
            "canonical_dispatch_read": rewritten,
            "materialized_messages": lambda: messages,
            "predecessor_epoch_view": SimpleNamespace(messages=messages[:1]),
        }
    )
    prefixes = enumerate_safe_summary_prefixes(
        source_view=snapshot_source,
        complete_tool_groups=(),
        retained_group_count=0,
        source_projection=projection,
        maximum_source_through_sequence=2,
    )
    assert [
        (proof.summary_prefix_message_count, proof.source_through_sequence)
        for _tail, proof in prefixes
    ] == [(1, 2)]


def test_repeated_stop_drains_adoption_and_retires_returned_continuation_once() -> None:
    async def exercise() -> None:
        started = asyncio.Event()
        release = asyncio.Event()
        closed = []
        dispatch = SimpleNamespace(close=lambda *, error: closed.append(error))

        async def settle():
            started.set()
            await release.wait()
            return CompactionExecutionResult(
                CompactionOutcome(
                    CompactionDisposition.COMPACTED, "turn", "snapshot", 1, "COMPACTED"
                ),
                successor_dispatch=dispatch,
            )

        worker = asyncio.create_task(settle())
        caller = asyncio.create_task(_join_compaction_settlement(worker))
        await started.wait()
        caller.cancel("user stop")
        await asyncio.sleep(0)
        caller.cancel("close")
        await asyncio.sleep(0)
        assert not worker.cancelled() and not worker.done()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await caller
        assert worker.done() and not worker.cancelled()
        assert len(closed) == 1
        assert str(closed[0]) == "user stop"

    asyncio.run(exercise())


def test_adoption_deterministic_rejection_is_not_retried() -> None:
    from pulsara_agent.conversation_kernel.compaction.contracts import (
        CompactionConfirmationKind,
    )
    from pulsara_agent.conversation_kernel.repository import ConversationKernelConflict

    none = SimpleNamespace(kind=CompactionConfirmationKind.NONE)
    writer = Mock(side_effect=ConversationKernelConflict("canonical head advanced"))
    confirm = Mock(return_value=none)
    owner = object.__new__(CompactionCoordinator)
    owner._repository = SimpleNamespace(
        confirm_context_snapshot_adoption=confirm, adopt_context_snapshot=writer
    )
    owner._writer_lease = SimpleNamespace(guard=object())
    owner._canonical_deadline = lambda: 1.0

    async def run(fn, *args, **kwargs):
        return fn(*args, **kwargs)

    owner._io = SimpleNamespace(run=run)
    result = asyncio.run(
        owner._settle_compaction_adoption(candidate=object(), preconditions=object())
    )
    assert result is none
    assert writer.call_count == 1
    assert confirm.call_count == 2


def test_adoption_lost_ack_confirms_full_without_reissuing_write() -> None:
    from pulsara_agent.conversation_kernel.compaction.contracts import (
        CompactionConfirmationKind,
    )

    none = SimpleNamespace(kind=CompactionConfirmationKind.NONE)
    full = SimpleNamespace(kind=CompactionConfirmationKind.FULL)
    confirm = Mock(side_effect=(none, full))
    writer = Mock(side_effect=OSError("acknowledgement lost after commit"))
    owner = object.__new__(CompactionCoordinator)
    owner._repository = SimpleNamespace(
        confirm_context_snapshot_adoption=confirm, adopt_context_snapshot=writer
    )
    owner._writer_lease = SimpleNamespace(guard=object())
    owner._canonical_deadline = lambda: 1.0

    async def run(fn, *args, **kwargs):
        return fn(*args, **kwargs)

    owner._io = SimpleNamespace(run=run)
    result = asyncio.run(
        owner._settle_compaction_adoption(candidate=object(), preconditions=object())
    )
    assert result is full
    assert writer.call_count == 1
    assert confirm.call_count == 2


def test_scheduled_initial_entry_uses_the_reader_typed_prompt_byte_charge() -> None:
    from dataclasses import replace
    from pulsara_agent.conversation_kernel.prompt_content import canonical_prompt_body_bytes
    from pulsara_agent.llm.input import FrozenPromptContent
    from pulsara_agent.scheduling.contracts import ScheduledProvenance, ScheduledTriggerSource

    original = _first_call_read().dispatch_read.compile_snapshot.canonical_input.items[0]
    scheduled = replace(original,
        content=(LLMTextPart("Saved report context. " * 1500),),
        input_origin=CanonicalInputOriginKind.SCHEDULED_TASK,
        scheduled_input=ScheduledProvenance(
            task_id=derive_id("schedule", "byte-charge"), task_revision=1,
            due_at_utc="2026-10-30T00:00:00+00:00",
            trigger_source=ScheduledTriggerSource.RUN_ONCE,
        ),
    )
    content = FrozenPromptContent(scheduled.content)
    exact_reader_charge = len(canonical_prompt_body_bytes(content))
    raw_text_bytes = sum(len(part.text.encode("utf-8")) for part in scheduled.content)
    assert exact_reader_charge > raw_text_bytes
    assert provider_input_item_canonical_expanded_bytes(scheduled) == exact_reader_charge
