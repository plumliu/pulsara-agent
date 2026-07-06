# Recovery Contract Design (debt §3: failed / aborted recovery)

_Draft for review. Freeze before implementation, like PLAN_WORKFLOW_EVENT_ARCHITECTURE.zh.md._

## 1. 目标

把当前**散在四处、各说各话**的 failed/aborted 恢复语义,收敛成一个 recovery contract:统一的恢复词汇 + 单一的 guidance 文案真源,同时消除 host↔runtime 之间 stringly-typed 的 abort 暗线。

不改 canonical event 真值语义,不写 synthetic tool result,不引入 provider repair。

## 2. 现状(已逐条核实)

恢复语义目前散在四个来源,彼此独立:

1. **transcript note**(`host/transcript.py`):`_NOTE_STATUS` 把 `RunEndEvent.status` ∈ {failed, aborted} 映射成两条**固定英文** `SystemMsg`(`FAILURE_NOTE_TEXT` / `INTERRUPTED_NOTE_TEXT`),直接扫 `event_log.iter()` 原始事件。
2. **unfinished classifier**(`host/unfinished_tools.py`):已有 `UnfinishedState` / `ToolSeverity` / `classify_unfinished_tool_calls()` / `render_unfinished_summary()`——已是结构化恢复语义的雏形,但挂在 transcript 边上,且已 import `PLAN_WORKFLOW_TOOL_NAMES` 做排除。
3. **runtime prompt**(`runtime/context.py:38`):`if state.recovery_mode:` 追加一条**固定英文** user message。
4. **abort 语义**(`runtime/agent.py:298`):`state.scratchpad["abort_reason"] = reason`,stringly-typed 暗线,**不进 event log**。

关键事实(决定设计):

- 两个消费者 scope 不同:**transcript** 在下一轮开始时从 **event log** 重建(跨 run、事后);**runtime prompt** 在当前 run 进行中从 **live LoopState** 读(同 run、在途)。这不是同一个恢复事件。
- `recovery_mode` 来源粗:tool error(`_after_tool_results`)、model error(`_recover_or_fail_model`)都置 `True`;abort 根本不走这套。
- `RunEndEvent` 字段:`status` / `stop_reason` / `error_message`,**无 abort_kind**。
- 运行级 abort 实际只有 `user_stop` 一种(teardown/watchdog 是 terminal-process 级,不是 run 级)。
- abort 一个挂起的 plan interaction 仍是 `abort_run("user_stop")`,但 plan 保持 active(§9.5)。这种"用户停了 plan 反问"**不是失败**,recovery guidance 不能对模型说"你的工作失败了,去恢复"。

## 3. 核心设计:一个词汇表,两个 producer

**不要**做成"一个 RecoveryState 让两边都消费同一份"——那会强行合并"跨 run 重建"和"in-run 信号"两条不同来源,造成漏抽象(某些字段对一方永远空)。

正确做法:**统一类型 + 双 producer**。

- 定义**一个** `RecoveryProjection` 类型(恢复词汇)。
- **两个 producer**:
  - `project_recovery_from_events(events)` —— cross-run,从 event log 产出,喂 transcript。
  - `project_recovery_from_state(state)` —— in-run,从 live `LoopState` 产出,喂 runtime prompt。
- 两个 consumer(transcript note renderer、runtime prompt builder)吃同一个**类型**,但不假装同一个**来源**。

统一的是**恢复语义词汇 + guidance 文案的单一真源**;不统一的是数据通路。

**cross-run producer 的两个事件窗口(写死,防 late-result 回归)**:`project_recovery_from_events` 内部必须同时维护两个窗口,**口径不同、不可互换**:

- `run_events_all` —— 按 `run_id` 取目标终止 run 的**全量**事件(**不按 sequence 截断**),喂 `classify_unfinished_tool_calls`。这保住现有 all-events 语义:`RunEnd` 之后才到的 late `ToolResultEnd` 仍会把对应 tool 从 unfinished 中消掉(由 `test_rebuild_prior_messages_late_tool_result_removes_unfinished_summary` 锁定;现状见 transcript.py 的 `_unfinished_summary`)。
- `events_through_target_run_end` —— 按 `sequence` **截到目标 `RunEnd`(含)**,喂 `reduce_plan_workflow_state` 求 `in_plan_workflow`。plan active 是"截至该 run 结束"的状态,不能掺入 RunEnd 之后的 late 事件。

两个窗口对同一个 run 故意取不同切片:classifier 要 late-result 自愈(全量),plan reducer 要 run-terminal 时点(截断)。实现者不要图省事用同一个列表喂两者。

### 3.1 RecoveryProjection 词汇

```python
@dataclass(frozen=True, slots=True)
class RecoveryProjection:
    run_status: Literal["failed", "aborted"] | None      # cross-run 时表示终结 run 的结果；in-run 可为空，由 guidance_kind 决定是否需要恢复
    abort_kind: AbortKind | None                          # 仅 aborted 时有意义
    unfinished_tools: tuple[UnfinishedToolCall, ...]      # classifier 输出(已排除 workflow tool)
    in_plan_workflow: bool                                # 终止时是否处于 plan(决定 guidance 口吻)
    guidance_kind: GuidanceKind                           # prompt-facing 文案选择器
```

> 注:`has_partial_assistant_output` 在 V1 不驱动任何行为(无 producer 计算、无 consumer 消费),故从词汇中删除。若未来要让 guidance 提示"上一轮的半截 assistant 文本可能不可靠",再以精确定义(半截 text block / 未配对 assistant turn / tool 残片,三选一)重新加入。

字段取舍:
- `run_status`:cross-run 表示终结 run 的结果,从 `RunEndEvent.status` 取;in-run 可为空,由 `guidance_kind` 决定是否需要恢复(例如 `IN_RUN_STEP_FAILED`)。
- `abort_kind`:见 §4。
- `unfinished_tools`:复用现有 classifier;**保持 workflow tool 排除**。
- `in_plan_workflow`:**复用 `reduce_plan_workflow_state` 的语义边界**——cross-run 把"截至该终止 run 结束的事件流"喂给 plan reducer,取其 `active`;in-run 从 `plan_state.active` 读。**不要**临时扫"该 run 是否有未 resolve 的 `PlanQuestionAsked`/`PlanExitRequested`":在一个已 active 的 plan 里发起的普通 read-only planning turn(无新 question/request 事件)中途被 stop 时,那种扫法会漏判成普通 abort。用于让 guidance 区分"规划被用户中止"vs"任务失败"。
- `guidance_kind`:见 §5,把文案选择从"散在 context.py + transcript.py 的三段固定英文"收成一个枚举。

### 3.2 completion note 不并入本契约

terminal completion note(`_completion_note_after_last_run_start`)是**生命周期投影**,不是恢复语义——它报告后台进程完成,与 failed/aborted 无关。本契约**不动它**,留在 transcript 作为独立投影(survey §7.2.2 已论证两者不相交)。这样 recovery contract 边界更干净。

## 4. AbortKind:收掉 scratchpad 暗线

把 `state.scratchpad["abort_reason"]: str` 升级成 `LoopState` 上的 typed 字段:

```python
class AbortKind(StrEnum):
    USER_STOP = "user_stop"
    # 预留扩展点;V1 运行级 abort 只有 user_stop。
```

- `LoopState.abort_kind: AbortKind | None = None`,在 `stream_abort_run` 里设置(替代 `scratchpad["abort_reason"]`)。
- **持久化到 `RunEndEvent`**:新增 `abort_kind: str | None = None` 字段(additive,旧事件 load 不受影响)。这样 cross-run producer 从事件读 abort_kind,而不是靠 scratchpad(scratchpad 本来就不进 log)。
- "是否在 plan workflow 中被中止"**不**做成新字段——由 cross-run producer 扫事件推导(`in_plan_workflow`),避免字段冗余。

诚实标注:V1 `AbortKind` 只有 `USER_STOP` 一个真实成员。typed 化的收益是 (a) 杀掉 stringly-typed 暗线,(b) 留扩展点 + 让 `RunEndEvent` 可审计 abort 类型。不臆造不存在的 teardown/watchdog 运行级 abort。

## 5. GuidanceKind:guidance 文案单一真源

现在 prompt-facing 文案散在三处(context.py 的固定英文、transcript.py 的 `FAILURE_NOTE_TEXT` / `INTERRUPTED_NOTE_TEXT`)。收成一个枚举 + 一张映射表:

```python
class GuidanceKind(StrEnum):
    RUN_FAILED = "run_failed"                 # 整个 run 失败(provider/runtime)
    USER_ABORTED = "user_aborted"             # 用户停了普通 run
    PLAN_ABORTED = "plan_aborted"             # 用户停了 plan 反问/退出(plan 仍 active)
    IN_RUN_STEP_FAILED = "in_run_step_failed" # 同一 run 内某 step 失败,就地恢复
```

- 共享的是 **`GuidanceKind` 枚举** + **unfinished summary / severity 语义**(单一真源,消灭散落的固定英文判断逻辑)。
- **逐字文案不强制共享**:transcript note 与 runtime prompt 语用不同——前者是"上一轮发生了什么"的事后说明,后者是"当前这轮该怎么恢复"的操作提示。强行共享逐字文案会把两个 surface 再次绑死。
- 因此用**两张表**,按 `GuidanceKind` 索引、同义不同口吻:
  - `GUIDANCE_TEXT_FOR_TRANSCRIPT: dict[GuidanceKind, str]` —— 取代 transcript.py 的 `FAILURE_NOTE_TEXT` / `INTERRUPTED_NOTE_TEXT`。
  - `GUIDANCE_TEXT_FOR_PROMPT: dict[GuidanceKind, str]` —— 取代 context.py 的固定英文恢复句。
  - 两张表的 key 集合必须一致(由测试守护),保证每个 `GuidanceKind` 在两个 surface 都有对应文案。
- `unfinished_tools` 摘要仍由 `render_unfinished_summary` 生成,**追加**在 guidance 文案之后(保持现有 transcript note 的"框架 + 摘要"结构)。
- `PLAN_ABORTED` 是新区分:之前 `INTERRUPTED_NOTE_TEXT` 一条死字符串无法表达"这是规划被中止,不是任务失败";新枚举让 plan-abort 的口吻正确(不说"你的工作失败了")。

## 6. 实施顺序

### Step 0(前置,行为保持但不是纯机械搬家):抽 recovery 域

把恢复语义提升成独立域模块 **`runtime/recovery.py`**(落点锁定在 runtime,不放 host/):
- 挪入现有 `unfinished_tools.py` 的 `UnfinishedState` / `ToolSeverity` / `UnfinishedToolCall` / `classify_unfinished_tool_calls` / `render_unfinished_summary`(语义不变,换位置 + 调用方 import)。
- 作为 `RecoveryProjection` / `AbortKind` / `GuidanceKind` / 两个 producer 的干净落点。

**这一步不是纯 import 搬家,必须先锁定依赖方向。** 现状 `host/unfinished_tools.py` 已经 import `runtime.plan`(`PLAN_WORKFLOW_TOOL_NAMES`)+ `runtime.permission`(`FILE_WRITE_TOOL_NAMES` / `TERMINAL_TOOL_NAMES`),它本就横跨 plan / permission / transcript 三个 surface,不是纯 host helper。迁移后的依赖方向:

- **采纳:`runtime.recovery` 依赖 `runtime.plan` + `runtime.permission`。** 三者同为 runtime 层,且 `plan`/`permission` 都不 import `recovery`,无 import cycle;`runtime.agent`(Step 4 的 in-run producer)会 import `recovery`,而 agent 本就 import plan+permission,不形成环。
- host/transcript 改 import `runtime.recovery`(host→runtime,沿用既有方向)。
- **替代方案(本步不做)**:把 `FILE_WRITE_TOOL_NAMES` / `TERMINAL_TOOL_NAMES` / `PLAN_WORKFLOW_TOOL_NAMES` 抽成更底层的共享 tool-taxonomy 常量再迁。这是更纯的分层,但属于 scope creep,留作未来清理;V1 采纳上面的依赖方向即可。

验收:现有 transcript / unfinished 测试全绿,行为不变;`uv run ruff check` 无 import-cycle 报错。这步对标 plan mode 前先抽 `PendingInteraction` 的做法。

### Step 1:定义类型 + 文案真源
- `RecoveryProjection` / `AbortKind` / `GuidanceKind` / `GUIDANCE_TEXT_FOR_TRANSCRIPT` / `GUIDANCE_TEXT_FOR_PROMPT`。
- 纯新增,无消费者改动。配单元测试(枚举、文案表完整性)。

### Step 2:cross-run producer + transcript 改吃投影
- `project_recovery_from_events(events)` 产出 `RecoveryProjection`,内部维护 §3 的两个事件窗口(`run_events_all` 喂 classifier、`events_through_target_run_end` 喂 plan reducer),口径不可互换。
- `transcript.py` 的 `_note_message` / `_unfinished_summary` 改为消费投影:failed/aborted note + unfinished summary 统一从投影渲染,不再各自扫原始事件拼 note。
- `_NOTE_STATUS` 的两条固定英文被 `GUIDANCE_TEXT_FOR_TRANSCRIPT` 取代。
- 行为基本保持(failed/aborted note 文案可微调),`completion note` 与 `_strip_unfinished_tool_calls` 不动。

### Step 3:abort_kind typed 化 + 持久化
- `LoopState.abort_kind` 字段;`stream_abort_run` 设置它,删 `scratchpad["abort_reason"]`。
- `RunEndEvent` 加 `abort_kind` 字段 + serialization round-trip 测试。
- cross-run producer 读 `RunEndEvent.abort_kind`,区分 `USER_ABORTED` vs `PLAN_ABORTED`(后者结合 `in_plan_workflow`)。

### Step 4:in-run producer + runtime prompt 改吃投影
- `project_recovery_from_state(state)` 产出 in-run `RecoveryProjection`(`guidance_kind=IN_RUN_STEP_FAILED`)。
- `context.py:38` 的 `if state.recovery_mode:` + 固定英文,改成消费 in-run 投影 + `GUIDANCE_TEXT_FOR_PROMPT`。
- `recovery_mode: bool` 可保留为 in-run producer 的输入信号,或升级成更结构化的 in-run 恢复状态(本步评估,不强求一次到位)。

## 7. 不做(明确排除)
- 不并入 terminal completion note(独立生命周期投影)。
- 不改 `_strip_unfinished_tool_calls` 的剥离行为(只复用其 completed 定义)。
- 不写 synthetic tool result / 不做 provider-only repair(survey §7.3/§7.4 留作未来逃生口)。
- 不臆造运行级 teardown/watchdog abort kind(V1 只有 user_stop)。
- 不让 runtime prompt 从 event log 重建(贵且 scope 错;它消费 in-run 投影)。
- 不投影 Oxigraph 图节点。

## 8. 必须保住的既有契约(回归守护)
- **plan workflow tool 不进 unfinished 分类**(刚做完的契约)——新 producer 必须沿用排除。
- **abort 一个 pending plan interaction:plan 仍 active、read-only,不发 PlanModeExitedEvent**(§9.5)——其 `RecoveryProjection.guidance_kind` 应为 `PLAN_ABORTED`,绝不能渲染成"任务失败,去恢复"。
- **late ToolResultEnd 跨 RunEnd 的 all-events completed 语义**——`unfinished_tools` 现有的"completed = 全部 ToolResultEndEvent"行为不变。

## 9. 测试计划
- **Step 0**:import 路径迁移后,现有 `test_unfinished_tools.py` / `test_host_core.py`(transcript note)全绿,无行为变化。
- **类型/文案**:`GUIDANCE_TEXT_FOR_TRANSCRIPT` 与 `GUIDANCE_TEXT_FOR_PROMPT` 各自覆盖所有 `GuidanceKind` 且 key 集合一致;`AbortKind` round-trip。
- **cross-run producer**:failed run → `run_status=failed` + `guidance_kind=RUN_FAILED`;user-stop run(plan 未 active)→ `aborted` + `USER_ABORTED`;在已 active 的 plan 里 stop(无论该 run 有无新 question/request 事件)→ `in_plan_workflow=True` + `PLAN_ABORTED`(不渲染失败口吻)。`in_plan_workflow` 由 plan reducer 截至该 run 结束的 `active` 决定,需专测"plan active 下普通 planning turn 被 stop"这一漏判反例。
- **cross-run producer 两窗口**:同一个 aborted/failed run,若其 `ToolResultEnd` 在 `RunEnd` 之后才到(late result),unfinished summary 仍把该 tool 消掉(`run_events_all` 全量窗口);同时 `in_plan_workflow` 由截至 `RunEnd` 的 plan reducer 决定,不受 late 事件影响。两窗口口径分别专测,守住 `test_rebuild_prior_messages_late_tool_result_removes_unfinished_summary` 不回归。
- **abort_kind 持久化**:`RunEndEvent.abort_kind` round-trip;reducer/transcript 从事件读到正确 abort 类型。
- **in-run producer**:tool error / model error → in-run 投影 `IN_RUN_STEP_FAILED`,prompt 注入对应 guidance。
- **transcript 回归**:failed/aborted note 仍注入、completion note 仍独立、`_strip_unfinished_tool_calls` 行为不变、plan workflow tool 不出现在 unfinished 摘要。
- **真实链路**:沿用现有 `test_real_llm_*` 的 failed/aborted recovery 场景确认不回归(脚本化为主,real LLM 不新增)。

## 10. 验证
```bash
uv run pytest tests/test_unfinished_tools.py tests/test_host_core.py tests/test_runtime_timeline.py tests/test_event_log_contract.py -q
uv run pytest -q
uv run ruff check src/pulsara_agent/runtime/recovery.py src/pulsara_agent/host/transcript.py src/pulsara_agent/runtime/context.py src/pulsara_agent/runtime/agent.py
```

## 11. 交付物
本设计冻结后,落成 `RECOVERY_CONTRACT.zh.md`(或并入 contracts/),再按 Step 0→4 分 PR 实施。每个 PR 独立可验,Step 0 行为保持先合。
