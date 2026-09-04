"""One bounded, non-persistent probe through the selected generic wire adapter."""

from __future__ import annotations

from dataclasses import dataclass, field

from pulsara_agent.llm.adapters.openai.chat_completions import (
    OpenAIChatCompletionsTransport,
)
from pulsara_agent.llm.adapters.openai.client import OpenAITransportTimeoutPolicy
from pulsara_agent.llm.adapters.openai.responses import OpenAIResponsesTransport
from pulsara_agent.llm.input import LLMMessage
from pulsara_agent.llm.model_catalog import SelectableModelCatalog, WireApi
from pulsara_agent.llm.model_connections import ModelCallBinding, ModelConnectionConfig
from pulsara_agent.llm.model_target import (
    ResolvedModelConnection,
    RouteWireRegistry,
    default_reasoning_selection,
)
from pulsara_agent.llm.normalized_transport import (
    NormalizedLLMTransport,
    NormalizedLLMTransportRegistry,
)
from pulsara_agent.llm.request import LLMContext
from pulsara_agent.llm.resolution import (
    resolve_model_call,
    resolve_model_target,
    with_call_output_cap,
)
from pulsara_agent.llm.retry import LLMRetryConfig
from pulsara_agent.ports.provider_stream import (
    ProviderNormalizedTerminalKind,
    ProviderStreamTerminal,
)
from pulsara_agent.primitives.model_call import ModelCallPurpose
from pulsara_agent.settings import LocalModelApiKey, LocalSettings


_PROBE_TIMEOUT = OpenAITransportTimeoutPolicy(
    connect_seconds=10.0,
    write_seconds=10.0,
    pool_seconds=10.0,
    read_idle_seconds=30.0,
    total_seconds=45.0,
)
_PROBE_OUTPUT_TOKENS = 64


class ModelConnectionProbeFailure(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        self.code = code
        self.message = message
        super().__init__(message)


@dataclass(slots=True)
class _TransientSettings:
    connection: ModelConnectionConfig
    _value: str = field(repr=False)

    def read(self) -> LocalSettings:
        api_keys = (
            (LocalModelApiKey(self.connection.id, self._value),)
            if self.connection.requires_api_key and self._value
            else ()
        )
        return LocalSettings(
            model_connections=(self.connection,),
            model_api_keys=api_keys,
        )

    def close(self) -> None:
        self._value = ""


async def probe_model_connection(
    *,
    resolved: ResolvedModelConnection,
    catalog: SelectableModelCatalog | None,
    route_wires: RouteWireRegistry,
    api_key: str | None,
) -> None:
    """Send one tiny request without publishing the draft configuration."""

    settings = _TransientSettings(resolved.config, api_key or "")
    execution = None
    try:
        registry = NormalizedLLMTransportRegistry()
        retry = LLMRetryConfig(enabled=False, attempts=1)
        if resolved.config.target.wire_api is WireApi.OPENAI_CHAT_COMPLETIONS:
            adapter = OpenAIChatCompletionsTransport(
                settings=settings,  # type: ignore[arg-type]
                timeout_policy=_PROBE_TIMEOUT,
                retry_config=retry,
            )
        else:
            adapter = OpenAIResponsesTransport(
                settings=settings,  # type: ignore[arg-type]
                timeout_policy=_PROBE_TIMEOUT,
                retry_config=retry,
            )
        registry.register(NormalizedLLMTransport(adapter))
        binding = ModelCallBinding(
            resolved.config.id,
            default_reasoning_selection(resolved.target.reasoning),
        )
        target = with_call_output_cap(
            resolve_model_target(
                connection=resolved.config,
                binding=binding,
                catalog=catalog,
                route_wires=route_wires,
                registry=registry,
            ),
            _PROBE_OUTPUT_TOKENS,
        )
        call = resolve_model_call(
            target=target,
            binding=binding,
            purpose=ModelCallPurpose.AGENT_MODEL_LOOP,
        )
        context = LLMContext(
            messages=(LLMMessage.user("Reply with OK."),),
            context_id="model-connection-probe",
            resolved_model_call_id=call.resolved_model_call_id,
            target_fingerprint=call.target.fact.target_fingerprint,
            model_call_index=None,
        )
        execution = target.transport.open_stream(call=call, context=context)
        while True:
            item = await execution.read_next()
            if item is None:
                raise ModelConnectionProbeFailure(
                    "transport_protocol_error",
                    "测试请求在协议终态前结束。",
                )
            if not isinstance(item, ProviderStreamTerminal):
                continue
            if item.terminal_kind is ProviderNormalizedTerminalKind.PROVIDER_ERROR:
                assert item.error is not None
                raise ModelConnectionProbeFailure(
                    item.error.code.value,
                    item.error.message,
                )
            return
    finally:
        try:
            if execution is not None:
                await execution.aclose()
        finally:
            settings.close()


__all__ = ["ModelConnectionProbeFailure", "probe_model_connection"]
