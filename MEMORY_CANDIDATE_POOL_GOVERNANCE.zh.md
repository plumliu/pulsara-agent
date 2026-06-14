# Pulsara 记忆候选池与后台治理设计

本文档沉淀 Pulsara 记忆写入路径的下一层目标形态：主 agent 不承担长期记忆治理职责，Flash 也不应在每个 run 里急于写入 canonical memory。更稳的系统应把“候选收集”和“候选治理”分开。

核心原则：

```text
当前轮听话靠上下文。
跨轮记忆靠 durable candidate pool + governance。
```

用户在当前 run 里说“记住这个”时，主 agent 的上下文已经包含这句话。即使长期持久化尚未完成，本轮 agent 也不应该表现得像没听见。长期记忆写入可以延迟到后台治理，只要候选不会丢、治理可审计、最终写入仍经过 gate。

## 1. 总体分层

Pulsara 的记忆写入应分成四层：

```text
主 agent memory fast path
  -> 提出候选

cheap hint path
  -> 发现可能的记忆信号

durable candidate pool
  -> 保存候选、失败 attempts、cheap hints、证据引用和来源

Memory Governance Agent
  -> 后台治理候选池，输出 skip / correct / propose / merge 决策
  -> MemoryWriteService / Gate / Ledger
```

其中，主 agent 和 cheap hint 都只是产生候选或信号，不是最终写入裁判。真正进入 `mem:*` 长期语义记忆的对象必须经过治理和 `MemoryWriteGate`。

## 2. 可迁移经验

这套设计吸收四类成熟经验，但不照搬任何一个系统。

MiMo-Code 的启发：

- raw trajectory 是 source of truth。
- 主模型不需要每轮写记忆。
- checkpoint / dream / distill 可以在旁路整理长期知识。
- 周期性整理适合做跨 session 去重、提升、压缩和技能化。

OpenClaw 的启发：

- session / reset / compaction 边界是天然 safe point。
- dreaming 应默认可控，不应隐式泛滥。
- 后台记忆维护不应阻塞主对话。

Hermes 的启发：

- 主模型 memory tool 和 background review fork 可以并存。
- background review 不应污染主会话状态。
- provider / adapter 可以提供同步点，但不能拥有主循环语义。

Claude Code 的启发：

- token 增量、工具调用数和自然停顿适合做触发信号。
- 记忆整理需要 forked / side model，不应扰动主 loop。
- 用户显式 remember 是单独入口，但不等于最终治理已经完成。

Pulsara 的转译：

```text
EventLog / ArtifactStore 保存 runtime truth 和证据原文。
GraphStore 只保存经过治理的 semantic memory。
主 agent 提出候选。
Flash / Governance Agent 做旁路治理。
所有 canonical memory 写入统一经过 MemoryWriteService / Gate / Ledger。
```

## 3. 主 Agent Memory Tool Fast Path

主 agent 仍然可以使用类型专用记忆工具：

- `remember_claim`
- `remember_preference`
- `remember_observation`
- `remember_action_boundary`
- `remember_decision`

但这些工具的语义应逐步从“直接写入长期记忆”收束为：

```text
主 agent 认为这里有记忆价值
  -> 构造 typed MemoryCandidate
  -> 写入 durable candidate pool
  -> 产生可审计事件
```

主 agent 负责提出候选，不负责最终治理。它可以在用户明确要求“记住这个”时快速提交候选，但不需要承担：

- 与已有记忆去重。
- 合并相似候选。
- 修正失败候选。
- 判断候选生命周期。
- 处理 projection echo。
- 选择是否 supersede / contradict 旧记忆。

这些职责属于后台 memory governance。

### 3.1 主 Agent 工具失败

主 agent 调用 `remember_*` 失败时，不应让用户任务失败。失败应当：

- 对用户层面尽量静默，不打断当前任务。
- 对审计层面不静默，记录 tool result、失败原因、原始参数。
- 进入候选池，供后续 governance agent 判断是否修正。

例如 `remember_action_boundary` 缺少 `do_not_apply_when` 时：

```text
工具返回结构化 ERROR
候选池记录 failed attempt
后台治理读取用户原话和失败原因
必要时生成 corrected ActionBoundary candidate
```

## 4. Cheap Hint Path

cheap hint 是极低成本的字符串级检测。它可以在每个 run 结束时检查用户输入，捕捉强记忆词，例如：

- “记住”
- “以后”
- “总是”
- “不要”
- “偏好”
- “决定”
- `remember`
- `from now on`
- `prefer`
- `always`
- `never`

cheap hint 的语义必须非常克制：

```text
cheap hint 是唤醒信号，不是写入判断。
```

推荐规则：

```text
每个 run 结束时检查用户输入。
若命中强记忆词，且本轮没有主 agent memory attempt，才唤醒 Flash reflection。
Flash 必须批判性判断，允许 should_reflect=false。
```

如果本轮主 agent 已经调用过 `remember_*`，cheap hint 不应再额外唤醒 Flash。原因是主 agent 已经对用户当前表达做过记忆判断，Flash 立即介入容易过度修补、扩写或重复写入。

因此优先级是：

```text
main memory attempt > cheap hint
```

cheap hint 的主要价值是兜底：

```text
用户明确说“记住/以后/不要……”
但主 agent 没有调用 remember_*
  -> Flash 可以在 run end safe point 看一眼
  -> 如果只是误报，返回 should_reflect=false
```

## 5. Durable Candidate Pool

候选池必须 durable。不能只存在于 hook 内存态，否则用户说完就走、进程重启、断电或长时间无新 run 时，候选会丢失。

候选池应至少保存：

- candidate id
- candidate kind
- typed candidate payload
- origin: `main_agent_tool` / `cheap_hint` / `reflection` / `governance`
- source run / turn / reply
- source tool call id
- source event ids
- user quote / tool evidence refs
- write attempt status
- failure reason
- created_at / updated_at
- governance status: `pending` / `accepted` / `skipped` / `corrected` / `merged` / `failed` / `needs_review`

候选池不是长期语义记忆。它是治理前的工作队列和审计材料。

## 6. Governance 输入包

无论是本轮轻量 reflection，还是后台 candidate pool governance，都不能只看最终 assistant 文本。它必须看到候选产生与失败的完整上下文。

推荐输入包包含：

```text
MemoryGovernanceInput
  runtime_session_id
  run_ids
  candidate_pool_snapshot
  user_message_summaries
  assistant_reply_summaries
  tool_traces
  runtime_events
  memory_tool_attempts
  memory_write_results
  memory_write_failures
  existing_memory_hits
  memory_projection_ids
  available_evidence_ids
  prior_reflection_events
  prior_governance_events
  trigger_reasons
```

其中 `memory_tool_attempts` 必须保留：

```text
tool_call_id
tool_name
candidate payload if valid
raw arguments if invalid
result status
candidate_id
memory_id
gate_reason
error_message
source event ids
```

Flash / Governance Agent 看到这些信息后，才能判断：

- 是否无需写入。
- 是否补写遗漏。
- 是否修正失败候选。
- 是否合并相似候选。
- 是否创建 review / supersede / contradiction proposal。

## 7. Safe Point 与 Trigger 的区别

safe point 和 trigger 不是一回事。

```text
safe point 决定“能不能现在做”。
trigger 决定“值不值得现在做”。
```

天然 safe point：

- `after_tool_results`：一批工具调用已经结束，tool traces 稳定。
- `on_run_end_before_finalize`：本次 agent run 的最终回答已经产生，`RunEndEvent` 尚未写入。
- `before_compaction`：上下文即将压缩，细节可能丢失。
- workspace / task / thread switch 前。
- 定时任务 tick。

不是 safe point：

- 每个 token delta。
- 工具线程正在运行中。
- LLM 正在 streaming。
- EventLog append 中途。
- counter 增加的瞬间。

因此：

```text
tool_call_count_since_last_governance >= N
```

只是 trigger。真正执行治理要等最近的 safe point。

## 8. Governance Trigger Policy

候选池治理不应该每个 run 都做，也不应该只靠用户继续对话触发。推荐四类路径并存。

### 8.1 本轮补漏 Reflection

当 cheap hint 命中、且本轮没有主 agent memory attempt 时，可以在 `on_run_end_before_finalize` 这一类 safe point 唤醒一次轻量 Flash reflection。

这不是主路径，只是防止明显的用户记忆请求被主 agent 漏掉。

输出仍必须允许：

```json
{
  "should_reflect": false,
  "reason": "The cheap hint was a false positive.",
  "candidates": []
}
```

### 8.2 连续 N 个 Runs 后治理

连续 N 个 user runs 后，可以触发一次候选池治理。

这里的目标不是“立刻抽取本轮记忆”，而是：

- 扫描 pending candidate pool。
- 将多个 run 的候选聚类。
- 合并相似候选。
- 修正失败 attempts。
- 跳过 projection echo。
- 选择需要真正进入 `mem:*` 的候选。

初始 N 可以保守，例如 5。

### 8.3 Compact Safe Point 治理

context compaction 前后是强治理节点。

原因是 compaction 会丢失部分上下文细节。如果候选池里有依赖原始 run trajectory 的 pending candidates，应在 compaction 前后做一次治理或至少生成治理任务，避免重要证据变得难以追溯。

### 8.4 定时兜底 Governance

必须有定时兜底治理。

用户可能在某个 run 最后说“记住这个”，然后不再继续对话。如果只依赖“连续 N 个 runs”或 compaction，pending candidates 可能长期无人处理。

推荐增加 scheduled governance：

```text
hourly or daily
scan pending candidate pool
process candidates older than X minutes
batch limit
skip if no pending candidates
```

第一版可以使用：

```text
every 1 hour
pending age >= 10 minutes
batch limit = 50
```

定时任务不是主路径，而是兜底路径。它确保候选最终会被治理，不会永远留在 pending 状态。

## 9. Projection 与 Evidence 边界

Projection 是 recall view，不是新证据。

Governance Agent 必须知道 prompt 中投影过哪些 memory ids：

```text
memory_projection.included_memory_ids
```

任何候选如果主要来自 projection，而不是来自当前 user input、assistant action、tool result 或 artifact evidence，都应视为 memory echo，并拒绝写入。

允许的情况：

- 当前用户明确确认旧 memory 仍然正确。
- 当前用户修正旧 memory。
- 当前工具结果重新验证旧 memory。
- 当前 run 产生更权威的 evidence，支持 supersede / update。

不允许的情况：

- 把 prompt 里已有 memory 换句话再写一遍。
- 把 recalled preference 当成用户本轮新说的话。
- 把 history search result 直接提升为 durable memory，除非有新证据或用户确认。

Evidence 边界：

```text
EventLog / ArtifactStore = 证据层，保存原始运行事实和原文。
GraphStore = 语义层，只保存经过治理的长期语义节点。
Semantic memory 通过 evidence refs 指向证据，不内联大段原文。
```

## 10. Memory Governance Agent

候选池治理本身很难。候选池可能包含：

- 多个 run。
- 多个候选。
- 主 agent 成功或失败的 memory attempts。
- 已有 memory。
- 工具 evidence。
- projection echo。
- 相似候选。
- 冲突候选。
- 低置信候选。

这种场景下，单次 JSON extraction 很快会吃力。更合理的是维护一个后台 `MemoryGovernanceAgent`。

它可以是轻量 agent loop，但必须是受限 agent，不是第二个通用主 agent。

### 10.1 允许的工具

Memory Governance Agent 可以拥有窄工具：

- `list_pending_candidates`
- `inspect_candidate`
- `inspect_run_timeline`
- `inspect_tool_evidence`
- `search_existing_memory`
- `inspect_memory`
- `submit_governance_decision`

它不应拥有：

- shell
- 任意 filesystem write
- 通用业务工具
- 直接写 GraphStore 的能力
- 绕过 `MemoryWriteGate` 的能力

### 10.2 输出决策

治理 agent 不直接写 canonical memory。它输出治理决策：

- `skip`
- `propose`
- `correct`
- `merge`
- `needs_review`
- `supersede_proposal`
- `contradiction_proposal`

宿主代码负责：

```text
validate decision
validate typed candidate
dedupe exact duplicates
MemoryWriteService.submit()
MemoryWriteGate
Ledger / GraphStore
update candidate pool status
emit governance events
```

### 10.3 候选池去重流程

治理不是“删除重复字符串”。它应按候选族做决策。

流程：

```text
1. 读取 pending candidates。
2. 读取相关 run timeline / tool evidence / existing memory。
3. 将候选规范化为 CandidateEnvelope。
4. 按 content_key / 语义相似关系聚类。
5. 对每个 cluster 输出 governance decision。
6. 宿主执行通过验证的决策。
```

其中：

```text
content_key =
  kind
  normalized statement
  scope
  type-specific fields
```

但 content_key 只是聚类线索，不是最终裁判。最终是否跳过、修正、合并或提出新候选，应由 governance agent 在读取证据和已有记忆后决定。

### 10.4 Prompt 原则

Memory Governance Agent 的 prompt 不应像主 agent prompt 那样包含完整业务工具说明。它只需要 memory governance 所需信息。

Prompt 必须强调：

- 候选池中的失败 attempt 可以被修正，但必须保留审计链。
- 已成功写入且足够好的候选不要重复写。
- Projection echo 不能写回。
- Evidence-backed candidates 优先于 conversation-only candidates。
- 用户显式指令可以有高 authority，但仍要经过 gate。
- 没有 durable future-use information 时应输出 skip，而不是硬写。

Few-shot 应覆盖：

- cheap hint 命中但只是“以后再说”的负例。
- 主 agent 已经成功提交 preference，治理 agent 跳过重复。
- 主 agent action boundary 失败，治理 agent 生成 corrected candidate。
- 多个相似候选被 merge。
- projection echo 被拒绝。
- tool evidence 支持 ObservationCandidate。

## 11. 慢速 Dream / Maintenance

候选池治理不是完整 maintenance。

Governance 处理的是 pending candidates：

- 当前是否该写入。
- 是否修正失败 attempt。
- 是否合并同批候选。
- 是否跳过 echo / duplicate。

慢速 dream / maintenance 处理更长期的问题：

- 合并重复长期记忆。
- 标记 superseded / contradicted。
- 检查 stale memory。
- 整理 needs-review。
- 将重复 workflow 提炼为 skill proposal。
- 产生 policy / projection proposal。

这一层不应混进主 loop。它应该是可审计、可重跑、可暂停的后台 job。

## 12. 推荐事件与审计

候选池治理应产生可审计事件。未来可以增加：

- `MemoryCandidateQueuedEvent`
- `MemoryGovernanceStartedEvent`
- `MemoryGovernanceDecisionEvent`
- `MemoryGovernanceCompletedEvent`
- `MemoryGovernanceFailedEvent`

这些事件回答：

- 哪些候选进入了池。
- 为什么触发治理。
- 治理 agent 读了哪些证据。
- 哪些候选被 skip / correct / propose / merge。
- 哪些最终进入 canonical memory。
- 哪些进入 needs review。

当前已有的：

- `MemoryCandidateProposedEvent`
- `MemoryWriteResultEvent`
- `MemoryWriteFailedEvent`
- `MemoryReflectionCompletedEvent`
- `MemoryReflectionFailedEvent`

可以继续保留，但长期应区分：

```text
reflection = 当前 run 的轻量补漏
governance = 跨候选池的后台治理
maintenance = 更慢的生命周期维护
```

## 13. 与当前实现的差异

当前第二阶段实现中，`ReflectiveMemoryHooks` 已经开始统一处理：

- cheap hint
- memory attempt
- tool call threshold
- turn threshold
- token threshold

但最终目标应更克制：

- `remember_*` 主 agent 工具主要写 candidate pool。
- cheap hint 只在主 agent 未调用 memory tool 时触发轻量补漏 reflection。
- tool / turn / token 阈值不直接等于“写入记忆”，而是触发候选池治理。
- 定时任务兜底 pending candidates。
- 复杂治理交给受限 Memory Governance Agent。

因此下一轮重构的方向不是继续增强每 run reflection，而是：

```text
durable candidate pool
  -> governance agent
  -> canonical memory write path
```

## 14. 核心原则

1. 主 agent 可以提出记忆候选，但不承担稳定写入责任。
2. Flash / Governance Agent 不能绕过 gate。
3. Governance 必须知道主模型 memory tool attempts 和结果。
4. 已成功写入且足够好的主模型记忆不能被重复写入。
5. 失败的主模型记忆可以被修正，但必须保留审计链。
6. Projection 不能被原样写回。
7. EventLog 和 ArtifactStore 是证据层，不是 canonical semantic memory。
8. GraphStore 只接受经过治理的语义节点。
9. 定时任务兜底 pending candidates。
10. 慢速 dream / maintenance 负责长期整理，不挤进主任务 loop。

## 15. 核心结论

Pulsara 不应让长期记忆写入依赖主模型“刚好想起调用工具”，也不应让 Flash 每轮都急于写入。

更稳定的设计是：

```text
主 agent 提候选。
cheap hint 补漏。
候选池 durable 保存。
后台治理 agent 读证据、查已有记忆、做 skip/correct/propose/merge。
MemoryWriteGate 决定 canonical memory 是否落盘。
定时任务保证 pending candidates 不会永远悬空。
```

这样当前用户体验不会被后台记忆治理阻塞，跨轮长期记忆也不会依赖偶然触发。
