# Runtime Event Publishing / Hooks Contract

_Created: 2026-07-04_

本文档冻结 Pulsara runtime 内部“事件已写入后如何发布给观察者 / hooks / 投影”的契约。它位于 event log truth 与 memory/runtime projection hooks 之间。

相关代码：

- [src/pulsara_agent/runtime/session.py](/Users/plumliu/Desktop/python_workspace/pulsara_agent/src/pulsara_agent/runtime/session.py)
- [src/pulsara_agent/runtime/publisher.py](/Users/plumliu/Desktop/python_workspace/pulsara_agent/src/pulsara_agent/runtime/publisher.py)
- [src/pulsara_agent/runtime/hooks.py](/Users/plumliu/Desktop/python_workspace/pulsara_agent/src/pulsara_agent/runtime/hooks.py)
- [src/pulsara_agent/runtime/tool_loop.py](/Users/plumliu/Desktop/python_workspace/pulsara_agent/src/pulsara_agent/runtime/tool_loop.py)
- [src/pulsara_agent/runtime/loop_helpers.py](/Users/plumliu/Desktop/python_workspace/pulsara_agent/src/pulsara_agent/runtime/loop_helpers.py)
- [tests/test_runtime_publisher.py](/Users/plumliu/Desktop/python_workspace/pulsara_agent/tests/test_runtime_publisher.py)
- [tests/test_runtime_hooks.py](/Users/plumliu/Desktop/python_workspace/pulsara_agent/tests/test_runtime_hooks.py)
- [tests/test_runtime_session.py](/Users/plumliu/Desktop/python_workspace/pulsara_agent/tests/test_runtime_session.py)
- [tests/test_agent_runtime_loop.py](/Users/plumliu/Desktop/python_workspace/pulsara_agent/tests/test_agent_runtime_loop.py)

相关契约：

- [EVENT_LOG_STORAGE_CONTRACT.zh.md](/Users/plumliu/Desktop/python_workspace/pulsara_agent/contracts/EVENT_LOG_STORAGE_CONTRACT.zh.md)
- [AGENT_RUNTIME_LOOP_CONTRACT.zh.md](/Users/plumliu/Desktop/python_workspace/pulsara_agent/contracts/AGENT_RUNTIME_LOOP_CONTRACT.zh.md)
- [RUNTIME_SEMANTIC_GRAPH_CONTRACT.zh.md](/Users/plumliu/Desktop/python_workspace/pulsara_agent/contracts/RUNTIME_SEMANTIC_GRAPH_CONTRACT.zh.md)
- [ARTIFACT_STORE_CONTRACT.zh.md](/Users/plumliu/Desktop/python_workspace/pulsara_agent/contracts/ARTIFACT_STORE_CONTRACT.zh.md)

---

## 1. 核心立场

Runtime event publishing 是 in-process post-commit bus。

它不是：

- event log truth；
- durable message queue；
- cross-process pub/sub；
- permission gate；
- memory governance coordinator；
- retry worker。

它的职责是：

1. `RuntimeSession.write_event(s)` 通过同一个 `SessionWriteCoordinator` conditional commit canonical batch；
2. 每个 registered committed reducer 先补齐自己的 sequence gap，再 apply current committed batch；
3. publisher 独立补齐自己的 gap，并按 canonical `event.sequence` enqueue；
4. 临界区外等待 live subscriber delivery；
5. 把 observer failure 与 durable commit / reducer truth 分开报告。

---

## 2. RuntimeSession committed writer boundary

`RuntimeSession.write_event(s)(..., expected_last_sequence=..., state=...)` 是 canonical 写入口：

- 所有 input event 的 `sequence` 必须是 `None`；
- async 与 thread writer 共享一个 session-owned、thread-safe serialization boundary；
- `EventLog.extend(..., expected_last_sequence=...)` 负责 conditional atomic commit；
- committed reducer 的 high-water 与 publisher high-water 相互独立；
- reducer 必须先 apply 完整 missing interval，再 apply current batch；
- publisher 必须先 enqueue missing committed events，再 enqueue current batch；
- observer error 不回滚 durable event，不允许 caller 重复生成同一 semantic fact；
- 返回 `EventWriteResult`，分别表达 commit、reducer high-water/reconciliation、publication status/errors。

`write_events_from_thread()` 使用同一 coordinator 与 reducer path，但不得等待 observer；只能报告 `enqueued` 或 `unavailable`。

`emit/emit_many/emit_from_thread` 是 compatibility wrapper：

- `emit_many()` 必须一次调用 `write_events()`，不能循环逐条 emit；
- compatibility wrapper 可在 publication error 后抛 `EventPublicationAfterCommitError`，异常必须携带已经 committed 的 `EventWriteResult`；
- Subagent 等新 command path 必须消费 `write_*` result，不把 observer error误判成 commit failure。

Critical publication-aware owner，包括 MCP input-required lifecycle、mandatory runtime
audit 与 compaction core，禁止调用上述 compatibility wrapper。它们必须在第一次 admission
冻结一个 ordinary deadline 与 terminal-maintenance tail，并调用
`write_event(s)_with_deadline()`；所有 `NONE` retry、confirmation、account/reducer 与
publication wait复用同一 absolute deadline，不能在循环中续期。

critical event durable FULL 后若 publication 为 `unavailable` 或
`failed_after_commit`，RuntimeSession 安装
`publication_reconciliation_required` latch。V1 不声称 live catch-up；ordinary mutation
立即 fail closed。只有 RuntimeSession 唯一 issuer 签发的 borrower-scoped
`PublicationTerminalMaintenanceLease` 可以提交预先绑定的 exact ordered terminal batch。
Lease 验证 owner kind、latch generation、handle对象身份、attempt generation、candidate
IDs/payload fingerprints与 transaction companion；`NONE` 回到新 generation 的 ISSUED，
`FULL` 才 CONSUMED，`PARTIAL/UNKNOWN` 进入 reconciliation。

`NONE` 重试必须使用确定性、有上限且受原 absolute deadline裁剪的退避；禁止
`sleep(0)` 忙循环。Session-owned mandatory audit owner在 shared task终结后必须移除
candidate/task owner，或只保留显式有界的receipt cache，不得让已完成attempt随session增长。

携带 `PublicationLatchedRunTerminationFact` 的 RunEnd在precommit阶段必须按reason把每个
source ref exact rebind到同一runtime ledger中的真实stored event，并继续验证domain matrix：
compaction的Started/terminal pairing、MCP suspension/resolution/disposition/closure/
ToolResult identity chain，以及mandatory audit的唯一typed source。仅重算caller提供的
accumulator不构成authority证明。

Online compaction Started/Completed/Failed 的唯一 publication owner也是 RuntimeSession。
Host listener只是 process-local observer；Host与 mid-turn compactor不得扫描 committed
sequence、调用 `publish_stored_events()` 或把 listener 当第二个 publisher。

registered committed reducer failure 不是 observer failure：event仍已 commit，但 session进入 `reconciliation_required`，后续 mutation fail closed；safe point必须从完整 EventLog rebuild，成功后才能恢复写入。

Typed failure audit只证明 durable failure fact，不等于 durable hook/projection retry job。
Publisher仍是 in-process bus；cross-process durable hook/outbox属于独立开放债务。

`publish_stored_event(event, state=...)` 契约：

- 只接受已经有 canonical sequence 的 stored event。
- 用于其它事务已提交的 event bridge；它必须先 catch up registered reducers，再 catch up publisher，不能制造 sequence gap；
- durable-only/offline repair 不允许写入 active session 后不更新 reducer/publisher high-water。

---

## 3. RuntimeEventPublisher

`RuntimeEventPublisher` 是单 runtime session 的有序发布器。

初始化：

- `runtime_session_id` 必填。
- `next_sequence_to_publish >= 1`。
- resume / reopen 时可以用 event log 的 `next_sequence()` 初始化，从历史末尾继续发布。

loop binding：

- 第一次 `publish()` 绑定当前 asyncio loop 与 loop thread id。
- 绑定后若在另一个 loop 上调用 `publish()`，必须报错。
- `publish_from_thread()` 可以从非 loop 线程调用，通过 `call_soon_threadsafe` 入队。

顺序：

- publisher 必须按 canonical sequence 投递，不按 arrival order。
- thread events 即使先到，也必须等待缺失的更小 sequence。
- `_pending_by_sequence` 保存已到但还不能发布的事件。
- `_next_sequence_to_publish` 是唯一发布游标。
- `enqueue_committed_batch()` 接收 canonical contiguous/catch-up batch；writer 在 session coordinator 内调用，observer await 在锁外发生。

subscriber：

- `subscribe()` 幂等；重复 subscriber 不重复加入。
- `unsubscribe()` 移除 subscriber。
- delivery 时必须对 subscriber 列表做 snapshot，避免迭代时被修改。

错误：

- subscriber 抛错时，publisher 记录到 `errors`。
- 同一 event 的其它 subscriber 仍必须收到该 event。
- 对 `publish()` 调用方，若任一 subscriber 抛错，delivery future 必须 set_exception。
- 对 `publish_from_thread()`，不能阻塞调用线程；错误只进入 `publisher.errors`。

mailbox：

- mailbox 收到 item 时必须确保 drain task 存在。
- 如果 drain task 在 mailbox 为空附近退出，同时新 item 到达，必须重新调度 drain task，不能遗留未发布 item。

---

## 4. discard_unpublished

`discard_unpublished(published)` 用于处理“event 已落盘但无法发布”的场景。

规则：

- event 必须有 canonical sequence。
- 若 sequence >= `_next_sequence_to_publish`，发布游标推进到 `sequence + 1`。
- 若 pending buffer 里已有更高 sequence，推进后必须尝试继续 drain。

用途：

- teardown/legacy direct publisher path中明确放弃 live notification；
- manual compact failure 后补发 started/failed events 时的 sequence gap 防线；
- host teardown / close 路径中不能再等待 live subscriber 的场景。

普通 `RuntimeSession.write_events_from_thread()` 不使用 discard 假装发布成功；loop不可用时返回 `publication_status="unavailable"`，durable graph/reducer truth仍必须正确。

禁止：

- 不允许用 discard 跳过还没写入 event log 的 sequence。
- 不允许在普通 active loop 中吞掉 subscriber error 后假装 projection 成功。

---

## 5. RuntimeHookManager

`RuntimeHookManager` 是默认 subscriber。

注册：

- `register_event(event_type | None, handler)`：
  - `None` 表示观察所有 event；
  - 具体 `EventType` 表示只观察该类型。
- `register_block(block_type | None, handler)`：
  - `None` 表示观察所有 completed block；
  - `"text"` 等字符串表示只观察该 block type。

执行：

- 支持 sync handler 与 async handler。
- handler 按注册顺序执行。
- observer hook 返回值只作 diagnostics / ignored value；当前没有 control effect。
- 单个 hook 抛错不得阻止后续 hook。
- hook error 必须记录为 `HookDispatchError`，包含 hook kind、selector、handler name、error type/message、run/turn/reply id、event/block id。

隔离：

- event hook 收到的是 `event.model_copy(deep=True)`。
- 一个 hook 修改 event 不得影响后续 hook，也不得影响 block assembler。
- block hook 收到 completed block projection；不得把它当 canonical event truth。

---

## 6. Block assembly for hooks

`RuntimeHookManager` 内部用 `BlockAssembler` 从 event stream 中识别 completed blocks。

规则：

- completed text/tool-result blocks 才触发 block hooks。
- orphan delta/end event 不触发 block hook。
- 相同 block id 可在不同 reply 中复用；assembler 必须按 reply 隔离。
- `REPLY_END` / `RUN_ERROR` / `EXCEED_MAX_ITERS` 必须清理对应 reply 的未完成 block state。
- cleanup 后迟到的 block end 不得触发 completion。

这条规则保证 run error / interrupted stream 不会把 partial text 或 partial tool result 投影成完整事实。

---

## 7. MemoryHooks 与 ToolResultPersistenceHook

`MemoryHooks` 是 `AgentRuntime` 主循环显式调用的 integration interface，不由 `RuntimeHookManager` 自动调用。

它包含：

- `on_turn_start`
- `baseline_projection`
- `project`
- `after_model_reply`
- `after_tool_results`
- `should_compact`
- `on_turn_end`
- `on_session_end`
- `memory_proposal_sink`

`NoopMemoryHooks` 是无副作用默认实现。

`ToolResultPersistenceHook.after_tool_results(state, results)` 是专门的 tool-result persistence seam，用于 execution evidence ledger 等 runtime semantic projection。

边界：

- Memory hooks 的失败语义由 `AgentRuntime` 契约控制，不由 publisher/hook manager 吞掉。
- Runtime observer hook failure 是 non-fatal diagnostic；MemoryHooks failure 可以导致 run failed。
- 不得把这两类 hook 混为一条错误策略。

---

## 8. Tool-loop helper events

`build_tool_result_error_events(context, tool_call_id, tool_call_name, message, state=ERROR)` 必须生成标准三段 tool result event：

1. `ToolResultStartEvent`
2. `ToolResultTextDeltaEvent`
3. `ToolResultEndEvent`

用途：

- malformed tool arguments；
- duplicate tool call id；
- capability access deny；
- permission deny；
- unknown tool / hidden tool fail-closed；
- workflow/tool suspension cancellation siblings。

这保证错误工具结果也能被 transcript reducer、inspector、recovery、compaction 按普通 tool result 处理。

---

## 9. 禁止事项

- 不允许 subscriber 直接写 canonical event log 表达新的 runtime truth，除非该 subscriber本身通过受控 `RuntimeSession.write_event(s)` 路径。
- 不允许 publisher 按 arrival order 发布跨线程 events。
- 不允许 hook 修改 canonical event object。
- 不允许 observer hook failure 中断后续 observer。
- 不允许把 observer hook return value 当作 permission/control decision。
- 不允许未完成 block 在 `RUN_ERROR` / `EXCEED_MAX_ITERS` 后变成 completed block。
- 不允许 `emit_from_thread()` 因慢 subscriber 阻塞 worker thread。
- 不允许 event 已落盘但 publisher sequence gap 未处理。
- 不允许 current committed batch 在 reducer missing interval 之前 apply。
- 不允许使用 publisher high-water 代替 reducer high-water。
- 不允许 observer failure 触发 semantic command retry。

---

## 10. 测试守卫

最低测试门槛：

- thread events 按 canonical sequence 发布。
- conditional writer conflict 在 insert 前失败，并把 reducer/publisher catch up到 actual high-water。
- reducer gap 先于 current batch apply。
- reducer与publisher从各自 high-water独立 catch up。
- batch observer failure不阻止后续 event/subscriber delivery。
- reducer failure保留commit truth并阻断后续 mutation，rebuild后才能恢复。
- `emit_from_thread()` 保留 `LoopState` 给 subscriber。
- `emit_from_thread()` 不等待慢 subscriber。
- 慢 subscriber 最终仍收到 thread event。
- mailbox/drain race 不遗留 unpublished item。
- subscriber failure 仍继续投递给其它 subscriber，并向 `publish()` 调用方抛错。
- publisher 可从已有历史 sequence 继续。
- event hook 支持 all/specific selector。
- sync/async hook 保持注册顺序。
- hook error non-fatal 且记录 `HookDispatchError`。
- event hook 收到 deep copy。
- block hook 只对 completed blocks 触发。
- orphan events 不触发 block hooks。
- block ids across replies 隔离。
- `RUN_ERROR` / `REPLY_END` 清理未完成 block。
- `build_tool_result_error_events()` 产出标准 tool result event shape。

---

## 11. Model stream 与 control disposition 提交分相

model stream commit port 分为 ledger commit/confirm、同步 reducer fold、锁外 ordered publication。control linearization lock 内只允许 guard
校验、durable confirm、fold 与 permit CAS；禁止 inline 等待 publisher 或 observer callback。ordered publisher 只向 bounded mailbox 入队，
observer failure只产生 operational diagnostic，不能撤销 durable disposition、fold、reservation settlement或process-local permit。

start/semantic/terminal 的 CAS 语义不同：semantic NONE 可用同一 stable bytes重试；terminal必须在 latest rollout account state上重做 guard，
但不得改变 event IDs/payload；PARTIAL/UNKNOWN 保留 owner并 latch。publication waiter cancellation只 detach，commit worker与physical operation
继续完成。observer回调触发 stop/close不得 self-join或在 control lock 内重入。
