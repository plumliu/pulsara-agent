"""Sealed process-local vocabulary for MCP continuation secrets.

These objects are intentionally neither Pydantic models nor dataclasses.  A
typed borrower capability is required to reveal values to the encryption
codec or to the SDK wire builder.
"""

from __future__ import annotations

import hmac
import json
import math
from dataclasses import fields, is_dataclass
from enum import StrEnum
from hashlib import sha256
from typing import Mapping, Sequence

from pydantic import BaseModel

from pulsara_agent.primitives._context_base import (
    canonical_json_bytes,
    context_fingerprint,
)
from pulsara_agent.primitives.mcp_continuation import (
    McpContinuationBoundsFact,
    McpFormElicitationRequestFact,
    McpUrlElicitationRequestFact,
)
from pulsara_agent.primitives.frozen import FrozenRuntimeStateBase
from pulsara_agent.primitives.storage_frozen import FrozenStorageFactBase


_CONSTRUCTION_TOKEN = object()
_BORROW_TOKEN = object()


class SealedMcpContinuationSecretBase:
    __slots__ = ()

    def __repr__(self) -> str:
        return "<sealed-mcp-continuation-secret>"

    __str__ = __repr__

    def __setattr__(self, name: str, value: object) -> None:
        del name, value
        raise TypeError("MCP continuation secrets are immutable")

    def __copy__(self):
        return self

    def __deepcopy__(self, memo):
        del memo
        return self

    def __reduce__(self):
        raise TypeError("MCP continuation secret cannot be pickled")

    def __reduce_ex__(self, protocol: int):
        del protocol
        raise TypeError("MCP continuation secret cannot be pickled")

    def __getstate__(self):
        raise TypeError("MCP continuation secret cannot be serialized")


class McpElicitationAction(StrEnum):
    ACCEPT = "accept"
    DECLINE = "decline"
    CANCEL = "cancel"


class McpSecretAccessPurpose(StrEnum):
    ENCRYPTION = "encryption"
    DECRYPTION_REBIND = "decryption_rebind"
    FRESH_WIRE_BUILD = "fresh_wire_build"
    HOST_FORM_RENDER = "host_form_render"
    HOST_FORM_SUBMIT = "host_form_submit"
    URL_DISPLAY = "url_display"
    URL_LAUNCH = "url_launch"


class SealedMcpJsonObject(SealedMcpContinuationSecretBase):
    __slots__ = ("_entries",)

    def __init__(
        self, entries: tuple[tuple[str, object], ...], *, _token: object
    ) -> None:
        if _token is not _CONSTRUCTION_TOKEN:
            raise TypeError("sealed MCP JSON must be built by its factory")
        object.__setattr__(self, "_entries", entries)


class McpFormElicitationResponse(SealedMcpContinuationSecretBase):
    __slots__ = (
        "_request_key",
        "_action",
        "_content_present",
        "_content",
        "_process_local_response_fingerprint",
    )

    def __init__(
        self,
        *,
        request_key: str,
        action: McpElicitationAction,
        content_present: bool,
        content: SealedMcpJsonObject | None,
        response_fingerprint: str,
        _token: object,
    ) -> None:
        if _token is not _CONSTRUCTION_TOKEN:
            raise TypeError("sealed MCP response must be built by its factory")
        object.__setattr__(self, "_request_key", request_key)
        object.__setattr__(self, "_action", action)
        object.__setattr__(self, "_content_present", content_present)
        object.__setattr__(self, "_content", content)
        object.__setattr__(
            self, "_process_local_response_fingerprint", response_fingerprint
        )

    @property
    def request_key(self) -> str:
        return self._request_key

    @property
    def action(self) -> McpElicitationAction:
        return self._action


class McpUrlElicitationResponse(SealedMcpContinuationSecretBase):
    __slots__ = ("_request_key", "_action", "_process_local_response_fingerprint")

    def __init__(
        self,
        *,
        request_key: str,
        action: McpElicitationAction,
        response_fingerprint: str,
        _token: object,
    ) -> None:
        if _token is not _CONSTRUCTION_TOKEN:
            raise TypeError("sealed MCP response must be built by its factory")
        object.__setattr__(self, "_request_key", request_key)
        object.__setattr__(self, "_action", action)
        object.__setattr__(
            self, "_process_local_response_fingerprint", response_fingerprint
        )

    @property
    def request_key(self) -> str:
        return self._request_key

    @property
    def action(self) -> McpElicitationAction:
        return self._action


McpElicitationResponse = McpFormElicitationResponse | McpUrlElicitationResponse


class McpFrozenRoundInputResponses(SealedMcpContinuationSecretBase):
    __slots__ = (
        "_response_schema_version",
        "_request_set_fingerprint",
        "_ordered_request_keys",
        "_ordered_process_local_response_fingerprints",
        "_wire_responses",
        "_process_local_response_set_fingerprint",
        "_commitment_key_id",
        "_keyed_current_round_responses_commitment",
        "_response_attribution_fingerprint",
    )

    def __init__(self, *, _token: object, **values: object) -> None:
        if _token is not _CONSTRUCTION_TOKEN:
            raise TypeError("sealed MCP response set must be built by its factory")
        for slot, value in values.items():
            object.__setattr__(self, f"_{slot}", value)

    @property
    def request_set_fingerprint(self) -> str:
        return self._request_set_fingerprint

    @property
    def ordered_request_keys(self) -> tuple[str, ...]:
        return self._ordered_request_keys

    @property
    def commitment_key_id(self) -> str:
        return self._commitment_key_id

    @property
    def keyed_current_round_responses_commitment(self) -> str:
        return self._keyed_current_round_responses_commitment

    @property
    def response_attribution_fingerprint(self) -> str:
        return self._response_attribution_fingerprint


class _McpRetryablePayloadBase(SealedMcpContinuationSecretBase):
    __slots__ = (
        "_payload_schema_version",
        "_payload_kind",
        "_source_method",
        "_source_method_schema_fingerprint",
        "_process_local_payload_fingerprint",
    )

    def _install_base(
        self,
        *,
        payload_kind: str,
        source_method: str,
        source_method_schema_fingerprint: str,
        process_local_payload_fingerprint: str,
        token: object,
    ) -> None:
        if token is not _CONSTRUCTION_TOKEN:
            raise TypeError("sealed MCP retry payload must be built by its factory")
        object.__setattr__(self, "_payload_schema_version", 1)
        object.__setattr__(self, "_payload_kind", payload_kind)
        object.__setattr__(self, "_source_method", source_method)
        object.__setattr__(
            self, "_source_method_schema_fingerprint", source_method_schema_fingerprint
        )
        object.__setattr__(
            self,
            "_process_local_payload_fingerprint",
            process_local_payload_fingerprint,
        )

    @property
    def payload_kind(self) -> str:
        return self._payload_kind

    @property
    def source_method(self) -> str:
        return self._source_method

    @property
    def source_method_schema_fingerprint(self) -> str:
        return self._source_method_schema_fingerprint

    @property
    def process_local_payload_fingerprint(self) -> str:
        return self._process_local_payload_fingerprint


class McpRetryableToolCallPayload(_McpRetryablePayloadBase):
    __slots__ = ("_tool_name", "_arguments")

    def __init__(
        self,
        *,
        tool_name: str,
        arguments: SealedMcpJsonObject,
        source_method_schema_fingerprint: str,
        process_local_payload_fingerprint: str,
        _token: object,
    ) -> None:
        self._install_base(
            payload_kind="tool_call",
            source_method="tools/call",
            source_method_schema_fingerprint=source_method_schema_fingerprint,
            process_local_payload_fingerprint=process_local_payload_fingerprint,
            token=_token,
        )
        object.__setattr__(self, "_tool_name", tool_name)
        object.__setattr__(self, "_arguments", arguments)


class McpRetryableResourceReadPayload(_McpRetryablePayloadBase):
    __slots__ = ("_uri",)

    def __init__(
        self,
        *,
        uri: str,
        source_method_schema_fingerprint: str,
        process_local_payload_fingerprint: str,
        _token: object,
    ) -> None:
        self._install_base(
            payload_kind="resource_read",
            source_method="resources/read",
            source_method_schema_fingerprint=source_method_schema_fingerprint,
            process_local_payload_fingerprint=process_local_payload_fingerprint,
            token=_token,
        )
        object.__setattr__(self, "_uri", uri)


class McpRetryablePromptGetPayload(_McpRetryablePayloadBase):
    __slots__ = ("_prompt_name", "_arguments")

    def __init__(
        self,
        *,
        prompt_name: str,
        arguments: SealedMcpJsonObject | None,
        source_method_schema_fingerprint: str,
        process_local_payload_fingerprint: str,
        _token: object,
    ) -> None:
        self._install_base(
            payload_kind="prompt_get",
            source_method="prompts/get",
            source_method_schema_fingerprint=source_method_schema_fingerprint,
            process_local_payload_fingerprint=process_local_payload_fingerprint,
            token=_token,
        )
        object.__setattr__(self, "_prompt_name", prompt_name)
        object.__setattr__(self, "_arguments", arguments)


McpRetryableRequestPayload = (
    McpRetryableToolCallPayload
    | McpRetryableResourceReadPayload
    | McpRetryablePromptGetPayload
)


class McpPrivateUrlElicitationPayload(SealedMcpContinuationSecretBase):
    __slots__ = (
        "_request_key",
        "_exact_url",
        "_url_policy_fingerprint",
        "_event_safe_request_fingerprint",
        "_process_local_private_payload_fingerprint",
    )

    def __init__(self, *, _token: object, **values: object) -> None:
        if _token is not _CONSTRUCTION_TOKEN:
            raise TypeError("private MCP URL payload must be built by its factory")
        for slot, value in values.items():
            object.__setattr__(self, f"_{slot}", value)

    @property
    def request_key(self) -> str:
        return self._request_key

    @property
    def process_local_private_payload_fingerprint(self) -> str:
        return self._process_local_private_payload_fingerprint


class _McpCarrierPlaintextBase(SealedMcpContinuationSecretBase):
    __slots__ = (
        "_carrier_schema_version",
        "_runtime_session_id",
        "_interaction_id",
        "_suspension_event_id",
        "_round_ordinal",
        "_retryable_request_payload",
        "_request_state",
        "_request_set_fingerprint",
        "_protocol_semantic_fingerprint",
        "_endpoint_attribution_fingerprint",
        "_auth_attribution_fingerprint",
        "_binding_contract_fingerprint",
        "_created_at_utc",
        "_operation_expires_at_utc",
        "_expiry_fingerprint",
    )

    def _install_common(self, *, token: object, **values: object) -> None:
        if token is not _CONSTRUCTION_TOKEN:
            raise TypeError("MCP carrier plaintext must be built by its factory")
        object.__setattr__(self, "_carrier_schema_version", 1)
        for slot, value in values.items():
            object.__setattr__(self, f"_{slot}", value)


class McpAwaitingInputCarrierPlaintext(_McpCarrierPlaintextBase):
    __slots__ = ("_private_url_requests",)

    def __init__(
        self,
        *,
        private_url_requests: tuple[McpPrivateUrlElicitationPayload, ...],
        _token: object,
        **values: object,
    ) -> None:
        self._install_common(token=_token, **values)
        object.__setattr__(self, "_private_url_requests", private_url_requests)


class McpReplayReadyCarrierPlaintext(_McpCarrierPlaintextBase):
    __slots__ = (
        "_resolution_event_id",
        "_current_round_input_responses",
        "_response_attribution_fingerprint",
    )

    def __init__(
        self,
        *,
        resolution_event_id: str,
        current_round_input_responses: McpFrozenRoundInputResponses,
        response_attribution_fingerprint: str,
        _token: object,
        **values: object,
    ) -> None:
        self._install_common(token=_token, **values)
        object.__setattr__(self, "_resolution_event_id", resolution_event_id)
        object.__setattr__(
            self, "_current_round_input_responses", current_round_input_responses
        )
        object.__setattr__(
            self, "_response_attribution_fingerprint", response_attribution_fingerprint
        )


McpContinuationCarrierPlaintext = (
    McpAwaitingInputCarrierPlaintext | McpReplayReadyCarrierPlaintext
)


class McpContinuationSecretBorrow:
    __slots__ = ("_issuer_id", "_generation", "_purpose", "_active", "_token")

    def __init__(
        self,
        *,
        issuer_id: str,
        generation: int,
        purpose: McpSecretAccessPurpose,
        _token: object,
    ) -> None:
        if _token is not _BORROW_TOKEN:
            raise TypeError("secret borrow must be issued by its owner")
        self._issuer_id = issuer_id
        self._generation = generation
        self._purpose = purpose
        self._active = True
        self._token = _token

    @property
    def purpose(self) -> McpSecretAccessPurpose:
        return self._purpose

    def revoke(self) -> None:
        self._active = False

    def _require(self, *purposes: McpSecretAccessPurpose) -> None:
        if not self._active or self._token is not _BORROW_TOKEN:
            raise RuntimeError("MCP continuation secret borrow is revoked")
        if self._purpose not in purposes:
            raise PermissionError("MCP continuation secret borrow purpose mismatch")

    def canonical_plaintext_bytes(
        self, value: McpContinuationCarrierPlaintext
    ) -> bytes:
        self._require(McpSecretAccessPurpose.ENCRYPTION)
        return canonical_json_bytes(_carrier_plaintext_value(value))

    def canonical_retry_payload_bytes(
        self,
        value: McpRetryableRequestPayload,
    ) -> bytes:
        self._require(McpSecretAccessPurpose.ENCRYPTION)
        return canonical_json_bytes(_retry_payload_value(value))

    def validate_physical_bounds(
        self,
        value: McpContinuationCarrierPlaintext,
        *,
        bounds: McpContinuationBoundsFact,
    ) -> None:
        self._require(
            McpSecretAccessPurpose.ENCRYPTION,
            McpSecretAccessPurpose.DECRYPTION_REBIND,
        )
        retry_bytes = canonical_json_bytes(
            _retry_payload_value(value._retryable_request_payload)
        )
        if len(retry_bytes) > bounds.maximum_retryable_base_params_bytes:
            raise ValueError("MCP retryable base params exceed their byte bound")
        request_state = value._request_state
        if (
            request_state is not None
            and len(request_state.encode("utf-8"))
            > bounds.maximum_request_state_utf8_bytes
        ):
            raise ValueError("MCP requestState exceeds its byte bound")
        if isinstance(value, McpAwaitingInputCarrierPlaintext):
            if len(value._private_url_requests) > bounds.maximum_input_requests:
                raise ValueError("MCP private URL request count exceeds bound")
            for private_url in value._private_url_requests:
                if (
                    len(private_url._exact_url.encode("utf-8"))
                    > bounds.maximum_private_url_utf8_bytes
                ):
                    raise ValueError("MCP private URL exceeds its byte bound")
        else:
            response_bytes = canonical_json_bytes(
                _sealed_json_thaw(value._current_round_input_responses._wire_responses)
            )
            if len(response_bytes) > bounds.maximum_current_round_response_bytes:
                raise ValueError("MCP current-round responses exceed their byte bound")

    def decode_plaintext_bytes(
        self,
        value: bytes,
    ) -> McpContinuationCarrierPlaintext:
        self._require(McpSecretAccessPurpose.DECRYPTION_REBIND)
        return _decode_carrier_plaintext(value)

    def wire_retry_parts(
        self,
        value: McpReplayReadyCarrierPlaintext,
    ) -> tuple[dict[str, object], str | None, dict[str, object]]:
        self._require(McpSecretAccessPurpose.FRESH_WIRE_BUILD)
        return (
            _retry_payload_value(value._retryable_request_payload),
            value._request_state,
            _sealed_json_thaw(value._current_round_input_responses._wire_responses),
        )

    def validate_carrier_authority(
        self,
        value: McpContinuationCarrierPlaintext,
        *,
        runtime_session_id: str,
        interaction_id: str,
        suspension_event_id: str,
        round_ordinal: int,
        operation_expires_at_utc: str,
        expiry_fingerprint: str,
    ) -> None:
        self._require(McpSecretAccessPurpose.DECRYPTION_REBIND)
        if (
            value._runtime_session_id != runtime_session_id
            or value._interaction_id != interaction_id
            or value._suspension_event_id != suspension_event_id
            or value._round_ordinal != round_ordinal
            or value._operation_expires_at_utc != operation_expires_at_utc
            or value._expiry_fingerprint != expiry_fingerprint
        ):
            raise ValueError("MCP continuation plaintext authority mismatch")

    def form_content(
        self, value: McpFormElicitationResponse
    ) -> dict[str, object] | None:
        self._require(
            McpSecretAccessPurpose.HOST_FORM_SUBMIT,
            McpSecretAccessPurpose.FRESH_WIRE_BUILD,
        )
        return _sealed_json_thaw(value._content) if value._content is not None else None

    def exact_private_url(self, value: McpPrivateUrlElicitationPayload) -> str:
        self._require(
            McpSecretAccessPurpose.URL_DISPLAY, McpSecretAccessPurpose.URL_LAUNCH
        )
        return value._exact_url


class McpContinuationSecretBorrowIssuer:
    __slots__ = ("_issuer_id", "_generation", "_closed")

    def __init__(self, issuer_id: str) -> None:
        if not issuer_id:
            raise ValueError("MCP secret borrow issuer identity is required")
        self._issuer_id = issuer_id
        self._generation = 0
        self._closed = False

    def issue(self, purpose: McpSecretAccessPurpose) -> McpContinuationSecretBorrow:
        if self._closed:
            raise RuntimeError("MCP secret borrow issuer is closed")
        self._generation += 1
        return McpContinuationSecretBorrow(
            issuer_id=self._issuer_id,
            generation=self._generation,
            purpose=purpose,
            _token=_BORROW_TOKEN,
        )

    def close(self) -> None:
        self._closed = True


class McpSealedElicitationResponseFactory:
    __slots__ = (
        "_commitment_key_id",
        "_commitment_key",
        "_maximum_current_round_response_bytes",
    )

    def __init__(
        self,
        *,
        commitment_key_id: str,
        commitment_key: bytes,
        bounds: McpContinuationBoundsFact,
    ) -> None:
        if not commitment_key_id or len(commitment_key) < 32:
            raise ValueError("MCP response commitment key is invalid")
        self._commitment_key_id = commitment_key_id
        self._commitment_key = bytes(commitment_key)
        self._maximum_current_round_response_bytes = (
            bounds.maximum_current_round_response_bytes
        )

    def form_response(
        self,
        request: McpFormElicitationRequestFact,
        *,
        action: McpElicitationAction,
        content_present: bool,
        content: Mapping[str, object] | None,
    ) -> McpFormElicitationResponse:
        if action is McpElicitationAction.ACCEPT:
            if not content_present or content is None:
                raise ValueError("accepted MCP form response requires content")
            _validate_form_content(request, content)
            sealed = seal_mcp_json_object(content)
        else:
            if content_present or content is not None:
                raise ValueError(
                    "declined/cancelled MCP form response cannot carry content"
                )
            sealed = None
        process_payload = {
            "request_key": request.key,
            "mode": "form",
            "action": action.value,
            "content_present": content_present,
            "content": dict(content) if content is not None else None,
        }
        return McpFormElicitationResponse(
            request_key=request.key,
            action=action,
            content_present=content_present,
            content=sealed,
            response_fingerprint=context_fingerprint(
                "mcp-form-response:process-local:v1", process_payload
            ),
            _token=_CONSTRUCTION_TOKEN,
        )

    def url_response(
        self,
        request: McpUrlElicitationRequestFact,
        *,
        action: McpElicitationAction,
    ) -> McpUrlElicitationResponse:
        return McpUrlElicitationResponse(
            request_key=request.key,
            action=action,
            response_fingerprint=context_fingerprint(
                "mcp-url-response:process-local:v1",
                {"request_key": request.key, "mode": "url", "action": action.value},
            ),
            _token=_CONSTRUCTION_TOKEN,
        )

    def freeze_round(
        self,
        *,
        request_set_fingerprint: str,
        ordered_request_keys: tuple[str, ...],
        responses: Sequence[McpElicitationResponse],
    ) -> McpFrozenRoundInputResponses:
        if (
            ordered_request_keys != tuple(sorted(set(ordered_request_keys)))
            or not ordered_request_keys
        ):
            raise ValueError("MCP response request keys must be ordered and unique")
        response_by_key = {item.request_key: item for item in responses}
        if set(response_by_key) != set(ordered_request_keys) or len(
            response_by_key
        ) != len(responses):
            raise ValueError("MCP response key set must exactly match request set")
        wire: dict[str, object] = {}
        local_fingerprints: list[str] = []
        for key in ordered_request_keys:
            response = response_by_key[key]
            wire[key] = _wire_response_value(response)
            local_fingerprints.append(response._process_local_response_fingerprint)
        canonical = canonical_json_bytes(wire)
        if len(canonical) > self._maximum_current_round_response_bytes:
            raise ValueError("MCP current-round responses exceed their byte bound")
        keyed_commitment = (
            "hmac-sha256:"
            + hmac.new(
                self._commitment_key,
                b"mcp-current-round-responses:v1\0" + canonical,
                sha256,
            ).hexdigest()
        )
        attribution = context_fingerprint(
            "mcp-response-attribution:v1",
            {
                "request_set_fingerprint": request_set_fingerprint,
                "ordered_response_keys": ordered_request_keys,
                "commitment_key_id": self._commitment_key_id,
                "keyed_current_round_responses_commitment": keyed_commitment,
            },
        )
        return McpFrozenRoundInputResponses(
            _token=_CONSTRUCTION_TOKEN,
            response_schema_version=1,
            request_set_fingerprint=request_set_fingerprint,
            ordered_request_keys=ordered_request_keys,
            ordered_process_local_response_fingerprints=tuple(local_fingerprints),
            wire_responses=seal_mcp_json_object(wire),
            process_local_response_set_fingerprint=context_fingerprint(
                "mcp-response-set:process-local:v1",
                {"request_set": request_set_fingerprint, "responses": wire},
            ),
            commitment_key_id=self._commitment_key_id,
            keyed_current_round_responses_commitment=keyed_commitment,
            response_attribution_fingerprint=attribution,
        )


def seal_mcp_json_object(value: Mapping[str, object]) -> SealedMcpJsonObject:
    entries = tuple(
        (str(key), _seal_json_value(item))
        for key, item in sorted(value.items(), key=lambda pair: str(pair[0]))
    )
    if len({key for key, _ in entries}) != len(entries):
        raise ValueError("sealed MCP JSON keys must be unique")
    return SealedMcpJsonObject(entries, _token=_CONSTRUCTION_TOKEN)


def build_retryable_tool_call_payload(
    *,
    tool_name: str,
    arguments: Mapping[str, object],
    source_method_schema_fingerprint: str,
) -> McpRetryableToolCallPayload:
    sealed = seal_mcp_json_object(arguments)
    payload = {"tool_name": tool_name, "arguments": dict(arguments)}
    return McpRetryableToolCallPayload(
        tool_name=tool_name,
        arguments=sealed,
        source_method_schema_fingerprint=source_method_schema_fingerprint,
        process_local_payload_fingerprint=context_fingerprint(
            "mcp-tool-call-payload:process-local:v1", payload
        ),
        _token=_CONSTRUCTION_TOKEN,
    )


def build_retryable_resource_read_payload(
    *, uri: str, source_method_schema_fingerprint: str
) -> McpRetryableResourceReadPayload:
    return McpRetryableResourceReadPayload(
        uri=uri,
        source_method_schema_fingerprint=source_method_schema_fingerprint,
        process_local_payload_fingerprint=context_fingerprint(
            "mcp-resource-read-payload:process-local:v1", {"uri": uri}
        ),
        _token=_CONSTRUCTION_TOKEN,
    )


def build_retryable_prompt_get_payload(
    *,
    prompt_name: str,
    arguments: Mapping[str, object] | None,
    source_method_schema_fingerprint: str,
) -> McpRetryablePromptGetPayload:
    sealed = seal_mcp_json_object(arguments) if arguments is not None else None
    return McpRetryablePromptGetPayload(
        prompt_name=prompt_name,
        arguments=sealed,
        source_method_schema_fingerprint=source_method_schema_fingerprint,
        process_local_payload_fingerprint=context_fingerprint(
            "mcp-prompt-get-payload:process-local:v1",
            {"prompt_name": prompt_name, "arguments": dict(arguments or {})},
        ),
        _token=_CONSTRUCTION_TOKEN,
    )


def build_private_url_elicitation_payload(
    *,
    request: McpUrlElicitationRequestFact,
    exact_url: str,
    url_policy_fingerprint: str,
) -> McpPrivateUrlElicitationPayload:
    if request.url_policy_fingerprint != url_policy_fingerprint:
        raise ValueError("MCP private URL policy identity mismatch")
    return McpPrivateUrlElicitationPayload(
        _token=_CONSTRUCTION_TOKEN,
        request_key=request.key,
        exact_url=exact_url,
        url_policy_fingerprint=url_policy_fingerprint,
        event_safe_request_fingerprint=request.request_fingerprint,
        process_local_private_payload_fingerprint=context_fingerprint(
            "mcp-private-url-payload:process-local:v1",
            {
                "request_key": request.key,
                "exact_url": exact_url,
                "url_policy_fingerprint": url_policy_fingerprint,
                "event_safe_request_fingerprint": request.request_fingerprint,
            },
        ),
    )


def build_awaiting_input_carrier_plaintext(
    *,
    runtime_session_id: str,
    interaction_id: str,
    suspension_event_id: str,
    round_ordinal: int,
    retryable_request_payload: McpRetryableRequestPayload,
    request_state: str | None,
    request_set_fingerprint: str,
    private_url_requests: tuple[McpPrivateUrlElicitationPayload, ...],
    protocol_semantic_fingerprint: str,
    endpoint_attribution_fingerprint: str,
    auth_attribution_fingerprint: str,
    binding_contract_fingerprint: str,
    created_at_utc: str,
    operation_expires_at_utc: str,
    expiry_fingerprint: str,
) -> McpAwaitingInputCarrierPlaintext:
    if round_ordinal < 1:
        raise ValueError("MCP continuation round must be positive")
    return McpAwaitingInputCarrierPlaintext(
        _token=_CONSTRUCTION_TOKEN,
        private_url_requests=private_url_requests,
        runtime_session_id=runtime_session_id,
        interaction_id=interaction_id,
        suspension_event_id=suspension_event_id,
        round_ordinal=round_ordinal,
        retryable_request_payload=retryable_request_payload,
        request_state=request_state,
        request_set_fingerprint=request_set_fingerprint,
        protocol_semantic_fingerprint=protocol_semantic_fingerprint,
        endpoint_attribution_fingerprint=endpoint_attribution_fingerprint,
        auth_attribution_fingerprint=auth_attribution_fingerprint,
        binding_contract_fingerprint=binding_contract_fingerprint,
        created_at_utc=created_at_utc,
        operation_expires_at_utc=operation_expires_at_utc,
        expiry_fingerprint=expiry_fingerprint,
    )


def build_replay_ready_carrier_plaintext(
    *,
    runtime_session_id: str,
    interaction_id: str,
    suspension_event_id: str,
    resolution_event_id: str,
    round_ordinal: int,
    retryable_request_payload: McpRetryableRequestPayload,
    request_state: str | None,
    current_round_input_responses: McpFrozenRoundInputResponses,
    request_set_fingerprint: str,
    protocol_semantic_fingerprint: str,
    endpoint_attribution_fingerprint: str,
    auth_attribution_fingerprint: str,
    binding_contract_fingerprint: str,
    created_at_utc: str,
    operation_expires_at_utc: str,
    expiry_fingerprint: str,
) -> McpReplayReadyCarrierPlaintext:
    if current_round_input_responses.request_set_fingerprint != request_set_fingerprint:
        raise ValueError("MCP replay response/request set mismatch")
    return McpReplayReadyCarrierPlaintext(
        _token=_CONSTRUCTION_TOKEN,
        resolution_event_id=resolution_event_id,
        current_round_input_responses=current_round_input_responses,
        response_attribution_fingerprint=(
            current_round_input_responses.response_attribution_fingerprint
        ),
        runtime_session_id=runtime_session_id,
        interaction_id=interaction_id,
        suspension_event_id=suspension_event_id,
        round_ordinal=round_ordinal,
        retryable_request_payload=retryable_request_payload,
        request_state=request_state,
        request_set_fingerprint=request_set_fingerprint,
        protocol_semantic_fingerprint=protocol_semantic_fingerprint,
        endpoint_attribution_fingerprint=endpoint_attribution_fingerprint,
        auth_attribution_fingerprint=auth_attribution_fingerprint,
        binding_contract_fingerprint=binding_contract_fingerprint,
        created_at_utc=created_at_utc,
        operation_expires_at_utc=operation_expires_at_utc,
        expiry_fingerprint=expiry_fingerprint,
    )


def _seal_json_value(value: object) -> object:
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("MCP secret JSON floats must be finite")
        return value
    if isinstance(value, Mapping):
        return seal_mcp_json_object(value)
    if isinstance(value, (list, tuple)):
        return tuple(_seal_json_value(item) for item in value)
    raise TypeError(f"unsupported MCP secret JSON value: {type(value).__name__}")


def _sealed_json_thaw(value: SealedMcpJsonObject | None) -> dict[str, object]:
    if value is None:
        return {}
    return {key: _thaw_secret_value(item) for key, item in value._entries}


def _thaw_secret_value(value: object) -> object:
    if isinstance(value, SealedMcpJsonObject):
        return _sealed_json_thaw(value)
    if isinstance(value, tuple):
        return [_thaw_secret_value(item) for item in value]
    return value


def _wire_response_value(response: McpElicitationResponse) -> dict[str, object]:
    payload: dict[str, object] = {"action": response.action.value}
    if isinstance(response, McpFormElicitationResponse) and response._content_present:
        payload["content"] = _sealed_json_thaw(response._content)
    return payload


def _retry_payload_value(payload: McpRetryableRequestPayload) -> dict[str, object]:
    if isinstance(payload, McpRetryableToolCallPayload):
        return {
            "kind": "tool_call",
            "source_method": payload._source_method,
            "source_method_schema_fingerprint": (
                payload._source_method_schema_fingerprint
            ),
            "tool_name": payload._tool_name,
            "arguments": _sealed_json_thaw(payload._arguments),
        }
    if isinstance(payload, McpRetryableResourceReadPayload):
        return {
            "kind": "resource_read",
            "source_method": payload._source_method,
            "source_method_schema_fingerprint": (
                payload._source_method_schema_fingerprint
            ),
            "uri": payload._uri,
        }
    return {
        "kind": "prompt_get",
        "source_method": payload._source_method,
        "source_method_schema_fingerprint": payload._source_method_schema_fingerprint,
        "prompt_name": payload._prompt_name,
        "arguments": _sealed_json_thaw(payload._arguments)
        if payload._arguments is not None
        else None,
    }


def _carrier_plaintext_value(
    value: McpContinuationCarrierPlaintext,
) -> dict[str, object]:
    payload: dict[str, object] = {
        "carrier_schema_version": value._carrier_schema_version,
        "runtime_session_id": value._runtime_session_id,
        "interaction_id": value._interaction_id,
        "suspension_event_id": value._suspension_event_id,
        "round_ordinal": value._round_ordinal,
        "retryable_request_payload": _retry_payload_value(
            value._retryable_request_payload
        ),
        "request_state": value._request_state,
        "request_set_fingerprint": value._request_set_fingerprint,
        "protocol_semantic_fingerprint": value._protocol_semantic_fingerprint,
        "endpoint_attribution_fingerprint": value._endpoint_attribution_fingerprint,
        "auth_attribution_fingerprint": value._auth_attribution_fingerprint,
        "binding_contract_fingerprint": value._binding_contract_fingerprint,
        "created_at_utc": value._created_at_utc,
        "operation_expires_at_utc": value._operation_expires_at_utc,
        "expiry_fingerprint": value._expiry_fingerprint,
    }
    if isinstance(value, McpAwaitingInputCarrierPlaintext):
        payload["carrier_kind"] = "awaiting_client_input"
        payload["private_url_requests"] = [
            {
                "request_key": item._request_key,
                "exact_url": item._exact_url,
                "url_policy_fingerprint": item._url_policy_fingerprint,
                "event_safe_request_fingerprint": item._event_safe_request_fingerprint,
            }
            for item in value._private_url_requests
        ]
    else:
        payload.update(
            {
                "carrier_kind": "replay_ready",
                "resolution_event_id": value._resolution_event_id,
                "current_round_input_responses": _sealed_json_thaw(
                    value._current_round_input_responses._wire_responses
                ),
                "ordered_response_keys": (
                    value._current_round_input_responses._ordered_request_keys
                ),
                "commitment_key_id": (
                    value._current_round_input_responses._commitment_key_id
                ),
                "keyed_current_round_responses_commitment": (
                    value._current_round_input_responses._keyed_current_round_responses_commitment
                ),
                "response_attribution_fingerprint": value._response_attribution_fingerprint,
            }
        )
    return payload


def _decode_carrier_plaintext(value: bytes) -> McpContinuationCarrierPlaintext:
    try:
        payload = json.loads(value.decode("utf-8"))
    except Exception as exc:
        raise ValueError("MCP continuation plaintext is not UTF-8 JSON") from exc
    if not isinstance(payload, dict):
        raise ValueError("MCP continuation plaintext must be an object")
    kind = payload.get("carrier_kind")
    common_keys = {
        "carrier_schema_version",
        "carrier_kind",
        "runtime_session_id",
        "interaction_id",
        "suspension_event_id",
        "round_ordinal",
        "retryable_request_payload",
        "request_state",
        "request_set_fingerprint",
        "protocol_semantic_fingerprint",
        "endpoint_attribution_fingerprint",
        "auth_attribution_fingerprint",
        "binding_contract_fingerprint",
        "created_at_utc",
        "operation_expires_at_utc",
        "expiry_fingerprint",
    }
    extra = (
        {"private_url_requests"}
        if kind == "awaiting_client_input"
        else {
            "resolution_event_id",
            "current_round_input_responses",
            "ordered_response_keys",
            "commitment_key_id",
            "keyed_current_round_responses_commitment",
            "response_attribution_fingerprint",
        }
        if kind == "replay_ready"
        else set()
    )
    if not extra or set(payload) != common_keys | extra:
        raise ValueError("MCP continuation plaintext field set is not closed")
    if payload["carrier_schema_version"] != 1:
        raise ValueError("unsupported MCP continuation plaintext version")
    retry_payload = _decode_retry_payload(payload["retryable_request_payload"])
    request_state = payload["request_state"]
    if request_state is not None and not isinstance(request_state, str):
        raise ValueError("MCP requestState must be an opaque string")
    common: dict[str, object] = {
        "runtime_session_id": _required_string(payload, "runtime_session_id"),
        "interaction_id": _required_string(payload, "interaction_id"),
        "suspension_event_id": _required_string(payload, "suspension_event_id"),
        "round_ordinal": _positive_integer(payload, "round_ordinal"),
        "retryable_request_payload": retry_payload,
        "request_state": request_state,
        "request_set_fingerprint": _required_string(payload, "request_set_fingerprint"),
        "protocol_semantic_fingerprint": _required_string(
            payload, "protocol_semantic_fingerprint"
        ),
        "endpoint_attribution_fingerprint": _required_string(
            payload, "endpoint_attribution_fingerprint"
        ),
        "auth_attribution_fingerprint": _required_string(
            payload, "auth_attribution_fingerprint"
        ),
        "binding_contract_fingerprint": _required_string(
            payload, "binding_contract_fingerprint"
        ),
        "created_at_utc": _required_string(payload, "created_at_utc"),
        "operation_expires_at_utc": _required_string(
            payload, "operation_expires_at_utc"
        ),
        "expiry_fingerprint": _required_string(payload, "expiry_fingerprint"),
    }
    if kind == "awaiting_client_input":
        private_items = payload["private_url_requests"]
        if not isinstance(private_items, list):
            raise ValueError("MCP private URL request set must be a list")
        private_urls = tuple(_decode_private_url(item) for item in private_items)
        return McpAwaitingInputCarrierPlaintext(
            _token=_CONSTRUCTION_TOKEN,
            private_url_requests=private_urls,
            **common,
        )
    raw_wire = payload["current_round_input_responses"]
    if not isinstance(raw_wire, dict):
        raise ValueError("MCP current-round responses must be an object")
    ordered_keys_value = payload["ordered_response_keys"]
    if not isinstance(ordered_keys_value, list) or any(
        not isinstance(item, str) for item in ordered_keys_value
    ):
        raise ValueError("MCP ordered response keys are malformed")
    ordered_keys = tuple(ordered_keys_value)
    if ordered_keys != tuple(sorted(raw_wire)):
        raise ValueError("MCP replay response key set is inconsistent")
    request_set = str(common["request_set_fingerprint"])
    key_id = _required_string(payload, "commitment_key_id")
    keyed_commitment = _required_string(
        payload, "keyed_current_round_responses_commitment"
    )
    attribution = _required_string(payload, "response_attribution_fingerprint")
    expected_attribution = context_fingerprint(
        "mcp-response-attribution:v1",
        {
            "request_set_fingerprint": request_set,
            "ordered_response_keys": ordered_keys,
            "commitment_key_id": key_id,
            "keyed_current_round_responses_commitment": keyed_commitment,
        },
    )
    if attribution != expected_attribution:
        raise ValueError("MCP replay response attribution mismatch")
    responses = McpFrozenRoundInputResponses(
        _token=_CONSTRUCTION_TOKEN,
        response_schema_version=1,
        request_set_fingerprint=request_set,
        ordered_request_keys=ordered_keys,
        ordered_process_local_response_fingerprints=(),
        wire_responses=seal_mcp_json_object(raw_wire),
        process_local_response_set_fingerprint=context_fingerprint(
            "mcp-response-set:process-local:v1",
            {"request_set": request_set, "responses": raw_wire},
        ),
        commitment_key_id=key_id,
        keyed_current_round_responses_commitment=keyed_commitment,
        response_attribution_fingerprint=attribution,
    )
    return McpReplayReadyCarrierPlaintext(
        _token=_CONSTRUCTION_TOKEN,
        resolution_event_id=_required_string(payload, "resolution_event_id"),
        current_round_input_responses=responses,
        response_attribution_fingerprint=attribution,
        **common,
    )


def _decode_retry_payload(value: object) -> McpRetryableRequestPayload:
    if not isinstance(value, dict):
        raise ValueError("MCP retry payload must be an object")
    kind = value.get("kind")
    schema_fingerprint = _required_string(value, "source_method_schema_fingerprint")
    if kind == "tool_call":
        if (
            set(value)
            != {
                "kind",
                "source_method",
                "source_method_schema_fingerprint",
                "tool_name",
                "arguments",
            }
            or value.get("source_method") != "tools/call"
        ):
            raise ValueError("MCP tool retry payload field set is invalid")
        arguments = value["arguments"]
        if not isinstance(arguments, dict):
            raise ValueError("MCP tool retry arguments must be an object")
        return build_retryable_tool_call_payload(
            tool_name=_required_string(value, "tool_name"),
            arguments=arguments,
            source_method_schema_fingerprint=schema_fingerprint,
        )
    if kind == "resource_read":
        if (
            set(value)
            != {
                "kind",
                "source_method",
                "source_method_schema_fingerprint",
                "uri",
            }
            or value.get("source_method") != "resources/read"
        ):
            raise ValueError("MCP resource retry payload field set is invalid")
        return build_retryable_resource_read_payload(
            uri=_required_string(value, "uri"),
            source_method_schema_fingerprint=schema_fingerprint,
        )
    if kind == "prompt_get":
        if (
            set(value)
            != {
                "kind",
                "source_method",
                "source_method_schema_fingerprint",
                "prompt_name",
                "arguments",
            }
            or value.get("source_method") != "prompts/get"
        ):
            raise ValueError("MCP prompt retry payload field set is invalid")
        arguments = value["arguments"]
        if arguments is not None and not isinstance(arguments, dict):
            raise ValueError("MCP prompt retry arguments must be an object")
        return build_retryable_prompt_get_payload(
            prompt_name=_required_string(value, "prompt_name"),
            arguments=arguments,
            source_method_schema_fingerprint=schema_fingerprint,
        )
    raise ValueError("unsupported MCP retry payload kind")


def _decode_private_url(value: object) -> McpPrivateUrlElicitationPayload:
    expected = {
        "request_key",
        "exact_url",
        "url_policy_fingerprint",
        "event_safe_request_fingerprint",
    }
    if not isinstance(value, dict) or set(value) != expected:
        raise ValueError("MCP private URL payload field set is invalid")
    return McpPrivateUrlElicitationPayload(
        _token=_CONSTRUCTION_TOKEN,
        request_key=_required_string(value, "request_key"),
        exact_url=_required_string(value, "exact_url"),
        url_policy_fingerprint=_required_string(value, "url_policy_fingerprint"),
        event_safe_request_fingerprint=_required_string(
            value, "event_safe_request_fingerprint"
        ),
        process_local_private_payload_fingerprint=context_fingerprint(
            "mcp-private-url-payload:process-local:v1",
            value,
        ),
    )


def _required_string(value: Mapping[str, object], key: str) -> str:
    item = value.get(key)
    if not isinstance(item, str) or not item:
        raise ValueError(f"MCP continuation field {key!r} must be a string")
    return item


def _positive_integer(value: Mapping[str, object], key: str) -> int:
    item = value.get(key)
    if not isinstance(item, int) or isinstance(item, bool) or item < 1:
        raise ValueError(f"MCP continuation field {key!r} must be positive")
    return item


def _validate_form_content(
    request: McpFormElicitationRequestFact, content: Mapping[str, object]
) -> None:
    from jsonschema.validators import validator_for
    from pulsara_agent.primitives._context_base import thaw_json

    schema = thaw_json(request.requested_schema)  # type: ignore[arg-type]
    validator_cls = validator_for(schema)
    validator_cls.check_schema(schema)
    errors = tuple(validator_cls(schema).iter_errors(dict(content)))
    if errors:
        raise ValueError(
            f"MCP elicitation response does not satisfy request schema: {errors[0].message}"
        )


def assert_not_mcp_secret(value: object, *, sink: str) -> None:
    """Reject non-authority runtime and storage values before serialization."""

    visited: set[int] = set()

    def inspect(item: object) -> None:
        if isinstance(item, SealedMcpContinuationSecretBase):
            raise TypeError(f"{sink} rejects MCP continuation secrets")
        if isinstance(item, FrozenStorageFactBase):
            raise TypeError(f"{sink} rejects MCP storage-only facts")
        if isinstance(item, FrozenRuntimeStateBase):
            raise TypeError(f"{sink} rejects process-local runtime state")
        if item is None or isinstance(item, (str, bytes, int, float, bool)):
            return
        identity = id(item)
        if identity in visited:
            return
        visited.add(identity)
        if isinstance(item, BaseModel):
            for field_name in type(item).model_fields:
                inspect(getattr(item, field_name))
            return
        if isinstance(item, Mapping):
            for key, nested in item.items():
                inspect(key)
                inspect(nested)
            return
        if isinstance(item, (list, tuple, set, frozenset)):
            for nested in item:
                inspect(nested)
            return
        if is_dataclass(item) and not isinstance(item, type):
            for field in fields(item):
                inspect(getattr(item, field.name))

    inspect(value)


__all__ = [
    "McpAwaitingInputCarrierPlaintext",
    "McpContinuationCarrierPlaintext",
    "McpContinuationSecretBorrow",
    "McpContinuationSecretBorrowIssuer",
    "McpElicitationAction",
    "McpElicitationResponse",
    "McpFormElicitationResponse",
    "McpFrozenRoundInputResponses",
    "McpPrivateUrlElicitationPayload",
    "McpReplayReadyCarrierPlaintext",
    "McpRetryablePromptGetPayload",
    "McpRetryableRequestPayload",
    "McpRetryableResourceReadPayload",
    "McpRetryableToolCallPayload",
    "McpSealedElicitationResponseFactory",
    "McpSecretAccessPurpose",
    "McpUrlElicitationResponse",
    "SealedMcpContinuationSecretBase",
    "SealedMcpJsonObject",
    "assert_not_mcp_secret",
    "build_awaiting_input_carrier_plaintext",
    "build_private_url_elicitation_payload",
    "build_replay_ready_carrier_plaintext",
    "build_retryable_prompt_get_payload",
    "build_retryable_resource_read_payload",
    "build_retryable_tool_call_payload",
    "seal_mcp_json_object",
]
