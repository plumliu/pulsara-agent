"""Effective worker ancestry, using the canonical reader for every segment.

No history body, imported rows, or execution state is created. A segment's own
cut controls tool closure/late-result visibility; the child cut never widens it.
"""

from __future__ import annotations

from dataclasses import dataclass, fields, replace
from time import monotonic

from pulsara_agent.conversation_kernel.repository_errors import (
    ConversationKernelConflict,
)
from pulsara_agent.model_input.contracts import (
    CanonicalInputOriginKind,
    CanonicalModelInputSnapshot,
    CompactionActiveRequestLocation,
    CompactionContinuationMode,
    ContextBindingBaseKind,
    FrozenCanonicalCompileSnapshot,
    FrozenProviderInputItem,
    FrozenProviderInputItemKind,
    FrozenRetainedHistoricalRequest,
    PreparedProviderInputCut,
    canonical_compile_snapshot_fingerprint,
    canonical_model_input_snapshot_fingerprint,
    context_binding_compile_fact_fingerprint,
)
from pulsara_agent.model_input.provider_replay import (
    FrozenCanonicalProviderDispatchRead,
    freeze_provider_replay_manifest_cut,
)
from pulsara_agent.conversation_kernel.compaction.contracts import (
    FrozenCompactionSummary,
    provider_input_item_canonical_expanded_bytes,
)
from pulsara_agent.llm.input import LLMTextPart
from pulsara_agent.primitives.context import canonical_json_bytes, context_fingerprint


def validate_worker_history_source(
    connection, *, session_id, source_task_id, through_sequence, binding_revision_id
):
    source = connection.execute(
        """SELECT task.status, turn.id AS turn_id, initial.entry_sequence,
                  revision.base_kind, revision.source_through_sequence
           FROM pulsara_v3.subagent_tasks AS task
           JOIN pulsara_v3.turns AS turn ON turn.session_id=task.session_id
            AND turn.scope_subagent_task_id=task.id
           JOIN pulsara_v3.transcript_entries AS initial
            ON initial.session_id=turn.session_id AND initial.id=turn.initial_entry_id
           JOIN pulsara_v3.turn_context_binding_revisions AS revision
            ON revision.session_id=turn.session_id AND revision.turn_id=turn.id
            AND revision.id=%s
           WHERE task.session_id=%s AND task.id=%s""",
        (binding_revision_id, session_id, source_task_id),
    ).fetchone()
    if (
        source is None
        or source["status"] not in {"COMPLETED", "FAILED", "CANCELLED", "INTERRUPTED"}
        or through_sequence < int(source["entry_sequence"])
        or through_sequence < int(source["source_through_sequence"])
    ):
        raise ConversationKernelConflict(
            "worker history source/cut/binding is unavailable"
        )
    # A cut must be an actual entry in this source scope, never another worker's head.
    if (
        connection.execute(
            "SELECT 1 FROM pulsara_v3.transcript_entries WHERE session_id=%s "
            "AND scope_subagent_task_id=%s AND entry_sequence=%s",
            (session_id, source_task_id, through_sequence),
        ).fetchone()
        is None
    ):
        raise ConversationKernelConflict("worker history cut is not a source frontier")
    return str(source["turn_id"])


@dataclass(frozen=True, slots=True)
class WorkerHistoryReadPlan:
    """Disposable scope cuts and the one effective base; canonical rows own all truth."""

    segments: tuple[PreparedProviderInputCut, ...]
    snapshot_segment: PreparedProviderInputCut | None
    source_floor: int


def worker_history_segments(connection, cut, *, deadline_monotonic, maximum_items):
    """Return oldest-to-newest cuts and the nearest base plus its uncovered tail.

    Each uncompressed segment has at least its real objective. The existing
    per-input item budget bounds this operation, not the lifetime/depth of a tree.
    """
    segments = []
    snapshot_segment = None
    source_floor = 0
    seen = set()
    current = cut
    while True:
        if monotonic() >= deadline_monotonic:
            raise TimeoutError("worker history read deadline exceeded")
        row = connection.execute(
            """SELECT turn.scope_subagent_task_id, revision.base_kind, revision.source_through_sequence,
                      task.history_source_task_id, task.history_cut_sequence,
                      task.history_context_binding_revision_id
               FROM pulsara_v3.turns AS turn
               JOIN pulsara_v3.turn_context_binding_revisions AS revision
                ON revision.session_id=turn.session_id AND revision.turn_id=turn.id
                AND revision.id=%s
               LEFT JOIN pulsara_v3.subagent_tasks AS task
                ON task.session_id=turn.session_id AND task.id=turn.scope_subagent_task_id
               WHERE turn.session_id=%s AND turn.id=%s""",
            (current.context_binding_revision_id, current.session_id, current.turn_id),
        ).fetchone()
        if row is None:
            raise ConversationKernelConflict("worker effective binding is absent")
        if row["scope_subagent_task_id"] is None:
            return None
        task_id = str(row["scope_subagent_task_id"])
        if task_id in seen:
            raise ConversationKernelConflict("worker history ancestry is cyclic")
        seen.add(task_id)
        segments.append(current)
        if len(segments) > maximum_items:
            raise ConversationKernelConflict(
                "worker history exceeds provider input item budget"
            )
        if snapshot_segment is None and row["base_kind"] == "SNAPSHOT":
            snapshot_segment = current
            source_floor = int(row["source_through_sequence"])
        if row["history_source_task_id"] is None:
            break
        # A child snapshot may preserve a protected tool tail in an ancestor.
        # Skip only covered ancestors, not every ancestor merely because a base exists.
        if int(row["history_cut_sequence"]) <= source_floor:
            break
        turn_id = validate_worker_history_source(
            connection,
            session_id=current.session_id,
            source_task_id=str(row["history_source_task_id"]),
            through_sequence=int(row["history_cut_sequence"]),
            binding_revision_id=str(row["history_context_binding_revision_id"]),
        )
        current = PreparedProviderInputCut(
            session_id=current.session_id,
            turn_id=turn_id,
            context_binding_revision_id=str(row["history_context_binding_revision_id"]),
            provider_input_through_sequence=int(row["history_cut_sequence"]),
        )
    return WorkerHistoryReadPlan(
        tuple(reversed(segments)), snapshot_segment, source_floor
    )


def worker_initial_material_items(connection, *, cut, floor):
    row = connection.execute(
        """SELECT turn.initial_entry_id, initial.entry_sequence,
                  task.parent_context_body, task.dependency_context_body,
                  task.terminal_material_body
           FROM pulsara_v3.turns AS turn
           JOIN pulsara_v3.transcript_entries AS initial
            ON initial.session_id=turn.session_id AND initial.id=turn.initial_entry_id
           JOIN pulsara_v3.subagent_tasks AS task
            ON task.session_id=turn.session_id AND task.id=turn.scope_subagent_task_id
           WHERE turn.session_id=%s AND turn.id=%s""",
        (cut.session_id, cut.turn_id),
    ).fetchone()
    if (
        row is None
        or not floor < row["entry_sequence"] <= cut.provider_input_through_sequence
    ):
        return ()
    return tuple(
        FrozenProviderInputItem(
            item_kind=FrozenProviderInputItemKind.INITIAL_CONTEXT_MATERIAL,
            source_entry_id=str(row["initial_entry_id"]),
            source_entry_sequence=int(row["entry_sequence"]),
            source_turn_id=cut.turn_id,
            content=(
                LLMTextPart(
                    canonical_json_bytes(
                        {
                            "pulsara_initial_context": {
                                "source": kind,
                                "content_semantics": "UNTRUSTED_COLLABORATION_DATA",
                                "body": row[column],
                            }
                        }
                    ).decode("utf-8")
                ),
            ),
        )
        for kind, column in (
            ("PARENT_CONTEXT", "parent_context_body"),
            ("DEPENDENCY_RESULTS", "dependency_context_body"),
            ("TERMINAL_MATERIAL", "terminal_material_body"),
        )
        if row[column] is not None
    )


def _refingerprint(value, **changes):
    """Rebuild an existing compile DTO's existing boundary digest, not a new proof."""
    pending = type(value).__new__(type(value))
    for field in fields(value):
        object.__setattr__(
            pending, field.name, changes.get(field.name, getattr(value, field.name))
        )
    if isinstance(value, FrozenCanonicalCompileSnapshot):
        changes["canonical_read_cut_fingerprint"] = (
            canonical_compile_snapshot_fingerprint(pending)
        )
    else:
        changes["fact_fingerprint"] = context_binding_compile_fact_fingerprint(pending)
    return replace(value, **changes)


def historicalize_snapshot(connection, item, segment):
    from pulsara_agent.conversation_kernel.compaction.prompt import (
        build_compaction_snapshot_carrier,
    )

    carrier = item.content
    retained = carrier.retained_historical_requests
    active = carrier.active_request
    if active is not None:
        row = connection.execute(
            "SELECT initial.id FROM pulsara_v3.turns AS turn "
            "JOIN pulsara_v3.transcript_entries AS initial "
            "ON initial.session_id=turn.session_id AND initial.id=turn.initial_entry_id "
            "WHERE turn.session_id=%s AND turn.id=%s AND initial.id=%s "
            "AND initial.entry_sequence=%s",
            (
                segment.session_id,
                segment.turn_id,
                active.entry_id,
                active.entry_sequence,
            ),
        ).fetchone()
        if (
            row is None
            or active.item_kind is not FrozenProviderInputItemKind.USER
            or active.input_origin is not CanonicalInputOriginKind.SUBAGENT_OBJECTIVE
        ):
            raise ConversationKernelConflict(
                "worker snapshot active objective attribution is invalid"
            )
        if active.location is CompactionActiveRequestLocation.SNAPSHOT_EXACT:
            retained += (
                FrozenRetainedHistoricalRequest(
                    active.item_kind, active.input_origin, active.content
                ),
            )
    historical = build_compaction_snapshot_carrier(
        summary=FrozenCompactionSummary(
            body=carrier.earlier_context_summary,
            body_utf8_bytes=len(carrier.earlier_context_summary.encode("utf-8")),
            body_digest=context_fingerprint(
                "pulsara.frozen-compaction-summary.v2-guided-freeform",
                carrier.earlier_context_summary,
            ),
        ),
        recent_human_requests=carrier.recent_human_requests,
        continuation_mode=CompactionContinuationMode.AWAIT_NEXT_USER,
        active_request=None,
        retained_historical_requests=retained,
    )
    return replace(item, content=historical)


def read_effective_worker_dispatch(
    reader, connection, cut, segments, deadline_monotonic, *, historical_scope=False
):
    """Compose bounded typed reads using the same canonical lowering for each scope."""
    plan = segments
    items, closures, late, manifests = [], [], [], []
    base = None
    base_dispatch = None
    canonical_bytes = 0
    if plan.snapshot_segment is not None:
        base_dispatch = reader.read_frozen_dispatch(
            plan.snapshot_segment,
            deadline_monotonic=deadline_monotonic,
            _connection=connection,
            _single_scope=True,
            _historical_scope=historical_scope or plan.snapshot_segment != cut,
        )
        base = base_dispatch.compile_snapshot.context_binding_fact
        snapshot_item = base_dispatch.compile_snapshot.canonical_input.items[0]
        if snapshot_item.item_kind is not FrozenProviderInputItemKind.CONTEXT_SNAPSHOT:
            raise ConversationKernelConflict("effective worker snapshot item is absent")
        if plan.snapshot_segment != cut:
            snapshot_item = historicalize_snapshot(
                connection, snapshot_item, plan.snapshot_segment
            )
        items.append(snapshot_item)
        canonical_bytes = snapshot_item.content.canonical_expanded_bytes
    for segment in plan.segments:
        dispatch = (
            base_dispatch
            if segment == plan.snapshot_segment
            else reader.read_frozen_dispatch(
                segment,
                deadline_monotonic=deadline_monotonic,
                _connection=connection,
                _single_scope=True,
                _historical_scope=historical_scope or segment != cut,
                _canonical_byte_budget=reader._maximum_canonical_bytes
                - canonical_bytes,
                _effective_source_floor=plan.source_floor,
                _include_snapshot=False,
            )
        )
        facts = dispatch.compile_snapshot
        if base is None:
            base = facts.context_binding_fact
        segment_items = tuple(
            item
            for item in facts.canonical_input.items
            if item.item_kind is not FrozenProviderInputItemKind.CONTEXT_SNAPSHOT
        )
        items.extend(segment_items)
        canonical_bytes += sum(
            provider_input_item_canonical_expanded_bytes(item) for item in segment_items
        )
        closures.extend(facts.canonical_input.closures)
        late.extend(facts.canonical_input.late_outcomes)
        manifests.extend(dispatch.replay_manifest_cut.manifests)
        if (
            len(items) > reader._maximum_items
            or canonical_bytes > reader._maximum_canonical_bytes
        ):
            raise ConversationKernelConflict(
                "effective worker history exceeds provider input bounds"
            )
    # Last segment owns current identity/permissions; the nearest adopted base owns the snapshot.
    old = facts.canonical_input
    joined_items, joined_closures, joined_late = (
        tuple(items),
        tuple(closures),
        tuple(late),
    )
    canonical = CanonicalModelInputSnapshot(
        identity=old.identity,
        items=joined_items,
        canonical_expanded_bytes=canonical_bytes,
        closures=joined_closures,
        late_outcomes=joined_late,
        snapshot_fingerprint=canonical_model_input_snapshot_fingerprint(
            identity=old.identity,
            items=joined_items,
            canonical_expanded_bytes=canonical_bytes,
            closures=joined_closures,
            late_outcomes=joined_late,
        ),
    )
    binding = _refingerprint(
        facts.context_binding_fact,
        base_kind=base.base_kind,
        context_snapshot_id=base.context_snapshot_id,
        source_through_sequence=base.source_through_sequence
        if base.base_kind is ContextBindingBaseKind.SNAPSHOT
        else 0,
        context_base_semantic_identity=base.context_base_semantic_identity,
    )
    joined = _refingerprint(
        facts, canonical_input=canonical, context_binding_fact=binding
    )
    manifest_cut = freeze_provider_replay_manifest_cut(
        session_id=cut.session_id,
        scope=dispatch.replay_manifest_cut.scope,
        context_binding_revision_id=cut.context_binding_revision_id,
        provider_input_through_sequence=cut.provider_input_through_sequence,
        manifests=tuple(manifests),
    )
    if manifest_cut.aggregate_manifest_utf8_bytes > reader._maximum_canonical_bytes:
        raise ConversationKernelConflict(
            "effective worker replay metadata exceeds input bound"
        )
    return FrozenCanonicalProviderDispatchRead(
        compile_snapshot=joined,
        replay_manifest_cut=manifest_cut,
        composite_fingerprint=context_fingerprint(
            "pulsara.canonical-provider-dispatch-read:v1",
            {
                "compile": joined.canonical_read_cut_fingerprint,
                "replay_manifest_cut": manifest_cut.cut_fingerprint,
            },
        ),
    )
