"""Canonical Stage 2 readers and provider-context rematerialization.

The reader never replays ``agent_events``.  It reads canonical relations at a
single repeatable-read cut and lowers incomplete physical effects into
provider-only closure items.  Those closures are not durable facts and never
authorize a retry.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from hashlib import sha256
import json
from typing import Mapping, Protocol, Sequence

from psycopg import IsolationLevel
from psycopg.rows import dict_row

from pulsara_agent.conversation_kernel.repository import (
    ConversationKernelConflict,
)
from pulsara_agent.model_input.contracts import (
    ApprovedPlanMaterializationFact,
    CanonicalInputOriginKind,
    CanonicalModelInputIdentity,
    CanonicalModelInputSnapshot,
    FrozenCanonicalCompileSnapshot,
    ContextBindingBaseKind,
    FrozenContextBindingCompileFact,
    FrozenPlanHandoffCompileFact,
    FrozenPlanWorkflowCompileFact,
    FrozenPreviousTurnOutcomeCompileFact,
    FrozenToolObservationFreshnessCompileFact,
    FrozenProviderInputItem,
    FrozenProviderInputItemKind,
    LateToolOutcomeObservation,
    MAXIMUM_CANONICAL_PROVIDER_INPUT_BYTES,
    MAXIMUM_CANONICAL_PROVIDER_INPUT_ITEMS,
    ModelInputScopeKind,
    ProviderToolCall,
    ProviderToolResultClosure,
    ProviderToolResultClosureKind,
    ProviderToolResultContextMetadata,
    PreviousTurnOutcomeKind,
    AcceptedAssistantDisposition,
    PreparedProviderInputCut,
    canonical_model_input_identity_fingerprint,
    canonical_model_input_snapshot_fingerprint,
    canonical_compile_snapshot_fingerprint,
    context_binding_compile_fact_fingerprint,
    plan_handoff_compile_fact_fingerprint,
    plan_workflow_compile_fact_fingerprint,
    approved_plan_materialization_fingerprint,
    provider_input_item_fingerprint,
    previous_turn_outcome_fingerprint,
    tool_observation_freshness_fingerprint,
)
from pulsara_agent.model_input.continuity import (
    FULL_HISTORY_CONTEXT_BASE_IDENTITY,
    PROVIDER_MESSAGE_LOWERING_CONTRACT,
)
from pulsara_agent.ports.artifact import (
    ToolOutputArtifactDisposition,
    ToolOutputArtifactUnavailabilityReason,
    ToolResultDisplayKind,
)
from pulsara_agent.ports.tool_execution import (
    ToolOutputSourceCoverage,
    ToolOutputSourceCoverageReason,
)
from pulsara_agent.primitives.context import (
    FrozenJsonObjectFact,
    context_fingerprint,
    freeze_json,
)
from pulsara_agent.primitives.permission import PermissionMode
from pulsara_agent.primitives.plan_workflow import (
    PlanApprovedMaterializationDisposition,
    PlanHandoffKind,
    PlanInteractionBinding,
    PlanWorkflowEnteredBy,
    PlanWorkflowStatus,
    extract_plan_draft,
)
from pulsara_agent.primitives.run_permission import (
    FrozenRunPermissionSnapshot,
    RunPermissionAdmissionSource,
    RunPermissionOverlay,
)
from pulsara_agent.primitives.tool_observation import (
    FrozenToolObservationTimingFact,
    ToolObservationDurationDisposition,
    ToolObservationOrigin,
    canonical_utc_timestamp,
    provider_visible_turn_ref,
    tool_observation_timing_fingerprint,
)
from pulsara_agent.ports.terminal_observation import (
    TerminalDeliveryCoverage,
    TerminalObservationContentV1,
    TerminalObservationKind,
)
from pulsara_agent.storage.postgres_connection_provider import (
    PostgresConnectionLane,
    VerifiedPostgresConnectionProviderProtocol,
)


class CanonicalProviderContinuityFailureKind(StrEnum):
    BLOB_READER_UNAVAILABLE = "BLOB_READER_UNAVAILABLE"
    BLOB_UNAVAILABLE_OR_CORRUPT = "BLOB_UNAVAILABLE_OR_CORRUPT"
    CONTENT_SIZE_MISMATCH = "CONTENT_SIZE_MISMATCH"
    CONTENT_DIGEST_MISMATCH = "CONTENT_DIGEST_MISMATCH"
    INVALID_UTF8 = "INVALID_UTF8"


class CanonicalProviderContinuityError(ConversationKernelConflict):
    """Canonical history cannot be admitted to a new provider operation."""

    def __init__(
        self,
        kind: CanonicalProviderContinuityFailureKind,
        *,
        content_identity: str | None = None,
    ) -> None:
        self.kind = kind
        self.content_identity = content_identity
        suffix = "" if content_identity is None else f" ({content_identity})"
        super().__init__(f"canonical provider continuity failed: {kind.value}{suffix}")


ProviderInputItemKind = FrozenProviderInputItemKind
ProviderInputItem = FrozenProviderInputItem


class CanonicalBlobReader(Protocol):
    def read_exact(
        self,
        *,
        blob_id: str,
        expected_digest: str,
        expected_size: int,
        deadline_monotonic: float,
    ) -> bytes: ...


class CanonicalProviderInputReader:
    def __init__(
        self,
        connection_provider: VerifiedPostgresConnectionProviderProtocol,
        *,
        blob_reader: CanonicalBlobReader | None = None,
        maximum_items: int = MAXIMUM_CANONICAL_PROVIDER_INPUT_ITEMS,
        maximum_canonical_bytes: int = MAXIMUM_CANONICAL_PROVIDER_INPUT_BYTES,
    ) -> None:
        if maximum_items < 1 or maximum_canonical_bytes < 1:
            raise ValueError("provider input bounds must be positive")
        self._provider = connection_provider
        self._blob_reader = blob_reader
        self._maximum_items = maximum_items
        self._maximum_canonical_bytes = maximum_canonical_bytes

    def read_frozen_snapshot(
        self,
        cut: PreparedProviderInputCut,
        *,
        deadline_monotonic: float,
    ) -> CanonicalModelInputSnapshot:
        return self.read_frozen_compile_snapshot(
            cut, deadline_monotonic=deadline_monotonic
        ).canonical_input

    def read_frozen_compile_snapshot(
        self,
        cut: PreparedProviderInputCut,
        *,
        deadline_monotonic: float,
    ) -> FrozenCanonicalCompileSnapshot:
        with self._provider.connection(
            lane=PostgresConnectionLane.INSPECTOR,
            row_factory=dict_row,
            deadline_monotonic=deadline_monotonic,
            isolation_level=IsolationLevel.REPEATABLE_READ,
        ) as connection:
            binding = connection.execute(
                """
                SELECT t.workspace_id, t.conversation_scope_kind,
                       t.scope_subagent_task_id, t.initial_entry_id,
                       t.status AS turn_status,
                       t.current_context_binding_revision_id,
                       t.permission_snapshot_id,
                       t.requested_permission_mode,
                       t.effective_permission_mode,
                       t.permission_admission_source,
                       t.permission_overlay,
                       t.permission_plan_context_ordinal,
                       t.permission_plan_workflow_id,
                       t.permission_plan_revision_at_admission,
                       t.permission_inherited_from_turn_id,
                       t.permission_contract_id,
                       t.permission_contract_fingerprint,
                       t.permission_snapshot_fingerprint,
                       r.revision_ordinal, r.base_kind,
                       r.context_snapshot_id, r.source_through_sequence,
                       s.latest_entry_sequence,
                       initial_entry.entry_sequence AS current_initial_entry_sequence
                FROM pulsara_v3.turns AS t
                JOIN pulsara_v3.turn_context_binding_revisions AS r
                  ON r.session_id = t.session_id
                 AND r.id = %s
                 AND r.turn_id = t.id
                JOIN pulsara_v3.sessions AS s ON s.id = t.session_id
                JOIN pulsara_v3.transcript_entries AS initial_entry
                  ON initial_entry.session_id = t.session_id
                 AND initial_entry.id = t.initial_entry_id
                 AND initial_entry.turn_id = t.id
                WHERE t.session_id = %s AND t.id = %s
                """,
                (
                    cut.context_binding_revision_id,
                    cut.session_id,
                    cut.turn_id,
                ),
            ).fetchone()
            if binding is None:
                raise ConversationKernelConflict("provider binding is absent")
            if (
                binding["current_context_binding_revision_id"]
                != cut.context_binding_revision_id
            ):
                raise ConversationKernelConflict("provider binding revision is stale")
            if cut.provider_input_through_sequence > int(
                binding["latest_entry_sequence"]
            ):
                raise ConversationKernelConflict(
                    "provider input cut exceeds canonical head"
                )

            scope_kind = str(binding["conversation_scope_kind"])
            scope_task_id = binding["scope_subagent_task_id"]
            previous_turn_outcome_fact, freshness_fact = self._load_round7_scope_facts(
                connection,
                cut=cut,
                workspace_id=str(binding["workspace_id"]),
                scope_kind=ModelInputScopeKind(scope_kind),
                scope_task_id=(
                    None if scope_task_id is None else str(scope_task_id)
                ),
                current_initial_entry_sequence=int(
                    binding["current_initial_entry_sequence"]
                ),
            )
            source_floor = 0
            items: list[FrozenProviderInputItem] = []
            canonical_bytes = 0
            snapshot = None
            if binding["base_kind"] == "SNAPSHOT":
                snapshot = connection.execute(
                    """
                    SELECT id, blob_id, content_digest, content_size,
                           content_media_type, content_codec, source_through_sequence
                    FROM pulsara_v3.context_snapshots
                    WHERE session_id = %s AND id = %s
                    """,
                    (cut.session_id, binding["context_snapshot_id"]),
                ).fetchone()
                if snapshot is None:
                    raise ConversationKernelConflict("context snapshot is absent")
                source_floor = int(snapshot["source_through_sequence"])
                if source_floor != int(binding["source_through_sequence"]):
                    raise ConversationKernelConflict(
                        "snapshot binding source cut drifted"
                    )
            binding_values = {
                "binding_revision_id": cut.context_binding_revision_id,
                "revision_ordinal": int(binding["revision_ordinal"]),
                "base_kind": ContextBindingBaseKind(str(binding["base_kind"])),
                "context_snapshot_id": (
                    None
                    if binding["context_snapshot_id"] is None
                    else str(binding["context_snapshot_id"])
                ),
                "source_through_sequence": int(binding["source_through_sequence"]),
                "context_base_semantic_identity": (
                    FULL_HISTORY_CONTEXT_BASE_IDENTITY
                    if snapshot is None
                    else context_fingerprint(
                        "pulsara:context-snapshot-base-semantic-identity:v1",
                        {
                            "snapshot_id": str(snapshot["id"]),
                            "blob_id": str(snapshot["blob_id"]),
                            "digest": str(snapshot["content_digest"]),
                            "size": int(snapshot["content_size"]),
                            "media_type": str(snapshot["content_media_type"]),
                            "codec": str(snapshot["content_codec"]),
                            "source_through_sequence": int(
                                snapshot["source_through_sequence"]
                            ),
                            "lowering": PROVIDER_MESSAGE_LOWERING_CONTRACT,
                        },
                    )
                ),
            }
            provisional_binding = FrozenContextBindingCompileFact.__new__(
                FrozenContextBindingCompileFact
            )
            for name, value in binding_values.items():
                object.__setattr__(provisional_binding, name, value)
            object.__setattr__(provisional_binding, "fact_fingerprint", "")
            binding_fact = FrozenContextBindingCompileFact(
                **binding_values,
                fact_fingerprint=context_binding_compile_fact_fingerprint(
                    provisional_binding
                ),
            )

            entries = connection.execute(
                """
                SELECT e.id, e.turn_id, e.entry_sequence, e.entry_kind,
                       e.context_binding_revision_id,
                       e.provider_input_through_sequence,
                       e.source_job_id, e.source_subagent_result_id,
                       e.source_plan_workflow_id,
                       e.source_plan_interaction_id,
                       e.source_plan_handoff_kind,
                       e.blob_id, e.content_digest, e.content_size,
                       e.content_media_type, e.content_codec,
                       t.status AS owning_turn_status
                FROM pulsara_v3.transcript_entries AS e
                JOIN pulsara_v3.turns AS t
                  ON t.session_id = e.session_id AND t.id = e.turn_id
                WHERE e.session_id = %s
                  AND e.entry_sequence > %s
                  AND e.entry_sequence <= %s
                  AND e.conversation_scope_kind = %s
                  AND e.scope_subagent_task_id IS NOT DISTINCT FROM %s
                ORDER BY e.entry_sequence
                LIMIT %s
                """,
                (
                    cut.session_id,
                    source_floor,
                    cut.provider_input_through_sequence,
                    scope_kind,
                    scope_task_id,
                    self._maximum_items + 1,
                ),
            ).fetchall()
            if len(entries) > self._maximum_items:
                raise ConversationKernelConflict("provider input item bound exceeded")

            entry_ids = tuple(str(row["id"]) for row in entries)
            block_metadata = self._load_block_metadata(
                connection, cut.session_id, entry_ids
            )
            tool_state = self._load_tool_state(
                connection,
                cut.session_id,
                entry_ids,
                provider_input_through_sequence=(
                    cut.provider_input_through_sequence
                ),
            )
            self._preflight_physical_bytes(
                snapshot=snapshot,
                entries=entries,
                blocks=block_metadata,
            )
            entry_payloads = self._load_entry_payloads(
                connection,
                cut.session_id,
                tuple(
                    str(row["id"])
                    for row in entries
                    if row["entry_kind"]
                    in (
                        "USER_MESSAGE",
                        "USER_STEER",
                        "TERMINAL_OBSERVATION",
                        "TOOL_RESULT",
                        "PLAN_CONTINUATION",
                    )
                ),
            )
            block_payloads = self._load_block_payloads(
                connection,
                cut.session_id,
                tuple(str(row["id"]) for row in block_metadata),
            )
            blocks_by_entry = self._join_block_payloads(block_metadata, block_payloads)
            remaining_bytes = _RemainingReadBudget(self._maximum_canonical_bytes)
            if snapshot is not None:
                snapshot_payload = self._load_snapshot_payload(
                    connection,
                    cut.session_id,
                    str(snapshot["id"]),
                )
                snapshot_row = dict(snapshot)
                snapshot_row["inline_content"] = snapshot_payload
                content = self._read_content(
                    snapshot_row,
                    deadline_monotonic=deadline_monotonic,
                    remaining_bytes=remaining_bytes,
                )
                text = _decode_provider_text(content, str(snapshot["content_codec"]))
                items.append(
                    ProviderInputItem(
                        item_kind=ProviderInputItemKind.CONTEXT_SNAPSHOT,
                        source_entry_id=None,
                        source_entry_sequence=source_floor,
                        source_turn_id=None,
                        text=text,
                    )
                )
                canonical_bytes += len(content)
            next_assistant_cut = _next_assistant_cuts(entries)
            closures: list[ProviderToolResultClosure] = []
            late: list[LateToolOutcomeObservation] = []
            late_items: list[tuple[int, ProviderInputItem]] = []

            for row in entries:
                entry_id = str(row["id"])
                sequence = int(row["entry_sequence"])
                kind = str(row["entry_kind"])
                if kind == "TOOL_RESULT":
                    continue
                if kind in ("USER_MESSAGE", "USER_STEER"):
                    content = self._read_content(
                        _with_inline_payload(row, entry_payloads[entry_id]),
                        deadline_monotonic=deadline_monotonic,
                        remaining_bytes=remaining_bytes,
                    )
                    canonical_bytes += len(content)
                    text = _decode_provider_text(content, str(row["content_codec"]))
                    items.append(
                        ProviderInputItem(
                            item_kind=ProviderInputItemKind.USER,
                            source_entry_id=entry_id,
                            source_entry_sequence=sequence,
                            source_turn_id=str(row["turn_id"]),
                            text=text,
                            input_origin=_canonical_input_origin(
                                row, scope_kind=scope_kind
                            ),
                        )
                    )
                    continue
                if kind == "TERMINAL_OBSERVATION":
                    content = self._read_content(
                        _with_inline_payload(row, entry_payloads[entry_id]),
                        deadline_monotonic=deadline_monotonic,
                        remaining_bytes=remaining_bytes,
                    )
                    canonical_bytes += len(content)
                    text = _decode_provider_text(content, str(row["content_codec"]))
                    if (
                        str(row["content_media_type"])
                        != "application/vnd.pulsara.terminal-observation+json"
                        or str(row["content_codec"]) != "utf-8"
                    ):
                        raise ConversationKernelConflict(
                            "terminal observation content descriptor is invalid"
                        )
                    _validate_terminal_observation_content(content)
                    items.append(
                        ProviderInputItem(
                            item_kind=ProviderInputItemKind.TERMINAL_OBSERVATION,
                            source_entry_id=entry_id,
                            source_entry_sequence=sequence,
                            source_turn_id=str(row["turn_id"]),
                            text=text,
                        )
                    )
                    continue
                if kind == "PLAN_CONTINUATION":
                    content = self._read_content(
                        _with_inline_payload(row, entry_payloads[entry_id]),
                        deadline_monotonic=deadline_monotonic,
                        remaining_bytes=remaining_bytes,
                    )
                    storage_text = _decode_provider_text(
                        content, str(row["content_codec"])
                    )
                    provider_text = _project_plan_continuation_storage(
                        row, storage_text
                    )
                    canonical_bytes += len(provider_text.encode("utf-8"))
                    items.append(
                        ProviderInputItem(
                            item_kind=ProviderInputItemKind.PLAN_CONTINUATION,
                            source_entry_id=entry_id,
                            source_entry_sequence=sequence,
                            source_turn_id=str(row["turn_id"]),
                            text=provider_text,
                            input_origin=CanonicalInputOriginKind.PLAN_CONTINUATION,
                        )
                    )
                    continue
                if kind not in ("ASSISTANT_MESSAGE", "ASSISTANT_TOOL_REQUEST"):
                    raise ConversationKernelConflict(
                        "provider input entry kind is not closed"
                    )
                # The assistant parent content is a storage-only block manifest.
                # Provider continuity, byte admission and semantic lowering are
                # owned exclusively by the ordered TEXT/DATA/TOOL_CALL blocks.
                # Reading or charging the manifest here would let a carrier that
                # is never sent to the model veto an otherwise valid call.
                blocks = blocks_by_entry.get(entry_id, ())
                text_parts = tuple(
                    self._block_text(
                        item,
                        deadline_monotonic=deadline_monotonic,
                        remaining_bytes=remaining_bytes,
                    )
                    for item in blocks
                    if item["block_kind"] in ("TEXT", "DATA")
                )
                canonical_bytes += sum(len(part.encode("utf-8")) for part in text_parts)
                calls = tuple(
                    ProviderToolCall(
                        tool_call_id=str(item["tool_call_id"]),
                        tool_name=str(item["tool_name"]),
                        arguments=_freeze_tool_arguments(
                            item["tool_arguments"],
                            expected_physical_bytes=int(item["tool_arguments_size"]),
                            remaining_bytes=remaining_bytes,
                        ),
                    )
                    for item in blocks
                    if item["block_kind"] == "TOOL_CALL"
                )
                items.append(
                    ProviderInputItem(
                        item_kind=(
                            ProviderInputItemKind.ASSISTANT_TOOL_REQUEST
                            if calls
                            else ProviderInputItemKind.ASSISTANT
                        ),
                        source_entry_id=entry_id,
                        source_entry_sequence=sequence,
                        source_turn_id=str(row["turn_id"]),
                        text="".join(text_parts),
                        tool_calls=calls,
                    )
                )
                if not calls:
                    continue
                target_cut = next_assistant_cut.get(entry_id)
                if target_cut is None:
                    # This read is preparing the first provider call after the
                    # request.  Its immutable cut is the only truthful answer
                    # to whether a result is ordinarily visible.  Once that
                    # call commits an assistant entry, _next_assistant_cuts()
                    # returns the exact same attributed cut, so a historical
                    # request/result pair never changes representation merely
                    # because its owning turn became terminal.
                    target_cut = cut.provider_input_through_sequence
                for call in calls:
                    state = tool_state.get((entry_id, call.tool_call_id), {})
                    result = state.get("result")
                    result_sequence = (
                        None if result is None else int(result["entry_sequence"])
                    )
                    if result is not None and result_sequence <= target_cut:
                        result_content = self._read_content(
                            _with_inline_payload(
                                result,
                                entry_payloads[str(result["result_entry_id"])],
                            ),
                            deadline_monotonic=deadline_monotonic,
                            remaining_bytes=remaining_bytes,
                        )
                        canonical_bytes += len(result_content)
                        items.append(
                            ProviderInputItem(
                                item_kind=ProviderInputItemKind.TOOL_RESULT,
                                source_entry_id=str(result["result_entry_id"]),
                                source_entry_sequence=result_sequence,
                                source_turn_id=str(result["result_turn_id"]),
                                text=_decode_provider_text(
                                    result_content, str(result["content_codec"])
                                ),
                                tool_call_id=call.tool_call_id,
                                tool_result_context=_tool_result_metadata(result),
                                tool_result_body_text=_decode_provider_text(
                                    result_content, str(result["content_codec"])
                                ),
                            )
                        )
                        continue
                    if state.get("plan_interaction_status") == "ABORTED":
                        closure_kind = (
                            ProviderToolResultClosureKind.PLAN_INTERACTION_ABORTED
                        )
                    else:
                        closure_kind = (
                            ProviderToolResultClosureKind.INTERRUPTED_MAY_HAVE_PARTIALLY_EXECUTED
                            if state.get("attempt_id") is not None
                            else ProviderToolResultClosureKind.INTERRUPTED_BEFORE_DISPATCH
                        )
                    closure = ProviderToolResultClosure(
                        assistant_entry_id=entry_id,
                        tool_call_id=call.tool_call_id,
                        closure_kind=closure_kind,
                        target_provider_input_through_sequence=target_cut,
                    )
                    closures.append(closure)
                    closure_text = (
                        "Plan interaction ended before a user decision was "
                        "accepted; no physical tool effect occurred."
                        if closure_kind
                        is ProviderToolResultClosureKind.PLAN_INTERACTION_ABORTED
                        else _canonical_json_text(
                            {
                                "schema_version": "provider_tool_result_closure.v1",
                                "tool_call_id": call.tool_call_id,
                                "disposition": closure_kind.value,
                            }
                        )
                    )
                    canonical_bytes += len(closure_text.encode("utf-8"))
                    items.append(
                        ProviderInputItem(
                            item_kind=ProviderInputItemKind.TOOL_RESULT_CLOSURE,
                            source_entry_id=None,
                            source_entry_sequence=sequence,
                            source_turn_id=str(row["turn_id"]),
                            text=closure_text,
                            tool_call_id=call.tool_call_id,
                        )
                    )
                    if (
                        result is not None
                        and result_sequence <= cut.provider_input_through_sequence
                    ):
                        observation = LateToolOutcomeObservation(
                            assistant_entry_id=entry_id,
                            tool_call_id=call.tool_call_id,
                            result_entry_id=str(result["result_entry_id"]),
                            result_entry_sequence=result_sequence,
                            result_state=str(result["result_state"]),
                        )
                        late.append(observation)
                        result_content = self._read_content(
                            _with_inline_payload(
                                result,
                                entry_payloads[str(result["result_entry_id"])],
                            ),
                            deadline_monotonic=deadline_monotonic,
                            remaining_bytes=remaining_bytes,
                        )
                        late_text = _canonical_json_text(
                            {
                                "schema_version": "late_tool_outcome_observation.v1",
                                "tool_call_id": call.tool_call_id,
                                "result_state": result["result_state"],
                                "result": _decode_provider_text(
                                    result_content, str(result["content_codec"])
                                ),
                            }
                        )
                        canonical_bytes += len(late_text.encode("utf-8"))
                        late_items.append(
                            (
                                result_sequence,
                                ProviderInputItem(
                                    item_kind=ProviderInputItemKind.LATE_TOOL_OUTCOME,
                                    source_entry_id=str(result["result_entry_id"]),
                                    source_entry_sequence=result_sequence,
                                    source_turn_id=str(result["result_turn_id"]),
                                    text=late_text,
                                    tool_call_id=call.tool_call_id,
                                    tool_result_context=_tool_result_metadata(result),
                                    tool_result_body_text=_decode_provider_text(
                                        result_content,
                                        str(result["content_codec"]),
                                    ),
                                ),
                            )
                        )

            # Late outcomes retain their real canonical sequence but never
            # replace the closure paired with the historical request.
            for late_sequence, late_item in sorted(
                late_items, key=lambda item: item[0]
            ):
                insert_at = len(items)
                for index, item in enumerate(items):
                    if (
                        item.source_entry_sequence is not None
                        and item.source_entry_sequence > late_sequence
                    ):
                        insert_at = index
                        break
                items.insert(insert_at, late_item)
            if len(items) > self._maximum_items:
                raise ConversationKernelConflict("provider input item bound exceeded")
            if canonical_bytes > self._maximum_canonical_bytes:
                raise ConversationKernelConflict("provider input byte bound exceeded")
            pure_scope = ModelInputScopeKind(scope_kind)
            identity_fingerprint = canonical_model_input_identity_fingerprint(
                session_id=cut.session_id,
                turn_id=cut.turn_id,
                initial_entry_id=str(binding["initial_entry_id"]),
                context_binding_revision_id=cut.context_binding_revision_id,
                provider_input_through_sequence=cut.provider_input_through_sequence,
                conversation_scope_kind=pure_scope,
                scope_subagent_task_id=scope_task_id,
            )
            identity = CanonicalModelInputIdentity(
                session_id=cut.session_id,
                turn_id=cut.turn_id,
                initial_entry_id=str(binding["initial_entry_id"]),
                context_binding_revision_id=cut.context_binding_revision_id,
                provider_input_through_sequence=cut.provider_input_through_sequence,
                conversation_scope_kind=pure_scope,
                scope_subagent_task_id=scope_task_id,
                identity_fingerprint=identity_fingerprint,
            )
            frozen_items = tuple(items)
            frozen_closures = tuple(closures)
            frozen_late = tuple(late)
            canonical_input = CanonicalModelInputSnapshot(
                identity=identity,
                items=frozen_items,
                canonical_utf8_bytes=canonical_bytes,
                snapshot_fingerprint=canonical_model_input_snapshot_fingerprint(
                    identity=identity,
                    items=frozen_items,
                    canonical_utf8_bytes=canonical_bytes,
                    closures=frozen_closures,
                    late_outcomes=frozen_late,
                ),
                closures=frozen_closures,
                late_outcomes=frozen_late,
            )
            permission = _permission_snapshot_from_binding(binding)
            workflow_fact = _plan_workflow_compile_fact(
                connection,
                cut=cut,
                binding=binding,
                permission=permission,
            )
            handoff_fact, approved_fact = _plan_handoff_compile_facts(
                connection,
                cut=cut,
                binding=binding,
                items=frozen_items,
            )
            provisional = FrozenCanonicalCompileSnapshot.__new__(
                FrozenCanonicalCompileSnapshot
            )
            object.__setattr__(provisional, "canonical_input", canonical_input)
            object.__setattr__(provisional, "context_binding_fact", binding_fact)
            object.__setattr__(provisional, "run_permission_snapshot", permission)
            object.__setattr__(provisional, "plan_workflow_fact", workflow_fact)
            object.__setattr__(provisional, "plan_handoff_fact", handoff_fact)
            object.__setattr__(
                provisional, "approved_plan_materialization_fact", approved_fact
            )
            object.__setattr__(
                provisional, "previous_turn_outcome_fact", previous_turn_outcome_fact
            )
            object.__setattr__(
                provisional, "tool_observation_freshness_fact", freshness_fact
            )
            object.__setattr__(provisional, "canonical_read_cut_fingerprint", "")
            return FrozenCanonicalCompileSnapshot(
                canonical_input=canonical_input,
                context_binding_fact=binding_fact,
                run_permission_snapshot=permission,
                plan_workflow_fact=workflow_fact,
                plan_handoff_fact=handoff_fact,
                approved_plan_materialization_fact=approved_fact,
                previous_turn_outcome_fact=previous_turn_outcome_fact,
                tool_observation_freshness_fact=freshness_fact,
                canonical_read_cut_fingerprint=(
                    canonical_compile_snapshot_fingerprint(provisional)
                ),
            )

    def _load_block_metadata(
        self, connection, session_id: str, entry_ids: Sequence[str]
    ) -> tuple[Mapping[str, object], ...]:
        if not entry_ids:
            return ()
        rows = tuple(
            connection.execute(
                """
                SELECT id, assistant_entry_id, block_ordinal, block_kind,
                       tool_call_id, tool_name, blob_id, content_digest,
                       content_size, content_media_type, content_codec,
                       CASE WHEN inline_content IS NULL THEN 0
                            ELSE octet_length(inline_content) END
                           AS inline_content_size,
                       CASE WHEN tool_arguments IS NULL THEN 0
                            ELSE octet_length(tool_arguments::text) END
                           AS tool_arguments_size
                FROM pulsara_v3.assistant_message_blocks
                WHERE session_id = %s AND assistant_entry_id = ANY(%s)
                ORDER BY assistant_entry_id, block_ordinal
                LIMIT %s
                """,
                (session_id, list(entry_ids), self._maximum_items + 1),
            ).fetchall()
        )
        if len(rows) > self._maximum_items:
            raise ConversationKernelConflict("provider input block bound exceeded")
        if (
            sum(1 for row in rows if row["block_kind"] == "TOOL_CALL")
            > self._maximum_items
        ):
            raise ConversationKernelConflict("provider input tool-call bound exceeded")
        return rows


def _permission_snapshot_from_binding(
    binding: Mapping[str, object],
) -> FrozenRunPermissionSnapshot:
    return FrozenRunPermissionSnapshot(
        snapshot_id=str(binding["permission_snapshot_id"]),
        requested_mode=PermissionMode(str(binding["requested_permission_mode"])),
        effective_mode=PermissionMode(str(binding["effective_permission_mode"])),
        admission_source=RunPermissionAdmissionSource(
            str(binding["permission_admission_source"])
        ),
        overlay=RunPermissionOverlay(str(binding["permission_overlay"])),
        plan_context_ordinal_at_admission=int(
            binding["permission_plan_context_ordinal"]
        ),
        plan_workflow_id=(
            None
            if binding["permission_plan_workflow_id"] is None
            else str(binding["permission_plan_workflow_id"])
        ),
        plan_workflow_revision_at_admission=(
            None
            if binding["permission_plan_revision_at_admission"] is None
            else int(binding["permission_plan_revision_at_admission"])
        ),
        inherited_from_turn_id=(
            None
            if binding["permission_inherited_from_turn_id"] is None
            else str(binding["permission_inherited_from_turn_id"])
        ),
        permission_contract_id=str(binding["permission_contract_id"]),
        permission_contract_fingerprint=str(
            binding["permission_contract_fingerprint"]
        ),
        snapshot_fingerprint=str(binding["permission_snapshot_fingerprint"]),
    )


def _plan_workflow_compile_fact(
    connection,
    *,
    cut: PreparedProviderInputCut,
    binding: Mapping[str, object],
    permission: FrozenRunPermissionSnapshot,
) -> FrozenPlanWorkflowCompileFact | None:
    if permission.overlay is RunPermissionOverlay.NONE:
        return None
    row = connection.execute(
        """
        SELECT * FROM pulsara_v3.plan_workflows
        WHERE session_id = %s AND id = %s AND status = 'ACTIVE'
        """,
        (cut.session_id, permission.plan_workflow_id),
    ).fetchone()
    if row is None:
        raise ConversationKernelConflict("active Plan compile workflow is absent")
    provisional = FrozenPlanWorkflowCompileFact.__new__(
        FrozenPlanWorkflowCompileFact
    )
    values = {
        "session_id": cut.session_id,
        "workspace_id": str(binding["workspace_id"]),
        "turn_id": cut.turn_id,
        "permission_snapshot_id": permission.snapshot_id,
        "permission_snapshot_fingerprint": permission.snapshot_fingerprint,
        "workflow_id": str(row["id"]),
        "workflow_ordinal": int(row["workflow_ordinal"]),
        "current_workflow_revision": int(row["workflow_revision"]),
        "workflow_status": PlanWorkflowStatus(str(row["status"])),
        "entered_by": PlanWorkflowEnteredBy(str(row["entered_by"])),
        "resume_permission_mode": PermissionMode(
            str(row["resume_permission_mode"])
        ),
        "permission_contract_id": str(row["permission_contract_id"]),
        "permission_contract_fingerprint": str(
            row["permission_contract_fingerprint"]
        ),
    }
    for name, value in values.items():
        object.__setattr__(provisional, name, value)
    object.__setattr__(provisional, "fact_fingerprint", "")
    return FrozenPlanWorkflowCompileFact(
        **values,
        fact_fingerprint=plan_workflow_compile_fact_fingerprint(provisional),
    )


def _plan_handoff_compile_facts(
    connection,
    *,
    cut: PreparedProviderInputCut,
    binding: Mapping[str, object],
    items: tuple[FrozenProviderInputItem, ...],
) -> tuple[
    FrozenPlanHandoffCompileFact | None,
    ApprovedPlanMaterializationFact | None,
]:
    row = connection.execute(
        """
        SELECT e.id AS carrier_entry_id, e.entry_sequence,
               e.source_plan_handoff_kind, e.source_plan_interaction_id,
               t.permission_plan_revision_at_admission,
               w.*, i.assistant_entry_id, i.tool_call_id,
               i.request_contract_id, i.request_contract_version,
               i.request_contract_fingerprint, i.request_semantic_digest,
               b.tool_arguments
        FROM pulsara_v3.transcript_entries AS e
        JOIN pulsara_v3.turns AS t
          ON t.session_id = e.session_id AND t.id = e.turn_id
        JOIN pulsara_v3.plan_workflows AS w
          ON w.session_id = e.session_id AND w.id = e.source_plan_workflow_id
        LEFT JOIN pulsara_v3.plan_interactions AS i
          ON i.session_id = e.session_id AND i.id = e.source_plan_interaction_id
        LEFT JOIN pulsara_v3.assistant_message_blocks AS b
          ON b.session_id = i.session_id
         AND b.assistant_entry_id = i.assistant_entry_id
         AND b.tool_call_id = i.tool_call_id
        WHERE e.session_id = %s AND e.turn_id = %s
          AND e.source_plan_handoff_kind IS NOT NULL
          AND e.entry_sequence <= %s
        ORDER BY e.entry_sequence DESC LIMIT 1
        """,
        (cut.session_id, cut.turn_id, cut.provider_input_through_sequence),
    ).fetchone()
    if row is None:
        return None, None
    kind = PlanHandoffKind(str(row["source_plan_handoff_kind"]))
    interaction_id = (
        None
        if row["source_plan_interaction_id"] is None
        else str(row["source_plan_interaction_id"])
    )
    transition_revision = (
        int(row["permission_plan_revision_at_admission"])
        if row["permission_plan_revision_at_admission"] is not None
        else int(row["workflow_revision"])
    )
    transition_digest = context_fingerprint(
        "pulsara:plan-transition:v1",
        {
            "workflow_id": str(row["id"]),
            "workflow_revision": transition_revision,
            "interaction_id": interaction_id,
            "handoff_kind": kind.value,
            "workflow_status": str(row["status"]),
        },
    )
    values = {
        "session_id": cut.session_id,
        "workspace_id": str(binding["workspace_id"]),
        "target_turn_id": cut.turn_id,
        "carrier_entry_id": str(row["carrier_entry_id"]),
        "carrier_entry_sequence": int(row["entry_sequence"]),
        "workflow_id": str(row["id"]),
        "workflow_ordinal": int(row["workflow_ordinal"]),
        "workflow_revision_at_transition": transition_revision,
        "interaction_id": interaction_id,
        "handoff_kind": kind,
        "workflow_status": PlanWorkflowStatus(str(row["status"])),
        "resume_permission_mode": PermissionMode(
            str(row["resume_permission_mode"])
        ),
        "transition_semantic_digest": transition_digest,
    }
    provisional = FrozenPlanHandoffCompileFact.__new__(
        FrozenPlanHandoffCompileFact
    )
    for name, value in values.items():
        object.__setattr__(provisional, name, value)
    object.__setattr__(provisional, "fact_fingerprint", "")
    handoff = FrozenPlanHandoffCompileFact(
        **values,
        fact_fingerprint=plan_handoff_compile_fact_fingerprint(provisional),
    )
    if kind is not PlanHandoffKind.APPROVED_PLAN:
        return handoff, None
    if (
        interaction_id is None
        or row["tool_arguments"] is None
        or row["assistant_entry_id"] is None
        or row["tool_call_id"] is None
    ):
        raise ConversationKernelConflict("approved Plan content is absent")
    frozen_arguments = freeze_json(dict(row["tool_arguments"]))
    if not isinstance(frozen_arguments, FrozenJsonObjectFact):
        raise ConversationKernelConflict("approved Plan arguments are invalid")
    extracted = extract_plan_draft(
        interaction_id=interaction_id,
        assistant_entry_id=str(row["assistant_entry_id"]),
        tool_call_id=str(row["tool_call_id"]),
        binding=PlanInteractionBinding(
            str(row["request_contract_id"]),
            str(row["request_contract_version"]),
            str(row["request_contract_fingerprint"]),
        ),
        request_semantic_digest=str(row["request_semantic_digest"]),
        arguments=frozen_arguments,
    )
    pinned_item = next(
        (
            item
            for item in items
            if item.source_entry_id == str(row["assistant_entry_id"])
            and any(
                call.tool_call_id == str(row["tool_call_id"])
                for call in item.tool_calls
            )
        ),
        None,
    )
    disposition = (
        PlanApprovedMaterializationDisposition.PIN_EXISTING_CANONICAL_BLOCK
        if pinned_item is not None
        else PlanApprovedMaterializationDisposition.MATERIALIZE_REFERENCED_BLOCK
    )
    approved_values = {
        "session_id": cut.session_id,
        "workspace_id": str(binding["workspace_id"]),
        "target_turn_id": cut.turn_id,
        "workflow_id": str(row["id"]),
        "interaction_id": interaction_id,
        "assistant_entry_id": str(row["assistant_entry_id"]),
        "tool_call_id": str(row["tool_call_id"]),
        "request_contract_id": str(row["request_contract_id"]),
        "request_contract_version": str(row["request_contract_version"]),
        "request_contract_fingerprint": str(row["request_contract_fingerprint"]),
        "request_semantic_digest": str(row["request_semantic_digest"]),
        "content_identity": extracted.identity,
        "exact_plan_utf8": extracted.exact_plan_utf8,
        "disposition": disposition,
        "pinned_canonical_item_fingerprint": (
            None
            if pinned_item is None
            else provider_input_item_fingerprint(pinned_item)
        ),
    }
    provisional_approved = ApprovedPlanMaterializationFact.__new__(
        ApprovedPlanMaterializationFact
    )
    for name, value in approved_values.items():
        object.__setattr__(provisional_approved, name, value)
    object.__setattr__(provisional_approved, "fact_fingerprint", "")
    approved = ApprovedPlanMaterializationFact(
        **approved_values,
        fact_fingerprint=approved_plan_materialization_fingerprint(
            provisional_approved
        ),
    )
    return handoff, approved

class CanonicalProviderInputReader(CanonicalProviderInputReader):
    """Complete the bounded physical hydration methods after pure fact helpers."""

    def _load_round7_scope_facts(
        self,
        connection,
        *,
        cut: PreparedProviderInputCut,
        workspace_id: str,
        scope_kind: ModelInputScopeKind,
        scope_task_id: str | None,
        current_initial_entry_sequence: int,
    ) -> tuple[
        FrozenPreviousTurnOutcomeCompileFact | None,
        FrozenToolObservationFreshnessCompileFact,
    ]:
        predecessor = connection.execute(
            """
            SELECT e.turn_id, e.entry_sequence, t.workspace_id, t.status,
                   t.terminal_reason, t.terminal_at,
                   t.conversation_scope_kind, t.scope_subagent_task_id
            FROM pulsara_v3.transcript_entries AS e
            JOIN pulsara_v3.turns AS t
              ON t.session_id = e.session_id
             AND t.id = e.turn_id
             AND t.initial_entry_id = e.id
            WHERE e.session_id = %s
              AND e.conversation_scope_kind = %s
              AND e.scope_subagent_task_id IS NOT DISTINCT FROM %s
              AND e.entry_sequence < %s
            ORDER BY e.entry_sequence DESC
            LIMIT 1
            """,
            (
                cut.session_id,
                scope_kind.value,
                scope_task_id,
                current_initial_entry_sequence,
            ),
        ).fetchone()
        predecessor_turn_id = (
            None if predecessor is None else str(predecessor["turn_id"])
        )
        freshness_values = {
            "session_id": cut.session_id,
            "workspace_id": workspace_id,
            "current_turn_id": cut.turn_id,
            "current_scope_kind": scope_kind,
            "scope_subagent_task_id": scope_task_id,
            "current_turn_ref": provider_visible_turn_ref(
                session_id=cut.session_id, turn_id=cut.turn_id
            ),
            "current_initial_entry_sequence": current_initial_entry_sequence,
            "immediate_predecessor_turn_id": predecessor_turn_id,
            "immediate_predecessor_turn_ref": (
                None
                if predecessor_turn_id is None
                else provider_visible_turn_ref(
                    session_id=cut.session_id, turn_id=predecessor_turn_id
                )
            ),
            "classification_contract": "pulsara.tool-observation-freshness.v1",
        }
        provisional_freshness = FrozenToolObservationFreshnessCompileFact.__new__(
            FrozenToolObservationFreshnessCompileFact
        )
        for name, value in freshness_values.items():
            object.__setattr__(provisional_freshness, name, value)
        object.__setattr__(provisional_freshness, "fact_fingerprint", "")
        freshness = FrozenToolObservationFreshnessCompileFact(
            **freshness_values,
            fact_fingerprint=tool_observation_freshness_fingerprint(
                provisional_freshness
            ),
        )
        if predecessor is None:
            return None, freshness
        if (
            str(predecessor["workspace_id"]) != workspace_id
            or str(predecessor["conversation_scope_kind"]) != scope_kind.value
            or predecessor["scope_subagent_task_id"] != scope_task_id
        ):
            raise ConversationKernelConflict("previous-turn scope identity drifted")
        status = str(predecessor["status"])
        raw_reason = str(predecessor["terminal_reason"] or "")
        if status == "COMPLETED" or raw_reason.startswith("PLAN_FORCE_EXIT:"):
            return None, freshness
        if status != "INTERRUPTED" or predecessor["terminal_at"] is None:
            raise ConversationKernelConflict(
                "previous turn is neither completed nor terminal interrupted"
            )
        outcome_kind = _previous_turn_outcome_kind(raw_reason)
        assistant_count_row = connection.execute(
            """
            SELECT count(*) AS accepted_assistant_count
            FROM pulsara_v3.transcript_entries
            WHERE session_id = %s AND turn_id = %s
              AND entry_sequence <= %s
              AND entry_kind IN ('ASSISTANT_MESSAGE', 'ASSISTANT_TOOL_REQUEST')
            """,
            (
                cut.session_id,
                predecessor_turn_id,
                cut.provider_input_through_sequence,
            ),
        ).fetchone()
        unresolved = connection.execute(
            """
            SELECT
              count(*) FILTER (WHERE a.id IS NULL) AS not_dispatched_count,
              count(*) FILTER (WHERE a.id IS NOT NULL) AS unknown_count
            FROM pulsara_v3.assistant_message_blocks AS b
            JOIN pulsara_v3.transcript_entries AS ae
              ON ae.session_id = b.session_id
             AND ae.id = b.assistant_entry_id
            LEFT JOIN pulsara_v3.tool_execution_attempts AS a
              ON a.session_id = b.session_id
             AND a.assistant_entry_id = b.assistant_entry_id
             AND a.tool_call_id = b.tool_call_id
            LEFT JOIN pulsara_v3.tool_results AS r
              ON r.session_id = b.session_id
             AND r.tool_call_entry_id = b.assistant_entry_id
             AND r.tool_call_id = b.tool_call_id
            LEFT JOIN pulsara_v3.transcript_entries AS re
              ON re.session_id = r.session_id AND re.id = r.result_entry_id
            LEFT JOIN pulsara_v3.plan_interactions AS pi
              ON pi.session_id = b.session_id
             AND pi.assistant_entry_id = b.assistant_entry_id
             AND pi.tool_call_id = b.tool_call_id
            WHERE b.session_id = %s AND ae.turn_id = %s
              AND ae.entry_sequence <= %s
              AND b.block_kind = 'TOOL_CALL'
              AND pi.id IS NULL
              AND (r.id IS NULL OR re.entry_sequence > %s)
            """,
            (
                cut.session_id,
                predecessor_turn_id,
                cut.provider_input_through_sequence,
                cut.provider_input_through_sequence,
            ),
        ).fetchone()
        sample_rows = connection.execute(
            """
            SELECT b.tool_name
            FROM pulsara_v3.assistant_message_blocks AS b
            JOIN pulsara_v3.transcript_entries AS ae
              ON ae.session_id = b.session_id
             AND ae.id = b.assistant_entry_id
            LEFT JOIN pulsara_v3.tool_results AS r
              ON r.session_id = b.session_id
             AND r.tool_call_entry_id = b.assistant_entry_id
             AND r.tool_call_id = b.tool_call_id
            LEFT JOIN pulsara_v3.transcript_entries AS re
              ON re.session_id = r.session_id AND re.id = r.result_entry_id
            LEFT JOIN pulsara_v3.plan_interactions AS pi
              ON pi.session_id = b.session_id
             AND pi.assistant_entry_id = b.assistant_entry_id
             AND pi.tool_call_id = b.tool_call_id
            WHERE b.session_id = %s AND ae.turn_id = %s
              AND ae.entry_sequence <= %s
              AND b.block_kind = 'TOOL_CALL'
              AND pi.id IS NULL
              AND (r.id IS NULL OR re.entry_sequence > %s)
            ORDER BY ae.entry_sequence, b.block_ordinal
            LIMIT 3
            """,
            (
                cut.session_id,
                predecessor_turn_id,
                cut.provider_input_through_sequence,
                cut.provider_input_through_sequence,
            ),
        ).fetchall()
        assistant_count = int(assistant_count_row["accepted_assistant_count"])
        previous_values = {
            "session_id": cut.session_id,
            "workspace_id": workspace_id,
            "current_turn_id": cut.turn_id,
            "current_scope_kind": scope_kind,
            "scope_subagent_task_id": scope_task_id,
            "predecessor_turn_id": predecessor_turn_id,
            "predecessor_initial_entry_sequence": int(
                predecessor["entry_sequence"]
            ),
            "predecessor_terminal_at_utc": canonical_utc_timestamp(
                predecessor["terminal_at"]
            ),
            "outcome_kind": outcome_kind,
            "accepted_assistant_disposition": (
                AcceptedAssistantDisposition.ACCEPTED_PREFIX_PRESENT
                if assistant_count
                else AcceptedAssistantDisposition.NONE_ACCEPTED
            ),
            "accepted_assistant_entry_count": assistant_count,
            "definitely_not_dispatched_tool_count": int(
                unresolved["not_dispatched_count"] or 0
            ),
            "outcome_unknown_tool_count": int(unresolved["unknown_count"] or 0),
            "bounded_tool_name_samples": tuple(
                _bounded_tool_name_sample(str(row["tool_name"]))
                for row in sample_rows
            ),
            "user_input_preserved": True,
            "canonical_entries_preserved": True,
        }
        provisional_previous = FrozenPreviousTurnOutcomeCompileFact.__new__(
            FrozenPreviousTurnOutcomeCompileFact
        )
        for name, value in previous_values.items():
            object.__setattr__(provisional_previous, name, value)
        object.__setattr__(provisional_previous, "fact_fingerprint", "")
        return (
            FrozenPreviousTurnOutcomeCompileFact(
                **previous_values,
                fact_fingerprint=previous_turn_outcome_fingerprint(
                    provisional_previous
                ),
            ),
            freshness,
        )

    def _load_tool_state(
        self,
        connection,
        session_id: str,
        entry_ids: Sequence[str],
        *,
        provider_input_through_sequence: int,
    ):
        state: dict[tuple[str, str], dict[str, object]] = {}
        if not entry_ids:
            return state
        rows = tuple(
            connection.execute(
                """
                SELECT b.assistant_entry_id, b.tool_call_id, a.id AS attempt_id,
                       i.kind AS plan_interaction_kind,
                       i.status AS plan_interaction_status,
                       r.session_id AS result_session_id, r.id AS result_id,
                       r.result_state, r.result_entry_id,
                       r.output_artifact_disposition, r.output_artifact_id,
                       r.output_source_coverage, r.output_display_kind,
                       r.output_source_coverage_reason,
                       r.output_artifact_unavailability_reason,
                       r.result_origin_kind, r.observed_at,
                       r.observation_duration_microseconds,
                       r.observation_origin_kind,
                       r.tool_reported_duration_microseconds,
                       r.model_visible_memory_fact_ids,
                       e.entry_sequence, e.blob_id,
                       e.content_digest, e.content_size,
                       e.content_media_type, e.content_codec,
                       e.turn_id AS result_turn_id
                FROM pulsara_v3.assistant_message_blocks AS b
                LEFT JOIN pulsara_v3.tool_execution_attempts AS a
                  ON a.session_id = b.session_id
                 AND a.assistant_entry_id = b.assistant_entry_id
                 AND a.tool_call_id = b.tool_call_id
                LEFT JOIN pulsara_v3.tool_results AS r
                  ON r.session_id = b.session_id
                 AND r.tool_call_entry_id = b.assistant_entry_id
                 AND r.tool_call_id = b.tool_call_id
                LEFT JOIN pulsara_v3.plan_interactions AS i
                  ON i.session_id = b.session_id
                 AND i.assistant_entry_id = b.assistant_entry_id
                 AND i.tool_call_id = b.tool_call_id
                LEFT JOIN pulsara_v3.transcript_entries AS e
                  ON e.session_id = r.session_id AND e.id = r.result_entry_id
                WHERE b.session_id = %s
                  AND b.assistant_entry_id = ANY(%s)
                  AND b.block_kind = 'TOOL_CALL'
                ORDER BY b.assistant_entry_id, b.block_ordinal
                LIMIT %s
                """,
                (session_id, list(entry_ids), self._maximum_items + 1),
            ).fetchall()
        )
        if len(rows) > self._maximum_items:
            raise ConversationKernelConflict("provider input tool-call bound exceeded")
        for row in rows:
            payload: dict[str, object] = {
                "attempt_id": row["attempt_id"],
                "plan_interaction_kind": row["plan_interaction_kind"],
                "plan_interaction_status": row["plan_interaction_status"],
            }
            visible_result = visible_tool_result_at_cut(
                row,
                provider_input_through_sequence=provider_input_through_sequence,
            )
            if visible_result is not None:
                payload["result"] = visible_result
            state[(str(row["assistant_entry_id"]), str(row["tool_call_id"]))] = payload
        if len(state) != len(rows):
            raise ConversationKernelConflict(
                "provider input tool-call identity conflicts"
            )
        return state

    def _load_entry_payloads(
        self, connection, session_id: str, entry_ids: Sequence[str]
    ) -> dict[str, object]:
        if not entry_ids:
            return {}
        rows = tuple(
            connection.execute(
                """
                SELECT id, inline_content
                FROM pulsara_v3.transcript_entries
                WHERE session_id = %s AND id = ANY(%s)
                ORDER BY id
                LIMIT %s
                """,
                (session_id, list(entry_ids), len(entry_ids) + 1),
            ).fetchall()
        )
        if len(rows) != len(entry_ids):
            raise ConversationKernelConflict("provider input entry payload set drifted")
        return {str(row["id"]): row["inline_content"] for row in rows}

    def _load_block_payloads(
        self, connection, session_id: str, block_ids: Sequence[str]
    ) -> dict[str, Mapping[str, object]]:
        if not block_ids:
            return {}
        rows = tuple(
            connection.execute(
                """
                SELECT id, inline_content, tool_arguments
                FROM pulsara_v3.assistant_message_blocks
                WHERE session_id = %s AND id = ANY(%s)
                ORDER BY id
                LIMIT %s
                """,
                (session_id, list(block_ids), len(block_ids) + 1),
            ).fetchall()
        )
        if len(rows) != len(block_ids):
            raise ConversationKernelConflict("provider input block payload set drifted")
        return {str(row["id"]): row for row in rows}

    @staticmethod
    def _load_snapshot_payload(connection, session_id: str, snapshot_id: str) -> object:
        row = connection.execute(
            """
            SELECT inline_content
            FROM pulsara_v3.context_snapshots
            WHERE session_id = %s AND id = %s
            """,
            (session_id, snapshot_id),
        ).fetchone()
        if row is None:
            raise ConversationKernelConflict("context snapshot payload is absent")
        return row["inline_content"]

    @staticmethod
    def _join_block_payloads(
        metadata: Sequence[Mapping[str, object]],
        payloads: Mapping[str, Mapping[str, object]],
    ) -> dict[str, tuple[Mapping[str, object], ...]]:
        result: dict[str, list[Mapping[str, object]]] = {}
        for row in metadata:
            block_id = str(row["id"])
            payload = payloads.get(block_id)
            if payload is None:
                raise ConversationKernelConflict(
                    "provider input block payload is absent"
                )
            joined = dict(row)
            joined["inline_content"] = payload["inline_content"]
            joined["tool_arguments"] = payload["tool_arguments"]
            result.setdefault(str(row["assistant_entry_id"]), []).append(joined)
        return {key: tuple(value) for key, value in result.items()}

    def _preflight_physical_bytes(
        self,
        *,
        snapshot: Mapping[str, object] | None,
        entries: Sequence[Mapping[str, object]],
        blocks: Sequence[Mapping[str, object]],
    ) -> None:
        total = 0 if snapshot is None else int(snapshot["content_size"])
        for row in entries:
            if row["entry_kind"] in (
                "USER_MESSAGE",
                "USER_STEER",
                "TERMINAL_OBSERVATION",
                "TOOL_RESULT",
            ):
                total += int(row["content_size"])
        for row in blocks:
            if row["block_kind"] in ("TEXT", "DATA"):
                total += max(int(row["content_size"]), int(row["inline_content_size"]))
            else:
                total += int(row["tool_arguments_size"])
            if total > self._maximum_canonical_bytes:
                raise ConversationKernelConflict(
                    "provider input physical byte bound exceeded"
                )
        if total > self._maximum_canonical_bytes:
            raise ConversationKernelConflict(
                "provider input physical byte bound exceeded"
            )

    def _read_content(
        self,
        row: Mapping[str, object],
        *,
        deadline_monotonic: float,
        remaining_bytes: _RemainingReadBudget | None = None,
    ) -> bytes:
        expected_size = int(row["content_size"])
        if remaining_bytes is not None:
            remaining_bytes.consume(expected_size)
        expected_digest = str(row["content_digest"])
        if row["inline_content"] is not None:
            content = bytes(row["inline_content"])
        else:
            if self._blob_reader is None:
                raise CanonicalProviderContinuityError(
                    CanonicalProviderContinuityFailureKind.BLOB_READER_UNAVAILABLE,
                    content_identity=str(row.get("blob_id")),
                )
            try:
                content = self._blob_reader.read_exact(
                    blob_id=str(row["blob_id"]),
                    expected_digest=expected_digest,
                    expected_size=expected_size,
                    deadline_monotonic=deadline_monotonic,
                )
            except CanonicalProviderContinuityError:
                raise
            except Exception as exc:
                raise CanonicalProviderContinuityError(
                    CanonicalProviderContinuityFailureKind.BLOB_UNAVAILABLE_OR_CORRUPT,
                    content_identity=str(row.get("blob_id")),
                ) from exc
        if len(content) != expected_size:
            raise CanonicalProviderContinuityError(
                CanonicalProviderContinuityFailureKind.CONTENT_SIZE_MISMATCH,
                content_identity=expected_digest,
            )
        if "sha256:" + sha256(content).hexdigest() != expected_digest:
            raise CanonicalProviderContinuityError(
                CanonicalProviderContinuityFailureKind.CONTENT_DIGEST_MISMATCH,
                content_identity=expected_digest,
            )
        return content

    def _block_text(
        self,
        row: Mapping[str, object],
        *,
        deadline_monotonic: float,
        remaining_bytes: _RemainingReadBudget | None = None,
    ) -> str:
        return _decode_provider_text(
            self._read_content(
                row,
                deadline_monotonic=deadline_monotonic,
                remaining_bytes=remaining_bytes,
            ),
            str(row["content_codec"]),
        )


@dataclass(slots=True)
class _RemainingReadBudget:
    remaining_bytes: int

    def consume(self, physical_bytes: int) -> None:
        if physical_bytes < 0 or physical_bytes > self.remaining_bytes:
            raise ConversationKernelConflict(
                "provider input physical byte bound exceeded"
            )
        self.remaining_bytes -= physical_bytes


def _with_inline_payload(
    row: Mapping[str, object], inline_content: object
) -> Mapping[str, object]:
    result = dict(row)
    result["inline_content"] = inline_content
    return result


def _canonical_input_origin(
    row: Mapping[str, object], *, scope_kind: str
) -> CanonicalInputOriginKind:
    if scope_kind == ModelInputScopeKind.SUBAGENT_TASK.value:
        return CanonicalInputOriginKind.SUBAGENT_OBJECTIVE
    if row["source_subagent_result_id"] is not None:
        return CanonicalInputOriginKind.SUBAGENT_RESULT
    if row["source_job_id"] is not None:
        return CanonicalInputOriginKind.JOB_RESULT
    if row["entry_kind"] == "USER_STEER":
        return CanonicalInputOriginKind.HUMAN_STEER
    return CanonicalInputOriginKind.HUMAN_MESSAGE


def _next_assistant_cuts(
    entries: Sequence[Mapping[str, object]],
) -> dict[str, int]:
    assistants = [
        row
        for row in entries
        if row["entry_kind"] in ("ASSISTANT_MESSAGE", "ASSISTANT_TOOL_REQUEST")
    ]
    result: dict[str, int] = {}
    for index, row in enumerate(assistants[:-1]):
        next_row = assistants[index + 1]
        result[str(row["id"])] = int(next_row["provider_input_through_sequence"])
    return result


def _decode_provider_text(content: bytes, codec: str) -> str:
    if codec != "utf-8":
        return _canonical_json_text(
            {
                "kind": "binary_content",
                "codec": codec,
                "size": len(content),
            }
        )
    try:
        return content.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise CanonicalProviderContinuityError(
            CanonicalProviderContinuityFailureKind.INVALID_UTF8,
            content_identity="sha256:" + sha256(content).hexdigest(),
        ) from exc


def _project_plan_continuation_storage(
    row: Mapping[str, object], storage_text: str
) -> str:
    """Validate the canonical carrier, then emit its closed provider DTO."""

    transition = str(row.get("source_plan_handoff_kind") or "")
    if transition not in {"ENTERED_PLAN", "REVISION_REQUESTED", "APPROVED_PLAN"}:
        raise ConversationKernelConflict("Plan continuation transition is invalid")
    try:
        value = json.loads(storage_text)
    except json.JSONDecodeError as exc:
        raise ConversationKernelConflict(
            "Plan continuation storage carrier is invalid"
        ) from exc
    if not isinstance(value, dict) or str(value.get("transition") or "") != transition:
        raise ConversationKernelConflict(
            "Plan continuation storage transition conflicts"
        )
    typed_workflow = row.get("source_plan_workflow_id")
    typed_interaction = row.get("source_plan_interaction_id")
    if value.get("workflow_id") is not None and str(value["workflow_id"]) != str(
        typed_workflow
    ):
        raise ConversationKernelConflict("Plan continuation workflow conflicts")
    if value.get("interaction_id") is not None and str(
        value["interaction_id"]
    ) != str(typed_interaction):
        raise ConversationKernelConflict("Plan continuation interaction conflicts")

    projected: dict[str, object] = {
        "status": "APPROVED" if transition == "APPROVED_PLAN" else "ACTIVE",
        "transition": transition,
    }
    if transition == "REVISION_REQUESTED":
        feedback = value.get("feedback")
        if feedback is not None and not isinstance(feedback, str):
            raise ConversationKernelConflict("Plan revision feedback is invalid")
        projected["feedback"] = (
            {"presence": "ABSENT"}
            if feedback is None
            else {"presence": "PRESENT", "text": feedback}
        )
    if transition == "APPROVED_PLAN" and not isinstance(
        value.get("approved_plan"), dict
    ):
        raise ConversationKernelConflict("approved Plan identity carrier is invalid")
    return _canonical_json_text({"pulsara_plan_continuation": projected})


def _canonical_json_text(payload: Mapping[str, object]) -> str:
    return json.dumps(
        dict(payload), ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )


def _freeze_tool_arguments(
    value: object,
    *,
    expected_physical_bytes: int,
    remaining_bytes: _RemainingReadBudget,
) -> FrozenJsonObjectFact:
    remaining_bytes.consume(expected_physical_bytes)
    frozen = freeze_json(value)
    if not isinstance(frozen, FrozenJsonObjectFact):
        raise ConversationKernelConflict("canonical tool arguments are not an object")
    return frozen


def visible_tool_result_at_cut(
    row: Mapping[str, object],
    *,
    provider_input_through_sequence: int,
) -> Mapping[str, object] | None:
    """Return one exact result only when its canonical entry is in the cut."""

    if row.get("result_entry_id") is None:
        return None
    if row.get("entry_sequence") is None or row.get("result_turn_id") is None:
        raise ConversationKernelConflict("tool result canonical entry is absent")
    sequence = int(row["entry_sequence"])
    if sequence > provider_input_through_sequence:
        return None
    if sequence < 1:
        raise ConversationKernelConflict("tool result entry sequence is invalid")
    return row


def _previous_turn_outcome_kind(raw_reason: str) -> PreviousTurnOutcomeKind:
    return {
        "USER_STOPPED": PreviousTurnOutcomeKind.USER_STOPPED,
        "FOREGROUND_EXECUTION_INTERRUPTED": PreviousTurnOutcomeKind.EXECUTION_FAILED,
        "SESSION_CLOSED": PreviousTurnOutcomeKind.HOST_SESSION_CLOSED,
        "HOST_TAKEOVER": PreviousTurnOutcomeKind.HOST_REPLACED,
        "PROVIDER_INPUT_PLAN_CONFLICT": (
            PreviousTurnOutcomeKind.PROVIDER_INPUT_CONFLICT
        ),
        "PROVIDER_INPUT_RESOURCE_EXHAUSTED": (
            PreviousTurnOutcomeKind.RESOURCE_BOUNDARY
        ),
        "PLAN_CONTINUATION_NOT_BOUND": (
            PreviousTurnOutcomeKind.PLAN_CONTINUATION_FAILED
        ),
    }.get(raw_reason, PreviousTurnOutcomeKind.UNKNOWN_INTERRUPTION)


def _bounded_tool_name_sample(value: str) -> str:
    encoded = value.encode("utf-8")
    if len(encoded) <= 128:
        return value
    candidate = encoded[:128]
    while candidate:
        try:
            return candidate.decode("utf-8")
        except UnicodeDecodeError:
            candidate = candidate[:-1]
    raise ConversationKernelConflict("tool name cannot form a bounded UTF-8 sample")


def _tool_result_metadata(
    row: Mapping[str, object],
) -> ProviderToolResultContextMetadata:
    origin = ToolObservationOrigin(str(row["observation_origin_kind"]))
    result_origin = str(row["result_origin_kind"])
    duration = (
        None
        if row["observation_duration_microseconds"] is None
        else int(row["observation_duration_microseconds"])
    )
    disposition = (
        ToolObservationDurationDisposition.NO_PHYSICAL_ATTEMPT
        if result_origin in {"POLICY_NO_ATTEMPT", "PLAN_CONTROL"}
        else ToolObservationDurationDisposition.MEASURED
        if duration is not None
        else ToolObservationDurationDisposition.MEASUREMENT_UNAVAILABLE
    )
    timing_values = {
        "source_turn_ref": provider_visible_turn_ref(
            session_id=str(row["result_session_id"]),
            turn_id=str(row["result_turn_id"]),
        ),
        "observed_at_utc": canonical_utc_timestamp(row["observed_at"]),
        "observation_duration_microseconds": duration,
        "duration_disposition": disposition,
        "tool_reported_duration_microseconds": (
            None
            if row["tool_reported_duration_microseconds"] is None
            else int(row["tool_reported_duration_microseconds"])
        ),
        "observation_origin": origin,
    }
    provisional_timing = FrozenToolObservationTimingFact.__new__(
        FrozenToolObservationTimingFact
    )
    for name, value in timing_values.items():
        object.__setattr__(provisional_timing, name, value)
    object.__setattr__(provisional_timing, "fact_fingerprint", "")
    timing = FrozenToolObservationTimingFact(
        **timing_values,
        fact_fingerprint=tool_observation_timing_fingerprint(provisional_timing),
    )
    return ProviderToolResultContextMetadata(
        result_id=str(row["result_id"]),
        result_state=str(row["result_state"]),
        display_kind=ToolResultDisplayKind(str(row["output_display_kind"])),
        artifact_disposition=ToolOutputArtifactDisposition(
            str(row["output_artifact_disposition"])
        ),
        artifact_id=(
            None
            if row["output_artifact_id"] is None
            else str(row["output_artifact_id"])
        ),
        source_coverage=ToolOutputSourceCoverage(str(row["output_source_coverage"])),
        source_coverage_reason=(
            None
            if row["output_source_coverage_reason"] is None
            else ToolOutputSourceCoverageReason(
                str(row["output_source_coverage_reason"])
            )
        ),
        artifact_unavailability_reason=(
            None
            if row["output_artifact_unavailability_reason"] is None
            else ToolOutputArtifactUnavailabilityReason(
                str(row["output_artifact_unavailability_reason"])
            )
        ),
        model_visible_memory_fact_ids=tuple(
            str(value) for value in row["model_visible_memory_fact_ids"]
        ),
        timing=timing,
    )


def _validate_terminal_observation_content(content: bytes) -> None:
    try:
        payload = json.loads(content)
        if not isinstance(payload, dict) or set(payload) != {
            "schema_version",
            "observation_id",
            "monitor_id",
            "process_id",
            "observation_ordinal",
            "observation_kind",
            "process_status",
            "exit_code",
            "output_disposition",
            "gap_before_output",
            "delivery_coverage",
            "available_source_utf8_bytes",
            "included_source_utf8_bytes",
            "omitted_by_delivery_bound_utf8_bytes",
            "output",
            "host_scoped",
        }:
            raise ValueError("terminal observation schema is not closed")
        integer_fields = (
            "observation_ordinal",
            "available_source_utf8_bytes",
            "included_source_utf8_bytes",
            "omitted_by_delivery_bound_utf8_bytes",
        )
        if any(
            not isinstance(payload[field], int) or isinstance(payload[field], bool)
            for field in integer_fields
        ):
            raise ValueError("terminal observation integer field is invalid")
        if not isinstance(payload["gap_before_output"], bool) or not isinstance(
            payload["host_scoped"], bool
        ):
            raise ValueError("terminal observation boolean field is invalid")
        if payload["exit_code"] is not None and (
            not isinstance(payload["exit_code"], int)
            or isinstance(payload["exit_code"], bool)
        ):
            raise ValueError("terminal observation exit code is invalid")
        if any(
            not isinstance(payload[field], str)
            for field in (
                "schema_version",
                "observation_id",
                "monitor_id",
                "process_id",
                "observation_kind",
                "process_status",
                "output_disposition",
                "delivery_coverage",
                "output",
            )
        ):
            raise ValueError("terminal observation string field is invalid")
        fact = TerminalObservationContentV1(
            schema_version=str(payload["schema_version"]),
            observation_id=str(payload["observation_id"]),
            monitor_id=str(payload["monitor_id"]),
            process_id=str(payload["process_id"]),
            observation_ordinal=int(payload["observation_ordinal"]),
            observation_kind=TerminalObservationKind(str(payload["observation_kind"])),
            process_status=str(payload["process_status"]),
            exit_code=(
                None if payload["exit_code"] is None else int(payload["exit_code"])
            ),
            output_disposition=str(payload["output_disposition"]),
            gap_before_output=payload["gap_before_output"],
            delivery_coverage=TerminalDeliveryCoverage(
                str(payload["delivery_coverage"])
            ),
            available_source_utf8_bytes=int(payload["available_source_utf8_bytes"]),
            included_source_utf8_bytes=int(payload["included_source_utf8_bytes"]),
            omitted_by_delivery_bound_utf8_bytes=int(
                payload["omitted_by_delivery_bound_utf8_bytes"]
            ),
            output=str(payload["output"]),
            host_scoped=payload["host_scoped"],
        )
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise ConversationKernelConflict(
            "terminal observation canonical envelope is invalid"
        ) from exc
    if fact.canonical_bytes() != content:
        raise ConversationKernelConflict(
            "terminal observation canonical encoding is not unique"
        )


__all__ = [
    "CanonicalBlobReader",
    "CanonicalProviderInputReader",
    "CanonicalProviderContinuityError",
    "CanonicalProviderContinuityFailureKind",
    "LateToolOutcomeObservation",
    "ProviderInputItem",
    "ProviderInputItemKind",
    "ProviderToolCall",
    "ProviderToolResultClosure",
    "ProviderToolResultClosureKind",
]
