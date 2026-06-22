# Approval Resume V1 Implementation Plan

本文定义 Pulsara approval resume 的第一轮落地方案。目标是补齐当前 permission V1 的最大缺口：`WAIT_FOR_USER` 不再是不可恢复的 halt，而是可由用户批准/拒绝后继续执行的暂停点。

## 0. Scope

V1 做：

- host-session scoped in-memory pending approval。
- 结构化 approve/deny resolution。
- 通过 `UserConfirmResultEvent` 记录用户选择。
- approve 后执行原始 tool call snapshot。
- deny 后生成 denied tool result，让模型看到拒绝并继续。
- 先覆盖现有 `trusted_host + risky_only + terminal=allow` 下的 risky terminal approval。

V1 不做：

- durable permission request table。
- allow forever/session/project。
- mobile remote approval。
- web API 路由。
- 把 approval 写入 memory graph。
- 开放 `terminal_access=ask`。
- 开放 write/terminal 可用时的 `approval_policy=on_request`。

## 1. Current Code Grounding

### 1.1 WAIT_FOR_USER 已经存在

`src/pulsara_agent/runtime/permission.py` 定义：

- `PermissionDecisionKind.ALLOW`
- `PermissionDecisionKind.DENY`
- `PermissionDecisionKind.WAIT_FOR_USER`

`PolicyPermissionGate` 会在 risky terminal 命令下返回 `WAIT_FOR_USER`。

### 1.2 Runtime 已经会 emit RequireUserConfirmEvent

`src/pulsara_agent/runtime/agent.py` 的 `_execute_tool_blocks()` 中：

- permission gate 返回 `WAIT_FOR_USER`
- tool calls 被转成 `ToolCallBlock(state=ASKING)`
- runtime emit `RequireUserConfirmEvent`
- state 进入 `LoopStatus.WAITING_USER`

### 1.3 UserConfirmResultEvent 已定义但没有生产者

`src/pulsara_agent/event/events.py` 定义：

- `ConfirmResult`
- `UserConfirmResultEvent`

`src/pulsara_agent/message/reducer.py` 已经能消费它，但全仓没有实际 emit 该事件的恢复入口。

### 1.4 HostSession 每轮新建 LoopState

`src/pulsara_agent/host/session.py` 的 `run_turn()` / `stream_turn()` 每次都会：

- rebuild prior messages
- `agent_runtime.new_state()`
- run/stream task

因此普通下一轮用户输入不会恢复 pending tool call。

### 1.5 Policy resolver 暂时禁止 unresumable 策略

`resolve_permission_policy()` 当前拒绝：

- `terminal_access=ask`
- write/terminal 可用时的 `approval_policy=on_request`

V1 完成前保持这个限制。

## 2. Core Contract

### 2.1 Pause is not finish

approval pause 不是 finished，不是 failed，也不是用户新 turn。

事件语义：

```text
RunStart
ReplyStart/ReplyEnd with tool call
RequireUserConfirm
...pause...
UserConfirmResult
ToolResultStart/Delta/End
next model turn
RunEnd(final/failed/aborted)
```

因此 V1 采用 suspended run 作为唯一语义：进入 `WAITING_USER` 时不立即 `_finalize_run()`，也不发 terminal `RunEnd`。批准或拒绝后，runtime 在同一个 `LoopState`、同一个挂起 `reply_id` 下写入 confirmation 和 tool results，然后才进入下一轮 model turn。

V1 明确不做 continuation-run fallback。`UserConfirmResultEvent` 在 reducer 中按 `reply_id` 投影回原 assistant reply；另开 continuation run 会让确认结果和原 `ASKING` tool call 容易错配。若未来确实需要兼容旧 trace，也应作为独立迁移方案，而不是 V1 的备选路径。

### 2.2 Resolution must reference the original request

用户批准/拒绝必须引用 pending approval id，不能依赖自然语言“继续”。

V1 的 resolution 输入：

```python
@dataclass(frozen=True, slots=True)
class ApprovalResolution:
    approval_id: str
    decisions: tuple[ToolApprovalDecision, ...]

@dataclass(frozen=True, slots=True)
class ToolApprovalDecision:
    tool_call_id: str
    confirmed: bool
    rules: tuple[dict, ...] = ()
```

其中 `approval_id` 由 HostSession 在构造 `PendingApproval` 时 mint。当前事件 schema 上没有 `approval_id` 字段：`RequireUserConfirmEvent` 只携带待确认 tool calls，`UserConfirmResultEvent` 通过 `ConfirmResult.tool_call.id` 关联具体调用。V1 不改事件 schema；durable approval request id 是否进入事件/timeline 留给后续持久化 PR。

`rules` 暂时只用于穿透到 `ConfirmResult.rules`，不写任何持久 rule store。当前 `MessageReducer` 不消费 `rules`，所以 replay 只恢复 confirmed/denied 状态，不恢复规则。

### 2.3 Approval request snapshots original tool calls

V1 pending approval 对象：

```python
@dataclass(slots=True)
class PendingApproval:
    approval_id: str
    host_session_id: str
    runtime_session_id: str
    run_id: str
    turn_id: str
    reply_id: str
    tool_calls: tuple[ToolCallBlock, ...]
    suggested_rules: tuple[dict, ...]
    created_at: float
```

注意：

- `tool_calls` 必须是 deep copy。
- `tool_calls[*].input` 是批准后执行的唯一输入来源。
- 不重新询问模型生成 tool call。
- 不把 pending approval 写入 memory graph。

### 2.4 One pending approval per HostSession in V1

V1 限制一个 HostSession 同时最多一个 pending approval。

理由：

- 当前 runtime loop 是单 active run。
- HostSession 已有 `_run_lock`。
- 多 pending approval 需要更复杂的 UI/turn arbitration，不是 V1。

当 pending approval 存在时：

- `run_turn()` / `stream_turn()` 应拒绝普通新用户输入，提示先 resolve/cancel pending approval。
- `resolve_approval()` / `stream_approval_resolution()` 是唯一继续入口。

锁不变量：暂停不能在 `_run_lock` 内 `await` 一个用户 future。`run_task()` / `stream_task()` 必须在产生 pending approval 后返回，让 `async with self._run_lock` 退出；之后 `resolve_approval()` 再获取同一把锁继续运行。否则 resolve 入口会被仍在等待用户的 run 自己锁死。

### 2.5 Deny is also a tool result

拒绝不是静默丢弃。deny 后应为对应 tool call 生成 denied tool result，使模型能看到：

- 哪个 tool call 被拒绝。
- 拒绝原因。
- 接下来应解释、询问替代方案或停止。

可以复用 `build_tool_result_error_events(..., state=ToolResultState.DENIED)`。

## 3. Proposed Code Changes

### 3.1 新增 runtime approval model

新增文件：

- `src/pulsara_agent/runtime/approval.py`

包含：

- `PendingApproval`
- `ApprovalResolution`
- `ToolApprovalDecision`
- helper：`pending_approval_from_state(state, host_session_id)`

`pending_approval_from_state()` 要求：

- `state.status is LoopStatus.WAITING_USER`
- `state.pending_tool_calls` 非空
- 每个 pending tool call state 是 `ASKING`

### 3.2 调整 `_execute_tool_blocks()`

在进入 `_execute_tool_blocks()` 之前，当前代码已经把 assistant 产出的原始 tool-call blocks 写入 `state.pending_tool_calls`。但在 `WAIT_FOR_USER` 分支中，runtime 又构造了一组带 `ASKING` 和 `suggested_rules` 的 approval blocks；这组 approval snapshot 目前只是局部变量，没有替换回 state。

需要改为：

```python
state.pending_tool_calls = blocks
state.status = LoopStatus.WAITING_USER
...
emit RequireUserConfirmEvent(..., tool_calls=blocks)
return
```

这样 HostSession 能从 state 构造 pending approval。

### 3.3 避免 WAITING_USER 立即 finalize

当前 `_stream_task()` 循环结束后无条件：

```python
async for event in self._finalize_run(state):
    yield event
```

V1 必须改成：

```python
if state.status is LoopStatus.WAITING_USER:
    return
async for event in self._finalize_run(state):
    yield event
```

这使 waiting run 保持 suspended。`RunEnd` 只在真正 final/failed/aborted 后发出。

影响：

- `on_turn_end` memory hook 不会在等待审批时运行。
- 用户批准/拒绝后继续同一 loop，最终才运行 turn-end hook。
- transcript projection 不会把 half-run 误当完成 run。

同时需要修正 runtime timeline 投影：`build_run_timeline()` 当前只有看到 `RunEndEvent` 才设置 run 级 terminal status；如果没有 `RunEndEvent`，会落到 `completed`。而 `RunTimelinePersistenceHook` 会在 `REPLY_END` 增量持久化 timeline，所以 suspended run 可能被错误写成 completed。PR1 必须让尾部存在未解决的 `RequireUserConfirmEvent` 时把 timeline `status` 投影为 `waiting_user`，并覆盖测试。

### 3.4 新增 AgentRuntime resume 入口

新增：

```python
async def resume_after_approval(
    self,
    state: LoopState,
    resolution: ApprovalResolution,
) -> AgentRunResult: ...

async def stream_after_approval(
    self,
    state: LoopState,
    resolution: ApprovalResolution,
) -> AsyncIterator[AgentEvent]: ...
```

职责：

1. 校验 `state.status is WAITING_USER`。
2. 校验 `resolution.approval_id` 匹配 HostSession pending approval。
3. 校验每个 decision 的 `tool_call_id` 属于 `state.pending_tool_calls`。
4. 在挂起的 `reply_id` 下 emit `UserConfirmResultEvent`。
5. 在同一个挂起 `reply_id` 下为 denied calls 生成 denied tool result。
6. 在同一个挂起 `reply_id` 下执行 approved calls 的原始 tool call snapshot。
7. 把 tool results append 到 `state.messages`。
8. 执行原本 tool results 后的 hook/persistence/compaction 流程。
9. tool results 处理完之后才 `state.begin_next_turn()`，轮换到下一条 assistant reply。
10. 回到模型 loop，直到 final/failed/aborted/再次 waiting。

finalize-once 不变量：暂停期间不能触发 `_finalize_run()`，因为它把 `RunEndEvent`、`on_turn_end` 和 memory hook 的 `on_session_end` 清理耦在一起。跳过 finalize 会让 per-run memory hook 缓存存活过暂停；resume 后真正终止时必须恰好触发一次。若 resume 后再次 waiting，也仍然不能触发 finalize。

### 3.5 Refactor tool execution helpers

为了避免复制 `_execute_tool_blocks()` 中 post-gate 执行逻辑，PR1 必须先抽取 helper。当前 ALLOW 路径的批次迭代、tool budget 检查、`_stream_tool_batch_events()`、result block 组装、`state.tool_results/messages` append 和 `tool_call_count` 自增都内联在 `_execute_tool_blocks()` 中；resume 路径没有现成 helper 可直接调用。

建议提取：

```python
async def _stream_confirmed_tool_blocks(
    self,
    state: LoopState,
    decisions_by_id: Mapping[str, ToolApprovalDecision],
) -> AsyncIterator[AgentEvent]: ...

async def _after_tool_results(
    self,
    state: LoopState,
) -> AsyncIterator[AgentEvent]: ...
```

`_stream_confirmed_tool_blocks()` 不再经过 permission gate。原因是用户刚刚对这些 exact tool calls 做了结构化确认。如果再次经过 gate，会再次产生 `WAIT_FOR_USER`。

但 hardline deny 不会走到这里，因为 hardline 在 permission gate 中返回 `DENY`，不会生成 `RequireUserConfirmEvent`。

### 3.6 HostSession owns pending approval

修改 `src/pulsara_agent/host/session.py`：

新增字段：

```python
pending_approval: PendingApproval | None = None
_suspended_state: LoopState | None = None
suspended_run_id: str | None = None
```

`run_turn()` / `stream_turn()` 完成后：

- 如果最终 state 是 `WAITING_USER`：
  - 从 state 构造 `PendingApproval`。
  - 保存 `_suspended_state = state`。
  - 在 `finally` 清空 `active_run_id` 之前捕获 `suspended_run_id = state.run_id`。
- 否则清空 pending approval。

两条入口拿 state 的方式不同，必须分别处理：

- `run_turn()` 从 `AgentRunResult.state` 读取最终状态。
- `stream_turn()` 没有 `AgentRunResult` 返回值；它自己创建局部 `state = new_state()` 并传给 `stream_task()`，该对象会被原地 mutate。因此 `async for` 耗尽后，应从 `stream_turn()` 的局部 `state` 读取 `WAITING_USER`、`pending_tool_calls` 和 `run_id`，并在 `finally` 清空 `active_run_id` 之前保存 `suspended_run_id`。

`active_run_id` 仍表示正在占用 `_run_lock` 的活跃执行；`suspended_run_id` 表示已经释放锁、等待用户 resolution 的挂起 run。不要依赖 `active_run_id` 保存暂停身份，因为现有 `finally` 会把它清成 `None`。

当 `pending_approval is not None` 时，普通 `run_turn()` / `stream_turn()` 应抛出 `HostSessionPendingApprovalError`。

守卫顺序要明确：`pending_approval is not None` 必须独立于 `_run_lock.locked()`，并放在 busy-lock 检查之前。暂停后 `_run_lock` 已释放，所以 `_run_lock.locked()` 会是 false；如果没有先检查 pending approval，普通新 turn 会漏过 busy 检查并错误进入新 run。

新增：

```python
def get_pending_approval(self) -> PendingApproval | None: ...

async def resolve_approval(self, resolution: ApprovalResolution) -> AgentRunResult: ...

async def stream_approval_resolution(
    self,
    resolution: ApprovalResolution,
) -> AsyncIterator[AgentEvent]: ...
```

resolution 完成后：

- 如果 state 再次进入 `WAITING_USER`，更新 pending approval。
- 如果 state finished/failed/aborted，清空 pending approval 和 suspended state。

`resolve_approval()` / `stream_approval_resolution()` 也必须使用 `_run_lock`。测试要证明：第一次 run 产生 suspended approval 后，锁已经释放，resolve 入口可以立刻获取锁并继续；实现不得把暂停建模成一个在锁内等待用户输入的 future。

### 3.7 HostCore facade

修改 `src/pulsara_agent/host/core.py`：

新增：

```python
async def get_pending_approval(self, host_session_id: str) -> PendingApproval | None: ...
async def resolve_approval(self, host_session_id: str, resolution: ApprovalResolution) -> AgentRunResult: ...
async def stream_approval_resolution(...): ...
```

V1 不需要 HTTP API，但 facade 要让未来 desktop/web 复用。

### 3.8 CLI REPL 最小支持

`host run` 是一次性命令，结束后会 close session。V1 不要求它支持审批恢复。若触发 pending approval，可以打印 pending summary，并提示该模式需要 REPL/desktop host。

`host repl` 可以加最小命令：

```text
:approval
:approve <approval_id> [tool_call_id|all]
:deny <approval_id> [tool_call_id|all]
:cancel-approval
```

V1 可以更小：

- 只支持 `:approval`
- `:approve`
- `:deny`

默认 all tool calls。

## 4. Event Semantics

### 4.1 Approve all

```text
RunStart
ReplyStart
assistant reply contains tool-call block
ReplyEnd
RequireUserConfirm
UserConfirmResult(confirmed=true)
ToolResultStart
ToolResultDelta
ToolResultEnd(success)
ReplyStart
...
RunEnd(final)
```

### 4.2 Deny all

```text
RequireUserConfirm
UserConfirmResult(confirmed=false)
ToolResultStart
ToolResultDelta("[TOOL_DENIED] ...")
ToolResultEnd(denied)
ReplyStart
assistant explains / asks alternative
RunEnd(final)
```

### 4.3 Partial approve

V1 may support partial approve because `ConfirmResult` is per tool call. Execution order:

1. emit one `UserConfirmResultEvent` with all decisions in original tool-call order.
2. process each original tool call deterministically: denied calls receive denied tool results, approved calls execute their saved snapshot.
3. preserve per-call identity by `tool_call_id`; tests should assert the reducer updates the matching call even when partial approval mixes approved and denied calls.

If this is too much for V1, the CLI can expose approve/deny all while internal schema still supports per-call resolution.

## 5. Permission Policy Changes

V1 policy resolver remains conservative:

- Keep rejecting `terminal_access=ask`.
- Keep rejecting executable `approval_policy=on_request`.
- Keep `read_only + on_request + off` allowed because it cannot produce approval.

After V1 is stable:

PR2 can lift:

- `terminal_access=ask`
- `workspace_guarded + on_request + off` for file writes
- `trusted_host + on_request + allow`

但 PR2 必须新增 real LLM smoke，因为 approval 会进入常规路径。

## 6. Memory Boundary

Approval resume 不写 memory graph。

允许：

- event log 记录 `RequireUserConfirmEvent` / `UserConfirmResultEvent`。
- runtime timeline 显示 permission item。
- future durable approval table 记录 request/audit。

不允许：

- `RememberPreferenceTool` 自动记录“用户批准了某命令”。
- memory reflection 把 approval note 当成用户偏好。
- governance 根据 approval resolution 创建 canonical Claim/Preference。

如果未来需要“记住类似命令不再问”，那是独立的 permission rule store，不是 memory graph。

## 7. Failure Handling

### 7.1 Session closed

如果 HostSession closed：

- pending approval invalid。
- suspended state discarded。
- UI 应显示 approval expired。

V1 in-memory 不承诺 host crash recovery。

### 7.2 Tool missing after approval

如果 approval 后 tool 不在 registry：

- emit `UserConfirmResultEvent`。
- 为该 tool call 生成 error tool result。
- 模型看到错误后继续。

### 7.3 Approved tool execution fails

和普通 tool failure 一致：

- `ToolResultEnd(state=ERROR)`
- append tool result message
- 走 tool error budget
- 模型可恢复或最终 failed

### 7.4 User submits new prompt while pending

V1 应拒绝普通新 prompt：

```text
host session has a pending approval; resolve or deny it before starting a new turn
```

不要把新 prompt 自动解释为 approve 或 deny。

### 7.5 Re-approval loops

resume 后如果后续模型再次发出 risky tool call，可以再次进入 pending approval。HostSession 更新 pending approval 即可。

## 8. Test Plan

### 8.1 Runtime unit tests

新增/扩展 `tests/test_agent_runtime_loop.py`：

1. risky terminal produces `RequireUserConfirmEvent` and no tool execution before approval。
2. waiting approval does not emit terminal `RunEnd` under suspended-run design。
3. approve emits `UserConfirmResultEvent` and executes original tool call。
4. approved call does not re-enter permission gate。
5. deny emits `UserConfirmResultEvent` and denied tool result。
6. partial approve preserves tool call order。
7. hardline deny still never creates approval。
8. `UserConfirmResultEvent` and resumed tool results use the suspended reply_id; `begin_next_turn()` happens only after tool results。
9. `_finalize_run()` / `on_turn_end` / `on_session_end` run exactly once at true terminal state, not at suspend and not on re-suspend。
10. run timeline status for a suspended approval is `waiting_user`, not `completed`。
11. ALLOW path and resume-approved path share the extracted post-gate execution helper, so budget/result/message accounting stays identical。

### 8.2 HostSession tests

新增/扩展 `tests/test_host_core.py`：

1. `run_turn()` stores pending approval and suspended state。
2. normal second `run_turn()` while pending raises `HostSessionPendingApprovalError`。
3. `resolve_approval()` continues the same task and clears pending approval after final。
4. if resumed run asks again, pending approval is replaced with the new one。
5. closing session invalidates pending approval。
6. suspended approval releases `_run_lock`; `resolve_approval()` can acquire it immediately。
7. `suspended_run_id` is captured before `active_run_id` is cleared in `finally`。
8. `approval_id` is host-minted, validates resolution, and is not required on current event schema。
9. `ConfirmResult.rules` can be carried in events but remains inert in message replay。

### 8.3 CLI tests

扩展 `tests/test_cli_host.py`：

1. `host run` that hits pending approval exits with clear pending summary。
2. `host repl :approval` prints pending request。
3. `host repl :approve` resumes。
4. `host repl :deny` resumes with denied result。

如果 REPL command 测试成本太高，V1 可以先覆盖 HostCore API，CLI 只做 smoke。

### 8.4 Real LLM smoke

只在 unit/integration 通过后加两条 lightweight real smoke：

1. model calls risky terminal command, runtime pauses, test approves, model continues and reports sentinel。
2. model calls risky terminal command, runtime pauses, test denies, model acknowledges denial and does not execute command。

注意：real smoke 需要使用无破坏命令但会触发 risky 规则，例如 `rm -rf build`，并在临时 workspace 内验证没有执行前置副作用。

### 8.5 Grounding checks completed before this plan

已直接核验：

1. hardline terminal / terminal_process input 在 `PolicyPermissionGate` 中返回 `PermissionDecisionKind.DENY`；`RequireUserConfirmEvent` 只在 `WAIT_FOR_USER` 分支发出，所以 hardline 不进入 approval resume。
2. `AgentRunResult` 包含 `state: LoopState`，`run_task()` 返回 `_run_result(state)`，HostSession 可以从 result 拿到挂起态。
3. `ToolCallBlock.input` 的执行路径通过 `json.loads(block.input or "{}")` 解析参数；未发现生产代码依赖原始 JSON 字符串逐字相等。重序列化会影响展示/摘要文本，但不影响执行匹配。

## 9. Suggested PR Breakdown

### PR1: Runtime suspended approval

- Add approval dataclasses。
- Update `_execute_tool_blocks()` to store ASKING blocks。
- Skip `_finalize_run()` on `WAITING_USER`。
- Extract post-gate tool execution helper shared by ALLOW and approved-resume paths。
- Add `stream_after_approval()` / `resume_after_approval()`。
- Fix runtime timeline so unresolved approval projects run status as `waiting_user`。
- Unit tests for approve/deny。

### PR2: HostSession approval facade

- Store pending approval and suspended state。
- Store `suspended_run_id` separately from `active_run_id`。
- Add `get_pending_approval()`。
- Add `resolve_approval()` / streaming variant。
- Block ordinary new turns while pending。
- Assert suspend releases `_run_lock` before approval resolution。
- HostCore pass-through。
- Host tests。

### PR3: Minimal CLI REPL support

- `:approval`
- `:approve`
- `:deny`
- Clear user-facing messages for `host run` one-shot limitation。

### PR4: Open policy combinations

Only after PR1-3:

- Allow `terminal_access=ask`。
- Allow executable `approval_policy=on_request` combinations。
- Expand tests and real LLM smoke。

### PR5: Durable approval store

Later, when desktop/web/mobile needs cross-process approval:

- Postgres/SQLite approval request table。
- pending request list API。
- request expiration。
- audit trail。
- optional remote approval integration。

## 10. Acceptance Criteria

V1 is complete when:

- A risky terminal command produces a pending approval rather than irreversible halt。
- User approval executes the exact original command snapshot。
- User denial produces denied tool result and lets model continue。
- Ordinary new user prompts are blocked while approval is pending。
- Suspended approval does not emit final `RunEnd` and is not projected as completed in runtime timeline。
- Approval resolution reuses the suspended `LoopState` and suspended `reply_id` until tool results are emitted。
- `_run_lock` is released while waiting for approval and reacquired by resolution。
- finalize / turn-end / session-end hooks run exactly once at true terminal state。
- Approval events are replayable in message reducer/timeline。
- No approval data is written to memory graph。
- Existing full-access/trusted-host non-risky terminal flows still pass。
- `terminal_access=ask` and broad `on_request` remain disabled until resume is tested。

## 11. Design Principle

Approval resume should feel like a pause in the same task, not a new conversation trick.

The user is not teaching memory. The user is deciding whether one concrete runtime action may proceed.
