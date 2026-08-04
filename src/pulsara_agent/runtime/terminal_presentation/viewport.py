"""Bounded unified history viewport and direction-neutral page service."""

from __future__ import annotations

from time import monotonic

from pulsara_agent.ports.terminal_presentation import (
    PresentationHistoryCursorStale,
    PresentationHistoryPageData,
    PresentationHistoryPageDirection,
    PresentationHistoryPageReadLimits,
    PresentationHistoryPageReadOutcome,
    PresentationHistoryRebaseRequired,
    PresentationHistoryReconciliationRequired,
)
from pulsara_agent.primitives._context_base import canonical_json_bytes
from pulsara_agent.primitives.context import context_fingerprint
from pulsara_agent.primitives.frozen import build_frozen_fact
from pulsara_agent.primitives.presentation_history import (
    ActiveHeadRankBasisFact,
    ConfirmedRootRankBasisFact,
    PresentationHistoryActiveHeadFact,
    PresentationHistoryMaterializationPolicyFact,
    PresentationHistoryPageCursorFact,
    PresentationHistoryRankedEntryView,
    PresentationHistoryRootIdentityFact,
)
from pulsara_agent.primitives.presentation_view import (
    PresentationHistoryLatestRootCursorPairFact,
    PresentationHistoryViewportSnapshotFact,
)
from pulsara_agent.runtime.terminal_presentation.history_capacity import (
    PresentationHistoryCapacityOwner,
)
from pulsara_agent.runtime.terminal_presentation.history_checkpoint import (
    EMPTY_PRESENTATION_ENTRY_ACCUMULATOR,
    PresentationHistoryProjectionCheckpointOwner,
)
from pulsara_agent.runtime.terminal_presentation.history_retention import (
    PresentationHistoryRootRetentionOwner,
)
from pulsara_agent.runtime.terminal_presentation.history_tree import (
    PresentationHistoryTreeError,
)


EMPTY_TAIL_SOURCE_RANGE_ACCUMULATOR = context_fingerprint(
    "presentation-history-tail-source-range:v1", ()
)
EMPTY_TAIL_SEGMENT_ACCUMULATOR = context_fingerprint(
    "presentation-history-tail-segments:v1", ()
)
EMPTY_TAIL_MUTATION_ACCUMULATOR = context_fingerprint(
    "presentation-history-tail-mutations:v1", ()
)
MAXIMUM_SNAPSHOT_RESIDENT_CANONICAL_BYTES = 4 * 1024 * 1024
MAXIMUM_SNAPSHOT_RESIDENT_RENDERED_BYTES = 4 * 1024 * 1024


class PresentationHistoryViewportService:
    def __init__(
        self,
        *,
        runtime_session_id: str,
        checkpoint_owner: PresentationHistoryProjectionCheckpointOwner,
        retention_owner: PresentationHistoryRootRetentionOwner,
        capacity_owner: PresentationHistoryCapacityOwner,
        materialization_policy: PresentationHistoryMaterializationPolicyFact,
        resident_entry_limit: int = 200,
    ) -> None:
        if resident_entry_limit <= 0:
            raise ValueError("presentation resident entry limit must be positive")
        self.runtime_session_id = runtime_session_id
        self.checkpoint_owner = checkpoint_owner
        self.retention_owner = retention_owner
        self.capacity_owner = capacity_owner
        self.policy = materialization_policy
        self.resident_entry_limit = min(
            resident_entry_limit, materialization_policy.read_max_entries
        )
        self._projection_revision: int | None = None

    def install_checkpoint(
        self,
        checkpoint,
        *,
        deadline_monotonic: float | None = None,
    ) -> PresentationHistoryRootIdentityFact:
        identity = self.checkpoint_owner.materialize_root_identity(
            checkpoint, deadline_monotonic=deadline_monotonic
        )
        root = self.checkpoint_owner.read_root(
            checkpoint.projection_root_reference,
            deadline_monotonic=deadline_monotonic,
        )
        self.retention_owner.install(identity, root)
        self._projection_revision = checkpoint.projection_revision
        return identity

    def snapshot(
        self,
        *,
        deadline_monotonic: float | None = None,
    ) -> PresentationHistoryViewportSnapshotFact:
        identity, root = self.retention_owner.latest()
        if self._projection_revision is None:
            raise RuntimeError("presentation viewport has no installed checkpoint")
        active_head = self._build_empty_tail_active_head(identity, root.entry_count)
        page = self.checkpoint_owner.tree.read_page(
            root.tree_root_node_reference,
            exclusive_placement_key=None,
            direction="before",
            max_entries=self.resident_entry_limit,
            max_node_reads=self.policy.read_max_node_reads,
            deadline_monotonic=deadline_monotonic,
        )
        available = tuple(
            _ranked_entry(
                entry,
                rank=rank,
                basis=build_frozen_fact(
                    ActiveHeadRankBasisFact,
                    schema_version="presentation_active_head_rank_basis.v1",
                    rank_basis_kind="active_head",
                    history_active_head_fingerprint=active_head.active_head_fingerprint,
                    through_authority_sequence=active_head.through_authority_sequence,
                ),
            )
            for entry, rank in page.ordered_ranked_entries
        )
        ranked = _fit_newest_resident_suffix(
            available,
            maximum_entries=self.resident_entry_limit,
            maximum_canonical_bytes=min(
                MAXIMUM_SNAPSHOT_RESIDENT_CANONICAL_BYTES,
                self.policy.read_max_page_canonical_bytes,
            ),
            maximum_rendered_bytes=min(
                MAXIMUM_SNAPSHOT_RESIDENT_RENDERED_BYTES,
                self.policy.read_max_page_rendered_bytes,
            ),
        )
        return _build_viewport_snapshot(
            runtime_session_id=self.runtime_session_id,
            projection_revision=self._projection_revision,
            active_head=active_head,
            ranked=ranked,
        )

    def read_page(
        self,
        *,
        cursor: PresentationHistoryPageCursorFact,
        direction: PresentationHistoryPageDirection,
        limits: PresentationHistoryPageReadLimits,
        absolute_deadline: float | None,
    ) -> PresentationHistoryPageReadOutcome:
        if absolute_deadline is not None and monotonic() >= absolute_deadline:
            return self._reconciliation(
                cursor,
                fault_code="PRESENTATION_PAGE_DEADLINE_EXPIRED",
                retry_after_ms=None,
            )
        if cursor.runtime_session_id != self.runtime_session_id:
            return self._reconciliation(
                cursor,
                fault_code="PRESENTATION_CURSOR_SESSION_MISMATCH",
                retry_after_ms=None,
            )
        retained = self.retention_owner.resolve(
            cursor.history_root_identity.root_identity_fingerprint
        )
        if retained is None:
            latest, _ = self.retention_owner.latest()
            return PresentationHistoryCursorStale(
                disposition="cursor_stale",
                requested_cursor_fingerprint=cursor.cursor_fingerprint,
                latest_root_identity=latest,
                replacement_cursor=None,
                replacement_cursor_anchor_proof_fingerprint=None,
                response_fingerprint=context_fingerprint(
                    "presentation-history-cursor-stale:v1",
                    {
                        "requested_cursor_fingerprint": cursor.cursor_fingerprint,
                        "latest_root_identity_fingerprint": (
                            latest.root_identity_fingerprint
                        ),
                        "replacement": None,
                    },
                ),
            )
        identity, root = retained
        if identity != cursor.history_root_identity:
            return self._reconciliation(
                cursor,
                fault_code="PRESENTATION_CURSOR_ROOT_IDENTITY_CONFLICT",
                retry_after_ms=None,
            )
        effective = PresentationHistoryPageReadLimits(
            maximum_entries=min(limits.maximum_entries, self.policy.read_max_entries),
            maximum_canonical_bytes=min(
                limits.maximum_canonical_bytes,
                self.policy.read_max_page_canonical_bytes,
            ),
            maximum_rendered_bytes=min(
                limits.maximum_rendered_bytes,
                self.policy.read_max_page_rendered_bytes,
            ),
            maximum_node_reads=min(
                limits.maximum_node_reads, self.policy.read_max_node_reads
            ),
            maximum_tree_height=min(
                limits.maximum_tree_height, self.policy.read_max_tree_height
            ),
        )
        if root.tree_height > effective.maximum_tree_height:
            return PresentationHistoryRebaseRequired(
                disposition="rebase_required",
                requested_cursor_fingerprint=cursor.cursor_fingerprint,
                latest_root_identity=self.retention_owner.latest()[0],
                bounded_snapshot_or_rebase_token=(
                    f"presentation-rebase:{cursor.cursor_fingerprint.removeprefix('sha256:')[:24]}"
                ),
                response_fingerprint=context_fingerprint(
                    "presentation-history-page-rebase:v1",
                    {
                        "cursor": cursor.cursor_fingerprint,
                        "reason": "tree_height_exceeded",
                    },
                ),
            )
        anchor_key = (
            None
            if cursor.anchor_placement_key is None
            else cursor.anchor_placement_key.canonical_comparable_key_bytes
        )
        if anchor_key is not None:
            try:
                found = self.checkpoint_owner.tree.find_entry(
                    root.tree_root_node_reference,
                    placement_key=anchor_key,
                    history_entry_id=cursor.anchor_history_entry_id or "",
                    max_node_reads=max(1, effective.maximum_node_reads // 2),
                    deadline_monotonic=absolute_deadline,
                )
            except TimeoutError:
                return self._reconciliation(
                    cursor,
                    fault_code="PRESENTATION_PAGE_DEADLINE_EXPIRED",
                    retry_after_ms=None,
                )
            except PresentationHistoryTreeError:
                return self._reconciliation(
                    cursor,
                    fault_code="PRESENTATION_TREE_READ_BOUND_OR_INTEGRITY_FAILURE",
                    retry_after_ms=None,
                )
            if found is None:
                return self._reconciliation(
                    cursor,
                    fault_code="PRESENTATION_CURSOR_ANCHOR_MISSING",
                    retry_after_ms=None,
                )
        try:
            page = self.checkpoint_owner.tree.read_page(
                root.tree_root_node_reference,
                exclusive_placement_key=anchor_key,
                direction=direction,
                max_entries=effective.maximum_entries,
                max_node_reads=(
                    effective.maximum_node_reads
                    if anchor_key is None
                    else max(1, effective.maximum_node_reads // 2)
                ),
                deadline_monotonic=absolute_deadline,
            )
        except TimeoutError:
            return self._reconciliation(
                cursor,
                fault_code="PRESENTATION_PAGE_DEADLINE_EXPIRED",
                retry_after_ms=None,
            )
        except PresentationHistoryTreeError:
            return self._reconciliation(
                cursor,
                fault_code="PRESENTATION_TREE_READ_BOUND_OR_INTEGRITY_FAILURE",
                retry_after_ms=None,
            )
        basis = build_frozen_fact(
            ConfirmedRootRankBasisFact,
            schema_version="presentation_confirmed_root_rank_basis.v1",
            rank_basis_kind="confirmed_root",
            history_root_identity_fingerprint=identity.root_identity_fingerprint,
        )
        fitted: list[PresentationHistoryRankedEntryView] = []
        canonical_bytes = 0
        rendered_bytes = 0
        truncated_by_bytes = False
        for entry, rank in page.ordered_ranked_entries:
            ranked = _ranked_entry(entry, rank=rank, basis=basis)
            entry_bytes = len(canonical_json_bytes(ranked.model_dump(mode="json")))
            entry_rendered_bytes = _rendered_public_bytes(ranked)
            if (
                canonical_bytes + entry_bytes > effective.maximum_canonical_bytes
                or rendered_bytes + entry_rendered_bytes
                > effective.maximum_rendered_bytes
            ):
                truncated_by_bytes = True
                break
            fitted.append(ranked)
            canonical_bytes += entry_bytes
            rendered_bytes += entry_rendered_bytes
        ranked_entries = tuple(fitted)
        before_cursor = _cursor(identity, ranked_entries[0]) if ranked_entries else None
        after_cursor = _cursor(identity, ranked_entries[-1]) if ranked_entries else None
        first_rank = (
            ranked_entries[0].root_local_display_rank if ranked_entries else None
        )
        last_rank = (
            ranked_entries[-1].root_local_display_rank if ranked_entries else None
        )
        has_more_before = bool(first_rank is not None and first_rank > 0)
        has_more_after = bool(
            last_rank is not None and last_rank + 1 < root.entry_count
        )
        if truncated_by_bytes or page.has_more:
            if direction == "before":
                has_more_before = True
            else:
                has_more_after = True
        accumulator = context_fingerprint(
            "presentation-history-page-entries:v1",
            tuple(
                (
                    item.history_entry.history_entry_id,
                    item.history_entry.entry_fingerprint,
                    item.root_local_display_rank,
                )
                for item in ranked_entries
            ),
        )
        continuity = context_fingerprint(
            "presentation-history-page-continuity-proof:v1",
            {
                "root_identity_fingerprint": identity.root_identity_fingerprint,
                "input_cursor_fingerprint": cursor.cursor_fingerprint,
                "direction": direction,
                "ordered_entry_accumulator": accumulator,
                "before_cursor": (
                    before_cursor.cursor_fingerprint if before_cursor else None
                ),
                "after_cursor": (
                    after_cursor.cursor_fingerprint if after_cursor else None
                ),
                "has_more_before": has_more_before,
                "has_more_after": has_more_after,
            },
        )
        payload = {
            "validated_input_cursor_fingerprint": cursor.cursor_fingerprint,
            "validated_request_direction": direction,
            "root_identity_fingerprint": identity.root_identity_fingerprint,
            "ordered_history_entry_accumulator": accumulator,
            "continuity_proof_fingerprint": continuity,
            "before_cursor_fingerprint": (
                before_cursor.cursor_fingerprint if before_cursor else None
            ),
            "after_cursor_fingerprint": (
                after_cursor.cursor_fingerprint if after_cursor else None
            ),
            "has_more_before": has_more_before,
            "has_more_after": has_more_after,
        }
        return PresentationHistoryPageData(
            disposition="page",
            validated_input_cursor_fingerprint=cursor.cursor_fingerprint,
            validated_request_direction=direction,
            validated_root_identity=identity,
            ordered_history_entries=ranked_entries,
            ordered_history_entry_accumulator=accumulator,
            continuity_proof_fingerprint=continuity,
            before_cursor=before_cursor,
            after_cursor=after_cursor,
            has_more_before=has_more_before,
            has_more_after=has_more_after,
            response_fingerprint=context_fingerprint(
                "presentation-history-page-response:v1", payload
            ),
        )

    def _build_empty_tail_active_head(
        self,
        identity: PresentationHistoryRootIdentityFact,
        root_entry_count: int,
    ) -> PresentationHistoryActiveHeadFact:
        capacity = self.capacity_owner.capacity_state(
            confirmed_entry_count=root_entry_count,
            current_tail_worst_case_entry_count=0,
        )
        return build_frozen_fact(
            PresentationHistoryActiveHeadFact,
            schema_version="presentation_history_active_head.v1",
            runtime_session_id=self.runtime_session_id,
            confirmed_root_identity=identity,
            tail_from_sequence_exclusive=identity.through_authority_sequence,
            through_authority_sequence=identity.through_authority_sequence,
            tail_source_range_accumulator=EMPTY_TAIL_SOURCE_RANGE_ACCUMULATOR,
            tail_segment_count=0,
            ordered_tail_segment_accumulator=EMPTY_TAIL_SEGMENT_ACCUMULATOR,
            tail_mutation_count=0,
            ordered_tail_mutation_accumulator=EMPTY_TAIL_MUTATION_ACCUMULATOR,
            resulting_resident_entry_count=root_entry_count,
            resulting_resident_entry_accumulator=(
                self.retention_owner.latest()[1].ordered_history_entry_accumulator
                if root_entry_count
                else EMPTY_PRESENTATION_ENTRY_ACCUMULATOR
            ),
            capacity_state=capacity,
        )

    def _reconciliation(
        self,
        cursor: PresentationHistoryPageCursorFact,
        *,
        fault_code: str,
        retry_after_ms: int | None,
    ) -> PresentationHistoryReconciliationRequired:
        try:
            latest = self.retention_owner.latest()[0]
        except RuntimeError:
            latest = None
        payload = {
            "requested_cursor_fingerprint": cursor.cursor_fingerprint,
            "fault_code": fault_code,
            "latest_root_identity_fingerprint": (
                latest.root_identity_fingerprint if latest else None
            ),
        }
        return PresentationHistoryReconciliationRequired(
            disposition="reconciliation_required",
            requested_cursor_fingerprint=cursor.cursor_fingerprint,
            fault_code=fault_code,
            reconciliation_owner_identity=(
                f"presentation-page-reconciliation:{self.runtime_session_id}"
            ),
            retry_after_ms=retry_after_ms,
            trusted_latest_root_identity_hint=latest,
            response_fingerprint=context_fingerprint(
                "presentation-history-page-reconciliation:v1", payload
            ),
        )


def _ranked_entry(entry, *, rank: int, basis):
    return build_frozen_fact(
        PresentationHistoryRankedEntryView,
        schema_version="presentation_history_ranked_entry_view.v1",
        history_entry=entry,
        root_local_display_rank=rank,
        rank_basis=basis,
    )


def _cursor(
    identity: PresentationHistoryRootIdentityFact,
    ranked: PresentationHistoryRankedEntryView,
) -> PresentationHistoryPageCursorFact:
    return build_frozen_fact(
        PresentationHistoryPageCursorFact,
        schema_version="presentation_history_page_cursor.v1",
        runtime_session_id=identity.runtime_session_id,
        history_root_identity=identity,
        anchor_history_entry_id=ranked.history_entry.history_entry_id,
        anchor_placement_key=ranked.history_entry.placement_key,
    )


def _cursor_pair(
    identity: PresentationHistoryRootIdentityFact,
    ranked: tuple[PresentationHistoryRankedEntryView, ...],
) -> PresentationHistoryLatestRootCursorPairFact:
    return build_frozen_fact(
        PresentationHistoryLatestRootCursorPairFact,
        schema_version="presentation_history_latest_root_cursor_pair.v1",
        root_identity=identity,
        before_cursor=(_cursor(identity, ranked[0]) if ranked else None),
        after_cursor=(_cursor(identity, ranked[-1]) if ranked else None),
    )


def _resident_vector_fingerprint(
    ranked: tuple[PresentationHistoryRankedEntryView, ...],
) -> str:
    return context_fingerprint(
        "presentation-history-resident-vector:v1",
        tuple(
            (
                item.history_entry.history_entry_id,
                item.history_entry.entry_fingerprint,
                item.history_entry.placement_key.placement_key_fingerprint,
                item.root_local_display_rank,
            )
            for item in ranked
        ),
    )


def _rendered_public_bytes(ranked: PresentationHistoryRankedEntryView) -> int:
    total = 0
    for block in ranked.history_entry.cell.content_blocks:
        if block.block_kind == "text":
            total += block.text_utf8_bytes
        else:
            total += block.public_utf8_bytes
    return total


def fit_viewport_snapshot_resident_suffix(
    snapshot: PresentationHistoryViewportSnapshotFact,
    *,
    maximum_entries: int,
    maximum_canonical_bytes: int = MAXIMUM_SNAPSHOT_RESIDENT_CANONICAL_BYTES,
    maximum_rendered_bytes: int = MAXIMUM_SNAPSHOT_RESIDENT_RENDERED_BYTES,
) -> PresentationHistoryViewportSnapshotFact:
    """Return a pure newest-suffix projection of an already frozen viewport."""

    ranked = _fit_newest_resident_suffix(
        snapshot.ordered_resident_entries,
        maximum_entries=maximum_entries,
        maximum_canonical_bytes=maximum_canonical_bytes,
        maximum_rendered_bytes=maximum_rendered_bytes,
    )
    if ranked == snapshot.ordered_resident_entries:
        return snapshot
    return _build_viewport_snapshot(
        runtime_session_id=snapshot.active_head.runtime_session_id,
        projection_revision=snapshot.projection_revision,
        active_head=snapshot.active_head,
        ranked=ranked,
    )


def _fit_newest_resident_suffix(
    ranked: tuple[PresentationHistoryRankedEntryView, ...],
    *,
    maximum_entries: int,
    maximum_canonical_bytes: int,
    maximum_rendered_bytes: int,
) -> tuple[PresentationHistoryRankedEntryView, ...]:
    if min(maximum_entries, maximum_canonical_bytes, maximum_rendered_bytes) < 1:
        return ()
    selected_reversed: list[PresentationHistoryRankedEntryView] = []
    canonical_bytes = 0
    rendered_bytes = 0
    for item in reversed(ranked):
        if len(selected_reversed) >= maximum_entries:
            break
        item_canonical_bytes = len(canonical_json_bytes(item.model_dump(mode="json")))
        item_rendered_bytes = _rendered_public_bytes(item)
        if (
            canonical_bytes + item_canonical_bytes > maximum_canonical_bytes
            or rendered_bytes + item_rendered_bytes > maximum_rendered_bytes
        ):
            break
        selected_reversed.append(item)
        canonical_bytes += item_canonical_bytes
        rendered_bytes += item_rendered_bytes
    return tuple(reversed(selected_reversed))


def _build_viewport_snapshot(
    *,
    runtime_session_id: str,
    projection_revision: int,
    active_head: PresentationHistoryActiveHeadFact,
    ranked: tuple[PresentationHistoryRankedEntryView, ...],
) -> PresentationHistoryViewportSnapshotFact:
    resident_bytes = sum(
        len(canonical_json_bytes(item.model_dump(mode="json"))) for item in ranked
    )
    return build_frozen_fact(
        PresentationHistoryViewportSnapshotFact,
        schema_version="presentation_history_viewport_snapshot.v1",
        runtime_session_id=runtime_session_id,
        projection_revision=projection_revision,
        active_head=active_head,
        ordered_resident_entries=ranked,
        latest_root_cursor_pair=_cursor_pair(
            active_head.confirmed_root_identity, ranked
        ),
        resident_cell_count=len(ranked),
        resident_canonical_bytes=resident_bytes,
        oldest_history_entry_id=(
            ranked[0].history_entry.history_entry_id if ranked else None
        ),
        oldest_placement_key=(
            ranked[0].history_entry.placement_key if ranked else None
        ),
        newest_history_entry_id=(
            ranked[-1].history_entry.history_entry_id if ranked else None
        ),
        newest_placement_key=(
            ranked[-1].history_entry.placement_key if ranked else None
        ),
        resident_vector_fingerprint=_resident_vector_fingerprint(ranked),
    )


__all__ = [
    "EMPTY_TAIL_MUTATION_ACCUMULATOR",
    "EMPTY_TAIL_SEGMENT_ACCUMULATOR",
    "EMPTY_TAIL_SOURCE_RANGE_ACCUMULATOR",
    "MAXIMUM_SNAPSHOT_RESIDENT_CANONICAL_BYTES",
    "MAXIMUM_SNAPSHOT_RESIDENT_RENDERED_BYTES",
    "PresentationHistoryViewportService",
    "fit_viewport_snapshot_resident_suffix",
]
