"""One-call bounded provider port for Stage 2 first-party jobs."""

from __future__ import annotations

from dataclasses import dataclass, replace
import json
from typing import Mapping
from uuid import uuid4

from pulsara_agent.llm.adapters.openai.chat_completions import (
    OpenAIChatCompletionsTransport,
)
from pulsara_agent.llm.adapters.openai.responses import OpenAIResponsesTransport
from pulsara_agent.llm.config import LLMConfig, ModelSlotConfig
from pulsara_agent.llm.input import LLMMessage
from pulsara_agent.llm.estimator import estimate_model_context_for_call
from pulsara_agent.llm.models import ModelRole
from pulsara_agent.llm.normalized_transport import (
    NormalizedLLMTransport,
    NormalizedLLMTransportRegistry,
)
from pulsara_agent.llm.request import LLMContext
from pulsara_agent.llm.resolution import (
    ResolvedModelCall,
    resolve_model_call,
    resolve_model_target,
)
from pulsara_agent.llm.validation import validate_model_context_for_call
from pulsara_agent.primitives.model_call import ModelCallPurpose, ModelContextLimits
from pulsara_agent.ports.live_agent_event import (
    TextDeltaPayload,
    TextEndPayload,
    TextStartPayload,
)
from pulsara_agent.ports.provider_stream import (
    ProviderPhysicalCompletionStatus,
    ProviderStreamTerminal,
)


@dataclass(frozen=True, slots=True)
class PreparedKernelJobModelCall:
    call: ResolvedModelCall
    context: LLMContext
    estimated_input_tokens: int
    maximum_result_bytes: int


class DirectKernelJobModel:
    """Direct model operation with no model-lifecycle persistence or replay."""

    def __init__(self, config: LLMConfig) -> None:
        self._config = config

    async def complete_json(
        self,
        *,
        purpose: ModelCallPurpose,
        prompt: str,
        maximum_input_tokens: int,
        maximum_output_tokens: int,
        maximum_result_bytes: int = 256 << 10,
    ) -> Mapping[str, object]:
        prepared = self.prepare_json_call(
            purpose=purpose,
            prompt=prompt,
            maximum_input_tokens=maximum_input_tokens,
            maximum_output_tokens=maximum_output_tokens,
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
        maximum_result_bytes: int = 256 << 10,
    ) -> PreparedKernelJobModelCall:
        config = _with_output_cap(self._config, maximum_output_tokens)
        registry = NormalizedLLMTransportRegistry()
        registry.register(
            NormalizedLLMTransport(
                OpenAIResponsesTransport(
                    api_key=config.api_key,
                    retry_config=config.retry,
                    openai_sdk_max_retries=config.openai_sdk_max_retries,
                )
            )
        )
        registry.register(
            NormalizedLLMTransport(
                OpenAIChatCompletionsTransport(
                    api_key=config.api_key,
                    retry_config=config.retry,
                    openai_sdk_max_retries=config.openai_sdk_max_retries,
                )
            )
        )
        target = resolve_model_target(
            config=config,
            registry=registry,
            role=ModelRole.FLASH,
            requested_options=None,
        )
        call = resolve_model_call(
            target=target,
            purpose=purpose,
            resolved_model_call_id=f"model_call:{uuid4().hex}",
        )
        context = LLMContext(
            messages=(LLMMessage.user(prompt),),
            context_id=f"job-context:{uuid4().hex}",
            resolved_model_call_id=call.resolved_model_call_id,
            target_fingerprint=target.fact.target_fingerprint,
            model_call_index=None,
            compiler_estimated_input_tokens=None,
        )
        estimate = estimate_model_context_for_call(call=call, context=context)
        if estimate.total_input_tokens > maximum_input_tokens:
            raise ValueError("job provider input exceeds its per-attempt cap")
        context = replace(
            context,
            compiler_estimated_input_tokens=estimate.total_input_tokens,
        )
        validated = validate_model_context_for_call(call=call, context=context)
        if validated.estimate.total_input_tokens != estimate.total_input_tokens:
            raise RuntimeError("job provider input estimate changed before dispatch")
        return PreparedKernelJobModelCall(
            call=call,
            context=context,
            estimated_input_tokens=estimate.total_input_tokens,
            maximum_result_bytes=maximum_result_bytes,
        )

    async def complete_prepared_json(
        self, prepared: PreparedKernelJobModelCall
    ) -> Mapping[str, object]:
        call = prepared.call
        execution = call.target.transport.open_stream(
            call=call, context=prepared.context
        )
        block_order: list[str] = []
        open_blocks: dict[str, list[str]] = {}
        completed_blocks: dict[str, str] = {}
        size = 0
        try:
            while True:
                item = await execution.read_next()
                if item is None:
                    break
                if isinstance(item, ProviderStreamTerminal):
                    if item.outcome != "COMPLETED":
                        assert item.error is not None
                        raise RuntimeError(
                            f"job provider failed: {item.error.code.value}"
                        )
                    break
                if isinstance(item, TextStartPayload):
                    if item.block_identity in open_blocks:
                        raise RuntimeError("job provider reused a text block identity")
                    block_order.append(item.block_identity)
                    open_blocks[item.block_identity] = []
                elif isinstance(item, TextDeltaPayload):
                    if item.block_identity not in open_blocks:
                        raise RuntimeError("job provider text delta lacks start")
                    encoded = item.delta.encode("utf-8")
                    size += len(encoded)
                    if size > prepared.maximum_result_bytes:
                        raise ValueError("job provider result exceeds its byte cap")
                    open_blocks[item.block_identity].append(item.delta)
                elif isinstance(item, TextEndPayload):
                    if item.block_identity not in open_blocks:
                        raise RuntimeError("job provider text end lacks start")
                    if item.final_text != "".join(open_blocks[item.block_identity]):
                        raise RuntimeError("job provider terminal text drifted")
                    completed_blocks[item.block_identity] = item.final_text
                    del open_blocks[item.block_identity]
                else:
                    # Jobs have no tools, data blocks, or model-visible thinking.
                    continue
        finally:
            await execution.aclose()
            completion = await execution.wait_physical_completion()
            if completion.status is not ProviderPhysicalCompletionStatus.COMPLETED:
                raise RuntimeError("job provider physical operation did not exit")
        if open_blocks:
            raise RuntimeError("job provider ended with open text blocks")
        value = json.loads("".join(completed_blocks[item] for item in block_order))
        if not isinstance(value, dict):
            raise ValueError("job provider result must be a JSON object")
        return value


def _with_output_cap(config: LLMConfig, maximum_output_tokens: int) -> LLMConfig:
    if maximum_output_tokens < 1:
        raise ValueError("job provider output cap must be positive")
    slot = config.flash
    limits = slot.limits
    cap = min(maximum_output_tokens, limits.max_output_tokens)
    bounded = ModelContextLimits(
        total_context_tokens=limits.total_context_tokens,
        max_input_tokens=min(
            limits.max_input_tokens, limits.total_context_tokens - cap
        ),
        max_output_tokens=limits.max_output_tokens,
        default_output_tokens=cap,
        input_safety_margin_tokens=min(
            limits.input_safety_margin_tokens,
            max(0, limits.total_context_tokens - cap - 1),
        ),
    )
    return replace(config, flash=ModelSlotConfig(slot.model_id, bounded))


__all__ = ["DirectKernelJobModel", "PreparedKernelJobModelCall"]
