"""K1 gates for provider-neutral image input facts and pure contracts."""

from __future__ import annotations

import json

import pytest

from pulsara_agent.conversation_kernel.compaction.contracts import (
    CANONICAL_MINIMUM_SERVICE_HEADROOM_BYTES,
    CANONICAL_MINIMUM_SERVICE_HEADROOM_ITEMS,
    EPOCH_MINIMUM_SERVICE_HEADROOM_BYTES,
    resolved_compaction_headroom_bounds,
)
from pulsara_agent.conversation_kernel.limits import STAGE2_LIMITS
from pulsara_agent.conversation_kernel.prompt_content import (
    CanonicalPromptBody,
    MAXIMUM_PROMPT_MULTIPART_BYTES,
    PROMPT_BODY_CODEC,
    PROMPT_BODY_MEDIA_TYPE,
    PROMPT_BODY_SCHEMA,
    PROMPT_IMAGE_BLOB_CODEC,
    canonical_prompt_body_resource_quote,
    decode_canonical_prompt_body,
    freeze_canonical_prompt,
    hydrate_canonical_prompt_body,
)
from pulsara_agent.llm.estimator import (
    PulsaraHeuristicTokenEstimatorV2,
    estimate_image_visual_tokens,
)
from pulsara_agent.llm.input import (
    FrozenPromptContent,
    LLMImagePart,
    LLMMessage,
    LLMTextPart,
    MAXIMUM_PROMPT_IMAGE_PIXELS,
    MAXIMUM_PROMPT_TEXT_UTF8_BYTES,
    MessageRole,
    PromptContent,
    PromptImagePart,
    join_text_content,
    llm_content_identity_value,
    llm_content_logical_bytes,
    prompt_text_projection,
)
from pulsara_agent.model_input.continuity import (
    MAXIMUM_PROVIDER_INPUT_EPOCH_BYTES,
    provider_input_logical_bytes,
    provider_input_prefix_fingerprint,
)
from pulsara_agent.model_input.contracts import (
    MAXIMUM_CANONICAL_PROVIDER_INPUT_BYTES,
    MAXIMUM_CANONICAL_PROVIDER_INPUT_ITEMS,
)
from pulsara_agent.primitives.tool_result_projection import (
    provider_neutral_message_logical_bytes,
)


def _validated_image(
    payload: bytes = b"validated-image-bytes",
    *,
    media_type: str = "image/png",
    width: int = 31,
    height: int = 31,
) -> LLMImagePart:
    # K2 owns actual codec validation; this fixture starts at K1's frozen-value
    # boundary and never claims the placeholder bytes form a decodable image.
    return LLMImagePart(
        media_type=media_type,
        immutable_bytes=payload,
        width=width,
        height=height,
    )


def test_prompt_content_is_one_ordered_typed_ingress_vocabulary() -> None:
    first_text = LLMTextPart("before")
    raw_image = PromptImagePart(b"raw-image", "image/png")
    content = PromptContent((first_text, raw_image, LLMTextPart("after")))

    assert content.parts == (first_text, raw_image, LLMTextPart("after"))
    assert PromptContent.text("unchanged").parts == (LLMTextPart("unchanged"),)
    with pytest.raises(TypeError, match="immutable tuple"):
        PromptContent([first_text])  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="immutable bytes"):
        PromptImagePart(bytearray(b"mutable"), "image/png")  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="typed immutable parts"):
        LLMMessage(MessageRole.USER, ("legacy-string",))  # type: ignore[arg-type]


def test_prompt_content_nonempty_and_text_aggregate_rules_preserve_pure_image() -> None:
    raw_image = PromptImagePart(b"raw-image", "image/webp")
    assert PromptContent((raw_image,)).parts == (raw_image,)

    with pytest.raises(ValueError, match="cannot be empty"):
        PromptContent(())
    with pytest.raises(ValueError, match="contain text or an image"):
        PromptContent((LLMTextPart(""), LLMTextPart("")))
    assert PromptContent((LLMTextPart("a" * MAXIMUM_PROMPT_TEXT_UTF8_BYTES),))
    with pytest.raises(ValueError, match="UTF-8 byte bound"):
        PromptContent(
            (
                LLMTextPart("a" * (MAXIMUM_PROMPT_TEXT_UTF8_BYTES // 2)),
                LLMTextPart("🙂" * (MAXIMUM_PROMPT_TEXT_UTF8_BYTES // 8 + 1)),
            )
        )


def test_prompt_text_keeps_the_existing_control_character_contract() -> None:
    assert MAXIMUM_PROMPT_TEXT_UTF8_BYTES == STAGE2_LIMITS.prompt_hard_bytes
    assert PromptContent((LLMTextPart("line one\n\tline two"),))
    for forbidden in ("nul\0byte", "carriage\rreturn", "escape\x1bcode"):
        with pytest.raises(ValueError, match="forbidden control character"):
            PromptContent((LLMTextPart(forbidden),))
        with pytest.raises(ValueError, match="forbidden control character"):
            CanonicalPromptBody((LLMTextPart(forbidden),))


def test_frozen_image_shape_and_user_role_are_closed() -> None:
    image = _validated_image()
    assert FrozenPromptContent((image,)).parts == (image,)
    assert LLMMessage.user_content(FrozenPromptContent((image,))).content == (image,)

    with pytest.raises(ValueError, match="USER"):
        LLMMessage(MessageRole.ASSISTANT, (image,))
    with pytest.raises(ValueError, match="dimensions"):
        _validated_image(width=MAXIMUM_PROMPT_IMAGE_PIXELS + 1, height=1)
    with pytest.raises(ValueError, match="MIME"):
        _validated_image(media_type="image/gif")


def test_text_only_accessor_fails_closed_and_hook_projection_is_explicit() -> None:
    image = _validated_image()
    frozen = FrozenPromptContent((LLMTextPart("first"), image, LLMTextPart("second")))

    assert prompt_text_projection(frozen) == "first\nsecond"
    assert prompt_text_projection(FrozenPromptContent((image,))) == ""
    with pytest.raises(ValueError, match="text-only consumer"):
        join_text_content(frozen.parts)


def test_complete_content_identity_covers_image_bytes_shape_order_and_occurrence() -> (
    None
):
    image = _validated_image(b"A")
    changed_bytes = _validated_image(b"B")
    changed_shape = _validated_image(b"A", width=32)
    text = LLMTextPart("same")

    assert llm_content_identity_value((text,)) == ("same",)
    base = llm_content_identity_value((text, image, image))
    assert base != llm_content_identity_value((text, changed_bytes, image))
    assert base != llm_content_identity_value((text, changed_shape, image))
    assert base != llm_content_identity_value((image, text, image))
    assert base != llm_content_identity_value((text, image))
    descriptor = base[1]
    assert isinstance(descriptor, dict)
    assert descriptor == {
        "type": "image",
        "digest": image.content_digest,
        "encoded_bytes": 1,
        "media_type": "image/png",
        "width": 31,
        "height": 31,
    }


def test_prefix_identity_and_logical_budget_consume_complete_typed_content() -> None:
    image = _validated_image(b"image-bytes", width=512, height=512)
    changed_shape = _validated_image(b"image-bytes", width=511, height=512)
    message = LLMMessage.user_content(
        FrozenPromptContent((LLMTextPart("before"), image, LLMTextPart("after")))
    )
    changed = LLMMessage.user_content(
        FrozenPromptContent(
            (LLMTextPart("before"), changed_shape, LLMTextPart("after"))
        )
    )
    content_bytes = llm_content_logical_bytes(message.content)

    assert provider_neutral_message_logical_bytes(message) == content_bytes
    assert provider_input_logical_bytes(
        system_prompt="root", tools=(), messages=(message,)
    ) == len(b"root") + content_bytes
    assert provider_input_prefix_fingerprint(
        system_prompt="root", tools=(), messages=(message,)
    ) != provider_input_prefix_fingerprint(
        system_prompt="root", tools=(), messages=(changed,)
    )


def test_canonical_prompt_codec_preserves_interleaving_and_duplicate_refs() -> None:
    image = _validated_image(b"same-image")
    content = FrozenPromptContent(
        (
            LLMTextPart("before"),
            image,
            LLMTextPart("between"),
            image,
        )
    )
    frozen = freeze_canonical_prompt(content)
    value = json.loads(frozen.body)

    assert PROMPT_BODY_MEDIA_TYPE == "application/vnd.pulsara.prompt+json"
    assert PROMPT_BODY_CODEC == "utf-8"
    assert PROMPT_IMAGE_BLOB_CODEC == "binary"
    assert value["schema"] == PROMPT_BODY_SCHEMA == "pulsara.prompt/v1"
    assert [part["type"] for part in value["parts"]] == [
        "text",
        "image",
        "text",
        "image",
    ]
    assert value["parts"][1] == value["parts"][3]
    assert "ref_ordinal" not in value["parts"][1]
    assert "blob_id" not in value["parts"][1]
    assert tuple(item.ref_ordinal for item in frozen.image_occurrences) == (0, 1)
    assert tuple(item.part_index for item in frozen.image_occurrences) == (1, 3)
    assert all(item.image is image for item in frozen.image_occurrences)


def test_canonical_prompt_resource_quote_keeps_units_and_counts_occurrences() -> None:
    image = _validated_image(b"abc", width=1024, height=1024)
    content = FrozenPromptContent((LLMTextPart("🙂"), image, image))
    frozen = freeze_canonical_prompt(content)
    quote = frozen.resource_quote

    assert quote.text_utf8_bytes == 4
    assert quote.canonical_body_bytes == len(frozen.body)
    assert quote.image_occurrence_encoded_bytes == 6
    assert quote.multipart_bytes == len(frozen.body) + 6
    assert quote.canonical_expanded_bytes == quote.multipart_bytes
    assert quote.epoch_logical_bytes == llm_content_logical_bytes(content.parts)
    assert quote.epoch_logical_bytes == 4 + 2 * (3 + len(b"image/png"))
    assert quote.image_occurrences == 2
    assert quote.visual_image_tokens == 2 * 1_198


def test_canonical_prompt_decoder_and_hydration_are_exact() -> None:
    image = _validated_image(b"exact-payload", width=512, height=512)
    original = freeze_canonical_prompt(
        FrozenPromptContent((LLMTextPart("before"), image, LLMTextPart("after")))
    )

    decoded = decode_canonical_prompt_body(original.body)
    assert len(decoded.parts) == 3
    assert (
        hydrate_canonical_prompt_body(
            body=original.body,
            image_payloads=(image.immutable_bytes,),
        )
        == original
    )
    with pytest.raises(ValueError, match="incomplete or excessive"):
        hydrate_canonical_prompt_body(body=original.body, image_payloads=())
    with pytest.raises(ValueError, match="identity mismatch"):
        hydrate_canonical_prompt_body(
            body=original.body,
            image_payloads=(b"wrong-payload",),
        )


@pytest.mark.parametrize(
    "body",
    (
        b'{"parts":[{"text":"x","type":"text"}],"schema":"pulsara.prompt/v1","schema":"pulsara.prompt/v1"}',
        b'{"extra":0,"parts":[{"text":"x","type":"text"}],"schema":"pulsara.prompt/v1"}',
        b'{"parts":[{"extra":0,"text":"x","type":"text"}],"schema":"pulsara.prompt/v1"}',
        b'{"schema": "pulsara.prompt/v1", "parts": [{"type": "text", "text": "x"}]}',
    ),
)
def test_canonical_prompt_decoder_rejects_duplicate_unknown_and_noncanonical_json(
    body: bytes,
) -> None:
    with pytest.raises(ValueError):
        decode_canonical_prompt_body(body)


def test_complete_multipart_limit_uses_exact_body_plus_each_occurrence() -> None:
    assert MAXIMUM_PROMPT_MULTIPART_BYTES == 16 << 20
    oversized_image = _validated_image(b"x" * MAXIMUM_PROMPT_MULTIPART_BYTES)
    with pytest.raises(ValueError, match="multipart bound"):
        freeze_canonical_prompt(FrozenPromptContent((oversized_image,)))


def test_descriptor_quote_rejects_expanded_content_before_hydration() -> None:
    image = _validated_image(b"x")
    body = (
        b'{"parts":[{"digest":"'
        + image.content_digest.encode("ascii")
        + b'","encoded_bytes":16777216,"height":1,"media_type":"image/png",'
        b'"type":"image","width":1}],"schema":"pulsara.prompt/v1"}'
    )
    decoded = decode_canonical_prompt_body(body)

    with pytest.raises(ValueError, match="multipart bound"):
        canonical_prompt_body_resource_quote(decoded)
    with pytest.raises(ValueError, match="multipart bound"):
        hydrate_canonical_prompt_body(body=body, image_payloads=())


@pytest.mark.parametrize(
    ("width", "height", "expected"),
    (
        (31, 31, 256),
        (476, 476, 256),
        (477, 477, 284),
        (512, 512, 316),
        (1_024, 1_024, 1_198),
        (1_920, 1_080, 2_355),
        (4_096, 57, 386),
        (4_096, 4_096, 18_908),
    ),
)
def test_d1_visual_token_formula_is_frozen(
    width: int, height: int, expected: int
) -> None:
    assert estimate_image_visual_tokens(width=width, height=height) == expected


def test_v2_semantic_estimator_charges_each_image_occurrence() -> None:
    message = LLMMessage.user_content(FrozenPromptContent((_validated_image(),)))
    assert PulsaraHeuristicTokenEstimatorV2().estimate_message(message) == 4 + 256


def test_d2_minimum_service_headroom_is_distinct_and_exact() -> None:
    bounds = resolved_compaction_headroom_bounds()

    assert CANONICAL_MINIMUM_SERVICE_HEADROOM_BYTES == 4 << 20
    assert EPOCH_MINIMUM_SERVICE_HEADROOM_BYTES == 4 << 20
    assert CANONICAL_MINIMUM_SERVICE_HEADROOM_ITEMS == 296
    assert bounds.maximum_canonical_expanded_bytes == (
        MAXIMUM_CANONICAL_PROVIDER_INPUT_BYTES
    )
    assert bounds.maximum_epoch_logical_bytes == (
        MAXIMUM_PROVIDER_INPUT_EPOCH_BYTES
    )
    assert bounds.maximum_canonical_items == MAXIMUM_CANONICAL_PROVIDER_INPUT_ITEMS
    assert bounds.reserved_canonical_expanded_bytes == 4 << 20
    assert bounds.reserved_epoch_logical_bytes == 4 << 20
    assert bounds.reserved_canonical_items == 296
