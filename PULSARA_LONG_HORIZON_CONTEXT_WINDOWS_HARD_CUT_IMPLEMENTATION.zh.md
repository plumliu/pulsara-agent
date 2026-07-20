# Pulsara Long-Horizon Context Windows Hard Cut 实施规格

> 状态：L0A–L5 已完成；本文同时保存最终生产契约与验收记录
>
> 日期：2026-07-13
>
> Pulsara 基线：`781de401`（ResolvedModelCall、Host Run Boundary、MCP Startup、Context Compiler Input Hard Cut 已完成）
>
> 路线输入：`PULSARA_NEXT_FIVE_HARD_CUT_STAGES_PLAN.zh.md`
>
> prior art：`PULSARA_LONG_HORIZON_BUDGET_PRIOR_ART_RESEARCH.zh.md`
>
> 真实故障：`PULSARA_LONG_HORIZON_REAL_REPL_TRAJECTORY_ANALYSIS.zh.md`
>
> **ROAC carrier/rewrite hard cut（2026-07-20）：** 本文早期将runtime observation描述为非user inert system/developer carrier的条款已被
> `PULSARA_RUNTIME_OBSERVATION_AND_AUXILIARY_CONTEXT_PREFIX_CONTINUITY_HARD_CUT_IMPLEMENTATION.zh.md`取代。现行contract使用typed
> `pulsara_runtime_observation` user-wire envelope；runtime request是独立carrier。Long-Horizon rewrite domain包含runtime-observation stable state，必须保护effective
> heads、current run/latest clock、未闭合lifecycle与pending依赖，并通过bounded partition/coverage proof重建provider projection。

> 完成日期：2026-07-14
>
> 最终验收：默认全量 pytest、Ruff、`git diff --check`、PostgreSQL 路径与 architecture/grep gates 全绿；第 25.11
> 节八条 long-horizon real-LLM dogfood 已实现，并以 `PULSARA_RUN_REAL_LLM=1`、
> `PULSARA_RUN_LONG_HORIZON_DOGFOOD=1` 显式执行通过。真实轨迹保存 phase、model/tool counts、projection/window、
> finalization reserve、pairing 与 exact replay 证据，不以最终文本作为唯一验收。
>
> 2026-07-14 最终验证：`uv run pytest -q` 为 `2042 passed, 77 skipped, 160 warnings`；`uv run ruff check src tests`
> 与 `git diff --check` 通过。显式 real dogfood 为 `8 passed`：共享主轨迹实测 5 次 model call、5 次 tool settlement、
> 43,414 个模型可见 tool-result 字符、4 次 projection rewrite、5/5 exact replay；另有 finalization deny→reserved model call、
> parent-bounded subagent 和同 run window generation 1→2 三条独立轨迹。实现期真实轨迹还发现并修复了
> `openai_chat_completions` 缺少 run-frozen runtime-observation carrier，以及 `context_input` eager package initializer 的 clean-import cycle。

---

## 0. 文档目的与最终产品语义

本文把阶段四 **Long-Horizon Context Windows** 从研究方向冻结成可以连续实施、逐 PR 全绿、无需中途重新设计核心 DTO
的生产规格。它同时解决三个彼此相关但不能混成一套计数器的问题：

1. 单次 model call 的 model-visible context 如何始终满足 resolved input budget；
2. 同一个 user run 如何跨多个 bounded context window 继续，而不删除 durable history；
3. 一个 run 跨多次 model/tool call 的累计工作量如何逐步收窄并保留 finalization。

本文不让通用 runtime 判断搜索、抓取或工具输出是否形成了“新证据”。低增益探索由 L5 的 weighted rollout cost、phase
收窄与 finalization reserve 治理；runtime 只可向模型陈述已经发生的调用计数、预算消费、phase 与高置信 exact recurrence，不能据此
发出“继续”“停止”或具体下一步指令。

此外，本文接收阶段三明确移交的性能债务：subagent candidate selection 不再允许在每次 compile/replay 时从 parent ledger
sequence 1 全量 fold；必须迁移为 durable checkpoint + contiguous delta。

实施完成后的用户语义是：

```text
一个 user run
  -> 可以进行很多次 model/tool iteration
  -> 每次 model call 只看一个 bounded、可解释、可 exact replay 的 context projection
  -> 旧 completed observations 先 deterministic 降级/rollup
  -> 仍不够时在同一 run 内关闭旧 window、打开新 window
  -> 累计工作量接近边界时先 warning，再按 action class 限制探索
  -> production model pair在Host接收session/run前已证明finalization reserve可行
  -> finalization-only 仍可写目标文件、读取已有 artifact、做 bounded verification
  -> 至少保留两次 finalization model-call admission
  -> 无论成功还是紧急耗尽，都返回可读 terminal outcome
```

以下行为不再允许：

```text
36,001 chars tool-result aggregate -> fail run
50th model iteration仍允许继续搜索 -> 没有最终回答
同 run context变大 -> 要求用户发送无关消息创建新 run 才能继续
compile/replay -> 从 parent ledger sequence 1 重放全部 subagent graph events
projection pressure -> compiler静默改变旧 observation representation而不写 durable rewrite fact
provider prompt-too-long -> 才开始把 provider 当预算探针
```

### 0.1 真实验收目标

LangChain Docs 真实轨迹必须在原始 `run_id` 内完成：

- 连续 20 次以上 MCP/docs/artifact observations 不因固定 36K aggregate chars 失败；
- runtime 在需要时写 deterministic projection rewrite；
- weighted rollout consumption 触发 warning/restricted/finalization；
- agent 调用 `write_file` 生成 `langchain-getting-started.md`；
- agent 在同一 run 返回最终文本；
- 不需要用户再发送 `hello？` 续命；
- Inspector 能解释每次 model call 使用的 window、projection generation、预算状态、调用计数和中性 rollout status hint。

### 0.2 定义“长程”的边界

Pulsara 的长程任务不是“无限调用工具”，而是：

> 一个用户 run 可以在完整 durable provenance 之上跨多个 bounded model-visible context windows 持续推进；runtime 会压缩
> 投影、监控累计工作、按已冻结 action class 收窄探索，并优先保留可完成任务的 finalization 能力。

---

## 1. 范围冻结

### 1.1 本阶段必须完成

阶段四必须完成：

- durable、versioned、fingerprinted subagent graph reducer contract与可丢弃checkpoint memoization；
- graph semantic source与checkpoint/delta acceleration identity分离；
- checkpoint + bounded contiguous delta 的 live/replay 同算法 selection；
- production bounded bootstrap/hot path与privileged offline full-fold doctor分层；
- required initial context window 与 run entry 原子提交；
- `ContextWindow`、projection generation、rollup、rewrite 与 window compaction typed facts/events；
- aggregate tool-result budget 从固定 chars hard truth 迁移为 call-relative token policy；
- per-observation/I/O/artifact/security chars caps继续保留；
- deterministic cross-observation rollup；
- current-run deterministic micro-compaction；
- pairing-safe current-run LLM compaction；
- run/root-subagent-tree rollout budget与finalization reserve；
- production primary target/summarizer pair静态可行性矩阵与`pulsara config-check`；
- 中性 rollout status hint 与 bounded exact recurrence observation；
- model-call前 safe point orchestration；
- crash/cancel/unknown commit recovery；
- Inspector/replay/contract/architecture/real dogfood完整接线；
- 删除所有正常 production sequence-1 graph full-fold与36K aggregate fatal路径。

### 1.2 本阶段明确不完成

本阶段不完成：

- 完整 ContextSource registry与所有 non-transcript producer ownership迁移；
- Prompt Cache stable prefix lanes；
- provider-native remote context或server-side context editing；
- 自动接续一个已经失败的旧 run；
- 跨 conversation 的 memory summarization/retention policy；
- 通用 evidence ontology、novelty/provenance progress reducer或搜索质量评估器；
- 重设计所有search/scrape/MCP provider的业务返回schema或pagination UX；本章只要求它们通过descriptor action classifier接线；
- 无上界的 semantic dedupe；
- 删除 EventLog raw tool events或raw artifacts；
- 通过扩大模型窗口掩盖低增益循环；
- transport 内部 model fallback。

ContextSource Ownership Hard Cut 和 Prompt Cache 继续位于阶段五、阶段六。本阶段只增加 Long-Horizon 必需的 typed
projection/window attribution，不建立完整 source registry。

### 1.3 依赖顺序

```text
ResolvedModelTarget / ResolvedModelCall
  -> Host Run Boundary Safe Point
  -> Immutable ContextFactSnapshot + normalized units
  -> Long-Horizon Context Windows（本文）
  -> ContextSource Ownership Hard Cut
  -> Prompt Cache
```

本文允许消费 Stage 3 的：

- `ContextFactSnapshotFact`；
- `TranscriptCompileInput`；
- `ToolResultRenderUnit`；
- `ContextSectionCandidate`；
- `ContextCompileInputManifestFact`；
- `TokenEstimatorFact`与同一 process-local estimator binding；
- `RuntimeSession.write_events()/confirm_event_batch()`；
- compaction stable candidate/terminalization owner模式。

---

## 2. 当前代码真值与具体债务

### 2.1 固定 36K 仍是 run-ending production truth

当前 `LoopBudget` 仍包含：

```python
tool_result_context_chars: int = 36_000
tool_result_body_context_chars: int | None = None
tool_result_envelope_context_chars: int = 16_384
prior_tool_result_context_chars: int | None = None
current_tail_tool_result_context_chars: int | None = None
legacy_tool_result_context_chars: int | None = None
```

`resolve_context_compile_policy()` 把它们冻结进 `ToolResultRenderPolicyBasisFact`。`context_input/render.py` 在所有 progressive
degradation 后仍执行：

```text
rendered_total_chars > total_context_chars
  -> diagnostic severity=error
  -> tool_result_total_budget_unsatisfied
  -> ContextCompiledEvent pressure/failed
  -> RunEnd failed(model_error)
```

这条路径与 `ResolvedModelContextBudgetFact.input_budget_tokens` 无关，是本文 L1 必须删除的第二预算真源。

### 2.2 当前已有的 per-result 安全层必须保留

当前系统已经有：

- raw artifact archive threshold与persistence cap；
- adaptive head/tail preview；
- per-tool/per-message/per-envelope chars cap；
- terminal/terminal_process typed essential envelope；
- artifact locator与`artifact_read`；
- tool-call/result pairing与timing；
- final provider payload token validation。

阶段四不重做这些能力。L1 只删除 aggregate chars 的 context truth；per-observation、I/O、artifact persistence、secret、schema
与transport payload hard caps继续存在。

### 2.3 当前 run 仍不可被真正 compact

`RuntimeContextCompactor` 当前冻结：

```text
max_compactable_sequence = current_run_start_sequence - 1
```

因此 mid-turn compaction只能压当前 RunStart之前的历史，不能把当前 run 早期的 completed observations移交给新 window。新用户消息
创建新 run 后，旧 observations变成 prior history，才会偶然得到更激进降级。

### 2.4 Loop budget 仍会突然终止

当前默认：

```python
max_turns = 50
max_tool_calls = 64
```

达到 `max_turns` 时直接 `FAILED/MAX_TURNS`；达到 tool cap 时直接 `TOOL_ERROR_BUDGET`。没有 warning、restricted、
finalization-only，也没有为 synthesis call 预留 admission。

### 2.5 累计 usage 与 active context 已分开，但没有 rollout owner

`ModelCallEndEvent` 已保存：

- `usage_status`；
- reported `ModelTokenUsageFact`或missing；
- `estimated_input_tokens`；
- resolved call/target identity。

但 runtime 没有按 run/root-subagent-tree fold usage 的 durable reducer，也没有使用 cached/non-cached/output不同权重。累计 43.5 万或
120 万 input tokens 只出现在事后 Inspector/SQL，不参与 phase transition。

### 2.6 Stage 3 selection correctness 仍有 sequence-1 成本

当前 subagent candidate selection 从 frozen parent `ContextEventSlice` 纯派生，correctness 已闭环；但没有 durable graph checkpoint
时，source range必须为：

```text
sequence 1 .. source_through_sequence
```

每次 live compile、exact replay、Inspector 都可能读/解码/fold整个 parent ledger。长 session 的 context preparation 成本随 event
数量线性增长。这不是可长期接受的 Long-Horizon 行为。

### 2.7 Stage 3 已提供阶段四的正确 substrate

本文不得退回旧字符串路径。阶段四必须直接消费：

- canonical stored-event bytes；
- immutable snapshot；
- provider-native transcript pairing；
- typed tool-result units；
- event-safe render semantics/timing/artifacts；
- deterministic compiler/manifest fingerprints；
- exact replay。

---

## 3. 核心概念必须分层

### 3.1 三个控制面

| 控制面 | 约束对象 | 单位 | durable owner |
|---|---|---|---|
| active context | 一次model call最终payload | tokens | Context input manifest/compiled event |
| observation projection | 本window中tool observations当前表示 | unit/representation/tokens | projection reducer |
| rollout governance | run/tree累计model/tool工作量 | weighted tokens/tool units/calls | rollout reducer |

三者不能共用一个 `budget_remaining` 字段，也不能由 compiler 一个类任意修改。精确重复调用只作为 rollout
status 的派生观察，不形成第四个控制面，也不拥有独立 reducer。

### 3.2 Durable observation、projection、display 三层

```text
Durable observation
  ToolCall*/ToolResult* events + raw artifact + typed timing/semantics

Model-visible projection
  full | preview | essential | artifact_locator | rollup_member | pair_stub

Display/Inspector
  raw truth + current projection + rewrite reason + historical model-call join
```

Projection rewrite 不修改 durable observation。Inspector 可以读取 raw artifact，但 compiler 只能读取 matching projection所允许的
representation。

### 3.3 Run、window、projection generation 不同

- `run_id`：一个用户任务的 durable lifecycle；
- `window_id`：该 run 的一个 LLM-summary边界；
- `window_generation`：从1开始，只有打开新 window时递增；
- `projection_generation`：同一 window 内每次 deterministic rewrite递增；
- `safe_point_revision`：同一model step每次durable mutation/CAS winner变化后重新freeze authority的revision；
- `compile_attempt_index`：同一 model call 的 context pressure/retry；
- `model_call_index`：run 内真实 agent model call序号。

一次 deterministic body→locator rewrite不打开新 window。一次 LLM compaction必须关闭旧 window并打开下一 window。

### 3.4 Rollout finalization reserve不是context reserve

`ResolvedModelContextBudgetFact.input_budget_tokens` 是单次 payload hard budget，已包含 model safety margin。

finalization reserve 是未来 agent call、window compaction与finalization tool admission的cumulative work reserve。禁止：

```text
input_budget_tokens - finalization_reserve_tokens
```

把两者机械相减。Projection budget只解决本次 payload；rollout coordinator决定是否还允许发起下一次 exploration/finalization call。

### 3.5 Soft、hard、emergency边界

- projection soft target：触发降级/rewrite，不失败；
- call hard input budget：最终 provider payload不可超过；
- rollout warning/restricted/finalization thresholds：改变行为，不立即失败；
- emergency max model/tool calls：程序错误与失控保险丝；
- per-observation/artifact/payload caps：安全边界，仍可hard fail或artifact化。

### 3.6 长程不会弱化 pairing、安全与权限

Long-Horizon 不能通过以下方式“省预算”：

- 删除 assistant tool call却保留result；
- 删除result导致orphan tool call；
- 丢失 pending approval/MCP input-required；
- 展开 continuation 中已撤销capability；
- 省略 latest error/actionable state；
- 读取当前 live supervisor/LoopState补历史事实；
- 删除 raw EventLog/artifact；
- 跳过 final provider validation。

---

## 4. 模块与依赖方向

### 4.1 新模块

```text
src/pulsara_agent/primitives/long_horizon.py

src/pulsara_agent/runtime/long_horizon/
  __init__.py
  types.py
  checkpoint.py
  window.py
  projection.py
  projection_reducer.py
  budget.py
  rollout.py
  compaction.py
  service.py
```

职责：

- `primitives/long_horizon.py`：event-safe DTO、enum、fingerprint helpers；
- `checkpoint.py`：subagent checkpoint artifact/event owner与checkpoint+delta reader；
- `window.py`：window chain planner/reducer；
- `projection.py`：token allocation、rewrite/rollup planner；
- `projection_reducer.py`：纯 projection/window reducer；
- `budget.py`：resolved projection/rollout policy derivation；
- `rollout.py`：usage/account/reservation/phase reducer；
- `compaction.py`：current-window LLM compaction plan/owner；
- `service.py`：model-step safe-point coordinator，不拥有compiler内部实现。

### 4.2 依赖方向

```text
primitives.long_horizon
  <- event schema
  <- context_input snapshot/manifest/compiler
  <- runtime.long_horizon pure reducers/planners
  <- AgentRuntime/HostSession/SubagentRuntime
  <- Inspector/CLI
```

禁止：

- primitives import runtime；
- compiler import AgentRuntime/LoopState/HostSession；
- rollout reducer读取live child runtime；
- checkpoint loader读取process-local graph作为fallback；
- Inspector触发checkpoint/rewrite/compaction repair。

### 4.3 Process-local owners

RuntimeSession/HostSession组合根新增：

```text
SubagentGraphCheckpointService
ContextWindowProjectionStateStore
LongHorizonArtifactWriteService
ContextWindowCompactionService
RolloutBudgetCoordinator
LongHorizonCoordinator
```

其中 reducer stores只持有由durable facts派生的process-local cache；artifact writer、compaction、child reservations才拥有live
task/connection/lease。不得把 `asyncio.Task`、artifact store、manager或callback放入event-safe fact。

`LongHorizonStateStore`的production bootstrap还必须是active-run bounded sparse bootstrap：在同一个ledger snapshot中取得
whole-ledger high-water，但只返回尚无committed `RunEndEvent`的run所拥有的reducer-relevant facts。已经关闭的window/account不得
重新进入process-local maps；它们由canonical EventLog保留，并仅由Inspector或privileged replay按需恢复。后续committed event仍从
whole-ledger high-water的下一sequence连续fold，selection中未返回的历史event视为deterministic no-op。禁止RuntimeSession启动时扫描
全部历史long-horizon facts，或把closed-state LRU当作durable authority。

### 4.4 Settings composition

新增`LongHorizonSettings`作为composition-root配置源，字段覆盖本文checkpoint、allocation、rollout、status hint与compaction默认值。

- 所有字段在代码中有本文冻结的默认值；
- `.env`无需列出任何Long-Horizon变量即可启动；
- deployment可以通过正常settings/env映射覆盖，但必须在run preparation时resolve成required policy facts；
- 运行中的run不读取live settings；
- policy非法在RunStart前fail closed；
- 不提供`PULSARA_LONG_HORIZON_ENABLED`兼容开关；hard cut后production永远使用新协议。

---

## 5. Canonicalization、identity 与 fingerprint

### 5.1 统一 canonical helper

所有新 fact 使用 Stage 3 已冻结的 canonical JSON helper：

- UTF-8；
- sorted keys；
- compact separators；
- finite numbers only；
- enum序列化为稳定value；
- tuple顺序有语义；
- set必须先排序再序列化；
- recursive frozen JSON；
- fingerprint domain tag必须包含contract version。

禁止为 Long-Horizon 再实现第二套 `json.dumps()` 口径。

### 5.2 操作身份与语义身份

- `window_id`、`rewrite_id`、`rollup_id`、`compaction_id` 是每次操作身份；
- `*_semantic_fingerprint` 只覆盖模型可见/状态机语义；
- `*_fact_fingerprint` 额外覆盖owner/event attribution；
- subagent graph semantic source只覆盖按ledger order折叠的graph-domain semantic events、reducer contract与最终state；物理sequence/high-water、
  ledger continuity、checkpoint ID、delta range和rebase只属于acceleration/continuity attribution；
- artifact ID可以确定性生成，但不等于content fingerprint；
- event ID在第一次plan时生成并稳定复用；
- retry不得生成新ID或新payload。

### 5.3 Stable ID规则

V1使用：

```text
initial_window_id(run_id)
next_window_id(run_id, generation, compaction_id)
rollout_account_id(root_runtime_session_id, root_run_id)
window_compaction_id(run_id, source_window_id, compaction_attempt_index,
                     source_projection_generation, source_through_sequence)
window_open_event_id(window_id)
window_close_event_id(window_id)
rewrite_id(window_id, from_generation, source_through_sequence, plan_fingerprint)
rewrite_page_event_id(rewrite_id, page_index)
rollup_id(window_id, projection_generation, rollup_kind,
          ordered_member_set_fingerprint, renderer_contract_fingerprint)
checkpoint_id(runtime_session_id, ledger_through_sequence,
              ledger_continuity_accumulator, graph_semantic_accumulator,
              reducer_contract_fingerprint, graph_state_fingerprint)
checkpoint_artifact_id(checkpoint_id)
window_summary_artifact_id(compaction_id)
rollout_account_open_event_id(account_id)
rollout_reservation_id(account_id, owner_kind, owner_id)
rollout_phase_event_id(account_id, from_phase, to_phase, source_through_sequence, state_before_fingerprint)
rollout_account_close_event_id(account_id, terminal_run_end_event_id)
child_rollout_budget_resolved_event_id(account_id, subagent_run_id,
                                       budget_snapshot_event_id, parent_account_state_fingerprint)
child_rollout_subaccount_close_event_id(child_run_id, terminal_run_end_event_id)
reply_start_event_id(reply_id, resolved_model_call_id)
model_call_start_event_id(resolved_model_call_id)
model_call_end_event_id(resolved_model_call_id)
reply_end_event_id(reply_id, resolved_model_call_id)
```

函数输入必须是event-safe字符串/fingerprint，不得包含Python对象地址、当前进程时间、随机hash seed或filesystem path。

---

## 6. 基础 enum 与 reason code

### 6.1 Window 与 projection

```python
class ContextWindowOpenReason(StrEnum):
    INITIAL_RUN = "initial_run"
    LLM_COMPACTION = "llm_compaction"


class ContextWindowCloseReason(StrEnum):
    LLM_COMPACTION = "llm_compaction"
    RUN_FINISHED = "run_finished"
    RUN_FAILED = "run_failed"
    USER_STOP = "user_stop"
    HOST_TEARDOWN = "host_teardown"
    RECOVERED_INTERRUPTED = "recovered_interrupted"


class ToolObservationRepresentation(StrEnum):
    FULL = "full"
    PREVIEW = "preview"
    ESSENTIAL = "essential"
    ARTIFACT_LOCATOR = "artifact_locator"
    ROLLUP_MEMBER = "rollup_member"
    PAIR_STUB = "pair_stub"


class ProjectionRewriteReason(StrEnum):
    NEW_RESULT_INGESTED = "new_result_ingested"
    SOFT_TARGET_EXCEEDED = "soft_target_exceeded"
    HARD_AVAILABLE_PRESSURE = "hard_available_pressure"
    OLD_COMPLETED_BODY = "old_completed_body"
    REPEATED_OBSERVATION = "repeated_observation"
    ROLLUP_CREATED = "rollup_created"
    FINALIZATION_NARROWING = "finalization_narrowing"
    WINDOW_OPEN_NORMALIZATION = "window_open_normalization"


class LongHorizonPreparationStage(StrEnum):
    CHECKPOINT_RESTORE = "checkpoint_restore"
    STATE_REBUILD = "state_rebuild"
    SETTLEMENT = "settlement"
    PROJECTION_PLANNING = "projection_planning"
    PROJECTION_COMMIT = "projection_commit"
    WINDOW_COMPACTION = "window_compaction"
    ROLLOUT_ADMISSION = "rollout_admission"
    CONTEXT_INPUT = "context_input"
    CONTEXT_COMPILE = "context_compile"
    PRE_SEND_VALIDATION = "pre_send_validation"
```

`cleared` 不进入 enum。最小表示必须是 pairing-safe `pair_stub`。

### 6.2 Rollout

```python
class RolloutPhase(StrEnum):
    EXPLORATION = "exploration"
    WARNING = "warning"
    RESTRICTED = "restricted"
    FINALIZATION_ONLY = "finalization_only"
    EXHAUSTED = "exhausted"
    EMERGENCY_HARD_STOP = "emergency_hard_stop"


class RolloutBudgetBucket(StrEnum):
    EXPLORATION = "exploration"
    FINALIZATION_AGENT = "finalization_agent"
    FINALIZATION_COMPACTION = "finalization_compaction"
    FINALIZATION_TOOL = "finalization_tool"


class RolloutTransitionReason(StrEnum):
    WEIGHTED_TOKEN_THRESHOLD = "weighted_token_threshold"
    TOOL_COST_THRESHOLD = "tool_cost_threshold"
    MODEL_CALL_THRESHOLD = "model_call_threshold"
    EXPLORATION_ADMISSION_UNREACHABLE = "exploration_admission_unreachable"
    EXPLORATION_COMPACTION_ADMISSION_UNREACHABLE = (
        "exploration_compaction_admission_unreachable"
    )
    WINDOW_COMPACTION_UNAVAILABLE = "window_compaction_unavailable"
    EMERGENCY_CIRCUIT_BREAKER = "emergency_circuit_breaker"
```

Phase只能按enum顺序单调前进；restart/replay不能降级。

### 6.3 Tool action class

```python
class LongHorizonActionClass(StrEnum):
    EVIDENCE_ACQUISITION = "evidence_acquisition"
    EVIDENCE_HYDRATION = "evidence_hydration"
    SYNTHESIS_MUTATION = "synthesis_mutation"
    BOUNDED_VERIFICATION = "bounded_verification"
    USER_INTERACTION = "user_interaction"
    PROCESS_CONTROL = "process_control"
    EXTERNAL_ACTION = "external_action"
```

这属于 execution/governance contract，不是 ContextSource ownership。所有model-visible callable descriptor在L5后必须声明允许的action
classes、最大cost与versioned invocation classifier；不能假设一个terminal/process descriptor永远只有一种行为。

### 6.4 Bounded diagnostic

```python
class LongHorizonDiagnosticFact(BaseModel):
    code: str
    message: str
    stage: LongHorizonPreparationStage | None
    attributes: tuple[tuple[str, str | int | float | bool | None], ...]
```

`code`最大96字符、message最大512字符、attributes最多16项并按key排序；不得包含credential、raw tool body或URL query。

### 6.5 稳定错误码

至少冻结：

```text
subagent_checkpoint_artifact_missing
subagent_checkpoint_contract_mismatch
subagent_graph_reducer_contract_mismatch
subagent_checkpoint_delta_non_contiguous
subagent_checkpoint_delta_bound_exceeded
subagent_checkpoint_bootstrap_bound_exceeded
subagent_checkpoint_rebase_unavailable
subagent_checkpoint_state_mismatch
checkpoint_maintenance_session_not_quiescent
checkpoint_maintenance_lock_unavailable
context_window_missing
context_window_chain_conflict
context_projection_generation_conflict
context_projection_non_monotonic
context_projection_source_mismatch
context_rollup_source_conflict
context_projection_soft_target_exceeded
context_projection_minimum_floor_exceeds_hard_available
context_window_compaction_unavailable
context_window_compaction_failed
context_projection_unit_count_exceeded
rollout_phase_commit_unknown
rollout_reserve_exhausted
exploration_admission_unreachable
exploration_compaction_admission_unreachable
subagent_rollout_reservation_unavailable
subagent_batch_rollout_reservation_unavailable
rollout_budget_configuration_infeasible
rollout_budget_feasibility_drift
rollout_emergency_hard_stop
rollout_status_hint_contract_mismatch
long_horizon_preparation_cycle_exceeded
```

错误码使用enum/registry，不允许自由字符串决定控制流。

---

## 7. Run-level Long-Horizon contract

### 7.1 Required run contract

```python
class RolloutReservationReferenceFact(BaseModel):
    owner_runtime_session_id: str
    reservation_id: str
    reservation_event_id: str
    reservation_sequence: int
    reservation_fingerprint: str


class RunLongHorizonContractFact(BaseModel):
    contract_version: Literal["run-long-horizon:v1"]
    rollout_account_id: str
    rollout_account_owner_runtime_session_id: str
    rollout_account_owner_run_id: str
    inherited_rollout_reservation: RolloutReservationReferenceFact | None
    initial_window_id: str
    initial_window_open_event_id: str
    window_policy: LongHorizonContextAllocationPolicyFact
    window_compaction_summarizer_target: ResolvedModelTargetFact
    rollout_policy: RolloutBudgetPolicyFact
    child_rollout_policy: ChildRolloutReservationPolicyFact
    rollout_status_hint_policy: RolloutStatusHintPolicyFact
    subagent_graph_reducer_contract: SubagentGraphReducerContractFact
    contract_fingerprint: str
```

Host root run：

- account owner是自身runtime/run；
- `inherited_rollout_reservation=None`；
- initial window generation=1；
- policy由已resolved run model target派生；summarizer target也在PRE_RUN resolve并冻结；
- subagent graph reducer ID/version/contract fingerprint在RunStart冻结；resume只能rebind同一contract；
- checkpoint cadence、artifact ID与delta长度不进入run contract；
- RunStart与initial window open同一atomic batch。

Child run：

- `rollout_account_id`继承parent root account；
- owner runtime/run指向parent root ledger；
- `inherited_rollout_reservation`必须引用parent ledger已FULL commit的budget reservation；
- child可继承parent声明的summarizer target binding，但必须按**child自身primary target**重新解析并冻结自己的
  `window_policy`；不得复制parent的resolved window thresholds、projection reserve或input budget；
- child RunStart中的`window_compaction_summarizer_target`是child实际可调用的summarizer target。即使它与parent target fingerprint相同，
  child的window policy也仍由child primary target独立派生；
- child自身也有window chain，但usage通过terminal handoff归还parent account。

配置期可行性检查必须枚举所有production可启动的child profile，并验证每一个实际可达的
`(child primary target, child summarizer target)`组合：window policy可解析、summarizer input/output可满足、child rollout额度可容纳至少一次
primary call。Parent pair通过不代表child pair通过。

### 7.2 RunStart hard cut

`RunStartEvent` 新增 required：

```python
long_horizon: RunLongHorizonContractFact
```

Host初始commit：

```text
RuntimeSession.write_events(
  RunStartEvent,
  ContextWindowOpenedEvent(generation=1),
  RolloutBudgetAccountOpenedEvent,
  pending McpCapabilitySnapshotInstalledEvent(s),
  expected_last_sequence=boundary.expected_last_sequence,
)
```

Child初始commit：

```text
child RuntimeSession.write_events(
  child RunStartEvent,
  child ContextWindowOpenedEvent(generation=1),
  expected_last_sequence=0,
)
```

Child batch不能与parent reservation跨ledger原子提交。正确顺序是：parent先atomic commit graph start + reservation；FULL后child
batch写RunStart/window并携带exact reservation reference。child batch NONE时parent写stable start failure并结算reservation；UNKNOWN/
PARTIAL时保留parent reservation与child capacity owner，进入reconciliation。

任一batch NONE时不得创建run owner；FULL后必须安装owner或stable terminalize；UNKNOWN/PARTIAL沿Host Run Boundary/child
four-state contract latch，不能假装没有run。

### 7.3 Run terminal hard cut

每个window open fact预生成 `stable_close_event_id`。RunEnd commit必须原子包含active window close：

```text
ContextWindowClosedEvent(reason=matching run terminal reason)
RolloutBudgetAccountClosedEvent(root) 或 ChildRolloutSubaccountClosedEvent(child)
RunEndEvent
```

Window/account/subaccount close提交失败时RunEnd不得单独confirmed。RunEnd recovery builder必须从active window与rollout reducers取得同一组
stable terminal candidates。root account有active reservation时不得close；必须先settle/cancel或保留run owner。

### 7.4 Resume

Resume不创建新window。它：

1. 从原RunStart rebind primary model target与window summarizer target，任一fingerprint漂移都fail closed；
2. 从ledger重建唯一open window；
3. 重建projection/rollout reducers，并从冻结high-water重派生status hint；
4. 处理pending interaction terminal fact；
5. 在下一model call前进入LongHorizon safe point；
6. 只有LLM compaction FULL commit才切换window。

---

## 8. Context window DTO 与事件

### 8.1 Window fact

```python
class ContextWindowTranscriptBasisFact(BaseModel):
    basis_kind: Literal["initial_run", "window_compaction"]
    run_start_event_id: str
    source_compaction_started_event_id: str | None
    source_compaction_plan_fingerprint: str | None
    source_through_sequence_at_compaction: int | None
    summarized_pair_groups_fingerprint: str | None
    retained_pair_groups_fingerprint: str | None
    basis_fingerprint: str


class ContextWindowFact(BaseModel):
    contract_version: Literal["context-window:v1"]
    window_id: str
    run_id: str
    generation: int
    previous_window_id: str | None
    open_reason: ContextWindowOpenReason
    transcript_basis: ContextWindowTranscriptBasisFact
    source_through_sequence_at_open: int
    resolved_model_target_fingerprint: str
    input_budget_tokens: int
    token_estimator_fingerprint: str
    window_policy_fingerprint: str
    initial_projection_generation: Literal[0]
    initial_projection_unit_count: int
    initial_projection_state_fingerprint: str
    stable_close_event_id: str
    source_compaction_id: str | None
    source_summary_artifact_id: str | None
    source_summary_fingerprint: str | None
    window_semantic_fingerprint: str
    window_fact_fingerprint: str
```

规则：

- generation从1开始连续递增；
- generation=1时previous/compaction/summary全部为空，reason=`initial_run`；
- generation>1时previous、compaction、summary全部必填，reason=`llm_compaction`；
- initial transcript basis只保存run start ID，其余compaction字段为空；
- compacted basis必须精确引用source Started plan，并与Completed/window summary/retained group fingerprints一致；
- target、input budget与RunStart frozen target一致；
- `source_through_sequence_at_open`是plan时已知high-water，不伪造event自身sequence；
- generation 1 baseline包含该transcript basis中全部terminal tool result的最高安全表示；
- generation>1 baseline只包含retained tool groups，并精确继承old projection representation，禁止升级；
- baseline entries可由authority slice + transcript basis + source compaction plan纯重建，unit count/fingerprint必须匹配；
- opened event的真实sequence由event reference提供；
- semantic fingerprint不包含event ID与随机window ID；fact fingerprint包含。

### 8.2 Open/close events

```python
class ContextWindowOpenedEvent(EventBase):
    window: ContextWindowFact
    opening_batch_id: str


class ContextWindowClosedEvent(EventBase):
    window_id: str
    window_generation: int
    close_reason: ContextWindowCloseReason
    final_projection_generation: int
    final_projection_state_fingerprint: str
    source_through_sequence: int
    next_window_id: str | None
    compaction_terminal_event_id: str | None
```

规则：

- opened event ID必须等于run contract或previous compaction预生成ID；
- close event ID必须等于window `stable_close_event_id`；
- LLM compaction close必须有next window与terminal event；
- run terminal close两者必须为空；
- closed window禁止新rewrite/model call；
- 同一run最多一个open window；
- compaction close、compaction completed、next open必须同一atomic batch；
- run terminal close与RunEnd必须同一atomic batch。

### 8.3 Window chain reducer

```python
@dataclass(frozen=True, slots=True)
class ContextWindowChainState:
    run_id: str
    windows: Mapping[str, ContextWindowFact]
    ordered_window_ids: tuple[str, ...]
    active_window_id: str | None
    closed_window_ids: frozenset[str]
    through_sequence: int
    consistent: bool
    diagnostics: tuple[LongHorizonDiagnosticFact, ...]
```

Reducer是纯函数：

```python
def apply_context_window_event(
    state: ContextWindowChainState,
    event: AgentEvent,
) -> ContextWindowChainState: ...
```

它只消费RunStart、window open/close、window compaction terminal、RunEnd。任何chain gap、generation重复、close/open batch attribution
漂移都设置inconsistent并使committed reducer latch。

---
## 9. L0A：durable subagent graph reducer memoization hard cut

### 9.1 Authority 与 acceleration 必须分层

当前`SubagentGraphStateStore`可以从parent ledger重建graph，但Context Compiler Input Hard Cut的canonical slice仍需要从sequence 1
读取完整事实，才能证明result eligibility、delivery/consumption与selection high-water。长程run持续产生tool、child和delivery事实后，这条路径会
线性放大。

L0A引入的checkpoint只能是**可持久化、可丢弃、可重新计算的reducer memoization**：

```text
canonical EventLog + versioned reducer contract
    = 唯一 semantic authority

checkpoint artifact + chosen delta
    = production restore acceleration
```

因此必须同时满足：

- checkpoint调度、checkpoint ID、选择了哪个checkpoint、delta长度都不得改变模型输入的semantic identity；
- reducer contract、graph event count、graph semantic accumulator与最终graph state必须进入semantic identity；物理authority high-water和全ledger
  continuity不得进入semantic fingerprint；
- 删除checkpoint artifact不会删除事实；privileged repair可仅凭EventLog与匹配的版本化reducer重新生成；
- compiler/live replay不得因checkpoint缺失偷偷退回无界full fold。

### 9.2 Reducer contract 是durable semantic contract

仅保存`graph_schema_version`不够。schema不变时，event classifier、transition、invariant或canonical export语义仍可能改变。冻结：

```python
class EventSchemaDomainContractFact(BaseModel):
    event_type: str
    event_schema_version: str
    event_schema_fingerprint: str
    event_domain: Literal["subagent_graph", "non_graph"]
    decoder_contract_fingerprint: str
    domain_contract_fingerprint: str


class SupportedGraphEventContractFact(BaseModel):
    event_type: str
    event_schema_version: str
    event_schema_fingerprint: str
    event_domain_contract_fingerprint: str
    semantic_projection_contract_fingerprint: str
    supported_event_fingerprint: str


class SubagentGraphReducerContractFact(BaseModel):
    schema_version: Literal["subagent_graph_reducer_contract.v1"]
    graph_reducer_id: str
    graph_reducer_version: str
    graph_schema_version: str
    supported_graph_events: tuple[SupportedGraphEventContractFact, ...]
    event_filter_contract_fingerprint: str
    graph_semantic_event_canonicalization_fingerprint: str
    transition_contract_fingerprint: str
    invariant_contract_fingerprint: str
    canonical_state_contract_fingerprint: str
    graph_reducer_contract_fingerprint: str
```

`graph_reducer_contract_fingerprint`由统一canonical JSON helper重算，覆盖除自身之外的全部声明式字段；不hash Python源码、wheel、Git
commit或文件路径。composition root注册process-local reducer binding时必须使：

```text
run-frozen reducer ID/version/contract fingerprint
    == checkpoint reducer ID/version/contract fingerprint
    == registry binding ID/version/contract fingerprint
```

任一不一致均fail closed。同一`(graph_reducer_id, graph_reducer_version)`注册不同contract fingerprint属于registry configuration
conflict。任何会改变相同event prefix所得graph state的修改，必须**同时升级version并改变contract fingerprint**；纯性能重构可以保持二者。
process-local implementation build fingerprint只用于当前进程诊断，不进入event、checkpoint、manifest、selection或replay verdict。

Process-local binding seam冻结为：

```python
@dataclass(frozen=True, slots=True)
class SubagentGraphReducerBinding:
    contract: SubagentGraphReducerContractFact
    implementation_build_fingerprint: str
    empty_state_factory: Callable[[], SubagentGraphState]
    fold_stored_event: Callable[
        [SubagentGraphState, RawStoredEventEnvelope],
        SubagentGraphState,
    ]
    export_canonical_state: Callable[[SubagentGraphState], bytes]
    restore_canonical_state: Callable[[bytes], SubagentGraphState]


class SubagentGraphReducerRegistry:
    def resolve_binding(
        self,
        *,
        reducer_id: str,
        reducer_version: str,
        reducer_contract_fingerprint: str,
    ) -> SubagentGraphReducerBinding: ...
```

Callable只存在于process-local binding，不进入任何Pydantic/event-safe fact。Registry缺失、重复binding或fingerprint不匹配均在读取
checkpoint前fail closed；不得fallback到“当前默认reducer”。

Event serialization registry还必须为每个`(event_type, event_schema_version)`注册不可变的
`EventSchemaDomainContractFact`。V1至少区分`subagent_graph`与`non_graph`：

```python
class EventSchemaDomainRegistry(Protocol):
    def resolve_historical_binding(
        self,
        *,
        event_type: str,
        event_schema_version: str,
        event_schema_fingerprint: str,
        event_domain_contract_fingerprint: str,
    ) -> EventSchemaDomainContractFact: ...
```

Registry必须保留production仍可resume/replay的历史schema bindings；“当前latest domain”不是合法替代。

`domain_contract_fingerprint`由统一canonical JSON helper覆盖该fact除自身外的全部字段，因此也冻结decoder contract；同一
`(event_type, event_schema_version, event_schema_fingerprint)`解析到不同decoder/domain contract属于registry conflict。

- `event_domain="non_graph"`：只参与ledger continuity，不改变graph state；
- `event_domain="subagent_graph"`且event type/schema/fingerprint精确位于`supported_graph_events`：按reducer contract fold；
- `event_domain="subagent_graph"`但当前contract不支持：command planner/emitter在commit前fail closed；历史replay遇到时为
  `contract_mismatch`，绝不silent no-op；
- 未注册/unknown event type仍按EventLog schema contract fail closed，不存在unknown fallback。

`event_domain`是schema属性，不是进程当前registry的一次全局hash：同一个`(event_type, event_schema_version)`一经发布永远不能改domain；要改变
domain必须发布新的event schema version（必要时新的event type），保留旧domain binding供旧run/replay rebind。新增non-graph event不会改变任何既有
graph reducer contract，也不会使旧run因无关的全局registry fingerprint漂移而无法恢复。

每个`supported_graph_events` entry必须按`(event_type, event_schema_version)`唯一、排序，且其
`event_domain_contract_fingerprint`精确resolve为`event_domain="subagent_graph"`；schema fingerprint、semantic projection contract与entry
fingerprint任一不匹配均fail closed。Reducer contract fingerprint只覆盖这些supported graph entries及其fold语义，不覆盖整个serialization
registry。

新增graph-affecting event必须升级reducer version/contract fingerprint，并把它的完整`SupportedGraphEventContractFact`加入新contract，明确旧run
迁移/终结策略。Replay按run-frozen supported entries解析historical domain binding；binding缺失、event schema fingerprint漂移或同一schema被
重新分类均为`contract_mismatch`。已声明non-graph event对graph fold是deterministic no-op；未知graph-domain event绝不no-op。

### 9.2.1 Versioned stored-event envelope 是EventLog边界

Historical binding必须在当前`AgentEvent` Pydantic union反序列化**之前**可用。L0A因此同步hard cut EventLog存储边界：

```python
@dataclass(frozen=True, slots=True)
class RawStoredEventEnvelope:
    stored_envelope_version: Literal["stored-agent-event:v1"]
    event_id: str
    runtime_session_id: str
    run_id: str
    turn_id: str
    reply_id: str
    sequence: int
    created_at_utc: str
    event_type: str
    event_schema_version: str
    event_schema_fingerprint: str
    event_domain_contract_fingerprint: str
    canonical_payload_bytes: bytes
    payload_fingerprint: str
    envelope_fingerprint: str


@dataclass(frozen=True, slots=True)
class RawEventLogReadSnapshot:
    through_sequence: int
    events: tuple[RawStoredEventEnvelope, ...]
    snapshot_fingerprint: str


class EventLog(Protocol):
    def read_raw_range_snapshot(
        self,
        *,
        minimum_sequence: int,
        through_sequence: int | None,
        deadline_monotonic: float | None,
    ) -> RawEventLogReadSnapshot: ...

    def read_raw_events_by_id(
        self,
        event_ids: tuple[str, ...],
        *,
        deadline_monotonic: float | None,
    ) -> tuple[RawStoredEventEnvelope, ...]: ...
```

`read_raw_*`只读取columns/JSON payload、canonicalize bytes并验证wrapper；它绝不调用当前`load_agent_event()`、当前union或latest decoder。
`read_range_snapshot()`可以作为当前schema typed convenience wrapper保留，但checkpoint restore、graph reducer、historical exact replay与stable batch
confirmation必须走raw API。

PostgreSQL `agent_events` hard cut新增required columns：

```text
event_schema_version              TEXT NOT NULL
event_schema_fingerprint          TEXT NOT NULL
event_domain_contract_fingerprint TEXT NOT NULL
```

`event_type`继续单独存储，并与payload中的type及raw envelope逐项一致。Insert时serialization registry先解析exact schema/domain contract，再把三项
identity与payload同一事务写入。Raw read选择全部wrapper columns + payload；JSONB取回后用统一canonical JSON helper生成bytes，不能依赖数据库文本
格式。InMemory EventLog同样以immutable raw envelope为真源，typed iter返回独立decode copy，不能保存/返回原Pydantic对象引用。

现有全局`AGENT_EVENT_SCHEMA_VERSION`不再是历史decoder identity。它可以被删除，或只保留为catalog migration version，但不能写入每行替代
per-event version。项目未上线，L0A采用数据库reset或显式one-shot migration；production reader禁止根据当前class给旧row补造schema identity。
旧row未显式迁移时fail closed，不允许“能被当前union解析就算同schema”。

Per-event schema fingerprint算法冻结为`agent-event-schema-contract:v1`：

1. registry entry显式声明`event_type`与`event_schema_version`；
2. 对绑定event model生成validation JSON Schema，使用固定local `$defs` ref template；
3. 递归移除仅展示用的`title / description / examples / $comment`，保留`type / required / properties / discriminator / const / enum /
   oneOf / anyOf / allOf / additionalProperties / numeric-string-array constraints / default`及全部影响validation的字段；
4. `$defs`按key、object keys按UTF-8 lexical排序；array顺序保持schema语义；finite JSON only；
5. 对
   `(fingerprint_domain, event_type, event_schema_version, normalized_validation_schema)`使用Stage 3 canonical JSON + SHA-256；
6. 同一`(event_type, event_schema_version)`出现不同schema fingerprint是registry configuration conflict；任何会改变accepted payload、required字段、
   normalization或decoded semantic shape的修改都必须同时升级per-event version并改变fingerprint。

Historical decoder registry按`(event_type, event_schema_version, event_schema_fingerprint)`解析binding，并验证
`event_domain_contract_fingerprint`；只有此后才能decode owned typed copy。已持久化checkpoint graph projection可直接消费raw envelope + frozen
schema-specific semantic projector，不强制先转成当前AgentEvent union。

Process-local historical binding seam冻结为：

```python
@dataclass(frozen=True, slots=True)
class HistoricalEventDecoderBinding:
    schema_contract: EventSchemaDomainContractFact
    decoder_contract_fingerprint: str
    implementation_build_fingerprint: str
    decode_owned_payload: Callable[[bytes], object]
    project_graph_semantic_payload: Callable[[bytes], bytes] | None


class HistoricalEventDecoderRegistry(Protocol):
    def resolve_binding(
        self,
        *,
        event_type: str,
        event_schema_version: str,
        event_schema_fingerprint: str,
        event_domain_contract_fingerprint: str,
    ) -> HistoricalEventDecoderBinding: ...
```

Binding的`decoder_contract_fingerprint`必须等于resolved `EventSchemaDomainContractFact.decoder_contract_fingerprint`；它覆盖canonical payload
bytes到owned historical typed object的兼容语义。Graph-domain binding还必须提供与
`SupportedGraphEventContractFact.semantic_projection_contract_fingerprint`精确匹配的semantic projector。Build fingerprint只作当前进程诊断，
不进入row、checkpoint或replay equality。缺binding、重复binding、schema/domain/projector fingerprint不一致均为`contract_mismatch`；不得调用当前
union尝试“碰巧能解析”的fallback。

Stage 3 `FrozenStoredEvent`同步升级为上述schema-aware envelope（或直接由其包装），新增三项schema/domain identity并把它们纳入
`envelope_fingerprint`。`decode_owned()`必须显式接收historical decoder registry；wrapper event ID/type/sequence/created-at、schema identity、payload
fingerprint与decoded payload任一不一致均fail closed。

### 9.3 Semantic source 与 acceleration attribution 拆分

```python
class SubagentGraphSemanticSourceFact(BaseModel):
    schema_version: Literal["subagent_graph_semantic_source.v1"]
    runtime_session_id: str
    graph_event_count: int
    graph_semantic_accumulator: str
    graph_reducer_id: str
    graph_reducer_version: str
    graph_reducer_contract_fingerprint: str
    graph_state_semantic_fingerprint: str
    semantic_source_fingerprint: str


class SubagentGraphAccelerationFact(BaseModel):
    schema_version: Literal["subagent_graph_acceleration.v1"]
    checkpoint_id: str
    checkpoint_materialization_event_id: str
    checkpoint_through_sequence: int
    checkpoint_ledger_continuity_accumulator: str
    delta_from_sequence: int
    delta_through_sequence: int
    delta_count: int
    delta_byte_count: int
    ledger_through_sequence: int
    ledger_continuity_accumulator: str
    acceleration_fingerprint: str
```

不变量：

- `semantic_source_fingerprint`覆盖runtime、graph event count、graph semantic accumulator、reducer ID/version/contract fingerprint与最终
  graph state fingerprint；它不覆盖任何物理sequence、checkpoint、delta或ledger continuity字段；
- `ContextCandidateSourceSelectionFact(source_instance_id="subagent:results")`、candidate semantic fingerprint、Context snapshot semantic
  fingerprint和compiled payload identity只引用
  `SubagentGraphSemanticSourceFact`；
- `SubagentGraphAccelerationFact`只进入manifest的operational audit区、Inspector与性能metrics；不得进入selection/candidate/snapshot/
  input aggregate/provider payload semantic fingerprint；
- manifest root artifact自身的content hash仍覆盖完整audit bytes，但其`semantic_input_fingerprint`必须显式排除acceleration fact；
- manifest builder必须验证`checkpoint_through_sequence <= acceleration.ledger_through_sequence`、
  `delta_from_sequence == checkpoint_through_sequence + 1`、`delta_through_sequence == acceleration.ledger_through_sequence`；
- `ledger_continuity_accumulator`必须由checkpoint continuity base + 全delta重算；恢复出的graph event count/semantic accumulator/state必须与
  semantic source精确相等；
- empty delta表示`delta_from_sequence == delta_through_sequence + 1`且`delta_count == 0`；非空delta的
  `delta_count == delta_through_sequence - delta_from_sequence + 1`；
- checkpoint/event stable ID、delta byte/count与实际snapshot精确一致，不能由caller自报；
- production model step返回时`checkpoint_id`必填。新session bootstrap必须先confirm首个checkpoint，不能把`None`写入manifest再继续compile。

### 9.4 Checkpoint事实

```python
class SubagentGraphCheckpointStateFact(BaseModel):
    schema_version: Literal["subagent_graph_checkpoint.v1"]
    parent_runtime_session_id: str
    checkpoint_id: str
    through_sequence: int
    graph_reducer_id: str
    graph_reducer_version: str
    graph_reducer_contract_fingerprint: str
    graph_schema_version: str
    graph_state_semantic_fingerprint: str
    graph_event_count: int
    graph_semantic_accumulator: str
    ledger_continuity_accumulator: str
    run_count: int
    task_count: int
    result_count: int
    edge_count: int
    delivery_count: int
    consistent: Literal[True]


class SubagentGraphCheckpointArtifactFact(BaseModel):
    artifact_id: str
    media_type: Literal["application/vnd.pulsara.subagent-graph-checkpoint+json"]
    content_sha256: str
    byte_count: int
    semantic_metadata_fingerprint: str
    checkpoint_state: SubagentGraphCheckpointStateFact


class SubagentGraphCheckpointCommittedEvent(EventBase):
    checkpoint: SubagentGraphCheckpointStateFact
    artifact: SubagentGraphCheckpointArtifactFact
```

完整graph payload写入artifact，event只保存bounded identity、counts、hash与artifact reference。checkpoint payload是给定prefix与reducer
contract的确定性materialized view，不保存`previous_checkpoint_id`或创建路径；使用哪个旧checkpoint构建它只属于acceleration diagnostic。

Artifact canonical payload必须：

- 对entity ID排序，对set转为排序tuple，并recursively thaw为JSON-safe值；
- 不包含process-local manager、callable、lease、`MappingProxyType`或完整`applied_event_ids`集合；
- graph state fingerprint由canonical graph state计算；
- 同一checkpoint ID、相同bytes和semantic metadata为幂等成功；不同内容或metadata-only差异为`ArtifactContentConflict`。

`through_sequence >= 1`。checkpoint只能覆盖committed reducer已证明consistent的prefix；inconsistent graph禁止生成checkpoint。

### 9.5 Graph semantic accumulator 与ledger continuity accumulator

必须维护两个不同的accumulator，禁止互相代用。

Graph semantic accumulator只覆盖reducer实际消费的graph-domain events：

```text
graph_acc[0] = SHA256("pulsara-subagent-graph-semantic:v1")
graph_acc[next] = SHA256(
    graph_acc[prev] || event_id || graph_semantic_payload_fingerprint
)
```

`graph_semantic_payload_fingerprint`必须由统一
`canonicalize_graph_semantic_event(event, reducer_contract)` helper重算：保留event type、event ID、run/task/result/edge/delivery等业务字段，
剥离EventLog `sequence`、storage wrapper、publisher状态、checkpoint acceleration attribution和process-local diagnostics。禁止直接复用可能含
sequence的stored-event payload fingerprint。被剥离字段清单及canonicalization version进入reducer contract fingerprint。

这不是按字段名粗暴删除所有`*_sequence`。每种supported graph event必须在contract中声明typed semantic projection：指向canonical event的
reference以`owner_runtime_session_id + event_id + event_type + referenced_semantic_fingerprint`表达，冗余物理sequence只进入fact attribution；
真正有业务语义的有序index/generation仍保留。缺少projection、projection读取未知字段，或除已声明physical attribution差异外的两个不同
business payload意外投影成相同semantic payload时，fail closed并要求升级reducer contract。

Graph events按canonical ledger order进入reducer和accumulator，但hash输入不含物理sequence。Graph reducer可以用sequence验证本次delta顺序，
却不得把物理sequence写入`graph_state_semantic_fingerprint`；若Inspector/fact需要event sequence，必须放在独立attribution projection，不能回流
semantic state。插入、延后或doctor重建checkpoint event，以及插入任意non-graph event，都不能改变`graph_event_count`、
`graph_semantic_accumulator`或graph state semantic fingerprint。

Ledger continuity accumulator覆盖prefix中的**全部canonical stored events**：

```text
ledger_acc[0] = SHA256("pulsara-subagent-graph-ledger-continuity:v1")
ledger_acc[n] = SHA256(
    ledger_acc[n-1] || sequence[n] || event_id[n] || canonical_payload_fingerprint[n]
)
```

Ledger continuity accumulator只进入checkpoint/acceleration/manifest operational audit，用于证明物理delta连续；不得进入selection、candidate、
snapshot或provider payload semantic fingerprint。

Graph reducer对`supported_graph_events`中的exact type/schema contracts执行typed fold。以下已声明non-graph event只扩展ledger continuity，graph fold为deterministic
no-op：

- model/tool/context/memory等非graph event；
- `SubagentGraphCheckpointCommittedEvent`本身；
- historical `EventSchemaDomainContractFact`明确标记为`non_graph`的event。

Graph-domain但不在当前contract allowlist中的未来event不是no-op，必须按第9.2节fail closed。

Checkpoint覆盖`1..through_sequence`，其Committed event通常写在该prefix之后，因此不会把自己计算进自己的accumulator。后续checkpoint若
high-water已经越过旧checkpoint event，则该旧event正常进入ledger continuity accumulator，但不进入graph semantic accumulator。offline
repair在更晚sequence materialize历史prefix同样不改变graph semantic source，因而不存在“checkpoint是否计算进自身”的循环。

Checkpoint是runtime-scoped memoization，state/artifact不保存“由哪个当前run触发创建”的归因。Committed event若底层EventBase要求
EventContext，必须从`through_sequence`对应canonical event的context确定性派生；不得使用调用doctor时的active run或随机maintenance
context改变checkpoint payload。已存在event时offline repair只确认event并修复artifact，不重新生成不同`created_at`的同ID event。

稳定ID冻结为：

```text
checkpoint_id = stable_id(
    "subagent_graph_checkpoint:v1",
    parent_runtime_session_id,
    through_sequence,
    ledger_continuity_accumulator,
    graph_reducer_id,
    graph_reducer_version,
    graph_reducer_contract_fingerprint,
    graph_state_semantic_fingerprint,
)

artifact_id = stable_id("subagent_graph_checkpoint_artifact:v1", checkpoint_id)
event_id = stable_id("subagent_graph_checkpoint_committed:v1", checkpoint_id)
```

同一物理ledger prefix、同一reducer contract重试必须生成同一三元组。若offline doctor在更晚sequence为同一个graph semantic state创建更近
checkpoint，它会拥有不同operational checkpoint ID，但不得改变graph semantic source。wall-clock、writer generation、选择的base checkpoint
和随机UUID均不参与ID。

### 9.6 原子checkpoint + delta读取协议

```python
class SubagentGraphCheckpointReadPort(Protocol):
    def read_checkpoint_and_delta_snapshot(
        self,
        *,
        runtime_session_id: str,
        requested_through_sequence: int,
        reducer_contract: SubagentGraphReducerContractFact,
        preferred_checkpoint_id: str | None,
        max_delta_events: int,
        max_delta_bytes: int,
        max_checkpoint_candidates: int,
    ) -> SubagentGraphCheckpointReadResult: ...


@dataclass(frozen=True, slots=True)
class SubagentGraphCheckpointDeltaSnapshot:
    requested_through_sequence: int
    checkpoint_event: RawStoredEventEnvelope
    checkpoint_payload_bytes: bytes
    checkpoint_materialization_sequence: int
    delta_events: tuple[RawStoredEventEnvelope, ...]
    ledger_high_water_observed: int
    preferred_checkpoint_id: str | None
    selected_checkpoint_id: str
    rebased: bool


class SubagentGraphCheckpointReadUnavailable(BaseModel):
    runtime_session_id: str
    requested_through_sequence: int
    reason_code: Literal[
        "no_confirmed_checkpoint",
        "no_compatible_artifact",
        "delta_bound_exceeded",
        "reducer_contract_mismatch",
    ]
    confirmed_checkpoint_count: int
    contract_compatible_checkpoint_count: int
    readable_artifact_count: int
    nearest_compatible_checkpoint_id: str | None
    nearest_compatible_checkpoint_through_sequence: int | None


SubagentGraphCheckpointReadResult = (
    SubagentGraphCheckpointDeltaSnapshot
    | SubagentGraphCheckpointReadUnavailable
)
```

读取分为两个有界阶段。第一阶段在一个repeatable-read snapshot中只读取checkpoint catalog metadata，并在shared maintenance lease内确认
candidate artifact；第二阶段只为最终选中的candidate读取一次exact immutable suffix。两个阶段都冻结相同
`requested_through_sequence`、reducer contract与selected checkpoint raw-envelope identity；由于EventLog append-only，第二阶段发现checkpoint
identity漂移、suffix不连续或目标prefix尚未提交时fail closed，不得换用另一个未审计prefix。

1. Candidate可接受条件是：reducer contract匹配、artifact可确认、`through_sequence <= requested_through_sequence`，且到requested high-water
   的delta events/bytes均在bound内；
2. catalog阶段按`checkpoint.through_sequence`降序选择newest viable candidate；`preferred_checkpoint_id`用于rebase审计，不得迫使系统读取更长
   suffix；仅“可读”但delta超bound不算可接受；
3. catalog阶段可枚举bounded个compatible metadata/artifact identity，但不得为每个candidate读取重叠delta；
4. checkpoint的materialization event sequence可以晚于`requested_through_sequence`，因为它是acceleration catalog，不是authority prefix；
5. 读取`checkpoint.through_sequence + 1 .. requested_through_sequence`的全部canonical events；
6. 验证event sequence连续，无gap、duplicate、out-of-order，且events/bytes均在bound内；
7. 返回第二阶段snapshot观察到的ledger high-water；它可以高于requested prefix，但不能改变本次authority range；
8. checkpoint artifact hash、semantic metadata、event fact、decoded payload与reducer contract必须一致。

Checkpoint event与delta rows全部以`RawStoredEventEnvelope`返回；candidate compatibility先比较row中的schema/domain identity，再调用historical
projector/fold binding。该port禁止内部调用current `load_agent_event()`后再包装成raw bytes。

只有`reason_code="no_confirmed_checkpoint"`且该runtime从未有compatible checkpoint catalog record时可以进入bounded bootstrap。
`no_compatible_artifact`不能伪装成新session；`reducer_contract_mismatch`映射`contract_mismatch`；`delta_bound_exceeded`交给offline doctor。

Catalog选择与delta读取可以使用两个bounded transaction，但第二阶段必须重新精确确认selected checkpoint raw envelope，并只读取一次
`selected.through_sequence + 1 .. requested_through_sequence`；不得在第二阶段悄悄改选candidate。InMemory EventLog同样返回canonical deep
copy，不得返回stored object引用。artifact store若与EventLog不在同一物理transaction，reader必须在同一shared maintenance lease与逻辑attempt中
确认immutable artifact identity；read-absent映射为rebase unavailable，不得解释成ledger corruption。

### 9.7 Production hot path、bounded bootstrap与pure restore

```python
def restore_subagent_graph_from_checkpoint(
    *,
    snapshot: SubagentGraphCheckpointDeltaSnapshot,
    reducer_binding: SubagentGraphReducerBinding,
) -> tuple[
    SubagentGraphState,
    SubagentGraphSemanticSourceFact,
    SubagentGraphAccelerationFact,
]: ...
```

Production live compile与live exact replay只有两条合法路径：

1. confirmed compatible checkpoint + bounded contiguous delta；
2. **仅新session/尚无任何compatible checkpoint**时，在bootstrap event/byte cap内从sequence 1 fold，立即写并FULL confirm首个
   checkpoint，然后用该checkpoint（允许empty delta）继续本次model-step preparation。

禁止“本次先full fold继续compile、以后再补checkpoint”。无checkpoint且prefix超过bootstrap cap、所有compatible checkpoint artifact
均缺失、或最早可用checkpoint到source high-water的delta超bound时，model-step preparation fail closed，reason分别为：

```text
subagent_checkpoint_bootstrap_bound_exceeded
subagent_checkpoint_rebase_unavailable
subagent_checkpoint_delta_bound_exceeded
```

Restore必须验证：

- process-local binding与run/checkpoint reducer ID、version、contract fingerprint精确一致；
- checkpoint ledger continuity accumulator对应`1..checkpoint.through_sequence`；
- delta中的每个canonical event都扩展ledger continuity；只有supported graph-domain event同时扩展graph semantic accumulator并改变graph
  state；declared non-graph event只做continuity no-op；unsupported graph-domain event立即contract mismatch；
- 最终through sequence等于请求值；
- 最终graph state consistent；
- 最终graph event count与graph semantic accumulator按第9.5节重算；
- 最终semantic source fingerprint按第9.3节重算。

Delta gap、duplicate、out-of-order或reducer产生inconsistent state属于`ledger_untrusted`；连续ledger下contract/binding或最终semantic
fingerprint不匹配属于`contract_mismatch`；仅缺少可用acceleration artifact属于`artifact_missing`/稳定rebase reason，不得误报ledger损坏。

### 9.8 Privileged offline verify/repair

显式提供与production runtime分离的doctor API/CLI：

```python
class SubagentGraphCheckpointRepairOutcome(StrEnum):
    VERIFIED = "verified"
    REBUILT = "rebuilt"
    REDUCER_BINDING_UNAVAILABLE = "reducer_binding_unavailable"
    LEDGER_UNTRUSTED = "ledger_untrusted"
    ARTIFACT_CONFLICT = "artifact_conflict"


class SubagentGraphCheckpointRepairReport(BaseModel):
    runtime_session_id: str
    through_sequence: int
    graph_reducer_id: str
    graph_reducer_version: str
    graph_reducer_contract_fingerprint: str
    graph_event_count: int | None
    graph_semantic_accumulator: str | None
    ledger_continuity_accumulator: str | None
    graph_state_semantic_fingerprint: str | None
    checkpoint_id: str | None
    checkpoint_artifact_id: str | None
    scanned_event_count: int
    first_inconsistent_sequence: int | None
    outcome: SubagentGraphCheckpointRepairOutcome
    diagnostics: tuple[LongHorizonDiagnosticFact, ...]


def verify_or_rebuild_subagent_graph_checkpoint(
    *,
    runtime_session_id: str,
    through_sequence: int,
    reducer_contract: SubagentGraphReducerContractFact,
    mode: Literal["verify", "rebuild"],
) -> SubagentGraphCheckpointRepairReport: ...
```

Offline repair可以从canonical EventLog sequence 1无界full fold、验证任意历史prefix、重新写确定性artifact/event并报告第一处不一致。它必须：

- 由显式operator/doctor入口调用，不得被compiler、AgentRuntime live preparation、resume或Inspector replay隐式调用；
- 使用与checkpoint声明完全匹配的historical reducer binding；绑定不可用时fail closed；
- 不读取process-local `SubagentGraphStateStore`补造事实；
- 不改变业务graph事实，只增加/确认deterministic checkpoint memoization；
- 对EventLog gap/inconsistent reducer报告ledger repair需求，不能用checkpoint覆盖问题。

由此EventLog与版本化reducer单独足以恢复全部状态，同时production hot path永远保持bounded。

### 9.9 Checkpoint推进、writer与retention/GC

```python
class SubagentGraphCheckpointPolicyFact(BaseModel):
    checkpoint_every_events: int
    checkpoint_max_delta_events: int
    checkpoint_max_delta_bytes: int
    bootstrap_max_events: int
    bootstrap_max_bytes: int
    rebase_max_checkpoint_candidates: int
    retained_checkpoint_min_count: int
    policy_fingerprint: str
```

V1默认：`512 / 32_768 / 33_554_432 / 2048 / 8_388_608 / 8 / 2`。delta hard bound 必须至少容纳一个
合法最大 model-call materialization（当前为 `16_384 events / 16 MiB`）以及其控制面、工具和 safe-point 尾部；否则单次合法
provider stream 会在下一次 compile 前击穿 checkpoint 通道。该 bound 仍是防止异常事件流耗尽 CPU/内存的物理安全限制，不是
模型上下文窗口。配置加载/doctor 必须拒绝小于 model-call materialization 上界的生产组合。这些值是process-local acceleration policy，在
HostSession创建时resolve；不进入RunStart、graph semantic source或provider payload identity。达到events/bytes任一阈值可schedule；run
terminalization前若已有scheduled writer则bounded drain，但offline repair仍允许在RunEnd之后materialize历史prefix。

Checkpoint writer由`RuntimeSession`持有：

```python
class SubagentGraphCheckpointWriteState(str, Enum):
    PENDING = "pending"
    WRITING_ARTIFACT = "writing_artifact"
    WRITING_EVENT = "writing_event"
    CONFIRMING = "confirming"
    COMMITTED = "committed"
    CONFLICT = "conflict"
    UNKNOWN = "unknown"
```

- stable artifact/event candidates、service-owned worker与shared Future；
- waiter用`asyncio.shield()`，caller cancellation不取消owner；
- FULL commit后才发布latest compatible checkpoint pointer；
- PARTIAL/UNKNOWN latch writer，禁止把candidate作为读取基点；
- close bounded drain全部逻辑owner与物理I/O operation；
- 已有compatible checkpoint且delta仍bounded时，单次推进失败不阻塞run；越界前仍无法推进则fail closed。

Checkpoint artifact retention冻结为cache语义：

- Context Input Manifest**不永久pin**其原始checkpoint artifact；
- V1**不实现在线物理GC**；`RuntimeSession`、background timer、compiler和Inspector都无权删除checkpoint artifact；
- GC与doctor只允许显式privileged maintenance CLI/service调用，并对`runtime_session_id`取得PostgreSQL advisory maintenance lock；进程崩溃由
  PostgreSQL连接自动释放锁，不设计第二套durable expiring lease；
- maintenance lock持有期间必须确认：没有live HostSession/HostCore lease、没有open/resumable manifest mutation owner、没有pending
  RuntimeSession writer/physical I/O，session处于closed/quiescent；任一条件不满足立即拒绝，不等待或取消live owner；
- Host open/resume取得durable session lease的线性化步骤也必须经过同一maintenance lock domain，避免GC检查后新session穿透；
- closed-session Inspector/exact replay在读取checkpoint artifact时取得同一domain的shared transaction advisory lock；GC/doctor使用exclusive lock，
  因而不存在“确认hash后、decode前”被删除的窗口；
- InMemory只提供测试用同语义mutex；production maintenance authority只支持PostgreSQL durable wiring；
- historical manifest exact replay优先使用原checkpoint；原candidate缺失、冲突或delta超bound时允许rebase到任意不晚于目标high-water、
  contract-compatible且delta bounded的checkpoint；它可以比原preferred更新，也可以更早；
- rebase后semantic source、最终graph fingerprint、selection与candidate fingerprints全部相同，则仍为`exact_replay`；checkpoint ID变化
  只记为operational `rebased=True`；
- 若历史prefix暂时没有bounded rebase路径，Inspector报告`artifact_missing/subagent_checkpoint_rebase_unavailable`，operator可用offline
  doctor重建；这不是durable fact丢失；
- GC catalog可清理过时artifact bytes，但保留bounded checkpoint event/index metadata以寻找rebase或repair目标。

Doctor rebuild使用独立`SubagentGraphCheckpointMaintenanceWriter`：它只执行canonical full fold、artifact
`put-if-absent-or-confirm-identical`与stable checkpoint event confirmation，不使用RuntimeSession ordered publisher，不接收live HostSession对象，
也不能清除ledger reconciliation latch。CLI发现目标runtime仍live/resumable时必须返回`checkpoint_maintenance_session_not_quiescent`。

### 9.10 Selection、manifest与exact replay join

现有`ContextCandidateSourceSelectionFact(source_instance_id="subagent:results")`执行schema hard cut：删除
`source_from_sequence/source_through_sequence`，新增required：

```python
subagent_graph_semantic_source: SubagentGraphSemanticSourceFact
```

它不引用checkpoint ID或delta range。Selection必须由恢复出的graph纯派生并比较eligible IDs、selected IDs、omitted count、collection
reason与selection fingerprint。`ContextAuthoritySlicePlan.required_source_from_sequence`不再因为subagent selection被强制拉回sequence 1；
transcript/current-run/candidate event authority slice继续按Stage 3自己的最早required event ref冻结，与graph acceleration reader分离。

Snapshot validator必须使selection中的semantic source与`LongHorizonContextAttributionFact.subagent_graph_semantic_source`完全相等，并继续
验证policy cap、selected/omitted计数和selected result authority refs。禁止一边从checkpoint graph选择IDs，一边把较晚primary event-slice
high-water伪装成selection basis。

`ContextCompileInputManifestFact`同时保存：

```python
subagent_graph_semantic_source: SubagentGraphSemanticSourceFact
subagent_graph_acceleration: SubagentGraphAccelerationFact
```

前者进入input semantic aggregate；后者只保存当次实际读取路径。Exact replay算法：

1. 读取manifest semantic source与原acceleration；
2. 优先原checkpoint，缺失则按第9.6节rebase；
3. 恢复至manifest acceleration的`ledger_through_sequence`；
4. 比较semantic source、graph state、eligible/selected/omitted/collection与candidate fingerprints；
5. 不比较原acceleration fingerprint与replay acceleration fingerprint；Inspector并列展示二者及`rebased`。

Checkpoint后续推进、GC或offline重建不得改变旧manifest的semantic replay verdict。只有authority prefix/reducer contract/graph state/selection
变化才改变semantic identity。

### 9.11 L0A不做什么

- 不删除EventLog历史；
- 不把checkpoint或checkpoint schedule当作事实真源；
- 不允许checkpoint跳过graph reducer invariant；
- 不建立通用所有reducer snapshot框架；
- 不让compiler读取live `SubagentGraphStateStore`；
- 不让production compile/replay调用offline full-fold repair；
- 不用checkpoint解决context compaction。

---

## 10. Tool observation projection state

### 10.1 Projection是可审计派生状态

每个normalized tool result unit在window内有且只有一个active representation：

```python
class ToolObservationProjectionFact(BaseModel):
    schema_version: Literal["tool_observation_projection.v1"]
    window_id: str
    projection_generation: int
    unit_id: str
    tool_call_id: str
    tool_result_event_id: str
    tool_result_sequence: int
    tool_name: str
    representation: ToolObservationRepresentation
    representation_rank: int
    rendered_fragment_artifact_id: str | None
    rendered_fragment_fingerprint: str
    estimated_tokens: int
    primary_artifact_id: str | None
    essential_envelope_fingerprint: str
    observation_timing_fingerprint: str
    source_rollup_id: str | None
    protected_reason_codes: tuple[str, ...]
    decision_reason_code: ProjectionRewriteReason
    semantic_fingerprint: str
```

rank固定：

```text
full=60
preview=50
essential=40
artifact_locator=30
rollup_member=20
pair_stub=10
```

这里`full`表示该result经过Stage 3 per-observation安全capture/render policy后可供context使用的最高保真表示，不等于把未归档的
无限raw stdout/HTTP body直接放入prompt。raw durable truth仍在event/artifact层。

rewrite只能保持或降低rank。相同rank但正文变化仅在同一确定性renderer version、相同source fact下允许；否则是contract conflict。

`pair_stub`仍必须保留：

- assistant tool call；
- matching tool result role/message；
- tool_call_id；
- success/error/denied/cancelled状态；
- universal observation timing；
- essential domain state的bounded摘要；
- primary artifact locator（若有）；
- source rollup reference（若有）。

禁止`cleared`、空字符串result、删除result但保留call、跨call复用result。

### 10.2 Window projection state

```python
class ContextWindowProjectionState(BaseModel):
    window_id: str
    window_generation: int
    projection_generation: int
    through_sequence: int
    unit_projections: tuple[ToolObservationProjectionFact, ...]
    rollups: tuple[ObservationRollupFact, ...]
    total_projected_tokens: int
    protected_projected_tokens: int
    state_semantic_fingerprint: str
```

projection state只包含截至固定event high-water已经terminal的tool result。新result在下一safe point以其initial representation和
`reason=new_result_ingested`加入；这同样是generation rewrite，而不是隐式mutable append。即使未触发soft pressure，也必须先FULL
commit该ingest generation，compiler才能消费result。

`ContextWindowOpenedEvent`本身建立generation 0 baseline：initial run从opening high-water重建；compacted window从old projection中按
retained group取交集。baseline不是额外mutable cache，reducer/replay必须重算并核对window fact中的count/fingerprint。

### 10.3 Rewrite plan与event分页

```python
class ToolObservationProjectionRewriteEntryFact(BaseModel):
    unit_id: str
    from_representation: ToolObservationRepresentation | None
    to_projection: ToolObservationProjectionFact


class ContextProjectionRewritePageEvent(EventBase):
    rewrite_id: str
    window_id: str
    from_projection_generation: int
    to_projection_generation: int
    source_through_sequence: int
    page_index: int
    page_count: int
    entries: tuple[ToolObservationProjectionRewriteEntryFact, ...]
    rollups: tuple[ObservationRollupFact, ...]
    plan_fingerprint: str
    final_state_fingerprint: str
    reason_code: ProjectionRewriteReason
```

规则：

- 一个rewrite的所有pages必须同一`write_events()` atomic batch；
- page index从0连续到`page_count-1`；
- unit只能在一个page出现一次；
- reducer收齐全部pages后才发布新generation；
- source high-water不能超过safe point冻结值；
- from generation/state fingerprint必须CAS匹配当前state；
- retry复用相同rewrite ID、page event IDs与payload；
- NONE可重新plan；FULL fold并继续；PARTIAL/UNKNOWN latch session；
- publication failure在full commit后不回滚projection；先fold committed pages，再向上传播publication failure。

### 10.4 Projection reducer invariant

纯reducer验证：

- generation连续；
- window active；
- 每个entry的source event存在且terminal；
- tool_call/result四方pairing一致；
- rank单调不增；
- protected unit不进入禁止的降级级别；
- rollup member与rollup source集合完全一致；
- final state fingerprint可重算；
- 一个source result不能被两个active rollup消费；
- projection state不改变source fact、result status或artifact内容。

Reducer inconsistent时，普通rebuild只能清reducer latch，不能清由partial batch造成的ledger structural latch。

### 10.5 保护分类

```python
class ToolObservationProtectionFact(BaseModel):
    unit_id: str
    classes: tuple[Literal[
        "current_user_adjacent",
        "current_run_recent",
        "pending_interaction",
        "error_recovery",
        "explicit_user_requested_evidence",
        "unconsumed_subagent_result",
        "artifact_write_pending",
        "tool_call_in_flight",
    ], ...]
    minimum_representation: ToolObservationRepresentation
    protection_fingerprint: str
```

最低级别：

- pending interaction / in-flight：`full`，且正常情况下尚无terminal result，不参与rewrite；
- current user adjacent：至少`essential`；
- recent error recovery：至少`essential`；
- unconsumed subagent result：至少`essential`；
- artifact write pending：不得降到依赖该artifact的locator；
- 普通已归档历史：可降到`pair_stub`。

保护集合从canonical slice、window、pending interaction和artifact confirmation事实派生，不从scratchpad推断。

---

## 11. L1：动态token投影预算

### 11.1 删除36K aggregate第二真源

以下字段从生产预算模型删除：

- `LoopBudget.tool_result_context_chars`；
- `LoopBudget.tool_result_body_context_chars/tool_result_envelope_context_chars`；
- `LoopBudget.prior/current_tail/legacy_tool_result_context_chars`；
- `LoopBudget.latest_tool_result_reserved_chars/max_tool_results_per_context`的aggregate allocation职责；
- `ToolResultRenderPolicyBasisFact.total/body/envelope/prior/current/current_user/legacy *_context_chars`；
- `ToolResultRenderPolicyBasisFact.latest_result_reserved_chars_per_unit`的aggregate reserve职责；
- `ResolvedToolResultRenderPolicyFact.latest_reserved_total_chars/current_tail_normal_context_chars/protected_current_tail_total_chars`；
- `ResolvedToolResultRenderPolicyFact.initial_*_remaining_chars`；
- 任何`len(rendered_text) > 36_000`的aggregate hard failure；
- renderer内部独立的context-window常量；
- 以chars/4反向估算整个tool-result集合的兼容路径。

仍保留安全hard cap：

- 单个raw observation ingest byte/char cap；
- 单个artifact persistence byte cap；
- essential envelope字段长度；
- error/stdout/stderr preview长度；
- artifact ref数量；
- process inventory数量；
- JSON nesting/depth/string长度；
- provider SDK/request body的绝对尺寸防线。

这些安全cap只约束一个事实或一个外部边界，不决定模型总上下文分配。

Schema hard cut为：

```python
class ToolResultRenderPolicyBasisFact(BaseModel):
    policy_version: Literal["tool-result-render-policy:v2"]
    per_tool_cap_chars: int
    per_message_cap_chars: int
    per_envelope_cap_chars: int
    minimum_essential_envelope_chars: int
    max_artifact_refs_per_unit: int
    max_data_placeholder_chars: int
    envelope_render: ToolResultEnvelopeRenderPolicyFact
    basis_fingerprint: str


class ResolvedToolResultRenderPolicyFact(BaseModel):
    basis: ToolResultRenderPolicyBasisFact
    ordered_unit_ids: tuple[str, ...]
    protected_unit_ids: tuple[str, ...]
    unit_order_fingerprint: str
    protection_fingerprint: str
    policy_fingerprint: str
```

V2只决定单unit安全渲染与确定性order/protection ingress；跨unit取舍全部归
`LongHorizonContextBudgetDecisionFact + ContextWindowProjectionState`。旧V1 schema/constructor/reader production引用为零，不保留alias。

### 11.2 Frozen policy

```python
class LongHorizonContextAllocationPolicyFact(BaseModel):
    schema_version: Literal["long_horizon_context_allocation.v1"]
    tool_projection_soft_ratio_ppm: int
    tool_projection_post_rewrite_ratio_ppm: int
    window_compaction_trigger_ratio_ppm: int
    window_compaction_post_target_ratio_ppm: int
    latest_tool_result_reserve_tokens: int
    current_run_recent_unit_count: int
    max_projection_units_per_window: int
    max_rollup_members: int
    max_rewrite_entries_per_page: int
    max_safe_point_revisions: int
    max_compile_attempts_per_model_call: int
    policy_fingerprint: str
```

V1默认：

```text
tool_projection_soft_ratio_ppm          = 250_000   # input budget的25%
tool_projection_post_rewrite_ratio_ppm  = 180_000   # rewrite后目标18%
window_compaction_trigger_ratio_ppm     = 800_000   # 整体input 80%
window_compaction_post_target_ratio_ppm = 550_000   # 新window目标55%
latest_tool_result_reserve_tokens       = max(
    1,
    min(4_096, floor(input_budget_tokens * 20_000 / 1_000_000)),
)
current_run_recent_unit_count           = 4
max_projection_units_per_window         = 256
max_rollup_members                      = 64
max_rewrite_entries_per_page            = 128
max_safe_point_revisions                 = 16
max_compile_attempts_per_model_call      = 4
```

默认值属于composition-root policy，不属于provider limits。每个run将resolved值写入`RunLongHorizonContractFact`，运行中配置变化只影响新run。

Policy validator要求：

```text
0 < post_rewrite_ratio <= soft_ratio < window_trigger_ratio <= 1_000_000
0 < window_post_target_ratio < window_trigger_ratio
latest reserve/current recent/max projection units/max members/page size全部为正
max_safe_point_revisions >= 4
max_compile_attempts_per_model_call >= 2
max_rollup_members <= max_projection_units_per_window
max_rollup_members <= 256
max_rewrite_entries_per_page <= 256
```

非法配置在RunStart前失败，不silent clamp。

`max_projection_units_per_window`只在L4拥有可执行hard出口：

- L1仅计算`active_projection_unit_count/unit_count_limit_exceeded`诊断；unit count本身不触发失败或尚不存在的rewrite；
- L2/L3可通过representation降级降低tokens，但rollup仍保留每个member，不能声称unit count已减少；
- L4启用后，超过unit cap直接产生`window_compaction_required`，关闭旧window后由summary + protected/new tail构成新active unit set；
- L4 compaction后仍超cap，且超限全部来自protected current tail时，以`context_window_protected_tail_exceeds_budget` fail closed，绝不删除
  第257项或拆pairing。

因此L1–L3的unit-count路径是durable shadow diagnostic，不影响provider payload；L4 schema/production cutover后才成为hard admission invariant。

`latest_tool_result_reserve_tokens`是allocation优先级reserve，不是latest result hard cap。latest/protected essential真实需要更多时可占用
hard available；planner必须相应降级older eligible units。

### 11.3 预算公式

所有计算使用`ResolvedModelTarget.token_estimator`；不得在projection planner重写另一套估算器。

```text
input_budget
  = run_model_target.context_budget.input_budget_tokens

fixed_non_result_tokens
  = system + tool schemas + current user + candidates + request envelope
    + all non-tool transcript
    + assistant tool-call messages/arguments及其framing
    + tool-result role/message中不随representation变化的固定framing

minimum_result_projection_tokens
  = 所有active tool results使用各自minimum allowed representation后的可变fragment估算

hard_available_tool_projection_tokens
  = input_budget
    - fixed_non_result_tokens

soft_tool_projection_tokens
  = min(
        hard_available_tool_projection_tokens,
        floor(input_budget * tool_projection_soft_ratio_ppm / 1_000_000),
    )

post_rewrite_target_tokens
  = min(
        hard_available_tool_projection_tokens,
        floor(input_budget * tool_projection_post_rewrite_ratio_ppm / 1_000_000),
    )
```

`input_budget_tokens`已经扣除resolved safety margin与effective output reservation，本章不得再次重复扣除。

若`hard_available_tool_projection_tokens < minimum_result_projection_tokens`，deterministic projection不可达。L2/L3按第12.6节区分“minimum
projection仍可发送”与“超过hard input”；只有L4才进入window compaction planning。若
`fixed_non_result_tokens + minimum_result_projection_tokens > input_budget`，且L4 window compaction仍不能降低fixed历史prefix，fail closed为
`context_window_protected_tail_exceeds_budget`，不得删除配对事实。

### 11.4 两遍规划

每个model-step safe point执行：

1. 用当前projection渲染并取得完整token breakdown；
2. 若overall input低于window trigger且tool projection低于soft target，不rewrite；
3. 若tool projection超过soft target，运行deterministic rewrite到post target；
4. rewrite full commit后重新构建snapshot并用同一estimator重算；
5. 任何非零planner/compiler/pre-send estimate mismatch均为contract error；
6. L4启用后，active unit count超过cap或input仍超过window trigger时进入LLM window compaction；L1–L3对unit count只记录shadow
   diagnostic；
7. LLM compaction成功后重新构建，不复用旧estimate。

例外：phase已是`finalization_only`时，只要tool projection高于post-rewrite target就执行
`reason=finalization_narrowing`，即使尚未超过普通soft target；protected/current actionable facts仍遵守相同minimum representation。

Projection planner的估算必须产出与final lowered messages同形状的`message_tokens_by_index`；不得只估正文而遗漏framing、tool schemas或system envelope。

### 11.5 Budget audit

```python
class LongHorizonContextBudgetDecisionFact(BaseModel):
    window_id: str
    source_through_sequence: int
    input_budget_tokens: int
    fixed_non_result_tokens: int
    projected_tool_tokens_before: int
    minimum_result_projection_tokens: int
    soft_tool_projection_tokens: int
    post_rewrite_target_tokens: int
    projected_tool_tokens_after: int | None
    final_input_tokens_after: int | None
    active_projection_unit_count: int
    max_projection_units_per_window: int
    unit_count_limit_exceeded: bool
    decision: Literal[
        "within_soft_target",
        "projection_rewrite",
        "window_compaction_required",
        "protected_tail_unreachable",
    ]
    estimator_fingerprint: str
    decision_fingerprint: str


class LongHorizonProjectionPressureShadowFact(BaseModel):
    window_id: str
    source_through_sequence: int
    active_projection_unit_count: int
    max_projection_units_per_window: int
    unit_count_limit_exceeded: bool
    enforcement_mode: Literal["diagnostic_only"]
    operational_fingerprint: str
```

该fact进入Context Input Manifest的long-horizon attribution。只有发生durable rewrite/phase transition/compaction时才另写事件；普通within-target decision由manifest审计即可，避免每个model call额外写一条ledger event。

L1–L3在尚无unit-count出口时另写bounded、non-semantic `LongHorizonProjectionPressureShadowFact`到manifest operational audit，保存
`active_projection_unit_count/max_projection_units_per_window/unit_count_limit_exceeded`；它不进入上述`decision`、context semantic aggregate或
provider payload。L4 hard cut时删除该shadow carrier的production writer，unit-count超限必须映射正式`window_compaction_required`或
`protected_tail_unreachable`。这不是运行时feature flag；各PR的production composition只有一种行为，L4迁移reset旧中间schema。

---

## 12. L2：跨result rollup与artifact-aware thinning

### 12.1 Rollup不是把事实合成一条假result

Rollup由两部分组成：

1. 原始每个tool call/result仍保留pairing-safe `rollup_member`或更高representation；
2. 另加一个只读的derived evidence fragment，为模型提供聚合后的趋势、去重结果或索引。

因此rollup不能：

- 改写任何ToolResultEndEvent；
- 把多个tool_call_id映射成一个result role message；
- 宣称原始result已consumed；
- 生成新的工具成功事实；
- 隐藏error/deny/cancelled member；
- 覆盖primary artifact ownership。

### 12.2 Rollup DTO

```python
class ObservationRollupMemberFact(BaseModel):
    unit_id: str
    tool_call_id: str
    result_event_id: str
    result_sequence: int
    result_state: ToolResultStateFact
    essential_semantic_fingerprint: str
    primary_artifact_id: str | None


class ObservationRollupFact(BaseModel):
    schema_version: Literal["observation_rollup.v1"]
    rollup_id: str
    window_id: str
    rollup_kind: Literal[
        "repeated_search_results",
        "repeated_file_reads",
        "terminal_inventory",
        "repeated_error_family",
        "subagent_result_index",
    ]
    member_facts: tuple[ObservationRollupMemberFact, ...]
    ordered_member_set_fingerprint: str
    renderer_id: str
    renderer_version: str
    renderer_contract_fingerprint: str
    rendered_artifact_id: str
    rendered_content_sha256: str
    estimated_tokens: int
    evidence_keys: tuple[str, ...]
    semantic_fingerprint: str


class ObservationRollupRendererContractFact(BaseModel):
    schema_version: Literal["observation_rollup_renderer_contract.v1"]
    renderer_id: str
    renderer_version: str
    input_schema_fingerprint: str
    output_schema_fingerprint: str
    framing_policy_fingerprint: str
    placement_contract_fingerprint: str
    renderer_contract_fingerprint: str


class ObservationRollupPlacementAnchorFact(BaseModel):
    placement: Literal["after_complete_pair_group"]
    pair_group_id: str
    insert_after_transcript_message_id: str
    insert_after_source_sequence: int
    anchor_fingerprint: str


class RuntimeDerivedObservationCarrierContractFact(BaseModel):
    schema_version: Literal["runtime_derived_observation_carrier.v1"]
    carrier_id: str
    carrier_version: str
    provider_api: str
    provider_role_contract: Literal["runtime_inert_observation"]
    wire_shape_fingerprint: str
    contract_fingerprint: str


@dataclass(frozen=True, slots=True)
class RuntimeDerivedObservationCarrierBinding:
    contract: RuntimeDerivedObservationCarrierContractFact
    implementation_build_fingerprint: str
    lower_runtime_observation: Callable[..., FrozenJsonObjectFact]


class RuntimeDerivedObservationCarrierRegistry:
    def resolve_binding(
        self,
        *,
        carrier_id: str,
        carrier_version: str,
        contract_fingerprint: str,
    ) -> RuntimeDerivedObservationCarrierBinding: ...


class RuntimeDerivedObservationCompileUnit(BaseModel):
    unit_id: str
    source_kind: Literal["long_horizon_observation_rollup"]
    source_semantic_fingerprint: str
    inline_text: str
    inline_content_sha256: str
    inline_chars: int
    placement_anchor: ObservationRollupPlacementAnchorFact
    lowering_kind: Literal["runtime_owned_derived_observation"]
    carrier_contract_fingerprint: str
    unit_fingerprint: str


class PreparedObservationRollupUnit(BaseModel):
    schema_version: Literal["prepared_observation_rollup.v1"]
    rollup: ObservationRollupFact
    artifact_id: str
    artifact_content_sha256: str
    ordered_member_unit_ids: tuple[str, ...]
    ordered_member_set_fingerprint: str
    compile_unit: RuntimeDerivedObservationCompileUnit
    prepared_fingerprint: str
```

`RuntimeDerivedObservationCarrierContractFact`的durable owner是Resolved Model Target，而不是rollup planner或当前adapter配置：

```python
class ResolvedModelTargetFact(BaseModel):
    contract_version: Literal["resolved-model-target:v3"]
    # remaining existing required fields ...
    runtime_observation_carrier: (
        RuntimeDerivedObservationCarrierContractFact | None
    )
```

完整carrier fact进入`target_fingerprint`、RunStart model target、ResolvedModelCall、Context Input Manifest与replay/rebind。Target resolution按provider
API精确选择carrier contract；不支持时required为`None`，不再另存一个可能漂移的`supports_runtime_observation` bool。恢复时registry按
ID/version/contract fingerprint精确rebind；provider API、wire shape或contract任一不匹配fail closed。Process-local
`implementation_build_fingerprint`只作当前进程诊断，不进入target/event/manifest semantic identity。

这是`resolved-model-target:v2 -> v3` schema hard cut；不保留nullable field default、v2 reader fallback或由当前adapter补造carrier。虽然字段类型允许
`None`表示目标明确不支持，但v3 payload必须显式包含该key。开发数据库reset/显式migration与L2代码同PR完成。

`RuntimeDerivedObservationCompileUnit.carrier_contract_fingerprint`必须等于actual
`ResolvedModelCall.target.runtime_observation_carrier.contract_fingerprint`。Target为`None`时prepared rollup units必须为空；target非空时compiler、
estimator、pre-send validator与adapter四层都精确验证同一carrier fact。Exact replay从durable target fact恢复完整contract，不查询“当前provider默认
carrier”，也不只凭一个孤立hash猜wire lowering。

member至少2个、不超过policy cap，sequence严格递增，同一unit不能重复或跨active rollup。`ordered_member_set_fingerprint`覆盖按source
sequence排列的完整`(unit_id, tool_call_id, result_event_id, essential_semantic_fingerprint)` tuple；`rollup_id`必须按第5.3节覆盖该fingerprint，
不得用单数`source_unit_fingerprint`代表整个集合。

Prepared unit必须在进入pure compiler前由materializer读取artifact、确认ID/hash/semantic metadata，并将正文变成owned immutable inline
text。`artifact_id == rollup.rendered_artifact_id`、compile unit与artifact的两个content SHA必须都等于已确认artifact bytes，ordered member IDs必须逐项等于rollup
member facts；prepared fingerprint覆盖compile unit、ordered member set、renderer contract、carrier contract与anchor。Compiler不持有artifact
store，也不在lowering期间读取I/O。

Placement invariant：anchor必须是包含最后一个rollup member的完整assistant tool-call/result pair group；compiler先完整lower该group的全部
call/result。`insert_after_transcript_message_id`只标识该pair group的**顺序边界**，不把rollup归因给该message、某个tool call或某个tool
result。Pair group中的非member sibling继续按自身projection正常保留，**绝不**为了anchor自动加入member set；它可以属于不同family、protected
class或error state。最后一个member所属pair group若在frozen high-water下不完整，planner放弃本次rollup，不能扩展members、等待未来event或把
derived内容插入组内中间。每个member必须独立满足全部eligibility，并精确共享同一family、rollup kind与renderer contract。

Rollup随后作为独立的runtime-owned derived observation lower。Provider-neutral input层新增无`tool_call_id`的
`LLMMessage.runtime_observation(...)`（或等价typed carrier）；它不是assistant tool-result、user message或新的工具成功事实。每个production
adapter必须通过run-frozen `RuntimeDerivedObservationCarrierContractFact`把该carrier映射为provider支持的inert runtime/developer observation
wire shape，且该wire shape进入target/request-shape fingerprint、token estimator与prompt-cache identity。Adapter不得把它lower成
`LLMMessage.tool_result()`，不得附着最后member的call ID，也不得静默改成user role。

如果某provider没有可验证的inert carrier，`ResolvedModelTargetFact.runtime_observation_carrier`必须为`None`：该target下L2仍可做单unit thinning，
但不得生成跨call rollup；若minimum projection仍依赖rollup才能满足hard input，则按`ProjectionTargetUnreachable`处理，而不是伪造role。
Carrier ID/version/fingerprint缺失或rebind漂移均fail closed。

### 12.3 Eligibility

只有满足全部条件才可rollup：

- result terminal且durable；
- semantics builder提供稳定`rollup_family_key`与bounded evidence keys；
- 不处于pending interaction；
- 非current-user-adjacent；
- 不属于最近`current_run_recent_unit_count`个result，除非只是full→preview而非rollup；
- artifact写入已confirmed；
- result没有尚未投递的subagent handoff；
- result state能由rollup renderer无损标明；
- source timing和artifact locator可在member stub保留。

未知descriptor、generic semantics或缺少rollup family的result只能单体降级，不能凭JSON正文猜测可合并。

### 12.4 Deterministic renderer

```python
class ObservationRollupRenderer(Protocol):
    def render(
        self,
        *,
        rollup_kind: str,
        members: tuple[ObservationRollupMemberFact, ...],
        source_units: tuple[ToolResultRenderUnit, ...],
        policy: LongHorizonContextAllocationPolicyFact,
    ) -> ObservationRollupRenderResult: ...


@dataclass(frozen=True, slots=True)
class ObservationRollupRendererBinding:
    contract: ObservationRollupRendererContractFact
    implementation_build_fingerprint: str
    renderer: ObservationRollupRenderer


class ObservationRollupRendererRegistry:
    def resolve_binding(
        self,
        *,
        renderer_id: str,
        renderer_version: str,
        renderer_contract_fingerprint: str,
    ) -> ObservationRollupRendererBinding: ...
```

renderer只消费typed essential facts、timing、artifact refs与bounded preview，不解析raw result JSON以推断domain truth。输出canonical Markdown/JSON artifact；同ID同bytes同semantic metadata幂等。

Descriptor/rollup policy冻结renderer ID/version/contract fingerprint；normal live、materialization与exact replay三路必须精确rebind。同一
ID/version注册不同contract fingerprint为configuration conflict；implementation build fingerprint只作当前进程诊断，不参与durable
semantic identity。缺binding、版本漂移或artifact正文与renderer contract不匹配全部fail closed。

排序规则：

1. member按result sequence；
2. evidence key按UTF-8 lexical；
3. duplicate evidence保留首次source并记录出现次数；
4. error/denied分组不得与success混为同一结论；
5. 时间显示来自source timing，禁止用safe-point当前时间伪装历史事实。

### 12.5 Artifact-aware thinning顺序

单unit降级顺序固定：

```text
full
-> preview（保留开头/结尾与完整essential envelope）
-> essential（移除非必要body，保留typed essential）
-> artifact_locator（仅在confirmed primary text artifact存在时）
-> rollup_member（仅在active rollup存在时）
-> pair_stub
```

如果没有text artifact，binary/image ref可保留但不得标为`primary_text_artifact`或宣称可用`artifact_read`恢复正文。compact/minimal envelope必须优先保留真实primary text artifact，而不是`artifacts[0]`。

### 12.6 Planner目标函数

V1不是knapsack最优解。使用完全确定的分层算法：

1. 固定protected units；
2. 对eligible family生成节省token最多、member sequence最早的rollup；
3. 对remaining units按
   `(protection_rank asc, recency asc, estimated_savings desc, unit_id lexical)`
   逐级降级；
4. 达到post target立即停止；
5. 不允许为了多保留一个低价值body而降级protected essential；
6. 相同输入必须生成相同plan fingerprint。

若deterministic rewrite无法达到soft/post-rewrite target，返回结构化`ProjectionTargetUnreachable`；planner不直接写failed event。其阶段行为必须
唯一化：

- L2/L3尚无window compaction时，如果minimum pairing-safe projection的最终estimate仍不超过ResolvedModelCall hard input budget，则继续发送该
  minimum projection，并把typed `context_projection_soft_target_exceeded`/`ProjectionTargetUnreachable` diagnostic写入manifest operational
  audit；不得把soft target当成hard failure；
- L2/L3若minimum projection仍超过hard input budget，则本次model step以typed
  `context_window_compaction_unavailable`收口，不调用provider；不得循环重跑同一planner，也不得退回Stage 3字符串截断；
- L4 hard cut后，相同结果改为进入第14节window compaction admission；只有compaction成功并重启safe point后才可继续provider call。

因此`ProjectionTargetUnreachable`表达“deterministic projection无法达到目标”，并不在所有PR阶段等价于run failure；hard input estimate才是
L2/L3是否允许发送的最终边界。

---

## 13. L3：current-run deterministic micro-compaction

### 13.1 与现有compaction的区别

现有`RuntimeContextCompactor`主要压缩`current_run_start_sequence - 1`之前的历史prefix。L3第一次允许在同一active run内改变模型可见projection，但仍不修改source ledger，也不创建新context window。

L3只做确定性变化：

- full/preview/essential/locator/pair-stub降级；
- rollup创建；
- 重复artifact locator去重；
- 已确认读取结果的body thinning；
- 保留tool call/result pairing的fragment替换。

它不调用LLM，不生成自由文本summary，不改变window ID，只递增projection generation。

### 13.2 Safe point输入

```python
class CurrentRunProjectionPlanningInput(BaseModel):
    run_id: str
    window: ContextWindowFact
    current_projection: ContextWindowProjectionState
    canonical_slice: ContextEventSlice
    transcript: TranscriptCompileInput
    tool_result_units: tuple[ToolResultRenderUnit, ...]
    protection_facts: tuple[ToolObservationProtectionFact, ...]
    context_budget: ResolvedModelContextBudgetFact
    allocation_policy: LongHorizonContextAllocationPolicyFact
    estimator: TokenEstimatorFact
    source_through_sequence: int
```

process-local estimator callable单独bind；fact用于identity校验。planning input不得持有LoopState、scratchpad、live EventLog、MCP supervisor或mutable cache。

### 13.3 Current-run边界

“current run”由committed `RunStartEvent.sequence`到`source_through_sequence`定义，不由消息列表索引猜测。tool result属于current run当且仅当：

- result event run_id等于active run；
- sequence位于边界内；
- matching call也位于同run，或是合法resume所持有的原pending interaction；
- normalized transcript明确建立pair reference。

preflight历史compaction summary不属于current run；resume后的新tool result仍属于同一个durable run。

### 13.4 Planning与提交

```python
class CurrentRunProjectionPlan(BaseModel):
    rewrite_id: str
    from_generation: int
    to_generation: int
    source_through_sequence: int
    budget_decision: LongHorizonContextBudgetDecisionFact
    pages: tuple[ContextProjectionRewritePageEvent, ...]
    expected_state_fingerprint: str
    final_state_fingerprint: str
    plan_fingerprint: str
```

算法：

1. 从canonical slice重建source units与current projection；
2. 验证projection reducer high-water；
3. 计算budget decision；
4. 在纯内存中生成rollup artifacts candidates与rewrite pages；
5. 先以stable ID持久化/confirm所有required artifacts；
6. 对projection state执行CAS；
7. atomic commit全部pages；
8. FULL后fold committed pages；
9. rebuild ContextFactSnapshot；
10. 重新estimate，不信任plan的预估结果。

若第5步artifact失败：不写rewrite event，原projection仍有效。若第7步NONE：可用同plan重试。PARTIAL/UNKNOWN：ledger structural latch，不允许model call、resume或破坏性close。

### 13.5 同一model step的attempt identity

一次model step可经历：

```text
resolve call
-> build attempt 1
-> deterministic rewrite
-> build attempt 2
-> LLM compaction
-> build attempt 3
-> provider send
```

同一step复用`resolved_model_call_id`与`model_call_index`，但必须拆开两个计数：

```text
safe_point_revision
  每次freeze从0开始；checkpoint bootstrap、settlement fold、phase transition、rewrite、compaction、CAS winner变化等任何durable mutation后+1。
  revision增加本身不表示compiler已经运行。

compile_attempt_index
  只有真正构造snapshot/manifest并调用compiler时才+1；对应新的context_id。
```

`projection rewrite`与`window compaction`不创建新model call identity。FULL publication recovery若只fold/ack后继续，不消耗compile attempt；
只有旧snapshot被判失效、实际重新compile时才递增。

V1 bounds来自run-frozen allocation policy：`max_safe_point_revisions=16`、`max_compile_attempts_per_model_call=4`。Compile attempts最多表达：

1. initial compile；
2. deterministic rewrite后的compile；
3. window compaction后的compile；
4. 并发CAS/publication recovery使已编译snapshot失效后的最后一次compile。

同一revision不得重复提交同类rewrite/phase/checkpoint candidate；各mutation还受generation/CAS与单调phase约束。超过revision bound为
`long_horizon_preparation_cycle_exceeded`；超过compile bound为`context_compile_attempts_exhausted`。二者分别进入typed input failure，不写
含糊的`context_preparation_attempts_exhausted`，也不进入无限自压缩循环。

### 13.6 不允许的实现

- 原地修改`state.messages`中的ToolResultBlock；
- 把render decision写回scratchpad；
- compiler根据总字符数临时截断；
- tool renderer在不了解window projection时自行选择跨unit淘汰；
- pre-send发现超预算后由adapter静默truncation；
- 对当前run调用旧prefix compactor并假装RunStart仍在tail。

---

## 14. L4：pairing-safe current-run LLM compaction

### 14.1 何时触发

只有全部条件满足才允许：

- active window open；
- deterministic rewrite已运行或证明无可用rewrite；
- final estimate达到/超过`window_compaction_trigger_ratio_ppm`；
- protected facts能以summary+tail形式放入post target；
- 当前没有pending approval/MCP input/plan interaction；
- 没有in-flight tool call或sync worker；
- source ledger、projection reducer、manifest writer、checkpoint service均可信；
- summarizer target/call可以resolve；
- actual summarizer call的`ModelCallReservationQuoteFact`已经生成并通过第15.8节bucket admission；
- run尚未进入`exhausted`或`emergency_hard_stop`。

manual force可忽略trigger threshold，但不能绕过pending/in-flight/ledger/protected-tail约束。

### 14.2 新事件族，不复用旧prefix compaction

```python
class ContextWindowCompactionPlanFact(BaseModel):
    compaction_id: str
    compaction_attempt_index: int
    run_id: str
    source_window_id: str
    source_window_generation: int
    source_projection_generation: int
    source_projection_state_fingerprint: str
    source_through_sequence: int
    target_window_id: str
    target_window_generation: int
    source_context_fingerprint: str
    summarizer_call: ResolvedModelCallFact
    rollout_reservation: RolloutReservationFact
    summarizer_input_manifest_artifact_id: str
    protected_unit_ids: tuple[str, ...]
    summarized_unit_ids: tuple[str, ...]
    retained_tail_unit_ids: tuple[str, ...]
    estimated_tokens_before: int
    protected_tail_tokens: int
    summarizer_input_estimated_tokens: int
    summary_output_budget_tokens: int
    post_compaction_target_tokens: int
    stable_started_event_id: str
    stable_completed_event_id: str
    stable_failed_event_id: str
    plan_fingerprint: str


class ContextWindowCompactionStartedEvent(EventBase):
    plan: ContextWindowCompactionPlanFact


class ContextWindowCompactionCompletedEvent(EventBase):
    compaction_id: str
    plan_fingerprint: str
    summary_artifact_id: str
    summary_content_sha256: str
    summary_estimated_tokens: int
    actual_post_compaction_estimated_tokens: int
    target_reached: Literal[True]
    summarizer_usage: ModelTokenUsageFact | None
    usage_status: Literal["reported", "missing"]
    rollout_settlement_event_id: str
    source_window_close_event_id: str
    target_window_open_event_id: str


class ContextWindowCompactionFailedEvent(EventBase):
    compaction_id: str
    plan_fingerprint: str | None
    failure_stage: Literal[
        "planning",
        "summarizer_resolution",
        "input_manifest",
        "model_validation",
        "model_stream",
        "summary_validation",
        "summary_artifact",
        "terminal_batch",
        "recovery",
    ]
    reason_code: str
    summarizer_call: ResolvedModelCallFact | None
    rollout_settlement_event_id: str | None
    observed_summary_tokens: int | None
    observed_post_compaction_tokens: int | None
    retryable: bool
```

`source_projection_generation`与`source_projection_state_fingerprint`共同绑定Started前看到的exact projection；terminal batch必须再次验证
二者仍与active source window一致，不能只凭generation接受内容漂移。完整DTO中的`summarized_pair_group_ids`可以为空：纯文本历史同样可以
形成合法summarized prefix。只有source中实际存在tool-call/result pair时，才要求pair group完整、顺序稳定且summarized/retained集合不重叠；不得为了
满足非空pair集合而拒绝纯文本窗口压缩。

不要把它们塞入旧`ContextCompactionStarted/Completed/FailedEvent`：旧事件以跨run transcript prefix与`keep_after_sequence`为语义；window compaction以同run source window、projection和new window chain为语义。两者可共享低层direct-call collector、artifact writer与commit-confirmation helper，但不共享event schema。

Failed validator要求：planning/resolution/input_manifest/model_validation的`rollout_settlement_event_id=None`；model_stream及以后说明
Started/reservation已经FULL，settlement event ID必填。Started batch NONE不写Failed，UNKNOWN/PARTIAL时ledger已blocked，也不得再追加一条
猜测性Failed。

`compaction_attempt_index`在每个source window内从1连续递增。write/publication retry复用同一index/ID/payload；只有前一attempt的Failed
terminal FULL后才能计划下一index。Started outcome UNKNOWN/PARTIAL时禁止偷偷增加index。

### 14.3 Summary的事实边界

summarizer输入不是原始event dump，而是确定性`WindowCompactionSourceDocument`：

```python
class WindowCompactionSourceEntryFact(BaseModel):
    source_entry_id: str
    source_kind: Literal[
        "user_message",
        "assistant_text",
        "tool_call",
        "tool_result_projection",
        "observation_rollup",
        "subagent_result",
    ]
    source_event_refs: tuple[ContextEventReferenceFact, ...]
    source_artifact_refs: tuple[str, ...]
    model_visible_text: str
    timing: ContextSourceTimingFact | None
    semantic_fingerprint: str
```

manifest保存所有entry ID/fingerprint和canonical artifact。summarizer prompt必须要求：

- 区分观察事实、模型推论和未解决问题；
- 保留关键路径、错误、用户约束、artifact locator；
- 不宣称未执行的工具成功；
- 不吸收protected tail为已完成历史；
- 输出bounded结构化summary；
- 列出source entry IDs或citation map。

summary结果经schema validator验证source citation只引用input entries；不允许自由生成artifact ID或tool result identity。

`summary_output_budget_tokens`必须精确等于summarizer resolved target的
`context_budget.effective_output_tokens`。本章不重新引入per-call `max_output_tokens`，也不让provider默认决定cap。Planning使用该完整输出
cap做保守reservation：

```text
fixed_new_window_tokens
+ protected_retained_tail_tokens
+ summary_output_budget_tokens
<= post_compaction_target_tokens
```

若专用summarizer需要更小cap，应配置独立model slot的`default_output_tokens`并产生不同target fingerprint。实际summary低于reservation后
再用同一estimator复核；高于reservation写summary_validation Failed，不能截断后声称成功。

### 14.4 Pairing-safe切分

compaction unit以pair group为最小粒度：

```python
class WindowCompactionPairGroupFact(BaseModel):
    group_id: str
    assistant_message_id: str
    tool_call_ids: tuple[str, ...]
    result_unit_ids: tuple[str, ...]
    source_sequence_from: int
    source_sequence_through: int
    protection_classes: tuple[str, ...]
```

一个assistant message含多个tool calls时，要么整组进入summary，要么整组retained，除非normalized compiler已经支持将assistant multi-call message确定性拆为多个provider-valid messages并有独立contract version。V1不拆组。

当前user message、pending interaction邻接消息、最近恢复错误、最近N个pair groups与未消费subagent result必须retained。

### 14.5 Planning顺序

```text
freeze source high-water
-> rebuild exact source window/projection
-> choose summarized prefix + protected retained tail
-> rebind run-frozen summarizer target and resolve call
-> calculate physical-bound summarizer reservation quote
-> admit quote against current phase bucket; transition/restart if required
-> build/degrade/estimate summarizer context
-> persist input manifest
-> LLMRuntime start bundle atomic commits rollout reservation + Started + ModelCallStart
-> collect direct model call through End
-> validate summary/citations/post estimate
-> persist summary artifact
-> atomic terminal batch:
     Completed
     source ContextWindowClosed
     target ContextWindowOpened
-> publish/fold new active window
```

Started之前所有失败写Failed时：

- planning/resolution可无plan/call；
- input manifest之后必须带call与plan fingerprint；
- 如果连failure event都无法可信提交，由pending terminal owner接管，Host close不得跨越。

Compaction admission必须发生在构造/提交Started之前。Exploration/warning/restricted阶段若actual summarizer quote无法由`exploration`容纳，且
没有可等待/回收的reservation，coordinator先以
`reason=exploration_compaction_admission_unreachable`原子切换`finalization_only`，FULL后丢弃旧snapshot并从safe point第2步重启；不得先写
Started，也不得让同一summarizer绕过account。该次compaction在admission restart前后复用同一个尚未Start的resolved summarizer call identity；
重启后必须重新验证run-frozen target与quote fact fingerprint，并从`finalization_compaction` bucket admission，不能二次resolve出不同模型或把它计作
新的provider attempt。

如果actual summarizer quote连`finalization_compaction`也无法容纳，或当前resolved summarizer/provider capability无法执行required
compaction，则原子进入`exhausted`，reason=`window_compaction_unavailable`，并以typed
`context_window_compaction_unavailable` terminalize当前model step/run；不得消耗`finalization_agent` answer reserve，也不得在旧context已超过hard
input时继续provider call。若旧context仍低于hard input而失败只涉及soft target，则按第12.6节L2/L3/L4阶段规则决定是否继续，而不是错误耗尽。

### 14.6 Success atomic batch

成功必须使用一个`RuntimeSession.write_events()`：

```text
ContextWindowCompactionCompletedEvent
ContextWindowClosedEvent(old)
ContextWindowOpenedEvent(new)
```

batch内event ID在Started前预生成。任何一个缺失都视为partial structural corruption。FULL后才：

- 切换active window；
- 释放source document临时ownership；
- 允许下一次compile；
- 将summary projection纳入snapshot。

Completed batch之前，matching `ModelCallEndEvent + RolloutBudgetReservationSettledEvent`必须已经由LLMRuntime atomic FULL commit；Completed
保存settlement event ID并验证call/usage一致。不能为了把所有业务terminal塞入一批而延迟或复制模型usage settlement。

publication failure/full commit仍切换window并向上传播observer error；NONE保留old window；UNKNOWN/PARTIAL latch。

### 14.7 Failure保持旧window可用

summarizer provider error、invalid summary、summary超budget或artifact pre-commit失败时：

- old window保持open；
- projection state保持不变；
- 写stable Failed event；
- 增加bounded consecutive failure count；
- 不反复在同一model step自动调用summarizer；
- 若old context仍低于hard input cap，可继续原call；
- 若old context已无法发送，terminalize run为`context_window_compaction_failed`。

Started已与rollout reservation/ModelStart FULL commit时，LLMRuntime完整lifecycle owner必须先提交
`ModelCallEnd + matching settlement`（包括cancelled/runtime-error无usage的physical-quote settlement），Failed再引用该terminal batch。Window
service不得绕过LLMRuntime单独结算，也不得接受Started/ModelStart永久没有End。Failed不能释放一个从未确认存在的reservation，也不能留下已存在
reservation无terminal owner。

连续失败熔断仅对自动LLM compaction生效，不影响deterministic projection。默认同一window 2次失败后disable auto LLM compaction；新window重置。

同一compaction attempt在收到任何provider bytes、RunError或调用结果不确定后都不得重新调用summarizer来“复现”summary；retry只允许重试
stable durable candidates。provider未得到可信Completed summary就terminal Failed，后续若允许重试必须使用下一attempt index和新的
ResolvedModelCall ID。

### 14.8 Pending terminalization owner

`ContextWindowCompactionService`拥有：

- Started stable candidate；
- terminal batch stable candidates；
- service-owned writer/confirmation tasks；
- 所有物理artifact/database operations；
- shared shielded Future；
- attempt generation与identity-conditional cleanup；
- close bounded drain；
- restart scan/recovery。

对任意`BaseException`包括`CancelledError`：

1. confirm Started NONE/FULL/PARTIAL/UNKNOWN；
2. NONE可取消，不产生terminal obligation；
3. FULL必须保留terminal owner并最终Completed或Failed；
4. PARTIAL/UNKNOWN latch，保留owner；
5. waiter cancellation不得取消service worker；
6. blocking sync DB operation即使logical timeout，也继续留在physical operations集合，Host close不能遗失它。

### 14.9 Restart recovery

session reopen扫描：

- Started无terminal且source window仍open：用stable Failed event写`recovered_interrupted`；
- Completed存在但close/open batch不完整：ledger structural latch，不自动猜winner；
- terminal full batch存在但projection未fold：pure rebuild后恢复new active window；
- summary artifact missing/conflict：contract mismatch，不继续run；
- summarizer call有Start无End时先由`PendingModelStreamCommit`以stable terminal ID修复End/settlement；UNKNOWN/PARTIAL则保持reconciliation
  blocker，不能先写window Failed假装完整收口。

### 14.10 新window transcript projection

打开generation>1 window后，canonical authority改为**多范围**，不能继续用一个从RunStart开始的连续slice承载全部历史：

- primary range固定为`source_through_sequence_at_compaction + 1 .. compile high-water`的bounded contiguous delta；
- RunStart、effective capability、plan/continuation、memory projection与rollout evidence使用exact-ID或indexed sparse named ranges；
- source document持久化pairing-complete retained transcript baseline，包含normalized messages、tool pairs与tool-result units；
- summary/source document与named authority ranges共同保留permission、capability、pending、rollout与citation join，不把checkpoint或cache升级为第二真源；
- live compile和exact replay必须重建同一个`ContextEventAuthorityView`，primary/named range identity全部进入manifest fact fingerprint。
- L5 recurrence是soft observation：V1只统计当前window primary delta中仍有完整gate/result evidence的terminal calls；旧window的累计
  model/tool counters和phase由rollout reducer state延续，但不为保留soft recurrence而把历史text/data chunks重新加入sparse authority。未来若需
  跨window recurrence，必须新增versioned bounded outcome baseline，不能恢复无界raw deltas。

provider transcript projection变为：

```text
summary artifact derived message
+ Started plan中retained pair groups/messages
+ source_through_sequence_at_compaction之后新提交的messages/results
```

被summarized的旧message/tool groups不再作为独立provider messages lower，也不再要求其原始semantic chunk常驻compile authority。它们由
source document中的bounded provenance/citation facts审计；retained group从durable normalized baseline恢复，顺序沿原sequence，future tail从
`source_through_sequence_at_compaction + 1`连续追加。`ContextWindowTranscriptBasisFact`、Started plan和
Completed event三方fingerprint必须一致；compiler禁止根据当前token pressure自行改“哪些已经summary”。

RunStart current user若进入summary会破坏actionable attribution，因此V1始终属于retained protected group。未来允许summary current user必须
另升window contract version。

---

## 15. L5：cumulative rollout budget与finalization reserve

### 15.1 预算目标

单次model input未超过256K，不代表46次搜索累计120万tokens是合理执行。Rollout budget控制整个durable run的累计资源消耗，但不能退化成低上限`max_turns`突然断电。

V1 durable account只计weighted model tokens、tool cost units与call counters。wall-clock只作为Host deadline/operational metric，不进入可replay
phase公式；provider价格/USD同样等待独立cost contract，避免用会随时间变化的价目表重写历史budget state。

计入root account的model purposes只有：本run/child的`agent_model_loop`与本文`context_window_compaction_summary`。RunStart前的preflight
compaction没有该run account；memory governance/reflection在治理UOW大章落地前继续使用各自bounded audit，不得借用或消耗agent
finalization reserve。Inspector应分开展示这些direct subsystem usage，不能把它们伪装成rollout total。

`ModelCallPurpose`新增`CONTEXT_WINDOW_COMPACTION_SUMMARY="context_window_compaction_summary"`，不得复用现有跨run/preflight
`CONTEXT_COMPACTION_SUMMARY`而让account无法区分两类direct call。

正常状态机：

```text
exploration
-> warning
-> restricted
-> finalization_only
-> exhausted
-> emergency_hard_stop
```

### 15.2 Frozen policy

```python
class RolloutBudgetPolicyFact(BaseModel):
    schema_version: Literal["rollout_budget_policy.v1"]
    total_input_budget_multiplier_milli: int
    non_cached_input_weight_milli: int
    cached_input_weight_milli: int
    output_weight_milli: int
    tool_cost_unit_weight_milli: int
    finalization_reserved_model_calls: int
    finalization_reserved_window_compactions: int
    finalization_reserved_tool_cost_units: int
    warning_consumption_ratio_ppm: int
    restricted_consumption_ratio_ppm: int
    finalization_consumption_ratio_ppm: int
    emergency_model_call_limit: int
    emergency_tool_call_limit: int
    max_concurrent_subagent_reservations: int
    policy_fingerprint: str
```

V1默认：

```text
total_input_budget_multiplier_milli = 8_000
non_cached_input_weight_milli       = 1_000
cached_input_weight_milli           = 100
output_weight_milli                 = 4_000
tool_cost_unit_weight_milli         = 1_000_000
finalization_reserved_model_calls   = 2
finalization_reserved_window_compactions = 1
finalization_reserved_tool_cost_units = 16
warning_consumption_ratio_ppm       = 600_000
restricted_consumption_ratio_ppm    = 800_000
finalization_consumption_ratio_ppm  = 1_000_000
emergency_model_call_limit          = 200
emergency_tool_call_limit           = 256
max_concurrent_subagent_reservations = 8
```

这里“8倍”是相对单次resolved input budget的累计weighted allowance，不是模型context window；可按部署policy调整，但每个run frozen required。

Policy validator要求：

- 所有weights与multiplier为正，`cached_input_weight <= non_cached_input_weight`；
- `0 < warning < restricted < finalization <= 1_000_000`；
- finalization agent calls至少2、window compactions至少1、tool cost units至少1；
- emergency counters严格高于正常phase可达的默认工作量；
- account按run与summarizer targets计算后，total必须严格大于finalization reserve；
- 任何非法值在RunStart前失败，不clamp、不回落旧LoopBudget。

### 15.3 计量单位

统一使用整数`budget_milliunits`：

```text
non_cached_input_cost = non_cached_input_tokens * 1000
cached_input_cost     = cached_input_tokens * 100
output_cost           = output_tokens * 4000
tool_cost             = rollout_cost_units * 1_000_000
```

默认下一tool cost unit等价于1,000个non-cached input tokens的milliunits；它是稳定治理单位，不是provider价格。

reported usage规则：

- cached tokens已经包含在input tokens，先从input扣出再分别加权；
- reasoning tokens已经包含在output tokens，不重复计费；
- total必须等于input+output；
- usage missing时必须按下面唯一的物理上界reservation quote全额结算，不读取stream文本字符、不调用另一个token estimator，也不把“看起来较短”的输出当作可信上界。

Admission、finalization、window compaction、child budget和missing usage共用唯一pure helper：

```python
class ModelCallReservationQuoteFact(BaseModel):
    resolved_model_call_id: str | None
    target_fingerprint: str
    physical_input_token_upper_bound: int
    output_token_upper_bound: int
    non_cached_input_weight_milli: int
    output_weight_milli: int
    reserved_milliunits: int
    policy_fingerprint: str
    quote_semantic_fingerprint: str
    quote_fact_fingerprint: str | None


def calculate_model_call_reservation(
    *,
    target: ResolvedModelTargetFact,
    resolved_model_call_id: str | None,
    policy: RolloutBudgetPolicyFact,
) -> ModelCallReservationQuoteFact: ...
```

唯一公式：

```text
physical_input_token_upper_bound
  = target.context_budget.pre_margin_input_tokens

output_token_upper_bound
  = target.context_budget.effective_output_tokens

reserved_milliunits
  = physical_input_token_upper_bound * non_cached_input_weight_milli
    + output_token_upper_bound * output_weight_milli
```

`pre_margin_input_tokens`而不是chars/4 estimator值，代表provider请求在resolved total context/output cap下可合法报告的物理输入上界；它包含
input safety margin。因此provider tokenizer对中文、tool schema或framing的计数即使高于Pulsara estimate，只要未超过该上界，就不会使
settlement超过reservation，也不应触发reconciliation。

- `pre_send_estimated_input_tokens`仍来自该ResolvedModelCall提交Start前的同一validation result，但只用于pre-send admission与audit，不作为
  worst-case rollout reserve；
- usage missing时`charged_milliunits == reservation_quote.reserved_milliunits`，且不享受cached-input折扣；
- cancellation后若End usage存在仍按reported usage计入；provider已dispatch但无可信usage时按同一quote全额结算；只有可证明从未dispatch的
  pre-transport failure允许`not_started_zero`；
- provider reported input高于estimate但不高于`physical_input_token_upper_bound`是合法measurement；只有reported input/output超过各自
  resolved physical bound、usage arithmetic非法或settlement payload冲突才是contract violation；
- quote helper及其canonical fingerprint是runtime admission、静态feasibility、finalization、summarizer与child额度的唯一真源，禁止复制近似公式。

`quote_semantic_fingerprint`覆盖target、physical bounds、weights与policy，但排除随机call ID；配置期feasibility、finalization reserve与child
profile调用helper时传`resolved_model_call_id=None`，此时`quote_fact_fingerprint=None`，不会伪造尚不存在的ModelCall identity。
Runtime为actual `ResolvedModelCall`调用时必须传它的真实call ID，并验证target fingerprint相等；此时`quote_fact_fingerprint`必填且额外覆盖
call ID，reservation/start/settlement三方引用它。这样每次call有独立事实身份，又不会让配置矩阵或profile semantic identity受临时call ID污染。

结算使用单一typed breakdown：

```python
class RolloutUsageChargeFact(BaseModel):
    accounting_basis: Literal[
        "provider_reported_usage",
        "not_started_zero",
        "reserved_missing_usage",
        "cancelled_reserved",
    ]
    reported_input_tokens: int | None
    reported_cached_input_tokens: int | None
    reported_output_tokens: int | None
    pre_send_estimated_input_tokens: int
    physical_input_token_upper_bound: int
    output_token_upper_bound: int
    charged_output_tokens: int
    charged_milliunits: int
    reservation_quote_fact_fingerprint: str
    policy_fingerprint: str
    charge_fingerprint: str
```

`provider_reported_usage`要求三个reported字段非空（provider未报告cached tokens时规范化为0）、满足usage invariant，并要求
`charged_output_tokens == reported_output_tokens`；`not_started_zero`只允许在Start已FULL但transport/provider dispatch被证明从未发生时使用，要求
reported字段全空、charged tokens/milliunits全为0；`reserved_missing_usage`要求reported字段全空、
`charged_output_tokens == output_token_upper_bound`且charge等于完整reservation quote；`cancelled_reserved`同样按尚未取得可信terminal usage时的
完整quote结算。三种basis都必须保存同一quote fact fingerprint；reported usage可低于quote并释放差额，绝不能因高于estimator但仍在physical
bounds内而latch。

### 15.4 总预算与finalization reserve

```text
total_rollout_budget
  = input_budget_tokens
    * total_input_budget_multiplier_milli

one_finalization_call_reserve
  = calculate_model_call_reservation(primary_target, resolved_model_call_id=None).reserved_milliunits

one_window_compaction_reserve
  = calculate_model_call_reservation(summarizer_target, resolved_model_call_id=None).reserved_milliunits

finalization_tool_reserve
  = finalization_reserved_tool_cost_units
    * tool_cost_unit_weight_milli

finalization_reserve
  = one_finalization_call_reserve
    * finalization_reserved_model_calls
    + one_window_compaction_reserve
      * finalization_reserved_window_compactions
    + finalization_tool_reserve

exploration_allowance
  = total_rollout_budget - finalization_reserve
```

若policy使`exploration_allowance <= 0`，run preparation fail closed为配置错误。保留2次agent call的原因：第一次finalization call可能请求一个
已被阶段策略拒绝的exploration tool；第二次必须能看到typed deny result并真正给出最终答复。独立保留1次window compaction call，避免
进入finalization时上下文本身已过大却只能耗掉最后的agent answer admission；tool reserve只允许synthesis mutation、artifact hydration与
bounded verification，确保最终文件/验证不会被早期search消耗。

Finalization reserve是跨调用admission reserve，不从单次`input_budget_tokens`再扣除。

### 15.4.1 配置期静态可行性矩阵

不能等到用户`RunStart`才发现某个小窗口target与大output-cap summarizer组合使`exploration_allowance <= 0`。Composition root必须提供唯一
pure helper：

```python
class RolloutBudgetFeasibilityResult(BaseModel):
    execution_profile_kind: Literal["host_root", "subagent_child"]
    execution_profile_id: str
    primary_target_slot: str
    primary_target_fingerprint: str
    summarizer_target_slot: str
    summarizer_target_fingerprint: str
    resolved_window_policy_fingerprint: str
    policy_fingerprint: str
    total_rollout_budget_milliunits: int
    finalization_agent_reserve_milliunits: int
    finalization_compaction_reserve_milliunits: int
    finalization_tool_reserve_milliunits: int
    finalization_reserve_milliunits: int
    exploration_allowance_milliunits: int
    feasible: bool
    reason_code: Literal["feasible", "exploration_allowance_non_positive"]
    result_fingerprint: str


def evaluate_rollout_budget_feasibility(
    *,
    primary_target: ResolvedModelTargetFact,
    summarizer_target: ResolvedModelTargetFact,
    policy: RolloutBudgetPolicyFact,
) -> RolloutBudgetFeasibilityResult: ...
```

规则：

1. 配置加载完成后、Host接受session/run之前，枚举model selection、fallback与profile mapping实际可能产生的全部production
   root primary target/summarizer pair，以及全部可启动child profile实际可能产生的child primary/summarizer pair；不是只检查默认pair，也不是对永远
   不可选择的slot做无意义Cartesian product；
2. 每个pair都直接用resolved target调用同一个`calculate_model_call_reservation(..., resolved_model_call_id=None)`；配置矩阵只保存quote
   amount/semantic fingerprint，不创建假的ResolvedModelCall identity，不复制简化公式。Child row还必须按child primary target解析window
   policy并保存其fingerprint，不能复制parent policy；
3. 任一enabled production pair不可行时，production host配置校验失败，不能把问题推迟到首个用户run；
4. disabled/diagnostic-only slot可以显示不可行，但不能进入production model selection；
5. runtime仍在`PRE_RUN`对实际pair重复执行相同helper并比较fingerprint，防止配置加载后binding漂移；不一致使用稳定
   `rollout_budget_feasibility_drift`并在RunStart前fail closed；
6. 不允许通过silent减少finalization call、compaction或tool reserve让pair“变得可行”；必须调整model slot、output cap或versioned rollout policy。

V1复用现有`pulsara config-check`作为配置doctor surface，必须输出完整矩阵，至少包含：

```text
primary_target
summarizer_target
execution_profile_kind
execution_profile_id
resolved_window_policy_fingerprint
total_rollout_budget
finalization_agent_reserve
finalization_compaction_reserve
finalization_tool_reserve
finalization_reserve
exploration_allowance
feasible
reason_code
```

文本输出可做人类友好格式，JSON输出字段名与DTO一致。secret、endpoint query与credential不能进入报告。Inspector只展示实际run已冻结的
account数值，不重新执行配置矩阵；配置期matrix不是durable run truth。

### 15.5 Rollout account DTO

```python
class RolloutBudgetAccountFact(BaseModel):
    account_id: str
    owner_runtime_session_id: str
    root_run_id: str
    policy: RolloutBudgetPolicyFact
    total_budget_milliunits: int
    finalization_reserve_milliunits: int
    finalization_agent_reserve_milliunits: int
    finalization_compaction_reserve_milliunits: int
    finalization_tool_reserve_milliunits: int
    exploration_allowance_milliunits: int
    semantic_fingerprint: str


class RolloutBudgetStateFact(BaseModel):
    account_id: str
    phase: RolloutPhase
    charged_milliunits: int
    reserved_milliunits: int
    exploration_charged_milliunits: int
    exploration_reserved_milliunits: int
    finalization_agent_charged_milliunits: int
    finalization_agent_reserved_milliunits: int
    finalization_compaction_charged_milliunits: int
    finalization_compaction_reserved_milliunits: int
    finalization_tool_charged_milliunits: int
    finalization_tool_reserved_milliunits: int
    model_call_count: int
    recovered_incomplete_model_stream_count: int
    model_stream_reconciliation_blocker_count: int
    tool_call_count: int
    active_reservations: tuple[RolloutReservationFact, ...]
    through_sequence: int
    state_fingerprint: str
```

root host run创建account；subagent不创建独立无限budget，而从parent account取得reservation。

### 15.6 Admission reservation

每个model call发送前预留由resolved physical bounds决定的真实worst-case：

```python
class RolloutReservationFact(BaseModel):
    reservation_id: str
    account_id: str
    owner_kind: Literal["model_call", "tool_call", "subagent_run"]
    owner_id: str
    phase_at_reservation: RolloutPhase
    budget_bucket: RolloutBudgetBucket
    reserved_milliunits: int
    model_call_reservation_quote: ModelCallReservationQuoteFact | None
    source_sequence: int
    semantic_fingerprint: str
```

模型call必须满足`model_call_reservation_quote != None`且
`resolved_model_call_id/quote_fact_fingerprint`非空、owner ID精确匹配，并且
`reserved_milliunits == model_call_reservation_quote.reserved_milliunits`；tool/child reservation该字段必须为空。绝不使用estimated input冒充
worst case。End后以provider-reported usage结算并释放差额；missing/cancelled则按完整quote结算。Tool call按已验证
`ToolActionClassificationFact.rollout_cost_units`预留，且不得超过descriptor max；subagent根据budget snapshot申请bounded额度。

Bucket规则：

- exploration/warning/restricted下所有agent call、window compaction、tool、subagent只用`exploration`；
- finalization-only agent call只用`finalization_agent`；
- finalization-only window summarizer只用`finalization_compaction`；
- finalization允许的synthesis/hydration/verification tool只用`finalization_tool`；
- exhausted/emergency不创建新reservation；
- exploration永远不能借用三个finalization buckets；
- 各bucket独立校验remaining，settlement不能跨bucket挪账。

所有reservation/settlement/phase transition由pure reducer维护；不允许进程内counter成为真源。

### 15.7 Typed events

```python
class RolloutBudgetAccountOpenedEvent(EventBase):
    account: RolloutBudgetAccountFact


class RolloutBudgetAccountClosedEvent(EventBase):
    account_id: str
    final_state_fingerprint: str
    charged_milliunits: int
    model_call_count: int
    tool_call_count: int
    active_reservation_count: Literal[0]
    run_end_event_id: str


class ChildRolloutSettlementAggregateFact(BaseModel):
    subaccount_fingerprint: str
    provider_reported_model_call_count: int
    reserved_missing_model_call_count: int
    cancelled_reserved_model_call_count: int
    not_started_zero_model_call_count: int
    tool_terminal_settlement_count: int
    model_call_count: int
    tool_call_count: int
    reported_subset_input_tokens: int
    reported_subset_cached_input_tokens: int
    reported_subset_output_tokens: int
    model_charged_milliunits: int
    tool_charged_milliunits: int
    charged_milliunits: int
    through_sequence: int
    aggregate_fingerprint: str


class ChildRolloutSubaccountClosedEvent(EventBase):
    subaccount_fingerprint: str
    settlement_aggregate: ChildRolloutSettlementAggregateFact
    run_end_event_id: str


class RolloutBudgetReservationCreatedEvent(EventBase):
    reservation: RolloutReservationFact


class RolloutBudgetReservationSettledEvent(EventBase):
    reservation_id: str
    charged_milliunits: int
    usage_status: Literal[
        "provider_reported_usage",
        "not_started_zero",
        "reserved_missing_usage",
        "cancelled_reserved",
        "tool_terminal",
        "child_terminal_handoff",
        "child_not_started_zero",
    ]
    usage_charge: RolloutUsageChargeFact | None
    source_model_call_end_event_id: str | None
    source_tool_result_event_id: str | None
    child_usage_handoff: ChildRolloutUsageHandoffFact | None


class RolloutPhaseTransitionedEvent(EventBase):
    account_id: str
    from_phase: RolloutPhase
    to_phase: RolloutPhase
    source_through_sequence: int
    state_before_fingerprint: str
    state_after_fingerprint: str
    reason_code: RolloutTransitionReason
```

AccountOpened与RunStart同batch。Main call的ReplyStart、reservation与ModelCallStart同batch；ModelCallEnd、settlement与ReplyEnd同batch。
Window/direct call按第19.6节不创建Reply，但ModelStart/End仍由LLMRuntime完整lifecycle owner提交。
如果End batch无法FULL确认，account保留pending reservation并阻止下一次admission；不保留“End后再补settlement”的正常生产路径。

Root AccountClosed与root RunEnd同batch；child SubaccountClosed与child RunEnd同batch。parent随后通过EventLogLocator读取confirmed child close +
RunEnd，复制并验证exact `ChildRolloutSettlementAggregateFact`，构造`ChildRolloutUsageHandoffFact`并结算root reservation。

Settlement invariant：model-call settlement的`usage_charge`必填且
`charged_milliunits == usage_charge.charged_milliunits`；tool/child settlement的`usage_charge=None`，其charge分别由冻结的
`ToolActionClassificationFact`或`ChildRolloutUsageHandoffFact`验证。任一settlement charge不得超过对应reservation。Provider reported input
高于pre-send estimator但仍位于resolved physical bounds内必须正常结算；只有reported usage越过physical bounds或quote/reservation不一致才是
contract violation并latch，不能silent clamp或借用别的bucket。

### 15.8 Phase thresholds

Admission始终使用：

```text
effective_exploration_committed
  = exploration_charged_milliunits
    + exploration_reserved_milliunits
```

防止并发超卖。但不可逆phase transition只按已经settled的
`exploration_charged / exploration_allowance`判断，避免一次worst-case output reservation随后释放差额却永久误推phase：

- `<60%`：exploration；
- `>=60%`：warning；
- `>=80%`：restricted；
- `>=100%`：finalization_only。

threshold之外还存在一个确定性、不可缺失的admission transition。Coordinator在每次准备agent model call时先解析该次真实
`ResolvedModelCall`并通过`calculate_model_call_reservation()`计算physical-bound quote；如果当前phase仍为exploration/warning/restricted，且同时满足：

1. `exploration_remaining < actual_next_call_reservation`；
2. 没有active/reclaimable exploration reservation可以通过等待terminal settlement释放；
3. 当前call不是一个已经FULL commit reservation的continuation；

则必须以当前account state CAS原子提交：

```text
RolloutPhaseTransitionedEvent(
    to_phase="finalization_only",
    reason_code="exploration_admission_unreachable",
)
```

提交FULL后重新开始safe point，并为同一个实际ResolvedModelCall从`finalization_agent` bucket申请reservation。不得等settled ratio自行到100%，
否则会形成“剩余额度小于下一call、charged又不会再增长”的永久夹死。若尚有active reservation，则先bounded等待terminal settlement或按既有
ownership取消低优先级child；在这些reservation归宿UNKNOWN/PARTIAL时进入reconciliation blocked，不得抢跑phase transition。

`exhausted`不以抽象的“最小call”或slot default推断。它只在当前已经解析出的实际finalization ResolvedModelCall reservation无法由
`finalization_agent` bucket容纳，且没有可回收finalization reservation时进入；如context pressure要求先做window compaction，还必须先检验实际
summarizer call的`finalization_compaction` reservation。`emergency_hard_stop`只在model/tool emergency counter或明确runtime fail-safe触发。
ledger corruption进入reconciliation blocked，user/operator stop走RunTerminationIntent，二者都不伪造成rollout phase transition。

Window compaction有完全对称、但bucket不同的可达性规则：

1. safe point确定需要compaction后，先resolve actual summarizer call并调用`calculate_model_call_reservation()`；
2. exploration/warning/restricted下若`exploration_remaining < actual_summarizer_quote`且无可回收reservation，FULL commit
   `finalization_only(reason=exploration_compaction_admission_unreachable)`并重启safe point；
3. finalization-only下从`finalization_compaction`申请同一quote；
4. 该bucket仍不可容纳且无可回收reservation，或actual summarizer capability不可用，则FULL commit
   `exhausted(reason=window_compaction_unavailable)`并以typed failure收口；
5. phase transition、reservation created与Started不能位于同一模糊batch：transition FULL后必须重建account/window/snapshot，之后才允许
   `ReservationCreated + ContextWindowCompactionStarted + ModelCallStart`。

`WINDOW_COMPACTION_UNAVAILABLE`只用于actual required compaction在finalization reserve中仍不可执行；普通soft-target unreachable、provider
temporary error或ledger reconciliation不得冒充该transition。

phase只能单调前进。除`exploration_admission_unreachable`这个明确的admission可达性规则外，reservation压力不足不伪造settled threshold。
新context window、compaction成功、cache hit或subagent结束都不能把phase倒退。

### 15.9 能力门控

tool descriptor增加：

```python
class ToolActionClassifierContractFact(BaseModel):
    classifier_id: str
    classifier_version: str
    input_schema_fingerprint: str
    output_schema_fingerprint: str
    classification_policy_fingerprint: str
    contract_fingerprint: str


class LongHorizonToolPolicyFact(BaseModel):
    allowed_action_classes: tuple[LongHorizonActionClass, ...]
    max_rollout_cost_units: int
    allowed_in_phases: tuple[RolloutPhase, ...]
    action_classifier_contract: ToolActionClassifierContractFact
    policy_fingerprint: str


class ToolActionClassificationFact(BaseModel):
    tool_call_id: str
    descriptor_id: str
    descriptor_fingerprint: str
    action_class: LongHorizonActionClass
    rollout_cost_units: int
    normalized_action_fingerprint: str
    classifier_id: str
    classifier_version: str
    classifier_contract_fingerprint: str
    classification_fingerprint: str
```

classification必须在完整tool arguments可用、真实执行之前生成。`CapabilityGateDecisionEvent`新增
`action_classification: ToolActionClassificationFact | None`；known descriptor必填，descriptor-missing才允许为空。allow decision与tool rollout
reservation atomic commit后才能执行；phase deny不创建tool reservation，但仍写classification、gate decision和typed denied result。

单一语义工具的classifier可返回固定class；`terminal_process`按normalized action/arguments保守分类。无法证明是hydration/verification/synthesis
时归为`evidence_acquisition`或`external_action`，在finalization fail closed。classification cost必须在`0..max_rollout_cost_units`内。

classifier registry采用与tool-result semantics builder相同的durable contract fingerprint纪律：same ID/version不同contract fingerprint为配置
冲突；当前进程build fingerprint仅诊断。禁止在AgentRuntime写`if tool_name == "terminal_process"`第二分类真源。

MCP/remote server返回的description、annotations或自报cost不具有governance authority。Host-side MCP descriptor adapter按本地versioned policy映射；
无可信映射的remote tool保守归为`external_action`，restricted/finalization下deny。operator override必须进入event-safe descriptor fingerprint，
不能用进程内name allowlist暗改旧run。

需要外部completion或MCP input-required的`ExternalToolCallRequirementFact`同步required保存action classification与rollout reservation
reference。挂起不结算reservation；terminal resume/result FULL commit后才settle。resume不使用新classifier重新解释旧arguments；它先按当前
permission/capability/phase gate验证是否允许继续协议，再使用requirement冻结的exact classification完成原interaction。若选择deny/cancel，写
typed terminal result并结算同一reservation。

默认分类：

- search/list/broad read/spawn：evidence acquisition；
- artifact read exact locator：artifact hydration；
- targeted stat/test/check：verification；
- write/edit/report result/final response support：synthesis mutation；
- permission/plan/MCP input control：control plane。

进入finalization_only后：

- 禁止新的broad search、目录遍历、无目标web fetch、新subagent探索；
- 允许读取已经存在的exact artifact locator；
- 允许bounded verification；
- 允许写最终artifact、修复代码、生成答案；
- 允许control-plane continuation；
- 被拒绝的tool call仍生成typed deny result，随后至少保留下一次final model call reserve。

unknown descriptor或classifier binding缺失fail closed为不可在restricted/finalization阶段调用。

V1不因phase transition重写同run已冻结的`CapabilityExposureSnapshotFact`，也不隐藏已经暴露给provider的tool schema；否则会引入一个
没有Host boundary owner的exposure revision。约束通过required phase candidate + execution gate实现，所有被拒绝调用产生typed result。
阶段五若要按phase改变model-visible descriptor集合，必须为新exposure建立独立typed owner/identity，不能回写本章account。

### 15.9.1 Tool execution reservation与terminal owner

Tool调用不能继续由gate、rollout reducer与`ToolExecutor`分别写事件。L0B在接入最终account ownership时即建立session/run-scoped commit
port；phase固定exploration且gate尚不按phase拒绝，L5只激活同一port上的phase policy：

```python
class ToolExecutionEventCommitPort(Protocol):
    async def commit_gate_and_reservation(
        self,
        *,
        gate_candidate: FrozenEventWriteCandidate,
        reservation_candidate: FrozenEventWriteCandidate | None,
        expected_account_state_fingerprint: str,
    ) -> ConfirmedCommittedBatch: ...

    async def commit_terminal_and_settlement(
        self,
        *,
        terminal_candidate: FrozenEventWriteCandidate,
        settlement_candidate: FrozenEventWriteCandidate,
        expected_reservation_fingerprint: str,
    ) -> ConfirmedCommittedBatch: ...

    async def commit_gate_and_denial(
        self,
        *,
        gate_candidate: FrozenEventWriteCandidate,
        denied_terminal_candidate: FrozenEventWriteCandidate,
        expected_account_state_fingerprint: str,
    ) -> ConfirmedCommittedBatch: ...

    async def commit_suspension(
        self,
        *,
        suspension_candidate: FrozenEventWriteCandidate,
        expected_reservation_fingerprint: str,
    ) -> ConfirmedCommittedBatch: ...

    async def confirm_batch(
        self,
        candidates: tuple[FrozenEventWriteCandidate, ...],
    ) -> DurableBatchConfirmation: ...

    async def handoff_committed(
        self,
        batch: ConfirmedCommittedBatch,
    ) -> None: ...
```

`commit_*()`只做atomic CAS/write；FULL结果（包括由confirmation恢复的FULL）必须再经`handoff_committed()`进入同一RuntimeSession的
ordered publisher/reducer。`ToolExecutionTerminalOwner`由run/session registry按`tool_call_id + reservation_id`唯一持有stable candidates、attempt generation、shared
completion、physical I/O handles与commit classification。waiter cancellation使用shield；任意`BaseException`均执行第19.2节confirmation。
NONE可复用相同candidate retry；FULL fold/publish/ack；CONFLICT/PARTIAL/UNKNOWN保留owner并阻止新admission、RunEnd与破坏性close。

线性化顺序冻结为：

```text
known descriptor + allow:
  CapabilityGateDecisionEvent(allow, classification)
  + RolloutBudgetReservationCreatedEvent
  -- atomic FULL --> ToolExecutor may start

WAIT / pending interaction before execution:
  CapabilityGateDecisionEvent(wait, classification)
  -- no reservation, no ToolExecutor start

phase/permission/capability deny:
  CapabilityGateDecisionEvent(deny, classification when known)
  + typed ToolResultEndEvent(denied)
  -- atomic, no reservation and no settlement

normal executed terminal:
  ToolResultEndEvent
  + RolloutBudgetReservationSettledEvent(tool_terminal)
  -- atomic FULL

MCP input-required suspension after execution started:
  ToolExecutionSuspendedEvent
  -- reservation remains active; no settlement

MCP resume success/error/deny/cancel/timeout:
  terminal ToolResultEndEvent
  + settlement of the original exact reservation
  -- atomic FULL
```

`WAIT`与“已经开始执行后返回MCP input-required”不是同一状态：前者未取得执行预算，后者必须保留原reservation及Supervisor唯一pending
lease。Resume不得创建第二reservation或按新classifier重新计算cost；当前permission/capability/phase gate可以拒绝继续协议，但拒绝结果仍与原
settlement同批收口。Suspension fact FULL后被取消必须保留reservation；resume terminal FULL后即使publication await被取消，也必须fold result并
settle。UNKNOWN/PARTIAL时唯一terminal owner与Supervisor pending owner都保留，Host close blocked。

`ToolExecutor`在hard cut后只执行已admit的call并返回`PreparedToolTerminalResult`（typed result block、semantics、timing、stable terminal
candidate inputs）；它不再自己append `ToolResultEndEvent`。`AgentRuntime`/tool loop也不得在`finally`单独补settlement。所有normal、deny、
external、MCP resume路径必须汇合到上述port；production grep禁止executor直接event-log append terminal fact。

### 15.10 Subagent共享预算

```python
class ChildRolloutReservationPolicyFact(BaseModel):
    schema_version: Literal["child_rollout_reservation_policy.v1"]
    max_agent_model_calls_per_child: int
    max_window_compactions_per_child: int
    max_tool_cost_units_per_child: int
    max_parent_exploration_share_ppm: int
    policy_fingerprint: str


class ResolvedChildRolloutBudgetFact(BaseModel):
    child_profile: str
    child_primary_target_fingerprint: str
    child_summarizer_target_fingerprint: str
    child_window_policy_fingerprint: str
    child_policy_fingerprint: str
    child_primary_reservation_quote_semantic_fingerprint: str
    child_compaction_reservation_quote_semantic_fingerprint: str
    one_agent_call_reserve_milliunits: int
    one_compaction_call_reserve_milliunits: int
    tool_reserve_milliunits: int
    profile_limit_milliunits: int
    parent_share_limit_milliunits: int
    max_rollout_milliunits_per_child: int
    parent_account_state_fingerprint: str
    resolution_fingerprint: str


class SubagentRolloutBudgetResolvedEvent(EventBase):
    subagent_run_id: str
    subagent_task_id: str | None
    budget_snapshot_event_id: str
    resolved_budget: ResolvedChildRolloutBudgetFact


class ChildRolloutSubaccountFact(BaseModel):
    root_account_id: str
    parent_reservation: RolloutReservationReferenceFact
    child_runtime_session_id: str
    child_run_id: str
    resolved_budget: ResolvedChildRolloutBudgetFact
    reserved_milliunits: int
    subaccount_fingerprint: str


class ChildRolloutUsageHandoffFact(BaseModel):
    subaccount_fingerprint: str
    settlement_aggregate: ChildRolloutSettlementAggregateFact
    child_terminal_reference: ChildNativeTerminalReferenceFact
    handoff_fingerprint: str
```

`ChildRolloutSettlementAggregateFact`是child close/handoff的唯一usage aggregate，不保留单一`usage_status`或“estimated total tokens”：

```text
model_call_count
  = provider_reported_model_call_count
    + reserved_missing_model_call_count
    + cancelled_reserved_model_call_count
    + not_started_zero_model_call_count

tool_call_count
  = tool_terminal_settlement_count

charged_milliunits
  = model_charged_milliunits + tool_charged_milliunits
```

所有count/tokens/charges非负，`through_sequence >= 1`，且
`reported_subset_cached_input_tokens <= reported_subset_input_tokens`。`reported_subset_*_tokens`只对
`provider_reported_usage` model settlements求和；reserved-missing/cancelled/not-started没有reported token贡献，不能把physical quote或pre-send
estimate混入reported totals。`model_charged_milliunits`和`tool_charged_milliunits`分别逐条求和confirmed child settlements，aggregate fingerprint覆盖
全部字段和subaccount identity。

Identity join必须满足：

```text
ChildRolloutSubaccountClosedEvent.subaccount_fingerprint
  == ChildRolloutSubaccountClosedEvent.settlement_aggregate.subaccount_fingerprint
  == ChildRolloutUsageHandoffFact.subaccount_fingerprint
  == ChildRolloutUsageHandoffFact.settlement_aggregate.subaccount_fingerprint
```

`aggregate_fingerprint`覆盖除自身外的完整aggregate，包含`subaccount_fingerprint`；`handoff_fingerprint`再覆盖nested aggregate与child terminal
reference。任一外层/nested identity不等都在event schema或parent handoff构造前fail closed，不能只靠外层fact“顺便绑定”。

Subaccount只有在所有active reservations都已有FULL settlement时才能close，因此不存在`partial/missing` aggregate：某次provider usage缺失已经由
`reserved_missing_model_call_count`表达；settlement本身UNKNOWN/PARTIAL时child close、RunEnd和parent handoff一律blocked。Parent settlement amount
必须等于nested `charged_milliunits`，不能根据token totals重新计算。

V1 policy defaults与validator冻结为：

```text
max_agent_model_calls_per_child       = 16
max_window_compactions_per_child      = 1
max_tool_cost_units_per_child         = 32
max_parent_exploration_share_ppm      = 500_000
```

所有count/cost必须为正，share在`1..1_000_000`。Policy是`RunLongHorizonContractFact` required子项，并被复制进每个
`SubagentBudgetSnapshotEvent`；该snapshot冻结policy，但**不**伪造尚未启动时的parent remaining/resolved amount。
`ResolvedChildRolloutBudgetFact`只由实际child-start owner生成，并写入同批`SubagentRolloutBudgetResolvedEvent`。
`ChildRolloutSubaccountFact.reserved_milliunits == resolved_budget.max_rollout_milliunits_per_child`，且resolved event、parent reservation event与
child RunStart subaccount中的amount/fingerprint/snapshot reference必须四方相等。

额度使用唯一pure整数公式：

```text
one_agent_call_reserve
  = calculate_model_call_reservation(
      child_primary_target,
      resolved_model_call_id=None,
    ).reserved_milliunits

one_compaction_call_reserve
  = calculate_model_call_reservation(
      child_summarizer_target,
      resolved_model_call_id=None,
    ).reserved_milliunits

tool_reserve
  = max_tool_cost_units_per_child
    * tool_cost_unit_weight_milli

profile_limit
  = one_agent_call_reserve
    * max_agent_model_calls_per_child
    + one_compaction_call_reserve
      * max_window_compactions_per_child
    + tool_reserve

parent_share_limit
  = floor(
      parent_exploration_remaining_before_batch
      * max_parent_exploration_share_ppm
      / 1_000_000
    )

max_rollout_milliunits_per_child
  = min(profile_limit, parent_share_limit)
```

这里的target limits、resolved child window policy、reservation quotes、weights与parent remaining都来自同一child-start frozen facts。
`child_window_policy_fingerprint`必须等于用child primary target解析出的policy；它不得等于parent policy仅仅因为配置名称相同。不得读取模型别名的
当前配置、用平均usage、按并发数临时均分或silent clamp。若结果不足以容纳至少一次实际child primary call reservation，child不创建，稳定失败为
`subagent_rollout_reservation_unavailable`；不能先创建child再让它无调用可做。

Independent batch需要先解析整批child targets/budgets，并以同一个parent account expected fingerprint原子提交全部graph-start facts与全部
`SubagentRolloutBudgetResolvedEvent`、全部`ReservationCreated` candidates。任一额度、余额或并发条件不满足时整批NONE、零child启动，稳定错误
`subagent_batch_rollout_reservation_unavailable`；所有child使用同一个`parent_exploration_remaining_before_batch`计算share limit，再按
`subagent_run_id` canonical order组batch，并要求reservation总和不超过该remaining；禁止用迭代顺序为后续child改变公式，也禁止前几个child
成功、后几个失败的隐式部分批。Dependency scheduler中的waiting task不占
rollout reservation，只有依赖满足并且任务实际进入start transition时才用当时parent state解析并申请。Immediate primitive child同样在真实
start线性化点申请，不在descriptor exposure或计划创建时预留。

child ledger以subaccount作为本地hard ceiling，并durable记录本地reservation/settlement；它不直接写parent ledger account。parent在child
terminal handoff时验证subaccount与native terminal reference，再结算root reservation。parent最多承担预留额；child本地若耗尽必须先
finalize/terminalize，不能向parent无界透支。

`child_terminal_handoff`必须携带non-null `child_usage_handoff`，其nested aggregate charge必须等于settlement charge；model/tool source
event字段均为空。唯一无native terminal的例外是parent admission已经FULL、但child RunStart batch确认NONE：parent graph稳定启动失败与
`child_not_started_zero`在同一atomic batch提交，charge固定为0，且`usage_charge`、source event与`child_usage_handoff`全部为空。只要child
RunStart曾经FULL，任何completed/failed/cancelled parent graph terminal都必须先取得confirmed child RunEnd + SubaccountClosed并使用
`child_terminal_handoff`；不得把child启动后的异常或取消伪装成zero settlement。

Handoff nested aggregate必须与child `ChildRolloutSubaccountClosedEvent.settlement_aggregate`完全相等，且
`settlement_aggregate.charged_milliunits <= reserved_milliunits`。parent
terminal event/settlement使用由subagent_run_id + child terminal event ID确定的stable IDs；child close已FULL、parent尚未写入时，restart
repair重建同一payload。

每次child model-step safe point还必须通过`EventLogLocator`读取parent root account固定high-water，保存owner runtime、through sequence、
phase与state fingerprint到child Context Input Manifest。不能把HostSession当前内存phase当作durable truth。parent进入
`finalization_only`后，已经FULL commit的child call可以收口，但child不得创建新exploration reservation或新evidence tool call；新phase在
下一child safe point执行。parent close/phase transition可请求取消child，实际在途tool borrow仍按真实completion drain。

parent spawn前：

1. 解析child primary/summarizer target，按child primary target解析独立window policy，并生成`ResolvedChildRolloutBudgetFact`；
2. 读取`SubagentBudgetSnapshotEvent`中冻结的policy，生成`SubagentRolloutBudgetResolvedEvent`并申请exact reservation；
3. atomic commit parent graph start facts + resolved budget event + rollout reservation；
4. child RunStart保存完整`ChildRolloutSubaccountFact`；
5. child每次model/tool使用该reservation的子账；
6. child terminal handoff携带usage/cost summary；
7. parent atomic结算reservation与graph terminal fact；
8. child unknown/partial terminal保留reservation，不假装释放capacity。

不能让多个child各自获得完整8倍budget。reservation总和不得超过root account可用余额；concurrency cap和budget cap是两个独立条件。

### 15.11 替代旧max_turns/max_tool_calls

`LoopBudget.max_turns=50`与`max_tool_calls=64`不再作为正常产品控制。迁移为：

- phase状态机决定正常收敛；
- emergency defaults提升为200/256；
- emergency触发时写`emergency_hard_stop`，不得伪装成普通budget exhausted；
- stop reason、Inspector、CLI明确显示触发的counter与budget state；
- dogfood断言常规长程任务进入finalization而非撞emergency hard stop。

---

## 16. 中性 rollout status hint

### 16.1 范围冻结

阶段四不实现 provenance-aware evidence progress guard。生产 schema 中不得新增：

- `EvidenceContributionFact`；
- `EvidenceProgressStateFact` / `EvidenceProgressPolicyFact`；
- `EvidenceProgressEvaluatedEvent`；
- evidence policy/builder registry；
- subagent evidence contribution handoff；
- novelty/provenance reducer与3/5/7 progress phase联动。

原因不是低增益探索不存在，而是通用runtime无法可靠判断“新URL”“不同正文”“重复验证”是否真的推动用户目标。L5已经通过
weighted model/tool cost、action class、warning/restricted/finalization与finalization reserve提供可审计的正常收敛机制。Stage 4不再建立
第二套语义进展控制面。

### 16.2 提示原则

Long-Horizon可以向模型陈述已经发生的事实，但不得给出下一步策略：

- 可以陈述当前phase；
- 可以陈述settled model/tool call counts；
- 可以陈述exploration allowance已消费比例与remaining；
- 可以陈述bounded recent window中，同一exact normalized action出现次数及相同terminal outcome次数；
- 不得写“继续”“停止”“改变策略”“直接回答”“应当验证”等指令；
- 不得因调用次数本身触发phase transition、tool deny或RunEnd；
- 不得把“任务执行很久”推断成“任务没有进展”。

模型侧status hint只在以下任一条件成立时出现：

1. rollout phase不再是`exploration`；
2. bounded recent window存在达到阈值的exact recurrence。

单纯达到某个model/tool call count不触发hint。CLI/Inspector仍可始终展示计数。

Activation boundary：L0B落account/window reducer，并为最终usage math required记录tool action classification/cost；这些facts在L0B只驱动
Inspector shadow与exact recurrence shadow，不触发phase、deny或model-visible hint。L0B–L4不得把status candidate加入
`PreparedContextCandidateSet`、manifest semantic input或provider payload。只有L5同时启用phase transition、admission gate与status-hint
candidate materialization；因此L0B验收中的provider payload必须与Stage 3逐字节等价。

### 16.3 Bounded exact recurrence

Recurrence复用L0B起已经为最终account算法durable保存的`ToolActionClassificationFact.normalized_action_fingerprint`与typed terminal result
semantic fingerprint；L5只激活model-visible carrier，不新增evidence extractor或第二派生算法：

```python
class RolloutStatusHintPolicyFact(BaseModel):
    schema_version: Literal["rollout-status-hint-policy:v1"]
    recent_tool_call_window: int
    minimum_equivalent_outcome_occurrences: int
    max_recurrence_entries: int
    policy_fingerprint: str


class RecentToolActionRecurrenceFact(BaseModel):
    normalized_action_fingerprint: str
    terminal_outcome_fingerprint: str
    action_class: LongHorizonActionClass
    action_occurrence_count: int
    equivalent_terminal_outcome_count: int
    recent_tool_call_window: int
    source_event_refs: tuple[ContextEventReferenceFact, ...]
    recurrence_fingerprint: str
```

V1默认：

```text
recent_tool_call_window          = 16
minimum_equivalent_outcome_occurrences = 3
max_recurrence_entries          = 4
```

规则：

- 只统计`evidence_acquisition`与`external_action`；
- `artifact hydration`、`process_control`、`user_interaction`与bounded verification不计入目标action，但允许夹在两次目标action之间；
- 只比较classifier已经产出的exact normalized fingerprint，不做模糊query、URL或正文相似度；
- model-visible recurrence只有在同一normalized action与同一typed terminal outcome fingerprint都达到阈值时才成立；只重复action、
  但outcome不同，不触发hint；
- terminal outcome equivalence只比较已有typed result state与semantic fingerprint，不解析raw JSON或artifact正文；fingerprint必须排除
  observation timestamp、event ID等易变归因；
- 相同action但外部状态改变、结果fingerprint不同，只增加`action_occurrence_count`，不增加同一recurrence entry的
  `equivalent_terminal_outcome_count`；
- recurrence只生成context/Inspector事实，不修改rollout account、phase、cost、permission或capability decision。

### 16.4 模型可见 DTO

```python
class LongHorizonRolloutStatusCandidateFact(BaseModel):
    account_id: str
    rollout_phase: RolloutPhase
    settled_model_call_count: int
    settled_tool_call_count: int
    exploration_consumption_ratio_ppm: int
    remaining_exploration_milliunits: int
    finalization_reserve_milliunits: int
    allowed_action_classes: tuple[LongHorizonActionClass, ...]
    recurrence: tuple[RecentToolActionRecurrenceFact, ...]
    source_event_refs: tuple[ContextEventReferenceFact, ...]
    semantic_fingerprint: str


class RolloutStatusShadowProjectionFact(BaseModel):
    account_id: str
    source_through_sequence: int
    settled_model_call_count: int
    settled_tool_call_count: int
    exploration_consumption_ratio_ppm: int
    recurrence: tuple[RecentToolActionRecurrenceFact, ...]
    derivation_fingerprint: str
    model_visible: Literal[False]
```

canonical renderer只能输出中性状态，例如：

```text
[rollout status]
phase=warning
settled_model_calls=14
settled_tool_calls=27
exploration_consumed=630000ppm
remaining_exploration_milliunits=...
recent_action_recurrence:
- action_class=evidence_acquisition action_occurrences=4 equivalent_terminal_outcomes=3
```

禁止附加自然语言建议。phase execution gate本身仍按L5执行；candidate只是把当前事实和当前允许的action classes如实呈现给模型。

### 16.5 派生、cache与exact replay

Status candidate由同一frozen high-water下的rollout reducer state和最近bounded terminal tool facts纯派生：

- 不新增durable status-hint event；
- 不新增process-local progress store；
- manifest保存candidate、policy fingerprint、source refs与source high-water；
- exact replay从原ledger range重新派生并比较semantic fingerprint；
- cache key包含rollout state fingerprint、policy fingerprint和recurrence source fingerprint；
- operational cache failure等价于miss，不改变durable candidate/manifest fingerprint；
- phase为`exploration`且无recurrence时candidate为空，不给普通长程任务增加无意义催促。

在L0B–L4，Inspector生成`RolloutStatusShadowProjectionFact`（phase固定exploration、settled counts/charge与exact recurrence），但它不进入
input semantic fingerprint或Context Input Manifest candidate集合，并始终标`model_visible=False`。L5 cutover后从同一required
classifier/result facts和pure helper生成`LongHorizonRolloutStatusCandidateFact`；不得在L5另写一套recurrence算法。

未来若要建设AutoResearch/Web Research专用evidence policy，必须作为独立产品层规格重新设计，不得重新塞回本阶段的通用
Context Windows runtime。

## 17. LongHorizonSafePointCoordinator

### 17.1 只统一model-step preparation，不吞并Host run boundary

Host PRE_RUN/PRE_RESUME仍负责RunStart、permission、capability、MCP installation与run target。Long-horizon safe point发生在已committed run内、每次真正model call之前：

```python
class LongHorizonSafePointCoordinator:
    async def prepare_model_step(
        self,
        *,
        committed_run_entry: CommittedRunEntry,
        run_working_set: RunWorkingSet,
        resolved_call: ResolvedModelCall,
        model_call_index: int,
    ) -> PreparedLongHorizonModelStep: ...
```

它不直接修改LoopState，不持有Host `_run_lock`之外的隐式authority，不负责tool execution或provider stream。

### 17.2 固定顺序

```text
1. assert run owner/open window/session mutation可信
2. freeze canonical ledger high-water
3. resolve run-frozen reducer binding; restore graph through checkpoint + bounded delta
4. rebuild current window chain/projection/rollout reducers
5. fold newly committed usage/terminal result
6. commit required settlement/phase transitions
7. capture immutable Context Compiler inputs
8. build snapshot/transcript/units/candidates
9. estimate context using resolved call estimator
10. if tool projection pressure: deterministic rewrite, then restart at 2
11. if window pressure: resolve actual summarizer call + physical quote, execute phase/bucket admission;
    transition FULL时restart at 2，admitted时LLM compaction，terminal FULL后restart at 2
12. calculate actual agent call physical quote and execute phase/bucket admission; transition FULL时restart at 2
13. build exact rollout reservation candidate and expected account CAS
14. freeze final Context Input Manifest
15. compile and validate exact estimate
16. return prepared context + complete lifecycle-start companion candidates to LLMRuntime
17. LLMRuntime repeats pre-yield identity/account/quote validation
18. LLMRuntime atomic commits ReplyStart + reservation + ModelCallStart（direct call无Reply）
19. only after FULL commit may transport/provider start
```

任何durable mutation后必须重新freeze high-water和重建snapshot，禁止把旧snapshot与新projection/window混用。

### 17.3 Result DTO

```python
@dataclass(frozen=True, slots=True)
class FrozenEventWriteCandidate:
    event_id: str
    event_type: str
    event_schema_version: str
    event_schema_fingerprint: str
    event_domain_contract_fingerprint: str
    canonical_payload_bytes: bytes
    payload_fingerprint: str


class RunExecutionActivationFact(BaseModel):
    """Event-safe identity of the segment activation that owns a model call."""

    schema_version: Literal["run_execution_activation.v1"]
    activation_owner_kind: Literal[
        "host_run_boundary",
        "host_resume_boundary",
        "subagent_run_start",
    ]
    activation_owner_id: str
    segment_generation: int = Field(ge=1)
    activation_fingerprint: str


class ModelCallControlDownstreamPredicateFact(BaseModel):
    schema_version: Literal["model_call_control_downstream_predicate.v1"]
    predicate_code: Literal[
        "capability_gate_decision",
        "tool_rollout_reservation",
        "tool_execution_suspended",
        "tool_result_terminal",
        "run_end_normal",
        "run_end_user_stop",
        "run_end_host_teardown",
        "run_end_execution_failure",
        "run_end_recovered_interrupted",
    ]
    event_type: str
    event_schema_version: str
    event_variant_contract_fingerprint: str
    required_prior_disposition_policy: Literal[
        "accepted_only",
        "accepted_or_termination_suppressed",
        "accepted_or_recovery_suppressed",
    ]
    predicate_fingerprint: str


class ModelCallControlDownstreamPredicateContractFact(BaseModel):
    schema_version: Literal["model_call_control_downstream_contract.v1"]
    contract_id: str
    contract_version: str
    predicates: tuple[ModelCallControlDownstreamPredicateFact, ...]
    control_event_domain_registry_fingerprint: str
    contract_fingerprint: str


class ModelStreamRecoveryPlanFact(BaseModel):
    schema_version: Literal["model_stream_recovery_plan.v1"]
    lifecycle_kind: Literal[
        "main_assistant_reply",
        "direct_internal_call",
        "window_compaction_summary",
    ]
    model_call_start_event_id: str
    stable_model_call_end_event_id: str
    reply_start_event_id: str | None
    stable_reply_end_event_id: str | None
    reservation_id: str | None
    reservation_quote_fingerprint: str | None
    stable_settlement_event_id: str | None
    window_compaction_started_event_id: str | None
    run_execution_activation: RunExecutionActivationFact | None
    control_downstream_predicate_contract: (
        ModelCallControlDownstreamPredicateContractFact | None
    )
    recovery_plan_fingerprint: str


@dataclass(frozen=True, slots=True)
class ModelLifecycleStartCommitBundle:
    resolved_model_call_id: str
    lifecycle_kind: Literal[
        "main_assistant_reply",
        "direct_internal_call",
        "window_compaction_summary",
    ]
    reply_id: str | None
    stable_reply_start_event_id: str | None
    stable_reply_end_event_id: str | None
    rollout_accounting_mode: Literal[
        "root_account",
        "child_subaccount",
        "not_rollout_accounted",
    ]
    expected_rollout_account_state_fingerprint: str | None
    reservation_quote: ModelCallReservationQuoteFact | None
    recovery_plan: ModelStreamRecoveryPlanFact
    companion_candidates: tuple[FrozenEventWriteCandidate, ...]
    bundle_fingerprint: str


class PreparedLongHorizonModelStep(BaseModel):
    run_id: str
    window: ContextWindowFact
    projection_state_fingerprint: str
    rollout_state: RolloutBudgetStateFact
    subagent_graph_semantic_source: SubagentGraphSemanticSourceFact
    subagent_graph_acceleration: SubagentGraphAccelerationFact
    context_snapshot: ContextFactSnapshot
    transcript: TranscriptCompileInput
    tool_result_units: tuple[ToolResultRenderUnit, ...]
    prepared_rollup_units: tuple[PreparedObservationRollupUnit, ...]
    prepared_candidates: PreparedContextCandidateSet
    context_budget_decision: LongHorizonContextBudgetDecisionFact
    model_call_reservation_quote: ModelCallReservationQuoteFact
    rollout_reservation: RolloutReservationFact
    model_lifecycle_start_commit_bundle: ModelLifecycleStartCommitBundle
    context_manifest: ContextCompileInputManifestFact
    compiled_context: LLMContext
    safe_point_revision: int
    compile_attempt_index: int
```

该DTO是owned deep copy/immutable fact carrier，不包含live EventLog、supervisor、registry或mutable cache port。execution handles继续由CommittedRunExecutionOwner持有。
`FrozenEventWriteCandidate`只能由serialization registry factory构造；三项schema/domain identity必须与event type及payload对应的exact registry contract
一致，并进入stable confirmation比较。Writer不得在insert时用“当前latest schema”覆盖candidate identity。
`model_call_reservation_quote.quote_fact_fingerprint`必须等于
`rollout_reservation.model_call_reservation_quote.quote_fact_fingerprint`及start bundle quote；call ID、amount、target与policy任一漂移均在进入
LLMRuntime前fail closed。

`RunExecutionActivationFact`是process-local segment的event-safe投影，而不是把ABA token持久化。它在start bundle freeze时从exact
`RunExecutionSegmentOwner`复制durable activation owner kind/ID与当次`segment_generation`；随机`segment_id`仍只留在进程内guard。Main lifecycle
必须保存非空activation与完整`ModelCallControlDownstreamPredicateContractFact`，且该generation必须等于当时active segment；direct/window lifecycle两者
必须为空。Activation fact进入recovery plan、control
disposition与audit join，但不进入model target、context payload或其他模型语义fingerprint。这样SESSION_REOPEN可以从ModelCallStart本身恢复相同
activation attribution，不需要伪造已经丢失的Python segment ID。

Downstream predicate contract是model-call控制因果的versioned历史binding，不是reopen时读取“当前代码的一组if”。它按event type/schema及variant contract
冻结哪些事实只能在某种disposition之后出现，并进入recovery-plan fingerprint。V1 predicate set是上述DTO枚举的封闭集合，
`control_event_domain_registry_fingerprint`必须精确绑定该集合；历史Start按自己冻结的contract恢复。属于model-control-downstream domain但不在
该historical contract中的event fail closed，禁止当作无关事件忽略。V1只接受上述已经定义、已经注册并有canonical writer的事件变体；不预留尚未定义的
downstream event类别或抽象predicate extension point。
Contract canonical validator要求predicate code唯一、`event_variant_contract_fingerprint`由声明式字段条件重算、V1五种RunEnd terminal matrix均有exact
entry，且所有accepted permit派生的gate/reservation/suspension/result facts均已登记。改变event归类、variant字段条件或required-prior policy必须同时升级
contract version与fingerprint；不允许只改当前registry实现。该contract只影响control/recovery因果审计，不进入provider-visible payload semantic identity。

### 17.4 Blocked union

```python
PrepareLongHorizonModelStepResult = (
    PreparedLongHorizonModelStep
    | LongHorizonModelStepBlocked
)


class LongHorizonModelStepBlocked(BaseModel):
    run_id: str
    stage: LongHorizonPreparationStage
    reason_code: str
    durable_mutation_outcome: DurableRunExistence
    retryable: bool
    diagnostic: LongHorizonDiagnosticFact
```

ledger latch、partial/unknown commit、pending compaction terminal和unsettled reservation一律blocked；不得以普通ContextBudgetExceeded降格继续provider call。

### 17.5 并发与ownership

- 同一durable run一次最多一个model-step preparation owner；
- child run有自己的coordinator owner，但共享parent rollout account通过atomic reservation reducer协调；
- projection rewrite以window generation CAS；
- compaction以active window ID CAS；
- stop/close先安装run termination intent，再取消preparation owner；
- FULL durable mutation后取消必须完成fold/terminal obligation；
- pending sync tool worker、MCP pending interaction存在时不得开始window compaction；
- user-facing stream cancellation默认detach observer，不隐式释放durable owner。

---

## 18. Context Compiler Input与exact replay接线

### 18.0 Schema hard cut

本章把：

```text
ContextInputIdentityFact.schema_version: context-input:v1 -> context-input:v2
ContextCompileInputManifestFact.schema_version: context-input-manifest:v1 -> context-input-manifest:v2
```

并同步升级snapshot semantic/fact fingerprint domain。旧v1 manifest不由production reader推断window/projection/account；开发数据库
reset或一次性显式migration。`ContextCompiledEvent.input_audit.input_manifest_schema_version`同步required为v2。

### 18.1 Snapshot新增单一long-horizon attribution

```python
class LongHorizonContextAttributionFact(BaseModel):
    run_contract_fingerprint: str
    window_id: str
    window_generation: int
    window_semantic_fingerprint: str
    projection_generation: int
    projection_state_fingerprint: str
    projection_rewrite_event_refs: tuple[ContextEventReferenceFact, ...]
    rollout_account_id: str
    rollout_account_owner_runtime_session_id: str
    rollout_state_through_sequence: int
    rollout_phase: RolloutPhase
    rollout_state_fingerprint: str
    subagent_graph_semantic_source: SubagentGraphSemanticSourceFact
    budget_decision: LongHorizonContextBudgetDecisionFact
    summary_artifact_id: str | None
    summary_content_sha256: str | None
    attribution_fingerprint: str
```

generation=1时summary两字段都为空；generation>1时两者必填并与window/Completed event精确一致。

`ContextFactSnapshotFact`和`ContextCompileInputManifestFact`都required保存此fact。不能分散到scratchpad多个key，也不能从
`ContextCompiledEvent.metadata`恢复。Checkpoint acceleration不属于该attribution semantic fact；只有manifest另存
`SubagentGraphAccelerationFact`作为operational audit。

### 18.2 Transcript projection input

`TranscriptCompileInput`仍保存canonical message order与pair refs，但tool result正文来源改为projection：

```python
class ProjectedToolResultCompileRefFact(BaseModel):
    transcript_message_id: str
    block_index: int
    tool_call_id: str
    tool_result_unit_id: str
    window_id: str
    projection_generation: int
    projected_fragment_fingerprint: str
    representation: ToolObservationRepresentation
    rollup_id: str | None
```

prepare和compiler两道边界验证：

- ref.tool_call_id == transcript pair tool_call_id；
- unit.tool_call_id == ref.tool_call_id；
- projection.unit_id == ref.unit_id；
- fragment source unit与projection一致；
- message/block position一致；
- window/projection generation与snapshot一致；
- source result sequence不超过canonical high-water。

`PreparedObservationRollupUnit`是rollup进入pure compiler的唯一carrier。Preparation必须先按renderer contract物化artifact，验证ordered member
set、ordering anchor与runtime-derived carrier contract，再把prepared unit传给compiler；compiler禁止持有renderer registry或artifact store。
Lowering时它在最后member所属完整pair group已经lower后创建独立`runtime_observation` message；ordering anchor只决定插入位置，不贡献
tool_call/result attribution。Ref/pair/unit/member/anchor/carrier任一不一致fail closed。Compiler或adapter把rollup追加到某个tool-result body、附加
tool_call_id或降成user role均属于architecture violation。

### 18.3 Candidate ingress边界

Long-Horizon不提前完成ContextSource Ownership Hard Cut。现有collector仍可产最小typed `ContextSectionCandidate`，但新增两个内建candidate：

```python
class ContextWindowSummaryCandidateFact(BaseModel):
    window_id: str
    source_window_id: str
    summary_artifact_id: str
    summary_content_sha256: str
    source_compaction_event_id: str
    semantic_fingerprint: str
```

`LongHorizonRolloutStatusCandidateFact`使用第16节唯一DTO。phase不是`exploration`时它是required system/control fact；处于
`exploration`但存在exact recurrence时它是optional informational fact；二者都由typed facts确定性渲染。不能让模型在finalization阶段仍看到
旧exploration权限说明。该candidate production ingress从L5才启用；L0B–L4只有non-model-visible shadow projection。summary candidate只在
generation>1时存在。

### 18.4 Compiler lowering顺序

同一snapshot下固定顺序：

1. static instruction；
2. capability/permission/plan/runtime candidates；
3. current context-window summary（若有）；
4. prior retained transcript；
5. projected tool call/result fragments；
6. prepared rollup derived observation（完整pair group完成后插入独立runtime-owned inert carrier）；
7. current user/current-run protected tail；
8. long-horizon rollout status candidate（按第16节触发）；
9. provider tool schemas。

最终顺序仍必须满足provider assistant/tool pairing。candidate priority只影响non-transcript allocation，不能跨越tool call/result结构重排。

### 18.5 Manifest内容

Manifest新增：

- active window fact/reference；
- window summary artifact hash；
- projection state fingerprint与所有active unit projection summaries；
- rollup facts、hard-cut `ResolvedModelTargetFact`中的完整runtime-observation carrier fact、renderer/carrier contract fingerprints、prepared inline/artifact
  hashes、ordered member set与ordering anchors；
- rollout state/phase；
- rollout status hint candidate与recurrence source refs（若有）；
- subagent graph semantic source fact；
- 当次checkpoint+delta acceleration fact（明确排除出input semantic aggregate）；
- context budget decision；
- L1–L3期间的non-semantic projection pressure shadow（L4 hard cut后production不再写）；
- bounded safe-point revision history与compile attempt history（分开计数）；
- applied rewrite/compaction/phase event refs；
- exact estimator fingerprint。

大的projection list可写独立manifest child artifact，但root manifest必须保存child artifact ID/hash/count与canonical order。不得仅保存“generation=3”然后replay查询当前最新generation。

Manifest同时冻结两个不同fingerprint domain：

```text
manifest_audit_fingerprint
    = hash(全部manifest bytes，包含SubagentGraphAccelerationFact)

semantic_input_fingerprint
    = hash(snapshot semantic fact、transcript、tool units、prepared rollup units、prepared candidates、
           graph semantic source、window/projection/rollout与compiler policy)
    # 明确不包含checkpoint ID/high-water、delta range/count、rebased、I/O timing/cache diagnostic
    # L1–L3 projection pressure shadow同样属于operational diagnostic，不进入semantic hash
```

Retry同一manifest candidate必须复用原audit bytes；新一次exact replay可以使用不同acceleration恢复同一semantic input，不重写历史manifest。
`ContextCompiledEvent.input_audit.manifest_artifact_id/content_sha256`属于fact attribution；`context_id`、candidate semantic fingerprint、
compiler semantic input fingerprint与provider-neutral payload fingerprint不得递归吸收manifest artifact identity。

### 18.6 Exact replay算法

```text
read ContextCompiledEvent input audit
-> confirm manifest artifact
-> freeze manifest-declared authority high-water
-> read versioned RawStoredEventEnvelope snapshot without current-union decode
-> resolve historical schema/domain/projector bindings
-> try manifest preferred checkpoint only if contract/artifact/delta bounds all acceptable
-> otherwise enumerate every compatible checkpoint through requested H by through_sequence desc
-> restore bounded delta to the manifest acceleration ledger high-water
-> rebuild window chain at that high-water
-> rebuild projection state at declared generation
-> rebuild rollout state
-> rebuild snapshot/transcript/tool units/candidate selection
-> compare all semantic fingerprints
-> rerun compiler with same estimator/policy
-> compare context_id-independent payload fingerprint
```

分类：

- `exact_replay`：所有semantic join与payload一致；允许checkpoint acceleration rebase；
- `fact_replay_only`：facts一致，但provider materialization依赖的非semantic process-local adapter不可用；
- `artifact_missing`：manifest/summary/rollup artifact缺失，或checkpoint artifact无任何bounded compatible rebase路径；
- `contract_mismatch`：连续可信ledger重建结果与manifest不同、renderer/contract version无法rebind；
- `ledger_untrusted`：gap、partial batch、inconsistent reducer、unknown confirmation。

Projection/selection漂移属于`contract_mismatch`，不得误报成ledger corruption。
原checkpoint artifact缺失但rebase后semantic source、graph、selection、candidate与payload均一致时仍是`exact_replay`；Inspector只把
acceleration变化标记为`rebased`。Reducer ID/version/contract fingerprint无法rebind属于`contract_mismatch`，不是checkpoint cache miss。

### 18.7 Provider payload最终校验

LLMRuntime pre-send继续执行ResolvedModelCall validator，并额外验证：

- context long-horizon attribution fingerprint匹配manifest；
- active window仍是prepared window；
- rollout reservation仍active且属于call ID；
- phase未被并发推进到更严格阶段；
- final estimated input等于compiler report；
- provider payload不含remote context/truncation入口；
- adapter不得执行silent omission、fallback model或provider-side truncation。

若phase在prepare后被child usage并发推进：当前call reservation已在旧phase合法commit时可继续；尚未commit reservation则重新prepare。

### 18.8 Pre-manifest failure audit

`ContextCompileFailureStage`新增`long_horizon_preparation`。checkpoint restore、state rebuild、projection planning、rollout admission或window
compaction失败发生在Context Input Manifest前时：

- ledger仍可信：写`ContextCompiledEvent(status="failed")` + `ContextCompileInputFailureFact`，available fingerprints中保存已形成的window/
  projection/account/status-hint identity与`LongHorizonPreparationStage`；
- stable window compaction Failed等已有更具体terminal event时，input failure引用其event ID，不复制自由文本；
- ledger PARTIAL/UNKNOWN/untrusted：禁止继续向同一ledger写“解释性失败”，只设置session latch并由Inspector process-local diagnostic展示；
- input failure outer call/index/context identity继续满足Stage 3 validator；不得只写RunEnd而没有compile input failure audit。

---

## 19. Durable commit、CAS与publication协议

### 19.1 所有candidate先稳定生成

以下事件/ifacts在第一次write前预生成稳定ID与canonical payload：

- checkpoint artifact/event；
- projection rewrite pages/rollup artifacts；
- rollout account/reservation/settlement/phase events；
- window compaction Started/terminal/summary artifact/open/close；
- RunStart initial window/account；
- RunEnd window close；
- Context Input Manifest。

不得在retry中重新生成UUID、summary ID或page split。

### 19.2 通用commit classification

```python
class DurableBatchCommitClassification(str, Enum):
    NONE = "none"
    FULL = "full"
    CONFLICT = "conflict"
    PARTIAL_UNTRUSTED = "partial_untrusted"
    UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True)
class ConfirmedCommittedBatch:
    committed_events: tuple[FrozenStoredEvent, ...]
    committed_through_sequence: int
    batch_fingerprint: str


@dataclass(frozen=True, slots=True)
class DurableBatchConfirmation:
    classification: DurableBatchCommitClassification
    confirmed_batch: ConfirmedCommittedBatch | None
    diagnostic_code: str | None
```

`classification == FULL`时`confirmed_batch`必填；其余状态必须为空。这样commit acknowledgement丢失后，owner仍能从confirmation取得真实
committed events/high-water并完成fold/publisher handoff，不能只得到一个bool再用candidate伪造sequence。

围绕每个commit捕获所有`BaseException`。使用`confirm_batch(stable_candidates)`逐ID读取并比较完整canonical payload：

- 无candidate存在：NONE；
- 全部存在、连续、payload相同：FULL；
- 同ID不同payload：CONFLICT；
- 部分存在/不连续：PARTIAL_UNTRUSTED；
- confirmation读取失败/超时：UNKNOWN。

不能只按ID判断FULL；不能把duplicate ID一律当幂等成功。

### 19.3 Outcome处理

| outcome | state mutation | retry/owner | 后续model call |
|---|---|---|---|
| NONE | 不fold | 保留stable candidate，可重试 | 禁止直到所需mutation完成或明确放弃 |
| FULL | fold committed facts | ack pending owner | 可按新state继续 |
| CONFLICT | latch contract conflict | 保留诊断/物理owner | 禁止 |
| PARTIAL_UNTRUSTED | ledger structural latch | close/reopen或专门ledger repair | 禁止 |
| UNKNOWN | reconciliation latch | service继续bounded confirm | 禁止 |

普通reducer rebuild不能清ledger structural latch。

### 19.4 Publication failure

`EventPublicationAfterCommitError`与cancel-during-publication都先按stable candidates confirm。FULL时：

1. fold committed events；
2. 更新window/projection/rollout ownership；
3. acknowledgement pending artifacts/audits；
4. 完成或转移resource lease；
5. 再向上传播publication failure/cancellation。

不能因为Python await失败而回滚已经durable的phase或释放错误的owner。

### 19.5 关键atomic batches

必须保持：

```text
Host RunStart
  + ContextWindowOpened(initial)
  + RolloutBudgetAccountOpened
  + pending MCP installation audits

Child RunStart
  + ContextWindowOpened(initial)
  + inherited reservation receipt inside RunStart
    (referenced parent reservation was already FULL in parent ledger)

Projection rewrite
  + all rewrite pages
  + any phase transition caused by the same frozen state (if coupled)

Tool execution admission
  + CapabilityGateDecisionEvent(allow)
  + RolloutBudgetReservationCreatedEvent(tool_call)

Tool pre-execution deny
  + CapabilityGateDecisionEvent(deny)
  + typed ToolResultEndEvent(denied)
  (no reservation / no settlement)

Tool execution terminal
  + ToolResultEndEvent
  + matching RolloutBudgetReservationSettledEvent(tool_terminal)

MCP resume terminal / deny / cancel / timeout
  + terminal ToolResultEndEvent
  + settlement of the original suspended reservation

Main model/reply start
  + ReplyStart
  + matching rollout reservation
  + ModelCallStart

Main model/reply terminal
  + ModelCallEnd
  + matching rollout settlement
  + ReplyEnd

Window summarizer start
  + matching rollout reservation
  + ContextWindowCompactionStarted
  + ModelCallStart

Window summarizer terminal
  + ModelCallEnd
  + matching rollout settlement

Non-rollout direct model start/terminal
  + ModelCallStart (start batch)
  + ModelCallEnd (separate terminal batch)
  -- no Reply lifecycle, no rollout reservation

Window compaction success
  + Completed
  + old WindowClosed
  + new WindowOpened

Window compaction failure after Started
  + ModelCallEnd(cancelled/runtime_error) + matching settlement (LLM lifecycle terminal batch)
  + ContextWindowCompactionFailed (subsequent window terminal fact)

Run terminal
  + final reservation settlement(s)
  + active WindowClosed
  + root AccountClosed or child SubaccountClosed
  + RunEnd

Parent child terminal
  + parent rollout settlement
  + graph terminal/handoff fact

Independent child batch start
  + every child graph start fact (referencing an already FULL budget snapshot)
  + every SubagentRolloutBudgetResolvedEvent
  + every matching parent RolloutBudgetReservationCreatedEvent
  (all-or-none; dependency-waiting children are excluded until actual start)
```

Reply/Model lifecycle与matching reservation/settlement不能拆批；这是LLMRuntime hard-cut API的一部分，不保留compat outbox或caller二次
emit路径。Start FULL后的live取消由同一`ModelStreamExecutionHandle`生成typed cancelled End并完成matching terminal batch；进程崩溃会失去live
handle，必须由20.5.1节`ModelStreamRecoveryService`使用Start-frozen recovery plan生成runtime-error End，不能假装“同一个process owner仍存在”。

### 19.6 完整model stream的唯一持久化owner

继续遵守ResolvedModelCall hard cut，但ownership扩大到完整model stream：`LLMRuntime`负责持久化
`ReplyStartEvent / ModelCallStartEvent / provider semantic events / ModelCallEndEvent / ReplyEndEvent`。AgentRuntime、direct collector、window
compaction service只消费committed notifications，不得持久化任何model-stream event。Transport只产process-local semantic/terminal measurement
drafts，不接触
EventLog。低层port相应改名为完整stream port：

```python
@dataclass(frozen=True, slots=True)
class ModelStreamStartCommitGuard:
    resolved_model_call_id: str
    stable_model_call_start_event_id: str
    lifecycle_kind: Literal[
        "main_assistant_reply",
        "direct_internal_call",
        "window_compaction_summary",
    ]
    recovery_plan_fingerprint: str
    rollout_accounting_mode: Literal[
        "root_account",
        "child_subaccount",
        "not_rollout_accounted",
    ]
    expected_rollout_account_state_fingerprint: str | None
    reservation_id: str | None
    reservation_quote_fingerprint: str | None


@dataclass(frozen=True, slots=True)
class ModelStreamSemanticCommitGuard:
    resolved_model_call_id: str
    model_call_start_event_id: str
    transport_sequence_index: int
    expected_previous_semantic_event_id: str | None


@dataclass(frozen=True, slots=True)
class ModelStreamTerminalCommitGuard:
    resolved_model_call_id: str
    model_call_start_event_id: str
    stable_model_call_end_event_id: str
    lifecycle_kind: Literal[
        "main_assistant_reply",
        "direct_internal_call",
        "window_compaction_summary",
    ]
    stable_reply_end_event_id: str | None
    stable_settlement_event_id: str | None
    expected_last_semantic_event_id: str | None
    semantic_item_count: int
    rollout_accounting_mode: Literal[
        "root_account",
        "child_subaccount",
        "not_rollout_accounted",
    ]
    reservation_id: str | None
    reservation_quote_fingerprint: str | None


class ModelStreamEventCommitPort(Protocol):
    async def commit_start(
        self,
        *,
        candidates: tuple[FrozenEventWriteCandidate, ...],
        guard: ModelStreamStartCommitGuard,
    ) -> ConfirmedCommittedBatch: ...

    async def commit_semantic(
        self,
        *,
        candidates: tuple[FrozenEventWriteCandidate, ...],
        guard: ModelStreamSemanticCommitGuard,
    ) -> ConfirmedCommittedBatch: ...

    async def commit_terminal(
        self,
        *,
        candidates: tuple[FrozenEventWriteCandidate, ...],
        guard: ModelStreamTerminalCommitGuard,
    ) -> ConfirmedCommittedBatch: ...

    async def confirm_model_stream_batch(
        self,
        candidates: tuple[FrozenEventWriteCandidate, ...],
    ) -> DurableBatchConfirmation: ...

    async def handoff_committed_model_stream(
        self,
        batch: ConfirmedCommittedBatch,
    ) -> None: ...
```

Port负责EventLog atomic CAS/write、stable full-payload confirmation，以及把FULL batch交给同一RuntimeSession的ordered publisher与committed
reducers；它不生成event、不重算charge、不持有transport。Port由composition root按durable runtime session构造，所有production call显式
传入；全局LLMRuntime不得暗取“当前session writer”。

三种commit的CAS语义不得合并成一个可空fingerprint参数：

- `commit_start()`：先验证lifecycle kind与完整recovery plan fingerprint；accounted call要求expected account fingerprint、matching reservation ID与quote
  fingerprint全非空，CAS当前account state并原子创建reservation；direct/not-accounted三者必须全空。Start已存在或terminal已存在均拒绝；
- `commit_semantic()`：只验证matching Start已FULL、terminal不存在、semantic cursor连续以及stable event payload；**绝不读取或CAS rollout account**。
  因此stream期间并发child/tool settlement不会使semantic commit误失败；
- `commit_terminal()`：验证matching Start recovery plan、lifecycle-specific End/settlement/ReplyEnd stable IDs、exact active reservation ID/quote与
  terminal absence。Port在每次尝试中读取**最新**account state，并在该
  latest state上CAS settlement；无关child/tool settlement导致CAS miss时只刷新account state并重试同一stable terminal bytes，不复用start时的旧
  fingerprint，也不重新生成terminal ID。Direct call要求reservation fields为空且不访问account；
- common `confirm_model_stream_batch()`按stable raw candidates分类NONE/FULL/CONFLICT/PARTIAL/UNKNOWN；confirmation不把三种CAS语义混成一个
  write API。

Terminal event/settlement payload不得嵌入一次易漂移的全局account-before fingerprint；它绑定exact reservation/quote，最终fold顺序由实际committed
sequence决定。否则并发无关settlement会迫使同event ID改payload。

公开观察面只包含已经FULL commit并完成handoff的notifications，不暴露需要caller acknowledgement的单向candidate。真正stream由
RuntimeSession-owned execution handle独立驱动：

```python
@dataclass(frozen=True, slots=True)
class CommittedModelStreamNotification:
    notification_kind: Literal["lifecycle", "provider_semantic"]
    committed_events: tuple[FrozenStoredEvent, ...]
    batch_fingerprint: str
    through_sequence: int
    notification_fingerprint: str


class ModelStreamSubscriptionCloseReason(StrEnum):
    TERMINAL_OBSERVED = "terminal_observed"
    DETACHED_BY_CALLER = "detached_by_caller"
    OBSERVER_LAGGED = "observer_lagged"
    OWNER_CANCELLED = "owner_cancelled"
    RECONCILIATION_BLOCKED = "reconciliation_blocked"


@dataclass(frozen=True, slots=True)
class ModelStreamSubscriptionClosed:
    close_reason: ModelStreamSubscriptionCloseReason
    last_confirmed_sequence: int | None
    terminal_sequence: int | None
    can_resume_from_cursor: bool


class ModelStreamSemanticAttributionFact(BaseModel):
    schema_version: Literal["model_stream_semantic_attribution.v1"]
    resolved_model_call_id: str
    model_call_start_event_id: str
    transport_sequence_index: int = Field(ge=0)
    draft_schema_version: Literal["provider_transport_semantic_draft.v1"]
    draft_kind: Literal[
        "text_block_start",
        "text_block_delta",
        "text_block_end",
        "thinking_block_start",
        "thinking_block_delta",
        "thinking_block_end",
        "data_block_start",
        "data_block_delta",
        "data_block_end",
        "tool_call_start",
        "tool_call_delta",
        "tool_call_end",
        "provider_error",
    ]
    draft_fingerprint: str
    attribution_fingerprint: str


class ProviderModelStreamErrorCode(StrEnum):
    AUTHENTICATION_FAILED = "authentication_failed"
    PERMISSION_DENIED = "permission_denied"
    INVALID_REQUEST = "invalid_request"
    RATE_LIMITED = "rate_limited"
    PROVIDER_OVERLOADED = "provider_overloaded"
    MODEL_UNAVAILABLE = "model_unavailable"
    PROVIDER_TIMEOUT = "provider_timeout"
    CONTENT_FILTERED = "content_filtered"
    TRANSPORT_PROTOCOL_ERROR = "transport_protocol_error"
    UNKNOWN_PROVIDER_ERROR = "unknown_provider_error"


class ProviderErrorSanitizationContractFact(BaseModel):
    schema_version: Literal["provider_error_sanitization_contract.v1"]
    contract_id: str
    contract_version: str
    stable_code_mapping_fingerprint: str
    sensitive_key_policy_fingerprint: str
    secret_pattern_policy_fingerprint: str
    url_redaction_policy_fingerprint: str
    diagnostic_attribute_allowlist_fingerprint: str
    max_message_chars: int = Field(ge=1)
    max_diagnostic_count: int = Field(ge=0)
    max_diagnostic_attribute_chars: int = Field(ge=1)
    contract_fingerprint: str


class ProviderSanitizedDiagnosticKind(StrEnum):
    PROVIDER_STATUS = "provider_status"
    PROVIDER_CODE = "provider_code"
    PROVIDER_REQUEST_ID = "provider_request_id"
    RETRY_AFTER = "retry_after"
    TRANSPORT_ENDPOINT = "transport_endpoint"
    ADAPTER_CONTEXT = "adapter_context"


class ProviderSanitizedDiagnosticFact(BaseModel):
    diagnostic_kind: ProviderSanitizedDiagnosticKind
    attributes: FrozenJsonObjectFact
    redaction_count: int = Field(ge=0)
    truncated: bool
    diagnostic_fingerprint: str


class ProviderSanitizedErrorFact(BaseModel):
    schema_version: Literal["provider_sanitized_error.v1"]
    code: ProviderModelStreamErrorCode
    message: str
    diagnostics: tuple[ProviderSanitizedDiagnosticFact, ...]
    redaction_count: int = Field(ge=0)
    truncated: bool
    sanitization_contract: ProviderErrorSanitizationContractFact
    error_fingerprint: str


class ProviderModelStreamErrorEvent(EventBase):
    type: Literal[EventType.PROVIDER_MODEL_STREAM_ERROR] = (
        EventType.PROVIDER_MODEL_STREAM_ERROR
    )
    model_stream_attribution: ModelStreamSemanticAttributionFact
    error: ProviderSanitizedErrorFact


class CommittedModelTextBlockFact(BaseModel):
    block_id: str
    text: str
    start_sequence: int
    end_sequence: int | None
    completion_status: Literal["completed", "interrupted"]


class CommittedModelThinkingBlockFact(BaseModel):
    block_id: str
    text: str
    start_sequence: int
    end_sequence: int | None
    completion_status: Literal["completed", "interrupted"]


class CommittedModelDataBlockFact(BaseModel):
    block_id: str
    media_type: str
    data: str
    start_sequence: int
    end_sequence: int | None
    completion_status: Literal["completed", "interrupted"]


class CommittedModelToolCallFact(BaseModel):
    tool_call_id: str
    tool_call_name: str
    raw_arguments_json: str
    start_sequence: int
    end_sequence: int | None
    completion_status: Literal["completed", "interrupted"]


class ModelCallResultControlDisposition(StrEnum):
    SUCCESS_ELIGIBLE = "success_eligible"
    AUDIT_ONLY = "audit_only"


class ModelCallControlDisposition(StrEnum):
    ACCEPTED = "accepted"
    SUPPRESSED_BY_TERMINATION = "suppressed_by_termination"
    SUPPRESSED_BY_RECOVERY = "suppressed_by_recovery"


class RunTerminationIntentAttributionFact(BaseModel):
    schema_version: Literal["run_termination_intent_attribution.v1"]
    intent_id: str
    kind: Literal["user_stop", "host_teardown"]
    requested_at_utc: str
    requester_id: str
    target_run_execution_activation: RunExecutionActivationFact
    attribution_fingerprint: str


class ModelCallControlDispositionResolvedEvent(EventBase):
    type: Literal[EventType.MODEL_CALL_CONTROL_DISPOSITION_RESOLVED] = (
        EventType.MODEL_CALL_CONTROL_DISPOSITION_RESOLVED
    )
    resolved_model_call_id: str
    model_call_start_event_id: str
    model_call_end_event_id: str
    model_call_index: int = Field(ge=1)
    source_result_fingerprint: str
    run_execution_activation: RunExecutionActivationFact
    disposition: ModelCallControlDisposition
    termination_intent: RunTerminationIntentAttributionFact | None
    recovery_reason_code: Literal[
        "process_restarted_before_control_resolution"
    ] | None
    event_fingerprint: str


class CommittedModelCallResult(BaseModel):
    schema_version: Literal["committed_model_call_result.v1"]
    resolved_model_call_id: str
    model_call_start_event_id: str
    model_call_start_sequence: int
    model_call_end_event_id: str
    model_call_end_sequence: int
    terminal_outcome: Literal[
        "completed",
        "provider_error",
        "cancelled",
        "runtime_error",
    ]
    control_disposition: ModelCallResultControlDisposition
    text_blocks: tuple[CommittedModelTextBlockFact, ...]
    combined_text: str
    thinking_blocks: tuple[CommittedModelThinkingBlockFact, ...]
    data_blocks: tuple[CommittedModelDataBlockFact, ...]
    tool_calls: tuple[CommittedModelToolCallFact, ...]
    provider_errors: tuple[ProviderSanitizedErrorFact, ...]
    usage_status: Literal["reported", "missing"]
    usage: ModelTokenUsageFact | None
    reported_model_id: str | None
    semantic_item_count: int
    source_through_sequence: int
    result_fingerprint: str


@dataclass(frozen=True, slots=True)
class SuccessfulCommittedModelCallControlView:
    resolved_model_call_id: str
    source_result_fingerprint: str
    combined_text: str
    completed_tool_calls: tuple[CommittedModelToolCallFact, ...]
    view_fingerprint: str


class ModelCallControlResultProjector(Protocol):
    def require_successful_control_view(
        self,
        result: CommittedModelCallResult,
    ) -> SuccessfulCommittedModelCallControlView: ...


@dataclass(frozen=True, slots=True)
class LiveModelCallControlDispositionCommitGuard:
    guard_kind: Literal["live_segment"]
    run_id: str
    run_execution_activation: RunExecutionActivationFact
    segment_id: str
    segment_generation: int
    resolved_model_call_id: str
    source_result_fingerprint: str
    expected_termination_intent_id: str | None
    guard_fingerprint: str


@dataclass(frozen=True, slots=True)
class RecoveryModelCallControlDispositionCommitGuard:
    guard_kind: Literal["session_reopen"]
    runtime_session_id: str
    run_id: str
    run_execution_activation: RunExecutionActivationFact
    resolved_model_call_id: str
    model_call_start_event_id: str
    model_call_end_event_id: str
    source_result_fingerprint: str
    reopen_high_water_sequence: int
    reopen_high_water_event_id: str
    reopen_high_water_payload_fingerprint: str
    control_downstream_predicate_contract_fingerprint: str
    expected_disposition_count: Literal[0]
    expected_matching_downstream_fact_count: Literal[0]
    matching_downstream_fact_accumulator: str
    recovery_scan_fingerprint: str
    guard_fingerprint: str


ModelCallControlDispositionCommitGuard = (
    LiveModelCallControlDispositionCommitGuard
    | RecoveryModelCallControlDispositionCommitGuard
)


class ModelCallControlDispositionCommitPort(Protocol):
    async def commit_and_confirm_resolution(
        self,
        *,
        candidate: FrozenEventWriteCandidate,
        guard: ModelCallControlDispositionCommitGuard,
    ) -> ConfirmedCommittedBatch: ...

    def fold_confirmed_resolution(
        self,
        *,
        confirmed: ConfirmedCommittedBatch,
        guard: ModelCallControlDispositionCommitGuard,
    ) -> "ModelCallControlDispositionFoldResult": ...

    async def publish_folded_resolution(
        self,
        *,
        folded: "ModelCallControlDispositionFoldResult",
    ) -> "ModelCallControlDispositionPublicationResult": ...


@dataclass(frozen=True, slots=True)
class ModelCallControlDispositionFoldResult:
    disposition_event_id: str
    disposition_sequence: int
    committed_payload_fingerprint: str
    reducer_state_fingerprint: str
    fold_fingerprint: str


@dataclass(frozen=True, slots=True)
class ModelCallControlDispositionPublicationResult:
    status: Literal["published", "observer_failed", "pending_retry"]
    disposition_event_id: str
    disposition_sequence: int
    diagnostic_code: str | None


@dataclass(frozen=True, slots=True)
class ModelCallControlPermit:
    disposition_event_id: str
    resolved_model_call_id: str
    source_result_fingerprint: str
    run_execution_activation_fingerprint: str
    segment_id: str
    segment_generation: int
    permit_fingerprint: str


@dataclass(frozen=True, slots=True)
class ModelCallControlResolutionResult:
    disposition_event: ModelCallControlDispositionResolvedEvent
    accepted_permit: ModelCallControlPermit | None
    publication: ModelCallControlDispositionPublicationResult


@dataclass(frozen=True, slots=True)
class ModelCallControlRecoveryReport:
    source_through_sequence: int
    repaired_disposition_event_ids: tuple[str, ...]
    existing_winner_event_ids: tuple[str, ...]
    reconciliation_blocker_code: str | None
    report_fingerprint: str


class ModelCallControlDispositionCoordinator(Protocol):
    async def resolve_main_call_control_disposition(
        self,
        *,
        result: CommittedModelCallResult,
        run_id: str,
        run_execution_activation: RunExecutionActivationFact,
        segment_id: str,
        segment_generation: int,
        deadline_monotonic: float,
    ) -> ModelCallControlResolutionResult: ...


class ModelCallControlDispositionRecoveryService(Protocol):
    async def repair_completed_calls_missing_disposition(
        self,
        *,
        runtime_session_id: str,
        reopen_high_water_sequence: int,
        deadline_monotonic: float,
    ) -> ModelCallControlRecoveryReport: ...


@dataclass(frozen=True, slots=True)
class ModelStreamCompletion:
    resolved_model_call_id: str
    terminal_commit: ConfirmedCommittedBatch | None
    terminal_outcome: Literal[
        "completed",
        "provider_error",
        "cancelled",
        "runtime_error",
        "rejected_before_start",
        "reconciliation_blocked",
    ]
    diagnostic_code: str | None
    completion_fingerprint: str


class ModelStreamNotificationSubscription(Protocol):
    def __aiter__(self) -> AsyncIterator[CommittedModelStreamNotification]: ...
    async def detach(self) -> ModelStreamSubscriptionClosed: ...
    async def wait_closed(self) -> ModelStreamSubscriptionClosed: ...


class ModelStreamExecutionHandle(Protocol):
    handle_id: str
    handle_generation: int
    resolved_model_call_id: str
    completion: asyncio.Future[ModelStreamCompletion]
    result: asyncio.Future[CommittedModelCallResult]

    def subscribe(
        self,
        *,
        after_sequence: int | None = None,
    ) -> ModelStreamNotificationSubscription: ...
    async def request_cancel(self, *, reason: Literal["user_stop", "host_teardown"]) -> None: ...
    async def wait_completed(self) -> ModelStreamCompletion: ...
    async def wait_result(self) -> CommittedModelCallResult: ...


class ModelStreamResultMaterializer(Protocol):
    async def materialize_committed_result(
        self,
        *,
        runtime_session_id: str,
        resolved_model_call_id: str,
        terminal_batch: ConfirmedCommittedBatch,
        deadline_monotonic: float,
    ) -> CommittedModelCallResult: ...


class ModelStreamWorkerControl(Protocol):
    async def wait_until_activated(self) -> None: ...
    def cancellation_reason(self) -> Literal["user_stop", "host_teardown"] | None: ...
    async def wait_cancellation_requested(
        self,
    ) -> Literal["user_stop", "host_teardown"]: ...
    def register_physical_operation(self, operation: asyncio.Future[Any]) -> str: ...
    def complete_physical_operation(self, operation_id: str) -> None: ...


class ModelStreamExecutionRegistry(Protocol):
    def install_and_start(
        self,
        *,
        handle_id: str,
        resolved_model_call_id: str,
        worker_factory: Callable[
            [ModelStreamWorkerControl],
            Coroutine[Any, Any, ModelStreamCompletion],
        ],
    ) -> ModelStreamExecutionHandle: ...

    async def drain_all(self, *, deadline_monotonic: float) -> None: ...


class ProviderSemanticDraftBase(BaseModel):
    schema_version: Literal["provider_transport_semantic_draft.v1"]
    transport_sequence_index: int
    draft_fingerprint: str


class ProviderTextBlockStartDraft(ProviderSemanticDraftBase):
    draft_kind: Literal["text_block_start"]
    block_id: str


class ProviderTextBlockDeltaDraft(ProviderSemanticDraftBase):
    draft_kind: Literal["text_block_delta"]
    block_id: str
    delta: str


class ProviderTextBlockEndDraft(ProviderSemanticDraftBase):
    draft_kind: Literal["text_block_end"]
    block_id: str


class ProviderThinkingBlockStartDraft(ProviderSemanticDraftBase):
    draft_kind: Literal["thinking_block_start"]
    block_id: str


class ProviderThinkingBlockDeltaDraft(ProviderSemanticDraftBase):
    draft_kind: Literal["thinking_block_delta"]
    block_id: str
    delta: str


class ProviderThinkingBlockEndDraft(ProviderSemanticDraftBase):
    draft_kind: Literal["thinking_block_end"]
    block_id: str


class ProviderDataBlockStartDraft(ProviderSemanticDraftBase):
    draft_kind: Literal["data_block_start"]
    block_id: str
    media_type: str


class ProviderDataBlockDeltaDraft(ProviderSemanticDraftBase):
    draft_kind: Literal["data_block_delta"]
    block_id: str
    media_type: str
    data: str


class ProviderDataBlockEndDraft(ProviderSemanticDraftBase):
    draft_kind: Literal["data_block_end"]
    block_id: str


class ProviderToolCallStartDraft(ProviderSemanticDraftBase):
    draft_kind: Literal["tool_call_start"]
    tool_call_id: str
    tool_call_name: str


class ProviderToolCallDeltaDraft(ProviderSemanticDraftBase):
    draft_kind: Literal["tool_call_delta"]
    tool_call_id: str
    delta: str


class ProviderToolCallEndDraft(ProviderSemanticDraftBase):
    draft_kind: Literal["tool_call_end"]
    tool_call_id: str


class ProviderErrorDraft(ProviderSemanticDraftBase):
    draft_kind: Literal["provider_error"]
    error: ProviderSanitizedErrorFact


ProviderTransportSemanticDraft = Annotated[
    ProviderTextBlockStartDraft
    | ProviderTextBlockDeltaDraft
    | ProviderTextBlockEndDraft
    | ProviderThinkingBlockStartDraft
    | ProviderThinkingBlockDeltaDraft
    | ProviderThinkingBlockEndDraft
    | ProviderDataBlockStartDraft
    | ProviderDataBlockDeltaDraft
    | ProviderDataBlockEndDraft
    | ProviderToolCallStartDraft
    | ProviderToolCallDeltaDraft
    | ProviderToolCallEndDraft
    | ProviderErrorDraft,
    Field(discriminator="draft_kind"),
]


class ProviderTransportTerminalDraft(BaseModel):
    schema_version: Literal["provider_transport_terminal_draft.v1"]
    outcome: Literal["completed", "provider_error"]
    usage: ModelTokenUsageFact | None
    usage_status: Literal["reported", "missing"]
    reported_model_id: str | None
    semantic_item_count: int = Field(ge=0)
    terminal_fingerprint: str


ProviderTransportStreamItem = (
    ProviderTransportSemanticDraft
    | ProviderTransportTerminalDraft
)


class ProviderTransportPhysicalCompletionStatus(StrEnum):
    COMPLETED = "completed"
    BLOCKED_UNTRUSTED = "blocked_untrusted"


@dataclass(frozen=True, slots=True)
class ProviderTransportPhysicalCompletion:
    status: ProviderTransportPhysicalCompletionStatus
    diagnostic_code: str | None
    completion_fingerprint: str


class ProviderTransportExecution(Protocol):
    async def read_next(self) -> ProviderTransportStreamItem | None: ...
    async def request_cancel(
        self,
        *,
        reason: Literal["user_stop", "host_teardown"],
    ) -> None: ...
    async def aclose(self) -> None: ...
    async def wait_physical_completion(
        self,
    ) -> ProviderTransportPhysicalCompletion: ...


class LLMTransport(Protocol):
    def open_stream(
        self,
        *,
        call: ResolvedModelCall,
        context: LLMContext,
    ) -> ProviderTransportExecution: ...


@dataclass(frozen=True, slots=True)
class ProviderErrorSanitizerBinding:
    contract: ProviderErrorSanitizationContractFact
    implementation_build_fingerprint: str
    sanitize_failure: Callable[..., ProviderSanitizedErrorFact]


class ProviderErrorSanitizerRegistry(Protocol):
    def resolve_binding(
        self,
        *,
        transport_binding_id: str,
        transport_contract_version: str,
    ) -> ProviderErrorSanitizerBinding: ...


class SanitizingProviderTransportState(StrEnum):
    OPEN = "open"
    ERROR_DRAFT_PENDING = "error_draft_pending"
    ERROR_DRAFT_DELIVERED = "error_draft_delivered"
    FAILURE_PHYSICAL_DRAIN = "failure_physical_drain"
    ERROR_TERMINAL_PENDING = "error_terminal_pending"
    TERMINAL_DELIVERED = "terminal_delivered"
    CANCELLING = "cancelling"
    PHYSICAL_DRAIN_BLOCKED = "physical_drain_blocked"
    CLOSED = "closed"


class SanitizingProviderTransportExecution(ProviderTransportExecution):
    # Public execution returned by every production transport binding.
    # The adapter-private raw execution is never exposed to LLMRuntime.
    state: SanitizingProviderTransportState
    sanitizer_binding: ProviderErrorSanitizerBinding
    next_transport_sequence_index: int
    pending_error_draft: ProviderErrorDraft | None
    pending_terminal_draft: ProviderTransportTerminalDraft | None


class SanitizingLLMTransport(LLMTransport):
    # Synchronous/no-I/O factory around one adapter-private raw transport.
    def open_stream(
        self,
        *,
        call: ResolvedModelCall,
        context: LLMContext,
    ) -> SanitizingProviderTransportExecution: ...

```

`ModelCallControlDispositionCommitPort`的三段API是强制ownership边界，不是可合并的便捷接口：

- `commit_and_confirm_resolution()`只操作durable ledger与stable confirmation，不得调用`RuntimeSession.emit()`这类会等待publisher/observer的高层入口；
- `fold_confirmed_resolution()`只把FULL committed bytes同步fold进matching run/control reducer，验证sequence/payload/guard并返回不可变fold result；
- `publish_folded_resolution()`才把已fold的sequence交给session-owned ordered publisher，并等待或登记observer notification owner。

两种guard同样是hard-cut边界：`LiveModelCallControlDispositionCommitGuard`只允许ACCEPTED或SUPPRESSED_BY_TERMINATION，必须对exact active
segment ID/generation与termination-intent CAS；`RecoveryModelCallControlDispositionCommitGuard`只允许SUPPRESSED_BY_RECOVERY，刻意不含随机
segment ID、termination intent或permit字段。Commit port按`guard_kind`验证candidate disposition，禁止live caller使用recovery guard绕过ABA，也禁止
reopen伪造live guard。

因此实现不得用一个“write-and-publish”方法同时满足前两段。Composition root必须注入可以在不触碰observer的ledger commit seam，以及独立的ordered
publication seam；测试/内存实现也不得把两者重新合并，否则死锁只会被测试helper隐藏。
`resolve_main_call_control_disposition()`在等待锁之前先按stable event ID注册session-owned resolution attempt；由该worker取得/释放锁并完成前三段，
API caller只通过`asyncio.shield()`等待shared Future。Caller cancellation不会取消持锁worker，也不会造成“ledger write继续、锁却提前释放”的半所有权。
Host close必须drain这个attempt及其publication owner；若ledger阶段无法bounded收口则保留run/session blocker，若只剩observer notification则按
operational publication policy收口，不回滚control authority。

`ProviderTransportSemanticDraft`是`ProviderTransportExecution.read_next()`与LLMRuntime之间的process-local、versioned discriminated union。每个variant与
`TextBlock* / ThinkingBlock* / DataBlock* / ToolCall* / ProviderModelStreamErrorEvent`恰好一一映射；LLMRuntime不得对
`draft_kind: str + arbitrary JSON`做动态
推断。Unknown discriminator、variant字段不匹配、空required delta、生命周期式draft或同一block/tool-call的非法start/delta/end迁移都在transport
boundary fail closed。`ProviderErrorDraft`只接受中央wrapper生成的`ProviderSanitizedErrorFact`；transport不得把provider exception、response
body、headers或任意JSON原样塞进draft。**只有中央`SanitizingProviderTransportExecution`可以创建
`ProviderErrorDraft`**；具体OpenAI/DeepSeek/compatible adapter的raw execution既不能直接把error draft交给LLMRuntime，也不能成为production registry
返回值。LLMRuntime在生成event candidate前按resolved transport binding ID/version取得**唯一**sanitizer binding，再把
draft中的full contract与registry expected contract逐项比较并重算error/diagnostic/contract fingerprint；binding缺失、同一transport ID/version注册不同
sanitizer contract fingerprint或payload未通过contract validator均fail closed。Caller/draft不能通过传入另一个fingerprint选择较宽松的sanitizer。

Composition root的唯一production wiring是：

```text
adapter-private raw factory/execution
  -> central SanitizingLLMTransport
  -> SanitizingProviderTransportExecution
  -> public LLMTransport / ProviderTransportExecution protocol
  -> LLMRuntime
```

`LLMTransportRegistry`只接受带exact sanitizer binding与boundary contract fingerprint的`SanitizingLLMTransport`；直接注册adapter raw transport、
provider SDK iterator或自定义`ProviderTransportExecution`必须在Host可用前fail closed。Raw protocol/type只能位于adapter-private module，
`llm/runtime.py`、AgentRuntime、direct caller与tests/support production fixture均不得import。每个raw adapter仍可有自己的SDK exception mapping hint，
但它只把瞬时raw value交给中央wrapper，不能决定durable error code、redaction bounds或event shape。Provider以stream frame返回的structured error也先
进入adapter-private raw failure capture，再由同一`sanitize_failure`路径处理；禁止因“它不是Python exception”而直接构造另一种public error draft。
`SanitizingLLMTransport.open_stream()`还必须包住adapter-private raw factory：factory构造失败时返回初始状态为`ERROR_DRAFT_PENDING`的pre-failed
sanitizing execution，而不是抛异常。Factory不得执行network I/O；配置/credential binding无效本应在target resolution失败，若仍迟到到此处则使用固定
`TRANSPORT_PROTOCOL_ERROR`并按started-call的provider-error lifecycle收口。

脱敏发生在**adapter boundary最先接触原始provider error的位置**，顺序冻结为：结构化提取 → stable code mapping → recursive redaction → URL
redaction → secret-pattern redaction → bounded allowlist diagnostics → message/attribute/count truncation → canonical fingerprint。Key先做Unicode NFKC、trim、
lower并移除`- _ .`与空白后匹配；至少覆盖`authorization`、`proxy-authorization`、`api-key`、`x-api-key`、`token`、
`access-token`、`refresh-token`、`password`、`passwd`、`secret`、`cookie`与`set-cookie`。URL必须删除userinfo、query与fragment，只允许保存
normalized scheme/host/port/path；message与attributes还必须清除Bearer/Basic credential、常见API key/token assignment及URL secret。先脱敏再截断，
避免截断制造可绕过pattern的残片。

中央wrapper必须在自己的`read_next()` task内捕获所有provider/HTTP/SDK/network `Exception`，完成sanitization后**正常返回typed item**，不能重新
raise。Caught raw exception、traceback、response、headers/body与SDK request object不得写入wrapper字段、task result、Future exception、logger、metrics
label或exception chain；sanitized fact构造完成后立即释放全部raw references。因为task以typed value正常完成，LLMRuntime看不到raw exception或其
`__context__`。禁止用`ProviderTransportSanitizedFailure`、`raise ... from None`或其他exception作为第二条受支持的transport failure协议；
`from None`只抑制默认展示，并不是本章认可的secret boundary。

同一约束覆盖public wrapper的**全部**方法，不只`read_next()`：`request_cancel()`、`aclose()`与`wait_physical_completion()`都必须在内部捕获raw
SDK/network cleanup exception且不得向worker抛出。Cancel/close cleanup failure不反向制造provider error；wrapper只保存constant secret-safe
operational diagnostic并继续确认physical state。`wait_physical_completion()`只有确认全部inner operation退出时返回`COMPLETED`；inner cleanup/
completion本身失败、状态不可证明或adapter违反contract时返回`BLOCKED_UNTRUSTED`并进入`PHYSICAL_DRAIN_BLOCKED`。后者必须保留handle、reservation与
Host/session lease，禁止terminal commit；caller不得把“method已返回”误读成“physical operation已完成”。

Sanitizer自身、provider-specific mapper或diagnostic extractor若抛错，wrapper使用composition root预构造并验证的constant
`TRANSPORT_PROTOCOL_ERROR` fallback fact；其message固定、diagnostics为空，只包含同一sanitization contract，不读取或格式化原异常。Fallback也必须走
`ProviderErrorDraft + provider_error terminal draft`，不能让sanitizer failure恢复异常穿透。第三方SDK的HTTP/debug logging在adapter启用前必须关闭或
安装同一contract的redacting filter；无法证明不会记录headers/body/query的adapter不得注册为production binding。

`ProviderModelStreamErrorCode`是唯一durable code vocabulary；provider-specific code只能作为已脱敏、allowlisted bounded diagnostic attribute，不能取代
stable code。`message`最大长度、diagnostic数与attribute长度完全由full `ProviderErrorSanitizationContractFact`决定。该contract的fingerprint必须进入
resolved transport binding/request-shape contract fingerprint；event同时保存full contract fact，historical replay无需查询当前adapter默认值。
`implementation_build_fingerprint`只作当前进程诊断，不进入event/target semantic identity，也不参与historical允许判断。
任何会改变stable code、redaction、allowlist或bounds语义的修改，必须同时升级sanitizer contract version与所属transport contract version并改变
fingerprint；version不变但fingerprint漂移是composition-root configuration conflict，不能把fingerprint当成忘记升级version的替代品。
Contract、diagnostic与error fingerprint都由统一canonical JSON helper覆盖除自身fingerprint字段外的全部semantic fields；validator必须重算长度、
diagnostic allowlist、递归敏感key不存在、URL无userinfo/query/fragment以及`redaction_count`汇总关系，不能只信adapter声称“已脱敏”。

V1不持久化任何raw provider diagnostic。未来若确有取证需求，只能新增独立的access-controlled、encrypted、retention-bounded且先经过secret-safe
scrubber的artifact contract；普通ArtifactStore、event metadata、diagnostic string、logger和exception chain均不得成为raw error旁路。即使该artifact写入
失败，sanitized terminal event仍按本契约收口，不能回退到保存原文。

中央wrapper的provider/network failure状态机唯一化：

```text
OPEN: adapter-private read发生provider/HTTP/SDK/network failure
  -> 在同一wrapper task内sanitize或使用constant fallback
  -> 以next_transport_sequence_index构造唯一ProviderErrorDraft
  -> 预构造outcome=provider_error、usage_status=missing的terminal draft
  -> 清除全部raw references
  -> ERROR_DRAFT_PENDING

ERROR_DRAFT_PENDING
  -> read_next()正常返回ProviderErrorDraft
  -> ERROR_DRAFT_DELIVERED

ERROR_DRAFT_DELIVERED
  -> 不再调用adapter raw read
  -> aclose并等待inner physical completion
  -> 尚未退出时保持FAILURE_PHYSICAL_DRAIN并阻塞terminal/Host close
  -> physical completion返回BLOCKED_UNTRUSTED时进入PHYSICAL_DRAIN_BLOCKED并保留owner
  -> 只有COMPLETED后read_next()正常返回预构造terminal draft
  -> TERMINAL_DELIVERED
```

Error draft后的terminal不允许再次访问network/provider，也不能因当前usage/config变化重算payload；`semantic_item_count`恰为此前FULL semantic count加该
error draft。Raw exception path没有可信reported usage，固定`usage=None/status=missing`，settlement按原physical quote；已在先前typed item中确认的
reported model identity可以保留。Error draft可以先durable commit，terminal仍必须等physical drain，因此崩溃窗口继续由Start-without-End recovery
收口，不能为了原子外观提前伪造End。

Failure与user/Host cancellation的winner按线性化顺序决定：exact termination intent先安装时走cancelled path，不制造provider error；wrapper先捕获并冻结
sanitized failure时走provider_error path，随后cancel只可加速physical drain，不能改写已冻结error/terminal outcome。Transport内部
`CancelledError`只有在matching handle generation已有termination intent时才是受控physical-cancel acknowledgement；没有matching intent的
`CancelledError`是architecture fault，不能伪装成provider failure。

LLMRuntime仍保留最后一道containment guard，但它不是第二条transport API：若public `SanitizingProviderTransportExecution.read_next()`竟抛出任意
非受控`Exception`，说明中央boundary实现违反contract。Runtime不得读取`str(exc)`、`repr(exc)`、traceback、`__cause__`或`__context__`，不得记录
`exc_info`；它只使用预构造constant architecture diagnostic并清除raw reference。尚无durable provider-error semantic winner时，以`runtime_error`
terminalization并latch该transport binding；若matching `ProviderModelStreamErrorEvent`已经FULL，则不得覆盖winner，保持provider-error terminal obligation，
physical state不可证明时先latch/等待restart recovery。Production test必须证明所有已知provider/network failure在到达此guard前已经变成typed
error+terminal drafts。

`ProviderModelStreamErrorEvent`的type必须使用现有事件体系的canonical enum成员
`EventType.PROVIDER_MODEL_STREAM_ERROR`；代码中禁止新增裸字符串事件类别或仅靠
`Literal["provider_model_stream_error"]`绕过`EventType`。L0B同一schema hard-cut PR必须原子完成：

1. 在`EventType`增加`PROVIDER_MODEL_STREAM_ERROR`；
2. 把typed event加入`AgentEvent` discriminated union与所有event visitor/exhaustiveness guard；
3. 在serialization registry登记required per-event schema version、schema fingerprint与event-domain binding；
4. 在historical decoder/domain registry注册该exact type/schema binding，并让raw stored envelope round-trip；
5. 将其声明为model-stream semantic/non-graph domain event，graph reducer按已冻结domain binding deterministic no-op；
6. Inspector按sanitized typed fields投影，禁止回退解析通用`RunErrorEvent`或metadata字符串。

同一event type/schema version只能对应一个schema/domain fingerprint；将来payload shape变化必须升级per-event schema version并保留historical binding，
不能只修改当前Pydantic union后用全局schema常量解释旧row。

LLMRuntime为typed draft分配stable event ID、outer context和canonical event bytes，再通过同一个stream port提交。Transport不得直接构造event-safe
stored event或选择EventLog sequence，也不再接收`EventContext`、event writer或lifecycle IDs。Text/thinking/data的`block_id`由
`resolved_model_call_id + block_kind + logical_block_ordinal`确定性生成；不得使用每次network retry变化的随机UUID。Tool-call ID优先使用provider stable
call ID；provider未提供时按run-frozen adapter contract确定性派生。任何已commit semantic item之后的transport retry必须从exact next cursor继续；无法做到
时以provider error terminalize，不能从index 0重放另一套draft。

所有上述model semantic event在L0B schema hard cut后required携带`ModelStreamSemanticAttributionFact`，其call/start/index/draft kind/fingerprint必须
逐项等于source typed draft及outer stable event ID derivation。`ProviderErrorDraft`映射到新的`ProviderModelStreamErrorEvent`，不复用可能来自其他
runtime subsystem的通用`RunErrorEvent`。Recovery、Inspector与semantic cursor只读typed attribution，禁止解析event ID或metadata字符串反推call/index。
Transport必须以exactly one final `ProviderTransportTerminalDraft`结束正常/provider-error stream；它只是usage/reported-model/outcome measurement，
不是`ModelCallEndEvent`，由LLMRuntime验证后生成terminal lifecycle batch。Provider/network failure必须由中央wrapper产出error+terminal drafts；只有
matching termination intent的受控cancellation由LLMRuntime按dispatch status与reservation quote构造cancelled End。Public wrapper意外抛错属于上一节
architecture containment，构造constant runtime-error End并latch binding，不是provider failure compatibility path。

Terminal validator要求`usage_status="reported" iff usage is not None`，reported token invariants与ResolvedModelCall hard cut完全相同；
`outcome="provider_error"`要求此前恰有一个matching `ProviderErrorDraft`，completed禁止provider error draft。Controlled cancellation与architecture
containment不允许伪造normal/provider-error terminal draft。

Transport `transport_sequence_index`必须从0连续递增；semantic stored event ID由
`resolved_model_call_id + transport_sequence_index + draft_fingerprint`确定性生成。`draft_fingerprint`由统一canonical helper覆盖具体variant除自身外的
全部字段；同一index不同variant/payload是contract conflict。Terminal draft不占semantic index，`semantic_item_count`必须等于已确认semantic draft数，且
只能出现在最后；terminal
之后继续yield、重复terminal、index gap/duplicate或同index不同draft均为transport contract violation。LLMRuntime尚未确认当前semantic draft归宿前
不得请求下一item，因此transport backpressure边界与durable commit边界一致。Transport API是同步、无I/O的
`open_stream() -> ProviderTransportExecution` factory，不再是由consumer pull动的裸async generator；provider dispatch、network connect与首个body
read只能在registered execution的`read_next()`中发生，execution的read/cancel/physical completion由service-owned worker独占。Factory若需要网络才能
返回execution即违反contract，因为cancel authority尚无法定位该physical request。

`ModelStreamExecutionHandle`是process-local owner，不进入event或manifest。`LLMRuntime.start_stream()`必须先把handle、worker activation gate、completion
Future与physical-operation set同步注册进RuntimeSession-owned `ModelStreamExecutionRegistry`，然后才允许worker进入validation/commit/transport；不得先
`create_task()`运行model代码再补owner。Handle worker独立拉取transport、提交事实并terminalize，即使没有subscriber也必须运行到terminal或明确的
reconciliation blocker。

`install_and_start()`的同步线性化顺序固定为：reserve `(resolved_model_call_id, handle_generation)` → install handle/control/completion in registry →
构造coroutine → create task → attach exact-generation done callback → open activation gate。Factory/task-start失败在Start commit前收敛为
`rejected_before_start`并移除owner；gate打开后的任意`BaseException`都由worker terminalizer或reconciliation state收口。旧generation done callback不得
删除新handle。

`ModelStreamCompletion.terminal_commit`对completed/provider_error/cancelled/runtime_error必填且必须是FULL terminal batch；
`rejected_before_start`要求为空且ledger中无Start；`reconciliation_blocked`允许为空但handle/commit owner仍留在registry，不能被视为正常完成或释放
reservation。Completion Future恰好完成一次；`wait_completed()`内部使用`asyncio.shield()`，waiter cancellation不取消shared completion或worker。
对四种durable terminal outcome，result Future必须先以matching terminal batch materialize成功，completion Future随后才能完成；
`rejected_before_start`没有result，`reconciliation_blocked`的result与completion都不得伪装成成功。

Subscription有durable cursor：`after_sequence=None`表示从该handle的ModelStart batch开始，显式sequence表示从其后catch-up再tail；catch-up与live
handoff必须按ordered publisher cursor无gap衔接。每个subscription使用bounded mailbox，但subscriber backpressure不阻塞durable worker：mailbox满时
以`close_reason=OBSERVER_LAGGED`关闭，并保存精确`last_confirmed_sequence`与可用的terminal sequence。Caller `break`、`aclose()`、task
cancellation或不再调用`__anext__()`以`DETACHED_BY_CALLER`关闭；terminal/reconciliation分别使用`TERMINAL_OBSERVED`/
`RECONCILIATION_BLOCKED`。`can_resume_from_cursor`只陈述是否能为UI建立新的观察订阅，不能被解释成canonical result完整性。
`last_confirmed_sequence`必须等于该subscription最后实际交付notification的`through_sequence`，尚未交付则为`None`；
`TERMINAL_OBSERVED`要求`terminal_sequence`等于matching ModelEnd sequence且`can_resume_from_cursor=False`，lag/detach只有在EventLog仍可按cursor
catch-up时才允许`can_resume_from_cursor=True`。

**Subscription仅用于UI、CLI、trace、telemetry与best-effort live rendering。** AgentRuntime、direct subsystem、window summarizer、governance、
reflection或任何控制决策都不得从subscription累积canonical text/tool calls/error/usage。Mailbox lag、observer detach、UI进程崩溃或漏看某个
notification绝不能改变模型调用结果。

Terminal batch FULL且ordered durable handoff完成后，handle调用session-owned `ModelStreamResultMaterializer`，从EventLog的raw stored envelopes按
`resolved_model_call_id + model_call_start_event_id + semantic attribution`确定性读取Start至End的事实，构造`CommittedModelCallResult`。Materializer
必须验证：

- Start/End唯一且terminal batch与handle confirmation逐项相等；
- semantic index从0连续到`semantic_item_count - 1`，每个event attribution、schema与payload fingerprint合法；
- text/thinking/data/tool-call的start/delta/end状态机合法，tool raw arguments按durable delta原样连接；completed terminal要求全部block闭合，
  cancelled/runtime-error/provider-error允许把terminal时仍open的block标为`interrupted`且`end_sequence=None`，不得补造End；
- provider error、terminal outcome、usage status、reported model identity与End逐项一致；
- `combined_text`只按committed text block与sequence顺序构造，不从subscription mailbox或process-local partial buffer补值。

`completion_status="completed"`只表示某个block/tool-call在provider semantic stream中看到了matching End，**不授予执行或成功交付 authority**。
Materializer必须按terminal outcome派生并验证唯一矩阵：

| `terminal_outcome` | `control_disposition` | text/data/tool-call处理 |
|---|---|---|
| `completed` | `SUCCESS_ELIGIBLE` | 闭合text可参与成功reply，闭合tool call可进入后续gate；仍需run termination/permission/capability/phase gate |
| `provider_error` | `AUDIT_ONLY` | 所有已闭合或interrupted内容仅供UI/Inspector/audit，不执行tool、不交付成功reply |
| `cancelled` | `AUDIT_ONLY` | 所有已闭合或interrupted内容仅供UI/Inspector/audit，不执行tool、不交付成功reply |
| `runtime_error` | `AUDIT_ONLY` | 所有已闭合或interrupted内容仅供UI/Inspector/audit，不执行tool、不交付成功reply |

`completed`要求全部block状态闭合且不得含provider error；其他outcome即使在失败前已经FULL提交`ToolCallEndEvent`，该tool call也仍是audit-only。
L0B同步将main lifecycle的`ReplyEndEvent` hard cut增加required `model_terminal_outcome: completed | provider_error | cancelled | runtime_error`，并要求
它与同一atomic terminal batch中的ModelEnd逐项相等；不发明另一套reply status映射。只有completed具备进入成功reply/transcript projector的资格，
但它不是充分条件；其他值均为non-success terminal。`ReplyEnd(model_terminal_outcome="completed")`只表示provider stream完整闭合，不表示Host控制面已经接纳；UI/Inspector在matching
disposition FULL前只能显示`awaiting_control_disposition`，ACCEPTED后才显示delivered/accepted，suppressed后标记suppressed而不能保留成功终态。
Streaming UI此前看到的fragment不改变该durable reply outcome或control disposition。Direct/window call没有ReplyEnd，直接读取ModelEnd矩阵。
`ModelCallControlResultProjector.require_successful_control_view()`只接受`terminal_outcome=completed + control_disposition=SUCCESS_ELIGIBLE`，输出中也只含
闭合tool calls；其他矩阵分支抛typed non-success outcome，不返回空的“成功”view。该projection是唯一控制入口，caller禁止自行以
`completion_status == completed`过滤后执行。

成功terminal仍只是必要条件，不是充分条件：在terminal FULL/result materialization之后、tool gate之前必须再次CAS检查matching run owner没有
`user_stop/host_teardown` termination intent。Intent先赢时，即使model terminal是completed，也不得执行tool或交付成功reply；run按winning intent
terminalize。禁止在streaming期间、ToolCallEnd到达时或terminal commit尚未FULL时做speculative tool execution。

这个“最终接纳”不能只留在process-local CAS。每个`main_assistant_reply`且ModelEnd completed的call必须恰好有一个durable
`ModelCallControlDispositionResolvedEvent`，并在任何tool execution、final text delivery或canonical transcript admission之前FULL commit + ordered
handoff。Stable event ID由`run_id + resolved_model_call_id + model_call_index + "control_disposition"`确定；event/result/call/start/end与
`ModelCallStartEvent.recovery_plan.run_execution_activation`必须逐项join。相同ID不同payload是structural conflict；缺event表示尚未获得控制authority，
绝不等价于accepted。Durable event不得保存或依赖随机process-local `segment_id`；live ABA校验仍由commit guard中的exact segment ID/generation承担。
这里的commit guard明确指`LiveModelCallControlDispositionCommitGuard`；SESSION_REOPEN不得构造它。
L0B同PR增加canonical `EventType.MODEL_CALL_CONTROL_DISPOSITION_RESOLVED`，并同步AgentEvent union、per-event schema/domain registry、raw envelope、
historical decoder与Inspector projection；它是model-control/non-graph event，禁止用CustomEvent或metadata替代。

Disposition invariant：

- `ACCEPTED`：`termination_intent=None`、`recovery_reason_code=None`；source result必须是completed/SUCCESS_ELIGIBLE；
- `SUPPRESSED_BY_TERMINATION`：termination attribution必填且精确复制winning process-local `RunTerminationIntent`的ID/kind/requester/time/target
  activation；`target_run_execution_activation`必须等于event activation，recovery reason为空。未绑定segment的run-level intent不能抑制一个已经换代的
  activation，必须先由run owner把它归一化到当前exact activation再冻结event；
- `SUPPRESSED_BY_RECOVERY`：termination attribution为空，recovery reason固定为
  `process_restarted_before_control_resolution`；只允许SESSION_REOPEN在completed ModelEnd之后、无disposition且无任何downstream
  `CapabilityGateDecision/tool reservation/ToolExecutionSuspended/ToolResultEnd` fact时写；text success delivery本身以ACCEPTED disposition为durable
  authority，不另猜外部UI状态；
- provider-error/cancel/runtime-error不写该event，它们已经由ModelEnd terminal matrix durable确定为AUDIT_ONLY；
- direct/window/internal call不写main disposition event；它们由各自Completed/Failed terminal fact承担下游接纳，仍必须先验证ModelEnd completed。

`ModelCallControlResolutionResult.accepted_permit`存在iff durable event为ACCEPTED；permit逐项绑定event/call/result/activation fingerprint及live
segment ID/generation，仅在当前live segment内使用，不持久化、不跨resume重建。Suppressed与recovery结果permit必须为空。Tool/reply gate同时验证permit
仍属于active segment且该segment投影出的activation等于durable event；stale permit或segment generation变化fail closed。Historical replay只需要durable
activation与event，不需要恢复Python segment ID或permit。

Live线性化必须使用exact run owner的shared `control_linearization_lock`，且Host的`install_termination_intent()`与
`resolve_main_call_control_disposition()`都只能通过同一coordinator：

```text
acquire exact run-control lock
  -> validate active segment ID/generation + its event-safe activation + call/result fingerprint
  -> require activation == ModelCallStart recovery-plan activation
  -> read current RunTerminationIntent
  -> freeze ACCEPTED or SUPPRESSED_BY_TERMINATION stable candidate
  -> commit_and_confirm_resolution()（ledger only；禁止observer callback）
  -> FULL时同步fold_confirmed_resolution()并安装matching process-local control permit
release lock
  -> publish_folded_resolution()（ordered publisher / observer notification）
  -> only ACCEPTED permit may reach reply/tool gate
```

这是每个run owner独有的narrow `asyncio.Lock`，不是Host `_run_lock`、supervisor lock或tool execution lock；锁内只允许读取/冻结owner state并调用
bounded ledger commit/confirmation，以及执行同步、确定性、无callback的reducer fold；不允许ordered publisher、observer、provider、tool、MCP或任意
unbounded I/O。`fold_confirmed_resolution()`必须是event-loop同步方法，不得await、不得调用用户代码或publisher。Lock order固定为run-control lock →
disposition ledger commit port；其他路径不得反向获取。Commit进入UNKNOWN/latch后释放lock但owner保持blocked，stop/close只能收口同一pending owner，
不能另写winner。

`publish_folded_resolution()`只能在lock释放后运行。它以fold result中的exact sequence向session-owned ordered publisher提交notification；publisher worker
拥有retry/drain，caller cancellation只detach waiter。Observer callback即使同步触发`stop_current_turn()`或Host close，也能取得run-control lock：此时
ACCEPTED/SUPPRESSED winner和permit已经线性化，不会形成lock inversion。`observer_failed`与`pending_retry`只生成bounded operational diagnostic，不能
撤销durable disposition、回滚reducer fold、删除permit或改写winner；ordered publisher自身出现structural sequence conflict时可以latch publication
subsystem，但也不得把已经FULL的disposition降格成NONE。AgentRuntime在使用permit前仍执行active-segment/termination gate，因此accepted之后到达的stop
可以取消尚未开始的downstream work，却不能重写历史control event。

Ordered publisher只负责按sequence把immutable notification入队到bounded observer mailbox，不得在publisher临界区inline await任意用户callback body。
Observer callback由独立owned observer task消费；mailbox lag可detach observer并记录operational diagnostic。Observer内部调用close时，Host先detach当前
observer handle，再drain publisher/control owners，且不得join正在发起close的observer task本身。这样锁外分相不仅消除run-control反向等待，也避免
observer callback → close → publisher → same callback的自等待环。

因此winner定义明确：termination intent先安装到owner时生成suppressed；accepted disposition先FULL时该model-step接纳已经线性化，之后到达的stop不能
改写event，但仍可取消尚未完成的下游tool/run。Control commit为NONE时不产生permit并用同stable bytes retry；CONFLICT/PARTIAL/UNKNOWN latch run并保留
owner；caller cancellation只detach waiter。Publication-after-commit必须先confirm/fold FULL再决定permit，不能因observer failure把durable accepted
退回“未接纳”；实际publisher/observer wait始终发生在lock外。Lock等待与ledger commit都有bounded deadline；无法收口时stop/close返回blocker并保留
session，不能绕过event直接执行或teardown。

SESSION_REOPEN由独立`ModelCallControlDispositionRecoveryService`扫描completed ModelEnd无disposition的main call；它绝不调用live coordinator，也不读取或
伪造旧Python segment ID。恢复算法冻结为：

```text
enter session REOPENING lifecycle state
acquire reopen mutation gate
  -> read one continuous canonical ledger snapshot through high-water H
  -> materialize completed result and recover Start-frozen RunExecutionActivationFact
  -> rebind Start-frozen downstream predicate contract
  -> verify disposition count == 0
  -> verify matching downstream fact count == 0
  -> freeze SUPPRESSED_BY_RECOVERY candidate + Recovery commit guard
  -> atomic ledger CAS expected_last_sequence=H
  -> FULL: deterministic fold; never create ModelCallControlPermit
release reopen mutation gate mutex (session remains REOPENING)
  -> lock-free ordered publication / observer notification
finish all recovery items, then mark session OPEN
```

Recovery guard的`recovery_scan_fingerprint`覆盖Start到H的canonical stored-event identities、matching Start/End/result、disposition absence、Start-frozen
downstream predicate contract及matching fact的零计数/空accumulator；`reopen_high_water_event_id/payload_fingerprint`必须等于H处raw envelope，guard
contract fingerprint必须等于ModelCallStart recovery plan。Commit transaction重新验证：

1. current ledger last sequence仍等于H；
2. matching Start recovery plan activation逐项等于guard activation；
3. matching completed End/result identity与fingerprint未漂移；
4. disposition仍不存在；
5. matching downstream predicate仍返回零事实；V1覆盖`CapabilityGateDecisionEvent`、tool reservation、`ToolExecutionSuspendedEvent`、
   `ToolResultEndEvent`与所有matching `RunEndEvent`；
6. candidate只能是`SUPPRESSED_BY_RECOVERY`，termination attribution为空且recovery reason固定。

`RunEndEvent`矩阵固定如下；“prior”均要求sequence严格早于RunEnd。RunEnd按event context中的exact `run_id`和
`RunEnd.sequence > matching ModelEnd.sequence`归属该run内尚未解决的completed call；当前RunEnd schema没有call/activation字段，predicate不得伪造。
其他具有call或activation attribution的downstream fact仍必须逐项join：

| RunEnd terminal matrix | required prior disposition | disposition缺失时 |
|---|---|---|
| `status=finished, stop_reason=FINAL, terminalization_kind=NORMAL` | `ACCEPTED` | structural latch；正常结束是final reply/control success最强downstream事实 |
| `status=aborted, terminalization_kind=USER_STOP` | `ACCEPTED`或`SUPPRESSED_BY_TERMINATION` | structural latch；不得在RunEnd之后补`SUPPRESSED_BY_RECOVERY` |
| `status=aborted, terminalization_kind=HOST_TEARDOWN` | `ACCEPTED`或`SUPPRESSED_BY_TERMINATION` | structural latch；不得在RunEnd之后补`SUPPRESSED_BY_RECOVERY` |
| `status=aborted, terminalization_kind=RECOVERED_INTERRUPTED` | `ACCEPTED`或`SUPPRESSED_BY_RECOVERY` | structural latch；reopen必须先修复disposition、后写recovered RunEnd |
| `status=failed, terminalization_kind=EXECUTION_FAILURE` | `ACCEPTED` | structural latch；control resolution未收口时本就禁止写RunEnd |

因此“aborted/recovered-interrupted允许suppression”的准确含义是：允许**已经在先且类别匹配**的suppression解释该RunEnd，不允许Recovery Service在看到
既成RunEnd后追加一个新的suppression。V1 downstream predicate registry只包含上表与
`ModelCallControlDownstreamPredicateContractFact`中已有schema、writer和recovery规则的事件变体；这是阶段四的完整封闭集合，不预留或引入尚未定义的
downstream event类别或predicate code。

任一条件失败时transaction必须是NONE/CAS_STALE，不能插入event。Service随后从新high-water重新扫描：若已经存在唯一、结构合法且与Start/result/activation
精确join的ACCEPTED或suppressed disposition，则由canonical snapshot pure reducer直接重建/fold该existing durable winner，并把stored sequence交给
RuntimeSession publication recovery，不再写recovery event、也不调用要求“disposition absent”的recovery commit guard；若downstream control fact已经出现但仍无
disposition，则ledger违反“accepted在先”顺序并structural latch；若仍无两者则用相同stable event bytes和新generation recovery guard bounded retry。
Existing winner与downstream同时存在时，只有winner sequence严格早于fact且满足该predicate的required-prior policy才合法；所有已有的call/result
attribution必须一致，RunEnd使用上述same-run/after-End predicate。任一要求`accepted_only`的已登记capability gate、tool reservation、suspension或
tool-result terminal fact跟在suppressed winner之后、termination suppression跟
recovered-interrupted RunEnd、recovery suppression跟user-stop/host-teardown RunEnd，或downstream早于required winner，均structural latch。
同event ID不同payload若不是由一次可解释的CAS_STALE后读到的完整existing winner，而是write/confirmation层的CONFLICT/PARTIAL/UNKNOWN，必须保留
recovery owner并latch，禁止猜测。

Recovery FULL后只返回fold/publication report，API形状中没有permit。即使existing winner是ACCEPTED，reopen也不得重建旧segment permit或重新执行旧tool；
它只保留历史control attribution，随后由统一run recovery terminalize中断run。Publication继续服从锁外规则：observer failure仅为operational diagnostic，
不会撤销recovered disposition。Replay只认durable disposition，不读取当前process-local owner或根据“看起来有ToolCallEnd”推断。

Materialized result fingerprint覆盖全部derived content及source sequences。它是由durable stream确定性派生的process-local结果，不新增第二套durable
result event；允许在exact handle generation内缓存，但cache miss必须重新读EventLog得到相同结果。Materialization失败时result Future不得返回partial
payload：schema/sequence/payload conflict进入reconciliation blocker；暂时I/O失败由owned bounded retry负责。`wait_result()`使用`asyncio.shield()`，
waiter cancellation只detach waiter；`rejected_before_start`与`reconciliation_blocked`分别抛typed terminal/preparation error，不伪造空成功结果。

Production控制边界统一为：

- AgentRuntime等待`handle.wait_result()`，先通过`require_successful_control_view()`，再由coordinator FULL commit durable disposition；只有matching
  `ACCEPTED` control permit才可把view中的completed tool calls交给permission/capability/phase gate或把text交给成功reply；raw result中的tool calls
  绝不直接进入executor；
- `collect_direct_model_call(handle)`先按terminal matrix映射outcome；只有success view可生成successful `DirectModelCallResult`，non-success text/tool calls
  只进入bounded audit/diagnostic，不消费subscription、不等待ReplyEnd；
- window summarizer只允许从success view的`combined_text`构造summary validation input；provider-error/cancel/runtime-error一律走compaction Failed；
- governance/reflection等direct subsystem使用同一success projector，non-success结果生成各自Failed/RunResult，不解析partial text为成功输出；
- subscription notifications可以更早交给UI展示，但UI展示内容没有反向写入或控制authority。

Transcript/replay同样服从terminal matrix：只有`SUCCESS_ELIGIBLE + ModelCallControlDispositionResolvedEvent(ACCEPTED)`的assistant text/tool-call facts
能进入canonical success transcript。Suppressed或缺disposition均不得接纳；`AUDIT_ONLY`内容不得在下一轮被重放成assistant成功消息、tool request或
ordinary context candidate；未来若需向模型解释失败，必须由独立typed
failure source构造，不得重新包装partial body。UI可实时展示partial/closed fragment，但terminal到达后必须明确标记failed/cancelled，不得显示为最终成功回复。

`ACCEPTED`对tool call也只是控制接纳，不替代Stage 3 pairing：历史canonical transcript还要求matching terminal ToolResult，或对仍活跃suspended run要求
matching durable pending-interaction/suspension fact与process owner。Accepted之后、tool真正开始前进程崩溃或later stop导致没有result/pending fact时，该tool
call标记`accepted_but_unexecuted`并从closed historical transcript排除；不得因为已有accepted event重新插入孤立assistant tool call。反之，任何
CapabilityGateDecision/tool reservation/ToolExecutionSuspended/ToolResultEnd durable fact若没有在先ACCEPTED disposition则是structural latch。

Subscription的单向`AsyncIterator`只承担观察，不承担execution、result aggregation或ack协议。Durable confirmation完全封闭在handle worker +
LLMRuntime + `ModelStreamEventCommitPort`内部；不存在“consumer继续拉取才推进provider”“consumer抛异常即表示NONE/FULL”或“observer看到的片段就是
canonical result”的解释。Semantic event的NONE/FULL/CONFLICT/PARTIAL/UNKNOWN与cancel-during-publication全部使用第19.2节stable candidate
confirmation状态机。

Production API hard cut为：

```python
class LLMRuntime:
    def start_stream(
        self,
        *,
        call: ResolvedModelCall,
        context: LLMContext,
        lifecycle_start_bundle: ModelLifecycleStartCommitBundle,
        model_stream_commit_port: ModelStreamEventCommitPort,
        model_stream_execution_registry: ModelStreamExecutionRegistry,
        event_context: EventContext,
    ) -> ModelStreamExecutionHandle: ...
```

这是唯一production execution API；删除直接返回iterator的`LLMRuntime.stream()` compatibility overload。User stop/Host teardown必须先安装run termination
intent，再调用matching handle的`request_cancel()`并bounded await completion；取消Agent observer/segment task本身不是model cancellation authority。
`request_cancel()`以handle ID + generation CAS，重复同reason幂等；不同reason按已冻结的run termination intent决定winner，不能由最后一个task覆盖。
Terminal FULL后请求只返回existing completion；reconciliation-blocked handle不得伪装cancel success。

`request_cancel()`不只是设置一个供worker下一轮轮询的bool。Registry必须为每个generation持有single-assignment awaitable cancellation signal；
`ModelStreamWorkerControl.wait_cancellation_requested()`在intent线性化后立即唤醒。Worker对每次provider read执行明确race：

```text
read_task = exact ProviderTransportExecution.read_next() physical operation
cancel_task = worker_control.wait_cancellation_requested()
await FIRST_COMPLETED(read_task, cancel_task)

read wins before cancellation linearization
  -> 按正常typed draft/terminal validation提交该item
  -> 下一次read前再次检查cancellation intent

cancel wins
  -> 不再请求下一item
  -> 并行启动transport.request_cancel(reason)与transport.aclose()
  -> await cancel/close operations + read_task + transport.wait_physical_completion()
  -> 只有physical status=COMPLETED才构造cancelled terminal batch
  -> BLOCKED_UNTRUSTED保留owner并阻塞terminal/Host close
```

`read_task`、transport cancellation/close operation及adapter内部SDK/network/thread future全部注册进exact handle generation的physical-operation set。
Adapter的`request_cancel()`必须尝试取消exact HTTP response/body iterator或provider SDK request，`aclose()`必须幂等并关闭stream资源；两者互不以
对方先返回为前提且都登记为physical operation。只取消外层
awaiting task而底层socket/thread仍运行不算physical completion。Read与cancel同时ready时以registry记录的termination intent sequence/monotonic
linearization为准：已在线性化前物理完成的item可提交，其他item不得在cancel后继续扩展semantic stream。

若provider SDK或同步bridge无法中断阻塞read，worker保持`cancelling_physical_operation`，reservation与handle owner均不得退休。Terminal
`ModelCallEndEvent`、settlement、ReplyEnd和RunEnd必须等到operation真实退出或adapter提供可证明的detached-safe contract；Host close deadline耗尽时保留
RuntimeSession/Host lease并返回typed close blocker，不能写假的cancelled End后释放资源。Transport真正退出后同一owner再完成terminal commit；迟到
read/cancel callback只能作用于matching handle generation。

Lifecycle kind与canonical atomic batch唯一化；`ModelStreamRecoveryPlanFact`已在17.3节作为start bundle required fact定义：

`ModelCallStartEvent`在本章增加required `recovery_plan`。Main kind要求reply start/end、reservation/quote/settlement与
`run_execution_activation`、`control_downstream_predicate_contract`均非空且window Started为空；window kind要求reservation/quote/settlement/window
Started非空、reply fields、activation与downstream contract为空；direct kind要求这些optional fields全部为空。Start outer event ID必须等于plan中的
start ID，且matching companion IDs逐项等于同一atomic start batch。
Stable End/ReplyEnd/settlement IDs与main activation attribution在provider dispatch前已经冻结，restart不得按当前代码版本重新发明。

Canonical batches：

```text
main_assistant_reply start:
  ReplyStartEvent
  RolloutBudgetReservationCreatedEvent
  ModelCallStartEvent

main_assistant_reply terminal:
  ModelCallEndEvent
  RolloutBudgetReservationSettledEvent
  ReplyEndEvent

window_compaction_summary start:
  RolloutBudgetReservationCreatedEvent
  ContextWindowCompactionStartedEvent
  ModelCallStartEvent

window_compaction_summary terminal:
  ModelCallEndEvent
  RolloutBudgetReservationSettledEvent

direct_internal_call / not_rollout_accounted start:
  ModelCallStartEvent

direct_internal_call / not_rollout_accounted terminal:
  ModelCallEndEvent
```

只有`main_assistant_reply`允许/要求reply ID与stable ReplyStart/ReplyEnd IDs；所有direct subsystem和window summarizer禁止伪造reply lifecycle。
Main bundle companion必须恰好一个matching reservation；window bundle必须恰好是matching reservation + matching Started；direct bundle没有任意
companion。Caller不能借bundle注入业务event。Pre-yield validation失败时整个start batch不存在，compaction可写
`Failed(failure_stage=model_validation)`但不能声称Started/reservation/ModelStart存在。

`root_account/child_subaccount`要求expected account fingerprint与actual quote fact fingerprint非空，并精确join matching reservation；
`not_rollout_accounted`要求两者为空。Main reply必须是rollout-accounted agent call；window summarizer可accounted但无reply；preflight、memory
governance/reflection等direct subsystem只允许`direct_internal_call + not_rollout_accounted`，不能借main kind产生用户reply。

LLMRuntime顺序固定：

1. validate call/context/estimator/target/binding与reservation quote；
2. validate lifecycle kind、reply identity、allowed companions与outer context；
3. 重新CAS root/local rollout account expected state；
4. 生成全部stable lifecycle candidates；
5. 通过required stream port `commit_start()` atomic commit canonical start batch；
6. 对任意`BaseException`（包含`CancelledError`与publication异常）执行stable full-payload confirmation；
7. FULL后`handoff_committed_model_stream()`，随后才向已attached subscriptions发布committed notification；
8. 只有FULL且publication/fold ownership已转移后调用central `SanitizingLLMTransport.open_stream()`；验证返回execution的boundary/sanitizer
   identity精确等于resolved transport binding，再把execution及其全部physical operations注册到handle；raw adapter execution不可见；
9. 对每次`read_next()`与awaitable cancellation signal执行上述race；对胜出的typed `ProviderTransportSemanticDraft`构造stable event candidate，通过
   `commit_semantic()` + confirm/handoff；只有FULL后才publish
   `notification_kind=provider_semantic`，并且在该draft归宿确定前不读取下一transport draft；provider/network failure也只能作为wrapper正常返回的
   `ProviderErrorDraft`进入此路径；
10. semantic batch的NONE可由同一stable bytes重试；CONFLICT/PARTIAL/UNKNOWN保留pending stream owner并latch，绝不把未确认draft交给caller；
11. normal/provider_error/cancel/runtime failure均由同一owner构造canonical terminal batch并通过`commit_terminal()`结算；cancel/runtime failure必须先
    确认全部relevant physical operation退出或detached-safe，FULL handoff后才publish terminal notification；
12. terminal FULL后从EventLog materialize `CommittedModelCallResult`并完成result Future；subscription是否存在、是否lagged不参与该步骤；
13. result Future完成后才完成normal handle completion并允许registry退休。Materialization进入reconciliation blocker时completion也只能是
    `reconciliation_blocked`，不能先报告completed/provider_error。

Start batch已FULL但ordered publication/observer handoff报告failure时，transport尚未开始；stream owner必须先以`runtime_error` End、matching
`not_started_zero` settlement和（main时）ReplyEnd收口，再传播publication error。Terminal batch已FULL但publication失败时按committed slice完成reservation/reply
ownership、materialize同一durable result后再传播，不重新写terminal IDs。Cancellation落在任一publication await同样先confirm
FULL/NONE/CONFLICT/PARTIAL/UNKNOWN，不能按Python
exception类型猜commit状态。

Provider semantic batch已FULL但publication/observer失败时，该semantic fact仍由handoff/fold owner确认存在。普通text/thinking/data/tool semantic
之后的failure用stable `runtime_error` terminal batch收口并传播observer failure；如果FULL semantic正是唯一合法
`ProviderModelStreamErrorEvent`，durable provider-error winner已经冻结，后续publication/observer failure只能增加operational diagnostic，terminal仍必须
是provider_error，physical state不可信时latch等待recovery。它不得重写semantic event，也不得把publication failure伪装成NONE。Caller在收到
notification前后取消只影响observer attachment；全部physical write/confirmation owner仍由LLMRuntime/port drain。

`ModelCallEndEvent.outcome`在本阶段hard cut为稳定enum：`completed | provider_error | cancelled | runtime_error`。Cancelled/runtime-error无可信
usage时还必须保存`provider_dispatch_status: not_started | dispatched`。只有已证明`not_started`才允许`not_started_zero` settlement；一旦provider
dispatch可能发生，usage missing就按原reservation quote生成`cancelled_reserved`或`reserved_missing_usage` settlement。不得省略End后让另一个owner
猜测。若底层provider/sync operation仍在物理运行，terminal commit必须等该operation进入可证明terminal/detached-safe状态；否则pending lifecycle
owner继续阻止Host close，不能提前写假的cancelled End。

对rollout-accounted call，Start后LLMRuntime持有matching reservation直到terminal。End usage、quote与settlement由同一validation result构造；
reported usage高于estimator但不超过physical quote正常结算。`not_rollout_accounted` terminal batch没有settlement，但End仍走同一confirmation、
handoff与pending owner。

每个handle worker内部持有`PendingModelStreamCommit`；start、每个semantic batch与terminal各有attempt generation、stable bytes、shared shielded
Future和全部physical write/confirmation handles。NONE可用同candidate重试；FULL handoff后ack；CONFLICT/PARTIAL/UNKNOWN保留owner并latch。
Subscription cancellation只detach observer，不销毁worker。`ModelStreamExecutionRegistry`只有在terminal FULL且全部physical operation退出后才能retire
handle；对于四种durable terminal outcome还必须等canonical result materialization成功。Materializer的EventLog read同样使用bounded I/O deadline并登记为
handle physical operation。RuntimeSession close必须在EventLog之前request-cancel并bounded drain全部handle/commit/materialization/physical-operation
owner，超时保留Host/session lease。

迁移时必须物理删除旧的`AsyncIterator[AgentEvent]`混流、直接返回iterator的`LLMRuntime.stream()`、公开
`ProviderSemanticEventCandidate`以及AgentRuntime统一emit路径；不保留兼容overload、caller-side semantic writer或按event type运行时猜测“它是否已经
committed”。

### 19.7 CAS线性化点

- checkpoint publish：latest confirmed checkpoint pointer CAS；
- projection rewrite：`window_id + from_generation + state_fingerprint`；
- window compaction：`active_window_id + source_projection_generation`；
- rollout reservation：`account state fingerprint + available balance`；
- phase transition：`from_phase + state fingerprint`；

CAS失败不是ledger corruption：丢弃尚未commit的plan，读取新state重新prepare。artifact candidates若无event引用可留待TTL GC；不得错误安装旧plan。

### 19.8 LongHorizonArtifactWriteService

checkpoint、rollup、window source manifest与summary artifact统一走RuntimeSession-owned service，禁止在event loop同步调用
`PostgresArtifactStore.put_text()`：

```python
class LongHorizonArtifactWriteState(StrEnum):
    PENDING = "pending"
    WRITING = "writing"
    CONFIRMING = "confirming"
    STORED = "stored"
    CONFLICT = "conflict"
    UNKNOWN = "unknown"


class PendingLongHorizonArtifactWrite(BaseModel):
    candidate_id: str
    artifact_id: str
    content_sha256: str
    semantic_metadata_fingerprint: str
    attempt_generation: int
    logical_state: LongHorizonArtifactWriteState
```

实现要求：

- stable candidate bytes与semantic metadata；
- service-owned worker + shared Future，waiter只`asyncio.shield()`；
- bounded executor执行sync store；数据库statement/connect deadline与outer deadline同时配置；
- outer timeout不等于物理thread停止，所有inflight operations继续登记；
- logical retry generation不能遗失旧physical operation；
- 任一old write仍在运行时，暂时read-absent不能terminal为ABSENT；
- 所有physical operations退出后做最终read/compare，再完成STORED/CONFLICT/UNKNOWN；
- same ID、same bytes、same semantic metadata为confirmed existing；metadata-only差异也是conflict；
- close等待全部physical operations；deadline失败保留session供retry；
- unreferenced confirmed artifact进入bounded GC catalog，不在write path直接删除。

Context Input Manifest、rollup与checkpoint artifact writer可共享process-owned `auxiliary_io` executor/confirmation primitive，但不得与
durable event writer的`critical_ledger` lane共享worker容量。各类service的pending registries与artifact media type仍分开，避免一个manifest
waiter取消rollup/checkpoint owner。

---

## 20. Cancellation、stop、close与restart recovery

### 20.1 Session-owned pending owners

HostSession/RuntimeSession必须可枚举：

```python
class LongHorizonPendingOwnersSnapshot(BaseModel):
    model_stream_execution_handle_count: int
    model_stream_commit_count: int
    checkpoint_write_count: int
    projection_artifact_write_count: int
    projection_commit_count: int
    window_compaction_count: int
    rollout_settlement_count: int
    context_manifest_write_count: int
    physical_io_operation_count: int
```

这些owner不藏在被清理的LoopState里。Inspector与Host close可观察其ID、stage、deadline和last error（脱敏）。

### 20.2 Stop顺序

用户stop active run：

1. 通过exact run owner的shared control coordinator取得linearization lock并CAS安装`RunTerminationIntent(user_stop)`；若matching accepted disposition已先
   FULL，则intent只取消其后尚未完成的downstream execution，不改写durable disposition；
2. 阻止新safe point/reservation/tool call；
3. 对active `ModelStreamExecutionHandle.request_cancel(user_stop)`，detach UI observer但不直接cancel worker；
4. bounded drain active async/sync tool owners；
5. bounded await model handle terminal FULL/physical operation退出；
6. 对FULL Started compaction写stable Failed/aborted terminal；
7. settle/cancel其余active reservations（model reservation只能由matching stream terminal owner结算）；
8. drain/confirm本run已调度的checkpoint writer；checkpoint event不得迟到RunEnd之后；
9. atomic close active window + RunEnd；
10. retire execution handles。

用户stop不能把window compaction FULL Started遗留为悬空；也不能在sync tool线程仍运行时释放borrow/handle。

### 20.3 Host close顺序

```text
install host_teardown intent through shared run-control coordinator
-> request-cancel and drain ModelStreamExecutionHandles/commit workers/physical provider operations
-> stop/drain remaining active run/tool segments
-> terminalize suspended run and MCP pending interactions
-> drain MCP pending leases/supervisor
-> drain window compaction owners
-> drain projection/checkpoint/rollout/manifest writers
-> confirm active window closed + RunEnd
-> close RuntimeSession
-> release terminal lease/workspace/session registry
```

任何canonical fact owner在deadline内无法收口，close失败并保留session、lease、workspace与共享close attempt供retry。HostCore不得继续破坏性teardown。

### 20.4 Cancellation矩阵

| cancellation point | required action |
|---|---|
| projection artifact pre-write | cancel if no physical op; otherwise track op to exit |
| rewrite commit publication wait | confirm batch；FULL fold，UNKNOWN latch |
| compaction before Started | 无terminal obligation，清理unreferenced candidates |
| compaction Started publication wait | confirm；FULL保留terminal owner |
| summarizer stream | collect End if possible，写Failed |
| compaction terminal batch wait | confirm；FULL切window，UNKNOWN latch |
| model notification subscriber break/cancel/lag | 只detach subscription；service-owned worker继续 |
| explicit model handle request_cancel | worker停止读取新transport item、drain physical op、写canonical cancelled terminal |
| completed model result control-resolution commit | stop与resolution争用shared lock；intent先赢写suppressed，accepted FULL先赢则stop只取消downstream |
| reopen suppressed-by-recovery commit | session-owned recovery worker持有high-water guard；caller cancel只detach，CAS stale重扫，FULL fold且永不生成permit |
| model reservation commit wait | confirm；FULL必须保留并最终settle |
| tool execution coroutine | async cancel/drain；sync worker borrow直到真实thread结束 |
| checkpoint writer | waiter detach，service worker继续；close drain physical ops |

### 20.5 Restart扫描

session reopen在允许新run/resume前执行：

1. raw ledger/batch完整性确认，window chain与rollout account reducer rebuild；
2. `ModelStreamRecoveryService`修复所有Start-without-End；
3. `ModelCallControlDispositionRecoveryService`以Start-frozen activation + canonical reopen high-water CAS修复completed main ModelEnd无control
   disposition：无downstream tool-control effect时写`SUPPRESSED_BY_RECOVERY`，存在CapabilityGate/tool reservation/suspension/result effect却缺
   ACCEPTED时latch；该路径不构造live segment guard或permit；
4. Started compaction无terminal repair（必须消费第2步已确认的summarizer End）；
5. 其余pending rollout reservation repair（tool/child等；不得再次结算model reservation）；
6. projection page batch完整性验证；
7. latest checkpoint验证；
8. context manifest physical state恢复；
9. subagent child terminal/parent graph handoff repair；
10. MCP pending interaction按Host contract恢复/deny；
11. 最后才对仍无RunEnd的run执行recovered-interrupted RunEnd/window/account close；
12. 只有所有structural latches清晰分类后才开放mutation。

V1不对partial window compaction batch做live canonical winner推断：只能ledger repair/reset或关闭重开仍保持blocked。正常网络/发布失败应通过stable full confirmation避免进入partial。

### 20.5.1 ModelStreamRecoveryService

`PendingModelStreamCommit`是process-local owner，进程崩溃后必然消失；durable恢复authority因此是
`ModelCallStartEvent.recovery_plan + matching reservation/semantic prefix/terminal facts`。RuntimeSession-owned
`ModelStreamRecoveryService`在reopen mutation gate内扫描每个Start：

```python
class ModelStreamRecoveryService(Protocol):
    async def repair_incomplete_model_streams(
        self,
        *,
        runtime_session_id: str,
        through_sequence: int,
        deadline_monotonic: float,
    ) -> ModelStreamRecoveryReport: ...
```

算法冻结：

1. 按`resolved_model_call_id`验证唯一Start、required recovery plan、连续typed semantic indices及最多一个canonical terminal batch；
2. Accounted call必须从同一Start batch读取matching `RolloutBudgetReservationCreatedEvent`，验证reservation ID、full quote及quote fingerprint；Start
   atomic companion缺失、End已存在但matching settlement/ReplyEnd缺失、同stable ID不同payload或semantic index gap，分别归为
   PARTIAL_UNTRUSTED/CONFLICT并latch；不得“补齐”原本要求atomic的半批；
3. Start与完整terminal batch都存在：只fold/ack，不再写；
4. Start存在、End不存在时先分类连续semantic prefix中的`ProviderModelStreamErrorEvent`：
   - 恰好一个、attribution精确join同一Start/call/index/draft fingerprint，且它是prefix最后一个semantic item：durable winner为`provider_error`；
   - 一个也没有：winner为`runtime_error`；
   - 多个error、error后仍有任意semantic item、error attribution/schema/fingerprint不一致：CONTRACT_MISMATCH/structural latch，禁止选择winner；
5. V1没有跨进程可证明的durable `provider_not_dispatched` proof，因此两种winner都**保守视为dispatched**。即使崩溃实际发生在HTTP发送前，也不能使用
   `not_started_zero`；
6. provider-error winner使用recovery plan已冻结的terminal IDs构造
   `ModelCallEndEvent(outcome="provider_error", provider_dispatch_status="dispatched", usage_status="missing")`；sanitized error仍以已durable
   `ProviderModelStreamErrorEvent`为唯一诊断，不重新sanitize、不改code/message，不请求provider。reported model无durable terminal measurement时为None；
7. 无provider error event时才构造
   `ModelCallEndEvent(outcome="runtime_error", provider_dispatch_status="dispatched", usage_status="missing",
   diagnostic="process_restarted_before_model_terminal")`；不重新请求provider，也不生成新的ResolvedModelCall；
8. main call按winner atomic提交`ModelEnd + reserved_missing_usage settlement(full original quote) + ReplyEnd`；ReplyEnd required
   `model_terminal_outcome`必须分别等于provider_error或runtime_error；
9. direct/not-accounted call按winner只提交matching `ModelEnd`，没有reservation/settlement/reply；
10. window summarizer按winner atomic提交`ModelEnd + reserved_missing_usage settlement(full original quote)`；随后window compaction recovery owner使用该
    outcome写stable `ContextWindowCompactionFailedEvent(failure_stage="model_stream_recovery")`，保留provider-error attribution，旧window继续active；
11. 所有repair batch走`commit_terminal()`、stable raw confirmation与ordered handoff。NONE可用同bytes bounded retry；FULL fold后继续下一项；
   CONFLICT/PARTIAL/UNKNOWN保留recovery owner并阻止run/account close；
12. repair service与live `ModelStreamExecutionRegistry`互斥：open session若仍有matching live handle不运行crash repair；reopen时不存在跨进程live
    handle，物理provider operation也已随进程终止。

Generic “pending rollout reservation repair”不得直接删除或零结算model reservation；它只能在确认没有matching ModelStart后处理真正的orphan
reservation。任何有ModelStart的reservation都归本service所有，避免model recovery与account repair双重结算。

### 20.6 Window recovery规则

- RunStart+initial WindowOpened FULL，RunEnd缺失：run recovery写recovered_interrupted RunEnd与window close同batch；
- RunEnd存在、window close缺失：structural latch，不补单边事实；
- old close + new open + Completed全在：new active；
- Started + Failed：old active；
- Started无terminal：repair Failed，old active；
- projection generation pages不完整：structural latch；
- projection artifact缺失但event已commit：contract mismatch，禁止exact execution；
- preferred checkpoint损坏、artifact已GC或虽可读但delta超bound：按through sequence降序尝试所有不晚于目标high-water、reducer contract完全匹配
  且delta在bound内的confirmed checkpoint；候选可能比manifest preferred更新，也可能更早；
  semantic source与selection一致时仍是exact；
- 已存在历史ledger但无bounded compatible基点：production reopen/replay blocked并提示offline doctor，禁止从sequence 1隐式重建；
- 仅“从未产生过checkpoint的新session”可在bootstrap cap内full fold并先FULL confirm首个checkpoint。

---

## 21. Inspector、CLI与可观测性

### 21.1 Run projection

Inspector为每个run展示：

```python
class InspectorLongHorizonRunProjection(BaseModel):
    run_id: str
    active_or_final_window_id: str
    window_count: int
    projection_generation_count: int
    rollout_phase: RolloutPhase
    rollout_charged_milliunits: int
    rollout_total_milliunits: int
    finalization_reserve_remaining_milliunits: int
    finalization_agent_remaining_milliunits: int
    finalization_compaction_remaining_milliunits: int
    finalization_tool_remaining_milliunits: int
    model_call_count: int
    tool_call_count: int
    rollout_status_shadow: RolloutStatusShadowProjectionFact | None
    latest_rollout_status_hint: LongHorizonRolloutStatusCandidateFact | None
    subagent_graph_event_count: int
    subagent_graph_semantic_accumulator: str
    subagent_graph_state_semantic_fingerprint: str
    graph_reducer_id: str
    graph_reducer_version: str
    graph_reducer_contract_fingerprint: str
    preferred_checkpoint_id: str | None
    checkpoint_id: str | None
    checkpoint_through_sequence: int
    checkpoint_delta_count: int
    ledger_through_sequence: int
    ledger_continuity_accumulator: str
    checkpoint_rebased: bool
    pending_owner_counts: LongHorizonPendingOwnersSnapshot
    replay_status: ContextInputReplayStatus
```

`preferred_checkpoint_id`来自原manifest operational audit；`checkpoint_id`是本次live/replay实际使用的acceleration。二者不同但semantic source、
graph、selection与payload一致时，`checkpoint_rebased=True`且replay仍为`exact_replay`。

Child projection必须展开`ChildRolloutSettlementAggregateFact`的四种model settlement basis counts、tool settlement count、reported-subset token
totals及model/tool charged split，并验证aggregate/close/handoff的subaccount fingerprint三方join；不得再投影单一`complete/partial/missing` usage
status，也不得把reserved-missing quote显示为reported tokens。

Model-call timeline还必须区分live service-owned handle、正常terminal、reopen conservative recovery与reconciliation blocker；recovered call显示原
Start、recovery plan stable terminal IDs、semantic item count、`reserved_missing_usage` charge及repair terminal sequence，不能把它伪装成provider正常返回。
每个main completed call还必须投影matching control disposition、source result fingerprint、Start-frozen activation owner/generation、resolution sequence及
termination/recovery attribution。ModelEnd/ReplyEnd已completed但disposition尚未FULL时显示`awaiting_control_disposition`；ACCEPTED显示
`control_accepted`；两种suppressed分别显示termination或recovery原因。Inspector不得从ReplyEnd、下游ToolResult或当前process owner反推accepted。
SUPPRESSED_BY_RECOVERY另展示recovery guard high-water、scan fingerprint与CAS retry count；这些是operational attribution，不进入disposition event的
semantic payload fingerprint，也不得被解释为旧segment identity。Inspector同时展示Start-frozen downstream contract ID/version/fingerprint以及命中的
已注册predicate code/event type；缺disposition latch必须报告具体predicate code，不能笼统标为ledger damaged。

L0B–L4的`rollout_status_shadow`可非空而`latest_rollout_status_hint`必须为空；L5后二者可join到同一derivation fingerprint。Inspector不得把
shadow显示成“模型已收到提示”。

### 21.2 Window timeline

每个window展示：

- generation/open/close sequence；
- open/close reason；
- source summary artifact；
- projection rewrite次数与原因；
- representation分布；
- full→preview→essential→locator→rollup_member→stub的token节省；
- compaction estimated before/after、target与usage；
- protected tail token；
- retry/failure/recovery；
- window semantic/fact fingerprint。

### 21.3 Rollout与status hint timeline

展示每次phase transition：

- source sequence；
- budget触发原因；
- charged/reserved/remaining；
- model/tool call counts；
- model-visible neutral status hint及bounded exact recurrence；
- affected capability action classes；
- child reservations；
- finalization-only阶段被拒绝的tool calls。

不得只显示“max turns reached”，也不得把recurrence展示成runtime判定“没有进展”。

### 21.4 CLI status

REPL `:status`/Inspector摘要至少显示：

```text
Context window: 2/2 (projection generation 5)
Input estimate: 91,240 / 239,616 tokens
Tool projection: 28,100 tokens (soft target 59,904)
Rollout: restricted, 71% exploration allowance consumed
Finalization reserve: 2 calls preserved
Recent exact recurrence: 3 equivalent search actions in the last 16 settled tool calls
Subagent graph checkpoint: seq 2048 + 73 delta events
```

用户不需要理解内部milliunits，CLI可同时给humanized百分比；Inspector保留原始整数。

### 21.5 Diagnostics与metrics

process-local metrics：

- safe point wall time按checkpoint/fold/render/estimate/write拆分；
- checkpoint delta length/bytes；
- projection saved tokens；
- rollup member count；
- compaction provider latency；
- rollout reservation contention；
- exact recurrence observation rate；
- cache hit不进入semantic state；
- physical writer drain time；
- exact replay mismatch reason。

durable event只保存bounded typed audit，不重复写完整catalog、manifest或raw tool body。

### 21.6 长期Inspector contract

更新`contracts/INSPECTOR_PROJECTION_CONTRACT.zh.md`：

- 固定五种replay status，无alias；
- window/rollout/status-hint/checkpoint join规则；
- child run跨runtime session定位root rollout account；
- event-safe facts与process-local build/latency diagnostics区分；
- artifact missing与ledger untrusted不得混淆；
- `policy_limit`省略与`no_eligible_sources`继续可区分。

---

## 22. Failure semantics总表

| stage | failure | durable outcome | resource outcome | run outcome |
|---|---|---|---|---|
| checkpoint read | preferred artifact missing，有bounded compatible base | operational rebased diagnostic | 无新owner | exact restore继续 |
| checkpoint read | reducer ID/version/fingerprint不匹配 | contract_mismatch | session保留 | resume/model step blocked |
| checkpoint read | 无bounded compatible base | artifact_missing/rebase_unavailable | session保留 | production blocked；offline doctor可修复 |
| checkpoint bootstrap | 新session prefix在cap内 | deterministic checkpoint FULL commit | writer完成 | 再进入model step |
| checkpoint bootstrap | prefix超过cap | blocked diagnostic | session保留 | production blocked；offline doctor可修复 |
| checkpoint write | pre-commit NONE | 无event | stable candidate可重试 | delta未超limit可继续 |
| checkpoint write | UNKNOWN/PARTIAL | latch | owner/physical op保留 | blocked |
| projection plan | target unreachable，minimum仍在hard input内（L2/L3） | manifest diagnostic | 无mutation | 使用minimum projection继续 |
| projection plan | target unreachable且minimum超过hard input（L2/L3） | typed compaction-unavailable failure | 无mutation | 不调用provider，bounded terminalize |
| projection plan | target unreachable（L4） | manifest decision | 无mutation | 进入actual summarizer admission/window compaction |
| projection artifact | pre-commit failure | 无rewrite | physical op按真实状态drain | 保持旧projection |
| projection commit | FULL+publication failure | rewrite durable并fold | ack owner | 向上传播observer failure |
| projection commit | PARTIAL/UNKNOWN | latch | owner保留 | blocked |
| compaction planning | protected tail超target | Failed(planning) | 无Started owner | 若input仍可发则旧window继续，否则RunEnd |
| compaction Started | NONE | 可无terminal | 清candidate | old window继续 |
| compaction Started | FULL后取消 | stable Failed obligation | owner保留 | old window继续/stop |
| summarizer | provider error | Failed(model_stream) | settle summarizer call | old window继续或RunEnd |
| summary validation | invalid/overbudget | Failed(summary_validation)含observed值 | artifact不安装 | old window继续或RunEnd |
| terminal batch | FULL publication failure | Completed+close+open durable | new window成为owner | observer error向上 |
| terminal batch | PARTIAL/UNKNOWN | ledger latch | old/new handles都保留供close | blocked |
| rollout reserve | insufficient exploration | phase transition | 无model call | finalization-only重新prepare |
| compaction reserve | insufficient exploration | compaction-admission phase transition | 无Started/model call | finalization-only重新prepare |
| compaction reserve | finalization_compaction不足/actual summarizer unavailable | exhausted(window_compaction_unavailable) | 无Started/model call | typed terminalize，不借answer reserve |
| rollout reserve | final reserve不足 | exhausted | 无model call | terminalize boundedly |
| tool admission batch | gate allow + reservation NONE | 两者均不可见 | stable owner可重试，executor未启动 | blocked/retry |
| tool admission batch | PARTIAL/UNKNOWN | ledger latch | terminal owner保留，executor未启动 | blocked |
| tool terminal batch | result + settlement FULL publication failure | 两者durable并fold | reservation释放，observer error上抛 | 可继续/stop |
| tool terminal batch | result/settlement PARTIAL或UNKNOWN | ledger latch | reservation、MCP pending owner按原状态保留 | blocked |
| main model terminal batch | End+settlement+ReplyEnd NONE | 三者均不可见，stable batch可重试 | reservation/reply owner保留 | 禁止下一admission |
| main model terminal batch | End/settlement/ReplyEnd PARTIAL或UNKNOWN | ledger/lifecycle owner latch | reservation/reply owner保留 | blocked |
| direct/window model terminal batch | End(+settlement) NONE/PARTIAL/UNKNOWN | 按confirmation分类 | lifecycle owner保留直到FULL | blocked/retry |
| finalization tool deny | forbidden action class | typed denied ToolResult | reservation结算 | 保留最终model call |
| emergency counter | exceeded | emergency transition+RunEnd | drain owners | hard stop |
| exact replay | facts mismatch | contract_mismatch | 无live repair | inspect可用，resume blocked |
| close drain | deadline | close attempt error | session/lease/workspace保留 | retry close |

### 22.1 稳定run stop reasons

在现有低层`RunStopReason`增加typed成员，并同步加入`FAILURE_STOP_REASONS`：

```text
context_window_protected_tail_exceeds_budget
context_window_compaction_failed
context_window_contract_mismatch
rollout_budget_exhausted
rollout_emergency_model_call_limit
rollout_emergency_tool_call_limit
long_horizon_ledger_reconciliation_required
```

不新增第二套`terminal_reason_code`。现有详细`stop_reason`仍是单一reason enum，`terminalization_kind`只做
normal/user_stop/host_teardown/execution_failure/recovered_interrupted大类；`error_message`仅保存bounded诊断，不参与控制流。

---

## 23. 代码落点清单

### 23.1 新增低层事实与事件

| 文件 | 修改 |
|---|---|
| `src/pulsara_agent/primitives/long_horizon.py` | 本文全部event-safe enum、window/projection/rollout/status-hint、reducer contract、graph semantic source与acceleration facts |
| `src/pulsara_agent/primitives/run_boundary.py` | `RunExecutionActivationFact`；从durable activation owner + segment generation构造event-safe attribution，不持久化随机segment ID |
| `src/pulsara_agent/primitives/model_call.py` | hard-cut target schema与完整runtime-observation carrier contract；model stream recovery plan、versioned control-downstream predicate contract；新window compaction purpose；usage/account cross-validator |
| `src/pulsara_agent/primitives/context.py` | long-horizon attribution、projected tool result ref；selection range字段硬切为graph semantic source；删除aggregate char policy真源 |
| `src/pulsara_agent/primitives/capability.py` | `LongHorizonToolPolicyFact`、action/cost/rollup classification contract |
| `src/pulsara_agent/event/events.py` | window、projection pages、checkpoint、rollout、window compaction events；RunStart/RunEnd/ToolResult/ModelCall joins；ReplyEnd required model terminal outcome；canonical model-control disposition与provider stream error EventType、typed events；terminal outcome hard cut |
| `src/pulsara_agent/event_log/serialization.py` | per-event schema contract/fingerprint、historical decoder/domain binding；provider error event进入AgentEvent union/registry/raw envelope round-trip；全局schema version不再充当row identity |
| `src/pulsara_agent/event_log/protocol.py` | raw versioned envelope snapshot/read-by-ID；atomic checkpoint catalog + delta；confirmation与maintenance port |
| `src/pulsara_agent/event_log/in_memory.py` | raw envelope为存储真源、owned typed decode、canonical deep-copy atomic snapshot |
| `src/pulsara_agent/event_log/postgres.py` | 写入/读取per-event schema columns；不经current union的repeatable-read raw snapshot、checkpoint enumeration、deadline/index |
| `src/pulsara_agent/storage/postgres_schema.py` | `agent_events` required per-event schema/domain columns与显式migration/reset |

### 23.2 新runtime package

```text
src/pulsara_agent/runtime/long_horizon/
├── __init__.py
├── types.py
├── ids.py
├── reducer_contract.py
├── checkpoint.py
├── checkpoint_store.py
├── checkpoint_gc.py          # privileged closed/quiescent maintenance only
├── checkpoint_doctor.py
├── window.py
├── window_reducer.py
├── projection.py
├── projection_reducer.py
├── rollup.py
├── allocation.py
├── feasibility.py
├── rollout.py
├── rollout_reducer.py
├── compaction.py
├── commit.py
├── recovery.py
└── service.py
```

依赖方向：

```text
primitives + event + event_log protocol
             ↑
runtime/long_horizon pure reducers/planners
             ↑
runtime/long_horizon services
             ↑
RuntimeSession / AgentRuntime / HostSession composition root
```

`primitives`禁止import runtime。pure planner禁止import HostSession、AgentRuntime、Postgres store或provider adapter。

### 23.3 Context input/compiler

| 文件/目录 | 修改 |
|---|---|
| `runtime/context_input/live.py` | 从window/projection/graph semantic source/rollout state收集immutable inputs；acceleration单独进入manifest audit |
| `runtime/context_input/transcript.py` | 只投影terminal completed且已被model-step成功路径接纳的assistant text/tool calls；audit-only partial/closed fragments不得伪装canonical transcript |
| `runtime/context_input/snapshot.py` | required long-horizon attribution与replay join |
| `runtime/context_input/manifest.py` | manifest schema、child artifact与writer drain保持现有ownership语义 |
| `runtime/context_input/replay.py` | exact window/projection/graph/rebase/rollout/status-hint重建与五态分类；不得调用offline doctor |
| `runtime/context_input/candidate.py` | neutral rollout status/summary typed candidates，不新增完整source registry |
| `runtime/context_input/render.py` | 按projection fact渲染unit fragment；不做跨unit隐式淘汰 |
| `runtime/context_input/compiler.py` | lowering projected refs与`PreparedObservationRollupUnit`；在完整pair group后创建独立runtime observation；final estimate identity |
| `runtime/context_engine/types.py` | prepared invocation/result DTO |
| `runtime/context.py` | 删除aggregate char cap与兼容估算入口 |
| `llm/input.py`、`llm/request.py` | 新增无tool-call归因的typed `runtime_observation` carrier；禁止伪装user/tool result |
| `llm/estimator.py` | 估算runtime observation的固定framing/wire contract；reservation仍使用resolved physical bounds而非该estimate |
| `llm/adapters/openai/*`及其他production adapters | 按target-frozen carrier contract lower inert runtime observation；unsupported target fail closed |

### 23.4 Runtime/Host/Subagent

| 文件 | 修改 |
|---|---|
| `runtime/state.py` | 删除正常`max_turns/max_tool_calls`控制与36K aggregate；只保留resolved emergency policy |
| `runtime/agent.py` | 每个model step调用coordinator；phase gate；`wait_result()`后必须先取得successful control view并重查termination intent，raw/audit-only tool call不得进入executor；subscription仅交给UI observer；stop通过handle request_cancel，不提交、不fold、不二次写任何model-stream event |
| `runtime/session.py` | long-horizon services、model/tool/control-disposition commit ports、`ModelStreamExecutionRegistry`、result materializer、committed reducers、recovery、close drain |
| `runtime/wiring.py` | production/in-memory wiring必须提供checkpoint/artifact/event/model-call/tool-event services与registries；production composition root在Host可用前校验全部model pair feasibility，并拒绝任何未由central sanitizing transport包装的provider binding |
| `runtime/run_entry.py` | initial window/account stable facts与atomic RunStart batch |
| `host/session.py` | PRE_RUN/child/resume接线；termination intent安装必须经过shared run-control coordinator；disposition/recovery、termination batch与close blocker |
| `host/run_boundary.py` | exact run owner增加bounded `control_linearization_lock`与matching accepted permit；stop intent和model disposition共用同一线性化入口 |
| `runtime/subagent/runtime.py` | root rollout reservation、child receipt/settlement、terminal reference |
| `runtime/subagent/store.py` | checkpoint canonical export/restore pure seam；不持有authority |
| `runtime/subagent/reducer.py` | versioned reducer contract、typed event filter、non-graph deterministic no-op与stable projection |
| `runtime/compaction/*` | 保留cross-run/preflight compaction；删除同run scratchpad inline ownership，复用direct-call/commit底座 |
| `runtime/execution_handles.py` | 不增加rollout/pending双账；只保护真实active execution borrow |
| `runtime/model_control.py`（新增） | live/recovery两种disposition guard、`Coordinator/RecoveryService/CommitPort`、stable candidate、termination attribution、锁内ledger confirm/fold/permit、锁外ordered publication与reopen suppression repair |
| `llm/commit.py`（新增） | 低层split start/semantic/terminal `ModelStreamEventCommitPort`、三类CAS guard、stable confirmation、committed notification与handoff DTO；不import AgentRuntime |
| `llm/execution.py`（新增） | `ModelStreamExecutionHandle/Registry`、activation gate、UI-only subscription hub、awaitable cancellation signal、transport read race、completion与physical-operation drain |
| `llm/result.py`（新增） | 从raw EventLog semantic stream确定性materialize `CommittedModelCallResult`；验证block/tool-call状态机、usage/outcome/source sequences与terminal control matrix；唯一successful control-view projector |
| `llm/recovery.py`（新增） | `ModelStreamRecoveryService`、Start-without-End扫描、semantic-prefix provider-error winner分类、三类lifecycle stable terminal repair |
| `llm/runtime.py` | required stream commit port；`start_stream()`返回service-owned handle，为typed transport drafts生成stable events、持久化完整model stream并在terminal FULL后触发canonical result materialization |
| `llm/direct.py` | 只消费`handle.wait_result()`并映射direct result；不从subscription聚合、不等待ReplyEnd、不写任何model-stream event |
| `llm/transport.py` | 只暴露public `LLMTransport/ProviderTransportExecution`与typed drafts；API删除EventContext/writer、raw SDK iterator/exception、arbitrary JSON、stored event/lifecycle |
| `llm/sanitizing_transport.py`（新增） | 唯一central `SanitizingLLMTransport/Execution`、raw failure catch、error→physical-drain→terminal状态机、constant fallback与architecture containment seam |
| production adapters | raw factory/execution保持adapter-private；提供可取消exact read/close/physical completion与structured mapping hints；不得直接注册、创建durable error或记录raw diagnostics |
| `llm/provider_error.py`（新增） | stable provider error code、declarative sanitization contract、recursive secret/URL redaction、bounded diagnostic、constant fallback与binding registry；raw provider error不得离开central adapter boundary |
| `llm/registry.py` | 只接受带exact sanitizer/boundary contract identity的`SanitizingLLMTransport`；同transport ID/version不同sanitizer或raw binding fail closed |
| `llm/runtime_observation.py`（新增） | runtime-observation carrier registry/binding与target resolution/rebind；implementation build fingerprint仅诊断 |
| `memory/governance/engine.py`、`memory/reflection/engine.py` | 传入所属durable session/composition root的`not_rollout_accounted` commit bundle/port；不恢复caller写lifecycle event |

### 23.5 Capability/tool semantics

| 文件/目录 | 修改 |
|---|---|
| `capability/provider.py` | descriptor snapshot必须带long-horizon policy；projection-only provider不伪造descriptor |
| `capability/runtime.py` | descriptor semantic identity纳入long-horizon policy；V1不做无boundary的exposure rewrite |
| `runtime/tool_loop.py` | gate+reservation、terminal+settlement唯一commit coordinator；normal/deny/error继续通过Stage 3统一result semantics builder |
| `tools/executor.py` | hard cut删除独立`ToolResultEndEvent` append；只返回`PreparedToolTerminalResult` |
| `runtime/result_semantics.py` | 提供typed terminal result semantic fingerprint；不新增evidence/provenance extractor |
| `runtime/tool_action.py`（新增） | invocation action classifier registry、durable binding与pre-execution classification |
| builtin/MCP/terminal descriptor providers | 声明action class、cost与rollup family |

### 23.6 Inspector/CLI/contracts/tests

| 文件 | 修改 |
|---|---|
| `inspector/service.py` | window、projection、rollout、status hint、checkpoint、model terminal/control disposition与replay projection |
| `inspector/diagnostics.py` | stable long-horizon diagnostics |
| `cli.py` | `:status`与inspect输出；`config-check`枚举primary/summarizer feasibility matrix；显式checkpoint doctor入口 |
| `contracts/*` | 第27节矩阵 |
| `tests/support/context_input.py` | 新typed builders，不保留old char-budget facade |
| 新测试文件 | 第25节矩阵 |

---

## 24. PR序列：L0A–L5必须逐个全绿

### 24.1 L0A：Subagent graph checkpoint

**目标**：消除每次compile/replay从sequence 1 fold graph的无界成本，不改变provider payload。

实施：

1. additive checkpoint facts、artifact format与EventLog atomic read；
2. EventLog hard cut为per-event versioned raw envelope；PostgreSQL/InMemory raw snapshot在current union decode前返回schema identity与canonical bytes；
3. per-event schema fingerprint canonicalizer、historical decoder/domain registry与schema-aware `FrozenStoredEvent`；
4. pure export/restore/fold；
5. RuntimeSession-owned checkpoint writer；
6. live/replay selection共用checkpoint+delta；
7. reducer ID/version/contract fingerprint进入RunStart、checkpoint与semantic source exact join；
8. manifest分别保存semantic source与operational acceleration；
9. exact replay支持compatible checkpoint rebase，Inspector展示preferred/actual/rebased；
10. 新session只允许bounded bootstrap；privileged doctor独占unbounded full fold；
11. V1不实现online physical GC；privileged doctor/GC只在session closed/quiescent并取得PostgreSQL advisory maintenance lock后运行；
12. 达到delta bound时fail closed；
13. 删除production full-fold selection及raw-row current-union historical read路径。

Schema hard cut与所有消费者在同PR落地；不留`use_checkpoint=False`生产开关。

验收：

- provider payload与迁移前相同；
- 10万parent graph events下selection只foldbounded delta；
- Postgres race不会产生selection/high-water漂移；
- 完全相同authority prefix使用不同checkpoint schedule时，selection/snapshot/input semantic fingerprints相同；
- exact replay可优先旧checkpoint，也可在原artifact缺失后rebase且保持exact；
- reducer schema相同但contract fingerprint不同会fail closed；
- checkpoint event与所有非graph event进入continuity accumulator、对graph fold deterministic no-op；
- production hot path无checkpoint且超过bootstrap cap时blocked；offline doctor仍能仅凭EventLog全量修复；
- privileged GC不永久pin历史manifest checkpoint；live/open/resumable session下拒绝执行，checkpoint rebase维持historical exact replay；
- full fold production grep为零。

### 24.2 L0B：Window/rollout/status-hint骨架

**目标**：在不改变模型行为前提下建立required durable identity与pure reducers。

实施：

1. RunLongHorizonContract required；
2. initial window/account与RunStart atomic batch；
3. window close与RunEnd atomic batch；
4. generation 0 baseline + `new_result_ingested` projection pages先接线，representation保持Stage 3最高安全表示，不做降级；
5. model/tool/child reservation与settlement ownership先完整接线，但phase固定为EXPLORATION、尚不执行阶段gate；
6. RuntimeSession使用active-run sparse bootstrap：读取active run的reducer facts与同快照whole-ledger high-water，closed run不驻留；
7. split start/semantic/terminal `ModelStreamEventCommitPort`、service-owned `ModelStreamExecutionHandle/Registry`、UI-only committed notification
   subscription、EventLog-backed canonical result materializer、可取消owned transport execution、typed transport draft union、
   `ModelStreamRecoveryService`、`ToolExecutionEventCommitPort`、child resolved budget与action classifier/cost完整接线；完整model stream从L0B起由
   LLMRuntime唯一持久化，AgentRuntime/direct/window控制侧只消费`handle.wait_result()`，不再emit、驱动transport或从subscription拼结果；provider
   binding全部由central `SanitizingLLMTransport`包装，raw SDK/network exception在wrapper task内转成error+terminal drafts；provider error按
   declarative contract脱敏并使用`EventType.PROVIDER_MODEL_STREAM_ERROR`；completed main call在tool/reply前必须FULL commit typed control disposition，
   stop intent与accepted disposition共用run-control linearization lock；control ledger confirm + deterministic fold + permit在线性化锁内，ordered
   publisher与observer notification严格在锁外且observer failure仅为operational diagnostic；SESSION_REOPEN使用独立high-water CAS recovery guard，
   不伪造segment ID、不生成permit；ModelStart冻结versioned downstream predicate contract，五类RunEnd及
   `CapabilityGateDecisionEvent`、tool reservation、`ToolExecutionSuspendedEvent`、`ToolResultEndEvent`进入因果校验；V1只实现DTO枚举的封闭
   predicate set，不引入其他downstream event或抽象predicate；classifier在L0B只为最终account与recurrence提供typed facts，
   不执行phase gate；Inspector接exact recurrence shadow，
   `model_visible=False`，不创建candidate、不进入manifest semantic input或provider payload；
8. Context snapshot/manifest required attribution；
9. Inspector/replay接线；
10. migration/reset数据库。

这里的“尚不gate”不是第二套usage算法：reservation、settlement、usage math和account reducer已经是最终实现；L5只启用本文阈值、phase
transition、execution gate和模型可见status candidate。该PR序列作为不可拆分部署的阶段四migration连续合入；每个PR测试全绿，但不把L0B
中间状态单独发布为产品契约。

验收：每个host/child run恰好一条initial window、一个root或inherited account；每个新tool result在下一model call前有FULL ingest
generation；consumer detach/lag不会停止model worker或截断canonical result；blocked transport read可由awaitable cancel signal打断，物理operation未退出时
close fail closed；reopen可修复三类Start-without-End；并发child/tool settlement不影响semantic commit；provider error不会泄漏credential/URL
secret；control observer回调触发stop/close不会死锁且不会撤销durable winner/permit；non-success terminal中的closed tool call/text始终audit-only；
completed但未accepted的main result也不会进入tool/reply/transcript；recovery guard能拒绝late downstream fact、接受在先合法winner并对冲突latch；
normal RunEnd缺在先ACCEPTED时也必须latch；provider payload与Stage 3相同；正常完成window闭合；replay exact。

### 24.3 L1：动态token projection target

**目标**：删除36K aggregate hard cap，tool projection由resolved input budget派生。

实施：

1. 新allocation policy/fact；
2. renderer只做per-unit safety cap；
3. compiler输出完整token breakdown；
4. safe point两遍budget decision；
5. ContextCompiled operational audit接入`LongHorizonProjectionPressureShadowFact`；L2 production rewrite cutover时才把正式
   `LongHorizonContextBudgetDecisionFact`设为required semantic attribution；
6. `max_projection_units_per_window`只产shadow diagnostic，不阻断、不触发尚不存在的rewrite/compaction；
7. 删除LoopBudget/DTO/config中的aggregate char真源；
8. 小窗口/大窗口tests。

L1尚不做跨result rewrite；tool projection超过soft target时只记录`context_projection_soft_target_exceeded`/unit-count shadow diagnostic，
只要overall hard input可发送仍保持Stage 3 payload。L2/L3接管token projection，L4才允许产出真正的`window_compaction_required`行为。
若overall hard input超ResolvedModelCall预算，任何阶段仍fail closed。

### 24.4 L2：Deterministic projection与rollup

**目标**：将历史tool results转换为可审计、pairing-safe、artifact-aware的bounded projection。

实施：

1. projection facts/reducer/events；
2. renderer按projection生成per-unit fragment；
3. artifact-aware thinning；
4. rollup semantics/renderer/artifact；
5. safe point CAS/commit/rebuild；
6. manifest/replay/Inspector；
7. current-run protection；
8. typed `PreparedObservationRollupUnit` materialization/renderer registry、ordering anchor与独立runtime-derived observation carrier；
9. `ResolvedModelTargetFact`完整冻结carrier fact，registry exact rebind，unsupported target显式`None`；
10. 删除renderer aggregate truncation与mutable cache decision真源。

验收：同输入plan deterministic；无tool pairing断裂；rollup在完整pair group后以无tool-call归因的独立runtime observation进入真实provider
payload；projection单调；publication/cancel窗口
正确；真实9K tool projection不因36K字符失败。L2/L3仍只降低token表示，不以unit-count hard fail。

### 24.5 L3：Current-run deterministic micro-compaction

**目标**：让active run新产生的tool result在每次model call前进入同一projection机制。

实施：

1. current-run source boundary；
2. protection facts；
3. preparation retry loop；
4. current-run rewrite pages；
5. pending interaction/in-flight tool barrier；
6. safe-point revision与compile attempt identity分离；
7. remove current-tail raw estimate path。

L2可先只处理prior/historical units；L3再开放current run rewrite，因此两个PR均可独立全绿。

### 24.6 L4：LLM context window compaction

**目标**：deterministic projection仍不足时，在同一run内关闭旧window、开启新window。

实施：

1. new event family/DTO；
2. source document/pair groups；
3. summarizer target/call/input budget；
4. Started→terminal owner；
5. success atomic batch；
6. restart recovery；
7. replace old inline compactor in same-run path；
8. `max_projection_units_per_window`首次成为hard invariant：通过关闭旧window/summary真正减少active units；
9. Inspector timeline。

验收：old window失败可继续、success仅一个active window、summary citations有效、cancel/unknown/partial矩阵全覆盖。

### 24.7 L5：Rollout/finalization phase

**目标**：控制累计输入/输出/tool/subagent工作量并保证收尾。

实施：

1. 启用L0B已接线account/reducer的最终threshold policy；
2. 验证model/tool/subagent reservation在phase pressure下的admission；复用L0B的LLM/tool commit ports与child versioned额度公式，不重新接线
   第二owner；
3. 验证End/terminal settlement repair owner；child close/handoff使用含subaccount identity的settlement-basis aggregate并做nested三方join，不使用有损
   usage status或estimated token total；
4. 启用phase transitions；
5. 启用L0B已required记录的descriptor action classification/cost作为phase gate authority；不新增第二classifier；
6. phase-aware tool execution gate与required phase candidate；
7. finalization agent/compaction reserve；
8. 对所有production primary target/summarizer pair运行静态可行性矩阵，并接入配置校验与`pulsara config-check`；
9. 启用中性rollout status hint；exact recurrence只陈述事实，不改变phase或deny调用；L0B shadow在此才成为model-visible candidate；
10. 加入agent与window summarizer各自的admission-unreachable确定性transition、safe-point restart及actual resolved call可达性校验；
11. model reservation统一使用`calculate_model_call_reservation()`的physical-bound quote，missing usage按quote全额结算；
12. 验证L0B已经落地的`ModelStreamEventCommitPort`在phase transition、finalization与missing-usage路径仍独占完整model stream；L5不得重新引入
    caller writer或第二套terminal settlement owner；
13. emergency counter替代旧正常limit。

验收：cached input正确降权；missing usage按physical quote保守结算；provider usage高于estimate但位于physical bound内可正常settle；并发child不
超卖；finalization deny后仍有一次call；window compaction不会被exploration边缘余额夹死；phase不可倒退；
每个启用的primary/summarizer组合在Host接收session/run前通过静态可行性校验；`config-check`可解释reserve与exploration allowance；
status hint不含停止、继续或下一步建议，且不会改变gate结果。

阶段四至此结束，不存在L6。未来若需要AutoResearch/Web Research专用的证据质量策略，必须另立产品层规格，不得作为本阶段
rollout reducer的隐式扩展。

### 24.8 每个PR的共同要求

- `uv run pytest -q`全绿；
- `uv run ruff check src tests`全绿；
- `git diff --check`全绿；
- PostgreSQL与InMemory parity；
- event serialization round-trip；
- Inspector contract与代码同PR更新；
- negative schema test有planner/reducer twin；
- 每个fault injection验证resource owner；
- 不以compat overload维持dual truth；
- production grep gate同PR通过。

---

## 25. 测试矩阵

### 25.1 Checkpoint

```text
test_checkpoint_export_is_canonical_and_order_independent
test_checkpoint_artifact_same_id_same_bytes_is_idempotent
test_checkpoint_artifact_metadata_only_conflict_fails_closed
test_checkpoint_event_rejects_inconsistent_graph
test_checkpoint_delta_snapshot_is_one_database_snapshot
test_checkpoint_delta_rejects_gap_duplicate_and_out_of_order
test_checkpoint_restore_matches_full_fold
test_checkpoint_schema_match_reducer_contract_mismatch_fails_closed
test_postgres_raw_snapshot_returns_schema_envelope_without_current_union_decode
test_raw_envelope_wrapper_schema_and_payload_identity_are_consistent
test_event_schema_fingerprint_is_canonical_under_schema_key_order
test_same_event_type_version_with_different_schema_fingerprint_is_registry_conflict
test_event_schema_semantic_change_requires_version_and_fingerprint_change
test_historical_decoder_restores_old_schema_before_current_union
test_historical_decoder_contract_fingerprint_drift_is_contract_mismatch
test_row_without_explicit_per_event_schema_identity_fails_closed
test_in_memory_event_log_stores_raw_envelope_and_returns_owned_decode_copy
test_confirm_batch_compares_raw_per_event_schema_identity_and_payload_without_current_union
test_same_reducer_id_version_with_different_contract_fingerprint_is_registry_conflict
test_reducer_semantic_change_requires_version_and_contract_fingerprint_change
test_checkpoint_event_and_declared_non_graph_events_extend_ledger_continuity_only
test_checkpoint_event_does_not_change_graph_semantic_accumulator_or_source_fingerprint
test_graph_semantic_accumulator_ignores_physical_sequence_and_checkpoint_schedule
test_graph_semantic_payload_fingerprint_cannot_reuse_storage_payload_with_sequence
test_graph_state_semantic_fingerprint_excludes_event_sequence_attribution
test_future_declared_graph_event_unsupported_by_run_contract_fails_before_emit
test_replay_unknown_or_unsupported_graph_domain_event_is_contract_mismatch
test_new_non_graph_event_does_not_change_existing_graph_reducer_contract
test_event_domain_is_immutable_for_event_type_and_schema_version
test_historical_event_domain_binding_rebinds_after_registry_upgrade
test_event_schema_domain_fingerprint_drift_is_contract_mismatch
test_checkpoint_does_not_include_its_own_materialization_event
test_checkpoint_reader_returns_deep_copies
test_checkpoint_writer_cancel_after_commit_confirms_full
test_checkpoint_partial_commit_latches_ledger
test_checkpoint_unknown_keeps_physical_owner
test_checkpoint_close_drains_blocking_postgres_operation
test_checkpoint_falls_back_to_earlier_contract_compatible_checkpoint
test_readable_preferred_checkpoint_with_oversized_delta_uses_newer_compatible_checkpoint
test_checkpoint_delta_bound_blocks_unbounded_full_fold
test_new_session_bounded_bootstrap_confirms_checkpoint_before_compile
test_existing_checkpoint_catalog_with_missing_artifacts_does_not_use_bootstrap
test_existing_session_without_checkpoint_never_uses_production_full_fold
test_offline_doctor_can_full_fold_and_rebuild_checkpoint
test_compiler_and_live_replay_cannot_import_or_call_offline_repair
test_checkpoint_schedule_does_not_change_semantic_source_or_selection_fingerprint
test_manifest_semantic_fingerprint_excludes_checkpoint_acceleration
test_original_checkpoint_missing_rebase_can_remain_exact_replay
test_checkpoint_materialized_after_manifest_can_accelerate_historical_exact_replay
test_rebase_compares_semantic_source_graph_selection_and_candidates
test_checkpoint_gc_does_not_pin_historical_manifest_artifact
test_checkpoint_gc_refuses_live_open_or_resumable_session
test_checkpoint_gc_requires_exclusive_postgres_advisory_maintenance_lock
test_checkpoint_doctor_refuses_runtime_session_writer_and_uses_offline_writer
test_checkpoint_maintenance_lock_is_released_when_process_connection_dies
test_live_and_replay_subagent_selection_use_same_semantic_source
test_new_child_completion_between_reads_cannot_drift_selection_high_water
```

### 25.2 Window chain

```text
test_run_start_initial_window_and_rollout_account_commit_atomically
test_child_run_start_has_initial_window_and_inherited_account
test_run_end_and_window_close_commit_atomically
test_window_generation_is_contiguous
test_window_rejects_two_active_windows
test_window_close_rejects_wrong_projection_generation
test_compaction_completed_close_open_batch_is_atomic
test_window_chain_full_commit_publication_failure_folds_new_window
test_window_chain_partial_batch_latches
test_restart_recovers_started_without_terminal_as_failed
test_run_recovery_closes_active_window_with_recovered_run_end
```

### 25.3 Projection/pairing

```text
test_projection_rank_never_increases
test_pair_stub_keeps_call_result_state_timing_and_artifact_locator
test_projection_rejects_cross_call_result_ref
test_projection_rejects_fragment_from_other_window_generation
test_projection_pages_must_be_complete_and_atomic
test_projection_plan_is_deterministic_under_input_order_variation
test_current_user_adjacent_result_never_drops_below_essential
test_pending_interaction_is_not_rewritten
test_inflight_sync_tool_blocks_rewrite_and_compaction
test_projection_artifact_failure_keeps_old_projection
test_projection_cancel_during_publication_confirms_full
test_projection_unknown_commit_keeps_owner_and_blocks_model
test_projected_transcript_preserves_multi_tool_call_group
test_compiler_and_prepare_both_validate_four_way_pair_identity
```

### 25.4 Dynamic token allocation

```text
test_256k_target_derives_tool_soft_target_from_same_resolved_budget
test_small_model_target_derives_smaller_projection_target
test_input_safety_margin_is_not_subtracted_twice
test_36k_aggregate_chars_no_longer_causes_context_pressure
test_single_observation_and_artifact_safety_caps_remain_enforced
test_allocation_uses_final_message_framing_and_tool_schema_tokens
test_compiler_presend_estimate_mismatch_fails_closed
test_minimum_pairing_over_budget_never_deletes_tool_result
test_budget_decision_round_trips_in_context_manifest
test_long_horizon_pre_manifest_failure_writes_typed_context_input_failure
test_ledger_untrusted_preparation_failure_does_not_append_explanatory_event
test_l1_unit_count_over_soft_limit_is_diagnostic_only
test_l2_l3_unit_count_over_limit_does_not_fail_before_window_compaction_exists
test_l2_l3_projection_target_unreachable_within_hard_input_sends_minimum_and_audits
test_l2_l3_projection_target_unreachable_over_hard_input_fails_compaction_unavailable
test_l4_unit_count_limit_compacts_window_or_fails_protected_tail_closed
test_safe_point_revision_and_compile_attempt_index_have_independent_bounds
```

### 25.5 Rollup/artifact thinning

```text
test_rollup_keeps_each_original_member_pair_stub
test_rollup_never_merges_success_and_error_as_same_fact
test_rollup_requires_descriptor_family_and_typed_evidence
test_rollup_does_not_parse_generic_json_body
test_rollup_member_order_is_source_sequence
test_rollup_artifact_is_idempotent_and_metadata_checked
test_rollup_id_covers_ordered_complete_member_set_and_renderer_contract
test_rollup_materializer_confirms_inline_and_artifact_hash_before_compile
test_rollup_prepared_unit_rejects_member_or_anchor_identity_mismatch
test_rollup_is_independent_runtime_observation_after_complete_pair_group
test_rollup_runtime_observation_has_no_tool_call_id_or_tool_result_role
test_rollup_unsupported_provider_carrier_fails_closed_or_disables_rollup
test_rollup_carrier_contract_enters_target_estimator_and_payload_identity
test_resolved_target_v3_requires_explicit_runtime_observation_carrier_key
test_runtime_observation_carrier_full_fact_enters_target_fingerprint
test_runtime_observation_carrier_registry_rebinds_exact_id_version_fingerprint
test_compile_unit_carrier_must_equal_resolved_call_target_carrier
test_exact_replay_uses_durable_target_carrier_not_current_adapter_default
test_rollup_never_inserts_message_between_tool_call_and_result
test_rollup_anchor_does_not_add_nonmember_sibling_from_same_pair_group
test_rollup_abandons_incomplete_anchor_group_without_expanding_members
test_rollup_source_cannot_be_active_in_two_rollups
test_compact_envelope_keeps_primary_text_artifact_not_first_binary
test_binary_artifact_is_not_claimed_as_recoverable_primary_text
test_rollup_exact_replay_matches_live_payload
```

### 25.6 Window LLM compaction

```text
test_compaction_below_trigger_does_not_start
test_manual_force_still_honors_pending_and_inflight_barriers
test_compaction_source_document_is_canonical_and_cited
test_compaction_pair_group_is_not_split
test_protected_current_run_tail_is_in_post_estimate
test_summary_over_output_budget_records_observed_measurement
test_invalid_summary_citation_fails
test_compaction_failure_keeps_old_window_open
test_compaction_success_closes_old_and_opens_new_atomically
test_compaction_cancel_before_started_has_no_terminal_obligation
test_compaction_cancel_after_started_registers_terminal_owner
test_compaction_terminal_publication_cancel_confirms_and_switches_window
test_compaction_partial_terminal_batch_latches
test_compaction_blocking_artifact_writer_blocks_destructive_close
test_compaction_restart_repairs_started_without_terminal
test_new_window_snapshot_uses_summary_and_protected_tail
test_exploration_compaction_admission_unreachable_transitions_then_restarts
test_finalization_compaction_bucket_uses_actual_summarizer_physical_quote
test_window_compaction_unavailable_exhausts_without_borrowing_agent_reserve
```

### 25.7 Rollout/finalization

```text
test_rollout_account_weighted_usage_math
test_root_account_close_and_run_end_commit_atomically
test_child_subaccount_close_and_child_run_end_commit_atomically
test_parent_settlement_joins_child_subaccount_close_and_native_terminal_reference
test_account_close_rejects_active_reservations
test_cached_input_is_subset_of_input_and_charged_once
test_model_reservation_uses_pre_margin_input_and_effective_output_physical_bounds
test_missing_usage_settles_full_physical_reservation_quote
test_missing_usage_never_uses_stream_chars_or_cached_discount
test_start_committed_but_provider_not_dispatched_settles_zero_not_full_quote
test_reported_input_above_estimate_within_physical_bound_does_not_latch
test_reported_input_above_resolved_physical_bound_latches
test_model_call_requires_active_reservation
test_model_call_stream_requires_session_scoped_commit_port
test_model_call_start_requires_lifecycle_specific_recovery_plan_matrix
test_model_stream_recovery_plan_ids_match_atomic_start_companions
test_main_model_start_freezes_event_safe_run_activation_in_recovery_plan
test_direct_and_window_recovery_plans_reject_run_activation
test_run_activation_uses_durable_owner_and_generation_without_process_segment_id
test_main_model_start_freezes_versioned_control_downstream_predicate_contract
test_direct_and_window_recovery_plans_reject_control_downstream_predicate_contract
test_control_downstream_contract_requires_all_run_end_terminal_variants
test_control_downstream_contract_drift_or_unknown_domain_event_fails_closed
test_start_stream_registers_handle_before_worker_enters_validation_or_transport
test_model_stream_worker_task_start_failure_removes_prestart_owner_without_run_fact
test_old_model_stream_generation_done_callback_cannot_remove_new_handle
test_late_subscription_catches_up_from_model_start_without_notification_gap
test_subscription_break_detaches_without_stopping_transport_or_terminalization
test_subscription_task_cancel_detaches_without_cancelling_stream_worker
test_no_subscribers_still_drives_stream_to_terminal_and_settlement
test_subscription_close_state_records_typed_reason_last_sequence_and_terminal_cursor
test_request_cancel_is_only_public_model_worker_cancellation_authority
test_slow_subscription_is_detached_and_can_replay_from_last_confirmed_sequence
test_lagged_subscription_does_not_truncate_agent_canonical_text_or_tool_calls
test_direct_call_materializes_complete_result_after_observer_lag_detach
test_window_summarizer_uses_materialized_committed_result_not_subscription_buffer
test_model_result_materializer_rebuilds_text_thinking_data_tools_error_and_usage_from_ledger
test_model_result_materializer_rejects_semantic_index_gap_or_unclosed_block_on_completed_outcome
test_cancelled_or_runtime_error_result_marks_open_blocks_interrupted_and_never_executes_partial_tool_call
test_closed_tool_call_then_provider_error_is_audit_only_and_never_executes
test_closed_tool_call_then_user_cancel_is_audit_only_and_never_executes
test_closed_tool_call_then_runtime_error_is_audit_only_and_never_executes
test_non_success_closed_text_is_not_delivered_as_final_reply_or_successful_direct_result
test_non_success_model_fragments_are_excluded_from_canonical_transcript_replay
test_completed_terminal_is_required_before_any_tool_call_can_enter_execution_gate
test_stop_intent_after_completed_terminal_but_before_tool_gate_blocks_execution
test_completed_main_call_requires_durable_control_disposition_before_tool_or_reply
test_control_disposition_event_requires_exact_call_result_and_start_activation_join
test_control_disposition_rejects_process_segment_id_as_durable_identity
test_termination_intent_wins_shared_control_lock_and_commits_suppressed_disposition
test_suppressed_termination_attribution_requires_matching_run_activation
test_accepted_disposition_full_installs_exact_control_permit_before_unlock
test_control_permit_is_segment_scoped_and_rejects_stale_resume_generation
test_control_disposition_none_retries_same_bytes_without_execution_permit
test_control_disposition_partial_unknown_or_conflict_latches_and_blocks_execution
test_control_disposition_publication_after_commit_folds_full_before_permit
test_control_disposition_reducer_fold_is_synchronous_and_never_calls_observer
test_control_disposition_releases_run_control_lock_before_ordered_publication
test_control_disposition_observer_callback_can_stop_run_without_deadlock
test_control_disposition_observer_callback_can_request_close_without_self_join_deadlock
test_control_disposition_observer_failure_does_not_revoke_durable_winner_or_permit
test_control_disposition_publication_waiter_cancel_detaches_owned_publisher_retry
test_accepted_first_then_later_stop_does_not_rewrite_disposition_but_cancels_downstream
test_reopen_completed_main_without_disposition_writes_suppressed_by_recovery
test_reopen_control_suppression_reuses_start_frozen_run_activation
test_recovery_disposition_guard_contains_no_process_segment_id_or_live_segment_fields
test_recovery_disposition_guard_cas_validates_reopen_high_water_and_zero_downstream_facts
test_recovery_disposition_full_folds_without_ever_creating_control_permit
test_recovery_disposition_late_valid_accepted_winner_is_folded_without_suppressed_overwrite
test_recovery_disposition_late_conflicting_payload_latches
test_recovery_disposition_downstream_fact_arriving_before_commit_cas_latches
test_recovery_disposition_existing_accepted_must_precede_every_downstream_fact
test_recovery_disposition_existing_suppressed_with_accepted_only_downstream_fact_latches
test_recovery_disposition_cas_stale_without_winner_refreezes_guard_not_event_bytes
test_recovery_completed_with_normal_run_end_and_missing_disposition_latches
test_recovery_accepted_sequence_before_normal_run_end_is_valid
test_recovery_completed_without_downstream_writes_suppressed_before_recovered_interrupted_run_end
test_recovery_user_stop_or_host_teardown_run_end_requires_prior_accepted_or_termination_suppressed
test_recovery_recovered_interrupted_run_end_requires_prior_accepted_or_recovery_suppressed
test_recovery_execution_failure_run_end_requires_prior_accepted
test_model_control_downstream_contract_rejects_unregistered_event_variant
test_reopen_downstream_effect_without_accepted_disposition_latches
test_transcript_requires_completed_result_and_accepted_disposition
test_wait_result_waiter_cancellation_does_not_cancel_materialization_or_worker
test_runtime_close_drains_model_stream_handles_commit_workers_and_physical_operations
test_main_model_start_batch_atomically_commits_reply_reservation_and_model_start
test_main_model_terminal_batch_atomically_commits_model_end_settlement_and_reply_end
test_reply_end_requires_model_terminal_outcome_without_default_or_legacy_fallback
test_main_reply_end_requires_exact_matching_model_terminal_outcome
test_transcript_rejects_reply_end_and_model_end_outcome_mismatch
test_model_control_disposition_uses_event_type_union_schema_and_historical_decoder
test_public_model_notification_subscription_contains_committed_notifications_only
test_llm_runtime_commits_typed_provider_semantic_draft_before_notification
test_transport_semantic_union_rejects_unknown_kind_and_variant_field_mismatch
test_transport_semantic_union_rejects_lifecycle_like_or_arbitrary_json_draft
test_transport_semantic_union_maps_each_variant_to_exactly_one_agent_event
test_transport_api_does_not_accept_event_context_writer_or_lifecycle_ids
test_transport_open_stream_returns_owned_cancelable_execution_not_async_generator
test_transport_open_stream_factory_performs_no_network_before_execution_registration
test_every_model_semantic_event_requires_exact_call_start_index_and_draft_attribution
test_provider_error_draft_maps_to_dedicated_model_stream_error_event_not_generic_run_error
test_provider_model_stream_error_uses_event_type_enum_and_agent_event_union
test_provider_model_stream_error_raw_envelope_round_trips_schema_and_domain_binding
test_historical_decoder_rebinds_provider_model_stream_error_schema_version
test_provider_error_sanitizer_redacts_bearer_basic_api_key_cookie_and_password
test_provider_error_sanitizer_removes_url_userinfo_query_and_fragment
test_provider_error_sanitizer_normalizes_stable_code_and_bounds_message_diagnostics
test_provider_error_sanitizer_contract_drift_fails_before_error_event_commit
test_same_transport_id_version_cannot_register_two_error_sanitization_contracts
test_provider_error_raw_payload_never_reaches_event_metadata_log_or_general_artifact
test_composition_root_rejects_unwrapped_raw_provider_transport_binding
test_central_sanitizing_execution_is_only_public_provider_transport_execution
test_raw_adapter_factory_exception_returns_prefailed_sanitizing_execution_without_raising
test_raw_sdk_http_network_exception_returns_sanitized_error_draft_without_raising
test_error_draft_next_index_and_terminal_semantic_count_are_deterministic
test_error_draft_is_followed_by_prebuilt_terminal_without_another_provider_read
test_provider_error_terminal_waits_for_inner_physical_drain
test_raw_exception_from_cancel_close_or_completion_never_crosses_public_wrapper
test_physical_completion_blocked_untrusted_preserves_owner_and_forbids_terminal_commit
test_sanitizer_exception_uses_preconstructed_constant_fallback_without_raw_message
test_caught_raw_exception_context_traceback_and_response_are_not_retained_or_logged
test_raise_from_none_sanitized_failure_is_not_a_supported_transport_protocol
test_matching_cancellation_wins_without_fabricating_provider_error
test_failure_frozen_before_cancel_keeps_provider_error_outcome_and_only_accelerates_drain
test_unexpected_public_wrapper_exception_before_error_uses_constant_runtime_containment_and_latches_binding
test_public_wrapper_fault_after_full_provider_error_preserves_provider_error_winner
test_transport_block_state_machine_rejects_delta_before_start_and_duplicate_end
test_transport_terminal_draft_is_measurement_not_stored_event
test_transport_requires_exactly_one_final_terminal_draft_on_normal_or_provider_error
test_transport_semantic_indices_are_contiguous_and_stable_event_ids_are_deterministic
test_llm_runtime_builds_model_end_from_terminal_draft_and_commits_before_notification
test_semantic_full_publication_failure_is_confirmed_and_terminalized
test_provider_error_semantic_full_publication_failure_does_not_change_outcome_to_runtime_error
test_semantic_none_retries_same_stable_candidate
test_semantic_partial_or_unknown_keeps_stream_owner_and_blocks_close
test_subscription_cancel_before_and_after_semantic_commit_never_owns_confirmation
test_transport_cannot_emit_stored_event_or_any_reply_model_lifecycle
test_semantic_commit_ignores_concurrent_child_and_tool_account_settlements
test_terminal_commit_rebases_cas_on_latest_account_state_without_changing_event_bytes
test_terminal_commit_requires_exact_active_reservation_and_quote
test_direct_terminal_commit_never_reads_rollout_account
test_model_start_stream_port_confirms_cancel_after_commit_before_transport
test_model_start_full_publication_failure_terminalizes_reply_before_propagating
test_model_end_stream_port_handoffs_full_batch_before_direct_collector_returns
test_not_rollout_accounted_direct_call_still_commits_start_and_end_via_same_port
test_direct_call_has_no_reply_lifecycle_and_collector_does_not_wait_for_reply_end
test_cancelled_started_model_call_is_terminalized_by_same_stream_owner
test_awaitable_cancel_signal_interrupts_never_returning_transport_read
test_user_stop_calls_exact_transport_request_cancel_and_aclose
test_host_close_deadline_preserves_stream_owner_while_physical_read_is_running
test_cancelled_model_terminal_does_not_commit_before_transport_physical_completion
test_late_read_callback_cannot_mutate_new_handle_generation
test_model_commit_unknown_keeps_stream_owner_and_blocks_close
test_reopen_repairs_main_start_without_end_with_full_quote_and_reply_end
test_reopen_repairs_direct_start_without_end_with_model_end_only
test_reopen_repairs_window_start_without_end_then_compaction_failed
test_reopen_main_error_event_without_end_recovers_provider_error_settlement_and_reply_end
test_reopen_direct_error_event_without_end_recovers_provider_error_end_only
test_reopen_window_error_event_without_end_recovers_provider_error_then_compaction_failed
test_reopen_error_event_recovery_preserves_original_sanitized_error_fact
test_reopen_multiple_provider_errors_latches_without_terminal_guess
test_reopen_provider_error_not_last_semantic_item_latches
test_reopen_provider_error_attribution_mismatch_latches
test_reopen_no_provider_error_event_still_recovers_runtime_error
test_reopen_conservatively_treats_missing_dispatch_proof_as_dispatched
test_model_stream_recovery_reuses_start_frozen_terminal_ids
test_model_stream_recovery_partial_atomic_terminal_batch_latches_without_completion
test_generic_reservation_repair_does_not_double_settle_model_stream_reservation
test_model_call_end_and_same_reservation_settlement_commit_atomically
test_model_terminal_batch_none_retries_without_visible_end
test_model_terminal_batch_partial_latches_and_blocks_next_admission
test_phase_transitions_are_monotonic
test_warning_restricted_finalization_thresholds
test_exploration_admission_unreachable_transitions_to_finalization_with_actual_call
test_active_reclaimable_exploration_reservation_prevents_early_unreachable_transition
test_exhausted_uses_actual_resolved_finalization_call_not_abstract_minimum
test_finalization_reserve_preserves_two_calls
test_finalization_reserve_separately_preserves_one_window_compaction
test_finalization_reserve_separately_preserves_synthesis_and_verification_tools
test_rollout_feasibility_matrix_enumerates_all_enabled_primary_summarizer_pairs
test_rollout_feasibility_matrix_enumerates_every_launchable_child_primary_summarizer_pair
test_child_window_policy_is_derived_from_child_primary_target_not_parent
test_resolved_child_budget_joins_child_window_policy_fingerprint
test_child_budget_quote_uses_target_semantics_without_fabricating_model_call_id
test_child_usage_aggregate_counts_mixed_settlement_bases_without_lossy_status
test_child_reported_subset_tokens_exclude_reserved_missing_and_cancelled_calls
test_child_aggregate_close_and_handoff_require_same_nested_subaccount_fingerprint
test_child_close_blocks_when_any_settlement_is_partial_or_unknown
test_parent_handoff_copies_exact_child_settlement_aggregate_and_charge
test_child_run_start_none_atomically_settles_parent_reservation_zero
test_native_child_cancel_keeps_owner_until_atomic_parent_handoff
test_materialized_batch_repair_atomically_settles_mixed_children_after_cancel
test_rollout_feasibility_rejects_non_positive_exploration_allowance
test_infeasible_pair_fails_production_host_configuration_before_session_open
test_disabled_diagnostic_pair_is_reported_but_cannot_be_selected
test_config_check_reports_total_reserve_components_and_exploration_allowance
test_pre_run_pair_rechecks_same_policy_and_model_fingerprints
test_pre_run_rejects_pair_that_drifted_since_configuration_validation
test_exploration_cannot_borrow_any_finalization_bucket
test_window_compaction_started_and_rollout_reservation_commit_atomically
test_window_compaction_completed_references_full_summarizer_settlement
test_window_compaction_cancel_terminalizes_model_end_then_fails_window_compaction
test_forbidden_exploration_tool_returns_typed_deny_in_finalization
test_terminal_invocation_classifier_distinguishes_search_write_and_verification
test_unknown_terminal_action_is_denied_in_finalization
test_tool_gate_allow_and_rollout_reservation_commit_atomically
test_tool_wait_does_not_create_rollout_reservation
test_tool_result_and_reservation_settlement_commit_atomically
test_tool_executor_cannot_append_tool_result_end_directly
test_mcp_suspend_keeps_original_tool_reservation
test_mcp_resume_deny_cancel_and_timeout_settle_original_reservation_atomically
test_tool_terminal_unknown_keeps_single_terminal_owner_and_blocks_close
test_same_action_classifier_id_version_different_contract_conflicts
test_deny_result_still_leaves_one_final_model_call
test_artifact_hydration_allowed_in_finalization
test_bounded_verification_allowed_in_finalization
test_new_subagent_exploration_denied_in_finalization
test_concurrent_child_reservations_do_not_oversell_parent
test_child_rollout_budget_uses_frozen_primary_summarizer_targets_and_policy_formula
test_child_budget_snapshot_requires_versioned_rollout_policy_but_not_future_parent_remaining
test_child_actual_start_atomically_commits_resolved_budget_graph_start_and_reservation
test_independent_child_batch_reservations_are_all_or_none
test_dependency_waiting_child_reserves_only_when_actual_start_wins
test_child_below_one_actual_model_call_reservation_fails_before_run_start
test_child_unknown_terminal_keeps_rollout_reservation
test_emergency_counter_is_not_reported_as_normal_budget_exhaustion
```

### 25.8 Neutral rollout status hint

```text
test_exploration_without_exact_recurrence_does_not_inject_model_hint
test_l0b_status_shadow_is_excluded_from_context_semantic_input_and_provider_payload
test_l5_cutover_materializes_shadow_recurrence_as_model_visible_candidate
test_model_call_count_alone_does_not_trigger_hint
test_non_exploration_hint_reports_phase_counts_and_remaining_allowance
test_hint_contains_no_continue_stop_finalize_or_next_step_directive
test_exact_recurrence_requires_same_normalized_action_and_terminal_outcome
test_exact_recurrence_allows_interleaved_hydration_and_control_calls
test_same_action_with_different_terminal_outcomes_does_not_trigger_hint
test_recurrence_window_and_entry_count_are_bounded
test_recurrence_does_not_change_phase_gate_permission_or_reservation
test_status_hint_is_derived_from_frozen_high_water
test_status_hint_exact_replay_matches_live_candidate
test_inspector_can_show_counts_when_model_hint_is_not_injected
```

### 25.9 Cancellation/close/recovery

```text
test_stop_during_projection_commit_confirms_before_terminalization
test_stop_during_compaction_terminalizes_started
test_stop_during_model_reservation_settles_or_preserves_owner
test_sync_tool_thread_keeps_borrow_until_real_completion
test_close_failure_preserves_host_session_terminal_lease_and_workspace
test_shared_close_waiters_receive_long_horizon_drain_error
test_retry_close_reuses_session_owned_pending_owners
test_restart_latches_unknown_or_partial_ledger_state
test_reducer_rebuild_does_not_clear_structural_latch
test_no_completed_run_owner_remains_after_deferred_borrows_drain
```

### 25.10 Inspector/replay

```text
test_inspector_distinguishes_artifact_missing_contract_mismatch_and_ledger_untrusted
test_inspector_projects_window_timeline
test_inspector_projects_projection_token_savings
test_inspector_projects_rollout_phase_and_finalization_reserve
test_inspector_projects_live_detached_and_recovered_model_stream_states
test_inspector_distinguishes_awaiting_accepted_termination_suppressed_and_recovery_suppressed_control
test_inspector_control_projection_uses_start_frozen_activation_not_current_process_segment
test_inspector_projects_neutral_rollout_status_and_exact_recurrence
test_inspector_projects_checkpoint_and_delta_counts
test_inspector_distinguishes_graph_semantic_source_from_checkpoint_acceleration
test_inspector_projects_preferred_actual_checkpoint_and_rebased_status
test_exact_replay_rebuilds_window_projection_rollout_and_status_hint
test_selection_mismatch_is_contract_mismatch_not_ledger_untrusted
test_process_local_build_fingerprint_is_not_historical_fact
```

### 25.11 Real LLM与dogfood

```text
test_real_llm_long_horizon_repeated_search_converges
test_real_llm_long_horizon_writes_requested_artifact_before_finalization
test_real_llm_long_horizon_tool_projection_exceeds_old_36k_chars
test_real_llm_long_horizon_current_run_projection_preserves_pairing
test_real_llm_long_horizon_window_compaction_continues_same_run
test_real_llm_long_horizon_finalization_reserve_survives_denied_search
test_real_llm_long_horizon_subagent_budget_is_parent_bounded
test_real_llm_long_horizon_exact_replay_matches_live_manifest
```

真实trajectory验收不能只断言最终文本出现。必须打印/保存：phase、model/tool counts、projection generations、window transitions、
neutral status hint、exact recurrence、finalization reserve与artifact事实。不得把recurrence解释成“无进展”或自动停止理由。

---

## 26. Architecture guards与grep gates

### 26.1 禁止旧aggregate真源

生产源码必须匹配零结果：

```bash
rg -n 'tool_result_context_chars|36_000|36000' src/pulsara_agent
rg -n 'chars_per_token|event_chars_per_token' src/pulsara_agent/runtime/long_horizon
rg -n 'len\(.+tool.+result.+\).+context' src/pulsara_agent/runtime
```

允许测试/迁移文档显式引用旧值；production architecture test按AST/import边界执行，不只靠字符串grep。

### 26.2 禁止mutable/dual truth

```bash
rg -n 'scratchpad.*(window|projection|rollout|status_hint|recurrence|compaction)' src/pulsara_agent
rg -n 'state\.messages.*(truncate|clear|compact)' src/pulsara_agent
rg -n 'ContextCompileInputs|build_llm_context\(|msg_to_llm_messages\(' src/pulsara_agent
rg -n 'max_turns\s*=\s*50|max_tool_calls\s*=\s*64' src/pulsara_agent
rg -n 'SubagentGraphSourceRangeFact|checkpoint_source_range|subagent_graph_source_range' src/pulsara_agent
rg -n 'source_from_sequence|source_through_sequence' src/pulsara_agent/primitives/context.py
```

第一条guard只禁止把window/projection/rollout/status/recurrence/compaction的durable semantic truth写入scratchpad。
Memory hook中按run缓存的working-context/recall projection是可丢弃的process-local collection cache，不是compiler或long-horizon
authority，因此不应被该guard误判；architecture test必须按字段名与import/consumer边界精确匹配，不能禁止所有包含
`projection`字样的scratchpad cache key。

最后一条由AST architecture test限定在`ContextCandidateSourceSelectionFact(source_instance_id="subagent:results")`旧字段；其他context
authority/transcript high-water仍合法使用`source_through_sequence`。另设import guard：`runtime/context_input/{live,replay}.py`不得import
`checkpoint_doctor`，production restore不得调用sequence-1 full-fold helper。

另加AST ownership guards：

- `tools/executor.py`不得构造、append或publish `ToolResultEndEvent`；
- transport adapter不得构造/提交`ReplyStartEvent`、`ModelCallStartEvent`、`ModelCallEndEvent`或`ReplyEndEvent`；
- production不得存在直接返回iterator的`LLMRuntime.stream()`；唯一入口是返回registered `ModelStreamExecutionHandle`的`start_stream()`；
- 只有`ModelStreamExecutionRegistry`持有的worker可以拉取transport；subscriber/AgentRuntime/direct collector不得直接调用transport `__anext__()`；
- AgentRuntime/direct collector不得对`CommittedModelStreamNotification`中的任何event再次调用`RuntimeSession.emit()`；
- 公开LLMRuntime API不得返回`ProviderSemanticEventCandidate`或其他需要caller durable acknowledgement的candidate；
- transport/adapters不得构造`FrozenEventWriteCandidate`、选择stored event ID或调用EventLog writer；
- transport semantic output必须是versioned discriminated union；禁止`draft_kind: str + dict/FrozenJsonObjectFact`通用入口；
- model semantic events必须有typed call/start/index/draft attribution；recovery/Inspector不得解析event ID或metadata推断stream cursor；
- control disposition的ordered publisher/observer方法不得在`control_linearization_lock`词法作用域内调用；锁内只允许ledger
  commit/confirmation、同步reducer fold与permit CAS；
- `ModelCallControlDispositionRecoveryService`不得构造`LiveModelCallControlDispositionCommitGuard`或`ModelCallControlPermit`；live coordinator不得构造
  `RecoveryModelCallControlDispositionCommitGuard`；
- recovery downstream scan必须消费matching ModelStart冻结的`ModelCallControlDownstreamPredicateContractFact`；禁止只硬编码tool facts、忽略RunEnd，
  或按当前latest registry替历史call补造predicate；
- semantic commit不得读取/CAS rollout account；terminal commit不得复用start-time account fingerprint；
- tool allow path必须经`ToolExecutionEventCommitPort`，不得把gate、reservation、terminal、settlement拆成独立writer；
- rollout accounting不得读取stream text chars/bytes/chunk count估算missing usage；
- production runtime不得import privileged checkpoint GC/doctor entrypoint；
- 不得重新引入单一`preparation_attempt_number`替代`safe_point_revision + compile_attempt_index`。
- production hot-path modules `runtime/context_input/live.py`与`runtime/compaction/service.py`默认禁止调用`EventLog.iter()`，不能只检查
  顶层coordinator函数并允许helper绕过；只有命名明确、不会被live compile/PRE_RUN调用的privileged offline repair模块可进入allowlist；
- Host PRE_RUN不得调用`_prior_messages()`、`read_event_snapshot_through_current()`、`rebuild_prior_messages()`或
  `rebuild_prior_messages_before_sequence()`等legacy full-scan helper；必须使用bounded transcript projection；
- Context live authority与child parent attribution的range/run reads必须显式传入events/bytes双cap。`RunStart`冻结的transcript
  checkpoint basis独立于本次preflight terminal；没有新preflight compaction不能成为退回sequence 1无界读取的条件；
- compaction observation与prior transcript只能消费accepted-disposition reducer的canonical reply view；禁止根据`ReplyEnd(completed)`
  单独纳入assistant text/tool call。

L0A另加storage architecture guards：

- checkpoint/graph/exact-replay raw read路径不得调用current `load_agent_event()`或直接依赖current `AgentEvent` union；
- `agent_events` insert必须显式写per-event schema version/fingerprint/domain fingerprint；
- production reader不得用全局`AGENT_EVENT_SCHEMA_VERSION`为旧row补造per-event identity；
- `FrozenEventWriteCandidate`与raw envelope必须携带并验证exact schema/domain identity；
- `ResolvedModelTargetFact`必须显式含`runtime_observation_carrier` key，adapter不得从当前配置补造。

Long-horizon planner不得import：

- `host.session`；
- provider adapter；
- Postgres concrete store；
- `LoopState`；
- MCP supervisor；
- wall-clock global helper（时间必须来自source facts）。

### 26.3 禁止JSON inference与通用progress子系统回流

Rollup/essential builder与exact recurrence必须从descriptor、typed arguments和typed terminal outcome解析。Architecture test禁止
coordinator/planner：

- `json.loads(tool_result.output)`后判断domain keys；
- `if tool_name == ...`构造通用novelty/evidence结论；
- 字符串搜索URL/observed_at/process_id决定semantics；
- 从rendered text反推timing/artifact inclusion。

Unknown descriptor只允许generic display，不允许rollup或exact recurrence推断。生产源码还必须禁止重新引入
`EvidenceProgressStateFact`、`EvidenceProgressEvaluatedEvent`、通用novelty reducer或3/5/7 progress phase联动。

### 26.4 Event/schema guard

- 所有new event进入serialization registry；
- event type必须使用canonical `EventType` enum成员；禁止只加裸string literal而漏掉AgentEvent union、historical decoder或domain registry；
- required字段无default/nullable compatibility；
- event-safe DTO不得包含callable、manager、lease、raw credential、endpoint query；
- provider error event/draft只能携带通过run-bound sanitization contract验证的typed sanitized fact；禁止raw exception/headers/body/URL query进入
  event、metadata、普通artifact或logger；
- nested facts recursive freeze/JSON thaw；
- semantic fingerprint排除random IDs/timing diagnostics；
- fact fingerprint包含attribution；
- schema negative tests与pure reducer negative twin一一对应。

Model stream architecture guards还必须禁止：

- AgentRuntime、direct caller、window summarizer从notification subscription累计canonical text/tool calls/usage；
- 任意caller仅凭`CommittedModelToolCallFact.completion_status="completed"`执行tool，而未先验证整个model terminal为completed并取得successful
  control view；
- completed main call缺少FULL `ModelCallControlDispositionResolvedEvent(ACCEPTED)`时执行tool、交付final text或投影canonical transcript；
- termination intent安装与control disposition提交绕过shared run-control linearization lock，或从process-local owner/scratchpad推断historical accepted；
- ToolCallEnd后、ModelEnd FULL/result materialization前的speculative tool execution，或non-success text进入final reply/canonical transcript；
- production控制代码以`async for transport.stream(...)`直接驱动provider；
- production registry直接返回provider SDK/raw adapter execution，或`llm/runtime.py` import adapter-private raw protocol/exception；
- 用`ProviderTransportSanitizedFailure`、`raise ... from None`或任意exception作为正常provider/network failure channel；
- central wrapper捕获raw exception后读取/记录其`str/repr/traceback/__cause__/__context__`，或把raw对象保存进state/Future/task result；
- 仅取消awaiting coroutine、却在physical provider read仍运行时释放stream owner或提交cancelled terminal；
- `ProviderModelStreamErrorEvent.type`绕过`EventType.PROVIDER_MODEL_STREAM_ERROR`。

---

## 27. 长期contract迁移矩阵

### L0A

- `contracts/EVENT_LOG_STORAGE_CONTRACT.zh.md`
  - checkpoint artifact/event；
  - atomic checkpoint catalog + canonical delta snapshot；
  - graph semantic accumulator与all-event ledger continuity accumulator分离；checkpoint event只进入后者并对graph fold deterministic no-op；
  - stable batch confirmation；
  - versioned event-schema domain binding；同一type/schema version的domain不可重分类；
  - per-event schema version/fingerprint/domain columns、raw stored envelope与pre-union historical read；
  - schema fingerprint canonicalization、durable decoder contract fingerprint及historical decoder registry；
- `contracts/RUNTIME_SEMANTIC_GRAPH_CONTRACT.zh.md`
  - EventLog + versioned reducer是唯一authority；
  - reducer ID/version/contract fingerprint；
  - semantic source与checkpoint acceleration分离；
  - production bounded bootstrap/hot path与privileged offline full-fold repair；
  - reducer只冻结supported graph event entries，无关non-graph schema新增不漂移旧run contract；
- `contracts/ARTIFACT_STORE_CONTRACT.zh.md`
  - checkpoint artifact可GC、可确定性重建，不被historical manifest永久pin；
- `contracts/INSPECTOR_PROJECTION_CONTRACT.zh.md`
  - semantic source、preferred/actual checkpoint、delta与rebase audit。

### L0B/L1

- `contracts/AGENT_RUNTIME_LOOP_CONTRACT.zh.md`
  - required run window/account；
  - model-step safe point；
  - AgentRuntime控制侧只消费EventLog-backed `CommittedModelCallResult`；notification subscription仅供UI observation，不驱动transport、不聚合
    canonical result、不拥有worker、不持久化semantic/lifecycle；只有terminal completed的successful control view可进入tool/reply gate，gate前重查
    termination intent并FULL commit typed control disposition；stop intent与accepted event共用run-control linearization lock；stop通过handle
    request-cancel；
- `contracts/MESSAGE_TRANSCRIPT_CONTEXT_CONTRACT.zh.md`
  - window/projection/tool pairing；
  - dynamic token allocation；
  - provider-error/cancel/runtime-error的closed/partial model fragments均为audit-only，不进入canonical assistant/tool-call transcript；
  - completed main reply还必须join durable `ModelCallControlDispositionResolvedEvent(ACCEPTED)`；suppressed/missing disposition不进入transcript；
- `contracts/CONTEXT_COMPACTION_CONTINUITY_CONTRACT.zh.md`
  - cross-run prefix compaction与same-run window compaction分离；
- `contracts/LLM_TRANSPORT_CONTRACT.zh.md`
  - long-horizon manifest/physical-bound rollout reservation pre-send join；
  - LLMRuntime拥有完整model stream持久化ownership；
  - production registry只暴露central `SanitizingLLMTransport/Execution`；adapter raw factory/execution为private，transport不接收
    EventContext/writer，只产versioned discriminated semantic/terminal measurement drafts，不产arbitrary JSON、stored event或lifecycle；
  - 每个committed model semantic event required保存call/start/index/draft attribution，provider error使用专用event；
  - service-owned handle独立驱动可取消的transport execution，public subscription只观察committed notifications，不存在caller acknowledgement、
    pull-to-progress或canonical result authority；terminal FULL后从EventLog materialize完整result；
  - start/semantic/terminal split commit CAS、awaitable handle cancellation signal、blocked read race、exact transport aclose与physical operation drain；
  - provider/network failure在wrapper task内正常返回sanitized error+terminal drafts；禁止raw exception、sanitized exception旁路、exception chain/log；
    provider error使用stable code、recursive secret/URL redaction、bounded diagnostics、constant fallback与sanitization contract rebind；
  - provider dispatch status区分可证明未发送的zero settlement与已发送但usage missing的full-quote settlement；
- `contracts/EVENT_LOG_STORAGE_CONTRACT.zh.md`
  - provider stream error与model control disposition两个canonical EventType进入AgentEvent union、per-event schema/domain registry、raw envelope与
    historical decoder；
- `contracts/RUNTIME_EVENT_PUBLISHING_HOOKS_CONTRACT.zh.md`
  - lifecycle batch FULL handoff、ordered publish/fold与notification不得二次append；
  - model control disposition的ledger confirm/fold与observer notification分相：fold在线性化锁内且无callback，ordered notification在锁外；observer
    failure不得撤销durable winner或process-local permit。
- `contracts/RECOVERY_CONTRACT.zh.md`
  - `ModelStreamRecoveryPlanFact`、三类Start-without-End repair、full-quote conservative settlement与stable terminal IDs；
  - semantic prefix末尾唯一provider error恢复provider-error End；无error恢复runtime-error；invalid error ordering/attribution latch；
  - completed main ModelEnd缺control disposition时使用独立recovery guard、Start-frozen activation与reopen high-water CAS；不构造live segment
    guard/permit；Start-frozen versioned downstream predicate与normal/user-stop/host-teardown/recovered-interrupted/execution-failure RunEnd的required
    prior disposition矩阵；suppressed-by-recovery、late existing winner与downstream-without-accepted structural latch；

### L2–L4

- `contracts/LLM_TRANSPORT_CONTRACT.zh.md`
  - `ResolvedModelTargetFact`完整runtime-observation carrier fact、registry rebind与provider wire lowering；
- `contracts/TERMINAL_OUTPUT_THREE_LAYER_CONTRACT.zh.md`
  - projection representations、artifact-aware thinning、timing/essential保留；
- `contracts/ARTIFACT_STORE_CONTRACT.zh.md`
  - rollup/summary/checkpoint deterministic artifact；
- `contracts/RUNTIME_EVENT_PUBLISHING_HOOKS_CONTRACT.zh.md`
  - publication-after-commit fold/owner语义；
- `contracts/RECOVERY_CONTRACT.zh.md`
  - window/compaction/rewrite recovery；
- `contracts/HOST_RESUME_CONTRACT.zh.md`
  - active window/phase rebind、close blocker。

### L5

- `contracts/CAPABILITY_SURFACE_CONTRACT.zh.md`
  - action class、phase narrowing、normalized action fingerprint contract；
- `contracts/BUILTIN_TOOLS_CONTRACT.zh.md`
  - per-tool cost/action/rollup classification；
- `contracts/MCP_CAPABILITY_CONTRACT.zh.md`
  - MCP descriptor long-horizon policy，不改变pending lease ownership；
- `contracts/AGENT_RUNTIME_LOOP_CONTRACT.zh.md`
  - rollout phase/finalization/emergency、静态可行性、中性status hint与child settlement-basis aggregate；
- `contracts/EVAL_DOGFOOD_GATE_CONTRACT.zh.md`
  - 长程trajectory门禁；
- `contracts/INSPECTOR_PROJECTION_CONTRACT.zh.md`
  - 最终全部projection。

长期contract与对应production代码必须同PR迁移，不能提前承诺尚未实现字段，也不能推迟到最后统一补文档。

---

## 28. 数据迁移、上线与删除策略

### 28.1 未上线阶段采用hard reset优先

本项目尚未上线时：

- new required RunStart/RunEnd/event schema不保留nullable reader；
- development PostgreSQL可reset；
- 如保留fixture，写显式one-shot migration，不在production reader推断；
- old run没有window/account fact时不可resume；
- Inspector可将legacy ledger标`unsupported_legacy_schema`，但AgentRuntime不得运行它。

### 28.2 不保留feature flag双写

禁止：

- 同时维护36K char cap和token projection；
- 同时从state.messages和projection state渲染；
- 同时由LoopBudget counter和rollout account终止；
- 同时由rendered/raw result text和typed action/terminal facts产生recurrence；
- 同时用old inline compaction和new window compaction处理current run；
- production compile/replay同时full fold graph与checkpoint restore再“择一信任”；
- 把checkpoint ID、checkpoint schedule或delta长度写进candidate/snapshot/input semantic fingerprint；
- 从Inspector或live replay隐式调用privileged full-fold doctor。

短期PR additive只能发生在schema定义与尚未启用的pure代码层；production cutover当PR必须删除旧入口。

### 28.3 Rollout顺序

```text
reset/migrate dev ledger
-> L0A bounded checkpoint
-> L0B required passive facts
-> L1 token allocation
-> L2 historical projection
-> L3 current-run projection
-> L4 LLM window compaction
-> L5 rollout/finalization
-> full unit/Postgres/real-LLM/dogfood
```

每一步可单独revert代码commit，但不能用旧数据库schema与新代码混跑。

---

## 29. 与后续阶段的边界

### 29.1 ContextSource Ownership Hard Cut仍在阶段五

本章只增加long-horizon所需的typed phase/summary/rollup ingress，不建立完整source registry，也不迁移所有system、memory、capability、plan、recovery、subagent producer ownership。

阶段五继续负责：

- 正式`ContextSource` registry；
- 每个non-transcript byte唯一source attribution；
- 删除AgentRuntime collector字符串拼接；
- source只产facts/candidates，不产provider message；
- capability/memory/plan等artifact/content refs最终统一。

### 29.2 Prompt Cache仍在阶段六

Prompt Cache可以依赖本章稳定的：

- context window ID/generation；
- projection generation/state fingerprint；
- rollup/summary identity；
- rollout phase与tool execution policy identity；
- final provider-visible payload；
- exact tool render units。

但stable prefix lanes与所有source ownership仍等待阶段五。不得在本章为了cache重排system/tools或实现provider-specific cache controls。

### 29.3 Prior-art研究文档的定位

`PULSARA_LONG_HORIZON_BUDGET_PRIOR_ART_RESEARCH.zh.md`保留为研究输入；其中仍有效的是：

- 运行预算必须跨调用累计；
- finalization reserve；
- observation compaction与artifact offload；
- emergency cap不能替代正常phase控制。

其中通用provenance-aware evidence progress guard仅保留为被否决的研究分叉：阶段四不实现novelty ontology、progress reducer、
3/5/7收窄或基于“无新证据”的自动deny。替代策略是L5确定性rollout phase，以及只陈述预算、调用次数和exact recurrence事实的
中性status hint。

被本文取代的实施细节：

- chars/粗糙累计作为主预算；
- 单一context budget同时承担run horizon；
- 未typed的“clear observation”；
- 依赖mutable LoopState的compaction；
- 没有window/projection/event identity的in-place rewrite；
- 没有checkpoint边界的全ledger fold。

---

## 30. Definition of Done

只有同时满足以下条件，阶段四才可标记完成：

### 30.1 Correctness

- 每个run有完整window chain与rollout account；
- model call始终消费同一resolved target/estimator下的context；
- tool call/result pairing在所有projection/rollup/compaction后保持；
- current user、pending interaction、protected tail不丢失；
- phase单调、reservation不超卖、finalization reserve真实可用；
- exploration admission不可达时可由实际ResolvedModelCall确定性切入finalization，不会永久夹死；
- ReplyStart/ModelStart/ModelEnd/ReplyEnd分别通过split start/semantic/terminal commit CAS与required atomic reservation/settlement batch；semantic
  commit不受无关account mutation影响，terminal始终在latest account state结算exact reservation；
- service-owned model handle在subscriber detach后仍驱动typed transport union到terminal；public subscription只包含committed notifications，caller
  不会驱动transport、持久化、二次写入model-stream event或聚合canonical call result；
- terminal FULL后canonical text/thinking/data/tool calls/error/usage只从EventLog与semantic attribution materialize；observer lag/detach不改变
  Agent/direct/window控制结果；
- 只有整个model terminal outcome为completed时，闭合tool calls/text才有资格进入后续gate；provider-error/cancel/runtime-error中的所有fragment即使
  已闭合也仅供audit/UI，不执行副作用、不交付成功回复、不进入canonical transcript；completed main call还必须先FULL commit durable ACCEPTED
  disposition，stop intent与接纳共用线性化锁；锁内不调用publisher/observer，锁外observer failure不撤销winner/permit；suppressed/missing
  disposition均不能执行或replay；
- request-cancel通过awaitable signal打断blocked provider read并关闭exact transport execution；physical operation未退出前不得提交假的cancelled terminal、
  释放reservation或越过Host close；
- provider error在adapter boundary完成stable code mapping、recursive credential/URL redaction与bounded diagnostics；raw provider error不进入durable fact、
  metadata、普通artifact、日志、Future exception或exception chain；所有production binding均由central sanitizing execution包装，provider/network
  failure只以typed error+terminal drafts越界，sanitizer failure使用constant fallback；专用event使用canonical
  `EventType.PROVIDER_MODEL_STREAM_ERROR`并可historical decode；
- reopen能用Start-frozen recovery plan稳定修复main/direct/window三类Start-without-End，并阻止generic reservation repair双重结算；semantic prefix以
  唯一合法provider error结尾时保持provider-error winner，无error才使用runtime-error，ambiguous prefix fail closed；
- completed main call缺disposition时，reopen只用Start-frozen activation + ledger high-water recovery guard提交SUPPRESSED_BY_RECOVERY；不依赖旧
  segment ID、不创建permit，late winner/downstream race由CAS重扫矩阵确定性收口；Start-frozen downstream predicate只覆盖五类RunEnd及
  `CapabilityGateDecisionEvent`、tool reservation、`ToolExecutionSuspendedEvent`、`ToolResultEndEvent`；V1 predicate registry是DTO枚举的封闭集合，
  正常RunEnd缺在先ACCEPTED必定latch；
- rollup以hard-cut target schema冻结的完整carrier contract作为独立runtime observation进入provider payload；ordering anchor不产生任何tool-call/result归因，
  也不会把nonmember sibling扩展进rollup；
- 所有启用的primary/summarizer组合在配置期通过同一整数公式的可行性校验；
- status hint不从raw result body猜truth，也不改变phase、gate或permission；
- child usage以charged milliunits与各settlement basis counts精确join parent account；nested aggregate/close/handoff三方subaccount identity相等，reported
  token totals只描述provider-reported子集；
- cancellation/publication/unknown/partial都有owner和稳定恢复语义。
- prior transcript与preflight compaction使用同一个accepted-disposition reducer；`suppressed_by_termination`/
  `suppressed_by_recovery`的reply blocks只保留在ledger/Inspector，不进入未来transcript、summary或provider payload。

### 30.2 Boundedness

- compiler/replay不再从sequence 1无界fold subagent graph；
- production只使用confirmed checkpoint + bounded delta；新session bootstrap也受events/bytes双cap约束；
- live `SubagentGraphStateStore`同样从confirmed checkpoint + bounded delta初始化；仅`InMemoryEventLog` pytest double允许在
  bootstrap cap内做prefix reference fold，PostgreSQL production缺checkpoint时fail closed；
- child rollout admission/commit/tool/compaction只读取child-owned增量subaccount state与parent-owned root-account state，不再
  每次full fold parent/child ledger；
- PRE_RUN transcript使用最新durable compaction summary作为projection checkpoint，再按event type/sequence与reply ID索引读取
  bounded delta；无checkpoint首窗也受control events/bytes硬上限，不调用`read_event_snapshot_through_current()`或
  `rebuild_prior_messages()`全量路径；
- RunStart transcript fact独立保存当前checkpoint ID/terminal sequence/keep-after/window；本次preflight未触发时仍复用更早的
  durable checkpoint。ContextFactSnapshot从该frozen basis加bounded authority delta恢复，不把空preflight terminal解释为sequence-1读取；
- legacy preflight `ContextCompactionService`的source、attempt/failure与orphan terminal recovery使用bounded checkpoint delta或indexed sparse
  lifecycle reads；production `compact_if_needed()`单次读取并完成threshold/planning，不先`should_auto_compact()`再重复扫描；
- model subscription从安装时冻结cursor观察bounded committed history，不读取ledger；canonical result materialization按
  `resolved_model_call_id` expression index执行events/bytes/deadline三重有界查询；control attribution锁外exact-ID读取Start/End；
- model semantic stream使用handle-owned confirmed cursor；正常chunk只校验上一confirmed semantic identity，text/thinking/tool deltas按
  event/char/time窗口批量commit。只有reopen、UNKNOWN或recovery才执行一次bounded per-call reconstruction；successful writer直接返回canonical
  envelopes，不再逐chunk回读Start/End/history/刚写event；
- RuntimeSession中央lifecycle validator禁止whole-ledger `iter()`：RunStart/rollout/window facts来自incremental store，compaction与settlement按exact
  event ID读取，resume exposure按run/type sparse bounded snapshot读取；
- PRE_RUN一次批量读取全部目标reply，使用同一frozen high-water、aggregate events/bytes cap与PostgreSQL server-side cursor；禁止per-reply N+1；
- session-owned immutable authority-slice LRU按`(runtime owner, run/basis, minimum sequence)`缓存canonical envelopes；后续compile只读
  `(cached_high_water, new_high_water]`，basis变化自然换key。Child的parent authority只保留sparse RunStart/spawn/rollout exact ranges与独立
  observed-high-water cursor，不得再从parent RunStart连续读取到latest relevant event；
- generation>1 window的context authority使用post-compaction primary delta + exact/sparse named ranges；不得把primary range重新扩回RunStart，
  `ContextEventSlice.subslice()`也不得在每次compile展平完整历史prefix。Window source document中的normalized retained baseline是durable semantic
  carrier，authority cache仅memoize canonical envelopes；
- 本阶段冻结的production复杂度上界是`O(active-window events + bounded named facts)`，不是`O(session events)`。V1允许pure transcript projector在
  每次compile重新归一化当前active-window delta；这不等于已实现`O(events since previous compile)`的incremental transcript reducer。后续若继续
  优化，只能增加由canonical reducer input纯派生、可随时丢弃并重建的memoized projector state，不能把process-local cache提升为authority；
- 全部production async event commit共用session-owned FIFO writer queue；event loop不得直接等待PostgreSQL事务或跨线程`RLock`。RuntimeSession继续
  拥有serialization、commit/reducer/publication顺序、physical-operation tracking与close drain；blocking worker pool可由Host/process共享；
- 每个event write attempt只分配一个absolute deadline，且该deadline覆盖queue wait、physical commit与stable confirmation。queued operation到期时
  waiter通过CAS移除并立即得到typed NONE；越过physical-start后由critical owner在原deadline内完成FULL/NONE/UNKNOWN裁决。同步
  `confirm_event_batch()`只允许critical worker调用；async continuation必须显式传入原attempt deadline并重新进入同一FIFO。Host、resume、RunEnd、
  compaction、MCP与checkpoint外层只消费writer返回的typed outcome，不得同步回查ledger或重新获得30秒confirmation窗口；
- PostgreSQL event writer使用Host/process-owned bounded connection pool或writer-owned reconnectable connection。已确认的session/run/turn parent identity
  可在事务成功后缓存；semantic micro-batch不得每25ms重新建立物理连接并重复逐event parent upsert；
- prepared rollup cache identity只包含durable rollup/member/placement basis/policy/estimator/carrier contract，不包含完整transcript fingerprint；不相关的
  transcript append不得自动失效稳定rollup materialization；
- window compaction attempt/failure/pending recovery来自增量store，source validation使用atomic high-water + exact-ID snapshot；
- LongHorizonStateStore对无关semantic/UI event只校验contiguous sequence并推进high-water，不clone maps/LRU/projection reducer；
- checkpoint restore先读取bounded catalog metadata并确认artifact，只为newest viable candidate加载一次delta；active rollup artifact在rewrite时
  write-confirm一次，后续compile使用bounded verified-content cache，session reopen首轮只read-confirm；
- privileged offline doctor是唯一允许无界full fold的入口，且不在compiler/live replay依赖图中；
- checkpoint physical GC只在closed/quiescent session与exclusive PostgreSQL maintenance lock下运行；
- aggregate tool observation不再受独立36K字符真源控制；
- projection/rollup/event pages/artifact refs/diagnostics都有cap；
- completed owners、retiring handles、model stream execution/subscription/physical-operation owners、pending writers可释放；
- long session内内存不会因每run保留完整LoopState而O(n²)增长；
- close不会越过blocking I/O/sync tool/canonical fact owner。

### 30.3 Audit/replay

- Context Input Manifest可exact join window/projection/rollout/status-hint/subagent graph semantic source；
- 每条stored event在current union decode前具有required type/schema-version/schema-fingerprint/domain identity，historical decoder可按row identity
  精确rebind；
- checkpoint acceleration单独审计且不污染selection/snapshot/input semantic identity；
- graph semantic payload/state排除storage sequence与checkpoint event；checkpoint schedule不改变semantic source；
- 原checkpoint artifact缺失或delta超bound时，任意不晚于目标high-water的bounded contract-compatible rebase可保持exact replay；
- EventLog + matching versioned reducer可通过offline doctor重建任意checkpoint；
- Inspector五态准确；
- live/replay生成同一provider-visible payload fingerprint；
- stable artifacts/events同ID幂等、冲突fail closed；
- reported usage、reserved-missing usage、cache status不混淆。
- missing usage按`pre_margin_input_tokens + effective_output_tokens`的物理上界reservation quote全额结算，不使用estimator或stream字符启发式；
- provider reported input高于estimator但未超过resolved physical bound不会被误报为reconciliation。

### 30.4 Product behavior

- “今天LOL比赛结果”类任务不会因46次重复search而无限推进；
- rollout接近预算边界时按累计资源进入warning/restricted/finalization，不依赖判断“是否有新证据”；
- context尚未到256K也可因rollout phase进入finalization；
- 中性hint可报告已执行轮次、剩余allowance与exact recurrence，但不命令模型继续、停止或选择下一步；
- context真正接近单call上限时先deterministic projection，再window compaction；
- 长程coding/research任务可跨多个window继续，不删除durable history；
- 用户要求写artifact时，finalization阶段仍允许完成写入和bounded verification。

### 30.5 Engineering gates

- 全量pytest、Ruff、diff check全绿；
- Postgres integration与fault injection全绿；
- real LLM非dogfood、全量dogfood按门禁运行并保存trajectory；
- architecture grep/AST gates全绿；
- contracts与代码同版本；
- 旧facade/旧字段/兼容reader生产引用为零；
- `pulsara config-check`枚举全部production primary/summarizer组合并解释不可行组合；
- 本文L0A–L5的每项acceptance均有测试或Inspector证据。

---

## 31. 最终冻结结论

阶段四不是“把36K改大”，也不是“到了200K就总结一次”。最终hard-cut语义是：

```text
Durable ledger + versioned reducer remain the only graph authority
        |
        +--> semantic graph source (graph-event accumulator + reducer contract + state fingerprint)
        |       |
        |       +--> operational ledger continuity + discardable checkpoint memoization + bounded contiguous delta
        |               +--> preferred checkpoint or contract-compatible rebase
        |               +--> privileged offline full-fold repair only
        |
        +--> active ContextWindow
                |
                +--> monotonic ToolObservationProjection generations
                |       +--> artifact-aware thinning
                |       +--> pairing-safe rollups
                |
                +--> LLM compaction closes old window and opens new window
        |
        +--> cumulative RolloutBudgetAccount
                +--> warning
                +--> restricted
                +--> finalization reserve
                +--> neutral rollout status hint
                        +--> settled call counts
                        +--> remaining allowance
                        +--> bounded exact recurrence facts
```

单次模型输入、模型可见projection与累计run资源分别拥有自己的事实、预算、owner和恢复语义；它们只在model-step safe point
确定性汇合。Checkpoint只加速版本化reducer，不获得semantic authority；status hint是canonical facts的派生视图，不是新的控制面。
完成该hard cut后，Pulsara宣传的“长程任务”才不再依赖偶然没撞到`max_turns`、36K observation cap或provider context limit。
