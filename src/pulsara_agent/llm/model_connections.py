"""Closed local model-connection and per-turn binding values."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Any
from uuid import uuid4

from pulsara_agent.llm.model_catalog import (
    MINIMUM_SELECTABLE_CONTEXT_TOKENS,
    ModelHardLimits,
    ModelTargetKey,
    ReasoningControlContract,
    ReasoningEffortChoices,
    ReasoningProviderDefault,
    ReasoningSelectableControls,
    ReasoningToggle,
    WireApi,
)


USER_DECLARED_MODEL_ROUTE_ID = "user_declared"


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


class ModelConnectionAuthentication(StrEnum):
    BEARER_API_KEY = "bearer_api_key"
    NONE = "none"


class ReasoningWireProfile(StrEnum):
    """Explicit reasoning request shape selected for one wire protocol."""

    PROVIDER_DEFAULT = "provider_default"
    EFFORT = "effort"
    TOGGLE = "toggle"
    ENABLE_THINKING = "enable_thinking"
    THINKING_TYPE = "thinking_type"
    THINKING_EFFORT = "thinking_effort"
    BROAD_COMPAT = "broad_compat"
    CATALOG_STANDARD = "catalog_standard"


@dataclass(frozen=True, slots=True)
class UserDeclaredModelTarget:
    """Closed operator assertion for one OpenAI-compatible custom target."""

    configuration_name: str
    total_context_tokens: int
    max_output_tokens: int
    tool_call: bool
    reasoning: ReasoningControlContract
    authentication: ModelConnectionAuthentication
    input_modalities: tuple[str, ...] | None = None

    def __post_init__(self) -> None:
        if (
            not self.configuration_name
            or self.configuration_name != self.configuration_name.strip()
        ):
            raise ValueError("custom model configuration name must be canonical text")
        if self.total_context_tokens < MINIMUM_SELECTABLE_CONTEXT_TOKENS:
            raise ValueError("custom model context window is below the product minimum")
        ModelHardLimits(
            self.total_context_tokens,
            self.total_context_tokens,
            self.max_output_tokens,
        )
        if not isinstance(self.tool_call, bool):
            raise TypeError("custom model tool-call support must be boolean")
        if self.input_modalities is not None and (
            not isinstance(self.input_modalities, tuple)
            or any(
                not isinstance(value, str) or not value or value != value.strip()
                for value in self.input_modalities
            )
        ):
            raise ValueError("custom model input modalities must be canonical text values")
        if not isinstance(self.authentication, ModelConnectionAuthentication):
            raise TypeError("custom model authentication must be typed")
        if isinstance(self.reasoning, ReasoningProviderDefault):
            return
        if not isinstance(self.reasoning, ReasoningSelectableControls):
            raise ValueError("custom model reasoning must be provider default or selectable")
        selectable = tuple(
            item is not None
            for item in (
                self.reasoning.effort,
                self.reasoning.toggle,
                self.reasoning.budget,
            )
        )
        if sum(selectable) != 1 or self.reasoning.budget is not None:
            raise ValueError("custom model reasoning must select one supported control")
        if self.reasoning.effort is not None and any(
            value is None for value in self.reasoning.effort.values
        ):
            raise ValueError("custom model effort choices must be explicit text")


@dataclass(frozen=True, slots=True)
class ModelConnectionConfig:
    id: ModelConnectionId
    target: ModelTargetKey
    base_url: str
    reasoning_wire_profile: ReasoningWireProfile
    user_declared: UserDeclaredModelTarget | None = None

    def __post_init__(self) -> None:
        if not self.base_url or self.base_url != self.base_url.strip():
            raise ValueError("model connection base URL must be non-empty canonical text")
        declared_route = self.target.route_id == USER_DECLARED_MODEL_ROUTE_ID
        if declared_route != (self.user_declared is not None):
            raise ValueError("model connection source union is invalid")
        if not isinstance(self.reasoning_wire_profile, ReasoningWireProfile):
            raise TypeError("model connection reasoning profile must be typed")
        if self.user_declared is not None:
            _validate_declared_reasoning_profile(
                self.reasoning_wire_profile,
                self.user_declared.reasoning,
            )

    @property
    def authentication(self) -> ModelConnectionAuthentication:
        if self.user_declared is None:
            return ModelConnectionAuthentication.BEARER_API_KEY
        return self.user_declared.authentication

    @property
    def requires_api_key(self) -> bool:
        return self.authentication is ModelConnectionAuthentication.BEARER_API_KEY


def _validate_declared_reasoning_profile(
    profile: ReasoningWireProfile,
    reasoning: ReasoningControlContract,
) -> None:
    if isinstance(reasoning, ReasoningProviderDefault):
        if profile is not ReasoningWireProfile.PROVIDER_DEFAULT:
            raise ValueError("provider-default reasoning requires its matching profile")
        return
    if not isinstance(reasoning, ReasoningSelectableControls):
        raise ValueError("custom model reasoning must be provider default or selectable")
    effort_profiles = {
        ReasoningWireProfile.EFFORT,
        ReasoningWireProfile.THINKING_EFFORT,
        ReasoningWireProfile.BROAD_COMPAT,
    }
    toggle_profiles = {
        ReasoningWireProfile.TOGGLE,
        ReasoningWireProfile.ENABLE_THINKING,
        ReasoningWireProfile.THINKING_TYPE,
    }
    if (
        (reasoning.effort is not None) != (profile in effort_profiles)
        or (reasoning.toggle is not None) != (profile in toggle_profiles)
        or profile
        in {
            ReasoningWireProfile.PROVIDER_DEFAULT,
            ReasoningWireProfile.CATALOG_STANDARD,
        }
    ):
        raise ValueError("custom model reasoning control and wire profile disagree")


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
    payload: dict[str, object] = {
        "id": value.id.value,
        "route_id": value.target.route_id,
        "wire_api": value.target.wire_api.value,
        "model_id": value.target.model_id,
        "base_url": value.base_url,
    }
    if value.user_declared is not None:
        payload["user_declared"] = _user_declared_model_target_to_dict(
            value.user_declared,
            value.reasoning_wire_profile,
        )
    elif value.reasoning_wire_profile is not ReasoningWireProfile.CATALOG_STANDARD:
        payload["reasoning_wire_profile"] = value.reasoning_wire_profile.value
    return payload


def model_connection_from_dict(value: object) -> ModelConnectionConfig:
    common = {
        "id",
        "route_id",
        "wire_api",
        "model_id",
        "base_url",
    }
    if not isinstance(value, dict):
        raise ValueError("model connection metadata has an invalid closed shape")
    keys = frozenset(value)
    if keys not in {
        frozenset(common),
        frozenset((*common, "reasoning_wire_profile")),
        frozenset((*common, "user_declared")),
    }:
        raise ValueError("model connection metadata has an invalid closed shape")
    if not all(isinstance(value[key], str) for key in common):
        raise ValueError("model connection metadata fields must be strings")
    raw_declared = value.get("user_declared")
    if raw_declared is None:
        raw_profile = value.get(
            "reasoning_wire_profile", ReasoningWireProfile.CATALOG_STANDARD.value
        )
        if not isinstance(raw_profile, str):
            raise ValueError("model connection reasoning profile must be text")
        profile = ReasoningWireProfile(raw_profile)
        declared = None
    else:
        declared, profile = _user_declared_model_target_from_dict(raw_declared)
    return ModelConnectionConfig(
        id=ModelConnectionId(value["id"]),
        target=ModelTargetKey(
            route_id=value["route_id"],
            wire_api=WireApi(value["wire_api"]),
            model_id=value["model_id"],
        ),
        base_url=value["base_url"],
        reasoning_wire_profile=profile,
        user_declared=declared,
    )


def _user_declared_model_target_to_dict(
    value: UserDeclaredModelTarget,
    profile: ReasoningWireProfile,
) -> dict[str, object]:
    return {
        "configuration_name": value.configuration_name,
        "total_context_tokens": value.total_context_tokens,
        "max_output_tokens": value.max_output_tokens,
        "tool_call": value.tool_call,
        "reasoning": _user_declared_reasoning_to_dict(
            profile, value.reasoning
        ),
        "authentication": value.authentication.value,
        **(
            {"input_modalities": list(value.input_modalities)}
            if value.input_modalities is not None
            else {}
        ),
    }


def _user_declared_model_target_from_dict(
    value: object,
) -> tuple[UserDeclaredModelTarget, ReasoningWireProfile]:
    expected = {
        "configuration_name",
        "total_context_tokens",
        "max_output_tokens",
        "tool_call",
        "reasoning",
        "authentication",
    }
    if not isinstance(value, dict) or set(value) - {"input_modalities"} != expected:
        raise ValueError("custom model target has an invalid closed shape")
    name = value["configuration_name"]
    total = value["total_context_tokens"]
    output = value["max_output_tokens"]
    tool_call = value["tool_call"]
    authentication = value["authentication"]
    input_modalities = value.get("input_modalities")
    if (
        not isinstance(name, str)
        or isinstance(total, bool)
        or not isinstance(total, int)
        or isinstance(output, bool)
        or not isinstance(output, int)
        or not isinstance(tool_call, bool)
        or not isinstance(authentication, str)
        or (input_modalities is not None and not isinstance(input_modalities, list))
    ):
        raise ValueError("custom model target fields are invalid")
    reasoning_profile, reasoning = user_declared_reasoning_from_dict(
        value["reasoning"]
    )
    return (
        UserDeclaredModelTarget(
            configuration_name=name,
            total_context_tokens=total,
            max_output_tokens=output,
            tool_call=tool_call,
            reasoning=reasoning,
            authentication=ModelConnectionAuthentication(authentication),
            input_modalities=(
                None if input_modalities is None else tuple(input_modalities)
            ),
        ),
        reasoning_profile,
    )


def _user_declared_reasoning_to_dict(
    profile: ReasoningWireProfile,
    value: ReasoningControlContract,
) -> dict[str, object]:
    if isinstance(value, ReasoningProviderDefault):
        if profile is not ReasoningWireProfile.PROVIDER_DEFAULT:
            raise ValueError("custom model reasoning profile is inconsistent")
        return {"kind": profile.value}
    if isinstance(value, ReasoningSelectableControls):
        if value.toggle is not None:
            return {"kind": profile.value}
        if value.effort is not None:
            return {"kind": profile.value, "values": list(value.effort.values)}
    raise ValueError("custom model reasoning contract is invalid")


def user_declared_reasoning_from_dict(
    value: object,
) -> tuple[ReasoningWireProfile, ReasoningControlContract]:
    if value == {"kind": "provider_default"}:
        return ReasoningWireProfile.PROVIDER_DEFAULT, ReasoningProviderDefault()
    toggle_profiles = {
        ReasoningWireProfile.TOGGLE,
        ReasoningWireProfile.ENABLE_THINKING,
        ReasoningWireProfile.THINKING_TYPE,
    }
    if isinstance(value, dict) and set(value) == {"kind"}:
        try:
            profile = ReasoningWireProfile(value["kind"])
        except (TypeError, ValueError) as exc:
            raise ValueError("custom model reasoning kind is invalid") from exc
        if profile in toggle_profiles:
            return profile, ReasoningSelectableControls(toggle=ReasoningToggle())
        raise ValueError("custom model reasoning kind is invalid")
    if isinstance(value, dict) and set(value) == {"kind", "values"}:
        try:
            profile = ReasoningWireProfile(value["kind"])
        except (TypeError, ValueError) as exc:
            raise ValueError("custom model reasoning kind is invalid") from exc
        if profile not in {
            ReasoningWireProfile.EFFORT,
            ReasoningWireProfile.THINKING_EFFORT,
            ReasoningWireProfile.BROAD_COMPAT,
        }:
            raise ValueError("custom model reasoning kind is invalid")
        values = value["values"]
        if not isinstance(values, list) or not all(
            isinstance(item, str) for item in values
        ):
            raise ValueError("custom model reasoning efforts are invalid")
        return (
            profile,
            ReasoningSelectableControls(
                effort=ReasoningEffortChoices(tuple(values))
            ),
        )
    raise ValueError("custom model reasoning has an invalid closed shape")


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
    "ModelConnectionAuthentication",
    "ModelConnectionConfig",
    "ModelConnectionId",
    "ReasoningBudgetSelection",
    "ReasoningEffortSelection",
    "ReasoningSelection",
    "ReasoningToggleSelection",
    "ReasoningWireProfile",
    "USER_DECLARED_MODEL_ROUTE_ID",
    "UserDeclaredModelTarget",
    "freeze_binding_json",
    "model_call_binding_from_dict",
    "model_call_binding_to_dict",
    "model_connection_from_dict",
    "model_connection_to_dict",
    "reasoning_selection_from_dict",
    "reasoning_selection_to_dict",
    "user_declared_reasoning_from_dict",
]
