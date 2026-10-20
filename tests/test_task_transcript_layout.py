"""Compare conversation styling with real Chromium and the production cascade."""

from pathlib import Path
import re

from playwright.sync_api import sync_playwright
import pytest


ROOT = Path(__file__).resolve().parents[1]
STYLES = ROOT / "frontend/app/styles"
CSS = "\n".join(
    (STYLES / name).read_text()
    for name in re.findall(r"import '../app/styles/(.*?\.css)'", (ROOT / "frontend/local/main.tsx").read_text())
)


@pytest.fixture(scope="module")
def browser():
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        yield browser
        browser.close()


@pytest.mark.parametrize("width", [320, 620])
@pytest.mark.parametrize("theme", ["light", "dark"])
def test_root_and_task_share_compact_tool_and_reply_styles(browser, width, theme):
    # These are the shared ConversationMessages/TraceCard DOM classes. Component
    # tests exercise task projection and live-to-final grouping separately.
    transcript = '''<section class="conversation-run">
      <header class="assistant-heading"><div class="assistant-avatar"></div>
        <div class="assistant-identity"><strong>Pulsara</strong></div></header>
      <button class="conversation-run__toggle">处理过程</button>
      <div class="conversation-run__step"><article class="assistant-turn assistant-turn--operational">
        <div class="reasoning-disclosure"><div class="reasoning-row">
          <button class="reasoning-row__summary"><span class="reasoning-row__icon"></span>
            <span class="reasoning-row__copy"><strong>思考</strong><span>检查结果</span></span></button>
        </div></div>
        <div class="execution-rail"><div class="trace-node"><div class="trace-card">
          <button class="trace-card__summary"><span class="trace-icon"><svg width="14" height="14"></svg></span>
            <span class="trace-summary-copy"><span class="trace-summary-title"><strong>运行命令</strong></span>
              <small>命令执行完成</small></span><span class="trace-state">已完成</span><svg width="13" height="13"></svg></button>
        </div></div></div>
        <section class="subagent-group"><button class="subagent-group__header">任务执行</button>
          <div class="subagent-group__runs"><article class="trace-card subagent-run">
            <button class="trace-card__summary subagent-run__header"><span class="trace-icon"><svg></svg></span>
              <span class="trace-summary-copy"><span class="trace-summary-title"><strong>分析订单</strong></span><small>研究</small></span>
              <span class="trace-state">进行中</span><svg width="13" height="13"></svg></button>
          </article></div></section>
        </article></div>
      <article class="assistant-turn assistant-turn--response"><div class="assistant-copy">
        <div class="assistant-markdown"><h2>检查结果</h2><p>已完成本次检查。</p></div>
      </div></article></section>'''
    page = browser.new_page(viewport={"width": 1440, "height": 900})
    try:
        page.set_content(f'''<html data-theme="{theme}"><style>{CSS}</style>
          <div class="desktop-shell" style="display:block">
            <div id="root" class="thread-column conversation-transcript" style="width:{width}px">{transcript}</div>
            <aside class="task-node-detail" style="width:{width}px"><section>
              <div id="task" class="task-conversation conversation-transcript">{transcript}</div>
            </section></aside>
            <div id="preview" class="file-preview-text is-markdown assistant-markdown">
              <p>文档导语</p><h2>文档标题</h2><p>文档正文保留自己的阅读间距。</p>
            </div>
          </div></html>''')
        metrics = page.evaluate('''() => ['root', 'task'].map(id => {
            const host = document.getElementById(id);
            const style = selector => getComputedStyle(host.querySelector(selector));
            return {
                toolHeight: host.querySelector('.trace-card__summary').getBoundingClientRect().height,
                toolPadding: style('.trace-card__summary').padding,
                toolTitle: style('.trace-summary-copy strong').fontSize,
                toolSubtitle: style('.trace-summary-copy small').fontSize,
                iconWidth: style('.trace-icon').width,
                svgWidth: style('.trace-icon svg').width,
                radius: style('.trace-card').borderRadius,
                reasoningHeight: host.querySelector('.reasoning-row__summary').getBoundingClientRect().height,
                bodyFont: style('.assistant-copy .assistant-markdown').fontSize,
                bodyLine: style('.assistant-copy .assistant-markdown').lineHeight,
                headingFont: style('.assistant-copy h2').fontSize,
                headingColor: style('.assistant-copy h2').color,
                paragraphMargin: style('.assistant-copy p').marginBottom,
                headingMargin: style('.assistant-copy h2').marginBlock,
                groupTopGap: style('.subagent-group').marginTop,
                taskHeight: host.querySelector('.subagent-run__header').getBoundingClientRect().height,
                taskTitle: style('.subagent-run .trace-summary-copy strong').fontSize,
            };
        })''')
        root, task = metrics
        assert root == task, metrics
        assert root["toolHeight"] <= 45
        assert root["toolTitle"] == "11.5px"
        assert root["toolSubtitle"] == "10.5px"
        assert root["iconWidth"] == "22px"
        assert root["svgWidth"] == "12px"
        assert root["bodyFont"] == "16px"
        assert float(root["bodyLine"].removesuffix("px")) == pytest.approx(16 * 1.52)
        assert root["headingFont"] == "19px"
        assert root["paragraphMargin"] == "5px"
        assert root["headingMargin"] == "12px 5px"
        assert float(root["groupTopGap"].removesuffix("px")) <= 8
        assert root["taskHeight"] == root["toolHeight"]
        assert root["taskTitle"] == root["toolTitle"]
        preview = page.evaluate('''() => {
            const host = document.getElementById('preview');
            const style = getComputedStyle(host);
            return {
                font: parseFloat(style.fontSize), line: parseFloat(style.lineHeight),
                paragraphMargin: getComputedStyle(host.querySelector('p')).marginBottom,
                headingMargin: getComputedStyle(host.querySelector('h2')).marginBlock,
            };
        }''')
        assert preview["line"] == pytest.approx(preview["font"] * 1.78)
        assert preview["paragraphMargin"] == "10px"
        assert preview["headingMargin"] == "18px 8px"
    finally:
        page.close()


@pytest.mark.parametrize("theme", ["light", "dark"])
def test_task_detail_sticky_header_covers_scrolled_execution_dots(browser, theme):
    page = browser.new_page(viewport={"width": 900, "height": 700})
    try:
        rows = "".join('''<div class="trace-node trace-node--completed"><span class="trace-node__dot"></span>
          <article class="trace-card"><button class="trace-card__summary">读取文件</button></article></div>''' for _ in range(30))
        page.set_content(f'''<html data-theme="{theme}"><style>{CSS}</style>
          <div class="task-graph-dialog" style="width:380px;height:500px">
            <header>分析任务组</header><div class="task-graph-dialog__body">
              <aside class="task-node-detail"><header><span>子代理</span><button>取消任务</button></header>
                <section><div class="task-conversation conversation-transcript">
                  <div class="execution-rail">{rows}</div>
                </div></section>
              </aside>
            </div>
          </div></html>''')
        page.evaluate('''() => {
            const scroller = document.querySelector('.task-graph-dialog__body');
            const header = document.querySelector('.task-node-detail > header');
            const dot = document.querySelectorAll('.trace-node__dot')[3];
            const h = header.getBoundingClientRect(), d = dot.getBoundingClientRect();
            scroller.scrollTop += d.top + d.height / 2 - h.top - h.height / 2;
        }''')
        result = page.evaluate('''() => {
            const header = document.querySelector('.task-node-detail > header');
            const dot = document.querySelectorAll('.trace-node__dot')[3];
            const h = header.getBoundingClientRect(), d = dot.getBoundingClientRect();
            const x = d.left + d.width / 2, y = d.top + d.height / 2;
            return { overlaps: y > h.top && y < h.bottom,
                headerOnTop: header.contains(document.elementFromPoint(x, y)) };
        }''')
        assert result == {"overlaps": True, "headerOnTop": True}
    finally:
        page.close()


@pytest.mark.parametrize("width", [240, 620])
def test_task_error_text_uses_full_notice_width_and_wraps_normally(browser, width):
    page = browser.new_page(viewport={"width": 900, "height": 700})
    try:
        page.set_content(f'''<html><style>{CSS}</style><aside style="width:{width}px">
          <div class="task-inventory-notice"><p role="alert">请求的本地资源已失效。</p></div>
          <div class="task-inventory-notice"><p role="alert">{"任务暂时无法读取，请稍后重试。" * 12}</p></div>
        </aside></html>''')
        metrics = page.locator('[role="alert"]').evaluate_all('''items => items.map(item => {
            const box = item.getBoundingClientRect(), host = item.parentElement;
            const range = document.createRange(); range.selectNodeContents(item);
            return {width: box.width, available: host.clientWidth - 20,
                lines: range.getClientRects().length, overflow: item.scrollWidth > item.clientWidth};
        })''')
        assert all(item["width"] == pytest.approx(item["available"]) for item in metrics)
        assert metrics[0]["lines"] == 1
        assert metrics[1]["lines"] > 1
        assert all(not item["overflow"] for item in metrics)
    finally:
        page.close()
