"""Native drag endpoints at a reply footer, using the production selection mapper."""

import json
from pathlib import Path
import re
import subprocess

from playwright.sync_api import sync_playwright
import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="module")
def selection_browser(tmp_path_factory):
    output = tmp_path_factory.mktemp("response-selection")
    # Vite owns TypeScript compilation; the test runs the actual mapper rather
    # than maintaining a JavaScript copy of its source-mapping rules.
    subprocess.run(
        [
            "node", "--input-type=module", "-e",
            '''import { build } from 'vite';
            await build({configFile:false,logLevel:'silent',build:{
              outDir:process.argv[2],minify:false,lib:{entry:process.argv[1],
              formats:['iife'],name:'PulsaraSelection',fileName:()=> 'selection.js'}}});''',
            str(ROOT / "frontend/lib/response-selection.ts"), str(output),
        ],
        cwd=ROOT / "frontend", check=True, capture_output=True, text=True,
    )
    css = "\n".join(
        (ROOT / "frontend/app/styles" / name).read_text()
        for name in re.findall(
            r"import '../app/styles/(.*?\.css)'",
            (ROOT / "frontend/local/main.tsx").read_text(),
        )
    )
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        yield browser, output / "selection.js", css
        browser.close()


@pytest.mark.parametrize("width", [390, 1446])
@pytest.mark.parametrize("origin", ["below-footer", "inside-footer"])
def test_drag_up_from_blank_footer_maps_only_selected_reply_source(
    selection_browser, width, origin
):
    browser, script, css = selection_browser
    body = "第一段需要解释。\n\n第二段需要核对。\n\n最后一段。"
    paragraphs = "".join(
        '<p><span data-source-text=\''
        + json.dumps([[body.index(text) + i, body.index(text) + i + 1] for i in range(len(text))])
        + "'>" + text + "</span></p>"
        for text in body.split("\n\n")
    )
    page = browser.new_page(viewport={"width": width, "height": 952})
    try:
        page.set_content(f'''<style>{css}</style>
          <div class="desktop-shell" style="display:block">
            <div class="workbench" style="display:block"><div class="thread-scroll" style="height:790px">
              <div class="thread-column conversation-transcript"><article class="assistant-turn">
                <div class="assistant-copy"><div class="assistant-markdown">
                  <div data-annotation-entry="reply">{paragraphs}</div>
                </div><div class="response-actions">
                  <button><svg width="13" height="13"><path d="M0 0L10 10" /></svg></button>
                  <time class="response-time">15:18</time>
                </div></div>
              </article></div>
            </div></div>
          </div>''')
        page.add_script_tag(path=str(script))
        page.evaluate('''body => PulsaraSelection.registerAnnotationBody(
            document.querySelector('[data-annotation-entry]'), body)''', body)
        reply = page.locator('[data-annotation-entry]').bounding_box()
        footer = page.locator('.response-actions').bounding_box()
        second = page.locator('[data-source-text]').nth(1).bounding_box()
        page.mouse.move(
            reply["x"] + reply["width"] * 0.45,
            footer["y"] + (footer["height"] + 12 if origin == "below-footer" else 8),
        )
        page.mouse.down()
        page.mouse.move(second["x"] + 1, second["y"] + second["height"] / 2, steps=10)
        page.mouse.up()
        result = page.evaluate('''() => {
            const selection = getSelection(), native = selection.getRangeAt(0);
            const mapped = PulsaraSelection.mapResponseSelection(selection);
            return {text: selection.toString(), nativeEnd: native.endContainer.nodeName,
                nativeOffset: native.endOffset, annotation: mapped?.annotation,
                menuRangeInsideBody: mapped?.root.contains(mapped.range.endContainer)};
        }''')
        assert "15:18" not in result["text"]
        assert result["nativeEnd"] == "TIME" and result["nativeOffset"] == 0, result
        assert result.get("annotation") == {
            "type": "annotation", "quote": body[body.index("第二段"):],
            "source": {"entry_id": "reply", "start": body.index("第二段"), "end": len(body)},
        }, result
        assert result["menuRangeInsideBody"], result
    finally:
        page.close()
