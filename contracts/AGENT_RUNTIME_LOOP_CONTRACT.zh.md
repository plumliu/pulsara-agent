# Agent Runtime Loop Contract

_Created: 2026-07-04_

本文档定义 Pulsara 单次 agent run / tool loop / pending interaction 的核心契约。它是 Host lifecycle、permission、capability、MCP、context compaction、memory hook 等契约的运行骨架。

相关代码：

- [src/pulsara_agent/runtime/agent.py](/Users/plumliu/Desktop/python_workspace/pulsara_agent/src/pulsara_agent/runtime/agent.py)
- [src/pulsara_agent/runtime/state.py](/Users/plumliu/Desktop/python_workspace/pulsara_agent/src/pulsara_agent/runtime/state.py)
- [src/pulsara_agent/runtime/plan.py](/Users/plumliu/Desktop/python_workspace/pulsara_agent/src/pulsara_agent/runtime/plan.py)
- [src/pulsara_agent/runtime/session.py](/Users/plumliu/Desktop/python_workspace/pulsara_agent/src/pulsara_agent/runtime/session.py)
- [src/pulsara_agent/tools/executor.py](/Users/plumliu/Desktop/python_workspace/pulsara_agent/src/pulsara_agent/tools/executor.py)
- [src/pulsara_agent/event/events.py](/Users/plumliu/Desktop/python_workspace/pulsara_agent/src/pulsara_agent/event/events.py)
- [tests/test_agent_runtime_loop.py](/Users/plumliu/Desktop/python_workspace/pulsara_agent/tests/test_agent_runtime_loop.py)
- [tests/test_host_core.py](/Users/plumliu/Desktop/python_workspace/pulsara_agent/tests/test_host_core.py)
- [tests/test_plan_workflow.py](/Users/plumliu/Desktop/python_workspace/pulsara_agent/tests/test_plan_workflow.py)

---

## 1. 核心立场

`AgentRuntime` 负责一次 active run 内的模型调用、工具调用、pending 状态和 run finalization。

它不负责：

- Host session registry；
- workspace terminal supervisor ownership；
- durable resume manifest；
- MCP client lifecycle；
- governance worker lifecycle；
- UI/REPL command parsing。

这些由对应契约和 Host/RuntimeSession owner 负责。

---

## 2. Identity

一次 run 内必须持有三层 event context：

- `run_id`
- `turn_id`
- `reply_id`

所有 `AgentEvent` 必须包含这三个字段。`EventContext` 是构造 typed event 的唯一轻量上下文对象。

`runtime_session_id` 不写在每个 event model 字段里，而由 `RuntimeSession` / event log 存储边界提供。

---

## 3. LoopState

`LoopState` 是 active run 的短生命周期工作缓存，不是 durable truth。

它可以保存：

- model-visible messages；
- pending tool calls；
- pending interaction payload；
- tool results；
- token usage；
- in-run recovery state；
- stop/abort state；
- committed `RunWorkingSet`的process-local execution handles与projection；
- 当前segment的短生命周期状态。

禁止把 `LoopState.scratchpad` 当作 resume / inspect / long-term facts。需要跨 run 或跨进程解释的事实必须写 typed event 或 durable projection。

---

## 4. Run start 顺序

普通 user turn 必须先经过Host `PRE_RUN` boundary：

1. 冻结authority high-water、permission、model target、MCP installation、execution surface与typed current-user fact；
2. 完成preflight compaction及required gates；
3. 通过一个`emit_many()`原子提交`RunStartEvent`与本run首次引用的pending MCP installation audits；
4. FULL commit后安装`CommittedRunExecutionOwner`与initial segment；
5. AgentRuntime只消费`CommittedHostRunEntry`和frozen execution handles，生成typed capability exposure event；
6. 构造immutable context input snapshot/transcript/tool units/candidates，确认input manifest durable后进入model loop。

AgentRuntime不拥有Host `RunStartEvent`写权限，也不允许先创建active `LoopState`再补ledger。Child只通过
`CommittedSubagentRunEntry`进入。

Approval、plan interaction与MCP input-required resume在safe point后必须先提交该safe point产生的pending MCP
installation audit，再将suspended `LoopState`恢复为active。Audit commit失败时不允许model continuation，原pending
interaction保持可重试；streaming resume把已提交audit作为本次stream的前缀事件返回。

Capability descriptor/execution surface必须在RunStart前冻结；context-sensitive projection在RunStart FULL后解析并写typed
`CapabilityExposureResolvedEvent`。Projection与memory candidate可以在compile准备阶段生成，但不得改变已冻结execution surface。

Context input preparation从event-slice读取到manifest candidate形成前必须有stage-aware error boundary。只要ledger结构仍可信，
snapshot、transcript/tool-unit normalization、policy/cache prepare、candidate collection/materialization任一步失败都必须写
`ContextCompiledEvent(status="failed", failure_stage=..., input_failure=...)`，outer context/call/index必须与inner failure一致；
不得只留下RunStart/RunEnd。ledger UNKNOWN/PARTIAL/reconciliation latch时才禁止继续append audit。

Snapshot必须先冻结`candidate_source_selections`与`candidate_authorities`。Selection拥有eligible/selected/omitted、policy与
source range审计；authority只拥有实际model-visible正文/timing/attribution。Subagent selection必须在已冻结parent event slice上
运行pure reducer一次性派生，Agent不得分别读取live selected/count后绑定较晚high-water。随后candidate prepare与compiler allocation都对正文hash、event/artifact refs、
source/channel/lowering矩阵及high-water执行强join。`lowering_kind`是compiler实际lowering依据，不是只供审计展示的闲置字段。
Candidate collector只消费snapshot selection/authority，不接受第二份source字符串。即使selection为空而没有authority，collector
仍必须把no-eligible或omitted-only decision写入prepared set/manifest。Memory
authority以当前run最新ProjectionRequested为基准，只接受其后`projection_id + role + scope`唯一匹配的terminal；Ready从event
重建正文，Failed不生成candidate，terminal缺失/不唯一fail closed，不得复用旧Ready。Subagent authority从同一event slice的
SubagentRunCompleted facts重建正文；两者用真实event created-at/sequence生成timing，不能复用current-user observation time。
Plan revision从当前run最新`PlanExitResolvedEvent(decision="revise")`重建，禁止读取scratchpad feedback。
Runtime context从冻结的environment/timing facts纯渲染；memory hook prompt必须先冻结为versioned static-instruction fact。
Lifecycle prepare后、budget allocation前必须运行纯timing overlay：每个compiled section都产生structured timing metadata，
需要model-visible timing的candidate由overlay生成header；memory/subagent不得统一标成current_turn。

所有context-input同步PostgreSQL I/O由RuntimeSession-owned bounded service执行。caller cancellation或soft timeout不释放真实worker；
Host close必须先bounded drain该owner。static instruction artifact只在首次persist时通过该service写入，随后复用frozen fact；
每次model call不得在event loop同步写PostgreSQL archive。

RuntimeSession拥有的tool-render与candidate-lifecycle cache只负责优化。cache read/write异常必须被隔离为bounded operational
diagnostic；durable ContextCompiled FULL后cache write失败不得阻止model call。candidate lifecycle cache使用bounded LRU，
session summary暴露entry/chars/eviction/oversized-skip而不把它们写成historical semantic fact。Candidate cache read exception的
canonical lifecycle与普通miss相同，不能改变manifest fingerprint；oversized entry必须在LRU mutation前skip。

---

## 5. System prompt composition

每次模型调用的系统上下文必须从immutable `ContextFactSnapshot`与`PreparedContextCandidateSet` fresh compose，不从compaction
summary、`LoopState`或scratchpad恢复。

系统上下文包括：

- base system prompt；
- runtime context block（当前日期、timezone、workspace kind/root、terminal cwd、读写边界提示）；
- memory projection；
- capability catalog prompt；
- active skill prompt；
- plan workflow instruction；
- recovery guidance。

Context compaction 只压缩 downstream trajectory，不压缩系统提示词或 active skill frontmatter/body。

---

## 6. Tool call pipeline

模型返回 tool calls 后，runtime 必须按以下顺序处理：

1. parse tool calls；
2. 去重/规范化同批 call；
3. 取本 run 的 `CapabilityExposurePlan`，缺失时只能用 `CapabilityRuntime.resolve_for_turn()` 重新生成 fallback；
4. 对每个 call 先做 capability exposure access；
5. call-local access deny 只生成该 call 的 tool result，不阻断同批合法 call；
6. workflow control tools 经 exposure access 后交给 workflow plane；
7. 剩余 call 批量进入 `PolicyPermissionGate`；
8. `WAIT_FOR_USER` 生成 pending approval；
9. `DENY` 生成 tool result；
10. `ALLOW` 交给 `ToolExecutor`。

工具不得绕过 capability exposure access。尤其是 approval resume 后的 confirmed pending calls 也必须重新执行 exposure access。

---

## 7. ToolExecutor

`ToolExecutor` 是工具执行与 tool-result events 的边界。

职责：

- emit / record `TOOL_RESULT_START`；
- 调用 sync / async / streaming tool；
- 捕获普通异常并格式化为 `ToolExecutionResult(status=ERROR)`；
- 调用 `ToolResultArtifactService` 处理 artifact；
- emit streaming `TOOL_RESULT_TEXT_DELTA`；
- emit `TOOL_RESULT_END`。

`AsyncTool` 必须通过 `ToolRuntimeContext(runtime_session_id, event_context)` 获得 runtime context。同步工具不得自己开新 event loop 访问 async-only provider，除非它显式通过安全 bridge。

Tool execution suspension 是合法结果类型，不是异常。`ToolExecutionSuspended` 必须返回给 `AgentRuntime` 进入 pending interaction。

---

## 8. Pending states

V1 有三类 waiting user 状态：

- pending approval：`REQUIRE_USER_CONFIRM` + `pending_tool_calls`。
- pending plan interaction：`pending_interaction_kind="plan"`。
- pending MCP input-required：`pending_interaction_kind="mcp_input_required"`。

进入 pending 状态时：

- `state.status = WAITING_USER`；
- `state.stop_reason = RunStopReason.WAITING_USER`；production state不得赋自由字符串；
- run 不 emit terminal `RUN_END`；
- HostSession 必须暴露 typed pending interaction object；
- 后续普通 `run_turn` 必须拒绝，直到 pending 被 resolve/cancel/abort。

Resume pending 后必须回到同一个 `LoopState` 继续，或在 durable resume 里先 repair dangling run；不得在没有上下文的情况下执行旧 pending tool。

---

## 9. Plan workflow

Plan mode 是 workflow 子系统，不是 permission mode 的第五个值。

规则：

- 进入 plan mode 不改写已有 run 的 permission snapshot，也不把 HostSession stored default live mutate 成 read-only。
- `PlanModeEnteredEvent` 必须持久写入；用户手动进入也不能只保存在内存里。
- active plan mode 下禁止用户通过 `:mode bypass-permissions` 等命令绕过 read-only；plan active 时所有新 run 的 snapshot 强制为 `read-only` / `plan_mode`。
- `ask_plan_question` 只允许在 plan active 时使用。
- agent `enter_plan` 是 run-ending workflow control：写入 event + tool result 后 finalize 当前 run，不再 follow-up model call。
- `exit_plan` 产生 pending exit interaction；用户 approve/cancel/force-exit 后退出 plan mode，并恢复 pre-plan stored default for future run。exit_plan 所在 run 仍保持 read-only snapshot，不在当前 run 内放宽。
- `:revise-plan <feedback>` 保持 plan active/read-only，并要求模型重新提交 updated plan + `exit_plan`，不得只输出普通解释。
- `:cancel-plan` 放弃整个 plan workflow，退出 plan mode。
- `PlanModeEnteredEvent.previous_permission_mode/policy` 与 `PlanModeExitedEvent.restored_permission_mode/policy` 必须是 preset mode + `preset_to_policy(mode).to_dict()`；缺失、自定义或不一致为 contract error。

Plan workflow tools 必须先通过 capability exposure access；descriptor 缺失时不得改变 plan state。

---

## 10. MCP input-required

MCP input-required 使用通用 tool suspension seam。

`ToolExecutionSuspended(interaction_kind="mcp_input_required")` 必须：

- 停止当前 tool batch；
- 取消尚未完成的兄弟 tool task；
- 写 `tool_execution_suspended` custom event；
- 设置 `pending_interaction_kind="mcp_input_required"`；
- 由 HostSession 暴露 `PendingMcpInputRequired`，并由supervisor保留exact pending slot lease。

用户 resolution 后，runtime 必须调用 adapter 的 resume path，将 answer 路由回原pending lease对应的
manager/server/request state。Resume前仍重做capability/permission gate；它不能按当前同名tool acquire新slot。
Supervisor safe point只在Host `_run_lock`内安装candidate；worker completion、TTL timer或retry callback不得在active run
中间交换surface。Related binding reconfigure会terminal deny pending request并取消引用该binding identity的child；无关
server变化不影响pending lease。

---

## 11. Context compaction safe points

Agent runtime 内允许的 compact safe point：

- preflight：HostSession 在新 user input 后、run start 前执行；
- mid-turn inline：工具结果完成后、follow-up model call 前执行；
- manual idle：用户显式 `:compact` 且 session idle。

AgentRuntime active loop 不得在 pending approval / pending plan / pending MCP input-required 时 compact。

Mid-turn inline compact 必须只 compact current run 之前的历史 prefix，保留 current run tail。

---

## 12. Run finalization

Run finalization 必须 emit exactly one `RUN_END`，除非 run 已经 finalized。

`RUN_END` 必须携带：

- status；
- stop_reason；
- abort_kind（若有）；
- error_message（若有）。

Memory `on_turn_end` hook 在正常 finalization 前运行；如果 hook 失败，runtime 必须 emit `RUN_ERROR` 并把 run 标记为 failed，而不是吞掉错误。

Abort / host teardown / stop request 必须通过 typed abort state，而不是 scratchpad magic key。

---

## 13. 禁止事项

- 不允许直接把 transient scratchpad 当成 resume/inspect truth。
- 不允许工具执行绕过 `ToolExecutor` 的 result event 边界。
- 不允许 approval resume 后跳过 capability fail-closed。
- 不允许 workflow tools 在 capability descriptor 缺失时执行。
- 不允许 pending 状态下自动 compact。
- 不允许一个 run emit 多个 terminal `RUN_END`。
- 不允许把 plan mode 做成 permission mode 枚举成员。
- 不允许把 MCP input-required 格式化成普通 tool error 后继续。
- 不允许恢复已删除的`mcp_elicitation`/`respond_elicitation`第二套production continuation。

---

## 14. 测试守护

最低测试门槛：

- run start event 顺序包含 capability exposure resolved。
- runtime context prompt 每 turn fresh 注入，且不创建 terminal session。
- runtime context正文只能由本次snapshot的`ContextRuntimeEnvironmentFact + ContextCompileTimingFact`生成；Agent不得另传
  pre-rendered字符串。
- unknown/hidden/unavailable tool call-local deny，不阻断同批合法 tool。
- approval resume 后重新做 capability exposure access。
- workflow control descriptor missing 时 fail-closed。
- plan entered event 用户入口立即持久化。
- active plan mode 下 `set_permission_mode` 被拒。
- plan revise 后模型必须重新 `exit_plan`。
- MCP input-required suspend/resume与exact pending lease路径完整。
- pending approval/plan/MCP 下不触发 auto compact。
- mid-turn compact 保留 current run tail。
- abort / host teardown run finalization exactly-once。

---

## 15. Committed run entry 与 Host interaction continuation

生产 run 只能通过 `CommittedRunEntry` 进入 AgentRuntime：Host 使用已原子提交的
`RunStartEvent.new_run_boundary`，child 使用已提交的 `RunStartEvent.subagent_run_entry`。AgentRuntime 不拥有
`RunStartEvent` 写权限，也不得根据 caller 类型伪造 entry。

一个 durable run 由稳定 `CommittedRunExecutionOwner` 持有；每个 initial/resume activation 使用独立、单调递增的
`RunExecutionSegmentOwner`。Host run 正常进入 `WAITING_USER` 只结束当前 segment，不写 RunEnd；child V1 不支持
WAITING_USER，必须先终结 child ledger，再终结 parent graph。

`CurrentUserMessageFact`是run draft的唯一current-user真源；`UserMsg`的text/id/observed time与
`RunStart.user_input_chars`必须从同一fact派生，不允许并行`user_input`参数。Agent exposure只消费
`AgentRunDraft.frozen_execution_surface`，禁止回读scratchpad或current live wiring。

同步tool的execution ownership以真实worker thread completion为终点。取消awaiting coroutine不得提前释放borrow；
tool-batch driver必须等待shielded thread task收口，Host stop/close在deadline耗尽时保留run/session owner并fail closed，
不得先写RunEnd或释放workspace/binding后让线程继续产生副作用/事件。

approval、plan、MCP input-required 的 live continuation 必须在执行前原子提交：

1. pending MCP installation audits；
2. `CapabilityExposureResolvedEvent(resolution_kind=continuation_*)`；
3. `RunInteractionResumeBoundaryEvent`。

continuation 始终重绑原 RunStart 的 model target 与 permission snapshot，只允许 capability exposure 语义完全复用或
单调收窄。commit FULL 后才可安装新的 segment；commit NONE 保留原 pending/token/lease；partial/unknown 必须 latch，
不得用 bool 压成“未提交”。stop/close 在取消 segment 前必须先 CAS 安装 typed termination intent。
