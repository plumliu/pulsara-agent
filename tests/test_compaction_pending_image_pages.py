"""Fresh image pages remain exact through the real compaction tail planner."""

import asyncio
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from pulsara_agent.conversation_kernel.compaction.contracts import CompleteToolGroup
from pulsara_agent.conversation_kernel.compaction.contracts import (
    CompactionDisposition,
    CompactionOutcome,
    ResolvedCompactionPolicy,
)
from pulsara_agent.conversation_kernel.compaction.coordinator import (
    CompactionCoordinator,
    CompactionExecutionResult,
    PendingToolBatchCompaction,
)
from pulsara_agent.conversation_kernel.compaction.runtime import (
    HostCompactionRuntimeOwner,
)
from pulsara_agent.conversation_kernel.memory.contracts import MemoryUsePolicy
from pulsara_agent.conversation_kernel.compaction.planner import (
    CompactionPlanningError,
    enumerate_safe_summary_prefixes,
    freeze_tail_and_prefix,
    minimum_pending_image_tool_groups,
    pending_image_evidence_start_sequence,
    pending_image_tool_group_start_sequence,
)
from pulsara_agent.identity import derive_id
from pulsara_agent.llm.input import LLMMessage, LLMTextPart, LLMToolCall, MessageRole
from pulsara_agent.model_input.contracts import FrozenProviderInputItemKind as Kind
from pulsara_agent.model_input.contracts import (
    ModelInputScopeKind,
    PreparedProviderInputCut,
)
from pulsara_agent.primitives.context import context_fingerprint
from tests.support.images import bound_test_image


def _pending_page(*, consumed=False, old_turn=False, response_saw_image=True):
    request_id = derive_id("entry", "pending-image-request")
    image_entry = derive_id("entry", "pending-image-result")
    turn = derive_id("turn", "pending-image-current")
    image_turn = derive_id("turn", "pending-image-old") if old_turn else turn
    image = bound_test_image("image/png", b"fresh-page", 2, 2)
    calls = (
        LLMToolCall("view-call", "view_image", "{}"),
        LLMToolCall("text-call", "read_file", "{}"),
    )

    def item(kind, sequence, body, **values):
        return SimpleNamespace(
            item_kind=kind,
            source_entry_id=request_id
            if sequence == 4
            else derive_id("entry", "pending-page", str(sequence)),
            source_entry_sequence=sequence,
            source_turn_id=image_turn if kind is Kind.TOOL_RESULT else turn,
            content=body,
            tool_request_entry_id=values.pop("tool_request_entry_id", None),
            **values,
        )

    items = [
        item(Kind.USER, 1, (LLMTextPart("old question"),)),
        item(Kind.ASSISTANT, 2, (LLMTextPart("old answer"),)),
        item(Kind.USER, 3, (LLMTextPart("compare this image"),)),
        item(Kind.ASSISTANT_TOOL_REQUEST, 4, (LLMTextPart(""),)),
        item(
            Kind.TOOL_RESULT,
            5,
            (LLMTextPart("frame 1 at 12.5s"), image),
            tool_request_entry_id=request_id,
        ),
        item(
            Kind.TOOL_RESULT,
            6,
            (LLMTextPart("the accompanying text"),),
            tool_request_entry_id=request_id,
        ),
    ]
    messages = [
        LLMMessage.user("old question"),
        LLMMessage.assistant("old answer"),
        LLMMessage.user("compare this image"),
        LLMMessage.assistant_turn(tool_calls=calls),
        LLMMessage.tool_result("frame 1 at 12.5s", tool_call_id="view-call"),
        LLMMessage.tool_result("the accompanying text", tool_call_id="text-call"),
        LLMMessage(
            role=MessageRole.USER, content=(LLMTextPart("view-call image"), image)
        ),
    ]
    placements = [
        SimpleNamespace(
            origin_entry_id=value.source_entry_id,
            origin_item_fingerprint=None,
            tool_attachment_source=None,
        )
        for value in items
    ]
    placements.append(
        SimpleNamespace(
            origin_entry_id=None,
            origin_item_fingerprint=None,
            tool_attachment_source=SimpleNamespace(
                members=(
                    SimpleNamespace(
                        source=SimpleNamespace(
                            source_entry_id=image_entry,
                            source_entry_sequence=5,
                        )
                    ),
                )
            ),
        )
    )
    if consumed:
        last = item(Kind.ASSISTANT, 7, (LLMTextPart("I see frame 1 at 12.5s"),))
        items.append(last)
        messages.append(LLMMessage.assistant(last.content[0].text))
        placements.append(
            SimpleNamespace(
                origin_entry_id=last.source_entry_id,
                origin_item_fingerprint=None,
                tool_attachment_source=None,
            )
        )
    canonical = SimpleNamespace(
        identity=SimpleNamespace(turn_id=turn), items=tuple(items)
    )
    binding = SimpleNamespace(
        binding_fingerprint=context_fingerprint("test:pending-binding", "one"),
        tool_surface=SimpleNamespace(tool_specs=()),
    )
    source = SimpleNamespace(
        canonical_dispatch_read=SimpleNamespace(
            compile_snapshot=SimpleNamespace(canonical_input=canonical)
        ),
        materialized_messages=lambda: tuple(messages),
        materialized_system_prompt=lambda: "unchanged system",
        predecessor_epoch_view=SimpleNamespace(messages=tuple(messages[:2])),
        source_view_fingerprint=context_fingerprint(
            "test:pending-source", (consumed, old_turn)
        ),
        normal_compile_binding=binding,
        exact_safe_canonical_head=7 if consumed else 6,
        latest_assistant_input_through_sequence=6
        if consumed and response_saw_image
        else 3,
    )
    projection = SimpleNamespace(
        messages=tuple(messages),
        message_placements=tuple(placements),
        system_prompt="unchanged system",
        tools=(),
        compile_binding_fingerprint=binding.binding_fingerprint,
    )
    group = CompleteToolGroup(
        request_id, 4, ("view-call", "text-call"), ("image-result", "text-result")
    )
    return canonical, source, projection, (group,), image


@pytest.mark.parametrize("old_turn", (False, True))
def test_optional_zero_tail_keeps_whole_unconsumed_image_and_mixed_batch(old_turn):
    canonical, source, projection, groups, image = _pending_page(old_turn=old_turn)
    assert (
        pending_image_tool_group_start_sequence(
            canonical,
            consumed_through_sequence=source.latest_assistant_input_through_sequence,
        )
        == 4
    )
    assert (
        minimum_pending_image_tool_groups(
            source_view=source, complete_tool_groups=groups
        )
        == 1
    )
    tail, prefix = freeze_tail_and_prefix(
        source_view=source,
        complete_tool_groups=groups,
        retained_group_count=0,
        source_projection=projection,
    )
    assert tail.retained_groups == groups
    assert prefix.source_through_sequence == 3
    assert tail.protected_tail_message_start_index == 3
    kept = source.materialized_messages()[tail.protected_tail_message_start_index :]
    assert kept[0].tool_calls[0].id == "view-call"
    assert tuple(message.tool_call_id for message in kept[1:3]) == (
        "view-call",
        "text-call",
    )
    assert kept[-1].content[-1] is image
    candidates = enumerate_safe_summary_prefixes(
        source_view=source,
        complete_tool_groups=groups,
        retained_group_count=0,
        source_projection=projection,
    )
    assert candidates
    assert all(value.source_through_sequence < 4 for _, value in candidates)
    assert all(
        source.materialized_messages()[value.protected_tail_message_start_index :][
            -1
        ].content[-1]
        is image
        for value, _ in candidates
    )


def test_business_assistant_consumption_releases_image_tail_floor():
    canonical, source, projection, groups, _ = _pending_page(consumed=True)
    assert (
        pending_image_tool_group_start_sequence(
            canonical,
            consumed_through_sequence=source.latest_assistant_input_through_sequence,
        )
        is None
    )
    assert (
        minimum_pending_image_tool_groups(
            source_view=source, complete_tool_groups=groups
        )
        == 0
    )
    tail, prefix = freeze_tail_and_prefix(
        source_view=source,
        complete_tool_groups=groups,
        retained_group_count=0,
        source_projection=projection,
    )
    assert not tail.retained_groups
    assert prefix.source_through_sequence == 7


def test_pending_image_cannot_be_summarized_after_losing_its_batch():
    canonical, source, _, _, _ = _pending_page()
    with pytest.raises(CompactionPlanningError, match="not complete"):
        minimum_pending_image_tool_groups(source_view=source, complete_tool_groups=())
    canonical.items = tuple(
        item
        for item in canonical.items
        if item.item_kind is not Kind.ASSISTANT_TOOL_REQUEST
    )
    with pytest.raises(CompactionPlanningError, match="whole assistant tool batch"):
        pending_image_tool_group_start_sequence(
            canonical,
            consumed_through_sequence=source.latest_assistant_input_through_sequence,
        )


def test_assistant_accepted_after_late_image_must_have_actually_read_it():
    canonical, source, projection, groups, image = _pending_page(
        consumed=True, old_turn=True, response_saw_image=False
    )
    assert (
        pending_image_tool_group_start_sequence(canonical, consumed_through_sequence=3)
        == 4
    )
    assert (
        minimum_pending_image_tool_groups(
            source_view=source, complete_tool_groups=groups
        )
        == 1
    )
    tail, prefix = freeze_tail_and_prefix(
        source_view=source,
        complete_tool_groups=groups,
        retained_group_count=0,
        source_projection=projection,
    )
    assert prefix.source_through_sequence == 3
    assert any(
        part is image
        for message in source.materialized_messages()[
            tail.protected_tail_message_start_index :
        ]
        for part in message.content
    )


def test_no_accepted_assistant_input_cut_cannot_acknowledge_a_page():
    canonical, _, _, _, _ = _pending_page()
    assert (
        pending_image_tool_group_start_sequence(
            canonical, consumed_through_sequence=None
        )
        == 4
    )


def test_settled_imported_image_is_not_a_new_executed_pending_batch():
    canonical, source, projection, groups, _ = _pending_page()
    history = derive_id("history", "imported-visual-history")
    for item in canonical.items:
        item.source_turn_id = history
    source.latest_assistant_input_through_sequence = None
    assert (
        pending_image_tool_group_start_sequence(
            canonical, consumed_through_sequence=None
        )
        is None
    )
    assert (
        pending_image_evidence_start_sequence(canonical, consumed_through_sequence=None)
        is None
    )
    assert (
        minimum_pending_image_tool_groups(
            source_view=source, complete_tool_groups=groups
        )
        == 0
    )
    tail, prefix = freeze_tail_and_prefix(
        source_view=source,
        complete_tool_groups=groups,
        retained_group_count=0,
        source_projection=projection,
    )
    assert not tail.retained_groups
    assert prefix.source_through_sequence == source.exact_safe_canonical_head


def test_late_image_after_its_request_was_summarized_keeps_only_observation_carrier():
    canonical, source, projection, _, image = _pending_page(old_turn=True)
    late = canonical.items[4]
    late.item_kind = Kind.LATE_TOOL_OUTCOME
    canonical.items = (*canonical.items[:3], late)
    text = LLMMessage.user("late pending-page frame 1 at 12.5s")
    attachment = LLMMessage(
        role=MessageRole.USER, content=(LLMTextPart("late image"), image)
    )
    messages = (*projection.messages[:3], text, attachment)
    projection.messages = messages
    projection.message_placements = (
        *projection.message_placements[:3],
        SimpleNamespace(
            origin_entry_id=late.source_entry_id,
            origin_item_fingerprint=None,
            tool_attachment_source=None,
        ),
        SimpleNamespace(
            origin_entry_id=None,
            origin_item_fingerprint=None,
            tool_attachment_source=SimpleNamespace(
                members=(
                    SimpleNamespace(
                        source=SimpleNamespace(
                            source_entry_id=late.source_entry_id,
                            source_entry_sequence=late.source_entry_sequence,
                        )
                    ),
                )
            ),
        ),
    )
    source.materialized_messages = lambda: messages
    assert (
        pending_image_tool_group_start_sequence(canonical, consumed_through_sequence=3)
        is None
    )
    assert (
        pending_image_evidence_start_sequence(canonical, consumed_through_sequence=3)
        == 5
    )
    assert (
        minimum_pending_image_tool_groups(source_view=source, complete_tool_groups=())
        == 0
    )
    choices = enumerate_safe_summary_prefixes(
        source_view=source,
        complete_tool_groups=(),
        retained_group_count=0,
        source_projection=projection,
    )
    assert choices
    for tail, prefix in choices:
        assert prefix.source_through_sequence < 5
        assert messages[tail.protected_tail_message_start_index :][-2:] == (
            text,
            attachment,
        )
        assert not tail.retained_groups
    tail, prefix = freeze_tail_and_prefix(
        source_view=source,
        complete_tool_groups=(),
        retained_group_count=0,
        source_projection=projection,
    )
    assert prefix.source_through_sequence == 3
    assert messages[tail.protected_tail_message_start_index :] == (text, attachment)


def test_prepared_prefix_is_released_if_fence_removal_is_cancelled():
    close = Mock()
    prepared = PendingToolBatchCompaction(
        None,
        None,
        None,
        None,
        SimpleNamespace(dry_dispatch=SimpleNamespace(close=close)),
    )
    coordinator = CompactionCoordinator.__new__(CompactionCoordinator)
    session, turn = (
        derive_id("session", "pending-cancel"),
        derive_id("turn", "pending-cancel"),
    )

    async def fenced(**kwargs):
        await kwargs["operation"]()
        raise asyncio.CancelledError

    async def workspace():
        return derive_id("workspace", "pending-cancel")

    async def prepare(**_kwargs):
        return prepared

    coordinator._compaction_owner = SimpleNamespace(
        policy=SimpleNamespace(enabled=True, automatic_enabled=True),
        automatic_allowed=lambda **_kwargs: True,
        run_fenced=fenced,
    )
    coordinator._resolved_workspace_id = workspace
    coordinator._execute_compaction_fenced = prepare
    coordinator._writer_lease = SimpleNamespace(
        guard=SimpleNamespace(session_id=session)
    )
    coordinator._session_scope_kind = ModelInputScopeKind.ROOT
    coordinator._hook_root_scope = None
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(
            coordinator.prepare_pending_tool_batch(
                turn_id=turn,
                model_call_index=2,
                inherited_memory_use_policy=MemoryUsePolicy.ENABLED,
                closed_source_cut=PreparedProviderInputCut(
                    session, turn, derive_id("binding", "pending-cancel"), 1
                ),
                assistant_entry_id=derive_id("entry", "pending-cancel"),
                ordered_tool_call_ids=("provider-call",),
            )
        )
    close.assert_called_once()
    prepared.close()
    close.assert_called_once()


@pytest.mark.parametrize(
    ("disposition", "code", "counts_as_failure"),
    (
        (CompactionDisposition.FAILED, "NO_EXECUTABLE_SUMMARY_PREFIX", True),
        (CompactionDisposition.FAILED, "HOOK_PRE_COMPACT_BLOCKED", False),
        (CompactionDisposition.NOT_NEEDED, "CONTEXT_ALREADY_COMPACT", False),
    ),
)
def test_pending_preparation_uses_existing_automatic_failure_circuit(
    disposition, code, counts_as_failure
):
    coordinator = CompactionCoordinator.__new__(CompactionCoordinator)
    session, turn = (
        derive_id("session", "pending-circuit"),
        derive_id("turn", "pending-circuit"),
    )
    owner = HostCompactionRuntimeOwner(
        policy=ResolvedCompactionPolicy(automatic_enabled=True)
    )
    calls = []

    async def workspace():
        return derive_id("workspace", "pending-circuit")

    async def prepare(**_kwargs):
        calls.append(True)
        return CompactionExecutionResult(
            CompactionOutcome(disposition, turn, None, None, code)
        )

    coordinator._compaction_owner = owner
    coordinator._resolved_workspace_id = workspace
    coordinator._execute_compaction_fenced = prepare
    coordinator._writer_lease = SimpleNamespace(
        guard=SimpleNamespace(session_id=session)
    )
    coordinator._session_scope_kind = ModelInputScopeKind.ROOT
    coordinator._hook_root_scope = None
    arguments = dict(
        turn_id=turn,
        model_call_index=2,
        inherited_memory_use_policy=MemoryUsePolicy.ENABLED,
        closed_source_cut=PreparedProviderInputCut(
            session, turn, derive_id("binding", "pending-circuit"), 1
        ),
        assistant_entry_id=derive_id("entry", "pending-circuit"),
        ordered_tool_call_ids=("provider-call",),
    )

    async def exercise():
        for _ in range(owner.policy.maximum_consecutive_auto_failures):
            assert owner.automatic_allowed(scope_kind=ModelInputScopeKind.ROOT)
            assert await coordinator.prepare_pending_tool_batch(**arguments) is None
        assert (
            owner.automatic_allowed(scope_kind=ModelInputScopeKind.ROOT)
            is not counts_as_failure
        )
        if counts_as_failure:
            assert await coordinator.prepare_pending_tool_batch(**arguments) is None
            assert len(calls) == owner.policy.maximum_consecutive_auto_failures
        await owner.aclose()

    asyncio.run(exercise())
