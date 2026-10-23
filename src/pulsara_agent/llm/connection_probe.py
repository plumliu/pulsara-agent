"""One bounded, non-persistent probe through the selected generic wire adapter."""

from __future__ import annotations

from pulsara_agent.identity import random_id

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
from pulsara_agent.llm.provider_open import EphemeralProbeCredentialOwner
from pulsara_agent.llm.ephemeral import PreparedEphemeralRequest, EphemeralRequestFailure, execute_ephemeral_request
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
            context_id=random_id("context"),
            resolved_model_call_id=call.resolved_model_call_id,
            model_call_index=None,
        )
        prepared = PreparedEphemeralRequest(
            call_target=_freeze_provider_physical_call_target(
                target=target, call=call,
                maximum_input_tokens=target.context_budget.input_budget_tokens,
            ),
            context=context, timeout_policy=_PROBE_TIMEOUT, credential_owner=credential_owner,
        )
        # Incomplete output still proves reachability for this purpose only.
        await execute_ephemeral_request(runtime=runtime, prepared=prepared)
    except EphemeralRequestFailure as exc:
        raise ModelConnectionProbeFailure(exc.code, exc.message) from exc
    finally:
        credential_owner.close()


__all__ = ["ModelConnectionProbeFailure", "probe_model_connection"]
