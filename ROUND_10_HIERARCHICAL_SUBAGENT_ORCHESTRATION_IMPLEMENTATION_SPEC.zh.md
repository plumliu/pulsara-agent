# Round 10：ROOT-Orchestrated Subagent Task Graph 与 ROOT-to-Worker Messaging 实施规格

> 状态：**ACTIVATED**
>
> Fingerprint hard-cut：[`PULSARA_FINGERPRINT_SUBTRACTION_HARD_CUT_IMPLEMENTATION_SPEC.zh.md`](PULSARA_FINGERPRINT_SUBTRACTION_HARD_CUT_IMPLEMENTATION_SPEC.zh.md)覆盖本文冗余的same-process DTO fingerprint/proof字段以及所有逐文件、文档与activation evidence SHA门禁；canonical task/result/message identity与provider-prefix边界digest继续有效。
>
> 记录日期：2026-08-20
>
> 本次产品收口修订：2026-08-21
>
> 实际编码基线：`504b8c8fe509ec55c12acad97f32ee6d24687932`（Round 5B ACTIVATED clean checkpoint）
>
> hard-cut 前产品参考基线：`5b7ad9f7ffc8565bc572180b2bde0c81ab64473a`
>
> 编码前置：Round 9、Round 9.1与Round 5B均已ACTIVATED。本文直接复用当前production Builtin/MCP/Skill capability cut、sealed Builtin composition、exact child tool surface与唯一`KernelColdEpochInputAssembler`；不得在Round 10分支补一份A0、临时child capability profile或child prompt builder。Round 5B预留的`SubagentInitialSeed`只是粗粒度consumer seam；Round 10必须按本文的exact-object契约收紧并bump process-local cold-assembler contract，不改变Round 5B已激活的normal/compaction语义。
>
> 上位契约：[Stage 2 hard cut](STAGE_2_HARD_CUT_IMPLEMENTATION_SPEC.zh.md)、[Round 3 structured compiler](ROUND_3_STRUCTURED_MODEL_INPUT_COMPILER_IMPLEMENTATION_SPEC.zh.md)、[Round 3.1 provider-input prefix continuity](ROUND_3_1_PROVIDER_INPUT_PREFIX_CONTINUITY_IMPLEMENTATION_SPEC.zh.md)、[Round 4 Plan/permission](ROUND_4_PLAN_WORKFLOW_AND_RUN_PERMISSION_IMPLEMENTATION_SPEC.zh.md)、[Round 5A execution envelope](ROUND_5_LONG_HORIZON_EXECUTION_ENVELOPE_IMPLEMENTATION_SPEC.zh.md)、[Round 5A.2 durable provider replay](ROUND_5A_2_DURABLE_PROVIDER_REPLAY_AND_CROSS_RESTART_THREAD_CONTINUATION_IMPLEMENTATION_SPEC.zh.md)、[Round 7 model-visible outcome](ROUND_7_MODEL_VISIBLE_FAILURE_AND_TOOL_OBSERVATION_IMPLEMENTATION_SPEC.zh.md)、[Round 7.1 ToolResult projection](ROUND_7_1_PROVIDER_VISIBLE_TOOL_RESULT_PROJECTION_IMPLEMENTATION_SPEC.zh.md)、[Round 9 unified capability](ROUND_9_UNIFIED_CAPABILITY_SEMANTICS_IMPLEMENTATION_SPEC.zh.md)、[Round 9.1 Agent Skills](ROUND_9_1_AGENT_SKILLS_STANDARD_IMPLEMENTATION_SPEC.zh.md)、[Gap Index PHC-10](POST_HARD_CUT_PRODUCT_CAPABILITY_GAP_INDEX.zh.md#9-phc-10hierarchical--batch-subagent-task-graph)
>
> 共享Runtime seam：[Round 5B §10.1.1 shared cold-epoch assembler](ROUND_5B_LONG_HORIZON_CONTEXT_COMPACTION_IMPLEMENTATION_SPEC.zh.md#1011-shared-kernelcoldepochinputassembler)。Round 10是该已激活production seam的第三个consumer：它只新增sealed subagent seed与child-scope frozen inputs，继续复用同一semantic compile、wire plan与continuity candidate路径。
>
> 本轮集成：将Round 5B已激活的flat-subagent runtime handoff原地升级为hierarchical task-board carrier。下游仅保留future Round 9.2 Plugin Subagent contribution。

本文恢复 hard-cut 前已经存在的 **batch task、dependency、terminal result 与 task board** 产品能力，并把当前单个 flat child 扩展成由ROOT统一管理的有界并行task graph。这里的“层次化”严格表示一个星型拓扑：`one ROOT parent -> many worker leaves`。**不允许subagent继续创建subagent，也不存在worker作为另一个worker的parent。**旧实现只作为产品状态机和测试语义的参考；其模型主动上报phase、EventLog reducer、projection/hydration、checkpoint、repair、child execution recovery、跨 Host resume 一律不恢复。

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
  report_agent_result
~~~

七个ROOT orchestration descriptors属于固定Builtin surface：只要当前caller scope为ROOT，它们就在`READ_ONLY | ASK_PERMISSIONS | ACCEPT_EDITS | BYPASS_PERMISSIONS`四种permission mode的provider `tools[]`中保持完全相同。permission mode不得通过增删descriptor改变provider tool prefix。

“可见”不等于“可成功执行”。七个ROOT orchestration tools统一归类为现有`subagent_parent` builtin family；只有exact caller permission snapshot为`BYPASS_PERMISSIONS`时才可通过local authorize。其他三种mode中的任意调用都必须在attempt admission之前返回typed blocked result，固定reason为`subagent_requires_bypass_mode`，且不得创建task/dependency/turn、建立waiter、写mailbox、发出cancel或取得child capacity。`list_agents`、`wait_agent*`等只读/等待操作也不例外；本轮不建立“只读编排可放宽”的第二张permission matrix。

唯一child self-report tool不是parent orchestration，也不归类为`subagent_parent`。它继续受exact child scope、task lifecycle和参数bounds约束。所有worker profile拥有同一份ordinary permission/capability policy；profile不得缩窄普通工作能力或影响result协议。

当前dormant `report_agent_phase` descriptor与任何旧phase executor一并删除。Worker进度由canonical task status、dependency state与Runtime-derived pending state表达；模型不需要持续打断工作来报告“正在做什么”。

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
  owns asyncio task, cancellation intent, capacity and Runtime-derived task status
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

这是唯一合法的agent topology。`A -> B` dependency同时表示“ROOT scheduler必须等待A terminal成功后才能启动B”以及“B启动时获得A的统一terminal result summary”；它不是A拥有B、A向B派工或B继承A transcript/完整上下文。worker之间没有parent/child、ancestor/descendant、subtree、sibling mailbox或直接通信语义。

每个task冻结：

- exact ROOT创建turn；
- immutable objective、profile、`NONE | LAST_N` context policy与dependency edges；
- current lifecycle与terminal result。

Terminal result不是“发给ROOT的一条消息”，也不保存recipient。它是该task唯一的canonical output：Runtime把它自动投影给所有**直接下游依赖者**，ROOT则只能通过显式`list/wait/accept`观察或接纳。无论worker调用`report_agent_result`，还是直接输出final assistant answer，最终都形成同一种terminal result routing；工具只改变结果的producer shape，不改变收件人语义。

所有task创建、list、wait、stop与message控制只允许ROOT，且只有ROOT当前exact permission mode为`BYPASS_PERMISSIONS`时才能成功。七个descriptor仍稳定暴露在所有ROOT permission mode的provider `tools[]`中。child provider surface绝不暴露`spawn_agent`、`create_agent_tasks`、`list_agents`、`wait_agent*`、`send_agent_message`或`stop_agent`；child只能执行普通工作并在需要结构化终结时调用自己的`report_agent_result`。dependency只表达ROOT创建的worker task之间的调度先后与direct-result data edge，不产生递归agent hierarchy。

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

Round 5B ACTIVATED后的当前真值为`28 / 24 / 11 / 1 / 24 / 0`。因此目标oracle为：

~~~text
Committed events       29
Live events            24
subject slots          11
append guards           1
product relations      25
durable jobs            0
~~~

`SubagentProgress`只承担Runtime-derived task/status/pending-count的process-local UI更新，不承载模型自报phase，也不新增第二个task-board Live event。

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
- child `report_agent_result`与terminal assistant inferred result；
- explicit result优先于inferred final assistant text；
- bounded task inventory与统一task board。

主要参考：

- `5b7ad9f7:src/pulsara_agent/ports/subagent.py`的closed command/outcome；
- `5b7ad9f7:src/pulsara_agent/runtime/subagent/tool_port.py`的dependency纯函数；
- `5b7ad9f7:src/pulsara_agent/runtime/subagent/runtime.py`的调度状态转移；
- `5b7ad9f7:tests/test_subagent_runtime.py`中的batch、dependency、wait、result与failure propagation测试。

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

Codex V2的`send_message`可解析已知agent path/thread，测试也明确允许child向ROOT发送。Round 10不照搬这组任意已知target语义：本轮只有ROOT向其ACTIVE worker发送指导；child只提交统一terminal task output，由Runtime投影给direct downstream，并供ROOT显式wait/accept。这样spawn/list/wait/stop/message全部共享ROOT-only访问矩阵，不需要agent path、root inbox、sibling通信或跨层路由。若后续真实产品需要双向协作，应单独扩展message access matrix，而不是把它藏进task-id解析。

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
| current physical child/capacity/mailbox/prepared parent context | `RootSubagentCoordinator` | database replay、Committed events |
| child permission | exact inherited ROOT creation-turn `BYPASS_PERMISSIONS` snapshot | profile name、ROOT model prose、后续ROOT mode变化 |
| child tool surface | Round 9 exact scope capability cut + physical binding | task metadata、Skill text |
| task terminal output | exact terminal `subagent_task_children.RESULT` | Live summary、provider response、mailbox |
| downstream dependency input | direct dependency RESULT rows的exact ordered pure projection | ancestor transcript、ROOT prose、scheduler guess |
| ROOT-visible accepted result | explicit `ACCEPT_SUBAGENT_RESULT` or wait ToolResult | automatic background injection |

### 2.2 一个Host-wide coordinator

不得为每个child创建独立manager。使用一个Host-wide：

~~~text
RootSubagentCoordinator
  live_tasks: task_id -> LiveSubagentExecution
  root_owned_task_inventory
  dependency_ready_queue
  per_task_message_mailboxes
  per_task_parent_context_start_material
  one global capacity counter
~~~

Round 10不再新增一个与现有tool runtime重叠的`SubagentInvocationOwner`。它扩展既有`KernelToolInvocationContext`，让产生本次orchestration tool call的**实际model call**携带一个repr-safe、process-local exact subject：

~~~text
FrozenSubagentParentContextCallSubject
  session_id
  scope = ROOT
  caller_turn_id
  provider_input_cut_fingerprint
  continuity_epoch_nonce / revision
  compiled_semantic_input_fingerprint
  compiled_message_placements_fingerprint
  ordered eligible ROOT context-unit tail facts (maximum 3)
  eligible_context_units_fingerprint
  subject_fingerprint
~~~

在`KernelToolInvocationContext`中该字段是closed optional：ROOT orchestration tool request必须为VALUE；child report与所有ordinary tools必须为NONE。携带VALUE但caller/tool family不匹配属于typed invariant failure，不能静默忽略。

该subject只能在本次provider input最终compile、wire preflight与continuity CAS安装成功后由sealed factory冻结，并与同一`_PreparedProviderDispatch`、assistant tool-call batch及`KernelToolInvocationContext`逐对象exact join。它只通过structural sharing引用最多3个eligible tail facts，不复制整份compiled history；不包含hidden reasoning、provider-private replay body、physical borrow或mutable registry，也不进入canonical row。`KernelSubagentToolPort`必须接收这个exact object，而不是只接收`parent_turn_id`或一串可由调用者重建的hash；CAS/dispatch重建失败时丢弃整个subject并重做planning，不建立`fingerprint -> snapshot` mutable map。

Coordinator据此构造scope-filtered view。orchestration operations必须机械证明caller scope为ROOT；report operations继续通过既有invocation context机械绑定caller自己的exact`SUBAGENT_TASK(task_id)`scope。调用者不得通过参数伪造ROOT或另一个task。

### 2.3 ROOT orchestration与child self-report

~~~text
caller ROOT
  always sees create/list/wait/stop/message descriptors
  may successfully invoke them only under BYPASS_PERMISSIONS

caller SUBAGENT_TASK(A)
  may only report result for A
  may not orchestrate any task
~~~

所有新task的创建turn必须属于ROOT scope且其exact permission snapshot必须为`BYPASS_PERMISSIONS`。任何child调用orchestration descriptor都应在tool surface构造阶段不可见；invoke seam仍保留ROOT exact-scope防绕过校验。ROOT descriptor exposure不得按permission mode变化；permission只在local authorize与invoke防绕过seam重验。

### 2.4 Scope访问矩阵

| 操作 | ROOT + `BYPASS_PERMISSIONS` | ROOT + 其他mode | child |
|---|---:|---:|---:|
| 创建batch/single task | 是 | blocked | 否 |
| list/wait/stop/message task | 是 | blocked | 否 |
| report own result | 否 | 否 | 是 |
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
- `context_mode=LAST_N` iff `1 <= context_last_n_turns <= 3`；
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
- 同一exact ROOT `parent_turn_id`经任意数量的`spawn_agent | create_agent_tasks`调用累计最多接受16个task；repository必须在writer transaction中锁定并重验该累计值，不能只检查单次batch；
- batch事务内使用预生成task ids解析forward reference并拒绝cycle；
- 既有task的dependency集合永不backpatch。

本relation只表达产品DAG的调度先后与direct-result data edge，不拥有scheduler、result副本、delivery state或recovery。

### 3.3 `subagent_task_children`扩展

现有relation继续表达child产生的ordered MESSAGE与唯一terminal RESULT。RESULT增加nullable closed字段：

~~~text
result_source            EXPLICIT | INFERRED
summary                  bounded nullable for MESSAGE, required for RESULT
output_preview           bounded nullable
diagnostics              bounded JSON array
result_fingerprint       nullable for MESSAGE, required for RESULT
~~~

`subagent_task_children.entry_id`在RESULT row上明确是**producer provenance**：`EXPLICIT`指向本task`report_agent_result`的exact acknowledgement ToolResult entry，并可沿既有attempt/result relation回到assistant tool request；`INFERRED`指向terminal assistant entry。两者不得同时存在。该entry不是ROOT交付正文的隐式来源，尤其不得把EXPLICIT acknowledgement误当成result content。

RESULT row没有`recipient_task_id`或`delivered_to_root`字段。一个immutable result可以被任意数量的direct downstream tasks以pure projection消费，也可以被ROOT显式读取；不得为fan-out复制result row或写N条delivery relation。

Repository与controller共享一个pure `FrozenSubagentResultPublicFact` builder：

~~~text
FrozenSubagentResultPublicFact
  task/result identity
  result_source
  producer_entry_id
  structured summary/output_preview/diagnostics       # EXPLICIT
  exact source assistant public-content digest         # INFERRED
  bounded list/wait preview
  acceptance content projection
  result_fingerprint
~~~

`EXPLICIT`的list/wait与ROOT acceptance都从structured fields构造；`INFERRED`的list/wait只使用确定性bounded preview，而ROOT acceptance从exact source assistant public content构造。既有`ACCEPT_SUBAGENT_RESULT`必须exact join同一`result_fingerprint`，不能读取producer ToolResult acknowledgement正文来替代result，也不增加第二张result relation、额外transcript row或result artifact authority。

下游只消费同一public fact的统一bounded summary，不读取producer entry：

~~~text
FrozenDependencyResultContextItem
  dependency_ordinal
  dependency_task_id / optional task_key / label
  result_id
  result_source
  exact bounded result summary
  result_fingerprint
  item_fingerprint

FrozenDependencyResultContext
  exact target task/scope
  ordered direct dependency items
  context_fingerprint
~~~

`EXPLICIT` summary来自required tool argument；`INFERRED` summary来自同一result row的16,384-byte UTF-8-safe HEAD_TAIL。两者对下游使用完全相同的shape。`output_preview`、diagnostics、raw assistant content、tool transcript与祖先结果不进入该carrier；详细产物应写入workspace或已有artifact，并在summary中给出可操作位置。

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

`source_inter_agent_tool_attempt_id`在same session中使用partial `UNIQUE`，并由deferred composite FK/constraint trigger exact证明该attempt属于同session、tool name为`send_agent_message`且目标entry是它唯一消费出的inter-agent message。一个send attempt最多产生一个target entry；不得仅靠application查询避免重复消费。

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

`mode=last_n`时`turns`必须为`1..3`；`mode=none`时不得出现`turns`。不得接受`all`、`full`、负数、字符串化数字或unknown field。

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
context = {mode: none} | {mode: last_n, turns: 1..3}
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
  "include_dependencies": true,
  "cursor": "optional opaque next_cursor"
}
~~~

结果按`accepted_at, task_id`确定性排序并使用keyset pagination。第一页不传`cursor`；存在后续行时返回bounded opaque `next_cursor`，下一页必须原样复用同一`max_items`与`include_dependencies`。Cursor由当前Host签发并绑定exact session、query shape、ordering key、last accepted row与已返回数量；伪造、跨session、改变query或Host重启后的cursor返回typed `INVALID_CURSOR | STALE_CURSOR`。它不是durable cursor row、offset registry或恢复authority。

Cursor的签名与lineage校验发生在schema验证和canonical attempt acceptance之后，因此两类typed拒绝的outer ToolResult state统一为`APPLICATION_ERROR`，正文保留精确error code；不得返回只允许no-attempt路径使用的`INVALID_ARGUMENTS`并制造attempt/result union冲突。

每页返回：

- task id；
- task key/label/profile；
- objective preview；
- status/pending reason/terminal reason；
- dependency ids/status；
- terminal result id、source与bounded summary；
- pending ROOT-message count（只在当前Host存在时）；
- page/total/remaining omitted counts；
- `next_cursor | null`。

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

### 4.6 `report_agent_result`

工具名保持不变，但这里的“report”指向Runtime提交本task output，不表示向ROOT发送。Runtime依据dependency edges自动让direct downstream消费；ROOT没有隐式收件权。

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
- `report_agent_result`必须是该assistant response中唯一的tool call；若同一batch还包含任意ordinary tool或第二个`report_agent_result`，Runner必须在普通attempt admission前原子拦截整个batch，为每个request返回typed `INVALID_ARGUMENTS`、创建零attempt/零physical side effect、保持task ACTIVE并让child重新输出；不得先执行siblings再发现result终结；
- invoke在物理dispatch前冻结`PreparedExplicitSubagentResultSettlement`；其canonical ToolResult接受事务同时插入exact `subagent_task_children.RESULT`、完成child turn与task并追加对应occurrences；
- specialized settlement的ACK unknown通过同一prepared candidate确认全部row为`FULL | NONE | CONFLICT`，不得出现“ToolResult成功但result/task仍未完成”的中间终局；
- 这里的`FULL`只表示整组canonical rows逐字段确认存在；不要求summary/output正文以provider `FULL` variant再次交付，也不建立result专用provider预算；
- 如果child没有调用本工具而直接给final assistant text，Runtime产生`INFERRED` result；
- explicit与inferred winner只能有一个，ACK unknown使用prepared candidate做`FULL | NONE | CONFLICT`确认；
- 两种winner都只提交task output，不直接向ROOT或下游写message；dependency scheduler在下游start时投影该result，ROOT则继续按需wait/accept。

### 4.7 `send_agent_message`

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

所有profile共享一条stable result rule：terminal result summary可能成为direct downstream的唯一自动输入，因此应自洽陈述结论、关键约束与后续可操作的file/artifact位置；不得假设下游能看到当前worker transcript或tool results。该提示只约束表达，不改变result authority或强制worker必须调用工具。

模型不能提交raw tool allowlist、MCP server id、Skill name、permission mode或provider credential。

`profile`只改变worker的模型行为与结果表达，不改变permission或ordinary capability集合。所有child profile都没有subagent orchestration能力；七个ROOT orchestration tools只出现在ROOT surface。每个child在统一的ordinary child capability surface上只增加自身的`report_agent_result`。不得用profile、prompt文案、隐藏gate或profile-specific allowlist间接恢复child spawn或删减MCP/Skill/ordinary Builtin能力。

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

由于task creation只可能在`BYPASS_PERMISSIONS`下成功，每个worker均以该exact bypass snapshot运行；profile、objective、task metadata和模型输出都不能缩窄、扩大或重解释它。创建后ROOT在后续turn切换permission mode不会回写running worker的frozen snapshot；显式停止或Host close才终结该worker。child不得进入Plan或直接询问用户。ordinary capability仍须通过scope visibility、schema、dirty/unavailable、effect policy、physical binding与hard safety gates；`BYPASS_PERMISSIONS`不会把ROOT-only、scope-invisible或失效能力变成可用。`report_agent_result`是child自有协议出口，不属于`subagent_parent` family。

### 5.3 Context policy

Round 10只有：

~~~text
NONE
LAST_N(turns = 1..3)
~~~

默认`NONE`，这是普通worker的主路径。只有objective单独不足以表达任务、确实需要少量ROOT原话时才显式选择`LAST_N`。两种mode都会给child：

- exact task objective；
- 由child authority重新构造的stable BASE_SYSTEM与project/runtime policy；
- child启动时冻结的Round 9 Builtin/MCP exposure与Round 9.1 Skill catalog；
- 独立child cold continuity epoch。

`NONE`不复制任何ROOT conversation。它不是“没有上下文”：objective和当前child authority始终存在；被省略的只是parent transcript data。

`LAST_N`从`FrozenSubagentParentContextCallSubject`中选择最近N个**ROOT user-turn conversation units**。每个unit由sealed builder在产生当前spawn/create调用的actual provider-neutral compiled input中机械形成：

~~~text
FrozenRootConversationContextUnitFact
  ordered exact ROOT turn/entry identities represented by this provider group
  ordered accepted ROOT USER_MESSAGE items (one or more)
  ordered USER_STEER items already visible in that call
  ordered assistant public message/text items already visible in that call
  unit fingerprint
~~~

规则冻结为：

1. unit的边界是实际送入ROOT provider call的user-turn group，而不是“每条user message各算一个turn”。如果上一turn运行期间通过ordinary non-steer路径连续接纳多条`USER_MESSAGE`，并在下一次model call中一起交付，这些消息必须保持原顺序、整体进入同一个unit；不得只取最后一条，也不得因底层queue item或canonical turn数量把它们拆成多个LAST_N名额；
2. 同turn中已经进入该exact cut的全部`USER_STEER`按真实顺序保留；
3. assistant只保留公开message/text/data内容。assistant entry同时包含tool-call blocks时，只quote公开assistant正文，删除tool-call blocks，且不得生成悬空tool call；
4. **所有tool calls、ToolResult、tool groups、artifact handle、MCP ref和其他tool-derived carrier均不进入parent context**。LAST_N不是ROOT工作证据复制机制；需要工具事实时应写进objective或让worker自己读取canonical source；
5. 最新unit允许是open unit，即包含当前一组accepted ROOT user messages/steers而没有assistant reply。产生spawn/create的assistant tool-call batch本身尚不在其输入cut中，也不得回填；
6. 若历史少于N个eligible units，返回全部available units；不补空项、不跨source floor重扫更早历史。

选择只基于该ROOT call实际可见并已continuity-installed的provider-neutral semantic view，而不是从genesis重扫canonical transcript。它只携带上述public conversational data；明确排除：

- ROOT BASE_SYSTEM、provider tools与runtime-observation carriers；
- hidden reasoning、Round 5A.2 provider-private replay与remote response id；
- current permission/Plan/TODO/memory head等应由authority重建的状态；
- 所有tool request/result group及其MCP ref、pagination cursor、artifact handle、memory citation handle与physical borrow authority。

已知scope/epoch-local augmentation不得原样迁移；`PARENT_CONTEXT`使用provider-neutral quote renderer重新表达选中的user/steer/assistant public items，使其成为背景数据而不是child native history。它不递归解释或清洗opaque user/assistant body。无论正文长得多像指令，carrier固定为：

~~~text
source      PARENT_CONTEXT
trust       UNTRUSTED_OBSERVATION
lifecycle   SNAPSHOT
presence    VALUE
budget      MUST_KEEP
variants    FULL only
body        ordered bounded context units
~~~

有direct dependencies的task还会在真正启动时安装另一个独立低authority source；它不受`NONE | LAST_N`控制：

~~~text
source      DEPENDENCY_RESULTS
trust       UNTRUSTED_OBSERVATION
lifecycle   SNAPSHOT
presence    VALUE
budget      MUST_KEEP
variants    FULL only
body        ordered direct-dependency result summaries
~~~

该body由`FrozenDependencyResultContext`唯一渲染。只包含dependency relation直接指向的task，按`dependency_ordinal`排序；不递归携带祖先结果。每task最多16个依赖、每条result summary最多16,384 UTF-8 bytes，因此完整投影始终位于既有single-source physical bound内；仍须执行normal exact quote。不得为了fit而漏掉某个dependency、裁剪summary或退化成“仅状态”。零依赖时source absent。

稳定BASE_SYSTEM同时说明：dependency summaries是其他worker产生的untrusted collaboration data，不是system policy、permission或外部事实authority；worker应结合objective与当前真实workspace验证后使用。

`NONE`以及`LAST_N`没有eligible unit时不安装`PARENT_CONTEXT`；新child cold epoch中不需要伪造`CLEARED`。`LAST_N`quote projection必须在task admission时整体通过现有single-source physical bound；不fit时整个task/batch typed拒绝且canonical task row为0。Child真正启动时还要把它、exact `DEPENDENCY_RESULTS`与当时的SYSTEM/tools/runtime sources做一次normal compiler preflight；若aggregate不fit，task以typed resource-boundary `FAILED`终结且provider open为0。两层都不允许静默缩小N、拆开同turn的多条user messages、截断unit、遗漏dependency或退化为`NONE`。

Prepared parent context在task admission时冻结；dependency/capacity导致的延迟启动不能改用较新的ROOT history。为了避免batch中多task重复复制正文，同一assistant tool-call batch只引用一个private、repr-safe `FrozenSubagentParentContextCallSubject`，每个task保存自己的selection与projection fingerprint。它是Host-local start material；Host close后nonterminal task统一`INTERRUPTED`，新Host不恢复该body。

`RootSubagentCoordinator`在batch FULL/ACK-confirmed settlement中exact安装这些start materials；caller cancellation只能detach，不能留下已接受task却没有其prepared context。Material在task terminal或Host close后释放；该规则同样覆盖从未启动的`PENDING_START/WAITING_DEPENDENCY` task：显式stop、start failure、dependency failure cascade及其ACK-unknown confirmation一旦得到canonical terminal winner，必须从同一个coordinator lock移除start material、mailbox、mailbox ordinal与completion marker。已经拥有physical child task的carrier只能由其done callback退休，不能被cascade路径提前清除。Canonical task row只保存mode与N，不复制parent正文；ROOT canonical transcript仍是语义来源，但Round 10不从它执行跨Host child recovery。

Child真正启动时，coordinator通过Round 10 sealed factory把immutable objective、optional `PARENT_CONTEXT`与optional `DEPENDENCY_RESULTS`封装为修订后的`SubagentInitialSeed`。Round 5B原始consumer seam只提供粗粒度字段，本轮必须在同一production `conversation_kernel/cold_epoch.py`中收紧并bump process-local cold-assembler contract：seed显式引用task/scope、objective item fingerprint、parent call subject/selection/effective source fingerprint或NONE，以及exact dependency-result context/effective source fingerprint或NONE。Factory证明objective逐对象等于child initial canonical USER_MESSAGE，`LAST_N`逐项等于本次effective `PARENT_CONTEXT VALUE`，`NONE`时不存在parent source；dependency carrier逐项等于direct edges指向的terminal result facts，零依赖iff该source absent。不得接受调用者手写hash或建立mutable fingerprint lookup。

随后按Round 9标准顺序冻结child-scope owner snapshots/registry、resolve exact target/native eligibility、以`EmptyCapabilityEpochPredecessor`构造`FrozenCapabilityDispatchCut`及两个sibling views、完成`FrozenToolCapabilityExposureSelection`与selected native materialization得到final `FrozenToolCapabilityExposurePlan`，再由normal context collector生成exact-joining `FrozenNonTriggerContextSources`。该seed、上述exact semantic results、trigger/current-turn source candidates与唯一planning deadline一起交给`KernelColdEpochInputAssembler`。Coordinator不得自行拼接SYSTEM、lower tools/messages、构造第二份wire plan或复制cold continuity candidate逻辑；assembler也不得反向选择N、查询owner/registry、规划Tool route、读取task repository或取得child physical execution authority。

Child与ROOT之间没有continuity或cache compatibility承诺。Round 3.1 strict-prefix从child第一次provider open之后才开始；因此不存在`FULL_PREFIX_FORK`。Round 10也不提供`FULL_SEMANTIC`/`all`：需要精确旧事实时，ROOT应写入自洽objective、给出canonical file/artifact定位，或在child ACTIVE后使用`send_agent_message`补充，而不是无界复制整个会话。

### 5.4 Orchestration bounds

默认与hard bound：

~~~text
maximum live child executions per Host 4
maximum tasks per create batch         16
maximum dependency edges per batch     64
maximum accepted tasks per caller turn 16
maximum wait targets                   32
maximum LAST_N parent context turns     3
~~~

所有task都处于固定worker层。Global capacity由唯一coordinator预留，不能让每个child独立计数而突破Host上限。`maximum accepted tasks per caller turn`是跨同一`parent_turn_id`全部tool invocations的canonical累计上限，不是单个batch的别名。

---

## 6. Task admission与dependency scheduler

### 6.1 Prepared batch

~~~text
PreparedSubagentTaskBatchAdmission
  session/workspace/writer generation
  exact ROOT caller turn/tool attempt
  exact FrozenSubagentParentContextCallSubject
  batch id
  ordered task row drafts
  ordered dependency row drafts
  initial status per task
  shared exact parent-call subject reference
  ordered per-task parent-context selections
  profile/context projection fingerprints
  event drafts
  candidate fingerprint
~~~

Factory先完成：

1. args bounds；
2. ROOT scope/capability检查；
3. exact spawn-producing ROOT provider-input cut、continuity-installed compiled view与`FrozenSubagentParentContextCallSubject`逐对象join；
4. `NONE | LAST_N` selection、quote rendering与physical preflight；
5. stable ids预生成；
6. dependency解析；
7. cycle detection；
8. existing dependency exact session/status read；
9. initial status计算；
10. capacity soft preflight。

Repository transaction重新验证canonical ROOT creation provenance/dependency与同`parent_turn_id`累计task数量，但不重新解释自然语言objective或profile含义。Direct fast path、ACK confirmation与同一assistant batch中的所有task calls都必须消费相同的exact call subject；subject缺失、来自另一cut/scope/epoch或CAS已失效时，task row必须为0并回到normal dispatch planning重建，不能退化为“只按turn id选历史”。

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

`all dependencies COMPLETED`同时意味着每个dependency必须拥有exact唯一terminal RESULT。若出现`COMPLETED`但无result、wrong result fingerprint或重复winner，属于canonical invariant failure，downstream不得启动。Dependency success不是只有一个status bit；它携带可供下游消费的task output。

### 6.3 Start

Coordinator对`PENDING_START`按`accepted_at, task_id`公平排序：

~~~text
PreparedSubagentTaskStart
  exact target task / writer generation
  ordered direct dependency edges
  ordered FrozenSubagentResultPublicFacts
  FrozenDependencyResultContext | NONE
  creation-time parent-context start material | NONE
  start candidate fingerprint
~~~

Factory必须从same RR cut读取direct edges与terminal result facts，证明每个edge恰有一个`COMPLETED + RESULT` winner，并按`dependency_ordinal`生成context；不读取祖先、不使用list/wait preview缓存，也不写delivery relation。

~~~text
reserve Host-global slot
-> consume the task's creation-time frozen parent-context selection
-> freeze exact direct dependency result context
-> freeze current exact child-scope Builtin/MCP/Skill owner snapshots
-> resolve child model target/native eligibility
-> construct Round 9 EMPTY parent cut + Tool/Skill sibling views
-> finalize Tool exposure plan and FrozenNonTriggerContextSources
-> CAS task PENDING_START -> ACTIVE
-> admit initial child turn
-> sealed factory constructs exact revised SubagentInitialSeed
-> KernelColdEpochInputAssembler.prepare_semantic()
-> selected replay hydration under the same deadline, normally NONE for a fresh child
-> KernelColdEpochInputAssembler.finalize_wire()
-> exact physical tool-surface join and DirectModel preflight
-> continuity CAS installs new child cold epoch
-> open provider through the same PreparedKernelModelExecution
~~~

这里有意使用三个不同linearization：parent context冻结在task admission；dependency results在所有direct dependencies terminal成功后、target start时冻结；execution capability在child真正启动、第一次provider open之前冻结。等待dependency期间ROOT新增的对话不会偷渡进`LAST_N`，且后续新增的非direct dependency不会进入已冻结DAG；但期间新READY且child-visible的MCP、当前Skill winners和physical reconnect可以进入child的新cold epoch。

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

`SubagentCompletionPermit`只携带exact `task_id`；`COMPLETING`集合本身是其唯一one-shot owner，不再保存一个从未被consume/revalidate的装饰性nonce。Live physical carrier只保存调度与取消真正消费的`task_id、parent_turn_id、Task、cancellation intent、status、cancellation_reason`；objective继续只属于start material，terminal result与failure truth只从canonical task/result读取。

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

## 8. Unified task result、downstream routing与ROOT显式交付

### 8.1 Explicit result优先

一旦`report_agent_result`的specialized canonical settlement获得`FULL`：

- 后续普通assistant final text不再成为另一个result；
- coordinator先取得task completion permit；随后同一transaction接受ToolResult、result并完成turn/task；
- result source固定`EXPLICIT`；
- summary/output/diagnostics来自exact tool call args；
- producer provenance指向small acknowledgement ToolResult，但ROOT wait/list/acceptance全部经`FrozenSubagentResultPublicFact`读取structured result fields，绝不把ack正文当成result；
- ROOT wait/list读取同一row的bounded public projection。

`report_agent_result`的sole-call约束在assistant batch进入ordinary tool loop前执行。mixed/multiple batch整体产生no-attempt `INVALID_ARGUMENTS` results并保持task ACTIVE；不能允许先执行一个有副作用的sibling，再由result call终结task。

Production只有这条runner specialized settlement会消费`report_agent_result`。普通Builtin invoke seam若收到该名称必须fail closed为internal invariant error；不得保留一个只返回“accepted”却不提交ToolResult/result/turn/task的第二条伪happy path。Batch/start confirmation DTO也只返回`FULL | NONE | CONFLICT`，不重复携带调用方已经持有且没有消费者的task-id tuple。

### 8.2 Inferred result

没有explicit candidate时，terminal assistant text形成`INFERRED` result：

- source entry保存exact full canonical assistant public content；
- result row中的summary/list/wait projection使用确定性UTF-8-safe HEAD_TAIL preview，最多16,384 bytes，并明确完整正文仍在source entry；
- ROOT显式acceptance从exact source assistant public content构造，不从preview反推完整结果；
- 不创建subagent result artifact、ToolResult分页或另一套大正文authority；
- 一个prepared terminal candidate在单一repository transaction中接受assistant entry、`INFERRED` result、turn terminal、task terminal及对应occurrences；ACK unknown只确认这一整组`FULL | NONE | CONFLICT`，不允许先提交assistant再补写result。

### 8.3 ROOT可见性

Result提交时不执行“发给ROOT”或“发给下游”的第二次写入。它只是immutable task output：

- 已经存在或未来创建的direct downstream task，在其start时从canonical dependency/result join构造`DEPENDENCY_RESULTS`；
- 同一result可以fan-out给多个direct downstream，projection不消费、不打已投递标记；
- 每个downstream只收到自己的direct dependencies，不自动收到祖先全图结果；
- result不会被转成`INTER_AGENT_MESSAGE`，也不经过process-local mailbox。

Result也不会自动伪装成ROOT user message。ROOT只有两条显式result-content路径：

1. ROOT调用`wait_agent`/`wait_agent_tasks`得到ordinary ToolResult；
2. ROOT controller调用既有`ACCEPT_SUBAGENT_RESULT`，按同一`FrozenSubagentResultPublicFact.result_fingerprint`把chosen result的acceptance projection作为ROOT external-result entry接受。

ROOT可以通过`list_agents`检查任何task的bounded terminal metadata，方便诊断或人工接管，但这不会影响下游消费。若result content要进入ROOT model input，必须通过wait ToolResult或既有显式result acceptance，不自动注入；Round 5B handoff也只投影active/task-board状态，不复制result正文。External-result writer仍只创建既有acceptance entry；本轮不引入result-copy relation、artifact、delivery marker或ack-to-content特殊分支。

---

## 9. Cancellation、close与failure matrix

| 场景 | canonical outcome | physical outcome |
|---|---|---|
| stop waiting task | `CANCELLED/USER_CANCELLED` | 无child可取消 |
| stop active child | child turn `USER_STOPPED` + task `CANCELLED` exact transaction | cancel exact task |
| Host close | 全部nonterminal `INTERRUPTED/SESSION_CLOSED` | drain/cancel，不detach |
| writer takeover | 全部nonterminal `INTERRUPTED/HOST_TAKEOVER` | 新Host不恢复 |
| dependency failed | downstream `BLOCKED_DEPENDENCY_FAILED` | 不启动child |
| dependency标记COMPLETED但result缺失/冲突 | downstream `FAILED/DEPENDENCY_RESULT_INVARIANT` | provider open=0，不猜测输入 |
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

Child permission从创建turn的bypass snapshot冻结；child capability不复制ROOT exposure，而在该task真正启动时从current owner snapshots构造Round 9 exact child-scope EMPTY cold cut。该cut、两个sibling views、final Tool plan及normal `FrozenNonTriggerContextSources`作为named inputs进入shared assembler；assembler不查询owner或重新决定DIRECT/META/Skill winner。完整child-visible MCP cohort fit时全部DIRECT，否则全部META_ONLY，与ordinary cold-open/compaction successor使用同一Round 9规则。所有worker profile共享同一ordinary child capability policy：

- scope-visible、execution-backed Builtin tools；
- `subagent_visible`且在child cold epoch被选中的DIRECT MCP tools；
- fixed `list_mcp_servers | inspect_new_mcp_tool | use_new_mcp_tool` meta Builtins；
- exact child-scope `MCP_CATALOG | SKILL_CATALOG | ACTIVE_SKILL` provider sources；
- ordinary `read_file | search_files | terminal`等现有能力；
- exact child自己的`report_agent_result`。

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

Round 5B已经激活的production `COMPACTION_RUNTIME_HANDOFF`仍使用`flat_subagents`。Round 10必须在原owner/source上完成一次明确的迁移，而不是只修改文案或另建subagent handoff source：

~~~text
FrozenRootSubagentTaskBoardHandoffFact
  task_id
  task_key/label                     optional bounded display metadata
  objective_preview                 optional bounded untrusted text
  status                            ACTIVE | PENDING_START | WAITING_DEPENDENCY
  dependency_total
  dependency_remaining
  pending_message_count
  fact fingerprint
~~~

provider body key改为`subagent_tasks`，source-specific runtime-handoff renderer/contract identity同步bump；旧`flat_subagents`字段不再输出。它不包含model-authored phase、mail body、child raw transcript、result正文、hidden reasoning、physical executor或dependency edge明细。

选择与bounds固定为：

- 最多16个visible rows；
- `task_key/label`各最多64 UTF-8 bytes，`objective_preview`为确定性UTF-8-safe HEAD_TAIL、最多512 bytes；
- 所有`ACTIVE` task优先且必须全部保留；Host global active cap为4，因此该集合有硬界；
- 剩余名额依次给`PENDING_START`、`WAITING_DEPENDENCY`；每组内部按`accepted_at, task_id`；
- FULL与COMPACT均返回按status拆分的exact total/omitted counts；
- COMPACT必须保留所有ACTIVE whole rows，只能从尾部移除PENDING/WAITING whole rows；不得截断objective、伪造较低dependency count或把ordinal当作ID；
- ACTIVE rows加fixed envelope仍不fit时，返回typed resource boundary而不是隐藏正在运行的worker。

Round 10直接修改production `conversation_kernel/compaction/runtime_handoff.py`与`tool_runtime.freeze_compaction_runtime_handoff`。在已经安装handoff的snapshot-based ROOT epoch中，task lifecycle、dependency readiness或pending-message count变化按既有`SNAPSHOT_ON_CHANGE`追加successor；全部消失时追加`CLEARED`；无变化no-op。它继续是同一个untrusted handoff authority，不新增relation、event、source或task-board projection owner。

Compaction不恢复mailbox，不晋升result authority。一个active child自身compact时，已canonical的`INTER_AGENT_MESSAGE`自然进入其source view；尚在process-local mailbox的message在fence结束后的下一个safe point追加。

Active child compaction对parent context使用同一条只读规则：`NONE`没有source；`LAST_N`复用child首次安装的exact `PARENT_CONTEXT` body/fingerprint，不重新读取ROOT、扩大N或吸收后续ROOT消息。若该Host-local material与installed source head发生invariant conflict，compaction typed失败并保留旧child epoch；不得猜测重建，也不得新增durable context snapshot。

同一child compaction对`DEPENDENCY_RESULTS`也只复用首次启动时安装的exact body/source fingerprint，不重新遍历图、不吸收祖先结果或后来创建的downstream edges。零依赖仍无source；installed VALUE无法exact join时采用同样的typed failure。它与`PARENT_CONTEXT`都是semantic seed data，不进入compaction summary的创作职责。

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
result id/source/accepted
~~~

Canonical snapshot从relations读取；mailbox count只能由same-Host live overlay提供，GAP后允许为空。不存在model-authored phase字段。

### 11.2 Live event

复用`SubagentProgress`，payload只增加Runtime可机械导出的closed optional fields：

~~~text
pending_count
dependency_status
~~~

它仍是process-local presentation，不承担canonical状态转移。attach/GAP按照现有baseline-first then owner snapshot顺序重建，不从Live events replay task board。

### 11.3 用户控制

Round 10不新增client直接给child发message或编辑DAG的UI。Controller继续支持list/stop/result accept；model工具先完成产品面。高级task-board交互属于后续Web/Desktop UI round。

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
- 扩展既有`KernelToolInvocationContext`与`KernelSubagentToolPort`，贯穿actual continuity-installed model-call的`FrozenSubagentParentContextCallSubject`；不新增平行invocation owner；
- 复用Round 5B已激活的唯一neutral `KernelColdEpochInputAssembler`，在同一module收紧`SubagentInitialSeed` sealed factory与validator并bump process-local contract；不得创建subagent-private wrapper或重写normal/compaction assembly；
- `spawn_agent`复用single-item batch；
- provider DTO统一`task_id`；
- 实现默认`NONE`与`LAST_N(1..3)`纯ROOT user/steer/assistant context-unit selection、untrusted quote projection与prepared start-material settlement；
- child first open构造包含optional `PARENT_CONTEXT | DEPENDENCY_RESULTS`的`SubagentInitialSeed`，并通过shared assembler使用当前capability cold reconstruction，不复用ROOT provider prefix；
- ROOT-only orchestration surface与child report-only surface；
- central Host-global capacity。

### R10-2：Batch/DAG/wait/stop

- 原子batch admission；
- exact dependency scheduler；
- direct dependency result-context freeze与start-time exact join；
- partial multi-wait；
- unified stop；
- failure/block cascade；
- list task board。

### R10-3：Result reporting

- child-bound authorization；
- explicit result的specialized atomic canonical settlement；
- `report_agent_result` sole-call batch intercept；
- shared `FrozenSubagentResultPublicFact`与existing acceptance exact join；
- inferred fallback；
- unified downstream projection与ROOT explicit wait/result acceptance。

### R10-4：Inter-agent message

- `send_agent_message` descriptor/executor；
- mailbox owner与linearization；
- safe-point batch consumption；
- final-answer/tool-group races；
- reader/lowering/BASE_SYSTEM guardrail。

### R10-5：Protocol、compaction contract与activation

- Protocol v3 renderer-neutral projections；
- 把Round 5B production `flat_subagents` handoff原地迁移为bounded hierarchical `subagent_tasks` task-board projection；
- Gap Index标记ACTIVATED；
- architecture/oracle/evidence；
- real-provider ROOT-orchestrated dogfood。

每个slice完成后均可在同一feature branch继续，但只有R10-0..5全部通过才能标记ACTIVATED。不得把未完成的DAG或mailbox descriptor暴露到production tool surface。

---

## 13. Test matrix

### 13.1 Happy path

1. ROOT spawn child，child直接完成工作并通过sole-call `report_agent_result`提交explicit result，ROOT wait取得result。
2. ROOT batch创建A/B/C，B依赖A、C依赖B；A/B完成后按序自动启动，B只看到A的terminal result summary，C只看到B而不重复携带A。
3. ROOT创建多个independent worker；coordinator严格遵守Host-global并发上限并公平启动pending task。
4. ROOT给running child发送message；child下次provider call看到typed message并修订结论。
5. child正在执行tool时收到message；message严格位于完整tool group之后。
6. `settle=first`返回第一个terminal，其他task继续；后续`all`返回全部。
7. `context=NONE`时child只看到objective与自身重建authority，不看到ROOT conversation。
8. `context=LAST_N(2)`时child看到spawn-producing call subject中最近两个ROOT user-turn units；同turn一起送往provider的多条ROOT user messages整体保留，只包含ROOT user/steer/assistant public text，不包含任何tool group。

### 13.2 Scope与固定两层边界

- 四种ROOT permission mode的七个orchestration descriptors及其ordered provider `tools[]` bytes完全相同；
- `READ_ONLY | ASK_PERMISSIONS | ACCEPT_EDITS`下逐一调用七个tools，均在attempt前得到`subagent_requires_bypass_mode`，repository/coordinator/physical invocation计数为0；
- `BYPASS_PERMISSIONS`下七个tools分别进入其正常authorize/invoke路径；invoke使用不同或stale permission snapshot时仍拒绝；
- child surface没有任何spawn/create/list/wait/stop/message orchestration tool；
- child绕过surface直接invoke orchestration port仍typed拒绝；
- 五种worker profile逐项证明permission snapshot均exact等于ROOT creation-turn `BYPASS_PERMISSIONS`，ordinary Builtin/MCP/Skill capability集合除语义版本变化外相同；
- ROOT后续切换到非bypass mode不回写已经ACTIVE child的permission snapshot；显式stop/Host close仍按既有terminalization路径生效；
- child可为自己的exact task调用`report_agent_result`；
- child-visible cold MCP可DIRECT调用；ROOT-only/scope-invisible MCP即使在bypass下仍不可见且无法invoke；
- child epoch内late-ready MCP只追加其本地`MCP_CATALOG` successor，`tools[]`保持不变，child经list/inspect/FULL-install/use成功调用；ROOT或另一child的ref/cursor typed stale；
- child运行中安装、修改、删除Skill后，下一safe point产生其本地`SKILL_CATALOG VALUE | CLEARED | UNAVAILABLE`正确successor，普通`read_file`可采用正文；
- task admission后、child实际启动前READY的child-visible MCP可进入child DIRECT surface；child cut之后READY的同类MCP只能走该leaf meta route；
- ROOT与child第一call不要求SYSTEM/tools/messages prefix相等；child first open完成后，同一child epoch继续满足strict prefix；
- ROOT list只读且bounded；
- `list_agents`跨越50条历史task时通过opaque `(accepted_at,id)` keyset cursor到达后续ACTIVE/terminal rows；cursor跨session、改filter、改page size、伪造与Host重启均typed stale/invalid，且不创建cursor owner；
- cross-session/workspace/task id拒绝；
- 所有child profile都看不到七个ROOT orchestration tools；
- ROOT/child/different child continuity完全隔离。

### 13.3 Dependency

- same-batch forward reference；
- existing same-session ROOT-owned task reference；
- unknown/self/cycle rejection且零row；
- cross-session/workspace dependency rejection；
- upstream FAILED/CANCELLED/INTERRUPTED/BLOCKED逐项cascade；
- never-started downstream在failure cascade、显式stop、start failure及frontier commit ACK-unknown后立即释放全部process-local start/mailbox/completion carrier，canonical task/result history仍可分页查询；
- concurrent dependency completion只有一个start winner；
- downstream start按dependency ordinal完整获得所有direct terminal result summaries；explicit/inferred producer产生同一provider shape；
- one-to-many fan-out复用同一result fingerprint且不写delivery row；later-created direct downstream也可消费已经terminal的result；
- downstream不获得祖先result、producer transcript、tool groups、output diagnostics或mailbox；
- `COMPLETED`但缺result、wrong fingerprint或duplicate result使downstream provider open为0；
- capacity不足保持PENDING_START并公平启动；
- Host close使waiting/pending/active全部INTERRUPTED且reopen不启动。

### 13.3.1 Parent context

- omitted `context`精确等于`NONE`；unknown mode/field与`mode=last_n, turns=0|4`在task admission前拒绝；
- `LAST_N`选择不足N个unit时返回全部available units；
- 一个ROOT turn中共同进入provider call的多条ordinary `USER_MESSAGE`保持为同一个unit并逐条保序；USER_STEER与assistant public text按真实顺序保留；
- latest open user unit可选，spawn assistant/tool-call batch不进入自己的parent context；
- assistant公开正文与同entry tool-call blocks分离，tool calls/ToolResult/tool groups一律排除且不产生dangling call；
- ROOT SYSTEM/tools/runtime sources、hidden reasoning、provider-private replay及scope-bound opaque handles不迁移；
- `PARENT_CONTEXT`固定为`UNTRUSTED_OBSERVATION/SNAPSHOT/VALUE`，不能授予permission、MCP、Skill或file access；
- parent source自身physical overbound拒绝整个atomic batch，task/dependency/event row均为0；child start aggregate overbound则typed `FAILED`且provider open为0；两者都不静默缩小N或退化为NONE；
- dependency延迟启动仍使用creation-time frozen context；ROOT后续消息不改变它；
- 同一assistant tool-call batch共享一个private exact parent-call subject并以per-task selection引用，内存probe证明无正文deep-copy；
- direct FULL、ACK confirmation与batch settlement均从同一actual model-call subject exact安装prepared context；caller cancellation只能detach；
- call subject与final compiled input/provider cut/continuity epoch逐对象join；wrong cut/scope/epoch、仅有turn id或caller自造hash不能创建task；continuity CAS失败时subject丢弃并随dispatch重建；
- `NONE`与`LAST_N`分别经sealed factory形成修订后的`SubagentInitialSeed`，并按direct dependency集合携带exact `DEPENDENCY_RESULTS | NONE`进入唯一shared assembler；wrong task/scope/target/objective/parent source/dependency set/result/effective-head或mixed seed typed拒绝；
- shared assembler两阶段result中的compiled input、wire plan与cold candidate inputs共同进入第一次continuity CAS；fresh child selection的replay hydration request/proof必须为NONE，DirectModel不得重新lower另一份child input；
- 单独调用assembler不读取repository/owner、不取得physical borrow、不安装continuity且provider open为0；
- Host close丢弃prepared body并terminalize nonterminal tasks；新Host不恢复parent context；
- active child compaction复用installed `PARENT_CONTEXT`，不重新读取ROOT或扩大N；
- active child compaction复用installed `DEPENDENCY_RESULTS`，不遍历祖先或吸收后来创建的edges；
- Chat Completions与Responses均不要求ROOT/child prefix兼容，child first open后的same-epoch strict-prefix仍成立。

### 13.4 Messaging races

- send vs finalizer两种linearization；
- send during provider streaming；
- send duringtool execution；
- multiple orderedmessages in one safe-point batch；
- mailbox item/body bounds；
- delivery ACK unknown FULL/NONE/CONFLICT；
- same send attempt并发/ACK-unknown consumption只产生一个same-session `INTER_AGENT_MESSAGE` entry，partial UNIQUE与exact lineage constraint均通过；
- Host close丢未consumed mail但interrupt target；
- message不会被lower为USER_MESSAGE/USER_STEER；
- message不触发memory/cheap-hint/textual Skill；
- sameepochSYSTEM/tools不变、messages只追加suffix。

### 13.5 Result/cancel

- explicit result不产生额外model call；
- explicit与inferred互斥；
- `report_agent_result`与ordinary tool混合或同batch多次出现时，所有requests均no-attempt typed拒绝、task仍ACTIVE且ordinary physical effects为0；
- EXPLICIT producer ack与structured result projection分离，wait/list/acceptance不能回显small ack；
- INFERRED list/wait使用16,384-byte UTF-8-safe HEAD_TAIL，acceptance仍读取exact source assistant public content，且没有result-specific artifact；
- explicit/inferred terminal result都自动成为direct downstream的同形summary source，但不会自动进入ROOT；ROOT wait/accept后才产生ROOT-visible input；
- report result vs stop/Host close；
- result accepted into ROOT exact once；
- repeated wait idempotent；
- oversized result arguments在attempt前typed拒绝，且无ToolResult/result/task-terminal假成功。

### 13.6 Physical bounds

- 4个active child + queued runnable task；
- global active-worker cap；
- 16-task/64-edge batch；
- 同一ROOT caller turn跨多次spawn/create累计第17个task由repository拒绝且并发admission只有一个合法16-task winner；
- 32-target wait；
- mailbox 16 items/64 KiB；
- 16个direct dependencies、各16,384-byte summary仍完整形成单个`DEPENDENCY_RESULTS` source且无正文deep-copy；aggregate preflight失败不得partial omit；
- load probe证明无per-child manager/executor duplication和无continuity body deep-copy。

### 13.7 Architecture guards

- 无durable subagent job/lease/receipt/checkpoint/replay/repair；
- 仓库只有一个neutral `KernelColdEpochInputAssembler`；不存在child/compaction/private prompt builder或第二套SYSTEM/tools/source placement；
- assembler没有repository/Host/current-owner read、physical borrow、provider open、continuity CAS或durable state依赖；
- dependency relation只有canonical product writer/reader；
- downstream result routing只是direct edges + canonical RESULT的pure start-time projection，不存在delivery relation、result copy、durable inbox或fan-out owner；
- Live event不成为task authority；
- Round 5B handoff使用唯一`subagent_tasks` hierarchical renderer：全部ACTIVE保留，PENDING/WAITING确定性裁剪，status-specific exact omitted counts正确，旧`flat_subagents`与phase字段不再出现；
- no raw provider/model/MCP object进入canonical task rows；
- child profile不接受raw allowlist；
- oracle精确`29 / 24 / 11 / 1 / 25 / 0`；
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
src/pulsara_agent/conversation_kernel/compaction/runtime_handoff.py
src/pulsara_agent/conversation_kernel/_repository/external_results.py

src/pulsara_agent/capability/builtin_catalog.py
src/pulsara_agent/model_input/contracts.py
src/pulsara_agent/model_input/lowering.py

src/pulsara_agent/ports/live_agent_event.py
src/pulsara_agent/terminal_protocol/schema/terminal_kernel_v3.proto
src/pulsara_agent/terminal_protocol/canonical_v3.py
src/pulsara_agent/terminal_protocol/v3_gateway.py
~~~

预计新增subagent-specific production Python module不超过4个，建议：

~~~text
conversation_kernel/subagents/contracts.py
conversation_kernel/subagents/coordinator.py
conversation_kernel/subagents/mailbox.py
conversation_kernel/subagents/scheduler.py
~~~

repository继续留在现有模块化`_repository/subagents.py`，不要恢复旧`runtime/subagent/`十余文件的reducer/projection层。

Neutral `conversation_kernel/cold_epoch.py`属于Round 5B已激活共享基础，不计作第五个subagent模块；仓库中只能有这一份production cold-epoch assembler。Round 10只允许在其既有closed seed union上收紧`SubagentInitialSeed` exact-object validator并升级contract identity。

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
- advanced Web/Desktop task-board UI。

---

## 16. Definition of Done

Round 10只有同时满足以下条件才可ACTIVATED：

1. 8个最终subagent tools（7个ROOT orchestration + `report_agent_result`）全部拥有sealed production binding，无dead descriptor；`report_agent_phase` descriptor与executor均删除；
2. 七个ROOT orchestration descriptors跨四种permission mode保持同一tool surface，只有`BYPASS_PERMISSIONS`可执行；child report-only surface、global capacity与scope isolation通过；
3. batch/dependency/start/block/stop/wait状态机由canonical relations闭合；每条dependency同时形成direct-result data edge，downstream只在start时获得全部direct summaries；
4. explicit/inferred result是同一种canonical task output且互斥；explicit result与其ToolResult/turn/task同事务闭合；sole-call batch约束、fan-out与public result projection通过；result不自动进入ROOT；
5. `send_agent_message`只允许ROOT向ACTIVE ROOT-owned child queue，safe-point后形成`INTER_AGENT_MESSAGE`；
6. message/tool-group/final-answer/close的全部竞态有exact test；
7. no `followup_task`、no task reopen、no second AgentThread identity；
8. Host close/takeover将全部nonterminal task terminalize，新Host不恢复execution；
9. 所有worker exact继承creation-turn bypass snapshot；Round 9/9.1 child-scope Builtin、DIRECT/late-meta MCP与dynamic Skill refresh exact join，且ROOT/leaf/foreign-leaf动态ref与catalog完全隔离；
10. context contract只有默认`NONE | LAST_N(1..3)`；parent context绑定spawn-producing actual installed model-call subject，只保留按ROOT turn分组的全部user messages、steers与assistant public content，排除所有tool groups，并使用untrusted quote projection；
11. Child first open用sealed factory产生的exact-object `SubagentInitialSeed`调用唯一shared assembler，逐项绑定optional `PARENT_CONTEXT`与全部direct `DEPENDENCY_RESULTS`，按启动时current capability cold reconstruction并复用既有compiled/wire/continuity artifacts；没有subagent-private prompt builder；
12. Child不继承ROOT prefix；Chat Completions与Responses均证明child first-open wire来自same assembly，随后same-epoch SYSTEM/tools不变、messages只追加suffix；
13. Protocol v3 Python contract、full pytest、PostgreSQL、Ruff、compileall、generator与architecture gates全部通过；
14. clean-v0 fresh/repeat/deep verify/reset-required通过；
15. architecture oracle为`29 / 24 / 11 / 1 / 25 / 0`；
16. real-provider dogfood至少覆盖ROOT batch workers、默认`NONE`、含同turn多USER_MESSAGE的`LAST_N`、pre-start MCP promotion、active message steer、explicit/inferred dependency-result chain、sole-call explicit result与compaction task-board handoff；
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

Graph的数据流也只有一条：每个worker提交一个统一terminal task output，Runtime在direct downstream start时把其bounded summary投影为`DEPENDENCY_RESULTS`。Explicit tool result与inferred final answer不再有两种收件人语义；它们只是在producer shape上不同。ROOT保留观察权，但只有显式wait/accept才把result带入ROOT上下文。

Spawn是一次新的child cold construction，不是ROOT provider-prefix fork。默认`NONE`与最多3个纯对话turn的`LAST_N`足以覆盖“完全由objective驱动”与“需要少量parent原话/assistant对话”两类实际worker任务；删除tool groups与full-history模式同时消除了不确定预算、无关工具证据扩散、scope-bound handle迁移和ROOT/child cache兼容的伪命题。

Cold construction本身也不是subagent专属机制：Round 10只提供`SubagentInitialSeed`与child-scope frozen authorities，最终SYSTEM/tools/messages/wire/continuity candidate由Round 5B抽出的唯一neutral assembler构造。这样compaction rebase与spawn共享正确的channel placement和wire proof，却仍各自拥有summary、task、permission与lifecycle。

Codex最值得吸收的不是recursive agent tree或它的存储方式，而是“typed communication、queue-only message与trigger-turn followup必须分开”。Pulsara本轮只需要ROOT向running worker的queue-only message；terminal task之后继续工作，用新task表达比复活旧task更符合append-only与dependency语义。
