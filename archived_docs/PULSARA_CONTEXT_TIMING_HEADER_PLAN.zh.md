# Pulsara Context Timing Header 计划

## 0. 目标

让 Pulsara 通过 compiled prompt 更稳定地感知时间，尤其是在 replay 历史上下文、terminal / terminal_process 长跑日志、memory recall、compaction summary、subagent result 等场景中，明确告诉模型：

- 这段上下文是什么时候被编译进模型输入的。
- 这段上下文来自什么时候的事实。
- 这是 fresh observation、historical replay、cached snapshot、compaction handoff，还是 recalled projection。
- terminal / terminal_process 结果对应的命令或进程已经运行多久、最近一次观察是什么时候。

这不是新增事实源。Timing header 只是把已有 runtime/event/tool metadata 以轻量、可审计、模型可见的方式呈现出来。

## 1. 当前代码事实

### 1.1 已有时间来源

当前 Pulsara 已经有足够的时间原材料：

- `EventBase.created_at`：所有 typed events 都有创建时间。
- `Msg.created_at` / `Msg.finished_at`：rebuild 后的 runtime message 可携带消息时间。
- `ContextCompiledEvent.created_at`：一次 context compile 本身有 event time。
- `ContextCompiledEvent.sections`：已经保存 compiler section 的 event projection。
- `ContextSection.metadata` / `CompiledContextSection.metadata`：可以承载 section-level timing metadata。
- terminal / terminal_process payload 已经包含：
  - `status`
  - `exit_code`
  - `cwd`
  - `process_id`
  - `duration_seconds`
  - `terminal_session_id`
  - `backend_type`
  - `terminal_process_action`

### 1.2 主要缺口

这些时间事实目前大多停留在 runtime/event 层，模型不一定能看到。

典型问题：

- 历史 terminal log replay 时，模型只看到 log 内容，不知道它是刚刚产生还是上一轮产生。
- `terminal_process.log` / `poll` / `wait` 结果缺少模型可见的 `observed_at` / `last_output_at` / `elapsed` 语义。
- memory recall / working context 缺少统一的 “这是历史 projection，不是用户刚说的话” 的时间 envelope。
- compaction summary 能告诉模型它是 summary，但缺少 compacted_at / source window time range 的统一 header。
- ContextCompiledEvent 可以 inspect sections，但 section metadata 还没有统一 timing schema。

## 2. 设计原则

### 2.1 不给 base system prompt 加动态时间

`system:prompt` 是稳定 instruction，不应因为每次 compile 都加入动态 timestamp 而破坏 cache/stability。

但除了 base `system:prompt` 之外，所有 runtime-provided context section 都可以带 timing header。即使某些 section 当前 lowered 到 provider system message（例如 `handoff_hint`），它仍是 runtime context，不是 base instruction。

### 2.2 Section-level，不做逐行 timestamp

V1 不给每一行 log / 每个 message block 都加时间戳。默认只在 section header 或 tool observation envelope 上加一行紧凑时间信息。

原因：

- 控制 token 开销。
- 避免模型被时间格式噪音淹没。
- 保持 section 与 inspect projection 一一对应。

### 2.3 Wall-clock + event sequence 双轨

模型可见的时间使用 ISO 8601 / 带时区的人类可读时间；inspect 和 diagnostics 同时保留 event sequence range。

V1 至少区分：

- `compiled_at_utc`
- `source_started_at`
- `source_ended_at`
- `source_sequence_start`
- `source_sequence_end`
- `observed_at`
- `age_seconds`
- `freshness`

其中并非每个 section 都能填满所有字段。缺失时必须省略或标注 `unknown`，不能伪造。

### 2.4 Historical replay 必须显式标注

对模型最重要的不是绝对时间，而是“这是不是当前状态”。

因此 V1 timing header 必须能表达：

- `current_turn`
- `current_run_tail`
- `historical_replay`
- `compacted_history`
- `memory_projection`
- `current_tool_observation`
- `cached_snapshot`
- `background_process_observation`
- `subagent_result`

## 3. Timing metadata DTO

建议新增 Pulsara-owned DTO：

```python
@dataclass(frozen=True, slots=True)
class ContextSectionSourceTiming:
    observed_at: str | None = None
    source_started_at: str | None = None
    source_ended_at: str | None = None
    source_sequence_start: int | None = None
    source_sequence_end: int | None = None
    freshness: Literal[
        "current_turn",
        "current_run_tail",
        "historical_replay",
        "compacted_history",
        "memory_projection",
        "current_tool_observation",
        "cached_snapshot",
        "background_process_observation",
        "subagent_result",
        "unknown",
    ] = "unknown"
    clock_source: Literal["event_created_at", "message_created_at", "tool_payload", "compiler_wall_clock", "mixed"] = "mixed"


@dataclass(frozen=True, slots=True)
class ContextSectionRenderTiming:
    compiled_at_utc: str
    session_timezone: str | None = None
    compiled_local_date: str | None = None
    age_seconds: float | None = None
    source: ContextSectionSourceTiming = field(default_factory=ContextSectionSourceTiming)
```

落点：

- `ContextSection.metadata["source_timing"]`
- `CompiledContextSection.metadata["timing"]`
- `ContextCompiledEvent.sections[*].metadata.timing`
- 可选：`ContextCompiledEvent.metadata["compiled_at_utc"]`，但不是必须；event 自身已有 UTC `created_at`。

重要约束：

- `ContextSection.metadata["source_timing"]` 可以进入 lifecycle cache。
- `compiled_at_utc`、`age_seconds`、模型可见 timing header 必须是 lifecycle 之后的 render-time overlay。
- 不允许在 `_collect_sections()` 写入最终 `metadata["timing"].compiled_at_utc`，否则 `ContextLifecycleCoordinator` 复用旧 `ContextSection` 时会显示旧 compiled time。
- `dependency_fingerprint` 不得包含 `compiled_at_utc` / `age_seconds`。

V1 不建议新增数据库列。JSON event payload 足够。

## 4. 模型可见格式

### 4.1 通用 section header

对 non-system context section，V1 采用一行紧凑 header：

```text
[context timing: freshness=historical_replay; compiled_at_utc=2026-07-08T19:18:44Z; session_timezone=Asia/Shanghai; local_date=2026-07-09; source=2026-07-08T18:55:00Z..2026-07-08T19:10:12Z; age=8m32s]
```

如果字段太少：

```text
[context timing: freshness=memory_projection; compiled_at_utc=2026-07-08T19:18:44Z; source=unknown]
```

模型可见 header 的 `compiled_at_utc` 永远使用 UTC。若有 session timezone，可额外显示 `session_timezone` / `local_date`，但不要把本地时间伪装成 event truth。

命名约定：DTO / event metadata 字段名固定为 `compiled_local_date`；模型可见 header 为了短小可渲染成 `local_date`。实现不要同时持久化两份本地日期字段。

### 4.2 leading_user section

当前 `_leading_user_context_text()` 会生成：

```text
<pulsara_context>
The following sections are runtime-provided context for this turn...

## Recalled Memory and Working Context
...
</pulsara_context>
```

建议改为：

```text
## Recalled Memory and Working Context
[context timing: freshness=memory_projection; compiled_at_utc=...; source=...]
...
```

不要把 timing header 放在用户消息正文前面冒充用户输入。它仍在 `<pulsara_context>` 内，并保留 “runtime-provided context, not user requests” 的边界说明。

### 4.3 handoff_hint / subagent result

`handoff_hint` 当前可能被 lowered 到 system prompt。但它不是 base system instruction，而是 runtime context。

建议：

```text
## Subagent Results
[context timing: freshness=subagent_result; observed_at=...; source=...]
...
```

对于 `SubagentResultDeliveredEvent`，delivered 的语义仍必须绑定实际 `ModelCallStartEvent`。Timing header 不改变 delivery fact。

### 4.4 transcript sections

对 `transcript:prior_history`、`transcript:current_user`、`transcript:current_run_tail`：

- `current_user`：freshness=`current_turn`
- `current_run_tail`：freshness=`current_run_tail`
- `prior_history`：freshness=`historical_replay`
- `legacy_history`：freshness=`historical_replay`，并记录 `split=legacy`

V1 不要求改变 provider-native message 的每条 message content；可以先只把 timing 放入 `CompiledContextSection.metadata` 和 `ContextCompiledEvent.sections`。如果要模型可见 transcript timing，必须保证不破坏 provider-native tool-call/tool-result pairing。

Compaction summary 当前以 `SystemMsg` 形式混在 transcript replay 中，不是独立 `ContextSection`。V1 不在 PR1 强行提升为独立 section，也不对单条 transcript message 插入模型可见 timing header。PR1 只要求在 `transcript:prior_history` / `transcript:legacy_history` section metadata 中检测并记录 `compaction_summary_messages`，每条包含 `compaction_id`、`summary_artifact_id`、`compacted_at`、`keep_after_sequence`、`through_sequence`、`freshness="compacted_history"`。`source_sequence_start` 可以作为可推导字段补充；如果无法从上一条 boundary 或 event window 推导，则必须为 null/unknown。未来如果要让 compaction summary 拥有独立模型可见 timing header，必须单独设计 transcript split / de-dup，避免 summary 同时出现在 prior history 和独立 section 中。

## 5. terminal / terminal_process timing

这是最高优先级场景。

### 5.1 当前 terminal payload 的不足

当前 terminal payload 有 `duration_seconds` 和 `process_id`，但缺：

- `observed_at`
- `process_started_at`
- `last_output_at`
- `output_window_started_at`
- `output_window_ended_at`
- `freshness`

terminal process runtime 当前主要保存 monotonic time：

- `TerminalProcessState.started_at: float`
- `TerminalProcessInfo.started_at_monotonic`
- `duration_seconds`

monotonic time 适合计算 duration，但不适合作为模型可见 wall-clock timestamp。

### 5.2 V1 terminal timing envelope

V1 建议在 terminal / terminal_process tool result payload 增加 wall-clock observation metadata：

```json
{
  "timing": {
    "observed_at": "2026-07-08T19:18:44Z",
    "duration_seconds": 1112.3,
    "freshness": "background_process_observation",
    "clock_source": "tool_payload"
  }
}
```

terminal timing 必须由统一 helper 生成，例如：

```python
terminal_timing_payload(...)
```

`terminal_timing_payload(...)` 是 PR3 的事实生成边界。所有 terminal-like 模型可见 payload、`ToolExecutionResult.metadata`、artifact metadata 中的 timing 都必须从这个 helper 返回的同一份 dict 派生，不允许各调用点独立 `utc_now()`。

字段表：

| 字段 | Required | 说明 |
|---|---:|---|
| `observed_at` | yes | 当前工具观察完成/产生日志摘要时的 UTC ISO 8601；V1 可统一渲染为 `Z`，但 helper 必须保证 payload/metadata/artifact 一致。 |
| `freshness` | yes | `current_tool_observation` 或 `background_process_observation`。 |
| `clock_source` | yes | 固定为 `"tool_payload"`。 |
| `duration_seconds` | no | 有真实 duration 才填；可来自 monotonic 差值，但它不是 wall-clock。 |
| `command_started_at` | no | 只有 terminal 执行开始时真实捕获 wall-clock 才填。 |
| `process_started_at` | no | 只有 background process 启动时真实捕获 wall-clock 才填。 |
| `last_output_at` | no | 只有 output reader/accumulator 维护了真实 wall-clock 才填。 |

同步写入至少两层：

- 模型可见 JSON payload：`terminal_result_payload()["timing"]`
- `ToolExecutionResult.metadata["timing"]`

artifact metadata 可以只保留 bounded timing subset，例如 `observed_at` / `duration_seconds` / `freshness`，但来源仍然必须是同一个 helper，不能与 payload / metadata 表达不同事实。

对 `terminal` 初次执行：

```json
{
  "timing": {
    "command_started_at": "...",
    "observed_at": "...",
    "duration_seconds": 3.2,
    "freshness": "current_tool_observation"
  }
}
```

`command_started_at` 只有在执行开始时捕获了 wall-clock timestamp 才能填写。若 PR3 暂时只拿到 monotonic duration，则只输出 `observed_at`、`duration_seconds`、`freshness`，不要用 `observed_at - duration_seconds` 反推 wall-clock start。

对 `terminal_process.log/poll/wait/list/write/submit/kill/close_stdin` 等所有 action：

```json
{
  "timing": {
    "process_started_at": "...",
    "observed_at": "...",
    "last_output_at": "...",
    "duration_seconds": 1112.3,
    "freshness": "background_process_observation"
  }
}
```

action 矩阵：

| 工具/action | `timing.observed_at` | freshness | 备注 |
|---|---:|---|---|
| `terminal` success/error | required | `current_tool_observation` | streaming delta 不要求逐条带 timing；最终 payload 必须带。 |
| `terminal` yielded/running | required | `background_process_observation` | payload 还应保留 `process_id` / status。 |
| `terminal_process list` | required | `background_process_observation` | list 是当前 inventory observation，即使没有新输出也必须有 observed_at。 |
| `terminal_process poll/log/wait` | required | `background_process_observation` | wait 若产生 terminal completed state，也仍是当前 observation。 |
| `terminal_process write/submit/close_stdin/kill` | required | `background_process_observation` | 这些是 control/action observation，不是历史 replay。当前代码真实 action 是 `write` / `submit` / `close_stdin`，不是 `input`。 |
| terminal-like tool error payload | required when produced by terminal tool | `current_tool_observation` 或 `background_process_observation` | 如果错误由 permission gate 在 tool 外生成，走 generic tool-result timing，不强行要求 terminal payload timing。 |

permission gate deny / approval deny 这类没有进入 terminal tool 的结果不一定会调用 `terminal_timing_payload(...)`。它们仍应通过 tool-result `Msg.created_at` / `finished_at` 与 PR4 render decision timing 表达当前观察时间，不能假装是 terminal payload。

streaming 规则：

- streaming delta 不要求携带最终 timing，避免给每块 log 加 timestamp。
- finish/finalize 阶段生成的最终 terminal payload 必须包含 `timing`。
- `ToolExecutionResult.metadata["timing"]` 必须与最终模型可见 JSON payload 同源一致。
- artifact metadata 只保存 bounded subset，但必须从同一 helper 派生。

写入顺序必须钉死。当前 `terminal_result_payload(...)` 和 `terminal_artifact_candidates(...)` 是独立函数；PR3 实现时不能让两条路径分别计算 timing。推荐二选一：

1. 在 terminal action 完成处先生成一次 `timing = terminal_timing_payload(...)`，再显式传入 payload builder、`ToolExecutionResult.metadata`、artifact candidate builder。
2. 或先把 helper 结果写入 `ToolExecutionResult.metadata["timing"]`，后续 payload / artifact builder 只能从 metadata 读取同一份 timing。

无论选择哪种形态，artifact path 不得自行再次调用 `utc_now()`，也不得因为没有拿到 timing 而静默丢失 bounded timing metadata。

### 5.3 不阻塞 V1 的简化

如果暂时没有 `last_output_at`，不要伪造。先提供：

- `observed_at`
- `duration_seconds`
- `freshness`

`last_output_at` 可以 PR2/PR3 再从 OutputAccumulator reader loop 中维护。

### 5.4 Tool-result budgeting 的关系

tool result body 被压缩到 essential envelope 时，timing 应作为 terminal essential envelope 的一部分参与预算，但不得突破 hard cap。

例如：

```json
{
  "tool_name": "terminal_process",
  "state": "success",
  "process_id": "proc_...",
  "status": "running",
  "timing": {
    "observed_at": "...",
    "duration_seconds": 1112.3,
    "freshness": "background_process_observation"
  },
  "body": "[omitted by budget]"
}
```

这应纳入 tool-result envelope hard cap 计算。PR4 冻结以下降级顺序：

1. full envelope 能放下：保留完整 timing dict，`timing_policy="full"`。
2. full timing 放不下但 minimal timing 能放下：保留 minimal timing subset，至少尝试 `observed_at` / `duration_seconds` / `freshness`，`timing_policy="minimal"`。
3. minimal timing 仍放不下：省略 timing，保持 parseable JSON，不突破 hard cap，并在 render decision diagnostic 中记录 `tool_result_timing_omitted_for_envelope_cap`，`timing_policy="omitted_for_cap"`。

PR4 不允许为了保留 timing 输出半截 JSON。任何 terminal essential envelope 降级后都必须仍是 parseable JSON 或明确的 parseable ultra-minimal envelope。

## 6. 实施落点

### 6.1 ContextSection metadata

改动点：

- `src/pulsara_agent/runtime/context_engine/types.py`
  - 新增 `ContextSectionSourceTiming` / `ContextSectionRenderTiming` DTO 或 helper。
- `src/pulsara_agent/runtime/context_engine/compiler.py`
  - 在 `_collect_sections()` 只写 source timing：`metadata["source_timing"]`。
  - lifecycle cache 之后，用 render-time overlay 生成最终 `metadata["timing"]`。
  - `_compiled_section()` 携带最终 timing metadata。
  - `_leading_user_context_text()` / `_lower_system_prompt()` 渲染 timing header。

注意：`system:prompt` 不渲染 timing header，但可以在 event metadata 中保留 compile timing。

推荐编译顺序固定为：

1. `_collect_sections()` 收集 section text 与 `metadata["source_timing"]`，不含 `compiled_at_utc` / `age_seconds` / 模型可见 header。
2. `ContextLifecycleCoordinator.apply()` 只复用 source output / source timing。
3. lifecycle 之后执行 timing render overlay：为每个 section 生成最终 `metadata["timing"]`，必要时生成 `metadata["timing_header_text"]`。
4. overlay 后更新 section `estimated_tokens`，把 timing header token 计入预算。
5. 再执行 `_apply_section_budget()`。
6. 最后 lowering 到 `_leading_user_context_text()` / `_lower_system_prompt()` / transcript messages。

不要在 lowering 阶段才临时拼 header，否则 `ContextBudgetReport.sections_estimated_tokens` 会低估真实模型输入。

### 6.2 Compile request

建议给 `ContextCompileRequest` 增加：

```python
compiled_at_utc: str
user_observed_at_utc: str
session_timezone: str | None = None
compiled_local_date: str | None = None
```

由 `build_compiled_context()` 或 `AgentRuntime` 在一次 compile 开始时生成一次，保证同一个 compiled context 内所有 section 的 `compiled_at_utc` 一致。

不要在每个 section 内单独调用 `utc_now()`，否则同一 context 内会出现无意义微差。

时区策略：

- Event truth 继续使用 UTC。
- `compiled_at_utc` 必须是 UTC ISO 8601。
- 如果 HostSession / runtime 有 session timezone，则 timing metadata 可以附带 `compiled_local_date`，timing header 可以渲染 `session_timezone` / `local_date`，用于模型理解“今天/昨天/本地日期”。
- 如果没有 session timezone，模型可见 header 明确使用 UTC，不猜测本地时间。

### 6.3 Source time inference

V1 source time 规则：

```text
current_user:
  source_started_at = current_user.created_at if present
  fallback = ContextCompileRequest.user_observed_at_utc
  source_ended_at = current_user.finished_at or source_started_at

current_run_tail:
  source_started_at = first tail Msg.created_at
  source_ended_at = last tail Msg.finished_at or created_at

prior_history:
  source_started_at = first prior Msg.created_at
  source_ended_at = last prior Msg.finished_at or created_at

component prompts:
  source time from component-specific metadata if available
  otherwise source=unknown, observed_at=compiled_at_utc

tool results:
  source time from Msg.created_at/finished_at and terminal payload timing if available
  fallback source time from ToolResultStartEvent/ToolResultEndEvent spans
```

当前 active run 里 `current_user.created_at` 可能为空；PR1 必须先提供稳定来源。冻结实现路径：

- AgentRuntime 在当前 run 开始时捕获一次 `user_observed_at_utc`。
- 构造当前 user `Msg` 时写入 `created_at=user_observed_at_utc`。
- 同一个值写入 `ContextCompileRequest.user_observed_at_utc`。
- `RunStartEvent.created_at` 只作为 replay / recovery 校验来源，不作为实现时的第三套主路径。

不能让最重要的 `current_turn` timing 默默退化成 unknown。

live tool-result timing 也必须有稳定来源。PR1/PR3 必须二选一：

- 写入 tool-result `Msg.created_at` / `Msg.finished_at`。
- 或从 event log 的 `ToolResultStartEvent` / `ToolResultEndEvent` span 推导 source timing。

注意：现有 `RuntimeEventSpan` 本身主要保存 `sequence` / `source_event_id` 等定位信息，不直接保存 start/end wall-clock。若采用 span 路径，必须通过 batch event slice 或 event log 的 `sequence` / `source_event_id` 找回对应 start/end event 的 `created_at`；不能把 `RuntimeEventSpan` 当成 wall-clock 来源。

普通 tool result 不能因为 live run 中 `Msg.created_at` 为空就默认退化成 unknown。

### 6.4 Tool result render decisions

`tool_result_render_decisions` 应增加 timing fields：

- `source_message_created_at`
- `source_message_finished_at`
- `observed_at`
- `tool_timing`
- `timing_policy`: `full | minimal | omitted_for_cap | not_applicable`
- `rendered_timing_chars`
- `rendered_timing_header_chars`

这样 inspect 能解释为什么模型看到某个 command 是 fresh / stale。

字段定义：

- `tool_timing` 是从 terminal payload / metadata 中读取的 timing dict；没有 terminal payload timing 时为 null/空 dict。
- `observed_at` 优先来自 `tool_timing.observed_at`，否则来自 tool-result message/event span 的 end time。
- `rendered_timing_chars` 是最终 tool-result body/envelope 中 timing JSON 字段实际占用的字符数，不包含 section-level timing header。
- `rendered_timing_header_chars` 仅用于 section timing header；tool-result envelope timing 不应混进这个字段。
- `rendered_envelope_chars` / `rendered_total_chars` 必须包含 rendered timing 字符，不能把 timing 当作“免费 metadata”。

`timing_policy="not_applicable"` 的触发条件：

- 非 terminal-like tool result。
- 或 terminal-like 但没有 payload/metadata timing 可渲染。

此时 `rendered_timing_chars=0`，不写 `tool_result_timing_omitted_for_envelope_cap` diagnostic。只有 terminal timing 本来可用、但因为 envelope cap 放不下时，才使用 `omitted_for_cap` 并写 omission diagnostic。

render cache 规则：

- `tool_results.py` 的 render cache entry 必须保存 `timing_policy` / `rendered_timing_chars` / `tool_timing`，或在 cache hit 恢复 rendered payload 时重新计算并填入这些字段。
- cache hit 不应更新 `observed_at`。terminal payload timing 是工具观察事实，不是 compile-time fact；复用 cache 时必须保持稳定。
- cache hit path 的 render decision 不允许因为字段恢复不完整而缺少 timing 字段；否则 inspect 会在 cache miss/hit 间抖动。

## 7. PR 拆分

### PR1：Section timing metadata，不改模型可见文本

目标：先让 inspect 能看见 timing。

改动：

- 新增 timing helper / DTO。
- `_collect_sections()` 写入 `metadata["source_timing"]`，不包含 `compiled_at_utc`。
- lifecycle 之后 overlay 最终 `metadata["timing"]`，包含 `compiled_at_utc` / source range / freshness。
- `ContextCompiledEvent.sections[*].metadata.timing` 可 inspect。
- `ContextCompileRequest` 正式增加 `user_observed_at_utc: str`。
- 当前 active run 的 current user timing 必须有来源：`Msg.created_at` 或 `ContextCompileRequest.user_observed_at_utc`；`RunStartEvent.created_at` 只做 replay / recovery 校验。

验收：

- current_user section timing = current_turn。
- current_run_tail section timing = current_run_tail。
- prior_history section timing = historical_replay。
- lifecycle cache 复用 section 时，`compiled_at_utc` 更新为本次 compile 时间，不沿用旧 compiled time。
- system:prompt 不带 model-visible timing header。
- transcript section metadata 能检测到 context-compaction-summary message，并在 `compaction_summary_messages` 中记录 compacted_history timing：`compacted_at`、`keep_after_sequence`、`through_sequence`，可选 `source_sequence_start`。
- PR1 不把 compaction summary 提升成独立 section，也不把它误当 base system prompt。

### PR2：Model-visible lightweight timing header

目标：让模型在 compiled prompt 中看到 timing。

改动：

- `_leading_user_context_text()` 给每个 leading_user section 加一行 timing header。
- `_lower_system_prompt()` 对 non-base runtime context section 加 timing header，但不改 `system:prompt` 本体。
- header 计入 section estimated_tokens。
- 所有 compact / omit / degrade 后的 section 必须通过统一 helper 重新计算 rendered body + timing header token，或显式维护 `rendered_timing_header_tokens`。

验收：

- memory projection prompt 包含 timing header。
- subagent result section 包含 timing header。
- system base prompt 不包含动态 timestamp。
- budget estimate 包含 header token。
- section 被 compact/degrade 后，`ContextBudgetReport.sections_estimated_tokens` 仍包含 timing header token。

### PR3：terminal / terminal_process payload timing

目标：让长跑工具结果具备 observation timing。

改动：

- 新增并冻结 `terminal_timing_payload(...)` helper。
- terminal result payload 增加 `timing.observed_at`。
- terminal result payload 增加 `timing.duration_seconds`。
- terminal_process 所有 action payload 增加 observation timing：`list` / `poll` / `log` / `wait` / `write` / `submit` / `kill` / `close_stdin` / error payload。
- running/yielded process 设置 freshness=`background_process_observation`。
- 使用统一 `terminal_timing_payload(...)` 写入模型可见 payload 和 `ToolExecutionResult.metadata`。
- streaming delta 不带 timing；最终 payload 与 metadata 必须带 timing。
- artifact metadata 从同一个 helper 派生 bounded timing subset。

验收：

- terminal success result 有 observed_at。
- terminal yielded result 有 observed_at + duration_seconds + process_id。
- terminal_process log/poll/wait/list/write/submit/kill/close_stdin/error result 有 observed_at + action/process context + freshness。
- ToolExecutionResult metadata 与模型可见 JSON payload 的 timing 同源一致。
- streaming terminal finish payload 有 timing，delta 不重复携带 timing。
- replay 历史 terminal result 时，模型能看到 historical observation timing。

### PR4：tool result essential envelope 保留 timing

目标：预算压缩时不丢时间感。

改动：

- `_essential_tool_result_envelope()` 将 terminal timing 作为 essential envelope 候选字段。
- terminal timing 按 hard cap 降级：`full -> minimal -> omitted_for_cap`。
- terminal-like envelope 的 per-envelope cap 计算包含 timing。
- tool_result_render_decisions 记录 timing render decision。
- render decision 增加 `timing_policy` / `rendered_timing_chars` / `tool_result_timing_omitted_for_envelope_cap` diagnostic。
- render cache entry 或 cache hit recovery 必须保留/恢复 `timing_policy` / `rendered_timing_chars` / `tool_timing`。
- 小预算下必须保留 parseable JSON，不允许为了 timing 输出半截 JSON。

验收：

- 大 terminal output 被省略时，在 envelope hard cap 允许时保留 observed_at / duration_seconds / process_id。
- terminal_process list/log/wait 降级时在预算允许时保留 full/minimal timing。
- 极小 envelope cap 下不突破 hard cap，timing 可省略，并写 `tool_result_timing_omitted_for_envelope_cap` diagnostic。
- `rendered_envelope_chars` / `rendered_total_chars` 包含 timing 字符。
- rendered_total hard cap 不被 timing header 绕过。

### PR5：inspect / dogfood

目标：排障可见。

改动：

- inspector 直接展示 `ContextCompiledEvent.sections[*].metadata.timing`，不重新推断 timing。
- inspector 展示 tool_result_render_decisions 中的 timing 字段和 `timing_policy`。
- old event log 缺 timing 时显示 `missing` / `unknown`，不报错、不回算当前时间。
- fake provider / golden payload 覆盖 normalized shape。
- real/dogfood：长跑 terminal_process 日志场景，验证模型能区分 fresh log 与 historical replay；该 smoke 走显式环境变量，不作为普通 CI 稳定性前提。

建议 normalized shape：

```json
{
  "section_timings": [
    {
      "section_id": "memory:projection",
      "freshness": "memory_projection",
      "compiled_at_utc": "...",
      "source_started_at": "...",
      "source_ended_at": "...",
      "age_seconds": 12.0,
      "status": "present"
    },
    {
      "section_id": "legacy:old_section",
      "freshness": "unknown",
      "compiled_at_utc": null,
      "source_started_at": null,
      "source_ended_at": null,
      "age_seconds": null,
      "status": "missing"
    }
  ],
  "tool_result_timings": [
    {
      "tool_call_id": "call:...",
      "tool_name": "terminal_process",
      "observed_at": "...",
      "freshness": "background_process_observation",
      "timing_policy": "minimal",
      "rendered_timing_chars": 96,
      "diagnostics": []
    },
    {
      "tool_call_id": "call:old",
      "tool_name": "terminal",
      "observed_at": null,
      "freshness": "unknown",
      "timing_policy": "not_applicable",
      "rendered_timing_chars": 0,
      "status": "missing",
      "diagnostics": []
    }
  ]
}
```

这里的 `age_seconds` 是 compile-time 记录，不是 inspector 调用时重新计算。inspect 只投影 durable event payload，不查询当前时间来改写 timing。

验收：

- inspect context compiled event 能看到 section timing。
- inspect tool result render decisions 能看到 timing。
- 缺 timing 的旧 event 显示 `status="missing"` 或 `unknown`，inspect 不失败。
- fake/golden 测试覆盖 `section_timings` / `tool_result_timings` shape。
- real LLM 训练日志/长跑命令 smoke：模型能正确说明命令是否仍在运行、日志是最近观察还是历史 replay；该测试显式 opt-in。

## 8. 风险与边界

### 8.1 Token 开销

每个 section 一行 timing header 会增加 token。V1 必须：

- 使用紧凑 header。
- 不对每行 log 加 timestamp。
- 允许 policy 关闭 model-visible timing header，仅保留 inspect metadata。

### 8.2 False precision

不能把 monotonic duration 伪造成 wall-clock start time。

如果只有 `duration_seconds`，可以显示：

```text
observed_at=...
duration=18m32s
process_started_at=unknown
```

### 8.3 Provider-native pairing

不要为了给 transcript tool_result 加 header 破坏 provider-native assistant tool-call / tool_result pairing。

V1 优先：

- section header for runtime context sections
- tool result JSON payload timing
- essential envelope timing

不要强行插入额外 user/system message 到 tool-call batch 中间。

### 8.4 Cache stability

动态 timestamp 会影响 cache。

因此：

- base system prompt 不加 timing。
- stable/cached sections 的 dependency_fingerprint 不应包含 `compiled_at_utc`。
- timing header 是 render-time metadata，不应导致 stable section lifecycle cache 反复 invalidated。
- lifecycle cache 只能缓存 source timing；最终 `metadata["timing"]` 与模型可见 header 必须在 cache 复用之后重新生成。

## 9. 最小可行实现

如果只做最小版本：

1. `ContextSection.metadata.source_timing` + lifecycle 后 overlay 到 `ContextCompiledEvent.sections[*].metadata.timing`。
2. leading_user / handoff_hint section 加一行 `[context timing: ...]`。
3. terminal / terminal_process payload 加 `timing.observed_at` 和 `timing.duration_seconds`。
4. essential terminal envelope 按 `full -> minimal -> omitted_for_cap` 策略保留或诊断省略 timing。

这已经足以让模型获得与当前 Codex 工具类似的时间感知：不是连续体感时间，而是明确知道“这段观察是什么时候产生的、距现在多久、是不是历史 replay”。
