"""Versioned subagent graph reducer binding and canonical state codec."""

from __future__ import annotations

import types
from collections.abc import Mapping
from dataclasses import dataclass, fields, is_dataclass
from datetime import datetime
from enum import Enum
from typing import Any, Callable, TypeVar, get_args, get_origin, get_type_hints

from pulsara_agent.event_log.protocol import RawStoredEventEnvelope
from pulsara_agent.event_log.serialization import (
    DEFAULT_EVENT_SCHEMA_REGISTRY,
    EventSchemaContractMismatch,
    EventSchemaDomainRegistry,
)
from pulsara_agent.primitives.context import canonical_json_bytes, context_fingerprint
from pulsara_agent.primitives.long_horizon import (
    SubagentGraphReducerContractFact,
    SupportedGraphEventContractFact,
)
from pulsara_agent.runtime.subagent.facts import (
    SubagentConsumptionFact,
    SubagentDeliveryFact,
    SubagentEdgeFact,
    SubagentGraphDiagnostic,
    SubagentGraphState,
    SubagentResultFact,
    SubagentRunFact,
    SubagentTaskFact,
)
from pulsara_agent.runtime.subagent.immutable import thaw_json_value
from pulsara_agent.runtime.subagent.reducer import apply_subagent_event


GRAPH_REDUCER_ID = "pulsara.subagent_graph"
GRAPH_REDUCER_VERSION = "1"
GRAPH_SCHEMA_VERSION = "subagent-graph-state.v1"
GRAPH_SEMANTIC_CANONICALIZATION_VERSION = "subagent-graph-event-semantic:v1"


class SubagentGraphReducerRegistryConflict(RuntimeError):
    pass


class SubagentGraphReducerContractMismatch(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class SubagentGraphReducerBinding:
    contract: SubagentGraphReducerContractFact
    implementation_build_fingerprint: str
    empty_state_factory: Callable[[], SubagentGraphState]
    fold_stored_event: Callable[
        [SubagentGraphState, RawStoredEventEnvelope], SubagentGraphState
    ]
    export_canonical_state: Callable[[SubagentGraphState], bytes]
    restore_canonical_state: Callable[[bytes], SubagentGraphState]


class SubagentGraphReducerRegistry:
    def __init__(self) -> None:
        self._bindings: dict[tuple[str, str], SubagentGraphReducerBinding] = {}

    def register(self, binding: SubagentGraphReducerBinding) -> None:
        identity = (
            binding.contract.graph_reducer_id,
            binding.contract.graph_reducer_version,
        )
        existing = self._bindings.get(identity)
        if existing is not None and (
            existing.contract.graph_reducer_contract_fingerprint
            != binding.contract.graph_reducer_contract_fingerprint
        ):
            raise SubagentGraphReducerRegistryConflict(
                "one reducer id/version cannot name multiple contracts"
            )
        self._bindings[identity] = binding

    def resolve_binding(
        self,
        *,
        reducer_id: str,
        reducer_version: str,
        reducer_contract_fingerprint: str,
    ) -> SubagentGraphReducerBinding:
        try:
            binding = self._bindings[(reducer_id, reducer_version)]
        except KeyError as exc:
            raise SubagentGraphReducerContractMismatch(
                "subagent graph reducer binding is unavailable"
            ) from exc
        if (
            binding.contract.graph_reducer_contract_fingerprint
            != reducer_contract_fingerprint
        ):
            raise SubagentGraphReducerContractMismatch(
                "subagent graph reducer contract fingerprint mismatch"
            )
        return binding


def _supported_graph_event_contracts(
    registry: EventSchemaDomainRegistry,
) -> tuple[SupportedGraphEventContractFact, ...]:
    entries: list[SupportedGraphEventContractFact] = []
    for contract in registry.contracts():
        if contract.event_domain != "subagent_graph":
            continue
        projection_fp = context_fingerprint(
            "subagent-graph-event-semantic-projection-contract:v1",
            {
                "event_type": contract.event_type,
                "event_schema_version": contract.event_schema_version,
                "event_schema_fingerprint": contract.event_schema_fingerprint,
                "canonicalization": GRAPH_SEMANTIC_CANONICALIZATION_VERSION,
                "excluded_storage_fields": ("sequence",),
            },
        )
        payload = {
            "event_type": contract.event_type,
            "event_schema_version": contract.event_schema_version,
            "event_schema_fingerprint": contract.event_schema_fingerprint,
            "event_domain_contract_fingerprint": (
                contract.domain_contract_fingerprint
            ),
            "semantic_projection_contract_fingerprint": projection_fp,
        }
        entries.append(
            SupportedGraphEventContractFact(
                **payload,
                supported_event_fingerprint=context_fingerprint(
                    "supported-subagent-graph-event:v1", payload
                ),
            )
        )
    return tuple(
        sorted(entries, key=lambda item: (item.event_type, item.event_schema_version))
    )


def build_default_subagent_graph_reducer_contract(
    registry: EventSchemaDomainRegistry = DEFAULT_EVENT_SCHEMA_REGISTRY,
) -> SubagentGraphReducerContractFact:
    supported = _supported_graph_event_contracts(registry)
    payload = {
        "schema_version": "subagent_graph_reducer_contract.v1",
        "graph_reducer_id": GRAPH_REDUCER_ID,
        "graph_reducer_version": GRAPH_REDUCER_VERSION,
        "graph_schema_version": GRAPH_SCHEMA_VERSION,
        "supported_graph_events": supported,
        "event_filter_contract_fingerprint": context_fingerprint(
            "subagent-graph-event-filter:v1",
            tuple(
                (item.event_type, item.event_schema_version) for item in supported
            ),
        ),
        "graph_semantic_event_canonicalization_fingerprint": context_fingerprint(
            "subagent-graph-semantic-canonicalization:v1",
            {
                "version": GRAPH_SEMANTIC_CANONICALIZATION_VERSION,
                "excluded_storage_fields": ("sequence",),
            },
        ),
        "transition_contract_fingerprint": context_fingerprint(
            "subagent-graph-transition-contract:v1",
            "apply_subagent_event:hard-cut-2026-07",
        ),
        "invariant_contract_fingerprint": context_fingerprint(
            "subagent-graph-invariant-contract:v1",
            "subagent-reducer-invariants:hard-cut-2026-07",
        ),
        "canonical_state_contract_fingerprint": context_fingerprint(
            "subagent-graph-state-contract:v1",
            {
                "codec": "canonical-json-dataclass:v1",
                "semantic_excluded_fields": (
                    "through_sequence",
                    "applied_subagent_event_ids",
                    "created_sequence",
                    "last_sequence",
                    "terminal_sequence",
                    "sequence",
                ),
            },
        ),
    }
    return SubagentGraphReducerContractFact(
        **payload,
        graph_reducer_contract_fingerprint=context_fingerprint(
            "subagent-graph-reducer-contract:v1", payload
        ),
    )


def _event_contract(
    contract: SubagentGraphReducerContractFact,
    envelope: RawStoredEventEnvelope,
) -> SupportedGraphEventContractFact | None:
    return next(
        (
            item
            for item in contract.supported_graph_events
            if item.event_type == envelope.event_type
            and item.event_schema_version == envelope.event_schema_version
        ),
        None,
    )


def _fold_raw_event(
    state: SubagentGraphState,
    envelope: RawStoredEventEnvelope,
    *,
    contract: SubagentGraphReducerContractFact,
    schema_registry: EventSchemaDomainRegistry,
) -> SubagentGraphState:
    historical = schema_registry.resolve_historical_binding(
        event_type=envelope.event_type,
        event_schema_version=envelope.event_schema_version,
        event_schema_fingerprint=envelope.event_schema_fingerprint,
        event_domain_contract_fingerprint=envelope.event_domain_contract_fingerprint,
    )
    supported = _event_contract(contract, envelope)
    if historical.schema_contract.event_domain == "subagent_graph":
        if supported is None:
            raise SubagentGraphReducerContractMismatch(
                "graph-domain event is unsupported by the frozen reducer contract"
            )
        if (
            supported.event_schema_fingerprint
            != envelope.event_schema_fingerprint
            or supported.event_domain_contract_fingerprint
            != envelope.event_domain_contract_fingerprint
            or historical.project_graph_semantic_payload is None
        ):
            raise SubagentGraphReducerContractMismatch(
                "graph event schema/projector contract mismatch"
            )
    elif supported is not None:
        raise SubagentGraphReducerContractMismatch(
            "reducer declares a non-graph event as graph-domain"
        )
    event = envelope.decode_owned(schema_registry)
    return apply_subagent_event(state, event)


def export_subagent_graph_state(state: SubagentGraphState) -> bytes:
    if not state.consistent:
        raise ValueError("inconsistent subagent graph cannot be checkpointed")
    payload = thaw_json_value(state)
    if not isinstance(payload, dict):
        raise TypeError("subagent graph state did not serialize to an object")
    payload.pop("applied_subagent_event_ids", None)
    return canonical_json_bytes(
        {
            "schema_version": GRAPH_SCHEMA_VERSION,
            "state": payload,
        }
    )


T = TypeVar("T")


def _restore_value(annotation: Any, value: Any) -> Any:
    if value is None:
        return None
    origin = get_origin(annotation)
    args = get_args(annotation)
    if origin in (types.UnionType, getattr(__import__("typing"), "Union")):
        candidates = [item for item in args if item is not type(None)]
        for candidate in candidates:
            try:
                return _restore_value(candidate, value)
            except (TypeError, ValueError):
                continue
        return value
    if origin in (tuple,):
        item_type = args[0] if args else Any
        return tuple(_restore_value(item_type, item) for item in value)
    if origin in (frozenset,):
        item_type = args[0] if args else Any
        return frozenset(_restore_value(item_type, item) for item in value)
    if origin is not None and isinstance(value, dict) and len(args) == 2:
        key_type, value_type = args
        return {
            _restore_value(key_type, key): _restore_value(value_type, item)
            for key, item in value.items()
        }
    if annotation is datetime:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if isinstance(annotation, type) and issubclass(annotation, Enum):
        return annotation(value)
    if isinstance(annotation, type) and is_dataclass(annotation):
        return _restore_dataclass(annotation, value)
    return value


def _restore_dataclass(cls: type[T], payload: Mapping[str, Any]) -> T:
    hints = get_type_hints(cls)
    values = {
        field.name: _restore_value(hints.get(field.name, Any), payload[field.name])
        for field in fields(cls)
        if field.name in payload
    }
    return cls(**values)


def restore_subagent_graph_state(payload_bytes: bytes) -> SubagentGraphState:
    import json

    payload = json.loads(payload_bytes.decode("utf-8"))
    if not isinstance(payload, dict) or payload.get("schema_version") != GRAPH_SCHEMA_VERSION:
        raise ValueError("unsupported subagent graph checkpoint payload")
    raw = payload.get("state")
    if not isinstance(raw, dict):
        raise ValueError("subagent graph checkpoint state must be an object")
    state = SubagentGraphState(
        tasks={
            key: _restore_dataclass(SubagentTaskFact, value)
            for key, value in raw.get("tasks", {}).items()
        },
        runs={
            key: _restore_dataclass(SubagentRunFact, value)
            for key, value in raw.get("runs", {}).items()
        },
        results={
            key: _restore_dataclass(SubagentResultFact, value)
            for key, value in raw.get("results", {}).items()
        },
        edges={
            key: _restore_dataclass(SubagentEdgeFact, value)
            for key, value in raw.get("edges", {}).items()
        },
        consumptions={
            key: _restore_dataclass(SubagentConsumptionFact, value)
            for key, value in raw.get("consumptions", {}).items()
        },
        deliveries={
            key: _restore_dataclass(SubagentDeliveryFact, value)
            for key, value in raw.get("deliveries", {}).items()
        },
        diagnostics=tuple(
            _restore_dataclass(SubagentGraphDiagnostic, value)
            for value in raw.get("diagnostics", [])
        ),
        consistent=bool(raw.get("consistent", False)),
        through_sequence=int(raw.get("through_sequence", 0)),
        applied_subagent_event_ids=frozenset(),
    )
    if not state.consistent:
        raise ValueError("restored subagent graph checkpoint is inconsistent")
    return state


def build_default_subagent_graph_reducer_binding(
    schema_registry: EventSchemaDomainRegistry = DEFAULT_EVENT_SCHEMA_REGISTRY,
) -> SubagentGraphReducerBinding:
    contract = build_default_subagent_graph_reducer_contract(schema_registry)
    return SubagentGraphReducerBinding(
        contract=contract,
        implementation_build_fingerprint="builtin-subagent-reducer:2026-07",
        empty_state_factory=SubagentGraphState.empty,
        fold_stored_event=lambda state, event: _fold_raw_event(
            state, event, contract=contract, schema_registry=schema_registry
        ),
        export_canonical_state=export_subagent_graph_state,
        restore_canonical_state=restore_subagent_graph_state,
    )


DEFAULT_SUBAGENT_GRAPH_REDUCER_REGISTRY = SubagentGraphReducerRegistry()
DEFAULT_SUBAGENT_GRAPH_REDUCER_REGISTRY.register(
    build_default_subagent_graph_reducer_binding()
)


def graph_semantic_payload_fingerprint(
    *,
    envelope: RawStoredEventEnvelope,
    contract: SubagentGraphReducerContractFact,
    schema_registry: EventSchemaDomainRegistry = DEFAULT_EVENT_SCHEMA_REGISTRY,
) -> str | None:
    historical = schema_registry.resolve_historical_binding(
        event_type=envelope.event_type,
        event_schema_version=envelope.event_schema_version,
        event_schema_fingerprint=envelope.event_schema_fingerprint,
        event_domain_contract_fingerprint=envelope.event_domain_contract_fingerprint,
    )
    supported = _event_contract(contract, envelope)
    if historical.schema_contract.event_domain == "non_graph":
        if supported is not None:
            raise SubagentGraphReducerContractMismatch(
                "non-graph event appears in supported graph contract"
            )
        return None
    if supported is None or historical.project_graph_semantic_payload is None:
        raise SubagentGraphReducerContractMismatch(
            "unsupported graph-domain event encountered"
        )
    if (
        supported.event_schema_fingerprint != envelope.event_schema_fingerprint
        or supported.event_domain_contract_fingerprint
        != envelope.event_domain_contract_fingerprint
    ):
        raise EventSchemaContractMismatch("graph event contract identity drift")
    projected = historical.project_graph_semantic_payload(
        envelope.canonical_payload_bytes
    )
    return context_fingerprint(
        "subagent-graph-semantic-event-payload:v1", projected.decode("utf-8")
    )


def semantic_graph_state_bytes(state: SubagentGraphState) -> bytes:
    payload = thaw_json_value(state)
    if not isinstance(payload, dict):
        raise TypeError("subagent graph state did not serialize to an object")
    excluded = {
        "through_sequence",
        "applied_subagent_event_ids",
        "created_sequence",
        "last_sequence",
        "terminal_sequence",
        "sequence",
    }

    def strip(value: Any) -> Any:
        if isinstance(value, dict):
            return {
                key: strip(item)
                for key, item in sorted(value.items())
                if key not in excluded
            }
        if isinstance(value, list):
            return [strip(item) for item in value]
        return value

    return canonical_json_bytes(strip(payload))


def graph_state_semantic_fingerprint(state: SubagentGraphState) -> str:
    return context_fingerprint(
        "subagent-graph-state-semantic:v1",
        semantic_graph_state_bytes(state).decode("utf-8"),
    )
