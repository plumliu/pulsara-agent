"""Typed external tool-result ingress bound to committed requirements."""

from __future__ import annotations

import json
from dataclasses import dataclass

from pulsara_agent.capability.result_semantics import (
    ToolResultSemanticsBuilderRegistry,
)
from pulsara_agent.event import RequireExternalExecutionEvent
from pulsara_agent.message import ToolResultBlock
from pulsara_agent.primitives.context import (
    FrozenJsonObjectFact,
    context_fingerprint,
    freeze_json,
)
from pulsara_agent.primitives.tool_observation import ToolObservationTimingFact
from pulsara_agent.primitives.tool_result import (
    ExternalExecutionRequirementReferenceFact,
    ExternalToolCallRequirementFact,
    ExternalToolResultIngressFact,
    ExternalToolResultSubmissionFact,
    FrozenToolResultBlockFact,
    TerminalPayloadTimingFact,
    ToolResultDomainSubmissionFact,
    ToolResultRenderVariantCode,
    ToolResultStateFact,
)
from pulsara_agent.runtime.context_input.event_slice import FrozenStoredEvent


@dataclass(frozen=True, slots=True)
class _ProviderKindView:
    value: str


@dataclass(frozen=True, slots=True)
class _ExternalDescriptorView:
    id: str
    name: str
    provider_kind: _ProviderKindView
    result_render_contract: object


class ExternalToolResultIngressBuilder:
    def __init__(self, registry: ToolResultSemanticsBuilderRegistry) -> None:
        if not registry.frozen:
            raise ValueError("external ingress requires a frozen semantics registry")
        self._registry = registry

    def bind_submission(
        self,
        *,
        requirement_event: RequireExternalExecutionEvent,
        requirement: ExternalToolCallRequirementFact,
        submission: ExternalToolResultSubmissionFact,
        owner_runtime_session_id: str,
    ) -> ExternalToolResultIngressFact:
        if requirement_event.sequence is None:
            raise ValueError("external requirement event must be full committed")
        matches = tuple(
            item
            for item in requirement_event.external_tool_calls
            if item.tool_call_id == requirement.tool_call_id
        )
        if len(matches) != 1 or matches[0] != requirement:
            raise ValueError("external submission requirement identity mismatch")
        block = submission.result_block
        if (
            block.tool_call_id != requirement.tool_call_id
            or block.model_tool_name != requirement.model_tool_name
        ):
            raise ValueError("external result identity differs from requirement")
        variants = tuple(
            item
            for item in requirement.result_render_contract.allowed_variants
            if item.variant_code == submission.selected_variant_code
        )
        if len(variants) != 1 or variants[0].execution_phase != "post_execution":
            raise ValueError("external result variant is not allowed")
        variant = variants[0]
        binding = self._registry.resolve_binding(
            requirement.result_render_contract.semantics_builder_id,
            requirement.result_render_contract.semantics_builder_version,
        )
        if binding.builder_contract != (
            requirement.result_render_contract.semantics_builder_contract
        ):
            raise ValueError("external semantics builder contract mismatch")
        descriptor = _ExternalDescriptorView(
            id=requirement.descriptor_attribution.descriptor_id,
            name=requirement.model_tool_name,
            provider_kind=_ProviderKindView(requirement.tool_origin),
            result_render_contract=requirement.result_render_contract,
        )
        normalized_arguments = _normalized_arguments(requirement.raw_arguments_json)
        semantics = binding.builder.build(
            descriptor=descriptor,
            descriptor_attribution=requirement.descriptor_attribution,
            selected_variant=variant,
            normalized_arguments=normalized_arguments,
            typed_result=None,
            domain_submission=submission.domain_result,
            observation_timing=submission.observation_timing,
            terminal_payload_timing=submission.terminal_payload_timing,
            essential_capture_policy=requirement.essential_capture_policy,
            result_state=block.result_state,
        )
        if semantics.render_profile.tool_origin != requirement.tool_origin:
            raise ValueError("external semantics tool origin mismatch")
        frozen_event = FrozenStoredEvent.from_stored_event(requirement_event)
        reference = ExternalExecutionRequirementReferenceFact(
            owner_runtime_session_id=owner_runtime_session_id,
            require_event_id=requirement_event.id,
            require_event_sequence=requirement_event.sequence,
            require_event_payload_fingerprint=frozen_event.payload_fingerprint,
            tool_call_id=requirement.tool_call_id,
            requirement_fingerprint=requirement.requirement_fingerprint,
        )
        payload = {
            "requirement_ref": reference,
            "result_block": block,
            "observation_timing": submission.observation_timing,
            "execution_semantics": semantics,
        }
        return ExternalToolResultIngressFact(
            **payload,
            ingress_fingerprint=context_fingerprint(
                "external-tool-result-ingress:v1", payload
            ),
        )


def freeze_external_tool_result_submission(
    *,
    result_block: ToolResultBlock,
    observation_timing: ToolObservationTimingFact,
    selected_variant_code: ToolResultRenderVariantCode,
    domain_result: ToolResultDomainSubmissionFact | None,
    terminal_payload_timing: TerminalPayloadTimingFact | None,
) -> ExternalToolResultSubmissionFact:
    frozen_payload = freeze_json(result_block.model_dump(mode="json"))
    if not isinstance(frozen_payload, FrozenJsonObjectFact):
        raise AssertionError("tool result block must freeze as an object")
    frozen_block = FrozenToolResultBlockFact(
        tool_call_id=result_block.id,
        model_tool_name=result_block.name,
        result_state=ToolResultStateFact(result_block.state.value),
        canonical_block_payload=frozen_payload,
        block_payload_fingerprint=context_fingerprint(
            "tool-result-block:v1", frozen_payload
        ),
    )
    payload = {
        "result_block": frozen_block,
        "observation_timing": observation_timing,
        "selected_variant_code": selected_variant_code,
        "domain_result": domain_result,
        "terminal_payload_timing": terminal_payload_timing,
    }
    return ExternalToolResultSubmissionFact(
        **payload,
        submission_fingerprint=context_fingerprint(
            "external-tool-result-submission:v1", payload
        ),
    )


def _normalized_arguments(raw: str) -> FrozenJsonObjectFact | None:
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return None
    frozen = freeze_json(parsed)
    return frozen if isinstance(frozen, FrozenJsonObjectFact) else None


__all__ = [
    "ExternalToolResultIngressBuilder",
    "freeze_external_tool_result_submission",
]
