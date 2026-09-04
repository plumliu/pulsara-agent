"""The small, explicit registry of production route/wire adapter contracts."""

from __future__ import annotations

from pulsara_agent.llm.model_catalog import (
    ReasoningControlContract,
    RouteWireDialect,
    WireApi,
)
from pulsara_agent.llm.model_connections import (
    ReasoningEffortSelection,
    ReasoningSelection,
)
from pulsara_agent.llm.model_target import (
    ReasoningWireFields,
    RouteWireContract,
    RouteWireRegistry,
)
from pulsara_agent.llm.provider import (
    ModelIdentityPolicy,
    RouteWireProfile,
    ThinkingProfile,
    ThinkingReplayPolicy,
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
    selection: ReasoningSelection, _controls: ReasoningControlContract
) -> ReasoningWireFields:
    if not isinstance(selection, ReasoningEffortSelection):
        raise ValueError("Chat Completions only accepts catalog effort controls")
    return ReasoningWireFields({"reasoning_effort": selection.value}, {})


def _responses_reasoning(
    selection: ReasoningSelection, _controls: ReasoningControlContract
) -> ReasoningWireFields:
    if not isinstance(selection, ReasoningEffortSelection):
        raise ValueError("Responses only accepts catalog effort controls")
    return ReasoningWireFields({"reasoning": {"effort": selection.value}}, {})


def _contract(
    *,
    wire_api: WireApi,
    model_identity_policy: ModelIdentityPolicy,
    families: frozenset[str],
    lowerer,
) -> RouteWireContract:
    profile = RouteWireProfile(
        id=f"route-wire:{wire_api.value}",
        wire_api=wire_api.value,
        model_identity_policy=model_identity_policy,
        thinking=ThinkingProfile(
            replay_policy=(
                ThinkingReplayPolicy.WHEN_TOOL_CALLS
                if wire_api is WireApi.OPENAI_CHAT_COMPLETIONS
                else ThinkingReplayPolicy.NEVER
            )
        ),
    )
    return RouteWireContract(
        transport_binding_id=(
            "pulsara.openai.chat_completions"
            if wire_api is WireApi.OPENAI_CHAT_COMPLETIONS
            else "pulsara.openai.responses"
        ),
        transport_contract_version=(
            "v6-route-target-reasoning-and-tool-correlation"
            if wire_api is WireApi.OPENAI_CHAT_COMPLETIONS
            else "v6-route-target-reasoning"
        ),
        model_identity_policy=model_identity_policy,
        assistant_replay_contract=(
            _CHAT_REPLAY
            if wire_api is WireApi.OPENAI_CHAT_COMPLETIONS
            else _RESPONSES_REPLAY
        ),
        profile=profile,
        supported_reasoning_families=frozenset(families),
        lower_reasoning=lowerer,
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
            families=frozenset({"effort"}),
            lowerer=_chat_reasoning,
        ),
    )
    registry.register_dialect(
        RouteWireDialect.OPENAI_COMPATIBLE,
        WireApi.OPENAI_RESPONSES,
        _contract(
            wire_api=WireApi.OPENAI_RESPONSES,
            model_identity_policy=ModelIdentityPolicy.ACCEPT_REPORTED,
            families=frozenset({"effort"}),
            lowerer=_responses_reasoning,
        ),
    )
    return registry


__all__ = ["production_route_wire_registry"]
