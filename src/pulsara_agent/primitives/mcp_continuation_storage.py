"""Storage-only encrypted MCP continuation records."""

from __future__ import annotations

from typing import Any, Literal, TypeVar

from pydantic import Field, model_validator

from pulsara_agent.primitives.mcp_continuation import McpContinuationCarrierState
from pulsara_agent.primitives.storage_frozen import (
    FrozenStorageFactBase,
    build_frozen_storage_fact,
    register_durable_storage_fact,
)


def _storage_fact(schema_version: str, own_field: str, domain_separator: str):
    def decorate(cls):
        register_durable_storage_fact(
            schema_version=schema_version,
            own_fingerprint_field=own_field,
            domain_separator=domain_separator,
        )
        return cls

    return decorate


@_storage_fact(
    "mcp_stored_continuation_envelope.v1",
    "stored_envelope_fingerprint",
    "mcp-stored-continuation-envelope:v1",
)
class McpStoredContinuationEnvelopeFact(FrozenStorageFactBase):
    schema_version: Literal["mcp_stored_continuation_envelope.v1"]
    continuation_carrier_id: str = Field(min_length=1)
    carrier_kind: Literal["awaiting_client_input", "replay_ready"]
    algorithm: Literal["AES-256-GCM"]
    key_id: str = Field(min_length=1)
    nonce_bytes: bytes = Field(min_length=12, max_length=12)
    ciphertext_bytes: bytes = Field(min_length=17, max_length=512 * 1024 + 16)
    aad_fingerprint: str
    carrier_plaintext_commitment: str
    created_at_utc: str
    operation_expires_at_utc: str
    expiry_fingerprint: str
    stored_envelope_fingerprint: str


@_storage_fact(
    "mcp_continuation_carrier_control.v1",
    "control_fingerprint",
    "mcp-continuation-carrier-control:v1",
)
class McpContinuationCarrierControlFact(FrozenStorageFactBase):
    schema_version: Literal["mcp_continuation_carrier_control.v1"]
    continuation_carrier_id: str = Field(min_length=1)
    carrier_state: McpContinuationCarrierState
    control_revision: int = Field(ge=0)
    source_event_id: str = Field(min_length=1)
    stored_envelope_fingerprint: str
    control_fingerprint: str

    @model_validator(mode="after")
    def _initial_revision(self) -> "McpContinuationCarrierControlFact":
        if self.carrier_state in {
            McpContinuationCarrierState.AWAITING_CLIENT_INPUT,
            McpContinuationCarrierState.REPLAY_READY,
        } and self.control_revision < 1:
            raise ValueError("MCP continuation control starts at revision one")
        return self


McpContinuationStorageFact = (
    McpStoredContinuationEnvelopeFact | McpContinuationCarrierControlFact
)

_StorageT = TypeVar("_StorageT", bound=FrozenStorageFactBase)


def build_mcp_continuation_storage_fact(
    fact_type: type[_StorageT],
    /,
    **payload: Any,
) -> _StorageT:
    return build_frozen_storage_fact(fact_type, **payload)


__all__ = [
    "McpContinuationCarrierControlFact",
    "McpContinuationStorageFact",
    "McpStoredContinuationEnvelopeFact",
    "build_mcp_continuation_storage_fact",
]
