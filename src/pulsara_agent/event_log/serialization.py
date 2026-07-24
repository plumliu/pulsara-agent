"""Versioned AgentEvent serialization and historical decoder bindings."""

from __future__ import annotations

import json
from dataclasses import dataclass
from hashlib import sha256
from typing import Any, Callable, Mapping, cast, get_args

from pydantic import BaseModel

from pulsara_agent.event.events import AgentEvent, EventType
from pulsara_agent.primitives.context import (
    canonical_json_bytes,
    canonical_utc_timestamp,
    context_fingerprint,
)
from pulsara_agent.primitives.long_horizon import EventSchemaDomainContractFact


# This is a catalog migration version only.  Per-row decoder identity is the
# event type/version/schema/domain tuple below.
AGENT_EVENT_SCHEMA_VERSION = 6


class EventSchemaRegistryConflict(RuntimeError):
    """Two bindings claim one immutable event schema identity."""


class EventSchemaContractMismatch(RuntimeError):
    """A stored event cannot be rebound to its historical schema contract."""


@dataclass(frozen=True, slots=True)
class FrozenEventWriteCandidate:
    """One pre-commit event payload frozen against an exact schema binding."""

    event_id: str
    event_type: str
    event_schema_version: str
    event_schema_fingerprint: str
    event_domain_contract_fingerprint: str
    canonical_payload_bytes: bytes
    payload_fingerprint: str

    def __post_init__(self) -> None:
        if not self.event_id or not self.event_type:
            raise ValueError("event write candidate identity is required")
        if payload_sha256(self.canonical_payload_bytes) != self.payload_fingerprint:
            raise ValueError("event write candidate payload fingerprint mismatch")
        try:
            payload = json.loads(self.canonical_payload_bytes.decode("utf-8"))
        except Exception as exc:
            raise ValueError("event write candidate payload is not canonical JSON") from exc
        if not isinstance(payload, dict):
            raise ValueError("event write candidate payload must be an object")
        if (
            payload.get("id") != self.event_id
            or str(payload.get("type")) != self.event_type
            or payload.get("sequence") is not None
        ):
            raise ValueError("event write candidate wrapper identity mismatch")

    def fingerprint_payload(self) -> dict[str, str]:
        return {
            "event_id": self.event_id,
            "event_type": self.event_type,
            "event_schema_version": self.event_schema_version,
            "event_schema_fingerprint": self.event_schema_fingerprint,
            "event_domain_contract_fingerprint": (
                self.event_domain_contract_fingerprint
            ),
            "canonical_payload_utf8": self.canonical_payload_bytes.decode("utf-8"),
            "payload_fingerprint": self.payload_fingerprint,
        }


def _event_type_for_class(event_cls: type[BaseModel]) -> str:
    return str(event_cls.model_fields["type"].default)


_EVENT_CLASS_BY_TYPE: dict[str, type[BaseModel]] = {
    _event_type_for_class(event_cls): event_cls for event_cls in get_args(AgentEvent)
}


def _schema_version(event_type: str) -> str:
    return f"agent-event:{event_type.lower()}:v1"


_DISPLAY_ONLY_SCHEMA_KEYS = frozenset(
    {"title", "description", "examples", "$comment"}
)


def _normalize_validation_schema(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            key: _normalize_validation_schema(item)
            for key, item in sorted(value.items())
            if key not in _DISPLAY_ONLY_SCHEMA_KEYS
        }
    if isinstance(value, list):
        return [_normalize_validation_schema(item) for item in value]
    return value


def event_schema_fingerprint(
    *, event_type: str, event_schema_version: str, event_model: type[BaseModel]
) -> str:
    normalized = _normalize_validation_schema(
        event_model.model_json_schema(ref_template="#/$defs/{model}")
    )
    return context_fingerprint(
        "agent-event-schema-contract:v1",
        {
            "event_type": event_type,
            "event_schema_version": event_schema_version,
            "normalized_validation_schema": normalized,
        },
    )


def _event_domain(event_type: str) -> str:
    if event_type.startswith("SUBAGENT_") and event_type != str(
        EventType.SUBAGENT_GRAPH_CHECKPOINT_COMMITTED
    ):
        return "subagent_graph"
    return "non_graph"


def _decoder_contract_fingerprint(
    *, event_type: str, event_schema_version: str, event_schema_fingerprint: str
) -> str:
    return context_fingerprint(
        "agent-event-decoder-contract:v1",
        {
            "event_type": event_type,
            "event_schema_version": event_schema_version,
            "event_schema_fingerprint": event_schema_fingerprint,
            "decode_contract": "pydantic-owned-strict-round-trip:v1",
        },
    )


def _domain_contract(
    *, event_type: str, event_schema_version: str, event_model: type[BaseModel]
) -> EventSchemaDomainContractFact:
    schema_fingerprint = event_schema_fingerprint(
        event_type=event_type,
        event_schema_version=event_schema_version,
        event_model=event_model,
    )
    payload = {
        "event_type": event_type,
        "event_schema_version": event_schema_version,
        "event_schema_fingerprint": schema_fingerprint,
        "event_domain": _event_domain(event_type),
        "decoder_contract_fingerprint": _decoder_contract_fingerprint(
            event_type=event_type,
            event_schema_version=event_schema_version,
            event_schema_fingerprint=schema_fingerprint,
        ),
    }
    return EventSchemaDomainContractFact(
        **payload,
        domain_contract_fingerprint=context_fingerprint(
            "event-schema-domain-contract:v1", payload
        ),
    )


@dataclass(frozen=True, slots=True)
class HistoricalEventDecoderBinding:
    schema_contract: EventSchemaDomainContractFact
    decoder_contract_fingerprint: str
    implementation_build_fingerprint: str
    decode_owned_payload: Callable[[bytes], object]
    project_graph_semantic_payload: Callable[[bytes], bytes] | None


class EventSchemaDomainRegistry:
    """Immutable historical event-schema and decoder registry."""

    def __init__(self) -> None:
        self._by_identity: dict[
            tuple[str, str, str], HistoricalEventDecoderBinding
        ] = {}
        self._latest_by_type: dict[str, HistoricalEventDecoderBinding] = {}

    def register(
        self,
        *,
        event_model: type[BaseModel],
        event_schema_version: str,
        implementation_build_fingerprint: str = "builtin-agent-events:v1",
    ) -> HistoricalEventDecoderBinding:
        event_type = _event_type_for_class(event_model)
        contract = _domain_contract(
            event_type=event_type,
            event_schema_version=event_schema_version,
            event_model=event_model,
        )
        identity = (
            event_type,
            event_schema_version,
            contract.event_schema_fingerprint,
        )

        def decode(payload_bytes: bytes) -> object:
            payload = json.loads(payload_bytes.decode("utf-8"))
            if not isinstance(payload, dict):
                raise EventSchemaContractMismatch("event payload must be an object")
            event = event_model.model_validate(payload)
            if canonical_json_bytes(event.model_dump(mode="json")) != payload_bytes:
                raise EventSchemaContractMismatch(
                    "historical event payload is not strict round-trip stable"
                )
            return event

        projector = None
        if contract.event_domain == "subagent_graph":

            def project(payload_bytes: bytes) -> bytes:
                payload = json.loads(payload_bytes.decode("utf-8"))
                if not isinstance(payload, dict):
                    raise EventSchemaContractMismatch(
                        "graph event payload must be an object"
                    )
                # Storage sequence is operational attribution.  All other
                # fields are part of this v1 event's declared semantic shape.
                semantic = dict(payload)
                semantic.pop("sequence", None)
                return canonical_json_bytes(semantic)

            projector = project
        binding = HistoricalEventDecoderBinding(
            schema_contract=contract,
            decoder_contract_fingerprint=contract.decoder_contract_fingerprint,
            implementation_build_fingerprint=implementation_build_fingerprint,
            decode_owned_payload=decode,
            project_graph_semantic_payload=projector,
        )
        existing = self._by_identity.get(identity)
        if existing is not None and existing.schema_contract != contract:
            raise EventSchemaRegistryConflict(
                f"event schema identity conflict: {identity!r}"
            )
        type_version_conflict = next(
            (
                candidate
                for key, candidate in self._by_identity.items()
                if key[:2] == identity[:2]
                and candidate.schema_contract.event_schema_fingerprint
                != contract.event_schema_fingerprint
            ),
            None,
        )
        if type_version_conflict is not None:
            raise EventSchemaRegistryConflict(
                "one event type/version cannot name multiple schema fingerprints"
            )
        self._by_identity[identity] = binding
        self._latest_by_type[event_type] = binding
        return binding

    def resolve_for_event(self, event: AgentEvent) -> HistoricalEventDecoderBinding:
        try:
            binding = self._latest_by_type[str(event.type)]
        except KeyError as exc:
            raise EventSchemaContractMismatch(
                f"event type has no schema binding: {event.type}"
            ) from exc
        if not isinstance(event, _EVENT_CLASS_BY_TYPE[str(event.type)]):
            raise EventSchemaContractMismatch("event class/type binding mismatch")
        return binding

    def resolve_historical_binding(
        self,
        *,
        event_type: str,
        event_schema_version: str,
        event_schema_fingerprint: str,
        event_domain_contract_fingerprint: str,
    ) -> HistoricalEventDecoderBinding:
        identity = (
            event_type,
            event_schema_version,
            event_schema_fingerprint,
        )
        try:
            binding = self._by_identity[identity]
        except KeyError as exc:
            raise EventSchemaContractMismatch(
                f"historical event schema binding is unavailable: {identity!r}"
            ) from exc
        if (
            binding.schema_contract.domain_contract_fingerprint
            != event_domain_contract_fingerprint
        ):
            raise EventSchemaContractMismatch(
                "historical event domain contract fingerprint mismatch"
            )
        if (
            binding.decoder_contract_fingerprint
            != binding.schema_contract.decoder_contract_fingerprint
        ):
            raise EventSchemaContractMismatch(
                "historical event decoder contract fingerprint mismatch"
            )
        return binding

    def latest_contract_for_type(self, event_type: str) -> EventSchemaDomainContractFact:
        try:
            return self._latest_by_type[event_type].schema_contract
        except KeyError as exc:
            raise EventSchemaContractMismatch(
                f"event type has no latest schema binding: {event_type}"
            ) from exc

    def contracts(self) -> tuple[EventSchemaDomainContractFact, ...]:
        return tuple(
            sorted(
                (binding.schema_contract for binding in self._by_identity.values()),
                key=lambda item: (item.event_type, item.event_schema_version),
            )
        )


DEFAULT_EVENT_SCHEMA_REGISTRY = EventSchemaDomainRegistry()
for _event_cls in get_args(AgentEvent):
    _event_type = _event_type_for_class(_event_cls)
    DEFAULT_EVENT_SCHEMA_REGISTRY.register(
        event_model=_event_cls,
        event_schema_version=_schema_version(_event_type),
    )


def freeze_event_write_candidate(
    event: AgentEvent,
    *,
    registry: EventSchemaDomainRegistry = DEFAULT_EVENT_SCHEMA_REGISTRY,
) -> FrozenEventWriteCandidate:
    """Freeze one uncommitted event using the registry's exact current binding."""

    if event.sequence is not None:
        raise ValueError("event write candidate requires sequence=None")
    binding = registry.resolve_for_event(event)
    contract = binding.schema_contract
    payload = canonical_event_payload_bytes(event)
    candidate = FrozenEventWriteCandidate(
        event_id=event.id,
        event_type=str(event.type),
        event_schema_version=contract.event_schema_version,
        event_schema_fingerprint=contract.event_schema_fingerprint,
        event_domain_contract_fingerprint=contract.domain_contract_fingerprint,
        canonical_payload_bytes=payload,
        payload_fingerprint=payload_sha256(payload),
    )
    # Decode through the historical binding now so a malformed registry entry
    # cannot escape preparation and fail only inside the durable writer.
    decode_event_write_candidate(candidate, registry=registry)
    return candidate


def decode_event_write_candidate(
    candidate: FrozenEventWriteCandidate,
    *,
    registry: EventSchemaDomainRegistry = DEFAULT_EVENT_SCHEMA_REGISTRY,
) -> AgentEvent:
    """Return an owned event only after exact candidate/registry validation."""

    binding = registry.resolve_historical_binding(
        event_type=candidate.event_type,
        event_schema_version=candidate.event_schema_version,
        event_schema_fingerprint=candidate.event_schema_fingerprint,
        event_domain_contract_fingerprint=(
            candidate.event_domain_contract_fingerprint
        ),
    )
    event = binding.decode_owned_payload(candidate.canonical_payload_bytes)
    if not isinstance(event, BaseModel):
        raise EventSchemaContractMismatch(
            "event write candidate decoder returned a non-event payload"
        )
    owned = cast(AgentEvent, event)
    if (
        owned.id != candidate.event_id
        or str(owned.type) != candidate.event_type
        or owned.sequence is not None
        or canonical_event_payload_bytes(owned) != candidate.canonical_payload_bytes
    ):
        raise EventSchemaContractMismatch(
            "event write candidate does not round-trip its frozen payload"
        )
    return owned


def dump_agent_event(event: AgentEvent) -> dict[str, Any]:
    return event.model_dump(mode="json")


def load_agent_event(payload: Mapping[str, Any]) -> AgentEvent:
    event_type = payload.get("type")
    if event_type is None:
        raise ValueError("AgentEvent payload is missing type")
    event_cls = _EVENT_CLASS_BY_TYPE.get(str(event_type))
    if event_cls is None:
        raise ValueError(f"Unknown AgentEvent type: {event_type}")
    return cast(AgentEvent, event_cls.model_validate(payload))


def canonical_event_payload_bytes(event: AgentEvent) -> bytes:
    return canonical_json_bytes(dump_agent_event(event))


def payload_sha256(payload: bytes) -> str:
    return f"sha256:{sha256(payload).hexdigest()}"


def canonical_event_created_at(event: AgentEvent) -> str:
    return canonical_utc_timestamp(event.created_at)
