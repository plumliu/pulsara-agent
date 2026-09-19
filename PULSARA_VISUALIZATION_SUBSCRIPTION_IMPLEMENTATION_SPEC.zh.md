# Pulsara HTML 可视化订阅与回看实施规格

状态：**设计冻结，首版已实施并完成交叉审阅**。日期：2026-09-20。

## 0. 产品形状

这是一个轻量的对话展示工具，不是内置浏览器、网页托管服务或可视化项目系统。模型用现有文件工具写一份自包含 HTML，再调用 `visualization_render` 登记“在接下来一条无工具调用的 assistant message 下展示它”。前端从该消息的 canonical 展示结果得到一个内部展示引用，在对话里用隔离 iframe 渲染；展示标记不由模型写入消息正文，也不进入 provider 输入。

普通订阅只登记进程内意图，不读取文件、不启动浏览器、不写 HTML blob。模型可以在订阅后继续编辑。目标 assistant message 提交前，runtime 才读取当时的文件并冻结确切 HTML 字节；消息、HTML blob 与有序展示结果在同一 canonical 事务中提交。后续编辑或删除工作文件都不改变历史展示。

`review=true` 是同一工具的可选即时回看：它不替代订阅，只在该次调用中读取当前版本、做一次性截图，并沿现有工具图片链路交给支持图片输入的模型。普通展示和历史重载不启动截图浏览器。

即使待处理 steer、Stop Hook 或其他 continuation 使 turn 继续，只要这条无工具调用 assistant message 已成功提交，展示就属于这条消息并消费这一批订阅；不等待真正结束 turn 的后续消息，也不重复展示。

本规格服从 [AGENTS.md](AGENTS.md)：保持 provider-input prefix 连续性、只增加必要的持久化事实、实施时 hard-cut，不保留旧路径兼容。

## 1. 首版边界与 owner

| 事项 | owner 与首版合同 |
|---|---|
| HTML 创作和删除 | 现有文件/terminal 工具及其权限、Hook、确认；`visualization_render` 不写文件 |
| 当前轮订阅 | active-turn runtime 的进程内有序槽位；崩溃后允许丢失，不恢复 |
| 即时回看 | 仅显式 `review=true` 使用一次性隔离截图；截图复用现有工具图片与 `image_ref` |
| 最终冻结 | assistant message publication 在提交前读取来源，调用现有 PostgreSQL blob owner |
| 历史展示 | 该 assistant message 拥有零到多个有序 `READY` / `FAILED` 结果；前端只投影它们 |
| HTML 执行 | 前端隔离 iframe；不提供任意文件、资源或网页导航服务 |

工作文件是普通完整 HTML，默认放在 `<workspace_root>/.pulsara/visualizations/<descriptive-name>.html`。它应能由用户直接用普通浏览器打开。这里的相对路径与现有只读文件工具一样锚定本次调用冻结的 `workspace_root`，**不跟随 terminal 的进程 cwd 或先前的 `cd`**；模型若在其他目录创建文件，应传 workspace-relative 或绝对路径。标准目录只是创作约定，不是新权限边界：用户指定的其他本地路径仍走现有路径解析和权限语义，不能仅因绝对路径或 `..` 离开 workspace 就被本工具额外拒绝。

首版嵌入展示要求**自包含 HTML**：样式、脚本、数据与图片随同一个 HTML 文件提供，可使用内联 SVG、Canvas 和 `data:` 图片。需要第三方库时，模型可通过现有授权工具取得并打包进这个文件；`visualization_render` 不安装、注入或映射库。嵌入展示与回看不加载 CDN、开发服务器、工作区配套文件、`file:` URL 或其他外部资源，也不开放 `fetch`、XHR、WebSocket 等网络出口。运行时不为此自造完整的 HTML/JavaScript 静态资源分析器；若作者仍写了外部依赖，请求会在隔离执行时被阻止，该 HTML 不保证正确显示，已提交的 `READY` 不因此倒写成 `FAILED`。用户脱离 Pulsara 手动打开工作文件时，由普通浏览器及用户环境决定其行为；Pulsara 不承诺替用户管理那条路径。

若创作的是图表、卡片或单个组件，作者可在希望展示的**唯一元素**上加 `data-pulsara-visualization-root`，例如 `<main data-pulsara-visualization-root>…</main>`。这是普通浏览器会忽略的可选 HTML 属性，文件仍是完整、可独立打开的网页。若创作的是完整网站页面，则不加标记，展示整页。不能猜测 `<main>`、`body` 或任意最大的元素为主体，也不能把主体选择另存为工具参数、blob metadata 或 canonical 字段。

因此首版**没有**内置 ECharts/Vega-Lite/D3 profile、`_vendor` 目录、库 metadata、资产 manifest、版本保留表或通用资源代理。若以后要让多文件或联网 HTML 在 Pulsara 内嵌展示，必须另立产品合同，不能推断现有 terminal 联网批准自动覆盖浏览器请求。

## 2. 工具接口与订阅

工具只有一个，来源二选一：

```json
{"path":".pulsara/visualizations/example.html","review":false}
```

```json
{"visualization_ref":"sha256:...","review":false}
```

- `path` 是非空本地路径；相对路径按本次调用冻结的 `workspace_root` 解析，沿用现有只读文件工具的 `~`、绝对路径与 `..` 语义。登记时只冻结经现有 owner 规范化的闭合路径值；review 与最终物化均使用同一值，不重新按后来变化的 cwd 解释，也不跳过实际读取时的既有权限和文件检查。
- `visualization_ref` 是模型已获知的、已提交 `READY` HTML 内容引用。它不是数据库 row/blob ID；解析时必须验证当前 session/workspace 存在拥有该内容的 canonical `READY` 展示结果，知道摘要本身不授予访问权。
- `review` 是可选布尔值，默认 `false`。`path` 与 `visualization_ref` 必须恰有一个；顶层不接受其他字段。

模型可见的工具及三个参数说明必须交代实际工作流和分岔：先用现有工具写/改完整的自包含 HTML；组件/图表可标记唯一主体，网站整页不标记；普通调用只预约下一条无工具调用的回复展示，允许继续修改，发布时才取最终文件；删除文件会取消该路径的待展示；`review=true` 当场尝试截图供模型检查，但不是最终版本锁定；无图片能力、读取/截图失败时仅回看失败而订阅仍有效；已发布的引用可在同一会话中重新订阅，不应臆造未知引用。描述只使用模型完成任务所需的概念，不泄露 canonical row、epoch、owner、exact-join 等内部机制。

普通调用在现有 schema、权限、Hook 与 attempt 流程允许该工具执行后，只生成闭合来源值与进程内待登记 token；被拒绝的调用不生成 token。只有对应 canonical `SUCCESS` ToolResult 已提交或经现有 exact confirmation 确认后，active-turn owner 才原子 install 该 token；失败、取消或未确认时 discard，不提前留下可物化槽位，也不新增持久事实。成功调用只返回简短状态：

```text
Visualization is subscribed for this response.
```

再次调用同一来源不是模型错误，也不得标作 duplicate、ignored 或 warning；统一的中立状态不提前声称自己是首次登记。`review=true` 每次都重新回看当前来源，不能因已订阅而跳过；成功时在相应状态后附加 `Current preview attached.`。工具结果不包含 HTML、blob ID、路径内容摘要，也不假称普通订阅已验证或持久化文件。

同一批待消费订阅以规范化路径或完整 `visualization_ref` 去重，第一次已确认 `SUCCESS` 结果的 token 被原子 install 时决定展示顺序。进程内 owner 提供原子 insert-if-absent；并发同源调用只形成一个槽位，所有成功调用返回同一中立状态。不同来源各占一项，一个调用只订阅一个来源。不为判断内容相同而提前读取文件，也不跨路径与引用来源去重。

带工具调用的 assistant message 不消费订阅。下一条无工具调用 assistant message 及其展示结果写入成功返回，或不确定回执经现有 exact confirmation 确认为同一 winner 后，消费这一批。若提交未确认，不得提前清空；若 turn 取消、失败或始终没有这样的消息，丢弃进程内订阅。订阅不写入 snapshot、不跨进程重启/新 Host 恢复，也不由子代理继承；**同一进程的 active-turn owner 在已采纳的 compaction successor 或前端 UI reconnect 后仍保留已确认的槽位**，不能把这些过程当成隐式取消。已消费后同一 turn 若继续执行，新的显式调用可开启下一批，包括再次订阅相同来源。

### 删除即取消

路径来源以目标消息物化读取时的真实文件存在性为准。可靠的 not-found 表示取消该项：不产生 `READY`、`FAILED`、HTML blob、工具补充结果或 durable 取消记录；同批其他结果保留顺序并重新编号为连续 ordinal。删除后在读取前重建同一路径，则使用新内容。权限拒绝、I/O 错误、非普通文件或无法可靠判断的读取错误不得伪装成 not-found。引用来源没有本地路径，不适用删除取消。

用户 steer 只在下一个 safe point 才能指导模型；模型经现有授权工具在物化前删除文件才能取消。若展示消息已先提交，随后才消费的 steer 不能追溯撤销它。已有 review 图片也不因后来删除工作文件而回滚。

## 3. 显式回看

`review=true` 先接受订阅，再对该次调用时刻的路径字节或已冻结引用做一次性截图。它不建立 HTML 草稿 blob；模型看完截图后仍可编辑路径，最终展示照样以目标消息提交前读取的版本为准。

截图使用已有 canonical 工具图片 occurrence、图片 blob、derived user-role 图片 carrier 和 `image_ref`；模型日后可用 `view_image(image_ref=...)` 重读像素，但不能据此恢复 HTML。现有 typed 工具图片结果、图片资源额度 quote/校验、图片结果交付以及 compiler 来源解析目前都按 `view_image` 收口；实施时只把 `visualization_render(review=true)` 的成功截图纳入这些既有 owner 的窄分支，以 tool name、call ID、冻结参数和图片 occurrence exact-join，不创建通用媒体框架或第二套截图存储。`review=true` 调用在执行前按现有图片 owner 预留本次调用的资源额度，实际截图仍须经现有图片验证并在该额度内；`review=false` 不预留图片额度。

图片交付要求必须从**实际已结算的 typed 图片内容及其 canonical image occurrence** 推导，不能仅凭 `visualization_render` 工具名与 `SUCCESS` 推断。settlement、正常历史读取和 late outcome 均用同一闭合判别：只有实际带一张图片的成功回看按图片附件 FULL 交付；普通订阅或“订阅已接受、回看未生成”的纯文本成功结果不附图片，也不错误占用图片交付语义。现有 `view_image` 成功必须带图片的既有不变量保持不变；不为分类增加持久标志。截图遵循现有图片像素与字节边界，首版采样视口定为 **1200×800 CSS px、DPR 1、非 full-page**；这是模型回看的固定采样，不承诺与用户当前 UI 尺寸逐像素相同，不增加 turn 或任务总量上限。截图按 §5 的同一可选主体规则选择画面：唯一、可见且整个边界位于该采样视口中的主体，直接用浏览器的元素截图；无标记、多个标记或主体不适于安全裁剪时仍截固定视口整页，不失败或猜测其他主体。回看和用户 UI 的视口不同，因而不承诺像素一致。

若当前模型不支持图片输入，或本次读取、隔离加载、截图失败，工具结果仍如实说明订阅已接受以及“本次回看未生成”的公开原因；不附图片、不生成新的 `image_ref`、不回退到旧截图，也不撤销订阅。随后修复文件再调用 `review=true` 可以重新尝试。普通 `review=false` 调用不启动浏览器。

回看与前端展示使用同一自包含、禁外部资源的 HTML 策略。一次性截图可使用维护中的浏览器依赖及其受限上下文；Pulsara 只负责来源、资源策略、截图物理边界和图片结算，不实现自己的浏览器引擎。现有通用 physical-I/O watchdog 会在逻辑超时后等待线程物理退出，不能单独用它保证卡死页面可终止。截图 owner 必须为浏览器加载、脚本等待和截图设置依赖自身的逐操作 deadline，并使该次隔离 context/浏览器进程在超时、取消或 Host close 时可以被终止和回收；不得把不可终止的浏览器调用留在需 join 的线程中。调用超时而 turn 仍在运行时，物理退出确认后按纯文本回看失败结算且订阅仍保留；turn 取消或 Host close 时则按 §2 丢弃未消费订阅。不增加 durable job 或浏览器调度框架。

## 4. 消息提交与 canonical 真相

模型生成订阅后的下一条无工具调用 assistant message、但尚未完成 canonical publication 时，publication owner 一次性物化当前批次：

1. 按订阅顺序取得来源；路径按现有 filesystem owner 读取当时的完整字节，可靠 not-found 按 §2 取消；引用按当前 session/workspace 的 canonical `READY` owner exact read。
2. 只做提交前可确定的检查：普通文件、现有单次内容大小边界、非空 UTF-8 文本。工具描述要求作者提供完整 HTML 文档，但 publication 不自造 HTML 语法解析器，也不为了“预验证”启动浏览器；脚本、解析或资源加载问题属于展示时观察，不能在此阶段伪装成已知物化失败。
3. 冻结本次候选的确切字节或公开失败结果。同一 assistant 候选的提交/确认重试沿用同一冻结值，不重读可变路径。
4. 在该 assistant message 的同一 canonical 事务中，成功路径内容通过现有 blob owner 保存为 immutable `text/html`；成功引用复用已有 blob；各项有序 `READY` / `FAILED` 结果与消息一起提交。事务失败则消息、展示结果和本次 blob 都不成立。
5. 消息提交确认后消费订阅。即使 steer 或 Stop Hook 使 turn 继续，也不把该批展示挪到后续消息。

不保存 `write`/`edit` 中间版本、每次订阅版本或 review 所见 HTML。历史 UI 不能回读原路径；同一 workspace、相同 media type/codec 与相同字节可由现有 blob owner 自然复用，但每次已提交展示仍有自己的消息 occurrence。当前 blob ID 只由 workspace 与内容摘要构造：若相同字节已以其他 media type/codec 发布，再写 `text/html` 会报 identity conflict 并错误地失败整条消息。clean-v0 hard cut 必须让 blob 身份包含完整的 workspace、media type、codec 和内容摘要；逻辑内容摘要本身保持按字节计算，不增加 DTO fingerprint 或第二套 blob store。同字节跨 media type/codec 为不同 blob，同类型同字节仍 exact-reuse；HTML `visualization_ref` 继续使用原有内容摘要表示，并以 `READY` owner 验权，不能把 blob ID 充当引用。**同步 hard-cut 所有只凭 workspace＋摘要反算 blob ID 的消费者**：尤其 `PostgresCanonicalImageReferenceReadPort` 的 `view_image(image_ref=...)` 候选定位与 canonical refs/descriptor 校验，必须先由当前 session/workspace 的 canonical 图片 owner 和其 blob/descriptor 得到真实 media type、codec，再按新完整身份 exact-join 并读取；`image_ref` 仍是逻辑摘要，不能增加旧 ID fallback 或双读。

唯一新增 durable 产品关系是 assistant message 的有序展示结果，包含所属 assistant entry、连续 ordinal；新执行的结果还须 exact-join 最初成功订阅的 canonical tool result，fork 导入结果的来源规则见下文。结果种类互斥：

- `READY`：HTML blob 引用；没有失败原因。
- `FAILED`：安全可公开的失败分类与简短说明；没有 HTML blob 或 `visualization_ref`。

该关系的理由只有历史 HTML 稳定展示与历史失败占位可重载。新执行的展示只由 assistant message publication 写入；须约束同 session/workspace、assistant entry kind、连续 ordinal 和 `READY`/`FAILED` 字段互斥。`EXECUTED_TURN` 展示与其 assistant entry、首次订阅的 canonical tool result 同 turn exact-join；fork 产生的 `IMPORTED_HISTORY` 展示随已复制的 assistant entry 归属 imported group，`turn_id` 为 NULL，不能套用执行轮的同 turn 约束。不要为此新增订阅表、事件、subject slot、guard、job、receipt、checkpoint、恢复机制、版本链或通用 artifact registry。

提交前已知的权限、读取、编码、大小或引用解析失败，可作为 `FAILED` 随消息提交；其他成功项照常展示。数据库、blob 或消息事务失败是整次 publication 失败，不能转换成已提交的 `FAILED`。消息提交后浏览器才遇到的脚本错误或被阻止的资源请求，只是当次前端展示/回看的运行时错误，不能倒写 canonical 结果。`FAILED` 占位必须在历史重载后仍可见；可靠 not-found 取消没有占位。

`visualization_ref` 只从已提交 `READY` HTML 的现有内容摘要派生，同内容可有相同引用；解析时仍要 exact-join 当前 session/workspace 的 canonical owner。不借 tool result artifact 字段承载 HTML，也不向工具结果提前返回尚未提交的最终引用。

会话 fork 若复制了拥有展示结果的 assistant entry，必须在同一 fork 事务中复制其有序 `READY` / `FAILED` 结果，重映射 assistant entry ID，并沿用同 workspace 的 immutable HTML blob 与公开失败说明；否则子会话历史会丢展示，`visualization_ref` 也无法在子 session 验权。若首次订阅 tool result 也在 fork 的有效历史内，重映射其 result entry ID；若因合法的有效上下文 cut 未复制该 tool result，导入的展示只由复制后的 assistant entry 和结果 blob/失败说明拥有，不伪造 tool result、attempt 或跨 session FK。执行轮展示始终必须保有本轮订阅结果归属；导入展示不成为新的订阅执行事实。fork material reader 必须 exact-read 并验证所有被复制 `READY` 的 blob；任一 owner/blob 不一致时 fork 事务整体失败，不静默丢项。

现有 blob GC 仅检查既有引用表；新 `READY` 展示关系也必须进入其可达性检查，直到最后一个拥有它的消息/会话被合法移除才允许回收 HTML blob。fork 复用同 workspace blob，不复制字节或建持久计数器；`FAILED` 没有 blob。实施时同时测试原会话、fork 子会话和源消息清理后的历史读取与 GC，不增加独立保留 job。

## 5. Provider 与前端投影

普通订阅只产生正常 tool call 和短文本 tool result。HTML 字节、展示标记与前端 iframe 不进入 provider prompt。仅显式回看图片进入现有工具图片 carrier；本功能不创建新的 SYSTEM/tools/messages 重建边界。

最终 `visualization_ref` 直到所属 assistant message 提交后才存在。若以后发生 provider call，compiler 首次将该 assistant entry 追加到 provider 历史时，紧随它派生一个不显示于 UI 的稳定 user-role metadata carrier，逐项告知实际 `READY` 引用；没有后续调用则不额外唤醒模型。carrier 不写进 canonical 用户正文、不改变 recent human 或 Hook/Skill 匹配，后续调用保持同一历史位置和字节；compaction 随所属 assistant evidence 取舍，不维护全会话引用清单。`FAILED` 和取消项不产生引用。

前端收到的是 assistant entry 所拥有的有序展示结果，效果类似识别一枚内部可视化标记，但**不解析模型正文中的任意路径或伪造标记**。协议在 `CanonicalEntry` 暴露 `READY` / `FAILED` 摘要；HTML 字节沿现有内容读取链路增加窄目标 `(session_id, assistant_entry_id, ordinal)`，读取时重新验证该 `READY` owner，不能用 blob ID 或摘要直接绕过授权。`FAILED` 摘要足以显示占位，无 HTML 读取。

assistant 正文与工具卡仍在普通对话版心；每个 `READY` 结果在它所属消息下独占一行，不继承正文 max-width，但也不强制铺满中央工作区。未标记主体时展示整页，宽度上限 `960px`、高度上限 `min(70dvh, 560px)`，超出部分在 iframe 内滚动。若 HTML 中恰有一个 `data-pulsara-visualization-root`，在独立于外层裁剪尺寸的 iframe 采样视口中量取该元素的实际渲染边界，只在边界有限、可见且完整落于采样视口内时裁出主体；展示宽高取主体尺寸，仍受上述宽高上限约束。主体旁的整页背景不会填满对话区。面板开合、响应式布局、字体或内容变更后重新量取；不能因外层缩小导致 iframe 视口随之缩小，再引起不断重排的反馈环。标记缺失、重复、隐藏、越过采样视口或测量无效时安全退回整页视口，不猜主体、不把它记成 durable 失败。作者应让想单独展示的主体在采样视口内响应式适配；本工具不替任意超宽/超高网页重写布局。面板不能进入侧栏下方或造成应用级横向滚动。`FAILED` 用短占位说明，不伪装为工具卡。

隔离 iframe 只向父页面报告“整页/主体”与有限边界数据；父页面仅接受**该 iframe 的**消息，校验数值和边界，不接收 HTML、路径、脚本或导航命令。测量协议是可丢失的前端布局观察，不新增持久状态、前端回执或跨进程恢复；没有测量结果时展示整页。作者脚本也在该隔离 iframe 中，因此不得把消息里的几何数值当成授权或可信内容。截图直接使用浏览器元素截图，不另造 HTML 解析/裁剪引擎。

前端只用受限 iframe 显示 canonical HTML，不把它注入 Pulsara 主 DOM。iframe 可执行该 HTML 的内联可视化脚本，但必须隔离主页面 DOM、凭据、cookie、存储和内部 API，阻止顶层导航、弹窗、下载、任意本地文件及外部网络资源。可以复用现有内容读取并在 iframe 中装载字节；若为施加浏览器 CSP 需要一个文档响应入口，它只能服务经授权的 canonical occurrence，不能变成任意路径/资产服务器。实施必须用浏览器测试实际证明上述隔离与禁网，而不能把 terminal 的网络权限当成浏览器授权。

## 6. 明确不做

- 不提供 HTML 参数、可视化专用 write/edit/patch、草稿版本、撤销/分支、latest pointer 或跨重启订阅恢复。
- 不提供通用浏览器导航器、站点托管、分享 URL、任意本地资源映射或内嵌联网代理。
- 不内置或自动注入图表库；不提供 `_vendor`、profile metadata、资产 manifest 或旧库版本保留机制。第三方代码若用于首版嵌入展示，应由现有授权工具打包进自包含 HTML。
- 不因脚本运行时错误修改已提交 canonical `READY`，也不为失败另开事件、表或后台修复任务。
- 不把图片 `image_ref` 当作 HTML 引用，不让前端从历史路径重读文件，不把 HTML 自动重放进 provider 输入。
- 不增加 feature flag、旧/新双轨、兼容 schema 或迁移期 fallback。

## 7. 实施与验收

实施前检查现有 builtin catalog、DirectKernelToolPort、权限/Hook、active-turn owner、assistant publication 与 exact confirmation、PostgreSQL blob、图片 result/compiler、terminal canonical 内容读取和前端消息投影。只扩展这些 owner 的必要窄字段与分支，不复制工具、存储、图片或浏览器框架。开发期采用 clean-v0 hard cut，生产代码、schema baseline、协议、前端、工具描述与测试只保留新合同。

至少证明：

1. 普通订阅不读文件、不启动浏览器、不写 HTML blob；并发和重复同源调用只有一个有序槽位，状态文案中立。ToolResult 未确认 SUCCESS、确认失败或取消时不 install；已确认后 install 与 assistant publication 的顺序用 settlement 故障测试证明。
2. 订阅后继续编辑只展示消息提交前的最终版本；带工具调用的 assistant message 不消费订阅；可靠 not-found 静默取消，其他已知读取失败随消息提交 `FAILED` 且重载仍可见。
3. 待处理 steer 使 turn 继续时，已提交的无工具调用 assistant message 立即展示并消费订阅，后续消息不重复附着；提交不确定回执的确认重试不重读路径。
4. `READY` HTML 与所属消息同事务提交，修改/删除原文件不影响历史；数据库或 blob 事务失败不留下孤儿结果。相同字节跨 media type/codec 发布不会碰撞，同类型同字节 exact-reuse，`visualization_ref` 仍按当前 session/workspace 的 `READY` owner 验权。现有图片 `image_ref` 的 canonical refs 校验和 `view_image` 重读在新 blob 身份下仍正确，且无旧 ID 兼容路径。
5. `review=true` 每次查看当前版本；成功截图通过扩展现有 typed 图片结果、额度 quote/校验、交付和 compiler 来源分支形成可重读的 `image_ref`，不增加独立图片存储。settlement、历史读取和 late outcome 对实际带图片的成功结果要求 FULL；普通订阅、无图片能力模型和回看失败均只有纯文本结果，不伪造图片附件或 FULL 图片语义，订阅仍成立。
6. 前端从 canonical occurrence 展示而非解析模型正文或可变路径；HTML 不进入 provider 输入，引用 carrier 在下一次真实 provider call 中稳定追加，冷启动、append、compaction、reconnect 不破坏已安装前缀。
7. 工作文件无需 Pulsara 专用 metadata 即可由普通浏览器打开；Pulsara 嵌入与回看只接受自包含资源策略。浏览器测试覆盖外部请求、`file:`、主应用 DOM/API/凭据、导航/弹窗/下载的阻断，以及无标记整页、唯一标记裁出主体、重复/无效标记安全退回整页、动态尺寸与面板开合后重测、主体外背景消失、超出正文版心但不强制铺满工作区的布局。模型可见工具/参数说明须让模型正确选择组件主体或整页路径，并理解普通订阅、即时回看、继续编辑、删除取消和失败后继续的结果。
8. 真实 provider dogfood 覆盖 `write` / `edit` → subscribe → review → edit → 无工具调用 assistant message 展示，并保留实际工具结果、截图 `image_ref`、`READY` HTML blob 和失败/取消边界的可复核证据；证据只排除真实凭据值。
9. 截图浏览器遇到永不结束的脚本/加载或取消时，单次操作 deadline 与进程终止使物理执行可回收；仍在运行的 turn 获得“订阅已接受、回看未生成”的纯文本结果，Host close 不被卡死页面无限阻塞。
10. fork 复制范围内的 `READY` / `FAILED` 展示随 assistant entry 导入，归属重映射正确；有效 cut 省略源 tool result 时不伪造执行事实。原会话或 fork 子会话仍引用 HTML 时 GC 不删除 blob，合法移除最后一个 owner 后才可回收。
11. 相对路径始终锚定冻结的 `workspace_root`；terminal 后续 `cd` 不改变订阅目标，review 与最终读取指向同一规范化路径，绝对路径和 `..` 继续走现有权限语义。
12. 同一 active turn 中 subscribe → compaction successor／前端重连 → 最终消息仍展示；崩溃和新 Host 不恢复槽位。

设计冻结不代表自动验收；backend、storage、compiler、frontend、浏览器与真实 provider 的证据均应与代码一起复核，交叉审阅发现的实际问题须在标记实施完成前修复。
