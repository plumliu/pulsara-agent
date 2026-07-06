# JSON-LD 记忆系统实现细则

## 状态标注

```text
Current
  JSON-LD canonical object、ontology 分层、Execution Evidence Ledger 方向已体现在代码结构中

Next
  GraphStore 从 in-memory JSON-LD store 演进到真正的 RDF query layer
  summary / projection / recall pipeline 继续实现

Target
  RDF quads + SPARQL + richer memory ontology
  Durable Semantic Memory 的完整 typed memory system
```

本文档是一份面向 Python 版通用 Agent 的记忆系统设计规范。它参考了 Gliding Horse 对 JSON-LD / Oxigraph / 多级记忆的愿景，也参考了 Claude Code、OpenClaw、Hermes 及其外部 memory provider 的实现思路。

但本文档的核心立场是收敛的：

```text
不能把所有 memory provider 的思想都吸收到核心系统里。
否则记忆系统会变成“记忆功能博物馆”：概念很多，核心对象不清楚，JSON-LD 最后又退化成套壳。
```

因此，本系统只吸收少数真正必要的思想：

```text
Hindsight: typed memory、evidence、observation、temporal/graph recall
Claude Code / OpenClaw: memory governance、compaction、dreaming、保存闸门
Honcho: Peer / Session 建模，用于没有项目目录的通用 Agent
Supermemory / Mem0: 只吸收工程经验，例如自动抽取、去重、context fencing
```

OpenViking、ByteRover、Holographic 等 provider 的思想暂不进入核心。它们可以作为未来 adapter、browser 或实验模块，而不是 V1 的本体论基础。

本文档的最终目标是把 JSON-LD 做成真正的 semantic memory substrate，而不是普通 JSON 外面包几个 `@id`、`@type`。

## 1. 总体判断

### 1.1 我们真正要解决的问题

通用 Agent 的记忆系统至少要解决这些问题：

1. 它应该记住什么，不应该记住什么。
2. 记忆应该挂在哪个上下文上：用户、会话、任务、领域、项目、文档、skill，还是 agent 自己。
3. 长期记忆如何避免污染、重复、过期、矛盾。
4. 召回时如何不是简单 top-k 文本，而是按 scope、type、relation、evidence 和时间共同检索。
5. 压缩上下文时如何不丢关键状态。
6. 没有工作目录或项目时，通用 Agent 如何组织记忆。
7. JSON-LD 进入图数据库后，语义信息不能被拍扁。

其中第 7 点是 Gliding Horse 当前实现和宏大愿景之间的关键落差。JSON-LD 如果不能展开为 RDF quads 并被 SPARQL 查询，那么它只是 JSON 样式的装饰。

### 1.2 V1 不做什么

V1 不做这些事：

1. 不实现 provider marketplace。
2. 不把 Mem0、Supermemory、Honcho、Hindsight 都接一遍。
3. 不让 LLM 原生生成 JSON-LD。
4. 不让 LLM 直接写 RDF triple。
5. 不把所有聊天记录都当长期记忆。
6. 不把向量库当 canonical memory store。
7. 不追求复杂心理画像、人格模拟、实验性向量代数。

V1 只做一件事：

```text
把记忆定义成有 scope、有 type、有 evidence、有生命周期、可展开为 RDF、可被 SPARQL 查询的 JSON-LD 对象。
```

## 2. 核心原则

### 2.1 JSON-LD 是 canonical object

系统中的长期记忆对象必须以 JSON-LD 文档作为 canonical representation。

JSON-LD 的职责：

```text
stable identity: 每个重要对象都有 @id
semantic type: 每个对象都有 @type
semantic relation: 对象之间用 IRI 边连接
context mapping: 字段名通过 @context 映射到统一谓词
provenance: 每条记忆保留来源、证据、创建方式
interop: 未来可与 skill graph、task graph、artifact graph 对接
```

### 2.2 RDF quads 是查询事实层

JSON-LD 文档写入后必须展开为 RDF quads。

```text
JSON-LD document
  -> JSON-LD expansion
  -> RDF quads
  -> GraphStore / Oxigraph / rdflib
  -> SPARQL query
```

禁止把 JSON 字段拍扁成伪 triple：

```text
mem:node http://agent-os.org/prop/foo "{...json string...}" .
```

这种做法会丢掉 JSON-LD 的核心价值。

### 2.3 向量索引只是召回候选器

向量检索可以用，但它不是事实源。

```text
DocumentStore: 保存原始 JSON-LD 文档
GraphStore: 保存 RDF quads，负责语义查询
SearchIndex: BM25 / vector / hybrid search，只负责找候选
SessionArchive: 保存完整历史，用于证据和回放
```

如果向量搜索和 GraphStore 冲突，以 GraphStore 和 evidence 为准。

### 2.4 Provider 思想只允许转译，不允许照搬

外部 memory provider 的概念不能直接污染核心本体。

例如：

```text
Mem0 的 profile       -> 可以转译成 mem:UserProfile / mem:Fact
Honcho 的 Peer        -> 可以转译成 mem:Peer
Hindsight Observation -> 可以转译成 mem:Observation
Supermemory container -> 可以转译成 ctx:Scope / ctx:Container
```

但不应该把每个 provider 的内部术语都变成核心类型。

## 3. V1 应吸收什么

### 3.1 从 Hindsight 吸收 typed memory

Hindsight 最值得吸收的是它把记忆分层：

```text
WorldFact
  关于外部世界的事实。

ExperienceFact
  Agent 自己经历过的事实，例如做过什么、哪里失败过、用户纠正过什么。

Observation
  从多条事实和证据中归纳出的观察。

MentalModel
  稳定模型，例如用户偏好、任务模式、领域规律。
```

这套分类非常适合 JSON-LD，因为它避免所有长期记忆都退化成 `mem:Memory` + `mem:text`。

V1 采用以下核心类型：

```text
mem:Fact
mem:WorldFact
mem:ExperienceFact
mem:Observation
mem:MentalModel
mem:Decision
mem:ActionBoundary
mem:Evidence
```

### 3.2 从 Claude Code / OpenClaw 吸收治理

Claude Code 和 OpenClaw 的强项不是复杂图谱，而是治理：

```text
什么该保存
什么不该保存
何时保存
何时压缩
何时晋升
何时需要用户确认
如何避免 prompt 和 memory 互相污染
```

V1 必须有 memory write gate。没有保存闸门，长期记忆一定会变脏。

### 3.3 从 Honcho 吸收 Peer / Session

通用 Agent 不一定有项目目录，但一定有交互双方。

因此 V1 吸收 Honcho 的最小建模思想：

```text
mem:Peer
mem:UserPeer
mem:AgentPeer
mem:Session
mem:Turn
mem:Message
```

但不吸收复杂 dialectic reasoning 作为核心能力。它以后可以作为 consolidation strategy。

### 3.4 从 Mem0 / Supermemory 吸收工程经验

Mem0 和 Supermemory 值得吸收的是工程手法，不是本体论：

```text
自动抽取候选记忆
重复检测
profile 分层
session-end ingest
context fencing
hybrid search
rerank
forget / update API
```

其中最重要的是 context fencing：

```text
召回出来的 memory 不能在下一轮被原样写回成新 memory。
```

否则系统会产生 memory echo，记忆会自我复制、自我强化，最后污染事实层。

## 4. V1 暂不吸收什么

### 4.1 不吸收 provider marketplace

外部 provider adapter 可以以后做，但不应该先做。

原因：

```text
如果 canonical memory object 没有定型，adapter 越多，混乱越多。
```

### 4.2 不吸收 OpenViking / ByteRover 的知识文件系统

它们的层级浏览思想有价值，但更适合未来的 memory browser。

V1 可以保留 `memory_browse` 的设计空间，但不把 tree ontology 放入核心。

### 4.3 不吸收 Holographic 的实验性查询

Holographic 的 trust score、fact feedback 有参考价值，但 HRR / compositional algebra 对 V1 来说过早。

V1 只保留简单的：

```text
mem:confidenceLevel
mem:verificationStatus
mem:trustLevel
mem:sourceAuthority
mem:userFeedback
```

### 4.4 不吸收复杂人格画像

用户 profile 必须服务于任务连续性和交互偏好，而不是过度心理化。

不保存：

```text
未经确认的心理推断
敏感身份推断
医疗、政治、宗教等高风险属性推断
一次性情绪状态
```

## 5. 记忆层级

V1 只受 Gliding Horse 的多级记忆愿景启发，不沿用 `L0/L1/L2/L3` 命名。旧命名容易让实现误以为每一层都应该是独立数据库；本文档采用按职责命名的五个概念层。

```text
Working Context Cache
  当前 loop 的短期状态，不是长期记忆。

Task Graph / Blackboard
  当前 session / task 的执行图和工作黑板。

Durable Semantic Memory
  长期 JSON-LD object + RDF quads。

Archive / Blob Store
  完整会话、工具调用、附件、大对象和原文证据。

Prompt Projection
  为当前 prompt 生成的可读视图，不是事实源。
```

MVP 不把五层做成五套存储。物理实现应收敛为：

```text
LoopState
  承载 Working Context Cache。

GraphStore
  承载 Task Graph / Blackboard 与 Durable Semantic Memory，通过 named graph / scope / type 区分。

ArchiveStore
  承载 Archive / Blob Store。

ProjectionEngine
  生成 Prompt Projection。
```

### 5.1 Working Context Cache

Working Context Cache 是当前 loop 的短期状态。

它只放：

```text
当前 turn 状态
当前工具调用队列
budget / token 估算
临时 scratchpad
当前 projection 的 @id 引用
```

它不放：

```text
完整历史
大段工具输出
低置信度观察
可通过文件或工具重新获得的信息
任何 canonical memory
```

Working Context Cache 可丢弃，不作为事实源。若其中内容有长期价值，必须转成 `MemoryCandidate`，再经过 `MemoryWriteGate`。

### 5.2 Task Graph / Blackboard

Task Graph / Blackboard 是当前 session / task 的执行图。但 MVP 不实现完整工作黑板，而是先落地 `Execution Evidence Ledger`。

MVP 的目标不是维护完整任务关系，而是证明：

```text
工具执行可追溯。
工具结果可形成证据。
证据可支撑或反驳结论。
新结论可替代旧结论。
Projection 能过滤过期、矛盾、被替代的结论。
```

保存：

```text
mem:Turn
mem:ToolResult
mem:Artifact
mem:Evidence
mem:Claim
mem:Decision
```

Execution Evidence Ledger 可以频繁追加，也可以进入 GraphStore 查询，但它默认不是跨 session 长期记忆。

`Turn / ToolResult / Artifact / Evidence` 属于 runtime provenance，可以由 runtime 按固定 schema 追加。`Claim / Decision` 属于结论节点，只能先作为 candidate，由 `MemoryWriteGate` 审核后才能成为 active claim 或 durable decision。

### 5.3 Durable Semantic Memory

Durable Semantic Memory 是长期语义记忆。

每条 Durable Semantic Memory 必须：

```text
有 @id
有 @type
有 scope
有 evidence 或 createdFrom
有 confidenceLevel / verificationStatus / sourceAuthority
有 lifecycle status
能展开为 RDF quads
能被 SPARQL 查询
```

Durable Semantic Memory 不默认全量进入 prompt。

### 5.4 Archive / Blob Store

Archive / Blob Store 保存原文和大对象：

```text
完整会话
完整工具输出
大文档正文
附件
网页原文
原始 JSON response
压缩边界
```

Archive 不承担语义推理。GraphStore 只保存 archive metadata，例如 `@id`、hash、summary、storedAt、createdFrom。

### 5.5 Prompt Projection

Prompt Projection 是 prompt view，不是事实源。

它根据当前任务从 GraphStore、ArchiveStore、LoopState 中召回和压缩：

```text
MemoryProjection = selected memories + summaries + @id references + warnings
```

Projection 必须保留 `@id`，方便 agent 后续精确读取完整记忆。Projection 不允许被原样写回 Durable Semantic Memory。

## 6. Scope 模型

通用 Agent 不能只依赖 cwd / project root。V1 使用 `ctx:Scope` 作为记忆锚点。

### 6.1 Scope 类型

```text
ctx:UserScope
  用户长期偏好、稳定背景、沟通习惯。

ctx:AgentScope
  Agent 自身操作经验、角色设置、能力边界。

ctx:SessionScope
  当前会话。

ctx:TaskScope
  一个明确目标或任务链。

ctx:DomainScope
  长期主题，例如 Python agent OS、LLM theory、JSON-LD memory。

ctx:WorkspaceScope
  本地项目目录或 git repo，如果存在。

ctx:ArtifactScope
  文件、网页、PDF、notebook、issue、邮件线程等。

ctx:SkillScope
  某个 skill 或工具的使用经验。

ctx:TeamScope
  团队共享规则和协作记忆。
```

### 6.2 没有项目目录时怎么办

没有 cwd 时，默认 scope 不是空，而是：

```text
UserScope + AgentScope + SessionScope
```

如果对话围绕稳定主题持续展开，系统创建或复用 `DomainScope`。

例如：

```text
ctx:domain/jsonld-memory-system
ctx:domain/python-agent-os
ctx:domain/user-llm-learning
```

这解决了通用 Agent 没有项目依托的问题。

### 6.3 Scope Resolver

每轮开始运行 `ScopeResolver`。

输入：

```text
user message
session id
active task id
cwd / git root, if any
attached artifacts
recent tool calls
explicit scope words: "以后都这样"、"只在这个项目"、"这次会话"
```

输出：

```json
{
  "activeScopes": [
    "ctx:user/plumliu",
    "ctx:agent/default",
    "ctx:session/2026-06-04",
    "ctx:domain/jsonld-memory-system",
    "ctx:workspace/gliding-horse"
  ],
  "writeDefaultScope": "ctx:domain/jsonld-memory-system",
  "recallScopes": [
    "ctx:user/plumliu",
    "ctx:domain/jsonld-memory-system",
    "ctx:workspace/gliding-horse"
  ]
}
```

## 7. 核心 JSON-LD 本体

### 7.1 Namespace

建议 namespace：

```text
mem:   https://agent.example/memory#
ctx:   https://agent.example/context#
agt:   https://agent.example/agent#
task:  https://agent.example/task/
sess:  https://agent.example/session/
turn:  https://agent.example/turn/
art:   https://agent.example/artifact/
skill: https://agent.example/skill/
prov:  http://www.w3.org/ns/prov#
xsd:   http://www.w3.org/2001/XMLSchema#
```

### 7.2 基础字段

每个长期 memory node 必须有：

```text
@context
@id
@type
mem:scope
mem:statement
mem:createdAt
mem:updatedAt
mem:createdFrom
mem:confidenceLevel
mem:verificationStatus
mem:sourceAuthority
mem:status
```

建议有：

```text
mem:evidence
mem:expiresAt
mem:staleAfter
mem:lastVerifiedAt
mem:howToApply
mem:why
mem:supersedes
mem:contradicts
mem:relatedTask
mem:relatedSkill
mem:relatedArtifact
```

### 7.3 类型层级

```text
mem:Memory
  mem:Turn
  mem:ToolResult
  mem:Artifact
  mem:Evidence
  mem:Claim
    mem:Decision
  mem:Fact
    mem:WorldFact
    mem:ExperienceFact
  mem:Observation
  mem:MentalModel
    mem:UserProfile
    mem:AgentProfile
  mem:ActionBoundary
  mem:SkillMemory
  mem:SessionSummary
  mem:WorkingNote
```

### 7.4 关系谓词

```text
mem:scope
mem:createdFrom
mem:hasEvidence
mem:derivedFrom
mem:produced
mem:storedAs
mem:provides
mem:supports
mem:contradicts
mem:supersedes
mem:basedOn
mem:aboutPeer
mem:aboutTask
mem:aboutArtifact
mem:aboutSkill
mem:relatedTo
mem:appliesWhen
mem:doNotApplyWhen
mem:hasStatus
```

## 8. JSON-LD 示例

### 8.1 用户偏好

```json
{
  "@context": {
    "mem": "https://agent.example/memory#",
    "ctx": "https://agent.example/context#",
    "xsd": "http://www.w3.org/2001/XMLSchema#",
    "scope": { "@id": "mem:scope", "@type": "@id" },
    "createdFrom": { "@id": "mem:createdFrom", "@type": "@id" },
    "createdAt": { "@id": "mem:createdAt", "@type": "xsd:dateTime" },
    "updatedAt": { "@id": "mem:updatedAt", "@type": "xsd:dateTime" }
  },
  "@id": "mem:user/plumliu/preference/design-critique-20260604",
  "@type": ["mem:Memory", "mem:MentalModel", "mem:UserProfile"],
  "scope": "ctx:user/plumliu",
  "mem:statement": "用户更关心 Agent 系统的设计缺陷、记忆系统和 JSON-LD 底层对象，而不是代码整洁度。",
  "mem:howToApply": "讨论 Gliding Horse 或 Python 版 Agent 时，优先分析架构、语义对象、记忆治理和可查询性。",
  "createdFrom": "turn:2026-06-04/agent-memory-jsonld-discussion",
  "mem:confidenceLevel": "verified",
  "mem:verificationStatus": "user_confirmed",
  "mem:sourceAuthority": "explicit_user_instruction",
  "mem:status": "active",
  "createdAt": "2026-06-04T00:00:00Z",
  "updatedAt": "2026-06-04T00:00:00Z"
}
```

### 8.2 设计决策

```json
{
  "@context": {
    "mem": "https://agent.example/memory#",
    "ctx": "https://agent.example/context#",
    "task": "https://agent.example/task/",
    "xsd": "http://www.w3.org/2001/XMLSchema#",
    "scope": { "@id": "mem:scope", "@type": "@id" },
    "createdFrom": { "@id": "mem:createdFrom", "@type": "@id" },
    "aboutTask": { "@id": "mem:aboutTask", "@type": "@id" },
    "hasEvidence": { "@id": "mem:hasEvidence", "@type": "@id" },
    "createdAt": { "@id": "mem:createdAt", "@type": "xsd:dateTime" }
  },
  "@id": "mem:decision/jsonld-before-agent-loop",
  "@type": ["mem:Memory", "mem:Decision"],
  "scope": "ctx:domain/jsonld-memory-system",
  "aboutTask": "task:python-agent-memory-v1",
  "mem:statement": "在构建主 agent loop 之前，必须先定义 JSON-LD memory object、IRI、scope、RDF conversion 和 recall projection。",
  "mem:why": "否则多级记忆和 skill 系统会退化成字符串拼接，Oxigraph/SPARQL 无法发挥作用。",
  "mem:howToApply": "先实现 JSON-LD/RDF 底座，再实现 agent loop 和 provider adapter。",
  "hasEvidence": "mem:evidence/20260604/provider-research",
  "createdFrom": "turn:2026-06-04/provider-triage",
  "mem:confidenceLevel": "high",
  "mem:verificationStatus": "user_confirmed",
  "mem:sourceAuthority": "conversation_evidence",
  "mem:status": "active",
  "createdAt": "2026-06-04T00:00:00Z"
}
```

### 8.3 Action Boundary

```json
{
  "@context": {
    "mem": "https://agent.example/memory#",
    "ctx": "https://agent.example/context#",
    "xsd": "http://www.w3.org/2001/XMLSchema#",
    "scope": { "@id": "mem:scope", "@type": "@id" },
    "expiresAt": { "@id": "mem:expiresAt", "@type": "xsd:dateTime" }
  },
  "@id": "mem:boundary/gliding-horse/no-code-cleanliness-review",
  "@type": ["mem:Memory", "mem:ActionBoundary"],
  "scope": "ctx:workspace/gliding-horse",
  "mem:statement": "评估 Gliding Horse 时只谈设计想法，不谈代码整洁度。",
  "mem:appliesWhen": "用户要求分析该仓库的设计缺陷或愿景落地程度。",
  "mem:doNotApplyWhen": "用户明确要求 code review、重构或实现修复。",
  "mem:sourceAuthority": "explicit_user_instruction",
  "mem:confidenceLevel": "verified",
  "mem:verificationStatus": "user_confirmed",
  "mem:status": "active"
}
```

## 9. JSON-LD 到 RDF 的硬性要求

这是整个系统最关键的部分。

### 9.1 `@context` 必须展开 predicate

错误：

```text
input_file -> http://agent-os.org/prop/input_file
source_url -> http://agent-os.org/prop/source_url
data_path  -> http://agent-os.org/prop/data_path
```

正确：

```json
{
  "@context": {
    "input_file": "skill:sourceDataURI",
    "source_url": "skill:sourceDataURI",
    "data_path": "skill:sourceDataURI"
  }
}
```

最终 RDF predicate 必须相同：

```text
skill:sourceDataURI
```

这才是 JSON-LD 对 skill 参数互操作的价值。

### 9.2 `{"@id": "..."}` 必须变成 IRI edge

JSON-LD：

```json
{
  "@id": "mem:decision/1",
  "mem:createdFrom": { "@id": "turn:17" }
}
```

RDF：

```text
mem:decision/1 mem:createdFrom turn:17 .
```

禁止变成字符串 literal：

```text
"{\"@id\":\"turn:17\"}"
```

### 9.3 数组必须展开成多条 edge

JSON-LD：

```json
{
  "@id": "skill:jwt-auth",
  "skill:requires": [
    { "@id": "skill:rust-basic" },
    { "@id": "skill:http-middleware" }
  ]
}
```

RDF：

```text
skill:jwt-auth skill:requires skill:rust-basic .
skill:jwt-auth skill:requires skill:http-middleware .
```

### 9.4 typed literal 必须保留

JSON-LD：

```json
{
  "createdAt": "2026-06-04T00:00:00Z",
  "retrievalCount": 12
}
```

RDF 应保留：

```text
xsd:dateTime
xsd:float
```

否则时间查询、置信度排序、过期检查都会退化。

### 9.5 Named Graph 用于 scope 隔离

建议 named graph：

```text
graph:user/plumliu
graph:agent/default
graph:session/2026-06-04
graph:domain/jsonld-memory-system
graph:workspace/gliding-horse
graph:skill/jsonld-rdf-modeling
```

同一个实体可以被多个 scope 引用，但实体本体不应无意义复制。

## 10. 写入流程

### 10.1 写入入口

写入分三类：

```text
explicit write
  用户明确说“记住”“以后”“不要再”。

implicit candidate
  系统发现可能有长期价值，但只进入 candidate。

consolidated write
  session end / compaction / dreaming 后晋升。
```

### 10.2 MemoryWriteGate

每条记忆写入 Durable Semantic Memory 前必须经过 gate：

```text
1. 是否有 future utility
2. 是否可通过文件、git、工具实时获得
3. 是否用户明确确认
4. 是否涉及敏感信息
5. 是否有 scope
6. 是否有 evidence
7. 是否已有重复或相似记忆
8. 是否需要过期时间
9. 是否会改变未来行动
```

没有通过 gate 的非结论材料只能进入 Execution Evidence Ledger、Working Context Cache 或 SessionArchive，不能进入 Durable Semantic Memory。`Claim / Decision` 未通过 gate 时只能保留为 rejected / needs_review candidate，不能成为 active 结论。

### 10.3 写入管线

```text
MemoryCandidate
  -> classify type
  -> resolve scope
  -> attach evidence
  -> sensitive data scan
  -> duplicate / contradiction search
  -> build JSON-LD node
  -> JSON-LD expansion
  -> write DocumentStore
  -> write RDF quads to GraphStore
  -> update SearchIndex
  -> optional update prompt-facing working context view
```

### 10.4 Context Fencing

每轮 prompt 中注入的 recalled memory 必须带来源标记。

后台抽取时必须知道哪些内容来自：

```text
new user message
new assistant output
tool result
previously recalled memory
```

规则：

```text
previously recalled memory 不能原样再次写入。
```

只有当本轮产生了新的证据、用户确认、用户纠正或事实变化，才允许更新旧 memory。

## 11. 召回流程

### 11.1 候选生成

候选来源：

```text
SPARQL by scope/type/relation
BM25 / FTS
vector search
recent Execution Evidence Ledger state
SessionArchive search
explicit @id lookup
```

Hindsight 的启发是：不要迷信单一路径。

但 V1 的核心优先级应该是：

```text
scope + type + relation > semantic similarity
```

因为我们要充分发挥 JSON-LD/RDF 的优势。

### 11.2 SPARQL 查询示例

查询当前 domain 下的活跃设计决策：

```sparql
PREFIX mem: <https://agent.example/memory#>
PREFIX ctx: <https://agent.example/context#>

SELECT ?m ?statement ?why ?confidenceLevel WHERE {
  GRAPH <graph:domain/jsonld-memory-system> {
    ?m a mem:Decision ;
       mem:scope ctx:domain/jsonld-memory-system ;
       mem:statement ?statement ;
       mem:confidenceLevel ?confidenceLevel ;
       mem:status "active" .
    OPTIONAL { ?m mem:why ?why . }
  }
}
LIMIT 10
```

查询所有影响行动的边界：

```sparql
PREFIX mem: <https://agent.example/memory#>

SELECT ?m ?statement ?appliesWhen ?doNotApplyWhen WHERE {
  ?m a mem:ActionBoundary ;
     mem:statement ?statement ;
     mem:status "active" .
  OPTIONAL { ?m mem:appliesWhen ?appliesWhen . }
  OPTIONAL { ?m mem:doNotApplyWhen ?doNotApplyWhen . }
}
```

查询由某条 evidence 支撑的 observation：

```sparql
PREFIX mem: <https://agent.example/memory#>

SELECT ?obs ?statement WHERE {
  ?obs a mem:Observation ;
       mem:hasEvidence <mem:evidence/20260604/provider-research> ;
       mem:statement ?statement .
}
```

### 11.3 Rerank

排序信号：

```text
scope match
type relevance
relation distance
recency
confidenceLevel
verificationStatus
sourceAuthority
evidence strength
staleness risk
user explicit recall request
token cost
```

### 11.4 Projection

Projection 输出给 LLM 的不是完整 JSON-LD，而是可读摘要 + `@id`。

```json
{
  "@type": "mem:MemoryProjection",
  "mem:forRole": "planner",
  "mem:items": [
    {
      "@id": "mem:decision/jsonld-before-agent-loop",
      "@type": "mem:Decision",
      "mem:summary": "先实现 JSON-LD/RDF 记忆底座，再实现主 agent loop。",
      "mem:why": "否则记忆系统会退化成字符串拼接，SPARQL 无法发挥作用。",
      "mem:confidenceLevel": "high",
      "mem:verificationStatus": "user_confirmed"
    }
  ],
  "mem:warnings": [
    "不要把外部 provider 的全部术语引入核心本体。"
  ]
}
```

## 12. Consolidation / Dreaming

Consolidation 的任务不是“总结聊天记录”，而是把短期材料转化成高质量长期记忆。

### 12.1 输入

```text
Execution Evidence Ledger evidence and claim candidates
SessionSummary
user corrections
explicit remember requests
repeated recall hits
failed actions
tool result summaries
existing memories
```

### 12.2 输出

```text
promote candidate to Durable Semantic Memory
merge duplicates
create Observation from Facts
create MentalModel from repeated Observations
mark stale
mark contradicted
supersede old memory
request human review
delete low-value candidate
```

### 12.3 晋升规则

WorkingNote 晋升为 Durable Memory 前，至少满足其一：

```text
用户明确要求保存
用户纠正过 agent 行为
影响未来行动安全
跨 session 反复出现
是不易重新发现的背景信息
是已确认设计决策
是稳定偏好
是稳定 reference
```

### 12.4 Observation 生成规则

Observation 必须来自多个 evidence 或高权威 evidence。

```text
Observation = conclusion + evidence links + confidenceLevel + freshness
```

禁止无证据地产生用户画像。

## 13. 记忆治理

### 13.1 What To Save

应该保存：

```text
用户明确要求记住的内容
用户纠正 agent 的行为方式
跨会话有用的偏好
已确认的设计决策
外部系统入口和用途
不容易重新发现的项目背景
影响未来行动的边界条件
skill 使用经验和失败模式
```

### 13.2 What Not To Save

不应保存：

```text
可通过读文件、git、工具实时获得的信息
一次性闲聊
未经确认的敏感推断
原始日志和大段工具输出
临时路径和临时错误
API key、password、token、private key
已经稳定写在 README / AGENTS.md / 项目文档里的内容
召回出来的旧 memory 本身
```

### 13.3 Action Boundary

任何会改变未来行动的记忆都必须包含：

```text
appliesWhen
doNotApplyWhen
sourceAuthority
safeToActCondition
expiresAt or staleAfter
```

例如“不要自动提交代码”“只在这个项目使用某规则”“以后回答更简洁”都属于 action boundary。

### 13.4 Staleness

每条长期记忆必须支持：

```text
active
stale
superseded
contradicted
deleted
```

召回 stale memory 时，Projection 必须提醒 agent 需要验证。

## 14. 工具接口

V1 工具应该少而清楚：

```text
memory_search
  按 query + scope + type 检索候选。

memory_get
  通过 @id 获取完整 JSON-LD document。

memory_write
  写入候选或长期记忆。

memory_update
  merge / replace / supersede / mark stale。

memory_forget
  删除、redact 或标记 deleted / archived / stale。

memory_review
  查看待晋升、冲突、重复、过期记忆。

session_search
  查询完整历史，不默认进入 prompt。

memory_project
  为当前 role 和 token budget 生成 Projection。
```

工具返回必须包含：

```text
@id
@type
scope
statement / summary
confidenceLevel
verificationStatus
status
evidence count
freshness
source
```

## 15. Provider Adapter 原则

外部 provider 只能作为 adapter，不做 canonical store。

### 15.1 Adapter 的位置

```text
Canonical JSON-LD Memory
  -> optional provider adapter
  -> Mem0 / Supermemory / Honcho / Hindsight
```

反向写入时：

```text
Provider result
  -> normalize
  -> MemoryCandidate
  -> MemoryWriteGate
  -> JSON-LD Memory
```

provider 不能绕过 `MemoryWriteGate`。

### 15.2 允许的 adapter 类型

```text
ExtractionAdapter
  调用 Mem0 类服务抽取候选事实。

ProfileAdapter
  同步用户 profile，但必须转成 mem:UserProfile。

SearchAdapter
  把外部 provider 当召回候选来源。

ConsolidationAdapter
  调用外部模型做总结，但输出仍必须转成 JSON-LD。
```

### 15.3 Provider 不得污染核心本体

禁止：

```text
把每个 provider 的内部类型都加入 core schema
把 provider 的文本结果直接当长期事实
把 provider 当唯一真相源
绕过 evidence / scope / confidenceLevel / status
```

## 16. Python 版模块拆分

建议模块：

```text
memory/
  ontology.py
  context_registry.py
  iri.py
  jsonld_node.py
  jsonld_to_rdf.py
  document_store.py
  graph_store.py
  search_index.py
  session_archive.py
  scope_resolver.py
  write_gate.py
  extractor.py
  recall.py
  projection.py
  consolidation.py
  security.py
  tools.py
  providers/
    base.py
    mem0_adapter.py
    honcho_adapter.py
    supermemory_adapter.py
    hindsight_adapter.py
```

核心依赖：

```text
rdflib
pyld
sqlite / duckdb / postgres
tantivy / sqlite fts5
embedding provider, optional
```

### 16.1 关键接口

```python
class JsonLdMemoryNode:
    id: str
    types: list[str]
    scope: str
    statement: str
    evidence: list[str]
    confidence_level: str
    verification_status: str
    source_authority: str
    status: str


class GraphStore:
    def put_jsonld(self, node: JsonLdMemoryNode) -> None: ...
    def sparql(self, query: str) -> list[dict]: ...


class MemoryWriteGate:
    def evaluate(self, candidate: "MemoryCandidate") -> "WriteDecision": ...


class MemoryRecall:
    def recall(self, query: str, scopes: list[str], types: list[str], budget: int) -> "MemoryProjection": ...
```

## 17. 最小验收测试

### 17.1 JSON-LD / RDF 测试

```text
test_context_alias_collapse
  input_file/source_url/data_path 展开为同一 predicate。

test_nested_id_edge
  {"@id": "..."} 生成 IRI edge，不 stringify。

test_array_edges
  数组生成多条 triple。

test_typed_literals
  时间、数字、boolean 保留 datatype。

test_named_graph_scope
  不同 scope 写入不同 named graph。
```

### 17.2 记忆治理测试

```text
test_no_save_derivable_code_structure
  可实时读取的信息不进 Durable Semantic Memory。

test_context_fencing
  召回 memory 不会被原样再次写入。

test_action_boundary_required_fields
  ActionBoundary 缺少 appliesWhen / doNotApplyWhen / sourceAuthority 时拒绝写入。

test_sensitive_data_block
  token/private key/API key 不写入长期记忆。

test_duplicate_detection
  相似记忆合并或 supersede，不重复堆积。
```

### 17.3 召回测试

```text
test_recall_by_scope_and_type
  同一 query 在不同 scope 下召回不同记忆。

test_sparql_relation_recall
  能通过 evidence、task、skill、artifact 关系召回。

test_projection_keeps_id
  Projection 保留 @id。

test_stale_warning
  stale memory 进入 Projection 时带验证提醒。
```

### 17.4 Consolidation 测试

```text
test_working_note_not_auto_durable
  WorkingNote 不自动进入长期记忆。

test_observation_requires_evidence
  Observation 必须有 evidence。

test_session_end_extracts_candidates
  session 结束只产生 candidate，仍需 gate。
```

## 18. 对 Gliding Horse 的改造重点

当前最优先不是加更多记忆层，而是修正 JSON-LD 到 Oxigraph 的语义路径。

### 18.1 第一优先级

```text
替换拍扁式 build_triples
引入 JSON-LD expansion
保留 @context predicate mapping
保留 nested @id edge
保留 array edge
保留 typed literal
使用 named graph 表达 scope
```

### 18.2 第二优先级

```text
定义 MemoryType / Scope / Evidence / Status
为 memory write 增加 gate
实现 context fencing
实现 memory projection
```

### 18.3 第三优先级

```text
实现 consolidation / dreaming
接入 hybrid search
实现 provider adapter
实现 memory review UI 或 CLI
```

## 19. 一句话架构

最终系统应满足：

```text
记忆不是聊天记录。
记忆不是向量库 top-k。
记忆不是 Markdown 摘要。
记忆也不是带 @ 符号的 JSON 套壳。

记忆是有身份、有类型、有作用域、有证据、有生命周期的 JSON-LD 语义对象。
JSON-LD 文档保存对象，RDF quads 保存事实，SPARQL 查询关系，Projection 控制 prompt。
```

这才是 JSON-LD 在 Agent 记忆系统中真正值得使用的地方。
