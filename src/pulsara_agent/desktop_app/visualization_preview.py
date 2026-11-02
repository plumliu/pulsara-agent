"""Disposable thumbnails of authorized canonical HTML; no new stored artifacts."""

from __future__ import annotations

import asyncio
import base64
from hashlib import sha256
from time import monotonic
from collections.abc import Callable

from pulsara_agent.conversation_kernel.visualization import VISUALIZATION_HTML_SNAPSHOT_MEDIA_TYPE, html_snapshot_content
from pulsara_agent.desktop_app.file_preview_http import bind_html_snapshot

from pulsara_agent.conversation_kernel.visualization_screenshot import (
    VisualizationScreenshotOwner,
)


class VisualizationPreviews:
    def __init__(self, bridge, origin: Callable[[], str] | None = None):
        self.bridge = bridge
        self.origin = origin
        self.screenshots = VisualizationScreenshotOwner()
        # One disposable browser at a time for UI thumbnails. Other requests wait;
        # this is a physical execution bound, never a bound on the gallery size.
        self.slot = asyncio.Lock()

    async def read(self, connection_id: str, body: dict, *, cancelled: Callable[[], bool] = lambda: False) -> dict:
        return await self.read_canonical(body, read_content=lambda value: self.bridge.read_content(connection_id, value), cancelled=cancelled, connection_id=connection_id)

    async def read_canonical(self, body: dict, *, read_content, connection_id: str | None = None, cancelled: Callable[[], bool] = lambda: False) -> dict:
        entry, ordinal = body.get("entry_id"), body.get("ordinal")
        digest, size = body.get("digest"), body.get("size")
        if (not isinstance(entry, str) or not entry or type(ordinal) is not int
                or ordinal < 0 or not isinstance(digest, str)
                or type(size) is not int or size < 1):
            raise ValueError("invalid visualization reference")
        target = {"entry_id": entry, "visualization_ordinal": ordinal}
        async with self.slot:
            if cancelled():
                raise asyncio.CancelledError
            content = bytearray()
            media_type = None
            while True:
                result = await read_content({
                    **target, "offset_bytes": len(content), "limit_bytes": 1 << 20,
                })
                if cancelled():
                    raise asyncio.CancelledError
                chunk = result.get("content")
                if not isinstance(chunk, dict):
                    raise ValueError("visualization owner unavailable")
                if chunk.get("media_type") not in {"text/html", VISUALIZATION_HTML_SNAPSHOT_MEDIA_TYPE} or chunk.get("codec") != "utf-8":
                    raise ValueError("thumbnail screenshot requires HTML content")
                if media_type is not None and chunk.get("media_type") != media_type:
                    raise ValueError("visualization media changed")
                media_type = chunk.get("media_type")
                if (chunk.get("digest") != digest
                        or int(chunk.get("complete_size", 0)) != size
                        or int(chunk.get("offset_bytes", 0)) != len(content)):
                    raise ValueError("visualization content identity changed")
                data = base64.b64decode(chunk.get("content", ""), validate=True)
                content.extend(data)
                if len(content) > size:
                    raise ValueError("visualization content size changed")
                if chunk.get("complete"):
                    break
                if not data or len(content) >= size:
                    raise ValueError("visualization content is incomplete")
            if len(content) != size or "sha256:" + sha256(content).hexdigest() != digest:
                raise ValueError("visualization content integrity failed")
            snapshot = None
            try:
                if media_type == VISUALIZATION_HTML_SNAPSHOT_MEDIA_TYPE:
                    if connection_id is None or self.origin is None:
                        raise ValueError("local HTML media requires a live file connection")
                    html, path = html_snapshot_content(bytes(content))
                    snapshot = await bind_html_snapshot(self.bridge, self.origin(), connection_id, str(path), html.decode("utf-8"))
                    image = await self.screenshots.render(
                        snapshot["html"].encode("utf-8"), deadline_monotonic=monotonic() + 30,
                        thumbnail=True, resource_prefix=snapshot["resource_prefix"],
                    )
                else:
                    image = await self.screenshots.render(
                        bytes(content), deadline_monotonic=monotonic() + 30, thumbnail=True,
                    )
            finally:
                if snapshot is not None:
                    self.bridge.file_previews.revoke(connection_id, snapshot["read_token"])
            # Revalidate the attached owner after the disposable browser settles.
            result = await read_content({
                **target, "offset_bytes": 0, "limit_bytes": 1,
            })
            if result.get("content", {}).get("digest") != digest:
                raise ValueError("visualization owner changed")
            return {"image": "data:image/png;base64," + base64.b64encode(image).decode("ascii")}

    async def aclose(self):
        await self.screenshots.aclose()
