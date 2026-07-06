# Runtime Storage Architecture

## 当前选型

Pulsara 初版生产级存储分层采用：

```text
EventLog       -> PostgreSQL
ArtifactStore  -> PostgreSQL
GraphStore     -> Oxigraph
```

这不是“两个数据库都存一份记忆”，而是把运行事实和语义事实拆开。

## PostgreSQL: runtime truth

PostgreSQL 负责可审计、可 replay、可恢复的运行事实：

```text
sessions
runs
turns
agent_events
tool_execution_records
artifacts
```

它回答的问题是：

```text
发生过什么？
事件原文是什么？
工具输出原文在哪里？
能否按 sequence 完整 replay？
run 失败或中断后能否恢复？
```

PostgreSQL 不负责长期语义推理，也不保存 canonical semantic memory。

## Oxigraph: semantic truth

Oxigraph 负责 JSON-LD/RDF/SPARQL 语义层：

```text
Claim
Evidence
Decision
Preference
ActionBoundary
Skill
Tool
ProjectionPolicy
MemoryPolicyVersion
```

以及这些语义关系：

```text
supersedes
contradicts
derivedFrom
supports
createdFrom
storedAs
```

它回答的问题是：

```text
什么值得长期记住？
哪些证据支撑这个结论？
哪些结论过期、被替代或相互矛盾？
当前 scope 下 projection 应召回什么？
```

## 二者如何连接

Oxigraph 中的语义节点只引用 PostgreSQL runtime truth 的稳定 ID：

```text
claim:<id>
  prov:wasDerivedFrom event:<event_id>
  mem:storedAs artifact:<artifact_id>
```

PostgreSQL 保存事件和 artifact 原文；Oxigraph 保存语义节点、关系和 provenance link。

## 硬边界

1. EventLog 只回答“发生过什么”，不回答“什么应该被长期相信”。
2. GraphStore 只保存语义节点和 provenance link，不保存完整事件流或大对象原文。
3. ArtifactStore 初版可以用 PostgreSQL 表实现，未来迁移到 S3/R2/local blob 时，GraphStore 仍只引用 `artifact:<id>`。
4. JSON-LD 进入 Oxigraph 后必须保留 RDF 语义：named graph、IRI edge、typed literal、array edge 和 blank node 不能被拍扁成 JSON 字符串。

## 当前落地状态

已落地：

```text
OxigraphGraphStore
InMemoryEventLog
InMemoryArchiveStore
RunTimeline persistence/query
PostgreSQL runtime truth schema
local docker compose for oxigraph/postgres
```

下一步应落地：

```text
PostgresEventLog
PostgresArtifactStore
Oxigraph-backed projection smoke
```

其中优先级最高的是 `PostgresEventLog`，因为它会把 runtime replay 从测试内存对象推进到可恢复的生产运行事实层。
