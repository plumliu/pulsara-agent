"""Chromium layout coverage for the desktop's platform-specific window chrome.

Use production CSS: jsdom click tests cannot catch clipped controls or overlapping
drag regions. Native GNOME window actions still require desktop acceptance.
"""
from pathlib import Path

from playwright.sync_api import sync_playwright
import pytest


STYLES = Path(__file__).resolve().parents[1] / "frontend/app/styles"


@pytest.fixture(scope="module")
def browser():
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        yield browser
        browser.close()


def desktop_page(browser, *, width, platform, sidebar, surface=False):
    page = browser.new_page(viewport={"width": width, "height": 800})
    classes = "pulsara-shell desktop-shell " + ("is-surface" if surface else "is-workbench")
    if sidebar:
        classes += " sidebar-visible"
    controls = "" if platform == "darwin" else '''<div class="desktop-window-controls">
        <button aria-label="最小化">−</button><button aria-label="最大化或还原">□</button><button aria-label="关闭窗口">×</button></div>'''
    page.set_content(f'''<main data-platform="{platform}" class="{classes}">
        <header class="desktop-titlebar">
            <button aria-label="切换会话侧栏">☰</button>
            {controls}
        </header>
        <div class="desktop-navigation"></div>
    </main>''')
    page.add_style_tag(content="* { box-sizing: border-box; } body { margin: 0; } button { border: 0; }")
    for name in ("base", "shell", "workbench", "responsive", "desktop"):
        page.add_style_tag(content=(STYLES / f"{name}.css").read_text())
    return page


@pytest.mark.parametrize("width", [1000, 1440])
@pytest.mark.parametrize("sidebar", [False, True])
def test_entering_conversation_restores_sidebar_without_replaying_motion(browser, width, sidebar):
    page = desktop_page(browser, width=width, platform="darwin", sidebar=sidebar,
                        surface=True)
    try:
        result = page.evaluate('''async () => {
            const shell = document.querySelector('.desktop-shell');
            const navigation = document.querySelector('.desktop-navigation');
            const frame = () => new Promise(requestAnimationFrame);
            const samples = [];
            for (let visit = 0; visit < 2; visit++) {
                await frame(); await frame();
                shell.classList.replace('is-surface', 'is-workbench');
                navigation.innerHTML = '<aside class="session-sidebar">会话</aside>';
                for (let i = 0; i < 4; i++) {
                    samples.push({ width: navigation.getBoundingClientRect().width,
                        animations: shell.getAnimations({subtree: true}).length });
                    await frame();
                }
                shell.classList.replace('is-workbench', 'is-surface');
                navigation.replaceChildren();
            }
            return samples;
        }''')
        expected = 314 if width > 1150 and sidebar else 62
        assert all(sample["width"] == expected for sample in result), result
        assert all(sample["animations"] == 0 for sample in result), result
    finally:
        page.close()


@pytest.mark.parametrize("width", [1000, 1440])
def test_manual_sidebar_toggles_keep_their_transition(browser, width):
    page = desktop_page(browser, width=width, platform="darwin", sidebar=False)
    try:
        result = page.evaluate('''async () => {
            const shell = document.querySelector('.desktop-shell');
            const navigation = document.querySelector('.desktop-navigation');
            navigation.innerHTML = '<aside class="session-sidebar">会话</aside>';
            const sidebar = navigation.firstElementChild;
            const frame = () => new Promise(requestAnimationFrame);
            await frame(); await frame();
            const samples = [];
            for (const open of [true, false]) {
                shell.classList.toggle('sidebar-visible', open);
                getComputedStyle(sidebar).transform;
                const property = innerWidth > 1150 ? '--desktop-navigation-width' : 'transform';
                const motion = shell.getAnimations({subtree: true})
                    .find(animation => animation.transitionProperty === property);
                if (!motion) throw new Error('Missing sidebar transition: ' + property);
                motion.pause();
                motion.currentTime = motion.effect.getTiming().duration / 2;
                samples.push(innerWidth > 1150 ? navigation.getBoundingClientRect().width
                    : new DOMMatrix(getComputedStyle(sidebar).transform).m41);
                for (const animation of shell.getAnimations({subtree: true})) animation.finish();
                await frame(); await frame();
            }
            return samples;
        }''')
        lower, upper = (62, 314) if width > 1150 else (-252, 0)
        assert all(lower < value < upper for value in result), result
    finally:
        page.close()


@pytest.mark.parametrize("width", [760, 1000, 1440])
def test_linux_overview_keeps_clickable_window_controls(browser, width):
    page = desktop_page(browser, width=width, platform="linux", sidebar=False, surface=True)
    try:
        assert page.locator('.desktop-window-controls button').evaluate_all('''buttons => buttons.every(button => {
            const rect = button.getBoundingClientRect();
            return document.elementFromPoint(rect.x + rect.width / 2, rect.y + rect.height / 2)?.closest('button') === button;
        })''')
    finally:
        page.close()
