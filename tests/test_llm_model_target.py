from __future__ import annotations

from dataclasses import replace

import pytest

from pulsara_agent.llm.adapters.openai.chat_completions import (
    build_chat_completions_payload,
    project_chat_context_bearing_payload_fields,
)
from pulsara_agent.llm.adapters.openai.client import OpenAITransportTimeoutPolicy
from pulsara_agent.llm.adapters.openai.responses import (
    build_responses_payload,
    project_responses_context_bearing_payload_fields,
)
from pulsara_agent.llm.errors import (
    ModelContextIdentityMismatch,
    ModelTargetCapabilityMismatch,
)
from pulsara_agent.llm.input import (
    FrozenPromptContent,
    LLMImagePart,
    LLMMessage,
    LLMTextPart,
    ToolSpec,
)
from pulsara_agent.llm.model_catalog import (
    ModelTargetKey,
    ReasoningEffortChoices,
    ReasoningFixedOn,
    ReasoningProviderDefault,
    ReasoningSelectableControls,
    ReasoningToggle,
    ReasoningUnavailable,
    WireApi,
    parse_models_dev_catalog,
    selectable_catalog,
)
from pulsara_agent.llm.model_connections import (
    ModelCallBinding,
    ModelConnectionAuthentication,
    ModelConnectionId,
    ReasoningBudgetSelection,
    ReasoningEffortSelection,
    ReasoningToggleSelection,
    UserDeclaredModelTarget,
)
from pulsara_agent.llm.model_target import (
    ModelReasoningSelectionInvalid,
    ModelTargetNotExecutable,
    canonicalize_endpoint,
    create_model_connection,
    create_user_declared_model_connection,
    default_reasoning_selection,
    reasoning_wire_fields,
    reconcile_model_call_binding,
    validate_reasoning_selection,
)
from pulsara_agent.llm.provider import RouteWireProfile
from pulsara_agent.llm.request import LLMContext
from pulsara_agent.llm.resolution import resolve_model_call, resolve_model_target
from pulsara_agent.llm.route_wires import production_route_wire_registry
from pulsara_agent.llm.validation import validate_model_context_shape_for_call
from pulsara_agent.primitives.model_call import (
    ModelCallPurpose,
    resolved_model_target_fingerprint,
)
from tests.support.model_config import test_model_runtime
from tests.test_llm_model_catalog import catalog_fixture


def _resolved(route: str, model: str, api: WireApi):
    catalog = selectable_catalog(parse_models_dev_catalog(catalog_fixture()))
    return create_model_connection(
        catalog=catalog,
        target=ModelTargetKey(route, api, model),
        route_wires=production_route_wire_registry(),
        connection_id=ModelConnectionId("model-connection:" + "a" * 32),
    )


def _resolved_call(
    route: str,
    model: str,
    api: WireApi,
    selection,
    *,
    fixture: dict[str, object] | None = None,
):
    catalog = selectable_catalog(
        parse_models_dev_catalog(fixture or catalog_fixture())
    )
    connection = create_model_connection(
        catalog=catalog,
        target=ModelTargetKey(route, api, model),
        route_wires=production_route_wire_registry(),
        connection_id=ModelConnectionId("model-connection:" + "c" * 32),
    ).config
    binding = ModelCallBinding(connection.id, selection)
    timeout = OpenAITransportTimeoutPolicy(1, 1, 1, 1, None)
    transport_registry = test_model_runtime(
        wire_api=api.value
    ).transport_registry(timeout)
    target = resolve_model_target(
        connection=connection,
        binding=binding,
        catalog=catalog,
        route_wires=production_route_wire_registry(),
        registry=transport_registry,
    )
    call = resolve_model_call(
        target=target,
        binding=binding,
        purpose=ModelCallPurpose.AGENT_MODEL_LOOP,
    )
    context = LLMContext(
        messages=(LLMMessage.user("hello"),),
        context_id="reasoning-wire-golden",
        resolved_model_call_id=call.resolved_model_call_id,
        target_fingerprint=call.target.fact.target_fingerprint,
        model_call_index=1,
    )
    return call, context


def test_exact_route_wire_and_model_resolve_to_different_control_domains() -> None:
    direct = _resolved(
        "zhipuai", "glm-5.3", WireApi.OPENAI_CHAT_COMPLETIONS
    )
    gateway = _resolved(
        "openrouter", "z-ai/glm-5.2", WireApi.OPENAI_CHAT_COMPLETIONS
    )

    assert default_reasoning_selection(direct.target.reasoning) == (
        ReasoningEffortSelection("high")
    )
    assert default_reasoning_selection(gateway.target.reasoning) == (
        ReasoningEffortSelection("xhigh")
    )
    with pytest.raises(ModelReasoningSelectionInvalid):
        validate_reasoning_selection(
            direct.target.reasoning, ReasoningEffortSelection("xhigh")
        )


@pytest.mark.parametrize("wire_api", tuple(WireApi))
def test_unregistered_models_dev_openai_compatible_route_uses_generic_wire_adapter(
    wire_api: WireApi,
) -> None:
    fixture = {
        "future-provider": {
            "name": "Future Provider",
            "npm": "@ai-sdk/openai-compatible",
            "api": "https://future.example/v1",
            "models": {
                "future-model": {
                    "reasoning": True,
                    "reasoning_options": [
                        {"type": "effort", "values": ["low", "high"]}
                    ],
                    "tool_call": True,
                    "limit": {"context": 256_000, "output": 8_192},
                }
            },
        }
    }
    call, _context = _resolved_call(
        "future-provider",
        "future-model",
        wire_api,
        ReasoningEffortSelection("high"),
        fixture=fixture,
    )

    assert call.target.fact.canonical_endpoint_base_url == "https://future.example/v1"
    assert call.target.fact.route_id == "future-provider"
    assert call.target.fact.model_id == "future-model"
    assert call.target.contract.route_wire.transport_binding_id == (
        "pulsara.openai.chat_completions"
        if wire_api is WireApi.OPENAI_CHAT_COMPLETIONS
        else "pulsara.openai.responses"
    )


def test_deepseek_uses_existing_generic_chat_and_responses_request_builders() -> None:
    chat_call, chat_context = _resolved_call(
        "deepseek",
        "deepseek-v4-flash",
        WireApi.OPENAI_CHAT_COMPLETIONS,
        ReasoningEffortSelection("high"),
    )
    chat_payload = build_chat_completions_payload(
        call=chat_call, context=chat_context
    )
    assert chat_payload["model"] == "deepseek-v4-flash"
    assert chat_payload["reasoning_effort"] == "high"

    responses_call, responses_context = _resolved_call(
        "deepseek",
        "deepseek-v4-flash",
        WireApi.OPENAI_RESPONSES,
        ReasoningEffortSelection("high"),
    )
    responses_payload = build_responses_payload(
        call=responses_call, context=responses_context
    )
    assert responses_payload["model"] == "deepseek-v4-flash"
    assert responses_payload["reasoning"] == {
        "effort": "high",
        "summary": "auto",
    }


@pytest.mark.parametrize("wire_api", tuple(WireApi))
def test_openai_endpoint_fallback_does_not_duplicate_a_wire_contract(
    wire_api: WireApi,
) -> None:
    fixture = {
        "openai": {
            "name": "OpenAI",
            "npm": "@ai-sdk/openai",
            "models": {
                "gpt-test": {
                    "reasoning": True,
                    "reasoning_options": [
                        {"type": "effort", "values": ["low", "high"]}
                    ],
                    "tool_call": True,
                    "limit": {"context": 256_000, "output": 8_192},
                }
            },
        }
    }
    call, _context = _resolved_call(
        "openai",
        "gpt-test",
        wire_api,
        ReasoningEffortSelection("high"),
        fixture=fixture,
    )

    assert call.target.fact.canonical_endpoint_base_url == "https://api.openai.com/v1"
    assert call.target.contract.route_wire.model_identity_policy.value == (
        "accept_reported"
    )


def test_exact_wire_lowering_belongs_to_generic_wire_api_adapter() -> None:
    direct = _resolved(
        "zhipuai", "glm-5.3", WireApi.OPENAI_CHAT_COMPLETIONS
    )
    gateway = _resolved(
        "openrouter", "z-ai/glm-5.2", WireApi.OPENAI_CHAT_COMPLETIONS
    )

    zhipu = reasoning_wire_fields(
        direct.target, ReasoningEffortSelection("max")
    )
    router = reasoning_wire_fields(
        gateway.target, ReasoningEffortSelection("xhigh")
    )
    zhipu_responses = reasoning_wire_fields(
        _resolved("zhipuai", "glm-5.3", WireApi.OPENAI_RESPONSES).target,
        ReasoningEffortSelection("max"),
    )
    assert dict(zhipu.root) == {"reasoning_effort": "max"}
    assert dict(zhipu.extra_body) == {}
    assert dict(router.root) == {"reasoning_effort": "xhigh"}
    assert dict(router.extra_body) == {}
    assert dict(zhipu_responses.root) == {
        "reasoning": {"effort": "max", "summary": "auto"}
    }
    assert dict(zhipu_responses.extra_body) == {}


@pytest.mark.parametrize("enabled", (False, True))
def test_generic_chat_toggle_lowers_to_reasoning_enabled(enabled: bool) -> None:
    fixture = {
        "future-provider": {
            "name": "Future Provider",
            "npm": "@ai-sdk/openai-compatible",
            "api": "https://future.example/v1",
            "models": {
                "future-toggle-model": {
                    "reasoning": True,
                    "reasoning_options": [{"type": "toggle"}],
                    "tool_call": True,
                    "limit": {"context": 256_000, "output": 8_192},
                }
            },
        }
    }
    selection = ReasoningToggleSelection(enabled)
    call, context = _resolved_call(
        "future-provider",
        "future-toggle-model",
        WireApi.OPENAI_CHAT_COMPLETIONS,
        selection,
        fixture=fixture,
    )

    assert isinstance(call.target.contract.reasoning, ReasoningSelectableControls)
    assert call.target.contract.reasoning.toggle is not None
    assert default_reasoning_selection(call.target.contract.reasoning) == (
        ReasoningToggleSelection(True)
    )
    fields = reasoning_wire_fields(call.target.contract, selection)
    assert dict(fields.root) == {}
    assert dict(fields.extra_body) == {"reasoning": {"enabled": enabled}}

    payload = build_chat_completions_payload(call=call, context=context)
    assert payload["extra_body"] == {"reasoning": {"enabled": enabled}}
    assert "reasoning" not in payload
    assert "reasoning_effort" not in payload
    assert "reasoning" not in project_chat_context_bearing_payload_fields(payload)


def test_generic_adapter_exposes_catalog_effort_without_route_specific_controls() -> None:
    gateway = _resolved(
        "openrouter", "z-ai/glm-5.2", WireApi.OPENAI_RESPONSES
    )

    assert isinstance(gateway.target.reasoning, ReasoningSelectableControls)
    assert gateway.target.reasoning.effort is not None
    assert gateway.target.reasoning.effort.values == ("high", "xhigh")
    assert gateway.target.reasoning.toggle is None
    assert gateway.target.reasoning.budget is None
    with pytest.raises(ModelReasoningSelectionInvalid):
        validate_reasoning_selection(
            gateway.target.reasoning, ReasoningToggleSelection(False)
        )
    with pytest.raises(ModelReasoningSelectionInvalid):
        validate_reasoning_selection(
            gateway.target.reasoning, ReasoningBudgetSelection(2048)
        )
    fields = reasoning_wire_fields(
        gateway.target, ReasoningEffortSelection("xhigh")
    )
    assert dict(fields.root) == {
        "reasoning": {"effort": "xhigh", "summary": "auto"}
    }
    assert dict(fields.extra_body) == {}


def test_stale_choice_reconciles_to_target_default_but_legal_choice_stays_locked() -> None:
    resolved = _resolved(
        "zhipuai", "glm-5.3", WireApi.OPENAI_CHAT_COMPLETIONS
    )
    stale = ModelCallBinding(
        resolved.config.id, ReasoningEffortSelection("medium")
    )
    updated, changed = reconcile_model_call_binding(
        current=stale,
        connection=resolved.config,
        target=resolved.target,
    )
    assert changed
    assert updated.reasoning == ReasoningEffortSelection("high")

    same, changed = reconcile_model_call_binding(
        current=ModelCallBinding(
            resolved.config.id, ReasoningEffortSelection("max")
        ),
        connection=resolved.config,
        target=resolved.target,
    )
    assert not changed
    assert same.reasoning == ReasoningEffortSelection("max")


def test_adapter_unsupported_reasoning_families_degrade_locally() -> None:
    fixture = catalog_fixture()
    fixture["zhipuai"]["models"]["glm-5.3"]["reasoning_options"] = [  # type: ignore[index]
        {"type": "budget_tokens"}
    ]
    catalog = selectable_catalog(parse_models_dev_catalog(fixture))
    resolved = create_model_connection(
        catalog=catalog,
        target=ModelTargetKey(
            "zhipuai", WireApi.OPENAI_CHAT_COMPLETIONS, "glm-5.3"
        ),
        route_wires=production_route_wire_registry(),
    )
    assert isinstance(resolved.target.reasoning, ReasoningProviderDefault)
    assert default_reasoning_selection(resolved.target.reasoning) is None


def test_unknown_model_and_provider_native_wire_are_not_inferred() -> None:
    catalog = selectable_catalog(parse_models_dev_catalog(catalog_fixture()))
    registry = production_route_wire_registry()
    with pytest.raises(ModelTargetNotExecutable, match="outside the selectable"):
        create_model_connection(
            catalog=catalog,
            target=ModelTargetKey(
                "zhipuai", WireApi.OPENAI_CHAT_COMPLETIONS, "glm"
            ),
            route_wires=registry,
        )
    with pytest.raises((ModelTargetNotExecutable, KeyError), match="adapter"):
        create_model_connection(
            catalog=catalog,
            target=ModelTargetKey(
                "filtered", WireApi.OPENAI_RESPONSES, "ordinary-model"
            ),
            route_wires=registry,
        )


def test_provider_native_route_cannot_borrow_the_generic_adapter() -> None:
    fixture = catalog_fixture()
    fixture["openrouter"]["npm"] = "@ai-sdk/anthropic"  # type: ignore[index]
    catalog = selectable_catalog(parse_models_dev_catalog(fixture))
    entry = catalog.require(
        ModelTargetKey(
            "openrouter",
            WireApi.OPENAI_CHAT_COMPLETIONS,
            "z-ai/glm-5.2",
        ).catalog_key
    )
    registry = production_route_wire_registry()

    assert not registry.supports(entry, WireApi.OPENAI_CHAT_COMPLETIONS)
    with pytest.raises(ModelTargetNotExecutable, match="adapter"):
        create_model_connection(
            catalog=catalog,
            target=ModelTargetKey(
                "openrouter",
                WireApi.OPENAI_CHAT_COMPLETIONS,
                "z-ai/glm-5.2",
            ),
            route_wires=registry,
        )


def test_static_route_defaults_cannot_take_reasoning_field_ownership() -> None:
    with pytest.raises(ValueError, match="reasoning owner"):
        RouteWireProfile(request_defaults={"reasoning": {"effort": "high"}})
    with pytest.raises(ValueError, match="reasoning owner"):
        RouteWireProfile(request_extra_body={"thinking": {"type": "enabled"}})


def test_canonical_endpoint_keeps_full_base_path() -> None:
    assert canonicalize_endpoint("https://API.Example.test:443/v1/") == (
        "https://api.example.test/v1"
    )
    assert canonicalize_endpoint("https://api.example.test/v1") != (
        canonicalize_endpoint("https://api.example.test/api/v4")
    )


@pytest.mark.parametrize(
    ("values", "expected"),
    (
        (("low", "high", "max"), ReasoningEffortSelection("high")),
        (("high", "max"), ReasoningEffortSelection("max")),
        (
            ("none", "low", "medium", "high", "xhigh", "max"),
            ReasoningEffortSelection("high"),
        ),
        ((None,), ReasoningEffortSelection(None)),
    ),
)
def test_default_effort_is_the_upper_middle_positive_catalog_choice(
    values: tuple[str | None, ...], expected: ReasoningEffortSelection
) -> None:
    fixture = catalog_fixture()
    fixture["zhipuai"]["models"]["glm-5.3"][  # type: ignore[index]
        "reasoning_options"
    ] = [{"type": "effort", "values": list(values)}]
    catalog = selectable_catalog(parse_models_dev_catalog(fixture))
    resolved = create_model_connection(
        catalog=catalog,
        target=ModelTargetKey(
            "zhipuai", WireApi.OPENAI_CHAT_COMPLETIONS, "glm-5.3"
        ),
        route_wires=production_route_wire_registry(),
    )
    assert default_reasoning_selection(resolved.target.reasoning) == expected


def test_openrouter_disabled_effort_does_not_alias_the_toggle_control() -> None:
    fixture = catalog_fixture()
    fixture["openrouter"]["models"]["z-ai/glm-5.2"][  # type: ignore[index]
        "reasoning_options"
    ][1]["values"] = ["none", "high"]  # type: ignore[index]
    catalog = selectable_catalog(parse_models_dev_catalog(fixture))
    resolved = create_model_connection(
        catalog=catalog,
        target=ModelTargetKey(
            "openrouter",
            WireApi.OPENAI_CHAT_COMPLETIONS,
            "z-ai/glm-5.2",
        ),
        route_wires=production_route_wire_registry(),
    )
    assert dict(
        reasoning_wire_fields(
            resolved.target, ReasoningEffortSelection("none")
        ).root
    ) == {"reasoning_effort": "none"}


def test_reasoning_request_golden_is_not_context_bearing() -> None:
    zhipu_call, zhipu_context = _resolved_call(
        "zhipuai",
        "glm-5.3",
        WireApi.OPENAI_CHAT_COMPLETIONS,
        ReasoningEffortSelection("max"),
    )
    zhipu_payload = build_chat_completions_payload(
        call=zhipu_call, context=zhipu_context
    )
    assert zhipu_payload["reasoning_effort"] == "max"
    assert "extra_body" not in zhipu_payload
    zhipu_projection = project_chat_context_bearing_payload_fields(zhipu_payload)
    assert "reasoning_effort" not in zhipu_projection
    assert "thinking" not in zhipu_projection

    openai_call, openai_context = _resolved_call(
        "openai",
        "gpt-test",
        WireApi.OPENAI_RESPONSES,
        ReasoningEffortSelection("high"),
        fixture={
            "openai": {
                "name": "OpenAI",
                "npm": "@ai-sdk/openai",
                "models": {
                    "gpt-test": {
                        "reasoning": True,
                        "reasoning_options": [
                            {"type": "effort", "values": ["low", "high"]}
                        ],
                        "tool_call": True,
                        "limit": {"context": 256_000, "output": 8_192},
                    }
                },
            }
        },
    )
    openai_payload = build_responses_payload(
        call=openai_call, context=openai_context
    )
    assert openai_payload["reasoning"] == {
        "effort": "high",
        "summary": "auto",
    }
    assert "reasoning" not in project_responses_context_bearing_payload_fields(
        openai_payload
    )


def test_reasoning_selection_is_excluded_from_target_compatibility_digest() -> None:
    low, _ = _resolved_call(
        "zhipuai",
        "glm-5.3",
        WireApi.OPENAI_CHAT_COMPLETIONS,
        ReasoningEffortSelection("low"),
    )
    maximum, _ = _resolved_call(
        "zhipuai",
        "glm-5.3",
        WireApi.OPENAI_CHAT_COMPLETIONS,
        ReasoningEffortSelection("max"),
    )
    fact = low.target.fact
    payload = {
        "route_id": "zhipuai",
        "wire_api": "openai_chat_completions",
        "model_id": "glm-5.3",
        "canonical_endpoint_base_url": "https://open.bigmodel.cn/api/paas/v4",
        "transport_binding_id": "pulsara.openai.chat_completions",
        "transport_contract_version": (
            "v6-route-target-reasoning-and-tool-correlation"
        ),
        "model_identity_policy": "accept_reported",
        "input_modalities": ("text",),
        "limits": {
            "total_context_tokens": 1_000_000,
            "max_input_tokens": 1_000_000,
            "max_output_tokens": 131_072,
            "default_output_tokens": 8_192,
            "input_safety_margin_tokens": 8_192,
        },
    }
    assert fact.target_fingerprint == resolved_model_target_fingerprint(payload)
    assert maximum.target.fact.target_fingerprint == fact.target_fingerprint
    assert "reasoning" not in payload
    assert "endpoint_fingerprint" not in payload


def test_explicit_tool_rejection_happens_before_provider_open() -> None:
    fixture = {
        "openai": {
            "name": "OpenAI",
            "npm": "@ai-sdk/openai",
            "models": {
                "no-tools": {
                    "reasoning": False,
                    "tool_call": False,
                    "limit": {"context": 256_000, "output": 8_192},
                }
            },
        }
    }
    call, context = _resolved_call(
        "openai",
        "no-tools",
        WireApi.OPENAI_RESPONSES,
        None,
        fixture=fixture,
    )
    context = LLMContext(
        messages=context.messages,
        tools=(ToolSpec("read", "Read a file", {"type": "object"}),),
        context_id=context.context_id,
        resolved_model_call_id=context.resolved_model_call_id,
        target_fingerprint=context.target_fingerprint,
        model_call_index=context.model_call_index,
    )
    with pytest.raises(ModelTargetCapabilityMismatch, match="does not support tools"):
        validate_model_context_shape_for_call(call=call, context=context)


@pytest.mark.parametrize(
    ("input_modalities", "accepted"),
    (
        (["text", "image"], True),
        (["text"], False),
        (None, True),
    ),
)
def test_frozen_target_input_modalities_gate_image_shape_before_provider_open(
    input_modalities: list[str] | None,
    accepted: bool,
) -> None:
    fixture = catalog_fixture()
    model = fixture["zhipuai"]["models"]["glm-5.3"]  # type: ignore[index]
    if input_modalities is None:
        model.pop("modalities")  # type: ignore[union-attr]
    else:
        model["modalities"] = {"input": input_modalities}  # type: ignore[index]
    call, context = _resolved_call(
        "zhipuai",
        "glm-5.3",
        WireApi.OPENAI_CHAT_COMPLETIONS,
        ReasoningEffortSelection("high"),
        fixture=fixture,
    )
    image = LLMImagePart(
        media_type="image/png",
        immutable_bytes=b"validated-image-value",
        width=31,
        height=31,
    )
    context = replace(
        context,
        messages=(
            LLMMessage.user_content(
                FrozenPromptContent(
                    (LLMTextPart("before"), image, LLMTextPart("after"))
                )
            ),
        ),
    )

    expected = None if input_modalities is None else tuple(input_modalities)
    assert call.target.fact.input_modalities == expected
    if accepted:
        validate_model_context_shape_for_call(call=call, context=context)
        validate_model_context_shape_for_call(
            call=call,
            context=replace(
                context,
                messages=(
                    LLMMessage.user_content(FrozenPromptContent((image,))),
                ),
            ),
        )
    else:
        with pytest.raises(ModelTargetCapabilityMismatch, match="image input"):
            validate_model_context_shape_for_call(call=call, context=context)


def test_internal_empty_user_text_keeps_its_existing_closed_shape() -> None:
    call, context = _resolved_call(
        "zhipuai",
        "glm-5.3",
        WireApi.OPENAI_CHAT_COMPLETIONS,
        ReasoningEffortSelection("high"),
    )

    validate_model_context_shape_for_call(
        call=call,
        context=replace(context, messages=(LLMMessage.user(""),)),
    )
    with pytest.raises(ModelContextIdentityMismatch, match="invalid closed shape"):
        validate_model_context_shape_for_call(
            call=call,
            context=replace(
                context,
                messages=(LLMMessage(role=LLMMessage.user("").role),),
            ),
        )


def test_input_modalities_are_part_of_the_frozen_target_identity() -> None:
    text_fixture = catalog_fixture()
    image_fixture = catalog_fixture()
    image_fixture["zhipuai"]["models"]["glm-5.3"]["modalities"] = {  # type: ignore[index]
        "input": ["text", "image"]
    }
    text_call, _ = _resolved_call(
        "zhipuai",
        "glm-5.3",
        WireApi.OPENAI_CHAT_COMPLETIONS,
        ReasoningEffortSelection("high"),
        fixture=text_fixture,
    )
    image_call, _ = _resolved_call(
        "zhipuai",
        "glm-5.3",
        WireApi.OPENAI_CHAT_COMPLETIONS,
        ReasoningEffortSelection("high"),
        fixture=image_fixture,
    )

    assert text_call.target.fact.input_modalities == ("text",)
    assert image_call.target.fact.input_modalities == ("text", "image")
    assert (
        text_call.target.fact.target_fingerprint
        != image_call.target.fact.target_fingerprint
    )


@pytest.mark.parametrize(
    ("reasoning", "options", "expected_type"),
    (
        (False, None, ReasoningUnavailable),
        (True, [], ReasoningFixedOn),
        (True, None, ReasoningProviderDefault),
    ),
)
def test_no_selector_targets_omit_reasoning_request_fields(
    reasoning: bool,
    options: list[object] | None,
    expected_type: type,
) -> None:
    model: dict[str, object] = {
        "reasoning": reasoning,
        "tool_call": True,
        "limit": {"context": 256_000, "output": 8_192},
    }
    if options is not None:
        model["reasoning_options"] = options
    fixture = {
        "openrouter": {
            "name": "OpenRouter",
            "npm": "@openrouter/ai-sdk-provider",
            "api": "https://openrouter.ai/api/v1",
            "models": {"fixture/no-selector": model},
        }
    }
    call, context = _resolved_call(
        "openrouter",
        "fixture/no-selector",
        WireApi.OPENAI_CHAT_COMPLETIONS,
        None,
        fixture=fixture,
    )
    assert isinstance(call.target.contract.reasoning, expected_type)
    payload = build_chat_completions_payload(call=call, context=context)
    assert "reasoning" not in payload
    assert "reasoning_effort" not in payload
    assert "extra_body" not in payload


@pytest.mark.parametrize("input_modalities", (None, ("text",), ("text", "image")))
def test_user_declared_target_resolves_without_catalog_and_uses_generic_chat(
    input_modalities: tuple[str, ...] | None,
) -> None:
    route_wires = production_route_wire_registry()
    resolved = create_user_declared_model_connection(
        model_id="local-model",
        wire_api=WireApi.OPENAI_CHAT_COMPLETIONS,
        base_url="http://127.0.0.1:9000/v1/",
        declaration=UserDeclaredModelTarget(
            configuration_name="Local Gateway",
            input_modalities=input_modalities,
            total_context_tokens=300_000,
            max_output_tokens=12_000,
            tool_call=True,
            reasoning=ReasoningSelectableControls(
                effort=ReasoningEffortChoices(("low", "high"))
            ),
            authentication=ModelConnectionAuthentication.NONE,
        ),
        route_wires=route_wires,
        connection_id=ModelConnectionId("model-connection:" + "d" * 32),
    )
    binding = ModelCallBinding(
        resolved.config.id,
        ReasoningEffortSelection("high"),
    )
    registry = test_model_runtime(
        wire_api=WireApi.OPENAI_CHAT_COMPLETIONS.value
    ).transport_registry(OpenAITransportTimeoutPolicy(1, 1, 1, 1, None))
    target = resolve_model_target(
        connection=resolved.config,
        binding=binding,
        catalog=None,
        route_wires=route_wires,
        registry=registry,
    )
    call = resolve_model_call(
        target=target,
        binding=binding,
        purpose=ModelCallPurpose.AGENT_MODEL_LOOP,
    )
    context = LLMContext(
        messages=(LLMMessage.user("hello"),),
        context_id="custom-target",
        resolved_model_call_id=call.resolved_model_call_id,
        target_fingerprint=call.target.fact.target_fingerprint,
        model_call_index=1,
    )

    assert target.contract.target_facts.route_name == "Local Gateway"
    assert target.contract.target_facts.limits.total_context_tokens == 300_000
    assert target.contract.target_facts.input_modalities == input_modalities
    assert target.fact.input_modalities == input_modalities
    assert target.contract.canonical_endpoint_base_url == "http://127.0.0.1:9000/v1"
    assert build_chat_completions_payload(call=call, context=context)[
        "reasoning_effort"
    ] == "high"

    image_context = replace(
        context,
        messages=(LLMMessage.user_content(FrozenPromptContent((
            LLMImagePart(
                media_type="image/png", immutable_bytes=b"validated-image-value",
                width=31, height=31,
            ),
        ))),),
    )
    if input_modalities == ("text",):
        with pytest.raises(ModelTargetCapabilityMismatch, match="image input"):
            validate_model_context_shape_for_call(call=call, context=image_context)
    else:
        validate_model_context_shape_for_call(call=call, context=image_context)


def test_user_declared_reasoning_control_cannot_be_silently_dropped() -> None:
    with pytest.raises(ModelTargetNotExecutable, match="unsupported"):
        create_user_declared_model_connection(
            model_id="local-model",
            wire_api=WireApi.OPENAI_RESPONSES,
            base_url="http://127.0.0.1:9000/v1",
            declaration=UserDeclaredModelTarget(
                configuration_name="Responses Toggle",
                total_context_tokens=256_000,
                max_output_tokens=8_192,
                tool_call=True,
                reasoning=ReasoningSelectableControls(toggle=ReasoningToggle()),
                authentication=ModelConnectionAuthentication.NONE,
            ),
            route_wires=production_route_wire_registry(),
        )
