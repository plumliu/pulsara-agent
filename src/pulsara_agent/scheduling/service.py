"""One process-local scheduler; accepted work belongs to the ordinary ROOT queue."""

from __future__ import annotations
from pulsara_agent.identity import derive_id, is_valid_id


import asyncio
from contextlib import suppress
from contextvars import ContextVar
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
    validate_rule,
    next_occurrence,
)
from pulsara_agent.workspace_identity import WorkspaceUnavailable
from pulsara_agent.primitives.context import canonical_json_bytes
from pulsara_agent.primitives.tool_result_budget import ToolResultPageBoundary

_LOG = logging.getLogger(__name__)


def task_payload(row: dict) -> dict:
    return {
        key: value.astimezone(utc_timezone.utc).isoformat()
        if isinstance(value, datetime)
        else value
        for key, value in row.items()
    }


def model_task_payload(payload: dict) -> dict:
    """Model identity handoff; native service/HTTP rows keep their own fields."""
    return {
        "target": schedule_target(payload["id"]),
        "session": {"kind": "SESSION", "session_id": payload["session_id"]},
        **{key: value for key, value in payload.items() if key not in {"id", "session_id"}},
    }


def model_task_summary(payload: dict) -> dict:
    value = model_task_payload(task_payload(payload))
    result = {key: value[key] for key in (
        "target", "session", "revision", "status", "schedule", "timezone", "permission_mode", "next_run_at"
    ) if key in value}
    result["inspect"] = {"tool": "query_schedules", "arguments": {"target": result["target"]}}
    return result


def model_management_receipt(action, payload):
    if action == "switch":
        return {"action": action, **{key: model_task_summary(payload[key]) for key in ("paused", "active")}}
    if action == "delete":
        return {"action": action, "target": schedule_target(payload["id"]), "deleted": True}
    return {"action": action, **model_task_summary(payload)}


def _require_model_result(page_budget, value):
    if not page_budget.fits(canonical_json_bytes(value).decode("utf-8")):
        raise ToolResultPageBoundary("The minimum schedule result does not fit this call's result allowance")


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

    async def list(self, *, cursor=None, status=None, session_id=None, model_summary=False):
        rows, cursor = await self._read(
            "list_scheduled_tasks", cursor=cursor, status=status, session_id=session_id,
            **({"model_summary": True} if model_summary else {})
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

    async def create(self, *, session_id, values, status=None, result_check=None):
        row = await self._manage(
            "create_scheduled_task",
            session_id=session_id,
            values=values,
            status=status,
            model_resolution_snapshot=self._core.model_runtime.freeze_resolution_snapshot(),
            **({"result_check": result_check} if result_check is not None else {}),
        )
        self.wake()
        return self._payload(row)

    async def mutate(self, *, task_id, expected_revision, action, values=None, result_check=None):
        row = await self.get(task_id)
        result = await self._manage(
            "mutate_scheduled_task",
            session_id=row["session_id"],
            task_id=task_id,
            expected_revision=expected_revision,
            action=action,
            values=values,
            **({"result_check": result_check} if result_check is not None else {}),
        )
        self._prepare_errors.pop(task_id, None)
        self.wake()
        return None if result is None else self._payload(result)

    async def switch(self, *, task_id, expected_revision, replacement, result_check=None):
        validate_action(
            dict(
                action="switch",
                target=schedule_target(task_id),
                expected_revision=expected_revision,
                replacement=replacement,
            )
        )
        source = await self.get(task_id)
        paused, active = await self._manage(
            "switch_scheduled_task",
            session_id=source["session_id"],
            task_id=task_id,
            expected_revision=expected_revision,
            replacement=replacement,
            model_resolution_snapshot=self._core.model_runtime.freeze_resolution_snapshot(),
            **({"result_check": result_check} if result_check is not None else {}),
        )
        self._prepare_errors.pop(task_id, None)
        self._prepare_errors.pop(active["id"], None)
        self.wake()
        return {"paused": self._payload(paused), "active": self._payload(active)}

    async def invoke_query(self, arguments, *, default_session_id, permission_mode, page_budget, memory_domain_id=None):
        token = self._memory_domain.set(memory_domain_id)
        try:
            return await self._invoke_query(arguments, default_session_id=default_session_id,
                permission_mode=permission_mode, page_budget=page_budget)
        finally:
            self._memory_domain.reset(token)

    async def _invoke_query(self, arguments, *, default_session_id, permission_mode, page_budget):
        value = dict(validate_query(dict(arguments)))
        if "target" in value:
            return model_task_payload(await self.get(value["target"]["task_id"]))
        # Model-only scope default; service/HTTP list(None) remains user-level.
        session = value.pop("session", None)
        value["session_id"] = (
            default_session_id if session is None else session["session_id"]
        )
        page = await self.list(**value, model_summary=True)
        tasks = page["tasks"]
        for count in range(len(tasks), -1, -1):
            if tasks and count == 0:
                break
            selected = tasks[:count]
            more = count < len(tasks) or page["next_cursor"] is not None
            result = {"tasks": [model_task_summary(task) for task in selected],
                "next": {"tool": "query_schedules", "arguments": {
                    "session": {"kind": "SESSION", "session_id": value["session_id"]},
                    **({"status": value["status"]} if "status" in value else {}),
                    "cursor": selected[-1]["id"]}} if more else None}
            if page_budget.fits(canonical_json_bytes(result).decode("utf-8")):
                return result
        raise ToolResultPageBoundary("The minimum schedule page does not fit this call's result allowance")

    async def invoke_manage(self, arguments, *, default_session_id, permission_mode, page_budget, memory_domain_id=None):
        token = self._memory_domain.set(memory_domain_id)
        try:
            return await self._invoke_manage(arguments, default_session_id=default_session_id,
                permission_mode=permission_mode, page_budget=page_budget)
        finally:
            self._memory_domain.reset(token)

    async def _invoke_manage(self, arguments, *, default_session_id, permission_mode, page_budget):
        value = dict(validate_action(dict(arguments)))
        action = value.pop("action")
        if action == "create" and value.get("session") is None:
            value["session"] = {"kind": "SESSION", "session_id": default_session_id}
        if action in {"create", "update", "switch"}:
            values = value.get("values")
            if action == "switch":
                replacement = value["replacement"]
                values = (
                    replacement["values"]
                    if replacement["kind"] == "create"
                    else await self.get(replacement["target"]["task_id"])
                )
            # A model can preserve or narrow its current preset, never grant
            # itself broader future permissions by saving task text/config.
            modes = list(PermissionMode)
            if not isinstance(values, dict) or "permission_mode" not in values:
                raise ScheduledTaskError("INVALID_REQUEST", "任务权限配置缺失。")
            requested = PermissionMode(values["permission_mode"])
            if modes.index(requested) > modes.index(permission_mode):
                raise ScheduledTaskError(
                    "PERMISSION_ESCALATION",
                    "当前会话权限不足以保存此任务配置，请由用户调整会话权限后再试。",
                )
        if "session" in value:
            value["session_id"] = value.pop("session")["session_id"]
        if "target" in value:
            value["task_id"] = value.pop("target")["task_id"]
        def result_check(payload):
            _require_model_result(page_budget, model_management_receipt(action, payload))
        if action == "create":
            result = await self.create(**value, result_check=result_check)
        elif action == "switch":
            result = await self.switch(**value, result_check=result_check)
            return model_management_receipt(action, result)
        else:
            result = await self.mutate(action=action, **value, result_check=result_check)
        return model_management_receipt(action, {"id": value["task_id"]} if action == "delete" else result)

    async def preview(self, *, schedule, timezone):
        rule = validate_rule(schedule, timezone)
        # Database clock is the only authority for due admission and preview.
        now, _ = await self._read("scheduled_next_time")
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

        if not isinstance(run_at_local, str) or len(run_at_local) != 16:
            raise ScheduledTaskError("INVALID_SCHEDULE", "运行时间格式无效。")
        try:
            naive = datetime.fromisoformat(run_at_local)
            zone = ZoneInfo(timezone)
        except (ValueError, TypeError, ZoneInfoNotFoundError) as exc:
            raise ScheduledTaskError("INVALID_SCHEDULE", "时间或时区无效。") from exc
        if (
            naive.tzinfo is not None
            or naive.isoformat(timespec="minutes") != run_at_local
        ):
            raise ScheduledTaskError("INVALID_SCHEDULE", "请填写当地日期和时间。")
        local = naive.replace(tzinfo=zone, fold=0)
        if not tz.datetime_exists(local):
            raise ScheduledTaskError(
                "INVALID_SCHEDULE", "这个当地时间因夏令时切换不存在，请选择其他时间。"
            )
        from datetime import timezone as datetime_timezone

        return {
            "schedule": validate_rule(
                {
                    "contract": "scheduled-rule:v1",
                    "kind": "once",
                    "run_at_utc": local.astimezone(datetime_timezone.utc).isoformat(),
                },
                timezone,
            )
        }

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
