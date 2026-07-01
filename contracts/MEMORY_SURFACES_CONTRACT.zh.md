# Memory Surfaces 写回边界契约

_Created: 2026-06-27_
_Rewritten: 2026-06-28_
_Amended: 2026-06-30 — freeze semantic relatedness v1 same-batch boundary_

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
  - 已在当前或更早事务中提交的 canonical projection rows
- **异步 recall 读面**
  - `memory_search_index`
  - `memory_vector_index`

冻结口径如下：

- recall 可以读同步面，也可以读 `memory_search_index` / `memory_vector_index`
- governance relatedness 可以用异步 search/vector index 加速 candidate generation，但必须回到同步面复核 canonical target
- lifecycle validation 只认同步面；异步 index 不提升为 governance validation truth source
- same-batch candidate dedupe/merge 依赖 whole-batch planner input 与 `merge_and_submit`，不把 pending candidate 伪装成 canonical truth
- v1 不承诺 transaction-local staged candidate 已具有 canonical id；尚未 apply 的 siblings 之间的 canonical contradiction/supersede edge 是显式 deferred gap
- deferred gap 不会在下一 batch 自动补回；若产品要求补回，必须新增 two-phase governance 或 maintenance/reconciliation

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

### 4.5 显式多跳搜索边界

- `memory_search.max_hops` 只允许 `0 | 1 | 2`,默认 `0`;automatic recall 强制按 0 跳执行。
- 多跳以基础 recall 命中的、已通过 canonical filter 的节点为 seed,并从同步 `memory_relations` 分层批量读取 typed edges。
- 1 跳只允许 canonical lifecycle/dependency neighbor;2 跳只允许共享 evidence、共享 basis、supersede lineage 三类冻结 motif,不得退化为无类型 BFS。
- path 必须携带 materialized edge 的原始 source / predicate / target 和 traversal direction,不得把逆向遍历伪装成正向事实。
- scope / type / status / suppression filter 必须覆盖 result 与 canonical intermediate;不可见 canonical node 不得成为跳板。
- graph channel 故障只允许结构化降级并保留可用的基础 recall 结果。
- 关系发现统一进入 `memory_search`;不再提供独立 `memory_related` 工具。单节点详情与 direct edges 由 `memory_get` 返回。

### 4.6 contradiction companion 是 0 跳特例

§4.5 把 typed 关系展开放在 `max_hops>=1`,但 **`CONTRADICTS` 是唯一的例外,在 0 跳(含 automatic recall)就必须展开**:

- 任何被基础通道命中的 memory,其 **active、同 scope、同 type** 的 `CONTRADICTS` 邻居即使未被直接命中,也必须作为 contradiction companion 补出,使模型不会只看到一个已知冲突的一半。
- 理由:对冲突只见一面是**正确性风险**,而非信息完整性问题;这与其它 typed 关系(shared-evidence / basis / supersede-lineage,纯属"更多上下文")在风险等级上不同,故单独前置到 0 跳。
- companion 受与任何召回节点**相同的 scope / type / status / suppression 过滤**约束:hidden-scope 或非-active 的对立面绝不泄漏。
- companion **不受 `limit` 约束**(它是安全补充,不是排序竞争者),但**不附 grounded path**(path 仍只在 `max_hops>=1` 出现)。
- companion 与被命中方都标 `contradiction_warning`;companion 额外标 `contradiction_companion`。
- **只展开一轮直接伙伴,不做传递闭包**:companion 仅从**基础通道命中的 seed**(lexical/FTS/dense 直接命中、已过 canonical filter 的节点)的 `CONTRADICTS` 边补出;companion 自身的 `CONTRADICTS` 边**不再触发二次展开**。理由:contradiction **不可传递** —— A↔B、B↔C 不蕴含 A↔C(A 与 C 可能恰好一致),对 A 展开整个 contradiction component 会把与 A 不冲突的 C 当成 A 的冲突伙伴,既语义错误又重新引入 top-k 收紧时要避免的噪声。C 是 B 的冲突;只有当 B 自己也是基础通道命中的 seed 时,C 才会作为 B 的 companion 出现。
- v1 不对 automatic 路径的 companion 数量做预算/上限:多重 contradiction 是治理副路径中的小概率事件,蓄意构造场景暂不在 v1 防护范围。
- **projection 自述必须诚实**:`ProjectionBuilder` 的冲突对必须作为**不可拆分单元**优先渲染,同时保留双方 id、短摘要与 `CONTRADICTS` 关系;普通条目再按剩余预算逐条完整 packing,不得做可能留下半行/半个冲突对的整体字符截断。`included_memory_ids` / `conflict_groups` / `items` 只能反映实际进入 summary 的完整单元。若最小冲突单元本身超过 nominal `token_budget`,正确性优先于 soft budget:保留完整冲突安全单元并丢弃普通尾部,不得重新制造 half-conflict。

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

生产路径的 hard-cut 边界：

- `MemoryGovernanceExecutor` 只有一条 UoW 执行路径，`memory_write_uow_factory` 是必填依赖；缺失或显式传 `None` 必须在构造期失败，不得推断 storage backend。
- 生产 wiring 只能注入 PostgreSQL `MemoryWriteUnitOfWork`。PostgreSQL 是 governed canonical authority；Oxigraph、search 与 vector 都是 outbox 驱动的异步派生面，不进入同步 UoW。
- `InMemoryMemoryWriteUnitOfWork` 只服务显式的 deprecated compatibility/test wiring，不是 fallback，也不满足生产 durability、事务原子性或 async materialization 契约。测试 fake 只能验证 executor 决策逻辑；事务、rollback 与 outbox 一致性必须由 real PostgreSQL 测试证明。
- `durable=False` / in-memory runtime 暂留作后向兼容，但属于 unsupported production path；后续功能不得新增对该路径的依赖。

### 8.3 迁移前后不变的硬约束

1. governance 是唯一 governed memory 写入口
2. Postgres-intent-first
3. 每条 governed memory 写都有原子的 decision / mutation provenance
4. 不得以“未配置 UoW”为条件 fallback 到 InMemory 或 no-op outbox

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
- 精确 embedding/rerank score 只用于内部排序、截断与诊断，不进入 Flash prompt
- lifecycle target 必须属于该 candidate 在当前 governance batch 的 executor-side allowlist；prompt 规则不能替代该校验
- partial-channel degradation 仍应产出 allowlist 和诊断，但 v1 不在证据链不完整时执行 contradiction/supersede
- `full` 相对于当前 deployment 已配置且本轮计划执行的通道定义；未配置 reranker 不得让部署永久处于 `partial`
- canonical allowlist 只约束 contradiction/supersede 使用的 canonical memory IDs；`merge_and_submit.target_entry_ids` 属于 whole-batch candidate entry ID 空间，不受该 allowlist 约束
- dense candidate threshold 必须属于可版本化 relatedness options，并与 embedding fingerprint、fixture version 一起校准；不得以提高 threshold、牺牲 recall 的方式换取表面 precision

### 9.3 substrate 口径

迁移后，它的 canonical target 应读：

- 已提交的 Postgres `memory_nodes` / `memory_relations` 同步 projection
- 可选的 async search/vector candidate IDs，但这些 ID 必须回到同步面复核

而不是 Oxigraph。

原因不是“当前实现恰好这样写”，而是更稳定的两条：

> async materialization 不能作为 target validation truth；已经提交的 canonical target 必须在同步 Postgres 面复核。

> v1 的 whole-batch candidate visibility 与 canonical visibility 是两回事：Flash 可以同时看到 pending siblings 并用 `merge_and_submit` 去重/合并，但尚未 apply 的 sibling 没有 provisional canonical id，不能成为另一个 sibling 的 contradiction/supersede target。

因此必须区分：

- `committed-but-unindexed`：可由同步面读取、vector candidate union 与 bounded inline embed 做 best-effort 修补；超出预算的余量由 async worker 回填；
- `staged-but-uncommitted`：v1 对 canonical lifecycle edge 明确 defer，并记录 `same_batch_lifecycle_deferred`。

不得声称 deferred edge 会在下一 batch 自动补回。若未来需要该保证，必须显式引入 two-phase plan/apply 或 maintenance/reconciliation。

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
4. `memory_nodes` / `memory_relations` 是 committed canonical memory 的事务内同步面
5. `memory_search_index` / `memory_vector_index` / Oxigraph 是异步面
6. recall / relatedness / dedupe / lifecycle validation 这些 hot-path 不读 Oxigraph
7. graph delete 的 mirror 清理也属于异步面；authoritative Postgres cleanup 不得被 Oxigraph 可用性反向阻断
8. whole-batch pending candidates 可用于 candidate-level merge，但 v1 不为 staged siblings 创建 provisional canonical id；它们之间的 canonical contradiction/supersede edge 明确 defer
9. contradiction/supersede 的 canonical lifecycle target 必须来自当前 candidate 的 relatedness allowlist，并在 apply 的同一 UoW 中从同步面重新验证；candidate-entry merge 不在该 canonical allowlist 约束内

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
- 不得把 async search/vector candidate 命中直接当成 lifecycle target validation
- 不得把 same-batch pending candidate 当成已有 canonical target，或声称 deferred lifecycle edge 会由下一 batch 自动补回

---

## 13. 测试守护

### 13.1 当前已有测试直接守住的部分

当前已有测试主要直接覆盖：

- recall unavailable payload 形状
- recall echo guard
- `related_existing_memories` 的 advisory 形状

semantic relatedness 落地后必须新增：

- allowlist 强制：真实存在且合法、但未被该 candidate advisory surface 的 ID 必须被 executor 拒绝；这与不存在/编造 ID 分开测试
- transaction-local re-read：snapshot 后 target 状态漂移，apply 时必须在同一 UoW 中重读并拒绝 lifecycle action
- partial-channel degradation：rerank 失败但 dense 可用（或反向的可用 semantic channel）时仍产生 allowlist，不把整个 relatedness batch 判为失败
- same-batch boundary：candidate-level duplicate 可 merge；staged siblings 之间的 canonical contradiction/supersede edge deferred，并留下结构化诊断
- candidate recall：versioned cross-lingual/alias/paraphrase fixture 必须有 recall@k 与 miss-rate gate，且与 destructive-action precision 分开报告
- deployment-relative status：未配置 reranker时，全部已配置通道成功必须得到 `full`；只有已配置/计划通道失败才得到 `partial`
- ID-space carve-out：canonical allowlist enforcement 不得阻断 whole-batch `merge_and_submit.target_entry_ids`
- target drift：snapshot 后 target 漂移必须留下需要 re-governance/maintenance 的诊断，不能把安全 downgrade 当成 lifecycle 终态正确

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
