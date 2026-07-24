# Runtime Semantic Graph / Timeline Persistence Contract

_Created: 2026-07-04_

本文档定义 Pulsara runtime semantic graph 的写入、物化、查询与防回写边界。它补充 [MEMORY_SURFACES_CONTRACT.zh.md](/Users/plumliu/Desktop/python_workspace/pulsara_agent/contracts/MEMORY_SURFACES_CONTRACT.zh.md) 中的高层 surface 规则：governed memory 与 runtime semantic graph 共享部分 graph substrate，但不是同一种 authority。

相关代码：

- [src/pulsara_agent/runtime/timeline.py](/Users/plumliu/Desktop/python_workspace/pulsara_agent/src/pulsara_agent/runtime/timeline.py)
- [src/pulsara_agent/memory/hooks/run_timeline_persistence.py](/Users/plumliu/Desktop/python_workspace/pulsara_agent/src/pulsara_agent/memory/hooks/run_timeline_persistence.py)
- [src/pulsara_agent/memory/hooks/runtime_persistence.py](/Users/plumliu/Desktop/python_workspace/pulsara_agent/src/pulsara_agent/memory/hooks/runtime_persistence.py)
- [src/pulsara_agent/memory/canonical/ledger.py](/Users/plumliu/Desktop/python_workspace/pulsara_agent/src/pulsara_agent/memory/canonical/ledger.py)
- [src/pulsara_agent/memory/working_context.py](/Users/plumliu/Desktop/python_workspace/pulsara_agent/src/pulsara_agent/memory/working_context.py)
- [src/pulsara_agent/memory/foundation/run_timeline_query.py](/Users/plumliu/Desktop/python_workspace/pulsara_agent/src/pulsara_agent/memory/foundation/run_timeline_query.py)
- [src/pulsara_agent/entities/runtime/](/Users/plumliu/Desktop/python_workspace/pulsara_agent/src/pulsara_agent/entities/runtime)
- [src/pulsara_agent/ontology/runtime.py](/Users/plumliu/Desktop/python_workspace/pulsara_agent/src/pulsara_agent/ontology/runtime.py)
- [tests/test_runtime_timeline.py](/Users/plumliu/Desktop/python_workspace/pulsara_agent/tests/test_runtime_timeline.py)
- [tests/test_execution_evidence_ledger.py](/Users/plumliu/Desktop/python_workspace/pulsara_agent/tests/test_execution_evidence_ledger.py)
- [tests/test_working_context.py](/Users/plumliu/Desktop/python_workspace/pulsara_agent/tests/test_working_context.py)

---

## 1. 核心立场

Runtime semantic graph 是 runtime event log 的语义投影，不是 governed canonical memory。

它允许表达：

- run timeline；
- tool execution evidence；
- artifact provenance；
- turn / tool-result / evidence runtime entities；
- working-context 的 recent-activity 投影来源。

它不允许表达：

- 新的长期 `Claim` / `Preference` / `Observation` / `Decision` / `ActionBoundary` governed memory；
- supersede / contradiction lifecycle decision；
- “模型刚说过，所以写入长期记忆”的 shortcut。

一句话：

```text
event log 是 runtime truth
runtime semantic graph 是 runtime truth 的语义投影
governed memory 是 governance 决策后的长期事实
```

这三者不得互相冒充。

---

## 2. Authority 与写入口

### 2.1 Runtime event log 是事实源

Runtime semantic projection 的事实源必须是 typed `AgentEvent` 与已归档 artifact。

允许输入：

- `AgentEvent` 序列；
- persisted event span；
- `ToolResultBlock`；
- tool-result artifact refs；
- archive metadata。

禁止输入：

- model free text 直接作为 runtime semantic truth；
- inspector 输出回写；
- recall projection 回写；
- working_context projection 回写。

### 2.2 允许的写入口

当前允许写 runtime semantic graph 的入口：

- `RunTimelinePersistenceHook`
  - 从 event log 构造 `RunTimeline`；
  - 归档 timeline JSON artifact；
  - 写 `RunTimelineRecord` JSON-LD；
  - 可追加 `runtime_semantic` outbox payload。
- `ExecutionEvidencePersistenceHook`
  - 在 tool result safe point 调用 `ExecutionEvidenceLedger.record_tool_result_block(...)`。
- `ExecutionEvidenceLedger`
  - 写 `ToolResult`、`Artifact`、`Evidence`、`Turn` 等 runtime entities；
  - 可把 runtime semantic document 镜像到 mutation outbox。

新增写入口必须满足：

- 输入来自 persisted runtime fact；
- 不创建 governed memory node；
- 不绕过 artifact service 重复归档大输出；
- 不依赖 Oxigraph-only truth；
- 有 deterministic tests 覆盖。

---

## 3. Run timeline projection

### 3.1 构造规则

`build_run_timeline(...)` 必须只从同一 run 的 ordered events 构造 timeline。

它可以总结：

- reply lifecycle；
- model call usage；
- assistant text / thinking；
- tool call / tool result；
- permission request；
- plan mode / plan question / plan exit；
- error / abort / exceeded max iterations。

它不得：

- 重新解释模型意图；
- 执行 memory governance；
- 写 working_context；
- 写 event log。

Host run 的 durable timeline 必须能从 `RunStartEvent` 的 typed host/child entry branch、initial
`CapabilityExposureResolvedEvent`、零个或多个 `RunInteractionResumeBoundaryEvent`，以及 matching stable-ID
`RunEndEvent` 重建。Live `RunWorkingSet`、boundary task、MCP manager、lease 或 Python object identity 不进入
semantic graph truth。

### 3.2 持久化规则

`RunTimelinePersistenceHook` 只在 terminal-ish events 上持久化 snapshot：

- `REPLY_END`
- `RUN_ERROR`
- `EXCEED_MAX_ITERS`
- `RunEndEvent`

持久化必须：

- 用 `event_store.iter(run_id=...)` 重新读取 persisted events；
- 把完整 timeline JSON 归档为 `artifact_kind=run_timeline`；
- 写 `RunTimelineRecord(stored_as=<artifact ref>)`；
- 保留同一个 timeline id 的 `created_at`，更新 `updated_at`；
- 若配置 outbox，则追加 `runtime_semantic` lane。

### 3.3 Snapshot 可被更新

同一个 run 可能触发多次 timeline persistence，例如先 `REPLY_END` 后 `RunEndEvent`。

规则：

- timeline node id 稳定：`run-timeline:{runtime_session_id}:{run_id}`；
- 后写 snapshot 可以覆盖 JSON-LD document；
- `created_at` 必须保持首次写入值；
- artifact blob id 可随 event sequence 变化；
- read side 应读取最新 `stored_as` artifact。

---

## 4. Execution evidence ledger

### 4.1 定位

`ExecutionEvidenceLedger` 是 runtime fact ledger，不是 governed memory writer。

它可以记录：

- tool result；
- tool-result-backed evidence；
- artifact provenance；
- turn produced relation；
- runtime semantic JSON-LD document。

它不得：

- 通过 tool result 自动写 governed memory；
- 绕过 `MemoryGovernanceExecutor` 写长期 memory；
- 对大输出重新归档第二份 artifact；
- 把 recall projection 写回 evidence。

### 4.2 大输出归档单一 authority

大工具输出归档的唯一入口是 `ToolResultArtifactService`。

因此：

- `record_tool_result()` 只接受小输出；
- 大输出必须先经过 executor artifact pipeline，形成 `ToolResultBlock.artifacts`；
- ledger 只能引用 artifact refs；
- ledger 不得再次把同一大输出写入 archive。

### 4.3 Event span provenance

ledger 写出的 runtime semantic node 应带 `RuntimeEventSpan`，当 span 可得时至少包含：

- runtime session id；
- run id；
- turn id；
- reply id；
- sequence range。

从 persisted event slice 重建 tool result 时，必须按 sequence range 过滤并使用 `completed_tool_result_from_events(...)` 一类的 event-derived reconstruction。

---

## 5. Runtime semantic outbox lane

Runtime semantic materialization 使用同一个 canonical mutation outbox，但使用独立 lane：

```text
mutation_lane = "runtime_semantic"
```

默认 async surface：

```text
("oxigraph",)
```

规则：

- `dirty_memory_ids` 必须为空；
- payload documents 是 runtime semantic JSON-LD documents；
- payload 必须携带 source runtime ids；
- 若 document 由 artifact 派生，必须携带 `source_artifact_ids`；
- runtime semantic lane 不写 `search_index` / `vector_index`，除非未来新增明确契约。

`runtime_semantic` 使用 outbox 是为了统一 async materialization 与 diagnostics，不表示它是 governed memory mutation。

---

## 6. Working context

Working context 是 recent activity cache，不是 memory。

规则：

- 存储表是 `working_context_summaries`；
- key 是 `memory_domain_id`，当前最新 summary 覆盖旧 summary；
- summary 来源是 run timeline summary；
- update 需要通过低信号过滤；
- 支持 TTL；
- 注入上下文时必须带：
  - `projection_kind="working_context"`
  - `do_not_write_back=True`
  - `<working-context-projection do_not_write_back="true" authority="recent_activity">`

Working context 不得：

- 写 candidate pool；
- 写 governed memory；
- 写 event log；
- 作为 recall result id；
- 作为 lifecycle target。

---

## 7. Read side

Runtime semantic read side 包括：

- `load_run_timeline(...)`
- `summarize_run_timeline(...)`
- inspector timeline projection；
- working_context projection；
- Oxigraph read-side smoke tests。

Read side 不得把缺失 runtime semantic projection 当成 run 不存在。权威 run truth 仍然是 event log。

因此：

- timeline artifact 缺失应报告 diagnostic；
- working_context 缺失应表现为无 projection，而不是 error；
- Oxigraph materialization lag 不得阻塞 inspector 的 event-log-based timeline。

---

## 8. 禁止事项

- 不允许 runtime semantic writer 创建 governed memory node。
- 不允许 runtime semantic outbox payload 填 `dirty_memory_ids`。
- 不允许 working_context 回写 candidate pool。
- 不允许 ledger 对大 tool output 重新归档。
- 不允许 read side 把 Oxigraph 当作 runtime truth。
- 不允许 timeline persistence 写入或修改 event log。
- 不允许 timeline summary 参与 memory governance destructive lifecycle。

---

## 9. 测试守护

最低测试门槛：

- `build_run_timeline(...)` 汇总 model/tool/permission/plan/error events。
- pending permission / plan interaction 在 timeline 中保持 waiting 状态。
- confirm / plan resolution 能清除 waiting 状态。
- timeline persistence 归档 JSON artifact 并写 `RunTimelineRecord`。
- timeline snapshot update 保留 `created_at`。
- read side 能加载 timeline artifact 并生成 summary。
- runtime semantic outbox payload 默认为 Oxigraph-only surface。
- durable wiring 在 run end replay runtime semantic outbox。
- `ExecutionEvidenceLedger` 记录 small tool result。
- 大输出必须通过 artifact refs 路径记录。
- working_context 低信号 run 不更新。
- working_context summary upsert 按 domain latest 覆盖。
- working_context projection 带 `do_not_write_back` 且不生成 memory ids。

---

## 10. Subagent graph checkpoint memoization

Subagent graph 的唯一 authority 仍是 canonical EventLog 和 RunStart 冻结的 versioned reducer contract。Checkpoint 只是
`fold(events[1:k])` 的可丢弃 memoization，不是第二真源。Durable contract 必须精确绑定 reducer ID、version、contract fingerprint、
supported graph event schema/domain entries 和 canonical state codec。Graph-domain event 不在 RunStart contract 中时 fail closed；明确声明的
non-graph/checkpoint event 只推进 ledger continuity。

Production selection/replay 只能从 compatible checkpoint 加 bounded contiguous delta 恢复；新 session 只允许 bounded bootstrap。已有
session 没有 bounded path 时 fail closed，不得隐式回退 sequence-1 full fold。无界 full fold 只属于 closed/quiescent session 上的
privileged offline doctor。

`SubagentGraphSemanticSourceFact` 只表达 graph semantic source；`SubagentGraphAccelerationFact` 只表达 checkpoint、delta、
ledger high-water 与 rebase 归因。Checkpoint ID、materialization event、delta range/count、physical sequence 不得进入 selection、snapshot、
candidate 或 provider payload 的 semantic fingerprint。不同 checkpoint schedule 恢复出相同 graph 时 semantic source 必须相同。

---

## 9. Governance transcript authority boundary

Memory governance source authority属于 transcript projection domain，不属于 runtime semantic
graph，也不从 raw model-stream segment、working-context summary或 live scratchpad派生。
`TranscriptProjectionStateStore.capture_governance_authority_snapshot()`必须在同一 reducer
锁域内冻结 ledger through-sequence、accepted model projections/dispositions、stable user
entries、tool call/pair/result state与snapshot fingerprint。

所有 exact/sparse event reads只能读取到该 snapshot high-water。不得先读取 EventLog high-water，
再把稍后取得的 reducer state贴到旧 H；也不得用 H 之后的 event回答本次 batch。

Model stream segments继续属于 `non_transcript` ledger continuity。Governance只消费 accepted
terminal projection及其 typed disposition/pairing结果；segment数量、seal schedule和transaction
batching变化只能影响 physical attribution，不能改变 governance evidence semantic fingerprint。

Authority snapshot与 exact referenced envelopes冲突时，runtime必须安装 reconciliation latch；
candidate自身引用不存在的 source call、或 source run terminal后仍缺合法 pairing，才是可确定性
终结的 candidate provenance invalid。两类失败不得合并为普通 governance skip。

---

## 11. Typed runtime audit projection boundary

新的 MCP lifecycle、compaction request/skip与 tool-result evidence projection failure events
是 canonical EventLog audit facts，不自动成为 governed memory node或 runtime semantic graph
entity。它们可以进入 timeline/Inspector的 bounded typed projection，但不得由 generic hook
把 payload字典直接写进 Oxigraph。

Compaction memory proposal继续使用独立
`ContextCompactionMemoryCandidatesProposedEvent + memory_candidate_projection_outbox`
authority。Typed failure event不等于 durable projection retry job；D3 hook/outbox与 D5
compaction-memory crash-to-durable-owner仍是独立开放债务。
