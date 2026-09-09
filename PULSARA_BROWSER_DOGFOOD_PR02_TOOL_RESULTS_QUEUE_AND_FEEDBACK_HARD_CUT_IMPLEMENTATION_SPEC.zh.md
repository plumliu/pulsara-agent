# Pulsara 浏览器修复 PR02：工具结果、输入队列与反馈 Hard Cut 实施规格

状态：**ACTIVATED — 2026-09-10 第二轮审查闭环（工作树，未提交）**。

日期：2026-09-08；第一轮审查闭环：2026-09-09；第二轮审查闭环：2026-09-10。代码调查基线：`2212804e`。

索引：[浏览器 dogfood 复盘与修复方案](PULSARA_BROWSER_DOGFOOD_REVIEW_AND_FIX_PLAN_2026-09-08.zh.md)。本 PR 一次完成 **F01、F02、F03、F05、M03**。

本规格已按 Phase A–D 完成实现与验收；未创建远端 PR，也未把历史截图或 PR01 的测试结果挪作本 PR 证据。下面的调查行号仅用于定位，不是内容身份凭证；最终事实以当前工作树、测试和第15节激活记录为准。

建议 PR 标题：`fix: expose exact tool results and queued inputs with accurate feedback`。

## 1. 目标与批次边界

用户应能核对：工具到底返回了什么、结果属于哪次调用、自己的哪条输入仍在排队、取消规划后会不会立即实施，以及文件编辑参数应该怎样填写。

| 编号 | 本 PR 必须完成 | 不以什么替代 |
| --- | --- | --- |
| F01 | 工具摘要与可读详情分离，已有完整保留输出可按需读取 | 仅显示“操作已完成”或要求模型再总结一次 |
| F02 | 独立 result live 流与 exact 调用关联；root/child、恢复与分页一致 | 挂到最后一个 trace、同名工具或最近一条消息 |
| F03 | 服务端队列正文可见；发送、受理、消费按提交身份衔接 | 只显示数量；按正文或正文 hash 去重 |
| F05 | 取消规划不再承诺自动继续实施 | 修改调度来使错误 toast 成真 |
| M03 | 明确显示行号前缀与写入文本的区别；说明完整替换例外 | 自动剥前缀、fuzzy recovery 或收紧既有 replace_file 语义 |

F01 与 F02 共用结果身份和展示模型，F03 补齐另一条输入可观测性链路，F05 与 M03 是有明确边界的反馈/说明修正；五项在一个 PR 内各自完成 hard cut。

### 1.1 依赖与保留

- PR01 的 F07 实现已随 `2212804e` 提交：正文不再经过全局术语翻译。保留其复制、Markdown 安全、用户换行及记忆页回归。
- PR01 尚未完成全部 activation 收尾。其 T02/T05 断言缺口在本 PR 涉及的 live/plan 回归中补强，但不能据此替 PR01 宣称旧 wheel/browser 证据覆盖了最终产物。
- F07 原文边界是前提：新增详情、队列和 toast 不得重新接回正文翻译器。
- 既有 filesystem revision、permission、Hook、effect settlement、cancellation、artifact 和 provider-prefix owner 保留。

### 1.2 明确不纳入

- F04 历史方案与各版决策入口；不增加旧 draft 再审批或 fork 计划审批权。
- F06 exact-target stop、M01 后台进程/自动续轮暂停、M02 detach 确认寿命。
- M04 observation 按会话、子任务或模型上下文隔离；不改变现有 process-local/workspace/path owner。
- 编辑/撤回/重排队列、重新执行工具、重新投递失败输入、全站错误信息治理。
- 新的 diff 编辑器、JSON 查询语言、通用 artifact 管理平台、全历史虚拟化重构。
- 记忆页、provider 调度和文件写入算法的进一步重构。

## 2. 当前代码真源与已经确认的缺口

路径从仓库根目录起算；`src/pulsara_agent/` 下的路径不省略前缀。

| 真源 | 当前事实 | 必要改动 |
| --- | --- | --- |
| `frontend/lib/runtime-adapter.ts:2439/2606/2727` | root/child canonical 结果已有 resultText，但展示 output 经 formatToolResult 摘要化；部分空字符串被 truthy 判断跳过 | 保留确切源内容与完成状态；摘要不再充当唯一详情 |
| `frontend/components/workbench-view.tsx:321` | TraceCard 的展开条件和内容主要看 command/output/MCP 专用摘要 | 所有工具共享可访问的原文详情入口 |
| `src/pulsara_agent/conversation_kernel/tool_execution.py:1193/1791` | 结果使用独立 draft、tool_call_id、attempt_id；END 使用 canonical preview，可能不同于此前 terminal streaming chunks | 按真实 producer 形状测试；不改执行/结算流程 |
| `src/pulsara_agent/terminal_protocol/v3_gateway.py:1105/1220/1360` | live envelope 和 settlement 已包含 scope/channel/generation/proposed entry 等身份 | 前端接收并使用现成身份，不丢弃后再猜 |
| `frontend/lib/runtime-adapter.ts:1686` | 未处理 RESULT_START 的关联；RESULT_DELTA/END 取 traces.at(-1)；CALL_END 也有末项 fallback | 删除末项归属路径；建立连接内 typed 关联 |
| `src/pulsara_agent/terminal_protocol/canonical_v3.py:631/837` | control 已投影全部被允许读取的 PENDING 队列、顺序、正文引用和权限 | 投影 typed 队列，不再只取 total_count |
| `frontend/app/pulsara-app.tsx:198/716` | ACK 后新增 optimistic user message，再用 role/body 相等去除 | 用提交身份和队列/消费事实替代 |
| `src/pulsara_agent/conversation_kernel/host.py:3917` | query_command 的 target_id 会从队列 ID 变成消费后的 turn ID；REJECTED 也可能表示 turn 中断 | 显式区分输入投递状态和执行状态，不猜 target_id 类型 |
| `src/pulsara_agent/storage/migrations/sql/0000_conversation_kernel_baseline.sql:891` | prompt_queue_items 已有 command_id、client_submission_id、status、consumed_entry_id | 只扩展读取投影，不新增对账表/列 |
| `src/pulsara_agent/web_app/browser_bridge.py:321` 与 `canonical_v3.py:400` | read-content 只支持 entry/block，不支持 queue 引用 | 补 exact queue content 读取目标，复用分块 machinery |
| `src/pulsara_agent/conversation_kernel/tool_artifacts.py:488` | PostgresToolArtifactReadPort 已负责 session/workspace fence、完整性检查和文本切片；尚无对应浏览器入口 | 加最小只读适配，不从浏览器调用 artifact_read 模型工具 |
| `frontend/app/pulsara-app.tsx:870` 与 `host.py:2940` | approve/revise/cancel 共用继续处理文案；只有前两者创建 continuation | 按被接受的决策分别反馈 |
| `src/pulsara_agent/capability/builtin_catalog.py:148/913/1048` | lines 说明未直接警告 N\| 显示前缀；read/edit 的泛化 seen 文案未说清 replace_file 例外 | 修改描述字符串与例子，不修改工具输入形状和执行器 |

两项边界尤其不能从索引的简写推断为“已可直接复用”：

1. 有 canonical content reader，不代表它已经支持队列内容；当前入口强制要求 entry_id。
2. 有 artifact_read builtin，不代表浏览器已有 artifact API；复用的是底层只读 owner，不是一次新的工具执行。

## 3. 权威、持久化与协议范围

### 3.1 唯一权威

| 事实 | 权威 | 前端职责 |
| --- | --- | --- |
| 工具请求和已结算结果 | assistant_message_blocks、tool_results、transcript_entries | exact join 与只读展示 |
| 尚未结算的输出 | 已有 live bus 的 typed channel/draft | 连接内可丢弃的实时投影 |
| 输出完整性与 artifact | tool_results 的既有 artifact edge、immutable blob、现有 read port | 展示 coverage；按需读取保留内容 |
| 待处理输入与消费 | prompt_queue_items、session_commands、consumed_entry_id | 展示 pending；按身份接替本地发送态 |
| 规划决策与续轮 | 现有 plan resolution outcome、workflow/interaction | 准确反馈，不自行创建续轮 |
| 文件编辑资格 | filesystem 当前 observation、exact revision、permission | 提供正确说明，不根据浏览器看过正文签发资格 |

本 PR 的新增状态只允许是 typed DTO 和 process-local UI 状态。**durable event、live event kind、subject、guard、relation、job、数据库表/列的数量均不增加。**

不引入消息 hash、submission fingerprint、trace fingerprint、receipt/checkpoint、恢复 job、通用 registry、LRU 历史或新总量上限。现有 SHA-256 content_revision、immutable content digest 和协议兼容 identity 只沿用既有 owner；不另算文件/文档/验收证据 SHA。

### 3.2 允许的最小后端改动

- canonical reader 对既有行增加输入来源身份、工具结果 artifact 元数据的只读投影。
- command/query outcome 增加从既有队列关系读取的 typed input delivery 信息；不改变 command 的接纳、消费、取消或调度事务。
- queue content 与 tool artifact 的受限只读协议/bridge 适配。
- builtin descriptor 的描述字符串；更新其现有测试与 active line-edit specification 的说明。

Protocol v3 schema、生成 binding、gateway、browser bridge 和 fixture 必须同次更新，只保留当前单一合同。没有旧字段猜测 fallback、旧新协商或双写。不同的 entry/queue/artifact 内容目标是不同产品读取边界，不是旧接口兼容别名。

发现需要新增 durable 数据、改变执行或权限边界时，停止该扩张，先修订本规格；不能把它藏进“展示优化”。

## 4. F01：摘要、原文和保留输出分开

### 4.1 单一结果展示模型

复用 ToolTrace 并作最小 typed 调整，root/child、live/canonical 共用结果投影与组件。必须能区分：

- 尚无结果；
- 已返回、正文为空字符串；
- live streaming 文本；
- 已收到 END 的完整 canonical preview；
- canonical 已结算；
- 内容尚未加载、加载失败或只存在不完整保留输出。

`undefined` 与 `''` 不得用 truthy 判断混同。结果状态从协议 result_state 取得；不从正文中的 success/error 单词推断执行结果，也不能把工具进程状态当成调用结算状态。

`resultText` 或其替代字段始终保存合法解码后的源内容。summary 是派生值；允许为可读性解析 JSON，但不得把 pretty-print、翻译、trim 或重新序列化结果写回源字段。不要保留两套竞争的 root/child 结果格式化路径。

TraceCard 有结果、结果引用或结果加载状态时即可展开；不能因没有 command/output 摘要就隐藏详情。现有 MCP list/inspect/use 专用摘要可以保留，但直接命名的 `mcp__...`、未知工具也必须可读原文。

### 4.2 展开内容

| 类型 | 展示要求 |
| --- | --- |
| read_file | path、content_revision、offset/limit、total_lines、truncated 与实际 content；总行数不能冒充本次已读行数 |
| edit_file | path、operations_applied、base_revision、新 content_revision、diff、changed_windows、changed_windows_truncated；有字段即保留 |
| write_file | “已创建”而非覆盖；path、bytes_written、新 revision；已有路径拒绝仍为失败 |
| terminal/terminal_process | 命令、输出、process_id、running/exit_code、coverage/截断；SUCCESS 且 running=true 不表示进程退出 |
| MCP/未知 JSON | 对象、数组、字符串、数字、布尔、null 都有安全原文视图，不统一退化为通用成功 |
| plain text/空输出 | 完整可用文本及其空白；空结果明确显示“工具未返回正文” |
| 错误/取消 | 原 result_state、error/code/message/hint 等实际可用字段；取消不显示成功样式，不承诺没有副作用 |

仅对精确的 builtin tool_name 选择文件结果专用解释；外部工具返回类似字段不自动获得 filesystem builtin 语义。未知形状回到安全原文视图，这是内容类型降级，不是旧摘要路径兼容。

代码、diff、JSON 和普通工具文本使用安全的只读文本组件。复用现有 React/Markdown 能力，不启用 raw HTML、不执行输出中的命令、不因展开触发 MCP 调用或新的工具执行。

`edit_file` 的 unified diff **必须直接显示**在专用只读视图中，这是本 PR 的最终产品语义；不设置单独的“复制 diff”或“复制差异”按钮。完整 diff 仍保留在工具原始结果中，需要复制时通过“复制结果原文”连同其 canonical JSON 一并复制。其他现有复制入口只注明真实对象：“复制结果原文”“复制当前保留输出页”。复制参数严格等于对应源字符串；不能复制 DOM textContent、摘要、格式化 JSON 或拼接错误的重复行。分页尚未完整加载时，不提供虚假的“复制全部输出”。

键盘可展开、可收起、aria-expanded 准确；展开或流式增长不得遮住 composer、强制把正在看历史的用户拉回底部。重复输出行用其真实序列/位置作 React key，不用行文本当唯一身份。

### 4.3 两种“完整”必须分开

当前 ToolOutputArtifactProcessor 要求 canonical preview 为 InlineContent，preview 本身也可能是 HEAD_TAIL。完整读取 canonical preview **不等于**取得工具的完整保留输出。

沿用现有元数据闭集：

- artifact disposition：NOT_REQUIRED、AVAILABLE、INCOMPLETE、UNAVAILABLE；
- display kind：COMPLETE、HEAD_TAIL；
- source coverage 及已有 reason。

这些字段从 tool_results 关系投影，不能靠从提示文本中正则提取 artifact_id 后假装完成授权。live 阶段没有 metadata 时只说“当前返回内容”；canonical 到达后补充实际 metadata，不伪造 COMPLETE。

AVAILABLE：可按需读取完整保留输出。INCOMPLETE：可读取已经保留的片段，但始终标注源不完整。UNAVAILABLE：保留 preview 并解释既有 unavailable reason，不生成无效读取按钮。NOT_REQUIRED：按现有 display/coverage 表达，不臆造 artifact。

### 4.4 浏览器 artifact 只读适配

增加连接内 `read-tool-artifact` 操作及对应 typed wire request/response，范围只限某个 canonical tool result 的保留文本：

1. 请求携带 `result_entry_id`、`offset_chars`、`max_chars`。session/workspace 从已验证 attachment/host 派生，不接收客户端任意 scope、blob_id 或文件路径。
2. 先 exact join 当前 session 的 tool_results.result_entry_id，取得该结果自己的 artifact edge。缺行、无 artifact 或跨会话目标返回明确读取错误。
3. 调用现有 PostgresToolArtifactReadPort.read_text；复用既有 verified provider、ARTIFACT lane、完整性检查、字符切片和单次上限。不得复制 SQL/blob 校验器，不经 builtin executor，不制造 tool result、provider 调用或 durable 记录。
4. 返回页正文、现有范围/next_offset/has_more 和 coverage/disposition；不向浏览器暴露内部 blob_id。完整性检查继续归 read port。
5. 已授权的同会话 controller/observer 按普通 canonical 内容读取能力使用该只读入口；不借此放开 plan content 的 controller gate。Host/Origin、attachment generation、数据库就绪和会话授权必须逐层保留。
6. UI 按需逐页读取，续页使用服务端返回的字符 offset，不拿 JS UTF-16 length 计算 Python 字符偏移。每次读取绑定 exact `result_entry_id`、session/connection owner 和 process-local request revision；折叠工具卡、关闭 artifact、身份变化或卸载即使该代次失效，迟到的 success/error/finally 均不得落入旧卡或新卡。
7. fork 只读子会话自身持有的 imported tool_result/保留 artifact edge；没有子会话 edge 就明确不可读，不能跳回源会话授权。

同样保留现有 read-content 对 canonical entry/block 的支持；若对应引用未 inline，不能把“未加载”显示为空结果。按需分块读取要验证既有 digest/size/offset、EOF 和解码完整性；浏览器必须对实际组装 bytes 计算并核对既有 canonical SHA-256，UTF-8 必须 fatal decode，不能只相信服务端回显元数据或用替换字符静默接受损坏。这里沿用既有 immutable-content digest 边界，不增加新 fingerprint 或摘要种类。

## 5. F02：独立结果流的 exact join

### 5.1 身份与信息不足

调用关联以当前 session、scope_kind、scope_subagent_task_id、turn、assistant_entry_id 和 tool_call_id 为依据。attempt_id、result draft/generation/proposed_entry_id 用来连接同一次物理尝试和其结果。

当前 RESULT_START 不携带 assistant_entry_id，不能在规格或测试中假装它已有该字段。合法关联来源为：

- 已有 control.tool_attempts 的 attempt_id → assistant_entry_id/tool_call_id；
- 已有 canonical tool_result 的 result entry → assistant_entry_id/tool_call_id；
- assistant live draft 的 proposed_entry_id → 后续 canonical request entry。

前端须保留现有 envelope 中被当前 TS 接口忽略的身份字段。不能只凭 turn/tool_call_id 或工具名猜测，因为它们未被当前关系约束为全会话唯一。

绑定尚不充分时，显示身份准确的独立结果卡和“调用信息尚未加载”，不挂错卡、不丢正文。获得 exact binding 与请求后合并为一张卡；这是真实信息不足时的展示，不是永久并行的第二份结果。无需为避免短暂独立卡增加 live event kind 或新的身份查询框架。

### 5.2 事件应用规则

1. RESULT_START 为当前结果 channel 建立 typed presentation；按 scope、draft/generation、block、attempt 记录身份，不创建空的普通 assistant 消息占位。
2. RESULT_DELTA 按 exact result channel/block 追加源文本。不得取 traces.at(-1)，也不得按同名工具查找。
3. RESULT_END 的 final_text 是最终 preview，**替换**之前的 streaming 文本，包括 final_text 为 `''` 的情况；不能将 streamed terminal chunks 再追加到 END JSON 后面。
4. CALL_END 只更新 exact tool_call_id/block 对应的请求；删除最后一个 trace 的 fallback。缺 START 的完整 END 若有充分身份可建立相应记录，否则保持未关联/等待同步，不猜。
5. 同一 owner epoch 内重复 revision 不重复追加；处理遵循协议既有排序、snapshot 和 gap 合同。不能新造一个跨 owner 的 revision 比较轴。
6. canonical 结果一旦存在，成为该结果正文与终态的权威；迟到 live 不能把 completed/cancelled/failed 变回 running，也不能覆盖 canonical 空结果。
7. root 与 child 使用同一关联规则，child 不因“同一工具名”被接到主会话或另一个子任务。

没有 START 且无法重建 attribution 的 DELTA/END，不得伪造成完整调用。保留可识别的未关联状态并走既有 gap/snapshot 重同步；正常生产 live snapshot 必须证明能够重建必要的 START/attribution。

### 5.3 settlement 与恢复

- 使用已有 settlement 的 exact identity、scope、channel、committed_entry_id、reason，不把任意 settlement 当成“清除所有 live”。
- COMMITTED 清理其 live 源之前应有相应 canonical 替代，或明确处于等待读取 committed entry 的状态；canonical entry 暂不在窗口不能使已有结果消失。
- ABORTED 结束该展示流，不推断物理效果已经撤销，也不改工具实际 settlement。
- 请求或结果跨 history page 时，先独立展示，加载后按 exact relation 合并；最终消息/trace 数量有断言，不能只检查第一条正文。
- owner epoch 变化、重连和 reload 使用现有 snapshot/gap 路径重建；process-local 记录可以丢失，canonical 正文不能靠旧前端状态恢复。
- 同 session 的 connection 替换、跨 session 导航都要校验回调 owner；每个异步操作保存发起时的 exact connection/session/generation，并在每个 `await` 后、任何 reconnect、toast 或 UI mutation 前重新验证，不能把旧连接的 ACK、页结果或错误发布到新连接。

## 6. F03：输入身份、队列正文与消费衔接

### 6.1 最小只读身份投影

当前数据库已经保存关系，但 wire 没把所有身份交给浏览器。本 PR 明确允许以下 typed 扩展，不新增存储：

| 投影位置 | 新增语义 | 真源 |
| --- | --- | --- |
| PromptQueueControl | command_id | 该 queue row 的 command_id；不再同时扩一份等价 client_submission_id |
| CanonicalEntry 的输入来源字段 | queue_item_id、command_id、delivery_mode | 同 session、consumed_entry_id exact join 到队列；只对真实消费的 USER_MESSAGE/USER_STEER 提供 |
| CommandOutcome 的 typed prompt_delivery | queue_item_id、queue_status、consumed_entry_id、delivery_mode | 当前 query_command 已能读取的 queue relation；command_id 沿用外层 |

无队列关系的普通命令不带 prompt_delivery。接纳前校验/Hook 拒绝也不能伪造队列行。新前端对已受理输入要求新 typed 投影，不保留根据 targetId 前缀、publicMessage 或 body 猜身份的 fallback。

query_command 的原 status/public_code 可以继续描述执行状态，但输入显示必须看 queue_status：**CONSUMED + TURN_INTERRUPTED 仍表示输入已经被接受和消费，不是“发送失败”。** 同一轮内多条 steer 不能只靠 turn_id 对账。

Imported history 不带源会话的提交对账身份；导入正文相同不代表本会话接受过同一提交。不得回查祖先队列。

### 6.2 本地发送态与服务端状态

沿用 `command:web:<UUID>`，但在网络请求发出前就获得本次 commandId，并把同一个 ID 传过 React → RuntimeConnection → command。不得 ACK 后才生成可见身份，或每次查询/重试生成新 ID。

本地提交记录按 session + 本次 connection owner + commandId 归属，仅存在于进程内。它不是 Message、不是 canonical transcript、不是服务端队列，也不是 durable recovery job。

| 时点 | UI 语义 | 接替/消除依据 |
| --- | --- | --- |
| POST 在途，尚无 canonical 证据 | 正在发送 | 本次 commandId |
| ACK 表示 PENDING，队列已投影 | 待处理输入 | queue.command_id 与本次提交 exact join，UI key 为 queue_item_id |
| ACK 已受理，但投影尚未追上 | 已受理，正在同步 | 保留同一短暂提交项，主动读取新 snapshot/query；不假装已经执行 |
| observation 先到、ACK 后到 | 直接采用队列或消费事实 | 同 commandId 已有 canonical 证据时，迟到 ACK 不复活本地副本 |
| 已消费，queue 从未被本页看见 | 展示对应 USER_MESSAGE/USER_STEER | consumed_entry_id/输入来源身份，不等待一条不再存在的 PENDING row |
| 队列 CANCELLED/REJECTED，或明确接纳拒绝 | 展示真实终态/原因 | typed delivery 或明确拒绝结果；不残留“待处理” |
| 传输失败、是否受理未知 | 受理状态待核对 | 对原 commandId 使用现有 query-command，并刷新 canonical |

不把网络错误等同于服务端拒绝；不自动重发、不自动执行工具、不因 query found=false 就断言从未接纳。未决状态可以保留供用户重新核对/复制正文，也可关闭本地提示；关闭提示不宣称取消服务端工作。

成功查询后按真实状态清理本地记录，不创建一张保存所有历史 commandId 的“已确认注册表”。在页面观察/重连的既有节奏和用户重试动作中对未决项收敛，不逐帧重查全部历史，也不引入最大重试次数或等待 TTL 后冒充失败。

导航后丢弃旧连接 UI 回调的应用资格。旧 owner 的迟到 resolve/reject 不得读取或重连当前 `connectionRef`，也不得在新会话显示 ACK、interaction 或错误 toast。服务端仍可能已接纳请求；回到源会话时从服务端重建，不能因为 UI 没等到 ACK 而取消或再发。

### 6.3 待处理区域

- RuntimeProjection 增加 typed queuedPrompts，直接投影 control.prompt_queue，按 queue_sequence 排序，queue_item_id 为 React identity。
- 位于独立的“待处理输入”区域，不混进已执行 transcript。展示正文、投递类型、适用权限、steer 的目标 turn 等实际可用信息；不提供无后端语义的删除/拖动按钮。
- 两条完全相同的输入仍是两条；历史里已有同文、prompt 与 steer 同文都不影响身份。
- 多行正文 pre-wrap，长 URL/JSON 安全折行；展开收起不改原文或丢失空白。
- permission 缺省不等于 read-only。STEER 沿用既有目标轮权限，不把 composer 当前选择伪装成其新授权。
- total_count 与已加载列表的关系必须明确。当前 bounded control 若超资源边界，沿用明确资源结果；不能只取前 N 条却称全部，也不改后台接纳上限来迁就 UI。
- 同会话第二窗口和 reload 能看到同一 canonical 队列，observer 可以查看但不因队列 UI 获得发送/审批权限。

### 6.4 队列正文引用

不能将 CANONICAL_BLOB 或暂未解码的 content 当成空正文。扩展既有 read-content 合同：请求必须且只能选择 `entry_id` 或 `queue_item_id`；block_id 仅可随 entry_id 使用。二者为真实不同读取目标，不保留 legacy 别名。

queue 分支从当前 attachment 对应 session 的 PENDING queue row exact 取 content edge，再复用现有 inline/blob 分块读取与校验。客户端不提交 blob_id、digest→对象索引、workspace 或任意路径。

队列在读取期间消费，返回 typed “已不在待处理队列”结果并刷新投影，随后从 consumed entry 读取；不能按相同正文找替代队列项。若初次 hydration 窗口内转成 CANCELLED/REJECTED、没有 consumed entry，则保留原 queue item/command 身份，在当前可用 owner 上查询原 command，将 typed 终态和原因投影为 process-local item；不得静默消失、重发或增加 durable registry。读取缺页、offset 错误、EOF 不一致、实际 bytes digest 不符、非法 UTF-8 都不得伪装成完整正文。现有 content digest 只用于已有不可变内容边界，不为对账新增 hash。

## 7. F05：反馈绑定被接受的决策

成功提示只在原 interaction/workflow revision 的决策确实被接受后显示。失败、过期、owner 丢失等情况保留现有错误路径，不先显示成功再修正。

| 已接受决策 | 标题 | 说明 |
| --- | --- | --- |
| approve | 方案已批准 | 已创建按批准方案继续处理的任务。 |
| revise | 修改意见已提交 | 已创建继续修订方案的任务。 |
| cancel | 规划已取消 | 未因本次取消启动这份方案的实施。你可以发送新任务。 |

approve/revise 的续轮表达以现有 resolve-plan-interaction 返回的 continuation_turn_id 为依据；保留该字段到 typed 前端结果，不以 `targetId = continuation 或 workflow` 的混合字段证明续轮。字段缺失或结果矛盾时先同步/报结果异常，不断言任务必然正在运行。

cancel 不新增 continuation；不声称“所有任务已停止”“没有任何副作用”或“已回滚”。取消前发生的只读探查、其他后台进程、后续新输入及既有 handoff 语义不变。

只替换 plan draft 的共同续轮文案；不借此重做普通工具确认或 capability form 的取消生命周期，不增加 F04 历史展示。

## 8. M03：行号前缀和完整替换说明

### 8.1 修改位置与示例

主要修改 builtin_catalog 的 read_file/edit_file 描述、`_edit_operation_schema()` 的 lines/items 说明，以及 replace_file.content 描述。write_file.content 如引用带行号的读取结果，同样说明它接收原始目标文本。

描述必须明确表达，语言沿用当前英文 descriptor 风格，不引入另一套工具说明 owner：

~~~text
read_file displays: 2|预算=360
replace_lines: {"kind":"replace_lines","start_line":2,"end_line":2,"lines":["预算=400"]}
The leading 2| is a display locator, not replacement text.
Do not copy the display prefix unless those characters are intended file content.
~~~

另加不能自动清洗的反例：文件第一行本来是 `2|预算=360` 时，read 显示 `1|2|预算=360`；把真实内容改为 `2|预算=400`，lines 就必须是 `["2|预算=400"]`。

### 8.2 seen 例外不变

- 普通 replace_lines/delete_lines 的整个范围、insert_before/after 的 anchor 必须在当前 revision 的已有 seen observation 内。
- replace_file 必须是唯一 operation，仍要求 exact base_revision、存在的当前 observation 和现有 permission；**不要求全文件 seen**。
- replace_file 的 content 是完整目标文本，不是带定位前缀的 patch；空文件/清空文件及 mixed line endings 的现有完整替换路径保留。
- observation 指当前 filesystem runtime 的读取观察，不宣称是当前模型、当前会话或当前子任务独立看过的证明。浏览器展开 read/edit/artifact 详情不建立或扩张 seen observation。

依据为 active line-edit spec 的 replace_file 定义与 **9.1**；9.2 规定 observation 缺失返回 READ_OBSERVATION_REQUIRED。同步修正误引和笼统“所有 edit 必须逐行已见”的文案，不改变执行规则。

### 8.3 必须保持零改动的语义

不新增前缀剥除、猜测修复、prefix-looking 文本拒绝、old_text/new_text、fuzzy matching 或 feature flag。执行器必须忠实写入 lines/content 中真实提交的字符；不能因为文本长得像 `N|` 就删除它。

不改变 operation 字段/枚举、write_file create-only、stale/seen 拒绝、atomic no-clobber、atomic replace、写后校验、Hook 参数/结果及 effect settlement。M03 生产修改只限描述字符串；如发现另一个执行器 bug，单列，不混入本规格。

### 8.4 provider-prefix 连续性

descriptor 描述属于 provider tools，不是纯 UI 文案。新说明只进入新的 cold epoch 或明确采用的 compaction successor；不能通过 reconnect、重新读取 catalog、刷新页面或本地设置变化改写已安装 SYSTEM/tools/messages 前缀。

既有 surface/schema identity 由原 owner 正常更新，不新增“文案版本 hash”。应同时测试新冷会话看到新说明、旧 epoch 仍保持原 SYSTEM/tools byte-identical 和 messages append-only。

## 9. Hard cut 删除清单

同次删除/替换：

1. RESULT_DELTA/END 和 CALL_END 的“最后一个 trace”归属路径。
2. 结果 draft 被投影为空普通 assistant 消息的路径；精确独立结果卡除外。
3. “没有非空字符串就没有结果”的 truthy 判断，以及孤立 cancelled canonical result 被映射为 failed 的分叉。
4. 以通用摘要充当唯一详情的路径；保留明确命名的派生摘要，不保留两套详情实现。
5. role/body 相等消除 optimisticMessages，以及 ACK 后向 transcript 插入已受理队列正文的路径。
6. 用 targetId、正文、文本 hash 猜输入身份；只在 React 保存已受理队列正文的路径。
7. approve/revise/cancel 共用续轮承诺的路径。
8. read/edit/lines 描述中不区分显示前缀、以及不区分普通行编辑/replace_file 的笼统承诺。

不得把旧代码搬家、包装后原样保留或用开关隐藏。F07 的三项旧符号继续在生产源码中零保留。

## 10. 实施阶段与文件范围

### Phase A：合同与红灯测试

- 重新阅读本规格、AGENTS、当前 producer/gateway/reader/bridge/UI/descriptor。
- 冻结第4至8节的 typed 字段和读取错误；先写真实 adapter/组件/协议 owner 的失败测试。
- 固定 ACK/observation、独立 result draft、空 END、页读取中消费等故障时序，不依赖 provider 恰好变慢。
- 确认 schema/table/event/job 等 oracle 不需扩张；SQL 只读投影不增加 migration。

### Phase B：身份和只读边界

- 从既有关系补齐 queue/consumed/command typed identity；接好 queue content 和 artifact 只读访问。
- 让 adapter 正确处理 result channels、canonical 优先级、settlement 和完整性状态。
- scope、attachment、provider-prefix 和资源边界先有跨层测试，再接 UI。

### Phase C：视图与说明 hard cut

- TraceCard 使用共同结果展示，接入按需读取/复制。
- 独立待处理区域取 canonical queue，本地发送态按身份退出；处理迟到回调。
- 分开 plan draft 反馈；完成 M03 descriptor 与例子。
- 删除第9节旧路径，同步受影响 tests/docs/active spec，只保留新合同。

### Phase D：回归、发布与真实 dogfood

- 执行第12节；使用实际最终 bundle 和安装版，不拿旧截图替代。
- 回写本规格和索引：逐项记录结果/失败/未决项；全部满足第14节才 ACTIVATED。
- 不自动 stage/commit，不创建远端 PR，除非用户另行要求。

主要文件范围：

| 文件/目录 | 允许变化 |
| --- | --- |
| frontend/lib/pulsara-types.ts、runtime-adapter.ts | typed result/queue/提交态与 exact 投影 |
| frontend/app/pulsara-app.tsx | 提交身份、owner 防迟到、plan 反馈 |
| frontend/components/workbench-view.tsx 及必要小组件/样式 | 详情、复制、队列、可访问性 |
| src/pulsara_agent/terminal_protocol/{canonical_v3.py,v3_gateway.py,schema/,generated_v3/} | 只读投影/读取适配与协议单一路径 |
| src/pulsara_agent/web_app/{browser_bridge.py,http_server.py} | 连接内只读操作与参数校验 |
| src/pulsara_agent/conversation_kernel/host.py、必要 repository 读取方法 | 从已有关系提供 prompt delivery/artifact 读取，不改写事务 |
| src/pulsara_agent/capability/builtin_catalog.py | M03 描述字符串 |
| 相关 tests、现有协议 fixture/generator 校验、static/ | 回归与正常生成产物 |
| 本规格、索引、active line-edit spec/工具说明 | 新事实与验收记录 |

tool_execution.py、filesystem.py、Hook 和 artifact processor/read port 主要是阅读与回归对象，不要求重写。只有确切的只读适配缺口才调整接口；不复制这些 owner。

## 11. 验收矩阵

测试必须经过真实 adapter/组件或生产 owner；fake UI 消息不能替代真实 wire 输入测试。数值相等与字符串相等之外，必须检查卡片/消息数量和归属身份。

| 编号 | 场景 | 必须断言 |
| --- | --- | --- |
| R01 | read/edit/write 成功 | 正文、diff、revision、窗口、创建语义准确；非仅关键词 |
| R02 | plain/JSON 对象/数组/null/数字/空串 | 安全可读；空串与未加载分开；无通用成功替代详情 |
| R03 | 直接 MCP 与同名非 builtin | 通用详情可读，不冒充文件工具语义 |
| R04 | terminal live chunks → END JSON | END 替换 chunks，不双拼；running 与调用完成区分 |
| R05 | ERROR/CANCELLED/CANCELLED_BEFORE_DISPATCH | root/child、关联/孤立卡终态一致；无回滚承诺 |
| R06 | 独立 request/result draft、多调用倒序完成 | 每条结果归 exact 调用；没有空 assistant 占位 |
| R07 | 两 scope/两 turn 重用 tool_call_id | 不跨 scope/turn/request 误关联 |
| R08 | attempt mapping 暂缺、跨页请求 | 先独立可读、后 exact 合并；仅一份结果 |
| R09 | END 为空、重复 revision、late live after canonical | 空结果不被旧 delta 填回，不重复、不回退状态 |
| R10 | COMMITTED/ABORTED、owner epoch/gap/reload | 清理 exact channel，canonical 保留；不从旧 owner 恢复临时状态 |
| R11 | COMPLETE/HEAD_TAIL 与四种 artifact disposition | 正确标注 preview/保留完整性，不可用不伪装完整 |
| R12 | artifact 分页含中文/emoji、续页损坏/越界 | 使用服务端字符 offset；错误明确；复制页原文相等 |
| R13 | entry/blob 正文读取、空正文、非法 chunk | 校验现有 digest/size/offset/EOF/UTF-8；不显示伪完整 |
| R14 | 跨会话/伪造 entry、blob、scope、旧 attachment | 读取拒绝；同会话 observer 仅有既定只读权；无越权 plan 正文 |
| Q01 | 两条同文、与旧历史同文、prompt/steer 同文 | 每个独立提交可见，正文不作为身份 |
| Q02 | ACK 先到 / observation 先到 | 同一提交只有一个当前表示；迟到 ACK 不复活副本 |
| Q03 | 排队后立即消费、队列从未被浏览器看见 | 通过 consumed entry 收敛，不永久等待 queue row |
| Q04 | CONSUMED 后 turn 中断；queue 拒绝/取消 | 接纳/执行状态区分；取消或拒绝不留 pending |
| Q05 | POST 结果未知、query 未找到/随后找到 | 不自动重发、不按文本找、不误报明确失败 |
| Q06 | reload/同会话第二窗口/切会话后迟到 ACK | canonical 队列一致；源会话数据不串入目标会话 |
| Q07 | queue CANONICAL_BLOB、读取中消费 | 正文可读取；typed 终止后更新到 consumed entry |
| Q08 | 顺序、权限、资源边界 | queue_sequence 排序；无错误权限承诺、静默截队列或新总量 cap |
| P01 | approve/revise/cancel 已接受 | 文案分别对应 continuation/无 continuation；无额外 command |
| P02 | stale/rejected/owner 切换/结果矛盾 | 不弹成功；请求仍绑定原 interaction/revision |
| P03 | 多块计划、错误 interaction_id/返回 offset | 原文相等；既有拒绝断言补齐，不改 plan reader 权限 |
| D01 | 新 descriptor 与 lines/replace_file 示例 | 实际生产 descriptor 包含定位前缀规则与完整替换例外 |
| D02 | 真实内容含 N\|、调用方明确写入 N\| | 字节忠实保留；无自动剥除、拒绝或重写 |
| D03 | partial read 后 replace_file | 允许完整替换；无 observation/stale/mixed operation 仍按现有规则拒绝 |
| D04 | 新冷 epoch 与已安装 epoch | 新说明可见；旧 SYSTEM/tools 原字节不变、messages 仅追加 |
| X01 | F07/复制/Markdown 安全/用户换行/记忆页 | 保持全部既有断言，未恢复全文翻译 |
| X02 | Hook、permission、started-effect cancellation | 显示变化不改授权、Hook payload、settlement 与物理效果边界 |

本表 D01–D04 专门验证 M03；索引中的 M01/M02/M04 产品改造不在本 PR 范围内。

## 12. 验证命令与证据要求

### 12.1 本地自动化

本规格编写时不运行实施验收。以下是实现者必须执行并记录实际结果的命令；新增测试文件必须补入 focused 清单。

在 frontend：

~~~sh
npm test -- lib/runtime-adapter.test.ts app/pulsara-app.test.tsx components/markdown-body.test.tsx components/memory-view.test.tsx
npm test
npm run lint
npm run build:local
~~~

在仓库根目录，使用 uv 管理的 `.venv`：

~~~sh
.venv/bin/python tools/generate_terminal_protocol_contract.py --check
.venv/bin/pytest tests/test_stage2_protocol_v3.py tests/test_stage2_live_contract.py tests/test_local_web_browser_bridge.py tests/test_local_web_http_surface.py -q
.venv/bin/pytest tests/test_stage2_canonical_reader.py tests/test_round1_tool_output_artifact.py tests/test_round4_plan_host.py tests/test_round4_plan_postgres.py -q
.venv/bin/pytest tests/test_content_revision_line_edit.py tests/test_round9_unified_capability_semantics.py tests/test_round9_2_hook_subsystem.py tests/test_round9_2_hook_integration_postgres.py -q
.venv/bin/pytest tests/test_frontend_reasoning_projection.py tests/test_round3_1_provider_input_prefix_continuity.py tests/test_fingerprint_subtraction_architecture.py -q
.venv/bin/pytest -q
git diff --check
~~~

Protocol generator 当前依赖系统 protoc，且正常生成不自动替你更新所有 fixture/schema identity；按现有脚本真实行为同步，不能手改生成 binding 伪造 --check 通过。这些是现有协议兼容边界，不为本 PR 新设另一套 hash 校验。

需 PostgreSQL 的 fixture 先阅读 `tests/support/postgres.py` 并核对目标。真实浏览器 dogfood 默认复用保存的生产配置与真实本地数据库，不为 UI 验证 reset 用户会话；自动化清理只能作用于已经验证的 fixture/可丢弃本地目标。

失败不得以 skip/xfail、弱化断言或改变原始证据消除。小型失配 fixture 可按当前生产合同修正；较大无关基线失败逐项列明，不能声称全回归通过或 ACTIVATED。

### 12.2 构建与 isolated launcher/wheel

1. build:local 生成实际 static 资源；记录最终 index 引用，不能沿用 PR01 bundle 名。
2. 用 mktemp 创建独立 wheel 输出目录，执行 `uv build --wheel --out-dir <实际临时目录>`；尖括号不是可直接执行的参数。
3. 参考现有 `tools/run_round9_3_plugin_installer_discovery_dogfood.py` 的 isolated launcher 安装步骤，不运行整个插件测试。安装到临时环境，不覆盖全局 launcher。
4. 从非源码 cwd 启动安装版，核对包导入位置、静态入口，并在真实浏览器复测详情/队列/取消反馈。只有 --version 成功不算发布验证。
5. 识别并避免源码版/安装版同时争用同一 host owner；不为打包验证清空真实数据库或改保存配置。
6. 代码、descriptor 或 bundle 在 dogfood 后又变化，必须重建并复验受影响路径；旧截图不能覆盖后来的产物。

### 12.3 真实 provider + 浏览器

通过 LocalSettingsStore 和 require_pulsara_home() 只读加载保存的生产配置，优先使用已有可用 OpenRouter 模型，不依赖 bobapi 故障制造测试。缺配置则报告缺项，不导出密钥、不写生产配置、不引入环境凭据 fallback。

必须在真实浏览器交互，并结合截图、DOM/clipboard 精确断言、协议或 canonical 事实：

- 在专用探针目录创建、读取、修改含中文、URL、代码及字面 `N|` 的文件；核对工具实际参数、diff、revision、changed windows 和真实文件内容。
- 覆盖 root 与只读 child 的结果；比较 live 完成、canonical 接替和 reload 后同一调用详情，不只核对最终文件。
- 至少一个直接 MCP/未知结构化结果和一个超过 artifact 保留阈值的输出；验证保留输出分页、coverage、复制和读取失败。已有 MCP 配置不足时先记录缺项，不私自安装第三方服务。
- 用已知可用模型和有界探针让运行保持可观察，连续提交相同正文，并在第二窗口/reload 查看队列；至少核对一条同文 steer 与其 exact 消费记录。
- 对 ACK/observation 乱序、迟到回调和极快消费使用可控的测试 harness；明确标成故障注入证据，不冒充自然 provider 行为。
- 规划到 draft 后取消：没有因 cancel 新建 continuation 或实施文件；另验 approve/revise 的确切续轮和失败提示。
- M03 在新 cold epoch 的实际 provider tools 中可见；模型能够在明确探针上提交不带显示前缀的 lines。若模型仍误用，保留真实输入/参数/失败，不用执行器清洗或修改证据冒充通过。
- 宽/窄布局、长 diff/长队列正文、展开/折叠/复制、composer 可达和焦点均有视觉检查。

测试只在明确探针目录创建/修改文件，不删除用户文件。详情读取不能引起工具再次执行、provider 请求、seen observation 扩张或权限改变。所有模型任务结束后确认没有本次遗留待审批或无意运行的探针。

证据保存实际命令、模型标识、输入、关键返回、断言、session/entry/command/queue/call 定位与截图。保留实际错误，不只写“无控制台错误”。仅排除实际配置凭据；不计算证据文件 hash。

## 13. 失败与剩余风险

- 未有 canonical edge 时，artifact 完整正文可能暂不可读；显示当前 preview/待结算状态，不绕过 owner。
- 不完整 source 无法由前端补齐；artifact 可读不等于原始进程输出完整。
- live 丢失、重连和队列被快速消费是正常时序；通过现有事实重建，不变成执行 replay。
- 输入接纳后的取消/中断与传输未知可能同时存在；UI 必须说明其层次，不承诺 exactly-once 网络交付。
- M03 提升可理解性，不保证所有模型永不误填；真实 `N|` 字符仍应被忠实写入。
- filesystem 的跨进程 CAS、replace_file 例外、workspace 内共享 observation 都维持现状，不扩大安全承诺。
- F04/F06/M01/M02/M04 的剩余产品缺口继续由索引跟踪，不被本 PR 的绿灯覆盖。

## 14. Activation acceptance criteria

以下全部满足才将本规格与索引 PR02 标记 **ACTIVATED**：

- [x] F01/F02/F03/F05/M03 五项均按本规格完成，没有只修其中 UI 外观。
- [x] 工具摘要、源 preview、保留 artifact 的完整性边界清楚；详情与复制有 exact 断言。
- [x] 独立 live 结果按 exact identity 合并；空 END、乱序、跨 scope、settlement、分页和 reload 不丢失/重复/错配。
- [x] 所有可读取的已受理队列项有正文和身份；同文提交独立，ACK/observation/消费先后均正确收敛。
- [x] 网络未知、接纳拒绝、初次大正文 hydration 中的 CANCELLED/REJECTED、queue 终态和消费后 turn 中断不被混同；没有静默消失、自动重发或执行恢复机制。
- [x] queue/artifact 读取复用既有 owner；跨 session、伪造目标、旧 attachment、observer 权限、迟到 artifact 页、实际 digest 不符及非法 UTF-8 负例通过。
- [x] cancel 不承诺续轮/回滚；approve/revise 与被接受结果一致；原 interaction/revision 绑定保留。
- [x] M03 descriptor 说明前缀与 replace_file 例外；literal N\|、partial seen、missing observation、stale 及混合 operation 测试通过。
- [x] F07 与记忆页既有回归保持；新冷 epoch 使用新 descriptor，旧 epoch prefix 连续性通过。
- [x] 第9节旧路径同次删除；无兼容 fallback、feature flag、新 hash/registry 或 durable 类别增长；无 schema migration。
- [x] focused、前端全回归/lint/build、协议生成校验、Python 完整回归均通过，失败记录真实。
- [x] 最终 source build 与 isolated wheel/launcher 的真实浏览器行为已验证，provider/截图/精确断言对应最终产物。
- [x] 本规格、索引与被修改的 active tool specification 已回写当前事实；其他未修编号不被误标完成。
- [x] 最终交接列出修改范围、删除路径、测试命令及结果、真实 dogfood 证据、剩余风险和 Git 状态。

在此之前可以提交开发代码，但不得把“已 commit”“测试总数增加”或“最终文件正确”当作 activation。

## 15. Activation 记录

本 hard cut 于 2026-09-08 基于 `2212804e` 完成，当前仅存在于未暂存、未提交的工作树。Phase A 先得到可复现红灯：前端暴露空结果、队列正文和旧 `output` 归属路径的 3 个失败，Python 暴露协议字段和 M03 descriptor 的 2 个失败；浏览器末检又先补出 artifact 独立折叠/单页末尾提示的 1 个失败。后续针对 edit diff 展示先补了 adapter 与组件两条红灯，分别证明旧路径返回“操作已完成。”且仍暴露“复制差异”。生产修复后没有删除、跳过或弱化这些断言。

最终单一路径包括：canonical result 的完整性元数据和 exact result-entry→artifact 边；entry/queue-item 精确正文读取；caller 生成的 command identity、typed prompt delivery 和 canonical input source；独立 live result 的 exact scope/call 合并；queue 正文/身份投影；按 accepted plan outcome 区分的 approve/revise/cancel 反馈；以及只修改说明、不清洗 `N|` 的文件工具 descriptor。第9节列出的最近 trace、truthy result、正文去重、optimistic transcript、通用续轮承诺和笼统行编辑说明路径均已删除或替换，没有 compatibility fallback、feature flag、新 durable 类别、数据库 migration 或新 fingerprint。

最终自动化结果：

- 协议 focused：25 passed；canonical/artifact/plan focused：79 passed；M03/Hook focused：112 passed；continuity/fingerprint focused：18 passed。
- 前端 focused（adapter/app/Markdown/memory）：132 passed；最终全回归：163 passed；源码 ESLint 通过；`build:local` 通过，最终入口为 `index-TFmj_PGF.js` 与 `index-BtUAoSIC.css`，仅有既有 500 kB chunk 提示。
- 协议生成校验通过；Python 最终完整回归为 1686 passed、19 个既有 aiohttp deprecation warnings；`ruff check .` 与 `git diff --check` 通过。
- 审查闭环后的 wheel 为 `/tmp/pulsara-pr02-audit-fix.m3mXNa/wheel/pulsara_agent-0.1.0-py3-none-any.whl`，安装到独立 venv 后从非源码 cwd 验证版本、site-packages 导入位置和打包后的最终静态入口，并按保存的生产配置以 `pulsara app --port 8876 --no-open` 成功启动。一次额外尝试把 `PULSARA_HOME` 指向 `/tmp/pulsara-pr02-audit-fix.m3mXNa/home`，Darwin 上 `/tmp` 是 `/private/tmp` 的系统符号链接，而设置读取采用逐级 no-follow 目录绑定，因此返回 `[Errno 20] Not a directory: 'tmp'`；这不是空值或空目录被拒绝。改用同一目录的真实路径 `/private/tmp/pulsara-pr02-audit-fix.m3mXNa/home` 后，空目录配置以 `pulsara app --port 8878 --no-open` 成功启动。`PULSARA_HOME` 未设置或设为空字符串仍按既有合同回退到 `~/.pulsara`。

上一轮真实 provider/browser 使用保存配置中的 `testDS · deepseek-v4.1-flash-expires-on-0910`，会话 `6aa4ed20`，只在 `output/playwright/pr02-dogfood/probe` 创建探针。覆盖文件原文与 literal `N|`、长 diff、12,000 行 retained artifact 分页/复制、直接 Firecrawl MCP 未知结构结果、root/只读 child、同文 queue/steer、observer/reload、网络未知不自动重发、plan cancel/approve/revise，以及宽窄布局和 composer 可达。用户指出 18:03 Firecrawl 卡片的交互歧义后，该轮 wheel 恢复工具卡标题栏原箭头，并把 artifact 重做为轻量终端工具栏：收起态是“查看完整输出”入口；展开态把标题、`1–23982 / 23982`、复制和独立收起图标放在同一栏，末页显示绿色“已到末页”状态而非不可操作按钮。点击收起图标只关闭 artifact 页面，工具详情和原始结果继续展开。这条 artifact 只有一页，原“下一页”无响应来自旧 disabled 状态缺少可见说明，不是 artifact owner 丢页。末检还发现终端卡通用 `footer` 选择器误把负边距施加到 artifact 底栏；最终改为只匹配直属 footer，390×844 与 1280×900 实测 artifact 的 section/header/pre/footer 均无水平溢出，“完整保留”“已到末页”完整位于边框内。`edit_file` 卡片把笼统的“操作已完成。”替换为真实 unified diff，删除冗余“复制差异”按钮；长 diff、原始结果和 artifact 内容均限制视口高度并使用独立右侧滚动槽，不截断 canonical 原文。

### 15.1 2026-09-09 审查闭环

审查指出的六项可行动缺陷均先补红灯再修复：大于 64 KiB 的 queue/entry/block canonical blob 正文能够泛化 hydrate；队列读取期间被消费时刷新 canonical 并只按 `consumed_entry_id` 改读精确 transcript entry；`prompt_delivery` 进入按 session、connection owner、command ID 管理的 process-local 提交状态机，queue 终态、输入接纳与 turn 中断分层显示，网络未知只在可用的新连接上查询原 command 而不自动重发；live result 以 attempt/channel 稳定身份保存，canonical mapping 迟到或 settlement 先到时都能精确迁移/清除；文件专用摘要和 diff 视图只由精确 builtin tool name 触发，`read_file` 区分返回窗口与文件总行数，`write_file` 使用“已创建”；待处理输入显示实际 permission 和 steer target；M03 的 `logical_lines`、items 与 `replace_file.content` schema 字段均写明显示 locator、literal `N|` 和完整原始目标文本例外。没有新增 durable 状态、数据库 migration、hash/fingerprint、执行恢复或兼容 fallback。

Phase A 的新增测试曾如预期暴露前端 10 个失败和 Python collection 缺失 typed queue transition；observer queue 终态又由单独红灯补出。修复后新增覆盖包括：大 blob 消费/reload/竞态、queue terminal、observer、多 steer/权限、`CONSUMED + TURN_INTERRUPTED`、替换连接 exact command 查询、live mapping 迟到及 settlement 前后、MCP 字段碰撞负例和 schema 字段级断言。没有 skip、xfail、断言弱化或按正文匹配替代精确身份。

用户于 2026-09-09 明确接受上一轮真实 provider/browser 结果作为本 PR 的验收证据，并要求暂不把证据附件密度作为阻断项；因此本轮没有为补写 command/queue/call/entry ID 或故障注入轨迹而重复真实 provider/browser dogfood。此处只记录该证据边界，不将审查闭环后的自动化与 isolated launcher 误写成一次新的真实浏览器会话。

详细且不含凭据的证据索引位于 `output/playwright/pr02-dogfood/activation-evidence.md`。剩余风险保持第13节所列物理边界；F04/F06/M01/M02/M04 未纳入且未标完成。

### 15.2 2026-09-10 第二轮审查闭环

第二轮审查指出四项遗漏，均以失败测试固定后再修改生产实现：旧会话 A 的提交或 interaction 在切换到 B 后迟到时，不能重连 B 或向 B 发布 A 的 ACK/toast；大于 64 KiB 的 queue 正文在初次 hydration 中转成 CANCELLED/REJECTED 时，保留原 queue/command 身份并用原 command 精确查询 typed 终态；artifact 页读取绑定 result/session/connection/request revision，在折叠、身份变化和卸载时失效；canonical blob 在浏览器端对实际组装 bytes 重新核对既有 SHA-256，并以 fatal UTF-8 解码拒绝损坏内容。前端初始红灯共 11 个失败；artifact 两个 deferred-promise 测试经修正测试夹具后单独确认旧实现确实接收迟到内容。没有弱化断言、skip/xfail、自动重发、durable registry、数据库 migration 或新 fingerprint。

最终自动化以当前工作树重跑：adapter/app focused 123 passed；adapter/app/Markdown/memory focused 143 passed；前端全量 174 passed；ESLint、`build:local` 和协议生成校验通过。Python focused 分别为 25 passed（协议/web，5 个既有 aiohttp warning）、79 passed（canonical/artifact/plan）、112 passed（M03/Hook）、18 passed（continuity/fingerprint）；`ruff check .` 通过；Python 全量 1686 passed、19 个既有 aiohttp deprecation warnings；`git diff --check` 通过。最终静态入口为 `index-B-bNmNHv.js` 与 `index-BtUAoSIC.css`。

最终 wheel 构建于 `/private/tmp/pulsara-pr02-final.lcFOgv/wheel/pulsara_agent-0.1.0-py3-none-any.whl`，安装到独立 venv 后从非源码 cwd、保存的生产配置在 `127.0.0.1:8879` 启动；导入路径来自该独立 venv，首页与 bootstrap 均来自安装包。真实浏览器打开该最终 wheel，复读已接受的真实 provider 会话 `6aa4ed20`：18:03 Firecrawl artifact 完整读取到 `1–23982 / 23982`，独立收起和工具卡折叠均正确；1200 px 下 artifact `clientWidth/scrollWidth = 616/616`，390×844 下为 `324/324`，“完整保留”“已到末页”均在边框内，composer 位于可视区且可用。最终截图为 `output/playwright/pr02-dogfood/artifact-final-wheel-desktop.png` 和 `artifact-final-wheel-narrow-390.png`；控制台只有非功能性的 `favicon.ico` 404，无应用 warning。用户已明确接受上一轮真实 provider 行为证据，本轮没有重复发起模型或工具执行，只用最终安装包重验 canonical 会话和受影响 UI。

至此四项审查缺陷、对应回归、最终 build/wheel/launcher/browser 均闭合；第14节仍全部满足，规格保持 ACTIVATED。剩余边界仍仅为第13节列出的物理不完整输出、process-local 未决态和未纳入的 F04/F06/M01/M02/M04。
