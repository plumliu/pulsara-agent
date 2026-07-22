# Failed / Aborted Recovery Contract

_Created: 2026-06-27_

这份文档定义 Pulsara failed / aborted recovery 的长期契约。它不是 implementation plan(实现指导见根目录 `RECOVERY_CONTRACT_DESIGN.zh.md`),而是当前和后续实现必须遵守的硬协议:中断/失败后的恢复语义,必须用**一个结构化恢复词汇**表达,由**两个 producer**(跨 run / in-run)分别产出,绝不在 transcript / runtime prompt 里各自硬编码散落的恢复文案。

核心立场:

- **一个词汇,两个 producer。** `RecoveryProjection` 是唯一恢复词汇;cross-run(从 event log)与 in-run(从 live `LoopState`)是两个独立 producer。统一的是语义与文案真源,不统一的是数据通路。
- **guidance 文案两张表、同义不同口吻。** transcript(事后说明)与 runtime prompt(操作提示)共享 `GuidanceKind`,但各有一张文案表;不强制逐字共享。
- **终结语义是 typed、可审计的事实。** 运行中用 `AbortKind` 表达 stop authority；durable
  `RunEndEvent` 同时保存 low-level `RunStopReason`、`RunTerminalizationKind` 与按分支要求的
  `abort_kind`，不再走 `scratchpad` 或自由字符串暗线。
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

`project_recovery_from_state(state)` 从 live `LoopState` 产出,喂 runtime prompt。`state.in_run_recovery is None` 时返回 `None`。产出固定 `guidance_kind=IN_RUN_STEP_FAILED`、`run_status=None`、`unfinished_tools=()`。

## 3. AbortKind:typed、可审计

abort 类型用 `AbortKind`(`recovery.py`)表达,不再走 `scratchpad["abort_reason"]` 的 stringly-typed 暗线:

```python
class AbortKind(StrEnum):
    USER_STOP = "user_stop"
    HOST_TEARDOWN = "host_teardown"
```

- `LoopState.abort_kind: AbortKind | None`,在 `stream_abort_run` 设置。
- **持久化到 hard-cut `RunEndEvent` contract**：`stop_reason` 必须属于
  `primitives.run_lifecycle.RunStopReason`，`terminalization_kind` 必须属于
  `RunTerminalizationKind`；只有 user-stop/host-teardown 分支携带 matching `abort_kind`。旧的缺字段
  RunEnd payload 不属于 supported replay schema。cross-run producer只从 durable terminal fact读取，不从
  scratchpad推断。
- `abort_run` / `stop_current_turn` 链路上的 `reason` 参数类型为 `AbortKind`(不是 str),确保不会有裸字符串流入 `state.abort_kind`。

V1 运行级 abort 有两个成员：`USER_STOP` 表示用户显式停止；`HOST_TEARDOWN` 表示 HostSession/application 生命周期关闭。二者不得共享“用户停止”的恢复文案。terminal process 的 teardown / watchdog 仍是 process 级原因，不进入这个 run 枚举。

## 4. GuidanceKind 与两张文案表

恢复文案的选择收敛成一个枚举 `GuidanceKind`,prompt-facing 文案收敛成两张按 `GuidanceKind` 索引的表:

```python
class GuidanceKind(StrEnum):
    RUN_FAILED = "run_failed"                 # 整个 run 失败(provider/runtime)
    USER_ABORTED = "user_aborted"             # 用户停了普通 run
    PLAN_ABORTED = "plan_aborted"             # 用户停了 plan 中的 turn(plan 仍 active)
    HOST_TEARDOWN = "host_teardown"            # host/session 生命周期关闭，不归因于用户 stop
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
- `run_status == "aborted"` 且 `abort_kind == HOST_TEARDOWN` → `HOST_TEARDOWN`（优先于 plan active 判定）。
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
- in-run producer:`recovery_mode` → `run_status=None` + `IN_RUN_STEP_FAILED`，Stage-3 recovery candidate producer把typed projection写入`PreparedContextCandidateSet`。
- `AbortKind` / `RunEndEvent.abort_kind` round-trip。
- 回归:transcript failed/aborted note 仍注入、completion note 仍独立、`_strip_unfinished_tool_calls` 行为不变、plan workflow tool 不出现在 unfinished 摘要。

---

## 11. Long-horizon model/window recovery

`ModelCallStartEvent` 必须冻结 lifecycle-specific `ModelStreamRecoveryPlanFact`、run activation、downstream predicate contract、terminal IDs 与
rollout reservation quote。reopen 对 main/direct/window 三类 Start-without-End 使用各自 terminal batch修复：semantic prefix末尾恰有一个合法
provider error时保持 `provider_error`；没有 provider error时使用 `runtime_error`；多个、顺序错误、attribution漂移或未知 schema一律 structural
latch。generic reservation repair不得与 model stream owner双重结算。

completed main call缺 control disposition时，recovery只使用 Start-frozen activation、reopen high-water与versioned downstream predicate；不得
伪造 live segment ID、调用 live coordinator或生成 permit。没有 downstream facts时先写 `SUPPRESSED_BY_RECOVERY`，再写
recovered-interrupted RunEnd。已有 RunEnd、late conflicting winner、suppressed 后出现 accepted-only downstream、或 predicate contract漂移均
fail closed。

active context window、projection、rollout state与status hint必须从 confirmed checkpoint + bounded contiguous delta exact replay；checkpoint只是
versioned reducer memoization，不是第二权威。window compaction Started-without-terminal由service-owned recovery补稳定 failed terminal，成功的
close-old/open-new batch不得被拆开。PARTIAL/UNKNOWN ledger state、unsupported historical event domain或无法证明 continuity时禁止恢复执行。

---

## 12. Provider input generation recovery

Recovery从session ledger重建`generation start/append/close/rollover`、committed core、preparation ownership、
transcript frontier和persistent vector root；不得从当前compiler重新生成历史prefix，也不得以run-scoped子集
替代完整session generation lifecycle。

Prepared-without-ModelStart由session-owned preparation owner按原stable candidate处理：manifest/append artifact尚未
FULL时继续confirm；`NONE`重试原字节；`FULL`完成原子ModelStart；`PARTIAL/UNKNOWN`保留owner并latch。RunEnd、
Host close或rejected-before-start必须先consume或typed abandon preparation，不能留下永久阻塞scope的active owner。

Terminal projection与control disposition分批到达时，reducer先保存awaiting-disposition状态。只有ACCEPTED才产生
pending continuation；SUPPRESSED消费等待态但不进入prefix。下一次prepare只能将pending continuation exact join
ordered projection中的同一个unit，不能回查raw model stream、按fragment模糊匹配或自行插入。RunEnd前仍有无法
解释的completed projection/disposition或continuation必须fail closed。

普通session resume若前一generation已因`session_close`关闭，必须建立绑定旧closed epoch的新generation ID；不能
复用旧start carrier。Explicit compaction rollover必须恢复old-close + authority + new-start + append + ModelStart的
完整atomic矩阵。缺artifact、prefix/frontier mismatch或invalid causal proof时不得报告exact replay；cache/resident
丢失可以从durable vector exact restore，但canonical authority conflict必须latch而非静默rollover。

Source-head恢复先读取committed semantic document identity，再从append artifact/vector unit重建hydration与placement attribution；不得调用当前memory/capability/source renderer。相同
semantic snapshot保持原revision和wire bytes。Runtime-observation rewrite恢复必须验证parent Long-Horizon authority、内嵌stable state、partition proof、transitive coverage与全部confirmed
artifacts；缺失/冲突时latch，不得回退为完整旧observation replay或standalone auxiliary rebase。

恢复任何open generation时，所有historical replacement source head必须有compiler-frozen
disposition。`projection_failed/no_new_fact/semantic_noop`保留exact head；empty/terminal消费
exact predecessor；`rewrite_required/allocation_omitted`只能恢复同一typed source-disposition
rollover candidate。不得因当前collector没有candidate而删除或复活旧head。One-shot recovery还要
恢复initial append中的exact clock；不得以当前时间补造新clock。

---

## 13. Terminal monitor recovery

Restart从durable registration/observation/termination/disposition/account events exact重建monitor core、双cursor、progress limiter、pending notification和reservation余额。`FIRING`的NONE保留相同stable candidate重试；UNKNOWN/PARTIAL进入reconciliation，不得发布notification。Rate-window历史、progress count、completion-only状态、wake chain和automatic ordinal均不得因进程重启归零。

正常reopen先验证notification/account与monitor projection checkpoint的exact ledger prefix，再从checkpoint保存的previous state重放bounded typed delta并逐字段核对resulting state；随后才消费checkpoint后的bounded sparse delta和其中显式引用的exact ToolResult，不得退化为全ledger bootstrap。首次checkpoint的sequence-zero base必须逐字段等于projection-kind-specific canonical genesis；后续base必须与PostgreSQL前一checkpoint完整state/fingerprint精确相等。Monitor physical recovery是Host-open拥有的async owner，不在`RuntimeSession.__post_init__`同步无限重试。HostCore在runtime wiring前只创建一个absolute reopen deadline；RuntimeSession materialization/transcript/provider-input/tool/long-horizon bootstrap、notification restore、monitor restore、双projection cross-join、HostSession plan bootstrap、physical monitor-owner recovery及其嵌套checkpoint I/O必须复用同一个monotonic timestamp，任一阶段不得以`now + timeout`续期。持续NONE在deadline后以typed blocked-open终结，UNKNOWN/PARTIAL继续latch。Host listener安装后必须立即检查checkpoint恢复出的pending notification，不能等下一次human turn才启动dispatch。

Restart遇到已经FULL但尚未delivery的`active_pending_delivery` progress/heartbeat时，必须保留原pending observation并让Host ingress先完成delivery/disposition；不得用`interrupted_by_host_restart` termination覆盖它。Disposition FULL后，session-owned recovery owner再根据confirmed completion authority生成terminal observation，或以该disposition为cause提交`interrupted_by_host_restart` termination与monitor-slot release。任何termination都不得无处分地清除一个durable pending observation。

Host处于`WAITING_USER`时，confirmed monitor notification写durable`host_waiting_user` defer并等待human merge；不得每200ms重复尝试runtime ingress。Registration返回正文与`initial_baseline_cursor`必须来自同一次typed journal snapshot，禁止随后再次poll形成重复输出窗口。

Public tool hard cut后，unfinished `terminal_monitor.list`在能恢复exact action时可归类为read-only；`register/cancel`归为terminal side effect。当前event缺少可验证action或参数损坏时必须保守归为terminal。旧`terminal_process` monitor action、result projection与decoder不可恢复，runtime store reset后只接受新catalog。

Output recovery先验证journal stream identity、retained/spooled cursors、page manifest与hash。Range完整时可恢复exact delta；range已淘汰或spool发生typed gap时只能生成`UnavailableRecoveredTerminalOutputDeltaFact`，保留completion/status/cursor authority但不伪造正文。Artifact/page hash conflict属于authority untrusted；spool普通不可用不得阻止completion terminalization。Session close将无法继续delivery的confirmed notification稳定 disposition为`session_closed`并释放对应slot。
