"""Session-owned delegation using the common request queue and turn facts."""

from __future__ import annotations

from datetime import datetime, timezone

from psycopg import IsolationLevel
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from pulsara_agent.conversation_kernel.prompt_content import freeze_canonical_prompt
from pulsara_agent.conversation_kernel.prompt_storage import (
    materialize_canonical_prompt,
)
from pulsara_agent.conversation_kernel.subagents.collaboration_contracts import (
    PreparedAgentRequest,
    validate_agent_request_graph,
)
from pulsara_agent.conversation_kernel.vocabulary import CommittedEventType, SubjectSlot
from pulsara_agent.llm.input import FrozenPromptContent, LLMTextPart
from pulsara_agent.llm.model_connections import model_call_binding_to_dict
from pulsara_agent.storage.postgres_connection_provider import PostgresConnectionLane

from .contracts import ConversationKernelConflict, _content_columns
from .locking import lock_session_admission
from .request_execution import REQUEST_EXECUTION_COLUMNS, request_execution_from_row


_WORK_SELECT = f"""SELECT q.*, {REQUEST_EXECUTION_COLUMNS}, qt.id AS turn_id,
    qt.final_entry_id, qt.terminal_public_detail, qt.terminal_at AS turn_terminal_at
    FROM pulsara_v3.prompt_queue_items q
    JOIN pulsara_v3.sessions s ON s.id=q.session_id
    LEFT JOIN pulsara_v3.transcript_entries qe ON qe.session_id=q.session_id AND qe.id=q.consumed_entry_id
    LEFT JOIN pulsara_v3.turns qt ON qt.session_id=qe.session_id AND qt.id=qe.turn_id
    WHERE s.root_session_id=%s AND q.input_origin='AGENT_INSTRUCTION'"""

_RUNTIME_SELECT = """SELECT s.*, s.id AS agent_id, s.id AS cursor, count(*) OVER() AS agent_total, w.workspace_kind, w.workspace_root, w.workspace_label,
    EXISTS(SELECT 1 FROM pulsara_v3.prompt_queue_items q WHERE q.session_id=s.id AND q.status='PENDING' AND q.delivery_mode='NEW_TURN') AS has_pending_work,
    EXISTS(SELECT 1 FROM pulsara_v3.turns t WHERE t.session_id=s.id AND t.status='RUNNING') AS has_running_turn,
    (SELECT q.id FROM pulsara_v3.prompt_queue_items q JOIN pulsara_v3.transcript_entries e ON e.session_id=q.session_id AND e.id=q.consumed_entry_id
       JOIN pulsara_v3.turns t ON t.session_id=e.session_id AND t.id=e.turn_id WHERE q.session_id=s.id AND t.status='RUNNING' LIMIT 1) AS current_work_id,
    (SELECT count(*) FROM pulsara_v3.prompt_queue_items q WHERE q.session_id=s.id AND q.status='PENDING' AND q.delivery_mode='NEW_TURN') AS queued_count,
    (SELECT q.id FROM pulsara_v3.prompt_queue_items q WHERE q.session_id=s.id AND q.input_origin='AGENT_INSTRUCTION' ORDER BY q.queue_sequence DESC LIMIT 1) AS latest_work_id,
    coalesce((SELECT q.waiting_on='[]'::jsonb FROM pulsara_v3.prompt_queue_items q
        WHERE q.session_id=s.id AND q.status='PENDING' AND q.delivery_mode='NEW_TURN'
        ORDER BY q.queue_sequence LIMIT 1),false) AS head_ready
    FROM pulsara_v3.sessions s JOIN pulsara_v3.workspaces w ON w.id=s.workspace_id AND w.memory_domain_id=s.memory_domain_id"""


class _SubagentOperations:
    @staticmethod
    def _agent_profile_text(profile):
        # The existing profile prose is shared verbatim; its editorial revision is deferred.
        from pulsara_agent.conversation_kernel.context_sources import (
            subagent_profile_text,
        )

        return subagent_profile_text(profile)

    def _agent_work_in_connection(self, connection, *, root_session_id, work_id):
        row = connection.execute(
            _WORK_SELECT + " AND q.id=%s", (root_session_id, work_id)
        ).fetchone()
        if row is None:
            return None
        return self._agent_work_value(connection, row)

    def _agent_work_value(self, connection, row):
        execution = request_execution_from_row(row)
        if execution is None:
            raise ConversationKernelConflict(
                "delegated work does not identify a NEW_TURN request"
            )
        value = {
            "agent_id": str(row["session_id"]),
            "work_id": str(row["id"]),
            "instruction": self._agent_prompt_text(connection, row),
            "status": execution.status.value,
            "terminal_reason": execution.terminal_reason,
            "turn_id": row["turn_id"],
            "accepted_at": row["accepted_at"],
            "profile": row["effective_profile"],
            "waiting_on": row["waiting_on"],
            "cursor": str(row["id"]),
            "request_execution": execution,
        }
        if execution.status.value == "completed":
            final = connection.execute(
                "SELECT * FROM pulsara_v3.transcript_entries WHERE session_id=%s AND id=%s",
                (row["session_id"], row["final_entry_id"]),
            ).fetchone()
            if final is None:
                raise ConversationKernelConflict(
                    "completed Work lacks its canonical final entry"
                )
            from pulsara_agent.conversation_kernel.history_projection import (
                canonical_history_message,
            )

            message = canonical_history_message(connection, final)
            value["final"] = "" if message is None else message["text"]
            value["final_entry_id"] = str(row["final_entry_id"])
        elif execution.status.value == "stopped":
            value["diagnostic"] = (
                row["terminal_public_detail"] or execution.terminal_reason
            )
        return value

    @staticmethod
    def _agent_prompt_text(connection, row):
        from pulsara_agent.conversation_kernel.prompt_storage import (
            hydrate_canonical_prompt_owner,
        )

        prompt = hydrate_canonical_prompt_owner(
            connection, row=row, queue_item_id=str(row["id"])
        )
        return "".join(
            p.text for p in prompt.content.parts if isinstance(p, LLMTextPart)
        )

    def admit_agent_requests(
        self,
        root_guard,
        *,
        requests,
        source_turn_id,
        source_tool_attempt_id,
        model_call_binding,
        permission_snapshot,
        occurred_at,
        actor_id,
        deadline_monotonic,
    ):
        requests = tuple(requests)
        if (
            not requests
            or len(requests) > 16
            or any(not isinstance(x, PreparedAgentRequest) for x in requests)
        ):
            raise ValueError("one delegation admission accepts 1..16 prepared requests")
        if sum(len(x.depends_on) for x in requests) > 64:
            raise ValueError(
                "one delegation admission accepts at most 64 explicit dependencies"
            )
        if permission_snapshot.effective_mode.value != "bypass-permissions":
            raise ValueError("delegation requires bypass permissions")
        with self._writer_transaction(
            root_guard,
            deadline_monotonic=deadline_monotonic,
            strong_session_control=True,
        ) as connection:
            root = self._require_writer(connection, root_guard, lock=True)
            if root["root_session_id"] is not None:
                raise ConversationKernelConflict("only ROOT can delegate requests")
            parent = connection.execute(
                "SELECT * FROM pulsara_v3.turns WHERE session_id=%s AND id=%s",
                (root_guard.session_id, source_turn_id),
            ).fetchone()
            attempt = connection.execute(
                "SELECT a.*, i.turn_id FROM pulsara_v3.tool_execution_attempts a JOIN pulsara_v3.tool_invocations i ON i.session_id=a.session_id AND i.id=a.invocation_id WHERE a.session_id=%s AND a.id=%s",
                (root_guard.session_id, source_tool_attempt_id),
            ).fetchone()
            if (
                parent is None
                or attempt is None
                or attempt["turn_id"] != source_turn_id
                or self._permission_from_row(parent) != permission_snapshot
            ):
                raise ConversationKernelConflict(
                    "delegation source does not identify its frozen ROOT execution"
                )
            if (
                model_call_binding_to_dict(model_call_binding)
                != parent["model_call_binding"]
            ):
                raise ConversationKernelConflict(
                    "delegation model binding differs from source turn"
                )
            # ROOT serialization prevents a concurrent batch from introducing a combined cycle.
            existing_rows = connection.execute(
                _WORK_SELECT
                + " AND (q.status='PENDING' OR qt.status='RUNNING') ORDER BY q.session_id,q.queue_sequence",
                (root_guard.session_id,),
            ).fetchall()
            existing = tuple(
                (
                    str(r["id"]),
                    str(r["session_id"]),
                    tuple(x["work_id"] for x in r["waiting_on"]),
                )
                for r in existing_rows
            )
            validate_agent_request_graph(requests, existing)
            for agent_id in sorted({r.agent_id for r in requests}):
                lock_session_admission(connection, session_id=agent_id)
            batch_ids = {r.work_id for r in requests}
            if len(batch_ids) != len(requests):
                raise ValueError("duplicate Work identity")
            for req in requests:
                session = connection.execute(
                    "SELECT * FROM pulsara_v3.sessions WHERE id=%s FOR UPDATE",
                    (req.agent_id,),
                ).fetchone()
                if req.create_agent:
                    if session is not None:
                        raise ConversationKernelConflict(
                            "prepared Agent identity already exists"
                        )
                    connection.execute(
                        """INSERT INTO pulsara_v3.sessions
                        (id,root_session_id,workspace_id,memory_domain_id,lifecycle,writer_generation,title,model_call_binding)
                        VALUES (%s,%s,%s,%s,'OPEN',1,%s,%s)""",
                        (
                            req.agent_id,
                            root_guard.session_id,
                            root["workspace_id"],
                            root["memory_domain_id"],
                            req.name,
                            Jsonb(model_call_binding_to_dict(model_call_binding)),
                        ),
                    )
                elif (
                    session is None
                    or session["root_session_id"] != root_guard.session_id
                    or session["lifecycle"] != "OPEN"
                ):
                    raise ConversationKernelConflict(
                        "Agent does not belong to this ROOT or is unavailable"
                    )
                previous = connection.execute(
                    "SELECT effective_profile FROM pulsara_v3.prompt_queue_items WHERE session_id=%s AND input_origin='AGENT_INSTRUCTION' ORDER BY queue_sequence DESC LIMIT 1",
                    (req.agent_id,),
                ).fetchone()
                profile = req.profile or (
                    previous["effective_profile"] if previous else "general_worker"
                )
                waiting, materials = [], []
                for ordinal, work_id in enumerate(req.depends_on):
                    if work_id in batch_ids:
                        waiting.append({"work_id": work_id, "ordinal": ordinal})
                        continue
                    work = self._agent_work_in_connection(
                        connection,
                        root_session_id=root_guard.session_id,
                        work_id=work_id,
                    )
                    if work is None:
                        raise ConversationKernelConflict(
                            "prerequisite Work is unavailable"
                        )
                    if work["status"] == "completed":
                        materials.append(
                            {
                                "source_work_id": work_id,
                                "kind": "dependency",
                                "ordinal": ordinal,
                                "body": work["final"],
                            }
                        )
                    else:
                        waiting.append({"work_id": work_id, "ordinal": ordinal})
                for ordinal, work_id in enumerate(req.materials):
                    if work_id in req.depends_on:
                        continue
                    if work_id in batch_ids:
                        raise ValueError("unfinished batch work must use depends_on")
                    work = self._agent_work_in_connection(
                        connection,
                        root_session_id=root_guard.session_id,
                        work_id=work_id,
                    )
                    if work is None or work["status"] not in ("completed", "stopped"):
                        raise ConversationKernelConflict(
                            "material Work must have ended"
                        )
                    materials.append(
                        {
                            "source_work_id": work_id,
                            "kind": "material",
                            "ordinal": ordinal,
                            "body": work["final"]
                            if work["status"] == "completed"
                            else work.get("diagnostic") or work["terminal_reason"],
                        }
                    )
                prompt = freeze_canonical_prompt(
                    FrozenPromptContent((LLMTextPart(req.instruction),))
                )
                pub = materialize_canonical_prompt(
                    connection,
                    publisher=self._canonical_content_publisher,
                    workspace_id=str(root["workspace_id"]),
                    prompt=prompt,
                )
                sequence = connection.execute(
                    "UPDATE pulsara_v3.sessions SET latest_prompt_queue_sequence=latest_prompt_queue_sequence+1,idle_since=NULL,updated_at=clock_timestamp() WHERE id=%s RETURNING latest_prompt_queue_sequence",
                    (req.agent_id,),
                ).fetchone()["latest_prompt_queue_sequence"]
                connection.execute(
                    """INSERT INTO pulsara_v3.prompt_queue_items
                    (id,session_id,workspace_id,queue_sequence,delivery_mode,input_origin,
                     source_session_id,source_turn_id,source_tool_attempt_id,effective_profile,profile_text,
                     initial_context,input_materials,waiting_on,model_call_binding,status,
                     inline_content,blob_id,content_digest,content_size,content_media_type,content_codec,
                     permission_snapshot_id,requested_permission_mode,effective_permission_mode,permission_admission_source,
                     permission_overlay,permission_plan_context_ordinal,permission_plan_workflow_id,
                     permission_plan_revision_at_admission,permission_inherited_from_turn_id,permission_contract_id,
                     permission_contract_fingerprint,permission_snapshot_fingerprint)
                    VALUES (%s,%s,%s,%s,'NEW_TURN','AGENT_INSTRUCTION',%s,%s,%s,%s,%s,%s,%s,%s,%s,'PENDING',
                    %s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                    (
                        req.work_id,
                        req.agent_id,
                        root["workspace_id"],
                        sequence,
                        root_guard.session_id,
                        source_turn_id,
                        source_tool_attempt_id,
                        profile,
                        self._agent_profile_text(profile),
                        Jsonb(list(req.initial_context)),
                        Jsonb(materials),
                        Jsonb(waiting),
                        Jsonb(model_call_binding_to_dict(model_call_binding)),
                        *_content_columns(pub.body),
                        *self._permission_columns(permission_snapshot),
                    ),
                )
                self._agent_queue_event(
                    connection,
                    root_guard,
                    req.agent_id,
                    str(root["workspace_id"]),
                    req.work_id,
                    CommittedEventType.PROMPT_QUEUED,
                    occurred_at,
                    actor_id,
                    {"delivery_mode": "NEW_TURN", "input_origin": "AGENT_INSTRUCTION"},
                )
            # Resolve already-terminal prerequisites, including cascading stopped requests,
            # in the very same atomic admission transaction.
            self._resolve_agent_dependencies_in_connection(
                connection,
                root_session_id=root_guard.session_id,
                guard=root_guard,
                occurred_at=occurred_at,
                actor_id=actor_id,
            )
            return tuple(
                self._agent_work_in_connection(
                    connection, root_session_id=root_guard.session_id, work_id=r.work_id
                )
                for r in requests
            )

    def confirm_agent_requests(
        self,
        root_guard,
        *,
        requests,
        source_turn_id,
        source_tool_attempt_id,
        model_call_binding,
        permission_snapshot,
        deadline_monotonic,
    ):
        """Classify a prepared atomic admission after an uncertain commit acknowledgement.

        Stable request IDs and the original source attempt identify the accepted
        rows even after their mutable waiting conditions have been satisfied.
        """
        requests = tuple(requests)
        with self._provider.connection(
            lane=PostgresConnectionLane.HOST_CONTROL,
            row_factory=dict_row,
            isolation_level=IsolationLevel.REPEATABLE_READ,
            deadline_monotonic=deadline_monotonic,
        ) as connection:
            self._require_writer(connection, root_guard, lock=False)
            rows = tuple(
                connection.execute(
                    "SELECT q.*,s.root_session_id,s.title FROM pulsara_v3.prompt_queue_items q JOIN pulsara_v3.sessions s ON s.id=q.session_id WHERE q.id=%s",
                    (r.work_id,),
                ).fetchone()
                for r in requests
            )
            if all(row is None for row in rows):
                if any(
                    r.create_agent
                    and connection.execute(
                        "SELECT 1 FROM pulsara_v3.sessions WHERE id=%s", (r.agent_id,)
                    ).fetchone()
                    is not None
                    for r in requests
                ):
                    raise ConversationKernelConflict(
                        "Agent rows exist without their atomic requests"
                    )
                return None
            if any(row is None for row in rows):
                raise ConversationKernelConflict(
                    "only part of an atomic Agent admission exists"
                )
            for req, row in zip(requests, rows, strict=True):
                if (
                    row["root_session_id"] != root_guard.session_id
                    or row["session_id"] != req.agent_id
                    or row["source_session_id"] != root_guard.session_id
                    or row["source_turn_id"] != source_turn_id
                    or row["source_tool_attempt_id"] != source_tool_attempt_id
                    or row["input_origin"] != "AGENT_INSTRUCTION"
                    or self._agent_prompt_text(connection, row) != req.instruction
                    or row["initial_context"] != list(req.initial_context)
                    or req.profile is not None
                    and row["effective_profile"] != req.profile
                    or req.create_agent
                    and row["title"] != req.name
                    or row["model_call_binding"]
                    != model_call_binding_to_dict(model_call_binding)
                    or self._permission_from_row(row) != permission_snapshot
                ):
                    raise ConversationKernelConflict(
                        "Agent admission differs from its prepared source and inputs"
                    )
            return tuple(
                self._agent_work_in_connection(
                    connection, root_session_id=root_guard.session_id, work_id=r.work_id
                )
                for r in requests
            )

    def _agent_queue_event(
        self,
        connection,
        guard,
        session_id,
        workspace_id,
        work_id,
        event_type,
        at,
        actor,
        payload,
    ):
        if guard is None:
            return
        self._append_events(
            connection,
            guard,
            owned_session_id=session_id,
            workspace_id=workspace_id,
            drafts=(
                self._event(
                    event_type,
                    SubjectSlot.QUEUE_ITEM,
                    work_id,
                    session_id=session_id,
                    occurrence_key="",
                    occurred_at=at,
                    actor_kind="runtime",
                    actor_id=actor,
                    payload=payload,
                ),
            ),
        )

    def _resolve_agent_dependencies_in_connection(
        self,
        connection,
        *,
        root_session_id,
        guard,
        occurred_at,
        actor_id,
        session_id=None,
    ):
        changed = []
        # Each iteration only consumes a condition or terminalizes a request. No execution recovery.
        while True:
            rows = connection.execute(
                """SELECT q.* FROM pulsara_v3.prompt_queue_items q JOIN pulsara_v3.sessions s ON s.id=q.session_id
                WHERE s.root_session_id=%s AND q.input_origin='AGENT_INSTRUCTION' AND q.status='PENDING'
                AND q.waiting_on <> '[]'::jsonb ORDER BY q.session_id,q.queue_sequence FOR UPDATE OF q""",
                (root_session_id,),
            ).fetchall()
            progressed = False
            for row in rows:
                waiting = []
                materials = list(row["input_materials"])
                failure = None
                for condition in row["waiting_on"]:
                    work = self._agent_work_in_connection(
                        connection,
                        root_session_id=root_session_id,
                        work_id=condition["work_id"],
                    )
                    if work is None or work["status"] == "stopped":
                        failure = condition["work_id"]
                        break
                    if work["status"] == "completed":
                        materials.append(
                            {
                                "source_work_id": condition["work_id"],
                                "kind": "dependency",
                                "ordinal": condition["ordinal"],
                                "body": work["final"],
                            }
                        )
                    else:
                        waiting.append(condition)
                if failure is not None:
                    reason = "DEPENDENCY_NOT_SUCCESSFUL:" + failure
                    connection.execute(
                        "UPDATE pulsara_v3.prompt_queue_items SET status='REJECTED',terminal_reason=%s,terminal_at=clock_timestamp(),waiting_on='[]'::jsonb WHERE id=%s AND status='PENDING'",
                        (reason, row["id"]),
                    )
                    self._agent_queue_event(
                        connection,
                        guard,
                        str(row["session_id"]),
                        str(row["workspace_id"]),
                        str(row["id"]),
                        CommittedEventType.PROMPT_REJECTED,
                        occurred_at,
                        actor_id,
                        {"reason": reason},
                    )
                elif waiting != row["waiting_on"]:
                    materials.sort(
                        key=lambda x: (
                            0 if x["kind"] == "dependency" else 1,
                            x["ordinal"],
                        )
                    )
                    connection.execute(
                        "UPDATE pulsara_v3.prompt_queue_items SET waiting_on=%s,input_materials=%s WHERE id=%s",
                        (Jsonb(waiting), Jsonb(materials), row["id"]),
                    )
                else:
                    continue
                progressed = True
                changed.append(str(row["id"]))
            if not progressed:
                return tuple(changed)

    def resolve_agent_dependencies(self, *, session_id, deadline_monotonic):
        self._begin_event_batch()
        try:
            with self._provider.connection(
                lane=PostgresConnectionLane.HOST_CONTROL,
                row_factory=dict_row,
                deadline_monotonic=deadline_monotonic,
            ) as connection:
                root = connection.execute(
                    "SELECT coalesce(root_session_id,id) AS root_id FROM pulsara_v3.sessions WHERE id=%s",
                    (session_id,),
                ).fetchone()
                result = ()
                if root is not None:
                    lock_session_admission(connection, session_id=str(root["root_id"]))
                    row = connection.execute(
                        "SELECT * FROM pulsara_v3.sessions WHERE id=%s FOR UPDATE",
                        (root["root_id"],),
                    ).fetchone()
                    if row is not None:
                        result = self._resolve_agent_dependencies_in_connection(
                            connection,
                            root_session_id=str(row["id"]),
                            guard=None,
                            occurred_at=datetime.now(timezone.utc),
                            actor_id="dependency-owner",
                        )
            self._finish_event_batch(committed=True)
            return result
        except BaseException:
            self._finish_event_batch(committed=False)
            raise

    def query_agent_work(self, *, root_session_id, work_id, deadline_monotonic):
        with self._provider.connection(
            lane=PostgresConnectionLane.INSPECTOR,
            row_factory=dict_row,
            isolation_level=IsolationLevel.REPEATABLE_READ,
            deadline_monotonic=deadline_monotonic,
        ) as connection:
            return self._agent_work_in_connection(
                connection, root_session_id=root_session_id, work_id=work_id
            )

    def list_agent_sessions(
        self, *, root_session_id, cursor=None, limit=20, deadline_monotonic
    ):
        if limit < 1:
            raise ValueError("page limit must be positive")
        with self._provider.connection(
            lane=PostgresConnectionLane.INSPECTOR,
            row_factory=dict_row,
            deadline_monotonic=deadline_monotonic,
        ) as connection:
            return tuple(
                dict(x)
                for x in connection.execute(
                    _RUNTIME_SELECT
                    + " WHERE s.root_session_id=%s AND (%s::text IS NULL OR s.id>%s) ORDER BY s.id LIMIT %s",
                    (root_session_id, cursor, cursor, limit),
                ).fetchall()
            )

    def list_agent_works(
        self, *, root_session_id, agent_id, cursor=None, limit=20, deadline_monotonic
    ):
        if limit < 1:
            raise ValueError("page limit must be positive")
        with self._provider.connection(
            lane=PostgresConnectionLane.INSPECTOR,
            row_factory=dict_row,
            isolation_level=IsolationLevel.REPEATABLE_READ,
            deadline_monotonic=deadline_monotonic,
        ) as connection:
            if (
                connection.execute(
                    "SELECT 1 FROM pulsara_v3.sessions WHERE id=%s AND root_session_id=%s",
                    (agent_id, root_session_id),
                ).fetchone()
                is None
            ):
                raise ValueError("Agent does not belong to this ROOT")
            rows = connection.execute(
                _WORK_SELECT
                + """ AND q.session_id=%s AND (%s::text IS NULL OR q.queue_sequence >
                (SELECT queue_sequence FROM pulsara_v3.prompt_queue_items WHERE session_id=%s AND id=%s))
                ORDER BY q.queue_sequence LIMIT %s""",
                (root_session_id, agent_id, cursor, agent_id, cursor, limit),
            ).fetchall()
            return tuple(self._agent_work_value(connection, x) for x in rows)

    def list_root_agent_session_ids(self, *, root_session_id, deadline_monotonic):
        with self._provider.connection(
            lane=PostgresConnectionLane.INSPECTOR,
            row_factory=dict_row,
            deadline_monotonic=deadline_monotonic,
        ) as connection:
            return tuple(
                str(x["id"])
                for x in connection.execute(
                    "SELECT id FROM pulsara_v3.sessions WHERE root_session_id=%s ORDER BY id",
                    (root_session_id,),
                ).fetchall()
            )

    def list_all_agent_sessions(self, *, deadline_monotonic):
        with self._provider.connection(
            lane=PostgresConnectionLane.INSPECTOR,
            row_factory=dict_row,
            deadline_monotonic=deadline_monotonic,
        ) as connection:
            return tuple(
                dict(x)
                for x in connection.execute(
                    _RUNTIME_SELECT
                    + " WHERE s.root_session_id IS NOT NULL ORDER BY s.id"
                ).fetchall()
            )

    def read_agent_runtime_state(self, *, session_id, deadline_monotonic):
        with self._provider.connection(
            lane=PostgresConnectionLane.INSPECTOR,
            row_factory=dict_row,
            deadline_monotonic=deadline_monotonic,
        ) as connection:
            row = connection.execute(
                _RUNTIME_SELECT + " WHERE s.id=%s", (session_id,)
            ).fetchone()
            return None if row is None else dict(row)

    def read_session_root_id(self, *, session_id, deadline_monotonic):
        with self._provider.connection(
            lane=PostgresConnectionLane.INSPECTOR,
            row_factory=dict_row,
            deadline_monotonic=deadline_monotonic,
        ) as connection:
            row = connection.execute(
                "SELECT root_session_id FROM pulsara_v3.sessions WHERE id=%s",
                (session_id,),
            ).fetchone()
            if row is None:
                raise LookupError("session not found")
            return row["root_session_id"]

    def mark_agent_idle(self, *, session_id, idle=True, at=None, deadline_monotonic):
        with self._provider.connection(
            lane=PostgresConnectionLane.HOST_CONTROL,
            row_factory=dict_row,
            deadline_monotonic=deadline_monotonic,
        ) as connection:
            lock_session_admission(connection, session_id=session_id)
            connection.execute(
                "SELECT id FROM pulsara_v3.sessions WHERE id=%s FOR UPDATE",
                (session_id,),
            ).fetchone()
            row = connection.execute(
                _RUNTIME_SELECT + " WHERE s.id=%s", (session_id,)
            ).fetchone()
            if row is None or row["root_session_id"] is None:
                return None
            busy = row["has_pending_work"] or row["has_running_turn"]
            if busy or not idle:
                connection.execute(
                    "UPDATE pulsara_v3.sessions SET idle_since=NULL WHERE id=%s",
                    (session_id,),
                )
                return None
            return connection.execute(
                "UPDATE pulsara_v3.sessions SET idle_since=coalesce(idle_since,%s) WHERE id=%s RETURNING idle_since",
                (at or datetime.now(timezone.utc), session_id),
            ).fetchone()["idle_since"]

    def cancel_agent_requests(
        self,
        root_guard,
        *,
        agent_id,
        work_id=None,
        reason="USER_CANCELLED",
        occurred_at,
        actor_id,
        deadline_monotonic,
    ):
        with self._writer_transaction(
            root_guard,
            deadline_monotonic=deadline_monotonic,
            strong_session_control=True,
        ) as connection:
            lock_session_admission(connection, session_id=agent_id)
            session = connection.execute(
                "SELECT * FROM pulsara_v3.sessions WHERE id=%s FOR UPDATE", (agent_id,)
            ).fetchone()
            if session is None or session["root_session_id"] != root_guard.session_id:
                raise ConversationKernelConflict("Agent does not belong to ROOT")
            rows = connection.execute(
                _WORK_SELECT
                + " AND q.session_id=%s AND (%s::text IS NULL OR q.id=%s) AND (q.status='PENDING' OR qt.status='RUNNING' OR %s::text IS NOT NULL) ORDER BY q.queue_sequence FOR UPDATE OF q",
                (root_guard.session_id, agent_id, work_id, work_id, work_id),
            ).fetchall()
            ids = []
            for row in rows:
                ids.append(str(row["id"]))
                if row["status"] == "PENDING":
                    connection.execute(
                        "UPDATE pulsara_v3.prompt_queue_items SET status='CANCELLED',terminal_reason=%s,terminal_at=clock_timestamp(),waiting_on='[]'::jsonb WHERE session_id=%s AND id=%s AND status='PENDING'",
                        (reason, agent_id, row["id"]),
                    )
                    self._agent_queue_event(
                        connection,
                        root_guard,
                        agent_id,
                        str(session["workspace_id"]),
                        str(row["id"]),
                        CommittedEventType.PROMPT_CANCELLED,
                        occurred_at,
                        actor_id,
                        {"reason": reason},
                    )
            self._resolve_agent_dependencies_in_connection(
                connection,
                root_session_id=root_guard.session_id,
                guard=root_guard,
                occurred_at=occurred_at,
                actor_id=actor_id,
            )
            return tuple(
                self._agent_work_in_connection(
                    connection, root_session_id=root_guard.session_id, work_id=x
                )
                for x in ids
            )

    def enqueue_agent_steer(
        self,
        guard,
        *,
        target_turn_id,
        queue_item_id,
        source_session_id,
        source_work_id,
        text,
        occurred_at,
        actor_id,
        deadline_monotonic,
        input_origin="AGENT_MESSAGE",
    ):
        if input_origin not in {"AGENT_MESSAGE", "AGENT_MATERIAL"}:
            raise ValueError("invalid Agent steer origin")
        with self._writer_transaction(
            guard, deadline_monotonic=deadline_monotonic
        ) as connection:
            existing = connection.execute(
                "SELECT * FROM pulsara_v3.prompt_queue_items WHERE id=%s",
                (queue_item_id,),
            ).fetchone()
            if existing is not None:
                if (
                    existing["session_id"] != guard.session_id
                    or existing["source_session_id"] != source_session_id
                    or existing["source_work_id"] != source_work_id
                    or (
                        existing["target_turn_id"] != target_turn_id
                        and input_origin != "AGENT_MATERIAL"
                    )
                    or existing["input_origin"] != input_origin
                    or self._agent_prompt_text(connection, existing) != text
                ):
                    raise ConversationKernelConflict(
                        "message request identity was reused"
                    )
                return dict(existing)
            source = connection.execute(
                "SELECT coalesce(root_session_id,id) root_id FROM pulsara_v3.sessions WHERE id=%s",
                (source_session_id,),
            ).fetchone()
            target = connection.execute(
                "SELECT coalesce(root_session_id,id) root_id FROM pulsara_v3.sessions WHERE id=%s",
                (guard.session_id,),
            ).fetchone()
            if (
                source is None
                or target is None
                or source["root_id"] != target["root_id"]
            ):
                raise ConversationKernelConflict(
                    "message sender and receiver are outside the same collaboration"
                )
            turn = connection.execute(
                "SELECT workspace_id FROM pulsara_v3.turns WHERE session_id=%s AND id=%s AND status='RUNNING' FOR UPDATE",
                (guard.session_id, target_turn_id),
            ).fetchone()
            if turn is None:
                return None
            prompt = freeze_canonical_prompt(FrozenPromptContent((LLMTextPart(text),)))
            pub = materialize_canonical_prompt(
                connection,
                publisher=self._canonical_content_publisher,
                workspace_id=str(turn["workspace_id"]),
                prompt=prompt,
            )
            seq = connection.execute(
                "UPDATE pulsara_v3.sessions SET latest_prompt_queue_sequence=latest_prompt_queue_sequence+1 WHERE id=%s RETURNING latest_prompt_queue_sequence",
                (guard.session_id,),
            ).fetchone()["latest_prompt_queue_sequence"]
            row = connection.execute(
                """INSERT INTO pulsara_v3.prompt_queue_items
                (id,session_id,workspace_id,queue_sequence,delivery_mode,input_origin,target_turn_id,status,
                 source_session_id,source_work_id,inline_content,blob_id,content_digest,content_size,content_media_type,content_codec)
                VALUES (%s,%s,%s,%s,'STEER_ACTIVE_TURN',%s,%s,'PENDING',%s,%s,%s,%s,%s,%s,%s,%s) RETURNING *""",
                (
                    queue_item_id,
                    guard.session_id,
                    turn["workspace_id"],
                    seq,
                    input_origin,
                    target_turn_id,
                    source_session_id,
                    source_work_id,
                    *_content_columns(pub.body),
                ),
            ).fetchone()
            self._agent_queue_event(
                connection,
                guard,
                guard.session_id,
                str(turn["workspace_id"]),
                queue_item_id,
                CommittedEventType.PROMPT_QUEUED,
                occurred_at,
                actor_id,
                {"delivery_mode": "STEER_ACTIVE_TURN", "input_origin": input_origin},
            )
            result = dict(row)
            work = connection.execute(
                """SELECT q.id FROM pulsara_v3.prompt_queue_items q JOIN pulsara_v3.transcript_entries e ON e.session_id=q.session_id AND e.id=q.consumed_entry_id WHERE q.session_id=%s AND e.turn_id=%s AND q.input_origin='AGENT_INSTRUCTION' """,
                (guard.session_id, target_turn_id),
            ).fetchone()
            result["work_id"] = None if work is None else work["id"]
            return result

    def pending_agent_message_sources(
        self,
        *,
        session_id,
        turn_id,
        source_session_ids,
        after_by_sender,
        deadline_monotonic,
        accepted_through_sequence=0,
    ):
        if not source_session_ids:
            return {}
        with self._provider.connection(
            lane=PostgresConnectionLane.INSPECTOR,
            row_factory=dict_row,
            deadline_monotonic=deadline_monotonic,
        ) as connection:
            rows = connection.execute(
                """SELECT source_session_id,max(queue_sequence) AS latest_sequence FROM pulsara_v3.prompt_queue_items q
                LEFT JOIN pulsara_v3.transcript_entries e ON e.session_id=q.session_id AND e.id=q.consumed_entry_id
                WHERE q.session_id=%s AND (%s::text IS NULL OR q.target_turn_id=%s) AND q.input_origin='AGENT_MESSAGE'
                AND (q.status='PENDING' OR (q.status='CONSUMED' AND e.entry_sequence>%s))
                AND source_session_id=ANY(%s) GROUP BY source_session_id""",
                (
                    session_id,
                    turn_id,
                    turn_id,
                    accepted_through_sequence,
                    list(source_session_ids),
                ),
            ).fetchall()
            return {
                str(r["source_session_id"]): int(r["latest_sequence"])
                for r in rows
                if int(r["latest_sequence"])
                > after_by_sender.get(str(r["source_session_id"]), 0)
            }
