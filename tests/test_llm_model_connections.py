from __future__ import annotations

import pytest

from pulsara_agent.llm.model_catalog import (
    ModelTargetKey,
    ReasoningEffortChoices,
    ReasoningProviderDefault,
    ReasoningSelectableControls,
    ReasoningToggle,
    WireApi,
)
from pulsara_agent.llm.model_connections import (
    ModelCallBinding,
    ModelConnectionConfig,
    ModelConnectionAuthentication,
    ModelConnectionId,
    ReasoningBudgetSelection,
    ReasoningEffortSelection,
    ReasoningToggleSelection,
    UserDeclaredModelTarget,
    model_call_binding_from_dict,
    model_call_binding_to_dict,
    model_connection_from_dict,
    model_connection_to_dict,
)


def test_model_connection_closed_codec_preserves_exact_target() -> None:
    value = ModelConnectionConfig(
        ModelConnectionId("model-connection:" + "1" * 32),
        ModelTargetKey(
            "openrouter",
            WireApi.OPENAI_RESPONSES,
            "openai/gpt-5.6-luna",
        ),
        "https://openrouter.ai/api/v1",
    )
    assert model_connection_from_dict(model_connection_to_dict(value)) == value


def test_user_declared_connection_closed_codec_preserves_asserted_facts() -> None:
    value = ModelConnectionConfig(
        ModelConnectionId("model-connection:" + "5" * 32),
        ModelTargetKey(
            "user_declared",
            WireApi.OPENAI_CHAT_COMPLETIONS,
            "local-model",
        ),
        "http://127.0.0.1:9000/v1",
        UserDeclaredModelTarget(
            configuration_name="Local Gateway",
            total_context_tokens=256_000,
            max_output_tokens=16_384,
            tool_call=True,
            reasoning=ReasoningSelectableControls(
                effort=ReasoningEffortChoices(("low", "high"))
            ),
            authentication=ModelConnectionAuthentication.NONE,
        ),
    )

    encoded = model_connection_to_dict(value)
    assert model_connection_from_dict(encoded) == value
    assert encoded["user_declared"] == {
        "configuration_name": "Local Gateway",
        "total_context_tokens": 256_000,
        "max_output_tokens": 16_384,
        "tool_call": True,
        "reasoning": {"kind": "effort", "values": ["low", "high"]},
        "authentication": "none",
    }
    assert value.requires_api_key is False


def test_user_declared_target_enforces_minimum_without_extra_reasoning_shapes() -> None:
    with pytest.raises(ValueError, match="below the product minimum"):
        UserDeclaredModelTarget(
            "Too Small",
            255_999,
            8_192,
            True,
            ReasoningProviderDefault(),
            ModelConnectionAuthentication.BEARER_API_KEY,
        )

    with pytest.raises(ValueError, match="source union"):
        ModelConnectionConfig(
            ModelConnectionId("model-connection:" + "6" * 32),
            ModelTargetKey(
                "catalog-route",
                WireApi.OPENAI_CHAT_COMPLETIONS,
                "local-model",
            ),
            "http://127.0.0.1:9000/v1",
            UserDeclaredModelTarget(
                "Wrong Source",
                256_000,
                8_192,
                True,
                ReasoningProviderDefault(),
                ModelConnectionAuthentication.NONE,
            ),
        )

    with pytest.raises(ValueError, match="one supported control"):
        UserDeclaredModelTarget(
            "Mixed Controls",
            256_000,
            8_192,
            True,
            ReasoningSelectableControls(
                effort=ReasoningEffortChoices(("low", "high")),
                toggle=ReasoningToggle(),
            ),
            ModelConnectionAuthentication.BEARER_API_KEY,
        )


@pytest.mark.parametrize(
    "selection",
    (
        None,
        ReasoningEffortSelection("xhigh"),
        ReasoningEffortSelection(None),
        ReasoningToggleSelection(False),
        ReasoningBudgetSelection(8192),
    ),
)
def test_model_call_binding_closed_codec_round_trips(selection) -> None:
    value = ModelCallBinding(
        ModelConnectionId("model-connection:" + "2" * 32), selection
    )
    assert model_call_binding_from_dict(model_call_binding_to_dict(value)) == value


def test_binding_does_not_duplicate_target() -> None:
    rendered = model_call_binding_to_dict(
        ModelCallBinding(
            ModelConnectionId("model-connection:" + "3" * 32),
            ReasoningEffortSelection("high"),
        )
    )
    assert rendered is not None
    assert set(rendered) == {"connection_id", "reasoning"}
    assert "target" not in rendered
    assert "fingerprint" not in str(rendered)


def test_unknown_binding_fields_are_rejected() -> None:
    with pytest.raises(ValueError, match="closed shape"):
        model_call_binding_from_dict(
            {
                "connection_id": "model-connection:" + "4" * 32,
                "reasoning": None,
                "target": {},
            }
        )
