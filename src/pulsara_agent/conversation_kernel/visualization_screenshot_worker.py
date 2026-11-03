"""Disposable browser process for one isolated HTML or SVG preview."""

from __future__ import annotations

import sys
import signal
import traceback
import asyncio
import json
import re
from urllib.parse import urlsplit
from pathlib import Path
from io import BytesIO
from math import isfinite

from typing import TYPE_CHECKING
if TYPE_CHECKING:
    from playwright.async_api import Page
from pulsara_agent.llm.input import MAXIMUM_PROMPT_IMAGE_PIXELS


_VIEWPORT_WIDTH = 1200
_VIEWPORT_HEIGHT = 800
_physical_cleanup_complete = True
_stop_requested = False
_main_task = None


def _request_stop(_signum, _frame):
    global _stop_requested
    # Keep Playwright's driver alive so its supported browser.close() can use
    # its own closeOrKill deadline and reap its separately detached Chromium.
    _stop_requested = True
    if _main_task is not None and not _main_task.cancelling():
        _main_task.cancel()


async def _sdk_join(operation):
    # A cancelled launch waiter must not lose the SDK object whose physical
    # launch is still in flight. Its existing launch/close deadline stays with
    # the SDK; after it joins, the same object enters the normal finally lane.
    task = asyncio.create_task(operation)
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            continue
        except BaseException:
            break
    return task.result()


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


async def _render_svg(page: Page, source: str) -> bytes:
    # Chromium owns XML, SVG length units and image decoding. Only the detached
    # document is inspected; SVG executes nowhere, even if it contains scripts.
    dimensions = await page.evaluate(
        """({source, pixelLimit}) => {
          const doc = new DOMParser().parseFromString(source, 'image/svg+xml');
          const svg = doc.documentElement;
          if (doc.querySelector('parsererror') || svg.localName !== 'svg'
              || svg.namespaceURI !== 'http://www.w3.org/2000/svg')
            throw new Error('SVG_DOCUMENT_INVALID');
          const absolute = name => {
            const value = svg[name].baseVal;
            return svg.hasAttribute(name) && value.unitType !== 2 && value.value > 0
              ? value.value : undefined;
          };
          const box = svg.viewBox.baseVal;
          const ratio = box.width > 0 && box.height > 0 ? box.width / box.height : undefined;
          let width = absolute('width'), height = absolute('height');
          if (ratio) {
            if (width && !height) height = width / ratio;
            if (height && !width) width = height * ratio;
            if (!width && !height) { width = box.width; height = box.height; }
          }
          // The browser's standard replaced-element fallback for unsized SVG.
          width = Math.ceil(width || 300); height = Math.ceil(height || 150);
          if (!Number.isFinite(width) || !Number.isFinite(height)
              || width < 1 || height < 1 || width * height > pixelLimit)
            throw new Error('IMAGE_RESOURCE_EXCEEDED');
          svg.setAttribute('width', String(width));
          svg.setAttribute('height', String(height));
          return {width, height, source: new XMLSerializer().serializeToString(svg)};
        }""",
        {"source": source, "pixelLimit": MAXIMUM_PROMPT_IMAGE_PIXELS},
    )
    await page.set_viewport_size({"width": dimensions["width"], "height": dimensions["height"]})
    await page.set_content(
        """<!doctype html><meta http-equiv="Content-Security-Policy"
        content="default-src 'none'; img-src data:; style-src 'unsafe-inline'; base-uri 'none'">
        <style>html,body{margin:0;overflow:hidden}img{display:block}</style><img alt="">"""
    )
    await page.locator("img").evaluate(
        """async (img, source) => {
          img.src = 'data:image/svg+xml;charset=utf-8,' + encodeURIComponent(source);
          await img.decode();
        }""",
        dimensions["source"],
    )
    return await page.screenshot(type="png", timeout=10_000)


async def main() -> None:
    global _physical_cleanup_complete
    html = sys.stdin.buffer.read().decode("utf-8")
    if not html:
        raise ValueError("preview input is empty")
    if _stop_requested:
        raise SystemExit("preview stopped")
    resource_prefix = None
    if "--local-media" in sys.argv:
        value = json.loads(html)
        html, resource_prefix = value["html"], value["resource_prefix"]
        url = urlsplit(resource_prefix)
        if (url.scheme not in {"http", "https"} or url.hostname != "127.0.0.1"
                or url.username or url.password or url.query or url.fragment
                or not re.fullmatch(r"/api/file-previews/[A-Za-z0-9_-]+/resources/", url.path)):
            raise ValueError("invalid local screenshot resource prefix")
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
    from playwright.async_api import async_playwright
    _physical_cleanup_complete = False
    playwright = await _sdk_join(async_playwright().start())
    browser = None
    browser_closed = False
    try:
        if _stop_requested:
            raise asyncio.CancelledError
        browser = await _sdk_join(playwright.chromium.launch(headless=True))
        try:
            if _stop_requested:
                raise SystemExit("preview stopped")
            context = await browser.new_context(
                viewport={"width": _VIEWPORT_WIDTH, "height": _VIEWPORT_HEIGHT},
                device_scale_factor=1,
                color_scheme="light",
                reduced_motion="reduce",
                service_workers="block",
                accept_downloads=False,
            )
            # Screenshots may load only images from this exact temporary mount;
            # video bytes, scripts and all other network requests stay blocked.
            await context.route("**/*", lambda route: route.continue_() if resource_prefix and route.request.url.startswith(resource_prefix) and route.request.resource_type == "image" else route.abort())
            page = await context.new_page()
            page.set_default_timeout(10_000)
            if "--svg" in sys.argv:
                sys.stdout.buffer.write(await _render_svg(page, html))
                return
            document = await page.evaluate(
                "args => {" + shell + "; return visualizationShell(args); }",
                {"html": html + "<style>" + Path(__file__).with_name("visualization_scrollbars.css").read_text() + "</style>", "bootstrap": environment, "mediaPrefix": resource_prefix},
            )
            await page.set_content(document, wait_until="load", timeout=10_000)
            element = await page.locator("#content").element_handle()
            frame = await element.content_frame()
            if frame is None or frame.url != "about:srcdoc":
                raise RuntimeError("HTML preview document is unavailable")
            await frame.wait_for_load_state("load", timeout=10_000)
            await frame.evaluate(
                "() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)))"
            )
            selector = Path(__file__).with_name("visualization_root.js").read_text()
            # The shared selector includes comments before the export.
            selector = selector.replace("export default ", "", 1)
            root_handle = await frame.evaluate_handle(
                "() => {" + selector + "; return visualizationRoot(); }"
            )
            root = root_handle.as_element()
            if root is not None and _usable_root_box(await root.bounding_box()):
                screenshot = await root.screenshot(type="png", timeout=10_000)
            else:
                screenshot = await page.screenshot(
                    type="png", full_page=False, timeout=10_000
                )
            await root_handle.dispose()
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
            await _sdk_join(browser.close())
            browser_closed = True
    finally:
        await _sdk_join(playwright.stop())
        _physical_cleanup_complete = browser is None or browser_closed


async def _run():
    global _main_task
    loop = asyncio.get_running_loop()
    _main_task = asyncio.create_task(main())
    loop.add_signal_handler(signal.SIGTERM, _request_stop, None, None)
    if _stop_requested:
        _main_task.cancel()
    try:
        await _main_task
    finally:
        _main_task = None
        loop.remove_signal_handler(signal.SIGTERM)


if __name__ == "__main__":
    signal.signal(signal.SIGTERM, _request_stop)
    # No SDK process can be launched before the parent consumes this byte and
    # supplies the HTML. This closes the OS spawn/handler-installation race.
    sys.stdout.buffer.write(b"\x00")
    sys.stdout.buffer.flush()
    try:
        asyncio.run(_run())
    except BaseException:
        traceback.print_exc()
        # 1 means rendering failed after SDK/browser physical cleanup; 2 keeps
        # that ownership unconfirmed. Neither status fabricates a screenshot.
        raise SystemExit(1 if _physical_cleanup_complete else 2)
