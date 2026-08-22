"""Provider-neutral LLM request objects."""

from __future__ import annotations

from dataclasses import dataclass, field

from pulsara_agent.llm.input import LLMMessage, LLMToolCall, MessageRole, ToolSpec
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
            *(((("TEXT", text),) if text else ())),
            *(
                ("TOOL_CALL", item.id, item.name, item.arguments)
                for item in tool_calls
            ),
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
            *(
                (("TEXT", "".join(message.content)),)
                if any(message.content)
                else ()
            ),
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
    generic_message_group_fingerprint: str
    replay_fragment_fingerprint: str
    replacement_wire_fingerprint: str
    semantic_debit_utf8_bytes: int
    replay_addend_utf8_bytes: int
    semantic_debit_tokens: int
    replay_addend_tokens: int

    def __post_init__(self) -> None:
        if (
            self.first_message_ordinal < 0
            or self.message_count < 1
            or not self.assistant_entry_id
            or min(
                self.semantic_debit_utf8_bytes,
                self.replay_addend_utf8_bytes,
                self.semantic_debit_tokens,
                self.replay_addend_tokens,
            )
            < 0
        ):
            raise ValueError("provider wire replacement identity is invalid")
        for value in (
            self.generic_message_group_fingerprint,
            self.replay_fragment_fingerprint,
            self.replacement_wire_fingerprint,
        ):
            if not value.startswith("sha256:"):
                raise ValueError("provider wire replacement fingerprint is invalid")


@dataclass(frozen=True, slots=True)
class FrozenProviderWireMaterialization:
    root_policy_value: FrozenJsonValue = field(repr=False)
    tool_items: tuple[FrozenJsonObjectFact, ...] = field(repr=False)
    ordered_input_items: tuple[FrozenJsonObjectFact, ...] = field(repr=False)


@dataclass(frozen=True, slots=True)
class FrozenProviderWireInputQuote:
    estimator_fingerprint: str
    effective_input_budget_tokens: int
    semantic_total_input_tokens: int
    semantic_message_tokens: int
    semantic_message_utf8_bytes: int
    replaced_semantic_debit_tokens: int
    replay_addend_tokens: int
    replaced_semantic_debit_utf8_bytes: int
    replay_addend_utf8_bytes: int
    final_message_tokens: int
    final_total_input_tokens: int
    final_message_utf8_bytes: int
    final_wire_utf8_bytes: int
    quote_contract_version: str

    def __post_init__(self) -> None:
        values = (
            self.effective_input_budget_tokens,
            self.semantic_total_input_tokens,
            self.semantic_message_tokens,
            self.semantic_message_utf8_bytes,
            self.replaced_semantic_debit_tokens,
            self.replay_addend_tokens,
            self.replaced_semantic_debit_utf8_bytes,
            self.replay_addend_utf8_bytes,
            self.final_message_tokens,
            self.final_total_input_tokens,
            self.final_message_utf8_bytes,
            self.final_wire_utf8_bytes,
        )
        if (
            any(value < 0 for value in values)
            or not self.estimator_fingerprint.startswith("sha256:")
            or not self.quote_contract_version
        ):
            raise ValueError("provider wire quote contains a negative value")
        if self.final_message_tokens != (
            self.semantic_message_tokens
            - self.replaced_semantic_debit_tokens
            + self.replay_addend_tokens
        ):
            raise ValueError("provider wire message token quote is inconsistent")
        if self.final_total_input_tokens != (
            self.semantic_total_input_tokens
            - self.replaced_semantic_debit_tokens
            + self.replay_addend_tokens
        ):
            raise ValueError("provider wire total token quote is inconsistent")
        if self.final_message_utf8_bytes != (
            self.semantic_message_utf8_bytes
            - self.replaced_semantic_debit_utf8_bytes
            + self.replay_addend_utf8_bytes
        ):
            raise ValueError("provider wire message byte quote is inconsistent")
        if self.final_total_input_tokens > self.effective_input_budget_tokens:
            raise ValueError("provider wire quote exceeds the input budget")
        if self.final_wire_utf8_bytes > MAXIMUM_PROVIDER_WIRE_INPUT_BYTES:
            raise ValueError("provider wire quote exceeds its hard byte bound")


@dataclass(frozen=True, slots=True)
class FrozenProviderWireInputPlan:
    context_id: str
    compiled_semantic_fingerprint: str
    message_placements_fingerprint: str
    wire_api: str
    provider_profile_fingerprint: str
    resolved_target_semantic_fingerprint: str
    materialization: FrozenProviderWireMaterialization = field(repr=False)
    replacements: tuple[FrozenProviderWireReplacementIdentity, ...]
    provider_replay_hydration_fingerprint: str | None
    wire_system_fingerprint: str
    wire_tools_fingerprint: str
    wire_input_prefix_fingerprint: str
    quote: FrozenProviderWireInputQuote

    def __post_init__(self) -> None:
        if (
            not self.context_id
            or self.wire_api
            not in {"openai_chat_completions", "openai_responses"}
        ):
            raise ValueError("provider wire plan identity is invalid")
        previous_end = 0
        for index, item in enumerate(self.replacements):
            if index and item.first_message_ordinal < previous_end:
                raise ValueError("provider wire replacements overlap")
            previous_end = item.first_message_ordinal + item.message_count
        if bool(self.replacements) != bool(
            self.provider_replay_hydration_fingerprint
        ):
            raise ValueError("provider replay hydration proof union is invalid")
        if self.provider_replay_hydration_fingerprint is not None and not (
            self.provider_replay_hydration_fingerprint.startswith("sha256:")
        ):
            raise ValueError("provider replay hydration fingerprint is invalid")
        for value in (
            self.compiled_semantic_fingerprint,
            self.message_placements_fingerprint,
            self.provider_profile_fingerprint,
            self.resolved_target_semantic_fingerprint,
            self.wire_system_fingerprint,
            self.wire_tools_fingerprint,
            self.wire_input_prefix_fingerprint,
        ):
            if not value.startswith("sha256:"):
                raise ValueError("provider wire plan fingerprint is invalid")
        root = thaw_json(self.materialization.root_policy_value)
        tools = tuple(thaw_json(item) for item in self.materialization.tool_items)
        inputs = tuple(
            thaw_json(item) for item in self.materialization.ordered_input_items
        )
        aggregate = (
            sum(item.semantic_debit_tokens for item in self.replacements),
            sum(item.replay_addend_tokens for item in self.replacements),
            sum(item.semantic_debit_utf8_bytes for item in self.replacements),
            sum(item.replay_addend_utf8_bytes for item in self.replacements),
        )
        if aggregate != (
            self.quote.replaced_semantic_debit_tokens,
            self.quote.replay_addend_tokens,
            self.quote.replaced_semantic_debit_utf8_bytes,
            self.quote.replay_addend_utf8_bytes,
        ):
            raise ValueError("provider wire replacement quote aggregate drifted")
        materialized_bytes = len(
            canonical_json_bytes(
                {"root": root, "tools": tools, "input": inputs}
            )
        )
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
            "pulsara.provider-wire-input-prefix:v1",
            {
                "api": self.wire_api,
                "profile": self.provider_profile_fingerprint,
                "root": root,
                "tools": tools,
                "input": inputs,
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
        },
    )


def provider_wire_input_quote_identity_fingerprint(
    quote: FrozenProviderWireInputQuote,
) -> str:
    return context_fingerprint(
        "pulsara.provider-wire-input-quote:v1",
        {
            "estimator": quote.estimator_fingerprint,
            "budget": quote.effective_input_budget_tokens,
            "semantic_total_tokens": quote.semantic_total_input_tokens,
            "semantic_message_tokens": quote.semantic_message_tokens,
            "semantic_message_bytes": quote.semantic_message_utf8_bytes,
            "debit_tokens": quote.replaced_semantic_debit_tokens,
            "addend_tokens": quote.replay_addend_tokens,
            "debit_bytes": quote.replaced_semantic_debit_utf8_bytes,
            "addend_bytes": quote.replay_addend_utf8_bytes,
            "final_message_tokens": quote.final_message_tokens,
            "final_total_tokens": quote.final_total_input_tokens,
            "final_message_bytes": quote.final_message_utf8_bytes,
            "final_wire_bytes": quote.final_wire_utf8_bytes,
            "contract": quote.quote_contract_version,
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
            "profile": plan.provider_profile_fingerprint,
            "target": plan.resolved_target_semantic_fingerprint,
            "materialization": provider_wire_materialization_identity_fingerprint(
                plan.materialization
            ),
            "replacements": tuple(
                (
                    item.assistant_entry_id,
                    item.first_message_ordinal,
                    item.message_count,
                    item.generic_message_group_fingerprint,
                    item.replay_fragment_fingerprint,
                    item.replacement_wire_fingerprint,
                    item.semantic_debit_utf8_bytes,
                    item.replay_addend_utf8_bytes,
                    item.semantic_debit_tokens,
                    item.replay_addend_tokens,
                )
                for item in plan.replacements
            ),
            "provider_replay_hydration": plan.provider_replay_hydration_fingerprint,
            "wire_system": plan.wire_system_fingerprint,
            "wire_tools": plan.wire_tools_fingerprint,
            "wire_input": plan.wire_input_prefix_fingerprint,
            "quote": provider_wire_input_quote_identity_fingerprint(plan.quote),
        },
    )


@dataclass(frozen=True, slots=True)
class LLMOptions:
    reasoning_effort: str | None = None


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
    tool_choice_none: bool = False

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "system_prompt",
            compose_provider_root_policy(self.system_prompt),
        )


def llm_context_fingerprint(context: LLMContext) -> str:
    """Canonical provider-neutral identity for one fully resolved input."""

    return context_fingerprint(
        "provider-neutral-llm-context:v2-compaction-tool-suppression",
        {
            "system_prompt": context.system_prompt,
            "messages": tuple(
                {
                    "role": message.role.value,
                    "content": message.content,
                    "thinking": message.thinking,
                    "tool_calls": tuple(
                        {
                            "id": call.id,
                            "name": call.name,
                            "arguments": call.arguments,
                        }
                        for call in message.tool_calls
                    ),
                    "tool_call_id": message.tool_call_id,
                    "name": message.name,
                    "arguments": message.arguments,
                }
                for message in context.messages
            ),
            "tools": tuple(
                {
                    "name": tool.name,
                    "description": tool.description,
                    "parameters": tool.parameters,
                }
                for tool in context.tools
            ),
            "context_id": context.context_id,
            "resolved_model_call_id": context.resolved_model_call_id,
            "target_fingerprint": context.target_fingerprint,
            "model_call_index": context.model_call_index,
            "compiler_estimated_input_tokens": (
                context.compiler_estimated_input_tokens
            ),
            "tool_choice_none": context.tool_choice_none,
            "provider_wire_input_plan": (
                None
                if context.provider_wire_input_plan is None
                else provider_wire_input_plan_identity_fingerprint(
                    context.provider_wire_input_plan
                )
            ),
        },
    )
