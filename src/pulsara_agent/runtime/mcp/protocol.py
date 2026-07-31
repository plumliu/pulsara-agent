"""SDK-conformed MCP protocol lowering owned by Pulsara.

This module accepts plain wire projections from the SDK facade and emits a
closed process-local MRTR leg.  It is the sole parser for elicitation mode,
request-set identity, restricted form schemas, and secret-safe URL facts.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Callable, Literal, Mapping
from urllib.parse import urlsplit

from pydantic import Field, model_validator

from pulsara_agent.ports.mcp_secret import (
    McpPrivateUrlElicitationPayload,
    McpRetryableRequestPayload,
    build_private_url_elicitation_payload,
)
from pulsara_agent.ports.mcp_elicitation import McpElicitationCapabilityFull
from pulsara_agent.primitives._context_base import (
    FrozenJsonObjectFact,
    canonical_json_bytes,
    context_fingerprint,
    freeze_json,
)
from pulsara_agent.primitives.frozen import FrozenRuntimeStateBase, build_frozen_fact
from pulsara_agent.primitives.mcp_continuation import (
    McpContinuationBoundsFact,
    McpElicitationRequestFact,
    McpFormElicitationRequestFact,
    McpUrlElicitationRequestFact,
)
from pulsara_agent.primitives.mcp_protocol import McpClientInputMethod


MCP_STATE_ONLY_RETRY_DELAYS_SECONDS = (0.05, 0.1, 0.2, 0.25)
MCP_INPUT_REQUIRED_MAX_LEGS = 10
MCP_URL_POLICY_FINGERPRINT = context_fingerprint(
    "mcp-elicitation-url-policy:v1",
    {
        "scheme": "https-only",
        "userinfo": "deny",
        "maximum_utf8_bytes": 8 * 1024,
        "prefetch": "deny",
        "redirect_probe": "deny",
    },
)
MCP_FORM_SCHEMA_POLICY_FINGERPRINT = context_fingerprint(
    "mcp-elicitation-form-schema-policy:v1",
    {
        "credential_fields": "deny-recursive",
        "root": "object",
        "additional_properties": "schema-controlled",
        "response_validation": "exact-request-schema",
    },
)

_CREDENTIAL_RE = re.compile(
    r"(?i)(?:password|passcode|passwd|secret|token|api[_ -]?key|private[_ -]?key|credential|authorization|auth[_ -]?code)"
)


class McpInputRequiredContractError(RuntimeError):
    pass


class McpUnadvertisedInputRequest(McpInputRequiredContractError):
    pass


class McpElicitationUrlPolicyRejected(McpInputRequiredContractError):
    pass


@dataclass(frozen=True, slots=True)
class McpClientInputRuntimeBinding:
    """Live capability required to advertise and lower elicitation requests."""

    commitment_key_id: str
    elicitation_capability: McpElicitationCapabilityFull
    _keyed_commitment: Callable[[str, bytes], str] = field(repr=False)

    def __post_init__(self) -> None:
        if (
            not self.commitment_key_id
            or self.elicitation_capability.capability_kind != "full"
        ):
            raise ValueError("MCP client-input runtime binding is incomplete")

    @property
    def host_contract_fingerprint(self) -> str:
        return self.elicitation_capability.contract_fingerprint

    def keyed_commitment(self, domain: str, payload: bytes) -> str:
        return self._keyed_commitment(domain, payload)

    def __reduce__(self):
        raise TypeError("MCP client-input runtime binding is process-local")


class McpStateOnlyRetryLeg(FrozenRuntimeStateBase):
    leg_kind: Literal["state_only"] = "state_only"
    request_state: str = Field(repr=False)
    leg_ordinal: int = Field(ge=1, le=MCP_INPUT_REQUIRED_MAX_LEGS)
    retryable_payload_fingerprint: str
    operation_deadline_monotonic: float
    leg_fingerprint: str

    @model_validator(mode="after")
    def _identity(self) -> "McpStateOnlyRetryLeg":
        expected = context_fingerprint(
            "mcp-state-only-retry-leg:v1",
            {
                "leg_kind": self.leg_kind,
                "request_state": self.request_state,
                "leg_ordinal": self.leg_ordinal,
                "retryable_payload_fingerprint": self.retryable_payload_fingerprint,
                "operation_deadline_monotonic": self.operation_deadline_monotonic,
            },
        )
        if self.leg_fingerprint != expected:
            raise ValueError("MCP state-only leg fingerprint mismatch")
        return self

    def model_dump(self, *args, **kwargs):
        del args, kwargs
        raise TypeError("MCP requestState leg cannot be generically serialized")

    def model_dump_json(self, *args, **kwargs):
        del args, kwargs
        raise TypeError("MCP requestState leg cannot be generically serialized")

    def __reduce__(self):
        raise TypeError("MCP requestState leg cannot be pickled")


class McpClientInputRequiredLeg(FrozenRuntimeStateBase):
    leg_kind: Literal["client_input_required"] = "client_input_required"
    input_requests: tuple[McpElicitationRequestFact, ...] = Field(min_length=1)
    ordered_request_keys: tuple[str, ...] = Field(min_length=1)
    request_set_fingerprint: str
    request_state: str | None = Field(repr=False)
    leg_ordinal: int = Field(ge=1, le=MCP_INPUT_REQUIRED_MAX_LEGS)
    retryable_payload_fingerprint: str
    operation_deadline_monotonic: float
    leg_fingerprint: str

    @model_validator(mode="after")
    def _identity(self) -> "McpClientInputRequiredLeg":
        keys = tuple(item.key for item in self.input_requests)
        if (
            keys != self.ordered_request_keys
            or keys != tuple(sorted(set(keys)))
        ):
            raise ValueError("MCP client-input request key set drifted")
        expected_set = context_fingerprint(
            "mcp-input-request-set:v1",
            tuple((item.key, item.request_fingerprint) for item in self.input_requests),
        )
        if self.request_set_fingerprint != expected_set:
            raise ValueError("MCP input request set fingerprint mismatch")
        expected_leg = context_fingerprint(
            "mcp-client-input-required-leg:v1",
            {
                "leg_kind": self.leg_kind,
                "input_requests": self.input_requests,
                "ordered_request_keys": self.ordered_request_keys,
                "request_set_fingerprint": self.request_set_fingerprint,
                "request_state": self.request_state,
                "leg_ordinal": self.leg_ordinal,
                "retryable_payload_fingerprint": self.retryable_payload_fingerprint,
                "operation_deadline_monotonic": self.operation_deadline_monotonic,
            },
        )
        if self.leg_fingerprint != expected_leg:
            raise ValueError("MCP client-input leg fingerprint mismatch")
        return self

    def model_dump(self, *args, **kwargs):
        del args, kwargs
        raise TypeError("MCP requestState leg cannot be generically serialized")

    def model_dump_json(self, *args, **kwargs):
        del args, kwargs
        raise TypeError("MCP requestState leg cannot be generically serialized")

    def __reduce__(self):
        raise TypeError("MCP requestState leg cannot be pickled")


McpInputRequiredLeg = McpStateOnlyRetryLeg | McpClientInputRequiredLeg


@dataclass(frozen=True, slots=True)
class McpClientInputRequired:
    interaction_id: str
    server_id: str
    exact_protocol_revision: str
    protocol_semantic_fingerprint: str
    endpoint_attribution_fingerprint: str
    auth_attribution_fingerprint: str
    sdk_client_generation_id: str
    leg: McpClientInputRequiredLeg
    retryable_request_payload: McpRetryableRequestPayload
    private_url_payloads: tuple[McpPrivateUrlElicitationPayload, ...]
    continuation_bounds: McpContinuationBoundsFact
    first_input_required_observed_at_utc: str

    def __post_init__(self) -> None:
        if self.leg.retryable_payload_fingerprint != (
            self.retryable_request_payload.process_local_payload_fingerprint
        ):
            raise ValueError("MCP client-input leg/retry payload identity mismatch")
        private_keys = tuple(item.request_key for item in self.private_url_payloads)
        expected = tuple(
            item.key
            for item in self.leg.input_requests
            if isinstance(item, McpUrlElicitationRequestFact)
        )
        if private_keys != expected:
            raise ValueError("MCP private URL payload order differs from request set")
        if self.leg.leg_ordinal > self.continuation_bounds.maximum_rounds:
            raise ValueError("MCP client-input leg exceeds its continuation bounds")


def lower_input_required_result(
    *,
    input_requests: Mapping[str, Mapping[str, object]] | None,
    request_state: object,
    leg_ordinal: int,
    retryable_payload: McpRetryableRequestPayload,
    operation_deadline_monotonic: float,
    commitment_key_id: str,
    keyed_commitment: Callable[[str, bytes], str],
    elicitation_advertised: bool,
    bounds: McpContinuationBoundsFact,
) -> tuple[McpInputRequiredLeg, tuple[McpPrivateUrlElicitationPayload, ...]]:
    if (
        leg_ordinal < 1
        or leg_ordinal > MCP_INPUT_REQUIRED_MAX_LEGS
        or leg_ordinal > bounds.maximum_rounds
    ):
        raise McpInputRequiredContractError("MCP input-required rounds exceeded")
    if request_state is not None and not isinstance(request_state, str):
        raise McpInputRequiredContractError("MCP requestState must be an opaque string")
    if (
        request_state is not None
        and len(request_state.encode("utf-8"))
        > bounds.maximum_request_state_utf8_bytes
    ):
        raise McpInputRequiredContractError("MCP requestState exceeds its byte bound")
    requests = dict(input_requests or {})
    if not requests:
        if request_state is None:
            raise McpInputRequiredContractError(
                "MCP InputRequiredResult contains neither inputRequests nor requestState"
            )
        payload = {
            "leg_kind": "state_only",
            "request_state": request_state,
            "leg_ordinal": leg_ordinal,
            "retryable_payload_fingerprint": (
                retryable_payload.process_local_payload_fingerprint
            ),
            "operation_deadline_monotonic": operation_deadline_monotonic,
        }
        return (
            McpStateOnlyRetryLeg(
                **payload,
                leg_fingerprint=context_fingerprint(
                    "mcp-state-only-retry-leg:v1", payload
                ),
            ),
            (),
        )
    if not elicitation_advertised:
        raise McpUnadvertisedInputRequest(
            "MCP server requested client input that was not advertised"
        )
    if len(requests) > bounds.maximum_input_requests:
        raise McpInputRequiredContractError("MCP input request count exceeds bound")
    event_safe: list[McpElicitationRequestFact] = []
    private_urls: list[McpPrivateUrlElicitationPayload] = []
    for key in sorted(requests):
        raw = requests[key]
        method_value = raw.get("method")
        try:
            method = McpClientInputMethod(str(method_value))
        except ValueError as exc:
            raise McpInputRequiredContractError(
                f"unknown MCP input request method: {method_value!r}"
            ) from exc
        if method is not McpClientInputMethod.ELICITATION_CREATE:
            raise McpUnadvertisedInputRequest(
                f"MCP input method was not advertised: {method.value}"
            )
        params = raw.get("params")
        if not isinstance(params, Mapping):
            raise McpInputRequiredContractError(
                "MCP elicitation params must be an object"
            )
        request, private = _lower_elicitation_request(
            key=str(key),
            params=params,
            commitment_key_id=commitment_key_id,
            keyed_commitment=keyed_commitment,
            bounds=bounds,
        )
        event_safe.append(request)
        if private is not None:
            private_urls.append(private)
    request_tuple = tuple(event_safe)
    event_payload_bytes = canonical_json_bytes(
        tuple(item.model_dump(mode="json") for item in request_tuple)
    )
    if len(event_payload_bytes) > bounds.maximum_input_requests_event_bytes:
        raise McpInputRequiredContractError(
            "MCP event-safe input requests exceed their byte bound"
        )
    keys = tuple(item.key for item in request_tuple)
    request_set = context_fingerprint(
        "mcp-input-request-set:v1",
        tuple((item.key, item.request_fingerprint) for item in request_tuple),
    )
    payload = {
        "leg_kind": "client_input_required",
        "input_requests": request_tuple,
        "ordered_request_keys": keys,
        "request_set_fingerprint": request_set,
        "request_state": request_state,
        "leg_ordinal": leg_ordinal,
        "retryable_payload_fingerprint": (
            retryable_payload.process_local_payload_fingerprint
        ),
        "operation_deadline_monotonic": operation_deadline_monotonic,
    }
    return (
        McpClientInputRequiredLeg(
            **payload,
            leg_fingerprint=context_fingerprint(
                "mcp-client-input-required-leg:v1", payload
            ),
        ),
        tuple(private_urls),
    )


def state_only_retry_delay(leg_ordinal: int) -> float:
    if leg_ordinal < 1:
        raise ValueError("MCP retry leg ordinal must be positive")
    index = min(leg_ordinal - 1, len(MCP_STATE_ONLY_RETRY_DELAYS_SECONDS) - 1)
    return MCP_STATE_ONLY_RETRY_DELAYS_SECONDS[index]


def _lower_elicitation_request(
    *,
    key: str,
    params: Mapping[str, object],
    commitment_key_id: str,
    keyed_commitment: Callable[[str, bytes], str],
    bounds: McpContinuationBoundsFact,
) -> tuple[McpElicitationRequestFact, McpPrivateUrlElicitationPayload | None]:
    mode_was_omitted = "mode" not in params
    mode = params.get("mode", "form")
    message = params.get("message")
    if not isinstance(message, str):
        raise McpInputRequiredContractError("MCP elicitation message must be text")
    if mode == "form":
        raw_schema = params.get("requestedSchema")
        if not isinstance(raw_schema, Mapping):
            raise McpInputRequiredContractError(
                "MCP form elicitation requires requestedSchema"
            )
        schema = dict(raw_schema)
        _validate_restricted_form_schema(schema)
        frozen = freeze_json(schema)
        if not isinstance(frozen, FrozenJsonObjectFact):
            raise AssertionError("MCP form schema freezer returned a non-object")
        request = build_frozen_fact(
            McpFormElicitationRequestFact,
            schema_version="mcp_form_elicitation_request.v1",
            key=key,
            method=McpClientInputMethod.ELICITATION_CREATE,
            mode="form",
            wire_mode_was_omitted=mode_was_omitted,
            message=message,
            requested_schema=frozen,
            requested_schema_fingerprint=context_fingerprint(
                "mcp-elicitation-requested-schema:v1", frozen
            ),
        )
        return request, None
    if mode != "url":
        raise McpInputRequiredContractError(
            f"unsupported MCP elicitation mode: {mode!r}"
        )
    exact_url = params.get("url")
    if not isinstance(exact_url, str):
        raise McpElicitationUrlPolicyRejected("MCP URL elicitation requires a URL")
    if len(exact_url.encode("utf-8")) > bounds.maximum_private_url_utf8_bytes or any(
        ord(char) < 32 or ord(char) == 127 for char in exact_url
    ):
        raise McpElicitationUrlPolicyRejected("MCP elicitation URL exceeds policy")
    parsed = urlsplit(exact_url)
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
        raise McpElicitationUrlPolicyRejected(
            "MCP elicitation URL must be credential-free HTTPS"
        )
    try:
        ascii_host = parsed.hostname.encode("idna").decode("ascii")
        unicode_host = ascii_host.encode("ascii").decode("idna")
        port = parsed.port
    except (UnicodeError, ValueError) as exc:
        raise McpElicitationUrlPolicyRejected(
            "MCP elicitation URL host is invalid"
        ) from exc
    origin_host = ascii_host if ":" not in ascii_host else f"[{ascii_host}]"
    display_origin = f"https://{origin_host}" + (f":{port}" if port else "")
    commitment = keyed_commitment(
        "mcp-private-elicitation-url:v1", exact_url.encode("utf-8")
    )
    request = build_frozen_fact(
        McpUrlElicitationRequestFact,
        schema_version="mcp_url_elicitation_request.v1",
        key=key,
        method=McpClientInputMethod.ELICITATION_CREATE,
        mode="url",
        message=message,
        display_origin=display_origin,
        ascii_host=ascii_host,
        unicode_host=unicode_host,
        explicit_port=port,
        punycode_warning_required=(ascii_host.startswith("xn--") or ".xn--" in ascii_host),
        commitment_key_id=commitment_key_id,
        keyed_full_url_commitment=commitment,
        url_policy_fingerprint=MCP_URL_POLICY_FINGERPRINT,
    )
    return (
        request,
        build_private_url_elicitation_payload(
            request=request,
            exact_url=exact_url,
            url_policy_fingerprint=MCP_URL_POLICY_FINGERPRINT,
        ),
    )


def _validate_restricted_form_schema(schema: Mapping[str, object]) -> None:
    if schema.get("type") != "object":
        raise McpInputRequiredContractError(
            "MCP form elicitation schema root must be object"
        )
    encoded = canonical_json_bytes(schema)
    if len(encoded) > 64 * 1024:
        raise McpInputRequiredContractError("MCP form schema exceeds its byte bound")
    _reject_credential_schema(schema, path="$", depth=0)
    from jsonschema.validators import validator_for

    validator = validator_for(dict(schema))
    try:
        validator.check_schema(dict(schema))
    except Exception as exc:
        raise McpInputRequiredContractError(
            "MCP form elicitation schema is invalid"
        ) from exc


def _reject_credential_schema(
    value: object,
    *,
    path: str,
    depth: int,
) -> None:
    if depth > 32:
        raise McpInputRequiredContractError("MCP form schema nesting exceeds bound")
    if isinstance(value, Mapping):
        for key, item in value.items():
            key_text = str(key)
            if path.endswith("properties") and _CREDENTIAL_RE.search(key_text):
                raise McpInputRequiredContractError(
                    "MCP form elicitation cannot request credentials"
                )
            if key_text in {"title", "description"} and isinstance(item, str):
                if _CREDENTIAL_RE.search(item):
                    raise McpInputRequiredContractError(
                        "MCP form elicitation cannot request credentials"
                    )
            _reject_credential_schema(
                item,
                path=f"{path}.{key_text}",
                depth=depth + 1,
            )
    elif isinstance(value, (list, tuple)):
        for index, item in enumerate(value):
            _reject_credential_schema(
                item,
                path=f"{path}[{index}]",
                depth=depth + 1,
            )


__all__ = [name for name in globals() if name.startswith("Mcp")] + [
    "MCP_FORM_SCHEMA_POLICY_FINGERPRINT",
    "MCP_INPUT_REQUIRED_MAX_LEGS",
    "MCP_STATE_ONLY_RETRY_DELAYS_SECONDS",
    "MCP_URL_POLICY_FINGERPRINT",
    "lower_input_required_result",
    "state_only_retry_delay",
]
