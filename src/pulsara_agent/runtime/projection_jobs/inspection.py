"""Bounded, typed durable-projection inspection."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime, timezone
from typing import Any, Protocol

from pydantic import TypeAdapter, ValidationError

from pulsara_agent.projection_jobs.contracts import (
    CanonicalMutationSurfaceDeliveryIdentityFact,
    CanonicalMutationSurfaceDeliveryStateFact,
    DurableProjectionAppliedResultReceiptFact,
    DurableProjectionDeliveryPolicyFact,
    DurableProjectionHandlerContractFact,
    DurableProjectionJobOperationalStateFact,
    DurableProjectionLedgerHorizonFact,
    DurableProjectionRepairActionFact,
    DurableProjectionResultReceiptFact,
    DurableProjectionSessionCutoverFact,
    DurableProjectionSourceEventReferenceFact,
    DurableProjectionTargetAuthorityConflictFact,
    DurableProjectionTargetHeadFact,
    PreActivationProjectionCoverageReceiptFact,
    PreActivationProjectionSessionCutoverFact,
    RuntimeWriteAdmissionEpochFact,
)


_RECEIPT_ADAPTER = TypeAdapter(DurableProjectionResultReceiptFact)


class DurableProjectionInspectionStore(Protocol):
    def durable_projection_jobs(self, **kwargs: object) -> list[dict[str, Any]]: ...

    def durable_projection_receipts_for_jobs(
        self, **kwargs: object
    ) -> list[dict[str, Any]]: ...

    def durable_projection_target_heads(
        self, **kwargs: object
    ) -> list[dict[str, Any]]: ...

    def durable_projection_conflicts(
        self, **kwargs: object
    ) -> list[dict[str, Any]]: ...

    def durable_projection_cutovers(self, session_id: str) -> list[dict[str, Any]]: ...

    def durable_projection_coverage_receipts(
        self, session_id: str, **kwargs: object
    ) -> list[dict[str, Any]]: ...

    def durable_projection_repair_actions(
        self, **kwargs: object
    ) -> list[dict[str, Any]]: ...

    def durable_surface_deliveries(self, **kwargs: object) -> list[dict[str, Any]]: ...

    def runtime_write_admission_epoch(self) -> dict[str, Any] | None: ...


def inspect_durable_projection_state(
    store: DurableProjectionInspectionStore,
    *,
    session_id: str | None = None,
    run_id: str | None = None,
    after_job_id: str | None = None,
    after_surface_key: tuple[str, str] | None = None,
    limit: int = 128,
    job_statuses: tuple[str, ...] | None = None,
) -> dict[str, Any]:
    """Project bounded durable authority without interpreting EventLog history."""

    if not 1 <= limit <= 256:
        raise ValueError("projection inspection limit must be between 1 and 256")
    diagnostics: list[dict[str, Any]] = []
    raw_jobs = store.durable_projection_jobs(
        session_id=session_id,
        run_id=run_id,
        after_job_id=after_job_id,
        limit=limit,
        statuses=job_statuses,
    )
    jobs_truncated = len(raw_jobs) > limit
    raw_jobs = raw_jobs[:limit]
    jobs = [_validated_job(row, diagnostics=diagnostics) for row in raw_jobs]
    job_ids = tuple(str(row["job_id"]) for row in raw_jobs)

    raw_receipts = store.durable_projection_receipts_for_jobs(
        session_id=session_id,
        run_id=run_id,
        limit=min(256, max(limit, len(job_ids) * 2 or 1)),
    )
    receipts_truncated = len(raw_receipts) > 256
    receipts = [
        _validated_payload(
            row["receipt_payload"],
            _RECEIPT_ADAPTER.validate_python,
            durable_kind="result_receipt",
            durable_id=str(row["receipt_id"]),
            expected_fingerprint=str(row["receipt_fingerprint"]),
            fingerprint_field="receipt_fingerprint",
            diagnostics=diagnostics,
        )
        for row in raw_receipts[:256]
    ]
    mutation_ids = tuple(
        sorted(
            {
                reference.mutation_id
                for receipt in receipts
                if isinstance(receipt, DurableProjectionAppliedResultReceiptFact)
                for reference in receipt.canonical_mutation_references
            }
        )
    )

    raw_heads = store.durable_projection_target_heads(
        session_id=session_id,
        run_id=run_id,
        limit=limit,
    )
    heads_truncated = len(raw_heads) > limit
    heads = [
        _validated_payload(
            row["head_payload"],
            DurableProjectionTargetHeadFact.model_validate,
            durable_kind="target_head",
            durable_id=f"{row['projection_kind']}:{row['target_key']}",
            expected_fingerprint=str(row["head_fingerprint"]),
            fingerprint_field="head_fingerprint",
            diagnostics=diagnostics,
        )
        for row in raw_heads[:limit]
    ]

    raw_conflicts = store.durable_projection_conflicts(
        session_id=session_id,
        run_id=run_id,
        limit=limit,
    )
    conflicts_truncated = len(raw_conflicts) > limit
    conflicts = [
        _validated_payload(
            row["conflict_payload"],
            DurableProjectionTargetAuthorityConflictFact.model_validate,
            durable_kind="target_authority_conflict",
            durable_id=str(row["conflict_id"]),
            expected_fingerprint=str(row["conflict_fingerprint"]),
            fingerprint_field="conflict_fingerprint",
            diagnostics=diagnostics,
        )
        for row in raw_conflicts[:limit]
    ]

    cutovers: list[object] = []
    coverage_receipts: list[object] = []
    if session_id is not None:
        for row in store.durable_projection_cutovers(session_id):
            validator = (
                DurableProjectionSessionCutoverFact.model_validate
                if row["cutover_state"] == "active"
                else PreActivationProjectionSessionCutoverFact.model_validate
            )
            cutovers.append(
                _validated_payload(
                    row["cutover_payload"],
                    validator,
                    durable_kind=f"{row['cutover_state']}_cutover",
                    durable_id=str(row["projection_kind"]),
                    expected_fingerprint=str(row["cutover_fingerprint"]),
                    fingerprint_field="cutover_fingerprint",
                    diagnostics=diagnostics,
                )
            )
        raw_coverage = store.durable_projection_coverage_receipts(
            session_id,
            limit=32,
        )
        coverage_receipts = [
            _validated_payload(
                row["receipt_payload"],
                PreActivationProjectionCoverageReceiptFact.model_validate,
                durable_kind="pre_activation_coverage_receipt",
                durable_id=str(row["coverage_receipt_id"]),
                expected_fingerprint=str(row["receipt_fingerprint"]),
                fingerprint_field="receipt_fingerprint",
                diagnostics=diagnostics,
            )
            for row in raw_coverage[:32]
        ]

    raw_repairs = store.durable_projection_repair_actions(
        job_ids=job_ids,
        limit=limit,
    )
    repairs_truncated = len(raw_repairs) > limit
    repairs = [
        _validated_payload(
            row["action_payload"],
            DurableProjectionRepairActionFact.model_validate,
            durable_kind="repair_action",
            durable_id=str(row["repair_action_id"]),
            expected_fingerprint=str(row["action_fingerprint"]),
            fingerprint_field="action_fingerprint",
            diagnostics=diagnostics,
        )
        for row in raw_repairs[:limit]
    ]

    raw_surfaces = store.durable_surface_deliveries(
        mutation_ids=mutation_ids
        if session_id is not None or run_id is not None
        else None,
        after_key=after_surface_key,
        limit=limit,
    )
    surfaces_truncated = len(raw_surfaces) > limit
    surfaces = [
        _validated_surface(row, diagnostics=diagnostics) for row in raw_surfaces[:limit]
    ]

    raw_epoch = store.runtime_write_admission_epoch()
    epoch = (
        _validated_payload(
            raw_epoch["epoch_payload"],
            RuntimeWriteAdmissionEpochFact.model_validate,
            durable_kind="runtime_write_admission_epoch",
            durable_id="singleton",
            expected_fingerprint=str(raw_epoch["epoch_fingerprint"]),
            fingerprint_field="epoch_fingerprint",
            diagnostics=diagnostics,
        )
        if raw_epoch is not None
        else None
    )

    durable_status = _health_status(
        jobs=jobs,
        surfaces=surfaces,
        conflict_count=len(conflicts),
        diagnostics=diagnostics,
    )
    return {
        "status": durable_status,
        "runtime_write_admission_epoch": _dump(epoch),
        "jobs": [_dump(item) for item in jobs],
        "job_page": {
            "limit": limit,
            "truncated": jobs_truncated,
            "next_cursor": (
                str(raw_jobs[-1]["job_id"]) if jobs_truncated and raw_jobs else None
            ),
        },
        "result_receipts": [_dump(item) for item in receipts],
        "result_receipts_truncated": receipts_truncated,
        "target_heads": [_dump(item) for item in heads],
        "target_heads_truncated": heads_truncated,
        "target_authority_conflicts": [_dump(item) for item in conflicts],
        "target_authority_conflicts_truncated": conflicts_truncated,
        "cutovers": [_dump(item) for item in cutovers],
        "pre_activation_coverage_receipts": [_dump(item) for item in coverage_receipts],
        "repair_actions": [_dump(item) for item in repairs],
        "repair_actions_truncated": repairs_truncated,
        "surface_deliveries": [_dump(item) for item in surfaces],
        "surface_page": {
            "limit": limit,
            "truncated": surfaces_truncated,
            "next_cursor": (
                {
                    "mutation_id": str(raw_surfaces[-1]["mutation_id"]),
                    "surface": str(raw_surfaces[-1]["surface"]),
                }
                if surfaces_truncated and raw_surfaces
                else None
            ),
        },
        "historical_projection_status": _historical_projection_status(
            session_id=session_id,
            cutovers=cutovers,
        ),
        "diagnostics": diagnostics,
    }


def _validated_job(
    row: Mapping[str, Any],
    *,
    diagnostics: list[dict[str, Any]],
) -> dict[str, Any]:
    job_id = str(row["job_id"])
    try:
        source = DurableProjectionSourceEventReferenceFact.model_validate(
            row["source_reference"]
        )
        horizon = DurableProjectionLedgerHorizonFact.model_validate(
            row["trigger_horizon"]
        )
        handler = DurableProjectionHandlerContractFact.model_validate(
            row["handler_contract"]
        )
        delivery = DurableProjectionDeliveryPolicyFact.model_validate(
            row["delivery_policy"]
        )
        state = DurableProjectionJobOperationalStateFact.model_validate(
            {
                "status": row["status"],
                "state_revision": row["state_revision"],
                "repair_generation": row["repair_generation"],
                "attempt_count": row["attempt_count"],
                "dispatch_attempt_count": row["dispatch_attempt_count"],
                "settlement_generation": row["settlement_generation"],
                "lease_generation": row["lease_generation"],
                "lease_owner_id": row["lease_owner_id"],
                "lease_expires_at": row["lease_expires_at"],
                "next_attempt_at": row["next_attempt_at"],
                "last_failure": row["last_failure"],
                "compaction_memory_deferral": row["compaction_memory_deferral"],
                "result_receipt_reference": row["result_receipt_reference"],
                "state_fingerprint": row["state_fingerprint"],
            }
        )
        if (
            source.event_id != row["source_event_id"]
            or source.sequence != row["source_sequence"]
            or source.event_type != row["source_event_type"]
            or horizon.through_sequence != source.sequence
            or source.runtime_session_id != row["runtime_session_id"]
            or handler.contract_fingerprint != row["handler_contract_fingerprint"]
            or delivery.delivery_policy_fingerprint
            != row["delivery_policy_fingerprint"]
        ):
            raise ValueError("job columns do not exact-join nested authority")
    except (KeyError, TypeError, ValueError, ValidationError) as exc:
        diagnostics.append(_authority_diagnostic("projection_job", job_id, exc))
        return {
            "job_id": job_id,
            "authority_status": "authority_untrusted",
        }
    return {
        "job_id": job_id,
        "projection_kind": str(row["projection_kind"]),
        "target_key": str(row["target_key"]),
        "runtime_session_id": str(row["runtime_session_id"]),
        "run_id": str(row["run_id"]),
        "source_event_reference": source.model_dump(mode="json"),
        "source_horizon": horizon.model_dump(mode="json"),
        "handler_contract_fingerprint": handler.contract_fingerprint,
        "activation_fingerprint": str(row["activation_fingerprint"]),
        "seed_contract_fingerprint": str(row["seed_contract_fingerprint"]),
        "delivery_policy_fingerprint": delivery.delivery_policy_fingerprint,
        "job_semantic_fingerprint": str(row["job_semantic_fingerprint"]),
        "job_candidate_fingerprint": str(row["job_candidate_fingerprint"]),
        "state": state.model_dump(mode="json"),
        "authority_status": "trusted",
    }


def _validated_surface(
    row: Mapping[str, Any],
    *,
    diagnostics: list[dict[str, Any]],
) -> object:
    durable_id = f"{row['mutation_id']}:{row['surface']}"
    try:
        state = CanonicalMutationSurfaceDeliveryStateFact.model_validate(
            {
                "delivery_identity": row["delivery_identity"],
                "delivery_policy": row["delivery_policy"],
                "status": row["status"],
                "state_revision": row["state_revision"],
                "repair_generation": row["repair_generation"],
                "attempt_count": row["attempt_count"],
                "lease_generation": row["lease_generation"],
                "lease_owner_id": row["lease_owner_id"],
                "lease_expires_at": _utc(row["lease_expires_at"]),
                "next_attempt_at": _utc(row["next_attempt_at"]),
                "terminal_receipt": row["terminal_receipt"],
                "last_failure": row["last_failure"],
                "state_fingerprint": row["state_fingerprint"],
            }
        )
        identity = CanonicalMutationSurfaceDeliveryIdentityFact.model_validate(
            row["delivery_identity"]
        )
        if (
            identity.mutation_id != row["mutation_id"]
            or identity.surface.value != row["surface"]
            or identity.delivery_identity_fingerprint
            != row["delivery_identity_fingerprint"]
            or identity.mutation_semantic_fingerprint
            != row["mutation_semantic_fingerprint"]
        ):
            raise ValueError("surface columns do not exact-join nested identity")
        return state
    except (KeyError, TypeError, ValueError, ValidationError) as exc:
        diagnostics.append(
            _authority_diagnostic("canonical_surface_delivery", durable_id, exc)
        )
        return {
            "delivery_id": durable_id,
            "authority_status": "authority_untrusted",
        }


def _validated_payload(
    payload: object,
    validator: Any,
    *,
    durable_kind: str,
    durable_id: str,
    expected_fingerprint: str,
    fingerprint_field: str,
    diagnostics: list[dict[str, Any]],
) -> object:
    try:
        fact = validator(payload)
        if getattr(fact, fingerprint_field) != expected_fingerprint:
            raise ValueError("outer fingerprint differs from nested fact")
        return fact
    except (TypeError, ValueError, ValidationError) as exc:
        diagnostics.append(_authority_diagnostic(durable_kind, durable_id, exc))
        return {
            "durable_id": durable_id,
            "authority_status": "authority_untrusted",
        }


def _health_status(
    *,
    jobs: list[dict[str, Any]],
    surfaces: list[object],
    conflict_count: int,
    diagnostics: list[dict[str, Any]],
) -> str:
    if conflict_count or diagnostics:
        return "authority_untrusted"
    job_statuses = {
        str(item.get("state", {}).get("status"))
        for item in jobs
        if isinstance(item, dict)
    }
    surface_statuses = {
        item.status
        for item in surfaces
        if isinstance(item, CanonicalMutationSurfaceDeliveryStateFact)
    }
    if "dead_letter" in job_statuses or "dead_letter" in surface_statuses:
        return "degraded_dead_letter"
    if {"retry_wait", "leased"} & (job_statuses | surface_statuses):
        return "retrying"
    if {"pending"} & (job_statuses | surface_statuses):
        return "backlogged"
    return "healthy"


def _historical_projection_status(
    *,
    session_id: str | None,
    cutovers: list[object],
) -> object:
    if session_id is None:
        return None
    by_kind: dict[str, str] = {}
    for cutover in cutovers:
        if isinstance(cutover, DurableProjectionSessionCutoverFact):
            by_kind[cutover.projection_kind.value] = "durably_observable"
        elif isinstance(cutover, PreActivationProjectionSessionCutoverFact):
            by_kind[cutover.projection_kind.value] = "not_durably_observable"
    return by_kind


def _authority_diagnostic(
    durable_kind: str,
    durable_id: str,
    exc: BaseException,
) -> dict[str, Any]:
    return {
        "severity": "error",
        "code": "durable_projection_authority_untrusted",
        "message": "Durable projection state failed typed authority validation.",
        "details": {
            "durable_kind": durable_kind,
            "durable_id": durable_id,
            "failure_type": type(exc).__name__,
        },
    }


def _dump(value: object) -> object:
    model_dump = getattr(value, "model_dump", None)
    if callable(model_dump):
        return model_dump(mode="json")
    return value


def _utc(value: object) -> object:
    if isinstance(value, datetime):
        return value.astimezone(timezone.utc)
    return value


__all__ = [
    "DurableProjectionInspectionStore",
    "inspect_durable_projection_state",
]
