"""Provider-neutral LLM request objects."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

from pulsara_agent.llm.input import LLMMessage, LLMToolCall, MessageRole, ToolSpec
from pulsara_agent.llm.model_connections import ReasoningSelection
from pulsara_agent.llm.user_carrier import compose_provider_root_policy
from pulsara_agent.primitives.context import (
    FrozenJsonObjectFact,
    FrozenJsonValue,
    canonical_json_bytes,
    context_fingerprint,
    thaw_json,
)


MAXIMUM_PROVIDER_WIRE_INPUT_BYTES = 64 << 20


def provider_assistant_public_projection_fingerprint(
    *,
    text: str,
    tool_calls: tuple[LLMToolCall, ...],
    ordered_blocks: tuple[tuple[object, ...], ...] | None = None,
) -> str:
    if ordered_blocks is None:
        ordered_blocks = (
            *((("TEXT", text),) if text else ()),
            *(("TOOL_CALL", item.id, item.name, item.arguments) for item in tool_calls),
        )
    return context_fingerprint(
        "pulsara.provider-assistant-public-projection:v2",
        {
            "text": text,
            "tool_calls": tuple(
                (item.id, item.name, item.arguments) for item in tool_calls
            ),
            "ordered_blocks": ordered_blocks,
        },
    )


def provider_assistant_message_public_projection_fingerprint(
    message: LLMMessage,
) -> str:
    if message.role is not MessageRole.ASSISTANT:
        raise ValueError("provider assistant projection requires assistant role")
    return provider_assistant_public_projection_fingerprint(
        text="".join(message.content),
        tool_calls=message.tool_calls,
        ordered_blocks=(
            *((("TEXT", "".join(message.content)),) if any(message.content) else ()),
            *(
                ("TOOL_CALL", item.id, item.name, item.arguments)
                for item in message.tool_calls
            ),
        ),
    )


@dataclass(frozen=True, slots=True)
class FrozenProviderWireReplacementIdentity:
    assistant_entry_id: str
    first_message_ordinal: int
    message_count: int
    replay_fragment_fingerprint: str
    generic_wire_estimated_tokens: int
    replay_wire_estimated_tokens: int

    def __post_init__(self) -> None:
        if (
            self.first_message_ordinal < 0
            or self.message_count < 1
            or not self.assistant_entry_id
            or min(
                self.generic_wire_estimated_tokens,
                self.replay_wire_estimated_tokens,
            )
            < 0
        ):
            raise ValueError("provider wire replacement identity is invalid")
        if not self.replay_fragment_fingerprint.startswith("sha256:"):
            raise ValueError("provider replay fragment fingerprint is invalid")


@dataclass(frozen=True, slots=True)
class FrozenProviderWireMaterialization:
    root_policy_value: FrozenJsonValue = field(repr=False)
    tool_items: tuple[FrozenJsonObjectFact, ...] = field(repr=False)
    ordered_input_items: tuple[FrozenJsonObjectFact, ...] = field(repr=False)
    context_bearing_projection: FrozenJsonObjectFact = field(repr=False)


@dataclass(frozen=True, slots=True)
class FrozenProviderWireInputQuote:
    wire_api: str
    estimator_fingerprint: str
    effective_input_budget_tokens: int
    semantic_estimated_input_tokens: int
    generic_wire_estimated_input_tokens: int
    replaced_generic_wire_estimated_tokens: int
    replay_wire_estimated_tokens: int
    final_wire_estimated_input_tokens: int
    final_wire_utf8_bytes: int

    def __post_init__(self) -> None:
        values = (
            self.effective_input_budget_tokens,
            self.semantic_estimated_input_tokens,
            self.generic_wire_estimated_input_tokens,
            self.replaced_generic_wire_estimated_tokens,
            self.replay_wire_estimated_tokens,
            self.final_wire_estimated_input_tokens,
            self.final_wire_utf8_bytes,
        )
        if (
            any(value < 0 for value in values)
            or self.replaced_generic_wire_estimated_tokens
            > self.generic_wire_estimated_input_tokens
            or self.wire_api not in {"openai_chat_completions", "openai_responses"}
            or not self.estimator_fingerprint.startswith("sha256:")
        ):
            raise ValueError("provider wire quote is invalid")
        if self.final_wire_estimated_input_tokens != (
            self.generic_wire_estimated_input_tokens
            - self.replaced_generic_wire_estimated_tokens
            + self.replay_wire_estimated_tokens
        ):
            raise ValueError("provider wire token quote is inconsistent")


@dataclass(frozen=True, slots=True)
class FrozenProviderWireInputPlan:
    context_id: str
    compiled_semantic_fingerprint: str
    message_placements_fingerprint: str
    wire_api: str
    route_wire_profile_fingerprint: str
    resolved_target_semantic_fingerprint: str
    materialization: FrozenProviderWireMaterialization = field(repr=False)
    replacements: tuple[FrozenProviderWireReplacementIdentity, ...]
    provider_replay_hydration_fingerprint: str | None
    wire_system_fingerprint: str
    wire_tools_fingerprint: str
    wire_input_prefix_fingerprint: str
    quote: FrozenProviderWireInputQuote

    def __post_init__(self) -> None:
        if not self.context_id or self.wire_api not in {
            "openai_chat_completions",
            "openai_responses",
        }:
            raise ValueError("provider wire plan identity is invalid")
        previous_end = 0
        for index, item in enumerate(self.replacements):
            if index and item.first_message_ordinal < previous_end:
                raise ValueError("provider wire replacements overlap")
            previous_end = item.first_message_ordinal + item.message_count
        if bool(self.replacements) != bool(self.provider_replay_hydration_fingerprint):
            raise ValueError("provider replay hydration proof union is invalid")
        if self.provider_replay_hydration_fingerprint is not None and not (
            self.provider_replay_hydration_fingerprint.startswith("sha256:")
        ):
            raise ValueError("provider replay hydration fingerprint is invalid")
        for value in (
            self.compiled_semantic_fingerprint,
            self.message_placements_fingerprint,
            self.route_wire_profile_fingerprint,
            self.resolved_target_semantic_fingerprint,
            self.wire_system_fingerprint,
            self.wire_tools_fingerprint,
            self.wire_input_prefix_fingerprint,
        ):
            if not value.startswith("sha256:"):
                raise ValueError("provider wire plan fingerprint is invalid")
        if self.quote.wire_api != self.wire_api:
            raise ValueError("provider wire plan API differs from its quote")
        if (
            self.quote.final_wire_estimated_input_tokens
            > self.quote.effective_input_budget_tokens
        ):
            raise ValueError("provider wire plan exceeds the input budget")
        if self.quote.final_wire_utf8_bytes > MAXIMUM_PROVIDER_WIRE_INPUT_BYTES:
            raise ValueError("provider wire plan exceeds its hard byte bound")
        root = thaw_json(self.materialization.root_policy_value)
        tools = tuple(thaw_json(item) for item in self.materialization.tool_items)
        aggregate = (
            sum(item.generic_wire_estimated_tokens for item in self.replacements),
            sum(item.replay_wire_estimated_tokens for item in self.replacements),
        )
        if aggregate != (
            self.quote.replaced_generic_wire_estimated_tokens,
            self.quote.replay_wire_estimated_tokens,
        ):
            raise ValueError("provider wire replacement quote aggregate drifted")
        projection = thaw_json(self.materialization.context_bearing_projection)
        materialized_bytes = len(canonical_json_bytes(projection))
        if self.quote.final_wire_utf8_bytes != materialized_bytes:
            raise ValueError("provider wire materialization byte quote drifted")
        if self.wire_system_fingerprint != context_fingerprint(
            "pulsara.provider-wire-system:v1", root
        ):
            raise ValueError("provider wire system proof drifted")
        if self.wire_tools_fingerprint != context_fingerprint(
            "pulsara.provider-wire-tools:v1", tools
        ):
            raise ValueError("provider wire tools proof drifted")
        expected_prefix = context_fingerprint(
            "pulsara.provider-wire-input-prefix:v2-final-context-projection",
            {
                "api": self.wire_api,
                "profile": self.route_wire_profile_fingerprint,
                "projection": projection,
            },
        )
        if self.wire_input_prefix_fingerprint != expected_prefix:
            raise ValueError("provider wire input prefix proof drifted")


def provider_wire_materialization_identity_fingerprint(
    materialization: FrozenProviderWireMaterialization,
) -> str:
    return context_fingerprint(
        "pulsara.provider-wire-materialization:v1",
        {
            "root": thaw_json(materialization.root_policy_value),
            "tools": tuple(thaw_json(item) for item in materialization.tool_items),
            "input": tuple(
                thaw_json(item) for item in materialization.ordered_input_items
            ),
            "context_projection": thaw_json(materialization.context_bearing_projection),
        },
    )


def provider_wire_input_plan_identity_fingerprint(
    plan: FrozenProviderWireInputPlan,
) -> str:
    """Derive the historical stable plan identity at its actual consumers."""

    return context_fingerprint(
        "pulsara.provider-wire-input-plan:v2-durable-replay",
        {
            "context": plan.context_id,
            "compiled": plan.compiled_semantic_fingerprint,
            "placements": plan.message_placements_fingerprint,
            "api": plan.wire_api,
            "profile": plan.route_wire_profile_fingerprint,
            "target": plan.resolved_target_semantic_fingerprint,
            "materialization": provider_wire_materialization_identity_fingerprint(
                plan.materialization
            ),
            "replacements": tuple(
                (
                    item.assistant_entry_id,
                    item.first_message_ordinal,
                    item.message_count,
                    item.replay_fragment_fingerprint,
                    item.generic_wire_estimated_tokens,
                    item.replay_wire_estimated_tokens,
                )
                for item in plan.replacements
            ),
            "provider_replay_hydration": plan.provider_replay_hydration_fingerprint,
            "wire_system": plan.wire_system_fingerprint,
            "wire_tools": plan.wire_tools_fingerprint,
            "wire_input": plan.wire_input_prefix_fingerprint,
            "quote": {
                "wire_api": plan.quote.wire_api,
                "estimator": plan.quote.estimator_fingerprint,
                "budget": plan.quote.effective_input_budget_tokens,
                "semantic_estimated": (plan.quote.semantic_estimated_input_tokens),
                "generic_wire_estimated": (
                    plan.quote.generic_wire_estimated_input_tokens
                ),
                "replaced_generic_wire_estimated": (
                    plan.quote.replaced_generic_wire_estimated_tokens
                ),
                "replay_wire_estimated": (plan.quote.replay_wire_estimated_tokens),
                "final_wire_estimated": (plan.quote.final_wire_estimated_input_tokens),
                "final_wire_bytes": plan.quote.final_wire_utf8_bytes,
            },
        },
    )


@dataclass(frozen=True, slots=True)
class LLMOptions:
    reasoning: ReasoningSelection | None = None


@dataclass(frozen=True, slots=True)
class LLMContext:
    messages: tuple[LLMMessage, ...]
    context_id: str
    resolved_model_call_id: str
    target_fingerprint: str
    model_call_index: int | None
    tools: tuple[ToolSpec, ...] = field(default_factory=tuple)
    system_prompt: str | None = None
    compiler_estimated_input_tokens: int | None = None
    provider_wire_input_plan: FrozenProviderWireInputPlan | None = field(
        default=None, repr=False
    )
    tool_choice: Literal["auto", "none"] | None = None

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "system_prompt",
            compose_provider_root_policy(self.system_prompt),
        )
