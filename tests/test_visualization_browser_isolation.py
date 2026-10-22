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


def test_ordinary_div_uses_natural_document_bounds_and_preserves_author_spacing() -> None:
    root = Path(__file__).resolve().parents[1]
    selector = (root / "src/pulsara_agent/conversation_kernel/visualization_root.js").read_text().replace("export default ", "", 1)
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        try:
            page = browser.new_page(viewport={"width": 600, "height": 500})
            html = """<!doctype html><style>
                body{margin:12px 0 18px;padding:6px 0 10px}
                .wrap{padding:20px 16px 40px;margin:14px 0 24px}
                .content{height:90px}.spacer{height:70px}
                </style><div class="wrap"><div class="content">Report</div>
                <div class="spacer"></div></div>"""
            page.set_content(html)
            bounds = page.evaluate("() => {" + selector + """;
                const root=visualizationRoot();
                return {tag:root?.tagName,height:root?.getBoundingClientRect().height,
                    contentBottom:document.querySelector('.content').getBoundingClientRect().bottom,
                    scrollHeight:document.documentElement.scrollHeight};
            }""")
            assert bounds == {"tag": "HTML", "height": 304, "contentBottom": 142, "scrollHeight": 500}
            # The 162px after text includes an intentional 70px spacer, wrapper
            # padding/margin and body padding/margin. None is trimmed away.
            assert bounds["height"] - bounds["contentBottom"] == 162
            page.set_content("<!doctype html><style>body{margin:0}.wrap{margin:30px 0}.inner{height:100px;margin:20px 0}</style><div class=wrap><div class=inner>Collapsed margins</div></div>")
            assert page.evaluate("() => {" + selector + "; return visualizationRoot()?.getBoundingClientRect().height; }") == 160
            page.set_content("<!doctype html><style>body{margin:0;min-height:100vh}</style><div>Intentional full page</div>")
            assert page.evaluate("() => {" + selector + "; return visualizationRoot()?.getBoundingClientRect().height; }") == 500
            for ambiguous in [
                "<style>html{margin:30px}</style><div>Page framing</div>",
                "<div>Report</div><footer>Source note</footer>",
                "<div><main>Report</main></div><footer>Source note</footer>",
                "<div>Report</div><div>Another panel</div>",
                "<div style='position:absolute;top:300px'>Out of flow</div>",
                "<div>Report<aside style='position:fixed;bottom:0'>Fixed note</aside></div>",
                "<style>body::after{content:'Source note'}</style><div>Report</div>",
            ]:
                page.set_content("<!doctype html>" + ambiguous)
                assert page.evaluate("() => {" + selector + "; return visualizationRoot() === null; }") is True, ambiguous
        finally:
            browser.close()


def test_ordinary_div_remeasures_expansion_and_keeps_overflow_reachable() -> None:
    root = Path(__file__).resolve().parents[1]
    selector = (root / "src/pulsara_agent/conversation_kernel/visualization_root.js").read_text().replace("export default ", "", 1)
    source = (root / "frontend/lib/visualization-frame.ts").read_text()
    script = re.search(r"export const visualizationFrameMeasurementScript = `(<script>.*?</script>)`;", source, re.DOTALL).group(1).replace("${visualizationRoot.toString()}", selector)
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        try:
            page = browser.new_page(viewport={"width": 800, "height": 700})
            page.set_content('<div id="stage" style="position:relative;overflow:clip;width:600px;height:500px"><iframe sandbox="allow-scripts" style="position:absolute;border:0;width:600px;height:500px"></iframe></div>')
            page.evaluate("""() => {
                window.layouts=[];
                addEventListener('message',e=>{
                    const d=e.data;window.layouts.push(d);
                    const r=d.rect,crop=d.mode==='root'&&r.y>=0&&r.y+r.height<=500;
                    document.querySelector('#stage').style.height=(crop?Math.ceil(r.y+r.height)-Math.floor(r.y):500)+'px';
                    document.querySelector('iframe').style.transform=crop?`translateY(${-Math.floor(r.y)}px)`:'';
                });
            }""")
            html = """<!doctype html><style>
                body{margin:0}.wrap{padding:20px 16px 40px}
                summary{height:20px;line-height:20px}.detail{height:180px}
                </style><div class="wrap"><input value="Retained state">
                <details><summary>Details</summary><div class="detail">Expanded content</div></details>
                <button onclick="this.previousElementSibling.lastElementChild.style.height='700px';this.previousElementSibling.open=true">Long details</button></div>"""
            page.locator('iframe').evaluate('(frame,html)=>frame.srcdoc=html', html + script)
            page.wait_for_function("()=>window.layouts.at(-1)?.mode==='root'")
            frame = page.frames[1]
            initial = page.evaluate("()=>window.layouts.at(-1).rect.height")
            assert initial < 200
            frame.locator('input').fill('Kept after expansion')
            frame.locator('summary').click()
            page.wait_for_function("height=>window.layouts.at(-1)?.mode==='root'&&window.layouts.at(-1).rect.height===height+180", arg=initial)
            assert page.locator('#stage').evaluate('el=>el.clientHeight') == initial + 180
            assert frame.locator('.wrap').evaluate('el=>getComputedStyle(el).paddingBottom') == '40px'
            frame.locator('summary').click()
            page.wait_for_function("height=>window.layouts.at(-1)?.mode==='root'&&window.layouts.at(-1).rect.height===height", arg=initial)
            frame.locator('.wrap').evaluate("el=>{const note=document.createElement('div');note.style.height='80px';note.textContent='Async content';el.append(note)}")
            page.wait_for_function("height=>window.layouts.at(-1)?.mode==='root'&&window.layouts.at(-1).rect.height===height+80", arg=initial)
            frame.locator('button').click()
            page.wait_for_function("()=>window.layouts.at(-1)?.mode==='page'")
            assert page.locator('#stage').evaluate('el=>el.clientHeight') == 500
            frame.evaluate('()=>window.scrollTo(0,document.documentElement.scrollHeight)')
            assert frame.evaluate('()=>scrollY>0')
            assert frame.locator('input').input_value() == 'Kept after expansion'
            assert frame.locator('.detail').is_visible()
            # Switching html -> main -> html must not disconnect the always-on
            # document observer used for later natural-height changes.
            frame.evaluate("()=>{document.body.innerHTML='<main style=height:100px>Main</main>';window.scrollTo(0,0)}")
            # Prior real overflow remains page until viewport changes.
            page.locator('iframe').evaluate("el=>el.style.width='620px'")
            page.wait_for_function("()=>window.layouts.at(-1)?.mode==='root'&&window.layouts.at(-1).rect.height===100")
            # WAAPI changes an ancestor's layout without DOM mutations or CSS
            # transition events; the main box itself does not resize. Only the
            # retained document ResizeObserver sees these intermediate bounds.
            page.mouse.move(750, 650)
            page.evaluate('()=>new Promise(resolve=>requestAnimationFrame(()=>requestAnimationFrame(resolve)))')
            page.evaluate('()=>window.layouts=[]')
            frame.evaluate("()=>window.spacingAnimation=document.body.animate([{paddingTop:'0px'},{paddingTop:'120px'}],{duration:2000,fill:'forwards'})")
            page.wait_for_function("()=>{const d=window.layouts.at(-1);return d?.mode==='root'&&d.rect.y>10&&d.rect.y<110}", timeout=1500)
            frame.evaluate("()=>{window.spacingAnimation.cancel();document.body.innerHTML='<div style=height:120px>Wrapper</div>'}")
            page.wait_for_function("()=>window.layouts.at(-1)?.mode==='root'&&window.layouts.at(-1).rect.height===120")
            frame.locator('div').evaluate("el=>el.style.height='160px'")
            page.wait_for_function("()=>window.layouts.at(-1)?.mode==='root'&&window.layouts.at(-1).rect.height===160")
        finally:
            browser.close()


def test_automatic_crop_stays_in_page_mode_after_real_interaction_overflow() -> None:
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
            # A pending animation alone does not expand the frame; actual overflow does.
            animated = '''<!doctype html><style>main{width:250px;height:100px}
                span{display:block;animation:move .1s 1s forwards}
                @keyframes move{to{transform:translateY(150px)}}</style><main><span>detail</span></main>'''
            page.evaluate('()=>window.layouts=[]')
            page.locator('iframe').evaluate('(frame,html)=>frame.srcdoc=html', animated + script)
            page.wait_for_function("()=>window.layouts.at(-1)?.mode==='root'")
            page.wait_for_function("()=>window.layouts.at(-1)?.mode==='page'")
            assert page.frames[1].locator('span').is_visible()
        finally:
            browser.close()



def test_color_and_contained_hover_transitions_keep_crop_throughout_interaction() -> None:
    root = Path(__file__).resolve().parents[1]
    selector = (root / "src/pulsara_agent/conversation_kernel/visualization_root.js").read_text().replace("export default ", "", 1)
    source = (root / "frontend/lib/visualization-frame.ts").read_text()
    script = re.search(r"export const visualizationFrameMeasurementScript = `(<script>.*?</script>)`;", source, re.DOTALL).group(1).replace("${visualizationRoot.toString()}", selector)
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        try:
            page = browser.new_page(viewport={"width": 800, "height": 700})
            page.set_content('<div id="stage" style="overflow:hidden;width:600px;height:500px"><iframe sandbox="allow-scripts" style="border:0;width:600px;height:500px"></iframe></div>')
            page.evaluate('''() => {
                window.layouts=[];
                addEventListener('message',e=>{
                    const d=e.data;window.layouts.push(d);
                    document.querySelector('#stage').style.height=(d.mode==='root'?d.rect.height:500)+'px';
                });
            }''')
            html = '''<!doctype html><style>
                body{margin:0}main{height:100px;background:white;transition:background-color .15s}
                main.dark{background:black}
                button{margin:10px;transition:transform .15s,box-shadow .15s}
                button:hover{transform:translateY(-2px);box-shadow:0 1px 4px #888}
                </style><main><button onclick="this.parentElement.classList.toggle('dark')">Theme</button></main>'''
            page.locator('iframe').evaluate('(frame,html)=>frame.srcdoc=html', html + script)
            page.wait_for_function("()=>window.layouts.at(-1)?.mode==='root'")
            frame = page.frames[1]
            frame.locator('button').evaluate("el=>el.dataset.kept='yes'")
            def assert_stable_height() -> None:
                # Drain animation completion and geometry reports, checking the
                # entire trace so a temporary expand/shrink cannot pass.
                frame.evaluate("""async () => {
                    await Promise.allSettled(document.getAnimations().map(a=>a.finished));
                    await new Promise(requestAnimationFrame);
                }""")
                page.evaluate("() => new Promise(resolve=>requestAnimationFrame(()=>requestAnimationFrame(resolve)))")
                assert page.evaluate("()=>window.layouts.every(d=>d.mode==='root' && d.rect.height===100)")
                assert page.locator('#stage').evaluate('el=>el.clientHeight') == 100
                assert frame.locator('button').get_attribute('data-kept') == 'yes'

            for _ in range(3):
                frame.locator('button').hover()
                frame.wait_for_function("()=>document.getAnimations().some(a=>a.playState==='running')")
                assert_stable_height()
                frame.locator('button').click()
                frame.wait_for_function("()=>document.getAnimations().some(a=>a.playState==='running')")
                assert_stable_height()
                page.mouse.move(750, 550)
                assert_stable_height()
            # Cancellation must also leave the host height unchanged.
            frame.locator('main').evaluate("el=>el.style.transitionDuration='10s'")
            frame.locator('button').click()
            frame.wait_for_function("()=>document.getAnimations().some(a=>a.playState==='running')")
            frame.locator('main').evaluate("el=>el.style.transition='none'")
            assert_stable_height()
            # Genuine in-flow expansion still updates the measured height.
            frame.locator('main').evaluate("""el=>{
                el.style.height='auto';el.style.minHeight='100px';
                const detail=document.createElement('div');
                detail.style.height='180px';detail.textContent='Expanded details';
                el.append(detail);
            }""")
            page.wait_for_function("()=>window.layouts.at(-1)?.mode==='root' && window.layouts.at(-1).rect.height>180")
            assert page.locator('#stage').evaluate('el=>el.clientHeight') > 180
        finally:
            browser.close()


def test_crop_recovers_after_zoom_viewport_and_retained_gallery_switch() -> None:
    root = Path(__file__).resolve().parents[1]
    selector = (root / "src/pulsara_agent/conversation_kernel/visualization_root.js").read_text().replace("export default ", "", 1)
    source = (root / "frontend/lib/visualization-frame.ts").read_text()
    script = re.search(r"export const visualizationFrameMeasurementScript = `(<script>.*?</script>)`;", source, re.DOTALL).group(1).replace("${visualizationRoot.toString()}", selector)
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        try:
            page = browser.new_page(viewport={"width": 1000, "height": 800})
            page.set_content('<div id="gallery"><iframe style="border:0;width:600px;height:500px"></iframe></div>')
            page.evaluate("() => {window.layouts=[];addEventListener('message',e=>window.layouts.push(e.data));}")
            html = '<!doctype html><style>body{margin:0}main{height:300px}button{margin-top:240px}</style><main><input value="saved interaction"><button>Bottom</button></main>'
            page.locator('iframe').evaluate('(el,html)=>el.srcdoc=html', html + script)
            page.wait_for_function("()=>window.layouts.at(-1)?.mode==='root'")
            frame = page.frames[1]
            original_frame = frame
            frame.locator('input').fill('edited before zoom')
            # Fullscreen uses a different, sometimes shorter viewport. Interacting
            # with its bottom control legitimately scrolls this document.
            page.locator('iframe').evaluate("el=>{el.style.width='800px';el.style.height='200px';}")
            frame.locator('button').click()
            frame.wait_for_function('()=>scrollY>0')
            page.wait_for_function("""()=>{
                const d=window.layouts.at(-1);
                return d?.viewport.height===200 && (d.mode==='page' || d.rect.y<0 || d.rect.y+d.rect.height>200);
            }""")
            page.locator('iframe').evaluate("el=>{el.style.width='600px';el.style.height='500px';}")
            frame.wait_for_function('()=>innerWidth===600 && innerHeight===500 && scrollY===0')
            page.wait_for_function("()=>window.layouts.at(-1)?.mode==='root' && window.layouts.at(-1).rect.height===300", timeout=2000)
            # Thumbnail selection retains the same iframe and author state.
            page.locator('#gallery').evaluate('el=>el.hidden=true')
            page.evaluate('()=>new Promise(resolve=>requestAnimationFrame(()=>requestAnimationFrame(resolve)))')
            page.evaluate('()=>window.layouts=[]')
            page.locator('#gallery').evaluate('el=>el.hidden=false')
            page.wait_for_function("()=>window.layouts.at(-1)?.mode==='root' && window.layouts.at(-1).rect.height===300", timeout=2000)
            assert page.frames[1] == original_frame
            assert frame.locator('input').input_value() == 'edited before zoom'
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
                    page.set_content('<style>'+css+'</style><div class="thread-scroll" style="height:500px"><div class="thread-column"><section class="inline-visualization"><div class="inline-visualization__layout has-thumbnails"><div class="inline-visualization__figure"><div class="inline-visualization__page"><div class="inline-visualization__viewport-probe"></div><div class="inline-visualization__stage">'+content+'</div></div></div><aside class="inline-visualization__controls"><button>next</button></aside></div></section><p>after chart</p></div></div>')
                    # Match the production non-modal layout, retaining the probe and crop ancestors.
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
            page.set_content('<style>'+css+'</style><section class="inline-visualization"><div class="inline-visualization__layout"><div class="inline-visualization__figure"><iframe title="chart" srcdoc="<h1>Chart</h1>" style="width:600px;height:200px"></iframe><button class="inline-visualization__zoom">Zoom</button></div></div></section>')
            button = page.locator('button')
            page.mouse.move(1000, 500)
            assert button.evaluate('el=>getComputedStyle(el).opacity') == '0'
            page.frames[1].locator('h1').hover()
            # Live transitions now remain enabled even with the OS reduce flag.
            page.wait_for_function("() => getComputedStyle(document.querySelector('button')).opacity === '1'")
            assert button.evaluate('el=>getComputedStyle(el).opacity') == '1'
            page.mouse.move(1000, 500)
            button.focus()
            page.wait_for_function("() => getComputedStyle(document.querySelector('button')).opacity === '1'")
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
                page.set_content('<style>'+css+'</style><section class="inline-visualization"><div class="inline-visualization__layout"><div class="inline-visualization__figure"><div class="inline-visualization__page"><div class="inline-visualization__viewport-probe"></div><div class="inline-visualization__stage"'+compact+' style="height:'+str(height)+'px"><div class="inline-visualization__crop" style="height:'+str(height)+'px"><iframe style="width:600px;height:500px" srcdoc="short chart"></iframe></div>'+control+'</div></div></div></div></section>')
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


def test_hidden_gallery_page_keeps_document_and_animation_without_extending_scroll() -> None:
    css = (Path(__file__).resolve().parents[1] / 'frontend/app/styles/workbench.css').read_text()
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        try:
            page = browser.new_page(viewport={'width': 800, 'height': 800})
            page.set_content('<style>' + css + '''</style>
                <div class="thread-scroll" style="height:500px;overflow:auto">
                  <button id="before">Before gallery</button>
                  <section class="inline-visualization">
                    <div class="inline-visualization__layout">
                      <div class="inline-visualization__figure">
                        <div id="long" class="inline-visualization__page">
                          <div class="inline-visualization__viewport-probe"></div>
                          <div class="inline-visualization__stage" style="height:1600px">
                            <div class="inline-visualization__crop" style="height:1600px">
                              <iframe sandbox="allow-scripts" style="width:600px;height:960px"></iframe>
                            </div>
                            <button id="hidden-control">Long page control</button>
                          </div>
                        </div>
                        <div id="short" class="inline-visualization__page" hidden inert aria-hidden="true">
                          <div class="inline-visualization__viewport-probe"></div>
                          <div class="inline-visualization__stage" style="height:120px">
                            <button id="short-control">Short page control</button>
                          </div>
                        </div>
                      </div>
                    </div>
                  </section>
                  <button id="after">After gallery</button>
                </div>''')
            frame = page.locator('iframe')
            frame.evaluate('''(frame) => {
                window.retainedFrame = frame;
                window.retainedWindow = frame.contentWindow;
                frame.srcdoc = `<!doctype html><style>
                    @keyframes spin { to { transform:rotate(360deg) } }
                    #animated { width:20px;height:20px;animation:spin 8s linear infinite }
                    </style><div id="animated">Chart</div><input value="initial">
                    <button onclick="window.clicks=(window.clicks||0)+1">Inner control</button>`;
            }''')
            inner = page.frames[1]
            inner.wait_for_function("() => document.querySelector('#animated')?.getAnimations().length === 1")
            inner.locator('input').fill('retained input')
            inner.evaluate('''() => {
                window.retainedAnimation = document.querySelector('#animated').getAnimations()[0];
                retainedAnimation.pause(); retainedAnimation.currentTime = 432;
            }''')
            dimensions = page.locator('#long').evaluate('''el => ({
                probe: el.querySelector('.inline-visualization__viewport-probe').clientHeight,
                frameWidth: el.querySelector('iframe').clientWidth,
                frameHeight: el.querySelector('iframe').clientHeight,
            })''')
            assert dimensions == {'probe': 960, 'frameWidth': 600, 'frameHeight': 960}
            assert page.locator('.thread-scroll').evaluate('el => el.scrollHeight') > 1600
            page.evaluate('''() => {
                const long = document.querySelector('#long'), short = document.querySelector('#short');
                long.hidden = true; long.inert = true; long.setAttribute('aria-hidden', 'true');
                short.hidden = false; short.inert = false; short.setAttribute('aria-hidden', 'false');
                document.querySelector('.thread-scroll').scrollTop = 0;
            }''')
            assert page.locator('.thread-scroll').evaluate('el => el.scrollHeight') == 500
            assert page.locator('.inline-visualization__figure').evaluate('el => el.clientHeight') == 120
            assert page.locator('#long').evaluate('''el => ({
                probe: el.querySelector('.inline-visualization__viewport-probe').clientHeight,
                frameWidth: el.querySelector('iframe').clientWidth,
                frameHeight: el.querySelector('iframe').clientHeight,
            })''') == dimensions
            assert page.evaluate('''() => document.querySelector('iframe') === retainedFrame
                && retainedFrame.contentWindow === retainedWindow''')
            assert inner.evaluate('''() => document.querySelector('#animated').getAnimations()[0] === retainedAnimation
                && retainedAnimation.playState === 'paused' && retainedAnimation.currentTime === 432''')
            assert page.locator('#long').evaluate('el => el.inert && getComputedStyle(el).visibility === "hidden"')
            page.locator('#before').focus()
            page.keyboard.press('Tab')
            assert page.evaluate('() => document.activeElement.id') == 'short-control'
            page.locator('#hidden-control').evaluate('el => el.focus()')
            assert page.evaluate('() => document.activeElement.id') == 'short-control'
            page.evaluate('''() => {
                const long = document.querySelector('#long'), short = document.querySelector('#short');
                short.hidden = true; short.inert = true; short.setAttribute('aria-hidden', 'true');
                long.hidden = false; long.inert = false; long.setAttribute('aria-hidden', 'false');
            }''')
            assert inner.locator('input').input_value() == 'retained input'
            assert inner.evaluate("() => document.querySelector('#animated').getAnimations()[0] === retainedAnimation")
            inner.evaluate('() => retainedAnimation.play()')
            inner.wait_for_function('() => retainedAnimation.currentTime > 432')
        finally:
            browser.close()
