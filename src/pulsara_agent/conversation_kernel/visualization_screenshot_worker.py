"""Disposable browser process for a single self-contained HTML preview."""

from __future__ import annotations

import sys
from pathlib import Path
from io import BytesIO
from math import isfinite

from playwright.sync_api import sync_playwright


_VIEWPORT_WIDTH = 1200
_VIEWPORT_HEIGHT = 800


def _usable_root_box(box: dict[str, float] | None) -> bool:
    if box is None:
        return False
    x, y, width, height = (box[key] for key in ("x", "y", "width", "height"))
    return (
        all(isfinite(value) for value in (x, y, width, height))
        and x >= 0
        and y >= 0
        and width >= 1
        and height >= 1
        and x + width <= _VIEWPORT_WIDTH
        and y + height <= _VIEWPORT_HEIGHT
    )


def main() -> None:
    html = sys.stdin.buffer.read().decode("utf-8")
    bootstrap = (
        Path(__file__)
        .with_name("visualization_environment.js")
        .read_text()
        .removeprefix("export default ")
    )
    environment = "<script>(" + bootstrap + ")(null)</script>"
    shell = (
        Path(__file__)
        .with_name("visualization_shell.js")
        .read_text()
        .replace("export default ", "", 1)
    )
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        try:
            context = browser.new_context(
                viewport={"width": _VIEWPORT_WIDTH, "height": _VIEWPORT_HEIGHT},
                device_scale_factor=1,
                color_scheme="light",
                reduced_motion="reduce",
                service_workers="block",
                accept_downloads=False,
            )
            context.route("**/*", lambda route: route.abort())
            page = context.new_page()
            page.set_default_timeout(10_000)
            document = page.evaluate(
                "args => {" + shell + "; return visualizationShell(args); }",
                {"html": html, "bootstrap": environment},
            )
            page.set_content(document, wait_until="load", timeout=10_000)
            frame = page.locator("#content").element_handle().content_frame()
            if frame is None or frame.url != "about:srcdoc":
                raise RuntimeError("HTML preview document is unavailable")
            frame.wait_for_load_state("load", timeout=10_000)
            frame.evaluate(
                "() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)))"
            )
            selector = Path(__file__).with_name("visualization_root.js").read_text()
            # The shared selector includes comments before the export.
            selector = selector.replace("export default ", "", 1)
            root_handle = frame.evaluate_handle(
                "() => {" + selector + "; return visualizationRoot(); }"
            )
            root = root_handle.as_element()
            if root is not None and _usable_root_box(root.bounding_box()):
                screenshot = root.screenshot(type="png", timeout=10_000)
            else:
                screenshot = page.screenshot(
                    type="png", full_page=False, timeout=10_000
                )
            root_handle.dispose()
            if "--thumbnail" in sys.argv:
                from PIL import Image

                image = Image.open(BytesIO(screenshot))
                # High-density navigation pixels, not a content size limit.
                image.thumbnail((320, 214), Image.Resampling.LANCZOS)
                output = BytesIO()
                image.save(output, format="PNG")
                screenshot = output.getvalue()
            sys.stdout.buffer.write(screenshot)
        finally:
            browser.close()


if __name__ == "__main__":
    main()
