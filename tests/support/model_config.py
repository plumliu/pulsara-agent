"""Production-shaped LLM configuration helpers for Kernel tests."""

from pulsara_agent.llm.config import LLMConfig, ModelSlotConfig
from pulsara_agent.llm.provider import ProviderProfile
from pulsara_agent.llm.retry import LLMRetryConfig
from pulsara_agent.primitives.model_call import ModelContextLimits


def test_model_limits(
    *,
    total_context_tokens: int = 256_000,
    max_input_tokens: int = 256_000,
    max_output_tokens: int = 8_192,
    default_output_tokens: int = 8_000,
    input_safety_margin_tokens: int = 64_000,
) -> ModelContextLimits:
    return ModelContextLimits(
        total_context_tokens=total_context_tokens,
        max_input_tokens=max_input_tokens,
        max_output_tokens=max_output_tokens,
        default_output_tokens=default_output_tokens,
        input_safety_margin_tokens=input_safety_margin_tokens,
    )


def test_model_slot(
    model_id: str,
    *,
    limits: ModelContextLimits | None = None,
) -> ModelSlotConfig:
    return ModelSlotConfig(model_id=model_id, limits=limits or test_model_limits())


def test_llm_config(
    *,
    api_key: str,
    base_url: str,
    pro_model: str,
    flash_model: str,
    api: str = "openai_responses",
    provider: str = "custom",
    provider_profile: ProviderProfile | None = None,
    retry: LLMRetryConfig = LLMRetryConfig(),
    openai_sdk_max_retries: int | None = None,
    pro_limits: ModelContextLimits | None = None,
    flash_limits: ModelContextLimits | None = None,
) -> LLMConfig:
    return LLMConfig(
        api_key=api_key,
        base_url=base_url,
        pro=test_model_slot(pro_model, limits=pro_limits),
        flash=test_model_slot(flash_model, limits=flash_limits),
        api=api,
        provider=provider,
        provider_profile=provider_profile,
        retry=retry,
        openai_sdk_max_retries=openai_sdk_max_retries,
    )


test_model_limits.__test__ = False
test_model_slot.__test__ = False
test_llm_config.__test__ = False


__all__ = ["test_llm_config", "test_model_limits", "test_model_slot"]
