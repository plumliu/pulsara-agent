"""Server-owned memory context and one-request confirmation stream ownership."""

from __future__ import annotations

import asyncio
import errno
import json
from tempfile import TemporaryFile

from aiohttp import web

from pulsara_agent.conversation_kernel.memory.contracts import canonical_json_bytes
from pulsara_agent.conversation_kernel.memory.management import (
    MemoryManagementError,
    MemoryManagementSelection,
    validate_product_record,
)


# A relation record carries two 8192-byte statements, a 2048-byte public summary,
# canonical IDs/context/timestamps/enums and JSON syntax. Six bytes per source
# byte covers JSON escaping. This bounds ONE record, never a graph or upload.
MAXIMUM_RECORD_BYTES = 6 * (2 * 8192 + 2048 + 4096)
STREAM_IDLE_SECONDS = 30  # loopback transport idle watchdog, not a total upload cap


class MemoryConfirmationUpload:
    """Unique temporary-file owner; no database lease exists while uploading."""

    def __init__(self):
        try:
            self.file = TemporaryFile(mode="w+b")
        except OSError as exc:
            if exc.errno in {errno.ENOSPC, errno.EDQUOT}:
                raise MemoryManagementError(
                    "MEMORY_CONFIRMATION_STORAGE_EXHAUSTED",
                    507,
                    "临时存储空间不足，尚未删除",
                ) from exc
            raise
        self.header = None
        self.additional = []

    def close(self):
        self.file.close()

    def records(self):
        self.file.seek(0)
        for line in self.file:
            yield line.rstrip(b"\n")

    async def receive(self, request, *, root, preview):
        if request.content_type != "application/x-ndjson":
            raise ValueError("记忆删除需要逐条确认记录")
        pending = bytearray()
        counts = {}
        ended = False
        last_order = -1
        order = {
            name: i
            for i, name in enumerate(
                (
                    "HEADER",
                    "ADDITIONAL_ROOT",
                    "FACT_DELETE",
                    "RELATION_EFFECT",
                    "FACT_RESTORE",
                    "RESTORATION_CONFLICT",
                    "END",
                )
            )
        }
        while True:
            async with asyncio.timeout(STREAM_IDLE_SECONDS):
                chunk = await request.content.read(65536)
            if not chunk:
                break
            pending.extend(chunk)
            while b"\n" in pending:
                line, _, rest = pending.partition(b"\n")
                pending = bytearray(rest)
                if ended or not line or len(line) > MAXIMUM_RECORD_BYTES:
                    raise ValueError("删除确认记录不完整或超出单条边界")
                record = validate_product_record(json.loads(line))
                kind = record["type"]
                if order[kind] < last_order:
                    raise ValueError("删除确认记录顺序无效")
                last_order = order[kind]
                if kind == "HEADER":
                    if self.header is not None or record["root"] != root:
                        raise ValueError("删除确认目标不匹配")
                    expected = {"type", "root", "view", "workspace_id"} | (
                        set() if preview else {"disposition"}
                    )
                    if set(record) != expected or (
                        not preview and record["disposition"] != "READY"
                    ):
                        raise ValueError("请先完整查看并解决删除预览")
                    self.header = record
                elif self.header is None:
                    raise ValueError("缺少删除确认头")
                elif kind == "ADDITIONAL_ROOT":
                    fact = record["fact_id"]
                    if fact == root or (
                        self.additional and fact <= self.additional[-1]
                    ):
                        raise ValueError("一并删除列表必须唯一且有序")
                    self.additional.append(fact)
                elif kind == "END":
                    if record["counts"] != counts:
                        raise ValueError("删除确认记录计数不匹配")
                    ended = True
                elif preview:
                    raise ValueError("预览请求不能携带确认结果")
                counts[kind] = counts.get(kind, 0) + 1
                self.file.write(canonical_json_bytes(record) + b"\n")
            if len(pending) > MAXIMUM_RECORD_BYTES:
                raise ValueError("删除确认超出单条记录边界")
        if pending or not ended or self.header is None:
            raise ValueError("删除确认未完整传输，尚未删除")


async def stream_records(request, records, *, status=200):
    # The complete operation-local tuple is already materialized by the
    # repository, and its transaction is closed. No post-commit disk allocation.
    response = web.StreamResponse(
        status=status,
        headers={
            "Content-Type": "application/x-ndjson",
            "Cache-Control": "no-store",
        },
    )
    await response.prepare(request)
    for record in records:
        async with asyncio.timeout(STREAM_IDLE_SECONDS):
            await response.write(record + b"\n")
    await response.write_eof()
    return response


class LocalMemoryController:
    def __init__(self, sessions):
        self.sessions = sessions

    @property
    def domain(self):
        return self.sessions.workspace_input.memory_domain_id

    @staticmethod
    def query(request, allowed):
        if set(request.query) - set(allowed) or any(
            len(request.query.getall(k)) != 1 for k in request.query
        ):
            raise ValueError("记忆查询参数无效")
        return request.query

    @staticmethod
    def selection(query):
        return MemoryManagementSelection(
            query.get("view", "global"), query.get("workspace_id")
        )

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
            request,
            {"view", "workspace_id", "lifecycle", "kind", "search", "limit", "cursor"},
        )
        return web.json_response(
            await self.sessions.core.memory_management_catalog(
                memory_domain_id=self.domain,
                selection=self.selection(q),
                lifecycle=q.get("lifecycle", "active"),
                kind=q.get("kind"),
                search=q.get("search"),
                limit=int(q.get("limit", 40)),
                cursor=q.get("cursor"),
            )
        )

    async def detail(self, request):
        q = self.query(request, {"view", "workspace_id", "limit", "cursor"})
        return web.json_response(
            await self.sessions.core.memory_management_detail(
                memory_domain_id=self.domain,
                selection=self.selection(q),
                fact_id=request.match_info["fact_id"],
                limit=int(q.get("limit", 40)),
                cursor=q.get("cursor"),
            )
        )

    async def edit_statement(self, request):
        self.query(request, set())
        if request.content_type != "application/json":
            raise ValueError("记忆编辑需要 JSON 正文")
        # One statement is at most 8 KiB of UTF-8, with JSON escaping and
        # a small fixed envelope. Never read an unbounded request body.
        raw = await request.content.read(6 * 8192 + 4096 + 1)
        if len(raw) > 6 * 8192 + 4096 or not request.content.at_eof():
            raise ValueError("记忆编辑请求超出单条正文边界")
        body = json.loads(raw)
        if not isinstance(body, dict) or set(body) != {
            "view", "workspace_id", "statement", "expected_updated_at"
        }:
            raise ValueError("记忆编辑参数无效")
        if not isinstance(body["view"], str) or not (
            body["workspace_id"] is None or isinstance(body["workspace_id"], str)
        ):
            raise ValueError("记忆范围无效")
        return web.json_response(
            await self.sessions.core.memory_management_edit_statement(
                memory_domain_id=self.domain,
                selection=MemoryManagementSelection(
                    body["view"], body["workspace_id"]
                ),
                fact_id=request.match_info["fact_id"],
                statement=body["statement"],
                expected_updated_at=body["expected_updated_at"],
            )
        )

    async def deletion(self, request):
        self.query(request, set())
        root = request.match_info["fact_id"]
        preview = request.method == "POST"
        upload = MemoryConfirmationUpload()
        try:
            try:
                await upload.receive(request, root=root, preview=preview)
            except TimeoutError as exc:
                raise MemoryManagementError(
                    "MEMORY_CONFIRMATION_UPLOAD_TIMEOUT",
                    408,
                    "确认传输中断，尚未删除，请重新预览",
                ) from exc
            except OSError as exc:
                if exc.errno in {errno.ENOSPC, errno.EDQUOT}:
                    raise MemoryManagementError(
                        "MEMORY_CONFIRMATION_STORAGE_EXHAUSTED",
                        507,
                        "临时存储空间不足，尚未删除",
                    ) from exc
                raise
            header = upload.header
            args = dict(
                memory_domain_id=self.domain,
                selection=MemoryManagementSelection(
                    header["view"], header["workspace_id"]
                ),
                fact_id=root,
                additional=tuple(upload.additional),
            )
            if preview:
                result = await self.sessions.core.memory_deletion_preview(**args)
            else:
                result = await self.sessions.core.execute_memory_deletion(
                    **args, expected_records=upload.records
                )
            return await stream_records(request, result)
        except MemoryManagementError as exc:
            if exc.preview is not None:
                return await stream_records(request, exc.preview, status=409)
            raise
        finally:
            upload.close()
