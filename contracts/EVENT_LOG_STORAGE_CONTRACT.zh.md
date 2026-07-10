# Event Log / Runtime Storage Contract

_Created: 2026-07-04_

本文档定义 Pulsara runtime event log 与 Postgres runtime truth storage 的长期契约。它描述“事实如何落盘”，不描述 model/tool loop 的执行顺序；执行顺序见 [AGENT_RUNTIME_LOOP_CONTRACT.zh.md](/Users/plumliu/Desktop/python_workspace/pulsara_agent/contracts/AGENT_RUNTIME_LOOP_CONTRACT.zh.md)。

相关代码：

- [src/pulsara_agent/event/events.py](/Users/plumliu/Desktop/python_workspace/pulsara_agent/src/pulsara_agent/event/events.py)
- [src/pulsara_agent/event_log/protocol.py](/Users/plumliu/Desktop/python_workspace/pulsara_agent/src/pulsara_agent/event_log/protocol.py)
- [src/pulsara_agent/event_log/postgres.py](/Users/plumliu/Desktop/python_workspace/pulsara_agent/src/pulsara_agent/event_log/postgres.py)
- [src/pulsara_agent/event_log/serialization.py](/Users/plumliu/Desktop/python_workspace/pulsara_agent/src/pulsara_agent/event_log/serialization.py)
- [src/pulsara_agent/storage/postgres_schema.py](/Users/plumliu/Desktop/python_workspace/pulsara_agent/src/pulsara_agent/storage/postgres_schema.py)
- [tests/test_event_message_system.py](/Users/plumliu/Desktop/python_workspace/pulsara_agent/tests/test_event_message_system.py)
- [tests/test_inspector.py](/Users/plumliu/Desktop/python_workspace/pulsara_agent/tests/test_inspector.py)

---

## 1. 核心立场

Runtime truth 是 append-only typed event log。

Canonical facts：

- `sessions`
- `runs`
- `turns`
- `agent_events`
- artifacts / tool result artifact index

`runs` / `turns` / inspector projections 是索引和摘要，不是 transcript 真源。任何需要解释 agent 历史的路径必须能从 `agent_events.payload` 重建。

---

## 2. AgentEvent typed payload

所有 runtime event 必须是 `AgentEvent` union 中的 typed Pydantic model。

序列化规则：

- 写入时使用 `dump_agent_event(event)`；
- 读取时使用 `load_agent_event(payload)`；
- payload 必须包含 `type`；
- unknown type 必须失败，而不是静默降级为 `CUSTOM`。

`CUSTOM` 只用于轻量 diagnostic / compatibility 事件。已经契约化的业务 boundary（如 context compaction、plan mode、tool result、run end）必须使用 typed event。

---

## 3. Sequence

每个 runtime session 内 event sequence 必须连续、单调递增。

Postgres 约束：

- `agent_events.sequence` 非空；
- `(session_id, sequence)` 唯一；
- `PostgresEventLog.extend()` 在一个事务内为 batch 分配 sequence；
- 同一 runtime session 的 append 必须使用 advisory transaction lock。

`event.sequence=None` 表示事件尚未 canonicalized。写入后返回的 event 必须带 canonical sequence。

live `append/extend` 只接受 `sequence=None` 的 event。带 sequence 的 input 必须失败；未来若需要 repair/import，必须提供独立的 offline authority，且不得复用 live EventLog API。

`append/extend` 同时冻结 conditional atomic contract：

- 可选 `expected_last_sequence` 必须在 session advisory transaction lock 内比较；
- mismatch 在任何 insert 前抛 `EventLogWriteConflict`，当前 batch 零写入；
- batch 先完整 materialize / validate，event id 在 batch 内唯一；session 内一个event id只能命名一个immutable payload；
- 一个 batch 获得连续 sequence，不能与其它 `append/extend` 交错；
- validation、serialization、parent-row、insert 或 projection sync 失败时整批 rollback；
- 空 batch 是 no-op。

单事件`append()`同时是精确幂等的commit-confirmation入口：若同一session中已有相同id，且除canonical `sequence`外的完整typed payload一致，返回已存储event，不再分配新sequence；同id不同payload抛`EventIdConflict`。该确认必须先于`expected_last_sequence`冲突判断，使“commit成功但ack丢失”的重试可以确认原事实。`get_by_id()`提供只读确认能力。

`extend()`的atomic batch不能把幂等语义扩成“部分复用、部分新增”。`confirm_batch()`必须在同一个session lock/transaction内返回候选的确认结果与当时的canonical high-water。Runtime writer在不确定batch commit后使用该结果：整批全部存在、payload一致、sequence按原顺序连续时，视为已commit，并至少catch up reducers/publisher到`max(confirmed_batch_end, confirmation_high_water, conditional_conflict.actual_last_sequence)`；只确认到部分batch或sequence不连续时必须latch `ledger_reconciliation_required`并阻止后续mutation。同id payload冲突保持稳定`EventIdConflict`类型，不能降格成普通commit error或吞成成功。

`ledger_reconciliation_required`与单个committed reducer的`reconciliation_required`是两种不同故障。普通reducer rebuild只能清自己的process-local drift，绝不能解除ledger atomicity/sequence结构异常；ledger latch只能通过专门的ledger verify/repair authority解除，V1未提供live repair时必须关闭并重新打开session。

PostgreSQL 是 production EventLog 真源和上述事务/CAS/跨进程语义的权威实现。`InMemoryEventLog` 仅是 pytest test double，只需复现会影响 reducer/writer 单测结论的最小单进程 contract；它不是 product backend，也不能替代 PostgreSQL transaction/restart 验收。

---

## 4. Parent rows

写 event 前必须确保 parent rows 存在：

- session row；
- run row；
- turn row。

边界规则：

- run id 已属于另一个 runtime session 时必须失败。
- turn id 已属于另一个 session/run 时必须失败。
- 不允许一个 event 写入导致跨 session/run 混线。

---

## 5. Run projection

`runs` 表是从 canonical events 同步出来的 summary。

同步规则：

- `RUN_START` 将 run 标为 `running`，清空 terminal fields。
- `RUN_END` 将 status / stop_reason / completed_at 写到 `runs`。
- `repair_run_projection()` 可以从 event log 重建 projection。

禁止只改 `runs` 来表达 run 结束、abort 或 recovery。必须先写 canonical `RUN_END`，再同步 projection。

---

## 6. Iteration / replay

`EventLog.iter()` 必须：

- 默认返回当前 runtime session 全部 events；
- 支持按 run / turn / reply 过滤；
- 支持 `after_sequence`；
- 按 sequence 升序返回。

`EventLog.replay(reply_id)` 只重放一个 assistant reply，对应 `ReplyStartEvent` 到 completed content blocks。完整 prior transcript 必须走 transcript reducer 契约，而不是直接拼接所有 reply replay。

---

## 7. Artifacts

Runtime artifact storage 是 event log 的旁路 payload store。

规则：

- event 保存 artifact refs；
- artifact 表保存 text/binary body、digest、size、metadata；
- `tool_result_artifacts` 是 tool result 与 artifact 的可查询索引；
- tool result artifact ref 缺失真实 artifact 是 inspector error；
- context/model replay 不应内联巨大 artifact body，只能用 preview + read_more。

---

## 8. Schema ownership

`RUNTIME_TRUTH_SCHEMA_SQL` 是 runtime truth schema 的创建入口。

它拥有：

- sessions；
- runs；
- turns；
- agent_events；
- artifacts；
- tool_result_artifacts；
- tool_execution_records；
- working_context_summaries。

Memory canonical substrate schema 不属于本契约；见 [MEMORY_SURFACES_CONTRACT.zh.md](/Users/plumliu/Desktop/python_workspace/pulsara_agent/contracts/MEMORY_SURFACES_CONTRACT.zh.md)。

---

## 9. 禁止事项

- 不允许 JSONL transcript 成为生产 truth。
- 不允许 unknown event type 静默通过。
- 不允许跳过 session advisory lock 并发写同一 runtime session。
- 不允许 live caller 提交预编号 event。
- 不允许把 conditional write conflict 当普通异常后盲目重试同一批 events。
- 不允许把 `runs` projection 当 canonical transcript truth。
- 不允许直接更新 projection 表来代替 typed event。
- 不允许跨 session 复用 run/turn id。
- 不允许 inspector 或 resume 读取 live scratchpad 来解释历史。

---

## 10. 测试守护

最低测试门槛：

- typed event dump/load roundtrip。
- unknown event type fails。
- Postgres append assigns contiguous sequence。
- batch extend sequence contiguous。
- conditional expected sequence mismatch produces zero writes。
- concurrent PostgreSQL batches do not interleave。
- PostgreSQL batch failure rolls back every event and projection mutation。
- pytest fake passes the bounded shared CAS/atomic batch contract；数据库事务和恢复只由 PostgreSQL tests 证明。
- run/turn parent rows are created。
- run id cannot belong to two sessions。
- `RUN_START` / `RUN_END` sync run projection。
- projection repair rebuilds stale/missing run status from events。
- `iter()` filters by run/turn/reply/after_sequence。
- `replay(reply_id)` reconstructs assistant message blocks and usage。
- inspector detects sequence gaps and stale run projection。
