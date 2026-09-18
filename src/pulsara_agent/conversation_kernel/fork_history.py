"""Pure historical material extraction inside the caller's Fork transaction.

This module reads canonical history only. It neither creates a dispatch nor
captures permission, inventory, writer, or executable continuation state.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from time import monotonic
from types import MappingProxyType
from typing import Mapping, TYPE_CHECKING

from psycopg import Connection, IsolationLevel

from pulsara_agent.conversation_kernel.compaction.contracts import (
    CompactionSnapshotCarrier,
    CompactionActiveRequestLocation,
    CompactionContinuationMode,
    FrozenRetainedHistoricalRequest,
)
from pulsara_agent.conversation_kernel.compaction.prompt import (
    compaction_snapshot_image_parts,
)
from pulsara_agent.conversation_kernel.prompt_storage import (
    hydrate_canonical_prompt_owner,
    hydrate_canonical_snapshot_owner,
)
from pulsara_agent.conversation_kernel.prompt_content import PROMPT_BODY_MEDIA_TYPE
from pulsara_agent.conversation_kernel.repository_errors import (
    ConversationKernelConflict,
)
from pulsara_agent.llm.model_connections import (
    ModelCallBinding,
    model_call_binding_from_dict,
)
from pulsara_agent.llm.provider_replay import ProviderAssistantReplayFragment
from pulsara_agent.llm.input import LLMToolCall
from pulsara_agent.llm.request import provider_assistant_public_projection_fingerprint
from pulsara_agent.primitives.context import canonical_json_bytes, freeze_json
from pulsara_agent.model_input.contracts import (
    CanonicalInputOriginKind,
    FrozenProviderInputItemKind,
    MAXIMUM_CANONICAL_PROVIDER_INPUT_BYTES,
    MAXIMUM_CANONICAL_PROVIDER_INPUT_ITEMS,
)
from pulsara_agent.model_input.provider_replay import (
    decode_provider_replay_fragment,
    freeze_provider_replay_manifest,
    manifest_cut_metadata_bytes,
    quote_provider_dispatch_composite_bytes,
)
from pulsara_agent.ports.user_control_feedback import (
    USER_CONTROL_FEEDBACK_MEDIA_TYPE,
    project_user_control_feedback_for_provider,
)

if TYPE_CHECKING:
    from pulsara_agent.conversation_kernel.reader import CanonicalProviderInputReader


@dataclass(frozen=True, slots=True)
class FrozenForkAnchor:
    source_entry_id: str
    source_entry_sequence: int
    owner_kind: str
    settled_status: str
    nonempty_text_block_ordinal: int
    anchor_model_call_binding: ModelCallBinding
    base_kind: str
    source_through_sequence: int
    context_snapshot_id: str | None


@dataclass(frozen=True, slots=True)
class FrozenForkGroup:
    source_group_key: str
    settled_status: str
    accepted_at: datetime
    terminal_at: datetime | None
    copied_final_source_entry_id: str | None


@dataclass(frozen=True, slots=True)
class FrozenForkHistoricalMaterial:
    source_session_id: str
    workspace_id: str
    workspace_kind: str
    workspace_root: str
    workspace_label: str
    memory_domain_id: str
    anchor: FrozenForkAnchor
    snapshot: Mapping[str, object] | None
    snapshot_carrier: CompactionSnapshotCarrier | None
    retained_historical_requests: tuple[FrozenRetainedHistoricalRequest, ...]
    imported_groups: tuple[FrozenForkGroup, ...]
    entries: tuple[Mapping[str, object], ...]
    blocks: tuple[Mapping[str, object], ...]
    tool_results: tuple[Mapping[str, object], ...]
    required_tool_closures: tuple[Mapping[str, object], ...]
    replay_fragments: tuple[tuple[str, ProviderAssistantReplayFragment], ...]
    referenced_artifact_blobs: tuple[Mapping[str, object], ...]


def _deadline(deadline: float) -> None:
    if monotonic() >= deadline:
        raise TimeoutError("Fork canonical transaction deadline expired")


def read_fork_anchor(
    connection: Connection, session_id: str, entry_id: str
) -> FrozenForkAnchor | None:
    """The one eligibility predicate shared by creation and UI projection."""
    row = connection.execute(
        """SELECT e.*, t.status AS executed_status, t.final_entry_id AS executed_final,
                  t.model_call_binding AS executed_model,
                  r.base_kind AS executed_base, r.context_snapshot_id AS executed_snapshot,
                  r.source_through_sequence AS executed_floor,
                  g.status AS imported_status, g.final_entry_id AS imported_final,
                  genesis.anchor_entry_id, genesis.model_call_binding AS genesis_model,
                  genesis.base_kind AS genesis_base, genesis.context_snapshot_id AS genesis_snapshot,
                  genesis.source_through_sequence AS genesis_floor
           FROM pulsara_v3.transcript_entries AS e
           LEFT JOIN pulsara_v3.turns AS t ON e.entry_owner_kind = 'EXECUTED_TURN'
             AND t.session_id = e.session_id AND t.id = e.turn_id
           LEFT JOIN pulsara_v3.turn_context_binding_revisions AS r
             ON r.session_id = e.session_id AND r.turn_id = t.id AND r.id = e.context_binding_revision_id
           LEFT JOIN pulsara_v3.imported_history_groups AS g ON e.entry_owner_kind = 'IMPORTED_HISTORY'
             AND g.session_id = e.session_id AND g.id = e.imported_history_group_id
           LEFT JOIN pulsara_v3.session_context_genesis AS genesis ON genesis.session_id = e.session_id
           WHERE e.session_id = %s AND e.id = %s""",
        (session_id, entry_id),
    ).fetchone()
    if (
        row is None
        or row["entry_kind"] != "ASSISTANT_MESSAGE"
        or row["conversation_scope_kind"] != "ROOT"
    ):
        return None
    executed = row["entry_owner_kind"] == "EXECUTED_TURN"
    prefix = "executed" if executed else "genesis"
    status = row["executed_status"] if executed else row["imported_status"]
    final = row["executed_final"] if executed else row["imported_final"]
    if status not in {"COMPLETED", "INTERRUPTED"} or final != entry_id:
        return None
    if not executed and (
        row["entry_owner_kind"] != "IMPORTED_HISTORY"
        or row["anchor_entry_id"] != entry_id
    ):
        return None
    if row[prefix + "_base"] not in {"FULL_HISTORY", "SNAPSHOT"}:
        return None
    blocks = connection.execute(
        """SELECT b.block_ordinal, b.content_digest, b.content_size,
                  COALESCE(b.inline_content, blob.body) AS body
           FROM pulsara_v3.assistant_message_blocks AS b
           LEFT JOIN pulsara_v3.blobs AS blob ON blob.id = b.blob_id AND blob.workspace_id = b.workspace_id
           WHERE b.session_id = %s AND b.assistant_entry_id = %s
             AND b.block_kind = 'TEXT' AND b.content_codec = 'utf-8'
           ORDER BY b.block_ordinal""",
        (session_id, entry_id),
    ).fetchall()
    from hashlib import sha256

    for block in blocks:
        if block["body"] is None:
            continue
        body = bytes(block["body"])
        if (
            len(body) != int(block["content_size"])
            or "sha256:" + sha256(body).hexdigest() != block["content_digest"]
        ):
            continue
        try:
            nonempty = bool(body.decode("utf-8").strip())
        except UnicodeDecodeError:
            continue
        if nonempty:
            model = model_call_binding_from_dict(row[prefix + "_model"])
            if model is None:
                return None
            base = str(row[prefix + "_base"])
            return FrozenForkAnchor(
                source_entry_id=entry_id,
                source_entry_sequence=int(row["entry_sequence"]),
                owner_kind=str(row["entry_owner_kind"]),
                settled_status=str(status),
                nonempty_text_block_ordinal=int(block["block_ordinal"]),
                anchor_model_call_binding=model,
                base_kind=base,
                source_through_sequence=(
                    0 if base == "FULL_HISTORY" else int(row[prefix + "_floor"])
                ),
                context_snapshot_id=row[prefix + "_snapshot"],
            )
    return None


def read_fork_historical_material(
    connection: Connection,
    source_session_id: str,
    anchor_entry_id: str,
    deadline_monotonic: float,
    *,
    reader: CanonicalProviderInputReader,
) -> FrozenForkHistoricalMaterial:
    from pulsara_agent.conversation_kernel.reader import (
        _RemainingReadBudget,
        _next_assistant_cuts,
        historical_source_attribution,
        historical_tool_closure_kind,
        _project_plan_continuation_storage,
        _validate_terminal_observation_content,
    )
    from pulsara_agent.conversation_kernel.subagents.contracts import (
        validate_subagent_completion_storage_body,
    )

    if connection.isolation_level is not IsolationLevel.REPEATABLE_READ:
        raise ConversationKernelConflict(
            "Fork requires its single REPEATABLE READ transaction"
        )
    _deadline(deadline_monotonic)
    anchor = read_fork_anchor(connection, source_session_id, anchor_entry_id)
    if anchor is None:
        raise ConversationKernelConflict("FORK_ANCHOR_INELIGIBLE")
    session = connection.execute(
        "SELECT workspace_id, workspace_kind, workspace_root, workspace_label, memory_domain_id "
        "FROM pulsara_v3.sessions WHERE id = %s",
        (source_session_id,),
    ).fetchone()
    if session is None:
        raise ConversationKernelConflict("Fork source session is absent")
    budget = _RemainingReadBudget(MAXIMUM_CANONICAL_PROVIDER_INPUT_BYTES)
    snapshot = None
    carrier = None
    retained: tuple[FrozenRetainedHistoricalRequest, ...] = ()
    if anchor.base_kind == "SNAPSHOT":
        snapshot = connection.execute(
            "SELECT * FROM pulsara_v3.context_snapshots WHERE session_id = %s AND id = %s",
            (source_session_id, anchor.context_snapshot_id),
        ).fetchone()
        if (
            snapshot is None
            or int(snapshot["source_through_sequence"])
            != anchor.source_through_sequence
        ):
            raise ConversationKernelConflict(
                "Fork snapshot base is absent or inconsistent"
            )
        carrier = hydrate_canonical_snapshot_owner(
            connection,
            row=snapshot,
            context_snapshot_id=str(snapshot["id"]),
        )
        budget.consume(
            len(carrier.body)
            + sum(
                len(image.immutable_bytes)
                for image in compaction_snapshot_image_parts(carrier)
            )
        )
        retained = carrier.retained_historical_requests
        active = carrier.active_request
        if (
            carrier.continuation_mode is CompactionContinuationMode.RESUME_ACTIVE_TURN
            and active is not None
            and active.location is CompactionActiveRequestLocation.SNAPSHOT_EXACT
        ):
            initial = connection.execute(
                "SELECT e.* FROM pulsara_v3.transcript_entries AS e JOIN pulsara_v3.turns AS t "
                "ON t.session_id = e.session_id AND t.id = e.turn_id AND t.initial_entry_id = e.id "
                "WHERE e.session_id = %s AND e.id = %s "
                "AND e.entry_sequence = %s AND e.entry_owner_kind = 'EXECUTED_TURN'",
                (source_session_id, active.entry_id, active.entry_sequence),
            ).fetchone()
            if initial is None:
                raise ConversationKernelConflict(
                    "Fork snapshot active request has no canonical origin"
                )
            kinds = {
                "USER_MESSAGE": (
                    FrozenProviderInputItemKind.USER,
                    CanonicalInputOriginKind.HUMAN_MESSAGE,
                ),
                "USER_STEER": (
                    FrozenProviderInputItemKind.USER,
                    CanonicalInputOriginKind.HUMAN_STEER,
                ),
                "PLAN_CONTINUATION": (
                    FrozenProviderInputItemKind.PLAN_CONTINUATION,
                    CanonicalInputOriginKind.PLAN_CONTINUATION,
                ),
                "INTER_AGENT_MESSAGE": (
                    FrozenProviderInputItemKind.INTER_AGENT_MESSAGE,
                    CanonicalInputOriginKind.INTER_AGENT_MESSAGE,
                ),
                "TERMINAL_OBSERVATION": (
                    FrozenProviderInputItemKind.TERMINAL_OBSERVATION,
                    None,
                ),
                "USER_CONTROL_FEEDBACK": (
                    FrozenProviderInputItemKind.USER,
                    CanonicalInputOriginKind.USER_CONTROL_FEEDBACK,
                ),
            }
            if (
                initial["entry_kind"] not in kinds
                or initial["conversation_scope_kind"] != "ROOT"
            ):
                raise ConversationKernelConflict(
                    "Fork retained request origin is invalid"
                )
            kind, origin = kinds[initial["entry_kind"]]
            if kind is not active.item_kind or origin is not active.input_origin:
                raise ConversationKernelConflict(
                    "Fork snapshot active request attribution drifted"
                )
            # A later exact request follows all retained predecessor requests.
            # Equal text can be a distinct request; do not deduplicate history.
            retained += (
                FrozenRetainedHistoricalRequest(
                    item_kind=kind,
                    input_origin=origin,
                    content=active.content,
                ),
            )

    entries = []
    blocks = []
    results = []
    replays = []
    artifacts = {}
    groups = {}
    block_bodies = {}
    blocks_by_entry = {}
    last = (anchor.source_through_sequence, "")
    while True:
        _deadline(deadline_monotonic)
        page = connection.execute(
            """SELECT e.*, t.status AS executed_status, t.accepted_at AS executed_accepted,
                      t.terminal_at AS executed_terminal, t.final_entry_id AS executed_final,
                      g.status AS imported_status, g.accepted_at AS imported_accepted,
                      g.terminal_at AS imported_terminal, g.final_entry_id AS imported_final
               FROM pulsara_v3.transcript_entries AS e
               LEFT JOIN pulsara_v3.turns AS t ON e.entry_owner_kind = 'EXECUTED_TURN'
                 AND t.session_id = e.session_id AND t.id = e.turn_id
               LEFT JOIN pulsara_v3.imported_history_groups AS g ON e.entry_owner_kind = 'IMPORTED_HISTORY'
                 AND g.session_id = e.session_id AND g.id = e.imported_history_group_id
               WHERE e.session_id = %s AND e.conversation_scope_kind = 'ROOT'
                 AND e.entry_sequence > %s AND (e.entry_sequence, e.id) > (%s, %s)
                 AND e.entry_sequence <= %s ORDER BY e.entry_sequence, e.id LIMIT 256""",
            (
                source_session_id,
                anchor.source_through_sequence,
                *last,
                anchor.source_entry_sequence,
            ),
        ).fetchall()
        if not page:
            break
        ids = [str(row["id"]) for row in page]
        page_blocks = connection.execute(
            "SELECT * FROM pulsara_v3.assistant_message_blocks WHERE session_id = %s "
            "AND assistant_entry_id = ANY(%s) ORDER BY assistant_entry_id, block_ordinal",
            (source_session_id, ids),
        ).fetchall()
        for block in page_blocks:
            if block["block_kind"] in {"TEXT", "DATA"}:
                block_bodies[str(block["id"])] = reader._read_content(
                    block,
                    deadline_monotonic=deadline_monotonic,
                    remaining_bytes=budget,
                    connection=connection,
                ).decode("utf-8")
            else:
                budget.consume(len(canonical_json_bytes(block["tool_arguments"])))
            blocks.append(MappingProxyType(dict(block)))
            blocks_by_entry.setdefault(str(block["assistant_entry_id"]), []).append(
                block
            )
        for raw in page:
            prefix = (
                "executed" if raw["entry_owner_kind"] == "EXECUTED_TURN" else "imported"
            )
            group_key = str(
                raw["turn_id"]
                if prefix == "executed"
                else raw["imported_history_group_id"]
            )
            if raw[prefix + "_status"] not in {"COMPLETED", "INTERRUPTED"}:
                raise ConversationKernelConflict(
                    "Fork effective history contains an unsettled group"
                )
            groups[group_key] = FrozenForkGroup(
                group_key,
                str(raw[prefix + "_status"]),
                raw[prefix + "_accepted"],
                raw[prefix + "_terminal"],
                raw[prefix + "_final"],
            )
            # Validate original content too: assistant manifests remain storage
            # data and are never used in place of their ordered blocks.
            assistant = raw["entry_kind"] in {
                "ASSISTANT_MESSAGE",
                "ASSISTANT_TOOL_REQUEST",
            }
            # Storage manifests never consume the canonical provider budget.
            if raw["entry_kind"] in {"USER_MESSAGE", "USER_STEER"}:
                if raw["content_media_type"] != PROMPT_BODY_MEDIA_TYPE:
                    raise ConversationKernelConflict(
                        "Fork ROOT human prompt is not canonical typed content"
                    )
                prompt = hydrate_canonical_prompt_owner(
                    connection,
                    row=raw,
                    transcript_entry_id=str(raw["id"]),
                )
                budget.consume(prompt.resource_quote.canonical_expanded_bytes)
                body = prompt.body
            else:
                body = reader._read_content(
                    raw,
                    deadline_monotonic=deadline_monotonic,
                    remaining_bytes=_RemainingReadBudget(int(raw["content_size"]))
                    if assistant
                    else budget,
                    connection=connection,
                )
            attributed = historical_source_attribution(raw)
            if raw["content_codec"] == "utf-8":
                text = body.decode("utf-8")
                if raw["entry_kind"] == "PLAN_CONTINUATION":
                    _project_plan_continuation_storage(attributed, text)
                elif raw["entry_kind"] == "TERMINAL_OBSERVATION":
                    _validate_terminal_observation_content(body)
                elif raw["entry_kind"] == "USER_CONTROL_FEEDBACK":
                    if raw["content_media_type"] != USER_CONTROL_FEEDBACK_MEDIA_TYPE:
                        raise ConversationKernelConflict(
                            "Fork user control feedback descriptor is invalid"
                        )
                    project_user_control_feedback_for_provider(body)
                elif raw["entry_kind"] == "INTER_AGENT_MESSAGE":
                    completion = validate_subagent_completion_storage_body(body)
                    if completion["task_id"] != attributed["source_subagent_task_id"]:
                        raise ConversationKernelConflict(
                            "Fork completion attribution differs"
                        )
            elif raw["entry_kind"] not in {
                "ASSISTANT_MESSAGE",
                "ASSISTANT_TOOL_REQUEST",
            }:
                raise ConversationKernelConflict(
                    "Fork request/result encoding is not UTF-8"
                )
            entry = {
                key: value
                for key, value in raw.items()
                if not key.startswith(
                    (
                        "executed_",
                        "imported_accepted",
                        "imported_terminal",
                        "imported_final",
                        "imported_status",
                    )
                )
            }
            entry["source_group_key"] = group_key
            entries.append(MappingProxyType(entry))
        if max(len(entries), len(blocks)) > MAXIMUM_CANONICAL_PROVIDER_INPUT_ITEMS:
            raise ConversationKernelConflict(
                "Fork effective context exceeds canonical item admission"
            )
        last = (int(page[-1]["entry_sequence"]), str(page[-1]["id"]))

    entry_ids = {str(row["id"]) for row in entries}
    call_ids = {
        str(b["assistant_entry_id"]) for b in blocks if b["block_kind"] == "TOOL_CALL"
    }
    # Results are copied only when their request is in the effective window.
    raw_results = (
        connection.execute(
            """SELECT r.*, e.entry_sequence FROM pulsara_v3.tool_results AS r
           JOIN pulsara_v3.transcript_entries AS e ON e.session_id = r.session_id AND e.id = r.result_entry_id
           WHERE r.session_id = %s AND r.tool_call_entry_id = ANY(%s)
             AND e.entry_sequence <= %s ORDER BY e.entry_sequence""",
            (source_session_id, list(call_ids), anchor.source_entry_sequence),
        ).fetchall()
        if call_ids
        else []
    )
    copied_result_ids = {str(row["result_entry_id"]) for row in raw_results}
    entries = [
        row
        for row in entries
        if row["entry_kind"] != "TOOL_RESULT" or row["id"] in copied_result_ids
    ]
    if not entries or entries[-1]["id"] != anchor_entry_id:
        raise ConversationKernelConflict(
            "Fork copy upper bound does not include its final"
        )
    for raw in raw_results:
        if raw["result_entry_id"] not in entry_ids:
            raise ConversationKernelConflict(
                "Fork result falls outside its effective range"
            )
        safe = dict(raw)
        for key, value in safe.items():
            if isinstance(value, list):
                safe[key] = tuple(value)
        for key in (
            "permission_snapshot_fingerprint",
            "attempt_id",
            "control_plan_workflow_id",
            "control_plan_interaction_id",
        ):
            safe.pop(key)
        results.append(MappingProxyType(safe))
        blob_id = raw["output_artifact_blob_id"]
        if blob_id is not None:
            artifact = connection.execute(
                "SELECT id, workspace_id, logical_digest, logical_size, media_type, codec "
                "FROM pulsara_v3.blobs WHERE id = %s AND workspace_id = %s",
                (blob_id, session["workspace_id"]),
            ).fetchone()
            if artifact is None:
                raise ConversationKernelConflict(
                    "Fork artifact does not exact-join its workspace"
                )
            artifacts[str(blob_id)] = MappingProxyType(dict(artifact))
    tool_state = reader._load_tool_state(
        connection,
        source_session_id,
        list(call_ids),
        provider_input_through_sequence=anchor.source_entry_sequence,
    )
    cuts = _next_assistant_cuts(entries)
    by_id = {str(row["id"]): row for row in entries}
    closures = []
    for block in blocks:
        if block["block_kind"] != "TOOL_CALL":
            continue
        entry_id = str(block["assistant_entry_id"])
        target = cuts.get(entry_id, anchor.source_entry_sequence)
        state = tool_state[(entry_id, str(block["tool_call_id"]))]
        result = state.get("result")
        if result is not None and int(result["entry_sequence"]) <= target:
            if state.get("imported_closure_kind") is not None:
                raise ConversationKernelConflict(
                    "Fork tool call has both visible result and frozen closure"
                )
            continue
        kind = historical_tool_closure_kind(
            state,
            owner_kind=str(by_id[entry_id]["entry_owner_kind"]),
            target_cut=target,
        )
        closures.append(
            MappingProxyType(
                {
                    "assistant_entry_id": entry_id,
                    "tool_call_id": block["tool_call_id"],
                    "target_provider_input_through_sequence": target,
                    "closure_kind": kind.name,
                }
            )
        )
    manifests = []
    replay_payload_bytes = 0
    for entry in entries:
        if entry["entry_kind"] not in {"ASSISTANT_MESSAGE", "ASSISTANT_TOOL_REQUEST"}:
            continue
        row = connection.execute(
            "SELECT * FROM pulsara_v3.provider_assistant_replay_fragments WHERE session_id = %s AND assistant_entry_id = %s",
            (source_session_id, entry["id"]),
        ).fetchone()
        if row is None:
            continue
        if row["assistant_entry_kind"] != entry["entry_kind"]:
            raise ConversationKernelConflict(
                "Fork replay attachment names a different assistant kind"
            )
        ordered = []
        calls = []
        text_parts = []
        for block in blocks_by_entry.get(str(entry["id"]), ()):
            if block["block_kind"] == "TOOL_CALL":
                arguments = canonical_json_bytes(block["tool_arguments"]).decode(
                    "utf-8"
                )
                calls.append(
                    LLMToolCall(
                        id=str(block["tool_call_id"]),
                        name=str(block["tool_name"]),
                        arguments=arguments,
                    )
                )
                ordered.append(
                    ("TOOL_CALL", block["tool_call_id"], block["tool_name"], arguments)
                )
            else:
                text = block_bodies[str(block["id"])]
                text_parts.append(text)
                ordered.append(
                    ("TEXT", text)
                    if block["block_kind"] == "TEXT"
                    else ("DATA", block["content_media_type"], text)
                )
        public = provider_assistant_public_projection_fingerprint(
            text="".join(text_parts),
            tool_calls=tuple(calls),
            ordered_blocks=tuple(ordered),
        )
        if public != row["public_projection_fingerprint"]:
            raise ConversationKernelConflict(
                "Fork native replay public projection differs from canonical blocks"
            )
        manifest = freeze_provider_replay_manifest(
            replay_id=str(row["id"]),
            assistant_entry_id=str(entry["id"]),
            wire_api=str(row["wire_api"]),
            codec_kind=str(row["codec_kind"]),
            provider_replay_contract_fingerprint=str(
                row["provider_replay_contract_fingerprint"]
            ),
            replay_target_fingerprint=str(row["replay_target_fingerprint"]),
            public_projection_fingerprint=str(row["public_projection_fingerprint"]),
            payload_digest=str(row["payload_digest"]),
            payload_size=int(row["payload_size"]),
            item_count=int(row["item_count"]),
            fragment_fingerprint=str(row["fragment_fingerprint"]),
        )
        manifests.append(manifest)
        replay_payload_bytes += manifest.payload_size
        metadata_bytes = manifest_cut_metadata_bytes(tuple(manifests))
        if metadata_bytes > MAXIMUM_CANONICAL_PROVIDER_INPUT_BYTES:
            raise ConversationKernelConflict(
                "Fork replay manifest metadata bound exceeded"
            )
        # Reuse the existing dispatch composite admission, not a new history cap.
        # Every copied native replay must be validated even if the next cold
        # epoch ultimately selects another provider target.
        quote_provider_dispatch_composite_bytes(
            canonical_compile_bytes=MAXIMUM_CANONICAL_PROVIDER_INPUT_BYTES
            - budget.remaining_bytes,
            manifest_metadata_bytes=metadata_bytes,
            selected_payload_bytes=replay_payload_bytes,
        )
        replays.append(
            (
                str(row["wire_api"]),
                decode_provider_replay_fragment(
                    manifest=manifest, payload_bytes=bytes(row["payload_bytes"])
                ),
            )
        )
    actual_ids = {str(row["id"]) for row in entries}
    actual_groups = {str(row["source_group_key"]) for row in entries}
    frozen_groups = tuple(
        FrozenForkGroup(
            g.source_group_key,
            g.settled_status,
            g.accepted_at,
            g.terminal_at,
            g.copied_final_source_entry_id
            if g.copied_final_source_entry_id in actual_ids
            else None,
        )
        for key, g in groups.items()
        if key in actual_groups
    )
    _deadline(deadline_monotonic)
    return FrozenForkHistoricalMaterial(
        source_session_id=source_session_id,
        **dict(session),
        anchor=anchor,
        snapshot=None if snapshot is None else MappingProxyType(dict(snapshot)),
        snapshot_carrier=carrier,
        retained_historical_requests=retained,
        imported_groups=frozen_groups,
        entries=tuple(entries),
        blocks=tuple(
            MappingProxyType(
                {
                    **b,
                    "tool_arguments": None
                    if b["tool_arguments"] is None
                    else freeze_json(b["tool_arguments"]),
                }
            )
            for b in blocks
        ),
        tool_results=tuple(results),
        required_tool_closures=tuple(closures),
        replay_fragments=tuple(replays),
        referenced_artifact_blobs=tuple(artifacts.values()),
    )
