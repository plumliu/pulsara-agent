"""Canonical mutation V2 allocation and durable surface-delivery ownership."""

from __future__ import annotations

from typing import cast

from psycopg import Connection
from psycopg.types.json import Jsonb

from pulsara_agent.primitives._context_base import context_fingerprint
from pulsara_agent.runtime.projection_jobs.contracts import (
    CanonicalMutationAppendReceipt,
    CanonicalMutationCandidateFact,
    CanonicalMutationDocumentFact,
    CanonicalMutationOrderingFact,
    CanonicalMutationSequenceHeadFact,
    CanonicalMutationSurface,
    CanonicalMutationSurfaceDeliveryIdentityFact,
    CanonicalMutationSurfaceDeliveryStateFact,
    CanonicalMutationSurfacePlanFact,
    CanonicalMutationSurfaceSequenceHeadFact,
    DurableProjectionResultOwner,
    build_projection_fact,
)


_ORDERING_CONTRACT = context_fingerprint(
    "canonical-mutation-ordering-contract:v1",
    {"sequence_key_policy": "graph-semantic-fingerprint"},
)


def canonical_mutation_sequence_key(graph_id: str) -> str:
    return "graph:" + context_fingerprint(
        "canonical-mutation-sequence-key:v1",
        {"graph_id": graph_id},
    )


class PostgresCanonicalMutationRepository:
    """Transaction-local V2 allocator used by all durable mutation producers."""

    @staticmethod
    def append_candidates_in_transaction(
        connection: Connection,
        *,
        source_owner: DurableProjectionResultOwner,
        surface_plan: CanonicalMutationSurfacePlanFact,
        candidates: tuple[CanonicalMutationCandidateFact, ...],
    ) -> tuple[CanonicalMutationAppendReceipt, ...]:
        if tuple(item.mutation_ordinal for item in candidates) != tuple(
            range(len(candidates))
        ):
            raise ValueError("canonical mutation ordinals must be contiguous")
        planned_surfaces = tuple(
            item.handler_contract.surface
            for item in surface_plan.ordered_surfaces
        )
        if len(planned_surfaces) != len(set(planned_surfaces)):
            raise ValueError("canonical mutation surface plan has duplicates")
        for candidate in candidates:
            if (
                candidate.source_owner_fingerprint
                != source_owner.owner_fingerprint
                or candidate.surface_plan_fingerprint
                != surface_plan.plan_fingerprint
                or candidate.requested_surfaces != planned_surfaces
            ):
                raise ValueError("canonical mutation candidate owner/plan drifted")

        sequence_keys = tuple(
            sorted(
                {
                    canonical_mutation_sequence_key(
                        item.mutation_semantic.graph_id
                    )
                    for item in candidates
                }
            )
        )
        for sequence_key in sequence_keys:
            connection.execute(
                """
                SELECT pg_advisory_xact_lock(
                    hashtextextended(
                        'pulsara-canonical-mutation-sequence:' || %s,
                        0
                    )
                )
                """,
                (sequence_key,),
            ).fetchone()
        # Lock order is graph sequence first, then surface registry order.
        heads = {
            key: PostgresCanonicalMutationRepository._read_sequence_head(
                connection,
                key,
                lock=True,
            )
            for key in sequence_keys
        }
        surface_heads: dict[
            tuple[str, str], CanonicalMutationSurfaceSequenceHeadFact | None
        ] = {}
        for surface in planned_surfaces:
            for key in sequence_keys:
                surface_heads[(surface.value, key)] = (
                    PostgresCanonicalMutationRepository._read_surface_head(
                        connection,
                        surface.value,
                        key,
                        lock=True,
                    )
                )

        receipts: list[CanonicalMutationAppendReceipt] = []
        for candidate in candidates:
            existing = connection.execute(
                """
                SELECT mutation_payload, mutation_fact_fingerprint
                FROM canonical_mutations_v2 WHERE mutation_id = %s
                """,
                (candidate.mutation_id,),
            ).fetchone()
            if existing is not None:
                document = CanonicalMutationDocumentFact.model_validate(
                    _field(existing, "mutation_payload", 0)
                )
                if (
                    document.candidate != candidate
                    or document.mutation_fact_fingerprint
                    != str(_field(existing, "mutation_fact_fingerprint", 1))
                ):
                    raise ValueError("canonical mutation identity conflict")
                deliveries = PostgresCanonicalMutationRepository._delivery_fingerprints(
                    connection,
                    candidate.mutation_id,
                    ordered_surfaces=candidate.requested_surfaces,
                )
                receipts.append(
                    PostgresCanonicalMutationRepository._append_receipt(
                        candidate=candidate,
                        document=document,
                        disposition="exact_confirmed",
                        delivery_fingerprints=deliveries,
                    )
                )
                continue

            sequence_key = canonical_mutation_sequence_key(
                candidate.mutation_semantic.graph_id
            )
            previous = heads[sequence_key]
            sequence_number = (
                previous.last_mutation_sequence_number + 1
                if previous is not None
                else 1
            )
            ordering = cast(
                CanonicalMutationOrderingFact,
                build_projection_fact(
                    CanonicalMutationOrderingFact,
                    schema_version="canonical_mutation_ordering.v1",
                    sequence_key=sequence_key,
                    sequence_number=sequence_number,
                    predecessor_mutation_id=(
                        previous.last_mutation_id if previous else None
                    ),
                    predecessor_ordering_fingerprint=(
                        previous.last_ordering_fingerprint
                        if previous
                        else None
                    ),
                    ordering_contract_fingerprint=_ORDERING_CONTRACT,
                ),
            )
            document = cast(
                CanonicalMutationDocumentFact,
                build_projection_fact(
                    CanonicalMutationDocumentFact,
                    schema_version="canonical_mutation_document.v2",
                    candidate=candidate,
                    ordering=ordering,
                ),
            )
            connection.execute(
                """
                INSERT INTO canonical_mutations_v2 (
                    mutation_id, mutation_kind, graph_id, sequence_key,
                    mutation_sequence_number, mutation_payload,
                    mutation_semantic_fingerprint, mutation_fact_fingerprint
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    candidate.mutation_id,
                    candidate.mutation_semantic.mutation_kind.value,
                    candidate.mutation_semantic.graph_id,
                    sequence_key,
                    sequence_number,
                    Jsonb(document.model_dump(mode="json")),
                    (
                        candidate.mutation_semantic.mutation_semantic_fingerprint
                    ),
                    document.mutation_fact_fingerprint,
                ),
            )
            head = cast(
                CanonicalMutationSequenceHeadFact,
                build_projection_fact(
                    CanonicalMutationSequenceHeadFact,
                    schema_version="canonical_mutation_sequence_head.v1",
                    sequence_key=sequence_key,
                    last_mutation_sequence_number=sequence_number,
                    last_mutation_id=candidate.mutation_id,
                    last_ordering_fingerprint=ordering.ordering_fingerprint,
                    head_revision=previous.head_revision + 1 if previous else 1,
                ),
            )
            PostgresCanonicalMutationRepository._write_sequence_head(
                connection,
                previous=previous,
                resulting=head,
            )
            heads[sequence_key] = head

            delivery_fingerprints: list[str] = []
            for planned in surface_plan.ordered_surfaces:
                surface = planned.handler_contract.surface
                prior_surface = surface_heads[(surface.value, sequence_key)]
                surface_sequence = (
                    prior_surface.last_surface_sequence_number + 1
                    if prior_surface is not None
                    else 1
                )
                identity = cast(
                    CanonicalMutationSurfaceDeliveryIdentityFact,
                    build_projection_fact(
                        CanonicalMutationSurfaceDeliveryIdentityFact,
                        schema_version=(
                            "canonical_mutation_surface_delivery_identity.v1"
                        ),
                        mutation_id=candidate.mutation_id,
                        surface=surface,
                        mutation_semantic_fingerprint=(
                            candidate.mutation_semantic.mutation_semantic_fingerprint
                        ),
                        mutation_fact_fingerprint=(
                            document.mutation_fact_fingerprint
                        ),
                        mutation_ordering_fingerprint=(
                            ordering.ordering_fingerprint
                        ),
                        surface_sequence_number=surface_sequence,
                        predecessor_surface_delivery_identity_fingerprint=(
                            prior_surface.last_delivery_identity_fingerprint
                            if prior_surface
                            else None
                        ),
                        predecessor_surface_sequence_number=(
                            prior_surface.last_surface_sequence_number
                            if prior_surface
                            else None
                        ),
                        handler_contract=planned.handler_contract,
                    ),
                )
                state = cast(
                    CanonicalMutationSurfaceDeliveryStateFact,
                    build_projection_fact(
                        CanonicalMutationSurfaceDeliveryStateFact,
                        schema_version=(
                            "canonical_mutation_surface_delivery_state.v1"
                        ),
                        delivery_identity=identity,
                        delivery_policy=planned.delivery_policy,
                        status="pending",
                        state_revision=0,
                        repair_generation=0,
                        attempt_count=0,
                        lease_generation=0,
                        lease_owner_id=None,
                        lease_expires_at=None,
                        next_attempt_at=None,
                        terminal_receipt=None,
                        last_failure=None,
                    ),
                )
                connection.execute(
                    """
                    INSERT INTO canonical_mutation_surface_deliveries (
                        mutation_id, surface, sequence_key,
                        surface_sequence_number, delivery_identity,
                        delivery_identity_fingerprint, delivery_policy,
                        status, state_revision, repair_generation,
                        attempt_count, lease_generation, lease_owner_id,
                        lease_expires_at, next_attempt_at, terminal_receipt,
                        last_failure, state_fingerprint
                    ) VALUES (
                        %s, %s, %s, %s, %s, %s, %s,
                        %s, %s, %s, %s, %s, NULL, NULL, NULL, NULL, NULL, %s
                    )
                    """,
                    (
                        candidate.mutation_id,
                        surface.value,
                        sequence_key,
                        surface_sequence,
                        Jsonb(identity.model_dump(mode="json")),
                        identity.delivery_identity_fingerprint,
                        Jsonb(planned.delivery_policy.model_dump(mode="json")),
                        state.status,
                        state.state_revision,
                        state.repair_generation,
                        state.attempt_count,
                        state.lease_generation,
                        state.state_fingerprint,
                    ),
                )
                surface_head = cast(
                    CanonicalMutationSurfaceSequenceHeadFact,
                    build_projection_fact(
                        CanonicalMutationSurfaceSequenceHeadFact,
                        schema_version=(
                            "canonical_mutation_surface_sequence_head.v1"
                        ),
                        surface=surface,
                        sequence_key=sequence_key,
                        last_surface_sequence_number=surface_sequence,
                        last_mutation_sequence_number=sequence_number,
                        last_mutation_id=candidate.mutation_id,
                        last_delivery_identity_fingerprint=(
                            identity.delivery_identity_fingerprint
                        ),
                        head_revision=(
                            prior_surface.head_revision + 1
                            if prior_surface
                            else 1
                        ),
                    ),
                )
                PostgresCanonicalMutationRepository._write_surface_head(
                    connection,
                    previous=prior_surface,
                    resulting=surface_head,
                )
                surface_heads[(surface.value, sequence_key)] = surface_head
                delivery_fingerprints.append(
                    identity.delivery_identity_fingerprint
                )
            receipts.append(
                PostgresCanonicalMutationRepository._append_receipt(
                    candidate=candidate,
                    document=document,
                    disposition="inserted",
                    delivery_fingerprints=tuple(delivery_fingerprints),
                )
            )
        return tuple(receipts)

    @staticmethod
    def _read_sequence_head(
        connection: Connection,
        sequence_key: str,
        *,
        lock: bool,
    ) -> CanonicalMutationSequenceHeadFact | None:
        suffix = " FOR UPDATE" if lock else ""
        row = connection.execute(
            """
            SELECT head_payload, head_fingerprint
            FROM canonical_mutation_sequence_heads
            WHERE sequence_key = %s
            """
            + suffix,
            (sequence_key,),
        ).fetchone()
        if row is None:
            return None
        head = CanonicalMutationSequenceHeadFact.model_validate(
            _field(row, "head_payload", 0)
        )
        if head.head_fingerprint != str(_field(row, "head_fingerprint", 1)):
            raise ValueError("canonical mutation sequence head drifted")
        return head

    @staticmethod
    def _write_sequence_head(
        connection: Connection,
        *,
        previous: CanonicalMutationSequenceHeadFact | None,
        resulting: CanonicalMutationSequenceHeadFact,
    ) -> None:
        if previous is None:
            row = connection.execute(
                """
                INSERT INTO canonical_mutation_sequence_heads (
                    sequence_key, head_payload, head_fingerprint
                ) VALUES (%s, %s, %s)
                ON CONFLICT (sequence_key) DO NOTHING
                RETURNING head_fingerprint
                """,
                (
                    resulting.sequence_key,
                    Jsonb(resulting.model_dump(mode="json")),
                    resulting.head_fingerprint,
                ),
            ).fetchone()
        else:
            row = connection.execute(
                """
                UPDATE canonical_mutation_sequence_heads
                SET head_payload = %s, head_fingerprint = %s, updated_at = now()
                WHERE sequence_key = %s AND head_fingerprint = %s
                RETURNING head_fingerprint
                """,
                (
                    Jsonb(resulting.model_dump(mode="json")),
                    resulting.head_fingerprint,
                    resulting.sequence_key,
                    previous.head_fingerprint,
                ),
            ).fetchone()
        if row is None:
            raise ValueError("canonical mutation sequence head CAS failed")

    @staticmethod
    def _read_surface_head(
        connection: Connection,
        surface: str,
        sequence_key: str,
        *,
        lock: bool,
    ) -> CanonicalMutationSurfaceSequenceHeadFact | None:
        suffix = " FOR UPDATE" if lock else ""
        row = connection.execute(
            """
            SELECT head_payload, head_fingerprint
            FROM canonical_mutation_surface_sequence_heads
            WHERE surface = %s AND sequence_key = %s
            """
            + suffix,
            (surface, sequence_key),
        ).fetchone()
        if row is None:
            return None
        head = CanonicalMutationSurfaceSequenceHeadFact.model_validate(
            _field(row, "head_payload", 0)
        )
        if head.head_fingerprint != str(_field(row, "head_fingerprint", 1)):
            raise ValueError("canonical mutation surface head drifted")
        return head

    @staticmethod
    def _write_surface_head(
        connection: Connection,
        *,
        previous: CanonicalMutationSurfaceSequenceHeadFact | None,
        resulting: CanonicalMutationSurfaceSequenceHeadFact,
    ) -> None:
        if previous is None:
            row = connection.execute(
                """
                INSERT INTO canonical_mutation_surface_sequence_heads (
                    surface, sequence_key, head_payload, head_fingerprint
                ) VALUES (%s, %s, %s, %s)
                ON CONFLICT (surface, sequence_key) DO NOTHING
                RETURNING head_fingerprint
                """,
                (
                    resulting.surface.value,
                    resulting.sequence_key,
                    Jsonb(resulting.model_dump(mode="json")),
                    resulting.head_fingerprint,
                ),
            ).fetchone()
        else:
            row = connection.execute(
                """
                UPDATE canonical_mutation_surface_sequence_heads
                SET head_payload = %s, head_fingerprint = %s, updated_at = now()
                WHERE surface = %s AND sequence_key = %s
                  AND head_fingerprint = %s
                RETURNING head_fingerprint
                """,
                (
                    Jsonb(resulting.model_dump(mode="json")),
                    resulting.head_fingerprint,
                    resulting.surface.value,
                    resulting.sequence_key,
                    previous.head_fingerprint,
                ),
            ).fetchone()
        if row is None:
            raise ValueError("canonical mutation surface head CAS failed")

    @staticmethod
    def _delivery_fingerprints(
        connection: Connection,
        mutation_id: str,
        *,
        ordered_surfaces: tuple[CanonicalMutationSurface, ...],
    ) -> tuple[str, ...]:
        rows = connection.execute(
            """
            SELECT surface, delivery_identity_fingerprint
            FROM canonical_mutation_surface_deliveries
            WHERE mutation_id = %s
            """,
            (mutation_id,),
        ).fetchall()
        by_surface = {
            str(_field(row, "surface", 0)): str(
                _field(row, "delivery_identity_fingerprint", 1)
            )
            for row in rows
        }
        expected = tuple(surface.value for surface in ordered_surfaces)
        if set(by_surface) != set(expected):
            raise ValueError(
                "canonical mutation exact-confirm surface set drifted"
            )
        return tuple(by_surface[surface] for surface in expected)

    @staticmethod
    def _append_receipt(
        *,
        candidate: CanonicalMutationCandidateFact,
        document: CanonicalMutationDocumentFact,
        disposition: str,
        delivery_fingerprints: tuple[str, ...],
    ) -> CanonicalMutationAppendReceipt:
        return cast(
            CanonicalMutationAppendReceipt,
            build_projection_fact(
                CanonicalMutationAppendReceipt,
                schema_version="canonical_mutation_append_receipt.v1",
                mutation_id=candidate.mutation_id,
                mutation_semantic_fingerprint=(
                    candidate.mutation_semantic.mutation_semantic_fingerprint
                ),
                append_disposition=disposition,
                mutation_fact_fingerprint=document.mutation_fact_fingerprint,
                ordering_fingerprint=document.ordering.ordering_fingerprint,
                ordered_surface_delivery_identity_fingerprints=(
                    delivery_fingerprints
                ),
            ),
        )


def _field(row: object, name: str, index: int) -> object:
    if isinstance(row, dict):
        return row[name]
    return row[index]  # type: ignore[index]


__all__ = [
    "PostgresCanonicalMutationRepository",
    "canonical_mutation_sequence_key",
]
