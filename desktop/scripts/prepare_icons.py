"""Render the desktop icon plate without changing the in-app transparent logo.

Run with the repository's .venv/bin/python. Chromium renders the SVG composition
with transparent outer corners; macOS iconutil owns the ICNS container.
"""

from __future__ import annotations

import base64
from pathlib import Path
import subprocess
import sys
import tempfile
from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[2]
ASSETS = ROOT / "desktop/assets"


def main() -> None:
    logo = base64.b64encode(
        (ROOT / "frontend/public/assets/pulsara-icon.png").read_bytes()
    ).decode("ascii")
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        try:
            _render(browser, logo)
        finally:
            browser.close()


def _render(browser, logo: str) -> None:
    page = browser.new_page(device_scale_factor=1)
    with tempfile.TemporaryDirectory(prefix="pulsara-icons-") as folder:
        iconset = Path(folder) / "Pulsara.iconset"
        iconset.mkdir()
        images: dict[int, bytes] = {}
        for size in (16, 32, 64, 128, 256, 512, 1024):
            source = (
                f'<svg xmlns="http://www.w3.org/2000/svg" width="{size}" '
                f'height="{size}" viewBox="0 0 1024 1024">'
                '<rect x="64" y="64" width="896" height="896" rx="196" fill="white"/>'
                f'<image x="144" y="144" width="736" height="736" '
                f'href="data:image/png;base64,{logo}"/></svg>'
            )
            page.set_viewport_size({"width": size, "height": size})
            page.set_content(
                f"<style>body{{margin:0}}svg{{display:block}}</style>{source}"
            )
            page.locator("image").evaluate(
                """async image => {
                      const pixels = new Image();
                      pixels.src = image.getAttribute('href');
                      await pixels.decode();
                    }"""
            )
            images[size] = page.screenshot(type="png", omit_background=True)
        (ASSETS / "pulsara.png").write_bytes(images[1024])
        if sys.platform == "darwin":
            for size in (16, 32, 128, 256, 512):
                for scale in (1, 2):
                    suffix = "@2x" if scale == 2 else ""
                    (iconset / f"icon_{size}x{size}{suffix}.png").write_bytes(
                        images[size * scale]
                    )
            subprocess.run(
                [
                    "iconutil",
                    "-c",
                    "icns",
                    str(iconset),
                    "-o",
                    str(ASSETS / "pulsara.icns"),
                ],
                check=True,
            )


if __name__ == "__main__":
    main()
