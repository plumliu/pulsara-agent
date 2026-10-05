"""Task row mutations compose with the existing session, queue and event owners."""

from __future__ import annotations
from contextlib import contextmanager
import json
from datetime import datetime, timezone
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from pulsara_agent.scheduling.contracts import (
    ScheduledProvenance,
    ScheduledTaskError,
    next_occurrence,
    validate_rule,
    utc_instant,
)
from pulsara_agent.storage.postgres_connection_provider import PostgresConnectionLane
from pulsara_agent.conversation_kernel.repository_errors import SessionWriterConflict

from pulsara_agent.conversation_kernel.steer import (
    PromptIngressAccepted,
    PromptIngressConfirmation,
    PromptIngressConfirmationKind,
    PromptIngressWriteRejection,
    prompt_ingress_semantic_digest,
    _stable_id,
)
from pulsara_agent.conversation_kernel.queued_prompt_actions import QueuedPromptAction
from pulsara_agent.conversation_kernel.limits import STAGE2_LIMITS
from pulsara_agent.llm.model_connections import model_call_binding_from_dict
from pulsara_agent.primitives.permission import PermissionMode
from pulsara_agent.primitives.context import context_fingerprint
from pulsara_agent.conversation_kernel.prompt_storage import (
    canonical_prompt_owner_is_exact,
    hydrate_canonical_prompt_owner,
)
from pulsara_agent.conversation_kernel.prompt_content import freeze_canonical_prompt
from pulsara_agent.llm.input import FrozenPromptContent, LLMTextPart


_TASK_PROJECTION = """SELECT t.*, q.id AS pending_queue_item_id, latest.status AS last_turn_status,
                              latest.turn_id AS last_turn_id, latest.consumed_entry_id AS last_entry_id
                FROM pulsara_v3.scheduled_tasks t JOIN pulsara_v3.sessions s ON s.id=t.session_id
                LEFT JOIN LATERAL (SELECT id FROM pulsara_v3.prompt_queue_items WHERE session_id=t.session_id
                    AND scheduled_task_id=t.id AND status='PENDING' ORDER BY queue_sequence LIMIT 1) q ON true
                LEFT JOIN LATERAL (SELECT x.consumed_entry_id, tr.id AS turn_id, tr.status FROM pulsara_v3.prompt_queue_items x
                    JOIN pulsara_v3.transcript_entries e ON e.session_id=x.session_id AND e.id=x.consumed_entry_id
                    JOIN pulsara_v3.turns tr ON tr.session_id=e.session_id AND tr.id=e.turn_id
                    WHERE x.session_id=t.session_id AND x.scheduled_task_id=t.id ORDER BY x.queue_sequence DESC LIMIT 1) latest ON true
"""


class _ScheduledTaskOperations:
    @contextmanager
    def _scheduled_transaction(
        self, *, session_id, memory_domain_id, guard, deadline_monotonic
    ):
        scope = (
            self._writer_transaction(guard, deadline_monotonic=deadline_monotonic)
            if guard is not None
            else self._event_transaction(
                lane=PostgresConnectionLane.HOST_CONTROL,
                deadline_monotonic=deadline_monotonic,
            )
        )
        with scope as connection:
            row = connection.execute(
                """SELECT *, writer_lease_expires_at > clock_timestamp() AS lease_live,
                      clock_timestamp() AS database_now FROM pulsara_v3.sessions
                      WHERE id=%s AND memory_domain_id=%s FOR UPDATE""",
                (session_id, memory_domain_id),
            ).fetchone()
            if row is None:
                raise KeyError(session_id)
            if row["lifecycle"] != "OPEN":
                raise ScheduledTaskError(
                    "SESSION_ARCHIVED", "已归档会话不能管理或接受任务。"
                )
            if (
                guard is None
                and row["writer_lease_owner_id"] is not None
                and row["lease_live"]
            ):
                raise SessionWriterConflict("session has another live writer")
            if guard is not None and guard.session_id != session_id:
                raise ValueError("scheduled writer belongs to another session")
            yield connection, row

    def read_scheduled_task(self, *, task_id, memory_domain_id, deadline_monotonic):
        with self._provider.connection(
            lane=PostgresConnectionLane.INSPECTOR,
            row_factory=dict_row,
            deadline_monotonic=deadline_monotonic,
        ) as c:
            return c.execute(
                _TASK_PROJECTION + "WHERE t.id=%s AND s.memory_domain_id=%s",
                (task_id, memory_domain_id),
            ).fetchone()

    def list_scheduled_tasks(
        self,
        *,
        memory_domain_id,
        cursor=None,
        status=None,
        due_only=False,
        session_id=None,
        deadline_monotonic,
    ):
        if status is not None and status not in ("ACTIVE", "PAUSED", "COMPLETED", "ENABLED"):
            raise ScheduledTaskError("INVALID_STATUS", "任务状态无效。")
        with self._provider.connection(
            lane=PostgresConnectionLane.INSPECTOR,
            row_factory=dict_row,
            deadline_monotonic=deadline_monotonic,
        ) as c:
            with c.cursor(name="scheduled_task_page", row_factory=dict_row) as page:
                page.itersize = 1
                page.execute(
                    _TASK_PROJECTION
                    + """                WHERE s.memory_domain_id=%s AND s.lifecycle='OPEN' AND (%s::text IS NULL OR t.id>%s)
                AND (%s::text IS NULL OR t.status=%s OR (%s='ENABLED' AND t.status IN ('ACTIVE','COMPLETED')))
                AND (%s::text IS NULL OR t.session_id=%s)
                AND (NOT %s OR (t.status='ACTIVE' AND t.next_run_at<=clock_timestamp()))
                ORDER BY t.id LIMIT %s""",
                    (
                        memory_domain_id,
                        cursor,
                        cursor,
                        status,
                        status,
                        status,
                        session_id,
                        session_id,
                        due_only,
                        STAGE2_LIMITS.history_page_default_entries + 1,
                    ),
                )
                rows = []
                used = 0
                more = False
                for row in page:
                    size = len(
                        json.dumps(row, ensure_ascii=False, default=str).encode("utf-8")
                    )
                    if rows and (
                        len(rows) == STAGE2_LIMITS.history_page_default_entries
                        or used + size > STAGE2_LIMITS.history_page_hard_bytes
                    ):
                        more = True
                        break
                    rows.append(row)
                    used += size
        return rows, (str(rows[-1]["id"]) if more else None)

    def scheduled_pending_sessions(
        self, *, memory_domain_id, cursor=None, deadline_monotonic
    ):
        with self._provider.connection(
            lane=PostgresConnectionLane.INSPECTOR,
            row_factory=dict_row,
            deadline_monotonic=deadline_monotonic,
        ) as c:
            return c.execute(
                """SELECT DISTINCT q.session_id FROM pulsara_v3.prompt_queue_items q
                     JOIN pulsara_v3.sessions s ON s.id=q.session_id WHERE s.memory_domain_id=%s
                     AND s.lifecycle='OPEN' AND q.input_origin='SCHEDULED_TASK' AND q.status='PENDING'
                     AND (%s::text IS NULL OR q.session_id>%s) ORDER BY q.session_id LIMIT %s""",
                (
                    memory_domain_id,
                    cursor,
                    cursor,
                    STAGE2_LIMITS.history_page_default_entries,
                ),
            ).fetchall()

    def scheduled_next_time(self, *, memory_domain_id, deadline_monotonic):
        with self._provider.connection(
            lane=PostgresConnectionLane.INSPECTOR, deadline_monotonic=deadline_monotonic
        ) as c:
            return c.execute(
                """SELECT clock_timestamp(), min(t.next_run_at) FROM pulsara_v3.scheduled_tasks t
                JOIN pulsara_v3.sessions s ON s.id=t.session_id WHERE s.memory_domain_id=%s AND s.lifecycle='OPEN'
                AND t.status='ACTIVE' """,
                (memory_domain_id,),
            ).fetchone()

    def create_scheduled_task(
        self,
        *,
        task_id,
        session_id,
        memory_domain_id,
        guard,
        values,
        model_resolution_snapshot,
        deadline_monotonic,
    ):
        values = self._validate_scheduled_values(values)
        with self._scheduled_transaction(
            session_id=session_id,
            memory_domain_id=memory_domain_id,
            guard=guard,
            deadline_monotonic=deadline_monotonic,
        ) as (c, s):
            binding = model_call_binding_from_dict(s["model_call_binding"])
            if binding is None:
                raise ScheduledTaskError(
                    "MODEL_CONFIGURATION_REQUIRED", "请先为会话选择模型。"
                )
            model_resolution_snapshot.reconcile(binding)
            due = next_occurrence(
                values["schedule"], values["timezone"], s["database_now"]
            )
            if due is None:
                raise ScheduledTaskError(
                    "TIME_RANGE_EXHAUSTED", "没有可用的未来触发时点。"
                )
            return c.execute(
                """INSERT INTO pulsara_v3.scheduled_tasks
                (id, session_id, name, prompt, schedule, timezone, permission_mode, status, next_run_at, revision)
                VALUES (%s,%s,%s,%s,%s,%s,%s,'ACTIVE',%s,1) RETURNING *""",
                (
                    task_id,
                    session_id,
                    values["name"],
                    values["prompt"],
                    Jsonb(values["schedule"]),
                    values["timezone"],
                    values["permission_mode"],
                    due,
                ),
            ).fetchone()

    @staticmethod
    def _validate_scheduled_values(values):
        if not isinstance(values, dict) or set(values) != {
            "name",
            "prompt",
            "schedule",
            "timezone",
            "permission_mode",
        }:
            raise ScheduledTaskError("INVALID_REQUEST", "任务配置字段无效。")
        if any(
            not isinstance(values[k], str) or not values[k].strip()
            for k in ("name", "prompt")
        ):
            raise ScheduledTaskError("INVALID_REQUEST", "名称和提示词不能为空。")
        # Reuse canonical input's actual storage/admission boundary, no new small cap.
        freeze_canonical_prompt(
            FrozenPromptContent(
                (LLMTextPart(values["name"]), LLMTextPart(values["prompt"]))
            )
        )
        mode = PermissionMode(values["permission_mode"])
        return {
            **values,
            "schedule": validate_rule(values["schedule"], values["timezone"]),
            "permission_mode": mode.value,
        }

    def mutate_scheduled_task(
        self,
        *,
        task_id,
        session_id,
        memory_domain_id,
        guard,
        expected_revision,
        action,
        values=None,
        cancelled_candidates=None,
        deadline_monotonic,
    ):
        with self._scheduled_transaction(
            session_id=session_id,
            memory_domain_id=memory_domain_id,
            guard=guard,
            deadline_monotonic=deadline_monotonic,
        ) as (c, s):
            t = c.execute(
                "SELECT * FROM pulsara_v3.scheduled_tasks WHERE id=%s AND session_id=%s FOR UPDATE",
                (task_id, session_id),
            ).fetchone()
            if t is None:
                raise KeyError(task_id)
            if type(expected_revision) is not int or t["revision"] != expected_revision:
                raise ScheduledTaskError(
                    "REVISION_CONFLICT", "任务已修改，请重新加载。"
                )
            old = {
                k: t[k]
                for k in ("name", "prompt", "schedule", "timezone", "permission_mode")
            }
            new = old
            status, due = t["status"], t["next_run_at"]
            if action in ("pause", "delete"):
                self._cancel_scheduled_pending_in_connection(
                    c, guard, t, cancelled_candidates
                )
                if action == "delete":
                    c.execute(
                        "DELETE FROM pulsara_v3.scheduled_tasks WHERE id=%s", (task_id,)
                    )
                    return None
                if status == "ACTIVE":
                    status, due = "PAUSED", None
            elif action == "resume":
                if status == "COMPLETED":
                    raise ScheduledTaskError(
                        "TASK_COMPLETED", "一次性任务已派发，请修改时间或立即运行。"
                    )
                if status == "PAUSED":
                    due = next_occurrence(
                        t["schedule"], t["timezone"], s["database_now"]
                    )
                    if t["schedule"]["kind"] == "once" and due is None:
                        due = utc_instant(t["schedule"]["run_at_utc"])
                    if due is None:
                        raise ScheduledTaskError(
                            "TIME_RANGE_EXHAUSTED", "没有可用的未来时点。"
                        )
                    status = "ACTIVE"
            elif action == "update":
                new = self._validate_scheduled_values(values)
                effective = new["schedule"] != old["schedule"] or (
                    new["timezone"] != old["timezone"]
                    and new["schedule"]["kind"] not in ("once", "interval")
                )
                if (
                    new["schedule"] != old["schedule"]
                    and new["schedule"]["kind"] == "once"
                    and utc_instant(new["schedule"]["run_at_utc"]) <= s["database_now"]
                ):
                    raise ScheduledTaskError(
                        "INVALID_SCHEDULE", "新的一次性时间必须在未来。"
                    )
                if effective and status in ("ACTIVE", "COMPLETED"):
                    due = next_occurrence(
                        new["schedule"], new["timezone"], s["database_now"]
                    )
                    if due is None:
                        raise ScheduledTaskError(
                            "TIME_RANGE_EXHAUSTED", "没有可用的未来时点。"
                        )
                    status = "ACTIVE"
            else:
                raise ScheduledTaskError("INVALID_ACTION", "任务操作无效。")
            if new == old and status == t["status"] and due == t["next_run_at"]:
                return t
            return c.execute(
                """UPDATE pulsara_v3.scheduled_tasks SET name=%s,prompt=%s,schedule=%s,timezone=%s,permission_mode=%s,
                 status=%s,next_run_at=%s,revision=revision+1,updated_at=clock_timestamp() WHERE id=%s RETURNING *""",
                (
                    new["name"],
                    new["prompt"],
                    Jsonb(new["schedule"]),
                    new["timezone"],
                    new["permission_mode"],
                    status,
                    due,
                    task_id,
                ),
            ).fetchone()

    def _cancel_scheduled_pending_in_connection(
        self, c, guard, task, cancelled_candidates
    ):
        rows = c.execute(
            """SELECT id FROM pulsara_v3.prompt_queue_items WHERE session_id=%s AND scheduled_task_id=%s
                          AND status='PENDING' ORDER BY queue_sequence FOR UPDATE""",
            (task["session_id"], task["id"]),
        ).fetchall()
        for row in rows:
            candidate = QueuedPromptAction(
                task["session_id"],
                _stable_id(
                    "command", task["id"], str(task["revision"]), row["id"], "cancel"
                ),
                row["id"],
            )
            if cancelled_candidates is not None:
                cancelled_candidates.append(candidate)
            self._apply_queued_prompt_action_in_connection(
                c,
                guard,
                candidate=candidate,
                occurred_at=datetime.now(timezone.utc),
                actor_id="scheduled-task-management",
            )

    def _append_queue_management_events(
        self, c, guard, *, session_id, workspace_id, drafts
    ):
        if guard is not None:
            return self._append_events(
                c, guard, workspace_id=workspace_id, drafts=drafts
            )
        # The cold management owner acquired the same session FOR UPDATE lock and
        # excluded all live writers. Only original queue cancellation effects are
        # permitted here; this is not a new general append guard or lease.
        from pulsara_agent.conversation_kernel.vocabulary import CommittedEventType

        row = c.execute(
            "SELECT writer_lease_owner_id, writer_lease_expires_at>clock_timestamp() AS live FROM pulsara_v3.sessions WHERE id=%s FOR UPDATE",
            (session_id,),
        ).fetchone()
        if row is None or (row["writer_lease_owner_id"] is not None and row["live"]):
            raise SessionWriterConflict("cold queue management has a live writer")
        if any(d.event_type is not CommittedEventType.PROMPT_CANCELLED for d in drafts):
            raise ValueError("cold management can only append queue cancellations")
        start = self._allocate_event_range(c, session_id, len(drafts))
        events = tuple(
            self._insert_event(
                c,
                workspace_id=workspace_id,
                session_id=session_id,
                sequence=start + i,
                draft=d,
                turn_id=None,
            )
            for i, d in enumerate(drafts)
        )
        self._record_event_batch(events)
        return events

    @staticmethod
    def _queue_scheduled_provenance(row):
        if row["input_origin"] != "SCHEDULED_TASK":
            return None
        return ScheduledProvenance(
            str(row["scheduled_task_id"]),
            int(row["scheduled_task_revision"]),
            row["scheduled_due_at"].astimezone(timezone.utc).isoformat(),
        )

    def _scheduled_queue_matches(self, row, candidate):
        return row["input_origin"] == candidate.input_origin.value and (
            None
            if candidate.scheduled is None
            else ScheduledProvenance.from_dict(candidate.scheduled.provenance())
        ) == self._queue_scheduled_provenance(row)

    def _advance_scheduled_in_connection(self, c, task_id):
        t = c.execute(
            "SELECT *, clock_timestamp() AS now FROM pulsara_v3.scheduled_tasks WHERE id=%s FOR UPDATE",
            (task_id,),
        ).fetchone()
        status = "COMPLETED" if t["schedule"]["kind"] == "once" else "ACTIVE"
        due = None
        if status == "ACTIVE":
            try:
                due = next_occurrence(t["schedule"], t["timezone"], t["now"])
            except ScheduledTaskError as exc:
                if exc.code != "TIME_RANGE_EXHAUSTED":
                    raise
            if due is None:
                status = "PAUSED"
        c.execute(
            """UPDATE pulsara_v3.scheduled_tasks SET status=%s,next_run_at=%s,
            revision=revision+CASE WHEN status<>%s THEN 1 ELSE 0 END,updated_at=clock_timestamp() WHERE id=%s""",
            (status, due, status, task_id),
        )

    def _prepare_scheduled_dispatch_in_connection(self, c, guard, candidate):
        a = candidate.scheduled
        if candidate.canonical_prompt.content.parts != (LLMTextPart(a.prompt),):
            raise ScheduledTaskError("INVALID_REQUEST", "任务输入与保存指令不同。")
        t = c.execute(
            "SELECT *,clock_timestamp() AS now FROM pulsara_v3.scheduled_tasks WHERE id=%s AND session_id=%s FOR UPDATE",
            (a.task_id, a.session_id),
        ).fetchone()
        if t is None:
            raise ScheduledTaskError("TASK_NOT_FOUND", "任务不存在。")
        if (
            t["revision"] != a.revision
            or t["prompt"] != a.prompt
            or t["permission_mode"] != a.permission_mode.value
            or (
                not a.manual
                and (
                    t["status"] != "ACTIVE"
                    or t["next_run_at"] != a.due_at
                    or a.due_at > t["now"]
                )
            )
        ):
            raise ScheduledTaskError("TASK_CUT_CHANGED", "任务配置或触发时点已改变。")
        q = c.execute(
            """SELECT * FROM pulsara_v3.prompt_queue_items WHERE session_id=%s AND scheduled_task_id=%s
                      AND status='PENDING' ORDER BY queue_sequence LIMIT 1 FOR UPDATE""",
            (a.session_id, a.task_id),
        ).fetchone()
        if q is None:
            return None
        if a.manual:
            c.execute(
                """INSERT INTO pulsara_v3.session_commands(session_id,command_id,command_kind,request_schema_version,semantic_digest,target_kind,target_queue_item_id)
                VALUES (%s,%s,'RUN_SCHEDULED_TASK','run_scheduled_task.v1',%s,'QUEUE_ITEM',%s)""",
                (
                    a.session_id,
                    candidate.command_id,
                    prompt_ingress_semantic_digest(
                        candidate,
                        None
                        if q["model_call_binding"] is None
                        else model_call_binding_from_dict(q["model_call_binding"]),
                    ),
                    q["id"],
                ),
            )
        else:
            self._advance_scheduled_in_connection(c, a.task_id)
        # Merged target is read through the same original command/queue owner.
        return PromptIngressAccepted(
            int(q["queue_sequence"]),
            model_call_binding_from_dict(q["model_call_binding"]),
            queue_item_id=str(q["id"]),
        )

    def observe_scheduled_due(
        self,
        *,
        session_id,
        admission,
        memory_domain_id,
        guard,
        pause=False,
        deadline_monotonic,
    ):
        if session_id != admission.session_id:
            raise ValueError("scheduled admission belongs to another session")
        with self._scheduled_transaction(
            session_id=admission.session_id,
            memory_domain_id=memory_domain_id,
            guard=guard,
            deadline_monotonic=deadline_monotonic,
        ) as (c, s):
            t = c.execute(
                "SELECT * FROM pulsara_v3.scheduled_tasks WHERE id=%s AND session_id=%s FOR UPDATE",
                (admission.task_id, admission.session_id),
            ).fetchone()
            if (
                t is None
                or t["revision"] != admission.revision
                or t["status"] != "ACTIVE"
                or t["next_run_at"] != admission.due_at
                or t["next_run_at"] > s["database_now"]
            ):
                return "STALE"
            q = c.execute(
                """SELECT id FROM pulsara_v3.prompt_queue_items WHERE session_id=%s AND scheduled_task_id=%s AND status='PENDING' ORDER BY queue_sequence LIMIT 1 FOR UPDATE""",
                (admission.session_id, admission.task_id),
            ).fetchone()
            if q is not None:
                self._advance_scheduled_in_connection(c, t["id"])
                return "MERGED"
            if pause:
                c.execute(
                    "UPDATE pulsara_v3.scheduled_tasks SET status='PAUSED',next_run_at=NULL,revision=revision+1,updated_at=clock_timestamp() WHERE id=%s",
                    (t["id"],),
                )
                return "PAUSED"
            return "PREPARE"

    def confirm_scheduled_action(
        self, *, request, memory_domain_id, deadline_monotonic
    ):
        digest = context_fingerprint("pulsara:run-scheduled-task:v1", request)
        with self._provider.connection(
            lane=PostgresConnectionLane.INSPECTOR,
            row_factory=dict_row,
            deadline_monotonic=deadline_monotonic,
        ) as c:
            row = c.execute(
                """SELECT cmd.semantic_digest,cmd.command_kind,cmd.request_schema_version,q.* FROM pulsara_v3.session_commands cmd
                JOIN pulsara_v3.sessions s ON s.id=cmd.session_id
                JOIN pulsara_v3.prompt_queue_items q ON q.session_id=cmd.session_id AND q.id=cmd.target_queue_item_id
                WHERE cmd.session_id=%s AND cmd.command_id=%s AND s.memory_domain_id=%s""",
                (request["session_id"], request["client_command_id"], memory_domain_id),
            ).fetchone()
            if row is None:
                return None
            if (
                row["command_kind"] != "RUN_SCHEDULED_TASK"
                or row["request_schema_version"] != "run_scheduled_task.v1"
                or row["semantic_digest"] != digest
                or row["input_origin"] != "SCHEDULED_TASK"
                or row["scheduled_task_id"] != request["task_id"]
            ):
                raise ScheduledTaskError(
                    "COMMAND_CONFLICT", "命令对应另一份立即运行请求。"
                )
            self._validate_confirmed_manual_queue(c, row, request)
            return row

    def _validate_confirmed_manual_queue(self, c, q, request):
        provenance = self._queue_scheduled_provenance(q)
        if (
            q["input_origin"] != "SCHEDULED_TASK"
            or q["scheduled_task_id"] != request["task_id"]
            or q["delivery_mode"] != "NEW_TURN"
            or q["target_turn_id"] is not None
            or model_call_binding_from_dict(q["model_call_binding"]) is None
        ):
            raise ScheduledTaskError("COMMAND_CONFLICT", "原立即运行输入不自洽。")
        self._permission_from_row(q)
        prompt = hydrate_canonical_prompt_owner(c, row=q, queue_item_id=q["id"])
        if len(prompt.content.parts) != 1 or not isinstance(
            prompt.content.parts[0], LLMTextPart
        ):
            raise ScheduledTaskError("COMMAND_CONFLICT", "原定时输入内容无效。")
        if q["command_id"] == request["client_command_id"] and (
            provenance.task_revision != request["expected_revision"]
            or utc_instant(provenance.due_at_utc)
            != utc_instant(request["request_at_utc"])
        ):
            raise ScheduledTaskError("COMMAND_CONFLICT", "原立即运行输入来源不一致。")

    def _confirm_manual_ingress_in_connection(self, c, candidate, command):
        a = candidate.scheduled
        q = c.execute(
            "SELECT * FROM pulsara_v3.prompt_queue_items WHERE session_id=%s AND id=%s",
            (candidate.session_id, command["target_queue_item_id"]),
        ).fetchone()
        if (
            q is None
            or command["command_kind"] != "RUN_SCHEDULED_TASK"
            or command["request_schema_version"] != "run_scheduled_task.v1"
            or command["semantic_digest"]
            != context_fingerprint("pulsara:run-scheduled-task:v1", a.action_value())
            or q["input_origin"] != "SCHEDULED_TASK"
            or q["scheduled_task_id"] != a.task_id
        ):
            return PromptIngressConfirmation(
                PromptIngressConfirmationKind.CONFLICT,
                rejection=PromptIngressWriteRejection.COMMAND_CONFLICT,
            )
        try:
            self._validate_confirmed_manual_queue(c, q, a.action_value())
            if q["command_id"] == candidate.command_id and (
                command["target_queue_item_id"] != candidate.queue_item_id
                or q["client_submission_id"] != candidate.client_submission_id
                or q["delivery_mode"] != candidate.delivery_mode.value
                or q["target_turn_id"] != candidate.target_turn_id
                or q["permission_snapshot_id"] != candidate.permission_snapshot_id
                or q["requested_permission_mode"]
                != candidate.requested_permission_mode.value
                or not self._scheduled_queue_matches(q, candidate)
                or not canonical_prompt_owner_is_exact(
                    c,
                    row=q,
                    expected=candidate.canonical_prompt,
                    queue_item_id=candidate.queue_item_id,
                )
            ):
                return PromptIngressConfirmation(
                    PromptIngressConfirmationKind.CONFLICT,
                    rejection=PromptIngressWriteRejection.COMMAND_CONFLICT,
                )
        except (ScheduledTaskError, ValueError, TypeError):
            return PromptIngressConfirmation(
                PromptIngressConfirmationKind.CONFLICT,
                rejection=PromptIngressWriteRejection.COMMAND_CONFLICT,
            )
        return PromptIngressConfirmation(
            PromptIngressConfirmationKind.FULL_COMPATIBLE,
            queue_sequence=int(q["queue_sequence"]),
            status=str(q["status"]),
        )

    def merge_scheduled_manual(
        self, *, session_id, admission, memory_domain_id, guard, deadline_monotonic
    ):
        if session_id != admission.session_id:
            raise ValueError("scheduled admission belongs to another session")
        with self._scheduled_transaction(
            session_id=admission.session_id,
            memory_domain_id=memory_domain_id,
            guard=guard,
            deadline_monotonic=deadline_monotonic,
        ) as (c, s):
            command = c.execute(
                "SELECT * FROM pulsara_v3.session_commands WHERE session_id=%s AND command_id=%s",
                (admission.session_id, admission.client_command_id),
            ).fetchone()
            if command is not None:
                if (
                    command["request_schema_version"] != "run_scheduled_task.v1"
                    or command["command_kind"] != "RUN_SCHEDULED_TASK"
                    or command["semantic_digest"]
                    != context_fingerprint(
                        "pulsara:run-scheduled-task:v1", admission.action_value()
                    )
                ):
                    raise ScheduledTaskError("COMMAND_CONFLICT", "命令已用于另一操作。")
                q = c.execute(
                    "SELECT * FROM pulsara_v3.prompt_queue_items WHERE session_id=%s AND id=%s",
                    (admission.session_id, command["target_queue_item_id"]),
                ).fetchone()
                if q is None:
                    raise ScheduledTaskError("COMMAND_CONFLICT", "原命令没有队列目标。")
                self._validate_confirmed_manual_queue(c, q, admission.action_value())
                return q
            t = c.execute(
                "SELECT * FROM pulsara_v3.scheduled_tasks WHERE id=%s AND session_id=%s FOR UPDATE",
                (admission.task_id, admission.session_id),
            ).fetchone()
            if t is None:
                raise KeyError(admission.task_id)
            if t["revision"] != admission.revision:
                raise ScheduledTaskError(
                    "REVISION_CONFLICT", "任务已修改，请重新加载。"
                )
            q = c.execute(
                """SELECT * FROM pulsara_v3.prompt_queue_items WHERE session_id=%s AND scheduled_task_id=%s AND status='PENDING' ORDER BY queue_sequence LIMIT 1 FOR UPDATE""",
                (admission.session_id, admission.task_id),
            ).fetchone()
            if q is None:
                return None
            c.execute(
                """INSERT INTO pulsara_v3.session_commands(session_id,command_id,command_kind,request_schema_version,semantic_digest,target_kind,target_queue_item_id)
                VALUES (%s,%s,'RUN_SCHEDULED_TASK','run_scheduled_task.v1',%s,'QUEUE_ITEM',%s)""",
                (
                    admission.session_id,
                    admission.client_command_id,
                    context_fingerprint(
                        "pulsara:run-scheduled-task:v1", admission.action_value()
                    ),
                    q["id"],
                ),
            )
            return q
