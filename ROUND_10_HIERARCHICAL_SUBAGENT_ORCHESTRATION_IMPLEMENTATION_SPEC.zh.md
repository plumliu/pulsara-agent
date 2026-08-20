# Round 10：ROOT-Orchestrated Subagent Task Graph 与 ROOT-to-Worker Messaging 实施规格

> 状态：**DRAFT — NOT ACTIVATED**
>
> 记录日期：2026-08-20
>
> 当前起草基线：`352da61dac75f1b46df5ec973e7cfb1be3774e2d`
>
> hard-cut 前产品参考基线：`5b7ad9f7ffc8565bc572180b2bde0c81ab64473a`
>
> 编码基线：**待 Round 9 与 Round 9.1 ACTIVATED、且Round 5B `R5B-A0` shared cold-epoch seam已落地后冻结**。本文依赖最终统一的 Builtin/MCP/Skill capability cut、sealed Builtin composition、exact child tool surface与唯一neutral cold assembler；不要求Round 5B完整compaction先ACTIVATED，也不得在当前起草基线上提前实现临时child capability profile或child prompt builder。
>
> 上位契约：[Stage 2 hard cut](STAGE_2_HARD_CUT_IMPLEMENTATION_SPEC.zh.md)、[Round 3 structured compiler](ROUND_3_STRUCTURED_MODEL_INPUT_COMPILER_IMPLEMENTATION_SPEC.zh.md)、[Round 3.1 provider-input prefix continuity](ROUND_3_1_PROVIDER_INPUT_PREFIX_CONTINUITY_IMPLEMENTATION_SPEC.zh.md)、[Round 4 Plan/permission](ROUND_4_PLAN_WORKFLOW_AND_RUN_PERMISSION_IMPLEMENTATION_SPEC.zh.md)、[Round 5A execution envelope](ROUND_5_LONG_HORIZON_EXECUTION_ENVELOPE_IMPLEMENTATION_SPEC.zh.md)、[Round 5A.2 durable provider replay](ROUND_5A_2_DURABLE_PROVIDER_REPLAY_AND_CROSS_RESTART_THREAD_CONTINUATION_IMPLEMENTATION_SPEC.zh.md)、[Round 7 model-visible outcome](ROUND_7_MODEL_VISIBLE_FAILURE_AND_TOOL_OBSERVATION_IMPLEMENTATION_SPEC.zh.md)、[Round 7.1 ToolResult projection](ROUND_7_1_PROVIDER_VISIBLE_TOOL_RESULT_PROJECTION_IMPLEMENTATION_SPEC.zh.md)、[Round 9 unified capability](ROUND_9_UNIFIED_CAPABILITY_SEMANTICS_IMPLEMENTATION_SPEC.zh.md)、[Round 9.1 Agent Skills](ROUND_9_1_AGENT_SKILLS_STANDARD_IMPLEMENTATION_SPEC.zh.md)、[Gap Index PHC-10](POST_HARD_CUT_PRODUCT_CAPABILITY_GAP_INDEX.zh.md#9-phc-10hierarchical--batch-subagent-task-graph)
>
> 共享Runtime seam：[Round 5B §10.1.1 shared cold-epoch assembler](ROUND_5B_LONG_HORIZON_CONTEXT_COMPACTION_IMPLEMENTATION_SPEC.zh.md#1011-shared-kernelcoldepochinputassembler)。Round 10只依赖可独立落地的`R5B-A0` pure seam，不依赖summary、snapshot adoption或完整Round 5B activation。
>
> 下游集成：Round 5B完整compaction的hierarchical handoff、future Round 9.2 Plugin Subagent contribution

本文恢复 hard-cut 前已经存在的 **batch task、dependency、phase/result reporting 与 task board** 产品能力，并把当前单个 flat child 扩展成由ROOT统一管理的有界并行task graph。这里的“层次化”严格表示一个星型拓扑：`one ROOT parent -> many worker leaves`。**不允许subagent继续创建subagent，也不存在worker作为另一个worker的parent。**旧实现只作为产品状态机和测试语义的参考；其 EventLog reducer、projection/hydration、checkpoint、repair、child execution recovery、跨 Host resume 一律不恢复。

本文同时补上旧系统没有真正完成的 parent-to-child communication。实现吸收 Codex 的两个关键经验：inter-agent message 必须拥有独立于 human user 的 typed envelope；运行中的消息应进入一个边界 mailbox，在 provider/tool group 的合法 safe point 才追加到目标上下文。本文不照搬 Codex 的 idle `followup_task`：Pulsara 的 terminal task 不复活，新工作必须创建新 task。

创建worker不是复制ROOT provider thread，而是建立一个全新的child cold continuity epoch。Round 10只提供`NONE | LAST_N`两种parent-conversation policy；不提供完整ROOT prefix fork或无界全历史继承。任务objective负责陈述工作，parent context只是一份可选、bounded、低authority的背景数据。Child的BASE_SYSTEM、provider tools、MCP routes与Skill catalog一律由child启动时的当前authority重新构造，并通过唯一`KernelColdEpochInputAssembler`进入既有compiler/wire/continuity路径；因此spawn前刚刚READY的child-visible MCP可以在新child epoch成为DIRECT，而不会改写ROOT epoch。

---

## 0. 执行结论

### 0.1 Round 10 的产品形状

Round 10 的最终工具面为：

~~~text
ROOT-only orchestration tools
  spawn_agent
  create_agent_tasks
  list_agents
  wait_agent
  wait_agent_tasks
  send_agent_message
  stop_agent

Child self-report tools
  report_agent_phase
  report_agent_result
~~~

七个ROOT orchestration descriptors属于固定Builtin surface：只要当前caller scope为ROOT，它们就在`READ_ONLY | ASK_PERMISSIONS | ACCEPT_EDITS | BYPASS_PERMISSIONS`四种permission mode的provider `tools[]`中保持完全相同。permission mode不得通过增删descriptor改变provider tool prefix。

“可见”不等于“可成功执行”。七个ROOT orchestration tools统一归类为现有`subagent_parent` builtin family；只有exact caller permission snapshot为`BYPASS_PERMISSIONS`时才可通过local authorize。其他三种mode中的任意调用都必须在attempt admission之前返回typed blocked result，固定reason为`subagent_requires_bypass_mode`，且不得创建task/dependency/turn、建立waiter、写mailbox、发出cancel或取得child capacity。`list_agents`、`wait_agent*`等只读/等待操作也不例外；本轮不建立“只读编排可放宽”的第二张permission matrix。

两个child self-report tools不是parent orchestration，也不归类为`subagent_parent`。它们继续受exact child scope、task lifecycle和参数bounds约束。所有worker profile拥有同一份ordinary permission/capability policy；profile不得缩窄普通工作能力或影响phase/result协议。

当前 dormant `stop_agent_task` descriptor删除。它的产品语义并未删除，而是并入`stop_agent(task_id=...)`：同一个canonical task id同时覆盖尚未启动、等待依赖和正在执行的task，没有必要再暴露第二个同义停止工具。

当前 flat 工具继续保留，但统一使用`task_id`。`subagent_run_id`从provider contract移除；当前代码中它只是`subagent_tasks.id`的别名，并不存在独立run relation。继续暴露两个名字只会制造虚假的身份分层。

### 0.2 三层事实，而不是第二套 runtime

~~~text
SubagentTask
  canonical logical work item
  owns objective/profile/dependencies/status/result lineage

Turn(scope = SUBAGENT_TASK(task_id))
  canonical conversation turn(s) for that task scope
  Round 10 V1仍只有一个 initial execution turn

RootSubagentCoordinator
  process-local physical scheduler/mailbox/live owner
  owns asyncio task, cancellation intent, capacity and phase snapshot
~~~

本轮不创建`SubagentRun`relation。当前一次task只绑定一次child execution；真实model/tool执行已经由`turns`、assistant entries、attempts和results表达。只有未来确实支持“同一logical task多次attempt”时，才有理由引入独立run identity。

### 0.3 固定两层的产品边界

ROOT是唯一orchestrator，但不是`subagent_tasks`row：

~~~text
ROOT
  task A (worker)
  task B (worker; depends_on A)
  task C (worker; independent)
~~~

这是唯一合法的agent topology。`A -> B` dependency只表示“ROOT scheduler必须等待A terminal成功后才能启动B”，不是A拥有B、A向B派工或B继承A上下文。worker之间没有parent/child、ancestor/descendant、subtree、sibling mailbox或直接通信语义。

每个task冻结：

- exact ROOT创建turn；
- immutable objective、profile、`NONE | LAST_N` context policy与dependency edges；
- current lifecycle与terminal result。

所有task创建、list、wait、stop与message控制只允许ROOT，且只有ROOT当前exact permission mode为`BYPASS_PERMISSIONS`时才能成功。七个descriptor仍稳定暴露在所有ROOT permission mode的provider `tools[]`中。child provider surface绝不暴露`spawn_agent`、`create_agent_tasks`、`list_agents`、`wait_agent*`、`send_agent_message`或`stop_agent`；child只能执行普通工作并调用自己的`report_agent_phase`、`report_agent_result`。dependency只表达ROOT创建的worker task之间的先后关系，不产生递归agent hierarchy。

### 0.4 Messaging 的最小定义

~~~text
send_agent_message(target_task_id, message)
  -> ROOT only, exact ACTIVE child
  -> process-local ordered mailbox
  -> target safe point
  -> canonical INTER_AGENT_MESSAGE entry
  -> next provider call sees an untrusted typed message suffix
~~~

它不：

- 冒充human `USER_MESSAGE`或`USER_STEER`；
- 唤醒terminal/idle task；
- 建立durable inbox；
- 允许child、foreign session或非ROOT scope发送；
- 修改BASE_SYSTEM或provider tools；
- 提升permission、memory、Skill或tool authority。

### 0.5 Durability 边界

本轮允许canonical relational rows记录已经接受的task、dependency、child transcript与terminal result；它们是产品历史，不是execution recovery。

Host close/takeover必须：

1. 停止新的task/message admission；
2. 取消所有live child execution；
3. 将所有`PENDING_START | WAITING_DEPENDENCY | ACTIVE` task终结为`INTERRUPTED`；
4. 丢弃尚未进入target canonical transcript的process-local mailbox items与未再可执行task的prepared parent-context bodies；
5. 不在新Host重启scheduler、不恢复child coroutine、不继续等待dependency。

禁止新增durable job、lease、claim、receipt、checkpoint、graph reducer、replay consumer或repair owner。

### 0.6 目标oracle

本轮只增加：

- 1张product relation：`subagent_task_dependencies`；
- 1个Committed event：`InterAgentMessageAccepted`，复用既有`ENTRY` subject slot；
- 1个transcript entry kind：`INTER_AGENT_MESSAGE`；
- 若干既有`subagent_tasks`与`subagent_task_children`列。

目标oracle：

~~~text
Committed events       32
Live events            24
subject slots          13
append guards           2
product relations      27
durable jobs            1
~~~

`SubagentProgress`继续承担task/phase/status的process-local UI更新，不新增第二个task-board Live event。

---

## 1. 调研结论与取舍

### 1.1 hard-cut 前 Pulsara 真正值得恢复的部分

`5b7ad9f7`之前已经有production代码和回归测试覆盖：

- `create_agent_tasks`原子materialize一批task；
- same-batch task key与既有task id dependency解析；
- DAG cycle rejection；
- dependency完成后自动启动downstream；
- dependency失败、取消或blocked时传递`blocked_dependency_failed`；
- `wait_agent_tasks(settle=all|first)`与timeout partial results；
- `stop_agent_task`取消waiting/active logical task；
- child `report_agent_phase`与`report_agent_result`；
- explicit result优先于inferred final assistant text；
- bounded task inventory与统一task board。

主要参考：

- `5b7ad9f7:src/pulsara_agent/ports/subagent.py`的closed command/outcome；
- `5b7ad9f7:src/pulsara_agent/runtime/subagent/tool_port.py`的dependency纯函数；
- `5b7ad9f7:src/pulsara_agent/runtime/subagent/runtime.py`的调度状态转移；
- `5b7ad9f7:tests/test_subagent_runtime.py`中的batch、dependency、wait、phase/result与failure propagation测试。

### 1.2 hard-cut 前明确不能恢复的部分

下列旧代码只解决旧EventLog架构下的durability/recovery，不进入Round 10：

- `runtime/subagent/reducer.py`；
- `runtime/subagent/projection.py`；
- `runtime/subagent/hydration.py`；
- parent/child event-ledger replay；
- restart graph equality与dangling-run repair；
- recovery generation、checkpoint、receipt与repair drain；
- child `RuntimeSession`跨Host复活；
- task-level retry/reset/redefine。

旧测试中带`restart`、`hydration`、`repair`或event reducer equality的断言不能机械搬运。应改写成：canonical历史仍可查询，但新Host只看到terminal `INTERRUPTED`，不会重启执行。

### 1.3 Codex 值得吸收的部分

本地Codex当前实现证明了以下产品边界：

1. Agent可以被实现为树形thread，Codex也允许child继续产生自己的child。
2. `AgentPath`提供root/child层次和相对target解析。
3. `send_message`与`followup_task`共享同一个`InterAgentCommunication` carrier，只差`trigger_turn`。
4. `send_message`只queue，不唤醒idle thread；running target在message/tool边界消费mailbox。
5. `followup_task`能唤醒idle thread并形成新turn。
6. inter-agent内容使用独立typed envelope，不伪装human user。
7. hierarchy bounds与global concurrency由一个control plane统一约束。

Round 10吸收3、4、6，以及7中的统一global capacity；明确不吸收1中的recursive child spawn。Pulsara已有canonical task id和task board，V1不增加第二个path alias；target继续使用task id。

Codex V2的`send_message`可解析已知agent path/thread，测试也明确允许child向ROOT发送。Round 10不照搬这组任意已知target语义：本轮只有ROOT向其ACTIVE worker发送指导；child向上通过`report_agent_phase`、`report_agent_result`和terminal result表达。这样spawn/list/wait/stop/message全部共享ROOT-only访问矩阵，不需要agent path、root inbox、sibling通信或跨层路由。若后续真实产品需要双向协作，应单独扩展message access matrix，而不是把它藏进task-id解析。

### 1.4 为什么不照搬 `followup_task`

Codex把Agent thread作为可长期继续的身份，因此completed turn之后可以继续给同一thread新task。Pulsara当前canonical identity是immutable `SubagentTask`：dependency、result与terminal status都挂在它上面。

若本轮照搬`followup_task`，必须二选一：

- 把terminal task重新改回ACTIVE；或
- 另造`SubagentAgentThread`身份，把task与thread拆成两套relation。

前者破坏append-only terminal语义，后者超出本轮真实需求。故冻结：

~~~text
ACTIVE task需要补充指导
  -> send_agent_message

terminal task出现新工作
  -> create a new task
     optionally depend on / cite old task result
~~~

future若确实需要同一child persona跨多个logical task延续，应单独设计`AgentThread`，不能把它偷偷塞进本轮task状态机。

---

## 2. Authority、owner与scope

### 2.1 Authority表

| Fact | 唯一owner | 非owner |
|---|---|---|
| task identity/objective/profile/dependency/status | canonical subagent relations | Live bus、compiler、provider text |
| child conversation | exact `SUBAGENT_TASK(task_id)` transcript | ROOT transcript、task board |
| current physical child/capacity/mailbox/phase/prepared parent context | `RootSubagentCoordinator` | database replay、Committed events |
| child permission | exact inherited ROOT creation-turn `BYPASS_PERMISSIONS` snapshot | profile name、ROOT model prose、后续ROOT mode变化 |
| child tool surface | Round 9 exact scope capability cut + physical binding | task metadata、Skill text |
| result | exact terminal `subagent_task_children.RESULT` | Live summary、provider response |
| ROOT-visible accepted result | existing `ACCEPT_SUBAGENT_RESULT` or wait ToolResult | background compiler guess |

### 2.2 一个Host-wide coordinator

不得为每个child创建独立manager。使用一个Host-wide：

~~~text
RootSubagentCoordinator
  live_tasks: task_id -> LiveSubagentExecution
  root_owned_task_inventory
  dependency_ready_queue
  per_task_message_mailboxes
  per_task_parent_context_start_material
  process-local phase snapshots
  one global capacity counter
~~~

每次tool invocation携带exact caller scope：

~~~text
SubagentInvocationOwner
  session_id
  caller_turn_id
  caller_scope_kind
  caller_scope_subagent_task_id
  tool_attempt_id
  tool_call_entry_id
  tool_call_id
  permission_snapshot_fingerprint
  exact tool-surface borrow
  owner_fingerprint
~~~

Coordinator据此构造scope-filtered view。orchestration operations必须机械证明caller scope为ROOT；report operations必须机械绑定caller自己的exact`SUBAGENT_TASK(task_id)`scope。调用者不得通过参数伪造ROOT或另一个task。

### 2.3 ROOT orchestration与child self-report

~~~text
caller ROOT
  always sees create/list/wait/stop/message descriptors
  may successfully invoke them only under BYPASS_PERMISSIONS

caller SUBAGENT_TASK(A)
  may only report phase/result for A
  may not orchestrate any task
~~~

所有新task的创建turn必须属于ROOT scope且其exact permission snapshot必须为`BYPASS_PERMISSIONS`。任何child调用orchestration descriptor都应在tool surface构造阶段不可见；invoke seam仍保留ROOT exact-scope防绕过校验。ROOT descriptor exposure不得按permission mode变化；permission只在local authorize与invoke防绕过seam重验。

### 2.4 Scope访问矩阵

| 操作 | ROOT + `BYPASS_PERMISSIONS` | ROOT + 其他mode | child |
|---|---:|---:|---:|
| 创建batch/single task | 是 | blocked | 否 |
| list/wait/stop/message task | 是 | blocked | 否 |
| report own phase/result | 否 | 否 | 是 |
| report另一个task | 否 | 否 | 否 |

不存在recursive list、subtree或跨层mutation语义；`list_agents`只分页读取本session中ROOT创建的task inventory。

---

## 3. Canonical schema

### 3.1 `subagent_tasks`扩展

clean-v0目标字段：

~~~text
subagent_tasks
  id
  session_id
  workspace_id

  parent_turn_id                   exact ROOT creation provenance

  batch_id                         nullable
  task_key                         nullable
  label                            nullable
  profile_kind                     closed enum
  display_role                     nullable
  context_mode                     NONE | LAST_N
  context_last_n_turns             nullable integer
  objective                        canonical bounded text

  status
  pending_reason                   nullable
  terminal_reason                  nullable
  execution_writer_generation
  accepted_at
  terminal_at
~~~

Closed status：

~~~text
PENDING_START
WAITING_DEPENDENCY
ACTIVE
COMPLETED
FAILED
CANCELLED
INTERRUPTED
BLOCKED_DEPENDENCY_FAILED
~~~

Terminal status：

~~~text
COMPLETED | FAILED | CANCELLED | INTERRUPTED | BLOCKED_DEPENDENCY_FAILED
~~~

关键约束：

- parent turn必须属于same session/workspace的ROOT scope；
- `task_key`在同ROOT turn与同batch内唯一；
- `context_mode=NONE` iff `context_last_n_turns IS NULL`；
- `context_mode=LAST_N` iff `1 <= context_last_n_turns <= 8`；
- objective/profile/context/dependency在insert后immutable；
- terminal row不可回到nonterminal；
- `terminal_at`与terminal status exact对应。

这些跨row条件使用deferred constraint trigger或repository transaction revalidation；不得依赖application-only先查后写。

### 3.2 唯一新增relation：`subagent_task_dependencies`

~~~text
subagent_task_dependencies
  session_id
  task_id
  dependency_task_id
  dependency_ordinal
  accepted_at

  PK/UNIQUE(session_id, task_id, dependency_task_id)
  UNIQUE(session_id, task_id, dependency_ordinal)
  FK task_id -> subagent_tasks
  FK dependency_task_id -> subagent_tasks
  CHECK task_id <> dependency_task_id
~~~

约束：

- 两端必须属于same session/workspace且均由ROOT创建；
- dependency必须是同batch task key解析出的task，或调用前已存在的same-session ROOT-owned task id；
- 不允许跨session/workspace dependency；
- 每task最多16条dependency；
- 每batch最多16个task、64条edge；
- batch事务内使用预生成task ids解析forward reference并拒绝cycle；
- 既有task的dependency集合永不backpatch。

本relation只表达产品DAG，不拥有scheduler或recovery。

### 3.3 `subagent_task_children`扩展

现有relation继续表达child产生的ordered MESSAGE与唯一terminal RESULT。RESULT增加nullable closed字段：

~~~text
result_source            EXPLICIT | INFERRED
summary                  bounded nullable for MESSAGE, required for RESULT
output_preview           bounded nullable
diagnostics              bounded JSON array
result_fingerprint       nullable for MESSAGE, required for RESULT
~~~

`EXPLICIT` exact join本task的`report_agent_result` assistant tool request/result；`INFERRED` exact jointerminal assistant message。两者不得同时存在。

### 3.4 `INTER_AGENT_MESSAGE` entry

`transcript_entries.entry_kind`增加：

~~~text
INTER_AGENT_MESSAGE
~~~

并增加nullable lineage字段：

~~~text
source_inter_agent_tool_attempt_id
~~~

只有`INTER_AGENT_MESSAGE`可以设置它们，且必须：

- target scope为`SUBAGENT_TASK`；
- source attempt的tool name为`send_agent_message`；
- source turn由attempt -> assistant entry机械join，且必须属于ROOT scope；
- body digest等于prepared mailbox item；
- source/target same session；
- entry插入时target turn仍RUNNING。

它不增加message relation。message admission truth已由source tool call/attempt/result与target entry共同表达；尚未被target safe point消费的mail只存在process-local。

### 3.5 Event vocabulary

新增：

~~~text
InterAgentMessageAccepted
  subject slot = ENTRY
  payload = recipient task id, message ordinal
~~~

现有：

- `SubagentTaskAccepted`记录每个task；
- `SubagentTaskStatusAccepted`记录状态转移；
- `SubagentMessageAccepted`继续只表示child assistant message；
- `SubagentResultAccepted`继续只表示child terminal result。

不得复用`UserSteerAccepted`，也不得把ROOT inter-agent message伪装成human input。

---

## 4. Tool contracts

### 4.1 `spawn_agent`

~~~json
{
  "task": "Review the transaction boundary",
  "task_name": "review_transaction",
  "profile": "review_worker",
  "context": {"mode": "none"}
}
~~~

字段：

- `task` required，1..65,536 UTF-8 bytes；
- `task_name` optional，1..64字符，`[a-z][a-z0-9_-]*`；映射为task_key/label，不形成第二个canonical path；
- `profile` default `general_worker`；
- `context` default `{"mode":"none"}`，closed union：

~~~json
{"mode": "none"}
~~~

或：

~~~json
{"mode": "last_n", "turns": 3}
~~~

`mode=last_n`时`turns`必须为`1..8`；`mode=none`时不得出现`turns`。不得接受`all`、`full`、负数、字符串化数字或unknown field。

它等价于一个无dependency的single-item batch。所有admission、capacity和ACK unknown逻辑必须复用`create_agent_tasks`的central factory，不保留第二套spawn writer。

结果：

~~~json
{
  "task_id": "subagent-task:...",
  "status": "active"
}
~~~

### 4.2 `create_agent_tasks`

~~~json
{
  "tasks": [
    {
      "task_key": "inspect",
      "task": "Inspect the current implementation",
      "profile": "research_worker",
      "context": {"mode": "last_n", "turns": 2},
      "depends_on": []
    },
    {
      "task_key": "review",
      "task": "Review the findings",
      "profile": "review_worker",
      "depends_on": ["inspect"]
    }
  ]
}
~~~

closed task fields：

~~~text
task
task_key?
label?
profile
display_role?
context = {mode: none} | {mode: last_n, turns: 1..8}
depends_on[]
~~~

`depends_on`接受：

- same batch exact `task_key`；
- `task:<canonical task id>`形式的既有same-session ROOT-owned task。

不接受模糊label、objective相似度或跨session查找。

结果返回全部task，不只返回已启动者：

~~~json
{
  "batch_id": "subagent-batch:...",
  "tasks": [
    {"task_key":"inspect","task_id":"...","status":"active"},
    {"task_key":"review","task_id":"...","status":"waiting_dependency"}
  ]
}
~~~

### 4.3 `list_agents`

~~~json
{
  "max_items": 50,
  "include_dependencies": true
}
~~~

结果按`accepted_at, task_id`确定性排序，返回：

- task id；
- task key/label/profile；
- objective preview；
- status/pending reason/terminal reason；
- dependency ids/status；
- current process-local phase（若同Host仍存在）；
- terminal result id、source与bounded summary；
- pending ROOT-message count（只在当前Host存在时）；
- total/omitted counts。

它从不返回child raw transcript、full diagnostics、hidden reasoning或process-local executor对象。

### 4.4 `wait_agent`与`wait_agent_tasks`

~~~json
{
  "task_id": "subagent-task:...",
  "timeout_seconds": 30
}
~~~

~~~json
{
  "task_ids": ["...", "..."],
  "settle": "first",
  "timeout_seconds": 30
}
~~~

规则：

- target必须是本session中由ROOT创建的task；
- `task_ids` 1..32，唯一；
- `settle=first|all`；
- timeout 0..300秒；0表示只poll；
- timeout返回terminal results与pending ids，不取消pending task；
- `first`只结束等待，不停止其他task；
- terminal结果是immutable read，因此重复wait返回相同结果；不再维护`consumed_by_wait`或`include_consumed`状态。

wait只是bounded process-local wait + canonical read，不新增durable waiter、receipt或Live subscription。

### 4.5 `stop_agent`

~~~json
{
  "task_id": "subagent-task:...",
  "reason": "No longer needed"
}
~~~

它支持ROOT-owned task的全部nonterminal状态：

- `PENDING_START | WAITING_DEPENDENCY -> CANCELLED`；
- `ACTIVE ->`安装exact per-turn `USER_REQUEST` cancellation cause，原子结算child turn/task；
- terminal ->幂等返回当前terminal status。

task取消后，依赖它的downstream在同transaction或紧随其后的shielded exact settlement中成为`BLOCKED_DEPENDENCY_FAILED`。不得自动取消无dependency关系的其他worker。

### 4.6 `report_agent_phase`

~~~json
{
  "phase": "checking transaction races",
  "message": "Two paths remain",
  "progress": {"completed": 3, "total": 5}
}
~~~

仅target child自己可调用；task id从invocation scope绑定，schema不接受task id。

phase是process-local advisory snapshot：

- `phase`最多256 UTF-8 bytes；
- `message`最多4,096 bytes；
- `progress`最多32 keys、8 KiB canonical JSON、depth 4；
- canonical ToolResult row得到`FULL`确认后才通过既有process-local settlement安装；这里的`FULL`是ACK confirmation，不是provider render mode；
- 安装后复用`SubagentProgress` Live event；
- Host restart可丢失；不写新relation或Committed event。

### 4.7 `report_agent_result`

~~~json
{
  "summary": "The reader needs an exact cut fence.",
  "output_preview": "Relevant files: ...",
  "diagnostics": [{"code":"CUT_RACE","severity":"high"}]
}
~~~

规则：

- 仅task自己的active child可调用；
- summary 1..16,384 UTF-8 bytes；
- output preview最多32,768 bytes；
- diagnostics最多32项，每项8 KiB、aggregate 64 KiB；
- ToolResult只返回小型typed acknowledgement，不复制summary/output/diagnostics，也不要求为了让child再次看到它而发起新model call；
- invoke在物理dispatch前冻结`PreparedExplicitSubagentResultSettlement`；其canonical ToolResult接受事务同时插入exact `subagent_task_children.RESULT`、完成child turn与task并追加对应occurrences；
- specialized settlement的ACK unknown通过同一prepared candidate确认全部row为`FULL | NONE | CONFLICT`，不得出现“ToolResult成功但result/task仍未完成”的中间终局；
- 这里的`FULL`只表示整组canonical rows逐字段确认存在；不要求summary/output正文以provider `FULL` variant再次交付，也不建立result专用provider预算；
- 如果child没有调用本工具而直接给final assistant text，Runtime产生`INFERRED` result；
- explicit与inferred winner只能有一个，ACK unknown使用prepared candidate做`FULL | NONE | CONFLICT`确认。

### 4.8 `send_agent_message`

~~~json
{
  "task_id": "subagent-task:...",
  "message": "Also check the cancellation path before concluding."
}
~~~

规则：

- 仅ROOT可调用，target必须是exact ACTIVE ROOT-owned child；
- body 1..16,384 UTF-8 bytes；
- per-target mailbox最多16条、64 KiB；
- invoke在coordinator lock内以host-local monotonic ordinal入队；
- 返回`{"status":"queued","task_id":"..."}`，不谎称模型已读取；
- terminal/closing target返回`TOOL_UNAVAILABLE`；
- child caller、foreign session、unknown task返回typed denial；
- queue-only，不创建新turn、不复活completed task。

---

## 5. Profile、context与capability

### 5.1 Closed profiles

~~~text
general_worker
research_worker
review_worker
verification_worker
synthesizer
~~~

`display_role`只用于展示；`profile`才是execution contract。

Profile由Host配置/代码映射到：

- stable child system supplement；
- model target policy；
- output/report hint。

模型不能提交raw tool allowlist、MCP server id、Skill name、permission mode或provider credential。

`profile`只改变worker的模型行为与结果表达，不改变permission或ordinary capability集合。所有child profile都没有subagent orchestration能力；七个ROOT orchestration tools只出现在ROOT surface。每个child在统一的ordinary child capability surface上增加自身的phase/result reporting tools。不得用profile、prompt文案、隐藏gate或profile-specific allowlist间接恢复child spawn或删减MCP/Skill/ordinary Builtin能力。

### 5.2 Child permission

ROOT orchestration permission沿用并收紧当前`subagent_parent` gate：

~~~text
caller scope == ROOT
and exact effective permission mode == BYPASS_PERMISSIONS
  -> orchestration authorize may continue

otherwise
  -> BLOCKED(subagent_requires_bypass_mode)
  -> no attempt admission
  -> no orchestration side effect
~~~

该gate同时覆盖`spawn_agent | create_agent_tasks | list_agents | wait_agent | wait_agent_tasks | send_agent_message | stop_agent`，并在invoke seam按同一exact permission snapshot再次验证，防止绕过local authorize。不得把`READ_ONLY`下的list/wait或`ASK_PERMISSIONS`下的spawn改成prompt/confirmation；用户若希望使用subagent，必须先以既有permission-mode切换机制开启`BYPASS_PERMISSIONS`。descriptor集合在切换前后保持不变。

Child permission必须exact继承创建它的ROOT turn permission snapshot：

~~~text
child permission snapshot
  == ROOT creation-turn permission snapshot
  == BYPASS_PERMISSIONS
~~~

由于task creation只可能在`BYPASS_PERMISSIONS`下成功，每个worker均以该exact bypass snapshot运行；profile、objective、task metadata和模型输出都不能缩窄、扩大或重解释它。创建后ROOT在后续turn切换permission mode不会回写running worker的frozen snapshot；显式停止或Host close才终结该worker。child不得进入Plan或直接询问用户。ordinary capability仍须通过scope visibility、schema、dirty/unavailable、effect policy、physical binding与hard safety gates；`BYPASS_PERMISSIONS`不会把ROOT-only、scope-invisible或失效能力变成可用。`report_agent_phase/result`是child自有协议出口，不属于`subagent_parent` family。

### 5.3 Context policy

Round 10只有：

~~~text
NONE
LAST_N(turns = 1..8)
~~~

默认`NONE`。两种mode都会给child：

- exact task objective；
- 由child authority重新构造的stable BASE_SYSTEM与project/runtime policy；
- child启动时冻结的Round 9 Builtin/MCP exposure与Round 9.1 Skill catalog；
- 独立child cold continuity epoch。

`NONE`不复制任何ROOT conversation。它不是“没有上下文”：objective和当前child authority始终存在；被省略的只是parent transcript data。

`LAST_N`从**产生当前spawn/create调用的exact ROOT provider-input cut**冻结最近N个ROOT user-led context units。Context unit定义为：

1. 一条accepted真实ROOT `USER_MESSAGE`开启一个unit；
2. 属于该user run且已经进入该exact cut的ordered steer、assistant message和完整tool-call/result groups归入同一unit；
3. 最新unit允许是open unit，即只有当前accepted user input而尚无产生spawn的assistant output；
4. assistant ordered blocks与tool group是原子，绝不从中间截断；
5. 若历史少于N个unit，返回全部available units，不补空项、不跨source floor猜测更早历史。

选择基于该ROOT call实际可见的provider-neutral semantic view，而不是从genesis重扫canonical transcript，也不能纳入ROOT当次尚未提交的spawn assistant/tool group。它只携带public conversational data；明确排除：

- ROOT BASE_SYSTEM、provider tools与runtime-observation carriers；
- hidden reasoning、Round 5A.2 provider-private replay与remote response id；
- current permission/Plan/TODO/memory head等应由authority重建的状态；
- ROOT或其他scope签发的MCP ref、pagination cursor、memory citation handle与physical borrow authority。

已知scope/epoch-local augmentation不得原样迁移；`PARENT_CONTEXT`使用provider-neutral quote renderer重新表达选中items，使其成为背景数据而不是child native tool-call history。Artifact等已有canonical、scope-safe public reference只有在现有读取gate允许时才可保留；不递归解释或清洗opaque user/tool body。无论正文长得多像指令，carrier固定为：

~~~text
source      PARENT_CONTEXT
trust       UNTRUSTED_OBSERVATION
lifecycle   SNAPSHOT
presence    VALUE
body        ordered bounded context units
~~~

`NONE`以及`LAST_N`没有eligible unit时不安装该source；新child cold epoch中不需要伪造`CLEARED`。`LAST_N`quote projection必须在task admission时整体通过现有single-source physical bound；不fit时整个task/batch typed拒绝且canonical task row为0。Child真正启动时还要把这份不可变projection与当时的SYSTEM/tools/runtime sources做一次normal compiler preflight；若aggregate不fit，task以typed resource-boundary `FAILED`终结且provider open为0。两层都不允许静默缩小N、截断unit或退化为`NONE`。

Prepared parent context在task admission时冻结；dependency/capacity导致的延迟启动不能改用较新的ROOT history。为了避免batch中多task重复复制大正文，同一batch只持有一个private、repr-safe frozen ROOT semantic cut，每个task保存自己的selection与projection fingerprint。它是Host-local start material；Host close后nonterminal task统一`INTERRUPTED`，新Host不恢复该body。

`RootSubagentCoordinator`在batch FULL/ACK-confirmed settlement中exact安装这些start materials；caller cancellation只能detach，不能留下已接受task却没有其prepared context。Material在task terminal或Host close后释放。Canonical task row只保存mode与N，不复制parent正文；ROOT canonical transcript仍是语义来源，但Round 10不从它执行跨Host child recovery。

Child真正启动时，coordinator只把immutable objective与optional `PARENT_CONTEXT`封装为Round 5B §10.1.1 closed `SubagentInitialSeed`，再把该seed、exact child scope、resolved target、Round 9 parent/views、current runtime-source candidates与唯一planning deadline交给`KernelColdEpochInputAssembler`。Coordinator不得自行拼接SYSTEM、lower tools/messages、构造第二份wire plan或复制cold continuity candidate逻辑；assembler也不得反向选择N、读取task repository或取得child physical execution authority。

Child与ROOT之间没有continuity或cache compatibility承诺。Round 3.1 strict-prefix从child第一次provider open之后才开始；因此不存在`FULL_PREFIX_FORK`。Round 10也不提供`FULL_SEMANTIC`/`all`：需要精确旧事实时，ROOT应写入自洽objective、给出canonical file/artifact定位，或在child ACTIVE后使用`send_agent_message`补充，而不是无界复制整个会话。

### 5.4 Orchestration bounds

默认与hard bound：

~~~text
maximum live child executions per Host 4
maximum tasks per create batch         16
maximum dependency edges per batch     64
maximum accepted tasks per caller turn 16
maximum wait targets                   32
maximum LAST_N parent context turns     8
~~~

所有task都处于固定worker层。Global capacity由唯一coordinator预留，不能让每个child独立计数而突破Host上限。

---

## 6. Task admission与dependency scheduler

### 6.1 Prepared batch

~~~text
PreparedSubagentTaskBatchAdmission
  session/workspace/writer generation
  exact ROOT caller turn/tool attempt
  batch id
  ordered task row drafts
  ordered dependency row drafts
  initial status per task
  shared frozen ROOT semantic cut reference
  ordered per-task parent-context selections
  profile/context projection fingerprints
  event drafts
  candidate fingerprint
~~~

Factory先完成：

1. args bounds；
2. ROOT scope/capability检查；
3. exact spawn-producing ROOT provider-input cut join；
4. `NONE | LAST_N` selection、quote rendering与physical preflight；
5. stable ids预生成；
6. dependency解析；
7. cycle detection；
8. existing dependency exact session/status read；
9. initial status计算；
10. capacity soft preflight。

Repository transaction重新验证canonical ROOT creation provenance/dependency，但不重新解释自然语言objective或profile含义。

### 6.2 Initial status

~~~text
any dependency terminal non-success
  -> BLOCKED_DEPENDENCY_FAILED

all dependencies COMPLETED
  -> PENDING_START

otherwise
  -> WAITING_DEPENDENCY
~~~

Batch acceptance不要求所有runnable task立即取得physical slot。`PENDING_START`是已接受、等待同Host coordinator capacity的状态，不是durable job。

### 6.3 Start

Coordinator对`PENDING_START`按`accepted_at, task_id`公平排序：

~~~text
reserve Host-global slot
-> consume the task's creation-time frozen parent-context selection
-> freeze current exact child-scope Builtin/MCP/Skill owner snapshots
-> resolve child model target and construct Round 9 parent/views
-> CAS task PENDING_START -> ACTIVE
-> admit initial child turn
-> construct exact SubagentInitialSeed
-> KernelColdEpochInputAssembler.prepare_semantic()
-> selected replay hydration under the same deadline, normally NONE for a fresh child
-> KernelColdEpochInputAssembler.finalize_wire()
-> exact physical tool-surface join and DirectModel preflight
-> continuity CAS installs new child cold epoch
-> open provider through the same PreparedKernelModelExecution
~~~

这里有意使用两个不同linearization：parent context冻结在task admission；execution capability冻结在child真正启动、第一次provider open之前。等待dependency期间ROOT新增的对话不会偷渡进`LAST_N`，但期间新READY且child-visible的MCP、当前Skill winners和physical reconnect可以进入child的新cold epoch。

若在ACTIVE/turn admission前失败：

- task -> FAILED，reason为closed sanitized code；
- 释放slot；
- cascade direct/transitive dependency block；
- 不重试、不repair、不恢复。

若task/turn已经FULL但assembler、wire preflight、physical join或continuity CAS失败，使用现有child runtime-failure atomic terminalization把task置为`FAILED`并关闭exact child turn；provider尚未open时open count必须为0。不得退回child-specific prompt builder、缩小`LAST_N`、换一套tools或绕过continuity安装来“挽救”该task。

ACK unknown时使用stable child turn candidate和stateless confirmation；不能因为waiter取消而重复启动child。

### 6.4 Dependency settlement

一个taskterminal时，shielded scheduler settlement锁定：

- exact terminal task；
- 直接downstream rows；
- dependency rows；
- 当前writer generation。

对于每个downstream：

~~~text
any dependency FAILED/CANCELLED/INTERRUPTED/BLOCKED
  -> BLOCKED_DEPENDENCY_FAILED

all dependencies COMPLETED
  -> PENDING_START

otherwise
  -> unchanged WAITING_DEPENDENCY
~~~

transitive propagation逐层bounded处理；单batch最多16 tasks，因此不需要通用graph job。

### 6.5 ROOT turn termination

ROOT-owned task可以跨创建它的ROOT turn继续运行，直到task terminal、显式stop或Host close。ROOT assistant完成当前turn不会隐式取消workers；下一条ROOT user turn可以继续list/wait/stop/message同一task。由于child不能创建child，不存在parent-task terminalization、descendant drain或orphan subtree问题。

---

## 7. Inter-agent mailbox与safe point

### 7.1 Process-local carrier

~~~text
PreparedInterAgentMailboxItem
  session_id
  sender ROOT turn
  sender tool attempt/call
  recipient task/current turn
  host-local ordinal
  content bytes/digest
  candidate entry id
  candidate event id
  item fingerprint
~~~

它不是receipt。Host crash后不能从tool call扫描并重建mailbox。

### 7.2 Linearization

`send_agent_message`与target terminalization共享coordinator lock：

- send先线性化：item进入mailbox，target finalizer必须先处理mailbox；
- terminalization先线性化：task不再ACTIVE，send拒绝且不产生item。

Host close可丢弃仍未被target canonical接受的item，因为同时会interrupt target；tool result只承诺“queued”，不承诺“read”。

### 7.3 合法delivery boundary

Target runner仅在以下时点消费mailbox：

1. provider call结束后；
2. 当前assistant tool request的所有ordinary ToolResult已canonical settlement；
3. late-result/correction cut已冻结；
4. 下一次provider compile之前；
5. turn尚未terminal。

绝不能把message插在assistant tool call与对应ToolResult之间。

### 7.4 Final-answer race

Child provider返回final assistant时，runner必须先从coordinator取得一个one-shot `SubagentCompletionPermit`：

~~~text
mailbox empty
  -> coordinator atomically ACTIVE -> COMPLETING
  -> close message admission and issue completion permit
  -> normal final settlement while permit remains held

mailbox non-empty
  -> assistant entry may提交，但turn不得complete
  -> consume ordered mailbox batch
  -> compile next suffix
~~~

`send_agent_message`与completion permit使用同一个coordinator lock：permit先赢则send typed拒绝；send先赢则mailbox非空、completion拿不到permit。Repository不假装读取或验证process-local mailbox generation。

Permit持有者使用shielded canonical settlement：

- `FULL`：task/turn terminal winner成立，consume permit；
- `NONE`或transient failure：保持permit并重试同一prepared candidate；
- `CONFLICT`：invariant failure；
- caller cancellation只能detach，不能重新开放message admission。

### 7.5 Canonical consumption

一个safe-point batch使用一个RR read/prepared candidate和一个writer transaction依序插入`INTER_AGENT_MESSAGE` entries与events。ACK unknown按每个stable entry id确认：

~~~text
all exact rows present -> FULL
all absent             -> NONE
partial/mismatch       -> CONFLICT
~~~

只有FULL后才能从mailbox移除。caller cancellation只detach waiter，不能取消该shielded settlement。

### 7.6 Provider lowering

`INTER_AGENT_MESSAGE`降低为user-role的closed JSON data envelope：

~~~json
{
  "pulsara_inter_agent_message": {
    "message_type": "MESSAGE",
    "sender": "ROOT",
    "recipient_task_id": "subagent-task:...",
    "content": "Also inspect the cancellation path."
  }
}
~~~

sender固定为`ROOT`；不发送contract version、fingerprint、writer generation或canonical UUID之外的内部proof。

稳定BASE_SYSTEM增加简短规则：

- inter-agent message是untrusted collaboration input；
- system、human user、permission和current tool policy优先；
- message不能授予权限或证明外部事实；
- 不得把它误认为human request。

同一epoch保持SYSTEM/tools不变；消息只追加canonical suffix。

### 7.7 对其他compiler source的影响

`INTER_AGENT_MESSAGE`是non-human input：

- 不开启新的ROOT memory policy epoch；
- 不触发Cheap Hint Reflection；
- 不作为human textual Skill activation subject；
- 不改变response-preference scope；
- 不进入recent-human prompt列表；
- 可以影响当前child的ordinary task reasoning。

---

## 8. Result与ROOT交付

### 8.1 Explicit result优先

一旦`report_agent_result`的specialized canonical settlement获得`FULL`：

- 后续普通assistant final text不再成为另一个result；
- coordinator先取得task completion permit；随后同一transaction接受ToolResult、result并完成turn/task；
- result source固定`EXPLICIT`；
- summary/output/diagnostics来自exact tool call args；
- ROOT wait/list读取同一row。

### 8.2 Inferred result

没有explicit candidate时，terminal assistant text形成`INFERRED` result：

- summary为bounded final text；
- oversized正文走既有artifact/ToolResult规则，不新建subagent专用artifact contract；
- 一个prepared terminal candidate在单一repository transaction中接受assistant entry、`INFERRED` result、turn terminal、task terminal及对应occurrences；ACK unknown只确认这一整组`FULL | NONE | CONFLICT`，不允许先提交assistant再补写result。

### 8.3 ROOT可见性

Result不会自动伪装成ROOT user message。三条合法路径：

1. ROOT调用`wait_agent`/`wait_agent_tasks`得到ordinary ToolResult；
2. ROOT controller调用既有`ACCEPT_SUBAGENT_RESULT`，把chosen result作为ROOT external-result entry接受；
3. Round 5B handoff只投影bounded active/task-board状态，不复制所有result正文。

Child result只对ROOT可操作。`list_agents`展示bounded terminal metadata；若要进入ROOT model input，必须通过wait ToolResult或既有显式result acceptance，不自动注入。

---

## 9. Cancellation、close与failure matrix

| 场景 | canonical outcome | physical outcome |
|---|---|---|
| stop waiting task | `CANCELLED/USER_CANCELLED` | 无child可取消 |
| stop active child | child turn `USER_STOPPED` + task `CANCELLED` exact transaction | cancel exact task |
| Host close | 全部nonterminal `INTERRUPTED/SESSION_CLOSED` | drain/cancel，不detach |
| writer takeover | 全部nonterminal `INTERRUPTED/HOST_TAKEOVER` | 新Host不恢复 |
| dependency failed | downstream `BLOCKED_DEPENDENCY_FAILED` | 不启动child |
| provider/runtime failure | task `FAILED` + sanitized reason | release capacity |
| ROOT非bypass调用任一orchestration tool | typed `subagent_requires_bypass_mode` blocked result；无task状态变化 | provider tool call已发生，但attempt/owner/query/mailbox/cancel/capacity均为0 |
| message target closes before enqueue | send `TOOL_UNAVAILABLE` | 无mail |
| message queued后Host close | target `INTERRUPTED`；无message entry也合法 | 丢process-local mail |
| message delivery ACK unknown | confirm FULL/NONE/CONFLICT | 不重复entry |
| explicit result与stop竞态 | exact canonical winner；FULL result优先于late cancel | loser仅确认 |

Cancellation cause仍使用Round 7 exact per-turn process-local intent。不得把ROOT message、dependency failure或provider exception压成generic cancellation。

---

## 10. Prefix、continuity、compaction与restart

### 10.1 Strict-prefix

每个task继续拥有独立：

~~~text
ProviderInputContinuityScope(
  session_id,
  SUBAGENT_TASK,
  task_id,
)
~~~

同scope同epoch：

~~~text
SYSTEM[n+1] == SYSTEM[n]
tools[n+1]  == tools[n]
messages[n] is strict prefix of messages[n+1]
~~~

每个worker task使用自己的new cold epoch，不继承ROOT epoch nonce、mailbox、memory context或TODO owner。

第一次open只能消费`KernelColdEpochInputAssembler`为该exact task/scope/target产生的`ColdEpochInputAssemblyResult`，并由existing continuity candidate/permit exact join同一compiled input与wire plan。后续compatible append仍走normal compiler，不把assembler变成每call wrapper。

### 10.2 Round 9/9.1 capability join与leaf-local refresh

Child permission从创建turn的bypass snapshot冻结；child capability不复制ROOT exposure，而在该task真正启动时从当前owner snapshots构造Round 9 exact child-scope cold cut。该cut及其两个view-bound planner/composer results作为named inputs进入shared assembler，assembler不查询owner或重新决定DIRECT/META/Skill winner。所有worker profile共享同一ordinary child capability policy：

- scope-visible、execution-backed Builtin tools；
- `subagent_visible`且在child cold epoch被选中的DIRECT MCP tools；
- fixed `list_mcp_servers | inspect_new_mcp_tool | use_new_mcp_tool` meta Builtins；
- exact child-scope `MCP_CATALOG | SKILL_CATALOG | ACTIVE_SKILL` provider sources；
- ordinary `read_file | search_files | terminal`等现有能力；
- exact child自己的`report_agent_phase | report_agent_result`。

唯一从leaf删除的是七个ROOT orchestration descriptors。该删除由Round 9 Builtin scope projection机械完成，不由profile allowlist、permission mode或prompt文案决定，也不修改Round 9 capability identity、MCP route或Skill filesystem owner。

每个leaf独立执行Round 9/9.1的append-only动态发现语义：

~~~text
child cold epoch
  -> before the child capability cut, current child-visible READY MCP may enter native tools[] as DIRECT
  -> current exact-scope Skill winners enter SKILL_CATALOG

late MCP at child safe point
  -> rebuild child-scope registry/projection
  -> append child MCP_CATALOG successor as NEW_MCP_META_ONLY
  -> child list -> inspect -> use
  -> do not modify child native tools[] in the same epoch

Skill filesystem change at child safe point
  -> next child provider planning performs the bounded four-root scan
  -> append VALUE | CLEARED | UNAVAILABLE SKILL_CATALOG successor
  -> child uses ordinary read_file for progressive disclosure
~~~

动态状态绝不在ROOT与leaf或两个leaf之间共享：`NewMcpToolRef`必须绑定exact `SUBAGENT_TASK(task_id)` scope和该leaf continuity epoch；ROOT/foreign leaf的ref、cursor、catalog snapshot或physical borrow均不得复用。`BYPASS_PERMISSIONS`只让已通过child scope admission的真实MCP调用不再请求permission，不能绕过`root_visible/subagent_visible`、route、policy、dirty fence、schema generation、slot或connection gate。Skill正文仍只是untrusted guidance，不能授予MCP或其他能力。

这也是spawn可吸收late capability的唯一机制：若MCP在ROOT epoch建立后、但在child capability cut冻结前成为READY，它可以成为child DIRECT tool；若在child cut之后READY，只能成为该child epoch的`NEW_MCP_META_ONLY` suffix。不得为了与ROOT保持cache prefix而压制这次合法cold reconstruction。

### 10.3 Round 5B integration

Round 5B后续把当前`flat_subagents`handoff改为bounded ROOT task-board projection：

- active/directly actionable task id；
- task id、status、phase；
- dependency counts与omitted count；
- pending message count；
- 不包含mail body、child raw transcript或所有result正文。

Compaction不恢复mailbox，不晋升result authority。一个active child自身compact时，已canonical的`INTER_AGENT_MESSAGE`自然进入其source view；尚在process-local mailbox的message在fence结束后的下一个safe point追加。

Active child compaction对parent context使用同一条只读规则：`NONE`没有source；`LAST_N`复用child首次安装的exact `PARENT_CONTEXT` body/fingerprint，不重新读取ROOT、扩大N或吸收后续ROOT消息。若该Host-local material与installed source head发生invariant conflict，compaction typed失败并保留旧child epoch；不得猜测重建，也不得新增durable context snapshot。

此时Round 5B caller改为构造`CompactionContinuationSeed`，但仍调用同一个`KernelColdEpochInputAssembler`；不得从初始`SubagentInitialSeed`复制一套child-compaction renderer。Seed不同只表示conversation base不同，不改变SYSTEM/tool/source placement或physical installation owner。

### 10.4 Restart

Cross-restart只承诺：

- task/dependency/result history可查询；
- child canonical transcript可检查；
- nonterminal task已被takeover/close终结；
- terminal result仍可被ROOT显式接受。

不承诺：

- 继续child provider thread；
- 恢复waiting dependency scheduler；
- 重发message mailbox；
- 恢复phase；
- 重新取得旧capacity reservation。

---

## 11. Protocol与UI

### 11.1 Canonical control projection

`SubagentTaskControl`扩展：

~~~text
task_id
batch_id/task_key/label/profile
status/pending_reason/terminal_reason
dependency_ids
objective preview
phase (live overlay only)
result id/source/accepted
~~~

Canonical snapshot从relations读取；phase和mailbox count只能由same-Host live overlay提供，GAP后允许为空。

### 11.2 Live event

复用`SubagentProgress`，payload增加closed optional fields：

~~~text
phase
pending_count
dependency_status
~~~

它仍是process-local presentation，不承担canonical状态转移。attach/GAP按照现有baseline-first then owner snapshot顺序重建，不从Live events replay task board。

### 11.3 用户控制

Round 10不新增TUI直接给child发message或编辑DAG的UI。Controller继续支持list/stop/result accept；model工具先完成产品面。高级task-board交互属于后续UI round。

---

## 12. Implementation slices

### R10-0：Schema与closed DTO

- 扩展task/status/result字段；
- 新增dependency relation；
- 新增`INTER_AGENT_MESSAGE` entry/event；
- 实现prepared batch/status/result/message candidates与fingerprints；
- clean-v0 reset/deep verify。

### R10-1：统一coordinator与flat API迁移

- 将当前`KernelSubagentManager`收敛为Host-wide coordinator；
- 复用Round 5B `R5B-A0`唯一neutral `KernelColdEpochInputAssembler`；若编码顺序由Round 10先落该seam，只能在同一neutral module按R5B-A0 contract实现，不得创建subagent-private wrapper或提前实现compaction；
- `spawn_agent`复用single-item batch；
- provider DTO统一`task_id`；
- 实现`NONE | LAST_N` parent-context selection、untrusted quote projection与prepared start-material settlement；
- child first open构造`SubagentInitialSeed`并通过shared assembler使用当前capability cold reconstruction，不复用ROOT provider prefix；
- ROOT-only orchestration surface与child report-only surface；
- central Host-global capacity。

### R10-2：Batch/DAG/wait/stop

- 原子batch admission；
- exact dependency scheduler；
- partial multi-wait；
- unified stop；
- failure/block cascade；
- list task board。

### R10-3：Phase/result reporting

- child-bound authorization；
- process-local phase settlement；
- explicit result的specialized atomic canonical settlement；
- inferred fallback；
- ROOT wait/result acceptance。

### R10-4：Inter-agent message

- `send_agent_message` descriptor/executor；
- mailbox owner与linearization；
- safe-point batch consumption；
- final-answer/tool-group races；
- reader/lowering/BASE_SYSTEM guardrail。

### R10-5：Protocol、compaction contract与activation

- Protocol v3/Go projections；
- Round 5B handoff wording；
- Gap Index标记ACTIVATED；
- architecture/oracle/evidence；
- real-provider ROOT-orchestrated dogfood。

每个slice完成后均可在同一feature branch继续，但只有R10-0..5全部通过才能标记ACTIVATED。不得把未完成的DAG或mailbox descriptor暴露到production tool surface。

---

## 13. Test matrix

### 13.1 Happy path

1. ROOT spawn child，child报告phase并explicit result，ROOT wait取得result。
2. ROOT batch创建A/B/C，B依赖A、C依赖B；A/B完成后按序自动启动。
3. ROOT创建多个independent worker；coordinator严格遵守Host-global并发上限并公平启动pending task。
4. ROOT给running child发送message；child下次provider call看到typed message并修订结论。
5. child正在执行tool时收到message；message严格位于完整tool group之后。
6. `settle=first`返回第一个terminal，其他task继续；后续`all`返回全部。
7. `context=NONE`时child只看到objective与自身重建authority，不看到ROOT conversation。
8. `context=LAST_N(2)`时child看到spawn-producing cut中最近两个user-led units；当前open user unit保留，任何tool group均未被切开。

### 13.2 Scope与固定两层边界

- 四种ROOT permission mode的七个orchestration descriptors及其ordered provider `tools[]` bytes完全相同；
- `READ_ONLY | ASK_PERMISSIONS | ACCEPT_EDITS`下逐一调用七个tools，均在attempt前得到`subagent_requires_bypass_mode`，repository/coordinator/physical invocation计数为0；
- `BYPASS_PERMISSIONS`下七个tools分别进入其正常authorize/invoke路径；invoke使用不同或stale permission snapshot时仍拒绝；
- child surface没有任何spawn/create/list/wait/stop/message orchestration tool；
- child绕过surface直接invoke orchestration port仍typed拒绝；
- 五种worker profile逐项证明permission snapshot均exact等于ROOT creation-turn `BYPASS_PERMISSIONS`，ordinary Builtin/MCP/Skill capability集合除语义版本变化外相同；
- ROOT后续切换到非bypass mode不回写已经ACTIVE child的permission snapshot；显式stop/Host close仍按既有terminalization路径生效；
- child可为自己的exact task调用`report_agent_phase/result`；
- child-visible cold MCP可DIRECT调用；ROOT-only/scope-invisible MCP即使在bypass下仍不可见且无法invoke；
- child epoch内late-ready MCP只追加其本地`MCP_CATALOG` successor，`tools[]`保持不变，child经list/inspect/FULL-install/use成功调用；ROOT或另一child的ref/cursor typed stale；
- child运行中安装、修改、删除Skill后，下一safe point产生其本地`SKILL_CATALOG VALUE | CLEARED | UNAVAILABLE`正确successor，普通`read_file`可采用正文；
- task admission后、child实际启动前READY的child-visible MCP可进入child DIRECT surface；child cut之后READY的同类MCP只能走该leaf meta route；
- ROOT与child第一call不要求SYSTEM/tools/messages prefix相等；child first open完成后，同一child epoch继续满足strict prefix；
- ROOT list只读且bounded；
- cross-session/workspace/task id拒绝；
- 所有child profile都看不到七个ROOT orchestration tools；
- ROOT/child/different child continuity完全隔离。

### 13.3 Dependency

- same-batch forward reference；
- existing same-session ROOT-owned task reference；
- unknown/self/cycle rejection且零row；
- cross-session/workspace dependency rejection；
- upstream FAILED/CANCELLED/INTERRUPTED/BLOCKED逐项cascade；
- concurrent dependency completion只有一个start winner；
- capacity不足保持PENDING_START并公平启动；
- Host close使waiting/pending/active全部INTERRUPTED且reopen不启动。

### 13.3.1 Parent context

- omitted `context`精确等于`NONE`；unknown mode/field与`mode=last_n, turns=0|9`在task admission前拒绝；
- `LAST_N`选择不足N个unit时返回全部available units；
- latest open user unit可选，spawn assistant/tool group不进入自己的parent context；
- assistant ordered blocks和tool call/result group不可拆分；
- ROOT SYSTEM/tools/runtime sources、hidden reasoning、provider-private replay及scope-bound opaque handles不迁移；
- `PARENT_CONTEXT`固定为`UNTRUSTED_OBSERVATION/SNAPSHOT/VALUE`，不能授予permission、MCP、Skill或file access；
- parent source自身physical overbound拒绝整个atomic batch，task/dependency/event row均为0；child start aggregate overbound则typed `FAILED`且provider open为0；两者都不静默缩小N或退化为NONE；
- dependency延迟启动仍使用creation-time frozen context；ROOT后续消息不改变它；
- batch共享一个private root cut并以per-task selection引用，内存probe证明无正文deep-copy；
- direct FULL、ACK confirmation与queued/batch settlement均exact安装prepared context；caller cancellation只能detach；
- `NONE`与`LAST_N`分别形成closed `SubagentInitialSeed`并进入唯一shared assembler；wrong task/scope/target/parent-view或mixed seed typed拒绝；
- shared assembler两阶段result中的compiled input、wire plan与cold candidate inputs共同进入第一次continuity CAS；fresh child selection的replay hydration request/proof必须为NONE，DirectModel不得重新lower另一份child input；
- 单独调用assembler不读取repository/owner、不取得physical borrow、不安装continuity且provider open为0；
- Host close丢弃prepared body并terminalize nonterminal tasks；新Host不恢复parent context；
- active child compaction复用installed `PARENT_CONTEXT`，不重新读取ROOT或扩大N；
- Chat Completions与Responses均不要求ROOT/child prefix兼容，child first open后的same-epoch strict-prefix仍成立。

### 13.4 Messaging races

- send vs finalizer两种linearization；
- send during provider streaming；
- send duringtool execution；
- multiple orderedmessages in one safe-point batch；
- mailbox item/body bounds；
- delivery ACK unknown FULL/NONE/CONFLICT；
- Host close丢未consumed mail但interrupt target；
- message不会被lower为USER_MESSAGE/USER_STEER；
- message不触发memory/cheap-hint/textual Skill；
- sameepochSYSTEM/tools不变、messages只追加suffix。

### 13.5 Result/cancel

- explicit result不产生额外model call；
- explicit与inferred互斥；
- report result vs stop/Host close；
- result accepted into ROOT exact once；
- repeated wait idempotent；
- oversized result arguments在attempt前typed拒绝，且无ToolResult/result/task-terminal假成功。

### 13.6 Physical bounds

- 4个active child + queued runnable task；
- global active-worker cap；
- 16-task/64-edge batch；
- 32-target wait；
- mailbox 16 items/64 KiB；
- load probe证明无per-child manager/executor duplication和无continuity body deep-copy。

### 13.7 Architecture guards

- 无durable subagent job/lease/receipt/checkpoint/replay/repair；
- 仓库只有一个neutral `KernelColdEpochInputAssembler`；不存在child/compaction/private prompt builder或第二套SYSTEM/tools/source placement；
- assembler没有repository/Host/current-owner read、physical borrow、provider open、continuity CAS或durable state依赖；
- dependency relation只有canonical product writer/reader；
- Live event不成为task authority；
- no raw provider/model/MCP object进入canonical task rows；
- child profile不接受raw allowlist；
- oracle精确`32 / 24 / 13 / 2 / 27 / 1`；
- dormant descriptor为0，catalog与sealed executor双向exact。

---

## 14. 预计修改面

~~~text
src/pulsara_agent/storage/migrations/sql/0000_conversation_kernel_baseline.sql
src/pulsara_agent/storage/migrations/manifest.py

src/pulsara_agent/conversation_kernel/subagent.py
src/pulsara_agent/conversation_kernel/_repository/subagents.py
src/pulsara_agent/conversation_kernel/contracts.py
src/pulsara_agent/conversation_kernel/vocabulary.py
src/pulsara_agent/conversation_kernel/runner.py
src/pulsara_agent/conversation_kernel/host.py
src/pulsara_agent/conversation_kernel/tool_runtime.py
src/pulsara_agent/conversation_kernel/reader.py
src/pulsara_agent/conversation_kernel/context_sources.py
src/pulsara_agent/conversation_kernel/cold_epoch.py

src/pulsara_agent/capability/builtin_catalog.py
src/pulsara_agent/model_input/contracts.py
src/pulsara_agent/model_input/lowering.py

src/pulsara_agent/ports/live_agent_event.py
src/pulsara_agent/terminal_protocol/schema/terminal_kernel_v3.proto
src/pulsara_agent/terminal_protocol/canonical_v3.py
src/pulsara_agent/terminal_protocol/v3_gateway.py
clients/terminal/...
~~~

预计新增subagent-specific production Python module不超过4个，建议：

~~~text
conversation_kernel/subagents/contracts.py
conversation_kernel/subagents/coordinator.py
conversation_kernel/subagents/mailbox.py
conversation_kernel/subagents/scheduler.py
~~~

repository继续留在现有模块化`_repository/subagents.py`，不要恢复旧`runtime/subagent/`十余文件的reducer/projection层。

Neutral `conversation_kernel/cold_epoch.py`属于Round 5B `R5B-A0`共享基础，不计作第五个subagent模块；仓库中只能有这一份production cold-epoch assembler。

---

## 15. Explicit non-goals

- cross-Host child continuation；
- durable mailbox或message redelivery；
- task retry/reset/redefine；
- same task多attempt；
- Codex-style idle `followup_task`；
- persistent AgentThread/persona identity；
- recursive child spawning或child orchestration tools；
- ROOT provider-prefix fork、`FULL_PREFIX_FORK`、`FULL_SEMANTIC`或无界parent history继承；
- profile-specific child permission narrowing或ordinary capability allowlist；
- child-to-ROOT与worker-to-worker messaging；
- arbitrary workflow DSL/Deno/Python scheduler；
- dynamic task graph backpatch；
- cross-session/cross-workspace dependency；
- child直接human interaction或Plan mode；
- raw child transcript自动注入ROOT；
- model-authoredtool allowlist/permission；
- Plugin subagent manifest execution；
- advanced task-board TUI。

---

## 16. Definition of Done

Round 10只有同时满足以下条件才可ACTIVATED：

1. 9个最终subagent tools全部拥有sealed production binding，无dead descriptor；
2. 七个ROOT orchestration descriptors跨四种permission mode保持同一tool surface，只有`BYPASS_PERMISSIONS`可执行；child report-only surface、global capacity与scope isolation通过；
3. batch/dependency/start/block/stop/wait状态机由canonical relations闭合；
4. phase是process-local，explicit/inferred result是canonical且互斥；explicit result与其ToolResult/turn/task同事务闭合；
5. `send_agent_message`只允许ROOT向ACTIVE ROOT-owned child queue，safe-point后形成`INTER_AGENT_MESSAGE`；
6. message/tool-group/final-answer/close的全部竞态有exact test；
7. no `followup_task`、no task reopen、no second AgentThread identity；
8. Host close/takeover将全部nonterminal task terminalize，新Host不恢复execution；
9. 所有worker exact继承creation-turn bypass snapshot；Round 9/9.1 child-scope Builtin、DIRECT/late-meta MCP与dynamic Skill refresh exact join，且ROOT/leaf/foreign-leaf动态ref与catalog完全隔离；
10. context contract只有`NONE | LAST_N(1..8)`；parent context绑定spawn-producing ROOT cut、完整group与creation-time selection，且使用untrusted quote projection；
11. Child first open用`SubagentInitialSeed`调用唯一shared assembler，按启动时current capability cold reconstruction并复用既有compiled/wire/continuity artifacts；没有subagent-private prompt builder；
12. Child不继承ROOT prefix；Chat Completions与Responses均证明child first-open wire来自same assembly，随后same-epoch SYSTEM/tools不变、messages只追加suffix；
13. Protocol/Go、full pytest、PostgreSQL、Ruff、compileall、generator、Go test/vet/module verify全部通过；
14. clean-v0 fresh/repeat/deep verify/reset-required通过；
15. architecture oracle为`32 / 24 / 13 / 2 / 27 / 1`；
16. real-provider dogfood至少覆盖ROOT batch workers、`NONE`、`LAST_N`、pre-start MCP promotion、active message steer、dependency chain与explicit result；
17. activation evidence不记录prompt、parent/child正文、hidden reasoning、credential、DSN或环境敏感信息。

---

## 17. 最终判断

Round 10恢复的是hard-cut前已经证明有产品价值的task orchestration，而不是旧的durable graph engine。最小正确拓扑是：

~~~text
canonical task/dependency/result history
        +
one Host-local ROOT orchestration coordinator
        +
one boundary-safe inter-agent mailbox
        +
existing per-task transcript/continuity/tool runtime
~~~

Agent topology恒为`one ROOT parent + many worker leaves`。Dependency graph只属于ROOT scheduler；它不产生第二层parent、subtree或worker-to-worker authority。Leaf拥有完整ordinary execution capability与leaf-local动态discovery，但永远不拥有orchestration capability。

Spawn是一次新的child cold construction，不是ROOT provider-prefix fork。`NONE | LAST_N`足以覆盖“完全由objective驱动”与“需要少量parent原话/近期tool evidence”两类实际worker任务；删除full-history模式同时消除了不确定预算、无关上下文扩散、scope-bound handle迁移和ROOT/child cache兼容的伪命题。

Cold construction本身也不是subagent专属机制：Round 10只提供`SubagentInitialSeed`与child-scope frozen authorities，最终SYSTEM/tools/messages/wire/continuity candidate由Round 5B抽出的唯一neutral assembler构造。这样compaction rebase与spawn共享正确的channel placement和wire proof，却仍各自拥有summary、task、permission与lifecycle。

Codex最值得吸收的不是recursive agent tree或它的存储方式，而是“typed communication、queue-only message与trigger-turn followup必须分开”。Pulsara本轮只需要ROOT向running worker的queue-only message；terminal task之后继续工作，用新task表达比复活旧task更符合append-only与dependency语义。
