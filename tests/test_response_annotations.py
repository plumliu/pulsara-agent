import asyncio
from dataclasses import replace
from io import BytesIO
from time import monotonic
import json

from PIL import Image
import pytest

from pulsara_agent.llm.input import (
    PromptAnnotationPart,
    PromptAnnotationSource,
    PromptContent,
    FrozenPromptContent,
    LLMTextPart,
    PromptImagePart,
    LLMImagePart,
    LLMMessage,
    MessageRole,
    prompt_provider_parts,
    prompt_text_projection,
    llm_content_logical_bytes,
)
from pulsara_agent.conversation_kernel.annotations import (
    utf16_slice,
    remap_annotation_sources,
)
from pulsara_agent.conversation_kernel.prompt_content import (
    freeze_canonical_prompt,
    hydrate_canonical_prompt_body,
)
from pulsara_agent.conversation_kernel.image_validation import HostPromptImageValidator
from pulsara_agent.web_app.browser_bridge import _prompt_content_from_json
from pulsara_agent.terminal_protocol.v3_gateway import _prompt_content_from_wire
from pulsara_agent.model_input.contracts import (
    FrozenProviderInputItem,
    FrozenProviderInputItemKind,
    CanonicalInputOriginKind,
    FrozenRetainedHistoricalRequest,
    StructuredModelInputLimits,
)
from pulsara_agent.model_input.lowering import (
    lower_canonical_item,
    lower_retained_request_content,
)


def annotation(quote="中文😀 &amp;", comment="$pulsara-docs 解释一下"):
    return PromptAnnotationPart(
        quote,
        PromptAnnotationSource("entry:source", 0, len(quote.encode("utf-16-le")) // 2),
        comment,
    )


def test_codec_provider_intent_and_protocol_roundtrip():
    a = annotation()
    content = FrozenPromptContent((a, LLMTextPart("原文不变")))
    frozen = freeze_canonical_prompt(content)
    assert hydrate_canonical_prompt_body(body=frozen.body, image_payloads=()) == frozen
    raw = json.loads(frozen.body)
    raw.pop("schema")
    assert _prompt_content_from_wire(_prompt_content_from_json(raw)) == PromptContent(
        content.parts
    )
    assert prompt_text_projection(content) == "$pulsara-docs 解释一下\n原文不变"
    inert = FrozenPromptContent((annotation("$skill 不使用记忆", None),))
    assert prompt_text_projection(inert) == ""
    parts = prompt_provider_parts(content.parts)
    assert parts[-1].text == "原文不变"
    assert a.quote in parts[0].text
    assert all(
        "entry:source" not in part.text and '"source"' not in part.text
        for part in parts
    )
    assert frozen.resource_quote.epoch_logical_bytes == llm_content_logical_bytes(parts)
    item = FrozenProviderInputItem(
        item_kind=FrozenProviderInputItemKind.USER,
        source_entry_id="entry:user",
        source_entry_sequence=2,
        source_turn_id="turn:1",
        input_origin=CanonicalInputOriginKind.HUMAN_MESSAGE,
        content=content.parts,
    )
    assert (
        lower_canonical_item(
            item, artifact_read_available=False, limits=StructuredModelInputLimits()
        ).fixed_message.content
        == parts
    )
    retained = FrozenRetainedHistoricalRequest(
        item.item_kind, item.input_origin, content
    )
    assert lower_retained_request_content(retained) == parts
    with pytest.raises(TypeError):
        LLMMessage(MessageRole.USER, content.parts)
    with pytest.raises(TypeError):
        replace(item, item_kind=FrozenProviderInputItemKind.TERMINAL_OBSERVATION)


def test_utf16_and_fork_sources_preserve_model_projection():
    assert utf16_slice("甲😀乙e\u0301", 1, 3) == "😀"
    for start, end in [(1, 2), (2, 3), (0, 99), (True, 2), (-1, 2)]:
        with pytest.raises(ValueError):
            utf16_slice("甲😀乙", start, end)
    original = FrozenPromptContent((annotation(),))
    child = remap_annotation_sources(original, {"entry:source": "entry:child"})
    assert child.parts[0].source.entry_id == "entry:child"
    grandchild = remap_annotation_sources(child, {"entry:child": "entry:grandchild"})
    missing = remap_annotation_sources(grandchild, {})
    assert missing.parts[0].source is None
    assert prompt_provider_parts(original.parts) == prompt_provider_parts(missing.parts)
    assert (
        hydrate_canonical_prompt_body(
            body=freeze_canonical_prompt(missing).body, image_payloads=()
        ).content
        == missing
    )
    with pytest.raises(ValueError):
        PromptContent(missing.parts)


def test_quote_preserves_canonical_line_endings_without_relaxing_active_input():
    quote = "前\r\n后\r尾"
    content = FrozenPromptContent((annotation(quote, None),))
    frozen = freeze_canonical_prompt(content)
    assert frozen.resource_quote.text_utf8_bytes == len(quote.encode("utf-8"))
    assert (
        hydrate_canonical_prompt_body(body=frozen.body, image_payloads=()).content
        == content
    )
    assert "\\r\\n" in prompt_provider_parts(content.parts)[0].text
    with pytest.raises(ValueError, match="control character"):
        PromptContent.text(quote)
    with pytest.raises(ValueError, match="control character"):
        PromptContent((annotation("quote", quote),))


@pytest.mark.parametrize(
    "origin",
    [CanonicalInputOriginKind.HUMAN_MESSAGE, CanonicalInputOriginKind.HUMAN_STEER],
)
def test_dispatch_anchor_uses_comment_intent_and_excludes_quote(origin):
    from types import SimpleNamespace
    from tests.test_round3_structured_model_input_compiler import (
        _snapshot,
        _append_anchor,
    )
    from pulsara_agent.conversation_kernel.provider_dispatch import (
        _activation_subject_for_anchor,
    )
    from pulsara_agent.model_input.contracts import CapabilityActivationSubjectKind

    item = FrozenProviderInputItem(
        item_kind=FrozenProviderInputItemKind.USER,
        source_entry_id="entry:user",
        source_entry_sequence=2,
        source_turn_id="turn:test",
        input_origin=origin,
        content=(annotation("$quoted-skill 使用记忆", "$pulsara-docs 不使用记忆"),),
    )
    snapshot = _snapshot(
        item,
        canonical_expanded_bytes=freeze_canonical_prompt(
            FrozenPromptContent(item.content)
        ).resource_quote.canonical_expanded_bytes,
    )
    anchor = _append_anchor(SimpleNamespace(canonical_input=snapshot))
    assert _activation_subject_for_anchor(snapshot, anchor) == (
        CapabilityActivationSubjectKind.ROOT_HUMAN_PROMPT,
        "$pulsara-docs 不使用记忆",
    )
    item = replace(item, content=(annotation("$quoted-skill 不使用记忆", None),))
    snapshot = _snapshot(
        item,
        canonical_expanded_bytes=freeze_canonical_prompt(
            FrozenPromptContent(item.content)
        ).resource_quote.canonical_expanded_bytes,
    )
    assert (
        _activation_subject_for_anchor(
            snapshot, _append_anchor(SimpleNamespace(canonical_input=snapshot))
        )[1]
        == ""
    )


def test_image_freezer_preserves_annotations_and_image_ordinals():
    asyncio.run(_image_freezer_check())


async def _image_freezer_check():
    validator = HostPromptImageValidator()
    a = annotation()
    empty = await validator.freeze(
        PromptContent((a,)), deadline_monotonic=monotonic() + 30
    )
    assert empty.parts == (a,)
    image = BytesIO()
    Image.new("RGB", (2, 3)).save(image, format="PNG")
    mixed = await validator.freeze(
        PromptContent(
            (a, PromptImagePart(image.getvalue(), "image/png"), LLMTextPart("后"), a)
        ),
        deadline_monotonic=monotonic() + 30,
    )
    assert isinstance(mixed.parts[1], LLMImagePart)
    frozen = freeze_canonical_prompt(mixed)
    assert [(x.ref_ordinal, x.part_index) for x in frozen.image_occurrences] == [(0, 1)]
    assert (
        hydrate_canonical_prompt_body(
            body=frozen.body, image_payloads=(image.getvalue(),)
        )
        == frozen
    )
    await validator.aclose()
