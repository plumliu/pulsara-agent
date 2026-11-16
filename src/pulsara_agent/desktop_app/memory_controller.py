"""User-level memory CRUD, independent of any active conversation."""

from aiohttp import web
from pulsara_agent.conversation_kernel.memory.management import (
    MemoryManagementSelection,
)


class LocalMemoryController:
    def __init__(self, sessions):
        self.sessions = sessions

    @property
    def domain(self):
        return self.sessions.memory_domain_id

    @staticmethod
    def query(request, allowed):
        if set(request.query) - set(allowed) or any(
            len(request.query.getall(k)) != 1 for k in request.query
        ):
            raise ValueError("记忆查询参数无效。")
        return request.query

    @staticmethod
    def selection(values):
        return MemoryManagementSelection(
            values.get("view", "global"), values.get("workspace_id")
        )

    @staticmethod
    async def body(request, allowed, required):
        if request.content_type != "application/json":
            raise ValueError("记忆操作需要JSON正文。")
        value = await request.json()
        if (
            not isinstance(value, dict)
            or set(value) - allowed
            or not required <= set(value)
        ):
            raise ValueError("记忆操作参数无效。")
        return value

    async def projects(self, request):
        q = self.query(request, {"limit", "cursor"})
        return web.json_response(
            await self.sessions.core.memory_management_projects(
                memory_domain_id=self.domain,
                limit=int(q.get("limit", 40)),
                cursor=q.get("cursor"),
            )
        )

    async def catalog(self, request):
        q = self.query(
            request, {"view", "workspace_id", "kind", "search", "limit", "cursor"}
        )
        return web.json_response(
            await self.sessions.core.memory_management_catalog(
                memory_domain_id=self.domain,
                selection=self.selection(q),
                kind=q.get("kind"),
                search=q.get("search"),
                limit=int(q.get("limit", 40)),
                cursor=q.get("cursor"),
            )
        )

    async def detail(self, request):
        q = self.query(request, {"view", "workspace_id"})
        return web.json_response(
            await self.sessions.core.memory_management_detail(
                memory_domain_id=self.domain,
                selection=self.selection(q),
                fact_id=request.match_info["fact_id"],
            )
        )

    async def create(self, request):
        self.query(request, set())
        body = await self.body(
            request,
            {"view", "workspace_id", "kind", "title", "statement"},
            {"view", "kind", "title"},
        )
        if body["kind"] != "RESPONSE_PREFERENCE" and "statement" not in body:
            raise ValueError("普通记忆须提供正文。")
        return web.json_response(
            await self.sessions.core.memory_management_create(
                memory_domain_id=self.domain,
                selection=self.selection(body),
                kind=body["kind"],
                title=body["title"],
                statement=body.get("statement", ""),
            ),
            status=201,
        )

    async def edit(self, request):
        self.query(request, set())
        body = await self.body(
            request,
            {"view", "workspace_id", "expected_updated_at", "statement", "destination"},
            {"view", "expected_updated_at"},
        )
        if not {"statement", "destination"} & set(body):
            raise ValueError("请提供正文或保存范围。")
        if "statement" in body and not isinstance(body["statement"], str):
            raise TypeError("正文须为字符串。")
        destination = None
        if "destination" in body:
            value = body["destination"]
            if (
                not isinstance(value, dict)
                or set(value) - {"view", "workspace_id"}
                or "view" not in value
            ):
                raise ValueError("保存范围无效。")
            destination = self.selection(value)
        return web.json_response(
            await self.sessions.core.memory_management_edit(
                memory_domain_id=self.domain,
                selection=self.selection(body),
                fact_id=request.match_info["fact_id"],
                expected_updated_at=body["expected_updated_at"],
                statement=body.get("statement"),
                destination=destination,
            )
        )

    async def delete(self, request):
        self.query(request, set())
        body = await self.body(
            request,
            {"view", "workspace_id", "expected_updated_at"},
            {"view", "expected_updated_at"},
        )
        return web.json_response(
            await self.sessions.core.memory_management_delete(
                memory_domain_id=self.domain,
                selection=self.selection(body),
                fact_id=request.match_info["fact_id"],
                expected_updated_at=body["expected_updated_at"],
            )
        )
