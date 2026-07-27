"""Closed executable and trigger registries for durable projections."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Protocol, cast

from pulsara_agent.event import EventType
from pulsara_agent.event_log.serialization import DEFAULT_EVENT_SCHEMA_REGISTRY
from pulsara_agent.primitives._context_base import context_fingerprint
from pulsara_agent.projection_jobs.canonical_mutation import (
    default_projection_delivery_policy,
)
from pulsara_agent.projection_jobs.contracts import (
    CanonicalMutationKind,
    CanonicalMutationPlannedSurfaceFact,
    CanonicalMutationSurface,
    CanonicalMutationSurfaceHandlerContractFact,
    CanonicalMutationSurfacePlanFact,
    DurableProjectionDeliveryPolicyFact,
    DurableProjectionHandlerContractFact,
    DurableProjectionKind,
    DurableProjectionSeedContractFact,
    DurableProjectionTargetUpdatePolicy,
    DurableProjectionTriggerBindingFact,
    LeasedDurableProjectionJob,
    PreparedDurableProjectionResultFact,
    build_projection_fact,
)


class DurableProjectionExecutable(Protocol):
    def __call__(
        self,
        leased_job: LeasedDurableProjectionJob,
        *,
        deadline_monotonic: float,
    ) -> PreparedDurableProjectionResultFact: ...


@dataclass(frozen=True, slots=True)
class DurableProjectionExecutableBinding:
    contract: DurableProjectionHandlerContractFact
    executable: DurableProjectionExecutable


class DurableProjectionExecutableRegistry:
    def __init__(
        self, bindings: tuple[DurableProjectionExecutableBinding, ...]
    ) -> None:
        self._by_kind = {item.contract.projection_kind: item for item in bindings}
        if len(self._by_kind) != len(bindings):
            raise ValueError("projection executable registry has duplicate kinds")

    def resolve(
        self,
        kind: DurableProjectionKind,
        *,
        contract_fingerprint: str | None = None,
    ) -> DurableProjectionExecutableBinding:
        try:
            binding = self._by_kind[kind]
        except KeyError as exc:
            raise ValueError(f"projection executable is unavailable: {kind}") from exc
        if (
            contract_fingerprint is not None
            and binding.contract.contract_fingerprint != contract_fingerprint
        ):
            raise ValueError("projection executable contract mismatch")
        return binding

    def contracts(self) -> tuple[DurableProjectionHandlerContractFact, ...]:
        return tuple(
            self._by_kind[kind].contract
            for kind in sorted(self._by_kind, key=lambda item: item.value)
        )

    def executables(
        self,
    ) -> Mapping[DurableProjectionKind, DurableProjectionExecutable]:
        return {
            kind: self._by_kind[kind].executable
            for kind in sorted(self._by_kind, key=lambda item: item.value)
        }


def build_projection_executable_registry(
    executables: Mapping[DurableProjectionKind, DurableProjectionExecutable],
) -> DurableProjectionExecutableRegistry:
    expected = {
        DurableProjectionKind.RUN_TIMELINE: _TIMELINE_HANDLER,
        DurableProjectionKind.TOOL_RESULT_EXECUTION_EVIDENCE: (_EVIDENCE_HANDLER),
    }
    if set(executables) != set(expected):
        raise ValueError(
            "projection executable bindings do not cover the closed kind set"
        )
    return DurableProjectionExecutableRegistry(
        tuple(
            DurableProjectionExecutableBinding(
                contract=expected[kind],
                executable=executables[kind],
            )
            for kind in sorted(expected, key=lambda item: item.value)
        )
    )


class DurableProjectionTriggerRegistry:
    def __init__(
        self, contracts: tuple[DurableProjectionSeedContractFact, ...]
    ) -> None:
        self._by_kind = {item.projection_kind: item for item in contracts}
        if len(self._by_kind) != len(contracts):
            raise ValueError("projection trigger registry has duplicate kinds")

    def resolve(self, kind: DurableProjectionKind) -> DurableProjectionSeedContractFact:
        try:
            return self._by_kind[kind]
        except KeyError as exc:
            raise ValueError(
                f"projection seed contract is unavailable: {kind}"
            ) from exc

    def contracts(self) -> tuple[DurableProjectionSeedContractFact, ...]:
        return tuple(
            self._by_kind[kind]
            for kind in sorted(self._by_kind, key=lambda item: item.value)
        )


def _surface_plan(
    policy: DurableProjectionDeliveryPolicyFact,
) -> CanonicalMutationSurfacePlanFact:
    handler = cast(
        CanonicalMutationSurfaceHandlerContractFact,
        build_projection_fact(
            CanonicalMutationSurfaceHandlerContractFact,
            schema_version="canonical_mutation_surface_handler_contract.v1",
            surface=CanonicalMutationSurface.OXIGRAPH,
            handler_id="pulsara.oxigraph-materializer",
            handler_version="2",
            accepted_mutation_kinds=tuple(CanonicalMutationKind),
            payload_codec_fingerprint=context_fingerprint(
                "canonical-mutation-oxigraph-payload-codec:v2", {}
            ),
            target_compatibility_fingerprint=context_fingerprint(
                "canonical-mutation-oxigraph-target-compatibility:v2", {}
            ),
            idempotency_contract_fingerprint=context_fingerprint(
                "canonical-mutation-oxigraph-idempotency:v2", {}
            ),
        ),
    )
    planned = cast(
        CanonicalMutationPlannedSurfaceFact,
        build_projection_fact(
            CanonicalMutationPlannedSurfaceFact,
            schema_version="canonical_mutation_planned_surface.v1",
            handler_contract=handler,
            delivery_policy=policy,
        ),
    )
    return cast(
        CanonicalMutationSurfacePlanFact,
        build_projection_fact(
            CanonicalMutationSurfacePlanFact,
            schema_version="canonical_mutation_surface_plan.v1",
            ordered_surfaces=(planned,),
            composition_fingerprint=context_fingerprint(
                "canonical-mutation-surface-composition:v1",
                (CanonicalMutationSurface.OXIGRAPH.value,),
            ),
        ),
    )


def _event_contracts(
    event_types: tuple[EventType, ...],
) -> tuple[tuple[str, ...], str]:
    contracts = tuple(
        DEFAULT_EVENT_SCHEMA_REGISTRY.latest_contract_for_type(str(event_type))
        for event_type in event_types
    )
    return (
        tuple(item.event_schema_fingerprint for item in contracts),
        context_fingerprint(
            "durable-projection-accepted-event-schema-bindings:v1",
            tuple(
                {
                    "event_type": item.event_type,
                    "event_schema_version": item.event_schema_version,
                    "event_schema_fingerprint": item.event_schema_fingerprint,
                    "event_domain_contract_fingerprint": (
                        item.domain_contract_fingerprint
                    ),
                }
                for item in contracts
            ),
        ),
    )


def _handler(
    *,
    kind: DurableProjectionKind,
    event_types: tuple[EventType, ...],
    update_policy: DurableProjectionTargetUpdatePolicy,
) -> tuple[DurableProjectionHandlerContractFact, tuple[str, ...]]:
    schema_fingerprints, binding_fingerprint = _event_contracts(event_types)
    handler = cast(
        DurableProjectionHandlerContractFact,
        build_projection_fact(
            DurableProjectionHandlerContractFact,
            schema_version="durable_projection_handler_contract.v1",
            projection_kind=kind,
            handler_id=f"pulsara.{kind.value}",
            handler_version="1",
            accepted_source_event_types=tuple(str(item) for item in event_types),
            accepted_source_schema_bindings_fingerprint=binding_fingerprint,
            target_update_policy=update_policy,
            result_schema_fingerprint=context_fingerprint(
                "durable-projection-result-schema:v1",
                {"projection_kind": kind.value},
            ),
            idempotency_contract_fingerprint=context_fingerprint(
                "durable-projection-idempotency-contract:v1",
                {"projection_kind": kind.value},
            ),
        ),
    )
    return handler, schema_fingerprints


def _seed_contract(
    *,
    handler: DurableProjectionHandlerContractFact,
    event_types: tuple[EventType, ...],
    schema_fingerprints: tuple[str, ...],
    delivery_policy: DurableProjectionDeliveryPolicyFact,
    surface_plan: CanonicalMutationSurfacePlanFact,
) -> DurableProjectionSeedContractFact:
    bindings = tuple(
        cast(
            DurableProjectionTriggerBindingFact,
            build_projection_fact(
                DurableProjectionTriggerBindingFact,
                schema_version="durable_projection_trigger_binding.v1",
                projection_kind=handler.projection_kind,
                trigger_event_type=str(event_type),
                accepted_event_schema_fingerprints=(schema_fingerprint,),
                target_resolver_id=(
                    "run-target"
                    if handler.projection_kind is DurableProjectionKind.RUN_TIMELINE
                    else "tool-result-target"
                ),
                target_resolver_version="1",
                target_resolver_contract_fingerprint=context_fingerprint(
                    "durable-projection-target-resolver:v1",
                    {
                        "projection_kind": handler.projection_kind.value,
                        "event_type": str(event_type),
                    },
                ),
            ),
        )
        for event_type, schema_fingerprint in zip(
            event_types, schema_fingerprints, strict=True
        )
    )
    return cast(
        DurableProjectionSeedContractFact,
        build_projection_fact(
            DurableProjectionSeedContractFact,
            schema_version="durable_projection_seed_contract.v1",
            projection_kind=handler.projection_kind,
            handler_contract=handler,
            delivery_policy=delivery_policy,
            canonical_mutation_surface_plan=surface_plan,
            ordered_trigger_bindings=bindings,
            source_query_contract_fingerprint=context_fingerprint(
                "durable-projection-source-query-contract:v1",
                {
                    "projection_kind": handler.projection_kind.value,
                    "page_size": 512,
                    "source_horizon": "exact-trigger-sequence",
                },
            ),
            candidate_factory_contract_fingerprint=context_fingerprint(
                "durable-projection-candidate-factory-contract:v1",
                {"projection_kind": handler.projection_kind.value},
            ),
        ),
    )


_DELIVERY_POLICY = default_projection_delivery_policy()
_SURFACE_PLAN = _surface_plan(_DELIVERY_POLICY)
_TIMELINE_TYPES = (
    EventType.REPLY_END,
    EventType.RUN_ERROR,
    EventType.RUN_END,
)
_EVIDENCE_TYPES = (EventType.TOOL_RESULT_END,)
_TIMELINE_HANDLER, _TIMELINE_SCHEMA_FPS = _handler(
    kind=DurableProjectionKind.RUN_TIMELINE,
    event_types=_TIMELINE_TYPES,
    update_policy=DurableProjectionTargetUpdatePolicy.FULL_REPLACEMENT,
)
_EVIDENCE_HANDLER, _EVIDENCE_SCHEMA_FPS = _handler(
    kind=DurableProjectionKind.TOOL_RESULT_EXECUTION_EVIDENCE,
    event_types=_EVIDENCE_TYPES,
    update_policy=DurableProjectionTargetUpdatePolicy.SINGLE_ASSIGNMENT,
)

DURABLE_PROJECTION_TRIGGER_REGISTRY = DurableProjectionTriggerRegistry(
    (
        _seed_contract(
            handler=_TIMELINE_HANDLER,
            event_types=_TIMELINE_TYPES,
            schema_fingerprints=_TIMELINE_SCHEMA_FPS,
            delivery_policy=_DELIVERY_POLICY,
            surface_plan=_SURFACE_PLAN,
        ),
        _seed_contract(
            handler=_EVIDENCE_HANDLER,
            event_types=_EVIDENCE_TYPES,
            schema_fingerprints=_EVIDENCE_SCHEMA_FPS,
            delivery_policy=_DELIVERY_POLICY,
            surface_plan=_SURFACE_PLAN,
        ),
    )
)


def validate_projection_registry_completeness(
    *,
    active_kinds: tuple[DurableProjectionKind, ...] | None = None,
    executable_registry: DurableProjectionExecutableRegistry | None = None,
) -> None:
    expected = set(DurableProjectionKind)
    trigger_kinds = {
        item.projection_kind for item in DURABLE_PROJECTION_TRIGGER_REGISTRY.contracts()
    }
    handler_kinds = {
        _TIMELINE_HANDLER.projection_kind,
        _EVIDENCE_HANDLER.projection_kind,
    }
    if trigger_kinds != expected or handler_kinds != expected:
        raise ValueError("projection registry does not cover the closed kind set")
    if active_kinds and executable_registry is None:
        raise ValueError("active projection validation requires executable registry")
    if executable_registry is not None:
        executable_kinds = {
            item.projection_kind for item in executable_registry.contracts()
        }
        if executable_kinds != expected:
            raise ValueError(
                "projection executable registry does not cover the closed kind set"
            )
        for kind in active_kinds or ():
            executable_registry.resolve(kind)


__all__ = [
    "DURABLE_PROJECTION_TRIGGER_REGISTRY",
    "DurableProjectionExecutable",
    "DurableProjectionExecutableBinding",
    "DurableProjectionExecutableRegistry",
    "DurableProjectionTriggerRegistry",
    "build_projection_executable_registry",
    "default_projection_delivery_policy",
    "validate_projection_registry_completeness",
]
