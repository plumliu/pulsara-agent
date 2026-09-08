"""The sole writer of sealed, child-owned imported conversation history."""

from __future__ import annotations

from bisect import bisect_right
from dataclasses import dataclass
import re
from time import monotonic

from psycopg import Connection, IsolationLevel, sql
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from pulsara_agent.conversation_kernel.compaction.contracts import (
    COMPACTION_SNAPSHOT_COMPILER_CONTRACT,
    CONTEXT_SNAPSHOT_CODEC,
    CONTEXT_SNAPSHOT_MEDIA_TYPE,
    CompactionContinuationMode,
    FrozenCompactionSummary,
)
from pulsara_agent.conversation_kernel.compaction.prompt import (
    build_compaction_snapshot_carrier,
)
from pulsara_agent.conversation_kernel.reader import CanonicalProviderInputReader
from pulsara_agent.llm.model_connections import model_call_binding_to_dict
from pulsara_agent.llm.provider_replay import rebind_durable_provider_assistant_replay
from pulsara_agent.primitives.context import context_fingerprint, thaw_json
from pulsara_agent.storage.postgres_connection_provider import PostgresConnectionLane
from .contracts import ConversationKernelConflict, _id


@dataclass(frozen=True, slots=True)
class CanonicalForkCreation:
    child_session_id: str
    created: bool
    public_code: str


_CONTENT_COLUMNS = (
    "inline_content",
    "blob_id",
    "content_digest",
    "content_size",
    "content_media_type",
    "content_codec",
)


def _insert(connection: Connection, table: str, values: dict[str, object]) -> None:
    """Local fixed-table insert; all identifiers originate in this owner."""
    connection.execute(
        sql.SQL("INSERT INTO pulsara_v3.{} ({}) VALUES ({})").format(
            sql.Identifier(table),
            sql.SQL(", ").join(map(sql.Identifier, values)),
            sql.SQL(", ").join(sql.Placeholder() for _ in values),
        ),
        tuple(values.values()),
    )


class _ForkOperations:
    def fork_conversation(
        self,
        *,
        source_session_id: str,
        anchor_entry_id: str,
        child_session_id: str,
        memory_domain_id: str,
        deadline_monotonic: float,
    ) -> CanonicalForkCreation:
        if re.fullmatch(r"session:[0-9a-f]{32}", child_session_id) is None:
            return CanonicalForkCreation(
                child_session_id, False, "INVALID_CHILD_SESSION_ID"
            )
        child_anchor_id = None
        try:
            with self._provider.connection(
                lane=PostgresConnectionLane.HOST_CONTROL,
                row_factory=dict_row,
                deadline_monotonic=deadline_monotonic,
                isolation_level=IsolationLevel.REPEATABLE_READ,
            ) as connection:
                if connection.execute(
                    "SELECT 1 FROM pulsara_v3.sessions WHERE id = %s",
                    (child_session_id,),
                ).fetchone():
                    return CanonicalForkCreation(
                        child_session_id, False, "CHILD_SESSION_ALREADY_EXISTS"
                    )
                source = connection.execute(
                    "SELECT 1 FROM pulsara_v3.sessions WHERE id = %s AND memory_domain_id = %s",
                    (source_session_id, memory_domain_id),
                ).fetchone()
                if source is None:
                    raise ConversationKernelConflict("FORK_SOURCE_UNAVAILABLE")
                material = CanonicalProviderInputReader(
                    self._provider
                ).read_fork_historical_material(
                    connection,
                    source_session_id,
                    anchor_entry_id,
                    deadline_monotonic,
                )
                entry_map = {
                    str(entry["id"]): _id("entry") for entry in material.entries
                }
                group_map = {
                    group.source_group_key: _id("history-group")
                    for group in material.imported_groups
                }
                sequence_map = {
                    str(entry["id"]): index
                    for index, entry in enumerate(material.entries, 1)
                }
                original_sequences = tuple(
                    int(entry["entry_sequence"]) for entry in material.entries
                )

                def local_cut(source_cut: int) -> int:
                    return bisect_right(original_sequences, source_cut)

                model = model_call_binding_to_dict(
                    material.anchor.anchor_model_call_binding
                )
                _insert(
                    connection,
                    "sessions",
                    {
                        "id": child_session_id,
                        "workspace_id": material.workspace_id,
                        "workspace_kind": material.workspace_kind,
                        "workspace_root": material.workspace_root,
                        "workspace_label": material.workspace_label,
                        "memory_domain_id": material.memory_domain_id,
                        "model_call_binding": Jsonb(model),
                        "lifecycle": "OPEN",
                        "writer_generation": 1,
                        "latest_entry_sequence": len(material.entries),
                    },
                )
                for group in material.imported_groups:
                    _insert(
                        connection,
                        "imported_history_groups",
                        {
                            "id": group_map[group.source_group_key],
                            "session_id": child_session_id,
                            "workspace_id": material.workspace_id,
                            "status": group.settled_status,
                            "accepted_at": group.accepted_at,
                            "terminal_at": group.terminal_at,
                            "final_entry_id": entry_map.get(
                                group.copied_final_source_entry_id
                            ),
                        },
                    )
                replay_map = {}
                for wire_api, fragment in material.replay_fragments:
                    rebuilt = rebind_durable_provider_assistant_replay(
                        fragment=fragment,
                        session_id=child_session_id,
                        workspace_id=material.workspace_id,
                        assistant_entry_id=entry_map[fragment.assistant_entry_id],
                        wire_api=wire_api,
                    )
                    replay_map[fragment.assistant_entry_id] = rebuilt
                for entry in material.entries:
                    if monotonic() >= deadline_monotonic:
                        raise TimeoutError(
                            "Fork canonical transaction deadline expired"
                        )
                    values = {key: entry[key] for key in _CONTENT_COLUMNS}
                    source_id = str(entry["id"])
                    values.update(
                        id=entry_map[source_id],
                        session_id=child_session_id,
                        workspace_id=material.workspace_id,
                        entry_owner_kind="IMPORTED_HISTORY",
                        imported_history_group_id=group_map[
                            str(entry["source_group_key"])
                        ],
                        entry_sequence=sequence_map[source_id],
                        entry_kind=entry["entry_kind"],
                        conversation_scope_kind="ROOT",
                        accepted_at=entry["accepted_at"],
                    )
                    source_prefix = (
                        "source_"
                        if entry["entry_owner_kind"] == "EXECUTED_TURN"
                        else "imported_source_"
                    )
                    for name in (
                        "subagent_task_id",
                        "plan_workflow_id",
                        "plan_interaction_id",
                        "plan_handoff_kind",
                    ):
                        values["imported_source_" + name] = entry[source_prefix + name]
                    if entry["entry_kind"] in {
                        "ASSISTANT_MESSAGE",
                        "ASSISTANT_TOOL_REQUEST",
                    }:
                        values.update(
                            provider_input_through_sequence=local_cut(
                                int(entry["provider_input_through_sequence"])
                            ),
                            provider_wire_api=entry["provider_wire_api"],
                            provider_replay_disposition=entry[
                                "provider_replay_disposition"
                            ],
                            provider_replay_fragment_id=None
                            if source_id not in replay_map
                            else replay_map[source_id].replay_id,
                        )
                    _insert(connection, "transcript_entries", values)
                for block in material.blocks:
                    values = {
                        key: block[key]
                        for key in (
                            *_CONTENT_COLUMNS,
                            "block_ordinal",
                            "block_kind",
                            "tool_call_id",
                            "tool_name",
                        )
                    }
                    values.update(
                        id=_id("block"),
                        session_id=child_session_id,
                        workspace_id=material.workspace_id,
                        assistant_entry_id=entry_map[str(block["assistant_entry_id"])],
                        tool_arguments=None
                        if block["tool_arguments"] is None
                        else Jsonb(thaw_json(block["tool_arguments"])),
                    )
                    _insert(connection, "assistant_message_blocks", values)
                for replay in replay_map.values():
                    _insert(
                        connection,
                        "provider_assistant_replay_fragments",
                        {
                            "id": replay.replay_id,
                            "session_id": child_session_id,
                            "workspace_id": material.workspace_id,
                            "assistant_entry_id": replay.assistant_entry_id,
                            "wire_api": replay.wire_api,
                            "codec_kind": replay.codec_kind.value,
                            "provider_replay_contract_fingerprint": replay.provider_replay_contract_fingerprint,
                            "replay_target_fingerprint": replay.replay_target_fingerprint,
                            "public_projection_fingerprint": replay.public_projection_fingerprint,
                            "payload_bytes": replay.payload_bytes,
                            "payload_digest": replay.payload_digest,
                            "payload_size": replay.payload_size,
                            "item_count": replay.item_count,
                            "fragment_fingerprint": replay.fragment_fingerprint,
                        },
                    )
                for result in material.tool_results:
                    values = {
                        key: list(value) if isinstance(value, tuple) else value
                        for key, value in result.items()
                    }
                    values.pop("entry_sequence")
                    values.update(
                        id=_id("tool-result"),
                        session_id=child_session_id,
                        workspace_id=material.workspace_id,
                        tool_call_entry_id=entry_map[str(result["tool_call_entry_id"])],
                        result_entry_id=entry_map[str(result["result_entry_id"])],
                        result_record_kind="IMPORTED_HISTORY",
                    )
                    _insert(connection, "tool_results", values)
                for closure in material.required_tool_closures:
                    values = dict(closure)
                    values.update(
                        session_id=child_session_id,
                        assistant_entry_id=entry_map[
                            str(closure["assistant_entry_id"])
                        ],
                        target_provider_input_through_sequence=local_cut(
                            int(closure["target_provider_input_through_sequence"])
                        ),
                    )
                    _insert(connection, "imported_tool_call_closures", values)
                snapshot_id = None
                floor = local_cut(material.anchor.source_through_sequence)
                if material.snapshot_carrier is not None:
                    old = material.snapshot_carrier
                    summary = FrozenCompactionSummary(
                        body=old.earlier_context_summary,
                        body_utf8_bytes=len(
                            old.earlier_context_summary.encode("utf-8")
                        ),
                        body_digest=context_fingerprint(
                            "pulsara.frozen-compaction-summary.v2-guided-freeform",
                            old.earlier_context_summary,
                        ),
                    )
                    carrier = build_compaction_snapshot_carrier(
                        summary=summary,
                        recent_user_messages=old.recent_user_messages,
                        continuation_mode=CompactionContinuationMode.AWAIT_NEXT_USER,
                        active_request=None,
                        retained_historical_requests=material.retained_historical_requests,
                    )
                    snapshot_id = _id("context-snapshot")
                    source = material.snapshot
                    _insert(
                        connection,
                        "context_snapshots",
                        {
                            "id": snapshot_id,
                            "session_id": child_session_id,
                            "workspace_id": material.workspace_id,
                            "source_through_sequence": floor,
                            "source_digest": context_fingerprint(
                                "pulsara.fork-genesis-snapshot.v1",
                                {
                                    "session_id": child_session_id,
                                    "source_through_sequence": floor,
                                    "content_digest": carrier.content_digest,
                                    "compiler_contract": COMPACTION_SNAPSHOT_COMPILER_CONTRACT,
                                    "prompt_contract": source["prompt_contract"],
                                    "model_contract": source["model_contract"],
                                },
                            ),
                            "compiler_contract": COMPACTION_SNAPSHOT_COMPILER_CONTRACT,
                            "prompt_contract": source["prompt_contract"],
                            "model_contract": source["model_contract"],
                            "inline_content": carrier.body,
                            "content_digest": carrier.content_digest,
                            "content_size": len(carrier.body),
                            "content_media_type": CONTEXT_SNAPSHOT_MEDIA_TYPE,
                            "content_codec": CONTEXT_SNAPSHOT_CODEC,
                        },
                    )
                child_anchor_id = entry_map[anchor_entry_id]
                _insert(
                    connection,
                    "session_context_genesis",
                    {
                        "session_id": child_session_id,
                        "workspace_id": material.workspace_id,
                        "anchor_entry_id": child_anchor_id,
                        "model_call_binding": Jsonb(model),
                        "base_kind": material.anchor.base_kind,
                        "source_through_sequence": floor,
                        "context_snapshot_id": snapshot_id,
                    },
                )
                # Force all deferred exact joins before the commit acknowledgement.
                connection.execute("SET CONSTRAINTS ALL IMMEDIATE")
        except Exception as error:
            # An ambiguous commit is resolved by the preselected child identity,
            # never by replaying the copy. If confirmation itself is unavailable,
            # let the transport fail so the client performs the same scoped lookup.
            if child_anchor_id is not None:
                with self._provider.connection(
                    lane=PostgresConnectionLane.INSPECTOR,
                    row_factory=dict_row,
                    deadline_monotonic=deadline_monotonic,
                ) as connection:
                    row = connection.execute(
                        "SELECT anchor_entry_id FROM pulsara_v3.session_context_genesis WHERE session_id = %s",
                        (child_session_id,),
                    ).fetchone()
                    if row is not None and row["anchor_entry_id"] == child_anchor_id:
                        return CanonicalForkCreation(child_session_id, True, "CREATED")
            return CanonicalForkCreation(
                child_session_id, False, f"FORK_NOT_CREATED: {error}"
            )
        return CanonicalForkCreation(child_session_id, True, "CREATED")
