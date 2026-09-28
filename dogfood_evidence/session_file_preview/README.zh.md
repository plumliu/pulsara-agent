# 会话文件链接与预览验收

日期：2026-09-28。规格：[会话文件链接与预览](../../PULSARA_SESSION_FILE_LINK_PREVIEW_IMPLEMENTATION_SPEC.zh.md)。本轮使用现有已完成会话，没有向模型发送新消息或调用 render。

## 自动回归

```sh
.venv/bin/pytest -q tests/test_session_file_preview.py tests/test_local_web_browser_bridge.py tests/test_local_web_http_surface.py
cd frontend
npm test -- app/pulsara-app.test.tsx components/file-preview-dialog.test.tsx components/workbench-view.test.tsx components/markdown-body.test.tsx components/task-workspace.test.tsx lib/runtime-adapter.test.ts
npx tsc --noEmit
npm run build:local
```

后端 33 项、前端 277 项通过。收尾修改后另跑预览后端 8 项、前端 8 项，全部通过。相关 ESLint、Ruff 和 `git diff --check` 通过；构建保留现有大 chunk 提示，后端保留 aiohttp shutdown_timeout 弃用提示。

测试涵盖 saved cwd 隔离、单次路径解码、UTF-8 分页重组、CSV BOM/多行记录/页重试/超预算降级、失败后不跳行、FD 绑定与文件替换、符号链接、跨连接/Origin/旧 token 拒绝、并发 Range、原始下载、修改文件后的关闭、普通流撤销与下载继续、慢打开竞争、前端迟到结果/owner 切换、嵌套链接失败重试、源码切换、超宽表格末尾列可达。

## 实际会话与视觉

会话 `c3c88c2c`，保存目录 `/Users/plumliu/Desktop/little_snake`，验收前后均为 170 条记录。点击该会话的 12 个真实 Markdown 链接：

| 文件（均在 customer_behavior/analysis_output 下） | 结果 |
| --- | --- |
| report.md | Markdown 阅读、源文本往返切换 |
| analyze_customer_behavior.py | 源码与行号 |
| segment_summary.csv、refund_summary.csv、model_selection.csv、association_tests.csv、data_quality_checks.csv | 表格 |
| run_manifest.json | 忠实源文本 |
| views/01_cluster_structure.html | PCA 散点与群体规模 |
| views/02_purchase_profiles.html | 消费箱线图与品类占比 |
| views/03_tenure_activity_channels.html | 注册时长、渠道与活跃度 |
| views/04_refund_profiles.html | 退款比例、金额率与原因 |

四张 HTML 的桌面截图保存在本目录。文件弹窗没有为这些链接增加可视化订阅。

- 桌面 1500×1000，原 canonical 卡片展开、收起、Enter 再展开保留同一个 iframe，采样宽度 770px；截图 `canonical-expanded.png`。
- 390×844：弹窗为 x=6、y=10、378×824，文档根无横向溢出。Escape 后焦点精确回到“分析报告”链接；先退出原生模态再恢复焦点，避免 inert 拦截。截图 `narrow-report-final.png`、`narrow-html-final.png`。
- HTML 文件原作者的响应式布局照原样执行。当前 Plotly 多面板报告在 390px 有标题挤压；这是文件自己的布局，预览器没有重写其图表。桌面四份报告正常显示。

## Chromium 与原生 macOS Safari

使用临时 loopback fixture server，挂载生产 `FilePreviewHttp` / `LocalBrowserBridge`、临时 saved-session 目录和生产 `htmlPreviewShell`。fake session 只替代目录来源，不替代文件读取、CSP、sandbox 或资源路由。Safari 通过普通页面运行同一 probe 并回报结果，未启用 Remote Automation，也没有改安全设置。

具体输入：

- canonical：内联脚本将标记主体宽度改为 237px；观察生产几何消息。
- local：HTML 加载同目录 CSS、CSS 图片/字体、本地 ESM，读取 JSON 值 237，检查图片解码与 CSS 颜色，观察有限几何消息。
- 两种模式分别执行 fetch、图片请求、脚本自身导航、top 导航、parent 导航、popup、form、anchor、meta refresh、storage/main DOM 访问，共 10 类。每个脚本先发出几何执行标记，确认攻击内容确实运行；服务端检查预览资源范围外的 `/blocked` / `/api/leak` 是否收到请求。
- 自包含真实 Plotly 文档运行后检查 plot 数据系列存在。

[Chromium 结果](chrome-browser.json) 与 [Safari 结果](safari-browser.json) 各 7 组全通过；两种装载均 10/10 执行，越界请求为零。资源测试完全本地，不依赖 CDN 缓存。

另真实点击临时资源链接进行顶层导航（非 fetch 模拟），Chromium 和 Safari 均显示“请在 Pulsara 文件预览中打开此页面。”；[请求记录](direct-navigation.json) 确认为 `Sec-Fetch-Dest: document`、403。记录未保留 token URL。

Safari 最初因入口继承的 `'self'` 在 opaque frame 重新解释而拒绝本地装载；最终入口仅允许 loopback `/api/file-previews/`，可信外壳进一步限定当前 origin/token。canonical 外壳保持 `frame-src 'none'`。没有放开主应用 API 或 canonical 外部资源。

## Critic 交叉核验

`file_preview_feasibility_critic` 审阅实现，先报告五项具体问题：Markdown 源文本切换、CSV 失败后跳记录、变化文件无法撤销、普通流在关闭后继续、嵌套链接重试依赖过期基准。修复后全部有回归覆盖。

critic 复核上述修复、Safari 策略、两种装载的实际攻击执行数和真实顶层导航；独立重跑预览后端 8 项及前端 8 项，结论为无剩余必须修复项。最后另修复浏览器实测的原生模态焦点恢复时机，并再次验证。

## 已知产品边界

UTF-8 分页；大 Markdown 使用源文分页；超出单条 CSV 解析/编码预算时明确降级，原始下载不限制文件总量；PDF 取决于浏览器查看器；Office 下载或系统应用打开；HTML 仅限自包含内容或当前目录内的静态依赖，资源不构成事务快照。未增加 provider 输入、canonical row/event/job、文件复制或持久恢复机制。

## 连续滚动白屏修复（2026-09-28）

用户反馈拖动 CSV 横向滚动条后整个页面空白。新增批量滚动测试和实际 Chromium 均复现 `Cannot read properties of null (reading 'scrollTop')`，应用及弹窗节点随未捕获渲染异常卸载。根因是 `onScroll` 的状态 updater 延后读取 React 事件的 `currentTarget`，事件分发结束后该字段已清空。

改为在事件回调内同步获取 `scrollTop` / `scrollLeft` 数值，再交给状态 updater。新增测试同时覆盖连续横向/纵向位置变化、可见单元格更新、会话保留及正常关闭。预览测试现为 **9 项通过**，TypeScript、相关 ESLint 和本地构建通过。实际会话反复左右滚动，位置依次为 500、1000、1500、800、1700、200、800、200，没有页面异常，关闭/重新打开正常。

直接拖动原生底部滚动条向右再向左，实际 `scrollLeft` 为 1610 → 257，页面异常为零，主导航仍保留。截图：`scroll-fixed.png`、`scrollbar-drag-fixed.png`。新静态包已构建并由运行中的服务提供；已白屏的旧页面需要刷新载入修复。

## 表格字号与源码入口（2026-09-28）

CSV/TSV 字号从 12px 调整为 14px；Markdown、CSV/TSV、HTML 的源码切换统一放到窗口右上角的 Code2 图标，移除原独立工具行。普通文本、代码和 JSON 直接展示源文。仅需分页时出现底部分页区。HTML 源码按原有 UTF-8 页预算读取、作为纯文本展示，切回预览保持同一个 iframe。

预览前端 11 项、后端 8 项通过，类型检查、相关 ESLint/Ruff 和本地构建通过。实际 CSV 的计算字号为 14px、源码按钮位于 header、单页内容无 pagebar；CSV/Markdown 双向切换、滚动、HTML 源码分页及 iframe 保留均已在浏览器验证。390px 窄屏无弹窗横向溢出。截图见 `csv-toolbar-final.png`、`csv-toolbar-narrow.png` 和 `html-source-toolbar.png`。
