# Pulsara Terminal Completion 后模型未继续与 Run Finalization 永久挂起事故分析

> 状态：FIX-0 至 FIX-6 及两轮 ownership/close post-review 已完成落地；主路径、故障矩阵与 PostgreSQL JSONB 回归已验证
> 事故日期：2026-08-05
> 文档性质：事故复盘、代码真值审计与修复 hard-cut 规格
> 适用范围：Python Runtime、RuntimeSession committed reducer、Terminal completion、Run finalization
> 不属于：Go TUI renderer 缺陷、Provider 模型故障、Context budget/compaction 故障

## 1. 摘要

本次事故的用户表象是：

- TUI 仍显示 `ready · controller`；
- Agent 已经调用多个工具，并在最后向一个交互式 terminal process 提交 `退出`；
- terminal process 随后确实正常退出；
- Agent 却没有给出预期的最终汇报；
- run 长时间保持 `running`，没有 `RunEndEvent` 或 `RunErrorEvent`。

经 durable ledger、PostgreSQL projection checkpoint、进程状态和代码路径联合审计，结论是：

1. 最终汇报并非已经生成后被 TUI 漏掉；生成最终汇报所需的下一次模型调用根本没有启动。
2. terminal completion event 已经 durable FULL commit。
3. terminal notification committed-reducer ingress 先原地推进 semantic store，随后在同一个 callback 内同步写 projection checkpoint。checkpoint successor 因 Python `tuple` 与 PostgreSQL JSONB round-trip 后 `list` 的表示差异，被错误判定为 validation-base drift。
4. callback 抛错时 semantic store 已经推进，但 reducer registration high-water 尚未推进；RuntimeSession 因而 latch `reconciliation_required`，形成 process-local partial application。
5. `EventWriteResult` 本来已经携带 `reducer_errors` 与 reconciliation outcome，但 `emit()`、`emit_many()`、`emit_from_thread()` 及其窄 adapter 会把它压扁为 committed event。terminal completion recorder 因而只记录了 event durability，无法观察或接管 reducer repair。
6. Agent 在 post-tool safe point 读取 context authority 时看到 reconciliation latch，抛出异常，未进入下一次 `ModelCallStart`。
7. activation driver 尝试转入失败终结，但普通 RunEnd 写入同样被 reconciliation gate 拒绝。
8. Run finalization repair service会重新抛出 `CancelledError`，但对其余 `BaseException` 无差别、无上限重试；同一个 hard latch 不会被该 task 改变，因此 run 永久停留在 `running`。

这是一个由「checkpoint storage identity 比较错误」触发、被「semantic fold 与 acceleration I/O 共用一个 reducer callback」「writer outcome 被便捷 API 压扁」和「finalization 对结构性故障无限重试」共同放大的 Python Runtime 主路径缺陷。

本事故的正确修复边界不是让 terminal completion 等待 checkpoint 健康，也不是给 RunEnd 增加通用 bypass。durable event、semantic reducer、acceleration checkpoint 和 run finalization 必须分别拥有自己的状态与 repair owner：checkpoint 失败不能反向否定已经 FULL 的 event，也不能继续污染 semantic reducer 的 live ingress。

## 2. 事故现场

### 2.1 Durable identity

本次诊断使用的现场 identity：

| 项目 | Identity |
|---|---|
| Runtime session | `runtime:bbfb559f958949c2951ce8d14752888d` |
| Run | `run:018df86d12814eac928e9bca91c8a56f` |
| Terminal process | `proc_b69d1686b5b7492884a2f413fd338eb1` |

这些 identity 仅用于本次本地事故追踪，不应进入长期协议或测试 fixture 的稳定 identity。

### 2.2 用户可见现象

TUI 在 Agent 调用多个 terminal tool 后重新显示 `ready`，但 transcript 中没有最终 assistant reply。界面底部仍允许 stop，说明 active run 并未 durable terminalize。

这里的 `ready` 只表示 Terminal client 已完成 attachment、snapshot 与 controller baseline，可以继续和 Python server 通信；它不是 `RunEnd` 的同义词，也不能证明 Agent run 已经结束。

### 2.3 Durable event 时间线

关键 durable timeline 如下：

| Sequence | Event / 结果 | 含义 |
|---:|---|---|
| 922 | 最后一次 model semantic text | 模型说明将发送“退出”并验证退出逻辑 |
| 934 | `ModelCallControlDispositionResolvedEvent(ACCEPTED)` | 本次 tool call 被控制面接纳 |
| 944–945 | 最后一个 ToolResult terminal batch | `terminal_process.submit("退出")` 返回；当时 process state 仍为 `running` |
| 946–947 | Tool accounting/settlement | 最后一次 tool batch 已完成 durable settlement |
| 948 | `TerminalProcessCompletedEvent` | process 正常退出，`status=success`、`exit_code=0`，输出以“再见”结束 |
| 949–950 | Physical/accounting events | completion 之后的物理结算完成 |

sequence 950 之后没有：

- 下一次 `ProjectionRequestedEvent`；
- 第 12 个 `ModelCallStartEvent`；
- `RunEndEvent`；
- `RunErrorEvent`。

这证明执行停在「最后 ToolResult 已 durable，下一次 model follow-up 尚未开始」的 post-tool 区间。

## 3. 预期行为与实际行为

### 3.1 预期行为

最后一次工具调用后的正常路径应当是：

```text
ToolResult terminal FULL
  -> post-tool hooks
  -> post-tool safe point
  -> live transcript projection / optional compaction preflight
  -> next ModelCallStart
  -> model reads terminal exit result
  -> final assistant reply
  -> accepted disposition
  -> ContextWindowClosed + RunEnd FULL
```

模型在 sequence 922 的正文只是调用工具前的意图说明，不是最终汇报。工具调用完成后仍需要下一次模型调用，才能基于 ToolResult 生成最终答复。

### 3.2 实际行为

```text
ToolResult terminal FULL
  -> background TerminalProcessCompleted FULL
  -> terminal notification semantic store mutates in-place
  -> same reducer callback synchronously persists checkpoint
  -> checkpoint successor CAS compares tuple against JSONB list
  -> false validation-base drift
  -> reducer callback exits before registration high-water advances
  -> RuntimeSession reconciliation latch
  -> post-tool context read fail closed
  -> activation driver transfers to failed finalization
  -> RunEnd write blocked by the same latch
  -> finalization service retries forever
```

这里必须保留一个关键区别：sequence 948 所在的物理 accounted batch 还包含后续 materialization/accounting events。生产路径通常通过 `_commit_accounted_one_shot_reduce_enqueue()` 和 `_reconcile_confirmed_attempt()` 收口，而不是只经过非 accounted writer 的逐 reducer error 收集分支。两条路径最终都会产生 `EventWriteResult`，但 accounted confirmation 路径目前只能从 reducer registration latch 合成较粗粒度的 `EventReconciliationRequired` error；修复不能只覆盖 `session.py:5053` 一条分支。

## 4. 已确认的直接根因：JSONB round-trip 后使用非 canonical Python equality

### 4.1 In-memory checkpoint payload 含 tuple

`HostIngressNotificationProjectionStore.checkpoint_payload()` 在以下字段中返回 tuple：

- `observation_events`
- `registration_events`
- `completion_events`
- 其他有序 collection carrier

代码位置：

- `src/pulsara_agent/runtime/terminal/notification.py:293`
- `src/pulsara_agent/runtime/terminal/notification.py:326`

这是合法的 process-local Python 表示；其 canonical JSON 语义仍然是 JSON array。

### 4.2 PostgreSQL JSONB 将 tuple 物理恢复为 list

checkpoint 写入 `runtime_projection_checkpoints.state_payload JSONB` 后，psycopg 读取出的 JSON array 必然是 Python `list`。

因此下面两个 payload 在 JSON 语义和 canonical bytes 上完全相同，但 Python 容器相等性不同：

```python
{"observation_events": (...,)}
{"observation_events": [...]}
```

### 4.3 RuntimeSession 保留了 pre-storage physical shape

checkpoint 成功写入后，RuntimeSession 将写入前的 `state_payload` 原样保存在 process-local checkpoint head：

- `src/pulsara_agent/runtime/session.py:1769`
- `src/pulsara_agent/runtime/session.py:1773`

所以 process-local successor candidate 的 `validation_base_state_payload` 仍含 tuple，而 PostgreSQL existing row 已经含 list。

更深一层的契约问题在 `RawRuntimeProjectionCheckpoint`：它虽然是 frozen dataclass，却直接持有两个可变的 `dict[str, Any]`，既没有递归不可变，也没有在构造时冻结 canonical bytes/normalized value：

- `src/pulsara_agent/primitives/stored_event.py:131`
- `src/pulsara_agent/primitives/stored_event.py:140`
- `src/pulsara_agent/primitives/stored_event.py:141`

因此当前类型既不能保证 retained reference 不被修改，也不能保证不同 storage adapter 看到同一种 JSON container shape。问题不是 PostgreSQL adapter 单点忘记调用 helper，而是 storage-facing carrier 没有拥有唯一的 canonical representation。

### 4.4 PostgreSQL checkpoint CAS 使用直接 dict equality

`PostgresEventLog.write_runtime_projection_checkpoint()` 在 successor CAS 中直接执行：

```python
checkpoint.validation_base_state_payload != dict(existing["state_payload"])
```

代码位置：

- `src/pulsara_agent/event_log/postgres.py:475`
- `src/pulsara_agent/event_log/postgres.py:481`
- `src/pulsara_agent/event_log/postgres.py:487`

同一个方法在 same-high-water compatible confirmation 分支中，也直接比较：

- `validation_base_state_payload`
- `state_payload`

代码位置：

- `src/pulsara_agent/event_log/postgres.py:453`
- `src/pulsara_agent/event_log/postgres.py:466`
- `src/pulsara_agent/event_log/postgres.py:468`

这与上层 RuntimeSession 已存在的 canonical comparison helper 不一致：

- `src/pulsara_agent/runtime/session.py:1558`

上层 helper 使用 `canonical_json_bytes(left) == canonical_json_bytes(right)`，能够正确消除 tuple/list 的 process-local 表示差异；PostgreSQL port 没有复用等价规则。

`InMemoryEventLog` 也不是可靠的反例。它把原始 `RawRuntimeProjectionCheckpoint` 对象保存在内存中，并使用 dataclass/direct dict equality：

- `src/pulsara_agent/event_log/in_memory.py:121`
- `src/pulsara_agent/event_log/in_memory.py:148`
- `src/pulsara_agent/event_log/in_memory.py:154`

因为它没有经历 JSONB round-trip，producer 写入的 tuple 会一直保持为 tuple，测试因此通过。换言之，当前 test double 与 PostgreSQL 对同一 storage protocol 实现了不同的 compatible-winner 语义。修复必须让两种 adapter 消费同一 canonical checkpoint codec，而不是只给 PostgreSQL 增加一个局部特判。

### 4.5 定向只读探针证据

使用数据库中 sequence 544 的 notification checkpoint，恢复 `HostIngressNotificationProjectionStore` 后重新 materialize 同一个 checkpoint payload，结果为：

```text
direct_equal False
canonical_equal True
first_drift_key observation_events tuple list
```

随后从 sequence 545 重放至 948：

```text
checkpoint 544
events 404
replay errors []
store through 948
projection through 948
```

这说明：

- terminal notification semantic reducer 本身可以 exact replay；
- completion event 及其 projection transition 是合法的；
- 失败不在 domain semantic；
- 失败发生在 checkpoint storage/CAS physical representation join。

该探针刻意停在completion event 948，用于证明completion semantic transition本身可重放。生产writer的accounted stored receipt还包含sequence 949–950。`HostIngressNotificationProjectionStore.apply_committed()`会对每个committed event推进store `through_sequence`，即使该event不改变notification projection正文：

- `src/pulsara_agent/runtime/terminal/notification.py:858`
- `src/pulsara_agent/runtime/terminal/notification.py:880`
- `src/pulsara_agent/runtime/terminal/notification.py:888`

因此生产checkpoint candidate应区分：

```text
checkpoint/store through_sequence = 950
notification projection semantic source_through_sequence = 948（允许小于store high-water）
completion semantic proof through_sequence = 948
```

repair candidate、ledger prefix与registration high-water必须绑定完整physical batch high-water 950，不能把探针的semantic stop 948误当成生产stored-batch边界。

数据库现场也与此完全吻合：

```text
terminal_notification_projection.v1 persisted checkpoint through_sequence = 544
live/offline exact projection semantic source_through_sequence = 948
physical ledger / accounted batch high-water = 950
```

因此错误预期为：

```text
ValueError: runtime projection checkpoint validation base drifted
```

## 5. 第一层放大器：reducer callback 非原子，typed write outcome 又被便捷 API 压扁

### 5.1 Event 已 FULL，但 reducer callback 只完成了一半

RuntimeSession 在 EventLog FULL 后才执行 process-local committed reducers。对 terminal notification，当前 ingress 不是一个纯 semantic fold：

```text
terminal_notification_store.apply_committed(events)
  -> synchronous _persist_terminal_notification_checkpoint()
  -> account coordinator
  -> monitor event channel
  -> optional listener
```

代码位置：

- `src/pulsara_agent/runtime/session.py:1654`
- `src/pulsara_agent/runtime/session.py:1657`
- `src/pulsara_agent/runtime/session.py:1675`
- `src/pulsara_agent/runtime/session.py:1676`
- `src/pulsara_agent/runtime/session.py:1677`

checkpoint CAS 在第二步抛错时，semantic store 已经推进至 sequence 948，但 account coordinator、event channel 与 listener 尚未运行。更重要的是，`_apply_live_receipt_to_reducer()` 只有在 ingress 正常返回后才推进 registration high-water：

- `src/pulsara_agent/runtime/session.py:2846`
- `src/pulsara_agent/runtime/session.py:2847`
- `src/pulsara_agent/runtime/session.py:2848`

事故现场因此不是简单的「event FULL，reducer entirely failed」，而是：

```text
durable event batch                         FULL
notification semantic store                advanced to 948
notification reducer registration          still behind
notification account/listener side effects not run
checkpoint                                 still at 544
RuntimeSession reducer latch                set
```

这意味着仅把 checkpoint write exception 改成 warning 仍然不安全。必须把 callback 拆成可验证的 semantic install 与独立 checkpoint maintenance；否则任何后置 I/O 或 listener exception 都可能再次制造相同的 partial application。

### 5.2 两条 writer 路径都能返回 typed result

`EventWriteResult` 已经是现有公开低层 carrier，并非本事故需要新发明的 DTO：

- `src/pulsara_agent/ports/event_write.py:84`
- `src/pulsara_agent/ports/event_write.py:91`
- `src/pulsara_agent/ports/event_write.py:93`

非 accounted 写入分支会直接捕获 reducer exception，保存 exact error type/message；accounted 或 idempotent-confirmed 分支则通过 `_reconcile_confirmed_attempt()` 与 `_catch_up_reducers()` 收口，再从 registration latch 合成 reducer error：

- `src/pulsara_agent/runtime/session.py:5053`
- `src/pulsara_agent/runtime/session.py:5069`
- `src/pulsara_agent/runtime/session.py:5150`
- `src/pulsara_agent/runtime/session.py:5467`
- `src/pulsara_agent/runtime/session.py:5484`
- `src/pulsara_agent/runtime/session.py:5572`

sequence 948 的 completion 所在生产路径还伴随 materialization/accounting events，因此修复和测试必须覆盖 accounted branch；只给非 accounted 分支补 assertion 不能闭环。

### 5.3 丢失 outcome 的不只 `emit_from_thread()`

后台 terminal completion 使用 `RuntimeThreadRecorder`，其 `__call__()` 最终调用 `emit_from_thread()`。该入口只取回 committed event：

```python
def emit_from_thread(self, event):
    result = self.write_events_from_thread((event,))
    return next(item for item in result.committed_events if item.id == event.id)
```

代码位置：

- `src/pulsara_agent/runtime/session.py:193`
- `src/pulsara_agent/runtime/session.py:197`
- `src/pulsara_agent/runtime/session.py:5901`

但同样的 outcome collapse 也存在于 async `emit()`、`emit_many()` 以及 `RuntimeSessionRunLedgerPort`：

- `src/pulsara_agent/runtime/session.py:5869`
- `src/pulsara_agent/runtime/session.py:5880`
- `src/pulsara_agent/runtime/session_run_capabilities.py:56`
- `src/pulsara_agent/runtime/session_run_capabilities.py:59`

async convenience API 目前只将 critical publication failure 提升为异常，同样不消费 `reducer_errors` 或 `reconciliation_required`。此外，memory governance、Host plan audit 等 production adapter 也会直接取 `.committed_events`。因此这不是一个 thread-only bug，而是「完整 writer outcome 与 producer-facing acceptance policy 之间缺少唯一 classifier」的系统性缺口。

terminal completion recorder 在 `record_event(event)` 返回一个 `AgentEvent` 后，将其放进 `completion_recorded_event` 并把 recording state 标记为 `RECORDED`：

- `src/pulsara_agent/runtime/terminal/process.py:1285`
- `src/pulsara_agent/runtime/terminal/process.py:1292`
- `src/pulsara_agent/runtime/terminal/process.py:1294`

这里的 `RECORDED` 应只表示「stable completion event 已 durable FULL」，不应偷偷等价于「所有 semantic reducers 与 acceleration checkpoint 均健康」。修复后的 terminal process owner也不应拥有 notification checkpoint 重试；它只需确认 RuntimeSession 已接受 reducer/checkpoint repair handoff。checkpoint repair 必须继续由 RuntimeSession session-scoped owner独占。

## 6. 第二层放大器：post-tool context fail closed 阻止下一次模型调用

最后一个 ToolResult 完成后，Agent 主路径执行：

- `src/pulsara_agent/runtime/agent.py:4496`
- `src/pulsara_agent/runtime/agent.py:4507`
- `src/pulsara_agent/runtime/agent.py:5353`

`_continue_after_tool_before_followup()` 首先执行 mid-turn compaction preflight。即使最终不需要 compaction，它也会准备一次 live transcript projection：

- `src/pulsara_agent/runtime/agent.py:5313`
- `src/pulsara_agent/runtime/agent.py:5322`

live authority read 在入口和 I/O 完成后都检查 RuntimeSession reconciliation：

- `src/pulsara_agent/runtime/context_input/live.py:1851`
- `src/pulsara_agent/runtime/context_input/live.py:2098`

sequence 948 的 reducer failure 已经 latch reconciliation，因此这里抛出 `ContextEventSliceError`。执行尚未到达下一轮 model loop，自然不会出现：

- 新 `ProjectionRequestedEvent`
- 第 12 个 `ModelCallStartEvent`
- 最终 assistant reply

## 7. 第三层放大器：失败 RunEnd 被 reconciliation gate 拒绝

activation driver 会捕获 producer 的异常，并准备一个 stable failed RunEnd candidate：

- `src/pulsara_agent/runtime/run_execution/service.py:1046`
- `src/pulsara_agent/runtime/run_execution/service.py:1066`
- `src/pulsara_agent/runtime/run_execution/service.py:1117`
- `src/pulsara_agent/runtime/run_execution/service.py:1132`

这一步的 ownership 方向是正确的：异常不应直接遗留 active segment。

但 RunEnd 仍走普通 RuntimeSession event writer。writer 在任何 hard reconciliation latch 存在时，于写入前直接拒绝：

- `src/pulsara_agent/runtime/session.py:4934`
- `src/pulsara_agent/runtime/session.py:4946`
- `src/pulsara_agent/runtime/session.py:4952`

因此 failed `ContextWindowClosed + RunEnd` batch 无法 commit。

RuntimeSession 已有一个低层同步 rebuild seam：`reconcile_committed_reducer(reducer_id)` 会在 writer lock 下 reset reducer、从 sequence 1 分页 fold 到当前 high-water，并只在成功后清除 registration latch：

- `src/pulsara_agent/runtime/session.py:2773`
- `src/pulsara_agent/runtime/session.py:2781`
- `src/pulsara_agent/runtime/session.py:2783`
- `src/pulsara_agent/runtime/session.py:2794`

但它目前只有测试调用，没有 production service owner；它使用固定 30 秒 deadline、由 caller 同步驱动，也没有 attempt handle、typed outcome、close drain 或 checkpoint-head integration。notification rebuild callback只重建 semantic store与account coordinator，并不会推进/确认 `_terminal_notification_checkpoint_head`。所以准确结论不是「完全没有 repair API」，而是「已有 bounded rebuild primitive，但没有能够编排 semantic rebuild、checkpoint adoption、latch clearing 与 stable RunEnd resume 的 session-owned live repair service」。

## 8. 第四层放大器：finalization 无限重试且没有可观察状态

`RunFinalizationService._execute_repair()` 当前逻辑为：

```python
while True:
    try:
        ...
    except asyncio.CancelledError:
        raise
    except BaseException:
        state = "retry_wait"
        await sleep(backoff <= 0.25s)
```

代码位置：

- `src/pulsara_agent/runtime/run_execution/finalization.py:144`
- `src/pulsara_agent/runtime/run_execution/finalization.py:151`
- `src/pulsara_agent/runtime/run_execution/finalization.py:159`
- `src/pulsara_agent/runtime/run_execution/finalization.py:168`

问题包括：

1. `CancelledError` 已单独重新抛出，但其余 `BaseException` 不区分 transient、structural、reconciliation-required。
2. 每次 retry 没有独立 attempt generation/deadline，也没有由 repair completion 唤醒的等待状态。
3. 没有稳定的 last-error fact 或 operational diagnostic。
4. 没有进入 `reconciliation_required` terminal state 的确定条件。
5. 同一 writer latch 永远不会自行变化，所以重试没有任何 liveness 可能。
6. run 没有 `RunEnd`，外部只能看到永久 `running`。

这解释了为什么进程既没有忙于 Provider/SQL，也没有退出：finalization task 只是以最多 250ms 的 backoff 反复尝试一个必然被 gate 拒绝的操作。

这里不应简单采用「超过 N 次就丢弃 candidate」作为修复。RunEnd candidate 已由 stable `RunFinalizationOwner` 拥有，caller cancellation 也应只 detach。正确的 liveness 是：transient physical attempt 有界；stable candidate 不丢失；结构性失败停止 busy retry并等待 exact repair receipt；Host close 到达共享 deadline时报告 blocked，而不是把 run伪装为成功或擅自生成第二个 terminal fact。

## 9. 已排除的其他假设

### 9.1 不是 TUI 漏显示最终回复

ledger 中不存在最终回复对应的第 12 次 Model lifecycle。TUI 没有可显示的 canonical assistant terminal document。

### 9.2 不是 Provider 卡住

现场没有 active provider network operation，也没有 active model reservation；最后一个 `ModelCallEnd` 已经完成。

### 9.3 不是 token 或 rollout budget 耗尽

Inspector 显示 rollout/account 仍有充足余额，没有预算 latch，也没有 physical model reservation 遗留。

### 9.4 不是 compaction 正在运行

没有 compaction Started/Failed/Completed event。当前输入规模也低于 resolved compaction threshold。执行在 compaction preflight 的 authority preparation入口即被 reconciliation guard 拦截。

### 9.5 不是 terminal process 尚未退出

`TerminalProcessCompletedEvent` 明确记录：

- `status=success`
- `exit_code=0`
- output 以退出提示结束

### 9.6 不是 notification semantic reducer 不可 replay

从 durable checkpoint 544 到 event 948 的逐事件离线重放无错误，并得到 exact projection high-water 948。

## 10. 影响范围

### 10.1 已确认影响

- 含 JSON array/tuple 的 runtime projection checkpoint，在第一次 successor CAS 时可能被错误判为 drift。
- terminal process completion 可在 event 已 durable 后 latch RuntimeSession reconciliation。
- 任意紧随其后的 Agent context read 会 fail closed。
- Run finalization 无法越过同一个 latch，形成永久挂起。

### 10.2 潜在更广影响

问题位于通用 `write_runtime_projection_checkpoint()`，而非 notification 专用 PostgreSQL port。因此所有复用该接口、且 process-local payload shape 与 JSONB hydrated shape 不完全相同的 projection，都应纳入审计。

至少应检查：

- terminal notification projection；
- terminal monitor projection；
- terminal presentation history projection；
- prompt queue projection；
- 未来新增的 runtime projection checkpoint。

审计必须同时覆盖 producer、carrier、in-memory adapter 和 PostgreSQL adapter。只要任一方仍用 Python object equality，test double 就可能继续隐藏 storage round-trip drift。

### 10.3 数据库迁移判断

这不是通过重置 PostgreSQL 才能解决的迁移问题。

重置数据库只能暂时删除已经形成的 checkpoint；新的 checkpoint 第一次经过 JSONB round-trip 后，下一次 successor CAS 仍会遇到同样的 tuple/list drift。

现有 durable event ledger 本身没有发现 semantic corruption。原则上应能通过代码修复和 bounded projection checkpoint repair 恢复，而不是要求清库。

就 tuple/list 根因本身而言，不需要 PostgreSQL schema migration：现有 JSONB row与其 fingerprint仍能按 canonical JSON语义重绑。若实现选择新增 durable repair-attempt、diagnostic 或 checkpoint outbox relation，则那是新 owner 的独立 schema subcut，不能笼统写成「本事故绝不需要 migration」。推荐 V1 复用现有 `runtime_projection_checkpoints` row，把 repair attempt保持为 session-owned process state，并依赖 reopen时从 checkpoint + bounded ledger delta确定性重建。

## 11. 修复 hard-cut 设计

### 11.1 唯一 owner 与不可违反的不变量

| Authority / operation | 唯一 owner | 不得承担 |
|---|---|---|
| stored event batch与 canonical sequence | EventLog / RuntimeSession writer | projection checkpoint健康度 |
| committed semantic fold与 reducer high-water | RuntimeSession reducer registration + domain store | PostgreSQL checkpoint I/O、UI/notification delivery |
| runtime projection checkpoint candidate、retry、confirmation、head | session-scoped checkpoint maintenance service | 重放 physical tool、修改 durable event outcome |
| terminal process completion physical lifecycle | terminal process owner | reducer rebuild、checkpoint CAS |
| stable RunEnd candidate与 run completion | `RunFinalizationOwner` | 清除任意 RuntimeSession latch、通用 maintenance bypass |

必须冻结以下不变量：

1. EventLog FULL 后，event durability不可被后续 reducer、checkpoint或publication failure改写。
2. semantic reducer只有在完整的 process-local transition安装后才能推进 registration high-water；不能留下「store已变、registration未变」的半状态。
3. acceleration checkpoint failure不进入全局 mutation/context hard latch。只有 semantic fold不一致、ledger continuity未知或 checkpoint冲突证明 semantic authority不可重建时，才能 latch对应 hard reconciliation。
4. checkpoint maintenance lag必须在越过 bounded reopen能力之前通过自己的 soft/hard admission policy收口，不能等到不可恢复后再污染整个 RuntimeSession。
5. terminal completion在 event FULL 后绝不重新生成新 event；RunEnd也必须复用 `RunStartEvent.terminal_run_end_event_id` 对应的 stable identity。
6. caller cancellation只 detach waiter。checkpoint和finalization的 physical attempt由各自service-owned task负责退出、重试或交给reconciliation。

### 11.2 Canonical JSON checkpoint carrier

局部把 notification tuple改成list不够。需要在 `primitives.stored_event` 或更低的 storage protocol owner中定义唯一 carrier，例如：

```text
CanonicalJsonObjectCarrier
  codec_id
  codec_version
  canonical_utf8
  canonical_payload_fingerprint

RawRuntimeProjectionCheckpoint
  ...
  validation_base_state: CanonicalJsonObjectCarrier
  state: CanonicalJsonObjectCarrier
```

唯一 factory负责：

- 接受 producer JSON-like value；
- 拒绝非字符串 key、非有限浮点、不可编码值与非 object根；
- 一次生成 canonical UTF-8 bytes与 fingerprint；
- 如 adapter需要 Python value，只能由 canonical bytes解码得到，不得保留 producer mutable dict；
- 禁止调用方直接构造或在构造后修改 nested payload。

现有 `pulsara_agent.primitives._context_base.canonical_json_bytes()` 已正确把 tuple/list统一为JSON array、排序object key并拒绝non-finite float，可作为codec实现真源；本修复不应再复制第四套canonical JSON函数。新carrier factory应调用该低层实现，并冻结自己的codec ID/version与domain fingerprint。

比较规则必须为：

```text
same semantic JSON
  iff codec binding相同
  and canonical_utf8 byte-equal
  and payload fingerprint exact
```

PostgreSQL写入时从 canonical bytes解码一次交给 `Jsonb`；读取JSONB后重新canonicalize并验证。InMemory adapter也必须保存/比较同一个 carrier，不能继续依靠producer object equality。same-high-water confirmation、successor validation-base CAS、RuntimeSession process-local head和fingerprint factory全部消费同一carrier。

为了兼容现有row，V1可以在read factory中把已有JSONB payload提升为新carrier；只要原有 `payload_fingerprint` 按同一 canonical JSON字段覆盖计算，就不需要改表或重写row。

### 11.3 Semantic fold 与 checkpoint I/O 的物理拆分

terminal notification ingress必须从：

```text
mutate store
  -> write checkpoint synchronously
  -> mutate account / notify observers
```

改为：

```text
prepare immutable semantic transition
  -> validate store/account base identities
  -> atomically install semantic store + semantic account head
  -> return CommittedReducerFoldReceipt
RuntimeSession advances reducer registration high-water
  -> offer checkpoint semantic head to maintenance owner (non-blocking)
  -> offer operational observation/listener delivery (no-fail or separately owned)
```

`CommittedReducerFoldReceipt`至少绑定：

```text
reducer_id
base_through_sequence
resulting_through_sequence
source_stored_batch_ordered_join_fingerprint | restored_range_fingerprint
base_semantic_state_fingerprint
resulting_semantic_state_fingerprint
checkpoint_state: CanonicalJsonObjectCarrier | None
fold_receipt_fingerprint
```

对现有mutable store，允许先用「clone/build next state -> validate -> single install」作为过渡，但禁止在可能抛错的checkpoint SQL之前原地修改live store。notification durable account state已经包含在domain store中，应与projection一起install。`TerminalNotificationAccountCoordinator._owners` 则是process-local reservation/lease owner，不是第二份semantic account；其 `on_committed()` 应改成基于fold receipt的幂等post-confirm handoff，失败由独立process-owner repair收口，不重复semantic fold。event channel、listener与UI同样属于后置observation，不得让其异常回滚或latch semantic reducer。

generic committed-reducer port不应硬编码terminal checkpoint。RuntimeSession在成功fold receipt之后，把可选的checkpoint semantic head交给注册时绑定的窄 `RuntimeProjectionCheckpointMaintenancePort`。

### 11.4 Session-owned checkpoint maintenance service

为terminal notification和monitor建立同一通用service、不同projection owner。每个projection kind最多一个active stable candidate和一个coalesced dirty successor：

```text
CLEAN
  -> DIRTY
  -> CANDIDATE_READY
  -> WRITING(g)
       -> FULL -> CLEAN | CANDIDATE_READY(successor)
       -> NONE -> RETRY_WAIT
       -> UNKNOWN -> CONFIRMING
       -> CONFLICT -> RECONCILIATION_REQUIRED
RETRY_WAIT -- new physical attempt/deadline --> WRITING(g+1)
CONFIRMING -> FULL | NONE | RECONCILIATION_REQUIRED
* -> CLOSING -> CLOSED | CLOSE_BLOCKED
```

stable candidate覆盖：

```text
projection kind/schema binding
base checkpoint sequence + canonical state fingerprint
target semantic through-sequence
target ledger-prefix identity
target canonical state carrier
source semantic-fold receipt accumulator
stable candidate fingerprint
```

physical write attempt另有generation、operation ID与absolute deadline；这些字段不得进入stable candidate。新committed folds在candidate I/O期间继续推进semantic store，只更新dirty successor，不修改在途candidate。

storage port应从当前 `None/raise` 改为closed outcome并提供exact confirmation read：

| Outcome | 行为 |
|---|---|
| `FULL` | 安装storage返回/确认的normalized head；只消费candidate覆盖的dirty prefix |
| `NONE` | 保留同一candidate，按not-before用新physical generation重试 |
| `UNKNOWN` | exact read `(session, projection_kind)`，比较canonical candidate/winner |
| compatible same/newer winner | 验证schema、ledger prefix与semantic lineage后adopt |
| incompatible winner | 只latch该checkpoint owner；若同时证明semantic state不可重建，再升级为reducer hard reconciliation |

checkpoint owner必须复用session的bounded blocking-I/O service或独立executor，不能在EventLog writer critical path等待SQL。PostgreSQL checkout还必须使用不会占尽EventLog critical-writer reserve的checkpoint-maintenance lane；当前 `write_runtime_projection_checkpoint()` 使用 `event_log.postgres_pool.PostgresConnectionLane.CRITICAL_WRITE`，该调用面需要进入hard cut。checkpoint transaction仍可锁同一session row执行CAS，但排队/容量不能让acceleration maintenance饿死durable event writer。close先停止新candidate admission，再在共享deadline内drain真实physical operation；超时返回close-blocked。reopen不恢复process-localattempt，而是读取validated checkpoint，从ledger delta重建当前semantic head，再确定性形成新的candidate。

现有 prompt queue与presentation checkpoint service已经提供background worker、stable candidate和close drain的局部模式，可以复用其ownership形状，但不能直接复用其domain DTO。

### 11.5 Writer outcome：保留现有 `EventWriteResult`，删除生产压扁路径

不再在本事故中发明第二个完整writer result。`EventWriteResult`继续是RuntimeSession低层唯一结果，新增一个中央、closed acceptance classifier，把多维结果映射为producer可消费的窄receipt：

```text
CommittedEventSettlementReceipt
  stored_batch_receipt_identity
  requested_event_references
  durability = FULL
  semantic_fold = HEALTHY | REPAIR_OWNER_INSTALLED
  checkpoint_handoff = NOT_APPLICABLE | ACCEPTED
  publication = COMPLETED | ENQUEUED | UNAVAILABLE
  settlement_fingerprint
```

注意：checkpoint physical状态不应同步塞回event writer result。writer只需证明成功semantic fold后，checkpoint semantic head已被maintenance owner接受；之后的checkpoint FULL/NONE/UNKNOWN由maintenance owner独立处理。

classifier不得只读取当前聚合布尔 `EventWriteResult.reconciliation_required`。该值来自RuntimeSession多个ledger/context/memory/publication/reducer latch的并集；semantic fold必须由本批次的typed reducer receipts/errors判断，publication也必须使用独立字段。必要时先扩充 `EventWriteResult` 为closed per-domain settlement，再由中央classifier降窄，不能让producer按字符串或布尔重新猜测。

`RuntimeThreadRecorder`应返回面向terminal process的窄receipt，而不是裸 `AgentEvent`。completion state按正交维度表达：

```text
event_record_state = PENDING | RECORDING | DURABLE_FULL
semantic_settlement = HEALTHY | REPAIR_OWNED
```

一旦durable FULL，任何reducer问题都不得把event退回PENDING或触发新的physical event execution。若semantic repair owner已成功安装，terminal process可以退休自己的recording operation；它不等待checkpoint落盘。

同时审计并hard-cut以下production convenience路径：

- `RuntimeSession.emit()`；
- `RuntimeSession.emit_many()`；
- `RuntimeSession.emit_from_thread()`；
- `RuntimeSessionRunLedgerPort.emit/emit_many()`；
- 直接取 `write_events_from_thread(...).committed_events` 的adapter。

每个caller必须显式选择一个closed acceptance policy。仅测试需要的unsafe collapsing helper应放入tests/support，不能继续作为production RuntimeSession API。

### 11.6 Reducer live repair service

现有 `reconcile_committed_reducer()`保留为内部primitive，但不能继续作为生产caller同步调用的完整方案。新增session-owned `CommittedReducerRepairService`：

```text
IDLE
  -> INSTALLED(stable repair plan)
  -> REBUILDING(g)
  -> VERIFYING
       -> REPAIRED
       -> RETRY_WAIT
       -> RECONCILIATION_REQUIRED
```

repair plan绑定reducer ID、失败前registration high-water、目标ledger high-water、last error code、recovery-base identity和plan fingerprint。terminal notification的优先恢复算法为：

1. 读取并验证现有checkpoint及ledger prefix；
2. 从checkpoint恢复新的semantic store/account实例；
3. 使用`read_joined_raw_range()`按256 events / 16 MiB page fold bounded delta；
4. 在writer lock内验证ledger high-water与repair plan仍compatible；
5. 原子替换domain store/account，推进registration high-water；
6. 仅清除该registration latch并重新计算RuntimeSession aggregate latch；
7. 把最新semantic head交给checkpoint maintenance owner；
8. 唤醒等待该exact repair receipt的RunFinalizationOwner。

当前 `reconcile_committed_reducer()`从sequence 1一直fold到head，虽然每页有界，但总工作量没有上界。terminal projection repair必须优先使用validated checkpoint + bounded delta；只有privileged offline doctor可以做无界full-ledger fold。若delta超过online hard bound，进入typed `OFFLINE_REPAIR_REQUIRED`，不能在event loop或writer lock里持续扫描。

repair attempt的caller取消只detach；physical rebuild在session-owned task中运行。close必须drain；reopen可从durable checkpoint与ledger重新派生，不需要持久化process-local attempt row。

### 11.7 Finalization liveness：等待repair，不轮询结构性故障

`RunFinalizationOwner`新增明确状态：

```text
CANDIDATE_FROZEN
  -> WRITING(g)
  -> RETRY_WAIT
  -> WAITING_REDUCER_REPAIR
  -> RECONCILIATION_REQUIRED
  -> RUN_END_FULL_OUTPUT_PENDING
  -> COMPLETED
```

分类矩阵：

| Failure / outcome | 行为 |
|---|---|
| `EventWriteConflict` | 沿用现有bounded replan；stable RunEnd ID不变 |
| commit `NONE` / transient I/O | 保留candidate；新attempt generation与deadline；bounded immediate burst后进入not-before等待 |
| compatible `FULL` | exact adopt并进入output materialization |
| commit `UNKNOWN` | 安装现有run-event reconciliation owner，停止普通write |
| exact committed-reducer latch | 绑定`CommittedReducerRepairHandle`，进入`WAITING_REDUCER_REPAIR`；repair receipt到达前不重试writer |
| structural mismatch / offline repair required | 进入`RECONCILIATION_REQUIRED`并暴露diagnostic |
| waiter cancellation | detach；service-owned physical owner继续收口 |
| Host close deadline | 返回close-blocked；不丢candidate、不伪装terminal |

不冻结「stable candidate最多N次后丢弃」。应该有界的是单次physical deadline、连续immediate retry burst、resident diagnostics与close等待；stable terminal authority必须保留到FULL、explicit reconciliation或session teardown blocked。

普通RunEnd writer继续遵守hard reconciliation gate。禁止添加可提交任意event的terminal-maintenance bypass。合法顺序只能是：repair exact reducer authority -> clear exact latch -> 重驱动同一RunEnd candidate。

Host close必须调整相对顺序。Repair admission不能只覆盖当前可见的RunEnd：tool terminal、governance、compaction、subagent cancellation、MCP closure以及provider-input generation close都可能在teardown后段提交最后一个durable FULL。冻结顺序如下：

1. 停止会产生新terminal completion的admission，并drain已准入completion recorder；
2. terminal notification、active/suspended run与finalization先收口，但matching `CommittedReducerRepairService`在整个阶段保持可准入、可执行；
3. 停止并drain tool、governance、compaction、subagent、MCP与provider-input close等其余EventLog producer；
4. producer全部退出后，依次drain physical writer、reducer repair与post-fold handoff，并重新join一次writer/repair链形成固定点；
5. 关闭repair与post-fold admission；最后drain acceleration checkpoint/presentation owner，再释放EventLog、connection和executor dependency；
6. 任一shared deadline耗尽都返回close-blocked，不跨过仍存活的physical owner。

这是一组dependency ordering约束，不要求把所有Host close步骤串成一个超长锁区，也不允许finalization在等待repair时占用writer lock。

### 11.8 现场 session 的恢复算法

进程仍存活时：

1. 为 `terminal_notification:<runtime_session_id>` 安装唯一repair attempt；
2. exact读取checkpoint 544并canonical rebind；
3. 从545分页fold至当前ledger high-water，不直接信任已经partial-mutated的live store；
4. 原子安装新semantic store/account并推进registration；
5. 清除该reducer latch，形成最新checkpoint dirty head；
6. 唤醒被冻结的failed RunEnd candidate；
7. checkpoint maintenance在后台推进至最新semantic high-water。

进程已重启时，旧process-local candidate对象不应恢复，但stable RunEnd event ID仍由`RunStartEvent.terminal_run_end_event_id`提供。现有 `runtime/run_execution/recovery.py` 已用该ID重建 `RunFinalizationOwner`并支持freeze/confirm recovered terminal batch。恢复流程应先完成RuntimeSession semantic repair，再按现有recovery policy构造「interrupted/recovered failure」terminal batch；不得假装重建事故前未持久化的exact working-state error message。

### 11.9 Operational diagnostics

Host/Inspector至少暴露：

- active run phase；
- finalization state与stable RunEnd ID；
- current physical attempt generation、not-before与first/last failure time；
- reducer repair attempt ID/state/target high-water；
- checkpoint kind、base/target high-water、candidate fingerprint与physical state；
- bounded sanitized last error code/message；
- `controller ready`、`run active`、`run blocked`、`run terminal`的独立状态。

TUI可以显示：

```text
controller ready · run blocked · reducer repair
```

但该状态必须来自Python operational projection，不能由Go根据长时间无event自行推断。diagnostic不得包含terminal原始输出、secret或无界traceback。

### 11.10 明确修改面与 architecture guards

预计production修改面至少包括：

- `src/pulsara_agent/primitives/stored_event.py`
- `src/pulsara_agent/event_log/in_memory.py`
- `src/pulsara_agent/event_log/postgres.py`
- `src/pulsara_agent/event_log/postgres_pool.py`
- `src/pulsara_agent/event_log/postgres_prompt_queue.py`
- `src/pulsara_agent/event_log/protocol.py`
- `src/pulsara_agent/ports/event_write.py`
- `src/pulsara_agent/ports/prompt_queue.py`
- `src/pulsara_agent/ports/stored_event.py`
- `src/pulsara_agent/runtime/session.py`
- `src/pulsara_agent/runtime/session_run_capabilities.py`
- `src/pulsara_agent/runtime/terminal/notification.py`
- `src/pulsara_agent/runtime/terminal/monitor.py`
- `src/pulsara_agent/runtime/terminal/process.py`
- `src/pulsara_agent/runtime/terminal_application/prompt_queue_checkpoint.py`
- `src/pulsara_agent/runtime/terminal_presentation/history_checkpoint.py`
- `src/pulsara_agent/runtime/run_execution/owner.py`
- `src/pulsara_agent/runtime/run_execution/finalization.py`
- `src/pulsara_agent/runtime/run_execution/service.py`
- `src/pulsara_agent/host/session.py`
- `src/pulsara_agent/storage/prompt_queue_bootstrap.py`
- Inspector与terminal operational projection对应模块

implementation前应建立以下AST/contract gates：

1. `RawRuntimeProjectionCheckpoint`不再声明 `dict[str, Any]` payload字段。
2. InMemory/PostgreSQL checkpoint adapter只能调用central canonical codec，不得对state payload使用直接 `==`/`!=`。
3. terminal notification/monitor committed reducer callback不得调用 `write_runtime_projection_checkpoint()` 或其他blocking storage API。
4. production代码不得调用返回裸event的 `emit_from_thread()`，也不得直接用 `.committed_events` 抹去 `EventWriteResult` 其他维度；每个例外必须是closed adapter并有exact architecture allowlist。
5. `RunFinalizationService`不得存在无条件 `while True + except BaseException + sleep` writer重试；retry必须由typed outcome或repair receipt推进。
6. Host close在finalization repair依赖未drain时不得先释放event writer、PostgreSQL provider、executor或RuntimeSession reducer registration。

## 12. 推荐实施顺序

### FIX-0：先冻结回归与现场向量（已完成）

- 将 tuple/list JSONB round-trip 变成最小 deterministic fixture。
- 固定 checkpoint 544-like base和 completion successor。
- 证明修复前稳定命中 validation-base drift。
- 同一fixture必须同时跑 InMemory 与 PostgreSQL，证明当前adapter语义分叉。
- 冻结 accounted completion batch（business completion + accounting events）的stored receipt与reducer error路径。

### FIX-1：canonical JSON checkpoint carrier hard cut（已完成）

- 用递归不可变的canonical carrier替换 `RawRuntimeProjectionCheckpoint` 内两个mutable dict。
- 收敛 storage normalization/equality/fingerprint owner。
- 同时修复 same-high-water 与 successor comparison。
- 同步修改 InMemory 与 PostgreSQL adapter。
- 审计 notification、monitor、prompt queue、presentation history全部调用方。
- 本阶段应独立全绿，并能直接阻止事故复现；但它不是最终ownership修复。

### FIX-2：semantic reducer / acceleration checkpoint hard cut（已完成）

- terminal notification/monitor reducer只安装semantic state并返回fold receipt。
- checkpoint SQL移出committed writer critical path。
- event channel/listener异常不再latch semantic reducer。
- 安装session-owned checkpoint maintenance service、stable candidate、typed confirmation与close drain。
- 增加soft/hard lag policy，确保online reopen delta持续有界。

### FIX-3：writer outcome acceptance hard cut（已完成）

- 保留现有 `EventWriteResult` 为低层真源。
- 新增中央closed acceptance classifier和窄producer receipt。
- 删除production `emit/emit_many/emit_from_thread` outcome collapse，逐caller迁移。
- terminal completion recorder使用 `DURABLE_FULL + semantic settlement` 正交状态。

### FIX-4：committed reducer live repair owner（已完成）

- 把现有同步 `reconcile_committed_reducer()`降为内部primitive。
- 建立checkpoint-based bounded rebuild、stable plan、typed result与close drain。
- semantic repair成功后独立唤醒checkpoint owner与RunEnd owner。
- 增加live repair、restart repair和offline-doctor边界。

### FIX-5：finalization liveness hard cut（已完成）

- 删除无界 `except BaseException -> retry forever`。
- 增加attempt generation/deadline、not-before与`WAITING_REDUCER_REPAIR`。
- structural failure停止轮询并进入typed blocked/reconciliation outcome。
- 确保exact repair完成后事件驱动地继续同一RunEnd candidate。
- close报告blocked但不丢stable candidate。

### FIX-6：现场恢复与真实 post-tool dogfood（已完成）

- 在修复后代码上恢复事故session或其脱敏数据库fixture；
- 运行交互式 terminal process；
- 多次 `submit/wait`；
- 最后 `submit("退出")`；
- completion 与 post-tool safe point 人为交错；
- 验证下一次模型调用、最终回复和 RunEnd。

## 13. 必须新增的测试

### 13.1 Storage contract

- tuple/list canonical-equivalent payload可 compatible-confirm。
- same-high-water canonical-equivalent payload不冲突。
- successor validation base canonical-equivalent时可推进。
- semantic不同但 shape相似的 payload仍 fail closed。
- fingerprint与 canonical equality 使用同一 covered representation。
- `RawRuntimeProjectionCheckpoint` 不再暴露mutable nested dict retained reference。
- InMemory/PostgreSQL对同一golden vector返回完全相同的FULL/NONE/CONFLICT分类。
- existing JSONB row可提升为新canonical carrier，无需row rewrite。
- 非字符串key、NaN/Infinity、非object根、不可编码值全部在candidate factory fail closed。

### 13.2 Reducer与checkpoint ownership

- semantic fold FULL后registration high-water与domain store high-water同时推进。
- checkpoint SQL failure不会留下store advanced/registration behind。
- checkpoint NONE/UNKNOWN/CONFLICT不会latch context read，除非semantic reconstruction proof也失败。
- checkpoint candidate在新events到达时保持byte-stable，successor只消费dirty suffix。
- notification reservation coordinator handoff失败时不重复semantic fold，并能按fold receipt幂等恢复process owner。
- listener/event-channel failure不进入committed reducer hard latch。
- checkpoint owner close等待真实physical I/O；deadline后返回typed close-blocked。
- checkpoint lag达到soft watermark时优先maintenance，普通admission不能越过hard online-recovery bound。

### 13.3 Writer与terminal completion lifecycle

- completion event FULL + checkpoint FULL。
- completion event FULL + checkpoint NONE，stable candidate重试。
- completion event FULL + checkpoint UNKNOWN，exact confirmation。
- completion event FULL + semantic reducer failure，event保持`DURABLE_FULL`且exact repair owner已安装，不重复写event。
- accounted completion batch的business/accounting partition与stored receipt exact join。
- async `emit`、`emit_many`、thread recorder及每个迁移后的domain adapter均不能丢失semantic settlement。
- completion 在 post-tool projection read前、期间、后提交的三种交错。
- terminal monitor firing收到`DURABLE FULL + REPAIR_OWNER_INSTALLED`时退休原firing owner，但不设置ledger UNKNOWN latch。
- monitor session-close terminalization与普通firing共用typed thread settlement，不再读取聚合`reconciliation_required`猜测physical outcome。

### 13.4 Agent happy path

```text
Model tool call
  -> terminal_process.submit("退出")
  -> ToolResult says running
  -> TerminalProcessCompletedEvent
  -> next ModelCallStart
  -> final assistant reply
  -> accepted disposition
  -> RunEnd(FINAL)
```

### 13.5 Finalization liveness

- reducer latch 下只安装一次repair dependency，不允许250ms轮询writer。
- transient NONE在bounded immediate burst后进入not-before，stable candidate不变。
- repair FULL receipt只唤醒matching RunEnd owner；stale receipt fail closed。
- structural failure进入typed blocked/reconciliation state。
- waiter cancellation只detach，physical owner仍可完成。
- Inspector可见 stable candidate与失败原因。
- Host close不会假称成功，也不会无界等待。
- event-loop换代只能join原executor physical future；同一repair plan的physical call count必须保持1。
- drain关闭外部finalization admission后，已准入RunEnd lineage仍可安装其output-materialization successor；drain必须重新快照直到两类task均结束。
- drain必须读取已经done的physical/output task结果；failed/cancelled task或仍处于`reconciliation_required`、`full_output_pending`等非terminal owner state时，close必须明确blocked。

### 13.6 Live/restart recovery

- 现有同步rebuild primitive失败时不清latch，成功时只清matching reducer latch。
- notification repair从validated checkpoint而非genesis开始，并受总events/bytes/deadline上限约束。
- event FULL、checkpoint未推进时 reopen可从旧 checkpoint + bounded delta恢复。
- repaired checkpoint与 event 948 exact join。
- live process修复后继续同一frozen RunEnd candidate。
- restart通过`RunStartEvent.terminal_run_end_event_id`构造recovered failure，不伪造旧process-local error payload。
- 超过online delta hard bound时返回`OFFLINE_REPAIR_REQUIRED`，不在Host open中无界fold。

### 13.7 PostgreSQL与真实终端

- 在真实JSONB round-trip上连续推进至少三个notification/monitor checkpoint successor。
- completion thread与checkpoint write故障注入可重复命中FULL/NONE/UNKNOWN。
- terminal process真实退出、completion迟到、post-tool safe point与下一次model call完成。
- Host close在checkpoint I/O、reducer repair、RunEnd write三种在途状态下均满足owner/drain矩阵。
- Host close必须先停止并drain tool、governance、compaction、subagent与MCP等全部EventLog producer，再drain writer与repair/post-fold固定点，最后关闭checkpoint owner。
- completion producer与monitor terminalization之间必须经过一次保持repair admission开放的writer → reducer-repair → post-fold barrier；monitor与notification terminalization后也必须建立相同safe point。
- provider-input preparation abandonment与generation close必须由session-owned async operation持有，脱离Host event loop，并让全部PostgreSQL read/write复用唯一Host close absolute deadline。
- 同步`RuntimeSession.close()`不得再生成provider-input durable event；它只接受已完成quiesce receipt与已经fold完成的provider-input terminal authority。

## 14. Definition of Done（已完成）

只有以下条件全部满足，才能认为本事故闭环：

- [x] Runtime projection checkpoint使用唯一、递归不可变的canonical JSON carrier；InMemory/PostgreSQL equality与fingerprint共享同一语义。
- [x] terminal notification/monitor semantic reducer callback不执行checkpoint SQL或可失败observer delivery。
- [x] semantic store、semantic account与reducer registration不会形成partial application。
- [x] notification checkpoint可从旧 base跨越含 ToolResult与completion的 bounded delta。
- [x] checkpoint NONE/UNKNOWN/重试由session-owned owner持有，physical failure不阻塞健康的context read。
- [x] production `emit/emit_many/emit_from_thread` 不再丢弃 reducer/reconciliation outcome；unsafe helper仅存在于tests/support。
- [x] completion event FULL后，terminal owner要么持有healthy settlement，要么能exact join RuntimeSession repair handle；不存在未知hard latch。
- [x] post-tool completion交错不会阻止下一次 model call。
- [x] finalization对结构性reducer latch不做periodic writer retry；只等待matching repair receipt。
- [x] physical retry有attempt deadline/backoff/close bound，stable RunEnd candidate不因次数或caller cancellation丢失。
- [x] live reconciliation repair完成后复用同一stable RunEnd candidate；restart使用RunStart中冻结的terminal event ID。
- [x] Host operational projection可区分 controller ready、run active、run blocked、run terminal，并暴露bounded repair/checkpoint/finalization diagnostics；离线Inspector不伪造process-local owner状态。
- [x] PostgreSQL JSONB integration与真实本地terminal physical process的deterministic post-tool dogfood通过。
- [x] 不需要重置 PostgreSQL才能避免复现。
- [x] 事故现场的等价脱敏fixture可在修复后完成bounded repair，不要求删除durable ledger。

## 15. 实施与验证回执

### 15.1 已落地的 ownership hard cut

本次实现按本文冻结的四层authority完成了物理拆分：

1. `CanonicalJsonObjectCarrier`成为runtime projection checkpoint payload的唯一递归不可变载体。InMemory与PostgreSQL adapter都从canonical bytes恢复carrier，successor、same-high-water confirmation与fingerprint不再依赖Python `tuple`/`list`对象相等性。
2. terminal notification与monitor改为`prepare -> validate -> atomic install`的semantic fold；`CheckpointedCommittedReducerIngress`返回完整`CommittedReducerFoldReceipt`。checkpoint SQL和listener/process-owner handoff均已移出semantic install临界路径。
3. `RuntimeProjectionCheckpointMaintenanceService`唯一持有stable candidate、FULL/NONE/UNKNOWN/CONFLICT、retry、confirmation、close drain与soft/hard lag。checkpoint PostgreSQL连接已迁入独立maintenance lane。
4. 物理suffix accounting覆盖完整`StoredEventBatchCommitReceipt`，包括与business event同批提交的materialization companion events。accounted producer在`LedgerMaterializationCoordinator`最终候选点执行exact pre-commit admission；越过4096 events或16 MiB前返回physical `NONE`，不会写入部分批次。
5. `EventWriteResult`继续作为低层真源，`CommittedEventSettlementReceipt`与`RuntimeThreadEventSettlementReceipt`提供closed producer视图。production `RuntimeSession.emit()`、`emit_many()`、`emit_from_thread()`以及对应run-ledger collapse入口已经删除。
6. semantic fold失败时，event仍保持durable FULL；`CommittedReducerRepairService`安装stable plan，从validated checkpoint读取连续bounded delta并原子替换domain state。256-event page、4096-event总量与16 MiB总量均为强上限，超限进入typed offline-repair边界。
7. post-fold process owner由独立`CommittedReducerPostFoldService`重驱，不重复semantic fold；listener失败仅形成operational diagnostic。
8. Agent在model admission、post-tool context/compaction与follow-up之前等待exact reducer safe point。`RunFinalizationService`不再周期性重写同一RunEnd；它等待matching repair receipt，兼容FULL winner可直接adopt，caller cancellation只detach waiter。
9. Host close先停止并drain terminal completion；在monitor terminalization前建立保持repair admission开放的semantic fixed point，并在monitor/notification terminalization后再次执行同形barrier。其余run、tool、governance、compaction、provider-input、subagent与MCP producer退出后，再完成最终writer、repair与post-fold fixed point；关闭repair admission后才drain checkpoint owner。任何晚到durable FULL在repair owner关闭前都有精确接管者。
10. Provider-input close producer由`RuntimeSession`稳定async task与`ContextInputIoService` physical handle共同持有。Host cancellation/timeout只detach waiter；preparation recovery、exact reads、generation close与FIFO writes复用同一个`close_deadline`。同步finalizer只验证receipt和folded terminal state，不执行数据库I/O或追加event。
11. Host resume修复dangling run时创建的临时`RuntimeSession`不再直接执行同步finalizer。专用async teardown先quiesce provider-input，完成writer → reducer-repair → post-fold固定点，再停止并drain runtime projection checkpoint maintenance，随后drain其余checkpoint与共享I/O owner，最后才调用同步`RuntimeSession.close()`；DIRTY checkpoint既不会被忽略，也不会被强制清空。

主要代码owner：

- `src/pulsara_agent/primitives/stored_event.py`
- `src/pulsara_agent/runtime/projection_checkpoint_maintenance.py`
- `src/pulsara_agent/runtime/committed_reducer_repair.py`
- `src/pulsara_agent/runtime/committed_reducer_post_fold.py`
- `src/pulsara_agent/runtime/session.py`
- `src/pulsara_agent/runtime/authority_materialization/account.py`
- `src/pulsara_agent/runtime/terminal/notification.py`
- `src/pulsara_agent/runtime/terminal/monitor.py`
- `src/pulsara_agent/runtime/run_execution/finalization.py`
- `src/pulsara_agent/runtime/agent.py`
- `src/pulsara_agent/host/session.py`
- `src/pulsara_agent/host/resume.py`

### 15.2 回归与故障注入证据

最终验证在2026-08-05完成：

| Gate | 结果 | 覆盖 |
|---|---:|---|
| 事故contract、owner与architecture矩阵，加完整Agent轨迹 | `58 passed, 91 deselected` | canonical carrier、checkpoint FULL/NONE/UNKNOWN/CONFLICT、stable successor、physical close、hard fence、semantic repair、finalization、Host close ordering |
| Runtime writer/session/publisher/terminal/monitor/projection/wiring相邻回归 | `137 passed` | typed settlement、thread recorder、publication与terminal lifecycle |
| Authority materialization定向 | `3 passed, 44 deselected` | genesis、dispatch、one-shot完整physical candidate |
| 真实PostgreSQL JSONB checkpoint定向 | `3 passed, 44 deselected` | row round-trip、ledger-prefix join、连续三个successor |
| 完整post-tool incident轨迹独立重复 | `3/3 passed` | 真实本地terminal child退出、completion semantic故障、checkpoint SQL故障、repair、第二次ModelStart、final reply、RunEnd(FINAL) |
| Static gates | 通过 | `ruff check src tests`、`compileall src tests`、`git diff --check` |

合入前post-review又补充验证了四个此前未覆盖的ownership窗口：

| Post-review gate | 结果 | 覆盖 |
|---|---:|---|
| Incident、repair、finalization、monitor与architecture | `89 passed` | 跨event-loop仅一次physical repair、drain内successor、monitor typed FULL settlement、Host依赖顺序 |
| 真实Host close定向矩阵 | `37 passed, 71 deselected` | teardown、active stream、Host session close及依赖drain |
| Runtime writer/session/publisher/terminal/Agent相邻矩阵 | `201 passed` | typed settlement API扩展、repair safe point与Agent主路径 |

其中loop-teardown回归不再把两次physical call写成预期行为：测试使用会拒绝第二次安装的semantic authority探针，并断言`physical_generation == 1`。Finalization回归在drain已经开始后才让已准入physical task取得RunEnd FULL，证明其内部output successor可以安装，且drain会继续等待该successor。Monitor回归同时覆盖fake typed receipt与真实RuntimeSession reducer故障，确认`REPAIR_OWNER_INSTALLED`不会污染ledger-level UNKNOWN latch。

第二轮post-review进一步补齐了四个close/finalization窗口：

1. `RunFinalizationService.drain()`不再把`task.done()`解释为空闲。它会消费每个physical/output task的结果，cancelled/failed task形成明确close blocker；没有live task时还必须证明所有已准入owner处于`idle | completed`。
2. Host在terminal completion drain、monitor terminalization与notification terminalization之间分别调用开放admission的`drain_open_committed_reducer_barrier()`。因此前一producer产生的durable FULL + reducer repair会在下一producer写入前完成，不会被hard-reconciliation gate反向阻塞。
3. provider-input close改为session-owned async quiesce。physical operation在bounded auxiliary I/O executor运行，内部EventLog exact read和Runtime writer共享Host传入的唯一absolute deadline；waiter deadline/cancellation不会取消physical owner。同步`RuntimeSession.close()`只消费完成receipt，并要求preparation/generation terminal authority已经semantic fold。
4. 原本漏收集的provider generation close回归已改为`test_runtime_session_for_test_close_durably_closes_open_provider_generation`，并覆盖direct-close不产生event、async receipt、open repair barrier与最终sync close。新增故障注入还覆盖off-loop执行、deadline detach、pending physical close blocker及provider reducer failure fail-closed。

第二轮新增/受影响验证：

| Gate | 结果 | 覆盖 |
|---|---:|---|
| Incident、provider-input与architecture定向 | `91 passed` | done-task failure、内部successor、开放repair barrier、async quiesce、单deadline、reducer failure、pytest collection |
| Host close相关筛选矩阵 | `43 passed, 208 deselected` | terminal completion retry、monitor/notification、subagent及Host teardown顺序 |
| Agent direct-runtime cleanup | `2 passed` | 有open provider generation时显式async quiesce后再执行sync finalizer |
| 真实PostgreSQL dangling-run resume | `1 passed` | 临时recovery session产生DIRTY checkpoint后执行bounded async teardown，resume继续进入replay且没有遗留checkpoint worker |

按任务约定，本轮仍未运行全量`pytest`。此前独立Host-resume探针暴露的`runtime checkpoint maintenance is not idle`已作为checkpoint-maintenance close ownership缺口闭环：resume cleanup现在只能经专用async teardown进入同步finalizer，architecture gate同时禁止恢复直接调用`runtime_session.close()`。

完整轨迹使用真实本地terminal physical process与deterministic scripted model，主动把completion安排在ToolResult为`running`之后、post-tool follow-up safe point之前，并同时注入一次semantic fold失败和一次checkpoint I/O失败。断言包括：completion只写一次、repair plan exact、第二次`ModelCallStartEvent`位于completion之后、最终assistant正文存在、`RunEndEvent(stop_reason="final")`存在。

按任务约定未运行全量`pytest`，没有运行real-provider LLM dogfood；本事故不依赖provider输出随机性，最终gate使用可重复的完整runtime/terminal物理轨迹。PostgreSQL无需reset，也没有新增schema migration。

### 15.3 Architecture guards

新增machine guards持续禁止以下回退：

- mutable `dict` checkpoint payload；
- terminal semantic reducer内的checkpoint/storage I/O；
- production event outcome collapse API；
- post-tool projection前后缺少reducer repair barrier；
- online使用unbounded reducer rebuild；
- terminal physical owner读取完整`EventWriteResult`或自行解释`.committed_events`；
- finalization无分类地无限retry；
- Host在completion/repair/finalization前释放其依赖；
- Host在任一EventLog producer退出前关闭repair/post-fold admission；
- RuntimeSession以外出现runtime projection checkpoint mutation owner。
- accounted producer绕过最终physical candidate的checkpoint admission，或在admission前进入storage transaction。

## 16. 非目标与相邻问题

本事故修复不应顺带改变：

- Go TUI transcript rendering语义；
- canonical transcript acceptance/suppression/pairing；
- terminal tool公开 API；
- model-call lifecycle ownership；
- Long-Horizon compaction policy。

此前旧 session 使用 `--continue` 时出现的：

```text
presentation spine does not match checkpoint transcript leaves
```

属于另一个 presentation restore/acceleration一致性问题。它可能共享“acceleration不可覆盖semantic authority”的设计原则，但没有证据表明与本次 tuple/list checkpoint CAS 是同一个直接根因，应独立追踪。

## 17. 最终结论

模型没有正常 finalization 的直接原因不是模型拒绝汇报，而是 Python Runtime 在最后 ToolResult 后、下一次模型调用前进入了 hard reconciliation。

该 reconciliation 是由一个语义相同、物理 Python container shape不同的 checkpoint payload误判触发的。真正让它升级为主线事故的是：checkpoint I/O仍嵌在semantic reducer callback中，callback失败后留下partial process state；完整`EventWriteResult`又被多组producer convenience API压扁；finalization最后对一个需要外部repair的hard latch持续轮询writer。三处ownership错误共同将一个可重建的acceleration故障放大成永久active run。

本事故的修复不能只做 `tuple -> list` 的局部转换。应同时闭环：

1. canonical storage identity；
2. semantic fold的原子install与registration high-water；
3. writer outcome的唯一acceptance classifier；
4. session-owned checkpoint/reducer repair owner；
5. finalization的event-driven repair dependency与bounded physical liveness；
6. Host operational projection可观察性，且离线Inspector不伪造process-local状态。

推荐实施不是「让checkpoint失败也算event write失败」，而是把四个事实分开：event已经FULL；semantic fold是否健康；checkpoint是否已被maintenance owner接受；run terminalization是否正在等待repair。只有这种分层，才能同时保住durability、fail-closed语义与实际liveness。
