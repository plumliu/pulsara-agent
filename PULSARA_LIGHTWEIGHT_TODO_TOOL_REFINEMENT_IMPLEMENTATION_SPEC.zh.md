# Pulsara Lightweight TODO Tool Refinement 实施规格

> 状态：**ACTIVATED**
>
> 记录日期：2026-08-19
>
> 当前代码基线：`7a61b8d6c1789f4cb730aa2bcbf910cd56fd9cde`
>
> 激活证据：[lightweight_todo_tool_refinement_activation.json](benchmarks/suites/core/v1/lightweight_todo_tool_refinement_activation.json)
>
> 上位契约：[Round 3 structured compiler](ROUND_3_STRUCTURED_MODEL_INPUT_COMPILER_IMPLEMENTATION_SPEC.zh.md)、[Round 3.1 provider-input prefix continuity](ROUND_3_1_PROVIDER_INPUT_PREFIX_CONTINUITY_IMPLEMENTATION_SPEC.zh.md)、[Round 5A execution envelope](ROUND_5_LONG_HORIZON_EXECUTION_ENVELOPE_IMPLEMENTATION_SPEC.zh.md)、[Round 7.1 ToolResult projection](ROUND_7_1_PROVIDER_VISIBLE_TOOL_RESULT_PROJECTION_IMPLEMENTATION_SPEC.zh.md)
>
> 下游集成：[Round 5B compaction](ROUND_5B_LONG_HORIZON_CONTEXT_COMPACTION_IMPLEMENTATION_SPEC.zh.md)

本文把现有`todo`从Host-global、action-by-action的可变list原型，收敛为一个**exact run-scoped、bounded、process-local、完整snapshot替换**的轻量模型工具。

它服务的是：

> 一个Agent正在处理当前小型多步骤任务时，用极低动作成本记录“接下来做什么、现在做什么、已经做完什么”。

它不是：

- Plan Mode、permission mode或human interaction workflow；
- 跨会话项目管理器；
- Hierarchical/batch subagent task graph；
- durable job/task authority；
- dependency graph、claim/lease、多人协作或恢复系统。

现有实现尚未对外发布，因此本轮**直接替换旧schema与旧process-local语义**：不实现旧`add/update/list/clear`兼容层，不保留旧descriptor版本，不建立迁移或双读。激活后的运行时仍须遵守正常的epoch内strict-prefix，但开发期旧shape不是产品兼容负担。

---

## 0. 执行结论

### 0.1 最终工具形状

工具名继续为：

~~~text
todo
~~~

唯一输入：

~~~json
{
  "items": [
    {
      "text": "Inspect the failing test",
      "status": "in_progress"
    },
    {
      "text": "Implement the narrow fix",
      "status": "pending"
    }
  ]
}
~~~

每次调用完整替换当前exact run的TODO snapshot；`items=[]`显式清空。没有action union、单项ID、merge、priority、cancelled、activeForm或单独list操作。

### 0.2 为什么使用完整snapshot

完整snapshot同时吸收Codex与Claude Code的最小产品形状，并避免当前Pulsara的动作放大：

~~~text
旧实现：
  add A -> full list result
  add B -> full list result
  add C -> full list result
  update A -> full list result
  update B -> full list result

新实现：
  todo([A, B, C])
  todo([A completed, B in_progress, C pending])
~~~

模型刚刚提交的完整arguments已在assistant tool call中；ToolResult无需再次复制整张清单。

### 0.3 Authority与durability

~~~text
TodoRunStateOwner
  owns       current exact-run advisory checklist
  does not   own conversation truth, Plan, job, subagent graph or user intent

canonical assistant tool call/result
  records    historical model action and exact acknowledgement
  does not   become a durable current-TODO projection
~~~

本轮不新增：

- PostgreSQL relation、column或migration；
- `CommittedEventType`、subject slot或append guard；
- durable job、receipt、checkpoint、projection、repair或replay；
- cross-Host TODO recovery。

本轮唯一新增的产品vocabulary是process-local通知：

- `LiveEventType.TODO_SNAPSHOT_UPDATED = "TodoSnapshotUpdated"`；
- 对应的Terminal Protocol payload与current-state resync view。

它们只投影`TodoRunStateOwner`的当前值，不是durable event、current-state authority或recovery source。最终architecture oracle因此为`31 / 24 / 13 / 2 / 25 / 1`；除Live event count由23增加到24之外，其余不变。

---

## 1. 当前代码真值与Findings

### 1.1 当前实现

当前[`TodoTool`](src/pulsara_agent/tools/builtins/todo.py)保存：

~~~text
_items: list[TodoItem(id, text, status)]
_next_id: int
~~~

支持：

~~~text
add(text, status?)
update(id, text?, status?)
list()
clear()
~~~

每次操作都返回整个`items` JSON。Host在[`DirectKernelToolPort`](src/pulsara_agent/conversation_kernel/tool_runtime.py)构造时只安装一个`TodoTool()`，而Builtin binding允许ROOT与child共同调用。

### 1.2 [P1] schema不是closed action union

Descriptor只要求`action`；`add.text`与`update.id`由physical `execute()`中的`required_str_arg()`验证。这导致：

~~~text
{"action":"add"}
  -> descriptor validation succeeds
  -> authorization ALLOW
  -> canonical ToolExecutionAttempt accepted
  -> physical execute raises ValueError
  -> recovery severity = unknown_effect
  -> turn may interrupt as TOOL_EFFECT_OUTCOME_UNKNOWN
~~~

unknown ID也在attempt之后抛`KeyError`。普通模型参数错误因此被错误提升为physical outcome uncertainty。

新实现必须在attempt acceptance之前完成全部schema与semantic validation；合法bounded replacement在mutation之后不得再执行可失败工作。

### 1.3 [P1] Host-global state破坏ROOT/child隔离

一个Host只有一个TodoTool实例，同时ROOT、不同child都可见。因此当前可能发生：

- child读取或修改ROOT的清单；
- child A与child B相互覆盖；
- 下一条真实ROOT用户消息继承旧任务；
- 多runner thread无锁更新同一个list与`_next_id`。

新owner必须按exact logical run分区，不能把Host当作TODO scope。

### 1.4 [P1] 没有物理bound或可见性保证

当前没有item count、text bytes、aggregate bytes或duplicate上限。状态先增长，随后完整list作为ToolResult进入普通artifact/HEAD_TAIL流程。清单过大时：

- 模型可能只看到截断preview；
- `list`再次产生同一个超大结果；
- 每次小更新都可能发布大artifact；
- process-local state可持续无界增长。

新实现必须在mutation之前证明完整snapshot与ack均处于bound内；TODO ack不得依赖artifact或降级表示。

### 1.5 [P2] 单项action造成动作与token二次放大

一次只能add/update一个item，却每次都返回全表。建立N项并逐项完成会形成O(N)次工具调用与近似O(N²)的重复结果正文。

完整snapshot把一次逻辑更新压成一次tool call，并让结果只返回计数。

### 1.6 [P2] lifecycle、UI与compaction未闭合

当前state：

- 在Host重新创建、takeover或resume后丢失；
- 没有exact run reset；
- 没有专用UI projection；
- 没有已实现的compaction handoff；
- 没有行为级TODO测试。

本轮闭合exact-run process-local lifecycle、一个原子live snapshot event、attach/gap后的current-state resync与future Round 5B handoff seam。丰富UI样式、跨Host恢复和durable current-state仍为非目标。

### 1.7 [P1] ROOT admission没有共同process-local终局

当前direct normal path在repository返回后直接继续；`_TurnAdmissionSettlementAttempt`只覆盖异常/取消。Queued `NEW_TURN`又由Host直接调用`consume_prompt_head()`并启动accepted turn，完全不经过Runner admission attempt。若TODO换代只挂在该attempt上，normal direct与queued都不会执行；若queued transaction已经提交而waiter丢失返回，甚至可能留下没有active task的canonical `RUNNING` turn。

本轮必须让direct fast、direct ACK confirmation与queued consume confirmation全部进入同一个幂等TODO-run activation finalizer，并为queued consumption补stable candidate与stateless `FULL | NONE | CONFLICT` confirmation。它是process-local settlement seam，不是新durable owner。

### 1.8 [P1] graceful close不能丢弃已经canonical成功的replacement

当前Runner先接受canonical ToolResult，再调用process-local settlement。若ack已经`FULL/UPDATED`，但close同时把run标为closing，丢弃prepared state会让durable transcript声称成功而surviving Host从未安装snapshot。

本轮删除`DISCARDED_CLOSING`：close先fence新工作，保留record并join exact pending token；canonical `FULL`必须先`INSTALLED`，随后才以更高revision `CLOSED`。只有`NONE/CONFLICT/stale writer`这类没有accepted winner的路径可`DISCARDED`。

### 1.9 [P2] settlement返回与child出生点不闭合

当前generic settlement port返回`None`，Runner却无条件把local effect标为committed；Terminal monitor的missing token也可能静默返回。与此同时subagent task先ACTIVE，initial child turn稍后才canonical admission，因此task acceptance时并不存在合法的`last_turn_id`。

本轮把generic process-local settlement收紧为显式`INSTALLED | DISCARDED`，并让child TODO run只在initial child turn admission `FULL`后以revision 0出生。Admission之前的child取消没有TODO record或`CLOSED` projection。

---

## 2. Prior art窄调研

### 2.1 Codex：最小snapshot与transient UI event

Codex真正的轻量工具是`update_plan`，不是Plan Mode。Schema为：

~~~json
{
  "explanation": "optional",
  "plan": [
    {"step": "string", "status": "pending | in_progress | completed"}
  ]
}
~~~

核心handler无当前list owner；每次只解析完整snapshot、发`EventMsg::PlanUpdate`，向模型返回`Plan updated`。app-server/TUI/exec各自消费transient update。

证据：

- `/Users/plumliu/Desktop/python_workspace/codex/codex-rs/core/src/tools/handlers/plan_spec.rs`：schema；
- `/Users/plumliu/Desktop/python_workspace/codex/codex-rs/core/src/tools/handlers/plan.rs`：无状态handler与ack；
- `/Users/plumliu/Desktop/python_workspace/codex/codex-rs/rollout/src/policy.rs`：PlanUpdate不作为独立rollout state持久化；
- `/Users/plumliu/Desktop/python_workspace/codex/codex-rs/tui/src/history_cell/plans.rs`：UI snapshot展示。

值得吸收：

- 完整snapshot极小schema；
- model ack与UI projection分离；
- 简单任务不滥用TODO的prompt guidance。

不照搬：

- “最多一个in_progress”只写在description而不机械校验；
- compaction没有current TODO专门交接；
- `plan`命名与真正Plan Mode混淆；
- Core没有scope-owned current state。

### 2.2 Claude Code：完整替换、per-agent state与hidden reminder

Claude Code轻量工具是`TodoWrite`：

~~~json
{
  "todos": [
    {
      "content": "string",
      "status": "pending | in_progress | completed",
      "activeForm": "string"
    }
  ]
}
~~~

它完整替换`AppState.todos[agentId/sessionId]`，通过transcript中最后一次TodoWrite输入恢复；工具结果与UI panel分离。连续约10个assistant turn未使用时，可注入隐藏、节流的`todo_reminder`。

证据：

- `/Users/plumliu/Desktop/python_workspace/claude-code/src/tools/TodoWriteTool/TodoWriteTool.ts`：schema、replace与ack；
- `/Users/plumliu/Desktop/python_workspace/claude-code/src/state/AppStateStore.ts`：per-agent/session state；
- `/Users/plumliu/Desktop/python_workspace/claude-code/src/utils/sessionRestore.ts`：transcript restore；
- `/Users/plumliu/Desktop/python_workspace/claude-code/src/utils/attachments.ts`：hidden reminder。

值得吸收：

- per-agent isolation；
- replacement而不是多action mutation；
- ToolResult与UI显示分离。

不照搬：

- `activeForm`重复表达同一个item；
- 全部完成时内部清空、返回仍含完整list的语义不一致；
- “最多一个in_progress”仍只靠prompt；
- turn-count hidden reminder可能制造噪音；
- transcript replay不应成为Pulsara新的current-state authority。

### 2.3 grok-build：稳定state owner与compaction actionable handoff

grok-build的`todo_write`支持稳定ID、merge与full replace；`TodoState`位于session Resources，另行向模型返回summary、向ACP/UI发送结构化Plan，并持久化`resources_state.json`。

最值得吸收的不是其ID/merge复杂度，而是compaction行为：

~~~text
read current TodoState
  -> include only pending/in_progress actionable items
  -> completed/cancelled只形成count
  -> no actionable items => no TODO section
  -> inject runtime-owned handoff after compaction
~~~

证据：

- `/Users/plumliu/Desktop/python_workspace/grok-build/crates/codegen/xai-grok-tools/src/implementations/grok_build/todo/mod.rs`：TodoState与todo_write；
- `/Users/plumliu/Desktop/python_workspace/grok-build/crates/codegen/xai-grok-tools/src/persistence.rs`：resource persistence；
- `/Users/plumliu/Desktop/python_workspace/grok-build/crates/common/xai-grok-compaction/src/reminder.rs`：actionable TODO handoff；
- `/Users/plumliu/Desktop/python_workspace/grok-build/crates/codegen/xai-grok-shell/src/session/compaction.rs`：compaction integration。

值得吸收：

- state owner与UI分离；
- bounded snapshot API；
- compaction从current owner重建，而非让summary模型猜；
- 只交接actionable items。

不照搬：

- stable ID、merge/full-replace双模式；
- priority/meta；
- position-generated compatibility ID；
- filesystem persistence；
- ACP Plan展示协议作为state authority。

### 2.4 综合选择

~~~text
Codex / Claude
  -> minimal full snapshot model API

grok-build
  -> explicit state owner
  -> bounded actionable compaction projection

Pulsara
  -> full snapshot + exact-run owner + Runtime compaction handoff
  -> no durable task board
~~~

---

## 3. 产品语义

### 3.1 适用场景

BASE_SYSTEM/工具description应引导模型：

- 单步、立即可完成的请求不使用TODO；
- 有2个以上可验证步骤、需要多次tool loop的小型任务可使用；
- TODO描述可观察的工作，不写空泛的“完成任务”；
- 开始一个步骤前把它标为`in_progress`；
- 完成后及时更新；
- 不把TODO当成对用户的最终答复；
- 不把TODO用于跨用户请求保存承诺。

### 3.2 三状态闭集

~~~text
TodoStatus
  PENDING
  IN_PROGRESS
  COMPLETED
~~~

本轮不提供`CANCELLED`。模型若放弃一项，在下一完整snapshot中删除它；TODO不是审计日志，不需要保留取消历史。

### 3.3 一次最多一个IN_PROGRESS

Runtime机械冻结：

~~~text
count(status == IN_PROGRESS) <= 1
~~~

允许：

- 全部pending；
- 一个in_progress，其余pending/completed；
- 全部completed；
- 空list。

不机械限制pending直接变completed，因为snapshot item没有durable identity，模型可能在一次provider call中完成一个极小步骤。该规则是progress guidance，不是业务状态机。

### 3.4 完成不等于隐式清空

提交全部`COMPLETED`时，owner仍保存该snapshot。只有：

- 模型显式提交`items=[]`；
- 下一条真实ROOT `USER_MESSAGE`已获得canonical admission `FULL`并开启新run；
- child task结束；
- Host关闭；

才清除相应process-local state。ROOT的assistant final/turn terminal只把该run标为`IDLE_RETAINED`，不清空TODO；这使UI在两条真实用户消息之间仍能展示最后状态，也避免把“turn返回”与“下一个user run已接受”混为一个linearization point。

---

## 4. Closed input contract

### 4.1 JSON Schema

~~~json
{
  "type": "object",
  "properties": {
    "items": {
      "type": "array",
      "maxItems": 64,
      "items": {
        "type": "object",
        "properties": {
          "text": {"type": "string", "minLength": 1, "maxLength": 512},
          "status": {
            "type": "string",
            "enum": ["pending", "in_progress", "completed"]
          }
        },
        "required": ["text", "status"],
        "additionalProperties": false
      }
    }
  },
  "required": ["items"],
  "additionalProperties": false
}
~~~

`maxLength`只是provider-side codepoint预筛；Runtime继续执行UTF-8 byte truth validation。

### 4.2 Runtime bounds

~~~text
maximum items                         64
maximum text UTF-8 bytes/item        512
maximum aggregate canonical JSON     32 KiB
maximum in_progress items              1
maximum exact duplicate texts          1
~~~

Text规则：

- 必须已是Unicode NFC；不在Runtime中静默改写；
- 必须等于自身`strip()`结果；
- 不得为空；
- 不得包含NUL、C0 control、line separator或多行正文；
- duplicate按NFC exact、case-sensitive text判断；不casefold代码标识符。

32 KiB quote使用唯一canonical compact JSON encoder，覆盖ordered items、field names、status与UTF-8正文；不能只加总text长度。

### 4.3 Validation order

~~~text
provider arguments
  -> JSON schema validation
  -> UTF-8/text validation
  -> item/in_progress/duplicate/aggregate validation
  -> authorize observes exact scope/run as currently active
  -> canonical ToolExecutionAttempt acceptance
  -> build exact KernelToolInvocationContext
  -> invoke rechecks exact scope/run
       closed/replaced -> known no-mutation TOOL_UNAVAILABLE/CANCELLED result
       active          -> freeze candidate + ack + settlement token
  -> return frozen ack as ToolResult candidate
  -> canonical ToolResult exact settlement
       FULL      -> atomically install replacement, then publish live snapshot
       NONE/CONFLICT/stale writer
                 -> discard replacement, publish nothing
~~~

所有预期模型错误必须在attempt前形成`INVALID_ARGUMENTS`。不得用physical exception表达：

- missing/unknown field；
- invalid status；
- duplicate；
- overbound；
- multiple in_progress。

Authorize同时验证exact run identity在当时仍active，但现有`KernelToolAuthorization`不携带prepared candidate/permit，Runner也在authorize之后直接接受attempt。本轮因此明确选择更小的竞态语义：

- schema/text/item/bound等预期模型错误仍在attempt前返回`INVALID_ARGUMENTS`；
- authorize后发生的run close/replacement可以已接受attempt；
- invoke通过exact context重验后返回已知、无mutation的`TOOL_UNAVAILABLE/CANCELLED`结果；
- 该分支不生成prepared replacement、settlement token或LiveEvent，不表达unknown effect。

不引入`PreparedTodoDispatchPermit`或pre-attempt consume seam。Authorize与invoke调用同一个pure validator；invoke重建immutable candidate是机械复验，不是第二套semantic authority。

---

## 5. Process-local owner

### 5.1 Pure DTO

~~~text
TodoStatus

FrozenTodoItem
  ordinal
  text
  status
  item_fingerprint

TodoRunIdentity
  session_id
  scope_kind: ROOT | SUBAGENT_TASK
  root_run_id? | subagent_task_id?
  owner_epoch
  identity_fingerprint

TodoRunPhase
  ACTIVE
  IDLE_RETAINED
  CLOSING

FrozenTodoSnapshot
  run_identity
  revision
  ordered_items
  pending_count
  in_progress_count
  completed_count
  snapshot_fingerprint

TodoRunRecord
  run_identity
  phase
  last_turn_id
  current_snapshot
  bounded pending_settlement_token_ids

PreparedTodoReplacement
  run_identity
  attempt_id
  proposed_result_entry_id
  candidate_fingerprint
  acknowledgement_fingerprint
  frozen_items/counts
  token_id/fingerprint

TodoInstallation
  installed_snapshot
  optional_live_projection

ProcessLocalEffectSettlementResult
  outcome: INSTALLED | DISCARDED

FrozenTodoCloseProjection
  run_identity
  last_turn_id
  closing_revision = final_installed_revision + 1
  disposition = CLOSED
~~~

`ordinal`只用于当前snapshot排序/UI；不是模型可引用的稳定ID，不跨replacement承诺identity。

### 5.2 TodoRunStateOwner

~~~text
TodoRunStateOwner
  lock
  ROOT TodoRunRecord?
  child task id -> TodoRunRecord

  activate_root_run(PreparedTodoRootRunActivation, exact FULL admission)
  activate_child_run(PreparedTodoChildRunActivation, exact FULL admission)
  bind_root_continuation(exact turn)
  mark_root_idle(exact run/turn)
  close_root_run(...) -> FrozenTodoCloseProjection?
  bind_child_turn(...)
  close_child_run(...) -> FrozenTodoCloseProjection?
  prepare_replace(candidate) -> PreparedTodoReplacement
  commit(prepared, accepted result identity) -> TodoInstallation
  discard(prepared)
  snapshot(exact identity) -> FrozenTodoSnapshot
  current_snapshots() -> bounded exact-scope tuple
~~~

它是唯一current TODO owner。`TodoTool`变成薄adapter，不再拥有`_items/_next_id`。

### 5.3 Exact run identity

ROOT：

- Host可以在canonical admission之前安装ROOT task，但这不得换代TODO run；
- direct或queued的每条真实`ROOT USER_MESSAGE`只在exact admission确认`FULL`后开启新`root_run_id`；
- 换代必须发生在该accepted turn第一次source collection/compiler之前；
- `USER_STEER`、tool loop与automatic continuation继承同一个identity；
- Plan/Terminal/external-result continuation在同一surviving Host仍有run时只更新`last_turn_id`，不创建新run；若writer takeover、resume或Host replacement已按本轮弱完成契约丢失process-local owner，则该绑定是明确no-op，canonical continuation必须继续运行，TODO tools保持typed unavailable，直到下一条accepted human ROOT message创建新run；
- ROOT chain terminal后进入`IDLE_RETAINED`；
- 下一条accepted human ROOT message在同一Host lock内冻结旧run `CLOSED`、再创建空的新run。

Child：

- identity绑定exact `subagent_task_id`；
- subagent task acceptance/activation本身不创建TODO run；只有initial child turn的canonical admission确认`FULL`后，才在其第一次source collection/compiler之前创建空run；
- 若child在initial turn admission `FULL`之前取消、失败或Host关闭，不创建record，也不发布虚假的`CLOSED`；
- initial empty snapshot固定为revision `0`，第一次state-changing replacement固定为revision `1`；
- child manager结束task时清除；
- child不能访问ROOT或其他child snapshot。

Plan Mode拥有自己的Plan workflow authority；TODO不得跨Plan handoff自动变成Plan draft，也不得从Plan draft自动生成TODO。

#### 5.3.1 共同ROOT admission finalizer

`_TurnAdmissionSettlementAttempt`只存在于direct admission异常/取消路径，不能拥有正常fast path或queued delivery的TODO换代。本文新增一个窄、pure、process-local carrier：

~~~text
PreparedTodoRootRunActivation
  session_id
  admission_kind: DIRECT | QUEUED
  command_id? | queue_item_id + queue_sequence?
  exact_turn_id
  exact_initial_entry_id
  exact_context_binding_revision_id
  proposed_root_run_id
  exact_admission_candidate_fingerprint
  activation_fingerprint
~~~

它不拥有数据库写入，也不是receipt。Direct分支从`PreparedRootTurnAdmission`确定性构造；queued分支从下述stable queue consumption candidate确定性构造。Host/TODO owner提供唯一幂等`finalize_todo_root_run_activation(prepared, accepted)`：

~~~text
ROOT USER_MESSAGE candidate prepared
  -> repository direct admission or queued consumption/confirmation
       FULL
         -> invoke the common finalizer exactly once
         -> under Host/Todo owner lock:
              freeze old run CLOSED projection
              create revision-0 empty ACTIVE run bound to accepted turn
         -> offer old CLOSED projection outside lock
         -> first provider-input collection may begin
       NONE / CONFLICT / stale writer
         -> discard prepared activation
         -> old TODO run remains unchanged
~~~

Direct fast-path `FULL`、direct ACK confirmation `FULL`与queued consume confirmation `FULL`必须全部调用同一个finalizer；不得让任一路径自行关闭/创建run。重复`FULL`按turn/root-run identity幂等。Finalizer在返回前完成，因此任何首次compile、active task body或provider open都只能发生在它之后。

`proposed_root_run_id`必须由session与exact accepted turn identity确定性派生；同一direct/queued candidate的重试或confirmation不得签发第二个run ID。

Finalizer先在锁外构造new empty record与old close projection，再按唯一锁序`Host lock -> TodoRunStateOwner lock`完成一次不可分割的old/new tuple assignment；任何TODO path不得反向取得Host lock，锁内没有await、I/O或Live offer。ROOT进入`IDLE_RETAINED`之前必须已join其tool settlement tasks，因此next accepted human message看到的old run不得仍有pending token；违反者是invariant failure，不能在finalizer内等待。

Finalizer失败是process-local invariant failure。Runtime必须启动一个shielded exact-turn interruption owner并等待它把该accepted turn终结；仅仅“不继续provider open”不足以关闭数据库中已为`RUNNING`的winner。Direct admission已经`FULL`后若caller在finalizer期间取消，同样必须先等待finalizer，再由该owner终结exact turn，最后才向外重抛cancellation；此时普通run loop尚未启动，不能依赖其failure settlement。该interruption复用现有turn terminalization与confirmation，不通过durable repair或event replay补做。

#### 5.3.2 Queued ROOT consumption的stable confirmation

现有`consume_prompt_head()`把选head、生成turn/entry与消费queue放在一个transaction，却没有ACK-unknown confirmation。实施时必须先从FIFO head冻结一个stable candidate：

~~~text
PreparedQueuedRootTurnAdmission
  session/workspace
  queue_item_id + queue_sequence + command/content/permission fingerprints
  exact new turn/entry/context-binding IDs
  exact occurrence drafts/fingerprints
  candidate_fingerprint

QueuedRootTurnAdmissionConfirmation
  FULL(accepted entry)
  NONE
  CONFLICT
~~~

Repository提供`consume_prepared_prompt_head(...)`与stateless `confirm_prepared_prompt_head_consumption(...)`。`FULL`必须exact join queue row为`CONSUMED`、`consumed_entry_id`、ROOT turn、initial `USER_MESSAGE` entry、revision-0 context binding与两条既有committed occurrences；`NONE`只允许原head仍为同一`PENDING` candidate且上述winner全部不存在；其他组合均为`CONFLICT`。旧`consume_prompt_head(...)` mutation seam必须物理删除，retained test也只能通过stable candidate路径，不得保留一条caller-supplied identity的旁路。

Queued physical call返回`FULL`时直接进入共同finalizer；异常、timeout或waiter cancellation时由唯一shielded settlement task持续确认。确认`FULL`后：Host仍open则先finalize TODO run再安装exact active task；Host已closing或任务安装失败则先finalize、再shieldedly interrupt exact accepted turn。确认`NONE`可重新消费同一candidate，`CONFLICT`终止该delivery attempt。这样不会留下“queue已消费且turn为RUNNING、但没有task/finalizer owner”的canonical孤儿。

这只是把queued admission补到Round 5A已经采用的stable-candidate + stateless exact-confirm模式；不新增relation、event、receipt或recovery owner。

#### 5.3.3 Child initial admission finalizer

Child使用独立窄carrier `PreparedTodoChildRunActivation`，绑定`session_id + subagent_task_id + exact initial turn/entry/context binding + admission candidate fingerprint`。Subagent task先被接受并不触发它；initial child turn的direct fast-path或ACK confirmation得到`FULL`后，同一个admission终局在首次compile前创建revision-0空run。激活失败同样shieldedly interrupt exact child turn，并由现有subagent task terminalization收口；`FULL`之前的取消没有TODO lifecycle。

### 5.4 Atomic replacement与并发

TODO复用Round 5A已有`ProcessLocalEffectSettlementToken`，不建第二套settlement owner。Invoke只冻结prepared replacement与small ack；只有canonical ToolResult被exact settlement为`FULL`后，`COMMITTED`分支才在owner lock内完成一次immutable tuple assignment。`DISCARDED`分支不修改owner。

此处`FULL`是repository confirmation的`FULL | NONE | CONFLICT`，不是Round 7.1 provider-visible ToolResult representation的`FULL`。TODO current state不依赖下一次compiler是否能立即打开provider，但必须依赖canonical acknowledgement已经精确接受。

Validation、fingerprint与ack在锁外完成，因此commit锁内没有I/O、await、JSON parsing或provider work。Live notification在assignment之后best-effort offer，不在owner lock内执行，失败也不回滚已安装snapshot。

通用`KernelToolPort.settle_process_local_effect(...)`不得继续返回`None`。它必须返回closed `ProcessLocalEffectSettlementResult(outcome=INSTALLED | DISCARDED)`；Terminal monitor与TODO都实现同一接口，Runner不得按tool name特判。Runner只有在返回`INSTALLED`时才能设置`process_local_effect_committed=true`；`DISCARDED`明确表示没有安装local effect。

`PreparedTodoReplacement`必须exact join run identity、attempt ID、proposed/accepted result entry ID、candidate fingerprint与ack fingerprint。`settle_process_local_effect(COMMITTED)`不得像当前monitor-only implementation一样在token missing时静默返回：

- canonical confirmation `FULL`时，无论run仍`ACTIVE`还是已经`CLOSING`，TODO token都必须先返回`INSTALLED`并安装exact snapshot；`CLOSING`不是丢弃已接受ack的理由；
- token missing、fingerprint/attempt/result conflict或run identity drift是invariant failure，Runner必须interrupt该turn，不得设置`process_local_effect_committed=true`；
- 只有canonical confirmation为`NONE/CONFLICT`或writer已经stale、因而没有exact accepted ToolResult winner时，cleanup才返回`DISCARDED`；
- close不得直接丢弃未结算token；它先关闭新admission并join exact runner及其shielded settlement task。Join完成后owner必须机械断言pending token为空；若仍有token，这是没有推进owner的invariant failure，close继续清理其他physical owner并最终报告错误，不得在裸condition上无限等待。

这只是收窄现有process-local settlement返回值，不增加receipt、durable row或recovery graph。

不同scope可以各自独立更新；同scope由owner lock/revision顺序串行。因为模型每次提交完整snapshot，后一个accepted replacement自然成为current winner；本轮不引入CAS retry或durable revision。

### 5.5 Host lifecycle

Run record必须单独保存`last_turn_id`。每次exact continuation换代current turn时在Host lock内更新该字段，但不改变model-visible snapshot revision。

Close是一个唯一shielded process-local task。它在owner lock内先把record标成`CLOSING`并fence新provider/tool admission，但不删除record，也不提前冻结revision：

~~~text
mark exact run CLOSING
  -> stop new same-run TODO/provider admission
  -> join every already-prepared exact settlement token outside owner lock
       canonical FULL
         -> install exact snapshot at revision N+1
         -> best-effort offer ACTIVE/CLEARED N+1
       NONE / CONFLICT / stale writer
         -> discard token; revision remains N
  -> reacquire owner lock after pending set is empty
  -> freeze CLOSED at final installed revision + 1 using record.last_turn_id
  -> remove current record
  -> release lock
  -> best-effort offer CLOSED
~~~

因此在同一surviving Host的正常结算/close路径中，canonical ToolResult若声称`UPDATED/CLEARED`，owner必然实际安装过对应snapshot；closing竞态最多形成相邻的`INSTALLED -> CLOSED`，不能形成虚假的successful transcript。重复close不发布第二个event，也不保留closed tombstone authority。Host close在Live bus物理close之前尝试发布projection；即使best-effort offer失败，client断开/新Host空snapshot仍会清除UI。Writer takeover/new Host从空state开始；不读取旧ToolResult重建current snapshot，也不通过committed-event replay执行TODO mutation。

这是明确的advisory weak-completion语义：Host crash可能丢失TODO，但不能丢失业务事实、文件effect或conversation canonical rows。

---

## 6. ToolResult与provider/UI投影

### 6.1 Model-visible ack

成功replacement只返回small closed JSON：

~~~json
{
  "status": "UPDATED",
  "counts": {
    "pending": 2,
    "in_progress": 1,
    "completed": 3,
    "total": 6
  }
}
~~~

空snapshot：

~~~json
{
  "status": "CLEARED",
  "counts": {
    "pending": 0,
    "in_progress": 0,
    "completed": 0,
    "total": 0
  }
}
~~~

ToolResult不重复items、不产生artifact、不携带run/owner/revision/fingerprint。模型可从自己刚发出的tool-call arguments看到完整snapshot。

### 6.2 Domain rejection

由于closed validation发生在attempt前，预期错误复用现有`INVALID_ARGUMENTS` no-attempt结果。消息应指出一个可修复原因，例如：

~~~text
todo accepts at most one in_progress item
todo item text exceeds 512 UTF-8 bytes
todo contains duplicate item text
todo scope is no longer active
~~~

不得返回Python exception type或内部fingerprint。

### 6.3 UI边界

本轮增加一个、且只增加一个TODO live event：

~~~text
LiveEventType.TODO_SNAPSHOT_UPDATED = "TodoSnapshotUpdated"

TodoLiveDisposition
  ACTIVE
  CLEARED
  CLOSED

LiveTodoItemProjection
  ordinal
  text
  status

TodoSnapshotUpdatedPayload
  todo_run_id
  todo_revision
  disposition
  ordered_items
  pending_count
  in_progress_count
  completed_count
~~~

不使用`TODO_START / TODO_DELTA / TODO_END`，也不发per-item event。一次accepted replacement只产生一个原子full-snapshot payload；它本身已受64 items、512 UTF-8 bytes/item与32 KiB aggregate bound限制。

Disposition语义：

- `ACTIVE`：current run的非空snapshot，包含pending/in-progress/completed全部条目；
- `CLEARED`：模型显式提交`items=[]`，items与counts必须为空/0；
- `CLOSED`：ROOT run被下一条canonical-admitted human user message取代、child task结束或Host正在关闭；items与counts必须为空/0，`todo_revision = final installed revision + 1`，client删除exact `todo_run_id`。

`todo_run_id`是Host签发的opaque、public-safe运行identity；`todo_revision`只在exact run内单调。Session、ROOT/child scope、subagent task与turn attribution继续由既有`LiveEventProjection`外层携带，payload不重复这些字段。内部fingerprint、owner epoch和canonical row identity不进入payload。

`LiveEventProjection`外层冻结为：

~~~text
turn_id                         exact replacement turn or closing run's last turn
scope_kind / scope_subagent... exact TodoRunIdentity scope
channel_kind                    TERMINAL_EXTENSION
draft_identity                  todo:<todo_run_id>:<todo_revision>
generation_id                   todo:<todo_run_id>
block_id                        same as draft_identity
block_ordinal                   0
block_kind                      OPERATIONAL
proposed_entry_id               absent
~~~

这是UI/observer扩展通道，不是assistant/result draft；因此不伪造`proposed_entry_id`，也不将canonical result entry ID塞进live identity。

普通replacement event只能在canonical ToolResult acceptance确认`FULL`、process-local settlement返回`INSTALLED`且owner已安装exact snapshot后offer。`INVALID_ARGUMENTS`、no-attempt、`NONE`、`CONFLICT`、stale writer或`DISCARDED`都不能发布TODO update。Lifecycle `CLOSED`没有ToolResult，由exact run owner等待pending settlement全部终结、冻结final revision并删除record后best-effort offer。Live bus不可反向调用owner或回滚state。

UI必须将item text当作untrusted model-generated data进行转义；不执行ANSI/control sequence、Markdown command或Plugin directive。TODO event不进入provider input、compaction summary或Hook authority。

### 6.4 Initial attach与gap resync

Live ring是bounded的；event可丢失，因此`TodoSnapshotUpdated`绝不是current-state重建来源。现有`SessionLiveControlSnapshot`增加一个bounded完整清单：

~~~text
LiveTodoRunSnapshot
  todo_run_id
  todo_revision
  scope_kind
  scope_subagent_task_id?
  disposition: ACTIVE | CLEARED
  ordered_items
  pending_count
  in_progress_count
  completed_count

SessionLiveControlSnapshot
  ... existing fields ...
  current_todos: ordered tuple[LiveTodoRunSnapshot]
~~~

`current_todos`由gateway在处理snapshot request时直接读取`TodoRunStateOwner.current_snapshots()`。它是当前Host上最多一个ROOT加最多四个child scope的完整inventory；client必须整体替换本地TODO map，清单中缺失的旧run即为已关闭。`SessionLiveControlSnapshot.live_revision`仍只表达现有interaction-control revision；TODO顺序使用每个record自带的`todo_run_id + todo_revision`，不伪造跨owner原子revision。

为避免baseline与owner snapshot之间的lost-update window，顺序必须冻结为**先建立live baseline，再读TODO owner**：

~~~text
initial attach
  -> server subscribe LiveAgentEventBus and freeze baseline B
  -> client records B but does not resume observe yet
  -> request SessionLiveControlSnapshot
  -> server reads TodoRunStateOwner after B
  -> client atomically replaces local TODO map
  -> resume observe strictly after B

contiguous live events
  -> apply TodoSnapshotUpdated only when run/revision is newer

LIVE_GAP / live owner epoch change
  -> server installs a fresh subscription/baseline B2
  -> client pauses observe and discards incremental assumptions
  -> request SessionLiveControlSnapshot again
  -> server reads TodoRunStateOwner after B2
  -> client atomically replaces local TODO map
  -> resume observe strictly after B2
~~~

Baseline之前的更新必然反映在随后的owner snapshot中；baseline之后的更新必然进入新subscription。两者之间并发的更新可在snapshot与event中各出现一次，client按`todo_run_id + todo_revision`幂等处理。新Host的inventory为空，不从旧event、ToolResult或committed-event replay恢复。

TUI/Plugin Hook/diagnostic只能消费这个immutable projection，不能写回或成为current-state owner；也不得解析model-visible ToolResult JSON重建状态。

---

## 7. Compaction handoff

### 7.1 Runtime负责精确状态，summary模型不负责TODO

Summary模型只总结语义进展。TODO由Runtime从`TodoRunStateOwner`读取并重建；summary prompt不得要求模型复制、修复或判断当前TODO。

这继承grok-build最值得吸收的边界：

~~~text
semantic summary       model-owned
current TODO snapshot  Runtime-owned
~~~

### 7.2 复用COMPACTION_RUNTIME_HANDOFF

不新增`TODO_CONTEXT` compiler source。Round 5B现有`COMPACTION_RUNTIME_HANDOFF`继续组合Terminal/monitor/TODO/flat subagent状态；其中TODO subshape由本文替换为：

~~~json
{
  "todos": [
    {"ordinal": 0, "status": "in_progress", "text": "Inspect failure"},
    {"ordinal": 1, "status": "pending", "text": "Implement fix"}
  ],
  "todo_counts": {
    "pending": 1,
    "in_progress": 1,
    "completed_omitted": 3
  }
}
~~~

旧Round 5B示例中的TODO `id`不再存在；`ordinal`只表达本次投影的ordered position，模型后续仍提交完整snapshot，不引用ordinal做mutation。

### 7.3 Selection

Compaction只注入：

- `PENDING`；
- `IN_PROGRESS`。

`COMPLETED`正文不注入，只给`completed_omitted` count。若没有actionable item：

~~~text
todos = []
pending = 0
in_progress = 0
completed_omitted may be non-zero
~~~

Runtime renderer可在整个handoff不存在其他live state时省略空TODO section。不得为展示completed历史挤占rebase预算。

### 7.4 Exact freeze与adoption

Active mid-run compaction：

~~~text
summary completes
  -> exact-scope compaction fence remains installed
  -> freeze current Todo snapshot
  -> build bounded runtime handoff
  -> dry compile successor input
  -> before adoption, confirm snapshot fingerprint unchanged
       same     -> adopt
       changed  -> discard dry compile and rebuild from new snapshot
~~~

TODO owner不属于repository REPEATABLE READ transaction；它在summary后单独freeze，并进入process-local handoff fingerprint。不得在summary开始前读取一次后盲用几分钟后的旧state。

Idle compaction不冻结未来run TODO；下一条真实ROOT user message开启空TodoRunIdentity。

### 7.5 Bounds与trust

- 仍使用最多64项、每项512 UTF-8 bytes；
- 因只选择actionable，结果必为原snapshot子序列；
- TODO handoff与其他runtime state一起受Round 5B aggregate 32 KiB bound；
- `trust=UNTRUSTED_OBSERVATION`；
- 不进入BASE_SYSTEM或provider tools；
- rebase后作为cold successor message/context suffix安装；
- 不跨unrelated ROOT run或child scope注入。

若连最小runtime handoff也无法放入successor input，遵循Round 5B typed resource boundary；不得静默让旧TODO文本冒充current state。

### 7.6 普通运行不增加hidden reminder

V1不复制Claude的“每10个assistant turn提醒一次”机制。理由：

- 小型TODO的call arguments已在当前上下文；
- ToolResult ack足以确认替换；
- compaction有精确Runtime handoff；
- turn-count reminder可能在模型已经选择不再使用TODO时制造噪音。

如dogfood证明模型频繁遗忘，后续可单独设计bounded、state-change-aware reminder；不能在本轮凭固定turn数加入。

---

## 8. Permission、effect与failure contract

### 8.1 Permission

TODO只改变Agent自己的advisory process-local state：

- provider exposure保持direct builtin；
- `permission_category=agent_local`；
- READ_ONLY permission preset仍允许；
- 不请求human confirmation；
- 不授予filesystem、terminal、MCP或memory权限。

Descriptor可继续把`is_read_only=true`解释为“对用户/外部世界没有effect”，但代码注释与binding必须明确它存在local advisory mutation，不能据此声称函数可并发或无状态。

### 8.2 不再进入unknown-effect path

TODO replacement满足：

- 全部validation在attempt前；
- 无网络、数据库、filesystem或subprocess；
- bounded immutable candidate与ack先构造；
- lock内只做一次process-local assignment；
- 同一snapshot replacement幂等。

实现应在`DirectKernelToolPort`中使用专门的bounded local-state branch，而不是把TodoTool投入可能跨deadline的generic physical worker。预期domain error产生no-attempt invalid result；authorize/invoke之间exact run关闭产生known no-mutation result；unexpected internal fault可终止该调用，但不得表达外部effect unknown或触发自动replay。

为复用现有closed recovery vocabulary，Builtin catalog将`todo`的`recovery_contract.severity`冻结为`read_only`，其含义严格限定为“没有用户/外部世界effect”。这不否认owner内部发生local advisory replacement；安全性来自上述prebuild + single assignment形状，而不是把TODO谎称为无状态。

### 8.3 Cancellation

TODO不得把“worker已返回”当作“current state已安装”。它复用Round 5A process-local settlement seam：

- canonical ToolResult settlement前取消 -> prepared replacement仍未安装；
- caller cancellation只detach waiter，existing exact settlement owner继续确认ToolResult；
- confirmation `FULL` + active/closing run -> `COMMITTED/INSTALLED`一次安装snapshot、再发布一个live event；closing owner随后以更高revision发布`CLOSED`；
- `NONE/CONFLICT`、stale writer -> `DISCARDED`，state不变、无event；
- active或closing run上的missing/conflicting token -> invariant failure并interrupt，不得伪报committed；
- settlement commit完成后的waiter cancellation不能回滚owner或重复发布event。

不得在assignment之后运行可抛异常的renderer、artifact publication或blocking UI callback。Live notification是bounded best-effort offer；通知失败由snapshot resync弥补，不得改变ToolResult或TODO state。

---

## 9. Implementation modification map

### 9.1 Production files

- `src/pulsara_agent/tools/builtins/todo.py`
  - 删除action union、mutable `_items/_next_id`；
  - 新增pure parser/validator、DTO与thin adapter；
- `src/pulsara_agent/capability/builtin_catalog.py`
  - 替换input schema与description；
  - 保持direct/agent_local/local_state分类；
  - 收窄recovery语义，不再进入unknown external effect；
- `src/pulsara_agent/conversation_kernel/tool_runtime.py`
  - 安装唯一`TodoRunStateOwner`；
  - authorize执行closed semantic validation；
  - invoke使用exact run context与bounded local-state branch；
  - 复用`ProcessLocalEffectSettlementToken`在canonical FULL后安装replacement；
  - generic settlement port返回closed `INSTALLED | DISCARDED`；TODO canonical `FULL`只允许`INSTALLED`，token missing/conflict不得静默成功；
  - Terminal monitor迁移到同一显式返回契约，Runner不对TODO作tool-name特判；
  - 安装后offer一个`TodoSnapshotUpdatedPayload`；
  - 提供read-only snapshot API；
- `src/pulsara_agent/conversation_kernel/runner.py`
  - direct ROOT fast-path与ACK confirmation都在`FULL`后、首次compile前执行共同TODO-run finalizer；
  - child initial turn fast-path与ACK confirmation在`FULL`后创建revision-0 TODO run；
  - TODO invoke前使用exact context重验run，authorize后lifecycle race返回known no-mutation result；
  - 只有settlement明确返回`INSTALLED`才设置process-local effect committed；
- `src/pulsara_agent/conversation_kernel/host.py`
  - Host task安装不换代TODO run；
  - queued NEW_TURN使用stable prepared consumption与stateless `FULL | NONE | CONFLICT` confirmation；
  - direct/queued共同finalizer原子关闭旧run/开启新run；
  - accepted turn的finalizer/task安装失败由shielded exact interruption收口；
  - successor continuation继承并更新`last_turn_id`；
  - ROOT terminal进入`IDLE_RETAINED`；
  - Host close先fence、join exact runner/settlement task，再断言owner没有pending token，清理并best-effort发布`CLOSED`；
- `src/pulsara_agent/conversation_kernel/subagent.py`
  - child task acceptance不创建TODO run；initial child turn admission `FULL`后注册exact scope，结束时drain/清理并发布`CLOSED`；
- `src/pulsara_agent/conversation_kernel/_repository/prompts.py`
  - 将queued NEW_TURN head冻结为stable candidate；
  - 增加prepared consume与stateless exact confirmation，不改变既有queue/turn/event authority；
  - 删除旧`consume_prompt_head()` mutation seam；
- `src/pulsara_agent/conversation_kernel/vocabulary.py`
  - 新增唯一`LiveEventType.TODO_SNAPSHOT_UPDATED`；
- `src/pulsara_agent/ports/live_agent_event.py`与`src/pulsara_agent/conversation_kernel/live.py`
  - 新增bounded payload DTO与exact event/payload registry mapping；
  - 将live vocabulary架构断言由23更新为24；
- `src/pulsara_agent/conversation_kernel/live_control.py`
  - 组合read-only current TODO inventory，不接管TODO mutation authority；
- `src/pulsara_agent/terminal_protocol/schema/terminal_kernel_v3.proto`、generator与`v3_gateway.py`
  - 新增`TODO_SNAPSHOT_UPDATED = 24`、payload、`LiveTodoRunSnapshot`与`SessionLiveControlSnapshot.current_todos`；
  - 保留现有interaction live-control revision语义；
  - initial attach/LIVE_GAP严格先建立live baseline、再读TODO owner snapshot；
- Protocol v3 renderer-neutral consumer contract
  - consumer验证并安装atomic TODO event；
  - initial attach或`LIVE_GAP`后暂停observe，从current snapshot整体resync后再从新baseline继续；
  - consumer用`todo_run_id + todo_revision`拒绝stale/duplicate update；
- Round 5B future implementation
  - 读取本文snapshot API；
  - 使用无ID actionable TODO subshape；
  - 将整个`COMPACTION_RUNTIME_HANDOFF`冻结为`UNTRUSTED_OBSERVATION`；
  - 不复制第二套TODO owner。

### 9.2 Prompt/description

Tool description必须足以让模型知道：

~~~text
Maintain a small checklist for the current run by replacing the complete list.
Use it for multi-step work, not simple one-step answers. Keep at most one item
in_progress. Submit an empty list to clear it.
~~~

不需要长篇few-shot或隐藏system protocol。状态、bounds与错误由Runtime保证。

### 9.3 明确不修改

- storage migration；
- repository relation/CommittedEvent/job；
- Plan workflow；
- memory subsystem；
- provider adapter；
- MCP/Skill/Plugin contracts。

---

## 10. Test plan

### 10.1 Pure contract

- empty list合法并清空；
- 1..64项合法；第65项在attempt前拒绝；
- pending/in_progress/completed enum；
- two in_progress拒绝；
- exact duplicate拒绝；
- NFC、leading/trailing whitespace、newline/control拒绝；
- 512 UTF-8 bytes边界覆盖ASCII/CJK/emoji；
- 32 KiB canonical JSON quote边界；
- unknown fields拒绝。

### 10.2 Mutation与ack

- one call在canonical result confirmation `FULL`后完整安装多个items；
- later call完整替换而不是merge；
- completed-only snapshot保留；
- empty snapshot显式clear；
- invalid candidate不改变prior snapshot；
- ack只有status/counts，不包含item text、fingerprint或run identity；
- TODO结果永不生成artifact/HEAD_TAIL。

### 10.3 Scope/lifecycle

- ROOT看不到child；
- child A看不到child B；
- subagent task已ACTIVE但initial child turn尚未admission `FULL`时不创建TODO run；此时取消/失败无`CLOSED`；
- child initial turn的fast-path与ACK confirmation `FULL`都在首次compile前创建revision-0空run；
- child结束先drain exact pending settlement再清理；
- empty run revision为0，首次state-changing replacement revision为1；
- USER_STEER/tool loop/automatic continuation继承；
- Host task安装但ROOT USER_MESSAGE尚未canonical `FULL`时，旧run不变；
- direct与queued ROOT USER_MESSAGE都在canonical `FULL`后、首次compile前换代新run；
- admission `NONE/CONFLICT/stale writer`不关闭旧run；
- direct fast path、direct ACK与queued confirmation的`FULL`执行同一幂等finalizer；
- queued consume ACK unknown不会留下无task的RUNNING turn；finalizer/task安装失败会interrupt exact accepted turn；
- ROOT terminal后保留`IDLE_RETAINED`，下一accepted human USER_MESSAGE才从空snapshot开始；
- Plan/Terminal/external continuation在owner仍存在时继承同run并更新`last_turn_id`；Host replacement后的空owner不得阻断这些已接受的canonical continuation，也不得伪造替代snapshot/event；
- Host close/new Host从空state开始；
- concurrent different-scope updates不互相覆盖；
- same-scope replacements按owner顺序原子完成。

### 10.4 Live projection与resync

- 每个accepted non-empty replacement只发布一个`ACTIVE` full snapshot event；
- `items=[]`只发布一个`CLEARED`，不发start/delta/end或per-item event；
- next ROOT run、child close与Host close对exact old run发布`CLOSED`；
- `CLOSED` revision严格等于pending settlement drain后的final installed revision + 1，并使用owner保存的exact `last_turn_id`；
- duplicate close不发布第二个`CLOSED`；
- payload items/counts与owner snapshot逐项相等，不含internal fingerprint/canonical ID；
- ROOT、child A、child B的run identity、scope与revision不混淆；
- invalid/no-attempt、canonical `NONE/CONFLICT`、discarded settlement和stale writer都无update event；
- process-local state仅在`COMMITTED`安装，caller cancellation不造成半snapshot或重复event；
- closing与canonical `FULL`竞态形成`INSTALLED`后紧随更高revision的`CLOSED`，不得丢弃successful replacement；
- initial attach先建live baseline，再通过`SessionLiveControlSnapshot.current_todos`安装当前完整inventory；
- event ring裁剪或`LIVE_GAP`后先安装新baseline、暂停observe、重读snapshot，再继续observe；
- baseline前/后与owner-snapshot间的竞态测试证明无lost update；
- snapshot/event重叠交付通过`todo_run_id + todo_revision`幂等；
- 新Host的snapshot为空，不从event ring或ToolResult重建owner；
- UI转义untrusted TODO text，不解析ToolResult作current-state authority；
- TODO event与snapshot字节不进入provider input或compaction summary。

### 10.5 Runner与failure

- malformed input -> noattempt、provider-visible invalid arguments；
- internal bounded replacement正常产生canonical attempt/result；
- permission READ_ONLY仍允许；
- 不触发human confirmation；
- expected validation错误不进入physical worker；
- authorize/invoke间scope关闭 -> known no-mutation result；
- authorize后close可以存在accepted attempt，但无prepared replacement/token/event；
- active或closing run上canonical `FULL`的COMMITTED settlement必须返回`INSTALLED`，Runner才标记effect committed；
- missing/conflicting TODO token必须invariant-fail并interrupt，不能静默使用旧snapshot；
- generic TODO/Terminal-monitor settlement必须显式返回`INSTALLED | DISCARDED`，不能用`None`暗示成功；
- `DISCARDED`只用于没有canonical FULL winner的`NONE/CONFLICT/stale writer`清理；
- no unknown-effect interruption；
- caller cancellation不留下半snapshot。

### 10.6 Compaction retained contract

在Round 5B实现时必须覆盖：

- pending/in_progress按原顺序注入；
- completed正文不注入，只计数；
- `COMPACT`只保留能完整放入的ordered whole-item prefix；每项必须同时包含ordinal/status/text，并给出exact omitted count；
- 不允许把ordinal伪装成稳定ID，也不允许删除text后仍声称交接current TODO；
- no actionable items不产生TODO正文；
- ROOT/child exact scope；
- summary模型输出不决定TODO；
- summary期间state fingerprint变化导致dry compile重建；
- successor input包含current actionable snapshot；
- 整个`COMPACTION_RUNTIME_HANDOFF`的trust为`UNTRUSTED_OBSERVATION`；
- old epoch SYSTEM/tools保持；cold successor使用正常rebase；
- no durable TODO row/CommittedEvent/job。

### 10.7 Retained gates

- Round 3/3.1 compiler与continuity；
- Round 5A tool attempt/late exact settlement；
- Round 7/7.1 ToolResult projection；
- ROOT/child subagent scope；
- full pytest/PostgreSQL marker；
- Ruff、compileall、Protocol generator与architecture gates；
- `uv lock --check`、`git diff --check`；
- architecture oracle`31 / 24 / 13 / 2 / 25 / 1`。

---

## 11. Definition of Done

只有同时满足以下条件，本文才可标记`ACTIVATED`：

1. 旧`add/update/list/clear` schema已删除，没有compat adapter；
2. `todo(items=[...])`一次完整替换；
3. closed bounds与最多一个in_progress由Runtime机械验证；
4. 所有expected invalid input在attempt前拒绝；
5. current state由唯一exact-run process-local owner持有；
6. ROOT/child与不同child完全隔离；
7. ROOT TODO run只在canonical USER_MESSAGE admission `FULL`后、首次compile前换代；direct fast、direct ACK与queued confirmation全部经过同一finalizer；
8. ROOT terminal保留`IDLE_RETAINED`，next accepted human message与child close执行正确lifecycle reset；
9. authorize后lifecycle race表达为accepted attempt + known no-mutation result，不发明permit；
10. canonical `FULL`的TODO settlement在ACTIVE/CLOSING均必须返回`INSTALLED`；`DISCARDED`只对应无winner，missing/conflict不得静默成功；
11. ToolResult只返回small ack，不复制全表、不产生artifact；
12. TODO不进入unknown external effect路径；
13. Host crash/takeover丢失TODO被明确接受，不建立durable recovery；丢失后的Plan/Terminal/external-result continuation继续运行但不重建TODO，下一accepted human message才创建新run；
14. Round 5B消费同一个snapshot owner，只交接actionable items；
15. 整个`COMPACTION_RUNTIME_HANDOFF`使用`UNTRUSTED_OBSERVATION`，summary模型不拥有或改写current TODO；
16. BASE_SYSTEM/tools在运行epoch内不因TODO更新而改变；
17. messages只通过普通assistant tool call/ToolResult追加suffix；
18. 只新增一个`TodoSnapshotUpdated` Live event与对应Protocol projection；
19. `CLOSED`在pending settlement全部终结后使用final installed revision + 1与owner保存的last turn；
20. initial attach/gap严格先建live baseline、再读owner snapshot，不使event replay成为current-state authority；
21. 无storage schema、CommittedEvent、job、guard或durable recovery变化；
22. oracle精确为`31 / 24 / 13 / 2 / 25 / 1`；
23. targeted、retained、full与quality gates全部通过。

---

## 12. 最终冻结

~~~text
accepted human ROOT USER_MESSAGE
  -> direct/queued canonical admission FULL
  -> common idempotent activation finalizer
  -> close old TODO run at revision + 1
  -> open new empty TODO run at revision 0 before first compile

initial child turn
  -> canonical admission FULL
  -> open exact child TODO run at revision 0 before first compile
  -> cancellation before FULL creates no TODO lifecycle

model calls todo with complete bounded snapshot
  -> pre-attempt validation
  -> canonical attempt acceptance
  -> exact invoke recheck
  -> prepare replacement + canonical small acknowledgement
  -> canonical ToolResult exact settlement FULL
  -> settlement proves INSTALLED
  -> exact run-scoped process-local owner has replaced atomically
  -> one TodoSnapshotUpdated live event
  -> if run is already CLOSING, CLOSED follows at installed revision + 1
  -> initial attach/gap: live baseline first, then owner snapshot

mid-run compaction
  -> summary model handles semantic history only
  -> Runtime reads exact current TODO owner
  -> pending/in_progress enter COMPACTION_RUNTIME_HANDOFF
  -> completed text omitted
  -> successor continues current small task

never
  -> Plan Mode
  -> Host-global ROOT/child list
  -> one-call-per-item action protocol
  -> full-list ToolResult repetition
  -> durable task board
  -> event replay as current-state authority
  -> cross-Host TODO recovery
~~~

该设计保留TODO对小型agentic任务最有价值的部分：轻量、清晰、低动作成本、可在compaction后继续；同时拒绝把它扩张为第二套Plan、Job或Subagent authority。
