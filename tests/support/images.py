"""Bound image facts for unit tests that start after workspace admission."""

from pulsara_agent.conversation_kernel.prompt_content import bind_prompt_images
from pulsara_agent.llm.input import FrozenPromptContent, LLMImagePart


def bound_test_image(*args, workspace_id="workspace:" + "a" * 26, **kwargs):
    image = LLMImagePart(*args, **kwargs)
    return bind_prompt_images(FrozenPromptContent((image,)), workspace_id).parts[0]
