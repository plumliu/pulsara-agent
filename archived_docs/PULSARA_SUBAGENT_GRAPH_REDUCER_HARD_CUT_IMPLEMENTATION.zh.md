# Pulsara Subagent Graph 单一 Reducer Hard Cut 实施规格

> 状态：Implementation-ready draft
>
> 适用范围：Subagent runtime graph facts、bootstrap、scheduler、inspect/list、wait/delivery、child execution lifecycle
>
> 依赖文档：`ARCHITECTURE_DEBT_AUDIT.zh.md` 第 4、5、13、14 节
>
> 实施策略：开发期 hard cut；不保留旧 event payload、旧 bootstrap 或旧 mutable cache 的兼容路径

## 0. 文档目标

本文把架构债务审计中的“Subagent graph 单一 reducer”收敛成可以直接拆 PR、写代码和验收的工程规格。

它不只是要求新增一个 reducer，而是冻结以下四层边界：

1. **Durable graph facts**：只由 parent runtime session 中的 typed `Subagent*Event` 归约；
2. **Graph hydration**：从 archive 与 child event log 补充正文和 child-native facts，但不修改 durable graph；
3. **Committed reducer seam**：event commit 后、observer publish 前，将 stored events 交给 deterministic reducers；
4. **Child execution registry**：只持有当前进程的 coroutine/session/reservation/cancel handle，不冒充 durable graph truth。

最终要删除三套相互漂移的状态维护逻辑：

- command path 对 `SubagentRuntime._tasks/_runs/_results/...` 的手工 mutation；
- `_bootstrap_from_parent_event_log()` 及其 `_bootstrap_*` 分支；
- `project_subagent_graph()` 内独立维护的 event switch。

完成后，Pulsara 只有一条 graph truth 路径：

```text
parent Subagent*Event stream
        |
        v
pure SubagentGraphReducer
        |
        +----> scheduler / wait / delivery / command validation
        +----> list_agents projection
        +----> inspector projection
        +----> async hydrator ----> execution input / bounded hydrated view
```

## 1. 冻结结论

### 1.1 Subagent graph 的真源

parent runtime session 的 typed `Subagent*Event` 是 Subagent task/run/result/edge graph 的唯一 durable 真源。

以下内容不是 graph 真源：

- `SubagentRuntime` 内存字典；
- child `RuntimeSession` Python 实例；
- child coroutine 的 `asyncio.Task`；
- archive 中的完整 task/result 正文；
- 当前进程的 `default_budget`；
- inspector 临时投影；
- tool 返回给模型的 JSON；
- child raw transcript。

### 1.2 Fact、hydrated view 与 execution handle 必须是三种不同类型

不允许继续用当前 `SubagentTask` / `SubagentRun` 同时承载：

- event 可恢复事实；
- archive hydration 后的正文；
- 当前进程 execution handle 的关联信息。

V1 hard cut 后采用：

```text
SubagentTaskFact / SubagentRunFact / SubagentResultFact / SubagentEdgeFact
    只含 parent event 可归约事实

HydratedSubagentTaskView / HydratedSubagentRunView / HydratedSubagentResultView
    fact + archive/child-log 的 bounded hydration

ChildExecutionHandle
    child RuntimeSession + asyncio.Task + reservation + cancellation handle
```

### 1.3 Graph reducer 必须纯函数化

核心 API 冻结为：

```python
def apply_subagent_event(
    state: SubagentGraphState,
    event: AgentEvent,
) -> SubagentGraphState: ...

def fold_subagent_graph(
    events: Iterable[AgentEvent],
    *,
    initial: SubagentGraphState | None = None,
) -> SubagentGraphState: ...
```

约束：

- 不读取 archive；
- 不打开 child event log；
- 不读取当前 runtime 配置；
- 不创建 session/coroutine；
- 不发布事件；
- 不写日志或数据库；
- 不调用 wall clock；
- 对相同 state + event 必须得到相同结果。

### 1.4 Command 不能直接改 graph

所有 graph state transition 必须先形成 typed event 并 durable commit，再由 committed reducer apply。

禁止：

```python
self._tasks[task_id] = replace(task, status="running")
self._runs[run_id] = replace(run, result_id=result_id)
self._consumed_result_ids.add(result_id)
```

允许：

```text
validate / plan events
    -> commit stored events
    -> committed reducer apply
    -> inspect new graph state
    -> operate ephemeral execution registry
    -> publish observers
```

### 1.5 Stage A 与 Stage B 分开验收

- **Stage A**：fact schema、budget snapshot、pure reducer、bootstrap/projector/hydrator；无 writer 前置依赖。
- **Stage B**：conditional atomic EventLog batch + minimal committed-reducer seam、command path、execution registry、sync/async cancellation；依赖 Stage A。

不得在 Stage A 使用“`await emit()` 成功后再 apply reducer”作为临时生产语义。`emit()` 可能在 durable commit 后因 observer failure 抛异常，这会立即制造第四种漂移。

### 1.6 既有 product/security contract 不因 reducer 重构改变

- parent 的 `spawn_agent/create_agent_tasks/wait_agent/wait_agent_tasks/stop_agent/stop_agent_task/list_agents` 始终在 capability exposure中，但全部 bypass-only；
- non-bypass call由permission gate deny，stable reason=`subagent_requires_bypass_mode`；
- gate依据parent当前run的immutable permission snapshot，不读session下一run default；
- child profile仍不得超过parent exposure/permission，且默认更窄；
- child exposure不包含Subagent system tools，V1即使bypass也不允许nested child；
- child memory disabled；不recall、不持有memory tools、不写governance；
- child approval、MCP input-required、plan question继续在child内fail closed，不占parent pending slot；
- reducer只重构facts/state ownership，不放宽任何tool、profile或permission边界。

## 2. 当前代码真相与具体债务

### 2.1 当前 hydrated DTO 不是 reducer fact

当前 [`runtime/subagent/types.py`](src/pulsara_agent/runtime/subagent/types.py) 中：

- `SubagentTask.objective` 保存完整正文，但 `SubagentTaskCreatedEvent` 只有 `objective_preview` 与 `objective_artifact_id`；
- `SubagentRun.task` 保存完整正文，但 `SubagentRunStartedEvent` 只有 `task_preview`，完整 prompt 由 `SubagentMessageSentEvent.message_artifact_id` 指向；
- `SubagentRun.child_run_id` 由无 event 的 `set_child_run_id()` 写 live cache；
- `SubagentRun.budget` 会影响 timeout 与结果裁剪，但 `SubagentRunStartedEvent` 未保存 budget。

因此现有 DTO 不能作为 `fold(parent_events)` 的输出。

### 2.2 当前存在三套 reducer

1. [`runtime/subagent/runtime.py`](src/pulsara_agent/runtime/subagent/runtime.py) command methods 在 emit 后手工更新 `_tasks/_runs/_results`；
2. 同文件 `_bootstrap_from_parent_event_log()` 维护另一套 event switch；
3. [`runtime/subagent/projection.py`](src/pulsara_agent/runtime/subagent/projection.py) 维护第三套 event switch。

已确认 bootstrap 不处理 `SubagentTaskScheduledEvent` 与 `SubagentTaskBlockedEvent`，而 projection 会处理。现有测试可全部通过，但 restart 后 `waiting_dependency` 会漂移成 `created`。

### 2.3 当前 commit/publish 顺序不足以承载 live reducer

[`runtime/session.py`](src/pulsara_agent/runtime/session.py) 当前顺序为：

```text
event_log.append/extend
    -> await publisher.publish
    -> return stored event
```

subscriber failure 时，publisher 已推进 canonical sequence，但 `emit()` 会抛异常；`emit_many()` 的整个 batch 已提交，第一个 publish failure 还会阻止后续 stored events进入 publish loop。

因此 Stage B 必须先增加：

```text
commit
    -> apply committed reducers
    -> publish observers
    -> EventWriteResult(commit truth, publication errors)
```

### 2.4 当前 child execution handle 与 graph 混放

`SubagentRuntime` 同时保存：

- `_runs/_tasks/_results`：durable graph 的内存副本；
- `_child_sessions`：process-local runtime object；
- `_child_tasks`：process-local coroutine handle；
- implicit capacity state。

hard cut 后前一组删除，后一组移入 `ChildExecutionRegistry`。

## 3. 目标模块边界

建议新增/重构为：

```text
src/pulsara_agent/runtime/subagent/
├── facts.py              # immutable reducer facts / graph state / diagnostics
├── reducer.py            # pure apply/fold and transition validation
├── hydration.py          # archive + child EventLog async hydration
├── execution.py          # ChildExecutionRegistry and handles
├── commands.py           # pure-ish command/event planners; no state mutation
├── projection.py         # GraphState -> list/inspect normalized projection
├── runtime.py            # orchestration facade; I/O and command sequencing
├── profiles.py           # existing profile resolution, out of reducer
└── types.py              # public hydrated/tool view DTOs only

src/pulsara_agent/event/
├── subagent_dto.py       # event-visible frozen snapshot DTOs
└── events.py             # typed Subagent events

src/pulsara_agent/runtime/
├── committed.py          # reducer registration + EventWriteResult
└── session.py            # commit/apply/publish sequencing
```

不要求第一轮物理文件名完全相同，但依赖方向必须满足：

```text
event-visible DTO
    -> event schema
    -> subagent facts/reducer
    -> hydration/projection/commands
    -> SubagentRuntime
    -> tools / AgentRuntime / HostSession
```

`event` 层不得 import `runtime.subagent.types`。

## 4. Event contract hard cut

### 4.1 `SubagentRunStartedEvent` 的 immutable snapshots

`SubagentRunStartedEvent` 必须将以下三个字段改为 required、typed、`extra="forbid"` 的 event-visible snapshot：

```python
class SubagentContextPolicySnapshotEvent(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    mode: Literal["isolated", "fork"]
    include_parent_summary: bool
    include_parent_current_task: bool
    include_parent_memory_projection: bool
    include_parent_artifact_refs: bool
    max_parent_context_chars: int | None
    fork_source_context_id: str | None


class SubagentCapabilityProfileSnapshotEvent(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    profile_id: str
    profile_name: SubagentCapabilityProfileName
    inherited_from_parent_context_id: str | None
    permission_mode: Literal[
        "read-only",
        "ask-permissions",
        "accept-edits",
        "bypass-permissions",
    ]
    permission_policy: dict[str, object]
    allowed_tool_names: tuple[str, ...]
    allowed_descriptor_ids: tuple[str, ...]
    allowed_skill_names: tuple[str, ...]
    allowed_mcp_server_ids: tuple[str, ...]
    can_spawn_subagents: bool
    max_spawn_depth_from_root: int
    memory_enabled: bool
    computed_from_parent_exposure_generation: int | None
    diagnostics: tuple[dict[str, object], ...]


class SubagentBudgetSnapshotEvent(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    max_concurrent_children_per_parent_run: int
    max_concurrent_children_per_host_session: int
    max_spawn_depth_from_root: int
    child_timeout_seconds: float | None
    max_total_child_runs_per_parent_run: int
    max_result_summary_chars_per_child: int
    max_subagent_results_per_parent_compile: int
```

`SubagentRunStartedEvent` 最终字段：

```python
context_policy: SubagentContextPolicySnapshotEvent
capability_profile: SubagentCapabilityProfileSnapshotEvent
budget_snapshot: SubagentBudgetSnapshotEvent
```

同时删除重复的 `spawning_tool_call_id` 字段。canonical initiator是 `spawn_initiator_kind + spawn_initiator_id`；当kind为`tool_call`时，projection可将id展示为spawning tool call id，scheduler/dependency启动时不得伪造tool call。`spawning_tool_name`只在kind为`tool_call`时允许非空。

### 4.2 Snapshot validators

必须校验：

- 所有 count/char cap 为有限整数且 `>= 0`；并发/总 run cap 至少为 `1`；
- `child_timeout_seconds` 为 `None` 或 finite、`> 0`；拒绝 NaN/inf；
- capability `permission_mode` 必须是 production preset；
- `permission_policy == preset_to_policy(permission_mode).to_dict()`；
- `can_spawn_subagents=False` 且 `max_spawn_depth_from_root=0`，符合当前 V1 禁止 nested subagent；
- `memory_enabled=False`；
- tool/descriptor/skill/server ids 去重并使用 canonical tuple order；
- context `mode="isolated"` 时 `fork_source_context_id is None`；
- `max_parent_context_chars` 非空时必须为正数；V1 不借此 PR 改变现有 fork context 的默认 cap/source 解析行为。

permission preset validator 必须来自低层共享 contract helper，event DTO 不得反向 import `runtime.permission`。

Snapshot 是 run 创建时的事实，之后 parent default/profile/config 改变不得重写它。

### 4.3 `budget_snapshot` 的真源规则

- spawn/create command 在 event 构造前完成 budget resolution；
- scheduler、timeout、result clipping、delivery count 都读取 run fact 中的 snapshot；
- restart 后不允许使用新的 `SubagentRuntime.default_budget` 补旧 run；
- 缺字段的旧 `SubagentRunStartedEvent` 是 unsupported schema；
- 本项目尚未上线，实施时 reset/migrate event log，不提供 fallback。

### 4.4 完整正文仍只通过 artifact ref 表达

- `SubagentTaskCreatedEvent.objective_artifact_id` 是 task objective 正文的 canonical ref；
- `SubagentMessageSentEvent(delivery_kind="spawn_task").message_artifact_id` 是 child spawn prompt 正文的 canonical ref；
- event 中只保留 bounded preview；
- reducer 不读取 artifact；
- hydration 失败不允许把 preview 伪装成完整正文。

task-backed run复用owning task的`objective_artifact_id`作为spawn-task `message_artifact_id`，不再为相同objective写第二份`<subagent_run_id>:task` artifact；reducer验证两者ref相等。primitive `spawn_agent`没有task entity，仍创建run-owned task artifact。

### 4.5 `child_run_id` hard cut

V1 删除 `SubagentRuntime.set_child_run_id()`。

`child_run_id` 的规则：

1. child runtime raw `RunStartEvent.run_id` 是 child-native 真源；
2. `SubagentGraphHydrator` 通过 `child_runtime_session_id` 定位 child event log并读取；
3. parent terminal/edge event 若已有 `child_run_id`，它只是 parent graph 中已观察到的 bounded attribution，必须与 child log 一致；
4. 不允许通过无 event 的 live cache 注入；
5. 如果未来 parent graph 在 child terminal 之前必须 canonical 地引用 child run，再单独新增 `SubagentChildRunBoundEvent`，V1 不预埋。

### 4.6 Event context 与 stream 归属保持不变

- 所有 `SubagentRunStarted/Completed/Failed/Cancelled/Suspended`、task、edge、phase、result、consumption、delivery events 继续写 parent runtime session EventLog；
- child RuntimeSession 只写普通 `AgentRuntime` raw loop events；
- parent graph event 的 `EventBase.run_id/turn_id/reply_id` 使用 owning parent context；
- child 调用 `report_agent_phase/report_agent_result` 时，tool runtime携带的是child-native `EventContext`；该context只属于child raw loop，不得复制到parent EventLog。对应 `PhaseReported/ResultSubmitted` 必须从run fact恢复原spawn parent context，tool call id单独保留调用归因；
- detached child 在 parent `RunEndEvent` 后完成时，completed/failed/cancelled 仍挂原 spawn parent run，不重新打开 parent run；
- delivery event 挂实际 delivery 的 parent model-call context；
- graph events不进入 parent transcript conversational replay。

### 4.7 Event batch 原子组

以下 event 必须使用一次 `EventLog.extend()` / `write_events()` durable batch：

| 语义 | 最小原子组 |
|---|---|
| primitive spawn | `SubagentRunStartedEvent` + spawn-task `SubagentMessageSentEvent` |
| task start | `TaskScheduled` + `RunStarted` + spawn-task `MessageSent` + `TaskStarted` |
| explicit result completion | `ResultSubmitted` 已存在后，`RunCompleted` + `TaskCompleted` |
| inferred completion | `RunCompleted` + optional `TaskCompleted` |
| run failure | `RunFailed` + optional `TaskFailed` + transitive `TaskBlocked` cascade |
| run cancellation | `RunCancelled` + optional `TaskCancelled` + transitive `TaskBlocked` cascade |
| batch materialization | 全部 `TaskCreated/Blocked/Scheduled/RunStarted/MessageSent/TaskStarted` facts |
| wait consumption | 单个 `ResultConsumed` 或 run primitive 的单个 wait edge，不双写两种真源 |

“同一语义原子组”是 graph consistency 要求，不表示 child coroutine start 能和数据库事务原子化。child start 发生在 commit 后；失败时写 repair terminal events。

这里的“原子组”依赖第10.3节的EventLog hard contract，不只是RuntimeSession调用约定：整批sequence连续、不与其他batch交错、任一失败零partial events。PR3必须先完成该contract，PR5才允许依赖这些原子组实现all-or-nothing materialization。

### 4.8 Repair attribution 与 conditional validators

为避免 post-commit batch repair 只能依赖自由 metadata，PR0 同时补：

```python
class SubagentRunFailedEvent / SubagentRunCancelledEvent:
    batch_id: str | None = None
    create_tool_call_id: str | None = None
    repair_id: str | None = None

class SubagentTaskFailedEvent / SubagentTaskCancelledEvent:
    batch_id: str | None = None
    create_tool_call_id: str | None = None
    repair_id: str | None = None
```

task-backed run/task terminal event 的 batch/create attribution必须与 owning task/run fact一致。同一次 post-commit repair 的所有 terminal events共享一个 `repair_id`。

同时在 event schema 层冻结：

- `SubagentTaskCreatedEvent.objective_artifact_id` required；task objective不允许只有preview；
- `SubagentMessageSentEvent.message_artifact_id` required；尤其 `spawn_task` 必须与 `RunStarted` 同批commit；
- `SubagentResultSubmittedEvent.result_artifact_id` 与 `SubagentRunCompletedEvent.result_artifact_id` required，且必须出现在`artifact_ids`；
- `SubagentTaskCompletedEvent.subagent_run_id/result_id` required；V1不存在无child run或无result的completed task；
- `SubagentResultConsumedEvent(kind="wait_run")`：`subagent_run_id` required；
- `kind="wait_task"`：`task_id` required；
- 两个 target id不能都为空；
- `result_id is None` 时 `terminal_event_id` required；
- `consumed_status="completed"` 时 `result_id` required；failed/cancelled/blocked才允许nullable result；
- `SubagentResultDeliveredEvent` 的 `parent_run_id/context_id/model_call_index/section_id` required，因为 delivered只表示进入实际发起的 model-call payload；
- `SubagentResultDeliveredEvent.result_artifact_id` required，并与对应result fact一致；
- task-backed `SubagentTaskCompletedEvent` 的 result/run attribution必须能与同 batch前序 completion join。

## 5. Reducer fact schema

### 5.1 通用 provenance

每个 entity fact 至少保留：

```python
@dataclass(frozen=True, slots=True)
class SubagentFactProvenance:
    created_event_id: str
    created_sequence: int
    last_event_id: str
    last_sequence: int
    created_at: datetime
    updated_at: datetime
    terminal_event_id: str | None = None
    terminal_sequence: int | None = None
```

所有 sequence 必须是 stored canonical sequence。未存储的 event 不得进入 reducer。

### 5.2 `SubagentTaskFact`

```python
@dataclass(frozen=True, slots=True)
class SubagentTaskFact:
    task_id: str
    parent_run_id: str
    parent_turn_id: str | None
    parent_reply_id: str | None

    batch_id: str | None
    create_tool_call_id: str | None
    task_key: str | None
    label: str | None
    profile_id: str
    display_role: str | None

    objective_preview: str
    objective_artifact_id: str
    depends_on: tuple[str, ...]

    status: SubagentTaskStatus
    current_run_id: str | None
    run_index: int | None

    scheduled_at: datetime | None
    schedule_reason: Literal["immediate", "dependency_satisfied", "manual"] | None

    phase: str | None
    result_id: str | None

    blocked_reason: str | None
    blocked_by_task_ids: tuple[str, ...]
    dependency_status_snapshot: Mapping[str, str]
    dependency_terminal_event_ids: Mapping[str, str]
    dependency_generation: int | None

    failure_reason_code: str | None
    cancellation_reason_code: str | None

    provenance: SubagentFactProvenance
```

明确不包含：完整 `objective`、child session object、execution task handle。

`has_child_run` 与 task `pending_state` 不是独立fact：前者派生自 `current_run_id is not None`，后者派生自 owning run 的 suspended/pending fields。projection可以输出这些方便字段，但不得让它们成为会与基础fact冲突的第二自由度。

`SubagentTaskScheduledEvent` 不引入新的 public task status。它只写 `scheduled_at/schedule_reason`；紧随其后的 `TaskStarted` 将 status 改为 `running`。这样 event prefix 截断在 Scheduled 后时，fact 仍保持此前的 `created/waiting_dependency`，同时可审计“已计划但尚未启动”。

### 5.3 `SubagentRunFact`

```python
@dataclass(frozen=True, slots=True)
class SubagentRunFact:
    subagent_run_id: str
    parent_runtime_session_id: str
    parent_run_id: str
    parent_turn_id: str | None
    parent_reply_id: str | None
    parent_context_id: str | None
    parent_model_call_index: int | None

    edge_id: str
    spawning_tool_name: str | None
    spawn_initiator_kind: SubagentSpawnInitiatorKind | None
    spawn_initiator_id: str | None

    child_runtime_session_id: str
    reported_child_run_id: str | None
    task_id: str | None
    batch_id: str | None
    create_tool_call_id: str | None
    run_index: int | None

    label: str | None
    role: SubagentRole
    profile_id: str | None
    task_preview: str
    task_artifact_id: str | None

    context_policy: SubagentContextPolicySnapshot
    capability_profile: SubagentCapabilityProfileSnapshot
    budget_snapshot: SubagentBudgetSnapshot

    status: DurableSubagentRunStatus  # running | suspended | completed | failed | cancelled
    phase: str | None
    pending_kind: str | None
    pending_reason_code: str | None

    result_id: str | None
    failure_reason_code: str | None
    cancellation_reason_code: str | None

    provenance: SubagentFactProvenance
```

`reported_child_run_id` 只来自 parent terminal/edge event；hydrator 从 child log 获得的值不回写该字段。

durable graph不使用 `starting`。commit前的prepared/starting只属于 `ChildExecutionRegistry`；`RunStartedEvent` commit后run fact直接是`running`，启动失败随即追加terminal repair fact。这样不会把未提交reservation误写成durable run status。

task/run fact只保存`result_id`。`result_source`与primary result artifact从`SubagentResultFact`派生；现有TaskCompleted/RunCompleted event中的冗余值只能作为cross-event consistency assertion，不能在fact中形成第二自由度。projection需要方便字段时从result mapping计算。

### 5.4 `SubagentResultFact`

```python
@dataclass(frozen=True, slots=True)
class SubagentResultFact:
    result_id: str
    subagent_run_id: str
    task_id: str | None
    status: Literal["submitted", "completed"]
    result_source: Literal["explicit", "inferred"]

    summary: str
    output_preview: str | None
    final_message_artifact_id: str
    artifact_ids: tuple[str, ...]
    diagnostics: tuple[Mapping[str, object], ...]
    token_usage: Mapping[str, object] | None
    tool_call_count: int | None

    provenance: SubagentFactProvenance
```

规则：

- `ResultSubmitted` 创建 `submitted/explicit` fact；
- prior explicit result存在时，`RunCompleted.result_id`必须与`run.result_id`完全相同；不同id是ledger conflict，不能创建第二个替代result；
- 同 result id 的 `RunCompleted` 只能将其变为 `completed` 并补 token/tool count/terminal provenance；`summary/output_preview/artifact_ids/diagnostics/result_source`继续以`ResultSubmitted` fact为准，completion不得覆盖；
- completion携带的summary/artifact冗余字段必须与explicit result一致，否则记录`subagent_explicit_result_completion_mismatch`并保留先到的explicit正文；
- 没有 prior submission 的 `RunCompleted` 创建 `completed/inferred` fact；
- failed/cancelled run 可以没有 result fact；
- event inline preview 必须 bounded，完整 result 仍由 artifact ref恢复。

### 5.5 `SubagentEdgeFact`

沿用现有 `SubagentEdge` 字段，但改为 fact 名，并增加 provenance。`RunStarted` 可先创建 spawn edge，随后 `MessageSent` 用相同 `edge_id` 补 `payload_artifact_id`，这属于同一 entity 的合法 enrich，不是第二条 edge。合法 enrich 的完整 identity（edge kind、parent runtime/run/turn/reply、subagent run、child runtime session）必须相同；任何已有edge id换owner/kind/session都记录`subagent_edge_identity_conflict`，reducer不得覆盖旧edge。

### 5.6 Consumption 与 delivery facts

不再用独立 mutable set 作为真源。`SubagentGraphState` 内保存：

```python
@dataclass(frozen=True, slots=True)
class SubagentConsumptionFact:
    consumption_id: str
    kind: Literal["wait_run", "wait_task"]
    consumer_tool_call_id: str
    task_id: str | None
    subagent_run_id: str | None
    result_id: str | None
    consumed_status: Literal["completed", "failed", "cancelled", "blocked_dependency_failed"]
    terminal_event_id: str | None
    diagnostics: tuple[Mapping[str, object], ...]
    provenance: SubagentFactProvenance


@dataclass(frozen=True, slots=True)
class SubagentDeliveryFact:
    result_id: str
    subagent_run_id: str
    parent_run_id: str
    parent_turn_id: str | None
    parent_reply_id: str | None
    context_id: str
    model_call_index: int
    section_id: str
    result_artifact_id: str
    provenance: SubagentFactProvenance


consumptions: Mapping[str, SubagentConsumptionFact]  # key = consumption_id or wait edge id
deliveries: Mapping[str, SubagentDeliveryFact]  # key = result_id
```

run primitive wait edge转换成`SubagentConsumptionFact`时，`consumption_id=edge_id`、`consumer_tool_call_id=returned_to_tool_call_id`；如果wait edge缺这两个必要归因，reducer标记invalid。delivery fact不复制summary，summary从result fact派生并做event一致性校验。

projection 可派生：

- `consumed_result_ids`；
- `consumed_task_ids`；
- `delivered_result_ids`。

run primitive `wait_agent` 继续只写 wait edge；task layer `wait_agent_tasks` 只写 `SubagentResultConsumedEvent`。同一次 wait 不双写。`SubagentResultDeliveredEvent.summary`与`SubagentTaskCompletedEvent.result_source`都是cross-event assertion：必须分别等于canonical result fact的`summary/result_source`，不能形成第二自由度。

### 5.7 `SubagentGraphState`

```python
@dataclass(frozen=True, slots=True)
class SubagentGraphState:
    tasks: Mapping[str, SubagentTaskFact]
    runs: Mapping[str, SubagentRunFact]
    results: Mapping[str, SubagentResultFact]
    edges: Mapping[str, SubagentEdgeFact]
    consumptions: Mapping[str, SubagentConsumptionFact]
    deliveries: Mapping[str, SubagentDeliveryFact]
    diagnostics: tuple[SubagentGraphDiagnostic, ...]
    consistent: bool
    through_sequence: int
    applied_subagent_event_ids: frozenset[str]
```

实现可在 `fold()` 内使用私有 mutable builder 保证 O(n)，但对外状态必须冻结；command 不得取得 builder 引用。

这里的“冻结”是递归不可变而非只给dataclass/Pydantic外壳加`frozen=True`：event → fact边界必须detach并递归冻结permission policy、diagnostics、token usage和其他嵌套JSON-like mapping/list。任何event原对象后续mutation、fact属性链mutation、convenience DTO mutation都不能无event改变graph state。

`through_sequence` 针对 parent session 全事件流，不只 Subagent events。非 Subagent event 不改变 entity facts，但仍推进 high-water mark。

`applied_subagent_event_ids` 只保存 Subagent graph vocabulary 的 ids，不保存所有 parent transcript/tool events；incremental idempotency 主要依赖 canonical sequence high-water，避免把完整 parent history id集合复制进 graph state。

### 5.8 Graph diagnostic

```python
@dataclass(frozen=True, slots=True)
class SubagentGraphDiagnostic:
    code: str
    severity: Literal["warning", "error"]
    event_id: str
    sequence: int
    entity_kind: Literal["task", "run", "result", "edge", "graph"]
    entity_id: str | None
    message: str
    metadata: Mapping[str, object]
```

message/metadata 必须 bounded、redacted，不得放 task/result 全文。

递归冻结只存在于canonical fact内部。进入projection/inspector/tool JSON边界时必须统一调用Pulsara-owned recursive thaw helper，把`MappingProxyType`、tuple、nested mappings、dataclass、datetime/enum转换成普通JSON-safe值；禁止直接对含mapping proxy的fact调用`dataclasses.asdict()`，也禁止只对diagnostic做浅层`dict()`。inconsistent graph仍必须可inspect，带嵌套diagnostic的result仍必须可由wait工具序列化。

## 6. Reducer 状态机

### 6.1 通用处理顺序

`apply_subagent_event()`：

1. 校验 event 已有 `id`、canonical `sequence`、parseable UTC `created_at`；
2. `sequence <= through_sequence`：
   - 已知 Subagent event id：idempotent no-op；
   - 非 Subagent event或无法证明冲突的历史 replay：按 high-water no-op；
   - 已知同一 Subagent event id出现在不同 sequence：记录 `subagent_event_sequence_reuse` error；
3. `sequence > through_sequence + 1`：记录 `subagent_event_sequence_gap`，state inconsistent；live store 不继续 command；fresh full-log fold 也不得静默跳过；
4. 非 Subagent event：只推进 `through_sequence`；
5. Subagent event：按下表 apply；
6. 运行 entity invariants；
7. 返回新 immutable state。

### 6.2 Event transition table

| Event | 前置条件 | Fact transition | 非法情况 |
|---|---|---|---|
| `RunStarted` | run id与spawn edge id均不存在；task-backed 时 task 已存在或同 batch 前序已创建 | 创建 running run；创建 spawn edge；保存 immutable snapshots | duplicate run/edge、edge owner collision、unknown task、task 已有 run -> error |
| `MessageSent(spawn_task)` | run 存在且parent/child runtime及完整edge identity一致 | 补 run.task_artifact_id；补 spawn edge payload ref | unknown run/session/edge owner或payload mismatch -> error |
| `MessageSent(send/followup)` | run 存在且parent/child runtime及完整edge identity一致 | 创建或同identity补 send/followup edge与payload artifact ref；不改spawn task正文 | unknown run/runtime session/edge owner/kind冲突 -> error |
| `RunSuspended` | run running；parent/child runtime attribution一致 | status=suspended，写 pending fields | session drift或invalid transition -> error |
| `RunCompleted` | run running/suspended；parent/child runtime attribution与owning run一致；reported child run id若已有只能重复同值；若已有explicit result则id及冗余正文/artifact一致 | inferred时create result；explicit时只complete并补运行统计；首次写reported child id | terminal/session/child-run/result identity/body mismatch -> error，first explicit fact不变 |
| `RunFailed` | run running/suspended；parent/child runtime attribution一致 | run failed，写 reason | terminal/session conflict -> error |
| `RunCancelled` | run non-terminal；parent/child runtime attribution一致 | run cancelled，写 reason | session mismatch或completed/failed 后 cancel -> conflict error；相同 terminal event duplicate no-op |
| `EdgeRecorded` | referenced run与parent/child runtime session一致；reported child run id若已有只能重复同值；wait/result edge引用该run自己的completed result和同一primary artifact | 新建 edge；首次补reported child id；wait edge可产生 consumption fact | duplicate edge、cross-run result、child-run/artifact/session mismatch -> error |
| `ResultDelivered` | result completed；对应 run存在且parent runtime session一致；event summary等于result fact summary | 写 delivery fact | 未完成/未知 result、wrong parent runtime、summary drift -> error |
| `TaskCreated` | task id 不存在；depends_on 不含自己 | 创建 created task | duplicate key/id/self dependency -> error |
| `TaskScheduled` | task created/waiting；无 child run | 写 scheduled_at/reason，不改变 status | terminal/already-running -> error |
| `TaskStarted` | task created/waiting；run 已存在且 run.task_id匹配 | task running/current_run；projection据此派生has_child_run | unknown/mismatched run、run_index != 1 -> error |
| `TaskBlocked(waiting)` | task created/waiting，无 child run | status waiting_dependency，保存 blocker facts | running/terminal -> error |
| `TaskBlocked(failed)` | task created/waiting；至少一依赖 terminal failure/cancel/block | status blocked_dependency_failed，terminal provenance=本事件 | missing blocker terminal refs -> error |
| `TaskCompleted` | task running；run completed或同 batch前序已完成；event result_source等于result fact | task completed/result attribution | result/run/artifact/source mismatch -> error |
| `TaskFailed` | created/waiting task可无run；running task必须携带current run id且owning run已failed | task failed | missing/wrong/live/completed owning run或既有terminal -> error |
| `TaskCancelled` | created/waiting task可无run；running task必须携带current run id且owning run已cancelled | task cancelled | missing/wrong/live/failed owning run或既有terminal -> error |
| `PhaseReported` | run running；task id 若有必须匹配 | 更新 run.phase 与 task.phase | unknown/mismatch/terminal -> warning或error（见 6.5） |
| `ResultSubmitted` | run running；task id若有必须匹配 | 创建 submitted explicit result；run result attribution | duplicate different result、terminal run -> error |
| `ResultConsumed` | kind/id invariant；target settled | 写 consumption fact | completed无 result且无 terminal event -> error |

### 6.3 Terminal monotonicity

Terminal status 一旦确定，不允许回退或被不同 terminal outcome 覆盖。

- 相同 event id重放：no-op；
- 不同 event id重复同一 terminal status且内容一致：warning `duplicate_terminal_fact`，first wins；
- 不同 terminal status：error `conflicting_terminal_fact`，first wins，state inconsistent；
- inconsistent state 仍可供 inspector 展示，但所有 mutation command fail closed，直到从完整 log rebuild/repair。

### 6.4 Task/run V1 invariants

- 一个 task V1 最多一个 child run；
- `run_index` 固定 `1`，没有 retry/re-attempt/reset/redefine；
- upstream completed 才能使 dependent runnable；
- upstream failed/cancelled/blocked_dependency_failed 使 downstream terminal `blocked_dependency_failed`；
- block 必须递归传播到完整 downstream；
- failed/blocked task 不会未来自动恢复；主 agent应亲自处理或创建新 task；
- task-backed run 的 `batch_id/create_tool_call_id` 必须等于 task creation attribution；
- `spawn_initiator_kind="dependency_satisfied"` 时不伪造 tool call id。
- running task不能脱离owning run单独terminalize：run terminal fact必须先出现（可在同一atomic batch前序），task terminal status必须与owning run相同；created/waiting且从未启动的task才允许无run直接failed/cancelled。

### 6.5 Phase/result late event policy

由于跨线程/async close 边界可能出现 late report：

- terminal event sequence 之后的 `PhaseReported`：不修改 fact，warning `subagent_phase_after_terminal`；
- terminal event 之后的 `ResultSubmitted`：error，state inconsistent；explicit result必须先于 completion；
- same batch 中 `ResultSubmitted -> RunCompleted -> TaskCompleted` 是合法顺序；
- final assistant text 不覆盖 prior explicit result，只能形成 bounded diagnostic。

### 6.6 Same-batch dependency terminal refs

删除 synthetic terminal ref。event 构造时已经有 stable `event.id`，因此同一 materialization batch 中：

1. 先为 upstream blocked event构造真实 event object；
2. downstream `dependency_terminal_event_ids` 引用 upstream blocked event 的 `id`；
3. durable commit 后 sequence 可用于 join；
4. `dependency_generation` 对排序后的真实 terminal event ids做稳定 hash/版本化计算。

V1 算法冻结为：对按task id排序后的 `{task_id: terminal_event_id}` canonical JSON做 SHA-256，取前12个hex字符转为int（48 bit）；空mapping为`None`。它是dependency fact版本token，不表示event顺序，也不能与canonical sequence比较。

这样 inspector 可以直接 join 到真实 typed event，不再解释 batch-local synthetic string。

## 7. `SubagentGraphStateStore`

pure reducer 之上增加一个极薄的 process-local state owner：

```python
class SubagentGraphStateStore:
    reducer_id: str

    @property
    def state(self) -> SubagentGraphState: ...

    def apply_committed(self, events: tuple[AgentEvent, ...]) -> None: ...

    def rebuild(self, events: Iterable[AgentEvent]) -> SubagentGraphState: ...
```

它不是第二 reducer，只持有 reducer 的最新输出。

规则：

- 初始化时从完整 parent event log fold；
- 注册 committed reducer 时带 `through_sequence`；
- 注册过程检查 event log current high-water，若初始化后又有 commit，先 catch up 再激活；
- `apply_committed` 按 sequence 严格排序；
- reducer error 标记 session reconciliation required；
- command 只通过 `state_store.state` 读取 graph；
- `SubagentRuntime.tasks/runs/results` 返回事实或 projection，不返回可被替换的内部 dict。

## 8. Async `SubagentGraphHydrator`

### 8.1 职责

Hydrator 只负责将 durable fact 加载成 bounded runtime view：

```python
class SubagentGraphHydrator:
    async def hydrate_task(
        self,
        fact: SubagentTaskFact,
        *,
        max_chars: int,
    ) -> HydratedSubagentTaskView: ...

    async def hydrate_run(
        self,
        fact: SubagentRunFact,
        *,
        include_task_text: bool,
        include_child_native: bool,
        max_chars: int,
    ) -> HydratedSubagentRunView: ...

    async def hydrate_result(
        self,
        fact: SubagentResultFact,
        *,
        max_chars: int,
    ) -> HydratedSubagentResultView: ...
```

### 8.2 Archive hydration

来源：

- task objective：`SubagentTaskFact.objective_artifact_id`；
- run spawn task：`SubagentRunFact.task_artifact_id`；
- result full text：`final_message_artifact_id` / primary artifact ref。

规则：

- archive sync API 用 `asyncio.to_thread` 包装，不能在 async command 热路径直接阻塞；
- 读取时必须带 parent runtime session id；
- max_chars 为硬 cap；
- artifact missing/corrupt/permission mismatch 返回 preview + diagnostic，但 `complete=False`；
- child start 需要完整 task时，`complete=False` 是执行错误，不得用 preview 静默启动；
- list/inspect 默认不读完整正文，只显示 preview/ref；
- wait tool只读取 bounded result，超限通过 artifact_read恢复。

### 8.3 Child event log hydration

使用 `EventLogLocator`：

```text
parent RunFact.child_runtime_session_id
    -> EventLogLocator.open(child runtime session)
    -> child RunStartEvent / RunEndEvent / metadata["subagent"]
```

V1 规则：

- 选择 metadata attribution 与 `subagent_run_id/parent_runtime_session_id` 匹配的 child `RunStartEvent`；
- 0 个：`child_native_run_missing` diagnostic；
- 1 个：返回 `child_run_id`；
- 多个：`multiple_child_native_runs` error，除非未来 task retry contract明确加入；
- parent fact `reported_child_run_id` 非空时必须相等；不等则 `child_run_attribution_mismatch`；
- child log lookup 不改变 parent graph state；
- inspector 可按需展示 child timeline summary，但不把 raw child transcript塞进 parent projection。

### 8.4 Hydrated view schema

```python
@dataclass(frozen=True, slots=True)
class HydratedSubagentRunView:
    fact: SubagentRunFact
    task_text: str | None
    task_text_complete: bool
    child_run_id: str | None
    child_terminal_status: str | None
    diagnostics: tuple[SubagentHydrationDiagnostic, ...]
```

Task/result view同理。hydration diagnostic 与 graph diagnostic 分离：前者表示外部材料当前不可读，不等于 event stream 自相矛盾。

## 9. `ChildExecutionRegistry`

### 9.1 只保存 ephemeral handles

```python
@dataclass(slots=True)
class ChildExecutionHandle:
    subagent_run_id: str
    child_runtime_session_id: str
    child_session: RuntimeSession | None
    coroutine: asyncio.Task[None] | None
    capacity_reservation: ChildCapacityReservation | None
    cancellation_requested: bool
    release_requested: bool
    started_in_process_at: datetime


class ChildExecutionRegistry:
    def get(self, subagent_run_id: str) -> ChildExecutionHandle | None: ...
    def register_prepared(...): ...
    def attach_session(...): ...
    def attach_coroutine(...): ...
    def request_cancel(...): ...  # sync/thread-safe request only
    async def cancel(...): ...
    async def drain(...): ...
    def release_handle(...): ...  # live task时只mark closing
    def reconcile(graph: SubagentGraphState): ...
```

`ChildExecutionHandle` 可以有 process-local handle phase（prepared/started/closing/released），但不得复制 durable task/run status。

取消与释放顺序冻结为：先写durable run/task cancellation facts，再在`asyncio.Task` owning loop上请求cancel，await child coroutine退出（包括全部`finally`），最后关闭child RuntimeSession并释放capacity。`release_handle()`若从child coroutine内部调用，或目标task尚未done，只能标记`closing/release_requested`；task done callback或awaiting safe point才可执行physical release。sync/thread入口不得直接跨线程调用`Task.cancel()`，必须使用owner loop的`call_soon_threadsafe`；它也不得假装已经drained。

bounded drain超时时保留closing handle、child session与reservation并抛结构化/typed timeout，不得为了让Host close继续而强制pop。Host teardown只有在drain成功后才能关闭共享MCP/terminal/runtime资源；后续safe point可重试drain。

### 9.2 Reservation lifecycle

```text
preflight valid
    -> reserve capacity
    -> prepare artifacts/events
    -> commit graph facts
        commit fail -> release reservation, no child start
        commit success -> register/attach child resources
    -> start coroutine
        start fail -> emit terminal repair events, release reservation
    -> terminal child -> release reservation
```

reservation 不能写入 event payload。`budget_snapshot` 记录限制事实，不记录当前 slot handle。

### 9.3 Reconciliation

safe point 对账：

| Graph | Registry | 处理 |
|---|---|---|
| run active | handle active | 正常 |
| run active | handle missing | fail-closed dangling repair：RunFailed + TaskFailed/block cascade |
| run terminal | handle active | cancel/drain handle，不改 terminal fact |
| run missing | handle active | registry orphan，cancel/drain + diagnostic；不得生成伪 graph create |
| child session exists but no coroutine | graph active | 如不支持 durable resume则 dangling repair |

V1 不恢复进程崩溃前的 child coroutine；recovery 的正确语义是可解释地 fail closed，而不是假装继续。

### 9.4 Cap accounting

- per-parent-run concurrent = `graph active run ids ∪ registry attached/unreleased run ids` + 尚未attach的reservation slots；
- per-host-session concurrent = 整个 parent runtime session 上述同一union；detached或graph已terminal但coroutine仍closing的child在physical release前继续计数；
- total child runs per parent run = durable `RunStarted` facts总数，不因cancel/fail减少；
- waiting/blocked task没有 child run，不占concurrent slot；
- graph显示active但registry缺handle的dangling run在repair commit前仍占slot，避免restart窗口绕过cap；
- registry不能自行决定product cap，但必须提供uncommitted slot与attached/unreleased run ids；runtime用set union避免active graph+handle双计数，同时防止closing handle漏计；canonical total run count仍来自graph state。

## 10. Minimal committed-reducer seam

### 10.1 为什么是 Stage B 的硬前置

不能使用：

```python
stored = await runtime_session.emit(event)
graph_store.apply_committed((stored,))
```

因为 `emit()` 返回前会 await observers；observer failure 时 event 已 commit，但 `stored` 不会返回给 command。

### 10.2 API

新增低层 DTO：

```python
@dataclass(frozen=True, slots=True)
class EventPublicationError:
    event_id: str
    sequence: int
    subscriber_id: str | None
    error_type: str
    message: str  # bounded/redacted


@dataclass(frozen=True, slots=True)
class EventWriteResult:
    committed_events: tuple[AgentEvent, ...]
    commit_status: Literal["committed"]
    reducer_high_waters: Mapping[str, int]
    reconciliation_required: bool
    reducer_errors: tuple[CommittedReducerError, ...]
    publication_status: Literal["completed", "enqueued", "unavailable"]
    publisher_enqueued_through_sequence: int | None
    publication_errors: tuple[EventPublicationError, ...]

    def require_reduced(self, reducer_id: str) -> tuple[AgentEvent, ...]: ...
```

`RuntimeSession` 增加：

```python
async def write_event(
    self,
    event: AgentEvent,
    *,
    expected_last_sequence: int | None = None,
) -> EventWriteResult: ...

async def write_events(
    self,
    events: Sequence[AgentEvent],
    *,
    expected_last_sequence: int | None = None,
) -> EventWriteResult: ...

def write_events_from_thread(
    self,
    events: Sequence[AgentEvent],
    *,
    expected_last_sequence: int | None = None,
) -> EventWriteResult: ...

def register_committed_reducer(
    self,
    *,
    reducer_id: str,
    through_sequence: int,
    apply_committed: Callable[[tuple[AgentEvent, ...]], None],
) -> None: ...
```

Stage B 的 subagent command 只调用 `write_event(s)`，不调用 compatibility `emit/emit_many`。

`require_reduced("subagent_graph:<runtime_session_id>")` 只有在对应reducer high-water达到本批`last_sequence`且无该reducer error时成功。不能用所有reducers的最小值、最大值或publisher进度代替指定reducer事实。

DB append/transaction失败时抛`EventCommitError`且不返回`EventWriteResult`；只有已经durable的batch才有`commit_status="committed"`。任何commit outcome不确定的adapter错误都必须先按event id查询确认，不能盲目retry生成另一组ids。

`PlannedSubagentWrite.expected_through_sequence` 必须原样传为 `expected_last_sequence`。不匹配时在任何 event insert前抛：

```python
class EventLogWriteConflict(RuntimeError):
    expected_last_sequence: int
    actual_last_sequence: int


class EventWriteConflict(RuntimeError):
    runtime_session_id: str
    expected_last_sequence: int
    actual_last_sequence: int
```

EventLog adapter抛不依赖RuntimeSession identity的`EventLogWriteConflict`；SessionWriteCoordinator catch-up后将其翻译为带`runtime_session_id`的`EventWriteConflict`。caller捕获后释放/保留尚未commit的reservation，读取最新graph、重新plan/validate；不得提交旧plan，也不得把conflict记成reducer inconsistency。

### 10.3 EventLog production contract 与 pytest fake fidelity boundary

PR3 hard cut `EventLog` 的调用协议，但不要求所有 adapter 具备相同的运行能力：

> **冻结决策**：生产运行时只以 PostgreSQL EventLog 为 durable truth；`InMemoryEventLog` 只是 pytest test double，不是需要继续建设的 product backend。PR3 对它最多做防止测试产生错误结论所必需的局部修正。共享 contract cases 用来校验调用方可观察语义，不代表两种实现必须能力对等、代码对称或拥有相同故障模型。

```python
class EventLog(Protocol):
    def append(
        self,
        event: AgentEvent,
        *,
        expected_last_sequence: int | None = None,
    ) -> AgentEvent: ...

    def extend(
        self,
        events: Iterable[AgentEvent],
        *,
        expected_last_sequence: int | None = None,
    ) -> list[AgentEvent]: ...
```

这里分成两层验收：

- **production contract**：由 PostgreSQL EventLog 完整承担，包括事务、session-scoped CAS、跨进程 serialization、rollback 和 restart 后的 durable truth；
- **pytest fake fidelity**：InMemory 只复现会直接影响 reducer / writer 单测结论的确定性可观察行为，不模拟数据库与恢复系统。

对 live `append/extend` 冻结以下共享 invariant：

1. 先把 iterable完整materialize并验证，再开始写入；
2. live input event必须`sequence is None`，canonical sequence只能由EventLog分配；预编号event属于未来独立offline import/repair authority；
3. event id在batch内唯一，且不能与该session既有event id冲突；pytest fake至少复现该可观察contract；
4. 在同一个session-scoped原子临界区读取`actual_last_sequence`；
5. `expected_last_sequence`非空且不相等时，零写入并抛`EventLogWriteConflict`；RuntimeSession再翻译为`EventWriteConflict`；
6. 一个batch获得严格连续的`actual_last + 1 ... actual_last + N`；
7. 一个batch不能与其他`append/extend`交错；
8. production PostgreSQL路径任意validation/serialization/parent-row/insert/projection-sync失败，整个transaction零写入；pytest fake只需保证batch预验证或内存commit失败时不修改`_events`与`_next_sequence`，不负责模拟事务中途故障；
9. 返回顺序与input顺序一致；空batch是no-op，不分配sequence。

实现要求：

- `PostgresEventLog.extend()` 复用现有 `pg_advisory_xact_lock(runtime_session_id)` 与单数据库事务，在锁内先CAS再逐条insert；任一失败由事务rollback整批；
- PostgreSQL是production EventLog真源，也是并发CAS、事务rollback、跨进程serialization的权威验收实现；Oxigraph不承载Subagent event ledger；
- `InMemoryEventLog` 明确降级为pytest-only fake，后续可改名/迁移为`TestEventLog`并从product wiring删除；它不需要模拟数据库连接、跨进程锁、projection transaction或恢复能力；
- pytest fake仍必须避免对RuntimeSession测试“说谎”：`extend()`不得再循环调用`append()`，而是在同一个`threading.Lock`内完成CAS、预构造全部stored events，最后一次性`_events.extend()`并推进`_next_sequence`；
- `append()` 只作为单元素 `extend()` wrapper，不能有另一套编号逻辑；
- `_BoundedEventLog`、test doubles与所有EventLog adapter同步新签名；read-only adapter仍直接拒绝写入；
- live EventLog拒绝显式sequence，避免caller绕开连续batch契约。

这里要求的是**最小observable contract fidelity**，不是InMemory/PostgreSQL完整能力对等。纯reducer与RuntimeSession快速单测可使用pytest fake；涉及真实事务、跨线程/进程竞争、advisory lock与rollback的验收必须运行PostgreSQL-backed tests，不能用InMemory通过代替。

InMemory投入边界冻结如下：

- 不为它实现数据库transaction、advisory lock、跨进程serialization、crash recovery、projection transaction或production migration；
- 不把它继续抽象成可配置的product backend，也不以“与PostgreSQL功能对等”为验收目标；
- 只保留`RuntimeSession`与pure reducer快速测试真正依赖的可观察行为：单进程thread-safe、batch sequence连续、CAS conflict零写入、预验证失败零写入；
- 如果某项正确性只能由数据库事务、进程竞争或重启恢复证明，该测试必须直接使用PostgreSQL，不得继续扩写InMemory来模拟；
- 后续删除InMemory product路径时，这组最小实现可迁到`tests/support/TestEventLog`，不应阻塞本次hard cut。

实施规模上限也冻结如下：本阶段对InMemory的生产代码修改应局限于现有adapter内的单锁、整批预验证、连续sequence与CAS零写入；不为它新增数据库式UOW、恢复协调器、projection同步器或专属migration。若满足最小fake contract需要继续扩张其职责，应停止扩写，并把对应测试直接迁到PostgreSQL fixture。

因此PR3对InMemory的工作是一次小型test-double修正，不是一个子系统重写，也不是后续架构演进的阻塞项；主要工程投入与正确性证明都放在PostgreSQL EventLog和`SessionWriteCoordinator`。

如果现有`InMemoryEventLog`已经满足上述最小observable contract，PR3只需要补齐共享contract regression tests，不要求为了“实现形态对称”继续修改它。只有测试能够证明fake会让`RuntimeSession`/reducer得到错误结论时，才允许做局部修正；任何超出单进程test-double语义的需求都直接落到PostgreSQL fixture验证。

换言之，PR3 的默认动作顺序是：先用共享 contract tests 检查现有 fake；测试已通过则不改 InMemory 实现；测试失败才修正最小差异。不得先按 PostgreSQL 的内部结构反向设计 InMemory，也不得为了提高 fake fidelity 延后 production PostgreSQL writer/reducer seam。

RuntimeSession内新增一个session-owned、thread-safe `SessionWriteCoordinator`。async `write_events()` 与 `write_events_from_thread()` 必须进入同一个serialization boundary；单独的`asyncio.Lock`不满足要求。coordinator串行化：

```text
conditional EventLog commit
-> reducer catch-up/apply
-> ordered publisher enqueue
```

observer实际await delivery发生在coordinator临界区之外，避免慢observer阻塞后续commit；但本批所有events必须在释放临界区前按sequence enqueue或明确标记publisher unavailable。ordered publisher可buffer后到达的sequence，不能因async caller调度顺序制造永久gap。

PR3 V1 实现形态冻结为：coordinator内部使用一个session-owned `threading.RLock`，暴露无`await`的`commit_reduce_enqueue(...)`临界段；async与thread入口都调用它。`RuntimeEventPublisher`增加non-awaiting、可从loop/thread调用的`enqueue_committed_batch(...)`，async入口在锁外await返回的delivery receipts，thread入口只报告`enqueued/unavailable`。锁绝不跨observer await。V1可能仍承受sync DB I/O阻塞event loop的既有性能债务；后续async writer可以替换内部执行模型，但不能改变同一coordinator/CAS语义。

### 10.4 精确顺序

```text
1. inject/validate RuntimeSession default metadata; materialize input batch
2. enter the shared SessionWriteCoordinator serialization boundary
3. EventLog.extend(events, expected_last_sequence=...) does compare-and-append
   - conflict: zero current writes; catch reducers/publisher up to actual_last_sequence; raise EventWriteConflict
4. obtain current stored batch with canonical first_sequence/last_sequence
5. snapshot each reducer's own through_sequence and publisher's own next_sequence_to_publish
6. read the union of missing pre-current ranges from EventLog once when possible
7. for each reducer independently:
   - apply only its complete missing interval [through_sequence + 1, first_sequence - 1]
   - after catch-up succeeds, apply the current committed batch
   - never apply current batch before that reducer's gap is complete
8. if any reducer catch-up/apply failed:
     - commit_status remains committed
     - set reconciliation_required
     - block further mutating commands
     - rebuild from EventLog at safe point
9. independently catch publisher up from its own high-water
10. enqueue publisher catch-up events first, then every current committed event, all in sequence order
11. release SessionWriteCoordinator; async path awaits delivery receipts outside the boundary
12. collect publication errors; do not relabel commit as failed; return EventWriteResult
```

`write_events()` 在第一个 publication error 后仍必须尝试 publish batch 中后续 events；否则会制造 publisher sequence gap。

reducer high-water 与 publisher high-water 互相独立。共享读取结果只是I/O优化，不能用publisher进度替代reducer catch-up，也不能因某个reducer已对齐就跳过publisher缺口。假设graph reducer在10、EventLog已有11、compatibility current batch commit为12，顺序必须是apply 11再apply 12；先apply 12属于实现错误。

若任何reducer所需区间无法从EventLog完整读取，该reducer不apply current batch，session进入reconciliation required；其他可完整catch-up的reducer可以继续幂等apply。publisher缺失区间无法完整读取时不得无限await，返回`publication_status="unavailable"`与stable gap diagnostic，并保留已commit/reduced truth。

async `write_event(s)` await 完整 live publication，返回 `publication_status="completed"`。`write_events_from_thread()` 不得阻塞当前 event-loop thread等待observer：成功投递mailbox时返回 `enqueued`，loop不可用时返回 `unavailable`；这两种状态的 `publication_errors` 只包含调用时已知错误，不伪称observer已成功执行。

### 10.5 Compatibility wrapper

迁移期间：

```python
async def emit(self, event):
    result = await self.write_event(event)
    if result.publication_errors:
        raise EventPublicationAfterCommitError(result)
    return result.committed_events[0]
```

这样旧 caller 行为可暂时保留，但异常类型明确包含“已 commit”。SubagentRuntime 使用 `write_*`，observer failure 不会导致它重复创建同一 task/run。

`emit_many()` 也必须基于 `write_events()`；即使 compatibility wrapper最终抛错，整批 reducer 已 apply、整批 observer 已依序尝试。

### 10.6 Committed reducer 注册与 catch-up

注册流程：

1. 从完整 parent EventLog fold graph state；
2. 记录 `through_sequence`；
3. 调用 `register_committed_reducer`；
4. RuntimeSession 在锁内检查 `event_log.next_sequence() - 1`；
5. 若高于 state high-water，读取连续缺失区间并 apply；
6. high-water 对齐后激活 reducer。

同一 session/reducer id重复注册 fail closed。不能让两个 `SubagentRuntime` 各自注册 live store 并竞争。

registration object必须在初始catch-up之前进入session registry。初始catch-up失败时保留该registration，标记registration与session均`reconciliation_required`，普通mutation fail closed，但`reconcile_committed_reducer(reducer_id)`仍能找到它并执行full rebuild。不得出现“全局flag已置位、失败reducer却不在registry、因此永远无法reconcile”的状态。

full rebuild的成功条件不是callback正常return，而是重建后的reducer state明确`consistent=True`并达到完整log high-water。`SubagentGraphStateStore.rebuild()`若得到inconsistent state必须保存该state供read-only inspect、保留reconciliation flag并抛`SubagentReducerApplyError`；RuntimeSession只有在callback成功且一致后才清除registration/global flag。

### 10.7 Publication recovery guarantee

冻结：

- 同进程 gap：从 EventLog catch up缺失连续区间；
- restart：graph reducer从 EventLog重建，不重放所有历史 UI/CLI observers；
- live UI/CLI observer：best-effort、当前进程有序；
- 可靠副作用：未来使用 durable outbox/per-subscriber offset；
- 不承诺 exactly-once；subscriber必须按 event id幂等；
- `EventWriteResult` 分开表达 durable commit 与 publication outcome。

本阶段不要求完成全 async PostgreSQL writer，但新 seam 的 API 必须 async，后续可在内部将 sync EventLog I/O 移到 thread/pool，而不再次改变 Subagent command contract。

### 10.8 Reducer failure 不是 observer failure

- observer failure：graph 已正确 apply，command可以按 committed truth返回，并在 diagnostics/report暴露 publication error；
- reducer failure：graph truth尚未在 live store安全解释，session进入 `reconciliation_required`；Subagent mutation command fail closed；
- rebuild成功且重建state明确consistent：清除 flag；
- rebuild仍 inconsistent：保留 read-only inspect/list，禁止 spawn/wait consumption/delivery mutation。

## 11. Command planner 与 runtime 流程

### 11.1 `SubagentRuntime` 的新职责

`SubagentRuntime` 保留：

- command 参数验证；
- profile/budget resolution；
- artifact write/read orchestration；
- capacity reservation；
- event command planning；
- 调用 `RuntimeSession.write_events()`；
- 从 `SubagentGraphStateStore` 读取 commit 后事实；
- 驱动 `ChildExecutionRegistry`；
- 调用 hydrator；
- child completion/failure repair。

它不再保存 `_tasks/_runs/_results/_submitted_results/_consumed*/_delivered*`。

### 11.2 `SubagentCommandPlanner`

建议把无需 I/O 的 event 构造/transition preflight集中到 `commands.py`：

```python
class SubagentCommandPlanner:
    def plan_task_start(..., state: SubagentGraphState) -> PlannedSubagentWrite: ...
    def plan_completion(..., state: SubagentGraphState) -> PlannedSubagentWrite: ...
    def plan_failure_cascade(..., state: SubagentGraphState) -> PlannedSubagentWrite: ...
    def plan_cancellation_cascade(..., state: SubagentGraphState) -> PlannedSubagentWrite: ...
    def plan_consumption(..., state: SubagentGraphState) -> PlannedSubagentWrite: ...


@dataclass(frozen=True, slots=True)
class PlannedSubagentWrite:
    command_id: str
    expected_through_sequence: int
    events: tuple[AgentEvent, ...]  # ids已分配，canonical sequence尚未分配
    affected_task_ids: tuple[str, ...]
    affected_run_ids: tuple[str, ...]
    required_reservations: tuple[PlannedChildReservation, ...]
    diagnostics: tuple[SubagentCommandDiagnostic, ...]
```

Planner：

- 只读取 immutable graph state；
- 生成已分配 stable id 的 typed events；
- 记录 planning 时的 `expected_through_sequence`；
- 使用 `validate_planned_transitions()` 在不生成 canonical fact provenance 的前提下模拟 entity transition；
- planned transition invalid时不得 commit；
- 不读 archive、不创建 child session、不 publish。

Planner不是宽松提示器：它必须镜像reducer所有会把ledger判为inconsistent的负向entity约束，至少包括task batch/create attribution、result task/run attribution、consumption status/terminal/result attribution、explicit result identity/body不可替换、edge owner/session/payload不冲突、wait/consumption/delivery result属于目标run且artifact一致、message/suspend/terminal/delivery parent/child runtime attribution一致、reported child run id不可替换，以及running task只能在owning run同类terminal fact之后terminalize。若planner漏掉这些约束，非法batch会先commit再被committed reducer拒绝，污染durable ledger并把整session送入reconciliation。

为避免手写planner与reducer继续漂移，`validate_planned_transitions()`分两层：先维护轻量working maps以给出command-specific错误；再把每个planned event复制为仅供验证的临时event，按当前`through_sequence + 1`连续赋予synthetic sequence，喂给同一个pure `apply_subagent_event()`。任何一步令throwaway state从consistent变为inconsistent，planner必须在durable append前拒绝。这个差分guard不持久化event、不publish、不产生canonical provenance；唯一正式`SubagentGraphState`仍只来自stored events。每个reducer negative regression都必须有planner twin，或被共享差分测试覆盖。

commit调用必须是：

```python
await runtime_session.write_events(
    planned.events,
    expected_last_sequence=planned.expected_through_sequence,
)
```

CAS conflict发生在insert前；coordinator先把reducers catch up到actual last sequence，caller再从最新state重新plan/validate。async与thread command不能绕开同一个SessionWriteCoordinator。

“重新plan”不等于无条件retry同一events：若最新state显示目标已terminal/已consumed/已started，command按其幂等product语义返回现状或结构化conflict，不得再次生成矛盾terminal/start facts。只有新state仍满足原command前置条件时才构造新的planned write。

async/sync cancel必须复用同一个 planner，不能再维护两套 task terminalization逻辑。

### 11.3 Primitive `spawn_agent`

精确流程：

```text
1. gate 已确认 parent run snapshot 是 bypass
2. validate role/context/profile; resolve immutable budget/profile/context snapshots
3. enforce caps against graph state + execution reservations
4. reserve one child slot
5. write full task artifact
6. allocate subagent_run_id / child_runtime_session_id / spawn edge id
7. construct RunStarted(budget snapshot included) + MessageSent(spawn_task)
8. `validate_planned_transitions()`
9. write_events(batch, expected_last_sequence=planned.expected_through_sequence)
10. require_reduced(subagent_graph_reducer_id); observer errors only become diagnostic
11. hydrate task artifact as complete execution input
12. create child RuntimeSession; register handle
13. create child coroutine; attach handle
14. return run fact/projection to tool
```

失败边界：

- artifact write失败：无 graph event，释放 reservation；
- event commit失败：释放 reservation；artifact可能 orphan，由 GC处理；
- event commit后 hydration/session/coroutine start失败：先commit `RunFailedEvent(reason_code="subagent_child_start_failed")`，若 task-backed则在同一repair batch terminalize task/cascade；然后request cancel并bounded drain已attach handles；只释放未attach slots，closing attached slots保留到coroutine退出；
- publication failure：不重复 spawn；tool可以返回 started，并附 bounded inspect diagnostic。

### 11.4 `create_agent_tasks` batch

保留现有 all-or-nothing product语义，但 graph/event边界改为：

#### Preflight（不写 graph event）

1. parse task keys/dependencies；
2. validate unique keys、tagged task refs、cycle/self dependency；
3. resolve profiles/context/budgets；
4. validate all artifact inputs；
5. evaluate graph caps；
6. reserve所有立即 runnable child slots；
7. 生成 `batch_id`（validation通过后、reservation前可生成；preflight failure可在 tool result作为 non-durable correlation id返回）；
8. 为每个 objective写 artifact；
9. 计算 initial status；
10. 构造整批 typed events，并做 planned transition validation。

#### Commit

一次 `write_events(batch, expected_last_sequence=planned.expected_through_sequence)`，包含全部 materialized facts：

- 每个 task 的 `TaskCreated`；
- waiting/failed dependency 的 `TaskBlocked`；
- runnable task 的 `TaskScheduled + RunStarted + MessageSent + TaskStarted`；
- task-backed `RunStarted` 的 `batch_id/create_tool_call_id` 与 task一致；
- transitive blocked events 使用实际 upstream terminal event id。

#### Start

commit 后只为 reducer state 中 running 的 run创建 child session/coroutine。

#### Post-commit partial start failure repair

若任一 child start失败：

- 不删除已经 durable 的 batch events；
- 对整批 materialized tasks做结构化 repair；
- active started runs写 RunCancelled/Failed；
- 未启动 task写 TaskCancelled/Failed；
- 所有 repair events使用第 4.8 节的 typed fields引用同一 `batch_id/create_tool_call_id/repair_id`；
- tool返回 structured batch failure，不返回可继续等待的 task ids；
- 不留下部分 running/scheduled task。

顺序必须是`commit terminal repair batch -> request cancel all affected handles -> bounded drain -> physical release`。不得在repair commit前await process cleanup：child吞掉cancellation时，物理drain可以超时，但durable batch仍必须已经全部terminalized。drain timeout保留closing handles与capacity，并返回structured post-commit failure；不能让原batch永久停在running。

### 11.5 Dependency completion scheduling

`RunCompleted + TaskCompleted` commit 后：

1. committed reducer state 已是 upstream completed；
2. planner扫描直接 downstream waiting tasks；
3. 对每个 dependencies全部 completed 的 task，预留 capacity并 hydrate objective；
4. 写 `TaskScheduled + RunStarted + MessageSent + TaskStarted` batch；
5. commit后启动 child；
6. initiator=`dependency_satisfied`，不伪造 tool_call_id。

V1 不做 queue/waiting_capacity。若 runnable dependent无法立即满足 capacity/profile/security preflight：

- 不应默默保持 waiting_dependency；
- 写 terminal `TaskFailedEvent(reason_code="subagent_dependency_start_unavailable")`，随后 block其 downstream；

本文冻结上述 terminal failure语义；V1不新增 capacity queue/status，保持scheduler没有隐藏等待池。

### 11.6 Failure/cancellation cascade

`plan_failure_cascade` / `plan_cancellation_cascade` 使用 BFS/拓扑队列生成一个 event batch：

```text
target RunFailed/RunCancelled
optional owning TaskFailed/TaskCancelled
for each transitive downstream:
    TaskBlocked(status=blocked_dependency_failed,
                blocked_by_task_ids=direct failed deps,
                dependency_terminal_event_ids=actual event ids,
                dependency_status_snapshot=planned + committed statuses)
```

必须先在 planner 的 working state 中逐个 apply planned events，确保同 batch C依赖B、B因A失败被 block时：

- C snapshot能看到 B=`blocked_dependency_failed`；
- C terminal ref指向 B真实 Blocked event id；
- generation可稳定计算。

async `cancel()`、sync safety narrowing、host shutdown、post-commit batch repair共用该 planner，只改变 reason/cancelled_by。

### 11.7 Explicit result safe point

child 调用 `report_agent_result`：

1. validate child attribution；
2. 写 result artifact；
3. commit `SubagentResultSubmittedEvent`；
4. tool result返回 child model；
5. AgentRuntime after-tool-results safe point检查 reducer state中的 submitted result；
6. 不再发起 child follow-up model call；
7. 获取 child-native run id（hydrator或本次 child state）；
8. commit `RunCompleted + optional TaskCompleted` 原子 batch，二者引用同 result id；
9. completion event必须后于 Submitted event；
10. 后续 final text只能 diagnostic，不覆盖 explicit result。

`submitted_result()` 改为从 graph state查询 `result.status="submitted"`，删除 `_submitted_results`。

### 11.8 Inferred result completion

child正常 final text完成且没有 explicit submission：

- 将 final output写 artifact；
- 生成 `result_source="inferred"` 的 result id；
- commit `RunCompleted + optional TaskCompleted`；
- reducer创建 completed inferred result fact；
- result summary与preview受 run budget snapshot限制。

### 11.9 `wait_agent` 与 `wait_agent_tasks`

等待条件只读取 graph state：

- run settled：completed/failed/cancelled；
- task settled：completed/failed/cancelled/blocked_dependency_failed；
- `waiting_dependency/running/created` 不是 settled；
- timeout不取消未完成 task/run。

完成结果通过 hydrator读取 bounded artifact。消费写入：

- `wait_agent(run)`：completed且有result时写一个 `SubagentEdgeRecordedEvent(edge_kind="wait")`；
- `wait_agent_tasks(task)`：一个 `SubagentResultConsumedEvent(kind="wait_task")`；
- `result_id is None` 时 `terminal_event_id` required；completed必须有 result id；
- 重复 wait默认不重新返回 consumed result，除非显式 `include_consumed=true`。

primitive run failed/cancelled且无result时，`wait_agent`返回结构化terminal outcome（status/reason/terminal event id），不再错误报告`not_ready`，也不伪造result consumption edge；重复查询允许返回同一terminal状态。task-level wait则用nullable result id + required terminal event id记录settled outcome consumption。

commit consumption后从新 graph state确认 consumed，不手工更新 set。

### 11.10 Background delivery

`pending_results_for_delivery()` 从 graph state筛选：

- result completed；
- 未 consumed；
- 未 delivered；
- 按 completion sequence稳定排序；
- 数量/summary chars受各 run budget snapshot与parent compile policy共同限制。

`SubagentResultDeliveredEvent` 只在 result实际进入一次已发起的 parent model-call payload后写：

1. compiled context中有 `subagent:results` internal section；
2. `ModelCallStartEvent.context_id/model_call_index` 与 compiled context完全匹配；
3. provider call已实际开始；
4. commit delivery event；
5. compile成功但没有 matching ModelCallStart，不写 delivered。

delivery事件本身经 reducer写 delivery fact。它不是 user message，也不是凭空出现的 provider-native tool result。

### 11.11 Child completion 与 native run id

删除 [`AgentRuntime._run_child_agent()`](src/pulsara_agent/runtime/agent.py) 对 `set_child_run_id()` 的调用。

改为：

- child `run_task()` 返回的 `result.state.run_id` 可以作为当前 completion command的观察输入；
- completion event写入 `child_run_id`；
- reducer只记录 `reported_child_run_id`；
- hydrator再与 child EventLog `RunStartEvent.run_id` 校验；
- running期间需要展示 child id时，由 hydrator读取，不写 live graph cache。

### 11.12 Bypass revoke 与安全收窄

当前 run-bound permission contract下，session default切换不改变 active run snapshot。Subagent child继承 parent run snapshot，因此普通 run之间 mode switch不取消 child。

真正的安全收窄（MCP binding revoke、profile binding失效、host shutdown等）仍调用统一 cancellation planner：

- run -> cancelled；
- task-backed task -> cancelled；
- downstream -> blocked_dependency_failed；
- reason code稳定；
- execution registry取消实际 coroutine；
- graph repair事实与实际 handle cancellation分离。

## 12. Projection、list 与 inspector

### 12.1 删除 projection event switch

`project_subagent_graph()` 改为：

```python
def project_subagent_graph(
    state: SubagentGraphState,
    *,
    parent_run_id: str | None = None,
    max_items: int | None = None,
) -> SubagentGraphProjection: ...
```

它不再接收 raw events，不再自行解释状态迁移。

需要从 EventLog投影时：

```python
state = fold_subagent_graph(full_parent_event_log.iter())
projection = project_subagent_graph(state, parent_run_id=...)
```

必须先 fold完整 parent stream再按 parent run过滤。不能先筛选一段 events后再 reducer，否则 task/result/delivery attribution可能丢失。

### 12.2 `list_agents`

`list_agents` 读取 fact projection，不默认触发 archive/child transcript hydration：

- task/run IDs、status、phase、dependency、result/delivery/consumption；
- child runtime session id；
- bounded preview；
- `child_run_id` 可按轻量 child-log hydration提供，失败则 null + diagnostic；
- 不泄漏 full task prompt、full result、child raw transcript；
- 保持 bypass-only gate，read-only只描述副作用等级，不覆盖 family gate。

工具实现不得再访问 `subagent_runtime._tasks` 等私有 dict；当前 [`tools/builtins/subagent.py`](src/pulsara_agent/tools/builtins/subagent.py) 中的 direct access全部删除。

### 12.3 Inspector normalized shape

```json
{
  "subagent_graph": {
    "through_sequence": 123,
    "consistent": true,
    "tasks": [],
    "runs": [],
    "results": [],
    "edges": [],
    "consumptions": [],
    "deliveries": [],
    "diagnostics": []
  }
}
```

要求：

- task层不能被 inspector wrapper丢掉；
- blocker ids/status/terminal ids/generation完整展示；
- projection注明 fact来源 event id/sequence；
- hydration diagnostics单独放 `hydration_diagnostics`；
- child raw timeline通过 cross-session locator按需查询；
- post-RunEnd graph events不改变 parent run status/completed_at；
- old unsupported event schema直接 contract error，不显示“unknown/default budget”。

### 12.4 Three-way equality

对相同完整 parent event stream：

```text
live SubagentGraphStateStore.state
== fresh fold_subagent_graph(EventLog.iter())
== inspector normalized graph facts (去除展示派生字段后)
```

比较 normalized facts，不比较 hydrated全文、Python session handle或当前 wall clock。

## 13. Recovery、close 与 concurrency

### 13.1 Runtime construction

`SubagentRuntime.__init__` 不做 sync archive hydration。它只：

1. fold parent event log为 graph state；
2. 注册 committed reducer store；
3. 创建空 `ChildExecutionRegistry`；
4. 保存 hydrator/locator collaborators；
5. 不自动重启 child coroutine。

### 13.2 Before-turn dangling repair

AgentRuntime现有 before-turn repair保留，但读取 graph-vs-registry reconciliation结果：

- active run + no handle -> `subagent_dangling_after_restart` failure；
- task同步 failed；
- downstream递归 blocked；
- 使用一个 planned event batch；
- observer failure不导致重复 repair；
- repair后再允许 parent model call。

Host resume的dangling repair还有更早的storage ordering约束：必须先在registry中原子reserve目标runtime identity，确认它不是live/reserved/tombstoned，再在session wiring创建前执行durable repair。pending-close resume不得先append repair events再被reservation拒绝；repair异常必须释放reservation。

### 13.3 Host close

close顺序：

1. freeze new subagent commands；
2. snapshot graph active runs；
3. planner生成 cancellation facts；
4. commit cancellation batch；
5. execution registry cancel/drain handles；
6. close child sessions；
7. release reservations；
8. unregister committed reducer。

如果 event commit失败，仍必须尽力取消process handles，但返回 durable repair needed diagnostic；不能因为 graph write失败而泄漏 coroutine。

第5步必须覆盖所有registry closing handles，不只当前graph仍active的runs：sync safety path可能已将graph terminalize，但其owner-loop coroutine仍在执行`finally`。drain timeout时Host close fail closed并停止后续共享资源释放；handle/session/reservation保持可重试状态，不能把timeout当作drain成功。

该停止条件同时约束`HostSession.aclose()`与外层`HostCore.close_session()/shutdown()`：若session drain失败，HostCore不得pop terminal lease、`registry.finish_close()`、删除transient workspace、关闭workspace supervisor/MCP/retrieval等共享资源。session本身继续保持CLOSING mutation gate并留在索引中；后续close/shutdown调用从同一session/lease重试drain。全局shutdown只在所有sessions都成功drain并移出registry后进入破坏性共享teardown。

每次session close与HostCore shutdown都必须有不可复用的`attempt_id`和共享`Future[None]`。第一个caller是owner，后续caller await同一future并观察完全相同的成功或异常，不能只等无结果`Event`后误判成功。失败attempt的ownership释放、future resolution和允许新retry必须位于同一个registry/lifecycle lock线性化边界；finish/abort只能按attempt identity条件删除自己，旧owner不得在lock外无条件pop新retry的future。waiter cancellation使用shield，不得取消共享attempt。

session attempt还必须持有合并后的close intent，偏序固定为`detach/shutdown < explicit close`。任何在intent seal前加入的explicit caller都把attempt单调升级为`close_conversation=True`，owner按sealed intent决定是否`manifest.mark_closed()`；explicit owner不能被后来的detach/shutdown降级。若explicit caller在线性化seal后才加入，则它等待共享physical-close future后执行幂等manifest close，不能把较强语义静默合并成detach成功。owner顺序与shutdown竞争都必须有回归测试。

物理session teardown成功但`manifest.mark_closed()`失败时，不保留完整HostSession/lease，也不能遗忘explicit-close意图。registry写入bounded `ManifestCloseTombstone(host_session_id, runtime_session_id, conversation_id, retry_attempt)`；后续`close_session(..., close_conversation=True)`创建或加入manifest-only shared Future，成功后条件删除tombstone，失败只清当前retry ownership并保留tombstone。下一次shutdown先重试既有tombstones；仍失败则保持HostCore OPEN并阻止共享资源破坏性teardown。同一host/conversation id和对应runtime session在tombstone清除前不可open/resume，避免ABA复用。

manifest-only retry owner与physical close owner遵守同一异常收口：success调用identity-conditional finish；任意exception或`CancelledError`调用identity-conditional `abort_manifest_close_retry()`，解析共享Future并释放当前retry ownership，但不删除tombstone。后续explicit close必须能成为新owner，不能加入永不完成的旧Future。

resume/open的runtime identity检查必须与reservation同锁原子完成。`HostSessionRegistry.reserve(..., runtime_session_id=...)`同时拒绝live、reserved、tombstoned runtime identity；resume production path必须传入已知runtime_session_id。外层`has_manifest_close_tombstone_for_runtime()`只可用于inspect/友好诊断，不能承担正确性。publish还要再次校验实际session runtime identity，覆盖新session在wiring阶段才分配runtime id的路径。

### 13.4 同步线程路径

`fail_active_children_for_safety_narrowing_now()` 不再维护同步版 mutable update。它：

- 使用与 async path相同 planner；
- 调用 `write_events_from_thread(planned.events, expected_last_sequence=planned.expected_through_sequence)`；
- committed reducers在线程内于 publish enqueue前 apply；
- publisher loop不可用时，durable graph仍正确，live notification可丢；
- registry使用 thread-safe cancel request，实际 await drain在host safe point完成。

### 13.5 并发写保护

同一 RuntimeSession 的所有async/thread写入使用第10.3节的thread-safe `SessionWriteCoordinator`，Subagent command另外使用EventLog CAS防止跨进程或绕过coordinator的stale plan：

- validation读取 state version/through_sequence；
- `expected_last_sequence=planned.expected_through_sequence` 在EventLog session锁/transaction内比较；
- mismatch时零写入，catch up state后重新plan/validate；
- 不允许两个 command同时从同一 waiting task启动两个 run；
- batch reservation和graph commit的关联用 command/batch id审计。

## 14. 稳定 diagnostics/reason codes

至少冻结：

| Code | 层 | 含义 |
|---|---|---|
| `subagent_event_sequence_gap` | reducer | parent event stream不连续 |
| `subagent_event_sequence_reuse` | reducer | 同 sequence出现未知 event id |
| `duplicate_entity_creation` | reducer | task/run/result/edge重复创建 |
| `conflicting_terminal_fact` | reducer | terminal outcome冲突 |
| `orphan_subagent_event_reference` | reducer | event引用未知 entity |
| `subagent_task_run_attribution_mismatch` | reducer | task/run/batch attribution不一致 |
| `subagent_task_terminal_run_mismatch` | reducer | running task的owning run尚未以同类terminal outcome结束 |
| `subagent_edge_identity_conflict` | reducer | 已有edge id被另一个run/session/kind复用 |
| `subagent_edge_payload_conflict` | reducer | 同一edge identity出现不同payload artifact |
| `subagent_explicit_result_completion_mismatch` | reducer | completion试图替换explicit result id/body/artifact |
| `subagent_result_cross_event_mismatch` | reducer | delivery summary或task completion result_source与canonical result fact不一致 |
| `subagent_budget_snapshot_invalid` | event/contract | budget snapshot非法 |
| `event_write_conflict` | event writer | expected last sequence与ledger实际值不一致；当前batch零写入 |
| `event_log_atomic_batch_failed` | event log | batch validation/commit失败且已整批rollback |
| `committed_reducer_catch_up_failed` | writer/store | reducer缺失区间无法完整读取/apply |
| `subagent_graph_reconciliation_required` | writer/store | commit后 reducer未成功 apply |
| `subagent_publication_after_commit_failed` | writer | event已 commit，observer失败 |
| `subagent_objective_artifact_missing` | hydrator | task完整正文不可读 |
| `subagent_result_artifact_missing` | hydrator | result artifact不可读 |
| `child_native_run_missing` | hydrator | child log无匹配 RunStart |
| `multiple_child_native_runs` | hydrator | V1 child session出现多个 native run |
| `child_run_attribution_mismatch` | hydrator | parent reported id与child log不一致 |
| `subagent_child_start_failed` | command | graph commit后child启动失败 |
| `subagent_dangling_after_restart` | reconciliation | graph active但无execution handle |
| `subagent_dependency_start_unavailable` | scheduler | 依赖满足但V1无法立即启动 |
| `subagent_phase_after_terminal` | reducer | late phase report被忽略 |

diagnostic不得包含 secret、完整 task/result、SQL/路径等底层异常文本。保存 stable code、error_type与redacted bounded message。

## 15. 代码落脚点清单

### 15.1 Event schema

- [`src/pulsara_agent/event/events.py`](src/pulsara_agent/event/events.py)
  - `SubagentRunStartedEvent` required snapshots；
  - 必要的 field validators；
  - AgentEvent union保持完整；
  - serialization mapping/contract bump。
- 新增 `src/pulsara_agent/event/subagent_dto.py`（推荐）
  - event-visible snapshot models；
  - 不 import runtime。

### 15.2 Runtime subagent

- [`runtime/subagent/types.py`](src/pulsara_agent/runtime/subagent/types.py)
  - Stage A 新增 public hydrated view DTO，并明确旧 `SubagentTask/SubagentRun` 只是临时 legacy command view；
  - Stage B完成时删除旧 legacy DTO；
  - 保留/新增 public hydrated view DTO；
  - `SubagentBudget` 增加 `from_event_snapshot()`，生产 run总能 round-trip。
- 新增 `facts.py/reducer.py/hydration.py/execution.py/commands.py`。
- [`runtime/subagent/projection.py`](src/pulsara_agent/runtime/subagent/projection.py)
  - 删除 event switch；只适配 GraphState。
- [`runtime/subagent/runtime.py`](src/pulsara_agent/runtime/subagent/runtime.py)
  - Stage A 删除 `_bootstrap_*`；
  - Stage B 删除 graph mutable dict与 direct `replace()`；
  - command只读 store、写 events、驱动 registry。

### 15.3 Runtime session/writer

- [`event_log/protocol.py`](src/pulsara_agent/event_log/protocol.py)
  - conditional `expected_last_sequence` contract；
  - live pre-sequenced input rejection；
  - atomic/contiguous batch invariant。
- [`event_log/in_memory.py`](src/pulsara_agent/event_log/in_memory.py)
  - `extend()` 改为单锁整批prevalidate/allocate/commit，不再逐条`append()`。
- [`event_log/postgres.py`](src/pulsara_agent/event_log/postgres.py)
  - 在现有session advisory transaction lock内CAS；
  - 保持整批rollback与连续sequence。
- [`runtime/session.py`](src/pulsara_agent/runtime/session.py)
  - shared `SessionWriteCoordinator`；
  - `EventWriteResult` path；
  - committed reducer registry；
  - `write_event(s)` / thread path；
  - compatibility emit wrapper。
- [`runtime/publisher.py`](src/pulsara_agent/runtime/publisher.py)
  - 支持完整 batch publication attempt；
  - 明确 catch-up入口与best-effort observer语义；
  - 不让 committed reducer依赖 publisher success。

### 15.4 Agent/tool/inspector

- [`runtime/agent.py`](src/pulsara_agent/runtime/agent.py)
  - 删除 `set_child_run_id`调用；
  - child runner使用 hydrated execution input；
  - explicit result safe point读取 reducer state；
  - delivery join保持严格。
- [`tools/builtins/subagent.py`](src/pulsara_agent/tools/builtins/subagent.py)
  - 删除 private dict access；
  - 使用 command result/projection；
  - 保持 structured failures。
- [`inspector/service.py`](src/pulsara_agent/inspector/service.py)
  - 统一 GraphState projection；
  - task/result/diagnostic不丢失。

## 16. PR 实施顺序

每个 PR 都必须保持全量非 real tests绿色；不得把“最终会删的第二状态”继续扩展。

### PR0：Event contract 与 snapshot DTO hard cut

范围：

- 新增三个 event snapshot DTO；
- `SubagentRunStartedEvent.budget_snapshot` required；
- context/capability从自由 dict改 typed snapshot；
- 删除重复 `spawning_tool_call_id`，统一 `spawn_initiator_kind/id`；
- validators与serialization；
- 所有 writer、fixture、manual event constructor同步；
- event contract version bump；
- 明确旧 event log/reset策略。

验收：

- budget/context/profile完整 round-trip；
- missing/extra/NaN/inf/custom permission profile被拒绝；
- runtime配置改动不影响已序列化 snapshot；
- `uv run ruff check` 与 event contract tests通过。

### PR1：Facts + pure reducer

范围：

- `facts.py/reducer.py`；
- 覆盖全部现有 Subagent events；
- transition/invariant/diagnostic；
- fold builder与immutable state；
- actual same-batch terminal refs。

验收：

- 每个 event transition unit test；
- prefix/idempotency/conflict/gap tests；
- waiting/blocked/transitive cascade event stream正确；
- reducer无 archive/runtime config I/O。

### PR2：Bootstrap、projection 与 hydrator收口（Stage A 完成）

范围：

- SubagentRuntime初始化通过 reducer fold恢复 facts；在 committed seam落地前，`graph()/list/inspect` 每次从当前完整 EventLog fresh fold，不能假设一个未注册的 live store会自动更新；
- 删除 `_bootstrap_from_parent_event_log/_bootstrap_*`；
- projector改为 GraphState adapter；
- inspector/list切同一 projection；
- async hydrator读取 archive/child log；
- 新 fact DTO从第一天起不含完整正文/child live id；legacy command view暂留到Stage B。

验收：

- Stage A three-way equality：fresh reducer fold、fresh SubagentRuntime initial facts、inspector normalized facts相等；live incremental store equality在PR3后加入；
- restart waiting/blocked facts不漂移；
- config变化后budget不漂移；
- missing artifact/child log hydration diagnostics；
- list不读取/泄漏full transcript。

Stage A结束时，command path可以暂时继续维护一个明确命名的 `LegacySubagentCommandViewCache` 用于本进程调度，但它不能成为 bootstrap、list或inspector真源。该 cache 从 reducer facts经 async hydrator在首次 command safe point构建，不再拥有独立 event switch；facts查询在 Stage A 使用 fresh fold，PR3 注册 `SubagentGraphStateStore` 后才切换为 incremental committed state。所有新测试必须同时断言 event reducer。Stage B开始前不得新增新 command行为，PR5-PR7逐段删除该 cache的mutation，最终删除整个类型与容器。

### PR3：Minimal committed-reducer seam

范围：

- EventLog `append/extend(expected_last_sequence=...)` conditional atomic contract；
- pytest-only InMemory fake只做最小单锁batch/CAS fidelity修正；PostgreSQL实现并权威验收production advisory-lock transaction CAS；
- InMemory仅修正为可信test double，不补production transaction、跨进程、恢复或projection能力；
- 若现有InMemory实现已经通过最小共享contract tests，则不产生纯粹为了与PostgreSQL“代码对称”的改动；
- `SessionWriteCoordinator` 统一async/thread serialization；
- `EventWriteResult`；
- reducer registry/store；
- commit -> reducer catch-up -> current apply -> publisher catch-up/enqueue；
- observer error分离；
- emit compatibility wrapper；
- sync thread path；
- register catch-up/reconciliation。

验收：

- PostgreSQL batch在真实并发下获得连续sequence且不与其他batch交错；
- pytest fake通过最小contract smoke：CAS、连续sequence、单锁整批、零partial write；
- conditional mismatch在任何insert前抛`EventWriteConflict`，无partial write；
- PostgreSQL transaction在validation/insert/projection-sync失败后整批rollback；
- 不以InMemory测试替代上述PostgreSQL transaction/concurrency验收，也不要求两者具备完整能力对等；
- InMemory改动未超出既有adapter内的test-double修正；若需要数据库式能力，测试改用PostgreSQL fixture；
- reducer high-water落后时先apply missing range，再apply current batch；
- reducer与publisher使用各自high-water，不能互相代替；
- single observer failure：event durable、reducer applied、result标publication error；
- batch first observer failure：全 batch reducer applied、所有 sequences可继续；
- reducer failure：commit truth保留、session reconciliation required；
- restart从log恢复，不依赖observer replay；
- sequence gap不会无限等待。

### PR4：ChildExecutionRegistry

范围：

- registry/handle/reservation；
- 从 SubagentRuntime剥离 `_child_sessions/_child_tasks`；
- reconcile矩阵；
- close/drain/cancel primitives；
- 此 PR 不改变 graph facts。

验收：

- handle不进入events/projection；
- terminal graph可清理active handle；
- active graph缺handle可识别；
- reservation所有失败路径释放。

### PR5：Create/spawn/start command hard cut

范围：

- primitive spawn；
- create task；
- materialize batch；
- task start；
- dependency satisfied start；
- 全部使用 planner + write_events + reducer state；
- 删除对应 direct `_tasks/_runs` mutation。

验收：

- commit后 observer失败不重复spawn；
- batch all-or-nothing与post-commit repair；
- task/start attribution与budget snapshot；
- same-batch transitive blocker facts完整；
- 并发两个start只有一个成功。

### PR6：Result、wait、consume、deliver hard cut

范围：

- phase/result submitted；
- explicit/inferred completion；
- run/task wait；
- consumption；
- background delivery；
- child native id hydration；
- 删除 `_results/_submitted_results/_consumed*/_delivered*`。

验收：

- Submitted先于Completed；
- explicit优先于final text；
- child report graph events始终使用原spawn parent context，PostgreSQL路径不得发生cross-session run identity reuse；
- wait first/all/timeout/consumed；
- delivery严格join ModelCallStart；
- restart后pending delivery与consumption一致。

### PR7：Failure/cancel/cascade/recovery hard cut（Stage B 完成）

范围：

- fail/cancel/task cancel；
- transitive dependency block；
- async/sync safety narrowing共用planner；
- dangling repair；
- host shutdown；
- 删除剩余 graph direct mutation与旧 DTO；
-删除 `set_child_run_id()`。

验收：

- A->B->C failure/cancel transitive block；
- sync/async产生 normalized equal facts；
- restart active child fail-closed repair；
- no dangling handles；
- `rg`确认无 `_tasks[...] =`、`_runs[...] =`、`replace(task/run, ...)` graph mutation。

### PR8：PostgreSQL、inspect、real LLM与清理

范围：

- PostgreSQL-backed three-way equality；
- cross-session locator；
- inspector normalized facts；
- real LLM spawn/wait/background/task DAG；
- 删除 obsolete helpers/imports/tests；
- 文档与契约同步。

验收：

- pytest fake与PostgreSQL在共享的deterministic EventLog contract cases中输出一致；生产并发/事务正确性只由PostgreSQL验收；
- process restart后graph一致；
- real LLM child execution不回归；
- 全量 test + real LLM subagent dogfood通过。

## 17. 测试矩阵

### 17.1 Event/snapshot tests

- `test_subagent_run_started_requires_budget_snapshot`
- `test_subagent_budget_snapshot_round_trip_is_immutable`
- `test_subagent_budget_snapshot_rejects_non_finite_timeout`
- `test_subagent_capability_snapshot_requires_preset_permission_expansion`
- `test_subagent_context_snapshot_rejects_invalid_fork_contract`
- `test_subagent_event_serialization_preserves_all_snapshots`
- `test_subagent_task_created_requires_objective_artifact`
- `test_subagent_message_sent_requires_message_artifact`
- `test_subagent_completion_requires_run_result_and_artifact`
- `test_subagent_result_consumed_enforces_kind_target_and_terminal_invariants`
- `test_subagent_result_delivered_requires_model_call_join_fields`

### 17.2 Reducer transition tests

- `test_reducer_task_created_waiting_started_completed`
- `test_reducer_task_waiting_then_blocked_dependency_failed`
- `test_reducer_transitive_blocker_snapshot_uses_planned_status`
- `test_reducer_run_started_message_enriches_spawn_edge`
- `test_reducer_explicit_result_submitted_then_completed`
- `test_reducer_completion_cannot_replace_explicit_result_identity_or_body`
- `test_reducer_inferred_completion_creates_result_fact`
- `test_reducer_wait_edge_and_task_consumption_are_distinct`
- `test_wait_agent_failed_run_returns_terminal_outcome_without_fake_consumption`
- `test_reducer_delivery_requires_completed_result`
- `test_reducer_duplicate_event_id_is_idempotent`
- `test_reducer_rejects_conflicting_terminal_facts`
- `test_reducer_rejects_spawn_and_message_edge_identity_collisions`
- `test_reducer_running_task_terminal_requires_matching_terminal_owning_run`
- `test_reducer_rejects_wait_edge_consuming_another_runs_result`
- `test_reducer_rejects_run_terminal_session_attribution_mismatch`
- `test_reducer_rejects_runtime_and_child_run_attribution_drift`
- `test_reducer_rejects_result_summary_and_source_cross_event_drift`
- `test_reducer_recursively_freezes_capability_snapshot_and_convenience_value`
- `test_projection_recursively_thaws_inconsistent_graph_diagnostics_for_json`
- `test_reducer_marks_sequence_gap_inconsistent`
- `test_reducer_task_run_batch_attribution_mismatch`

### 17.3 Prefix/property-style equality

不强制新增 Hypothesis依赖；先使用固定 seed的合法 event-stream generator：

```python
for stream in generated_legal_subagent_streams(seed=...):
    for prefix in all_prefixes(stream):
        assert fold(prefix) == incremental_apply(prefix)
        assert normalize(project(fold(prefix))) == normalize(fold(prefix))
```

覆盖：

- independent batches；
- 3-5层 DAG；
- completed/failed/cancelled upstream；
- wait all/first；
- explicit/inferred result；
- delivery前后；
- post-parent-RunEnd child terminal event。

### 17.4 Restart tests

- `test_restart_preserves_waiting_dependency_fact`
- `test_restart_preserves_blocked_dependency_terminal_refs_and_generation`
- `test_restart_preserves_run_budget_after_default_config_change`
- `test_restart_preserves_consumed_and_delivered_sets_from_facts`
- `test_restart_does_not_require_archive_for_graph_equality`
- `test_restart_active_run_without_handle_repairs_fail_closed`

### 17.5 Hydrator tests

- `test_hydrator_loads_task_objective_from_artifact`
- `test_hydrator_missing_task_artifact_returns_incomplete_diagnostic`
- `test_child_log_hydrates_native_run_id`
- `test_child_log_multiple_native_runs_is_v1_error`
- `test_reported_and_native_child_run_id_must_match`
- `test_list_projection_does_not_hydrate_full_child_transcript`
- `test_wait_result_hydration_is_bounded_and_artifact_backed`

### 17.6 Committed seam tests

pytest fake只承担快速contract smoke：

- `test_in_memory_event_log_extend_allocates_contiguous_atomic_batch`
- `test_in_memory_event_log_conditional_extend_conflict_writes_nothing`
- `test_in_memory_event_log_batch_validation_failure_leaves_no_partial_events`
- `test_event_log_live_append_rejects_presequenced_event`

以下production contract tests必须使用真实PostgreSQL：

- `test_postgres_event_log_extend_allocates_contiguous_atomic_batch`
- `test_postgres_event_log_concurrent_batches_never_interleave`
- `test_postgres_event_log_transaction_failure_leaves_no_partial_events`
- `test_postgres_event_log_conditional_extend_conflict_writes_nothing`

writer/reducer tests可用pytest fake隔离逻辑，但关键case还要有PostgreSQL-backed integration覆盖：

- `test_reducer_catches_missing_interval_before_current_batch`
- `test_reducer_and_publisher_catch_up_from_independent_high_water_marks`
- `test_async_and_thread_writes_share_session_write_coordinator`
- `test_event_write_applies_reducer_before_observer`
- `test_event_write_returns_committed_truth_when_observer_fails`
- `test_event_batch_observer_failure_does_not_skip_later_sequences`
- `test_emit_compat_error_carries_event_write_result`
- `test_committed_reducer_failure_requires_reconciliation`
- `test_initial_reducer_catch_up_failure_remains_registered_for_reconciliation`
- `test_inconsistent_full_rebuild_keeps_runtime_reconciliation_required`
- `test_reducer_registration_catches_up_commit_race`
- `test_thread_write_applies_reducer_when_live_publisher_unavailable`

### 17.7 Execution registry tests

- `test_registry_handle_never_appears_in_graph_projection`
- `test_reservation_released_when_event_commit_fails`
- `test_child_start_failure_emits_terminal_repair`
- `test_terminal_graph_reconciles_and_cancels_live_handle`
- `test_graph_active_registry_missing_reports_dangling`
- `test_host_close_drains_all_handles`
- `test_cancel_waits_for_child_finally_before_session_close`
- `test_sync_cancel_requests_on_owner_loop_and_releases_after_done`
- `test_cancel_timeout_keeps_live_handle_and_session_until_coroutine_exits`
- `test_partial_reservation_release_keeps_attached_closing_slot_occupied`
- `test_closing_child_handle_continues_to_occupy_concurrency_capacity`
- `test_session_drain_failure_preserves_lease_and_allows_close_retry`
- `test_concurrent_session_close_waiters_share_failure_and_retry_attempt`
- `test_close_attempt_monotonically_merges_detach_and_explicit_close_intent`
- `test_shutdown_close_attempt_merges_competing_explicit_close_intent`
- `test_explicit_close_arriving_after_detach_intent_seal_closes_manifest`
- `test_explicit_close_manifest_failure_keeps_tombstone_for_retry`
- `test_late_explicit_manifest_failure_keeps_tombstone_for_retry`
- `test_shutdown_retries_pending_manifest_close_before_shared_teardown`
- `test_cancelled_manifest_retry_owner_releases_retry_ownership`
- `test_runtime_reservation_atomically_rejects_pending_manifest_tombstone`
- `test_tombstoned_resume_rejects_before_dangling_repair`
- `test_host_shutdown_stops_before_shared_teardown_when_session_drain_fails`
- `test_concurrent_shutdown_waits_for_owner_even_when_cleanup_fails`

### 17.8 Command integration tests

保留并改写现有 `tests/test_subagent_runtime.py`，额外增加：

- `test_spawn_observer_failure_does_not_duplicate_child`
- `test_materialized_batch_facts_are_applied_once`
- `test_concurrent_task_start_has_single_winner`（loser收到`EventWriteConflict`后replan为不可启动，ledger中只有一个RunStarted）
- `test_completion_run_and_task_events_are_atomic_batch`
- `test_child_report_events_use_parent_spawn_context_not_child_native_context`
- `test_wait_agent_tasks_timeout_returns_partial_without_cancelling_unsettled`
- `test_wait_agent_tasks_repeated_wait_requires_include_consumed`
- `test_cancel_sync_async_fact_equality`
- `test_transitive_cancel_block_events_use_real_event_ids`
- `test_tool_layer_never_reads_private_graph_dicts`
- `test_command_planner_rejects_explicit_result_replacement_before_commit`
- `test_command_planner_rejects_running_task_terminal_without_terminal_owning_run`
- `test_command_planner_rejects_cross_run_wait_result_before_commit`
- `test_command_planner_rejects_run_terminal_session_mismatch_before_commit`
- `test_command_planner_rejects_task_schedule_creation_attribution_drift`
- `test_command_planner_rejects_result_submitted_for_another_tasks_run`
- `test_command_planner_rejects_consumed_status_drift_before_commit`
- `test_command_planner_rejects_reducer_attribution_drift_before_commit`
- `test_command_planner_reducer_guard_rejects_result_cross_event_drift`
- `test_materialized_batch_start_failure_commits_repair_before_bounded_drain`
- `test_start_task_failure_commits_terminal_facts_before_child_drain`
- `test_wait_agent_serializes_nested_explicit_result_diagnostics`

### 17.9 PostgreSQL tests

- parent graph events与child raw events写不同 runtime sessions；
- `test_postgres_child_report_events_keep_parent_spawn_context`：先在child session持久化native `RunStartEvent`，再确认phase/result graph facts仍写parent session并沿用spawn context；
- fresh process通过 locator读取child run id；
- PostgreSQL `extend` batch在observer failure后graph完整；
- inspector从full parent stream投影tasks/runs/results；
- event sequence/high-water正确；
- 数据库reset后新 schema拒绝旧缺budget event。

### 17.10 Real LLM dogfood

至少保留并扩展：

1. primitive spawn + wait；
2. durable/restart wait；
3. background result delivery；
4. `create_agent_tasks` 两个独立任务并行；
5. A->B dependency，A完成后B启动；
6. A失败，B blocked，main agent亲自使用filesystem/terminal完成任务；
7. child explicit `report_agent_result` 后不再follow-up model call；
8. real PostgreSQL/Oxigraph wiring（若测试本身不需要memory，Oxigraph只做环境完整性，不参与graph truth）。

Real LLM断言结构化 events/projection，不依赖模型措辞。

## 18. Hard-cut rollout

### 18.1 不做 backward compatibility

- 旧 `SubagentRunStartedEvent` 缺 `budget_snapshot`：unsupported；
- 旧自由 dict context/capability payload：unsupported；
- 不提供 runtime fallback或默认预算补齐；
- 开发环境 reset event DB，或运行一次明确 migration；
- 所有 fixture/manual event constructors一次性修完；
- inspector/recovery遇到旧 payload报 contract/schema error。

### 18.2 不做双写期

禁止长期同时：

- reducer apply；
- command direct mutation；
- projection event switch。

PR过渡只允许按前述 Stage A/Stage B边界短期存在；每个 PR必须写清哪一套仍是 command execution adapter、哪一套已经是 durable truth。Stage B完成必须删除direct mutation。

### 18.3 数据与部署

本 hard cut本身不要求新增关系表；events继续存 JSONB payload。若增加数据库 CHECK，只检查：

- event type为 `SUBAGENT_RUN_STARTED` 时 required snapshot keys存在；
- version字段/enum基本合法。

深度 policy一致性由 Pydantic validators/contract tests完成，不在SQL重写preset逻辑。

## 19. 明确非目标

本阶段不做：

- task retry/re-attempt/reset/redefine；
- nested subagent；
- child memory recall/write/governance；
- InMemory EventLog作为product/runtime可选backend；它只保留pytest test-support用途并计划后续重命名/移出production package；
- child pending approval/MCP input-required/plan question路由；这些继续 child内 fail closed；
- Deno/WorkflowScript；
- durable child coroutine恢复；
- task capacity queue；
- full async event writer与所有 subsystem统一迁移；
- exactly-once observer delivery；
- 把 child raw transcript合并进parent stream；
- 让 main agent把 subagent result当最高真值。

Subagent仍是有 provenance 的次级证据生产者。最高可信度来自主 agent亲自通过 filesystem/terminal/artifact/event log验证的事实。

## 20. 完成定义

以下条件全部满足，才算 Subagent graph hard cut完成：

- [x] `SubagentRunStartedEvent` required typed context/capability/budget snapshots；
- [x] graph facts不含完整正文或process handle；
- [x] graph facts中的permission/diagnostics/token metadata等嵌套值已递归冻结，不可绕过event原地修改；
- [x] pure reducer覆盖全部 Subagent events；
- [x] reducer拒绝edge identity collision、explicit result替换和running task脱离owning run终态；
- [x] reducer拒绝跨run消费result、artifact错配以及terminal event的parent/child runtime session归因漂移；
- [x] reducer统一校验message/suspend/terminal/edge/delivery runtime attribution，reported child run identity一旦观察到不可替换；
- [x] delivery summary与task completion result_source只作为canonical result fact的cross-event assertion，不形成第二真源；
- [x] command planner在commit前镜像reducer的负向约束，并通过同一pure reducer的throwaway差分guard兜底，非法plan不会先污染ledger再触发reconciliation；
- [x] bootstrap、projection、scheduler、wait、delivery使用同一 reducer state；
- [x] async hydrator负责archive/child-log，失败不修改facts；
- [x] PostgreSQL EventLog通过conditional、连续sequence、并发不交错、transaction零partial write的production验收；
- [x] pytest-only EventLog fake只实现RuntimeSession快速测试所需的最小CAS/atomic batch contract，不进入production HostCore默认路径；
- [x] async/thread write共享SessionWriteCoordinator，stale plan由EventLog CAS在commit前拒绝；
- [x] reducer先catch up自己的缺失区间，再apply当前batch；publisher使用独立high-water；
- [x] committed reducer在commit后、publish前apply；
- [x] 初始catch-up失败的reducer仍可寻址reconcile；inconsistent full rebuild不会误清除flag；
- [x] observer failure不伪装成commit failure；
- [x] command path无direct graph mutation；
- [x] execution handles只存在于`ChildExecutionRegistry`；
- [x] child cancel在owning loop发起并await coroutine finally后才释放session/slot；sync path仅request、Host safe point负责drain；
- [x] terminal graph fact与closing coroutine分层计数；closing handle在physical exit前持续占用并发slot；
- [x] post-commit child start failure先写完整terminal repair facts，再做bounded drain；超时保留closing handle；
- [x] HostSession drain失败会阻断HostCore的lease/supervisor/workspace破坏性teardown，并保留可重试入口；
- [x] session close/shutdown使用带attempt identity的共享Future，owner/waiter共享最终异常，旧attempt不能删除新retry；
- [x] concurrent close intent按`detach/shutdown < explicit close`单调升级并在seal后执行，不会遗漏manifest closed语义；
- [x] manifest close失败保留bounded finalization tombstone，explicit retry/shutdown可恢复且pending runtime identity不可复用；
- [x] manifest-only retry owner取消/异常会解析共享Future并释放identity-conditional ownership，不会永久wedge；
- [x] resume runtime identity与registry reservation在同一临界区检查，live/reserved/tombstoned runtime均不能穿透；
- [x] immutable fact在projection/inspector/tool JSON边界统一递归thaw，嵌套diagnostic可稳定序列化；
- [x] `set_child_run_id()`已删除；
- [x] sync/async cancel共用command planner；
- [x] live/restart/inspect normalized facts三路相等；
- [x] waiting/blocked/budget/consumption/delivery restart测试齐全；
- [x] PostgreSQL-backed graph/recovery测试通过；
- [x] real LLM subagent dogfood通过；
- [x] `rg`无旧 bootstrap reducer与private graph dict tool access；
- [x] 全量 lint、compile/schema type-contract、unit/integration tests通过。

### 20.1 实施验证记录（2026-07-10）

- `uv run ruff check`：通过；
- `uv run python -m compileall -q src tests`：通过。仓库未配置独立 mypy/pyright gate；typed event/snapshot contract由Pydantic strict validators与contract tests验收；
- `uv run pytest -m 'not real_llm' -q`：`1471 passed, 1 skipped, 67 deselected`；唯一skip是未开启`PULSARA_RUN_REAL_MCP=1`的外部remote MCP dogfood，不属于本实施矩阵；
- `tests/test_subagent_postgres_integration.py tests/test_inspector.py`：`22 passed`，真实PostgreSQL未skip；
- 完整subagent real-LLM矩阵：`7 passed`，覆盖primitive spawn/wait、durable PostgreSQL、background delivery、A→B、独立双child、explicit result safe point、runtime restart后wait、failed/blocked后main agent filesystem+terminal自证，以及DurableGraphFacade/Oxigraph wiring；
- 文档中全部显式`test_*`名称均存在；
- 静态搜索确认无旧bootstrap reducer、legacy command cache、private graph dict mutation、`set_child_run_id()`或旧`SubagentTask/SubagentRun` hydrated state类型。

完成后的稳定架构是：

```text
typed parent graph events
    -> one pure reducer
        -> immutable graph facts
            -> scheduler / commands / list / inspector
            -> async hydrator -> bounded runtime views

commit boundary
    -> conditional atomic EventLog append
    -> reducer catch-up, then apply current graph facts
    -> publisher catch-up, then enqueue current observer events

child execution
    -> separate ephemeral registry
    -> safe-point reconciliation against graph facts
```

这次 hard cut的价值不在于减少几个字典，而在于让 Subagent task DAG、child runtime、wait/delivery 与 inspect第一次共享同一套可重放、可解释、可验证的事实语义。
