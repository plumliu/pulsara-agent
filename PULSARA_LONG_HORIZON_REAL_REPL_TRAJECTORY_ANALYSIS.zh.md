# Pulsara Long-Horizon Real REPL Trajectory Analysis

> 状态：真实 REPL 轨迹问题记录与阶段四设计输入
> 记录日期：2026-07-12
> 关联路线：`PULSARA_NEXT_FIVE_HARD_CUT_STAGES_PLAN.zh.md`
> 关联研究：`PULSARA_LONG_HORIZON_BUDGET_PRIOR_ART_RESEARCH.zh.md`

## 1. 结论摘要

本次真实 REPL 轨迹验证了一个明确的生产正确性问题：Pulsara 在 resolved model input budget
仍有大量余量时，被独立固定的 `tool_result_total_context_chars=36_000` 提前终止了长程 run。

该 run 并未触发 provider context overflow。失败前最后一次成功模型调用的实际 input 约为
`26,400 tokens`，而同一 resolved model call contract 的 input budget 为 `239,616 tokens`。
最终失败由 tool-result renderer 的固定字符 hard cap 触发：

```text
tool_result_total_budget_unsatisfied
rendered_total_chars = 36,888
tool_result_total_context_chars = 36,000
```

用户随后发送一句 `hello？`，Pulsara 创建了一个新 run。旧 run 的 tool observations 从
`current_run_tail` 变为 `prior_history`，因而被更激进地降级。新 run 只用两次模型调用便写入文件并
给出最终答复。这不是旧 run 被恢复，而是一次由新用户消息偶然触发的跨 run context rollover。

因此，本轨迹应作为阶段四 **Long-Horizon Context Windows** 的核心 real dogfood。正确目标不是简单
提高 36K 常量，而是让同一 user run 能在 bounded model-visible windows 之间持续推进，并在预算压力
出现前完成 evidence rollup、current-run thinning、progress restriction 和 finalization。

## 2. 用户可见轨迹

用户在 REPL 中要求 agent 查询 LangChain Docs MCP，并在 workspace 根目录生成入门文档：

```text
pulsara> 你能查一下这个langchain docs文档，然后在本项目根目录中落一份用于入门langchain的md文档吗
```

该轮没有向用户返回完成结果。Agent 持续使用 MCP 与 `artifact_read` 收集资料，随后 run 失败并静默
回到 REPL prompt。用户再输入：

```text
pulsara> hello？
```

新 run 随后调用 `write_file`，生成：

```text
/Users/plumliu/Desktop/little_snake/langchain-getting-started.md
```

最终文件大小约为 `8,014 bytes`。

## 3. Durable event 证据

### 3.1 失败的任务 run

```text
runtime_session_id = runtime:4791d950c5004feea4813bc8e492f1ef
run_id             = run:f2143fdede654fd8bb02de79f5a9dbe1
status             = failed
stop_reason        = model_error
started_at         = 2026-07-12 18:21:46 +08:00
completed_at       = 2026-07-12 18:24:44 +08:00
```

该 run 的模型使用情况：

```text
reported model calls = 21
missing usage calls  = 1
cumulative input     = 435,778 tokens
cumulative output    = 2,598 tokens
cumulative total     = 438,376 tokens
```

这里的 `435,778 cumulative input tokens` 是多次 model call 的累计值，不是单次 provider payload。
单次实际 input 从约 `14,490 tokens` 逐步增长到约 `26,400 tokens`。

Resolved model context contract：

```text
model_id                  = deepseek-v4-pro
total_context_tokens      = 256,000
effective_output_tokens   = 8,192
input_safety_margin       = 8,192
effective input budget    = 239,616 tokens
```

因此，失败时并未接近真实 provider input budget。

### 3.2 最终压力事件

```text
sequence 3227  ContextCompiledEvent(status="pressure")
sequence 3228  ContextCompiledEvent(status="failed")
sequence 3229  RunErrorEvent
sequence 3230  RunEndEvent(status="failed", stop_reason="model_error")
```

核心 diagnostic：

```text
code                   = tool_result_total_budget_unsatisfied
rendered_total_chars   = 36,888
hard cap               = 36,000
rendered_body_chars    = 21,274
rendered_envelope_chars= 15,614
```

同时存在：

```text
essential_envelope_budget_unsatisfied
envelope soft target = 12,000 chars
envelope overage     = 3,614 chars
```

最终 compile 需要处理 24 个 tool-result render units。压力主要来自大量历史 execution envelopes 与
current-run observations 的累积，而不是某一条最新结果不可归档。

### 3.3 失败前的趋势

在最终 hard failure 前，compiler 已多次报告预算压力：

```text
约第 17 次 compile：essential_envelope_budget_unsatisfied
后续 compile：       essential_envelope_budget_unsatisfied 持续存在
接近失败时：         latest_reserved_budget_unsatisfied
最终：               tool_result_total_budget_unsatisfied
```

Runtime 能观测到压力逐步增长，但目前没有把这些信号转换成同 run 内的 rollup、micro-compaction、
探索限制或 finalization transition。

## 4. Agent 的行为轨迹

失败 run 共进行了约 21 次工具探索，包括：

- 对 MCP docs filesystem 执行 `tree / -L 2`；
- 读取 tree 输出 artifact；
- 多次执行 `ls`、`cat`、`head`、`rg`、`grep`、`sed`；
- 多次调用 LangChain docs search；
- 多次使用 `artifact_read` 读取较大的 archived tool output；
- 在已掌握安装、overview、quickstart 等信息后继续搜索相关主题。

模型曾多次表达“再收集一点信息，然后写文档”，但直到 run 被终止仍未调用 `write_file`。

这说明轨迹同时包含两类问题：

1. **Runtime premature hard stop**：固定 36K tool-result cap 与 resolved model budget 脱节。
2. **Low-value exploration drift**：缺少 evidence progress guard、rollout phase 与 finalization reserve。

不应将问题单独归因为模型能力。模型确实探索过度，但 Runtime 既没有提供逐步收窄机制，又在真实
context window 尚有充足空间时强制终止了任务。

## 5. `hello？` 为什么能继续任务

第二个 run：

```text
run_id       = run:869762a693dc468f84851f6cea1e1f62
status       = finished
model calls  = 2
input tokens = 49,422 cumulative
```

模型在第一轮 thinking 中明确识别出旧任务尚未完成，并决定继续写文件。第一次 model call 调用
`write_file`，第二次 model call 返回最终说明。

关键预算变化：

```text
旧 run 失败前：
  tool-result total       = 36,888 chars
  current-run-tail body   = 15,165 chars

新 run 第一次 compile：
  tool-result total       = 23,523 chars
  prior-history body cap  = 8,000 chars
```

旧 run 的 observations 在新 run 中不再属于 `current_run_tail`，而是被归入 `prior_history`。现有 renderer
对 prior history 使用更紧的 body budget，因此同一批 evidence 被压缩到可发送范围。

这相当于一次偶然发生的 deterministic thinning，但它依赖用户创建新 run。正确实现应允许 Runtime 在
同一个 user run 内做等价的 projection rewrite，而不是要求用户用无关输入人工续命。

## 6. 与阶段三的关系

本问题不应跳过 **Context Compiler Input Hard Cut** 直接修补。

Long-Horizon 需要稳定识别：

- current user；
- prior transcript；
- current-run tail；
- provider-native tool-call/tool-result pairing；
- completed、pending、latest 与 actionable observations；
- artifact locator、timing、result state 与 render profile；
- projection rewrite 前后的 durable identity。

因此阶段三必须先提供：

- immutable `ContextFactSnapshot`；
- normalized `TranscriptCompileInput`；
- normalized `ToolResultRenderUnit`；
- typed `ContextSectionCandidate` ingress；
- 不读取 mutable `LoopState` 的 compiler API；
- live/replay 等价的 compile input fingerprint。

否则阶段四只能继续修改预渲染字符串，无法证明 pairing safety、rewrite determinism 或 replay equality。

## 7. 与阶段四各工作项的映射

### 7.1 L1：Dynamic soft projection target

`tool_result_total_context_chars=36_000` 不应继续作为与模型无关的 run-ending truth。

新的 soft projection target 应由以下事实派生：

- `ResolvedModelContextBudgetFact.input_budget_tokens`；
- required non-tool context 的实测 token cost；
- current user 与 protected current tail；
- finalization reserve；
- per-observation safety caps。

超过 soft target 应触发 degrade/rollup/rewrite，而不是直接 fail。最终 provider input budget 仍是不可突破的
hard cap。

### 7.2 L2：Cross-tool-result rollup

重复的目录探索、文档读取与搜索结果应被合并成 bounded evidence rollup，例如：

- 已确认的 docs 结构；
- 已读取的关键页面；
- 安装与 quickstart 的关键事实；
- 对应 artifact IDs 与 source event range；
- 尚未完成的下一步动作。

Raw events 与 artifacts 保持不变，只有 model-visible projection 被重写。

### 7.3 L3：Current-run deterministic micro-compaction

已完成、非 pending、非 latest、非 actionable 的旧 tool bodies 应在同一 run 内降级为：

```text
full -> preview -> essential envelope -> artifact locator -> rollup
```

该过程不得破坏 tool-call/tool-result pairing，也不得移除 current user、latest error evidence 或正在执行的
terminal/MCP interaction。

### 7.4 L4：Pairing-safe current-run LLM compaction

若 deterministic thinning 后仍无法满足目标，Runtime 应关闭当前 `ContextWindowFact`，生成覆盖明确
sequence/window 的 summary，并在同一 run 内打开下一 window。

本轨迹很可能在 L1-L3 后即可完成，但仍应作为 L4 的 fallback 验收输入。

### 7.5 L5：Rollout budget and finalization reserve

Runtime 应在探索接近预算边界前保留至少一次完整 synthesis/write/final-answer call。进入
`finalization_only` 后应禁止新的低价值搜索，但允许：

- 读取已经获得的 artifact；
- 写入目标文件；
- 验证文件存在与关键内容；
- 返回 bounded final answer。

### 7.6 L6：Evidence progress guard

需要区分：

- tool call 是否不同；
- query 是否不同；
- 返回 evidence 是否真正新增；
- 新 evidence 是否改变任务结论或下一步。

连续多次调用只产生重复或低增益 evidence 时，应依次进入 warning、restricted exploration 和
finalization，而不是继续消耗 tool/model rounds。

## 8. 阶段四核心 dogfood 规格

建议将本轨迹冻结为 Long-Horizon 的 required real dogfood：

> 在 resolved model input budget 仍有充足空间时，连续 20 次以上 MCP 文档读取不得因固定 36K
> tool-result cap 终止。Runtime 必须在同一个 user run 内完成 projection thinning、evidence progress
> restriction、final synthesis 和文件写入，不需要用户额外发送 `hello？`。

最低验收条件：

1. 不再出现由固定 `36_000` aggregate cap 单独触发的 run failure。
2. 每次 projection rewrite 都有 typed event、generation、source event range 与 reason code。
3. Raw MCP results、artifact 与 EventLog 不被删除。
4. Tool-call/tool-result pairing 在所有 window 中保持合法。
5. Latest/actionable result 与 artifact locator 不得被 rollup 丢失。
6. 至少保留一次完整 finalization model call 的预算。
7. 重复 evidence 达到阈值后停止新的低价值 docs search。
8. Agent 在原 run 内调用 `write_file` 并产生 final text。
9. Inspector 能解释每条 observation 当前为何是 full/preview/locator/rollup/cleared。
10. Live 与 replay 对相同 projection generation 生成相同 provider-neutral payload。

## 9. 不应采用的局部修复

以下改动不能作为最终方案：

- 仅把 `36_000` 提高到另一个固定常量；
- 只提高 `max_turns` 或 `max_tool_calls`；
- 遇到 pressure 后静默删除最旧 tool result；
- 破坏 assistant tool-call 与 tool-result pairing；
- 把 artifact locator 或 terminal/MCP actionable state一并丢弃；
- 仅在 system prompt 中要求模型“少调用工具”；
- 依赖用户发送新消息把 current-run tail 变成 prior history；
- 把累计 input tokens 误当成单次 context window 占用。

## 10. 独立的 continuity/UX 问题

新 run 将 `hello？` 推断为继续旧任务，并未明确告知用户“上一 run 已失败，现在正在接续”。本次结果符合用户
隐含意图，但该行为具有歧义：新的 current user input 可能本来是独立请求。

理想情况下，Long-Horizon 会让原 run 在失败前完成，因此不会依赖该恢复行为。即使如此，failed-run continuity
仍应单独定义：

- 新 run 是否自动接续 failed run；
- 接续时是否需要显式 recovery section；
- 是否应先向用户说明旧任务状态；
- 新 current user input 与旧 unfinished intent 冲突时谁优先。

该问题不应混入 Long-Horizon 的核心 projection/window 实现，但应保留为后续 recovery policy 输入。

## 11. 最终判断

本轨迹不是偶发 provider 错误，也不是 MCP startup latency 问题。它证明：

1. ResolvedModelCall 已正确给出了真实预算尺子；
2. tool-result aggregate policy 尚未消费这把尺子；
3. current-run observations 缺少可持久化、可 replay 的滚动降级机制；
4. Runtime 缺少从探索转向 finalization 的显式阶段控制；
5. 新 run 能成功，反向证明同一批 evidence 经过更紧 projection 后足以完成任务。

因此实施顺序保持为：

```text
Context Compiler Input Hard Cut
  -> Long-Horizon Context Windows
  -> ContextSource Ownership Hard Cut
  -> Prompt Cache
```

阶段四实施文档应将本文件作为真实问题证据和 required dogfood 输入。
