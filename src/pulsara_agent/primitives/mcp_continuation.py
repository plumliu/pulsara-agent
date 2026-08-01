"""Event-safe MCP multi-round continuation authority.

No plaintext request state, response value, retry parameters, URL, ciphertext,
or unkeyed digest of those values is permitted in this module.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any, Literal, TypeAlias, TypeVar

from pydantic import Field, model_validator

from pulsara_agent.primitives._context_base import (
    ContextEventReferenceFact,
    FrozenJsonObjectFact,
    context_fingerprint,
)
from pulsara_agent.primitives.frozen import (
    FrozenFactBase,
    build_frozen_fact,
    register_durable_fact,
)
from pulsara_agent.primitives.mcp_protocol import McpClientInputMethod


Fingerprint: TypeAlias = str


def _fact(schema_version: str, own_field: str, domain_separator: str):
    def decorate(cls):
        register_durable_fact(
            schema_version=schema_version,
            own_fingerprint_field=own_field,
            domain_separator=domain_separator,
        )
        return cls

    return decorate


class McpContinuationCarrierState(StrEnum):
    AWAITING_CLIENT_INPUT = "awaiting_client_input"
    REPLAY_READY = "replay_ready"
    DISPATCH_RESERVED = "dispatch_reserved"


class McpContinuationCompanionKind(StrEnum):
    SUSPENSION_INSERT = "suspension_insert"
    RESOLUTION_REPLAY_READY = "resolution_replay_ready"
    DISPATCH_RESERVE = "dispatch_reserve"
    SUCCESSOR_REPLACE = "successor_replace"
    TERMINAL_DELETE = "terminal_delete"


@_fact(
    "mcp_form_elicitation_request.v1",
    "request_fingerprint",
    "mcp-form-elicitation-request:v1",
)
class McpFormElicitationRequestFact(FrozenFactBase):
    schema_version: Literal["mcp_form_elicitation_request.v1"]
    key: str = Field(min_length=1)
    method: Literal[McpClientInputMethod.ELICITATION_CREATE]
    mode: Literal["form"]
    wire_mode_was_omitted: bool
    message: str
    requested_schema: FrozenJsonObjectFact
    requested_schema_fingerprint: Fingerprint
    request_fingerprint: Fingerprint


@_fact(
    "mcp_url_elicitation_request.v1",
    "request_fingerprint",
    "mcp-url-elicitation-request:v1",
)
class McpUrlElicitationRequestFact(FrozenFactBase):
    schema_version: Literal["mcp_url_elicitation_request.v1"]
    key: str = Field(min_length=1)
    method: Literal[McpClientInputMethod.ELICITATION_CREATE]
    mode: Literal["url"]
    message: str
    display_origin: str
    ascii_host: str
    unicode_host: str
    explicit_port: int | None = Field(default=None, ge=1, le=65535)
    punycode_warning_required: bool
    commitment_key_id: str
    keyed_full_url_commitment: str
    url_policy_fingerprint: Fingerprint
    request_fingerprint: Fingerprint


McpElicitationRequestFact: TypeAlias = (
    McpFormElicitationRequestFact | McpUrlElicitationRequestFact
)


@_fact(
    "mcp_continuation_bounds.v1",
    "bounds_fingerprint",
    "mcp-continuation-bounds:v1",
)
class McpContinuationBoundsFact(FrozenFactBase):
    schema_version: Literal["mcp_continuation_bounds.v1"]
    maximum_request_state_utf8_bytes: int = Field(ge=1)
    maximum_retryable_base_params_bytes: int = Field(ge=1)
    maximum_current_round_response_bytes: int = Field(ge=1)
    maximum_input_requests_event_bytes: int = Field(ge=1)
    maximum_private_url_utf8_bytes: int = Field(ge=1)
    maximum_plaintext_bytes: int = Field(ge=1)
    maximum_ciphertext_bytes: int = Field(ge=17)
    maximum_stored_envelope_bytes: int = Field(ge=1)
    maximum_input_requests: int = Field(ge=1)
    maximum_rounds: int = Field(ge=1)
    maximum_ttl_seconds: int = Field(ge=1)
    bounds_fingerprint: Fingerprint

    @model_validator(mode="after")
    def _physical_bounds(self) -> "McpContinuationBoundsFact":
        if self.maximum_ciphertext_bytes < self.maximum_plaintext_bytes + 16:
            raise ValueError("MCP ciphertext bound must include the AEAD tag")
        if self.maximum_stored_envelope_bytes < self.maximum_ciphertext_bytes:
            raise ValueError("stored envelope bound cannot be smaller than ciphertext")
        return self


@_fact(
    "mcp_continuation_expiry.v1",
    "expiry_fingerprint",
    "mcp-continuation-expiry:v1",
)
class McpContinuationExpiryFact(FrozenFactBase):
    schema_version: Literal["mcp_continuation_expiry.v1"]
    first_input_required_observed_at_utc: str
    resolved_operation_ttl_seconds: int = Field(ge=1)
    operation_expires_at_utc: str
    expiry_policy_fingerprint: Fingerprint
    expiry_fingerprint: Fingerprint


@_fact(
    "mcp_input_required_durable_continuation.v1",
    "continuation_fact_fingerprint",
    "mcp-input-required-durable-continuation:v1",
)
class McpInputRequiredDurableContinuationFact(FrozenFactBase):
    schema_version: Literal["mcp_input_required_durable_continuation.v1"]
    continuation_carrier_id: str
    initial_carrier_state: Literal["awaiting_client_input"]
    carrier_plaintext_commitment: str
    retryable_base_params_commitment: str
    request_state_commitment: str | None
    retryable_payload_kind: Literal["tool_call", "resource_read", "prompt_get"]
    source_method: Literal["tools/call", "resources/read", "prompts/get"]
    source_method_schema_fingerprint: Fingerprint
    request_set_fingerprint: Fingerprint
    stored_envelope_fingerprint: Fingerprint
    commitment_key_id: str
    bounds: McpContinuationBoundsFact
    protocol_semantic_fingerprint: Fingerprint
    endpoint_attribution_fingerprint: Fingerprint
    auth_attribution_fingerprint: Fingerprint
    binding_contract_fingerprint: Fingerprint
    round_ordinal: int = Field(ge=1)
    expiry: McpContinuationExpiryFact
    continuation_fact_fingerprint: Fingerprint

    @model_validator(mode="after")
    def _method_kind(self) -> "McpInputRequiredDurableContinuationFact":
        expected = {
            "tools/call": "tool_call",
            "resources/read": "resource_read",
            "prompts/get": "prompt_get",
        }[self.source_method]
        if self.retryable_payload_kind != expected:
            raise ValueError("MCP retry payload kind/method mismatch")
        if self.round_ordinal > self.bounds.maximum_rounds:
            raise ValueError("MCP continuation round exceeds frozen bounds")
        if self.expiry.resolved_operation_ttl_seconds > self.bounds.maximum_ttl_seconds:
            raise ValueError("MCP continuation expiry exceeds frozen bounds")
        return self


@_fact(
    "mcp_input_required_resolution.v2",
    "resolution_semantic_fingerprint",
    "mcp-input-required-resolution:v2",
)
class McpInputRequiredResolutionSemanticFact(FrozenFactBase):
    schema_version: Literal["mcp_input_required_resolution.v2"]
    request_set_fingerprint: Fingerprint
    ordered_response_keys: tuple[str, ...]
    commitment_key_id: str
    keyed_current_round_responses_commitment: str
    response_attribution_fingerprint: Fingerprint
    resolution_semantic_fingerprint: Fingerprint

    @model_validator(mode="after")
    def _keys(self) -> "McpInputRequiredResolutionSemanticFact":
        if (
            not self.ordered_response_keys
            or self.ordered_response_keys != tuple(sorted(self.ordered_response_keys))
            or len(self.ordered_response_keys) != len(set(self.ordered_response_keys))
        ):
            raise ValueError("MCP response keys must be non-empty, ordered, and unique")
        return self


@_fact(
    "mcp_continuation_resolution_carrier.v1",
    "resolution_carrier_fact_fingerprint",
    "mcp-continuation-resolution-carrier:v1",
)
class McpContinuationResolutionCarrierFact(FrozenFactBase):
    schema_version: Literal["mcp_continuation_resolution_carrier.v1"]
    source_continuation_carrier_id: str
    replay_continuation_carrier_id: str
    source_suspension_event_reference: ContextEventReferenceFact
    source_carrier_plaintext_commitment: str
    source_stored_envelope_fingerprint: Fingerprint
    replay_plaintext_commitment: str
    retryable_base_params_commitment: str
    ordered_response_keys: tuple[str, ...]
    keyed_current_round_responses_commitment: str
    response_attribution_fingerprint: Fingerprint
    replay_stored_envelope_fingerprint: Fingerprint
    retryable_payload_kind: Literal["tool_call", "resource_read", "prompt_get"]
    source_method: Literal["tools/call", "resources/read", "prompts/get"]
    source_method_schema_fingerprint: Fingerprint
    request_set_fingerprint: Fingerprint
    commitment_key_id: str
    bounds_fingerprint: Fingerprint
    protocol_semantic_fingerprint: Fingerprint
    endpoint_attribution_fingerprint: Fingerprint
    auth_attribution_fingerprint: Fingerprint
    binding_contract_fingerprint: Fingerprint
    resolution_event_id: str
    round_ordinal: int = Field(ge=1)
    operation_expires_at_utc: str
    expiry_fingerprint: Fingerprint
    resolution_carrier_fact_fingerprint: Fingerprint

    @model_validator(mode="after")
    def _distinct_carrier(self) -> "McpContinuationResolutionCarrierFact":
        if self.source_continuation_carrier_id == self.replay_continuation_carrier_id:
            raise ValueError("resolution must create a distinct replay carrier")
        if self.ordered_response_keys != tuple(sorted(set(self.ordered_response_keys))):
            raise ValueError("resolution response keys must be ordered and unique")
        return self


@_fact(
    "mcp_continuation_dispatch_reservation.v1",
    "dispatch_reservation_fingerprint",
    "mcp-continuation-dispatch-reservation:v1",
)
class McpContinuationDispatchReservationFact(FrozenFactBase):
    schema_version: Literal["mcp_continuation_dispatch_reservation.v1"]
    dispatch_reservation_id: str
    runtime_session_id: str
    interaction_id: str
    physical_operation_id: str
    replay_continuation_carrier_id: str
    source_resolution_event_reference: ContextEventReferenceFact
    source_physical_operation_reservation_event_reference: ContextEventReferenceFact
    expected_control_revision: int = Field(ge=0)
    expected_control_fingerprint: Fingerprint
    resulting_control_revision: int = Field(ge=1)
    resulting_control_fingerprint: Fingerprint
    retryable_payload_kind: Literal["tool_call", "resource_read", "prompt_get"]
    source_method: Literal["tools/call", "resources/read", "prompts/get"]
    source_method_schema_fingerprint: Fingerprint
    protocol_semantic_fingerprint: Fingerprint
    endpoint_attribution_fingerprint: Fingerprint
    auth_attribution_fingerprint: Fingerprint
    binding_contract_fingerprint: Fingerprint
    sdk_client_generation_id: str
    dispatch_ordinal: int = Field(ge=1)
    operation_expires_at_utc: str
    expiry_fingerprint: Fingerprint
    dispatch_reservation_fingerprint: Fingerprint

    @model_validator(mode="after")
    def _control_revision(self) -> "McpContinuationDispatchReservationFact":
        if self.resulting_control_revision != self.expected_control_revision + 1:
            raise ValueError("MCP dispatch control revision must advance exactly once")
        return self


@_fact(
    "mcp_continuation_companion_charge.v1",
    "charge_fingerprint",
    "mcp-continuation-companion-charge:v1",
)
class McpContinuationCompanionChargeFact(FrozenFactBase):
    schema_version: Literal["mcp_continuation_companion_charge.v1"]
    companion_kind: McpContinuationCompanionKind
    charged_payload_bytes: int = Field(ge=0)
    charge_contract_fingerprint: Fingerprint
    storage_mutation_plan_fingerprint: Fingerprint
    charge_fingerprint: Fingerprint

    @model_validator(mode="after")
    def _zero_charge_matrix(self) -> "McpContinuationCompanionChargeFact":
        if (
            self.companion_kind
            in {
                McpContinuationCompanionKind.DISPATCH_RESERVE,
                McpContinuationCompanionKind.TERMINAL_DELETE,
            }
            and self.charged_payload_bytes != 0
        ):
            raise ValueError("control/delete companion payload charge must be zero")
        return self


@_fact(
    "mcp_continuation_companion_plan.v1",
    "plan_fingerprint",
    "mcp-continuation-companion-plan:v1",
)
class McpContinuationCompanionPlanFact(FrozenFactBase):
    schema_version: Literal["mcp_continuation_companion_plan.v1"]
    companion_kind: McpContinuationCompanionKind
    runtime_session_id: str
    source_event_id: str
    source_continuation_carrier_id: str | None
    resulting_continuation_carrier_id: str | None
    expected_row_state: McpContinuationCarrierState | None
    resulting_row_state: McpContinuationCarrierState | None
    expected_control_revision: int | None = Field(default=None, ge=0)
    expected_control_fingerprint: Fingerprint | None
    resulting_control_revision: int | None = Field(default=None, ge=0)
    resulting_control_fingerprint: Fingerprint | None
    source_stored_envelope_fingerprint: Fingerprint | None
    resulting_stored_envelope_fingerprint: Fingerprint | None
    ordered_candidate_event_ids: tuple[str, ...]
    ordered_candidate_schema_binding_fingerprints: tuple[Fingerprint, ...]
    ordered_candidate_payload_fingerprints: tuple[Fingerprint, ...]
    exact_ordered_batch_fingerprint: Fingerprint
    charge: McpContinuationCompanionChargeFact
    plan_fingerprint: Fingerprint

    @model_validator(mode="after")
    def _plan_matrix(self) -> "McpContinuationCompanionPlanFact":
        if self.charge.companion_kind is not self.companion_kind:
            raise ValueError("MCP companion charge kind mismatch")
        expected_pair = (
            self.expected_control_revision,
            self.expected_control_fingerprint,
        )
        resulting_pair = (
            self.resulting_control_revision,
            self.resulting_control_fingerprint,
        )
        if any(value is None for value in expected_pair) != all(
            value is None for value in expected_pair
        ):
            raise ValueError("MCP companion expected-control identity is partial")
        if any(value is None for value in resulting_pair) != all(
            value is None for value in resulting_pair
        ):
            raise ValueError("MCP companion resulting-control identity is partial")
        if self.companion_kind is McpContinuationCompanionKind.SUSPENSION_INSERT:
            if expected_pair != (None, None) or None in resulting_pair:
                raise ValueError("MCP suspension insert control matrix is invalid")
        elif self.companion_kind is McpContinuationCompanionKind.TERMINAL_DELETE:
            if None in expected_pair or resulting_pair != (None, None):
                raise ValueError("MCP terminal delete control matrix is invalid")
        else:
            if None in expected_pair or None in resulting_pair:
                raise ValueError("MCP continuation transition requires both controls")
            if self.resulting_control_revision != self.expected_control_revision + 1:
                raise ValueError(
                    "MCP companion control revision must advance exactly once"
                )
        count = len(self.ordered_candidate_event_ids)
        if (
            count == 0
            or len(self.ordered_candidate_schema_binding_fingerprints) != count
            or len(self.ordered_candidate_payload_fingerprints) != count
            or len(set(self.ordered_candidate_event_ids)) != count
        ):
            raise ValueError("MCP companion requires one exact ordered event batch")
        if self.source_event_id not in self.ordered_candidate_event_ids:
            raise ValueError("MCP companion source event is absent from its batch")
        return self


_ContinuationFactT = TypeVar("_ContinuationFactT", bound=FrozenFactBase)


def build_mcp_continuation_fact(
    fact_type: type[_ContinuationFactT],
    /,
    **payload: Any,
) -> _ContinuationFactT:
    return build_frozen_fact(fact_type, **payload)


def default_mcp_continuation_bounds() -> McpContinuationBoundsFact:
    return build_mcp_continuation_fact(
        McpContinuationBoundsFact,
        schema_version="mcp_continuation_bounds.v1",
        maximum_request_state_utf8_bytes=64 * 1024,
        maximum_retryable_base_params_bytes=256 * 1024,
        maximum_current_round_response_bytes=64 * 1024,
        maximum_input_requests_event_bytes=64 * 1024,
        maximum_private_url_utf8_bytes=8 * 1024,
        maximum_plaintext_bytes=512 * 1024,
        maximum_ciphertext_bytes=512 * 1024 + 16,
        maximum_stored_envelope_bytes=576 * 1024,
        maximum_input_requests=64,
        maximum_rounds=10,
        maximum_ttl_seconds=1800,
    )


def mcp_continuation_lifetime_reservation_bytes(
    bounds: McpContinuationBoundsFact,
) -> int:
    """Reserve every immutable awaiting/replay envelope in one bounded operation."""

    return bounds.maximum_stored_envelope_bytes * bounds.maximum_rounds * 2


def mcp_continuation_charge_contract_fingerprint(
    bounds: McpContinuationBoundsFact,
) -> Fingerprint:
    return context_fingerprint(
        "mcp-continuation-companion-charge-contract:v1",
        {
            "bounds_fingerprint": bounds.bounds_fingerprint,
            "lifetime_reservation_bytes": (
                mcp_continuation_lifetime_reservation_bytes(bounds)
            ),
            "charge_rule": "canonical-storage-envelope-bytes-lifetime",
            "control_and_delete_charge_bytes": 0,
        },
    )


__all__ = [name for name in globals() if name.startswith("Mcp")] + [
    "build_mcp_continuation_fact",
    "default_mcp_continuation_bounds",
    "mcp_continuation_charge_contract_fingerprint",
    "mcp_continuation_lifetime_reservation_bytes",
]
