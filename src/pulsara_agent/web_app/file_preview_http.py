"""aiohttp adaptation for user-owned, descriptor-bound file previews."""

from __future__ import annotations

import asyncio
import os
import json
from typing import Callable

from aiohttp import web
from aiohttp.helpers import content_disposition_header

from .file_preview import FilePreviewError, PAGE_BYTES

HEADERS = {
    "Cache-Control": "no-store",
    "Referrer-Policy": "no-referrer",
    "X-Content-Type-Options": "nosniff",
}
# Reports observations only. The host never interprets these as file/action requests.
OBSERVATION_SCRIPT = b"""<!doctype html><script>
addEventListener('error',()=>parent.postMessage({type:'pulsara-preview-error'},'*'),true);
addEventListener('unhandledrejection',()=>parent.postMessage({type:'pulsara-preview-error'},'*'));
addEventListener('securitypolicyviolation',()=>parent.postMessage({type:'pulsara-preview-error'},'*'));
</script>"""


async def blocking(function, *args):
    """Keep descriptor ownership until an already-started OS operation settles."""
    task = asyncio.create_task(asyncio.to_thread(function, *args))
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        try:
            await task
        finally:
            raise


def html_policy(resource_prefix: str) -> str:
    return (
        "sandbox allow-scripts; default-src 'none'; "
        f"script-src 'unsafe-inline' {resource_prefix}; style-src 'unsafe-inline' {resource_prefix}; "
        f"img-src data: {resource_prefix}; font-src data: {resource_prefix}; connect-src {resource_prefix}; "
        "worker-src 'none'; frame-src 'none'; media-src 'none'; object-src 'none'; base-uri 'none'; form-action 'none'"
    )


class FilePreviewHttp:
    def __init__(self, bridge, origin: Callable[[], str]):
        self.bridge, self.origin = bridge, origin

    def install(self, router):
        path = "/api/connections/{connection_id}/file-preview"
        router.add_post(path, self.open)
        router.add_delete(path, self.close)
        router.add_post(path + "/page", self.page)
        router.add_post(path + "/action", self.action)
        router.add_get("/api/file-previews/{token}/content", self.content)
        router.add_get(
            "/api/file-previews/{token}/resources/{relative:.*}", self.resource
        )
        router.add_get("/api/file-previews/{token}/images/{relative:.*}", self.image)

    async def body(self, request):
        if (
            request.headers.get("Origin") != self.origin()
            or request.headers.get("Sec-Fetch-Site") != "same-origin"
        ):
            raise FilePreviewError(
                "ORIGIN_REJECTED", "请求不是从当前 Pulsara 页面发出的。", 403
            )
        body = await request.json()
        if not isinstance(body, dict):
            raise ValueError("object required")
        return body

    async def current(self, request, body, *, check_content=True):
        token = body.get("read_token")
        if not isinstance(token, str):
            raise ValueError("read_token required")
        return await self.bridge.require_file_preview(
            request.match_info["connection_id"], token, check_content=check_content
        )

    async def open(self, request):
        body = await self.body(request)
        path, base = body.get("path"), body.get("base_preview")
        if not isinstance(path, str) or (
            base is not None and not isinstance(base, str)
        ):
            raise ValueError("path required")
        result = await self.bridge.open_file_preview(
            request.match_info["connection_id"], path, base
        )
        return web.json_response(result, headers=HEADERS)

    async def close(self, request):
        item = await self.current(
            request, await self.body(request), check_content=False
        )
        self.bridge.file_previews.revoke(item.connection_id)
        return web.json_response({"closed": True}, headers=HEADERS)

    async def page(self, request):
        body = await self.body(request)
        item = await self.current(request, body)
        cursor, mode = body.get("cursor", 0), body.get("mode", "text")
        if type(cursor) is not int or mode not in {"text", "table"}:
            raise ValueError("invalid page")
        try:
            async with item.page_lock:
                result = await blocking(item.page, cursor, mode)
            # Recheck after IO; a replaced view never supplies a stale success.
            await self.bridge.require_file_preview(item.connection_id, item.token)
            return web.json_response(
                result,
                headers=HEADERS,
                dumps=lambda v: json.dumps(
                    v, ensure_ascii=False, separators=(",", ":")
                ),
            )
        finally:
            if item.closed:
                item.close_handles()

    async def action(self, request):
        body = await self.body(request)
        item = await self.current(request, body)
        action = body.get("action")
        # action_path re-opens without following symlinks before path handoff to OS.
        path = item.action_path(action)
        args = (
            ["open", "-R", str(path)]
            if action == "reveal" and item.kind != "directory"
            else ["open", str(path)]
        )
        process = await asyncio.create_subprocess_exec(
            *args, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL
        )
        if await process.wait() != 0:
            raise FilePreviewError("FILE_UNREADABLE", "系统暂时无法打开这个位置。", 409)
        return web.json_response({"opened": True}, headers=HEADERS)

    async def content(self, request):
        item = await self.bridge.read_file_preview(request.match_info["token"])
        if item.fd is None:
            raise FilePreviewError(
                "FILE_PREVIEW_UNSUPPORTED", "此目标没有文件内容。", 415
            )
        attachment = request.query.get("download") == "1"
        mime = (
            item.mime
            if item.kind in {"image", "svg", "pdf"}
            else "text/plain"
            if item.kind in {"html", "text", "table", "markdown"}
            else "application/octet-stream"
        )
        headers = dict(HEADERS)
        if item.kind == "svg":
            # Native <img> decoding owns SVG rendering, without script execution.
            # Do not expose that same file as an active browser document.
            headers["Content-Security-Policy"] = (
                "sandbox; default-src 'none'; style-src 'unsafe-inline'; "
                "img-src data:; font-src data:; base-uri 'none'; form-action 'none'"
            )
            if not attachment and request.headers.get("Sec-Fetch-Dest") != "image":
                return web.Response(
                    status=403,
                    text="请在 Pulsara 文件预览中查看此 SVG。",
                    headers=headers,
                    content_type="text/plain",
                )
        if attachment or item.kind == "file":
            headers["Content-Disposition"] = content_disposition_header(
                "attachment", filename=item.path.name
            )
        return await self.stream(
            request, os.dup(item.fd), mime, headers, item=None if attachment else item
        )

    async def image(self, request):
        return await self.resource(request, images=True)

    async def resource(self, request, images=False):
        item = await self.bridge.read_file_preview(request.match_info["token"])
        fd, mime = item.resource(request.match_info["relative"], images=images)
        headers = dict(HEADERS)
        if request.headers.get("Origin") == "null":
            headers.update({"Access-Control-Allow-Origin": "null", "Vary": "Origin"})
        prefix = self.origin() + "/api/file-previews/" + item.token + "/resources/"
        if mime == "text/html":
            headers["Content-Security-Policy"] = html_policy(prefix)
            # An untrusted document opened as a top-level tab has no trusted
            # parent CSP to constrain self-navigation. Serve it only in a frame.
            if request.headers.get("Sec-Fetch-Dest") != "iframe":
                os.close(fd)
                return web.Response(
                    status=403,
                    text="请在 Pulsara 文件预览中打开此页面。",
                    headers=headers,
                    content_type="text/plain",
                )
        return await self.stream(
            request, fd, mime, headers, html_document=mime == "text/html", item=item
        )

    async def stream(
        self, request, fd, mime, headers, *, html_document=False, item=None
    ):
        try:
            size = os.fstat(fd).st_size
            start, stop, status = 0, size, 200
            prefix = OBSERVATION_SCRIPT if html_document else b""
            headers["Content-Type"] = mime + (
                "; charset=utf-8" if mime.startswith("text/") else ""
            )
            # Executable HTML has a trusted observation prefix; ranges apply only
            # to the unmodified binary/download representation.
            if not html_document:
                headers["Accept-Ranges"] = "bytes"
                if request.headers.get("Range"):
                    try:
                        byte_range = request.http_range
                        start = byte_range.start or 0
                        stop = byte_range.stop if byte_range.stop is not None else size
                        if start < 0:
                            start, stop = max(0, size + start), size
                        stop = min(stop, size)
                        if start >= size or stop <= start:
                            raise ValueError("unsatisfied")
                    except ValueError:
                        headers["Content-Range"] = f"bytes */{size}"
                        return web.Response(status=416, headers=headers)
                    status = 206
                    headers["Content-Range"] = f"bytes {start}-{stop - 1}/{size}"
            headers["Content-Length"] = str(stop - start + len(prefix))
            response = web.StreamResponse(status=status, headers=headers)
            await response.prepare(request)
            if request.method != "HEAD":
                if prefix:
                    await response.write(prefix)
                while start < stop:
                    if item is not None and item.closed:
                        if request.transport:
                            request.transport.close()
                        return response
                    chunk = await blocking(
                        os.pread, fd, min(PAGE_BYTES, stop - start), start
                    )
                    if not chunk or (item is not None and item.closed):
                        # Can't replace an already-started response with JSON.
                        if request.transport:
                            request.transport.close()
                        return response
                    await response.write(chunk)
                    start += len(chunk)
            await response.write_eof()
            return response
        finally:
            os.close(fd)
