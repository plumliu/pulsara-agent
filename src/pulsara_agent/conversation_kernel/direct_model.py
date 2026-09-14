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
    chat_semantic_wire_group,
    materialize_chat_context_bearing_wire_projection,
)
from pulsara_agent.llm.adapters.openai.function_tools import (
    freeze_openai_native_tool_eligibility,
    materialize_openai_native_tool_projection_set,
    openai_native_function_tool_contract_fingerprint,
)
from pulsara_agent.llm.adapters.openai.responses import (
    materialize_responses_context_bearing_wire_projection,
    responses_semantic_wire_group,
)
from pulsara_agent.llm.input import LLMMessage, LLMToolCall, MessageRole, ToolSpec
from pulsara_agent.llm.model_connections import ModelCallBinding
from pulsara_agent.llm.model_target import default_reasoning_selection
from pulsara_agent.llm.runtime import ModelRuntime
from pulsara_agent.llm.provider import ProviderAssistantReplayCodecKind
from pulsara_agent.llm.provider import mutable_provider_value
from pulsara_agent.llm.provider_replay import (
    PreparedDurableProviderAssistantReplay,
    ProviderAssistantReplayFragment,
    ProviderReplayDisposition,
    ProviderReplayTargetCompatibilityFact,
    build_prepared_durable_provider_assistant_replay,
    build_provider_replay_target_compatibility,
)
from pulsara_agent.llm.request import (
    FrozenProviderWireInputPlan,
    FrozenProviderWireInputQuote,
    FrozenProviderWireMaterialization,
    FrozenProviderWireReplacementIdentity,
    LLMContext,
    provider_assistant_public_projection_fingerprint,
    provider_assistant_message_public_projection_fingerprint,
)
from pulsara_agent.llm.resolution import (
    ResolvedModelCall,
    ResolvedModelTarget,
    resolve_model_call,
)
from pulsara_agent.llm.result import TransportUsageReport
from pulsara_agent.llm.user_carrier import compose_provider_root_policy
from pulsara_agent.llm.validation import (
    validate_model_context_for_call,
    validate_model_message_content_for_call,
)
from pulsara_agent.model_input.contracts import (
    FrozenCompiledModelInput,
    FrozenModelToolSurface,
    FrozenToolSpec,
    ModelInputScopeKind,
    ModelInputCompileBinding,
    ProviderWireSemanticInput,
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
    binding: ModelCallBinding = field(repr=False)


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
            self.target.model_profile.route_wire_profile.wire_api
        )
        if self.native_function_tool_wire_contract_fingerprint != expected_contract:
            raise ValueError("prepared model target native contract drifted")


@dataclass(frozen=True, slots=True)
class KernelModelPreparationRequest:
    """Direct component-test request carrying the same exact model binding.

    Production dispatch uses ``prepare_target`` followed by ``bind_tool_surface``
    so native eligibility is frozen before the parent capability cut.
    """

    session_id: str
    turn_id: str
    model_call_index: int
    purpose: ModelCallPurpose
    maximum_input_tokens: int
    maximum_output_tokens: int
    binding: ModelCallBinding = field(repr=False)
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
                item.provider_name for item in self.native_projection_set.tool_versions
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
            or not self.surface_borrow.exactly_joins(self.prepared_call.tool_surface)
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
    replay_payload: ProviderAdapterCompletedReplayPayload | None = field(repr=False)
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


@dataclass(frozen=True, slots=True)
class ProviderFollowupWireResourceQuote:
    """Exact installed prefix plus conservative process-local follow-up suffix."""

    final_wire_utf8_bytes: int
    final_wire_estimated_input_tokens: int
    appended_wire_item_count: int

    def __post_init__(self) -> None:
        if min(
            self.final_wire_utf8_bytes,
            self.final_wire_estimated_input_tokens,
            self.appended_wire_item_count,
        ) < 0:
            raise ValueError("provider follow-up wire quote is invalid")


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
        transport_invocation_observer: (
            Callable[[KernelModelExecutionRequest, bool, str | None], None] | None
        ),
    ) -> None:
        self.request = request
        self.final_context = final_context
        self.append_candidate = append_candidate
        self._transport_timeout_policy_fingerprint = (
            transport_timeout_policy_fingerprint
        )
        self._install_authority = install_authority
        self._usage_observer = usage_observer
        self._transport_invocation_observer = transport_invocation_observer
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
                raise RuntimeError(
                    "prepared model execution can no longer be discarded"
                )
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
        try:
            execution = call.target.transport.open_stream(
                call=call, context=self.final_context
            )
        except BaseException as exc:
            if self._transport_invocation_observer is not None:
                self._transport_invocation_observer(request, False, str(exc))
            raise
        if self._transport_invocation_observer is not None:
            self._transport_invocation_observer(request, True, None)
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
                        profile = call.target.model_profile.route_wire_profile
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
        model_runtime: ModelRuntime,
        usage_observer: Callable[
            [KernelModelExecutionRequest, TransportUsageReport], None
        ]
        | None = None,
        timeout_policy: OpenAITransportTimeoutPolicy | None = None,
        transport_invocation_observer: (
            Callable[[KernelModelExecutionRequest, bool, str | None], None] | None
        ) = None,
    ) -> None:
        transport_timeout = (
            timeout_policy or DEFAULT_KERNEL_WATCHDOG_POLICY.foreground_transport
        )
        if transport_timeout.total_seconds is not None:
            raise ValueError(
                "foreground provider transport must not have a total response timeout"
            )
        self._model_runtime = model_runtime
        self._transport_timeout = transport_timeout
        self._usage_observer = usage_observer
        self._transport_invocation_observer = transport_invocation_observer
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
        target = self._model_runtime.resolve_target(
            request.binding,
            timeout_policy=self._transport_timeout,
        )
        return self.prepare_resolved_target(
            request,
            target=target,
            binding=request.binding,
        )

    def prepare_resolved_target(
        self,
        request: KernelModelTargetPreparationRequest,
        *,
        target: ResolvedModelTarget,
        binding: ModelCallBinding,
    ) -> PreparedKernelModelTarget:
        """Bind a fresh call to one exact process-local resolved target.

        Model-switch handover needs the already-installed source target after
        the session's durable turn binding has moved to the destination.  The
        target is carried as the complete typed value; it is never re-resolved
        from a provider/model lookup or reconstructed from a fingerprint.
        """

        if request.purpose is not ModelCallPurpose.AGENT_MODEL_LOOP:
            raise ValueError("foreground model preparation purpose is invalid")
        if (
            request.model_call_index < 1
            or request.maximum_output_tokens < 1
            or (
                request.maximum_input_tokens is not None
                and request.maximum_input_tokens < 1
            )
            or binding.connection_id != target.connection.id
        ):
            raise ValueError("foreground resolved-target preparation is invalid")
        call = resolve_model_call(
            target=target,
            binding=binding,
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
            target.model_profile.route_wire_profile.wire_api
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
            binding = ModelCallBinding(
                active_prepared_call.call.binding.connection_id,
                None,
            )
            binding = ModelCallBinding(
                binding.connection_id,
                default_reasoning_selection(target.contract.reasoning),
            )
        else:
            raise ValueError("compaction summary requires an origin model call")
        return resolve_model_call(
            target=target,
            binding=binding,
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
            wire_api=(prepared_target.target.model_profile.route_wire_profile.wire_api),
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
            wire_api=(prepared_target.target.model_profile.route_wire_profile.wire_api),
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
        if (
            surface.tool_specs
            and prepared_target.target.contract.target_facts.tool_call is False
        ):
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
        if (
            tool_surface.tool_specs
            and prepared_target.target.contract.target_facts.tool_call is False
        ):
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
            or plan.route_wire_profile_fingerprint
            != provider_wire_profile_fingerprint(prepared.call)
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
            transport_invocation_observer=self._transport_invocation_observer,
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

        measurement = freeze_provider_wire_measurement(
            call=prepared_call.call,
            binding=prepared_call.compile_binding,
            native_projection_set=prepared_call.native_projection_set,
            semantic_input=compiled_input,
            replay_hydration=replay_hydration,
        )
        del predecessor_view
        return measurement.prepare_executable_plan()

    @staticmethod
    def freeze_wire_measurement(
        *,
        call: ResolvedModelCall,
        compile_binding: ModelInputCompileBinding,
        native_projection_set: FrozenNativeToolProjectionSet,
        semantic_input: ProviderWireSemanticInput,
        replay_hydration: FrozenSelectedDurableProviderReplayHydration | None,
        tool_choice: str | None = None,
    ) -> "ProviderWireMeasurement":
        return freeze_provider_wire_measurement(
            call=call,
            binding=compile_binding,
            native_projection_set=native_projection_set,
            semantic_input=semantic_input,
            replay_hydration=replay_hydration,
            tool_choice=tool_choice,
        )

    @staticmethod
    def replay_target(
        prepared_call: PreparedKernelModelCall,
    ) -> ProviderReplayTargetCompatibilityFact:
        return DirectKernelModelPort.replay_target_for_resolved_call(prepared_call.call)

    @staticmethod
    def replay_target_for_resolved_call(
        call: ResolvedModelCall,
    ) -> ProviderReplayTargetCompatibilityFact:
        profile = call.target.model_profile.route_wire_profile
        return build_provider_replay_target_compatibility(
            wire_api=profile.wire_api,
            endpoint_identity_fingerprint=call.target.fact.endpoint_fingerprint,
            normalized_model_identifier=call.target.fact.model_id,
            transport_binding_id=call.target.fact.transport_binding_id,
        )


def provider_wire_profile_fingerprint(call: ResolvedModelCall) -> str:
    profile = call.target.model_profile.route_wire_profile
    return context_fingerprint(
        "pulsara.provider-wire-profile:v1",
        {
            "profile_id": profile.id,
            "wire_api": profile.wire_api,
            "request_defaults": mutable_provider_value(profile.request_defaults),
            "request_extra_body": mutable_provider_value(
                profile.request_extra_body
            ),
            "chat_replay_fields": tuple(
                (
                    item.field_name,
                    item.accumulation_mode.value,
                    item.required_on_selected_response,
                    item.final_value_required,
                )
                for item in profile.chat_replay_fields
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
            ordered_blocks.append(("TOOL_CALL", call.id, call.name, call.arguments))
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
                ordered_blocks.append(("TOOL_CALL", call.id, call.name, call.arguments))
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
    semantic_input: ProviderWireSemanticInput,
    native_projection_set: FrozenNativeToolProjectionSet,
) -> tuple[tuple[tuple[dict[str, object], ...], ...], tuple[dict[str, object], ...]]:
    profile = call.target.model_profile.route_wire_profile
    if profile.wire_api == "openai_chat_completions":
        groups = tuple(
            tuple(chat_semantic_wire_group(item, route_wire_profile=profile))
            for item in semantic_input.messages
        )
    elif profile.wire_api == "openai_responses":
        groups = tuple(
            tuple(responses_semantic_wire_group(item))
            for item in semantic_input.messages
        )
    else:  # pragma: no cover - resolved transport registry is closed
        raise ValueError("provider wire API is unsupported")
    expected_contract = openai_native_function_tool_contract_fingerprint(
        profile.wire_api
    )
    if (
        native_projection_set.native_function_tool_wire_contract_fingerprint
        != expected_contract
        or tuple(item.name for item in semantic_input.tools)
        != tuple(item.provider_name for item in native_projection_set.tool_versions)
    ):
        raise ValueError("native tool projection set does not join compiled tools")
    tools = tuple(
        thaw_json(item.wire_tool) for item in native_projection_set.projections
    )
    if any(not isinstance(item, dict) for item in tools):
        raise TypeError("native tool projection did not thaw to an object")
    if any(not group for group in groups):
        raise ValueError("compiled message lowered to an empty provider wire group")
    return groups, tools


class ProviderWireMeasurement:
    """One-shot owner of one exact adapter materialization and its quote."""

    def __init__(
        self,
        *,
        semantic_input: ProviderWireSemanticInput,
        wire_api: str,
        route_wire_profile_fingerprint: str,
        resolved_target_semantic_fingerprint: str,
        materialization: FrozenProviderWireMaterialization,
        replacements: tuple[FrozenProviderWireReplacementIdentity, ...],
        provider_replay_hydration_fingerprint: str | None,
        wire_system_fingerprint: str,
        wire_tools_fingerprint: str,
        wire_input_prefix_fingerprint: str,
        quote: FrozenProviderWireInputQuote,
    ) -> None:
        self._semantic_input = semantic_input
        self._wire_api = wire_api
        self._route_wire_profile_fingerprint = route_wire_profile_fingerprint
        self._resolved_target_semantic_fingerprint = (
            resolved_target_semantic_fingerprint
        )
        self._materialization: FrozenProviderWireMaterialization | None = (
            materialization
        )
        self._replacements: tuple[FrozenProviderWireReplacementIdentity, ...] | None = (
            replacements
        )
        self._provider_replay_hydration_fingerprint = (
            provider_replay_hydration_fingerprint
        )
        self._wire_system_fingerprint = wire_system_fingerprint
        self._wire_tools_fingerprint = wire_tools_fingerprint
        self._wire_input_prefix_fingerprint = wire_input_prefix_fingerprint
        self.quote = quote
        self._lock = Lock()
        self._consumed = False

    def discard_materialization_to_quote(self) -> FrozenProviderWireInputQuote:
        with self._lock:
            if self._consumed:
                raise RuntimeError("provider wire measurement is already consumed")
            self._consumed = True
            self._materialization = None
            self._replacements = None
        return self.quote

    def prepare_executable_plan(
        self,
        *,
        semantic_input: ProviderWireSemanticInput | None = None,
    ) -> FrozenProviderWireInputPlan:
        with self._lock:
            if self._consumed:
                raise RuntimeError("provider wire measurement is already consumed")
            self._consumed = True
            materialization = self._materialization
            replacements = self._replacements
            self._materialization = None
            self._replacements = None
        if materialization is None or replacements is None:
            raise RuntimeError("provider wire measurement lost its materialization")
        selected_semantic = semantic_input or self._semantic_input
        _require_same_wire_semantic_input(
            self._semantic_input,
            selected_semantic,
        )
        context_id, compiled_semantic_fingerprint = _wire_plan_semantic_identity(
            selected_semantic
        )
        # FrozenProviderWireInputPlan is the single hard-admission factory.
        return FrozenProviderWireInputPlan(
            context_id=context_id,
            compiled_semantic_fingerprint=compiled_semantic_fingerprint,
            message_placements_fingerprint=(
                compiled_message_placements_fingerprint(
                    selected_semantic.message_placements
                )
            ),
            wire_api=self._wire_api,
            route_wire_profile_fingerprint=self._route_wire_profile_fingerprint,
            resolved_target_semantic_fingerprint=(
                self._resolved_target_semantic_fingerprint
            ),
            materialization=materialization,
            replacements=replacements,
            provider_replay_hydration_fingerprint=(
                self._provider_replay_hydration_fingerprint
            ),
            wire_system_fingerprint=self._wire_system_fingerprint,
            wire_tools_fingerprint=self._wire_tools_fingerprint,
            wire_input_prefix_fingerprint=self._wire_input_prefix_fingerprint,
            quote=self.quote,
        )


def freeze_provider_wire_measurement(
    *,
    call: ResolvedModelCall,
    binding: ModelInputCompileBinding,
    native_projection_set: FrozenNativeToolProjectionSet,
    semantic_input: ProviderWireSemanticInput,
    replay_hydration: FrozenSelectedDurableProviderReplayHydration | None,
    tool_choice: str | None = None,
) -> ProviderWireMeasurement:
    if (
        semantic_input.compile_binding_fingerprint != binding.binding_fingerprint
        or semantic_input.tools != binding.tool_surface.tool_specs
        or call.target.fact != binding.target_fact
    ):
        raise ValueError("provider wire planning input does not join preparation")
    validate_model_message_content_for_call(
        call=call,
        messages=semantic_input.messages,
    )
    recomputed_semantic = binding.estimator.estimate_frozen_input(
        system_prompt=semantic_input.system_prompt,
        messages=semantic_input.messages,
        tools=semantic_input.tools,
    )
    if recomputed_semantic != semantic_input.final_estimate:
        raise ValueError("provider wire semantic estimate changed")
    generic_groups, wire_tools = _semantic_wire_groups(
        call=call,
        semantic_input=semantic_input,
        native_projection_set=native_projection_set,
    )
    profile = call.target.model_profile.route_wire_profile
    profile_fingerprint = provider_wire_profile_fingerprint(call)
    replay_target = DirectKernelModelPort.replay_target_for_resolved_call(call)
    fragments = () if replay_hydration is None else replay_hydration.fragments
    if replay_hydration is not None:
        identity = semantic_input.canonical_input_identity
        if (
            replay_hydration.scope.session_id != identity.session_id
            or replay_hydration.scope.scope_kind is not identity.conversation_scope_kind
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
    final_sources: list[LLMMessage | None] = []
    used_entries: set[str] = set()
    replaced_generic_wire_tokens = 0
    replay_wire_tokens = 0
    index = 0
    while index < len(semantic_input.messages):
        placement = semantic_input.message_placements[index]
        entry_id = placement.origin_entry_id
        fragment = None if entry_id is None else fragment_by_entry.get(entry_id)
        if fragment is None:
            final_items.extend(generic_groups[index])
            final_sources.extend(
                semantic_input.messages[index] for _ in generic_groups[index]
            )
            index += 1
            continue
        if entry_id in used_entries:
            raise ValueError("provider replay fragment matched more than one group")
        end = index + 1
        while (
            end < len(semantic_input.message_placements)
            and semantic_input.message_placements[end].origin_entry_id == entry_id
        ):
            end += 1
        placements = semantic_input.message_placements[index:end]
        if tuple(item.within_origin_ordinal for item in placements) != tuple(
            range(len(placements))
        ):
            raise ValueError("provider replay placement group is not contiguous")
        messages = semantic_input.messages[index:end]
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
        generic_tokens = sum(
            binding.estimator.estimate_wire_json_component(item) for item in generic
        )
        replacement_tokens = sum(
            binding.estimator.estimate_wire_json_component(item) for item in replacement
        )
        replacements.append(
            FrozenProviderWireReplacementIdentity(
                assistant_entry_id=entry_id or "",
                first_message_ordinal=index,
                message_count=end - index,
                replay_fragment_fingerprint=fragment.fragment_fingerprint,
                generic_wire_estimated_tokens=generic_tokens,
                replay_wire_estimated_tokens=replacement_tokens,
            )
        )
        final_items.extend(replacement)  # type: ignore[arg-type]
        final_sources.extend(None for _ in replacement)
        used_entries.add(entry_id or "")
        replaced_generic_wire_tokens += generic_tokens
        replay_wire_tokens += replacement_tokens
        index = end
    if used_entries != set(fragment_by_entry):
        raise ValueError("an installed provider replay fragment was omitted")
    hydration_fingerprint = (
        None if replay_hydration is None else replay_hydration.hydration_fingerprint
    )
    if replay_hydration is not None:
        replay_placements = tuple(
            item
            for item in semantic_input.message_placements
            if item.origin_entry_id in used_entries
        )
        if selected_message_placements_fingerprint(
            replay_placements
        ) != replay_hydration.selected_message_placements_fingerprint or tuple(
            item.assistant_entry_id for item in replay_hydration.fragments
        ) != tuple(item.assistant_entry_id for item in replacements):
            raise ValueError("provider replay hydration placements drifted")

    root_value = freeze_json(compose_provider_root_policy(semantic_input.system_prompt))
    frozen_tools = tuple(_freeze_wire_object(item) for item in wire_tools)
    frozen_inputs = tuple(_freeze_wire_object(item) for item in final_items)
    root_plain = thaw_json(root_value)
    tools_plain = tuple(thaw_json(item) for item in frozen_tools)
    inputs_plain = tuple(thaw_json(item) for item in frozen_inputs)
    generic_items = tuple(item for group in generic_groups for item in group)
    generic_sources = tuple(
        message
        for message, group in zip(
            semantic_input.messages, generic_groups, strict=True
        )
        for _ in group
    )
    fixed_projection = _materialize_context_bearing_projection(
        call=call,
        root_policy=root_plain,
        tool_items=tools_plain,
        ordered_input_items=(),
        tool_choice=tool_choice,
    )
    final_projection = _materialize_context_bearing_projection(
        call=call,
        root_policy=root_plain,
        tool_items=tools_plain,
        ordered_input_items=inputs_plain,
        tool_choice=tool_choice,
    )
    frozen_projection = freeze_json(final_projection)
    if not isinstance(frozen_projection, FrozenJsonObjectFact):
        raise TypeError("provider context projection did not freeze to an object")
    materialization = FrozenProviderWireMaterialization(
        root_policy_value=root_value,
        tool_items=frozen_tools,
        ordered_input_items=frozen_inputs,
        context_bearing_projection=frozen_projection,
    )
    generic_wire_tokens = binding.estimator.estimate_final_wire_json_components(
        fixed_context=fixed_projection,
        ordered_input_items=generic_items,
        ordered_input_sources=generic_sources,
    )
    final_wire_total_tokens = (
        generic_wire_tokens.total_input_tokens
        - replaced_generic_wire_tokens
        + replay_wire_tokens
    )
    direct_final_wire_tokens = binding.estimator.estimate_final_wire_json_components(
        fixed_context=fixed_projection,
        ordered_input_items=inputs_plain,
        ordered_input_sources=tuple(final_sources),
    )
    if (
        direct_final_wire_tokens.total_input_tokens != final_wire_total_tokens
        or direct_final_wire_tokens.visual_image_tokens
        != generic_wire_tokens.visual_image_tokens
    ):
        raise ValueError("provider wire component traversal is inconsistent")
    final_wire_bytes = len(canonical_json_bytes(final_projection))
    quote = FrozenProviderWireInputQuote(
        wire_api=profile.wire_api,
        estimator_fingerprint=binding.estimator_fingerprint,
        effective_input_budget_tokens=binding.effective_input_budget_tokens,
        semantic_estimated_input_tokens=(
            semantic_input.final_estimate.total_input_tokens
        ),
        semantic_visual_image_tokens=(
            semantic_input.final_estimate.visual_image_tokens
        ),
        generic_wire_estimated_input_tokens=(
            generic_wire_tokens.total_input_tokens
        ),
        generic_wire_visual_image_tokens=(
            generic_wire_tokens.visual_image_tokens
        ),
        replaced_generic_wire_estimated_tokens=(replaced_generic_wire_tokens),
        replay_wire_estimated_tokens=replay_wire_tokens,
        final_wire_estimated_input_tokens=final_wire_total_tokens,
        final_wire_visual_image_tokens=(
            direct_final_wire_tokens.visual_image_tokens
        ),
        final_wire_utf8_bytes=final_wire_bytes,
    )
    wire_system = context_fingerprint("pulsara.provider-wire-system:v1", root_plain)
    wire_tools_fingerprint = context_fingerprint(
        "pulsara.provider-wire-tools:v1", tools_plain
    )
    wire_input = context_fingerprint(
        "pulsara.provider-wire-input-prefix:v2-final-context-projection",
        {
            "api": profile.wire_api,
            "profile": profile_fingerprint,
            "projection": final_projection,
        },
    )
    return ProviderWireMeasurement(
        semantic_input=semantic_input,
        wire_api=profile.wire_api,
        route_wire_profile_fingerprint=profile_fingerprint,
        resolved_target_semantic_fingerprint=call.target.fact.target_fingerprint,
        materialization=materialization,
        replacements=tuple(replacements),
        provider_replay_hydration_fingerprint=hydration_fingerprint,
        wire_system_fingerprint=wire_system,
        wire_tools_fingerprint=wire_tools_fingerprint,
        wire_input_prefix_fingerprint=wire_input,
        quote=quote,
    )


def _wire_plan_semantic_identity(
    semantic_input: ProviderWireSemanticInput,
) -> tuple[str, str]:
    context_id = getattr(semantic_input, "context_id", None) or context_fingerprint(
        "pulsara.provider-wire-structural-context-id:v1",
        {
            "canonical": semantic_input.canonical_input_identity.identity_fingerprint,
            "placements": compiled_message_placements_fingerprint(
                semantic_input.message_placements
            ),
            "binding": semantic_input.compile_binding_fingerprint,
        },
    )
    compiled_semantic_fingerprint = getattr(
        semantic_input, "compiled_semantic_fingerprint", None
    ) or context_fingerprint(
        "pulsara.provider-wire-structural-semantic-input:v1",
        {
            "canonical": semantic_input.canonical_input_identity,
            "system": semantic_input.system_prompt,
            "messages": semantic_input.messages,
            "placements": compiled_message_placements_fingerprint(
                semantic_input.message_placements
            ),
            "tools": semantic_input.tools,
            "estimate": semantic_input.final_estimate,
            "binding": semantic_input.compile_binding_fingerprint,
        },
    )
    return context_id, compiled_semantic_fingerprint


def _require_same_wire_semantic_input(
    measured: ProviderWireSemanticInput,
    selected: ProviderWireSemanticInput,
) -> None:
    if (
        measured.canonical_input_identity != selected.canonical_input_identity
        or measured.system_prompt != selected.system_prompt
        or measured.messages != selected.messages
        or measured.message_placements != selected.message_placements
        or measured.tools != selected.tools
        or measured.final_estimate != selected.final_estimate
        or measured.compile_binding_fingerprint != selected.compile_binding_fingerprint
    ):
        raise ValueError("provider wire semantic input changed after measurement")


def _materialize_context_bearing_projection(
    *,
    call: ResolvedModelCall,
    root_policy: object,
    tool_items: tuple[object, ...],
    ordered_input_items: tuple[object, ...],
    tool_choice: str | None,
) -> dict[str, object]:
    if root_policy is not None and not isinstance(root_policy, str):
        raise TypeError("provider root policy must be text or null")
    if any(not isinstance(item, dict) for item in tool_items):
        raise TypeError("provider tool item is not an object")
    if any(not isinstance(item, dict) for item in ordered_input_items):
        raise TypeError("provider input item is not an object")
    profile = call.target.model_profile.route_wire_profile
    if profile.wire_api == "openai_chat_completions":
        return materialize_chat_context_bearing_wire_projection(
            call=call,
            root_policy=root_policy,
            tool_items=tool_items,  # type: ignore[arg-type]
            ordered_input_items=ordered_input_items,  # type: ignore[arg-type]
            tool_choice=tool_choice,
        )
    if profile.wire_api == "openai_responses":
        return materialize_responses_context_bearing_wire_projection(
            call=call,
            root_policy=root_policy,
            tool_items=tool_items,  # type: ignore[arg-type]
            ordered_input_items=ordered_input_items,  # type: ignore[arg-type]
            tool_choice=tool_choice,
        )
    raise ValueError("provider wire API is unsupported")


def quote_provider_followup_wire_resources(
    *,
    request: KernelModelExecutionRequest,
    actual_assistant_message: LLMMessage,
    provider_replay: PreparedDurableProviderAssistantReplay | None,
    bounded_suffix_messages: tuple[LLMMessage, ...],
) -> ProviderFollowupWireResourceQuote:
    """Quote one direct successor from the installed exact wire prefix.

    The response carrier is exact (native replay where the adapter returned
    one); only result/closure and source messages supplied by the caller are
    conservative existing-owner bounds.  The function rematerializes the same
    Chat/Responses context projection and uses the resolved target's frozen D1
    estimator.  It has no install or provider-open authority.
    """

    if actual_assistant_message.role is not MessageRole.ASSISTANT:
        raise ValueError("provider follow-up requires an assistant response")
    plan = request.wire_input_plan
    call = request.prepared_call.call
    profile = call.target.model_profile.route_wire_profile
    if plan.wire_api != profile.wire_api:
        raise ValueError("provider follow-up wire API differs from its prefix")

    existing_items = tuple(
        thaw_json(item) for item in plan.materialization.ordered_input_items
    )
    if any(not isinstance(item, dict) for item in existing_items):
        raise TypeError("installed provider input item is not an object")
    root = thaw_json(plan.materialization.root_policy_value)
    tools = tuple(thaw_json(item) for item in plan.materialization.tool_items)
    current_projection = thaw_json(
        plan.materialization.context_bearing_projection
    )
    if not isinstance(current_projection, dict):
        raise TypeError("installed provider context projection is not an object")
    tool_choice = current_projection.get("tool_choice")
    if tool_choice is not None and not isinstance(tool_choice, str):
        raise TypeError("installed provider tool choice is invalid")
    reproduced = _materialize_context_bearing_projection(
        call=call,
        root_policy=root,
        tool_items=tools,
        ordered_input_items=existing_items,  # type: ignore[arg-type]
        tool_choice=tool_choice,
    )
    if reproduced != current_projection:
        raise ValueError("installed provider context cannot be rematerialized")

    appended_items: list[dict[str, object]] = []
    if provider_replay is None:
        if profile.wire_api == "openai_chat_completions":
            appended_items.extend(
                chat_semantic_wire_group(
                    actual_assistant_message,
                    route_wire_profile=profile,
                )
            )
        elif profile.wire_api == "openai_responses":
            appended_items.extend(
                responses_semantic_wire_group(actual_assistant_message)
            )
        else:  # pragma: no cover - resolved transport registry is closed
            raise ValueError("provider wire API is unsupported")
    else:
        target = DirectKernelModelPort.replay_target_for_resolved_call(call)
        if (
            provider_replay.wire_api != profile.wire_api
            or provider_replay.replay_target_fingerprint
            != target.replay_target_fingerprint
        ):
            raise ValueError("provider follow-up replay targets another wire")
        for frozen in provider_replay.ordered_items:
            item = thaw_json(frozen)
            if not isinstance(item, dict):
                raise TypeError("provider replay item is not an object")
            appended_items.append(item)

    for message in bounded_suffix_messages:
        if profile.wire_api == "openai_chat_completions":
            appended_items.extend(
                chat_semantic_wire_group(message, route_wire_profile=profile)
            )
        elif profile.wire_api == "openai_responses":
            appended_items.extend(responses_semantic_wire_group(message))
        else:  # pragma: no cover - resolved transport registry is closed
            raise ValueError("provider wire API is unsupported")

    final_projection = _materialize_context_bearing_projection(
        call=call,
        root_policy=root,
        tool_items=tools,
        ordered_input_items=(*existing_items, *appended_items),  # type: ignore[arg-type]
        tool_choice=tool_choice,
    )
    suffix_tokens = sum(
        call.target.token_estimator.estimate_wire_json_component(item)
        for item in appended_items
    )
    return ProviderFollowupWireResourceQuote(
        final_wire_utf8_bytes=len(canonical_json_bytes(final_projection)),
        final_wire_estimated_input_tokens=(
            plan.quote.final_wire_estimated_input_tokens + suffix_tokens
        ),
        appended_wire_item_count=len(appended_items),
    )


__all__ = [
    "DirectKernelModelPort",
    "KernelModelExecutionRequest",
    "KernelModelPreparationRequest",
    "PreparedKernelModelExecution",
    "PreparedKernelModelCall",
    "PreparedKernelSemanticModelCall",
    "ProviderFollowupWireResourceQuote",
    "ProviderWireMeasurement",
    "freeze_provider_wire_measurement",
    "provider_wire_profile_fingerprint",
    "quote_provider_followup_wire_resources",
]
