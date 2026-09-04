from __future__ import annotations

import pytest

from pulsara_agent.llm.model_catalog import ModelTargetKey, WireApi
from pulsara_agent.llm.model_connections import (
    ModelCallBinding,
    ModelConnectionConfig,
    ModelConnectionId,
    ReasoningBudgetSelection,
    ReasoningEffortSelection,
    ReasoningToggleSelection,
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
