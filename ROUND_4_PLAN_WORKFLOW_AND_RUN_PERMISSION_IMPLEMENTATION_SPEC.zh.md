# Round 4：Plan Workflow 与 Run-bound Permission 实施规格

_状态：ACTIVATED（2026-08-12）；PHC-09 已在 canonical conversation Kernel 上恢复，验证证据见 [`round4_plan_workflow_and_run_permission_activation.json`](benchmarks/suites/core/v1/round4_plan_workflow_and_run_permission_activation.json)。_

## 0. 基线、目标与最终结论

### 0.1 两个代码基线

Round 4 必须同时对照两个 Git tree，且二者用途不同：

| 基线 | Commit | 用途 |
| --- | --- | --- |
| hard-cut 前产品真值 | `5b7ad9f7ffc8565bc572180b2bde0c81ab64473a` | 找回已经进入 production 的 Plan 生命周期、三项 workflow tool、结构化 question/draft review、run-bound permission 与交互预算 |
| 当前减法 Kernel | `d64dbeb23c6af0a00e112349a50878eae4abd9f6` | Round 3 激活后的唯一当前代码真值；canonical row、attempt-before-effect、Protocol v3、typed compiler 与 process-local live plane均以此为准 |

起草时前置材料 SHA-256 如下：

```text
PULSARA_DURABILITY_SUBTRACTION_REASSESSMENT.zh.md
cb3e7b0a9f33e5e4c5b17850d47e1af580a3f23f094f868076351bb17a6a6e80

POST_HARD_CUT_PRODUCT_CAPABILITY_GAP_INDEX.zh.md
95c339b937170964da9ec56adbbfb618e87e0486debb7d82176d6e4ba2211c72

ROUND_1_TOOL_OUTPUT_ARTIFACT_IMPLEMENTATION_SPEC.zh.md
7b34caa305f5a5f9f5f9fda1dd1d1254bbd8d33c6116ca86f8c5bb22cbe4374b

ROUND_2_TERMINAL_RUNTIME_IMPLEMENTATION_SPEC.zh.md
0de90b7b926fa53080729b4462946a4715429e9c3530ed49524c5b5f3f4532c4

ROUND_3_STRUCTURED_MODEL_INPUT_COMPILER_IMPLEMENTATION_SPEC.zh.md
1a996f8dda8c767043e4c84bf7d414724129dbd3d890d5cf3bb5463922cae6e6

STAGE_5_CLEAN_BASELINE_RUNBOOK.zh.md
d58e1c585c0f718a516ab4b292061393c6d71f2e1fb2475c311ce11ac5ea82e5
```

这些 hash 只标识起草输入。实施者必须在第一个 production diff 前重新记录实际 checkpoint HEAD、规格 hash 与 Gap Index hash；不得覆盖用户在文档或代码中的后续修改。

### 0.2 Round 4 的产品结论

Round 4 同时恢复两个不可分割、但 authority 不同的产品边界：

1. 用户在发送前选择本次 run 的 permission preset；message/command 被接受时冻结不可变 permission snapshot，后续 UI 调节只影响未来 run。
2. Plan 是 canonical workflow。它在 active 期间对新 run 安装 Runtime 强制的 read-only overlay，并拥有 question、draft review、approve/revise/cancel/force-exit 生命周期。

最终语义冻结为：

```text
composer-selected requested permission          # 发送前可变，不是authority
        + prompt / new-turn command
        |
        v
canonical admission
        +-- reads current canonical Plan workflow
        +-- applies PLAN_READ_ONLY overlay when active
        +-- freezes one FrozenRunPermissionSnapshot
        +-- writes snapshot with queue item or turn
        |
        v
all model calls and tool authorization in that turn
        use the same immutable snapshot
```

Plan 的 state 由 canonical rows 直接拥有；committed events 只记录 accepted occurrence。重开会话读取 Plan row 与 turn snapshot，不通过 event replay 恢复 Plan、permission、等待中的 coroutine 或 provider execution。

产品状态机冻结为：

```text
ordinary ROOT turn
  Agent enter_plan
    -> atomically COMPLETE origin
    -> ACTIVE workflow
    -> new read-only PLAN_CONTINUATION turn

Plan turn
  ask_plan_question
    -> same-turn QUESTION wait
    -> option / UI-owned Other free text
    -> canonical answer ToolResult
    -> resume same turn

  exit_plan(draft)
    -> canonical submission ToolResult
    -> COMPLETE origin turn
    -> OPEN asynchronous DRAFT_REVIEW
       APPROVE -> exact accepted plan + terminal workflow
                  + automatic implementation continuation using restored preset
       REVISE  -> ACTIVE workflow
                  + automatic read-only continuation with optional feedback
       CANCEL  -> terminal workflow, no automatic turn
                  + one-shot cancellation handoff on next real human prompt
```

这些automatic continuation都是新的canonical turn，不是同一provider call的隐藏resume；Runtime只让TUI体验无缝。它们使用typed `PLAN_CONTINUATION` origin，不能伪装成human message或触发skill activation。

### 0.3 最终物理拓扑

```text
Go TUI / typed Host API
  - composer permission selector
  - enter/cancel/force-exit Plan
  - resolve canonical Plan interaction
              |
              v
KernelHostSession
  - serializes command/admission
  - owns same-Host question continuation coordinator
  - is the only automatic Plan-turn scheduler
  - owns process-local ContinuationAdmissionAttempt / ROOT slot / force-exit fence
  - never stores durable Plan truth
              |
              v
ConversationKernelRepository
  - plan_workflows                         # current workflow truth
  - plan_interactions                      # question与异步draft review truth
  - turns / prompt_queue_items             # immutable permission snapshot
  - transcript/tool rows                   # exact Plan request/result/continuation
  - selective agent_events                 # accepted occurrence only
              |
              +-----------------------------+
              |                             |
              v                             v
Structured Model-Input Compiler      Typed permission policy port
  - RUN_PERMISSION source             - exact snapshot join
  - PLAN_HANDOFF source               - authorize -> attempt -> effect
  - PLAN_WORKFLOW source              - no mutable Host policy
  - no DB/event authority

CanonicalProviderInputReader
  - one repeatable-read compile snapshot
  - canonical items + permission/Plan/handoff/approved-plan facts
  - closes transaction before pure collector/compiler work
```

### 0.4 Round 4 完成后的 closed oracle

本轮明确允许有审查地扩展 canonical relation、subject 与 committed vocabulary。激活后的目标数字为：

| Oracle | Round 3 当前 | Round 4 目标 | 增量 |
| --- | ---: | ---: | ---: |
| product relations | 24 | **26** | `plan_workflows`、`plan_interactions` |
| Committed events | 27 | **34** | 七类 Plan domain occurrence |
| Live events | 23 | **23** | 复用现有 Interaction Opened/Replaced/Closed |
| typed subject slots | 13 | **15** | Plan workflow、Plan interaction |
| append guards | 2 | **2** | 不新增 writer domain |
| durable jobs | 4 | **4** | Plan 不升级为 job |

`34 / 23 / 15 / 2 / 26 / 4` 是 Round 4 activation oracle，不是永久产品上限。真正冻结的是每个新增类型的 subject、owner、guard、transaction、projection 与 sensitivity 均闭合。

### 0.5 实施切片

```text
R4-0  inventory、上位口径与negative guards
R4-A  run permission snapshot及所有new-turn admission
R4-B  Plan canonical rows、events与repository candidates
R4-C  workflow control barrier、question coordinator与automatic continuation
R4-D  RUN_PERMISSION / PLAN_HANDOFF / PLAN_WORKFLOW compiler sources
R4-E  Protocol v3与Go最小交互闭环
R4-F  reset-only activation、全量验证与真实provider dogfood
```

每个切片必须保持 production composition 可收集、可启动；不得以 dormant legacy branch、双状态 owner 或 event replay 临时过渡。

## 1. 必须保持的上位架构约束

1. canonical relational row 是 Plan 与 run permission 的 current semantic truth。
2. committed Plan event 只表示“某项 Plan transition 在 sequence N 被接受”，不证明 row 已存在，不恢复 workflow。
3. canonical Plan row、相关 transcript/tool row 与 committed occurrence由 `HostWriterGuard` owner在同一 PostgreSQL transaction写入。
4. 普通 hook、TUI、provider、Plan coordinator 与 tool executor都不能 append committed event。
5. Plan 不新增第三种 append guard、durable job、receipt、checkpoint、repair generation、projection worker或reducer。
6. completed assistant tool-request message仍须完整原子提交后，Plan control branch才可处理其中的 call。
7. Plan control call不产生 physical tool attempt；任何 sibling ordinary tool call也不得在同一 batch 中 physical dispatch。
8. ordinary physical tool仍遵守 authorization、attempt commit、invoke、result commit顺序；permission snapshot只改变typed policy输入，不改变 effect journal边界。
9. permission snapshot在run admission后不可修改；Plan exit、UI selector变化、detach、hook或后续command都不能放宽正在运行的turn。
10. provider input仍由 exact canonical cut 编译；Plan guidance与permission只是新的typed source，不是自由拼接的system string。
11. QUESTION interaction可以在同一Host内等待与重连，但Host退出后不恢复旧coroutine；takeover按canonical规则中断origin turn并终结question。DRAFT_REVIEW在提交transaction内已经关闭origin turn，因此它是可跨detach/Host replacement读取和决策的canonical异步交互，不是被恢复的coroutine。
12. committed/live observer、Go projection或hook失败不得否定canonical Plan commit，也不得让已冻结的permission失效。
13. raw question、answer、draft与feedback默认不进入committed event payload、ordinary hook或operational log。
14. Plan prompt不能授权physical effect。真正的read-only强制只来自typed permission policy port。
15. Runtime自动续转必须创建typed canonical continuation entry与新turn；它不得伪造human `USER_MESSAGE`、不得触发skill/capability activation，也不得依赖event replay。
16. canonical transcript、permission、Plan workflow/handoff与approved-plan presence proof必须来自同一个repeatable-read compile snapshot；collector/compiler不得二次查询数据库。
17. QUESTION human wait不消耗provider/DB/tool physical-operation deadline；每次恢复后的physical cycle与terminalization各自使用新的bounded deadline。
18. automatic continuation的write/confirmation、slot settlement与task bind由Host-owned process-local attempt完整拥有；gateway/origin request cancellation只能detach waiter，不能在FULL后留下taskless RUNNING turn。
19. `jsonb` structured value不是provider原始JSON字节。Plan exact read与approved-plan materialization只使用binding-aware central extractor定义的typed question identity和derived Plan UTF-8 body identity。
20. command winner只携带commit-time stable facts；handoff当前是否pending/claimed/superseded只属于canonical control read projection。

## 2. 当前代码真值

### 2.1 Permission preset存在，但仍是Host级固定注入

当前 [`primitives/permission.py`](src/pulsara_agent/primitives/permission.py) 已封闭四种产品preset：

```text
read-only
ask-permissions
accept-edits
bypass-permissions
```

[`tool_permission.py`](src/pulsara_agent/tool_permission.py) 能把preset展开为 `EffectivePermissionPolicy`，[`conversation_kernel/tool_policy.py`](src/pulsara_agent/conversation_kernel/tool_policy.py) 也保留typed allow/deny/confirmation决策。

但当前 [`conversation_kernel/host.py`](src/pulsara_agent/conversation_kernel/host.py) 在Host构造时把固定 policy 安装进 `DirectKernelToolPort`。`turns`、`prompt_queue_items`与Protocol command都不携带per-run snapshot。因此当前代码不能证明：

- 用户发送时选择的mode是什么；
- queue delay或ACK-unknown后是否仍使用同一mode；
- active Plan是否在admission线性化点强制收窄；
- authorization、attempt与invoke是否都读取了同一run contract。

残留的mutable `PermissionState`不是新Kernel的合法run authority，本轮不得重新把它提升为session全局真源。

### 2.2 Plan descriptor存在，但production executor不存在

[`capability/builtin_catalog.py`](src/pulsara_agent/capability/builtin_catalog.py) 仍声明：

```text
enter_plan
ask_plan_question
exit_plan
```

它们具有 `plan_workflow` permission category，但 [`conversation_kernel/tool_runtime.py`](src/pulsara_agent/conversation_kernel/tool_runtime.py) 没有对应executor或runtime-control branch。模型即使看到descriptor，也无法到达正式Plan owner。

### 2.3 当前runner的effect顺序正确，但没有workflow barrier

[`conversation_kernel/runner.py`](src/pulsara_agent/conversation_kernel/runner.py) 已正确做到：

```text
provider complete
-> atomic assistant message/tool-request commit
-> per-call authorize
-> attempt commit
-> invoke
-> result commit
```

当前实现随后按provider顺序逐个处理call。若直接把Plan当普通tool接线，排在`enter_plan`之前的sibling call可能已经physical dispatch；若给Plan伪造attempt，又会错误地把Runtime control记为external effect。本轮必须在assistant tool-request commit之后、普通per-call loop之前增加closed Plan workflow barrier。

### 2.4 当前schema没有Plan state或run permission snapshot

clean-v0 baseline当前有24张product relations。与本轮最相关的事实是：

- `turns`没有permission字段；
- `prompt_queue_items`没有permission字段；
- `session_commands`没有Plan command/target；
- `interaction_decisions`中的`PLAN`只是历史名义subject，不拥有open Plan interaction；
- `tool_results`只区分有attempt与policy no-attempt，无法表达成功的Runtime control result；
- `agent_events`只有13个typed FK subject slot和27类committed event。

Plan不能塞进`interaction_decisions`、自由JSON metadata或transcript parser。它需要独立canonical current state。

### 2.5 Protocol v3只有普通tool confirmation

当前 [`terminal_kernel_v3.proto`](src/pulsara_agent/terminal_protocol/schema/terminal_kernel_v3.proto) 支持submit/steer/stop/detach/close及job/subagent result acceptance。`ResolveInteractionRequest`绑定live owner epoch/revision并只表达allow/deny，适合process-local ordinary tool confirmation，不适合canonical Plan question/draft review。

`CanonicalControl`也没有active Plan、open Plan interaction或active/queued run permission投影。

### 2.6 Round 3 compiler提供了正确扩展点

[`model_input/contracts.py`](src/pulsara_agent/model_input/contracts.py)、[`model_input/compiler.py`](src/pulsara_agent/model_input/compiler.py) 与 [`conversation_kernel/context_sources.py`](src/pulsara_agent/conversation_kernel/context_sources.py) 已建立provider-neutral immutable source protocol。目前closed source是五类：

```text
BASE_SYSTEM
RUNTIME_ENVIRONMENT
RUNTIME_CLOCK
CAPABILITY_CATALOG
ACTIVE_SKILL
```

Round 4应增加`RUN_PERMISSION`、`PLAN_HANDOFF`与`PLAN_WORKFLOW`，而不是在runner/provider adapter中重新拼接Plan prompt。

当前 [`conversation_kernel/reader.py`](src/pulsara_agent/conversation_kernel/reader.py) 的repeatable-read transaction在返回`CanonicalModelInputSnapshot`时已经关闭，runner随后才独立调用source collector。Round 4不能让collector为Plan source自行查库；reader必须在同一transaction一并冻结canonical items、run permission与Plan/handoff facts，再把process-local composite carrier交给collector/compiler。

### 2.7 所有new-turn创建路径都必须纳入snapshot

当前ROOT turn不只来自直接用户prompt，还可能来自：

- prompt queue消费；
- Terminal monitor autonomous observation；
- accepted subagent result；
- accepted durable job result。

此外还有`SUBAGENT_TASK` scoped turn。只修改普通`submit_prompt()`会留下权限旁路；R4-A必须逐一列出并覆盖每个创建者。

## 3. hard-cut前产品真值与禁止移植面

### 3.1 必读旧代码

以下旧文件大多已物理删除，使用只读 `git show` / `git grep` 检查：

```bash
PRE_HARD_CUT=5b7ad9f7ffc8565bc572180b2bde0c81ab64473a

git show "$PRE_HARD_CUT:src/pulsara_agent/tools/builtins/plan.py"
git show "$PRE_HARD_CUT:src/pulsara_agent/runtime/plan.py"
git show "$PRE_HARD_CUT:src/pulsara_agent/runtime/permission_snapshot.py"
git show "$PRE_HARD_CUT:src/pulsara_agent/runtime/agent.py"
git show "$PRE_HARD_CUT:src/pulsara_agent/host/session.py"
git show "$PRE_HARD_CUT:archived_docs/PULSARA_RUN_BOUND_PERMISSION_MODE_PLAN.zh.md"
git show "$PRE_HARD_CUT:archived_docs/PLAN_WORKFLOW_EVENT_ARCHITECTURE.zh.md"
```

重点历史tests：

```text
5b7ad9f7:tests/test_host_core.py
5b7ad9f7:tests/test_agent_runtime_loop.py
5b7ad9f7:tests/test_plan_workflow.py
5b7ad9f7:tests/test_tools.py
5b7ad9f7:tests/test_subagent_runtime.py
```

### 3.2 必须恢复并冻结的产品语义

- `enter_plan`、`ask_plan_question`、`exit_plan`是Runtime control tool，不是ordinary physical tool。
- 用户可在idle时直接进入Plan；Agent也可在当前ROOT run内主动进入。
- Agent进入Plan后，当前run在写入control result后结束；Runtime在同一canonical transaction创建一个新的Plan continuation turn。read-only从该新turn开始，不追溯改写origin snapshot，TUI无需用户再次发送消息。
- active Plan中的新run始终read-only，即使用户composer选择更宽的mode。
- question回答回到同一Host内等待的原tool call，随后继续同一个read-only run；它不创建新turn。
- question UI展示模型给出的有限选项，并在`allow_free_text=true`时增加一个UI-only“其他（以上选项都不合适）”入口。用户提交前的选中态/草稿只在Go进程内；提交成功后，exact answer成为canonical Plan control result，从而在detach、ACK unknown与后续provider context中保持一致。
- `exit_plan`提交draft后立即关闭origin Plan turn。draft review成为canonical异步decision，不让provider coroutine跨用户审阅继续悬挂。
- draft `APPROVE`：持久化exact approved draft，退出Plan、恢复进入Plan前的permission preset，并由Runtime自动创建新的implementation continuation turn。
- draft `REVISE`：保持Plan/read-only；non-empty optional feedback由Runtime装入新的Plan continuation turn，missing与present-empty统一normalize为`NO_FEEDBACK`。无反馈同样合法，Agent仍可复用`ask_plan_question`澄清，不由Runtime判断文本是否“相关”。
- draft `CANCEL`：退出Plan并恢复composer的permission preset，但不自动调用provider；下一条真实human prompt获得一次性typed cancellation handoff。
- force-exit可以结束Plan，但不能让正在运行的Plan turn中途获得写权限。
- Plan interaction与revision有独立有限预算。
- workflow tool形成batch control barrier，sibling call不physical dispatch。

### 3.3 明确禁止恢复的旧machinery

- `reduce_plan_workflow_state(events)`或任何Plan event reducer；
- `RunStartEvent`作为permission truth；
- EventLog replay恢复pending question/draft或execution coroutine；
- pending interaction receipt、resume generation、reconciliation latch；
- mutable Host/session permission holder作为active run authority；
- Plan-specific checkpoint、projection worker、delivery ACK或execution continuation receipt；
- mutable session-wide permission authority。workflow只冻结一个`resume_permission_mode`值，用于后续automatic continuation和Go selector恢复；每个run仍以自己的immutable snapshot为唯一policy truth。

### 3.4 本规格对旧语义的明确取舍

旧文档和旧实现对cancel后的细节有过漂移。本规格冻结实际可实施且更克制的closed contract：

- draft `APPROVE`：workflow终态`APPROVED`，接受该interaction指向的exact draft，并原子创建implementation continuation turn；
- draft `REVISE`：workflow仍`ACTIVE`，feedback写入新的typed continuation entry并原子创建Plan continuation turn；
- draft `CANCEL`：workflow终态`CANCELLED`，不接受draft且不自动创建turn；下一条human prompt在admission时原子claim一次性cancellation handoff；
- user `CANCEL_PLAN`：仅在idle且没有open Plan interaction时允许；
- `FORCE_EXIT_PLAN`：可以终止open interaction/current Plan turn，workflow终态`FORCE_EXITED`。

Runtime continuation在provider wire上可以被adapter编码为带固定边界的user-role message，但canonical/internal kind必须是`PLAN_CONTINUATION`/`RUNTIME_PLAN_CONTINUATION`。它不是用户消息，不能改变skill、capability catalog或permission preset。

## 4. 本轮范围与非目标

### 4.1 必须完成

- 每个turn的immutable requested/effective permission snapshot；
- queue item在submission时冻结同一snapshot；
- Plan-scoped read-only overlay；
- 两张canonical Plan relation与closed DB invariants；
- 七类committed Plan occurrence、两个typed subject slot；
- 三项Plan tool的production closure与batch barrier；
- user enter/cancel/force-exit与question/draft resolution；
- question的same-Host suspension与detach/reattach；draft review的canonical异步决策；
- writer takeover/Host close的canonical abort语义；
- `RUN_PERMISSION`、`PLAN_HANDOFF`与`PLAN_WORKFLOW` compiler source；
- Protocol v3与Go最小Plan/permission交互；
- reset-only clean-v0、deep verifier、fixtures、tests与real-provider dogfood。

### 4.2 明确不做

- Plan模式内workspace write例外、临时write lease或“只写plan文件”；
- per-tool自定义policy editor或durable session default permission row；
- cross-Host恢复等待中的Plan coroutine/provider call；
- 把automatic Plan continuation伪装成human prompt，或为其建立durable runner/coroutine recovery；
- Plan event replay、reducer、receipt、checkpoint或repair graph；
- durable Plan job或第五类job handler；
- standalone Canonical Inspector或Legacy Python REPL兼容命令；
- advanced Go plan editor、历史diff、side-by-side review；
- 多个并行active Plan workflow、Plan DAG或跨session Plan；
- MCP、compaction、failure note、timing/freshness或memory重设计；
- exact compiled input audit或provider request replay。

## 5. Run permission canonical contract

### 5.1 Preset contract

新增closed常量：

```text
PERMISSION_PRESET_CONTRACT_ID = pulsara.permission-presets.v1
PERMISSION_PRESET_CONTRACT_FINGERPRINT = sha256:<canonical closed table>
```

fingerprint覆盖四种mode及其完整preset expansion，使用现有canonical JSON/framing helper。production snapshot只保存mode与contract fingerprint，不复制一份可自由漂移的policy JSON。已知fingerprint才能展开为`EffectivePermissionPolicy`；未知fingerprint fail closed。

custom `EffectivePermissionPolicy`仍可用于低层组件测试，但不能进入Host command、queue、turn、Protocol、provider compile或tool attempt。

### 5.2 Immutable DTO

在runtime-neutral primitives中冻结：

```text
RunPermissionAdmissionSource =
    USER_SUBMISSION
  | EXTERNAL_RESULT_COMMAND
  | TERMINAL_OBSERVATION
  | SUBAGENT_INHERITANCE
  | RUNTIME_PLAN_CONTINUATION

RunPermissionOverlay =
    NONE
  | PLAN_READ_ONLY

FrozenRunPermissionSnapshot
  snapshot_id
  requested_mode
  effective_mode
  admission_source
  overlay
  plan_context_ordinal_at_admission   # always present; 0 means no workflow has ever entered
  plan_workflow_id?             # iff PLAN_READ_ONLY
  plan_workflow_revision_at_admission?  # iff PLAN_READ_ONLY；是audit cut，不是当前revision lease
  inherited_from_turn_id?       # iff source derives from an earlier turn
  permission_contract_id
  permission_contract_fingerprint
  snapshot_fingerprint
```

`snapshot_fingerprint`使用唯一domain separator `pulsara:run-permission-snapshot:v1`并覆盖上述全部字段（排除自身）。DTO为frozen、无dict、无callback、无policy object、无transport capability。

### 5.3 Requested、effective与scope的唯一规则

```text
ROOT + no active Plan:
    effective_mode = requested_mode
    overlay = NONE
    plan_context_ordinal_at_admission = latest workflow ordinal (or 0)
    plan_workflow_id / revision = NULL

ROOT + active Plan:
    effective_mode = read-only
    overlay = PLAN_READ_ONLY
    plan_context_ordinal_at_admission = active workflow ordinal
    plan_workflow_id = exact current canonical workflow
    revision_at_admission = transaction读取到的current revision

SUBAGENT_TASK:
    requested_mode = parent.effective_mode
    effective_mode = parent.effective_mode
    admission_source = SUBAGENT_INHERITANCE
    overlay = NONE
    plan_workflow_id / revision = NULL
    plan_context_ordinal_at_admission = parent.plan_context_ordinal_at_admission
    inherited_from_turn_id = exact parent turn
```

Plan overlay只适用于ROOT并且只能收窄。即使requested本来就是`read-only`，active ROOT Plan仍记录`PLAN_READ_ONLY`，从而证明该run属于Plan workflow，而不是普通read-only聊天。child通过parent effective mode继承read-only结果，但不成为Plan workflow成员、不获得Plan source或Plan tool；central factory、SQL trigger与compiler必须使用同一closed scope matrix。

每个workflow在entry transaction冻结`resume_permission_mode`：

```text
Agent enter_plan
  -> origin turn.requested_permission_mode

User ENTER_PLAN
  -> command携带的composer-selected requested_permission_mode
```

它只是后续turn admission的输入，不是可变session policy。Plan continuation turn使用`requested_mode = resume_permission_mode`、`effective_mode = read-only`；APPROVE后的implementation continuation使用`requested_mode = effective_mode = resume_permission_mode`。CANCEL/FORCE_EXIT不创建自动turn，只把Go composer selector恢复为该值；用户可在发送下一条真实prompt前再次调整。

automatic continuation admission必须仍认识workflow冻结的permission contract。若Host replacement后的binary不认识该fingerprint，APPROVE/REVISE/Agent-enter candidate在写入任何transition前返回typed `PERMISSION_CONTRACT_UNAVAILABLE`；不得先终结workflow再留下无法构造的turn。CANCEL/FORCE_EXIT仍可用来安全退出，下一条human prompt采用当前binary支持的显式selector。

### 5.4 Snapshot线性化与carrier

- 直接new-turn：在user message/turn接受transaction内冻结并写入`turns`。
- queued prompt：在queue acceptance transaction冻结并写入`prompt_queue_items`；消费时exact copy到`turns`，不重新读取composer或Host default。
- steer：不接受permission字段，使用target turn既有snapshot。
- external job/subagent result创建新turn：command必须携带requested mode；target现有turn时不接受mode。
- autonomous Terminal observation创建新turn：requested mode继承origin turn；在installation admission处应用当时current Plan overlay。
- Runtime Plan continuation：requested mode取workflow冻结的`resume_permission_mode`；ENTERED/REVISE仍应用`PLAN_READ_ONLY`，APPROVED使用`NONE`。它必须绑定exact workflow/interaction handoff，不允许caller另传mode。
- SUBAGENT_TASK turn：严格使用5.3 matrix；它继承parent context ordinal cut而不是重新读取session current Plan，不继承ROOT-only overlay/workflow refs，Plan tool不进入child surface。本轮不新增可自行扩权的child profile。

每个new-turn repository method必须显式接收`FrozenRunPermissionSnapshot`或能在同一transaction内由sealed candidate构造它。禁止从Host mutable field隐式读取。

Python headless API应把`requested_permission_mode`提升为`run_turn`、queue与external-new-turn方法的显式参数。`open_session`只可接受一个preset-only launch default，供未显式传值的直接Python调用在candidate第一次冻结前解析一次；Protocol/TUI submission必须始终显式发送mode。launch default不可在session内修改，也不是canonical current-run authority。

### 5.5 Queue与Plan状态漂移

queue item保存冻结时的`plan_workflow_id`与`revision_at_admission`。workflow revision会因同一Plan内的question/revise正常推进，因此它只是snapshot audit cut，不是消费lease。消费全局FIFO head时：

```text
snapshot overlay NONE + current Plan ACTIVE
    -> REJECTED / PLAN_CONTEXT_CHANGED_BEFORE_DELIVERY

latest workflow ordinal != snapshot.plan_context_ordinal_at_admission
    -> REJECTED / PLAN_CONTEXT_CHANGED_BEFORE_DELIVERY

snapshot overlay PLAN_READ_ONLY
    + current active workflow id不同或已非ACTIVE
    -> REJECTED / PLAN_CONTEXT_CHANGED_BEFORE_DELIVERY

same workflow仍ACTIVE
    -> copy snapshot into new turn and consume
```

`plan_context_ordinal_at_admission`防止一个pre-Plan queue item在workflow已经进入又退出后“重新变得兼容”。consumer逐个处理head并复用现有bounded invalid-head rejection，不在Plan enter/exit transaction中无界扫描queue，也不把旧prompt转投到不同Plan或普通run。

### 5.6 Policy port与physical dispatch join

`ToolDispatchAuthorizationRequest`必须携带完整frozen snapshot或其不可伪造的process-local borrow。授权路径冻结为：

```text
turn snapshot exact read
-> known permission contract expansion
-> typed policy decision
-> before attempt commit: exact join turn.snapshot_fingerprint
-> before invoke: exact join attempt + surface borrow + snapshot fingerprint
```

`DirectKernelToolPort`不得再持有一份可变或固定Host policy作为run truth。owner close、surface revoke或snapshot mismatch均在physical effect前形成typed unavailable/conflict；不得降级为allow。

ordinary confirmation恢复后也必须使用原turn snapshot。用户对单次tool allow只解决该call，不修改permission snapshot或未来run preset。

## 6. Canonical Plan schema

### 6.1 `plan_workflows`

新增第25张product relation：

| 列族 | 约束 |
| --- | --- |
| identity | `id`, `session_id`, `workspace_id`；same-session/workspace FK |
| ordering | `workflow_ordinal >= 1`、`UNIQUE(session_id, workflow_ordinal)`；在session writer lock下按max+1分配 |
| lifecycle | `status = ACTIVE | APPROVED | CANCELLED | FORCE_EXITED` |
| entry | `entered_by = USER | AGENT`, `entry_reason` UTF-8最多4 KiB |
| user origin | `entry_command_id` |
| agent origin | exact `entry_turn_id + entry_assistant_entry_id + entry_tool_call_id` |
| permission resume | `resume_permission_mode`、exact permission contract id/fingerprint；entry transaction一次冻结，之后不可修改 |
| revision | `workflow_revision >= 1`，每个accepted Plan state transaction恰好加1 |
| accepted plan | `accepted_plan_interaction_id`仅`APPROVED`时必填 |
| time | `accepted_at`, `terminal_at`；ACTIVE时terminal为空，终态时必填 |

closed source union：

```text
entered_by USER
  -> entry_command_id required
  -> agent origin全部NULL

entered_by AGENT
  -> entry_command_id NULL
  -> agent origin全部required且指向ROOT TOOL_CALL block
```

数据库建立session级partial unique index，保证至多一个`ACTIVE` workflow。终态不可回到ACTIVE；新一轮Plan创建新workflow id。

workflow与interaction rows按session lifetime完整保留；本轮不定义自动裁剪、归档或retention job。对应committed occurrence沿用session-lifetime retention。

### 6.2 `plan_interactions`

新增第26张product relation：

| 列族 | 约束 |
| --- | --- |
| identity | `id`, `session_id`, `workspace_id`, `plan_workflow_id` |
| ordering | `interaction_ordinal >= 1`，`UNIQUE(workflow_id, interaction_ordinal)` |
| kind | `QUESTION | DRAFT_REVIEW` |
| status | question：`OPEN | ANSWERED | ABORTED`；draft：`OPEN | APPROVED | REVISION_REQUESTED | CANCELLED | ABORTED` |
| exact origin | `origin_turn_id`, `assistant_entry_id`, `tool_call_id`，必须是same-session ROOT tool call |
| request binding | `request_contract_id`, `request_contract_version`, `request_contract_fingerprint` |
| request identity | `request_semantic_digest`覆盖exact canonical tool arguments与上述完整binding |
| control result | `control_tool_result_id`：question仅ANSWERED时存在；draft从OPEN起即指向submission result |
| decision identity | `resolution_command_id`, `response_semantic_digest`, `decision_continuation_entry_id` |
| question resolution | `answer_kind = OPTION | FREE_TEXT`、`selected_option_ordinal`（仅OPTION）；DB closed union与Protocol oneof一致 |
| draft resolution | status本身表达`APPROVED | REVISION_REQUESTED | CANCELLED`，另有`feedback_present NOT NULL`；REVISE missing/present-empty均为false，只有normalized non-empty为true；APPROVED/REVISION_REQUESTED必须指向continuation entry，CANCELLED不得指向 |
| time | `resolved_at`或`aborted_at`，与terminal status exactly一致 |

request与response正文都不复制进该表：

- question、options、allow_free_text来自referenced immutable `assistant_message_blocks.tool_arguments`；
- plan draft与summary也来自同一exact tool arguments；
- question answer与selected label只存在于referenced canonical Plan control tool result正文；
- `exit_plan`的Plan control result在draft open transaction即以固定`DRAFT_SUBMITTED_FOR_REVIEW` envelope关闭tool call；后续review decision不伪造第二个tool result；
- normalized non-empty REVISE feedback只存在于referenced immutable `PLAN_CONTINUATION` entry；REVISE missing/present-empty不创建feedback body，APPROVE/CANCEL不携带feedback；
- interaction row只拥有typed状态、option ordinal/feedback-present、control-result/continuation pointer与digest；
- `accepted_plan_interaction_id`由workflow指向被批准的DRAFT_REVIEW。

Plan interaction使用窄的closed historical contract registry，不建立通用schema migration service：

```text
PLAN_INTERACTION_CONTRACTS = {
  exact ask_plan_question versions still decodable,
  exact exit_plan versions still decodable,
}
```

Host replacement按row中的ID/version/fingerprint选择decoder，不能拿当前同名descriptor猜测历史payload。未知binding仍允许DRAFT `CANCEL`、workflow `CANCEL_PLAN/FORCE_EXIT_PLAN`与session close；QUESTION ANSWER及DRAFT APPROVE/REVISE在任何write前返回typed `PLAN_INTERACTION_CONTRACT_UNAVAILABLE`。registry是代码内closed decoder表，不是relation、job、receipt或动态plugin registry。

binding在interaction open时来自该turn冻结并实际advertise给模型的Plan tool surface；Plan control branch在写OPEN row前exact join tool call name与advertised binding。它不能从resolve时的current builtin catalog补填，也不能由客户端声明。

本轮clean-v0至少只需注册当前v1 binding；“historical”表示未来binary replacement必须保留仍可能OPEN的已发布binding decoder，并不要求Round 4预先发明多个版本或通用升级路径。

这样完整plan只有一个canonical正文authority，不新增plan artifact relation。`plan`最大1 MiB UTF-8，直接受descriptor/runtime/DB bound保护；本轮不增加第二套blob edge。

每个workflow最多一个`OPEN` interaction。request identity字段不可修改；只允许closed CAS从`OPEN`转到一个terminal status。QUESTION OPEN要求origin turn仍RUNNING；DRAFT_REVIEW OPEN要求origin turn已COMPLETED。

`control_tool_result_id`与`tool_results.control_plan_interaction_id`构成同一transaction内的exact双向join，使用deferred FK/constraint trigger支持合法插入顺序。QUESTION仅ANSWERED有result；DRAFT_REVIEW从OPEN起必须有submission result，后续decision不得替换它；ABORTED QUESTION没有result，ABORTED DRAFT保留既有submission result。

### 6.3 `transcript_entries`的Plan handoff carrier

在既有relation增加closed entry kind与typed source edge，不新增第27张表：

```text
EntryKind.PLAN_CONTINUATION

source_plan_workflow_id?
source_plan_interaction_id?
source_plan_handoff_kind? =
    ENTERED_PLAN
  | REVISION_REQUESTED
  | APPROVED_PLAN
  | CANCELLED_PLAN
  | FORCE_EXITED_PLAN
```

closed union：

- `PLAN_CONTINUATION`只允许`ENTERED_PLAN | REVISION_REQUESTED | APPROVED_PLAN`，workflow必填；REVISION/APPROVED还要求exact interaction；它是automatic turn的initial entry。
- ordinary `USER_MESSAGE`仅可额外绑定`CANCELLED_PLAN | FORCE_EXITED_PLAN`，表示该真实human prompt直接admission或从queue消费时取得了一次性handoff。
- 其他entry kind不得携带这些字段；同一个workflow/handoff kind最多由一个entry claim。
- same-session/workspace、workflow terminal/active status、interaction decision、queue source与handoff kind由deferred invariant trigger验证。

continuation entry正文是typed bounded envelope：ENTER只含workflow identity/transition；REVISE可含用户提交的optional feedback；APPROVE只含approval transition和exact approved-draft reference/digest。它不是human-authored message，不能被capability resolver当作skill activation subject。CANCEL/FORCE的下一条真实用户正文仍只属于`USER_MESSAGE`，handoff facts由compiler独立投影，绝不字符串拼接。

### 6.4 `turns`与`prompt_queue_items`

二者增加同构permission列：

```text
permission_snapshot_id                  NOT NULL
requested_permission_mode               NOT NULL closed enum
effective_permission_mode               NOT NULL closed enum
permission_admission_source              NOT NULL closed enum
permission_overlay                       NOT NULL NONE | PLAN_READ_ONLY
permission_plan_context_ordinal          NOT NULL nonnegative bigint
permission_plan_workflow_id               nullable typed FK
permission_plan_revision_at_admission     nullable positive bigint
permission_inherited_from_turn_id          nullable same-session FK
permission_contract_id                   NOT NULL exact v1
permission_contract_fingerprint          NOT NULL sha256
permission_snapshot_fingerprint          NOT NULL sha256
```

约束必须表达：Plan字段与overlay exactly一致、effective/read-only一致、PLAN_READ_ONLY时context ordinal与referenced workflow ordinal一致、ROOT/SUBAGENT_TASK matrix、inheritance与source一致。SUBAGENT_TASK必须以parent effective mode作为requested/effective、以parent context ordinal作为cut，并保持overlay/workflow refs为空。NONE snapshot的ordinal是历史admission cut，后续workflow不得使它反向非法。`turns`中的snapshot不可update；queue item消费只能把exact fields复制进新turn。

`prompt_queue_items`另可携带nullable closed handoff三元组`pending_plan_handoff_workflow_id / interaction_id / kind`。它在prompt acceptance时和message/mode candidate一起冻结；同一workflow/kind只允许最早一个accepted human prompt（direct entry或queue item）取得claim。queue消费时exact copy到USER_MESSAGE entry，queue row保留其ingress audit edge；两者必须通过existing queue→entry source关系exact join。若该item后来因Plan context变化被REJECTED，handoff不转绑另一prompt，避免ACK/retry改变原candidate；它也不构成跨prompt必达承诺。

### 6.5 Tool authorization audit与`tool_results` control provenance

`tool_execution_attempts`与ordinary `interaction_decisions`增加`permission_snapshot_fingerprint NOT NULL`。attempt/decision acceptance transaction必须从target turn exact join该fingerprint；返回给invoke/resume的typed result也携带它。policy denial或invalid result虽然没有attempt，result acceptance仍需exact join target turn snapshot。

当前`attempt_id IS NULL`只允许policy/validation类结果，无法合法表达成功的Plan control tool。新增：

```text
result_origin_kind =
    PHYSICAL_ATTEMPT
  | POLICY_NO_ATTEMPT
  | PLAN_CONTROL

control_plan_workflow_id?
control_plan_interaction_id?
```

数据库closed union：

- `PHYSICAL_ATTEMPT`：attempt必填，Plan refs为空；保留现有success/error/cancel状态。
- `POLICY_NO_ATTEMPT`：attempt与Plan refs为空；只允许invalid/denied/unavailable/cancelled-before-dispatch。
- `PLAN_CONTROL`：attempt为空，exactly one Plan workflow/interaction ref；只允许`SUCCESS | APPLICATION_ERROR`。invalid arguments在建立Plan subject前仍属于`POLICY_NO_ATTEMPT/INVALID_ARGUMENTS`；用户选择CANCEL是成功接受的workflow decision，不把tool result伪装成execution cancellation。

Plan control不伪造physical attempt，也不让成功result落入“未dispatch即失败”的旧union。

Plan control result使用固定UTF-8 text envelope，最大48 KiB并始终inline，artifact disposition为`NOT_REQUIRED`；它不经过Round 1 artifact publication，也不能递归产生artifact。

### 6.6 `session_commands`与旧interaction table

新增command kind/target matrix：

```text
ENTER_PLAN       -> PLAN_WORKFLOW
CANCEL_PLAN      -> PLAN_WORKFLOW
FORCE_EXIT_PLAN  -> PLAN_WORKFLOW
RESOLVE_PLAN_INTERACTION -> PLAN_INTERACTION
```

command candidate fingerprint覆盖requested permission、workflow id/revision、interaction id、decision与bounded response字段，但明确排除writer generation/owner/lease。ACK-unknown query必须在绑定当前guard前区分compatible winner与conflict。

`interaction_decisions.subject_kind`删除`PLAN`分支。ordinary tool confirmation继续使用该表和`InteractionDecisionAccepted`；Plan resolution只使用`plan_interactions`及本轮专用events，避免两个合法Plan decision authority。

### 6.7 DB invariants与trigger

clean baseline的deferred invariant trigger至少验证：

- Plan workflow agent-origin block是ROOT、属于entry/turn且tool name为`enter_plan`；
- Plan interaction origin block是ROOT，tool name与kind匹配；
- Plan interaction request binding匹配SQL closed allowed tuple；Python historical decoder registry与该tuple set由同一golden证明，semantic digest覆盖binding与exact arguments；
- approved workflow指向same-workflow DRAFT_REVIEW且status APPROVED；
- workflow ordinal在session内strictly increasing；queue consumption重验latest ordinal，历史snapshot本身保持immutable；
- ROOT/SUBAGENT_TASK permission matrix与parent inheritance exact join；active session Plan不得让child出现PLAN_READ_ONLY overlay或workflow ref；
- workflow resume permission使用entry时已知contract且终身immutable；
- Plan continuation/source edge与entry kind、workflow/interaction状态exact一致，一次性cancel/force handoff不可重复claim；
- `turns.initial_entry_id`允许typed `PLAN_CONTINUATION`且必须same-turn/scope ROOT；automatic continuation的revision-0只包含该initial entry；
- turn/queue Plan snapshot FK、positive admission revision与effective mode/overlay union一致；workflow后续revision推进不得反向使历史snapshot非法；
- Plan control tool result的call name与Plan ref kind一致；
- turn initial/final entry约束继续成立；
- event exactly-one subject union及event→subject mapping成立。

不得把上述正确性留给repository字符串约定。

### 6.8 Canonical Plan content extraction contract

`assistant_message_blocks.tool_arguments`的authority是PostgreSQL `jsonb`中的结构化值，不是provider发送时的原始JSON字节。JSON key顺序、空白、escape spelling与provider原始序列化均不承诺保留；Round 4不得把`tool_arguments::text`、任意语言的JSON re-serialization或`request_semantic_digest`冒充Plan正文的byte identity。

建立窄的central historical extractor，并由interaction row中的`request_contract_id/version/fingerprint`选择closed decoder：

```text
PlanQuestionOption
  ordinal
  label
  description
  recommended

PlanQuestionContent
  interaction_id
  request_contract_id / version / fingerprint
  question
  options: tuple[PlanQuestionOption, ...]
  allow_free_text
  typed_content_fingerprint

PlanDraftContentIdentity
  interaction_id
  assistant_entry_id / tool_call_id
  request_contract_id / version / fingerprint
  request_semantic_digest
  plan_utf8_size
  plan_utf8_digest

PlanDraftTextChunk
  identity: PlanDraftContentIdentity
  offset_utf8_bytes
  body
  next_offset_utf8_bytes
  eof
```

extractor先按historical binding验证完整typed arguments与8.1 bounds，再取出`plan`字符串并**直接编码一次UTF-8**；不做Unicode normalization、不加JSON引号、不展开escape、不拼接summary。Plan正文identity唯一公式冻结为：

```text
plan_utf8 = UTF8(decoded_plan_string)
plan_utf8_digest = "sha256:" + HEX_LOWER(
  SHA256(
    UTF8("pulsara:plan-draft-utf8:v1")
    || 0x00
    || U64_BE(LENGTH(plan_utf8))
    || plan_utf8
  )
)
```

`PlanQuestionContent.typed_content_fingerprint`使用同一套repository canonical framing，对binding、question、按ordinal排序的每个typed option及`allow_free_text`全覆盖；它是typed DTO identity，不宣称存在一段“原始question JSON bytes”。`request_semantic_digest`继续证明完整tool request，不能替代上述question/draft content identity。

chunk offset只相对`plan_utf8`正文，范围为`0..plan_utf8_size`；request offset必须位于UTF-8 code-point boundary，`4 <= limit_bytes <= 65,536`，server返回的body也在code-point boundary结束并使`next_offset`严格推进，EOF除外。每个response重复identity，client在展示前验证identity稳定、连续offset、最终size与digest。missing block、wrong binding、invalid shape、digest/size变化或非法offset均是单请求typed content error，不终止attachment。

同一个extractor必须被interaction open validation、one-cut approved-plan fact、Protocol exact read与ACK confirmation复用。至少提供Python/Go golden vectors覆盖Unicode、JSON key order/whitespace差异、escaped字符、空plan、最大plan、wrong binding与chunk boundary。不得新增Plan正文列、artifact relation、projection或第二个content owner。

## 7. Selective AgentEvent扩展

### 7.1 七类新增Committed event

| Event type | Subject | Producer | Guard | Same transaction row | Projection |
| --- | --- | --- | --- | --- | --- |
| `PlanWorkflowEntered` | PLAN_WORKFLOW | user command或Agent control branch | HostWriter | workflow ACTIVE | CurrentControl |
| `PlanQuestionAsked` | PLAN_INTERACTION | `ask_plan_question` open | HostWriter | QUESTION OPEN | CurrentControl |
| `PlanQuestionAnswered` | PLAN_INTERACTION | canonical resolution | HostWriter | QUESTION ANSWERED + result | CurrentControl |
| `PlanDraftSubmitted` | PLAN_INTERACTION | `exit_plan` open | HostWriter | DRAFT_REVIEW OPEN | CurrentControl |
| `PlanDraftDecisionAccepted` | PLAN_INTERACTION | approve/revise/cancel | HostWriter | interaction terminal + frozen decision/continuation pointer | CurrentControl |
| `PlanWorkflowExited` | PLAN_WORKFLOW | approve/cancel/user cancel/force exit | HostWriter | workflow terminal | CurrentControl |
| `PlanContinuationAccepted` | ENTRY | enter/revise/approve automatic continuation | HostWriter | typed `PLAN_CONTINUATION` entry + new turn | ImmutableEntry |

这些事件是独立的用户可观察transition，不是candidate、receipt或projection proof。它们不参与reopen decision；gateway在bounded read transaction中按subject读取typed/redacted current projection。

### 7.2 两个typed subject slot

```text
subject_plan_workflow_id
subject_plan_interaction_id
```

两者使用真实FK。`agent_events`仍要求exactly-one typed subject；所有Plan event与subject mapping由SQL CHECK和Python descriptor双重封闭。禁止自由字符串`subject_kind/id`。

### 7.3 Payload与敏感等级

event payload只允许有限audit字段：

| Event | 可持久化payload | 禁止payload |
| --- | --- | --- |
| Entered | `entered_by`, `reason_present` | reason全文、prompt |
| QuestionAsked | option count、free-text bool | question、labels、descriptions |
| QuestionAnswered | selected-option bool、answer-present bool | answer/label正文 |
| DraftSubmitted | draft bytes、summary-present、semantic digest | draft/summary正文 |
| DecisionAccepted | decision、feedback-present | feedback正文 |
| Exited | terminal disposition、accepted-draft bool | accepted plan正文 |
| ContinuationAccepted | handoff kind、feedback-present、approved-draft digest-present | feedback、plan正文、runtime callback |

统一`sensitivity_class=CONTROL_REDACTED`（或现有closed等价值），payload上限沿用64 KiB，实际Plan payload应远低于该上限。ordinary hook默认只能看到该redacted projection；读取exact question/draft需要session/workspace/controller capability。

### 7.4 Permission不新增event

不新增`PermissionSnapshotCreated`、`PermissionModeChanged`或per-call gate event：

- accepted run permission是turn/queue canonical字段；
- `UserMessageAccepted`、`PromptQueued/Consumed`已经提供对应occurrence；
- ordinary capability decision仍使用现有`CapabilityDecisionAccepted`；
- TUI从immutable entry/current control projection读取requested/effective snapshot。

### 7.5 Live vocabulary保持23

Plan interaction的实时提示复用：

```text
InteractionOpened
InteractionReplaced
InteractionClosed
```

Plan复用这些live类型时只携带interaction id/kind与固定的“question awaiting input”或“plan ready for review”提示，不携带raw question/options/draft。controller从canonical control发现interaction后，通过capability-scoped exact content read取得正文。canonical snapshot始终可发现open interaction，所以live GAP不会丢失产品状态。callback、coordinator、future、lease owner不进入metadata。

## 8. Plan tool surface与workflow barrier

### 8.1 Descriptor closed bounds

更新现有descriptor与runtime validator，使两者完全一致：

| Tool/field | Bound |
| --- | ---: |
| `enter_plan.reason` | 4 KiB UTF-8 |
| `ask_plan_question.question` | 16 KiB UTF-8 |
| options | 0或2–3；不得只有1项 |
| option label | 256 bytes UTF-8，非空且唯一 |
| option description | 2 KiB UTF-8 |
| recommended | 至多1项 |
| allow_free_text | required boolean；options=0时必须true |
| `exit_plan.plan` | 1 MiB UTF-8 |
| `exit_plan.summary` | 8 KiB UTF-8 |
| answer/feedback | 32 KiB UTF-8 |

Plan tools始终出现在ROOT model surface，运行时根据canonical state返回typed result；全部从SUBAGENT_TASK surface排除。工具清单不随Plan active动态抖动。

`allow_free_text=true`时，Go在模型给出的options之后渲染一个UI-owned“其他（以上选项都不合适）”入口。该入口不是第四个model option，不写回request arguments，也没有可被模型伪造的ordinal；选择后必须提交非空UTF-8 free text。提交前的selection与draft只存在于Go进程，canonical acceptance之后以Plan control result为唯一answer truth。

### 8.2 Batch control barrier

assistant tool-request原子commit后，runner先扫描完整ordered call list：

```text
no Plan workflow call
    -> existing ordinary per-call path

contains >=1 Plan workflow call
    -> select provider order中的第一个Plan call
    -> no call in this batch may physical dispatch
    -> selected call走typed Plan control port
    -> all sibling calls写CANCELLED_BEFORE_DISPATCH result
```

即使ordinary call排在Plan call之前也不得执行。sibling没有attempt，不能产生remote identity、artifact publication或memory side branch。每个sibling仍有canonical tool result与`ToolResultAccepted`，保持provider pairing。

invalid/unavailable Plan call仍拥有整个barrier；它写typed error后可进入下一次model call。不得因参数错误转而执行siblings。

### 8.3 `enter_plan`

#### Agent path

- 仅ROOT可达；
- inactive时在一个repository transaction创建ACTIVE workflow、Plan control result、sibling results、把origin turn置`COMPLETED`，并创建新的Plan turn/revision-0/`PLAN_CONTINUATION(ENTERED_PLAN)` initial entry；
- 同一transaction写`PlanWorkflowEntered`、`PlanContinuationAccepted`及相关result/turn occurrence；commit FULL后Host才调度新turn provider；
- 当前turn原permission snapshot保持不变；
- 新Plan turn使用workflow `resume_permission_mode`作为requested、`PLAN_READ_ONLY`作为overlay并强制effective read-only；TUI表现为无缝续转，但canonical上是两个turn；
- 已active时返回idempotent success，引用existing workflow，不重复`PlanWorkflowEntered`；该read-only run可以继续下一次model call。

#### User path

`ENTER_PLAN`只在没有active ROOT turn、没有open Plan interaction与没有进行中的new-turn reservation时接受。它创建workflow与command winner；如果用户尚未同时提交规划目标，则不凭空启动provider，下一条真实prompt按Plan overlay入场。并发prompt/Plan按session writer lock的实际commit顺序决定overlay。

### 8.4 `ask_plan_question`

前置条件：

- ROOT turn；
- turn snapshot `overlay=PLAN_READ_ONLY`；
- linked workflow id仍ACTIVE；repository另以本次prepared operation读取的current revision执行CAS；
- 没有open Plan interaction；
- budget未耗尽。

在open write之前，coordinator必须按stable interaction/candidate identity安装一个dormant `QuestionWaitAttempt`；它尚不发布live view，也不是canonical OPEN truth。随后transaction创建QUESTION interaction与`PlanQuestionAsked`并结算siblings；selected Plan call暂不写tool result。

open write的settlement：

```text
FULL / exact-compatible winner
  -> promote同一QuestionWaitAttempt
  -> row仍OPEN才发布best-effort live Opened
  -> runner await；若resolution已先到达则立即返回

NONE（决定不再重写时）
  -> release dormant attempt

CONFLICT
  -> abort attempt and fail closed
```

canonical row从commit起可被snapshot发现，因此resolver必须能在runner真正进入`await`之前，把terminal resolution存入同一process-local attempt并settle future。不得采用“commit OPEN后才创建future”的顺序。若row已经ANSWERED再promote，不合成迟到的historical live Opened/Closed。

resolution transaction：

- exact CAS `OPEN -> ANSWERED`；
- option answer必须匹配referenced request options；free text必须受`allow_free_text`约束；
- 写canonical Plan control tool result（bounded UTF-8 response envelope）、`PlanQuestionAnswered`与`ToolResultAccepted`；
- commit后才resolve future，runner继续同一个read-only turn。

answer是conversation truth，不能只保存在Go或Python future。Runtime只验证shape、option identity、bounds与CAS，不判断自由文本是否“回答得好”；Agent认为答案不足时可以再次调用`ask_plan_question`。该interaction不开新turn，也不推进workflow revision以外的permission snapshot。

### 8.5 `exit_plan`

前置条件与question相同。draft submission transaction创建DRAFT_REVIEW interaction、写固定`DRAFT_SUBMITTED_FOR_REVIEW` Plan control result、结算siblings并把origin turn置`COMPLETED`；同时写`PlanDraftSubmitted`、`ToolResultAccepted`与`TurnCompleted`。draft正文只在canonical tool arguments中。commit后runner结束，因此用户审阅期间没有等待中的provider coroutine或tool future。

closed resolution：

| Decision | Interaction | Workflow | Canonical continuation | Provider行为 |
| --- | --- | --- | --- | --- |
| APPROVE | APPROVED | APPROVED，指向该draft | 原子创建implementation turn + `APPROVED_PLAN` entry | commit FULL后自动启动 |
| REVISE | REVISION_REQUESTED | 保持ACTIVE | 原子创建read-only Plan turn + `REVISION_REQUESTED` entry | commit FULL后自动启动 |
| CANCEL | CANCELLED | CANCELLED，无accepted draft | 不创建turn；等待下一条human prompt claim `CANCELLED_PLAN` | 本次不调用 |

Go review UI的closed产品选项固定为：`批准此计划`、`修改/改进此计划`（optional natural-language feedback）、`取消此计划`。只有REVISE显示/提交feedback；APPROVE/CANCEL携带feedback必须被wire validator拒绝。UI可以调整本地草稿直到submit，canonical decision只接受一次，竞态loser返回existing winner而不是覆盖。

每次REVISE后的新`exit_plan`创建新interaction ordinal；旧draft immutable。APPROVE transaction必须同时冻结exact approved draft binding、恢复permission overlay、创建new turn/revision-0/continuation entry并写`PlanDraftDecisionAccepted + PlanWorkflowExited + PlanContinuationAccepted`。REVISE transaction写decision与new Plan turn/continuation；仅normalized non-empty feedback正文出现在continuation entry中。CANCEL transaction只写decision/workflow exit，Go selector恢复workflow preset。

APPROVE后的第一次model input必须**exactly once**包含approved plan，而不是只有摘要/digest，也不能同时在历史`exit_plan` arguments与continuation中复制两遍。reader在one-cut composite中给出exact block identity/digest与presence proof：canonical items已包含matching arguments时，pin该既有carrier并让continuation只携带approval/reference；只有adopted snapshot等合法cut确实缺失该block时，才在continuation中物化完整plan。如果目标预算无法容纳一次exact plan，在provider open前typed失败，不得静默截断、重复计量或退化为summary。批准只改变workflow与overlay，不能绕过恢复后的permission preset。

### 8.6 User cancel与force exit

`CANCEL_PLAN`：

- 仅workflow ACTIVE、无active ROOT turn、无OPEN Plan interaction时允许；
- status变`CANCELLED`并写`PlanWorkflowExited`；
- 不创建turn、tool result或provider call；下一条真实human prompt可原子claim一次性`CANCELLED_PLAN` handoff。

`FORCE_EXIT_PLAN`：

- 可针对OPEN interaction/current Plan turn；
- 若存在RUNNING turn/OPEN QUESTION，Host先停止新Plan resolution admission并取消active runner；runner既有`interrupt_turn` transaction必须同时将QUESTION置`ABORTED`，随后Host等待runner及其physical work完全join；
- 上述turn interruption FULL/exact-confirm后，Host执行第二个bounded command transaction把workflow置`FORCE_EXITED`并写`PlanWorkflowExited`；
- 若只有OPEN DRAFT_REVIEW（origin已COMPLETED）或idle ACTIVE workflow，单个bounded command transaction可将draft置ABORTED并把workflow置`FORCE_EXITED`，不创建/等待runner；
- pending Plan control call不伪造已被模型看到的result；future canonical reader按ABORTED row生成provider-only interruption closure。

force exit不能改变任何已接受turn的snapshot，也不能自动开始一个更高权限turn。

这里刻意不用“一次transaction同时等待physical join并退出Plan”：PostgreSQL lock不得跨process/provider join。若Host在两步之间崩溃，turn/interaction已安全终结而workflow继续ACTIVE；用户重试force-exit即可，绝不能在未确认physical exit时提前放宽后续run。

### 8.7 Interaction预算

```text
maximum_plan_interactions_per_turn = 16
maximum_plan_draft_revisions_per_workflow = 8
maximum_plan_interactions_per_workflow = 64
```

预算从canonical `plan_interactions`按origin turn/workflow/kind计数，不新增counter、event或receipt。QUESTION不使用共享foreground I/O deadline；它等待resolve、stop、force-exit或Host close。DRAFT_REVIEW在canonical row中保持OPEN直到decision或显式session close，不占用Python waiter。预算耗尽时selected Plan call得到Plan-specific error result，turn按typed interruption规则终结，workflow保持ACTIVE，用户可在下一run继续或退出。

当前runner从turn开始复用单一absolute deadline；Round 4必须把human wait从该deadline中拆出：

- question open DB write仍使用当前bounded physical-operation deadline；FULL后不持有DB connection/lock并结束该deadline的使用；
- human wait本身不启动provider/DB/tool timer；
- ANSWERED FULL后由runner签发一个新的bounded model-cycle deadline，供reader、compiler、provider及随后repository I/O使用；
- stop、force-exit、provider/tool failure的turn interruption始终使用独立fresh terminalization deadline，不能复用已经过期的cycle deadline；
- maximum model calls、tool calls、token budget及单次physical operation timeout保持原有上限，不因human wait重置累计预算。

## 9. Question continuation与draft review owner

### 9.1 Owner边界

新增process-local `KernelPlanInteractionCoordinator`（名称可等价，但职责不可扩张），它只拥有QUESTION：

- 持有当前Host中至多一个`QuestionWaitAttempt`与future；
- 将canonical interaction identity投影到live control；
- 在commit后向runner交付resolution；
- stop/close时协助取消本地question waiter。

closed process-local state：

```text
DORMANT             # stable IDs已冻结，OPEN write尚未settle
  -> OPEN_FULL | RESOLVED | ABORTED
OPEN_FULL
  -> RESOLVED | ABORTED
RESOLVED            # canonical ANSWERED winner已确认，可在await前到达
ABORTED
```

record中只保存identity、state、bounded typed resolution与future，不保存DB row truth、callback metadata或writer authority。resolver对QUESTION要求matching attempt存在，但不要求runner已经进入await；DRAFT resolution不使用该attempt。

它不得：

- 直接写repository；
- 分配event sequence；
- 创建turn或调度runner；
- 持有callback/recorder于event metadata；
- 从event replay重建future；
- 把自身状态当canonical open/closed truth。
- 为DRAFT_REVIEW创建future、等待coroutine或跨Host resume token。

### 9.2 Canonical-first与detach

open row commit是interaction成立线性化点，live Opened只是通知。controller detach不自动abort interaction：QUESTION在同一Host继续等待，后续controller从canonical snapshot发现并resolve；DRAFT_REVIEW本来就是canonical异步decision，不依赖Host-local waiter，因此Host replacement后也可由current writer resolve。普通tool confirmation的现有detach-deny语义不因此改变。

### 9.3 Resolve顺序

```text
Protocol/Host resolve request
-> repository exact command candidate + canonical CAS
-> FULL or exact-compatible winner
-> QUESTION: coordinator revalidates dormant/open identity, records terminal result, closes已有live view并settles future
-> DRAFT: Host schedules only the committed continuation turn, if one exists
```

UNKNOWN先query exact winner；不可直接重写不同answer/decision，也不可只靠live owner epoch确认canonical resolution。

### 9.4 Host crash与takeover

Host crash后QUESTION future消失。新writer takeover transaction：

- 将旧writer的RUNNING turn置INTERRUPTED；
- 将这些RUNNING turn上的OPEN QUESTION置ABORTED；
- 保留origin turn已经COMPLETED的OPEN DRAFT_REVIEW，供新Host读取和resolve；
- 保持`plan_workflows.status=ACTIVE`；
- 不合成历史Interaction Opened/Closed或tool result；
- 不恢复provider call。

下一次Plan run从canonical workflow继续；reader只对旧pending QUESTION call生成固定interruption closure。DRAFT submission已经有canonical result且origin turn已闭合，不需要closure。该closure是provider-only lowering，不是durable event或canonical result。

显式`CLOSE_SESSION`不同于Host replacement：它在session close transaction中将OPEN QUESTION/DRAFT_REVIEW置`ABORTED`、active workflow置`FORCE_EXITED`并终结RUNNING turn；closed session不得残留ACTIVE Plan。普通detach不关闭session或Plan。

### 9.5 Automatic continuation的Host交接

canonical continuation commit、ACK confirmation、ROOT slot settlement与process-local task安装之间只允许一个Host owner。一次被gateway或origin runner等待的请求协程不能成为这条链的physical owner；否则PostgreSQL transaction已经FULL后，请求取消仍可把slot永久留在`RESERVED`，或留下没有physical task的RUNNING successor。

为此新增纯process-local、Host-owned `ContinuationAdmissionAttempt`：

```text
ContinuationAdmissionAttempt
  attempt_id
  semantic_candidate: PreparedPlanContinuationAdmission
                                        # immutable；可引用stable command；不含writer guard/deadline
  expected_origin_slot_identity?
  expected_successor_turn_id
  expected_successor_initial_entry_id
  state = PREPARED
        | WRITING_OR_CONFIRMING
        | FULL_OBSERVED
        | BINDING
        | HISTORICAL_TERMINAL
        | CONFLICT
        | SETTLED
  physical_task                         # Host-owned
  bounded_waiters                       # detachable；不是owner
```

它拥有且仅拥有：repository write/exact confirmation、ROOT slot settlement和successor task bind。它不进入event metadata，不保存到数据库，不是lease、receipt、job、repair或跨Host recovery token。request cancellation、gateway connection close或origin runner cancellation只detach对应waiter，不能取消该attempt；Host close先停止新的continuation admission，再drain已有attempt及其数据库physical task和successor bind。不得持Host lock跨PostgreSQL I/O。

ROOT slot封闭为：

```text
IDLE
RUNNING(task, turn_id, optional_continuation_attempt_id)
RESERVED(continuation_attempt_id, semantic_command_id, expected_successor_turn_id)
```

- Agent enter path在origin slot仍由exact runner持有时，通过typed `AutomaticContinuationAdmissionPort`让Host先安装attempt，再由attempt执行transition write/confirmation。origin runner只等待settlement；它不再承担“FULL后通知Host换task”的职责。origin waiter被取消不影响attempt。
- APPROVE/REVISE path由gateway向Host提交stable semantic command；Host先安装attempt与exact `RESERVED` slot，随后由Host-owned task执行write/confirmation。connection task退出只detach waiter。
- `run_committed_continuation()`只执行已存在turn，绝不能再次创建turn。origin cleanup或request cleanup只有在slot仍精确属于自己且没有continuation attempt时才能清空slot。

attempt得到FULL winner后，必须在fresh bounded read/Host lease revalidation中按canonical successor分流：

| FULL winner状态 | Host处置 |
| --- | --- |
| exact successor为`RUNNING`，session仍由本Host exact current writer持有，slot仍绑定同一attempt | bind `run_committed_continuation()` exactly once；若同一successor task已经绑定则返回同一settlement |
| exact successor为`COMPLETED | INTERRUPTED` | 返回historical winner；不调度、不复活、不创建替代turn |
| successor缺失、initial entry/transition identity不匹配、slot被不相容owner占用 | `CONFLICT`，fail closed |
| successor为`RUNNING`但writer已换代或Host正在close | 不在旧Host启动；current writer的takeover/close规则负责将其interrupt，旧waiter只得到typed non-running settlement |

只有slot已经完成上述settlement后才唤醒waiter。NONE/CONFLICT在确认没有canonical winner后释放exact reservation；UNKNOWN永远先query stable semantic winner，不能盲写或换successor。若FULL后current Host仍有authority但physical task bind失败，attempt用fresh terminalization deadline把exact successor置`INTERRUPTED`，再释放slot；Host进程崩溃则由writer takeover完成同一canonical interruption。Plan transition不回滚，也不自动重建continuation。

## 10. Prepared candidates、transactions与ACK unknown

### 10.1 Immutable candidates

至少冻结以下process-local DTO：

```text
PreparedRunPermissionSnapshot
PreparedPlanWorkflowEntry
PreparedPlanQuestionOpen
PreparedPlanDraftOpen
PreparedPlanInteractionResolution
PreparedPlanWorkflowExit
PreparedPlanControlResult
PreparedPlanContinuationAdmission
PreparedPlanSemanticCommand
PlanCommandWriteAttemptGuard
```

稳定语义与可轮换writer authority必须分层：

- `PreparedPlanSemanticCommand`在第一次write前确定command/target、decision、bounded content digest、expected workflow revision、tool/result/entry/turn/event IDs与semantic fingerprint；它不包含writer generation、owner、lease、deadline或attempt-local timestamp。
- `PlanCommandWriteAttemptGuard`只包装当前`HostWriterGuard`与本次bounded deadline；Host replacement后可换代，但不能改变semantic command。
- canonical accepted/occurred time由成功transaction安装并由FULL winner返回，不进入command semantic equality。NONE后的新write attempt可以使用新的attempt time；FULL必须采用既有winner，不能重写event/time。
- Agent-origin、question-open等same-Host prepared operation也必须把guard与semantic payload分开，retry不能重新生成稳定result/entry/event identity或answer/draft binding。

所有command重试顺序冻结为：

```text
query command_id + semantic identity without writer capability
  FULL      -> return exact winner
  CONFLICT  -> fail closed
  NONE      -> bind current PlanCommandWriteAttemptGuard and write
```

不得先校验旧writer generation再查询winner，也不得把generation加入command digest。

### 10.2 Transaction matrix

| Operation | Canonical writes | Committed occurrences |
| --- | --- | --- |
| user enter | command + workflow ACTIVE | PlanWorkflowEntered |
| agent enter | workflow + Plan result + siblings + origin terminal + new Plan turn/revision/continuation entry | PlanWorkflowEntered + ToolResultAccepted(s) + TurnCompleted + PlanContinuationAccepted |
| question open | interaction OPEN + sibling results | PlanQuestionAsked + ToolResultAccepted(siblings) |
| question answer | command + interaction ANSWERED + Plan result | PlanQuestionAnswered + ToolResultAccepted |
| draft open | interaction OPEN + Plan result + siblings + origin terminal | PlanDraftSubmitted + ToolResultAccepted(s) + TurnCompleted |
| draft revise | command + interaction REVISION_REQUESTED + new Plan turn/revision/continuation entry | PlanDraftDecisionAccepted + PlanContinuationAccepted |
| draft approve | command + interaction APPROVED + workflow APPROVED + new implementation turn/revision/continuation entry | PlanDraftDecisionAccepted + PlanWorkflowExited + PlanContinuationAccepted |
| draft cancel | command + interaction CANCELLED + workflow CANCELLED | PlanDraftDecisionAccepted + PlanWorkflowExited |
| user cancel | command + workflow CANCELLED | PlanWorkflowExited |
| first human prompt after cancel/force | prompt direct entry/new turn或queue item + exact one-shot handoff edge | existing prompt/user-message occurrence only |
| force exit（running）phase 1 | QUESTION ABORTED + turn INTERRUPTED | existing TurnInterrupted |
| force exit phase 2 / idle | command + workflow FORCE_EXITED | PlanWorkflowExited |
| takeover/stop question abort | QUESTION ABORTED + turn INTERRUPTED | existing TurnInterrupted only |

table中的所有row/event均是同一owner、同一transaction。live notification和future settlement只在commit后发生。

### 10.3 Exact confirmation

每类write提供stateless exact-read confirmation，验证：

- command candidate及target；
- workflow/interaction完整identity、revision与status；
- exact assistant block/tool call binding；
- result entry/result row及origin union；
- continuation turn/revision/entry、handoff kind、permission snapshot与exact workflow/interaction binding；
- continuation FULL winner返回exact successor canonical status；Host-owned attempt按9.5分流，不把terminal successor复活；
- direct/queued human prompt的one-shot handoff claim与queue→entry exact copy；
- cancel/force winner只证明`handoff_created_at_commit`，不包含查询时的mutable handoff disposition；
- Plan semantic command equality独立于writer guard；跨generation query返回同一winner；
- expected events全有且payload fingerprint相等；
- sibling result集合全有或全无；
- turn terminal state（适用）。

确认只有`FULL | NONE | CONFLICT`。query本身不要求writer guard；`FULL`返回winner；`NONE`才允许以当前writer guard重写同一semantic candidate；`CONFLICT` fail closed。用于确认的bounded read在verified schema binding下运行，但不要求当前session writer capability。不得新增durable receipt或repair owner。

### 10.4 SQL lock order

统一顺序：

```text
session writer / event-sequence allocator
-> active plan_workflow
-> target plan_interaction
-> turn
-> prompt queue head / command
-> transcript entry / assistant block / tool result
-> append committed events
```

repository不得在持有Plan row lock后反向取得session allocator。Host process-local lock不得跨PostgreSQL I/O；先冻结candidate，再释放本地锁并执行I/O，返回后重验generation/identity。

## 11. Prompt queue、new-turn admission与safe point

### 11.1 Global ROOT admission fence

所有创建新ROOT turn的repository transaction都必须在session writer lock下读取canonical open Plan interaction，不能只依赖Host `_active_task`：

```text
OPEN QUESTION
  -> only its resolution may continue the existing origin turn
  -> reject/defer every new ROOT turn

OPEN DRAFT_REVIEW
  -> only APPROVE/REVISE/CANCEL/FORCE_EXIT/CLOSE_SESSION may transition it
  -> every other new ROOT turn returns PLAN_REVIEW_PENDING
```

该gate覆盖direct prompt、queue consumption、Terminal observation、job result、subagent result及未来任何ROOT producer。Terminal/monitor保留其process-local pending observation，job/subagent result保留原canonical result row；它们不得绕过review另开turn。SUBAGENT_TASK只可由仍RUNNING的parent创建，不是DRAFT review期间的ROOT旁路；其permission仍服从5.3 matrix。

force-exit另需一个process-local `PlanExitAdmissionFence`：从phase 1开始前安装，直到phase 2 `PlanWorkflowExited` FULL/exact winner或force operation确定terminal failure才释放。所有Host ROOT reservation入口都检查它，从而覆盖QUESTION已被ABORTED、active slot已清空但workflow尚未FORCE_EXITED的窗口。该fence不进入DB/event/metadata，不代替repository的canonical OPEN check。

APPROVE/REVISE transaction内创建的exact automatic continuation是review decision本身的一部分，通过9.5 reservation进入，不受“ordinary new ROOT”拒绝分支影响。

### 11.2 Prompt submission

`SUBMIT_PROMPT` candidate必须同时冻结：

```text
message identity/content
requested permission mode
current Plan overlay identity/revision
eligible one-shot Plan handoff identity, or NONE
permission snapshot fingerprint
delivery decision
```

ACK-unknown不能使用重试时composer的当前mode重建candidate。

存在OPEN DRAFT_REVIEW时，ordinary `SUBMIT_PROMPT`、queue delivery与steer均返回typed `PLAN_REVIEW_PENDING`；用户必须先通过dedicated decision选择APPROVE、REVISE或CANCEL。存在OPEN QUESTION时，只有`ResolvePlanInteractionRequest`可唤醒当前turn；generic steer不得冒充answer。新的ordinary prompt可以进入既有FIFO，但在question turn终结前不能消费。

### 11.3 FIFO消费

保持当前“先锁全局FIFO head再判断”的规则。Plan mismatch作为新的closed rejection reason；不得跳过head直接运行后续compatible item。拒绝一个失效head后，consumer可在同一bounded cycle继续有限个head，不能无界循环。

ENTER/REVISE/APPROVE automatic continuation不是queue item，而是在transition transaction中直接创建的canonical turn。它获得active-turn slot后，既有FIFO等待；不得让旧queued prompt插入origin turn与continuation之间。若continuation已commit但Host在启动provider前失败，takeover把该turn按普通canonical规则置`INTERRUPTED`，已经接受的Plan transition不回滚、不重建第二个turn。

### 11.4 Steer

`STEER_ACTIVE_TURN`不得携带requested mode、不得重新应用Plan overlay、不得改变snapshot。steer文本只成为existing turn exact cut后的新entry。

### 11.5 External result与autonomous observation

- 向existing turn安装仍要求provider safe point并使用existing snapshot。
- 创建new ROOT turn时必须形成新snapshot；job/subagent command提供requested mode，Terminal observation从origin turn继承requested mode。
- 创建new ROOT前必须先通过11.1 canonical interaction gate与Host admission fence；DRAFT_REVIEW期间不得安装，保留source result/observation等待后续safe point或返回typed pending。
- current Plan overlay在new-turn acceptance处生效。
- 如果candidate冻结后Plan revision变化，write返回conflict/reject，不能静默换到新workflow。

### 11.6 One-shot cancel/force handoff

workflow进入`CANCELLED | FORCE_EXITED`且没有automatic continuation时，repository在下一条真实`SUBMIT_PROMPT` acceptance中，在session lock内选择并claim eligible handoff：该prompt的accepted time/command order必须晚于workflow terminal transition，且其前面没有另一个已claim该handoff的human prompt。direct admission把edge写入USER_MESSAGE；queued admission先写入queue item，消费时exact copy。

requested mode始终以用户发送时selector为准，而不是强制使用workflow resume mode；用户在退出后先调整selector是预期行为。ACK unknown exact-confirm同一message/mode/handoff candidate，后续prompt不得重复得到旧cancellation source。若此前又进入了更新的Plan workflow，旧handoff被语义上supersede，不得在更晚prompt复活。关闭session不需要再投递handoff。

所有会创建该资格的DRAFT CANCEL、`CANCEL_PLAN`与`FORCE_EXIT_PLAN` stable command winner只返回`handoff_created_at_commit`，不得返回任何“当前仍待领取”的可变布尔值。`PENDING | CLAIMED | SUPERSEDED`只属于13.3 current-control read projection。

## 12. Structured model-input integration

### 12.1 One-cut canonical compile carrier

扩展canonical reader，使一次repeatable-read返回一个provider-neutral、process-local immutable composite。这里直接复用5.2定义的`FrozenRunPermissionSnapshot`，不得再引入含义相近的第二个permission fact DTO：

```text
PlanApprovedMaterializationDisposition =
    PIN_EXISTING_CANONICAL_BLOCK
  | MATERIALIZE_REFERENCED_BLOCK

FrozenPlanWorkflowCompileFact
  session_id / workspace_id / turn_id
  permission_snapshot_id / permission_snapshot_fingerprint
  workflow_id / workflow_ordinal / current_workflow_revision
  workflow_status = ACTIVE
  entered_by = USER | AGENT
  resume_permission_mode
  permission_contract_id / permission_contract_fingerprint
  fact_fingerprint

FrozenPlanHandoffCompileFact
  session_id / workspace_id / target_turn_id
  carrier_entry_id / carrier_entry_sequence
  workflow_id / workflow_ordinal / workflow_revision_at_transition
  interaction_id?
  handoff_kind = ENTERED_PLAN | REVISION_REQUESTED | APPROVED_PLAN
               | CANCELLED_PLAN | FORCE_EXITED_PLAN
  workflow_status = ACTIVE | APPROVED | CANCELLED | FORCE_EXITED
  resume_permission_mode
  transition_semantic_digest
  fact_fingerprint

ApprovedPlanMaterializationFact
  session_id / workspace_id / target_turn_id
  workflow_id / interaction_id
  assistant_entry_id / tool_call_id
  request_contract_id / version / fingerprint
  request_semantic_digest
  content_identity: PlanDraftContentIdentity
  exact_plan_utf8: bytes
  disposition: PlanApprovedMaterializationDisposition
  pinned_canonical_item_fingerprint?       # iff PIN_EXISTING_CANONICAL_BLOCK
  fact_fingerprint

FrozenCanonicalCompileSnapshot
  canonical_input: CanonicalModelInputSnapshot
  run_permission_snapshot: FrozenRunPermissionSnapshot
  plan_workflow_fact: FrozenPlanWorkflowCompileFact?
  plan_handoff_fact: FrozenPlanHandoffCompileFact?
  approved_plan_materialization_fact: ApprovedPlanMaterializationFact?
  canonical_read_cut_fingerprint
```

各fact均为frozen/slots、只含immutable scalar/enum/tuple/bytes，不含dict、connection、repository、policy、transport或callback。`exact_plan_utf8`必须与6.8的content identity重新计算相符；fingerprint只覆盖其size/digest identity，不重复序列化1 MiB正文。conditional union冻结为：ROOT active Plan snapshot才允许workflow fact；typed handoff edge存在才允许handoff fact；`APPROVED_PLAN` handoff才允许approved-plan fact；其他组合fail closed。

handoff matrix也必须closed：`ENTERED_PLAN`要求ACTIVE且interaction为空；`REVISION_REQUESTED`要求ACTIVE及exact DRAFT_REVIEW interaction；`APPROVED_PLAN`要求APPROVED及exact approved interaction；`CANCELLED_PLAN`要求CANCELLED；`FORCE_EXITED_PLAN`要求FORCE_EXITED。cancel/force interaction仅在该transition确实终结一个interaction时存在。`carrier_entry_id/sequence`必须exact join本次canonical input中的typed source entry，不能指向别的turn或由collector猜测。

fingerprint domain与覆盖范围冻结如下：

| DTO | Domain separator | Coverage |
| --- | --- | --- |
| workflow fact | `pulsara:plan-workflow-compile-fact:v1` | 除自身fingerprint外的全部字段 |
| handoff fact | `pulsara:plan-handoff-compile-fact:v1` | 除自身fingerprint外的全部字段及nullable presence |
| approved materialization fact | `pulsara:approved-plan-materialization-fact:v1` | 全部identity/binding/digest/disposition/pinned presence；正文以content size+digest覆盖 |
| composite | `pulsara:canonical-compile-snapshot:v1` | `canonical_input.snapshot_fingerprint`、`run_permission_snapshot.snapshot_fingerprint`及三个optional fact的presence+fingerprint |

全部使用Round 3现有central canonical framing/fingerprint helper；constructor/validator必须重算并exact join session/workspace/turn、permission/workflow与block identity。不得由collector或adapter各自解释workflow revision、handoff kind或approved-plan presence。

同一transaction必须读取turn/context revision、ordered canonical items、permission snapshot、referenced workflow/current revision、entry handoff edge与approved interaction/tool block presence，并通过6.8的central extractor构造approved content identity。`canonical_read_cut_fingerprint`不是durable audit row。

transaction关闭后：

```text
reader composite
  -> ContextSourceCollector.collect(canonical_facts=...)
  -> pure StructuredModelInputCompiler
  -> transport-aware adapter validation
```

collector可以继续采集Round 3定义的process-local environment/clock/capability facts，但不得持有connection/provider/repository或为Plan/permission二次查询数据库。workflow在reader返回后推进不污染本次model call；下一次compile重新取得新的one-cut composite。无法在同一RR内闭合exact joins时，reader在provider open前fail closed。

### 12.2 新source vocabulary

增加：

```text
ContextSourceKind.RUN_PERMISSION
ContextSourceKind.PLAN_HANDOFF
ContextSourceKind.PLAN_WORKFLOW

ProviderInputItemKind.PLAN_CONTINUATION
CanonicalInputOriginKind.PLAN_CONTINUATION
```

closed source policy：

| Source | Channel | Trust | Budget | Placement | Variants |
| --- | --- | --- | --- | ---: | --- |
| RUN_PERMISSION | SYSTEM | AUTHORIZED_CAPABILITY_CONTEXT | MUST_KEEP | 14 | FULL, COMPACT |
| PLAN_HANDOFF | SYSTEM | TRUSTED_RUNTIME_FACT | MUST_KEEP | 15 | FULL, COMPACT |
| PLAN_WORKFLOW | SYSTEM | ROOT_INSTRUCTION | MUST_KEEP | 16 | FULL, COMPACT |

它们位于runtime environment之后、capability catalog之前；placement与degradation priority仍是两个独立轴。现有32-source、4-variant、4 MiB aggregate variant和64 MiB working-set上限不需要扩大。

### 12.3 `RUN_PERMISSION`

每次model call都必须存在，内容只来自turn snapshot：

- requested/effective mode；
- Plan overlay是否生效；
- approval/terminal/filesystem行为的typed preset摘要；
- “本run内不可变，prompt不能放宽”的固定instruction；
- permission contract/snapshot fingerprint的bounded reference。

它不暴露内部路径、policy callback或secret，也不决定authorization。

### 12.4 `PLAN_HANDOFF`与continuation item

`PLAN_HANDOFF`只投影closed transition facts：workflow/interaction identity、`ENTERED_PLAN | REVISION_REQUESTED | APPROVED_PLAN | CANCELLED_PLAN | FORCE_EXITED_PLAN`、approval/cancellation语义及“不得由handoff扩大permission”的固定边界。它不包含raw draft、feedback、question或用户下一条正文。

automatic continuation的canonical initial entry由reader lower为`ProviderInputItemKind.PLAN_CONTINUATION`：

- ENTERED：固定进入Plan的Runtime transition；规划目标来自既有transcript，不伪造human message。
- REVISION_REQUESTED：bounded optional feedback作为用户决策内容，以明确的untrusted boundary呈现。
- APPROVED_PLAN：携带approval fact与exact approved-plan reference；是否在该item物化正文由下述presence proof决定。不能只给summary/digest。

provider adapter可以把该item编码为有固定边界的user-role message，但internal origin保持`PLAN_CONTINUATION`。capability/skill composer只消费真正的`USER_MESSAGE | USER_STEER`或已冻结的显式activation subject；continuation中的`$skill`、`skill:name`、plan正文或feedback均不能激活能力。

CANCELLED/FORCE_EXITED没有continuation item；下一条真实USER_MESSAGE照常参与capability selection，同时同一compile额外得到一次性`PLAN_HANDOFF` source。

`ApprovedPlanMaterializationFact`必须使用12.1的closed DTO和6.8的content identity。reader按block identity、provider item fingerprint与plan digest在同一RR中选择closed disposition，不能用字符串搜索或开放布尔值表达：

```text
PIN_EXISTING_CANONICAL_BLOCK
  -> existing exit_plan tool arguments is the sole exact materialization
  -> PLAN_CONTINUATION carries approval + reference only
  -> pinned_canonical_item_fingerprint required

MATERIALIZE_REFERENCED_BLOCK
  -> matching canonical provider item must be absent under a legal adopted snapshot
  -> PLAN_CONTINUATION materializes exact plan once
  -> pinned_canonical_item_fingerprint forbidden
```

两种disposition以外的状态、presence不匹配或content identity变化均在provider open前fail closed。allocator把被选中的唯一carrier视为protected，不得降级成summary或重复收费；若provider target budget不能容纳一次exact plan，compile在provider open前返回typed `REQUIRED_SOURCE_OVER_BUDGET`（或等价closed failure）。这不会恢复exact request audit或durable compiled prompt。

### 12.5 `PLAN_WORKFLOW`

只在ROOT turn snapshot绑定active Plan overlay时存在。reader在canonical input的同一repeatable-read视图中读取exact workflow id与当前revision并形成immutable fact；collector只渲染该fact。snapshot中的`revision_at_admission`用于audit，不要求等于current fact revision，因为question会在同一turn推进workflow，REVISE则以新turn的新snapshot承接后续revision：

- Plan active；
- physical write/terminal effect由Runtime拒绝；
- 先用read-only tools调查；
- 仅通过`ask_plan_question`询问真正阻塞的选择；
- 最终通过`exit_plan`提交完整draft；
- revise后由Runtime创建新的Plan turn，不在普通prose里宣称已退出。

source不复制question answer、feedback或draft；question/draft通过canonical tool request/result进入transcript，revision feedback通过typed continuation entry进入。

`entry_reason`也不插入trusted Plan instruction。它只供canonical control/audit读取；真正的规划目标必须由后续human prompt或既有transcript提供，避免把用户自由文本伪装成ROOT_INSTRUCTION。

### 12.6 Exact consistency

reader/collector必须在各自边界验证：

```text
turn.overlay == PLAN_READ_ONLY
-> same workflow id存在且ACTIVE

turn.overlay == NONE
-> no PLAN_WORKFLOW source

entry has Plan handoff edge
-> exactly one matching PLAN_HANDOFF source

entry kind PLAN_CONTINUATION
-> exactly one matching PLAN_CONTINUATION provider item
```

不一致在provider open前以`REQUIRED_SOURCE_UNAVAILABLE`或更窄closed failure终止turn；不得省略Plan source后继续，更不得靠event replay补齐。

### 12.7 Interrupted Plan call lowering

canonical reader在tool call无result时先检查exact Plan interaction：

```text
interaction ABORTED
  -> fixed provider-only closure:
     Plan interaction ended before a user decision was accepted;
     no physical tool effect occurred.

no Plan interaction / ordinary tool
  -> existing attempt-based before_dispatch / may_have_executed rules
```

Plan interruption不能被误写成physical side-effect unknown，也不能合成durableToolResult或历史live End。

## 13. Protocol v3与Go最小产品面

### 13.1 Wire变化

Protocol major保持v3，minor/schema fingerprint必须更新；当前未发布hard-cut不保留旧wire双分支。

新增：

- `PermissionMode` enum；
- new-turn command中的`requested_permission_mode`；
- command kind `ENTER_PLAN | CANCEL_PLAN | FORCE_EXIT_PLAN`；
- dedicated `ResolvePlanInteractionRequest`；
- Plan workflow/interaction current-control DTO；
- active turn与prompt queue permission DTO；
- Plan interaction exact content read target。

closed command-field matrix必须拒绝无关字段。例如steer/stop不得携带permission，existing-turn external acceptance不得携带新mode，Plan resolve不得携带live owner epoch/revision。

### 13.2 Canonical Plan resolution request

```text
ResolvePlanInteractionRequest
  request_id / attachment binding
  command_id
  attempt_expected_writer_generation   # attempt guard only; excluded from semantic identity
  plan_workflow_id
  expected_workflow_revision
  plan_interaction_id
  oneof:
    QuestionAnswer {
      oneof answer:
        OptionAnswer { option_ordinal }
        FreeTextAnswer { text }
    }
    DraftDecision {
      decision = APPROVE | REVISE | CANCEL
      optional string feedback
    }
```

它不使用`expected_owner_epoch/live_revision`，因为interaction是canonical。`attempt_expected_writer_generation`只用于本次write attempt；gateway必须先按command semantic查询winner，且允许重连后以同一command换成current generation。Question使用真实protobuf `oneof`证明presence：Option branch只携带合法ordinal；FreeText branch必须non-empty；UI-owned Other只能生成FreeText branch，永远没有伪ordinal。

Draft feedback使用proto3 presence-aware `optional string`（等价closed oneof也可，不能用普通string猜presence），wire与semantic normalization冻结为：

| Decision | feedback presence | 处置 |
| --- | --- | --- |
| APPROVE / CANCEL | absent | legal |
| APPROVE / CANCEL | present-empty或present-value | `INVALID_ARGUMENTS` |
| REVISE | absent | legal，normalize为`NO_FEEDBACK` |
| REVISE | present-empty | legal，同样normalize为`NO_FEEDBACK` |
| REVISE | present-nonempty且<=32 KiB UTF-8 | legal，normalize为`FEEDBACK_TEXT`并保存exact text |

因此REVISE absent与present-empty具有**相同semantic identity**：`feedback_present=false`、没有continuation feedback body、同一command重试可exact-confirm同一winner；只有present-nonempty使`feedback_present=true`并进入candidate digest。Runtime不判断非空feedback是否相关。普通`ResolveInteractionRequest`继续只处理process-local allow/deny confirmation。

response/winner必须typed返回`decision`、`workflow_status`、`resume_permission_mode`、optional `continuation_turn_id`与稳定的`handoff_created_at_commit`。APPROVE/REVISE的continuation id来自已commit candidate，Go不得自行创建turn；CANCEL的id为空，且只有该commit确实创建一次性handoff资格时`handoff_created_at_commit=true`。该字段是occurrence-like stable winner fact，不表示handoff当前仍pending；ACK unknown即使发生在handoff已被后续prompt claim之后，也返回相同winner。

当前handoff disposition只通过`CanonicalControl`读取并封闭为`PENDING | CLAIMED | SUPERSEDED`（closed session可以不再投影）。它由canonical workflow、claim entry/queue edge及更新workflow计算，不能进入command semantic winner或被客户端缓存为永久current state。

### 13.3 Snapshot与suffix projection

`CanonicalControl`必须bounded展示：

- active Plan workflow id/revision/entry source/status；
- latest CANCELLED/FORCE_EXITED workflow的bounded handoff disposition（`PENDING | CLAIMED | SUPERSEDED`）与resume-mode view；这是current read projection，不进入command winner；
- 至多一个open Plan interaction的kind/status/public summary；
- active turns requested/effective mode与Plan override；
- queued prompt的requested/effective mode与Plan binding。

七类Plan event suffix按其subject lower为typed/redacted `CurrentControlProjection`或`ImmutableEntryProjection`，gateway在同一bounded read transaction读取exact subject。TUI不解析event payload推断Plan状态。

### 13.4 Exact content read

扩展`ReadContentRequest`为真正的closed target union：

```text
ReadPlanQuestionContent { interaction_id }
  -> PlanQuestionContent                    # bounded typed whole DTO

ReadPlanDraftTextChunk {
  interaction_id
  expected_plan_utf8_digest?
  offset_utf8_bytes
  limit_bytes
}
  -> PlanDraftTextChunk
```

两条路径都必须调用6.8的central historical extractor，并校验session/workspace、controller capability、referenced assistant block/tool name、request binding与semantic digest。Question/options不暴露任意JSON carrier；draft offset只相对decoded `plan`字段的UTF-8 body，不相对整个JSONB、`tool_arguments::text`或summary。不得把1 MiB draft塞入snapshot/event/live frame。

`PlanDraftContentIdentity`的size/digest与chunk body保持原样；renderer安全不是storage rewrite。Go先验证stable identity、连续offset、最终size/digest，再在question、option label/description、summary、draft、answer/feedback的每个显示入口统一调用现有`publictext.Transform`（大正文可使用语义等价的bounded streaming wrapper），然后进入viewport/cache。C0/C1与ESC/OSC必须显示为inert visible escapes，不能到达terminal renderer；Protocol malformed bytes/digest mismatch是typed content error。display expansion计入现有client cache bound，不允许因最多1 MiB Plan body形成unbounded transformed buffer。

### 13.5 Go最小DoD

- 输入框旁有四态permission selector；
- 每次submit发送exact requested mode；
- active/queued run显示requested与effective，Plan override可见；
- 可进入Plan、取消Plan、force exit；
- 可显示question/options并提交answer；
- `allow_free_text`时显示UI-owned“其他”入口及自然语言输入；
- 可读取完整draft并approve/revise/cancel；
- APPROVE/REVISE后显示Runtime自动创建的后续turn；CANCEL不产生空白turn；
- Plan退出后permission selector回到workflow `resume_permission_mode`，但用户在下一次真实submit前可继续调整；
- detach/attach后从canonical snapshot恢复UI；
- live GAP后重新snapshot，不从event payload猜状态。
- 所有Plan content UI经过`publictext.Transform`；question fingerprint针对未改写typed fields，draft digest针对6.8未改写的derived UTF-8 body。

advanced editor与Plan diff/history不属于本轮。

## 14. Failure、cancellation与crash矩阵

| 场景 | Canonical处置 | 是否继续provider/effect |
| --- | --- | --- |
| invalid permission mode/fingerprint | command REJECTED，无turn/queue | 否 |
| workflow resume permission contract未知 | automatic transition REJECTED，既有workflow/interaction不变 | 否；允许cancel/force-exit |
| Plan row/snapshot mismatch | queue reject或turn conflict | 否 |
| user enter与prompt并发 | session lock commit order决定；loser按新Plan context重验 | 不越权 |
| Agent enter成功 | workflow+result+origin terminal+new Plan turn/continuation同tx | origin不再provider；FULL后调度new turn |
| Plan tool有siblings | siblings cancelled-before-dispatch | 无physical effect |
| question在waiter await前被resolve | canonical ANSWERED；dormant attempt保存resolution | promote后立即继续，不丢wake |
| question等待超过旧cycle deadline | row/attempt保持OPEN；旧deadline不复用 | ANSWERED后签发fresh bounded cycle deadline |
| question resolve ACK unknown | exact-confirm same command/candidate | FULL后same-Host继续 |
| question stop/Host close | interaction ABORTED，turn INTERRUPTED，Plan ACTIVE（session close除外） | 否 |
| draft REVISE | decision + new Plan turn/continuation，Plan ACTIVE | FULL后自动启动read-only turn |
| draft APPROVE | exact accepted draft + workflow terminal + implementation continuation | FULL后按restored preset自动启动 |
| draft CANCEL | workflow terminal，无new turn | 等下一条human prompt；一次性cancellation handoff |
| DRAFT_REVIEW期间Terminal/job/subagent/queue尝试new ROOT | repository typed `PLAN_REVIEW_PENDING`或保留source pending | 不创建第二turn |
| force-exit phase 1后ordinary admission | process-local exit fence拒绝；repository继续重验 | phase 2 FULL前不创建turn |
| approved plan超过target budget | compile typed fail，implementation turn INTERRUPTED | 不省略exact plan、不调用provider |
| approved plan已在canonical items | pin existing block，continuation只引用 | exact正文只计量/呈现一次 |
| force exit | interrupt+abort tx，join local runner，再独立exit tx | 否，不自动新run；中途crash保持Plan active |
| Host crash with QUESTION | live/future消失；takeover aborts question/turn | 不恢复coroutine |
| Host crash with DRAFT_REVIEW | origin turn已完成；open draft保留 | 新Host可canonical resolve，不恢复coroutine |
| DRAFT decision由旧writer提交但ACK丢失 | 新Host先按stable semantic command查询FULL winner | 不受旧generation阻塞，不重写winner |
| request/gateway在continuation FULL后取消 | 只detach waiter；Host-owned admission attempt继续settle slot与bind | exact RUNNING successor bind once；无taskless RUNNING/永久RESERVED |
| ACK-lost winner的successor已COMPLETED/INTERRUPTED | 返回historical terminal winner | 不调度、不复活、不创建替代turn |
| ACK-lost winner缺失或successor identity不匹配 | attempt `CONFLICT`并释放exact reservation | 不调度 |
| Plan interaction descriptor binding未知 | 保留canonical row；answer/approve/revise typed拒绝 | cancel/force-exit仍可用 |
| continuation commit后scheduler/provider open失败 | committed new turn转INTERRUPTED；Plan decision保留 | 不重建第二个continuation |
| explicit CLOSE_SESSION | interaction ABORTED、workflow FORCE_EXITED、turn INTERRUPTED | session永久关闭 |
| provider failure in Plan | turn INTERRUPTED，Plan ACTIVE | 下一run继续Plan |
| event consumer/hook failure | canonical commit保留 | 不回滚 |
| live queue overflow | GAP/detach observer | 不阻塞runner |
| DB ACK unknown | stateless exact confirmation | 不换candidate |
| stale Plan-linked queue head | PromptRejected | 不转投其他workflow |
| permission UI changed mid-run | active snapshot不变 | 后续submission才生效 |
| Plan JSON key order/escape spelling变化但typed值相同 | central extractor得到相同typed question fingerprint/plan UTF-8 identity | 不把JSON serialization当正文identity |
| REVISE feedback missing或present-empty | normalize为同一`NO_FEEDBACK` candidate/winner | 自动continuation不携带feedback body |
| CANCEL winner返回后handoff已被claim | winner仍返回`handoff_created_at_commit=true`；CanonicalControl显示`CLAIMED` | 不把current state写回stable winner |
| Plan content含control字符或wire chunk损坏 | canonical typed fields/derived UTF-8 identity不变；Go显示inert escapes或返回typed content error | 不执行terminal control sequence |

## 15. Security、hooks与sensitivity

- question/answer/draft/feedback属于session-private conversation content；普通extension默认只看typed/redacted状态。
- `AssistantToolRequestAccepted`/immutable-entry projection对三项Plan tool的arguments同样按sensitive tool-arguments capability处理；不能因为正文位于既有assistant block就绕过Plan content redaction。
- exact draft read要求controller、session/workspace与content capability；observer默认只能看bounded public summary，除非显式授予content capability。
- Plan system source是trusted instruction；question/draft正文仍是model/user content，不能携带authorization capability。
- `PLAN_HANDOFF`只含trusted transition facts；approved plan、revision feedback与continuation正文使用独立typed/untrusted边界。它们不能激活skill或放宽permission。
- Go `publictext.Transform`只是display sink boundary，不能把sanitized text回写canonical Plan row、digest、answer或provider input。
- Terminal/MCP/private URL/secret能力不因Plan恢复而开放；read-only policy仍是最终effect gate。
- pre-commit Plan/permission policy只能通过显式typed port，不是ordinary hook。
- post-commit hook异常、超时、overflow按现有best-effort隔离；跨进程必达扩展必须另建具名durable job，本轮不新增。
- logs/diagnostics只记录ID、closed code、length、digest和decision，不记录raw plan或answer。

## 16. Reset-only schema与migration contract

### 16.1 唯一baseline

本仓库已经是clean migration universe：

```text
pulsara.conversation-kernel.v1
generation 1
version 0
```

Round 4直接修改 [`0000_conversation_kernel_baseline.sql`](src/pulsara_agent/storage/migrations/sql/0000_conversation_kernel_baseline.sql) 与对应manifest/verifier/golden，不新增`0001_plan.sql`。旧数据库必须返回typed `RESET_REQUIRED`且确认前DDL为0；本轮不做兼容迁移或dual-read/write。

### 16.2 必须更新的identity

- baseline contract hash；
- migration universe fingerprint / registry prefix；
- exact expected catalog与grant artifact；
- product relation oracle 24→26；
- event/subject SQL closed checks；
- deep verifier function/trigger/catalog checks；
- reset ACK tests与runbook evidence。

required extension仍只有compatible `public.vector >= 0.5.0`；Round 4不新增extension或grant domain。

## 17. 实施切片

### R4-0：Inventory、上位口径与guards

- 记录dirty worktree、HEAD与本文/Gap/Reassessment hash；
- inventory所有turn creator、permission reader、Plan descriptor、Protocol command、event oracle与schema relation；
- 先同步Reassessment active oracle为`34/23/15/2`及26 relations/4 jobs；
- 保留27/23/13/2只作为Round 2/3历史activation记录；
- 建立negative guards：无Plan reducer/replay/job/第三guard、无Host mutable permission authority。

### R4-A：Permission snapshot

- primitives contract与preset fingerprint；
- `turns`/queue schema列与DB invariant；
- ordinary prompt、queue、steer、external result、Terminal observation、subagent全部接入；
- ROOT/SUBAGENT_TASK closed matrix与parent context-ordinal inheritance；
- policy request、attempt acceptance与invoke exact join；
- Protocol requested mode先可dormant，但production不能同时保留隐式fixed policy路径。

### R4-B：Canonical Plan domain

- 两张relation、七类event、两个新增subject slot及既有ENTRY subject continuation；
- repository prepared candidates与exact confirmation；
- stable semantic command / rotatable writer-attempt guard分层；
- Plan interaction descriptor ID/version/fingerprint与窄historical decoder registry；
- central `PlanQuestionContent` / `PlanDraftContentIdentity` / chunk extractor及跨语言golden；
- tool result origin union；
- user Plan commands、current query与takeover abort；
- 此阶段未接runner前保持Plan tool executor unavailable，不做半套行为。

### R4-C：Runtime workflow

- ROOT surface/child exclusion；
- runner batch barrier；
- enter/question/exit control port；
- question-only process-local coordinator、detach/attach、stop/force-exit与canonical draft review；
- dormant question wait attempt与resolve-before-await closure；
- human wait之外的fresh model-cycle/terminalization deadlines；
- enter/revise/approve使用Host-owned `ContinuationAdmissionAttempt`；request cancellation只detach waiter，Host close drain；
- FULL successor status分流、ROOT slot exact settlement/bind-once、all-producer canonical review gate与force-exit admission fence；
- interrupted Plan lowering。

### R4-D：Compiler

- 三类source、continuation input kind与contract/registry/policy同步；
- reader在同一RR cut返回12.1四个exact DTO与closed disposition；每个fact及composite重算fingerprint，collector不查DB；
- approved plan identity-based exact-once materialization与budget proof；
- Plan mismatch fail closed；
- golden placement、FULL/COMPACT与budget tests。

### R4-E：Protocol v3与Go

- schema、Python/Go生成物与fingerprint；
- command/resolve/typed question read/draft UTF-8 chunk read/current-control；
- question answer真实oneof、Draft feedback真实presence、stable `handoff_created_at_commit`与Plan正文`publictext.Transform` renderer boundary；
- composer selector与最小Plan review UI；
- attach/snapshot/GAP回归。

### R4-F：Activation

- reset ephemeral DB并验证fresh install/second migrate/deep verify；
- 全量Python/PostgreSQL/Go gates；
- real-provider Plan question/revise/approve与permission denial dogfood；
- 更新Gap Index、README、activation evidence；
- 只有所有DoD满足才把本文标记`ACTIVATED`。

## 18. 主要修改面

预期production面（实现者必须按实际inventory调整，不得因列表遗漏绕开contract）：

```text
src/pulsara_agent/primitives/permission.py
src/pulsara_agent/tool_permission.py
src/pulsara_agent/conversation_kernel/contracts.py
src/pulsara_agent/conversation_kernel/vocabulary.py
src/pulsara_agent/conversation_kernel/repository.py
src/pulsara_agent/conversation_kernel/reader.py
src/pulsara_agent/conversation_kernel/runner.py
src/pulsara_agent/conversation_kernel/host.py
src/pulsara_agent/conversation_kernel/tool_policy.py
src/pulsara_agent/conversation_kernel/tool_runtime.py
src/pulsara_agent/conversation_kernel/context_sources.py
src/pulsara_agent/conversation_kernel/interaction.py              # ordinary confirmation边界不得混淆
src/pulsara_agent/model_input/contracts.py
src/pulsara_agent/model_input/compiler.py
src/pulsara_agent/capability/builtin_catalog.py
src/pulsara_agent/ports/live_agent_event.py                        # 仅复用/必要typed public shape
src/pulsara_agent/terminal_protocol/schema/terminal_kernel_v3.proto
src/pulsara_agent/terminal_protocol/canonical_v3.py
src/pulsara_agent/terminal_protocol/v3_gateway.py
src/pulsara_agent/storage/migrations/sql/0000_conversation_kernel_baseline.sql
src/pulsara_agent/storage/migrations/{manifest,verifier,...}.py
clients/terminal/internal/protocolv3/
clients/terminal/internal/kernelclient/
clients/terminal/internal/kernelapp/
```

推荐新增窄owner：

```text
src/pulsara_agent/primitives/run_permission.py
src/pulsara_agent/conversation_kernel/plan.py
src/pulsara_agent/conversation_kernel/plan_content.py              # optional central historical extractor owner
src/pulsara_agent/ports/plan_workflow.py
```

禁止新增`runtime/session.py`、`event_log/`、projection/reducer/checkpoint/receipt package。

## 19. 必须有的tests

### 19.1 Permission snapshot

- 四种preset golden与contract fingerprint；
- user submit requested/effective exact freeze；
- UI mode改变不影响active/queued turn；
- Plan overlay把任意requested mode收窄为read-only；
- active Plan下ROOT使用PLAN_READ_ONLY；SUBAGENT_TASK继承parent effective/context cut但overlay/workflow refs为空；
- queue copy字节级相同，ACK-unknown不重绑mode；
- steer不能携带/改变permission；
- job/subagent/Terminal/new child所有turn creator均有snapshot；
- unknown contract、snapshot drift、attempt/invoke join mismatch fail closed。

### 19.2 Plan schema与events

- one ACTIVE workflow/session；
- entered_by source union；
- exact ROOT tool-call FK；
- one OPEN interaction/workflow；
- kind/status/resolution union；
- request contract ID/version/fingerprint/digest与historical decoder golden；unknown binding只允许cancel/force-exit；
- interaction open binding exact join实际advertised Plan tool surface，client/current catalog不能伪造或补填；
- question typed extraction与draft UTF-8 identity golden覆盖JSON key order/whitespace/escape差异、Unicode、空/最大正文、wrong binding与chunk boundary；
- accepted draft exact join；
- continuation entry/source edge、one-shot handoff与resume permission invariants；
- seven event producer/subject/guard/transaction mapping；
- event payload无raw content；
- final oracle `34/23/15/2/26/4`。

### 19.3 Workflow happy path

- user enter→next bypass-requested prompt effective read-only；
- agent enter结束origin turn并原子创建new Plan turn；origin不再provider，new turn只在FULL后自动启动；
- batch `[write_file, enter_plan]`与`[enter_plan, write_file]`均无physical effect；
- active idempotent enter不重复event；
- question option/free-text resolve继续same turn；
- answer可在OPEN commit后、runner await前到达且不会lost wake；
- human wait超过旧operation deadline后，回答仍以fresh cycle deadline继续；terminalization也使用独立deadline；
- UI-only Other不污染model options，answer acceptance后成为exact canonical Plan result；
- draft submission关闭origin turn；REVISE创建新read-only turn并生成新ordinal；
- APPROVE exact绑定完整plan、恢复preset并自动创建implementation turn；
- CANCEL不自动provider，下一条真实prompt取得一次性handoff且只出现一次；
- queued next-human prompt冻结handoff并在消费时exact copy；ACK unknown或后续prompt不重绑，rejected item不转移claim；
- missing与present-empty REVISE feedback exact normalize为同一`NO_FEEDBACK` winner；non-empty/odd feedback进入new Plan turn，Agent可再次question；Runtime不做语义相关性判断；
- cancel/force exit不放宽current snapshot。

### 19.4 Crash、queue与race

- detach/reattach same Host可从canonical snapshot resolve；
- controller detach不自动abort Plan；
- stop/close/takeover abortQUESTION并interrupt origin turn；OPEN DRAFT_REVIEW在Host replacement后仍可resolve；
- reopen不恢复future/provider；
- continuation commit与provider scheduling failure不生成duplicate continuation；
- connection/origin waiter在DB FULL前后取消都只detach；Host-owned continuation attempt仍settle slot且不会留下taskless RUNNING或永久RESERVED；
- FULL successor为RUNNING且current-writer exact时bind once；已COMPLETED/INTERRUPTED只返回historical winner，缺失/identity mismatch conflict；
- agent/approve/revise统一由Host attempt原子settle successor slot，command reservation阻止queue/Terminal抢占；Host close drain attempts；
- DRAFT_REVIEW期间direct/queue/Terminal/job/subagent所有ROOT producer均不能创建turn；
- force-exit phase 1→2窗口由process-local admission fence封闭；
- ACK unknown跨writer replacement先查stable semantic winner，writer generation可换且不进入digest；
- CANCEL stable winner在handoff被claim后仍保持`handoff_created_at_commit=true`，CanonicalControl独立从`PENDING`推进`CLAIMED/SUPERSEDED`；
- pre-Plan queue head在Plan进入后确定性reject；
- pre-Plan queue head即使等到同一workflow已经退出也不会重新变compatible；
- Plan-linked queue在same workflow revision推进后仍使用原snapshot；workflow id改变/exit后reject且不转投；
- Plan enter vs prompt、resolve vs force-exit、resolve ACK unknown竞态重复运行。

### 19.5 Compiler

- RUN_PERMISSION始终存在且placement固定；
- canonical input、`FrozenRunPermissionSnapshot`、workflow/handoff与approved-plan closed fact来自同一RR composite；每个fact及composite fingerprint覆盖12.1 exact fields，workflow并发推进不产生mixed cut；
- collector/compiler production import与runtime probe证明不会读取repository/DB；
- PLAN_HANDOFF exact-once并只含typed transition facts；
- PLAN_WORKFLOW仅ROOT Plan run存在；
- missing/mismatched row在provider open前失败；
- Plan source不能扩大tool surface/permission；
- question answer通过tool result、revision feedback通过continuation item进入，不在trusted source重复；
- continuation在provider wire可用user-role，但不能成为skill/capability activation subject；
- approved plan按block/content identity与closed disposition exact-once：`PIN_EXISTING_CANONICAL_BLOCK`时pin既有tool arguments，`MATERIALIZE_REFERENCED_BLOCK`时才由continuation物化；非法presence或over-budget typed fail；
- interrupted Plan call使用Plan-specific closure，不出现side-effect unknown；
- 既有source golden与physical bounds不回归。

### 19.6 Protocol/Go

- command field closed union；
- QuestionAnswer必须是protobuf oneof；both/neither、伪Other ordinal与empty free text均拒绝；
- DraftDecision使用真实presence：APPROVE/CANCEL携带任意feedback拒绝，REVISE missing/present-empty同identity、non-empty exact保留；
- requested/effective snapshot round-trip；
- canonical Plan resolve不依赖live epoch；
- generic tool resolve不能处理Plan；
- current-control snapshot/suffix/GAP rebuild；
- question返回bounded typed DTO；1 MiB draft按derived UTF-8 body identity做bounded code-point-safe chunk read，跨语言golden一致；
- question/options/summary/draft/feedback的C0/C1/ESC/OSC经过`publictext.Transform`；question fingerprint针对typed fields，draft digest针对6.8 derived UTF-8 body，malformed wire bytes fail typed；
- Go selector、question、review、decision state tests；
- Other/free-text、automatic continuation、permission restore与cancel one-shot handoff tests；
- protocol generator check、Go test/vet/module verify。

### 19.7 Negative architecture

- production import不存在旧RuntimeSession/EventLog/reducer/replay；
- no `PlanSnapshotEvent`、receipt、checkpoint或durable continuation runner/recovery；
- no third append guard或fifth job；
- no raw plan content in event/log fixture；
- no mutable dict/policy in frozen snapshot；
- no Plan tool in SUBAGENT_TASK surface；
- no physical attempt for Plan control或siblings；
- context collector/compiler无Plan DB/repository capability；
- no second continuation turn creator outside Host ROOT slot owner；gateway/request/origin runner task不拥有continuation write→bind链。

## 20. Static architecture guards

至少加入/更新以下guard：

```text
product relations == 26
committed descriptors == 34
live vocabulary == 23
typed subject slots == 15
append guards == 2
durable job catalog == 4

Plan event append guards == {HostWriterGuard}
Plan events -> descriptor-defined exact subject
PlanContinuationAccepted -> exact ENTRY subject
Plan raw fields absent from event payload schema
Plan rows absent from event replay/reducer imports
permission snapshot required on every turn creator
ROOT/SUBAGENT permission matrix is closed
Plan tools absent from child prepared surface
interaction_decisions no longer accepts PLAN
Plan command semantic digest excludes writer guard/generation
Plan compiler source collector imports no repository/DB provider
Plan compile carrier uses exact closed fact DTOs and central fingerprints
all ROOT turn creators enforce canonical Plan interaction admission
continuation write/confirm/slot-bind owner == Host ContinuationAdmissionAttempt
Plan draft byte identity owner == central historical extractor
Protocol enum/projection map covers exact 34/23
```

guard必须证明正向闭合与负向缺失，不能只搜索某个名称存在。

## 21. 验证命令

实施者根据新增test文件补齐exact路径，但最终至少执行：

```bash
uv run pytest --collect-only -q
uv run pytest -q

PULSARA_TEST_POSTGRES_DSN="$PULSARA_BENCHMARK_POSTGRES_ADMIN_DSN" \
  uv run pytest -q -m postgres

uv run ruff check .
uv run python -m compileall -q src tests tools
uv run python tools/generate_terminal_protocol_contract.py --check

(cd clients/terminal && go test ./...)
(cd clients/terminal && go vet ./...)
(cd clients/terminal && go mod verify)

uv lock --check
git diff --check
```

还必须执行：Markdown fence、duplicate heading、本地链接、secret/raw Plan fixture、schema catalog/grant fingerprint与old-runtime negative import checks。禁止新增skip/xfail掩盖Round 4回归。

## 22. Real-provider dogfood

至少使用一个真实provider和ephemeral PostgreSQL数据库完成：

1. composer选择`accept-edits`，用户在同一prompt给出目标并要求先规划；Agent调用`enter_plan`；证明没有sibling effect、origin turn结束且Runtime自动创建Plan turn。
2. 自动Plan turn记录requested accept-edits/effective read-only；Agent可用read-only工具调查，写工具被typed policy拒绝。
3. Agent调用`ask_plan_question`；在一条竞态路径中OPEN FULL后立刻回答、早于runner await，在另一条路径中等待超过旧cycle deadline再回答；两者都继续同一turn且answer canonical。
4. Agent提交draft，origin turn关闭；用户REVISE；Runtime自动创建新Plan turn，模型接收optional feedback并再次提交。
5. 用户APPROVE；Runtime自动创建implementation turn，requested/effective恢复accept-edits；第一次compile用identity proof证明exact approved plan只materialize/计量一次，并完成一个受该preset允许的实现步骤。
6. 另起一次Plan并选择CANCEL；证明没有空白provider call。用户在composer把mode改为`read-only`后发送新需求；该真实prompt只获得一次cancellation handoff，下一prompt不再重复。
7. 检查canonical rows/events、sequence、continuation identity、Plan content redaction与permission fingerprints。

dogfood evidence不得记录API key、完整environment、raw thinking、完整plan正文或question answer；只记录ID、digest、长度、closed decision与查询结果。

## 23. Definition of Done

只有以下全部成立，Round 4才可从DRAFT改为ACTIVATED：

1. 两张Plan relation是唯一current workflow/interaction truth，DB约束闭合。
2. 每个turn和queued prompt都有immutable permission snapshot；所有turn creator无旁路。
3. Plan active在admission处强制read-only，Plan exit不改变in-flight turn。
4. tool authorization、attempt与invoke exact join同一snapshot。
5. 三项Plan tool在ROOT production surface可达、child不可见，并有batch control barrier。
6. QUESTION使用pre-write dormant attempt封闭resolve-before-await，human wait不消耗physical deadline；draft submission关闭origin turn；enter/revise/approve以canonical typed continuation和Host-owned admission attempt自动创建新turn，request cancellation只detach waiter且FULL后按successor status bind once/不复活，cancel不创建turn。
7. Host close drain continuation attempts；crash/takeover不恢复coroutine；DRAFT_REVIEW可由新Host以stable semantic command resolve，writer generation只属于write attempt。
8. 七类Plan event与row同transaction；payload typed/redacted；无event replay。
9. oracle为`34 Committed / 23 Live / 15 subjects / 2 guards / 26 relations / 4 jobs`。
10. reader在一个RR cut冻结canonical input、`FrozenRunPermissionSnapshot`及三个closed Plan fact，并按固定domain重算composite fingerprint；RUN_PERMISSION、PLAN_HANDOFF与PLAN_WORKFLOW通过Round 3 pure compiler进入provider input，collector/compiler不获得DB/policy capability，continuation origin不会激活skill。
11. Protocol v3与Go最小selector/question/draft/decision闭环，QuestionAnswer为true oneof，Draft feedback有真实presence，stable command winner不携带mutable handoff state；question typed DTO与draft UTF-8 body identity复用central extractor，所有Plan正文经renderer-safe transform，snapshot/GAP可重建。
12. clean-v0 fresh install、second migrate、deep verify与old-universe RESET_REQUIRED通过。
13. full pytest、PostgreSQL、Ruff、compileall、Protocol生成、Go test/vet/module verify、lock与diff checks全绿。
14. real-provider dogfood证明enter→question→revise→approve→next-run permission完整happy path。
15. 没有新增旧durability/recovery owner、第三guard、Plan job、generic receipt或raw sensitive event。

## 24. Coding handoff边界

coding agent可以在本规格内调整类名、私有helper与文件拆分，但不能自行改变：

- 两张canonical relation及其authority；
- run admission冻结requested/effective permission的语义；
- Plan read-only overlay只能收窄；
- 七类committed event、两个新增subject slot与两类guard；
- Plan control无physical attempt、batch siblings不dispatch；
- stable Plan semantic command不包含writer generation，write attempt可换guard且必须先查winner；
- enter/revise/approve automatic canonical continuation，cancel one-shot next-human handoff；
- question dormant waiter/fresh deadline、draft canonical async review与各自crash语义；
- all-producer ROOT admission fence、Host-owned continuation admission attempt/slot settlement与queue Plan mismatch rejection；
- one-cut exact fact DTO/composite fingerprints、three compiler source、PLAN_CONTINUATION origin及closed-disposition exact-once approved plan；
- true question-answer oneof、Draft feedback presence、stable handoff-created winner与current-control disposition分层；
- central Plan historical extractor、derived draft UTF-8 identity/chunk contract与Go renderer-safe content boundary；
- reset-only clean-v0；
- `34/23/15/2/26/4` activation oracle。

若实现发现上述任一contract在当前代码中物理不可闭合，必须停止相应slice，给出代码证据并修订规格；不得以自由JSON、event replay、mutable Host state、fake attempt、silent fallback或额外durable machinery绕过。
