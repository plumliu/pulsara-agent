# Pulsara Context Compaction / Continuity 调研与 V1 设计

## 0. 结论

Pulsara 不应该照搬 Claude Code 的 JSONL transcript compact，也不应该照搬 Codex 的“直接替换 in-memory history”作为唯一事实。Pulsara 已经有更强的事实层:

- Postgres `agent_events` 是 canonical event log。
- `artifacts` 是长文本/工具输出/计划等 payload 的 durable store。
- `HostSession._prior_messages()` 通过 `rebuild_prior_messages(event_log)` 在每轮开始前重建模型可见历史。
- resume 已经通过 `runtime_session_id + sessions.metadata manifest + event log replay` 重新打开 durable conversation。
- memory recall projection、working context、recovery note、terminal completion note 都已经是 derived projection，而不是 canonical truth。

因此 V1 compaction 应采用:

> typed compaction boundary event + summary artifact + transcript rehydration planner。

这里要先分清两个概念:

- 触发时机: token/context budget 达到阈值后自动触发，或用户在达到阈值前手动 `:compact` 提前触发。
- 执行落点: V1 只在安全点执行 compact；自动 compact 以 run 完成后为下一轮准备为主，并在下一轮 LLM call 前做 preflight 兜底；手动 compact 只允许在 idle / 无 pending interaction 时执行。

它的核心不是“删除旧消息”，也不是“每轮结束固定 compact”，而是在模型可见层按需引入一个压缩边界:

1. event log 全量保留。
2. compaction summary 存 artifact。
3. 新增 typed event 记录 summary artifact、覆盖到哪个 sequence、保留哪些 recent runs / artifacts。
4. `rebuild_prior_messages()` 在边界之后只投影:
   - compaction summary message；
   - 边界后的 recent event replay；
   - active plan reducer state / 活进程内 pending interaction；
   - terminal/recovery/memory/working-context 等重新计算的 projection。
5. resume 不需要特殊魔法；它继续用 event log，只是 rehydration planner 看到 compact boundary 后构造较短 prior context。

这条路最大化复用刚建好的 resume，并避免 compaction summary 变成新的事实源。

## 1. 本地调研结果

### 1.1 Codex 的做法

调研目录: `/Users/plumliu/Desktop/python_workspace/codex`

关键文件:

- `/Users/plumliu/Desktop/python_workspace/codex/codex-rs/core/src/compact.rs`
- `/Users/plumliu/Desktop/python_workspace/codex/codex-rs/core/src/compact_remote.rs`
- `/Users/plumliu/Desktop/python_workspace/codex/codex-rs/core/src/compact_remote_v2.rs`
- `/Users/plumliu/Desktop/python_workspace/codex/codex-rs/core/src/session/mod.rs`
- `/Users/plumliu/Desktop/python_workspace/codex/codex-rs/core/src/session/rollout_reconstruction.rs`
- `/Users/plumliu/Desktop/python_workspace/codex/codex-rs/protocol/src/protocol.rs`

Codex 的结构是“history replacement + rollout persistence”:

- 手动 compact 对应 protocol `Op::Compact`。
- compact run 会启动一个 `ContextCompactionItem`，模型生成 summary 后，构造 `CompactedItem`。
- `CompactedItem` 包含:
  - `message`
  - `replacement_history`
  - `window_number`
  - `window_id`
- `Session::replace_compacted_history()` 会:
  - 直接替换 session 内存 history；
  - 将 `RolloutItem::Compacted(compacted_item)` 持久化；
  - 如有需要，再持久化 `TurnContextItem`，用于 resume/fork 后重建上下文基线。
- `rollout_reconstruction` 读取 rollout 时，如果 `CompactedItem.replacement_history` 存在，就直接用它替换历史；旧格式没有 replacement history 时才 fallback 到从 user messages + summary 重建。
- Codex 有 context window 身份:
  - `current_window_id()`
  - `advance_auto_compact_window()`
  - analytics 记录 trigger/reason/phase/status/token before/after。

值得借鉴:

- compact 是 first-class turn item，而不是隐形摘要。
- 有 window identity，能解释“当前上下文窗口”。
- compact 前后有 hooks / analytics。
- replacement history 作为 durable rollout item 保存，resume 不靠猜。
- manual compact 和 auto compact 分 trigger/reason/phase。

不应照搬:

- Codex 可以把 `replacement_history` 当作后续事实基线，是因为它的 conversation history 本身就是 rollout/protocol 层事实。Pulsara 的 canonical truth 是 event log；如果直接替换 event history，会破坏 inspector、resume、memory governance 的事实完整性。
- Codex 的 compact summary 被编码成模型历史 item；Pulsara 需要额外保护 derived projections，避免 memory reflection 把 summary、recovery note、memory projection 当成用户事实。

### 1.2 Claude Code 的做法

调研目录: `/Users/plumliu/Desktop/python_workspace/claude-code`

关键文件:

- `/Users/plumliu/Desktop/python_workspace/claude-code/src/query.ts`
- `/Users/plumliu/Desktop/python_workspace/claude-code/src/services/compact/compact.ts`
- `/Users/plumliu/Desktop/python_workspace/claude-code/src/services/compact/autoCompact.ts`
- `/Users/plumliu/Desktop/python_workspace/claude-code/src/services/compact/prompt.ts`
- `/Users/plumliu/Desktop/python_workspace/claude-code/src/utils/sessionStorage.ts`
- `/Users/plumliu/Desktop/python_workspace/claude-code/src/remote/sdkMessageAdapter.ts`

Claude Code 的结构是“JSONL transcript + compact boundary + post-compact attachments”:

- 每次 query 开始先取 `getMessagesAfterCompactBoundary(messages)`，即 compact boundary 之前的消息不再进入当前 API prompt。
- query loop 顺序大致是:
  1. content replacement / tool result budget；
  2. snip compact；
  3. microcompact；
  4. context collapse；
  5. auto compact；
  6. 若 compact 成功，`buildPostCompactMessages(compactionResult)` 替换 `messagesForQuery`。
- auto compact 阈值是 context window 减去 reserved output 和 buffer；有 warning/error/blocking 阈值。
- 有 circuit breaker: 连续 autocompact 失败超过上限后停止反复尝试。
- compact 本身通过 forked agent/model summary 生成，prompt 明确要求:
  - 记录用户请求；
  - 记录文件/代码段；
  - 记录错误与修复；
  - 记录 pending tasks/current work/next step；
  - 禁止工具调用。
- compact 成功后会生成:
  - compact boundary marker；
  - summary message；
  - file attachments；
  - plan attachment；
  - plan mode attachment；
  - skill attachment；
  - tool/schema/MCP delta attachment；
  - hook/session-start results。
- JSONL resume 复杂点在 `sessionStorage.ts`:
  - compact boundary 会截断 pre-boundary 历史；
  - preserved segment 用 `headUuid / anchorUuid / tailUuid` 进行 relink；
  - 大 transcript 会跳过 pre-boundary bytes，只读 compact 后内容；
  - resume 后会清理 stale usage，避免立即再次 autocompact spiral；
  - compact boundary 在 remote SDK 中作为 `compact_boundary` system message，UI 显示“Conversation compacted”。

值得借鉴:

- compact boundary 是结构化消息，不只是 summary 文本。
- post-compact 不是只保留 summary，还重新注入运行所需 attachments/state。
- 有 preserved segment / recent tail 的概念。
- 有 circuit breaker，避免 prompt-too-long 或 compact 失败后每轮重复烧模型。
- summary prompt 强调“当前工作”和“下一步”，对 coding agent 很实用。
- plan mode、skills、tool listing、MCP instructions 等 runtime surface 在 compact 后重新注入，而不是相信 summary 记住。

不应照搬:

- Pulsara 不需要 JSONL parentUuid relink。event log 是 append-only ordered sequence，不存在 JSONL parent chain 断裂问题。
- Pulsara 不应把 post-compact attachments 都变成 ordinary messages；很多内容应由 typed reducer/projection 重新生成。
- Claude Code 的 compact boundary 是 transcript loader 的裁剪点；Pulsara 的边界应该是 event sequence + artifact id + typed metadata。

## 2. Pulsara 当前基础

### 2.1 已经具备的好基础

关键文件:

- `/Users/plumliu/Desktop/python_workspace/pulsara_agent/src/pulsara_agent/event/events.py`
- `/Users/plumliu/Desktop/python_workspace/pulsara_agent/src/pulsara_agent/event_log/postgres.py`
- `/Users/plumliu/Desktop/python_workspace/pulsara_agent/src/pulsara_agent/host/transcript.py`
- `/Users/plumliu/Desktop/python_workspace/pulsara_agent/src/pulsara_agent/host/resume.py`
- `/Users/plumliu/Desktop/python_workspace/pulsara_agent/src/pulsara_agent/host/session.py`
- `/Users/plumliu/Desktop/python_workspace/pulsara_agent/src/pulsara_agent/runtime/context.py`
- `/Users/plumliu/Desktop/python_workspace/pulsara_agent/src/pulsara_agent/runtime/agent.py`
- `/Users/plumliu/Desktop/python_workspace/pulsara_agent/src/pulsara_agent/runtime/session.py`
- `/Users/plumliu/Desktop/python_workspace/pulsara_agent/src/pulsara_agent/memory/hooks/durable.py`
- `/Users/plumliu/Desktop/python_workspace/pulsara_agent/src/pulsara_agent/memory/working_context.py`
- `/Users/plumliu/Desktop/python_workspace/pulsara_agent/src/pulsara_agent/inspector/service.py`

已有能力:

1. Durable event log
   - `PostgresEventLog.append/extend()` 给每个 event 分配 session-local monotonic `sequence`。
   - `agent_events.payload` 保存 typed event JSON。
   - `runs` 是 projection，可以从 RUN_START/RUN_END 修复。

2. Resume
   - `HostCore.resume_session()` 用 `runtime_session_id` 重新打开 session。
   - `repair_dangling_runs_for_resume()` 会给死 host 留下的 running run 补 `RUN_END`。
   - `SessionManifestStore` 把 conversation/workspace/model/permission 放在 `sessions.metadata`。

3. Prior context rebuild
   - `HostSession._prior_messages()` 现在唯一入口是 `rebuild_prior_messages(event_log)`。
   - `run_turn()` / `stream_turn()` 都从这里拿 prior messages。
   - 这是 compaction 的最佳接线点。

4. Derived projections 已经有边界意识
   - memory recall projection 有 `<recalled-memory-projection do_not_write_back="true">`。
   - working context projection 有 `<working-context-projection do_not_write_back="true" authority="recent_activity">`。
   - recovery note 由 `project_recovery_from_events/state()` 重建。
   - terminal completion note 明确是 lifecycle-only，不是假装包含 full output。

5. Artifact store
   - `PostgresArtifactStore` 已用于 tool result、timeline、accepted plan。
   - `ToolResultArtifactService` 已经有 preview + artifact ref 机制。
   - compact summary 可以自然落进 `artifacts`。

6. Inspector
   - `InspectorService.inspect_run()` 已能展示:
     - prior messages as seen；
     - projections as seen；
     - assistant replies；
     - tool artifacts；
     - recall traces；
     - outbox；
     - diagnostics。
   - compaction 只要进入 event/artifact 层，Inspector 很容易扩展解释它。

### 2.2 当前缺口

1. 没有 compact boundary event。
   - `EventType` 里没有 compaction start/end/boundary。
   - 目前只能全量 replay prior messages。

2. `LoopState.compacted` 只是字段，未成为 runtime 协议。
   - 目前没有 token accounting、compact threshold、compact artifact、rehydration 语义。

3. `rebuild_prior_messages()` 没有 compaction-aware 裁剪。
   - 它会遍历所有 `RunStartEvent` 和 `ReplyEndEvent`。
   - 长 session resume 后仍会把全部历史送回模型，直到 budget 截断 tool result，但不会压缩对话历史。

4. working context 不是 compaction。
   - `working_context_summaries` 适合跨 session recent activity。
   - 它不能替代对话级 compact，因为它不保留 pending state、artifact refs、计划、工具调用链等。

5. active suspended state 仍是 in-process。
   - pending approval / pending plan interaction 在同一 HostSession 内由 `_suspended_state` 保留。
   - 进程死掉后 resume 会 repair dangling run，而不是恢复同一个 suspended coroutine。
   - compaction V1 不应承诺恢复 suspended LoopState；应只保证 pending interaction 在活进程内 compact 不丢，以及跨进程 resume 有清晰 abort/recovery。

## 3. V1 设计原则

### 3.1 Canonical truth 不被 compact 改写

Compaction 不删除、不重写、不隐藏 event log。

`agent_events` 仍然是完整事实。Compaction 只新增事件，告诉 read model:

- 哪个 sequence 之前的 prior context 已被 summary 覆盖；
- summary artifact 在哪里；
- 保留 recent tail 的起点；
- 哪些 artifact ids / run ids 被 summary 引用；
- compact 的 trigger/reason/status/token 统计。

### 3.2 Summary 是 artifact，不是 canonical message

Summary 应写入 `artifacts`:

- `media_type = "text/markdown; charset=utf-8"` 或 `text/plain; charset=utf-8`
- `metadata.artifact_kind = "context_compaction_summary"`
- `metadata.compaction_id`
- `metadata.through_sequence`
- `metadata.trigger`
- `metadata.included_run_ids`
- `metadata.included_artifact_ids`
- `metadata.do_not_write_back = true`

模型看到的是一个 synthesized system/user projection message，但它的来源是 compact artifact。

### 3.3 Rehydration planner 是核心，不是 summarizer

Summarizer 只负责生成 summary。真正决定下一轮模型看到什么的是 rehydration planner。

V1 planner 输入:

- runtime session id；
- event log；
- latest valid compaction boundary；
- current HostSession plan state；
- memory hooks / working context；
- terminal completion events；
- recovery projection；
- budget。

V1 planner 输出:

- prior messages:
  - compact summary projection；
  - post-boundary replayed messages；
  - terminal/recovery notes；
  - plan runtime messages；
  - optionally recent tail before boundary if configured。

### 3.4 Compact 与 resume 使用同一条路径

不要为 resume 单独发明“恢复摘要”。

`HostSession._prior_messages()` 应改成:

```python
return rebuild_prior_messages(
    self.wiring.runtime_wiring.event_log,
    archive=self.wiring.runtime_wiring.archive,
    compaction_policy=...
)
```

resume 之后仍然调用同一个 `_prior_messages()`。这样 compact 与 resume 不会漂移。

## 4. 建议新增事件

V1 推荐三个事件，理由是它们分别服务于 UI/Inspector、artifact write、failure diagnostics。

### 4.1 ContextCompactionStartedEvent

用途:

- Inspector 显示 compact 尝试开始。
- 记录 trigger/reason。
- 支持失败诊断。

字段:

```python
class ContextCompactionStartedEvent(EventBase):
    type = EventType.CONTEXT_COMPACTION_STARTED
    compaction_id: str
    trigger: Literal["manual", "auto", "reactive"]
    reason: str
    from_sequence: int | None = None
    target_through_sequence: int
    active_context_tokens_before: int | None = None
```

### 4.2 ContextCompactionCompletedEvent

用途:

- canonical compact boundary。
- 记录 summary artifact。
- rehydration planner 以它为边界。

字段:

```python
class ContextCompactionCompletedEvent(EventBase):
    type = EventType.CONTEXT_COMPACTION_COMPLETED
    compaction_id: str
    trigger: Literal["manual", "auto", "reactive"]
    reason: str
    summary_artifact_id: str
    from_sequence: int | None = None
    through_sequence: int
    keep_after_sequence: int
    included_run_ids: list[str] = []
    included_artifact_ids: list[str] = []
    excluded_projection_ids: list[str] = []
    active_context_tokens_before: int | None = None
    active_context_tokens_after: int | None = None
    summary_chars: int = 0
    window_number: int
    window_id: str
```

语义:

- `through_sequence`: compact summary 覆盖到的最高 sequence。
- `keep_after_sequence`: planner 仍按原事件 replay 的起点。通常等于 `through_sequence`，但 V1 可保留 recent tail，例如 `through_sequence - tail_budget_window`。
- `summary_artifact_id`: 必须能从 `archive.get_text()` 读到。
- `window_id`: 类似 Codex，用于解释当前上下文窗口。

### 4.3 ContextCompactionFailedEvent

用途:

- circuit breaker。
- Inspector 解释为什么没有 compact。
- 避免 silent failure 后每轮重复烧模型。

字段:

```python
class ContextCompactionFailedEvent(EventBase):
    type = EventType.CONTEXT_COMPACTION_FAILED
    compaction_id: str
    trigger: Literal["manual", "auto", "reactive"]
    reason: str
    target_through_sequence: int
    error_type: str
    message: str
    retryable: bool = True
```

### 4.4 为什么不用 CustomEvent

可以临时用 `CustomEvent(name="context_compaction_completed")` dogfood，但不推荐作为 V1 正式实现。

原因:

- rehydration planner 需要强类型字段。
- Inspector/test 需要稳定 schema。
- event log 是 truth，compaction boundary 不能藏在自由 JSON 里。

## 5. 存储模型

### 5.1 不新增表，先复用 artifacts + agent_events

V1 不需要新表。

存储位置:

- `agent_events`: typed compaction lifecycle events。
- `artifacts`: summary body。
- `sessions.metadata`: 可选保存 latest compact window metadata，方便 list/resume 快速显示，但不能作为 truth。

### 5.2 Artifact id 规则

建议:

```text
artifact:context-compaction:<runtime_session_id>:<compaction_id>
```

由于 `runtime_session_id` 里有 `:`，需要复用现有 `_sanitize_part()` 风格，或者专门实现安全 id helper。

metadata:

```json
{
  "artifact_kind": "context_compaction_summary",
  "do_not_write_back": true,
  "compaction_id": "...",
  "trigger": "auto",
  "reason": "context_threshold",
  "through_sequence": 1234,
  "keep_after_sequence": 1234,
  "included_run_ids": ["run:..."],
  "included_artifact_ids": ["artifact:tool-result:..."],
  "excluded_projection_ids": ["projection:..."],
  "window_id": "context-window:..."
}
```

### 5.3 Window identity

需要一个 session-local monotonic compact window。

V1 可不新增列，直接从 latest `ContextCompactionCompletedEvent.window_number` 推导 next number:

- 没有 completed event: `window_number = 0`
- next completed event: `window_number = latest + 1`

`window_id`:

```text
context-window:<runtime_session_id>:<window_number>
```

注意: `window_id` 是 read-model identity，不是新的 session id。

## 6. Runtime 接线点

### 6.1 HostSession._prior_messages()

当前:

```python
def _prior_messages(self):
    return rebuild_prior_messages(self.wiring.runtime_wiring.event_log)
```

建议:

```python
def _prior_messages(self):
    return rebuild_prior_messages(
        self.wiring.runtime_wiring.event_log,
        archive=self.wiring.runtime_wiring.archive,
        compaction_policy=self.wiring.runtime_wiring.compaction_policy,
    )
```

这是最重要落点。所有 run_turn / stream_turn / resume 都走它。

### 6.2 host/transcript.py

`rebuild_prior_messages()` 应拆成两层:

1. event replay:
   - 继续负责 user/assistant/tool result reconstruction。

2. compaction-aware rehydration:
   - 查找 latest valid `ContextCompactionCompletedEvent`。
   - 从 artifact 读 summary。
   - 只 replay `sequence > keep_after_sequence` 的 events。
   - 在前面插入 compact summary projection message。

建议新增:

```python
@dataclass(frozen=True)
class CompactionRehydrationPlan:
    boundary_sequence: int | None
    keep_after_sequence: int | None
    summary_artifact_id: str | None
    window_id: str | None
    included_run_ids: tuple[str, ...]
    included_artifact_ids: tuple[str, ...]
```

和:

```python
def latest_compaction_boundary(events: list[AgentEvent]) -> ContextCompactionCompletedEvent | None: ...

def rebuild_prior_messages(..., archive: ArtifactStore | None = None, compaction_policy: ... = None) -> list[Msg]: ...
```

### 6.3 runtime/context.py

`build_llm_context()` 本身不需要知道 compact，只要 `state.messages` 已经是 compact-aware prior messages。

但应保证 compact summary projection 有 metadata:

```python
SystemMsg(
    name="pulsara",
    content=...,
    metadata={
        "kind": "context_compaction_summary",
        "do_not_write_back": True,
        "artifact_id": summary_artifact_id,
        "window_id": window_id,
    },
)
```

如果未来 reflection hook 会扫描 system/tool messages，必须利用这个 metadata 或文本 fence 阻断 write-back。

### 6.4 runtime/agent.py

需要新增 compact trigger 的调用点。

V1 建议先只做 threshold-driven boundary auto compact，不做 mid-turn compact:

- auto trigger: run 结束后，估算下一轮 prior context 若超过阈值 / 接近窗口上限，则 compact previous history；
- preflight guard: 新 run 调用模型前，如果 prior context + 当前用户输入估算已经超过阈值，而 latest compact boundary 仍不足以降到预算内，则先 compact 已完成历史 prefix，再继续本轮；
- manual trigger: 用户主动 `:compact`，即使尚未达到阈值，也可在 idle safe-point 提前 compact；
- execution point: compact 结果只影响下一轮 prior context，不修改当前 active LLM call 的 `LoopState.messages`。

更稳的 V1:

1. run 结束后检查 token/context budget，达到阈值则 compact previous history，为下一轮准备。
2. 新 run 进入模型前做 preflight，防止 resume / 超长输入 / 上轮 compact 失败导致本轮 prompt 直接超窗。
3. compact 只覆盖已完成历史 prefix；当前用户输入不被 summary 吞掉。
4. compact 结果只影响后续 prior context。
5. 不在当前 active LLM call 中途替换 history。

原因:

- 当前 Pulsara loop 没有 Codex 那种 mid-turn replacement history 协议。
- tool call pairing、pending approval、plan interaction 都在 LoopState 内。
- mid-turn compact 容易把尚未完成的 assistant/tool result 对压坏。

### 6.5 runtime/wiring.py

建议新增可选资源:

```python
compaction_service: ContextCompactionService | None
compaction_policy: ContextCompactionPolicy
```

durable wiring 才启用。非 durable / test fake 可显式关闭。

### 6.6 inspector/service.py

扩展:

- `inspect_run()`:
  - 显示 run 开始时使用的 compaction boundary。
  - 显示 prior messages 中 compact summary 来源 artifact。

- `inspect_session()`:
  - 显示 compact windows。
  - 显示 latest window id、through_sequence、summary artifact。

- `inspect_artifact()`:
  - 对 `artifact_kind=context_compaction_summary` 显示 compaction metadata。

- `inspect_health()`:
  - dangling compaction started without completed/failed。
  - completed boundary references missing artifact。
  - repeated failed compact circuit breaker state。

## 7. Compaction service 设计

### 7.1 ContextCompactionPolicy

建议字段:

```python
@dataclass(frozen=True)
class ContextCompactionPolicy:
    enabled: bool = True
    auto_enabled: bool = True
    trigger_after_estimated_tokens: int | None = None
    trigger_after_prior_chars: int = 120_000  # token estimator 接入前的临时近似
    manual_enabled: bool = True
    min_events_after_last_compact: int = 20
    keep_recent_runs: int = 3
    keep_recent_chars: int = 24_000
    max_summary_chars: int = 12_000
    max_consecutive_failures: int = 3
```

目标语义是 token threshold；`trigger_after_prior_chars` 只是 V1 早期没有可靠 provider token estimator 时的近似。实现时不要把“每轮结束”当成 compact 条件；每轮结束只是最安全的自动检查点。

### 7.2 ContextCompactionService

职责:

1. 判断是否应该 compact。
2. 构造 summarization input。
3. 调用 Flash/cheap model 生成 summary。
4. 写 artifact。
5. 发 typed events。
6. 返回 boundary metadata。

不要做:

- 不删除 event。
- 不写 memory candidate。
- 不修改 canonical graph。
- 不直接 mutate `state.messages`。

### 7.3 Summarization input

输入应来自 event/timeline，而不是直接把当前 model prompt 全塞给 summarizer。

推荐拼装:

- compact range 内的 run timelines；
- user messages；
- assistant final text；
- tool calls with bounded result previews；
- artifact refs；
- plan events；
- recovery/run-end status；
- terminal lifecycle events；
- memory projection ids 只列 id/source，不把 projection 当用户事实。

这比“把 prior messages 再喂给 summary model”更符合 Pulsara 的 event-sourced 架构。

### 7.4 Summary prompt 要求

借鉴 Claude Code，但加 Pulsara 专属约束。

生产 runtime 读取独立生产模板:

- `/Users/plumliu/Desktop/python_workspace/pulsara_agent/src/pulsara_agent/runtime/compaction/prompts/context_compaction_prompt.md`

其方向是: Claude Code-like 强结构化摘要 + Codex-like handoff prefix，但必须维护 Pulsara 的 source taxonomy 和 `do_not_write_back` 边界。

必须要求 summary 输出:

1. Current objective / user intent。
2. Decisions made。
3. Files/code touched or inspected。
4. Tool calls and important artifacts by id。
5. Plan workflow state if relevant。
6. Terminal background state if relevant。
7. Errors/recovery notes as runtime state, not durable user facts。
8. Memory projections seen, explicitly marked as recalled/projection, not user statements。
9. Pending next step only if directly tied to latest user request。

必须禁止:

- 不要说 memory projection 是用户刚说的。
- 不要把 recovery note 写成长期偏好。
- 不要内联长 artifact。
- 不要声称 terminal output 完整，除非 artifact/log ref 表明完整。
- 不要制造未在 event/artifact 中出现的事实。

## 8. 与 memory / working context 的关系

### 8.1 Compaction summary 不进入 durable memory

Compaction summary 是 continuity artifact，不是 semantic memory candidate。

需要守护:

- `metadata.do_not_write_back = true`
- summary projection 文本 fence:

```xml
<context-compaction-summary do_not_write_back="true" source="artifact:...">
...
</context-compaction-summary>
```

- memory hooks 对该 marker 做 echo guard。

### 8.2 working context 仍然独立

working context 是跨 session recent activity summary。

Compaction 是同 session transcript compression。

两者可以同时存在:

- compact summary: “这条 conversation 前半段发生了什么”
- working context: “这个 workspace 最近通常在做什么”

`build_llm_context()` 当前已经将 working context 与 recalled memory merge 成 projection；compaction 不应塞进该 merge 里。

### 8.3 explicit memory_search 历史结果

历史 explicit `memory_search` 的 tool result 应作为普通 tool result/artifact survive。

不要把 recall trace 当作 compact summary 的事实源；trace 只用于 Inspector 解释。

## 9. Pending state 与 close/resume 边界

### 9.1 活进程内 pending interaction

如果 compaction 在同一 HostSession 活进程内发生:

- pending plan question / approval 不应被 summary 恢复；
- 它仍然来自 `_suspended_state` / `pending_interaction`；
- compact 不应在 `LoopStatus.WAITING_USER` 的 suspended state 上自动运行，除非明确只 compact 已完成的历史 prefix，并且不触碰当前 suspended run。

V1 建议:

- 不对 suspended active run 做自动 compact。
- 等用户 resolve / cancel / close 后，在 run-end safe point compact。

### 9.2 跨进程 resume

当前 resume 会 repair dangling runs 为 aborted/host_teardown。

V1 compaction 应接受这个边界:

- 不承诺跨进程恢复 pending approval coroutine。
- 只保证 repaired run 的 recovery note 在 compact 后仍然可见。
- 如果 compact boundary 在 crash 前已 completed，则 resume 使用它。
- 如果只有 started 没有 completed/failed，Inspector health 报 dangling compact attempt，planner 忽略它。

### 9.3 plan mode

plan state 已有 typed reducer `reduce_plan_workflow_state()`。

compaction summary 可以提及 plan，但 plan active/accepted/pending 不能靠 summary 恢复。

V1 rules:

- active plan instruction 仍由 `HostSession._plan_runtime_messages()` 注入。
- accepted plan artifact id 仍来自 `PlanModeExitedEvent.accepted_plan_artifact_id`。
- compact summary 可包含 accepted plan artifact id，但不能替代 plan reducer。

## 10. 触发策略

### 10.1 V1 推荐: threshold-driven run-end + preflight auto compact

自动 compact 的触发原因是 context/token budget，而不是“每个 run 都 compact”。

V1 推荐:

1. 每个 run 结束后进入 safe-point。
2. 根据 latest completed compact boundary + event log 重建下一轮 prior context 的估算大小。
3. 若估算 token/char 超过阈值，并满足 `min_events_after_last_compact`、circuit breaker 等条件，则触发 compact。
4. compact 成功后写 boundary event + summary artifact。
5. 下一轮 `rebuild_prior_messages()` 才使用新的 compact boundary。

连续 compact 是一个特殊但必须支持的路径。因为 `rebuild_prior_messages()` 只采用 latest completed boundary，第二次及后续 compact 的输入必须包含上一条可用 boundary 的 summary artifact 正文，并要求新 summary carry forward 旧 summary 中仍然有效的上下文；不得只总结上一条 boundary 之后的 raw events。

因此 `_finish_active_run()` 或 `on_session_end` 后是“检查/执行落点”，不是“无条件 compact 时机”。

此外需要一个 run-start preflight guard:

1. 在新 run 真正调用模型前估算 `prior context + current user input + fixed system/projection overhead`。
2. 如果已超过 hard threshold，且当前没有 pending tool/approval/plan interaction，则 compact 已完成历史 prefix。
3. 当前用户输入必须留在 compact boundary 之后；summary 不能替代用户刚刚发来的本轮请求。
4. 如果 compact 后仍然超窗，应返回明确错误/要求用户缩短输入，而不是把当前请求也塞进 summary。

但注意 `_finish_active_run()` 当前还会 notify governance。compaction 不应与 governance 抢同一条 event publishing critical path。

建议:

- HostSession run 完成后检查是否需要 compact。
- 如需要，调用 compaction service 产生 typed events。
- 不在 memory governance hook 内做 compact。

### 10.2 手动 compact

CLI 可后续加:

```text
:compact
```

语义:

- 只能在无 active run、无 pending interaction 时运行；
- 不要求达到自动阈值，可用于用户预判接下来要进入长任务时提前 compact；
- compact 当前 session 历史，但仍受 `min_events_after_last_compact` / “没有可压缩内容” 等基本 guard 约束；
- 成功后下一轮生效；
- 显示 summary artifact id 和 window id。

### 10.3 暂不做 mid-turn compact

V1 不做 Codex/Claude Code 那种当前 query 内替换 `messagesForQuery`。

原因:

- Pulsara 工具执行、approval、plan interaction、recovery 都依赖当前 `LoopState.messages` 的工具配对。
- mid-turn compact 需要更强的 “tool-call pairing after compaction” 协议。
- run-end compact 已能解决 resume/长对话主问题，风险更小。

## 11. 可能遇到的坑

### P0-1: Summary over-claim

如果 summary claims 包含模型没看到或 artifact 里没有的事实，会破坏 continuity。

缓解:

- summary input 只从 event/timeline/artifact refs 构造。
- summary artifact metadata 记录 included run/artifact ids。
- Inspector 对 summary 引用的 artifact ids 做存在性检查。

### P0-2: Projection write-back

memory projection、working context、recovery note、terminal completion note 被 summary 后可能再次进入 memory reflection。

缓解:

- summary fence `do_not_write_back=true`。
- memory hooks echo guard 识别 `context-compaction-summary`。
- 测试必须断言 compact summary 不产生 memory candidate。

### P0-3: Tool call pairing 被裁断

如果边界切在 assistant tool_call 和 tool_result 中间，下一轮 provider 会拒绝历史。

缓解:

- boundary 只能落在 completed run 后。
- `through_sequence` 必须 <= 某个 `RunEndEvent.sequence`。
- rehydration replay 只从 run boundary 开始。
- 不做 mid-turn compact。

### P0-4: Missing artifact

Completed event 指向不存在的 summary artifact，会让 resume 失败。

缓解:

- 先写 artifact，再 append completed event。
- planner 发现 artifact missing 时忽略该 boundary 并记录 diagnostic，不要崩溃。
- Inspector health 报错。

### P0-5: Resume/compact race

一个 HostCore 正在 compact，另一个 HostCore 同时 resume 同 session。

缓解:

- `PostgresEventLog.extend()` 已对 session advisory lock 排序 event sequence。
- compaction artifact write + completed event 不是同一个 DB transaction；所以必须让 planner 只信 completed event，且 completed event append 在 artifact 成功之后。
- 可选: compact started/completed 都通过 same runtime session event log append，利用 sequence 排序。

### P1-1: Autocompact spiral

compact 失败后每轮继续尝试。

缓解:

- 记录 `ContextCompactionFailedEvent`。
- policy `max_consecutive_failures`。
- failure circuit breaker 可从 latest failed/completed events 推导，不放内存。

### P1-2: Summary 太大

summary 太长导致 compact 后仍然接近上限。

缓解:

- `max_summary_chars`。
- summary model prompt 要求结构化且 bounded。
- artifact 保存完整 summary，模型 projection 可二次裁剪。

### P1-3: Accepted plan / terminal / artifact refs 丢失

summary 文字提到了“已批准计划”但没有 artifact id。

缓解:

- summary input 显式给 accepted plan artifact id。
- completed event `included_artifact_ids` 包含 accepted plan/tool result/timeline artifacts。
- Inspector 验证 artifact refs。

### P1-4: Recent tail 选择不稳定

如果 keep recent tail 用 char/token 估计，可能切在 run 中间。

缓解:

- V1 以 whole-run 为单位保留 tail。
- `keep_recent_runs` 优先于 raw char。

### P1-5: compact summary 与 working context 重复

模型看到两份“最近活动”，可能过度确信。

缓解:

- 两者 heading 明确:
  - compact summary = current conversation earlier context；
  - working context = cross-session recent activity。
- Inspector 显示来源。

## 12. 测试矩阵

### 12.1 单元测试

1. Event serialization
   - 三个 compaction event round-trip。
   - missing required fields fail。

2. Boundary selection
   - latest completed boundary wins。
   - started-only ignored。
   - failed-only ignored。
   - completed with missing artifact ignored + diagnostic。

3. Rehydration
   - no boundary: output 等价当前 `rebuild_prior_messages()`。
   - boundary exists: output starts with compact summary projection。
   - repeated compaction: second compact input includes previous summary artifact, and latest boundary retains old summary context。
   - only events after `keep_after_sequence` are replayed。
   - replay never starts mid-run。

4. Tool pairing
   - compact at run boundary 后，post-boundary assistant tool_call has matching tool_result。
   - artificial mid-tool boundary rejected/ignored。

5. Projection no-write-back
   - compact summary containing recalled-memory projection marker 不产生 memory candidate。
   - recovery note in summary 不产生 memory candidate。

6. Artifact survival
   - tool result artifact refs included in summary metadata。
   - accepted plan artifact id included。
   - artifact_read can still read historical tool output after compact。

7. Circuit breaker
   - consecutive failed events >= N 后 auto compact skip。
   - completed event resets failure count。

### 12.2 Integration tests with Postgres

1. durable session compact writes:
   - started event；
   - summary artifact；
   - completed event；
   - monotonically increasing sequence。

2. resume after compact:
   - open session；
   - create several runs；
   - compact；
   - close/detach；
   - `resume_session()`；
   - prior context includes compact summary and recent tail only。

3. crash window:
   - manually insert started event without completed；
   - resume ignores it。

4. missing artifact:
   - delete summary artifact；
   - resume does not crash；
   - inspector health reports missing compaction artifact。

5. plan mode:
   - enter plan；
   - approve plan；
   - compact；
   - resume；
   - accepted plan artifact id remains inspectable。

6. terminal completion:
   - background terminal completion after compact boundary；
   - next prior context includes lifecycle completion note, not full output。

### 12.3 Inspector tests

1. `inspect_session` lists compact windows。
2. `inspect_run` shows which compaction boundary shaped prior context。
3. `inspect_artifact` recognizes context compaction summary。
4. `inspect_health` catches:
   - dangling started compact；
   - missing summary artifact；
   - failed compact circuit breaker。

### 12.4 Real LLM dogfood

Scenario:

1. Start durable host session in real workspace。
2. Ask agent to enter plan mode, inspect files, ask one structured plan question, then exit plan。
3. Approve plan。
4. Execute a small code edit and test。
5. Produce a large enough transcript / tool output to trigger compact。
6. Detach or fully restart host process。
7. Resume with `--continue`。
8. Ask: “刚才做到哪了？计划是什么？哪些文件改了？还有什么没做？”

Assertions:

- model cites compact summary as prior conversation context, not as durable memory。
- model can name accepted plan artifact id or equivalent plan summary。
- model does not re-run completed write/test without being asked。
- model does not hallucinate full terminal output if only lifecycle note exists。
- inspector can explain compact boundary and summary artifact。

## 13. 推荐 PR 切分

### PR C1: Contract + typed events

- 新增 compaction event types。
- 新增 event serialization tests。
- 新增 root/contract 文档可选；若只做实现准备，至少更新 contracts。

### PR C2: Summary artifact + rehydration planner

- 新增 `runtime/compaction.py` 或 `host/compaction.py`。
- 实现 latest boundary detection。
- 扩展 `rebuild_prior_messages()` 支持 archive + boundary。
- Tests: no-boundary 等价、boundary 裁剪、missing artifact。

### PR C3: Manual compact CLI / Host API

- `HostSession.compact_now()`。
- CLI `:compact` 或 `pulsara host compact SESSION`。
- 仅允许 idle session。

### PR C4: Threshold-driven auto compact at run-end + preflight safe points

- policy + token/char threshold。
- failure circuit breaker。
- run-end safe point 接线。
- run-start preflight guard。

### PR C5: Inspector support

- inspect session/run/artifact/health 展示 compaction。

### PR C6: Real dogfood

- 长程 real LLM compact/resume dogfood。
- 覆盖 plan + terminal + memory + artifact。

## 14. V1 默认决策

1. V1 compact 的触发原因是 token/context threshold 或手动 `:compact`；run-end / run-start preflight 只是自动 compact 的安全检查/执行落点。
2. V1 不删除 event log，不重写 event log。
3. V1 summary 必须进 artifact。
4. V1 completed boundary event 是唯一可信 compact boundary。
5. V1 resume 不新增路径，复用 `rebuild_prior_messages()`。
6. V1 suspended pending interaction 不自动 compact。
7. V1 compact summary 不进入 memory reflection。
8. V1 whole-run recent tail，不按裸 sequence 切 tool pair。
9. V1 first-class typed events，不用 `CustomEvent` 作为正式边界。

## 15. 最小实现骨架

建议新增文件:

- `/Users/plumliu/Desktop/python_workspace/pulsara_agent/src/pulsara_agent/runtime/compaction.py`

建议核心 API:

```python
@dataclass(frozen=True)
class ContextCompactionPolicy:
    enabled: bool = True
    auto_enabled: bool = True
    trigger_after_prior_chars: int = 120_000
    keep_recent_runs: int = 3
    max_summary_chars: int = 12_000
    max_consecutive_failures: int = 3


@dataclass(frozen=True)
class CompactionBoundary:
    compaction_id: str
    summary_artifact_id: str
    through_sequence: int
    keep_after_sequence: int
    window_number: int
    window_id: str


def latest_completed_boundary(events: list[AgentEvent]) -> CompactionBoundary | None:
    ...


def build_compaction_summary_projection(
    boundary: CompactionBoundary,
    archive: ArtifactStore,
    *,
    max_chars: int,
) -> SystemMsg | None:
    ...
```

And later:

```python
class ContextCompactionService:
    async def compact_if_needed(self, runtime_session: RuntimeSession, *, trigger: str, reason: str) -> CompactionBoundary | None:
        ...
```

## 16. 最重要的实现边界

如果只记一条:

> compact 只能改变“下一次模型看见的 prior context”，不能改变“系统认为发生过什么”。

Pulsara 的优势恰好在这里。Codex 和 Claude Code 都要很努力地维护 transcript/replacement history 的恢复卫生；Pulsara 已经有 durable event truth，所以 compaction 应该成为 event-sourced read model 的一个 projection，而不是新的 truth。
