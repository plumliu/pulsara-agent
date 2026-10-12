import asyncio
from io import BytesIO
from typing import cast

from aiohttp import ClientSession
from PIL import Image, PngImagePlugin
import pytest

from pulsara_agent.web_app.browser_bridge import LocalBrowserBridge
from pulsara_agent.web_app.http_server import LocalHttpServer
from pulsara_agent.web_app.session_controller import LocalSessionController
from pulsara_agent.web_app.user_avatar import UserAvatarHttp, normalize_avatar
from tests.test_local_web_http_surface import (
    _Bridge,
    _Sessions,
    _model_server_dependencies,
)


def picture(color="red", format="PNG"):
    output = BytesIO()
    Image.new("RGB", (600, 300), color).save(output, format=format)
    return output.getvalue()


@pytest.mark.parametrize("format", ["PNG", "JPEG", "WEBP", "GIF"])
def test_avatar_normalization(format):
    with Image.open(BytesIO(normalize_avatar(picture(format=format)))) as image:
        assert image.format == "WEBP"
        assert image.size == (256, 256)
        assert image.n_frames == 1
        assert not image.getexif()


def test_avatar_strips_metadata_and_uses_center_crop():
    source = Image.new("RGBA", (600, 300), "red")
    source.paste("blue", (150, 0, 450, 300))
    info = PngImagePlugin.PngInfo()
    info.add_text("description", "private metadata")
    output = BytesIO()
    source.save(output, format="PNG", pnginfo=info)
    with Image.open(BytesIO(normalize_avatar(output.getvalue()))) as image:
        assert "description" not in image.info
        assert image.getpixel((128, 128))[2] > 240
        assert image.getpixel((10, 128))[2] > 240


def test_avatar_applies_exif_orientation_and_uses_first_animation_frame():
    source = Image.new("RGB", (300, 300), "red")
    source.paste("blue", (0, 0, 300, 150))
    exif = Image.Exif()
    exif[274] = 6
    output = BytesIO()
    source.save(output, format="JPEG", exif=exif)
    with Image.open(BytesIO(normalize_avatar(output.getvalue()))) as image:
        assert image.getpixel((230, 128))[2] > 240
        assert image.getpixel((25, 128))[0] > 240
        assert not image.getexif()
    output = BytesIO()
    Image.new("RGB", (30, 30), "red").save(
        output,
        format="GIF",
        save_all=True,
        append_images=[Image.new("RGB", (30, 30), "blue")],
        duration=100,
        loop=0,
    )
    with Image.open(BytesIO(normalize_avatar(output.getvalue()))) as image:
        assert image.n_frames == 1
        assert image.getpixel((128, 128))[0] > 240


def test_avatar_http_custom_home_replacement_reset_and_security(tmp_path, monkeypatch):
    home = tmp_path / "custom-home"
    monkeypatch.setenv("PULSARA_HOME", str(home))

    async def exercise():
        (tmp_path / "index.html").write_text("Pulsara")
        server = LocalHttpServer(
            sessions=cast(LocalSessionController, _Sessions()),
            bridge=cast(LocalBrowserBridge, _Bridge()),
            static_root=tmp_path,
            requested_port=0,
            is_ready=lambda: True,
            is_draining=lambda: False,
            **_model_server_dependencies(),
        )
        await server.start()
        try:
            async with ClientSession() as client:
                url = server.origin + "/api/user-avatar"
                async with client.get(url) as response:
                    assert await response.json() == {"configured": False}
                async with client.get(url + "/image") as response:
                    assert response.status == 404
                async with client.put(
                    url, data=picture(), headers={"Origin": "https://other.test"}
                ) as response:
                    assert response.status == 403
                assert not (home / "avatar.webp").exists()
                async with client.put(
                    url, data=picture(), headers={"Origin": server.origin}
                ) as response:
                    assert response.status == 200
                    assert await response.json() == {"configured": True}
                path = home / "avatar.webp"
                first = path.read_bytes()
                assert path.stat().st_mode & 0o777 == 0o600
                async with client.put(
                    url, data=b"<svg>not an avatar</svg>"
                ) as response:
                    assert response.status == 400
                    assert (await response.json())["error"]["code"] == "INVALID_AVATAR"
                assert path.read_bytes() == first
                # Reuse the existing HTTP per-request 8 MiB body boundary.
                async with client.put(url, data=b"x" * ((8 << 20) + 1)) as response:
                    assert response.status == 413
                assert path.read_bytes() == first
                async with client.put(url, data=picture("blue")) as response:
                    assert response.status == 200
                assert path.read_bytes() != first
                async with client.get(url + "/image") as response:
                    assert response.content_type == "image/webp"
                    assert response.headers["Cache-Control"] == "no-store"
                    assert await response.read() == path.read_bytes()
                # File state survives recreation, without an in-memory authority.
                assert UserAvatarHttp().path == path
                async with client.get(url) as response:
                    assert await response.json() == {"configured": True}
                for _ in range(2):
                    async with client.delete(url) as response:
                        assert await response.json() == {"configured": False}
                assert not path.exists()
                assert not list(home.glob(".avatar-*"))
                assert not (home / "local-settings.yaml").exists()
        finally:
            await server.aclose()

    asyncio.run(exercise())
