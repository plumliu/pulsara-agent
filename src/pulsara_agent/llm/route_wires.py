"""The small, explicit registry of production route/wire adapter contracts."""

from __future__ import annotations

from pulsara_agent.llm.model_catalog import (
    ReasoningControlContract,
    ReasoningFixedOn,
    RouteWireDialect,
    WireApi,
)
from pulsara_agent.llm.model_connections import (
    ReasoningEffortSelection,
    ReasoningSelection,
    ReasoningToggleSelection,
    ReasoningWireProfile,
)
from pulsara_agent.llm.model_target import (
    ReasoningWireContract,
    ReasoningWireFields,
    RouteWireContract,
    RouteWireRegistry,
)
from pulsara_agent.llm.provider import (
    ModelIdentityPolicy,
    RouteWireProfile,
)
from pulsara_agent.llm.provider_replay import (
    ProviderAssistantReplayCodecKind,
    provider_replay_contract_fingerprint,
)


_CHAT_REPLAY = provider_replay_contract_fingerprint(
    ProviderAssistantReplayCodecKind.CHAT_CLOSED_REASONING_FIELDS
)
_RESPONSES_REPLAY = provider_replay_contract_fingerprint(
    ProviderAssistantReplayCodecKind.RESPONSES_EXACT_OUTPUT_ITEMS
)


def _chat_reasoning(
    selection: ReasoningSelection | None, _controls: ReasoningControlContract
) -> ReasoningWireFields:
    if isinstance(selection, ReasoningEffortSelection):
        return ReasoningWireFields({"reasoning_effort": selection.value}, {})
    if isinstance(selection, ReasoningToggleSelection):
        # ``reasoning`` is an OpenAI-compatible extension rather than a
        # keyword accepted by the OpenAI Python SDK's Chat create method.
        # ``extra_body`` is only the SDK carrier: the SDK merges it into the
        # root HTTP JSON object before sending the request.
        return ReasoningWireFields({}, {"reasoning": {"enabled": selection.enabled}})
    raise ValueError("Chat Completions only accepts catalog effort or toggle controls")


def _responses_reasoning(
    selection: ReasoningSelection | None, _controls: ReasoningControlContract
) -> ReasoningWireFields:
    if selection is None and isinstance(_controls, ReasoningFixedOn):
        effort = "high"
    elif isinstance(selection, ReasoningToggleSelection):
        effort = "high" if selection.enabled else "none"
    elif isinstance(selection, ReasoningEffortSelection):
        effort = selection.value
    else:
        raise ValueError("Responses only accepts effort or toggle controls")
    return ReasoningWireFields(
        {
            "reasoning": {
                "effort": effort,
                "summary": "auto",
            }
        },
        {},
    )


def _provider_default_reasoning(
    _selection: ReasoningSelection | None, _controls: ReasoningControlContract
) -> ReasoningWireFields:
    raise ValueError("provider-default reasoning has no caller selection")


def _chat_effort_reasoning(
    selection: ReasoningSelection | None, controls: ReasoningControlContract
) -> ReasoningWireFields:
    if not isinstance(selection, ReasoningEffortSelection):
        raise ValueError("effort profile requires an effort selection")
    return _chat_reasoning(selection, controls)


def _toggle_enabled(
    selection: ReasoningSelection | None,
    controls: ReasoningControlContract,
    *,
    profile_name: str,
) -> bool:
    if isinstance(selection, ReasoningToggleSelection):
        return selection.enabled
    if selection is None and isinstance(controls, ReasoningFixedOn):
        return True
    raise ValueError(f"{profile_name} profile requires a toggle or fixed-on model")


def _chat_toggle_reasoning(
    selection: ReasoningSelection | None, controls: ReasoningControlContract
) -> ReasoningWireFields:
    return ReasoningWireFields(
        {},
        {
            "reasoning": {
                "enabled": _toggle_enabled(
                    selection,
                    controls,
                    profile_name="toggle",
                )
            }
        },
    )


def _chat_enable_thinking_reasoning(
    selection: ReasoningSelection | None, controls: ReasoningControlContract
) -> ReasoningWireFields:
    return ReasoningWireFields(
        {},
        {
            "enable_thinking": _toggle_enabled(
                selection,
                controls,
                profile_name="enable_thinking",
            )
        },
    )


def _chat_thinking_type_reasoning(
    selection: ReasoningSelection | None, controls: ReasoningControlContract
) -> ReasoningWireFields:
    enabled = _toggle_enabled(
        selection,
        controls,
        profile_name="thinking.type",
    )
    return ReasoningWireFields(
        {},
        {"thinking": {"type": "enabled" if enabled else "disabled"}},
    )


def _normalized_reasoning_effort(value: str | None) -> str | None:
    if value in {"none", "disabled"}:
        return "none"
    if value == "enabled":
        return "high"
    return value


def _chat_thinking_effort_reasoning(
    selection: ReasoningSelection | None, _controls: ReasoningControlContract
) -> ReasoningWireFields:
    if not isinstance(selection, ReasoningEffortSelection):
        raise ValueError("thinking+effort profile requires an effort selection")
    normalized = _normalized_reasoning_effort(selection.value)
    if normalized == "none":
        return ReasoningWireFields({}, {"thinking": {"type": "disabled"}})
    return ReasoningWireFields(
        {"reasoning_effort": normalized},
        {"thinking": {"type": "enabled"}},
    )


def _chat_broad_compat_reasoning(
    selection: ReasoningSelection | None, _controls: ReasoningControlContract
) -> ReasoningWireFields:
    if not isinstance(selection, ReasoningEffortSelection):
        raise ValueError("broad compatibility profile requires an effort selection")
    normalized = _normalized_reasoning_effort(selection.value)
    enabled = normalized != "none"
    return ReasoningWireFields(
        {"reasoning_effort": normalized},
        {
            "thinking": {"type": "enabled" if enabled else "disabled"},
            "enable_thinking": enabled,
            "reasoning": {"effort": normalized},
        },
    )


def _reasoning_profile(
    profile: ReasoningWireProfile,
    families: frozenset[str],
    lowerer,
    *,
    supports_fixed_on: bool = False,
) -> ReasoningWireContract:
    return ReasoningWireContract(
        profile=profile,
        supported_reasoning_families=families,
        lower_reasoning=lowerer,
        supports_fixed_on=supports_fixed_on,
    )


def _chat_reasoning_profiles() -> dict[ReasoningWireProfile, ReasoningWireContract]:
    toggle = frozenset({"toggle"})
    effort = frozenset({"effort"})
    return {
        ReasoningWireProfile.PROVIDER_DEFAULT: _reasoning_profile(
            ReasoningWireProfile.PROVIDER_DEFAULT, frozenset(), _provider_default_reasoning
        ),
        ReasoningWireProfile.EFFORT: _reasoning_profile(
            ReasoningWireProfile.EFFORT, effort, _chat_effort_reasoning
        ),
        ReasoningWireProfile.TOGGLE: _reasoning_profile(
            ReasoningWireProfile.TOGGLE,
            toggle,
            _chat_toggle_reasoning,
            supports_fixed_on=True,
        ),
        ReasoningWireProfile.ENABLE_THINKING: _reasoning_profile(
            ReasoningWireProfile.ENABLE_THINKING,
            toggle,
            _chat_enable_thinking_reasoning,
            supports_fixed_on=True,
        ),
        ReasoningWireProfile.THINKING_TYPE: _reasoning_profile(
            ReasoningWireProfile.THINKING_TYPE,
            toggle,
            _chat_thinking_type_reasoning,
            supports_fixed_on=True,
        ),
        ReasoningWireProfile.THINKING_EFFORT: _reasoning_profile(
            ReasoningWireProfile.THINKING_EFFORT,
            effort,
            _chat_thinking_effort_reasoning,
        ),
        ReasoningWireProfile.BROAD_COMPAT: _reasoning_profile(
            ReasoningWireProfile.BROAD_COMPAT, effort, _chat_broad_compat_reasoning
        ),
        ReasoningWireProfile.CATALOG_STANDARD: _reasoning_profile(
            ReasoningWireProfile.CATALOG_STANDARD,
            frozenset({"effort", "toggle"}),
            _chat_reasoning,
        ),
    }


def _responses_reasoning_profiles() -> dict[ReasoningWireProfile, ReasoningWireContract]:
    return {
        ReasoningWireProfile.PROVIDER_DEFAULT: _reasoning_profile(
            ReasoningWireProfile.PROVIDER_DEFAULT, frozenset(), _provider_default_reasoning
        ),
        ReasoningWireProfile.EFFORT: _reasoning_profile(
            ReasoningWireProfile.EFFORT,
            frozenset({"effort"}),
            _responses_reasoning,
        ),
        ReasoningWireProfile.TOGGLE: _reasoning_profile(
            ReasoningWireProfile.TOGGLE,
            frozenset({"toggle"}),
            _responses_reasoning,
            supports_fixed_on=True,
        ),
        ReasoningWireProfile.CATALOG_STANDARD: _reasoning_profile(
            ReasoningWireProfile.CATALOG_STANDARD,
            frozenset({"effort", "toggle"}),
            _responses_reasoning,
        ),
    }


def _contract(
    *,
    wire_api: WireApi,
    model_identity_policy: ModelIdentityPolicy,
    reasoning_profiles: dict[ReasoningWireProfile, ReasoningWireContract],
) -> RouteWireContract:
    profile = RouteWireProfile(
        id=f"route-wire:{wire_api.value}",
        wire_api=wire_api.value,
        model_identity_policy=model_identity_policy,
    )
    return RouteWireContract(
        transport_binding_id=(
            "pulsara.openai.chat_completions"
            if wire_api is WireApi.OPENAI_CHAT_COMPLETIONS
            else "pulsara.openai.responses"
        ),
        transport_contract_version=(
            "v8-connection-reasoning-profiles-and-tool-correlation"
            if wire_api is WireApi.OPENAI_CHAT_COMPLETIONS
            else "v8-connection-reasoning-profiles"
        ),
        model_identity_policy=model_identity_policy,
        assistant_replay_contract=(
            _CHAT_REPLAY
            if wire_api is WireApi.OPENAI_CHAT_COMPLETIONS
            else _RESPONSES_REPLAY
        ),
        profile=profile,
        reasoning_profiles=reasoning_profiles,
        default_reasoning_profile=ReasoningWireProfile.CATALOG_STANDARD,
    )


def production_route_wire_registry() -> RouteWireRegistry:
    registry = RouteWireRegistry()
    registry.register_endpoint_default("openai", "https://api.openai.com/v1")
    registry.register_dialect(
        RouteWireDialect.OPENAI_COMPATIBLE,
        WireApi.OPENAI_CHAT_COMPLETIONS,
        _contract(
            wire_api=WireApi.OPENAI_CHAT_COMPLETIONS,
            model_identity_policy=ModelIdentityPolicy.ACCEPT_REPORTED,
            reasoning_profiles=_chat_reasoning_profiles(),
        ),
    )
    registry.register_dialect(
        RouteWireDialect.OPENAI_COMPATIBLE,
        WireApi.OPENAI_RESPONSES,
        _contract(
            wire_api=WireApi.OPENAI_RESPONSES,
            model_identity_policy=ModelIdentityPolicy.ACCEPT_REPORTED,
            reasoning_profiles=_responses_reasoning_profiles(),
        ),
    )
    return registry


__all__ = ["production_route_wire_registry"]
