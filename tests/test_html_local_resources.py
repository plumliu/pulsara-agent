"""Contract tests for local resources frozen into one immutable HTML value."""

import asyncio
import base64
from io import BytesIO
from pathlib import Path
from time import monotonic
from unittest.mock import AsyncMock

import html5lib
from PIL import Image
from playwright.sync_api import sync_playwright
import pytest

from pulsara_agent.conversation_kernel.html_resources import freeze_html_resources
from pulsara_agent.conversation_kernel.image_validation import HostPromptImageValidator
from pulsara_agent.conversation_kernel.io import KernelSessionIO
from pulsara_agent.conversation_kernel.visualization import materialize_visualizations
from pulsara_agent.conversation_kernel.visualization_screenshot import (
    VisualizationScreenshotOwner,
)
from pulsara_agent.identity import random_id
from pulsara_agent.tools.builtins.filesystem import ViewImageTool


def png(color="orange"):
    output = BytesIO()
    Image.new("RGB", (8, 8), color).save(output, format="PNG")
    return output.getvalue()


async def prepare(path, body=None):
    io = KernelSessionIO()
    validator = HostPromptImageValidator()
    try:
        return await freeze_html_resources(
            path.read_bytes() if body is None else body,
            path,
            io=io,
            validator=validator,
            deadline_monotonic=monotonic() + 20,
        )
    finally:
        await validator.aclose()
        await io.aclose(deadline_monotonic=monotonic() + 20)


def elements(body, tag):
    return html5lib.parse(
        body.decode(), treebuilder="dom", scripting=True
    ).getElementsByTagName(tag)


def decode(value):
    return base64.b64decode(value.split(",", 1)[1])


def shell(page, html):
    source = (
        (
            Path(__file__).parents[1]
            / "src/pulsara_agent/conversation_kernel/visualization_shell.js"
        )
        .read_text()
        .replace("export default ", "", 1)
    )
    return page.evaluate(
        "html => {" + source + "; return visualizationShell({html});}", html
    )


def test_classic_files_freeze_and_browser_interaction_survive_source_deletion(tmp_path):
    (tmp_path / "css").mkdir()
    (tmp_path / "js").mkdir()
    (tmp_path / "images").mkdir()
    (tmp_path / "images/logo.png").write_bytes(png())
    (tmp_path / "css/main.css").write_text(
        "main{width:200px;height:100px;background-image:url('../images/logo.png')} button{color:rgb(1,2,3)}"
    )
    (tmp_path / "js/first.js").write_text(
        "window.order=['first']; window.literal='</script>'; "
    )
    (tmp_path / "js/defer.js").write_text(
        "order.push(document.querySelector('button')?'defer':'too-early'); document.querySelector('button').onclick=()=>document.body.dataset.clicked='yes';"
    )
    (tmp_path / "js/defer2.js").write_text("order.push('defer2')")
    (tmp_path / "js/async.js").write_text("window.asyncDone=true")
    path = tmp_path / "index.html"
    path.write_text("""<!doctype html><html><head>
<link rel="stylesheet" href="css/main.css" media="screen">
<script src="js/first.js"></script><script>order.push('inline')</script>
<script defer src="js/defer.js"></script><script defer src="js/defer2.js"></script>
<script async src="js/async.js"></script></head>
<body><main><img src="images/logo.png"><button>Click</button></main></body></html>""")
    frozen = asyncio.run(prepare(path))
    assert elements(frozen, "link")[0].getAttribute("media") == "screen"
    assert b"data:image/png;base64," in decode(
        elements(frozen, "link")[0].getAttribute("href")
    )
    assert decode(elements(frozen, "img")[0].getAttribute("src")) == png()
    for file in tmp_path.rglob("*"):
        if file.is_file():
            file.unlink()
    with sync_playwright() as p:
        browser = p.chromium.launch()
        try:
            page = browser.new_page()
            page.set_content(shell(page, frozen.decode()), wait_until="load")
            frame = page.locator("#content").element_handle().content_frame()
            assert frame.evaluate("order") == ["first", "inline", "defer", "defer2"]
            assert frame.evaluate("asyncDone") is True
            assert frame.evaluate("literal") == "</script>"
            assert frame.locator("img").evaluate("el=>el.naturalWidth") == 8
            assert (
                frame.locator("button").evaluate("el=>getComputedStyle(el).color")
                == "rgb(1, 2, 3)"
            )
            frame.locator("button").click()
            assert frame.locator("body").get_attribute("data-clicked") == "yes"
        finally:
            browser.close()


def test_declaring_file_bases_symlink_aliases_and_decoding(tmp_path):
    shared = tmp_path / "shared"
    shared.mkdir()
    (shared / "index.html").write_text(
        '<link rel="stylesheet" href="style.css"><img src="图%20%23%3F.png">'
    )
    (shared / "style.css").write_text("main{background:url('./image.png')}")
    for directory, color in [("a", "red"), ("b", "blue")]:
        folder = tmp_path / directory
        folder.mkdir()
        (folder / "index.html").symlink_to(shared / "index.html")
        (folder / "style.css").symlink_to(shared / "style.css")
        (folder / "image.png").write_bytes(png(color))
        (folder / "图 #?.png").write_bytes(png(color))
        frozen = asyncio.run(prepare(folder / "index.html"))
        assert decode(elements(frozen, "img")[0].getAttribute("src")) == png(color)
        css = decode(elements(frozen, "link")[0].getAttribute("href"))
        assert base64.b64encode(png(color)) in css
    path = tmp_path / "paths.html"
    (tmp_path / "100%.png").write_bytes(png())
    (tmp_path / "~").mkdir()
    (tmp_path / "~/image.png").write_bytes(png())
    (tmp_path / "image:literal.png").write_bytes(png())
    path.write_text(
        f'<img src="{tmp_path}/100%25.png"><img src="%7E/image.png"><img src="image%3Aliteral.png">'
    )
    frozen = asyncio.run(prepare(path))
    assert all(decode(x.getAttribute("src")) == png() for x in elements(frozen, "img"))


@pytest.mark.parametrize(
    "content,code",
    [
        ('<img src="missing.png">', "RESOURCE_READ_FAILED"),
        ('<link rel="stylesheet" href="missing.css">', "CSS_NOT_FOUND"),
        ('<script src="missing.js"></script>', "JS_NOT_FOUND"),
        ('<script src=""></script>', "ADDRESS_UNSUPPORTED"),
        ('<link rel="stylesheet">', "ADDRESS_UNSUPPORTED"),
        ('<img src="//host/image.png">', "ADDRESS_UNSUPPORTED"),
        ('<img src="file:/image.png">', "ADDRESS_UNSUPPORTED"),
        ('<img src="https://host/image.png">', "ADDRESS_UNSUPPORTED"),
        ('<img src="image.png?x=1">', "ADDRESS_UNSUPPORTED"),
        ('<img src="image.png#x">', "ADDRESS_UNSUPPORTED"),
        ('<img src="bad%zz.png">', "ADDRESS_UNSUPPORTED"),
        ('<img src="bad%ff.png">', "ADDRESS_INVALID"),
        ('<img src="bad%00.png">', "ADDRESS_INVALID"),
        ('<script type="module" src="file.js"></script>', "MODULE_UNSUPPORTED"),
        ('<style>@import "file.css";</style>', "IMPORT_UNSUPPORTED"),
        ("<style>@media screen{@import url(file.css);}</style>", "IMPORT_UNSUPPORTED"),
        (
            "<style>@font-face{font-family:Test;src:url(font.woff2)}</style>",
            "FONT_UNSUPPORTED",
        ),
        ('<script integrity="abc" src="app.js"></script>', "INTEGRITY_UNSUPPORTED"),
    ],
)
def test_supported_slots_fail_explicitly(tmp_path, content, code):
    with pytest.raises(ValueError, match=code):
        asyncio.run(prepare(tmp_path / "index.html", content.encode()))


def test_data_unhandled_syntax_and_text_do_not_trigger_file_reads(tmp_path):
    body = b"""<!doctype html><!-- <img src="missing.png"> -->
<textarea><img src="missing.png"></textarea><noscript><img src="missing.png"></noscript>
<script type="application/json" src="data.json">{"url":"missing.png"}</script>
<script>const x='url(missing.png)'; const y='<img src="missing.png">';</script>
<img srcset="data:image/png;base64,abcd 1x"><picture><source srcset="other.png"></picture>
<svg><use href="icons.svg#x"/></svg><a href="missing.html">link</a>
<style>@import 'data:text/css,body{}'; @font-face{font-family:Test;src:url(data:font/woff2;base64,AA==),local(Test)}
main{background-image:image-set("unhandled.png" 1x);filter:url(#filter)}</style>
<img src="data:image/png;base64,AA==">"""
    assert asyncio.run(prepare(tmp_path / "index.html", body)) == body


def test_css_raw_text_and_attributes_remain_safe_after_rewrite(tmp_path):
    (tmp_path / "image.png").write_bytes(png())
    body = rb"""<style>main::before{content:"\3c /style>"} @media screen{main{background:url(image.png)}}</style>
<main style="background-image:image-set(url('image.png') 1x);--label:&quot;A&amp;B&quot;">OK</main>
<noscript><img src="missing.png"></noscript>"""
    frozen = asyncio.run(prepare(tmp_path / "index.html", body))
    assert len(elements(frozen, "style")) == 1
    assert b"data:image/png" in frozen
    with sync_playwright() as p:
        browser = p.chromium.launch()
        try:
            page = browser.new_page()
            page.set_content(shell(page, frozen.decode()), wait_until="load")
            frame = page.locator("#content").element_handle().content_frame()
            assert (
                frame.locator("main").evaluate(
                    "el=>getComputedStyle(el,'::before').content"
                )
                == '"</style>"'
            )
            assert (
                frame.locator("main")
                .evaluate("el=>getComputedStyle(el).getPropertyValue('--label')")
                .strip()
                == '"A&B"'
            )
            assert frame.locator("img").count() == 0
        finally:
            browser.close()


def test_empty_css_js_allowed_bad_utf8_and_images_fail(tmp_path):
    for name in ["app.js", "app.css"]:
        (tmp_path / name).write_bytes(b"")
    body = b'<link rel="stylesheet" href="app.css"><script src="app.js"></script>'
    frozen = asyncio.run(prepare(tmp_path / "index.html", body))
    assert decode(elements(frozen, "script")[0].getAttribute("src")) == b""
    (tmp_path / "app.js").write_bytes(b"\xff")
    with pytest.raises(ValueError, match="JS_NOT_UTF8"):
        asyncio.run(prepare(tmp_path / "index.html", body))
    (tmp_path / "broken.png").write_bytes(b"not an image")
    with pytest.raises(ValueError):
        asyncio.run(prepare(tmp_path / "index.html", b'<img src="broken.png">'))


def test_final_budget_includes_duplicate_and_nested_base64(tmp_path, monkeypatch):
    import pulsara_agent.conversation_kernel.html_resources as resources

    (tmp_path / "image.png").write_bytes(png())
    (tmp_path / "app.css").write_text("main{background:url(image.png)}")
    monkeypatch.setattr(resources, "MAXIMUM_BLOB_BYTES", 350)
    with pytest.raises(ValueError, match="TOO_LARGE"):
        asyncio.run(
            prepare(
                tmp_path / "index.html",
                b'<link rel="stylesheet" href="app.css"><link rel="stylesheet" href="app.css">',
            )
        )


def test_publication_isolates_failed_items_and_cache_bases(tmp_path):
    async def run():
        io = KernelSessionIO()
        validator = HostPromptImageValidator()
        tool = ViewImageTool(tmp_path)
        try:
            shared = tmp_path / "shared.html"
            shared.write_text('<img src="image.png">')
            for directory, color in [("a", "red"), ("b", "blue")]:
                folder = tmp_path / directory
                folder.mkdir()
                (folder / "index.html").symlink_to(shared)
                (folder / "image.png").write_bytes(png(color))
            result = await materialize_visualizations(
                "```pulsara-visualization\na/index.html\nb/index.html\na/index.html\nshared.html\n```",
                tool._read_access_path,
                io=io,
                validator=validator,
                image_reader=None,
                session_id=random_id("session"),
                workspace_id=random_id("workspace"),
                deadline_monotonic=monotonic() + 20,
            )
            assert [x.state for x in result] == ["READY", "READY", "READY", "FAILED"]
            assert result[0].html == result[2].html != result[1].html
            return result[0].html
        finally:
            await validator.aclose()
            await io.aclose(deadline_monotonic=monotonic() + 20)

    assert asyncio.run(run())


def test_preparation_timeout_and_cancel_are_not_item_failures(tmp_path):
    async def run():
        io = KernelSessionIO()
        validator = AsyncMock()
        (tmp_path / "image.png").write_bytes(png())
        try:
            validator.freeze_local_image.side_effect = TimeoutError("watchdog")
            with pytest.raises(TimeoutError, match="watchdog"):
                await freeze_html_resources(
                    b'<img src="image.png">',
                    tmp_path / "index.html",
                    io=io,
                    validator=validator,
                    deadline_monotonic=monotonic() + 20,
                )
            entered = asyncio.Event()

            async def wait(*args, **kwargs):
                entered.set()
                await asyncio.Future()

            validator.freeze_local_image.side_effect = wait
            pending = asyncio.create_task(
                freeze_html_resources(
                    b'<img src="image.png">',
                    tmp_path / "index.html",
                    io=io,
                    validator=validator,
                    deadline_monotonic=monotonic() + 20,
                )
            )
            await entered.wait()
            pending.cancel()
            with pytest.raises(asyncio.CancelledError):
                await pending
            assert not io._active
        finally:
            await io.aclose(deadline_monotonic=monotonic() + 20)

    asyncio.run(run())


def test_screenshot_waits_for_data_script_and_uses_isolated_author_frame(tmp_path):
    # Async load must complete before root selection; parent DOM and popup
    # permissions must match the UI even in the disposable screenshot process.
    (tmp_path / "app.js").write_text("""
let isolated=false; try{parent.document.body.dataset.leaked='yes'}catch(e){isolated=true}
let popup=window.open('about:blank');
document.body.innerHTML='<main data-pulsara-visualization-root style="width:180px;height:90px;background:'+ (isolated && !popup?'rgb(255,0,0)':'rgb(0,0,255)')+'"></main>';
""")
    frozen = asyncio.run(
        prepare(
            tmp_path / "index.html",
            b'<body><script async src="app.js"></script></body>',
        )
    )

    async def run():
        owner = VisualizationScreenshotOwner()
        try:
            return await owner.render(frozen, deadline_monotonic=monotonic() + 20)
        finally:
            await owner.aclose()

    image = Image.open(BytesIO(asyncio.run(run())))
    assert image.size == (180, 90)
    assert image.getpixel((20, 20))[:3] == (255, 0, 0)


def test_shared_shell_blocks_navigation_network_and_host_access(tmp_path):
    (tmp_path / "app.js").write_text("""
window.results={};
try{parent.document.body.dataset.leaked='yes';results.parent=true}catch(e){results.parent=false}
try{top.location.href='https://example.invalid/top'}catch(e){}
results.popup=!!window.open('about:blank');
Promise.allSettled([fetch('https://example.invalid/network'),fetch('file:///etc/passwd')]).then(values=>{
 results.fetches=values.map(x=>x.status); results.done=true;
});
""")
    frozen = asyncio.run(
        prepare(
            tmp_path / "index.html",
            b'<img srcset="https://example.invalid/image.png"><script src="app.js"></script>',
        )
    )
    with sync_playwright() as p:
        browser = p.chromium.launch()
        try:
            context = browser.new_context()
            requests = []
            context.route(
                "**/*",
                lambda route: (requests.append(route.request.url), route.abort()),
            )
            page = context.new_page()
            page.set_content(shell(page, frozen.decode()), wait_until="load")
            frame = page.locator("#content").element_handle().content_frame()
            frame.wait_for_function("results.done")
            assert frame.evaluate("results") == {
                "parent": False,
                "popup": False,
                "fetches": ["rejected", "rejected"],
                "done": True,
            }
            assert frame.url == "about:srcdoc"
            assert page.url == "about:blank"
            assert page.locator("body").get_attribute("data-leaked") is None
            assert requests == []
            assert len(context.pages) == 1
            # CSP blocks the target but Chromium may replace the author
            # document with its error page. Isolation does not promise that
            # arbitrary author navigation preserves the current DOM.
            with page.expect_event("framenavigated", predicate=lambda f: f == frame):
                frame.evaluate("location.href='https://example.invalid/self'")
            assert frame.url == "chrome-error://chromewebdata/"
            assert requests == []
        finally:
            browser.close()


def test_cancelled_resource_read_drains_admitted_io(tmp_path, monkeypatch):
    import threading
    import pulsara_agent.conversation_kernel.html_resources as resources

    async def run():
        entered = threading.Event()
        release = threading.Event()
        original = resources._read

        def blocked(*args, **kwargs):
            entered.set()
            release.wait()
            return original(*args, **kwargs)

        monkeypatch.setattr(resources, "_read", blocked)
        (tmp_path / "app.js").write_text("window.loaded=true")
        io = KernelSessionIO()
        pending = asyncio.create_task(
            freeze_html_resources(
                b'<script src="app.js"></script>',
                tmp_path / "index.html",
                io=io,
                validator=None,
                deadline_monotonic=monotonic() + 20,
            )
        )
        try:
            await asyncio.to_thread(entered.wait)
            pending.cancel()
            await asyncio.sleep(0)
            assert not pending.done()
            assert io._active
            release.set()
            with pytest.raises(asyncio.CancelledError):
                await pending
            assert not io._active
        finally:
            release.set()
            await io.aclose(deadline_monotonic=monotonic() + 20)

    asyncio.run(run())


@pytest.mark.parametrize(
    "rule,code",
    [
        ('@import "missing.css";body{}', "CSS_IMPORT_UNSUPPORTED"),
        ("@font-face{font-family:x;src:url(font.woff2)}", "CSS_FONT_UNSUPPORTED"),
    ],
)
def test_external_css_bom_does_not_hide_unsupported_declarations(tmp_path, rule, code):
    (tmp_path / "style.css").write_bytes(b"\xef\xbb\xbf" + rule.encode())
    with pytest.raises(ValueError, match=code):
        asyncio.run(
            prepare(
                tmp_path / "index.html", b'<link rel="stylesheet" href="style.css">'
            )
        )


def test_external_css_bom_background_is_frozen(tmp_path):
    (tmp_path / "image.png").write_bytes(png())
    (tmp_path / "style.css").write_bytes(
        b"\xef\xbb\xbf" + b"body{background:url(image.png)}"
    )
    frozen = asyncio.run(
        prepare(tmp_path / "index.html", b'<link rel="stylesheet" href="style.css">')
    )
    css = decode(elements(frozen, "link")[0].getAttribute("href"))
    assert base64.b64encode(png()) in css


def test_script_language_and_explicit_type_follow_browser_classification(tmp_path):
    (tmp_path / "app.js").write_text("window.executed=true")
    body = b'<script language="not-javascript" src="missing.js"></script><script type="" language="not-javascript" src="app.js"></script>'
    frozen = asyncio.run(prepare(tmp_path / "index.html", body))
    scripts = elements(frozen, "script")
    assert scripts[0].getAttribute("src") == "missing.js"
    assert decode(scripts[1].getAttribute("src")) == b"window.executed=true"
    with sync_playwright() as p:
        browser = p.chromium.launch()
        try:
            page = browser.new_page()
            page.set_content(shell(page, frozen.decode()), wait_until="load")
            frame = page.locator("#content").element_handle().content_frame()
            assert frame.evaluate("executed") is True
        finally:
            browser.close()


@pytest.mark.parametrize('source', [
    b'<svg xmlns="http://www.w3.org/2000/svg" width="100000" height="100000"/>',
    b'<svg xmlns="http://www.w3.org/2000/svg"><not-closed>',
])
def test_svg_renderer_rejects_invalid_or_over_pixel_budget_instead_of_cropping(source):
    from pulsara_agent.conversation_kernel.visualization_screenshot import VisualizationScreenshotError
    async def exercise():
        owner = VisualizationScreenshotOwner()
        try:
            with pytest.raises(VisualizationScreenshotError, match='PREVIEW_RENDER_FAILED'):
                await owner.render(source, svg=True, deadline_monotonic=monotonic()+15)
        finally:
            await owner.aclose()
        assert not owner._processes
    asyncio.run(exercise())
