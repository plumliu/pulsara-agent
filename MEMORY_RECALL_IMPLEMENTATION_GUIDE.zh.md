# Pulsara 记忆召回实现指导说明书

_Created: 2026-06-15_

> 本文是 `MEMORY_RECALL_PRODUCT_ARCHITECTURE.zh.md` 的**可落地化重写**。前者是方案性文档（讲"为什么这样"）；本文是**实现说明书**（讲"具体改哪个文件、写什么签名、什么 DDL、跑什么测试、什么时候算完成"）。
>
> 前置阅读（按需）：`MEMORY_RECALL_FEASIBILITY_ANALYSIS.zh.md`（批判与约束来源）、两份 survey（外部框架经验）。本文不再重复论证，只在每个决策处用一句话标注它消解了哪条已知风险。

## 0. 如何使用本文

- 本文按 **Phase 0 → 1 → 1.5 → 2 → 3** 组织。每个 Phase 给出：**任务清单（带目标文件与签名）→ 退出标准 → 测试**。
- 所有"现状"主张都带 `file:line`，均经源码核对（§1）。实现者**不需要重新勘探**这些事实。
- 代码签名、DDL、协议契约是**规范性**的；散文是解释性的。若两者冲突，以签名/DDL 为准。
- 标注约定：`【现状】`=今天代码已是如此；`【新增】`=本文要求新建；`【改造】`=本文要求修改既有；`【消解】`=对应 FEASIBILITY 文档的风险条目。

## 1. 已核实的代码基线（实现起点，勿再勘探）

下面每条都经源码核对。它们是本文所有任务的前提，构成"我们从哪里出发"。

### 1.1 召回侧：当前 0% 实现

- `MemoryHooks` Protocol 定义了 `async def project(self, state: LoopState, *, token_budget: int) -> dict[str, Any] | None`（[hooks.py:187](src/pulsara_agent/runtime/hooks.py:187)）。唯一实现是 `NoopMemoryHooks.project()`，**恒返回 `None`**（[hooks.py:210](src/pulsara_agent/runtime/hooks.py:210)）。
- `DurableMemoryHooks` 与 `ReflectiveMemoryHooks` **都不 override `project()`**（[durable_hooks.py:33,84](src/pulsara_agent/memory/hooks/durable.py:33)），所以无论哪种 wiring，projection 每轮空转。
- `_project_memory`（[agent.py:402](src/pulsara_agent/runtime/agent.py:402)）emit `ProjectionRequested` → `await self.memory_hooks.project(...)` → set `state.memory_projection` → emit `ProjectionReady/Failed`。在主循环 [agent.py:168](src/pulsara_agent/runtime/agent.py:168) 被 `async for` 消费，**没有任何 `asyncio.wait_for`/超时包裹**。
- 注入 fence 在 [context.py:165](src/pulsara_agent/runtime/context.py:165)：`"Recalled Memory (source=fenced_recalled_memory; do not write it back as new memory):"`。projection dict 只读 `summary`（str）或 `items`（list），否则 `str(projection)`（[context.py:177](src/pulsara_agent/runtime/context.py:177)）。
- 全仓**无** `MemoryRecallService` / `memory_search` / `memory_get` / `search_index` / `recall_trace`。
- `LoopBudget.projection_token_budget = 2_000`，`max_tool_calls = 64`（[state.py:42](src/pulsara_agent/runtime/state.py:42)）；`LoopState.memory_projection: dict[str, Any] | None = None`（[state.py:70](src/pulsara_agent/runtime/state.py:70)）。

### 1.2 写入侧：能创建 ACTIVE/NEEDS_REVIEW/REJECTED，但无 lifecycle 转换

- `ExecutionEvidenceLedger`（[ledger.py:27](src/pulsara_agent/memory/canonical/ledger.py:27)）的 `submit_*` 全是**创建**：`gate.evaluate_*` → `_require_existing_nodes(evidence)` → `graph.put_jsonld(node)` → 逐条 evidence `_add_relation(evidence_id, memory.SUPPORTS, node_id)`。**没有任何更新已存在节点 / 状态转换的方法。**
- `_add_relation`（[ledger.py:427](src/pulsara_agent/memory/canonical/ledger.py:427)）= `get_jsonld` + 改 dict + `put_jsonld`，是**独立于节点写的第二次 HTTP mutation**。
- gate 产出三种创建期状态：`ACTIVE` / `NEEDS_REVIEW` / `REJECTED`（[write_gate.py:45](src/pulsara_agent/memory/canonical/write_gate.py:45)，`WriteDecision(accepted, status, reason, confidence_level)`）。
- **REJECTED 节点仍然落图**（gate-reject ≠ 不写）：`submit_preference` 无条件 `put_jsonld(... status=decision.status ...)`（[ledger.py:245](src/pulsara_agent/memory/canonical/ledger.py:245)），测试钉死 [test_memory_write_service.py:120](tests/test_memory_write_service.py:120)（`graph.has_jsonld(result.memory_id)` 为真）与 [test_durable_memory_contract.py:284](tests/test_durable_memory_contract.py:284)。**只有** `ValidationError` / 缺 evidence 抛异常时才 `record=None` 且不写节点（[write_service.py:56](src/pulsara_agent/memory/canonical/write_service.py:56)，[test_memory_write_service.py:140](tests/test_memory_write_service.py:140)）。
- **创建后 lifecycle 状态（STALE/SUPERSEDED/CONTRADICTED/ARCHIVED/DELETED）无任何生产者**：枚举定义在 [memory.py:76](src/pulsara_agent/ontology/memory.py:76)；`MemorySupersededEvent` / `MemoryMarkedStaleEvent` / `MemoryMaintenance*` 定义在 [events.py:348](src/pulsara_agent/event/events.py:348) 但**无人 emit**；无路径把节点改成这些状态。
- **`supersedes` / `contradicts` 边无生产者**：ledger 只写 `PROVIDES`（[ledger.py:179](src/pulsara_agent/memory/canonical/ledger.py:179)）和 `SUPPORTS`（[ledger.py:217,262,306,344,386](src/pulsara_agent/memory/canonical/ledger.py:217)）。`SUPERSEDES`/`CONTRADICTS` 仅存在于 ontology/codec。
- `governance.apply_decision`（[governance.py:46](src/pulsara_agent/memory/governance/executor.py:46)）是**非原子四写**：`memory_write_service.submit`（写 graph）→ `event_log.extend`（写事件库）→ `_append_governance_candidate_if_needed`（写候选池）→ `_append_decision`（独立 `psycopg.connect` 写 `memory_governance_decisions`，[candidate_pool.py:296](src/pulsara_agent/memory/candidates/pool.py:296)）。无 2PC / outbox / 幂等键去重；只有 `governance_batch_id` 作关联键。
- `dedupe.already_exists`（[dedupe.py:56](src/pulsara_agent/memory/governance/dedupe.py:56)）= `find_by_type` 扫描 + 比对 statement/scope + status 白名单 `{ACTIVE, NEEDS_REVIEW}`。

### 1.3 存储 / 图后端

- `GraphStore` Protocol（[store.py:13](src/pulsara_agent/graph/store.py:13)）：`put_jsonld / get_jsonld / has_jsonld / find_by_type / query / update / delete_graph`。`DEFAULT_GRAPH_ID = "graph:default"`。`find_by_type`、`query` 返回 `list[dict]`。
- `InMemoryGraphStore`：`query()` 与 `update()` **抛 `NotImplementedError`**（[in_memory.py:69](src/pulsara_agent/graph/in_memory.py:69)）；有非 Protocol 的 `add_relation` 辅助（[in_memory.py:46](src/pulsara_agent/graph/in_memory.py:46)）。`find_by_type` 全表扫 + `deepcopy`。
- `OxigraphGraphStore`：`base_url="http://localhost:7878"`、`timeout_seconds=10.0`（[oxigraph.py:26](src/pulsara_agent/graph/oxigraph.py:26)）；**同步阻塞** `urllib.request.urlopen`（[oxigraph.py:118,140](src/pulsara_agent/graph/oxigraph.py:118)）；`query(bindings=...)` 抛 `NotImplementedError`（[oxigraph.py:103](src/pulsara_agent/graph/oxigraph.py:103)）；`find_by_type` = 1 SELECT + 每 subject 一次 `get_jsonld`（N+1）。**非嵌入式**，`pyoxigraph` 不是依赖。
- **codec 只给 `xsd:boolean` / `xsd:integer` 打类型**（[jsonld_codec.py:14](src/pulsara_agent/graph/jsonld_codec.py:14)）；时间戳等一切其他字面量 round-trip 成**无类型字符串**（序列化 [jsonld_codec.py:295](src/pulsara_agent/graph/jsonld_codec.py:295)，解析 [jsonld_codec.py:91](src/pulsara_agent/graph/jsonld_codec.py:91)）。SPARQL 对它们只能做字典序比较，**不能 dateTime 比较**。
- Postgres 现有表：`sessions / runs / turns / agent_events / tool_execution_records / artifacts`（[postgres_schema.py:11](src/pulsara_agent/storage/postgres_schema.py:11)）+ 候选池的 `memory_candidates / memory_governance_decisions`（[candidate_pool.py:409](src/pulsara_agent/memory/candidates/pool.py:409)，FK 到 `sessions/runs/turns`）。**无** canonical memory 表。DSN：`settings.storage.postgres_dsn`，env `PULSARA_POSTGRES_DSN`（[settings.py:19](src/pulsara_agent/settings.py:19)）。

### 1.4 实体与本体

- 5 类：`Claim / Decision / Preference / ActionBoundary / Observation`（[memory.py:13](src/pulsara_agent/ontology/memory.py:13)）。共享字段：`statement, scope, status, confidence_level, verification_status, source_authority, created_at, updated_at, gate_reason, evidence: tuple[NodeRef,...]`。`Decision` 多 `based_on: tuple[NodeRef,...]`；`ActionBoundary` 多 `applies_when: str` 与 `do_not_apply_when: str`（**自由文本**，[action_boundary.py:21](src/pulsara_agent/entities/memory/action_boundary.py:21)）。
- 枚举：`NodeStatus`（8 值）、`SourceAuthority`（6 值）、`VerificationStatus`（6 值）、`ConfidenceLevel`（4 值）。
- 工具：`Tool` Protocol（[base.py:26](src/pulsara_agent/tools/base.py:26)）= `name, description, parameters, is_read_only, is_concurrency_safe, execute(call)->ToolExecutionResult`。注册在 `build_core_tool_registry`（[registry.py:26](src/pulsara_agent/tools/builtins/registry.py:26)）。`remember_*` 工具在 [builtins/memory.py:113](src/pulsara_agent/tools/builtins/memory.py:113)。

## 2. 规范性不变量（实现必须满足）

这些是验收任何 PR 的硬约束。每条可被测试直接断言。

### 2.1 写入侧

写入有**两条不同的入口**，不可混淆（实现者常误以为只有一条）：

- **W1（创建入口）**：**新建** canonical memory 节点必经 `MemoryWriteService → MemoryWriteGate → ExecutionEvidenceLedger`。producer 只写 CandidatePool；只有 governance 能把 candidate 走这条链路写成 canonical。【现状成立】
- **W1b（生命周期入口，Phase 2 引入）**：**更新已有节点**的生命周期状态（ACTIVE→SUPERSEDED/STALE/CONTRADICTED）与写 `supersedes`/`contradicts` 边，**不走** `MemoryWriteService.submit`（它是创建语义），而是由 governance/maintenance 调用专门的 `MemoryLifecycle`（§7.2）。该入口同样要有审计事件、幂等键、事务边界。**不要把 lifecycle transition 塞进 `MemoryWriteService.submit`。**
- **W2**：normal recall 永不读 CandidatePool。【现状成立，召回未实现故 trivially 真；新代码必须保持】
- **W3（新增）**：W1 与 W1b 的一次写入——节点 / 状态变更、其 evidence/relation 边、governance decision、index-dirty/outbox 记录——必须**在同一逻辑提交中可恢复一致**。同库时用单事务（§4.2 的 `MemoryWriteUnitOfWork`）；不能跨存储原子提交时，必须有幂等键、transactional outbox、reconciliation job、damaged-node 检测、write retry/repair。【消解 split-brain 与半写入节点】

### 2.2 召回侧

- **R1**：normal recall 的**最终事实裁判只认 canonical memory store**；SearchIndex / lexical / BM25 / vector 只产候选 id；候选必须回 canonical store 做 status/scope/type/evidence/relation 裁定。
- **R2**：`canonical store` 物理上含 REJECTED 行（§1.2），因此 **"canonical" 性由 recall filter 定义，不由存储定义**。filter 不是可选清理，是构成性步骤。
- **R3**：projection 是 ephemeral，不写回 EventLog 用户原文；recalled memory 不能被当成新 evidence 写回（需 §Phase1 的代码护栏，不止 prompt）。
- **R4**：recency 不创造相关性——只有 lexical/BM25/graph 已建立相关性后，freshness 才能加分。

### 2.3 两类 canonical filter（必须区分，勿混写）

这是上一轮明确的修正点。§5.4 类过滤器**性质不同**，实现与测试要分开对待：

| 类别 | 过滤器 | 今天是否做实活 | 依据 |
|---|---|---|---|
| **承重（load-bearing）** | `status NOT IN (REJECTED)`、`status IN (ACTIVE[, NEEDS_REVIEW diagnostic])`、scope 可达、type 允许、非 projection-echo | **是**。REJECTED 行真实物化（§1.2），漏过会把被拒记忆当事实注入 | [dedupe.py:66](src/pulsara_agent/memory/governance/dedupe.py:66) 的白名单正为此 |
| **惰性（scaffolding）** | `NOT superseded / NOT stale / NOT contradicted / NOT archived/deleted` | **否，恒真空过滤**。无 producer 能把节点转进这些状态（§1.2） | 前向兼容占位，待 Phase 2 lifecycle producer 落地后才有意义 |

- **F1**：v1 canonical filter **只保证过滤当前已物化的 status/relation**；**不得声称**已实现自动 lifecycle freshness。
- **F2**：实现者与文档都必须知道哪些 filter 被测试真正 exercise（承重类）、哪些只是占位（惰性类）。惰性 filter 现在写进代码是对的（前向兼容），但其测试只能断言"语法存在且不误伤 ACTIVE"，不能断言"能正确过滤 superseded"——因为造不出 superseded 节点，直到 Phase 2。

### 2.4 解释正确性（graph-aware explainer 的硬门槛）

- **E1**：explanation 的每条 claim 必须 grounded 在**已物化的边/字段**上。`confabulation rate = 0` 是 blocker，与 superseded-leak rate = 0 同级。
- **E2**："无解释"是合法输出。证据不足时只返回真实信号（lexical/BM25/scope/type/status），**不得**因"让产品显得聪明"而猜测 lifecycle relation。
- **E3**：explanation 必须**结构化**（§Phase2 的 `{claim, grounded_on:[edge_id|signal_id]}`），prose 仅为渲染层；否则 `confab=0` 无法进 CI。

## 3. Canonical Memory Substrate（Phase 0 地基）

召回的事实裁判源。本文选择 **Postgres 作为 v1 默认 canonical recall substrate**，`GraphStore` 保留为可插拔 seam，Oxigraph 退居 v2/v3 可选后端。【消解：跨库 split-brain、N+1-over-HTTP、阻塞 urllib、InMemory 不能 SPARQL、时间戳无类型】

### 3.1 为什么是 Postgres 而非 Oxigraph（一句话）

canonical 节点与候选池/治理日志同库 → `apply_decision` 可经 `MemoryWriteUnitOfWork` 单事务（消解 W3）；1–2 跳 typed 扩展是递归 CTE 甜区；`timestamptz` 让 recency 正确；派生表（如 `memory_search_index`）可对 `memory_nodes` 加 FK 杜绝悬空索引 id（`memory_relations` 因可指向运行时节点，按 §3.2 不加 FK，靠 reconciliation 保完整）。Oxigraph 的进入条件见 §Phase3。

### 3.2 DDL：通用 `graph_documents` + canonical projection `memory_nodes` / `memory_relations`

【新增】放入新文件 `src/pulsara_agent/storage/memory_schema.py`，与 `postgres_schema.py` 并列，同 DSN 同库。

**关键设计（修正）**：`GraphStore.put_jsonld` 是**通用 JSON-LD 存储接口**，`ExecutionEvidenceLedger` 用它写的不止 memory conclusion 节点，还有 ToolResult / Artifact / Evidence / Turn 等运行时节点（[ledger.py:35](src/pulsara_agent/memory/canonical/ledger.py:35)）。一张 memory-specific 的表（带 `memory_type/status/statement`）装不下这些。因此分两层：

1. **`graph_documents`**：通用 JSON-LD 文档存储，`PostgresGraphStore` 在它之上实现 `GraphStore` Protocol。任何 `@id` 文档都能存。
2. **`memory_nodes` / `memory_relations`**：**canonical memory 投影**，供召回 typed 查询与索引，从 `graph_documents` 派生（写时同步投影，见 §3.3）。注意两者投影范围不同：`memory_nodes` **只投影 5 类 memory 节点**；`memory_relations` **投影所有文档中与 canonical memory 查询相关的 typed relation，包括 evidence/runtime 源节点的出边**（如 `evidence_id → supports → memory_id`）——否则 `CanonicalNodeView.evidence_ids` 会永远为空（详见 §3.3）。

所有表都带 `graph_id`，保持 `GraphStore` 的 namespace 分区语义（[store.py:13](src/pulsara_agent/graph/store.py:13)），避免 `graph:runtime/<session>`、测试 graph、默认 graph 互相污染。

```sql
-- 1) 通用 JSON-LD 文档存储：GraphStore 的真实落点（含 ToolResult/Artifact/Turn/memory 等一切节点）
CREATE TABLE IF NOT EXISTS graph_documents (
    graph_id    TEXT NOT NULL,
    id          TEXT NOT NULL,                    -- JSON-LD @id
    type        TEXT,                             -- JSON-LD @type（首类型，便于 find_by_type）
    payload     JSONB NOT NULL,                   -- 完整 JSON-LD 文档（含内联 relation 谓词）
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (graph_id, id)
);
CREATE INDEX IF NOT EXISTS idx_graph_documents_type ON graph_documents(graph_id, type);

-- 2) canonical memory 投影：仅 5 类 memory 节点，typed 列供召回过滤/排序/索引
CREATE TABLE IF NOT EXISTS memory_nodes (
    graph_id            TEXT NOT NULL,
    id                  TEXT NOT NULL,                    -- e.g. "preference:uuid"
    memory_type         TEXT NOT NULL,                    -- Claim|Decision|Preference|ActionBoundary|Observation
    scope               TEXT NOT NULL,
    status              TEXT NOT NULL,                    -- NodeStatus value
    statement           TEXT NOT NULL,
    summary             TEXT,
    source_authority    TEXT,
    verification_status TEXT,
    confidence_level    TEXT,
    applies_when        TEXT,                             -- ActionBoundary 自由文本（v1）
    do_not_apply_when   TEXT,
    created_at          TIMESTAMPTZ NOT NULL,             -- 真 timestamptz，非字符串
    updated_at          TIMESTAMPTZ NOT NULL,
    stale_after         TIMESTAMPTZ,
    expires_at          TIMESTAMPTZ,
    fts                 TSVECTOR,                          -- Phase 1.5 填充；Phase 1 可空
    PRIMARY KEY (graph_id, id)
);
CREATE INDEX IF NOT EXISTS idx_memory_nodes_type_status_scope
    ON memory_nodes(graph_id, memory_type, status, scope);
CREATE INDEX IF NOT EXISTS idx_memory_nodes_status ON memory_nodes(graph_id, status);
CREATE INDEX IF NOT EXISTS idx_memory_nodes_updated_at ON memory_nodes(graph_id, updated_at);

-- 3) typed 关系投影（含未来 supersedes/contradicts 的正反向边）
CREATE TABLE IF NOT EXISTS memory_relations (
    graph_id    TEXT NOT NULL,
    source_id   TEXT NOT NULL,
    predicate   TEXT NOT NULL,                            -- supports|supersedes|contradicts|basedOn|...
    target_id   TEXT NOT NULL,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (graph_id, source_id, predicate, target_id)
);
CREATE INDEX IF NOT EXISTS idx_memory_relations_source
    ON memory_relations(graph_id, source_id, predicate);
CREATE INDEX IF NOT EXISTS idx_memory_relations_target
    ON memory_relations(graph_id, target_id, predicate);   -- 逆向边（supersededBy = WHERE target_id=? AND predicate='supersedes'）
```

设计要点：

- **`graph_documents` 是 GraphStore 的真相落点**；`memory_nodes`/`memory_relations` 是其上的 canonical memory 投影，二者由 §3.3 的 put 路径在**同事务**内一起写，不会漂移。`payload` 不在 memory_nodes 重复存（已在 graph_documents），memory_nodes 只持 typed 列。
- **逆向边零成本**：`memory_relations` 双向可查（`idx_..._target`），不必物化 `supersededBy`。
- `created_at/updated_at/stale_after/expires_at` 是真 `timestamptz`——recency 比较正确（在 Postgres 后端彻底绕开 codec 时间戳问题）。
- **不建** FK 从 `memory_relations` 到 `memory_nodes`：关系可能指向运行时节点（如 evidence 指向 ToolResult，那是 `graph_documents` 行而非 `memory_nodes` 行）或尚未写入的节点；引用完整性由 reconciliation（§Phase1.5）保证，不由 FK 强加。

### 3.3 `PostgresGraphStore`：通用 JSON-LD 存储 + canonical 投影同步

【新增】`src/pulsara_agent/graph/postgres.py`，实现 `GraphStore` Protocol（[store.py:13](src/pulsara_agent/graph/store.py:13)）。**它是通用文档存储**（落 `graph_documents`），不是 memory-specific 存储——因此能承接 ledger 写的 ToolResult/Artifact/Turn/memory 全部节点。

**关系维护（修正）**：现有 `GraphStore` Protocol **没有** `add_relation`；`ExecutionEvidenceLedger._add_relation` 的真实做法是 `get_jsonld(source)` → 改 dict → `put_jsonld(document)`（[ledger.py:427](src/pulsara_agent/memory/canonical/ledger.py:427)），即**关系以谓词内联在 JSON-LD 文档里**回写。因此 `PostgresGraphStore.put_jsonld` **必须从文档解析出 relation 谓词，同步进 `memory_relations`**——否则 SUPPORTS/PROVIDES 永远不会进投影表，`CanonicalNodeView.evidence_ids/outgoing/incoming` 会失真。我们**不**新增 `add_relation` 到协议（保持协议不变、不改 ledger）。

两条必须钉死的投影语义：

1. **`memory_relations` 是 `graph_documents` 的投影，不是独立 truth**。任何写关系的路径（v1 的 ledger、Phase 2 的 lifecycle）都必须**先改 `graph_documents` 里源文档的 JSON-LD relation 字段，再调用同一个 `_sync_relations_from_document(doc)` helper** 投影出 `memory_relations` 行。禁止任何路径直接 `INSERT memory_relations` 而不动源文档——否则通用层与投影分叉。
2. **relation-sync 覆盖所有 `graph_documents` 文档，不只 5 类 memory 节点**。关键：`SUPPORTS` 的 source 是 evidence/runtime 节点（ledger 写的是 `evidence_id → supports → memory_id`，[ledger.py:216](src/pulsara_agent/memory/canonical/ledger.py:216)），而 evidence 文档不在 `memory_nodes` 投影里。若只对 5 类 memory 文档做 relation-sync，`CanonicalNodeView.evidence_ids` 会**永远为空**。因此 `_sync_relations_from_document` 对每个被 `put_jsonld` 的文档都执行，无论其 `@type` 是否属于 5 类。

```python
class PostgresGraphStore:  # 结构等价于 GraphStore Protocol；通用 JSON-LD 存储
    def __init__(self, dsn: str, *, graph_id: str = DEFAULT_GRAPH_ID) -> None: ...

    def put_jsonld(self, document: dict[str, Any], graph_id: str | None = None) -> None:
        # 单事务内：
        #  1) UPSERT graph_documents(graph_id, id, type, payload)   —— 通用落点，承接一切节点
        #  2) 若 @type ∈ 5 类 memory：UPSERT memory_nodes 投影（拆 typed 列）
        #  3) self._sync_relations_from_document(document, graph_id)  —— 对【任何】文档执行
        # 注：ledger 的 _add_relation 是 get+改+put 整文档回写，故关系一定经由本方法落到投影表。

    def _sync_relations_from_document(self, document, graph_id) -> None:
        # 从 document 的 relation 谓词（supports/contradicts/supersedes/basedOn/hasEvidence/provides/...）
        # 投影 memory_relations：DELETE WHERE (graph_id, source_id=document_id) 再 INSERT 当前所有出边，
        # 保证投影与源文档一致。对 evidence/runtime/memory 文档一视同仁。
        # 这是 v1 ledger 与 Phase 2 lifecycle 共用的【唯一】关系投影入口。

    def get_jsonld(self, node_id: str, graph_id: str | None = None) -> dict[str, Any]:
        # SELECT payload FROM graph_documents WHERE graph_id=%s AND id=%s；KeyError if 缺。

    def has_jsonld(self, node_id: str, graph_id: str | None = None) -> bool: ...

    def find_by_type(self, type_name: Term, graph_id: str | None = None) -> list[dict[str, Any]]:
        # SELECT payload FROM graph_documents WHERE graph_id=%s AND type=%s（索引命中）。

    def query(self, sparql: str, bindings: dict[str, Any] | None = None) -> list[dict[str, Any]]:
        raise NotImplementedError("PostgresGraphStore exposes typed reads via MemoryQuery, not raw SPARQL")

    def update(self, sparql: str) -> None:
        raise NotImplementedError("PostgresGraphStore uses typed mutations, not raw SPARQL")

    def delete_graph(self, graph_id: str) -> None: ...   # 删 graph_documents/memory_nodes/memory_relations 中该 graph_id 行
```

**关键判断**：v1 召回**不依赖 raw SPARQL**。`PostgresGraphStore` 的 `query/update` 抛 `NotImplementedError`，召回改走 §3.4 的 typed `MemoryQuery`。（注意：这与 `OxigraphGraphStore` 不同——后者支持无 bindings 的 `query/update`，[oxigraph.py:101](src/pulsara_agent/graph/oxigraph.py:101)；只是带 bindings 的 query 未实现。本文不声称三后端 query 行为一致，只声称**v1 recall 不依赖 raw SPARQL**。）

### 3.4 `MemoryQuery`：召回专用的 typed 查询接口

【新增】`src/pulsara_agent/memory/canonical/query.py`。这是 R1 裁判源的程序化入口，**取代** FEASIBILITY 文档批评的"逐候选 SPARQL 扩展"。

```python
@dataclass(frozen=True, slots=True)
class CanonicalNodeView:
    id: str
    memory_type: str
    scope: str
    status: memory.NodeStatus
    statement: str
    summary: str | None
    source_authority: memory.SourceAuthority | None
    verification_status: memory.VerificationStatus | None
    confidence_level: memory.ConfidenceLevel | None
    applies_when: str | None
    do_not_apply_when: str | None
    created_at: datetime
    updated_at: datetime
    evidence_ids: tuple[str, ...]          # 来自 memory_relations: predicate=supports, target=this
    outgoing: tuple[tuple[str, str], ...]  # (predicate, target_id) 直接出边
    incoming: tuple[tuple[str, str], ...]  # (predicate, source_id) 直接入边（逆向）

class MemoryQuery(Protocol):
    # 三个方法都带 graph_id（默认解析为 DEFAULT_GRAPH_ID），与 GraphStore / DDL 的分区语义一致。
    def fetch_nodes(self, ids: Sequence[str], *, graph_id: str | None = None) -> list[CanonicalNodeView]:
        """批量取节点 + 其直接边。一次查询，非 N+1。"""

    def lexical_candidates(
        self, *, terms: Sequence[str], scopes: Sequence[str] | None,
        types: Sequence[str] | None, limit: int, graph_id: str | None = None,
    ) -> list[tuple[str, float]]:
        """exact channel：按 id/alias/scope/type/literal 精确命中，返回 (memory_id, raw_score)。"""

    def fts_candidates(
        self, *, query_text: str, scopes: Sequence[str] | None,
        types: Sequence[str] | None, limit: int, graph_id: str | None = None,
    ) -> list[tuple[str, float]]:
        """BM25/FTS channel。Phase 1 可用朴素 lexical 实现，Phase 1.5 换 tsvector/GIN。"""
```

`fetch_nodes` 的 Postgres 实现（核心，**一次往返取节点+边**；`%(gid)s` = 解析后的 graph_id）：

```sql
-- 节点（typed 列来自 memory_nodes 投影）
SELECT id, memory_type, scope, status, statement, summary, source_authority,
       verification_status, confidence_level, applies_when, do_not_apply_when,
       created_at, updated_at
FROM memory_nodes WHERE graph_id = %(gid)s AND id = ANY(%(ids)s);
-- 出边
SELECT source_id, predicate, target_id FROM memory_relations
WHERE graph_id = %(gid)s AND source_id = ANY(%(ids)s);
-- 入边（逆向，supersededBy/contradictedBy 等由此得来，无需物化）
SELECT source_id, predicate, target_id FROM memory_relations
WHERE graph_id = %(gid)s AND target_id = ANY(%(ids)s);
```

三条查询 = **每轮 3 次 DB 往返**（非 K×fanout 次 HTTP）。这是 FEASIBILITY 文档要求的"1 coarse + 1 batched expansion"的落地形态。【消解 N+1】`MemoryQuery` 的 `fetch_nodes/lexical_candidates/fts_candidates` 均接受 `graph_id`，默认解析为 `DEFAULT_GRAPH_ID`，保持与 `GraphStore` 一致的分区语义。

## 4. Phase 0：地基与安全（无任何 fancy）

目标：在写召回逻辑之前，先让底座可查询、可超时、写入可恢复一致、评测能挡回退。

### 4.0 任务总览

| # | 任务 | 目标文件 | 消解 |
|---|---|---|---|
| 0.1 | `graph_documents` + `memory_nodes` + `memory_relations` DDL（均带 `graph_id`） | `storage/memory_schema.py`【新增】 | substrate 缺失 / graph 污染 |
| 0.2 | `PostgresGraphStore`（通用 JSON-LD 存储 + 投影同步）+ `MemoryQuery` | `graph/postgres.py`【新增】、`memory/canonical/query.py`【新增】 | InMemory 不能查 / N+1 / 关系失真 |
| 0.3 | projection 调用加 timeout/fail-soft | `agent.py`【改造】、`state.py`【改造】 | 阻塞 hot path |
| 0.4 | 异步可取消的存储访问 | `graph/postgres.py`（async DB 或 to_thread） | 同步 urllib 冻结 loop |
| 0.5 | `MemoryWriteUnitOfWork`：canonical 写 + decision + outbox 同事务 + 幂等键 | `memory/canonical/unit_of_work.py`【新增】、`memory/governance/executor.py`【改造】、`memory/candidates/pool.py`【改造】、`storage/memory_schema.py` | split-brain |
| 0.6 | timestamp 用 `timestamptz` | DDL（已在 0.1） | recency 不可比较 |
| 0.7 | golden eval harness + gate skeleton（floor 待 Phase 1 末冻结） | `evals/recall/`【新增】 | 不可证伪 |
| 0.8 | CI gate skeleton 接线（leak=0 即可生效；floor 占位） | CI 配置 + `evals/recall/runner.py` | 门槛只是文档 |

### 4.1 任务 0.3：projection timeout / fail-soft（最高优先）

【改造】[agent.py:416](src/pulsara_agent/runtime/agent.py:416) 当前是裸 `await self.memory_hooks.project(...)`。改为：

```python
# state.py：LoopBudget 增加字段
recall_hard_timeout_ms: int = 500      # cheap auto projection 总预算
# agent.py：_project_memory 内
try:
    projection = await asyncio.wait_for(
        self.memory_hooks.project(state, token_budget=self.budget.projection_token_budget),
        timeout=self.budget.recall_hard_timeout_ms / 1000,
    )
except asyncio.TimeoutError:
    state.memory_projection = None
    yield await self.runtime_session.emit(ProjectionFailedEvent(..., error="recall_timeout"), state=state)
    return
except Exception as exc:
    state.memory_projection = None
    yield await self.runtime_session.emit(ProjectionFailedEvent(..., error=f"{type(exc).__name__}: {exc}"), state=state)
    return
```

**关键前提（0.4）**：`wait_for` 只对**真正可 await/可取消**的协程有效。当前 `OxigraphGraphStore` 用同步阻塞 `urlopen`，`wait_for` 取消不了它（线程卡在 C 层 socket read）。因此：

- v1 默认走 `PostgresGraphStore`，DB 访问用 **async 驱动**（`psycopg` 3 的 async 连接 / 连接池）或 `asyncio.to_thread` 包裹同步调用，使其成为真正的 await 点。
- `OxigraphGraphStore` 在改成 async pooled client 之前（§Phase3 进入条件），**不得**进入默认召回路径。

### 4.2 任务 0.5：canonical 写原子化（消解 W3 split-brain）

【改造】[governance.py:46](src/pulsara_agent/memory/governance/executor.py:46) 当前四个独立写无事务，且 `PostgresCandidatePool.append_decision` 自己 `psycopg.connect`（[candidate_pool.py:296](src/pulsara_agent/memory/candidates/pool.py:296)）——只改 `governance.py` 不够。

**真相层定位（修正，与产品文档对齐）**：Postgres operational tables（`graph_documents` / `memory_nodes` / `memory_relations` / `memory_governance_decisions`）是 canonical memory 的真相源与提交锚点；**EventLog 是审计 / 通知 / 重放线索，不是这一层的提交锚点**。事件在 Postgres 事务提交成功后才 emit。

**核心：`MemoryWriteUnitOfWork`（共享 connection/transaction）**。【新增】`src/pulsara_agent/memory/canonical/unit_of_work.py`。问题根因是三个写入方各自开连接；解法是让它们绑定到同一 connection：

```python
class MemoryWriteUnitOfWork:
    """绑定单个 psycopg connection/transaction，供 canonical 写、decision、outbox 共用。"""
    def __enter__(self) -> "MemoryWriteUnitOfWork": ...      # BEGIN
    def __exit__(self, *exc) -> None: ...                    # COMMIT / ROLLBACK
    # connection-bound 组件（都用 self.conn，不再各自 connect）：
    graph: PostgresGraphStore                # put_jsonld 写 graph_documents + 投影，用 self.conn
    decisions: CandidateDecisionRepository   # 取代 PostgresCandidatePool.append_decision 的独立 connect
    outbox: OutboxRepository                 # 写 memory_write_outbox，用 self.conn
    # 关键：UoW 现造一个【绑定到 self.graph 的】MemoryWriteService，
    # 其内部 ExecutionEvidenceLedger.graph == self.graph（不是 wiring 期预造的旧 graph）。
    memory_write_service: MemoryWriteService     # = MemoryWriteService(ExecutionEvidenceLedger(graph=self.graph, ...))
    lifecycle: MemoryLifecycle                    # Phase 2：也绑定 self.graph / self.outbox
```

配套重构：

- 【改造】`PostgresGraphStore` 接受**可注入的 connection**（UnitOfWork 内复用，独立调用时自开自闭）。
- 【改造】把 `PostgresCandidatePool.append_decision` 的写入逻辑抽到 `CandidateDecisionRepository`，可绑定外部 connection（保留旧入口作薄包装，避免破坏其他调用方）。
- 【关键改造】**`apply_decision` 必须调用 `uow.memory_write_service.submit(...)`，而不是 `self.memory_write_service.submit(...)`**。当前 `MemoryGovernanceExecutor` 持有 wiring 期预造好的 `self.memory_write_service`（其 ledger 绑的是旧 graph/旧连接，[governance.py:83](src/pulsara_agent/memory/governance/executor.py:83)）；若沿用它，`submit()` 仍走旧连接，"同事务写"落空。`MemoryWriteService`/`ExecutionEvidenceLedger` 已是数据类（[ledger.py:27](src/pulsara_agent/memory/canonical/ledger.py:27)：`graph` 是字段），UoW 在 `__enter__` 时用 `self.graph` 现造一个等价 service，让 ledger 的 `put_jsonld`/`_add_relation` 全部走 UoW 的同一 connection。
- 【关键改造】**dedupe / `already_exists` 也必须使用 `uow.graph`**。当前 `apply_decision` 在写入前会跑 dedupe 检查（[governance.py:71](src/pulsara_agent/memory/governance/executor.py:71)），`already_exists` 接受一个 `graph` 参数做 `find_by_type` 扫描（[dedupe.py:56](src/pulsara_agent/memory/governance/dedupe.py:56)）。若它仍走 wiring 期的 `self.graph`，会出现"在旧连接上判重、在 UoW 新连接上写入"的读写错位——同一 batch 内刚写的节点判不到、或判重读到事务外的旧快照。因此 dedupe 必须 `already_exists(candidate, uow.graph, graph_id=...)`，与写入共用同一 connection/事务视图。
- 【改造】[governance.py:46](src/pulsara_agent/memory/governance/executor.py:46) `apply_decision` 用 `with MemoryWriteUnitOfWork(dsn) as uow:` 包裹整段写入，内部一律用 `uow.*`（`uow.graph` 跑 dedupe / `uow.memory_write_service` / `uow.decisions` / `uow.outbox` / `uow.lifecycle`），不再触碰 `self.memory_write_service` 或 `self.graph`。

**outbox 表**（加入 `memory_schema.py`，带 `graph_id`）：

```sql
CREATE TABLE IF NOT EXISTS memory_write_outbox (
    outbox_id           TEXT PRIMARY KEY,
    graph_id            TEXT NOT NULL,
    governance_batch_id TEXT NOT NULL,
    decision_id         TEXT NOT NULL,        -- 该 outbox 项对应的治理 decision（与 memory_governance_decisions.decision_id 对齐）
    target_entry_key    TEXT NOT NULL,        -- 决策目标的稳定键：单 target 即 entry_id；merge 用排序后 entry_ids 的 hash
    payload             JSONB NOT NULL,        -- 待投影的节点/边/索引-dirty 描述
    status              TEXT NOT NULL DEFAULT 'pending',  -- pending|applied|failed
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    applied_at          TIMESTAMPTZ,
    UNIQUE (governance_batch_id, decision_id)   -- 幂等键，重放安全
);
```

**幂等键设计（前瞻 merge）**：用 `decision_id` 作幂等键，而非裸 `target_entry_id`。理由：`submit_as_is` / `correct` 是单 target，但 `merge_and_submit` 是**多个 target_entry_ids → 一个决策**，裸 `target_entry_id` 装不下，将来实现 merge 又得改 schema。`target_entry_key` 给出稳定的目标指纹——单 target 时即 `entry_id`，merge 时为**排序后 entry_ids 列表的 hash**——既可追溯目标集合，又不参与唯一约束（唯一性由 `(governance_batch_id, decision_id)` 保证，一个决策一行）。

落地步骤：

1. **同事务写**（在 `MemoryWriteUnitOfWork` 内）：`graph_documents` UPSERT + `memory_nodes`/`memory_relations` 投影 + `memory_governance_decisions` + `memory_write_outbox(status=pending)`，**共享一个 connection、一次 COMMIT**（同库，可做到）。替代当前"graph 写 + 独立 psycopg 写"分离。
2. **事件 emit**：事务提交成功后再 `event_log.extend`（EventLog 是审计/通知/重放，不是真相源、不是提交锚点）。
3. **reconciliation job**【新增】`memory/canonical/reconcile.py`：扫 `memory_write_outbox WHERE status='pending' AND created_at < now()-interval` 重放/修复；扫"有节点但无 decision 行"的损坏态。
4. **幂等**：`UNIQUE(governance_batch_id, decision_id)` 让崩溃重放不重复写——对单 target 与 merge 多 target 一致适用。

> 注意：若团队短期内仍保留 Oxigraph 作 canonical（不推荐），graph 写在另一进程，**无法**与 Postgres 同事务，UnitOfWork 退化为"Postgres 部分单事务 + Oxigraph 部分靠 outbox 补偿"；此时 §Phase3 风格的图扩展遇"节点在、预期边缺失"必须判为 NEEDS_REVIEW/不召回。

### 4.3 任务 0.7：golden eval harness（最小可跑）

【新增】目录结构：

```text
evals/recall/
  fixtures/
    v1_golden.jsonl          # 每行一个 case（见下）
  runner.py                  # 加载 fixtures、跑 recall、算指标、对比 baseline
  baseline/
    v1_floor.json            # frozen 基线快照（§7）
  config.yaml                # golden set 版本、指标定义、floor 引用
```

case schema（`v1_golden.jsonl` 每行）：

```json
{
  "case_id": "pref-venv-recall",
  "seed_memory": [
    {"id": "preference:1", "memory_type": "Preference", "scope": "ctx:workspace/pulsara_agent",
     "status": "active", "statement": "测试和本地命令优先使用根目录 .venv 的 uv 环境"}
  ],
  "query": "帮我跑一下测试",
  "expected_included": ["preference:1"],
  "expected_excluded": [],
  "latency_budget_ms": 500,
  "projection_char_budget": 1200,
  "must_have_warning": false
}
```

`runner.py` 输出指标（朴素即可）：`included 命中率`、`excluded 漏过率`、`p50/p95 latency`、`projection 预算超限数`、`confabulation count`（Phase 2 起）。

### 4.4 任务 0.8：CI gate（Phase 0 只建 skeleton，floor 待 Phase 1 末冻结）

**分期（修正）**：frozen lexical+BM25 floor 锚定的是 **v1 RRF 融合输出**（§9 R-Floor-1），它要等 Phase 1 召回真正跑起来才存在。所以 Phase 0 **不能**直接拿 floor 做门槛——Phase 0 只建 runner/gate 的**骨架与可立即生效的硬约束**，`v1_floor.json` 在 **Phase 1 末尾生成并冻结**。

【新增】CI 步骤调用 `python -m evals.recall.runner --gate`：

```text
# Phase 0 起即可生效（不依赖 floor）：
candidate_leak      = 0    # 候选池内容出现在 projection
rejected_leak       = 0    # REJECTED 节点出现在 projection
p95_latency        <= budget

# Phase 1 末尾冻结 v1_floor.json 后启用：
quality            >= frozen lexical+BM25 floor（§9 的指标与快照）

# Phase 2 起有意义后启用（此前数据上恒 0，门槛先挂"占位/不阻塞"）：
superseded_leak     = 0
confabulation_rate  = 0
```

gate 的每条约束带一个"自哪个 Phase 起 enforce"的开关；未到期的约束在 runner 里标记为 informational（打印但不 fail），到期后切成 blocking。这样 CI 接线在 Phase 0 就存在、可演进，不会出现"门槛引用了还不存在的 floor"。

> §11"eval 能在本地跑只是工具存在；进入 CI 才是门槛"——0.8 是该门槛的骨架，随 Phase 推进逐条收紧。

### 4.5 Phase 0 退出标准

- 能用 `MemoryQuery.fetch_nodes` / `lexical_candidates` 查询 canonical active memory（Postgres 后端，按 `graph_id` 分区）。
- `_project_memory` 受 `recall_hard_timeout_ms` 约束，超时走 `ProjectionFailedEvent` 且不阻塞主响应。
- governance 写经 `MemoryWriteUnitOfWork` 单事务；写失败不产生静默 split-brain（outbox + reconciliation 可检测可修复）。
- `python -m evals.recall.runner` 本地稳定运行；`--gate` 的 leak/latency 约束在 CI 生效；floor 约束已接线但标 informational（待 Phase 1 末冻结后转 blocking）。

## 5. Phase 1：lexical exact + BM25/FTS 召回（产品可用底盘）

目标：让 `project()` 不再空转；用户偏好能被自动召回；显式工具可查；护栏到位。**不引入 embedding。**

### 5.0 任务总览

| # | 任务 | 目标文件 | 类型 |
|---|---|---|---|
| 1.1 | `MemoryRecallService` 协议 + 默认实现 | `memory/recall/service.py`【新增】 | 核心 |
| 1.2 | lexical exact channel | `memory/recall/service.py` | 核心 |
| 1.3 | BM25/FTS channel（v1 朴素 lexical 起步） | `memory/recall/service.py` | 核心 |
| 1.4 | bounded union + RRF 融合 | `memory/recall/service.py` | 核心 |
| 1.5 | canonical filter（承重类，§2.3） | `memory/recall/service.py` | 核心 |
| 1.6 | `project()` 真正实现 | `memory/hooks/durable.py`【改造】 | 接线 |
| 1.7 | projection builder（fenced + 结构化） | `memory/recall/projection.py`【新增】、`runtime/context.py`【改造】 | 接线 |
| 1.8 | `memory_search` / `memory_get` 工具 | `tools/builtins/memory_query.py`【新增】、`registry.py`【改造】 | 工具 |
| 1.9 | `do_not_write_back` 代码护栏 | `memory/recall/projection_ledger.py`【新增】、`durable_hooks.py`【改造】 | 护栏 |

### 5.1 `MemoryRecallService`（任务 1.1）

【新增】`src/pulsara_agent/memory/recall/service.py`：

```python
class RecallTrigger(StrEnum):
    CHEAP_AUTO = "cheap_auto"
    EXPLICIT_SEARCH = "explicit_search"

class RecallStatus(StrEnum):
    OK = "ok"
    EMPTY = "empty"
    UNAVAILABLE = "unavailable"

@dataclass(frozen=True, slots=True)
class RecallQuery:
    text: str
    scopes: tuple[str, ...] = ()        # 由 ScopeResolver 解析；空=仅当前 scope
    types: tuple[str, ...] = ()         # 空=全部 5 类
    limit: int = 5
    trigger: RecallTrigger = RecallTrigger.CHEAP_AUTO

@dataclass(frozen=True, slots=True)
class RecallItem:
    memory_id: str
    memory_type: str                    # = memory type term name（5 类之一）
    scope: str
    status: memory.NodeStatus           # typed，非自由字符串
    snippet: str
    score: float
    why: tuple[str, ...]                # 信号名: ["lexical_exact_scope","bm25_statement",...]
    deep_recall: str                    # "memory_get <id>"

@dataclass(frozen=True, slots=True)
class RecallResult:
    status: RecallStatus                # typed，非自由字符串
    items: tuple[RecallItem, ...] = ()
    filtered_ids: tuple[str, ...] = ()
    guidance: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()

class MemoryRecallService(Protocol):
    async def recall(self, query: RecallQuery, *, graph_id: str | None = None) -> RecallResult: ...
```

> typed-contract 主张：`RecallTrigger` / `RecallStatus` 用 `StrEnum`；`RecallItem.status` 复用既有 `memory.NodeStatus`（[memory.py:76](src/pulsara_agent/ontology/memory.py:76)）。不用自由字符串，避免 typo 在边界处静默漏过。

默认实现 `LexicalMemoryRecallService` 依赖 `MemoryQuery`（§3.4）：pipeline = `tokenize → lexical_candidates + fts_candidates → union+dedupe → RRF → fetch_nodes → canonical filter → rank → top-N → RecallItem[]`。

### 5.2 lexical exact / BM25 channels（任务 1.2 / 1.3）

- **exact channel**（高精度信号）：对 query 提取 token（Unicode letters/digits/underscore），命中 memory_id / alias / scope / memory_type / 工具名 / code identifier / path / command / 用户原词 / project token。解决 BM25 对短 literal、snake_case、路径、端口、命令不稳定的问题。
- **BM25/FTS channel**：覆盖 statement / summary / aliases / applies_when / do_not_apply_when / evidence summary / project metadata。**token OR，不用脆弱 AND**；bounded top-k；相对 score floor；保留 top-1；zero-result guidance；**不做任意 recent backfill**（守 R4）。
- v1 起步：`fts_candidates` 可先用 Postgres `ILIKE` / 简单 `ts_rank` 或纯 Python lexical ranking；Phase 1.5 换 `tsvector/GIN`。**接口不变，实现替换**。

### 5.3 union + RRF（任务 1.4）

```text
lexical top 20  +  bm25 top 40
  -> dedupe by memory_id
  -> RRF: score(id) = Σ_channel 1/(k + rank_channel(id)),  k=60
  -> canonical filter
  -> final top N (= query.limit)
```

用 RRF 而非分数相加：两通道分数尺度不同、BM25 原始分受 query 长度影响；RRF 稳定且易解释（`why` 可直接列出命中通道）。

### 5.4 canonical filter（任务 1.5，守 §2.3）

`fetch_nodes` 拿回 `CanonicalNodeView[]` 后过滤。**严格区分两类**：

```python
# 承重类——真实做事，必须测：
if view.status == NodeStatus.REJECTED: drop("rejected")          # REJECTED 物化在库，必须挡
if view.status != NodeStatus.ACTIVE:   # NEEDS_REVIEW 默认仅诊断，不作事实注入
    if not (query.trigger == "explicit_search" and allow_needs_review): drop("not_active")
if not scope_accessible(view.scope, query.scopes): drop("scope")
if query.types and view.memory_type not in query.types: drop("type")
if projection_ledger.is_echo(view.id, state): drop("projection_echo")  # 见 5.9

# 惰性类——前向兼容，恒真，测试只断言"不误伤 ACTIVE"：
if view.status in (SUPERSEDED, STALE, CONTRADICTED, ARCHIVED, DELETED): drop(...)  # Phase1 不会命中
```

被 drop 的 id 进 `RecallResult.filtered_ids`（供 trace / explain）。

### 5.5 `project()` 实现（任务 1.6）

【改造】[durable_hooks.py:33](src/pulsara_agent/memory/hooks/durable.py:33)：`DurableMemoryHooks` 增加 `recall: MemoryRecallService | None` 和 `projector: ProjectionBuilder` 依赖，override：

```python
async def project(self, state: LoopState, *, token_budget: int) -> dict[str, Any] | None:
    if self.recall is None:
        return None
    if _should_skip_recall(state):          # casual/too-short/user-said-ignore -> None（守 policy）
        return None
    query = _build_recall_query(state)      # 取 latest user msg + current_scope + active tools
    result = await self.recall.recall(query, graph_id=self._graph_id)
    if result.status != "ok" or not result.items:
        return None
    self.projection_ledger.record(state, result.items)   # 5.9：登记本轮 surfaced ids+fingerprint
    return self.projector.build(result, token_budget=token_budget)
```

`ReflectiveMemoryHooks` 继承即可（它已继承 `DurableMemoryHooks`）。wiring（[wiring.py:213](src/pulsara_agent/runtime/wiring.py:213)）注入 `recall=LexicalMemoryRecallService(MemoryQuery(dsn))`。

### 5.6 projection builder（任务 1.7）

【新增】`src/pulsara_agent/memory/recall/projection.py`，产出 §1.1 fence 兼容的 dict（保留 `summary`/`items` 键以兼容 [context.py:177](src/pulsara_agent/runtime/context.py:177)，但 `items` 升级为结构化）：

```python
{
  "summary": "<人类可读渲染，进 prompt>",
  "items": ["[mem:preference/1] ... (Preference, scope=..., why=bm25_statement)"],
  "included_memory_ids": ["preference:1"],
  "filtered_memory_ids": [...],
  "do_not_write_back": True,
}
```

输出格式（渲染进 fence 后）：

```text
<recalled-memory-projection do_not_write_back="true">
- [preference:1] 用户偏好：测试和本地命令优先使用根目录 .venv 的 uv 环境。
  type: Preference   scope: ctx:workspace/pulsara_agent   status: ACTIVE
  why: bm25_statement, scope_match
  deep_recall: memory_get preference:1
</recalled-memory-projection>
```

【改造】[context.py:165](src/pulsara_agent/runtime/context.py:165)：保持现有 fence 字符串；若 projection 带 `do_not_write_back` 标记，渲染时透传到 `<recalled-memory-projection>` 包裹。projection **不写回 EventLog 用户原文**（守 R3）。

### 5.7 `memory_search` / `memory_get`（任务 1.8）

【新增】`src/pulsara_agent/tools/builtins/memory_query.py`，实现 `Tool` Protocol（[base.py:26](src/pulsara_agent/tools/base.py:26)），`is_read_only=True, is_concurrency_safe=True`。

`memory_search` parameters / 返回：

```json
// in:  {"query": "...", "scope": "optional", "kind": "optional", "limit": 5}
// out (ok):
{"status":"ok","results":[
  {"memory_id":"preference:1","type":"Preference","scope":"ctx:...","snippet":"...",
   "score":0.73,"why":["bm25_statement","scope_match"],"deep_recall":"memory_get preference:1"}]}
// out (empty) — 必须给 guidance，不得幻觉：
{"status":"empty","guidance":[
  "try fewer distinctive terms",
  "use history_search for verbatim past conversation",
  "verify current files/tools if asking about current state"]}
// out (unavailable) — 结构化失败：
{"status":"unavailable","reason":"recall_backend_unavailable","fallback":"history_search_or_current_files","can_retry":false}
```

`memory_get`：按 id 返回完整 `CanonicalNodeView`（statement/type/scope/status/evidence refs/source_authority/verification_status/confidence/created-updated/直接关系/warnings）。

【改造】[registry.py:26](src/pulsara_agent/tools/builtins/registry.py:26)：注册两个工具（依赖 `MemoryRecallService` + `MemoryQuery`，恒注册，不挂在 `memory_proposal_sink` 条件上——它们是只读的）。工具描述强约束："用户问'之前/记得/偏好/决定/todo/我们上次'时先查 memory 或 history；不确定时报告'已查但无确认记录'，不要补全；涉及当前文件/配置/外部状态时召回只是历史线索，必须用当前工具验证。"

`history_search`（任务延后，但接口边界现在定）：查 EventLog/transcript/tool_execution/artifacts，回答"原话/上次任务/旧工具输出/verbatim evidence"。**不得**与 `memory_search` 合并。

### 5.8 `do_not_write_back` 代码护栏（任务 1.9，守 R3）

【新增】`src/pulsara_agent/memory/recall/projection_ledger.py`。prompt 提示不够，必须有代码校验。

```python
class ProjectionLedger:
    # 按 turn 记录本轮 surfaced 的 memory ids 与 statement fingerprint
    def record(self, state: LoopState, items: Sequence[RecallItem]) -> None: ...
    def is_echo(self, candidate_statement: str, state: LoopState) -> bool:
        # 候选 statement 与本轮 projection 任一 memory 高相似 -> True
    def surfaced_ids(self, state: LoopState) -> set[str]: ...
```

【改造】[durable_hooks.py](src/pulsara_agent/memory/hooks/durable.py) 的 `_drain_to_pool` / `_finalize_invalid_to_pool`：候选入池前检查 `projection_ledger.is_echo(candidate.statement, state)`，命中则拒绝或标 `skip_reason="projection_echo"`，不入池。**v1 必做**——否则系统会把召回内容当新证据自我增殖。

### 5.9 Phase 1 退出标准与测试

退出标准：用户偏好能在后续 turn 自动召回；显式问 memory 时工具可查；unrelated/rejected 不进 projection；projection echo 不入池；zero result 不幻觉。

**Phase 1 末尾：生成并冻结 `v1_floor.json`**（§9）——此时 v1 RRF 融合输出已稳定存在，跑一次 runner 把当前指标快照入库，由人工复核签字冻结；之后 CI 的 `quality >= floor` 约束从 informational 切为 blocking（§4.4）。这是"floor 锚定 v1 融合输出"的落地时点。

测试（`tests/test_recall_v1.py`【新增】）：

- `active preference 可召回`；`unrelated memory 不召回`；`rejected 不召回`（造一个 REJECTED 节点，断言被 §5.4 承重 filter 挡）。
- `candidate pool 不参与召回`（候选池有内容但 recall 只读 canonical）。
- `projection echo 不写回`（projection 出现 mem X，下一轮模型复述 X，断言 X 不重新入池）。
- `user says ignore memory 时 suppress`（`_should_skip_recall` 命中）。
- `zero result 有 guidance`；`memory_search unavailable 结构化`。
- `newly governed ACTIVE memory 最终可召回`（写一个 ACTIVE，断言能召回——守 §2.3 F2 的欠填充方向）。
- **惰性 filter 测试只断言"不误伤 ACTIVE"**，不断言"能过滤 superseded"（造不出，留给 Phase 2）。

## 6. Phase 1.5：durable SearchIndex + trace + suppression

目标：把 §5.2 的朴素 lexical/FTS 换成 durable 可重建索引；加召回追踪、近期去重、结构化不可用。**接口不变，实现升级。**

### 6.0 任务总览

| # | 任务 | 目标文件 | 类型 |
|---|---|---|---|
| 1.5.1 | `memory_search_index` 表 + `tsvector/GIN` | `storage/memory_schema.py`【改造】 | 索引 |
| 1.5.2 | index 写后增量同步（outbox 驱动） | `memory/canonical/index_sync.py`【新增】 | 同步 |
| 1.5.3 | rebuild job（全量从 canonical 重建） | `memory/canonical/index_sync.py` | 重建 |
| 1.5.4 | `recall_traces` / `recall_usages` 表 + 写入 | `storage/memory_schema.py`、`memory/recall/service.py`【改造】 | 追踪 |
| 1.5.5 | recent-recall suppression | `memory/recall/service.py`【改造】 | 去重 |
| 1.5.6 | structured unavailable + cooldown | `memory/recall/service.py`【改造】 | 降级 |

### 6.1 SearchIndex（任务 1.5.1）

```sql
CREATE TABLE IF NOT EXISTS memory_search_index (
    graph_id      TEXT NOT NULL,
    memory_id     TEXT NOT NULL,
    memory_type   TEXT NOT NULL,
    scope         TEXT NOT NULL,
    status        TEXT NOT NULL,
    fts           TSVECTOR NOT NULL,           -- statement+summary+aliases+applies_when 拼成
    aliases       TEXT[],
    updated_at    TIMESTAMPTZ NOT NULL,
    PRIMARY KEY (graph_id, memory_id),
    FOREIGN KEY (graph_id, memory_id)
        REFERENCES memory_nodes(graph_id, id) ON DELETE CASCADE   -- 复合 FK，杜绝悬空索引 id
);
CREATE INDEX IF NOT EXISTS idx_msi_fts ON memory_search_index USING GIN(fts);
CREATE INDEX IF NOT EXISTS idx_msi_type_scope ON memory_search_index(graph_id, memory_type, scope);
```

> 复合 FK `(graph_id, memory_id) → memory_nodes(graph_id, id)` 是选 Postgres 而非 Oxigraph 的红利之一：索引行的悬空 id 由 FK + `ON DELETE CASCADE` 杜绝。`fts_candidates` 改为 `WHERE graph_id=%s AND fts @@ plainto_tsquery(%s)` + `ts_rank` 排序。注意 FK 仅适用于投影到 `memory_nodes` 的 5 类 memory 节点；运行时节点不进 `memory_search_index`，故无矛盾。

### 6.2 增量同步与重建（任务 1.5.2 / 1.5.3，守 §2.3 SearchIndex 契约）

SearchIndex 是**派生投影**，不是真相源。两条维护路径，缺一不可：

- **写后增量**：§4.2 的 `memory_write_outbox` 在 governance 提交时附带一条 index-dirty 描述；`index_sync.py` 的 worker 消费 outbox，写/更新对应 `memory_search_index` 行。**freshness SLO**：index 落后 canonical 不超过一个 governance 批次 / N 秒（写进 config，eval 可断言）。
- **周期全量 rebuild**：`rebuild()` 从 `memory_nodes` 全量重建 index（漂移修复 / 灾备）。删 index 后能重建（退出标准）。

**悬空 id 契约**（R1 / R2）：召回拿到 index hit 后**必回** `MemoryQuery.fetch_nodes` 裁定 status/scope；解析到缺失或非 ACTIVE 节点 → 静默丢弃 + 记 trace，**绝不注入**。这覆盖两个危险方向：

1. **过期方向**：index 指向已 superseded/deleted 节点 → fetch_nodes 看到真实 status → 过滤。
2. **欠填充方向**：刚 governed 为 ACTIVE 但 index 未及更新 → 必须有 reconcile/repair 让它最终可召回（测试断言）。

### 6.3 recall trace（任务 1.5.4）

```sql
CREATE TABLE IF NOT EXISTS recall_traces (
    trace_id        TEXT PRIMARY KEY,
    graph_id        TEXT NOT NULL,                         -- 与 canonical 表同分区，suppression/eval 不跨 graph 污染
    session_id      TEXT NOT NULL,                         -- 完整定位；suppression 默认按 session 维度
    run_id          TEXT NOT NULL,
    turn_id         TEXT NOT NULL,
    reply_id        TEXT NOT NULL,
    query           TEXT NOT NULL,
    trigger_kind    TEXT NOT NULL,                         -- RecallTrigger value
    candidate_ids   JSONB NOT NULL,
    included_ids    JSONB NOT NULL,
    filtered_ids    JSONB NOT NULL,
    warnings        JSONB NOT NULL DEFAULT '[]'::jsonb,
    latency_ms      INTEGER NOT NULL,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_recall_traces_scope
    ON recall_traces(graph_id, session_id, created_at);

CREATE TABLE IF NOT EXISTS recall_usages (
    trace_id        TEXT NOT NULL,
    graph_id        TEXT NOT NULL,                         -- 冗余自 trace，便于不 JOIN 直接按 graph 聚合
    memory_id       TEXT NOT NULL,
    injected        BOOLEAN NOT NULL,
    selected_by_tool BOOLEAN NOT NULL DEFAULT false,
    cited_by_response BOOLEAN,
    later_confirmed BOOLEAN,
    later_contradicted BOOLEAN,
    PRIMARY KEY (trace_id, memory_id),
    FOREIGN KEY (trace_id) REFERENCES recall_traces(trace_id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_recall_usages_mem ON recall_usages(graph_id, memory_id);
```

- `graph_id` 与 canonical 表（§3.2）同分区：suppression 读"最近 N 轮 injected 的 memory_id"时必须**限定 `graph_id`**，否则跨 graph（如测试 graph 与生产 graph）互相污染；`(graph_id, memory_id)` 才是完整定位。
- `session_id` 让 suppression 默认落在"本 session"维度（与 Claude Code session dedupe 一致），也便于 trace 回溯。
- `recall_usages.trace_id` FK 到 `recall_traces`（`ON DELETE CASCADE`）；`graph_id` 冗余存一份，避免聚合时强制 JOIN。

trace **不改变 canonical truth**（退出标准）；它只喂给 suppression（6.4）、eval、未来 maintenance（Phase 4）。

### 6.4 suppression + structured unavailable（任务 1.5.5 / 1.5.6）

- **recent-recall suppression**：`recall()` 读最近 N 轮 `recall_usages.injected` 的 memory_id，对本轮同 id 降权或跳过，避免每轮反复塞同一条（借鉴 Claude Code session dedupe）。
- **structured unavailable + cooldown**：backend 超时/不可用时返回 `RecallResult(status=RecallStatus.UNAVAILABLE, ...)`（不是空大段错误），并对该 backend 设 cooldown，避免每轮撞同一个失败（借鉴 OpenClaw）。自动召回失败 fail-soft：不阻塞主请求、记 trace、不向模型注入错误大段。

### 6.5 Phase 1.5 退出标准与测试

- 删除 index 可重建（`rebuild()` 后召回结果与重建前一致）。
- stale index hit 回 canonical 被过滤（造 index 指向 superseded 节点——需 Phase 2 能造 superseded，故此项部分依赖 Phase 2；Phase 1.5 先测"index 指向 REJECTED/已删节点被过滤"）。
- active node 欠索引有 reconcile/repair（写 ACTIVE 节点、故意不同步 index、跑 reconcile、断言可召回）。
- trace 不改变 canonical（写 trace 前后 `memory_nodes` 不变）。

## 7. Phase 2：lifecycle producer + graph-aware reranker/explainer

这是 Pulsara 的产品差异化层。**它有一个被低估的前置：lifecycle producer。** 没有它，§5.4 的惰性 filter 永远恒真、§8 的招牌解释会编造。所以 Phase 2 的第一项不是 reranker，是让 lifecycle 真的发生。

### 7.0 任务总览（顺序即依赖）

| # | 任务 | 目标文件 | 类型 |
|---|---|---|---|
| 2.1 | **update-existing-node 原语**（两后端） | `graph/postgres.py`【改造】、`graph/in_memory.py`【改造】 | 前置·substrate |
| 2.2 | **lifecycle producer**：状态转换 + 双向边 + 事件 | `memory/canonical/lifecycle.py`【新增】、`governance.py`【改造】 | 前置·核心 |
| 2.3 | structured ActionBoundary trigger 字段 | `entities/memory/action_boundary.py`【改造】、`write_gate.py`【改造】 | 本体 |
| 2.4 | direct-relation reranker（进程内） | `memory/recall/rerank.py`【新增】 | 差异化 |
| 2.5 | grounded explainer（结构化 claim） | `memory/recall/explain.py`【新增】 | 差异化 |
| 2.6 | `memory_related` / `memory_explain` 工具 | `tools/builtins/memory_query.py`【改造】 | 工具 |
| 2.7 | confab=0 / floor 不回退 接 CI | `evals/recall/runner.py`【改造】 | 门槛 |

### 7.1 任务 2.1：update-existing-node 原语（前置 substrate）

【现状】ledger 全是创建，无更新；`InMemoryGraphStore.update` 抛 `NotImplementedError`（[in_memory.py:72](src/pulsara_agent/graph/in_memory.py:72)）。lifecycle 第一需求是**改已存在节点的 status**——这是系统第一个真正的"更新"语义，不能再靠 create。

**不污染 `GraphStore`**（修正自相矛盾）：`GraphStore` Protocol 保持不变（它是 RDF-ish 通用存储接口，§10.5 已声明"不变"）。lifecycle mutation 放进一个**独立的窄协议** `MutableCanonicalMemoryStore`，由 `PostgresGraphStore` 与 `InMemoryGraphStore` **额外** 实现（structural typing，二者本就有这俩类，只是多一个方法）。这样既不在 `GraphStore` 上加方法、也不自相矛盾。

```python
# 【新增】src/pulsara_agent/graph/mutable.py（或随 store.py）
class MutableCanonicalMemoryStore(Protocol):
    """canonical memory 的生命周期变更面，独立于 GraphStore 通用读写接口。"""
    def set_status(self, node_id: str, status: NodeStatus, *, updated_at: datetime,
                   graph_id: str | None = None) -> None:
        """原子地把已存在节点的 status 改写，并更新 updated_at。节点不存在则 KeyError。"""
```

- `updated_at` 用 `datetime`（非 `str`），与 DDL 的 `TIMESTAMPTZ` 列一致（§3.2 memory_nodes），让驱动直接绑定 timestamptz，避免字符串再解析。
- `PostgresGraphStore`（实现 `GraphStore` **且** `MutableCanonicalMemoryStore`）的 `set_status`：在 `MemoryWriteUnitOfWork` 内执行 `UPDATE memory_nodes SET status=%s, updated_at=%s WHERE graph_id=%s AND id=%s`（单行原子），并同步回写 `graph_documents` 该文档的 status 字段，保持通用层与投影一致。
- `InMemoryGraphStore.set_status`：`get_jsonld` → 改 dict → `put_jsonld`（不再让 `update` 的 NotImplementedError 挡路；`GraphStore.update` 仍可保持 NotImplementedError，因为 lifecycle 走的是 `MutableCanonicalMemoryStore`，不是 raw SPARQL `update`）。
- 调用方（`MemoryLifecycle`、reconciliation）依赖 `MutableCanonicalMemoryStore` 类型，而非 `GraphStore`，使能力边界在类型层显式。

### 7.2 任务 2.2：lifecycle producer（前置核心，守 §2.3 / 消解招牌空集）

【新增】`src/pulsara_agent/memory/canonical/lifecycle.py`。这是把"边 + 状态"同时落地、且**多节点原子**的唯一入口。

```python
class MemoryLifecycle:
    def supersede(self, *, old_id: str, new_id: str, batch_id: str,
                  graph_id: str | None = None) -> list[AgentEvent]:
        """A 被 B 替代。MUST 在单一可恢复提交内（同一 MemoryWriteUnitOfWork）完成：
           1) 改 new_id 的 graph_documents JSON-LD：在其 relation 字段加 supersedes -> old_id，
              再经 uow.graph.put_jsonld(new_doc) 落库，由 _sync_relations_from_document 投影 memory_relations
              （禁止直接 INSERT memory_relations，避免与 graph_documents 分叉，见 §3.3）
           2) uow.graph.set_status(old_id, SUPERSEDED, updated_at=now)（同时回写 graph_documents 与 memory_nodes）
           3) outbox 记录（重放安全）
           4) 事务提交后 emit MemorySupersededEvent(memory_id=old_id, superseded_by=new_id)"""
    def mark_contradicted(self, *, a_id: str, b_id: str, batch_id: str,
                          graph_id: str | None = None) -> list[AgentEvent]: ...
    def mark_stale(self, *, node_id: str, batch_id: str,
                   graph_id: str | None = None) -> list[AgentEvent]: ...
```

**这是系统第一个 multi-node atomic mutation**，比单节点 create 严格更难。要求：

- **原子性**：`supersede` 的步骤 1+2+3 在**同一 `MemoryWriteUnitOfWork` 事务**（同库，可做到）；事件 4 在事务提交后 emit。否则会出现"B active + A 仍 active + 悬空 supersedes 边"——正是要消解的产品 bug。
- **关系只经投影入口**：边通过"改源文档 + `_sync_relations_from_document`"落地（§3.3），**不**直接写 `memory_relations`。这保证 `graph_documents` 与投影表永不分叉。
- **正反向边**：不物化 `supersededBy`；`memory_relations` 的 `idx_..._target` 让逆向查询 = `WHERE graph_id=? AND target_id=old_id AND predicate='supersedes'`。
- **接入 governance（W1b 入口）**：【改造】[governance.py:46](src/pulsara_agent/memory/governance/executor.py:46)，当治理判定一个新节点替代旧节点时，在 UoW 内调 `lifecycle.supersede(...)`，而非只写新节点。
- **InMemory 后端也要支持**（`set_status` + 改文档 relation 字段再 `put_jsonld`），否则测试与轻量运行下 lifecycle 不可测。

【消解】完成 2.2 后：§5.4 的 `NOT superseded` 从惰性升为承重；§8 的"被替代"解释有真实边可依。**在 2.2 落地前，§8 不得展示替代/冲突解释。**

### 7.3 任务 2.3：ActionBoundary structured trigger（守 §2.4，消解自由文本）

【现状】`applies_when` / `do_not_apply_when` 是自由文本（[action_boundary.py:21](src/pulsara_agent/entities/memory/action_boundary.py:21)），cheap recall 无法机器匹配。【改造】新增结构化触发字段（保留自由文本作人类可读摘要）：

```python
# ActionBoundary 新增（均为可选，向后兼容；自由文本仍在）
trigger_tools:      tuple[str, ...] = ()
trigger_actions:    tuple[str, ...] = ()
trigger_file_globs: tuple[str, ...] = ()
trigger_scopes:     tuple[str, ...] = ()
trigger_keywords:   tuple[str, ...] = ()
negative_tools:     tuple[str, ...] = ()
negative_actions:   tuple[str, ...] = ()
negative_file_globs:tuple[str, ...] = ()
```

写入时索引（进 `memory_search_index` 或单独 trigger 表），让 RecallTriggerDetector 用**索引查找**匹配："当前任务涉及 pytest → 召回测试相关 ActionBoundary；当前工具是 web search → 召回 freshness/verification boundary"。**不做** LLM 匹配，**不**把自由文本解释成精确规则。本体扩展遵循 [feedback_schema_evolution]：hard-cut、边界处判别联合、不留 compat shim（既有 ActionBoundary 节点的新字段缺省为空 tuple，召回退回 BM25 命中）。

### 7.4 任务 2.4：direct-relation reranker（进程内，不上 Oxigraph）

【新增】`src/pulsara_agent/memory/recall/rerank.py`。在 §3.4 `CanonicalNodeView`（已含直接 in/out 边）上做，**进程内、零额外 HTTP**（借鉴 scope-recall-hermes 的 in-memory hop 权重）：

```python
def rerank(items: list[RecallItem], views: dict[str, CanonicalNodeView], ctx: RecallContext) -> list[RecallItem]:
    # 加分信号（全部基于已物化数据）：
    #   direct evidence count（supports 入边数）
    #   based_on direct refs（Decision lineage）
    #   same scope / same artifact / same tool 命中
    #   applies_when indexed tags 命中当前任务
    # 降分/警告信号：
    #   有 supersedes 出边指向更新节点 -> 本节点应让位（若自身已 SUPERSEDED 已被 filter 挡）
    #   有 contradicts 边 -> 加 warning，不直接删
    # recency 仅在相关性已建立后加分（守 R4）
```

**上线条件（§8 / CI）**：质量不跌破 frozen lexical+BM25 floor；latency 在预算内（graph 层 50–100ms，含在 500ms 总预算内）；失败降级回 lexical+BM25；不引入严重无关召回；不读 candidate pool；不依赖 N+1 HTTP。

### 7.5 任务 2.5：grounded explainer（守 §2.4 E1–E3，结构化）

【新增】`src/pulsara_agent/memory/recall/explain.py`。**解释必须机器可校验**，否则 confab=0 进不了 CI。

```python
class ClaimKind(StrEnum):
    EVIDENCE_SUPPORT = "evidence_support"   # grounded on SUPPORTS 入边
    SUPERSEDED_BY    = "superseded_by"       # grounded on supersedes 边
    CONTRADICTED_BY  = "contradicted_by"     # grounded on contradicts 边
    SCOPE_MATCH      = "scope_match"         # grounded on scope 字段
    TYPE_MATCH       = "type_match"          # grounded on type 字段
    BM25_HIT         = "bm25_hit"            # grounded on 召回信号
    LEXICAL_HIT      = "lexical_hit"

@dataclass(frozen=True, slots=True)
class ExplanationClaim:
    text: str                       # 渲染层人话
    kind: ClaimKind                 # typed，非自由字符串
    grounded_on: tuple[str, ...]    # edge id（"rel:new_id|supersedes|old_id"）或 signal id（"bm25_statement"）

@dataclass(frozen=True, slots=True)
class Explanation:
    memory_id: str
    claims: tuple[ExplanationClaim, ...]
```

**生成规则（硬约束）**：

```text
有真实 SUPPORTS 入边      -> 可发 evidence_support，grounded_on = 该边
有真实 supersedes 出/入边 -> 可发 superseded_by，  grounded_on = 该边
有真实 contradicts 边     -> 可发 contradicted_by， grounded_on = 该边
仅 lexical/BM25/scope/type/status 命中 -> 只能发对应 signal claim
其余一律不发（"无解释"是合法输出）
```

**校验器**（CI 与运行时都用）：遍历每条 `ExplanationClaim`，断言 `grounded_on` 的每个 edge id 能在 `memory_relations` 解析到真实行、每个 signal id 在本次召回信号集合内。任何不可解析 = confabulation。`confab_rate = 不可解析 claim 数 / 总 claim 数`，硬线 = 0。prose 是 `claims` 的渲染，不是事实源（E3）。

示例（grounded）：

```text
召回原因：这条 Decision 与当前 query 共享 scope，并由已物化 SUPPORTS 边指向的 evidence 支撑。
未召回原因：这条旧 Preference 有已物化 SUPERSEDES 边指向 newer preference。
```

边不存在时的合法降级：

```text
召回原因：BM25 命中 statement，且 scope 与当前 workspace 匹配。
```

### 7.6 任务 2.6 / 2.7：工具与 CI

- `memory_related`：沿 `memory_relations` 取上下游（evidence、based-on、supersedes、contradicts、same entity/artifact），只读。
- `memory_explain`：对给定 memory_id 返回结构化 `Explanation`（经校验器）。
- CI【改造】`runner.py`：启用 `confab_rate=0`、`superseded_leak=0`（此时能造 superseded，真正生效）、floor 不回退。

### 7.7 Phase 2 退出/上线标准

- lifecycle：用户改偏好 A→B 后，A 被置 SUPERSEDED、写 `supersedes` 边、召回不再把 A、B 都当 active（消解产品 bug）。
- reranker：质量 ≥ frozen floor；latency 有数字上限；fail-soft 回退；解释明显更好。
- explainer：`confab_rate = 0`（CI 强制）；无边时沉默。
- ActionBoundary：相关任务才出现对应 boundary（结构化 trigger 命中）。

## 8. Phase 3：hybrid / vector / planner / Oxigraph（仅当各自赢得成本）

本阶段全部是**可选增强**，每项有独立进入条件，不达标不进默认路径。细节从略（实现时再展开为子说明书），此处只钉死边界。

### 8.0 任务与进入条件

| 增强 | 目标文件 | 进入默认路径的条件 |
|---|---|---|
| pgvector / 外部 vector sidecar | `memory/vector.py`【新增】 | vector-only 中置信候选**不**注入；高阈值；与 lexical+BM25 hybrid 后质量 ≥ frozen floor |
| hybrid RRF（lexical+BM25+vector） | `memory/recall/service.py`【改造】 | 三通道 RRF 在 eval 上不跌破 floor、不增无关召回 |
| optional MMR 多样性 | `memory/recall/rerank.py`【改造】 | 默认关闭；开启需证明对复杂 query 有可感知收益 |
| LLM query planner（仿 OpenViking `search()`） | `memory/planner.py`【新增】 | 默认关闭；有 hard timeout；失败降级回非 planner 路径 |
| Oxigraph RDF/SPARQL 后端 | `graph/oxigraph.py`【改造】 | 见 §8.1 全部满足 |

### 8.1 Oxigraph 进入默认路径的硬条件（全部满足才可）

【现状】Oxigraph 是同步阻塞 urllib HTTP 客户端、`query(bindings)` 抛 `NotImplementedError`、N+1、codec 不 typed 时间戳、非嵌入式。在补齐以下之前，它**只能是实验后端，不进默认召回**：

- async / 可取消客户端（`httpx.AsyncClient` 或 `aiohttp` keep-alive，替换 `urllib`）；
- bounded batch query（单查询批量取候选+边，实现 `query(bindings)` 或安全 VALUES 模板）；
- **无** per-candidate N+1 hot path；
- write atomicity 或可靠 outbox/reconciliation（Oxigraph 无法与 Postgres 同事务，必须补偿）；
- typed `xsd:dateTime`（codec 升级，否则 recency 仍不可比较）；
- 集成测试**不依赖手动起本地服务**（否则 CI 静默跳过 = 回归漏网）；
- 能支持 Postgres 难表达的多跳语义 / RDF 互操作——**否则它是 backend choice，不是产品价值**，无理由进默认。

### 8.2 Phase 4（远期）：从召回使用反哺 maintenance

`recall_usages`（§6.3）积累后："repeated recall but never used → 低效用信号；frequently contradicted → maintenance candidate；stale warning 频发 → verification task；用户反馈 → trust/importance 调整"。**边界**：recall usage 只产 maintenance candidate；canonical 变更仍走 governance/lifecycle 写路径（W1/W3），不允许 recall 旁路改 canonical。

## 9. Frozen Floor：可证伪门槛的版本化工件

"frozen lexical+BM25 floor" 是 reranker/vector/planner 不许跌破的绝对底线。它必须是**仓库内的版本化工件**，不是一句话。否则棘轮效应会让"小输一点"逐层蚀掉底盘。

### 9.1 工件构成（全部入库）

```text
evals/recall/baseline/v1_floor.json     # baseline snapshot：每个 case 的 baseline 指标
evals/recall/config.yaml                 # 指标定义 + golden set 版本哈希 + runner 配置
evals/recall/fixtures/v1_golden.jsonl    # golden set（带版本哈希）
```

`v1_floor.json` 内容示例：

```json
{
  "golden_set_sha": "sha256:...",
  "runner_version": "1.0.0",
  "baseline_kind": "v1_fused_lexical_bm25",
  "frozen_at": "2026-06-15",
  "metrics": {
    "included_hit_rate": 0.94,
    "excluded_leak_rate": 0.0,
    "p95_latency_ms": 180,
    "projection_budget_violations": 0
  }
}
```

### 9.2 floor 的三条规则

- **R-Floor-1**：baseline 是 **v1 RRF 融合后的输出快照**，不是裸 BM25。理由：v1 不单独跑裸 BM25，拿裸 BM25 当底线对的是一个永不存在的基准。
- **R-Floor-2**：reranker/vector/planner 的 eval 与 `v1_floor.json` 比，`quality < floor` 即 CI fail。无论叠多少层增强，质量永不许跌破这条**固定**线（不是"相对上一版不更差"——那会逐步下滑）。
- **R-Floor-3**：floor 只能由**显式人工 "floor bump" PR** 重新冻结（带 reviewer 签字与原因），绝不自动刷新。golden set 改动必须同步 `golden_set_sha` 并触发 floor 复核。

### 9.3 指标定义（runner 实现）

```text
included_hit_rate     = Σ(expected_included 命中) / Σ(expected_included)
excluded_leak_rate    = Σ(expected_excluded 误召) / Σ(expected_excluded)      # 硬线 0
candidate_leak        = 候选池内容出现在 projection 的 case 数                 # 硬线 0
rejected_leak         = REJECTED 节点出现在 projection 的 case 数              # 硬线 0
superseded_leak       = SUPERSEDED 节点被当 active 注入的 case 数（Phase2 起生效）# 硬线 0
confabulation_rate    = 不可解析 explanation claim / 总 claim（Phase2 起生效）  # 硬线 0
p95_latency_ms        <= case.latency_budget_ms
```

## 10. 数据契约汇总（跨 Phase 引用）

集中列出本文引入的所有结构，便于实现时核对。规范性以此为准。

### 10.1 召回链路类型（`memory/recall/service.py`、`memory/canonical/query.py`）

```text
RecallTrigger      StrEnum: cheap_auto | explicit_search
RecallStatus       StrEnum: ok | empty | unavailable
RecallQuery        text, scopes, types, limit, trigger:RecallTrigger
RecallItem         memory_id, memory_type, scope, status:NodeStatus, snippet, score, why[], deep_recall
RecallResult       status:RecallStatus, items[], filtered_ids[], guidance[], warnings[]
CanonicalNodeView  id, memory_type, scope, status:NodeStatus, statement, summary, source_authority,
                   verification_status, confidence_level, applies_when, do_not_apply_when,
                   created_at:datetime, updated_at:datetime,
                   evidence_ids[], outgoing[(pred,target)], incoming[(pred,source)]
MemoryQuery        fetch_nodes(ids, *, graph_id) / lexical_candidates(..., graph_id) / fts_candidates(..., graph_id)
```

### 10.2 解释契约（`memory/recall/explain.py`，守 §2.4）

```text
ClaimKind          StrEnum: evidence_support|superseded_by|contradicted_by|scope_match|type_match|bm25_hit|lexical_hit
ExplanationClaim   text, kind:ClaimKind, grounded_on[edge_id|signal_id]
Explanation        memory_id, claims[]
校验器             每个 grounded_on 必须解析到 memory_relations 真实行 或 本次召回信号集；
                   不可解析 = confabulation；confab_rate 硬线 = 0
```

### 10.3 projection dict（`memory/recall/projection.py` → `runtime/context.py`）

```text
{ summary: str,                       # 进 prompt 的人类可读渲染
  items: [str],                       # 结构化行的渲染
  included_memory_ids: [str],
  filtered_memory_ids: [str],
  do_not_write_back: true }           # context 渲染时透传到 <recalled-memory-projection>
```

兼容性：保留 `summary`/`items` 键以满足现有 [context.py:177](src/pulsara_agent/runtime/context.py:177) 的读取；新增键不破坏旧渲染。

### 10.4 持久化 schema（`storage/memory_schema.py`，所有表带 `graph_id`）

```text
graph_documents         通用 JSON-LD 文档存储（GraphStore 真相落点，承接 ToolResult/Artifact/Turn/memory 全部节点）
memory_nodes            canonical memory 投影（typed 列 + timestamptz；从 graph_documents 派生）
memory_relations        typed 边投影（双向可查，逆向边免物化；从【所有】文档 relation 谓词同步，含 evidence/runtime 源）
memory_write_outbox     原子写 + 幂等键(governance_batch_id,decision_id)；target_entry_key 追溯目标(merge=排序 entry_ids hash)
memory_search_index     tsvector/GIN（Phase 1.5；复合 FK 到 memory_nodes 的 (graph_id,id)）
recall_traces           召回追踪（带 graph_id + session_id；不改 canonical）
recall_usages           召回使用反馈（带 graph_id；FK→recall_traces；喂 suppression / Phase 4）
```

### 10.5 写入边界、UnitOfWork 与存储协议（`memory/*.py`、`graph/*.py`）

```text
两条写入入口（§2.1）：
  W1  创建：MemoryWriteService → MemoryWriteGate → ExecutionEvidenceLedger（新节点）
  W1b lifecycle：governance/maintenance → MemoryLifecycle（已有节点状态转换 + 边，Phase 2）

原子性：MemoryWriteUnitOfWork（memory/canonical/unit_of_work.py）绑定单 connection，暴露：
  uow.graph                 PostgresGraphStore（绑定 self.conn）
  uow.memory_write_service  MemoryWriteService，其 ledger.graph == uow.graph（关键：apply_decision 必须用它，
                            而非 wiring 期预造的 self.memory_write_service，否则仍走旧连接）
  uow.decisions             CandidateDecisionRepository（绑定 self.conn）
  uow.outbox                OutboxRepository（绑定 self.conn）
  uow.lifecycle             MemoryLifecycle（绑定 uow.graph / uow.outbox，Phase 2）

存储协议（两个，互不污染）：
  GraphStore（不变）：put_jsonld/get_jsonld/has_jsonld/find_by_type/query/update/delete_graph
    PostgresGraphStore.put_jsonld 是通用 JSON-LD 存储；对【任何】文档经 _sync_relations_from_document
    投影 memory_nodes（仅 5 类）/ memory_relations（所有源，含 evidence/runtime）
  MutableCanonicalMemoryStore（新增窄协议，graph/mutable.py）：
    set_status(node_id, status:NodeStatus, *, updated_at:datetime, graph_id)   # Phase 2
    由 PostgresGraphStore 与 InMemoryGraphStore 额外实现；关系/状态变更只经此面或投影 helper，
    不直接 INSERT memory_relations、不污染 GraphStore。

v1 recall 不依赖 raw SPARQL：PostgresGraphStore.query/update 抛 NotImplementedError，召回走 MemoryQuery。
  （注：OxigraphGraphStore 支持无 bindings 的 query/update，[oxigraph.py:101](src/pulsara_agent/graph/oxigraph.py:101)；
   本文不声称三后端 query 行为一致，只声称 v1 recall 不依赖 raw SPARQL。）
```

## 11. 测试矩阵（按 Phase 累积）

| 测试 | 文件 | Phase | 断言要点 |
|---|---|---|---|
| projection 超时 fail-soft | `tests/test_recall_timeout.py` | 0 | 慢 recall 不阻塞，emit ProjectionFailed |
| 原子写 + 重放幂等 | `tests/test_governance_atomicity.py` | 0 | 崩溃点之间无 split-brain；outbox 重放不重复 |
| eval runner + CI gate | `tests/test_eval_runner.py` | 0 | floor 比对、leak 计数生效 |
| active preference 可召回 | `tests/test_recall_v1.py` | 1 | 写 ACTIVE → 后续 turn projection 含之 |
| unrelated 不召回 | 同上 | 1 | 无关 query 不注入 |
| rejected 不召回（承重 filter） | 同上 | 1 | 造 REJECTED 节点（已物化），断言被挡 |
| candidate pool 不参与 | 同上 | 1 | 池有内容但 recall 只读 canonical |
| projection echo 不写回 | `tests/test_projection_guard.py` | 1 | 复述召回内容不重新入池 |
| ignore memory 抑制 | `tests/test_recall_v1.py` | 1 | `_should_skip_recall` 命中 |
| zero result guidance / unavailable | 同上 | 1 | 结构化输出，不幻觉 |
| 惰性 filter 不误伤 ACTIVE | 同上 | 1 | 只断言语法存在 + ACTIVE 通过（不断言过滤 superseded） |
| index 删后可重建 | `tests/test_search_index.py` | 1.5 | rebuild 前后召回一致 |
| 欠索引 reconcile | 同上 | 1.5 | ACTIVE 未同步 index → reconcile → 可召回 |
| trace 不改 canonical | `tests/test_recall_trace.py` | 1.5 | 写 trace 前后 memory_nodes 不变 |
| set_status 原语（两后端） | `tests/test_graph_set_status.py` | 2 | Postgres + InMemory 均能改 status |
| **lifecycle supersede 原子** | `tests/test_lifecycle.py` | 2 | A→B 后 A=SUPERSEDED + 边存在 + 不双 active |
| superseded 不被召回（承重升级） | `tests/test_recall_v1.py` | 2 | 此时能造 superseded，断言过滤生效 |
| **confab=0** | `tests/test_explainer.py` | 2 | 无边时不发 superseded_by claim；校验器拦截 |
| ActionBoundary 结构化触发 | `tests/test_action_boundary_trigger.py` | 2 | pytest 任务召回测试 boundary；无关任务不召回 |

## 12. 文件级改动地图（落地清单）

### 12.1 新增文件

```text
src/pulsara_agent/storage/memory_schema.py     graph_documents + memory_nodes/relations/outbox/search_index/traces DDL（均带 graph_id）
src/pulsara_agent/graph/postgres.py             PostgresGraphStore（通用 JSON-LD 存储 + canonical 投影同步；默认后端）
src/pulsara_agent/graph/mutable.py              MutableCanonicalMemoryStore 窄协议（set_status；不污染 GraphStore）
src/pulsara_agent/memory/canonical/query.py               MemoryQuery + CanonicalNodeView（fetch_nodes/lexical/fts，均带 graph_id）
src/pulsara_agent/memory/canonical/unit_of_work.py        MemoryWriteUnitOfWork + CandidateDecisionRepository + OutboxRepository（共享 connection；暴露绑定 graph 的 memory_write_service/lifecycle）
src/pulsara_agent/memory/recall/service.py              MemoryRecallService + LexicalMemoryRecallService + RecallTrigger/RecallStatus
src/pulsara_agent/memory/recall/projection.py          ProjectionBuilder（结构化 + fenced）
src/pulsara_agent/memory/recall/projection_ledger.py   ProjectionLedger（do_not_write_back 护栏）
src/pulsara_agent/memory/canonical/index_sync.py          增量同步 + rebuild（Phase 1.5）
src/pulsara_agent/memory/canonical/reconcile.py           outbox 重放 + damaged-node 检测（Phase 0/1.5）
src/pulsara_agent/memory/canonical/lifecycle.py           MemoryLifecycle（supersede/contradict/stale，Phase 2，走 W1b；关系只经投影 helper）
src/pulsara_agent/memory/recall/rerank.py              direct-relation reranker（Phase 2）
src/pulsara_agent/memory/recall/explain.py             grounded explainer + ClaimKind + 校验器（Phase 2）
src/pulsara_agent/memory/vector.py              vector channel（Phase 3，可选）
src/pulsara_agent/memory/planner.py             LLM query planner（Phase 3，可选）
src/pulsara_agent/tools/builtins/memory_query.py memory_search/get/related/explain 只读工具
evals/recall/{runner.py,config.yaml,fixtures/,baseline/}  评测护栏与 frozen floor（floor 于 Phase 1 末冻结）
```

### 12.2 改造既有文件

```text
src/pulsara_agent/runtime/state.py          LoopBudget += recall_hard_timeout_ms（默认 500）
src/pulsara_agent/runtime/agent.py          _project_memory 包 asyncio.wait_for + fail-soft（:416）
src/pulsara_agent/runtime/context.py        projection do_not_write_back 透传渲染（:165）
src/pulsara_agent/memory/hooks/durable.py   project() 真正实现；_drain_to_pool 加 echo 护栏（:33,:57）
src/pulsara_agent/memory/governance/executor.py      apply_decision 用 MemoryWriteUnitOfWork 单事务；接 lifecycle（:46）
src/pulsara_agent/memory/candidates/pool.py  append_decision 逻辑抽到 CandidateDecisionRepository（可绑定外部 connection）（:296）
src/pulsara_agent/graph/in_memory.py        实现 MutableCanonicalMemoryStore.set_status（get+改+put；GraphStore.update 仍可保持 NotImplementedError，:72）
src/pulsara_agent/entities/memory/action_boundary.py  结构化 trigger 字段（:21，Phase 2）
src/pulsara_agent/memory/canonical/write_gate.py      ActionBoundary trigger 校验（Phase 2）
src/pulsara_agent/tools/builtins/registry.py  注册只读 memory 查询工具（:26）
src/pulsara_agent/runtime/wiring.py         注入 recall service / PostgresGraphStore 默认（:213, :116）
src/pulsara_agent/storage/memory_schema.py  Phase 1.5 追加 search_index/traces；Phase 0 建 graph_documents/nodes/relations/outbox
```

### 12.3 实现顺序硬依赖

```text
0.1 DDL（graph_documents+nodes+relations+outbox, 带 graph_id）
   ├─> 0.2 PostgresGraphStore（通用存储+投影同步）+ MemoryQuery ─┬─> 1.x lexical recall ──> 1.5.x index/trace
   └─> 0.5 MemoryWriteUnitOfWork（含 decision repo + outbox repo）┘
0.3 timeout ─(需 0.4 async 底座)─> 可上任何召回
Phase 1 末 ──> 生成并冻结 v1_floor.json ──> CI 的 quality>=floor 由 informational 转 blocking
2.1 set_status（两后端）──> 2.2 lifecycle（W1b 入口）──> 2.4 rerank ──> 2.5 explainer ──> 2.7 confab CI 门槛
                                └─(前置)─> §2.3 惰性 filter 升为承重；§7 解释可展示替代/冲突
0.7 eval harness + gate skeleton ──> 0.8 leak/latency 即时生效 ──> 9.x frozen floor（Phase1 末）──> 贯穿 1/2/3 每次合入
```

## 13. 一句话收束

```text
Phase 0 先把地基浇好：Postgres canonical substrate + MemoryQuery + 可超时 + 原子写 + 评测护栏；
Phase 1 用 lexical exact + BM25/FTS 做可靠底盘，project() 真正落地，护栏防自我增殖；
Phase 1.5 把索引 durable 化、可重建、可追踪、可降级；
Phase 2 先补 lifecycle producer（状态转换 + 双向边 + 原子多节点），再上 grounded reranker/explainer，confab=0 进 CI；
Phase 3 的 hybrid/vector/planner/Oxigraph 各自凭 eval 与运营成本赢得默认位置；
frozen lexical+BM25 floor 是一条任何增强都不许跌破的版本化绝对底线。
```
