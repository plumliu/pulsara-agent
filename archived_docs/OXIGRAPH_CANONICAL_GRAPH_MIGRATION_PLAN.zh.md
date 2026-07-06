# Oxigraph Canonical Graph Migration Plan

_Created: 2026-06-28_

这是一份**重写后的实施文档**。它取代之前那版“把 `PostgresGraphStore` 直接迁到 `OxigraphGraphStore`”的叙事，原因很简单：

- 那种写法会直接撞穿 governance 的 provenance / audit 不变量。
- 它没有正面回答“为什么一定要 Oxigraph，而不是先把 Postgres 里的 canonical / projection 解耦”。
- 它把 recall / relatedness / lifecycle / timeline / projection 几条链路混成了一次性大切换。

这版文档的核心目标是把问题说清楚：

1. 当前代码里，`PostgresGraphStore` 到底承担了哪些角色。
2. 为什么**不能**把 canonical write 直接改成“先写 Oxigraph，再补 Postgres”。
3. 如果最终仍希望形成 `PostgresEventLog + PostgresArtifactStore + OxigraphGraphStore` 的三层格局，正确的迁移路径应该是什么。
4. 哪些地方可以先停，哪些地方必须过线以后才能继续。

---

## 0. 结论先行

### 0.1 这次迁移的真正对象

这次迁移的对象不是“把一个 store 类名换掉”，而是把当前混在一起的五个角色拆开：

1. canonical semantic write
2. governance audit / provenance
3. typed recall projection
4. search / vector / future index projection
5. RDF graph materialization

当前代码里，这五件事大量缠在 `PostgresGraphStore` + `MemoryWriteUnitOfWork` 上。

### 0.2 本文的硬决策

这版计划明确选择：

- **崩溃安全 / provenance 完整性**
- **低延迟 hot-path**

优先于：

- **Oxigraph 上的即时 read-your-writes**

也就是说，本文**不再主张**：

- governance/lifecycle 直接把 canonical mutation 同步写进 Oxigraph；
- recall / relatedness / dedupe / lifecycle hot-path 改成直接读 Oxigraph；
- runtime wiring 里直接把 `PostgresGraphStore(...)` 替换成 `OxigraphGraphStore(...)`。

相反，本文选择：

> **Postgres-first intent commit + unified outbox + async materialization to Oxigraph**

这是这版文档最重要的改写。

### 0.3 新的目标态

长期想要的产品格局仍然是：

- `PostgresEventLog`
- `PostgresArtifactStore`
- `OxigraphGraphStore`

但要诚实地补一句：

> 在没有跨存储事务的前提下，**governance 的可审计写入真源**必须先落在 Postgres 事务里；Oxigraph 是由这份 Postgres intent journal 异步物化出的 canonical semantic graph。

因此，最终形态不是“完全没有 Postgres 侧 canonical 痕迹”，而是：

- **Postgres runtime DB** 承担：
  - event truth
  - artifact truth
  - governance decision / canonical mutation journal
  - recall/query/search/vector projection
- **Oxigraph** 承担：
  - canonical semantic graph 的 RDF materialization
  - named graph export / SPARQL / future graph-native consumers

这仍然是“PostgresEventLog + PostgresArtifactStore + OxigraphGraphStore”的产品级三层，只是**实现上必须额外承认 journal/outbox 是跨存储一致性的承重件**。

---

## 1. 当前代码事实

### 1.1 durable runtime today 仍把 PostgresGraphStore 当 canonical graph

当前 `build_durable_runtime_wiring()` 在 `src/pulsara_agent/runtime/wiring.py` 里创建：

- `PostgresEventLog`
- `PostgresArtifactStore`
- `PostgresGraphStore`
- `PostgresMemoryQuery`
- `MemoryWriteUnitOfWork`

也就是说，durable runtime 今天的 canonical graph 并不是 Oxigraph，而是 Postgres。

### 1.2 PostgresGraphStore 不是纯 GraphStore facade

`src/pulsara_agent/graph/postgres.py` 里的 `PostgresGraphStore.put_jsonld()` 当前同一次调用会同时：

1. upsert `graph_documents`
2. refresh `memory_nodes`
3. refresh `memory_relations`

因此它不是单纯的“canonical JSON-LD 文档存储”，而是：

- truth store
- projection maintainer

二合一。

### 1.3 MemoryWriteUnitOfWork 今天给的是单事务保证

`src/pulsara_agent/memory/canonical/unit_of_work.py` 当前用同一个 psycopg connection 绑定：

- `PostgresGraphStore(connection=...)`
- `CandidateDecisionRepository`
- `OutboxRepository`
- `MemoryLifecycle(graph=self.graph, mutable=self.graph)`
- `ExecutionEvidenceLedger(graph=self.graph, ...)`

其现实效果是：

> canonical write + typed projection + decision row + outbox row 在一个 Postgres transaction 中提交。

这条不变量目前是成立的。

### 1.4 OxigraphGraphStore today 不具备接这个 hot-path 的条件

`src/pulsara_agent/graph/oxigraph.py` 当前有几个非常关键的限制：

- HTTP 同步阻塞客户端；
- `query(bindings=...)` 还不支持；
- `find_by_type()` 是 `SELECT ?s` 后再对每个 subject 做一次 `get_jsonld()`，天然 N+1；
- 没有 `set_status(...)`；
- 没有 transaction-bound UoW 语义。

所以它今天适合作为一个 graph backend seam，但**完全不适合**直接接管 governance / recall / lifecycle 的热路径。

### 1.5 lifecycle today 依赖 mutable graph

`src/pulsara_agent/memory/canonical/lifecycle.py` 的 `MemoryLifecycle` 现在依赖：

- `graph.get_jsonld`
- `graph.put_jsonld`
- `mutable.set_status`

并且 `supersede()` / `link_contradiction()` 是典型的“多文档 logical mutation”：

- supersede:
  - 读 old
  - 读 new
  - 改 new 文档，加 `supersedes`
  - 改 old status -> `SUPERSEDED`
- contradiction:
  - 读 left
  - 读 right
  - 改两边文档，加 `contradicts`

在 Postgres UoW 里，这至少被一个事务兜住；直接改成 Oxigraph HTTP 写会立刻退化成多次独立远程写。

### 1.6 recall / governance relatedness today 读的是 Postgres substrate

`PostgresMemoryQuery` 当前直接查：

- `memory_nodes`
- `memory_relations`
- `memory_search_index`

`LexicalMemoryRecallService` 也是基于这层。

所以 recall today 的事实不是“只读 graph truth”，而是：

> recall 读的是 Postgres 上的 typed projection / search substrate。

这件事必须在迁移文档里明写，不能再说成“normal recall 只读 Oxigraph”。

---

## 2. 不能破的硬不变量

这次迁移不是只看“最终能不能读到数据”，而是必须守住以下不变量。

### 2.1 governance provenance 不变量

每一次 canonical memory 写入，都必须存在可追溯的：

- governance decision row
- canonical mutation intent
- replay / repair 入口

绝不能出现：

- Oxigraph 里有一条 canonical memory
- 但 Postgres 里没有对应 decision / outbox / mutation record

这种情况会直接让 canonical truth 失去可审计 provenance。

### 2.2 单 logical mutation 的完整性不变量

以 supersede 为例，“写新节点 + 退旧节点 + 建 supersedes 关系”是**一个 logical mutation**。

即使底层不能跨存储原子提交，也必须满足：

- 这组变更要么作为一份可重放的 mutation journal 留下；
- 要么根本不发生。

不能发生“只改了一半、且没人记得剩下那一半该补什么”。

### 2.3 outbox 必须统一,但不是所有投影都异步

不能长出两套甚至三套并行同步链：

- Oxigraph 一套 outbox
- search index 一套 outbox
- vector index 一套 outbox

正确做法是：

> **一条 canonical mutation outbox，驱动所有跨存储 / 异步派生 surface**

这里必须把同步面和异步面分清：

- **事务内同步面**
  - `memory_nodes`
  - `memory_relations`
- **outbox 异步面**
  - `memory_search_index`
  - `memory_vector_index`（未来）
  - Oxigraph materialization

原因很简单：

- `memory_nodes` / `memory_relations` 是 recall、same-batch dedupe、governance relatedness、lifecycle validation 的 hot-path substrate；
- 它们必须保留 transaction-local read-your-writes；
- 如果把它们也改成异步 outbox apply，就会重新引入“刚提交的 mutation，本批次 hot-path 看不见”的语义裂缝。

因此本文明确冻结：

> **`memory_nodes` / `memory_relations` 保持同事务同步投影；unified outbox 只驱动跨存储或可延迟的一致性面。**

### 2.4 hot-path 不因 Oxigraph 退化

governance relatedness / dedupe / recall / lifecycle validation 不能因为迁移到 Oxigraph 而变成：

- 每次都跨网 HTTP
- 每次都 N+1
- 每次都依赖 query features 还没实现的 SPARQL path

如果要在这几条路径上上 Oxigraph，必须是**后置 gate**，不是先上再修。

---

## 3. 这次必须正面承认的三难困境

这里是之前版本最大的问题。它同时暗示了三件不能同时拿到的东西：

| 想要 | 代价 |
|---|---|
| 崩溃安全（Postgres commit 才算真正落账） | Oxigraph 不能作为即时 read-your-writes 真源 |
| Oxigraph read-your-writes | canonical mutation 必须先写 Oxigraph，直接打破 provenance 安全 |
| 低延迟 hot-path | 不能把 relatedness / recall 热路径改成每次 Oxigraph HTTP 读 |

这三个目标不能同时成立。

### 3.1 本文的取舍

本文明确选择：

- **保崩溃安全**
- **保低延迟 hot-path**
- **放弃 Oxigraph 上的即时 read-your-writes**

这意味着：

- recall 继续读 Postgres projection；
- governance relatedness 继续读 Postgres projection / same-transaction staged docs；
- lifecycle validation 继续在 Postgres UoW 里完成；
- Oxigraph 接受“最终一致的 semantic graph materialization”角色。

### 3.2 relatedness 为什么不读 Oxigraph

之前那句“relatedness 必须读 canonical truth，而 Oxigraph 是 canonical truth”在这里必须删掉。

因为本文已经选择：

- relatedness 是 advisory，不是 final authority；
- advisory 输入允许短暂与 Oxigraph materialization 分歧；
- 既然允许短暂分歧，就没有必要在 hot-path 上为它支付 HTTP + N+1 成本。

所以本文的结论是：

> **relatedness 读 Postgres projection，不读 Oxigraph。**

这是一个明确、可执行、而且与崩溃安全一致的选择。

### 3.3 read-your-writes 放在哪里

迁移后，“刚写入的 canonical mutation 在同批次内可见”这件事，不再由 Oxigraph 保证，而由：

- Postgres transaction 内的 staged docs / projection rows

保证。

这里再精确一点：

- `memory_nodes` / `memory_relations` 是**同步投影**，在事务内即可用于 hot-path；
- `memory_search_index` / `memory_vector_index` / Oxigraph 是**异步面**，不承诺 same-batch read-your-writes。

也就是说：

- same-batch dedupe
- supersede target validation
- contradiction target validation
- governance relatedness

都继续依赖 transaction-local Postgres view。

---

## 4. Oxigraph 到底买到了什么

如果不回答这个问题，Phase 3/4 就只是概念洁癖。

### 4.1 Oxigraph 真正值得买的东西

Oxigraph 相对 “`graph_documents` 继续放在 Postgres” 真正多出来的价值，是这些：

1. **RDF / named graph / SPARQL 互操作**
   - 更适合导出、外部查询、语义网工具链集成。
2. **统一的 graph-native materialization**
   - 让 runtime semantic nodes、memory nodes、relations 都落在同一 RDF surface。
3. **未来非 hot-path 的图查询能力**
   - 例如跨实体、多跳、外部知识融合、图快照导出。
4. **把 graph 语义从 bespoke JSONB 查询逻辑里抽出来**
   - 让“图是什么”与“recall hot-path 怎么快查”分层。

### 4.2 Oxigraph 不会自动买到的东西

Oxigraph **不会自动**带来：

- 更快的 recall；
- 更好的 governance relatedness；
- 更简单的原子提交；
- 更低的运维复杂度。

这些恰恰是它当前更差的地方。

### 4.3 因此，Phase 2 是一个合理停点

本文明确允许一个长时间可接受的停点：

> **Phase 2：Postgres authoritative journal + Postgres hot-path projection + Oxigraph async mirror**

如果做到这一步以后，产品并没有强烈需要：

- external SPARQL
- RDF export
- graph-native non-hot-path consumers

那么完全可以停在这里，不继续推进“让 Oxigraph 接更多默认读路径”。

这是这版文档相对旧版最重要的降躁点之一。

---

## 5. 新的目标架构

### 5.1 写路径：Postgres intent-first

canonical mutation 不再直接“写 truth store 并顺手刷 projection”，而是先形成一个**完整的 logical mutation journal**。

一次 governance apply 的正确顺序应该是：

1. 在 UoW 内计算 final mutation：
   - 要写/改哪些 canonical 文档；
   - 这些文档的最终 JSON-LD 形状是什么；
   - 哪些 memory ids 是 projection dirty；
   - 这次 mutation 对应哪个 decision / batch。
2. 在同一个 Postgres transaction 中提交：
   - decision row
   - canonical mutation outbox row（携带完整 final docs）
   - 必要的 transaction-local projection refresh（`memory_nodes` / `memory_relations`，供同批次热路径使用）
3. transaction commit 后，后台 worker 再异步 apply 到：
   - Oxigraph
   - `memory_search_index`
   - `memory_vector_index`（未来）

**禁止**的旧顺序是：

1. 先写 Oxigraph
2. 再写 Postgres decision/outbox

因为它会产生“有 canonical write，但没有 provenance row”的 split-brain。

### 5.2 读路径：按 surface 分开

迁移后的读路径应该分成三类：

1. **hot-path operational reads**
   - recall
   - governance relatedness
   - same-batch dedupe / lifecycle validation
   - 这些都读 Postgres projection / staged docs
2. **semantic graph reads**
   - export
   - SPARQL tooling
   - future non-hot-path graph inspection
   - 这些读 Oxigraph
3. **event / artifact reads**
   - 继续走 PostgresEventLog / PostgresArtifactStore

### 5.3 lifecycle mutation 变成“journaled final-doc set”

这是另一个关键改动。

`MemoryLifecycle.supersede()` / `link_contradiction()` 不应再被理解成“直接对 live graph 连续打几次 mutation”，而应改成：

- 在 UoW 内读取 current docs；
- 计算**整次 logical mutation 的最终文档集合**；
- 把这组最终文档作为一份 canonical mutation journal 提交；
- 后台按 journal 幂等物化到 Oxigraph。

这样可以同时解决两个问题：

1. Oxigraph 内部没有 transaction-bound multi-step mutation 的问题；
2. worker 崩溃后可以整组重放，而不是丢失“剩半条边没补”的上下文。

### 5.4 unified outbox 是唯一异步派生驱动

`memory_write_outbox` 不应只装 “decision committed + index dirty ids”。

它应升级成统一的 canonical mutation outbox，至少承载：

- `mutation_id`
- `graph_id`
- `governance_batch_id`
- `decision_id`（仅 governance memory writes 必填）
- `mutation_lane`
  - `governed_memory`
  - `runtime_semantic`
- `documents`: 本次要写入/覆盖的完整 JSON-LD 文档集合
- `deleted_graph` / `graph_reset`（如需要）
- `dirty_memory_ids`
- `surface_apply_status`：
  - `search_index`
  - `vector_index`
  - `oxigraph`
- `sequence_key`
  - 例如 `graph_id`
  - 用于同一 graph / lane 的有序消费
- `attempt_count`
- `last_error`

这张表是全文的承重件。

这里再次明确：

- `postgres_projection` 不应再出现在 outbox `surface_apply_status` 里；
- 因为 `memory_nodes` / `memory_relations` 是事务内同步面，不是异步派生面。

### 5.5 governance 写和 runtime semantic 写不是同一条流量 lane

这版必须再补一刀，把两类写分开：

1. **governed memory writes**
   - 低频
   - 高价值
   - 强 provenance
   - 需要 `decision_id` / `governance_batch_id`
   - 适合走完整 mutation journal
2. **runtime semantic writes**
   - 高频
   - 如 `ToolResult` / `Turn` / `RunTimelineRecord`
   - 不应把每次写都抬升到和 governance memory 同等重的 decision lane

因此本文明确建议：

> **governed memory writes** 走完整 canonical mutation journal；  
> **runtime semantic writes** 走更轻的 async mirror lane。

这两条 lane 可以共用同一张 outbox 表和同一套 apply framework，但不能混成同一种语义。

更具体地说：

- governed memory lane:
  - 要有 `decision_id`
  - 要有完整 provenance
  - 要有 replay / audit 对账
- runtime semantic lane:
  - 无 `decision_id`
  - 可只保留 `event_source` / `artifact_source` / `run_id` 级别 provenance
  - 不要求和 governance memory 相同粒度的审计强度

这样才能避免：

- 每次 tool call / timeline snapshot 都把重 journal outbox 灌爆；
- 把“治理级记忆写”与“高频运行时语义镜像”混成一套过重路径。

---

## 6. 需要新增或重构的抽象

### 6.1 `CanonicalMutationBuilder`

职责：

- 把 `submit_as_is` / `supersede_and_submit` / `contradict_and_submit`
  翻译成“最终文档集合 + dirty ids + provenance”。

对 runtime semantic writes，可复用同样的技术壳，但不强求和 governed memory 同一语义 lane。

它不负责落 Oxigraph。

### 6.2 `CanonicalMutationJournal`

职责：

- 在 Postgres transaction 内原子写入：
  - decision row
  - mutation outbox row

这才是 governance provenance 的真正锚点。

### 6.3 `PostgresProjectionApplier`

职责：

- 从 canonical mutation payload 刷新：
  - `memory_nodes`
  - `memory_relations`

注意：

- 它不再是某个 store 的副作用；
- 它是一个显式 projection applier。

### 6.4 `SearchIndexApplier` / `VectorIndexApplier`

职责：

- 同样吃 unified outbox；
- 不再各自发明第二条同步链。

这里要点名一个现实差异：

- `memory_search_index` 今天主要还是 rebuild / consume-outbox 驱动；
- 迁移后它需要真正具备**增量 apply**能力。

所以这不是“把现有逻辑直接挂到 outbox 上”这么轻，而是一个新增能力面。

### 6.5 `OxigraphMaterializer`

职责：

- 把 canonical mutation journal 里的完整 final docs 幂等 apply 到 Oxigraph named graph。

它必须满足：

- 重放安全；
- 单 mutation 可多次 apply 而不改变最终结果；
- graph delete / reset 有显式语义。

### 6.6 `PostgresGraphStore` 的新定位

迁移后，`PostgresGraphStore` 不应该再是“runtime product-facing canonical graph implementation”。

它更合理的新定位是二选一：

1. **Phase 1/2**：
   - 作为 transaction-local canonical staging / journal helper 继续存在；
2. **Phase 3+**：
   - 拆成更小的组件：
     - `PostgresCanonicalJournalStore`
     - `PostgresProjectionApplier`

总之，不能再让一个 `GraphStore` 同时扮演 truth + projection + mutable lifecycle surface。

---

## 7. 分阶段实施

## 7.1 Phase 0：共享前置，不是 Oxigraph 专属

这一步不是 Oxigraph 独有，但它是前置：

- durable runtime 成为默认/唯一产品运行路径；
- in-memory 降为 test double；
- tests/fixtures 里明确区分：
  - product runtime -> Postgres
  - pure logic unit tests -> in-memory double

这一步做完后，后面的迁移不必同时背“还有一个 in-memory 产品模式”的歧义。

### 退出标准

- Host / CLI / real runtime 不再依赖 in-memory substrate；
- in-memory 只留纯逻辑单测。

---

## 7.2 Phase 1：先在 Postgres 内部把角色拆开

目标：

- 不动 Oxigraph；
- 先把“truth / projection / outbox / lifecycle mutation”拆清楚。

工作项：

1. 把 `memory_write_outbox` 升级为 unified canonical mutation outbox。
2. governance apply 不再直接把 graph write 当副作用散落在各处，而是先形成 mutation payload。
3. `PostgresProjectionApplier` 显式化，并明确它只负责事务内同步刷新 `memory_nodes` / `memory_relations`。
4. lifecycle 改成 journaled final-doc set，而不是 live graph 多次 mutation。
5. 明确 lane 分层：
   - governed memory lane
   - runtime semantic lane
   - graph reset lane

这是一个**非常重要的中间停点**：

> 如果这一步都做不干净，后面接 Oxigraph 只会把问题跨存储放大。

### 退出标准

- 当前产品仍完全可跑，canonical 还在 Postgres；
- 但 code shape 上已经没有“一个 `GraphStore` 同时干五件事”的缠绕；
- unified outbox 已成为唯一派生驱动。

---

## 7.3 Phase 2：把 Oxigraph 接成 async materialized graph

目标：

- Oxigraph 开始吃 unified outbox；
- 但 recall / relatedness / lifecycle validation / dedupe 仍不读它。

工作项：

1. 新增 `OxigraphMaterializer` worker。
2. outbox payload 支持多文档 final state apply。
3. worker 明确采用 **per-graph / per-lane 有序消费**：
   - 同一 `graph_id` 下的 mutation 必须按提交顺序 apply；
   - 至少不得让 `supersede(new -> old)` 先于 `new` 的 create apply。
4. 明确背压 / 故障策略：
   - Oxigraph 长时间不可用时，outbox 可积压，但必须有告警；
   - mirror 发散可接受，但 governance / recall hot-path 不因此停摆；
   - 不允许静默无限失败而无人可见。
5. `memory_search_index` 真正新增增量 apply path。
6. `delete_graph(graph_id)` 与 Oxigraph named graph 1:1 对齐。
   - authoritative cleanup 仍以 Postgres 为先；
   - 若 Oxigraph delete 失败，必须留下可 replay 的 `graph_reset` tombstone，而不是只打一条日志。
7. 新增 parity / reconcile：
   - 找到已 journaled 但未 materialize 的 mutation；
   - 找到 Oxigraph 与 Postgres projection 不一致的 graph ids / doc ids；
   - 既能报 `missing_in_oxigraph`，也能报 `stale_in_oxigraph`；
   - 支持 replay。

### 这一阶段的关键态度

Oxigraph 在这一阶段是：

- **产品级 semantic graph mirror**

但还不是：

- recall hot-path substrate
- governance validation substrate

### 退出标准

- 所有新的 canonical mutation 都只经 unified outbox 驱动 Oxigraph；
- runtime 不再直接对 Oxigraph 发“真相级 side write”；
- replay / repair / parity diff 跑通。
- `memory_search_index` 的增量 apply 已替代“只靠离线 rebuild 才一致”的状态。

补充一句实现语义：

> `delete_graph(graph_id)` 可以同步触发一次 mirror cleanup 尝试，但这次尝试必须是 **Postgres-first**，并且失败时要留下 outbox tombstone 供后续 replay。

---

## 7.4 Phase 3：只把值得的读路径迁到 Oxigraph

这一阶段不是自动发生的。

只有在明确存在下列需求时才做：

- 需要外部 SPARQL / RDF 互操作；
- 需要 graph-native export；
- 需要非 hot-path 的多跳图分析；
- Oxigraph 读能力（bindings、bounded query、非 N+1）已补齐。

一个足够具体的首个 consumer 例子是：

- **用户可下载 / 外部系统可消费的 semantic graph export endpoint**
  - 它读取 Oxigraph named graph；
  - 不进入 recall / governance hot-path；
  - 能真实利用 RDF / named graph / SPARQL 互操作价值。

**明确不在这一阶段迁过去的路径：**

- recall candidate generation
- governance relatedness
- same-batch dedupe
- lifecycle validation

如果将来真要迁这些热路径，也必须单独立项，而不是顺手带过去。

### 退出标准

- 至少有一个明确的、非热路径的产品消费者真正从 Oxigraph 获益；
- 不是“因为它存在所以用它”，而是有实测收益。

---

## 7.5 Phase 4：评估是否还要退役 `graph_documents` 作为 queryable surface

这一步**不是默认要做**。

这是一个单独的 go/no-go：

- 如果 Oxigraph 已经证明其价值，并且 journal + projection + materializer 形态稳定；
- 才评估是否把 `graph_documents` 从“queryable canonical doc surface”进一步收缩为“journal/staging-only implementation detail”。

如果没有这个必要，就不要为了“概念上更纯”去做。

---

## 8. 测试与验证

### 8.1 provenance / crash-safety 测试

必须新增或强化：

1. **Postgres-intent-first 崩溃点测试**
   - 在“decision/outbox committed，但 Oxigraph 未 apply”时崩溃：
     - 允许 Postgres journal 存在；
     - 不允许缺失 provenance；
     - replay 后 Oxigraph 补齐。
2. **禁止 Oxigraph-first split-brain**
   - 不应再存在“Oxigraph 已有 canonical node，但 Postgres 无 decision/outbox”的可达路径。

### 8.2 logical mutation replay 测试

对以下 mutation 做幂等 replay：

- submit-as-is
- supersede
- contradiction
- stale mark
- graph delete / reset

要求：

- 重放不产生额外 duplicate docs；
- 最终 Oxigraph 状态与 Postgres staged final docs 一致。

### 8.3 round-trip / fidelity 测试

不能只测 memory node。

至少覆盖：

- canonical memory nodes
- runtime semantic nodes（如 ToolResult / Artifact / Turn / RunTimelineRecord）
- relation-heavy docs
- 时间字段 / typed literal
- list / nested object / node reference

要明确防的是：

- JSON-LD -> RDF -> JSON-LD round-trip 语义损失

### 8.4 parity / reconciliation 测试

必须有：

- Postgres journal -> Oxigraph parity diff
- outbox replay after partial failure
- named graph delete 对齐
- `memory_search_index` / `memory_vector_index` / Oxigraph 都吃同一 outbox；`memory_relations` 仍是事务内同步面

### 8.5 performance gate

若未来任何 hot-path 想读 Oxigraph，先过门槛：

- query 不得 N+1
- bounded latency
- bindings / typed query 能力齐备

没有这几条，不得接 recall / governance hot-path。

---

## 9. 契约与文档需要同步修改的地方

### 9.1 `contracts/MEMORY_SURFACES_CONTRACT.zh.md`

这份契约当前已经固定了一条重要事实：

- Postgres 是唯一 recall substrate

这与本文是一致的，应保留。

但若继续推进本迁移，需要补充两件事：

1. **旧的“canonical graph 单事务隐含保证”不再成立**
   - 要改写成：
     - Postgres decision + mutation journal 原子提交；
     - Oxigraph 由 outbox 最终一致物化。
2. **canonical truth 的定义要更精确**
   - 从“单个 graph store 立即可读真相”改成：
     - governance commit truth = Postgres mutation journal
     - semantic graph materialization = Oxigraph

### 9.2 其他文档

需要同步扫的文档包括：

- `README.md`
- `RUNTIME_STORAGE_ARCHITECTURE.zh.md`
- 较早期的 memory architecture/design 文档

重点不是全改成一样的话，而是避免继续写出下面这种会误导实现者的话：

- “normal recall 只读 Oxigraph”
- “runtime 只要把 `PostgresGraphStore` 替换成 `OxigraphGraphStore`”
- “Oxigraph 是唯一立即可读的 canonical truth”

---

## 10. 最终建议

如果只用一句话概括这份重写后的计划，那就是：

> **先把 canonical mutation journal、projection、Oxigraph materialization 三件事拆开，再谈“Oxigraph 成为 canonical graph”。**

更具体一点：

1. **先做 Phase 0/1。**
   - 这是无争议地正确的收敛。
2. **再做 Phase 2。**
   - 让 Oxigraph 成为 async semantic graph materialization。
3. **只有在真实 consumer 出现时，才继续做 Phase 3/4。**
   - 不为了概念纯度强推 hot-path 迁移。

### 10.1 本文明确拒绝的方案

以下方案在本轮被明确否决：

- runtime wiring 直接把 `PostgresGraphStore` 换成 `OxigraphGraphStore`
- canonical write 先写 Oxigraph，再补 Postgres decision/outbox
- relatedness 改成默认读 Oxigraph
- recall v1/v2 直接查 Oxigraph SPARQL
- Oxigraph / search / vector 各自维护一条 outbox

### 10.2 本文允许的停点

以下状态被本文视为**完全可接受的长期停点**：

- Postgres runtime DB 里有 authoritative governance journal；
- recall / relatedness / search / vector 仍都在 Postgres substrate；
- Oxigraph 作为 async semantic graph mirror 存在；
- 外部 graph/export/non-hot-path consumer 从 Oxigraph 受益；
- 没有继续强推 Oxigraph hot-path 化。

如果到了那个状态，系统已经比今天清楚得多，而且没有把 provenance / crash-safety / hot-path latency 拿去换概念纯度。
