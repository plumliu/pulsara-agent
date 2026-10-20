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


def desktop_page(browser, *, width, platform, sidebar, workspace, surface=False):
    page = browser.new_page(viewport={"width": width, "height": 800})
    classes = "pulsara-shell desktop-shell " + ("is-surface" if surface else "is-workbench")
    if sidebar:
        classes += " sidebar-visible"
    if workspace and not surface:
        classes += " has-inspector"
    controls = "" if platform == "darwin" else '''<div class="desktop-window-controls">
        <button aria-label="最小化">−</button><button aria-label="最大化或还原">□</button><button aria-label="关闭窗口">×</button></div>'''
    categories = '''<nav><button>能力</button><button>任务</button>
        <button>后台进程</button><button>新建网页</button></nav>'''
    page.set_content(f'''<main class="{classes}">
        <header class="desktop-titlebar {'is-macos' if platform == 'darwin' else ''}">
            <button aria-label="切换会话侧栏">☰</button>
        </header>
        <div class="desktop-navigation"></div>
        <section class="{'surface-view' if surface else 'workbench'}">
            <header class="topbar"><span>会话 · Ubuntu 顶栏验证</span><button>压缩上下文</button></header>
        </section>
        {'' if surface else f'''<aside class="desktop-workspace {'is-open' if workspace else ''}"
                style="--workspace-width:{workspace or 320}px">
            <div class="workspace-surface"><header class="workspace-topbar">
                <div class="workspace-toolbar">{categories if platform == 'darwin' else ''}
                    <button aria-label="收起工作区">◫</button></div>
            </header>{f'<div class="workspace-toolbar workspace-categories">{categories}</div>' if platform == 'linux' else ''}
            <div class="workspace-tab-strip"><div class="workspace-tabs">
                {''.join('<div class="workspace-tab"><button role="tab">验证文档.md</button></div>' for _ in range(12))}
            </div></div></div></aside>'''}
        {controls}
    </main>''')
    page.add_style_tag(content="* { box-sizing: border-box; } body { margin: 0; } button { border: 0; }")
    for name in ("base", "shell", "workbench", "responsive", "desktop"):
        page.add_style_tag(content=(STYLES / f"{name}.css").read_text())
    # Finish the production sidebar/workspace transition before measuring it.
    page.add_style_tag(content="* { transition: none !important; }")
    return page


@pytest.mark.parametrize("width", [760, 1000, 1440])
@pytest.mark.parametrize("sidebar", [False, True])
@pytest.mark.parametrize("workspace", [0, 320, 700])
def test_linux_window_controls_are_clickable_above_workspace_categories(browser, width, sidebar, workspace):
    page = desktop_page(browser, width=width, platform="linux", sidebar=sidebar, workspace=workspace)
    try:
        geometry = page.evaluate('''() => {
            const categories = document.querySelector('.workspace-categories').getBoundingClientRect();
            const buttons = [...document.querySelectorAll('.desktop-window-controls button')];
            return {
                sessionTop: document.querySelector('.topbar').getBoundingClientRect().top,
                workspaceTop: document.querySelector('.workspace-topbar').getBoundingClientRect().top,
                categoriesTop: categories.top,
                buttons: buttons.map(button => {
                    const rect = button.getBoundingClientRect();
                    return { left: rect.left, right: rect.right, bottom: rect.bottom,
                        hit: document.elementFromPoint(rect.x + rect.width / 2, rect.y + rect.height / 2)?.closest('button') === button,
                        region: getComputedStyle(button).getPropertyValue('-webkit-app-region') };
                }),
                groupRegion: getComputedStyle(document.querySelector('.desktop-window-controls')).getPropertyValue('-webkit-app-region'),
                hasDragAncestor: (() => {
                    for (let node = buttons[0].parentElement; node; node = node.parentElement) {
                        if (getComputedStyle(node).getPropertyValue('-webkit-app-region') === 'drag') return true;
                    }
                    return false;
                })()
            };
        }''')
        assert geometry["sessionTop"] == 0
        if workspace:
            assert geometry["workspaceTop"] == 0
        assert geometry["groupRegion"] == "no-drag"
        assert not geometry["hasDragAncestor"]
        for button in geometry["buttons"]:
            assert 0 <= button["left"] < button["right"] <= width
            if workspace:
                assert button["bottom"] <= geometry["categoriesTop"]
            assert button["hit"]
            assert button["region"] == "no-drag"
    finally:
        page.close()


@pytest.mark.parametrize("width", [760, 1000, 1440])
def test_linux_overview_keeps_clickable_window_controls(browser, width):
    page = desktop_page(browser, width=width, platform="linux", sidebar=False, workspace=0, surface=True)
    try:
        assert page.locator('.desktop-window-controls button').evaluate_all('''buttons => buttons.every(button => {
            const rect = button.getBoundingClientRect();
            return document.elementFromPoint(rect.x + rect.width / 2, rect.y + rect.height / 2)?.closest('button') === button;
        })''')
    finally:
        page.close()


@pytest.mark.parametrize("width", [1000, 1440])
def test_macos_keeps_the_integrated_top_row_and_native_controls(browser, width):
    page = desktop_page(browser, width=width, platform="darwin", sidebar=True, workspace=320)
    try:
        assert page.locator('.desktop-window-controls').count() == 0
        assert page.locator('.topbar').bounding_box()["y"] == 0
        assert page.locator('.workspace-topbar').bounding_box()["y"] == 0
        assert page.locator('.desktop-titlebar').bounding_box()["width"] >= 124
    finally:
        page.close()
