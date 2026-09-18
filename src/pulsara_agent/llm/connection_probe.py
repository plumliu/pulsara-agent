"""One bounded, non-persistent probe through the selected generic wire adapter."""

from __future__ import annotations

from dataclasses import dataclass

from pulsara_agent.llm.adapters.openai.client import OpenAITransportTimeoutPolicy
from pulsara_agent.llm.input import LLMMessage
from pulsara_agent.llm.model_catalog import SelectableModelCatalog
from pulsara_agent.llm.model_connections import ModelCallBinding
from pulsara_agent.llm.model_target import (
    ResolvedModelConnection,
    RouteWireRegistry,
    default_reasoning_selection,
)
from pulsara_agent.llm.request import LLMContext
from pulsara_agent.llm.resolution import (
    resolve_model_call,
    resolve_model_target,
    with_call_output_cap,
)
from pulsara_agent.llm.retry import LLMRetryConfig
from pulsara_agent.llm.runtime import ModelRuntime
from pulsara_agent.llm.frozen_target import _freeze_provider_physical_call_target
from pulsara_agent.llm.provider_open import (
    EphemeralProbeCredentialOwner,
    _issue_connection_probe_provider_open_permit,
)
from pulsara_agent.ports.provider_stream import (
    ProviderNormalizedTerminalKind,
    ProviderPhysicalCompletionStatus,
    ProviderStreamTerminal,
)
from pulsara_agent.primitives.model_call import ModelCallPurpose


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


@dataclass(frozen=True, slots=True)
class _TransientCatalogOwner:
    snapshot: SelectableModelCatalog | None

    def selectable(self) -> SelectableModelCatalog | None:
        return self.snapshot


async def probe_model_connection(
    *,
    resolved: ResolvedModelConnection,
    catalog: SelectableModelCatalog | None,
    route_wires: RouteWireRegistry,
    api_key: str | None,
) -> None:
    """Send one tiny request without publishing the draft configuration."""

    credential_owner = EphemeralProbeCredentialOwner(
        resolved.config, api_key or ""
    )
    runtime = ModelRuntime(
        settings=credential_owner,  # type: ignore[arg-type]
        catalog=_TransientCatalogOwner(catalog),  # type: ignore[arg-type]
        route_wires=route_wires,
        retry=LLMRetryConfig(enabled=False, attempts=1),
    )
    execution = None
    borrowed = None
    try:
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
                registry=runtime.transport_registry(_PROBE_TIMEOUT),
            ),
            _PROBE_OUTPUT_TOKENS,
        )
        call = resolve_model_call(
            target=target,
            binding=binding,
            purpose=ModelCallPurpose.CONNECTION_PROBE,
        )
        context = LLMContext(
            messages=(LLMMessage.user("Reply with OK."),),
            context_id="model-connection-probe",
            resolved_model_call_id=call.resolved_model_call_id,
            model_call_index=None,
        )
        permit = _issue_connection_probe_provider_open_permit(
            probe_call_target=_freeze_provider_physical_call_target(
                target=target,
                call=call,
                maximum_input_tokens=target.context_budget.input_budget_tokens,
                maximum_output_tokens=_PROBE_OUTPUT_TOKENS,
            ),
            resolved_model_call_id=call.resolved_model_call_id,
            timeout_policy=_PROBE_TIMEOUT,
            credential_owner=credential_owner,
        )
        borrowed = runtime.borrow_transport(permit)
        if borrowed.call.fact != call.fact:
            raise RuntimeError("borrowed connection probe call drifted")
        execution = borrowed.open_stream(context=context)
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
                physical = await execution.wait_physical_completion()
                if physical.status is not ProviderPhysicalCompletionStatus.COMPLETED:
                    raise ModelConnectionProbeFailure(
                        "transport_protocol_error",
                        "测试请求的物理连接未完整释放。",
                    )
        finally:
            if borrowed is not None:
                borrowed.close()
            else:
                credential_owner.close()


__all__ = ["ModelConnectionProbeFailure", "probe_model_connection"]
