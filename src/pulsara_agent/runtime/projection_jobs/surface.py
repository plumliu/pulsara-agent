"""Unified lease and settlement owner for canonical-mutation V2 surfaces."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from time import monotonic
from typing import Protocol, cast

from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from pulsara_agent.primitives._context_base import context_fingerprint
from pulsara_agent.primitives.runtime_event_vocabulary import (
    build_bounded_runtime_failure_diagnostic,
)
from pulsara_agent.runtime.projection_jobs.contracts import (
    CanonicalMutationDocumentFact,
    CanonicalMutationSurface,
    CanonicalMutationSurfaceDecommissionedReceiptFact,
    CanonicalMutationSurfaceDeliveryStateFact,
    CanonicalMutationSurfaceRepairActionFact,
    CanonicalMutationSurfaceTargetHeadFact,
    ConfirmedCanonicalMutationSurfaceAppliedReceiptFact,
    DurableProjectionAppliedResultReceiptFact,
    DurableProjectionResultReceiptReferenceFact,
    DurableRepairAuthorityReferenceFact,
    DurableProjectionFailureKind,
    LeasedCanonicalMutationSurfaceDeliveryFact,
    build_projection_fact,
    durable_result_receipt_reference,
)
from pulsara_agent.storage.postgres_connection_provider import (
    PostgresConnectionLane,
    VerifiedPostgresConnectionProviderProtocol,
)


@dataclass(frozen=True, slots=True)
class BoundCanonicalMutationSurfaceDelivery:
    lease: LeasedCanonicalMutationSurfaceDeliveryFact
    mutation: CanonicalMutationDocumentFact


class CanonicalMutationSurfaceHandler(Protocol):
    surface: CanonicalMutationSurface

    def apply(
        self,
        delivery: BoundCanonicalMutationSurfaceDelivery,
        *,
        deadline_monotonic: float,
    ) -> tuple[str, str]: ...


@dataclass(slots=True)
class PostgresCanonicalMutationSurfaceRepository:
    connection_provider: VerifiedPostgresConnectionProviderProtocol

    def claim_due(
        self,
        *,
        surface: CanonicalMutationSurface,
        owner_id: str,
        limit: int,
        deadline_monotonic: float,
    ) -> tuple[BoundCanonicalMutationSurfaceDelivery, ...]:
        if limit < 1 or limit > 32:
            raise ValueError("surface claim limit must be between 1 and 32")
        now = datetime.now(timezone.utc)
        with self.connection_provider.connection(
            lane=PostgresConnectionLane.PROJECTION_MAINTENANCE,
            row_factory=dict_row,
            deadline_monotonic=deadline_monotonic,
        ) as connection:
            self._set_deadline(connection, deadline_monotonic)
            expired = tuple(
                connection.execute(
                    """
                    SELECT mutation_id, delivery_identity, delivery_policy,
                           status, state_revision, repair_generation,
                           attempt_count, lease_generation, lease_owner_id,
                           lease_expires_at, next_attempt_at, terminal_receipt,
                           last_failure, state_fingerprint
                    FROM canonical_mutation_surface_deliveries
                    WHERE surface = %s AND status = 'leased'
                      AND lease_expires_at <= %s
                    FOR UPDATE
                    """,
                    (surface.value, now),
                ).fetchall()
            )
            for row in expired:
                current = self._state_from_row(row)
                state = cast(
                    CanonicalMutationSurfaceDeliveryStateFact,
                    build_projection_fact(
                        CanonicalMutationSurfaceDeliveryStateFact,
                        schema_version=(
                            "canonical_mutation_surface_delivery_state.v1"
                        ),
                        delivery_identity=current.delivery_identity,
                        delivery_policy=current.delivery_policy,
                        status="pending",
                        state_revision=current.state_revision + 1,
                        repair_generation=current.repair_generation,
                        attempt_count=current.attempt_count,
                        lease_generation=current.lease_generation,
                        lease_owner_id=None,
                        lease_expires_at=None,
                        next_attempt_at=None,
                        terminal_receipt=None,
                        last_failure=current.last_failure,
                    ),
                )
                self._write_state(
                    connection,
                    mutation_id=str(row["mutation_id"]),
                    surface=surface,
                    state=state,
                )
            rows = tuple(
                connection.execute(
                    """
                    SELECT d.*, m.mutation_payload
                    FROM canonical_mutation_surface_deliveries AS d
                    JOIN canonical_mutations_v2 AS m
                      ON m.mutation_id = d.mutation_id
                    WHERE d.surface = %s
                      AND (
                        d.status = 'pending'
                        OR (d.status = 'retry_wait' AND d.next_attempt_at <= %s)
                      )
                      AND NOT EXISTS (
                        SELECT 1
                        FROM canonical_mutation_surface_deliveries AS prior
                        WHERE prior.surface = d.surface
                          AND prior.sequence_key = d.sequence_key
                          AND prior.surface_sequence_number
                              < d.surface_sequence_number
                          AND prior.status NOT IN ('applied', 'decommissioned')
                      )
                    ORDER BY d.sequence_key, d.surface_sequence_number
                    LIMIT %s
                    FOR UPDATE OF d SKIP LOCKED
                    """,
                    (surface.value, now, limit),
                ).fetchall()
            )
            claimed: list[BoundCanonicalMutationSurfaceDelivery] = []
            for row in rows:
                current = self._state_from_row(row)
                identity = current.delivery_identity
                if identity.surface is not surface:
                    raise ValueError("surface delivery identity drifted")
                expires = now + timedelta(
                    seconds=current.delivery_policy.retry_policy.lease_duration_seconds
                )
                state = cast(
                    CanonicalMutationSurfaceDeliveryStateFact,
                    build_projection_fact(
                        CanonicalMutationSurfaceDeliveryStateFact,
                        schema_version=(
                            "canonical_mutation_surface_delivery_state.v1"
                        ),
                        delivery_identity=identity,
                        delivery_policy=current.delivery_policy,
                        status="leased",
                        state_revision=current.state_revision + 1,
                        repair_generation=current.repair_generation,
                        attempt_count=current.attempt_count + 1,
                        lease_generation=current.lease_generation + 1,
                        lease_owner_id=owner_id,
                        lease_expires_at=expires,
                        next_attempt_at=None,
                        terminal_receipt=None,
                        last_failure=current.last_failure,
                    ),
                )
                self._write_state(
                    connection,
                    mutation_id=identity.mutation_id,
                    surface=surface,
                    state=state,
                )
                lease = cast(
                    LeasedCanonicalMutationSurfaceDeliveryFact,
                    build_projection_fact(
                        LeasedCanonicalMutationSurfaceDeliveryFact,
                        schema_version=(
                            "leased_canonical_mutation_surface_delivery.v1"
                        ),
                        delivery_identity=identity,
                        delivery_policy=state.delivery_policy,
                        expected_state_revision=state.state_revision,
                        repair_generation=state.repair_generation,
                        attempt_count=state.attempt_count,
                        lease_generation=state.lease_generation,
                        lease_owner_id=owner_id,
                        lease_expires_at=expires,
                    ),
                )
                claimed.append(
                    BoundCanonicalMutationSurfaceDelivery(
                        lease=lease,
                        mutation=CanonicalMutationDocumentFact.model_validate(
                            row["mutation_payload"]
                        ),
                    )
                )
        return tuple(claimed)

    def settle_applied(
        self,
        *,
        delivery: BoundCanonicalMutationSurfaceDelivery,
        target_semantic_identity: str,
        applied_document_semantic_fingerprint: str,
        deadline_monotonic: float,
    ) -> CanonicalMutationSurfaceDeliveryStateFact:
        lease = delivery.lease
        identity = lease.delivery_identity
        receipt = cast(
            ConfirmedCanonicalMutationSurfaceAppliedReceiptFact,
            build_projection_fact(
                ConfirmedCanonicalMutationSurfaceAppliedReceiptFact,
                schema_version=(
                    "confirmed_canonical_mutation_surface_applied_receipt.v1"
                ),
                receipt_kind="confirmed_applied",
                mutation_id=identity.mutation_id,
                surface=identity.surface,
                mutation_semantic_fingerprint=(
                    identity.mutation_semantic_fingerprint
                ),
                delivery_identity_fingerprint=(
                    identity.delivery_identity_fingerprint
                ),
                target_semantic_identity=target_semantic_identity,
                applied_document_semantic_fingerprint=(
                    applied_document_semantic_fingerprint
                ),
                surface_handler_contract_fingerprint=(
                    identity.handler_contract.contract_fingerprint
                ),
            ),
        )
        with self.connection_provider.connection(
            lane=PostgresConnectionLane.PROJECTION_MAINTENANCE,
            row_factory=dict_row,
            deadline_monotonic=deadline_monotonic,
        ) as connection:
            self._set_deadline(connection, deadline_monotonic)
            current = self._read_exact_lease(connection, lease)
            prior_head = self._read_target_head(
                connection,
                surface=identity.surface,
                sequence_key=delivery.mutation.ordering.sequence_key,
                lock=True,
            )
            expected_prior = identity.predecessor_surface_sequence_number
            if expected_prior is None:
                if prior_head is not None or identity.surface_sequence_number != 1:
                    raise ValueError("first surface delivery predecessor drifted")
            else:
                prior_delivery = self._read_terminal_surface_predecessor(
                    connection,
                    surface=identity.surface,
                    sequence_key=delivery.mutation.ordering.sequence_key,
                    surface_sequence_number=expected_prior,
                )
                if (
                    prior_head is None
                    or prior_head.terminal_surface_sequence_number
                    != expected_prior
                    or prior_head.terminal_mutation_id
                    != prior_delivery.delivery_identity.mutation_id
                    or prior_head.terminal_mutation_semantic_fingerprint
                    != prior_delivery.delivery_identity.mutation_semantic_fingerprint
                    or identity.predecessor_surface_delivery_identity_fingerprint
                    != prior_delivery.delivery_identity.delivery_identity_fingerprint
                ):
                    raise ValueError(
                        "surface predecessor target head is not terminal"
                    )
            state = cast(
                CanonicalMutationSurfaceDeliveryStateFact,
                build_projection_fact(
                    CanonicalMutationSurfaceDeliveryStateFact,
                    schema_version=(
                        "canonical_mutation_surface_delivery_state.v1"
                    ),
                    delivery_identity=identity,
                    delivery_policy=current.delivery_policy,
                    status="applied",
                    state_revision=current.state_revision + 1,
                    repair_generation=current.repair_generation,
                    attempt_count=current.attempt_count,
                    lease_generation=current.lease_generation,
                    lease_owner_id=None,
                    lease_expires_at=None,
                    next_attempt_at=None,
                    terminal_receipt=receipt,
                    last_failure=None,
                ),
            )
            self._write_state(
                connection,
                mutation_id=identity.mutation_id,
                surface=identity.surface,
                state=state,
            )
            head = cast(
                CanonicalMutationSurfaceTargetHeadFact,
                build_projection_fact(
                    CanonicalMutationSurfaceTargetHeadFact,
                    schema_version=(
                        "canonical_mutation_surface_target_head.v1"
                    ),
                    surface=identity.surface,
                    sequence_key=delivery.mutation.ordering.sequence_key,
                    terminal_surface_sequence_number=(
                        identity.surface_sequence_number
                    ),
                    terminal_mutation_sequence_number=(
                        delivery.mutation.ordering.sequence_number
                    ),
                    terminal_mutation_id=identity.mutation_id,
                    terminal_mutation_semantic_fingerprint=(
                        identity.mutation_semantic_fingerprint
                    ),
                    terminal_disposition="applied",
                    terminal_receipt_fingerprint=receipt.receipt_fingerprint,
                    head_revision=prior_head.head_revision + 1
                    if prior_head
                    else 1,
                ),
            )
            self._write_target_head(
                connection, previous=prior_head, resulting=head
            )
        return state

    def settle_failure(
        self,
        *,
        delivery: BoundCanonicalMutationSurfaceDelivery,
        failure_kind: DurableProjectionFailureKind,
        error: BaseException,
        deadline_monotonic: float,
    ) -> CanonicalMutationSurfaceDeliveryStateFact:
        lease = delivery.lease
        with self.connection_provider.connection(
            lane=PostgresConnectionLane.PROJECTION_MAINTENANCE,
            row_factory=dict_row,
            deadline_monotonic=deadline_monotonic,
        ) as connection:
            self._set_deadline(connection, deadline_monotonic)
            current = self._read_exact_lease(connection, lease)
            retry = current.attempt_count < (
                current.delivery_policy.retry_policy.maximum_attempts
            )
            diagnostic = build_bounded_runtime_failure_diagnostic(
                error=error,
                redaction_profile_id=(
                    "canonical_mutation_surface_delivery_error.v1"
                ),
            )
            next_at = None
            if retry:
                policy = current.delivery_policy.retry_policy
                delay = min(
                    policy.maximum_delay_milliseconds,
                    policy.base_delay_milliseconds
                    * (2 ** max(0, current.attempt_count - 1)),
                )
                next_at = datetime.now(timezone.utc) + timedelta(
                    milliseconds=delay
                )
            state = cast(
                CanonicalMutationSurfaceDeliveryStateFact,
                build_projection_fact(
                    CanonicalMutationSurfaceDeliveryStateFact,
                    schema_version=(
                        "canonical_mutation_surface_delivery_state.v1"
                    ),
                    delivery_identity=current.delivery_identity,
                    delivery_policy=current.delivery_policy,
                    status="retry_wait" if retry else "dead_letter",
                    state_revision=current.state_revision + 1,
                    repair_generation=current.repair_generation,
                    attempt_count=current.attempt_count,
                    lease_generation=current.lease_generation,
                    lease_owner_id=None,
                    lease_expires_at=None,
                    next_attempt_at=next_at,
                    terminal_receipt=None,
                    last_failure=diagnostic,
                ),
            )
            self._write_state(
                connection,
                mutation_id=lease.delivery_identity.mutation_id,
                surface=lease.delivery_identity.surface,
                state=state,
            )
        return state

    def repair_dead_letter(
        self,
        *,
        mutation_id: str,
        surface: CanonicalMutationSurface,
        action: str,
        operator_authority_id: str,
        rebuild_result_receipt_id: str | None = None,
        deadline_monotonic: float,
    ) -> CanonicalMutationSurfaceRepairActionFact:
        """Retry or terminally decommission one exact dead-letter delivery."""

        allowed_actions = {
            "retry_same_contract",
            "decommission_with_authority",
            "decommission_after_rebuild",
        }
        if action not in allowed_actions:
            raise ValueError("unknown canonical mutation surface repair action")
        if not mutation_id.strip() or not operator_authority_id.strip():
            raise ValueError("surface repair identity must be non-empty")
        if (action == "decommission_after_rebuild") != (
            rebuild_result_receipt_id is not None
        ):
            raise ValueError(
                "surface rebuild decommission requires one durable rebuild receipt"
            )
        with self.connection_provider.connection(
            lane=PostgresConnectionLane.PROJECTION_MAINTENANCE,
            row_factory=dict_row,
            deadline_monotonic=deadline_monotonic,
        ) as connection:
            self._set_deadline(connection, deadline_monotonic)
            row = connection.execute(
                """
                SELECT d.*, m.mutation_payload
                FROM canonical_mutation_surface_deliveries AS d
                JOIN canonical_mutations_v2 AS m
                  ON m.mutation_id = d.mutation_id
                WHERE d.mutation_id = %s AND d.surface = %s
                FOR UPDATE OF d
                """,
                (mutation_id, surface.value),
            ).fetchone()
            if row is None:
                raise KeyError(f"{mutation_id}:{surface.value}")
            state = self._state_from_row(row)
            mutation = CanonicalMutationDocumentFact.model_validate(
                row["mutation_payload"]
            )
            rebuild_receipt_reference: (
                DurableProjectionResultReceiptReferenceFact | None
            ) = None
            replacement_surface_identity_fingerprint: str | None = None
            if rebuild_result_receipt_id is not None:
                (
                    rebuild_receipt_reference,
                    replacement_surface_identity_fingerprint,
                ) = self._resolve_rebuild_receipt(
                    connection,
                    receipt_id=rebuild_result_receipt_id,
                    surface=surface,
                    sequence_key=mutation.ordering.sequence_key,
                    expected_handler_contract_fingerprint=(
                        state.delivery_identity.handler_contract.contract_fingerprint
                    ),
                )
            owner_id = f"{mutation_id}:{surface.value}"
            latest_row = connection.execute(
                """
                SELECT action_payload, action_fingerprint
                FROM durable_projection_repair_actions
                WHERE owner_kind = 'canonical_mutation_surface'
                  AND owner_id = %s
                ORDER BY repair_generation DESC
                LIMIT 1
                """,
                (owner_id,),
            ).fetchone()
            if latest_row is not None and state.status in {
                "pending",
                "decommissioned",
            }:
                latest = CanonicalMutationSurfaceRepairActionFact.model_validate(
                    latest_row["action_payload"]
                )
                if (
                    latest.action_fingerprint
                    != str(latest_row["action_fingerprint"])
                    or latest.resulting_repair_generation
                    != state.repair_generation
                ):
                    raise ValueError("surface repair lineage drifted")
                operator_authorities = tuple(
                    item.authority_id
                    for item in latest.authority_references
                    if item.authority_kind == "operator_command"
                )
                if (
                    latest.action == action
                    and operator_authorities == (operator_authority_id,)
                    and latest.rebuild_result_receipt_reference
                    == rebuild_receipt_reference
                ):
                    return latest
            if state.status != "dead_letter":
                raise ValueError(
                    "surface repair requires an exact dead-letter delivery"
                )
            prior_head = self._read_target_head(
                connection,
                surface=surface,
                sequence_key=mutation.ordering.sequence_key,
                lock=True,
            )
            resulting_generation = state.repair_generation + 1
            authority_semantic_fingerprint = context_fingerprint(
                "canonical-mutation-surface-repair-authority:v1",
                {
                    "mutation_id": mutation_id,
                    "surface": surface.value,
                    "action": action,
                    "operator_authority_id": operator_authority_id,
                    "rebuild_result_receipt_reference_fingerprint": (
                        rebuild_receipt_reference.reference_fingerprint
                        if rebuild_receipt_reference is not None
                        else None
                    ),
                    "expected_state_fingerprint": state.state_fingerprint,
                },
            )
            authority = cast(
                DurableRepairAuthorityReferenceFact,
                build_projection_fact(
                    DurableRepairAuthorityReferenceFact,
                    schema_version="durable_repair_authority_reference.v1",
                    authority_kind="operator_command",
                    authority_id=operator_authority_id,
                    authority_semantic_fingerprint=(
                        authority_semantic_fingerprint
                    ),
                ),
            )
            authorities = (authority,)
            if rebuild_receipt_reference is not None:
                rebuild_authority = cast(
                    DurableRepairAuthorityReferenceFact,
                    build_projection_fact(
                        DurableRepairAuthorityReferenceFact,
                        schema_version=(
                            "durable_repair_authority_reference.v1"
                        ),
                        authority_kind="projection_rebuild",
                        authority_id=rebuild_receipt_reference.receipt_id,
                        authority_semantic_fingerprint=(
                            rebuild_receipt_reference.receipt_fingerprint
                        ),
                    ),
                )
                authorities = (authority, rebuild_authority)
            action_id = "surface-repair:" + context_fingerprint(
                "canonical-mutation-surface-repair-action-id:v1",
                {
                    "delivery_identity_fingerprint": (
                        state.delivery_identity.delivery_identity_fingerprint
                    ),
                    "expected_state_revision": state.state_revision,
                    "expected_surface_head_fingerprint": (
                        prior_head.head_fingerprint
                        if prior_head is not None
                        else None
                    ),
                    "expected_repair_generation": state.repair_generation,
                    "action": action,
                    "authority_reference": authority.reference_fingerprint,
                },
            )
            requested_at = connection.execute(
                "SELECT clock_timestamp() AS requested_at"
            ).fetchone()["requested_at"]
            repair = cast(
                CanonicalMutationSurfaceRepairActionFact,
                build_projection_fact(
                    CanonicalMutationSurfaceRepairActionFact,
                    schema_version=(
                        "canonical_mutation_surface_repair_action.v1"
                    ),
                    repair_action_id=action_id,
                    delivery_identity_fingerprint=(
                        state.delivery_identity.delivery_identity_fingerprint
                    ),
                    expected_state_revision=state.state_revision,
                    expected_surface_head_fingerprint=(
                        prior_head.head_fingerprint
                        if prior_head is not None
                        else None
                    ),
                    expected_repair_generation=state.repair_generation,
                    action=action,
                    authority_references=authorities,
                    rebuild_result_receipt_reference=(
                        rebuild_receipt_reference
                    ),
                    resulting_repair_generation=resulting_generation,
                    requested_at=requested_at,
                ),
            )
            connection.execute(
                """
                INSERT INTO durable_projection_repair_actions (
                    repair_action_id, owner_kind, owner_id,
                    repair_generation, action_payload, action_fingerprint
                ) VALUES (
                    %s, 'canonical_mutation_surface', %s, %s, %s, %s
                )
                """,
                (
                    repair.repair_action_id,
                    owner_id,
                    repair.resulting_repair_generation,
                    Jsonb(repair.model_dump(mode="json")),
                    repair.action_fingerprint,
                ),
            )
            if action == "retry_same_contract":
                resulting_state = cast(
                    CanonicalMutationSurfaceDeliveryStateFact,
                    build_projection_fact(
                        CanonicalMutationSurfaceDeliveryStateFact,
                        schema_version=(
                            "canonical_mutation_surface_delivery_state.v1"
                        ),
                        delivery_identity=state.delivery_identity,
                        delivery_policy=state.delivery_policy,
                        status="pending",
                        state_revision=state.state_revision + 1,
                        repair_generation=resulting_generation,
                        attempt_count=0,
                        lease_generation=state.lease_generation,
                        lease_owner_id=None,
                        lease_expires_at=None,
                        next_attempt_at=None,
                        terminal_receipt=None,
                        last_failure=None,
                    ),
                )
                self._write_state(
                    connection,
                    mutation_id=mutation_id,
                    surface=surface,
                    state=resulting_state,
                )
                return repair

            self._validate_terminal_predecessor(
                connection,
                identity=state.delivery_identity,
                mutation=mutation,
                prior_head=prior_head,
            )
            receipt = cast(
                CanonicalMutationSurfaceDecommissionedReceiptFact,
                build_projection_fact(
                    CanonicalMutationSurfaceDecommissionedReceiptFact,
                    schema_version=(
                        "canonical_mutation_surface_decommissioned_receipt.v1"
                    ),
                    receipt_kind="decommissioned",
                    mutation_id=mutation_id,
                    surface=surface,
                    delivery_identity_fingerprint=(
                        state.delivery_identity.delivery_identity_fingerprint
                    ),
                    decommission_reason=(
                        "superseded_by_rebuild"
                        if action == "decommission_after_rebuild"
                        else "operator_decommission"
                    ),
                    repair_action_fingerprint=repair.action_fingerprint,
                    replacement_surface_identity_fingerprint=(
                        replacement_surface_identity_fingerprint
                    ),
                ),
            )
            resulting_state = cast(
                CanonicalMutationSurfaceDeliveryStateFact,
                build_projection_fact(
                    CanonicalMutationSurfaceDeliveryStateFact,
                    schema_version=(
                        "canonical_mutation_surface_delivery_state.v1"
                    ),
                    delivery_identity=state.delivery_identity,
                    delivery_policy=state.delivery_policy,
                    status="decommissioned",
                    state_revision=state.state_revision + 1,
                    repair_generation=resulting_generation,
                    attempt_count=state.attempt_count,
                    lease_generation=state.lease_generation,
                    lease_owner_id=None,
                    lease_expires_at=None,
                    next_attempt_at=None,
                    terminal_receipt=receipt,
                    last_failure=state.last_failure,
                ),
            )
            self._write_state(
                connection,
                mutation_id=mutation_id,
                surface=surface,
                state=resulting_state,
            )
            head = cast(
                CanonicalMutationSurfaceTargetHeadFact,
                build_projection_fact(
                    CanonicalMutationSurfaceTargetHeadFact,
                    schema_version=(
                        "canonical_mutation_surface_target_head.v1"
                    ),
                    surface=surface,
                    sequence_key=mutation.ordering.sequence_key,
                    terminal_surface_sequence_number=(
                        state.delivery_identity.surface_sequence_number
                    ),
                    terminal_mutation_sequence_number=(
                        mutation.ordering.sequence_number
                    ),
                    terminal_mutation_id=mutation_id,
                    terminal_mutation_semantic_fingerprint=(
                        state.delivery_identity.mutation_semantic_fingerprint
                    ),
                    terminal_disposition="decommissioned",
                    terminal_receipt_fingerprint=receipt.receipt_fingerprint,
                    head_revision=(
                        prior_head.head_revision + 1
                        if prior_head is not None
                        else 1
                    ),
                ),
            )
            self._write_target_head(
                connection,
                previous=prior_head,
                resulting=head,
            )
            return repair

    def _resolve_rebuild_receipt(
        self,
        connection,
        *,
        receipt_id: str,
        surface: CanonicalMutationSurface,
        sequence_key: str,
        expected_handler_contract_fingerprint: str,
    ) -> tuple[DurableProjectionResultReceiptReferenceFact, str]:
        row = connection.execute(
            """
            SELECT receipt_kind, receipt_payload, receipt_fingerprint
            FROM durable_projection_result_receipts
            WHERE receipt_id = %s
            """,
            (receipt_id,),
        ).fetchone()
        if row is None or str(row["receipt_kind"]) != "applied":
            raise ValueError("surface rebuild receipt is not durable FULL")
        receipt = DurableProjectionAppliedResultReceiptFact.model_validate(
            row["receipt_payload"]
        )
        if (
            receipt.receipt_id != receipt_id
            or receipt.receipt_fingerprint
            != str(row["receipt_fingerprint"])
        ):
            raise ValueError("surface rebuild receipt identity drifted")
        matching_target_identities: set[str] = set()
        for mutation_reference in receipt.canonical_mutation_references:
            delivery_row = connection.execute(
                """
                SELECT d.*, m.mutation_payload
                FROM canonical_mutation_surface_deliveries AS d
                JOIN canonical_mutations_v2 AS m
                  ON m.mutation_id = d.mutation_id
                WHERE d.mutation_id = %s
                  AND d.surface = %s
                  AND d.sequence_key = %s
                """,
                (
                    mutation_reference.mutation_id,
                    surface.value,
                    sequence_key,
                ),
            ).fetchone()
            if delivery_row is None:
                continue
            mutation = CanonicalMutationDocumentFact.model_validate(
                delivery_row["mutation_payload"]
            )
            state = self._state_from_row(delivery_row)
            if (
                mutation.candidate.mutation_semantic.mutation_semantic_fingerprint
                != mutation_reference.mutation_semantic_fingerprint
                or state.delivery_identity.delivery_identity_fingerprint
                not in mutation_reference.ordered_surface_delivery_identity_fingerprints
                or state.status != "applied"
                or not isinstance(
                    state.terminal_receipt,
                    ConfirmedCanonicalMutationSurfaceAppliedReceiptFact,
                )
                or state.terminal_receipt.delivery_identity_fingerprint
                != state.delivery_identity.delivery_identity_fingerprint
                or state.terminal_receipt.surface_handler_contract_fingerprint
                != expected_handler_contract_fingerprint
                or state.delivery_identity.handler_contract.contract_fingerprint
                != expected_handler_contract_fingerprint
            ):
                raise ValueError(
                    "surface rebuild receipt does not join an exact applied delivery"
                )
            matching_target_identities.add(
                state.terminal_receipt.target_semantic_identity
            )
        if len(matching_target_identities) != 1:
            raise ValueError(
                "surface rebuild receipt does not prove one replacement target"
            )
        return (
            durable_result_receipt_reference(receipt),
            next(iter(matching_target_identities)),
        )

    def _validate_terminal_predecessor(
        self,
        connection,
        *,
        identity,
        mutation: CanonicalMutationDocumentFact,
        prior_head: CanonicalMutationSurfaceTargetHeadFact | None,
    ) -> None:
        expected_prior = identity.predecessor_surface_sequence_number
        if expected_prior is None:
            if prior_head is not None or identity.surface_sequence_number != 1:
                raise ValueError("first surface decommission predecessor drifted")
            return
        prior_delivery = self._read_terminal_surface_predecessor(
            connection,
            surface=identity.surface,
            sequence_key=mutation.ordering.sequence_key,
            surface_sequence_number=expected_prior,
        )
        if (
            prior_head is None
            or prior_head.terminal_surface_sequence_number != expected_prior
            or prior_head.terminal_mutation_id
            != prior_delivery.delivery_identity.mutation_id
            or identity.predecessor_surface_delivery_identity_fingerprint
            != prior_delivery.delivery_identity.delivery_identity_fingerprint
        ):
            raise ValueError("surface decommission predecessor head drifted")

    @staticmethod
    def _state_from_row(row) -> CanonicalMutationSurfaceDeliveryStateFact:
        return CanonicalMutationSurfaceDeliveryStateFact.model_validate(
            {
                "schema_version": (
                    "canonical_mutation_surface_delivery_state.v1"
                ),
                "delivery_identity": row["delivery_identity"],
                "delivery_policy": row["delivery_policy"],
                "status": str(row["status"]),
                "state_revision": int(row["state_revision"]),
                "repair_generation": int(row["repair_generation"]),
                "attempt_count": int(row["attempt_count"]),
                "lease_generation": int(row["lease_generation"]),
                "lease_owner_id": row["lease_owner_id"],
                "lease_expires_at": _utc(row["lease_expires_at"]),
                "next_attempt_at": _utc(row["next_attempt_at"]),
                "terminal_receipt": row["terminal_receipt"],
                "last_failure": row["last_failure"],
                "state_fingerprint": str(row["state_fingerprint"]),
            }
        )

    def _read_exact_lease(
        self, connection, lease: LeasedCanonicalMutationSurfaceDeliveryFact
    ) -> CanonicalMutationSurfaceDeliveryStateFact:
        row = connection.execute(
            """
            SELECT delivery_identity, delivery_policy, status,
                   state_revision, repair_generation, attempt_count,
                   lease_generation, lease_owner_id, lease_expires_at,
                   next_attempt_at, terminal_receipt, last_failure,
                   state_fingerprint
            FROM canonical_mutation_surface_deliveries
            WHERE mutation_id = %s AND surface = %s
            FOR UPDATE
            """,
            (
                lease.delivery_identity.mutation_id,
                lease.delivery_identity.surface.value,
            ),
        ).fetchone()
        if row is None:
            raise ValueError("canonical mutation surface lease is absent")
        state = self._state_from_row(row)
        if (
            state.status != "leased"
            or state.delivery_identity != lease.delivery_identity
            or state.state_revision != lease.expected_state_revision
            or state.repair_generation != lease.repair_generation
            or state.attempt_count != lease.attempt_count
            or state.lease_generation != lease.lease_generation
            or state.lease_owner_id != lease.lease_owner_id
            or state.lease_expires_at != lease.lease_expires_at
        ):
            raise ValueError("canonical mutation surface lease is stale")
        return state

    @staticmethod
    def _read_terminal_surface_predecessor(
        connection,
        *,
        surface: CanonicalMutationSurface,
        sequence_key: str,
        surface_sequence_number: int,
    ) -> CanonicalMutationSurfaceDeliveryStateFact:
        row = connection.execute(
            """
            SELECT delivery_identity, delivery_policy, status,
                   state_revision, repair_generation, attempt_count,
                   lease_generation, lease_owner_id, lease_expires_at,
                   next_attempt_at, terminal_receipt, last_failure,
                   state_fingerprint
            FROM canonical_mutation_surface_deliveries
            WHERE surface = %s AND sequence_key = %s
              AND surface_sequence_number = %s
            FOR UPDATE
            """,
            (surface.value, sequence_key, surface_sequence_number),
        ).fetchone()
        if row is None:
            raise ValueError("surface predecessor delivery is absent")
        state = PostgresCanonicalMutationSurfaceRepository._state_from_row(row)
        if state.status not in {"applied", "decommissioned"}:
            raise ValueError("surface predecessor delivery is not terminal")
        return state

    @staticmethod
    def _write_state(
        connection,
        *,
        mutation_id: str,
        surface: CanonicalMutationSurface,
        state: CanonicalMutationSurfaceDeliveryStateFact,
    ) -> None:
        updated = connection.execute(
            """
            UPDATE canonical_mutation_surface_deliveries
            SET status = %s, state_revision = %s, repair_generation = %s,
                attempt_count = %s, lease_generation = %s,
                lease_owner_id = %s, lease_expires_at = %s,
                next_attempt_at = %s, terminal_receipt = %s,
                last_failure = %s, state_fingerprint = %s,
                updated_at = now()
            WHERE mutation_id = %s AND surface = %s
            RETURNING mutation_id
            """,
            (
                state.status,
                state.state_revision,
                state.repair_generation,
                state.attempt_count,
                state.lease_generation,
                state.lease_owner_id,
                state.lease_expires_at,
                state.next_attempt_at,
                (
                    Jsonb(state.terminal_receipt.model_dump(mode="json"))
                    if state.terminal_receipt is not None
                    else None
                ),
                (
                    Jsonb(state.last_failure.model_dump(mode="json"))
                    if state.last_failure is not None
                    else None
                ),
                state.state_fingerprint,
                mutation_id,
                surface.value,
            ),
        ).fetchone()
        if updated is None:
            raise ValueError("canonical mutation surface state disappeared")

    @staticmethod
    def _read_target_head(
        connection,
        *,
        surface: CanonicalMutationSurface,
        sequence_key: str,
        lock: bool,
    ) -> CanonicalMutationSurfaceTargetHeadFact | None:
        suffix = " FOR UPDATE" if lock else ""
        row = connection.execute(
            """
            SELECT head_payload, head_fingerprint
            FROM canonical_mutation_surface_target_heads
            WHERE surface = %s AND sequence_key = %s
            """
            + suffix,
            (surface.value, sequence_key),
        ).fetchone()
        if row is None:
            return None
        head = CanonicalMutationSurfaceTargetHeadFact.model_validate(
            row["head_payload"]
        )
        if head.head_fingerprint != str(row["head_fingerprint"]):
            raise ValueError("canonical mutation target head drifted")
        return head

    @staticmethod
    def _write_target_head(
        connection,
        *,
        previous: CanonicalMutationSurfaceTargetHeadFact | None,
        resulting: CanonicalMutationSurfaceTargetHeadFact,
    ) -> None:
        if previous is None:
            updated = connection.execute(
                """
                INSERT INTO canonical_mutation_surface_target_heads (
                    surface, sequence_key, head_payload, head_fingerprint
                ) VALUES (%s, %s, %s, %s)
                ON CONFLICT (surface, sequence_key) DO NOTHING
                RETURNING surface
                """,
                (
                    resulting.surface.value,
                    resulting.sequence_key,
                    Jsonb(resulting.model_dump(mode="json")),
                    resulting.head_fingerprint,
                ),
            ).fetchone()
        else:
            updated = connection.execute(
                """
                UPDATE canonical_mutation_surface_target_heads
                SET head_payload = %s, head_fingerprint = %s,
                    updated_at = now()
                WHERE surface = %s AND sequence_key = %s
                  AND head_fingerprint = %s
                RETURNING surface
                """,
                (
                    Jsonb(resulting.model_dump(mode="json")),
                    resulting.head_fingerprint,
                    resulting.surface.value,
                    resulting.sequence_key,
                    previous.head_fingerprint,
                ),
            ).fetchone()
        if updated is None:
            raise ValueError("canonical mutation target head CAS failed")

    @staticmethod
    def _set_deadline(connection, deadline_monotonic: float) -> None:
        remaining = deadline_monotonic - monotonic()
        if remaining <= 0:
            raise TimeoutError("canonical mutation surface deadline exceeded")
        milliseconds = max(1, int(remaining * 1000))
        connection.execute(
            "SELECT pg_catalog.set_config('statement_timeout', %s, true)",
            (str(milliseconds),),
        )
        connection.execute(
            "SELECT pg_catalog.set_config('lock_timeout', %s, true)",
            (str(milliseconds),),
        )


def _utc(value: datetime | None) -> datetime | None:
    return value.astimezone(timezone.utc) if value is not None else None


@dataclass(slots=True)
class CanonicalMutationSurfaceWorker:
    repository: PostgresCanonicalMutationSurfaceRepository
    handler: CanonicalMutationSurfaceHandler
    owner_id: str

    def run_once(
        self, *, limit: int = 4, deadline_monotonic: float
    ) -> int:
        deliveries = self.repository.claim_due(
            surface=self.handler.surface,
            owner_id=self.owner_id,
            limit=limit,
            deadline_monotonic=deadline_monotonic,
        )
        completed = 0
        for delivery in deliveries:
            physical = delivery.lease.delivery_policy.physical_policy
            attempt_deadline = min(
                deadline_monotonic,
                monotonic()
                + physical.external_surface_attempt_timeout_seconds,
            )
            try:
                target_identity, applied_fingerprint = self.handler.apply(
                    delivery,
                    deadline_monotonic=attempt_deadline,
                )
                self.repository.settle_applied(
                    delivery=delivery,
                    target_semantic_identity=target_identity,
                    applied_document_semantic_fingerprint=(
                        applied_fingerprint
                    ),
                    deadline_monotonic=min(
                        deadline_monotonic,
                        monotonic()
                        + physical.result_commit_timeout_seconds,
                    ),
                )
                completed += 1
            except BaseException as error:
                self.repository.settle_failure(
                    delivery=delivery,
                    failure_kind=(
                        DurableProjectionFailureKind
                        .TRANSIENT_STORAGE_UNAVAILABLE
                    ),
                    error=error,
                    deadline_monotonic=min(
                        deadline_monotonic,
                        monotonic()
                        + physical.result_commit_timeout_seconds,
                    ),
                )
        return completed


__all__ = [
    "BoundCanonicalMutationSurfaceDelivery",
    "CanonicalMutationSurfaceHandler",
    "CanonicalMutationSurfaceWorker",
    "PostgresCanonicalMutationSurfaceRepository",
]
