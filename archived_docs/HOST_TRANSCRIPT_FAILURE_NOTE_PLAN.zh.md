# Host Transcript 跨 Turn 失败提示计划

## 0. 结论

跨 turn 的失败提示应该注入在 `host/transcript.py` 的 transcript reconstruction 层，而不是 LLM adapter、retry helper、system prompt composer 或 memory hook。

但原计划里的门控条件需要修正：不能用“失败 run 没有 `ReplyEndEvent` / 没有可重放 assistant reply”判断是否注入提示。代码事实是：最常见的 provider/API 失败路径通常仍然会产生 `ReplyEndEvent`，因此 transcript 会重放出一个空的或半截的 assistant message。真正可靠的主信号应该是 terminal `RunEndEvent.status == "failed"`。

本计划只定义跨 turn 的轻量提示投影，不修改 retry 机制，不修改 provider error 分类，不把失败写入 memory，也不改变 canonical event log。

## 1. 已核实的代码事实

### 1.1 下一轮上下文来自 transcript reconstruction

`HostSession.run_turn()` / `stream_turn()` 每轮都会调用 `_prior_messages()`，而 `_prior_messages()` 直接调用：

```python
rebuild_prior_messages(self.wiring.runtime_wiring.event_log)
```

因此，用户在下一轮说“请继续”时，模型能看到什么历史，主要由 `src/pulsara_agent/host/transcript.py` 决定。

### 1.2 用户输入已经能跨 turn 保留

`AgentRuntime._stream_task()` 在模型调用前发出 `RunStartEvent`，并在 metadata 里保存：

```python
metadata={"user_input": user_input}
```

`rebuild_prior_messages()` 当前会把每个 `RunStartEvent.metadata["user_input"]` 重建成 `UserMsg`。所以如果“用户输入 1”之后发生 APIConnectionError，下一轮“请继续”时，模型已经能看到“用户输入 1”。

缺口不是“用户输入丢了”，而是“模型看不到上一轮没有完成”。

### 1.3 同 run 内 recovery 已经有自己的提示

`runtime/context.py` 中已有 `state.recovery_mode` prompt：

```text
The previous model/tool step failed. Recover by inspecting the latest observation...
```

这个提示只服务于同一个 `AgentRuntime` run 内的恢复。每个 host turn 都会 `new_state()`，所以它不会跨 turn 保留。

### 1.4 失败 run 可能有 ReplyEndEvent，也可能没有

`LLMRuntime._stream_reply()` 的结构是：

```python
yield ReplyStartEvent(...)
async for event in transport.stream(...):
    yield event
yield ReplyEndEvent(...)
```

OpenAI Chat Completions / Responses adapter 在 retry exhausted、provider failure event、APIConnectionError 等常见路径上，通常是 yield `RunErrorEvent` 后 `return`，而不是把异常继续抛出。只要 transport generator 正常结束，`LLMRuntime` 就会继续发 `ReplyEndEvent`。

这意味着失败 run 很可能有如下事件链：

```text
RunStart
ReplyStart
RunError
ReplyEnd
RunEnd(status="failed", stop_reason="model_error")
```

当前 transcript reconstruction 会把它重建成：

```text
UserMsg("原始用户输入")
AssistantMsg(content=[])
```

或在部分输出后失败时，重建成一个半截 assistant message。

但也有另一种失败形状：如果异常真正从 transport/LLM runtime 抛出到 `AgentRuntime._stream_task()` 的 `except` 分支，那么当前 run 会得到 `RunErrorEvent`，但不会得到 `ReplyEndEvent`。

因此，失败 run 可能有 `ReplyEndEvent`，也可能没有；“没有 `ReplyEndEvent` / 没有可重放 assistant reply”不是可靠门控。主信号仍然应该是 terminal `RunEndEvent.status == "failed"`。

### 1.5 `seen_replies` guard 会吞掉同 reply_id 的后续事件

当前 `rebuild_prior_messages()` 里：

```python
if event.reply_id in seen_replies:
    continue
if isinstance(event, ReplyEndEvent):
    seen_replies.add(event.reply_id)
    messages.append(event_log.replay(event.reply_id))
```

如果未来要处理 `RunEndEvent`，不能把 `RunEndEvent` 分支放在这个 guard 后面。因为 `RunEndEvent` 和最后一个 reply 通常共享同一个 `reply_id`，它会在 `ReplyEndEvent` 之后被 guard 跳过。

实现时必须先处理 terminal run state，或把 `seen_replies` guard 限定到 `ReplyEndEvent` replay 分支。

## 2. 修正版契约

### 2.1 主信号

以 `RunEndEvent.status == "failed"` 作为是否需要失败旁注的主信号。

不要依赖：

- `RunErrorEvent` 是否存在：`max_turns` 失败路径可能只有 `ExceedMaxItersEvent` + `RunEndEvent(status="failed")`。
- `RunErrorEvent.code`：它和 `RunEndEvent.stop_reason` 不是同一套词表。例如 tool budget 的 code 是 `tool_budget_exceeded`，stop_reason 是 `tool_error_budget`。
- 是否存在 `ReplyEndEvent`：常见 provider 失败路径仍然会发 `ReplyEndEvent`。

`stop_reason` 可以作为 metadata / 调试信息，但不作为必要条件。

补充一点：`aborted` 虽然在 `LoopStatus` 词表中存在，但当前 runtime loop 的正常终止路径实际主要产生 `finished`、`failed`、`waiting_user`。门控逻辑里可以把 `aborted` 视为未来兼容值，而不是当前主路径。

### 2.2 注入对象

注入一条固定、脱敏、轻量的旁注，作为 event log 到 prior messages 的投影结果。

建议文案：

```text
Pulsara note: the previous turn did not complete because the runtime/provider step failed. The user's input above was preserved. Any assistant text above from that turn may be partial or empty; if the user asks to continue, continue from the preserved input.
```

要求：

- 不包含 API key。
- 不包含 base_url。
- 不包含 raw stack trace。
- 不包含 provider_data。
- 不包含 retry traces。
- 不复制 `RunErrorEvent.message` 或 `RunEndEvent.error_message` 原文。

### 2.3 注入位置

旁注应该出现在该 failed run 的历史投影后面。

如果 failed run 有 assistant replay：

```text
UserMsg("上一轮用户输入")
AssistantMsg("可能为空或半截")
FailureNote
```

如果 failed run 没有 assistant replay：

```text
UserMsg("上一轮用户输入")
FailureNote
```

这样模型能同时看到原始输入、可能存在的半截输出、以及“上一轮没有完成”的事实。

### 2.4 注入寿命

只为最近一次 terminal run 是 failed 的情况注入旁注。

也就是说：

- turn 1 failed，turn 2 “请继续”时注入。
- turn 2 succeeded 后，turn 3 不再继续为 turn 1 注入旁注。
- 连续多次 failed 时，只为最近一次 failed run 注入，避免历史上下文中堆叠多条重复旁注。

这是一个产品选择：旁注用于帮助“继续上一轮失败的任务”，不是长期审计日志。审计仍然应该看 event log，而不是模型上下文。

### 2.5 SystemMsg vs UserMsg

语义上，旁注最适合是 `SystemMsg`：它不是用户原话，也不是 assistant 输出。

但当前代码库里 `SystemMsg` 生产路径基本未被使用，虽然 provider-neutral pipeline 和 Chat Completions / Responses adapter 都支持 message-level system，真实 provider 的行为仍需要用 real smoke 验证。

这里需要一个重要的实验纪律：如果某个 provider 在 real smoke 中出现“请求成功、thinking 正常、但 final text 为空”的现象，不能直接把它解释为“中段 `SystemMsg` wire shape 不兼容”。至少在 DashScope / Qwen 的一组对照实验中，同一条 message-level system 请求在较小 `max_output_tokens` 下只产出 thinking、不产出 final text；而在更大的 `max_output_tokens` 下会正常产出 final text。这说明真实 smoke 既在测通道，也在测模型是否把输出预算耗尽于 reasoning。

实施时有两个可选策略：

1. 优先使用 `SystemMsg`，并补一个真实 Responses API smoke test，确认 provider 接受中段 system message。
2. 若在合理 token 预算下，real smoke 仍然稳定表明 provider 不接受或无法正确消费中段 system message，再改用 user-role projection note；区分性必须写进 `content` 本身，例如直接使用固定的 `Pulsara note: ...` 文案；不要依赖 `UserMsg.name`，因为它不会下发到 provider。

无论选哪种，旁注都不能写回 canonical event log。

## 3. 实施建议

### 3.1 修改 `rebuild_prior_messages()`

建议分两步实现，避免 `seen_replies` guard 继续掩盖 terminal run state：

1. 先扫描 `event_log.iter()`，按 run 聚合：
   - `RunStartEvent`
   - `ReplyEndEvent`
   - terminal `RunEndEvent`
2. 再按事件顺序重建 messages：
   - 遇到 `RunStartEvent`：追加原有 `UserMsg`。
   - 遇到 `ReplyEndEvent`：只 replay 一次该 reply。
   - 在最近 failed run 的投影完成后追加 failure note。

也可以保持单 pass，但必须确保 `RunEndEvent` 分支位于 `seen_replies` guard 之前。

### 3.2 最近 failed run 的判定

从 event log 中找最后一个 `RunEndEvent`：

- 如果最后一个 `RunEndEvent.status == "failed"`，则这就是需要注入旁注的 run。
- 如果最后一个 terminal run 是 `finished` / `waiting_user`，则不注入任何失败旁注。
- `aborted` 可作为未来兼容值处理，但不是当前 runtime 主路径。

这比“为所有 failed run 注入”更克制，也避免上下文无界增长。

这个策略有一个明确的 scope boundary：它只处理“上一轮已经写下 terminal `RunEndEvent`”的失败。如果发生真正的硬崩溃，导致上一轮根本没有走到 `_finalize_run()`、没有写下 `RunEndEvent`，那么本计划不会为那一轮注入失败旁注。这类进程级硬崩溃不在本计划覆盖范围内。

### 3.3 不改变 event log

失败旁注只是 `rebuild_prior_messages()` 的返回值之一。

不能新增 `RunErrorEvent`，不能新增 durable event，不能把旁注持久化到 event log 或 memory graph。

## 4. 测试计划

### 4.1 聚焦 transcript 单元测试

优先新增直接针对 `rebuild_prior_messages()` 的测试，手工 seed `InMemoryEventLog`，不需要跑完整 runtime。

必须覆盖：

1. 成功 run：
   - `RunStart -> ReplyStart -> text -> ReplyEnd -> RunEnd(finished)`
   - 断言不出现失败旁注。
2. 失败 run 但有 `ReplyEndEvent`：
   - `RunStart -> ReplyStart -> RunError -> ReplyEnd -> RunEnd(failed)`
   - 断言出现失败旁注。
   - 断言空 assistant replay 不会阻止旁注。
3. 失败 run 且有半截 assistant：
   - `RunStart -> ReplyStart -> TextBlockDelta -> RunError -> ReplyEnd -> RunEnd(failed)`
   - 断言半截 assistant 保留，旁注追加在其后。
4. 脱敏：
   - `RunErrorEvent.message` / metadata 中放入假 API key、base_url、provider_data。
   - 断言 prior messages 中不包含这些原文。
5. 最近失败寿命：
   - failed run 后接 successful run。
   - 断言不再为旧 failed run 注入旁注。
6. `seen_replies` 回归：
   - 确认 `RunEndEvent` 与 `ReplyEndEvent` 共用 reply_id 时仍能被识别。
7. 抛异常路径：
   - 构造“有 `RunErrorEvent` 但没有 `ReplyEndEvent`”的失败形状。
   - 断言仍然依靠 `RunEndEvent.status == "failed"` 注入旁注。

### 4.2 runtime/host 集成测试

在 `tests/test_host_core.py` 中增加一个较轻的 host-level 测试：

- 第一轮通过 scripted transport 触发 failed run。
- 第二轮成功返回。
- 断言第二轮 `LLMContext.messages` 包含：
  - 第一轮用户输入。
  - 失败旁注。
  - 第二轮用户输入。

注意：默认 `max_consecutive_model_failures=2`，单次 `RunErrorEvent` 会进入 recovery，而不一定让 run failed。测试里要么设置 `LoopBudget(max_consecutive_model_failures=0)`，要么让 scripted transport 连续失败直到 error budget exceeded。

### 4.3 real smoke

如果选择 `SystemMsg`，需要单独跑一次真实 provider smoke：

- 构造 prior messages 中含中段 system message。
- 用 Responses API / Chat Completions API 各跑一条最小请求。
- 断言 provider 不拒绝该 wire shape，并且最终确实产出非空 final text。

这里的 smoke 需要留足 `max_output_tokens`，避免把“reasoning 耗尽输出预算”误判为“wire shape 不兼容”。经验上，不应使用过小的输出预算做这类兼容性判断；如果 provider 暴露了 thinking/reasoning token 消耗，优先选一个明显高于 reasoning 波动范围的预算。

只有当 real smoke 在合理预算下仍然稳定失败，才能把结论升级为“该 provider 不适合 message-level `SystemMsg`”。在那之前，不应仅凭空文本或只有 thinking 的一次现象，就直接退回 user-role projection note。

## 5. 非目标

本计划不做以下事情：

- 不修改 retry attempts / backoff / provider error classification。
- 不在 APIConnectionError 发生时自动重新执行上一轮。
- 不把失败旁注写入 memory。
- 不把 provider 原始错误暴露给模型。
- 不改变 `AgentRuntime` 同 run 内 recovery prompt。
- 不让失败旁注成为长期审计来源；审计仍然以 event log 为准。

## 6. 推荐实施顺序

1. 先写 transcript 单元测试，尤其是“failed run 仍有 ReplyEndEvent”的回归用例。
2. 修改 `rebuild_prior_messages()`，以 `RunEndEvent.status == "failed"` 和“最近 terminal run”作为主门控。
3. 补 host-level 测试，确认下一轮 “请继续” 能看到上一轮用户输入和失败旁注。
4. 如果使用 `SystemMsg`，补真实 provider smoke，并确保使用足够大的 `max_output_tokens` 做兼容性判断；只有在合理预算下仍稳定失败，才切换为标记型 `UserMsg`。
5. 跑：

```bash
uv run pytest tests/test_host_core.py -q
uv run pytest tests/test_agent_runtime_loop.py -q
uv run pytest -q
```
