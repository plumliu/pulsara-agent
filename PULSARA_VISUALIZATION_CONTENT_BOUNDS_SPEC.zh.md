# 普通 div 可视化内容边界

日期：2026-10-18。状态：已实现并通过自动化与 GPT-6 Astra high 独立复核；test8 实际页面由用户验收。

本篇仅扩展已有可视化主体选择规则。用户要求普通 `div` 页面也能自适应高度，并保留正常留白和展开后的内容。本篇优先于归档可视化规格中仅识别显式根和唯一 `main` 的限制；其余沙箱、冻结资源、固定参考视口和交互越界保护保持原合同。

## 行为与边界

- 显式 `data-pulsara-visualization-root` 优先级、可见性和无效标记行为不变。
- 存在可见 `main` 时继续走原唯一主体及完整内容检查；多个 `main`、主体之外的脚注等不通过时保留整页，不改用普通 `div` 路径绕过检查。
- 无可见 `main`、`body` 仅有一个可见顶层普通 `div` 时，以 `document.documentElement` 的实际排版边框盒作为候选。忽略脚本、样式、模板等非正文节点，不依赖 `.wrap`、`.container` 等作者类名。
- 使用浏览器 `getBoundingClientRect()`；`scrollHeight` 的视口下限不能当作自然内容高度。保留 `body` 和普通容器的 padding、margin、折叠后的 margin、显式 spacer、固定高度及 `min-height` 等实际布局，不修改作者 CSS，不按最后一个文字节点裁切。
- `html` 自身外边距不在其边框盒内，出现非零外边距时保留整页。多个可见顶层元素、脱流或变换导致内容超出候选、可见伪元素、Shadow DOM 等边界不明确时，沿原检查保留整页。
- 所有自动候选继续经过共享的完整可见内容检查。不得通过放宽 frame/network 权限或移除作者内容来取得紧凑高度。

## 展开与生命周期

浏览器负责 CSS 排版；现有 `ResizeObserver`、`MutationObserver` 和交互事件负责触发一次 RAF 重测；Pulsara 只选择可证明完整的几何窗口。不新增布局引擎、计时猜测或轮询。

- 自然流内展开、收起、异步内容追加及尺寸变化，且仍在参考视口内时更新裁切高度，正常留白随布局保留。
- 超过参考视口时沿原整页/原生滚动路径保持内容可达，不重建 iframe，不丢输入或作者交互状态。
- 同一视口内真实交互越界继续保留原整页防振荡规则；参考视口变化后按原规则重新评估。
- 根节点可从 `html` 切到 `main` 再切回。切换候选时不得解绑始终启用的 `document.documentElement` 尺寸观察。
- UI 与一次性截图 worker 复用同一个 Element 选择器。截图仍沿原静态动画审阅规则，用户页面不参与自动化测试。

## 实施范围

仅改共享主体选择器、现有测量脚本的观察目标切换及对应测试。宿主整像素包围、固定参考视口、原始 HTML bytes、provider prefix、模型上下文、工具执行、持久 schema/event/relation/job 均不改。

本次不修改提示词，不修改 test8 的 HTML，不处理 HTML 文件链接导航。已有其他未提交修复不属于本次扩展。

## 验证与验收

自动化使用独立 headless Chromium 合成页面，不访问用户 Chrome 或 test8 实际页面：

- 普通 `div` 的 body/container padding、margin 和 margin 折叠；显式底部 spacer；`min-height:100vh`。
- 非零 `html` 外边距、多个顶层元素、带外部脚注的 `main`、脱流及固定元素、伪元素的保守整页行为。
- 同一 iframe 的 details 展开/收起、内容追加、超视口滚动及输入状态保留；`html → main → html` 后继续尺寸观察。
- 截图尺寸包含完整自然留白；原显式根、`main`、动画、hover 防振荡、图集隐藏和视口变化回归。

自动化结果：`test_visualization_browser_isolation.py`、`test_inline_visualization.py`、`test_visualization_preview.py` 共 33 项通过；前端图集和几何共 22 项通过。随后加强观察器回归并重跑普通 div 两项通过：鼠标移出 iframe 后，通过无 DOM mutation 的 WAAPI 祖先 padding 动画检查中间布局报告；故意移除 documentElement 解绑保护的内存变体会在该断言失败，证明用例能检出此回归。该实验不修改生产文件。

TypeScript、前端测量脚本 ESLint、共享 JS 语法检查、Ruff 和 diff check 通过；静态构建已更新。只有既有大 chunk 构建提示及一条 aiohttp 弃用警告。独立代理复核未发现阻塞问题，margin 与观察器建议均已处理。

用户刷新本地测试页，自行使用 test8 验证视觉效果并反馈。本轮没有操作用户浏览器、访问 test8 实际页面或运行真实 provider。
