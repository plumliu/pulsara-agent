# Host User Stop / Interrupt Survey

## 0. Scope

本文调研本地三个 agent 项目的“用户显式 Stop / Interrupt”设计：

- `/Users/plumliu/Desktop/python_workspace/codex`
- `/Users/plumliu/Desktop/python_workspace/hermes-agent`
- `/Users/plumliu/Desktop/python_workspace/anybox`

目标不是比较 UI，而是抽取 Pulsara 下一步需要的后端语义：

1. 用户显式停止当前 turn 时，runtime 如何结束？
2. 停止是否写入 canonical history / event log？
3. 下一轮模型如何知道上一轮是“用户主动停止”，而不是 provider/runtime error？
4. tool / terminal / 子 agent / late events 如何处理？

结论先行：Pulsara 应该吸收 Codex 的 canonical lifecycle 和 history marker 思路，吸收 Hermes 的 soft interrupt / bounded wait 现实主义，吸收 Anybox 的 cancelling/cancelled UI 状态经验；但 V1 不应承诺强杀所有 host-side work。

## 1. Pulsara Current State

Pulsara 已经具备一半地基：

- `LoopStatus.ABORTED` 已存在。
- `StopReason` 已包含 `"aborted"`。
- runtime timeline 已能映射 `RunEndEvent(status="aborted")`。
- Host transcript 已实现“上一轮 failed 后下一轮注入 failure note”。

但缺的部分也很明确：

- 没有用户显式 stop API。
- `HostSession` 不保存 active run task，只保存 `active_run_id`。
- `HostCore` / CLI / REPL 没有 stop facade。
- `AgentRuntime` 没有 explicit cancellation path。
- transcript 只对最近 `RunEndEvent.status == "failed"` 注入 note，不处理 `aborted`。

因此当前 `aborted` 只是词表和 timeline 兼容值，不是实际可达的用户停止语义。

## 2. Codex

### 2.1 Core Shape

Codex 的 interrupt 是 actor/session queue 中的正式 operation，而不是 UI 直接杀线程。

关键路径：

- `Op::Interrupt`
- `handlers::interrupt()`
- `Session::interrupt_task()`
- `Session::abort_all_tasks(TurnAbortReason::Interrupted)`
- `handle_task_abort(...)`
- `TurnAbortedEvent`

本地代码位置：

- `codex-rs/core/src/session/handlers.rs`
- `codex-rs/core/src/tasks/mod.rs`
- `codex-rs/core/src/context/turn_aborted.rs`
- `codex-rs/core/src/thread_manager.rs`

### 2.2 Abort Lifecycle

`abort_all_tasks(TurnAbortReason::Interrupted)` 做的是完整状态转移：

1. 取出 active turn。
2. 对 running task 调 `handle_task_abort`。
3. cancel task 的 `CancellationToken`。
4. 等一个很短的 graceful timeout。
5. abort Tokio task handle。
6. 调 task 自己的 `abort()` hook。
7. 用户 interrupt 时，写入 interrupted history marker。
8. flush rollout，确保 marker 可见。
9. 发送 `TurnAbortedEvent`。
10. 清理 pending input / approvals。

这不是“中断失败的副作用”，而是 canonical lifecycle。

### 2.3 Model-Visible Marker

Codex 不只发 `TurnAbortedEvent` 给 UI，还会把上一轮中断写成模型可见历史 marker。

其 guidance 大意：

- 用户有意中断上一轮。
- 任何 running unified exec process 可能仍在后台运行。
- aborted tool/command 可能已经部分执行。

Codex 对不同版本可使用 contextual user marker 或 developer message。这个选择说明两个点：

1. “上一轮被中断”必须进入模型上下文，否则用户说“继续”时模型缺少关键事实。
2. marker 不应伪装成用户新输入；它是 derived runtime note。

### 2.4 Background Processes

Codex 将 interrupt 当前 turn 和清理 background terminals 区分开：

- interrupt 当前 task：`Op::Interrupt`
- clean background terminals：单独 operation

这点对 Pulsara 很重要。Stop 当前 agent turn 不应自动承诺杀死所有 yielded terminal process。否则会把“停止思考/工具链”与“进程管理”绑死。

### 2.5 Lessons for Pulsara

Codex 给出的主教训：

- Interrupt 应该是 first-class runtime operation。
- Abort 应该写 canonical event，而不是只改 UI。
- 下一轮模型应看到 interrupted marker。
- 清理 background process 是相邻但独立的语义。
- 中断后要避免留下“半个 turn”让 transcript 重建误判。

## 3. Hermes

### 3.1 Core Shape

Hermes 采用更 Python-native 的 soft interrupt：

- `AIAgent.interrupt(message=None)` 设置 `_interrupt_requested`。
- tool worker thread 会收到 per-thread interrupt flag。
- 子 agent 会被递归 interrupt。
- conversation loop、provider streaming、tool execution 在多个 checkpoint 检查 `_interrupt_requested`。

本地代码位置：

- `run_agent.py`
- `agent/conversation_loop.py`
- `agent/chat_completion_helpers.py`
- `agent/tool_executor.py`
- `gateway/platforms/api_server.py`

### 3.2 Soft Interrupt

`agent.interrupt()` 的职责：

- 设置 agent 级 `_interrupt_requested`。
- 保存可选 `interrupt_message`。
- 设置当前 execution thread 的 interrupt flag。
- 传播到 tool worker threads。
- 传播到 active child agents。

conversation loop 会在 API call 前、stream retry 前、tool loop 中、error handling 中检查该 flag，并返回带 `interrupted=True` 的 result。

这是一种实用的 cooperative cancellation，而不是 OS 级强杀。

### 3.3 API Stop

Hermes 的 API stop 更现实：

1. `agent.interrupt("Stop requested via API")`
2. 如果 asyncio task 还在，调用 `task.cancel()`
3. 用 bounded wait 等待最多数秒
4. 若 executor thread 没能退出，记录 warning，不阻塞 stop handler

它明确承认：

- Python executor thread 不能被 `task.cancel()` 可靠抢占。
- 真正退出依赖 agent/tool 自己检查 interrupt flag。
- stop handler 不应无限等待。

这正好适合 Pulsara V1：先做 soft stop，不承诺硬杀所有正在运行的 host-side work。

### 3.4 Busy Input Modes

Hermes 区分普通并发输入和显式 stop：

- `queue`：新输入排队。
- `steer`：新输入作为当前 turn 的 steering。
- `interrupt`：新输入打断当前 run。
- `/stop`：显式停止当前 session/run，走更强路径。

成熟产品经验是：用户普通发消息和用户点 Stop 不应混成一件事。

Pulsara V1 可以先不做 `queue/steer/interrupt` 三模式，但应保留这个分离：

- 普通 `run_turn()` 在 active run 时仍然拒绝或排队。
- 显式 `stop_current_turn()` 才进入 abort path。

### 3.5 Memory Boundary

Hermes 对 interrupted turn 很谨慎：

- interrupted turn 不当作完整成功事实。
- 后续 memory / skill review 会跳过 interrupted turn。
- 外部 memory sync 会收到 `interrupted=True`。

这与 Pulsara 的 memory graph 纪律一致：用户中断不是可直接写入 preference / claim 的用户事实。

### 3.6 Lessons for Pulsara

Hermes 给出的主教训：

- Python V1 应采用 cooperative soft interrupt。
- stop handler 必须 bounded wait。
- tool/worker/thread cancellation 要诚实，不可承诺抢占。
- 子 agent / worker propagation 是后续增强，不必塞入第一版。
- interrupted turn 应影响下一轮上下文，但不进入 memory graph。

## 4. Anybox

### 4.1 Caveat

Anybox 更像个人项目，成熟度不能和 Codex/Hermes 直接等量比较。它的不足不应被用作“反例证明”。它更适合作为桌面产品状态建模的参考。

### 4.2 Core Shape

Anybox 使用 TypeScript / JS 的 `AbortController`：

- `SessionRunner` 为每个 operation 创建 `AbortController`。
- `cancel()` 设置 runner 状态为 `cancelling`。
- 调 `controller.abort()`。
- 通知 running state cancelled。
- 后续 finally 再清 active。

本地代码位置：

- `packages/anyboxagent/src/session/runtime/session-runner.ts`
- `packages/anyboxagent/src/session/runtime/running-state.ts`
- `packages/anyboxagent/src/session/runtime/runtime-event.ts`
- `packages/desktop/src/renderer/src/app/stream.ts`

### 4.3 Runtime Events

Anybox 定义了明确的 runtime phase：

- `running`
- `cancelling`
- `cancelled`
- `failed`
- `completed`
- `waiting_approval`
- `executing_tool`

终态事件里有：

- `turn.completed`
- `turn.failed`
- `turn.cancelled`

前端会将 `turn.cancelled` 渲染为 “Turn cancelled”，并把 late tool trace / unfinished tool input 标记为 cancelled。

### 4.4 Known Weakness

Anybox 的 cancel 机制有一个风险：

- `RunningState.cancel()` 可能较早删除 running record。
- active turn 的 finally 可能尚未完成。
- 用户如果极快发起新 prompt，可能先通过 running-state 检查，再撞到旧 active turn。

它自己的设计文档也建议不要在 abort 发出后立即视为 idle，而是保留 `cancelling -> cancelled` 状态机。

这不是“Anybox 做错了所以不要学”，而是桌面 agent 在 cancel 上天然会遇到的竞态。

### 4.5 Lessons for Pulsara

Anybox 给出的主教训：

- UI 需要 `cancelling` 状态，而不只是 running/idle。
- canonical terminal event 应区分 `cancelled` / `failed`。
- late tool events 应归属到 cancelled turn，而不是复活 UI。
- stop 发出后不能马上允许新 turn，除非有明确 queue semantics。

## 5. Cross-System Comparison

| System | Primary Mechanism | Canonical Event | Model-Visible Marker | Tool/Process Story | Key Lesson |
| --- | --- | --- | --- | --- | --- |
| Codex | `Op::Interrupt` + `CancellationToken` + task abort | `TurnAborted` | interrupted marker | background terminal cleanup is separate | interrupt is lifecycle |
| Hermes | `_interrupt_requested` soft flag + bounded task cancel | interrupted result / API status | interrupt message / context handling | cooperative worker/tool checks | be honest about Python cancellation |
| Anybox | `AbortController` + runner status | `turn.cancelled` | mostly UI/runtime, not central marker | UI settles late tool traces cancelled | keep `cancelling` distinct from idle |
| Pulsara today | none | `aborted` vocabulary only | failed note only | terminal processes independent | needs explicit stop path |

## 6. Recommended Pulsara Direction

Pulsara V1 should implement:

1. Explicit user stop as a first-class Host operation.
2. Canonical `RunEndEvent(status="aborted", stop_reason="aborted")`.
3. Transcript-level interrupted note for the next turn.
4. Soft cancellation only: cancel model/agent task cooperatively, do not promise hard process kill.
5. Pending approval stop: if the current run is suspended waiting for approval, Stop should abort that suspended run.
6. No memory write: interrupted note is derived runtime context, not canonical memory.

V1 should not implement:

- Queue/steer/interrupt busy input modes.
- Durable cross-process cancellation.
- Hard kill of all host commands.
- Background terminal cleanup as part of Stop.
- Remote/mobile stop transport.

## 7. Product Semantics

Suggested user-facing distinction:

- **Stop current turn**: end current agent run as `aborted`; preserve partial history and allow next prompt.
- **Kill background process**: use terminal process management.
- **Deny pending approval**: answer approval with deny.
- **Cancel pending approval / stop turn**: abort the suspended run without executing tools.

This keeps user intent precise. “Stop” means stop the agent turn, not necessarily destroy every side effect it already started.

## 8. Main Risk

The main risk is pretending soft cancellation is stronger than it is.

Python cannot reliably preempt work already running in a thread. Terminal commands can also outlive the agent loop if they were intentionally yielded or if the subprocess has already been spawned. V1 must report this honestly in note text, logs, docs, and tests.

The good news: this is still valuable. A canonical aborted turn plus next-turn interrupted note is enough to make “我刚刚点了 Stop，现在继续” coherent, even before hard process control becomes perfect.
