"""Transport-bearing adapter for the structured model-input compiler."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
import json
from threading import Lock
from typing import AsyncIterator, Callable
from uuid import uuid4

from pulsara_agent.conversation_kernel.tool_surface import (
    PreparedKernelToolSurface,
    ProcessLocalToolSurfaceBorrow,
)
from pulsara_agent.capability.contracts import (
    FrozenNativeToolProjectionSet,
    FrozenNativeToolWireEligibilitySet,
    FrozenToolCapabilityFact,
    ToolCapabilityVersionRef,
    frozen_tool_spec_fingerprint,
)
from pulsara_agent.conversation_kernel.input_continuity import (
    ProcessLocalProviderInputInstallAuthority,
)
from pulsara_agent.conversation_kernel.execution_watchdogs import (
    DEFAULT_KERNEL_WATCHDOG_POLICY,
    OpenAITransportTimeoutPolicy,
)
from pulsara_agent.llm.adapters.openai.chat_completions import (
    OpenAIChatCompletionsTransport,
    chat_semantic_wire_group,
)
from pulsara_agent.llm.adapters.openai.function_tools import (
    freeze_openai_native_tool_eligibility,
    materialize_openai_native_tool_projection_set,
    openai_native_function_tool_contract_fingerprint,
)
from pulsara_agent.llm.adapters.openai.responses import (
    OpenAIResponsesTransport,
    responses_semantic_wire_group,
)
from pulsara_agent.llm.config import LLMConfig
from pulsara_agent.llm.input import LLMToolCall, ToolSpec
from pulsara_agent.llm.models import ModelRole
from pulsara_agent.process_api_key_boundary import ProcessApiKeyBoundary
from pulsara_agent.llm.provider import ProviderAssistantReplayCodecKind
from pulsara_agent.llm.provider_replay import (
    PreparedDurableProviderAssistantReplay,
    ProviderAssistantReplayFragment,
    ProviderReplayDisposition,
    ProviderReplayTargetCompatibilityFact,
    build_prepared_durable_provider_assistant_replay,
    build_provider_replay_target_compatibility,
)
from pulsara_agent.llm.normalized_transport import (
    NormalizedLLMTransport,
    NormalizedLLMTransportRegistry,
)
from pulsara_agent.llm.request import (
    FrozenProviderWireInputPlan,
    FrozenProviderWireInputQuote,
    FrozenProviderWireMaterialization,
    FrozenProviderWireReplacementIdentity,
    LLMContext,
    LLMOptions,
    provider_assistant_public_projection_fingerprint,
    provider_assistant_message_public_projection_fingerprint,
)
from pulsara_agent.llm.resolution import (
    ResolvedModelCall,
    ResolvedModelTarget,
    resolve_model_call,
    resolve_model_target,
)
from pulsara_agent.llm.result import TransportUsageReport
from pulsara_agent.llm.user_carrier import compose_provider_root_policy
from pulsara_agent.llm.validation import validate_model_context_for_call
from pulsara_agent.model_input.contracts import (
    FrozenCompiledModelInput,
    FrozenModelToolSurface,
    FrozenToolSpec,
    ModelInputScopeKind,
    ModelInputCompileBinding,
    PreparedProviderInputCut,
    compiled_message_placements_fingerprint,
    model_input_compile_binding_fingerprint,
)
from pulsara_agent.model_input.continuity import (
    FrozenProviderInputEpochView,
    PreparedProviderInputAppendCandidate,
    ProcessLocalProviderInputInstallPermit,
)
from pulsara_agent.model_input.provider_replay import (
    FrozenSelectedDurableProviderReplayHydration,
    selected_message_placements_fingerprint,
)
from pulsara_agent.ports.live_agent_event import ProviderStreamPayload
from pulsara_agent.ports.provider_stream import (
    ProviderAdapterCompletedReplayPayload,
    ProviderModelExecutionFailed,
    ProviderModelOutputIncomplete,
    ProviderNormalizedTerminalKind,
    ProviderPhysicalCompletionStatus,
    ProviderStreamTerminal,
)
from pulsara_agent.primitives.context import (
    FrozenJsonObjectFact,
    canonical_json_bytes,
    context_fingerprint,
    freeze_json,
    thaw_json,
)
from pulsara_agent.primitives.model_call import ModelCallPurpose
from pulsara_agent.conversation_kernel.memory.contracts import (
    FrozenModelCallMemoryContext,
    FrozenModelVisibleMemoryProvenance,
    ModelVisibleMemoryProvenanceDisposition,
)


@dataclass(frozen=True, slots=True)
class KernelModelTargetPreparationRequest:
    session_id: str
    turn_id: str
    model_call_index: int
    purpose: ModelCallPurpose
    maximum_input_tokens: int | None
    maximum_output_tokens: int


@dataclass(frozen=True, slots=True)
class PreparedKernelModelTarget:
    session_id: str
    turn_id: str
    model_call_index: int
    purpose: ModelCallPurpose
    target: ResolvedModelTarget = field(repr=False)
    call: ResolvedModelCall = field(repr=False)
    maximum_input_tokens: int
    maximum_output_tokens: int
    effective_input_budget_tokens: int
    native_function_tool_wire_contract_fingerprint: str
    transport_timeout_policy_fingerprint: str

    def __post_init__(self) -> None:
        if (
            not self.session_id
            or not self.turn_id
            or self.model_call_index < 1
            or self.purpose is not ModelCallPurpose.AGENT_MODEL_LOOP
            or self.call.target is not self.target
            or self.effective_input_budget_tokens < 1
            or self.target.context_budget.effective_output_tokens
            > self.maximum_output_tokens
        ):
            raise ValueError("prepared model target facts do not exact-join")
        expected_contract = openai_native_function_tool_contract_fingerprint(
            self.target.model_profile.provider_profile.wire_api
        )
        if (
            self.native_function_tool_wire_contract_fingerprint
            != expected_contract
        ):
            raise ValueError("prepared model target native contract drifted")


@dataclass(frozen=True, slots=True)
class KernelModelPreparationRequest:
    """Retained source-compatible request for direct component callers.

    Production dispatch uses ``prepare_target`` followed by
    ``bind_tool_surface`` so native eligibility is frozen before the parent
    capability cut.  This value remains a convenience at the adapter unit-test
    boundary only.
    """

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
    native_projection_set: FrozenNativeToolProjectionSet = field(repr=False)
    compile_binding: ModelInputCompileBinding
    transport_timeout_policy_fingerprint: str

    def __post_init__(self) -> None:
        specs = self.tool_surface.model_surface.tool_specs
        versions = self.native_projection_set.tool_versions
        projections = self.native_projection_set.projections
        if (
            not self.session_id
            or not self.turn_id
            or self.model_call_index < 1
            or self.call.fact != self.compile_binding.call_fact
            or self.call.target.fact != self.compile_binding.target_fact
            or self.tool_surface.model_surface != self.compile_binding.tool_surface
            or tuple(item.name for item in specs)
            != tuple(item.provider_name for item in versions)
            or self.tool_surface.model_surface.conversation_scope_kind
            is not self.native_projection_set.conversation_scope_kind
            or not self.transport_timeout_policy_fingerprint.startswith("sha256:")
        ):
            raise ValueError("prepared model call facts do not exact-join")
        for spec, projection in zip(specs, projections, strict=True):
            if (
                frozen_tool_spec_fingerprint(spec)
                != projection.canonical_tool_spec_fingerprint
            ):
                raise ValueError("prepared native projection changed canonical schema")


@dataclass(frozen=True, slots=True)
class PreparedKernelSemanticModelCall:
    """Provider-neutral compile binding with no executor or borrow authority."""

    session_id: str
    turn_id: str
    model_call_index: int
    call: ResolvedModelCall = field(repr=False)
    native_projection_set: FrozenNativeToolProjectionSet = field(repr=False)
    compile_binding: ModelInputCompileBinding

    def __post_init__(self) -> None:
        specs = self.compile_binding.tool_surface.tool_specs
        if (
            not self.session_id
            or not self.turn_id
            or self.model_call_index < 1
            or self.call.fact != self.compile_binding.call_fact
            or self.call.target.fact != self.compile_binding.target_fact
            or tuple(item.name for item in specs)
            != tuple(
                item.provider_name
                for item in self.native_projection_set.tool_versions
            )
            or self.compile_binding.tool_surface.conversation_scope_kind
            is not self.native_projection_set.conversation_scope_kind
        ):
            raise ValueError("semantic model call facts do not exact-join")


@dataclass(frozen=True, slots=True)
class KernelModelExecutionRequest:
    session_id: str
    turn_id: str
    model_call_index: int
    prepared_call: PreparedKernelModelCall = field(repr=False)
    compiled_input: FrozenCompiledModelInput = field(repr=False)
    wire_input_plan: FrozenProviderWireInputPlan = field(repr=False)
    cut: PreparedProviderInputCut
    surface_borrow: ProcessLocalToolSurfaceBorrow = field(repr=False)
    memory_context: FrozenModelCallMemoryContext = field(
        default_factory=lambda: FrozenModelCallMemoryContext(
            FrozenModelVisibleMemoryProvenance(
                ModelVisibleMemoryProvenanceDisposition.COMPLETE, ()
            )
        ),
        repr=False,
    )

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
            or self.wire_input_plan.context_id != self.compiled_input.context_id
            or self.wire_input_plan.compiled_semantic_fingerprint
            != self.compiled_input.compiled_semantic_fingerprint
            or self.wire_input_plan.message_placements_fingerprint
            != compiled_message_placements_fingerprint(
                self.compiled_input.message_placements
            )
            or self.wire_input_plan.resolved_target_semantic_fingerprint
            != self.prepared_call.call.target.fact.target_fingerprint
            or self.wire_input_plan.materialization.tool_items
            != tuple(
                item.wire_tool
                for item in self.prepared_call.native_projection_set.projections
            )
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


@dataclass(frozen=True, slots=True)
class CompletedProviderModelExecution:
    terminal: ProviderStreamTerminal
    replay_payload: ProviderAdapterCompletedReplayPayload | None = field(
        repr=False
    )
    replay_target: ProviderReplayTargetCompatibilityFact

    def bind_assistant_entry(
        self,
        *,
        assistant_entry_id: str,
        public_projection_fingerprint: str,
        has_tool_calls: bool,
    ) -> ProviderAssistantReplayFragment | None:
        """Compatibility helper for retained process-local contract tests."""

        del has_tool_calls
        payload = self.replay_payload
        if payload is None:
            return None
        if (
            _completed_replay_public_projection_fingerprint(payload)
            != public_projection_fingerprint
        ):
            raise RuntimeError(
                "completed provider replay differs from its public projection"
            )
        return build_prepared_durable_provider_assistant_replay(
            session_id="process-local-replay",
            workspace_id="process-local-replay",
            assistant_entry_id=assistant_entry_id,
            target=self.replay_target,
            public_projection_fingerprint=public_projection_fingerprint,
            ordered_items=payload.ordered_items,
        ).fragment()

    def bind_durable_assistant_entry(
        self,
        *,
        session_id: str,
        workspace_id: str,
        assistant_entry_id: str,
        public_projection_fingerprint: str,
    ) -> tuple[
        ProviderReplayDisposition,
        PreparedDurableProviderAssistantReplay | None,
    ]:
        payload = self.replay_payload
        if payload is None:
            if self.replay_target.wire_api == "openai_responses":
                raise RuntimeError("Responses completion lacks required native replay")
            return ProviderReplayDisposition.PUBLIC_SEMANTIC_ONLY, None
        if (
            _completed_replay_public_projection_fingerprint(payload)
            != public_projection_fingerprint
        ):
            raise RuntimeError(
                "completed provider replay differs from its public projection"
            )
        return (
            ProviderReplayDisposition.NATIVE_REPLAY,
            build_prepared_durable_provider_assistant_replay(
                session_id=session_id,
                workspace_id=workspace_id,
                assistant_entry_id=assistant_entry_id,
                target=self.replay_target,
                public_projection_fingerprint=public_projection_fingerprint,
                ordered_items=payload.ordered_items,
            ),
        )


class PreparedKernelModelExecution:
    """Transport-bearing one-shot produced without opening the transport."""

    def __init__(
        self,
        *,
        request: KernelModelExecutionRequest,
        final_context: LLMContext,
        append_candidate: PreparedProviderInputAppendCandidate,
        transport_timeout_policy_fingerprint: str,
        install_authority: ProcessLocalProviderInputInstallAuthority,
        usage_observer: Callable[
            [KernelModelExecutionRequest, TransportUsageReport], None
        ]
        | None,
    ) -> None:
        self.request = request
        self.final_context = final_context
        self.append_candidate = append_candidate
        self._transport_timeout_policy_fingerprint = (
            transport_timeout_policy_fingerprint
        )
        self._install_authority = install_authority
        self._usage_observer = usage_observer
        self._completed: CompletedProviderModelExecution | None = None
        self._completed_taken = False
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

    def take_completed_result_once(self) -> CompletedProviderModelExecution:
        with self._lock:
            if (
                self._state is not _PreparedExecutionState.PHYSICALLY_CLOSED
                or self._completed is None
                or self._completed_taken
            ):
                raise RuntimeError("completed provider execution is unavailable")
            self._completed_taken = True
            return self._completed

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
            or permit.epoch_nonce != self.append_candidate.epoch_nonce
            or permit.epoch_revision
            != self.append_candidate.expected_epoch_revision + 1
            or request.prepared_call.transport_timeout_policy_fingerprint
            != self._transport_timeout_policy_fingerprint
        ):
            raise RuntimeError("provider-input install permit does not exact-join")
        self._install_authority.consume(
            permit,
            candidate=self.append_candidate,
            execution=self,
        )
        with self._lock:
            if self._state is not _PreparedExecutionState.PREFLIGHTED:
                raise RuntimeError("prepared model execution is not openable")
            self._state = _PreparedExecutionState.OPENING
        for tool in request.compiled_input.tools:
            binding = request.surface_borrow.execution_binding(tool.name)
            if binding.descriptor_fingerprint != tool.descriptor_fingerprint:
                with self._lock:
                    self._state = _PreparedExecutionState.DISCARDED
                raise RuntimeError("prepared tool binding was revoked before open")
        call = request.prepared_call.call
        execution = call.target.transport.open_stream(
            call=call, context=self.final_context
        )
        with self._lock:
            self._state = _PreparedExecutionState.STREAMING
        semantic_error: BaseException | None = None
        try:
            while True:
                item = await execution.read_next()
                if item is None:
                    break
                if isinstance(item, ProviderStreamTerminal):
                    if self._usage_observer is not None:
                        try:
                            self._usage_observer(request, item.usage)
                        except Exception:
                            pass
                    if item.terminal_kind is ProviderNormalizedTerminalKind.COMPLETED:
                        profile = call.target.model_profile.provider_profile
                        if (
                            profile.assistant_replay_codec_kind
                            is ProviderAssistantReplayCodecKind.RESPONSES_EXACT_OUTPUT_ITEMS
                            and item.completed_replay_payload is None
                        ):
                            semantic_error = RuntimeError(
                                "completed provider response lacks required replay"
                            )
                        elif (
                            item.completed_replay_payload is not None
                            and item.completed_replay_payload.codec_kind
                            is not profile.assistant_replay_codec_kind
                        ):
                            semantic_error = RuntimeError(
                                "completed provider replay codec drifted"
                            )
                        else:
                            self._completed = CompletedProviderModelExecution(
                                terminal=item,
                                replay_payload=item.completed_replay_payload,
                                replay_target=(
                                    build_provider_replay_target_compatibility(
                                        wire_api=profile.wire_api,
                                        endpoint_identity_fingerprint=(
                                            call.target.fact.endpoint_fingerprint
                                        ),
                                        normalized_model_identifier=(
                                            call.target.fact.model_id
                                        ),
                                        transport_binding_id=(
                                            call.target.fact.transport_binding_id
                                        ),
                                    )
                                ),
                            )
                    elif item.terminal_kind is (
                        ProviderNormalizedTerminalKind.OUTPUT_INCOMPLETE
                    ):
                        assert item.incomplete_reason is not None
                        semantic_error = ProviderModelOutputIncomplete(
                            item.incomplete_reason
                        )
                    else:
                        assert item.error is not None
                        semantic_error = ProviderModelExecutionFailed(item.error)
                    break
                yield item
        finally:
            await execution.aclose()
            completion = await execution.wait_physical_completion()
            with self._lock:
                self._state = _PreparedExecutionState.PHYSICALLY_CLOSED
            if completion.status is not ProviderPhysicalCompletionStatus.COMPLETED:
                raise RuntimeError("provider physical operation did not exit")
        if semantic_error is not None:
            raise semantic_error
        if self._completed is None:
            raise RuntimeError("provider execution ended without completed terminal")


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
        timeout_policy: OpenAITransportTimeoutPolicy | None = None,
        api_key_boundary: ProcessApiKeyBoundary,
    ) -> None:
        transport_timeout = (
            timeout_policy or DEFAULT_KERNEL_WATCHDOG_POLICY.foreground_transport
        )
        if transport_timeout.total_seconds is not None:
            raise ValueError(
                "foreground provider transport must not have a total response timeout"
            )
        registry = NormalizedLLMTransportRegistry()
        registry.register(
            NormalizedLLMTransport(
                OpenAIResponsesTransport(
                    api_key=config.api_key,
                    timeout_policy=transport_timeout,
                    api_key_boundary=api_key_boundary,
                    retry_config=config.retry,
                    openai_sdk_max_retries=config.openai_sdk_max_retries,
                )
            )
        )
        registry.register(
            NormalizedLLMTransport(
                OpenAIChatCompletionsTransport(
                    api_key=config.api_key,
                    timeout_policy=transport_timeout,
                    api_key_boundary=api_key_boundary,
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
        self._transport_timeout_policy_fingerprint = (
            transport_timeout.policy_fingerprint
        )

    def prepare_target(
        self, request: KernelModelTargetPreparationRequest
    ) -> PreparedKernelModelTarget:
        if request.purpose is not ModelCallPurpose.AGENT_MODEL_LOOP:
            raise ValueError("foreground model preparation purpose is invalid")
        if (
            request.model_call_index < 1
            or request.maximum_output_tokens < 1
            or (
                request.maximum_input_tokens is not None
                and request.maximum_input_tokens < 1
            )
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
        input_budget = target.context_budget.input_budget_tokens
        if request.maximum_input_tokens is not None:
            input_budget = min(request.maximum_input_tokens, input_budget)
        native_contract = openai_native_function_tool_contract_fingerprint(
            target.model_profile.provider_profile.wire_api
        )
        return PreparedKernelModelTarget(
            session_id=request.session_id,
            turn_id=request.turn_id,
            model_call_index=request.model_call_index,
            purpose=request.purpose,
            target=target,
            call=call,
            maximum_input_tokens=input_budget,
            maximum_output_tokens=request.maximum_output_tokens,
            effective_input_budget_tokens=input_budget,
            native_function_tool_wire_contract_fingerprint=native_contract,
            transport_timeout_policy_fingerprint=(
                self._transport_timeout_policy_fingerprint
            ),
        )

    def resolve_compaction_summary_call(
        self,
        *,
        active_prepared_call: (
            PreparedKernelModelCall | PreparedKernelSemanticModelCall | None
        ) = None,
    ) -> ResolvedModelCall:
        """Resolve the current primary target once for a sealed summary call."""

        if active_prepared_call is not None:
            target = active_prepared_call.call.target
        else:
            target = resolve_model_target(
                config=self._config,
                registry=self._registry,
                role=self._role,
                requested_options=self._options,
            )
        return resolve_model_call(
            target=target,
            purpose=ModelCallPurpose.CONTEXT_COMPACTION_SUMMARY,
            resolved_model_call_id=f"model_call:{uuid4().hex}",
        )

    @staticmethod
    def freeze_native_tool_eligibility(
        *,
        prepared_target: PreparedKernelModelTarget,
        conversation_scope_kind: ModelInputScopeKind,
        scope_subagent_task_id: str | None,
        tool_facts: tuple[FrozenToolCapabilityFact, ...],
        retained_direct_inputs: tuple[
            tuple[ToolCapabilityVersionRef, FrozenToolSpec], ...
        ] = (),
        deadline_monotonic: float | None = None,
    ) -> FrozenNativeToolWireEligibilitySet:
        return freeze_openai_native_tool_eligibility(
            conversation_scope_kind=conversation_scope_kind,
            scope_subagent_task_id=scope_subagent_task_id,
            wire_api=(
                prepared_target.target.model_profile.provider_profile.wire_api
            ),
            tool_facts=tool_facts,
            retained_direct_inputs=retained_direct_inputs,
            deadline_monotonic=deadline_monotonic,
        )

    @staticmethod
    def materialize_native_tool_projection_set(
        *,
        prepared_target: PreparedKernelModelTarget,
        conversation_scope_kind: ModelInputScopeKind,
        scope_subagent_task_id: str | None,
        tool_versions: tuple[ToolCapabilityVersionRef, ...],
        tool_specs: tuple[FrozenToolSpec, ...],
        eligibility: FrozenNativeToolWireEligibilitySet,
        deadline_monotonic: float | None = None,
    ) -> FrozenNativeToolProjectionSet:
        return materialize_openai_native_tool_projection_set(
            conversation_scope_kind=conversation_scope_kind,
            scope_subagent_task_id=scope_subagent_task_id,
            wire_api=(
                prepared_target.target.model_profile.provider_profile.wire_api
            ),
            tool_versions=tool_versions,
            tool_specs=tool_specs,
            eligibility=eligibility,
            deadline_monotonic=deadline_monotonic,
        )

    def bind_tool_surface(
        self,
        *,
        prepared_target: PreparedKernelModelTarget,
        tool_surface: PreparedKernelToolSurface,
        native_projection_set: FrozenNativeToolProjectionSet,
    ) -> PreparedKernelModelCall:
        if (
            prepared_target.transport_timeout_policy_fingerprint
            != self._transport_timeout_policy_fingerprint
        ):
            raise ValueError("prepared model target belongs to another adapter")
        surface = tool_surface.model_surface
        if surface.tool_specs and not prepared_target.target.fact.supports_tools:
            raise ValueError("resolved model target does not support prepared tools")
        if (
            native_projection_set.native_function_tool_wire_contract_fingerprint
            != prepared_target.native_function_tool_wire_contract_fingerprint
            or native_projection_set.conversation_scope_kind
            is not surface.conversation_scope_kind
        ):
            raise ValueError("native projection set does not join resolved target")
        call = prepared_target.call
        target = prepared_target.target
        input_budget = prepared_target.effective_input_budget_tokens
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
        return PreparedKernelModelCall(
            session_id=prepared_target.session_id,
            turn_id=prepared_target.turn_id,
            model_call_index=prepared_target.model_call_index,
            call=call,
            tool_surface=tool_surface,
            native_projection_set=native_projection_set,
            compile_binding=compile_binding,
            transport_timeout_policy_fingerprint=(
                self._transport_timeout_policy_fingerprint
            ),
        )

    def bind_semantic_tool_surface(
        self,
        *,
        prepared_target: PreparedKernelModelTarget,
        tool_surface: FrozenModelToolSurface,
        native_projection_set: FrozenNativeToolProjectionSet,
    ) -> PreparedKernelSemanticModelCall:
        """Bind schemas for read-only source compilation without an executor."""

        if (
            prepared_target.transport_timeout_policy_fingerprint
            != self._transport_timeout_policy_fingerprint
        ):
            raise ValueError("prepared model target belongs to another adapter")
        if tool_surface.tool_specs and not prepared_target.target.fact.supports_tools:
            raise ValueError("resolved model target does not support prepared tools")
        if (
            native_projection_set.native_function_tool_wire_contract_fingerprint
            != prepared_target.native_function_tool_wire_contract_fingerprint
            or native_projection_set.conversation_scope_kind
            is not tool_surface.conversation_scope_kind
        ):
            raise ValueError("native projection set does not join resolved target")
        call = prepared_target.call
        target = prepared_target.target
        estimator_fingerprint = target.token_estimator.fact.estimator_fingerprint
        binding_fingerprint = model_input_compile_binding_fingerprint(
            call_fact=call.fact,
            target_fact=target.fact,
            estimator_fingerprint=estimator_fingerprint,
            effective_input_budget_tokens=(
                prepared_target.effective_input_budget_tokens
            ),
            effective_output_tokens=target.context_budget.effective_output_tokens,
            tool_surface=tool_surface,
        )
        binding = ModelInputCompileBinding(
            call_fact=call.fact,
            target_fact=target.fact,
            estimator=target.token_estimator,
            estimator_fingerprint=estimator_fingerprint,
            effective_input_budget_tokens=(
                prepared_target.effective_input_budget_tokens
            ),
            effective_output_tokens=target.context_budget.effective_output_tokens,
            tool_surface=tool_surface,
            binding_fingerprint=binding_fingerprint,
        )
        return PreparedKernelSemanticModelCall(
            session_id=prepared_target.session_id,
            turn_id=prepared_target.turn_id,
            model_call_index=prepared_target.model_call_index,
            call=call,
            native_projection_set=native_projection_set,
            compile_binding=binding,
        )

    def preflight_execution(
        self,
        request: KernelModelExecutionRequest,
        *,
        append_candidate: PreparedProviderInputAppendCandidate,
        install_authority: ProcessLocalProviderInputInstallAuthority,
    ) -> PreparedKernelModelExecution:
        if type(install_authority) is not ProcessLocalProviderInputInstallAuthority:
            raise TypeError("provider-input install authority is invalid")
        prepared = request.prepared_call
        compiled = request.compiled_input
        plan = request.wire_input_plan
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
            or prepared.transport_timeout_policy_fingerprint
            != self._transport_timeout_policy_fingerprint
            or plan.compiled_semantic_fingerprint
            != compiled.compiled_semantic_fingerprint
            or plan.message_placements_fingerprint
            != compiled_message_placements_fingerprint(compiled.message_placements)
            or plan.resolved_target_semantic_fingerprint
            != prepared.call.target.fact.target_fingerprint
            or plan.provider_profile_fingerprint
            != _provider_wire_profile_fingerprint(prepared.call)
            or plan.materialization.tool_items
            != tuple(
                item.wire_tool for item in prepared.native_projection_set.projections
            )
            or plan.quote.estimator_fingerprint
            != prepared.compile_binding.estimator_fingerprint
            or plan.quote.effective_input_budget_tokens
            != prepared.compile_binding.effective_input_budget_tokens
        ):
            raise ValueError("compiled model input does not exact-join preparation")
        install_authority.require_registered_plan(
            candidate=append_candidate,
            wire_input_plan=plan,
            tool_exposure_plan=append_candidate.tool_exposure_plan,
        )
        if (
            prepared.tool_surface.capability_exposure_plan
            is not append_candidate.tool_exposure_plan
            or prepared.native_projection_set
            is not append_candidate.direct_native_projection_set
        ):
            raise ValueError("model execution capability plan does not exact-join")
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
            binding = request.surface_borrow.execution_binding(tool.name)
            if binding.descriptor_fingerprint != tool.descriptor_fingerprint:
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
            provider_wire_input_plan=plan,
        )
        validated = validate_model_context_for_call(call=call, context=context)
        if validated.estimate != compiled.final_estimate:
            raise RuntimeError("compiler and pre-send input estimates differ")
        return PreparedKernelModelExecution(
            request=request,
            final_context=context,
            append_candidate=append_candidate,
            transport_timeout_policy_fingerprint=(
                self._transport_timeout_policy_fingerprint
            ),
            install_authority=install_authority,
            usage_observer=self._usage_observer,
        )

    def plan_wire_input(
        self,
        *,
        prepared_call: PreparedKernelModelCall,
        compiled_input: FrozenCompiledModelInput,
        predecessor_view: FrozenProviderInputEpochView | None,
        replay_hydration: FrozenSelectedDurableProviderReplayHydration | None = None,
    ) -> FrozenProviderWireInputPlan:
        """Purely freeze the exact provider wire subtree before preflight."""

        return _plan_provider_wire_input(
            call=prepared_call.call,
            binding=prepared_call.compile_binding,
            native_projection_set=prepared_call.native_projection_set,
            compiled_input=compiled_input,
            predecessor_view=predecessor_view,
            replay_hydration=replay_hydration,
        )

    @staticmethod
    def plan_compaction_wire_input(
        *,
        summary_call: ResolvedModelCall,
        compile_binding: ModelInputCompileBinding,
        native_projection_set: FrozenNativeToolProjectionSet,
        compiled_input: FrozenCompiledModelInput,
        predecessor_view: FrozenProviderInputEpochView | None,
        replay_hydration: FrozenSelectedDurableProviderReplayHydration | None,
    ) -> FrozenProviderWireInputPlan:
        if (
            summary_call.fact.purpose
            is not ModelCallPurpose.CONTEXT_COMPACTION_SUMMARY
        ):
            raise ValueError("compaction wire planning purpose is invalid")
        return _plan_provider_wire_input(
            call=summary_call,
            binding=compile_binding,
            native_projection_set=native_projection_set,
            compiled_input=compiled_input,
            predecessor_view=predecessor_view,
            replay_hydration=replay_hydration,
        )

    @staticmethod
    def replay_target(
        prepared_call: PreparedKernelModelCall,
    ) -> ProviderReplayTargetCompatibilityFact:
        return DirectKernelModelPort.replay_target_for_resolved_call(
            prepared_call.call
        )

    @staticmethod
    def replay_target_for_resolved_call(
        call: ResolvedModelCall,
    ) -> ProviderReplayTargetCompatibilityFact:
        profile = call.target.model_profile.provider_profile
        return build_provider_replay_target_compatibility(
            wire_api=profile.wire_api,
            endpoint_identity_fingerprint=call.target.fact.endpoint_fingerprint,
            normalized_model_identifier=call.target.fact.model_id,
            transport_binding_id=call.target.fact.transport_binding_id,
        )


def _provider_wire_profile_fingerprint(call: ResolvedModelCall) -> str:
    profile = call.target.model_profile.provider_profile
    return context_fingerprint(
        "pulsara.provider-wire-profile:v1",
        {
            "profile_id": profile.id,
            "wire_api": profile.wire_api,
            "target_request_shape": (
                call.target.fact.provider_request_shape_fingerprint
            ),
            "assistant_replay": profile.assistant_replay_contract_fingerprint,
            "function_tools": openai_native_function_tool_contract_fingerprint(
                profile.wire_api
            ),
        },
    )


def _completed_replay_public_projection_fingerprint(
    payload: ProviderAdapterCompletedReplayPayload,
) -> str:
    values = tuple(thaw_json(item) for item in payload.ordered_items)
    text_parts: list[str] = []
    calls: list[LLMToolCall] = []
    ordered_blocks: list[tuple[object, ...]] = []
    if payload.codec_kind.value.startswith("CHAT_"):
        if len(values) != 1 or not isinstance(values[0], dict):
            raise RuntimeError("Chat replay payload has an invalid message shape")
        message = values[0]
        if message.get("role") != "assistant":
            raise RuntimeError("Chat replay payload changed the assistant role")
        content = message.get("content")
        if content is not None:
            if not isinstance(content, str):
                raise RuntimeError("Chat replay content is not text")
            text_parts.append(content)
            if content:
                ordered_blocks.append(("TEXT", content))
        raw_calls = message.get("tool_calls", ())
        if not isinstance(raw_calls, (list, tuple)):
            raise RuntimeError("Chat replay tool calls are not an array")
        for raw_call in raw_calls:
            if not isinstance(raw_call, dict) or raw_call.get("type") != "function":
                raise RuntimeError("Chat replay tool call has an invalid shape")
            function = raw_call.get("function")
            if not isinstance(function, dict):
                raise RuntimeError("Chat replay function is absent")
            call = _replay_public_tool_call(
                call_id=raw_call.get("id"),
                name=function.get("name"),
                arguments=function.get("arguments"),
            )
            calls.append(call)
            ordered_blocks.append(
                ("TOOL_CALL", call.id, call.name, call.arguments)
            )
    elif payload.codec_kind.value == "RESPONSES_EXACT_OUTPUT_ITEMS":
        for item in values:
            if not isinstance(item, dict):
                raise RuntimeError("Responses replay item is not an object")
            item_type = item.get("type")
            if item_type == "reasoning":
                continue
            if item_type == "message":
                content = item.get("content")
                if not isinstance(content, (list, tuple)):
                    raise RuntimeError("Responses replay message content is invalid")
                message_text_parts: list[str] = []
                for block in content:
                    if not isinstance(block, dict) or block.get("type") not in {
                        "output_text",
                        "text",
                    }:
                        raise RuntimeError(
                            "Responses replay message block is unsupported"
                        )
                    value = block.get("text")
                    if not isinstance(value, str):
                        raise RuntimeError("Responses replay message text is invalid")
                    text_parts.append(value)
                    message_text_parts.append(value)
                message_text = "".join(message_text_parts)
                if message_text:
                    ordered_blocks.append(("TEXT", message_text))
                continue
            if item_type == "function_call":
                call = _replay_public_tool_call(
                    call_id=item.get("call_id"),
                    name=item.get("name"),
                    arguments=item.get("arguments"),
                )
                calls.append(call)
                ordered_blocks.append(
                    ("TOOL_CALL", call.id, call.name, call.arguments)
                )
                continue
            raise RuntimeError("Responses replay item is unsupported")
    else:  # pragma: no cover - payload DTO rejects NONE and enum is closed
        raise RuntimeError("provider replay codec is unsupported")
    return provider_assistant_public_projection_fingerprint(
        text="".join(text_parts),
        tool_calls=tuple(calls),
        ordered_blocks=tuple(ordered_blocks),
    )


def _replay_public_tool_call(
    *, call_id: object, name: object, arguments: object
) -> LLMToolCall:
    if (
        not isinstance(call_id, str)
        or not call_id
        or not isinstance(name, str)
        or not name
        or not isinstance(arguments, str)
    ):
        raise RuntimeError("provider replay tool-call identity is invalid")
    try:
        parsed = json.loads(arguments)
    except json.JSONDecodeError as exc:
        raise RuntimeError("provider replay tool arguments are invalid") from exc
    if not isinstance(parsed, dict):
        raise RuntimeError("provider replay tool arguments are not an object")
    return LLMToolCall(
        id=call_id,
        name=name,
        arguments=canonical_json_bytes(parsed).decode("utf-8"),
    )


def _freeze_wire_object(value: dict[str, object]) -> FrozenJsonObjectFact:
    frozen = freeze_json(value)
    if not isinstance(frozen, FrozenJsonObjectFact):
        raise TypeError("provider wire value did not freeze to an object")
    return frozen


def _semantic_wire_groups(
    *,
    call: ResolvedModelCall,
    compiled_input: FrozenCompiledModelInput,
    native_projection_set: FrozenNativeToolProjectionSet,
) -> tuple[tuple[tuple[dict[str, object], ...], ...], tuple[dict[str, object], ...]]:
    profile = call.target.model_profile.provider_profile
    if profile.wire_api == "openai_chat_completions":
        groups = tuple(
            tuple(chat_semantic_wire_group(item, provider_profile=profile))
            for item in compiled_input.messages
        )
    elif profile.wire_api == "openai_responses":
        groups = tuple(
            tuple(responses_semantic_wire_group(item))
            for item in compiled_input.messages
        )
    else:  # pragma: no cover - resolved transport registry is closed
        raise ValueError("provider wire API is unsupported")
    expected_contract = openai_native_function_tool_contract_fingerprint(
        profile.wire_api
    )
    if (
        native_projection_set.native_function_tool_wire_contract_fingerprint
        != expected_contract
        or tuple(item.name for item in compiled_input.tools)
        != tuple(item.provider_name for item in native_projection_set.tool_versions)
    ):
        raise ValueError("native tool projection set does not join compiled tools")
    tools = tuple(thaw_json(item.wire_tool) for item in native_projection_set.projections)
    if any(not isinstance(item, dict) for item in tools):
        raise TypeError("native tool projection did not thaw to an object")
    if any(not group for group in groups):
        raise ValueError("compiled message lowered to an empty provider wire group")
    return groups, tools


def _plan_provider_wire_input(
    *,
    call: ResolvedModelCall,
    binding: ModelInputCompileBinding,
    native_projection_set: FrozenNativeToolProjectionSet,
    compiled_input: FrozenCompiledModelInput,
    predecessor_view: FrozenProviderInputEpochView | None,
    replay_hydration: FrozenSelectedDurableProviderReplayHydration | None,
) -> FrozenProviderWireInputPlan:
    del predecessor_view
    if (
        compiled_input.compile_binding_fingerprint != binding.binding_fingerprint
        or compiled_input.tools != binding.tool_surface.tool_specs
        or call.target.fact != binding.target_fact
    ):
        raise ValueError("provider wire planning input does not join preparation")
    generic_groups, wire_tools = _semantic_wire_groups(
        call=call,
        compiled_input=compiled_input,
        native_projection_set=native_projection_set,
    )
    profile = call.target.model_profile.provider_profile
    profile_fingerprint = _provider_wire_profile_fingerprint(call)
    replay_target = DirectKernelModelPort.replay_target_for_resolved_call(call)
    fragments = () if replay_hydration is None else replay_hydration.fragments
    if replay_hydration is not None:
        identity = compiled_input.canonical_input_identity
        if (
            replay_hydration.scope.session_id != identity.session_id
            or replay_hydration.scope.scope_kind
            is not identity.conversation_scope_kind
            or replay_hydration.scope.scope_subagent_task_id
            != identity.scope_subagent_task_id
            or replay_hydration.replay_target_fingerprint
            != replay_target.replay_target_fingerprint
        ):
            raise ValueError("provider replay hydration does not join wire plan")
    fragment_by_entry = {item.assistant_entry_id: item for item in fragments}
    if len(fragment_by_entry) != len(fragments):
        raise ValueError("provider replay fragments are duplicated")

    replacements: list[FrozenProviderWireReplacementIdentity] = []
    final_items: list[dict[str, object]] = []
    used_entries: set[str] = set()
    semantic_message_bytes = sum(
        sum(len(canonical_json_bytes(item)) for item in group)
        for group in generic_groups
    )
    debit_bytes = 0
    addend_bytes = 0
    debit_tokens = 0
    addend_tokens = 0
    index = 0
    while index < len(compiled_input.messages):
        placement = compiled_input.message_placements[index]
        entry_id = placement.origin_entry_id
        fragment = None if entry_id is None else fragment_by_entry.get(entry_id)
        if fragment is None:
            final_items.extend(generic_groups[index])
            index += 1
            continue
        if entry_id in used_entries:
            raise ValueError("provider replay fragment matched more than one group")
        end = index + 1
        while (
            end < len(compiled_input.message_placements)
            and compiled_input.message_placements[end].origin_entry_id == entry_id
        ):
            end += 1
        placements = compiled_input.message_placements[index:end]
        if tuple(item.within_origin_ordinal for item in placements) != tuple(
            range(len(placements))
        ):
            raise ValueError("provider replay placement group is not contiguous")
        messages = compiled_input.messages[index:end]
        if len(messages) != 1:
            raise ValueError("provider replay currently requires one assistant message")
        message = messages[0]
        if (
            provider_assistant_message_public_projection_fingerprint(message)
            != fragment.public_projection_fingerprint
            or fragment.replay_target_fingerprint
            != replay_target.replay_target_fingerprint
            or fragment.codec_kind is not replay_target.codec_kind
            or fragment.provider_replay_contract_fingerprint
            != replay_target.provider_replay_contract_fingerprint
        ):
            raise ValueError("provider replay fragment does not exact-join input")
        generic = tuple(
            item for ordinal in range(index, end) for item in generic_groups[ordinal]
        )
        replacement = tuple(thaw_json(item) for item in fragment.ordered_items)
        if any(not isinstance(item, dict) for item in replacement):
            raise TypeError("provider replay item did not thaw to an object")
        generic_bytes = sum(len(canonical_json_bytes(item)) for item in generic)
        replay_bytes = sum(len(canonical_json_bytes(item)) for item in replacement)
        generic_tokens = sum(
            compiled_input.final_estimate.message_tokens_by_index[index:end]
        )
        replay_tokens = binding.estimator.estimate_json(replacement)
        generic_fingerprint = context_fingerprint(
            "pulsara.provider-wire-generic-message-group:v1", generic
        )
        replacement_fingerprint = context_fingerprint(
            "pulsara.provider-wire-replacement:v1", replacement
        )
        replacements.append(
            FrozenProviderWireReplacementIdentity(
                assistant_entry_id=entry_id or "",
                first_message_ordinal=index,
                message_count=end - index,
                generic_message_group_fingerprint=generic_fingerprint,
                replay_fragment_fingerprint=fragment.fragment_fingerprint,
                replacement_wire_fingerprint=replacement_fingerprint,
                semantic_debit_utf8_bytes=generic_bytes,
                replay_addend_utf8_bytes=replay_bytes,
                semantic_debit_tokens=generic_tokens,
                replay_addend_tokens=replay_tokens,
            )
        )
        final_items.extend(replacement)  # type: ignore[arg-type]
        used_entries.add(entry_id or "")
        debit_bytes += generic_bytes
        addend_bytes += replay_bytes
        debit_tokens += generic_tokens
        addend_tokens += replay_tokens
        index = end
    if used_entries != set(fragment_by_entry):
        raise ValueError("an installed provider replay fragment was omitted")
    hydration_fingerprint = (
        None
        if replay_hydration is None
        else replay_hydration.hydration_fingerprint
    )
    if replay_hydration is not None:
        replay_placements = tuple(
            item
            for item in compiled_input.message_placements
            if item.origin_entry_id in used_entries
        )
        if (
            selected_message_placements_fingerprint(replay_placements)
            != replay_hydration.selected_message_placements_fingerprint
            or tuple(item.assistant_entry_id for item in replay_hydration.fragments)
            != tuple(item.assistant_entry_id for item in replacements)
        ):
            raise ValueError("provider replay hydration placements drifted")

    root_value = freeze_json(compose_provider_root_policy(compiled_input.system_prompt))
    frozen_tools = tuple(_freeze_wire_object(item) for item in wire_tools)
    frozen_inputs = tuple(_freeze_wire_object(item) for item in final_items)
    root_plain = thaw_json(root_value)
    tools_plain = tuple(thaw_json(item) for item in frozen_tools)
    inputs_plain = tuple(thaw_json(item) for item in frozen_inputs)
    materialization = FrozenProviderWireMaterialization(
        root_policy_value=root_value,
        tool_items=frozen_tools,
        ordered_input_items=frozen_inputs,
    )
    final_message_tokens = (
        compiled_input.final_estimate.message_tokens - debit_tokens + addend_tokens
    )
    final_total_tokens = (
        compiled_input.final_estimate.total_input_tokens
        - debit_tokens
        + addend_tokens
    )
    final_message_bytes = semantic_message_bytes - debit_bytes + addend_bytes
    final_wire_bytes = len(
        canonical_json_bytes(
            {"root": root_plain, "tools": tools_plain, "input": inputs_plain}
        )
    )
    quote = FrozenProviderWireInputQuote(
        estimator_fingerprint=binding.estimator_fingerprint,
        effective_input_budget_tokens=binding.effective_input_budget_tokens,
        semantic_total_input_tokens=compiled_input.final_estimate.total_input_tokens,
        semantic_message_tokens=compiled_input.final_estimate.message_tokens,
        semantic_message_utf8_bytes=semantic_message_bytes,
        replaced_semantic_debit_tokens=debit_tokens,
        replay_addend_tokens=addend_tokens,
        replaced_semantic_debit_utf8_bytes=debit_bytes,
        replay_addend_utf8_bytes=addend_bytes,
        final_message_tokens=final_message_tokens,
        final_total_input_tokens=final_total_tokens,
        final_message_utf8_bytes=final_message_bytes,
        final_wire_utf8_bytes=final_wire_bytes,
        quote_contract_version="pulsara.provider-wire-input-quote.v1",
    )
    wire_system = context_fingerprint(
        "pulsara.provider-wire-system:v1", root_plain
    )
    wire_tools_fingerprint = context_fingerprint(
        "pulsara.provider-wire-tools:v1", tools_plain
    )
    wire_input = context_fingerprint(
        "pulsara.provider-wire-input-prefix:v1",
        {
            "api": profile.wire_api,
            "profile": profile_fingerprint,
            "root": root_plain,
            "tools": tools_plain,
            "input": inputs_plain,
        },
    )
    return FrozenProviderWireInputPlan(
        context_id=compiled_input.context_id,
        compiled_semantic_fingerprint=compiled_input.compiled_semantic_fingerprint,
        message_placements_fingerprint=(
            compiled_message_placements_fingerprint(
                compiled_input.message_placements
            )
        ),
        wire_api=profile.wire_api,
        provider_profile_fingerprint=profile_fingerprint,
        resolved_target_semantic_fingerprint=call.target.fact.target_fingerprint,
        materialization=materialization,
        replacements=tuple(replacements),
        provider_replay_hydration_fingerprint=hydration_fingerprint,
        wire_system_fingerprint=wire_system,
        wire_tools_fingerprint=wire_tools_fingerprint,
        wire_input_prefix_fingerprint=wire_input,
        quote=quote,
    )


__all__ = [
    "DirectKernelModelPort",
    "KernelModelExecutionRequest",
    "KernelModelPreparationRequest",
    "PreparedKernelModelExecution",
    "PreparedKernelModelCall",
    "PreparedKernelSemanticModelCall",
]
