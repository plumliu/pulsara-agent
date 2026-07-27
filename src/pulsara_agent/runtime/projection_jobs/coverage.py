"""Deterministic pre-activation coverage pages and receipts."""

from __future__ import annotations

from typing import Iterable, cast

from pulsara_agent.primitives._context_base import (
    canonical_json_bytes,
    context_fingerprint,
)
from pulsara_agent.projection_jobs.contracts import (
    DurableProjectionKind,
    DurableProjectionLedgerHorizonFact,
    DurableProjectionSourceEventReferenceFact,
    DurableProjectionTargetHeadFact,
    PreActivationProjectionCoveragePageFact,
    PreActivationProjectionCoverageReceiptFact,
    PreActivationProjectionCoverageSetReferenceFact,
    PreActivationProjectionTargetCoverageItemFact,
    build_projection_fact,
)


MAX_COVERAGE_PAGE_ITEMS = 256
MAX_COVERAGE_PAGE_BYTES = 8 * 1024 * 1024


def build_coverage_item(
    *,
    projection_kind: DurableProjectionKind,
    target_head: DurableProjectionTargetHeadFact,
    latest_trigger_event_reference: DurableProjectionSourceEventReferenceFact,
) -> PreActivationProjectionTargetCoverageItemFact:
    if target_head.projection_kind is not projection_kind:
        raise ValueError("coverage item kind/head mismatch")
    if (
        target_head.applied_source_event_reference_fingerprint
        != latest_trigger_event_reference.reference_fingerprint
        or target_head.applied_source_sequence
        != latest_trigger_event_reference.sequence
    ):
        raise ValueError("coverage item does not bind the effective trigger")
    return cast(
        PreActivationProjectionTargetCoverageItemFact,
        build_projection_fact(
            PreActivationProjectionTargetCoverageItemFact,
            schema_version="pre_activation_projection_target_coverage_item.v1",
            projection_kind=projection_kind,
            target_key=target_head.target_key,
            latest_trigger_event_reference=latest_trigger_event_reference,
            applied_result_receipt_reference=(
                target_head.applied_result_receipt_reference
            ),
        ),
    )


def build_coverage_pages(
    *,
    runtime_session_id: str,
    projection_kind: DurableProjectionKind,
    items: Iterable[PreActivationProjectionTargetCoverageItemFact],
) -> tuple[PreActivationProjectionCoveragePageFact, ...]:
    ordered = tuple(sorted(items, key=lambda item: item.target_key))
    if len({item.target_key for item in ordered}) != len(ordered):
        raise ValueError("coverage target keys must be unique")
    if any(item.projection_kind is not projection_kind for item in ordered):
        raise ValueError("coverage page contains another projection kind")
    pages: list[PreActivationProjectionCoveragePageFact] = []
    previous: str | None = None
    for page_index, start in enumerate(range(0, len(ordered), MAX_COVERAGE_PAGE_ITEMS)):
        chunk = ordered[start : start + MAX_COVERAGE_PAGE_ITEMS]
        item_bytes = len(
            canonical_json_bytes(tuple(item.model_dump(mode="json") for item in chunk))
        )
        if item_bytes > MAX_COVERAGE_PAGE_BYTES:
            raise ValueError("coverage page exceeds its canonical byte bound")
        accumulator = context_fingerprint(
            "pre-activation-coverage-page-items:v1",
            tuple(item.item_fingerprint for item in chunk),
        )
        page = cast(
            PreActivationProjectionCoveragePageFact,
            build_projection_fact(
                PreActivationProjectionCoveragePageFact,
                schema_version="pre_activation_projection_coverage_page.v1",
                runtime_session_id=runtime_session_id,
                projection_kind=projection_kind,
                page_index=page_index,
                previous_page_fingerprint=previous,
                ordered_items=chunk,
                item_count=len(chunk),
                item_accumulator=accumulator,
                canonical_utf8_bytes=item_bytes,
            ),
        )
        pages.append(page)
        previous = page.page_fingerprint
    return tuple(pages)


def build_coverage_set_reference(
    pages: tuple[PreActivationProjectionCoveragePageFact, ...],
) -> PreActivationProjectionCoverageSetReferenceFact:
    if pages:
        session_ids = {page.runtime_session_id for page in pages}
        kinds = {page.projection_kind for page in pages}
        if len(session_ids) != 1 or len(kinds) != 1:
            raise ValueError("coverage pages must share session and kind")
        for index, page in enumerate(pages):
            expected_previous = pages[index - 1].page_fingerprint if index else None
            if (
                page.page_index != index
                or page.previous_page_fingerprint != expected_previous
            ):
                raise ValueError("coverage page chain is not contiguous")
    items = tuple(item for page in pages for item in page.ordered_items)
    return cast(
        PreActivationProjectionCoverageSetReferenceFact,
        build_projection_fact(
            PreActivationProjectionCoverageSetReferenceFact,
            schema_version="pre_activation_projection_coverage_set_reference.v1",
            page_count=len(pages),
            target_count=len(items),
            ordered_page_fingerprint_accumulator=context_fingerprint(
                "pre-activation-coverage-page-root:v1",
                tuple(page.page_fingerprint for page in pages),
            ),
            ordered_target_item_accumulator=context_fingerprint(
                "pre-activation-coverage-target-root:v1",
                tuple(item.item_fingerprint for item in items),
            ),
            last_page_fingerprint=(pages[-1].page_fingerprint if pages else None),
        ),
    )


def build_coverage_receipt(
    *,
    runtime_session_id: str,
    projection_kind: DurableProjectionKind,
    pre_activation_contract_fingerprint: str,
    start_cutover_fingerprint: str,
    frozen_horizon: DurableProjectionLedgerHorizonFact,
    scanned_trigger_event_references: tuple[
        DurableProjectionSourceEventReferenceFact, ...
    ],
    target_coverage_set: PreActivationProjectionCoverageSetReferenceFact,
    maintenance_operation_id: str,
    maintenance_authority_fingerprint: str,
) -> PreActivationProjectionCoverageReceiptFact:
    if frozen_horizon.runtime_session_id != runtime_session_id:
        raise ValueError("coverage receipt horizon session mismatch")
    if any(
        item.runtime_session_id != runtime_session_id
        or item.sequence > frozen_horizon.through_sequence
        for item in scanned_trigger_event_references
    ):
        raise ValueError("coverage trigger lies outside the frozen horizon")
    trigger_accumulator = context_fingerprint(
        "pre-activation-coverage-trigger-root:v1",
        tuple(item.reference_fingerprint for item in scanned_trigger_event_references),
    )
    identity_payload = {
        "runtime_session_id": runtime_session_id,
        "projection_kind": projection_kind.value,
        "pre_activation_contract_fingerprint": (pre_activation_contract_fingerprint),
        "start_cutover_fingerprint": start_cutover_fingerprint,
        "frozen_horizon_fingerprint": frozen_horizon.horizon_fingerprint,
        "scanned_trigger_event_count": len(scanned_trigger_event_references),
        "scanned_trigger_event_accumulator": trigger_accumulator,
        "target_coverage_set_reference_fingerprint": (
            target_coverage_set.reference_fingerprint
        ),
        "maintenance_operation_id": maintenance_operation_id,
        "maintenance_authority_fingerprint": maintenance_authority_fingerprint,
    }
    receipt_id = "pre-activation-coverage:" + context_fingerprint(
        "pre-activation-coverage-receipt-id:v1",
        identity_payload,
    )
    return cast(
        PreActivationProjectionCoverageReceiptFact,
        build_projection_fact(
            PreActivationProjectionCoverageReceiptFact,
            schema_version="pre_activation_projection_coverage_receipt.v1",
            coverage_receipt_id=receipt_id,
            runtime_session_id=runtime_session_id,
            projection_kind=projection_kind,
            pre_activation_contract_fingerprint=(pre_activation_contract_fingerprint),
            start_cutover_fingerprint=start_cutover_fingerprint,
            frozen_horizon=frozen_horizon,
            scanned_trigger_event_count=len(scanned_trigger_event_references),
            scanned_trigger_event_accumulator=trigger_accumulator,
            target_coverage_set=target_coverage_set,
            maintenance_operation_id=maintenance_operation_id,
            maintenance_authority_fingerprint=(maintenance_authority_fingerprint),
        ),
    )


__all__ = [
    "MAX_COVERAGE_PAGE_BYTES",
    "MAX_COVERAGE_PAGE_ITEMS",
    "build_coverage_item",
    "build_coverage_pages",
    "build_coverage_receipt",
    "build_coverage_set_reference",
]
