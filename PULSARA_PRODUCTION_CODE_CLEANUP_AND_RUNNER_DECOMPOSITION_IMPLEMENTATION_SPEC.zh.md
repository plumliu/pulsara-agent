# Pulsara：生产代码清理与 Conversation Runner 解耦实施规格

> 状态：**ACTIVATED**
>
> 修订日期：2026-08-22
>
> 编码基线：以实际开始编码时已经提交的当前开发主线为准。Git提交负责代码身份；本文不记录per-file、document或activation-evidence SHA。
>
> 激活证据：[production_code_cleanup_and_runner_decomposition_activation.json](benchmarks/suites/core/v1/production_code_cleanup_and_runner_decomposition_activation.json)
>
> 强制上位契约：[AGENTS.md](AGENTS.md)、[Fingerprint Subtraction Hard Cut](PULSARA_FINGERPRINT_SUBTRACTION_HARD_CUT_IMPLEMENTATION_SPEC.zh.md)、[Round 3.1 Prefix Continuity](ROUND_3_1_PROVIDER_INPUT_PREFIX_CONTINUITY_IMPLEMENTATION_SPEC.zh.md)、[Round 5B Compaction](ROUND_5B_LONG_HORIZON_CONTEXT_COMPACTION_IMPLEMENTATION_SPEC.zh.md)、[Round 9 Capability](ROUND_9_UNIFIED_CAPABILITY_SEMANTICS_IMPLEMENTATION_SPEC.zh.md)、[Round 9.1 Agent Skills](ROUND_9_1_AGENT_SKILLS_STANDARD_IMPLEMENTATION_SPEC.zh.md)、[Round 10 Subagent](ROUND_10_HIERARCHICAL_SUBAGENT_ORCHESTRATION_IMPLEMENTATION_SPEC.zh.md)。

本文实施一次**产品语义完全不变的生产代码hard-cut清理**：

1. 删除已经没有生产消费者的旧registry、classifier、DTO、fingerprint与兼容参数；
2. 把当前过大的 ConversationKernelRunner 按既有authority边界拆成窄coordinator；
3. 不建立旧路径兼容层，不保留deprecated alias或dual import；
4. 不改变canonical schema、provider输入、工具surface、permission、memory、compaction、subagent、Plan或long-horizon行为。

本文不是新产品Round，不恢复已暂缓的Hook或Plugin系统，也不为未来能力预埋接口。

---

## 0. 最终目标

### 0.1 保持不变的产品truth

本轮结束后，下列行为必须逐项保持：

- ROOT与SUBAGENT_TASK turn admission、ACK-unknown confirmation和terminalization；
- same-epoch SYSTEM/tools byte-identical，messages只追加suffix；
- cold open与explicit compaction successor仍是仅有的root rebase边界；
- Round 9 capability owner snapshots、native/meta MCP与physical exact join；
- Round 9.1 Skill catalog、ordinary read_file与retained Skill；
- Round 5A.2 durable provider replay与跨重启thread continuation；
- Round 5B manual/automatic/mid-turn compaction、active/idle adoption和runtime handoff；
- Round 8 advisory memory、recall、reflection和citation；
- Round 10 ROOT-orchestrated leaf worker DAG、mailbox与dependency result；
- Plan workflow、Terminal process/monitor、TODO与ToolResult settlement；
- caller cancellation、known physical result、ACK-unknown和process-local effect settlement语义；
- existing physical deadlines、pagination、queueing与long-horizon availability。

本轮不得增加或删除任何真实产品能力。

### 0.2 Architecture oracle

Hook与Plugin仍为DEFERRED、NOT ACTIVATED，因此当前production oracle保持六维：

~~~text
Committed events   29
Live events        24
Subject slots      11
Append guards       1
Product relations  25
Durable jobs        0
~~~

本轮不得新增relation、event、subject、guard、job、receipt、checkpoint、reducer、replay、repair graph或projection authority。

### 0.3 目标依赖方向

~~~text
ConversationKernelRunner
  ├─ TurnAdmissionCoordinator
  ├─ ProviderDispatchCoordinator
  │    ├─ MemoryDispatchSupport
  │    ├─ SteerConsumptionCoordinator
  │    └─ existing cold/capability/compiler/continuity owners
  ├─ CompactionCoordinator
  │    └─ ProviderDispatchCoordinator
  ├─ ToolBatchExecutor
  └─ PlanToolBatchCoordinator
~~~

ConversationKernelRunner继续拥有foreground turn主状态机，但不再实现每个子系统的repository、compiler或physical settlement细节。

---

## 1. 当前问题

当前 src/pulsara_agent/conversation_kernel/runner.py 约7,354行，ConversationKernelRunner本体约5,643行；它导入约291个名字，并直接协调：

- turn admission；
- provider target/capability/source/compiler/continuity planning；
- memory recall与fallback；
- steer hydration与settlement；
- active/idle compaction；
- provider open与assistant settlement；
- Plan control batch；
- Tool authorize/invoke/live/result settlement；
- subagent explicit/inferred result；
- failure terminalization。

其中两个方法已经分别超过九百行：

~~~text
_prepare_provider_dispatch   1,252 lines
run_accepted_turn              975 lines
~~~

问题不是违反某个任意LOC上限，而是：

1. 一个方法同时跨越多个已有authority；
2. exact linearization与cancel/ACK路径难以局部证明；
3. tool_runtime、subagent和memory_tools反向从runner导入共享Tool contracts；
4. architecture tests被迫依赖runner.py中的具体符号位置；
5. compaction successor通过runner私有共享槽跨阶段传递；
6. 清理一个子系统时容易误触另一个产品路径。

---

## 2. 第一部分：确认死代码hard-cut

### 2.1 旧ToolRegistry owner

删除：

- src/pulsara_agent/tools/registry.py；
- src/pulsara_agent/tools/__init__.py中的ToolRegistry re-export；
- src/pulsara_agent/ports/tool_registry.py中的ToolRegistryReadPort；
- 仅服务上述registry的unused imports。

理由：

- production没有实例化、导入或调用ToolRegistry；
- Round 9之后唯一真实execution inventory owner是DirectKernelToolPort；
- ToolRegistryReadPort没有实现者或消费者。

必须保留：

- ToolBindingContractBase；
- BuiltinToolBindingContract与CustomToolBindingContract；
- ToolBindingOrigin；
- build_tool_binding_contract；
- tool_binding_contract_identity_fingerprint。

这些仍由builtin catalog和DirectKernelToolPort消费。

### 2.2 旧Tool action classifier execution path

删除 capability/tool_action.py 中没有生产消费者的execution-classifier路径：

- ToolActionClassifier callable alias；
- ToolActionClassifierBinding；
- ToolActionClassifierRegistry；
- ToolActionClassifierContractError；
- default_tool_action_classifier_registry；
- builtin_tool_action_policy；
- mcp_tool_action_policy；
- 仅由dead registry使用的runtime classify helpers；
- primitives/long_horizon.py中的ToolActionClassificationFact。

必须保留：

- LongHorizonToolPolicyFact；
- ToolActionClassifierContractFact；
- fixed_tool_action_policy；
- terminal_process_tool_action_policy；
- terminal_monitor_tool_action_policy；
- terminal_tool_action_policy；
- policy/contract构造所需的pure helpers。

这些仍参与builtin catalog的semantic policy与identity。本轮只删除从未进入production call path的“根据某次ToolCall产生classification fact”分支，不改变当前permission classifier。

当前真实per-call permission路径继续是：

~~~text
DefaultBuiltinToolCallClassifier
  -> tool_permission.py
  -> existing authorize / permission owner
~~~

### 2.3 Fingerprint hard-cut遗留

删除：

- conversation_kernel/context_sources.py中的frozen_non_trigger_context_sources_identity_digest；
- 对应export；
- llm/request.py中的llm_context_fingerprint；
- 对应dead imports或测试引用。

二者都没有生产或测试消费者，也不跨任何真实boundary。不得替换成新digest、registry或兼容wrapper。

必须保留已有真实边界摘要，例如：

- provider wire input plan identity；
- canonical/durable content digest；
- durable replay compatibility；
- provider prefix/source compatibility；
- opaque token MAC；
- canonical stable ID内部所需digest。

### 2.4 无效LLMMessage参数

删除 llm/input.py 的 LLMMessage.user 参数：

~~~text
causal_occurrence_semantic_fingerprint
~~~

该参数当前进入函数后立即被丢弃，且无调用者传入。本轮不保留keyword alias、不发warning、不建立兼容overload。

### 2.5 旧permission DTO

删除：

- PresetPermissionPolicyFact；
- preset_permission_policy_fact；
- 对应export。

必须保留：

- preset_permission_payload；
- current permission parser；
- EffectivePermissionPolicy；
- existing turn/run permission snapshot。

### 2.6 旧runtime message模型

删除：

- message/message.py；
- message package中的Usage、Msg、UserMsg、AssistantMsg、SystemMsg re-export。

必须保留 message/blocks.py及当前生产仍使用的：

- ToolResultState；
- ToolCallState；
- ContentBlock与具体block types。

本轮不得以删除旧message对象为理由改动canonical transcript或provider input model。

### 2.7 无消费者PostgreSQL deadline helper

删除：

- storage/postgres_connection_provider.py中的postgres_operation_deadline；
- 对应export。

当前deadline authority继续由KernelExecutionDeadlineFactory及Host/runner/tool owner持有。不得新建另一套database deadline context manager。

### 2.8 重复Builtin name inventory

删除 tool_runtime.py中的DIRECT_KERNEL_TOOL_NAMES及export。

它只有测试消费者，并且已不能完整表达Plan barrier tools。Architecture test必须改为检查DirectKernelToolPort实际sealed execution-backed inventory，而不是维护第二份名字常量。

### 2.9 Definition/export-only enum

编码开始时再次对以下符号运行production+tests exact consumer audit：

- PromptStatus；
- MemoryQueryDisposition；
- PlanInteractionStatus。

若仍然只有definition和export，则本轮直接删除，不保留deprecated alias。若发现真实production consumer，停止该单项删除并在实施报告中记录consumer；不得通过猜测修改业务状态词汇。

### 2.10 本轮明确不删除

以下项目容易被静态工具误报，本轮保留：

- repository.py canonical facade；
- current MCP config aliases与provider terminal compatibility view；
- retrieval OpenAI-compatible与DashScope backends；
- generated Protocol modules；
- CLI console entrypoint；
- _BUILTIN_TOOL_CATALOG；
- builtin_tool_catalog_entry；
- builtin_tool_catalog与builtin_tool_descriptors inspection views；
- ToolBindingContract及其identity helper；
- LongHorizonToolPolicyFact与ToolActionClassifierContractFact。

builtin catalog inspection views虽然主要由architecture tests消费，但它们是对唯一authoritative catalog的pure read view，不是第二份inventory。

---

## 3. 第二部分：共享Tool contracts移出runner

新增：

~~~text
src/pulsara_agent/conversation_kernel/tool_contracts.py
~~~

从runner.py移动：

- KernelToolResult；
- KernelToolInvocationContext；
- KernelToolAuthorization与KernelToolAuthorizationKind；
- KernelToolPhysicalInvocationError；
- ProcessLocalEffectSettlementToken；
- ProcessLocalEffectSettlementDisposition；
- ProcessLocalEffectSettlementOutcome；
- ProcessLocalEffectSettlementResult；
- KernelToolLiveSink；
- Tool execution所需的窄Protocol。

tool_runtime.py、subagent.py、memory_tools.py、runner.py与tests统一从tool_contracts.py导入。

禁止：

- runner.py兼容re-export；
- tool_contracts.py导入runner.py；
- tool_contracts.py导入repository、Host、provider adapter或concrete DirectKernelToolPort；
- 为移动后的DTO添加fingerprint。

目的不是增加一层抽象，而是终止concrete tool owners反向依赖foreground runner模块。

如一个Protocol同时包含surface planning与physical invocation两类方法，应按真实消费者拆为：

~~~text
ToolSurfacePlanningPort
ToolInvocationPort
~~~

DirectKernelToolPort可以同时结构化实现两者；不得为拆Protocol创建第二个physical owner。

---

## 4. Turn admission coordinator

新增：

~~~text
src/pulsara_agent/conversation_kernel/turn_admission.py
~~~

唯一TurnAdmissionCoordinator接收明确依赖：

- repository；
- KernelSessionIO；
- WriterLease；
- TodoRunAdmissionFinalizer；
- deadline factory。

移动当前runner职责：

- ROOT与SUBAGENT_TASK prepared admission；
- direct FULL fast path；
- ACK-unknown FULL/NONE/CONFLICT confirmation；
- cancellation detach与shielded settlement；
- TODO run activation finalizer；
- canonical failure interruption。

公开结果继续是existing AcceptedEntry或typed canonical conflict。不得增加durable admission receipt、attempt relation或recovery owner。

ConversationKernelRunner只负责：

~~~text
prepared user content
-> admission.accept(...)
-> accepted turn
-> run_accepted_turn(...)
~~~

ROOT与child必须继续使用同一exact admission implementation，差异只通过closed input union表达。

---

## 5. Provider dispatch coordinator

新增：

~~~text
src/pulsara_agent/conversation_kernel/provider_dispatch.py
~~~

该模块拥有从one exact canonical cut到prepared provider dispatch的完整process-local planning：

1. safe-point input handle；
2. canonical dispatch read；
3. exact workspace/scope；
4. Builtin/MCP/Skill owner snapshots；
5. exact model target与native preflight；
6. parent capability dispatch cut与tool/skill sibling views；
7. direct/meta tool surface；
8. non-trigger context sources；
9. memory/steer source composition；
10. cold/append compiler；
11. continuity planning；
12. provider execution preflight与install。

移动：

- PreparedProviderDispatch；
- InstalledProviderOpen；
- prepare provider dispatch；
- canonical reader wrappers；
- provider compatibility/replay target helpers；
- append candidate factory；
- dispatch anchor与canonical frontier helpers；
- install provider open。

ProviderDispatchCoordinator只接收明确ports，不得获得ConversationKernelRunner back-reference或通用service locator。

PreparedProviderDispatch继续完整持有需要在settlement前释放的input handle与surface borrow。所有异常路径必须exact close，不能依赖GC。

### 5.1 Memory dispatch support

新增：

~~~text
src/pulsara_agent/conversation_kernel/memory/dispatch.py
~~~

移动当前runner中的：

- memory source head/presence计算；
- invalidation reservation；
- recall/preference planning reservation；
- memory source application；
- compile fallback；
- memory reflection preparation；
- model-call memory context freeze。

它只能消费MemoryContextProjectionPort与compiler/source values，不拥有memory canonical facts、governance或repository。

### 5.2 Steer consumption

把当前runner中的：

- pending steer hydration；
- prepared steer consume；
- post-consumption Plan conflict；
- resource rejection settlement；

移动到existing steer.py中的SteerConsumptionCoordinator，或在同package建立一个窄steer consumer模块。

它继续使用existing canonical steer DTO和repository transaction；不得建立process-local steer registry或新generation。

---

## 6. Compaction coordinator

新增：

~~~text
src/pulsara_agent/conversation_kernel/compaction/coordinator.py
~~~

移动：

- automatic/manual threshold检查；
- active compaction lane/fence；
- source view、protected tail与summary call；
- retry with smaller tail；
- canonical adoption与ACK confirmation；
- active/idle settlement；
- successor dry dispatch/install；
- retained Skill与runtime handoff；
- idle compact public entrypoint。

它复用：

- ProviderDispatchCoordinator；
- KernelColdEpochInputAssembler；
- HostCompactionRuntimeOwner；
- existing compaction contracts/planner/model call；
- continuity owner。

不得复制Round 9 capability planner、Round 5B cold assembler或provider wire planner。

### 6.1 删除隐式successor槽

删除ConversationKernelRunner._pending_compaction_dispatch。

Compaction coordinator显式返回：

~~~text
CompactionExecutionResult
  canonical outcome
  optional prepared successor dispatch
~~~

完整prepared successor value直接交给当前run loop，不添加fingerprint。Idle compaction没有successor dispatch；active successful adoption可带一个已经安装或待普通open的successor。

取消或失败时，由唯一当前consumer关闭未采用的dispatch/borrow。不得建立pending dispatch registry、receipt或durable handoff。

---

## 7. Tool batch executor

新增：

~~~text
src/pulsara_agent/conversation_kernel/tool_execution.py
~~~

移动：

- ToolResult Live sink；
- ordinary tool authorization；
- permission confirmation；
- attempt admission；
- physical invoke；
- read-only/system-error lowering；
- process-local settlement token；
- canonical ToolResult acceptance与confirmation；
- remote identity publication；
- artifact/memory provenance；
- explicit subagent result tool settlement；
- cancellation-aware known-result join。

ToolBatchExecutor消费：

- ToolInvocationPort；
- repository与KernelSessionIO；
- WriterLease；
- LiveAgentEventBus；
- ToolOutputProcessor；
- continuity scope与installed provider-call facts；
- optional SubagentRuntimePort；
- memory projection/context。

它不得消费ProviderDispatchCoordinator、compiler或Host。

主循环只区分existing真实terminal outcomes：

~~~text
tool batch committed -> continue model loop
explicit subagent result committed -> return terminal KernelRunResult
known error/cancellation -> propagate existing typed failure
~~~

不得为了“统一”创建generic workflow state machine或任意string outcome。

---

## 8. Plan control settlement

把runner._accept_plan_control_batch移动到existing plan_runtime.py中的PlanToolBatchCoordinator。

它明确依赖：

- repository；
- KernelSessionIO；
- WriterLease；
- KernelPlanInteractionCoordinator；
- AutomaticPlanContinuationPort；
- current tool surface borrow；
- exact run permission/canonical facts。

Host继续拥有automatic continuation task。PlanToolBatchCoordinator不得启动Host continuation owner、修改permission mode或创建第二套Plan state。

Runner在一个assistant tool-call batch中发现Plan barrier tool时，把完整batch交给该coordinator，并消费existing AcceptedPlanToolBatch结果。

---

## 9. ConversationKernelRunner最终职责

清理后的runner.py只保留：

- ConversationKernelRunner composition；
- run_turn与run_subagent_turn；
- accepted-turn foreground loop；
- manual/automatic compaction分支选择；
- provider dispatch调用；
- provider stream收集与assistant canonical settlement；
- Plan batch与ordinary Tool batch分派；
- ROOT/child terminal KernelRunResult；
- top-level failure mapping与owner调用顺序；
- 极少量只服务该主循环的private helper。

Runner必须仍然让审阅者在一个文件中读懂：

~~~text
accepted turn
-> maybe compact
-> prepare/install provider call
-> collect/commit assistant
-> terminal or dispatch tool batch
-> repeat
~~~

本文不设runner目标LOC cap。验收标准是authority与依赖闭合，而不是把代码机械压到某个数字。

---

## 10. 禁止实现方式

本轮禁止：

1. RunnerMixin、multiple inheritance或动态method injection；
2. 把self传给coordinator；
3. 通用RunnerContext、service locator或dict形式依赖包；
4. 新旧模块dual import、deprecated alias、compatibility re-export；
5. 两套provider dispatch或compaction coordinator并存；
6. 通过callback任意访问runner私有字段；
7. 为跨函数调用添加DTO fingerprint；
8. 为cleanup新增total turn/tool/task/history cap；
9. 修改provider tools、BASE_SYSTEM或context-source语义；
10. 改变repository transaction、canonical row或event vocabulary；
11. 用skip/xfail、弱化assertion或删除dogfood来获得green；
12. 把已DEFERRED的Hook/Plugin seam顺手加入新模块。

共享process-local state必须继续由existing owner持有。Coordinator只保存其真正拥有的finite in-flight values，不获得新的durable authority。

---

## 11. Architecture tests同步

当前部分architecture tests把正确性绑定到runner.py具体文件位置。本轮必须按新owner更新，而不是删除不变量。

### 11.1 必须调整的测试形状

- “没有turn/model/tool lifetime cap”应扫描runner与新coordinator集合，不能只读runner.py；
- capability registry composition唯一caller应从runner.py更新为provider_dispatch.py；
- activation subject/dispatch anchor helper的唯一owner应更新到provider dispatch；
- process-local asyncio task allowlist应准确加入真正创建task的新模块，并移除已经不创建task的旧模块；
- Plan continuation仍必须只由Host拥有；
- extension/repository import guard应覆盖所有新coordinator；
- tool_runtime不得再导入runner；
- compaction coordinator不得导入Host、subagent manager或concrete provider adapter；
- dead symbols必须由negative architecture assertion防止回流。

### 11.2 禁止测试迁就

不得：

- 把exact caller assertion改成“至少一个”；
- 删除architecture guard而不建立等价新owner检查；
- 因移动模块而放宽import方向；
- 在production保留dead constant只为现有测试；
- 把具体owner检查替换成文本注释检查。

---

## 12. 实施顺序

### C0：删除确认死代码

- §2全部required deletion；
- consumer/import audit；
- targeted tests；
- no compatibility aliases。

### C1：移动Tool contracts

- 新tool_contracts.py；
- production/tests imports hard-cut；
- tool_runtime不再import runner。

### C2：Turn admission

- 新TurnAdmissionCoordinator；
- ROOT/child/ACK/cancel/TODO retained tests。

### C3：Provider dispatch与memory/steer

- 新ProviderDispatchCoordinator；
- memory dispatch support；
- steer consumer；
- Round 3/3.1/8/9/9.1 retained tests。

### C4：Compaction

- 新CompactionCoordinator；
- 删除pending compaction dispatch slot；
- Round 5B active/idle/repeated/mid-turn tests。

### C5：Tool与Plan

- 新ToolBatchExecutor；
- PlanToolBatchCoordinator进入plan_runtime；
- Terminal/MCP/memory/subagent/TODO/Plan retained tests。

### C6：Runner收口

- 删除已迁移方法/import；
- 确认主循环只协调narrow outcomes；
- full tests与real-provider dogfood。

每个slice结束后production只允许存在一条当前路径。可以分commit审阅，但不得用compatibility wrapper连接旧、新实现。

---

## 13. 验证计划

### 13.1 Static

~~~bash
uv run ruff check .
uv run python -m compileall -q src tests tools benchmarks
uv lock --check
git diff --check
~~~

并运行：

- Markdown fence、duplicate heading与local link检查；
- architecture oracle；
- no new skip/xfail；
- secret scan；
- import graph/cycle probe；
- dead symbol negative search；
- Protocol generator retained check。

### 13.2 Targeted pytest

至少覆盖：

- stage2 conversation runner/kernel composition/architecture；
- Round 3 structured compiler；
- Round 3.1 prefix continuity；
- Round 4 Plan；
- Round 5 execution envelope；
- Round 5A.1/5A.2；
- Round 5B compaction；
- Round 6 MCP；
- Round 7/7.1；
- Round 8 memory；
- Round 9/9.1 capability/Skill；
- TODO；
- Round 10 subagent；
- PostgreSQL marker tests；
- fingerprint subtraction architecture。

然后运行全量：

~~~bash
uv run pytest
~~~

不得新增skip/xfail或降低existing assertion。

### 13.3 Local PostgreSQL

可以使用并按需要重置已经验证为本地的disposable PostgreSQL数据库。本文不改变schema；clean-v0 fresh/repeat/deep verification仍必须通过，relation/oracle不得漂移。

### 13.4 Real-provider dogfood

中央runner路径被移动后，至少执行：

~~~bash
uv run python tools/run_round5a2_durable_replay_dogfood.py
uv run python tools/run_round9_capability_dogfood.py
uv run python tools/run_round5b_compaction_dogfood.py
uv run python tools/run_round10_subagent_dogfood.py
~~~

同时运行当前core dogfood suite的真实provider路径。

必须观察实际prompt、model reply、ToolResult、compaction summary、subagent result与失败body；只排除PULSARA_API_KEY值，不要过度脱敏。

Dogfood至少证明：

1. ordinary ROOT text-only turn；
2. multi-tool loop与known ToolResult settlement；
3. permission/Plan retained behavior；
4. cold resume与durable replay；
5. Round 9 direct/meta capability；
6. active与mid-turn compaction；
7. Round 10 DAG、mailbox、queued worker MCP；
8. same-epoch SYSTEM/tools不变、messages suffix-only；
9. process-local cancellation/settlement没有因模块移动而丢失。

不记录per-file、document或evidence SHA。

---

## 14. Definition of Done

全部满足才可把本文标记为ACTIVATED：

1. §2确认的dead production symbols与exports已删除；
2. definition/export-only enum完成最终consumer审计并按§2.9处理；
3. 没有legacy alias、compat wrapper、dual import或dead branch；
4. tool_runtime、memory_tools和subagent不再从runner导入Tool contracts；
5. ConversationKernelRunner不再实现turn admission、provider planning、compaction、Plan settlement或ToolResult settlement细节；
6. provider dispatch、compaction、tool execution、turn admission各有唯一窄coordinator；
7. coordinator之间没有runner back-reference、Mixin或service locator；
8. pending compaction dispatch共享槽已删除，successor用完整typed value显式返回；
9. architecture tests证明新owner与import方向，而不是仅适配文件名；
10. provider prefix、canonical transaction、permission、memory、Plan、TODO、MCP、Skill、compaction与subagent语义完全不变；
11. architecture oracle保持29 / 24 / 11 / 1 / 25 / 0；
12. 没有新fingerprint、cap、durability或recovery machinery；
13. targeted、full pytest、PostgreSQL、static checks全部通过；
14. Round 5A.2、Round 9、Round 5B、Round 10及core真实provider dogfood通过；
15. 没有新增skip/xfail；
16. 实施报告列出删除项、最终模块依赖、测试与真实dogfood结果，但不记录文件SHA。

---

## 15. 最终产品结论

本轮成功后，Pulsara不会获得任何新的用户功能，也不会失去任何已有功能。变化只体现在生产代码ownership更诚实：

~~~text
Runner负责“下一步做什么”
Admission负责“turn是否canonical”
Dispatch负责“这次provider看到什么”
Compaction负责“何时以及如何cold rebase”
Tool executor负责“工具如何执行并settle”
Plan coordinator负责“Plan control batch如何canonical接受”
各existing authority继续拥有自己的truth
~~~

这次清理的价值不是减少文件行数本身，而是让每条happy path、failure path、cancel path和ACK-unknown path都能在一个窄owner内被完整审阅，同时不通过新机制弥补旧机制的复杂度。
