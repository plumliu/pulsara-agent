# JSON-LD 多层记忆运行时治理设计

## 状态标注

```text
Current
  五层职责边界已经成为当前设计约束
  MemoryWriteGate / Execution Evidence Ledger 已有 MVP 代码

Next
  ProjectionEngine
  Background Memory Curator
  maintenance / stale / supersede 的自动化流程

Target
  完整的多层记忆运行时治理闭环
  持续 consolidation / review queue / 生命周期维护
```

本文档沉淀一次架构讨论的稳定结论，作为 Python 版通用 Agent 的记忆运行时治理规范。它与根目录下的 `MEMORY_SYSTEM_IMPLEMENTATION_DETAILS.zh.md` 和 `MVP_AGENT_RUNTIME_DESIGN.zh.md` 配套：

- `MEMORY_SYSTEM_IMPLEMENTATION_DETAILS.zh.md` 负责 JSON-LD / RDF / SPARQL / memory ontology。
- `MVP_AGENT_RUNTIME_DESIGN.zh.md` 负责主 Agent loop 和 provider 边界。
- 本文档负责记忆系统运行时的分层边界、写入决策、召回投影、维护删除、自进化边界。

本文档不会吸收讨论中所有观点。早期、不稳定、容易导致系统漂移的想法被主动排除。核心原则是：

```text
JSON-LD 是语义记忆的底层对象。
GraphStore 是可查询事实层。
Projection 是视图，不是存储。
Agent 可以提出记忆意图，但不能绕过 runtime 写入或查询。
```

## 1. 总体立场

我们只受 Gliding Horse 的多级记忆愿景启发，不沿用它的 `L0/L1/L2/L3` 命名。

原来的 `L*` 命名容易制造一种“缓存层级神话”：仿佛每一层都应该是一个独立数据库，每一层都有自己的持久化策略。但我们真正需要的是职责边界，而不是层级崇拜。

最终采用五个概念层：

```text
Working Context Cache
Task Graph / Blackboard
Durable Semantic Memory
Archive / Blob Store
Prompt Projection
```

但 MVP 工程实现不应该做成五套存储。第一版只需要四个运行时组件：

```text
LoopState
GraphStore
ArchiveStore
ProjectionEngine
```

其中：

```text
Working Context Cache      -> LoopState
Task Graph / Blackboard    -> GraphStore, scoped named graph
Durable Semantic Memory    -> GraphStore, scoped named graph
Archive / Blob Store       -> ArchiveStore + GraphStore metadata
Prompt Projection          -> ProjectionEngine generated view
```

`Task Graph / Blackboard` 和 `Durable Semantic Memory` 共享同一个 GraphStore，只靠 named graph、scope、type、lifecycle 区分。

## 2. 五层职责

### 2.1 Working Context Cache

当前 loop 的短期状态，不是长期记忆。

保存内容：

```text
当前 turn 状态
当前工具调用队列
budget / token 估算
临时 scratchpad
当前 projection 的 @id 引用
```

硬规则：

```text
只服务当前 loop，可丢弃。
不得作为事实源。
不得被 SPARQL 查询当作长期事实。
不得在 compaction 后自动晋升为长期记忆。
```

如果其中的信息有长期价值，必须转成 `MemoryCandidate`，经过 `MemoryWriteGate`。

### 2.2 Task Graph / Blackboard

当前任务和会话的执行图，是最适合 JSON-LD / RDF 的层之一。但 MVP 不实现完整 Task Graph / Blackboard。探索性工作边界太软，过早维护完整任务图会导致关系膨胀和证据失效难以治理。

MVP 将这一层收缩为 `Execution Evidence Ledger`：

```text
Execution Evidence Ledger = append-only execution/evidence graph + claim lifecycle
```

它只证明工具结果、证据、结论、反驳、替代和 Projection 过滤闭环成立。

保存内容：

```text
turn
tool call
tool result
artifact reference
evidence link
claim
decision
```

硬规则：

```text
MVP 只保存当前 session / turn 的执行证据关系。
必须有 scope。
可以被 SPARQL 查询。
默认不是跨 session 长期记忆。
```

如果某个执行经验具有未来价值，应由 consolidation 产生新的 `mem:ExperienceFact`、`mem:Observation` 或 `mem:Claim`，再经过 `MemoryWriteGate` 写入 Durable Semantic Memory。

### 2.3 Durable Semantic Memory

长期语义记忆，是第二个真正应该进入 JSON-LD / RDF 的层。

保存内容：

```text
UserPreference
ActionBoundary
Decision
ExperienceFact
WorldFact
Observation
MentalModel
SkillConstraint
WorkspaceConvention
```

硬规则：

```text
只保存跨 session 有未来价值的 typed memory。
必须有 scope。
必须有 evidence。
必须有 lifecycle status。
必须经过 MemoryWriteGate。
```

禁止把完整聊天记录、长工具输出、大文档正文直接塞入 Durable Semantic Memory。

### 2.4 Archive / Blob Store

原文、大对象、完整日志和工具输出的保管层。

保存内容：

```text
完整对话 turn
完整 tool stdout/stderr
大文档正文
附件
网页原文
测试输出全文
原始 JSON response
```

硬规则：

```text
Archive 保存原文，不参与语义推理。
GraphStore 只保存 archive metadata。
语义记忆通过 @id 指向 archive evidence。
```

示例：

```json
{
  "@id": "artifact:pytest-output-2026-06-06T01",
  "@type": "mem:Artifact",
  "mem:storedAt": "archive://session/042/tool/pytest-output.txt",
  "mem:hash": "sha256:...",
  "mem:summary": "pytest 输出，3 个失败，主要集中在 JSON-LD expansion",
  "mem:createdFrom": { "@id": "turn:042/tool-call-003" }
}
```

pytest 全文不进图，只有 metadata、summary、hash、source link 进图。

### 2.5 Prompt Projection

Projection 是从 GraphStore / ArchiveStore / LoopState 中查询、裁剪、压缩、排序后生成的 prompt context view。

它不是 store，不是事实源，也不是长期记忆。

硬规则：

```text
Projection 只能由查询生成。
不得手写成 canonical memory。
不得被原样写回 Durable Semantic Memory。
不得绕过 MemoryWriteGate。
```

Projection 的核心目标是角色化、任务化、预算化。

例如 `DA Projection` 是给 `Do Agent` 的执行视图。它不需要看到所有历史，只需要：

```text
当前步骤
上级计划
允许/禁止的工具
目标文件或 artifact
相关 action boundary
近期同类失败经验
当前 task 的 active decisions
```

如果未来实现 PA / DA / CA / AA，多角色之间不能平权共享全部上下文，必须通过 projection 裁剪。

## 3. 防止分层漂移的写入规则

分层能否长期保持，不取决于文档写得多漂亮，而取决于写入入口是否被限制。

每层只有一个事实责任和一个禁止事项：

```text
Working Context Cache
  事实责任：当前 loop 状态
  禁止事项：不得作为长期事实源

Task Graph / Blackboard
  事实责任：MVP 中只负责 Execution Evidence Ledger，即 turn/tool result/artifact/evidence/claim/decision 的证据账本
  禁止事项：不得扩张成完整 task/plan/role handoff graph，也不得直接冒充长期语义记忆

Durable Semantic Memory
  事实责任：跨 session 有未来价值的 typed memory
  禁止事项：不得保存全文和未经 gate 的推断

Archive / Blob Store
  事实责任：原文和大对象证据
  禁止事项：不得承担语义推理

Prompt Projection
  事实责任：给模型看的最小必要视图
  禁止事项：不得作为 canonical memory 写回
```

MVP 必须把这些规则落实成代码边界：

```text
LoopState API
GraphStore API
ArchiveStore API
ProjectionEngine API
MemoryWriteGate API
```

任何模块如果想写 Durable Semantic Memory，都必须走 `MemoryWriteGate`。

## 4. 写入侧：谁决定什么值得记住

主 Agent 不应该自由决定什么进入长期记忆。它可以提出候选，但最终由 runtime 裁决。

写入侧采用三条路径。

### 4.1 显式写入 Fast Path

当用户明确表达：

```text
记住……
以后……
不要再……
这个项目里……
我的偏好是……
```

系统应同步生成 `MemoryCandidate`，优先进入 `MemoryWriteGate`。

这类候选的默认来源权威较高：

```text
mem:sourceAuthority = explicit_user_instruction
mem:verificationStatus = user_confirmed
```

但仍需检查 scope、敏感性、冲突和过期条件。

### 4.2 回合后候选提取

每个 turn 结束后，系统从新增信息里提取候选记忆。

可提取对象：

```text
用户纠正
用户偏好
ActionBoundary
任务决策
工具验证事实
失败经验
未来可能复用的 workflow quirk
```

这一步可以由一个受限的后台整理器完成，但它只输出 `MemoryCandidate`，不能直接写长期记忆。

### 4.3 Session 结束后的 Consolidation

会话结束或空闲时，运行慢速 consolidation。

职责：

```text
合并重复候选
把多条 ExperienceFact 归纳为 Observation
把稳定 Observation 晋升为 MentalModel
发现旧记忆被 supersede
发现矛盾或过期
生成 MaintenanceProposal
```

Consolidation 不是主 Agent loop 的一部分，不能推进用户任务。

## 5. Background Memory Curator

系统可以有后台辅助 Agent，但它必须是弱权限的 `Background Memory Curator`，不是第二个主 Agent。

允许它做：

```text
提取候选记忆
合并候选
给候选打 type / scope / evidence
生成 session summary
生成 consolidation proposal
生成 maintenance proposal
```

禁止它做：

```text
修改 workspace
调用有副作用的外部工具
直接写 Durable Semantic Memory
直接物理删除记忆
推进主任务
直接写 SQL / SPARQL
修改 ontology / schema
```

它的输出必须进入：

```text
MemoryCandidate -> MemoryWriteGate
MaintenanceProposal -> MaintenanceGate / ReviewQueue
```

## 6. MemoryWriteGate

`MemoryWriteGate` 是长期记忆的唯一入口。

它检查：

```text
是否有 future utility
是否有明确 scope
是否有 evidence
是否只是临时任务状态
是否可轻易重新发现
是否重复
是否与旧记忆冲突
是否包含敏感信息
是否影响未来行动
是否需要 staleAfter / expiresAt
是否应进入 Archive 而非 Semantic Memory
```

`MemoryWriteGate` 的输出只有几类：

```text
accepted
rejected
working_context_only
archive_only
needs_user_review
needs_more_evidence
supersedes_existing
contradicts_existing
```

### 6.1 置信度不由 LLM 决定

LLM 不应该输出最终 confidence。

LLM 可以输出：

```text
candidate
evidence
source explanation
rationale
```

最终置信等级由 runtime 根据证据计算。

MVP 采用离散字段：

```text
mem:confidenceLevel
  low | medium | high | verified

mem:verificationStatus
  unverified | inferred | user_confirmed | tool_verified | contradicted | stale

mem:sourceAuthority
  model_inference | conversation_evidence | explicit_user_instruction | tool_result | document_source | system_rule
```

示例：

```text
用户明确说“以后不要自动提交代码”
  sourceAuthority = explicit_user_instruction
  verificationStatus = user_confirmed
  confidenceLevel = verified

模型推断“用户可能喜欢架构讨论”
  sourceAuthority = model_inference
  verificationStatus = inferred
  confidenceLevel = low

工具验证“当前仓库存在 Cargo.toml”
  sourceAuthority = tool_result
  verificationStatus = tool_verified
  confidenceLevel = high
  staleAfter = short
```

这比浮点置信度更诚实，也更容易调试。

## 7. 读取侧：谁写查询，谁构建 Prompt

MVP 不允许主 Agent 自己写 SQL 或 SPARQL。

原因：

```text
查询容易漂移
prompt injection 可能影响查询
token 成本不可控
召回稳定性难以测试
SPARQL 语义错误会污染 projection
```

读取侧流程：

```text
ScopeResolver
  -> ProjectionPlanner
  -> fixed SPARQL templates
  -> QueryExecutor
  -> ProjectionFilter
  -> MemoryProjection
  -> ContextBuilder
```

### 7.1 ScopeResolver

决定当前 turn 的有效 scope：

```text
user
agent
session
task
workspace
domain
artifact
skill
```

没有工作目录时，通用 Agent 仍然可以用：

```text
mem:UserPeer
mem:AgentPeer
mem:Session
ctx:ConversationThread
ctx:Domain
ctx:Task
```

项目目录是强 scope，但不是唯一 scope。

### 7.2 ProjectionPlanner

根据角色、任务、预算选择 projection 模板。

MVP 可先实现：

```text
UserPreferenceProjection
ActionBoundaryProjection
TaskDecisionProjection
RecentFailureProjection
SessionRecoveryProjection
WorkspaceConventionProjection
```

未来如果实现多角色：

```text
PAProjection
DAProjection
CAProjection
AAProjection
```

### 7.3 Agent 如何按需深挖

Agent 不写 SPARQL，但可以提出 retrieval intent。

提供工具：

```text
memory_search(query, scopes, types)
memory_get(@id)
memory_expand(@id, relation, hops)
```

Agent 输出：

```json
{
  "intent": "find_prior_failures",
  "scope": ["ctx:workspace/current", "ctx:task/current"],
  "types": ["mem:ExperienceFact", "mem:Observation"],
  "about": "pytest failure after JSON-LD expansion"
}
```

runtime 将 retrieval intent 编译成固定查询模板。

## 8. Projection Filter

Projection 构建前必须过滤记忆状态，防止旧记忆污染 prompt。

默认策略：

```text
active + high/verified
  正常注入。

active + medium
  可注入，但权重低。

stale
  默认不注入；高相关时可注入并标注“需验证”。

superseded
  默认不注入，只在追溯/审计 projection 中出现。

contradicted
  不注入执行上下文，只在冲突审计中出现。

deleted / redacted
  永不注入。
```

Projection 的职责不是“多召回”，而是“召回后不伤害当前任务”。

## 9. 记忆生命周期

长期记忆不是静态条目，而是有生命周期的对象。

推荐状态：

```text
candidate
active
stale
superseded
contradicted
archived
redacted
deleted
```

常见转移：

```text
candidate -> active
active -> stale
active -> superseded
active -> contradicted
stale -> active
stale -> archived
superseded -> archived
active -> redacted
active -> deleted
```

其中 `deleted` 应该少用。大多数情况下，保留 tombstone 和 provenance 更符合 JSON-LD / graph memory 的优势。

### 9.1 删除不是第一选择

默认策略：

```text
过时 -> stale
被新事实替代 -> superseded
与新证据冲突 -> contradicted
长期无用 -> archived
敏感信息移除 -> redacted / deleted
用户明确要求删除 -> deleted
```

例如旧记忆：

```text
“这个项目使用 Rust”
```

后来项目切换到 Python 版，不应该直接物理删除旧记忆，而应：

```text
old_memory mem:status "superseded"
old_memory mem:supersededBy new_memory
new_memory mem:supersedes old_memory
```

这样图里保留了决策演化链。

### 9.2 物理删除的适用场景

物理删除只用于：

```text
用户明确要求删除
PII / sensitive data removal
法律或合规要求
明显垃圾数据
prompt injection 污染条目
测试数据污染真实库
```

其他情况优先使用 `archived`、`redacted`、`superseded`。

## 10. 维护触发：不只靠定时任务

记忆维护由三种触发并存。

### 10.1 写入时维护

新记忆进入前检查：

```text
是否重复
是否冲突
是否 supersede 旧记忆
是否只该进入 Working Context
是否需要 expiresAt / staleAfter
是否需要 user review
```

很多“删除”应在写入时变成 `superseded`。

### 10.2 召回时维护

每次召回都是一次污染防线。

召回时检查：

```text
status
staleAfter / expiresAt
scope 是否仍匹配
是否被 superseded
是否有 contradicted 关系
是否可轻量工具验证
```

例如召回：

```text
“这个项目用 cargo test”
```

但当前 workspace 没有 `Cargo.toml`，应立即降低权重或标记 `stale`，而不是等夜间任务。

### 10.3 定时后台维护

定时任务适合做慢清理：

```text
扫描 staleAfter / expiresAt
找长期未使用的 low confidence 记忆
找重复记忆
找已 superseded 很久的旧记忆
生成 review queue
归档低价值记忆
```

定时任务默认生成 `MaintenanceProposal`，不应大规模物理删除。

低风险提案可自动执行，高风险提案进入人工审阅。

## 11. 使用统计

每条长期记忆应记录运行时统计：

```text
retrievalCount
lastRetrievedAt
appliedCount
correctionCount
rejectionCount
lastVerifiedAt
lastContradictedAt
```

维护规则可以参考：

```text
长期没召回 + low confidence + 非用户明确要求
  -> archived proposal

频繁召回但从未被使用
  -> 降权

被用户纠正
  -> contradicted / superseded

多次被工具验证
  -> confidenceLevel 提升
```

这些统计不应由 LLM 自报，而应由 runtime 记录。

## 12. Hermes 的启发和边界

Hermes 对我们有启发，但不能照搬。

### 12.1 内置 memory 删除

Hermes 内置 memory 主要是 `MEMORY.md` / `USER.md` 两个 bounded text store。

它支持：

```text
add
replace
remove
```

`remove` 是用短字符串匹配条目，然后从文件里删除。

这不是 semantic deletion：

```text
没有 supersededBy
没有 tombstone
没有 provenance
没有 confidence lifecycle
没有 graph relation
```

所以我们的系统不能把“删文本条目”当作核心删除模型。

### 12.2 Session prune

Hermes 的 `sessions prune` 是历史 session 存储保留策略，不是单条语义记忆的生命周期管理。

我们可以借鉴它的保守默认：

```text
自动 prune 默认关闭
只清理 ended sessions
active session 不动
```

但 Durable Semantic Memory 的维护必须更细。

### 12.3 Skill curator

Hermes 的 skill curator 更值得借鉴：

```text
active / stale / archived
never deletes
archive is recoverable
pinned items are protected
```

这与我们的记忆维护哲学一致：默认归档，不默认硬删除。

### 12.4 Honcho delete

Honcho 插件有删除 conclusion 的能力，但主要用于 PII removal。

错误结论更多依赖后续 self-heal，而不是频繁物理删除。

这启发我们：

```text
PII 删除必须硬。
普通错误记忆优先 contradicted / superseded。
```

## 13. “无限进化”的正确转译

这里的“无限进化”不是让模型自改权重，也不是让主 loop 在线自改代码。

对 JSON-LD 多层记忆系统来说，合理的转译是：

```text
在线运行时收集真实轨迹。
离线演化器优化记忆策略工件。
评测通过后进入 review。
批准后发布新版本。
```

### 13.1 可以演化的对象

允许演化：

```text
Memory extraction prompt
MemoryWriteGate policy text
Projection templates
SPARQL template variants
Recall ranking policy
Consolidation prompt
Maintenance policy
Skill descriptions
Tool descriptions
```

这些是可替换工件，收益高，风险可控。

### 13.2 不允许自动演化的对象

不允许自动演化：

```text
核心 ontology
JSON-LD @context 基础语义
status state machine
permission / action boundary 硬规则
GraphStore 写入语义
主 Agent loop
MemoryWriteGate 硬约束
物理删除策略
```

这些属于内核，改动必须人工设计、评审和迁移。

### 13.3 记忆系统自进化流水线

推荐流水线：

```text
collect traces
  -> build write / recall eval dataset
  -> generate candidate policy/template
  -> validate constraints
  -> evaluate holdout
  -> emit proposal
  -> human review
  -> promote version
```

评测集分两类：

```text
Write eval
  输入 session archive / tool trace。
  期望输出：应该写哪些 MemoryCandidate，应该拒绝哪些。

Recall eval
  输入 task context。
  期望输出：projection 应召回哪些记忆，不应召回哪些噪声。
```

硬指标：

```text
type_correctness
scope_correctness
evidence_preserved
sensitive_data_blocked
memory_echo_rate
stale_memory_leak_rate
action_boundary_hit_rate
projection_token_cost
useful_memory_hit_rate
contradiction_detection_rate
```

### 13.4 演化过程也应写成 JSON-LD

演化本身应进入图。

示例：

```json
{
  "@id": "evo:projection/da-input/v3",
  "@type": ["mem:EvolutionCandidate", "mem:ProjectionTemplateVersion"],
  "mem:derivedFrom": { "@id": "evo:projection/da-input/v2" },
  "mem:target": { "@id": "projection:da_input" },
  "mem:status": "pending_review",
  "mem:metrics": {
    "mem:recallPrecision": 0.82,
    "mem:noiseRate": 0.14,
    "mem:tokenCostDelta": -0.21,
    "mem:staleMemoryLeakRate": 0.03
  }
}
```

这样“无限进化”不是口号，而是可追溯的版本链：

```text
policy:v1
  -> candidate:v2
  -> eval_run
  -> approved:v2
```

## 14. MVP 落地顺序

第一阶段只做运行时骨架：

```text
LoopState
ArchiveStore
GraphStore
ProjectionEngine
MemoryWriteGate
```

第二阶段做写入闭环：

```text
Execution Evidence Ledger
mem:Turn / mem:ToolResult / mem:Artifact / mem:Evidence / mem:Claim / mem:Decision
MemoryCandidate
explicit memory fast path
post-turn extractor
sourceAuthority / verificationStatus / confidenceLevel
basic duplicate and conflict check
```

第三阶段做读取闭环：

```text
ScopeResolver
ProjectionPlanner
fixed SPARQL templates
ProjectionFilter
memory_search / memory_get / memory_expand
```

第四阶段做维护闭环：

```text
staleAfter / expiresAt
supersedes / contradicts
retrieval stats
MaintenanceJob
ReviewQueue
archive / redaction / deletion policy
```

第五阶段才做自进化：

```text
trace collection
write eval
recall eval
policy/template candidate generation
offline evaluation
human review
versioned promotion
```

## 15. 最小不可破坏约束

无论后续实现如何变化，以下约束不能破：

```text
1. Projection 不是 memory store。
2. Archive 不承担语义推理。
3. Durable Semantic Memory 必须经过 MemoryWriteGate。
4. Agent 不直接写 SQL / SPARQL。
5. LLM 不决定最终 confidence。
6. 默认状态变更，不默认物理删除。
7. MVP 中 Task Graph / Blackboard 只落地为 Execution Evidence Ledger；未来扩展出的 Task Graph 和 Durable Semantic Memory 可以共享 GraphStore，但必须用 scope / named graph / type 区分。
8. JSON-LD 必须能展开为 RDF quads，不能拍扁成伪 triple。
9. 自进化只能优化策略工件，不能自动改核心 ontology 和主 loop。
10. 所有高风险维护动作必须进入 ReviewQueue。
```

如果这些约束守住，五层架构就不会漂移成五套混乱存储，JSON-LD 也不会退化成普通 JSON 的装饰层。
