"""Canonical declarative result-render contracts for capability descriptors."""

from __future__ import annotations

from functools import lru_cache

from pulsara_agent.primitives.context import context_fingerprint
from pulsara_agent.primitives.tool_result import (
    CapabilityResultRenderContractFact,
    CapabilityResultRenderVariantFact,
    ToolResultEssentialEnvelopeKind,
    ToolResultOperationalKind,
    ToolResultRenderVariantCode,
    ToolResultStateFact,
    capability_result_render_variant_identity_payload,
)


def _variant(
    *,
    code: ToolResultRenderVariantCode,
    operational: ToolResultOperationalKind,
    essential: ToolResultEssentialEnvelopeKind,
    states: tuple[ToolResultStateFact, ...],
    phase: str,
    timing: str,
) -> CapabilityResultRenderVariantFact:
    payload = {
        "variant_code": code,
        "operational_kind": operational,
        "essential_envelope_kind": essential,
        "allowed_result_states": states,
        "execution_phase": phase,
        "terminal_payload_timing_requirement": timing,
    }
    return CapabilityResultRenderVariantFact(**payload)


def _contract(
    *,
    variants: tuple[CapabilityResultRenderVariantFact, ...],
    denial: ToolResultRenderVariantCode,
) -> CapabilityResultRenderContractFact:
    operational = tuple(sorted({item.operational_kind for item in variants}, key=str))
    essential = tuple(
        sorted({item.essential_envelope_kind for item in variants}, key=str)
    )
    payload = {
        "allowed_operational_kinds": operational,
        "allowed_essential_envelope_kinds": essential,
        "allowed_variants": variants,
        "pre_execution_denial_variant_code": denial,
    }
    identity_payload = {
        **payload,
        "allowed_operational_kinds": tuple(item.value for item in operational),
        "allowed_essential_envelope_kinds": tuple(item.value for item in essential),
        "allowed_variants": tuple(
            capability_result_render_variant_identity_payload(item)
            for item in variants
        ),
        "pre_execution_denial_variant_code": denial.value,
    }
    return CapabilityResultRenderContractFact(
        **payload,
        contract_fingerprint=context_fingerprint(
            "capability-result-render-contract:v1", identity_payload
        ),
    )


@lru_cache(maxsize=1)
def generic_result_render_contract() -> CapabilityResultRenderContractFact:
    variants = (
        _variant(
            code=ToolResultRenderVariantCode.GENERIC_RESULT,
            operational=ToolResultOperationalKind.GENERIC,
            essential=ToolResultEssentialEnvelopeKind.NONE,
            states=(
                ToolResultStateFact.ERROR,
                ToolResultStateFact.INTERRUPTED,
                ToolResultStateFact.SUCCESS,
            ),
            phase="executed",
            timing="forbidden",
        ),
        _variant(
            code=ToolResultRenderVariantCode.GENERIC_DENIED,
            operational=ToolResultOperationalKind.GENERIC,
            essential=ToolResultEssentialEnvelopeKind.NONE,
            states=(ToolResultStateFact.DENIED, ToolResultStateFact.ERROR),
            phase="pre_execution",
            timing="forbidden",
        ),
        _variant(
            code=ToolResultRenderVariantCode.EXTERNAL_GENERIC_RESULT,
            operational=ToolResultOperationalKind.GENERIC,
            essential=ToolResultEssentialEnvelopeKind.NONE,
            states=(
                ToolResultStateFact.ERROR,
                ToolResultStateFact.INTERRUPTED,
                ToolResultStateFact.SUCCESS,
            ),
            phase="post_execution",
            timing="forbidden",
        ),
    )
    return _contract(
        variants=variants,
        denial=ToolResultRenderVariantCode.GENERIC_DENIED,
    )


@lru_cache(maxsize=1)
def terminal_result_render_contract() -> CapabilityResultRenderContractFact:
    variants = (
        _variant(
            code=ToolResultRenderVariantCode.TERMINAL_COMMAND_EXECUTED,
            operational=ToolResultOperationalKind.TERMINAL_COMMAND,
            essential=ToolResultEssentialEnvelopeKind.TERMINAL_COMMAND,
            states=(
                ToolResultStateFact.ERROR,
                ToolResultStateFact.INTERRUPTED,
                ToolResultStateFact.SUCCESS,
            ),
            phase="executed",
            timing="required",
        ),
        _variant(
            code=ToolResultRenderVariantCode.TERMINAL_COMMAND_MALFORMED_ARGUMENTS,
            operational=ToolResultOperationalKind.TERMINAL_COMMAND_ERROR,
            essential=ToolResultEssentialEnvelopeKind.TERMINAL_COMMAND_ERROR,
            states=(ToolResultStateFact.ERROR,),
            phase="pre_execution",
            timing="forbidden",
        ),
        _variant(
            code=ToolResultRenderVariantCode.TERMINAL_COMMAND_DENIED,
            operational=ToolResultOperationalKind.TERMINAL_COMMAND_ERROR,
            essential=ToolResultEssentialEnvelopeKind.TERMINAL_COMMAND_ERROR,
            states=(ToolResultStateFact.DENIED, ToolResultStateFact.ERROR),
            phase="pre_execution",
            timing="forbidden",
        ),
        _variant(
            code=ToolResultRenderVariantCode.TERMINAL_COMMAND_ADAPTER_ERROR,
            operational=ToolResultOperationalKind.TERMINAL_COMMAND_ERROR,
            essential=ToolResultEssentialEnvelopeKind.TERMINAL_COMMAND_ERROR,
            states=(ToolResultStateFact.ERROR,),
            phase="pre_execution",
            timing="forbidden",
        ),
        _variant(
            code=ToolResultRenderVariantCode.EXTERNAL_TERMINAL_RESULT,
            operational=ToolResultOperationalKind.TERMINAL_COMMAND,
            essential=ToolResultEssentialEnvelopeKind.TERMINAL_COMMAND,
            states=(
                ToolResultStateFact.ERROR,
                ToolResultStateFact.INTERRUPTED,
                ToolResultStateFact.SUCCESS,
            ),
            phase="post_execution",
            timing="required",
        ),
    )
    return _contract(
        variants=variants,
        denial=ToolResultRenderVariantCode.TERMINAL_COMMAND_DENIED,
    )


@lru_cache(maxsize=1)
def terminal_process_result_render_contract() -> CapabilityResultRenderContractFact:
    variants = (
        _variant(
            code=ToolResultRenderVariantCode.TERMINAL_PROCESS_INVENTORY,
            operational=ToolResultOperationalKind.TERMINAL_PROCESS_INVENTORY,
            essential=ToolResultEssentialEnvelopeKind.TERMINAL_PROCESS_INVENTORY,
            states=(ToolResultStateFact.SUCCESS,),
            phase="executed",
            timing="required",
        ),
        _variant(
            code=ToolResultRenderVariantCode.TERMINAL_PROCESS_OBSERVATION,
            operational=ToolResultOperationalKind.TERMINAL_PROCESS_OBSERVATION,
            essential=ToolResultEssentialEnvelopeKind.TERMINAL_PROCESS_OBSERVATION,
            states=(ToolResultStateFact.INTERRUPTED, ToolResultStateFact.SUCCESS),
            phase="executed",
            timing="required",
        ),
        _variant(
            code=ToolResultRenderVariantCode.TERMINAL_PROCESS_ERROR,
            operational=ToolResultOperationalKind.TERMINAL_PROCESS_ERROR,
            essential=ToolResultEssentialEnvelopeKind.TERMINAL_PROCESS_ERROR,
            states=(ToolResultStateFact.DENIED, ToolResultStateFact.ERROR),
            phase="pre_execution",
            timing="forbidden",
        ),
        _variant(
            code=ToolResultRenderVariantCode.TERMINAL_PROCESS_ADAPTER_ERROR,
            operational=ToolResultOperationalKind.TERMINAL_PROCESS_ERROR,
            essential=ToolResultEssentialEnvelopeKind.TERMINAL_PROCESS_ERROR,
            states=(ToolResultStateFact.ERROR,),
            phase="executed",
            timing="optional",
        ),
        _variant(
            code=ToolResultRenderVariantCode.EXTERNAL_TERMINAL_RESULT,
            operational=ToolResultOperationalKind.TERMINAL_PROCESS_OBSERVATION,
            essential=ToolResultEssentialEnvelopeKind.TERMINAL_PROCESS_OBSERVATION,
            states=(
                ToolResultStateFact.ERROR,
                ToolResultStateFact.INTERRUPTED,
                ToolResultStateFact.SUCCESS,
            ),
            phase="post_execution",
            timing="optional",
        ),
    )
    return _contract(
        variants=variants,
        denial=ToolResultRenderVariantCode.TERMINAL_PROCESS_ERROR,
    )


def result_render_contract_for_tool(name: str) -> CapabilityResultRenderContractFact:
    if name == "exec_process":
        return terminal_result_render_contract()
    if name == "manage_process":
        return terminal_process_result_render_contract()
    return generic_result_render_contract()


__all__ = [
    "generic_result_render_contract",
    "result_render_contract_for_tool",
    "terminal_process_result_render_contract",
    "terminal_result_render_contract",
]
