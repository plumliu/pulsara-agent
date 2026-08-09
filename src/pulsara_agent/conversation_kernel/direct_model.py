"""Direct Stage 2 provider adapter without model lifecycle persistence."""

from __future__ import annotations

import json
from dataclasses import replace
from typing import AsyncIterator, Sequence
from uuid import uuid4

from pulsara_agent.conversation_kernel.reader import (
    ProviderInputItem,
    ProviderInputItemKind,
)
from pulsara_agent.conversation_kernel.runner import KernelModelRequest
from pulsara_agent.llm.adapters.openai.chat_completions import (
    OpenAIChatCompletionsTransport,
)
from pulsara_agent.llm.adapters.openai.responses import OpenAIResponsesTransport
from pulsara_agent.llm.config import LLMConfig
from pulsara_agent.llm.input import LLMMessage, LLMToolCall, ToolSpec
from pulsara_agent.llm.estimator import estimate_model_context_for_call
from pulsara_agent.llm.models import ModelRole
from pulsara_agent.llm.normalized_transport import (
    NormalizedLLMTransport,
    NormalizedLLMTransportRegistry,
)
from pulsara_agent.llm.request import LLMContext, LLMOptions
from pulsara_agent.llm.resolution import resolve_model_call, resolve_model_target
from pulsara_agent.llm.validation import validate_model_context_for_call
from pulsara_agent.primitives.model_call import ModelCallPurpose
from pulsara_agent.ports.live_agent_event import ProviderStreamPayload
from pulsara_agent.ports.provider_stream import (
    ProviderPhysicalCompletionStatus,
    ProviderStreamTerminal,
)


class DirectKernelModelPort:
    """One transport execution per ``stream`` call, with no durable lifecycle."""

    def __init__(
        self,
        *,
        config: LLMConfig,
        tools: Sequence[ToolSpec] = (),
        system_prompt: str | None = None,
        role: ModelRole = ModelRole.PRO,
        options: LLMOptions | None = None,
    ) -> None:
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
        self._config = config
        self._registry = registry
        self._tools = tuple(tools)
        self._system_prompt = system_prompt
        self._role = role
        self._options = options

    async def stream(
        self, request: KernelModelRequest
    ) -> AsyncIterator[ProviderStreamPayload]:
        target = resolve_model_target(
            config=self._config,
            registry=self._registry,
            role=self._role,
            requested_options=self._options,
        )
        call = resolve_model_call(
            target=target,
            purpose=ModelCallPurpose.AGENT_MODEL_LOOP,
            resolved_model_call_id=f"model_call:{uuid4().hex}",
        )
        if (
            target.context_budget.effective_output_tokens
            > request.maximum_output_tokens
        ):
            raise ValueError(
                "resolved provider output exceeds the foreground attempt cap"
            )
        context = LLMContext(
            messages=tuple(
                _to_llm_message(item) for item in request.provider_input.items
            ),
            context_id=f"context:{uuid4().hex}",
            resolved_model_call_id=call.resolved_model_call_id,
            target_fingerprint=target.fact.target_fingerprint,
            model_call_index=request.model_call_index,
            tools=self._tools,
            system_prompt=request.system_prompt or self._system_prompt,
            compiler_estimated_input_tokens=None,
        )
        estimate = estimate_model_context_for_call(call=call, context=context)
        if estimate.total_input_tokens > request.maximum_input_tokens:
            raise ValueError("provider input exceeds the foreground attempt cap")
        context = replace(
            context,
            compiler_estimated_input_tokens=estimate.total_input_tokens,
        )
        validated = validate_model_context_for_call(call=call, context=context)
        if validated.estimate.total_input_tokens != estimate.total_input_tokens:
            raise RuntimeError("provider input estimate changed before dispatch")
        execution = target.transport.open_stream(call=call, context=context)
        try:
            while True:
                item = await execution.read_next()
                if item is None:
                    break
                if isinstance(item, ProviderStreamTerminal):
                    if item.outcome != "COMPLETED":
                        assert item.error is not None
                        raise RuntimeError(f"provider failed: {item.error.code.value}")
                    break
                yield item
        finally:
            await execution.aclose()
            completion = await execution.wait_physical_completion()
            if completion.status is not ProviderPhysicalCompletionStatus.COMPLETED:
                raise RuntimeError("provider physical operation did not exit")


def _to_llm_message(item: ProviderInputItem) -> LLMMessage:
    if item.item_kind in {
        ProviderInputItemKind.CONTEXT_SNAPSHOT,
        ProviderInputItemKind.USER,
        ProviderInputItemKind.LATE_TOOL_OUTCOME,
    }:
        prefix = {
            ProviderInputItemKind.CONTEXT_SNAPSHOT: "[CONTEXT_SNAPSHOT]",
            ProviderInputItemKind.USER: "",
            ProviderInputItemKind.LATE_TOOL_OUTCOME: "[RUNTIME_LATE_TOOL_OUTCOME]",
        }[item.item_kind]
        text = item.text if not prefix else f"{prefix}\n{item.text}"
        return LLMMessage.user(text)
    if item.item_kind is ProviderInputItemKind.ASSISTANT:
        return LLMMessage.assistant(item.text)
    if item.item_kind is ProviderInputItemKind.ASSISTANT_TOOL_REQUEST:
        return LLMMessage.assistant_turn(
            text=item.text or None,
            tool_calls=tuple(
                LLMToolCall(
                    id=call.tool_call_id,
                    name=call.tool_name,
                    arguments=json.dumps(
                        dict(call.arguments),
                        ensure_ascii=False,
                        sort_keys=True,
                        separators=(",", ":"),
                    ),
                )
                for call in item.tool_calls
            ),
        )
    if item.item_kind in {
        ProviderInputItemKind.TOOL_RESULT,
        ProviderInputItemKind.TOOL_RESULT_CLOSURE,
    }:
        if item.tool_call_id is None:
            raise ValueError("tool result input lacks call identity")
        return LLMMessage.tool_result(item.text, tool_call_id=item.tool_call_id)
    raise TypeError(item.item_kind)


__all__ = ["DirectKernelModelPort"]
