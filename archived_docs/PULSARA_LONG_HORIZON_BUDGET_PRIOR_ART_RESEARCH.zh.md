# Pulsara 长程任务预算控制调研：Codex、Claude Code 与 tool-result cap

> 状态：调研结论与下一步设计输入，不是已实施规格
> 日期：2026-07-11
> Pulsara 基线：`6a6e62672fefe3e150b6d043b7ae4196d29eb972`（工作区含未提交的 ResolvedModelCall hard-cut）
> Codex 基线：`6138909d6ec58b2fbe635ef973e02caecad5a5aa`
> Claude Code 快照基线：`5a774a2b62d7949c1d94e0b726281554d7893cfd`

> 后续生产规格：`PULSARA_LONG_HORIZON_CONTEXT_WINDOWS_HARD_CUT_IMPLEMENTATION.zh.md`。本文保留prior-art与问题分析价值；若DTO、
> identity、预算公式、commit/recovery或PR顺序与实施规格不同，以实施规格为准。

## 0. 结论先行

`LoopBudget.tool_result_context_chars = 36_000` 可以改，而且应该改。但首要改动不应是把 `36_000` 换成另一个更大的固定数，而应是删除它当前承担的错误职责：

> **tool-result 聚合预算不应是独立于模型上下文预算的致命总闸。它应是一个可降级的模型可见投影预算。**

Codex 与本地 Claude Code 快照都保留工具输出上界，但都没有采用“历史 tool result 合计刚超过一个较小字符常量，就在 model call 前终止整个 run”的主路径：

- Codex 对每个工具输出做硬上界截断，在上下文接近模型窗口时允许当前 turn 中途 compaction；必要时还会把旧 tool output 改写成极小占位符。
- Claude Code 把大结果保存到磁盘，只给模型预览和路径；随后还有单消息聚合预算、旧工具结果清理、完整 autocompaction 与 prompt-too-long reactive recovery。
- 两者都把“原始结果是否完整保存”和“模型下一次必须看到多少”分开处理。

Pulsara 当前其实已经完成了这条主线的前半段，不能把问题描述成“Pulsara 还没有截断或 artifact-backed tool result”：

- 长结果会先保留完整 artifact；
- 超过 preview 阈值时，模型只看 head/tail 两段与中间省略说明；
- terminal / terminal_process 会把 preview、truncation metadata 与 `artifact_read` locator 写进结构化结果；
- ContextCompiler 还会按 full body、artifact preview、essential envelope 继续降级。

本次故障发生在这些降级**之后**：许多已经 head/tail 截断、artifact-backed 或 essential 化的 observations，仍各自需要 tool-result header、timing、状态和 artifact envelope；这些最小表示聚合到 `36,083 chars` 后，撞上最终 `36,000 chars` hard cap。

这也是 Pulsara 应采用的方向。Pulsara 比两者更适合做这件事，因为 EventLog、artifact 与 compiled context 已经天然分层：

1. EventLog / artifact 保留完整事实；
2. ContextCompiler 只决定当前 model call 看见什么；
3. 历史 tool result 可降级为 preview、artifact locator、essential envelope 或 bounded rollup；
4. 同一个 run 可以跨多个 model-visible context window，而不需要篡改 durable ledger。

本次真实 REPL 故障还证明，仅修 36K 不够。Pulsara 需要同时区分并治理四类预算：

| 预算 | 约束对象 | 本次现场是否触发 |
|---|---|---:|
| 活跃上下文窗口预算 | 单次 model call 的最终 payload | 否，最高约 25K–33K tokens |
| tool-result 投影预算 | 当前 compiled payload 中工具结果的模型可见表示 | 是，`36,083 > 36,000 chars` |
| 累计 rollout / 工作预算 | 一个 run 跨多次调用的累计输入、输出、成本与时间 | 没有该预算；累计输入超过 110–120 万 tokens |
| step / tool-call 预算 | agentic loop 次数与工具调用次数 | 第二次运行触发 `max_turns=50` |

因此，“为什么没有 auto-compaction”与“为什么任务失败”并不矛盾：auto-compaction 观察的是**当前活跃上下文**，不是累计计费 token；第一次运行失败于独立 tool-result cap，第二次运行失败于 loop step cap。

## 1. 调研范围与证据等级

### 1.1 仓库范围

本轮只读取以下三个本地仓库：

- Pulsara：`/Users/plumliu/Desktop/python_workspace/pulsara_agent`
- Codex：`/Users/plumliu/Desktop/python_workspace/codex`
- Claude Code 快照：`/Users/plumliu/Desktop/python_workspace/claude-code`

Codex 是开源实现，可以把源码视为该 commit 的直接实现证据。

Claude Code 仓库 README 明确标注它来自 2026-03-31 的 source-map 泄漏。本文只把它称为“本地 Claude Code 快照”，不把 feature flag、内部实验路径或默认值声称为 Anthropic 当前公开产品承诺。尤其是 `feature('...')`、`USER_TYPE === 'ant'`、GrowthBook gate 下的路径，只能证明该快照存在相应设计，不能证明所有用户默认启用。

### 1.2 本文区分三种事实

- **默认或通用生产路径**：从调用链和默认值可以确认会执行。
- **可选能力**：配置、CLI 参数或 feature flag 开启后执行。
- **设计信号**：源码存在，但本地快照缺少内部模块或默认关闭；只用于理解方向。

本文不会因为没有找到代码，就断言产品绝对没有某项服务端能力。本文只陈述本地仓库能证明的客户端 / core 行为。

## 2. Pulsara 现场复盘：不是 256K 窗口溢出

### 2.1 第一次 LOL 比赛搜索 run

现场 run：`run:73fe1114b4934606855a0633d17b9e5c`。

已从 PostgreSQL durable events 核对到：

- 46 次 model call；
- 46 次 tool call，其中 terminal 相关调用 36 次；
- 累计 provider input tokens 约 `1,206,360`；
- 累计 output tokens 约 `8,320`；
- 单次 compiled context 最大估算约 `25,392` tokens；
- provider 报告的单次最大 input 约 `33,198` tokens；
- 最后在 tool-result renderer 失败：
  - body：`20,041 chars`
  - envelope：`16,042 chars`
  - total：`36,083 chars`
  - cap：`36,000 chars`

也就是说，失败时距离 256K model context 仍很远，只是比独立字符 cap 多了 83 个字符。

### 2.2 第二次新 run

现场 run：`run:b148...`。

- 50 次 model call；
- 52 次 tool call；
- 累计 input 约 `1,108,871` tokens；
- 当前上下文仍只有约 `23.7K` tokens；
- 最终触发 `LoopBudget.max_turns=50`；
- 最后一轮仍然调用工具，没有保留一次强制综合答案的 model call。

### 2.3 搜索放大

两次轨迹都不是单个巨型搜索结果直接塞爆上下文，而是低增益循环：

- 反复 Web search / scrape；
- 把搜索响应保存到本地；
- 再用 terminal 解析 JSON；
- 再 read 文件；
- 继续换关键词搜索。

第一条轨迹大致包含 9 次 search、11 次 scrape、16 次本地解析，以及多次 read/artifact；第二条轨迹同样包含大量重复 search、scrape、terminal parse 与 read。

每次 model call 都重读约 20K–30K 的活跃上下文，所以单次没有接近 256K，累计计费输入却很快超过 100 万。

### 2.4 为什么旧 auto-compaction 没触发

旧实现使用约 200K tokens 的触发阈值；当前 ResolvedModelCall hard-cut 工作区已改为从目标模型 input budget 按 `auto_trigger_ratio=0.80` 动态派生。但两种版本都关注**活跃上下文尺寸**。

本次活跃上下文只有约 25K–33K，因此不应因为累计输入达到 120 万就触发 context compaction。累计输入是 46 个相似 payload 的总和，不是一个 payload 的大小。

同时，当前 mid-turn compaction 仍在
`src/pulsara_agent/runtime/compaction/inline.py` 中使用：

```text
max_compactable_sequence = current_run_start_sequence - 1
```

这使当前 run 的工具轨迹整体不可压缩。即使未来当前 run 自己涨到阈值，也无法像 Codex / Claude Code 那样把同一 turn 早期工具观察压成继续执行所需的 handoff。

### 2.5 Pulsara 当前已经有 head/tail tool observation preview

真实执行路径是：

```text
ToolExecutor._finalize_result()
  -> ToolResultArtifactService.process_result()
  -> ToolResultTextDeltaEvent / ToolResultEndEvent
  -> message replay / ToolResultBlock
  -> ContextCompiler tool-result allocator
```

`src/pulsara_agent/runtime/tool_artifacts.py:21-28` 的当前默认值是：

- archive threshold：8,000 bytes；
- 完整 preview body 上限：32,000 chars；
- large preview：8,000 chars；
- huge output threshold：200,000 chars；
- huge preview：4,000 chars；
- head/tail 比例：约 65% / 35%。

因此更准确的语义是：

- `<= 8KB`：一般无需额外 archive；
- `> 8KB`：完整输出进入 artifact（受 capability `artifact_mode` 约束）；
- `8KB–32K chars`：可以仍把完整 body 作为 preview，同时已有 artifact anchor；
- `> 32K chars`：模型可见 body 改成约 8K 的 head/tail preview；
- `> 200K chars`：模型可见 body 改成约 4K 的 head/tail preview；
- preview 中明确写出中间省略字符数和建议的 `artifact_read.offset_chars`。

`build_adaptive_preview()` 不是只留开头。它重新计算截断提示本身的开销，再从剩余预算中按约 65% / 35% 保留 head 与 tail。terminal 的结构化 payload 还会记录：

- `preview_policy`；
- `output_original_chars / bytes`；
- `visible_head_chars / visible_tail_chars`；
- `omitted_middle_chars`；
- `preview.read_more`。

ContextCompiler 是第二级控制。`runtime/context_engine/tool_results.py` 已支持：

```text
full_visible
  -> clipped_preview / artifact_preview
  -> essential_envelope
  -> ultra-minimal terminal status envelope
```

并且 body budget 与 envelope budget已经是可借用的 soft target。真正仍会 fail run 的是：

- 所有 rendered tool results 的最终总量超过 `tool_result_context_chars=36_000`；
- tool-result 数量超过 `max_tool_results_per_context=256`；
- universal observation timing contract 损坏。

所以本次调研的目标不是重新实现 head/tail 截断，而是解决**单结果已经充分截断后，跨结果的最小 envelope 仍线性累积**的问题。

## 3. 预算问题必须拆成不同控制面

### 3.1 Active context budget

回答：下一次 model call 的最终 payload 是否装得进模型。

它应该由当前正在 hard-cut 的 `ResolvedModelCall.context_budget` 与同一个 token estimator 决定。auto-compaction 解决的是这一层。

### 3.2 Observation render budget

回答：durable tool facts 中，本次 model call 要展示哪些 body、envelope、timing、artifact locator。

它是 ContextCompiler 的投影策略，不应成为另一套与模型窗口无关的事实真源。Pulsara 当前已经有 artifact-backed head/tail preview 和多级 renderer；后续需要补的是跨 observation rollup、同 run context-window compaction，以及从 resolved model budget 派生最终上界。

### 3.3 Rollout budget

回答：即使每个 payload 都很小，一个 run 是否已经调用模型太多次、重复预填太多上下文、花费太高或持续太久。

120 万累计输入属于这一层。compaction 不会自动减少已经发生的成本，也不能识别证据没有增长。

### 3.4 Step / action budget

回答：agent 还能发起多少轮模型与工具动作。

它是安全保险丝，但不能在最后一个 step 仍允许工具调用，然后不给模型留下综合答案机会。

### 3.5 Progress budget

回答：最近若干次动作是否真的增加了新证据。

这不是 token 数可以替代的。搜索关键词变化、重复抓取同一 URL、对同一 JSON 反复 `jq`，可能消耗大量 token，却没有增加事实覆盖率。

## 4. Codex 的预算控制

### 4.1 先限制每个模型可见项，而不是限制所有 tool result 总和

Codex 根目录 `AGENTS.md:91-99` 把 model-visible context 作为代码审查契约：

- 所有注入项必须 bounded 且有 hard cap；
- 单项不能超过 10K tokens；
- 可能超过 1K tokens 的新单项要额外审查。

这是一条很重要的边界：hard cap 约束的是**单项**，不是把所有历史工具事实塞进一个 36K 字符的全局桶。

`codex-rs/core/src/context_manager/history.rs:376-398` 在任何 function/custom tool output 进入 history 时，都按当前模型的 `truncation_policy` 截断。序列化有 1.2 倍预留，但最终模型可见历史中不存在不受控的单个工具输出。

模型元数据的 fallback truncation policy 是 10,000 bytes（`codex-rs/protocol/src/openai_models.rs:687`）；具体模型可提供不同 policy。

### 4.2 terminal / exec 有两层上界

`codex-rs/core/src/unified_exec/mod.rs:70-71`：

- 默认 model-facing output request：10,000 tokens；
- raw output collection hard cap：1 MiB。

`tools/context.rs` 会把原始 token 数、遗漏字节和截断 warning 一并提供给模型。也就是说，Codex 不假装结果完整，但也不会因为一条命令输出大就终止整个 turn。

### 4.3 当前 turn 可以中途 compact

`ModelInfo.auto_compact_token_limit()` 默认取 resolved context window 的 90%，显式配置也不会高于 90%（`codex-rs/protocol/src/openai_models.rs:441-452`）。

`codex-rs/core/src/session/turn.rs:318-372` 在每次 sampling 后检查：

- 模型是否还需要 follow-up；
- 是否有 pending input；
- 活跃上下文是否达到 compact limit。

如果还需要继续且达到上限，就在**当前 turn**执行 `CompactionPhase::MidTurn`，然后 `continue` agent loop。代码注释明确表达：只要 compaction 能显著降回阈值以下，就不必因为循环长而提前结束。

这与 Pulsara 当前“本 run 全部不可 compact”是最关键的差异。

### 4.4 compaction 是显式 context window 边界，不删除 durable rollout

`codex-rs/core/src/compact.rs:55-66` 专门区分 mid-turn compaction。它会把 initial context 插回最后一个真实 user message 之前，使模型在 summary 之后继续当前任务。

`compact.rs:330-365` 构造 `replacement_history`，推进 auto-compact window，持久化 `CompactedItem`，再替换 live history。durable rollout 因此仍能重建 compaction 后的工作上下文。

这与 Pulsara 的 EventLog / CompiledContext 架构并不冲突：Pulsara 完全可以保留 raw facts，只替换当前 run 的 model-visible projection。

### 4.5 上下文仍过大时，先清空旧 tool output body

`codex-rs/core/src/compact_remote.rs:368-430` 在 remote compaction 请求前，从历史尾部向前检查整体估算；只要仍超窗口，就把 function/custom tool output 改成：

```text
Output exceeded the available model context and was truncated
```

它保留 call/output pairing 与 success 状态，却牺牲旧 body。这里的优先级很清楚：

1. 保持 prompt 合法和可继续；
2. 保持工具调用结构；
3. 完整旧 body 最后。

### 4.6 active-context usage 与累计 usage 分开

`context_manager/history.rs:328-345` 计算 active context 时，使用最近一次 provider `last_token_usage.total_tokens`，再加最近 model item 之后新增的本地项。这是下一次调用的工作集，不是累计计费。

累计 `total_token_usage` 另行维护。Codex 因而不会把 46 次 25K context 错当成一个 1150K context。

### 4.7 可选的 rollout budget

Codex 还有 feature/config gate 下的累计 rollout budget：

`codex-rs/core/src/rollout_budget.rs:31-44` 使用：

```text
weighted_tokens
= output_tokens * sampling_token_weight
+ non_cached_input_tokens * prefill_token_weight
```

它在 root thread session tree 中共享，因此 child threads 也不能绕开总预算。接近阈值时，`session/rollout_budget.rs` 会向每个 thread/window 注入剩余预算提醒；耗尽后抛 `SessionBudgetExceeded`。

这项能力是可选的，不能描述成 Codex 所有运行的默认硬上限。但它提供了 Pulsara 当前缺失的正确抽象：**累计工作预算与 context-window budget 分离**。

### 4.8 Codex 没有证明存在通用“重复搜索”检测器

本轮在 core 中没有找到按相同 query、URL 或 evidence fingerprint 阻止重复工具调用的通用实现，也没有找到主 loop 默认固定 `max_turns=50` 的同类限制。

因此不能说 Codex 必然不会反复搜索。更准确的结论是：即使模型低效地调用很多工具，Codex 的逐项上界、当前 turn compaction 与可选 rollout budget，使它不容易先死在一个很小的聚合输出字符 cap 上。

## 5. Claude Code 快照的预算控制

### 5.1 大 tool result 默认落盘，不把全文放进 context

`src/constants/toolLimits.ts` 定义：

- generic per-result persistence threshold：50,000 chars；
- 单条 API-level user message 中 tool results 聚合预算：200,000 chars；
- 极大结果的 token/byte guard：100,000 tokens / 400KB 估算。

`src/utils/toolResultStorage.ts:272-327` 超阈值后把完整结果写入 session 的 `tool-results/`，模型只收到：

- 输出原始大小；
- 文件路径；
- 前 2,000 bytes preview；
- 是否仍有更多内容。

这不是简单丢弃。模型仍可用 Read / jq / search 定向读取 artifact。

### 5.2 每消息聚合预算会替换最大的新结果，不会终止 run

`toolResultStorage.ts:740-850` 对一个 API-level user message 中并行产生的 tool results 计算聚合大小。超过默认 200K chars 时，它选择最大的**新**结果落盘并替换为 preview，直到回到预算内。

这个决定按 `tool_use_id` 持久化和重放，以保持 prompt-cache prefix 稳定。旧结果不会在后续某一轮突然从 full 变 preview，导致缓存前缀漂移。

该聚合预算由 `tengu_hawthorn_steeple` gate 控制，快照里的 fallback 是关闭；因此应把它视为已实现的可选机制，不应描述为所有外部构建默认启用。

但它的失败语义仍值得借鉴：**超过聚合预算时降级结果，不抛出 run-ending context error。**

### 5.3 工具有自己的领域上界

不同工具没有强行共享同一字符规则：

- Bash：默认模型可见 stdout 30K chars，原始大输出最多保存 64 MiB 到 tool-results，再返回 2K preview 与路径（`BashTool.tsx:424`、`:728-750`）。
- Grep：20K chars persistence threshold。
- WebSearch / WebFetch / Glob：100K chars declared threshold，但 generic 50K fallback 会约束未 override 的工具。
- Read：不走“结果再保存给 Read”的循环，而是自己限制文件大小和 25K output tokens；超限要求 offset/limit（`FileReadTool/limits.ts`）。
- MCP：默认 25K output tokens；大文本/structured content 保存到文件，二进制保存为合适扩展名，模型拿路径与读取指令（`mcpValidation.ts`、`services/mcp/client.ts:2734-2799`）。

这说明“一个 cap 管所有工具”不是理想抽象。terminal、Read、MCP、search 的可分页性、artifact 能力和结果结构不同。

### 5.4 micro-compaction 优先清旧工具结果

`src/query.ts:369-455` 的顺序是：

1. tool-result persistence / per-message budget；
2. history snip（feature-gated）；
3. microcompact；
4. context collapse（feature-gated）；
5. full autocompact。

`microCompact.ts` 可把旧 Read、Shell、Grep、Glob、WebSearch、WebFetch、Edit、Write 等 tool result 的 body 清成：

```text
[Old tool result content cleared]
```

并至少保留最近结果。快照中的 cached microcompact 依赖未包含的 internal module / feature gate；time-based microcompact 默认关闭。因此不能把所有 microcompact 路径当成外部默认行为，但分层顺序非常明确：先做廉价、结构化的 tool observation thinning，再做昂贵的 LLM summary。

`apiMicrocompact.ts` 还展示了服务端 context-editing 设计信号：约 180K input tokens 时清 tool uses/results，目标降到约 40K。该路径只对内部用户/env 开启，仍应按可选设计信号理解。

### 5.5 完整 autocompaction 发生在同一个 query loop 中

`autoCompact.ts`：

- 为 summary reserve 最多 20K output tokens；
- 在 effective context window 之外再留 13K autocompact buffer；
- 以最近 provider usage 加新消息估算当前活跃 context；
- 连续 compaction 失败 3 次后 circuit-break，避免无限重试。

`query.ts:455-540` 在每次 agentic iteration 发 provider request 前运行 autocompact；成功后把 `messagesForQuery` 替换为 post-compact messages，并继续同一个 query loop。

因此当前 user request 与当前工具轨迹可以被 summary 接管，不存在“只许压上一 run，当前 run 全部保护”的绝对边界。

### 5.6 prompt-too-long 有 reactive recovery

当 provider 已返回 prompt-too-long / 413 时，`query.ts:1080-1183` 还会：

1. 尝试 drain 已 staged 的 context collapses；
2. 尝试 reactive compact；
3. 用 post-compact messages 重试同一 query；
4. 如果仍失败，才把错误暴露给调用方。

这与 Pulsara 当前在 compiler tool-result cap 处直接 fail run 的体验差异很大。

### 5.7 交互式主 loop 默认不受固定 maxTurns 限制

`QueryParams.maxTurns` 是 optional。CLI `--max-turns` 只面向非交互 `--print`；fork subagent 的内建默认是 200。交互式主 loop 会在模型不再发工具、hook 不要求继续或用户中断时结束。

Claude Code 仍有安全边界：

- headless 可设 `--max-turns`；
- headless 可设 `--max-budget-usd`；
- API `task_budget` 可跨 compaction 延续；
- 用户可在 prompt 中明确给 token target，feature-gated token-budget 会在模型过早停止时提醒继续，并在连续低增益时停止；
- autocompact failure 和 max-output recovery 都有 circuit breaker。

本轮同样没有找到默认启用的、对所有 search/tool call 做 evidence-level no-progress 检测的通用实现。

## 6. 三者对比

| 维度 | Codex | Claude Code 快照 | Pulsara 当前 |
|---|---|---|---|
| 单结果上界 | model truncation policy；通常不超过 10K tokens | tool-specific；generic 50K chars，大结果落盘 | 完整 artifact + adaptive head/tail preview；随后还有 per-tool / per-message / envelope allocator |
| 聚合 tool-result 过大 | 截断单项；必要时清旧 output body | 最大结果落盘 / preview；可选每消息 200K | 单项会继续降级，但最小 header/envelope 跨结果累积；最终 total 超 36K 可直接 `ContextBudgetExceeded` |
| 原始结果 | exec raw collection 自身有 1 MiB 上界；history/rollout 保存 bounded 表示，不承诺 Pulsara 式完整 raw truth | tool-results 文件保留 | EventLog + artifact 保留，架构基础最好 |
| 当前 turn 压缩 | 支持 MidTurn compaction | query loop 内 autocompact / reactive compact | current run 全保护，不能压 |
| 旧工具结果清理 | compaction 前改写为极小 placeholder | microcompact / API context editing | allocator 可降级，但 essential envelope 聚合仍可致命 |
| 活跃窗口阈值 | 默认 resolved window 的 90% | effective window - summary reserve - 13K | 当前工作区按 target input budget × 0.80 |
| 累计 rollout budget | 可选 weighted budget + reminders | 可选 task/token/USD budget | 没有累计输入/成本/时间预算 |
| 默认主 loop step cap | 未找到固定 50 轮 | interactive 无默认；headless optional | `max_turns=50`，`max_tool_calls=64` |
| 最终回答保留 | loop 由 follow-up 状态继续 | maxTurns 有明确错误，但无固定 interactive cap | 最后 step 可继续调用工具，未保留 synthesis call |
| no-progress guard | 未找到通用 evidence guard | 未找到默认通用 guard | 未实现 |

## 7. Codex 为什么能完成更长程的编码任务

不是因为 Codex 没有预算，而是预算层次不同：

1. 每个工具输出先被 bounded，单次错误输出不会无限放大后续每个请求。
2. 当前 turn 自己可以跨 context windows；达到阈值就 compact，然后继续。
3. compaction 不要求结束用户任务，也不要求新建 user run。
4. 主 loop 没有 Pulsara 式默认 50 model calls 硬截止。
5. 可选 rollout budget 用累计非缓存 input / output 控制总工作量，但与 context compaction 分开。
6. terminal / process 工具直接提供 bounded 增量输出，通常不需要“生成大 JSON → 保存 → terminal parse → read”的长链。
7. UI 持续展示工具进展，用户更容易在低增益循环中中断。

这并不表示 Codex 不会低效或不会循环。它只是把“任务很长”与“上下文失控”解耦得更好。

## 8. 对 36,000 chars 的判断

### 8.1 36K 作为 soft target 并非完全不合理

36K chars 粗略约 9K tokens。限制工具内容占用可以：

- 给 system、current user、代码上下文、memory 与输出预留空间；
- 避免大量 terminal/search 噪音挤掉任务目标；
- 促使 artifact-backed 读取。

而且 Pulsara 已经不是把 raw output 直接堆到 36K：大结果先被 archive，并变成 head/tail preview；allocator 还会进一步选择 artifact preview 或 essential envelope。36K 衡量的是**第二级渲染后的所有 tool observations 总和**。

问题不是数字一定太小，也不是单结果没有截断，而是它同时被当成：

- 所有工具结果的总量上界；
- body + metadata envelope 的共同上界；
- 与 resolved model input budget 无关的固定字符上界；
- 超过后 run-ending 的 hard error。

本次只多 83 chars 就终止 run，说明这个失败语义不适合生产。

### 8.2 单纯提高到 100K / 200K 只能缓解，不能解决

提高常量会让这条轨迹多跑几轮，但不会解决：

- 当前 run 不可 compact；
- 46 次调用累计 input 超过 120 万；
- 搜索证据低增益；
- `max_turns` 前没有 finalization reserve；
- envelope 数量会随工具调用继续线性增长；
- 更小模型与更大模型仍共享同一字符常量。

所以可以做临时上调，但不能把它当成最终设计。

### 8.3 推荐的 hard-cut 语义

生产真源改为 token-based、call-relative：

```text
input_budget_tokens
= ResolvedModelCall.context_budget.input_budget_tokens

non_tool_tokens
= estimate(system + tools + current user + memory + subagent handoff + non-tool transcript)

tool_result_available_tokens
= max(0, input_budget_tokens - non_tool_tokens - compile_safety_margin_tokens)
```

再定义：

- `tool_result_soft_budget_tokens`：触发降级的目标，可按模型 input budget 比例并设置上下界；
- `tool_result_hard_available_tokens`：当前 call 在其他 required context 之后真正剩余的空间；
- chars 只用于 artifact / I/O / preview 的尺寸保护，不再作为最终 model-context truth。

任何 tool-result 投影超过 soft budget 时，必须继续降级，而不是直接失败：

1. older full body → bounded preview；
2. preview → artifact locator + essential status/timing；
3. 多个 completed old envelopes → bounded tool-observation rollup；
4. 当前 context window 的旧工具轨迹 → in-run compaction summary；
5. latest result 保留优先级最高；
6. unresolved call、pending approval、active terminal process 与 current user 不能被误删。

只有以下情况才应在 model call 前 fail closed：

- required non-tool context 本身已经超过 input budget；
- tool call/result pairing 或 timing contract 损坏；
- 最新且不可 artifact 化的 required result 本身无法放入模型窗口；
- 降级与 compaction 全部失败。

“历史 completed tool envelopes 合计超过固定 36K”不应属于 run-ending 条件。

## 9. Pulsara 应学习的目标架构

### 9.1 三层 tool observation

```text
Durable observation
  EventLog + raw artifact + typed timing + provenance

Model-visible observation
  full / preview / locator / essential / rollup

Display observation
  Inspector / REPL 可按需读取 durable truth
```

ContextCompiler 的降级不修改 durable observation。Inspector 仍能解释模型当时看见的是哪个 render policy。

这三层并非从零开始：当前 `ToolResultArtifactService`、`ToolResultArtifactRef.preview`、universal `ToolObservationTiming` 和 `tool_result_render_decisions` 已经形成主要 substrate。下一步应删除剩余的聚合 terminal condition，而不是推翻现有 artifact/head-tail 路径。

### 9.2 同一 run 允许多个 context window

当前 `current_run_start_sequence - 1` 应被替换为 context-window 语义，而不是 run 语义：

```text
run
  context window 1
    user request
    tool/model trajectory
    ContextWindowCompactedEvent
  context window 2
    bounded handoff summary
    retained unresolved/latest tail
    continued trajectory
```

建议的 safe-point 条件：

- 当前 tool batch 已完整持久化且 pairing 完成；
- 没有尚未归属的 provider delta；
- pending approval / MCP input-required 有 durable suspend fact；
- active terminal process 可保留 process locator，不必保留全部 log；
- explicit subagent result / consumption 状态已提交。

同 run compaction 只改下一次 compiled context，不改变 run status，不伪造新 user turn。

### 9.3 先做 deterministic observation thinning，再调用 compact LLM

顺序建议参考 Claude Code：

1. per-result artifact/preview；
2. dedupe identical/replayed observations；
3. old completed result body → locator；
4. bounded rollup；
5. 仍接近 active context threshold 才做 LLM compaction；
6. provider prompt-too-long 时允许一次 reactive compact/recompile。

LLM compaction 不应该承担“把 16K envelope 删到 15.9K”这种机械工作。

### 9.4 增加累计 rollout budget

建议借鉴 Codex 的 weighted budget，但使用 Pulsara typed facts：

```text
weighted_work
= non_cached_input_tokens * prefill_weight
+ output_tokens * sampling_weight
+ tool_cost_units
+ optional_wall_clock_weight
```

需要区分：

- run budget；
- HostSession budget；
- root subagent graph shared budget。

接近阈值时，不应直接终止。先写 durable reminder / diagnostic，并向模型注入：

- 已使用预算；
- 剩余预算；
- 当前phase与允许的action classes；
- bounded exact recurrence（若存在）。

该提示只陈述runtime已经观察到的事实，不要求模型停止、继续、finalize或选择下一步。真正的探索收窄由确定性的rollout phase与
capability gate执行，不能把自然语言hint当控制面。

预算耗尽后，默认动作应是进入 finalization，而不是继续允许工具调用。

### 9.5 给 step cap 留 finalization reserve

建议把 loop budget 拆成：

```text
exploration_model_calls
finalization_model_calls_reserved
tool_calls
```

例如在达到 exploration soft limit 或只剩 1–2 次 model call 时：

- capability exposure 中暂时 gate deny search/scrape/terminal 等扩散工具；
- 注入只包含当前phase、剩余reserve与allowed action classes的中性runtime fact；
- 允许至少一次无工具的最终综合调用；
- 如果模型仍请求工具，返回稳定 denial，并继续保留 finalization call，而不是消耗掉最后机会。

硬 `max_turns` 仍可作为异常保险丝，但不应是正常长程工作完成协议。

### 9.6 被否决的研究分叉：通用 evidence progress guard

Codex 与 Claude Code 的通用预算机制并不能证明事实增益。早期调研曾考虑让Pulsara利用typed events实现：

- canonicalize query；
- URL / document content fingerprint；
- 同域重复抓取计数；
- 新增 source 数；
- 新增可引用事实数；
- 最近 N 次工具调用的 evidence delta。

若连续若干次搜索没有新 URL / 新事实，早期方案拟执行：

1. 先向模型给 no-progress diagnostic；
2. 要求改变策略或直接回答；
3. 再重复则 gate deny同类 search；
4. 进入 finalization reserve。

后续与Codex、Claude Code、MiMo-Code、DeepSeek-Reasonix对照后，本阶段明确不采纳该方案。原因是“新证据”“低增益”和
“改变结论”需要产品域语义，通用runtime难以稳定判断；它还会把长程探索误收敛为搜索任务，并引入novelty ontology、builder
registry、progress reducer与阈值联动。

阶段四只保留更窄的机制：从typed tool name/arguments/terminal outcome派生bounded exact recurrence，并在中性status hint中陈述
次数。它不形成progress状态、不改变phase、不deny调用，也不告诉模型下一步该做什么。若未来建设AutoResearch/Web Research，
应在独立产品层重新设计证据质量策略。

### 9.7 为 search / scrape 提供直接 bounded 查询结果

本次轨迹的 token 放大来自工具链组合：search JSON → shell parse → read artifact → 再 search。

Pulsara 应优先让 search/scrape adapter 直接返回：

- bounded structured result；
- stable source IDs / URLs；
- title/snippet/published_at；
- artifact locator；
- pagination cursor；
- dedupe fingerprint。

模型需要全文时再定向 hydrate，而不是默认通过 terminal 和 read 重建结构化结果。

## 10. 建议的实施顺序

本调研不修改 runtime。建议在 ResolvedModelCall hard-cut 完成后单独开一个“Long-Horizon Context Windows”大章。

### PR0：预算 vocabulary 与事件

冻结：

- active context budget；
- observation soft/hard available budget；
- rollout budget；
- exploration/finalization budget；
- context window identity；
- degradation / rollup / compaction reason codes。

所有 token budget 都引用同一个 `ResolvedModelCall` estimator，不再创建 chars/token 双真源。

### PR1：在现有 progressive degradation 之后增加跨结果收口

- 保留现有完整 artifact、adaptive head/tail preview 和 per-result I/O hard caps；
- 保留现有 full → artifact preview → essential envelope 降级状态机；
- 36K 改为兼容 soft target，随后删除固定生产真源；
- 单项已到 essential、但 total 仍超 soft target时，合并 old completed envelopes 为 bounded rollup；
- latest / unresolved / pending facts受保护；
- render decision durable/inspectable；
- 不因多 83 chars 终止 run。

### PR2：收口现有 artifact-backed universal tool observations

- 核对 terminal、MCP、search、Read 是否都完整进入现有 raw artifact / preview / locator contract；
- 不重做已经存在的 head/tail preview；只修 descriptor policy、异常路径和 replay 漂移；
- 工具可声明 pagination / hydration 能力；
- ContextCompiler 不解析外部业务 JSON 猜可压缩性；
- full raw truth 与 model-visible preview 明确分离。

### PR3：current-run deterministic micro-compaction

- 删除“当前 run 一律不可压”的绝对规则；
- 先只压 completed old tool bodies；
- 保留 pairing、result status、timing、artifact IDs；
- 写 context-window projection event，不修改原始 tool events。

### PR4：current-run LLM compaction window

- safe point 生成 bounded continuation handoff；
- 同 run 开新 context window；
- next compile 使用 summary + retained tail；
- current user request、pending state、active processes 与 latest evidence 受保护；
- provider prompt-too-long 可进行一次 reactive compact/recompile。

### PR5：rollout budget 与 finalization reserve

- usage facts累计 non-cached input / output；
- optional cost / wall-clock；
- root subagent graph共享上界；
- soft reminder；
- exploration gate；
- reserved final answer call；
- 对所有production primary target/summarizer pair运行静态可行性矩阵；
- 配置加载与现有`pulsara config-check`报告total rollout、reserve components、exploration allowance与不可行组合；
- 中性status hint只报告phase、settled calls、remaining allowance与bounded exact recurrence；
- hard exhaustion 有明确 terminal reason，而不是模糊 `max_turns`。

### 已取消的后续分叉：通用 evidence progress guard

通用evidence progress guard已从阶段四实施范围删除。阶段四在PR5结束；不存在后续progress PR，不保留隐式future flag或兼容schema。

## 11. 验收与 dogfood

### 11.1 36K 回归

构造与现场相同的：

```text
body = 20,041 chars
envelope = 16,042 chars
soft target = 36,000 chars compatibility value
```

验收：

- compile 不抛 `ContextBudgetExceeded`；
- old body/envelope 被进一步降级；
- latest result 仍可见；
- pairing、timing、artifact locator 可见；
- final payload 在 resolved input budget 内。

### 11.2 当前 run 跨 window

一个 user run 连续产生足够多的 terminal/search observations：

- 触发 deterministic thinning；
- 触发一次 LLM compaction；
- 同一 run_id 继续；
- context_window_id 改变；
- EventLog raw tool events数量和内容不变；
- Inspector 能重建每次 model call 看见的 projection。

### 11.3 累计输入大、活跃上下文小

模拟 50 次每次 25K input 的调用：

- active context meter始终约 25K；
- cumulative rollout meter达到约 1.25M；
- auto-compaction不会因为累计值误触发；
- rollout reminder / finalization 会触发。

### 11.4 中性 exact recurrence 观察

连续返回相同 URL 与相同 content fingerprint：

- Inspector能展示bounded recent window中的exact normalized action recurrence；
- hydration/control调用夹在两次search之间不破坏recurrence统计；
- 模型可见hint只报告phase、调用次数、remaining allowance与recurrence次数；
- recurrence本身不改变phase、不拒绝search、不替runtime判断“是否有新证据”；
- 最终综合调用由rollout finalization reserve保证。

### 11.5 真实 REPL dogfood

复现“今天 LOL 比赛结果”：

- 允许真实 web/MCP/terminal；
- 记录每次 query、URL、artifact、current-context tokens 与 cumulative tokens；
- 目标不是禁止长搜索，而是在证据覆盖足够或无增益时停止扩散；
- 无论成功还是预算耗尽，都必须给用户最终可读结论，不能静默卡住。

## 12. 不应照搬的部分

### 12.1 不照搬 Claude Code 的磁盘文件作为唯一 truth

Pulsara 已有 EventLog 与 artifact archive，应继续使用 typed provenance。文件路径只是 locator，不是事实源。

### 12.2 不照搬服务端私有 context editing 假设

Pulsara 要支持 OpenAI-compatible、DeepSeek、中转站与不同 provider。核心正确性不能依赖 Anthropic 特有 cache-editing API。

### 12.3 不删除 durable raw history

Codex/Claude Code 的 live message rewrite 适合它们的存储模型。Pulsara 应只重写 compiled projection；EventLog 与 raw artifact 保持 append-only。

### 12.4 不以超大 context window 掩盖低增益 loop

把 256K 改成更大，或把 36K 改成 200K，只会延后失败。一个 25K context 被发送 46 次，仍会产生 120 万累计输入。

### 12.5 不把所有限制都做成 fatal hard cap

正确的生产顺序应是：

```text
observe -> warn -> degrade -> compact -> restrict exploration -> finalize -> hard stop
```

而不是：

```text
observe -> exceed 83 chars -> fail run
```

## 13. 最终建议

对 `tool-result cap: 36,000 chars` 的最终裁决：

1. **短期**：可以上调，但更重要的是立即把 aggregate total 从 run-ending hard cap 改为可继续降级的 soft target。
2. **中期**：保留现有完整 artifact + head/tail preview，从固定 chars hard truth 迁移到 `ResolvedModelCall` 派生的 token budget；保留 tool-specific I/O/persistence caps。
3. **长程主线**：让当前 run 跨多个 model-visible context window，先 microcompact old tool observations，再做 LLM summary。
4. **独立治理**：增加累计 rollout budget、配置期reserve可行性校验与 finalization reserve；运行期status hint只陈述事实，
   不把通用no-progress判断做成runtime控制面。不要指望auto-compaction解决累计成本和搜索循环。

Pulsara README 中的“长程任务”不应被定义为“允许无限轮工具调用”，而应定义为：

> **一个用户 run 可以在完整 durable provenance 之上跨多个 bounded context windows 持续推进；系统会压缩模型可见工作集、保留可检查事实、监控累计工作量，并在预算收窄时优先产出结论，而不是静默卡死。**

这比单纯把上下文窗口或字符 cap 调大，更符合 Pulsara 已经建立的 EventLog / typed events / Inspector / ContextCompiler 主线。
