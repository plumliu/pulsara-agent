# Memory Governance Write / Outbox Contract

_Created: 2026-07-04_

本文档定义 governed canonical memory 写入、governance executor、UOW、mutation outbox 与 coordinator 的长期契约。它补充 [MEMORY_SURFACES_CONTRACT.zh.md](/Users/plumliu/Desktop/python_workspace/pulsara_agent/contracts/MEMORY_SURFACES_CONTRACT.zh.md)：后者描述 memory surface 边界，本文件描述写入执行和异步物化。

相关代码：

- [src/pulsara_agent/memory/governance/engine.py](/Users/plumliu/Desktop/python_workspace/pulsara_agent/src/pulsara_agent/memory/governance/engine.py)
- [src/pulsara_agent/memory/governance/executor.py](/Users/plumliu/Desktop/python_workspace/pulsara_agent/src/pulsara_agent/memory/governance/executor.py)
- [src/pulsara_agent/memory/governance/coordinator.py](/Users/plumliu/Desktop/python_workspace/pulsara_agent/src/pulsara_agent/memory/governance/coordinator.py)
- [src/pulsara_agent/memory/governance/relatedness.py](/Users/plumliu/Desktop/python_workspace/pulsara_agent/src/pulsara_agent/memory/governance/relatedness.py)
- [src/pulsara_agent/memory/canonical/unit_of_work.py](/Users/plumliu/Desktop/python_workspace/pulsara_agent/src/pulsara_agent/memory/canonical/unit_of_work.py)
- [src/pulsara_agent/memory/canonical/mutation_outbox.py](/Users/plumliu/Desktop/python_workspace/pulsara_agent/src/pulsara_agent/memory/canonical/mutation_outbox.py)
- [src/pulsara_agent/memory/canonical/index_sync.py](/Users/plumliu/Desktop/python_workspace/pulsara_agent/src/pulsara_agent/memory/canonical/index_sync.py)
- [src/pulsara_agent/memory/canonical/vector_index_sync.py](/Users/plumliu/Desktop/python_workspace/pulsara_agent/src/pulsara_agent/memory/canonical/vector_index_sync.py)
- [src/pulsara_agent/memory/canonical/oxigraph_materializer.py](/Users/plumliu/Desktop/python_workspace/pulsara_agent/src/pulsara_agent/memory/canonical/oxigraph_materializer.py)
- [tests/test_memory_governance.py](/Users/plumliu/Desktop/python_workspace/pulsara_agent/tests/test_memory_governance.py)
- [tests/test_memory_governance_coordinator.py](/Users/plumliu/Desktop/python_workspace/pulsara_agent/tests/test_memory_governance_coordinator.py)
- [tests/test_canonical_mutation_outbox.py](/Users/plumliu/Desktop/python_workspace/pulsara_agent/tests/test_canonical_mutation_outbox.py)
- [tests/test_memory_vector_index_sync.py](/Users/plumliu/Desktop/python_workspace/pulsara_agent/tests/test_memory_vector_index_sync.py)

---

## 1. 核心立场

Governed canonical memory 写入只有一条 executor 路径：

```text
candidate pool
  -> MemoryGovernanceEngine
  -> MemoryGovernanceExecutor.apply_decision()
  -> GovernanceWriteUnitOfWork
  -> canonical Postgres graph rows + decisions + outbox
  -> stored AgentEvents
  -> async materializers
```

不存在 no-UOW fallback。`memory_write_uow_factory` 必填；显式传 `None` 必须构造失败。

---

## 2. Production substrate

生产 canonical authority 是 PostgreSQL：

- canonical graph substrate；
- candidate pool；
- governance decisions；
- mutation outbox；
- runtime event parent rows；
- search/vector substrate。

Oxigraph 是异步派生面，不是生产 governed memory 的原子写入口。

InMemory UOW 只允许显式测试/兼容路径使用；不得作为生产 fallback，不提供 durability、transaction atomicity 或 async materialization 契约。

---

## 3. GovernanceWriteUnitOfWork

UOW structural contract 必须提供：

- graph；
- decisions repository；
- outbox repository；
- lifecycle；
- memory write service；
- resolved graph id；
- `ensure_event_context_rows()`；
- context manager transaction boundary。

`MemoryWriteUnitOfWork` 必须在同一个 PostgreSQL transaction 内写：

- candidate/decision rows；
- canonical graph rows；
- lifecycle mutation；
- mutation outbox row；
- synthetic governance event parent rows。

异常时 rollback。成功时 commit。

---

## 4. Executor safety gates

Executor 必须保留以下 safety gates：

- candidate missing/invalid -> skip/no-write；
- scope 不在 allowed write scopes -> skip/no-write；
- exact duplicate already exists -> skip/no-write；
- supersede target type/status/scope 校验；
- contradiction target type/status/scope 校验；
- target drift transaction 内重读；
- relatedness allowlist 强制；
- partial/unavailable relatedness 禁止 destructive lifecycle action；
- supersede single target；
- contradiction single target；
- replacement evidence required for supersede。

当 supersede/contradiction 被安全门拦下时，必须 downgrade to coexist/submit-as-is 语义，而不是执行破坏性 lifecycle action。

---

## 5. Relatedness side path

Semantic relatedness 是 governance destructive lifecycle 的授权 side path，不是普通 memory 落盘主路径。

规则：

- duplicate/coexist/submit_as_is 是主路径。
- 只有 relatedness 找到可信 canonical target 且 availability FULL 时，Flash 才可选择 supersede/contradict 分叉。
- executor 必须强制 lifecycle target 来自 candidate 的 relatedness allowlist。
- relatedness context 必须携带每个 surfaced canonical target 的 monotonic `node_revision`；executor 在 governance UOW 内锁定 canonical document 与 projection revision 后精确比较，防止模型决策与 mutation 之间的 target TOCTOU。
- relatedness partial/unavailable 仍可允许非破坏性 submit/coexist，但不得允许 supersede/contradict。
- same-batch staged-but-uncommitted sibling visibility 是 V1 deferred gap；committed-but-unindexed 可由 bounded inline gap embedding best-effort 补齐。

---

## 6. Mutation outbox

Canonical mutation outbox 是所有异步 materialization surface 的统一队列。

Mutation lane：

- `governed_memory`
- `runtime_semantic`
- `graph_reset`

Surface：

- `search_index`
- `vector_index`
- `oxigraph`

Payload 必须包含：

- mutation lane；
- dirty memory ids；
- JSON-LD documents；
- per-surface apply status；
- source runtime ids（runtime semantic lane）；
- graph reset flag（reset lane）。

Outbox top-level status 是 per-surface status 的 summary：

- all pending -> `pending`
- all applied -> `applied`
- any failed -> `failed`
- mixed nonfailed -> `partial`

---

## 7. Async surfaces

Governed memory executor 的 async surfaces 必须由 wiring 决定：

- durable wiring 默认包含 `search_index` 与 `oxigraph`；
- 仅当 retrieval embedding provider 存在时包含 `vector_index`；
- in-memory compatibility UOW 不提供 outbox materialization。

新增 async surface 必须进入同一个 outbox lane，不得另开第二条队列。

---

## 8. Coordinator

`MemoryGovernanceCoordinator` 是 application-level safe-point runner。

规则：

- 只在有 pending candidate 时 notify 生效。
- 按 runtime session debounce。
- 按 session minimum interval 限频。
- 调用 `engine.run_pending(trigger_reason="turn_safe_point")`。
- 若 result applied 且 `on_commit` 存在，调用 `on_commit()` 唤醒 downstream worker。
- `aclose()` 只设置 stop/wake；不得直接关闭 retrieval providers。

Coordinator 不属于 memory hook；它是 HostCore-owned worker，生命周期见 retrieval contract。

---

## 9. Stored events publishing

Executor 在 UOW commit 后写 runtime events 到 event log。

规则：

- `event_log.extend()` 返回 canonical stored events。
- 若 `stored_event_publisher` 存在，必须 publish stored events，避免 active session publisher sequence gap。
- governance write events 必须有 event context parent rows。

---

## 10. 禁止事项

- 不允许恢复 legacy no-UOW branch。
- 不允许 executor 自动选择 InMemory UOW fallback。
- 不允许生产 governed memory 写跳过 PostgreSQL UOW。
- 不允许 Oxigraph 成为 governed memory 的同步权威写入口。
- 不允许 supersede/contradict 在 relatedness context missing/partial/unavailable 时执行 lifecycle action。
- 不允许 Flash 选择未 surfaced 的 canonical target 后 executor 仍放行。
- 不允许 vector materialization 使用独立队列。
- 不允许 coordinator 每 turn 无条件跑 Flash governance。

---

## 11. 测试守护

最低测试门槛：

- executor missing UOW factory fails。
- durable wiring injects PostgreSQL `MemoryWriteUnitOfWork`。
- in-memory wiring only via explicit compatibility/test factory。
- submit/merge/correct/skip/supersede/contradict all use UOW path。
- supersede/contradict without relatedness context are downgraded with diagnostic。
- target not in relatedness allowlist is rejected/downgraded。
- target drift re-read causes downgrade/regovernance diagnostic。
- target revision drift causes downgrade/regovernance，且旧 canonical node 保持不变。
- Postgres UOW writes graph + decision + outbox atomically。
- failed write rolls back outbox mutation.
- outbox surface status summarizes pending/applied/failed/partial。
- index/vector/oxigraph consumers mark surface applied/failed.
- coordinator debounces and wakes vector worker on commit.

---

## 11. Governance source evidence 与 batch ownership hard cut

Memory governance 不得扫描整轮 EventLog、读取 raw model stream segment，或从序列化
tool result 猜测 candidate provenance。Production input 只能来自以下三层 typed facts：

- `GovernanceSourceEvidenceSemanticFact`：candidate、accepted marker、tool/result 或
  reflection/compaction semantics；不得包含 sequence、artifact placement、segment policy；
- `GovernanceSourceEvidenceAttributionFact`：exact stored event/artifact refs、producer
  identity 与 source high-water；
- `GovernanceEvidencePromptProjectionFact`：引用完整 semantic fingerprint 的 bounded
  model-visible projection，不替代 canonical evidence。

MAIN_AGENT_TOOL evidence 必须 join completed terminal projection、`ACCEPTED` control
disposition、exact tool call/pair/result 与 canonical user span。REFLECTION 与 COMPACTION
producer 必须先原子提交 producer event、ledger materialization account transition 与
`memory_candidate_projection_outbox`；dispatcher 只能从 confirmed producer event 幂等投影
candidate row。Candidate row 先于 producer carrier、或 producer event 失败后仍发布 candidate，
均为禁止状态。`CandidateOrigin.GOVERNANCE` 继续只作 canonical-write audit attribution，
不得重新进入治理输入。

候选选择与 durable claim 必须在同一事务中 all-or-none 完成。同一 candidate 同时只能有
一个 `preparing | prepared` owner。Evidence invalid 是 system-owned
`MemoryCandidateEvidenceRejectedEvent` terminal outcome；canonical reducer、artifact hash、
historical binding 或 EventLog proof 不可信时必须 latch 整个 governance batch，不能伪装成
模型 `skip`。

Governance 调用顺序固定为：

```text
claim
-> atomic transcript authority snapshot
-> typed evidence + relatedness freeze
-> resolve exact model call
-> persist content-addressed GovernanceBatchInputSnapshotFact
-> MemoryGovernanceBatchPreparedEvent FULL
-> ModelCallStartEvent
-> decisions/UOW
-> one terminal governance batch event
```

Batch input artifact必须保存 exact system prompt、ordered messages、resolved call、target、
context ID、token estimate与完整 input fingerprint。Prepared、governance ModelStart、decision
record和terminal event必须 join 同一 batch-input/reference fingerprint。Prepared recovery只可
rebind artifact中冻结的historical target/call，不得读取当前配置重新 resolve。

`GovernanceBatchExecutionOwnerRegistry`是 session-owned physical owner。Caller cancel、detach
或 coordinator timeout不取消已准入 batch；close先停止 admission，再 bounded drain owner。
PREPARING/PREPARED 状态必须从 durable claims、preparation locator与lifecycle events恢复；
partial decision suffix按 stable decision identity幂等续写，不得重新治理已经提交的prefix。
