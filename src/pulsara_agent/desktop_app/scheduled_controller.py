"""HTTP uses the shared closed schedule request owners."""

from __future__ import annotations
from aiohttp import web
from jsonschema import Draft202012Validator
from pulsara_agent.identity import is_valid_id
from pulsara_agent.primitives.permission import PermissionMode
from pulsara_agent.scheduling.contracts import ScheduledTaskError
from pulsara_agent.scheduling.requests import validate_schedule_target, creation_status, schedule_values_schema


class ScheduledTaskController:
    def __init__(self, service):
        self.service = service

    def install(self, router):
        router.add_get("/api/scheduled-tasks", self.list)
        router.add_post("/api/scheduled-tasks", self.create)
        router.add_post("/api/scheduled-tasks/preview", self.preview)
        router.add_post("/api/scheduled-tasks/local-once", self.local_once)
        router.add_get("/api/scheduled-tasks/sessions", self.list_sessions)
        router.add_get("/api/scheduled-tasks/{task_id}", self.get)
        router.add_patch("/api/scheduled-tasks/{task_id}", self.update)
        router.add_post("/api/scheduled-tasks/{task_id}/{action}", self.action)

    @staticmethod
    async def body(request, fields, optional=()):
        value = await request.json()
        if (
            not isinstance(value, dict)
            or not set(fields) <= set(value)
            or set(value) - set(fields) - set(optional)
        ):
            raise ScheduledTaskError("INVALID_REQUEST", "请求字段无效。")
        return value

    @staticmethod
    def task_id(request):
        if not is_valid_id(request.match_info["task_id"], "schedule"):
            raise ScheduledTaskError("INVALID_ID", "定时任务身份无效。")

    @staticmethod
    def revision(value):
        if type(value) is not int or value < 1:
            raise ScheduledTaskError("INVALID_REQUEST", "请重新读取当前计划后再修改。")

    @staticmethod
    def values(value, *, creating):
        # The user page explicitly chooses permission; native actions inherit it.
        schema = schedule_values_schema(creating=creating)
        schema["properties"]["permission_mode"] = {
            "type": "string", "enum": [mode.value for mode in PermissionMode]
        }
        if creating:
            schema["required"].append("permission_mode")
        if not Draft202012Validator(schema).is_valid(value):
            raise ScheduledTaskError("INVALID_REQUEST", "任务配置字段无效。")

    async def list(self, request):
        if set(request.query) - {"cursor", "status", "session_id"}:
            raise ScheduledTaskError("INVALID_REQUEST", "任务查询字段无效。")
        for field, kind in (("cursor", "schedule"), ("session_id", "session")):
            if field in request.query and not is_valid_id(request.query[field], kind):
                raise ScheduledTaskError("INVALID_ID", "查询身份无效。")
        if "status" in request.query and request.query["status"] not in {"ACTIVE", "PAUSED"}:
            raise ScheduledTaskError("INVALID_STATUS", "任务状态无效。")
        return web.json_response(
            await self.service.list(
                cursor=request.query.get("cursor"), status=request.query.get("status"),
                session_id=request.query.get("session_id"),
            )
        )

    async def list_sessions(self, request):
        if set(request.query) - {"query", "cursor"}:
            raise ScheduledTaskError("INVALID_REQUEST", "会话查询字段无效。")
        if "cursor" in request.query and not is_valid_id(request.query["cursor"], "session"):
            raise ScheduledTaskError("INVALID_ID", "会话身份字段无效。")
        return web.json_response(await self.service.list_sessions(
            query=request.query.get("query", ""), cursor=request.query.get("cursor"),
        ))

    async def get(self, request):
        self.task_id(request)
        return web.json_response(await self.service.get(request.match_info["task_id"]))

    async def create(self, request):
        value = await self.body(request, {"session_id", "values"}, {"status"})
        if not is_valid_id(value["session_id"], "session"):
            raise ScheduledTaskError("INVALID_ID", "会话身份无效。")
        self.values(value["values"], creating=True)
        creation_status(value.get("status"))
        return web.json_response(await self.service.create(**value), status=201)

    async def preview(self, request):
        value = await self.body(request, {"schedule", "timezone"})
        return web.json_response(await self.service.preview(**value))

    async def local_once(self, request):
        value = await self.body(request, {"run_at_local", "timezone"})
        return web.json_response(await self.service.local_once(**value))

    async def update(self, request):
        value = await self.body(request, {"expected_revision", "values"})
        self.task_id(request)
        self.revision(value["expected_revision"])
        self.values(value["values"], creating=False)
        return web.json_response(
            await self.service.mutate(
                task_id=request.match_info["task_id"], action="update", **value
            )
        )

    async def action(self, request):
        self.task_id(request)
        action = request.match_info["action"]
        if action == "switch":
            value = await self.body(request, {"expected_revision", "replacement"})
            self.revision(value["expected_revision"])
            replacement = value["replacement"]
            if not isinstance(replacement, dict) or replacement.get("kind") not in {"existing", "create"}:
                raise ScheduledTaskError("INVALID_REQUEST", "切换目标无效。")
            if replacement["kind"] == "existing":
                if set(replacement) != {"kind", "target", "expected_revision"}:
                    raise ScheduledTaskError("INVALID_REQUEST", "切换目标字段无效。")
                validate_schedule_target(replacement["target"])
                self.revision(replacement["expected_revision"])
            elif set(replacement) != {"kind", "values"}:
                raise ScheduledTaskError("INVALID_REQUEST", "切换目标字段无效。")
            else:
                self.values(replacement["values"], creating=True)
            return web.json_response(await self.service.switch(task_id=request.match_info["task_id"], **value))
        if action == "run-now":
            value = await self.body(
                request,
                {
                    "client_command_id",
                    "session_id",
                    "task_id",
                    "expected_revision",
                    "request_at_utc",
                },
            )
            if value["task_id"] != request.match_info["task_id"]:
                raise ScheduledTaskError("INVALID_REQUEST", "任务身份不同。")
            return web.json_response(await self.service.run_now(value))
        if action not in {"pause", "resume", "delete"}:
            raise ScheduledTaskError("INVALID_ACTION", "任务操作无效。")
        value = await self.body(request, {"expected_revision"})
        self.revision(value["expected_revision"])
        result = await self.service.mutate(
            task_id=request.match_info["task_id"], action=action, **value
        )
        return web.json_response({"task": result})
