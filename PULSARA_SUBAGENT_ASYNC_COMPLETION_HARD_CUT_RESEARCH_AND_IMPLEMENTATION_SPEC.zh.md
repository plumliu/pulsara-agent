# Pulsara 子代理异步完成交付与 Late-Join Hard-Cut 调研及实施契约

> 状态：**READY_FOR_IMPLEMENTATION**
>
> 记录日期：2026-08-31
>
> Pulsara 调研基线：当前工作树 production source；根目录文档仅作次级证据。
>
> Codex 调研基线：本地 `/Users/plumliu/Desktop/python_workspace/codex`，commit
> `fb0781b9eee6`；官方产品说明仅作补充证据。
>
> 本文是下一次 hard-cut 的目标契约，不声称当前代码已经实现。实现激活前，现行 authority
> 仍是 `ROUND_10_HIERARCHICAL_SUBAGENT_ORCHESTRATION_IMPLEMENTATION_SPEC.zh.md` 与 production
> source；实现激活后，本文只替换 Round 10 中关于“ROOT 如何收到 terminal outcome”、
> `wait_agent*`、ROOT completion mailbox、result acceptance 与对应 UI 的条款，其余 task graph、
> dependency、profile、context、permission、capacity、child result 和 restart 边界继续有效。

---

## 0. 一句话结论

Pulsara **已经能够让 ROOT 与多个 worker 真正并行执行**。它之所以在真实模型使用中频繁、
甚至连续调用 `wait_agent`，不是因为 child scheduler 是同步的，也不是因为 wait 在 CPU
busy-poll，而是因为现行契约把两件本应分开的事情绑在了一起：

1. 等待某个异步事实发生；
2. 把 worker 的最终结果送进 ROOT 的模型上下文。

目前 worker 完成后不会主动向 ROOT 投递 terminal completion。ROOT 想在当前回答中使用结果，
只能让模型调用 target-specific `wait_agent` / `wait_agent_tasks`，或者由前端用户显式点击
“带入会话并继续”。因此 wait 既是同步屏障，又是结果运输工具；模型自然会在 spawn 后尽早
wait，以免永远看不到结果。

本次 hard-cut 要把它改成 Codex 风格的 late join：

```text
spawn
  -> worker 在独立 asyncio task 中运行
  -> ROOT 立即继续自己的非重叠工作
  -> worker terminal outcome 自动进入 session-local ROOT completion inbox
  -> ROOT 在下一个合法 provider safe point 接纳 completion
  -> 只有当 ROOT 已无其他有用工作、且当前回答确实依赖未完成 worker 时才调用 wait_agent
```

`wait_agent` 只保留同步/谓词屏障语义，不再返回 task result；terminal completion 才是正常、自动的
结果交付通道。`list_agents` 仍可作为用户明确要求下的诊断查询，但不承担正常编排的数据运输。
worker 成功、失败、取消、中断和依赖失败都走同一条 completion 路径，
从而保证“某个子代理失败”不会再天然演化成“主 Agent 没有自然语言回答”。

---

## 1. 证据优先级、范围与明确非目标

### 1.1 证据优先级

本调研按以下顺序判断“现有实现是什么”：

1. 当前 production source、clean-v0 schema、实际 tool descriptor 与测试；
2. 当前已激活规格；
3. 根目录 README、历史规格与营销文案；
4. 根据 UI 或模型行为做出的推断。

README 融合了新旧内容，不能覆盖源码事实。Codex 也以本地源码为主；
[OpenAI Subagents 文档](https://learn.chatgpt.com/docs/agent-configuration/subagents)仅用于确认
“主任务汇总结果、子任务保留独立工作区/线程、并行减少主上下文噪声”的产品方向。

### 1.2 本轮要解决的问题

- ROOT spawn worker 后，如何在 worker 运行期间继续独立工作；
- worker terminal 后，如何无需 target-specific wait 就让 ROOT 得到结果；
- `wait_agent` 如何从 result transport 降级为真正的 late-join barrier；
- worker failure 如何成为 ROOT 可理解的输入，并促使 ROOT 正常收尾；
- user steer、tool group、provider streaming、answer boundary 与 completion 如何线性化；
- 成功与失败、活跃态与空闲态、自动与手动接纳如何共享一个 canonical writer；
- 当前 batch、DAG dependency、四 worker physical capacity、profile、context、message、stop、
  explicit/inferred result 与 session-bound UI 如何全部保留；
- 如何在不引入 restart 自动续跑、delivery job、ack graph 或第二套 compiler 的前提下完成 hard-cut。

### 1.3 明确非目标

本轮不做：

- child 继续 spawn child；Pulsara 仍是 `one ROOT -> many worker leaves`；
- child 向 ROOT 的任意 mid-flight progress message；先只统一 terminal completion；
- ROOT 主动轮询 child 的近期 assistant message 或 tool trace；这些只供 UI 观察；
- terminal task 的 `followup_task` 式复活；新工作仍创建新 task；
- 进程重启后的 child 自动续跑、completion queue replay 或 ROOT 自动续写；
- 新增 durable waiter、delivery job、receipt、lease、checkpoint、reducer 或 repair graph；
- Git 集成；
- 把 provider/internal protocol 名称、JSON envelope 或错误码直接展示给前端用户；
- 通过修改 README 来代替实现和激活证据。

---

## 2. Pulsara 当前实现：它已经异步在哪里

### 2.1 产品拓扑与持久事实

现行 Round 10 使用固定两层拓扑：一个 ROOT 管理多个 leaf worker。核心 durable truth 是：

```text
subagent_tasks
  objective / profile / context policy / parent_turn_id
  current status / pending_reason / terminal_reason

subagent_task_dependencies
  ROOT 创建的 task DAG direct edges

subagent_task_children
  MESSAGE：ROOT -> active worker 的已接纳消息
  RESULT：COMPLETED task 的唯一 explicit 或 inferred result

Turn(scope = SUBAGENT_TASK(task_id))
  worker 的 canonical model/tool transcript
```

task 状态覆盖：

```text
PENDING_START
WAITING_DEPENDENCY
ACTIVE
COMPLETED
FAILED
INTERRUPTED
CANCELLED
BLOCKED_DEPENDENCY_FAILED
```

`COMPLETED` 必须具有唯一 result；其他 terminal 状态通过 task row 的 `terminal_reason` 表达，
目前没有 result row。这一点正是“失败无法沿 result acceptance 进入 ROOT”的结构性原因。

### 2.2 process-local coordinator

`KernelSubagentManager` 是一个 HostSession 内唯一的物理调度 owner。它持有：

- live child `asyncio.Task`；
- start material、launch permit 与 cancellation intent；
- worker mailbox 与 message ordinal；
- dependency frontier 与 batch admission；
- condition/revision 驱动的状态唤醒；
- 当前 HostSession 内四 worker physical concurrency admission。

四 worker 是**每个 HostSession**的物理并行上限，不是全进程共享槽位，也不是 task 总量上限。
多出的 logical tasks 进入
`PENDING_START` / `WAITING_DEPENDENCY`，不会被拒绝。task 历史由数据库分页读取，live carrier
在 terminal 后退休，不随历史无限增长。

### 2.3 spawn 本身不等待 worker 完成

`spawn_agent` 复用单元素 `create_agent_tasks` admission。`_start_available_tasks_worker()` 为
可运行 task 创建独立 `asyncio.Task`，`_run_child()` 在该 task 内运行 child runner。

因此：

- spawn tool 只等待 canonical admission 和可能的物理启动，不等待 child final；
- ROOT tool call 返回后会继续自己的 provider loop；
- 多个 child 可在容量内同时运行；
- ROOT 也可在 child 执行期间继续调用工具、思考和产生文本。

从调度角度看，Pulsara 已具备真正的并发。需要 hard-cut 的不是 scheduler，而是 ROOT input
delivery。

### 2.4 child lifecycle 与结果

`_run_child()` 调用 `ConversationKernelRunner.run_admitted_subagent_turn()`。child 有自己的 cold
epoch、BASE_SYSTEM、provider tool surface 与 canonical transcript；它不是 ROOT provider thread 的
复制。

结果有两个 producer shape：

- explicit：worker 以 sole-call `report_agent_result` 提交结构化 summary；
- inferred：worker 直接输出 final assistant text，runtime 从 final 生成同形 result。

两者原子收口为 `COMPLETED + RESULT`。dependency scheduler 会把 direct prerequisite 的 bounded
summary 自动加入下游 worker 的 `DEPENDENCY_RESULTS`，因此 worker-to-worker DAG data edge 已经
自动化。

失败、取消、中断和依赖失败只收口 task status/reason，不产生 RESULT。

### 2.5 ROOT -> worker message

`send_agent_message` 将 ROOT 消息放入 process-local child mailbox。child runner 在下一次 model
call 前的 safe point 调用 `consume_mailbox_safe_point()`，把消息 canonicalize 为 child-scope
`INTER_AGENT_MESSAGE`，再由统一 compiler lower 为 user-role typed envelope。

这条路径已经证明 Pulsara 具备正确的异步输入模式：

```text
process-local signal
  -> provider-safe boundary
  -> canonical transcript append
  -> unified compiler
  -> provider user-role non-human envelope
```

缺失的是同样结构的反方向 terminal completion。

### 2.6 当前 ROOT 观察/接纳结果的三种方式

现行实现只有：

1. `list_agents`：读取 task 状态与 bounded result summary；
2. `wait_agent` / `wait_agent_tasks`：等待指定 task，并把 terminal result 放进 ToolResult；
3. 前端/controller 发出 `ACCEPT_SUBAGENT_RESULT`：把一个已完成 result 作为 ROOT
   `USER_MESSAGE + source_subagent_result_id` 接纳，必要时创建新 ROOT turn。

第三条已经进入统一 structured prompt compiler：reader 将它标为 `SUBAGENT_RESULT`，lowerer
将其包装成 `pulsara_subagent_result` user-role envelope。它不是绕过 compiler 的 provider call。

但是这条 acceptance 目前是显式外部命令，不由 child completion 自动触发；而且只接受
`COMPLETED` 的 result，无法表达 FAILED/CANCELLED/INTERRUPTED/BLOCKED。

### 2.7 当前 wait 不是 busy-poll

`_wait_for_tasks()` 每轮读取 canonical task state；未满足 settle 条件时，它在
`asyncio.Condition` 上等待 state revision 变化或 timeout。它不会占满 CPU。

用户观察到的“一直 wait”指模型不断选择 wait tool 或一次长时间停在 wait tool，不是 runtime
用 tight loop 轮询数据库。根因是产品契约和 tool affordance，而非 condition implementation。

### 2.8 当前前端

前端已经可以按 session 读取完整 subagent task 历史、显示 task 状态/详情，并把 process-local
live progress 叠加到 durable rows。它还提供“带入会话并继续”一类显式 result acceptance。

这一 UI 把 runtime 缺失的自动交付暴露给了用户：用户必须替 Agent 完成 routing。失败 task
又没有 result 可带入，因此容易出现“child 出问题，ROOT 也没有自然语言收尾”的断层。

---

## 3. Codex 当前实现：它如何做到经常异步

### 3.1 spawn 是 dispatch，不是 join

Codex Multi-Agent V2 的 `spawn_agent` 调用 `spawn_agent_with_communication()` 创建 child thread，
等待的是创建/启动 admission，然后立即把 task name 返回给 parent。它不等待 child completion。

tool 描述明确要求：只有当子任务可以与当前本地工作独立并行时才 spawn；child 的 final answer
完成后会自动提供给 parent。这个表述让模型知道 spawn 后无需立刻调用 wait 才能“取回”结果。

### 3.2 child terminal 自动投递到 parent mailbox

child 到达 final status 后，`forward_child_completion_to_parent()` 构造标准
`InterAgentCommunication`：

```text
kind = Result
message type = FINAL_ANSWER
sender = child agent path
recipient = parent agent path
trigger_turn = false
```

成功携带 child final message；error/shutdown/not-found 也会形成可理解的 terminal envelope。
`trigger_turn=false` 很关键：结果会入 parent session mailbox，但不会因为 parent 当前 idle 就擅自
创建新 turn。

### 3.3 session-scoped InputQueue

Codex 的 `InputQueue` 同时接收 mailbox communication 和 user steer，并用一个 activity watch 唤醒
等待者。mailbox 是 session-scoped，而不是绑定某次 wait call；因此 completion 可以先于 wait、
晚于当前 provider call，或者在 parent 做其他工作时到达。

在下一次 model sample 前，pending input 被 drain 到 history。inter-agent completion 最终也是
user-role 输入，但通过 typed non-human envelope 与真实 human intent 区分。

### 3.4 answer boundary

Codex 不会让一个 `trigger_turn=false` 的 late child result 在 parent 已经给出 final answer 后偷偷
追加一次采样：

- 若当前 model step 还需要 follow-up，例如产生了 tool call，mailbox 可在当前 turn 的下一步进入；
- 若当前 model step 已形成 answer boundary，只有 queue-only child mail 时，delivery 延后到下一
  turn；
- pending user input 可按 Codex 自己的 task phase 保持/重开 mailbox delivery；这不等价于 Pulsara
  已 durable terminal 的 turn，Pulsara 的更严格映射在 §8.6 单独定义；
- idle session 不因 queue-only completion 自动启动。

这使“自动交付”和“禁止后台自说自话”同时成立。

### 3.5 V2 wait 等待 level-triggered activity，不运输结果

Codex V2 `wait_agent`：

- 不接收 target task id；
- 订阅 mailbox/steer activity；
- 先订阅 activity，再同时 level-check pending user input 与 mailbox；先到的活动也会立即返回；
- 返回值只有 wait completed / interrupted by input / timed out；
- 不承载 child result content。

于是 result transport 与 synchronization 完全分离。parent 通常采用：

```text
spawn sidecar
  -> 做本地非重叠工作
  -> completion 自动到达
  -> 仅在最终 join point 尚缺结果时 wait 一次
```

Codex 不是通过频繁查看 child 当前 assistant message 或近期 tool 来获得异步性。UI 可以展示独立
child thread，parent model 默认收到的是显式 mid-flight message 或 terminal final answer，而不是
自动轮询 child trace。

### 3.6 哪些 Codex 设计不直接复制

Pulsara 不复制：

- recursive agent tree；
- terminal child 的 follow-up/restart；
- 完整 thread fork；
- Codex rollout/history 的具体存储结构；
- 把 child worktree 或 Git 状态作为 subagent contract。

Pulsara 要复制的是更小、也更关键的四件事：session mailbox、terminal auto-delivery、
answer-boundary fence、level-triggered wait。Codex V2 的 descriptor 倾向一次较长等待；“非常少用
wait”是异步交付与工作拆分的结果，不是 runtime 机械禁止调用 wait。

---

## 4. 为什么 Pulsara 模型会一直 wait

这是四层契约共同造成的稳定行为。

### 4.1 Tool descriptor 直接把 wait 描述成结果读取 API

当前 `spawn_agent` 描述要求模型复制 `task_id` 到 `wait_agent`；当前 `wait_agent` 描述承诺“等待
并返回状态和最终结果”；`wait_agent_tasks` 又提供 `settle=all|first`。对模型而言，最可靠的计划
自然是：

```text
spawn -> wait -> 得到结果 -> 回答
```

“先做其他有用工作”的一句软提示无法抵消结果只从 wait 返回的硬事实。

### 4.2 Round 10 明确禁止自动进入 ROOT

现行规格把 result 定义为 task output，不是发给 ROOT 的消息；ROOT 只能显式 list/wait/accept。
实现忠实遵守了该约束，所以缺少自动通知并不是偶发 bug，而是旧产品契约的直接结果。

### 4.3 completion 没有 ROOT input queue

child 有接收 ROOT message 的 mailbox，ROOT 没有接收 child terminal outcome 的对称 mailbox。
`_run_child()` terminal 后只做：

- durable result/status 确认；
- live progress projection；
- dependency frontier settlement；
- process-local carrier retirement。

它不会唤醒 ROOT input，也不会让 ROOT runner 下一次 compile 看到 completion。

### 4.4 失败路径甚至没有可 accept 的 result

成功 task 至少还能由用户点击带入；FAILED/CANCELLED/INTERRUPTED/BLOCKED 没有 RESULT row，
现有 acceptance writer 无法表达它。若 ROOT 没有通过 wait ToolResult 亲自看到失败，就没有一个
model-visible trigger 要求它恢复、降级或解释。

### 4.5 现有 dogfood 强化了 barrier-first 行为

当前 Round 10 real-provider dogfood 的 graph/capacity 场景明确提示模型“使用
`wait_agent_tasks` 直到 terminal”。这些用例能证明 DAG、capacity 和 result projection，却不能
证明 async ROOT work；它们反而把 wait-first 当成正确答案。

---

## 5. 目标产品契约

### 5.1 核心语义

每个 terminal subagent task 都产生一个面向 ROOT 的 **terminal completion**。completion 是
task terminal truth 的一次 ROOT-visible canonical projection，不是第二份 task result authority。

```text
SubagentTask terminal truth
  -> process-local completion hint (task_id only)
  -> next legal ROOT safe point
  -> one canonical ROOT-scope INTER_AGENT_MESSAGE
  -> unified structured compiler
  -> provider user-role FINAL_ANSWER envelope
```

成功 completion 引用 task 的唯一 result summary；非成功 completion 引用 task status、closed
reason code 与可公开诊断。两者具有相同 routing、same safe point、same exact-once acceptance 和
same UI state。

### 5.2 late join

ROOT 的默认行为必须是：

1. 只把可独立并行的 bounded sidecar 派给 worker；
2. spawn 返回后立即继续自己的非重叠工作；
3. completion 到达后在下一合法 safe point 自动进入上下文；
4. 有 completion 就综合；有 failure 就恢复、降级或明确说明；
5. 只有当前回答确实依赖仍未 terminal 的 worker，且已无其他有用工作时，才调用一次
   `wait_agent`；
6. 不以短 timeout 反复轮询。

### 5.3 不自动创建 ROOT turn

terminal completion 固定 `trigger_turn=false`：

- active ROOT 若仍需要 follow-up，可在当前 turn 消费；
- active ROOT 已越过 answer boundary，则留到下一 turn；
- idle ROOT 不自动启动；
- process restart 不重建 pending completion queue；
- 用户可在 UI 显式选择“用这份结果继续”，该动作才允许创建新 ROOT turn。

### 5.4 所有 terminal 状态对称

以下状态都必须形成 completion candidate：

| task status | ROOT completion content |
| --- | --- |
| `COMPLETED` | task identity、profile/label、result source、self-contained summary |
| `FAILED` | task identity、失败状态、terminal reason、继续主任务的提示 |
| `CANCELLED` | task identity、取消状态与原因 |
| `INTERRUPTED` | task identity、中断状态与原因 |
| `BLOCKED_DEPENDENCY_FAILED` | task identity、依赖失败状态与原因 |

ROOT 收到任何非成功 completion 后仍必须继续自己的 turn，并给用户自然语言结论；除非 ROOT
自身被用户停止、provider 不可恢复失败或会话关闭。

---

## 6. Authority 与 durability

### 6.1 Authority 分层

| 事实 | 唯一 authority | 可否丢失 |
| --- | --- | --- |
| task status/reason/public failure detail | `subagent_tasks` | 否 |
| completed result | 唯一 `subagent_task_children.RESULT` | 否 |
| ROOT 已接纳 completion | ROOT transcript 中 source task 唯一的 `INTER_AGENT_MESSAGE` | 否 |
| 尚待投递的 wakeup/顺序 | HostSession-local completion inbox | 进程退出可丢失 |
| UI live progress | process-local projection | 可丢失 |

completion inbox 只保存 `task_id + process-local ordinal`，不复制 result body，不拥有 terminal
truth。drain 时必须重新读取 canonical task/result。

### 6.2 不新增 durable 机制类别

本轮：

- 不新增 relation；
- 不新增 committed event type；
- 不新增 subject slot；
- 不新增 append guard；
- 不新增 durable job；
- 不新增 delivery/ack table。

ROOT completion 复用既有 `INTER_AGENT_MESSAGE` entry kind 与
`InterAgentMessageAccepted` event。为表达反方向 lineage，clean-v0 将现有
`source_subagent_result_id` hard-cut 为 `source_subagent_task_id`。

为使失败在 reconnect 后仍可解释，允许在既有 `subagent_tasks` row 上 hard-cut 增加
`terminal_public_detail`，并把 `terminal_reason` 收紧为 closed reason code。它是 terminal task
truth 的必要字段，不是新 relation、event、job、receipt 或 replay mechanism；其 UTF-8 上限与现有
result summary 的 `16,384` bytes 对齐，理由是两者都会成为一次 provider-visible completion 的
主要正文。只移除 `PULSARA_API_KEY` 的实际值，不做宽泛诊断遮蔽。

### 6.3 exact-once 的含义

数据库对 `(session_id, source_subagent_task_id)` 建立唯一约束。一个 task 的 terminal completion
最多进入 ROOT transcript 一次，不论 winner 是：

- active-turn automatic drain；
- idle UI 显式“用这份结果继续”；
- ACK unknown 后的 retry。

process-local duplicate offer 不需要永久 dedupe registry。drain 重查 canonical entry：已接纳则
退休 hint；未接纳则尝试唯一 writer。automatic path 用 deterministic entry identity、source-task
unique constraint 与 source query 完成 ACK-unknown confirmation，**不写内部 `session_commands`
receipt**。

UI manual path 是公开 command，因此无论它创建 entry，还是输给已完成的 automatic writer，都必须
在同一事务绑定 client `command_id` 与 exact request semantics。相同 command 重试返回同一
disposition；同一 command ID 改投另一 task 必须 conflict。writer 返回 closed disposition：
`CREATED | ALREADY_DELIVERED | TARGET_STALE`。

### 6.4 不做 restart replay

Host close 继续 terminalize 非 terminal workers。pending completion hint 随进程丢失可接受：

- 已经进入 ROOT transcript 的 completion 是 durable；
- 未进入 ROOT 的 terminal task/result 仍可由 session task sidebar 查询；
- reconnect 后不自动启动 ROOT、不自动续跑 child、不扫描数据库重建 completion inbox；
- 用户可显式选择某个未交付 terminal task，让统一 completion writer 创建新 ROOT turn。

---

## 7. Canonical schema hard-cut

### 7.1 ROOT-scope `INTER_AGENT_MESSAGE`

现有 `INTER_AGENT_MESSAGE` 只允许 `ROOT -> SUBAGENT_TASK`。hard-cut 后它是一个 closed union：

```text
MESSAGE
  scope = SUBAGENT_TASK(task_id)
  source_inter_agent_tool_attempt_id != null
  source_subagent_task_id = null

FINAL_ANSWER
  scope = ROOT
  source_inter_agent_tool_attempt_id = null
  source_subagent_task_id != null
```

不开放 arbitrary child-to-ROOT message；ROOT-scope variant 只能由 terminal task state 构造。

### 7.2 transcript 字段

clean-v0：

- 删除 `source_subagent_result_id`；
- 新增 `source_subagent_task_id`，FK 到 `subagent_tasks`；
- 唯一约束改为 `(session_id, source_subagent_task_id)`；
- source task 仅允许 ROOT-scope `INTER_AGENT_MESSAGE`；
- child-scope `INTER_AGENT_MESSAGE` 继续要求 source tool attempt；
- protocol projection 的 `result_accepted` 改为 `completion_delivered`，通过 source task join 推导。

成功 completion 的 result lineage 通过 task 的唯一 RESULT join 获得，无需把完整 frozen result
再冗余复制成 DTO fingerprint 或第二个 FK。

### 7.3 storage body

ROOT completion entry 保存一个完整、冻结、可独立读取的 typed body：

```json
{
  "schema_version": "pulsara.subagent-completion.v1",
  "message_type": "FINAL_ANSWER",
  "task_id": "...",
  "task_key": "cap4",
  "label": "检查第四个场景",
  "display_role": "...",
  "profile": "general_worker",
  "status": "COMPLETED",
  "failure": null,
  "result": {
    "result_id": "...",
    "source": "EXPLICIT",
    "summary": "self-contained bounded summary"
  }
}
```

非成功状态的 `result` 为 `null`，并携带 closed failure object：

```json
{
  "code": "DEPENDENCY_FAILED",
  "detail": "上游任务未能完成，因此本任务没有启动。",
  "failed_dependency_task_ids": ["..."],
  "retryability": "NEW_TASK_ONLY | USER_ACTION_REQUIRED | NOT_APPLICABLE | UNKNOWN",
  "next_action": "检查上游失败；必要时以新任务重试。"
}
```

`code/detail` 来自 task 的 durable terminal truth；dependency IDs 由 completion writer transaction
exact 查询既有 immutable edge 与 terminal status 后冻结；`retryability/next_action` 由 closed code
映射生成。child exception/provider failure
必须保存实际、可操作的公开 detail，不能只压成 `CHILD_<TYPE>`。provider-visible body 只带完成任务
所需的 self-contained summary 或 failure object；output preview 与其他 diagnostics 继续在 task
detail/list 查询中可见，不把无关诊断噪声自动塞进 ROOT 上下文。

### 7.4 command hard-cut

`ACCEPT_SUBAGENT_RESULT(child_result_id=...)` 替换为：

```text
ACCEPT_SUBAGENT_COMPLETION(task_id, target_turn_id?, requested_permission_mode?)
```

同一 core writer 同时服务：

- runtime automatic safe-point acceptance；
- UI idle explicit acceptance；
- ACK-unknown confirmation。

automatic path 不创建 command row；它使用由 `task_id + target_turn_id` 稳定派生的 entry identity，
并以 source-task unique constraint 决胜。UI path 使用 client command identity，即使返回
`ALREADY_DELIVERED` 也 durable bind 到该 winner entry。唯一 source-task constraint 决定最终
winner，command semantic binding 决定 UI retry 的幂等。

manual idle acceptance 以 ROOT-scope `INTER_AGENT_MESSAGE` 作为新 ROOT turn 的 initial entry。clean-v0
必须同步更新 initial-entry closed constraint、turn admission DTO、reader/activation contract 与
compaction request-like classifier；该 input origin 是 non-human，不得误触 human prompt memory、hook
或 human-intent activation。`RunPermissionAdmissionSource.EXTERNAL_RESULT_COMMAND` 同步 hard-cut 为
`SUBAGENT_COMPLETION_COMMAND`，不保留 alias。

不保留旧 command alias、旧 result-id 参数、dual read 或 compatibility branch。

---

## 8. ROOT completion inbox 与线性化

### 8.1 owner

每个 `KernelHostSession` 拥有一个 process-local `RootInputQueue`，而不是全进程唯一 inbox。
不同 session 的 ROOT 与 workers 可继续并行；修复单个 session 的 completion routing 不得恢复成
全局单 active HostSession。

`RootInputQueue` 持有：

- FIFO pending terminal task IDs；
- pending task-id set，仅覆盖当前队列；
- monotonic process-local activity revision；
- `asyncio.Condition` 或等价通知；
- completion pending level；activity revision 同时由 completion offer 与已 durable accepted 的 user
  steer 推进。Host close 通过现有 turn/task cancellation 退出 wait，不伪装成 input activity。

它不持有 result body、provider handle、canonical entry 或 durable delivery state。

### 8.2 offer point

每条 terminalization path 必须在 durable terminal winner 已 FULL 确认后调用同一个
`offer_subagent_completion(task_id)`：

- explicit result；
- inferred result；
- child exception；
- user cancellation；
- Host interruption；
- dependency failure propagation；
- launch/admission 后的失败收口；
- ACK-unknown 确认得到 historical terminal winner。

不得在 result/status transaction 前 offer，也不得让 `_run_child()` 的某个异常分支遗漏通知。

这里不是一句抽象回调要求，而是三条可执行算法：

1. batch admission FULL 后，对每个 `draft.initial_status.terminal` 的 task 重新读 canonical row 并
   offer；terminal draft 本来就不会成为 `_TaskStartMaterial`，不能等待 `_run_child()` 补发；
2. dependency frontier 正常返回时，在 retire carrier 前 offer 每个 terminal
   `changed_task_id`，包括 transitive `BLOCKED_DEPENDENCY_FAILED`；
3. frontier transaction commit 但 ACK unknown 时，不能相信 retry 返回的空 `changed`。对该次
   causal frontier 的 exact dormant candidate IDs 做 canonical terminal confirmation，逐个 offer 后再
   retire。这个 bounded same-process confirmation 不是 restart replay，也不扫描全历史。

### 8.3 ROOT safe point

completion **不得**由 runner 在 loop 顶部独立 drain。现有 steer acceptance、resource quote、
compaction 与 provider handle 已由 `provider_dispatch.prepare()` 共同持有；另开 writer 会反转输入
顺序，或写穿 immutable compaction successor。

hard-cut 将现有 `PreparedSteerSuffixAdmissionPlan`/`SteerConsumptionCoordinator` 提升为唯一的
`PreparedRootPendingInputSuffixPlan`/coordinator。它只在普通 dispatch path 执行，并在一次 plan 中：

1. 冻结 exact ROOT turn、canonical base fence 与 completion inbox cut；
2. 读取该 exact active turn 的 durable pending steer lane；
3. 以固定 lane 顺序规划 suffix：先按 `queue_sequence` 排列 human steers，再按 process-local offer
   ordinal 排列 completions。两个 lane 没有伪造的跨源 wall-clock 全序；
4. hydrate task/result/failure，使用**同一个** structured compiler、token quote 与 resource admission
   选择最大合法 suffix；human steer 保留现有 rejection/compaction 语义，未选中的 completion 留在
   inbox；
5. 在 exact base CAS 下按计划顺序 canonicalize；steer 使用现有 consume authority，completion 使用
   无 internal command 的 core writer；
6. ACK unknown 时按 steer command/source-task entry 分别确认，随后从新 canonical base 重新 prepare；
7. 重读 canonical snapshot，验证 exact suffix 后才安装新的 provider-input handle。

completion 的 delivery target 是 plan 时该 session 的 exact active ROOT turn，不强制回写它的
`parent_turn_id`；后者只保留 task origin/grouping。若 session 此时没有 active ROOT turn，则不运行
plan，保持 idle pending，等待下一 user turn 或 manual action。

如果任一步在首个 write 前 stale，则整份 plan 作废并重建；若 ACK unknown，禁止盲目重放。不要为
完整 frozen plan 再创建 fingerprint registry：现有 exact turn/base/source identity 与 typed values
已经足够。

合法普通边界是：

- 没有 installed/open provider operation；
- 没有未闭合 assistant tool request；
- 上一 tool batch 的所有 ToolResult 已 canonical commit；
- 没有 compaction write fence；
- target ROOT turn 仍为 exact RUNNING owner；
- 尚未提交 answer boundary。

completion 不得插入 assistant tool request 与其 ToolResult 之间，也不得修改已冻结 provider input
cut。若 `successor_dispatch is not None`，该 immutable compaction successor 必须原样消费；本轮禁止
pending-input plan，steer/completion 都留到 successor call 结束后的下一个普通 safe point。

### 8.4 到达时序

| completion 到达时机 | 行为 |
| --- | --- |
| 普通 provider prepare 前 | 进入统一 pending-input plan；若被 resource admission 选中则进入本次 call |
| provider call/stream 中 | 只入 process-local queue；当前 call 不变 |
| ordinary tool batch 中 | 等整组 ToolResult commit 后接纳 |
| `wait_agent` 正在等待 | 唤醒 wait；tool closure 先 commit，下一 safe point 再接纳 |
| model 已产生 tool call | tool follow-up 使当前 turn 继续，下一 safe point 接纳 |
| model 已提交自然语言 answer boundary | 不扩展当前 turn，留给下一 turn |
| ROOT idle | 不启动 turn；留在 inbox，UI 可显式继续 |
| Host closing | 不创建新工作；queue 可丢失，durable task truth 保留 |
| compaction successor 已冻结 | 不修改 successor；下一普通 safe point 再规划 steer/completion |

### 8.5 bounded operation，不设历史总上限

pending-input writer 可用固定小 batch 做单次数据库事务，但必须对 frozen cut 继续 prepare，直到它
为空、answer boundary 关闭本 turn，或 provider resource admission 要求先 compaction。不得因为一次
batch bound 丢弃或拒绝 logical completion；后续新到达项留给下一 safe point。单个 completion 在
compaction successor 后仍无法进入 provider minimum context 时，走现有 typed resource-boundary
outcome 并保留 undelivered truth/UI action，不凭空截断或标为已交付。

不设置 session lifetime completion 总数、task 总数或 ROOT turn 总数上限。

### 8.6 answer-boundary fence

ROOT runner/HostSession 必须为 exact `turn_id` 共享一个 `mailbox_delivery_phase` 或等价 closed state：

```text
CURRENT_TURN_OPEN
NEXT_TURN_ONLY
```

- tool call / model-needs-follow-up 保持 `CURRENT_TURN_OPEN`；
- 无-tool assistant settlement **之前**先切换为 `NEXT_TURN_ONLY`；
- settlement 若返回 `pending_steer_at_settlement=true` 且 exact turn 仍 RUNNING，才切回
  `CURRENT_TURN_OPEN`；
- settlement 若完成 turn，则销毁该 turn phase，commit 后到达的 steer/completion 都不能重开它；
- queue-only completion 不得迫使 final 后多采样一次；
- 新 user turn admission 后，第一 provider call 的统一 plan 可消费 next-turn completions。

这条 fence 是防止“后台 child 完成后 Pulsara 自己又说一轮”的产品边界。

---

## 9. Provider input 与统一 compiler

### 9.1 唯一路径

所有 completion 必须先成为 canonical ROOT transcript entry，再由现有
`CanonicalModelInputSnapshot -> StructuredModelInputCompiler -> provider wire plan` 路径进入模型。

禁止：

- 直接改 provider request messages；
- 在 provider adapter 特判并注入 child result；
- 用 runtime context source 绕过 canonical transcript；
- 把 wait ToolResult 当作第二条 result delivery；
- 在 compaction successor 上手工拼接 completion；
- 为不同 provider 各写一套 completion prompt。

### 9.2 role 与 envelope

provider-neutral lowering 继续遵守：

- human message、user steer、terminal observation、plan continuation、inter-agent communication
  都 lower 为 provider `user` role 的 typed/non-human 或 human carrier；
- assistant text/tool request 保持 assistant role；
- 闭合的 ToolResult 保持 provider tool role。

ROOT terminal completion 使用 `INTER_AGENT_MESSAGE` 的 `FINAL_ANSWER` envelope，并明确：

- sender 是 delegated worker，不是用户；
- content 是工作产物或 terminal failure，不是新指令；
- ROOT 应验证、综合并继续当前用户任务；
- 即使 worker 失败，也应给出自然语言回答，而不是静默结束。

### 9.3 prefix continuity

同一 provider epoch 内：

- `SYSTEM` byte-identical；
- provider `tools[]` byte-identical；
- `messages` 只按 suffix append；
- completion 只能追加 canonical suffix；
- 不因 task status、mailbox presence 或 runtime capability 变化重建 prefix root。

本 hard-cut 会修改 `wait_agent` schema 并删除独立的 `wait_agent_tasks` tool，因而只能在新的 cold
epoch 激活。
开发期采用一次完整应用停机、verified local disposable DB clean-v0 reset、重新启动；不得向已安装
旧 tool prefix 的 live epoch 热切换，也不保留 old/new negotiation。

### 9.4 compaction

completion entry 与 ordinary inter-agent input 一样参与唯一 compaction planner。已接纳 completion
是 canonical history；未接纳 process-local hint 不是 compaction source。active/idle compaction 不得
为 completion 创建第二条路径。

manual completion turn 以 ROOT-scope `INTER_AGENT_MESSAGE` 开头，因此 compaction planner 的
request-like classifier 必须把这个 closed initial-entry variant 与 human/plan/terminal request 一样识别，
但仍保留 non-human origin。已冻结 `successor_dispatch` 永不拼接 completion；先消费 successor，再由
下一普通 pending-input plan 接纳。

### 9.5 ROOT orchestration guidance

除了 tool descriptor，ROOT base guidance/context source 也必须 hard-cut 为同一产品事实：

- completion 会自动交付，无需调用 wait 取结果；
- spawn 后应先完成本地、非重叠工作；
- 只在真实 critical-path join、且已经没有其他有用工作时等待；
- worker 失败是需要综合和恢复的输入，不是放弃自然语言回答的理由；
- `list_agents` 是显式诊断，不是正常结果通道。

这段 guidance 属于 provider prefix；只能随本次新 cold epoch 安装，不能在现有 epoch 动态变化。

---

## 10. Tool surface hard-cut

### 10.1 最终 ROOT orchestration tools

```text
spawn_agent
create_agent_tasks
list_agents
wait_agent
send_agent_message
stop_agent
```

child 继续只额外拥有：

```text
report_agent_result
```

删除独立的 `wait_agent_tasks` tool，但把它有价值的 multi-target predicate barrier 合并进
`wait_agent`。batch/DAG 能力没有删除：`create_agent_tasks`、dependency scheduler、multiple
terminal completions、`list_agents` 与 `all|first` join predicate 全部保留；被删除的是重复 tool
surface 和“用 wait ToolResult 搬运结果”的旧交付方式。

### 10.2 `spawn_agent`

descriptor 必须说明：

- 只派发能与 ROOT 当前工作独立并行的 bounded task；
- 返回 task ID/status 后 child 在后台继续；
- final outcome 会自动送回当前 conversation；
- 不要为了取结果立即 wait；
- 当下一关键步骤立即依赖该工作时，ROOT 通常应本地完成，不应把 critical path 委托出去。

不得再写“copy task_id into wait_agent”。task ID 仍用于 list、message、stop 和 UI 定位。

### 10.3 `create_agent_tasks`

保留：

- 原子 batch admission；
- 1..16 task 的现有 per-operation bound；
- direct DAG dependencies；
- independent tasks 并行；
- direct dependency summary 自动投影；
- capacity queueing；
- profile/context/task names。

descriptor 新增：每个 terminal outcome 会独立自动送回 ROOT；不要在创建后立刻
`wait_agent` 形成全局 barrier。

### 10.4 `wait_agent`

新 schema：

```json
{
  "timeout_seconds": 30,
  "task_ids": ["..."],
  "settle": "all | first"
}
```

`task_ids` 可省略；若提供，沿用现有 `1..16` per-operation bound、拒绝重复/跨 session task，
`settle` 默认 `all` 且仅在提供 task IDs 时合法。两种模式共享一个 implementation：

- 无 targets：等待当前 ROOT 有 input level 可处理；
- 有 targets：等待 exact canonical predicate 达成，或 user input 先到而需要中断 join。

它不返回 task result body：

```json
{
  "outcome": "input_available | predicate_satisfied | nothing_pending | timeout",
  "satisfied_task_ids": ["..."],
  "pending_task_ids": ["..."]
}
```

语义：

- pending completion、exact-turn durable pending steer 或已满足 task predicate 存在时立即返回；
- 无 targets、没有 pending input 且不存在任何 nonterminal worker 时，立即返回 `nothing_pending`；
- user steer 到达时中断 predicate join，让 ROOT 优先处理用户输入；
- timeout 只结束本次 wait，不取消 worker；
- result content 在 tool closure 后的下一 ROOT safe point 由 completion envelope 提供；
- task identities/status 只用于表达 join predicate；详情查询使用 `list_agents`；
- tool guidance 要求偏好一次较长 wait，禁止短间隔反复 polling。

wait 必须是 level-triggered，而非只听 edge：

1. 订阅/读取 activity revision；
2. 检查 process-local completion pending level；
3. 查询 exact active turn 的 durable pending steer；
4. 若有 targets，读取其 canonical terminal predicate；
5. 重读 revision；若任一 level 已满足或 revision 改变，立即返回/重试检查；否则才 sleep；
6. wake 后重复上述检查。

这样 steer 在 wait 订阅前 durable 入队也不会丢 wake；已经 canonical delivered、inbox hint 已退休的
target 仍能由 task terminal truth 满足 predicate。activity signal 只负责唤醒，不作为结果或顺序
authority。Host close 依赖既有 cancellation typed outcome，不新增 `host_closing` activity value。

保留现有明确的 per-operation timeout bound；不得把它扩展成 worker lifetime 或 task total bound。
`wait_agent` 从 result hydration tool hard-cut 为 synchronization tool 后，builtin catalog 的
long-horizon classification、schema、descriptor、executor 与测试必须一起更新。

### 10.5 `list_agents`

继续作为分页诊断/恢复接口，返回完整 status、dependency、pending message count、terminal reason 与
bounded result summary。它不改变 delivery state，也不成为“必须先 list 才能收到 completion”的
门禁。

### 10.6 `send_agent_message`

继续只允许 ROOT -> exact ACTIVE worker，在 child safe point canonicalize。terminal task 不复活；
不增加 child -> ROOT mid-flight API。

### 10.7 `stop_agent`

继续覆盖 queued/waiting/active worker。它的 ToolResult 只返回 stop action/status acknowledgement，
不承载另一份 terminal result；terminal completion 由统一 inbox 交付。

### 10.8 permission

现行 fixed tool surface 与 exact permission snapshot 规则继续有效。ROOT orchestration 仍只在
`BYPASS_PERMISSIONS` 成功执行；其他 mode 在 attempt admission 前得到 typed blocked outcome。

由于 tool descriptors 是 provider prefix 的一部分，permission mode 仍不得动态增删工具。

---

## 11. User steer、wait 与 completion 的共同输入活动

### 11.1 一条 activity signal

Host 的 steer admission 与 subagent completion offer 必须通知同一个 process-local input activity
owner。`wait_agent` 订阅该 owner，而不是只订阅 subagent task condition。activity revision 只是
coalescing wake signal；pending steer 的 durable queue 与 completion inbox 才是 level authority。

### 11.2 顺序

provider safe point 的 canonical append 顺序：

1. 在 base freeze 前已经 canonical accepted 的 tool closures、terminal observations 与 recovery
   entries 保留其现有 `entry_sequence`；
2. 统一 pending-input plan 按 durable `queue_sequence` 追加 selected prompt/steer ingress；
3. 同一 plan 按 process-local offer ordinal 追加 selected completion inbox entries；
4. 验证 exact suffix 后安装 handle 并 compile。

若 user steer 与 completion 并发，不靠 wall-clock 猜测，也不比较两个不同排序域；固定 lane priority
就是产品顺序。base freeze 后才到达的输入进入下一 plan。任何未来 ROOT suffix producer 必须在 base
freeze 前完成 canonical write，或显式加入这一个 plan；禁止再出现绕开 arbiter 的第四条 writer。
所有输入均为 suffix。

### 11.3 wait tool group

`wait_agent` 本身仍是 ordinary tool call。completion 唤醒 wait 时：

1. wait 返回不含 result body 的 synchronization ToolResult；
2. ToolResult 先闭合 assistant tool call；
3. runner 进入下一个普通 `provider_dispatch.prepare()`；
4. 统一 pending-input plan canonicalize completion；
5. compiler 生成下一 provider call。

绝不能为了更快交付而把 completion 插进未闭合 tool group。

---

## 12. UI/UX 契约

### 12.1 session-bound task surface

继续删除独立“任务”顶层页面。选中 session 后，右侧当前会话侧栏展示该 session 的完整 task
历史与 live overlay；左侧 session item 只显示简洁聚合。

任务按创建 batch 或 origin ROOT turn 分组，覆盖：进行中、等待依赖、已完成、失败、已取消、
已中断、因依赖失败阻塞。点击 task 可展开 objective、dependencies、recent progress、result/failure
summary，并定位主对话中的 subagent execution block。

### 12.2 自动交付状态

task detail 使用用户语言显示：

- “正在处理”；
- “结果会自动交给 Pulsara”；
- “Pulsara 已收到结果”；
- idle、live overlay 确认仍在 inbox 时：“结果已就绪；发送下一条消息时会一并交给 Pulsara”，
  并提供立即继续动作；
- reconnect 后只有 durable undelivered truth、没有 live hint 时：“结果尚未用于对话”，并提供手动
  继续动作，不虚构自动队列仍在。
- 上述 reconnect 状态若 ROOT 正 active，则说明“本轮结束后可继续处理”；不把 manual action 强塞进
  已安装的 provider cut。

不得向用户显示 `INTER_AGENT_MESSAGE`、`FINAL_ANSWER`、provider safe point、ROOT、kernel、
Terminal Protocol、command kind、schema version 或内部 reason code。公开失败文案由 typed reason
映射成自然语言；详情可保留有用诊断，但不把内部协议名当 UI label。

### 12.3 手动按钮

- active ROOT 且 completion 等待当前 safe point：不显示“带入会话”按钮，避免与自动路径竞争；
- completion 已 canonical delivered：显示只读“已用于对话”；
- ROOT idle 且 terminal task 尚未 delivered：显示“用这份结果继续”；
- failure completion 的对应动作显示“让 Pulsara 处理这个问题”；
- 按钮 hover 解释：会启动新一轮，让 Pulsara 基于该 task 的结果或失败继续，不会重新运行
  worker；
- 点击后调用统一 `ACCEPT_SUBAGENT_COMPLETION(task_id)`，不走前端拼 prompt。

automatic/manual race 由 canonical unique source task 决胜；loser 刷新为“已用于对话”，不弹出
模糊的“当前状态不可用”。

UI projection 的 authority 必须明确分层：

- durable task DTO 顶层提供 `completionDelivered`，由 source-task transcript join 推导，覆盖所有
  terminal status，不再嵌在 success-only `result` 内；
- `completionPendingNow/rootDeliveryPhase` 若展示，只能来自独立、non-fingerprinted、可丢失的 Host
  live overlay，不能塞进 canonical snapshot 充当 durable truth；
- overlay 缺失时，前端只根据 `terminal + !completionDelivered + active/idle ROOT` 保守显示，不声称
  知道 inbox 的 exact 状态；
- manual action 始终发送 `task_id`，由后端 exact revalidation 决定 disposition；failure task 不依赖
  `task.result` 才能操作。

### 12.4 主对话渲染

completion entry 不是用户输入，不渲染成“你”的气泡；也不伪装成 Pulsara 的自然语言回答。
它更新对应 subagent execution group，可按需显示一条简洁的“子任务已完成/失败”状态。真正的
ROOT assistant natural-language synthesis 继续以一个 Pulsara identity block 渲染。

原始 provider envelope 与 storage JSON 永远不进入前端文本。

### 12.5 可观察 child，不等于 ROOT polling

前端可以展示 child 当前 reasoning summary、tool activity、status 与 final output；这是用户观察面。
ROOT model 不因 UI 可见就自动读取 child 全量 trace。terminal summary 仍是 ROOT 默认数据边界。

---

## 13. Race 与 failure matrix

| 场景 | 唯一合法结果 |
| --- | --- |
| child result FULL 后 enqueue 前进程崩溃 | result/task durable；不自动 replay；UI 可手动继续 |
| enqueue 后 ROOT provider 正在运行 | 当前 provider request 不变；下个 safe point 接纳 |
| completion 与 ROOT final 并发 | answer boundary winner 后 completion 留给下一 turn |
| completion 与 user steer 并发 | 同一 activity owner 唤醒；统一 plan 固定 steer lane 后 completion lane |
| automatic 与 UI manual 同时接纳 | source task 唯一约束只允许一个 entry；manual loser 仍绑定 command semantics |
| manual loser 用同一 command ID 重试 | 返回同一 winner/disposition；改投另一 task 明确 conflict |
| duplicate terminal callback | inbox pending set 合并；canonical writer 再保证 exact-once |
| task `FAILED` | 生成 failure completion；ROOT 可恢复并必须自然语言收尾 |
| task `BLOCKED_DEPENDENCY_FAILED` | 不启动 child；仍生成 terminal completion |
| batch admission 直接得到 terminal draft | FULL 后 canonical re-read 并 offer，不依赖 child carrier |
| dependency cascade commit 后 ACK 丢失 | 对 exact causal frontier terminal scan/offer 后再 retire |
| stop pending task | canonical CANCELLED 后生成 completion；不占 child capacity |
| host close | terminalize nonterminal workers；不启动 ROOT；process-local inbox 可丢弃 |
| completion 已 delivered 后重连 | transcript/task query 恢复“已用于对话” |
| wait timeout | worker 继续；ROOT 可继续工作或在最终 join point再 wait |
| unrelated child 先完成并唤醒 targeted wait | 返回 input available；相关 predicate 仍由 canonical task truth 保留 |
| steer 在 wait 订阅前 durable 入队 | level check 立即返回，不等待下一 edge |
| final settlement 与 steer 并发 | 只有 settlement 事务观察到的 pending steer 保持 exact turn open |
| compaction successor 已冻结 | 不修改 successor；completion 留到其后的普通 plan |
| provider/tool error导致 ROOT 终止 | 不强行续 turn；task truth/UI 保留 |

---

## 14. 实施切片

### HC-0：冻结目标规格与 supersession

- 将本文评审后状态改为 `READY_FOR_IMPLEMENTATION`；
- 更新 Round 10 的 supersession 注记；
- 明确删除 automatic-injection prohibition、result-carrying wait 与手动-only acceptance；
- 不在 README 先行宣称实现完成。

### HC-1：clean-v0 schema 与 closed contracts

主要文件：

- `src/pulsara_agent/storage/migrations/sql/0000_conversation_kernel_baseline.sql`
- `src/pulsara_agent/conversation_kernel/contracts.py`
- `src/pulsara_agent/conversation_kernel/subagents/contracts.py`
- `src/pulsara_agent/conversation_kernel/vocabulary.py`
- terminal canonical/projection contracts

工作：

- hard-cut source result lineage 为 source task lineage；
- generalize `INTER_AGENT_MESSAGE` closed union；
- 定义 frozen completion body、closed failure code、`terminal_public_detail` 与 validation；
- command kind 改为 `ACCEPT_SUBAGENT_COMPLETION`；
- ROOT-scope completion initial-entry、non-human origin 与
  `RunPermissionAdmissionSource.SUBAGENT_COMPLETION_COMMAND` 一起 hard-cut；
- 保持 event/subject/relation/job 类别数量不增长；
- 更新 clean-v0 expected catalog；
- verified local disposable DB reset，不写迁移兼容层。

### HC-2：统一 completion writer

主要文件：

- `conversation_kernel/_repository/external_results.py`：删除并以准确命名的 completion module 替代；
- `conversation_kernel/repository.py`
- `conversation_kernel/safe_point.py`
- `conversation_kernel/reader.py`

工作：

- 一个 core repository writer 接受任意 terminal task；
- COMPLETED exact join unique RESULT；其他状态 exact join reason/public detail/dependency cause；
- 写 ROOT-scope `INTER_AGENT_MESSAGE`；
- automatic 不写 internal command；manual 即使是 loser 也 bind client command semantics；
- writer 返回 `CREATED | ALREADY_DELIVERED | TARGET_STALE`，automatic/manual/ACK-unknown 复用；
- reader 产生 typed `FINAL_ANSWER` input；
- 已 delivered query 从 source task join 推导。

### HC-3：RootInputQueue 与 terminal offer

主要文件：

- `conversation_kernel/host.py`
- `conversation_kernel/subagent.py`
- `conversation_kernel/subagents/runtime_port.py`
- `conversation_kernel/subagents/launch.py`

工作：

- 每个 HostSession 安装唯一 ROOT input activity owner；
- 所有 terminal settlement FULL 分支调用统一 offer；
- initial terminal drafts 与 dependency cascade ACK-unknown 使用 exact causal candidate confirmation；
- task-id-only FIFO、pending set、revision/condition；
- durable user steer 通知同一 activity owner；Host close 走现有 cancellation；
- 不把 queue 变成 durable/replay authority。

### HC-4：统一 ROOT pending-input plan 与 answer fence

主要文件：

- `conversation_kernel/runner.py`
- `conversation_kernel/host.py`
- `conversation_kernel/provider_dispatch.py`
- `conversation_kernel/steer.py`
- `conversation_kernel/steer_consumption.py`
- compaction coordinator/safe-point integration

工作：

- 禁止 ROOT loop-top 独立 drain；hard-cut 为一个 `PreparedRootPendingInputSuffixPlan`；
- 同一 base/quote/admission/CAS 下按 steer lane、completion lane 规划与提交；
- provider-open/tool-group/compaction fences，immutable successor 先原样消费；
- exact turn phase、settlement 前 `NEXT_TURN_ONLY` 与 pending-steer reopen 规则；
- completion 到达不能改写已 frozen dispatch。

### HC-5：Tool surface

主要文件：

- `capability/builtin_catalog.py`
- `conversation_kernel/tool_runtime.py`
- `conversation_kernel/subagent.py`
- tool registry/permission tests

工作：

- `wait_agent` 改为 level-triggered synchronization tool，并合并 optional multi-target
  `all|first` predicate；
- 删除独立 `wait_agent_tasks` descriptor/executor/dispatch，但迁移其 predicate tests；
- spawn/create guidance 改为 async sidecar + late join；
- base orchestration guidance 与 long-horizon tool classification 同步更新；
- stop result 去除重复 result transport；
- root/child fixed surface 与 bypass-only gate 保持。

### HC-6：compiler 与 compaction

主要文件：

- `model_input/contracts.py`
- `model_input/lowering.py`
- `conversation_kernel/provider_dispatch.py`
- `conversation_kernel/compaction/planner.py`
- `conversation_kernel/cold_epoch.py`

工作：

- 删除 `SUBAGENT_RESULT` 特殊 input origin；
- ROOT/child `INTER_AGENT_MESSAGE` 均走一个 typed lowering；
- completion 作为 user-role non-human input；
- prefix append-only 与 compaction retained semantics；
- ROOT completion initial entry 纳入 request-like classifier；
- frozen successor 禁止注入，普通 plan 使用唯一 prospective compiler/resource quote；
- 所有 provider 共用同一 compiler path。

### HC-7：protocol、browser bridge 与 frontend

主要文件：

- terminal schema/canonical/gateway；
- `web_app/browser_bridge.py`；
- `frontend/lib/runtime-adapter.ts`；
- current-session inspector、workbench conversation renderer。

工作：

- command/task projection hard-cut；
- 顶层 durable `completionDelivered` 与独立 non-fingerprinted Host live overlay；
- overlay 缺失时保守推导，所有 manual action 后端 exact revalidate；
- 删除旧“带入 result id”路径；
- hover detail、failure action、race refresh；
- 不展示内部 envelope/protocol 名称；
- 不恢复独立 tasks 页面。

### HC-8：激活与删除

- 删除旧 command、old source field、独立 `wait_agent_tasks`、result-carrying wait payload、
  `EXTERNAL_RESULT_COMMAND` 与兼容测试；
- 重生成 protocol artifacts/fixtures；
- reset verified local disposable database；
- 新 cold epoch 启动；
- focused tests、完整相关回归、real-provider dogfood、浏览器肉眼验收；
- 证据通过后才把状态改为 `ACTIVATED` 并修正 README 营销文案。

### 14.9 实施依赖与提交边界

推荐严格顺序：

```text
HC-0
  -> HC-1
  -> HC-2 + HC-3
  -> HC-4
  -> HC-5 + HC-6
  -> HC-7
  -> HC-8
```

`HC-2 + HC-3` 可以在同一分支并行开发，但在 core writer、所有 terminal offer 与 ACK confirmation
同时完成前不得声明可用；`HC-5 + HC-6` 共同改变 provider prefix，必须作为一个 cold-epoch cut
激活。schema/protocol/frontend 不保留双版本。实现过程允许中间 commit 暂时不形成可运行产品，
但 activation commit 必须同时删除旧路、reset verified disposable DB、重生成 artifacts，并通过
§15 的 gates。

---

## 15. 测试与 real LLM dogfood

### 15.1 deterministic tests

至少证明：

1. spawn admission 返回时 child 尚未完成，ROOT 可继续执行另一个 tool；
2. ROOT local work 与多个 child 的时间窗口真实重叠；
3. completed result 自动成为 ROOT-scope completion，不调用 wait；
4. FAILED/CANCELLED/INTERRUPTED/BLOCKED 全部自动形成同形 completion；
5. child failure 后 ROOT 下一 provider call产生自然语言收尾；
6. completion 在 provider stream 中到达时不污染 frozen request；
7. completion 不插入 assistant tool call 与 ToolResult 之间；
8. completion 唤醒 wait，但 wait ToolResult 不含 result body；
9. completion 先到、wait 后调用时立即返回；
10. steer 在 wait 订阅前/后到达都能由 level check 唤醒，不丢 edge；
11. optional multi-target `all|first` predicate 保留，unrelated input 不会伪造 predicate satisfied；
12. 已 delivered/retired inbox 的 terminal task 仍能立即满足 predicate；
13. steer/completion 同时 pending 时，由统一 plan 形成可断言的 fixed lane/entry 顺序；
14. 多 completion FIFO，duplicate offer 不重复 canonical entry；
15. initial terminal draft、transitive dependency cascade、frontier ACK 丢失都 offer 完整 terminal IDs；
16. automatic/manual/ACK-unknown race 只有一个 source-task entry；
17. manual loser command retry 返回同一 disposition，同 command ID 改投其他 task conflict；
18. automatic delivery 不产生 internal `session_commands` row；
19. actual child/provider exception detail 与 blocked dependency IDs 进入 failure completion；
20. final settlement/steer 同事务 race 符合 exact-turn phase，final 后 completion 不触发额外 call；
21. compaction successor 已冻结时保持 byte-identical，completion 只进入下一普通 plan；
22. manual completion initial turn 可首 call，也可按 request-like classifier 正确 compaction；
23. 下一 user turn 能消费仍在 process-local queue 的 late completion；
24. Host restart 不自动 replay queue、不自动 rerun child；
25. reconnect 后 durable delivered/failure/task projection 正确，undelivered task 可手动继续；
26. SYSTEM/tools 在同 epoch byte-identical，messages 只追加；
27. wait tool surface删除 result transport；独立 `wait_agent_tasks` 不存在，但 predicate tests 已迁移；
28. event/subject/guard/relation/job oracle 不增长；
29. 两个不同 session 的 ROOT/children 并行，互不共享 inbox 或 active slot；
30. terminal observation、tool closure、steer、completion 四类 ROOT suffix producer 的顺序和 owner
    均有测试，不存在绕开统一 plan 的 writer。

### 15.2 更新现有 Round 10 dogfood

现有 graph、context/message、capacity 三组能力继续保留，但去掉“创建后立刻 wait 到全部完成”的
主提示。新增 async 观察点：

- graph：ROOT 创建 DAG 后先完成自己的独立分析，dependency results 仍只给 direct downstream；
- context/message：ROOT spawn 后继续工作，再发送一次 mid-flight correction，最后自动收到 terminal；
- capacity：同一 HostSession 四个 live worker + queued worker，ROOT 不 wait 也能保持响应；terminal
  completion 分批到达但不丢失；
- predicate join：多 batch/DAG leaves 使用一次 `wait_agent(task_ids, settle=all)`，确认只返回
  satisfied/pending identities、不运输 result，也不退化成 wait/list polling loop；
- failure：一个 worker 可控失败，其他 worker 与 ROOT 继续，ROOT 给出有用结论；
- late answer：ROOT 故意先 final，child 后完成，不自动开启新 turn；下一次 user turn 或显式 UI
  action 才消费；
- manual idle：点击“用这份结果继续”走统一 command，并验证不会 rerun child。

### 15.3 前端肉眼验收

real LLM dogfood 必须通过 Pulsara 前端实际发送，不只运行接入代码和断言。验收者逐场景观察：

- spawn 后 ROOT 的 reasoning/tool 是否继续，而非立刻出现连续 wait；
- 多 worker 是否同时显示 active/queued/dependency 状态；
- child tool/reasoning 是否在任务详情可观察；
- terminal 后 task group 是否更新，ROOT 是否自动综合；
- failure 后 ROOT 是否仍产生自然语言回答；
- answer boundary 后是否没有后台额外气泡；
- idle action 的 hover、按钮状态与 race 刷新是否清楚；
- completion 是否没有被错误渲染为“你”；
- raw JSON、内部协议名和错误码是否没有泄漏到对话 UI；
- 长会话、多个 session 并行时是否没有无限重连或全应用卡顿。

每个场景保留截图/录屏、session/task IDs、实际 prompt、provider response 与观察结论；只排除
`PULSARA_API_KEY` 的值。

---

## 16. 删除清单

激活分支必须删除，而非 deprecate：

- `wait_agent_tasks` tool descriptor、executor、dispatch 和旧 dogfood 指令；
- 旧的 singular target/result-hydrating `wait_agent` schema 与 payload；predicate identities 迁移到新
  synchronization schema；
- `ACCEPT_SUBAGENT_RESULT` command 和 browser API；
- `source_subagent_result_id` transcript field/projection；
- `CanonicalInputOriginKind.SUBAGENT_RESULT` 与 `_project_subagent_result()`；
- “terminal result 不自动进入 ROOT”的 Round 10 条款；
- active ROOT 上的手动“带入会话”主路径；
- success-only acceptance 文案与 `result_accepted` UI 命名；
- old/new command、field 或 tool alias；
- `RunPermissionAdmissionSource.EXTERNAL_RESULT_COMMAND`；
- 为旧数据库做的 migration compatibility、dual read/write、fallback branch。

不得顺手删除：

- `list_agents` 分页历史；
- batch/DAG/dependency result；
- per-HostSession fixed four-worker physical capacity；
- `NONE | LAST_N` parent context；
- profiles/display roles；
- ROOT -> active child message；
- explicit/inferred result；
- stop/cancel/interrupt/dependency-failure lifecycle；
- session-bound task UI 与 child trace visibility；
- permission snapshot 与 provider prefix continuity。

---

## 17. Definition of Done

只有同时满足以下条件，本文才可从 `READY_FOR_IMPLEMENTATION` 改为 `ACTIVATED`：

- Pulsara ROOT 在未调用 wait 的情况下收到成功 terminal completion；
- 所有非成功 terminal 状态也自动进入 ROOT，并有自然语言收尾测试；
- `wait_agent` 只做 level-triggered activity/predicate synchronization，结果只从 completion envelope
  进入；
- automatic/manual acceptance 共用一个 canonical writer；
- automatic 不写 internal command，manual loser command 仍有 durable semantic binding；
- actual failure detail、dependency cause 与 next action 对 ROOT 可见；
- answer boundary 与 `trigger_turn=false` 行为被 race tests 证明；
- steer/completion 共用一个 prepared pending-input plan，immutable compaction successor 不被改写；
- provider 输入只走统一 compiler，严格 prefix 不回归；
- clean-v0 schema 已 hard-cut，旧 path 无残留；
- event/subject/guard/relation/job oracle 未无故增长；
- 无 restart 自动续跑、无 delivery job/replay；
- current kernel 的 batch、DAG、capacity、context、profiles、messaging、stop、result 全部有保留证据；
- 两个 session 可并行运行各自 ROOT/children；
- focused、相关完整回归与 real-provider dogfood 通过；
- 通过前端肉眼完成全部关键场景，截图/记录可追溯；
- README 最后才改为少内部实现、更多产品语言的准确版本。

---

## 18. 调研索引

### Pulsara production source

- `src/pulsara_agent/conversation_kernel/subagent.py`
  - `KernelSubagentManager`
  - `_start_available_tasks_worker`
  - `_run_child`
  - `_wait` / `_wait_many` / `_wait_for_tasks`
  - `consume_mailbox_safe_point`
- `src/pulsara_agent/conversation_kernel/runner.py`
  - `run_accepted_turn`
- `src/pulsara_agent/conversation_kernel/provider_dispatch.py`
- `src/pulsara_agent/conversation_kernel/steer.py`
- `src/pulsara_agent/conversation_kernel/steer_consumption.py`
- `src/pulsara_agent/conversation_kernel/safe_point.py`
- `src/pulsara_agent/conversation_kernel/_repository/external_results.py`
  - `accept_subagent_result_into_root`
- `src/pulsara_agent/conversation_kernel/_repository/subagents.py`
- `src/pulsara_agent/conversation_kernel/reader.py`
- `src/pulsara_agent/model_input/lowering.py`
  - `_project_subagent_result`
- `src/pulsara_agent/capability/builtin_catalog.py`
- `src/pulsara_agent/storage/migrations/sql/0000_conversation_kernel_baseline.sql`
- `src/pulsara_agent/conversation_kernel/host.py`
  - `accept_subagent_result`
- `frontend/components/inspector-panel.tsx`
- `frontend/app/pulsara-app.tsx`
- `tools/run_round10_subagent_dogfood.py`
- `tests/test_round10_hierarchical_subagent_orchestration.py`

### Codex local source (`fb0781b9eee6`)

- `codex-rs/core/src/tools/handlers/multi_agents_v2/spawn.rs`
- `codex-rs/core/src/tools/handlers/multi_agents_v2/wait.rs`
- `codex-rs/core/src/tools/handlers/multi_agents_spec.rs`
- `codex-rs/core/src/session/mod.rs`
  - `forward_child_completion_to_parent`
- `codex-rs/core/src/session/input_queue.rs`
- `codex-rs/core/src/session/turn.rs`
- `codex-rs/core/src/session_prefix.rs`
- `codex-rs/core/src/session/multi_agents.rs`
- `codex-rs/core/src/session/tests.rs`

### 独立 critic review

第一版落盘后，由独立 `gpt-5.6-sol`、`max` reasoning 子代理只读审查 Pulsara production source、
Codex `fb0781b9eee6d2da741b984bed9dde95834d909d` 与初稿。critic 报告的结论是：总体方向正确，
但初稿有 1 个 P0、7 个 P1，不能原样进入实现。本文没有把 critic 当作更高 authority，而是逐项回到
源码验证后作如下处理：

| critic finding | 决策 | 本文修订 |
| --- | --- | --- |
| P0：loop-top completion drain 与 steer/compaction owner 冲突 | **采纳** | 删除独立 drain，改为唯一 `PreparedRootPendingInputSuffixPlan`；冻结 successor 时明确禁止注入 |
| P1：activity-only wait 会丢订阅前 steer | **采纳** | 改成 subscribe → completion level → durable exact-turn steer → predicate → revision recheck 的 level-trigger 算法 |
| P1：Codex answer phase 不能直接套到 Pulsara terminal turn | **采纳** | phase keyed by exact turn；以 assistant settlement 的 `pending_steer_at_settlement` 决定是否继续，final commit 后不重开 |
| P1：`terminal_reason` 单码不足以恢复 | **采纳但收窄机制** | 只在既有 task row 增加 bounded public detail、closed code；dependency cause/next action 进入 failure object，不新增 failure table/event/job |
| P1：initial-blocked 与 frontier ACK-unknown 会漏 offer | **采纳** | 给出 initial terminal draft、normal cascade、exact causal frontier confirmation 三条算法和独立测试 |
| P1：automatic internal command 多余，manual loser 又未绑定 | **采纳** | automatic 改用 deterministic entry/source unique confirmation；manual winner/loser 都 durable bind client semantics |
| P1：直接删 `wait_agent_tasks` 会删掉 DAG predicate barrier | **部分采纳** | 不保留重复 tool；把 multi-target `all|first` predicate 合并进一个不运输结果的 `wait_agent`，既减少 affordance 又保留能力 |
| P1：UI pending/phase 没有 authority owner | **采纳** | durable 顶层 delivered + 可丢失 Host overlay；overlay 缺失时保守推导，后端 exact revalidation |
| P2：manual initial compaction、permission source、terminal observation 顺序遗漏 | **采纳** | 补 request-like classifier、permission source hard-cut，并封闭所有 ROOT suffix producer 的统一 owner |

critic 给出的另一条 P0 备选是“completion 先写，再让现有 provider dispatch 消费 steer”。本文**没有
采用**该最小改法：它虽然改动较少，却会让两个 pending lane 分别做 resource quote/CAS，无法一次
证明 exact suffix，而且未来容易再次产生第四条 writer。本文保留“human steer lane 在前”的产品
顺序，但要求它与 completion 在一个 prepared plan 内共同 quote、admit、canonicalize 和验证；这是
比简单改写顺序更大的 hard-cut，也是较可审计的最终边界。
