# Pulsara 浏览器 dogfood 复盘与后续修复方案

日期：2026-09-08。状态：**PROPOSED，尚未实施**。

依据：完整阅读 `PULSARA_REAL_BROWSER_DOGFOOD_BUG_REPORT_2026-09-08.zh.md`，以当前工作树生产代码为主要事实来源。Git 基线为 `7e2ec332`。原报告保留，不把本次静态发现倒填成此前真实浏览器已复现的问题。

本次只新增此文档，未修改产品实现、数据库、生产配置或 active specification，也未进行新的真实 provider 调用。

路径约定：正文中简写的 `conversation_kernel/...`、`terminal_protocol/...`、`tools/...`、`capability/...`、`terminal_process/...` 均位于 `src/pulsara_agent/`；`frontend/...` 和 `tests/...` 从仓库根目录起算。行号对应本次审阅工作树，用于定位，不是内容身份凭证。

## 实施文档索引

本文件作为问题与批次索引；每个 PR 由独立实施规格确定其完整 hard-cut 边界。批次是优先级分组，不等同于必须合并成一个 PR。拆分不允许同一边界保留新旧双路径。

| PR | 实施文档 | 范围 | 状态 |
| --- | --- | --- | --- |
| PR01 | [原文保真 Hard Cut 实施规格](PULSARA_BROWSER_DOGFOOD_PR01_SOURCE_TEXT_FIDELITY_HARD_CUT_IMPLEMENTATION_SPEC.zh.md) | F07；删除原文全局中文化，保留 typed UI 标签 | READY_FOR_IMPLEMENTATION，尚未实施 |

F01/F02/F03 的关联与展示修复、F06 的 exact-target stop，以及其他问题仍按下文保留优先级；尚未创建其实施规格。本索引不会因 PR01 单项完成而宣称 Batch A 完成。

## 1. 我的总体判断

当前问题不是“核心执行器普遍不可靠”，而是**执行事实与用户能够看见、能够控制的事实之间存在断层**。这个断层不能当作单纯的界面润色：用户看不到 diff、待执行输入或批准过的方案，就很难判断下一次授权是否合适。

三轮测试对以下边界给出了有价值的正面证据：旧 revision 和未见行拒绝、非法操作批次不部分写入、create-only 不覆盖、审批前不实施、子任务依赖失败不启动下游、取消后仍可接新任务、分叉历史与共享文件事实可以区分。当前 filesystem 和 Host 代码也支持这些具体观察。

但是，“最终文件正确”“有最终回复”“测试全绿”都不能替代以下保证：每一步修改可核查、重复输入不会被界面误合并、实时结果准确挂到原调用、停止不会误中下一轮、历史审批可回看。本次修复应优先补齐这些保证，而不是重写已经工作的调度、权限或文件执行机制。

本文所说的代码缺陷不等同于已经证实的安全越权漏洞。没有足够证据宣称存在任意文件写入、审批绕过、跨会话泄密或 provider-prefix 损坏。

## 2. 问题分类与优先顺序

| 编号 | 问题 | 判断及证据等级 | 优先级 |
| --- | --- | --- | --- |
| F01 | 完整工具结果被摘要替代 | BUG-01；浏览器、canonical、当前代码一致 | P2，首批 |
| F02 | live tool-result draft 未准确关联调用 | 代码确认，内存运行诊断复现；是 BUG-01 外另一个底层原因，不是原浏览器证据的完整替代解释 | P2，首批 |
| F03 | 排队正文不投影；按正文消除 optimistic 消息 | BUG-02 主因已确认；重复正文问题为额外代码发现 | P2，首批 |
| F04 | 历史规划没有正文和对应决策视图 | BUG-03；底层正文仍在，缺历史投影和入口 | P2，第二批 |
| F05 | 取消规划 toast 承诺继续 | BUG-04；确定文案缺陷 | P3，可随首批修复 |
| F06 | 停止请求未绑定点击时的 turn | 代码级并发目标缺口；尚无浏览器延迟竞态复现 | P2，控制链首批 |
| F07 | 正则中文化改写模型代码和方案正文 | 新增代码发现，实际函数运行已复现；复制也使用改写后的正文 | P1，首批优先 |
| M01 | 停止推理、停止进程、取消监视、停止自动续轮被混同 | 真实行为已确认；具体产品选择需明确 | 与 F06 一起定稿 |
| M02 | 切会话/重连会终止普通工具确认 | 当前有意 fail-closed；缺预告、原因展示和恢复指引 | 与 F04 并行 |
| M03 | `N\|` 前缀误写、replace_file 与 seen guard 边界 | 前者是模型易用性风险；后者现有例外需说清 | 小范围 descriptor 修复 |
| M04 | “已见”实际是 workspace 内进程级 observation | 代码事实；不等于每个模型都看过，也不是权限漏洞 | 明确承诺，暂不扩大实现 |

F01/F02/F03 应成组处理：只加一个 diff 组件，会保留 live 丢结果；只把 queue 正文加回去，会保留重复消息身份错误；只依赖刷新，又会让所有问题变成难复现的时序差异。

F07 比一般文案问题严重：它已经越过“解释运行状态”的职责，修改了用户应当核对和复制的内容。在修复详情展示前应先划清原文与界面标签的边界。

## 3. F01/F02：工具结果必须可核验，live 必须准确关联

### 3.1 当前真实链路

- `tools/builtins/filesystem.py:614` 的 EditFileTool 读取当前 bytes、校验 revision/observation、内存 stage、二次比较当前 bytes、atomic replace、写后比对，再返回 diff、changed_windows 和新 revision。
- `conversation_kernel/tool_execution.py:1791` 使用 output processor 的 canonical preview 作为 live final_text；随后以同一 prepared output 进入 canonical settlement。预览可能因现有 artifact 规则而有边界，不能承诺所有工具都有无限完整 inline 正文。
- `frontend/lib/runtime-adapter.ts:2454` 等 canonical 路径保存了 `resultText`，但又将展示用 `output` 设为 `formatToolResult(result)`。
- `formatToolResult`（同文件2727）遇到 `path + total_lines` 提前返回路径/行数；遇到 edit_file 的结构没有 diff 分支，最终返回“操作已完成。”。
- `frontend/components/workbench-view.tsx:321` 的 TraceCard 主要展开 command、output、meta；普通文件和直接命名 MCP 工具没有读取完整 resultText 的通用详情入口。现有 MCP 专用详情主要处理 list/inspect/use-new 三类工具，不覆盖所有 `mcp__...` 返回。

因此，摘要不是不存在，而是被错误地当作唯一详情。失败卡片能显示 message，不代表成功路径可观测。

### 3.2 另一个独立缺陷：result draft 是独立流

后端 `tool_execution.py:1193` 为结果建立独立 `draft_identity=result_entry_id`、TOOL_RESULT channel、tool_call_id、attempt_id，并发出 TOOL_RESULT_START；`terminal_protocol/v3_gateway.py:1220` 保留 start 中的调用身份。

前端 `applyLive`（runtime-adapter.ts:1686）按 draft_identity 建 draft，但：

1. 只在 TOOL_CALL_START 创建 trace，没有处理 TOOL_RESULT_START 来创建或关联 result trace。
2. RESULT_DELTA 和 RESULT_END 直接取当前 draft 的 `traces.at(-1)`。
3. 结果 draft 与 assistant draft 不同，通常没有 trace；实际结果和结束状态因而没有落到原卡片。
4. 即便某种输入碰巧共用 draft，“最后一个”也不是多调用、乱序结果的合法关联方式。

本次将真实生产 adapter 在内存编译后，输入与 producer 一致的“canonical 工具请求 + 独立 result START/END”载荷，得到：

```text
原调用 trace.status = running
原调用 trace.resultText = undefined
另有 live:result 消息但没有 traces
同一结果改以 canonical TOOL_RESULT 输入后：
trace.status = completed
trace.resultText = 完整 JSON
trace.output = [已读取 probe.txt · 2 行]
```

这证明 live 关联缺陷与摘要丢详情可以分别成立；不能用其中一个替代另一个。对于原报告特定时刻为什么显示通用成功文本，仍应在回归中同时记录 committed/live/settlement 时序，不凭推测补齐全部因果。

### 3.3 修复设计

**数据归属**：canonical result 继续归现有 tool settlement；frontend 只持有可丢弃的展示状态。用已有 session、scope、turn、tool_call_id、attempt_id、result draft identity 做 exact join，不增加 durable 记录或 fingerprint。

- 接纳 RESULT_START，把 result draft 与确切调用关联；DELTA/END 按其 block/channel identity 更新该调用的 result presentation。
- 请求可能已 canonical 化，也可能仍为 assistant live draft；两者共享同一展示关联规则。
- 请求在分页范围外时，显示身份准确的独立结果卡，不猜“最近一次调用”，之后出现请求时按 exact identity 合并。
- committed result 一旦到达，以 canonical 为完成权威；晚到 live 不能覆盖已完成结果。settlement 清理对应临时状态，不删除另一 scope/call 的内容。
- 统一 root/child、live/canonical 使用的结果展示函数，区分 `summary` 与详情；不要求增加新的后端工具结果格式。

**展示层最小范围**：

| 结果类型 | 收起摘要 | 展开内容 |
| --- | --- | --- |
| read_file | path、实际窗口及总行数、截断标记 | 带行号原文、content_revision、读取范围 |
| edit_file | path、操作数、结算状态 | unified diff、新 revision、changed_windows、窗口截断标记 |
| write_file | 已创建 path、字节数 | 创建结果、新 revision；不称覆盖 |
| MCP/未知 JSON | 工具名和明确状态 | 只读结构化 JSON/文本；数组也不能退化为通用成功 |
| terminal | 工具调用状态与进程状态分开 | output、process_id、running/exit_code、现有截断/coverage 字段 |
| error/cancel | 已拒绝、已取消、执行失败等真实状态 | error code、message、hint；不得统一翻译成“未发生副作用” |

保留当前输出/artifact 资源边界；inline 只是预览时应明确说明，并复用已有 artifact 授权读取机制。不要为了展开卡片重新执行工具、自动调用 MCP、解析输出中的指令或放宽权限。不要把 HTML 直接注入页面；代码与 diff 按文本展示。不得广泛删掉“技术字段”来换取好看。

### 3.4 必须补的验收

- 独立 assistant/result draft、多 tool call、结果乱序、跨 scope 同名工具、请求暂不在当前窗口、late live after commit。
- read/edit/write 和直接 MCP 调用的实时详情与 reload 后详情语义一致；复制详情保留换行。
- 空内容不是丢结果；失败与 cancelled 不显示成功图标/文字；后台 running 不是“命令已结束”。
- artifact preview/incomplete/unavailable 明确区分，不伪装完整源内容。

### 3.5 F07：不能用产品术语翻译器改写原文

`runtime-adapter.ts:3189` 定义 PRODUCT_TEXT_REPLACEMENTS，3236的 productVisibleMessage 对整段 assistant body、reasoning、子任务活动正文直接运行这些正则。`readInteraction` 对 plan draft body 也调用同一函数（1431附近）。它不区分普通叙述、代码块、命令、JSON、文件名或精确引文。

使用生产函数 `productVisibleText` 的内存诊断得到：

````text
输入：
```python
from api import read_file
ROOT = "read-only"
exit_code = 0
```

展示结果：
```python
from api import 读取文件
主任务 = "只读"
退出码为 0
```
````

这不是模型生成了错误代码，而是前端改坏了正确输入。`workbench-view.tsx:774` 的复制回复取 `message.body`，因此复制也带走变换后的内容。plan draft 的展示变换同样可能改变命令/选项的含义，但提交决策仍指向原始 draft identity，导致“看见的方案”与“授权的原文”不一致。数据库和 provider 历史未因此被改写，不应报告成 canonical 数据损坏。

修复应是一个内容边界 hard cut：

- 模型正文、思考、计划原文、代码/diff、工具实际输出、精确引用保持原文；只允许已有真正凭据保护规则处理实际密钥值，不做术语全局替换。
- 中文化只用于产品自己生成的标题、状态枚举、按钮、已知 public code 的说明。保留原 code/message 的查看入口。
- 不用“跳过 fenced code 的正则”修补，因为命令可以出现在普通段落、行内代码、JSON字符串、链接和文件路径中。边界应由字段的语义 owner 决定，不猜文本形状。
- Copy 使用与原始正文一致的数据；Markdown 负责渲染，不负责修改词义。审批视图与批准的 exact draft 内容必须一致。
- 删除对上述原文字段调用 productVisibleText 的旧路径。已有测试中“把任意传入字符串里的 read_file 翻译掉”的断言，应随这个明确的产品 hard cut 改为原文保真断言，并另测 typed UI label 翻译；不能只删测试取得绿灯。

验收覆盖 fenced/inline code、shell参数、JSON key/value、URL/path、英文 ROOT 与 Kernel、read-only 字面值、中文混合文本，以及 live/canonical/reload/复制/方案批准前后的同文性。既有展示函数的单元测试全绿，不能证明这些内容未被改写。

## 4. F03：排队输入已经有真源，不应再依赖正文猜身份

### 4.1 根因已闭合

`terminal_protocol/canonical_v3.py:655` 读取 PENDING 队列并计数；837起已经投影 queue_item_id、queue_sequence、delivery_mode、target_turn_id、content、permission。当前协议不是只有一个数字。

`runtime-adapter.ts:549` 声明了 prompt_queue，但1923只投影 queuedCount。`pulsara-app.tsx:753` 将成功提交正文存到本地 optimisticMessages；362在重新连接后清空。刷新后只剩计数完全符合此实现。

还有一个相关代码缺陷：`publishProjection`（pulsara-app.tsx:198）用 `role 相同 && body 相同` 删除 optimistic 消息。历史中已有“继续”，此时再排队一次“继续”，任意后续 projection 就可能把新消息的可见占位删除；两次不同输入不能因为正文相同而共享身份。这不删除数据库记录，但会误导用户是否已经提交。

本次内存 adapter 诊断输入一条含正文的 canonical queue，输出是：

```text
queuedCount = 1
messages 中包含 QUEUED_BODY = false
```

### 4.2 修复设计

- RuntimeProjection 增加 typed queued prompts 展示，直接来自 control.prompt_queue，按 queue_sequence 排序；使用 queue_item_id 作为 UI identity。
- 建议独立“待处理输入”区域，允许展开正文和查看本次权限/投递类型。它们不是已经执行的 USER_MESSAGE，不能混进 provider transcript 或让用户误以为已执行。
- optimistic 状态只负责尚未取得明确受理结果的短暂本地发送态；受理后应由同一提交的 canonical queue/执行记录接替。不能用全文或文本 hash 对账。
- 使用现有 commandId、queue_item_id、command outcome/consumption 关系。注意 `query_command`（host.py:3917）在 pending 与 consumed 时 targetId 可能分别指向队列项和 turn；不能把所有 targetId 无条件当队列 ID。
- ACK 与 observation 的先后顺序必须覆盖：observation 可能先于 POST 响应；接纳后马上消费的队列项可能根本没出现在下一次 snapshot。应按明确身份和新鲜 snapshot 收敛，而不是长期保留 optimistic 副本。
- 当前 control 有协商资源边界，超限会返回资源结果而不是悄悄截掉队列。保留现有边界，不加总队列/总会话上限；不能出现“显示前几条却宣称全部”。未来若需要分页，仍读现有队列表，不另建消息缓存表。
- 同会话第二窗口、重连、reload 看到相同已接受队列；切换其他会话不会显示上个会话的 pending 内容。

验收至少包括：连续两条完全相同正文、与旧历史同文、普通 prompt 与 steer 同文、ACK/observation 乱序、队列立即消费、刷新/接管、取消/拒绝终态。队列状态和 transcript 状态只投影各自语义，不能双重显示同一项已经执行的消息。

## 5. F04/F05：规划历史与即时反馈分开修

### 5.1 已有正文可以读取，但缺历史状态入口

`_repository/plans.py:1854` 的 read_plan_draft_text_chunk 通过 interaction→assistant tool arguments 读取原文；1904的查询不限定 OPEN，所以当前会话中已终结的 draft 正文并未因批准而被删掉。

但 `canonical_v3.py:708` 只向 control 提供 active workflow/open interaction；前端 projectInteraction（runtime-adapter.ts:2951）据此只构造当前交互。历史 TraceCard 没有相应决策投影，所以唯一剩下的 DRAFT_SUBMITTED_FOR_REVIEW 原始结果仍显示“等待确认”。

重要边界：gateway 的 `_has_plan_content_capability`（v3_gateway.py:1064）把精确 plan 正文读取限制在**当前 controller**。不能因增加“历史查看”就顺手对 observer 放开这个 owner gate。

### 5.2 建议的历史只读投影

- 每份 draft 卡片关联现有 interaction ID 与确切 assistant_entry_id/tool_call_id，而不是只找 workflow 的最后一版。
- 从现有 plan_interactions/workflows 读取该份 draft 的状态、ordinal、决策与 revision；需要新的 read endpoint/DTO 时，它仅是现有关系的查询投影，不是新增审批记录。
- 卡片提供“查看此版方案”，复用现有分块读取；对大方案按需读取，不把所有历史 draft 无界灌入初始 control。
- 已批准、已要求修改、已取消、已中止都有准确标签；历史只读不提供旧 approve 按钮。打开的是第一版，就不能显示第二版批准的状态。
- 原 TOOL_RESULT 仍表示当时提交成功，不能改写 canonical 旧结果来使现在的状态看起来正确；在旁边叠加当前关系投影。
- observer 暂按现有权限显示可用的状态摘要/无精确正文权限提示；如产品希望旁观者读全文，应单独明确 read 权限边界，仍与提交决策权限分离。

**Fork 必须单独处理**：`_repository/fork.py:225` 把 assistant blocks 的 tool_arguments 导入子会话，但并不据此创建子会话的审批 workflow。不能拿导入 TOOL_RESULT 中的源 interaction ID 到子会话中查询，更不能偷偷跨会话查询源计划。

对于导入历史，可从子会话自己持有的 frozen tool arguments 展示原 draft，标签写明“导入历史；不是本会话的待审批方案”；没有足够证据时不要伪造本会话 APPROVED 状态。现有 tool_arguments_preview 有截断边界（canonical_v3.py:566），不能把截断 JSON 当完整计划。完整正文读取若缺少合适入口，增加按本会话 entry/block 定位的只读分块适配，复用内容提取代码；不复制新 plan rows、不给旧草稿重新执行权。

### 5.3 取消 toast 是确定缺陷，不需要改调度

`pulsara-app.tsx:896` 为 approve/revise/cancel 共用“Pulsara 将继续处理。”。当前 `host.py:2940` 明确只有 APPROVE/REVISE 创建 continuation turn，CANCEL 不创建。

建议按结果分别反馈：

- approve：“方案已批准，将按批准内容继续。”
- revise：“修改意见已提交，将生成修订方案。”
- cancel：“规划已取消，未启动这份方案的实施。你可以发送新任务。”

取消可能留下供未来输入消费的既有 handoff，不等于立即开启自动续轮；也不意味着全会话、后台命令或已经完成的副作用被回滚。不能把这里的 toast 修成过强的“所有工作已停止”。

验收：question→draft1→revise→draft2→approve 全链各版可回看；cancel/abort 后可读；服务重启；双窗口 controller/observer；fork 后只读历史；跨页中文 UTF-8；过期决策拒绝；toast 与是否实际创建 continuation 一致。

## 6. F06/M01：停止必须回答“停哪一个”和“停到什么程度”

### 6.1 一个代码级目标竞态

现在 `runtime-adapter.ts:1319` 的 stopActiveTurn 不传 target_turn_id；gateway（v3_gateway.py:630）甚至拒绝携带 target 的 STOP_REQUEST；Host（host.py:4039）取请求处理时的 `_active_task` 并取消。

由此存在确定的可达时序：

```text
页面显示 A 运行 → 用户点击停止 A → 请求在途
→ A 自然结束，队列 B 被接纳 → Host 收到 STOP → 取消 B
```

这是从代码推出的并发目标缺口，本次未注入网络延迟做真实浏览器复现。若将产品定义成“处理时无条件停止当前任何轮次”，实现可被这样解释；但它不符合用户对所见停止按钮的通常目标预期，因此建议修成 exact-target，而不是只在文案中回避。

### 6.2 推荐 hard cut：绑定点击时的确切 turn

- 点击时冻结 `projection.activeTurnId`；缺 ID 时不要发送无目标停止请求。
- 使用协议已有 target_turn_id 字段，gateway 改为要求该字段；删除无目标的前端停止路径，不增加兼容 fallback。
- Host 在同一锁下比较期望 turn ID 与当前活动 owner，再安装 cancellation cause、取得对应 task。A 已终结或换成 B 时返回明确的“目标已结束/已变化”，不能取消 B。
- 复用当前 cancellation/effect settlement；不要为停止额外建立 durable job、receipt 表、generation 或 hash。
- 其他调用方若已有内部的“停止当前任务”需要保留内部语义，须与 UI exact-target 入口清楚分开；不能让外部入口悄悄退回内部无目标动作。
- 重复点击或 uncertain retry 仍针对 A。A 已结束时给出可理解的幂等式反馈，不自动重发成“停止新的当前轮次”。

验收：A结束/B接纳后迟到 STOP、重复 STOP、stop与tool settlement竞争、stop与plan successor接纳竞争、stop与terminal observation竞争；同时证明 B 未被误停，已开始的副作用仍走原结算。

### 6.3 当前停止的实际产品范围

Host 的 stop_current_turn 没有杀进程或取消所有 monitor。terminal monitor 是独立 process-local owner，`terminal_process/monitor.py:423` 的 cancel 只关闭指定观察注册；Host 的 `_terminal_monitor_delivery_loop`（3569）可以在 idle 时接受 NewTurnInstallation，再由3792启动新的 root turn。人工排队输入优先，但“之前点过停止”不是一个永久抑制条件。

这解释了 E01：原 turn 真正中断，后台进程继续，之后 TERMINAL_OBSERVATION 开启另一轮。不是原轮次复活，也不能把继续运行的进程说成已杀掉。

**推荐近期保留分离语义，不把普通停止扩大成停止一切**：

| 动作 | 明确目标 | 不承诺 |
| --- | --- | --- |
| 停止本轮回复 | 点击时那一 root turn | 不撤销已发生效果、不清空队列、不杀所有进程/子任务 |
| 停止某个后台命令 | 精确 process_id，通过已有进程管理 owner | 不因卡片显示“完成”就认定进程已退出 |
| 取消某项观察 | 精确 monitor_id | 不杀进程，也不回滚已经开始/提交的 observation 安装 |
| 取消某个子任务 | 精确 task_id，通过已有 stop_agent 语义 | 不自动复制任务，也不让失败依赖继续执行 |

首批先完成 exact-target 和停止后的解释，命令卡片明确显示后台仍运行/完成后可能通知。若新增 UI 进程/monitor 操作入口，必须走现有授权 owner，不能让前端任意发 kill 指令。

如果产品最终想要“停止后直到下一次人工输入都不再自动回复”，这是另一项暂停自动接纳的产品契约，不是修一行 toast：要同时规定 terminal/subagent completion、已冻结安装、已提交 observation、队列优先级、恢复条件和进程重启后的弱保证。届时优先复用 process-local owner，不建立持久暂停任务系统。本方案不把这项扩大后的语义默认为已获批准。

## 7. M02：确认随 controller detach 结束，应显式告知而非改成 durable

### 7.1 当前行为是有意的安全收敛

`pulsara-app.tsx:325` 切会话/重连时先 close 旧连接；`runtime-adapter.ts:1523` 删除 attachment。`conversation_kernel/interaction.py:552` 在 controller_detached 时调用 `_abort_all`，普通工具确认最终 DENY，原文说明 controller detached。

这与 E02 的文件未创建一致。它与可跨进程恢复的 canonical plan draft 不是同一种对象，不应为了“切回来还能点允许”把普通确认升级为 durable approval。

此外，代码专门处理 resolving 与已获 durable winner 的竞争。不能粗暴清除 future/重置 pending，把已经决策的 winner 当成没有发生。

### 7.2 推荐近期方案

- 在有普通工具确认/能力配置表单时，**主动切会话或接管**前明确提示“离开将结束尚未确认的操作”，允许留在当前会话或继续离开。
- 能力页/记忆页导航如果没有 detach，不应误弹相同警告。判据是实际 owner 生命周期，不是所有页面点击。
- reload、网络断开无法可靠依赖离开前弹框：重连后必须明确展示“因控制连接结束而未获授权”，而不是让“已拒绝”看起来像用户主动点击拒绝。
- 保持 canonical result_state=PERMISSION_DENIED，展示可补充真实 reason；优先利用现有结果/决策引用。若需要 typed reason，只扩展现有结果投影，不新增事件类别。
- 提示用户重新发送任务；不自动重放原写调用，不把旧“允许”迁移给新 attachment。
- plan question/draft 保留现有 durable 恢复；不要为了统一 UX 把两套语义强行做成同一生命周期。

验收：主动切会话、同会话接管、刷新、断连、批准与detach同时发生、已允许但写入未结算、恢复后重新提交。必须同时验证“未授权时无写入”和“已提交的合法 winner 不被前端误判为回滚”。

## 8. 文件工具：保持新语义，修说明，不恢复猜测

### 8.1 测试支持保留的核心实现

`filesystem.py:633–688` 的精确 revision 检查、seen intervals、stage、二次字节核对、atomic replace 和写后验证，与三轮测试一致。`_atomic_create_bytes`（1450）使用同目录临时文件、fsync、os.link 发布，新路径竞争触发 FileExistsError，不是“先判断不存在再普通覆盖”。

但这些检查不等于跨进程 CAS；replace 之前到真正发布之间仍有不合作外部写入的窗口。写后验证抛错也不代表没有写入。修 UI 时必须保留这些不确定/副作用边界，不能把所有失败统一写成“文件未修改”。

### 8.2 行号前缀错误应该怎么修

descriptor（builtin_catalog.py:913、1048）说明 read 返回 line_number|text；`_edit_operation_schema`（148）只说明 lines 不含 CR/LF/NUL，没有足够直接地说“不要复制显示前缀”。

建议在 read/edit 和 lines 字段说明中补一句明确规则与一个最小例子：

```text
read 显示：2|预算=360
replace_lines 的 lines：['预算=400']
不要传 ['2|预算=400']；2| 是定位显示，不是文件文本。
若原文件本来包含 2|，应按真实文本原样保留。
```

不加自动剥前缀、不 fuzzy 修复、不恢复 old_text/new_text；执行器忠实写入模型提交的字节是正确边界。

### 8.3 两项需要诚实表达的例外

- `_stage_edit`（1042）对 replace_file 走独立分支，不检查全文件 seen intervals；外层仍要求有效 revision 和当前 observation。这是现有显式完整替换语义，不能因为负例都通过就宣称所有改动都必须逐行看过。当前 active line-edit spec 的9.2也明确这一例外；此处查规格仅用于确认它不是意外遗漏。
- `_state_for_workspace`（194）按 resolve 后的 workspace root 取得模块级状态，observations 再按 canonical path 存；没有 session、subagent 或 provider-context owner。相同 workspace 的另一次读取可能扩大此进程的 seen ranges。它防“未曾观察过的范围”，不证明“当前模型请求确实看见过每一行”，更不是权限 token。

建议首批修清 descriptor 和对外承诺：用准确的“当前 runtime 的读取观察 + exact revision”，明确 replace_file 例外。若产品要更强的“每个模型上下文都亲自见过”，应另定 observation scope，复用已有 scope identity、保持 process-local，并处理子任务、fork、compaction、返回 changed windows 后的资格。不要偷偷加 session/epoch hash 或 durable observation。这是后续语义增强，不是已证实的权限绕过修复。

## 9. 落地批次与删除清单

### Batch A：展示准确性与停止目标

F01/F02/F03/F05/F06/F07 属于首批修复；按上方实施文档索引拆分 PR，每个已选边界均须带回归完整落地。PR01 先完成 F07 原文边界；后续 F01/F02/F03 仍成组设计，F06 单独闭合控制目标契约。主线所有生产修改应保持单一路径。

删除/替换：

- 以“最后一个 trace”为工具结果归属的猜测。
- 以通用成功摘要充当完整详情的单一路径。
- 以正文相等消除 optimistic 消息的路径。
- 已受理队列正文只存 React 本地状态的路径。
- UI 无 target_turn_id 的停止请求与对应网关旧规则。
- approve/revise/cancel 共用续轮承诺文案。
- 对模型正文、思考、计划和原始输出做全局术语替换的路径。

保留：已有 tool result、queue、command、content/artifact owner；不加 feature flag、兼容双投影或新 durable 状态。

### Batch B：历史审批与生命周期反馈

完成 F04、M02 和 M01 的分离语义展示。复用既有 plan 正文读取、canonical relation 和 exact owner gate；补历史查询只读投影，不把 history 塞进 active interaction。

删除：历史卡片只剩“等待确认”的唯一展示、因 controller detach 而失败却让用户误以为主动拒绝的唯一反馈。

### Batch C：descriptor 与组合回归

补 `N|` 规则、replace_file 例外、observer/后台状态说明，验证修改不改变 provider-prefix 连续性。

descriptor 改动属于 provider-visible tool contract：使用已有 cold epoch/明确采用的 compaction successor 安装新根；不得在已有 epoch 内通过“更新 UI”重写 SYSTEM/tools。测试用新冷会话验证新说明，用已有会话验证旧 prefix 不被重建。

如果决定引入“暂停所有自动续轮”或 per-context seen observation，先单独把产品语义和 owner 写进 active specification，再 hard-cut；不混入上面的展示修复。

## 10. 验证方案与通过门槛

### 10.1 本次实际运行的诊断

在 frontend 目录执行：

```sh
npm test -- lib/runtime-adapter.test.ts app/pulsara-app.test.tsx
```

结果：**2个文件、83个测试全部通过**，耗时约6.12秒。Node 有 `--localstorage-file` 未指定有效路径的警告，本次未更改运行环境消除它。

另用 `node --input-type=module` 的 stdin 脚本，通过 frontend 已安装 TypeScript 在内存编译实际 adapter 与其本地依赖，替换 fetch 输入最小生产协议形状，并直接调用实际文本变换函数；不写测试文件、不访问 provider、不改数据库。结果已列于3.2、3.5与4.1。它是确定性投影诊断，不冒充真实浏览器第二次复现，也没有修改/弱化83项原测试。

现有 adapter test 已覆盖 canonical reordered results；出现 RESULT_START 的一项测试主要验证终结 child 的 stale draft 被过滤，并没有验证 active result draft 的内容正确连接。因此现有绿灯与本次缺陷并不矛盾。

### 10.2 实施时应补的测试层次

| 层次 | 必须证据 | 主要代码/测试落点 |
| --- | --- | --- |
| Adapter | exact live join、queue 正文与身份、root/child、乱序和late事件 | runtime-adapter.test.ts |
| React | 原文与复制保真、展开可读详情、重复正文、历史各版、toast、点击时停止目标、detach原因 | pulsara-app.test.tsx；必要时新增专用 TraceCard 测试 |
| Protocol/Host | exact-target stop、owner切换、plan历史读取权限、terminal安装竞争 | terminal_protocol 对应测试；test_round2_terminal_host.py；test_round4_plan_host.py |
| Settlement | 已开始副作用的取消与结果落库不被UI逻辑改变 | 现有 tool execution / settlement 测试 |
| 文件回归 | stale/unseen/batch/no-clobber、合法 N\| 文本不被自动改写 | test_content_revision_line_edit.py |
| 连续性 | UI刷新、队列、权限变化不重建prefix；descriptor升级只走批准边界 | 仓库现有 continuity oracle / dogfood |
| 真实浏览器 | live→完成→reload 详情一致；排队复文；晚到stop；审批历史；双窗口 | 保存生产配置，通过 OpenRouter，截图与canonical对照 |

前端 focused 命令沿用上述两文件；Python focused 使用根目录 `.venv/bin/pytest`，例如 `tests/test_content_revision_line_edit.py` 和被修改 owner 对应测试。需数据库的 fixture 先核对其目标和清理机制，不能为验证文案随意 reset 当前真实 dogfood 数据。

落地后再跑前端完整 `npm test`、lint/build:local，以及受影响的后端回归。协议/打包资源变化时补 isolated launcher/wheel；若 active specification 要求完整回归/real-provider/continuity，以其门槛为准，不以“只是 UI”豁免真实协议修改。

真实 dogfood 应从确定场景出发，而不是靠 bobapi 恰好慢来撑出竞态：保留真实模型交互，同时在测试 harness 中可控地延迟 ACK/observation/stop，分开记录故障注入证据与自然浏览器证据。使用保存的 Pulsara home 配置，不导出密钥，不写入配置，不上传本地敏感内容。

### 10.3 最终接受标准

1. 展开工具结果能核对路径、读取文本或 diff；实时和恢复一致，状态无错配。
2. 所有已受理待执行输入可查；完全相同的两次输入仍是两项，无重复投递。
3. 任何迟到的停止 A 都不能误停 B；不承诺取消已发生效果。
4. 历史 draft1/draft2 与各自决策对应，全文读取权限不扩大，fork 不伪造审批权威。
5. 取消规划、controller detach、后台继续运行分别给出准确反馈。
6. 无新增 durable event/job/relation/subject/guard 或 fingerprint；无兼容旧路径；provider prefix 规则不变。
7. 新失败场景先有能失败的断言，再修成绿；无弱化断言、skip/xfail 或改写原报告证据。
8. 原文内容不被术语中文化改写；复制代码可用，审批时显示的内容与被批准的 draft 一致。

## 11. 尚未由本次测试证明的事情

- bobapi 两次主动中断不足以确定是 provider 故障、Pulsara 挂死或 transport bug。
- 一次压缩后的事实复述正确，不证明长期无信息损失；短上下文压缩未落快照也不能自动判为故障。当前 compact UI 已区分 NOT_NEEDED，后续应按实际 outcome 验证，不先重写压缩系统。
- 只读测试中模型主动不发 write 调用，不等于已验证后端拒绝恶意写调用。
- atomic publication、写后验证和 seen guard 的通过不证明跨进程事务、每模型读取证明或绝对文件安全。
- warn/error 日志为空不证明无请求失败；截图可读不证明所有尺寸/辅助功能都通过。

下一次修复最值得追求的结果是：用户看到的内容足以核对真实执行，用户点击的控制动作作用于确切对象。完成这两点，再讨论更强的自动续轮暂停或 observation 隔离，范围更清楚，也更容易证明没有破坏现有 machinery。
