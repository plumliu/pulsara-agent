# 记忆召回系统与 Agent Runtime 接线审计

_Created: 2026-06-29_

本文聚焦一个问题：`MEMORY_RECALL_EVERMEMOS_NEXT_DESIGN.zh.md` 中规划的 semantic / hybrid recall，真正接入当前 Agent Runtime 时应该落到哪些代码位置，以及哪些现有边界不能被忽略。

本文不是新的产品设计，也不替代：

- `MEMORY_RECALL_EVERMEMOS_NEXT_DESIGN.zh.md`：下一代召回目标设计。
- `contracts/MEMORY_SURFACES_CONTRACT.zh.md`：冻结的 memory surface 与写回边界。

本文以当前代码为事实来源，记录 integration gap、实施顺序和风险。

---

## 0. 核心结论

当前系统不是“缺一个 vector query 就能升级成 semantic recall”。真正的接线跨越六个边界：

1. Postgres vector substrate 与 unified outbox。
2. retrieval provider 的 async lifecycle。
3. 自动 projection 的 async agent loop。
4. 显式 `memory_search` 的同步 Tool / worker-thread 边界。
5. governance safe-point 与 relatedness。
6. HostSession shutdown 与后台 worker 生命周期。

最重要的代码事实：

- 自动 recall 已经运行在 Agent Runtime 的主 async loop 内。
- 显式 `memory_search` 是同步 Tool，在 worker thread 中执行，并在 tool 内再次调用 `asyncio.run()`。
- `memory_search` 标记为 `is_concurrency_safe=True`，因此同一批多个搜索可能在不同 worker thread、不同 event loop 中并发访问同一个 recall/provider 实例。
- `MemoryGovernanceEngine` 已在 wiring 中构造，但正常 Host/Agent 路径没有调用 `run_pending()`。
- outbox replay 是 runtime event observer hook；当前只消费 FTS 与 Oxigraph surface，没有独立 vector worker。
- `RuntimeSession.close()` / `AgentRuntime.close()` / `HostSession.close()` 都是同步关闭接口，目前只管理 terminal resources，不管理 async retrieval provider。

因此，在 PR A-C 前应增加一个很小但必要的 **Integration PR 0**：先冻结 async tool/provider ownership、governance trigger 和 index worker 的运行模型。

---

## 1. 当前运行时对象图

### 1.1 Durable wiring

入口：`src/pulsara_agent/runtime/wiring.py::build_durable_runtime_wiring`

当前 durable wiring 构造：

- `PostgresGraphStore`
- optional `OxigraphGraphStore`
- `DurableGraphFacade`
- `PostgresCandidatePool`
- `PostgresMemoryQuery`
- `LexicalMemoryRecallService`
- `PostgresRecallTraceStore`
- `MemoryGovernanceExecutor`
- `CanonicalMutationOutboxReplayHook`

当前明确写死的是：

```python
memory_recall_service = LexicalMemoryRecallService(
    memory_query=memory_query,
    trace_store=PostgresRecallTraceStore(...),
)
```

`PulsaraSettings.retrieval` 已存在，但 wiring 没有调用：

- `build_tokenizer(...)`
- `build_embedding_provider(...)`
- `build_rerank_provider(...)`

所以 provider 目前是可独立调用的基础设施，不是 runtime capability。

### 1.2 Agent Runtime 注入

入口：`src/pulsara_agent/runtime/wiring.py::build_agent_runtime_wiring`

`RuntimeWiring.memory_recall_service` 被注入：

1. `DurableMemoryHooks.recall`
2. `AgentRuntime.tool_executor` 中的 `MemorySearchTool`

这保证了自动 recall 与显式 recall 共用一个 `MemoryRecallService` 实例，也意味着未来 provider/client 同样会被两条路径共享。

### 1.3 Host 生命周期

入口：

- `src/pulsara_agent/host/core.py::HostCore.open_session`
- `src/pulsara_agent/host/session.py::HostSession.close`
- `src/pulsara_agent/runtime/agent.py::AgentRuntime.close`
- `src/pulsara_agent/runtime/session.py::RuntimeSession.close`

当前关闭链：

```text
HostCore.close_session
  -> HostSessionRegistry.close_session
  -> HostSession.close
  -> AgentRuntime.close
  -> RuntimeSession.close
  -> terminal session shutdown / kill_owned
```

整条关闭链是同步接口，没有 retrieval resource owner，也没有 `await provider.aclose()` 的位置。

---

## 2. 当前自动 recall 调用链

### 2.1 时序

```text
AgentRuntime._stream_model_loop
  -> AgentRuntime._project_memory
  -> asyncio.wait_for(memory_hooks.project(...), configurable deadline; default 500ms)
  -> DurableMemoryHooks.project
       -> sync working_context read
       -> latest user quote
       -> RecallQuery(trigger=CHEAP_AUTO)
       -> await recall.recall(...)
  -> state.memory_projection
  -> build_llm_context
  -> projection appended to system prompt
  -> LLM call
```

关键位置：

- `src/pulsara_agent/runtime/agent.py::_project_memory`
- `src/pulsara_agent/memory/hooks/durable.py::DurableMemoryHooks.project`
- `src/pulsara_agent/runtime/context.py::_system_prompt_with_projection`

### 2.2 已有保护

- configurable hard timeout，来自 `LoopBudget.recall_hard_timeout_ms`；当前默认值为 500ms。
- recall timeout 只产生 `ProjectionFailedEvent`，不会阻断模型调用。
- `CHEAP_AUTO` recent suppression 防止重复注入。
- `ProjectionLedger` + `do_not_write_back="true"` 防 echo write-back。
- scope 来自 `MemoryDomainContext.read_scopes`。

### 2.3 semantic recall 接入点

`DurableMemoryHooks` 不应知道 sparse/vector/reranker 细节。正确替换点是 wiring 中的 service：

```text
LexicalMemoryRecallService
  -> HybridMemoryRecallService
```

只要 `HybridMemoryRecallService` 继续实现现有 `MemoryRecallService` protocol，hook 与 projection builder 可保持不动。

但是 automatic path 的 policy 必须显式传入 hybrid service：

- v1 默认开启 vector/dense；query-embedding cache用于降本降延迟，但cache miss不关闭dense。
- reranker 默认关闭或严格 top-M。
- provider timeout 必须小于当前 trigger 配置的 runtime hard timeout；默认 `CHEAP_AUTO` 外层预算为 500ms。
- provider failure 降级 sparse-only。
- remote call cancellation 必须可观察。

---

## 3. 当前显式 `memory_search` 调用链

### 3.1 时序

```text
LLM emits memory_search ToolCall
  -> AgentRuntime._stream_parsed_tool_calls
  -> _tool_batches
  -> asyncio.to_thread(ToolExecutor.execute)
  -> MemorySearchTool.execute                 # sync
  -> asyncio.run(recall.recall(...))          # new event loop in worker thread
  -> tool result events
  -> next model turn
```

关键位置：

- `src/pulsara_agent/runtime/agent.py::_stream_tool_batch_events`
- `src/pulsara_agent/tools/executor.py::ToolExecutor.execute`
- `src/pulsara_agent/tools/builtins/memory_query.py::MemorySearchTool.execute`

### 3.2 当前并发语义

`MemorySearchTool` 同时声明：

```python
is_read_only = True
is_concurrency_safe = True
```

Agent Runtime 会把连续的 concurrency-safe read-only tools 放进同一批，并为每个 call 创建独立 worker thread。

如果同一条模型回复包含两个 `memory_search`：

```text
shared HybridMemoryRecallService
  -> worker thread A -> event loop A -> shared provider/client/semaphore
  -> worker thread B -> event loop B -> shared provider/client/semaphore
```

当前 lexical service 的数据库路径大体可承受这种模型，因为每次查询临时建立独立 Postgres connection。async provider 不具备这个默认保证。

### 3.3 推荐改法

首选方案：给 Tool Runtime 增加 async tool 能力，而不是继续在 `MemorySearchTool` 内做 `asyncio.run()`。

建议形状：

```python
class AsyncTool(Protocol):
    async def execute_async(
        self,
        call: ToolCall,
        *,
        event_context: EventContext,
    ) -> ToolExecutionResult: ...
```

`ToolExecutor` / Agent Runtime 根据能力选择：

- async tool：在 Agent Runtime 所在 loop 内执行。
- sync/blocking tool：继续 `asyncio.to_thread(...)`。

`MemorySearchTool` 改为 async tool 后：

- automatic recall 与 explicit recall 可共享同一个 event loop。
- `AsyncOpenAI` / `httpx.AsyncClient` / `asyncio.Semaphore` 不跨 loop。
- cancellation 能沿 task 传播。
- EventContext 可直接进入 `RecallQuery`。

短期兼容方案是在 runtime 内维护一个专用、长期存活的 async bridge loop；但复杂度高于原生 async tool，且 HostSession shutdown 仍需关闭 bridge 与 provider。

不建议把“每次搜索新建 provider/client”作为默认方案，因为它会丢失连接池、TLS session 和限流器共享收益。

---

## 4. 当前 recall core 与替换边界

### 4.1 当前 `LexicalMemoryRecallService`

位置：`src/pulsara_agent/memory/recall/service.py`

当前职责混合在一个类中：

- backend cooldown。
- query token 提取。
- lexical candidates。
- FTS candidates。
- RRF。
- canonical fetch/filter。
- recent suppression。
- contradiction companion expansion。
- direct relation rerank。
- trace write。

这解释了最新设计为什么要求拆为：

- `SparseCandidateService`
- `DenseCandidateService`
- `RecallRerankService`
- `HybridMemoryRecallService`

### 4.2 建议文件落点

建议保留 provider-neutral 的 `retrieval/`，把 memory-specific orchestration 放回 `memory/recall/`：

```text
src/pulsara_agent/retrieval/
  tokenizer/...
  embedding/...
  rerank/...

src/pulsara_agent/memory/recall/
  candidates.py          # SparseCandidate / DenseCandidate / batch models
  sparse.py              # SparseCandidateService
  dense.py               # DenseCandidateService
  semantic_rerank.py     # RecallRerankService
  hybrid.py              # HybridMemoryRecallService
  service.py             # shared query/result protocol models; compatibility shim only
  projection.py
  trace.py
```

不要让 `retrieval/embedding` 直接依赖：

- `CanonicalNodeView`
- memory scope/status
- Postgres schema
- recall trigger policy

这些都属于 memory service 层。

### 4.3 Query substrate 建议

不要把 query embedding 注入现有 `PostgresMemoryQuery`。建议新增：

```text
src/pulsara_agent/memory/canonical/vector_query.py
```

职责只包括：

- 接受已经计算好的 query vector。
- 查询 `memory_vector_index`。
- 按 fingerprint / graph / scope / type/status 做粗过滤。
- 返回 id + cosine score。

`DenseCandidateService` 才负责：

- 调 provider embed query。
- 选择 fingerprint。
- 调 vector query。
- 形成 channel diagnostics。

这样 governance relatedness 可以复用 embedding/vector primitive，而不必依赖完整 recall orchestrator。

---

## 5. Vector schema 与索引同步落点

### 5.1 Schema

主要位置：

- `src/pulsara_agent/storage/memory_schema.py`
- `docker-compose.yml`
- `pyproject.toml`

需要：

- pgvector extension。
- `memory_vector_index`。
- HNSW cosine index。
- FK cascade 到 `memory_nodes`。
- Python pgvector adapter，或稳定的 vector literal/cast 边界。

当前 `docker-compose.yml` 使用 `postgres:16-alpine`，镜像不自带 pgvector。开发环境必须切换到 pgvector-enabled image 或构建扩展镜像。

当前 schema 在多个 store 的 `__post_init__` 中自动执行。直接加入 `CREATE EXTENSION vector` 会引入 managed Postgres 权限问题。需要明确：

- extension 是部署/bootstrap 前置；还是
- application schema bootstrap 在有权限时创建。

生产环境不应假设应用连接拥有 `CREATE EXTENSION` 权限。

### 5.2 Embedded text

建议新增：

```text
src/pulsara_agent/memory/canonical/embedded_text.py
```

输入是 canonical Postgres projection/document，输出稳定字符串与 hash。它必须：

- deterministic。
- 不读 evidence full text / run transcript / recalled projection。
- 对 aliases 排序、去重。
- 明确版本号；builder 格式变化必须触发 re-embed。

`embedded_text_hash` 最好包含 builder version，否则只改拼接规则而 canonical 字段不变时，旧 embedding 会被错误跳过。

### 5.3 Vector sync

建议新增：

```text
src/pulsara_agent/memory/canonical/vector_index_sync.py
```

它应与 `MemorySearchIndexSync` 共用同一 outbox row/surface state，但不能机械复制当前实现。

当前 FTS consumer 在 `SELECT ... FOR UPDATE SKIP LOCKED` 所在事务中完成全部本地 SQL。这对 FTS 尚可；vector consumer 若在持锁事务中调用远端 embedding API，会造成：

- 长事务。
- 行锁持有几十秒。
- provider retry 期间阻塞其它 surface。
- worker crash 后事务回滚、重复外部调用。
- 数据库连接被网络 latency 占住。

建议使用 claim/lease 两阶段：

```text
transaction A:
  claim mutation/fingerprint work
  commit

outside transaction:
  build text
  call embedding provider

transaction B:
  verify claim/version still current
  upsert vector row
  mark vector surface applied
  commit
```

所有操作必须按 `(graph_id, memory_id, fingerprint, embedded_text_hash)` 幂等。

### 5.4 Unified outbox 修改点

位置：

- `src/pulsara_agent/memory/canonical/mutation_outbox.py`
- `src/pulsara_agent/memory/governance/executor.py`
- `src/pulsara_agent/memory/canonical/reconcile.py`
- `src/pulsara_agent/memory/canonical/outbox_replay_hook.py`

需要新增 vector surface，但不能新增第二条 vector-only outbox lane。

当前 governance executor 硬编码：

```python
async_surfaces=(SEARCH_INDEX, OXIGRAPH)
```

这必须改为由 durable wiring 注入启用的 surface set。否则 retrieval disabled 的部署仍会产生永远 pending 的 vector surface。

另一个容易漏掉的细节是：`runtime_semantic_mutation_payload(...)` 与 `graph_reset_mutation_payload(...)` 的 `async_surfaces` 默认值都只有 `(OXIGRAPH,)`。新增 `CanonicalMutationSurface.VECTOR_INDEX` 枚举不会让任何 mutation 自动获得 vector `PENDING` 状态；每个 mutation lane / deployment 必须显式决定 surface 集合。具体要求：

- governed-memory mutation 在 vector-enabled 部署必须显式包含 `VECTOR_INDEX`。
- vector-disabled 部署必须显式排除它，避免 outbox 永久 partial。
- runtime-semantic / graph-reset lane 是否需要 vector 必须分别决定，不能把默认 tuple 当成“所有派生 surface”。
- 增加枚举但忘记修改 payload caller 时，vector consumer甚至看不到待处理状态，会静默不物化，而不是显式失败。

---

## 6. Outbox worker 与 runtime event hook

### 6.1 当前事实

`CanonicalMutationOutboxReplayHook` 在以下事件后同步调用 reconciler：

- `REPLY_END`
- `RUN_ERROR`
- `EXCEED_MAX_ITERS`
- `RUN_END`

observer hook 虽然声明为 async，但内部直接执行同步 Postgres/Oxigraph I/O。hook manager 会 await 它，所以它会占用 Agent Runtime event loop。

### 6.2 Vector consumer 不能直接塞进该 hook

如果在 `replay_outbox()` 中直接加入 remote embedding：

- 每个 `REPLY_END` 可能等待 provider。
- agent 在 after-model-reply / tool execution 前被阻塞。
- provider 30s timeout 会外溢到主交互延迟。
- hook 异常只进入 `RuntimeHookManager.errors`，用户未必看到 index lag。

推荐模型：

- runtime event hook 只负责发送 non-blocking wake-up/nudge。
- 独立 index worker 消费 unified outbox。
- worker 可进程内后台 task，也可独立进程；接口不应绑死。
- `sync_memory/rebuild` 保留为 repair/backfill 管理入口。

如果先做进程内 worker：

- owner 应是 HostCore/application lifecycle，而不是单个 Agent turn。
- 多个 HostSession 共享相同 graph 时不能重复创建无协调 worker。
- shutdown 必须 drain/cancel worker 并关闭 provider。

### 6.3 Ordering pitfall

当前 outbox 查询虽然 `ORDER BY sequence_key, created_at, outbox_id`，但 `FOR UPDATE SKIP LOCKED` 本身不能阻止两个 worker 同时取得同一 `sequence_key` 的前后两条 mutation。

semantic indexing 引入多 worker 后，应增加：

- per-sequence advisory lock；或
- 只允许领取每个 sequence 的 head row；或
- 明确可交换并通过 version/hash 丢弃 stale completion。

仅有 SQL `ORDER BY` 不等于并发下的严格顺序。

---

## 7. Governance 连接点

### 7.1 当前事实

`build_agent_runtime_wiring` 会构造 `MemoryGovernanceEngine` 并放入 `RuntimeWiring`。

但当前 `src/` 中没有正常 Host/Agent 调用：

```python
memory_governance_engine.run_pending(...)
```

现有调用主要来自测试。也就是说：

- agent/reflection 可以把 candidate 放入 pool。
- governance engine 可以手动运行。
- 但 HostSession turn completion 不会自动 govern pending candidates。

这不一定是当前 bug；它可能代表治理故意由外部 scheduler/operator 驱动。但下一代 vector indexing 必须知道真实触发模型，否则 governance commit 后没有可靠的 worker wake-up。

### 7.2 必须先拍板的 safe-point

可选方案：

1. Host turn 完成后自动运行 governance。
2. 独立 governance worker 监听 candidate pool。
3. 显式 host/API 操作触发 governance。

推荐独立 coordinator/worker，不建议把 Flash governance 塞进 `DurableMemoryHooks.on_session_end`：

- memory hook failure 当前会影响 run finalization 语义。
- governance 是额外 LLM 调用，延迟与失败模式独立。
- reflection 在 on-turn-end 才可能产生候选。
- outbox replay 的 `RUN_END` hook 发生时序可能早于随后发生的 governance commit。

### 7.3 Semantic relatedness 落点

当前 token-overlap 位于：

```text
src/pulsara_agent/memory/governance/engine.py::_related_existing_memories
```

建议新增：

```text
src/pulsara_agent/memory/governance/relatedness.py
```

协议应接受：

- candidate statement/type/scope。
- Postgres synchronous canonical views。
- current batch staged candidates/docs。
- embedding/rerank budget。

然后注入 `MemoryGovernanceEngine`。不要让 governance engine 自己构造 provider。

### 7.4 `_candidate_snapshot` 需要 async/batch 化

当前 `_candidate_snapshot()` 是同步函数，`run_pending()` 用 list comprehension 一次构造所有 snapshot。未来 semantic relatedness 需要 async provider，不能在这里继续套 `asyncio.run()`。

建议：

- `await relatedness.collect_batch(pending, ...)` 一次 embed batch。
- 再构造 snapshot。
- 避免每个 candidate 单独 remote request。

### 7.5 Same-batch 并非“注入 service”就自动解决

当前所有 snapshots 在任何 decision apply 之前生成。即使 relatedness service 改读 `memory_nodes`，本 batch 后续将写入的节点仍不会作为canonical memory出现在同步面。

但这里必须区分两种visibility：

1. **candidate-level visibility已经存在**：`run_pending()`把整批pending snapshots放进同一个`governance_input.candidates`，Flash能同时看到siblings；prompt和decision model也已支持`merge_and_submit(target_entry_ids=[...])`。因此同batch duplicate/近义candidate可以直接合并，并不依赖`related_existing_memories`。
2. **canonical lifecycle visibility尚不存在**：`supersede_and_submit.superseded_memory_ids`与`contradict_and_submit.contradicted_memory_ids`必须引用`related_existing_memories`中的已提交canonical id。两个尚未apply的新siblings没有可引用memory id，不能在一次plan-before-apply中建立相互的supersede/contradiction edge。

v1固定选择**deferred canonical lifecycle gap**：

- 保留整批candidate可见与`merge_and_submit`，不放弃同batch去重/合并。
- 不为两个新siblings制造provisional canonical id。
- 不在v1强行建立新siblings之间的canonical supersede/contradiction edge。
- 允许它们暂时以两个active memories共存；只有未来新candidate、显式maintenance/reconciliation或后续两阶段治理才可能补边。
- 记录`same_batch_lifecycle_deferred`诊断/指标，评估真实发生率，再决定是否升级。

这不是天然的one-batch最终一致：已经获得terminal decision的candidate会被`list_pending()`排除，下一batch不会自动重审它。若未来要求即时canonical lifecycle edge，再选择two-phase plan/apply；单纯增加staged advisory只会帮助模型看见siblings，而模型今天已经能从整批input看见它们。

---

## 8. Trace、EventContext 与可观测性

### 8.1 Automatic recall trace 正常

`DurableMemoryHooks.project()` 构造 `RecallQuery` 时包含：

- session_id
- run_id
- turn_id
- reply_id

因此 automatic recall 能写 `recall_traces` / `recall_usages`。

### 8.2 `memory_search` 实际没有写 trace

`MemorySearchTool.execute()` 构造的 `RecallQuery` 没有任何 trace coordinates。

而 `LexicalMemoryRecallService._record_trace()` 要求四个 coordinate 全部存在，否则直接 return。

结果是：

- service-level 测试能证明 EXPLICIT_SEARCH trace，因为测试手动传 coordinate。
- 真实 `memory_search` tool path 不会记录 trace/usage。

修复落点：

- `MemorySearchTool` 实现带 `EventContext` 的 async execute。
- `RuntimeSession.runtime_session_id` 也要传入 tool，或扩展 tool execution context。
- 构造完整 `RecallQuery`。

这是 semantic eval、reranker cost accounting 和 agentic search diagnostics 的前置修复。

### 8.3 Trace schema 扩展

建议不要继续向顶层表无限加列。增加 JSONB metadata/channel diagnostics：

- fingerprint。
- channel candidate ids/scores。
- channel latency。
- channel degraded reason。
- fusion policy/version。
- reranker model/results。
- index coverage/lag。

模型可见 tool payload保持简洁，完整诊断进入 trace。

### 8.4 错误不能全吞

当前 `_record_trace()` 对任何 trace exception 都直接忽略。这符合“trace 失败不能让 recall 失败”，但需要另外的 metrics/logging，否则 semantic rollout 时会出现“召回工作、诊断静默丢失”。

至少应计数：

- trace write failure。
- embedding degraded。
- reranker degraded。
- vector index missing/mismatch。
- outbox lag。

---

## 9. 具体技术陷阱

### P0-1：shared async provider 跨 thread / event loop

触发条件：

- automatic recall 在主 loop 使用 provider。
- explicit recall 在 worker thread 的新 loop 使用同一 provider。
- 或一批多个 `memory_search` 并发。

可能影响：

- `AsyncClient` 连接池绑定旧 loop。
- `asyncio.Semaphore` 在竞争时绑定不同 loop。
- `Event loop is closed`。
- close 与进行中 request race。

解决：原生 async tool + single owner lifecycle；接线前不得忽略。

2026-06-29 编码前 live preflight已复现：同一`DashScopeRerankProvider`实例第一次`asyncio.run()`成功，第二次跨loop调用在复用`httpx.AsyncClient`连接时触发`RuntimeError: Event loop is closed`。同一轮probe中embedding provider恰好连续成功，但这不构成跨loop安全保证；两类provider仍必须统一收口到单loop owner。

### P0-2：remote embedding 期间持有 outbox DB lock

不能照抄 FTS `consume_outbox()` 的单事务结构。使用 claim/lease + idempotent finalize。

### P0-3：governance commit 后没有确定的 worker wake-up

governance engine 当前没有正常 runtime scheduler；event replay hook 也不保证发生在手动 governance commit 后。必须定义 coordinator。

### P0-4：开发 Postgres 没有 pgvector

`postgres:16-alpine` 不包含 vector extension。schema code 写完不等于环境可运行。

### P1-1：一个 HNSW 混合多个 embedding fingerprint

设计允许同维度 old/new fingerprint 并存，但单一 HNSW index 覆盖所有模型空间会有问题：

- 不同模型的向量距离没有语义可比性。
- fingerprint/status/scope 过滤与 approximate index 结合可能 under-return。

可选策略：

- active fingerprint 使用 partial HNSW index。
- 按 fingerprint partition/table。
- shadow fingerprint 独立索引。
- 使用 pgvector iterative scan + 足够 overfetch，并通过 eval 验证。

不要只在 SQL 中加 `WHERE embedding_fingerprint = ...` 就假设 ANN recall 不受影响。

### P1-2：canonical filter 在 candidate limit 之后

当前 sparse path 会先截 candidate，再 fetch canonical/filter。若 top-N 被 rejected/superseded/stale 占满，合法 active memory 可能进不了候选集。

vector path尤其需要：

- coarse status/scope/type filter尽量进入 SQL。
- overfetch。
- final canonical filter仍保留。
- filter 后不足时可补取下一页。

### P1-3：automatic configurable timeout 与 cancellation

`AgentRuntime._project_memory()` 使用可配置的 `LoopBudget.recall_hard_timeout_ms`，当前默认值为 500ms；实现不得把 500ms 硬编码到 recall/provider service。

当前 lexical service通过 `asyncio.to_thread` 执行同步 DB；task cancellation不会停止已经运行的 thread/SQL。未来：

- async provider request应响应 cancellation。
- DB thread可能继续后台运行。
- timeout路径要记录 channel diagnostics。
- provider内部 timeout必须小于外层 hard timeout，或为 CHEAP_AUTO 设置单独更小预算。

### P1-4：explicit search 没有 runtime hard timeout

`memory_search` 只受 provider自身 timeout/retry控制。默认 provider timeout 30s + retries可能长时间占用 worker。

需要 trigger-specific deadline：

- CHEAP_AUTO：严格低延迟。
- EXPLICIT_SEARCH：可更长，但必须有总 deadline。
- GOVERNANCE_RELATEDNESS：独立预算。

### P1-5：CJK tokenizer 与 FTS index 不对称

新增 jieba query tokenizer并不会自动改善 PostgreSQL FTS。如果 index 仍是：

```sql
to_tsvector('simple', raw_unsegmented_text)
```

而 query 端使用 jieba token，双方 lexeme 可能不一致。

必须决定：

- index sync 将 tokenizer 输出以空格拼接后写入 FTS；或
- 使用合适的 Postgres tokenizer/config；或
- jieba只服务 lexical channel，不声称改善 FTS。

如果 index tokenization 变化，需要 tokenizer fingerprint 与 rebuild。

### P1-6：projection budget 是两个 projection 各自使用后直接合并

`working_context_projection` 与 recall projection 都收到完整 `token_budget`，随后 `_merge_projections()` 直接拼接，没有再次全局裁剪。

semantic recall增加更丰富 why/diagnostics后更容易超过总 projection budget。应在 merge 后做最终 budget enforcement，或在 hook 中明确分配预算。

### P1-7：echo ledger 只保存最近一次 record 集合

`ProjectionLedger.record()` 覆盖 scratchpad 中旧 fingerprints，而不是 bounded accumulation。

当一个 run 内多轮投影不同 memories 时，较早 surfaced memory 可能失去 echo guard。semantic recall候选更丰富后概率上升。

建议维护 run-local bounded union，并在 run finalize 清理。

### P1-8：surface enablement 与 outbox completion

如果 vector surface写进每条 mutation，但 provider未配置/部署禁用，outbox会永久 partial/failed。

wiring必须生成 deployment-specific enabled surfaces，rebuild/repair命令另行处理历史 fingerprint。

反方向同样危险：当前部分 mutation helper 的 `async_surfaces` 默认只有 `OXIGRAPH`。如果 vector-enabled deployment 没有在 governed-memory payload caller 中显式加入 `VECTOR_INDEX`，row不会产生 vector `PENDING` state，vector materialization会被静默跳过。枚举、deployment surface set、payload state与consumer测试必须一起提交。

### P1-9：query embedding cache

automatic recall在一个 agent run的每个模型 turn前执行，query仍是最近 user quote。工具循环较长时，同一 user text可能被重复 embed。

recent suppression只抑制 memory id，不避免 query embedding成本。可增加 run-local `(fingerprint, normalized_query)` cache，但要有小上限。

---

## 10. 推荐实施顺序

### Integration PR 0：冻结 runtime 接线模型

目标：在 vector code进入主链前解决所有权问题。

改动：

- 增加 async tool execution capability。
- `MemorySearchTool` 改 async，并传完整 runtime context。
- 定义 retrieval resource owner 与 `aclose()`。
- 定义 governance trigger/coordinator。
- v1 固定为 HostCore/application 级进程内单 vector worker，并保留可迁移到独立进程的 worker protocol/claim seam。
- 定义 governance cadence、debounce 与 worker wake-up。

测试：

- automatic recall后连续显式搜索复用同一 provider。
- 同批两个 `memory_search` 并发。
- session close等待/取消进行中provider request。
- provider close幂等且 exactly-once；drain/cancel有界超时。
- 真实 `memory_search` tool call 写入 `recall_traces`，且 `trigger_kind=explicit_search`；命中结果同时写 usage row。

### PR A：pgvector substrate

落点：

- `docker-compose.yml`
- `src/pulsara_agent/storage/memory_schema.py`
- Postgres integration tests

完成标准：

- extension可用。
- vector table/FK/HNSW可用。
- schema PK/查询边界包含 fingerprint，允许将来隔离；v1只启用一个 pinned fingerprint，具体partial-index/partition策略延后到首次model swap/shadow rollout。
- canonical JSON-LD payload无embedding字段。

### PR B：embedded text + vector worker

落点：

- `memory/canonical/embedded_text.py`
- `memory/canonical/vector_index_sync.py`
- mutation outbox surface/reconciler
- worker/coordinator

完成标准：

- single/rebuild/outbox sync。
- hash + builder version skip。
- remote call不持DB row lock。
- stale completion不覆盖新版本。
- provider down不影响governance commit。

### PR C：四层 hybrid recall

落点：

- `memory/recall/sparse.py`
- `memory/recall/dense.py`
- `memory/recall/semantic_rerank.py`
- `memory/recall/hybrid.py`
- runtime wiring

完成标准：

- sparse/vector fusion。
- vector degraded sparse fallback。
- automatic/explicit trigger policy：v1 `CHEAP_AUTO`与`EXPLICIT_SEARCH`都开启dense；auto使用严格deadline、小top-k、query cache和sparse fallback，explicit使用完整dense预算。
- canonical filter与contradiction语义保持。
-完整channel trace。

### PR D：显式搜索 reranker

完成标准：

- EXPLICIT_SEARCH top-M rerank。
-总deadline。
- failure fallback。
- provider生命周期/并发测试。
- live smoke只作shadow/dogfood，不是唯一CI gate。

### PR E：semantic governance relatedness

前置：governance coordinator与第六项决策中的same-batch产品语义已确定。

完成标准：

- async batch relatedness。
- 既有 canonical relatedness 只读同步 Postgres 面。
- 同batchcandidate snapshots继续整体提供给Flash，duplicate/近义candidate可通过`merge_and_submit`合并。
- 两个新siblings之间的canonical supersede/contradiction edge明确defer，并记录`same_batch_lifecycle_deferred`；不得承诺下一batch自动补回。
- 如未来要求最终补回，必须新增maintenance/reconciliation或two-phase治理及对应测试。
- relatedness 仅 advisory。
- executor 继续复核 target。

---

## 11. 测试矩阵

| 层 | 必测内容 | 建议位置 |
|---|---|---|
| provider | order、dimension、retry、close、concurrency | `tests/test_retrieval_providers.py` |
| schema | extension、FK、HNSW、cascade | 新增 vector schema tests |
| embedded text | deterministic、version、hash、echo exclusion | 新增 builder unit tests |
| vector worker | claim、retry、stale completion、idempotency | `tests/test_memory_vector_index_sync.py` |
| sparse/dense | channel candidates与degraded warning | service unit tests |
| hybrid | RRF、filter、contradiction、stable trim | `tests/test_recall_v2.py` |
| runtime auto | configurable timeout（默认500ms）、vector默认开启、query cache、provider失败/超时后sparse fallback | agent runtime tests |
| runtime explicit | async tool、parallel search；真实tool call写入`trigger_kind=explicit_search`的trace/usage row | runtime/tool integration tests |
| lifecycle | HostSession close、provider close、worker cancel | host lifecycle tests |
| governance same-batch merge | 整批candidate对Flash可见；两个近义siblings在apply前可形成一个`merge_and_submit` | governance integration tests |
| governance deferred lifecycle | 两个新冲突siblings不伪造canonical target/edge，记录deferred诊断且不承诺下一batch自动补回 | governance integration tests |
| governance validation | semantic relatedness仅advisory、executor继续复核target | governance integration tests |
| outbox surface registration | vector-enabled payload显式含`VECTOR_INDEX: pending`；disabled payload明确不含；不得依赖默认tuple | mutation outbox tests |
| eval | lexical regression + semantic-only hits | versioned recall eval fixtures |

---

## 12. 开工前需要明确的七个决策

决策 1、2、5 是同一个 meta 决策的三个切面：async retrieval资源由谁拥有、在哪里运行、如何关闭。它们必须在 Integration PR 0 中由同一个 `RetrievalRuntimeResources` owner 一起拍板，不能分别实现一半。

### 决策 1：Tool Runtime 是否支持原生 async tool

推荐：支持**能力分派**，而不是把所有tool全量async化。新增`AsyncTool.execute_async(...)`并与现有sync Tool并存：async tool在Agent Runtime主loop执行，sync/blocking tool继续`asyncio.to_thread(...)`。同时必须把`EventContext`与`RuntimeSession.runtime_session_id`纳入tool execution context；否则只修复event-loop问题，却仍修不好explicit recall trace gap。

### 决策 2：vector indexing worker由谁拥有

推荐：v1固定为**进程内HostCore/application级单worker**，不属于单个Agent turn或HostSession。worker API、claim protocol与wake-up接口保持transport-neutral seam，使后续可迁移到独立进程，而不改outbox/vector sync语义。

### 决策 3：governance何时自动运行

推荐：独立coordinator在turn safe-point后接收wake-up；不要塞进memory hook导致run finalization耦合。coordinator只在存在pending candidate时运行，并实施debounce/batch/session rate limit，例如距上次运行至少N秒、合并短时间内多个turn的候选、每session限制并发与频率。具体N是配置/调参项，但“有pending才跑 + debounce + batch”是v1固定行为，避免每turn触发一次Flash调用。

### 决策 4：fingerprint schema边界与ANN隔离时机

推荐：v1只pin一个active fingerprint。schema PK、trace与query API必须携带fingerprint，以允许未来隔离；但partial index、partition或iterative scan的具体机制延后到第一次model swap/shadow rollout时再依据数据规模和eval决定。不要在只有一个模型时提前做speculative ANN tuning。

### 决策 5：retrieval资源如何关闭

推荐：统一async lifecycle owner；HostCore/application提供async close路径。close必须幂等、资源exactly-once关闭，drain/cancel必须有有界超时，不能让in-flight embedding永久挂住shutdown。关闭顺序为：

```text
stop accepting recall
  -> cancel/drain retrieval tasks
  -> stop vector worker
  -> close rerank/embedding clients
  -> close terminal/runtime resources
```

### 决策 6：same-batch relatedness 的 v1 边界

这不是“governance何时运行”的子问题。当前所有 `_candidate_snapshot()` 都在任何 decision apply 之前构造；因此只读 `memory_nodes` 同步面仍看不到同 batch sibling，笼统加入“staged docs”也不代表调用时序已经成立。

同时，整批candidate snapshots已经在同一个planner input中，且`merge_and_submit`能引用多个candidate entry id。因此v1无需为candidate-level dedupe/merge另造staged relatedness层；缺口只剩新siblings之间的canonical lifecycle edge。

v1决策：

- candidate-level：保留whole-batch input + `merge_and_submit`，同batch去重/合并必须工作。
- canonical lifecycle-level：选择deferred gap，不创建provisional id，不建立新siblings之间的即时supersede/contradiction edge。
- observability：记录deferred次数与candidate refs；不能声称下一batch必然补回。
- escalation：只有数据证明该gap重要时，才新增maintenance/reconciliation；若产品要求即时edge，采用two-phase plan/apply。

### 决策 7：`CHEAP_AUTO` 是否默认启用远程 dense channel

决定：v1 `CHEAP_AUTO`默认启用vector/dense，和`EXPLICIT_SEARCH`共享semantic recall能力。auto路径通过严格deadline、小top-k、run-local query-embedding cache和provider失败后的sparse fallback控制成本与延迟；cache miss仍允许远程embedding，不把dense降成opt-in。

这条策略必须由trigger policy表达，不能藏在provider factory中；trace要记录本次dense是`cache_hit`、`remote_call`、`timeout`还是`degraded`。若真实p95无法满足automatic budget，再基于数据调整，而不是在v1预先关闭vector。

---

## 13. 最终落点总览

```text
PulsaraSettings.retrieval
  -> build_*_provider
  -> RetrievalRuntimeResources (owner + aclose)
       ├─ MemoryVectorIndexWorker
       ├─ DenseCandidateService
       └─ RecallRerankService

Postgres canonical mutation
  -> unified outbox
  -> vector worker claim/embed/finalize
  -> memory_vector_index

Agent main loop
  -> DurableMemoryHooks.project
  -> HybridMemoryRecallService(CHEAP_AUTO)
  -> system prompt projection

Agent tool loop
  -> Async MemorySearchTool
  -> HybridMemoryRecallService(EXPLICIT_SEARCH)
  -> traced tool payload

Governance coordinator
  -> SemanticRelatednessService
  -> MemoryGovernanceEngine
  -> MemoryGovernanceExecutor
  -> canonical commit
  -> wake index worker
```

最小正确路径不是“先把 vector channel 塞进 `LexicalMemoryRecallService`”，而是：

1. 先让 runtime能安全拥有、调用和关闭async retrieval资源。
2. 再让unified outbox安全驱动vector projection。
3. 最后用`HybridMemoryRecallService`替换外层入口。

这样既不破坏现有memory contract，也不会把provider的event-loop、worker和shutdown债务埋进召回核心。
