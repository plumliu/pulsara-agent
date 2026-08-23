# Pulsara durability subtraction：Stage 0/1 实施规格

状态：**DRAFT FOR REVIEW**

适用代码基线：`f752a04439cf18961899ab6345929a59d0d80082`（2026-08-07）

架构真源：[PULSARA_DURABILITY_SUBTRACTION_REASSESSMENT.zh.md](PULSARA_DURABILITY_SUBTRACTION_REASSESSMENT.zh.md)

本稿对应架构文档内容 SHA-256：`a86a6e43f592e9f3db40eddb787f9a67412334637cf892c8781c299a845672ff`。

---

## 1. 文档角色与执行前提

本文件不是第二份架构评审，也不重新讨论目标方案。它只把已经冻结的 Stage 0/1 决策转换成 coding agent 可以逐项实施和验收的施工契约。

规范优先级如下：

1. 架构文档负责产品语义、durability 边界和最终 Stage 2–5 方向；
2. 本规格负责 Stage 0/1 的工作包、文件范围、依赖顺序和验收证据；
3. 当前代码负责描述“现在真实存在什么”，不能用旧实现反向否定已经冻结的目标；
4. 若本规格与架构文档冲突，停止实现并先修订本规格，不允许 coding agent 临场折中。

`MUST`、`MUST NOT`、`SHOULD`均为规范要求。“建议文件”不是授权大范围重构；只有达到对应工作包结果所必需的最小改动才进入 diff。

本架构文档当前仍是 dirty worktree 内容。开始生产代码实施前，必须：

- 将架构文档与本规格放入一个可引用的已提交基线；
- 用实际 commit ID 替换本页的 provisional 文档哈希说明；
- 重新执行 Stage 0 inventory；若代码或文档哈希漂移，不得直接沿用旧报告；
- 先运行 `git status --short`，保存并避开所有用户现有修改；
- 未获得单独授权时，不 stage、commit、push 或改写历史。

## 2. 目标结果

Stage 0 完成后，仓库仍保持当前生产行为，但架构边界已经成为机器可检查的闭合输入：

- 当前 151 类 EventType、producer/consumer、transaction、gate 与目标处置可重复盘点；
- 目标 exact 26 个 Committed、23 个 Live、13 个 subject slot 和 2 个 append guard 不再只是自然语言数字；
- 所有 acceleration、audit、publication、presentation 和 close owner 都有 semantic/derived/physical 分类；
- complete-reset 和旧 physical owner quiesce 有独立 runbook；
- Stage 1 的故障注入先例、指标采集和禁止项已进入 CI。

Stage 1 完成后，仍不切换 canonical authority，但必须得到以下可运行状态：

- 普通 model call 自动 context-input exact audit artifact 写入数为 0；
- audit archive、checkpoint、derived projection、presentation或TUI delivery不能否定已经写入 EventLog 的accepted fact；search surface apply失败不回滚canonical memory success；
- acceleration 没有追平不能成为新的 foreground admission error；
- Host 与 non-Host close 不等待上述 derived owner “成功、FULL、追平或发布完成”，但仍 stop admission、cancel/terminate 并 bounded join physical task；
- DB pool、artifact store、executor、RuntimeSession 等资源只在相关 physical task 确认退出或已失去资源访问能力后释放；
- 当前 EventLog、Protocol v2、Presentation Foundation、event schema v11 和生产 transcript 内容保持兼容；
- 不新增 receipt、repair owner、generation、dual-write、compat reducer 或 target-schema 半成品。

Stage 0/1 的价值是建立一个更小、更安全的 Stage 2 起点。它不是通过给旧机制换名提前实现 Stage 2。

## 3. 范围与非目标

### 3.1 Stage 0 允许范围

Stage 0 只允许：

- 新增 test-only inventory fixture；
- 新增 AST/static architecture gate；
- 新增只读 baseline/measurement 工具；
- 新增故障注入 test harness；
- 编写 complete-reset / old-owner quiesce runbook；
- 修正因上述工具暴露出的文档引用错误。

Stage 0 不得修改 production behavior、storage schema、Protocol、README、migration、event serializer 或 Runtime composition。

### 3.2 Stage 1 允许范围

Stage 1 只允许：

- 删除普通 model call 自动 audit capture/materialization 的 production call path；
- 把明确可重建的 checkpoint/projection/presentation failure 从 foreground semantic gate 降为 bounded operational degradation；
- 用failure test确认现有search surface delivery不回滚canonical memory success，但不修改query/surface contract；
- 把 derived owner close 从“业务成功/追平”改成“physical quiescence”；
- 删除只因 acceleration 没有追平而拒绝 foreground admission 的分支；
- 更新对应 tests、metrics 与默认 composition。

### 3.3 明确非目标

Stage 0/1 `MUST NOT`：

- 创建 Stage 2 relational conversation schema或修改任一 migration；
- 写入 selective `agent_events`、实现 26/23 新类型或改变 schema-v11 historical decoder；
- 停写当前 universal EventLog 的正常 semantic event；
- 删除 `ContextCompiledEvent.audit_expectation`；它仍是当前 v11 compiled DTO 的必填 compact carrier；
- 删除 audit storage/loader/doctor/GC 文件；它们在 Stage 1 只成为 legacy/read-only 或显式离线工具；
- 删除 Presentation Foundation、Protocol v2、checkpoint schema、universal EventLog 或 reducer；
- 实现 Protocol v3、Go client hard cut、`CommittedObservationProjection`、`ReadCanonicalContent` 或 live event bus；
- 删除 durable model stream segment、Oxigraph 或 projection-job graph；这些属于 Stage 2–5；
- 改变 terminal 跨 Host、subagent interruption、memory candidate/governance 等已冻结语义；
- 新增 audit sampling feature、用户开关、可靠 hook、custom event 或 generic durable job；
- 以 background detach 代替 physical join；
- 把所有 `await_delivery=True` 一次性改成 false；
- 把当前所有 publisher subscriber 都宣称为非语义 observer；
- 为了回滚建立旧/new dual authority。

## 4. 冻结基线

### 4.1 当前代码基线

Stage 0 report 必须记录并校验：

| 项目 | 当前基线 |
|---|---:|
| Git commit | `f752a04439cf18961899ab6345929a59d0d80082` |
| universal `EventType` | 151 |
| schema registry version | 11 |
| target 审计分类 | A 39 / B 25 / C 16 / D 71 |
| text-only universal events | 43 |
| one-tool universal events | 83 |
| text steady-state durable write scope | ≥15 |
| one-tool steady-state durable write scope | ≥31 |
| foreground committed reducers | 9 |
| mainline reconciliation latches | 6 |
| Host close await expressions | 45 |
| committed-reducer barrier | 4 |
| normal model-call audit artifacts | 4 |

数字探针用于发现漂移，不允许替代 exact set、producer/consumer 和行为 gate。若当前 HEAD 与本基线不同，Stage 0 必须生成新报告并由架构 review 接受，不能静默更新 golden 数字。

### 4.2 Stage 0 target oracle

Stage 0 要保存目标 oracle，但不得把它注册进 production Runtime。

Committed core exact 为：

| type | subject slot | append guard |
|---|---|---|
| `UserMessageAccepted` | `subject_entry_id` | `HostWriterGuard` |
| `AssistantMessageAccepted` | `subject_entry_id` | `HostWriterGuard` |
| `AssistantToolRequestAccepted` | `subject_entry_id` | `HostWriterGuard` |
| `ToolResultAccepted` | `subject_entry_id` | `HostWriterGuard` |
| `TurnCompleted` | `subject_turn_id` | `HostWriterGuard` |
| `TurnInterrupted` | `subject_turn_id` | `HostWriterGuard` |
| `UserSteerAccepted` | `subject_entry_id` | `HostWriterGuard` |
| `CapabilityDecisionAccepted` | `subject_interaction_decision_id` | `HostWriterGuard` |
| `InteractionDecisionAccepted` | `subject_interaction_decision_id` | `HostWriterGuard` |
| `ToolAttemptAccepted` | `subject_tool_attempt_id` | `HostWriterGuard` |
| `ToolRemoteIdentityPublished` | `subject_tool_attempt_id` | `HostWriterGuard` |
| `PromptQueued` | `subject_queue_item_id` | `HostWriterGuard` |
| `PromptConsumed` | `subject_queue_item_id` | `HostWriterGuard` |
| `PromptCancelled` | `subject_queue_item_id` | `HostWriterGuard` |
| `PromptRejected` | `subject_queue_item_id` | `HostWriterGuard` |
| `CompactionAdopted` | `subject_context_binding_revision_id` | `HostWriterGuard` |
| `SubagentTaskAccepted` | `subject_subagent_task_id` | `HostWriterGuard` |
| `SubagentTaskStatusAccepted` | `subject_subagent_task_id` | `HostWriterGuard` |
| `SubagentMessageAccepted` | `subject_subagent_message_id` | `HostWriterGuard` |
| `SubagentResultAccepted` | `subject_subagent_result_id` | `HostWriterGuard` |
| `JobQueued` | `subject_job_id` | `HostWriterGuard` or `JobAttemptClaimGuard` |
| `JobAttemptAccepted` | `subject_job_attempt_id` | `JobAttemptClaimGuard` |
| `JobTerminalAccepted` | `subject_job_id` | `JobAttemptClaimGuard` |
| `MemoryFactAccepted` | `subject_memory_fact_id` | `JobAttemptClaimGuard` |
| `MemoryFactLifecycleChanged` | `subject_memory_fact_id` | `JobAttemptClaimGuard` |
| `MemoryRelationAccepted` | `subject_memory_relation_id` | `JobAttemptClaimGuard` |

Subject union exact 为：

1. `subject_turn_id`；
2. `subject_entry_id`；
3. `subject_tool_attempt_id`；
4. `subject_job_id`；
5. `subject_job_attempt_id`；
6. `subject_queue_item_id`；
7. `subject_interaction_decision_id`；
8. `subject_context_binding_revision_id`；
9. `subject_subagent_task_id`；
10. `subject_subagent_message_id`；
11. `subject_subagent_result_id`；
12. `subject_memory_fact_id`；
13. `subject_memory_relation_id`。

Live core exact 为：

| family | exact types |
|---|---|
| Text | `TextStart`、`TextDelta`、`TextEnd` |
| Thinking | `ThinkingStart`、`ThinkingDelta`、`ThinkingEnd` |
| Data | `DataStart`、`DataDelta`、`DataEnd` |
| ToolCall | `ToolCallStart`、`ToolCallDelta`、`ToolCallEnd` |
| ToolResult | `ToolResultStart`、`ToolResultDelta`、`ToolResultEnd` |
| Live control | `InteractionOpened`、`InteractionReplaced`、`InteractionClosed` |
| Terminal | `TerminalProcessCompleted`、`TerminalMonitorOpened`、`TerminalMonitorObservation`、`TerminalMonitorClosed` |
| Subagent | `SubagentProgress` |

Formal AgentEvent 总数为 exact 49；独立 `RawProvider*`、custom/free-form extension type、`ToolOutcomeUnknown` committed type均为 0。

这些 target declarations 在 Stage 0 只能存在于 test fixture、spec 或只读工具。任何 production import 它们的行为都属于越过 Stage 2 authority cut。

## 5. 当前关键调用路径

### 5.1 自动 context-input audit

当前普通 model call 的 production 路径是：

~~~text
AgentRuntime
  build compact audit_expectation
  build PreparedContextInputAuditSourceCapture
    -> ProviderInputGenerationCoordinator.bind_optional_context_audit_source
      -> PreparedProviderInputStartBundle
        -> LLMRuntime after ModelStart commit
          -> ContextInputIoService.offer_best_effort_nowait
            -> materialize_captured_context_input_audit
              -> plan + pages + root in artifact archive
~~~

主要入口：

- [runtime/agent.py](src/pulsara_agent/runtime/agent.py#L3680)
- [provider_input/coordinator.py](src/pulsara_agent/runtime/provider_input/coordinator.py#L402)
- [provider_input/planner.py](src/pulsara_agent/runtime/provider_input/planner.py#L180)
- [ports/model_lifecycle.py](src/pulsara_agent/ports/model_lifecycle.py#L50)
- [llm/runtime.py](src/pulsara_agent/llm/runtime.py#L400)
- [context_input/io_service.py](src/pulsara_agent/runtime/context_input/io_service.py#L150)
- [context_input/audit_materializer.py](src/pulsara_agent/runtime/context_input/audit_materializer.py#L1056)

Stage 1 删除的是这条默认 production 连接，不是立即删除所有 legacy audit DTO、reader 或 artifact code。

### 5.2 Publication 与 reducer 混层

[runtime/publisher.py](src/pulsara_agent/runtime/publisher.py#L38) 当前用一个 ordered publisher 串行调用所有 subscriber；`await_delivery=True`时，任一 subscriber exception 可以回传给 writer caller。

[runtime/session.py](src/pulsara_agent/runtime/session.py#L3300) 当前还会：

- 等待 publication future；
- 对 critical publication failure安装全局 `publication_reconciliation_required`；
- 用 `accept_committed_event_result`同时判断 publication、reducer fold、checkpoint handoff；
- 在 foreground safe point等待 committed-reducer repair；
- 让 mutation gate同时观察 canonical corruption与 derived publication/checkpoint状态。

这里的 “subscriber” 不是同一种语义。Stage 0 必须逐项区分：

| 类别 | 示例 | Stage 1 处置 |
|---|---|---|
| 当前 canonical write | EventLog transaction、stable event ID confirmation | 保持 fail closed |
| 当前执行所需 semantic consumer | 没有替代查询路径的 control handoff | 保留，或先建立同一 EventLog 上的无新durability fallback |
| derived acceleration | checkpoint、projection、materialized high-water | failure不得否定accepted fact |
| presentation observer | TUI/Foundation publication、delivery | failure只降级presentation |
| operational observer | Inspector callback、diagnostic、metrics | failure只记录bounded diagnostic |

Coding agent 不得以函数名包含 “publisher” 或 “reducer” 作为分类依据，必须追踪它失败后是否会改变 accepted product truth 或 physical effect。

### 5.3 Checkpoint 与 admission

当前 foreground 可以到达：

- `await_committed_reducer_repair_safe_point()`；
- `TranscriptProjectionCheckpointService.checkpoint_if_needed()`；
- checkpoint barrier pre-commit admission；
- `RuntimeProjectionCheckpointAdmissionBlocked`；
- terminal presentation checkpoint retry/close blocking。

主要入口：

- [authority_materialization/checkpoint_service.py](src/pulsara_agent/runtime/authority_materialization/checkpoint_service.py#L1009)
- [projection_checkpoint_maintenance.py](src/pulsara_agent/runtime/projection_checkpoint_maintenance.py#L293)
- [authority_materialization/account.py](src/pulsara_agent/runtime/authority_materialization/account.py#L596)
- [terminal_presentation/service.py](src/pulsara_agent/runtime/terminal_presentation/service.py#L477)

Stage 1 不删除这些 owner；它要求 cache lag、checkpoint non-FULL或presentation catch-up failure不能作为 foreground product failure。若当前 consumer 没有从 EventLog 重建的安全路径，该 slice 必须停下并报告，不能通过吞异常伪造成功。

### 5.4 Close 与 physical ownership

两个必须同时修改和验证的 close owner是：

- [HostSession.aclose](src/pulsara_agent/host/session.py#L4992)
- [RuntimeSession.teardown_non_host_runtime_session](src/pulsara_agent/runtime/session.py#L6719)

当前 close 还等待 reducer fixed point、mandatory audit、checkpoint FULL、presentation catch-up及多类 durable owner。Stage 1 只删除 derived “业务成功”要求，不追求 await 数量归零。

正确顺序固定为：

~~~text
stop admission
  -> cancel / terminate / close physical input
    -> bounded join: task no longer accesses session resources
      -> release DB pool / artifact store / executor / Runtime object
        -> synchronous close_if_idle assertions
~~~

physical task 在 deadline 内无法退出时，close 仍应明确失败并保留可重试资源；不能返回成功、detach task或新增 teardown generation。

## 6. Stage 0 工作包

### 6.1 S0-A：基线与 dirty-worktree 记录

实现步骤：

1. 记录 `git status --short`、HEAD、Python、uv、PostgreSQL测试可用性；
2. 运行当前 text-only、one-tool probes及静态 owner/await/table/type inventory；
3. 保存命令、退出码与稳定摘要；
4. 不把临时输出、DSN、secret或用户路径写入 fixture；
5. 对与本规格数字不一致的结果开显式 drift section。

验收：

- 同一 commit重复运行得到字节稳定或规范化稳定的结果；
- dirty file没有被覆盖；
- probe不写生产数据库，不启动Oxigraph，不修改migration。

### 6.2 S0-B：machine-readable lifecycle manifest

建议新增：

- `tests/fixtures/durability_subtraction_stage0_manifest.json`；
- `tools/durability_subtraction_inventory.py`；
- `tests/test_durability_subtraction_stage0_architecture.py`。

manifest中的每个当前 EventType 至少包含：

~~~text
current_type
family
producer_symbols
consumer_symbols
write_owner
transaction_boundary
durability
sensitivity
recovery_or_gate_role
target_class = A | B | C | D
target_core_type = nullable
removal_stage
evidence_paths
~~~

约束：

- current type exact set必须来自 `EventType` AST，而不是只检查数量；
- 151 个type必须恰好各出现一次；
- A/B/C/D必须分别为39/25/16/71；
- A候选经过semantic dedup后必须精确映射到本规格26类Committed；
- Live target必须精确映射到23类，`RawProvider*` target为0；
- fixture使用稳定排序，evidence path使用repo-relative路径；
- CI只能compare，不得在test run自动重写golden；
- fixture不得被production Runtime、serializer、migration或code generation import；
- 新增/删除当前EventType只能经architecture review更新manifest，不能只改expected count。

### 6.3 S0-C：consumer、gate 与 close-owner inventory

为每个 publisher subscriber、committed reducer、checkpoint owner、audit owner、presentation owner和search-index projection记录：

~~~text
owner_id / symbol
producer input
durable input truth
output
output是否可重建
failure当前传播路径
是否进入require_mutation_allowed
是否进入run completion
是否进入Host/non-Host close
使用的DB/artifact/executor资源
stop_admission入口
cancel/terminate入口
physical join入口
Stage 1 classification
~~~

分类只能是：

- `canonical_write`；
- `semantic_consumer_no_fallback`；
- `derived_acceleration`；
- `presentation_observer`；
- `operational_observer`；
- `physical_owner_only`。

任何 `semantic_consumer_no_fallback` 都不能直接de-gate。Stage 0 report必须指出建立read/fold fallback的最小方式，或将该项标为Stage 1 blocker。

每个committed reducer注册项还必须闭合标注为 `semantic_required` 或 `derived_best_effort`。只有已证明存在bounded canonical rebuild/read fallback的项才能进入后者；这个分类是process-local Runtime contract，不新增durable identity、receipt或schema。

### 6.4 S0-D：静态 architecture gates

Stage 0 CI至少增加以下检查：

1. normal model path中audit carrier/offer调用点数量有exact baseline；
2. `latch_publication_reconciliation_required`、`RuntimeProjectionCheckpointAdmissionBlocked`、`await_committed_reducer_repair_safe_point`和close drain调用点有exact清单；
3. 所有close相关 task creation、`shield`、`gather`、`wait_for`、`to_thread`和executor operation均能归属一个physical owner；
4. 不允许derived/UI module新增 `require_mutation_allowed`、reconciliation latch或stable receipt；
5. Stage 0 target oracle exact为26/23/13/2，custom、`ToolOutcomeUnknown`和RawProvider target为0；
6. production schema/migration/import graph在Stage 0没有发生改变；
7. Stage 1 implementation后，normal path不得再引用 `PreparedContextInputAuditSourceCapture`、`bind_optional_context_audit_source`或 `offer_best_effort_nowait` audit operation；
8. 禁止通过新增配置把automatic exact audit默认为on；
9. 禁止新的per-owner close deadline；被修改owner必须使用传入的单一absolute deadline；
10. 禁止 background detach、new teardown generation、receipt、repair或checkpoint owner。

这些测试应解析AST或import graph，不依赖脆弱的整段source substring；少量必须检查闭合调用顺序的测试可以使用AST line order。

### 6.5 S0-E：故障 characterization

Stage 0 先固定当前测试入口和注入点，不改变预期生产行为。至少覆盖：

- audit archive put阻塞、抛错、超过deadline；
- checkpoint write/read返回none、timeout、conflict；
- presentation worker抛错、TUI subscriber断开；
- search-index projection lag/failure；
- publisher subscriber exception；
- EventLog transaction none/unknown/conflict；
- close时audit/checkpoint/presentation physical I/O仍在执行；
- waiter cancellation但executor/thread继续运行；
- Host close和non-Host teardown各一次；
- pool/store关闭后捕获late query/write。

每个fixture必须能判断：

- canonical EventLog commit是否已经成功；
- failure属于semantic、derived还是physical；
- resource release发生在physical exit之前还是之后；
- 是否安装了新的reconciliation latch/repair owner。

### 6.6 S0-F：complete-reset / quiesce runbook

建议新增 `DURABILITY_SUBTRACTION_CUTOVER_RUNBOOK.zh.md`，但 Stage 0 只写runbook，不执行reset。

runbook必须列出：

1. stop admission；
2. fence旧Host writer、worker claim、terminal monitor和subagent executor；
3. cancel/join当前进程 physical owner；
4. 对可能仍在外部运行的process/effect交给operator，不导入新Runtime；
5. 清空Pulsara-owned PostgreSQL schema/data；
6. 清空shared blob namespace与derived indexes/presentation state；
7. 从empty store执行Stage 2 migration；
8. rollback只能再次complete reset；
9. 不提供import、cold reader、converter、identity map或reverse projection。

### 6.7 Stage 0 exit gate

Stage 0 只有在以下全部成立时才可声明完整完成：

- production behavior和schema diff为0；
- exact 151 inventory与39/25/16/71分类可重复；
- exact 26/23/13/2 target oracle通过；
- subscriber/gate/close owner全部有分类和resource ownership；
- 没有未分类的 `semantic_consumer_no_fallback` 被安排到Stage 1直接de-gate；
- fault harness可稳定触发每类failure；
- baseline report不含secret或环境专属绝对路径；
- complete-reset/quiesce runbook完成；
- full test suite与ruff通过。

本仓库不发布Stage 0/1中间版本，因此“Stage 0完整完成”不是进入Stage 2的前置条件。若full suite存在失败，必须完整运行并记录，但失败数量本身不构成Stage 2入口gate；handoff只要求每个失败至少被初步分类为retained-safety regression、待Stage 2删除/替换的legacy contract、过时测试契约或环境问题，并有明确disposition。尚未完成最终root-cause proof只阻止当前阶段的“完整完成”声明；已有初步分类和接管路径时，不阻止开始Stage 2规格与dormant implementation。

## 7. Stage 1 行为契约

### 7.1 三种完成语义必须分离

Stage 1代码必须在命名和控制流上区分：

| 语义 | 成功条件 | failure后果 |
|---|---|---|
| canonical acceptance | 当前EventLog transaction成功且stable candidate可确认 | none/unknown/conflict继续fail closed |
| derived delivery | cache/checkpoint/presentation/index更新或observer收到 | operational degraded；不得回滚accepted fact |
| physical quiescence | task退出或已被不可逆撤销全部session resource access | 未满足时close失败且不得释放资源 |

不得再用一个 `drain` 同时表达“追平成功”和“physical task已经退出”。可以拆方法，也可以保留一个方法并返回process-local状态，但不能创建durable receipt、stable identity或新recovery graph。

### 7.2 普通 model call 的audit终态

Stage 1 healthy path固定为：

~~~text
build semantic commit
  -> retain compact audit_expectation for current v11 DTO
    -> commit current ContextCompiled/ModelStart authority
      -> dispatch provider

no PreparedContextInputAuditSourceCapture
no provider bundle audit carrier
no ContextInputIoService audit offer
no plan/page/root archive writes
~~~

必须删除production连接：

- [runtime/agent.py](src/pulsara_agent/runtime/agent.py#L3708)中的normal-path capture构造与bind；
- [provider_input/coordinator.py](src/pulsara_agent/runtime/provider_input/coordinator.py#L402)的optional audit carrier rebind；
- [provider_input/planner.py](src/pulsara_agent/runtime/provider_input/planner.py#L180)和[ports/model_lifecycle.py](src/pulsara_agent/ports/model_lifecycle.py#L50)中的normal-path carrier字段；
- [llm/runtime.py](src/pulsara_agent/llm/runtime.py#L405)中的automatic materialization offer。

必须保留：

- `ContextCompiledEvent.audit_expectation`及其v11 validator；
- 已有artifact的read-only loader、Inspector、doctor和GC；
- audit storage vocabulary不进入EventLog的现有guard；
- audit unavailable/reconstructed loader结果；
- sealed secret拒绝和bounded encoder tests。

Stage 1新产生的normal call仍携带deterministic expected artifact IDs，但它们不证明artifact存在。canonical reconstruction成功时，`RECONSTRUCTED_AUDIT` + `audit_root_missing` 是预期optional read结果，默认不计为incident、GC debt或Runtime degraded；只有显式 `require_exact_audit` 才因缺失而失败。missing与已存在artifact的integrity failure仍必须区分。

Stage 1 不新增capture mode、sampling ratio或用户配置。未来显式debug capture是可选产品，不是本阶段交付物。

### 7.3 Derived failure de-gating

只对Stage 0分类为 `derived_acceleration`、`presentation_observer` 或 `operational_observer` 的consumer执行：

- accepted EventLog write结果立即成为semantic success；
- publication unavailable/exception写入bounded operational diagnostic；
- callback失败不能安装global publication latch；
- `derived_best_effort` reducer失败时quarantine/detach当前instance并记录bounded diagnostic，不安装semantic repair或global latch；初次注册catch-up与commit后的catch-up服从同一分类；
- 纯加速路径上的checkpoint soft lag、non-FULL或timeout只跳过/延后acceleration；
- presentation failure只detach/degrade当前presentation owner；
- 对具备bounded fallback的owner，下一次foreground从EventLog/current authority直接读或执行deterministic in-memory fold，不等待repair receipt。

`semantic_required` reducer失败仍fail closed。search-index surface不纳入上述reducer降级：Stage 1只确认canonical memory success不等待后续surface delivery，并保留现有 `retry_wait` / `dead_letter`；当前recall没有freshness join，因此本阶段不承诺query返回freshness或degraded标记，也不修改query contract。

Stage 1不得：

- 删除EventLog commit confirmation；
- 吞掉payload conflict、stable event ID conflict或unknown commit；
- 对 `semantic_consumer_no_fallback` 只catch `Exception` 后继续；
- 将全部 `required_reducer_ids` 设为空；
- 将所有 `_batch_requires_critical_publication` 类型清零；
- 让tool physical dispatch发生在现有durable gate事实未提交时；
- 改变MCP、compaction或tool control handoff的现有顺序，除非同一改动先提供不新增durability的exact fallback。

### 7.4 Acceleration admission de-gating

Checkpoint gate必须按原因区分，不能按exception class做全局catch：

| 原因分类 | Stage 1契约 |
|---|---|
| soft acceleration lag/pressure | 可skip或degrade，不阻止foreground |
| hard online recovery bound或owner reconciliation | 保留admission fence；只有先证明bounded canonical fallback后才能解除 |
| physical materialization capacity exhausted | 保留effect admission gate；先回收headroom并重新检查capacity，capacity仍为0时不得physical dispatch |

这些名称是审查分类，不要求新增Runtime enum。只有第一类属于本阶段的无条件de-gating；checkpoint corruption或缺少bounded fallback的owner仍是Stage 1 blocker。presentation lag和optional audit缺失可以降级，但不得借此软化EventLog confirmation、tool pre-dispatch durable gate或physical capacity检查。

### 7.5 Close 的physical-safe终态

对audit、checkpoint、projection和presentation owner，close只要求：

1. stop new admission；
2. unregister wakeup/subscription；
3. cancel cooperative coroutine或关闭其独占connection/process input；
4. 等待已开始blocking/thread/process operation在共享absolute deadline内退出；
5. 确认它不再能访问session-owned DB/artifact/executor；
6. 丢弃未完成derived candidate、retry state和delivery intent；
7. 再调用 `close_if_idle`、释放pool/store和Runtime object。

已经结束的worker exception、checkpoint non-FULL、unresolved presentation high-water或delivery缺失不能单独阻塞close。

仍在执行的SQL、artifact write、executor future、thread或process必须继续阻塞资源释放；deadline到期时：

- Host close返回现有typed/retryable close failure；
- session lease/resource保持可重试状态；
- 不声明CLOSED；
- 不detach task；
- 不创建新的teardown generation、reconciliation receipt或background job。

本阶段保留当前 `drain_timeout_seconds=5.0` API默认值，并向被修改的owner传递同一个 `close_deadline`。不得为每个owner重新计算一个完整timeout，从而把总close预算放大。

## 8. Stage 1 工作包与顺序

### 8.1 S1-A：先提交failure tests

在改production code的同一工作分支上先建立：

- automatic audit write counter；
- checkpoint/presentation/search-index failure injector；
- EventLog hard-failure control group；
- Host/non-Host physical I/O barrier；
- resource close order recorder；
- latch/repair owner snapshot。

测试可以先失败，但任何合并单元必须同时包含使其通过的最小production改动；主分支不得长期保留无期限xfail。

### 8.2 S1-B：断开automatic audit production path

执行顺序：

1. 删除 AgentRuntime capture构造；
2. 删除provider bundle audit carrier与rebind方法；
3. 删除LLMRuntime automatic offer；
4. 清理production imports；
5. 保留legacy reader/materializer test；
6. 改写原先断言normal run产生exact root的测试；
7. 增加text/tool model call均为0 archive write，以及default reconstructed / explicit exact-failure的断言。

该工作包可以独立发布和回滚，不依赖Stage 1后续de-gating。

### 8.3 S1-C：解除derived foreground gate

执行顺序：

1. 根据Stage 0 manifest列出所有derived subscriber/reducer；
2. 为committed reducer注册冻结 `semantic_required` / `derived_best_effort` closed classification，并让initial/live catch-up使用同一failure path；
3. 从required delivery/fold集合移除纯derived项，失败时quarantine该instance而非安装global repair/latch；
4. 只在derived caller处取消publication latch传播；
5. 仅把soft checkpoint lag转换为skip/degraded，保留hard recovery与physical capacity gate；
6. presentation/TUI error只记录bounded diagnostic；search-index保留现有surface retry/dead-letter与query contract；
7. 保持semantic consumer、EventLog confirmation和physical effect gate不变。

每删除一个gate，必须在同一diff加入对应故障测试和canonical fallback证明。

### 8.4 S1-D：拆开semantic drain与physical quiesce

优先修改：

- [runtime/session.py](src/pulsara_agent/runtime/session.py#L6407)
- [host/session.py](src/pulsara_agent/host/session.py#L4992)
- [projection_checkpoint_maintenance.py](src/pulsara_agent/runtime/projection_checkpoint_maintenance.py#L966)
- [terminal_presentation/service.py](src/pulsara_agent/runtime/terminal_presentation/service.py#L477)
- [context_input/io_service.py](src/pulsara_agent/runtime/context_input/io_service.py#L250)

要求：

- Host与non-Host使用同一physical quiesce规则；
- worker已经退出后，过去的derived error不阻止close；
- blocking I/O仍在运行时，close不能释放资源；
- waiter cancellation不取消或遗失physical owner；
- close coordinator attempt允许沿用当前Host contract换新token；但已开始的subsystem physical operation必须仍可定位、不得被替换或并发重启；
- Stage 1不新增durable teardown generation或receipt；
- Stage 1不删除这些await；Stage 3删除owner后才归零。

### 8.5 S1-E：回归、测量与gate report

完成全部Stage 1 tests后重新生成：

- normal model call audit artifact count；
- text/one-tool durable write scopes；
- latches与checkpoint admission call sites；
- close await与barrier数量；
- publisher subscriber分类；
- production import graph；
- net production LOC。

Stage 1只强制：

- automatic audit artifact/model call = 0；
- 新增authority/latch/receipt/repair owner = 0；
- derived failure到foreground semantic failure路径 = 0；
- physical task退出前resource release = 0。

transaction、await、LOC其余数字只报告，不为了漂亮数字破坏当前physical safety。

Stage 1允许形成明确标注的partial checkpoint。新增回归不按数量阻止Stage 2主路径，也不要求先修复即将在Stage 2删除的旧owner；但必须调查到足以完成分类。对允许悬挂的失败，gate report至少记录test ID、旧owner/root cause、`delete | replace | repair-in-stage2 | baseline-refresh | environment` disposition、接管它的新invariant/test，以及最迟清除该失败的authority-cut slice。`不修复`不得写成`已通过`，也不得用skip/xfail或删除测试隐藏。

## 9. 文件级改动地图

| 文件/区域 | Stage 1预期 | 禁止事项 |
|---|---|---|
| [runtime/agent.py](src/pulsara_agent/runtime/agent.py) | 删除normal audit capture/bind | 不改目标conversation schema；不软化physical headroom gate |
| [llm/runtime.py](src/pulsara_agent/llm/runtime.py) | 删除automatic audit offer/materializer import | 不改provider stream durability或segment |
| [provider_input/coordinator.py](src/pulsara_agent/runtime/provider_input/coordinator.py) | 删除process-local audit source rebind | 不改provider generation recovery |
| [provider_input/planner.py](src/pulsara_agent/runtime/provider_input/planner.py) | 删除bundle audit carrier | 不改wire ordering contract |
| [ports/model_lifecycle.py](src/pulsara_agent/ports/model_lifecycle.py) | 删除normal audit carrier port字段 | 不创建新Stage 2 port |
| [runtime/context_input](src/pulsara_agent/runtime/context_input) | reader/doctor/GC保留；I/O close保持physical-safe | 不删除legacy artifact schema，不新增sampling feature |
| [runtime/session.py](src/pulsara_agent/runtime/session.py) | reducer按closed classification隔离failure；close语义拆层 | 不全局关闭publication confirmation，不软化EventLog unknown |
| [runtime/publisher.py](src/pulsara_agent/runtime/publisher.py) | 仅做实现Stage 1 isolation所需最小改动 | 不在本阶段实现新bounded LiveAgentEventBus |
| [projection_checkpoint_maintenance.py](src/pulsara_agent/runtime/projection_checkpoint_maintenance.py) | 仅soft lag降级；close只等physical exit | 不删除hard recovery fence、owner/schema或新增repair |
| [terminal_presentation/service.py](src/pulsara_agent/runtime/terminal_presentation/service.py) | worker error不否定run/close semantic success；physical I/O仍join | 不删除Foundation或切Protocol v3 |
| [publication_maintenance.py](src/pulsara_agent/runtime/publication_maintenance.py) | 仅缩窄derived caller reachability | 不删除仍服务semantic control的lease |
| [mandatory_audit.py](src/pulsara_agent/runtime/mandatory_audit.py) | 重新分类后只去除derived completion gate | 不把EventLog canonical write failure改成best-effort |
| [host/session.py](src/pulsara_agent/host/session.py) | 使用shared deadline和physical quiesce顺序 | 不压缩成Stage 3三band，不提前release |
| tests | 新故障矩阵、architecture gates、改写旧exact-audit健康路径 | 不通过删除重要failure test获得绿灯 |

若实现需要修改该表之外的production文件，PR描述必须逐个解释依赖和为什么仍属于Stage 1；“顺手清理”不构成理由。

## 10. 故障与行为验收矩阵

| 场景 | 注入点 | 必须结果 | 禁止结果 |
|---|---|---|---|
| normal text model call | healthy archive | audit plan/page/root write均为0；reply与旧transcript行为不变 | 隐式采样、仍构造capture |
| normal tool model call | healthy archive | 每个model call audit write为0；tool语义不变 | 通过tool路径重新触发audit |
| audit archive unavailable | legacy explicit reader/materializer | normal run不访问archive；legacy tool返回unavailable/typed error | run fail、model不dispatch、global latch |
| normal Stage 1 audit read | missing expected root | canonical reconstruction成功时返回 `RECONSTRUCTED_AUDIT` + `audit_root_missing`；默认视为optional | incident、GC debt、Runtime degraded；静默伪造exact |
| checkpoint write timeout | commit后derived checkpoint | accepted event保留；后续foreground走fallback/skip | rollback、RunError、repair candidate |
| checkpoint read corrupt | acceleration load | 不信任checkpoint，使用canonical EventLog path或显式阻止该Stage 1 slice | 将corrupt cache当canonical、吞错后使用错误state |
| checkpoint pressure admission | soft pressure | foreground继续；checkpoint可skip | `RuntimeProjectionCheckpointAdmissionBlocked`逃出 |
| checkpoint hard recovery bound | `assert_event_admission` | admission fence保留，直到有bounded canonical fallback | catch后继续写入 |
| physical dispatch capacity为0 | `checkpoint_for_admission` / headroom recovery | 回收后重新检查；仍为0则禁止dispatch | 把effect gate当acceleration lag吞掉 |
| presentation worker exception | subscriber callback | run/tool commit成功；presentation degraded/detach | publication latch、run termination |
| TUI disconnect | delivery callback | Runtime继续；可重新attach现有v2数据源 | UI ACK成为semantic gate |
| search-index lag | projection/index handler | canonical memory success不等待surface apply；保留retry/dead-letter和当前query contract | 声称已有freshness join；扩大Stage 1修改query/surface协议 |
| subscriber exception control group | derived subscriber | error只进bounded diagnostic | ordinary mutation被拒绝 |
| EventLog transaction none | writer | 保持原有fail closed/retry contract | 把canonical write当observer failure |
| EventLog ACK unknown/conflict | writer/confirmation | 保持reconciliation/stop mutation | 自动重写、重复commit、soft success |
| Host close during audit/checkpoint I/O | blocking executor | stop admission；resource保持open直到physical exit | close先返回、pool/store先close |
| non-Host teardown duringpresentation I/O | worker/executor | 与Host相同physical顺序 | 新teardown generation或detached task |
| worker已退出但derived结果失败 | completed task result | close可继续，不要求FULL/追平 | 因旧worker_error永久close-blocked |
| physical I/O超close deadline | executor/thread | close明确失败且可重试，资源仍有效 | 标CLOSED、释放资源、后台late query |
| waiter cancelled | shielded physical owner | owner仍可被close定位并join | ownership丢失、第二owner并发 |
| healthy v2 TUI | normal run/reconnect | 可见transcript内容与Stage 0一致 | 提前切v3或改变DTO |

## 11. 测试与验证命令

使用repo root的uv管理`.venv`：

~~~bash
uv run ruff check .

uv run pytest -q \
  tests/test_durability_subtraction_stage0_architecture.py \
  tests/test_agent_runtime_loop.py \
  tests/test_context_input_commit_audit.py \
  tests/test_context_input_io.py \
  tests/test_runtime_event_architecture.py \
  tests/test_runtime_publication_maintenance.py \
  tests/test_terminal_completion_incident_architecture.py \
  tests/test_terminal_presentation_foundation.py \
  tests/test_host_lifecycle_contract.py \
  tests/test_run_reconciliation.py \
  tests/test_long_horizon_checkpoint.py

uv run pytest -q
~~~

完整pytest在Stage 0/1 partial checkpoint中是迁移观测，不是Stage 2入口的零失败gate。报告必须保留精确的passed/failed/skipped/collection error计数及失败node ID；Stage 2 retained-safety gate与各slice gate仍必须独立为绿。若测试无法collection、canonical transaction/confirmation被软化、physical effect safety fence失效或数据出现不可界定的损坏，不得仅以“后续会删除”为由忽略。

若配置了PostgreSQL测试DSN，CI还必须运行全部`postgres` marker；本地缺少数据库可以skip，但Stage 1不能只凭in-memory test宣布完成：

~~~bash
uv run pytest -q -m postgres
~~~

文档与静态产物还需：

~~~bash
git diff --check
rg -n \
  "PreparedContextInputAuditSourceCapture|bind_optional_context_audit_source|context-input-audit-materialize" \
  src/pulsara_agent/runtime src/pulsara_agent/llm src/pulsara_agent/ports
rg -n \
  "latch_publication_reconciliation_required|RuntimeProjectionCheckpointAdmissionBlocked" \
  src/pulsara_agent/runtime
uv run python tools/generate_terminal_protocol_contract.py --check
(cd clients/terminal && go test ./...)
(cd clients/terminal && go vet ./...)
git status --short
~~~

`rg`结果不是简单要求为0：

- audit三个normal-path符号在production call graph必须为0，legacy materializer/type definition可以保留；
- publication/checkpoint符号必须与Stage 0 exact allowlist相等，且无derived failure caller；
- 任何新增命中都需要architecture review。

## 12. 提交与评审切片

推荐按以下独立切片交付：

1. **S0 inventory/gates**：test-only fixture、AST probe、runbook；production diff为0；
2. **S1 automatic audit off**：只断开capture/carrier/offer，独立全绿；
3. **S1 derived de-gating**：按consumer family逐项提交，每项带failure test；
4. **S1 physical close safety**：Host与non-Host同一提交，带blocked I/O test；
5. **S1 gate report**：full suite、PostgreSQL、ruff、metrics与diff review。

不得把Stage 2 schema、Protocol v3或event vocabulary实现在这些切片中。若某切片必须依赖Stage 2才能正确完成，应保留现有gate并把Stage 1标为blocked/partial，不能造compat authority。

每个评审单元必须说明：

- 改了哪个owner；
- 删除了哪个semantic gate；
- canonical fallback是什么；
- physical task由谁stop、cancel、join；
- resource release顺序；
- 新增/删除的latch、receipt、generation、transaction和await数量；
- 对应failure test；
- rollback是否只需回退binary/config。

## 13. 停止条件

出现以下任一情况，coding agent必须停止该工作包并报告，不得自行扩大范围：

- 需要migration、dual-write或新canonical row才能保证正确；
- 无法从当前EventLog或现有canonical store重建某semantic consumer；
- de-gate会允许tool在durable gate事实提交前dispatch；
- 需要改变Protocol/TUI DTO或transcript内容；
- 需要新增stable receipt、repair owner、lease generation或background job；
- physical operation无法在不释放共享资源的情况下cancel/join；
- 测试只能通过吞掉canonical corruption、unknown commit或payload conflict；
- 主架构文档、target 26/23/13/2或complete-reset决策发生漂移；
- dirty worktree与本工作包修改重叠且无法安全保留用户内容。

单纯出现新增pytest失败不属于停止条件。coding agent应先分类而不是顺手修复；属于旧authority且已有Stage 2删除/替换路径的失败可以悬挂。只有无法继续安全构建或验证目标kernel的retained-safety failure，才阻止相关Stage 2 authority activation；它不阻止其他dormant Stage 2切片继续推进。

## 14. Definition of Done

### 14.1 Stage 0 DoD

- exact current inventory和target oracle均进入CI；
- 151类无遗漏、无重复，分类总数闭合；
- 每个producer/consumer/gate/close owner均有证据路径；
- semantic consumer与derived observer不再混写为一个分类；
- failure harness和baseline probe稳定；
- runbook完成但未执行；
- production code/schema/behavior diff为0；
- full tests、ruff、Markdown与link checks通过。

以上是“Stage 0完整完成”的DoD，不是Stage 2入口条件。partial checkpoint必须诚实记录未满足项及其disposition。

### 14.2 Stage 1 DoD

- text、tool与multi-call normal path均不构造audit capture、不调度audit I/O、不写plan/page/root；
- compact v11 `audit_expectation`和legacy reader仍可解码已有数据；正常新调用缺root时默认得到 `RECONSTRUCTED_AUDIT` + `audit_root_missing`，只有显式 `require_exact_audit` 才失败；
- audit missing/unavailable不影响detach/reattach、context reconstruction和新model call；
- derived checkpoint/presentation/observer failure不能改变accepted EventLog fact、run completion或tool result；search surface delivery不回滚canonical memory success；
- soft acceleration lag不能触发foreground admission failure；hard recovery bound与physical effect capacity gate仍fail closed；
- 没有为de-gating新增receipt、repair、generation、job、schema或event；
- EventLog none/unknown/conflict和真正canonical corruption仍fail closed；
- Host/non-Host close只等待derived owner physical exit，不等待其FULL/追平/发布成功；
- pool/store/executor release之前无late task访问；
- physical timeout使close失败且可重试，不伪造CLOSED；
- current Protocol v2/TUI healthy behavior与transcript内容不变，protocol生成物check、Go test与Go vet通过；
- automatic audit artifact/model call指标从4降为0；
- 所有targeted与full tests、PostgreSQL integration和ruff通过；
- Stage 1 gate report明确列出未删除owner与仍保留await，供Stage 3处理。

以上是“Stage 1完整完成”的DoD。由于本hard cut不发布Stage 0/1中间态，可以在未全部满足时冻结partial checkpoint并进入Stage 2；不得把partial写成complete，也不得让已分类的legacy red掩盖retained-safety gate结果。

## 15. Stage 2 handoff

Stage 1完整完成不是冻结`STAGE_2_HARD_CUT_IMPLEMENTATION_SPEC.zh.md`的前置条件。Stage 0/1 partial checkpoint完成一次完整验证、所有失败已分类且retained-safety边界可独立验证后，即可冻结实际代码基线并进入Stage 2。handoff必须包含：

- 新HEAD与clean/known dirty status；
- Stage 0 exact manifest；
- Stage 1 fault matrix结果；
- normal-path audit write=0证据；
- remaining semantic consumers与latches；
- remaining physical owners与close await；
- text/one-tool transaction和event基线；
- complete-reset/quiesce runbook；
- 任何Stage 1 blocker及其为何必须由Stage 2 coherent authority cut解决。
- full pytest精确计数、全部失败node ID与逐项disposition；
- 每个悬挂legacy test将由哪个Stage 2 slice删除或用新契约测试替换。

Stage 2不得假设Stage 1已经删除或de-gate旧owner，也不得假设full suite为绿。仍会反向定义foreground semantic success的legacy reducer/latch必须成为对应authority-cut slice的显式删除/替换义务；physical lifecycle已有的stop/cancel/join safety fence在替代实现通过测试前不得移除。

### 15.1 2026-08-08 partial checkpoint实测

在HEAD `5b7ad9f7ffc8565bc572180b2bde0c81ab64473a`及当前已知dirty worktree上执行未带deselect/skip的`uv run pytest -q`：共2848项，`2843 passed, 3 failed, 2 skipped, 7 warnings`，collection error为0，耗时806.09秒。三个失败的disposition如下：

| test ID | 分类与根因 | disposition | Stage 2接管义务 |
|---|---|---|---|
| `tests/test_durability_subtraction_stage0_architecture.py::test_stage0_frozen_documents_match_recorded_sha256` | 本规格在测试前被有意修订，冻结SHA尚未同步；不是Runtime回归 | `baseline-refresh` | 本次文档定稿后同步SHA并定向验证，不进入Stage 2 debt |
| `tests/test_host_core.py::test_host_terminal_monitor_registration_completion_and_autonomous_delivery` | legacy durable `terminal_notification` reducer的checkpoint monitor account/head join失败，安装全局reconciliation latch并在Host close暴露 | `replace` | terminal authority-cut slice删除durable monitor registration/checkpoint/repair graph，以Host-scoped process-local monitor及physical stop/cancel/join契约测试替换 |
| `tests/test_host_core.py::test_host_terminal_monitor_repeated_progress_without_reregistration` | 与上一项相同的旧owner/root cause；progress与delivery已发生，失败位于close时的reconciliation latch | `replace` | 与上一项在同一terminal authority-cut slice替换，不单独修复旧join协议 |

因此本次完整运行观察到的Runtime失败为2个test、1个legacy failure family；相对上一轮handoff没有新增Runtime failure。文档SHA失败在本节定稿后刷新fixture并定向复核，不改变上述全量运行的原始计数。两个terminal test继续保留为可见red，直至Stage 2同一切片完成旧authority删除与新契约测试替换；它们不阻止Stage 2规格或dormant implementation。
