"""Real Chromium checks for the lightweight HTML display boundary."""

from __future__ import annotations

from pathlib import Path
import re

from playwright.sync_api import sync_playwright


_CSP = (
    "default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; "
    "img-src data:; font-src data:; connect-src 'none'; worker-src 'none'; "
    "frame-src 'none'; media-src 'none'; object-src 'none'; base-uri 'none'; "
    "form-action 'none'; navigate-to 'none'"
)


def test_visualization_srcdoc_keeps_parent_and_network_isolated() -> None:
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        try:
            context = browser.new_context(accept_downloads=False)
            requests: list[str] = []
            context.route("**/*", lambda route: (requests.append(route.request.url), route.abort()))
            page = context.new_page()
            popups: list[str] = []
            page.on("popup", lambda popup: popups.append(popup.url))
            page.set_content(
                '<meta http-equiv="Content-Security-Policy" content="frame-src about:">'
                '<div id="secret">parent-private</div><div id="mount"></div>'
            )
            payload = """<!doctype html><html><body>
              <h1 id="demo">safe chart</h1>
              <img src="https://example.invalid/chart.png">
              <script>
                document.body.dataset.scriptRan = 'yes';
                try { parent.document.querySelector('#secret').remove(); } catch (_) {}
                fetch('https://example.invalid/collect').catch(() => {});
                try { window.open('https://example.invalid/popup'); } catch (_) {}
                try { top.location.href = 'https://example.invalid/top'; } catch (_) {}
                try { location.href = 'https://example.invalid/self'; } catch (_) {}
              </script>
            </body></html>"""
            page.locator("#mount").evaluate(
                "(mount, value) => { const frame = document.createElement('iframe'); "
                "frame.setAttribute('sandbox', 'allow-scripts'); "
                "frame.referrerPolicy = 'no-referrer'; frame.srcdoc = value; "
                "mount.appendChild(frame); }",
                f'<!doctype html><meta http-equiv="Content-Security-Policy" content="{_CSP}">' + payload,
            )
            page.wait_for_timeout(1000)
            assert page.locator("#secret").inner_text() == "parent-private"
            assert page.locator("#mount iframe").count() == 1
            assert popups == []
            assert requests == []
            assert page.locator("#mount iframe").count() == 1
            page.locator("#mount iframe").evaluate(
                "frame => frame.srcdoc = '<!doctype html><h1 id=demo>safe chart</h1>'"
            )
            assert page.frame_locator("#mount iframe").locator("#demo").inner_text() == "safe chart"
        finally:
            browser.close()


def test_measurement_script_ignores_hidden_roots_and_remeasures_dynamic_marker() -> None:
    source = (
        Path(__file__).resolve().parents[1] / "frontend/lib/visualization-frame.ts"
    ).read_text(encoding="utf-8")
    match = re.search(
        r"export const visualizationFrameMeasurementScript = `(<script>.*?</script>)`;",
        source,
        re.DOTALL,
    )
    assert match is not None
    script = match.group(1).replace(
        "${visualizationRoot.toString()}",
        (Path(__file__).resolve().parents[1] / "src/pulsara_agent/conversation_kernel/visualization_root.js")
        .read_text().replace("export default ", "", 1),
    )
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        try:
            page = browser.new_page(viewport={"width": 800, "height": 600})
            page.set_content("<div id='mount'></div>")
            page.evaluate(
                """({html}) => {
                  window.layouts = [];
                  window.addEventListener('message', event => window.layouts.push(event.data));
                  const frame = document.createElement('iframe');
                  frame.setAttribute('sandbox', 'allow-scripts');
                  frame.width = '600'; frame.height = '500';
                  frame.srcdoc = html;
                  document.querySelector('#mount').append(frame);
                }""",
                {"html": (
                    "<!doctype html><html><body>"
                    "<main data-pulsara-visualization-root "
                    "style='visibility:hidden;width:200px;height:100px'></main>"
                    + script + "</body></html>"
                )},
            )
            page.wait_for_function("() => window.layouts.some(item => item.mode === 'page')")
            assert not page.evaluate(
                "() => window.layouts.some(item => item.mode === 'root')"
            )
            page.locator("iframe").evaluate(
                """(frame, script) => {
                  window.layouts = [];
                  frame.srcdoc = `<!doctype html><html><body><div>page</div>
                    <script>setTimeout(() => {
                      const root = document.createElement('main');
                      root.setAttribute('data-pulsara-visualization-root', '');
                      root.style.cssText = 'width:220px;height:120px;background:#abc';
                      document.body.append(root);
                      setTimeout(() => root.animate(
                        [{width:'220px'}, {width:'280px'}],
                        {duration:200, fill:'forwards'}
                      ), 300);
                    }, 50);</script>${script}</body></html>`;
                }""",
                script,
            )
            page.wait_for_function(
                "() => window.layouts.some(item => item.mode === 'root' && item.rect.width === 220)"
            )
            page.wait_for_function(
                "() => window.layouts.some(item => item.mode === 'root' && item.rect.width >= 278)"
            )
        finally:
            browser.close()


def test_automatic_main_requires_complete_unambiguous_visible_content() -> None:
    selector = (
        Path(__file__).resolve().parents[1]
        / "src/pulsara_agent/conversation_kernel/visualization_root.js"
    ).read_text().replace("export default ", "", 1)
    base = '<main id="chart" style="width:320px;height:180px">chart</main>'
    cases = [
        (base, "chart"),
        ('<style>html::after{content:"Important note"}</style>' + base, None),
        ('<div>' + base + '</div>', "chart"),
        (base + '<main hidden>other</main><footer hidden>hidden</footer>', "chart"),
        (base + '<main>other</main>', None),
        (base + '<footer>Important source note</footer>', None),
        (base + 'Important source note', None),
        (base + '<div style="display:contents">source note</div>', None),
        (base.replace('chart</main>', '<div style="position:absolute;top:300px">overflow</div></main>'), None),
        (base.replace('chart</main>', '<span style="white-space:nowrap">' + 'Long text ' * 40 + '</span></main>'), None),
        ('<style>main::after{content:"note";position:absolute;top:300px}</style>' + base, None),
        (base + '<aside style="visibility:hidden"><span style="visibility:visible">note</span></aside>', None),
        (base + '<aside data-pulsara-visualization-root hidden>bad explicit root</aside>', None),
        (base + '<aside id="explicit" data-pulsara-visualization-root>explicit choice</aside>', "explicit"),
        ('<div style="display:contents">' + base + '</div>', "chart"),
    ]
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        try:
            page = browser.new_page()
            for html, expected in cases:
                page.set_content('<!doctype html><body>' + html)
                actual = page.evaluate('() => {' + selector + '; return visualizationRoot()?.id ?? null; }')
                assert actual == expected, html
        finally:
            browser.close()


def test_automatic_crop_stays_in_page_mode_after_interaction_overflow_or_animation() -> None:
    root = Path(__file__).resolve().parents[1]
    selector = (root / "src/pulsara_agent/conversation_kernel/visualization_root.js").read_text().replace("export default ", "", 1)
    source = (root / "frontend/lib/visualization-frame.ts").read_text()
    script = re.search(r"export const visualizationFrameMeasurementScript = `(<script>.*?</script>)`;", source, re.DOTALL).group(1)
    script = script.replace("${visualizationRoot.toString()}", selector)
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        try:
            page = browser.new_page(viewport={"width": 800, "height": 600})
            page.set_content('<iframe style="width:600px;height:500px"></iframe>')
            page.evaluate("() => {window.layouts=[]; addEventListener('message',e=>window.layouts.push(e.data));}")
            base = '''<!doctype html><style>
                body{margin:0;height:500px}main{position:relative;width:250px;height:100px;background:#ddd}
                .note{display:none;position:absolute;top:130px;background:red}
                main:hover .note{display:block}
                </style><main><button>Details</button><div class="note">Important detail</div></main>'''
            page.locator('iframe').evaluate('(frame,html)=>frame.srcdoc=html', base + script)
            page.wait_for_function("()=>window.layouts.at(-1)?.mode==='root'")
            frame = page.frames[1]
            frame.locator('button').hover()
            page.wait_for_function("()=>window.layouts.at(-1)?.mode==='page'")
            assert frame.locator('.note').is_visible()
            # Leaving hover does not trigger crop/page oscillation or recreate controls.
            frame.locator('button').evaluate("el=>el.dataset.kept='yes'")
            page.mouse.move(750, 550)
            frame.locator('button').focus()
            page.wait_for_timeout(60)
            assert page.evaluate("()=>window.layouts.at(-1).mode") == 'page'
            assert frame.locator('button').get_attribute('data-kept') == 'yes'
            # A fresh iframe with an ordinary safe interaction remains cropped.
            page.locator('iframe').evaluate('(frame,html)=>frame.srcdoc=html', base.replace('main:hover .note{display:block}', '') + script)
            page.wait_for_function("()=>window.layouts.at(-1)?.mode==='root'")
            frame = page.frames[1]
            frame.locator('button').focus()
            page.wait_for_timeout(60)
            assert page.evaluate("()=>window.layouts.at(-1).mode") == 'root'
            # A CSS transition starts without changing the root's own size.
            frame.locator('main').evaluate("el=>{el.firstElementChild.style.transition='transform .05s';el.firstElementChild.style.transform='translateY(150px)';}")
            page.wait_for_function("()=>window.layouts.at(-1)?.mode==='page'")
            page.wait_for_timeout(100)
            assert page.evaluate("()=>window.layouts.at(-1).mode") == 'page'
            # Delayed animations are detected on initial measurement, before animationstart.
            animated = '''<!doctype html><style>main{width:250px;height:100px}
                span{display:block;animation:move .1s 1s forwards}
                @keyframes move{to{transform:translateY(150px)}}</style><main><span>detail</span></main>'''
            page.evaluate('()=>window.layouts=[]')
            page.locator('iframe').evaluate('(frame,html)=>frame.srcdoc=html', animated + script)
            page.wait_for_function("()=>window.layouts.length>0")
            assert not page.evaluate("()=>window.layouts.some(x=>x.mode==='root')")
        finally:
            browser.close()


def test_hover_growing_root_does_not_oscillate_between_crop_and_scroll() -> None:
    root = Path(__file__).resolve().parents[1]
    selector = (root / "src/pulsara_agent/conversation_kernel/visualization_root.js").read_text().replace("export default ", "", 1)
    source = (root / "frontend/lib/visualization-frame.ts").read_text()
    script = re.search(r"export const visualizationFrameMeasurementScript = `(<script>.*?</script>)`;", source, re.DOTALL).group(1).replace("${visualizationRoot.toString()}", selector)
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        try:
            page = browser.new_page(viewport={"width": 800, "height": 700})
            page.set_content('<div id="stage" style="position:relative;overflow:hidden;width:600px;height:500px"><iframe style="position:absolute;border:0;width:600px;height:500px"></iframe></div>')
            page.evaluate('''() => {
                window.layouts=[];
                addEventListener('message',e=>{
                    const d=e.data;window.layouts.push(d);
                    const r=d.rect, crop=d.mode==='root' && r.x>=0 && r.y>=0 && r.x+r.width<=600 && r.y+r.height<=500;
                    document.querySelector('#stage').style.height=(crop?r.height:500)+'px';
                    document.querySelector('iframe').style.transform=crop?`translate(${-r.x}px,${-r.y}px)`:'';
                });
            }''')
            html = '''<!doctype html><style>body{margin:0;min-height:500px}main{margin-top:80px;width:250px;height:100px;background:#ddd}main:hover{height:600px}</style><main><button>Hover</button></main>'''
            page.locator('iframe').evaluate('(frame,html)=>frame.srcdoc=html',html+script)
            page.wait_for_function("()=>window.layouts.at(-1)?.mode==='root'")
            page.frames[1].locator('button').hover()
            page.wait_for_function("()=>window.layouts.at(-1)?.mode==='page'")
            page.wait_for_timeout(200)
            assert page.evaluate("()=>{const at=window.layouts.findIndex(x=>x.mode==='page');return window.layouts.slice(at).every(x=>x.mode==='page')}")
        finally:
            browser.close()


def test_crop_window_cannot_scroll_away_from_chart_but_inner_page_can_scroll() -> None:
    css = (Path(__file__).resolve().parents[1] / 'frontend/app/styles/workbench.css').read_text()
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        try:
            page = browser.new_page(viewport={'width':800,'height':600})
            page.set_content('<style>'+css+'</style><section class="inline-visualization"><div class="inline-visualization__stage" style="height:200px"><div class="inline-visualization__crop" style="width:400px;height:200px"><iframe style="width:450px;height:900px;transform:translate(-10px,-20px)"></iframe></div></div></section>')
            frame = page.locator('iframe')
            frame.evaluate('(el,html)=>el.srcdoc=html', '<body style="margin:0"><input value="top"><div style="height:1800px">long chart</div><button>bottom</button>')
            page.frames[1].locator('input').focus()
            frame.evaluate("el=>el.scrollIntoView({block:'end'})")
            for name in ['.inline-visualization', '.inline-visualization__stage', '.inline-visualization__crop']:
                # overflow:hidden would accept this scroll and lose the chart's top.
                page.locator(name).evaluate('el=>el.scrollTop=254')
                assert page.locator(name).evaluate('el=>el.scrollTop') == 0
            geometry = frame.evaluate("el=>({top:el.getBoundingClientRect().top,parent:el.parentElement.getBoundingClientRect().top})")
            assert geometry['top'] - geometry['parent'] == -20
            page.frames[1].evaluate('()=>window.scrollTo(0,500)')
            assert page.frames[1].evaluate('()=>window.scrollY') == 500
        finally:
            browser.close()


def test_reference_probe_does_not_extend_scroll_range_for_failed_or_small_chart() -> None:
    css = (Path(__file__).resolve().parents[1] / 'frontend/app/styles/workbench.css').read_text()
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        try:
            for width in [760, 1200]:
                page = browser.new_page(viewport={'width':width,'height':800})
                for content in ['<div class="inline-visualization__status">HTML_NOT_FOUND</div>', '<div style="height:165px">compact chart</div>']:
                    page.set_content('<style>'+css+'</style><div class="thread-scroll" style="height:500px"><div class="thread-column"><section class="inline-visualization"><dialog open class="has-thumbnails"><div class="inline-visualization__figure"><div class="inline-visualization__page"><div class="inline-visualization__viewport-probe"></div><div class="inline-visualization__stage">'+content+'</div></div></div><aside class="inline-visualization__controls"><button>next</button></aside></dialog></section><p>after chart</p></div></div>')
                    # Real reference measurement still exists, but must not create blank scrolling.
                    assert page.locator('.inline-visualization__viewport-probe').evaluate('el=>el.clientHeight') == 960
                    assert page.locator('.thread-scroll').evaluate('el=>el.scrollHeight') == 500
                    assert page.locator('.inline-visualization').evaluate('el=>el.clientHeight') < 250
                    if width == 1200:
                        assert page.locator('aside').evaluate('el=>el.getBoundingClientRect().left') > page.locator('.inline-visualization__page').evaluate('el=>el.getBoundingClientRect().right')
                page.close()
        finally:
            browser.close()


def test_zoom_control_reveals_on_iframe_hover_and_keyboard_focus() -> None:
    css = (Path(__file__).resolve().parents[1] / 'frontend/app/styles/workbench.css').read_text()
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        try:
            page = browser.new_page(reduced_motion='reduce')
            page.set_content('<style>'+css+'</style><section class="inline-visualization"><dialog open><div class="inline-visualization__figure"><iframe title="chart" srcdoc="<h1>Chart</h1>" style="width:600px;height:200px"></iframe><button class="inline-visualization__zoom">Zoom</button></div></dialog></section>')
            button = page.locator('button')
            page.mouse.move(1000, 500)
            assert button.evaluate('el=>getComputedStyle(el).opacity') == '0'
            page.frames[1].locator('h1').hover()
            assert button.evaluate('el=>getComputedStyle(el).opacity') == '1'
            page.mouse.move(1000, 500)
            button.focus()
            assert button.evaluate('el=>getComputedStyle(el).opacity') == '1'
            page.close()
            touch = browser.new_page(has_touch=True, reduced_motion='reduce')
            touch.set_content('<style>'+css+'</style><section class="inline-visualization"><button class="inline-visualization__zoom">Zoom</button></section>')
            assert touch.locator('button').evaluate('el=>getComputedStyle(el).opacity') == '0.65'
        finally:
            browser.close()


def test_short_chart_omits_zoom_space_and_regular_chart_keeps_zoom_clickable() -> None:
    css = (Path(__file__).resolve().parents[1] / 'frontend/app/styles/workbench.css').read_text()
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        try:
            page = browser.new_page(reduced_motion='reduce')
            for height in [8, 20, 45, 46, 165]:
                # Include the production clip ancestors; a figure-only fixture misses this bug.
                compact = ' data-visualization-compact="true"' if height < 46 else ''
                control = '' if height < 46 else '<div class="inline-visualization__zoom-anchor"><button class="inline-visualization__zoom" onclick="this.dataset.clicked=1">Zoom</button></div>'
                page.set_content('<style>'+css+'</style><section class="inline-visualization"><dialog open><div class="inline-visualization__figure"><div class="inline-visualization__page"><div class="inline-visualization__viewport-probe"></div><div class="inline-visualization__stage"'+compact+' style="height:'+str(height)+'px"><div class="inline-visualization__crop" style="height:'+str(height)+'px"><iframe style="width:600px;height:500px" srcdoc="short chart"></iframe></div>'+control+'</div></div></div></dialog></section>')
                stage = page.locator('.inline-visualization__stage').bounding_box()
                assert stage['height'] == height
                button = page.locator('button')
                if height < 46:  # Below the 8 + 30 + 8px control footprint, no extra space.
                    assert button.count() == 0
                else:
                    button.focus()
                    bounds = button.bounding_box()
                    assert bounds['y'] >= stage['y'] + 8
                    assert bounds['y'] + bounds['height'] <= stage['y'] + stage['height'] - 8
                    button.click()
                    assert button.get_attribute('data-clicked') == '1'
                assert page.locator('.inline-visualization__crop').evaluate('el=>el.clientHeight') == height
                assert page.locator('iframe').evaluate('el=>el.clientHeight') == 500
        finally:
            browser.close()


def test_toast_keeps_its_layout_inside_maximized_visualization() -> None:
    styles = Path(__file__).resolve().parents[1] / 'frontend/app/styles'
    css = '\n'.join((styles / name).read_text() for name in ['base.css', 'workbench.css', 'overlays.css'])
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        try:
            page = browser.new_page()
            page.set_content('<style>' + css + '</style><section class="inline-visualization"><dialog><div class="inline-visualization__figure"><button class="inline-visualization__zoom">zoom</button></div><div class="toast-stack" popover="manual"><button class="toast toast--success"><span class="toast-mark">✓</span><span><strong>输入已接受</strong><small>已送达本地服务。</small></span><svg width="11" height="11"></svg></button></div></dialog></section>')
            page.locator('dialog').evaluate('el => el.showModal()')
            page.locator('.toast-stack').evaluate('el => el.showPopover()')
            toast = page.locator('.toast')
            assert toast.evaluate('el => getComputedStyle(el).display') == 'grid'
            assert toast.evaluate('el => getComputedStyle(el).borderRadius') == '11px'
            assert toast.evaluate('el => getComputedStyle(el).padding') == '9px 10px'
            geometry = toast.evaluate('el => Array.from(el.children).map(child => child.getBoundingClientRect().left)')
            assert geometry[0] < geometry[1] < geometry[2]
            page.locator('.inline-visualization__zoom').focus()
            assert page.locator('.inline-visualization__zoom').evaluate('el => getComputedStyle(el).padding') == '6px'
        finally:
            browser.close()
