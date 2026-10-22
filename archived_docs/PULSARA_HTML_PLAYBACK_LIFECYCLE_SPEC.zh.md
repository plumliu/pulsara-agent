# HTML 可视化宿主播放生命周期

状态：已完成并归档。方案冻结于 2026-10-22，归档于 2026-10-23。

用户要求：离屏暂停、回来继续，尽量保留交互状态，由宿主负责，不依赖模型生成的 HTML 配合。三位 GPT-6 Astra high 审查者分别审查平台能力、展示生命周期与调度语义，交叉讨论后采用本方案。本文替代旧 inline visualization 规格中“宿主不拦截动画调度、依赖作者订阅 visible 暂停”的部分；其余保存、授权、安全和布局合同不变。

## 产品行为

- 只有实际进入当前展示视口的 HTML 才首次读取并创建执行文档。读取结束时已经离屏的，等待再次显示后首次挂载。
- 已挂载页面离屏、切为非当前图集页、应用文档隐藏时暂停受控播放；返回使用同一个 iframe，保留 DOM、JS 对象、表单、内部滚动和画布。
- 可见性使用图集激活状态、正面积视口相交及 document.visibilityState；不按窗口焦点暂停，不使用历史预读的 200px 余量。
- 恢复后动画以正常速度继续；不追赶离屏时长，不补跑 interval 次数。用户/作者主动暂停或取消的动画不能被恢复操作复活。
- 非当前驻留页保留内部参考视口与执行文档，外层退出布局且不可见、不可交互，不使用 display:none 销毁 CSS 动画，也不能由隐藏长页撑大滚动范围。
- 暂停不是永久保存。沿用每图集当前页与最近两页的三个 HTML 执行实例预算，以及现有历史卸载规则。HTML 一旦卸载即不保存交互状态，重新打开时从已保存的 HTML 初始化；淘汰、切会话、切工作区标签或关闭应用导致卸载时均遵守此规则。不新建序列化或持久恢复协议。
- 工作区“打开保存的可视化副本”沿用独立实例语义，不与内联图自动同步状态。本轮不重新设计工作区实例所有权。

## 所有权与平台选择

Chromium 继续拥有真实事件循环、定时任务执行、动画对象和渲染；Pulsara 只管理已有内嵌文档的播放许可，并在隐藏期间保留受控回调及剩余延迟。复用双层 sandbox、环境消息及 bootstrap；不添加 renderer 全局调度服务、后台进程、数据库字段、事件、摘要或回执。

已检查本地 Electron 44.9.0 类型及官方 API：WebFrameMain 没有单 iframe freeze。CDP Page.setWebLifecycleState 没有 frameId，Chromium PageHandler 经 GetWebContentsForTopLevelActiveFrame 冻结整个 WebContents，会影响整个工作台；Emulation 虚拟时间也不是单图生产播放开关。每图迁成独立 WebContentsView 将改变原生布局、裁剪、焦点和安全所有权，超出本次最小需求。因此使用注入的窄播放适配，不能称作任意 JS 的进程冻结。

参考：[Electron WebFrameMain](https://www.electronjs.org/docs/latest/api/web-frame-main)、[Chromium PageHandler](https://github.com/chromium/chromium/blob/main/content/browser/devtools/protocol/page_handler.cc)、[HTML timers](https://html.spec.whatwg.org/multipage/timers-and-user-prompts.html#timers)、[Web Animations](https://www.w3.org/TR/web-animations-1/)。

## 播放适配合同

仅 live 内嵌可视化与工作区保存副本在作者脚本之前安装适配。URL 文件预览、外部网页、HTML 截图 worker 保持原行为。冻结 HTML 字节、canonical occurrence、provider 已安装前缀完全不变。

1. rAF：隐藏时保留未取消请求，返回后各执行一次；递归请求仍交给原生 rAF。取消有效，同一原生帧的时间戳一致，一个回调异常不阻断其他回调。
2. 函数型 timeout/interval：隐藏时取消物理等待并保存剩余延迟，恢复重新交给原生 timer；保留参数、window this、交叉 clear 和回调内取消语义。字符串 handler 仍由原生 CSP 拒绝，不通过 eval 绕过。延迟沿浏览器数值转换，不新增产品总时长/调用数上限。
3. rAF 时间戳与 performance.now 共用扣除宿主隐藏时长的活动钟；重复环境消息不重复扣时间。Date 保持真实墙钟。
4. CSS animations、CSS transitions、普通 document-timeline WAAPI 由 document.getAnimations 统一发现。CSSAnimation 使用原生 playbackRate=0 暂停时间推进并保持 currentTime，恢复作者原速率；不能调用 pause/play 永久夺走 CSS animation-play-state 的后续控制权。其余普通动画使用原生 Animation.pause/play，暂停后设置捕获的 currentTime 同步完成 pending pause。不能依赖下一渲染帧完成暂停：真实离屏 frame 会被 Chromium 跳过，异步 updatePlaybackRate/pause 可一直 pending，内部时间仍在走。只恢复被宿主暂停、作者仍希望播放的对象；适配常用 play/pause/cancel/finish/reverse、CSS 动画速率修改与 Element.animate，隐藏期间通过 DOM 变化和动画开始事件发现新增动画，不做轮询或自建动画引擎。元素临时移出再放回 DOM 不等于作者取消，不遗忘宿主暂停所有权。
5. SVG SMIL 使用原生 pauseAnimations/unpauseAnimations，保留作者原有暂停意图。
6. 环境仍由宿主经可信外壳传入，内层仅接受 parent；可见性变化不替换 srcdoc 或 key，不放宽 CSP、网络或 allow-same-origin。

## 明确边界

- 不是任意 JS 冻结或零 CPU 保证：同步工作、Promise/microtask、postMessage/MessageChannel、Observer/其他事件、主动替换宿主包装的脚本不在停止保证内。
- Date 驱动或混用 document.timeline.currentTime、Event.timeStamp、原生 performance entries 与活动钟的脚本仍可能按真实时间推进；不篡改全部平台时间域来伪装透明冻结。
- 动态图片、WebAudio、自定义/滚动 timeline、直接重设 Animation.startTime 等非常规入口，以及隐藏期间在未被观察的 shadow tree 新建 CSS/SMIL 动画，不承诺透明暂停；不为这些边界构建通用浏览器替代层。
- 可见且自带无限重绘循环的 HTML 仍正常运行。宿主不能可靠推断“画面已经没有意义地静止”，不会擅自降低可见图的帧率。
- 不新增全会话 iframe 数量上限，不承诺 heap 严格有界；本次通过延后初始化与暂停离屏播放削减无效工作。历史可见/选区/焦点的连续阅读区间不得因 HTML 优化中间断开。

## 验收

- 原生 Chromium 验证无环境订阅 HTML 的 rAF/timer 计数、同帧时间、取消、参数/this、异常隔离、剩余延迟与多轮暂停恢复。
- CSS/WAAPI/SMIL 相位和作者暂停/取消保留，初始隐藏、隐藏时新增动画；非当前图集布局不销毁 CSS 动画。
- 真实会话 frzaaaxz 的八份冻结 HTML 只读加载作本地复验，至少两版最速降线与投针验证离屏停止绘制/采样、回来继续、控件状态保留；不修改生产保存内容，不提交用户 HTML。
- 首次离屏不读取、不挂载；读取途中离屏延迟挂载；已挂载返回 iframe identity、srcdoc、输入与内部滚动保持。
- 多图互不暂停、快速滚动/图集切换、主界面正常运行、已有三个实例淘汰规则、截图与文件预览隔离、伪造非 parent 环境消息无效。
- 聚焦前端回归、TypeScript、真实浏览器及现有隔离/截图测试通过后，记录结果。不为验证功能重新生成 HTML 或修改生产模型配置。

## 实施与验证记录

- 核心播放适配、环境接线、首次可见懒加载与隐藏页布局已实施。三位审查者完成交叉审计及最终复核，冻结范围内无剩余阻塞。
- 12 项新增真实 Chromium 播放回归通过；连同现有隔离、布局、inline、preview、统一可视化回归，共 49 项 Python 聚焦测试通过。前端 inline/environment/workbench/task-workspace 共 202 项聚焦回归通过；TypeScript、相关 ESLint、Ruff、diff 检查及生产构建通过（构建输出在临时目录，不改已发布包）。
- 浏览器验证发现并修正：CSS pause/play 夺走作者后续 CSS 控制权；临时 detach 导致暂停所有权遗失；真正离屏后 pending 动画操作不完成；隐藏长页仍撑大滚动范围。分别增加行为回归。已有 hover 验证按始终启用动画的新合同等待真实过渡完成，仍断言最终 opacity=1。
- 真实 React/IntersectionObserver 端到端复验：第二张离屏图未提前读取；第一张滚出视口后，回调计数 19→19、CSS currentTime 299.986→299.986，300ms 内保持；滚回后回调继续到 31，原动画对象和输入值保持，未重新读取。
- 只读复验 frzaaaxz 的全部八份已保存 HTML，无运行错误。450ms 暂停期间，投针绘制计数 24→24、恢复到 33；两版最速降线分别 22→22、恢复到 31。控件值和 JS 对象保持，恢复首帧不包含隐藏时间。其余五份静态图正常显示；未修改生产保存内容、配置或会话。
- 本地证据位于 `output/html-playback-20261022/`：`real-html-results.json`、`react-integration.json` 与截图，不提交用户 HTML。临时 Vite 验证页面与服务器已清理。
