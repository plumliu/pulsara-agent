"""Completed Responses sources enrich one replay without semantic overwrites."""
from copy import deepcopy

import pytest

from pulsara_agent.llm.adapters.openai import responses
from pulsara_agent.llm.adapters.openai.events import ProviderLiveItemBuilder
from pulsara_agent.llm.adapters.openai.responses import ResponsesCompletionAccumulator
from pulsara_agent.llm.errors import LLMTransportContractError
from pulsara_agent.ports.provider_stream import ProviderAdapterTerminal
from pulsara_agent.primitives.context import canonical_json_bytes, thaw_json


def message(**fields):
    return {"type": "message", "role": "assistant",
            "content": [{"type": "output_text", "text": "done"}], **fields}


def reasoning(**fields):
    return {"type": "reasoning", "id": "reasoning:provider", "summary": [], **fields}


def item_done(owner, index, item):
    owner.apply({"type": "response.output_item.done", "output_index": index, "item": item})


def completed(owner, output):
    owner.apply({"type": "response.completed", "response": {"status": "completed", "output": output}})
    terminal = owner.finish()
    assert isinstance(terminal, ProviderAdapterTerminal)
    assert terminal.completed_replay_payload is not None
    return [thaw_json(item) for item in terminal.completed_replay_payload.ordered_items]


@pytest.mark.parametrize("output_field", [{}, {"output": None}, {"output": []}])
def test_complete_item_done_stream_does_not_require_duplicate_terminal_items(output_field):
    owner = ResponsesCompletionAccumulator(builder=ProviderLiveItemBuilder())
    item = message(id="message:provider", status="completed")
    item_done(owner, 0, item)
    owner.apply({"type": "response.completed", "response": {"status": "completed", **output_field}})
    terminal = owner.finish()
    assert isinstance(terminal, ProviderAdapterTerminal)
    assert [thaw_json(item) for item in terminal.completed_replay_payload.ordered_items] == [item]


@pytest.mark.parametrize("output_field", [{}, {"output": None}, {"output": []}, {"output": "invalid"}])
def test_terminal_without_any_completed_items_still_fails(output_field):
    owner = ResponsesCompletionAccumulator(builder=ProviderLiveItemBuilder())
    with pytest.raises(LLMTransportContractError):
        owner.apply({"type": "response.completed", "response": {"status": "completed", **output_field}})
    assert owner.terminal is None


@pytest.mark.parametrize("done_value,final_value,selected", [
    ("done-ciphertext", "different-terminal-ciphertext", "done-ciphertext"),
    ("done-ciphertext", None, "done-ciphertext"),
    (None, "terminal-ciphertext", "terminal-ciphertext"),
    ("", "terminal-ciphertext", "terminal-ciphertext"),
    ("done-ciphertext", "", "done-ciphertext"),
])
def test_opaque_reasoning_uses_present_done_value_without_comparing_ciphertext(
    done_value, final_value, selected,
):
    owner = ResponsesCompletionAccumulator(builder=ProviderLiveItemBuilder())
    item_done(owner, 0, reasoning(encrypted_content=done_value))
    result = completed(owner, [reasoning(encrypted_content=final_value)])
    assert result == [reasoning(encrypted_content=selected)]


@pytest.mark.parametrize("malformed", [3, [], {}, False])
@pytest.mark.parametrize("malformed_source", ["done", "terminal"])
def test_opaque_exception_does_not_hide_malformed_carrier(malformed, malformed_source):
    owner = ResponsesCompletionAccumulator(builder=ProviderLiveItemBuilder())
    a, b = reasoning(encrypted_content="valid"), reasoning(encrypted_content="valid")
    (a if malformed_source == "done" else b)["encrypted_content"] = malformed
    item_done(owner, 0, a)
    with pytest.raises(LLMTransportContractError):
        completed(owner, [b])
    assert owner.terminal is None


def test_reasoning_preserves_complementary_public_and_opaque_fields():
    owner = ResponsesCompletionAccumulator(builder=ProviderLiveItemBuilder())
    a = reasoning(format="openai-responses-v1", summary=[{"type": "summary_text", "text": "summary"}])
    b = reasoning(status="completed", encrypted_content="opaque",
                  content=[{"type": "reasoning_text", "text": "reasoning"}])
    item_done(owner, 0, a)
    result = completed(owner, [b])
    assert result == [{**a, "status": "completed", "encrypted_content": "opaque", "content": b["content"]}]


@pytest.mark.parametrize("field", ["id", "status", "phase"])
@pytest.mark.parametrize("rich_source", ["done", "terminal"])
def test_message_optional_fields_enrich_in_either_direction(field, rich_source):
    owner = ResponsesCompletionAccumulator(builder=ProviderLiveItemBuilder())
    value = {"id": "message:provider", "status": "completed", "phase": "commentary"}[field]
    a, b = message(), message()
    (a if rich_source == "done" else b)[field] = value
    item_done(owner, 0, a)
    assert completed(owner, [b]) == [message(**{field: value})]


def test_terminal_fills_other_items_without_reordering_partial_done_sequence():
    owner = ResponsesCompletionAccumulator(builder=ProviderLiveItemBuilder())
    a = reasoning(encrypted_content="done-opaque")
    item_done(owner, 0, a)
    final_message = message(id="message:provider", status="completed")
    assert completed(owner, [reasoning(encrypted_content="terminal-opaque"), final_message]) == [a, final_message]


def test_terminal_can_omit_function_call_fields_without_losing_the_exact_call():
    owner = ResponsesCompletionAccumulator(builder=ProviderLiveItemBuilder())
    tool = {"type": "function_call", "id": "fc_provider", "call_id": "call_provider",
            "name": "read_file", "arguments": '{ "x": 1 }', "status": "completed"}
    item_done(owner, 0, tool)
    assert completed(owner, [{"type": "function_call", "name": "read_file"}]) == [tool]


def test_part_done_and_item_snapshots_union_metadata_without_mutating_events():
    owner = ResponsesCompletionAccumulator(builder=ProviderLiveItemBuilder())
    part = {"type": "output_text", "text": "done", "logprobs": []}
    event = {"type": "response.content_part.done", "output_index": 0, "content_index": 0,
             "item_id": "message:provider", "part": part}
    a = message(id="message:provider", content=[{"type": "output_text", "text": "done"}])
    citation = {"type": "url_citation", "url": "https://example.test", "title": "source", "start_index": 0, "end_index": 4}
    b = message(content=[{"type": "output_text", "text": "done", "annotations": [citation]}])
    originals = deepcopy((event, a, b))
    owner.apply(event)
    item_done(owner, 0, a)
    result = completed(owner, [b])
    assert result == [message(id="message:provider", content=[{**part, "annotations": [citation]}])]
    assert (event, a, b) == originals


@pytest.mark.parametrize("changed", [
    message(content=[{"type": "output_text", "text": "changed"}]),
    message(phase="commentary"),
    message(id="message:other"),
    message(content=[{"type": "output_text", "text": "done"}, {"type": "output_text", "text": "extra"}]),
])
def test_union_does_not_overwrite_conflicting_message_facts(changed):
    owner = ResponsesCompletionAccumulator(builder=ProviderLiveItemBuilder())
    item_done(owner, 0, message(id="message:provider", phase="final_answer"))
    with pytest.raises(LLMTransportContractError):
        completed(owner, [changed])


@pytest.mark.parametrize("field,other", [("call_id", "call_other"), ("name", "write_file"), ("arguments", '{"x":2}')])
def test_union_does_not_overwrite_tool_identity_or_arguments(field, other):
    owner = ResponsesCompletionAccumulator(builder=ProviderLiveItemBuilder())
    item = {"type": "function_call", "id": "fc_provider", "call_id": "call_provider",
            "name": "read_file", "arguments": '{"x":1}'}
    item_done(owner, 0, item)
    with pytest.raises(LLMTransportContractError):
        completed(owner, [{**item, field: other}])


@pytest.mark.parametrize("field", ["summary", "content"])
def test_public_reasoning_cannot_use_the_opaque_conflict_exception(field):
    owner = ResponsesCompletionAccumulator(builder=ProviderLiveItemBuilder())
    kind = "summary_text" if field == "summary" else "reasoning_text"
    a = reasoning(**{field: [{"type": kind, "text": "first"}]}, encrypted_content="a")
    b = reasoning(**{field: [{"type": kind, "text": "second"}]}, encrypted_content="b")
    item_done(owner, 0, a)
    with pytest.raises(LLMTransportContractError):
        completed(owner, [b])


def test_two_nonempty_annotation_sets_are_not_combined_by_guessing_order():
    owner = ResponsesCompletionAccumulator(builder=ProviderLiveItemBuilder())
    a = message(content=[{"type": "output_text", "text": "done", "annotations": [{"type": "first"}]}])
    b = message(content=[{"type": "output_text", "text": "done", "annotations": [{"type": "second"}]}])
    item_done(owner, 0, a)
    with pytest.raises(LLMTransportContractError):
        completed(owner, [b])


@pytest.mark.parametrize("field", ["unknown_item", "unknown_part"])
def test_union_never_drops_unknown_fields_to_make_an_item_pass(field):
    owner = ResponsesCompletionAccumulator(builder=ProviderLiveItemBuilder())
    a, b = message(), message()
    if field == "unknown_item":
        b["future_semantic_instruction"] = "unknown"
    else:
        b["content"][0]["future_semantic_instruction"] = "unknown"
    item_done(owner, 0, a)
    with pytest.raises(LLMTransportContractError):
        completed(owner, [b])


def test_merge_checks_aggregate_size_after_both_sources_fit(monkeypatch):
    owner = ResponsesCompletionAccumulator(builder=ProviderLiveItemBuilder())
    a = reasoning(summary=[{"type": "summary_text", "text": "a" * 100}])
    b = reasoning(content=[{"type": "reasoning_text", "text": "b" * 100}])
    boundary = max(len(canonical_json_bytes([a])), len(canonical_json_bytes([b])))
    monkeypatch.setattr(responses, "MAX_COMPLETED_PROVIDER_RESPONSE_AGGREGATE_BYTES", boundary)
    item_done(owner, 0, a)
    with pytest.raises(LLMTransportContractError, match="Merged Responses output aggregate"):
        completed(owner, [b])
    assert owner.terminal is None


def test_part_and_item_duplicate_carriers_do_not_halve_the_output_budget(monkeypatch):
    owner = ResponsesCompletionAccumulator(builder=ProviderLiveItemBuilder())
    part = {"type": "output_text", "text": "a" * 300}
    item = message(id="message:provider", content=[part])
    boundary = len(canonical_json_bytes([item]))
    monkeypatch.setattr(responses, "MAX_COMPLETED_PROVIDER_RESPONSE_AGGREGATE_BYTES", boundary)
    owner.apply({"type": "response.content_part.done", "output_index": 0, "content_index": 0,
                 "item_id": item["id"], "part": part})
    item_done(owner, 0, item)
    assert completed(owner, [item]) == [item]


def test_part_representation_has_the_existing_aggregate_boundary(monkeypatch):
    owner = ResponsesCompletionAccumulator(builder=ProviderLiveItemBuilder())
    monkeypatch.setattr(responses, "MAX_COMPLETED_PROVIDER_RESPONSE_AGGREGATE_BYTES", 100)
    with pytest.raises(LLMTransportContractError, match="content_part.done aggregate"):
        owner.apply({"type": "response.content_part.done", "output_index": 0, "content_index": 0,
                     "item_id": "message:provider", "part": {"type": "output_text", "text": "x" * 100}})


def test_changed_content_part_owner_cannot_be_hidden_by_field_union():
    owner = ResponsesCompletionAccumulator(builder=ProviderLiveItemBuilder())
    owner.apply({"type": "response.content_part.done", "output_index": 0, "content_index": 0,
                 "item_id": "message:other", "part": {"type": "output_text", "text": "done"}})
    with pytest.raises(LLMTransportContractError, match="different item"):
        completed(owner, [message(id="message:provider")])


@pytest.mark.parametrize("rich_source", ["done", "terminal"])
def test_empty_message_parts_can_be_filled_without_inventing_text(rich_source):
    owner = ResponsesCompletionAccumulator(builder=ProviderLiveItemBuilder())
    a, b = message(content=[]), message()
    if rich_source == "done":
        a, b = b, a
    item_done(owner, 0, a)
    assert completed(owner, [b]) == [message()]


def test_null_message_content_is_not_healed_into_a_valid_array():
    owner = ResponsesCompletionAccumulator(builder=ProviderLiveItemBuilder())
    item_done(owner, 0, message(content=None))
    with pytest.raises(LLMTransportContractError):
        completed(owner, [message()])


@pytest.mark.parametrize("metadata", ["annotations", "logprobs"])
@pytest.mark.parametrize("invalid", [3, "bad", {}])
def test_malformed_part_metadata_cannot_be_hidden_by_an_empty_other_source(metadata, invalid):
    owner = ResponsesCompletionAccumulator(builder=ProviderLiveItemBuilder())
    a = message(content=[{"type": "output_text", "text": "done", metadata: invalid}])
    b = message(content=[{"type": "output_text", "text": "done", metadata: []}])
    item_done(owner, 0, a)
    with pytest.raises(LLMTransportContractError):
        completed(owner, [b])


def test_terminal_cannot_hide_an_added_item_outside_its_complete_sequence():
    owner = ResponsesCompletionAccumulator(builder=ProviderLiveItemBuilder())
    owner.apply({"type": "response.output_item.added", "output_index": 1, "item": reasoning()})
    with pytest.raises(LLMTransportContractError, match="omits a streamed output item"):
        completed(owner, [message()])


def test_nonnull_reasoning_format_conflict_still_fails():
    owner = ResponsesCompletionAccumulator(builder=ProviderLiveItemBuilder())
    item_done(owner, 0, reasoning(format="openai-responses-v1"))
    with pytest.raises(LLMTransportContractError):
        completed(owner, [reasoning(format="different-format")])


def test_part_owner_enriches_an_omitted_terminal_id_without_changing_input():
    owner = ResponsesCompletionAccumulator(builder=ProviderLiveItemBuilder())
    part = {"type": "output_text", "text": "done"}
    owner.apply({"type": "response.content_part.done", "output_index": 0, "content_index": 0,
                 "item_id": "message:provider", "part": part})
    source = message()
    before = deepcopy(source)
    assert completed(owner, [source]) == [message(id="message:provider")]
    assert source == before
