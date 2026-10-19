# Pulsara 网页标签实现方案

日期：2026-10-19

状态：已实施 WebContentsView 网页标签，完成 GPT-6 Astra high 子代理交叉检查及当前 macOS / Electron 44.9.0 打包应用验收。`open_in_pulsara` 工具尚未实施。Linux 图形桌面、真实输入法组合输入和多显示器 DPI 切换尚未实测，不以 macOS 结果代替这些覆盖。

本次验证：前端 913 项测试、桌面 15 项测试、生产网页 owner 的 11 项真实 Electron 集成检查通过。打包应用中已检查原生鼠标和键盘输入、切换会话/设置及收起保活、拖动收起、快捷键连续输入、页面导航和弹出标签、关闭最后一个标签、外部 HTTPS，以及既有 HTML → Markdown → 后退。验收中发现并修复了快捷键后输入落入网页、弹出标签地址空白的问题；消息监听在布局提交时安装，避免文件预览首次消息的监听空窗。

本地证据：`output/playwright/browser-tab-acceptance/` 与 `output/playwright/browser-owner-integration/`。生产改动限于桌面宿主和界面；未改 Python runtime、工具注册、模型输入前缀或执行/结算路径。当前使用隐藏原生页后的普通背景过渡，没有引入未使用的截图接口。

## 1. 目标与范围

在会话右侧工作区中增加网页标签，用户可以浏览网站与本地开发服务器，保留页面导航、表单和滚动状态。沿用 Pulsara 的暖色界面、实例标签栏、侧栏宽度和展开/收起动画；能力、任务、后台进程三个顶层入口不变，在顶部入口栏的“后台进程”后增加“新建网页”按钮，下方实例标签栏只显示已打开的内容。

首版支持 HTTP(S)、地址输入、前进、后退、刷新/停止、页面标题、加载错误重试、复制地址和主动在系统浏览器打开。网页属于打开它的会话，切换标签、切换会话、进入设置或收起侧栏不销毁网页实例。

本地文件仍由现有文件浏览器负责，包含 test8 的 HTML → Markdown 跨文件浏览；冻结可视化仍保持发布快照语义。网页标签不接收磁盘路径、文件预览 token 或 `image:` 引用，不把现有沙盒 HTML 改为可任意联网的网页。

本次不增加模型工具、模型浏览/操作能力、对外页面截图接口、远程调试端口、下载管理、浏览器扩展、账号管理、权限授权面板或浏览记录产品。仅允许为宿主遮挡及必要的布局/拖动过渡按需生成进程内临时截图背景，不保存、导出或送入模型。网页内容不进入模型输入。完成本功能后再修订并实施 [open_in_pulsara 方案](PULSARA_OPEN_IN_PULSARA_FILE_IMPLEMENTATION_PLAN.zh.md)。

## 2. 已核对的实现与复用边界

### 2.1 Pulsara

| 现有 owner | 当前行为 | 本次复用/修改 |
| --- | --- | --- |
| `frontend/components/desktop-workspace.tsx` | 会话标签、选中项、关闭回调和宽度；仅挂载选中标签正文 | 增加 browser 类型及网页显示槽位，复用 open/activate/close/forgetSession；原生实例由主进程保活 |
| `frontend/app/pulsara-app.tsx` | Provider 跨会话存活，但 InspectorPanel 在页面/会话加载条件变化时卸载 | 在跨页面存活的现有 owner 中连接网页身份与可见性；槽位卸载只隐藏原生页，不关闭它 |
| `frontend/components/inspector-panel.tsx` | 三个顶层入口与实例视图 | 保留入口和非网页视图现有加载方式 |
| `frontend/components/markdown-body.tsx` | 外部链接目前使用 `_blank` | 有明确会话归属的 HTTP(S) 点击进入网页打开入口 |
| `frontend/components/file-browser.tsx` | 本地文档包导航；外部地址调用系统浏览器 | 外部 HTTP(S) 用户导航交给同会话网页入口，文件导航保留 |
| `desktop/src/main.ts`、`preload.ts` | 顶层来源校验、窄 IPC、统一窗口生命周期；现禁用 webview | 保持 webviewTag=false 和拒绝 webview 挂载，增加主进程创建的 WebContentsView owner、受限命令及显示适配 |
| `desktop/src/ui-preferences.ts`、`frontend/lib/ui-preferences.ts` | 按会话保存侧栏展开与宽度 | 继续复用；本次不新增网页持久化格式 |
| `src/pulsara_agent/desktop_app/http_server.py` | 现有 Host/Origin/Sec-Fetch 检查 | 保持原合同；网页不能访问工作台控制服务，不靠 CORS 充当隔离 |

当前 Electron 固定版本为 `44.9.0`。已检查安装包类型声明中的 `WebContentsView`、`View.setBounds/setVisible`、导航事件、原生 history 以及权限 API；没有现成的 Pulsara 网页宿主可直接调用。

### 2.2 DSH 参考及载体取舍

参考本机 `/Users/plumliu/Desktop/python_workspace/deepseek-harness`，检查时 Git revision 为 `687f282e86`。关键源码：

- `apps/desktop/src/browser-guests.ts`：主进程批准 guest、隔离分区、权限和导航限制、清理。
- `packages/client/ui-sidebar-browser/src/client/index.ts`：桌面 browser 标签声明 keepMounted。
- `packages/client/ui-sidebar-browser/src/client/electron/ElectronWebviewPresentation.ts`：在稳定 DOM 中创建 webview，不随显示/隐藏搬移节点。
- `packages/client/ui-sidebar-browser/src/client/electron/ElectronWebViewImpl.ts`：观察 Chromium 导航、标题、加载及崩溃；使用原生历史。
- `.agents/notes/implemented/feature/2026-09-20-desktop-browser-webview.zh.md`：明确 DOM 重挂载会丢失 guest，隐藏和存储分区是不同生命周期。

DSH 的桌面路径使用 `<webview>`，其 Web iframe、插件框架、controller/provider 分层、workspace CWD 共享策略和跨重启恢复并非 Pulsara 的必要依赖，不复制。

**唯一网页载体采用 Electron WebContentsView，替代原 webview 选择。** 主进程创建并持有原生视图，直接加入所属窗口的 `contentView`；React 负责工具栏、标签和布局槽位。保持宿主禁用 webview，不保留双实现、兼容开关或 iframe 降级路径。DSH 的导航观察、分区与清理经验仍可参考，其稳定 DOM 挂载、allowpopups、will-attach/did-attach 及 bootstrap 认领协议不再适用。

选择依据是实测确认原生页面可以脱离 React 条件挂载保活，适合明确管理会话归属、显示和销毁；当前右锚侧栏动画也可适配。代价是坐标同步、原生层叠、焦点与快捷键不能由 DOM 自动解决。没有做 webview/WCV 的 CPU 或内存对照，不承诺更快或更省内存。官方参考：[WebContentsView](https://www.electronjs.org/docs/latest/api/web-contents-view)、[webview 风险说明](https://www.electronjs.org/docs/latest/api/webview-tag)。

Electron/Chromium 拥有网页进程、文档、网络、存储和导航历史。Pulsara 拥有会话/标签归属、主进程原生实例、布局可见性、资源清理、允许的地址与宿主权限边界；通过窄 IPC 衔接，不自建网页历史引擎、HTTP 代理、动画引擎或浏览器框架。

### 2.3 实验依据及未验证范围

2026-10-19 在 macOS arm64 / Electron 44.9.0 中进行了两组实验：独立生命周期/隔离 fixture，以及加载真实 Pulsara 前端与已有会话的隔离桌面窗口。实验窗口 UI 偏好只写内存，未修改生产配置或发送模型任务。当前能力页仅作为网页几何占位，最终 browser tab 尚未接入。

- 隐藏、原生视图移除再挂回、切换页面保留同一 WebContents、文档、表单、滚动与原生历史；真实 HTTPS 网站可显示。同会话存储共享、跨会话隔离，关光标签再打开仍保留分区数据。
- 原生父 View 设为100px宽、子网页459px宽时，子页仍完整绘制，父范围外点击仍能命中。不能用父 View bounds 或 DOM overflow 代替真实裁剪。
- 直接挂载当前右锚侧栏：收起时完整 viewport 随侧栏移出窗口，网页未触发 resize，滚动不变；分隔条避让后真实拖动、拉宽及拖到右侧自动收起正常。
- 宿主弹窗/窄屏遮罩会被原生网页盖住。搜索弹窗已验证隐藏网页、按需截图背景及焦点交接；人为延迟截图至弹窗关闭后，未安装迟到图片。该结果不代表全部弹层和异步竞态通过。
- 宿主 zoom=1.25、网页 zoom=1/1.2 分别验证；真实坐标点击和中文粘贴正常。网页聚焦时宿主 DOM 的 ⌘K 不触发，需要应用层路由。
- 生命周期实验24项通过，保留1项错误码诊断失败：显式 stop 产生 `ERR_FAILED (-2)` 而非预期的 `ERR_ABORTED (-3)`；独立验证停止生效且后续导航可恢复。不能把 -2 全部吞掉，也不能仅以 -3 判定用户停止。
- 分区请求过滤拦住导航、重定向、fetch、iframe、WebSocket、worker/Service Worker 对受保护端口的请求；页面关闭后存活的 Service Worker 延迟请求仍被退役分区拒绝。宿主重新 loadURL 不会自动销毁原生页。

本地实验材料位于 `output/playwright/wcv-layout/` 与 `output/playwright/wcv-lifecycle/`（临时产物，不作为生产依赖）。Linux、真实输入法组字、多屏 DPI、全部 toast/tooltip/弹层组合、完整异步竞态、TLS失败交互、原生文件选择与真实设备未完成验证；页面截图耗时的少量样本不构成性能基准。以上边界进入第8节验收，不能将编译或可行性实验写成产品集成完成。

## 3. 会话、标签与存储身份

### 3.1 标签

网页标签代表一次独立浏览过程，身份为现有会话 `sessionId` 加独立前端 `tabId`。URL 和标题可以改变，不能拿 URL 当永久 tab ID；网页标签也不绑定 runtime 的 `host_session_id` 或 connection generation。

- 点击“新建网页”：在已选定的真实会话下新建空白标签并聚焦地址栏；未选择会话时禁用，不暗中创建会话。
- 从聊天/文件预览打开 URL：只在来源会话中查找当前已提交 URL 完全匹配的网页标签。优先当前标签，其次现有标签顺序；命中则聚焦，不刷新。正在跳转的标签不凭旧 URL 命中。没有匹配则新建。
- 地址栏提交、网页内普通链接、页面重定向：导航当前网页标签，不改变会话归属。
- 明确“在新标签打开”：始终创建独立实例，不去重。关闭某一实例不关闭其他同 URL 实例。
- URL 经 WHATWG URL 解析后比较完整 href，保留路径大小写、查询参数和 fragment；不去 tracking 参数、不自动合并 HTTP/HTTPS、不用标题匹配。
- 标签标题来自实际页面 title；空标题使用 URL 主机名。作为普通文本渲染，不执行 HTML；地址栏显示实际已提交 URL，不用标题掩盖地址。

所有异步回调绑定稳定的网页实例记录/操作对象及已批准 guest。这里不能直接比较 `WorkspaceTab` 展示对象：现有 update 使用对象展开更新，标题变化就会产生新对象；稳定实例记录与可替换的展示字段必须分开。关闭/移除后使原操作失效，迟到的申请、导航、标题和 popup 回调不得重建标签或写入另一个会话。A → B → A 也必须验证原实例仍存活，不能只看字符串重新相等。复用现有操作对象/失效检查，不增加持久化 generation、通用实例注册框架或执行回执。

### 3.2 网页存储

首版采用**每个 Pulsara 会话一个独立的进程内 Electron Session 分区**。同会话网页共享该分区，跨会话不共享，即使工作目录相同也不共享；不继承工作台默认 Session、系统浏览器账号或文件预览资源。

主进程根据经过可信顶层 IPC 提交的会话身份分配随机、无 `persist:` 前缀的 partition。renderer 不得指定任意 partition 或选择默认分区。会话 ID 是展示归属，不是授权凭据；真正的宿主权限来自既有 trusted sender/main-frame 校验。

关闭单个网页释放 guest，但同会话分区保留到会话视图被 forget、工作台整页销毁/重载或应用退出，避免在当前工作台重新打开网页立即丢失登录态。会话被归档、删除或数据库重置时，沿用 forgetSession/clear 关闭全部所属 guest，清理该分区网站数据与 Service Worker；其他会话不受影响。内存分区由 Electron 管理，不承诺清理后进程内存立即归零。

不保存网页标签、URL、Cookie 或 history 到磁盘；应用重启/工作台整页重载后无网页标签恢复。已有侧栏宽度/开关偏好继续按原路径保存，新会话仍默认收起。这是首版范围；不照搬 DSH 的恢复检查点，不增加数据库表或同步机制。

## 4. 保活与布局

仅网页 guest 需要保活，不把所有任务、进程、MCP、文件视图都改成常驻组件或维持所有会话 runtime 连接。

### 4.1 实例与显示槽位

每个网页的 WebContentsView 由主进程 owner 持有；隐藏或从窗口移除后可重新挂回同一实例，不依赖 React 组件常驻。前端在现有跨页面存活的 workspace owner 范围内连接身份、命令与状态订阅；InspectorPanel/网页正文槽位卸载只撤销可见性，不等于关闭标签。只有显式 close/forget/clear 或宿主生命周期结束才释放实例。切回网页不重新 loadURL。

复用现有 WorkspacePane、工具栏和非网页 renderTab；网页正文提供一个测量槽位，不再为了保活把整个 InspectorPanel 提升为常驻壳，不建设第二套侧栏。只有当前可见会话中选中的网页可以显示。切到文件/MCP/其他标签、会话加载或设置时，先撤销旧原生页显示，再挂载目标内容；没有有效槽位时保持隐藏。显示通知携带所属实例及当前显示操作身份，迟到矩形不能让旧页面覆盖当前内容。

### 4.2 矩形、动画与拖拽

前端提供相对工作台 viewport 的实际槽位矩形；主进程用**宿主** zoom factor 将 CSS像素换算为窗口内容区 DIP，校验有限数值和所属窗口。不要再乘 devicePixelRatio，也不要用网页自身 zoom 换算宿主矩形。测量排除工具栏、标签栏及当前分隔条命中范围；当前条宽7px，应取实际布局，不能把实验常量当新布局权威。

沿用现有 CSS 布局与过渡，由 ResizeObserver、窗口/zoom变化和既有拖动/过渡状态触发同步；仅在动画或拖动期间按帧更新，结束/取消时补一次最终矩形并停止。不得直接复制实验中的永久 rAF 和全 DOM 扫描，也不另写插值引擎或让 CSS 与原生动画各自驱动不同进度。

当前右锚侧栏收起时，保留网页完整 viewport 宽度，按槽位位置向窗口右侧移出，利用**窗口边界**裁剪，结束后隐藏。展开执行逆过程。点击收起与拖至阈值后的收起共用该路径；不把网页逐帧压成几像素引发响应式重排。普通用户拉宽/缩窄及窗口 resize 则真实调整网页尺寸，允许正常响应式布局，保持同一实例。

该方式仅适用于当前右锚、移出窗口的布局。内部任意矩形裁剪不能依赖父 View 或 DOM overflow；若布局使原生页会越过宿主内容边界，使用实际受限 bounds 或在相应过渡期间隐藏并按需显示截图背景，不能让网页覆盖聊天、输入框或工具栏。快速反向展开、取消过渡和关闭标签时，以当前显示操作为准，不让旧动画结束回调重新显示网页。

分隔条区域始终留给宿主命中，拖动继续复用现有 pointer capture；必须用真实鼠标验证开始拖动、跨过网页继续拖动和释放。若目标平台原生视图仍截获拖动，拖动期间临时隐藏网页并使用相同占位路径，不另建拖动系统。

### 4.3 弹层、截图与焦点

原生网页不受宿主 DOM z-index/top layer 管理。接入现有弹层状态，在用户主动打开的搜索/对话框、菜单、popover及窄屏侧栏遮罩实际覆盖网页前，撤销该原生页可见性和输入资格。网页尚未收到隐藏确认时，不把弹层呈现为已经可交互；关闭弹层也不直接无条件恢复网页，而重新核对当前会话、选中实例、槽位和其他遮挡。使用现有弹层 owner 和小型适配，不建设通用窗口合成框架。

自动 toast、非模态提示和 hover tooltip 优先布局到当前网页矩形之外的宿主区域；当前右下角 toast-stack 不能继续无条件覆盖网页。不得因后台通知或悬停提示自动隐藏网页、打断输入或夺走焦点。只有用户主动进入确实需要遮挡的交互，才走隐藏与焦点交接路径；未覆盖网页的提示不参与遮挡。验收必须包含网页正在输入时收到后台通知的场景。

可按需用该网页 `webContents.capturePage()` 的进程内图片保持遮罩后的视觉连续性；截图失败或未完成时允许使用普通背景，不能延迟隐藏原生页。截图只是不可交互的临时外观，无 DOM、脚本、网页权限或模型可见性；不缓存到磁盘，不新增通用截图 IPC/工具。实验中宿主 `BrowserWindow.capturePage()` 未包含原生子页，不能用它代替网页截图或原生合成验收。

异步截图绑定稳定实例、当前文档/导航操作及具体遮挡或布局/拖动过渡操作；安装前在最终接收处复核，完成后已结束操作、切会话/标签、导航、销毁或撤销的图片直接丢弃。同一会话/标签字符串再次相等不能让旧截图通过。对应操作结束或失效时清除占位；无需持久版本、截图回执或跨重启恢复。实验只证明了其中一个迟到分支，其余必须在集成时验证。

隐藏前若焦点在网页，将焦点交回对应弹层或工作台合理控件。恢复可见性不自动抢焦点；用户选中网页/点击页面才按交互意图聚焦。隐藏必须落实到原生 View 可见性/挂载与焦点，不用 DOM inert 冒充原生输入隔离。

网页聚焦时宿主 DOM 快捷键不会自然收到按键。由现有桌面菜单/快捷键 owner 路由明确的应用命令（如 Cmd/Ctrl+K）到可信工作台，处理后避免重复分发；不把所有按键、文本和网页事件转发给 React。复制粘贴、页面编辑、输入法组字及网页自己的常规按键继续交给实际获焦页面，应用命令冲突在定向验收中确认。

| 事件 | guest 与页面 | 界面 |
| --- | --- | --- |
| 切换标签、会话；进入设置；会话正文暂时加载 | 保留同一实例、history、DOM/表单和滚动 | 旧网页隐藏且不可聚焦，不抢输入 |
| 点击收起或拖至自动收起阈值 | 保留，不重新 loadURL | 完整网页随右锚槽位移出窗口，结束后隐藏 |
| 调宽、窗口缩放 | 原实例响应布局变化 | 不遮盖分隔线、输入框、标签栏或弹层 |
| 关闭最后一个标签 | 关闭该 guest | 沿用现有自动收起，不清空其他会话 |
| 关闭/forget/clear | 显式关闭所属 WebContents、移除 View、监听及未完成操作 | 移除对应标签；不终止模型任务或本地开发服务器 |
| guest 崩溃 | 保留错误标签；清理失效 guest | 用户点击重试后在原标签创建新 guest；不恢复内存表单/history |
| 工作台顶层导航、renderer 崩溃、窗口真正销毁、退出 | 主进程主动清理全部所属 guest | 不依赖 React cleanup，也不自动重放打开请求 |
| 隐藏窗口/最小化 | 保留 guest | 沿用应用关闭窗口驻留语义 |

隐藏网页可能继续执行脚本、联网或播放媒体；使用 Chromium 默认后台节流，不新增定时回收、页面数上限或自动重载策略，不声称“保活”代表冻结执行。关闭标签明确释放该页面，关闭视图不会停止它连接的服务器。

## 5. 导航合同

### 5.1 输入与路由

地址栏允许省略协议：普通主机默认 HTTPS，localhost（含其子域）与回环地址默认 HTTP；显式 HTTP(S) 保持原协议。空格修剪后交给标准 URL 解析，错误就地提示。协议补全只用于用户提交地址栏，不改写链接或原生导航请求；不做搜索引擎、失败后自动降级或将磁盘路径转换成 `file://`。支持 `http://localhost:端口`、`127.0.0.1` 和 `[::1]` 的任务服务器，但阻止 Pulsara 自身控制服务地址。

聊天和有会话归属的文件预览中，HTTP(S) 普通点击默认在所属会话的网页标签打开；Cmd/Ctrl 点击使用新网页标签。全局设置等无会话归属的外部链接保留系统浏览器行为。网页工具栏的“在浏览器打开”是显式用户操作，复用当前 openExternal 校验，打开实际当前地址。

本地文件地址继续原文件链路；网页中的 `file:`、特权协议和本地文件拖放不打开宿主路径。冻结可视化不因本功能取得磁盘目录或新的宿主权限。

文件 HTML 预览现有桥接只验证 source+token，**不证明用户点击**：文档脚本能从自身 URL 获得 token 并发送同款 navigation 消息。因此接入网页创建时，在可信接收外壳中继续验证当前可见预览的 source+token，并检查 Chromium 实际 transient user activation；不信任消息携带的 userGesture/isTrusted，也不只在不可信 HTML 中添加检查。没有可验证激活的请求仅在该预览显示“打开网页”及目标地址，等用户点击可信外壳按钮后打开；不自动创建标签、展开侧栏或启动系统浏览器。跨 sandbox 激活传播必须实测，不可靠时同样走这个明确的点击入口。原文件内导航仍沿用现有合同，不为这项接入重写文件协议。

### 5.2 原生历史与观察

前进/后退、重定向、hash、History API、页面内普通 GET/POST 导航由 Chromium 处理，不将它们转换成自建 URL 数组或重新发起 GET。UI 观察原生 `did-navigate`、`did-navigate-in-page`、标题、加载、失败和进程退出事件，同步地址与按钮状态；工具栏按钮调用原生 history/reload/stop。

主进程创建的初始空白页不是用户历史；不再加载 `about:blank#句柄` 或进行 DOM-ready 认领。首次成功提交 HTTP(S) 后，若原生历史实际包含内部初始空白项，仅在这个边界清除启动项；没有则不添加清理步骤。之后不因普通导航、重试或切换标签清空历史。首次加载失败仍可在同一标签重试。遵循 Chromium 对无用户激活的脚本历史项的后退策略，不自行补回或强制逐项回放。

地址输入草稿和实际 URL 分开：用户编辑地址时不被标题/加载通知覆盖；提交后显示加载状态，成功导航再采用真实地址。页面内跳转也更新真实地址。当前已提交 URL、待加载 URL、错误状态由本页 owner 持有，不把失败 URL 冒充已完成导航。

正在加载时按钮为“停止”；普通刷新沿用 Chromium 重载行为，不伪造无副作用的 GET。连续导航/停止导致的中止不是需要弹 toast 的失败；真正失败在标签内简短显示原因、目标地址和重试按钮。子资源失败不能替代整个主页面；TLS 验证失败不自动忽略。

停止的状态结算绑定本页当前导航操作及明确的用户停止意图，不能仅以 `ERR_ABORTED` 判断。实测 stop 的 loadURL Promise 可能以 `ERR_FAILED` 拒绝；只有与这次停止对应的回调按停止处理，不屏蔽普通 `ERR_FAILED`，也不能把旧导航的失败通知写到后续新导航。

### 5.3 新窗口请求

原生新窗口始终拒绝。通过 `setWindowOpenHandler` 捕获直接 HTTP(S) 且不带 POST body 的新页面请求，复用原 guest 的会话归属创建新网页标签，不转到此刻恰好活跃的另一个会话。来源会话不可见时只显示该来源标签的“有新页面”提示，用户回来后点击打开；不在后台展开工作区或切会话。该提示只保留最近一个待打开地址，是单个交互提示，不是持久队列或网站 popup 历史。

来源会话当前可见时按原生 foreground/background disposition 决定选中与否，background 不抢焦点。`about:blank` 再写入、依赖 opener、具名窗口复用或 POST popup 流程不作模拟；保留当前网页并给出可在系统浏览器继续的简短提示。清理来源 guest 后取消尚未处理的请求。

现有 `DesktopWorkspaceProvider.open/activate` 无条件选中并展开，因此不能直接用于 background popup。在原 workspace owner 上增加明确的“不激活、不展开”打开选项，仍走同一标签集合；默认保留其他入口原行为，不建立另一个后台标签管理器。

在主进程创建 WebContentsView 后、加载任何外部地址前安装 `setWindowOpenHandler`；不需要 webview 的 allowpopups 或挂载分发标志。转发给工作台的窄事件包含来源原生实例身份、已验证 URL 和原生 disposition，不能照搬 DSH 仅含 lease/url 的事件后猜测前后台行为。是否选中还要在处理时检查来源会话可见性和原实例是否仍存活。

## 6. 主进程管理与权限隔离

### 6.1 最小可信桥接

新增一个桌面网页 owner，记录 `(工作台 WebContents, sessionId, tabId, 原生实例身份, Electron Session, WebContentsView)`。前端 workspace 继续拥有展示标签集合；主进程只管理与其对应的原生资源，不成为 runtime authority、租约服务或 durable job。

沿用 main.ts 的 trusted sender 与顶层 frame 检查，preload 提供带类型的创建、关闭、显示/隐藏与矩形、限定导航命令及状态事件订阅。renderer 不能直接调用原生页面 API；命令仅能操作调用窗口拥有的实例。主进程分配不复用的进程内实例身份，防止同 tabId 重建后旧关闭/布局回调误伤新实例；它只是跨 IPC 的资源引用，不是授权凭据或挂载握手。不得暴露任意 webContents ID、executeJavaScript、ipcRenderer/channel、cookie、代理设置、磁盘读取或调试端口。所有最终请求仍由主进程策略约束。

创建在唯一 owner 中完成：校验会话/标签请求与调用窗口，取得并装好策略的会话分区，以固定 webPreferences 创建 WCV，立即纳入该窗口资源记录并安装 destroyed、导航、popup、安全监听，然后才允许经过校验的外部 loadURL。显式 `setVisible(false)` 必须先于 `addChildView`，不依赖构造默认可见性；只有仍有效的当前显示操作能将它显示。renderer 不能传 partition、preload 或 webPreferences，不设置 about:blank 句柄、不开放 will-attach-webview，也不建立未绑定 guest 集合或 dom-ready 认领流程。

创建响应只等待资源分配和登记，随即返回实例身份，不等待外部网页 loadURL 完成。导航通过同一 owner 的限定命令异步处理、以页面状态事件观察；慢加载期间也能立即关闭精确实例，不为加载另建创建取消握手或超时机制。

创建失败清理已经创建的原生资源；创建期间标签关闭/forget 或工作台失效，使前端原创建操作失效。若资源回包迟到，由原操作关闭其精确实例，不能重新插入标签；主进程在宿主清理时也使在途创建失效，await 返回后不允许继续加载/显示。关闭幂等且作用于原实例：先撤销显示和操作资格，移出 contentView，显式关闭 WebContents并清理监听。removeChildView、setVisible(false)、DOM卸载都不是销毁，宿主重新 loadURL 也不会替代这一步。

资源记录及失效判断均为进程内状态，不加入过期时间、续租、心跳、数据库记录、恢复协议或成功回执。可见性变更使用同一受限 IPC 的正常调用结果协调弹层，不能另加确认事件/投递协议；失败时不让仍可点击的网页被误认为已经遮挡。

分区清理不能仅枚举现存标签：当前 workspace.clear 只遍历仍有 tab 的会话，关光标签后保留的分区也必须由网页 owner 的 clearAll 清理。forgetSession 同时通知标签 owner 和网页 owner；全局重置/退出清理所有已分配分区及在途创建。异步清理绑定旧分区对象，不能把后来新建的同会话分区一并清掉。

清理顺序为：先将该分区的网络策略永久置为拒绝、使创建及回调失效，再关闭全部原生页面，最后清理 Service Worker/网站数据；清理失败保留拒绝态并记录诊断，不能重新用于新页面。`showStartup()` 在清空 origin 或导航前执行这个退役入口；工作台 reload/crash/销毁走相同路径。分区 owner 持有创建时的受保护控制端口，不从可能暂为空的全局 origin getter 推断“无须保护”。新工作台只创建绑定新已验证 origin 的新分区，不复用退役分区。Service Worker 请求可能没有 webContentsId，过滤必须依赖其所属分区而非存活标签查表；这样无标签但仍存活的 worker 也不能在重置期间获得访问空窗。

### 6.2 固定网页策略

- `sandbox: true`、`contextIsolation: true`、`webSecurity: true`；所有 Node integration 关闭，无 guest preload、嵌套 webview 或插件，不加载工作台 contextBridge。
- 不继承工作台 Session、认证头、文件 token、模型配置或本地文件读取资格。guest 与子 frame 调宿主 IPC 一律不通过现有 trusted 检查。
- 权限 request/check、设备、屏幕捕获均明确拒绝；不支持摄像头、麦克风、定位、通知、剪贴板 API、浏览器文件系统和自定义协议启动。普通网页用户主动触发的原生文件选择仍遵循 Chromium 用户选择边界，不授予目录级或静默磁盘读取能力。
- 下载请求取消并在当前标签给简短反馈；不写文件或自动启动外部程序。HTTP 认证、网页模态对话框不弹宿主窗口；HTTPS 证书验证保持默认严格行为。
- 主 frame 导航只接受无内嵌用户名/密码的 HTTP(S)；初始 about:blank 仅作为宿主已批准的启动页，后续脚本不能用它逃逸导航校验。
- 网络资源可用 HTTP(S)/WS(S)，页面资源可用 Chromium 正常 data/blob；拒绝 file 和特权协议。对主导航、重定向、子 frame、fetch、WebSocket、worker/Service Worker 请求一致执行工作台地址阻断，不能只检查地址栏。
- Electron 的 will-navigate / will-frame-navigate 不覆盖程序化 loadURL 和原生历史操作；所有显式加载入口先验证 URL，且分区 `webRequest.onBeforeRequest` 承担网络协议/控制端口的最终拦截。不能只加导航事件监听。使用同一分区策略 owner 安装一次请求过滤器，不让另一个监听覆盖已有过滤。
- 阻断当前 Pulsara 控制服务端口的全部网络请求，不依赖 hostname 字符串；这样同时覆盖 loopback 别名及 DNS 别名指向本机的同端口访问。端口来自主进程已验证的 runtime origin，不由网页填写。代价是同端口的其他站点也不可在内嵌网页访问，可主动转系统浏览器。内核切换到启动/重置页前先关闭 guest，不能在 origin 暂为空时放开访问。
- 不向用户提供关闭 sandbox、忽略证书或放开控制端口的开关。保留服务端现有 Host/Origin 检查，不能为网页浏览扩宽后端 CORS。

本地开发网页是正常浏览目标，其他私网地址按 Chromium 标准网络/权限策略处理；上述控制端口过滤不是通用私网防火墙或对任意任务服务器的授权系统。Electron 44 的 local-network/loopback 权限若阻止验收目标，必须先核对实际请求及支持的权限细分，再修订明确的放行边界；不以 `return true` 放开全部权限来完成验收。

## 7. 实施顺序与文件范围

1. 增加主进程 WebContentsView owner 和窄 preload 合同，先验证创建时策略安装、实例归属、在途操作失效和退出清理；保持禁用 webview，不动 Python runtime。
2. 在现有 workspace 中引入 browser 标签和正文测量槽位，接上会话清理、按需矩形同步、右锚动画、分隔条避让、弹层/焦点和应用快捷键适配。只为网页保活，不引入通用插件、合成器或标签生命周期框架。
3. 增加网页工具栏和原生导航观察；接入新建网页、会话链接及文件预览外链，处理 popup 和失败状态。
4. 完成定向测试及实际桌面验收，再将网页目标加入 open_in_pulsara 方案。该工具及提示词修改仍是后续工作。

预期生产改动限于 desktop 的 main/preload/guest owner、frontend 的 desktop-host/workspace/网页组件、会话展示入口及对应样式。除发现当前合同中必需的最小接入点外，不修改 Python 工具、runtime、schema、provider 或发布路径。

## 8. 验证与完成条件

测试验证可观察行为和隔离，不模拟整套 Chromium。DOM 测试只能证明标签状态，不能代替 Electron guest 验收。

| 重点 | 定向验证与实际桌面检查 |
| --- | --- |
| 会话归属 | A/B 同 URL 独立；A 页面迟到加载/popup 不落入 B；A→B→A 不串页；重新载入会话 runtime 不改绑/刷新网页 |
| 保活 | 页面记录一次初始化计数、表单输入和滚动；切文件/MCP、切会话、设置页、隐藏窗口及收起/拖动收起后仍为同一页面，开发服务器仅收到一次初始加载 |
| 布局 | 最大/最小宽度、两侧动画及快速反向/取消；真实鼠标从分隔条起拖、跨网页释放、自动收起；窗口缩放、host/guest独立zoom和多屏DPI；原生画面与点击命中都须正确，不能只断言setBounds数值 |
| 遮挡与输入 | 搜索/对话框、菜单、popover、窄屏scrim及嵌套弹层；自动toast/hover tooltip避让，后台通知不打断网页输入；截图失败、迟到、导航/切会话/标签关闭交错；隐藏页不吞输入，关闭一层不露出仍被遮挡页；网页聚焦时应用快捷键、复制粘贴、真实输入法组字及恢复焦点 |
| 导航 | 普通链接、302、fragment、pushState/replaceState、原生back/forward及无用户激活脚本历史；首次空白项、慢加载后停止、停止后立即新导航、真实失败重试、POST；不依赖单个中止错误码，地址和按钮以原生状态为准 |
| 新标签 | 同 URL 普通打开聚焦；显式新标签独立；_blank、background disposition、隐藏来源、关闭后迟到请求、POST/blank popup 明确降级 |
| 存储 | 同会话共享测试站点 Cookie/storage，跨会话隔离；不共享工作台 cookie；forget/clear 清理；重启无 Cookie 和网页恢复承诺 |
| 来源与权限 | guest/子 frame IPC、跨窗口或旧实例身份、注入 partition/preload/webPreferences、Node/宿主接口、file URL、下载及设备权限均不能获得额外能力；宿主仍拒绝webview挂载 |
| 接入空窗 | 伪造预览 navigation 消息且无用户激活不创建tab；策略和owner监听完成前不加载外部页；创建中关闭/forget/重载后不留孤儿实例；旧布局/截图/关闭回调不能作用于新实例；普通标题更新不误判网页实例失效 |
| 网络边界 | 真实工作台 HTTP/API/WS 端口通过导航、redirect、iframe、fetch、worker 请求均被拒绝；常见 loopback/别名不能绕过；其他端口任务服务器可正常浏览 |
| 故障清理 | 加载中关闭/删除/归档、guest 崩溃重试、renderer 重载/崩溃、数据库 reset、退出；关光标签但 Service Worker 仍存活时 reset 不产生控制端口访问空窗；无遗留 guest，旧事件不复活标签 |
| 回归 | test8 HTML→MD、文件前后退、冻结可视化、MCP 独立 tab、后台进程查看器、关闭最后标签收起、新会话默认收起、按会话宽度保持原行为 |

实际验收使用当前 macOS 打包应用或同资源构成的桌面运行入口，以及已有 Linux 桌面环境；Linux 或多屏环境不可用时明确记录未验收，不将仅编译成功写成 GUI 通过，不为首版建立所有系统版本的测试矩阵。用可控本地站点覆盖导航/状态/隔离，并用一个普通 HTTPS 网站验证真实顶层浏览。原生合成截图与真实鼠标用于检查遮挡和命中；宿主DOM截图、主动向指定webContents发输入或中文粘贴不能替代实际画面、原生命中及输入法验证。未通过保活或权限检查不能宣告完成。

不需要为纯浏览功能调用真实模型或重跑长任务；回归现有会话即可。SYSTEM/tools 前缀、messages 追加规则、模型上下文、任务/终端/MCP 执行及结算、历史定位/引用/分叉/分组均不变。无新增 durable/live event、数据库关系、执行恢复或 UI 成功回执。

## 9. 交叉检查记录

审阅代理：`browser_tab_plan_review`，模型 `gpt-6-astra`，reasoning effort `high`。只读检查本文、Pulsara 生产代码、DSH 桌面网页实现和 Electron 44 API，未修改生产代码。

原审阅中的 webview 载体选择及其挂载协议已被本次用户决定替代；保留会话归属、background popup、可信预览用户激活、原生历史、分区退役和稳定实例回调等独立于载体的结论。

同一子代理已参与 WCV 生命周期/隔离实验并只读交叉检查主代理布局实验，确认当前 macOS 右侧栏可行，但不是生产集成完成证明。本次文档复核覆盖：移除原webview专属机制；直接创建的owner与清理闭环；按需几何/遮挡/焦点适配不过度建设；会话归属、保活、原生导航与权限隔离；runtime执行语义不变。

主代理复核并采纳四项调整：自动toast/非模态提示避让网页、不打断输入；临时截图范围统一为遮挡及必要布局/拖动过渡、绑定具体操作失效；显式隐藏先于原生视图附加；创建只分配登记并返回身份、网页导航异步观察，不建立加载取消握手或超时机制。

2026-10-19 修订后复核结论：以上四项均已闭合，当前审阅范围内无剩余阻断或实质矛盾，方案可以进入实现。正常IPC返回协调隐藏不构成额外回执系统；明确应用快捷键路由与普通编辑/IME分离的边界成立。保留生产集成、Linux、真实输入法、多屏DPI等未验收项。本轮只修订本文，未写生产代码。
