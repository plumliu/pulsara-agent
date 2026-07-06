# Pulsara Context Compaction Timing 实施文档

> 基于 `PULSARA_CONTEXT_COMPACTION_TIMING_NEXT_DESIGN.zh.md` 的落地计划。
>
> 本文档只覆盖 timing / REPL 体验优化。summary 内容质量、continuity carry-forward、malformed output fail-closed、RunErrorEvent fail-closed 等已有连续性修复不在本文档重复展开。

## 0. 设计审查结论

`PULSARA_CONTEXT_COMPACTION_TIMING_NEXT_DESIGN.zh.md` 的核心判断可以落地:

1. 当前问题的根因不是 summary 质量，而是 auto compaction 的 UI 可见执行时机错放在 run-end 后台任务。
2. V1 应把 auto compaction 的唯一热路径收敛到下一轮 user turn 的 preflight。
3. 用户提交输入后，如果 preflight compact 触发，HostSession 必须先 compact，再继续消费同一个 `user_input`。
4. manual `:compact` 仍保留 idle 时立即执行语义。
5. mid-turn inline compact 应作为后续 PR，不能混入 V1。

但设计文档还需要实施时钉住四个硬边界:

1. `contracts/CONTEXT_COMPACTION_CONTINUITY_CONTRACT.zh.md` 必须和 timing 设计保持同步。PR T1 的第一验收项是确认 contract 固定: run-end 不再调度 UI 可见后台 auto compact；run-start / next-turn preflight 是 V1 auto compact 主路径。
2. `HostSession.compact_now()` 的 manual 路径必须和 preflight 一样 publish direct-written compaction events，避免 `RuntimeSession.publisher` sequence gap。这不是体验优化附属项，而是事件发布正确性。
3. V1 直接删除 `_auto_compaction_task` / `_drain_auto_compaction()` / run-end schedule 残留，而不是保留 no-op 后门。
4. CLI listener 可以继续保留，但它只能在 preflight 这种非输入编辑期收到 auto 事件。manual `:compact` 由 command return dict 打印，不走 listener，避免双输出。不要试图先通过 prompt_toolkit redraw 修复后台输出。

## 1. 当前代码事实

### 1.1 Runtime wiring

当前 compaction service 在 runtime composition root 中创建:

- `src/pulsara_agent/runtime/wiring.py`
- `_with_memory_governance_engine(...)`
- `RuntimeWiring.compaction_service`

重要事实:

- durable runtime 才配置 `ContextCompactionService`。
- in-memory test wiring 默认 `compaction_service=None`，测试里会用 `replace(runtime_wiring, compaction_service=fake)` 注入 fake。
- `AgentRuntime` 当前不持有 compaction service，也不应该在 V1 里新增依赖。

V1 接线原则:

```text
RuntimeWiring owns ContextCompactionService
HostSession schedules and notifies compaction
AgentRuntime remains unaware of compaction
CLI only observes HostSession listener notices
```

### 1.2 HostSession 当前路径

当前文件:

- `src/pulsara_agent/host/session.py`

V1 目标关键路径:

```text
run_turn(user_input)
  -> _prepare_prior_messages_for_turn(user_input)
     -> _prior_messages()
     -> _compact_if_needed_and_notify(
          current_user_input=user_input,
          model_visible_messages=prior_messages,
          reason="preflight_context_threshold",
        )
     -> if compacted: _prior_messages()
  -> AgentRuntime.run_task(user_input, prior_messages=...)
  -> _finish_active_run()
     -> _notify_governance()
     -> clear active run bookkeeping
```

V1 只保留 preflight 路径，删除或禁用 run-end schedule。

### 1.3 ContextCompactionService 当前能力

当前文件:

- `src/pulsara_agent/runtime/compaction/service.py`

已有能力足够支撑 V1:

- `compact_if_needed(current_user_input=..., model_visible_messages=..., reason=...)`
- `compact(trigger="manual", reason="user_requested", force=True)`
- auto trigger 估算基于 model-visible messages。
- compact input 不包含当前 user input。
- repeated compaction 会 carry forward previous summary。
- malformed output / compact model RunErrorEvent 会 fail-closed。

V1 不需要大改 service。主要改 HostSession 调度。

### 1.4 CLI 当前监听

当前文件:

- `src/pulsara_agent/cli.py`
- `_attach_repl_compaction_notifications(session)`
- `_print_context_compaction_event(event)`
- `:compact` 分支调用 `session.compact_now()`

CLI listener 当前是普通 `print()`。V1 不需要引入 prompt_toolkit safe print，前提是 run-end background auto compact 被禁掉。否则 listener 仍可能在 `read_line()` 等待期间污染 prompt。

另外有一个 implementation 必须覆盖的隐藏坑: `ContextCompactionService.compact(...)` 会直接 append compaction events 到 event log；manual 路径必须像 preflight 一样调用 `runtime_session.publish_stored_events(...)`。如果 runtime publisher 已经绑定并期待连续 sequence，manual compact 后的下一条 `RuntimeSession.emit(...)` 可能被 publisher sequence gap 卡住。

## 2. 目标行为

### 2.1 Auto compact

允许的 auto compact 时机:

```text
用户提交新的普通 user turn 输入后
模型开始处理该输入前
```

流程:

```text
pulsara> 讲讲梅西世界杯表现

HostSession.run_turn("讲讲梅西世界杯表现")
  -> rebuild prior messages
  -> estimate model-visible prior + current_user_input
  -> threshold reached
  -> compact_if_needed(reason="preflight_context_threshold")
  -> publish compaction events
  -> notify CLI listener while no prompt is active
  -> rebuild prior messages
  -> AgentRuntime.run_task(original user_input, compacted prior_messages)
```

用户不能被要求再次输入同一个问题。

V1 preflight auto compact 只属于普通 `run_turn(...)` / `stream_turn(...)` 新 user input 路径。以下 suspended-run resume 路径不得自动 compact:

- approval resume;
- plan interaction answer / revise / approve;
- MCP elicitation resume;
- abort / stop / host teardown recovery。

原因: 这些路径正在继续同一个 suspended run，中途替换 prior context 会把原始模型调用、pending tool/interaction 与恢复后的上下文拆开。

### 2.2 Run end

run 完成后:

```text
assistant: final answer
pulsara>
```

到这里必须是真 idle:

- 不创建新的 auto compaction task。
- 不会在数秒后打印 `context compaction completed: ...`。
- close / detach 不需要等待刚 schedule 的 compaction task。

### 2.3 Manual compact

manual compact 保持:

```text
pulsara> :compact
context compaction completed: ...
pulsara>
```

约束:

- 只允许 idle session。
- active run / pending approval / pending plan interaction / pending MCP elicitation 下仍拒绝。
- 继续走 `compact(trigger="manual", reason="user_requested", force=True)`。
- manual compact 直接写入的 started/completed/failed events 也必须 publish 给 `RuntimeSession.publisher`，但不要再通过 listener 打印 completed，否则 CLI 会双输出。

## 3. 实施分解

### PR T1: contract + 禁用 run-end background auto compact

落点:

- `src/pulsara_agent/host/session.py`
- `tests/test_context_compaction.py`
- `contracts/CONTEXT_COMPACTION_CONTINUITY_CONTRACT.zh.md`

contract 改动是本 PR 的硬前置:

```text
V1 auto compact 的 UI 可见执行点是 run-start / next-turn preflight。
run-end 不得调度后台 auto compact；`pulsara>` 显示后不得再由 auto compaction 写入 REPL 输入区。
```

代码改动:

1. 修改 `_finish_active_run()`:

```python
def _finish_active_run(self) -> None:
    self._notify_governance()
    self._active_task = None
    self._active_state = None
    self.active_run_id = None
    self.stopping_run_id = None
    self.last_active_at = time.monotonic()
    # Do not schedule run-end background auto compaction in V1.
```

2. 删除 run-end background task 残留:

- 删除 `_schedule_auto_compaction_after_run()`。
- 删除 `_compact_after_run_end(...)`。
- 删除 `_auto_compaction_task` 字段。
- 删除 `_drain_auto_compaction(...)`。
- 同步移除 `compact_now()` / `_prepare_prior_messages_for_turn()` / `aclose()` / `close()` 中的 drain/cancel 调用。

同时更新 contract 测试门槛:

- 删除或改写 “single huge completed run 可触发 auto compact” 的 run-end 暗示。
- 保留 “single huge completed run 在下一轮 preflight 可触发 auto compact”。

验收测试:

- `test_host_session_does_not_notify_compaction_listener_after_run_end`
- fake service 的 `compact_if_needed` 只收到 `preflight_context_threshold`，没有 `run_end_context_threshold`。
- 代码中不存在 `_auto_compaction_task` / `_drain_auto_compaction` / `_compact_after_run_end`。

需要改写的现有测试:

- `test_host_session_invokes_compaction_at_preflight_and_run_end_safe_points`

改名建议:

- `test_host_session_invokes_compaction_at_preflight_only`

断言建议:

```python
calls = await run()
assert [call["reason"] for call in calls] == ["preflight_context_threshold"]
assert calls[0]["current_user_input"] == "hello compaction"
assert calls[0]["model_visible_messages"] == []
```

### PR T2: 修正 manual compact 的 event publish

落点:

- `src/pulsara_agent/host/session.py`
- `tests/test_context_compaction.py`

问题:

- `ContextCompactionService.compact(...)` 会直接写 `ContextCompactionStartedEvent` 和 terminal event 到 event log。
- `compact_now()` 当前只拿返回值给 CLI 打印，没有 publish 这些 stored events。
- preflight 路径已经通过 `_compact_if_needed_and_notify(...)` 处理 sequence gap，manual 路径也应做等价处理。

建议改法:

新增一个 helper，把“捕获 direct-written compaction events 并 publish”与“是否通知 listener”拆开:

```python
async def _publish_compaction_events_after(self, before_sequence: int) -> list[AgentEvent]:
    compaction_events = await asyncio.to_thread(self._compaction_events_after, before_sequence - 1)
    self.wiring.runtime_wiring.runtime_session.publish_stored_events(compaction_events)
    return compaction_events
```

preflight:

```python
before_sequence = await asyncio.to_thread(event_log.next_sequence)
compacted = await service.compact_if_needed(...)
compaction_events = await self._publish_compaction_events_after(before_sequence)
terminal_event = self._latest_terminal_compaction_event(compaction_events)
if terminal_event is not None:
    self._notify_compaction_listeners(terminal_event)
return compacted
```

manual:

```python
before_sequence = await asyncio.to_thread(event_log.next_sequence)
event = await service.compact(trigger="manual", reason="user_requested", force=True)
await self._publish_compaction_events_after(before_sequence)
return {...}
```

manual 不调用 `_notify_compaction_listeners(...)`。CLI 已经根据 `compact_now()` 的 return dict 打印结果。

测试:

- `test_host_session_compact_now_publishes_directly_written_compaction_events`
- `test_host_session_compact_now_publishes_events_without_notifying_listener`
- fake service 直接向 event log append started/completed。
- 先通过 `runtime_session.emit(...)` 绑定 publisher。
- `await session.compact_now()`。
- 再 `await runtime_session.emit(...)`，用 timeout 断言不会因为 sequence gap 卡住。
- listener list 仍为空，避免 CLI manual compact 双输出。

### PR T3: 固化 preflight compact 后继续消费原 user input

落点:

- `src/pulsara_agent/host/session.py`
- `tests/test_context_compaction.py`

当前代码已经基本满足:

- `run_turn()` 先拿 `prior_messages = await _prepare_prior_messages_for_turn(user_input)`。
- `AgentRuntime.run_task(user_input, prior_messages=prior_messages, ...)` 使用原始 `user_input`。
- `_prepare_prior_messages_for_turn()` compact 成功后重新 `_prior_messages()`。

需要补测试把这个行为变成契约。

新增 fake service:

```python
class _FakePreflightCompactingService:
    async def compact_if_needed(self, **kwargs) -> bool:
        self.calls.append(kwargs)
        # 模拟 service 直接写 started/completed event 或只返回 True。
        return True
```

更好的 fake:

- 写入一个 completed boundary 及 summary artifact，使第二次 `_prior_messages()` 真正变短。
- 这样可以证明 HostSession compact 后 rebuild prior，而不是复用 compact 前 prior。

测试断言:

1. service 收到:

```python
reason == "preflight_context_threshold"
current_user_input == original_input
model_visible_messages == prior_before_compact
```

2. agent runtime / transport 收到同一个原始输入:

```python
assert transport.contexts[0].messages[-1].content == [original_input]
```

或通过 fake `AgentRuntime.run_task` 记录参数。

3. compact 后 `prior_messages` 是重新 rebuild 的结果:

```python
assert any(message.metadata.get("kind") == "context_compaction_summary" for message in prior_messages_seen_by_agent)
```

坑点:

- 不要把当前 `user_input` 写入 summary。service 现有测试 `test_preflight_current_user_input_affects_threshold_but_not_summary_input` 要保留。
- 如果 fake service 只返回 `True` 但没有写 boundary，`_prior_messages()` 二次 rebuild 内容不会变。测试“重新 rebuild”时应写真实 boundary 或 spy `_prior_messages()` 调用次数。

### PR T4: CLI prompt 污染回归测试

落点:

- `src/pulsara_agent/cli.py`
- `tests/test_cli_host.py`

V1 代码上 CLI 可以不改，但必须加测试防止回潮。

测试策略一: session fake

构造 fake session:

- `run_turn()` 返回 final answer。
- `add_compaction_listener()` 保存 listener。
- run 结束后不主动调用 listener。

断言:

- `_host_repl` 或较小的 REPL command loop helper 输出里没有 run-end `context compaction completed:`。

测试策略二: HostSession integration

用 fake compaction service + scripted prompt:

1. 第一轮输入普通问题。
2. `run_turn()` 完成。
3. prompt 再次显示前后没有 listener notice。
4. 第二轮输入触发 preflight fake compaction，notice 可出现于第二轮执行期。

注意:

- 当前 `_print_context_compaction_event()` 仍是普通 `print()`，测试目的不是验证 prompt_toolkit redraw。
- 测试目标是 run-end 不会调用 listener。

保留现有测试:

- `test_cli_host_repl_compact_command_invokes_session_compaction`
- `test_cli_context_compaction_event_notices`

这两个测试验证 manual / listener formatting，不应因为禁用 run-end auto compact 而删除。

### PR T5: 清理旧 background task 残留

落点:

- `src/pulsara_agent/host/session.py`

V1 直接删除旧 background task 残留:

- `_auto_compaction_task`
- `_schedule_auto_compaction_after_run()`
- `_compact_after_run_end(...)`
- `_drain_auto_compaction(...)`

删除理由:

1. manual compact 仍受 `_run_lock` 和 `_raise_if_active_run()` 保护，不需要 drain。
2. preflight 也在 `_run_lock` 内，且 run-end 不再 schedule，不需要 drain。
3. close 不再需要 cancel compaction task。
4. 保留 no-op drain 会让后续开发误以为仍存在后台 compaction owner。

## 4. ContextCompactionService 改动建议

V1 不建议改 service 行为。

必须保持:

- `should_auto_compact(...)`
- `compact_if_needed(...)`
- `compact(trigger="manual", force=True)`
- `current_user_input` 只参与 threshold estimate。
- compact input 不包含当前 user input。
- repeated compaction carry-forward。
- RunErrorEvent / malformed output fail-closed。

可以考虑的小改:

1. reason 常量化

新增:

```python
AUTO_COMPACTION_REASON_PREFLIGHT = "preflight_context_threshold"
MANUAL_COMPACTION_REASON_USER_REQUESTED = "user_requested"
```

不建议保留:

```python
AUTO_COMPACTION_REASON_RUN_END = "run_end_context_threshold"
```

除非为了历史事件/inspector 兼容，只作为 legacy string 存在。

2. service 测试改名

现有 service 级测试里传 `reason="run_end_context_threshold"` 的用例应改成 neutral 或 preflight reason，除非它是在测试 legacy event parsing。

例如:

- `test_auto_context_compaction_is_threshold_driven_not_run_end_unconditional`
- `test_auto_context_compaction_uses_model_visible_messages_not_raw_streaming_events`

这些测试重点是 service threshold，不是 HostSession timing。reason 可以改成 `"preflight_context_threshold"` 或 `"context_threshold"`，避免继续暗示 run-end 是合法主路径。

## 5. AgentRuntime 接线说明

V1 不改 AgentRuntime。

原因:

- 当前 compact 的 safe point 在 user turn 进入 AgentRuntime 之前。
- HostSession 能完整控制 prior_messages、current_user_input、listeners 和 runtime_session event publishing。
- AgentRuntime 内部 loop 处理 tool calls、approval、plan interaction、MCP elicitation，V1 不应在这些中途替换 `LoopState.messages`。

V1 调用链应保持:

```text
HostSession.run_turn(user_input)
  -> preflight compact maybe
  -> AgentRuntime.run_task(user_input, prior_messages=compacted_prior)
```

不要做:

- 不要把 `ContextCompactionService` 注入 `AgentRuntime`。
- 不要在 `_stream_model_loop()` 或 `_execute_tool_blocks()` 中 compact。
- 不要在 pending approval resume / plan interaction resume / MCP elicitation resume 前 auto compact。
- 不要让 compact summary 写入 `LoopState.messages` 中途替换当前 run 的上下文。

V2 mid-turn inline compact 才需要 AgentRuntime seam。届时建议另开设计:

- `CompactionCoordinator` 或 `RuntimeContextCompactor` 注入 AgentRuntime。
- 明确 safe point: model call 前、tool-result 后且没有 pending interaction 时。
- 为 events 加 `phase="mid_turn"` 或 metadata。
- compact 完成后 rebuild model-visible prior，并保留当前 in-flight user input / tool result tail。

## 6. Event publish 与 listener 语义

`ContextCompactionService` 直接写 event log。

HostSession 必须继续做:

```python
before_sequence = event_log.next_sequence()
compacted = await service.compact_if_needed(...)
compaction_events = self._compaction_events_after(before_sequence - 1)
runtime_session.publish_stored_events(compaction_events)
terminal_event = self._latest_terminal_compaction_event(compaction_events)
if terminal_event:
    self._notify_compaction_listeners(terminal_event)
```

原因:

- 避免 RuntimeSession publisher sequence gap。
- CLI listener 需要 completed/failed notice。
- inspector 依赖 durable events。

V1 改动后，这段只由:

- preflight auto compact
- explicit tests

触发。

manual compact 也必须 publish stored events，但不应 notify CLI listener。推荐拆成两个层次:

- publish helper: 所有 direct-written compaction events 都走。
- listener notification: 只给 auto preflight 使用；manual 由 command return dict 打印。

## 7. 测试矩阵

### 7.1 HostSession

新增/修改:

- `test_host_session_invokes_compaction_at_preflight_only`
- `test_host_session_does_not_notify_compaction_listener_after_run_end`
- `test_preflight_compaction_continues_original_user_input`
- `test_preflight_compaction_rebuilds_prior_messages_after_completed_boundary`
- `test_host_session_notifies_preflight_auto_compaction_failure`
- `test_host_session_publishes_directly_written_preflight_compaction_events_to_avoid_sequence_gap`
- `test_host_session_compact_now_publishes_directly_written_compaction_events`
- `test_host_session_compact_now_publishes_events_without_notifying_listener`
- `test_pending_approval_resume_does_not_auto_compact`
- `test_plan_interaction_resume_does_not_auto_compact`
- `test_mcp_elicitation_resume_does_not_auto_compact`

删除或改写:

- `test_host_session_invokes_compaction_at_preflight_and_run_end_safe_points`

### 7.2 ContextCompactionService

保留:

- typed events roundtrip
- manual compact writes artifact and events
- repeated compaction carry-forward
- malformed output fail-closed
- RunErrorEvent fail-closed
- threshold driven
- current user input affects estimate but not summary input
- circuit breaker

调整:

- service-level tests 不再使用 `run_end_context_threshold` 作为主要 reason。
- “single huge completed run” 改成 “下一轮 preflight 可 compact single huge completed run”。

### 7.3 CLI

新增:

- run-end 后不出现 background compaction notice。
- preflight compact notice 出现在用户提交输入后、模型输出前。

保留:

- `:compact` 成功/失败/skipped 输出。
- `_print_context_compaction_event()` formatting。

### 7.4 Inspector

现有 inspector 对 completed boundary、missing artifact、windows 的测试保留。

可新增:

- run report 能看到 latest run 使用的 preflight boundary。
- `reason="preflight_context_threshold"` 能在 window/event projection 中显示。

## 8. 编码坑点

1. Contract 会和实现冲突

长期 contract 当前仍认可 run-end safe-point。实施 T1 时必须同步更新 contract，否则未来 review 会以 contract 为准把 run-end 调度加回来。

2. background compaction task 不能偷偷保留

测试要确认代码中不存在 `_auto_compaction_task` / `_drain_auto_compaction()` / `_compact_after_run_end()`，而不是只确认 run-end 后没有输出。

3. fake service 只返回 True 不等于真实 compact

如果测试要证明 prior 被 compacted，需要 fake service 写 completed event 和 summary artifact。否则 HostSession 二次 `_prior_messages()` 看起来不会变化。

4. current user input 不能进 summary

preflight compact 发生在用户提交之后，但 summary 仍只能总结旧 context。当前 user input 只能参与 threshold estimate，并随后原样交给 AgentRuntime。

5. listener 不要双打印 manual compact

manual `:compact` 当前通过 return dict 打印。修复 manual event publish 时不要同时 notify listener，否则 CLI 会输出两次 completed。

6. close / detach 语义要保持幂等

删除 `_auto_compaction_task` 后，`close()` 和 `aclose()` 不再有 compaction cancel/drain 分支。Host teardown 仍只需要 drain active/suspended run、关闭 MCP manager、关闭 runtime。

7. pending interaction 不应触发 auto compact

preflight only 意味着只有普通 `run_turn/stream_turn` 新 user input 会触发。approval resume、plan interaction answer / revise / approve、MCP elicitation resume 不应自动 compact，避免中途重写 suspended run 的上下文。

8. governance timing 不要顺手改

`_finish_active_run()` 仍要调用 `_notify_governance()`。本 PR 只移除 compaction schedule，不要改 memory governance run-end 行为。

9. in-memory wiring 没有 compaction_service

很多 tests 使用 in-memory runtime wiring。HostSession compaction tests 需要显式 `replace(runtime_wiring, compaction_service=fake)`。

10. historical events may contain `run_end_context_threshold`

Inspector / old sessions 可能已有 run-end reason 的 compaction events。不要让新代码无法读取旧 events。只是新调度不再产生该 reason。

## 9. 推荐最终 PR 顺序

1. PR T1: contract + HostSession 禁用 run-end schedule + HostSession 测试改写。
2. PR T2: manual compact stored event publish 修复，抽 shared publish helper，并防止 listener 双输出。
3. PR T3: preflight user input continuation、prior rebuild、suspended-run resume 不 compact 的测试补强。
4. PR T4: CLI prompt pollution regression tests。
5. PR T5: 删除 `_auto_compaction_task` 残留和 drain/cancel 逻辑。
6. PR T6: 单独设计 mid-turn inline compact。

## 10. 验收命令

最低:

```bash
uv run ruff check src/pulsara_agent/host/session.py src/pulsara_agent/runtime/compaction tests/test_context_compaction.py tests/test_cli_host.py
uv run pytest tests/test_context_compaction.py tests/test_cli_host.py::test_cli_host_repl_compact_command_invokes_session_compaction tests/test_cli_host.py::test_cli_context_compaction_event_notices -q
```

建议:

```bash
uv run ruff check src tests
uv run pytest tests/test_context_compaction.py tests/test_cli_host.py tests/test_inspector.py tests/test_recovery.py -q
uv run pytest -q
```

手动 REPL 验收:

```text
1. 启动 REPL。
2. 制造足够长的上下文并完成一轮 assistant 回复。
3. 看到 pulsara> 后等待数秒，不应出现 context compaction completed/failed。
4. 下一轮输入触发阈值时，preflight compaction notice 可以出现在用户提交输入之后、assistant 回答之前。
5. notice 后系统继续回答刚提交的同一条输入，不要求用户再次回车。
6. :compact 仍立即执行并打印结果。
```
