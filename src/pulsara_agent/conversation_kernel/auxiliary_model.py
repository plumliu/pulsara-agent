"""Finite, tool-free auxiliary JSON model operations.

This process-local port is shared by durable jobs and advisory-memory owners.
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
    OpenAIChatCompletionsTransport,
)
from pulsara_agent.llm.adapters.openai.client import OpenAITransportTimeoutPolicy
from pulsara_agent.llm.adapters.openai.responses import OpenAIResponsesTransport
from pulsara_agent.llm.config import LLMConfig, ModelSlotConfig
from pulsara_agent.llm.estimator import estimate_model_context_for_call
from pulsara_agent.llm.input import LLMMessage
from pulsara_agent.llm.models import ModelRole
from pulsara_agent.llm.normalized_transport import (
    NormalizedLLMTransport,
    NormalizedLLMTransportRegistry,
)
from pulsara_agent.llm.request import LLMContext
from pulsara_agent.llm.resolution import (
    ResolvedModelCall,
    resolve_model_call,
    resolve_model_target,
)
from pulsara_agent.llm.validation import validate_model_context_for_call
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
from pulsara_agent.primitives.model_call import ModelCallPurpose, ModelContextLimits
from pulsara_agent.primitives.context import canonical_json_bytes


@dataclass(frozen=True, slots=True)
class PreparedAuxiliaryJsonModelCall:
    call: ResolvedModelCall
    context: LLMContext
    estimated_input_tokens: int
    maximum_result_bytes: int
    transport_timeout_policy_fingerprint: str


class AuxiliaryJsonModelPort(Protocol):
    def prepare_json_call(
        self,
        *,
        purpose: ModelCallPurpose,
        prompt: str,
        maximum_input_tokens: int,
        maximum_output_tokens: int,
        timeout_policy: OpenAITransportTimeoutPolicy,
        maximum_result_bytes: int = 256 << 10,
    ) -> PreparedAuxiliaryJsonModelCall: ...

    async def complete_prepared_json(
        self, prepared: PreparedAuxiliaryJsonModelCall
    ) -> Mapping[str, object]: ...


class DirectKernelAuxiliaryJsonModel:
    """One finite JSON call with no tools or continuity capability."""

    def __init__(
        self,
        config: LLMConfig,
        *,
        target_resolver: Callable[..., object] = resolve_model_target,
        call_resolver: Callable[..., object] = resolve_model_call,
        context_validator: Callable[..., object] = validate_model_context_for_call,
    ) -> None:
        self._config = config
        self._target_resolver = target_resolver
        self._call_resolver = call_resolver
        self._context_validator = context_validator

    def prepare_json_call(
        self,
        *,
        purpose: ModelCallPurpose,
        prompt: str,
        maximum_input_tokens: int,
        maximum_output_tokens: int,
        timeout_policy: OpenAITransportTimeoutPolicy,
        maximum_result_bytes: int = 256 << 10,
    ) -> PreparedAuxiliaryJsonModelCall:
        if purpose not in {
            ModelCallPurpose.CONTEXT_COMPACTION_SUMMARY,
            ModelCallPurpose.MEMORY_GOVERNANCE,
            ModelCallPurpose.MEMORY_HINT_REVIEW,
        }:
            raise ValueError("auxiliary JSON purpose is not in the closed contract")
        if timeout_policy.total_seconds is None:
            raise ValueError("auxiliary provider call requires a finite total timeout")
        if not 1 <= maximum_result_bytes <= 256 << 10:
            raise ValueError("auxiliary JSON result byte bound is invalid")
        config = _with_output_cap(self._config, maximum_output_tokens)
        registry = NormalizedLLMTransportRegistry()
        registry.register(
            NormalizedLLMTransport(
                OpenAIResponsesTransport(
                    api_key=config.api_key,
                    timeout_policy=timeout_policy,
                    retry_config=config.retry,
                    openai_sdk_max_retries=config.openai_sdk_max_retries,
                )
            )
        )
        registry.register(
            NormalizedLLMTransport(
                OpenAIChatCompletionsTransport(
                    api_key=config.api_key,
                    timeout_policy=timeout_policy,
                    retry_config=config.retry,
                    openai_sdk_max_retries=config.openai_sdk_max_retries,
                )
            )
        )
        target = self._target_resolver(
            config=config,
            registry=registry,
            role=ModelRole.FLASH,
            requested_options=None,
        )
        call = self._call_resolver(
            target=target,
            purpose=purpose,
            resolved_model_call_id=f"model_call:{uuid4().hex}",
        )
        context = LLMContext(
            messages=(LLMMessage.user(prompt),),
            context_id=f"auxiliary-context:{uuid4().hex}",
            resolved_model_call_id=call.resolved_model_call_id,
            target_fingerprint=target.fact.target_fingerprint,
            model_call_index=None,
            compiler_estimated_input_tokens=None,
        )
        estimate = estimate_model_context_for_call(call=call, context=context)
        if estimate.total_input_tokens > maximum_input_tokens:
            raise ValueError("auxiliary provider input exceeds its finite cap")
        context = replace(
            context,
            compiler_estimated_input_tokens=estimate.total_input_tokens,
        )
        validated = self._context_validator(call=call, context=context)
        if validated.estimate.total_input_tokens != estimate.total_input_tokens:
            raise RuntimeError("auxiliary provider estimate changed before dispatch")
        return PreparedAuxiliaryJsonModelCall(
            call=call,
            context=context,
            estimated_input_tokens=estimate.total_input_tokens,
            maximum_result_bytes=maximum_result_bytes,
            transport_timeout_policy_fingerprint=(
                timeout_policy.policy_fingerprint
            ),
        )

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


def _with_output_cap(config: LLMConfig, maximum_output_tokens: int) -> LLMConfig:
    if maximum_output_tokens < 1:
        raise ValueError("auxiliary provider output cap must be positive")
    slot = config.flash
    limits = slot.limits
    cap = min(maximum_output_tokens, limits.max_output_tokens)
    bounded = ModelContextLimits(
        total_context_tokens=limits.total_context_tokens,
        max_input_tokens=min(limits.max_input_tokens, limits.total_context_tokens - cap),
        max_output_tokens=limits.max_output_tokens,
        default_output_tokens=cap,
        input_safety_margin_tokens=min(
            limits.input_safety_margin_tokens,
            max(0, limits.total_context_tokens - cap - 1),
        ),
    )
    return replace(config, flash=ModelSlotConfig(slot.model_id, bounded))


def provider_trust_domain_identity(config: LLMConfig) -> str:
    """Freeze a non-secret identity for same-provider auxiliary data egress."""

    parsed = urlsplit(config.base_url)
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
            "provider": config.provider,
            "wire_api": config.api,
            "origin": origin,
            "base_path": parsed.path.rstrip("/") or "/",
            # A credential-slot digest prevents two tenants at one endpoint
            # from silently becoming one trust domain; the secret itself is
            # never exposed or persisted.
            "credential_slot": sha256(config.api_key.encode("utf-8")).hexdigest(),
        }
    )
    return "provider-trust-domain:sha256:" + sha256(payload).hexdigest()


__all__ = [
    "AuxiliaryJsonModelPort",
    "DirectKernelAuxiliaryJsonModel",
    "PreparedAuxiliaryJsonModelCall",
    "provider_trust_domain_identity",
]
