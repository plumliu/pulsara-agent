"""Closed local model-connection and per-turn binding values."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from uuid import uuid4

from pulsara_agent.llm.model_catalog import ModelTargetKey, WireApi


@dataclass(frozen=True, slots=True, order=True)
class ModelConnectionId:
    value: str

    def __post_init__(self) -> None:
        if not self.value.startswith("model-connection:"):
            raise ValueError("model connection ID has an invalid namespace")
        suffix = self.value.removeprefix("model-connection:")
        if len(suffix) != 32 or any(character not in "0123456789abcdef" for character in suffix):
            raise ValueError("model connection ID must contain a lowercase UUID hex")

    @classmethod
    def new(cls) -> "ModelConnectionId":
        return cls(f"model-connection:{uuid4().hex}")


@dataclass(frozen=True, slots=True)
class ModelConnectionConfig:
    id: ModelConnectionId
    target: ModelTargetKey
    base_url: str

    def __post_init__(self) -> None:
        if not self.base_url or self.base_url != self.base_url.strip():
            raise ValueError("model connection base URL must be non-empty canonical text")


@dataclass(frozen=True, slots=True)
class ReasoningToggleSelection:
    enabled: bool


@dataclass(frozen=True, slots=True)
class ReasoningEffortSelection:
    value: str | None

    def __post_init__(self) -> None:
        if self.value is not None and (not self.value or self.value != self.value.strip()):
            raise ValueError("reasoning effort selection is invalid")


@dataclass(frozen=True, slots=True)
class ReasoningBudgetSelection:
    tokens: int

    def __post_init__(self) -> None:
        if self.tokens < 1:
            raise ValueError("reasoning budget selection must be positive")


ReasoningSelection = (
    ReasoningToggleSelection | ReasoningEffortSelection | ReasoningBudgetSelection
)


@dataclass(frozen=True, slots=True)
class ModelCallBinding:
    connection_id: ModelConnectionId
    reasoning: ReasoningSelection | None


def model_connection_to_dict(value: ModelConnectionConfig) -> dict[str, object]:
    return {
        "id": value.id.value,
        "route_id": value.target.route_id,
        "wire_api": value.target.wire_api.value,
        "model_id": value.target.model_id,
        "base_url": value.base_url,
    }


def model_connection_from_dict(value: object) -> ModelConnectionConfig:
    if not isinstance(value, dict) or set(value) != {
        "id",
        "route_id",
        "wire_api",
        "model_id",
        "base_url",
    }:
        raise ValueError("model connection metadata has an invalid closed shape")
    if not all(isinstance(value[key], str) for key in value):
        raise ValueError("model connection metadata fields must be strings")
    return ModelConnectionConfig(
        id=ModelConnectionId(value["id"]),
        target=ModelTargetKey(
            route_id=value["route_id"],
            wire_api=WireApi(value["wire_api"]),
            model_id=value["model_id"],
        ),
        base_url=value["base_url"],
    )


def reasoning_selection_to_dict(
    selection: ReasoningSelection | None,
) -> dict[str, object] | None:
    if selection is None:
        return None
    if isinstance(selection, ReasoningToggleSelection):
        return {"kind": "toggle", "enabled": selection.enabled}
    if isinstance(selection, ReasoningEffortSelection):
        return {"kind": "effort", "value": selection.value}
    if isinstance(selection, ReasoningBudgetSelection):
        return {"kind": "budget_tokens", "tokens": selection.tokens}
    raise TypeError(type(selection).__name__)


def reasoning_selection_from_dict(value: object) -> ReasoningSelection | None:
    if value is None:
        return None
    if not isinstance(value, dict) or not isinstance(value.get("kind"), str):
        raise ValueError("reasoning selection must be a closed object or null")
    kind = value["kind"]
    if kind == "toggle" and set(value) == {"kind", "enabled"}:
        enabled = value["enabled"]
        if not isinstance(enabled, bool):
            raise ValueError("reasoning toggle must be boolean")
        return ReasoningToggleSelection(enabled)
    if kind == "effort" and set(value) == {"kind", "value"}:
        effort = value["value"]
        if effort is not None and not isinstance(effort, str):
            raise ValueError("reasoning effort must be text or null")
        return ReasoningEffortSelection(effort)
    if kind == "budget_tokens" and set(value) == {"kind", "tokens"}:
        tokens = value["tokens"]
        if isinstance(tokens, bool) or not isinstance(tokens, int):
            raise ValueError("reasoning budget must be an integer")
        return ReasoningBudgetSelection(tokens)
    raise ValueError("reasoning selection kind or shape is invalid")


def model_call_binding_to_dict(value: ModelCallBinding | None) -> dict[str, object] | None:
    if value is None:
        return None
    return {
        "connection_id": value.connection_id.value,
        "reasoning": reasoning_selection_to_dict(value.reasoning),
    }


def model_call_binding_from_dict(value: object) -> ModelCallBinding | None:
    if value is None:
        return None
    if not isinstance(value, dict) or set(value) != {"connection_id", "reasoning"}:
        raise ValueError("model call binding has an invalid closed shape")
    connection_id = value["connection_id"]
    if not isinstance(connection_id, str):
        raise ValueError("model call binding connection ID must be text")
    return ModelCallBinding(
        connection_id=ModelConnectionId(connection_id),
        reasoning=reasoning_selection_from_dict(value["reasoning"]),
    )


def freeze_binding_json(value: ModelCallBinding | None) -> dict[str, Any] | None:
    """Return the ordinary JSON object copied into session/queue/turn rows."""

    return model_call_binding_to_dict(value)


__all__ = [
    "ModelCallBinding",
    "ModelConnectionConfig",
    "ModelConnectionId",
    "ReasoningBudgetSelection",
    "ReasoningEffortSelection",
    "ReasoningSelection",
    "ReasoningToggleSelection",
    "freeze_binding_json",
    "model_call_binding_from_dict",
    "model_call_binding_to_dict",
    "model_connection_from_dict",
    "model_connection_to_dict",
    "reasoning_selection_from_dict",
    "reasoning_selection_to_dict",
]
