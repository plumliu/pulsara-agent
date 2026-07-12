"""The single V1 model-input token estimator."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from pulsara_agent.llm.input import LLMMessage, ToolSpec
from pulsara_agent.llm.request import LLMContext
from pulsara_agent.primitives.model_call import (
    TokenEstimatorFact,
    canonical_json_bytes,
    sha256_fingerprint,
)

TEXT_CHARS_PER_TOKEN = 4
JSON_CHARS_PER_TOKEN = 2
REQUEST_ENVELOPE_TOKENS = 3
SYSTEM_MESSAGE_FRAMING_TOKENS = 4
MESSAGE_FRAMING_TOKENS = 4
TOOL_CALL_FRAMING_TOKENS = 4
TOOL_SPEC_FRAMING_TOKENS = 8


@dataclass(frozen=True, slots=True)
class TokenEstimate:
    system_tokens: int
    message_tokens: int
    message_tokens_by_index: tuple[int, ...]
    tool_tokens: int
    envelope_tokens: int
    total_input_tokens: int

    def __post_init__(self) -> None:
        if self.message_tokens != sum(self.message_tokens_by_index):
            raise ValueError("message token total does not match per-message breakdown")
        if self.total_input_tokens != (
            self.system_tokens
            + self.message_tokens
            + self.tool_tokens
            + self.envelope_tokens
        ):
            raise ValueError("total input tokens do not match estimate components")
        if any(value < 0 for value in self.message_tokens_by_index):
            raise ValueError("message token estimates must be non-negative")


class TokenEstimator(Protocol):
    fact: TokenEstimatorFact

    def estimate_text(self, text: str) -> int: ...

    def estimate_json(self, value: object) -> int: ...

    def estimate_tool_spec(self, tool: ToolSpec) -> int: ...

    def estimate_message(self, message: LLMMessage) -> int: ...

    def estimate_context(self, context: LLMContext) -> TokenEstimate: ...


def _ceil_div(value: int, divisor: int) -> int:
    return (value + divisor - 1) // divisor


class PulsaraHeuristicTokenEstimatorV1:
    def __init__(self) -> None:
        payload = {
            "estimator_id": "pulsara_heuristic",
            "estimator_version": "v1",
            "constants": {
                "text_chars_per_token": TEXT_CHARS_PER_TOKEN,
                "json_chars_per_token": JSON_CHARS_PER_TOKEN,
                "request_envelope_tokens": REQUEST_ENVELOPE_TOKENS,
                "system_message_framing_tokens": SYSTEM_MESSAGE_FRAMING_TOKENS,
                "message_framing_tokens": MESSAGE_FRAMING_TOKENS,
                "tool_call_framing_tokens": TOOL_CALL_FRAMING_TOKENS,
                "tool_spec_framing_tokens": TOOL_SPEC_FRAMING_TOKENS,
            },
            "unicode_counting": "python_code_points",
            "canonical_json": "sort_keys,compact,utf8,finite",
            "message_fields": [
                "content",
                "thinking",
                "tool_calls.id",
                "tool_calls.name",
                "tool_calls.arguments",
                "tool_call_id",
                "name",
                "arguments",
            ],
            "breakdown": "per_message_includes_message_and_tool_call_framing",
        }
        self.fact = TokenEstimatorFact(
            estimator_id="pulsara_heuristic",
            estimator_version="v1",
            estimator_fingerprint=sha256_fingerprint("token-estimator:v1", payload),
        )

    def estimate_text(self, text: str) -> int:
        return 0 if text == "" else _ceil_div(len(text), TEXT_CHARS_PER_TOKEN)

    def estimate_json(self, value: object) -> int:
        rendered = canonical_json_bytes(value).decode("utf-8")
        return 0 if rendered == "" else _ceil_div(len(rendered), JSON_CHARS_PER_TOKEN)

    def estimate_tool_spec(self, tool: ToolSpec) -> int:
        return TOOL_SPEC_FRAMING_TOKENS + self.estimate_json(
            {
                "name": tool.name,
                "description": tool.description,
                "parameters": tool.parameters,
            }
        )

    def estimate_message(self, message: LLMMessage) -> int:
        total = MESSAGE_FRAMING_TOKENS
        total += sum(self.estimate_text(part) for part in message.content)
        total += sum(self.estimate_text(part) for part in message.thinking)
        for call in message.tool_calls:
            total += TOOL_CALL_FRAMING_TOKENS
            total += self.estimate_text(call.id)
            total += self.estimate_text(call.name)
            total += self.estimate_text(call.arguments)
        for value in (message.tool_call_id, message.name, message.arguments):
            if value is not None:
                total += self.estimate_text(value)
        return total

    def estimate_context(self, context: LLMContext) -> TokenEstimate:
        system_tokens = (
            SYSTEM_MESSAGE_FRAMING_TOKENS + self.estimate_text(context.system_prompt)
            if context.system_prompt
            else 0
        )
        message_tokens_by_index = tuple(
            self.estimate_message(message) for message in context.messages
        )
        message_tokens = sum(message_tokens_by_index)
        tool_tokens = sum(self.estimate_tool_spec(tool) for tool in context.tools)
        envelope_tokens = REQUEST_ENVELOPE_TOKENS
        return TokenEstimate(
            system_tokens=system_tokens,
            message_tokens=message_tokens,
            message_tokens_by_index=message_tokens_by_index,
            tool_tokens=tool_tokens,
            envelope_tokens=envelope_tokens,
            total_input_tokens=(
                system_tokens + message_tokens + tool_tokens + envelope_tokens
            ),
        )


def estimate_model_context_for_call(
    *, call: object, context: LLMContext
) -> TokenEstimate:
    """PR1 estimate-only seam; validation is layered around this in PR3."""

    target = getattr(call, "target", None)
    estimator = getattr(target, "token_estimator", None)
    if estimator is None:
        raise TypeError("resolved model call does not carry a token estimator")
    return estimator.estimate_context(context)
