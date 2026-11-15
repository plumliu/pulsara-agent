"""Globally bound immutable image facts for tests after image validation."""

from pulsara_agent.conversation_kernel.prompt_content import bind_prompt_images
from pulsara_agent.llm.input import FrozenPromptContent, LLMImagePart


def bound_test_image(
    media_type: str,
    immutable_bytes: bytes,
    width: int,
    height: int,
    image_ref: str | None = None,
) -> LLMImagePart:
    image = LLMImagePart(media_type, immutable_bytes, width, height, image_ref)
    return bind_prompt_images(FrozenPromptContent((image,))).parts[0]
