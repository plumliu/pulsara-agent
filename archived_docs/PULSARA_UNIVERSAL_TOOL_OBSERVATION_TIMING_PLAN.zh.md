# Pulsara Universal Tool Observation Timing hard-cut 计划

## 0. 结论

我们决定直接 hard-cut 到长期方案：

- 所有工具结果都拥有 Pulsara-owned `ToolObservationTiming`。
- 通用 timing 不再从工具业务输出 JSON 的顶层 `timing` 字段推断。
- terminal / terminal_process 是特殊 built-in tool：它们可以继续在自身 payload 中携带 terminal-domain `timing`，但这不是 universal timing 的事实源。
- MCP / custom / filesystem / memory / artifact_read 等工具不需要、也不应该修改自己的业务 payload schema。
- context renderer 统一把 `ToolObservationTiming` 渲染成模型可见 timing header / envelope，并把预算、cache、inspect 都挂在这个 Pulsara-owned fact 上。

这个设计的核心边界是：

> 工具输出属于工具；工具观察事实属于 Pulsara runtime。

因此，外部 MCP server 返回：

```json
{"timing": {"phase": "p95"}, "result": "..."}
```

时，`timing` 只是 MCP 的业务字段，不是 Pulsara timing。Pulsara 只能相信自己在 executor / event log / runtime metadata 中记录的 observation timing。

## 1. 为什么要 hard-cut

上一轮 terminal timing 实现解决了 terminal / terminal_process 长跑日志的时间感知问题，但留下了一个设计隐患：

- `src/pulsara_agent/runtime/context_engine/tool_results.py` 里的 `_tool_timing_payload(parsed)` 只要看到任意 JSON 顶层 `timing` dict，就把它当成 Pulsara timing。
- artifact-backed envelope 会把这个 `timing` 提升到外层 envelope。
- inspector 会把它投影成 `tool_result_timings`。
- render cache 的 high-fidelity 判定也会受这个字段影响。

这会误伤所有非 terminal 工具：

- MCP structured content 可能有业务字段 `timing`。
- custom tool 可能返回 profiling JSON。
- memory / search / filesystem 工具未来也可能自然使用 `timing` 作为业务字段。

这个问题和之前 “非 terminal JSON 不应被 terminal essential envelope 误伤” 是同一类边界错误：renderer 不能靠 payload shape 猜工具语义。

所以 V2 不做“加强 schema 判断”的小修，而是直接改成：

- payload timing 只对 terminal-like built-ins 有意义；
- universal timing 从 runtime fact 注入；
- renderer 不再读取任意 payload 的 `timing` 作为通用 observation timing。

## 2. 当前代码事实

当前已经有足够的 execution timing 原材料：

- `ToolResultStartEvent.created_at`
- `ToolResultEndEvent.created_at`
- `ToolExecutionResult.metadata`
- `ToolExecutionSuspended` payload
- `ToolRuntimeContext`
- `Msg.created_at` / `Msg.finished_at`
- live run 中 `_tool_result_message_from_events()` 已经能从 start/end event 写入 `Msg.metadata["source_timing"]`
- terminal / terminal_process payload 现在已经有 built-in payload timing

主要缺口不是“没有时间”，而是时间事实的 ownership 错了：

- 当前 tool-result renderer 从 payload JSON 猜 timing；
- generic timing 没有独立 DTO；
- `ToolResultRenderUnit` 没有携带 universal tool observation fact；
- cache / inspect / diagnostics 与 payload-derived timing 绑定。

## 3. 设计原则

### 3.1 Pulsara-owned，不信任工具 payload

通用 tool observation timing 的唯一事实来源是 Pulsara runtime：

- executor 什么时候开始执行工具；
- executor 什么时候拿到最终结果；
- runtime 什么时候 suspend / resume；
- artifact service / event log 什么时候 finalize result；
- descriptor / registry 认定这个工具来自 builtin、MCP 还是 custom。

工具 payload 中的任何字段，包括 `timing`、`observed_at`、`duration`，默认都只是业务输出。

### 3.2 terminal / terminal_process 是特殊 built-in

terminal 和 terminal_process 的 JSON payload 是 Pulsara 自己定义的 operational payload，因此可以继续包含 terminal-domain timing：

```json
{
  "status": "finished",
  "exit_code": 0,
  "output": "...",
  "timing": {
    "observed_at": "2026-07-09T12:34:56Z",
    "duration_seconds": 12.3,
    "freshness": "current_tool_observation",
    "clock_source": "tool_payload"
  }
}
```

但这层 timing 只表示 terminal payload 的业务状态，不再作为 generic renderer timing 的事实源。

universal timing 仍然来自 `ToolObservationTiming`。两者可以同时存在：

- `ToolObservationTiming`：Pulsara 观察到这个 tool result 的执行事实。
- terminal payload `timing`：terminal backend 观察到命令 / background process 的状态事实。

它们通常接近，但不要求完全相同。例如 terminal payload 可以在命令完成时生成，generic timing 可以在 artifact processing 后写入 `ToolResultEndEvent`。

### 3.3 不污染 MCP / custom structured content

MCP tools 的 text / image / resource / structured content 不改 schema。

如果一个 MCP server 返回：

```json
{"docs": [...], "timing": {"phase": "server-search"}}
```

Pulsara renderer 不把 `timing` 提升、不解释、不删除。它只是原样业务 payload。

模型可见的 Pulsara timing 通过 Pulsara-framed tool-result header / envelope 进入上下文。

### 3.4 timing 是 context rendering fact，不是用户消息

通用 timing 不应伪装成用户输入。它属于 runtime-provided tool observation header。

V1 冻结为 A+ 方案：full raw tool result 是 Pulsara-framed raw payload。

也就是说，模型可见的完整 tool result content 不承诺整体 `json.loads()` 成功。Pulsara 只承诺：

- 原始 payload region 在 header 后原样保留。
- 如果原始 payload 是 JSON，则 header 后的 payload region 仍是 parseable JSON。
- full raw path 不把业务 payload 包进新的 JSON root，也不插入 `pulsara_*` 字段污染原始结构。
- compact / artifact / essential path 可以使用 Pulsara JSON envelope；那是明确的 rendered envelope，不是 raw payload。

这个 tradeoff 必须写死，否则实现会在 full raw path 用 header、compact path 用 envelope，最后让模型和 inspect 都难以判断“这里看到的是原始 payload 还是 Pulsara envelope”。

推荐把 observation timing 合并进现有 tool-result framing，而不是额外增加第二行 header：

```text
[tool_result:docs_search:success; observed_at=2026-07-09T12:34:56Z; observation_duration=1.23s; freshness=current_tool_observation; origin=mcp]
<original payload>
```

如果原始 payload 是 JSON：

```text
[tool_result:custom_tool:success; observed_at=2026-07-09T12:34:56Z; observation_duration=1.23s; freshness=current_tool_observation; origin=custom]
{"timing": {"phase": "p95"}, "result": "..."}
```

此时整体 content 不是 JSON，但 header 后的 payload region 仍是原始 JSON。

选择 A+ 而不是 “JSON full raw 也包成 Pulsara envelope” 的原因：

- 不改变工具 payload 结构。
- 不让模型误以为工具原始输出里有 Pulsara-owned field。
- 和 text / JSON / MCP structured content 一致。
- 当前 renderer 已经有 `[tool_result:{tool}:{state}]` framing；A+ 是扩展既有 framing，不是引入新的 payload schema。

render decision 必须明确记录：

```json
{
  "framing": "pulsara_tool_result_header",
  "payload_preserved": true,
  "payload_format": "json | text | mixed | binary_ref | unknown"
}
```

当 tool-result 已经因为预算降级为 Pulsara envelope（artifact preview、essential envelope、metadata-only envelope）时，可以把 timing 放进 envelope 的 Pulsara-owned 字段：

```json
{
  "pulsara_tool_observation": {
    "observed_at": "2026-07-09T12:34:56Z",
    "observation_duration_seconds": 1.23,
    "freshness": "current_tool_observation",
    "tool_origin": "mcp"
  },
  "output_preview": "...",
  "output_truncated": true,
  "artifacts": [...]
}
```

## 4. DTO：ToolObservationTiming

建议新增低层 DTO，放在 `event/events.py` 可安全引用的低层位置，或单独的 `event/tool_observation.py`，避免 context renderer 反向 import runtime parser。

```python
class ToolObservationTiming(BaseModel):
    observed_at: str
    source_started_at: str | None = None
    source_ended_at: str | None = None
    observation_duration_seconds: float | None = None
    tool_reported_duration_seconds: float | None = None
    freshness: Literal[
        "current_tool_observation",
        "background_process_observation",
        "historical_tool_observation",
        "suspended_tool_observation",
        "unknown",
    ] = "current_tool_observation"
    clock_source: Literal[
        "tool_result_events",
        "tool_runtime_metadata",
        "mixed",
    ] = "tool_result_events"
    tool_origin: Literal["builtin", "mcp", "custom", "workflow", "subagent_system", "unknown"] = "unknown"
    tool_name: str | None = None
    tool_call_id: str | None = None
    suspended_at: str | None = None
    resumed_at: str | None = None
```

字段语义：

- `observed_at`：必填，UTC ISO timestamp。通常等于 final `ToolResultEndEvent.created_at`。
- `source_started_at`：工具执行开始时间，通常来自 `ToolResultStartEvent.created_at`。
- `source_ended_at`：工具结果完成时间，通常来自 `ToolResultEndEvent.created_at`。
- `observation_duration_seconds`：Pulsara observation duration，即 `ToolResultStartEvent.created_at` 到 `ToolResultEndEvent.created_at` 的 wall-clock 差值。它可能包含 artifact processing、result persistence、adapter overhead；这是 runtime observation 耗时，不等同于工具内部执行耗时。
- `tool_reported_duration_seconds`：可选，只能来自 trusted built-in runtime metadata，例如 terminal backend 自己报告的命令执行耗时。MCP / custom payload 里的 `duration`、`timing.duration` 默认不能进入此字段。
- `freshness`：描述这次 observation 在当前 compile 中的语义。
- `clock_source`：说明 timing 由哪个 runtime 层产生。
- `tool_origin`：builtin / mcp / custom 等，用于 inspector 和模型理解工具来源。
- `suspended_at` / `resumed_at`：MCP input-required、approval、external execution 等 suspend/resume 路径使用。

约束：

- 新生产路径的 completed `ToolResultEndEvent` 必须有 tool observation timing。
- 组件级 unit test 可以用 helper 构造 timing；不能让生产路径缺失后靠 payload fallback。
- 不支持从任意 JSON payload 自动构造 `ToolObservationTiming`。
- production runtime 不允许把 `permission` / `descriptor` / payload 猜测结果混成 `tool_origin`。`tool_origin` 必须来自 capability descriptor / provider metadata / registry-owned classification。
- production `AgentRuntime` path 必须给 `ToolExecutor.execute(... descriptor=...)` 传入 descriptor；`descriptor is None` 时 `tool_origin="unknown"` 只允许 component tests、direct executor tests，或专门测试 unsupported legacy replay diagnostic 的 fixture。它不能成为生产 replay 兼容路径。
- production fail-closed path 里的 unknown tool / descriptor-missing denial 是例外：`capability_descriptor_missing`、`unknown_tool` 这类稳定 deny 可以使用 `tool_origin="unknown"`，但必须带 stable reason code / diagnostic，不能伪装成正常 unknown-origin tool execution。
- MCP origin 以 descriptor/provider 为准，不按 tool name 前缀、mangled name、server name字符串猜。

## 5. Event / message 承载方式

### 5.1 推荐承载

最小改动但足够硬的承载方式：

- `ToolResultEndEvent.metadata["tool_observation_timing"] = ToolObservationTiming.model_dump()`
- `ExternalExecutionResultEvent.metadata["tool_observation_timing_by_call_id"] = {tool_call_id: ToolObservationTiming.model_dump()}`
- `Msg.metadata["tool_observation_timing_by_call_id"][tool_call_id] = ...`
- 单 block tool-result message 可额外设置 `Msg.metadata["tool_observation_timing"] = ...`
- `ToolResultRenderUnit.tool_observation_timing = ToolObservationTiming | None`

如果我们愿意做更强 schema hard-cut，可以把 `tool_observation_timing` 提升为 `ToolResultEndEvent` 的 required typed field。

我更建议 V1 先用 EventBase.metadata 承载，但以 contract/test 保证生产路径必填。原因：

- `EventBase.metadata` 已存在，不需要立刻改所有 event serialization union。
- ToolResultEndEvent 的人工构造点很多，PR 可以更小。
- Inspector / renderer 仍然只认 Pulsara-owned metadata，不认 payload。

无论是否作为 typed field，语义都必须 hard-cut：

> 新生产路径的 ToolResultEndEvent 没有 `tool_observation_timing` 是 contract error。

### 5.1.1 Hard-cut compatibility boundary

V1 不提供旧事件日志的 runtime 兼容兜底：

- 新生产 event log 中，completed tool result 必须能通过 `ToolResultEndEvent.metadata["tool_observation_timing"]` 或等价 typed field 恢复 `ToolObservationTiming`。
- `ExternalExecutionResultEvent` 必须能通过 `metadata["tool_observation_timing_by_call_id"]` 恢复每个 block 的 timing。
- suspend/resume 必须能通过 `tool_observation_timing_seed` 恢复 original start。
- 缺这些事实的旧 log 是 unsupported schema；inspector / recovery 可以报告 contract error，但不能用 payload `timing`、当前 wall-clock、session default 或工具名猜测 timing。

如果开发阶段需要继续查看旧数据，应该走显式 migration / diagnostic 工具，而不是让 runtime path 静默兼容。

### 5.2 ExternalExecutionResultEvent 旁路

`ExternalExecutionResultEvent` 是当前事件系统里一条 tool-result 旁路：assembler 会把 `execution_results` 直接转成 `ToolResultBlock`，没有 `ToolResultStartEvent` / `ToolResultEndEvent` span 参与。

V1 冻结为：保留 `ExternalExecutionResultEvent`，但事件必须携带 `metadata["tool_observation_timing_by_call_id"]`，key 为每个 `ToolResultBlock.id`。不带 timing map 的 `ExternalExecutionResultEvent` 是 unsupported schema。

规则：

- `execution_results` 中每个 `ToolResultBlock.id` 都必须在 timing map 中有 entry。
- assembler / transcript rebuild 必须把该 timing map 投影到 rebuilt `Msg.metadata["tool_observation_timing_by_call_id"]`。
- 缺 timing map 的 `ExternalExecutionResultEvent` 在 hard-cut 后是 unsupported schema；不得 fallback payload `timing`。
- 如果某个 external execution result 只能证明事件级时间，`source_started_at` / `source_ended_at` 可等于 `ExternalExecutionResultEvent.created_at`，但必须 `clock_source="tool_runtime_metadata"` 或 diagnostic 标明 `external_execution_event_time_only`。

### 5.3 Suspended tool timing seed

MCP input-required / external execution / approval suspension 有一个特殊问题：async tool 返回 `ToolExecutionSuspended` 后不会进入 `_finalize_result()`，也不会产生 completed `ToolResultEndEvent`。因此 original call start timing 必须在 suspend 阶段持久化，否则 resume 时只能用当前时间重新合成 start，这是错误的。

V1 冻结一个 Pulsara-owned seed：

```python
ToolObservationTimingSeed = {
    "tool_call_id": str,
    "tool_name": str,
    "tool_origin": "builtin | mcp | custom | workflow | subagent_system | unknown",
    "source_started_at": str,
    "suspended_at": str,
    "start_event_id": str | None,
    "start_event_sequence": int | None,
    "source_context_id": str | None,
    "source_model_call_index": int | None,
}
```

持久落点：

- `ToolExecutionSuspended.payload["tool_observation_timing_seed"]`
- `CustomEvent(name="tool_execution_suspended").value["tool_observation_timing_seed"]`
- `LoopState.pending_interaction_payload["tool_observation_timing_seed"]`
- 如果未来把 tool suspension 升级为 typed event，也必须保留同名字段或等价 typed DTO。

约束：

- executor 是 seed 的首要生成者：`ToolExecutor.execute_async()` 在收到 `ToolExecutionSuspended` 时，必须把 original start event facts 写入 `ToolExecutionSuspended.payload["tool_observation_timing_seed"]`；runtime 只能补充/校验，不能重新发明 original start。
- suspend path 必须在写 pending payload 前生成 seed。
- seed 的 `source_started_at` 必须来自 original `ToolResultStartEvent.created_at` 或 start event 的 stored event；不能用 suspend/resume 时的当前时间替代。
- seed 必须包含 `start_event_id` / `start_event_sequence`，若 event log adapter 尚未返回 stored id/sequence，则字段可为 null，但必须写 diagnostic。
- resume path 只能从 seed 延续 original start：final `ToolObservationTiming.source_started_at = seed.source_started_at`。
- final resumed timing 必须填 `suspended_at`、`resumed_at`、`observed_at`；`observation_duration_seconds` 表示从 original start 到 final observed 的 wall-clock，必要时另在 diagnostics 标明包含 suspended waiting time。

### 5.4 Msg metadata

`Msg.metadata["source_timing"]` 仍保留给 section-level timing。

新增 tool-result 专用 metadata：

```python
Msg.metadata["tool_observation_timing_by_call_id"] = {
    "call:abc": {...ToolObservationTiming...}
}
```

单结果消息可以同步：

```python
Msg.metadata["tool_observation_timing"] = {...}
```

原因：

- 一个 `Msg` 可能包含多个 `ToolResultBlock`。
- renderer 需要按 `ToolResultBlock.id` 找到准确 timing。
- 不把 timing 塞进 `ToolResultBlock.output`，避免污染业务 payload。

### 5.5 start/end event span

`ToolResultStartEvent.created_at` 与 `ToolResultEndEvent.created_at` 是最稳定的通用来源。

实现必须注意：

- `RuntimeEventSpan` 只保存 sequence / source_event_id，不保存 wall-clock。
- 如果通过 span 推导 timing，必须用 sequence / source_event_id 回查对应 event 的 `created_at`。
- live path 可以直接用 batch events 中的 start/end event。
- replay path 可以从 event log rebuild message metadata。

## 6. Renderer hard-cut

### 6.1 删除 generic payload timing inference

必须删除或重写当前语义：

```python
def _tool_timing_payload(parsed):
    timing = parsed.get("timing")
    return dict(timing) if isinstance(timing, dict) else None
```

新的规则：

- generic tool observation timing 只从 `ToolResultRenderUnit.tool_observation_timing` 读取。
- 非 terminal tool payload 的顶层 `timing` 永远不触发 `timing_policy != "not_applicable"`。
- 如果需要读取 terminal payload timing，只能在 terminal-specific helper 中读取，并且必须要求 `block.name in {"terminal", "terminal_process"}` 或明确 terminal descriptor。

### 6.2 ToolResultRenderUnit

在 tool-result allocator collect 阶段加入：

```python
@dataclass(frozen=True, slots=True)
class ToolResultRenderUnit:
    ...
    tool_observation_timing: ToolObservationTiming | None
```

来源优先级：

1. `message.metadata["tool_observation_timing_by_call_id"][block.id]`
2. `message.metadata["tool_observation_timing"]`，仅当 message 内只有一个 ToolResultBlock
3. 从 `message.created_at` / `message.finished_at` 派生 minimal timing，仅限 direct component test，或 replay adapter 已经从 supported event facts 构造该 message、但尚未把 timing map 写入 metadata 的内部过渡点；不要从 business payload 推断，也不要把它用于 unsupported old log 兜底

如果新生产路径缺失 timing：

- production/replay：contract error；context compile 必须写 `ContextCompiledEvent(status="failed")` 或等价 durable structured diagnostic，并且不得发起 model call；
- direct component test：允许 `timing_policy="not_applicable"`，但必须标明这是 test/direct construction path。
- old event log 在 hard-cut 后属于 unsupported schema；不得 fallback 到 payload `timing`，也不得从当前 wall-clock 推断。

### 6.3 模型可见格式

V1 full raw result 使用 Pulsara-framed raw payload。Observation timing 合并进现有 tool-result header：

```text
[tool_result:<model_tool_name>:<state>; observed_at=...; observation_duration=...; freshness=...; origin=...]
<rendered tool output>
```

约束：

- 不承诺整个 tool result content 是 parseable JSON。
- 承诺 header 后的 raw payload region 原样保留。
- 如果 raw payload region 是 JSON，它自身仍应 parseable。
- 不因为 timing 把 full raw JSON 改包成 `{"pulsara_tool_observation": ..., "tool_result": ...}`。

对于 essential / artifact / compact envelope，使用 JSON 字段：

```json
{
  "pulsara_tool_observation": {...},
  "output_preview": "...",
  "output_truncated": true,
  "artifacts": [...]
}
```

字段名必须用 `pulsara_tool_observation`，不要用通用 `timing`，避免再次和业务 payload 撞名。

render decision 必须标明当前 shape：

- `framing="pulsara_tool_result_header"`：full raw path。
- `framing="pulsara_tool_result_envelope"`：compact / artifact / essential envelope。
- `payload_preserved=true` 只适用于 full raw path；envelope path 必须说明 `body_policy` / `output_truncated`。

### 6.4 Budget / cache

timing header / envelope 必须计入：

- `rendered_envelope_chars`
- `rendered_total_chars`
- total hard cap
- per-envelope cap
- render cache entry weight

render decision 新增或继续使用：

```json
{
  "tool_observation_timing": {...},
  "timing_policy": "full | minimal | omitted_for_cap | not_applicable",
  "rendered_timing_chars": 123,
  "timing_clock_source": "tool_result_events",
  "timing_origin": "mcp"
}
```

cache 规则：

- timing 来自 event facts，是稳定事实，可以 cache。
- cache key / unit fingerprint 必须包含 `tool_observation_timing` 的 stable fingerprint，或至少包含 start/end timestamps + tool_call_id + state。
- 如果 timing 被 `omitted_for_cap`，该 degraded render 不应写入 canonical high-fidelity cache。
- cache reuse 时必须恢复 `timing_policy`、`rendered_timing_chars`、`tool_observation_timing`，不能只恢复 rendered body。

### 6.5 小预算降级

降级顺序：

1. full timing header / envelope
2. minimal timing：`observed_at`、`observation_duration_seconds`、`freshness`、`tool_origin`
3. omitted for cap，并写 diagnostic：`tool_observation_timing_omitted_for_envelope_cap`

任何情况下都不能为了 timing 突破 `tool_result_context_chars` hard cap。

如果 JSON envelope 放不下 timing，必须输出 parseable JSON，不允许半截 JSON。

## 7. terminal / terminal_process 特例

terminal / terminal_process 保留 payload timing，但它被降级为 terminal-domain field：

- `payload["timing"]`：terminal backend observation。
- `ToolObservationTiming`：executor/event observation。

renderer 不再用 `payload["timing"]` 作为 generic timing source。

terminal-specific essential envelope 可以同时包含：

```json
{
  "pulsara_tool_observation": {...},
  "timing": {...terminal payload timing...},
  "status": "finished",
  "exit_code": 0,
  "process_id": "..."
}
```

如果预算不足：

- 优先保留 `pulsara_tool_observation` minimal subset，因为它是统一 runtime fact。
- terminal payload `timing` 作为 terminal operational metadata，可按 terminal envelope policy 裁剪。
- 必须记录 diagnostic 区分：
  - `tool_observation_timing_omitted_for_envelope_cap`
  - `terminal_payload_timing_omitted_for_envelope_cap`

terminal action 覆盖：

- terminal execute / blocked / denied / error
- terminal_process list / log / poll / wait / kill / write / submit / close_stdin / error
- streaming delta 不要求携带 final timing；finish payload 与 `ToolResultEndEvent.metadata["tool_observation_timing"]`（或等价 typed field）必须存在

## 8. MCP / suspend / resume

MCP 不改 payload schema。

普通 MCP call：

- `ToolResultStartEvent.created_at` -> `source_started_at`
- `ToolResultEndEvent.created_at` -> `source_ended_at` / `observed_at`
- descriptor/provider -> `tool_origin="mcp"`

MCP input-required / suspended path：

- original call start：`source_started_at`
- suspend event：`suspended_at`
- resume action：`resumed_at`
- final result end：`observed_at`

如果 V1 暂时无法完整建模 suspend/resume，至少要做到：

- suspended result 不伪造成 completed observation；
- final resumed result 的 `ToolObservationTiming` 包含 original `source_started_at` 和 final `observed_at`；
- 缺失 suspend/resume detail 时写 bounded diagnostic，不从 MCP payload 猜。

## 9. Inspector / diagnostics

Inspector 不重新读取工具 payload 中的 `timing`。

Normalized projection：

```json
{
  "tool_result_timings": [
    {
      "tool_call_id": "call:...",
      "tool_name": "docs-langchain.search_docs",
      "tool_origin": "mcp",
      "status": "available",
      "observed_at": "2026-07-09T12:34:56Z",
      "source_started_at": "2026-07-09T12:34:55Z",
      "source_ended_at": "2026-07-09T12:34:56Z",
      "observation_duration_seconds": 1.23,
      "tool_reported_duration_seconds": null,
      "freshness": "current_tool_observation",
      "clock_source": "tool_result_events",
      "timing_policy": "full",
      "rendered_timing_chars": 98,
      "diagnostics": []
    }
  ]
}
```

如果 direct test / old constructed message 没有 timing：

```json
{
  "status": "missing",
  "timing_policy": "not_applicable",
  "diagnostics": [{"code": "tool_observation_timing_missing"}]
}
```

生产 event log hard-cut 后，new runtime path 不应出现 missing。

## 10. 动刀落脚点

### 10.1 DTO / event contract

- `src/pulsara_agent/event/events.py`
  - 新增 `ToolObservationTiming` DTO，或从低层 event DTO 模块导入。
  - 冻结 `ToolResultEndEvent.metadata["tool_observation_timing"]` contract，或新增 typed field。
  - `ExternalExecutionResultEvent` 冻结 `metadata["tool_observation_timing_by_call_id"]` contract。

### 10.2 Tool executor

- `src/pulsara_agent/tools/executor.py`
  - emit `ToolResultStartEvent` 后保留 stored start event。
  - `_finalize_result()` 计算 `ToolObservationTiming`。
  - `ToolResultEndEvent` 写入 timing。
  - sync / async / exception result / artifact processed result 都覆盖。
  - production AgentRuntime path 必须传 descriptor；descriptor missing 只允许 test/direct path 或 unsupported legacy diagnostic fixture，并产生 `tool_origin="unknown"` diagnostic。
  - unknown tool / descriptor-missing deny 作为 production fail-closed 例外可以 `tool_origin="unknown"`，但必须写 stable reason code / diagnostic。
  - async `ToolExecutionSuspended` path 不进入 `_finalize_result()`，必须在返回 suspended 前生成 timing seed，并交给 runtime 写入 pending payload / suspension event。

- `src/pulsara_agent/runtime/tool_loop.py`
  - `build_tool_result_error_events()` 也必须写 timing。

- `src/pulsara_agent/runtime/agent.py`
  - `_suspend_tool_execution()` 写 `tool_observation_timing_seed` 到 durable event 与 pending payload。
  - `_suspend_tool_execution()` 优先使用 `ToolExecutionSuspended.payload["tool_observation_timing_seed"]`，只能做 bounded validation / enrichment。
  - resume MCP pending interaction 时从 seed 延续 timing，不允许重新合成 original `source_started_at`。

### 10.3 Agent runtime live message

- `src/pulsara_agent/runtime/agent.py`
  - `_tool_result_message_from_events()` 从 start/end event 构造 `tool_observation_timing_by_call_id`。
  - 当前已有 `source_timing` 可保留给 section timing。

### 10.4 Message rebuild / transcript

- `src/pulsara_agent/message/assembler.py`
- `src/pulsara_agent/message/reducer.py`
- `src/pulsara_agent/runtime/transcript.py`

确保 replay 出来的 tool-result Msg 能带 `created_at` / `finished_at` / `tool_observation_timing_by_call_id`。

### 10.5 Context renderer

- `src/pulsara_agent/runtime/context_engine/tool_results.py`
  - 删除 generic `_tool_timing_payload(parsed)` 读取任意 payload timing 的语义。
  - `ToolResultRenderUnit` 加 `tool_observation_timing`。
  - `_render_tool_result_body()` / artifact envelope / essential envelope 使用 `pulsara_tool_observation` 或 header。
  - render decisions / cache / diagnostics 全部基于 DTO。

### 10.6 terminal built-ins

- `src/pulsara_agent/tools/builtins/terminal.py`
- `src/pulsara_agent/tools/builtins/terminal_process.py`

保留 payload timing helper，但改名/文档上明确为 terminal payload timing，例如：

- `terminal_payload_timing(...)`
- `terminal_timing_metadata_subset(...)`

不要让 context renderer 把它当作 universal timing。

### 10.7 Inspector

- `src/pulsara_agent/inspector/service.py`
  - `tool_result_timings` 从 render decision / event timing 投影。
  - 不从 rendered payload 反解析 `timing`。

## 11. PR 顺序

### PR0：文档与 immediate guard

- 先补回归：
  - 非 terminal / MCP-ish tool result 顶层包含 `timing` 时，`timing_policy == "not_applicable"`。
  - artifact normal envelope 不提升 outer `timing`。
- 可选 quick guard：若 PR1 不能同轮完成 universal DTO，可以临时把 `_tool_timing_payload()` 限定为 terminal-like + Pulsara schema 作为短期阻血。
- quick guard 不能成为 V1 最终路径。PR1–PR3 完成后必须删除 payload-derived generic timing，只保留 terminal-specific payload timing helper。

> hard-cut 的最终状态只有一条：generic tool observation timing 来自 `ToolObservationTiming`，不来自 payload JSON。

### PR1：ToolObservationTiming DTO + executor fact

- 新增 DTO。
- ToolExecutor 写 `ToolResultEndEvent.metadata["tool_observation_timing"]`。
- ExternalExecutionResultEvent 冻结 `tool_observation_timing_by_call_id`；不带 map 的事件是 unsupported schema。
- duration 语义改为 `observation_duration_seconds`；terminal-domain duration 保留在 terminal payload timing，必要时复制到 `tool_reported_duration_seconds`。
- production AgentRuntime path 要求 descriptor；MCP origin 从 descriptor/provider 来，不按名称猜。
- sync / async / exception / artifact / synthetic error events 全覆盖。
- 新增 helper：
  - `tool_observation_timing_from_events(start, end, result, descriptor)`
  - `tool_observation_timing_from_synthetic_event_pair(...)`
- suspend path 新增 `tool_observation_timing_seed` 持久落点。

验收：

- read_file / search_files / artifact_read / memory / MCP mock / terminal 都有 timing。
- 不读取 payload `timing`。
- descriptor missing 在 production path fail / diagnostic；仅 component tests 可 `tool_origin="unknown"`。
- unknown tool / descriptor-missing denial 可以 `tool_origin="unknown"`，但必须有 stable reason code。
- suspended tool pending payload 和 suspension event 都有 timing seed。
- ExternalExecutionResultEvent replay 必须能从 timing map 恢复 timing；缺 map 为 contract error。

### PR2：message rebuild / render unit

- live path `_tool_result_message_from_events()` 写 `tool_observation_timing_by_call_id`。
- replay/reducer path 从 event log 恢复 timing。
- `ToolResultRenderUnit` 携带 timing。

验收：

- 当前 run tail tool result 有 timing。
- prior history replay tool result 有 timing。
- 一个 Msg 多个 ToolResultBlock 时按 call id 匹配。

### PR3：renderer hard-cut

- 删除 generic payload timing inference。
- full raw result 扩展现有 `[tool_result:<tool>:<state>]` header，不承诺整体 JSON parseable，但必须原样保留 header 后 payload region。
- compact/artifact/essential envelope 使用 `pulsara_tool_observation`。
- timing 计入 budget/cache/decision。

验收：

- MCP-ish JSON 顶层 `timing` 不被解释。
- 所有工具结果 render decision 有 `tool_observation_timing` 或明确 diagnostic。
- JSON full raw path 记录 `framing="pulsara_tool_result_header"`、`payload_preserved=true`、`payload_format="json"`。
- 小预算下 parseable JSON，不破 hard cap。

### PR4：terminal special built-in 收口

- terminal payload timing helper 改名/注释，明确是 terminal-domain payload timing。
- terminal essential envelope 可以保留 payload `timing`，但 generic timing 使用 `pulsara_tool_observation`。
- 区分 generic timing omit diagnostic 与 terminal payload timing omit diagnostic。

验收：

- terminal artifact-backed normal envelope 同时能展示 generic observation 与 terminal payload timing。
- terminal_process poll/log/wait/list/write/submit/close_stdin timing 不回退到 payload guessing。

### PR5：MCP suspend/resume timing

- MCP普通 call 使用 executor timing。
- input-required suspend/resume 记录 suspended_at/resumed_at。
- final resumed result 继承 original start。
- resume 只能读取 `tool_observation_timing_seed`，不能用 resume 当前时间伪造 original start。

验收：

- input-required 不产生假的 completed timing。
- resume 后 final observation 可 inspect。
- seed 缺失时 fail closed / diagnostic，不 fallback payload timing。

### PR6：Inspector / dogfood

- inspector 展示 tool observation timing。
- 不从 rendered JSON 反解析。
- dogfood 一个 MCP docs search + 一个 terminal_process 长跑 replay，验证模型能区分当前观察和历史 replay。

## 12. 测试矩阵

必须覆盖：

- `test_non_terminal_json_timing_field_is_business_payload_not_pulsara_timing`
- `test_mcp_structured_content_timing_field_not_promoted_to_outer_envelope`
- `test_all_tool_results_get_executor_observation_timing`
- `test_tool_observation_duration_is_runtime_observation_duration_not_payload_duration`
- `test_tool_reported_duration_only_from_trusted_builtin_metadata`
- `test_production_tool_execution_requires_descriptor_for_origin`
- `test_unknown_tool_denial_allows_unknown_origin_with_reason_code`
- `test_external_execution_result_requires_tool_observation_timing_map`
- `test_external_execution_result_replay_restores_tool_observation_timing`
- `test_tool_result_observation_timing_rendered_for_plain_text_output`
- `test_tool_result_observation_timing_rendered_for_json_output_as_framed_raw_payload`
- `test_json_full_raw_tool_result_records_payload_preserved_and_payload_format`
- `test_artifact_backed_tool_result_uses_pulsara_tool_observation_not_payload_timing`
- `test_timing_header_counts_against_tool_result_total_budget`
- `test_timing_omitted_for_cap_does_not_cache_degraded_render`
- `test_replay_tool_result_restores_observation_timing_from_event_log`
- `test_terminal_payload_timing_is_terminal_domain_not_generic_source`
- `test_terminal_essential_envelope_distinguishes_pulsara_observation_and_terminal_payload_timing`
- `test_mcp_input_required_resume_records_suspend_resume_timing`
- `test_suspended_tool_persists_tool_observation_timing_seed`
- `test_tool_execution_suspended_payload_is_executor_runtime_timing_seed_bridge`
- `test_resume_uses_timing_seed_source_started_at`
- `test_missing_production_tool_observation_timing_fails_context_compile_without_model_call`
- `test_inspector_tool_result_timings_use_render_decisions_not_payload_json`

## 13. 和现有 timing header 文档的关系

`PULSARA_CONTEXT_TIMING_HEADER_PLAN.zh.md` 继续负责 section-level timing：

- compiled_at
- source timing
- memory / compaction / subagent section timing
- section lifecycle cache overlay

本文档负责 tool-result-level timing：

- executor/event-derived tool observation timing
- tool-result render header / envelope
- terminal payload timing 与 generic observation timing 的边界
- MCP / custom payload 不被污染

当本文档落地后，旧文档中 PR3/PR4 关于 `_tool_timing_payload(parsed)` / terminal payload timing 作为 renderer source 的描述应视为过时，由本文档替代。

## 14. 最终口径

最终我们希望模型看到的不是“某个工具 payload 里碰巧有 timing 字段”，而是：

```text
[tool_result:docs_search:success; observed_at=2026-07-09T12:34:56Z; observation_duration=1.23s; freshness=current_tool_observation; origin=mcp]
{"docs": [...], "timing": {"phase": "server-search"}}
```

header 中的 timing 来自 Pulsara 自己的 event log 和 executor，不来自外部工具业务输出。header 后的 JSON 仍是工具原始 payload，里面的 `timing` 只是业务字段。

terminal / terminal_process 可以继续拥有自己的 operational timing，因为它们是 Pulsara-owned built-ins；但 universal timing 仍统一走 `ToolObservationTiming`。

这条边界会让所有工具、所有 MCP 都自然支持 timing header，同时不会污染外部 schema，也不会让 renderer 被一个普通 JSON 字段牵着走。
