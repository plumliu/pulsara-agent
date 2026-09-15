"""models.dev-backed model catalog and target-local reasoning contracts.

The catalog is an advisory, process-local projection.  It is deliberately not
persisted and it never performs provider requests on the model-dispatch path.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from types import MappingProxyType
from typing import Literal

import httpx


MODELS_DEV_CATALOG_URL = "https://models.dev/api.json"
MINIMUM_SELECTABLE_CONTEXT_TOKENS = 256_000


class WireApi(StrEnum):
    OPENAI_CHAT_COMPLETIONS = "openai_chat_completions"
    OPENAI_RESPONSES = "openai_responses"


class RouteWireDialect(StrEnum):
    """The transport family advertised by one models.dev provider entry."""

    OPENAI_COMPATIBLE = "openai_compatible"
    PROVIDER_NATIVE = "provider_native"
    UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True, order=True)
class ModelCatalogEntryKey:
    route_id: str
    model_id: str

    def __post_init__(self) -> None:
        if not self.route_id or self.route_id != self.route_id.strip():
            raise ValueError("route_id must be non-empty canonical text")
        if not self.model_id or self.model_id != self.model_id.strip():
            raise ValueError("model_id must be non-empty canonical text")


@dataclass(frozen=True, slots=True, order=True)
class ModelTargetKey:
    route_id: str
    wire_api: WireApi
    model_id: str

    def __post_init__(self) -> None:
        ModelCatalogEntryKey(self.route_id, self.model_id)
        if not isinstance(self.wire_api, WireApi):
            raise TypeError("wire_api must be a WireApi")

    @property
    def catalog_key(self) -> ModelCatalogEntryKey:
        return ModelCatalogEntryKey(self.route_id, self.model_id)


@dataclass(frozen=True, slots=True)
class ModelHardLimits:
    total_context_tokens: int
    max_input_tokens: int
    max_output_tokens: int

    def __post_init__(self) -> None:
        if min(
            self.total_context_tokens,
            self.max_input_tokens,
            self.max_output_tokens,
        ) < 1:
            raise ValueError("model hard limits must be positive")
        if self.max_input_tokens > self.total_context_tokens:
            raise ValueError("model input limit exceeds context limit")
        if self.max_output_tokens > self.total_context_tokens:
            raise ValueError("model output limit exceeds context limit")


@dataclass(frozen=True, slots=True)
class ReasoningEffortChoices:
    values: tuple[str | None, ...]

    def __post_init__(self) -> None:
        if not self.values or len(set(self.values)) != len(self.values):
            raise ValueError("reasoning effort choices must be non-empty and unique")
        if any(value is not None and (not value or value != value.strip()) for value in self.values):
            raise ValueError("reasoning effort choices contain invalid text")


@dataclass(frozen=True, slots=True)
class ReasoningToggle:
    pass


@dataclass(frozen=True, slots=True)
class ReasoningTokenBudgetRange:
    minimum_tokens: int | None
    maximum_tokens: int | None

    def __post_init__(self) -> None:
        if self.minimum_tokens is not None and self.minimum_tokens < 1:
            raise ValueError("reasoning budget minimum must be positive")
        if self.maximum_tokens is not None and self.maximum_tokens < 1:
            raise ValueError("reasoning budget maximum must be positive")
        if (
            self.minimum_tokens is not None
            and self.maximum_tokens is not None
            and self.minimum_tokens > self.maximum_tokens
        ):
            raise ValueError("reasoning budget range is inverted")

    @property
    def closed(self) -> bool:
        return self.minimum_tokens is not None and self.maximum_tokens is not None


@dataclass(frozen=True, slots=True)
class ReasoningSelectableControls:
    effort: ReasoningEffortChoices | None = None
    toggle: ReasoningToggle | None = None
    budget: ReasoningTokenBudgetRange | None = None

    def __post_init__(self) -> None:
        if self.effort is None and self.toggle is None and self.budget is None:
            raise ValueError("selectable reasoning controls cannot be empty")


@dataclass(frozen=True, slots=True)
class ReasoningFixedOn:
    pass


@dataclass(frozen=True, slots=True)
class ReasoningUnavailable:
    pass


@dataclass(frozen=True, slots=True)
class ReasoningProviderDefault:
    pass


ReasoningControlContract = (
    ReasoningSelectableControls
    | ReasoningFixedOn
    | ReasoningUnavailable
    | ReasoningProviderDefault
)


@dataclass(frozen=True, slots=True)
class ModelCatalogDiagnostic:
    code: str
    route_id: str | None = None
    model_id: str | None = None
    detail: str = ""


@dataclass(frozen=True, slots=True)
class ModelCatalogEntry:
    key: ModelCatalogEntryKey
    route_name: str
    display_name: str
    endpoint: str | None
    wire_dialect: RouteWireDialect
    total_context_tokens: int | None
    limits: ModelHardLimits | None
    reasoning: ReasoningControlContract
    tool_call: bool | None
    wire_shape_hint: Literal["responses", "completions"] | None
    input_modalities: tuple[str, ...] | None = None
    output_modalities: tuple[str, ...] | None = None
    diagnostics: tuple[ModelCatalogDiagnostic, ...] = ()


@dataclass(frozen=True, slots=True)
class ModelCatalogRoute:
    route_id: str
    display_name: str
    entries: tuple[ModelCatalogEntry, ...]


@dataclass(frozen=True, slots=True)
class ModelCatalogSnapshot:
    entries: Mapping[ModelCatalogEntryKey, ModelCatalogEntry]
    routes: tuple[ModelCatalogRoute, ...]
    diagnostics: tuple[ModelCatalogDiagnostic, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "entries", MappingProxyType(dict(self.entries)))

    def get(self, key: ModelCatalogEntryKey) -> ModelCatalogEntry | None:
        return self.entries.get(key)


@dataclass(frozen=True, slots=True)
class SelectableModelCatalog:
    entries: Mapping[ModelCatalogEntryKey, ModelCatalogEntry]
    routes: tuple[ModelCatalogRoute, ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "entries", MappingProxyType(dict(self.entries)))

    def require(self, key: ModelCatalogEntryKey) -> ModelCatalogEntry:
        try:
            return self.entries[key]
        except KeyError as exc:
            raise KeyError(
                f"model is outside the selectable catalog: {key.route_id}/{key.model_id}"
            ) from exc


class ModelCatalogUnavailable(RuntimeError):
    pass


class ModelCatalogInvalid(ValueError):
    pass


def selectable_model(entry: ModelCatalogEntry) -> bool:
    leaf = entry.key.model_id.rsplit("/", 1)[-1].removeprefix("~").casefold()
    return (
        entry.total_context_tokens is not None
        and entry.total_context_tokens >= MINIMUM_SELECTABLE_CONTEXT_TOKENS
        and not leaf.startswith("claude")
        and not leaf.startswith("gemini")
    )


def selectable_catalog(snapshot: ModelCatalogSnapshot) -> SelectableModelCatalog:
    entries = {
        key: entry for key, entry in snapshot.entries.items() if selectable_model(entry)
    }
    routes = tuple(
        ModelCatalogRoute(
            route_id=route.route_id,
            display_name=route.display_name,
            entries=tuple(item for item in route.entries if item.key in entries),
        )
        for route in snapshot.routes
        if any(item.key in entries for item in route.entries)
    )
    return SelectableModelCatalog(entries=entries, routes=routes)


def parse_models_dev_catalog(payload: object) -> ModelCatalogSnapshot:
    if not isinstance(payload, Mapping):
        raise ModelCatalogInvalid("models.dev catalog root must be an object")
    entries: dict[ModelCatalogEntryKey, ModelCatalogEntry] = {}
    routes: list[ModelCatalogRoute] = []
    snapshot_diagnostics: list[ModelCatalogDiagnostic] = []
    for raw_route_id, raw_route in payload.items():
        if not isinstance(raw_route_id, str) or not raw_route_id.strip():
            snapshot_diagnostics.append(
                ModelCatalogDiagnostic("catalog_route_id_invalid")
            )
            continue
        route_id = raw_route_id
        if not isinstance(raw_route, Mapping):
            snapshot_diagnostics.append(
                ModelCatalogDiagnostic("catalog_route_invalid", route_id=route_id)
            )
            continue
        route_name = raw_route.get("name")
        if not isinstance(route_name, str) or not route_name.strip():
            route_name = route_id
        inner_route_id = raw_route.get("id")
        if inner_route_id is not None and inner_route_id != route_id:
            snapshot_diagnostics.append(
                ModelCatalogDiagnostic(
                    "catalog_route_inner_id_mismatch", route_id=route_id
                )
            )
        provider_endpoint = _optional_nonempty_text(raw_route.get("api"))
        wire_dialect = _parse_route_wire_dialect(
            raw_route.get("npm"),
            route_id=route_id,
            diagnostics=snapshot_diagnostics,
        )
        raw_models = raw_route.get("models")
        if not isinstance(raw_models, Mapping):
            snapshot_diagnostics.append(
                ModelCatalogDiagnostic("catalog_route_models_invalid", route_id=route_id)
            )
            continue
        route_entries: list[ModelCatalogEntry] = []
        for raw_model_id, raw_model in raw_models.items():
            if not isinstance(raw_model_id, str) or not raw_model_id.strip():
                snapshot_diagnostics.append(
                    ModelCatalogDiagnostic("catalog_model_id_invalid", route_id=route_id)
                )
                continue
            model_id = raw_model_id
            if not isinstance(raw_model, Mapping):
                snapshot_diagnostics.append(
                    ModelCatalogDiagnostic(
                        "catalog_model_invalid", route_id=route_id, model_id=model_id
                    )
                )
                continue
            entry = _parse_model_entry(
                route_id=route_id,
                route_name=route_name,
                model_id=model_id,
                raw_model=raw_model,
                provider_endpoint=provider_endpoint,
                wire_dialect=wire_dialect,
            )
            entries[entry.key] = entry
            route_entries.append(entry)
        routes.append(
            ModelCatalogRoute(route_id, route_name, tuple(route_entries))
        )
    if not routes:
        raise ModelCatalogInvalid("models.dev catalog has no enumerable providers")
    return ModelCatalogSnapshot(entries, tuple(routes), tuple(snapshot_diagnostics))


def _parse_model_entry(
    *,
    route_id: str,
    route_name: str,
    model_id: str,
    raw_model: Mapping[object, object],
    provider_endpoint: str | None,
    wire_dialect: RouteWireDialect,
) -> ModelCatalogEntry:
    diagnostics: list[ModelCatalogDiagnostic] = []

    def diagnostic(code: str, detail: str = "") -> None:
        diagnostics.append(ModelCatalogDiagnostic(code, route_id, model_id, detail))

    inner_model_id = raw_model.get("id")
    if inner_model_id is not None and inner_model_id != model_id:
        diagnostic("catalog_model_inner_id_mismatch")
    display_name = raw_model.get("name")
    if not isinstance(display_name, str) or not display_name.strip():
        display_name = model_id
    model_provider = raw_model.get("provider")
    model_provider_mapping = model_provider if isinstance(model_provider, Mapping) else {}
    endpoint = _optional_nonempty_text(model_provider_mapping.get("api")) or provider_endpoint
    shape = model_provider_mapping.get("shape")
    wire_shape_hint: Literal["responses", "completions"] | None = None
    if shape in {"responses", "completions"}:
        wire_shape_hint = shape
    elif shape is not None:
        diagnostic("catalog_wire_shape_unknown", str(shape))

    raw_limit = raw_model.get("limit")
    total_context: int | None = None
    limits: ModelHardLimits | None = None
    if not isinstance(raw_limit, Mapping):
        diagnostic("catalog_limits_invalid")
    else:
        context = _positive_int(raw_limit.get("context"))
        output = _positive_int(raw_limit.get("output"))
        raw_input = raw_limit.get("input")
        input_limit = context if raw_input is None else _positive_int(raw_input)
        total_context = context
        if context is None:
            diagnostic("catalog_context_limit_invalid")
        if output is None:
            diagnostic("catalog_output_limit_invalid")
        if raw_input is not None and input_limit is None:
            diagnostic("catalog_input_limit_invalid")
        if context is not None and output is not None and input_limit is not None:
            try:
                limits = ModelHardLimits(context, input_limit, output)
            except ValueError as exc:
                diagnostic("catalog_limits_contradictory", str(exc))

    reasoning = _parse_reasoning(
        raw_model.get("reasoning"),
        raw_model.get("reasoning_options", _MISSING),
        diagnostic,
    )
    raw_tool_call = raw_model.get("tool_call")
    tool_call = raw_tool_call if isinstance(raw_tool_call, bool) else None
    if raw_tool_call is not None and tool_call is None:
        diagnostic("catalog_tool_call_invalid")
    modalities = raw_model.get("modalities", _MISSING)
    input_modalities = _parse_modalities(modalities, "input", diagnostic)
    output_modalities = _parse_modalities(modalities, "output", diagnostic)
    return ModelCatalogEntry(
        key=ModelCatalogEntryKey(route_id, model_id),
        route_name=route_name,
        display_name=display_name,
        endpoint=endpoint,
        wire_dialect=wire_dialect,
        total_context_tokens=total_context,
        limits=limits,
        reasoning=reasoning,
        tool_call=tool_call,
        wire_shape_hint=wire_shape_hint,
        input_modalities=input_modalities,
        output_modalities=output_modalities,
        diagnostics=tuple(diagnostics),
    )


_MISSING = object()


def _parse_modalities(
    raw_modalities: object,
    direction: Literal["input", "output"],
    diagnostic: Callable[[str, str], None],
) -> tuple[str, ...] | None:
    if raw_modalities is _MISSING:
        diagnostic(f"catalog_{direction}_modalities_missing")
        return None
    if not isinstance(raw_modalities, Mapping):
        diagnostic(f"catalog_{direction}_modalities_invalid", "modalities_not_an_object")
        return None
    if direction not in raw_modalities:
        diagnostic(f"catalog_{direction}_modalities_missing")
        return None
    raw_input = raw_modalities[direction]
    if not isinstance(raw_input, list):
        diagnostic(f"catalog_{direction}_modalities_invalid", f"{direction}_not_an_array")
        return None
    if any(
        not isinstance(value, str) or not value or value != value.strip()
        for value in raw_input
    ):
        diagnostic(f"catalog_{direction}_modalities_invalid", f"{direction}_member_invalid")
        return None
    return tuple(raw_input)


def _parse_reasoning(
    raw_reasoning: object,
    raw_options: object,
    diagnostic: Callable[[str, str], None],
) -> ReasoningControlContract:
    if raw_reasoning is not True:
        if raw_reasoning is not False and raw_reasoning is not None:
            diagnostic("catalog_reasoning_flag_invalid", str(raw_reasoning))
        return ReasoningUnavailable()
    if raw_options is _MISSING:
        return ReasoningProviderDefault()
    if not isinstance(raw_options, list):
        diagnostic("catalog_reasoning_options_invalid", "not_an_array")
        return ReasoningProviderDefault()
    if not raw_options:
        return ReasoningFixedOn()

    families: dict[str, list[Mapping[object, object]]] = {}
    for option in raw_options:
        if not isinstance(option, Mapping):
            diagnostic("catalog_reasoning_option_invalid", "not_an_object")
            continue
        kind = option.get("type")
        if kind not in {"effort", "toggle", "budget_tokens"}:
            diagnostic("catalog_reasoning_option_kind_unknown", str(kind))
            continue
        families.setdefault(str(kind), []).append(option)

    effort: ReasoningEffortChoices | None = None
    toggle: ReasoningToggle | None = None
    budget: ReasoningTokenBudgetRange | None = None
    if len(families.get("effort", ())) > 1:
        diagnostic("catalog_reasoning_effort_duplicated", "")
    elif families.get("effort"):
        values = families["effort"][0].get("values")
        if isinstance(values, list) and values and all(
            value is None or isinstance(value, str) for value in values
        ):
            try:
                effort = ReasoningEffortChoices(tuple(values))
            except ValueError as exc:
                diagnostic("catalog_reasoning_effort_invalid", str(exc))
        else:
            diagnostic("catalog_reasoning_effort_invalid", "values")
    if len(families.get("toggle", ())) > 1:
        diagnostic("catalog_reasoning_toggle_duplicated", "")
    elif families.get("toggle"):
        toggle = ReasoningToggle()
    if len(families.get("budget_tokens", ())) > 1:
        diagnostic("catalog_reasoning_budget_duplicated", "")
    elif families.get("budget_tokens"):
        item = families["budget_tokens"][0]
        raw_minimum = item.get("min")
        raw_maximum = item.get("max")
        minimum = None if raw_minimum is None else _positive_int(raw_minimum)
        maximum = None if raw_maximum is None else _positive_int(raw_maximum)
        if (raw_minimum is not None and minimum is None) or (
            raw_maximum is not None and maximum is None
        ):
            diagnostic("catalog_reasoning_budget_invalid", "bounds")
        else:
            try:
                budget = ReasoningTokenBudgetRange(minimum, maximum)
            except ValueError as exc:
                diagnostic("catalog_reasoning_budget_invalid", str(exc))
    if effort is None and toggle is None and budget is None:
        return ReasoningProviderDefault()
    return ReasoningSelectableControls(effort, toggle, budget)


def _positive_int(value: object) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        return None
    return value


def _optional_nonempty_text(value: object) -> str | None:
    return value if isinstance(value, str) and value.strip() else None


_OPENAI_COMPATIBLE_PROVIDER_PACKAGES = frozenset(
    {
        "@ai-sdk/openai-compatible",
        "@ai-sdk/openai",
        "@openrouter/ai-sdk-provider",
    }
)


def _parse_route_wire_dialect(
    raw_package: object,
    *,
    route_id: str,
    diagnostics: list[ModelCatalogDiagnostic],
) -> RouteWireDialect:
    if raw_package in _OPENAI_COMPATIBLE_PROVIDER_PACKAGES:
        return RouteWireDialect.OPENAI_COMPATIBLE
    if isinstance(raw_package, str) and raw_package.strip():
        return RouteWireDialect.PROVIDER_NATIVE
    diagnostics.append(
        ModelCatalogDiagnostic("catalog_route_wire_dialect_unknown", route_id=route_id)
    )
    return RouteWireDialect.UNKNOWN


CatalogFetch = Callable[[], Awaitable[object]]


@dataclass(slots=True)
class ModelsDevCatalogClient:
    fetch_override: CatalogFetch | None = None

    async def fetch(self) -> ModelCatalogSnapshot:
        try:
            payload = (
                await self.fetch_override()
                if self.fetch_override is not None
                else await self._fetch_http()
            )
        except (httpx.HTTPError, TimeoutError, OSError) as exc:
            raise ModelCatalogUnavailable("models.dev catalog is unavailable") from exc
        except (TypeError, ValueError) as exc:
            raise ModelCatalogInvalid("models.dev catalog is invalid") from exc
        try:
            return parse_models_dev_catalog(payload)
        except ModelCatalogInvalid:
            raise
        except (TypeError, ValueError) as exc:
            raise ModelCatalogInvalid("models.dev catalog is invalid") from exc

    @staticmethod
    async def _fetch_http() -> object:
        timeout = httpx.Timeout(30.0, connect=10.0, read=30.0, write=10.0, pool=10.0)
        async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
            response = await client.get(
                MODELS_DEV_CATALOG_URL,
                headers={"Accept": "application/json"},
            )
            response.raise_for_status()
            return response.json()


@dataclass(slots=True)
class ModelCatalogOwner:
    client: ModelsDevCatalogClient
    _snapshot: ModelCatalogSnapshot | None = None
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock, repr=False)

    @property
    def snapshot(self) -> ModelCatalogSnapshot | None:
        return self._snapshot

    def selectable(self) -> SelectableModelCatalog | None:
        return None if self._snapshot is None else selectable_catalog(self._snapshot)

    async def refresh(self) -> ModelCatalogSnapshot:
        async with self._lock:
            snapshot = await self.client.fetch()
            self._snapshot = snapshot
            return snapshot


__all__ = [
    "MINIMUM_SELECTABLE_CONTEXT_TOKENS",
    "MODELS_DEV_CATALOG_URL",
    "ModelCatalogDiagnostic",
    "ModelCatalogEntry",
    "ModelCatalogEntryKey",
    "ModelCatalogInvalid",
    "ModelCatalogOwner",
    "ModelCatalogRoute",
    "ModelCatalogSnapshot",
    "ModelCatalogUnavailable",
    "ModelHardLimits",
    "ModelTargetKey",
    "ModelsDevCatalogClient",
    "ReasoningControlContract",
    "ReasoningEffortChoices",
    "ReasoningFixedOn",
    "ReasoningProviderDefault",
    "ReasoningSelectableControls",
    "ReasoningToggle",
    "ReasoningTokenBudgetRange",
    "ReasoningUnavailable",
    "RouteWireDialect",
    "SelectableModelCatalog",
    "WireApi",
    "parse_models_dev_catalog",
    "selectable_catalog",
    "selectable_model",
]
