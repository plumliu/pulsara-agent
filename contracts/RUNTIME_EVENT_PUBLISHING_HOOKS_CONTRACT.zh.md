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

1. 在 `RuntimeSession.emit*()` 已经把 event 写入 canonical event log 后；
2. 按 canonical `event.sequence` 顺序；
3. 把 `RuntimePublishedEvent(runtime_session_id, event, state)` 投递给 subscriber；
4. 让 `RuntimeHookManager` 等观察者做 derived projection / diagnostics。

---

## 2. RuntimeSession emit boundary

`RuntimeSession.emit(event, state=...)` 契约：

- 输入 event 的 `sequence` 必须是 `None`。
- `EventLog.append()` 分配 canonical sequence。
- append 成功后调用 `RuntimeEventPublisher.publish(...)`。
- `emit()` 等待 publish delivery 完成或 subscriber error 抛出。
- 返回带 canonical sequence 的 stored event。

`RuntimeSession.emit_many(events, state=...)` 契约：

- 逐个调用 `emit()`。
- 保持输入顺序。
- 若中途失败，已写入的 event 仍是 canonical truth；调用方必须按 runtime finalization/recovery 契约处理。

`RuntimeSession.emit_from_thread(event, state=...)` 契约：

- 输入 event 的 `sequence` 必须是 `None`。
- 先 append 到 event log 获取 canonical sequence。
- 尝试通过 `publisher.publish_from_thread()` 非阻塞投递。
- 若 publisher 尚未绑定 event loop 或 loop 已关闭，必须 `discard_unpublished(stored)`，推进 publisher sequence，避免后续 publish 因缺口永久卡住。
- 不等待慢 subscriber。

`publish_stored_event(event, state=...)` 契约：

- 只接受已经有 canonical sequence 的 stored event。
- 用于 durable hooks / outbox replay / repair 后发布已落盘事件。
- 若无法投递，也必须 discard sequence。

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

- background thread emit 时 publisher 未绑定 loop；
- manual compact failure 后补发 started/failed events 时的 sequence gap 防线；
- host teardown / close 路径中不能再等待 live subscriber 的场景。

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

- 不允许 subscriber 直接写 canonical event log 表达新的 runtime truth，除非该 subscriber 本身通过受控 `RuntimeSession.emit*()` 路径。
- 不允许 publisher 按 arrival order 发布跨线程 events。
- 不允许 hook 修改 canonical event object。
- 不允许 observer hook failure 中断后续 observer。
- 不允许把 observer hook return value 当作 permission/control decision。
- 不允许未完成 block 在 `RUN_ERROR` / `EXCEED_MAX_ITERS` 后变成 completed block。
- 不允许 `emit_from_thread()` 因慢 subscriber 阻塞 worker thread。
- 不允许 event 已落盘但 publisher sequence gap 未处理。

---

## 10. 测试守卫

最低测试门槛：

- thread events 按 canonical sequence 发布。
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
