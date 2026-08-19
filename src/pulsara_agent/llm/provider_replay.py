"""Closed provider-native assistant replay contracts.

The values in this module are provider-neutral and transport-object free.  They
describe only completed Chat Completions or Responses carriers that can be
replayed to the same compatible wire target.  Private carrier bodies are
intentionally excluded from ``repr``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from hashlib import sha256

from pulsara_agent.primitives.context import (
    FrozenJsonObjectFact,
    canonical_json_bytes,
    context_fingerprint,
    thaw_json,
)
from pulsara_agent.primitives.bounded_json import bounded_json_loads


MAXIMUM_PROVIDER_REPLAY_PAYLOAD_BYTES = 16 << 20
MAXIMUM_PROVIDER_REPLAY_RESPONSES_ITEMS = 4_096
MAXIMUM_PROVIDER_REPLAY_CHAT_NESTED_ITEMS = 65_536
MAXIMUM_PROVIDER_REPLAY_JSON_NODES = 65_536
MAXIMUM_PROVIDER_REPLAY_JSON_DEPTH = 128
MAXIMUM_PROVIDER_REPLAY_STRING_UTF8_BYTES = 16 << 20
MAXIMUM_PROVIDER_DISPATCH_COMPOSITE_BYTES = 64 << 20

PROVIDER_REPLAY_COMPATIBILITY_CONTRACT_VERSION = (
    "pulsara.provider-replay-target-compatibility.v1"
)


class ProviderReplayDisposition(StrEnum):
    PUBLIC_SEMANTIC_ONLY = "PUBLIC_SEMANTIC_ONLY"
    NATIVE_REPLAY = "NATIVE_REPLAY"


class ProviderAssistantReplayCodecKind(StrEnum):
    NONE = "NONE"
    CHAT_CLOSED_REASONING_FIELDS = "CHAT_CLOSED_REASONING_FIELDS"
    RESPONSES_EXACT_OUTPUT_ITEMS = "RESPONSES_EXACT_OUTPUT_ITEMS"


def provider_replay_codec_for_wire_api(
    wire_api: str,
) -> ProviderAssistantReplayCodecKind:
    if wire_api == "openai_chat_completions":
        return ProviderAssistantReplayCodecKind.CHAT_CLOSED_REASONING_FIELDS
    if wire_api == "openai_responses":
        return ProviderAssistantReplayCodecKind.RESPONSES_EXACT_OUTPUT_ITEMS
    raise ValueError("provider replay wire API is unsupported")


def provider_replay_contract_fingerprint(
    codec_kind: ProviderAssistantReplayCodecKind,
) -> str:
    if codec_kind is ProviderAssistantReplayCodecKind.CHAT_CLOSED_REASONING_FIELDS:
        contract: object = {
            "wire_api": "openai_chat_completions",
            "codec": codec_kind.value,
            "fields": (
                ("reasoning_content", "TEXT_CONCAT"),
                ("reasoning", "TEXT_CONCAT"),
                ("reasoning_details", "ORDERED_ARRAY_APPEND"),
            ),
            "top_level": "exact_single_assistant_message",
            "public_projection": "ordered_text_then_tool_calls:v2",
            "canonical_array": "pulsara.canonical-json.v1",
        }
    elif codec_kind is ProviderAssistantReplayCodecKind.RESPONSES_EXACT_OUTPUT_ITEMS:
        contract = {
            "wire_api": "openai_responses",
            "codec": codec_kind.value,
            "items": ("reasoning", "message", "function_call"),
            "message_content": ("output_text", "text"),
            "public_order": "optional_single_message_before_function_calls:v1",
            "canonical_array": "pulsara.canonical-json.v1",
        }
    else:
        raise ValueError("provider replay codec is unsupported")
    return context_fingerprint("pulsara.provider-replay-contract:v1", contract)


@dataclass(frozen=True, slots=True)
class ProviderReplayTargetCompatibilityFact:
    wire_api: str
    endpoint_identity_fingerprint: str
    normalized_model_identity_fingerprint: str
    transport_binding_id: str
    codec_kind: ProviderAssistantReplayCodecKind
    provider_replay_contract_fingerprint: str
    compatibility_contract_version: str
    replay_target_fingerprint: str

    def __post_init__(self) -> None:
        if (
            self.codec_kind is not provider_replay_codec_for_wire_api(self.wire_api)
            or not self.transport_binding_id
            or self.compatibility_contract_version
            != PROVIDER_REPLAY_COMPATIBILITY_CONTRACT_VERSION
        ):
            raise ValueError("provider replay target union is invalid")
        for value in (
            self.endpoint_identity_fingerprint,
            self.normalized_model_identity_fingerprint,
            self.provider_replay_contract_fingerprint,
            self.replay_target_fingerprint,
        ):
            if not value.startswith("sha256:"):
                raise ValueError("provider replay target fingerprint is invalid")
        if self.provider_replay_contract_fingerprint != (
            provider_replay_contract_fingerprint(self.codec_kind)
        ):
            raise ValueError("provider replay contract fingerprint drifted")
        if self.replay_target_fingerprint != _replay_target_fingerprint(
            wire_api=self.wire_api,
            endpoint_identity_fingerprint=self.endpoint_identity_fingerprint,
            normalized_model_identity_fingerprint=(
                self.normalized_model_identity_fingerprint
            ),
            transport_binding_id=self.transport_binding_id,
            codec_kind=self.codec_kind,
            provider_replay_contract_fingerprint=(
                self.provider_replay_contract_fingerprint
            ),
        ):
            raise ValueError("provider replay target fingerprint mismatch")


def build_provider_replay_target_compatibility(
    *,
    wire_api: str,
    endpoint_identity_fingerprint: str,
    normalized_model_identifier: str,
    transport_binding_id: str,
) -> ProviderReplayTargetCompatibilityFact:
    if not normalized_model_identifier:
        raise ValueError("provider replay model identity is empty")
    codec = provider_replay_codec_for_wire_api(wire_api)
    model_fingerprint = context_fingerprint(
        "pulsara.provider-replay-normalized-model-identity:v1",
        normalized_model_identifier,
    )
    contract_fingerprint = provider_replay_contract_fingerprint(codec)
    target_fingerprint = _replay_target_fingerprint(
        wire_api=wire_api,
        endpoint_identity_fingerprint=endpoint_identity_fingerprint,
        normalized_model_identity_fingerprint=model_fingerprint,
        transport_binding_id=transport_binding_id,
        codec_kind=codec,
        provider_replay_contract_fingerprint=contract_fingerprint,
    )
    return ProviderReplayTargetCompatibilityFact(
        wire_api=wire_api,
        endpoint_identity_fingerprint=endpoint_identity_fingerprint,
        normalized_model_identity_fingerprint=model_fingerprint,
        transport_binding_id=transport_binding_id,
        codec_kind=codec,
        provider_replay_contract_fingerprint=contract_fingerprint,
        compatibility_contract_version=(
            PROVIDER_REPLAY_COMPATIBILITY_CONTRACT_VERSION
        ),
        replay_target_fingerprint=target_fingerprint,
    )


def _replay_target_fingerprint(
    *,
    wire_api: str,
    endpoint_identity_fingerprint: str,
    normalized_model_identity_fingerprint: str,
    transport_binding_id: str,
    codec_kind: ProviderAssistantReplayCodecKind,
    provider_replay_contract_fingerprint: str,
) -> str:
    return context_fingerprint(
        "pulsara.provider-replay-target:v1",
        {
            "wire_api": wire_api,
            "endpoint": endpoint_identity_fingerprint,
            "model": normalized_model_identity_fingerprint,
            "transport_binding": transport_binding_id,
            "codec": codec_kind.value,
            "replay_contract": provider_replay_contract_fingerprint,
            "compatibility_contract": (
                PROVIDER_REPLAY_COMPATIBILITY_CONTRACT_VERSION
            ),
        },
    )


def provider_replay_payload_bytes(
    ordered_items: tuple[FrozenJsonObjectFact, ...],
) -> bytes:
    return canonical_json_bytes(tuple(thaw_json(item) for item in ordered_items))


def provider_replay_payload_digest(payload_bytes: bytes) -> str:
    return "sha256:" + sha256(payload_bytes).hexdigest()


def provider_replay_id(*, session_id: str, assistant_entry_id: str, wire_api: str) -> str:
    digest = context_fingerprint(
        "pulsara.provider-assistant-replay-id:v1",
        {
            "session": session_id,
            "entry": assistant_entry_id,
            "wire_api": wire_api,
        },
    )
    return "provider-replay:" + digest.removeprefix("sha256:")


@dataclass(frozen=True, slots=True)
class ProviderAssistantReplayFragment:
    """Validated private body used by one exact wire-plan replacement."""

    codec_kind: ProviderAssistantReplayCodecKind
    provider_replay_contract_fingerprint: str
    replay_target_fingerprint: str
    assistant_entry_id: str
    public_projection_fingerprint: str
    ordered_items: tuple[FrozenJsonObjectFact, ...] = field(repr=False)
    payload_bytes: bytes = field(repr=False)
    payload_digest: str
    payload_size: int
    item_count: int
    fragment_fingerprint: str

    @property
    def logical_utf8_bytes(self) -> int:
        return self.payload_size

    def __post_init__(self) -> None:
        _validate_fragment_fields(
            codec_kind=self.codec_kind,
            provider_replay_contract_fingerprint=(
                self.provider_replay_contract_fingerprint
            ),
            replay_target_fingerprint=self.replay_target_fingerprint,
            assistant_entry_id=self.assistant_entry_id,
            public_projection_fingerprint=self.public_projection_fingerprint,
            ordered_items=self.ordered_items,
            payload_bytes=self.payload_bytes,
            payload_digest=self.payload_digest,
            payload_size=self.payload_size,
            item_count=self.item_count,
            fragment_fingerprint=self.fragment_fingerprint,
        )


@dataclass(frozen=True, slots=True)
class PreparedDurableProviderAssistantReplay:
    replay_id: str
    session_id: str
    workspace_id: str
    assistant_entry_id: str
    wire_api: str
    codec_kind: ProviderAssistantReplayCodecKind
    provider_replay_contract_fingerprint: str
    replay_target_fingerprint: str
    public_projection_fingerprint: str
    ordered_items: tuple[FrozenJsonObjectFact, ...] = field(repr=False)
    payload_bytes: bytes = field(repr=False)
    payload_digest: str
    payload_size: int
    item_count: int
    fragment_fingerprint: str

    def __post_init__(self) -> None:
        if (
            not self.session_id
            or not self.workspace_id
            or self.codec_kind is not provider_replay_codec_for_wire_api(self.wire_api)
            or self.replay_id
            != provider_replay_id(
                session_id=self.session_id,
                assistant_entry_id=self.assistant_entry_id,
                wire_api=self.wire_api,
            )
        ):
            raise ValueError("durable provider replay identity is invalid")
        _validate_fragment_fields(
            codec_kind=self.codec_kind,
            provider_replay_contract_fingerprint=(
                self.provider_replay_contract_fingerprint
            ),
            replay_target_fingerprint=self.replay_target_fingerprint,
            assistant_entry_id=self.assistant_entry_id,
            public_projection_fingerprint=self.public_projection_fingerprint,
            ordered_items=self.ordered_items,
            payload_bytes=self.payload_bytes,
            payload_digest=self.payload_digest,
            payload_size=self.payload_size,
            item_count=self.item_count,
            fragment_fingerprint=self.fragment_fingerprint,
        )

    def fragment(self) -> ProviderAssistantReplayFragment:
        return ProviderAssistantReplayFragment(
            codec_kind=self.codec_kind,
            provider_replay_contract_fingerprint=(
                self.provider_replay_contract_fingerprint
            ),
            replay_target_fingerprint=self.replay_target_fingerprint,
            assistant_entry_id=self.assistant_entry_id,
            public_projection_fingerprint=self.public_projection_fingerprint,
            ordered_items=self.ordered_items,
            payload_bytes=self.payload_bytes,
            payload_digest=self.payload_digest,
            payload_size=self.payload_size,
            item_count=self.item_count,
            fragment_fingerprint=self.fragment_fingerprint,
        )


def build_prepared_durable_provider_assistant_replay(
    *,
    session_id: str,
    workspace_id: str,
    assistant_entry_id: str,
    target: ProviderReplayTargetCompatibilityFact,
    public_projection_fingerprint: str,
    ordered_items: tuple[FrozenJsonObjectFact, ...],
) -> PreparedDurableProviderAssistantReplay:
    payload = provider_replay_payload_bytes(ordered_items)
    digest = provider_replay_payload_digest(payload)
    fragment_fingerprint = context_fingerprint(
        "pulsara.provider-assistant-replay-fragment:v2-durable",
        {
            "codec": target.codec_kind.value,
            "replay_contract": target.provider_replay_contract_fingerprint,
            "replay_target": target.replay_target_fingerprint,
            "entry": assistant_entry_id,
            "public": public_projection_fingerprint,
            "payload_digest": digest,
            "payload_size": len(payload),
            "item_count": len(ordered_items),
        },
    )
    return PreparedDurableProviderAssistantReplay(
        replay_id=provider_replay_id(
            session_id=session_id,
            assistant_entry_id=assistant_entry_id,
            wire_api=target.wire_api,
        ),
        session_id=session_id,
        workspace_id=workspace_id,
        assistant_entry_id=assistant_entry_id,
        wire_api=target.wire_api,
        codec_kind=target.codec_kind,
        provider_replay_contract_fingerprint=(
            target.provider_replay_contract_fingerprint
        ),
        replay_target_fingerprint=target.replay_target_fingerprint,
        public_projection_fingerprint=public_projection_fingerprint,
        ordered_items=ordered_items,
        payload_bytes=payload,
        payload_digest=digest,
        payload_size=len(payload),
        item_count=len(ordered_items),
        fragment_fingerprint=fragment_fingerprint,
    )


def _validate_fragment_fields(
    *,
    codec_kind: ProviderAssistantReplayCodecKind,
    provider_replay_contract_fingerprint: str,
    replay_target_fingerprint: str,
    assistant_entry_id: str,
    public_projection_fingerprint: str,
    ordered_items: tuple[FrozenJsonObjectFact, ...],
    payload_bytes: bytes,
    payload_digest: str,
    payload_size: int,
    item_count: int,
    fragment_fingerprint: str,
) -> None:
    if codec_kind is ProviderAssistantReplayCodecKind.NONE or not assistant_entry_id:
        raise ValueError("provider replay fragment identity is invalid")
    if codec_kind is ProviderAssistantReplayCodecKind.CHAT_CLOSED_REASONING_FIELDS:
        valid_count = item_count == 1
    else:
        valid_count = 1 <= item_count <= MAXIMUM_PROVIDER_REPLAY_RESPONSES_ITEMS
    expected_payload = provider_replay_payload_bytes(ordered_items)
    if (
        not valid_count
        or item_count != len(ordered_items)
        or payload_size != len(payload_bytes)
        or payload_size < 2
        or payload_size > MAXIMUM_PROVIDER_REPLAY_PAYLOAD_BYTES
        or payload_bytes != expected_payload
        or payload_digest != provider_replay_payload_digest(payload_bytes)
        or provider_replay_contract_fingerprint
        != provider_replay_contract_fingerprint_for_codec(codec_kind)
    ):
        raise ValueError("provider replay payload is invalid")
    decoded = bounded_json_loads(
        payload_bytes,
        maximum_bytes=MAXIMUM_PROVIDER_REPLAY_PAYLOAD_BYTES,
        maximum_nodes=MAXIMUM_PROVIDER_REPLAY_JSON_NODES,
        maximum_depth=MAXIMUM_PROVIDER_REPLAY_JSON_DEPTH,
        maximum_string_utf8_bytes=MAXIMUM_PROVIDER_REPLAY_STRING_UTF8_BYTES,
    )
    _validate_provider_replay_payload_shape(codec_kind, decoded)
    for value in (
        provider_replay_contract_fingerprint,
        replay_target_fingerprint,
        public_projection_fingerprint,
        fragment_fingerprint,
    ):
        if not value.startswith("sha256:"):
            raise ValueError("provider replay fingerprint is invalid")
    expected_fragment = context_fingerprint(
        "pulsara.provider-assistant-replay-fragment:v2-durable",
        {
            "codec": codec_kind.value,
            "replay_contract": provider_replay_contract_fingerprint,
            "replay_target": replay_target_fingerprint,
            "entry": assistant_entry_id,
            "public": public_projection_fingerprint,
            "payload_digest": payload_digest,
            "payload_size": payload_size,
            "item_count": item_count,
        },
    )
    if fragment_fingerprint != expected_fragment:
        raise ValueError("provider replay fragment fingerprint mismatch")


def _validate_provider_replay_payload_shape(
    codec_kind: ProviderAssistantReplayCodecKind,
    decoded: object,
) -> None:
    if not isinstance(decoded, list):
        raise ValueError("provider replay payload is not an array")
    if codec_kind is ProviderAssistantReplayCodecKind.CHAT_CLOSED_REASONING_FIELDS:
        if len(decoded) != 1 or not isinstance(decoded[0], dict):
            raise ValueError("Chat provider replay shape is invalid")
        message = decoded[0]
        allowed = {
            "role",
            "content",
            "tool_calls",
            "reasoning_content",
            "reasoning",
            "reasoning_details",
        }
        if (
            message.get("role") != "assistant"
            or set(message).difference(allowed)
            or not {
                "reasoning_content",
                "reasoning",
                "reasoning_details",
            }.intersection(message)
        ):
            raise ValueError("Chat provider replay registry is invalid")
        details = message.get("reasoning_details")
        if details is not None and (
            not isinstance(details, list)
            or len(details) > MAXIMUM_PROVIDER_REPLAY_CHAT_NESTED_ITEMS
        ):
            raise ValueError("Chat provider replay nested item bound exceeded")
        return
    if codec_kind is ProviderAssistantReplayCodecKind.RESPONSES_EXACT_OUTPUT_ITEMS:
        if not decoded or len(decoded) > MAXIMUM_PROVIDER_REPLAY_RESPONSES_ITEMS:
            raise ValueError("Responses provider replay item count is invalid")
        if any(
            not isinstance(item, dict)
            or item.get("type") not in {"reasoning", "message", "function_call"}
            for item in decoded
        ):
            raise ValueError("Responses provider replay item type is invalid")
        return
    raise ValueError("provider replay codec is unsupported")


def provider_replay_contract_fingerprint_for_codec(
    codec_kind: ProviderAssistantReplayCodecKind,
) -> str:
    return provider_replay_contract_fingerprint(codec_kind)


__all__ = [
    "MAXIMUM_PROVIDER_DISPATCH_COMPOSITE_BYTES",
    "MAXIMUM_PROVIDER_REPLAY_CHAT_NESTED_ITEMS",
    "MAXIMUM_PROVIDER_REPLAY_JSON_DEPTH",
    "MAXIMUM_PROVIDER_REPLAY_JSON_NODES",
    "MAXIMUM_PROVIDER_REPLAY_PAYLOAD_BYTES",
    "MAXIMUM_PROVIDER_REPLAY_RESPONSES_ITEMS",
    "MAXIMUM_PROVIDER_REPLAY_STRING_UTF8_BYTES",
    "PROVIDER_REPLAY_COMPATIBILITY_CONTRACT_VERSION",
    "PreparedDurableProviderAssistantReplay",
    "ProviderAssistantReplayFragment",
    "ProviderAssistantReplayCodecKind",
    "ProviderReplayDisposition",
    "ProviderReplayTargetCompatibilityFact",
    "build_prepared_durable_provider_assistant_replay",
    "build_provider_replay_target_compatibility",
    "provider_replay_codec_for_wire_api",
    "provider_replay_contract_fingerprint",
    "provider_replay_id",
    "provider_replay_payload_bytes",
    "provider_replay_payload_digest",
]
