# HTML 本地资源与发布冻结实施规格

日期：2026-10-14。状态：**已实施，相邻测试与代码审阅通过**。代码基线：`979abcf6`。经主代理与 GPT‑6 Astra high reviewer 交叉审阅，路径、CSS/JS 冻结、沙箱与回看边界已闭合。规格冻结阶段未修改代码、配置或数据库；实施结果见第 7 节。本篇由原本地图片规格扩展，取代其仅允许图片外置的范围。

## 1 目标：支持经典 HTML + CSS + JS 文件组织

模型可以使用普通 HTML 入门页面的文件结构，无需框架、构建器或手工打包：

```text
page/
  index.html
  css/style.css
  js/script.js
  images/logo.png
```

```html
<link rel="stylesheet" href="./css/style.css">
<img src="./images/logo.png" alt="Logo">
<button id="toggle">切换配色</button>
<script src="./js/script.js"></script>
```

runtime 在发布时读取静态本地依赖，将内容冻结进一个自包含 HTML。**源文件可以分开组织，发布结果仍自包含。** 模型不必编写 base64 转换脚本，也不必先把 CSS、JS 合并进 HTML。

沿用[统一展示规格](archived_docs/PULSARA_UNIFIED_VISUALIZATION_PRESENTATION_SPEC.zh.md)的消息提交、图集、历史、fork、GC 与前缀连续性。`view_image(HTML)` 复用同一准备规则；图片直接发布及 `image:` 引用读取不变。

## 2 首版支持范围与路径

| 入口 | 支持内容 |
| --- | --- |
| HTML `<img src>` | 本地 PNG、JPEG、静态 WebP，复用现有实际解码校验 |
| HTML `<link rel="stylesheet" href>` | 本地 UTF-8 CSS；保留位置、media 等普通样式表属性 |
| HTML `<script src>` | 本地 UTF-8 经典 JavaScript，支持无 async/defer、defer、async；由浏览器维持各自加载与执行规则，不自行调度 |
| CSS `url(...)` | 本地静态图片，包括外链样式表、HTML `<style>` 和 style 属性中的背景图等 URL；复用同一图片校验 |
| 已有内嵌内容 | 内联 CSS/JS、data URL、内联 SVG 继续可用，不要求统一改写 |

本轮不做完整站点打包，不递归处理 module graph、样式导入、外置字体或动态依赖，也不获取远程资源。静态识别边界固定如下，不承诺发现整页的所有依赖：

- HTML 只冻结表中的入口，stylesheet 按 `rel` token 识别。这些入口的 `data:` 原样保留；非本地、非 data 地址明确失败。`script type="module"` 的非 data 外链明确失败；内联 module 不扫描源码，非可执行 script 数据块不当作 JS 文件入口。
- `srcset`、`picture/source`、其他 link rel、SVG href 及其他未列 HTML 资源机制不解析、不改写，原样交沙箱；不保证它们的本地依赖可用。已有纯 data 用法不因此被拒绝。
- CSS 只处理解析库识别出的 URL token / `url()`，普通本地 URL 按静态图片冻结；`data:` 和纯片段 `url(#id)` 原样保留。普通字符串不当 URL，包括 `image-set()` 中的字符串候选；其中真正的 `url()` 则沿用上述规则。
- 两个 CSS 语义例外：`@import` 的导入地址只保留 data，其他地址明确失败，不递归处理 data CSS；`@font-face` 的 src URL 只保留 data，其他地址明确失败，不送入图片解码。`local(...)` 和普通字体名不读取文件，保留浏览器行为。

普通链接、正文 URL、JSON 数据、注释和脚本字符串不属于资源依赖声明。不另造 srcset/image-set 解析器或通用依赖扫描器；上述未处理机制仍受既有沙箱政策约束，不向浏览器开放宿主文件或网络。

不解析或执行 JS 来猜测 `fetch()`、动态 import、`document.write()`、动态 src 等运行时资源；这些操作仍受沙箱限制，不承诺可用。READY 只表示上述约定入口的资源准备成功，不证明页面所有依赖已被发现，也不表示任意作者脚本均运行正确。已有自包含脚本不因字符串里出现 import/URL 就被正则误判为依赖。

相对路径基准按**声明资源的文件**确定：HTML 的 img/link/script 相对 HTML 所在目录；外链 CSS 的图片相对该 CSS 所在目录；HTML 内联 CSS 的图片相对 HTML 所在目录。例：`css/style.css` 中的 `url('../images/logo.png')` 指向 `page/images/logo.png`。各入口也支持当前宿主本地绝对路径，如 `/Users/example/logo.png`，不将它解释成 HTTP 站点根目录。

围栏里的 HTML 路径仍相对 workspace。每个源文件的相对基准使用其绝对访问地址所在目录，**在跟随该文件自身的符号链接之前确定**；实际文件解析与读取沿用原路径 owner，不新增 workspace-only 限制，也不扩大宿主读取权限。HTML/CSS 的缓存键须包括相对基准，不能仅按 realpath 合并来自不同目录的链接。

当前 `_resolve_read_path` 会立即调用 `.resolve()`，不能从返回值还原上述基准。实施时在原路径 owner 内分出“绝对访问路径”和“实际解析路径”两步，保留顶层工具路径既有的 home/macro 规则；不在可视化模块复制这套规则。依赖地址解码后先按声明文件目录组成绝对本地路径，再交给实际解析/读取步骤，不重新解释成 scheme、`~` 或 home macro。首版绝对路径遵循当前运行宿主的路径语法，不猜测其他操作系统的盘符或 UNC 地址。

HTML 实体和 CSS 转义由对应解析库处理，合法百分号编码由标准库解码一次。拒绝无效编码、NUL/控制字符和未编码的路径 query/fragment；文件名中的 `?`、`#` 须编码。`//host/…` 不是本地绝对路径；`file:`、`blob:`、`image:` 不新增资源解析支持。空/缺省 img src 不读取文件，声明了外链但没有有效地址的 CSS/JS 明确失败。`~` 不新增展开约定，`<base>` 不改变本地基准，保留 `base-uri 'none'`。

## 3 冻结与失败行为

1. 沿原入口 bounded-read UTF-8 HTML，并记录相对基准目录；解析静态依赖，不执行作者 JS。
2. 使用原 IO owner 读取本地 CSS/JS 和图片，CSS/JS 校验 UTF-8，图片复用 `HostPromptImageValidator.freeze_local_image`。先把 CSS 内的图片 URL 冻结为 data URL，再冻结 CSS 本体。
3. 将支持的资源地址替换为携带对应 MIME/charset 的 base64 data URL。**保留 link/script 元素及其位置，不把外链脚本简单改成内联脚本**，避免丢失 defer/async 的浏览器语义，也避免资源正文中的 `</script>`、`</style>` 截断 HTML。外链 JS 不重写源码，CSS 仅通过解析器改写资源 URL；不承诺脚本观察到的 `currentScript.src`、stylesheet href 仍是原文件地址。依赖源地址计算新资源属于未支持的动态加载。
4. 最终 UTF-8 HTML 校验后进入原 `FrozenVisualizationOccurrence.html` 和消息接受事务。blob 身份、digest、size 使用最终 bytes，提交确认和未知 ACK 重试使用同一冻结候选，不重新读路径。**磁盘源文件全部保持原样。**

只保存一个 HTML blob，不增加图片 owner、资源表、manifest、资源下载服务或依赖图数据库。内嵌图片不额外发布 `image:` 或加入历史恢复元数据。展示、截图缩略图、刷新和冷读取使用保存 bytes；fork、删除、GC 沿原 HTML owner 工作。源文件后来修改/删除不会改变旧展示，新回复重新发布才读取新版本。

调用内可缓存重复来源，重复元素及原顺序保留；缓存生命周期不跨准备调用。图片可按已解析路径复用 bytes，CSS/HTML 另区分基准目录。不承诺多个外部文件的事务快照，只保证最终接受内容不再漂移。

约定入口中缺文件、不可读、明确拒绝的依赖、无效地址/文本编码、图片解码失败或容量超界，使**对应整个 HTML 项 FAILED**，沿原错误载体说明来源和原因，不泄露资源正文或额外权限信息。同图集其他独立项目照常处理。带 integrity 的待改写外链首版明确不支持，不悄悄删除完整性属性；不为其他不支持属性伪造浏览器行为。

FAILED 不因文件修复自动转 READY。取消、整体 deadline、身份冲突、owner 损坏和提交错误沿原结算路径传播；不吞成普通项失败、不生成模型回执或额外轮次。作者运行时 JS 错误不新增反馈通道。

## 4 复用与实施边界

| 所有者 | 最小调整 |
| --- | --- |
| `conversation_kernel/visualization.py` | HTML 分支调用局部异步资源冻结帮助函数，返回 HTML bytes；补齐源文件基准和调用内缓存 |
| 原文件路径/读取 owner | 在原 owner 分开绝对访问路径与 realpath；复用普通文件检查与 bounded-read；必要时从 HTML reader 抽取共享 UTF-8 文本读取核心，允许空 CSS/JS，不伪装成 HTML 文件 |
| 原 runner IO / Host 图片校验器 | 原实例、原 deadline、原排队/取消/物理清理；不构造伪工具调用，不用 provider 视觉预算限制 UI 发布 |
| `tool_runtime.py` HTML 回看 | 截图前调用相同资源冻结函数，使用工具原物理 IO owner 与 deadline；保留视觉能力检查和截图 PNG 输出预算 |
| 前端沙箱 / 截图 worker | 同步在已有 script-src、style-src 增加 data:；截图端补齐与 UI 等价的隔离文档承载，并等待静态资源加载；保留原截图进程与裁切 owner |
| 存储 / 内容协议 / UI | 沿用 HTML bytes；不改 schema、protocol major、图集类型、裁切或消息生命周期 |

前端已有 `sandbox="allow-scripts"` 隔离文档；新增 data 脚本与原内联脚本使用相同权限，不获得 same-origin、宿主文件、网络、父页面权限。不开放 HTTP/file/blob 资源、不开放 connect/frame/worker 等能力；普通文件预览的动态资源 URL 与权限策略不变。

**截图端现状只有顶层文档 CSP 和网络 route 阻断，尚不等价于前端 iframe 沙箱。** 本轮补齐为与 UI HTML 分支等价的可信外壳和 `allow-scripts` 内层文档，由可信外壳阻止作者文档导航；在内层文档执行原根节点选择与截图。优先复用或抽取现有壳构造的最小共用部分，不扩成通用预览框架、不重做普通文件预览。保留一次性浏览器进程、网络 route 阻断、viewport、环境引导和超时杀进程清理。

截图在原 deadline 内等待目标内层文档及其普通静态资源完成 `load`，再沿用双 requestAnimationFrame；不能仅等待 DOMContentLoaded 就截图。等待必须对应已注入的作者文档，不能把初始空 iframe 的 load 当作完成。不开 network-idle、任意静默计时或作者脚本完成协议；lazy 图片、异步业务任务及动画不因此获得“全部完成”的承诺。UI 和截图都验证 data CSS/JS 加载以及隔离行为。

解析继续使用成熟库：HTML 采用 `html5lib` 的树与序列化 API，解析设置 `scripting=True` 对齐实际允许脚本的文档，CSS 采用 `tinycss2` 的规则/component-value 解析和序列化 API，新增普通依赖与锁文件。现有 Markdown parser 不负责 HTML 文档树；标准库 HTMLParser 只内建 script/style 原始文本处理，不自行补 HTML 上下文状态机。CSS 库负责转义、注释、嵌套 token 和重序列化；Pulsara 只识别上述资源入口及不支持的依赖，不实现 CSS 引擎或 JS parser/bundler。保留 script/style 正文语义和注释，不启用 sanitizer、空白压缩或样式清理；经改写的 CSS 不承诺原始转义、换行等逐字节一致。无资源需改写且无未支持依赖时返回原 HTML bytes。

改写后的内联 `<style>` 还须做 HTML raw-text 上下文安全处理：CSS 库可能把原转义恢复为字面的 `</style>`，须以 CSS 等价转义避免意外终止元素，不能对整段 CSS 使用 HTML 实体转义。style 属性由 HTML 属性序列化负责引号与实体；外链 CSS/JS 的 data 包装不把正文插入 raw-text。

API 依据：[html5lib](https://html5lib.readthedocs.io/en/latest/movingparts.html)、[tinycss2](https://doc.courtbouillon.org/tinycss2/stable/api_reference.html)。实施时检查所选版本真实 API，并以浏览器行为测试验证解析/序列化边界，不以库名代替证据。

源 HTML、CSS/JS 单次读取和最终 HTML 沿用 `MAXIMUM_BLOB_BYTES`，图片沿用现有 bytes/像素/格式上限。所有阶段共享原 deadline；解析、编码通过已有 IO 边界执行。逐步核算剩余容量，包括“图片进 CSS、CSS 再进 HTML”的两层编码膨胀与重复引用；有界构造/序列化并最终复核 UTF-8 总量，不先无限拼接后拒绝。不新增文件数量、历史总量、依赖深度或重试次数上限；首版没有递归模块/样式导入。

`view_image(HTML)` 与发布分别读取各自调用时刻的文件，不承诺跨时刻/不同视口像素一致。冻结资源不占用截图 PNG 的模型输出预算；截图本身仍受原预算。UI 缩略图只消费保存 HTML，不访问源文件。

## 5 模型说明

删除“CSS/JS 必须内联”和“模型用脚本把图片编码成 data URL”的强制指导。不在提示词中列举分文件 CSS/JS、加载属性或解析器知识；支持能力由 runtime 承接。沿用现有 HTML 制作段，只保留温和建议和必要图片路径语义：

> For simple visuals, consider a single HTML file. Local image paths may be absolute or relative to the file that references them. Use inline SVG for icons such as Lucide. Network resources are unavailable.

已有中立的宽度建议、图集发布语法、主题和动画指导保持。说明只在原获准前缀安装边界生效，不改写运行中 SYSTEM/tools。展示不要求先看图，不自动向模型发送资源 bytes 或渲染状态。

同步修改 `ports/system_prompt.py` 与 bundled skill 的 `pulsara-data-analysis/references/visualization-render.md` 中相同的旧内联限制；后者需区分“围栏路径相对 workspace”和“HTML/CSS 内资源相对声明文件”，避免两份模型说明互相冲突。

## 6 验收

- **基础页面**：独立 HTML/CSS/经典 JS；CSS 生效、图片显示、按钮可切换配色；多个同步脚本依序执行，defer 在文档解析后依序执行，async 不被强行改成同步；内联脚本与外链脚本交错正常。交互行为由真实浏览器验证。
- **路径与解析**：HTML 与 CSS 各自相对基准、绝对路径、中文/空格/编码字符、不同 cwd；HTML/CSS 自身符号链接及同一目标从不同目录访问，确认缓存不混用基准；解码一次后不再展开 scheme/home macro。CSS 背景图片与嵌套 media 中的 URL；注释、字符串、textarea 等伪标签/伪 URL 不读取文件；data URL、内联 SVG、无外部资源页面保持。
- **识别边界**：非 data module 外链、CSS import、外置字体明确失败；data import/font/srcset 保持，未列 HTML 机制不读取文件；image-set 字符串不误读，真正 url() 正常冻结；非可执行 script 数据块不误读。
- **重序列化**：含本地背景图的内联 style 触发改写时，`content:"\3c /style>"` 仍显示原 CSS 内容且不截断元素；style 属性的引号/实体正确；启用脚本语义下 noscript 中的伪依赖不被读取。
- **失败与沙箱**：缺 CSS/JS/图片、非法编码、损坏图片、超界及约定入口的未支持依赖明确失败；同组其他项正常。`</script>`/`</style>` 字面内容不会逃出资源边界；UI/截图的 data 脚本与样式可用，网络、宿主 DOM/API、file 读取以及作者导航、弹窗仍被隔离。截图确实等待目标作者文档的静态 load 和 async 脚本，不能以空 iframe 的 load 或 DOMContentLoaded 提前完成；超时仍由原 owner 清理。
- **保存与清理**：冻结后改/删源文件，提交确认、刷新、冷加载、缩略图、fork 仍完整；原 blob 冲突/回滚和 GC 断言保留；取消/超时完成物理清理，无半提交、无额外资源 owner。
- **回看与实测**：相邻 Python、截图、沙箱和前端回归通过；真实 provider 在 hello 测试子目录按上述基础结构生成页面，直接引用相对/绝对资源，不手工编码。验证按钮、图片、CSS 背景与 `view_image(HTML)`，删除测试副本的依赖后旧展示仍完整。ROOT/worker 复用同一准备入口。纯文本 provider 暂不补测，不默认重置数据库。

完成时记录实际验证及限制。除普通解析库和局部冻结适配外，不新增服务、注册表、事件、后台任务、资源代理、兼容分支或模型反馈机制。


## 7 实施与验证记录（2026-10-14）

已落地共用的 HTML 资源冻结入口，发布与 `view_image(HTML)` 使用同一解析规则；保存仍为单个 HTML blob，现有消息事务、fork、历史读取与 GC 未增加资源 owner。前端与截图 worker 共用沙箱文档构造，普通文件预览保留原有权限政策。模型说明和 bundled 指南已同步为温和建议。

- 相邻 Python 测试 **161 passed**：`test_html_local_resources.py`、`test_inline_visualization.py`、`test_visualization_preview.py`、`test_unified_visualization.py`、`test_visualization_browser_isolation.py`、`test_local_image_tool_executor.py`、`test_content_revision_line_edit.py`。覆盖真实 Chromium 交互与隔离、外置资源删除后提交/历史/fork/GC、工具回看、容量与取消清理；存在一条原有 aiohttp shutdown_timeout 弃用警告。
- 前端 `inline-visualization`、`file-preview-dialog`、`visualization-frame` 三组测试 **32 passed**；TypeScript 检查、变更 Python 的 Ruff 检查及 diff 空白检查通过。
- 同一 GPT‑6 Astra high reviewer 完成代码审阅。发现并闭合外链 CSS UTF-8 BOM、旧式 script language 分类两项 P2；reviewer 独立运行对应回归 **4 passed**，最终无剩余阻塞项。
- 随后按用户追加请求完成真实 provider 实测：使用保存配置的 DeepSeek V4.1 Flash（high），新建 `HTML · 本地资源冻结实测` 会话，在独立快速开始工作目录复制 hello 图片。模型直接生成独立 `index.html`、`css/style.css`、`js/script.js`，使用 HTML 相对/本地绝对图片路径及 CSS 相对背景图，没有手工编码资源；多次 `view_image(HTML)` 回看后发布 HTML＋图片图集。前端加载正常，用户和执行者都验证了配色按钮。临时移走生成的 CSS/JS/图片目录并刷新后，两张图仍完整加载（1254px），保存的 CSS/JS 为冻结 data 资源，绝对路径回退未触发；随后恢复测试目录。保存的生产配置未改写，数据库未重置，保留旧会话。纯文本 provider 未测。
- 实测另发现既有布局问题：宽屏下配色过渡会触发 `visualization-frame.ts` 的永久 `automaticPage` 退化；动画结束后仍保留整页高度。实测内容约 768.6px，宿主整页高度 1200px，额外空白约 431px。该问题属于裁切策略；用户随后要求修复，处理语义见 §8。

实现沿用原资源边界；HTML/CSS 解析采用 html5lib/tinycss2。tinycss2 的公开 serialize API 先收集全部片段再拼接，因此局部适配使用该库现有 writer primitive，在序列化期间执行同一 blob 容量边界，不复制语法或序列化器。

## 8 实测后的裁切修订（2026-10-14）

用户实测确认“动画期间展开、结束后恢复”仍会造成 hover 跳动，要求去掉动画触发整页的逻辑。本节替代归档内联可视化规格 §7.2 的动画退化规则，资源冻结和模型契约不变。

- 正文自动裁切只跟随实际内容布局，不因为存在运行/待启动动画切到整页。配色、阴影及主体内部的 hover 动效，只要内容边界仍合法，整个交互期间都保持裁切；不增加动画属性分类表、延时阈值、逐帧轮询或宿主高度过渡。
- 真正的交互越界仍锁定当前视口尺寸下的整页模式，保留避免 hover 伸缩振荡的机制。iframe 的宽或高改变时清除旧尺寸的裁切/越界记录和待处理交互，按新视口重新测量；放大、缩小及响应式布局不会继承旧尺寸的锁定。保留同一个 iframe 和作者交互状态，不通过重载恢复裁切。主体内部真正的内容展开则由原 ResizeObserver、MutationObserver 和 RAF 测量更新实际高度；显式根标记保持原规则。
- CSS animation/transition 启停/取消事件仍可触发正常边界重测，但事件本身不决定展示模式。任意不触发现有观察/事件的 JS/WAAPI 绘制变化仍可能在下一次测量才更新，不新增动画调度器。截图入口的静态审阅规则保持原样。
- 自动化回归必须检查完整测量轨迹，确保连续配色、hover 进入/离开、过渡取消期间没有临时整页；同时覆盖真实内容展开、动画完成后实际越界、悬浮增长不振荡和显式根行为。
- 图集切换中不可见的文档不参与交互越界判定，避免隐藏导致的根节点缺失误锁整页。放大后的短视口滚动、缩小回正文、隐藏再显示驻留图集的路径必须恢复合法主体裁切，并保留作者输入状态；同一视口内真实越界的防振荡保护仍有效。

前一轮验证仅证明动画结束后恢复高度，不作为本轮“交互全程高度稳定”的证据。本轮按用户要求仅运行自动化测试，不进行实机或真实 provider 实测。

本轮验证：前端脚本真实 Chromium 自动化回归 11 passed，前端图集/几何 21 passed；TypeScript、Ruff、diff 检查及构建通过。测量轨迹断言覆盖 hover 进入/离开、连续配色切换和过渡取消全程保持裁切，以及实际内容展开和越界保护。构建产物已更新到原测试服务的静态目录，等待用户刷新后自行实机验证；未执行实机或真实 provider 测试。

随后用户报告放大再缩小、切换缩略图后仍保留留白。新增 Chromium 用例先复现旧视口锁定，再复现隐藏文档误判；修复后 12 passed，前端图集/几何 21 passed，TypeScript 与构建通过。回归确认短视口滚动后返回正文恢复裁切、隐藏再显示恢复裁切且保留已编辑输入，以及同尺寸真实越界仍不振荡。沿用用户自行实机验证的安排，未重跑 provider dogfood。
