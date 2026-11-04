"""Canonical settlement bounds for real previews and late image results."""

from __future__ import annotations
from tests.support.images import bound_test_image

from dataclasses import replace
from io import BytesIO
from time import monotonic

from PIL import Image
import pytest

from pulsara_agent.conversation_kernel.assembler import CompletedToolCallBlock
from pulsara_agent.conversation_kernel.compaction.contracts import (
    provider_input_item_canonical_expanded_bytes,
)
from pulsara_agent.conversation_kernel.prompt_content import freeze_canonical_prompt
from pulsara_agent.conversation_kernel.runner import (
    ConversationKernelRunner,
    FrozenPostResponseResourceQuote,
    OutputResourceInterruption,
    _ordinary_result_followup_upper,
    _tool_result_closure_storage_text,
)
from pulsara_agent.conversation_kernel.tool_result_display import project_native_tool_result
from pulsara_agent.llm.input import (
    FrozenPromptContent,
    LLMTextPart,
    frozen_tool_result_public_text,
)
from pulsara_agent.model_input.contracts import (
    MAXIMUM_CANONICAL_PROVIDER_INPUT_BYTES,
    FrozenProviderInputItemKind,
)
from pulsara_agent.ports.tool_execution import ToolOutputSourceFormatHint
from pulsara_agent.primitives.context import canonical_json_bytes, freeze_json
from pulsara_agent.primitives.tool_result_budget import ToolResultPageBudget
from tests.test_round3_structured_model_input_compiler import _tool_result
from tests.support.tool_outputs import output_processor


def _preview_and_late_charge(body: str) -> tuple[CompletedToolCallBlock, int]:
    processor = output_processor()
    saved = processor.materialize_body(
        session_id='session:6ej3oydnee24zzis2etyixxioi',
        workspace_id='workspace:wdl6b3mtrkghwnvjo4an3ekbau',
        result_entry_id='entry:zvmjajlmix3vyoeoy2zhz6xd2a',
        public_output=body,
        output_body_candidate=None,
        public_format=ToolOutputSourceFormatHint.TEXT,
        deadline_monotonic=monotonic() + 10,
    )
    call = CompletedToolCallBlock(
        'block:eahusw3qjz45fyozavqv6xlbxa', "call:7", "read_file", freeze_json({"target": {"kind": "PATH", "path": "control.txt"}})
    )
    native, omitted = project_native_tool_result(
        public_output=body, native_output=None, page_complete=False, required_facts={},
        output_available=saved.output_body is not None,
        output_unavailability_reason=saved.output_unavailability_reason,
        page_budget=ToolResultPageBudget(call.tool_call_id, {
            "kind": "SESSION_ENTRY", "session_id": 'session:6ej3oydnee24zzis2etyixxioi',
            "entry_id": 'entry:zvmjajlmix3vyoeoy2zhz6xd2a',
        }),
    )
    projection = processor.prepare(body=saved, native_output=native, output_omitted=omitted)
    assert saved.output_body.canonical_bytes == body.encode("utf-8")
    preview = projection.canonical_preview.canonical_bytes.decode("utf-8")
    assert projection.output_omitted == (preview != body)
    # Match the reader's plain LATE_TOOL_OUTCOME storage contract, then charge
    # the hydrated item with the production canonical resource owner.
    late_body = canonical_json_bytes(
        {
            "schema_version": "late_tool_outcome_observation.v1",
            "tool_call_id": call.tool_call_id,
            "result_state": "SUCCESS",
            "result": preview,
        }
    ).decode("utf-8")
    late = replace(
        _tool_result(preview, sequence=7, turn_id='turn:klmmazz67vfgsfsvg6ceqgdsfu'),
        item_kind=FrozenProviderInputItemKind.LATE_TOOL_OUTCOME,
        content=(LLMTextPart(late_body),),
    )
    return call, (
        len(_tool_result_closure_storage_text(call.tool_call_id).encode("utf-8"))
        + provider_input_item_canonical_expanded_bytes(late)
    )


@pytest.mark.parametrize(
    "body",
    (
        "\x01" * 24_000,
        "\\" * 24_000,
        '"' * 24_000,
        "\n" * 24_000,
        "汉🙂" * 3_400,
    ),
    ids=("raw-controls", "slashes", "quotes", "newlines", "unicode"),
)
def test_real_frozen_preview_late_canonical_charge_fits_reserved_upper(body):
    call, actual_canonical = _preview_and_late_charge(body)

    assert actual_canonical <= _ordinary_result_followup_upper(call)[0]


def test_control_character_late_preview_is_rejected_before_canonical_exhaustion():
    call, actual_canonical = _preview_and_late_charge("\x01" * 24_000)
    upper = _ordinary_result_followup_upper(call)[0]
    current = MAXIMUM_CANONICAL_PROVIDER_INPUT_BYTES - actual_canonical + 1
    quote = FrozenPostResponseResourceQuote(
        current_canonical_expanded_bytes=current,
        actual_assistant_canonical_bytes=0,
        bounded_followup_canonical_bytes=upper,
        canonical_upper_after=current + upper,
        current_epoch_logical_bytes=0,
        actual_assistant_logical_bytes=0,
        bounded_followup_logical_bytes=0,
        logical_upper_after=0,
        current_canonical_items=0,
        bounded_followup_items=2,
        item_upper_after=3,
    )

    with pytest.raises(OutputResourceInterruption) as raised:
        ConversationKernelRunner._require_post_response_resources(
            quote, effective_input_budget_tokens=100_000
        )

    assert raised.value.reason == "CANONICAL_BYTES"


def test_late_image_canonical_charge_fits_text_base_plus_exact_image_increment():
    encoded = BytesIO()
    Image.new("RGB", (8, 8), "white").save(encoded, format="PNG")
    image = bound_test_image("image/png", encoded.getvalue(), 8, 8)
    content = FrozenPromptContent((LLMTextPart("Image loaded."), image))
    frozen = freeze_canonical_prompt(content)
    public_text = frozen_tool_result_public_text(content)
    call = CompletedToolCallBlock(
        'block:mst7dl2g5akhu3ok7gosyqjp2q', "call:7", "view_image", freeze_json({"target": {"kind": "PATH", "path": "image.png"}})
    )
    late_body = canonical_json_bytes(
        {
            "schema_version": "late_tool_outcome_observation.v1",
            "tool_call_id": call.tool_call_id,
            "result_state": "SUCCESS",
            "result": public_text,
        }
    ).decode("utf-8")
    late = replace(
        _tool_result(public_text, sequence=7, turn_id='turn:hei4tpo3ncy7wcxg6s4x3caeae'),
        item_kind=FrozenProviderInputItemKind.LATE_TOOL_OUTCOME,
        content=(LLMTextPart(late_body), image),
        tool_call_ordinal=0,
        tool_call_arguments=call.arguments,
        tool_call_name=call.tool_name,
    )
    actual = (
        len(_tool_result_closure_storage_text(call.tool_call_id).encode("utf-8"))
        + provider_input_item_canonical_expanded_bytes(late)
    )

    assert actual <= (
        _ordinary_result_followup_upper(call)[0]
        + frozen.resource_quote.canonical_expanded_bytes
    )
