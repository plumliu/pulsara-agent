# Memory Surfaces 写回边界契约

_Created: 2026-06-27_
_Rewritten: 2026-06-28_

这份文档定义 Pulsara memory surfaces 的长期硬契约。它不是实现计划，而是用来冻结三类边界：

1. **每个 surface 的 truth source 是什么**
2. **每个 surface 允许写到哪里**
3. **每个 surface 绝对不能回写到哪里**

这版重写有两个目的：

- 把 **governed canonical memory graph** 与 **runtime semantic graph** 明确拆开，避免“只有 governance 能写 graph”这类过宽说法与 runtime semantic node 写入打架。
- 让契约口径与 [OXIGRAPH_CANONICAL_GRAPH_MIGRATION_PLAN.zh.md](/Users/plumliu/Desktop/python_workspace/pulsara_agent/OXIGRAPH_CANONICAL_GRAPH_MIGRATION_PLAN.zh.md) 对齐：Postgres-first intent commit、同步/异步面分离、hot-path 不读 Oxigraph。

---

## 0. 术语边界

### 0.1 governed canonical memory graph

本文中的 **governed canonical memory graph**，特指被 memory governance 管理的长期记忆子图，包括：

- `Claim`
- `Preference`
- `Observation`
- `Decision`
- `ActionBoundary`

它的特点是：

- 只有 governance 能把新节点升级进来；
- 每条写入都要求强 provenance；
- 会被 recall / relatedness / lifecycle / contradiction / supersede 消费。

### 0.2 runtime semantic graph

本文中的 **runtime semantic graph**，特指运行时语义节点和关系，例如：

- `RunTimelineRecord`
- `ToolResult`
- `Artifact`
- `Turn`

它的特点是：

- 它不是 governed memory；
- 它不通过 memory governance 决策进入；
- 它可以被 timeline / evidence / diagnostics 等路径写入；
- 它与 governed memory graph 共享语义存储 substrate，但不共享“只有 governance 才能写”的规则。

### 0.3 canonical graph

若本文使用 **canonical graph** 这个泛称，默认指：

- governed canonical memory graph
- runtime semantic graph

这两者的并集。

但涉及“唯一写入口”“governance 决策”“candidate 升级”时，本文会优先使用更窄的 **governed canonical memory graph**，避免歧义。

---

## 1. 核心立场

### 1.1 governed canonical memory graph 的唯一写入口

> **governed canonical memory graph 的唯一写入口是 governance。**

也就是说：

- canonical memory 写只走 `MemoryGovernanceExecutor.apply_decision`
- 下游 canonical memory materialization 只走 `MemoryWriteService.submit`
- 任何其它 surface 都不得直接把内容写成 governed memory node

这条边界是 substrate-independent 的。无论 governed memory 最终物理上落在 Postgres 还是 Oxigraph，这条规则都不变。

### 1.2 runtime semantic graph 不受 1.1 约束

运行时语义节点不是 governed memory，因此不受“只有 governance 才能写”的限制。

例如：

- `RunTimelinePersistenceHook` 写 `RunTimelineRecord`
- `ExecutionEvidenceLedger` 写 runtime semantic provenance nodes

这些都属于**合法的 runtime semantic graph writes**，不是对 1.1 的违反。

### 1.3 每条 governed memory 写都必须有原子可审计 provenance

> **每条 governed memory 写都必须先在 Postgres 事务中留下 decision / mutation provenance，再允许异步物化。**

硬约束：

- 不允许出现“语义图里有 governed memory 节点，但 Postgres 没有对应 decision / mutation 记录”
- 写次序永远是 **Postgres-intent-first**
- 不允许先写 Oxigraph 再补 Postgres provenance

### 1.4 recall / relatedness 的 hot-path 只读 Postgres substrate

本文冻结：

- recall 不直接读 canonical truth
- governance relatedness 不直接读 Oxigraph
- same-batch dedupe / lifecycle validation 不直接读 Oxigraph

这里要进一步区分两类 Postgres 读面：

- **同步面**
  - `memory_nodes`
  - `memory_relations`
  - transaction-local staged docs / projection rows
- **异步 recall 读面**
  - `memory_search_index`

冻结口径如下：

- recall 可以读同步面，也可以读 `memory_search_index`
- governance relatedness / same-batch dedupe / lifecycle validation 只读同步面
- `memory_search_index` 不提升为 governance validation truth source

### 1.5 投影 surface 永不升级成 governed memory

以下 surface 都是投影，不是 governed memory：

- working_context
- recall projection
- recall degraded payload
- run timeline summary

它们绝不能绕过 governance 回写成 canonical memory。

---

## 2. surface 总表

| surface | truth source | allowed write targets | forbidden write-back |
|---|---|---|---|
| working_context | run timeline summary | `working_context_summaries` | candidate pool / governed memory / event log |
| recall | Postgres recall projection (`memory_nodes` / `memory_relations` / `memory_search_index`) | `recall_traces` / `recall_usages` | candidate pool / governed memory |
| reflection | current run trace + safe-point | candidate pool(origin=REFLECTION) + reflection events | governed memory |
| run timeline | event log projection | timeline artifact + runtime semantic graph `RunTimelineRecord` | event log / candidate pool / working_context / governed memory |
| recall degraded | backend unavailable fact | none | 任何把模型导回旧检索路径的自由文本 fallback |

补一句最重要的：

> 上表里的 **governed memory** 与 **runtime semantic graph** 不是一回事。

---

## 3. working_context

**定位**：operational cache，不是长期记忆。

- **truth source**：run timeline summary，由 `build_run_timeline(...)` + `summarize_run_timeline(...)` 推出。
- **允许写**：只允许写 `working_context_summaries`。
- **禁止回写**：
  - candidate pool
  - governed memory
  - event log
- **守护**：
  - prompt 注入块固定带 `do_not_write_back="true"`
  - `projection_kind="working_context"`

working_context 可以重算、可以过期、可以被替换；丢失它不会损坏 canonical truth。

---

## 4. recall

**定位**：从 canonical memory 的 Postgres 派生投影中检索已有记忆，并把结果投影到当前轮。

### 4.1 truth source

recall 的 truth source 不是 canonical truth 本身，而是它的 **Postgres read-optimized projection**：

- `memory_nodes`
- `memory_relations`
- `memory_search_index`

这条规则在迁移前后都成立：

- 今天 canonical truth 仍主要来自 Postgres `graph_documents`
- 迁移后 governed canonical memory 的物理真源会变成 “Postgres mutation journal + Oxigraph materialization”
- 但 recall 仍只读 Postgres projection，不直接读 Oxigraph
- 这条 recall 读面定义不外溢到 governance target validation；后者仍只认同步面

### 4.2 allowed write targets

recall 只允许写：

- `recall_traces`
- `recall_usages`

### 4.3 forbidden write-back

recall 绝不能把被召回内容再回写成新 memory candidate 或 governed memory。

### 4.4 echo guard

防回写污染必须存在两层：

1. `ProjectionLedger` 把本轮 surfaced 的 `memory_id` + snippet 指纹记录进 scratchpad
2. recall projection block 带 `do_not_write_back="true"`

---

## 5. reflection

**定位**：safe-point 上的候选提案器。

### 5.1 truth source

reflection 读的是：

- 当前 run 的 user / assistant / tool trace
- safe-point 触发条件

### 5.2 allowed write targets

reflection 只允许写：

- `memory_candidates`（origin=`REFLECTION`）
- reflection completion / failure events

### 5.3 forbidden write-back

reflection 不得直接写 governed canonical memory graph。

也就是说：

- reflection 可以“提案”
- governance 决定是否“记住”

candidate pool 是提案箱，不是 canonical memory 本身。

---

## 6. run timeline

**定位**：event log 的唯一 run-level business view。

### 6.1 truth source

run timeline 只来自 event log projection：

- `build_run_timeline`
- `summarize_run_timeline`

### 6.2 allowed write targets

run timeline 只允许写：

- timeline artifact
- runtime semantic graph `RunTimelineRecord`

这里要再次强调：

> `RunTimelineRecord` 是 **runtime semantic graph write**，不是 governed memory write。

### 6.3 forbidden write-back

run timeline 不得回写：

- event log
- candidate pool
- working_context
- governed canonical memory graph

### 6.4 唯一业务视图约束

不得再长第二套并行的 run-level summary 语义：

- status
- tool trace
- assistant summary
- item count

凡是需要 run business view 的消费方，都应复用：

- `build_run_timeline`
- `summarize_run_timeline`

`HostSession.summary()` 不算 run timeline，它只是 host/session lifecycle metadata。

---

## 7. recall degraded mode

当 recall backend unavailable 时，`memory_search` 只允许返回结构化 unavailable 事实，例如：

```json
{
  "status": "unavailable",
  "reason": "recall_backend_unavailable",
  "warnings": ["recall_backend_cooldown"],
  "can_retry": false
}
```

### 7.1 forbidden payload shape

不允许：

- `fallback: "history_search_or_current_files"` 之类自由文本退路
- 任何把模型导回旧检索路径的自然语言 guidance

### 7.2 empty 与 unavailable 必须区分

- `empty`：backend 正常，但无命中；可带面向查询重试的 guidance
- `unavailable`：backend 故障/冷却；不得用自由文本把模型导回旧路径

---

## 8. governed canonical memory graph 的唯一写入口

### 8.1 规则

governed canonical memory graph 的唯一写入口是：

- `MemoryGovernanceExecutor.apply_decision`

它通过：

- `MemoryWriteService.submit`

把 candidate 升级为 governed canonical memory。

### 8.2 当前态 vs 目标态

- **当前态**：`MemoryWriteUnitOfWork` 用单个 Postgres 事务把：
  - canonical write
  - `memory_nodes` / `memory_relations`
  - decision row
  - outbox row
  一次性原子提交
- **目标态**：迁移后机制改成：
  - decision row + canonical mutation journal + 同步 projection refresh 在 Postgres 事务内原子提交
  - Oxigraph / `memory_search_index` / `memory_vector_index` 由 unified outbox 异步物化

### 8.3 迁移前后不变的硬约束

1. governance 是唯一 governed memory 写入口
2. Postgres-intent-first
3. 每条 governed memory 写都有原子的 decision / mutation provenance

---

## 9. related_existing_memories

### 9.1 语义

`related_existing_memories` 是 advisory input，不是 subject truth。

它今天的实现是：

- 从 live graph 取同 scope、同 type、ACTIVE 的既有 memory
- 用 token overlap 排序
- 截断到一个有限 top-k

### 9.2 冻结约束

- 它只能辅助模型选择 supersede / contradict target
- 它绝不能机械决定 target
- target 的最终合法性由 executor 复核

### 9.3 substrate 口径

迁移后，它应读：

- Postgres projection
- transaction-local staged docs

而不是 Oxigraph。

原因不是“当前实现恰好这样写”，而是更稳定的那条：

> 它是 hot-path advisory read，需要 same-batch freshness；因此它必须依赖同步面，而不能依赖异步 materialization。

### 9.4 为什么不复用 FTS 当 target truth

这里的关键原因不是“今天 `MemorySearchIndexSync.rebuild` 怎么实现”，而是：

- `memory_search_index` 是异步 projection
- 它不承诺 same-transaction freshness
- 因此不适合作为 governance target validation 的 truth source

也就是说，即使未来 `memory_search_index` 改成 outbox 增量 apply，这个结论仍然成立。

---

## 10. Postgres substrate 与 in-memory test double

### 10.1 立场

> Postgres 是唯一检索 substrate；in-memory 只作为 test double 存活。

### 10.2 这条线的收益

- recall 只有一套语义
- relatedness 将来可以统一升级到向量/embedding 路径
- 不再有“某些运行模式压根做不了 recall”的暗分叉

### 10.3 范围澄清

这不意味着：

- 要砍 protocol
- 要砍 test double

保留的东西：

- `GraphStore` / `EventLog` / `MemoryQuery` protocol
- 纯逻辑 unit tests 可继续使用 in-memory double

被砍的是：

- in-memory 作为产品运行 substrate
- in-memory 承担 recall / governance relatedness 语义

---

## 11. canonical graph substrate 迁移立场

### 11.1 目标态

长期目标是：

- `PostgresEventLog`
- `PostgresArtifactStore`
- governed canonical memory 的 Postgres mutation journal
- Oxigraph 作为 canonical RDF materialization

### 11.2 冻结的不变量

无论 substrate 如何迁移，下列不变量都不变：

1. governed memory 只由 governance 写入
2. Postgres-intent-first
3. unified outbox 是唯一异步驱动
4. `memory_nodes` / `memory_relations` 是事务内同步面
5. `memory_search_index` / `memory_vector_index` / Oxigraph 是异步面
6. recall / relatedness / dedupe / lifecycle validation 这些 hot-path 不读 Oxigraph
7. graph delete 的 mirror 清理也属于异步面；authoritative Postgres cleanup 不得被 Oxigraph 可用性反向阻断

### 11.3 合法停点

下列状态是完全可接受的长期停点：

- Postgres mutation journal 是 authoritative commit/provenance truth
- Postgres projection 继续服务 recall / relatedness / search
- Oxigraph 只是 async semantic graph mirror
- 非 hot-path consumer 从 Oxigraph 获益

不需要为了概念纯度，强推 hot-path 迁到 Oxigraph。

---

## 12. 禁止事项

- 任何非-governance surface 不得直接把内容写成 governed canonical memory
- working_context 不得写 candidate pool / governed memory / event log
- recalled content 不得回写为 candidate 或 governed memory
- reflection 不得直接写 governed memory
- run timeline 不得反向写 event log
- `memory_search` unavailable payload 不得带自由文本 fallback
- 不得把 `memory_nodes` / `memory_relations` 改成 outbox 异步刷新
- 不得为 Oxigraph / search / vector 各长一条 outbox
- 不得先写 Oxigraph 再补 Postgres provenance
- 不得因为 Oxigraph named-graph delete 失败而保留 Postgres truth；mirror delete 失败时必须留下可 replay 的 repair 事实
- hot-path 不得直接读 Oxigraph，除非单独立项并通过性能 gate

---

## 13. 测试守护

### 13.1 当前已有测试直接守住的部分

当前已有测试主要直接覆盖：

- recall unavailable payload 形状
- recall echo guard
- `related_existing_memories` 的 advisory 形状

### 13.2 substrate / migration 不变量由独立测试守住

下面这些不变量不应假装已经由 `tests/test_recall_v1.py` 全部守住：

- Postgres-intent-first
- unified outbox
- `memory_nodes` / `memory_relations` 同步面
- hot-path 永不读 Oxigraph

这些属于 substrate / migration 级不变量，应由：

- canonical mutation journal tests
- outbox replay / reconciliation tests
- Oxigraph materialization parity tests
- hot-path substrate tests

分别守住。

也就是说：

> §13 不是“整份契约都已经有测试”，而是“哪些部分已有测试、哪些部分必须由迁移测试负责”。
