"""Exact route + wire API + model target resolution."""

from __future__ import annotations

import math
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from types import MappingProxyType
from typing import Literal
from urllib.parse import unquote, urlsplit

from pulsara_agent.llm.model_catalog import (
    ModelCatalogEntry,
    ModelCatalogEntryKey,
    ModelHardLimits,
    ModelTargetKey,
    ReasoningControlContract,
    ReasoningFixedOn,
    ReasoningProviderDefault,
    ReasoningSelectableControls,
    ReasoningUnavailable,
    RouteWireDialect,
    SelectableModelCatalog,
    WireApi,
)
from pulsara_agent.llm.model_connections import (
    ModelCallBinding,
    ModelConnectionConfig,
    ModelConnectionId,
    ReasoningBudgetSelection,
    ReasoningEffortSelection,
    ReasoningSelection,
    ReasoningToggleSelection,
    ReasoningWireProfile,
    USER_DECLARED_MODEL_ROUTE_ID,
    UserDeclaredModelTarget,
)
from pulsara_agent.llm.provider import ModelIdentityPolicy
from pulsara_agent.llm.provider import RouteWireProfile
from pulsara_agent.primitives.model_call import ModelContextLimits


DEFAULT_OUTPUT_TOKEN_TARGET = 8_192
INPUT_SAFETY_MARGIN_TARGET = 8_192

_PERCENT_ESCAPE_RE = re.compile(r"%([0-9a-fA-F]{2})")
_INVALID_PERCENT_ESCAPE_RE = re.compile(r"%(?![0-9a-fA-F]{2})")


@dataclass(frozen=True, slots=True)
class ReasoningWireFields:
    root: Mapping[str, object]
    extra_body: Mapping[str, object]

    def __post_init__(self) -> None:
        root = MappingProxyType(dict(self.root))
        extra_body = MappingProxyType(dict(self.extra_body))
        if set(root).intersection(extra_body):
            raise ValueError("reasoning wire fields have duplicate owners")
        object.__setattr__(self, "root", root)
        object.__setattr__(self, "extra_body", extra_body)


ReasoningLowerer = Callable[
    [ReasoningSelection | None, ReasoningControlContract], ReasoningWireFields
]


@dataclass(frozen=True, slots=True)
class ReasoningWireContract:
    profile: ReasoningWireProfile
    supported_reasoning_families: frozenset[
        Literal["effort", "toggle", "budget_tokens"]
    ]
    lower_reasoning: ReasoningLowerer
    supports_fixed_on: bool = False


@dataclass(frozen=True, slots=True)
class RouteWireContract:
    transport_binding_id: str
    transport_contract_version: str
    model_identity_policy: ModelIdentityPolicy
    assistant_replay_contract: str
    profile: RouteWireProfile
    reasoning_profiles: Mapping[ReasoningWireProfile, ReasoningWireContract]
    default_reasoning_profile: ReasoningWireProfile

    def __post_init__(self) -> None:
        if not self.transport_binding_id or not self.transport_contract_version:
            raise ValueError("route/wire transport identity is incomplete")
        if not self.assistant_replay_contract:
            raise ValueError("route/wire replay contract is incomplete")
        if self.profile.model_identity_policy is not self.model_identity_policy:
            raise ValueError("route/wire model identity policy drifted")
        profiles = MappingProxyType(dict(self.reasoning_profiles))
        if self.default_reasoning_profile not in profiles:
            raise ValueError("route/wire default reasoning profile is unavailable")
        if any(key is not contract.profile for key, contract in profiles.items()):
            raise ValueError("route/wire reasoning profile registry drifted")
        object.__setattr__(self, "reasoning_profiles", profiles)

    def reasoning_contract(
        self, profile: ReasoningWireProfile
    ) -> ReasoningWireContract:
        try:
            return self.reasoning_profiles[profile]
        except KeyError as exc:
            raise KeyError(
                f"reasoning profile {profile.value!r} is unavailable for "
                f"{self.profile.wire_api}"
            ) from exc


@dataclass(slots=True)
class RouteWireRegistry:
    _dialect_contracts: dict[tuple[RouteWireDialect, WireApi], RouteWireContract]
    _route_endpoint_defaults: dict[str, str]

    def __init__(self) -> None:
        self._dialect_contracts = {}
        self._route_endpoint_defaults = {}

    def register_endpoint_default(self, route_id: str, base_url: str) -> None:
        if not route_id or route_id != route_id.strip():
            raise ValueError("route endpoint default has an invalid route id")
        if route_id in self._route_endpoint_defaults:
            raise ValueError("route endpoint default is already registered")
        self._route_endpoint_defaults[route_id] = canonicalize_endpoint(base_url)

    def register_dialect(
        self,
        dialect: RouteWireDialect,
        wire_api: WireApi,
        contract: RouteWireContract,
    ) -> None:
        key = (dialect, wire_api)
        if key in self._dialect_contracts:
            raise ValueError("dialect/wire contract is already registered")
        self._dialect_contracts[key] = contract

    def contract_for(
        self, entry: ModelCatalogEntry, wire_api: WireApi
    ) -> RouteWireContract:
        try:
            return self._dialect_contracts[(entry.wire_dialect, wire_api)]
        except KeyError as exc:
            raise KeyError(
                "route/wire adapter is unavailable: "
                f"{entry.key.route_id}/{wire_api.value}"
            ) from exc

    def supports(self, entry: ModelCatalogEntry, wire_api: WireApi) -> bool:
        return (entry.wire_dialect, wire_api) in self._dialect_contracts

    def endpoint_for(self, entry: ModelCatalogEntry) -> str | None:
        return entry.endpoint or self._route_endpoint_defaults.get(entry.key.route_id)


@dataclass(frozen=True, slots=True)
class ModelTargetFacts:
    display_name: str
    route_name: str
    tool_call: bool | None
    limits: ModelContextLimits
    wire_shape_hint: Literal["responses", "completions"] | None
    input_modalities: tuple[str, ...] | None


@dataclass(frozen=True, slots=True)
class ModelTargetContract:
    key: ModelTargetKey
    target_facts: ModelTargetFacts
    reasoning: ReasoningControlContract
    reasoning_wire: ReasoningWireContract
    route_wire: RouteWireContract
    canonical_endpoint_base_url: str


@dataclass(frozen=True, slots=True)
class ResolvedModelConnection:
    config: ModelConnectionConfig
    target: ModelTargetContract


@dataclass(frozen=True, slots=True)
class FrozenModelResolutionSnapshot:
    """Database-independent connection/target cut for one admission attempt."""

    resolved: Mapping[ModelConnectionId, ResolvedModelConnection]
    unavailable: Mapping[ModelConnectionId, str]

    def __post_init__(self) -> None:
        resolved = MappingProxyType(dict(self.resolved))
        unavailable = MappingProxyType(dict(self.unavailable))
        if set(resolved).intersection(unavailable):
            raise ValueError("model resolution snapshot has overlapping outcomes")
        object.__setattr__(self, "resolved", resolved)
        object.__setattr__(self, "unavailable", unavailable)

    def connection(self, connection_id: ModelConnectionId) -> ResolvedModelConnection:
        resolved = self.resolved.get(connection_id)
        if resolved is not None:
            return resolved
        unavailable = self.unavailable.get(connection_id)
        if unavailable is not None:
            raise ModelTargetNotExecutable(unavailable)
        raise ModelTargetNotExecutable("model connection metadata is unavailable")

    def validate(self, binding: ModelCallBinding) -> ResolvedModelConnection:
        resolved = self.connection(binding.connection_id)
        validate_reasoning_selection(resolved.target.reasoning, binding.reasoning)
        return resolved

    def reconcile(
        self, binding: ModelCallBinding
    ) -> tuple[ModelCallBinding, ResolvedModelConnection, bool]:
        resolved = self.connection(binding.connection_id)
        reconciled, changed = reconcile_model_call_binding(
            current=binding,
            connection=resolved.config,
            target=resolved.target,
        )
        return reconciled, resolved, changed


class ModelTargetNotExecutable(ValueError):
    pass


class ModelReasoningSelectionInvalid(ValueError):
    pass


def derive_model_context_limits(entry: ModelCatalogEntry) -> ModelContextLimits:
    hard = entry.limits
    if hard is None:
        raise ModelTargetNotExecutable("model hard limits are unavailable")
    default_output = min(
        DEFAULT_OUTPUT_TOKEN_TARGET,
        hard.max_output_tokens,
        hard.total_context_tokens - 1,
    )
    pre_margin_input = min(
        hard.max_input_tokens,
        hard.total_context_tokens - default_output,
    )
    margin = min(INPUT_SAFETY_MARGIN_TARGET, pre_margin_input - 1)
    if min(default_output, pre_margin_input, pre_margin_input - margin) < 1:
        raise ModelTargetNotExecutable("model context budget is non-positive")
    return ModelContextLimits(
        total_context_tokens=hard.total_context_tokens,
        max_input_tokens=hard.max_input_tokens,
        max_output_tokens=hard.max_output_tokens,
        default_output_tokens=default_output,
        input_safety_margin_tokens=margin,
    )


def controls_supported_by_adapter(
    reasoning: ReasoningControlContract,
    contract: ReasoningWireContract,
) -> ReasoningControlContract:
    if not isinstance(reasoning, ReasoningSelectableControls):
        return reasoning
    effort = (
        reasoning.effort
        if "effort" in contract.supported_reasoning_families
        else None
    )
    toggle = (
        reasoning.toggle
        if "toggle" in contract.supported_reasoning_families
        else None
    )
    budget = (
        reasoning.budget
        if "budget_tokens" in contract.supported_reasoning_families
        else None
    )
    if effort is None and toggle is None and budget is None:
        return ReasoningProviderDefault()
    return ReasoningSelectableControls(effort, toggle, budget)


def reasoning_profile_compatible(
    reasoning: ReasoningControlContract,
    contract: ReasoningWireContract,
) -> bool:
    """Whether one catalog reasoning contract can use this wire shape."""

    if contract.profile in {
        ReasoningWireProfile.CATALOG_STANDARD,
        ReasoningWireProfile.PROVIDER_DEFAULT,
    }:
        return True
    if isinstance(reasoning, ReasoningFixedOn):
        return contract.supports_fixed_on
    if isinstance(reasoning, (ReasoningUnavailable, ReasoningProviderDefault)):
        return False
    if not isinstance(reasoning, ReasoningSelectableControls):
        return False
    return any(
        (
            family == "effort"
            and reasoning.effort is not None
        )
        or (
            family == "toggle"
            and reasoning.toggle is not None
        )
        or (
            family == "budget_tokens"
            and reasoning.budget is not None
        )
        for family in contract.supported_reasoning_families
    )


def compatible_reasoning_profiles(
    reasoning: ReasoningControlContract,
    route_wire: RouteWireContract,
) -> tuple[ReasoningWireProfile, ...]:
    """Return compatible profiles in the route/wire registry's stable order."""

    return tuple(
        profile
        for profile, contract in route_wire.reasoning_profiles.items()
        if reasoning_profile_compatible(reasoning, contract)
    )


def resolve_model_target_contract(
    *,
    catalog: SelectableModelCatalog | None,
    connection: ModelConnectionConfig,
    route_wires: RouteWireRegistry,
) -> ModelTargetContract:
    entry = _entry_for_connection(catalog=catalog, connection=connection)
    try:
        route_wire = route_wires.contract_for(entry, connection.target.wire_api)
    except KeyError as exc:
        raise ModelTargetNotExecutable(str(exc)) from exc
    limits = derive_model_context_limits(entry)
    canonical_endpoint = canonicalize_endpoint(connection.base_url)
    requested_profile = connection.reasoning_wire_profile
    try:
        reasoning_wire = route_wire.reasoning_contract(requested_profile)
    except KeyError as exc:
        raise ModelTargetNotExecutable(str(exc)) from exc
    if not reasoning_profile_compatible(entry.reasoning, reasoning_wire):
        raise ModelTargetNotExecutable(
            "selected reasoning request profile is incompatible with this model"
        )
    reasoning = controls_supported_by_adapter(entry.reasoning, reasoning_wire)
    if connection.user_declared is not None and reasoning != entry.reasoning:
        raise ModelTargetNotExecutable(
            "custom reasoning control is unsupported by the selected wire API"
        )
    return ModelTargetContract(
        key=connection.target,
        target_facts=ModelTargetFacts(
            display_name=entry.display_name,
            route_name=entry.route_name,
            tool_call=entry.tool_call,
            limits=limits,
            wire_shape_hint=entry.wire_shape_hint,
            input_modalities=entry.input_modalities,
        ),
        reasoning=reasoning,
        reasoning_wire=reasoning_wire,
        route_wire=route_wire,
        canonical_endpoint_base_url=canonical_endpoint,
    )


def _entry_for_connection(
    *,
    catalog: SelectableModelCatalog | None,
    connection: ModelConnectionConfig,
) -> ModelCatalogEntry:
    declared = connection.user_declared
    if declared is not None:
        hard_limits = _user_declared_hard_limits(declared)
        return ModelCatalogEntry(
            key=connection.target.catalog_key,
            route_name=declared.configuration_name,
            display_name=connection.target.model_id,
            endpoint=connection.base_url,
            wire_dialect=RouteWireDialect.OPENAI_COMPATIBLE,
            total_context_tokens=hard_limits.total_context_tokens,
            limits=hard_limits,
            reasoning=declared.reasoning,
            tool_call=declared.tool_call,
            wire_shape_hint=None,
            input_modalities=declared.input_modalities,
        )
    if catalog is None:
        raise ModelTargetNotExecutable("model catalog snapshot is unavailable")
    try:
        return catalog.require(connection.target.catalog_key)
    except KeyError as exc:
        raise ModelTargetNotExecutable(str(exc)) from exc


def _user_declared_hard_limits(value: UserDeclaredModelTarget) -> ModelHardLimits:
    return ModelHardLimits(
        value.total_context_tokens,
        value.total_context_tokens,
        value.max_output_tokens,
    )


def create_model_connection(
    *,
    catalog: SelectableModelCatalog,
    target: ModelTargetKey,
    route_wires: RouteWireRegistry,
    reasoning_wire_profile: ReasoningWireProfile = ReasoningWireProfile.CATALOG_STANDARD,
    connection_id: ModelConnectionId | None = None,
) -> ResolvedModelConnection:
    try:
        entry = catalog.require(ModelCatalogEntryKey(target.route_id, target.model_id))
    except KeyError as exc:
        raise ModelTargetNotExecutable(str(exc)) from exc
    endpoint = route_wires.endpoint_for(entry)
    if endpoint is None:
        raise ModelTargetNotExecutable("model endpoint is unknown")
    config = ModelConnectionConfig(
        connection_id or ModelConnectionId.new(),
        target,
        endpoint,
        reasoning_wire_profile,
    )
    return ResolvedModelConnection(
        config=config,
        target=resolve_model_target_contract(
            catalog=catalog, connection=config, route_wires=route_wires
        ),
    )


def create_user_declared_model_connection(
    *,
    model_id: str,
    wire_api: WireApi,
    base_url: str,
    declaration: UserDeclaredModelTarget,
    route_wires: RouteWireRegistry,
    reasoning_wire_profile: ReasoningWireProfile,
    connection_id: ModelConnectionId | None = None,
) -> ResolvedModelConnection:
    resolved_id = connection_id or ModelConnectionId.new()
    config = ModelConnectionConfig(
        id=resolved_id,
        target=ModelTargetKey(USER_DECLARED_MODEL_ROUTE_ID, wire_api, model_id),
        base_url=canonicalize_endpoint(base_url),
        reasoning_wire_profile=reasoning_wire_profile,
        user_declared=declaration,
    )
    return ResolvedModelConnection(
        config=config,
        target=resolve_model_target_contract(
            catalog=None,
            connection=config,
            route_wires=route_wires,
        ),
    )


def default_reasoning_selection(
    reasoning: ReasoningControlContract,
) -> ReasoningSelection | None:
    if not isinstance(reasoning, ReasoningSelectableControls):
        return None
    disabled: list[str | None] = []
    positive: list[str] = []
    if reasoning.effort is not None:
        for value in reasoning.effort.values:
            if value is None or value == "none":
                disabled.append(value)
            else:
                positive.append(value)
    if positive:
        return ReasoningEffortSelection(positive[len(positive) // 2])
    if reasoning.budget is not None and reasoning.budget.closed:
        assert reasoning.budget.minimum_tokens is not None
        assert reasoning.budget.maximum_tokens is not None
        return ReasoningBudgetSelection(
            math.ceil(
                (reasoning.budget.minimum_tokens + reasoning.budget.maximum_tokens)
                / 2
            )
        )
    if reasoning.toggle is not None:
        return ReasoningToggleSelection(True)
    if disabled:
        return ReasoningEffortSelection(disabled[len(disabled) // 2])
    return None


def validate_reasoning_selection(
    reasoning: ReasoningControlContract,
    selection: ReasoningSelection | None,
) -> None:
    if selection is None:
        if isinstance(reasoning, ReasoningSelectableControls) and (
            reasoning.effort is not None
            or reasoning.toggle is not None
            or (reasoning.budget is not None and reasoning.budget.closed)
        ):
            raise ModelReasoningSelectionInvalid(
                "selectable target requires an explicit reasoning selection"
            )
        return
    if not isinstance(reasoning, ReasoningSelectableControls):
        raise ModelReasoningSelectionInvalid("target has no caller reasoning control")
    if isinstance(selection, ReasoningEffortSelection):
        if reasoning.effort is None or selection.value not in reasoning.effort.values:
            raise ModelReasoningSelectionInvalid(
                "reasoning effort is not an exact target choice"
            )
        return
    if isinstance(selection, ReasoningToggleSelection):
        if reasoning.toggle is None:
            raise ModelReasoningSelectionInvalid("target has no reasoning toggle")
        return
    if isinstance(selection, ReasoningBudgetSelection):
        budget = reasoning.budget
        if budget is None or not budget.closed:
            raise ModelReasoningSelectionInvalid(
                "target has no closed reasoning budget range"
            )
        assert budget.minimum_tokens is not None
        assert budget.maximum_tokens is not None
        if not budget.minimum_tokens <= selection.tokens <= budget.maximum_tokens:
            raise ModelReasoningSelectionInvalid(
                "reasoning token budget is outside the target range"
            )
        return
    raise TypeError(type(selection).__name__)


def reconcile_model_call_binding(
    *,
    current: ModelCallBinding | None,
    connection: ModelConnectionConfig,
    target: ModelTargetContract,
) -> tuple[ModelCallBinding, bool]:
    if current is None or current.connection_id != connection.id:
        return ModelCallBinding(
            connection.id, default_reasoning_selection(target.reasoning)
        ), True
    try:
        validate_reasoning_selection(target.reasoning, current.reasoning)
    except ModelReasoningSelectionInvalid:
        return ModelCallBinding(
            connection.id, default_reasoning_selection(target.reasoning)
        ), True
    return current, False


def with_output_cap(
    target: ModelTargetContract, maximum_output_tokens: int
) -> ModelTargetContract:
    if maximum_output_tokens < 1:
        raise ValueError("model output cap must be positive")
    limits = target.target_facts.limits
    cap = min(maximum_output_tokens, limits.max_output_tokens)
    pre_margin = min(
        limits.max_input_tokens, limits.total_context_tokens - cap
    )
    margin = min(limits.input_safety_margin_tokens, pre_margin - 1)
    bounded = ModelContextLimits(
        total_context_tokens=limits.total_context_tokens,
        max_input_tokens=limits.max_input_tokens,
        max_output_tokens=limits.max_output_tokens,
        default_output_tokens=cap,
        input_safety_margin_tokens=margin,
    )
    return replace(
        target,
        target_facts=replace(target.target_facts, limits=bounded),
    )


def canonicalize_endpoint(base_url: str) -> str:
    parsed = urlsplit(base_url)
    scheme = parsed.scheme.lower()
    if scheme not in {"http", "https"}:
        raise ValueError("model endpoint scheme must be http or https")
    if parsed.username is not None or parsed.password is not None:
        raise ValueError("model endpoint cannot contain userinfo")
    if parsed.query or parsed.fragment:
        raise ValueError("model endpoint cannot contain query or fragment")
    if not parsed.hostname:
        raise ValueError("model endpoint hostname is required")
    hostname = parsed.hostname.encode("idna").decode("ascii").lower()
    try:
        port = parsed.port
    except ValueError as exc:
        raise ValueError("model endpoint port is invalid") from exc
    if port == (80 if scheme == "http" else 443):
        port = None
    rendered_hostname = f"[{hostname}]" if ":" in hostname else hostname
    authority = rendered_hostname if port is None else f"{rendered_hostname}:{port}"
    path = parsed.path or "/"
    if _INVALID_PERCENT_ESCAPE_RE.search(path):
        raise ValueError("model endpoint path contains an invalid percent escape")
    path = _PERCENT_ESCAPE_RE.sub(lambda match: f"%{match.group(1).upper()}", path)
    if any(segment in {".", ".."} for segment in unquote(path).split("/")):
        raise ValueError("model endpoint path cannot contain dot segments")
    if not path.startswith("/"):
        path = f"/{path}"
    if path != "/":
        path = path.rstrip("/")
    return f"{scheme}://{authority}{'' if path == '/' else path}"


def reasoning_wire_fields(
    target: ModelTargetContract,
    selection: ReasoningSelection | None,
) -> ReasoningWireFields:
    validate_reasoning_selection(target.reasoning, selection)
    if selection is None:
        if (
            isinstance(target.reasoning, ReasoningFixedOn)
            and target.reasoning_wire.supports_fixed_on
        ):
            return target.reasoning_wire.lower_reasoning(None, target.reasoning)
        return ReasoningWireFields({}, {})
    return target.reasoning_wire.lower_reasoning(selection, target.reasoning)


__all__ = [
    "FrozenModelResolutionSnapshot",
    "DEFAULT_OUTPUT_TOKEN_TARGET",
    "INPUT_SAFETY_MARGIN_TARGET",
    "ModelTargetFacts",
    "ModelReasoningSelectionInvalid",
    "ModelTargetContract",
    "ModelTargetNotExecutable",
    "ReasoningWireFields",
    "ReasoningWireContract",
    "ResolvedModelConnection",
    "RouteWireContract",
    "RouteWireRegistry",
    "canonicalize_endpoint",
    "compatible_reasoning_profiles",
    "controls_supported_by_adapter",
    "create_model_connection",
    "create_user_declared_model_connection",
    "default_reasoning_selection",
    "derive_model_context_limits",
    "reasoning_wire_fields",
    "reasoning_profile_compatible",
    "reconcile_model_call_binding",
    "resolve_model_target_contract",
    "validate_reasoning_selection",
    "with_output_cap",
]
