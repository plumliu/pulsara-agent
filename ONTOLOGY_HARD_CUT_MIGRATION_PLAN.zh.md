# Pulsara Ontology 硬切迁移方案

## 0. 文档定位

本文档定义从当前单一 `mem:*` ontology，迁移到收敛版四 ontology 的实施方案：

```text
mem:* = agent should remember / believe / obey
ctx:* = where it applies
rt:*  = what happened / evidence exists
cap:* = what the system can do / how it is governed
```

本迁移是硬切，不做旧实现的前向兼容：

```text
不继续把旧 mem:RunTimeline 当作 rt:RunTimeline 读取。
不继续把旧 mem:ToolResult 当作 rt:ToolResult 读取。
不继续把旧 mem:Artifact / mem:Evidence / mem:Turn 当作 runtime evidence 类型读取。
不在 read-side 同时查询旧类型和新类型。
不为旧图数据写兼容 adapter。
```

如果本地或测试 Oxigraph 中有旧 `mem:*` runtime/evidence 图数据，应删除旧 named graph 或运行一次性迁移脚本。生产环境若已有不可丢弃数据，必须在硬切前先做离线迁移；硬切后的应用代码不承担兼容读取责任。

本文档不要求一次提交完成所有迁移。它要求每一刀都朝硬切目标前进，不引入双写、双读或 fallback。

## 1. 当前代码状态

当前代码中，`src/pulsara_agent/ontology/memory.py` 同时承载了四类概念：

```text
runtime evidence:
  Turn
  RunTimeline
  ToolResult
  Artifact
  Evidence
  EventSpan
  sourceSession/sourceRun/sourceTurn/sourceReply/startSequence/endSequence

durable memory:
  Claim
  Decision
  status/confidence/verification/sourceAuthority

context:
  scope 字段和 ctx: 前缀

capability/policy:
  暂未独立建模
```

当前实体写入点集中在：

```text
src/pulsara_agent/memory/entities/
  artifact.py
  claim.py
  evidence.py
  tool_result.py
  turn.py

src/pulsara_agent/memory/foundation/provenance.py
src/pulsara_agent/memory/canonical/ledger.py
src/pulsara_agent/memory/hooks/run_timeline_persistence.py
src/pulsara_agent/memory/foundation/run_timeline_query.py
```

当前 GraphStore 基础设施的主要约束：

```text
OxigraphGraphStore 写入时使用 document["@context"] 展开。
OxigraphGraphStore 读回、find_by_type、compact IRI/predicate/type 时硬编码 memory.CONTEXT。
InMemoryGraphStore find_by_type 只按 compact type name 匹配。
```

因此，真正的第一风险不是新增 ontology module，而是 `OxigraphGraphStore` 还不是 ontology-agnostic。

## 2. 目标 ontology

### 2.1 `mem:*`

`mem` 只放长期语义记忆和结论。它回答：

```text
agent 应该记住什么？
agent 应该相信什么？
agent 未来应该遵守什么？
```

核心类型：

```text
mem:Claim
mem:Decision
mem:Preference
mem:ActionBoundary
mem:Observation
```

核心字段/关系：

```text
mem:statement
mem:summary
mem:scope
mem:hasEvidence
mem:supports
mem:contradicts
mem:supersedes
mem:derivedFrom
mem:confidenceLevel
mem:verificationStatus
mem:sourceAuthority
mem:status
mem:gateReason
mem:appliesWhen
mem:doNotApplyWhen
mem:staleAfter
mem:expiresAt
mem:createdAt
mem:updatedAt
```

保留在 `mem` 的枚举：

```text
NodeStatus
SourceAuthority
VerificationStatus
ConfidenceLevel
```

原因：这些枚举服务于 durable memory 和 conclusion lifecycle，而不是 runtime event 本身。

### 2.2 `ctx:*`

`ctx` 只放 scope 和 context anchor。它回答：

```text
这条记忆或证据适用于哪里？
```

核心类型：

```text
ctx:Scope
ctx:UserScope
ctx:AgentScope
ctx:SessionScope
ctx:TaskScope
ctx:DomainScope
ctx:WorkspaceScope
ctx:ArtifactScope
ctx:SkillScope
ctx:TeamScope
```

核心字段/关系：

```text
ctx:scopeKind
ctx:scopeLabel
ctx:scopeKey
ctx:parentScope
ctx:contains
ctx:activeIn
ctx:workspaceRoot
ctx:gitRemote
ctx:domainSlug
```

第一轮迁移可以只定义 terms，不要求立刻创建 scope entity。已有业务仍可使用 `"ctx:..."` 字符串作为 `mem:scope` / `rt:scope` 的对象值。

### 2.3 `rt:*`

`rt` 放 runtime evidence ledger 和 artifact metadata。它回答：

```text
发生过什么？
有什么证据？
证据在 EventLog / ArtifactStore 的哪里？
```

核心类型：

```text
rt:RunTimeline
rt:Turn
rt:ToolResult
rt:Artifact
rt:Evidence
rt:EventSpan
rt:EvalRun
rt:Judgment
```

第一轮必须迁移的类型：

```text
rt:RunTimeline
rt:Turn
rt:ToolResult
rt:Artifact
rt:Evidence
rt:EventSpan
```

后续才接入的类型：

```text
rt:EvalRun
rt:Judgment
```

核心字段/关系：

```text
rt:produced
rt:storedAs
rt:storedAt
rt:hash
rt:itemCount
rt:toolName
rt:inputSummary
rt:outputSummary
rt:truncated
rt:sourceType
rt:sourceEvent
rt:eventSpan
rt:sourceSession
rt:sourceRun
rt:sourceTurn
rt:sourceReply
rt:startSequence
rt:endSequence
rt:observedAt
rt:createdAt
rt:updatedAt
rt:status
rt:scope
```

Runtime-specific 枚举迁移到 `rt`：

```text
ToolExecutionStatus
EvidenceSourceType
```

### 2.4 `cap:*`

`cap` 放 skill/tool/plugin/policy 能力图。它回答：

```text
系统会什么？
工具能做什么？
策略如何约束能力？
```

核心类型：

```text
cap:Skill
cap:Tool
cap:Plugin
cap:Policy
```

核心字段/关系：

```text
cap:version
cap:versionOf
cap:supersedes
cap:providesTool
cap:providesSkill
cap:requires
cap:hasInputSchema
cap:hasOutputSchema
cap:allowedInScope
cap:blockedInScope
cap:sourceDataURI
```

第一轮只定义 ontology，不接 runtime 业务。

## 3. 统一上下文策略

硬切迁移前必须引入统一 ontology context。建议新增：

```text
src/pulsara_agent/ontology/context.py
src/pulsara_agent/ontology/runtime.py
src/pulsara_agent/ontology/capability.py
src/pulsara_agent/ontology/registry.py
```

`registry.py` 输出：

```python
CORE_CONTEXT: dict[str, Any]
```

`CORE_CONTEXT` 合并：

```text
mem prefix and terms
ctx prefix and terms
rt prefix and terms
cap prefix and terms
graph prefix
event prefix
```

所有 JSON-LD entity 的 `CONTEXT` 改为使用 `CORE_CONTEXT` 或对应 ontology module re-export 的合并 context。

禁止：

```text
GraphStore 内部继续硬编码 memory.CONTEXT。
实体各自拼局部 context 后依赖读侧猜测。
新旧 context 双轨运行。
```

## 4. GraphStore 迁移

### 4.1 OxigraphGraphStore

必须先让 `OxigraphGraphStore` ontology-agnostic。

改动目标：

```text
OxigraphGraphStore(default_context=CORE_CONTEXT)

put_jsonld(document)
  写入仍优先使用 document["@context"]，缺失时使用 default_context。

get_jsonld(node_id)
  使用 default_context expand node_id。
  返回文档 @context 使用 default_context。

has_jsonld(node_id)
  使用 default_context expand node_id。

find_by_type(type_name)
  优先使用 type_name.value 或 type_name.iri.value。
  不再用 type_name.name + memory.CONTEXT 猜测 type IRI。

_compact_iri / _compact_type / _compact_predicate
  使用 default_context。

_expand_graph_id
  graph: 仍映射到 https://pulsara.dev/graph/...
  其他 compact IRI 使用 default_context。
```

验收：

```text
能写入 rt:RunTimeline 并 find_by_type(rt.RUN_TIMELINE)。
能写入 mem:Claim 并 find_by_type(mem.CLAIM)。
能写入 cap:Tool 并 find_by_type(cap.TOOL)。
get_jsonld 读回后 @type compact name 稳定。
storedAs/sourceEvent/produced/supports 等 @id edge 仍是 {"@id": "..."}。
```

### 4.2 InMemoryGraphStore

当前 `find_by_type` 只按 compact name 匹配。硬切后可以短期保留这个行为，因为四 ontology 中当前没有同名核心类型。

但为了避免未来歧义，应改为支持：

```text
type_name.name
type_name.value
compact IRI
```

推荐规则：

```text
如果 doc["@type"] 中包含 type_name.name，匹配。
如果 doc["@type"] 中包含 type_name.value，匹配。
如果 doc["@type"] 中包含 compact IRI 且可由 CORE_CONTEXT 展开为 type_name.value，匹配。
```

## 5. Entity 迁移

### 5.1 Runtime evidence entities

迁移前：

```text
memory.entities.Turn       -> mem:Turn
memory.entities.ToolResult -> mem:ToolResult
memory.entities.Artifact   -> mem:Artifact
memory.entities.Evidence   -> mem:Evidence
RunTimelineRecord          -> mem:RunTimeline
RuntimeEventSpan           -> mem:EventSpan fields
```

迁移后：

```text
memory.entities.Turn       -> rt:Turn
memory.entities.ToolResult -> rt:ToolResult
memory.entities.Artifact   -> rt:Artifact
memory.entities.Evidence   -> rt:Evidence
RunTimelineRecord          -> rt:RunTimeline
RuntimeEventSpan           -> rt:EventSpan fields
```

实体文件可以先保持路径不变，避免同时做目录重构：

```text
src/pulsara_agent/memory/entities/tool_result.py
```

但 imports 应从：

```python
from pulsara_agent.ontology import memory
```

切换为：

```python
from pulsara_agent.ontology import runtime as rt
from pulsara_agent.ontology import memory as mem
```

runtime evidence 实体只使用 `rt` terms；只有 claim/decision 使用 `mem` terms。

### 5.2 Claim / Decision

当前只有 `Claim` entity。硬切时：

```text
Claim 保留 mem:Claim。
Decision 新增 mem:Decision entity 或扩展 Claim entity 支持 TYPE override。
```

第一轮可以不实现独立 Decision 行为，但 `mem:Decision` term 应保留在 `memory.py`。

### 5.3 Preference / ActionBoundary / Observation

新增 terms：

```text
mem:Preference
mem:ActionBoundary
mem:Observation
```

第一轮可以只加 ontology term 和 context，不要求立刻写入。真正写入必须等 `MemoryWriteGate` 支持对应规则。

## 6. Read-side 迁移

### 6.1 RunTimeline

迁移前：

```python
graph.find_by_type(memory.RUN_TIMELINE)
record[memory.STORED_AS.name]
record[memory.SOURCE_RUN.name]
record[memory.SOURCE_SESSION.name]
```

迁移后：

```python
graph.find_by_type(rt.RUN_TIMELINE)
record[rt.STORED_AS.name]
record[rt.SOURCE_RUN.name]
record[rt.SOURCE_SESSION.name]
```

不做：

```text
graph.find_by_type(rt.RUN_TIMELINE) or graph.find_by_type(memory.RUN_TIMELINE)
```

旧数据读不到是硬切预期行为。

### 6.2 ExecutionEvidenceLedger

迁移前：

```python
graph.find_by_type(memory.TOOL_RESULT)
graph.find_by_type(memory.EVIDENCE)
graph.find_by_type(memory.CLAIM)
```

迁移后：

```python
graph.find_by_type(rt.TOOL_RESULT)
graph.find_by_type(rt.EVIDENCE)
graph.find_by_type(mem.CLAIM)
```

关系也要分层：

```text
rt:ToolResult rt:provides rt:Evidence
rt:Evidence mem:supports mem:Claim
```

也可以将 `supports` 保留在 `mem`，因为它表达 evidence 支持 claim 的语义关系。推荐规则：

```text
runtime provenance edge 用 rt:*。
conclusion relation edge 用 mem:*。
```

因此：

```text
Turn -> ToolResult
  rt:produced

ToolResult -> Evidence
  rt:provides

Evidence -> Claim
  mem:supports
```

## 7. Event model 迁移

`event/events.py` 当前 `MemoryEventBase` 引用：

```python
memory.SourceAuthority
memory.VerificationStatus
```

这两个枚举应继续留在 `mem` ontology，因此事件模型不需要大改，只需 import alias：

```python
from pulsara_agent.ontology import memory as mem
```

事件名 `MEMORY_*` 不变。

Runtime event stream 不需要因 ontology 硬切改变 payload schema，除非事件 payload 中直接存了 memory type compact name。对于 `memory_type: str` 字段，硬切后应写新类型名，例如：

```text
Claim
Decision
Preference
ActionBoundary
Observation
```

而不是 runtime evidence 类型。

## 8. 测试迁移

必须同步更新测试，不做兼容断言。

### 8.1 新增基础测试

新增或扩展：

```text
tests/test_ontology_registry.py
  CORE_CONTEXT 包含 mem/ctx/rt/cap prefix。
  CORE_CONTEXT 中无冲突 term。
  rt.RUN_TIMELINE.value 是 https://pulsara.dev/runtime#RunTimeline。
  mem.CLAIM.value 是 https://pulsara.dev/memory#Claim。

tests/test_oxigraph_graph_store_unit.py
  put_jsonld(rt entity) 使用 rt IRI。
  find_by_type(rt.RUN_TIMELINE) 使用 rt IRI。
  get_jsonld 能 compact rt predicate。

tests/test_oxigraph_graph_store.py
  rt:ToolResult / rt:RunTimeline round-trip。
```

### 8.2 更新现有测试

需要更新：

```text
tests/test_execution_evidence_ledger.py
  ToolResult / Artifact / Evidence / Turn -> rt。
  Claim -> mem。

tests/test_runtime_timeline.py
  RunTimelineRecord -> rt。

tests/test_runtime_wiring.py
  durable wiring 查 rt.RUN_TIMELINE。

tests/test_oxigraph_graph_store.py
  ledger provenance 使用 rt terms。

tests/test_real_llm_integration.py
  durable real LLM timeline 查 rt.RUN_TIMELINE。

tests/test_agent_runtime_loop.py
  ExecutionEvidencePersistenceHook 查 rt.TOOL_RESULT / rt.EVIDENCE。
```

不写：

```text
assert old mem type still works
```

### 8.3 验证命令

推荐验证：

```text
uv run pytest tests/test_ontology_registry.py tests/test_oxigraph_graph_store_unit.py -q
uv run pytest tests/test_execution_evidence_ledger.py tests/test_runtime_timeline.py -q
uv run pytest tests/test_oxigraph_graph_store.py tests/test_runtime_wiring.py -q
PULSARA_RUN_REAL_LLM=1 uv run pytest tests/test_real_llm_integration.py::test_real_llm_trajectory_suite_collects_five_rollouts -q -s
uv run pytest -q
uv run ruff check
git diff --check
```

## 9. 推荐提交切分

### Commit 1: ontology registry 与 GraphStore 基础设施

内容：

```text
新增 ontology/context.py
新增 ontology/runtime.py
新增 ontology/capability.py
新增 ontology/registry.py
memory.py 收缩/整理但暂不迁实体
OxigraphGraphStore 改用 CORE_CONTEXT
InMemoryGraphStore find_by_type 支持 IRI-aware matching
新增 ontology registry 和 graph store 单测
```

不做：

```text
不迁 RunTimeline / ToolResult。
不新增 ActionBoundary 写入。
不改 MemoryWriteGate 行为。
```

### Commit 2: runtime evidence 硬切到 `rt:*`

内容：

```text
Turn / ToolResult / Artifact / Evidence / RunTimelineRecord 改用 rt terms。
RuntimeEventSpan 改用 rt source/eventSpan terms。
RunTimeline read-side 改查 rt.RUN_TIMELINE。
ExecutionEvidenceLedger 关系改为 rt:produced / rt:provides + mem:supports。
更新全部相关测试。
```

不做：

```text
不双读旧 mem runtime 类型。
不保留 memory.RUN_TIMELINE alias。
不保留 memory.TOOL_RESULT alias。
```

### Commit 3: durable memory 类型补齐

内容：

```text
mem:Preference
mem:ActionBoundary
mem:Observation
对应 entity skeleton
MemoryWriteGate 类型化入口
ActionBoundary required fields test
Preference / Observation gate tests
```

不做：

```text
不让小模型直接写 active memory。
不接 projection 全量逻辑。
```

### Commit 4: cap ontology skeleton

内容：

```text
cap:Skill
cap:Tool
cap:Plugin
cap:Policy
basic JSON-LD entity or fixture tests
```

不做：

```text
不接 skill extraction。
不接 tool registry projection。
```

## 10. 数据处理策略

本迁移不前向兼容旧实现，因此数据策略必须明确。

### 10.1 开发/测试环境

直接删除旧 named graph：

```text
graph:test/*
graph:runtime/*
graph:default, if it contains old experimental data
```

测试继续使用唯一 graph id 并在 finally 清理。

### 10.2 本地 durable trajectory

Postgres EventLog 和 ArtifactStore 不需要迁移，因为它们不保存 GraphStore ontology 类型作为 canonical semantic truth。

Oxigraph 中旧 semantic graph 数据如果重要，执行一次性脚本：

```text
读旧 mem:RunTimeline / mem:ToolResult / mem:Artifact / mem:Evidence / mem:Turn
重写为 rt:RunTimeline / rt:ToolResult / rt:Artifact / rt:Evidence / rt:Turn
删除旧 graph 或旧 subject
```

应用代码不内置该迁移逻辑。

### 10.3 生产环境

硬切前必须选择：

```text
停机迁移旧 graph 数据
或删除旧 graph 重新从 Postgres EventLog / ArtifactStore 重建 runtime evidence graph
```

硬切后不允许：

```text
读侧 fallback 到 mem:RunTimeline
写侧同时写 mem 和 rt
```

## 11. 风险与防线

### 11.1 Context drift

风险：

```text
不同 entity 使用不同 @context，Oxigraph 读回 compact 不稳定。
```

防线：

```text
统一 CORE_CONTEXT。
GraphStore 只依赖 CORE_CONTEXT compact。
单测覆盖 mem/rt/cap mixed graph round-trip。
```

### 11.2 Type name collision

风险：

```text
不同 ontology 出现同名 type，InMemoryGraphStore 按 name 匹配误命中。
```

防线：

```text
InMemoryGraphStore 支持 IRI-aware type matching。
新增 registry 测试检查核心 type name 冲突。
若未来必须重名，测试必须使用 full IRI。
```

### 11.3 Read-side missed records

风险：

```text
RunTimeline read-side 改 rt 后旧记录读不到。
```

这是硬切预期，不是 bug。

防线：

```text
测试只创建新 rt records。
文档明确旧图需删除或离线迁移。
```

### 11.4 Relation namespace confusion

风险：

```text
supports/provides/produced 关系分层不清。
```

规则：

```text
runtime provenance edge 用 rt:*。
durable conclusion relation 用 mem:*。
scope/context relation 用 ctx:*。
capability relation 用 cap:*。
```

### 11.5 Over-fragmentation

风险：

```text
每个概念都新增类型，图变成 ontology junk drawer。
```

防线：

```text
新增 type 必须带来新的 query、projection、gate 或 lifecycle 行为。
否则用现有类型 + scope + relation + facet 字段表达。
```

## 12. 完成标准

迁移完成后，系统应满足：

```text
GraphStore 可以同时写入/读回 mem、ctx、rt、cap ontology 的节点。

RunTimelineRecord 是 rt:RunTimeline。

ToolResult 是 rt:ToolResult。

Artifact metadata 是 rt:Artifact。

Evidence 是 rt:Evidence。

Claim / Decision 仍是 mem:Claim / mem:Decision。

RunTimeline read-side 只查 rt.RUN_TIMELINE。

ExecutionEvidenceLedger 只写 rt runtime evidence 和 mem conclusion。

OxigraphGraphStore 不再 import memory.py 作为唯一 context source。

测试中不再断言 runtime evidence 属于 mem namespace。

没有旧 mem runtime/evidence 类型 fallback。
```

最终命名空间边界：

```text
mem:*   长期语义记忆和结论。
ctx:*   scope/context。
rt:*    runtime evidence ledger 和 artifact metadata。
cap:*   skill/tool/plugin/policy 能力图。
```

这次迁移的目标不是“让类型更多”，而是让图里的语义边界变清楚：发生过什么属于 `rt`，适用于哪里属于 `ctx`，未来应记住/相信/遵守什么属于 `mem`，系统能做什么以及被什么策略约束属于 `cap`。
