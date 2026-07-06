# Plan Workflow / Event Architecture Design

## 1. 目标

本文回答两个问题：

1. Pulsara 当前 event 底层系统是怎样设计的。
2. Plan workflow 如何复用现有 event / WAITING_USER / permission mode 机制，同时新增必要抽象，覆盖：
   - 进入 Plan 由谁发起。
   - 退出 Plan 由谁发起、谁批准。
   - Plan HITL 反问原语如何阻塞和恢复。
   - Plan mode 期间允许写什么，最终计划产物落在哪。

本文不是 PR 实施文档，而是产品语义和架构边界的设计文档。后续实现应以这里的语义为准。

## 2. 当前 Event 系统架构

### 2.1 AgentEvent 是 append-only 事实流

当前 runtime 的公共事实层是 `AgentEvent`：

- 定义位置：`src/pulsara_agent/event/events.py`
- 所有事件继承 `EventBase`
- `EventBase` 统一携带：
  - `id`
  - `created_at`
  - `run_id`
  - `turn_id`
  - `reply_id`
  - `sequence`
  - `metadata`

这意味着 Pulsara 现在的 event log 不是 session-scoped 任意事件流，而是 **run / turn / reply scoped 的 runtime 操作日志**。事件天然绑定一次 agent run 中的某个 turn / reply。

`AgentEvent` 是一个显式 union。新增 typed event 不能只加一个 class，还必须接入：

- `EventType`
- 事件 `BaseModel`
- `AgentEvent` union
- `src/pulsara_agent/event/__init__.py`
- event serialization round-trip 测试

### 2.2 EventLog 是存储层，语义在上层投影

EventLog 负责顺序和持久化，不理解业务语义：

- `InMemoryEventLog` 直接保存 `AgentEvent`。
- `PostgresEventLog` 将事件 payload 以 JSON 存入 `agent_events`，并按 session 维护 canonical `sequence`。
- `event_log/serialization.py` 通过 `AgentEvent` union 中的 `type` 找到对应 pydantic class 做 load。

因此，Plan workflow 新增事件时，存储层基本不需要改 schema；真正需要改的是 event union、reducer、timeline、host pending 状态和 resume 入口。

### 2.3 当前事件的主要投影面

同一条事件流被多个系统消费：

- **message replay**：`event_log.replay(reply_id)` + `MessageReducer` 把 provider/runtime events 重建成 `Msg`。
- **prior transcript**：`host/transcript.py` 从 event log 重建下一轮上下文，并额外投影 failed/aborted note、terminal completion note。
- **runtime timeline**：`runtime/timeline.py` 从事件重建 UI / business timeline。
- **memory hooks / ledger**：工具结果、memory event、turn end 等从事件和 `LoopState` 派生。
- **host replay API**：`HostSession.replay_events()` 直接暴露 canonical event stream。

Plan 事件如果要成为可审计事实，必须进入 typed event stream，而不是只写 host 内存字段。

### 2.4 当前 WAITING_USER 机制

现有 `WAITING_USER` 来自 approval resume：

1. 模型产生 tool call。
2. permission gate 返回 `WAIT_FOR_USER`。
3. runtime 设置：
   - `state.pending_tool_calls`
   - `state.status = LoopStatus.WAITING_USER`
   - `state.stop_reason = "waiting_user"`
   - `state.transition(LoopTransition.WAIT_FOR_USER)`
4. runtime emit `RequireUserConfirmEvent(tool_calls=...)`。
5. `_stream_model_loop()` 看到 `WAITING_USER` 后直接 return，不发 `RunEndEvent`。
6. `HostSession` 捕获 suspended `LoopState`，构造 `PendingApproval`。
7. 用户 resolve 后，`resume_after_approval()` 在同一个 run / turn / reply 上 emit `UserConfirmResultEvent`，执行/拒绝工具，继续模型循环。

这套机制有两个关键性质：

- 它是 **suspended run**，不是结束当前 run 再开新 run。
- provider transcript 配对仍然成立：工具调用最终会得到 tool result，或者被拒绝为 tool result。

这正是 Plan HITL 可以复用的底座。

### 2.5 当前 WAITING_USER 的局限

当前实现把 `WAITING_USER` 事实绑定死在 approval 上：

- `HostSession.pending_approval: PendingApproval | None`
- `HostSession._capture_pending_approval()` 遇到 `WAITING_USER` 就调用 `pending_approval_from_state()`
- `pending_approval_from_state()` 要求 `state.pending_tool_calls` 非空且每个 call 都是 `ASKING`
- `resolve_approval()` 只能处理 tool approval

所以 Plan 不能直接复用 `PendingApproval` / `RequireUserConfirmEvent`。Plan 应复用 suspend/resume 骨架，但必须新增 pending interaction 抽象。

## 3. Plan 与 Permission 的关系

### 3.1 Plan 不是 PermissionMode

Plan 不进入 `PermissionMode` enum。权限轴仍然只包含四个 preset：

- `read-only`
- `ask-permissions`
- `accept-edits`
- `bypass-permissions`

Plan 是 workflow 子系统。它通过切换到 `read-only` 获得执行强制力，而不是新增第五种 permission mode。

### 3.2 Plan 进入时捕获 pre-plan permission

进入 Plan 时必须保存：

- `pre_plan_permission_mode`
- 或自定义三轴 policy 时的完整 `EffectivePermissionPolicy`

然后调用现有 `set_permission_mode("read-only")` 进入强制 read-only 状态。

捕获点按进入来源不同：

- **用户 `:plan` / Plan 按钮路径**：host 在 idle 轮边界同步捕获 pre-plan permission，并立即切到 `read-only`。这一步不立即写 event，也不需要 control run；审计 event 在下一次真实 run 中补发。
- **agent `enter_plan` 路径**：`enter_plan` workflow tool 在真实 run 内捕获 pre-plan permission，并切到 `read-only`。

两条路径都必须写入同一个 `PlanWorkflowState`，避免后续退出恢复逻辑分裂。

退出 Plan 只有在用户批准后才恢复 pre-plan permission。这个恢复不是 agent 自行提权，因为恢复目标是进入 Plan 前由 host 已经持有的权限状态，且退出有用户闸门。

### 3.3 Plan 行为塑形不改 system prompt prefix

Plan 的行为塑形必须是 append-only message，而不是修改 `AgentRuntime.system_prompt` 或 `compose_system_prompt()` 的前缀。

理由：

- Plan 会频繁进出。
- system prompt prefix 是 prompt cache 的关键稳定面。
- Pulsara 当前已经采用“all tools visible, some blocked”的模式；Plan 的行为差异应来自 workflow message + permission gate，而不是工具 catalog 重建或 system prefix 改写。

推荐 V1：

- 在每次 Plan active 的模型上下文中追加一个 host-authored runtime instruction message。
- 当用户通过 `:plan` / Plan 按钮发起一轮请求时，host 已经同步切到 `read-only`；同时追加一条一次性的 plan-entry runtime instruction message，而不是改写用户输入。
- plan-entry instruction 明确：
  - 当前已经由 host 进入 read-only plan workflow。
  - 不要直接执行实现；先规划、读取/检查必要上下文、必要时通过 plan question 询问用户。
  - 不要调用 workspace mutation、terminal、memory-write 或其他 side-effecting 工具。
- Plan active instruction 明确：
  - 当前处于 Plan workflow。
  - workspace/file/terminal/durable side effects 被 read-only 阻止。
  - 可以读取、搜索、维护 agent-local todo、询问用户。
  - 最终必须通过 `exit_plan` 请求用户批准。
- 首轮 `:plan` / Plan 按钮 run 中，plan-entry instruction 取代当轮 plan-active instruction，避免重复提示；从后续 run 开始只追加 plan-active instruction。
- 不把这段内容拼入 `system_prompt` 字符串。
- 用户原始输入仍作为普通 user message 和 `RunStartEvent.metadata["user_input"]` 保存；runtime instruction 是独立的 host-authored append-only message。

## 4. Plan 进入语义

### 4.1 用户通过 `:plan` / Plan 按钮发起

V1 不做 control run，但用户入口必须立即具备强制力。

用户通过 REPL/API/UI 的 `:plan` / Plan 按钮发起一轮请求时，host 在 idle 轮边界同步做三件事：

1. 捕获当前 `pre_plan_permission_mode` / `pre_plan_permission_policy`。
2. 调用现有 `set_permission_mode("read-only")`。
3. 设置 `plan_state.active = true`，并标记下一次真实 run 需要补发 `PlanModeEnteredEvent(source="user")`。

这一步不创建 control run、不立即写 event，但从下一轮第一个 tool call 起已经是 read-only enforcement。

语义：

1. 用户原始请求仍作为普通 user input 进入 run。
2. `RunStartEvent.metadata["user_input"]` 保存原始用户请求，不混入 plan-entry 指令。
3. Host 在 context 中追加一次性 plan-entry runtime instruction message。
4. Runtime 在下一次真实 run 中、模型调用前 emit `PlanModeEnteredEvent(source="user")`，使用该 run 的真实 `EventContext`。
5. 该 event 是审计事实；强制力已经在用户按下 `:plan` 时通过 host permission switch 生效。

这比 control run 更小，也避免把“用户轮外 workflow 操作”伪装成 run/turn/reply。它同时关闭了“模型在调用 `enter_plan` 前仍拥有旧权限”的 enforcement gap。

用户自然语言说“先规划”但没有使用 `:plan` / Plan 按钮时，模型可以自主决定调用 `enter_plan`。该路径没有 host 预先强制 read-only，直到 `enter_plan` tool 执行后才进入 Plan。

### 4.2 Agent 发起进入 Plan

Agent 可以通过 `enter_plan` workflow tool 发起进入 Plan。

语义：

- `enter_plan` 不是普通 workspace mutation；它只收窄权限，因此可以在任意非 read-only/非 plan 状态下执行。
- 它不需要用户批准，因为进入 Plan 只会降低能力。
- 它 emit `PlanModeEnteredEvent(source="agent")`。
- 它切换 permission 到 `read-only`。
- 它返回一个普通 tool result 给 provider，保持 tool-call/tool-result 配对。

如果已经处于 Plan，`enter_plan` 应返回 idempotent success 或明确的 already_active result，不应重复 emit `PlanModeEnteredEvent`。

## 5. Plan HITL 反问原语

### 5.1 不复用 RequireUserConfirmEvent

Plan HITL 不是 tool approval：

- approval 是“批准/拒绝一个具体 tool call”。
- Plan HITL 是“模型提出自由问题，用户用自由文本或选项回答”。

因此不应复用：

- `RequireUserConfirmEvent`
- `UserConfirmResultEvent`
- `PendingApproval`
- `ApprovalResolution`

复用它们会污染 unfinished tool recovery、timeline permission_request、approval CLI/API 语义。

### 5.2 复用 WAITING_USER 状态机

Plan HITL 应复用：

- `LoopStatus.WAITING_USER`
- `LoopTransition.WAIT_FOR_USER`
- `HostSession._suspended_state`
- `_run_lock`
- stop/abort suspended run 的基础设施

V1 分两步做：

1. **前置重构**：先把现有 approval pending 行为保持地包进 `PendingInteraction` union，approval 测试必须全绿。这个 PR 不引入 plan 语义，只消除 `HostSession` 对 `pending_approval` 的硬编码。
2. **Plan 语义 PR**：再加入 `PendingPlanInteraction(kind="question" | "exit")`，复用同一套 suspend/resume 机器。

pending payload 应改成并列结构：

```python
PendingInteraction =
    PendingApproval
  | PendingPlanInteraction

@dataclass(slots=True)
class PendingPlanInteraction:
    interaction_id: str
    kind: Literal["question", "exit"]
    host_session_id: str
    runtime_session_id: str
    run_id: str
    turn_id: str
    reply_id: str
    tool_call_id: str
    question: str = ""
    options: tuple[str, ...] = ()
    allow_free_text: bool = True
    exit_request_id: str | None = None
    plan_text: str = ""
    plan_artifact_id: str | None = None
    summary: str = ""
    created_at: float = field(default_factory=time.monotonic)
```

HostSession 应从：

```python
pending_approval: PendingApproval | None
```

演进为：

```python
pending_interaction: PendingInteraction | None
```

或至少新增 plan pending 字段，并强制同一时刻只能存在一个 pending。

### 5.3 Plan question 应作为 workflow tool

推荐把 HITL question 设计成特殊 workflow tool，例如：

- `ask_plan_question`
- 参数：
  - `question: str`
  - `options?: list[str]`
  - `allow_free_text: bool`
  - `reason?: str`

执行语义：

1. 模型发起 `ask_plan_question` tool call。
2. runtime 不真正“完成”该工具，而是：
   - emit `PlanQuestionAskedEvent`
   - 将 `state.status` 置为 `WAITING_USER`
   - 将 pending payload 设为 `PendingPlanInteraction(kind="question")`
   - return/suspend，不发 `RunEndEvent`
3. 用户回答。
4. host 调用 `resolve_plan_question(answer)`。
5. runtime emit `PlanQuestionAnsweredEvent`。
6. runtime 给原 `ask_plan_question` tool call 写入 tool result，内容是用户回答。
7. runtime 继续同一 run 的模型循环。

这种设计保持 provider transcript 的严格配对：模型发出的 tool call 最终总有 tool result。不要让 Plan question 只作为自然语言 assistant text 悬空等待，否则 runtime 很难结构化知道何时该 suspend。

### 5.4 PlanQuestionResolution 数据形状

question 和 exit 共享 `PendingPlanInteraction`，因此不新增 `PendingPlanQuestion`。问题答案的 resolution 建议：

```python
@dataclass(frozen=True, slots=True)
class PlanQuestionResolution:
    interaction_id: str
    answer_text: str
    selected_option: str | None = None
```

## 6. Plan 退出语义

### 6.1 退出由 agent 发起、用户批准

推荐语义：

1. Agent 在 Plan mode 下调用 `exit_plan` tool，并附上 plan draft。
2. runtime 进入 `WAITING_USER`，pending 类型为 `PendingPlanInteraction(kind="exit")`。
3. 用户选择：
   - approve：接受 plan，退出 Plan，恢复 pre-plan permission。
   - revise：给出修改意见，继续留在 Plan/read-only。
   - cancel：取消退出，继续留在 Plan/read-only。
4. runtime 将用户决定作为 tool result 返回给 `exit_plan` tool call。
5. 如果 approve，emit `PlanModeExitedEvent`，并在后续模型循环中允许 agent 开始执行。

这实现了“用户是最终闸”。Agent 可以请求退出，但不能自行恢复权限。

### 6.2 用户主动退出 Plan

用户也可以在 UI/REPL/API 主动退出 Plan。

但为了避免绕过模型已有 draft 的语义，推荐分两类：

- `cancel_plan`：用户终止 Plan workflow，不接受任何 plan，恢复 pre-plan permission。
- `force_exit_plan`：用户强制退出并恢复 pre-plan permission；这属于 host control 操作，应 emit control event。

普通产品路径仍应是 agent `exit_plan` -> 用户 approve。

### 6.3 PlanExitResolution 数据形状

question 和 exit 共享 `PendingPlanInteraction`，因此不新增 `PendingPlanExitApproval`。退出请求的 resolution 建议：

```python
@dataclass(frozen=True, slots=True)
class PlanExitResolution:
    interaction_id: str
    decision: Literal["approve", "revise", "cancel"]
    user_feedback: str = ""
```

## 7. Plan Mode 期间能写什么

### 7.1 不做 workspace plan file 例外

V1 不采用 Claude Code 式“plan file 可写例外”。

Plan mode 期间：

- 不写用户 workspace 文件。
- 不运行 terminal。
- 不写 durable memory。
- 不做外部副作用。
- permission 强制力来自 `read-only`。

这样 Plan 的边界简单、可解释、可测试。

### 7.2 允许写 control-plane state

Plan mode 可以写以下控制面状态：

- Plan workflow events。
- pending interaction state。
- agent-local ephemeral todo。
- final plan draft in `exit_plan` request。

这里的“写”不是 workspace mutation。它属于 runtime control plane，目的是支持 agent 组织计划和 HITL，而不是改变用户项目。

当前 `todo` 工具被标记为 permission-level read-only，因为它只修改 agent-local ephemeral state，不写 workspace / external / terminal / durable memory。Plan 可以使用 todo，但 todo 不应成为唯一权威 plan artifact。

### 7.3 Plan 产物的权威位置

V1 推荐：

- 最终 plan draft 随 `exit_plan` tool call 提交。
- `PlanExitRequestedEvent` 保存结构化 plan draft。
- 用户 approve 后，`PlanModeExitedEvent` 记录 accepted plan 的摘要、来源 event、恢复的 permission。
- 如果 plan 很长，draft 走 Tool Result Artifact 协议，event 中保存 artifact ref，而不是塞超长文本。

因此，V1 的权威链路是：

```text
todo / conversation scratch
  -> exit_plan(plan_draft)
  -> PlanExitRequestedEvent
  -> user approve
  -> PlanModeExitedEvent(accepted_plan)
```

`todo` 是 agent 工作草稿，不是 durable plan。最终可审计计划以 `PlanExitRequestedEvent` / `PlanModeExitedEvent` 为准。

## 8. 需要新增的 Event 类型

### 8.1 为什么要 typed events

Plan workflow 不应只用 `CustomEvent`。

`CustomEvent` 可以用于原型，但不适合作为长期协议：

- timeline 无法稳定识别 plan 等待状态。
- host / UI 无法依靠 schema 渲染 pending payload。
- transcript / recovery 很容易把 plan event 当普通 runtime event 忽略。
- durable replay 缺少字段级兼容边界。

Plan 是产品级 workflow，应进入 typed `AgentEvent`。

### 8.2 V1 typed event 集合

V1 只新增下面六个 typed `AgentEvent`。它们是 Plan workflow 的 canonical event-log facts，**暂不投影为 Oxigraph 语义图节点**。

也就是说：

- `PlanModeEnteredEvent` / `PlanQuestionAskedEvent` / `PlanQuestionAnsweredEvent` / `PlanExitRequestedEvent` / `PlanExitResolvedEvent` / `PlanModeExitedEvent` 全量写入 event log。
- timeline 可以从这些 event 投影 UI 状态。
- transcript 可以按需投影极轻量 note。
- Oxigraph 暂不新增 `PlanQuestion` / `PlanWorkflow` / `AcceptedPlan` 等图节点；如果后续要查询 accepted plan，再单独设计 graph projection。

#### PlanModeEnteredEvent

记录进入 Plan：`source=user|agent`、进入前 permission、reason。

字段：

```python
class PlanModeEnteredEvent(EventBase):
    type: Literal[EventType.PLAN_MODE_ENTERED]
    source: Literal["user", "agent"]
    previous_permission_mode: str | None
    previous_permission_policy: dict[str, object]
    reason: str = ""
```

说明：

- `source=user` 用于 `:plan` / Plan 按钮路径；host 先同步切 read-only，再在下一次真实 run 中补发该审计 event。
- `source=agent` 用于 `enter_plan` workflow tool。
- 必须记录 pre-plan permission，方便审计“退出时恢复到哪里”。
- `source=user` 路径下，`previous_permission_mode` / `previous_permission_policy` 必须来自 `PlanWorkflowState.pre_plan_*` 捕获值，不能从 emit 时的 live permission 读取；emit 时 live permission 已经是 `read-only`。

> 注（source 语义边界）：`source` 只区分进入 Plan 的**技术入口**——`user`=host 经 `:plan`/Plan 按钮同步切入，`agent`=模型调用 `enter_plan` workflow tool。它**不**表达用户意图来源。因此“用户用自然语言说‘先规划’、模型据此自行调 `enter_plan`”会记成 `source=agent`，审计时无法据此区分“模型自主规划”与“用户口头要求、模型执行”。V1 不引入第三种 source 值；若日后需要区分意图来源，应另加字段，而不是重载 `source`。

#### PlanQuestionAskedEvent

记录 agent 在 Plan 中反问用户：`question_id`、`tool_call_id`、`question`、`options`、`allow_free_text`。

字段：

```python
class PlanQuestionAskedEvent(EventBase):
    type: Literal[EventType.PLAN_QUESTION_ASKED]
    question_id: str
    tool_call_id: str
    question: str
    options: list[str] = []
    allow_free_text: bool = True
    reason: str = ""
```

#### PlanQuestionAnsweredEvent

记录用户回答 Plan question：`question_id`、`answer_text`、`selected_option`。

这是 Plan HITL 的用户输入事实，不是普通聊天 `RunStartEvent.metadata["user_input"]`，也不是 tool approval result。

字段：

```python
class PlanQuestionAnsweredEvent(EventBase):
    type: Literal[EventType.PLAN_QUESTION_ANSWERED]
    question_id: str
    answer_text: str
    selected_option: str | None = None
```

回答事件之后，runtime 还必须给原 `ask_plan_question` tool call emit 普通 `ToolResultStart/TextDelta/End`，让 provider transcript 保持配对。原 tool call id 可从 `PendingPlanInteraction(kind="question")` 或对应 `PlanQuestionAskedEvent` 的 `question_id` 关联取得，不需要在 answer event 重复存。

#### PlanExitRequestedEvent

记录 agent 提交 plan 并请求退出：`exit_request_id`、`tool_call_id`、`plan_text` 或 `plan_artifact_id`、`summary`。

字段：

```python
class PlanExitRequestedEvent(EventBase):
    type: Literal[EventType.PLAN_EXIT_REQUESTED]
    exit_request_id: str
    tool_call_id: str
    plan_text: str = ""
    plan_artifact_id: str | None = None
    summary: str = ""
```

如果 `plan_text` 超过 artifact 阈值，应只保留 summary + artifact ref。

#### PlanExitResolvedEvent

记录用户批准 / 要求修改 / 取消退出：`decision=approve|revise|cancel`、`user_feedback`。

字段：

```python
class PlanExitResolvedEvent(EventBase):
    type: Literal[EventType.PLAN_EXIT_RESOLVED]
    exit_request_id: str
    tool_call_id: str
    decision: Literal["approve", "revise", "cancel"]
    user_feedback: str = ""
```

该事件之后也必须给原 `exit_plan` tool call emit 普通 tool result。

#### PlanModeExitedEvent

记录 Plan workflow 真正结束：`source=approved_exit_plan|user_cancel|user_force_exit`、恢复的 permission、accepted plan summary/artifact。

字段：

```python
class PlanModeExitedEvent(EventBase):
    type: Literal[EventType.PLAN_MODE_EXITED]
    source: Literal["approved_exit_plan", "user_cancel", "user_force_exit"]
    exit_request_id: str | None = None
    restored_permission_mode: str | None
    restored_permission_policy: dict[str, object]
    accepted_plan_summary: str = ""
    accepted_plan_artifact_id: str | None = None
```

只有 `approved_exit_plan` 代表“用户接受了 agent 提交的计划”。`user_cancel` / `user_force_exit` 只代表 workflow 结束，不代表 plan 被采纳。

### 8.3 Event 接入点

新增事件必须接入：

- `EventType`
- event `BaseModel`
- `AgentEvent` union
- `event/__init__.py`
- `event_log/serialization.py` round-trip 测试
- `runtime/timeline.py`
- 如需 transcript 投影，再接入 `host/transcript.py`

## 9. 需要新增的 Runtime / Host 抽象

### 9.1 PendingInteraction

当前 host 只有 `pending_approval`。Plan 需要把 pending 概念从 approval 中抽出来，但这一步应先作为行为保持的前置 PR 完成。

前置 PR 目标：

- 只引入 `PendingInteraction` union。
- `PendingInteraction` 里暂时只有 `PendingApproval`。
- `run_turn()` / `stream_turn()` / `stop_current_turn()` / `resolve_approval()` 行为保持。
- approval 相关测试全绿。
- 不引入 plan event、不引入 plan tool、不改变 permission 语义。

Plan 语义 PR 再把 `PendingPlanInteraction` 加入 union。

建议新增：

```python
PendingInteraction =
    PendingApproval
  | PendingPlanInteraction

@dataclass(slots=True)
class PendingPlanInteraction:
    interaction_id: str
    kind: Literal["question", "exit"]
    host_session_id: str
    runtime_session_id: str
    run_id: str
    turn_id: str
    reply_id: str
    tool_call_id: str
    question: str = ""
    options: tuple[str, ...] = ()
    allow_free_text: bool = True
    exit_request_id: str | None = None
    plan_text: str = ""
    plan_artifact_id: str | None = None
    summary: str = ""
    created_at: float = field(default_factory=time.monotonic)
```

HostSession 字段建议演进为：

```python
pending_interaction: PendingInteraction | None = None
_suspended_state: LoopState | None = None
suspended_run_id: str | None = None
```

为了兼容已有 API，可以保留：

```python
def get_pending_approval() -> PendingApproval | None
```

但它应只是 `pending_interaction` 的 typed view，不再是底层唯一 pending 状态。

### 9.2 LoopState pending carrier

不要把 Plan pending 塞进 `state.pending_tool_calls`。

推荐新增：

```python
@dataclass(slots=True)
class LoopState:
    ...
    pending_interaction_kind: str | None = None
    pending_interaction_payload: dict[str, Any] = field(default_factory=dict)
```

不建议为 question / exit 分别新增 `pending_plan_question` / `pending_plan_exit`。它们共享同一台 pending/resume 机器，只通过 `PendingPlanInteraction.kind` 分派差异。

`pending_tool_calls` 继续只表达 tool approval。Plan pending 放入 `pending_interaction_payload` 或等价 typed state 中，避免 runtime state 层把 plan question 和 approval 混在一起。

注意：这并不能单独修复 failed/aborted recovery 的 unfinished tool 分类。当前 unfinished classifier 读的是 event log 中的 `ToolCallStartEvent` / `ToolResultEndEvent`，不是 `LoopState`。`ask_plan_question` / `exit_plan` 作为真实 workflow tool call，在挂起时会有 `ToolCallStartEvent` 且暂时没有 `ToolResultEndEvent`。因此 recovery classifier 必须显式识别 workflow tool：

- `enter_plan`
- `ask_plan_question`
- `exit_plan`

推荐 V1：把 workflow tool call 从 unfinished side-effect classifier 中排除，或标记为 `workflow_control` severity，避免它落入 `UNKNOWN_EFFECT` 并生成“effect is unknown; verify before continuing”的错误恢复 note。

### 9.3 Host resolve 入口

新增 host API：

```python
def get_pending_interaction(...) -> PendingInteraction | None
async def resolve_plan_interaction(..., resolution: PlanInteractionResolution) -> AgentRunResult
async def stream_plan_interaction_resolution(...) -> AsyncIterator[AgentEvent]
```

UI / API 层可以提供便利包装：

```python
async def answer_plan_question(...): ...
async def resolve_plan_exit(...): ...
```

但底层 host/session 只保留一套 plan interaction resolve 入口。

普通 `run_turn()` / `stream_turn()` 在任意 pending interaction 存在时都应拒绝，而不是只拒绝 pending approval。

`stop_current_turn()` 也应从“pending approval special case”变成“pending interaction special case”：无论卡在 approval、plan question、还是 exit approval，用户 stop 都应 abort 同一个 suspended run，并清理 pending。

### 9.4 AgentRuntime resume 分派

不要把 plan resolution 复用进 `_stream_approval_resolution()`。

新增 sibling 方法：

```python
async def resume_after_plan_interaction(...)
async def stream_after_plan_interaction(...)
```

它和 approval resume 共享的只有：

- 校验 `state.status is WAITING_USER`
- emit resolution event
- 写入对应 tool result
- `state.status = RUNNING`
- 继续 `_stream_model_loop()`

它不共享：

- approval decisions
- `ConfirmResult`
- `state.pending_tool_calls`
- file/terminal permission gate 逻辑

`PendingPlanInteraction.kind` 决定 resolution 分派：

- `kind="question"`：emit `PlanQuestionAnsweredEvent`，给 `ask_plan_question` tool call 返回用户回答。
- `kind="exit"`：emit `PlanExitResolvedEvent`，给 `exit_plan` tool call 返回用户决定；如果 decision 是 `approve`，再 emit `PlanModeExitedEvent` 并恢复 pre-plan permission。

### 9.5 Abort / stop 语义

用户 stop 一个 suspended plan interaction 只取消当前 pending interaction，不退出 Plan。

规则：

- pending `kind="question"` 被 abort：清空 pending interaction，run 记为 aborted；`plan_state.active` 保持 `True`，permission 保持 `read-only`。
- pending `kind="exit"` 被 abort：清空 pending interaction，run 记为 aborted；`plan_state.active` 保持 `True`，permission 保持 `read-only`，不发 `PlanModeExitedEvent`。
- 只有 `PlanModeExitedEvent` 表示 Plan 真正结束并恢复 pre-plan permission。

否则 `stop_current_turn()` 会变成一条绕过用户批准的隐式退出通道，和 Plan 退出闸门冲突。

### 9.6 Plan HITL budget

Plan HITL 往返可能很多：`ask_plan_question` / resolve / 继续，或 `exit_plan` / revise / 继续 / 再 `exit_plan`。如果完全复用普通 `max_turns` / `max_tool_calls`，长规划容易被误杀成 `max_turns` 或 `tool_error_budget`。

V1 推荐新增独立上限，而不是完全豁免：

```python
max_plan_interactions_per_run: int = 16
max_plan_exit_revisions_per_run: int = 8
```

规则：

- workflow control tool calls 计入 plan interaction budget，不计入 ordinary side-effect tool error budget。
- `ask_plan_question` 每次挂起计 1 次 plan interaction。
- `exit_plan` 每次挂起计 1 次 plan interaction；`decision="revise"` 额外计 1 次 exit revision。
- 超过 plan interaction budget 时，runtime 应产生清晰的 plan-specific failure，而不是伪装成普通 tool failure。
- 普通 read/search/todo 等工具仍按现有 tool-call budget 计数，避免 plan mode 变成无限工具循环。

## 10. Workflow Tools

### 10.1 enter_plan

`enter_plan` 是 workflow tool，不是 workspace tool。

建议参数：

```json
{
  "reason": "string"
}
```

执行：

1. 若 plan inactive：
   - 保存 pre-plan permission。
   - set permission to `read-only`。
   - set `plan_state.active = true`。
   - emit `PlanModeEnteredEvent(source="agent")`。
   - 返回 success tool result。
2. 若 plan active：
   - 返回 already_active success tool result。

该工具的 effect 是降权，不需要用户 approval。

### 10.2 ask_plan_question

`ask_plan_question` 是 Plan-only workflow tool。

调用条件：

- 只能在 plan active 时调用。
- 普通 mode 下调用应返回 denied/error tool result，不能进入 waiting。

执行：

1. emit `PlanQuestionAskedEvent`。
2. 设置 `WAITING_USER`。
3. 保存 `PendingPlanInteraction(kind="question")`。
4. suspend 当前 run。

resolve：

1. emit `PlanQuestionAnsweredEvent`。
2. 为原 tool call 写入 tool result：

```json
{
  "answer_text": "...",
  "selected_option": "..." 
}
```

3. 继续同一 run。

### 10.3 exit_plan

`exit_plan` 是 Plan-only workflow tool。

建议参数：

```json
{
  "plan": "string",
  "summary": "string"
}
```

执行：

1. emit `PlanExitRequestedEvent`。
2. 设置 `WAITING_USER`。
3. 保存 `PendingPlanInteraction(kind="exit")`。
4. suspend 当前 run。

resolve：

- approve：
  - emit `PlanExitResolvedEvent(decision="approve")`
  - emit `PlanModeExitedEvent(source="approved_exit_plan")`
  - 恢复 pre-plan permission
  - 给 `exit_plan` tool call 返回 approved tool result
  - 继续同一 run
- revise：
  - emit `PlanExitResolvedEvent(decision="revise")`
  - 保持 plan active/read-only
  - tool result 包含用户反馈
  - 继续同一 run
- cancel：
  - emit `PlanExitResolvedEvent(decision="cancel")`
  - 保持 plan active/read-only
  - tool result 说明退出取消
  - 继续同一 run

> 注（tool result 结构）：三种 decision 返回给 `exit_plan` tool call 的 result 应是结构化对象，与 §6.3 `PlanExitResolution` 对齐，至少含 `{"decision": "approve"|"revise"|"cancel", "user_feedback": "..."}`，而不是裸自由文本。这样模型能结构化区分“计划已批准、可开始执行”（approve）与“仍在 plan/read-only、需据 feedback 改方案”（revise/cancel），不必从自然语言里猜当前是否已退出 Plan。

## 11. Timeline / Transcript / Memory 投影

### 11.1 Timeline

`runtime/timeline.py` 当前只把 `RequireUserConfirmEvent` 显示成 `permission_request`，并用它决定 `waiting_user`。

Plan 事件应新增 timeline item kind：

```python
TimelineItemKind =
    ...
  | "plan_mode"
  | "plan_question"
  | "plan_exit_request"
```

投影规则：

- `PlanQuestionAskedEvent` -> item status `waiting`，run status `waiting_user`。
- `PlanQuestionAnsweredEvent` -> 对应 item status `answered`，run 不再因该 question 等待。
- `PlanExitRequestedEvent` -> item status `waiting`，run status `waiting_user`。
- `PlanExitResolvedEvent` -> 对应 item status 为 `approved` / `revise` / `cancel`。
- `RunEndEvent` 仍是最终 run 状态来源。

### 11.2 Transcript

Plan workflow event 不应默认变成普通 prior messages。

推荐：

- active plan 的行为塑形 message 由 HostSession 当前 `plan_state` 注入，不从历史 transcript 反复投影。
- 已解决的 plan question/answer 不需要作为 system note 重放；它们已经通过 tool result 进入同一 run 的 provider transcript。
- `PlanModeExitedEvent` 可以在后续 transcript 中投影一条轻量 system note：
  - “Previous plan was approved by the user.”
  - 附 accepted plan summary / artifact ref。
- `user_cancel` / `user_force_exit` 不应暗示 plan 被批准。

### 11.3 Memory

Plan workflow 默认不写 durable memory。

边界：

- Plan active 阶段，`remember_*` 工具在 read-only 下被 permission gate 阻止。
- `PlanQuestionAnsweredEvent` 是用户本轮回答，不自动提升为长期 preference。
- `PlanModeExitedEvent` 的 accepted plan 是 runtime/task evidence，不等于 durable user preference。
- 如果后续要把 approved plan 写入 memory，应作为单独 memory governance 决策，不在 Plan V1 自动做。

## 12. 用户入口与 Control Run 决策

当前 `EventBase` 没有 session-only event。所有 event 都必须有：

- `run_id`
- `turn_id`
- `reply_id`

用户 idle 状态下点击 `:plan` / Plan 按钮时，没有天然 `EventContext`。V1 不创建 control run，也不把 `EventBase` 迁移成 session-scoped event。

### 12.1 V1 不采用 control run

control run 看似能让 idle 进入 Plan 可审计，但它会把“非模型 workflow 操作”伪装成 run/turn/reply，导致：

- Postgres 里出现假 run / turn parent rows。
- timeline 需要区分真实模型 run 和 control run。
- transcript 需要避免把 control run 投进 provider context。
- memory hooks / reflection 需要避免把 control run 当普通任务处理。

这和改 `EventBase` 一样会带来大范围涟漪。V1 明确不做。

### 12.2 V1 的用户入口：同步切 read-only + 追加 runtime instruction

`:plan` / Plan 按钮不创建 control run，但会立即改变 host workflow state：

1. Host 捕获 pre-plan permission。
2. Host 同步调用 `set_permission_mode("read-only")`。
3. Host 设置 `plan_state.active=True` 和 `pending_entry_audit=True`。
4. 用户原始请求照常进入下一次真实 run 的 `RunStartEvent.metadata["user_input"]`。
5. Host 追加一条 host-authored plan-entry runtime instruction message。
6. Runtime 在该真实 run 的模型调用前 emit `PlanModeEnteredEvent(source="user")`，并清掉 `pending_entry_audit`。

这个设计保留了 event log 的真实 run/turn/reply 结构，也避免了用户 idle enter 的独立 event surface。同时，read-only enforcement 从用户点击 `:plan` 后的下一轮第一个 tool call 起已经生效，不依赖模型先自觉调用 `enter_plan`。

### 12.3 为什么不改 EventBase

另一种方案是把 `EventBase` 改成支持 session-scoped event。

不推荐在 Plan V1 做：

- 影响所有 event log schema 和 parent-row 校验。
- Postgres event log 当前会为每个 event upsert runs / turns。
- 大量 reducer/timeline/test 都默认 event 有 run/turn/reply。

追加 runtime instruction 是更小的 V1 适配。session-scoped event 可作为未来更大架构 PR 单独评估。

## 13. 与 Permission / Tool Visibility 的关系

Plan mode 使用现有 read-only enforcement。

这意味着：

- 所有工具仍可见。
- side-effecting 工具被 permission gate 拦。
- `todo` 允许，因为它是 agent-local ephemeral state。
- `terminal` / `terminal_process` 在 read-only 下按当前 permission contract 被拦。
- `enter_plan` / `ask_plan_question` / `exit_plan` 属于 workflow tool，不应被 read-only 拦住。

工具广告必须保持恒定。`enter_plan` / `ask_plan_question` / `exit_plan` 应无条件注册，并在普通 mode、Plan active、read-only、bypass 等所有上下文中都出现在 provider 可见的 tools 数组里。Plan 不通过“隐藏/显示工具”来 gate workflow，而是在执行期由 runtime 根据 `plan_state` 处理：

- 普通 mode 下调用 `ask_plan_question` / `exit_plan`：返回受控 tool result/error，说明当前不在 Plan。
- Plan active 下调用 `enter_plan`：返回 idempotent already-active result。
- 用户 `:plan` 路径已经由 host 同步进入 Plan，模型不需要再调用 `enter_plan`。

这条规则直接服务 §3.3 的 prompt cache 不变量：Plan 进出不能改变 base system prompt，也不能改变 tools catalog；只能追加 runtime instruction message，并在 execution path 里 gate。

实现上要避免一个陷阱：如果 workflow tools 直接注册进普通 ToolRegistry，并且 permission gate 只按 read-only allowlist 判断，那么 `ask_plan_question` / `exit_plan` 会被 read-only 拦掉。需要二选一：

1. workflow tools 不走普通 permission gate，由 runtime 在 tool loop 里先识别并处理。
2. workflow tools 进入 read-only allowlist，但工具实现自己限制只能在 plan active 时调用。

推荐方案 1：workflow tools 是 runtime control plane，不是普通 workspace tool。它们应该在 permission gate 之前被识别，避免把 workflow 控制权混进 workspace side-effect 语义。

### 13.1 Workflow tool 拦截落点

Workflow tool 拦截必须落在“provider tool calls 已经解析成内部 `ToolCall`，但还没有进入 permission gate / ToolExecutor”之间。

按当前 runtime 结构，落点应在 tool batch dispatch 的中央路径里：assistant reply 中的 tool blocks 被解析成 `parsed_calls` 之后、`permission_gate.evaluate(parsed_calls)` 之前。也就是说：

1. 先完成 provider delta / tool-call id 的普通解析和去重。
2. 再扫描 `parsed_calls` 是否包含 workflow tool name。
3. 如果包含，进入 workflow branch，并且本批不再调用 ordinary permission gate / executor。
4. 如果不包含，完全走现有 permission gate / executor 路径。

这能保证 workflow tool 与 approval gate、批量工具执行、suspend/resume 共存时只有一个控制流入口。不要把 workflow tool 做成普通 ToolExecutor 插件后再依赖工具实现自己挂起；那样 permission gate、批内 sibling 执行、provider tool-result 配对都会变成分散语义。

### 13.2 批内混合 tool call 语义

Workflow tool 是控制流屏障。模型在同一个 assistant reply 中同时发出 workflow tool 和其他 tool call 时，runtime 必须避免“先挂起/切权限，同时又执行 sibling side-effect tool”的模糊语义。

V1 规则：

1. 在 permission gate 之前扫描本批 tool calls。
2. 如果批内没有 workflow tool，走普通 permission gate / tool execution。
3. 如果批内有 workflow tool：
   - 只处理 provider 顺序中的第一个 workflow tool。
   - 所有 sibling tool calls 都不执行。
   - runtime 为 sibling calls emit 普通 tool result，状态为 denied/not-executed，说明“not executed because a plan workflow control tool suspended or changed workflow state; retry after the workflow step completes”。
4. 如果第一个 workflow tool 是 `enter_plan`：
   - 执行 `enter_plan`，进入 read-only plan state。
   - sibling calls 不执行，即使它们是 read-only。模型下一轮可在 plan state 下重新发起读取。
5. 如果第一个 workflow tool 是 `ask_plan_question` 或 `exit_plan`：
   - emit 对应 plan request event。
   - sibling calls 先收到 not-executed tool result。
   - workflow tool 自身进入 `WAITING_USER`，其 tool result 等用户 resolve 后再补。

这样 provider transcript 仍保持配对：同批 sibling calls 有立即 tool result，挂起的 workflow call 在用户 resolve 后得到 tool result。更重要的是，任何 side-effecting sibling call 都不会绕过 plan workflow 的控制边界。

> 注（provider 配对时序）：这与现有 approval suspend 完全同构。挂起期间，这一条 assistant message 的全部 tool result 尚未凑齐（sibling 的 not-executed result 先到，workflow tool 自己的 result 等用户 resolve 后才补）是预期状态，不是 unpaired 错误。和 approval 一样，整条 message 的 result 会在 resume 后一次性补齐再继续模型循环。

## 14. 状态模型

建议 HostSession 新增：

```python
@dataclass(slots=True)
class PlanWorkflowState:
    active: bool = False
    entered_by: Literal["user", "agent"] | None = None
    entered_at: float | None = None
    pre_plan_permission_mode: str | None = None
    pre_plan_permission_policy: dict[str, object] | None = None
    pending_entry_audit: bool = False
    latest_accepted_plan_summary: str = ""
    latest_accepted_plan_artifact_id: str | None = None
```

这个状态是 host workflow state，不替代 event log。event log 是可审计事实，HostSession state 是当前执行缓存。

### 14.1 PlanWorkflowState reducer

恢复 durable session 时，应从 event log 重放 plan workflow events，重建当前 `PlanWorkflowState`。Reducer 规则：

1. 初始状态：`active=False`。
2. `PlanModeEnteredEvent`：
   - `active=True`
   - `entered_by=event.source`
   - 保存 `previous_permission_mode` / `previous_permission_policy` 到 pre-plan 字段
   - `pending_entry_audit=False`
3. `PlanModeExitedEvent`：
   - `active=False`
   - 清空 pre-plan 字段
   - 保存 latest accepted plan summary/artifact（仅当 `source="approved_exit_plan"`）
   - `accepted_plan_artifact_id` 是 approve 时写入 `ArtifactStore` 的 durable pointer，不是预留字段
4. `PlanQuestionAskedEvent` / `PlanQuestionAnsweredEvent`：
   - 不改变 plan active 状态。
5. `PlanExitRequestedEvent` / `PlanExitResolvedEvent`：
   - 不直接改变 plan active 状态；只有 `PlanModeExitedEvent` 才表示真正退出。

冷启动约束：

- 如果 reducer 得到 `active=True`，HostSession 在接受下一轮前必须重新施加 `read-only` permission。
- 如果 reducer 得到 `active=False`，HostSession 不应因为历史 plan event 继续保持 read-only。
- 如果存在 pending plan interaction，HostSession 还必须恢复对应 `PendingPlanInteraction`，否则应把该 run 标记为 interrupted/aborted，而不是静默允许新 turn 绕过 pending。

> 注（budget 计数与 restore）：§9.6 的 `max_plan_interactions_per_run` / `max_plan_exit_revisions_per_run` 是 per-run 计数。reducer 重建的是 `PlanWorkflowState`，不含这些计数器。durable restore 后在**同一 run** 继续时，若不一并恢复已消耗计数，重启会绕过 budget 上限。该 budget 是防失控、非安全闸门，因此 V1 可在两者中择一并写明：要么从 event log 中该 run 的 plan event 数重建计数，要么明确 budget 只做进程内保证（restore 后计数归零）。

用户 `:plan` / Plan 按钮路径有一个极短的 pre-run 状态：host 已经同步切到 read-only，但 `PlanModeEnteredEvent(source="user")` 要等下一次真实 run 才能 emit。该状态必须保存在 HostSession 的 `PlanWorkflowState(pending_entry_audit=True)` 中。若产品支持 durable host-session restore，这个 host state 也必须持久化；否则 V1 应明确该 pre-run pending audit window 只在进程内保证。

如果进程在用户点击 `:plan` 后、下一次真实 run 前重启，且 V1 没有 durable host-session restore，那么这次 `:plan` 状态会被 fail-safe 地丢弃：系统回到旧权限，不留下幽灵 Plan；用户需要重新触发 `:plan`。

## 15. 实施边界

### 15.1 V1 要做

- 前置 PR：行为保持地把 `pending_approval` 重构为 `PendingInteraction = PendingApproval`，approval 测试全绿。
- typed plan events。
- 加入 `PendingPlanInteraction(kind="question" | "exit")`。
- plan question / exit 共享一套 pending + resume。
- `:plan` / Plan 按钮同步切 read-only，并在下一次真实 run 追加一次性 plan-entry runtime instruction message。
- plan active 时追加 append-only runtime instruction message。
- `enter_plan` / `ask_plan_question` / `exit_plan` 无条件注册并恒定广告。
- read-only permission 切换、pre-plan permission 捕获/恢复。
- timeline 支持 plan waiting。
- workflow tool 在 permission gate 前集中拦截。
- workflow tool 批内隔离语义。
- failed/aborted unfinished classifier 识别 workflow tool。
- plan HITL 独立 budget。
- PlanWorkflowState reducer 与冷启动 read-only 重施加。
- tests 覆盖 event round-trip、suspend/resume、permission restore。

### 15.2 V1 不做

- workspace plan file 例外。
- durable memory 自动写 approved plan。
- 多 plan 并发。
- nested plan。
- provider system prompt prefix mutation。
- 把 plan 作为第五个 permission mode。
- control run。
- session-scoped event base migration。

### 15.3 V1 已知限制

- Plan active 时 `terminal` / `terminal_process` 按当前 read-only permission contract 被拦，因此 V1 规划期不能查看后台 terminal 进程 retained log。`terminal_process` 的 observe-action 豁免属于 permission lattice 的独立精化，不混进 Plan V1。
- 如果用户 `:plan` 的 pre-run host state 没有 durable restore，进程重启会让这次 plan-entry fail-safe 丢失。用户重新发送 `:plan` 即可；系统不应保留半截 active Plan。
- Plan HITL 使用独立 interaction budget，但普通 read/search/todo 等工具仍受现有 run/tool budget 约束。

## 16. 测试计划

### 16.1 Event contract

- 每个 plan event JSON round-trip。
- 旧事件 load 不受影响。
- `AgentEvent` union 可识别 plan events。

### 16.2 Enter Plan

- `:plan` / Plan 按钮同步捕获 pre-plan permission，并立即切到 read-only。
- 下一次真实 run 在模型调用前 emit `PlanModeEnteredEvent(source="user")`。
- `PlanModeEnteredEvent(source="user").previous_permission_*` 来自 `PlanWorkflowState.pre_plan_*`，不是 emit 时已经变成 read-only 的 live policy。
- plan-entry instruction 不改写用户输入，不改 base system prompt。
- 首轮 `:plan` run 只注入 plan-entry instruction，不同时注入 plan-active instruction。
- 第二轮起注入 plan-active instruction。
- 第一轮任意 side-effecting tool call 都被 read-only gate 拦住，即使模型没有调用 `enter_plan`。
- agent `enter_plan` 在 run 内创建 `PlanModeEnteredEvent`，返回 tool result。
- enter plan 捕获 pre-plan permission，并切到 read-only。
- repeated enter plan idempotent。

### 16.3 Plan Question

- `ask_plan_question` 发起后：
  - run status `WAITING_USER`。
  - 不发 `RunEndEvent`。
  - HostSession 暴露 `PendingPlanInteraction(kind="question")`。
  - 普通新 turn 被拒。
- resolve 后：
  - emit `PlanQuestionAnsweredEvent`。
  - 原 tool call 收到 tool result。
  - 同一 run 继续执行。

### 16.4 Exit Plan

- `exit_plan` 发起后：
  - run status `WAITING_USER`。
  - HostSession 暴露 `PendingPlanInteraction(kind="exit")`。
- approve：
  - emit `PlanExitResolvedEvent(decision="approve")`。
  - emit `PlanModeExitedEvent(source="approved_exit_plan")`。
  - 恢复 pre-plan permission。
- revise/cancel：
  - 不恢复 permission。
  - 保持 plan active/read-only。
  - 用户反馈进入 tool result。

### 16.5 PendingInteraction 前置重构

- approval 行为保持：pending approval 仍能 resolve / deny / abort。
- 普通 new turn 在 pending approval 时仍被拒。
- `get_pending_approval()` 仍返回 `PendingApproval` typed view。
- `stop_current_turn()` 对 pending approval 的行为不变。

### 16.6 Workflow classifier / batch guards

- `enter_plan` / `ask_plan_question` / `exit_plan` 在普通 mode、Plan active、read-only、bypass 下 tool spec 列表保持恒定。
- failed/aborted unfinished classifier 不把 `enter_plan` / `ask_plan_question` / `exit_plan` 渲染成 unknown-effect side-effect warning。
- 同批 `ask_plan_question + write_file`：只挂起 plan question，write_file 收到 not-executed tool result，不写文件。
- 同批 `enter_plan + read_file/write_file`：只执行 enter_plan，siblings 收到 not-executed tool result，模型下一轮可在 plan state 下重试。
- 同批多个 workflow tool：只处理 provider 顺序中的第一个，其余收到 not-executed tool result。

### 16.7 Durable reducer / cold start

- 重放 `PlanModeEnteredEvent` 后 `PlanWorkflowState.active=True`。
- 重放 `PlanModeExitedEvent` 后 `PlanWorkflowState.active=False`。
- cold start 重建出 active plan 时，HostSession 在接受新 turn 前重新施加 read-only。
- `pending_entry_audit=True` 的 pre-run host state 若支持 durable restore，必须恢复并在下一真实 run 发 `PlanModeEnteredEvent(source="user")`；否则测试应明确该窗口只做进程内保证。

### 16.8 Stop / abort

- pending approval / pending plan question / pending plan exit 都能被 `stop_current_turn()` abort。
- abort 后 pending interaction 清空。
- abort 后 `plan_state.active=True`，permission 仍为 `read-only`，不发 `PlanModeExitedEvent`。
- aborted plan question 不被 unfinished tool recovery 误判成 ordinary workspace tool failure。

### 16.9 Plan HITL budget

- `ask_plan_question` / `exit_plan` suspend 计入 `max_plan_interactions_per_run`。
- `exit_plan` 的 `decision="revise"` 计入 `max_plan_exit_revisions_per_run`。
- 超过 plan interaction budget 时返回 plan-specific failure，不走 ordinary tool error budget 文案。
- 普通 read/search/todo 等工具仍按现有 tool-call budget 计数。

### 16.10 Transcript / timeline

- plan-entry / active plan runtime instruction 不改 base system prompt。
- active plan 注入 append-only runtime instruction message。
- timeline 能显示 plan question waiting。
- timeline 能显示 exit plan waiting / approved / revise / cancel。

## 17. 最终结论

Plan workflow 可以接入当前事件系统，但接入点不是复用 approval event，而是复用更底层的 suspended run 机制。

准确说：

- `AgentEvent` / EventLog / sequence / replay 是可复用的事实底座。
- `WAITING_USER` / `_suspended_state` / `_run_lock` 是可复用的阻塞底座。
- `RequireUserConfirmEvent` / `PendingApproval` / `ApprovalResolution` 不是可复用语义；它们只属于 tool approval。
- V1 不做 control run；用户 `:plan` / Plan 按钮在 host 侧同步切 read-only，并在下一次真实 run 中补发 `PlanModeEnteredEvent(source="user")`。
- Plan 期间不写 workspace plan file；只写 control-plane state。
- 最终 plan 的权威事实是用户批准后的 `PlanModeExitedEvent`，而不是 todo 或普通 assistant text。
- 被批准 plan 的原文由 approve 路径同步写入 `ArtifactStore`；`accepted_plan_artifact_id` / `latest_accepted_plan_artifact_id` 指向这份 durable plan artifact。
