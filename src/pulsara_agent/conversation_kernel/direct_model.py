"""Transport-bearing adapter for the structured model-input compiler."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from threading import Lock
from typing import AsyncIterator, Callable
from uuid import uuid4

from pulsara_agent.conversation_kernel.tool_surface import (
    PreparedKernelToolSurface,
    ProcessLocalToolSurfaceBorrow,
)
from pulsara_agent.conversation_kernel.input_continuity import (
    ProcessLocalProviderInputInstallAuthority,
)
from pulsara_agent.llm.adapters.openai.chat_completions import (
    OpenAIChatCompletionsTransport,
)
from pulsara_agent.llm.adapters.openai.responses import OpenAIResponsesTransport
from pulsara_agent.llm.config import LLMConfig
from pulsara_agent.llm.input import ToolSpec
from pulsara_agent.llm.models import ModelRole
from pulsara_agent.llm.normalized_transport import (
    NormalizedLLMTransport,
    NormalizedLLMTransportRegistry,
)
from pulsara_agent.llm.request import LLMContext, LLMOptions
from pulsara_agent.llm.resolution import (
    ResolvedModelCall,
    resolve_model_call,
    resolve_model_target,
)
from pulsara_agent.llm.result import TransportUsageReport
from pulsara_agent.llm.validation import validate_model_context_for_call
from pulsara_agent.model_input.contracts import (
    FrozenCompiledModelInput,
    ModelInputCompileBinding,
    PreparedProviderInputCut,
    model_input_compile_binding_fingerprint,
)
from pulsara_agent.model_input.continuity import (
    ProcessLocalProviderInputInstallPermit,
)
from pulsara_agent.ports.live_agent_event import ProviderStreamPayload
from pulsara_agent.ports.provider_stream import (
    ProviderPhysicalCompletionStatus,
    ProviderStreamTerminal,
)
from pulsara_agent.primitives.context import context_fingerprint, thaw_json
from pulsara_agent.primitives.model_call import ModelCallPurpose


@dataclass(frozen=True, slots=True)
class KernelModelPreparationRequest:
    session_id: str
    turn_id: str
    model_call_index: int
    purpose: ModelCallPurpose
    maximum_input_tokens: int
    maximum_output_tokens: int
    tool_surface: PreparedKernelToolSurface = field(repr=False)


@dataclass(frozen=True, slots=True)
class PreparedKernelModelCall:
    session_id: str
    turn_id: str
    model_call_index: int
    call: ResolvedModelCall = field(repr=False)
    tool_surface: PreparedKernelToolSurface = field(repr=False)
    compile_binding: ModelInputCompileBinding
    preparation_fingerprint: str

    def __post_init__(self) -> None:
        if (
            not self.session_id
            or not self.turn_id
            or self.model_call_index < 1
            or self.call.fact != self.compile_binding.call_fact
            or self.call.target.fact != self.compile_binding.target_fact
            or self.tool_surface.model_surface != self.compile_binding.tool_surface
        ):
            raise ValueError("prepared model call facts do not exact-join")
        expected = _prepared_model_call_fingerprint(
            session_id=self.session_id,
            turn_id=self.turn_id,
            model_call_index=self.model_call_index,
            resolved_model_call_id=self.call.resolved_model_call_id,
            compile_binding_fingerprint=self.compile_binding.binding_fingerprint,
            surface_fingerprint=self.tool_surface.model_surface.surface_fingerprint,
        )
        if self.preparation_fingerprint != expected:
            raise ValueError("prepared model call fingerprint mismatch")


@dataclass(frozen=True, slots=True)
class KernelModelExecutionRequest:
    session_id: str
    turn_id: str
    model_call_index: int
    prepared_call: PreparedKernelModelCall = field(repr=False)
    compiled_input: FrozenCompiledModelInput = field(repr=False)
    cut: PreparedProviderInputCut
    surface_borrow: ProcessLocalToolSurfaceBorrow = field(repr=False)

    def __post_init__(self) -> None:
        identity = self.compiled_input.canonical_input_identity
        if (
            not self.session_id
            or not self.turn_id
            or self.model_call_index < 1
            or self.cut.session_id != self.session_id
            or self.cut.turn_id != self.turn_id
            or identity.session_id != self.cut.session_id
            or identity.turn_id != self.cut.turn_id
            or identity.context_binding_revision_id
            != self.cut.context_binding_revision_id
            or identity.provider_input_through_sequence
            != self.cut.provider_input_through_sequence
            or identity.conversation_scope_kind
            is not self.prepared_call.tool_surface.access.conversation_scope_kind
            or identity.scope_subagent_task_id
            != self.prepared_call.tool_surface.access.scope_subagent_task_id
            or not self.surface_borrow.exactly_joins(
                self.prepared_call.tool_surface
            )
        ):
            raise ValueError("model execution request is not structurally joined")


class _PreparedExecutionState(StrEnum):
    PREFLIGHTED = "PREFLIGHTED"
    OPENING = "OPENING"
    STREAMING = "STREAMING"
    PHYSICALLY_CLOSED = "PHYSICALLY_CLOSED"
    DISCARDED = "DISCARDED"


class PreparedKernelModelExecution:
    """Transport-bearing one-shot produced without opening the transport."""

    def __init__(
        self,
        *,
        request: KernelModelExecutionRequest,
        final_context: LLMContext,
        expected_append_candidate_fingerprint: str,
        execution_fingerprint: str,
        install_authority: ProcessLocalProviderInputInstallAuthority,
        usage_observer: Callable[
            [KernelModelExecutionRequest, TransportUsageReport], None
        ]
        | None,
    ) -> None:
        self.request = request
        self.final_context = final_context
        self.expected_append_candidate_fingerprint = (
            expected_append_candidate_fingerprint
        )
        self.execution_fingerprint = execution_fingerprint
        self._install_authority = install_authority
        self._usage_observer = usage_observer
        self._state = _PreparedExecutionState.PREFLIGHTED
        self._lock = Lock()

    @property
    def state(self) -> str:
        with self._lock:
            return self._state.value

    def discard(self) -> None:
        with self._lock:
            if self._state is not _PreparedExecutionState.PREFLIGHTED:
                raise RuntimeError("prepared model execution can no longer be discarded")
            self._state = _PreparedExecutionState.DISCARDED

    async def open_once(
        self,
        permit: ProcessLocalProviderInputInstallPermit,
    ) -> AsyncIterator[ProviderStreamPayload]:
        request = self.request
        if (
            permit.scope.session_id != request.session_id
            or permit.scope.scope_kind
            is not request.compiled_input.canonical_input_identity.conversation_scope_kind
            or permit.scope.scope_subagent_task_id
            != request.compiled_input.canonical_input_identity.scope_subagent_task_id
            or permit.candidate_fingerprint
            != self.expected_append_candidate_fingerprint
            or permit.execution_fingerprint != self.execution_fingerprint
        ):
            raise RuntimeError("provider-input install permit does not exact-join")
        self._install_authority.consume(
            permit,
            candidate_fingerprint=self.expected_append_candidate_fingerprint,
            execution_fingerprint=self.execution_fingerprint,
        )
        with self._lock:
            if self._state is not _PreparedExecutionState.PREFLIGHTED:
                raise RuntimeError("prepared model execution is not openable")
            self._state = _PreparedExecutionState.OPENING
        for tool in request.compiled_input.tools:
            if (
                request.surface_borrow.binding_fingerprint(tool.name)
                != tool.executor_binding_fingerprint
            ):
                with self._lock:
                    self._state = _PreparedExecutionState.DISCARDED
                raise RuntimeError("prepared tool binding was revoked before open")
        call = request.prepared_call.call
        execution = call.target.transport.open_stream(
            call=call, context=self.final_context
        )
        with self._lock:
            self._state = _PreparedExecutionState.STREAMING
        try:
            while True:
                item = await execution.read_next()
                if item is None:
                    break
                if isinstance(item, ProviderStreamTerminal):
                    if item.outcome != "COMPLETED":
                        assert item.error is not None
                        raise RuntimeError(f"provider failed: {item.error.code.value}")
                    if self._usage_observer is not None:
                        try:
                            self._usage_observer(request, item.usage)
                        except Exception:
                            pass
                    break
                yield item
        finally:
            await execution.aclose()
            completion = await execution.wait_physical_completion()
            with self._lock:
                self._state = _PreparedExecutionState.PHYSICALLY_CLOSED
            if completion.status is not ProviderPhysicalCompletionStatus.COMPLETED:
                raise RuntimeError("provider physical operation did not exit")


class DirectKernelModelPort:
    """Resolve once, exact-join once, then perform one physical stream."""

    def __init__(
        self,
        *,
        config: LLMConfig,
        role: ModelRole = ModelRole.PRO,
        options: LLMOptions | None = None,
        usage_observer: Callable[
            [KernelModelExecutionRequest, TransportUsageReport], None
        ]
        | None = None,
    ) -> None:
        registry = NormalizedLLMTransportRegistry()
        registry.register(
            NormalizedLLMTransport(
                OpenAIResponsesTransport(
                    api_key=config.api_key,
                    retry_config=config.retry,
                    openai_sdk_max_retries=config.openai_sdk_max_retries,
                )
            )
        )
        registry.register(
            NormalizedLLMTransport(
                OpenAIChatCompletionsTransport(
                    api_key=config.api_key,
                    retry_config=config.retry,
                    openai_sdk_max_retries=config.openai_sdk_max_retries,
                )
            )
        )
        self._config = config
        self._registry = registry
        self._role = role
        self._options = options
        self._usage_observer = usage_observer

    def prepare_call(
        self, request: KernelModelPreparationRequest
    ) -> PreparedKernelModelCall:
        if request.purpose is not ModelCallPurpose.AGENT_MODEL_LOOP:
            raise ValueError("foreground model preparation purpose is invalid")
        if (
            min(
                request.model_call_index,
                request.maximum_input_tokens,
                request.maximum_output_tokens,
            )
            < 1
        ):
            raise ValueError("foreground model preparation bounds are invalid")
        target = resolve_model_target(
            config=self._config,
            registry=self._registry,
            role=self._role,
            requested_options=self._options,
        )
        call = resolve_model_call(
            target=target,
            purpose=request.purpose,
            resolved_model_call_id=f"model_call:{uuid4().hex}",
        )
        if (
            target.context_budget.effective_output_tokens
            > request.maximum_output_tokens
        ):
            raise ValueError(
                "resolved provider output exceeds the foreground attempt cap"
            )
        surface = request.tool_surface.model_surface
        if surface.tool_specs and not target.fact.supports_tools:
            raise ValueError(
                "resolved model target does not support the prepared tools"
            )
        input_budget = min(
            request.maximum_input_tokens,
            target.context_budget.input_budget_tokens,
        )
        estimator_fingerprint = target.token_estimator.fact.estimator_fingerprint
        binding_fingerprint = model_input_compile_binding_fingerprint(
            call_fact=call.fact,
            target_fact=target.fact,
            estimator_fingerprint=estimator_fingerprint,
            effective_input_budget_tokens=input_budget,
            effective_output_tokens=target.context_budget.effective_output_tokens,
            tool_surface=surface,
        )
        compile_binding = ModelInputCompileBinding(
            call_fact=call.fact,
            target_fact=target.fact,
            estimator=target.token_estimator,
            estimator_fingerprint=estimator_fingerprint,
            effective_input_budget_tokens=input_budget,
            effective_output_tokens=target.context_budget.effective_output_tokens,
            tool_surface=surface,
            binding_fingerprint=binding_fingerprint,
        )
        preparation_fingerprint = _prepared_model_call_fingerprint(
            session_id=request.session_id,
            turn_id=request.turn_id,
            model_call_index=request.model_call_index,
            resolved_model_call_id=call.resolved_model_call_id,
            compile_binding_fingerprint=binding_fingerprint,
            surface_fingerprint=surface.surface_fingerprint,
        )
        return PreparedKernelModelCall(
            session_id=request.session_id,
            turn_id=request.turn_id,
            model_call_index=request.model_call_index,
            call=call,
            tool_surface=request.tool_surface,
            compile_binding=compile_binding,
            preparation_fingerprint=preparation_fingerprint,
        )

    def preflight_execution(
        self,
        request: KernelModelExecutionRequest,
        *,
        expected_append_candidate_fingerprint: str,
        install_authority: ProcessLocalProviderInputInstallAuthority,
    ) -> PreparedKernelModelExecution:
        if type(install_authority) is not ProcessLocalProviderInputInstallAuthority:
            raise TypeError("provider-input install authority is invalid")
        if not expected_append_candidate_fingerprint.startswith("sha256:"):
            raise ValueError("append candidate fingerprint is invalid")
        prepared = request.prepared_call
        compiled = request.compiled_input
        if (
            request.session_id != prepared.session_id
            or request.turn_id != prepared.turn_id
            or request.model_call_index != prepared.model_call_index
            or compiled.canonical_input_identity.session_id != request.session_id
            or compiled.canonical_input_identity.turn_id != request.turn_id
            or compiled.canonical_input_identity.context_binding_revision_id
            != request.cut.context_binding_revision_id
            or compiled.canonical_input_identity.provider_input_through_sequence
            != request.cut.provider_input_through_sequence
        ):
            raise ValueError("model execution identity does not exact-join preparation")
        if (
            compiled.compile_binding_fingerprint
            != prepared.compile_binding.binding_fingerprint
            or compiled.tools != prepared.tool_surface.model_surface.tool_specs
            or compiled.budget_report.tool_surface_fingerprint
            != prepared.tool_surface.model_surface.surface_fingerprint
            or compiled.final_estimate.total_input_tokens
            > prepared.compile_binding.effective_input_budget_tokens
        ):
            raise ValueError("compiled model input does not exact-join preparation")
        if not request.surface_borrow.exactly_joins(prepared.tool_surface):
            raise ValueError("model execution surface borrow does not join preparation")
        if (
            compiled.canonical_input_identity.conversation_scope_kind
            is not prepared.tool_surface.access.conversation_scope_kind
            or compiled.canonical_input_identity.scope_subagent_task_id
            != prepared.tool_surface.access.scope_subagent_task_id
        ):
            raise ValueError("model execution scope access does not exact-join input")
        # Revalidate the complete advertised binding immediately before any
        # mutable schema is created or the transport is opened.
        for tool in compiled.tools:
            if (
                request.surface_borrow.binding_fingerprint(tool.name)
                != tool.executor_binding_fingerprint
            ):
                raise RuntimeError("prepared tool binding was revoked")
        thawed_tools: list[ToolSpec] = []
        for item in compiled.tools:
            parameters = thaw_json(item.parameters)
            if not isinstance(parameters, dict):
                raise TypeError("frozen tool schema did not thaw to an object")
            thawed_tools.append(ToolSpec(item.name, item.description, parameters))
        call = prepared.call
        context = LLMContext(
            messages=compiled.messages,
            context_id=compiled.context_id,
            resolved_model_call_id=call.resolved_model_call_id,
            target_fingerprint=call.target.fact.target_fingerprint,
            model_call_index=request.model_call_index,
            tools=tuple(thawed_tools),
            system_prompt=compiled.system_prompt,
            compiler_estimated_input_tokens=compiled.final_estimate.total_input_tokens,
        )
        validated = validate_model_context_for_call(call=call, context=context)
        if validated.estimate != compiled.final_estimate:
            raise RuntimeError("compiler and pre-send input estimates differ")
        execution_fingerprint = context_fingerprint(
            "pulsara:prepared-kernel-model-execution:v1",
            {
                "preparation": prepared.preparation_fingerprint,
                "compiled": compiled.compiled_semantic_fingerprint,
                "cut": {
                    "session": request.cut.session_id,
                    "turn": request.cut.turn_id,
                    "revision": request.cut.context_binding_revision_id,
                    "through": request.cut.provider_input_through_sequence,
                },
                "append_candidate": expected_append_candidate_fingerprint,
            },
        )
        return PreparedKernelModelExecution(
            request=request,
            final_context=context,
            expected_append_candidate_fingerprint=(
                expected_append_candidate_fingerprint
            ),
            execution_fingerprint=execution_fingerprint,
            install_authority=install_authority,
            usage_observer=self._usage_observer,
        )


def _prepared_model_call_fingerprint(
    *,
    session_id: str,
    turn_id: str,
    model_call_index: int,
    resolved_model_call_id: str,
    compile_binding_fingerprint: str,
    surface_fingerprint: str,
) -> str:
    return context_fingerprint(
        "prepared-kernel-model-call:v1",
        {
            "session_id": session_id,
            "turn_id": turn_id,
            "model_call_index": model_call_index,
            "call_id": resolved_model_call_id,
            "compile_binding": compile_binding_fingerprint,
            "surface": surface_fingerprint,
        },
    )


__all__ = [
    "DirectKernelModelPort",
    "KernelModelExecutionRequest",
    "KernelModelPreparationRequest",
    "PreparedKernelModelExecution",
    "PreparedKernelModelCall",
]
