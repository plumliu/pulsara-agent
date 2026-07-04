# Pulsara Context Compaction Mid-turn Inline 实施文档

> PR T6 实施文档。本文把原 mid-turn inline compact 草案收敛成可落地的 V2.1 方案。
>
> V2.1 只做“follow-up model call 前压缩已完成历史 prefix”。它不在第一版总结当前 run 的 user input、assistant reply、tool call 或 tool result tail。当前 run tail 必须原样保留在 `LoopState.messages`，并作为下一次 model call 的后缀继续传入。

## 0. 设计审查结论

原草案方向是对的: run-end background auto compact 已经禁掉后，真正需要补的是同一个 run 内工具结果把上下文推高、但模型还要继续 follow-up 的场景。

不过原草案还不够可实施，主要缺口有六个:

1. safe point 没有落到当前 `AgentRuntime` 的具体函数。
2. “summary + tail” 的重建语义没有避免当前 run 被重复 replay。
3. `ContextCompactionService` 当前只能按 event log 自动选窗口，不能显式禁止 compact 当前 run。
4. typed event metadata 说了要加 `phase`，但没有定义 service API 如何透传。
5. direct-written compaction events 必须 publish，同时 active stream 也要 yield 给当前观察者。
6. 当前 `_after_tool_results()` 里旧的 `memory_hooks.should_compact()` 只是 diagnostic，不能和真正 inline compact 混成两个决策源。

本文档的实施策略是:

- V2.1 只 compact current run 之前的历史 prefix。
- current run 的 user input / assistant reply / tool result 作为 in-flight tail 保留。
- 如果上下文压力主要来自当前 tool result tail，V2.1 可以 skip，后续通过 artifact/clipping 或 V2.2 current-run segment compact 解决。
- inline compact 的 durable truth 仍是 `CONTEXT_COMPACTION_*` typed events 和 summary artifact。

## 1. 当前代码事实

### 1.1 HostSession timing

当前 V1 timing 已经是:

```text
HostSession.run_turn(user_input)
  -> _prepare_prior_messages_for_turn(user_input)
     -> preflight compact if needed
     -> rebuild prior messages
  -> AgentRuntime.run_task(user_input, prior_messages=...)
```

落点:

- `src/pulsara_agent/host/session.py`
- `_prepare_prior_messages_for_turn(...)`
- `_compact_if_needed_and_notify(...)`
- `compact_now()`

V2.1 不改变 HostSession preflight/manual 语义。HostSession 仍然负责:

- next-turn preflight auto compact;
- idle `:compact` manual compact;
- CLI compaction listener for preflight notice。

### 1.2 AgentRuntime loop

当前主循环落点:

- `src/pulsara_agent/runtime/agent.py`
- `AgentRuntime._stream_model_loop(...)`

当前工具 follow-up 路径是:

```text
_stream_model_loop(state, exposure)
  while state.status is RUNNING:
    _project_memory(state)
    context = build_llm_context(state, ...)
    llm_runtime.stream(...)
    assistant = event_log.replay(state.reply_id)
    state.messages.append(assistant)
    tool_blocks = _tool_call_blocks(assistant)
    state.pending_tool_calls = tool_blocks
    state.transition(CONTINUE_AFTER_MODEL)
    _execute_tool_blocks(...)
    _after_tool_results(...)
    state.transition(CONTINUE_AFTER_TOOL)
    state.begin_next_turn()
```

V2.1 inline compact 的自然 safe point 是:

```text
after _after_tool_results(...)
after status remains RUNNING
after state.transition(CONTINUE_AFTER_TOOL)
before state.begin_next_turn()
before next while iteration builds the next LLM context
```

也就是当前文件约在 `_stream_model_loop()` 的工具结果处理尾部，现有逻辑:

```python
state.transition(LoopTransition.CONTINUE_AFTER_TOOL)
state.begin_next_turn()
```

不要在四个路径里分别手写这段逻辑。要先抽一个共享 helper，然后所有 follow-up 入口都走它:

```python
async def _continue_after_tool_before_followup(self, state: LoopState) -> AsyncIterator[AgentEvent]:
    state.transition(LoopTransition.CONTINUE_AFTER_TOOL)
    async for event in self._maybe_compact_mid_turn_before_followup(state):
        yield event
    state.begin_next_turn()
```

这个位置满足:

- model stream 已结束；
- tool execution 已结束；
- tool side effects 已经写入 event log；
- unresolved approval / plan / MCP 会把 `state.status` 设为 `WAITING_USER` 并提前 break，不会走到这里；
- resolved approval / plan / MCP resume 分支也必须调用同一个 helper，不能只改 `_stream_model_loop()`；
- 下一次 model call 尚未构造 context。

### 1.3 ContextCompactionService 现有限制

当前 service 落点:

- `src/pulsara_agent/runtime/compaction/service.py`
- `ContextCompactionService.compact_if_needed(...)`
- `ContextCompactionService.compact(...)`
- `_build_plan(...)`

当前 `_build_plan(...)` 会从 latest completed boundary 后的 event log 自动选 candidate events，并用 `keep_recent_runs` 决定保留最近 run。对于 mid-turn 来说，这不够安全:

- current run 还没有 `RunEnd`；
- current run 的 `RunStartEvent`、assistant tool call、tool result 已经在 event log 中；
- 如果直接调用现有 `compact_if_needed(model_visible_messages=state.messages)`，service 可能把 current run events 纳入 summary；
- 即使 summary 后未来 rehydrate 能 replay tail，运行中 `LoopState.messages` 也可能与 rebuild 结果重复或错序。

所以 V2.1 必须给 service 增加“最大可 compact sequence”边界。

## 2. V2.1 目标语义

### 2.1 允许 compact 的内容

V2.1 只允许 compact:

```text
latest completed boundary keep_after_sequence 之后
且 current run RunStartEvent 之前
的历史事件 prefix
```

换句话说:

```text
compaction input <= current_run_start_sequence - 1
```

不得 compact:

- current run 的 `RunStartEvent`;
- current user input;
- current run 已完成 assistant reply;
- current run tool call;
- current run tool result;
- pending approval / plan / MCP elicitation payload;
- active skill/capability injections;
- memory projection prompt。

### 2.2 触发条件

触发估算仍使用 model-visible context:

```text
state.messages
+ current recovery note if any
+ current memory projection prompt effect
+ tool result clipped model view
```

V2.1 第一版可以先用 `state.messages` 作为估算输入，和 HostSession preflight 一样交给 `ContextCompactionService` 的 conservative estimator。后续如果要更精确，可增加一个 `estimate_llm_context_tokens(LLMContext)`，但不作为第一版前置。

compact 真正执行时，只压缩 current run 之前的历史 prefix。若模型可见上下文超阈值，但 current run 之前没有可压缩 prefix，则 skip，并记录 diagnostic:

```json
{
  "name": "mid_turn_compaction_skipped",
  "value": {
    "reason": "no_compactable_prefix_before_current_run",
    "current_run_id": "run:...",
    "current_run_start_sequence": 123
  }
}
```

### 2.3 成功后的运行中消息形态

成功后 `LoopState.messages` 必须变成:

```text
compacted historical prefix messages
+ preserved current run tail messages
```

其中 current run tail 来自 compact 前的 `state.messages`，不是从 event log replay 出来的。这一点很重要:

- current run 尚未 terminal；
- `rebuild_prior_messages()` 当前主要面向 completed prior turns；
- 直接全量 rebuild 可能把 current run 的 user/assistant 部分 replay 一次，再 append tail 一次，造成重复；
- tool result message ordering 必须保持 runtime 已构造的 provider-compatible 顺序。

V2.1 推荐把 tail 切分规则写死:

```python
def split_current_run_tail(state: LoopState) -> tuple[list[Msg], list[Msg]]:
    prefix = []
    tail = []
    in_current_run = False
    for message in state.messages:
        if message.metadata.get("run_id") == state.run_id or message.id == f"user-message:{state.run_id}":
            in_current_run = True
        if in_current_run:
            tail.append(message.model_copy(deep=True))
        else:
            prefix.append(message.model_copy(deep=True))
    return prefix, tail
```

实际实现可以更稳: 在 `RuntimeContextCompactor` 中用 current run start sequence 从 event log 定位，并只把 `state.messages` 中自 current run user message 起的后缀作为 tail。

### 2.4 失败语义

mid-turn compact 失败不得直接失败当前 agent run。失败处理:

- 写 `CONTEXT_COMPACTION_FAILED` typed event；
- publish direct-written events，避免 publisher sequence gap；
- yield failed event 给当前 stream observer；
- 保持原 `state.messages` 不变；
- 继续下一次 model call；
- 依赖 `ContextCompactionService` 现有 consecutive failure circuit breaker，避免每个 follow-up 都烧 compact model。

如果失败来自 programmer error，例如 state rewrite 后消息为空或 provider ordering 无效，测试应 fail fast；生产 runtime 不应吞掉这类 bug。

## 3. 新增接口

### 3.1 ContextCompactionService API 扩展

落点:

- `src/pulsara_agent/runtime/compaction/service.py`

为 `compact_if_needed(...)` 和 `compact(...)` 增加两个可选参数:

```python
async def compact_if_needed(
    self,
    *,
    current_user_input: str = "",
    model_visible_messages: list[Msg] | tuple[Msg, ...] | None = None,
    reason: str = "context_threshold",
    max_compactable_sequence: int | None = None,
    event_metadata: dict[str, object] | None = None,
) -> bool: ...

async def compact(
    self,
    *,
    trigger: ContextCompactionTrigger,
    reason: str,
    current_user_input: str = "",
    model_visible_messages: list[Msg] | tuple[Msg, ...] | None = None,
    force: bool = False,
    max_compactable_sequence: int | None = None,
    keep_recent_runs_override: int | None = None,
    event_metadata: dict[str, object] | None = None,
) -> ContextCompactionCompletedEvent | None: ...
```

`_build_plan(...)` 增加同名参数:

```python
candidate_events = [
    event
    for event in events
    if event.sequence is not None
    and event.sequence > last_keep_after
    and (max_compactable_sequence is None or event.sequence <= max_compactable_sequence)
]
```

注意:

- `estimated_tokens_before` 仍用 `model_visible_messages` 估算完整 model-visible context。
- `estimated_compaction_input_tokens_before` 只估算 candidate prefix。
- 如果 candidate prefix 为空，返回 `None`。
- 如果 current run 之前不足 `min_events_after_last_compact`，除非 force，否则返回 `None`。
- mid-turn 不直接复用 preflight 的 `keep_recent_runs`。V2.1 推荐由 caller 传 `keep_recent_runs_override=1`，即 current run 之前至少保留最近一个完整历史 run；如果产品后续想更激进，可调成 `0`。缺省 `None` 表示沿用 service policy，主要给 preflight/manual 保持现有语义。
- `_keep_after_sequence_for_recent_runs(...)` 应使用 `keep_recent_runs_override if keep_recent_runs_override is not None else self.policy.keep_recent_runs`。

event metadata merge 到 started/completed/failed:

```python
metadata = {
    "estimate_source": "model_visible_context",
    "estimated_compaction_input_tokens_before": plan.estimated_compaction_input_tokens_before,
    **(event_metadata or {}),
}
```

summary artifact metadata 也应包含这些 phase 字段，方便 inspector 解释。

### 3.2 RuntimeContextCompactor

新增模块:

- `src/pulsara_agent/runtime/compaction/inline.py`

建议接口:

```python
@dataclass(frozen=True, slots=True)
class MidTurnCompactionResult:
    compacted: bool
    events: tuple[AgentEvent, ...] = ()
    rewritten_messages: tuple[Msg, ...] | None = None
    skipped_reason: str | None = None


@dataclass(slots=True)
class RuntimeContextCompactor:
    event_log: EventLog
    archive: ArtifactStore
    runtime_session: RuntimeSession
    service: ContextCompactionService

    async def maybe_compact_before_followup(
        self,
        *,
        state: LoopState,
        model_visible_messages: list[Msg],
    ) -> MidTurnCompactionResult:
        ...
```

职责:

1. 确认 safe point:
   - `state.status is LoopStatus.RUNNING`
   - `state.last_transition is LoopTransition.CONTINUE_AFTER_TOOL`
   - `state.pending_interaction_kind is None`
   - `state.stop_request is None`
   - 若 `state.pending_tool_calls` 非空，所有 pending call 都必须已有 terminal tool result 覆盖。不能简单要求 `state.pending_tool_calls == []`，因为当前 runtime 在 `begin_next_turn()` 前可能仍保留刚执行完的 tool blocks。
2. 定位 current run start sequence。
3. 计算 `max_compactable_sequence = current_run_start_sequence - 1`。
4. 捕获 `before_sequence = event_log.next_sequence()`。
5. 调用 `service.compact_if_needed(...)`。
6. 在 `finally` 中 publish direct-written compaction events。
7. 如果 completed，构造 rewritten messages。
8. 返回需要 yield 的 stored compaction events。

publish 必须用 `finally`:

```python
before_sequence = event_log.next_sequence()
try:
    compacted = await service.compact_if_needed(...)
finally:
    events = compaction_events_after(before_sequence - 1)
    runtime_session.publish_stored_events(events)
```

这条和 manual `:compact` 一样，是 sequence 正确性，不是 UI 细节。

current run start 定位必须是硬算法:

```python
current_run_start = first RunStartEvent where event.run_id == state.run_id
if current_run_start is None or current_run_start.sequence is None:
    return MidTurnCompactionResult(
        compacted=False,
        skipped_reason="current_run_start_missing",
        diagnostics=(CustomEvent(name="mid_turn_compaction_skipped", ...),),
    )
max_compactable_sequence = current_run_start.sequence - 1
```

缺 `RunStartEvent`、sequence 缺失、或 `max_compactable_sequence <= latest_boundary.keep_after_sequence` 都必须 skip，不得 fallback 到全量 event log compact。

### 3.3 Noop / disabled compactor

为了不让大量单元测试都必须构造 real compactor，提供 disabled/noop:

```python
class NoopRuntimeContextCompactor:
    async def maybe_compact_before_followup(...) -> MidTurnCompactionResult:
        return MidTurnCompactionResult(compacted=False, skipped_reason="disabled")
```

`AgentRuntime` constructor 增加:

```python
context_compactor: RuntimeContextCompactorProtocol | None = None
```

默认 `None` 表示 disabled/noop。生产 wiring 如果存在 `runtime_wiring.compaction_service`，再注入 real `RuntimeContextCompactor`。

这不是旧兼容桥；这是因为 in-memory/test runtime 当前没有 `ContextCompactionService`，而 mid-turn compact 是可选能力。

## 4. Rehydration / Rewrite 策略

### 4.1 不要让 runtime import host

当前 `rebuild_prior_messages()` 在:

- `src/pulsara_agent/host/transcript.py`

V2.1 如果 `runtime/compaction/inline.py` 直接 import `pulsara_agent.host.transcript`，会制造 runtime -> host 的反向依赖。

推荐 PR T6.1 先做小迁移:

- 新增 `src/pulsara_agent/runtime/transcript.py`
- 将 `rebuild_prior_messages(...)` 和相关 helpers 从 `host/transcript.py` 移过去
- `host/transcript.py` 保留薄 re-export，避免现有 import 一次性大改

### 4.2 新增 prefix-only rehydration helper

`rebuild_prior_messages()` 当前会按 latest boundary replay boundary 后所有 events。mid-turn 成功事件的 sequence 会出现在 current run 事件之后，但 `keep_after_sequence` 指向 current run 之前。全量 rebuild 会把 current run replay 出来，不适合作为运行中 state rewrite 的 prefix。

新增 helper:

```python
def rebuild_prior_messages_before_sequence(
    event_log: EventLog,
    *,
    archive: ArtifactStore | None,
    session_id: str,
    before_sequence: int,
) -> list[Msg]:
    ...
```

语义:

- 使用 latest valid completed boundary；
- 可以使用刚刚写入的 mid-turn completed boundary，即便该 boundary event 的 sequence 大于 `before_sequence`；
- 但该 boundary 的 `keep_after_sequence` 必须小于 `before_sequence`，否则忽略该 boundary 并 fallback 到更早可用 boundary / full prefix replay；
- 加入 compaction summary system message；
- 只 replay `keep_after_sequence < event.sequence < before_sequence` 的事件；
- 不 replay current run；
- 不生成 terminal completion note for current run。

如果不想扩展公共 API，也可以把 helper 放在 `runtime/compaction/inline.py`，但长期更推荐放到 `runtime/transcript.py`，因为 inspector/rehydration 测试以后也会需要。

### 4.3 State rewrite

`RuntimeContextCompactor` 成功后:

```python
prefix = rebuild_prior_messages_before_sequence(
    event_log,
    archive=archive,
    session_id=runtime_session.runtime_session_id,
    before_sequence=current_run_start_sequence,
)
tail = current_run_tail_from_state(state)
state.messages = [*prefix, *tail]
state.compacted = True
state.scratchpad["mid_turn_compaction"] = {
    "compaction_id": completed.compaction_id,
    "phase": "mid_turn",
    "safe_point": "before_followup_model_call",
    "current_run_id": state.run_id,
    "current_run_start_sequence": current_run_start_sequence,
    "tail_message_count": len(tail),
}
```

tail invariants:

- first tail message must be current user message;
- tail must contain the assistant tool-call message that produced the just-finished tool results;
- tail must contain corresponding tool result messages;
- tail ordering must match provider tool-call/tool-result expectations;
- tail messages must be deep-copied before assignment。

## 5. AgentRuntime 接线

### 5.1 Constructor

落点:

- `src/pulsara_agent/runtime/agent.py`

增加字段:

```python
self.context_compactor = context_compactor or NoopRuntimeContextCompactor()
```

### 5.2 Inline safe point

新增方法:

```python
async def _maybe_compact_mid_turn_before_followup(
    self,
    state: LoopState,
) -> AsyncIterator[AgentEvent]:
    model_visible_messages = [message.model_copy(deep=True) for message in state.messages]
    result = await self.context_compactor.maybe_compact_before_followup(
        state=state,
        model_visible_messages=model_visible_messages,
    )
    if result.rewritten_messages is not None:
        state.messages = [message.model_copy(deep=True) for message in result.rewritten_messages]
    for event in result.events:
        yield event
```

再新增共享 continuation helper:

```python
async def _continue_after_tool_before_followup(
    self,
    state: LoopState,
) -> AsyncIterator[AgentEvent]:
    state.transition(LoopTransition.CONTINUE_AFTER_TOOL)
    async for event in self._maybe_compact_mid_turn_before_followup(state):
        yield event
    state.begin_next_turn()
```

接入位置:

- `_stream_model_loop(...)` 普通 tool follow-up path；
- `_stream_approval_resolution(...)` approval resume 后的 follow-up path；
- `_stream_plan_interaction_resolution(...)` plan interaction resume 后的 follow-up path；
- `_stream_mcp_elicitation_resolution(...)` MCP elicitation resume 后的 follow-up path。

所有这些地方都应替换原来的:

```python
state.transition(LoopTransition.CONTINUE_AFTER_TOOL)
state.begin_next_turn()
```

不要只改 `_stream_model_loop()`，否则 resume 后的 follow-up 会漏掉 mid-turn compact。

不要放在:

- `_after_tool_results()` 内部: 这个函数还承载 memory hook、tool persistence、旧 diagnostic；把 state rewrite 放进去会让职责混在一起。
- `build_llm_context()` 内部: context builder 应保持纯组装，不写 event log。
- HostSession: HostSession 看不到 tool-loop safe point。

### 5.3 Resume paths

这三个 resume path 当前都会在 resolved tool result 后进入 `_stream_model_loop(...)`:

- `_stream_approval_resolution(...)`
- `_stream_plan_interaction_resolution(...)`
- `_stream_mcp_elicitation_resolution(...)`

V2.1 策略:

- resolution 本身不触发 preflight compact；
- resolution 后如果工具结果已完成、状态回到 RUNNING、准备 follow-up model call，可以走同一个 mid-turn safe point；
- 但必须确保 pending interaction 已清空。
- 实现上必须通过 §5.2 的 `_continue_after_tool_before_followup(...)` helper 接入，避免四个分支行为漂移。

换言之，契约里的“suspended-run resume 不触发 auto compact”指的是 HostSession preflight；resume 后回到 active tool-follow-up safe point，V2.1 可以 compact 历史 prefix，但不能 compact pending payload 或 unresolved interaction。

第一版如果希望更保守，可以在 `RuntimeContextCompactor` 增加:

```python
if state.scratchpad.get("resumed_from_pending_interaction"):
    skip
```

但长期不必禁止，只要 safe point 已经在 resolution 后。

## 6. Runtime wiring

落点:

- `src/pulsara_agent/runtime/wiring.py`

`RuntimeWiring` 当前已经持有 `compaction_service`。在 `build_agent_runtime_wiring(...)` 中，`_with_memory_governance_engine(...)` 之后创建 compactor:

```python
context_compactor = (
    RuntimeContextCompactor(
        event_log=runtime_wiring.event_log,
        archive=runtime_wiring.archive,
        runtime_session=runtime_wiring.runtime_session,
        service=runtime_wiring.compaction_service,
    )
    if runtime_wiring.compaction_service is not None
    else None
)
```

然后传给 `AgentRuntime(...)`。

不要在 `build_durable_runtime_wiring(...)` 里创建 compactor。durable wiring 只组装 durable resources；AgentRuntime composition root 才负责 runtime behavior。

## 7. Event / Inspector / CLI

### 7.1 Event metadata

mid-turn compaction events 使用现有 typed event，metadata 至少包含:

```json
{
  "phase": "mid_turn",
  "safe_point": "before_followup_model_call",
  "current_run_id": "run:...",
  "current_run_start_sequence": 123,
  "max_compactable_sequence": 122,
  "tail_message_count": 4,
  "model_visible_message_count": 18
}
```

`reason` 固定:

```text
mid_turn_context_threshold
```

历史兼容:

- `preflight_context_threshold`: HostSession next-turn preflight。
- `run_end_context_threshold`: 仅历史事件可读，新代码不得产生。
- `mid_turn_context_threshold`: AgentRuntime follow-up safe point。

### 7.2 CLI

V2.1 第一版建议不新增 CLI 主动打印。

原因:

- preflight listener 是 HostSession 层通知；
- mid-turn events 发生在 active run 内；
- streaming 和 non-streaming REPL 的安全打印策略不同；
- 先让 event stream / inspector 可见，避免把 prompt 污染问题带回来。

如果要在 streaming REPL 显示，必须通过当前 run event stream 输出，不得后台 listener 在 idle prompt 后 print。

### 7.3 Inspector

Inspector 需要能显示:

- compaction phase: `preflight` / `mid_turn` / legacy `run_end`;
- safe point;
- current run id;
- max compactable sequence;
- tail message count;
- whether boundary was used by a later run。

可在现有 `inspect run/session` 的 compaction surface 中读取 typed event metadata，不需要新增 event type。

## 8. 旧 memory_hooks.should_compact 的处理

当前 `_after_tool_results()` 中:

```python
should_compact = await self.memory_hooks.should_compact(state)
if should_compact:
    state.compacted = True
    emit CustomEvent(name="compaction_requested")
```

这不是 context compaction 实现，只是旧 diagnostic hook。V2.1 必须避免两个决策源:

推荐第一版:

- 保留 hook 和 `compaction_requested` 事件，避免破坏现有 memory hook 测试；
- 不让它触发 `RuntimeContextCompactor`；
- 真正 inline compact 只由 `RuntimeContextCompactor` 基于 `ContextCompactionService` 阈值判断。

后续清理 PR 可以把 hook 重命名为 `memory_hooks.should_request_memory_compaction` 或删除，但不要混在 T6 第一版。

## 9. PR 拆分

### T6.1 Transcript helper extraction

落点:

- `src/pulsara_agent/runtime/transcript.py`
- `src/pulsara_agent/host/transcript.py`
- `tests/test_context_compaction.py`

内容:

- runtime 层提供 `rebuild_prior_messages(...)`。
- host 层保留 re-export。
- 新增 `rebuild_prior_messages_before_sequence(...)`。
- 覆盖 latest boundary + before_sequence 不 replay current run。

### T6.2 Service bounded compaction

落点:

- `src/pulsara_agent/runtime/compaction/service.py`
- `tests/test_context_compaction.py`

内容:

- `max_compactable_sequence` 参数。
- `event_metadata` 参数。
- summary artifact metadata 同步。
- 测试 current run 事件不进入 compact input。

### T6.3 RuntimeContextCompactor

落点:

- `src/pulsara_agent/runtime/compaction/inline.py`
- `tests/test_context_compaction.py`

内容:

- safe point guard。
- current run start sequence lookup。
- publish direct-written events in `finally`。
- success rewrite result。
- failure no state rewrite。

### T6.4 AgentRuntime wiring

落点:

- `src/pulsara_agent/runtime/agent.py`
- `src/pulsara_agent/runtime/wiring.py`
- `tests/test_agent_runtime_loop.py`

内容:

- constructor 接收 optional compactor。
- `_maybe_compact_mid_turn_before_followup(...)`。
- 插入 safe point。
- fake compactor 测试调用时机。

### T6.5 Inspector/event surface

落点:

- `src/pulsara_agent/inspector/service.py`
- inspector tests

内容:

- phase/safe_point/current_run_id projection。
- mid-turn boundary 解释。

### T6.6 Real dogfood

落点:

- `tests/test_real_llm_context_compaction.py` 或 dogfood 测试文档

内容:

- 长历史 + 当前工具结果 + follow-up。
- 确认 follow-up 仍能引用 current user intent 和 tool result。
- detach/resume 后 latest boundary 能 replay current run tail。

## 10. 最低测试矩阵

必须覆盖:

- model call 后、tool result 后、follow-up 前触发 compactor。
- final answer 无 tool call 时不触发。
- pending approval / plan / MCP elicitation 停在 WAITING_USER 时不触发。
- current run start sequence 之后的 events 不进入 compact summary。
- current user input 不出现在 summary artifact。
- current run assistant tool call 和 tool result 保留在 rewritten `state.messages` tail。
- rewritten `state.messages` 不重复 current user input。
- direct-written started/completed events 被 publish，后续 `runtime_session.emit(...)` 不 hang。
- compact failed event 也被 publish，后续 emit 不 hang。
- compact failure 不改变 `state.messages`，run 继续 follow-up。
- repeated failure 走 service circuit breaker。
- inspector 能显示 `phase="mid_turn"`。
- CLI idle prompt 后不出现后台 notice。

建议 targeted 命令:

```bash
uv run ruff check src/pulsara_agent/runtime/agent.py src/pulsara_agent/runtime/compaction src/pulsara_agent/runtime/transcript.py src/pulsara_agent/host/transcript.py tests/test_context_compaction.py tests/test_agent_runtime_loop.py tests/test_inspector.py
uv run pytest tests/test_context_compaction.py tests/test_agent_runtime_loop.py tests/test_inspector.py -q
```

最终合并前:

```bash
uv run ruff check src tests
uv run pytest -q
```

## 11. 编码坑位

1. 不要把 current run 放进 compacted prefix。
   `max_compactable_sequence` 必须小于 current run `RunStartEvent.sequence`。

2. 不要全量调用现有 `rebuild_prior_messages()` 后再 append tail。
   这会重复 replay current run。

3. 不要在 `build_llm_context()` 里写 event log。
   context builder 必须保持纯函数。

4. 不要只 publish success events。
   manual 和 mid-turn 都要在 `finally` publish direct-written started/failed events。

5. 不要通过 HostSession listener 打印 mid-turn notice。
   active run 事件要走 stream；第一版可以只 inspector 可见。

6. 不要把 active skill / capability prompt 写入 summary。
   exposure 每 turn 已经解析，follow-up 使用同一 exposure；summary 只承载 canonical conversation/tool facts。

7. 不要让 runtime 层 import host 层 transcript。
   先抽 `runtime/transcript.py`。

8. 小心 `state.begin_next_turn()` 会清空 `state.tool_results` 和 pending fields。
   inline compact 必须在它之前执行，否则 tail 分析会丢掉刚完成的 tool results。

9. 小心 projection。
   `_project_memory(state)` 在下一轮 while 顶部会重新执行；inline compact 成功后不要手动复制旧 `state.memory_projection` 到 summary。

10. 小心 latest boundary 的 event sequence。
    mid-turn completed event 本身 sequence 在 current run events 之后，但它的 `keep_after_sequence` 指向 current run 之前。这是允许的；rehydration 要按 `keep_after_sequence` replay tail。

## 12. 后续 V2.2: current-run segment compact

如果未来要处理“单个 current run tail 自己就巨大”的场景，需要单独设计 V2.2。那会比 V2.1 更危险，因为它要把当前 run 切成:

```text
current user intent
+ summarized earlier current-run segment
+ unsummarized latest tool result tail
```

V2.2 需要新增更强的 invariants:

- tool call/result pairing 不能被破坏；
- provider wire ordering 不能被破坏；
- summary 必须明确这是 same user intent 下的 earlier segment；
- latest actionable tool result 必须原样保留；
- side effects 不能重放。

不要把 V2.2 混进 V2.1。
