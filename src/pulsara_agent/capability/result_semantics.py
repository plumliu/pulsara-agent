"""Process-local bindings for durable tool-result semantics contracts."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol

from pulsara_agent.primitives.context import (
    FrozenJsonObjectFact,
    freeze_json,
    thaw_json,
)
from pulsara_agent.primitives.context import CapabilityDescriptorRenderAttributionFact
from pulsara_agent.primitives.context import (
    canonical_utc_timestamp,
    context_fingerprint,
)
from pulsara_agent.primitives.tool_observation import ToolObservationTimingFact
from pulsara_agent.primitives.tool_result import (
    ArtifactDomainSubmissionFact,
    ArtifactEssentialResultFact,
    CapabilityResultRenderVariantFact,
    CapabilityResultRenderContractFact,
    TerminalCommandEssentialFact,
    TerminalCommandErrorEssentialFact,
    TerminalCommandDomainSubmissionFact,
    TerminalCommandErrorDomainSubmissionFact,
    TerminalProcessErrorEssentialFact,
    TerminalProcessErrorDomainSubmissionFact,
    TerminalProcessInventoryEssentialFact,
    TerminalProcessInventoryDomainSubmissionFact,
    TerminalProcessObservationEssentialFact,
    TerminalProcessObservationDomainSubmissionFact,
    TerminalProcessSummaryFact,
    TerminalMonitorCancellationDomainSubmissionFact,
    TerminalMonitorCancellationEssentialFact,
    TerminalMonitorErrorDomainSubmissionFact,
    TerminalMonitorErrorEssentialFact,
    TerminalMonitorInventoryDomainSubmissionFact,
    TerminalMonitorInventoryEssentialFact,
    TerminalMonitorRegistrationDomainSubmissionFact,
    TerminalMonitorRegistrationEssentialFact,
    TerminalPayloadTimingFact,
    ToolResultErrorPreviewFact,
    ToolResultDomainSubmissionFact,
    ToolResultEssentialCapturePolicyFact,
    ToolResultEssentialEnvelopeKind,
    ToolResultExecutionSemanticsFact,
    ToolResultSemanticsBuilderContractFact,
    ToolResultRenderProfileFact,
    ToolResultRenderVariantCode,
    ToolResultRollupSemanticsFact,
    ToolResultStateFact,
    validate_tool_result_profile_contract,
)

if TYPE_CHECKING:
    from pulsara_agent.capability.descriptor import CapabilityDescriptor
    from pulsara_agent.tools.base import ToolCall, ToolExecutionResult


class ToolResultSemanticsRuntimeInput(Protocol):
    semantics_input_kind: ToolResultRenderVariantCode

    def to_frozen_domain_submission(self) -> ToolResultDomainSubmissionFact | None: ...


@dataclass(frozen=True, slots=True)
class FrozenToolResultSemanticsRuntimeInput:
    """Typed execution-boundary input; never inferred from serialized output."""

    semantics_input_kind: ToolResultRenderVariantCode
    domain_submission: ToolResultDomainSubmissionFact | None

    def to_frozen_domain_submission(self) -> ToolResultDomainSubmissionFact | None:
        return self.domain_submission


class ToolResultSemanticsBuilder(Protocol):
    builder_id: str
    builder_version: str

    def build(
        self,
        *,
        descriptor: object,
        descriptor_attribution: CapabilityDescriptorRenderAttributionFact,
        selected_variant: CapabilityResultRenderVariantFact,
        normalized_arguments: FrozenJsonObjectFact | None,
        typed_result: ToolResultSemanticsRuntimeInput | None,
        domain_submission: ToolResultDomainSubmissionFact | None,
        observation_timing: ToolObservationTimingFact,
        terminal_payload_timing: TerminalPayloadTimingFact | None,
        essential_capture_policy: ToolResultEssentialCapturePolicyFact | None,
        result_state: ToolResultStateFact,
    ) -> ToolResultExecutionSemanticsFact: ...


@dataclass(frozen=True, slots=True)
class ToolResultSemanticsBuilderBinding:
    builder_id: str
    builder_version: str
    builder_contract: ToolResultSemanticsBuilderContractFact
    implementation_build_fingerprint: str | None
    builder: ToolResultSemanticsBuilder

    def __post_init__(self) -> None:
        identities = {
            (self.builder_id, self.builder_version),
            (
                self.builder_contract.builder_id,
                self.builder_contract.builder_version,
            ),
            (self.builder.builder_id, self.builder.builder_version),
        }
        if len(identities) != 1:
            raise ValueError("semantics builder binding identity mismatch")


class ToolResultSemanticsBuilderRegistry:
    def __init__(self) -> None:
        self._bindings: dict[tuple[str, str], ToolResultSemanticsBuilderBinding] = {}
        self._frozen = False

    def register(self, binding: ToolResultSemanticsBuilderBinding) -> None:
        if self._frozen:
            raise RuntimeError("tool-result semantics registry is frozen")
        key = (binding.builder_id, binding.builder_version)
        if key in self._bindings:
            existing = self._bindings[key]
            if (
                existing.builder_contract.contract_fingerprint
                != binding.builder_contract.contract_fingerprint
            ):
                raise ValueError(
                    "same semantics builder ID/version has a different contract"
                )
            raise ValueError("duplicate tool-result semantics builder binding")
        self._bindings[key] = binding

    def resolve_binding(
        self,
        builder_id: str,
        builder_version: str,
    ) -> ToolResultSemanticsBuilderBinding:
        try:
            return self._bindings[(builder_id, builder_version)]
        except KeyError as exc:
            raise LookupError(
                f"missing tool-result semantics builder {builder_id}@{builder_version}"
            ) from exc

    def freeze(self) -> None:
        self._frozen = True

    @property
    def frozen(self) -> bool:
        return self._frozen


@dataclass(frozen=True, slots=True)
class DeclarativeToolResultSemanticsBuilder:
    builder_id: str
    builder_version: str

    def build(
        self,
        *,
        descriptor: object,
        descriptor_attribution: CapabilityDescriptorRenderAttributionFact,
        selected_variant: CapabilityResultRenderVariantFact,
        normalized_arguments: FrozenJsonObjectFact | None,
        typed_result: ToolResultSemanticsRuntimeInput | None,
        domain_submission: ToolResultDomainSubmissionFact | None,
        observation_timing: ToolObservationTimingFact,
        terminal_payload_timing: TerminalPayloadTimingFact | None,
        essential_capture_policy: ToolResultEssentialCapturePolicyFact | None,
        result_state: ToolResultStateFact,
    ) -> ToolResultExecutionSemanticsFact:
        del observation_timing
        if typed_result is not None and domain_submission is not None:
            raise ValueError(
                "live and external semantics inputs are mutually exclusive"
            )
        contract = getattr(descriptor, "result_render_contract", None)
        if not isinstance(contract, CapabilityResultRenderContractFact):
            raise ValueError("semantics builder requires frozen render contract")
        if (
            contract.semantics_builder_id,
            contract.semantics_builder_version,
        ) != (self.builder_id, self.builder_version):
            raise ValueError("semantics builder/render contract identity mismatch")
        profile = _profile(
            descriptor=descriptor,  # type: ignore[arg-type]
            attribution=descriptor_attribution,
            contract=contract,
            variant=selected_variant,
        )
        frozen_submission = (
            typed_result.to_frozen_domain_submission()
            if typed_result is not None
            else domain_submission
        )
        essential = _essential_from_domain_submission(
            selected_variant=selected_variant,
            domain_submission=frozen_submission,
            capture_policy=essential_capture_policy,
        )
        rollup_semantics = _rollup_semantics(
            descriptor=descriptor,
            contract=contract,
            selected_variant=selected_variant,
            normalized_arguments=normalized_arguments,
            result_state=result_state,
            essential=essential,
        )
        return ToolResultExecutionSemanticsFact(
            render_profile=profile,
            result_state=result_state,
            essential_capture_policy=(
                essential_capture_policy if essential is not None else None
            ),
            essential_result=essential,
            terminal_payload_timing=terminal_payload_timing,
            rollup_semantics=rollup_semantics,
        )


def build_default_tool_result_semantics_registry() -> (
    ToolResultSemanticsBuilderRegistry
):
    from pulsara_agent.capability.result_contracts import (
        generic_result_render_contract,
        terminal_monitor_result_render_contract,
        terminal_process_result_render_contract,
        terminal_result_render_contract,
    )

    registry = ToolResultSemanticsBuilderRegistry()
    for contract in (
        generic_result_render_contract(),
        terminal_result_render_contract(),
        terminal_process_result_render_contract(),
        terminal_monitor_result_render_contract(),
    ):
        builder = DeclarativeToolResultSemanticsBuilder(
            builder_id=contract.semantics_builder_id,
            builder_version=contract.semantics_builder_version,
        )
        registry.register(
            ToolResultSemanticsBuilderBinding(
                builder_id=builder.builder_id,
                builder_version=builder.builder_version,
                builder_contract=contract.semantics_builder_contract,
                implementation_build_fingerprint="pulsara:declarative-builder:v1",
                builder=builder,
            )
        )
    registry.freeze()
    return registry


def default_essential_capture_policy() -> ToolResultEssentialCapturePolicyFact:
    payload = {
        "policy_version": "tool-result-essential-capture:v1",
        "max_error_chars": 512,
        "max_process_summaries": 8,
        "max_process_command_chars": 160,
        "max_process_cwd_chars": 160,
    }
    return ToolResultEssentialCapturePolicyFact(
        **payload,
        policy_fingerprint=context_fingerprint(
            "tool-result-essential-capture-policy:v1", payload
        ),
    )


def build_execution_semantics(
    *,
    descriptor: "CapabilityDescriptor",
    descriptor_attribution: CapabilityDescriptorRenderAttributionFact,
    call: "ToolCall",
    result: "ToolExecutionResult",
    observation_timing: ToolObservationTimingFact,
    capture_policy: ToolResultEssentialCapturePolicyFact,
    registry: ToolResultSemanticsBuilderRegistry,
) -> ToolResultExecutionSemanticsFact:
    contract = _require_contract(descriptor)
    binding = _resolve_contract_binding(contract, registry=registry)
    state = ToolResultStateFact(result.status.value)
    typed_result = result.semantics_input or _fallback_runtime_input(
        contract=contract,
        call=call,
        result=result,
        state=state,
        capture_policy=capture_policy,
    )
    variant = _variant(contract, typed_result.semantics_input_kind)
    normalized_arguments = freeze_json(call.arguments)
    if not isinstance(normalized_arguments, FrozenJsonObjectFact):
        raise AssertionError("tool arguments must freeze as an object")
    semantics = binding.builder.build(
        descriptor=descriptor,
        descriptor_attribution=descriptor_attribution,
        selected_variant=variant,
        normalized_arguments=normalized_arguments,
        typed_result=typed_result,
        domain_submission=None,
        observation_timing=observation_timing,
        terminal_payload_timing=result.terminal_payload_timing,
        essential_capture_policy=(
            capture_policy
            if variant.essential_envelope_kind
            is not ToolResultEssentialEnvelopeKind.NONE
            else None
        ),
        result_state=state,
    )
    validate_tool_result_profile_contract(
        profile=semantics.render_profile, contract=contract
    )
    return semantics


def build_adapter_failure_runtime_input(
    *,
    contract: CapabilityResultRenderContractFact,
    call: "ToolCall",
    error_text: str,
    state: ToolResultStateFact,
) -> FrozenToolResultSemanticsRuntimeInput:
    """Create typed adapter-failure input without inspecting serialized output."""

    error = unbounded_error_preview(error_text or "tool adapter failed")
    if contract.semantics_builder_id == "tool-result-semantics:terminal-command":
        return FrozenToolResultSemanticsRuntimeInput(
            semantics_input_kind=ToolResultRenderVariantCode.TERMINAL_COMMAND_ADAPTER_ERROR,
            domain_submission=TerminalCommandErrorDomainSubmissionFact(
                requested_command=_str_or_none(call.arguments.get("command")),
                failure_stage="adapter_initialization",
                status=state.value,
                error=error,
                policy_code="tool_adapter_error",
                observed_cwd=None,
                terminal_session_id=None,
                backend_type=None,
                io_mode=None,
            ),
        )
    if contract.semantics_builder_id == "tool-result-semantics:terminal-process":
        return FrozenToolResultSemanticsRuntimeInput(
            semantics_input_kind=ToolResultRenderVariantCode.TERMINAL_PROCESS_ADAPTER_ERROR,
            domain_submission=TerminalProcessErrorDomainSubmissionFact(
                requested_action=str(call.arguments.get("action") or "unknown"),
                process_id=_str_or_none(call.arguments.get("process_id")),
                status=state.value,
                error=error,
                policy_code="tool_adapter_error",
                terminal_session_id=None,
                backend_type=None,
            ),
        )
    if contract.semantics_builder_id == "tool-result-semantics:terminal-monitor":
        requested_action = str(call.arguments.get("action") or "unknown")
        return FrozenToolResultSemanticsRuntimeInput(
            semantics_input_kind=ToolResultRenderVariantCode.TERMINAL_MONITOR_ADAPTER_ERROR,
            domain_submission=TerminalMonitorErrorDomainSubmissionFact(
                requested_action=requested_action,
                process_id=(
                    _str_or_none(call.arguments.get("process_id"))
                    if requested_action == "register"
                    else None
                ),
                monitor_id=(
                    _str_or_none(call.arguments.get("monitor_id"))
                    if requested_action == "cancel"
                    else None
                ),
                status=state.value,
                error=error,
                policy_code="tool_adapter_error",
            ),
        )
    return FrozenToolResultSemanticsRuntimeInput(
        semantics_input_kind=ToolResultRenderVariantCode.GENERIC_RESULT,
        domain_submission=None,
    )


def _fallback_runtime_input(
    *,
    contract: CapabilityResultRenderContractFact,
    call: "ToolCall",
    result: "ToolExecutionResult",
    state: ToolResultStateFact,
    capture_policy: ToolResultEssentialCapturePolicyFact,
) -> FrozenToolResultSemanticsRuntimeInput:
    del call, result, state, capture_policy
    if contract.semantics_builder_id != "tool-result-semantics:generic":
        raise ValueError(
            "terminal result requires typed semantics input; serialized output inference is forbidden"
        )
    return FrozenToolResultSemanticsRuntimeInput(
        semantics_input_kind=ToolResultRenderVariantCode.GENERIC_RESULT,
        domain_submission=None,
    )


def unbounded_error_preview(value: str) -> ToolResultErrorPreviewFact:
    return ToolResultErrorPreviewFact(
        text=value,
        original_chars=len(value),
        truncated=False,
    )


def build_pre_execution_denial_semantics(
    *,
    descriptor: "CapabilityDescriptor",
    descriptor_attribution: CapabilityDescriptorRenderAttributionFact,
    requested_arguments: FrozenJsonObjectFact | None,
    message: str,
    result_state: ToolResultStateFact,
    reason_code: str,
    failure_stage: str = "permission_denied",
    capture_policy: ToolResultEssentialCapturePolicyFact,
    registry: ToolResultSemanticsBuilderRegistry,
    observation_timing: ToolObservationTimingFact,
) -> ToolResultExecutionSemanticsFact:
    contract = _require_contract(descriptor)
    binding = _resolve_contract_binding(contract, registry=registry)
    variant = _variant(contract, contract.pre_execution_denial_variant_code)
    if variant.operational_kind.value == "terminal_command_error":
        domain_submission: ToolResultDomainSubmissionFact | None = (
            TerminalCommandErrorDomainSubmissionFact(
                requested_command=_argument_string(requested_arguments, "command"),
                failure_stage=failure_stage,
                status=result_state.value,
                error=unbounded_error_preview(message),
                policy_code=reason_code,
                observed_cwd=None,
                terminal_session_id=None,
                backend_type=None,
                io_mode=None,
            )
        )
    elif variant.operational_kind.value == "terminal_process_error":
        domain_submission = TerminalProcessErrorDomainSubmissionFact(
            requested_action=(
                _argument_string(requested_arguments, "action") or "unknown"
            ),
            process_id=_argument_string(requested_arguments, "process_id"),
            status=result_state.value,
            error=unbounded_error_preview(message),
            policy_code=reason_code,
            terminal_session_id=None,
            backend_type=None,
        )
    elif variant.operational_kind.value == "terminal_monitor_error":
        requested_action = _argument_string(requested_arguments, "action") or "unknown"
        domain_submission = TerminalMonitorErrorDomainSubmissionFact(
            requested_action=requested_action,
            process_id=(
                _argument_string(requested_arguments, "process_id")
                if requested_action == "register"
                else None
            ),
            monitor_id=(
                _argument_string(requested_arguments, "monitor_id")
                if requested_action == "cancel"
                else None
            ),
            status=result_state.value,
            error=unbounded_error_preview(message),
            policy_code=reason_code,
        )
    else:
        domain_submission = None
    semantics = binding.builder.build(
        descriptor=descriptor,
        descriptor_attribution=descriptor_attribution,
        selected_variant=variant,
        normalized_arguments=requested_arguments,
        typed_result=FrozenToolResultSemanticsRuntimeInput(
            semantics_input_kind=variant.variant_code,
            domain_submission=domain_submission,
        ),
        domain_submission=None,
        observation_timing=observation_timing,
        terminal_payload_timing=None,
        essential_capture_policy=(
            capture_policy
            if variant.essential_envelope_kind
            is not ToolResultEssentialEnvelopeKind.NONE
            else None
        ),
        result_state=result_state,
    )
    validate_tool_result_profile_contract(
        profile=semantics.render_profile,
        contract=contract,
    )
    return semantics


def build_unknown_result_semantics(
    *,
    result_state: ToolResultStateFact,
) -> ToolResultExecutionSemanticsFact:
    from pulsara_agent.capability.result_contracts import (
        generic_result_render_contract,
    )

    contract = generic_result_render_contract()
    variant_code = (
        ToolResultRenderVariantCode.GENERIC_DENIED
        if result_state in {ToolResultStateFact.DENIED, ToolResultStateFact.ERROR}
        else ToolResultRenderVariantCode.GENERIC_RESULT
    )
    variant = _variant(contract, variant_code)
    payload = {
        "profile_version": "tool-result-profile:v1",
        "selected_variant": variant,
        "render_contract": contract,
        "tool_origin": "unknown",
        "descriptor_attribution": None,
        "render_contract_fingerprint": contract.contract_fingerprint,
    }
    profile = ToolResultRenderProfileFact(
        **payload,
        profile_fingerprint=context_fingerprint(
            "tool-result-render-profile:v1", payload
        ),
    )
    return ToolResultExecutionSemanticsFact(
        render_profile=profile,
        result_state=result_state,
        essential_capture_policy=None,
        essential_result=None,
        terminal_payload_timing=None,
        rollup_semantics=None,
    )


def _rollup_semantics(
    *,
    descriptor: object,
    contract: CapabilityResultRenderContractFact,
    selected_variant: CapabilityResultRenderVariantFact,
    normalized_arguments: FrozenJsonObjectFact | None,
    result_state: ToolResultStateFact,
    essential: object | None,
) -> ToolResultRollupSemanticsFact | None:
    """Classify only frozen arguments and typed essential facts."""

    tool_name = str(getattr(descriptor, "name", "") or "")
    arguments = (
        thaw_json(normalized_arguments) if normalized_arguments is not None else {}
    )
    if not isinstance(arguments, dict):
        raise ValueError("normalized tool arguments must thaw to an object")
    evidence = tuple(
        sorted(
            {
                f"{key}={value[:192]}"
                for key in ("path", "query", "process_id", "action")
                if isinstance((value := arguments.get(key)), str) and value
            }
        )
    )
    rollup_kind: str | None = None
    family_basis: object | None = None
    if result_state in {ToolResultStateFact.ERROR, ToolResultStateFact.DENIED}:
        rollup_kind = "repeated_error_family"
        family_basis = {
            "tool_name": tool_name,
            "result_state": result_state.value,
            "variant": selected_variant.variant_code.value,
        }
    elif tool_name == "read_file" and result_state is ToolResultStateFact.SUCCESS:
        rollup_kind = "repeated_file_reads"
        family_basis = {"tool_name": tool_name, "path": arguments.get("path")}
    elif tool_name == "search_files" and result_state is ToolResultStateFact.SUCCESS:
        rollup_kind = "repeated_search_results"
        family_basis = {
            "tool_name": tool_name,
            "path": arguments.get("path"),
            "query": arguments.get("query"),
        }
    elif isinstance(essential, TerminalProcessInventoryEssentialFact):
        rollup_kind = "terminal_inventory"
        family_basis = {"tool_name": tool_name, "action": "list"}
    elif (
        tool_name in {"wait_agent", "wait_agent_tasks", "list_agents"}
        and result_state is ToolResultStateFact.SUCCESS
    ):
        rollup_kind = "subagent_result_index"
        family_basis = {"tool_name": tool_name}
    if rollup_kind is None or family_basis is None:
        return None
    payload = {
        "schema_version": "tool-result-rollup-semantics.v1",
        "rollup_kind": rollup_kind,
        "family_key": context_fingerprint("tool-result-rollup-family:v1", family_basis),
        "evidence_keys": evidence,
        "renderer_id": contract.rollup_renderer_id,
        "renderer_version": contract.rollup_renderer_version,
        "renderer_contract_fingerprint": (
            contract.rollup_renderer_contract_fingerprint
        ),
    }
    return ToolResultRollupSemanticsFact(
        **payload,
        semantics_fingerprint=context_fingerprint(
            "tool-result-rollup-semantics:v1", payload
        ),
    )


def _essential_from_domain_submission(
    *,
    selected_variant: CapabilityResultRenderVariantFact,
    domain_submission: ToolResultDomainSubmissionFact | None,
    capture_policy: ToolResultEssentialCapturePolicyFact | None,
):
    expected = selected_variant.essential_envelope_kind
    if expected is ToolResultEssentialEnvelopeKind.NONE:
        if domain_submission is not None or capture_policy is not None:
            raise ValueError("no-essential variant forbids domain result and policy")
        return None
    if domain_submission is None or capture_policy is None:
        raise ValueError("essential variant requires domain result and capture policy")
    policy_fp = capture_policy.policy_fingerprint
    if isinstance(domain_submission, TerminalCommandDomainSubmissionFact):
        return TerminalCommandEssentialFact(
            capture_policy_fingerprint=policy_fp,
            command=domain_submission.command,
            status=domain_submission.status,
            exit_code=domain_submission.exit_code,
            cwd=domain_submission.cwd,
            timed_out=domain_submission.timed_out,
            output_truncated=domain_submission.output_truncated,
            error=_recapture_error(domain_submission.error, capture_policy),
            process_id=domain_submission.process_id,
            yielded_to_background=domain_submission.yielded_to_background,
            terminal_session_id=domain_submission.terminal_session_id,
            backend_type=domain_submission.backend_type,
            io_mode=domain_submission.io_mode,
            stdin_closed=domain_submission.stdin_closed,
            policy_code=domain_submission.policy_code,
            duration_seconds=domain_submission.duration_seconds,
        )
    if isinstance(domain_submission, TerminalCommandErrorDomainSubmissionFact):
        return TerminalCommandErrorEssentialFact(
            capture_policy_fingerprint=policy_fp,
            requested_command=domain_submission.requested_command,
            failure_stage=domain_submission.failure_stage,  # type: ignore[arg-type]
            status=domain_submission.status,
            error=_recapture_required_error(domain_submission.error, capture_policy),
            policy_code=domain_submission.policy_code,
            observed_cwd=domain_submission.observed_cwd,
            terminal_session_id=domain_submission.terminal_session_id,
            backend_type=domain_submission.backend_type,
            io_mode=domain_submission.io_mode,
        )
    if isinstance(domain_submission, TerminalProcessObservationDomainSubmissionFact):
        return TerminalProcessObservationEssentialFact(
            capture_policy_fingerprint=policy_fp,
            action=domain_submission.action,  # type: ignore[arg-type]
            process_id=domain_submission.process_id,
            status=domain_submission.status,
            exit_code=domain_submission.exit_code,
            command=domain_submission.command,
            cwd=domain_submission.cwd,
            timed_out=domain_submission.timed_out,
            output_truncated=domain_submission.output_truncated,
            error=_recapture_error(domain_submission.error, capture_policy),
            yielded_to_background=domain_submission.yielded_to_background,
            terminal_session_id=domain_submission.terminal_session_id,
            backend_type=domain_submission.backend_type,
            io_mode=domain_submission.io_mode,
            stdin_closed=domain_submission.stdin_closed,
            policy_code=domain_submission.policy_code,
            duration_seconds=domain_submission.duration_seconds,
        )
    if isinstance(domain_submission, TerminalProcessInventoryDomainSubmissionFact):
        return TerminalProcessInventoryEssentialFact(
            capture_policy_fingerprint=policy_fp,
            status=domain_submission.status,
            live_process_count=domain_submission.live_process_count,
            finished_process_count=domain_submission.finished_process_count,
            process_summaries=_recapture_process_summaries(
                domain_submission.process_summaries, capture_policy
            ),
            omitted_process_count=(
                domain_submission.omitted_process_count
                + max(
                    0,
                    len(domain_submission.process_summaries)
                    - capture_policy.max_process_summaries,
                )
            ),
            summaries_truncated=(
                domain_submission.summaries_truncated
                or len(domain_submission.process_summaries)
                > capture_policy.max_process_summaries
            ),
        )
    if isinstance(domain_submission, TerminalMonitorRegistrationDomainSubmissionFact):
        return TerminalMonitorRegistrationEssentialFact(
            capture_policy_fingerprint=policy_fp,
            process_id=domain_submission.process_id,
            monitor_id=domain_submission.monitor_id,
            expires_at_utc=domain_submission.expires_at_utc,
            status=domain_submission.status,
            exit_code=domain_submission.exit_code,
            output_truncated=domain_submission.output_truncated,
            terminal_session_id=domain_submission.terminal_session_id,
            backend_type=domain_submission.backend_type,
        )
    if isinstance(domain_submission, TerminalMonitorInventoryDomainSubmissionFact):
        visible_summaries = domain_submission.monitor_summaries[
            : capture_policy.max_process_summaries
        ]
        return TerminalMonitorInventoryEssentialFact(
            capture_policy_fingerprint=policy_fp,
            status=domain_submission.status,
            monitor_summaries=visible_summaries,
            omitted_monitor_count=(
                domain_submission.omitted_monitor_count
                + len(domain_submission.monitor_summaries)
                - len(visible_summaries)
            ),
            summaries_truncated=(
                domain_submission.summaries_truncated
                or len(visible_summaries) != len(domain_submission.monitor_summaries)
            ),
        )
    if isinstance(domain_submission, TerminalMonitorCancellationDomainSubmissionFact):
        return TerminalMonitorCancellationEssentialFact(
            capture_policy_fingerprint=policy_fp,
            monitor_id=domain_submission.monitor_id,
            outcome=domain_submission.outcome,
        )
    if isinstance(domain_submission, TerminalMonitorErrorDomainSubmissionFact):
        return TerminalMonitorErrorEssentialFact(
            capture_policy_fingerprint=policy_fp,
            requested_action=domain_submission.requested_action,
            process_id=domain_submission.process_id,
            monitor_id=domain_submission.monitor_id,
            status=domain_submission.status,
            error=_recapture_required_error(domain_submission.error, capture_policy),
            policy_code=domain_submission.policy_code,
        )
    if isinstance(domain_submission, TerminalProcessErrorDomainSubmissionFact):
        return TerminalProcessErrorEssentialFact(
            capture_policy_fingerprint=policy_fp,
            requested_action=domain_submission.requested_action,
            process_id=domain_submission.process_id,
            status=domain_submission.status,
            error=_recapture_required_error(domain_submission.error, capture_policy),
            policy_code=domain_submission.policy_code,
            terminal_session_id=domain_submission.terminal_session_id,
            backend_type=domain_submission.backend_type,
        )
    if isinstance(domain_submission, ArtifactDomainSubmissionFact):
        return ArtifactEssentialResultFact(
            capture_policy_fingerprint=policy_fp,
            primary_artifact_id=domain_submission.primary_artifact_id,
            output_truncated=domain_submission.output_truncated,
            output_preview_available=domain_submission.output_preview_available,
        )
    raise TypeError("unsupported external domain submission")


def _require_contract(
    descriptor: "CapabilityDescriptor",
) -> CapabilityResultRenderContractFact:
    return descriptor.result_render_contract


def _resolve_contract_binding(
    contract: CapabilityResultRenderContractFact,
    *,
    registry: ToolResultSemanticsBuilderRegistry,
) -> ToolResultSemanticsBuilderBinding:
    binding = registry.resolve_binding(
        contract.semantics_builder_id,
        contract.semantics_builder_version,
    )
    if binding.builder_contract != contract.semantics_builder_contract:
        raise ValueError("semantics builder binding contract mismatch")
    if (
        binding.builder_contract.contract_fingerprint
        != contract.semantics_builder_contract_fingerprint
    ):
        raise ValueError("semantics builder binding fingerprint mismatch")
    return binding


def _variant(
    contract: CapabilityResultRenderContractFact,
    code: ToolResultRenderVariantCode,
) -> CapabilityResultRenderVariantFact:
    matches = tuple(
        item for item in contract.allowed_variants if item.variant_code == code
    )
    if len(matches) != 1:
        raise ValueError(
            f"render contract does not contain exact variant {code.value!r}"
        )
    return matches[0]


def _profile(
    *,
    descriptor: "CapabilityDescriptor",
    attribution: CapabilityDescriptorRenderAttributionFact,
    contract: CapabilityResultRenderContractFact,
    variant: CapabilityResultRenderVariantFact,
) -> ToolResultRenderProfileFact:
    payload = {
        "profile_version": "tool-result-profile:v1",
        "selected_variant": variant,
        "render_contract": contract,
        "tool_origin": _tool_origin_for_profile(descriptor, variant),
        "descriptor_attribution": attribution,
        "render_contract_fingerprint": contract.contract_fingerprint,
    }
    return ToolResultRenderProfileFact(
        **payload,
        profile_fingerprint=context_fingerprint(
            "tool-result-render-profile:v1", payload
        ),
    )


def _tool_origin_for_profile(
    descriptor: "CapabilityDescriptor",
    variant: CapabilityResultRenderVariantFact,
) -> str:
    if variant.operational_kind.value.startswith("terminal_"):
        return "terminal"
    provider_kind = descriptor.provider_kind.value
    if provider_kind == "mcp":
        return "mcp"
    if provider_kind == "workflow":
        return "workflow"
    if provider_kind in {"skill", "custom"}:
        return "custom"
    if provider_kind in {"subagent", "subagent_system"}:
        return "subagent_system"
    return "builtin"


def tool_origin_for_descriptor_variant(
    descriptor: "CapabilityDescriptor",
    variant_code: ToolResultRenderVariantCode,
) -> str:
    contract = _require_contract(descriptor)
    return _tool_origin_for_profile(
        descriptor,
        _variant(contract, variant_code),
    )


def build_terminal_payload_timing(
    *,
    observed_at_utc: str,
    duration_seconds: float | None,
    freshness: str,
    clock_source: str,
    command_started_at_utc: str | None = None,
    process_started_at_utc: str | None = None,
    last_output_at_utc: str | None = None,
) -> TerminalPayloadTimingFact:
    timing_payload = {
        "observed_at_utc": canonical_utc_timestamp(observed_at_utc),
        "duration_seconds": (
            float(duration_seconds) if duration_seconds is not None else None
        ),
        "freshness": freshness,
        "clock_source": clock_source,
        "command_started_at_utc": (
            canonical_utc_timestamp(command_started_at_utc)
            if command_started_at_utc is not None
            else None
        ),
        "process_started_at_utc": (
            canonical_utc_timestamp(process_started_at_utc)
            if process_started_at_utc is not None
            else None
        ),
        "last_output_at_utc": (
            canonical_utc_timestamp(last_output_at_utc)
            if last_output_at_utc is not None
            else None
        ),
    }
    return TerminalPayloadTimingFact(
        **timing_payload,
        timing_fingerprint=context_fingerprint(
            "terminal-payload-timing:v1", timing_payload
        ),
    )


def _recapture_process_summaries(
    summaries: tuple[TerminalProcessSummaryFact, ...],
    policy: ToolResultEssentialCapturePolicyFact,
) -> tuple[TerminalProcessSummaryFact, ...]:
    captured = []
    for item in summaries[: policy.max_process_summaries]:
        summary_payload = {
            "process_id": item.process_id,
            "status": item.status,
            "exit_code": item.exit_code,
            "command": _bounded_optional(
                item.command, policy.max_process_command_chars
            ),
            "cwd": _bounded_optional(item.cwd, policy.max_process_cwd_chars),
            "terminal_session_id": item.terminal_session_id,
            "backend_type": item.backend_type,
            "io_mode": item.io_mode,
            "timed_out": item.timed_out,
            "stdin_closed": item.stdin_closed,
            "duration_seconds": item.duration_seconds,
        }
        captured.append(
            TerminalProcessSummaryFact(
                **summary_payload,
                summary_fingerprint=context_fingerprint(
                    "terminal-process-summary:v1", summary_payload
                ),
            )
        )
    return tuple(captured)


def _recapture_error(
    value: ToolResultErrorPreviewFact | None,
    policy: ToolResultEssentialCapturePolicyFact,
) -> ToolResultErrorPreviewFact | None:
    if value is None:
        return None
    text = value.text[: policy.max_error_chars]
    return ToolResultErrorPreviewFact(
        text=text,
        original_chars=value.original_chars,
        truncated=len(text) < value.original_chars,
    )


def _recapture_required_error(
    value: ToolResultErrorPreviewFact,
    policy: ToolResultEssentialCapturePolicyFact,
) -> ToolResultErrorPreviewFact:
    captured = _recapture_error(value, policy)
    assert captured is not None
    return captured


def _bounded_optional(value: object, cap: int) -> str | None:
    return str(value)[:cap] if value is not None else None


def _argument_string(
    arguments: FrozenJsonObjectFact | None,
    key: str,
) -> str | None:
    if arguments is None:
        return None
    value = thaw_json(arguments).get(key)
    return str(value) if value is not None else None


def _str_or_none(value: object) -> str | None:
    return str(value) if value is not None else None


__all__ = [
    "DeclarativeToolResultSemanticsBuilder",
    "ToolResultSemanticsBuilder",
    "ToolResultSemanticsBuilderBinding",
    "ToolResultSemanticsBuilderRegistry",
    "ToolResultSemanticsRuntimeInput",
    "build_execution_semantics",
    "build_default_tool_result_semantics_registry",
    "build_pre_execution_denial_semantics",
    "FrozenToolResultSemanticsRuntimeInput",
    "build_adapter_failure_runtime_input",
    "build_terminal_payload_timing",
    "build_unknown_result_semantics",
    "tool_origin_for_descriptor_variant",
    "default_essential_capture_policy",
    "unbounded_error_preview",
]
