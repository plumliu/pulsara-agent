"""One process-local scheduler; accepted work belongs to the ordinary ROOT queue."""

from __future__ import annotations
from pulsara_agent.identity import derive_id, is_valid_id, require_id


import asyncio
from contextlib import suppress
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import datetime, timezone as utc_timezone
import logging

from pulsara_agent.conversation_kernel.io import KernelSessionIO
from pulsara_agent.conversation_kernel.session_text import display_title
from pulsara_agent.scheduling.requests import schedule_target, validate_action, validate_query
from pulsara_agent.conversation_kernel.host import (
    PromptBlockedByHook,
    KernelHostCoreClosing,
    SessionControlRejected,
)
from pulsara_agent.conversation_kernel.repository_errors import SessionWriterConflict
from pulsara_agent.llm.input import PromptContent, LLMTextPart
from pulsara_agent.llm.runtime import ModelRuntimeUnavailable
from pulsara_agent.primitives.permission import PermissionMode
from pulsara_agent.scheduling.contracts import (
    ScheduledAdmission,
    ScheduledTriggerSource,
    ScheduledTaskError,
    utc_instant,
    normalize_schedule_input,
    public_schedule_rule,
    next_occurrence,
)
from pulsara_agent.workspace_identity import WorkspaceUnavailable
from pulsara_agent.ports.tool_execution import FrozenToolJsonDict, ToolBusinessValue, freeze_tool_json_object
from pulsara_agent.conversation_kernel.content_parts import ResultPart

_LOG = logging.getLogger(__name__)


def task_payload(row: dict) -> dict:
    return {
        key: value.astimezone(utc_timezone.utc).isoformat()
        if isinstance(value, datetime)
        else value
        for key, value in row.items()
    }


def model_schedule_record(payload: dict) -> dict:
    return {"target": schedule_target(payload["id"]), **{key: payload[key] for key in (
        "name", "prompt", "schedule", "timezone", "permission_mode", "status", "next_run_at")}}


@dataclass(frozen=True, slots=True)
class ScheduledToolObservation:
    """One tool value and the revisions read with its actual schedule rows."""

    value: ToolBusinessValue
    result_facts: FrozenToolJsonDict

    def __post_init__(self) -> None:
        if not isinstance(self.value, ToolBusinessValue):
            raise TypeError("schedule observation requires a typed business value")
        object.__setattr__(self, "result_facts", freeze_tool_json_object(self.result_facts))

    @classmethod
    def from_rows(cls, value, rows):
        revisions = {}
        for row in rows:
            task_id = require_id(row["id"], "schedule")
            revision = row["revision"]
            if type(revision) is not int or revision < 1:
                raise ValueError("schedule observation requires the row's actual revision")
            revisions[task_id] = revision
        return cls(value, {"schedule_revisions": revisions} if revisions else {})


class ScheduledTaskService:
    def __init__(self, core):
        self._core = core
        self._memory_domain: ContextVar[str | None] = ContextVar("scheduled_task_memory_domain", default=None)
        self._io = KernelSessionIO()
        self._wake = asyncio.Event()
        self._task: asyncio.Task | None = None
        self._start_lock = asyncio.Lock()
        self._stopped = False
        self._run_started_at = datetime.now(utc_timezone.utc)
        # Advisory details only. No durable run-result or execution recovery.
        self._prepare_errors: dict[str, tuple[int, str]] = {}

    async def start(self):
        async with self._start_lock:
            if self._stopped:
                raise KernelHostCoreClosing("Scheduled task service is stopping")
            if self._task is None:
                # Use the database clock shared by schedule admission. This cutoff
                # stays fixed for the entire kernel run, including timer delays.
                self._run_started_at, _due = await self._read("scheduled_next_time")
                if self._stopped:
                    raise KernelHostCoreClosing("Scheduled task service is stopping")
                self._task = asyncio.create_task(self._loop(), name="scheduled-tasks")
            self.wake()

    def wake(self):
        self._wake.set()

    @property
    def physical_operation_deadline(self):
        return self._io.physical_operation_deadline

    @property
    def physical_close_complete(self) -> bool:
        return (self._stopped and (self._task is None or self._task.done())
                and self._io.physical_close_complete)

    async def stop(self):
        """Stop new timer deliveries and join admitted preparation before teardown."""
        self._stopped = True
        task = self._task
        if task is not None:
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task
            if self._task is task:
                self._task = None
        # A concurrent start may still own the database-clock read. It must
        # observe _stopped before publishing a loop, and finish before IO closes.
        async with self._start_lock:
            pass

    async def aclose(self):
        await self.stop()
        await self._io.aclose(deadline_monotonic=self._core._canonical_deadline())

    async def _repository(self):
        return await self._core._ensure_resources()

    async def _read(self, method, **kwargs):
        repo = await self._repository()
        return await self._io.run(
            getattr(repo, method),
            memory_domain_id=self._memory_domain.get() or self._core.memory_domain_id,
            deadline_monotonic=self._core._canonical_deadline(),
            **kwargs,
        )

    async def _manage(self, method, *, session_id, **kwargs):
        repo = await self._repository()
        domain = self._memory_domain.get() or self._core.memory_domain_id
        host = await self._core.scheduled_management_host(session_id, memory_domain_id=domain)
        arguments = dict(
            session_id=session_id,
            memory_domain_id=domain,
            deadline_monotonic=self._core._canonical_deadline(),
            **kwargs,
        )
        if host is None:
            return await self._io.run(getattr(repo, method), guard=None, **arguments)
        return await host.apply_scheduled_management(method, arguments)

    def _payload(self, row):
        payload = task_payload(row)
        payload["schedule"] = public_schedule_rule(row["schedule"], row["timezone"])
        if "session_title" in row:
            payload["session_title"] = display_title({"id": row["session_id"], "title": row["session_title"]})
        error = self._prepare_errors.get(row["id"])
        payload["prepare_error"] = (
            error[1] if error is not None and error[0] == row["revision"] else None
        )
        if (
            payload["prepare_error"] is None
            and row["status"] == "PAUSED"
            and row["schedule"]["kind"] != "once"
        ):
            try:
                exhausted = (
                    next_occurrence(row["schedule"], row["timezone"], row["updated_at"])
                    is None
                )
            except ScheduledTaskError as exc:
                exhausted = exc.code == "TIME_RANGE_EXHAUSTED"
            if exhausted:
                payload["prepare_error"] = "时间规则已超出可表示范围，请修改规则。"
        return payload

    async def list(self, *, cursor=None, status=None, session_id=None):
        rows, cursor = await self._read(
            "list_scheduled_tasks", cursor=cursor, status=status, session_id=session_id,
        )
        return {"tasks": [self._payload(row) for row in rows], "next_cursor": cursor}

    async def list_sessions(self, *, query="", cursor=None):
        rows, cursor = await self._read("list_scheduled_task_sessions", query=query, cursor=cursor)
        return {"sessions": rows, "next_cursor": cursor}

    async def get(self, task_id):
        row = await self._read("read_scheduled_task", task_id=task_id)
        if row is None:
            raise KeyError(task_id)
        return self._payload(row)

    async def create(self, *, session_id, values, status=None):
        row = await self._manage(
            "create_scheduled_task",
            session_id=session_id,
            values=values,
            status=status,
            model_resolution_snapshot=self._core.model_runtime.freeze_resolution_snapshot(),
        )
        self.wake()
        return self._payload(row)

    async def mutate(self, *, task_id, expected_revision, action, values=None):
        row = await self.get(task_id)
        result = await self._manage(
            "mutate_scheduled_task",
            session_id=row["session_id"],
            task_id=task_id,
            expected_revision=expected_revision,
            action=action,
            values=values,
        )
        self._prepare_errors.pop(task_id, None)
        self.wake()
        return None if action == "delete" or result is None else self._payload(result)

    async def switch(self, *, task_id, expected_revision, replacement):
        source = await self.get(task_id)
        paused, active = await self._manage(
            "switch_scheduled_task",
            session_id=source["session_id"],
            task_id=task_id,
            expected_revision=expected_revision,
            replacement=replacement,
            model_resolution_snapshot=self._core.model_runtime.freeze_resolution_snapshot(),
        )
        self._prepare_errors.pop(task_id, None)
        self._prepare_errors.pop(active["id"], None)
        self.wake()
        return {"paused": self._payload(paused), "active": self._payload(active)}

    async def invoke_query(self, arguments, *, default_session_id, permission_mode, memory_domain_id=None):
        token = self._memory_domain.set(memory_domain_id)
        try:
            value = dict(validate_query(dict(arguments)))
            if "target" in value:
                row = await self.get(value["target"]["task_id"])
                if row["session_id"] != default_session_id:
                    raise ScheduledTaskError("TASK_NOT_FOUND", "该定时任务不存在或不属于当前会话。")
                rows = (row,)
            else:
                raw_rows = await self._read("list_model_scheduled_tasks", session_id=default_session_id,
                    status=None if value.get("status", "ALL") == "ALL" else value["status"])
                rows = tuple(self._payload(row) for row in raw_rows)
            parts = []
            for row in rows:
                parts.append(ResultPart("object", model_schedule_record(row)))
                if row.get("prepare_error"):
                    parts.append(ResultPart("text", f"定时任务 {row['id']} 当前无法准备执行：{row['prepare_error']}"))
            if not rows:
                parts.append(ResultPart("text", "当前会话没有符合条件的定时任务。"))
            return ScheduledToolObservation.from_rows(ToolBusinessValue(content=tuple(parts)), rows)
        finally:
            self._memory_domain.reset(token)

    async def invoke_manage(self, arguments, *, default_session_id, permission_mode, memory_domain_id=None):
        token = self._memory_domain.set(memory_domain_id)
        try:
            value = dict(validate_action(dict(arguments)))
            action = value["action"]
            if action == "create":
                row = await self.create(session_id=default_session_id,
                    values={**value["values"], "permission_mode": permission_mode.value}, status=value.get("status"))
                public = {"target": schedule_target(row["id"]), "meta": {
                    "schedule_status": row["status"], "next_run_at": row["next_run_at"]}}
                text = "已创建并启用定时任务。" if row["status"] == "ACTIVE" else "已创建定时任务，当前为暂停状态。"
                return ScheduledToolObservation.from_rows(ToolBusinessValue(public_fields=public,
                    content=(ResultPart("text", text),)), (row,))
            task_id = value["target"]["task_id"]
            ids = [task_id]
            replacement = value.get("replacement")
            if action == "switch" and replacement["kind"] == "existing":
                ids.append(replacement["target"]["task_id"])
            revisions = await self._read("read_saved_schedule_revisions", session_id=default_session_id, task_ids=ids)
            if any(task_id not in revisions for task_id in ids):
                raise ScheduledTaskError("SNAPSHOT_REQUIRED", "请先用 query_schedules 查询目标计划，再执行此操作。")
            rows = [await self.get(task_id) for task_id in ids]
            if any(row["session_id"] != default_session_id for row in rows):
                raise ScheduledTaskError("TASK_NOT_FOUND", "该定时任务不存在或不属于当前会话。")
            if action == "resume" or (action == "switch" and replacement["kind"] == "existing"):
                enabled = rows[-1]
                if list(PermissionMode).index(PermissionMode(enabled["permission_mode"])) > list(PermissionMode).index(permission_mode):
                    raise ScheduledTaskError("PERMISSION_ESCALATION", "当前调用权限不足以启用这个计划，请由用户调整权限后再试。")
            if action == "switch":
                replacement = dict(replacement)
                if replacement["kind"] == "existing":
                    replacement["expected_revision"] = revisions[ids[-1]]
                else:
                    replacement["values"] = {**replacement["values"], "permission_mode": permission_mode.value}
                paused, active = await self._manage("switch_scheduled_task", session_id=default_session_id,
                    task_id=task_id, expected_revision=revisions[task_id], replacement=replacement,
                    model_resolution_snapshot=self._core.model_runtime.freeze_resolution_snapshot())
                self.wake()
                public = {"target": schedule_target(active["id"]), "meta": {
                    "schedule_status": active["status"], "next_run_at": task_payload(active)["next_run_at"]}}
                return ScheduledToolObservation.from_rows(ToolBusinessValue(public_fields=public,
                    content=(ResultPart("text", "已暂停原计划并启用替换计划。"),)), (paused, active))
            row = await self._manage("mutate_scheduled_task", session_id=default_session_id, task_id=task_id,
                expected_revision=revisions[task_id], action=action, values=value.get("values"))
            self._prepare_errors.pop(task_id, None)
            self.wake()
            if action == "update":
                text = "定时任务未发生变化。" if row["revision"] == revisions[task_id] else "已更新定时任务。"
            else:
                text = {"pause": "已暂停定时任务，尚未执行的排队输入已取消。", "resume": "已启用定时任务。", "delete": "已删除定时任务，已开始的回复继续运行。"}[action]
            public = {"target": schedule_target(task_id)}
            if action == "pause":
                public["meta"] = {"schedule_status": "PAUSED", "next_run_at": None}
            elif action == "resume":
                public["meta"] = {"schedule_status": row["status"], "next_run_at": task_payload(row)["next_run_at"]}
            return ScheduledToolObservation.from_rows(ToolBusinessValue(public_fields=public,
                content=(ResultPart("text", text),)), (row,))
        finally:
            self._memory_domain.reset(token)

    async def preview(self, *, schedule, timezone):
        # Database clock is the only authority for defaults and preview.
        now, _ = await self._read("scheduled_next_time")
        rule = normalize_schedule_input(schedule, timezone, now, creating=True)
        result = next_occurrence(rule, timezone, now)
        if result is None:
            raise ScheduledTaskError("TIME_RANGE_EXHAUSTED", "没有可用的未来触发时点。")
        from dateutil import tz
        from zoneinfo import ZoneInfo

        local = result.astimezone(ZoneInfo(timezone))
        return {
            "next_run_at": result.isoformat(),
            "local_time_fold": local.fold if tz.datetime_ambiguous(local) else None,
        }

    async def local_once(self, *, run_at_local, timezone):
        from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
        from dateutil import tz

        if not isinstance(run_at_local, str) or len(run_at_local) not in {16, 19}:
            raise ScheduledTaskError("INVALID_SCHEDULE", "运行时间格式无效。")
        try:
            naive = datetime.fromisoformat(run_at_local)
            zone = ZoneInfo(timezone)
        except (ValueError, TypeError, ZoneInfoNotFoundError) as exc:
            raise ScheduledTaskError("INVALID_SCHEDULE", "时间或时区无效。") from exc
        if (
            naive.tzinfo is not None
            or naive.isoformat(timespec="minutes" if len(run_at_local) == 16 else "seconds") != run_at_local
        ):
            raise ScheduledTaskError("INVALID_SCHEDULE", "请填写当地日期和时间。")
        local = naive.replace(tzinfo=zone, fold=0)
        if not tz.datetime_exists(local):
            raise ScheduledTaskError(
                "INVALID_SCHEDULE", "这个当地时间因夏令时切换不存在，请选择其他时间。"
            )
        return {"schedule": {"kind": "once", "run_at": naive.isoformat(timespec="seconds")}}

    @staticmethod
    def _run_request(request):
        if not isinstance(request, dict) or set(request) != {
            "client_command_id",
            "session_id",
            "task_id",
            "expected_revision",
            "request_at_utc",
        }:
            raise ScheduledTaskError("INVALID_REQUEST", "运行一次参数无效。")
        if any(
            not is_valid_id(request[field], kind)
            for field, kind in (
                ("client_command_id", "command"),
                ("session_id", "session"),
                ("task_id", "schedule"),
            )
        ):
            raise ScheduledTaskError("INVALID_ID", "运行一次身份无效。")
        if (
            type(request["expected_revision"]) is not int
            or request["expected_revision"] < 1
        ):
            raise ScheduledTaskError("INVALID_REQUEST", "运行一次身份无效。")
        return {
            **request,
            "request_at_utc": utc_instant(request["request_at_utc"]).isoformat(),
        }

    @staticmethod
    def _run_result(row):
        return {
            "queue_item_id": row["id"],
            "delivery_mode": row["delivery_mode"],
            "target_turn_id": row["target_turn_id"],
            "execution": (
                None if row["request_execution"] is None
                else row["request_execution"].to_dict()
            ),
        }

    async def run_now(self, request):
        # UI-only operation. Model management cannot dispatch a future turn;
        # a request to execute saved work in chat belongs to that current turn.
        request = {
            **self._run_request(request),
            "trigger_source": ScheduledTriggerSource.RUN_ONCE.value,
        }
        # Confirmation precedes current task existence/revision/configuration.
        prior = await self._read("confirm_scheduled_action", request=request)
        if prior is not None:
            self.wake()
            return self._run_result(prior)
        task = await self.get(request["task_id"])
        if task["session_id"] != request["session_id"]:
            raise ScheduledTaskError("INVALID_REQUEST", "任务不属于指定会话。")
        if task["status"] == "PAUSED" and task["schedule"]["kind"] == "once":
            retired = await self._manage(
                "retire_expired_paused_once", session_id=task["session_id"],
                task_id=task["id"], expected_revision=task["revision"],
            )
            if retired == "RETIRED":
                self._prepare_errors.pop(task["id"], None)
                raise ScheduledTaskError("TASK_NOT_FOUND", "任务不存在。")
        a = ScheduledAdmission(
            task["id"],
            task["session_id"],
            request["expected_revision"],
            utc_instant(request["request_at_utc"]),
            task["prompt"],
            PermissionMode(task["permission_mode"]),
            trigger_source=ScheduledTriggerSource.RUN_ONCE,
            client_command_id=request["client_command_id"],
        )
        if self._stopped:
            raise KernelHostCoreClosing("Scheduled task service is stopping")
        host = await self._core.resume_scheduled_session(a.session_id)
        outcome = await self._submit(host, a)
        if outcome.status == "REJECTED" and outcome.prompt_delivery is None:
            raise ScheduledTaskError(
                outcome.public_code or "PREPARE_REJECTED",
                outcome.public_message or "任务未被接受。",
            )
        self.wake()
        accepted = await self._read("confirm_scheduled_action", request=request)
        if accepted is None:
            raise RuntimeError("accepted run-once command has no queue target")
        return self._run_result(accepted)

    async def _submit(self, host, admission):
        command = admission.client_command_id or derive_id("command", "scheduled-tick", admission.task_id, str(admission.revision), admission.due_at.astimezone(utc_timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z"))
        return await host.submit_prompt(
            command_id=command,
            content=PromptContent((LLMTextPart(admission.prompt),)),
            requested_permission_mode=admission.permission_mode,
            _scheduled=admission,
        )

    async def _dispatch(self, row):
        a = ScheduledAdmission(
            row["id"],
            row["session_id"],
            row["revision"],
            row["next_run_at"].astimezone(utc_timezone.utc),
            row["prompt"],
            PermissionMode(row["permission_mode"]),
        )
        try:
            if a.due_at < self._run_started_at:
                await self._manage(
                    "retire_missed_scheduled_due", session_id=a.session_id,
                    admission=a, run_started_at=self._run_started_at,
                )
                return
            state = await self._manage(
                "observe_scheduled_due", session_id=a.session_id, admission=a
            )
            if state != "PREPARE":
                return
            if self._stopped:
                raise KernelHostCoreClosing("Scheduled task service is stopping")
            host = await self._core.resume_scheduled_session(a.session_id)
            outcome = await self._submit(host, a)
            if outcome.status == "REJECTED" and outcome.public_code in {
                "MODEL_CONFIGURATION_REQUIRED",
                "MODEL_CONFIGURATION_UNAVAILABLE",
                "INVALID_PROMPT",
                "HOOK_BLOCKED",
            }:
                await self._pause(a, outcome.public_message or "任务准备被拒绝。")
        except (
            WorkspaceUnavailable,
            ModelRuntimeUnavailable,
            PromptBlockedByHook,
        ) as exc:
            await self._pause(a, str(exc))
        except ScheduledTaskError as exc:
            if exc.code not in {
                "TASK_CUT_CHANGED",
                "TASK_NOT_FOUND",
                "REVISION_CONFLICT",
            }:
                _LOG.warning("Scheduled preparation rejected: %s", exc.code)
        except (
            SessionControlRejected,
            SessionWriterConflict,
            KernelHostCoreClosing,
            KeyError,
        ):
            # A lifecycle owner or another process has the writer. Reobserve the
            # database cut, without takeover or an artificial execution failure.
            return

    async def _pause(self, admission, detail):
        state = await self._manage(
            "observe_scheduled_due",
            session_id=admission.session_id,
            admission=admission,
            pause=True,
        )
        if state == "PAUSED":
            self._prepare_errors[admission.task_id] = (admission.revision + 1, detail)

    async def tick(self):
        while True:
            advanced = False
            cursor = None
            while True:
                rows, cursor = await self._read(
                    "list_scheduled_tasks", cursor=cursor, due_only=True
                )
                for row in rows:
                    try:
                        if row["status"] == "PAUSED":
                            state = await self._manage(
                                "retire_expired_paused_once", session_id=row["session_id"],
                                task_id=row["id"], expected_revision=row["revision"],
                            )
                            if state == "RETIRED":
                                self._prepare_errors.pop(row["id"], None)
                        else:
                            await self._dispatch(row)
                        current = await self._read("read_scheduled_task", task_id=row["id"])
                        if (
                            current is None or current["status"] != row["status"]
                            or current["next_run_at"] != row["next_run_at"]
                        ):
                            advanced = True
                    except asyncio.CancelledError:
                        raise
                    except Exception as exc:
                        # No provider body/credential or fabricated durable failure.
                        _LOG.warning(
                            "Scheduled tick waiting for resource: %s", type(exc).__name__
                        )
                if cursor is None:
                    break
            # Revisit every overdue task once per pass. Each occurrence remains
            # independent, while one delayed task cannot monopolize catch-up.
            if not advanced:
                break
        # Accepted once/manual rows must wake even when no ACTIVE plan remains.
        cursor = None
        while True:
            rows = await self._read("scheduled_pending_sessions", cursor=cursor)
            for row in rows:
                try:
                    if self._stopped:
                        return
                    host = await self._core.resume_scheduled_session(row["session_id"])
                    host.wake_queued_inputs()
                except asyncio.CancelledError:
                    raise
                except Exception:
                    pass
            if not rows:
                break
            cursor = rows[-1]["session_id"]

    async def _loop(self):
        # Timer scans belong to the explicit user-level owner even if start was
        # requested while an independent model invocation carried another domain.
        self._memory_domain.set(None)
        while True:
            self._wake.clear()
            try:
                await self.tick()
                now, due = await self._read("scheduled_next_time")
                delay = (
                    30.0
                    if due is None or due <= now
                    else min(30.0, (due - now).total_seconds())
                )
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                _LOG.warning(
                    "Scheduled timer waiting for database: %s", type(exc).__name__
                )
                delay = 30.0
            with suppress(asyncio.TimeoutError):
                await asyncio.wait_for(self._wake.wait(), timeout=delay)
