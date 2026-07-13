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
    ToolResultSemanticsBuilderContractFact,
    ToolResultStateFact,
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
    return CapabilityResultRenderVariantFact(
        **payload,
        variant_fingerprint=context_fingerprint(
            "tool-result-render-variant:v1", payload
        ),
    )


def _contract(
    *,
    builder_id: str,
    builder_version: str,
    variants: tuple[CapabilityResultRenderVariantFact, ...],
    denial: ToolResultRenderVariantCode,
) -> CapabilityResultRenderContractFact:
    builder_payload = {
        "schema_version": "tool-result-semantics-builder-contract:v1",
        "builder_id": builder_id,
        "builder_version": builder_version,
        "input_schema_fingerprints": (
            "schema:frozen-json-object:v1",
            "schema:tool-result-runtime-input:v1",
            "schema:tool-result-domain-submission:v1",
            "schema:tool-observation-timing:v1",
            "schema:terminal-payload-timing:v1",
            "schema:tool-result-essential-capture-policy:v1",
        ),
        "output_schema_fingerprint": "schema:tool-result-execution-semantics:v1",
        "variant_table_fingerprint": context_fingerprint(
            "tool-result-variant-table:v1",
            [item.model_dump(mode="json") for item in variants],
        ),
        "classifier_policy_fingerprint": context_fingerprint(
            "tool-result-classifier-policy:v1",
            [
                builder_id,
                builder_version,
                tuple(item.variant_code for item in variants),
            ],
        ),
        "normalization_contract_versions": (
            "arguments:v1",
            "terminal-domain:v1",
            "capture:v1",
            "profile:v1",
        ),
    }
    builder = ToolResultSemanticsBuilderContractFact(
        **builder_payload,
        contract_fingerprint=context_fingerprint(
            "tool-result-semantics-builder-contract:v1", builder_payload
        ),
    )
    operational = tuple(sorted({item.operational_kind for item in variants}, key=str))
    essential = tuple(
        sorted({item.essential_envelope_kind for item in variants}, key=str)
    )
    payload = {
        "allowed_operational_kinds": operational,
        "allowed_essential_envelope_kinds": essential,
        "allowed_variants": variants,
        "semantics_builder_id": builder_id,
        "semantics_builder_version": builder_version,
        "semantics_builder_contract": builder,
        "semantics_builder_contract_fingerprint": builder.contract_fingerprint,
        "pre_execution_denial_variant_code": denial,
    }
    return CapabilityResultRenderContractFact(
        **payload,
        contract_fingerprint=context_fingerprint(
            "capability-result-render-contract:v1", payload
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
        builder_id="tool-result-semantics:generic",
        builder_version="1",
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
        builder_id="tool-result-semantics:terminal-command",
        builder_version="1",
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
        builder_id="tool-result-semantics:terminal-process",
        builder_version="1",
        variants=variants,
        denial=ToolResultRenderVariantCode.TERMINAL_PROCESS_ERROR,
    )


def result_render_contract_for_tool(name: str) -> CapabilityResultRenderContractFact:
    if name == "terminal":
        return terminal_result_render_contract()
    if name == "terminal_process":
        return terminal_process_result_render_contract()
    return generic_result_render_contract()


__all__ = [
    "generic_result_render_contract",
    "result_render_contract_for_tool",
    "terminal_process_result_render_contract",
    "terminal_result_render_contract",
]
