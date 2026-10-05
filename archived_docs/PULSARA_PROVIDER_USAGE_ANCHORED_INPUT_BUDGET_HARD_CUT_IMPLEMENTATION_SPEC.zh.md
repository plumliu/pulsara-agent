# Pulsara：Provider usage 持久化与输入预算实测锚点实施规格

状态：已实施，验收通过，代码冻结。日期：2026-10-04。主代理完成落地，GPT-6 Astra/high critic 完成代码审查及证据复核，双方认可最终冻结；无剩余必须修复项。

## 1. 产品结果与范围

用户可以跨重启查询过去模型响应实际报告的 token 数。正在运行的对话优先以最近一次兼容请求的 reported input 校准输入预算，只估算其后追加到 provider 的内容；usage 缺失不会阻断会话。

本轮覆盖 `DirectKernelModelPort` 的 `AGENT_MODEL_LOOP`：ROOT 与 SUBAGENT_TASK 分别计量、分别维护 live anchor。保存的是一个逻辑模型执行可观察到的响应 usage，不是 SDK 内部每个可能计费的 HTTP retry attempt。

compaction summary、标题、memory sidecar、connection probe 不参加前台 epoch 锚点，也不进入本轮持久化查询的覆盖范围。本文不承诺全应用账单完整性，不新增模型工具、GUI context meter、累计统计面板或 tokenizer 库。

本轮是一个 hard cut：新的预算 authority 贯穿实际请求准入、输出上限、压缩与 follow-up 准入；不会保留旧启发式硬门作为另一个同时生效的准入来源。启发式仍是无锚点时的正式策略，以及语义分项与 wire 原始估值的诊断来源。

## 2. 实施前代码事实与研究依据

本规格最初制定时，`PulsaraHeuristicTokenEstimatorV2` 提供文本、JSON、图片和 framing 估算。compiler 同 epoch 已增量估算 suffix，但历史基准仍是旧启发式计数。provider usage 从 adapter 到 normalized terminal，再进入 host 的 operational hook；当时没有 canonical usage 表或实测预算校准。当前落地结果见第 11 节；文字计数已按第 12 节切换为 v3。

生产同时有两个不同的数字：

- `TokenEstimate` / `compiler.final_estimate`：语义内容分项估算，满足 system + messages + tools + envelope 恒等式。
- `FrozenProviderWireInputQuote`：最终 SYSTEM/tools、ordered wire items、provider replay 和图片的请求估值。当前它驱动输出 lowering、dispatch、compaction 与后续资源准入。

reported input 对应最终实发请求，不能填入语义分项并伪造其分解。

四份 Luna 调研与 Astra critic 的细节见 `output/token-usage-research-20261003/`。其中值得保留的实践是请求级 usage 与当前预算分离、持久化独立于消息是否有可见正文、对缺失字段保留未知；不复制跨模型旧锚点、跳过锚点回复、全量 output 等同未来 input、缺失归零等行为。

主要实施入口：

| Owner | 当前文件 | 本轮职责 |
|---|---|---|
| usage 规范化 | `primitives/model_call.py`、`llm/result.py`、`llm/adapters/openai/events.py` | partial 字段、reported/computed total 分离、逐字段验证 |
| 协议生命周期 | `llm/adapters/openai/chat_completions.py`、`responses.py`、`llm/normalized_transport.py` | 复用 SDK 与既有协议 owner，输出一份本响应规范化 observation |
| 一次模型执行 | `conversation_kernel/direct_model.py` | 绑定实发 wire 输入、捕获终态、物理关闭后交付 observation |
| live epoch | `conversation_kernel/input_continuity.py` | 在既有 scope slot 内持有一个可选 live anchor，遵循 epoch 生命周期 |
| 最终请求预算 | `llm/request.py`、`conversation_kernel/direct_model.py`、`provider_dispatch.py` | 唯一有效输入预算估算、provenance 与物理资源分离 |
| 消费者 | `runner.py`、`steer.py`、`compaction/*`、`llm/resolution.py` | 统一读取预算估算；不再用语义/raw wire 估值绕过它 |
| canonical storage/read | `conversation_kernel/_repository/`、`repository.py`、`query.py`、`host.py` | 最小逐执行 usage 行、best-effort 写入、分页查询 |
| schema | `storage/migrations/sql/0000_conversation_kernel_baseline.sql` | 更新 clean-v0，不新增兼容迁移链 |

## 3. 计数语义

### 3.1 报告的值与估算的值

`reported_input_tokens`、`reported_output_tokens` 是 provider/SDK 对该次响应报告的规范化 inclusive 计数；它们不是每条历史消息的分词结果。cached input 是 input 的子集；reasoning output 是 output 的子集。不能重复加计。

provider 报告的 total 保留为 `reported_total_tokens`，不以 input+output 覆盖它。`computed_total_tokens` 是只读派生值：仅当 input 与 output 都已知时等于两者之和，否则为未知；不额外落一个冗余列。报告不一致用诊断表达，不捏造一致性。

“reported”表示服务端报告，不表示客户端证明服务端内部 tokenizer 正确，也不表示完整计费账单。`estimated_input_tokens` 表示将用于下一请求的估值，即使 suffix 为空也不标为本次请求实测。

### 3.2 Partial hard cut

规范化 usage 的 input、output、total、cached input、reasoning output 均可独立未知。状态：

| 状态 | 含义 |
|---|---|
| `reported` | input 与 output 均有效；细分字段和 provider total 可缺失 |
| `partial` | 至少一个有效 token 字段，但 input/output 不齐 |
| `missing` | provider 未提供 usage 或提供空对象 |
| `invalid` | usage 存在，但没有可用 token 字段；保留诊断代码 |

每个字段独立验证。接受非负整数，拒绝 bool、float、数字字符串、负数和超出 PostgreSQL bigint 范围的值；bigint 范围是本次单响应记录的存储表示边界，不是会话总消耗或生命周期 cap。坏字段置未知并附诊断，不能导致有效 output/message 失败。非 object 的 usage 同样产生 invalid observation，不能因解析 usage 破坏合法响应。

input 可取 `input_tokens` 或 `prompt_tokens`；output 可取 `output_tokens` 或 `completion_tokens`，遵循当前 wire API 的主字段，别名同时存在且冲突时保留主字段并记诊断。不得从 total 减未知 output、cache 合计或字符数制造 reported input。

若 cached > input 或 reasoning > output，该细分字段无效并记诊断；顶层有效计数仍保留。顶层未知时保留有效细分观测，但细分不能使输入锚点合格。

更新 `ModelTokenUsageFact` 与 `TransportUsageReport`，移除“input/output 任一缺失则整份丢弃”和“reported total 必须等于 input+output”的旧契约；所有 adapter status gate、normalized terminal、hook payload 和测试同时切到新事实。

### 3.3 Streaming

不逐 token 推算真实 usage，不新增自写 SSE parser、transport retry 或等待最终 usage 的 grace timer。继续由现有 SDK/adapter lifecycle 判定 terminal 与物理关闭。

“已经报告”在此仅指 adapter 在既有认可的 carrier 中实际捕获并交付的字段。尚未读到、只存在于 SDK 内部状态或在取消前未向 Pulsara 暴露的 usage 仍为未知；不重新打开/读取已关闭 stream 来寻找它。

Chat 仍只在既有认可的终态或 usage-only carrier 位置采纳 usage；同一响应合法 carrier 可逐字段保留此前已报告值，后续有效值覆盖，缺失不抹掉此前值。不得对 cumulative usage 求和，不得跨 SDK retry attempt 合并。

missing/invalid carrier 或坏字段不得擦除同响应此前已有效的 input；冲突和非法字段保留诊断。响应级合并后再检查 breakdown/parent 与 total 关系，不以某个新 carrier 的不完整状态替换已捕获的完整事实。

Responses 继续采用现有 terminal response usage 和重复 report 协议规则；partial/invalid observation 不再被 `reported` gate 无声丢掉。规范化执行最终只交付一份 observation。提前断流、没有 terminal carrier 或取消时，没有报告的数保持未知。

## 4. 三层预算策略

### 4.1 锚点测量边界

一个 live anchor 属于既有 `ProviderInputContinuityScope` 的当前 epoch，携带：

- 本执行 `resolved_model_call_id` 与已有 epoch nonce/revision；
- 请求实际输入的冻结 wire materialization 引用及 ordered item 结束边界；
- 冻结有效 target/profile 与固定 context-bearing projection；
- 已通过现有 identity policy 接受的 reported model identity（可未知）；
- 有效 reported input；
- 该请求的 raw final-wire heuristic 输入计数。

保持一个最新合格锚点即可；挂在既有 scope slot，复用不可变输入对象，不建立新的 scope registry、hash registry、durable epoch 或全历史 live usage 集合。这些值只用于预算观测，不取得 install、execution 或 source authority。

边界是**上一请求的输入末尾**，不是该请求生成的 assistant 末尾，也不是 semantic message 数。一个 Responses assistant 可以成为多个 wire items；Chat 聚合与 replay 替换也不能按语义数组索引处理。

### 4.2 合格条件

以下条件全部满足时，新的 observation 可以更新 anchor：

1. 属于实际已打开的当前 scope/epoch 执行，而非被丢弃的 preparation、候选测量或其他调用 purpose；既有 physical completion 已确认，close/replay 契约没有失败。
2. 规范化终态为 `COMPLETED` 或 `OUTPUT_INCOMPLETE`，有有效 input；partial input-only 也可用。
3. 该报告与请求冻结的 target 通过现有 `ModelIdentityPolicy`。不能另加“reported id 必须等于配置 model id”而拒绝合法 alias。已观察 reported identity 改变时，旧锚点不继续代表新 identity；当前兼容请求的新报告可以建立新锚点。
4. 实发 materialization 已知，固定 context 与 wire input prefix 可按当前连续性 owner 证明对应。
5. 对 Pulsara 的非空 context 请求，input=0 保留为报告值，但不作为锚点，并记相应诊断。不能把缺失归零占位符当精确基准。

对 `PROVIDER_ERROR`、本地取消、协议失败等 observation，保存已经报告的值，保守不建立新 anchor，也不因失败清除仍兼容的旧 anchor。失败 input 未必不准确；不锚定它是本轮产品选择，而非协议普遍事实。

同 epoch 的新 observation 只有来自当前认可的执行，且其 epoch revision/ordered input 边界不早于当前 anchor，才能替换 anchor。旧请求迟到只进入历史观测，不让较新的锚点回退；复用已有 revision/item cursor，不新增 generation。

### 4.3 公式与 fallback

令 `W` 为当前待发送 final wire 输入，`A` 为合格 anchor 对应的实际请求输入，`H` 为当前冻结 estimator 的 raw final-wire heuristic：

```text
兼容锚点存在：
  estimated_input_tokens(W)
    = reported_input_tokens(A) + H(actual final-wire suffix after A)

没有兼容锚点：
  estimated_input_tokens(W) = H(W)
```

现有 ordered wire estimator 对 item 组件可加，因此证明前缀完全对应后，可以使用等价的 `H(W) - H(A)`；差值应非负，不能以 `max(0, delta)` 掩盖前缀漂移。增量必须复用既有 estimator 与图片 source quote，不能按 raw JSON 字符数另造一套算式。

| 观测情况 | 策略 |
|---|---|
| 最新响应有合格 input | 刷新 anchor；新请求估其后 suffix |
| 最新 usage 缺失/invalid/只有 output，或失败不合格 | 继续使用仍兼容的旧 anchor；suffix 从旧请求输入边界累计到当前 |
| 从未有锚点，或 anchor 不兼容 | 全量 raw final-wire heuristic |

suffix 包括刚生成且实际回灌的 assistant、tool calls/arguments、tool results、用户消息、steer、runtime/hook 注入、图片及 replay items。只计算实际 provider 投影；可见 UI 正文长度、canonical transcript 长度、output_tokens 都不能代替它。SYSTEM/tools 与请求固定包装已经在 anchor input 中，不重复相加。

保存 output 供历史观测；不将全部 output、reasoning detail 或 total 直接加成未来 input。新 opaque reasoning/ciphertext 仍由当前 suffix heuristic 处理，可能过估；后续新 reported input 能覆盖这一历史部分，但本轮不保证新 opaque suffix 可精确计数。

### 4.4 兼容与失效

兼容不仅是同一 session/model 名称，还包括当前 scope、epoch、有效 target/route/wire API、estimator、replay codec、固定 context-bearing projection 与 ordered item prefix。固定 projection 包括 tool_choice、profile defaults、影响 context 的 extra body；复用现有完整冻结值、exact equality 和连续性机制，不增加兼容 fingerprint。

- cold epoch、host 重启、采用 compaction successor：清空 live anchor，全量启发式起步；历史 usage 行仍可查，不恢复执行或热恢复 anchor。
- 模型/路由/输入投影不兼容：停止使用旧 anchor。沿用既有切模与 handover/compaction 路径；**计数失效不是新的根重建许可**。
- 仅限额/output cap 变化：按固定 context、有效 identity 与输入投影判断，不机械要求整个 target 的资源限额逐字段相等；本次保守无法证明兼容时 fallback。
- 缺失 usage、UI 操作、hook/MCP/skill 变化：不触发根重写。若合法追加到当前 epoch，则仍是 suffix；若不能保持前缀，执行既有连续性错误路径。
- usage 来自已失效 epoch 或迟到执行：只可作为历史观测；不写入当前 live anchor。

透明 fallback 只处理“没有测量依据”的情况。实际违反当前 epoch prefix 契约不能通过全量估算隐藏或擅自修复。

服务端静默切换上游 tokenizer、输入包装或隐藏状态，在下一报告前无法由客户端可靠检测。兼容锚点仍是估算依据，不承诺服务端行为完全稳定。

## 5. 唯一预算 authority 与消费者

保留 raw semantic estimate 与 raw final-wire estimate 的诊断意义和各自恒等式。新增/明确 final-wire quote 的**有效预算输入估值**及其 provenance；raw 计数不能再充当另一道 token 准入硬门。字段命名在本轮统一为：

- `raw_final_wire_estimated_input_tokens`：现有 replay-adjusted heuristic；
- `budget_input_tokens`：第 4 节算法选出的唯一预算值；
- `budget_source`：`heuristic` 或 `reported_input_anchor`；
- `anchor_model_call_id`、`anchor_reported_input_tokens`、`estimated_suffix_tokens`：有锚点时的可解释分解，无锚点为空；
- `final_wire_utf8_bytes` 与既有视觉/结构 quote：保持物理事实原义。

原 `final_wire_estimated_input_tokens` 的 raw 等价字段统一更名并更新全部引用，不保留旧名 alias/双读。semantic/raw 分项仍可展示，但不能伪称分项之和等于经校准的 budget_input。

`budget_input_tokens` 可以低于当前 raw heuristic；不能取 `max(raw, calibrated)`，否则实测无法修正显著高估。它也可以高于 raw heuristic。保留现有 input safety margin、共享窗口和 provider output ceiling，不额外发明校准倍率、最低压缩触发值、锚点年龄或调用次数 cap。

必须同步更新：

1. adapter 请求构造与 `resolve_wire_output_tokens`，输出空间按 budget_input 计算。
2. provider dispatch、wire plan validation、候选测量与 adopt/install 前的 token admission。
3. 自动/强制/切模/continuation compaction 的 source、successor 与 trigger；改变 prefix 的候选没有旧 anchor，只用其自身 heuristic。
4. runner/steer/hook sibling、tool result 与 post-response follow-up token admission。
5. semantic compiler 的提前 token rejection、源内容退化/裁剪与 direct-model validation。不能因为 raw semantic 过估而提前拒绝或退化；预算决策必须取得对应 final-wire 候选的 budget quote。cold/no-anchor 也以 raw final-wire heuristic 作为唯一 token 预算，维持的是 estimator 与退化顺序，不保留旧 semantic token 硬门。
6. operational diagnostics：分别标识语义/raw 估算与有效 budget estimate，避免旧 `total_input_tokens` 被误读为 provider reported input。

既有 canonical bytes、logical epoch bytes、wire bytes、item/结构/图片引用、provider replay 完整性、native tool/schema 与 deadline 等非 token 约束保持原 authority，不受校准减小影响。

visual token 分项仍属于 raw heuristic，不是物理图片大小事实；维持 `visual <= raw estimate` 的分项关系，但不得新增 `budget_input >= raw visual tokens` 的隐性下限。图片数量、尺寸、字节与引用合法性仍由既有独立 physical contract 保护。

每个新 quote 捕获不可变的 anchor snapshot 与当时输入对象。usage 到达不回写已经冻结的 installed prefix/旧 quote。post-response 产生新的资源 quote 时，可用刚收到的合格 input 重新校准该请求前缀，再估算回复与 prospective tool-result suffix；不能继续沿用请求前旧预算而遗漏最新实测。

### 5.1 候选生成与准入顺序

compiler 只生产结构完整、物理边界有效、**尚未 token-admit** 的 semantic 候选。复用已有 `FrozenModelInputSemanticProjection` / `ProviderWireSemanticInput`、`PreparedProviderWireCandidate` 与 `ProviderWireMeasurement`；不建立第二个候选框架或 scheduler。

`ContextCompileBudgetReport` / `FrozenCompiledModelInput` 等纯语义/raw 报告的 `raw total <= budget` 约束移除；分项恒等式、身份精确 join、来源/完整性、物理结构与 bytes 约束保留。若最终 compiled 对象在 wire admission 后才创建，同样不得把 reported input 填进它的 raw semantic 分项来满足旧断言。

既有 dispatch owner 采用以下单向流程：

1. 取得本次现有编译/执行 deadline 与一次不可变 anchor snapshot，产生初始完整 semantic 候选。
2. materialize/hydrate 到实际 final wire，按该 snapshot 生成有效 budget quote。
3. 若超限，按已有 source/tool-result sacrifice 顺序与 render floors 生成下一个合法候选，再 materialize/hydrate、重测；只调整候选允许变化的部分，既有 epoch prefix 不动。
4. 候选 floor 必须严格推进，复用现有有限 variant/physical working-set 与 deadline；不引入新的次数、历史或生命周期 cap。穷尽仍超限则返回现有 typed budget/compaction outcome。
5. 只有最终 `FrozenProviderWireInputPlan` 对 budget_input 与既有 physical boundaries 完成 token admission 后，才进入现有 prepare/install/open authority。

同一次候选选择的所有重测使用同一 anchor snapshot，防止中途校准变化使选择结果漂移。compiler 不得为了取得 quote 递归调用 dispatch；dispatch 不在 measurement 中打开 provider。raw semantic 可用于候选排序和诊断，但不能先裁掉本可容纳的完整内容；第一次候选必须在已有 physical/render 合法范围内保留完整可选内容。

没有 anchor 时也走同一路径，`budget_input = raw final-wire heuristic`。这保留当前启发式 estimator 和退化政策，明确切除旧 semantic 提前准入路径，不声称 cold 请求的每一个退化结果逐字保持旧实现。

## 6. 持久化设计

### 6.1 必要性与 oracle delta

新增一张 canonical relational 表 `pulsara_v3.provider_call_usage`。独立产品理由是跨重启查询每次实际可观察响应的 reported token 数；不是证明调用成功的 receipt，也不负责执行恢复。

turn 一次可含多次模型调用；assistant blocks 是内容块；replay 表只服务特定 codec 和成功 assistant settlement。不能把 usage 绑到这些 owner，造成空响应、incomplete、失败或无可见正文的观测丢失。

冻结 delta：product relation/table +1；durable event kind +0、live event kind +0、subject +0、append guard +0、durable job +0。live anchor 是既有 scope slot 的 typed process-local 附属观测，不增加主体 slot 或执行 authority。

### 6.2 行粒度与列

一行对应一个实际打开的 `AGENT_MODEL_LOOP` 逻辑执行。主键复用已有 `resolved_model_call_id`（每次准备生成 UUID），不新造 ID 或 fingerprint；`model_call_index` 仅展示，不作为跨重启唯一键。

| 列/值 | 规则 |
|---|---|
| resolved_model_call_id | 主键，复用执行携带的已有值 |
| session_id、turn_id | 组合 FK 至 `turns(session_id, id) ON DELETE CASCADE`，session 删除沿其既有 cascade；scope 从这个确切 turn owner 读取，不能分别验证两列而错配 session |
| model_call_index | 本 turn 调用序号，非唯一键 |
| connection_id、route_id、wire_api、requested_model_id、reported_model_id | 取执行冻结值与规范化 report，不读终态时可能已变化的 session selection |
| observed_at | 首次成功插入的数据库写入时刻，用于查询排序；不是 commit 完成时刻或 provider 开始/完成时间证明 |
| normalized_terminal_kind | 观察到的既有 terminal kind，可为空；无 terminal 时保存明确诊断 |
| usage_status | reported/partial/missing/invalid |
| input_tokens、output_tokens、cached_input_tokens、reasoning_output_tokens、reported_total_tokens | nullable bigint；未知为 NULL，真实零为 0 |
| diagnostic_codes | 既有 typed diagnostic codes 的小型列表，保存不一致/缺失原因 |

不保存第二份完整 prompt、wire body、opaque replay 或 provider 原始 usage JSON，不保存持久 anchor、request-prefix cursor、预算恢复 checkpoint。输入计数属于整份实际请求，不能从查询行推导历史消息逐条 token。

### 6.3 写入事务与失败语义

direct execution 在收口前冻结已捕获的规范化 observation；以既有 try/finally 完成 transport aclose、physical completion 等待和 borrow 释放，然后在仍有 cleanup 控制权时，经 host 的现有 repository 执行通道尝试异步写入。不能在 SSE 热路径逐 chunk 写库，不能让 usage 写入延后物理 transport 收口。

取消或 close/wait 失败时，同样可以 best-effort 保存此前已捕获的观测；不要求关闭一定成功才保存历史行。physical completion 或 replay 契约失败则不建立新 live anchor。存储异常只能变成独立 diagnostic，不得替换原取消、协议错误或 physical-close 异常；不能重新进入已关闭 stream 或解析 SDK 私有状态取 usage。

写入由 ConversationKernelRepository 拥有，以单行短事务插入，不与 assistant 文本/replay settlement 绑为同一成功条件。同一执行 observation 若代码重复交付，比较其冻结身份、terminal、usage/status、diagnostic_codes 等 observation payload；主键相同且 payload 相等则幂等，不等则 diagnostic/conflict，不覆盖原行或合并两个逻辑调用。数据库生成的 observed_at 不参加 payload 相等比较，取首次成功插入值且不更新。复用数据库约束，不引入 append guard/receipt。

持久化是**best-effort observation**：正常写入成功后可跨重启查询；数据库不可用、删除竞争、进程在 commit 前崩溃或本地取消 cleanup 无法完成时，允许最后一行缺失。写错要有 operational/log diagnostic，不能无声假装已保存；不得令已经完成的模型响应失败、触发 provider 重试或新增 durable repair job。

能获得 cleanup 控制权的已打开执行，没有 terminal usage 时尝试记录 missing 和对应诊断。被丢弃/未打开的准备不写 usage 行。硬崩溃没有完整 started/closed 执行账本，本轮不恢复它；不能把行数解释为所有物理请求数。

live anchor 从可信输入 observation 更新，不以数据库 commit 为成立条件；存储暂时失败时仍可用于当前 epoch。反过来，历史行存在不授权恢复当前 epoch anchor。

沿用既有数据库/cleanup 每操作 deadline 与 cancellation 收口机制；不增加无限阻塞、后台保存任务、全局重试队列、新的 wall-clock cap 或用户等待 approval。

既有 `PROVIDER_USAGE_OBSERVED` operational hook 继续发出，payload 切到 partial nullable 字段与 reported/computed total 语义；它不成为持久化 owner，也不新增 live event kind。

### 6.4 生命周期与读接口

永久删除 session/turn 按 FK cascade 删除相关 usage；归档保留。fork/import 不复制 usage 成新执行，源 session 的实测不成为目标会话锚点。没有按 30 天等任意历史期限清理的政策。

在 `CanonicalConversationQuery` 与 host/query port 增加一个只读 `page_provider_call_usage`，按 session 及可选 scope 过滤，用 `(observed_at, resolved_model_call_id)` keyset 分页；复用当前 canonical page 的 1–1024 单页上限与默认 256。该限制只约束单次读操作，可持续翻页，不限制历史总行数。

查询返回逐行 reported 字段、computed total（已知才给）、model identity 与缺失状态。不新增累计 summary DTO；未来统计必须带 coverage，不能 SQL SUM(NULL) 后声称完整总消耗。本轮通过此 read port、现有观测 hook 与 dogfood 证据提供可检查结果，不增加新 GUI 页面或模型工具。

## 7. 不采用的替代方案

- 不维护所有模型 tokenizer；当前启发式由现有 owner 继续提供。
- 不按 reported_total/heuristic 的全量比例缩放后续所有内容。固定 SYSTEM/tools、自然语言、tool JSON、图片、opaque replay 的误差并不同比；倍率也不能证明兼容前缀。
- 不将 input+output 当下一次输入，不扣除 reasoning detail 后把“visible output”当可重放输入。以实际 wire suffix 为准。
- 不新增 provider count endpoint 或请求前计数网络调用。本轮要利用已有响应观测，避免增加延迟、服务依赖与兼容服务特例。
- 不使用历史累计消耗决定当前窗口，不把 cached token 当免费上下文，不将丢 usage 等同丢响应。
- 不复制其他仓库的全量 event ledger、retention cap、跨重启 anchor recovery 或 SDK retry state machine。

## 8. 实施顺序

1. partial usage primitive/parser/terminal hard cut，补齐 Chat/Responses status gate；更新 operational payload。
2. canonical usage 表与 repository/query owner，写入失败与无终态路径；更新 clean-v0 baseline、删除生命周期与 oracle delta。
3. 既有 epoch slot 的 live anchor 与兼容判定，先证明 request-input/wire item 边界。
4. final-wire budget quote hard cut，统一 raw 命名、有效 budget/provenance 与全部消费路径；清除 semantic 过估提前阻断。
5. focused integration、real-provider 与长程 dogfood，修订当前 README/contract 文档，移除 superseded code 与旧字段。

不能以“usage 已存表”或“UI 已显示数字”作为完成；所有 token admission 必须消费同一有效预算值，且现有物理资源约束持续成立。

## 9. 验收矩阵

| 场景 | 必须证明的结果 |
|---|---|
| 完整 Chat/Responses usage | reported input/output/cache/reasoning 原义保留，total mismatch 可查而不覆写 |
| input-only / output-only / total-only | 独立字段持久化；input-only 可合格锚定，其他不得制造 input |
| missing、invalid、真实 0 | 三者可区分；坏字段不破坏合法回复；0 占位不能清空预算 |
| 连续三次请求，第二次丢 usage | 第三次继续旧 anchor，新增 assistant/tool/user 后缀恰计一次；第三次成功后刷新 |
| 新回复含 tool calls、多 Responses items、replay/opaque | cursor 使用 wire item 边界；不漏回复、不把 output 总量再加一次 |
| SYSTEM/tools 已含缓存 | anchor 已覆盖固定部分与缓存 input；cache/reasoning 不重复累加 |
| 异步迟到 usage、ROOT/child 并行 | 不跨 scope、epoch 或模型覆盖 anchor；storage 身份来自实际 request |
| accept_reported alias / strict mismatch | 服从现有身份政策；合法 alias 可用，strict mismatch 不锚定 |
| 模型/路由/固定 projection 变化 | 无法证明兼容则无锚点 fallback；不增加根重建边界，不隐瞒连续性错误 |
| cold/restart/采用 compaction successor | live anchor 清空；历史行可读；source/successor 不混用计数 |
| raw semantic/wire 明显高估而 reported 较小 | 下一合法请求被校准值接纳，保留必要 sources；旧 semantic 硬门不能提前拒绝 |
| reported 较 raw 大 | output cap、compaction trigger、dispatch、steer/follow-up 同步使用更大的 budget |
| 新 observation 到达后的 follow-up quote | 使用最新实测 input 加实际/有依据的 prospective suffix，不修改 installed quote |
| 物理 byte/item/opaque 完整性超界 | 即使 calibrated input 很小，既有物理保护仍拒绝/降级 |
| 正常终态 missing/取消/失败/无正文 | 可观察时保存一行；已有字段保留；模型成功不依赖 usage 写入 |
| DB 写失败、重复提交、session 删除竞争 | 不重试 provider、不新增恢复机制；有诊断；相同主键值相等幂等，不同值不可覆盖 |
| restart history / fork / archive / delete | 历史正确查询；不制造新会话实测；生命周期与第 6 节一致 |
| 长程工具与分页 | 不新增 turn/call/history lifetime cap；超过单页仍可持续读全历史 |

focused tests 使用现有 `.venv`/uv，覆盖 primitive/adapters、epoch/dispatch/runner/compaction 与 PostgreSQL integration；不要用镜像实现测试替代真实消费者验证。

real-provider dogfood 使用 `LocalSettingsStore` 和 `require_pulsara_home()` 读取用户保存的生产连接/route/credentials，作为只读输入；隔离时复用既有注入与已验证本地 disposable DB。至少在已配置的 Chat 与 Responses 路径分别观察一次实际 usage；某路径未配置或 provider 不返回 usage，明确记录缺口，不造配置、不改 saved settings。

缺失/partial/取消通过确定性的 transport fixture/integration 注入验证，不要求真实 provider 恰好触发。真实证据应保留实际请求与 reply、normalized usage、raw/budget estimate、anchor/suffix 分解和 prefix continuity，排除实际 credential 值；不加每文件 SHA 或无关证明机制。

## 10. 冻结条件

规格冻结须由主代理与 GPT-6 Astra/high critic 明确认可：计数和 partial 口径、final-wire 唯一预算、scope/epoch 兼容、best-effort 持久化、唯一新增 relation、物理资源保护、跨重启降级与 SDK retry 覆盖限制均已闭合；文档不存在要求实施者自行决定的产品边界。

功能激活另须完成第 9 节验证与必需 dogfood，并更新当前 contracts/tests/oracle。规格冻结不等于代码、测试或生产行为已经通过验收。

冻结审阅记录：critic 首轮提出 compiler 候选与 wire measurement 的依赖环、cold 模式预算歧义、取消/close 异常优先级、幂等 observed_at、组合 FK 和迟到锚点问题；主代理已按第 3–6 节修订，并补齐 raw visual 分项不能形成预算下限。最终完整复核后，critic 明确“认可冻结”，主代理亦认可；本规格没有待实施者决定的开放产品边界。


## 11. 落地与代码冻结记录（2026-10-04）

生产链路已按第 3–6 节完成 hard cut：逐字段 partial usage 规范化、物理关闭后 best-effort canonical 写入、跨重启 keyset 查询、scope/epoch 内实发 wire input 锚点，以及所有 token admission 消费唯一 `budget_input_tokens`。旧 raw 字段已更名，semantic/raw 估值保留为诊断，物理 byte/item/replay 约束继续独立生效。clean-v0 唯一新增 relation 为 `provider_call_usage`；未新增 event、subject、guard 或 job。

最终代码复核修正了 source compaction 校准、写入失败诊断、取消异常优先级、cold successor 不继承旧 source head 禁省略规则，以及 memory 既有失效 carrier 在最终 wire 超限时的 fallback。fallback 保留既有 source/tool sacrifice 顺序、同一个 anchor snapshot 与 source collection 的 exact join；steer headroom 由最终 wire quote 驱动降级与拒绝，不恢复 semantic token 硬门。

验证结果：

- 最终全量：`.venv/bin/python -m pytest -q`，**2726 passed，55 warnings**。包含既有 64 次模型调用、超过 64 个工具调用、24 次调用后 steer/cancel 和有限安全压缩边界等长程回归；没有新增会话生命周期 cap。
- 83 项专项覆盖 partial/parser、wire anchor、memory fallback、cold source floor、最终 admission、PostgreSQL 与真实 Host。Host fixture 连续三次请求的第二次缺 usage，第三次继续第一锚点并累计完整实际后缀；随后真正采用 `COMPACTED` successor、清旧锚点，下一请求 heuristic，summary 不纳入前台 usage 表。
- 只读复用保存的 Chat / Responses 生产连接，两协议真实 dogfood 均通过。记录实际 provider 输入与回复，验证 fixed projection 相等、ordered input 严格前缀追加、anchor 指向首 call、reported+suffix 分解及 cold history read。
- Chat 第二请求 budget 为 `24210 + 860 = 25070`，raw 为 `52323`；Responses 为 `24204 + 933 = 25137`，raw 为 `52154`。
- 真实 provider compaction 验证 source quote 使用锚点及 summary 排除；两次结果均为 `NOT_NEEDED`，没有采用 successor。实际采用后清锚点的证据来自确定性 Host fixture，不能归属于真实 provider dogfood。
- Ruff 与 `git diff --check` 通过。验收使用隔离的本地 disposable DB，未改写保存的设置或主开发数据库；旧 clean-v0 开发库需要重建后使用新 schema。

证据与执行说明见 [验证记录](../output/provider-usage-budget-20261004/verification.md)、[最终全量日志](../output/provider-usage-budget-20261004/full-tests.log)、[真实调用证据](../output/provider-usage-budget-20261004/dogfood.json)。这些 output 文件是本地验收产物。

GPT-6 Astra/high critic 最终结论：“最终认可代码冻结……没有剩余必须修复项。”主代理同意，代码与本规格均已冻结。

## 12. 文字启发式 v3 更新（2026-10-06）

用户已明确授权将文字计数替换为以下单一公式，普通文本与 canonical compact JSON 共用：

```text
H_text(s) = ceil(non_cjk_utf8_bytes(s) / 4 + cjk_code_points(s))
```

Unicode 名称与属性由 Python 标准库 `unicodedata` 提供。Pulsara 将名称以 CJK、IDEOGRAPHIC、HIRAGANA、KATAKANA、KATAKANA-HIRAGANA、HANGUL、BOPOMOFO、HALFWIDTH KATAKANA 或 HALFWIDTH HANGUL 开头的码点归入中日韩文字；Unicode category 为 `P*` 且 East Asian Width 为 `W` 或 `F` 的标点也按一个码点一个 token 计数。其他文字按实际 UTF-8 字节数除以四。全角拉丁字母、其他语种文字和 emoji 仍走其他文字的字节规则。每个既有计数组件只向上取整一次；JSON 的引号、键名、转义与标点仍计入序列化文本，没有独立 `/2` 倍率。

唯一生产实现为 `PulsaraHeuristicTokenEstimatorV3`，其事实版本为 `v3`。现有 estimator 身份包含公式、分类规则和标准库 Unicode 数据版本，以区分 provider target/前缀预算边界；没有新增 fingerprint 类别、持久状态或恢复机制。删除 v2 实现与旧字符/JSON 常量，不保留兼容别名。Hook 的既有 prospective byte 上界引用四字节/token 常量：v3 中每四个 UTF-8 字节至少产生一个估算 token，该物理上界仍成立。

第 3–6 节的 input anchor、provider/model 隔离、ordered wire 组件可加性、framing、图片估算、物理资源约束和输出 token 上限继续按原契约工作。算法更新只在正常进程重启后的 cold epoch 生效，不为正在运行的 epoch 添加根重写边界。此前章节的 v2 数字与验收记录是当时证据，不是 v3 的计量结果。

本次验收覆盖中日韩、宽标点、其他 Unicode、JSON 转义，以及真实 Chat/Responses 的 input usage 锚点与 prefix 连续性。首次完整回归为 2791 passed、51 failed；修订依赖旧密度的测试样本后，核心专项 314、四个边界模块 83、完整 runner 模块 127 项均通过，覆盖全部失败项。真实 Chat 首次估算 25,917、实报 24,204，Responses 首次估算 25,796、实报 24,204；后续正常使用 reported input anchor。Ruff 与 diff 检查通过。完整命令没有在样本修订后重复执行，详见 [v3 验证记录](../output/text-estimator-v3-20261006/verification.md)，保留首次失败日志和最终复验日志。
