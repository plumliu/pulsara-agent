"""Provider-neutral LLM input objects."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from pulsara_agent.llm.user_carrier import (
    EncodedProviderUserCarrier,
    ProviderUserCarrierSemanticFact,
    encode_human_input,
    encode_runtime_observation,
    encode_runtime_request,
    canonical_runtime_observation_wire_from_semantic,
    decode_runtime_observation_wire_semantic,
    provider_user_carrier_binding,
    rebind_provider_user_carrier_semantic,
)
from pulsara_agent.primitives._context_base import context_fingerprint
from pulsara_agent.primitives.runtime_observation import (
    HumanInputWireSemanticFact,
    RuntimeObservationWireSemanticFact,
    RuntimeObservationPayloadFact,
    RuntimeRequestWireSemanticFact,
    RuntimeRequestKind,
)
from pulsara_agent.primitives.provider_input import ProviderUserCarrierBindingFact


class MessageRole(StrEnum):
    SYSTEM = "system"
    USER = "user"
    ASSISTANT = "assistant"
    TOOL_CALL = "tool_call"
    TOOL_RESULT = "tool_result"
    RUNTIME_REQUEST = "runtime_request"
    RUNTIME_OBSERVATION = "runtime_observation"


@dataclass(frozen=True, slots=True)
class ToolSpec:
    name: str
    description: str
    parameters: dict[str, Any]


@dataclass(frozen=True, slots=True)
class LLMToolCall:
    id: str
    name: str
    arguments: str = "{}"


@dataclass(frozen=True, slots=True)
class LLMMessage:
    """Provider-neutral input item sent to a model provider.

    Runtime messages are rebuilt from AgentEvent streams in ``pulsara_agent.message``.
    Text messages use ``content``. Tool transcripts use ``tool_call_id``,
    ``name``, and ``arguments`` so each provider adapter can emit its native
    tool-call and tool-result wire format.
    """

    role: MessageRole
    content: tuple[str, ...] = field(default_factory=tuple)
    thinking: tuple[str, ...] = field(default_factory=tuple)
    tool_calls: tuple[LLMToolCall, ...] = field(default_factory=tuple)
    tool_call_id: str | None = None
    name: str | None = None
    arguments: str | None = None
    provider_user_carrier_semantic: ProviderUserCarrierSemanticFact | None = None
    provider_user_carrier_binding: ProviderUserCarrierBindingFact | None = None

    def __post_init__(self) -> None:
        expected = {
            MessageRole.USER: HumanInputWireSemanticFact,
            MessageRole.RUNTIME_REQUEST: RuntimeRequestWireSemanticFact,
            MessageRole.RUNTIME_OBSERVATION: RuntimeObservationWireSemanticFact,
        }.get(self.role)
        semantic = self.provider_user_carrier_semantic
        binding = self.provider_user_carrier_binding
        if expected is None:
            if semantic is not None or binding is not None:
                raise ValueError("non-user provider message cannot carry user authority")
            return
        if (
            not isinstance(semantic, expected)
            or binding is None
            or len(self.content) != 1
        ):
            raise ValueError("typed provider-user message lacks exact semantic owner")
        if rebind_provider_user_carrier_semantic(
            self.content[0], binding=binding
        ) != semantic:
            raise ValueError("typed provider-user message wire/semantic mismatch")

    @classmethod
    def from_provider_user_carrier(
        cls,
        *,
        role: MessageRole,
        carrier: EncodedProviderUserCarrier,
    ) -> "LLMMessage":
        if role not in {
            MessageRole.USER,
            MessageRole.RUNTIME_REQUEST,
            MessageRole.RUNTIME_OBSERVATION,
        }:
            raise ValueError("provider user carrier has an invalid internal role")
        semantic = carrier.semantic_fact
        if not isinstance(
            semantic,
            HumanInputWireSemanticFact
            | RuntimeRequestWireSemanticFact
            | RuntimeObservationWireSemanticFact,
        ):
            raise TypeError("provider user carrier has an invalid semantic fact")
        return cls(
            role=role,
            content=(carrier.canonical_text,),
            provider_user_carrier_semantic=semantic,
            provider_user_carrier_binding=provider_user_carrier_binding(carrier),
        )

    @classmethod
    def system(cls, text: str) -> "LLMMessage":
        return cls(role=MessageRole.SYSTEM, content=(text,))

    @classmethod
    def user(
        cls,
        text: str,
        *,
        causal_occurrence_semantic_fingerprint: str | None = None,
    ) -> "LLMMessage":
        carrier = encode_human_input(
            text,
            causal_occurrence_semantic_fingerprint=(
                causal_occurrence_semantic_fingerprint
            ),
        )
        return cls.from_provider_user_carrier(
            role=MessageRole.USER,
            carrier=carrier,
        )

    @classmethod
    def assistant(cls, text: str) -> "LLMMessage":
        return cls(role=MessageRole.ASSISTANT, content=(text,))

    @classmethod
    def assistant_turn(
        cls,
        *,
        text: str | None = None,
        thinking: str | tuple[str, ...] = (),
        tool_calls: tuple[LLMToolCall, ...] = (),
    ) -> "LLMMessage":
        content = (text,) if text else ()
        thinking_parts = (thinking,) if isinstance(thinking, str) and thinking else tuple(thinking)
        return cls(
            role=MessageRole.ASSISTANT,
            content=content,
            thinking=thinking_parts,
            tool_calls=tool_calls,
        )

    @classmethod
    def tool_call(cls, *, tool_call_id: str, name: str, arguments: str) -> "LLMMessage":
        return cls(
            role=MessageRole.TOOL_CALL,
            tool_call_id=tool_call_id,
            name=name,
            arguments=arguments,
        )

    @classmethod
    def tool_result(cls, text: str, *, tool_call_id: str | None = None) -> "LLMMessage":
        return cls(role=MessageRole.TOOL_RESULT, content=(text,), tool_call_id=tool_call_id)

    @classmethod
    def runtime_request(
        cls,
        text: str,
        *,
        request_kind: RuntimeRequestKind,
        business_occurrence_semantic_fingerprint: str | None = None,
        lifecycle_class: str | None = None,
    ) -> "LLMMessage":
        occurrence = business_occurrence_semantic_fingerprint or context_fingerprint(
            "runtime-request-content-occurrence:v1", (request_kind, text)
        )
        carrier = encode_runtime_request(
            text,
            request_kind=request_kind,
            business_occurrence_semantic_fingerprint=occurrence,
            lifecycle_class=lifecycle_class,
        )
        return cls.from_provider_user_carrier(
            role=MessageRole.RUNTIME_REQUEST,
            carrier=carrier,
        )

    @classmethod
    def runtime_observation(
        cls,
        payload: RuntimeObservationPayloadFact,
        *,
        observation_kind: str,
        source_instance_id: str,
        lifecycle_class: str,
        authority_class: str,
        causal_occurrence_semantic_fingerprint: str | None = None,
    ) -> "LLMMessage":
        occurrence = causal_occurrence_semantic_fingerprint or context_fingerprint(
            "runtime-observation-content-occurrence:v1",
            (
                observation_kind,
                source_instance_id,
                payload.payload_semantic_fingerprint,
            ),
        )
        carrier = encode_runtime_observation(
            payload,
            observation_kind=observation_kind,
            source_instance_id=source_instance_id,
            lifecycle_class=lifecycle_class,
            authority_class=authority_class,
            causal_occurrence_semantic_fingerprint=occurrence,
        )
        return cls.from_provider_user_carrier(
            role=MessageRole.RUNTIME_OBSERVATION,
            carrier=carrier,
        )

    @classmethod
    def runtime_observation_from_wire(
        cls,
        text: str,
        *,
        causal_occurrence_semantic_fingerprint: str,
    ) -> "LLMMessage":
        semantic = decode_runtime_observation_wire_semantic(
            text,
            causal_occurrence_semantic_fingerprint=(
                causal_occurrence_semantic_fingerprint
            ),
        )
        canonical = canonical_runtime_observation_wire_from_semantic(semantic)
        carrier = EncodedProviderUserCarrier(
            carrier_kind="runtime_observation",
            canonical_text=canonical,
            canonical_utf8_sha256=semantic.canonical_wire_utf8_sha256,
            canonical_utf8_bytes=semantic.canonical_wire_utf8_bytes,
            semantic_fingerprint=semantic.wire_semantic_fingerprint,
            semantic_fact=semantic,
            occurrence_semantic_fingerprint=(
                causal_occurrence_semantic_fingerprint
            ),
        )
        return cls.from_provider_user_carrier(
            role=MessageRole.RUNTIME_OBSERVATION,
            carrier=carrier,
        )
