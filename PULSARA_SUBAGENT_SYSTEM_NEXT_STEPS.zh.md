# Pulsara Subagents 系统下一步完善路线

## 1. 背景

Pulsara 已经完成了 subagent runtime 的第一层地基：

- `spawn_agent` / `wait_agent` / `stop_agent` 工具入口；
- parent runtime session 写 subagent graph typed events；
- child 使用独立 runtime session，不污染 parent transcript；
- child raw events 通过 session-bound metadata 注入 subagent attribution；
- child result 可通过 `wait_agent` 显式领取；
- background result 可通过 `subagent:results` context section 进入 parent compile；
- `SubagentResultDeliveredEvent` 只在结果真正进入一次 parent model-call payload 后写入；
- real LLM dogfood 已跑通，包括 Postgres-backed parent / child event log。

这说明 Pulsara 的大方向是成立的：

> Subagent 首先是 runtime thread/session，不是脚本函数，也不是一段伪造成用户消息的文本。

但当前实现仍处于 V1 substrate 阶段。它更像 Codex-style runtime thread tree，还不是 Tanzo-style task product layer。下一步最重要的工作不是急着做 Deno/Python workflow runner，而是把 runtime primitive 和 task product layer 分清楚。

本文档整合 Tanzo、Codex harness、本地 Claude Code/OpenClaw 调研，以及 reviewer 对工具数量和 PR 顺序的建议，作为下一轮 subagents 系统的实施路线。

### 1.1 核心哲学：Subagent 是次级证据生产者

Subagent 不应该被设计成“主 agent 必须等到它成功”的下级执行单元。它更像一个带 provenance 的次级证据生产者：

```text
可检查事实：filesystem / terminal / artifact / event log
  > parent agent 亲自读取、运行、验证得到的证据
  > subagent 产出的 summary / recommendation / report
```

因此，subagent result 不是最高真值；subagent failure 也不是需要自动修好的异常，而是 parent 可以使用的一条运行时事实。主 agent 在 subagent 失败后，应该能够亲自读取文件、运行命令、检查 artifact，或者创建一个新的、更窄的 task，而不是围着旧 task 做 retry/reset/redefine。

冻结原则：

- 不做模型可见的 `retry_agent` / `retry_agent_task` / `reset_agent_task` / `redefine_agent_task`；
- 不做 task graph 的自动 retry / cascade unblock；
- 如果需要“再试一次”，创建一个新的 `SubagentTask`，并把旧 task 的 failure/result/artifact 作为 context/evidence；
- 旧 task 保持 immutable history，不被 reset、supersede 或 overwrite；
- runtime 可以做 bounded internal robustness retry，例如 transient network/provider error，但这不成为 task-level 产品语义。

---

## 2. 核心结论：冻结两层身份

下一步必须先冻结两个身份：

```text
SubagentTask = 逻辑任务身份
SubagentRun  = 一次真实 child runtime run
```

这两个概念不能混。

### 2.1 SubagentTask 是逻辑任务

`SubagentTask` 表达“有一个工作需要完成”。它可以：

- 尚未启动；
- 等待依赖；
- 正在由某个 child run 执行；
- 完成；
- 失败；
- 被取消；
- 因上游失败而 blocked。

这些语义都应该挂在 `task_id` 上：

- `depends_on`
- batch wait
- phase/progress
- explicit result
- dependency blocking
- task-level consumption marker

`task_id` 表达的是一条 immutable task history。失败后的“再试一次”不复用旧 `task_id`，而是创建新的 task，并显式引用旧 task 的失败事实作为 context。

### 2.2 SubagentRun 是一次 child runtime run

`SubagentRun` 表达“一次真实 child runtime/session 执行”。它是 runtime primitive 层的事实：

- 什么时候启动；
- child runtime session id 是什么；
- 使用哪个 context policy；
- 使用哪个 capability profile；
- 什么时候 completed/failed/cancelled；
- 结果 artifact 在哪里；
- 是否被 `wait_agent` 按 run 显式领取。

V1 中，一个 task-backed task 至多绑定一个 active/completed child run。`SubagentRun` 仍然是必要身份，因为 runtime primitive、child event stream、capability profile、context policy、result delivery 都需要绑定到真实 child session。

```text
task:review-1
  subagent_run:a failed

task:review-2
  context includes task:review-1 failure evidence
  subagent_run:b completed
```

如果所有语义都挂到 `subagent_run_id`，`depends_on`、wait all/first、phase/result report 和 result consumption 都会变得别扭。冻结 task/run 分层后，后面的 DAG 和产品层才不会长歪。

### 2.3 Profile 是 canonical execution contract，role 只是展示

`profile` 应该成为 child 执行契约：

```text
profile
  -> system prompt supplement
  -> allowed tool profile
  -> permission constraints
  -> output contract
  -> diagnostics/version
```

`role` 只能做展示或兼容字段。模型可以选择 profile，但不能手写 raw allowlist。runtime 必须负责把 profile 解析为真实 capability/permission 边界。

---

## 3. 对标 Tanzo 与 Codex 后的判断

### 3.1 Tanzo：task product layer 更成熟

Tanzo 的 multi-agent 更像一个成熟 task service：

- parent 通过 `spawn` 创建一个或多个 sub-agent task；
- `await` 等待结果；
- `tasks` 查看状态；
- `steer` 追加指导；
- `cancel` 取消任务；
- child 通过 `report` 提交 phase/result；
- task 有 `pending/running/blocked/done/failed/cancelled` 状态机；
- 支持 `dependsOn`；
- 支持 approval blocking / resume；
- 支持 retry、dependency cascade、orphan reconciliation，这是 Tanzo 的产品选择，不是 Pulsara 必须照搬的方向；
- 内置 `explore` / `verify` / `review` 角色；
- UI 有 task progress / approval / result switcher。

Tanzo 没有让主 agent 写一段 workflow script。它的 workflow 编排来自结构化工具 + TaskService + task state machine：

```text
main agent prompt
  -> spawn / await / tasks / steer / cancel
  -> TaskService
  -> subagent_tasks state machine
  -> child conversation drivers
  -> report phase/result
```

这说明一个重要判断：

> Multi-agent DAG 应该服务于认知分工，而不是变成模型需要维护的一门程序语言。

### 3.2 Codex：runtime substrate 更底层

本地开源 Codex core 的 multi-agent harness 更接近 thread tree：

- `multi_agents_v2.rs` 暴露 `spawn_agent` / `send_message` / `followup_task` / `wait_agent` / `interrupt_agent` / `list_agents`；
- `multi_agents_spec.rs` 冻结工具 schema、`fork_turns`、canonical task path、same tools、concurrency slots；
- `agent/control.rs` / `agent/control/spawn.rs` 是真正的 control plane；
- `thread_spawn_edges` 持久化 `parent_thread_id -> child_thread_id`；
- app-server / TUI 把 collab tool call 和 sub-agent activity 投影成独立 UI item。

Codex 做得很对的是：

- subagent 是新的 runtime thread，不是函数调用；
- model-facing tool handler 和 spawn/control plane 分离；
- child 继承 parent 当前 runtime config；
- `fork_turns=none|all|N` 是显式 context policy；
- canonical task path 让 parent-child 引用比裸 UUID 友好；
- 有 concurrency/depth cap；
- child final answer 通过 inter-agent completion envelope 回到 parent mailbox。

但 Codex 没有 Tanzo 的 task product layer。它没有真正的 `depends_on`、batch task、phase/result report、retry cascade 或 task board 语义。

### 3.3 Pulsara 的路线

Pulsara 应吸收两边：

```text
Codex-style runtime substrate
  child runtime session / typed graph / fork policy / control API / inspect projection

+ Tanzo-style task product layer
  task_id / profile / batch task / depends_on / wait all-first / phase-result report

- general workflow script as fact source
```

也就是说：

- `spawn_agent` 保留为底层 runtime primitive；
- `create_agent_tasks` 承担高层 task product layer；
- Deno / WorkflowScript 未来只能作为 authoring surface，不能成为事实源；
- 所有状态、权限、结果、delivery、inspect 仍归 Pulsara runtime 和 typed events。

---

## 4. 不可退让的边界

### 4.1 Child raw loop events 不写 parent transcript stream

child raw events 写 child runtime session。parent stream 只写 graph / edge / delivery / task facts。

这样可以避免 parent `rebuild_prior_messages()`、compaction、resume 把 child 原始对话误当成 parent 历史。

### 4.2 Child result 是 internal evidence，不是 user message

child completion 不能伪造成用户输入。

允许进入 parent 的方式只有：

1. `wait_agent` 对应的 provider-native tool result；
2. `wait_agent_tasks` 对应的 provider-native tool result；
3. parent next compile 的 `subagent:results` internal section；
4. artifact / evidence / graph projection；
5. future workflow node result section。

`SubagentResultDeliveredEvent` 仍只表示：

> result 已进入一次实际发起的 parent model-call payload。

仅仅 compiled 成功不算 delivered；必须能 join 到同一次 `ModelCallStartEvent`。

### 4.3 默认 isolated，fork 必须显式

默认 child 不继承 parent transcript。`fork` 必须是显式 context policy，并且带：

- source context id；
- source model call index；
- max chars / token estimate；
- artifact refs；
- diagnostics。

### 4.4 Child 不参与 memory system

V1 继续保持：

- child 不做 memory recall；
- child 不持有 `memory_*` tools；
- child 不写 memory candidate；
- child 不参与 governance。

parent 可以把 memory-derived context 摘要注入 child task prompt，但 memory authority 留在 parent / host session。

### 4.5 Graph projection 是 inspect 一等公民

subagents 不能只在 transcript 中可见。必须投影为 graph / task board：

```text
parent run
  ├─ task:review status=running current_run=subagent_run:abc
  │   └─ subagent_run:abc child_runtime_session=...
  ├─ task:verify status=blocked_dependency
  ├─ wait edge -> consumed result
  └─ delivered edge -> context_id/model_call_index
```

---

## 5. 工具面：克制地分三层

Reviewer 的提醒是对的：工具数量必须谨慎。Pulsara 不能一次把所有动作都暴露给模型，否则 parent agent 会在 run/task/report/send 之间迷路。

推荐把工具面明确分成三层，并在 system prompt 中强约束默认用法。

### 5.1 底层 runtime primitive

这些工具保留 Codex-style 语义，薄而稳定：

#### `spawn_agent`

底层 primitive。立即启动一个 child runtime。

- 输入：单个 task prompt / profile / context policy；
- 输出：`subagent_run_id`；
- 不支持 batch；
- 不支持 `depends_on`；
- 不支持 retry 语义；
- 不创建 logical `task_id`，除非 runtime 为 primitive run 生成兼容 projection。

#### `wait_agent`

底层 primitive。等待一个 `subagent_run_id`。

- 保留当前兼容语义；
- 成功 wait 后写 run-level consumption edge；
- 不等同于 `SubagentResultDeliveredEvent`；
- 如果后续 parent model call 真的看见该 tool result，由 context/model-call 事件证明。

#### `stop_agent`

底层 primitive。停止一个 `subagent_run_id`。

### 5.2 只读 observability

#### `list_agents`

只读状态工具。它是 parent 模型的“任务板”，也是用户理解系统的窗口。

它应返回 unified projection：

- primitive runs；
- task-layer tasks；
- current child run；
- child runtime session；
- phase；
- running / blocked / completed / failed / cancelled；
- consumed；
- delivered；
- dependency state；
- permission/bypass diagnostics。

`list_agents` 必须是 read-only capability，输出有界，不能泄漏 child raw transcript。这里的 read-only 只描述副作用等级，不覆盖 subagent tool family 的 bypass-only 要求。

### 5.2.1 Subagent system tools 的 permission 规则

Subagent system tools 有一点特殊：它们应该始终出现在 capability exposure 中，因为 permission mode 可以在同一会话内随时切换；但真正执行必须由 gate 明确门控。

冻结规则：

- 所有 subagent system tool descriptors 必须始终在 exposure 中可见；
- 非 bypass 下不能把 descriptor 隐藏成 unavailable / hidden；
- 非 bypass 下应由 permission gate 产生 typed deny，而不是 exposure 阶段移除工具；
- `list_agents` 虽然是 read-only observability tool，也属于 subagent system tool family；
- `spawn_agent` / `wait_agent` / `stop_agent` / `list_agents` / `create_agent_tasks` / `wait_agent_tasks` / `stop_agent_task` 全部必须要求 `permission_mode=bypass`；
- 非 bypass 下调用任何 subagent system tool，一律直接 deny；
- deny reason code 固定为 `subagent_requires_bypass_mode`；
- subagent system tools 不创建 HostSession approval pending；
- bypass 只允许进入 subagent system path，不代表 child 无限制。
- 每个新增 subagent system tool 的 PR 都必须新增同类 gate regression：non-bypass deny + `reason_code="subagent_requires_bypass_mode"`，bypass 下才进入原功能路径。

运行期安全边界：

- child spawn 时必须记录 parent/session permission snapshot；
- child model-loop safe point 和 child tool-execution safe point 都必须重新检查 parent/session permission mode；
- 如果 parent/session 已不再是 bypass，child 必须 cancel/fail-closed，并写 typed diagnostic；
- permission mode 收窄终止 child 时，`SubagentRun.status=cancelled`；
- 如果 child 属于 task-backed run，`SubagentTask.status=cancelled`；
- terminal reason code / diagnostic 固定为 `subagent_bypass_revoked`；
- 因 permission mode 收窄而终止 child 时，不进入 HostSession pending，也不尝试自动恢复；
- 这保证 bypass-only 不只是入口门，而是 child 生命周期内持续成立的安全条件。

parent 与 child exposure 的边界：

- “subagent system tool descriptors 始终可见”是 parent runtime 的 exposure 规则；
- child runtime V1 不允许 nested subagent；
- child exposure 中应 hard-deny 或隐藏 subagent system tools，即使 parent/session 仍处于 bypass；
- 若未来支持 depth > 1，必须单独定义 child subagent profile、depth cap、permission inheritance 和 graph attribution，不能复用 parent 规则。

child runtime 仍必须受以下约束：

- built-in profile / capability profile；
- hard-deny tools；
- workspace / filesystem / terminal policy；
- MCP snapshot / capability binding；
- memory disabled 规则；
- context compiler 和 artifact/evidence 规则。

child 内部如果遇到 approval、MCP input-required、plan question 等需要 HostSession pending 的路径，V1 一律 fail-closed，并写 typed diagnostic / denied tool result。不要在 V1 里做 nested pending router。

### 5.3 高层 task product layer

这些工具面向 orchestrator 主路径。

#### `create_agent_tasks`

创建一个或多个逻辑任务。

PR5 阶段只开放 independent batch，不开放 dependency scheduler。也就是说，PR5 中 `depends_on` 字段要么不出现在 schema 中，要么必须为空；真正的 `depends_on` 在 PR7 才进入正式语义。

PR5 示例：

```json
{
  "tasks": [
    {
      "task_key": "review",
      "label": "review",
      "profile": "review_worker",
      "task": "Review the changed files for correctness."
    },
    {
      "task_key": "verify",
      "label": "verify",
      "profile": "verification_worker",
      "task": "Run focused tests and report pass/fail."
    }
  ]
}
```

语义：

- 返回 `task_id[]`；
- PR5 中所有 task 都必须是 independent task；
- PR5 是 all-or-nothing：如果 independent batch 不能全部立即创建并进入 scheduled/start，则整个 call fail-fast，不创建半批；
- PR5 不引入排队/容量等待状态；`queued_capacity` / `waiting_capacity` 留给未来 scheduler；
- task 失败后不 reset、不 retry、不 redefine；如果 parent 认为值得再试，应创建新的 task，并把旧 task 的 failure/result/artifact 作为 context。

`task_key` 是一次 `create_agent_tasks` call 内的稳定引用名，供 PR7 dependency 引用。它必须在本次 call 内唯一，并且不能使用 `task:` 前缀。`label` 只用于展示，不参与依赖解析。

#### `wait_agent_tasks`

以 task 为单位等待。

输入：

```json
{
  "task_ids": ["task:..."],
  "settle": "all",
  "timeout_seconds": 60
}
```

settled 语义：

- `completed` 算 settled；
- `failed` 算 settled；
- `cancelled` 算 settled；
- `blocked_dependency_failed` 算 settled；
- `running` 不算 settled；
- `waiting_dependency` 不算 settled；

规则：

- `settle=all` 等所有目标 task settled 或 timeout；
- `settle=first` 返回第一个 settled task；
- timeout 返回 partial result；
- timeout 不取消仍在跑的任务；
- consumption marker 只写已返回 result；
- repeated wait 不重复消费已 consumed result，除非显式 `include_consumed=true`。

#### `stop_agent_task`

停止一个逻辑 task。

- 如果 task 有 active child run，则取消当前 `subagent_run_id`；
- task 进入 cancelled；
- 被取消的 task 保持 cancelled history；如果 parent 之后想继续，应创建新的 task。

### 5.4 Child-only report tools

这些工具只暴露给 child runtime。

#### `report_agent_phase`

提交阶段/进度，不结束 child run。

#### `report_agent_result`

提交 explicit result，并结束当前 child run 的业务结果。

这里的“结束”应是 graceful terminal：runtime 写 structured result，并允许必要的收尾事件落库。不要把它实现成粗暴 kill。

explicit result 优先级高于 final assistant text。final assistant text 只能作为 fallback / inferred result：

```text
result_source = explicit | inferred
```

执行语义必须钉死：

1. child 调用 `report_agent_result`；
2. runtime 写 `SubagentResultSubmittedEvent`；
3. tool result 返回给 child model；
4. `AgentRuntime` 在 after-tool-results safe point 发现 explicit result；
5. child run 转为 terminal/completed；
6. runtime 不再继续发起 child follow-up model call；
7. 如果之后仍出现 final assistant text，只能作为 diagnostic / transcript fact，不得覆盖 explicit result。

事件顺序也必须稳定：

- `SubagentResultSubmittedEvent` 必须先于 `SubagentRunCompletedEvent` / `SubagentTaskCompletedEvent`；
- completion event 必须引用同一个 `result_id`；
- inspector/projection 不应出现“run completed 但 result 尚未出现”的中间状态。

### 5.5 暂不进入最小组的工具

以下工具重要，但不进入最小组：

- `send_agent`
- `followup_agent`
- `cancel_tree`

原因是 task identity、result report、consumption marker、dependency scheduler 尚未稳定。过早暴露这些工具会让模型多交一份“接口选择税”。

以下工具不是“暂缓”，而是明确不进入模型可见产品语义：

- `retry_agent`
- `retry_agent_task`
- `reset_agent_task`
- `redefine_agent_task`

如果 parent 需要重新探索，应创建新 task。旧 task 的失败、取消、blocked 和产物继续作为 evidence 存在。

---

## 6. Typed events / DTO vocabulary

### 6.1 核心 DTO

```python
SubagentTask
  task_id: str
  batch_id: str | None
  create_tool_call_id: str | None
  task_key: str | None
  label: str | None
  profile_id: str
  display_role: str | None
  objective_preview: str
  status: created | waiting_dependency | running | blocked_dependency_failed | completed | failed | cancelled
  depends_on: tuple[str, ...]  # resolved task_id list; never labels
  current_run_id: str | None
  has_child_run: bool
  phase: str | None
  result_id: str | None
  primary_result_artifact_id: str | None  # projection-only, derived from SubagentResult
  created_at / updated_at / completed_at

SubagentRun
  subagent_run_id: str
  task_id: str | None
  batch_id: str | None
  create_tool_call_id: str | None
  run_index: int | None  # V1 task-backed run uses 1; no task-level retry semantics
  parent_runtime_session_id: str
  child_runtime_session_id: str
  parent_run_id: str
  spawn_initiator_kind: tool_call | scheduler | dependency_satisfied
  spawn_initiator_id: str
  profile_id: str
  context_policy: isolated | fork
  status: running | completed | failed | cancelled
  result_id: str | None
  result_source: explicit | inferred | none

SubagentResult
  result_id: str
  task_id: str | None
  subagent_run_id: str
  summary: str
  result_source: explicit | inferred
  artifact_ids: tuple[str, ...]
  error_message: str | None

SubagentConsumption
  consumption_id: str
  consumer_tool_call_id: str
  kind: wait_run | wait_task
  task_id: str | None
  subagent_run_id: str | None
  result_id: str | None
  consumed_status: completed | failed | cancelled | blocked_dependency_failed
  terminal_event_id: str | None
  diagnostics: tuple[dict[str, object], ...]
```

`create_tool_call_id` 与 spawn initiator 的关系：

- `create_tool_call_id` 表示创建 task batch 的 parent tool call；
- `spawn_initiator_kind` 表示当前 child run 的真实启动来源：`tool_call | scheduler | dependency_satisfied`；
- `spawn_initiator_id` 是该启动来源的稳定 id；
- PR5 immediate-start 时通常是 `spawn_initiator_kind="tool_call"`，且 `spawn_initiator_id == create_tool_call_id`；
- PR7 dependency delayed-start / scheduler-start 不得伪造 tool call id；必须使用 `spawn_initiator_kind` / `spawn_initiator_id` 表达真实来源。

DTO denormalization invariant：

- `SubagentTask.primary_result_artifact_id` 是 projection-only 字段，必须由 result artifact policy 从 `SubagentResult.artifact_ids` 中选择；不得把 `artifact_ids` 的顺序本身当作 primary 语义，除非该 policy 明确规定顺序含义；多 artifact / primary artifact 语义以 `SubagentResult` + result artifact policy 为准；
- `SubagentRun.result_source` 是 projection convenience；`result_id != None` 时必须等于对应 `SubagentResult.result_source`，`result_id == None` 时必须为 `none`；
- task-backed `SubagentRun.batch_id` / `create_tool_call_id` 必须等于 owning task 的 creation attribution；
- 重新探索必须创建新 task；不得通过覆盖旧 task attribution 来伪装 reset/redefine。

`SubagentConsumption` 字段 invariant：

- `kind == wait_run` 时 `subagent_run_id` 必填；
- `kind == wait_task` 时 `task_id` 必填；
- `task_id` 与 `subagent_run_id` 不能同时为空；
- `result_id is None` 时，`terminal_event_id` 必填；
- `consumed_status == completed` 时原则上必须有 `result_id`；
- 如果出现 completed-without-result，必须带 explicit diagnostic，并且 projection 不得把它伪装成正常 completed result；
- diagnostic 的承载位置应是 `SubagentResultConsumedEvent.diagnostics[]`，或 projection-level diagnostic；不得塞进 human message 或含糊的 reason string。

这些 DTO 可以先作为 projection DTO，不一定新增独立表。Pulsara 的事实源仍应是 typed events。

### 6.2 Task-level events

最小事件 vocabulary：

- `SubagentTaskCreatedEvent`
- `SubagentTaskScheduledEvent`
- `SubagentTaskStartedEvent`
- `SubagentTaskBlockedEvent`
- `SubagentTaskCompletedEvent`
- `SubagentTaskFailedEvent`
- `SubagentTaskCancelledEvent`
- `SubagentResultConsumedEvent`

事件可以在实现时适当合并，但必须表达以下事实：

- task 已创建；
- task 因依赖 blocked；
- task 开始某个 child run；
- task 进入 terminal status；
- task 当前 child run id；
- task/run linkage history；
- wait consumption。

PR5 起，task created/scheduled/started/terminal events 至少必须携带：

- `batch_id`
- `create_tool_call_id`

task-backed `SubagentRunStartedEvent` 也必须带同一组归因字段。这样 post-commit repair 后，inspect 可以稳定重建“同一批创建后统一 terminalized”的事实链。

PR7 起，dependency blocked 事件还必须携带可重算的 blocker facts：

- `blocked_reason="dependency_failed"` 或 `blocked_reason="waiting_dependency"`；
- `blocked_by_task_ids`；
- `dependency_status_snapshot`，记录阻塞发生时每个 dependency 的状态；
- `dependency_terminal_event_ids`，仅对 failed/cancelled 等终态 dependency 填充；
- `dependency_generation` 或等价版本号，用于审计该 blocked fact 对应的 dependency snapshot。

这些字段不是为了实现 retry/reset，而是为了让 parent、inspector 和用户知道下游为什么没有启动。失败依赖本身就是 evidence，不应被静默吞掉。

### 6.3 Report / result events

Report/result events 不是 task-only。primitive `spawn_agent` 不创建 `task_id`，但 child 仍然应该可以 `report_agent_phase` / `report_agent_result`。

- `SubagentPhaseReportedEvent`
- `SubagentResultSubmittedEvent`

冻结规则：

- `subagent_run_id` 必填；
- `task_id` 可为空；
- primitive run 的 report 进入 run projection；
- task-backed run 的 report 同时更新 task projection。

### 6.4 Run-level events

保留当前 runtime graph events：

- `SubagentRunStartedEvent`
- `SubagentRunCompletedEvent`
- `SubagentRunFailedEvent`
- `SubagentRunCancelledEvent`
- `SubagentMessageSentEvent`
- `SubagentResultDeliveredEvent`

`SubagentResultDeliveredEvent` 仍只表示 model-visible delivery，不表示 wait tool 返回。

### 6.5 Consumption facts 与 wait edge

当前 primitive `wait_agent` 已经通过 `SubagentEdgeRecordedEvent(edge_kind="wait")` 表达 run-level wait/consumption。新增 task layer 后不能制造第二套互相冲突的事实源。

冻结规则：

- `wait_agent(subagent_run_id=...)` 继续写 run-level wait edge；
- `wait_agent_tasks(task_ids=...)` 写 `SubagentResultConsumedEvent`；
- projection 将二者 normalize 成同一种 `consumed_by_wait` 状态；
- 同一次 wait 不应同时写 wait edge 和 `SubagentResultConsumedEvent`，除非明确一个是 graph edge、一个是 task consumption fact，并且两者用同一个 `consumption_id` 关联；
- `SubagentResultDeliveredEvent` 不表示 wait consumption，只表示模型可见 delivery。
- 对 failed / cancelled / blocked_dependency_failed 这类没有 result 的终态，`SubagentResultConsumedEvent.result_id` 可以为空，但必须记录 `consumed_status` 和 `terminal_event_id`。

### 6.6 Dependency failure 语义

上游失败时，下游默认进入：

```text
blocked_dependency_failed
```

不要直接把下游标记为 terminal failed。

PR7 只负责记录足够的 blocker facts，不负责暴露 retry/reset 能力。换句话说：

- PR7 可以把下游从 `waiting_dependency` 推进到 `blocked_dependency_failed`；
- PR7 可以在 dependency satisfied 后自动启动仍处于 `waiting_dependency` 的下游 task；
- PR7 不提供 `retry_agent`、`reset_task`、`redefine_task` 或任何 public retry tool；
- PR7 不把已经 `blocked_dependency_failed` 的下游自动恢复；
- parent 可以消费这个 settled blocked fact，然后亲自验证或创建新的 task。

因此，`blocked_dependency_failed` 是“当前 graph 中已确定无法继续启动的 blocked 状态”，不是 terminal failed，也不是等待自动恢复的 pending 状态。这个状态必须保留 dependency blocker facts，方便 parent 后续亲自处理或创建新 task。

---

## 7. Built-in profiles

先内置 3 个 canonical profiles。

### `research_worker`

用途：只读调查。

允许：

- read-only filesystem tools；
- search tools；
- artifact_read；
- docs MCP tools。

禁止：

- terminal write-ish operations；
- file write/edit；
- memory tools；
- subagent tools。

### `review_worker`

用途：只读 code review / design review。

允许：

- read_file；
- search_files；
- artifact_read；
- maybe inspect read-only projection。

输出 contract：

- prioritized findings；
- file/line if available；
- severity；
- actionable recommendation。

### `verification_worker`

用途：验证改动。

允许：

- read_file；
- search_files；
- terminal；
- terminal_process；
- artifact_read。

禁止：

- write_file；
- edit_file；
- memory tools；
- subagent tools。

---

## 8. Parent context compiler

保留 `subagent:results`，但它应服务 task/run 分层：

```text
<subagent_results>
  <result task_id="task:..." run_id="subagent_run:..." profile="review_worker" status="completed">
    summary...
    artifact: ...
  </result>
</subagent_results>
```

规则：

- wait-consumed result 默认不再自动注入；
- delivered event 只在实际 model call started 后写；
- result section 是 internal/handoff section，不是 user message；
- context pressure 下可降级为 result summary + artifact ref；
- compile 成功但 provider call 未开始，不写 delivered；
- compile 失败不写 delivered。

---

## 9. 推荐 PR 顺序

### PR0：术语、契约、事件 vocabulary

冻结：

- `task_id` = 逻辑任务身份；
- `subagent_run_id` = 一次 child runtime run；
- `run_index` = task-backed child run 的序号；V1 通常为 1，不代表产品级 retry；
- `profile` = canonical execution contract；
- `role` = display/compat；
- child report 是 typed event，不是普通 text；
- delivered event 仍只表示进入实际 parent model-call payload。

补文档/契约：

- task/run/edge/result/delivery 的关系；
- `spawn_agent` vs `create_agent_tasks`；
- wait consumption marker；
- child report event；
- list/status projection shape。
- subagent system tools 始终在 exposure 中可见；
- 非 bypass 下 descriptor 不隐藏，执行时由 permission gate deny；
- deny reason code = `subagent_requires_bypass_mode`。
- PR2–PR7 每新增一个 subagent system tool，都必须添加同类 gate regression：non-bypass deny + reason_code，bypass allow path。

### PR1：`list_agents` + unified projection

先做只读 observability。

实现：

- `list_agents` tool；
- PR1 阶段只投影 primitive runs；
- 输出 shape 预留 task 字段：`item_kind="run"`、`task_id=null`、`run_index=null`；
- 对当前已有 `SubagentRun` 也能返回合理状态；
- bounded 输出；
- 不泄漏 child raw transcript。

PR2 引入 task identity 后，projection 再增加 `item_kind="task"`。这样 API shape 从 PR1 起稳定，不会 PR2 再整体改一次。

验收：

- 非 bypass 下 `list_agents` 不返回 projection，而是 gate deny；
- `list_agents` 的 non-bypass deny reason_code = `subagent_requires_bypass_mode`；
- bypass 下 `list_agents` 返回 bounded projection；
- spawn 后 list 显示 running；
- complete 后 list 显示 completed；
- wait 后 list 显示 consumed；
- delivered 后 list 显示 delivered；
- Postgres-backed projection 可用。

### PR2：Task identity substrate

新增 task-level typed events / DTO。

必须能表达：

- created；
- blocked dependency；
- running child run；
- terminal status；
- current_run_id；
- task/run linkage history。

验收：

- task 可存在但没有 child run；
- task start 后关联 `subagent_run_id`；
- task 失败/取消后保持 immutable；再次探索必须创建新 task；
- primitive run 可在 unified projection 中作为 run-only item 出现。

### PR3：child-only report tools

新增：

- `report_agent_phase`
- `report_agent_result`

规则：

- 只在 child runtime exposure 中出现；
- parent runtime 不暴露；
- `report_phase` 写 phase event；
- `report_result` 写 explicit result，并 graceful 结束 current child run；
- final text fallback 标记为 `result_source="inferred"`。

验收：

- child phase 出现在 `list_agents`；
- explicit result 优先于 final text；
- wait 返回 explicit result；
- child raw events 仍只在 child runtime session。

### PR4：Built-in profiles

新增 canonical profiles：

- `research_worker`
- `review_worker`
- `verification_worker`

profile 解析为：

- system prompt supplement；
- allowed tools；
- permission constraints；
- output contract；
- diagnostics/version。

验收：

- research/review 无 terminal/write；
- verification 可 terminal/terminal_process，但无 write；
- memory tools 永远不进 child；
- profile 不是模型自由 allowlist。

### PR5：`create_agent_tasks`

实现高层 task creation 的 independent batch 版本。

输入：

```json
{
  "tasks": [
    {
      "task_key": "review",
      "label": "review",
      "profile": "review_worker",
      "task": "..."
    }
  ]
}
```

语义：

- 创建 `task_id`；
- PR5 不开放 `depends_on`；
- 如果 schema 保留 `depends_on`，则必须为空，否则 fail-fast；
- 所有 task 都是 independent task；
- PR5 是 all-or-nothing：如果任何 task 因并发/权限/配置限制无法立即进入 scheduled/start，则整个 call fail-fast，不创建半批；
- PR5 不引入 `queued_capacity` / `waiting_capacity`；
- `task_key` 只在当前 request 内稳定，用于返回结果映射和未来 PR7 dependency 引用；它必须唯一，且不能以 `task:` 开头；
- `label` 只用于展示，不参与依赖解析。

原子边界：

- PR5 必须先做 capacity / profile / permission / config preflight validation；
- `batch_id` 在 tool call validation 通过后、slot reservation 前生成；
- preflight failure 的 tool result 可以包含 `batch_id` 作为 non-durable correlation id，但 event log 不写任何 task/run facts；
- structured batch failure 的最小字段为：`batch_id`、`error_code`、`failed_stage`、`failed_task_keys`、`diagnostics`；
- `failed_stage` 至少包含 `preflight | post_commit_start`；
- preflight 必须先完成 slot reservation，确认所有 task 都可以启动；
- preflight failure 不得写 task/run 事件，tool result 直接返回 structured batch failure；
- pre-commit 任何失败都必须释放 reservation，并且不得启动 child；
- 只有全部可启动时，才进入 post-commit 阶段；
- post-commit 阶段顺序固定为：`reservation -> event_log.extend(batch facts) -> start children -> repair if needed`；
- `event_log.extend(batch facts)` 至少写 task created/scheduled/started 与 run started 事件；
- child runtime start 发生在 event log batch commit 之后，因此不要求与数据库事件批写处于同一事务；
- 如果启动阶段仍发生 partial failure，runtime 必须结构化 cancel 已启动 child，并写一致的 failed/cancelled 事件；
- post-commit partial failure 必须将整批已 materialized tasks 统一修复到 terminal cancelled/failed，不返回可继续等待的 task ids；
- repair events 必须引用同一个 `batch_id` / `create_tool_call_id`，让 inspect 能解释“为什么这一批创建后立刻取消”；
- post-commit batch failure 的 tool result 必须返回 structured batch failure；
- 禁止静默留下“部分 task 已创建、部分 child 已运行”的半批状态；
- 如果已经 post-commit materialized，不删除已写事件；必须用同一 `batch_id` / `create_tool_call_id` 写 terminal repair facts。

验收：

- 一个 tool call 创建多个 tasks；
- 无依赖 task 并发启动；
- 非空 `depends_on` 在 PR5 fail-fast；
- preflight 阶段任何一个 task 无法启动时整个 batch fail-fast，且不写 task/run 事件；
- post-commit 启动失败时，不留下部分 running/scheduled task；如已 materialized，则整批以同一 batch failure 归因进入 terminal 状态；
- 成功时返回 task ids 和已启动 child run ids；
- preflight/post-commit batch failure 时返回 structured batch failure，不返回可继续等待的 task ids。

### PR6：`wait_agent_tasks(settle=all|first)` + `stop_agent_task` 基础版

实现 task-level wait 和基础 task cancellation。

规则：

- `settle=all` 等所有目标 task settled 或 timeout；
- `settle=first` 返回第一个 settled task；
- timeout 返回 partial；
- timeout 不取消 child；
- consumption marker 只写已返回 result；
- failed/cancelled/blocked_dependency_failed 的 settled 语义固定进 schema。

`stop_agent_task` 基础语义：

- 如果 task 有 active child run，则取消当前 `subagent_run_id`；
- task 进入 `cancelled`；
- PR6 不处理 dependency side effects；
- dependency waiting / failed-blocking 主干留到 PR7；
- 不做 downstream reset / unblock；如果 parent 仍需要后续工作，应创建新 task。

验收：

- wait all 返回多个 result；
- wait first 只消费第一个 result；
- timeout 不取消 child；
- repeated wait 不重复消费已 consumed result，除非显式 `include_consumed=true`。
- stop active task 会取消 active child run 并把 task 标记为 cancelled。

### PR7：depends_on minimal scheduler

把 dependency 变成真实调度语义。

PR7 的边界必须刻意收窄：它只实现 dependency scheduler 主干，不实现 retry/reset 产品层。失败依赖会阻塞下游；parent 之后可以亲自验证或创建新 task，但旧 graph 不自动修复。

实现：

- dependency graph validation；
- self-dependency/cycle fail-fast；
- dependency satisfied 后自动 schedule/start 仍处于 `waiting_dependency` 的下游 task；
- upstream failed/cancelled -> downstream `blocked_dependency_failed`；
- 记录 blocker facts，解释下游为什么没有启动；
- 不暴露 retry/reset tool；
- 不把 `blocked_dependency_failed` task 自动 reset。

明确不属于 PR7 的内容：

- `retry_agent` / `retry_agent_task` / `reset_agent_task` / `redefine_agent_task`；
- retry requested / reset requested / dependency blocker superseded 事件；
- upstream retry 成功后自动 cascade unblock；
- child run result supersede、旧 result invalidation、retry artifact lineage；
- parent 模型手动修改已 blocked task 的 objective/profile。

如果 parent 需要重新尝试，应通过 `create_agent_tasks` 创建新 task。新 task 可以引用旧 task 的 failed/cancelled/blocked facts、result artifact 或 diagnostic，但不能覆盖旧 task。

dependency identity 规则：

- `label` 只用于显示，永远不能作为 dependency identity；
- `task_key` 必须在同一个 create call 内唯一，且不能使用 `task:` 前缀；
- `depends_on` 中以 `task:` 开头的字符串按真实 `task_id` 解析；
- `depends_on` 中不以 `task:` 开头的字符串按同 call sibling `task_key` 解析；
- 同一个 `create_agent_tasks` call 内，`depends_on` 可以引用 sibling `task_key`；
- 跨 call 引用必须使用真实 `task_id`；
- PR7 开始正式开放 `waiting_dependency`、auto schedule/start、`blocked_dependency_failed`；
- PR7 不开放 retry/reset/cascade unblock；blocked facts 只用于解释、审计和后续新 task 的 context。

PR7 示例：

```json
{
  "tasks": [
    {
      "task_key": "review",
      "label": "Review",
      "profile": "review_worker",
      "task": "Review the patch."
    },
    {
      "task_key": "verify",
      "label": "Verify",
      "profile": "verification_worker",
      "task": "Run tests after review.",
      "depends_on": ["review"]
    }
  ]
}
```

验收：

- B depends on A，A 未完成时 B 无 child run；
- A 完成后 B 自动启动；
- A failed/cancelled 后 B 进入 `blocked_dependency_failed`；
- B 的 blocked event/projection 包含 `blocked_by_task_ids`、dependency status snapshot、upstream terminal event id；
- invalid dependency ref / self-dependency / cycle 均 fail-fast，且不启动任何 child；
- PR7 不验收 “retry A 成功后 B 可启动”；Pulsara 不提供这种 graph mutation。

## 10. 和 Deno / WorkflowScript 的关系

Deno / WorkflowScript 不应进入 PR1-PR7 主线。

原因：

1. 当前最缺的是 runtime task semantics，不是脚本语言。
2. 如果 runtime 语义没钉好，workflow script 会绕过事实源。
3. Tanzo 证明：不写脚本也能获得高价值 multi-agent 编排。
4. Codex 证明：thread/session tree、canonical task path、fork policy、mailbox wait 比 workflow language 更底层。
5. Claude Code 证明：脚本可以作为高级 authoring surface，但事实源仍必须归 runtime。
6. OpenClaw 证明：workflow runtime 应拥有状态和 wait/resume，业务分支逻辑可以在外层。

未来如果引入 Deno，它的位置应是：

```text
Deno WorkflowScript
  -> calls Pulsara task DAG APIs
  -> emits/uses Pulsara typed events
  -> never owns child state
  -> never owns result delivery
  -> never bypasses context compiler
```

如果 DAG 复杂到必须用脚本表达，也应先反问：

> 这个任务是不是应该拆成几个用户可见阶段，而不是让模型一次写完整 workflow？

---

## 11. 风险点

### 11.1 工具面太多，模型学不会

必须在 prompt/tool description 中强制区分：

```text
低层 primitive：
  spawn_agent / wait_agent / stop_agent

高层 task workflow：
  create_agent_tasks / wait_agent_tasks / stop_agent_task / list_agents

child-only：
  report_agent_phase / report_agent_result
```

默认建议：

- 单个临时 worker/debug 场景，用 primitive；
- 正常 orchestrator 工作流，用 task layer；
- child 只能 report，不能 create/wait parent tasks；
- parent 不应调用 child-only report tools。

### 11.2 Task/run 双身份可能让 projection 复杂

这是必要复杂度。它比把 `depends_on`、phase/result、wait consumption 全塞进 `subagent_run_id` 更可控。

### 11.3 Result 自动注入可能重复消费

必须保留 durable consumption marker：

- wait-consumed result 不再默认进入 `subagent:results`；
- delivered result 不重复进入；
- compile/provider failure 不写 delivered。

### 11.4 Capability profile 不能让模型手写 allowlist

parent 可以选 profile，但最终 allowed tools 必须由 runtime 计算。

### 11.5 Context pressure 下 result section 要可降级

长 child result 应进入 artifact，parent context 中保留 summary + artifact ref。

### 11.6 DAG 复杂度不可失控

V1 默认限制：

- max depth = 1；
- 单次 `create_agent_tasks` batch 上限较小，例如 4；
- parent run 总 task 上限较小，例如 16；
- `depends_on` 只允许引用同一 request 内的 `task_key`，或同一 parent run 内已经存在的真实 `task_id`；
- child 默认不能再 spawn child；
- 不支持 script loop 自动扩图。

### 11.7 V1 不做 nested pending

pending/approval routing 会碰：

- HostSession multi-pending；
- MCP input-required；
- plan question；
- approval scope；
- child resume；
- parent 自身 pending。

V1 直接不做这条路径。child 内部一旦触发这些 pending 类能力，runtime 应 fail-closed，并把原因写成 diagnostic / denied tool result。这样 subagent runtime 可以先保持单向、可审计、无嵌套交互的形态。

---

## 12. 建议下一步

下一轮最自然、收益最高的一步仍然是：

> PR1：实现 `list_agents` + unified subagent/task status projection。

理由：

1. 它不改变 child 执行语义，风险低；
2. 它能立刻改善 parent orchestrator 的掌控感；
3. 它是 task identity、batch DAG、bypass-only control gate 的前置能力；
4. 它能复用现有 typed events；
5. 它把 Pulsara 的 graph-first 优势暴露给模型和用户。

但在真正编码 PR1 之前，PR0 必须先完成术语和契约冻结：

```text
Task = 逻辑工作单元
Run = 某次 child runtime run
Profile = canonical execution contract
Report = child 显式产物
Consumption = wait 返回过 result
Delivery = parent model-call 实际看见 result
```

这个顺序比先做 WorkflowScript 更稳，也比继续把 `spawn_agent` 做胖更稳。它保留 Codex-like runtime substrate，同时给 Pulsara 长出 Tanzo-like task board 的空间。
