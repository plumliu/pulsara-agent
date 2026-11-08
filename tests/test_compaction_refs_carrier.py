"""Summary-only carriers and exact canonical prefix selections."""

from dataclasses import replace
import json

import pytest

from pulsara_agent.conversation_kernel.compaction.contracts import (
    CompactionContinuationMode,
    CompactionScope,
    ResolvedCompactionPolicy,
    freeze_compaction_canonical_range,
)
from pulsara_agent.conversation_kernel.compaction.prompt import (
    build_compaction_snapshot_carrier,
    compaction_snapshot_canonical_expanded_bytes,
    freeze_compaction_summary_output,
    parse_compaction_snapshot_carrier,
)
from pulsara_agent.identity import derive_id
from pulsara_agent.llm.input import LLMTextPart, MessageRole
from pulsara_agent.model_input.contracts import (
    CanonicalInputOriginKind,
    FrozenProviderInputItem,
    FrozenProviderInputItemKind,
    ModelInputScopeKind,
    provider_input_item_logical_utf8_bytes,
)
from pulsara_agent.model_input.lowering import lower_canonical_item
from pulsara_agent.ports.system_prompt import (
    DEFAULT_SUBAGENT_SYSTEM_PROMPT,
    DEFAULT_SYSTEM_PROMPT,
)
from pulsara_agent.primitives.context import canonical_json_bytes


def _id(kind, role):
    return derive_id(kind, "compaction-refs-test", role)


def _carrier(*, refs=(), active=None):
    return build_compaction_snapshot_carrier(
        summary=freeze_compaction_summary_output(
            "Earlier investigation is complete.", maximum_utf8_bytes=4096
        ),
        retained_entry_ids=refs,
        continuation_mode=(
            CompactionContinuationMode.AWAIT_NEXT_USER
            if active is None
            else CompactionContinuationMode.RESUME_ACTIVE_TURN
        ),
        active_request_entry_id=active,
    )


def _original(role, sequence, text="same text"):
    return FrozenProviderInputItem(
        item_kind=FrozenProviderInputItemKind.USER,
        source_entry_id=_id("entry", role),
        source_entry_sequence=sequence,
        source_turn_id=_id("turn", "history"),
        content=(LLMTextPart(text),),
        input_origin=CanonicalInputOriginKind.HUMAN_MESSAGE,
    )


def _snapshot(carrier):
    return FrozenProviderInputItem(
        item_kind=FrozenProviderInputItemKind.CONTEXT_SNAPSHOT,
        source_entry_id=None,
        source_entry_sequence=10,
        source_turn_id=None,
        content=carrier,
    )


def _range(items):
    return freeze_compaction_canonical_range(
        scope=CompactionScope(
            session_id=_id("session", "local"),
            workspace_id=_id("workspace", "local"),
            turn_id=_id("turn", "active"),
            scope_kind=ModelInputScopeKind.ROOT,
        ),
        effective_materialization_lineage_floor=10,
        source_through_sequence=20,
        ordered_items=items,
        closures=(),
        late_outcomes=(),
    )


def test_carrier_persists_only_summary_selection_and_minimal_continuation():
    active = _id("entry", "active")
    carrier = _carrier(refs=(active,), active=active)
    assert json.loads(carrier.body) == {
        "continuation": {
            "mode": "RESUME_ACTIVE_TURN", "active_request_entry_id": active
        },
        "earlier_context_summary": "Earlier investigation is complete.",
        "retained_entry_ids": [active],
    }
    assert parse_compaction_snapshot_carrier(carrier.body) == carrier
    assert compaction_snapshot_canonical_expanded_bytes(carrier.body) == len(carrier.body)
    assert not hasattr(carrier, "active_request")
    assert not hasattr(carrier, "recent_human_requests")
    assert not hasattr(carrier, "handoff_instruction")


@pytest.mark.parametrize("active_in_prefix", [False, True])
def test_active_request_may_be_selected_prefix_or_normal_suffix(active_in_prefix):
    active = _id("entry", "active")
    carrier = _carrier(refs=(active,) if active_in_prefix else (), active=active)
    message = lower_canonical_item(_snapshot(carrier)).fixed_message
    assert message is not None and message.role is MessageRole.USER
    assert message.content == (
        LLMTextPart(
            "The following is a summary of earlier history.\n\n"
            "Earlier investigation is complete."
        ),
    )
    assert active not in message.content[0].text


@pytest.mark.parametrize("mutate", [
    lambda value: value.update(recent_human_requests=[]),
    lambda value: value["continuation"].update(instruction="HANDOFF"),
    lambda value: value.update(retained_entry_ids=["not-an-entry"]),
    lambda value: value.update(retained_entry_ids=[_id("entry", "x")] * 2),
    lambda value: value["continuation"].update(active_request_entry_id=_id("entry", "x")),
    lambda value: value.update(earlier_context_summary=""),
])
def test_carrier_rejects_noncontract_payloads(mutate):
    value = json.loads(_carrier().body)
    mutate(value)
    with pytest.raises((TypeError, ValueError)):
        parse_compaction_snapshot_carrier(canonical_json_bytes(value))


def test_carrier_rejects_duplicate_json_keys_and_noncanonical_json():
    with pytest.raises(ValueError, match="duplicate key"):
        parse_compaction_snapshot_carrier(b'{"continuation":null,"continuation":null}')
    with pytest.raises(ValueError, match="canonical JSON"):
        parse_compaction_snapshot_carrier(json.dumps(json.loads(_carrier().body)))


def test_range_retains_exact_prefix_rows_and_suffix_with_real_content_charge():
    first = _original("first", 2, "large original " * 1000)
    same_text_other_identity = _original("second", 4, first.content[0].text)
    suffix = _original("new", 12, "new input")
    carrier = _carrier(refs=(first.source_entry_id, same_text_other_identity.source_entry_id))
    selected = _range((_snapshot(carrier), first, same_text_other_identity, suffix))
    assert selected.ordered_items == (first, same_text_other_identity, suffix)
    assert selected.retained_entry_ids == carrier.retained_entry_ids
    assert selected.canonical_utf8_bytes == sum(
        provider_input_item_logical_utf8_bytes(item)
        for item in (first, same_text_other_identity, suffix)
    )
    assert selected.canonical_utf8_bytes > len(carrier.body)
    assert lower_canonical_item(first).fixed_message.content == first.content


@pytest.mark.parametrize("failure", ["missing", "suffix", "reversed", "unordered", "assistant", "unselected"])
def test_range_fails_closed_on_invalid_prefix_references(failure):
    first = _original("first", 2)
    second = _original("second", 4)
    refs = (first.source_entry_id, second.source_entry_id)
    rows = (first, second)
    if failure == "missing":
        rows = (first,)
    elif failure == "suffix":
        rows = (first, replace(second, source_entry_sequence=12))
    elif failure == "reversed":
        refs = tuple(reversed(refs))
    elif failure == "unordered":
        refs = tuple(reversed(refs))
        rows = tuple(reversed(rows))
    elif failure == "assistant":
        rows = (first, replace(second, item_kind=FrozenProviderInputItemKind.ASSISTANT, input_origin=None))
    else:
        rows = (first, second, _original("not-selected", 8))
    carrier = _carrier(refs=refs)
    if failure == "unselected":
        # Factory selects explicit members; an unselected old row cannot leak in.
        assert _range((_snapshot(carrier),) + rows).ordered_items == (first, second)
    else:
        with pytest.raises(ValueError):
            _range((_snapshot(carrier),) + rows)


def test_system_prompts_share_the_neutral_context_description():
    sentence = (
        "Context may include a summary of earlier history and recent interactions "
        "in their original message form, with usually only limited overlap between them."
    )
    assert DEFAULT_SYSTEM_PROMPT.count(sentence) == 1
    assert DEFAULT_SUBAGENT_SYSTEM_PROMPT.count(sentence) == 1
    policy = ResolvedCompactionPolicy()
    assert policy.auto_trigger_ratio == .85
    assert not hasattr(policy, "minimum_reclaim_tokens")
    assert not hasattr(policy, "post_compaction_target_ratio")
