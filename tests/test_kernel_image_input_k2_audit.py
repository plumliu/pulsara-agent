from __future__ import annotations
from tests.support.images import bound_test_image

from hashlib import sha256

import pytest

from pulsara_agent.conversation_kernel.blob import PostgresCanonicalBlobStore
from pulsara_agent.conversation_kernel.repository import ConversationKernelConflict
from pulsara_agent.llm.input import (
    FrozenPromptContent,
    LLMImagePart,
    LLMTextPart,
)
from pulsara_agent.model_input.contracts import (
    CanonicalInputOriginKind,
    FrozenProviderInputItem,
    FrozenProviderInputItemKind,
)
from pulsara_agent.conversation_kernel.prompt_content import freeze_canonical_prompt



def _image(payload: bytes = b"validated-image") -> LLMImagePart:
    return bound_test_image("image/png", payload, 1, 1)


def _mixed_content() -> FrozenPromptContent:
    return FrozenPromptContent(
        (LLMTextPart("before"), _image(), LLMTextPart("after"))
    )


@pytest.mark.parametrize(
    "origin",
    (
        CanonicalInputOriginKind.AGENT_INSTRUCTION,
        CanonicalInputOriginKind.USER_CONTROL_FEEDBACK,
    ),
)
def test_original_request_images_accept_agent_instruction_and_reject_feedback(origin):
    def item():
        return FrozenProviderInputItem(
            item_kind=FrozenProviderInputItemKind.USER,
            source_entry_id="entry:2b6xhmrsvzz7q4sgugd5cuq34a",
            source_entry_sequence=1,
            source_turn_id="turn:lm5ibydqxua377vt3bxuuqgvvu",
            input_origin=origin,
            content=_mixed_content().parts,
        )

    if origin is CanonicalInputOriginKind.AGENT_INSTRUCTION:
        assert item().content == _mixed_content().parts
    else:
        with pytest.raises(ValueError, match="cannot carry image"):
            item()


def test_canonical_expanded_bytes_counts_each_original_image_occurrence():
    image = _image()
    prompt = freeze_canonical_prompt(
        FrozenPromptContent((image, LLMTextPart("again"), image))
    )
    assert prompt.resource_quote.canonical_expanded_bytes == (
        len(prompt.body) + 2 * len(image.immutable_bytes)
    )
    assert prompt.resource_quote.image_occurrences == 2
    assert tuple(part.image for part in prompt.image_occurrences) == (image, image)


def test_blob_exact_read_guards_body_projection_with_expected_metadata() -> None:
    class _Result:
        def fetchone(self):
            return {
                "workspace_id": 'workspace:wdl6b3mtrkghwnvjo4an3ekbau',
                "logical_digest": "sha256:" + "0" * 64,
                "logical_size": 1024,
                "media_type": "image/png",
                "codec": "binary",
                "body": None,
            }

    class _Connection:
        sql = ""

        def execute(self, sql, parameters):
            del parameters
            self.sql = sql
            return _Result()

    connection = _Connection()
    expected = "sha256:" + sha256(b"small").hexdigest()
    with pytest.raises(ConversationKernelConflict, match="integrity mismatch"):
        PostgresCanonicalBlobStore.read_exact_in_connection(
            connection,
            blob_id="blob:test",
            expected_digest=expected,
            expected_size=5,
            expected_workspace_id='workspace:wdl6b3mtrkghwnvjo4an3ekbau',
            expected_media_type="image/png",
            expected_codec="binary",
        )
    assert "CASE WHEN logical_digest" in connection.sql
    assert "octet_length(body)" in connection.sql
