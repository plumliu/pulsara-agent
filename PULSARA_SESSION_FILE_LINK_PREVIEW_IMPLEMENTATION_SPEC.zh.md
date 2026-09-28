# Pulsara 会话文件链接与预览实施规格

状态：**已实施，独立代码复核通过**。日期：2026-09-28。依据本轮用户讨论、[AGENTS.md](AGENTS.md) 与当前生产代码，作为当前实施合同。本篇只规定用户点击文件链接后的本地阅读体验，不改变模型工具、权限档位、provider 输入、对话持久结构或既有可视化订阅语义。

## 1. 目标与产品合同

模型可以继续输出普通 Markdown 文件链接，例如：

```markdown
[分析报告](customer_behavior/analysis_output/report.md)
[客户群体汇总](customer_behavior/analysis_output/segment_summary.csv)
[群体结构](customer_behavior/analysis_output/views/01_cluster_structure.html)
```

用户点击后，Pulsara 按该链接所属会话的工作目录找到文件，打开会话内预览；浏览器不再将文件路径拼到 `http://127.0.0.1:<port>/` 根路由。本机实际案例中的工作目录是 `/Users/plumliu/Desktop/little_snake`；Pulsara 仓库目录和启动进程 cwd 都不能替代它。

明确的行为：

- 旧消息中的普通 Markdown 链接立即适用，无需重跑模型、迁移消息或重写链接正文。
- 相对路径以所属会话保存的 `workspace_root` 为基准，不随当前选中的另一个会话、浏览器地址、terminal 后续 `cd` 或 Pulsara 启动目录变化。
- Markdown 文件预览中再点击相对链接，以当前文件的父目录为基准。聊天消息和 Markdown 正文分别保有自己的基准；HTML 内部链接只在其冻结的静态资源树内导航，不取得通用本地文件访问权。
- 相对路径、绝对路径、`~/` 与本机 `file:///` 链接可使用本地入口；普通 `https:` / `http:` / `mailto:` 链接保留原行为。显式 HTTP URL 即使包含 localhost，也不自动猜测成磁盘路径。
- 默认不扫描普通正文、行内代码或代码块中的路径，也不在消息渲染时访问磁盘；只有用户激活链接时读取。
- 文件已移动、删除、类型不符或无权限时显示可操作的说明，保留原消息和路径，不跳转到空白 404 页面。
- 点击链接只影响当前前端阅读状态，不给模型追加消息或 tool result，不修改文件。

## 2. 当前代码事实

| 代码/规格 | 已有机制 | 本次用途或缺口 |
| --- | --- | --- |
| `frontend/components/markdown-body.tsx` | `MarkdownBody` 与 `MarkdownInline` 共用链接分类和文件阅读上下文 | 本地文件进入弹窗，外链保留原行为 |
| `frontend/components/workbench-view.tsx` | `VisualizationPanel` 读取已提交 occurrence，展开后使用共享隔离外壳 | 底层展示由 `SandboxedHtmlPreview` 负责，卡片订阅和 canonical 读取继续由原 owner 负责 |
| `frontend/lib/visualization-frame.ts` | 可视化主体测量，只接收匹配 frame 的几何消息 | 可视化卡片继续使用；文件弹窗默认整页展示，不强行裁剪图表主体 |
| `frontend/components/file-reference-chip.tsx` | 路径 hover 与复制，无通用内容预览 | 复用外观/路径展示；本次不改变编辑器节点和 canonical prompt 格式 |
| `frontend/lib/runtime-adapter.ts` | JSON API、连接生命周期及 canonical 内容读取 | 新增窄文件预览调用；文件流不经过 JSON/base64 包装 |
| `web_app/session_controller.py::complete_workspace_paths` | 通过 `read_resumable_session` 得到已保存工作目录 | 复用目录来源，不能直接复用路径补全函数充当文件读取授权 |
| `web_app/http_server.py::_security` | 检查 loopback Host；Origin/Fetch-Site 检查目前主要用于变更请求 | 预览创建/原生操作走严格同源 POST；新二进制 GET 必须自己验证授权，不能把既有 GET 当作已鉴权 |
| `web_app/session_controller.py::open_capability_root` | macOS 通过 `create_subprocess_exec("open", path)` 打开固定根目录 | 可复用宿主进程调用方式；新增文件动作要有自己的目标校验 |
| `local_source_binding.py` | 词法路径处理、Darwin 系统别名、逐组件 no-follow 目录打开 | 复用 OS 文件绑定原语，避免校验路径后又按可变路径重开 |
| `PULSARA_VISUALIZATION_SUBSCRIPTION_IMPLEMENTATION_SPEC.zh.md` | 订阅后的 HTML 随消息保存为 immutable blob，历史从 occurrence 读 | 此合同保持；本地链接预览是独立、临时的当前文件阅读入口 |

canonical 可视化 CSP 禁止外部脚本、样式和网络连接。当前实现使用可信外壳限制内容 frame 的导航，已移除对 `navigate-to` 的依赖；实际浏览器证据见 §12。

## 3. 两种 HTML 入口的明确分工

| 入口 | 内容来源 | 容器 | 生命周期 |
| --- | --- | --- | --- |
| 模型调用 `visualization_render` 后的卡片 | 已提交消息的 `READY` occurrence / immutable HTML blob | 现有可展开、宽版图表卡片 | 原文件更改/删除不影响历史展示 |
| 用户点击 `.html` 文件链接 | 本次打开时的本地文件 | 文件预览弹窗 | 关闭即丢弃前端状态；重新打开读取当前文件 |

未注册可视化的 HTML 也可点击预览。预览不会自动调用 `visualization_render`、创建订阅槽位、增加 occurrence、生成 `visualization_ref` 或把 HTML 保存到 canonical blob。

组件所有权：

```text
VisualizationPanel ── canonical occurrence reader ──┐
                                                    ├─ SandboxedHtmlPreview
FilePreviewDialog ── local preview reader ──────────┘
```

`SandboxedHtmlPreview` 只拥有 frame、安全装载、加载/显示错误及可选的布局报告。文件查找、订阅、数据库读取、下载和原生打开不进入该组件。两种来源使用显式封闭配置：canonical 只接受自包含 HTML；local 可以在下文规定的资源范围内加载附属文件。不能因为本地预览需要资源就放开 canonical 卡片的网络权限。

## 4. 文件预览界面

使用当前主题的居中大模态弹窗，桌面提供充足阅读面积，窄屏占满可用区域。顶部显示文件名；完整路径可查看/复制。提供关闭、下载、在 Finder 中显示；适用类型另外提供“使用系统应用打开”。保留短过渡及减少动画设置。CSV/TSV 表格使用 14px 字号。Markdown、CSV/TSV、HTML 的源码切换统一使用右上角的 `Code2` 图标按钮，保持按下状态与可访问名称；原有独立源码工具栏删除。代码/JSON/普通文本直接显示源文。仅在内容需要分页时，在内容底部显示翻页操作。HTML 切换源码时沿同一已绑定文件的 UTF-8 分页读取；恢复预览保留原 iframe 交互状态。

- 打开后立即显示容器与加载状态，异步读取；原对话滚动位置不变。
- Escape 和关闭按钮关闭，焦点回到原链接；遮罩与内部滚动、文本选择互不干扰。正在下载不因关闭预览而伪称下载失败。
- 同一阅读视图只有一个文件弹窗；点击另一个文件替换当前内容，取消旧内容请求并释放旧 frame/Object URL/文件句柄。创建、替换与关闭按 §5.3 结算；不能仅靠 AbortSignal 保证服务端顺序。晚到的响应不能覆盖新选择。
- 所属会话切换、连接失效或会话删除后，关闭该视图并释放资源。可恢复会话若已经建立现有浏览器连接，可以直接阅读；预览不另建模型轮次。
- Markdown 预览中的链接沿同一个文件入口处理，基准改为当前文件父目录；只保留一个弹窗，不递归堆叠模态层。
- Cmd/Ctrl 点击本地文件链接仍进入文件预览；首版不承诺可收藏的本地文件网页 URL。复制链接地址不得复制临时授权 token；复制路径提供原始解析路径。

| 类型 | 默认呈现 | 边界 |
| --- | --- | --- |
| `.md` / `.markdown` | 复用 Markdown 阅读组件，可切换源文本 | 不允许 raw HTML 注入主 DOM；本地图片走 §5.4 的图片范围。超出单页预算时改为源文本分页，不将任意字节页伪装成完整 Markdown 文档 |
| UTF-8 文本、代码、JSON | 等宽文本、行号；JSON 默认忠实显示源文 | 不执行代码、不自动修复或重排文件；无法解码时说明并允许下载 |
| `.csv` / `.tsv` | 简单表头和表格，按需继续读取记录 | 使用成熟解析器，正确处理 BOM、引号、换行字段；不执行公式、不推断分析结论 |
| PNG/JPEG/WebP/GIF 等被支持的栅格图像 | 图片缩放预览 | 沿用已有图片展示组件可用部分；SVG 不直接注入主 DOM |
| PDF | 浏览器 PDF 查看能力可用时内嵌阅读 | 不可用时提供下载/系统应用打开；不把浏览器 PDF 插件依赖伪装为所有浏览器均保证支持 |
| HTML | 下文规定的隔离页面预览 | 不依赖可视化注册；无后端服务、CDN 或任意网络代理 |
| DOC/DOCX、XLS/XLSX、PPT/PPTX、压缩包和其他二进制 | 文件信息、下载；适用时系统应用打开 | 本次不新增 Office 转换依赖或后台格式转换 |
| 文件夹 | 文件夹信息、复制路径、在 Finder 中打开 | 不复制/压缩整个目录，不引入文件管理器 |

类型路由结合文件名和实际读取结果；不能仅凭扩展名将任意内容作为有主应用权限的 HTML 执行。无已知扩展名但采样可解码为 UTF-8 且无 NUL 的文件可尝试文本分页；后续解码失败仍明确降级。其余未知类型按下载处理。

## 5. 链接解析与本地读取所有权

### 5.1 前端分类

共用一个分类函数供 `MarkdownBody` 和 `MarkdownInline` 使用，并通过所在阅读视图提供不可混淆的会话上下文。需要覆盖最终回复、过程文字、任务详情与子代理摘要。没有所属会话信息的全局 Markdown 界面不猜测当前 cwd。

- `http:`、`https:`、`mailto:` 和协议相对网络 URL 保留正常外链语义；`#anchor` 是当前文档锚点。
- 无 scheme 的文件路径、绝对磁盘路径、`~/` 和空 host/localhost 的 `file:` URL 进入本地解析。拒绝远程 `file://host/`、控制字符、NUL、非法编码。
- URL 只按确定的单次百分号解码规则转换为文件路径，避免 `%252e` 等重复解码；query 和 fragment 不属于磁盘文件名，带这些字符的真实文件名须编码。fragment 不参与文件寻址，首版不提供跨文件的标题/行号跳转，不扩大文件权限。
- `javascript:`、任意 `data:` 链接和未知 scheme 不交给宿主执行。`sandbox:` 等其他产品专用链接不给本机路径猜测规则，展示“无法识别这个文件地址”。
- `react-markdown` 当前默认 URL transform 会处理 scheme；新增 transform 仅在 `a[href]` 上保留经过分类认可的本地 URL，其余属性沿用默认规则，不能全局 identity transform 放开脚本 URL。

### 5.2 主文件的范围

主文件是当前本地用户在 Pulsara UI 中明确激活的目标。相对路径锚定所属会话；绝对路径、`~` 与 `..` 延续当前本地路径产品的可表达性。HTTP owner 验证本机浏览器操作与连接归属，文件 owner 执行 OS 权限和普通文件/目录检查。这不授予模型新权限，也不把“模型写了一个路径”变成后台自动读取。

主文件授权只允许所选文件的读取、下载和显式原生动作；页面脚本不能把它改成另一条路径。打开 Markdown 还包含 §5.4 的栅格图片范围；打开 HTML 还包含 §7 的静态资源范围，二者均由服务端按文件类型固定，不能由页面扩权。连接的 ROOT/SUBAGENT 归属以当前现有连接/会话事实为准，不接收前端自报 workspace_root。

使用目录 FD 相对打开和普通文件验证处理检查/打开竞争。当前通用 no-follow helper 可直接用于无符号链接路径；若用户选择的路径含符号链接，明确返回该链接暂不可预览及 Finder 操作，不能静默跟随到另一个根。不要用 `Path.resolve()` 后调用未经绑定的 `FileResponse(path)` 冒充同一文件身份。

### 5.3 HTTP 与临时资源

现有 browser connection 下的窄入口：

| 请求 | 用途 |
| --- | --- |
| `POST /api/connections/{id}/file-preview` | 按 `{path, base_preview?}` 打开目标并替换该视图当前预览；返回类型/文件元数据与临时读取入口 |
| `POST /api/connections/{id}/file-preview/action` | 携带预期 `read_token`，对该实例执行 `reveal` 或允许的 `open`；目标从服务端槽位取得 |
| `DELETE /api/connections/{id}/file-preview` | 携带预期 `read_token`，只释放匹配的实例 |
| `GET/HEAD /api/file-previews/{read_token}/content` | 主文件原始阅读/下载；HTML 在此仅允许 attachment 或忠实源文本，不能成为另一个可执行入口。不通过 query 接收另一条路径 |
| `POST /api/connections/{id}/file-preview/page` | 携带预期 `read_token`；文本/CSV 继续读取，cursor 只属于该实例 |
| `GET/HEAD /api/file-previews/{read_token}/resources/{relative_path}` | HTML 文档与静态资源树；可执行主文档明确位于 `resources/<真实叶名>`，所有 HTML 响应统一隔离 |
| `GET/HEAD /api/file-previews/{read_token}/images/{relative_path}` | 仅 Markdown 的栅格图片范围；读取不替换主文档槽位 |

浏览器 iframe/img/PDF 不能复用 JSON POST 内容读取，因此需要临时只读 URL。每个已有 browser connection 只持有当前预览的一个进程内槽位：随机不可猜测的读 token、所属会话、绑定的文件/目录句柄、预览类型及必要的页读取状态。正常替换、关闭、connection detach、session delete 和 Host close 都撤销。服务重启后旧 token 失效，UI 重新打开即可。

浏览器崩溃不等于 Web Host 到 kernel 的内部连接断开，DELETE keepalive 也只是尽力发送，不能承诺崩溃时立即清理。残留槽位在可观察到的连接替换、disconnect、stale connection、session detach/delete 或 Host close 时撤销。实现接入 `LocalBrowserBridge.connect` 的旧连接替换、`disconnect`、`_detach_session_for_operation`、`_connection` 的 stale 分支及 `_close_owner`；复用统一的预览释放动作，不另建 TTL、租约、心跳或恢复框架。

并发合同：

- `read_token` 同时标识本次预览实例。action/page/DELETE 必须在该 connection 下比较当前 token；不匹配返回 `PREVIEW_EXPIRED`，旧请求不得操作新实例。DELETE 只校验连接与实例身份，文件已变化也必须可撤销。`base_preview` 同样精确匹配，并在替换前取得当前文件父目录。前端同时保存用户所点击目标的绝对 URL 供失败重试，重试不依赖已经撤销的父文档 token。
- 同一前端 connection owner 串行提交创建、替换和关闭。用户可立即选择下一文件并看到加载状态；已经提交的创建先结算，若结果过时则按返回 token 清理，再提交最新仍待处理的选择，不积压已经被替代的点击。
- 后端在现有连接锁保护下登记当前进程内打开操作，慢速磁盘读取在锁外完成；安装结果前同时复查连接仍有效、操作对象仍为当前。被替代/关闭的操作只能释放自己的句柄，不得重新安装。操作对象身份已经足够，不增加 generation、持久请求收据或通用任务队列。
- AbortSignal 用于内容请求与前端迟到结果隔离，不能代替变更请求结算。连接失效即放弃该视图；请求中断后也不能假定服务器未执行，服务端关闭/替换检查仍负责收尾。带 cursor 的分页在实例内串行读取，重读同一当前页不得再次推进 CSV 迭代器。解析失败后本实例的表格模式持续返回明确失败，不沿出错后的位置继续冒充原 cursor；源文本和下载仍可使用。

该槽位仅为浏览器子资源访问提供限域能力，不增加数据库关系、持久事件、job、receipt、内容哈希身份或跨重启恢复；不作为模型 tool 权限凭据。不能以客户端可控路径或内容摘要替代不可猜测 token。token 不进入模型、导出消息、访问日志或外链 referrer。创建/动作端点要求精确 Origin 和 same-origin Fetch Metadata；不因当前 middleware 对无 Origin 请求较宽松而在新文件入口省略验证。

用已有 HTTP/connection owner 收尾并发请求。关闭时先撤销后续读权，再结束持有 FD 的请求；不要为下载添加独立持久 job 或 lease。普通预览流在每块读入前后检查撤销，撤销后中止后续传输并释放该请求的句柄。显式下载流只保持当前请求所需文件句柄，UI 关闭后允许已经由用户开始的下载完成。

读取响应使用 `Cache-Control: no-store`、`Referrer-Policy: no-referrer` 与 `X-Content-Type-Options: nosniff`，不依赖浏览器缓存续用已经撤销的能力。当前 HTTP access log 已关闭；新增诊断也不能记录完整 token URL。header 与 MIME 由服务端按已验证类型生成。

### 5.4 Markdown 本地图片

用户打开 Markdown 时，该实例可加载当前文档父目录及子目录中的受支持栅格图片。前端为 Markdown 的 `img` 单独解析相对地址并映射到 `images/`，不直接交给浏览器按应用根路由加载，也不逐图调用创建预览接口。此范围拒绝越根、绝对路径、点文件/点目录、符号链接、特殊文件以及 SVG/HTML；缺失或不支持时显示图片占位和原始地址。

服务端沿绑定目录 FD 校验并读取，明确图片 MIME，不执行图片地址中的脚本。图片请求不替换主文档、不取得文件动作能力。普通外部图片沿已有 Markdown 的网络策略，不借本地接口代理；聊天消息中的路径型图片不能因为新增链接处理就自动获得磁盘读取授权。

## 6. 文件传输、分页与变化

- 下载流式传输并尊重背压/取消，不全文 base64，不设文件总大小、会话累计下载量或总次数上限。
- 预览使用有界页和按需读取；前端只挂载可见/当前页内容，不把大文本或 CSV 全量塞进 DOM。复用现有 `content_chunk_hard_bytes` 作为每次内容块的传输内存边界；它是分页块大小，不能变成文件拒绝阈值。
- 文本按完整 UTF-8 字符边界返回，首版支持 UTF-8/BOM；其他编码提供明确说明与下载，系统应用打开仍遵守文件类型白名单，不悄悄替换乱码。
- CSV/TSV 复用 Python 标准库 `csv` 顺序解析，不能按 `split(',')` 或每行一条记录处理。页传输预算不能限制解析器构造一条完整记录的内存：另将已有 1 MiB 内容块值用作**单次记录原始 UTF-8 输入预算**，理由是浏览器表格预览不能无限累积一个尚未产出的记录。使用带预算的行迭代器，同时约束物理行读取与 `next(csv_reader)` 消费的多行累计输入；仍由 `csv` 处理语法，不复制 CSV 解析器。超过记录预算或解析器实际字段边界时返回 `FILE_PREVIEW_UNSUPPORTED` 并说明原因，不默默丢行/截断字段，原文件仍可不限总大小地下载。
- 记录解析预算和页面编码后的 JSON 预算分别检查；后一项必须计入转义和结构开销。下一完整记录装不进当前页时留给下一页；单记录连同结构本身超过页面预算时明确降级为源文本/下载，不陷入空页循环。页内行列虚拟化属展示资源控制。横向每 1024 列构成一个可直接跳转的展示分组，以免浏览器 CSS 布局坐标上限导致末尾列无法到达；全部列仍可访问，没有总列数限制。这些都是一次解析/传输的物理边界，不限制总记录数、总页数或会话寿命。
- Markdown 只有在完整内容落在单页预算内时保证完整的 Markdown 排版。大 Markdown 提供源文本分页并说明，不能逐字节页独立解释围栏、表格和跨页引用定义；首版不自建增量 Markdown 解析器。
- 图片/PDF 读取可使用原始流；Range 通过 aiohttp 已有 request range 解析能力实现必要的单范围响应，非法/不满足返回标准结果。不要复制完整 HTTP range/缓存协议栈。
- 已检查当前 aiohttp `FileResponse`：公开构造参数只收 path，内部会重新 `stat/open`，没有接收已经验证的 FD 的公开入口。限域读需要采用 `StreamResponse` + 已绑定文件句柄的小适配，保留 aiohttp 的流写入/取消/Range 解析；此处的自定义仅补 FD 身份缺口。
- 并发 Range/下载使用基于显式 offset 的读取（如 `os.pread`），不共享可变 seek 位置；`os.dup` 可以持有独立关闭生命周期，但它仍共享文件偏移，不能据此声称并发读取互不干扰。CSV/文本解析状态由实例内串行分页路径拥有，与下载流分离。
- 使用文件已有的 device/inode/size/mtime 等值识别分页期间的变化，不增加文件 SHA。观察到变化时明确提示刷新，不把两个版本拼成一个完整报告。重新打开读取当前版本；这一语义不覆盖 canonical visualization 快照。
- HTML 与附属文件不是事务性快照；预览期间被修改时可刷新，不伪称所有资源来自同一时刻。禁止为达到快照效果复制整棵目录或写入新 blob。

## 7. HTML 的具体复用与资源支持

### 7.1 自包含 HTML

先实现自包含 HTML，覆盖当前四份 Plotly 报告。用户点击 → 主文件校验/读取 → 弹窗 → `SandboxedHtmlPreview`。即便模型完全没有调用 render 工具也可以展示。保持内联脚本可用，主页面 DOM、cookie、存储与内部 API 隔离。

canonical 卡片与本地弹窗共享 frame 外壳，但各自保持读源。canonical 首次展开后保留 iframe 交互状态；本地弹窗关闭释放 iframe，重新打开刷新文件。两者都不能把 HTML 直接注入 Pulsara 主 DOM。

### 7.2 同目录的静态资源

本计划包含本地 HTML 的静态资源支持，但必须在 §10 的浏览器验证门通过后启用；不能仅凭 iframe 设置推断可行。

- 资源根固定为本次所选 HTML 的父目录，允许该目录及子目录中的普通 HTML、CSS、JavaScript、JSON、CSV/TSV、栅格图片和字体等明确支持类型；不提供目录列举、点文件/点目录、符号链接、设备/FIFO 或任意其他文件读取。
- 保留浏览器原生相对 URL 解析：可执行主 HTML 的 `document_url` 明确为 `/api/file-previews/{read_token}/resources/<真实叶名>`，主文件位置使用该实例已绑定 FD；嵌套 HTML 位于同一资源树对应位置。这样 `./style.css`、CSS 的 `url()` / `@import`、本地 ESM 相对 import 和 `fetch('./data.json')` 都按各自原有目录关系解析。不能用 `/content` 作文档地址再期望浏览器自动添加 `/resources/`。资源端点不做外部重定向，也不开放可改 root 的参数。
- 单次解码、规范化后验证仍位于绑定根内，再按目录 FD no-follow 打开。`../` 仅可在根内消解，不能越过根；拒绝编码绕过和目录替换。浏览器先消解 URL 导致 token 前缀丢失时请求同样必须失败。
- 站点根路径 `/assets/...`、目录之外的 `../data.csv`、Node/Python 后端接口、CDN、外网、WebSocket、service worker 和开发服务器均不在支持范围。说明需要相对且在资源根内的静态依赖，保留下载/Finder 操作；不自动启动 server、不安装依赖、不把 URL 代理到网络。
- 沙箱 frame 不启用 `allow-same-origin`、弹窗、下载、表单或顶层导航。主文档及嵌套资源树内的每一个 HTML 响应都需以响应头施加 sandbox/CSP，防止读 URL 被直接打开时获得主应用同源能力；不能把 `sandbox` 指令只放进不支持该指令的 meta。
- 本地资源模式的 CSP 只允许精确当前 token 的资源前缀，按类型开放 script/style/img/font/connect；内联脚本/样式和 data 图片按需要保留。绝不能将 `connect-src` 或 `script-src` 放开为整个主应用 origin。canonical 模式继续不允许这些本地资源。
- opaque origin 的 ESM/font/fetch 请求确有 CORS 需求。只在有效 token 的资源 GET/HEAD 上返回受限 CORS，允许该 opaque origin，不携带凭据；不能给整个 `/api` 加通配 CORS。GET 的 token 验证是授权来源，Origin `null` 本身不是授权。

### 7.3 导航与消息边界

iframe sandbox 会隔离页面权限，但不能据此声称任意 HTML 都无法发起 iframe 自身导航。当前 `navigate-to` 不作为安全门。使用**可信预览外壳约束内层内容 frame**：外壳的 CSP `frame-src` 只允许当前预览文档/资源前缀；内层 frame 使用 `sandbox="allow-scripts"` 和自身资源 CSP。外壳不混入不可信 HTML，不向内层提供主应用 API。

该组合已通过 §12 的 Chromium/Safari 检查。后续改动仍必须同时验证自包含 HTML、静态资源 HTML 和读 URL 被直接打开三种装载方式，覆盖脚本自导航、meta refresh、`_self/_top/_blank`、重定向和父级导航的网络结果；若不能守住声明的边界，应先修订装载方案，不能通过放宽主应用权限让图表“显示成功”。顶层直接打开没有可信父外壳，因此资源端点仅在 `Sec-Fetch-Dest: iframe` 时返回可执行 HTML；其他装载方式返回 403 纯文本并保留 sandbox/CSP 响应头，禁止直接打开后以自身导航绕过父级约束。

应用两个入口的 CSP 使用 `frame-src about: http://127.0.0.1:*/api/file-previews/`。端口通配适应本机服务动态端口；可信外壳仍进一步收窄为当前 origin 的精确 token 前缀，canonical 外壳则为 `frame-src 'none'`。Safari 会在 opaque 子 frame 重新解释继承的 `'self'`，因此此处明确写 loopback 预览路径，不开放整个主应用路由。

主页面只接受已知 frame/source 的有限加载/尺寸观察。文件路径、资源读取授权、原生打开、下载和用户操作不能由 HTML 的 `postMessage` 请求触发。文件弹窗不需要沿用可视化主体裁剪协议；共享底层后 canonical 的原布局测量必须回归验证。

## 8. 原生操作与失败呈现

- Finder 定位使用 macOS 的参数数组调用，文件使用 `open -R <absolute-path>`，目录使用 `open <absolute-path>`；不经过 shell 拼接。动作发生前重新验证当前目标及所属预览，不能仅凭旧 token 指向已替换文件。
- 原生动作的合同是“重新校验后把明确路径交给 OS”。macOS `open` 接受路径，不能承诺与预览 FD 原子绑定；检查时已变化则返回 `FILE_CHANGED`。符号链接不可内容预览时，可保留只含所选词法路径的失败状态供用户显式 Finder 定位，不发放内容 URL，不把这条路径交给系统应用执行。
- 系统应用打开由用户显式点击；优先对 PDF、图片与 Office 等文档类型提供。代码、可执行文件和未知类型默认只提供预览/下载/Finder，不因为点蓝链而自动执行。
- 文件读取失败使用 `FILE_NOT_FOUND`、`FILE_UNREADABLE`、`FILE_CHANGED`、`FILE_PREVIEW_UNSUPPORTED`、`PREVIEW_EXPIRED`、`RESOURCE_OUTSIDE_ROOT` 等窄错误；沿用现有 toast/弹窗错误样式，不新增 durable 错误事件。
- 空文件正常显示“空文件”。缺少 HTML 依赖在预览内说明，不能把空白 iframe 当作成功验收；iframe load 事件本身不证明页面所有脚本正确。
- 文件内容不可信。Markdown 不启用 raw HTML，CSV 不执行公式，HTML 保持沙箱，文件名不能插入响应头控制字符；下载 Content-Disposition 使用库提供的编码能力。

## 9. 实施切分与文件落点

阶段是开发/验证顺序，不是生产中的新旧双路径或 feature flag。

1. **链接与 owner**：增加链接分类模块、所属会话阅读上下文、服务端文件预览 owner；明确类型、异常、关闭行为。测试正确的 cwd、绝对路径、编码、无效/外链，以及两个会话同名文件不会串读。
2. **基础阅读**：完成文本/Markdown/CSV、图片/PDF、下载/Finder/适用系统打开；先覆盖本次报告及结果表。长文件按页或流处理，不修改原始文件。
3. **自包含 HTML 与组件拆分**：提取共享 `SandboxedHtmlPreview`，本地弹窗读取文件；canonical 卡片保持快照来源、折叠、交互状态与失败占位。两个入口同时验收。
4. **静态依赖与浏览器隔离验证**：实现受控资源 URL，先验证沙箱、CORS、导航、路径竞争，再开启 CSS/JS/图片/字体/本地 JSON 组合案例；失败则修订方案，不能宣布 HTML 依赖支持完成。
5. **真实案例验收**：在 `little_snake` 会话点击实际报告、CSV 和四张 HTML；准备一份未注册 render 的 HTML 及含静态依赖目录，验收后确认未增加消息/订阅/数据库事实。

已实施落点：

- `frontend/components/markdown-body.tsx`：复用文件链接组件与分类；保留公式/Mermaid行为。
- `frontend/app/pulsara-app.tsx`：以当前 connection/session owner 提供阅读上下文，覆盖工作台、检查器及其任务视图 portal；只让内部预览 host 随 owner 替换，不重挂载会话正文或输入框。
- 新增 `frontend/components/file-preview-dialog.tsx`、`sandboxed-html-preview.tsx` 和窄的链接/预览类型模块；避免复制现有可视化整张卡片。
- `frontend/lib/runtime-adapter.ts`：预览/动作/分页 DTO 与 AbortSignal，二进制 URL 与 JSON 请求明确分开。
- `web_app/http_server.py`、`browser_bridge.py`：入口、现有连接生命周期、saved workspace 来源；文件绑定/分页在 `web_app/file_preview.py`，HTTP/流读适配在 `web_app/file_preview_http.py`，不扩散到 kernel 或 tools。
- 复用 `local_source_binding.py` 的目录绑定。若需要补文件 FD 原语，限制为可复用的窄 OS 适配，不增加路径权限框架。
- 本篇是用户阅读行为的新增合同；可视化规格同步说明共享组件与两种来源，既有 canonical 快照/权限/禁网条款仍有效。

## 10. 验收与可行性门

### 功能

1. 本次真实消息的 12 个 Markdown 链接分类正确；报告、脚本、CSV/JSON 和 HTML 按各自类型打开，不再请求应用根路径。
2. 没有 render occurrence 的 HTML 可展示；已有 occurrence 的同路径文件被修改/删除后，旧卡片保持原快照，链接预览显示当前文件或真实缺失。
3. 已渲染 Markdown 及嵌套文件相对链接各自使用正确基准；Markdown 图片不会替换主文档。切换会话、快速点击两个文件、关闭后晚到创建/action/page/DELETE 不串内容、不撤销新实例。
4. 大文本/CSV 分页完整且不乱码、不丢多行字段；覆盖超长物理行、多行超预算记录、大量空字段及 JSON 编码膨胀的明确降级。大 Markdown 源文分页不假冒完整排版。并发 Range/下载字节与源文件一致，取消后句柄释放，代码/CSV 不执行。
5. 静态资源案例覆盖 CSS `url()`、本地 JS、ESM、字体、JSON fetch、资源缺失、中文/空格文件名、嵌套目录；不靠 CDN 缓存掩盖依赖缺失。
6. 桌面与窄屏、键盘焦点、关闭与恢复滚动、HTML 图例/hover交互；组件复用不破坏原可视化折叠与主体测量。

### 边界与失败

7. 无 token/旧 token/错误 connection/跨站 POST/伪造 Host 不能读取文件或启动原生动作；只读预览可适用于现有 observer，不能提升为 controller。
8. 目录穿越、重复编码、反斜杠歧义、符号链接、检查后替换、目录枚举、特殊文件和附属资源越根有明确拒绝。
9. 在实际受支持的 Chromium 与 macOS Safari 浏览器中验证主 DOM/API/cookie/storage 隔离，以及外部请求、iframe自导航、顶层导航、弹窗、表单、下载和消息伪造；只测试 jsdom 不算通过。
10. 主 HTML 与嵌套 HTML 的读 URL 直接打开也受响应头隔离；资源 CORS 不扩散到其他 API，撤销能力不因缓存恢复。请求中断、连接替换/stale/detach、Host 关闭、失效预览按上述合同释放资源；不把浏览器 crash 误作已收到断开通知。
11. 不增加 canonical row/event/subject/guard/job，不写 provider SYSTEM/tools/messages，不调用模型或 render tool，不创建文件副本、目录压缩包或新持久内容身份。

上述合同已按 §12 的范围完成实现与验证；既有 canonical 快照和身份合同继续由原测试覆盖。

## 11. 依赖与浏览器依据

已核对本机 aiohttp 的 `FileResponse` 构造及 `_make_response` 实现；其公开参数和流响应机制见 [aiohttp Server Reference](https://docs.aiohttp.org/en/stable/web_reference.html)。

iframe 权限、opaque origin 与 `srcdoc` 相对地址行为依据 [MDN iframe](https://developer.mozilla.org/en-US/docs/Web/HTML/Reference/Elements/iframe)；HTTP sandbox 约束依据 [MDN CSP sandbox](https://developer.mozilla.org/en-US/docs/Web/HTTP/Reference/Headers/Content-Security-Policy/sandbox)。父容器约束子 frame 装载的能力见 [MDN CSP frame-src](https://developer.mozilla.org/en-US/docs/Web/HTTP/Reference/Headers/Content-Security-Policy/frame-src)。这些资料支持选用浏览器原生机制；本地 URL、CORS 与双层 frame 的具体组合仍以 §10 的真实浏览器结果为准。

CSV 解析器接口和字段限制见 [Python csv 文档](https://docs.python.org/3.12/library/csv.html)；字段限制不等价于整条记录的输入预算。frame 导航约束的规范依据见 [CSP Level 3 frame-src](https://www.w3.org/TR/CSP3/#directive-frame-src)。

## 12. 实施与独立代码复核

初始计划已先提交于 `402c1975`。实现沿本篇的文件/连接 owner 落地，未修改 provider 输入、可视化订阅语义或数据库结构。

验证证据见 [会话文件预览验收记录](dogfood_evidence/session_file_preview/README.zh.md)：

- 后端预览、browser bridge 与 HTTP surface 共 **33 项通过**；前端 app、预览、Markdown、工作台、任务视图与 runtime adapter 共 **277 项通过**。类型检查、相关 ESLint、Ruff 和本地打包通过。
- 真实 `little_snake` 会话的 12 个链接可阅读，四张 Plotly HTML 在弹窗内可见；390px 窄屏无应用级横向溢出，原有可视化卡片再次开合保留同一 iframe。
- Chromium 和原生 macOS Safari 均验证 canonical/local 两种装载，各 10 个攻击样本确认执行且无越界请求；静态 CSS/ESM/JSON/字体/图片和 Plotly 成功。真实顶层导航均返回 403 纯文本。
- critic 首轮发现 Markdown 源文切换、CSV 失败重试、变化文件撤销、普通流释放、嵌套链接重试五项问题；均已修复并加入回归。critic 独立重跑后端预览 8 项和前端预览 8 项，复核结论为**无剩余必须修复项**。Safari 入口策略、宽列展示及失败占位也已交叉核验。

HTML 静态资源仍不是事务快照，PDF 依赖浏览器查看器，Office 通过下载/系统应用打开；这些是当前产品合同，不宣称已经提供转换或任意网站托管能力。
