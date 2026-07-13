from __future__ import annotations

import math
from dataclasses import replace
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from pulsara_agent.capability.result_semantics import (
    DeclarativeToolResultSemanticsBuilder,
    FrozenToolResultSemanticsRuntimeInput,
    ToolResultSemanticsBuilderBinding,
    ToolResultSemanticsBuilderRegistry,
    build_execution_semantics,
    build_pre_execution_denial_semantics,
    build_default_tool_result_semantics_registry,
    build_terminal_payload_timing,
)
from pulsara_agent.capability.result_contracts import terminal_result_render_contract
from pulsara_agent.primitives.context import (
    FrozenJsonArrayFact,
    FrozenJsonObjectFact,
    ToolArgumentsParseErrorCode,
    TranscriptToolCallFact,
    canonical_json_bytes,
    context_fingerprint,
    freeze_json,
    thaw_json,
)
from pulsara_agent.primitives.tool_observation import ToolObservationTimingFact
from pulsara_agent.primitives.tool_result import (
    CapabilityDescriptorRenderAttributionFact,
    CapabilityResultRenderContractFact,
    CapabilityResultRenderVariantFact,
    ExternalToolCallRequirementFact,
    TerminalCommandEssentialFact,
    TerminalCommandErrorEssentialFact,
    TerminalCommandDomainSubmissionFact,
    ToolResultEssentialCapturePolicyFact,
    ToolResultEssentialEnvelopeKind,
    ToolResultErrorPreviewFact,
    ToolResultExecutionSemanticsFact,
    ToolResultOperationalKind,
    ToolResultRenderProfileFact,
    ToolResultRenderVariantCode,
    ToolResultSemanticsBuilderContractFact,
    ToolResultStateFact,
    validate_tool_result_profile_contract,
)
from pulsara_agent.runtime.context_input import resolve_context_compile_policy
from pulsara_agent.runtime.context_input import (
    ExternalToolResultIngressBuilder,
    freeze_external_tool_result_submission,
)
from pulsara_agent.runtime.state import LoopBudget
from pulsara_agent.event import EventContext, RequireExternalExecutionEvent
from pulsara_agent.event_log import InMemoryEventLog
from pulsara_agent.message import TextBlock, ToolResultBlock, ToolResultState
from pulsara_agent.tools.base import ToolCall, ToolExecutionResult
from tests.conftest import external_tool_call_requirement_fact


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


def _builder_contract(
    variants: tuple[CapabilityResultRenderVariantFact, ...],
    *,
    builder_id: str = "builder:test",
    builder_version: str = "1",
) -> ToolResultSemanticsBuilderContractFact:
    payload = {
        "schema_version": "tool-result-semantics-builder-contract:v1",
        "builder_id": builder_id,
        "builder_version": builder_version,
        "input_schema_fingerprints": tuple(f"schema:{index}" for index in range(6)),
        "output_schema_fingerprint": "schema:output",
        "variant_table_fingerprint": context_fingerprint(
            "tool-result-variant-table:v1",
            [variant.model_dump(mode="json") for variant in variants],
        ),
        "classifier_policy_fingerprint": "policy:classifier",
        "normalization_contract_versions": ("arguments:v1", "capture:v1"),
    }
    return ToolResultSemanticsBuilderContractFact(
        **payload,
        contract_fingerprint=context_fingerprint(
            "tool-result-semantics-builder-contract:v1", payload
        ),
    )


def _render_contract() -> CapabilityResultRenderContractFact:
    denial = _variant(
        code=ToolResultRenderVariantCode.GENERIC_DENIED,
        operational=ToolResultOperationalKind.GENERIC,
        essential=ToolResultEssentialEnvelopeKind.NONE,
        states=(ToolResultStateFact.DENIED, ToolResultStateFact.ERROR),
        phase="pre_execution",
        timing="forbidden",
    )
    external = _variant(
        code=ToolResultRenderVariantCode.EXTERNAL_TERMINAL_RESULT,
        operational=ToolResultOperationalKind.TERMINAL_COMMAND,
        essential=ToolResultEssentialEnvelopeKind.TERMINAL_COMMAND,
        states=(ToolResultStateFact.SUCCESS,),
        phase="post_execution",
        timing="required",
    )
    variants = (denial, external)
    builder = _builder_contract(variants)
    payload = {
        "allowed_operational_kinds": tuple(
            sorted({item.operational_kind for item in variants}, key=str)
        ),
        "allowed_essential_envelope_kinds": tuple(
            sorted({item.essential_envelope_kind for item in variants}, key=str)
        ),
        "allowed_variants": variants,
        "semantics_builder_id": builder.builder_id,
        "semantics_builder_version": builder.builder_version,
        "semantics_builder_contract": builder,
        "semantics_builder_contract_fingerprint": builder.contract_fingerprint,
        "pre_execution_denial_variant_code": denial.variant_code,
    }
    return CapabilityResultRenderContractFact(
        **payload,
        contract_fingerprint=context_fingerprint(
            "capability-result-render-contract:v1", payload
        ),
    )


def _capture_policy() -> ToolResultEssentialCapturePolicyFact:
    payload = {
        "policy_version": "capture:v1",
        "max_error_chars": 512,
        "max_process_summaries": 8,
        "max_process_command_chars": 256,
        "max_process_cwd_chars": 256,
    }
    return ToolResultEssentialCapturePolicyFact(
        **payload,
        policy_fingerprint=context_fingerprint(
            "tool-result-essential-capture-policy:v1", payload
        ),
    )


def _attribution(
    contract: CapabilityResultRenderContractFact,
) -> CapabilityDescriptorRenderAttributionFact:
    payload = {
        "owner_runtime_session_id": "runtime:owner",
        "exposure_id": "exposure:1",
        "exposure_fact_fingerprint": "exposure:fp",
        "descriptor_set_fingerprint": "descriptors:fp",
        "descriptor_id": "descriptor:terminal",
        "descriptor_fingerprint": "descriptor:fp",
        "result_render_contract_fingerprint": contract.contract_fingerprint,
        "descriptor_source_event_id": "event:exposure",
        "descriptor_source_sequence": 3,
        "descriptor_source_payload_fingerprint": "event:fp",
    }
    return CapabilityDescriptorRenderAttributionFact(
        **payload,
        attribution_fingerprint=context_fingerprint(
            "capability-descriptor-render-attribution:v1", payload
        ),
    )


def _profile(
    contract: CapabilityResultRenderContractFact,
    variant: CapabilityResultRenderVariantFact,
) -> ToolResultRenderProfileFact:
    payload = {
        "profile_version": "tool-result-profile:v1",
        "selected_variant": variant,
        "render_contract": contract,
        "tool_origin": "terminal",
        "descriptor_attribution": _attribution(contract),
        "render_contract_fingerprint": contract.contract_fingerprint,
    }
    return ToolResultRenderProfileFact(
        **payload,
        profile_fingerprint=context_fingerprint(
            "tool-result-render-profile:v1", payload
        ),
    )


class _Builder:
    builder_id = "builder:test"
    builder_version = "1"

    def build(self, **_kwargs):  # pragma: no cover - registry contract only
        raise NotImplementedError


class _CountingBuilder:
    def __init__(self, *, builder_id: str, builder_version: str) -> None:
        self.builder_id = builder_id
        self.builder_version = builder_version
        self.calls = 0
        self._delegate = DeclarativeToolResultSemanticsBuilder(
            builder_id=builder_id,
            builder_version=builder_version,
        )

    def build(self, **kwargs):
        self.calls += 1
        return self._delegate.build(**kwargs)


def test_frozen_json_is_recursive_deterministic_and_returns_owned_thaws() -> None:
    frozen = freeze_json({"z": [1, {"b": True}], "a": "text"})
    assert isinstance(frozen, FrozenJsonObjectFact)
    assert tuple(entry.key for entry in frozen.entries) == ("a", "z")
    nested = frozen.entries[1].value
    assert isinstance(nested, FrozenJsonArrayFact)

    first = thaw_json(frozen)
    second = thaw_json(frozen)
    first["z"][1]["b"] = False
    assert second["z"][1]["b"] is True
    assert canonical_json_bytes(frozen) == b'{"a":"text","z":[1,{"b":true}]}'


@pytest.mark.parametrize("value", [math.nan, math.inf, -math.inf])
def test_frozen_json_rejects_non_finite_numbers(value: float) -> None:
    with pytest.raises(ValueError, match="finite"):
        freeze_json({"value": value})


def test_tool_observation_timing_normalizes_utc_and_rejects_negative() -> None:
    fact = ToolObservationTimingFact(
        observed_at_utc="2026-07-12T08:00:00+08:00",
        observation_duration_seconds=1.5,
    )
    assert fact.observed_at_utc == "2026-07-12T00:00:00.000000Z"
    with pytest.raises(ValidationError):
        ToolObservationTimingFact(
            observed_at_utc="2026-07-12T00:00:00Z",
            observation_duration_seconds=-1,
        )


def test_malformed_tool_arguments_preserve_raw_provider_string() -> None:
    call = TranscriptToolCallFact(
        tool_call_id="call:1",
        model_tool_name="terminal",
        raw_arguments_json='{ "command": ',
        arguments_status="invalid_json",
        parsed_arguments=None,
        parse_error_code=ToolArgumentsParseErrorCode.INVALID_JSON_SYNTAX,
        state="finished",
        source_events=(),
    )
    assert call.raw_arguments_json == '{ "command": '
    with pytest.raises(ValidationError):
        TranscriptToolCallFact(
            **call.model_dump(exclude={"parsed_arguments"}),
            parsed_arguments=freeze_json({"command": "pwd"}),
        )


def test_render_contract_rejects_denial_variant_with_executed_semantics() -> None:
    contract = _render_contract()
    denial = contract.allowed_variants[0]
    invalid = denial.model_copy(
        update={
            "execution_phase": "executed",
            "variant_fingerprint": context_fingerprint(
                "tool-result-render-variant:v1",
                {
                    **denial.model_dump(mode="json", exclude={"variant_fingerprint"}),
                    "execution_phase": "executed",
                },
            ),
        }
    )
    payload = contract.model_dump(mode="python", exclude={"contract_fingerprint"})
    payload["allowed_variants"] = (invalid, *contract.allowed_variants[1:])
    payload["semantics_builder_contract"] = _builder_contract(
        payload["allowed_variants"]
    )
    payload["semantics_builder_contract_fingerprint"] = payload[
        "semantics_builder_contract"
    ].contract_fingerprint
    with pytest.raises(ValidationError, match="denial variant must be pre-execution"):
        CapabilityResultRenderContractFact(
            **payload,
            contract_fingerprint=context_fingerprint(
                "capability-result-render-contract:v1", payload
            ),
        )


def test_registry_binds_declarative_contract_not_build_identity() -> None:
    contract = _render_contract().semantics_builder_contract
    registry = ToolResultSemanticsBuilderRegistry()
    binding = ToolResultSemanticsBuilderBinding(
        builder_id=contract.builder_id,
        builder_version=contract.builder_version,
        builder_contract=contract,
        implementation_build_fingerprint="build:one",
        builder=_Builder(),
    )
    registry.register(binding)
    registry.freeze()
    assert registry.resolve_binding("builder:test", "1") is binding
    assert (
        replace(binding, implementation_build_fingerprint="build:two").builder_contract
        == contract
    )
    with pytest.raises(RuntimeError, match="frozen"):
        registry.register(binding)


def test_pre_execution_denial_invokes_resolved_semantics_builder() -> None:
    contract = terminal_result_render_contract()
    builder = _CountingBuilder(
        builder_id=contract.semantics_builder_id,
        builder_version=contract.semantics_builder_version,
    )
    registry = ToolResultSemanticsBuilderRegistry()
    registry.register(
        ToolResultSemanticsBuilderBinding(
            builder_id=builder.builder_id,
            builder_version=builder.builder_version,
            builder_contract=contract.semantics_builder_contract,
            implementation_build_fingerprint="build:test",
            builder=builder,
        )
    )
    registry.freeze()
    descriptor = SimpleNamespace(
        id="descriptor:terminal",
        result_render_contract=contract,
        provider_kind=SimpleNamespace(value="builtin"),
    )
    arguments = freeze_json({"command": "rm -rf build"})
    assert isinstance(arguments, FrozenJsonObjectFact)

    semantics = build_pre_execution_denial_semantics(
        descriptor=descriptor,
        descriptor_attribution=_attribution(contract),
        requested_arguments=arguments,
        message="permission denied",
        result_state=ToolResultStateFact.DENIED,
        reason_code="permission_denied",
        failure_stage="permission_denied",
        capture_policy=_capture_policy(),
        registry=registry,
        observation_timing=ToolObservationTimingFact(
            observed_at_utc="2026-07-12T00:00:00Z",
            freshness="current_tool_observation",
            clock_source="tool_result_events",
            tool_origin="terminal",
            tool_name="terminal",
            tool_call_id="call:denied",
        ),
    )

    assert builder.calls == 1
    assert isinstance(semantics.essential_result, TerminalCommandErrorEssentialFact)
    assert semantics.render_profile.tool_origin == "terminal"


def test_registry_rejects_same_version_with_different_contract() -> None:
    render_contract = _render_contract()
    first = render_contract.semantics_builder_contract
    variants = render_contract.allowed_variants
    changed = _builder_contract(variants)
    changed_payload = changed.model_dump(
        mode="python", exclude={"contract_fingerprint"}
    )
    changed_payload["classifier_policy_fingerprint"] = "policy:changed"
    changed = ToolResultSemanticsBuilderContractFact(
        **changed_payload,
        contract_fingerprint=context_fingerprint(
            "tool-result-semantics-builder-contract:v1", changed_payload
        ),
    )
    registry = ToolResultSemanticsBuilderRegistry()
    registry.register(
        ToolResultSemanticsBuilderBinding(
            builder_id=first.builder_id,
            builder_version=first.builder_version,
            builder_contract=first,
            implementation_build_fingerprint=None,
            builder=_Builder(),
        )
    )
    with pytest.raises(ValueError, match="different contract"):
        registry.register(
            ToolResultSemanticsBuilderBinding(
                builder_id=changed.builder_id,
                builder_version=changed.builder_version,
                builder_contract=changed,
                implementation_build_fingerprint=None,
                builder=_Builder(),
            )
        )


def test_declarative_builder_contract_rejects_handwritten_fingerprint() -> None:
    contract = _render_contract().semantics_builder_contract
    with pytest.raises(ValidationError, match="contract_fingerprint mismatch"):
        ToolResultSemanticsBuilderContractFact(
            **contract.model_dump(exclude={"contract_fingerprint"}),
            contract_fingerprint="sha256:" + "0" * 64,
        )


def test_pre_execution_terminal_error_needs_no_execution_identity_or_timing() -> None:
    contract = _render_contract()
    denial = contract.allowed_variants[0]
    profile = _profile(contract, denial)
    validate_tool_result_profile_contract(profile=profile, contract=contract)
    policy = _capture_policy()
    essential = TerminalCommandErrorEssentialFact(
        capture_policy_fingerprint=policy.policy_fingerprint,
        requested_command="rm -rf build",
        failure_stage="permission_denied",
        status="denied",
        error=ToolResultErrorPreviewFact(
            text="permission denied", original_chars=17, truncated=False
        ),
        policy_code="permission_denied",
        observed_cwd=None,
        terminal_session_id=None,
        backend_type=None,
        io_mode=None,
    )
    semantics = ToolResultExecutionSemanticsFact(
        render_profile=profile,
        result_state=ToolResultStateFact.DENIED,
        essential_capture_policy=None,
        essential_result=None,
        terminal_payload_timing=None,
    )
    assert essential.execution_started is False
    assert semantics.terminal_payload_timing is None


def test_result_state_cannot_select_incompatible_variant() -> None:
    contract = _render_contract()
    denial_profile = _profile(contract, contract.allowed_variants[0])
    with pytest.raises(ValidationError, match="state is not allowed"):
        ToolResultExecutionSemanticsFact(
            render_profile=denial_profile,
            result_state=ToolResultStateFact.SUCCESS,
            essential_capture_policy=None,
            essential_result=None,
            terminal_payload_timing=None,
        )


def test_external_requirement_freezes_capture_policy_when_essential_is_possible() -> (
    None
):
    contract = _render_contract()
    policy = _capture_policy()
    payload = {
        "tool_call_id": "call:external",
        "model_tool_name": "terminal",
        "raw_arguments_json": '{"command":"pwd"}',
        "tool_origin": "terminal",
        "descriptor_attribution": _attribution(contract),
        "result_render_contract": contract,
        "essential_capture_policy": policy,
    }
    requirement = ExternalToolCallRequirementFact(
        **payload,
        requirement_fingerprint=context_fingerprint(
            "external-tool-call-requirement:v1", payload
        ),
    )
    assert requirement.essential_capture_policy == policy
    with pytest.raises(ValidationError, match="capture policy branch mismatch"):
        ExternalToolCallRequirementFact(
            **{**payload, "essential_capture_policy": None},
            requirement_fingerprint="invalid",
        )


def test_external_ingress_binds_committed_requirement_and_frozen_builder_contract() -> (
    None
):
    requirement = external_tool_call_requirement_fact(
        "call:external", tool_name="external_lookup"
    )
    ctx = EventContext(
        run_id="run:external",
        turn_id="turn:external",
        reply_id="reply:external",
    )
    log = InMemoryEventLog()
    stored = log.append(
        RequireExternalExecutionEvent(
            id="require-external:1",
            **ctx.event_fields(),
            external_tool_calls=(requirement,),
        )
    )
    assert isinstance(stored, RequireExternalExecutionEvent)
    timing = ToolObservationTimingFact(
        observed_at_utc="2026-07-09T00:00:00Z",
        tool_call_id="call:external",
        tool_name="external_lookup",
        tool_origin="custom",
    )
    submission = freeze_external_tool_result_submission(
        result_block=ToolResultBlock(
            id="call:external",
            name="external_lookup",
            output=[TextBlock(text="done")],
            state=ToolResultState.SUCCESS,
        ),
        observation_timing=timing,
        selected_variant_code=ToolResultRenderVariantCode.EXTERNAL_GENERIC_RESULT,
        domain_result=None,
        terminal_payload_timing=None,
    )
    ingress = ExternalToolResultIngressBuilder(
        build_default_tool_result_semantics_registry()
    ).bind_submission(
        requirement_event=stored,
        requirement=requirement,
        submission=submission,
        owner_runtime_session_id="runtime:test",
    )

    assert ingress.requirement_ref.require_event_id == stored.id
    assert ingress.requirement_ref.require_event_sequence == stored.sequence
    assert ingress.requirement_ref.requirement_fingerprint == (
        requirement.requirement_fingerprint
    )
    assert ingress.execution_semantics.render_profile.tool_origin == "custom"
    assert ingress.execution_semantics.result_state is ToolResultStateFact.SUCCESS


def test_terminal_semantics_never_infers_from_serialized_result_json() -> None:
    contract = terminal_result_render_contract()
    descriptor = SimpleNamespace(
        name="terminal",
        provider_kind=SimpleNamespace(value="builtin"),
        result_render_contract=contract,
    )
    with pytest.raises(ValueError, match="typed semantics input"):
        build_execution_semantics(
            descriptor=descriptor,
            descriptor_attribution=_attribution(contract),
            call=ToolCall(
                id="call:terminal",
                name="terminal",
                arguments={"command": "pwd"},
            ),
            result=ToolExecutionResult(
                call_id="call:terminal",
                tool_name="terminal",
                status=ToolResultState.SUCCESS,
                output='{"status":"success","cwd":"/json-inference"}',
            ),
            observation_timing=ToolObservationTimingFact(
                observed_at_utc="2026-01-01T00:00:00Z",
                tool_call_id="call:terminal",
                tool_name="terminal",
                tool_origin="terminal",
            ),
            capture_policy=_capture_policy(),
            registry=build_default_tool_result_semantics_registry(),
        )


def test_typed_terminal_semantics_wins_over_conflicting_display_json() -> None:
    contract = terminal_result_render_contract()
    descriptor = SimpleNamespace(
        name="terminal",
        provider_kind=SimpleNamespace(value="builtin"),
        result_render_contract=contract,
    )
    timing = build_terminal_payload_timing(
        observed_at_utc="2026-01-01T00:00:00Z",
        duration_seconds=0,
        freshness="current_tool_observation",
        clock_source="tool_runtime_metadata",
    )
    semantics = build_execution_semantics(
        descriptor=descriptor,
        descriptor_attribution=_attribution(contract),
        call=ToolCall(
            id="call:terminal",
            name="terminal",
            arguments={"command": "pwd"},
        ),
        result=ToolExecutionResult(
            call_id="call:terminal",
            tool_name="terminal",
            status=ToolResultState.SUCCESS,
            output='{"status":"error","cwd":"/forged","command":"evil"}',
            semantics_input=FrozenToolResultSemanticsRuntimeInput(
                semantics_input_kind=ToolResultRenderVariantCode.TERMINAL_COMMAND_EXECUTED,
                domain_submission=TerminalCommandDomainSubmissionFact(
                    command="pwd",
                    status="success",
                    exit_code=0,
                    cwd="/typed",
                    timed_out=False,
                    output_truncated=False,
                    error=None,
                    process_id=None,
                    yielded_to_background=False,
                    terminal_session_id="typed",
                    backend_type="local",
                    io_mode=None,
                    stdin_closed=None,
                    policy_code=None,
                    duration_seconds=0,
                ),
            ),
            terminal_payload_timing=timing,
        ),
        observation_timing=ToolObservationTimingFact(
            observed_at_utc="2026-01-01T00:00:00Z",
            tool_call_id="call:terminal",
            tool_name="terminal",
            tool_origin="terminal",
        ),
        capture_policy=_capture_policy(),
        registry=build_default_tool_result_semantics_registry(),
    )

    assert isinstance(semantics.essential_result, TerminalCommandEssentialFact)
    assert semantics.essential_result.command == "pwd"
    assert semantics.essential_result.cwd == "/typed"
    assert semantics.essential_result.status == "success"


def test_context_compile_policy_resolves_default_allocator_values() -> None:
    policy = resolve_context_compile_policy(LoopBudget())
    basis = policy.tool_result_basis
    assert basis.total_context_chars == 36_000
    assert basis.body_context_chars == 24_000
    assert basis.envelope_context_chars == 12_000
    assert basis.prior_history_context_chars == 8_000
    assert basis.current_run_tail_context_chars == 16_000
    assert basis.current_user_context_chars == 16_000
    assert basis.legacy_history_context_chars == 24_000
    assert basis.per_tool_cap_chars == 12_000
    assert basis.per_message_cap_chars == 20_000
    assert basis.per_envelope_cap_chars == 1_200
    assert policy.candidate_collection.projection_token_budget == 2_000
    assert policy.candidate_collection.max_subagent_results_per_parent_compile == 8


def test_context_compile_policy_resolves_every_optional_budget_source() -> None:
    policy = resolve_context_compile_policy(
        LoopBudget(
            tool_result_context_chars=1_000,
            tool_result_body_context_chars=700,
            tool_result_envelope_context_chars=900,
            prior_tool_result_context_chars=100,
            current_tail_tool_result_context_chars=200,
            legacy_tool_result_context_chars=300,
            tool_result_per_tool_cap_chars=111,
            tool_result_per_message_cap_chars=222,
            tool_result_per_envelope_cap_chars=1,
        )
    )
    basis = policy.tool_result_basis
    assert basis.body_context_chars == 700
    assert basis.envelope_context_chars == 300
    assert basis.prior_history_context_chars == 100
    assert basis.current_run_tail_context_chars == 200
    assert basis.legacy_history_context_chars == 300
    assert basis.per_tool_cap_chars == 111
    assert basis.per_message_cap_chars == 222
    assert basis.per_envelope_cap_chars == 256
