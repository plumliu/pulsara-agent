# 浏览器尾部首屏、历史按需分页与稳定定位实现方案

日期：2026-10-18

状态：已按审阅方案实施，并完成 GPT-6 Astra high 独立代码交叉检查；真实长会话验收与最新加载界面调整记录见 §13。

范围：浏览器读取、投影和展示 canonical 会话记录。本文作为本次实现规格；不改变模型上下文、执行权或既有归档/工作目录恢复限制。

## 1. 目标与产品行为

打开长会话时直接呈现最近一段记录及当前运行状态。较早历史在用户向上滚动或明确定位时按需读取；不先读完整个会话，再让页面从开头追赶到底部。

同时完成四件事：尾部首屏、向上分页、稳定滚动锚点、实时观察不等待历史和正文读取。仅隐藏旧 DOM、但仍全量读历史与正文，不算完成。

- 首次进入显示最新区域；首屏定位使用无动画定位，避免可见的全程滚动。恢复一个明确的旧消息目标时，以该目标优先。
- 接近已加载区域上边界时请求下一页；保留可键盘操作的“加载更早记录 / 重试”入口。全部历史始终可达，没有总条数或总轮数上限。
- 回看历史时，新进展不强拉到底部；“回到最新”显式返回最近区域。
- 未加载的历史有清楚的边界提示。它不代表记录不存在、任务未发生或上下文被压缩掉。
- 历史页读取失败只在对应区域展示失败与重试，不把仍然在线的会话改成断线，不清空输入草稿。
- 既有只读历史视图采用同样的分页与定位体验，保持原有写操作限制。归档管理仍要求取消归档才能打开会话，不新增归档会话直接打开入口；session-scoped canonical 读取继续按其原授权合同提供只读能力。

## 2. 实施前基线

下表记录本次改造开始时核对的生产路径，相关全量预读、等待和局部推断路径已在本次 hard cut 中移除；它不是保留旧实现的要求。

| 当前路径 | 事实与影响 |
| --- | --- |
| `frontend/lib/runtime-adapter.ts`：`backfillOlderHistory()` | 已有 `HistoryCursor` 和单页读取，但循环读尽全部旧页。 |
| 同文件：`LocalRuntimeConnection.initialize()`、`snapshot()`、`refreshCanonicalAfterQueueTransition()` | 在线初始化、快照刷新、队列交接都可能回到全历史预读；不能只改首次打开。 |
| 同文件：`CanonicalHistoryView.initialize()` | 只读历史也读尽旧页和正文。 |
| 同文件：`observe()`、`hydrateProjectionContent()`、`hydrateCanonicalContent()` | 每次观察还等待已加载 entry 的正文、blocks、reasoning 和队列正文；正文读取可阻塞下一次观察。 |
| `frontend/app/pulsara-app.tsx`：观察循环 | 等 `connection.observe()` 返回才发布投影并继续观察。 |
| `frontend/components/workbench-view.tsx` | 跟随最新和 ResizeObserver 根据内容高度追底；分组 key、定位、高亮依赖已挂载消息。 |
| `src/pulsara_agent/terminal_protocol/canonical_v3.py` | snapshot 已经是有界尾页；`history_page()` 保留 entry/event 双 cut，按所有 scope 的 canonical entry 分页，不是按 ROOT 气泡数分页。 |
| `src/pulsara_agent/web_app/session_controller.py`：`_history_reader()` | 每次按 memory domain 授权，只读，不需要 writer、Host 或浏览器 attachment；现有 session-scoped history/content 读取可服务在线会话。 |

实际长会话曾出现 connect 13.54 秒、随后两页历史读取 4.53 秒 / 677 entries，再遇到 `LIVE_RING_OVERFLOW` 的样本。证据见 `output/runtime-million-20261018/case2/reconnect-investigation/`。

本方案减少前端等待历史和正文的窗口，不承诺消除 connect 本身的耗时、所有浏览器调度延迟或所有 live 缺口。实际缺口继续遵守 `PULSARA_BROWSER_LIVE_GAP_REBASELINE_SPEC.zh.md`；不得悄悄跳过 live cursor 来降低 toast 计数。

## 3. 不变边界

### 3.1 模型上下文与执行

浏览器已加载的记录集合只是 disposable 展示投影，绝不是模型历史来源。

- 不修改 provider input compiler、installed epoch、SYSTEM、provider tools 或 messages 的安装和续接。现有 epoch 内仍保持 SYSTEM/tools 字节一致、messages 仅追加。
- 不新增 cold epoch、rebase 或自动压缩触发条件。向上翻页、跳转、折叠、刷新、缓存淘汰都不能触发模型上下文重建。
- 不把浏览器尾部窗口变成 LAST_N、模型记忆、分叉上下文或子代理继承上下文。
- 不启动、停止、重跑、重投递、确认或取消 ROOT、工具、子任务、队列项、计划、终端或定时任务。查看历史仍然是读取。
- 当前运行状态、权限和交互仍由现有 canonical control / live-control owner 提供；不从局部消息数量或 DOM 推断任务结束、权限或是否可提交。
- 模型端的历史检索工具与其返回语义保持原路径；本方案新增的是浏览器只读查询，不新增模型工具。

### 3.2 持久化和资源

- 不新增 durable event、event kind、subject、guard、业务表、关系、job、receipt、checkpoint、lease 或执行恢复机制。
- 不新增内容 fingerprint、窗口 fingerprint 或读取证据 registry。正文仍按现有 digest/size/media/owner 校验，这是既有 immutable content 边界。
- 不新增总历史、总任务、总页数、总轮次或浏览时长上限。页大小、正文 chunk 和响应字节上限沿用既有物理边界。
- 本方案不扩大 live ring，不复制协议调度器，不引入第二套数据库历史或 replay 执行。

## 4. 所有权与读取路径

| 内容 | 唯一所有者 | 浏览器职责 |
| --- | --- | --- |
| canonical entries、正文、工具原始结果 | 既有 PostgreSQL rows / blobs；`CanonicalProtocolReader` 与既有 content reader | 按身份读取、校验、缓存、展示；不写回执行事实。 |
| 当前执行、队列、计划、权限 | 现有 canonical control 与 command/query owners | 独立呈现当前值，不采用旧历史页中的“当前状态”。 |
| 临时流、当前 TODO / interaction | 现有 observer 与 live-control owner | 连续观察；缺口恢复和 TODO invalidation 沿用现有规则。 |
| 历史区间、正文需求、折叠与滚动位置 | `CanonicalHistoryReader` 及工作台的 process-local UI 状态 | 可丢失、可重读，不成为恢复任务的权威。 |

实施时，浏览器 canonical 历史、正文、图片、可视化和原始工具结果统一使用现有 `/api/sessions/{session_id}/history/*` 只读路径。在线和只读使用同一个 reader 与内容 owner；移除这些浏览器读操作经 controller attachment 的旧路径，不增加失败后切回旧路径的兼容分支。其他协议客户端不在本次删除范围内。

理由：`browser_bridge.py` 当前把 history/content 放在 controller，把 observe/live-control 放在 observer；`protocol_client.py` 的每个 attachment 内请求串行，且取消正在进行的 `_round_trip()` 会关闭 transport。已有 session-scoped 读取能复用相同内容校验与授权，又不占 controller，不需要另造一个 attachment 或控制优先级框架。

必须保留并验证：每次 memory-domain 授权、session/entry/block/queue/image 的精确 owner、内容完整性、工具 artifact 可用性和既有范围限制。统一入口不是权限绕行。

注意现有缺口：cold `history_page` 当前只传 `maximum_entries`，没有把客户端 `maximum_serialized_bytes` 传给 reader，而是使用 reader 默认值。实施时须在统一入口校验并传递该字节预算；不能声称当前已执行客户端的 2 MiB 请求预算。既有 content chunk 的 1 MiB 硬边界保持。

## 5. 分页与快照状态

### 5.1 分开维护三种位置

1. canonical history 页的 `(session_id, cut_sequence, event_sequence_cut, entry_sequence)`。
2. 当前 committed observation 的 event cursor。
3. live / live-control 各自的 owner epoch 和 revision。

历史分页只能推进自己的页游标，不修改另两类位置；正文完成不能推进任何 observation cursor。保留 wire uint64 的精确值与 optional event cut 的 presence，包括显式零；cursor 不经 `Number` 舍入后重建。

### 5.2 初始尾页

- 保留既有 snapshot 的 entry/event cut 和 control；不自动请求任何旧历史页。
- 在取得可用 connection/current state 后立即进入现有单消费者观察循环；首屏正文读取和渲染不作为启动 observe 的前置条件。
- 初始 snapshot、普通旧页和定位窗口统一经过 §7.1 的 page-context enrichment；连接与当前状态先就绪、observe 先运行；首屏 context 尚未就绪时保留恢复会话页，不逐条渲染分组占位；后续补页仅显示局部加载状态并保留已读工作台，不先猜测新记录分组。
- 尾页可能落在一个很长 ROOT 或大量子任务活动内部。不能为了凑够“完整一轮”或一定数量的 ROOT 气泡而自动向前读到会话开头。
- 用有界的展示上下文描述该区域属于哪个 turn/task，以及“较早过程尚未加载”；长过程按页展开。既有原子 entry 和响应上限保持，不擅自切碎 canonical entry。
- `snapshot()` 与队列交接刷新均移除全量 backfill。当前 control 的刷新和尾部元数据更新不清空用户已展开的不可变历史及校验后的正文。

### 5.3 有界向上分页和非连续区间

`CanonicalHistoryReader` 统一拥有在线、只读的单页读取与去重。每次用户需求只推进一个现有有界页，快速重复触发合并，不预排“直到开头”的循环。

分页请求保留其原 cut；响应验证 session、双 cut、方向与游标前进关系后才合入。快照或会话切换后，旧响应不能更改新游标。同一 session 的尾部快照刷新不自动废弃仍有效的旧历史 cut：旧页只推进其原区间中仍与发起时相同的 cursor；该区间已被替换或 reader 已关闭时丢弃。不能把旧页的历史 cut 安装为新的观察起点。

浏览器记录已实际读取的区间及其边界，不将“Map 中存在若干 entry”误当完整历史。定位得到的孤立窗口与最新窗口之间显示“中间记录尚未加载”，只能沿相应边界分页填补。不能把不同窗口简单排序后伪装成连续对话，也不能用所有 entry 的最小 sequence 替换唯一的 older cursor。

按 entry ID 去重；重叠页只合并相同 canonical 身份。缺号不由前端猜测补齐，覆盖范围由服务端有界查询的边界与分页连续性确定。不同 cut 的窗口保留各自游标，只有实际读取证明中间覆盖后才连接展示。

### 5.4 元数据与动态字段

entry 的内容身份和正文可以复用；不能把整份 `CanonicalEntry` 当永远不变。当前 `_entry()` 的 `root_final`、`fork_eligible` 来自当前 turn / imported group 的关系投影。

正文缓存与这些字段分离。普通重叠历史页只补缺失 entry 和可复用内容，不回写已由当前观察发布的 entry 展示字段；显式定位/重新读取采用原 canonical owner 的关系投影，不将字段差异报成正文完整性冲突。历史页不覆盖当前 control，不能用旧页的固定 cut 假装它是当前字段的 freshness。点击分叉仍由服务端实时校验，按钮状态不是执行授权。

正常最终 assistant 的接受与 turn 完成目前在同一 writer transaction；本方案不把“正常 final 先发布 false、再变 true”当已确认问题，也不为此新增轮询、终态事件或字段刷新 registry。

## 6. 实时观察与正文解耦

`observe()` 保持一个顺序消费链：读取 observation → 校验 → 合并 committed 元数据和 live/control → 安装已确认 cursors → 发布轻量投影 → 继续观察。保留既有工具 live/canonical reconciliation 和精确 gap 规则。

以下工作移出 observe 的等待链：读取旧历史、批量读取正文、读取 reasoning、下载 artifact、可视化内容和图片、恢复队列正文。浏览器主线程仍然共享，不能宣称解耦后渲染再慢也不影响观察。

正文状态必须区分 `unloaded / loading / ready / error`；`ready` 允许真正的空文本。当前 `decodeContent()` 会把缺少 inline 的内容变成空串，`decodePromptContent()` 会报完整性错误；两者都不能直接拿来处理尚未加载的有效引用。

- 页面只请求首屏、邻近预读区、用户展开或明确定位所需正文。折叠的 reasoning、工具原始输出和 HTML/图片继续按需求读取。
- 每个内容 owner 合并重复需求；按既有有限 chunk 逐次读取，不一次排入全会话的下载队列。不引入总下载次数或浏览寿命上限。
- 内容返回后校验 owner、digest、完整 size、media/codec，再标为 ready 并局部发布。可视化冻结资源和图片身份校验沿用原有 owner。
- 过期请求不写入新会话/新 owner；丢弃过期结果不等于删除原始记录。滚出视口不是关闭 controller 或 observer 的理由。
- 单条正文失败允许局部重试；真实授权失败或服务断开按既有错误处理，不以“局部加载失败”掩盖真实连接问题。

### 6.1 snapshot 与 observe 的安装顺序

当前输入状态核对也会从观察循环外调用 `connection.snapshot()`。现有 `replaceSnapshot()` 会推进 event cursor，而进行中的 observe 随后可能回写较旧的 cursor/control；解耦时必须关闭该竞态。

- 输入核对使用既有 receipt 与 exact consumed entry，不再额外 snapshot。
- 除首次启动外，确需重新采样 snapshot 的请求及安装进入现有单消费者观察链，等待进行中的 observe 自然返回后再执行；不能从正文、定位或历史读链反向触发 snapshot/publish。
- 不为取得安装顺序而取消正在进行的协议 round trip。等待和失败不阻塞独立的局部历史/正文读；旧页面保留，真实连接错误仍走既有处理。
- 安装 snapshot 时保留旧的已读区间；新尾页与旧区间之间未覆盖的内容仍显示加载边界。首次连接以外的 snapshot 不推进 observer 消费位置，只合并有界尾页和有 freshness 证据的 control；随后仍由 observe 消费原位置之后的全部 committed suffix，包括每次 adoption。snapshot 的新 cut 不能冒充已消费事件或已加载中间 entries。
- current control 使用协议已有 `control_read_event_sequence` 的读取位置判定其来源；它可以超前于本批 `through_event_sequence`。前者不是事件消费进度，不得用它跳过尚未观察的 committed suffix；旧消费批次也不能覆盖较新的 control。

这只是现有连接的顺序安装责任，不新增 durable 队列、锁服务、epoch 或 watermark registry。

### 6.2 live 到 canonical 的正文交接

当前 settlement 会移除 draft/live result，而旧实现等待 hydrate 才发布，掩盖了正式正文尚未读取的空窗。解耦后同一 canonical entry/工具关系保持一个稳定的展示宿主：可先显示“已保存，正文读取中”，不能让气泡消失、重复，或把缺少正文当空回复。

如短暂保留已收到的临时文本以平滑交接，必须标为临时展示，不能当已通过 digest 校验的 ready 正文；正式内容完成校验后替换。真实 `LIVE_RING_OVERFLOW` 仍按现行规格清理临时态，不为保住旧气泡跳过缺口规则。

队列的本地已知文本与正式 entry 同样按 `input_source` 交接：正式 metadata 到达不等于正式正文已经可读，不能提前丢掉唯一可见的用户文本、错误提示或尚未提交的草稿。local submission 的语义仍由原 command receipt 决定。

### 6.3 队列正文交接

`CONTENT_QUEUE_NOT_PENDING` 只表示 pending 正文来源已经退休，当前 wire error 不包含 consumed entry ID。不得从该错误凭空构造消费 entry。

在线时沿用 committed `input_source` 或既有 `queryCommand` receipt 的 `promptDelivery.consumedEntryId`，精确核对 command ID、queue item ID；需要正文时再按已提交 entry 读取。状态未确定则保留已有 unknown / unavailable 展示，不重新 SUBMIT，不全量 snapshot/backfill。

只读历史没有 command attachment 时保留现有不可用正文提示；不得为补正文恢复 Host。当前 pending/已消费状态与浏览器可见历史条数无关。

## 7. 精确定位：新增小型只读窗口查询

当前 `locateAnnotationSource()` 只能沿 older cursor 逐页回溯，不存在“按 ID 获取附近窗口”的现成 API。本方案明确新增该能力，不把当前能力夸大为已支持。

在既有 `CanonicalProtocolReader` 增加有界 anchor-window 读取，经同一 session-scoped history HTTP owner 暴露；不经 Host 命令，不新增模型工具或持久状态。

建议接口为 `POST /api/sessions/{session_id}/history/window`：

- 初次定位接受精确 `anchor_entry_id`；task 定位先用已有 task/inventory 的真实 parent/creator 身份解析目标，不接收任意客户端路径。
- reader 在一次一致只读读取中校验授权和目标属于此 session，取得配对 entry/event cut，查询包含目标的有界附近区域。锚点必须包含，预算分配不能让邻居占满响应而漏掉目标。
- 后续按服务端返回的双向 cursor keyset 分页。cursor 包含 session、双 cut、方向和精确边缘；不增加 fingerprint、nonce 或 durable cursor registry。
- 返回现有 canonical entry / content reference、已覆盖范围、前后边界；和其他页面一样经 §7.1 的共享 enrichment 获取中断/分组/压缩边界，不为 anchor 单开一套正确的分组逻辑。
- 单页 entries、字节、content chunk 沿用既有协议资源边界。一个 entry 或最小必要上下文装不下时返回有类型的 resource outcome，保留当前页面，不无界扩页或截断正文假装成功。
- 授权失败、会话已删除、来源不存在、目标超过给定 cut、资源不足分别保留真实错误。未加载绝不是“来源不存在”的判据。

窗口和展示上下文是新的浏览器只读 DTO，复用既有 canonical row/content 转换与授权；不是新的 canonical 真相或模型协议。实施时更新对应 HTTP 类型和契约测试，不另造第二个历史存储引擎。

### 7.1 所有页面共用的有界展示上下文

选定同一条路径：`CanonicalHistoryReader` 取得任意 canonical 页元数据后，以 session、原双 cut 和该页实际覆盖边界，调用新增 session-scoped `POST /api/sessions/{session_id}/history/context`。后端复用 `CanonicalProtocolReader` 的一个共享 builder 生成 page context。初始在线 snapshot、只读 snapshot、普通 older page、anchor 和 forward window 全部接入，不允许仅定位窗口有正确上下文。

observe 新到的 committed canonical entries 同样适用：按现有单批事件数和响应字节边界形成精确 entry ID 集合，交给同一 builder 的需求合并路径。该集合没有历史页的完整双 cut 时，由 reader 取得并返回合法配对 cut、校验每个 entry 的归属，不从局部最大 sequence 伪造 cut。context 未就绪只显示稳定的 loading / 已知 owner 组壳，不阻塞下一 observe，也不退回“最近 assistant / 局部 ROOT 数组相邻”的旧推断。无新增 canonical entry 的 delta 不逐条请求 context。

此读取不再次下载全部正文，也不为一页补齐整个 ROOT。builder 在一致只读事务中验证授权、cut 和有界覆盖范围，按该范围内的 canonical entries 查询：

- 真正的 executed turn / imported group 身份、段边界，以及当前页需要的 ROOT 前后关系；
- 可正向精确确定的 creator/batch 摘要、任务真实 parent 和现有分页入口；
- 对应 owner 的既有 interruption notices；
- 覆盖范围内、且 `event_sequence <= context_event_cut` 的已有 `CompactionAdopted` occurrences 及其真实展示边界，不只查询 latest compaction control。

返回的 `BrowserHistoryPageContext` 携带 session、配对 context cut、对应覆盖边界及上述有界投影。它不含 queue、active turn 等当前控制状态，不授予执行权，也不持久化。所有内容连同 envelope 受既有页字节边界约束；不足时返回明确的 resource outcome，不能默默漏标记或改成无限查询。

页元数据可以先入 cache；分组展示必须等该页匹配的 context 通过校验再安装。上下文未就绪、失败或资源不足时保留该区域的 loading/error 与重试，已有页面、composer 和实时观察照常工作。并发同页需求合并；重试只重读失败的 context，不重读全史。单次额外的有界 metadata 请求是本方案明确承担的成本，需计入性能测量。

context 请求只更改自己的展示区间，不推进历史页 cursor 或 observer cursor。初次 context 使用原页 cut；若后续出现新的 adoption / interruption 等相关 committed 事件，只对受影响的已加载范围使 context 失效，在新的合法配对 cut 下重读同一范围，保留原页的分页 cut。旧 context 响应不能盖掉该范围已经安装的新 context；使用请求身份及既有 event cut 判定，不引入新 generation registry。

因此，同一旧 entry 区域后续发生 adoption 也能补出标记，且不会因原页 event cut 较旧而永远漏掉它。新 context cut 不代表其间全部历史 entries 已加载，也不能冒充 observation 消费进度。

查询和失效重读的单位始终是原来的有界页（或同样受现有 entries/bytes 边界约束的精确 entry 集合）。已读区间在界面连接后，仍保留其原页边界；不能把合并后的整段历史交给一次 context 查询，也不能因为一个新事件重读所有已看页面。

### 7.2 定位入口与完成条件

以下入口统一使用 reader 的精确定位流程，而不依赖目标已在 DOM：

| 入口 | 必须保留的行为 |
| --- | --- |
| 已保存批注、草稿引用的“引用回复” | 读取目标 TEXT blocks，保持 entry/block、原文字符范围和 quote；展开对应过程，挂载后再高亮。 |
| 定时任务来源、已有 memory 来源的 entry 跳转 | 在正确会话加载目标区域、展开、居中；不只设置 `focusSourceEntry` 然后无限等待 DOM 自己出现。 |
| 任务定位 / 中间过程定位 | 先定位其真实 ROOT、creator 或 task activity，再展开该组；不能挂到当前最近 assistant。 |
| 从当前已显示最终回复分叉 | 始终传原 session + 原 entry ID，由现有 fork owner 复制有效上下文；不把浏览器缓存条数作为分叉范围。 |

当前会话搜索只返回 session 与 snippet，没有 entry 命中位置。本方案保留它的会话搜索行为，不新增全文消息搜索产品；若未来返回 entry 命中，必须接入上述 locator。浏览器原生查找只覆盖已挂载内容，不能宣称它搜索了全历史。

定位顺序：确认会话 owner → 读取目标窗口与所需正文 → 发布并展开稳定宿主 → 等 React commit / DOM 注册 → 居中与高亮 → 验证目标已经显示。固定等一个 animation frame 不再作为完成保证。

采用现有取消/会话身份检查丢弃过期定位。快速点击另一引用、切换会话或关闭引用，旧请求不得把新页面拉回旧目标；引用文字与草稿本身不因未挂载而消失。已有 DOM text/Range 注册逻辑复用，未读正文不以 snippet 替代 quote 校验。

分叉网络结果不确定仍沿用现有“未确认，不自动再创建”；分页不能添加自动重试创建行为。分叉会话的 imported history 同样可按需浏览，模型有效上下文复制规则完全不变。

## 8. 任务分组与分页边界

这是启用分页前必须一并完成的正确性工作，不能留作视觉小修。

### 8.1 ROOT 与过程分组

- group identity 使用真实 turn/imported group 身份及真实段边界；不能继续把 `run[0].id` 当稳定 key。向前补入同一 turn 不应重置用户的折叠选择。
- 初始页位于长过程内部时显示“本轮较早过程尚未加载”的组壳。不得将窗口第一条当成用户真正发起点，也不自动读完整 ROOT。
- 工具链连接只在有真实相邻证据的连续区域内部绘制；不跨未加载间隔、ROOT、compaction 或中断边界连接。
- 完成状态仍来自真实 `root_final`、fork eligibility、turn/task owner；局部窗口最后一条 assistant 不自动成为最终回答。

### 8.2 工具结果和 terminal observation

`projectEntries()` 当前对缺少 assistant 的 TOOL_RESULT 产生 fallback 消息；补前页后它会合并到 assistant trace，fallback 消失。此时需维护 `canonical entry / block / tool result → 当前展示宿主` 映射，滚动锚和定位跟随合并，不靠旧 DOM ID 碰运气。

结果关联使用现有 wire 中的 `tool_result.assistant_entry_id / tool_call_id`。若展示确需 attempt 关联，由后端既有 canonical relation 读取；`CanonicalToolResult` 当前没有 `attempt_id` 字段，不能假装已有。creator 不在窗口时显示有身份的边界项并允许定位，禁止凭“当前窗口最近一次工具”配对。

TERMINAL_OBSERVATION 当前会找局部列表最后一个 assistant；分页后必须核对既有 turn/process/owner 关系。没有足够关联依据时保留独立同 owner 记录，不能挂到其他 ROOT 或跨缺口连接。

### 8.3 子任务组

任务是否存在及其状态来自现有 task/inventory owner；不在当前页、不在已加载清单都不代表已结束或不存在。继续复用已有分页任务 inventory、batch 摘要和 activity 查询，不在首屏读尽整个任务图。

精确 creator 挂载不能依赖读尽所有 spawn 工具结果 JSON。已有 batch ID 由 source tool attempt 经唯一 builder 稳定派生（`subagents/contracts.py`）；任务已有 `batch_id` 和 `parent_turn_id`。在 canonical 只读展示上下文中复用这些身份，为当前区域返回有界 creator/batch 摘要和分页入口；不新增持久关系或前端 hash registry。

一个 batch 可有很多任务，展示上下文只带有界摘要，不展开全 batch。creator 未加载时组壳保留真实 parent/batch 身份，待对应区域载入再挂载，不能暂时塞给同 ROOT 的任意 spawn 消息。

batch ID 的派生不是可逆映射：允许由已知 tool attempt 正向求 batch，不能从 task 的 batch ID 反解 creator。task 定位优先使用真实 parent ROOT 的 initial entry 或已有 task activity 的精确查询；没有直接 creator 身份时不遍历整个长 ROOT 来反找它。

imported history 的旧 spawn 文本也不是子会话中存在 task owner 的证据。现有分叉只复制其合同保留的 ROOT 历史并清除执行关联，不能据旧工具 JSON 捏造新会话任务或恢复旧任务执行。

`orderedRootTurnIdsFromEntries()` 当前只看到已加载 ROOT；非连续窗口的数组相邻不等于“上一轮”。“当前轮 / 上一轮 / 更早轮”需由 canonical 只读查询对所需 parent/target 关系计算，不要求返回全会话 ROOT ID 列表。复用 executed turns 和 imported groups 的原归属与排序；没有关系证据时保留未知，不猜 previous。

### 8.4 compaction 和中断标记

分页不创建或移动真实 compaction 边界。旧边界在窗口外时可保留既有最新/继承摘要和定位入口，而不是把它伪装成发生在窗口顶部/底部；不为列出所有旧边界全量预读。更早区域被加载时补出该区域的真实 occurrences。`initialContextBase` 的继承摘要与时间线实际分界分开呈现。

中断标记使用既有 owner、`display_after_entry_sequence` 及其真实宿主；不能因为局部列表缺消息就找另一 ROOT 的最近消息挂载。跨窗口定位时按需要查询相关既有 notices，不能只看最新 compaction control 去推断全部历史边界。

标记只在覆盖真实边界的区域插入，以既有 occurrence/event ID 去重后恰好一次；前插、填补缺口、只读与 imported history 都保持相同归属。继承的初始上下文摘要不冒充本会话新发生的 adoption。分组展开不触发压缩或恢复执行。

## 9. 滚动、渲染与状态保存

### 9.1 两种阅读意图

- `follow latest`：首次进入最新区域，或用户明确点“回到最新”。新增内容和异步高度变化保持底部；首次定位不从顶部播放滚动动画。
- `reading history / locating`：用户上滚或定位旧消息后退出跟随。新消息、工具输出、页补入和图片加载不抢夺阅读位置。

这只是 UI 状态，不是新的 runtime 状态机或 durable 阅读确认。切换会话时清理过期定位任务，保留原有输入草稿。

### 9.2 前插锚定

加载前记录视口内稳定的 canonical entry + block/trace/subanchor 与相对视口位置。合并和 React commit 后，用宿主映射恢复同一语义位置；fallback result 合并到 assistant 时仍能找到它。

不只用 `scrollHeight` 差，因为 Markdown、字体、图片、HTML、过程展开都会延迟改变高度。只在该阅读意图仍有效时补偿；用户主动滚动优先。自动补偿不应触发下一轮无限加载或误切换 follow latest。

### 9.3 首版渲染范围

首版完成按需页、正文与折叠内容挂载，不顺手引入新的完整虚拟列表框架。被折叠的过程不能仅 CSS 隐藏却仍解析和挂载全部复杂正文；引用/显式展开可以按需重新挂载。

首版不得声称“用户一直上翻后 DOM 和缓存也保持恒定”。用户主动加载的元数据和普通可见记录可以增长，按需分页本身不是完整列表虚拟化。后续如需窗口化所有已加载记录，再单独选择维护中的组件并验证文本选择、可变高度、键盘阅读、浏览器查找、iframe 生命周期和锚定；不在本次未经评估地自造通用虚拟列表。

HTML/SVG/图片、代码换行和异步字体继续保留现有冻结资源、安全边界和阅读功能；离屏重挂载的展示行为不得冒充重新执行工具。正文读取重试不能触发任何模型请求。

## 10. 实施切分与删除旧路径

以下是同一次完整改造的实施顺序，不是可长期并存的旧/新产品模式。

1. 明确共享 reader 的页状态、正文状态、精确定位完成契约；补 canonical 有界 anchor-window 与所需展示上下文，复用既有授权/转换/资源边界。
2. 在线与只读 canonical 浏览读取统一到 session-scoped owner；修正 history byte budget 的解析传递。移除浏览器 controller canonical 读分支。
3. 移除 initialize、snapshot、queue transition 中的全量 backfill；让 observe 不等待正文。实现局部正文需求、错误与队列交接。
4. 工作台接入尾部首屏、向上加载、非连续区间、稳定组身份、锚定和异步高度补偿。
5. 将引用、定时来源等 entry 跳转及任务定位接入新 locator；检查分叉和 imported history，完成全部保护性验收后才启用。
6. 删除依赖“浏览器必定装有完整历史”的旧分组/定位/解码分支；保留合法空正文和真正不可用结果的原语义。更新测试及当前实现文档，不写双读、fallback 全量加载或“临时兼容”路径。

不需要新增数据库迁移或重置现有八个会话。不要重启正在执行的 Host 仅为验证 UI；需要后端新只读 API 部署时，待测试任务自然结束后按既有本地应用生命周期安排。

## 11. 验收矩阵

实现时补行为测试；矩阵定义实现必过行为；实际测试和页面证据分别记录，不能以方案审查替代验收。

| 范围 | 必须验证的结果 |
| --- | --- |
| 打开与旧页 | 首屏没有自动读尽 history；只在需求时单页前进；只读与在线一致；尾页只有 SUBAGENT 活动也不空白误报或无界预读。 |
| 实时与正文 | 慢 history/content 请求期间 observe 仍连续；一条正文失败不影响输入/控制；历史 HTTP 取消不会关闭 controller/observer；没有重复消费者。 |
| 游标与竞争 | 双 cut、显式零、wire uint64 精度、重叠页、快照刷新、session切换、旧响应迟到、快速重复加载、非连续窗口补洞均正确；history 不修改 live/event cursors。 |
| 安装顺序 | 输入核对不再 snapshot；snapshot 与 observe 只能顺序安装；current control 读取位置超前于消费位置时不回退状态、不跳过事件；snapshot 跨过但未持有的 entries 保留加载边界。 |
| 页上下文 | initial/readonly/older/anchor/forward 与 observe 新 canonical entry 批次全用同一 context 契约；正文缺席也可分组；新 adoption 落在已读旧页边缘时换 context cut 局部补标记；迟到旧 context、不连续区间和多次 compaction 不抹标、不重读全史。 |
| 正文交接 | live settlement、canonical metadata 和正文异步 ready 的不同到达顺序都保留稳定宿主、明确 loading/error、不重复气泡；临时文本不标 verified；队列已知文本和未提交草稿不因 metadata 到达而消失。 |
| 队列迁移 | pending → consumed/cancelled/rejected/unknown；exact command/queue/entry 关联；不重复提交、不恢复只读 Host、不 backfill 全史；原草稿和输入顺序保持。 |
| 引用 | 早于首屏的批注与草稿引用能定位、展开和高亮；中文/代码原文范围不漂移；快速切换引用与会话不会被旧响应拉回；缺失原文仍保留引用文字。 |
| 来源跳转 | scheduled / memory 等已有 entry 来源可跨窗口加载；会话搜索继续按当前 session 搜索契约，不冒称已有全文命中定位。 |
| 分叉 | 从旧回复分叉，effective context 与浏览器载入页数、折叠、缓存和渲染无关；在 auto compaction 前后、imported history 中选相同 anchor，仍走原 fork owner；结果不确定不重复创建。 |
| 分组 | 页切在 ROOT 中部、TOOL_RESULT 前、terminal observation、spawn creator、跨轮完成通知、batch/task 页边界；不漏组、错挂、重复、假 previous；稳定 key 不重置展开状态。 |
| 标记 | compaction/interruption 在窗口前/内/后和未加载间隔中都不伪造位置；真实边界只出现一次，imported 与只读 owner 正确。 |
| 滚动 | 前插后原句与工具结果位置不跳；fallback 合并、长代码、图片、HTML/SVG异步高度变化、折叠、resize、回到最新和键盘加载均稳定。 |
| 执行不变 | 分页/定位/失败重试不调用模型或提交执行命令；在受控测试中不产生额外 ROOT/task/工具 dispatch 或队列消费；当前已安装 epoch 的 SYSTEM/tools 字节与 messages 前缀不被浏览动作改写。运行中的任务可以正常增加调用和记录，不能误要求其数量冻结；既有 prefix continuity、分叉和自动压缩契约测试仍过。 |

实际页面验收继续使用现有长会话和用户保存的生产配置，不为造压力追加填充文本。至少覆盖：运行中打开、只读长历史、上翻时新输出、旧引用跳转、旧回复分叉、跨页任务组、刷新后恢复；模型任务只作有意义的业务续作。

性能证据比较同类操作的进入到可阅读时间、开始/持续 observe 的延迟、自动 history/content 请求数量、页面滚动跳动和真实 LIVE_RING_OVERFLOW 次数。记录会话长度、并发和页面状态，不能以隐藏 toast、静默跳 cursor 或缩小模型上下文换取好看的结果，也不以一次未出现 toast 宣称已根除。

## 12. 交叉审查与交付条件

审查者：用户指定的 GPT-6 Astra high 子代理。审查只读源码与本文，不修改生产实现。

交付前逐项关闭：所有旧定位入口可达；引用原文和高亮契约完整；分叉仍由原 owner 决定；任务分组不依赖完整历史；模型上下文和执行路径零语义变化；没有新增持久化或总量上限；取消、队列迁移、current/durable 展示字段与历史 cut 明确分离。

### 审查记录

GPT-6 Astra high 子代理独立读取当前生产源码，并对具体草稿进行多轮复核。已合并的主要修订：

| 审查发现 | 本文关闭方式 |
| --- | --- |
| 只删初始化 backfill 仍会从 observe、snapshot、队列交接和只读路径读全史 | §4–6 统一 browser read owner，移除这些路径的全量预读和正文等待。 |
| snapshot 与进行中的 observe 可回写旧 cursor/control | §6.1 单消费者顺序安装；control 读取位置与消费位置分开；队列核对不再 snapshot。 |
| 定位窗口有上下文，但初始页、旧页或实时追加没有 | §7.1 所有页面与有界 committed entry 批次共用 context builder，未就绪不猜分组、不阻塞 observe。 |
| 旧页 cut 查不到后发生的 adoption，或一刷新就重读所有已加载历史 | §7.1 明确独立 context cut、迟到响应校验和原有界页刷新；§8.4 按真实 occurrence 去重。 |
| tool-result fallback 合并、组 key 改变导致定位与滚动丢锚 | §8–9 稳定语义身份及展示宿主映射；不依赖窗口第一条消息或纯高度差。 |
| batch 派生不可反解、wire 无 attempt_id、imported spawn 可能被误当活任务 | §8.2–8.3 使用既有精确关系与正向 builder，定位真实 parent/activity，不捏造任务 owner。 |
| 正文解耦后 live settlement / 队列 metadata 会造成文本交接空窗 | §6.2–6.3 保留稳定宿主和明确 loading；不把临时文本当 verified，不重复提交。 |

审查中曾提出“正常最终 assistant 可能先发布非 final 再变 final”的示例，后经核查 writer transaction 主动撤回，未据此添加刷新框架。

最终复核结论：快照/观察顺序、分页与 context cuts、正文交接、定位取消、任务归属和边界标记已明确，未要求全量历史或新增持久机制；未发现尚未关闭的设计阻塞项。

方案审查通过仅表示设计约束闭合，不代表功能已实施、测试已通过或真实页面验证已完成。本文已将历史定位、引用、分叉、任务分组及模型上下文/执行不变列入实施必过验收，不能以文档审查替代这些验证。


## 13. 实现记录与验收证据（2026-10-18）

### 13.1 最终读取与展示路径

- 在线与缺失目录等既有只读 reader 共用 session-scoped canonical 读取；浏览器不再用 controller attachment 读取历史正文、图片、artifact 或可视化。初始化、snapshot、输入交接均没有全量 backfill。
- 保留实际原始有界 page、配对 cut、独立双向游标及连续窗口。直接 anchor 定位，不从会话尾部逐页扫描；不相连窗口的工具结果保留独立宿主，实际补洞后再合并并保留 canonical 别名。
- observe 与 snapshot 顺序安装；历史/context/正文不推进消费游标。精确 uint64 的十进制字符串贯通浏览器 HTTP 的 observe 入口，不经过 JavaScript Number 重建。
- 旧队列已读取正文和本窗口 local submission 都按精确 command/queue 与 canonical metadata 交接，正式正文 ready 前只作“本地已知内容”展示。退役 pending 正文用 exact receipt 核对，不重发输入、不额外 snapshot。
- 正文与 reasoning 分开按需求读取。已折叠过程不挂载复杂正文；定位最终回答不展开整个过程或读取其思考。已发送批注弹窗、草稿预览统一接入原 locator；等待正文注册，并按引用 Range 起始行居中，高亮使用原文范围。

### 13.2 边界失效读取的细化

独立审查确认 current control 可领先观察批次，latest compaction 不能代表每次 adoption。因此既有 `/history/context` 增加可选 `boundary_event_ids`：它与 `entry_ids` 共用原 256 项物理预算、session 授权和合法配对 cut，返回 `boundary_entries[{event_id,event_sequence,entry_id,entry_sequence}]`。这是对既有 occurrence 的有界只读定位，不新增事件、存储关系或执行命令。

前端按实际消费的每个 boundary occurrence 异步查真实边缘，仅刷新持有该边缘的原 page。fresh context 失败后保留需求与最低事件 cut，重试不退回旧 cut。边缘页稍后才被用户加载时，如其 context 早于已消费展示边界位置，只对这张新页补一次 fresh context，分页 cut 保持不变。多个 adoption、同边缘多个 occurrence、迟到旧响应和全局读取失败重试均有独立回归。

### 13.3 用户现场调整的加载界面

用户在实际八个长会话切换中指出两点，本次纳入最终展示合同：

1. 上滑到顶部补页时合并边界提示，只显示一条“正在加载…”，不同时堆叠“较早记录未加载”和“本轮较早过程未加载”；完成或失败后恢复有意义的加载/重试入口。
2. 首次尾页 context 尚未就绪，继续显示现有恢复会话页面，不逐条渲染“正在读取记录分组…”。`history.initialReady` 仅控制首次界面切换；连接、订阅和 observe 已在后台启动。首屏 ready 后粘住，旧页、正文或 adoption 刷新不把工作台退回恢复页；初始 context 失败保留原页与局部重试，不重连 Host、不清草稿。重试开始立即发布 loading，连续点击合并等待同一 context 请求，不消费 older cursor。已有精确 entry context 的页面刷新时保留原分组与已读正文，仅窗口显示加载/失败。

### 13.4 验收记录

证据目录：`output/history-paging-20261018/`。测试使用仓库 `.venv` 和现有前端工具链，未重置现有数据库或修改用户生产设置。

- 初次打开 1018test1（原 1210 条 canonical entries），实际浏览器只发一笔 tail context，无旧 history/body 全量读取。折叠时记录宿主约 15 个，实施前同会话约 470 个；这不是完整虚拟列表或长期恒定内存承诺。
- 首次冷恢复连接约 5.27 秒，tail context 约 1.15 秒；observe 在 context 请求后约 0.12 秒就启动，早于 context 返回。这是此次现场时序，没有同条件旧版延迟对照，不宣称冷恢复总耗时已消除。
- 正常上翻触发有界旧页，前插后保留原阅读区域；一条带真实旧回复引用的业务复核运行时，上方旧页保持原位置，没有追到底部。
- 已发送引用在刷新后来源尚未挂载，点击产品入口按 anchor window 读取；原中文与 `CLI/Web` 引用完整保留。最终页面已核验：来源回复首行位于可视区约 y=355px，CSS `annotation-source` Range 精确覆盖整句原文，中间过程保持折叠；加载期间仅一条提示，首次进入保留恢复页。截图见 `quote-located.png`、`loading-single.png`、`initial-restoring.png`。
- 1018test2（1464 条）仅尾部读取仍可查看四个真实任务组；05:13 批次两个精确 task 身份、规范终态和任务详情读取正常，不依靠补齐 ROOT。
- 从 1018test1 压缩前 entry `entry:g5tve2ei4rvvlx7vq5yloxd2ji`（source sequence 900）通过实际“从此处分叉”创建测试会话 `session:wiobqenbahjacwblqtwr7gn46e`，现名“分页验收 · 压缩前分叉”。新会话恰好复制源范围的 569 条 ROOT 记录；turn、task、tool attempt、provider usage 均为 0。分叉没有恢复旧任务，浏览器载入条数没有决定复制范围。测试分叉已保留，未永久删除。
- 唯一额外真实模型业务续作是上述两句话的交接证据复核；其新增 ROOT/回复按显式提交单独计数，不能把它混作分页产生的模型调用。最终八会话只读计数确认：仅 1018test1 显式复核增加 1 个 turn、1 次 provider call、2 条 entry；其 tool attempts 未变。其余七会话上述计数及 event head 全部未变，全部无 RUNNING turn。对照见 `before-ui-acceptance.json` 和 `after-ui-acceptance.json`。

最终 GPT-6 Astra high 独立复审无阻塞，独立 reader 回归 10/10 通过；覆盖历史定位、引用、分叉、任务分组以及首屏和刷新状态。后端相关 89 项、provider prefix continuity / 分叉 / 长程压缩相关 109 项通过。最终前端 10 文件 / 436 项、TypeScript、ESLint、构建和 diff check 均通过；日志保存在证据目录。构建仅有既有大 chunk 提示，后端仅有既有 aiohttp deprecation warnings。真实页面最终构建已再次核验首次恢复页与单条上滑加载，未修改生产设置或模型上下文/执行语义。
