"""Finite, tool-free auxiliary JSON model operations.

This process-local port is shared by bounded advisory-memory owners.
It owns transport setup and physical completion, but has no job claim,
conversation continuity, canonical mutation, or retry authority.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
import json
from hashlib import sha256
from typing import Callable, Mapping, Protocol
from urllib.parse import urlsplit
from uuid import uuid4

from pulsara_agent.llm.adapters.openai.chat_completions import (
    chat_semantic_wire_group,
    materialize_chat_context_bearing_wire_projection,
)
from pulsara_agent.llm.adapters.openai.client import OpenAITransportTimeoutPolicy
from pulsara_agent.llm.adapters.openai.responses import (
    materialize_responses_context_bearing_wire_projection,
    responses_semantic_wire_group,
)
from pulsara_agent.llm.estimator import estimate_model_context_for_call
from pulsara_agent.llm.input import (
    LLMMessage,
    LLMTextPart,
    MessageRole,
)
from pulsara_agent.llm.model_connections import ModelCallBinding
from pulsara_agent.llm.model_target import default_reasoning_selection
from pulsara_agent.llm.request import LLMContext
from pulsara_agent.llm.resolution import (
    ResolvedModelCall,
    ResolvedModelTarget,
    resolve_model_call,
    with_call_output_cap,
)
from pulsara_agent.llm.runtime import ModelRuntime
from pulsara_agent.llm.validation import validate_model_context_shape_for_call
from pulsara_agent.memory.product_contract import (
    MEMORY_GOVERNANCE_SYSTEM_PROMPT_V3,
)
from pulsara_agent.ports.live_agent_event import (
    TextDeltaPayload,
    TextEndPayload,
    TextStartPayload,
)
from pulsara_agent.ports.provider_stream import (
    ProviderModelExecutionFailed,
    ProviderModelOutputIncomplete,
    ProviderNormalizedTerminalKind,
    ProviderPhysicalCompletionStatus,
    ProviderStreamTerminal,
)
from pulsara_agent.primitives.model_call import ModelCallPurpose
from pulsara_agent.primitives.context import canonical_json_bytes


@dataclass(frozen=True, slots=True)
class PreparedAuxiliaryJsonModelCall:
    call: ResolvedModelCall
    context: LLMContext
    estimated_input_tokens: int
    final_wire_utf8_bytes: int
    maximum_result_bytes: int
    transport_timeout_policy_fingerprint: str


class AuxiliaryJsonModelPort(Protocol):
    def prepare_json_call(
        self,
        *,
        purpose: ModelCallPurpose,
        messages: tuple[LLMMessage, ...],
        maximum_input_tokens: int,
        maximum_input_bytes: int,
        maximum_output_tokens: int,
        timeout_policy: OpenAITransportTimeoutPolicy,
        origin_binding: ModelCallBinding,
        maximum_result_bytes: int = 256 << 10,
    ) -> PreparedAuxiliaryJsonModelCall: ...

    def prepare_first_fitting_json_call(
        self,
        *,
        purpose: ModelCallPurpose,
        message_variants: tuple[tuple[LLMMessage, ...], ...],
        maximum_input_tokens: int,
        maximum_input_bytes: int,
        maximum_output_tokens: int,
        timeout_policy: OpenAITransportTimeoutPolicy,
        origin_binding: ModelCallBinding,
        maximum_result_bytes: int = 256 << 10,
    ) -> tuple[PreparedAuxiliaryJsonModelCall, int] | None: ...

    async def complete_prepared_json(
        self, prepared: PreparedAuxiliaryJsonModelCall
    ) -> Mapping[str, object]: ...


class DirectKernelAuxiliaryJsonModel:
    """One finite JSON call with no tools or continuity capability."""

    def __init__(
        self,
        model_runtime: ModelRuntime,
        *,
        call_resolver: Callable[..., object] = resolve_model_call,
        context_shape_validator: Callable[..., object] = (
            validate_model_context_shape_for_call
        ),
    ) -> None:
        self._model_runtime = model_runtime
        self._call_resolver = call_resolver
        self._context_shape_validator = context_shape_validator

    def prepare_json_call(
        self,
        *,
        purpose: ModelCallPurpose,
        messages: tuple[LLMMessage, ...],
        maximum_input_tokens: int,
        maximum_input_bytes: int,
        maximum_output_tokens: int,
        timeout_policy: OpenAITransportTimeoutPolicy,
        origin_binding: ModelCallBinding,
        maximum_result_bytes: int = 256 << 10,
    ) -> PreparedAuxiliaryJsonModelCall:
        selected = self.prepare_first_fitting_json_call(
            purpose=purpose,
            message_variants=(messages,),
            maximum_input_tokens=maximum_input_tokens,
            maximum_input_bytes=maximum_input_bytes,
            maximum_output_tokens=maximum_output_tokens,
            timeout_policy=timeout_policy,
            origin_binding=origin_binding,
            maximum_result_bytes=maximum_result_bytes,
        )
        if selected is None:
            raise ValueError("auxiliary provider final wire exceeds its finite caps")
        return selected[0]

    def prepare_first_fitting_json_call(
        self,
        *,
        purpose: ModelCallPurpose,
        message_variants: tuple[tuple[LLMMessage, ...], ...],
        maximum_input_tokens: int,
        maximum_input_bytes: int,
        maximum_output_tokens: int,
        timeout_policy: OpenAITransportTimeoutPolicy,
        origin_binding: ModelCallBinding,
        maximum_result_bytes: int = 256 << 10,
    ) -> tuple[PreparedAuxiliaryJsonModelCall, int] | None:
        """Resolve once and return the first exact-final-wire fitting variant.

        The caller supplies a finite, semantically ordered set of shedding
        boundaries.  Only the selected context escapes this method and can be
        opened by the transport.
        """

        if purpose not in {
            ModelCallPurpose.CONTEXT_COMPACTION_SUMMARY,
            ModelCallPurpose.MEMORY_GOVERNANCE,
        }:
            raise ValueError("auxiliary JSON purpose is not in the closed contract")
        if timeout_policy.total_seconds is None:
            raise ValueError("auxiliary provider call requires a finite total timeout")
        if not 1 <= maximum_result_bytes <= 256 << 10:
            raise ValueError("auxiliary JSON result byte bound is invalid")
        if maximum_input_bytes < 1:
            raise ValueError("auxiliary provider input byte cap must be positive")
        if not message_variants:
            raise ValueError("auxiliary provider requires at least one message variant")
        base_target = self._model_runtime.resolve_target(
            origin_binding,
            timeout_policy=timeout_policy,
        )
        binding = ModelCallBinding(
            origin_binding.connection_id,
            default_reasoning_selection(base_target.contract.reasoning),
        )
        target = with_call_output_cap(
            self._model_runtime.resolve_target(
                binding,
                timeout_policy=timeout_policy,
            ),
            maximum_output_tokens,
        )
        call = self._call_resolver(
            target=target,
            binding=binding,
            purpose=purpose,
            resolved_model_call_id=f"model_call:{uuid4().hex}",
        )
        effective_input_cap = min(
            maximum_input_tokens,
            call.target.context_budget.input_budget_tokens,
        )
        for ordinal, messages in enumerate(message_variants):
            system_prompt, ordered_messages = _validate_auxiliary_message_shape(
                purpose=purpose,
                messages=messages,
            )
            context = LLMContext(
                system_prompt=system_prompt,
                messages=ordered_messages,
                context_id=f"auxiliary-context:{uuid4().hex}",
                resolved_model_call_id=call.resolved_model_call_id,
                target_fingerprint=target.fact.target_fingerprint,
                model_call_index=None,
                compiler_estimated_input_tokens=None,
            )
            semantic_estimate = estimate_model_context_for_call(
                call=call, context=context
            )
            context = replace(
                context,
                compiler_estimated_input_tokens=(
                    semantic_estimate.total_input_tokens
                ),
            )
            # Shape and target binding remain mandatory, but the semantic
            # estimate is diagnostic only. Admission is exclusively based on
            # the adapter's final context-bearing wire materialization below.
            self._context_shape_validator(call=call, context=context)
            (
                fixed_projection,
                final_projection,
                ordered_wire_items,
                ordered_wire_sources,
            ) = (
                _materialize_auxiliary_final_wire(call=call, context=context)
            )
            final_wire_estimate = (
                call.target.token_estimator.estimate_final_wire_json_components(
                    fixed_context=fixed_projection,
                    ordered_input_items=ordered_wire_items,
                    ordered_input_sources=ordered_wire_sources,
                )
            )
            final_wire_utf8_bytes = len(canonical_json_bytes(final_projection))
            if (
                final_wire_estimate.total_input_tokens > effective_input_cap
                or final_wire_utf8_bytes > maximum_input_bytes
            ):
                continue
            return (
                PreparedAuxiliaryJsonModelCall(
                    call=call,
                    context=context,
                    estimated_input_tokens=final_wire_estimate.total_input_tokens,
                    final_wire_utf8_bytes=final_wire_utf8_bytes,
                    maximum_result_bytes=maximum_result_bytes,
                    transport_timeout_policy_fingerprint=(
                        timeout_policy.policy_fingerprint
                    ),
                ),
                ordinal,
            )
        return None

    async def complete_prepared_json(
        self, prepared: PreparedAuxiliaryJsonModelCall
    ) -> Mapping[str, object]:
        if not prepared.transport_timeout_policy_fingerprint.startswith("sha256:"):
            raise ValueError("auxiliary timeout policy fingerprint is invalid")
        execution = prepared.call.target.transport.open_stream(
            call=prepared.call, context=prepared.context
        )
        block_order: list[str] = []
        open_blocks: dict[str, list[str]] = {}
        completed_blocks: dict[str, str] = {}
        size = 0
        body_error: BaseException | None = None
        try:
            while True:
                item = await execution.read_next()
                if item is None:
                    break
                if isinstance(item, ProviderStreamTerminal):
                    if item.terminal_kind is (
                        ProviderNormalizedTerminalKind.OUTPUT_INCOMPLETE
                    ):
                        assert item.incomplete_reason is not None
                        raise ProviderModelOutputIncomplete(item.incomplete_reason)
                    if item.terminal_kind is ProviderNormalizedTerminalKind.PROVIDER_ERROR:
                        assert item.error is not None
                        raise ProviderModelExecutionFailed(item.error)
                    break
                if isinstance(item, TextStartPayload):
                    if item.block_identity in open_blocks:
                        raise RuntimeError(
                            "auxiliary provider reused a text block identity"
                        )
                    block_order.append(item.block_identity)
                    open_blocks[item.block_identity] = []
                elif isinstance(item, TextDeltaPayload):
                    if item.block_identity not in open_blocks:
                        raise RuntimeError("auxiliary text delta lacks start")
                    size += len(item.delta.encode("utf-8"))
                    if size > prepared.maximum_result_bytes:
                        raise ValueError("auxiliary result exceeds its byte cap")
                    open_blocks[item.block_identity].append(item.delta)
                elif isinstance(item, TextEndPayload):
                    if item.block_identity not in open_blocks:
                        raise RuntimeError("auxiliary text end lacks start")
                    if item.final_text != "".join(open_blocks[item.block_identity]):
                        raise RuntimeError("auxiliary terminal text drifted")
                    completed_blocks[item.block_identity] = item.final_text
                    del open_blocks[item.block_identity]
        except BaseException as exc:
            body_error = exc
        finally:
            await execution.aclose()
            completion = await execution.wait_physical_completion()
        if body_error is not None:
            raise body_error
        if completion.status is not ProviderPhysicalCompletionStatus.COMPLETED:
            raise RuntimeError("auxiliary provider physical operation did not exit")
        if open_blocks:
            raise RuntimeError("auxiliary provider ended with open text blocks")
        value = json.loads("".join(completed_blocks[item] for item in block_order))
        if not isinstance(value, dict):
            raise ValueError("auxiliary provider result must be a JSON object")
        return value


def _validate_auxiliary_message_shape(
    *,
    purpose: ModelCallPurpose,
    messages: tuple[LLMMessage, ...],
) -> tuple[str | None, tuple[LLMMessage, ...]]:
    """Close every auxiliary purpose over one exact provider message shape."""

    if purpose is ModelCallPurpose.MEMORY_GOVERNANCE:
        if (
            len(messages) != 2
            or messages[0].role is not MessageRole.SYSTEM
            or messages[0].content
            != (LLMTextPart(MEMORY_GOVERNANCE_SYSTEM_PROMPT_V3),)
            or messages[1].role is not MessageRole.USER
        ):
            raise ValueError(
                "memory governance requires its exact stable SYSTEM and one USER packet"
            )
        system_prompt = MEMORY_GOVERNANCE_SYSTEM_PROMPT_V3
        ordered_messages = (messages[1],)
    else:
        if len(messages) != 1 or messages[0].role is not MessageRole.USER:
            raise ValueError("auxiliary purpose requires exactly one USER message")
        system_prompt = None
        ordered_messages = messages
    if any(
        len(message.content) != 1
        or not isinstance(message.content[0], LLMTextPart)
        or not message.content[0].text
        for message in messages
    ):
        raise ValueError("auxiliary provider messages require one non-empty text block")
    return system_prompt, ordered_messages


def _materialize_auxiliary_final_wire(
    *,
    call: ResolvedModelCall,
    context: LLMContext,
) -> tuple[
    dict[str, object],
    dict[str, object],
    tuple[object, ...],
    tuple[LLMMessage | None, ...],
]:
    """Lower once through the selected adapter's exact context materializer."""

    profile = call.target.model_profile.route_wire_profile
    if profile.wire_api == "openai_chat_completions":
        groups = tuple(
            chat_semantic_wire_group(message, route_wire_profile=profile)
            for message in context.messages
        )
        ordered = tuple(item for group in groups for item in group)
        sources = tuple(
            message
            for message, group in zip(context.messages, groups, strict=True)
            for _ in group
        )
        fixed = materialize_chat_context_bearing_wire_projection(
            call=call,
            root_policy=context.system_prompt,
            ordered_input_items=(),
            tool_items=(),
            tool_choice=None,
        )
        final = materialize_chat_context_bearing_wire_projection(
            call=call,
            root_policy=context.system_prompt,
            ordered_input_items=ordered,
            tool_items=(),
            tool_choice=None,
        )
        return fixed, final, ordered, sources
    if profile.wire_api == "openai_responses":
        groups = tuple(
            responses_semantic_wire_group(message) for message in context.messages
        )
        ordered = tuple(item for group in groups for item in group)
        sources = tuple(
            message
            for message, group in zip(context.messages, groups, strict=True)
            for _ in group
        )
        fixed = materialize_responses_context_bearing_wire_projection(
            call=call,
            root_policy=context.system_prompt,
            ordered_input_items=(),
            tool_items=(),
            tool_choice=None,
        )
        final = materialize_responses_context_bearing_wire_projection(
            call=call,
            root_policy=context.system_prompt,
            ordered_input_items=ordered,
            tool_items=(),
            tool_choice=None,
        )
        return fixed, final, ordered, sources
    raise ValueError("auxiliary provider wire API is unsupported")


def provider_trust_domain_identity(target: ResolvedModelTarget) -> str:
    """Freeze a non-secret identity for same-provider auxiliary data egress."""

    parsed = urlsplit(target.contract.canonical_endpoint_base_url)
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname:
        raise ValueError("provider endpoint is not a valid HTTP origin")
    host = parsed.hostname.encode("idna").decode("ascii").lower()
    port = parsed.port
    if port == (443 if parsed.scheme.lower() == "https" else 80):
        port = None
    origin = f"{parsed.scheme.lower()}://{host}"
    if port is not None:
        origin += f":{port}"
    payload = canonical_json_bytes(
        {
            "route_id": target.contract.key.route_id,
            "wire_api": target.contract.key.wire_api.value,
            "origin": origin,
            "base_path": parsed.path.rstrip("/") or "/",
        }
    )
    return "provider-trust-domain:sha256:" + sha256(payload).hexdigest()


__all__ = [
    "AuxiliaryJsonModelPort",
    "DirectKernelAuxiliaryJsonModel",
    "PreparedAuxiliaryJsonModelCall",
    "provider_trust_domain_identity",
]
