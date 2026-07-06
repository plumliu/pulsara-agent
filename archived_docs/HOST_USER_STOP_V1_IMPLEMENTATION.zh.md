# Host User Stop / Interrupt V1 Implementation Plan

## 0. Summary

Pulsara 当前已经有 failed-turn transcript note，但没有用户显式 Stop 功能。V1 的目标是补齐：

1. 用户可以显式停止当前未终结 turn。
2. runtime 将该 turn 记录为 canonical `aborted`。
3. 下一轮模型能看到“上一轮是用户主动停止”的轻量 note。
4. Stop 是 soft cancellation，不承诺强杀所有 host-side work。

设计原则：

> Stop should be a canonical turn lifecycle transition, not a UI-only task cancellation.

## 1. Current Code Baseline

已存在：

- `LoopStatus.ABORTED = "aborted"`。
- `StopReason` 包含 `"aborted"`。
- `RunEndEvent.status` 是字符串，可承载 `"aborted"`。
- runtime timeline 已能映射 `RunEndEvent(status="aborted")`。
- `HostSession` 维护 `active_run_id`、`suspended_run_id`、`pending_approval`。
- `HostSession` 在 approval resume 后已经具备 suspended state 生命周期。
- `host/transcript.py` 会对最近一次 `RunEndEvent.status == "failed"` 注入 failure note。

缺口：

- `HostSession` 没有 active task/state handle。
- `HostCore` 没有 stop facade。
- `AgentRuntime` 没有 explicit abort/finalize helper。
- `AgentRuntime._finalize_run()` 没有 finalize-once guard；同一个 state 被两条路径 finalize 时会双发 `RunEndEvent`，也会重复触发 memory hook。
- active `stream_turn()` 是 async generator，stop 需要和它的锁/状态交互。
- pending approval run 可以 suspended，但没有“取消这个 suspended run”的 API。
- transcript 不处理 `aborted`。
- memory reflection 目前不按 `LoopStatus` 跳过失败或中断 run；`ABORTED` 若直接走现有 `on_session_end`，半截 assistant 文本可能被送进 reflection。

## 2. V1 Semantics

### 2.1 What Stop Means

V1 的 Stop 只表示：

> End the current Pulsara agent turn because the user explicitly asked to stop it.

它应产生：

```text
RunEndEvent(status="aborted", stop_reason="aborted")
```

并使下一轮 transcript 注入 interrupted note。

### 2.2 What Stop Does Not Mean

V1 不承诺：

- 强杀所有正在运行的 Python thread。
- 强杀所有 terminal subprocess。
- 清理 yielded background process。
- 取消 durable / remote / mobile session。
- 自动把用户的下一条 prompt 解释为 continuation。
- 写 memory graph。

如果 tool/terminal 已经部分执行，Stop 只能记录 turn 被用户停止。后台进程应由 terminal process 管理或 session close 管理。

### 2.3 Active vs Suspended Stop

Stop 应最终覆盖两种“当前 turn 未终结”形态：

1. **Active run**：`HostSession` 正在执行 `run_turn()` / `stream_turn()`。
2. **Suspended approval run**：runtime 已经 `WAITING_USER`，`HostSession.pending_approval is not None`。

两者都应最终变成 `RunEndEvent(status="aborted")`。

区别：

- active run 需要 soft cancel running task。
- suspended approval run 没有 active task，只需要用保存的 `_suspended_state` finalize aborted，并清 pending approval。

V1 implementation scope:

- first-class support for suspended approval stop
- first-class support for non-streaming `run_turn()` active stop
- `stream_turn()` stop deferred to a follow-up queue/driver-task refactor

### 2.4 Stop vs Approval Deny

Stop suspended approval 和 deny approval 不同：

- deny approval：向模型返回 denied tool result，让同一 run 继续。
- stop suspended approval：终结这个 run，不执行任何 pending tool，也不让模型继续本轮。

这两个操作都需要 UI/CLI 表达清楚。

## 3. Data / Event Contract

### 3.1 Status

Use existing vocabulary:

```python
state.status = LoopStatus.ABORTED
state.stop_reason = "aborted"
state.error_message = None
```

Do not introduce `cancelled` in runtime V1. Anybox 使用 `cancelled`，但 Pulsara 已经有 `aborted` 词表；V1 保持单词表，避免 `aborted/cancelled/interrupted/stopped` 混用。

### 3.2 RunEndEvent

`RunEndEvent` should be emitted exactly once for a stopped run:

```python
RunEndEvent(
    status="aborted",
    stop_reason="aborted",
    error_message=None,
)
```

No `RunErrorEvent` is required. User stop is not a provider/runtime error.

### 3.3 Optional Stop Event

V1 can avoid adding a new event type if `RunEndEvent(status="aborted")` is enough.

Only add `RunAbortRequestedEvent` if the UI needs a visible `cancelling` phase in the event log. This plan recommends deferring it unless tests reveal the need.

Rationale:

- Canonical terminal state already exists.
- `HostSession` can expose `stopping_run_id` / `active_run_id` in summary for live UI.
- Additional event schema should not be added just for transient control state.

### 3.4 Transcript Note

Add an interrupted note parallel to failure note.

Suggested text:

```text
Pulsara note: the previous turn was stopped by the user. The user's input from that turn was preserved. Any assistant text or tool work from that turn may be partial; if the user asks to continue, continue from the preserved input.
```

Important:

- Do not include stack traces.
- Do not include raw tool outputs.
- Do not write this note into canonical event log.
- Do not write this note into memory.
- Emit only for the most recent terminal `RunEndEvent`.

### 3.5 Failure Note Unification

Current `host/transcript.py` has `FAILURE_NOTE_TEXT` and `_FailedRunNoteTarget`.

Refactor to a generic terminal-note target:

```python
@dataclass(frozen=True, slots=True)
class _TerminalRunNoteTarget:
    run_id: str
    reply_id: str
    created_at: str | None
    kind: Literal["previous_turn_failed", "previous_turn_aborted"]
    text: str
```

Gate:

- last `RunEndEvent.status == "failed"` -> failure note
- last `RunEndEvent.status == "aborted"` -> interrupted note
- any newer `finished` / `failed` / `aborted` supersedes older notes

This preserves the existing “only most recent terminal run matters” rule.

## 4. Runtime API

### 4.1 Add abort helper

Add an internal helper to `AgentRuntime`:

```python
async def abort_run(
    self,
    state: LoopState,
    *,
    reason: str = "user_stop",
) -> AgentRunResult:
    async for _event in self.stream_abort_run(state, reason=reason):
        pass
    return self._run_result(state)
```

and streaming variant:

```python
async def stream_abort_run(
    self,
    state: LoopState,
    *,
    reason: str = "user_stop",
) -> AsyncIterator[AgentEvent]:
    ...
```

### 4.2 Abort helper behavior

`stream_abort_run()` should:

1. If state is already terminal `FINISHED` / `FAILED` / `ABORTED`, no-op or raise `ValueError` consistently. Prefer no-op only if called by finally after race.
2. Clear pending tool calls.
3. Set `state.status = LoopStatus.ABORTED`。
4. Set `state.stop_reason = "aborted"`。
5. Set `state.error_message = None`。
6. Emit `_finalize_run(state)` exactly once.

Need a guard to avoid double finalization. Add a first-class state flag:

```python
@dataclass(slots=True)
class LoopState:
    ...
    finalized: bool = False
```

`_finalize_run()` must start with:

```python
if state.finalized:
    return
state.finalized = True
```

Set `finalized = True` before the first `await`, so a stop/natural-finish race cannot emit two terminal events or run memory hooks twice. Do not use `scratchpad` for this; `finalized` is a core lifecycle invariant, not ad hoc task metadata.

### 4.3 Catch explicit cancellation in model loop

For active task cancellation, host will cancel the asyncio task. The runtime/host pair must convert explicit user stop into `ABORTED`, not `FAILED`.

Do not assume that emitting `RunEndEvent` from inside an `except asyncio.CancelledError` block is always sufficient. Local code shows `RuntimeEventPublisher` uses an independent drain task, so a single ordinary cancellation may still deliver correctly after the exception is caught. But teardown, repeated cancellation, or cancellation of the drain path can still make “emit while unwinding cancellation” fragile.

Before implementation, add an empirical test:

1. Start a run task that is inside runtime event publishing.
2. Cancel it once.
3. Catch `CancelledError` and try to emit `RunEndEvent(status="aborted")`.
4. Assert that a subscriber receives the event, not merely that the event log contains it.

V1 should still prefer the more defensive shape:

1. Host snapshots `_active_task` and `_active_state` off-lock.
2. Host marks `state.scratchpad["stop_requested"] = True` and cancels the task.
3. If the task exits without finalizing, host calls `agent_runtime.abort_run(state)` from the stopper's uncancelled context.
4. If the task already finalized naturally, `state.finalized` makes `abort_run()` a no-op.

This avoids misclassifying caller shutdown / test cancellation as user stop.

### 4.4 Tool execution cancellation

Current `_stream_tool_batch_events()` creates `asyncio.to_thread(...)` tasks and cancels pending asyncio tasks in `finally`.

V1 contract:

- Best-effort only.
- If the tool thread cooperatively exits, good.
- If already spawned subprocess continues, it is managed by terminal session/process APIs.
- Do not claim Stop killed the underlying OS work.

Potential optional improvement:

- When stop is requested, emit synthetic cancelled tool result for tool calls that have started but not ended.

This is useful for UI but can be deferred if it complicates event consistency. Anybox’s renderer does this at UI projection time; Pulsara V1 can start with run-level aborted.

## 5. HostSession API

### 5.1 New fields

Add to `HostSession`:

```python
active_state: LoopState | None
active_task: asyncio.Task | None
stopping_run_id: str | None
```

or private variants:

```python
_active_state: LoopState | None
_active_task: asyncio.Task | None
```

`active_run_id` remains public summary. `active_state` is internal.

### 5.2 run_turn path

Today `run_turn()` awaits `agent_runtime.run_task(...)` directly inside `_run_lock`.

To support stop:

1. Create state.
2. Store `_active_state = state`.
3. Create task for `agent_runtime.run_task(...)`.
4. Store `_active_task = task`.
5. Await task.
6. In finally, clear `_active_task`, `_active_state`, `active_run_id`, `stopping_run_id`。

Stop API can then cancel `_active_task`.

### 5.3 stream_turn path

`stream_turn()` is harder because it is an async generator.

Recommended shape:

- Keep local `state`.
- Store `_active_state = state` before streaming.
- Use an internal queue/task to drive `agent_runtime.stream_task(...)`, so `stop_current_turn()` can cancel that driver task.

This refactor is large enough to keep out of the first stop implementation. V1 should support suspended approval stop and non-streaming `run_turn()` stop first. Streaming stop should be a follow-up PR with its own queue/driver-task tests, because `stream_turn()` currently holds `_run_lock` across `yield` boundaries and has different caller semantics from `run_turn()`.

### 5.4 stop_current_turn

Add:

```python
async def stop_current_turn(self, *, reason: str = "user_stop") -> AgentRunResult | None:
    ...
```

Behavior:

1. If session closed: raise.
2. If `pending_approval is not None`: abort suspended state through runtime helper; clear pending.
3. Else if no active task/state: return `None` or raise `HostSessionNoActiveRunError`。
4. Snapshot `_active_task` / `_active_state` into local variables without taking `_run_lock`。
5. If the snapshot is `None` or task is already done, treat stop as an idempotent no-op or race with natural finish.
6. Mark `stopping_run_id = active_run_id`。
7. Mark `state.scratchpad["stop_requested"] = True`。
8. Cancel the snapshotted task.
9. Await bounded completion.
10. If the task exited without finalizing, call `agent_runtime.abort_run(state)` from this uncancelled stopper context.
11. If task does not finish within timeout:
   - leave session in `stopping`? or return best-effort summary?
   - V1 should not release `_run_lock` until driver finally exits, unless a queue is implemented.

Recommendation:

- Use bounded wait for API facade responsiveness.
- But keep `_run_lock` held by the underlying task until it actually exits.
- `run_turn()` / `stream_turn()` should still reject new turns while `_run_lock.locked()` or `stopping_run_id is not None`。

This avoids Anybox’s “cancel then immediately idle” race.

Important locking invariant:

- active-run stop must not acquire `_run_lock`, because the active `run_turn()` already holds it while awaiting runtime work.
- off-lock `_active_task` reads are inherently racy; solve this by snapshotting local references and treating `None` / `done()` as a clean race with natural completion.
- suspended-approval stop may acquire `_run_lock`, because a suspended approval run has returned and released the lock.

### 5.5 Pending approval stop

If the run is suspended waiting for approval:

1. `pending = self.pending_approval`
2. `state = self._require_suspended_state(pending)`
3. Clear approval only after abort finalization succeeds.
4. Call `agent_runtime.abort_run(state, reason="user_stop")`
5. Clear `pending_approval`, `_suspended_state`, `suspended_run_id`
6. Return aborted result.

This makes “Stop current turn” work even when no active task is running.

### 5.6 HostCore facade

Add:

```python
async def stop_current_turn(self, host_session_id: str, *, reason: str = "user_stop") -> AgentRunResult | None
```

and optional streaming/events variant later if UI needs it.

### 5.7 Summary fields

Extend `HostSession.summary()`:

```python
"stopping_run_id": self.stopping_run_id,
"is_stopping": self.stopping_run_id is not None,
```

This is enough for inspect/UI to show cancelling state without a new event type.

## 6. CLI / REPL

### 6.1 Minimal REPL command

Add to `host repl`:

```text
:stop
```

Behavior:

- If active run exists, stop it.
- If pending approval exists, abort suspended run.
- If nothing active/pending, print “No active turn to stop.”

### 6.2 One-shot host run

`host run` is one-shot and synchronous. There is no separate user input channel to send `:stop`, so V1 does not need stop support in one-shot mode.

KeyboardInterrupt can be mapped later:

- Ctrl+C during `host run` should ideally request stop and emit aborted run.
- This is a separate CLI signal handling task, not required for first HostCore API.

## 7. Transcript Reconstruction

### 7.1 Current failure behavior

Current behavior:

- Find last terminal `RunEndEvent`。
- If status is `failed`, inject failure note.
- Only latest terminal run matters.

### 7.2 New aborted behavior

Extend to:

- `failed` -> `FAILURE_NOTE_TEXT`
- `aborted` -> `INTERRUPTED_NOTE_TEXT`

Examples:

```text
User: please do a long task
Assistant: partial...
RunEnd(status=aborted)
User: continue
System note: previous turn was stopped by user...
```

The note should be inserted after the aborted run’s replayable assistant content if any, same as failure note.

### 7.3 Provider delivery

Current transcript failure note uses `SystemMsg` in prior messages. Earlier provider experiments showed mid-list system role can be provider-sensitive, but in current code this path already exists.

Implementation choices:

1. Keep parity with current failure note for V1.
2. If later moving failure note into top-level system prompt, move interrupted note together.

Do not introduce a separate delivery channel only for stop.

## 8. Memory Boundary

Stop / interrupt does not write memory.

Allowed:

- `RunEndEvent(status="aborted")`
- runtime timeline item/status
- transcript projection note
- UI status

Not allowed:

- canonical Claim / Preference
- “user likes stopping commands” memory
- summarizing partial assistant output as completed fact

Memory hooks need a new status gate. Today reflection does not skip `FAILED` or `ABORTED` by status, so this is not “failed parity”; it is new protection required by Stop.

For `ABORTED`, hooks should treat the run as incomplete:

- do not run memory reflection that assumes a successful answer
- do not sync interrupted turn as durable conversational truth

Need to audit `_finalize_run()` hooks:

- Today `_finalize_run()` calls `on_turn_end` / `on_session_end` style hooks.
- For `ABORTED`, hooks may still need to cleanup per-run cache.
- Reflection/write hooks must check status and skip fact extraction before `reflection.reflect(...)` can read `state.messages`.
- Tool or memory events that already completed before Stop should not be retroactively unwound; the gate is about not extracting new durable facts from partial aborted conversation text.

## 9. Timeline / UI Projection

Runtime timeline should show:

```text
status = "aborted"
stop_reason = "aborted"
```

If partial assistant text exists, keep it but mark run aborted.

If tool calls started and no result end arrived, V1 may leave them as incomplete in event log. UI projection can mark them cancelled later. Do not synthesize false success.

Anybox lesson:

- late tool input/history after cancellation must not revive a cancelled turn.

Pulsara V1 test should at least ensure timeline terminal status remains `aborted` even if a replayable partial reply exists.

## 10. Race / Lock Invariants

### 10.1 No immediate idle after stop request

After stop is requested:

- session should be considered busy/stopping until abort finalization completes
- ordinary new `run_turn()` should not start

This avoids Anybox’s early-idle race.

### 10.2 Stop does not deadlock

Stop must not wait on a task while holding a lock that the task needs to finalize.

If `stop_current_turn()` acquires `_run_lock`, it may deadlock because active `run_turn()` already holds it. Therefore:

- `stop_current_turn()` must not require `_run_lock` for active-run cancellation.
- It can use a separate `_stop_lock` if needed.
- It can read `_active_task/_active_state` under ordinary Python atomic assignment discipline or a small separate lock.

For suspended approval, `_run_lock` is released; stop can acquire it or call runtime helper directly. Prefer acquiring `_run_lock` to serialize with `resolve_approval()`.

### 10.3 Finalize once

A stopped run must emit exactly one terminal `RunEndEvent`.

Cases:

- stop before model stream starts
- stop during model stream
- stop during tool execution
- stop after model already completed but before finalize
- stop races with natural finish

Tests must lock this down.

## 11. Test Plan

### 11.0 Cancellation delivery experiment

Before choosing the active-run cancellation implementation, add a focused asyncio test:

1. Create a runtime session with a subscriber.
2. Start a task that catches a single `CancelledError` and emits a terminal event.
3. Cancel the task once.
4. Assert the subscriber receives the terminal event.

This test does not replace the defensive host-finalize design. It documents the real behavior of the current publisher and prevents future changes from invalidating cancellation delivery assumptions silently.

### 11.1 Transcript tests

In `tests/test_host_core.py` or dedicated transcript tests:

1. `RunEnd(status="aborted")` injects interrupted note.
2. Interrupted note includes preserved user input.
3. Interrupted note does not include raw error/tool data.
4. A newer successful run suppresses older aborted note.
5. Failed and aborted use distinct note text and metadata kind.

### 11.2 Runtime tests

In `tests/test_agent_runtime_loop.py`:

1. `abort_run(state)` emits `RunEndEvent(status="aborted")`。
2. abort result has `LoopStatus.ABORTED` and `stop_reason == "aborted"`。
3. no `RunErrorEvent` emitted for user stop。
4. finalize hooks run once for aborted run, or documented cleanup hook behavior is asserted。
5. stop during scripted slow model stream produces aborted terminal event。
6. stop during tool execution is best-effort and does not mark tool success falsely。
7. stop racing natural finish emits exactly one `RunEndEvent` because `LoopState.finalized` gates `_finalize_run()`。

### 11.3 HostSession tests

In `tests/test_host_core.py`:

1. active `run_turn()` can be stopped from another task.
2. stop while active blocks new run until finalization.
3. stop returns/records `ABORTED` and clears `active_run_id` after finalization.
4. pending approval stop aborts suspended run and clears pending approval.
5. stop while no active/pending run returns clean no-op or raises typed error.
6. stop does not deadlock with `_run_lock`.
7. stop racing natural finish emits only one `RunEndEvent`.
8. after aborted turn, next `run_turn()` prior context contains interrupted note.
9. active stop snapshots `_active_task` off-lock; a `None` or done snapshot is treated as a clean race, not an exception.

### 11.4 HostCore / CLI tests

1. `HostCore.stop_current_turn()` delegates to session.
2. `host repl :stop` calls stop API.
3. `:stop` while no run prints clear message.
4. `:stop` while pending approval aborts suspended run.

### 11.5 Real LLM smoke

After unit tests:

1. Start a long-running real turn; stop it; next prompt asks “继续刚才的任务”; assert model sees interrupted note and does not hallucinate completion.
2. Trigger pending approval; stop instead of approve/deny; next prompt asks continue; assert model knows previous turn was stopped before tool execution.

Keep real smoke lightweight and use harmless commands.

## 12. Suggested PR Breakdown

### PR0: Cancellation/finalization grounding

- Add the cancellation-delivery experiment.
- Add `LoopState.finalized`.
- Guard `_finalize_run()` with finalize-once behavior.
- Add double-finalize regression tests.

This PR is a prerequisite for active-run stop. Transcript-only work can be developed independently, but active cancellation should not land without this guard.

### PR1: Transcript aborted note

- Add `INTERRUPTED_NOTE_TEXT`。
- Refactor terminal note target.
- Add transcript tests for `aborted`。

This can land before active stop API because `RunEnd(status="aborted")` is already representable.

### PR2: Runtime abort helper

- Add `stream_abort_run()` / `abort_run()`。
- Ensure `_finalize_run()` handles `ABORTED` cleanly.
- Add memory reflection status gate so aborted runs clean up but do not reflect partial conversation text.
- Add runtime unit tests.

### PR3: HostSession stop for suspended approval

- Add `stop_current_turn()` handling `pending_approval` first.
- Abort suspended state.
- Clear pending approval.
- Add HostSession tests.

This is easier than active task cancellation and validates the lifecycle.

### PR4: HostSession active-run soft stop

- Store `_active_state` / `_active_task`。
- Support non-streaming `run_turn()` active stop first.
- Convert explicit cancellation to `ABORTED` using host-side abort finalization if the cancelled task did not finalize itself。
- Ensure no immediate idle race.
- Add active stop tests.

### PR5: HostCore / CLI facade

- Add HostCore pass-through.
- Add `host repl :stop`。
- Add CLI tests.

### PR6: UI / process polish later

- `stream_turn()` stop via queue/driver-task refactor.
- `cancelling` live status.
- Synthetic cancelled tool projection if needed.
- Ctrl+C mapping for `host run`。
- Terminal process cleanup command integration.
- Durable / remote stop endpoints.

## 13. Acceptance Criteria

V1 is complete when:

- User can explicitly stop an active or suspended current turn.
- A stopped turn emits exactly one `RunEndEvent(status="aborted", stop_reason="aborted")`。
- Stop does not emit `RunErrorEvent`。
- Next turn receives an interrupted note distinct from failure note.
- Pending approval can be stopped without executing or denying tools.
- New turns cannot start while active stop finalization is still in progress.
- Memory graph receives no stop-derived facts.
- Existing approval resume tests still pass.
- Existing failure-note behavior remains unchanged.

## 14. Open Questions

1. Should aborted run call memory `on_turn_end` cleanup hooks? Likely yes for cleanup, but reflection/write must skip extraction.
2. Should active stop return immediately after requesting cancellation, or wait until `RunEndEvent` is emitted? V1 should prefer waiting for finalization in tests, but API may expose “stopping” later.
3. Should transcript note stay as mid-list `SystemMsg` or move with failure note into top-level system prompt? Keep parity with current failure note for this feature.
4. Should stop of pending approval be named `stop_current_turn()` or `cancel_pending_approval()` in user-facing UI? API can support both later, but V1 should have one canonical host method.

## 15. Principle

User Stop is not failure recovery. It is a user-authored boundary in the runtime history.

The next model call should know that boundary exists, but should not treat the partial prior turn as completed work.
