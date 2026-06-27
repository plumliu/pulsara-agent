# Failed / Aborted Recovery Contract

_Created: 2026-06-27_

这份文档定义 Pulsara failed / aborted recovery 的长期契约。它不是 implementation plan(实现指导见根目录 `RECOVERY_CONTRACT_DESIGN.zh.md`),而是当前和后续实现必须遵守的硬协议:中断/失败后的恢复语义,必须用**一个结构化恢复词汇**表达,由**两个 producer**(跨 run / in-run)分别产出,绝不在 transcript / runtime prompt 里各自硬编码散落的恢复文案。

核心立场:

- **一个词汇,两个 producer。** `RecoveryProjection` 是唯一恢复词汇;cross-run(从 event log)与 in-run(从 live `LoopState`)是两个独立 producer。统一的是语义与文案真源,不统一的是数据通路。
- **guidance 文案两张表、同义不同口吻。** transcript(事后说明)与 runtime prompt(操作提示)共享 `GuidanceKind`,但各有一张文案表;不强制逐字共享。
- **abort 类型是 typed、可审计的事实。** 用 `AbortKind` 表达,持久化到 `RunEndEvent.abort_kind`;不再走 `scratchpad` 的 stringly-typed 暗线。
- **plan 被中止 ≠ 任务失败。** 在 active plan 中被 stop 的 run,恢复口吻是"规划仍在继续",不能渲染成"任务失败,去恢复"。
- **completion note 不属于恢复契约。** terminal 后台进程完成是生命周期投影,与 failed/aborted 正交,不并入。

相关代码:

- [src/pulsara_agent/runtime/recovery.py](/Users/plumliu/Desktop/python_workspace/pulsara_agent/src/pulsara_agent/runtime/recovery.py)
- [src/pulsara_agent/runtime/tool_taxonomy.py](/Users/plumliu/Desktop/python_workspace/pulsara_agent/src/pulsara_agent/runtime/tool_taxonomy.py)
- [src/pulsara_agent/host/transcript.py](/Users/plumliu/Desktop/python_workspace/pulsara_agent/src/pulsara_agent/host/transcript.py)
- [src/pulsara_agent/runtime/context.py](/Users/plumliu/Desktop/python_workspace/pulsara_agent/src/pulsara_agent/runtime/context.py)
- [src/pulsara_agent/runtime/state.py](/Users/plumliu/Desktop/python_workspace/pulsara_agent/src/pulsara_agent/runtime/state.py)
- [src/pulsara_agent/event/events.py](/Users/plumliu/Desktop/python_workspace/pulsara_agent/src/pulsara_agent/event/events.py)
- [tests/test_recovery.py](/Users/plumliu/Desktop/python_workspace/pulsara_agent/tests/test_recovery.py)

---

## 1. 恢复词汇:RecoveryProjection

恢复语义只有一个结构化载体 `RecoveryProjection`(`recovery.py`):

```python
@dataclass(frozen=True, slots=True)
class RecoveryProjection:
    run_status: Literal["failed", "aborted"] | None   # run-terminal outcome;in-run 恢复时为 None
    abort_kind: AbortKind | None                       # 仅 aborted 有意义
    unfinished_tools: tuple[UnfinishedToolCall, ...]   # classifier 输出(已排除 workflow tool)
    in_plan_workflow: bool                             # 终止/当前是否处于 plan
    guidance_kind: GuidanceKind                        # prompt-facing 文案选择器
```

字段语义(硬约束):

- `run_status` **只表示 run-terminal outcome**。cross-run 取 `RunEndEvent.status`;in-run 恢复(同一 run 内 step 失败、run 未终结)时为 `None`。`run_status is None` **不等于"无需恢复"**——它表示"这是 in-run 恢复,没有 terminal status"。`render_recovery_text` 在 `run_status is None` 时不追加 unfinished 摘要。
- `abort_kind` 见 §3。
- `unfinished_tools` 由 `classify_unfinished_tool_calls` 产出,**必须排除 plan workflow tool**(见 §5)。
- `in_plan_workflow` 见 §2.2。
- `guidance_kind` 见 §4。

## 2. 两个 producer

### 2.1 cross-run:project_recovery_from_events

`project_recovery_from_events(events)` 从 event log 产出,喂 transcript。它**只观察事件流中的最后一个 `RunEndEvent`**:若最后一个 run 是 `failed/aborted`,就为该 run 产出恢复投影;若最后一个 run 已 `finished`,则返回 `None`,**不会回头再为更早的 recoverable run 投影 recovery note**。

它内部维护**两个口径不同、不可互换的事件窗口**:

- `run_events_all` —— 按 `run_id` 取目标终止 run 的**全量**事件(**不按 sequence 截断**),喂 `classify_unfinished_tool_calls`。这保住 all-events 语义:`RunEnd` 之后才到的 late `ToolResultEnd` 仍会把对应 tool 从 unfinished 中消掉(自愈)。
- `events_through_target_run_end` —— 按 `sequence` **截到目标 `RunEnd`(含)**,喂 `reduce_plan_workflow_state` 求 `in_plan_workflow`。plan active 是"截至该 run 结束"的状态,不能掺入 RunEnd 之后的 late 事件。

**实现者不得用同一个事件列表喂两者。** classifier 要 late-result 自愈(全量),plan reducer 要 run-terminal 时点(截断)。

### 2.2 in_plan_workflow 的判定来源

`in_plan_workflow` **只能复用 `reduce_plan_workflow_state` 的语义边界**:

- cross-run:把 `events_through_target_run_end` 喂 plan reducer,取其 `.active`。
- in-run:从 `LoopState.scratchpad["plan_state"].active`(或 `["plan_active"]`)读。

**禁止**用"该 run 是否有未 resolve 的 `PlanQuestionAsked`/`PlanExitRequested`"来推断。反例:在一个已 active 的 plan 里发起的普通 read-only planning turn,中途被 stop,该 run 没有新的 question/request 事件,但 plan 仍 active——扫法会漏判成普通 abort,reducer 法不会。

### 2.3 in-run:project_recovery_from_state

`project_recovery_from_state(state)` 从 live `LoopState` 产出,喂 runtime prompt。`state.recovery_mode` 为 False 时返回 `None`。产出固定 `guidance_kind=IN_RUN_STEP_FAILED`、`run_status=None`、`unfinished_tools=()`。

## 3. AbortKind:typed、可审计

abort 类型用 `AbortKind`(`recovery.py`)表达,不再走 `scratchpad["abort_reason"]` 的 stringly-typed 暗线:

```python
class AbortKind(StrEnum):
    USER_STOP = "user_stop"
```

- `LoopState.abort_kind: AbortKind | None`,在 `stream_abort_run` 设置。
- **持久化到 `RunEndEvent.abort_kind: str | None`**(additive 字段,旧事件 load 不受影响)。cross-run producer 从事件读 abort_kind,而非靠 scratchpad(scratchpad 不进 log)。
- `abort_run` / `stop_current_turn` 链路上的 `reason` 参数类型为 `AbortKind`(不是 str),确保不会有裸字符串流入 `state.abort_kind`。

V1 运行级 abort 只有 `USER_STOP` 一个成员(teardown / watchdog 是 terminal-process 级,不是 run 级,不在此枚举)。typed 化的价值是杀掉暗线 + 让 abort 类型可审计 + 留扩展点。

## 4. GuidanceKind 与两张文案表

恢复文案的选择收敛成一个枚举 `GuidanceKind`,prompt-facing 文案收敛成两张按 `GuidanceKind` 索引的表:

```python
class GuidanceKind(StrEnum):
    RUN_FAILED = "run_failed"                 # 整个 run 失败(provider/runtime)
    USER_ABORTED = "user_aborted"             # 用户停了普通 run
    PLAN_ABORTED = "plan_aborted"             # 用户停了 plan 中的 turn(plan 仍 active)
    IN_RUN_STEP_FAILED = "in_run_step_failed" # 同一 run 内某 step 失败,就地恢复
```

- **共享**:`GuidanceKind` 枚举 + unfinished summary / severity 语义(`classify_unfinished_tool_calls` / `render_unfinished_summary`)。这消灭了散落在 transcript.py / context.py 的固定英文判断逻辑。
- **不强制共享逐字文案**:transcript 是"上一轮发生了什么"的事后说明,runtime prompt 是"当前这轮该怎么恢复"的操作提示,语用不同。因此用两张表:
  - `GUIDANCE_TEXT_FOR_TRANSCRIPT: dict[GuidanceKind, str]`
  - `GUIDANCE_TEXT_FOR_PROMPT: dict[GuidanceKind, str]`
  - 两表 key 集合必须一致(由测试守护),保证每个 `GuidanceKind` 在两个 surface 都有文案。
- `render_recovery_text(projection, audience="transcript" | "prompt")` 是唯一渲染入口:按 audience 选表,取 `guidance_kind` 文案;仅当 `run_status is not None` 时追加 `render_unfinished_summary`。
- transcript note 的 `metadata.kind` 仍是**按 run status 的粗粒度标签**:`previous_turn_failed` / `previous_turn_aborted`。像 `PLAN_ABORTED` 这样的细粒度恢复语义,当前只存在于 `RecoveryProjection.guidance_kind` 与渲染后的 note 文本里,**不额外编码进 note metadata**。

## 5. guidance_kind 的判定规则

cross-run:

- `run_status == "failed"` → `RUN_FAILED`。
- `run_status == "aborted"` 且 `in_plan_workflow` → `PLAN_ABORTED`。
- `run_status == "aborted"` 且非 plan → `USER_ABORTED`。

in-run:固定 `IN_RUN_STEP_FAILED`。

**plan 被中止 ≠ 任务失败**:`PLAN_ABORTED` 的文案口吻是"规划仍在继续、read-only 仍生效、不要实现改动直到 exit_plan 被批准",绝不能说"任务失败,去恢复"。这与 plan workflow 契约一致:abort 一个 pending plan interaction 时 plan 保持 active,不发 `PlanModeExitedEvent`。

## 6. completion note 不属于本契约

terminal 后台进程完成(`TerminalProcessCompletedEvent` → transcript 的 completion note)是**生命周期投影**,与 failed/aborted 恢复正交,**不并入** `RecoveryProjection`。transcript 继续独立投影 completion note。两者覆盖的事件集合不相交。

## 7. classifier:unfinished tool 语义(沿用,纳入契约)

`classify_unfinished_tool_calls` 把"有 `ToolCallStartEvent`、无 `ToolResultEndEvent`"的 tool call 分类成 `UnfinishedState × ToolSeverity`,并:

- **排除 plan workflow tool**(`enter_plan` / `ask_plan_question` / `exit_plan`,来自 `tool_taxonomy.PLAN_WORKFLOW_TOOL_NAMES`)——它们是控制面工具,不是未完成的副作用工具,不得渲染成 unknown-effect 警告。
- **completed = 全量 `ToolResultEndEvent`**(all-events 语义,见 §2.1):late result 自愈。
- severity 用 `tool_taxonomy` 的共享常量分桶(terminal / file-write / read-only / unknown)。

## 8. 工具名 taxonomy 的归属

`FILE_WRITE_TOOL_NAMES` / `TERMINAL_TOOL_NAMES` / `PLAN_WORKFLOW_TOOL_NAMES` / `READ_ONLY_RECOVERY_TOOL_NAMES` 统一定义在 `runtime/tool_taxonomy.py`,作为底层共享常量,被 recovery / permission / plan 等域复用,避免跨域 import 形成环。

注意两个 read-only 集合是**不同概念,不可合并**:

- `tool_taxonomy.READ_ONLY_RECOVERY_TOOL_NAMES`(`read_file` / `search_files` / `artifact_read`)—— recovery severity 分类用。
- `permission.READ_ONLY_ALLOWED_TOOL_NAMES`(还含 memory 读工具 + todo)—— read-only 权限模式的 gate allowlist 用。

## 9. 禁止事项

- 不在 transcript / runtime prompt 里硬编码散落的恢复文案;所有恢复文案只来自 `GUIDANCE_TEXT_FOR_TRANSCRIPT` / `GUIDANCE_TEXT_FOR_PROMPT`。
- 不用 `scratchpad` 字符串传 abort 语义;abort 类型只走 `AbortKind` + `RunEndEvent.abort_kind`。
- 不用"未 resolve question/exit"推 `in_plan_workflow`;只复用 `reduce_plan_workflow_state`。
- 不把 cross-run 的两个事件窗口合成一个列表喂 classifier + plan reducer。
- 不把 plan 被中止渲染成任务失败口吻。
- 不把 terminal completion note 并入 `RecoveryProjection`。
- 不让 classifier 把 plan workflow tool 当未完成副作用工具。
- 不为 `RecoveryProjection` 增加不驱动任何行为的字段(例如曾经的 `has_partial_assistant_output`,因无 consumer 已删除;未来要加须给精确定义)。

## 10. 测试守护

这份契约由以下测试守住(`tests/test_recovery.py` 为主):

- 两张 guidance 表 key 集合一致。
- failed run → `RUN_FAILED`;普通 user-stop(plan 未 active)→ `USER_ABORTED`;**在已 active plan 中 stop 且该 run 无新 question/exit 事件 → `in_plan_workflow=True` + `PLAN_ABORTED`**(reducer 判定的漏判反例)。
- cross-run 两窗口:late `ToolResultEnd`(`RunEnd` 之后才到)仍把 tool 从 unfinished 中消掉;`in_plan_workflow` 不受 late 事件影响。
- in-run producer:`recovery_mode` → `run_status=None` + `IN_RUN_STEP_FAILED`,`build_llm_context` 注入对应 prompt 文案。
- `AbortKind` / `RunEndEvent.abort_kind` round-trip。
- 回归:transcript failed/aborted note 仍注入、completion note 仍独立、`_strip_unfinished_tool_calls` 行为不变、plan workflow tool 不出现在 unfinished 摘要。
