# Approval Resume Survey

Pulsara 的目标不是一个 memory demo，而是 universal desktop agent。Memory 只是最早开发的一个 feature。站在这个目标上，approval resume 不是“安全弹窗”的附属品，而是本地桌面 agent 的运行时交互原语：模型提出一个有副作用的动作，runtime 暂停，用户批准或拒绝，然后同一个任务继续往前走。

本文调研 Anybox 的 approval 实现，并对照 Pulsara 当前代码，明确哪些经验应吸收，哪些不必照搬。

## 1. 为什么 approval resume 是产品级能力

本地 desktop agent 和普通 CLI 脚本的差别之一，是它天然会遇到“需要用户裁决但不该丢任务上下文”的时刻：

- 删除、覆盖、安装、运行外部命令、访问第三方系统。
- 用户开启 full access 作为一等模式，但仍希望少数灾难动作有硬拦截。
- 用户没有开启 full access，agent 需要请求一次性授权。
- 手机端、桌面端、浏览器扩展、后台自动化都需要把 pending 状态展示出来，而不是让模型在下一轮猜测发生了什么。

所以 approval resume 至少要做到：

1. pending request 是 runtime 可见状态。
2. 用户批准或拒绝后，原 tool call 的命运被记录。
3. 批准后执行原 tool call，而不是让模型重新生成一次。
4. 拒绝后把 denial 作为 tool result 回到模型上下文，让模型可以解释、换方案或停下。
5. 整个过程不写入 memory graph，也不把审批状态伪装成长期事实。

## 2. Anybox 的参考实现

Anybox 在这块比 Pulsara 当前成熟。它的实现不是只有一个 permission enum，而是有完整的 pending/resolution 生命周期。

### 2.1 Permission request 是产品对象

Anybox 定义了 `permission_requests` 和 `permission_audits`。request schema 包含：

- `id`
- `approvalID`
- `sessionID`
- `messageID`
- `toolCallID`
- `projectID`
- `tool`
- `toolKind`
- `risk`
- `status = pending | approved | denied | expired`
- `input`
- `resource`
- `prompt`
- `runtime`
- `resolution`

代码位置：

- `/Users/plumliu/Desktop/python_workspace/anybox/packages/anyboxagent/src/permission/schema.ts`
- `/Users/plumliu/Desktop/python_workspace/anybox/packages/anyboxagent/src/permission/permission.ts`

这说明 Anybox 把 approval 当成 UI/API 可以查询、过滤、审计的运行时对象，而不是一条临时字符串。

### 2.2 Tool 可以参与权限描述

Anybox 的 tool runtime 支持：

- `assessPermission`
- `describeApproval`
- `validate`
- `authorize`
- `toModelOutput`
- structured output / attachments

代码位置：

- `/Users/plumliu/Desktop/python_workspace/anybox/packages/anyboxagent/src/tool/tool.ts`

这让每个 tool 可以生成更具体的 approval prompt。例如文件写入可以展示 path，shell 可以展示 command，MCP tool 可以展示 server/tool 名称。

### 2.3 Pending request 会阻断当前 turn

Anybox 在 `registerApprovalRequest()` 中：

- 检查 tool part 是否处于 `waiting-approval`。
- 重新计算 permission decision。
- 构造 prompt snapshot 和 runtime snapshot。
- 写入 `permission_requests`。
- emit `permission.requested`。
- finish managed turn，状态为 `blocked`，finish reason 为 `tool-approval`。

代码位置：

- `/Users/plumliu/Desktop/python_workspace/anybox/packages/anyboxagent/src/permission/permission.ts`

这和 Pulsara 当前 `RequireUserConfirmEvent` 的方向一致，但 Anybox 多了 request 存储和后续 resolution。

### 2.4 Approve 后执行原 tool call

Anybox 的 `resolveRequest()` 会：

- 把 pending request 更新为 approved 或 denied。
- emit `permission.resolved`。
- 若 approved，找到原 `waiting-approval` tool part。
- emit `tool.call.approved`。
- 调用 `completeApprovedRequest()` 执行原 tool runtime。
- 更新原 tool part 为 `completed` 或 `error`。
- 若 denied，更新原 tool part 为 `denied`。

关键代码：

- `completeApprovedRequest()`：`/Users/plumliu/Desktop/python_workspace/anybox/packages/anyboxagent/src/permission/permission.ts`
- `resolveRequest()`：同文件
- API route：`/Users/plumliu/Desktop/python_workspace/anybox/packages/anyboxagent/src/server/routes/permissions.ts`

测试也覆盖了 approve 后 tool part completed、deny 后 tool part denied、approve 后执行失败则 tool part error：

- `/Users/plumliu/Desktop/python_workspace/anybox/packages/anyboxagent/Test/permission.api.test.ts`

### 2.5 Anybox 的取舍

值得借鉴：

- request 是明确对象。
- approval prompt 与 runtime input 分开。
- approve 后执行原调用，不让模型重猜。
- deny 后也回写 tool state。
- 有 audit 表。
- API 支持 list/get/approve/deny/resolve。

不必照搬：

- V1 不一定需要先做 SQLite/Postgres permission table。Pulsara 当前 HostCore 仍是进程内 session registry，先做 host-session scoped resume 更符合现状。
- V1 不必立即做“allow session/project/forever”规则持久化。Anybox 的 schema 也把旧的 allow-once/session/project/forever 规约为 allow，Pulsara 可以先只做一次性 approve/deny。
- V1 不应把 approval 写入 memory graph。approval 是 runtime control plane，不是长期记忆。

## 3. Pulsara 当前状态

Pulsara 已经有 approval resume 的部分地基，但还不是闭环。

### 3.1 已有的地基

`PermissionDecisionKind` 已有：

- `ALLOW`
- `DENY`
- `WAIT_FOR_USER`

代码：

- `src/pulsara_agent/runtime/permission.py`

事件层已有：

- `RequireUserConfirmEvent`
- `UserConfirmResultEvent`
- `ConfirmResult`

代码：

- `src/pulsara_agent/event/events.py`

消息 reducer 已经能处理 `UserConfirmResultEvent`：

- confirmed -> `ToolCallState.ALLOWED`
- denied -> `ToolCallState.FINISHED`

代码：

- `src/pulsara_agent/message/reducer.py`

runtime timeline 已经会把 `RequireUserConfirmEvent` 投影为 `permission_request` item：

- `src/pulsara_agent/runtime/timeline.py`

### 3.2 当前断点

`AgentRuntime._execute_tool_blocks()` 在 permission gate 返回 `WAIT_FOR_USER` 时：

- 构造 `ToolCallBlock(state=ASKING)`。
- 设置 `state.status = LoopStatus.WAITING_USER`。
- 设置 `state.stop_reason = "waiting_user"`。
- emit `RequireUserConfirmEvent`。
- return。

代码：

- `src/pulsara_agent/runtime/agent.py`

但目前没有生产者会 emit `UserConfirmResultEvent`。`UserConfirmResultEvent` 只是 schema 和 reducer 分支存在。

同时，`HostSession.run_turn()` 每次都会：

- 从 transcript 重建 prior messages。
- `agent_runtime.new_state()`。
- 调用 `run_task()`。

代码：

- `src/pulsara_agent/host/session.py`

这意味着如果当前 turn 等用户审批，下一次普通 user turn 会新建 `LoopState`，不会自动恢复 pending tool call。

### 3.3 当前策略为何禁用 ask/on_request

Pulsara permission V1 已经明确定义：

- `TerminalAccess.ASK`
- `ApprovalPolicy.ON_REQUEST`

但 resolver 会拒绝会产生实际 `WAIT_FOR_USER` 的组合：

- `terminal_access=ask`
- write/terminal 可用时的 `approval_policy=on_request`

原因是 approval resume 还没实现。

代码：

- `src/pulsara_agent/runtime/permission.py`

这个禁用是正确的。否则用户一旦触发 approval，就会进入不可恢复的 halt。

## 4. 对 Pulsara 的设计启发

### 4.1 Approval resume 属于 Host/Runtime，不属于 LLM adapter

不应该放在：

- provider adapter
- retry helper
- system prompt composer
- memory governance
- graph store

它应该放在：

- `AgentRuntime`：懂得如何暂停和继续 tool execution loop。
- `HostSession`：拥有 pending approval 状态和用户交互入口。
- `HostCore`：为桌面/web/CLI 暴露 list/resolve API。

### 4.2 Approval 是 runtime control plane，不是 memory

approval 可以被 runtime timeline 展示，可以进入 event log，可以产生 audit trail，但不应该进入 memory graph。

理由：

- 用户批准 `rm -rf build` 不等于“用户长期偏好删除 build”。
- 用户拒绝一次 command 不等于“以后永远拒绝这个工具”。
- approval 关心的是当前 tool call 的执行权，而 memory 关心的是可沉淀的事实、偏好、决策、边界。

### 4.3 V1 应先闭环，再扩策略空间

Pulsara 当前已经会在 `trusted_host + risky_only + terminal=allow` 下对 risky terminal 返回 `WAIT_FOR_USER`。这条路径今天已经可能撞到不可恢复的暂停。

因此 V1 优先级应是：

1. 让现有 risky terminal approval 可恢复。
2. 保持 `terminal_access=ask` 和 write/terminal `on_request` 暂时禁用。
3. 等 resume 路径稳定后，再开放这些更频繁触发 approval 的策略组合。

### 4.4 V1 不必一开始 durable

Anybox 的 request 是 SQLite-backed。Pulsara 未来作为 universal desktop agent 也需要 durable approval store，尤其是后台自动化、移动端远程审批和应用重启恢复。

但当前 Pulsara HostCore 的 session registry 是 in-memory，workspace supervisor 也是进程内生命周期。因此 V1 可以先做 host-session scoped in-memory approval resume：

- host 进程在，pending approval 可恢复。
- host/session 关闭，pending approval 失效。
- durable permission store 作为 V2。

这和 `HOST_CORE_AND_INMEMORY_SUPERVISOR_PLAN.zh.md` 的“先做 InMemory soft recovery”方向一致。

## 5. Recommended Shape

Pulsara V1 应采用：

```text
model emits tool call
  -> permission gate returns WAIT_FOR_USER
  -> AgentRuntime emits RequireUserConfirmEvent
  -> AgentRuntime pauses without final RunEnd
  -> HostSession stores PendingApproval + suspended LoopState
  -> UI/CLI resolves approval
  -> AgentRuntime emits UserConfirmResultEvent
  -> approved calls execute, denied calls become denied tool results
  -> tool results are appended to state.messages
  -> loop continues to next model turn
  -> final RunEnd emitted when task truly finishes/fails/aborts
```

这里的关键是“pause 不是 terminal”。V1 的推荐实现是让 waiting run 真正保持 suspended，直到 approve/deny 后再结束。

V1 不采用 continuation-run fallback。Pulsara 的 reducer 会按 `reply_id` 把 `UserConfirmResultEvent` 投影回原 assistant reply；如果暂停后另开 continuation run，很容易让 approval result 和原 `ASKING` tool call 落在不同 reply 上，trace 语义会碎。V1 的契约应直接固定为 suspended run：暂停时不发 terminal `RunEnd`，批准/拒绝后在同一个挂起状态中继续，真正结束时只发一次最终 `RunEnd`。

## 6. 风险与边界

### 6.1 不要让 approval 变成 rule engine

V1 只做一次性 approve/deny。不要立刻做：

- allow forever
- allow this project
- remember this rule
- prompt 里自动学习 permission rule

这些属于后续 policy/rule store，不是 approval resume MVP。

### 6.2 不要让 user prompt 代替 approval

“用户下一轮说继续”不是 approval resume。真正的 approval resume 必须引用 pending request id 或 pending tool call id，并把 resolution 写成结构化事件。

### 6.3 不要重新生成 tool call

批准后必须执行原 tool call 的 snapshot，而不是让模型重新调用一次。否则用户批准的是 A，模型可能执行 B。

### 6.4 不要跳过 hardline

hardline blocklist 仍是不可批准的底线。approval resume 只恢复 `WAIT_FOR_USER`，不恢复 `DENY`。

## 7. 结论

Anybox 证明了 approval resume 是 desktop agent 产品化的必备能力，而不是可有可无的安全 UX。Pulsara 当前已经有事件类型、消息 reducer、permission gate 和 host session 地基，缺的是“pending request -> user resolution -> continue tool loop”的闭环。

Pulsara 的正确路线不是照搬 Anybox 的完整 permission database，而是先做 host-session scoped suspended-run resume。等这个闭环稳定，再把 approval request durable 化，并开放更广的 `ASK` / `ON_REQUEST` 策略空间。
