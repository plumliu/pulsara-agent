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


def desktop_page(browser, *, width, platform, sidebar, workspace, surface=False, motion=False):
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
    if not surface:
        # Match the production display:contents wrapper: it still participates
        # in CSS inheritance between the shell and the two connected panes.
        page.evaluate('''() => {
            const shell = document.querySelector('.desktop-shell');
            const connected = document.createElement('div');
            connected.className = 'session-connected-views';
            shell.append(connected);
            connected.append(shell.querySelector('.workbench'), shell.querySelector('.desktop-workspace'));
        }''')
    page.add_style_tag(content="* { box-sizing: border-box; } body { margin: 0; } button { border: 0; }")
    for name in ("base", "shell", "workbench", "responsive", "desktop"):
        page.add_style_tag(content=(STYLES / f"{name}.css").read_text())
    # Finish the production sidebar/workspace transition before measuring it.
    if not motion:
        page.add_style_tag(content="* { transition: none !important; }")
    return page


@pytest.mark.parametrize("width", [1000, 1440])
@pytest.mark.parametrize("sidebar", [False, True])
def test_entering_conversation_restores_sidebar_without_replaying_motion(browser, width, sidebar):
    page = desktop_page(browser, width=width, platform="darwin", sidebar=sidebar,
                        workspace=0, surface=True, motion=True)
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
    page = desktop_page(browser, width=width, platform="darwin", sidebar=False,
                        workspace=0, motion=True)
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


@pytest.mark.parametrize("platform", ["darwin", "linux"])
@pytest.mark.parametrize("workspace", [320, 1000])
def test_sidebar_motion_preserves_window_controls_and_workspace_bounds(browser, platform, workspace):
    page = desktop_page(browser, width=1440, platform=platform, sidebar=True,
                        workspace=workspace, motion=True)
    try:
        samples = page.evaluate('''async () => {
            const shell = document.querySelector('.desktop-shell');
            const rect = selector => document.querySelector(selector).getBoundingClientRect();
            const frame = () => new Promise(requestAnimationFrame);
            await frame(); await frame();
            const samples = [];
            for (const open of [false, true]) {
                shell.classList.toggle('sidebar-visible', open);
                getComputedStyle(shell).gridTemplateColumns;
                const motion = shell.getAnimations()
                    .find(animation => animation.transitionProperty === '--desktop-navigation-width');
                if (!motion) throw new Error('Missing sidebar motion');
                motion.pause();
                for (const progress of [0, .25, .5, .75, 1]) {
                    motion.currentTime = motion.effect.getTiming().duration * progress;
                    const button = document.querySelector('.desktop-titlebar button');
                    const bounds = button.getBoundingClientRect();
                    samples.push({
                        navigation: rect('.desktop-navigation').width,
                        workbenchLeft: rect('.workbench').left,
                        topbarLeft: rect('.topbar').left,
                        workspaceRight: rect('.desktop-workspace').right,
                        surfaceWidth: rect('.workspace-surface').width,
                        toggleHit: document.elementFromPoint(bounds.x + bounds.width / 2,
                            bounds.y + bounds.height / 2)?.closest('button') === button,
                    });
                }
                for (const animation of shell.getAnimations({subtree: true})) animation.finish();
                await frame(); await frame();
            }
            return samples;
        }''')
        for sample in samples:
            navigation = sample["navigation"]
            assert 62 <= navigation <= 314, sample
            assert sample["workbenchLeft"] == pytest.approx(navigation, abs=.1), sample
            assert sample["topbarLeft"] == pytest.approx(
                max(124, navigation) if platform == "darwin" else navigation, abs=.1,
            ), sample
            assert sample["surfaceWidth"] == pytest.approx(min(workspace, 1440 - navigation - 360), abs=.1), sample
            assert sample["workspaceRight"] == pytest.approx(1440, abs=.1), sample
            assert sample["toggleHit"], sample
    finally:
        page.close()


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
