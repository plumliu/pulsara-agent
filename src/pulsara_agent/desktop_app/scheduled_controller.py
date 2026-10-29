"""HTTP uses the shared closed schedule request owners."""

from __future__ import annotations
from aiohttp import web
from pulsara_agent.identity import is_valid_id
from pulsara_agent.scheduling.contracts import ScheduledTaskError
from pulsara_agent.scheduling.requests import schedule_target, validate_action, validate_query


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

    async def list(self, request):
        if set(request.query) - {"cursor", "status", "session_id"}:
            raise ScheduledTaskError("INVALID_REQUEST", "任务查询字段无效。")
        query = dict(request.query)
        if "session_id" in query:
            query["session"] = {"kind": "SESSION", "session_id": query.pop("session_id")}
        validate_query(query)
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
        validate_query({"target": schedule_target(request.match_info["task_id"])})
        return web.json_response(await self.service.get(request.match_info["task_id"]))

    async def create(self, request):
        value = await self.body(request, {"session_id", "values"}, {"status"})
        validate_action({"action": "create", "session": {"kind": "SESSION", "session_id": value["session_id"]},
                         **{key: field for key, field in value.items() if key != "session_id"}})
        return web.json_response(await self.service.create(**value), status=201)

    async def preview(self, request):
        value = await self.body(request, {"schedule", "timezone"})
        return web.json_response(await self.service.preview(**value))

    async def local_once(self, request):
        value = await self.body(request, {"run_at_local", "timezone"})
        return web.json_response(await self.service.local_once(**value))

    async def update(self, request):
        value = await self.body(request, {"expected_revision", "values"})
        validate_action({"action": "update", "target": schedule_target(request.match_info["task_id"]), **value})
        return web.json_response(
            await self.service.mutate(
                task_id=request.match_info["task_id"], action="update", **value
            )
        )

    async def action(self, request):
        action = request.match_info["action"]
        if action == "switch":
            value = await self.body(request, {"expected_revision", "replacement"})
            validate_action({"action": "switch", "target": schedule_target(request.match_info["task_id"]), **value})
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
        validate_action({"action": action, "target": schedule_target(request.match_info["task_id"]), **value})
        result = await self.service.mutate(
            task_id=request.match_info["task_id"], action=action, **value
        )
        return web.json_response({"task": result})
