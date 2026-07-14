"""Provider bindings for runtime-owned inert observation messages."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from pulsara_agent.primitives.model_call import (
    RuntimeDerivedObservationCarrierContractFact,
    sha256_fingerprint,
)


@dataclass(frozen=True, slots=True)
class RuntimeDerivedObservationCarrierBinding:
    contract: RuntimeDerivedObservationCarrierContractFact
    implementation_build_fingerprint: str
    wire_role: Literal["developer", "system"]


def runtime_observation_carrier_for_api(
    api: str,
    *,
    allow_nonproduction_api: bool = False,
) -> RuntimeDerivedObservationCarrierContractFact | None:
    """Return the canonical V1 carrier or explicit unsupported ``None``."""

    supported_apis = {"openai_responses", "openai_chat_completions", "mock"}
    if api not in supported_apis and not allow_nonproduction_api:
        return None
    wire_role: Literal["developer", "system"] = (
        "system" if api == "openai_chat_completions" else "developer"
    )
    carrier_suffix = "system_message" if wire_role == "system" else "developer_message"
    payload = {
        "schema_version": "runtime_derived_observation_carrier.v1",
        "carrier_id": f"pulsara.runtime_observation.{carrier_suffix}",
        "carrier_version": "v1",
        "provider_api": api,
        "provider_role_contract": "runtime_inert_observation",
        "wire_shape_fingerprint": sha256_fingerprint(
            "runtime-derived-observation-wire-shape:v1",
            {
                "provider_api": api,
                "wire_role": wire_role,
                "content_shape": "string",
                "tool_call_identity": "forbidden",
            },
        ),
    }
    return RuntimeDerivedObservationCarrierContractFact(
        **payload,
        contract_fingerprint=sha256_fingerprint(
            "runtime-derived-observation-carrier:v1", payload
        ),
    )


def resolve_runtime_observation_binding(
    contract: RuntimeDerivedObservationCarrierContractFact,
) -> RuntimeDerivedObservationCarrierBinding:
    # Production resolution never creates an unknown-API contract. The
    # non-production branch exists so exact replay and compiler tests can bind
    # contracts produced by test-only scripted transports with distinct APIs.
    expected = runtime_observation_carrier_for_api(
        contract.provider_api,
        allow_nonproduction_api=True,
    )
    if expected is None or expected != contract:
        raise ValueError("runtime observation carrier binding is unavailable")
    return RuntimeDerivedObservationCarrierBinding(
        contract=contract,
        implementation_build_fingerprint=sha256_fingerprint(
            "runtime-derived-observation-implementation:v1",
            {"implementation": "pulsara.llm.runtime_observation", "version": "v1"},
        ),
        wire_role=(
            "system"
            if contract.provider_api == "openai_chat_completions"
            else "developer"
        ),
    )


__all__ = [
    "RuntimeDerivedObservationCarrierBinding",
    "resolve_runtime_observation_binding",
    "runtime_observation_carrier_for_api",
]
