"""Local UI avatar; Pillow owns image decoding and normalization."""

import asyncio
from io import BytesIO
import os
from pathlib import Path
import tempfile

from aiohttp import web

from pulsara_agent.capability.pulsara_home import require_pulsara_home


def normalize_avatar(payload: bytes) -> bytes:
    from PIL import Image, ImageOps

    with Image.open(BytesIO(payload), formats=("PNG", "JPEG", "WEBP", "GIF")) as image:
        # Keep Pillow's default decompression boundary, including its warning tier.
        if image.width * image.height > Image.MAX_IMAGE_PIXELS:
            raise ValueError("image exceeds Pillow's pixel boundary")
        image.seek(0)
        image.thumbnail((256, 256), Image.Resampling.LANCZOS)
        corrected = ImageOps.exif_transpose(image)
        avatar = ImageOps.fit(
            corrected.convert("RGBA"), (256, 256), method=Image.Resampling.LANCZOS
        )
        # Strip source metadata. Animated inputs use their first frame.
        clean = Image.new("RGBA", avatar.size)
        clean.paste(avatar)
        output = BytesIO()
        clean.save(output, format="WEBP", quality=90)
        return output.getvalue()


def _normalize_avatar_upload(payload: bytes) -> bytes | None:
    # Image decoding is only needed for an upload. Keep both Pillow's import
    # and its existing HTTP error classification on the upload worker.
    from PIL import Image, UnidentifiedImageError

    try:
        return normalize_avatar(payload)
    except (UnidentifiedImageError, OSError, ValueError, Image.DecompressionBombError):
        return None


class UserAvatarHttp:
    def __init__(self) -> None:
        self.path = require_pulsara_home() / "avatar.webp"
        self._write_lock = asyncio.Lock()

    def install(self, router: web.UrlDispatcher) -> None:
        router.add_get("/api/user-avatar", self.status)
        router.add_get("/api/user-avatar/image", self.image)
        router.add_put("/api/user-avatar", self.upload)
        router.add_delete("/api/user-avatar", self.clear)

    async def status(self, _request: web.Request) -> web.Response:
        return web.json_response(
            {"configured": self.path.is_file()}, headers={"Cache-Control": "no-store"}
        )

    async def image(self, _request: web.Request) -> web.Response:
        try:
            payload = self.path.read_bytes()
        except FileNotFoundError:
            raise web.HTTPNotFound() from None
        return web.Response(
            body=payload,
            content_type="image/webp",
            headers={"Cache-Control": "no-store"},
        )

    async def upload(self, request: web.Request) -> web.Response:
        # request.read retains the local HTTP surface's existing 8 MiB boundary.
        payload = await request.read()
        async with self._write_lock:
            normalized = await asyncio.to_thread(_normalize_avatar_upload, payload)
            if normalized is None:
                return web.json_response(
                    {
                        "error": {
                            "code": "INVALID_AVATAR",
                            "message": "请选择有效的 PNG、JPEG、WebP 或 GIF 图片。",
                        }
                    },
                    status=400,
                )
            # Only publish after decoding finishes. Cancellation cannot leave a
            # background decoder writing over a later upload or reset.
            self.path.parent.mkdir(parents=True, exist_ok=True)
            temporary: Path | None = None
            try:
                with tempfile.NamedTemporaryFile(
                    dir=self.path.parent, prefix=".avatar-", delete=False
                ) as stream:
                    temporary = Path(stream.name)
                    stream.write(normalized)
                os.replace(temporary, self.path)
            finally:
                if temporary is not None:
                    temporary.unlink(missing_ok=True)
        return web.json_response({"configured": True})

    async def clear(self, _request: web.Request) -> web.Response:
        async with self._write_lock:
            self.path.unlink(missing_ok=True)
        return web.json_response({"configured": False})
