from __future__ import annotations

import asyncio
import inspect
import math
from dataclasses import dataclass, replace
from pathlib import Path
from typing import AsyncIterator

import pytest
from pydantic import ValidationError

from tests.support import (
    bind_test_context,
    context_compiled_contract_fields,
    make_test_run_execution_activation,
    resolve_test_call,
    test_llm_config,
    test_llm_context,
    test_model_limits,
    test_model_slot,
)
from tests.support.runtime_session import in_memory_runtime_session
from tests.support.model_call import model_terminal_projection_end_reference_fixture
from tests.conftest import open_test_root_rollout_run, run_start_permission_fields

from pulsara_agent.event import (
    AgentEvent,
    ContextCompiledEvent,
    EventContext,
    ModelCallEndEvent,
    ModelCallStartEvent,
    ModelCallTerminalProjectionCommittedEvent,
    ModelCallRejectedEvent,
    ProviderModelStreamErrorEvent,
    ReplyEndEvent,
    ReplyStartEvent,
    RolloutBudgetReservationSettledEvent,
    RunErrorEvent,
    RunStartEvent,
)
from pulsara_agent.event_log import dump_agent_event, load_agent_event
from pulsara_agent.llm.adapters.openai.chat_completions import (
    OpenAIChatCompletionsTransport,
    build_chat_completions_payload,
)
from pulsara_agent.llm.adapters.openai.responses import (
    OpenAIResponsesTransport,
    build_responses_payload,
)
from pulsara_agent.llm.config import LLMConfig
from pulsara_agent.llm.errors import (
    ModelInputBudgetExceeded,
    ModelInputEstimateMismatch,
    ModelOptionUnsupported,
    ModelTargetBindingMismatch,
)
from pulsara_agent.llm.estimator import PulsaraHeuristicTokenEstimatorV1
from pulsara_agent.llm.input import LLMMessage, ToolSpec
from pulsara_agent.llm.models import ModelRole
from pulsara_agent.llm.provider import (
    ModelIdentityPolicy,
    ProviderProfile,
    ThinkingProfile,
)
from pulsara_agent.llm.registry import LLMTransportRegistry
from pulsara_agent.llm.request import LLMContext, LLMOptions
from pulsara_agent.llm.resolution import (
    canonicalize_endpoint,
    redact_provider_request_shape,
)
from pulsara_agent.llm.result import TransportUsageReport
from pulsara_agent.llm.runtime import LLMRuntime
from pulsara_agent.llm.commit import RuntimeSessionModelStreamEventCommitPort
from pulsara_agent.llm.lifecycle import prepare_model_lifecycle_start_bundle
from pulsara_agent.llm.sanitizing_transport import SanitizingLLMTransport
from pulsara_agent.primitives.model_call import (
    ModelCallPurpose,
    ModelContextLimits,
    ModelTokenUsageFact,
    ResolvedModelTargetFact,
    TokenEstimatorFact,
    canonical_json_bytes,
    resolved_model_target_fingerprint,
)
from pulsara_agent.runtime.compaction.service import ContextCompactionPolicy


EVENT_CONTEXT = EventContext(
    run_id="run:resolved-contract",
    turn_id="turn:resolved-contract",
    reply_id="reply:resolved-contract",
)


def _start_test_stream(
    runtime: LLMRuntime,
    *,
    call,
    context: LLMContext,
    event_context: EventContext,
    runtime_session,
    run_execution_activation=None,
):
    lifecycle_kind = (
        "main_assistant_reply"
        if context.model_call_index is not None
        else "direct_internal_call"
    )
    if lifecycle_kind == "main_assistant_reply":
        open_test_root_rollout_run(
            runtime_session,
            event_context=event_context,
            model_target=call.target.fact,
        )
    bundle = prepare_model_lifecycle_start_bundle(
        call=call,
        context=context,
        event_context=event_context,
        runtime_session=runtime_session,
        lifecycle_kind=lifecycle_kind,
        run_execution_activation=run_execution_activation,
    )
    return runtime.start_stream(
        call=call,
        context=context,
        event_context=event_context,
        start_bundle=bundle,
        commit_port=RuntimeSessionModelStreamEventCommitPort(
            runtime_session=runtime_session,
            state=None,
        ),
        execution_registry=runtime_session.model_stream_execution_registry,
    )


@dataclass(slots=True)
class _Transport:
    api: str = "contract"
    binding_id: str = "test.resolved_contract"
    contract_version: str = "v1"
    items: tuple[AgentEvent | TransportUsageReport, ...] = ()
    calls: int = 0

    async def stream(self, *, call, context, event_context) -> AsyncIterator:
        self.calls += 1
        for item in self.items:
            yield item


def _runtime(
    *,
    config: LLMConfig | None = None,
    transport: _Transport | None = None,
) -> tuple[LLMRuntime, _Transport]:
    transport = transport or _Transport()
    config = config or test_llm_config(
        api_key="sk-test",
        base_url="https://example.test/v1",
        pro_model="pro",
        flash_model="flash",
        api=transport.api,
    )
    registry = LLMTransportRegistry()
    registry.register(transport)
    return LLMRuntime(config=config, registry=registry), transport


def _target(
    *,
    config: LLMConfig | None = None,
    transport: _Transport | None = None,
    role: ModelRole = ModelRole.PRO,
    options: LLMOptions | None = None,
):
    runtime, transport = _runtime(config=config, transport=transport)
    return (
        runtime.resolve_target(role=role, requested_options=options),
        runtime,
        transport,
    )


def _call_and_context(
    *,
    purpose: ModelCallPurpose = ModelCallPurpose.AGENT_MODEL_LOOP,
    messages: tuple[LLMMessage, ...] = (LLMMessage.user("hello"),),
    config: LLMConfig | None = None,
    transport: _Transport | None = None,
):
    target, runtime, transport = _target(
        config=config,
        transport=transport,
        role=(
            ModelRole.PRO
            if purpose is ModelCallPurpose.AGENT_MODEL_LOOP
            else ModelRole.FLASH
        ),
    )
    call = runtime.resolve_call(target=target, purpose=purpose)
    context = bind_test_context(call, test_llm_context(messages=messages))
    return runtime, transport, call, context


def _changed_target_fact(fact, **updates) -> ResolvedModelTargetFact:
    payload = fact.model_dump(mode="json", exclude={"target_fingerprint"})
    payload.update(updates)
    payload["target_fingerprint"] = resolved_model_target_fingerprint(payload)
    return ResolvedModelTargetFact.model_validate(payload)


def test_model_context_limits_require_positive_values() -> None:
    for field in (
        "max_input_tokens",
        "max_output_tokens",
        "default_output_tokens",
    ):
        payload = test_model_limits().model_dump()
        payload[field] = 0
        with pytest.raises(ValidationError):
            ModelContextLimits.model_validate(payload)


def test_model_context_limits_reject_inconsistent_maxima() -> None:
    with pytest.raises(ValidationError, match="max_input_tokens"):
        ModelContextLimits(
            total_context_tokens=100,
            max_input_tokens=101,
            max_output_tokens=20,
            default_output_tokens=10,
            input_safety_margin_tokens=0,
        )


def test_model_context_limits_validate_default_output() -> None:
    with pytest.raises(ValidationError, match="default_output_tokens"):
        ModelContextLimits(
            total_context_tokens=100,
            max_input_tokens=80,
            max_output_tokens=20,
            default_output_tokens=21,
            input_safety_margin_tokens=0,
        )


def test_model_slot_limits_default_when_env_is_omitted(monkeypatch) -> None:
    prefix = "PULSARA_CONTRACT_MISSING"
    monkeypatch.setenv(f"{prefix}_API_KEY", "sk-test")
    monkeypatch.setenv(f"{prefix}_PRO_MODEL", "pro")
    monkeypatch.setenv(f"{prefix}_FLASH_MODEL", "flash")

    config = LLMConfig.from_env(prefix)

    expected = {
        "total_context_tokens": 256_000,
        "max_input_tokens": 256_000,
        "max_output_tokens": 128_000,
        "default_output_tokens": 8_192,
        "input_safety_margin_tokens": 8_192,
    }
    assert config.pro.limits.model_dump() == expected
    assert config.flash.limits.model_dump() == expected


def test_model_identity_policy_defaults_to_accept_reported_and_allows_exact_env(
    monkeypatch,
) -> None:
    prefix = "PULSARA_CONTRACT_IDENTITY"
    monkeypatch.setenv(f"{prefix}_API_KEY", "sk-test")
    for role in ("PRO", "FLASH"):
        monkeypatch.setenv(f"{prefix}_{role}_MODEL", role.lower())
        monkeypatch.setenv(f"{prefix}_{role}_TOTAL_CONTEXT_TOKENS", "4096")
        monkeypatch.setenv(f"{prefix}_{role}_MAX_INPUT_TOKENS", "3584")
        monkeypatch.setenv(f"{prefix}_{role}_MAX_OUTPUT_TOKENS", "1024")
        monkeypatch.setenv(f"{prefix}_{role}_DEFAULT_OUTPUT_TOKENS", "512")
        monkeypatch.setenv(f"{prefix}_{role}_INPUT_SAFETY_MARGIN_TOKENS", "128")

    default_config = LLMConfig.from_env(prefix)
    assert (
        default_config.provider_profile is not None
        and default_config.provider_profile.model_identity_policy
        is ModelIdentityPolicy.ACCEPT_REPORTED
    )

    monkeypatch.setenv(f"{prefix}_MODEL_IDENTITY_POLICY", "exact")
    exact_config = LLMConfig.from_env(prefix)
    assert (
        exact_config.provider_profile is not None
        and exact_config.provider_profile.model_identity_policy
        is ModelIdentityPolicy.EXACT
    )


def test_pro_and_flash_limits_are_independent() -> None:
    pro = test_model_limits(
        total_context_tokens=8_000,
        max_input_tokens=7_000,
        max_output_tokens=1_000,
        default_output_tokens=500,
        input_safety_margin_tokens=100,
    )
    flash = test_model_limits(
        total_context_tokens=4_000,
        max_input_tokens=3_000,
        max_output_tokens=1_000,
        default_output_tokens=500,
        input_safety_margin_tokens=100,
    )
    config = test_llm_config(
        api_key="sk-test",
        base_url="https://example.test/v1",
        pro_model="pro",
        flash_model="flash",
        api="contract",
        pro_limits=pro,
        flash_limits=flash,
    )
    runtime, _ = _runtime(config=config)
    assert runtime.resolve_target(role=ModelRole.PRO).limits == pro
    assert runtime.resolve_target(role=ModelRole.FLASH).limits == flash


def test_output_budget_uses_slot_default() -> None:
    target, _, _ = _target()
    assert (
        target.context_budget.effective_output_tokens
        == target.limits.default_output_tokens
    )


def test_per_call_output_and_temperature_options_are_not_supported() -> None:
    with pytest.raises(TypeError):
        LLMOptions(max_output_tokens=101)  # type: ignore[call-arg]
    with pytest.raises(TypeError):
        LLMOptions(temperature=0)  # type: ignore[call-arg]


def test_provider_request_defaults_reject_output_budget_keys() -> None:
    with pytest.raises(ValueError, match="max_tokens"):
        test_llm_config(
            api_key="sk-test",
            base_url="https://example.test/v1",
            pro_model="pro",
            flash_model="flash",
            provider_profile=ProviderProfile(request_defaults={"max_tokens": 4}),
        )


def test_provider_extra_body_rejects_output_budget_keys() -> None:
    with pytest.raises(ValueError, match="max_output_tokens"):
        test_llm_config(
            api_key="sk-test",
            base_url="https://example.test/v1",
            pro_model="pro",
            flash_model="flash",
            provider_profile=ProviderProfile(
                request_extra_body={"max_output_tokens": 4}
            ),
        )


@pytest.mark.parametrize(
    "source_name,reserved_key",
    [
        ("request_defaults", "model"),
        ("request_defaults", "instructions"),
        ("request_defaults", "tools"),
        ("request_defaults", "temperature"),
        ("request_defaults", "reasoning_effort"),
        ("request_extra_body", "input"),
        ("request_extra_body", "messages"),
        ("request_extra_body", "tool_choice"),
        ("request_extra_body", "parallel_tool_calls"),
        ("request_extra_body", "stream"),
        ("request_defaults", "functions"),
        ("request_defaults", "function_call"),
        ("request_defaults", "web_search_options"),
        ("request_defaults", "conversation"),
        ("request_defaults", "previous_response_id"),
        ("request_defaults", "prompt"),
        ("request_defaults", "truncation"),
        ("request_extra_body", "context_management"),
    ],
)
def test_provider_extensions_reject_pulsara_owned_payload_keys(
    source_name: str,
    reserved_key: str,
) -> None:
    profile_kwargs = {source_name: {reserved_key: "injected"}}
    with pytest.raises(ValueError, match=reserved_key):
        test_llm_config(
            api_key="sk-test",
            base_url="https://example.test/v1",
            pro_model="pro",
            flash_model="flash",
            provider_profile=ProviderProfile(**profile_kwargs),
        )


def test_provider_extensions_allow_non_conflicting_fingerprinted_shape() -> None:
    config = test_llm_config(
        api_key="sk-test",
        base_url="https://example.test/v1",
        pro_model="pro",
        flash_model="flash",
        api="openai_responses",
        provider_profile=ProviderProfile(
            request_defaults={"service_tier": "priority"},
            request_extra_body={"thinking": {"type": "enabled"}},
        ),
    )
    target, _, _ = _target(config=config, transport=_Transport(api="openai_responses"))
    assert target.fact.provider_request_shape_fingerprint.startswith("sha256:")


def test_provider_extension_keys_require_exact_canonical_spelling() -> None:
    with pytest.raises(ValueError, match="service-tier"):
        test_llm_config(
            api_key="sk-test",
            base_url="https://example.test/v1",
            pro_model="pro",
            flash_model="flash",
            api="openai_responses",
            provider_profile=ProviderProfile(
                request_defaults={"service-tier": "priority"}
            ),
        )


def test_unknown_api_cannot_borrow_provider_profile_extension_allowlist() -> None:
    with pytest.raises(ValueError, match="service_tier"):
        test_llm_config(
            api_key="sk-test",
            base_url="https://example.test/v1",
            pro_model="pro",
            flash_model="flash",
            api="custom_transport",
            provider_profile=ProviderProfile(
                wire_api="openai_responses",
                request_defaults={"service_tier": "priority"},
            ),
        )


def test_config_canonicalizes_provider_profile_to_runtime_api() -> None:
    config = test_llm_config(
        api_key="sk-test",
        base_url="https://example.test/v1",
        pro_model="pro",
        flash_model="flash",
        api="custom_transport",
        provider_profile=ProviderProfile(wire_api="openai_responses"),
    )
    assert config.provider_profile is not None
    assert config.provider_profile.wire_api == "custom_transport"
    assert (
        config.model_for(ModelRole.PRO).provider_profile.wire_api == "custom_transport"
    )


def test_thinking_omit_cannot_remove_output_budget() -> None:
    with pytest.raises(ValueError, match="omission policy"):
        test_llm_config(
            api_key="sk-test",
            base_url="https://example.test/v1",
            pro_model="pro",
            flash_model="flash",
            provider_profile=ProviderProfile(
                omit_params_when_thinking=("max_output_tokens",)
            ),
        )


def test_temperature_and_reasoning_summary_are_absent_from_options_contract() -> None:
    fields = set(LLMOptions.__dataclass_fields__)
    assert fields == {"reasoning_effort"}


def test_compaction_summarizer_options_default_to_empty_options() -> None:
    assert ContextCompactionPolicy().summarizer_options == LLMOptions()


def test_resolved_target_fingerprint_is_stable() -> None:
    first, _, _ = _target()
    second, _, _ = _target()
    assert first.fact.target_fingerprint == second.fact.target_fingerprint


def test_resolved_target_fingerprint_changes_with_limits() -> None:
    first, _, _ = _target()
    limits = test_model_limits(input_safety_margin_tokens=63_999)
    config = test_llm_config(
        api_key="sk-test",
        base_url="https://example.test/v1",
        pro_model="pro",
        flash_model="flash",
        api="contract",
        pro_limits=limits,
    )
    second, _, _ = _target(config=config)
    assert first.fact.target_fingerprint != second.fact.target_fingerprint


def test_resolved_target_fingerprint_changes_with_options() -> None:
    first, _, _ = _target()
    second, _, _ = _target(options=LLMOptions(reasoning_effort="medium"))
    assert first.fact.target_fingerprint != second.fact.target_fingerprint


def test_resolved_target_fingerprint_changes_with_estimator() -> None:
    target, _, _ = _target()
    changed = _changed_target_fact(
        target.fact,
        token_estimator=TokenEstimatorFact(
            estimator_id="pulsara_heuristic",
            estimator_version="v2",
            estimator_fingerprint="sha256:changed",
        ).model_dump(mode="json"),
    )
    assert changed.target_fingerprint != target.fact.target_fingerprint


def test_resolved_target_fingerprint_changes_with_transport_binding() -> None:
    first, _, _ = _target(transport=_Transport(binding_id="test.binding.a"))
    second, _, _ = _target(transport=_Transport(binding_id="test.binding.b"))
    assert first.fact.target_fingerprint != second.fact.target_fingerprint


def test_resolved_target_fingerprint_changes_with_model_identity_policy() -> None:
    default_config = test_llm_config(
        api_key="sk-test",
        base_url="https://example.test/v1",
        pro_model="pro",
        flash_model="flash",
        api="contract",
    )
    exact_config = replace(
        default_config,
        provider_profile=ProviderProfile(
            model_identity_policy=ModelIdentityPolicy.EXACT
        ),
    )
    default_target, _, _ = _target(config=default_config)
    exact_target, _, _ = _target(config=exact_config)

    assert default_target.fact.model_identity_policy == "accept_reported"
    assert exact_target.fact.model_identity_policy == "exact"
    assert (
        default_target.fact.target_fingerprint != exact_target.fact.target_fingerprint
    )


def test_endpoint_canonicalization_normalizes_host_port_and_trailing_slash() -> None:
    first = canonicalize_endpoint("HTTPS://Example.COM:443/v1/")
    second = canonicalize_endpoint("https://example.com/v1")
    third = canonicalize_endpoint("https://example.com/%7e")
    fourth = canonicalize_endpoint("https://example.com/%7E")
    assert first == second
    assert third == fourth


def test_endpoint_canonicalization_rejects_userinfo_query_fragment_and_dot_segments() -> (
    None
):
    for endpoint in (
        "https://user:pass@example.com/v1",
        "https://example.com/v1?q=secret",
        "https://example.com/v1#frag",
        "https://example.com/a/%2e%2e/b",
    ):
        with pytest.raises(ValueError):
            canonicalize_endpoint(endpoint)


def test_request_shape_secret_keys_are_recursively_redacted() -> None:
    redacted = redact_provider_request_shape(
        {"nested": {"API-Key": "one", "client secret": "two"}}
    )
    assert redacted == {
        "nested": {
            "api_key": "<redacted:secret>",
            "client_secret": "<redacted:secret>",
        }
    }


def test_request_shape_header_and_cookie_secrets_are_redacted() -> None:
    redacted = redact_provider_request_shape(
        {
            "headers": {"Authorization": "Bearer secret", "X-Trace": "safe"},
            "cookies": {"session": "secret", "locale": "zh"},
        }
    )
    assert redacted["headers"]["authorization"] == "<redacted:secret>"
    assert redacted["headers"]["x_trace"] == "safe"
    assert set(redacted["cookies"].values()) == {"<redacted:secret>"}


def test_credential_rotation_does_not_change_request_shape_fingerprint() -> None:
    first = redact_provider_request_shape({"headers": {"authorization": "a"}})
    second = redact_provider_request_shape({"headers": {"authorization": "b"}})
    assert canonical_json_bytes(first) == canonical_json_bytes(second)


def test_non_secret_request_shape_change_changes_target_fingerprint() -> None:
    config_a = test_llm_config(
        api_key="sk-test",
        base_url="https://example.test/v1",
        pro_model="pro",
        flash_model="flash",
        api="openai_responses",
        provider_profile=ProviderProfile(request_defaults={"service_tier": "auto"}),
    )
    config_b = replace(
        config_a,
        provider_profile=ProviderProfile(request_defaults={"service_tier": "priority"}),
    )
    transport = _Transport(api="openai_responses")
    first, _, _ = _target(config=config_a, transport=transport)
    second, _, _ = _target(
        config=config_b, transport=_Transport(api="openai_responses")
    )
    assert first.fact.target_fingerprint != second.fact.target_fingerprint


def test_normalized_secret_key_collision_is_configuration_error() -> None:
    with pytest.raises(ValueError, match="collision"):
        redact_provider_request_shape({"API-Key": "a", "api key": "b"})


def test_requested_option_rejected_when_thinking_policy_forbids_it() -> None:
    profile = ProviderProfile(
        thinking=ThinkingProfile(enabled=True),
        omit_params_when_thinking=("reasoning_effort",),
    )
    config = test_llm_config(
        api_key="sk-test",
        base_url="https://example.test/v1",
        pro_model="pro",
        flash_model="flash",
        api="contract",
        provider_profile=profile,
    )
    with pytest.raises(ModelOptionUnsupported):
        _target(config=config, options=LLMOptions(reasoning_effort="medium"))


def test_unrequested_none_option_is_not_treated_as_omission() -> None:
    profile = ProviderProfile(
        thinking=ThinkingProfile(enabled=True),
        omit_params_when_thinking=("temperature",),
    )
    config = test_llm_config(
        api_key="sk-test",
        base_url="https://example.test/v1",
        pro_model="pro",
        flash_model="flash",
        api="contract",
        provider_profile=profile,
    )
    target, _, _ = _target(config=config)
    assert target.effective_options.reasoning_effort is None


def test_payload_uses_only_resolved_reasoning_and_context_budget_output() -> None:
    limits = test_model_limits(default_output_tokens=64)
    config = test_llm_config(
        api_key="sk-test",
        base_url="https://example.test/v1",
        pro_model="pro",
        flash_model="flash",
        pro_limits=limits,
    )
    call = resolve_test_call(
        config,
        options=LLMOptions(reasoning_effort="medium"),
        transport=OpenAIResponsesTransport(api_key="sk-test"),
    )
    payload = build_responses_payload(
        call=call,
        context=bind_test_context(
            call, test_llm_context(messages=(LLMMessage.user("x"),))
        ),
    )
    assert "temperature" not in payload
    assert payload["max_output_tokens"] == 64
    assert payload["reasoning"] == {"effort": "medium"}


def test_rebind_target_reconstructs_only_sent_effective_options() -> None:
    target, runtime, _ = _target(options=LLMOptions())
    rebound = runtime.rebind_target(target.fact)
    assert rebound.effective_options == target.effective_options
    assert rebound.fact == target.fact


def test_resolved_calls_share_target_but_have_unique_ids() -> None:
    target, runtime, _ = _target()
    first = runtime.resolve_call(
        target=target, purpose=ModelCallPurpose.AGENT_MODEL_LOOP
    )
    second = runtime.resolve_call(
        target=target, purpose=ModelCallPurpose.AGENT_MODEL_LOOP
    )
    assert first.target.fact == second.target.fact
    assert first.resolved_model_call_id != second.resolved_model_call_id


def test_resolved_fact_round_trip() -> None:
    target, _, _ = _target()
    assert (
        ResolvedModelTargetFact.model_validate_json(target.fact.model_dump_json())
        == target.fact
    )


def test_resolved_target_fact_requires_slot_default_output_cap() -> None:
    target, _, _ = _target()
    payload = target.fact.model_dump(mode="json", exclude={"target_fingerprint"})
    effective_output = payload["limits"]["default_output_tokens"] - 1
    payload["context_budget"]["effective_output_tokens"] = effective_output
    pre_margin = min(
        payload["limits"]["max_input_tokens"],
        payload["limits"]["total_context_tokens"] - effective_output,
    )
    payload["context_budget"]["pre_margin_input_tokens"] = pre_margin
    payload["context_budget"]["input_budget_tokens"] = (
        pre_margin - payload["context_budget"]["safety_margin_tokens"]
    )
    payload["target_fingerprint"] = resolved_model_target_fingerprint(payload)

    with pytest.raises(ValidationError, match="must equal model slot default"):
        ResolvedModelTargetFact.model_validate(payload)


def test_model_token_usage_fact_round_trip() -> None:
    usage = ModelTokenUsageFact(
        input_tokens=10,
        cached_input_tokens=4,
        output_tokens=5,
        reasoning_output_tokens=2,
        total_tokens=15,
    )
    assert ModelTokenUsageFact.model_validate_json(usage.model_dump_json()) == usage


def test_model_token_usage_rejects_invalid_cached_or_reasoning_breakdown() -> None:
    with pytest.raises(ValidationError):
        ModelTokenUsageFact(
            input_tokens=1,
            cached_input_tokens=2,
            output_tokens=1,
            total_tokens=2,
        )
    with pytest.raises(ValidationError):
        ModelTokenUsageFact(
            input_tokens=1,
            output_tokens=1,
            reasoning_output_tokens=2,
            total_tokens=2,
        )


def test_model_token_usage_preserves_missing_breakdown_as_null() -> None:
    usage = ModelTokenUsageFact(input_tokens=1, output_tokens=2, total_tokens=3)
    assert usage.cached_input_tokens is None
    assert usage.reasoning_output_tokens is None


def test_model_token_usage_total_equals_input_plus_output() -> None:
    with pytest.raises(ValidationError, match="total_tokens"):
        ModelTokenUsageFact(input_tokens=1, output_tokens=2, total_tokens=4)


def test_resolved_fact_contains_no_api_key() -> None:
    target, _, _ = _target()
    assert "sk-test" not in target.fact.model_dump_json()


def test_resolved_fact_redacts_endpoint_userinfo_query_path() -> None:
    target, _, _ = _target()
    payload = target.fact.model_dump_json()
    assert target.fact.endpoint_origin == "https://example.test"
    assert "/v1" not in payload


def test_nested_resolved_facts_are_immutable() -> None:
    profile = ProviderProfile(request_defaults={"nested": {"value": 1}})
    with pytest.raises(TypeError):
        profile.request_defaults["nested"]["value"] = 2
    target, _, _ = _target()
    with pytest.raises(ValidationError):
        target.fact.limits.max_input_tokens = 1


def test_fingerprint_rejects_nan_and_infinity() -> None:
    for value in (math.nan, math.inf, -math.inf):
        with pytest.raises(ValueError):
            canonical_json_bytes({"value": value})


def test_resolve_target_binds_transport_once() -> None:
    target, _, transport = _target()
    assert isinstance(target.transport, SanitizingLLMTransport)
    assert target.transport._raw_transport is transport
    assert target.fact.transport_binding_id == transport.binding_id


def test_resolve_call_does_not_reparse_config() -> None:
    target, runtime, _ = _target()
    first = runtime.resolve_call(
        target=target, purpose=ModelCallPurpose.AGENT_MODEL_LOOP
    )
    second = runtime.resolve_call(
        target=target, purpose=ModelCallPurpose.AGENT_MODEL_LOOP
    )
    assert first.target is target
    assert second.target is target


def test_rebind_target_accepts_identical_runtime_config() -> None:
    target, runtime, _ = _target()
    assert runtime.rebind_target(target.fact).fact == target.fact


@pytest.mark.parametrize(
    "change",
    ["model", "limits", "endpoint", "provider_shape", "transport"],
)
def test_rebind_target_rejects_semantic_change(change: str) -> None:
    target, runtime, _ = _target()
    config = runtime._config
    transport = _Transport()
    if change == "model":
        config = replace(config, pro=test_model_slot("changed"))
    elif change == "limits":
        config = replace(
            config,
            pro=test_model_slot(
                "pro", limits=test_model_limits(input_safety_margin_tokens=63_999)
            ),
        )
    elif change == "endpoint":
        config = replace(config, base_url="https://other.test/v1")
    elif change == "provider_shape":
        config = replace(
            config,
            provider_profile=ProviderProfile(supports_tools=False),
        )
    elif change == "transport":
        transport = _Transport(contract_version="v2")
    changed_runtime, _ = _runtime(config=config, transport=transport)
    with pytest.raises(ModelTargetBindingMismatch):
        changed_runtime.rebind_target(target.fact)


def test_rebind_target_rejects_model_change() -> None:
    test_rebind_target_rejects_semantic_change("model")


def test_rebind_target_rejects_limits_change() -> None:
    test_rebind_target_rejects_semantic_change("limits")


def test_rebind_target_rejects_endpoint_change() -> None:
    test_rebind_target_rejects_semantic_change("endpoint")


def test_rebind_target_rejects_provider_shape_change() -> None:
    test_rebind_target_rejects_semantic_change("provider_shape")


def test_rebind_target_rejects_transport_contract_change() -> None:
    test_rebind_target_rejects_semantic_change("transport")


def test_rebind_target_rejects_estimator_change() -> None:
    target, runtime, _ = _target()
    changed = _changed_target_fact(
        target.fact,
        token_estimator=TokenEstimatorFact(
            estimator_id="pulsara_heuristic",
            estimator_version="v2",
            estimator_fingerprint="sha256:v2",
        ).model_dump(mode="json"),
    )
    with pytest.raises(ModelTargetBindingMismatch):
        runtime.rebind_target(changed)


def test_stream_role_options_signature_is_removed() -> None:
    assert not hasattr(LLMRuntime, "stream")
    parameters = inspect.signature(LLMRuntime.start_stream).parameters
    assert "role" not in parameters
    assert "options" not in parameters
    assert {
        "call",
        "context",
        "event_context",
        "start_bundle",
        "commit_port",
        "execution_registry",
    }.issubset(parameters)
    assert "runtime_session" not in parameters


def test_llm_context_identity_is_required_at_construction() -> None:
    with pytest.raises(TypeError):
        LLMContext(messages=(LLMMessage.user("x"),))


def test_compiled_call_requires_compiler_final_estimate() -> None:
    runtime, transport, call, context = _call_and_context()
    context = replace(context, compiler_estimated_input_tokens=None)

    with pytest.raises(ModelInputEstimateMismatch):
        asyncio.run(_collect_runtime(runtime, call=call, context=context))
    assert transport.calls == 0


def test_pr1_standalone_validation_rejects_oversized_direct_call_before_reply_start() -> (
    None
):
    limits = ModelContextLimits(
        total_context_tokens=64,
        max_input_tokens=48,
        max_output_tokens=16,
        default_output_tokens=16,
        input_safety_margin_tokens=4,
    )
    config = test_llm_config(
        api_key="sk-test",
        base_url="https://example.test/v1",
        pro_model="pro",
        flash_model="flash",
        api="contract",
        flash_limits=limits,
    )
    runtime, transport, call, context = _call_and_context(
        purpose=ModelCallPurpose.MEMORY_REFLECTION,
        messages=(LLMMessage.user("x" * 1_000),),
        config=config,
    )

    with pytest.raises(ModelInputBudgetExceeded):
        asyncio.run(_collect_runtime(runtime, call=call, context=context))
    assert transport.calls == 0


def test_estimate_only_failure_writes_no_start_or_fake_end() -> None:
    limits = ModelContextLimits(
        total_context_tokens=64,
        max_input_tokens=48,
        max_output_tokens=16,
        default_output_tokens=16,
        input_safety_margin_tokens=4,
    )
    config = test_llm_config(
        api_key="sk-test",
        base_url="https://example.test/v1",
        pro_model="pro",
        flash_model="flash",
        api="contract",
        flash_limits=limits,
    )
    runtime, transport, call, context = _call_and_context(
        purpose=ModelCallPurpose.MEMORY_REFLECTION,
        messages=(LLMMessage.user("x" * 1_000),),
        config=config,
    )
    with pytest.raises(ModelInputBudgetExceeded):
        asyncio.run(_collect_runtime(runtime, call=call, context=context))
    assert transport.calls == 0


async def _collect_runtime(
    runtime: LLMRuntime,
    *,
    call,
    context: LLMContext,
) -> list[AgentEvent]:
    session = in_memory_runtime_session(Path.cwd())
    handle = _start_test_stream(runtime,
        call=call,
        context=context,
        event_context=EVENT_CONTEXT,
        runtime_session=session,
        run_execution_activation=(
            make_test_run_execution_activation()
            if context.model_call_index is not None
            else None
        ),
    )
    completion = await handle.wait_completed()
    if completion.terminal_outcome == "rejected_before_start":
        await handle.wait_result()
    return list(completion.committed_events)


def _end_event(call, *, outcome: str = "completed") -> ModelCallEndEvent:
    return ModelCallEndEvent(
        **EVENT_CONTEXT.event_fields(),
        resolved_model_call_id=call.fact.resolved_model_call_id,
        target_fingerprint=call.target.fact.target_fingerprint,
        reported_model_id=None,
        outcome=outcome,
        provider_dispatch_status="dispatched",
        usage_status="missing",
        usage=None,
        estimated_input_tokens=1,
        terminal_projection=model_terminal_projection_end_reference_fixture(
            call.fact.resolved_model_call_id,
            outcome=outcome,
        ),
    )


def test_llm_runtime_emits_model_start_once() -> None:
    runtime, _, call, context = _call_and_context()
    events = asyncio.run(_collect_runtime(runtime, call=call, context=context))
    assert sum(isinstance(event, ReplyStartEvent) for event in events) == 1
    assert sum(isinstance(event, ModelCallStartEvent) for event in events) == 1
    assert sum(isinstance(event, ModelCallEndEvent) for event in events) == 1
    assert sum(isinstance(event, ReplyEndEvent) for event in events) == 1


def test_model_call_end_records_reported_model_identity() -> None:
    transport = _Transport(
        items=(
            TransportUsageReport(
                usage_status="missing",
                usage=None,
                reported_model_id="provider-snapshot",
            ),
        )
    )
    runtime, _, call, context = _call_and_context(transport=transport)
    events = asyncio.run(_collect_runtime(runtime, call=call, context=context))
    end = next(event for event in events if isinstance(event, ModelCallEndEvent))

    assert end.reported_model_id == "provider-snapshot"


@pytest.mark.parametrize(
    "raw_item",
    (
        ModelCallStartEvent.model_construct(),
        ReplyStartEvent(**EVENT_CONTEXT.event_fields(), name="assistant"),
        ReplyEndEvent(
            **EVENT_CONTEXT.event_fields(), model_terminal_outcome="completed"
        ),
    ),
)
def test_raw_transport_lifecycle_event_fails_closed(raw_item: AgentEvent) -> None:
    runtime, transport, call, context = _call_and_context()
    transport.items = (raw_item,)
    events = asyncio.run(_collect_runtime(runtime, call=call, context=context))
    assert any(isinstance(event, ProviderModelStreamErrorEvent) for event in events)
    end = next(event for event in events if isinstance(event, ModelCallEndEvent))
    assert end.outcome == "provider_error"


def test_duplicate_transport_usage_report_fails_closed() -> None:
    report = TransportUsageReport(usage_status="missing", usage=None)
    runtime, transport, call, context = _call_and_context()
    transport.items = (report, report)
    events = asyncio.run(_collect_runtime(runtime, call=call, context=context))
    assert any(isinstance(event, ProviderModelStreamErrorEvent) for event in events)
    assert next(
        event for event in events if isinstance(event, ModelCallEndEvent)
    ).outcome == "provider_error"


def test_missing_provider_usage_is_missing_not_zero() -> None:
    runtime, _, call, context = _call_and_context()
    events = asyncio.run(_collect_runtime(runtime, call=call, context=context))
    end = next(event for event in events if isinstance(event, ModelCallEndEvent))
    assert end.usage_status == "missing"
    assert end.usage is None


def test_pr1_estimate_only_seam_supplies_model_end_input_tokens() -> None:
    runtime, _, call, context = _call_and_context()
    expected = call.target.token_estimator.estimate_context(context).total_input_tokens
    events = asyncio.run(_collect_runtime(runtime, call=call, context=context))
    end = next(event for event in events if isinstance(event, ModelCallEndEvent))
    assert end.estimated_input_tokens == expected


def test_pr1_token_estimate_includes_message_breakdown() -> None:
    runtime, _, call, context = _call_and_context(
        messages=(LLMMessage.user("one"), LLMMessage.assistant("two")),
    )
    estimate = call.target.token_estimator.estimate_context(context)
    assert len(estimate.message_tokens_by_index) == 2
    assert sum(estimate.message_tokens_by_index) == estimate.message_tokens
    asyncio.run(_collect_runtime(runtime, call=call, context=context))


def test_runtime_injects_validation_estimate_into_model_end() -> None:
    test_pr1_estimate_only_seam_supplies_model_end_input_tokens()


def test_model_end_references_same_call() -> None:
    runtime, _, call, context = _call_and_context()
    events = asyncio.run(_collect_runtime(runtime, call=call, context=context))
    end = next(event for event in events if isinstance(event, ModelCallEndEvent))
    assert end.resolved_model_call_id == call.fact.resolved_model_call_id
    assert end.target_fingerprint == call.target.fact.target_fingerprint


def test_responses_payload_uses_effective_output() -> None:
    limits = test_model_limits(default_output_tokens=77)
    config = test_llm_config(
        api_key="sk-test",
        base_url="https://example.test/v1",
        pro_model="pro",
        flash_model="flash",
        pro_limits=limits,
    )
    call = resolve_test_call(
        config,
        options=LLMOptions(),
        transport=OpenAIResponsesTransport(api_key="sk-test"),
    )
    payload = build_responses_payload(
        call=call,
        context=bind_test_context(
            call, test_llm_context(messages=(LLMMessage.user("x"),))
        ),
    )
    assert payload["max_output_tokens"] == 77


def test_chat_payload_uses_effective_output() -> None:
    limits = test_model_limits(default_output_tokens=88)
    config = test_llm_config(
        api_key="sk-test",
        base_url="https://example.test/v1",
        pro_model="pro",
        flash_model="flash",
        api="openai_chat_completions",
        pro_limits=limits,
    )
    call = resolve_test_call(
        config,
        options=LLMOptions(),
        transport=OpenAIChatCompletionsTransport(api_key="sk-test"),
    )
    payload = build_chat_completions_payload(
        call=call,
        context=bind_test_context(
            call, test_llm_context(messages=(LLMMessage.user("x"),))
        ),
    )
    assert payload["max_completion_tokens"] == 88


def test_v1_estimator_text_json_and_framing_golden_values() -> None:
    estimator = PulsaraHeuristicTokenEstimatorV1()
    assert estimator.estimate_text("") == 0
    assert estimator.estimate_text("12345") == 2
    assert estimator.estimate_json({"a": 1}) == 4
    context = test_llm_context(
        messages=(LLMMessage.user("1234"),),
        tools=(ToolSpec(name="t", description="d", parameters={"type": "object"}),),
    )
    estimate = estimator.estimate_context(context)
    assert estimate.envelope_tokens == 3
    assert estimate.message_tokens_by_index == (5,)
    assert estimate.tool_tokens > 8


def test_provider_run_error_precedes_model_end_and_reply_end() -> None:
    run_error = RunErrorEvent(
        **EVENT_CONTEXT.event_fields(),
        message="provider failed",
        code="provider_error",
    )
    runtime, transport, call, context = _call_and_context()
    transport.items = (run_error,)
    events = asyncio.run(_collect_runtime(runtime, call=call, context=context))
    assert [type(event) for event in events[-5:]] == [
        ProviderModelStreamErrorEvent,
        ModelCallTerminalProjectionCommittedEvent,
        ModelCallEndEvent,
        RolloutBudgetReservationSettledEvent,
        ReplyEndEvent,
    ]
    assert events[-3].outcome == "provider_error"


def test_final_context_call_id_mismatch_rejected() -> None:
    runtime, transport, call, context = _call_and_context()
    context = replace(context, resolved_model_call_id=f"model_call:{'f' * 32}")
    with pytest.raises(Exception, match="call identity mismatch"):
        asyncio.run(_collect_runtime(runtime, call=call, context=context))
    assert transport.calls == 0


def test_final_context_identity_matches_call() -> None:
    runtime, transport, call, context = _call_and_context()
    events = asyncio.run(_collect_runtime(runtime, call=call, context=context))
    start = next(event for event in events if isinstance(event, ModelCallStartEvent))
    assert start.resolved_call == call.fact
    assert start.context_id == context.context_id
    assert transport.calls == 1


def test_final_context_target_fingerprint_mismatch_rejected() -> None:
    runtime, transport, call, context = _call_and_context()
    context = replace(context, target_fingerprint="sha256:mismatch")
    with pytest.raises(Exception, match="fingerprint mismatch"):
        asyncio.run(_collect_runtime(runtime, call=call, context=context))
    assert transport.calls == 0


def test_any_nonzero_compiler_pre_send_estimate_mismatch_is_rejected() -> None:
    runtime, transport, call, context = _call_and_context()
    context = replace(
        context,
        compiler_estimated_input_tokens=(context.compiler_estimated_input_tokens or 0)
        + 1,
    )
    with pytest.raises(ModelInputEstimateMismatch):
        asyncio.run(_collect_runtime(runtime, call=call, context=context))
    assert transport.calls == 0


def test_final_context_over_budget_rejected_before_reply_start() -> None:
    test_estimate_only_failure_writes_no_start_or_fake_end()


def test_final_context_over_budget_rejected_before_model_start() -> None:
    test_estimate_only_failure_writes_no_start_or_fake_end()


def test_final_context_over_budget_never_invokes_transport() -> None:
    test_estimate_only_failure_writes_no_start_or_fake_end()


def test_compiler_and_pre_send_estimates_are_equal() -> None:
    runtime, _transport, call, context = _call_and_context()
    events = asyncio.run(_collect_runtime(runtime, call=call, context=context))
    end = next(event for event in events if isinstance(event, ModelCallEndEvent))
    assert end.estimated_input_tokens == context.compiler_estimated_input_tokens


def test_llm_runtime_does_not_persist_model_call_rejected() -> None:
    # LLMRuntime has no RuntimeSession/EventLog dependency and yields nothing
    # before validation succeeds; rejection persistence is AgentRuntime-owned.
    test_estimate_only_failure_writes_no_start_or_fake_end()


def test_run_start_model_target_is_required() -> None:
    fields = run_start_permission_fields(EVENT_CONTEXT.run_id, user_input="x")
    fields.pop("model_target")
    with pytest.raises(ValidationError, match="model_target"):
        RunStartEvent(
            **EVENT_CONTEXT.event_fields(),
            **fields,
            user_input_chars=1,
        )


def test_model_call_start_resolved_fact_is_required() -> None:
    with pytest.raises(ValidationError, match="resolved_call"):
        ModelCallStartEvent(
            **EVENT_CONTEXT.event_fields(),
            context_id="context:test",
            model_call_index=1,
        )


def test_model_call_end_identity_is_required() -> None:
    with pytest.raises(ValidationError, match="resolved_model_call_id"):
        ModelCallEndEvent(
            **EVENT_CONTEXT.event_fields(),
            target_fingerprint="sha256:test",
            reported_model_id=None,
            outcome="completed",
            provider_dispatch_status="dispatched",
            usage_status="missing",
            usage=None,
            estimated_input_tokens=1,
            terminal_projection=model_terminal_projection_end_reference_fixture(
                "missing-model-call-id",
                outcome="completed",
            ),
        )


def test_model_call_end_reported_model_identity_is_required() -> None:
    _, _, call, _ = _call_and_context()
    with pytest.raises(ValidationError, match="reported_model_id"):
        ModelCallEndEvent(
            **EVENT_CONTEXT.event_fields(),
            resolved_model_call_id=call.fact.resolved_model_call_id,
            target_fingerprint=call.target.fact.target_fingerprint,
            outcome="completed",
            provider_dispatch_status="dispatched",
            usage_status="missing",
            usage=None,
            estimated_input_tokens=1,
            terminal_projection=model_terminal_projection_end_reference_fixture(
                call.fact.resolved_model_call_id,
                outcome="completed",
            ),
        )


def test_model_call_end_reported_usage_requires_fact() -> None:
    _, _, call, _ = _call_and_context()
    with pytest.raises(ValidationError, match="requires a usage fact"):
        ModelCallEndEvent(
            **EVENT_CONTEXT.event_fields(),
            resolved_model_call_id=call.fact.resolved_model_call_id,
            target_fingerprint=call.target.fact.target_fingerprint,
            reported_model_id=None,
            outcome="completed",
            provider_dispatch_status="dispatched",
            usage_status="reported",
            usage=None,
            estimated_input_tokens=1,
            terminal_projection=model_terminal_projection_end_reference_fixture(
                call.fact.resolved_model_call_id,
                outcome="completed",
            ),
        )


def test_model_call_end_missing_usage_requires_null() -> None:
    _, _, call, _ = _call_and_context()
    with pytest.raises(ValidationError, match="cannot contain"):
        ModelCallEndEvent(
            **EVENT_CONTEXT.event_fields(),
            resolved_model_call_id=call.fact.resolved_model_call_id,
            target_fingerprint=call.target.fact.target_fingerprint,
            reported_model_id=None,
            outcome="completed",
            provider_dispatch_status="dispatched",
            usage_status="missing",
            usage=ModelTokenUsageFact(input_tokens=1, output_tokens=1, total_tokens=2),
            estimated_input_tokens=1,
            terminal_projection=model_terminal_projection_end_reference_fixture(
                call.fact.resolved_model_call_id,
                outcome="completed",
            ),
        )


def test_context_compiled_resolved_fact_and_budget_are_required() -> None:
    _, _, call, _ = _call_and_context()
    fields = context_compiled_contract_fields(resolved_call=call.fact)
    fields.pop("budget")
    with pytest.raises(ValidationError, match="budget"):
        ContextCompiledEvent(
            **EVENT_CONTEXT.event_fields(),
            **fields,
            context_id="context:test",
            model_call_index=1,
        )


def test_model_call_rejected_round_trip() -> None:
    _, _, call, _ = _call_and_context()
    event = ModelCallRejectedEvent(
        **EVENT_CONTEXT.event_fields(),
        resolved_call=call.fact,
        context_id="context:test",
        model_call_index=1,
        reason_code="model_input_budget_exceeded",
        estimated_input_tokens=100,
        input_budget_tokens=call.target.fact.context_budget.input_budget_tokens,
    )
    assert load_agent_event(dump_agent_event(event)) == event


@pytest.mark.parametrize(
    "reason_code",
    ["model_input_budget_exceeded", "model_input_estimate_mismatch"],
)
def test_model_call_rejected_requires_estimate_after_estimation(
    reason_code: str,
) -> None:
    _, _, call, _ = _call_and_context()
    with pytest.raises(ValidationError, match="requires estimated_input_tokens"):
        ModelCallRejectedEvent(
            **EVENT_CONTEXT.event_fields(),
            resolved_call=call.fact,
            context_id="context:test",
            model_call_index=1,
            reason_code=reason_code,
            estimated_input_tokens=None,
            input_budget_tokens=call.target.fact.context_budget.input_budget_tokens,
        )


def test_model_call_rejected_allows_missing_estimate_before_estimation() -> None:
    _, _, call, _ = _call_and_context()
    event = ModelCallRejectedEvent(
        **EVENT_CONTEXT.event_fields(),
        resolved_call=call.fact,
        context_id="context:test",
        model_call_index=1,
        reason_code="model_target_binding_mismatch",
        estimated_input_tokens=None,
        input_budget_tokens=call.target.fact.context_budget.input_budget_tokens,
    )
    assert event.estimated_input_tokens is None


def test_old_model_call_event_payload_is_rejected() -> None:
    payload = {
        "type": "MODEL_CALL_START",
        "run_id": EVENT_CONTEXT.run_id,
        "turn_id": EVENT_CONTEXT.turn_id,
        "reply_id": EVENT_CONTEXT.reply_id,
        "model_name": "legacy",
        "model_role": "pro",
        "provider": "legacy",
    }
    with pytest.raises(ValidationError):
        load_agent_event(payload)
