"""Narrow durable-job adapter over the purpose-neutral auxiliary JSON port."""

from __future__ import annotations

from typing import Mapping

from pulsara_agent.conversation_kernel.auxiliary_model import (
    DirectKernelAuxiliaryJsonModel,
    PreparedAuxiliaryJsonModelCall,
)
from pulsara_agent.llm.adapters.openai.client import OpenAITransportTimeoutPolicy
from pulsara_agent.llm.config import LLMConfig
from pulsara_agent.primitives.model_call import ModelCallPurpose
from pulsara_agent.llm.resolution import resolve_model_call, resolve_model_target
from pulsara_agent.llm.validation import validate_model_context_for_call


PreparedKernelJobModelCall = PreparedAuxiliaryJsonModelCall


class DirectKernelJobModel:
    """Durable-job-facing name without granting job claims to other owners."""

    def __init__(self, config: LLMConfig) -> None:
        # Preserve the durable-job adapter's narrow fault-injection seams while
        # the physical implementation remains the purpose-neutral auxiliary leaf.
        self._auxiliary = DirectKernelAuxiliaryJsonModel(
            config,
            target_resolver=resolve_model_target,
            call_resolver=resolve_model_call,
            context_validator=validate_model_context_for_call,
        )

    async def complete_json(
        self,
        *,
        purpose: ModelCallPurpose,
        prompt: str,
        maximum_input_tokens: int,
        maximum_output_tokens: int,
        timeout_policy: OpenAITransportTimeoutPolicy,
        maximum_result_bytes: int = 256 << 10,
    ) -> Mapping[str, object]:
        prepared = self.prepare_json_call(
            purpose=purpose,
            prompt=prompt,
            maximum_input_tokens=maximum_input_tokens,
            maximum_output_tokens=maximum_output_tokens,
            timeout_policy=timeout_policy,
            maximum_result_bytes=maximum_result_bytes,
        )
        return await self.complete_prepared_json(prepared)

    def prepare_json_call(
        self,
        *,
        purpose: ModelCallPurpose,
        prompt: str,
        maximum_input_tokens: int,
        maximum_output_tokens: int,
        timeout_policy: OpenAITransportTimeoutPolicy,
        maximum_result_bytes: int = 256 << 10,
    ) -> PreparedKernelJobModelCall:
        if purpose is not ModelCallPurpose.CONTEXT_COMPACTION_SUMMARY:
            raise ValueError("durable job adapter received a foreign purpose")
        if timeout_policy.total_seconds is None:
            raise ValueError("durable job provider requires an attempt total timeout")
        return self._auxiliary.prepare_json_call(
            purpose=purpose,
            prompt=prompt,
            maximum_input_tokens=maximum_input_tokens,
            maximum_output_tokens=maximum_output_tokens,
            timeout_policy=timeout_policy,
            maximum_result_bytes=maximum_result_bytes,
        )

    async def complete_prepared_json(
        self, prepared: PreparedKernelJobModelCall
    ) -> Mapping[str, object]:
        return await self._auxiliary.complete_prepared_json(prepared)


__all__ = ["DirectKernelJobModel", "PreparedKernelJobModelCall"]
