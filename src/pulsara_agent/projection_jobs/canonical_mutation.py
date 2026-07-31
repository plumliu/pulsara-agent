"""Pure deterministic canonical-mutation V2 factories."""

from __future__ import annotations

from hashlib import sha256
from typing import Any, cast

from pulsara_agent.primitives._context_base import (
    canonical_json_bytes,
    context_fingerprint,
)
from pulsara_agent.projection_jobs.contracts import (
    CanonicalInlineJsonDocumentFact,
    CanonicalMutationCandidateFact,
    CanonicalMutationKind,
    CanonicalMutationPlannedSurfaceFact,
    CanonicalMutationSemanticFact,
    CanonicalMutationSurface,
    CanonicalMutationSurfaceHandlerContractFact,
    CanonicalMutationSurfacePlanFact,
    CanonicalMutationOwner,
    DurableProjectionDeliveryPolicyFact,
    DurableProjectionPhysicalPolicyFact,
    DurableProjectionRetryPolicyFact,
    PreparedCanonicalMutationBundleFact,
    build_projection_fact,
)


_MUTATION_CODEC = context_fingerprint(
    "canonical-mutation-inline-json-codec:v2",
    {"codec": "canonical-json", "ensure_ascii": True},
)
_MUTATION_CONTRACT = context_fingerprint(
    "canonical-mutation-payload-contract:v2",
    {"carrier": "inline_json", "surface_state": "external"},
)
CANONICAL_MUTATION_ORDERING_CONTRACT = context_fingerprint(
    "canonical-mutation-ordering-contract:v1",
    {"sequence_key_policy": "graph-semantic-fingerprint"},
)


def canonical_mutation_sequence_key(graph_id: str) -> str:
    return "graph:" + context_fingerprint(
        "canonical-mutation-sequence-key:v1",
        {"graph_id": graph_id},
    )


def default_projection_delivery_policy() -> DurableProjectionDeliveryPolicyFact:
    retry = cast(
        DurableProjectionRetryPolicyFact,
        build_projection_fact(
            DurableProjectionRetryPolicyFact,
            schema_version="durable_projection_retry_policy.v1",
            maximum_attempts=12,
            base_delay_milliseconds=1000,
            maximum_delay_milliseconds=300_000,
            lease_duration_seconds=180,
            claim_batch_size=32,
        ),
    )
    physical = cast(
        DurableProjectionPhysicalPolicyFact,
        build_projection_fact(
            DurableProjectionPhysicalPolicyFact,
            schema_version="durable_projection_physical_policy.v1",
            database_operation_timeout_seconds=10,
            source_hydration_timeout_seconds=20,
            handler_compute_timeout_seconds=30,
            result_commit_timeout_seconds=20,
            external_surface_attempt_timeout_seconds=60,
            maximum_physical_attempt_seconds=120,
        ),
    )
    return cast(
        DurableProjectionDeliveryPolicyFact,
        build_projection_fact(
            DurableProjectionDeliveryPolicyFact,
            schema_version="durable_projection_delivery_policy.v1",
            retry_policy=retry,
            physical_policy=physical,
        ),
    )


def build_surface_handler_contract(
    surface: CanonicalMutationSurface,
    *,
    target_compatibility_fingerprint: str | None = None,
) -> CanonicalMutationSurfaceHandlerContractFact:
    return cast(
        CanonicalMutationSurfaceHandlerContractFact,
        build_projection_fact(
            CanonicalMutationSurfaceHandlerContractFact,
            schema_version="canonical_mutation_surface_handler_contract.v1",
            surface=surface,
            handler_id=f"pulsara.{surface.value}-materializer",
            handler_version="2",
            accepted_mutation_kinds=tuple(CanonicalMutationKind),
            payload_codec_fingerprint=context_fingerprint(
                "canonical-mutation-surface-payload-codec:v2",
                {"surface": surface.value, "codec": _MUTATION_CODEC},
            ),
            target_compatibility_fingerprint=(
                target_compatibility_fingerprint
                or context_fingerprint(
                    "canonical-mutation-surface-target:v2",
                    {"surface": surface.value},
                )
            ),
            idempotency_contract_fingerprint=context_fingerprint(
                "canonical-mutation-surface-idempotency:v2",
                {
                    "surface": surface.value,
                    "identity": "delivery_identity_fingerprint",
                },
            ),
        ),
    )


def build_surface_plan(
    surfaces: tuple[CanonicalMutationSurface, ...],
    *,
    delivery_policy: DurableProjectionDeliveryPolicyFact | None = None,
    target_compatibility_fingerprints: dict[CanonicalMutationSurface, str]
    | None = None,
) -> CanonicalMutationSurfacePlanFact:
    if len(surfaces) != len(set(surfaces)):
        raise ValueError("canonical mutation surface plan contains duplicates")
    policy = delivery_policy or default_projection_delivery_policy()
    compatibilities = target_compatibility_fingerprints or {}
    planned = tuple(
        cast(
            CanonicalMutationPlannedSurfaceFact,
            build_projection_fact(
                CanonicalMutationPlannedSurfaceFact,
                schema_version="canonical_mutation_planned_surface.v1",
                handler_contract=build_surface_handler_contract(
                    surface,
                    target_compatibility_fingerprint=compatibilities.get(surface),
                ),
                delivery_policy=policy,
            ),
        )
        for surface in surfaces
    )
    return cast(
        CanonicalMutationSurfacePlanFact,
        build_projection_fact(
            CanonicalMutationSurfacePlanFact,
            schema_version="canonical_mutation_surface_plan.v1",
            ordered_surfaces=planned,
            composition_fingerprint=context_fingerprint(
                "canonical-mutation-surface-composition:v2",
                tuple(item.handler_contract.contract_fingerprint for item in planned),
            ),
        ),
    )


def build_canonical_mutation_bundle(
    *,
    source_owner: CanonicalMutationOwner,
    mutation_kind: CanonicalMutationKind,
    graph_id: str,
    payloads: tuple[dict[str, Any], ...],
    surface_plan: CanonicalMutationSurfacePlanFact,
    source_authority_fingerprints: tuple[str, ...] = (),
    mutation_ids: tuple[str, ...] | None = None,
) -> PreparedCanonicalMutationBundleFact:
    if mutation_ids is not None and len(mutation_ids) != len(payloads):
        raise ValueError("canonical mutation ID cardinality mismatch")
    semantics = canonical_mutation_semantics_for_payloads(
        mutation_kind=mutation_kind,
        graph_id=graph_id,
        payloads=payloads,
    )
    candidates: list[CanonicalMutationCandidateFact] = []
    requested_surfaces = tuple(
        item.handler_contract.surface for item in surface_plan.ordered_surfaces
    )
    for ordinal, semantic in enumerate(semantics):
        mutation_id = (
            mutation_ids[ordinal]
            if mutation_ids is not None
            else "canonical-mutation:"
            + context_fingerprint(
                "canonical-mutation-id:v2",
                {
                    "mutation_kind": mutation_kind.value,
                    "source_owner_fingerprint": source_owner.owner_fingerprint,
                    "mutation_ordinal": ordinal,
                    "graph_id": graph_id,
                    "mutation_semantic_fingerprint": (
                        semantic.mutation_semantic_fingerprint
                    ),
                },
            )
        )
        candidates.append(
            cast(
                CanonicalMutationCandidateFact,
                build_projection_fact(
                    CanonicalMutationCandidateFact,
                    schema_version="canonical_mutation_candidate.v2",
                    mutation_id=mutation_id,
                    mutation_ordinal=ordinal,
                    mutation_semantic=semantic,
                    source_owner_fingerprint=source_owner.owner_fingerprint,
                    source_authority_fingerprints=(source_authority_fingerprints),
                    requested_surfaces=requested_surfaces,
                    surface_plan_fingerprint=surface_plan.plan_fingerprint,
                ),
            )
        )
    return cast(
        PreparedCanonicalMutationBundleFact,
        build_projection_fact(
            PreparedCanonicalMutationBundleFact,
            schema_version="prepared_canonical_mutation_bundle.v1",
            source_owner=source_owner,
            surface_plan=surface_plan,
            ordered_mutation_candidates=tuple(candidates),
        ),
    )


def canonical_mutation_semantics_for_payloads(
    *,
    mutation_kind: CanonicalMutationKind,
    graph_id: str,
    payloads: tuple[dict[str, Any], ...],
) -> tuple[CanonicalMutationSemanticFact, ...]:
    """Freeze mutation semantics before the projection result owner exists."""

    semantics: list[CanonicalMutationSemanticFact] = []
    for payload in payloads:
        encoded = canonical_json_bytes(payload)
        content_sha = f"sha256:{sha256(encoded).hexdigest()}"
        carrier = cast(
            CanonicalInlineJsonDocumentFact,
            build_projection_fact(
                CanonicalInlineJsonDocumentFact,
                schema_version="canonical_inline_json_document.v1",
                carrier_kind="inline_json",
                canonical_json_utf8=encoded.decode("utf-8"),
                canonical_utf8_bytes=len(encoded),
                canonical_sha256=content_sha,
            ),
        )
        semantics.append(
            cast(
                CanonicalMutationSemanticFact,
                build_projection_fact(
                    CanonicalMutationSemanticFact,
                    schema_version="canonical_mutation_semantic.v2",
                    mutation_kind=mutation_kind,
                    graph_id=graph_id,
                    graph_document_semantic_fingerprint=(
                        carrier.document_semantic_fingerprint
                    ),
                    mutation_payload=carrier,
                    mutation_contract_fingerprint=_MUTATION_CONTRACT,
                ),
            )
        )
    return tuple(semantics)


def subset_surface_plan(
    plan: CanonicalMutationSurfacePlanFact,
    requested: tuple[CanonicalMutationSurface, ...],
) -> CanonicalMutationSurfacePlanFact:
    if not requested:
        return plan
    by_surface = {item.handler_contract.surface: item for item in plan.ordered_surfaces}
    try:
        selected = tuple(by_surface[surface] for surface in requested)
    except KeyError as error:
        raise ValueError("canonical mutation requested an unbound surface") from error
    return cast(
        CanonicalMutationSurfacePlanFact,
        build_projection_fact(
            CanonicalMutationSurfacePlanFact,
            schema_version="canonical_mutation_surface_plan.v1",
            ordered_surfaces=selected,
            composition_fingerprint=context_fingerprint(
                "canonical-mutation-surface-composition:v2",
                tuple(item.handler_contract.contract_fingerprint for item in selected),
            ),
        ),
    )


__all__ = [
    "CANONICAL_MUTATION_ORDERING_CONTRACT",
    "build_canonical_mutation_bundle",
    "build_surface_handler_contract",
    "build_surface_plan",
    "canonical_mutation_sequence_key",
    "canonical_mutation_semantics_for_payloads",
    "default_projection_delivery_policy",
    "subset_surface_plan",
]
