"""One process-local scheduler; accepted work belongs to the ordinary ROOT queue."""

from __future__ import annotations

import asyncio
from contextlib import suppress
from datetime import datetime, timezone as utc_timezone
import logging
from uuid import uuid4

from pulsara_agent.conversation_kernel.io import KernelSessionIO
from pulsara_agent.scheduling.requests import validate_action
from pulsara_agent.conversation_kernel.host import (
    PromptBlockedByHook,
    KernelHostCoreClosing,
)
from pulsara_agent.conversation_kernel.repository_errors import SessionWriterConflict
from pulsara_agent.conversation_kernel.steer import _stable_id
from pulsara_agent.llm.input import PromptContent, LLMTextPart
from pulsara_agent.llm.runtime import ModelRuntimeUnavailable
from pulsara_agent.primitives.permission import PermissionMode
from pulsara_agent.scheduling.contracts import (
    ScheduledAdmission,
    ScheduledTaskError,
    utc_instant,
    validate_rule,
    next_occurrence,
)
from pulsara_agent.workspace_identity import WorkspaceUnavailable

_LOG = logging.getLogger(__name__)


def task_payload(row: dict) -> dict:
    return {
        key: value.astimezone(utc_timezone.utc).isoformat()
        if isinstance(value, datetime)
        else value
        for key, value in row.items()
    }


class ScheduledTaskService:
    def __init__(self, sessions):
        self.sessions = sessions
        self._io = KernelSessionIO()
        self._wake = asyncio.Event()
        self._task: asyncio.Task | None = None
        # Advisory details only. No durable run-result or execution recovery.
        self._prepare_errors: dict[str, tuple[int, str]] = {}

    async def start(self):
        if self._task is None:
            self._task = asyncio.create_task(self._loop(), name="scheduled-tasks")
        self.wake()

    def wake(self):
        self._wake.set()

    async def aclose(self):
        task, self._task = self._task, None
        if task is not None:
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task
        await self._io.aclose(
            deadline_monotonic=self.sessions.core._canonical_deadline()
        )

    async def _repository(self):
        return await self.sessions.core._ensure_resources()

    async def _read(self, method, **kwargs):
        repo = await self._repository()
        return await self._io.run(
            getattr(repo, method),
            memory_domain_id=self.sessions.memory_domain_id,
            deadline_monotonic=self.sessions.core._canonical_deadline(),
            **kwargs,
        )

    async def _manage(self, method, *, session_id, **kwargs):
        repo = await self._repository()
        host = await self.sessions.scheduled_management_host(session_id)
        arguments = dict(
            session_id=session_id,
            memory_domain_id=self.sessions.memory_domain_id,
            deadline_monotonic=self.sessions.core._canonical_deadline(),
            **kwargs,
        )
        if host is None:
            return await self._io.run(getattr(repo, method), guard=None, **arguments)
        return await host.apply_scheduled_management(method, arguments)

    def _payload(self, row):
        payload = task_payload(row)
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
            "list_scheduled_tasks", cursor=cursor, status=status, session_id=session_id
        )
        return {"tasks": [self._payload(row) for row in rows], "next_cursor": cursor}

    async def get(self, task_id):
        row = await self._read("read_scheduled_task", task_id=task_id)
        if row is None:
            raise KeyError(task_id)
        return self._payload(row)

    async def create(self, *, session_id, values):
        row = await self._manage(
            "create_scheduled_task",
            session_id=session_id,
            task_id=f"scheduled:{uuid4().hex}",
            values=values,
            model_resolution_snapshot=self.sessions.core._model_runtime.freeze_resolution_snapshot(),
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
        return None if result is None else self._payload(result)

    async def invoke(self, arguments, *, default_session_id, permission_mode):
        value = dict(validate_action(dict(arguments)))
        action = value.pop("action")
        if action in {"create", "update"}:
            values = value["values"]
            # A model can preserve or narrow its current preset, never grant
            # itself broader future permissions by saving task text/config.
            modes = list(PermissionMode)
            if not isinstance(values, dict) or "permission_mode" not in values:
                raise ScheduledTaskError("INVALID_REQUEST", "任务权限配置缺失。")
            requested = PermissionMode(values["permission_mode"])
            if modes.index(requested) > modes.index(permission_mode):
                raise ScheduledTaskError(
                    "PERMISSION_ESCALATION", "当前会话权限不足以保存此任务配置，请由用户调整会话权限后再试。"
                )
        if action in {"list", "create", "run_now"}:
            value.setdefault("session_id", default_session_id)
        if action == "list":
            return await self.list(**value)
        if action == "get":
            return await self.get(**value)
        if action == "create":
            return await self.create(**value)
        if action == "run_now":
            return await self.run_now(value)
        return await self.mutate(action=action, **value)

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
            raise ScheduledTaskError("INVALID_REQUEST", "立即运行参数无效。")
        if (
            any(
                not isinstance(request[k], str) or not request[k]
                for k in ("client_command_id", "session_id", "task_id")
            )
            or type(request["expected_revision"]) is not int
            or request["expected_revision"] < 1
        ):
            raise ScheduledTaskError("INVALID_REQUEST", "立即运行身份无效。")
        return {
            **request,
            "request_at_utc": utc_instant(request["request_at_utc"]).isoformat(),
        }

    async def run_now(self, request):
        request = self._run_request(request)
        # Confirmation precedes current task existence/revision/configuration.
        prior = await self._read("confirm_scheduled_action", request=request)
        if prior is not None:
            self.wake()
            return {"queue_item_id": prior["id"], "status": prior["status"]}
        task = await self.get(request["task_id"])
        if task["session_id"] != request["session_id"]:
            raise ScheduledTaskError("INVALID_REQUEST", "任务不属于指定会话。")
        a = ScheduledAdmission(
            task["id"],
            task["session_id"],
            request["expected_revision"],
            utc_instant(request["request_at_utc"]),
            task["prompt"],
            PermissionMode(task["permission_mode"]),
            manual=True,
            client_command_id=request["client_command_id"],
        )
        merged = await self._manage(
            "merge_scheduled_manual", session_id=a.session_id, admission=a
        )
        if merged is not None:
            self.wake()
            return {"queue_item_id": merged["id"], "status": merged["status"]}
        host = (
            await self.sessions.resume_session(a.session_id, scheduled=True)
        ).session
        outcome = await self._submit(host, a)
        if outcome.status == "REJECTED":
            raise ScheduledTaskError(
                outcome.public_code or "PREPARE_REJECTED",
                outcome.public_message or "任务未被接受。",
            )
        self.wake()
        return {
            "queue_item_id": outcome.prompt_delivery.queue_item_id
            if outcome.prompt_delivery is not None
            else outcome.target_id,
            "status": outcome.status,
        }

    async def _submit(self, host, admission):
        command = admission.client_command_id or _stable_id(
            "scheduled-tick",
            admission.task_id,
            str(admission.revision),
            admission.due_at.isoformat(),
        )
        return await host.submit_prompt(
            command_id=command,
            content=PromptContent((LLMTextPart(admission.prompt),)),
            requested_permission_mode=admission.permission_mode,
            _scheduled=admission,
        )

    async def _dispatch(self, row):
        from pulsara_agent.web_app.session_controller import SessionControlRejected

        a = ScheduledAdmission(
            row["id"],
            row["session_id"],
            row["revision"],
            row["next_run_at"].astimezone(utc_timezone.utc),
            row["prompt"],
            PermissionMode(row["permission_mode"]),
        )
        try:
            state = await self._manage(
                "observe_scheduled_due", session_id=a.session_id, admission=a
            )
            if state != "PREPARE":
                return
            host = (
                await self.sessions.resume_session(a.session_id, scheduled=True)
            ).session
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
        cursor = None
        while True:
            rows, cursor = await self._read(
                "list_scheduled_tasks", cursor=cursor, due_only=True
            )
            for row in rows:
                try:
                    await self._dispatch(row)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    # No provider body/credential or fabricated durable failure.
                    _LOG.warning(
                        "Scheduled tick waiting for resource: %s", type(exc).__name__
                    )
            if cursor is None:
                break
        # Accepted once/manual rows must wake even when no ACTIVE plan remains.
        cursor = None
        while True:
            rows = await self._read("scheduled_pending_sessions", cursor=cursor)
            for row in rows:
                try:
                    host = (
                        await self.sessions.resume_session(
                            row["session_id"], scheduled=True
                        )
                    ).session
                    host.wake_queued_inputs()
                except asyncio.CancelledError:
                    raise
                except Exception:
                    pass
            if not rows:
                break
            cursor = rows[-1]["session_id"]

    async def _loop(self):
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
