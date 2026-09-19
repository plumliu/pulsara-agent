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
    script = match.group(1)
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
