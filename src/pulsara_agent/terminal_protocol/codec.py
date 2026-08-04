"""Exact domain-to-wire mapping for the renderer-neutral terminal protocol.

This module is intentionally boring and exhaustive: protocol payloads are a
projection of the registered Python authority facts.  It never accepts caller
supplied dictionaries and it never reconstructs domain outcomes in the client.
"""

from __future__ import annotations

from pulsara_agent.ports.terminal_application import (
    ApprovalRequestView,
    McpInteractionView,
    PlanExitView,
    PlanQuestionView,
    TerminalCommandOutcome,
    TerminalUiSessionSnapshot,
)
from pulsara_agent.primitives.context import context_fingerprint
from pulsara_agent.primitives.frozen import build_frozen_fact
from pulsara_agent.primitives.presentation_history import (
    ActiveHeadRankBasisFact,
    AssistantMessageCell,
    AuditCell,
    CanonicalTranscriptHistorySourceFact,
    CompactionBoundaryCell,
    ConfirmedRootRankBasisFact,
    DurableAuditHistorySourceFact,
    ErrorCell,
    InteractionCell,
    ModelActivityCell,
    PresentationDataContentBlockFact,
    PresentationHistoryPlacementKeyFact,
    PresentationHistoryPageCursorFact,
    PresentationTextContentBlockFact,
    RecoveryCell,
    SubagentActivityCell,
    SystemNoticeCell,
    TerminalProcessActivityCell,
    ToolTerminalCell,
    ToolActivityCell,
    UserPromptCell,
)
from pulsara_agent.primitives.presentation_view import (
    BoundedOrderedResidentChangesFact,
    PresentationHistoryResidentRemoveFact,
    PresentationHistoryResidentUpsertFact,
    ResidentEntriesUnchangedFact,
    ResidentHistoryRebaseRequiredFact,
)
from pulsara_agent.terminal_protocol.generated import terminal_client_pb2 as wire
from pulsara_agent.terminal_protocol.generated.terminal_client_fingerprint import (
    canonical_protobuf_json_vector_bytes,
    install_protobuf_fingerprint,
    operational_activity_accumulator,
)
from pulsara_agent.runtime.terminal_presentation.observation import (
    OperationalActivityRemoval,
    OperationalActivitySnapshot,
)
from pulsara_agent.runtime.terminal_application.control_projection import (
    TERMINAL_ACTIVE_QUEUE_PROJECTION_CONTRACT_FINGERPRINT,
    ControlProjectionCursor,
    PromptQueueClientProjection,
    PromptQueueProjectionHead,
    TerminalControlProjectionSnapshot,
    TerminalControlSectionSourceVersion,
)


PROTOCOL_MAJOR = 2
PROTOCOL_MINOR = 0
PROTOCOL_SCHEMA_FINGERPRINT = (
    "sha256:61c53a72b867a1f10794959e0b27244833ca5488a965550b1881f252a22004e6"
)
MAXIMUM_FRAME_BYTES = 8 * 1024 * 1024
MAXIMUM_HISTORY_PAGE_CELLS = 256
MAXIMUM_HISTORY_PAGE_BYTES = 4 * 1024 * 1024
MAXIMUM_PINNED_HISTORY_ROOTS = 4
HEARTBEAT_INTERVAL_MS = 10_000
HEARTBEAT_GRACE_MS = 20_000
HEARTBEAT_MAXIMUM_MISSED_COUNT = 2
# Requests are serialized on one local connection. A long poll therefore must
# return before the next heartbeat is due.
MAXIMUM_OBSERVATION_WAIT_MS = HEARTBEAT_INTERVAL_MS // 2
SECRET_FRAME_MAXIMUM_BYTES = 64 * 1024
MAXIMUM_ACTIVE_QUEUE_ITEMS = 64
MAXIMUM_SERVER_CONTROL_NOTIFICATIONS = 16
MAXIMUM_OPERATIONAL_ACTIVITY_CELLS = 256
MAXIMUM_DURABLE_OBSERVATION_BYTES = 4 * 1024 * 1024
MAXIMUM_OPERATIONAL_OBSERVATION_BYTES = 1 * 1024 * 1024
MAXIMUM_CONTROL_OBSERVATION_BYTES = 256 * 1024
MAXIMUM_OBSERVATION_BATCH_BYTES = 6 * 1024 * 1024
OPERATIONAL_ACTIVITY_CONTRACT_FINGERPRINT = context_fingerprint(
    "terminal-operational-activity-contract:v1",
    {
        "maximum_cells": MAXIMUM_OPERATIONAL_ACTIVITY_CELLS,
        "maximum_bytes": MAXIMUM_OPERATIONAL_OBSERVATION_BYTES,
        "ordering": "coalesce_key",
    },
)

_PRESENTATION_SEMANTIC_ROLE_TO_WIRE = {
    "primary": wire.PRESENTATION_SEMANTIC_ROLE_PRIMARY,
    "secondary": wire.PRESENTATION_SEMANTIC_ROLE_SECONDARY,
    "diagnostic": wire.PRESENTATION_SEMANTIC_ROLE_DIAGNOSTIC,
    "code": wire.PRESENTATION_SEMANTIC_ROLE_CODE,
    "tool_arguments": wire.PRESENTATION_SEMANTIC_ROLE_TOOL_ARGUMENTS,
    "tool_result": wire.PRESENTATION_SEMANTIC_ROLE_TOOL_RESULT,
}
_PRESENTATION_VISIBILITY_TO_WIRE = {
    "always": wire.PRESENTATION_VISIBILITY_ALWAYS,
    "normal": wire.PRESENTATION_VISIBILITY_NORMAL,
    "diagnostic_only": wire.PRESENTATION_VISIBILITY_DIAGNOSTIC_ONLY,
}
_PRESENTATION_TOOL_RESULT_TO_WIRE = {
    "success": wire.PRESENTATION_TOOL_RESULT_SUCCESS,
    "error": wire.PRESENTATION_TOOL_RESULT_ERROR,
    "denied": wire.PRESENTATION_TOOL_RESULT_DENIED,
    "interrupted": wire.PRESENTATION_TOOL_RESULT_INTERRUPTED,
}
_PRESENTATION_INTERACTION_KIND_TO_WIRE = {
    "approval": wire.PRESENTATION_INTERACTION_APPROVAL,
    "plan": wire.PRESENTATION_INTERACTION_PLAN,
    "mcp_input": wire.PRESENTATION_INTERACTION_MCP_INPUT,
    "external_input": wire.PRESENTATION_INTERACTION_EXTERNAL_INPUT,
}
_PRESENTATION_INTERACTION_STATE_TO_WIRE = {
    "pending": wire.PRESENTATION_INTERACTION_PENDING,
    "resolved": wire.PRESENTATION_INTERACTION_RESOLVED,
    "cancelled": wire.PRESENTATION_INTERACTION_CANCELLED,
    "failed": wire.PRESENTATION_INTERACTION_FAILED,
}
_PRESENTATION_AUDIT_KIND_TO_WIRE = {
    "run_lifecycle": wire.PRESENTATION_AUDIT_RUN_LIFECYCLE,
    "suppressed_model_output": wire.PRESENTATION_AUDIT_SUPPRESSED_MODEL_OUTPUT,
    "permission": wire.PRESENTATION_AUDIT_PERMISSION,
    "interaction_lifecycle": wire.PRESENTATION_AUDIT_INTERACTION_LIFECYCLE,
    "subagent_lifecycle": wire.PRESENTATION_AUDIT_SUBAGENT_LIFECYCLE,
    "compaction_lifecycle": wire.PRESENTATION_AUDIT_COMPACTION_LIFECYCLE,
    "recovery_lifecycle": wire.PRESENTATION_AUDIT_RECOVERY_LIFECYCLE,
}
_PRESENTATION_SEVERITY_TO_WIRE = {
    "info": wire.PRESENTATION_SEVERITY_INFO,
    "warning": wire.PRESENTATION_SEVERITY_WARNING,
    "error": wire.PRESENTATION_SEVERITY_ERROR,
}
_PRESENTATION_RELATIVE_POSITION_TO_WIRE = {
    "before_first": wire.PRESENTATION_RELATIVE_POSITION_BEFORE_FIRST,
    "before_leaf": wire.PRESENTATION_RELATIVE_POSITION_BEFORE_LEAF,
    "canonical_leaf": wire.PRESENTATION_RELATIVE_POSITION_CANONICAL_LEAF,
    "after_leaf": wire.PRESENTATION_RELATIVE_POSITION_AFTER_LEAF,
    "ledger_gap": wire.PRESENTATION_RELATIVE_POSITION_LEDGER_GAP,
    "after_last": wire.PRESENTATION_RELATIVE_POSITION_AFTER_LAST,
}
_PRESENTATION_RELATIVE_POSITION_FROM_WIRE = {
    value: key for key, value in _PRESENTATION_RELATIVE_POSITION_TO_WIRE.items()
}
_OPERATIONAL_REPLACEMENT_TO_WIRE = {
    "replace_same_key": wire.OPERATIONAL_REPLACE_SAME_KEY,
    "expire_at_terminal": wire.OPERATIONAL_EXPIRE_AT_TERMINAL,
}
_OPERATIONAL_REMOVAL_REASON_TO_WIRE = {
    "durable_terminal": wire.OPERATIONAL_REMOVAL_DURABLE_TERMINAL,
    "owner_replaced": wire.OPERATIONAL_REMOVAL_OWNER_REPLACED,
    "explicit_retire": wire.OPERATIONAL_REMOVAL_EXPLICIT_RETIRE,
}
_MCP_INPUT_MODE_TO_WIRE = {
    "form": wire.MCP_INPUT_FORM,
    "url": wire.MCP_INPUT_URL,
}
_PRESENTATION_ROOT_RELATION_TO_WIRE = {
    "strict_prefix_extended": wire.STRICT_PREFIX_EXTENDED,
    "rewritten_generation": wire.REWRITTEN_GENERATION,
}
_PRESENTATION_PREVIOUS_CURSOR_TO_WIRE = {
    "retained_pinned": wire.RETAINED_PINNED,
}
_COMMAND_OUTCOME_STATUS_TO_WIRE = {
    "succeeded": wire.SUCCEEDED,
    "rejected": wire.REJECTED,
    "pending_confirmation": wire.PENDING_CONFIRMATION,
    "reconciliation_required": wire.RECONCILIATION_REQUIRED,
    "superseded_by_compatible_winner": wire.SUPERSEDED_BY_COMPATIBLE_WINNER,
}


def _closed_wire_value(mapping: dict[str, int], value: str, *, field: str) -> int:
    try:
        return mapping[value]
    except KeyError as exc:
        raise ValueError(f"unknown {field}: {value!r}") from exc


def _closed_domain_value(mapping: dict[int, str], value: int, *, field: str) -> str:
    try:
        return mapping[value]
    except KeyError as exc:
        raise ValueError(f"unknown wire {field}: {value!r}") from exc


def protocol_version() -> wire.ProtocolVersion:
    return wire.ProtocolVersion(
        major=PROTOCOL_MAJOR,
        minor=PROTOCOL_MINOR,
        schema_contract_fingerprint=PROTOCOL_SCHEMA_FINGERPRINT,
        minimum_compatible_minor=0,
    )


def attachment_to_wire(lease) -> wire.AttachmentIdentity:
    role = (
        wire.ATTACHMENT_ROLE_CONTROLLER
        if lease.role == "controller"
        else wire.ATTACHMENT_ROLE_OBSERVER
    )
    return wire.AttachmentIdentity(
        client_instance_id=lease.client_instance_id,
        attachment_id=lease.attachment_id,
        runtime_session_id=lease.runtime_session_id,
        attachment_generation=lease.attachment_generation,
        role=role,
        controller_generation=lease.controller_generation,
        issued_at_utc=lease.issued_at_utc,
        expires_at_utc=lease.expires_at_utc,
        identity_fingerprint=lease.identity_fingerprint,
    )


def event_reference_to_wire(reference) -> wire.EventReferenceView:
    return wire.EventReferenceView(
        runtime_session_id=reference.runtime_session_id,
        event_id=reference.event_id,
        sequence=reference.sequence,
        event_type=reference.event_type,
        payload_fingerprint=reference.payload_fingerprint,
    )


def placement_to_wire(placement) -> wire.PresentationHistoryPlacementKey:
    result = wire.PresentationHistoryPlacementKey(
        placement_key_contract_id=placement.placement_key_contract_id,
        placement_key_contract_version=placement.placement_key_contract_version,
        placement_key_contract_fingerprint=(
            placement.placement_key_contract_fingerprint
        ),
        relative_position_kind=_closed_wire_value(
            _PRESENTATION_RELATIVE_POSITION_TO_WIRE,
            placement.relative_position_kind,
            field="presentation relative position",
        ),
        source_ledger_sequence_or_zero=placement.source_ledger_sequence_or_zero,
        relative_local_ordinal=placement.relative_local_ordinal,
        stable_source_tiebreaker=placement.stable_source_tiebreaker,
        canonical_comparable_key_bytes=placement.canonical_comparable_key_bytes,
        placement_key_fingerprint=placement.placement_key_fingerprint,
    )
    if placement.canonical_spine_left_coordinate is not None:
        result.canonical_spine_left_coordinate = (
            placement.canonical_spine_left_coordinate
        )
    if placement.canonical_spine_right_coordinate is not None:
        result.canonical_spine_right_coordinate = (
            placement.canonical_spine_right_coordinate
        )
    return result


def _content_block_to_wire(block) -> wire.PresentationContentBlock:
    result = wire.PresentationContentBlock()
    if isinstance(block, PresentationTextContentBlockFact):
        result.text.CopyFrom(
            wire.PresentationTextBlock(
                text=block.text,
                text_utf8_bytes=block.text_utf8_bytes,
                semantic_role=_closed_wire_value(
                    _PRESENTATION_SEMANTIC_ROLE_TO_WIRE,
                    block.semantic_role,
                    field="presentation semantic role",
                ),
                block_fingerprint=block.block_fingerprint,
            )
        )
    elif isinstance(block, PresentationDataContentBlockFact):
        result.data.CopyFrom(
            wire.PresentationDataBlock(
                media_type=block.media_type,
                public_canonical_text=block.public_canonical_text,
                public_utf8_bytes=block.public_utf8_bytes,
                block_fingerprint=block.block_fingerprint,
            )
        )
    else:  # pragma: no cover - the domain union is closed
        raise TypeError("unknown presentation content block")
    return result


def _cell_common_to_wire(cell) -> wire.DurableCellCommon:
    return wire.DurableCellCommon(
        stable_cell_id=cell.stable_cell_id,
        semantic_revision=cell.semantic_revision,
        ordered_source_event_references=(
            event_reference_to_wire(item)
            for item in cell.ordered_source_event_references
        ),
        source_accumulator=cell.source_accumulator,
        visibility_policy=_closed_wire_value(
            _PRESENTATION_VISIBILITY_TO_WIRE,
            cell.visibility_policy,
            field="presentation visibility policy",
        ),
        content_blocks=(_content_block_to_wire(item) for item in cell.content_blocks),
        semantic_group_id=cell.semantic_group_id or "",
        cell_fingerprint=cell.cell_fingerprint,
    )


def cell_to_wire(cell) -> wire.DurableHistoryCell:
    result = wire.DurableHistoryCell()
    common = _cell_common_to_wire(cell)
    if isinstance(cell, UserPromptCell):
        result.user_prompt.CopyFrom(wire.UserPromptCell(common=common))
    elif isinstance(cell, AssistantMessageCell):
        result.assistant_message.CopyFrom(wire.AssistantMessageCell(common=common))
    elif isinstance(cell, ToolTerminalCell):
        result.tool_terminal.CopyFrom(
            wire.ToolTerminalCell(
                common=common,
                tool_call_id=cell.tool_call_id,
                tool_name=cell.tool_name,
                result_state=_closed_wire_value(
                    _PRESENTATION_TOOL_RESULT_TO_WIRE,
                    cell.result_state,
                    field="presentation tool result state",
                ),
            )
        )
    elif isinstance(cell, ErrorCell):
        result.error.CopyFrom(
            wire.ErrorCell(common=common, stable_error_code=cell.stable_error_code)
        )
    elif isinstance(cell, InteractionCell):
        result.interaction.CopyFrom(
            wire.InteractionCell(
                common=common,
                interaction_kind=_closed_wire_value(
                    _PRESENTATION_INTERACTION_KIND_TO_WIRE,
                    cell.interaction_kind,
                    field="presentation interaction kind",
                ),
                interaction_state=_closed_wire_value(
                    _PRESENTATION_INTERACTION_STATE_TO_WIRE,
                    cell.interaction_state,
                    field="presentation interaction state",
                ),
            )
        )
    elif isinstance(cell, CompactionBoundaryCell):
        result.compaction_boundary.CopyFrom(wire.CompactionBoundaryCell(common=common))
    elif isinstance(cell, RecoveryCell):
        result.recovery.CopyFrom(
            wire.RecoveryCell(common=common, recovery_kind=cell.recovery_kind)
        )
    elif isinstance(cell, AuditCell):
        result.audit.CopyFrom(
            wire.AuditCell(
                common=common,
                audit_kind=_closed_wire_value(
                    _PRESENTATION_AUDIT_KIND_TO_WIRE,
                    cell.audit_kind,
                    field="presentation audit kind",
                ),
                severity=_closed_wire_value(
                    _PRESENTATION_SEVERITY_TO_WIRE,
                    cell.severity,
                    field="presentation severity",
                ),
            )
        )
    elif isinstance(cell, SystemNoticeCell):
        result.system_notice.CopyFrom(
            wire.SystemNoticeCell(common=common, notice_kind=cell.notice_kind)
        )
    else:  # pragma: no cover - the domain union is closed
        raise TypeError("unknown durable presentation cell")
    return result


def operational_activity_to_wire(cell) -> wire.OperationalActivityCell:
    common = wire.OperationalActivityCommon(
        owner_kind=cell.owner_kind,
        owner_id=cell.owner_id,
        owner_generation=cell.owner_generation,
        operational_generation=cell.operational_generation,
        operational_cursor=cell.operational_cursor,
        coalesce_key=cell.coalesce_key,
        replacement_semantics=_closed_wire_value(
            _OPERATIONAL_REPLACEMENT_TO_WIRE,
            cell.replacement_semantics,
            field="operational replacement semantics",
        ),
        bounded_public_text=cell.bounded_public_text,
        activity_fingerprint=cell.activity_fingerprint,
    )
    result = wire.OperationalActivityCell()
    if isinstance(cell, ModelActivityCell):
        result.model_activity.CopyFrom(wire.ModelActivityCell(common=common))
    elif isinstance(cell, ToolActivityCell):
        result.tool_activity.CopyFrom(wire.ToolActivityCell(common=common))
    elif isinstance(cell, TerminalProcessActivityCell):
        result.terminal_process_activity.CopyFrom(
            wire.TerminalProcessActivityCell(common=common)
        )
    elif isinstance(cell, SubagentActivityCell):
        result.subagent_activity.CopyFrom(wire.SubagentActivityCell(common=common))
    else:  # pragma: no cover - the domain union is closed
        raise TypeError("unknown operational activity cell")
    return result


def operational_change_to_wire(change) -> wire.OperationalActivityChange:
    result = wire.OperationalActivityChange()
    if isinstance(change, OperationalActivityRemoval):
        result.remove.CopyFrom(
            wire.OperationalActivityRemove(
                operational_generation=change.operational_generation,
                operational_cursor=change.operational_cursor,
                owner_kind=change.owner_kind,
                owner_id=change.owner_id,
                owner_generation=change.owner_generation,
                coalesce_key=change.coalesce_key,
                expected_activity_fingerprint=(change.expected_activity_fingerprint),
                removal_reason=_closed_wire_value(
                    _OPERATIONAL_REMOVAL_REASON_TO_WIRE,
                    change.removal_reason,
                    field="operational removal reason",
                ),
                removal_fingerprint=change.removal_fingerprint,
            )
        )
    else:
        result.upsert.CopyFrom(operational_activity_to_wire(change))
    return result


def operational_snapshot_to_wire(
    snapshot: OperationalActivitySnapshot,
    *,
    request: wire.OperationalSnapshotRequest,
) -> wire.OperationalSnapshotFrame:
    cells = tuple(
        operational_activity_to_wire(item) for item in snapshot.ordered_activity_cells
    )
    if len(cells) > MAXIMUM_OPERATIONAL_ACTIVITY_CELLS:
        raise ValueError("operational activity snapshot exceeds its item bound")
    encoded_bytes = len(canonical_protobuf_json_vector_bytes(cells))
    if encoded_bytes > MAXIMUM_OPERATIONAL_OBSERVATION_BYTES:
        raise ValueError("operational activity snapshot exceeds its byte bound")
    frame = wire.OperationalSnapshotFrame(
        request_id=request.request_id,
        runtime_session_id=request.runtime_session_id,
        attachment_id=request.attachment_id,
        attachment_generation=request.attachment_generation,
        attachment_identity_fingerprint=request.attachment_identity_fingerprint,
        acknowledged_transport_binding_fingerprint=(
            request.current_transport_binding.binding_fingerprint
        ),
        operational_generation=snapshot.operational_generation,
        operational_cursor=snapshot.operational_cursor,
        ordered_activity_cells=cells,
        activity_count=len(cells),
        encoded_activity_bytes=encoded_bytes,
        activity_fingerprint_accumulator=operational_activity_accumulator(cells),
        operational_state_fingerprint=snapshot.snapshot_fingerprint,
        snapshot_contract_fingerprint=OPERATIONAL_ACTIVITY_CONTRACT_FINGERPRINT,
    )
    install_protobuf_fingerprint(
        "terminal-operational-snapshot-frame:v1",
        frame,
        own_field="snapshot_frame_fingerprint",
    )
    return frame


def _source_to_wire(source) -> wire.HistorySourceView:
    result = wire.HistorySourceView()
    if isinstance(source, CanonicalTranscriptHistorySourceFact):
        leaf = source.transcript_leaf_reference
        anchor = source.transcript_placement_anchor
        result.canonical_transcript.CopyFrom(
            wire.CanonicalTranscriptSourceView(
                transcript_leaf_id=leaf.reference_fingerprint,
                transcript_leaf_fingerprint=leaf.entry_fact_fingerprint,
                transcript_anchor_slot_key=anchor.stable_anchor_slot_key,
                transcript_anchor_fingerprint=anchor.anchor_reference_fingerprint,
                transcript_reducer_id=source.transcript_reducer_id,
                transcript_reducer_version=source.transcript_reducer_version,
                transcript_reducer_contract_fingerprint=(
                    source.transcript_reducer_contract_fingerprint
                ),
                source_fold_delta_fingerprint=source.source_fold_delta_fingerprint,
                source_leaf_change_ordinal=source.source_leaf_change_ordinal,
                source_fingerprint=source.source_fingerprint,
            )
        )
    elif isinstance(source, DurableAuditHistorySourceFact):
        result.durable_audit.CopyFrom(
            wire.DurableAuditSourceView(
                audit_cell_id=source.audit_cell_id,
                audit_cell_semantic_revision=source.audit_cell_semantic_revision,
                audit_cell_fingerprint=source.audit_cell_fingerprint,
                ordered_source_event_references=(
                    event_reference_to_wire(item)
                    for item in source.ordered_source_event_references
                ),
                presentation_policy_fingerprint=(
                    source.presentation_policy_fingerprint
                ),
                extractor_id=source.extractor_id,
                extractor_version=source.extractor_version,
                extractor_contract_fingerprint=source.extractor_contract_fingerprint,
                extractor_output_ordinal=source.extractor_output_ordinal,
                audit_anchor_kind=source.audit_placement_anchor.anchor_kind,
                audit_anchor_fingerprint=(
                    source.audit_placement_anchor.anchor_fingerprint
                ),
                source_fingerprint=source.source_fingerprint,
            )
        )
    else:  # pragma: no cover - the domain union is closed
        raise TypeError("unknown presentation source")
    return result


def entry_to_wire(ranked) -> wire.PresentationHistoryRankedEntry:
    entry = ranked.history_entry
    entry_wire = wire.PresentationHistoryEntry(
        runtime_session_id=entry.runtime_session_id,
        history_entry_id=entry.history_entry_id,
        placement_key=placement_to_wire(entry.placement_key),
        source=_source_to_wire(entry.source),
        durable_history_cell=cell_to_wire(entry.cell),
        entry_fingerprint=entry.entry_fingerprint,
    )
    basis_wire = wire.HistoryRankBasis()
    if isinstance(ranked.rank_basis, ConfirmedRootRankBasisFact):
        basis_wire.confirmed_root.CopyFrom(
            wire.ConfirmedRootRankBasis(
                history_root_identity_fingerprint=(
                    ranked.rank_basis.history_root_identity_fingerprint
                ),
                rank_basis_fingerprint=ranked.rank_basis.rank_basis_fingerprint,
            )
        )
    elif isinstance(ranked.rank_basis, ActiveHeadRankBasisFact):
        basis_wire.active_head.CopyFrom(
            wire.ActiveHeadRankBasis(
                history_active_head_fingerprint=(
                    ranked.rank_basis.history_active_head_fingerprint
                ),
                through_authority_sequence=ranked.rank_basis.through_authority_sequence,
                rank_basis_fingerprint=ranked.rank_basis.rank_basis_fingerprint,
            )
        )
    else:  # pragma: no cover - the domain union is closed
        raise TypeError("unknown presentation rank basis")
    return wire.PresentationHistoryRankedEntry(
        entry=entry_wire,
        root_local_display_rank=ranked.root_local_display_rank,
        rank_basis=basis_wire,
        ranked_view_fingerprint=ranked.ranked_view_fingerprint,
    )


def history_ranked_entry_vector_decoded_bytes(
    entries: tuple[wire.PresentationHistoryRankedEntry, ...],
) -> int:
    """Return the Protocol 2.0 decoded-page accounting for full entry carriers."""

    return sum(len(entry.SerializeToString(deterministic=True)) for entry in entries)


def root_to_wire(root) -> wire.PresentationHistoryRootIdentity:
    return wire.PresentationHistoryRootIdentity(
        runtime_session_id=root.runtime_session_id,
        history_projection_contract_fingerprint=(
            root.history_projection_contract_fingerprint
        ),
        materialization_policy_fingerprint=root.materialization_policy_fingerprint,
        tree_contract_fingerprint=root.tree_contract_fingerprint,
        placement_key_contract_id=root.placement_key_contract_id,
        placement_key_contract_version=root.placement_key_contract_version,
        placement_key_contract_fingerprint=root.placement_key_contract_fingerprint,
        checkpoint_generation=root.checkpoint_generation,
        checkpoint_fingerprint=root.checkpoint_fingerprint,
        projection_generation=root.projection_generation,
        projection_root_fingerprint=root.projection_root_fingerprint,
        through_authority_sequence=root.through_authority_sequence,
        presentation_source_segment_count=root.presentation_source_segment_count,
        presentation_source_prefix_accumulator=(
            root.presentation_source_prefix_accumulator
        ),
        presentation_policy_registry_contract_fingerprint=(
            root.presentation_policy_registry_contract_fingerprint
        ),
        audit_extractor_registry_contract_fingerprint=(
            root.audit_extractor_registry_contract_fingerprint
        ),
        root_identity_fingerprint=root.root_identity_fingerprint,
    )


def cursor_to_wire(cursor) -> wire.PresentationHistoryCursor:
    result = wire.PresentationHistoryCursor(
        runtime_session_id=cursor.runtime_session_id,
        root_identity=root_to_wire(cursor.history_root_identity),
        cursor_fingerprint=cursor.cursor_fingerprint,
    )
    if cursor.anchor_history_entry_id is not None:
        result.anchor_history_entry_id = cursor.anchor_history_entry_id
        result.anchor_placement_key.CopyFrom(
            placement_to_wire(cursor.anchor_placement_key)
        )
    return result


def cursor_pair_to_wire(pair) -> wire.PresentationHistoryLatestRootCursorPair:
    result = wire.PresentationHistoryLatestRootCursorPair(
        root_identity=root_to_wire(pair.root_identity),
        cursor_pair_fingerprint=pair.cursor_pair_fingerprint,
    )
    if pair.before_cursor is not None:
        result.before_cursor.CopyFrom(cursor_to_wire(pair.before_cursor))
    if pair.after_cursor is not None:
        result.after_cursor.CopyFrom(cursor_to_wire(pair.after_cursor))
    return result


def cursor_from_wire(message: wire.PresentationHistoryCursor, *, foundation_service):
    retained = foundation_service.retention_owner.resolve(
        message.root_identity.root_identity_fingerprint
    )
    if retained is None:
        return None
    identity, root = retained
    if root_to_wire(identity).SerializeToString(deterministic=True) != (
        message.root_identity.SerializeToString(deterministic=True)
    ):
        raise ValueError("terminal history root wire identity mismatch")
    if message.runtime_session_id != identity.runtime_session_id:
        raise ValueError("terminal history cursor crosses runtime sessions")
    has_entry = message.HasField("anchor_history_entry_id")
    has_key = message.HasField("anchor_placement_key")
    if has_entry != has_key:
        raise ValueError("terminal history cursor anchor is partial")
    anchor = None
    if has_entry:
        placement = message.anchor_placement_key
        anchor = build_frozen_fact(
            PresentationHistoryPlacementKeyFact,
            schema_version="presentation_history_placement_key.v1",
            placement_key_contract_id=placement.placement_key_contract_id,
            placement_key_contract_version=placement.placement_key_contract_version,
            placement_key_contract_fingerprint=(
                placement.placement_key_contract_fingerprint
            ),
            canonical_spine_left_coordinate=(
                placement.canonical_spine_left_coordinate
                if placement.HasField("canonical_spine_left_coordinate")
                else None
            ),
            canonical_spine_right_coordinate=(
                placement.canonical_spine_right_coordinate
                if placement.HasField("canonical_spine_right_coordinate")
                else None
            ),
            relative_position_kind=_closed_domain_value(
                _PRESENTATION_RELATIVE_POSITION_FROM_WIRE,
                placement.relative_position_kind,
                field="presentation relative position",
            ),
            source_ledger_sequence_or_zero=placement.source_ledger_sequence_or_zero,
            relative_local_ordinal=placement.relative_local_ordinal,
            stable_source_tiebreaker=placement.stable_source_tiebreaker,
            canonical_comparable_key_bytes=bytes(
                placement.canonical_comparable_key_bytes
            ),
        )
        if anchor.placement_key_fingerprint != placement.placement_key_fingerprint:
            raise ValueError("terminal history placement fingerprint mismatch")
        if placement_to_wire(anchor).SerializeToString(
            deterministic=True
        ) != message.anchor_placement_key.SerializeToString(deterministic=True):
            raise ValueError("terminal history cursor placement identity mismatch")
    cursor = build_frozen_fact(
        PresentationHistoryPageCursorFact,
        schema_version="presentation_history_page_cursor.v1",
        runtime_session_id=identity.runtime_session_id,
        history_root_identity=identity,
        anchor_history_entry_id=(
            message.anchor_history_entry_id if anchor is not None else None
        ),
        anchor_placement_key=anchor,
    )
    if cursor.cursor_fingerprint != message.cursor_fingerprint:
        raise ValueError("terminal history cursor fingerprint mismatch")
    return cursor


def _capacity_to_wire(capacity) -> wire.PresentationHistoryCapacityState:
    result = wire.PresentationHistoryCapacityState()
    kind = capacity.capacity_kind
    if kind == "available":
        result.available.CopyFrom(
            wire.AvailableHistoryCapacity(
                confirmed_entry_count=capacity.confirmed_entry_count,
                current_tail_worst_case_entry_count=(
                    capacity.current_tail_worst_case_entry_count
                ),
                active_growth_reservation_remaining_entry_count=(
                    capacity.active_growth_reservation_remaining_entry_count
                ),
                projected_ordinary_entry_count=capacity.projected_ordinary_entry_count,
                soft_rotation_threshold_entries=(
                    capacity.soft_rotation_threshold_entries
                ),
                minimum_ordinary_growth_quote_entries=(
                    capacity.minimum_ordinary_growth_quote_entries
                ),
                capacity_state_fingerprint=capacity.capacity_state_fingerprint,
            )
        )
    elif kind == "session_rotation_required":
        result.session_rotation_required.CopyFrom(
            wire.HistorySessionRotationRequired(
                confirmed_entry_count=capacity.confirmed_entry_count,
                current_tail_worst_case_entry_count=(
                    capacity.current_tail_worst_case_entry_count
                ),
                active_growth_reservation_remaining_entry_count=(
                    capacity.active_growth_reservation_remaining_entry_count
                ),
                projected_ordinary_entry_count=capacity.projected_ordinary_entry_count,
                soft_rotation_threshold_entries=(
                    capacity.soft_rotation_threshold_entries
                ),
                stable_reason=capacity.stable_reason,
                capacity_state_fingerprint=capacity.capacity_state_fingerprint,
            )
        )
    elif kind == "tree_capacity_exhausted":
        result.tree_capacity_exhausted.CopyFrom(
            wire.HistoryTreeCapacityExhausted(
                observed_entry_count=capacity.observed_entry_count,
                maximum_representable_entries=capacity.maximum_representable_entries,
                stable_fault_code=capacity.stable_fault_code,
                capacity_state_fingerprint=capacity.capacity_state_fingerprint,
            )
        )
    elif kind == "capacity_reconciliation_required":
        value = wire.HistoryCapacityReconciliationRequired(
            stable_fault_code=capacity.stable_fault_code,
            capacity_state_fingerprint=capacity.capacity_state_fingerprint,
        )
        if capacity.trusted_active_head_fingerprint is not None:
            value.trusted_active_head_fingerprint = (
                capacity.trusted_active_head_fingerprint
            )
        result.reconciliation_required.CopyFrom(value)
    else:  # pragma: no cover - the domain union is closed
        raise TypeError("unknown presentation capacity state")
    return result


def active_head_to_wire(head) -> wire.PresentationHistoryActiveHeadIdentity:
    return wire.PresentationHistoryActiveHeadIdentity(
        runtime_session_id=head.runtime_session_id,
        confirmed_root_identity=root_to_wire(head.confirmed_root_identity),
        tail_from_sequence_exclusive=head.tail_from_sequence_exclusive,
        through_authority_sequence=head.through_authority_sequence,
        tail_source_range_accumulator=head.tail_source_range_accumulator,
        tail_segment_count=head.tail_segment_count,
        ordered_tail_segment_accumulator=head.ordered_tail_segment_accumulator,
        tail_mutation_count=head.tail_mutation_count,
        ordered_tail_mutation_accumulator=head.ordered_tail_mutation_accumulator,
        resulting_resident_entry_count=head.resulting_resident_entry_count,
        resulting_resident_entry_accumulator=(
            head.resulting_resident_entry_accumulator
        ),
        capacity_state=_capacity_to_wire(head.capacity_state),
        active_head_fingerprint=head.active_head_fingerprint,
    )


def queue_item_to_wire(item) -> wire.QueueItemView:
    delivery_state = {
        "accepted_pending": wire.PROMPT_QUEUE_ACCEPTED_PENDING,
        "steer_reserved": wire.PROMPT_QUEUE_STEER_RESERVED,
        "follow_up_reserved": wire.PROMPT_QUEUE_FOLLOW_UP_RESERVED,
        "reconciliation_required": wire.PROMPT_QUEUE_RECONCILIATION_REQUIRED,
    }.get(item.delivery_state)
    retention_state = {
        "active": wire.PROMPT_QUEUE_CONTENT_ACTIVE,
    }.get(item.content_retention_state)
    requested_mode = {
        "auto": wire.PROMPT_QUEUE_REQUESTED_AUTO,
        "steer": wire.PROMPT_QUEUE_REQUESTED_STEER,
        "follow_up": wire.PROMPT_QUEUE_REQUESTED_FOLLOW_UP,
    }.get(item.requested_delivery_mode)
    resolved_mode = {
        "pending": wire.PROMPT_QUEUE_RESOLVED_PENDING,
        "steer": wire.PROMPT_QUEUE_RESOLVED_STEER,
        "follow_up": wire.PROMPT_QUEUE_RESOLVED_FOLLOW_UP,
    }.get(item.resolved_delivery_mode)
    if None in {delivery_state, retention_state, requested_mode, resolved_mode}:
        raise ValueError("terminal prompt queue item is not client-visible")
    return wire.QueueItemView(
        queue_item_id=item.queue_item_id,
        accepted_ordinal=item.accepted_ordinal,
        delivery_state=delivery_state,
        content_retention_state=retention_state,
        requested_delivery_mode=requested_mode,
        resolved_delivery_mode=resolved_mode,
        public_preview=item.public_preview,
        head_event_id=item.head_event_id,
        item_revision=item.item_revision,
        view_fingerprint=item.view_fingerprint,
    )


def interaction_to_wire(item) -> wire.PendingInteraction:
    result = wire.PendingInteraction()
    if isinstance(item, ApprovalRequestView):
        result.approval.CopyFrom(
            wire.ApprovalInteraction(
                interaction_id=item.interaction_id,
                run_id=item.run_id,
                tool_calls=(
                    wire.ToolApproval(tool_call_id=call_id, tool_name=name)
                    for call_id, name in item.tool_calls
                ),
                view_fingerprint=item.view_fingerprint,
            )
        )
    elif isinstance(item, PlanQuestionView):
        result.plan_question.CopyFrom(
            wire.PlanQuestionInteraction(
                interaction_id=item.interaction_id,
                run_id=item.run_id,
                question=item.question,
                options=(
                    wire.PlanOption(label=label, description=description)
                    for label, description in item.options
                ),
                allow_free_text=item.allow_free_text,
                view_fingerprint=item.view_fingerprint,
            )
        )
    elif isinstance(item, PlanExitView):
        value = wire.PlanExitInteraction(
            interaction_id=item.interaction_id,
            run_id=item.run_id,
            summary=item.summary,
            view_fingerprint=item.view_fingerprint,
        )
        if item.plan_artifact_id is not None:
            value.plan_artifact_id = item.plan_artifact_id
        result.plan_exit.CopyFrom(value)
    elif isinstance(item, McpInteractionView):
        result.mcp_input.CopyFrom(
            wire.McpInteraction(
                interaction_id=item.interaction_id,
                run_id=item.run_id,
                tool_call_id=item.tool_call_id,
                tool_name=item.tool_name,
                server_id=item.server_id,
                requests=(
                    wire.McpInputPublicRequest(
                        request_key=request.request_key,
                        mode=_closed_wire_value(
                            _MCP_INPUT_MODE_TO_WIRE,
                            request.mode,
                            field="MCP input mode",
                        ),
                        public_prompt=request.public_prompt,
                        schema_or_url_present=request.schema_or_url_present,
                    )
                    for request in item.requests
                ),
                view_fingerprint=item.view_fingerprint,
            )
        )
    else:  # pragma: no cover - the domain union is closed
        raise TypeError("unknown terminal interaction branch")
    return result


def _source_version_to_wire(
    item: TerminalControlSectionSourceVersion,
) -> wire.TerminalControlSectionSourceVersion:
    section_kind = {
        "session_lifecycle": wire.CONTROL_SESSION_LIFECYCLE,
        "run_control": wire.CONTROL_RUN_CONTROL,
        "pending_interaction": wire.CONTROL_PENDING_INTERACTION,
        "prompt_queue": wire.CONTROL_PROMPT_QUEUE,
        "notifications": wire.CONTROL_NOTIFICATIONS,
    }[item.section_kind]
    return wire.TerminalControlSectionSourceVersion(
        section_kind=section_kind,
        source_owner_id=item.source_owner_id,
        source_owner_generation=item.source_owner_generation,
        source_owner_revision=item.source_owner_revision,
        source_view_fingerprint=item.source_view_fingerprint,
        source_version_fingerprint=item.source_version_fingerprint,
    )


def _queue_head_to_wire(
    head: PromptQueueProjectionHead,
) -> wire.PromptQueueProjectionHead:
    result = wire.PromptQueueProjectionHead()
    if head.head_kind == "empty_genesis":
        result.empty_genesis.CopyFrom(
            wire.EmptyPromptQueueGenesisHead(
                checkpoint_generation=head.checkpoint_generation,
                checkpoint_through_sequence=head.checkpoint_through_sequence,
                checkpoint_fingerprint=head.checkpoint_fingerprint,
                checkpoint_transition_count=head.checkpoint_transition_count,
                bounded_tail_count=head.bounded_tail_count,
                head_receipt_fingerprint=head.head_receipt_fingerprint,
                empty_head_fingerprint=head.head_fingerprint,
            )
        )
    else:
        if head.head_event_id is None or head.head_event_payload_fingerprint is None:
            raise ValueError("committed prompt queue head is partial")
        result.committed.CopyFrom(
            wire.CommittedPromptQueueHead(
                checkpoint_generation=head.checkpoint_generation,
                checkpoint_through_sequence=head.checkpoint_through_sequence,
                checkpoint_fingerprint=head.checkpoint_fingerprint,
                checkpoint_transition_count=head.checkpoint_transition_count,
                checkpoint_transition_accumulator=(
                    head.checkpoint_transition_accumulator
                ),
                bounded_tail_first_sequence_or_zero=(
                    head.bounded_tail_first_sequence_or_zero
                ),
                bounded_tail_last_sequence_or_zero=(
                    head.bounded_tail_last_sequence_or_zero
                ),
                bounded_tail_count=head.bounded_tail_count,
                bounded_tail_accumulator=head.bounded_tail_accumulator,
                head_event_id=head.head_event_id,
                head_event_sequence=head.head_event_sequence,
                head_event_payload_fingerprint=(head.head_event_payload_fingerprint),
                head_receipt_fingerprint=head.head_receipt_fingerprint,
                committed_head_fingerprint=head.head_fingerprint,
            )
        )
    return result


def _queue_projection_to_wire(
    item: PromptQueueClientProjection,
) -> wire.PromptQueueClientProjection:
    return wire.PromptQueueClientProjection(
        projection_contract_id="terminal-active-prompt-queue-projection",
        projection_contract_version=1,
        projection_contract_fingerprint=(
            TERMINAL_ACTIVE_QUEUE_PROJECTION_CONTRACT_FINGERPRINT
        ),
        queue_head=_queue_head_to_wire(item.queue_head),
        queue_account_revision=item.queue_account_revision,
        ordered_active_items=(
            queue_item_to_wire(value) for value in item.ordered_active_items
        ),
        active_item_count=item.active_item_count,
        active_item_accumulator=item.active_item_accumulator,
        projection_fingerprint=item.projection_fingerprint,
    )


def control_cursor_to_wire(
    item: ControlProjectionCursor,
) -> wire.ControlProjectionCursor:
    return wire.ControlProjectionCursor(
        control_generation=item.control_generation,
        control_revision=item.control_revision,
        control_projection_fingerprint=item.control_projection_fingerprint,
        transition_prefix_accumulator=item.transition_prefix_accumulator,
        registry_contract_fingerprint=item.registry_contract_fingerprint,
        cursor_fingerprint=item.cursor_fingerprint,
    )


def control_snapshot_to_wire(
    snapshot: TerminalControlProjectionSnapshot,
) -> wire.TerminalControlProjectionSnapshot:
    versions = {item.section_kind: item for item in snapshot.view.section_versions}
    lifecycle = {
        "open": wire.TERMINAL_SESSION_OPEN,
        "closing": wire.TERMINAL_SESSION_CLOSING,
        "closed": wire.TERMINAL_SESSION_CLOSED,
    }[snapshot.view.lifecycle]
    lifecycle_view = wire.TerminalSessionLifecycleControlView(
        source_version=_source_version_to_wire(versions["session_lifecycle"]),
        lifecycle=lifecycle,
        view_fingerprint=versions["session_lifecycle"].source_view_fingerprint,
    )
    run_view = wire.TerminalRunControlView(
        source_version=_source_version_to_wire(versions["run_control"]),
        view_fingerprint=versions["run_control"].source_view_fingerprint,
    )
    if snapshot.view.active_run_id is not None:
        run_view.active_run_id = snapshot.view.active_run_id
    if snapshot.view.suspended_run_id is not None:
        run_view.suspended_run_id = snapshot.view.suspended_run_id
    if snapshot.view.stopping_run_id is not None:
        run_view.stopping_run_id = snapshot.view.stopping_run_id
    interaction_view = wire.TerminalPendingInteractionControlView(
        source_version=_source_version_to_wire(versions["pending_interaction"]),
        view_fingerprint=versions["pending_interaction"].source_view_fingerprint,
    )
    if snapshot.view.pending_interaction is not None:
        interaction_view.interaction.CopyFrom(
            interaction_to_wire(snapshot.view.pending_interaction)
        )
    queue_view = wire.TerminalPromptQueueControlView(
        source_version=_source_version_to_wire(versions["prompt_queue"]),
        projection=_queue_projection_to_wire(snapshot.view.prompt_queue),
        view_fingerprint=versions["prompt_queue"].source_view_fingerprint,
    )
    notifications = wire.TerminalServerNotificationProjection(
        source_version=_source_version_to_wire(versions["notifications"]),
        ordered_notifications=(),
        notification_count=0,
        notification_accumulator=context_fingerprint(
            "terminal-control-server-notifications:v1", ()
        ),
        view_fingerprint=versions["notifications"].source_view_fingerprint,
    )
    return wire.TerminalControlProjectionSnapshot(
        view=wire.TerminalControlProjectionView(
            runtime_session_id=snapshot.view.runtime_session_id,
            session_lifecycle=lifecycle_view,
            run_control=run_view,
            pending_interaction=interaction_view,
            prompt_queue=queue_view,
            server_notifications=notifications,
            control_view_fingerprint=snapshot.view.control_view_fingerprint,
        ),
        cursor=control_cursor_to_wire(snapshot.cursor),
        snapshot_fingerprint=snapshot.snapshot_fingerprint,
    )


def snapshot_to_wire(
    snapshot: TerminalUiSessionSnapshot,
    *,
    control_snapshot: TerminalControlProjectionSnapshot,
    request_id: str,
    validated_minimum_control_cursor_fingerprint: str | None = None,
) -> wire.ProjectionSnapshotFrame:
    viewport = snapshot.viewport
    result = wire.ProjectionSnapshotFrame(
        request_id=request_id,
        host_session_id=snapshot.host_session_id,
        runtime_session_id=snapshot.runtime_session_id,
        authority_high_water=snapshot.authority_high_water,
        projection_revision=snapshot.projection_revision,
        projection_contract_fingerprint=(
            viewport.active_head.confirmed_root_identity.history_projection_contract_fingerprint
        ),
        active_head=active_head_to_wire(viewport.active_head),
        ordered_resident_entries=(
            entry_to_wire(item) for item in viewport.ordered_resident_entries
        ),
        latest_root_cursor_pair=cursor_pair_to_wire(viewport.latest_root_cursor_pair),
        resident_vector_fingerprint=viewport.resident_vector_fingerprint,
        viewport_fingerprint=viewport.viewport_fingerprint,
        snapshot_fingerprint=snapshot.snapshot_fingerprint,
        control_projection_snapshot=control_snapshot_to_wire(control_snapshot),
    )
    if validated_minimum_control_cursor_fingerprint is not None:
        result.validated_minimum_observed_control_cursor_fingerprint = (
            validated_minimum_control_cursor_fingerprint
        )
    return result


def root_advanced_to_wire(fact, *, request_id: str):
    result = wire.PresentationHistoryRootAdvancedFrame(
        request_id=request_id,
        base_projection_revision=fact.base_projection_revision,
        resulting_projection_revision=fact.resulting_projection_revision,
        previous_active_head_fingerprint=fact.previous_active_head_fingerprint,
        resulting_active_head=active_head_to_wire(fact.resulting_active_head),
        latest_root_cursor_pair=cursor_pair_to_wire(fact.latest_root_cursor_pair),
        previous_root_relation=wire.PresentationHistoryRootCursorRelation(
            previous_root_identity=root_to_wire(
                fact.previous_root_relation.previous_root_identity
            ),
            resulting_root_identity=root_to_wire(
                fact.previous_root_relation.resulting_root_identity
            ),
            relation=_closed_wire_value(
                _PRESENTATION_ROOT_RELATION_TO_WIRE,
                fact.previous_root_relation.relation_kind,
                field="presentation root relation",
            ),
            previous_cursor=_closed_wire_value(
                _PRESENTATION_PREVIOUS_CURSOR_TO_WIRE,
                fact.previous_root_relation.previous_cursor_disposition,
                field="presentation previous cursor disposition",
            ),
            shared_prefix_entry_count=(
                fact.previous_root_relation.shared_prefix_entry_count
            ),
            shared_prefix_accumulator=(
                fact.previous_root_relation.shared_prefix_accumulator
            ),
            relation_fingerprint=fact.previous_root_relation.relation_fingerprint,
        ),
        consumed_checkpoint_candidate_cut_fingerprint=(
            fact.consumed_checkpoint_candidate_cut_fingerprint
        ),
        consumed_tail_prefix_through_sequence=(
            fact.consumed_tail_prefix_through_sequence
        ),
        consumed_tail_prefix_source_range_accumulator=(
            fact.consumed_tail_prefix_source_range_accumulator
        ),
        consumed_tail_prefix_segment_count=fact.consumed_tail_prefix_segment_count,
        consumed_tail_prefix_segment_accumulator=(
            fact.consumed_tail_prefix_segment_accumulator
        ),
        consumed_tail_prefix_mutation_count=(fact.consumed_tail_prefix_mutation_count),
        consumed_tail_prefix_mutation_accumulator=(
            fact.consumed_tail_prefix_mutation_accumulator
        ),
        retained_tail_suffix_from_sequence_exclusive=(
            fact.retained_tail_suffix_from_sequence_exclusive
        ),
        retained_tail_suffix_through_sequence=(
            fact.retained_tail_suffix_through_sequence
        ),
        retained_tail_suffix_source_range_accumulator=(
            fact.retained_tail_suffix_source_range_accumulator
        ),
        retained_tail_suffix_segment_count=fact.retained_tail_suffix_segment_count,
        retained_tail_suffix_segment_accumulator=(
            fact.retained_tail_suffix_segment_accumulator
        ),
        retained_tail_suffix_mutation_count=(fact.retained_tail_suffix_mutation_count),
        retained_tail_suffix_mutation_accumulator=(
            fact.retained_tail_suffix_mutation_accumulator
        ),
        checkpoint_full_confirmation_fingerprint=(
            fact.checkpoint_full_confirmation_fingerprint
        ),
        frame_fingerprint=fact.root_advanced_fingerprint,
    )
    transition = fact.resident_transition
    if isinstance(transition, ResidentEntriesUnchangedFact):
        result.resident_transition.unchanged.CopyFrom(
            wire.ResidentEntriesUnchanged(
                before_resident_vector_fingerprint=(
                    transition.before_resident_vector_fingerprint
                ),
                after_resident_vector_fingerprint=(
                    transition.after_resident_vector_fingerprint
                ),
                exact_equivalence_proof_fingerprint=(
                    transition.exact_equivalence_proof_fingerprint
                ),
                transition_fingerprint=transition.transition_fingerprint,
            )
        )
    elif isinstance(transition, BoundedOrderedResidentChangesFact):
        value = wire.BoundedOrderedResidentChanges(
            before_resident_vector_fingerprint=(
                transition.before_resident_vector_fingerprint
            ),
            after_resident_vector_fingerprint=(
                transition.after_resident_vector_fingerprint
            ),
            change_count=transition.change_count,
            encoded_change_bytes=transition.encoded_change_bytes,
            transition_limits_policy_fingerprint=(
                transition.transition_limits_policy_fingerprint
            ),
            ordered_change_accumulator=transition.ordered_change_accumulator,
            transition_fingerprint=transition.transition_fingerprint,
        )
        for change in transition.ordered_changes:
            change_wire = wire.PresentationHistoryResidentChange()
            if isinstance(change, PresentationHistoryResidentUpsertFact):
                upsert = wire.PresentationHistoryResidentUpsert(
                    history_entry_id=change.history_entry_id,
                    placement_key=placement_to_wire(change.placement_key),
                    resulting_ranked_entry=entry_to_wire(change.resulting_ranked_entry),
                    change_fingerprint=change.change_fingerprint,
                )
                if change.expected_previous_entry_fingerprint is not None:
                    upsert.expected_previous_entry_fingerprint = (
                        change.expected_previous_entry_fingerprint
                    )
                change_wire.upsert.CopyFrom(upsert)
            elif isinstance(change, PresentationHistoryResidentRemoveFact):
                change_wire.remove.CopyFrom(
                    wire.PresentationHistoryResidentRemove(
                        history_entry_id=change.history_entry_id,
                        placement_key=placement_to_wire(change.placement_key),
                        expected_previous_entry_fingerprint=(
                            change.expected_previous_entry_fingerprint
                        ),
                        change_fingerprint=change.change_fingerprint,
                    )
                )
            else:  # pragma: no cover - the domain union is closed
                raise TypeError("unknown resident transition change")
            value.ordered_changes.append(change_wire)
        result.resident_transition.bounded_changes.CopyFrom(value)
    elif isinstance(transition, ResidentHistoryRebaseRequiredFact):
        result.resident_transition.rebase_required.CopyFrom(
            wire.ResidentHistoryRebaseRequired(
                before_resident_vector_fingerprint=(
                    transition.before_resident_vector_fingerprint
                ),
                target_root_identity=root_to_wire(transition.target_root_identity),
                target_active_head_fingerprint=(
                    transition.target_active_head_fingerprint
                ),
                stable_reason=transition.stable_reason,
                bounded_rebase_or_snapshot_token=(
                    transition.bounded_rebase_or_snapshot_token
                ),
                token_generation=transition.token_generation,
                expires_at_utc=transition.expires_at_utc,
                transition_fingerprint=transition.transition_fingerprint,
            )
        )
    else:  # pragma: no cover - the domain union is closed
        raise TypeError("unknown resident transition")
    return result


def outcome_to_wire(
    outcome: TerminalCommandOutcome, *, request_id: str
) -> wire.CommandOutcome:
    return wire.CommandOutcome(
        request_id=request_id,
        outcome_status=_closed_wire_value(
            _COMMAND_OUTCOME_STATUS_TO_WIRE,
            outcome.status,
            field="command outcome status",
        ),
        command_id=outcome.command_id,
        target_id=outcome.target_id,
        target_generation=outcome.target_generation,
        public_result_code=outcome.public_result_code,
        public_result_text=outcome.public_result_text,
        durable_reference_ids=outcome.durable_reference_ids,
        query_token=outcome.query_token,
        outcome_fingerprint=outcome.outcome_fingerprint,
    )


__all__ = [
    "HEARTBEAT_GRACE_MS",
    "HEARTBEAT_INTERVAL_MS",
    "HEARTBEAT_MAXIMUM_MISSED_COUNT",
    "MAXIMUM_FRAME_BYTES",
    "MAXIMUM_HISTORY_PAGE_BYTES",
    "MAXIMUM_HISTORY_PAGE_CELLS",
    "MAXIMUM_PINNED_HISTORY_ROOTS",
    "MAXIMUM_OBSERVATION_WAIT_MS",
    "MAXIMUM_ACTIVE_QUEUE_ITEMS",
    "MAXIMUM_SERVER_CONTROL_NOTIFICATIONS",
    "MAXIMUM_OPERATIONAL_ACTIVITY_CELLS",
    "MAXIMUM_DURABLE_OBSERVATION_BYTES",
    "MAXIMUM_OPERATIONAL_OBSERVATION_BYTES",
    "MAXIMUM_CONTROL_OBSERVATION_BYTES",
    "MAXIMUM_OBSERVATION_BATCH_BYTES",
    "PROTOCOL_MAJOR",
    "PROTOCOL_MINOR",
    "PROTOCOL_SCHEMA_FINGERPRINT",
    "SECRET_FRAME_MAXIMUM_BYTES",
    "active_head_to_wire",
    "attachment_to_wire",
    "cursor_from_wire",
    "cursor_pair_to_wire",
    "control_cursor_to_wire",
    "control_snapshot_to_wire",
    "cursor_to_wire",
    "entry_to_wire",
    "history_ranked_entry_vector_decoded_bytes",
    "interaction_to_wire",
    "outcome_to_wire",
    "operational_activity_to_wire",
    "operational_change_to_wire",
    "operational_snapshot_to_wire",
    "placement_to_wire",
    "protocol_version",
    "root_advanced_to_wire",
    "root_to_wire",
    "snapshot_to_wire",
]
