# Pulsara 当前架构债务审计

> 审计日期：2026-07-10
> 审计基线：`main@995b5746`
> 审计方法：以当前代码、生产 wiring、事件投影和一组窄动态探针为准；
> `archived_docs/ARCHITECTURE_DEBT_AUDIT.zh.md` 只用于学习文档结构，不继承其中的历史结论。

这不是功能 roadmap，也不是“文件太长所以要拆”的机械式重构清单。

这份文档只记录以下几类问题：

- 同一个运行时事实有两个或更多真源；
- 模块边界要求调用者了解不属于自己的内部语义；
- 已完成 hard cut 后，旧的输入、测试或 transport 路径仍留在生产包；
- durable event 已是事实源，但恢复、投影或缓存仍通过另一套 reducer 推导；
- 生产请求路径承担 schema migration、同步观察者 I/O 等部署职责；
- 当前代码已经用注释、local import、lazy facade 或 compatibility DTO 明确暴露出过渡状态。

## 0. 结论先行

当前 Pulsara 的总体架构已经从“单 loop + 若干工具”演进为一个有明确 durable truth 的 agent runtime：

- `RuntimeSession` 和 typed `AgentEvent` 承担事实记录；
- `ContextCompiler` 决定模型实际看到什么；
- capability runtime / permission gate 决定工具可见性与可调用性；
- parent/child runtime session 与 subagent graph 分离；
- PostgreSQL 是 canonical durable store，Oxigraph / vector / search 是投影；
- memory candidate 先进入 pool，再由 governance 决定是否写入 canonical memory。

这些主线是对的。当前最危险的债务不在这些架构选择本身，而在迁移尚未彻底收口的接缝处。

本轮最值得优先处理的五项是：

| 优先级 | 债务 | 风险 |
|---|---|---|
| P1 | compiler/transport 没有共享的 `ResolvedModelCall`，compiler 仍使用 `256_000 / 8_000` 常量 | options/limits 可能漂移；小窗口模型可能 provider overflow |
| P1 | subagent live cache、bootstrap reducer、inspect projector 三套状态归约不一致 | durable resume 后 task 状态可与 event projection 不同 |
| P1 | event append/publish 边界分裂，governance events 又与 memory mutation 跨事务 | sequence gap、live 可见性和 canonical fact 部分提交 |
| P1 | runtime / tools / event / message 的依赖环由 lazy import 隐藏 | 新功能继续跨层引用，重构成本呈非线性增长 |
| P1 | schema DDL / ALTER / extension bootstrap 仍在请求和 governance 热路径执行 | 部署权限、锁、延迟和 schema hard cut 无法受控 |

其次是：

- `AgentRuntime` / `HostSession` 的 orchestration 职责和 stringly scratchpad；
- ContextCompiler 的 compatibility inputs 与未落地的 `ContextSource`；
- legacy MCP spike、in-memory product branch、旧 `LLMMessage.TOOL_CALL`；
- `CustomEvent` 中已经稳定的事实，以及尚未区分 internal emitter / external ingress / reserved / orphan 的 typed events；
- tool-result renderer 对 terminal / MCP 领域语义的直接依赖；
- capability descriptor、tool binding、subagent profile 和 skill root 的重复声明；
- compaction core 对 memory candidate pool / ontology 的直接依赖；
- async event 热路径上的同步 PostgreSQL append、ordered publisher 上的同步 hook 与非 durable failure sink。

## 1. 先明确：哪些复杂度不是债务

重构前应冻结以下边界，避免用“简化”破坏已经正确的运行时语义。

### 1.1 terminal 的 preview、artifact、terminal_process 三层不是重复实现

它们分别解决：

- 当前工具调用的有界模型可见结果；
- 大输出的 durable 完整证据；
- 长进程的后续 log / poll / wait / input / kill 操作。

应减少的是 renderer 对 terminal payload 的猜测，不是合并这三层。

### 1.2 parent 与 child 使用不同 runtime-session event stream 是正确边界

parent stream 保存 graph / edge / result delivery；child stream 保存自己的完整 agent loop。

不要为了“统一查询”把 child 的 `RunStartEvent`、`ReplyEndEvent`、tool events 写进 parent transcript stream。正确方向是 cross-session projector / locator。

### 1.3 PostgreSQL canonical truth + 多种索引投影不是双写债务

Oxigraph、vector index、search index 都是 canonical memory 的查询投影。真正的债务是：

- schema migration 在热路径执行；
- 投影写入和失败恢复的边界不统一；
- hook failure 只留在内存。

### 1.4 Chat Completions 与 Responses adapter 并存不是债务

它们是两个仍需支持的 provider wire protocol。可以共享 canonical input / event builder，但不应强行合并协议差异。

### 1.5 capability descriptor 与 execution binding 分离是正确设计

descriptor 是模型可见/审计契约，binding 是执行对象。债务是 schema、read-only、profile 等事实又在 tool class、registry、descriptor provider 中重复声明。

### 1.6 memory candidate governance 不是多余中间层

candidate 不是 canonical memory；governance 决定 accept / merge / correct / skip 是必要的。需要调整的是 compaction producer 与 compaction core 的依赖方向，而不是绕开 governance。

## 2. ContextCompiler 的模型预算真源与半迁移债务

### 2.1 旧设计来源

ContextCompiler 最初在 AgentRuntime 已经渲染好 transcript、memory、capability、recovery prose 后，负责把这些兼容输入组织成 sections、做 lifecycle cache、预算降级并 lower 成 `LLMContext`。

因此当前代码同时存在两套抽象：

- 新抽象：`ContextCompileRequest`、`ContextSource`、`ContextSection`、`CompiledContext`；
- 旧接缝：`ContextCompileInputs` 接收已经渲染的 `LLMMessage` 和字符串 component prompts。

代码自己已经把后者标为：

> `Rendered compatibility inputs that still come from existing runtime code.`

见 `src/pulsara_agent/runtime/context_engine/compiler.py` 的 `ContextCompileInputs`。

### 2.2 当前兼容层

#### 单次模型调用没有可复用的 resolved contract

`ModelProfile` 已经定义：

- `context_window`
- `max_output_tokens`

但这两个字段仍是 optional，`LLMConfig.model_for()` 构造 profile 时也没有填充它们。
同时，单次调用的 `LLMOptions.max_output_tokens` 可以覆盖 model default。

当前 `ContextCompileRequest` 只携带 `model_role`，`LLMRuntime.stream()` 又根据 role
独立调用一次 `config.model_for(role)`。compiler 和 transport 因而没有消费同一个“本次
调用已经解析完毕的模型事实”。编译器只能直接返回：

```python
def _context_window_tokens(request):
    return 256_000

def _reserved_output_tokens(request):
    return 8_000
```

`AgentRuntime` 构造 pressure / failed `ContextCompiledEvent` 时也重复写入相同常量。
compaction 又有独立的
`ContextCompactionPolicy(context_window_tokens=256_000, summary_max_output_tokens=8_192)`。

因此仅新增一个 `ModelContextLimits` DTO 仍不够：如果 compiler 和
`LLMRuntime.stream()` 分别按 role 解析，它们仍可能在 options override、provider profile
或配置变化时得到不同结果。

#### compaction 混合了目标模型与 summarizer 的两组限制

compaction 实际涉及两个不同的 model call contract：

- `target_model_limits`：主模型下一轮的输入预算，决定何时需要 compact、compact 到什么程度；
- `summarizer_model_limits`：flash summarizer 自己可接收的 compaction input 和可生成的 summary output。

当前 policy 用一组手写 window/threshold 和一个 summary output 常量同时表达两者。未来不能
简单写成“compaction 从主模型 limits 派生全部限制”，否则可能让 flash summarizer 接收超过
自身窗口的输入。

#### tool result 在 compiler 之前完成主要分配

`runtime/context.py` 先调用 `render_segmented_llm_messages()`，完成：

- transcript segmentation；
- tool-result allocator；
- render cache；
- terminal/artifact envelope；
- budget report。

之后 compiler 接到的是已渲染 messages 和 decisions。也就是说，compiler 名义上拥有最终 model-visible context，实际却无法重新规划最昂贵、最复杂的一类 section。

#### `ContextSource` 目前只有 protocol，没有 production implementation

`ContextSource.collect()` 的“不创造新 truth”边界是合理的，但当前 DTO 还不足以承载计划中的
hard cut：

- `ContextSourceOutput` 只能返回普通 `ContextSection`；
- provider-native assistant tool calls、tool results、pairing、raw render units 不能安全压成 text section；
- `ContextCompileRequest` 直接暴露整个 mutable `LoopState`，source 仍可任意读取 scratchpad；
- production path 仍由 `runtime/context.py` 手工拼 `component_prompts`。

如果现在直接实现 source registry，只会把“预渲染字符串输入”重新包装成另一层 facade。

#### legacy transcript fallback 仍存在

current-user anchor 缺失时，compiler 仍能退回 legacy history；`LoopBudget` 仍保留 legacy tool-result budget。这个 fallback 会把结构错误变成“尽量编译”，与现在 pairing-safe、run-bound context 的 hard-cut 方向冲突。

#### render cache 仍可退回 `LoopState.scratchpad`

AgentRuntime 已有 session-owned render decision cache，但 `build_compiled_context()` 在未显式传入时仍把 LRU cache 塞入 `state.scratchpad`。同一契约存在两个 ownership。

### 2.3 当前成本

1. 小于 256k 的真实模型会被 compiler 误认为可容纳，最终在 provider 才失败。
2. 大于 256k 的模型会过早降级或触发 compaction。
3. `context_budget_still_exceeded_after_degradation` 目前只是 warning；只要 current user 本身没超限，仍可能发起总量超预算的 model call。
4. tool-result budget、section budget、compaction threshold 使用不同配置和估算口径。
5. 想新增 source 时，开发者需要同时理解 AgentRuntime、runtime/context facade、compiler component id 前缀与 lowering。
6. compiler 的 lifecycle cache 复用 section，但 source collection 仍散落在外部字符串拼接路径。
7. compiler 与 transport 分别解析模型/option 时，没有结构保证二者使用同一个 effective call。
8. flash summarizer 的实际输入窗口没有独立的 fail-closed contract。

### 2.4 能不能 hard cut

能，而且应按“调用解析 → immutable facts → normalized transcript → sources”的顺序推进。

第一步必须先统一模型预算真源；否则后续 source/refactor 无法建立可靠验收。

建议冻结不可变的单次调用事实：

```python
@dataclass(frozen=True, slots=True)
class ResolvedModelCall:
    model_profile: ModelProfile
    effective_options: LLMOptions
    context_limits: ModelContextLimits
    token_estimator: TokenEstimator
    fact: ResolvedModelCallFact
```

`LLMRuntime.resolve_call(role, options)` 只能解析一次；compiler 和 transport 必须消费同一个
`ResolvedModelCall`，不能分别按 role 再解析。

runtime object 之外还需要一个 event-safe、不可变的 `ResolvedModelCallFact`：

```python
@dataclass(frozen=True, slots=True)
class ResolvedModelCallFact:
    resolved_model_call_id: str
    model_id: str
    model_role: str
    provider: str
    api: str
    effective_max_output_tokens: int
    context_window_tokens: int
    input_budget_tokens: int
    safety_margin_tokens: int
    token_estimator_id: str
    token_estimator_version: str
    options_fingerprint: str
    limits_fingerprint: str
```

`ContextCompiledEvent` 与 `ModelCallStartEvent` 必须同时携带
`resolved_model_call_id` 和相同 fact snapshot/fingerprint。`context_id` 仍表示 compiled payload，
不能兼任 model-call resolution identity：pressure attempt 可能只有 compiled event，没有真正的
model start；同一个 model call resolution 也可能经历 context retry。

transport 内部网络/限流 retry 必须复用同一个 `ResolvedModelCall`。如果未来 provider/model
fallback 改变 model、window、options 或 estimator，必须创建新的 resolved-call id，重新 compile
并写新的 `ContextCompiledEvent`；不能只替换 transport model 后继续发送旧 context。

第二步不能直接上 source registry，而应先建立：

```text
ContextFactSnapshot       immutable、无 scratchpad、只含本次 compile 可读事实
TranscriptCompileInput    provider-native turn/pairing/order facts
ToolResultRenderUnit      tool-result allocator 的规范化输入
ContextSectionCandidate   非 transcript source 的 section candidate
```

其中 transcript/tool-result 是结构化 compile input，不应伪装成普通 prose section。

### 2.5 推荐 cut 顺序

1. 给 pro/flash model 配置 required 的 context window 与 default max output；未知模型缺配置时 fail closed，不回退 256k。
2. 新增 `ResolvedModelCallFact` 和 `LLMRuntime.resolve_call(role, options) -> ResolvedModelCall`。
3. `ContextCompiledEvent` / `ModelCallStartEvent` required 地保存同一 resolved-call identity/fact。
4. `ContextCompileRequest` 与 transport required 地消费同一个 resolved call；删除 compiler 和 AgentRuntime 的 256k/8k 常量。
5. compaction 明确接收 `target_model_limits`；summarizer 调用另行解析
   `summarizer_model_limits`，并分别校验 compact threshold 与 summarizer input/output。
6. 总 `estimated_tokens > input_budget_tokens` 时进入 pressure/compact/retry；retry 后仍超限则 fail closed，不只写 warning。
7. 定义 immutable `ContextFactSnapshot`，从 request 删除 mutable `LoopState`。
8. 定义 `TranscriptCompileInput` 和 `ToolResultRenderUnit`，保留 provider-native pairing/order，不预渲染成普通 section。
9. 定义 `ContextSectionCandidate` 并实现 source registry：system、memory、capability、recovery、subagent results、runtime timing。
10. compiler 统一对 transcript/tool units 与 section candidates 做 priority allocation 和 lowering。
11. 删除 `ContextCompileInputs`、legacy current-user fallback、legacy budget 字段、scratchpad render-cache fallback。
12. 删除仅供旧测试使用的 `build_llm_context()` / `msg_to_llm_messages()`，测试改断言 `CompiledContext`。

### 2.6 验收条件

- 任意 production model call 的 `ContextCompiledEvent` 与 `ModelCallStartEvent` 可通过
  `resolved_model_call_id` join 到相同 fact/fingerprint。
- compiler budget、transport payload 与 effective `max_output_tokens` 来自同一 resolved object。
- transport retry 不改变 resolved-call identity；model/limits fallback 必须重新 compile。
- 小窗口 fake model 在 provider 前就 pressure/fail closed。
- compiler 输出不可能带 `context_budget_still_exceeded_after_degradation` 后继续 model call。
- target model threshold 与 summarizer input/output limits 分别可审计，且不会互相冒充。
- production source 无法读取 mutable `LoopState.scratchpad`。
- transcript tool-call/tool-result pairing 在 source collection 前后保持 typed structure。
- production path 不再构造 `ContextCompileInputs`。

## 3. AgentRuntime / HostSession orchestration 与 working-state 债务

### 3.1 旧设计来源

Pulsara 的功能以纵向方式持续加入主 loop：plan mode、approval、MCP elicitation/input-required、compaction、memory、capability gate、subagent、run-bound permission、timing。

每个功能单独看都合理，但主要 orchestration 仍集中在：

- `src/pulsara_agent/runtime/agent.py`（约 3200 行）；
- `src/pulsara_agent/host/session.py`（约 1100 行）。

### 3.2 当前兼容层

`AgentRuntime` 同时负责：

- run permission snapshot；
- capability exposure 与 gate decision fact；
- context compile、pressure、compact/retry；
- provider loop；
- tool dispatch 与 persistence failure；
- approval / plan / MCP pending 的 suspend/resume；
- memory hooks / recall / projection；
- subagent child runner；
- workflow control tools。

`HostSession` 同时负责：

- MCP safe-point sync；
- run / stream / stop；
- pending approval、plan question、exit plan、MCP interaction 的 host API；
- compaction event post-publish；
- lifecycle close/drain。

`LoopState.scratchpad` 仍承载大量 production control facts，例如：

- `capability_exposure`
- `plan_state` / `plan_active` / revision audit
- `current_context_id` / `current_model_call_index`
- `mid_turn_compaction`
- `tool_result_event_spans`
- `tool_result_render_decision_cache`
- memory recall/projection caches

这些 key 没有统一 schema、owner、lifetime 或 invalidation contract。

### 3.3 当前成本

1. 新 pending 类型需要修改 AgentRuntime、HostSession、plan DTO、resume、inspector 多处 switch。
2. safe point 的顺序是隐式的：先 tool result persistence，还是 explicit subagent result terminalization，还是 compaction，依赖大函数中的位置。
3. scratchpad typo 或 stale value 只能在运行时表现为语义漂移。
4. domain 单元测试往往必须构造完整 AgentRuntime wiring。
5. `StopReason` 等 literal 已出现跨模块重复定义，说明 orchestration contract 没有稳定的低层归属。

### 3.4 能不能 hard cut

能，但不要把 `AgentRuntime` 改成一个“service locator”。它仍应是 loop conductor，只把可独立测试的状态机和决策拆出去。

建议目标形态：

```text
AgentRuntime
  ├─ ModelStepCoordinator
  ├─ ContextCompileCoordinator
  ├─ ToolBatchCoordinator
  ├─ InteractionResumeRouter
  ├─ WorkflowController
  └─ ChildRunAdapter
```

这些对象返回 typed decisions / events，不直接各自修改整份 `LoopState`。

### 3.5 推荐 cut 顺序

1. 新增 typed `RunWorkingSet`，把 production scratchpad key 逐个迁移为字段或 owned cache。
2. 冻结 safe-point pipeline，并给每个 phase 命名和事件归因。
3. 先抽 `InteractionResumeRouter`，统一 approval / plan / MCP resume 的输入与 terminal outcome。
4. 再抽 `ContextCompileCoordinator`，包含 pressure/compaction retry，但不包含 compiler 本身。
5. 再抽 `ToolBatchCoordinator`，统一 persistence、suspend、workflow tool、explicit child result safe point。
6. AgentRuntime 保留循环推进和各 coordinator 的顺序。
7. HostSession 只做用户交互入口、session-owned resource lifecycle 与 pending routing facade。

### 3.6 不应做的事

- 不把 durable truth 搬回 `RunWorkingSet`；它只是事件投影和短期工作状态。
- 不让 coordinator 直接写任意 `CustomEvent`。
- 不用“拆文件”代替状态机边界设计。

## 4. Subagent graph 的多 reducer / 多真源债务

完整 hard-cut event/fact/reducer/hydration/command/PR 规格见
[`PULSARA_SUBAGENT_GRAPH_REDUCER_HARD_CUT_IMPLEMENTATION.zh.md`](PULSARA_SUBAGENT_GRAPH_REDUCER_HARD_CUT_IMPLEMENTATION.zh.md)。

### 4.1 旧设计来源

subagent runtime 先实现了 live `_runs / _tasks / _results` cache，随后增加：

- task DAG；
- batch materialization；
- blocked dependency propagation；
- durable bootstrap；
- inspector graph projection；
- list/wait/consume/deliver 状态。

当前 event log 是正确的 durable truth，但至少有三套状态归约：

1. command path 手工更新 `SubagentRuntime._tasks/_runs/...`；
2. `_bootstrap_from_parent_event_log()` 重建 runtime cache；
3. `project_subagent_graph()` 重建 inspect/list projection。

### 4.2 当前兼容层

`project_subagent_graph()` 处理：

- `SubagentTaskScheduledEvent`
- `SubagentTaskBlockedEvent`
- blocked reason、dependency snapshot、terminal event ids、generation

但 `_bootstrap_from_parent_event_log()` 没有处理 scheduled / blocked event，只处理 created、started、completed、failed、cancelled。

### 4.3 已验证的实际漂移

本轮使用现有 test support 做了一个窄动态探针：

1. 创建 task A，状态 `running`；
2. 创建 task B，依赖 A，状态 `waiting_dependency`；
3. 用同一 parent event log 重新构造 `SubagentRuntime`；
4. 同时运行 event projection。

得到：

```python
{
  "live": {"a": "running", "b": "waiting_dependency"},
  "bootstrapped_cache": {"a": "running", "b": "created"},
  "event_projection": {"a": "running", "b": "waiting_dependency"},
}
```

同一代码基线下，`uv run pytest tests/test_subagent_runtime.py -q` 的结果是
`44 passed, 2 warnings`。也就是说，现有测试全部通过，但没有覆盖“包含
`waiting_dependency` / `blocked_dependency_failed` 的 parent event log 重新构造
SubagentRuntime”这一条恢复路径。

这不是理论上的“可能重复”，而是当前 durable resume 的具体状态漂移，也是一个已经
被 green baseline 隐藏的测试矩阵缺口。

### 4.4 当前成本

1. 重启后 `wait_agent_tasks(settle=all)` 可能把 waiting task 当 created。
2. scheduler 的 dependency predicates 读取 `_tasks`，因此 resume 后可能错误启动、永远等待或漏 block。
3. async cancel、sync safety-narrowing cancel、batch repair 分别维护 task terminalization/cascade。
4. 每新增 task event，必须同时记得更新 live path、bootstrap、projector；遗漏不会由类型系统发现。
5. 现有 `SubagentTask.objective` / `SubagentRun.task` 是 archive hydration 后的完整正文，不是 parent event 单独可归约事实。
6. `SubagentRunStartedEvent` 没有 immutable budget snapshot；bootstrap 使用当前
   `self.default_budget`，配置变化后 timeout/result clipping 会漂移。
7. `child_run_id` 可由无 event 的 `set_child_run_id()` 直接修改 live cache，running graph 无 durable parent fact 可恢复该绑定。

### 4.5 能不能 hard cut

应立即 hard cut。subagent 还是新系统，现在修正最便宜。

先建立只包含 parent graph event 可归约事实的状态，不直接复用现有 hydrated DTO：

```python
SubagentGraphState(
    tasks: Mapping[str, SubagentTaskFact],
    runs: Mapping[str, SubagentRunFact],
    results: Mapping[str, SubagentResultFact],
    edges: Mapping[str, SubagentEdgeFact],
)

SubagentGraphState apply(SubagentGraphState, AgentEvent)
```

`SubagentTaskFact` / `SubagentRunFact` 只包含：

- parent graph event 中真实存在的 ids、status、preview、artifact refs；
- immutable context/capability/budget snapshots；
- dependency、consumption、delivery、result attribution；
- event id/sequence/timestamps 等 provenance。

它们不包含 archive 读取后的完整 objective/task text，也不把当前配置/default 当作 event fact。

另建只读、可执行 I/O 的 async `SubagentGraphHydrator`：

```text
SubagentGraphFact
    ├─ parent archive -> objective/task/result 正文
    └─ child EventLog -> child native run id / child timeline facts
        ↓
HydratedSubagentTaskView / HydratedSubagentRunView
```

hydrator 可以失败并返回 bounded diagnostic，但不得修改 graph state。所有正文仍受 preview/
artifact read cap 约束，不能把完整 child transcript塞进 list/inspect。三路 equality 比较的是
normalized graph facts；hydrated runtime/list view 另做 archive/child-log 测试。

所有 parent-event-driven graph facts 都由 reducer 归约：

- bootstrap：fold parent event log；
- committed-reducer seam：event commit 后、observer publish 前 apply stored events；
- list / inspect：直接使用同一 reducer 或它的 projection adapter；
- scheduler：只读取 reducer state。

`SubagentRunStartedEvent` 必须 hard cut 增加 required `budget_snapshot`，完整保存本次 run 的
`SubagentBudget`。不能在 bootstrap 时使用新的 `default_budget` 补事实。

`child_run_id` V1 由 hydrator从 child session 的 `RunStartEvent` 读取。删除无 event 的
`set_child_run_id()`；如果 parent graph 将来需要在 child terminal 之前把它作为 canonical edge
使用，应新增 `SubagentChildRunBoundEvent`，而不是恢复 direct cache mutation。

这里的“所有状态”严格限定为 **durable graph facts**。以下对象不可由 event reducer 重建，
必须放入独立的 ephemeral `ChildExecutionRegistry`：

- `asyncio.Task` child coroutine handle；
- 当前进程内的 child `RuntimeSession` instance；
- cancellation / drain handle；
- capacity slot reservation；
- 尚未提交的 batch start reservation。

reducer 回答“durable graph 记录了什么”；execution registry 回答“当前进程实际拥有什么”。
二者只在 spawn、resume、cancel、shutdown safe point 对账。若 reducer 显示 running，但 registry
没有可恢复 handle，继续使用现有 fail-closed repair 语义，不能伪造一个 reducer state 来代表
live coroutine。

### 4.6 推荐 cut 顺序

这项 hard cut 分为两个可独立验收的阶段。

#### 阶段 A：纯 fact/reducer/bootstrap/projector，可立即开始

1. 定义 `SubagentTaskFact` / `SubagentRunFact` / `SubagentGraphState`。
2. `SubagentRunStartedEvent` 增加 required immutable `budget_snapshot`，修改所有 writer/fixture。
3. 把 projector 的完整 event switch 移入 reducer。
4. bootstrap 改成 fold reducer，删除 `_bootstrap_*` 手写 fact 更新函数。
5. 新增 `SubagentGraphHydrator`，负责 archive 正文和 child-log native facts。
6. inspector/list 由 graph facts + hydrator 输出，不直接读取 runtime mutable DTO。

#### 阶段 B：command-path hard cut，依赖最小 committed-reducer seam

7. 按第 5 节建立 `commit -> apply committed reducers -> publish observers` seam。
8. command methods 不再直接 `self._tasks[...] = replace(...)`；conditional batch commit 后先按 reducer high-water catch up，再一次 apply 当前全部 stored events。
9. sync/async cancel 都调用同一个 command builder，生成相同 terminal/cascade events。
10. 删除 `set_child_run_id()`；V1 通过 child log hydration，未来如有需要新增 typed binding event。
11. `_runs/_tasks` 若保留，只能是 reducer state 的 cache view，不是第二真源。
12. 新建 `ChildExecutionRegistry`，从 graph state 中剥离 `_child_tasks/_child_sessions` 和 slot reservations。
13. 在 recovery/shutdown safe point 增加 graph-vs-execution reconciliation，不让 reducer接管进程资源生命周期。

完整 async event writer 可以在阶段 B 之后继续替换底层 I/O；Subagent command hard cut 不需要等
整个 writer/hook/outbox 重构完成，只依赖最小 committed-reducer seam。

### 4.7 验收条件

- 对任意 event prefix，committed reducer state、fresh bootstrap、inspect normalized graph facts 三者一致。
- 增加 property test：随机合法 task DAG event stream 做三路 equality。
- 明确覆盖 `created -> waiting_dependency -> blocked_dependency_failed` 的 restart。
- blocked 的 dependency terminal event ids / generation 在 restart 后不丢失。
- budget config 修改后重启，既有 run fact 仍使用 event 中原始 `budget_snapshot`。
- graph fact equality 不读取 archive；hydrator 单独覆盖 artifact missing/corrupt/fallback diagnostic。
- child raw `RunStartEvent` 可 hydrate `child_run_id`，但不会反向改变 parent graph fact。
- durable running child 缺 live execution handle 时稳定进入 dangling-child fail-closed repair。
- execution registry 中的 handle 永远不进入 event payload、graph projection 或 equality assertion。

## 5. Event append / publish 与事件 vocabulary 债务

### 5.1 旧设计来源

`RuntimeSession.emit()` / `emit_many()` 已经集中执行 canonical append 与 publish：

1. 注入 default metadata；
2. append / extend event log；
3. publish stored event 给 runtime publisher / hooks。

但它还没有把 committed reducer 与 observer publication 分开，因此尚不是完整、正确的 command
commit boundary。

但较早或跨线程的 subsystem 仍直接持有 `EventLog`，因此“持久化”和“发布”成为两个需要调用者手工配对的阶段。

### 5.2 ordered publisher 不允许活跃 session 出现 durable-only sequence gap

当前 `RuntimeEventPublisher` 只会发布 `_next_sequence_to_publish`，后续 sequence 先进入
`_pending_by_sequence` 等待。如果活跃 session durable-only 写入 sequence 5，随后 live writer
提交并发布 sequence 6，而 5 从未 publish/discard，6 会一直等待。

因此原先设想的通用 `append_offline(events)` 不能存在于 live writer 上。repair/import 必须是
不同的、只允许 closed/quiescent session 使用的 authority，并在 session 重新开放前重建投影、
重新 seed publisher high-water mark。

此外，`RuntimeSession.emit()` 虽然是 async API，却直接调用同步 `event_log.append()`。
`PostgresEventLog.extend()` 每次再创建一个新的同步 psycopg connection。这个同步 I/O 位于所有
event 和 hook 之前，比第 11 节的 observer I/O 更靠近主热路径。

### 5.3 commit、deterministic reducer 与 observer failure 被混成一个结果

当前顺序是：

```text
append/commit event
    -> await ordered publisher/subscribers
    -> return stored event
```

publisher subscriber 失败时，publisher 仍会推进 sequence，但把异常设置给 `emit()` 正在等待的
future。因此 caller 看到 exception 时，event 实际已经 durable commit。Subagent 若采用简单的
“`stored = await emit(); reducer.apply(stored)`”，就不会 apply 已提交事实。

`emit_many()` 更危险：PostgreSQL EventLog 已原子提交整个 batch，但第一个 stored event 的 subscriber
失败会中断 Python for-loop，后续 committed events 连 publish enqueue 都不会进入，caller 也拿不到
batch result。`InMemoryEventLog.extend()` 当前只是逐个调用`append()`，还没有整批原子性，并发batch可交错。

应先建立最小 committed-reducer seam：

```text
conditional atomic append / transaction commit
    -> each reducer catch up its own missing pre-current interval
    -> apply current committed batch to caught-up reducers（无 I/O）
    -> independently catch up ordered publisher
    -> enqueue/publish current live observer events
    -> return EventWriteResult
```

```python
@dataclass(frozen=True, slots=True)
class EventWriteResult:
    committed_events: tuple[AgentEvent, ...]
    commit_status: Literal["committed"]
    reducer_high_waters: Mapping[str, int]
    publisher_enqueued_through_sequence: int | None
    publication_errors: tuple[EventPublicationError, ...]
```

live write API必须接受`expected_last_sequence`，并在EventLog session lock/transaction内compare-and-append；
mismatch在任何insert前抛`EventWriteConflict`，caller用最新graph重新plan。async与thread写共用同一个
thread-safe session write coordinator，不能只用`asyncio.Lock`。

EventLog contract同时hard cut：`extend()`为连续sequence、不可与其他batch交错、任一失败零partial
write的原子batch。PostgreSQL复用session advisory transaction lock并作为production权威；InMemory仅作为
pytest fake，在单个`threading.Lock`中预验证、预构造并一次性extend，复现最小observable contract，不能逐条append。

observer failure 不能伪装成 commit failure。deterministic reducer 如果对已提交 event 抛错，
session 应标记 `reconciliation_required` 并可从 log 重建，而不是回滚一个已经无法回滚的 commit。

### 5.4 publication 的恢复与投递保证

在此冻结投递语义：

- **同进程 sequence gap**：current batch commit后，live writer先按每个reducer自己的high-water从
  EventLog读取并apply缺失的pre-current连续区间，再apply current batch；publisher按自己的独立
  high-water catch up。共享range read只是优化，不能颠倒顺序或混用high-water。不能无限等待，也不能无证据 `discard`。reducer 以
  event id/sequence high-water 幂等，catch-up 不得重复改变 state。
- **进程重启**：durable reducers/projections 从 EventLog fold/rebuild；不泛化重放所有历史 live observers。
- **需要可靠副作用的 subscriber**：使用独立 durable outbox 或 per-subscriber offset，提供
  at-least-once delivery，并按 event id 幂等。
- **UI/CLI/live stream observer**：best-effort、当前进程有序；断线后通过 inspect/query 当前事实，
  不承诺补发每条历史 notification。
- **exactly-once**：不承诺。canonical commit 是 once；observer delivery 可能 retry/duplicate。

`RuntimeSession` 当前以 `event_log.next_sequence()` 初始化 publisher，明确证明它不会在重启时
自动重放历史 events。因此“下次启动自然补 publish”不是现有或未来默认保证。

### 5.5 当前兼容层：compaction post-scan publish

`ContextCompactionService` 对 started/completed/failed/memory-candidates-proposed 都直接：

```python
await asyncio.to_thread(self.event_log.append, event)
```

`HostSession._publish_compaction_events_after()` 随后重新扫描 sequence，并用硬编码 event type filter 发布。

mid-turn compaction 也有类似的外层补发布职责。

这意味着每增加一个 compaction event，必须同步修改发布 filter；之前新增 memory candidate audit event 时已经暴露过这类问题。

### 5.6 governance canonical mutation 与 event append 跨事务

`MemoryWriteUnitOfWork` 在一个 PostgreSQL transaction 中提交：

- canonical memory graph mutation；
- governance decision；
- canonical mutation outbox。

退出 UOW、transaction commit 之后，`MemoryGovernanceExecutor` 才单独执行
`event_log.extend(outcome.events + ...)`。因此当前存在真实窗口：

```text
canonical memory committed
    ↓
process / event append failed
    ↓
event log 缺少该次 governance outcome
```

仅在 EventLog 外面包一层 writer 不能获得事务原子性。

本审计冻结：memory governance mutation/result events 是 **canonical runtime facts**，不是允许
稍后缺席的 audit projection。当前 production 的 memory UOW 与 event log 都在 PostgreSQL，
所以这些 events 必须通过 transaction-aware event repository，使用同一个 UOW connection、
session advisory lock 和 sequence allocator 插入 `agent_events`。transaction commit 后再调用
`publish_committed(stored_events)`。

composition root 必须证明 event repository 与 memory UOW 属于同一个 PostgreSQL
database/schema transaction domain；不允许只因为两个对象都“看起来是 Postgres”就假设可原子
提交。binding 不一致时构造期 fail closed。

若未来 memory canonical store 与 event log 被拆到不同数据库，才改用 transactional outbox +
幂等 event materializer，并明确短暂不可见语义；不能假装跨数据库有原子 commit。

### 5.7 当前兼容层：稳定事实仍写入 CustomEvent

当前 production `CustomEvent.name` 包括：

- `capability_exposure_resolved`
- `mcp_elicitation_resolved`
- `mcp_input_required_expired`
- `mcp_input_required_resolved`
- `mcp_input_required_resume_failed`
- `compaction_requested`
- `tool_result_persistence_failed`
- `tool_execution_suspended`
- `mid_turn_compaction_skipped`
- `llm.retry`

其中 capability exposure 已被 Inspector 正式投影；MCP resume、tool suspension、persistence failure 也已经是稳定的恢复/审计事实。它们不再是“临时自定义扩展”。

### 5.8 event vocabulary 必须按 ingress/ownership 分类

“当前没有内部 emitter”不等于 orphan。event vocabulary 应明确分为：

1. **production internally emitted**：Pulsara runtime 自己产生；
2. **supported external ingress**：host/外部 executor 可以合法写入，内部无 emitter 也成立；
3. **reserved but unimplemented**：已有 schema，但产品语义尚未接通；
4. **truly orphaned**：无 writer、无 ingress contract、无 projection/recovery 责任。

`RequireExternalExecutionEvent` / `ExternalExecutionResultEvent` 属于第二类，而不是 orphan：

- `MESSAGE_TRANSCRIPT_CONTEXT_CONTRACT` 明确允许 external result 作为 completed tool-result block 输入；
- assembler/reducer 已支持 replay；
- universal timing hard cut 刚要求其 timing map 完整且与 call id 一致。

因此它们应保留，并补清 external ingress owner/API，而不是因为没有内部 emitter 就删除。

`SubagentRunSuspendedEvent`、`MemoryMaintenanceProposed/Applied/RejectedEvent` 更接近第三类：当前
必须做显式产品裁决——接通 writer/recovery，或降为真正 orphan 后删除。data/hint block events 也
应先检查 provider/external ingress contract，不能只按 constructor 搜索结果判断。

### 5.9 当前成本

1. durable log 与 live hook/CLI 可见性可能不一致。
2. caller-specific event filters 是隐形事件注册表。
3. CustomEvent value 没有 schema validator、reason-code enum、版本边界。
4. 未分类的 typed event 增加每次 event contract hard cut 的迁移面积。
5. offline repair 与 live production write 没有隔离 authority。
6. 同步 Postgres connection/append 阻塞 async event 热路径。
7. governance canonical mutation 与 runtime fact 可能部分提交。

### 5.10 能不能 hard cut

能。建议建立三个边界清晰、全部 async 的接口，而不是一个带危险 offline 方法的 service：

```text
LiveRuntimeEventWriter
  append(event)      -> async commit + committed reducers + ordered observers
  append_many(events)-> async batch commit + reducers + ordered observers
  return             -> EventWriteResult（commit 与 publication errors 分离）

publish_committed(events)
  -> apply/publish 其他 PostgreSQL UOW 已原子提交的连续 stored events
  -> gap 时从 EventLog catch up，不承诺 exactly-once observer delivery

OfflineEventRepairWriter
  -> 仅 closed/quiescent session
  -> durable repair/import
  -> rebuild projections + reseed publisher before reopen
```

live writer 使用 async PostgreSQL adapter / connection pool，不能在 async API 中直接打开同步连接。
compaction、subagent、AgentRuntime 都依赖 live writer；governance UOW 依赖 transaction-aware event
repository，commit 后把 stored events 交给 `publish_committed()`。

committed reducers 与 reliable outbox subscribers 是 writer 的明确 collaborators；普通 live observers
仍由 publisher 承载，但它们的 error 只进入 `EventWriteResult.publication_errors` 和 durable health
surface，不改变 `commit_status="committed"`。

### 5.11 推荐 cut 顺序

1. 先hard cut PostgreSQL EventLog conditional atomic batch：连续sequence、CAS expected last、无交错、零partial write；pytest-only InMemory fake只做最小contract fidelity。
2. 新增thread-safe session write coordinator、`EventWriteResult` 与 committed-reducer seam；async/thread共用同一serialization boundary。
3. reducer按各自high-water先catch up缺失区间、再apply current batch；publisher使用独立high-water。
4. observer exceptions 不再从 `emit/emit_many` 冒充 commit failure，改为 publication errors/health facts。
5. 定义 same-process gap catch-up、subscriber idempotency 和 outbox/offset contract。
6. 抽出 async `LiveRuntimeEventWriter`，PostgreSQL backend 改用 async connection pool。
7. 定义 `publish_committed()` 的连续 sequence、catch-up、duplicate/idempotent 行为。
8. 定义只对 closed/quiescent session 开放的 `OfflineEventRepairWriter` 与 reopen reseed 流程。
9. compaction service 注入 live writer，删除 HostSession sequence post-scan 和 event-type filter。
10. 给 memory UOW 增加 transaction-aware event repository；canonical mutation、decision、outbox、events 同事务提交。
11. governance commit 后调用 `publish_committed()`；增加 commit 后 crash、publish failure、restart rebuild 测试。
12. 为 stable CustomEvent 逐个新增 typed event；同一 PR 删除旧写路径，不做双写兼容。
13. 建立 event ownership/ingress registry；只删除确认属于 truly orphaned 的 event。
14. `CustomEvent` 仅允许 namespaced、bounded、明确不参与 recovery/projection 的实验 diagnostic。

### 5.12 最先应 typed 化的事件

建议顺序：

1. `ToolExecutionSuspendedEvent`
2. `ToolResultPersistenceFailedEvent`
3. MCP input-required resolution / expired / resume-failed events
4. `CapabilityExposureResolvedEvent`
5. `ContextCompactionRequested/SkippedEvent`
6. `LLMRetryScheduledEvent`

## 6. 依赖方向、lazy facade 与重复 domain contract 债务

### 6.1 旧设计来源

runtime、tools、message、event、memory 共同演进，很多 DTO 最初放在最先使用它们的模块。随着功能增加，底层模块开始需要上层 concrete object。

最直接的证据是 `runtime/__init__.py` 的模块注释：它“intentionally lazy”，因为 runtime submodules 依赖 tools/memory，而 tool built-ins 又导入 runtime；eager export 会造成 import cycle。

### 6.2 当前兼容层

#### local import 隐藏依赖环

`RuntimeSession.create_tool_executor()` 在函数内部 import：

- `pulsara_agent.tools.ToolExecutor`
- `tools.builtins.registry.build_core_tool_registry`

artifact/subagent built-ins 又需要 concrete `RuntimeSession` / subagent runtime。

#### event / message 互相依赖

event schema 使用 message block DTO；message assembler 又读取 concrete event classes。两边都是基础层，方向不再单向。

#### permission preset 被复制

event validator 需要验证 run-bound permission payload，但不能安全依赖 runtime permission，于是 preset mapping 在 event 与 runtime 中分别存在。只要某个 axis 新增或默认改变，两份表就可能漂移。

#### tool taxonomy / profile 重复

terminal、write、read-only、subagent report tools、profile names 分散在：

- runtime permission / taxonomy；
- subagent runtime；
- builtin tool schema；
- capability descriptor provider。

### 6.3 当前成本

1. package import 成功依赖 lazy facade 和 local imports，而不是清晰 layering。
2. 新 DTO 放在哪里经常取决于“哪边 import 不会炸”，而不是归属。
3. duplicated preset/taxonomy 无统一 drift test。
4. tools 无法在不知道 concrete RuntimeSession 的情况下独立组合。
5. 任何 runtime 拆分都会先触发一批循环 import，增加 PR 风险。

### 6.4 能不能 hard cut

能，但必须先建立真正的低层 contract package，不能只是把所有 DTO 搬到 `common.py`。

建议依赖方向不能把 event/message 简单并列在同一层；当前环正是由二者互相导入造成的。
应冻结为：

```text
contracts.primitives
        ↓
message.schema
        ↓
event.schema
        ↓
replay / assembler / reducers
        ↓
runtime domain services
        ↓
host / cli / inspector composition
```

另一种可行做法是把所有 event-visible block DTO 一并下沉到
`contracts.primitives`，但无论选择哪种，`message.assembler/reducer` 都不能再反向成为
`event.schema` 的依赖。

tools 也不应依赖一个囊括 workspace、archive、terminal、events、subagent、memory 的
`ToolRuntimePort`；那会迅速变成新的 service locator。应提供多个小 port，例如：

- `WorkspaceAccessPort`
- `ArtifactAccessPort`
- `TerminalControlPort`
- `RuntimeEventRecorderPort`
- `SubagentControlPort`
- `MemoryQueryPort`

每个 tool binding 只接收自己需要的 ports，由 composition root 组合。

### 6.5 推荐 cut 顺序

1. 建立 `pulsara_agent/contracts/primitives`：permission preset contract、run identity、tool observation timing、event-visible primitive DTO。
2. 将 block schema 的归属冻结为 `message.schema` 或 primitives；`event.schema` 只能单向依赖它。
3. assembler/reducer 移到 replay 层，允许依赖 event schema；event schema 不反向依赖 replay。
4. event 与 runtime 共用同一个 preset expander / validator。
5. 建立 workspace/artifact/terminal/event/subagent/memory 等小 ports，不建立全能 RuntimePort。
6. built-in tools 只注入实际需要的 ports；runtime composition 创建 adapters。
7. 删除 `runtime/__init__.py` 的 lazy export 机制；若仍有 cycle，视为 cut 未完成。

### 6.6 验收条件

- CI 增加 import-linter / dependency rule。
- `python -c 'import pulsara_agent.runtime; import pulsara_agent.tools'` 不依赖 lazy attribute side effects。
- import rule 明确禁止 `event.schema -> replay/assembler/reducer` 反向依赖。
- permission presets 只有一个生产定义。
- tool/profile taxonomy 有一个 registry，descriptor 与 execution binding 从它派生。
- 任意 built-in tool constructor 不接收全能 `RuntimeSession` / service-locator port。

## 7. 生产包中的 compatibility / test / legacy surface

### 7.1 legacy MCP client manager spike

#### 旧设计来源

官方 MCP SDK 接入前，仓库实现了：

- `HttpMcpClientManager`：手写 streamable-HTTP JSON-RPC；
- `StdioMcpClientManager`：手写 JSON-line/framing stdio client。

文件 docstring 仍明确写着 `Minimal ... spike`，resume error 也称其为 `legacy ... MCP manager`。

#### 当前兼容层

生产 CLI / supervisor 已使用 `SdkMcpClientManager`。两个 spike 仍从 `runtime.mcp` 导出并由测试维护，但没有实际 production composition path。

#### 当前成本

- 三个 manager 必须共同跟随 SDK lifecycle、input-required、elicitation、close ownership 演进；
- 手写 framing/HTTP behavior 容易给人“受支持 transport”的错误印象；
- legacy path 无法完整表达现代 MCP session capability。

#### hard cut

删除 `runtime/mcp/client.py`、`stdio.py` 的生产 export 和对应 contract tests。保留：

- `McpClientManager` protocol；
- `SdkMcpClientManager`；
- `CompositeMcpClientManager`；
- supervisor / snapshot DTO。

`MockMcpClientManager` 若无产品用途，移到 `tests/support`。

### 7.2 in-memory runtime 仍是 production composition option

#### 当前兼容层

- `build_in_memory_runtime_wiring()` 自己 warning 为 `compatibility/test-only`；
- `InMemoryMemoryWriteUnitOfWork` 标为 deprecated compatibility-only；
- 但这些仍从 production package export；
- `HostCore(durable=False)` / wiring 的 `durable: bool` 仍保留完整 product branch；
- subagent 还会按 concrete `InMemoryEventLog` / `PostgresEventLog` 分支选择 child backend。

#### hard cut

production `HostCore` 永远 durable。component tests 使用 `tests/support/runtime_factory.py` 显式构造 in-memory ports，不再通过产品 flag 选择另一套架构。

这不是要求所有 unit test 都连接 PostgreSQL；而是要求 test double 不成为 production product mode。

### 7.3 preset-only permission hard cut 仍有 API 尾巴

CLI 仍展示三个 `[deprecated/test-only]` raw axis 参数：

- `--permission-profile`
- `--approval-policy`
- `--terminal-access`

随后 production helper 又拒绝它们。公开一个永远报错的选项没有兼容价值。

同时若干 production property / manifest 字段仍用 optional mode 表达，而当前 run contract 已要求 non-null preset。

#### hard cut

- 删除 CLI options，而不是保留并报错；
- production `PermissionMode` / manifest / run snapshot 统一 non-null；
- raw `EffectivePermissionPolicy` 构造器移到 component-test support；
- production ingress 只接受四个 preset。

### 7.4 legacy `LLMMessage.TOOL_CALL` 输入形态

生产 context renderer 已使用 `assistant_turn(tool_calls=tuple[LLMToolCall, ...])`，但 provider-neutral input 仍支持：

- `MessageRole.TOOL_CALL`
- `LLMMessage.tool_call(...)`
- Chat / Responses adapter 的独立 TOOL_CALL branch
- `_legacy_message_to_chat_tool_call()`

搜索当前 production constructor 后，这个形态主要由测试维持。

#### hard cut

删除独立 TOOL_CALL role。所有 tool calls 必须属于 assistant turn；tool result 的 `tool_call_id` 在 provider-neutral input 中 required。这样 pairing contract 只有一种表示。

### 7.5 ToolExecutor 的 optional descriptor / runtime context

universal timing 与 capability hard cut 已要求 production tool execution 有 descriptor，但 `ToolExecutor` 仍允许 `descriptor=None`；artifact service 也保留“caller 没传 descriptor”的兼容 fallback。

应把 direct component execution 明确放到 test adapter，production executor constructor / execute path required 地接收 descriptor 与 runtime context。unknown-tool deny 是单独的 typed fail-closed outcome，不等于正常 descriptor 缺失。

### 7.6 推荐 cut 顺序

1. 删除 rejected CLI raw permission flags。
2. 把 in-memory factories / mock MCP manager移到 test support。
3. production HostCore 去掉 `durable=False`。
4. 删除 legacy MCP manager spike。
5. 删除 `LLMMessage.TOOL_CALL`。
6. ToolExecutor production port required descriptor/runtime context。

每一项都应直接修改测试，不做 deprecated shim；仓库当前处于适合 hard cut 的开发阶段。

## 8. 数据库 schema bootstrap / migration 债务

### 8.1 旧设计来源

为了让本地开发和测试开箱即用，PostgreSQL adapters 在首次使用时执行 `CREATE TABLE IF NOT EXISTS`。随着 memory substrate 演进，schema 常量逐渐包含：

- `CREATE EXTENSION IF NOT EXISTS vector`
- 多个 `ALTER TABLE ... ADD COLUMN IF NOT EXISTS`
- backfill / update
- index / outbox / trace tables

### 8.2 当前兼容层

仓库没有独立 migration runner。以下生产对象会在构造或操作时调用 `ensure_schema()`：

- session manifest / resume；
- working context；
- candidate pool；
- mutation outbox / reconcile / index sync；
- Oxigraph materializer tracking；
- recall trace；
- PostgreSQL graph。

更严重的是，`MemoryWriteUnitOfWork.__enter__()` 在每次 governance write transaction 中执行：

- `RUNTIME_TRUTH_SCHEMA_SQL`
- `CANDIDATE_POOL_SCHEMA_SQL`
- `MEMORY_SUBSTRATE_SCHEMA_SQL`

### 8.3 当前成本

1. 正常请求需要 DDL / extension 权限。
2. 热路径可能取得 schema locks，性能取决于 PostgreSQL DDL fast path。
3. schema hard cut 没有版本号，无法准确报“binary 与 DB 不兼容”。
4. 多个 adapter 自己决定何时 bootstrap，启动成功不代表稍后所有路径都可用。
5. rollback / forward-only migration / production deploy 顺序无法表达。

### 8.4 能不能 hard cut

必须 hard cut。是否使用 Alembic 不是核心；核心是 schema 变更必须离开请求路径，并有单一版本事实。

生产策略在此冻结，不保留二选一：

- 独立的 privileged `pulsara db migrate` 执行 schema migration；
- production host startup 永远 verify-only，只检查 version/checksum，不执行 DDL；
- local development 可通过显式配置启用 auto-migrate，但默认关闭，且不得复用 production runtime DB role；
- migration runner 使用 PostgreSQL advisory lock，避免多个进程并发迁移；
- 每个 migration 保存 immutable checksum，已应用 migration 内容变化必须报错；
- existing DB 只能通过显式 baseline/adopt 流程接管，并验证 schema fingerprint；不能把任意旧库静默标为 latest。

ledger 只保留一个真源：

```text
schema_migrations(
  version,
  name,
  checksum,
  applied_at,
  application_version
)
```

当前 schema version 由最新 migration row 派生，不再维护第二张
`pulsara_schema_version` 表或平行 metadata 值。

governance same-UOW 还要求 composition root 验证 transaction domain：event repository 与
`MemoryWriteUnitOfWork` 必须连接同一个 PostgreSQL database 和 schema。当前 durable wiring
恰好复用 `settings.storage.postgres_dsn`，但未来若配置拆分，必须在构造期 fail closed，不能退回
跨连接“先 memory commit、再 event append”。最好注入同一个 `PostgresTransactionDomain` / connection
factory，而不是分别传两个未经约束的 DSN 字符串。

### 8.5 推荐 cut 顺序

1. 新增唯一 `schema_migrations` ledger 和 versioned migration runner；version 由最新 row 派生。
2. 把当前三个 schema SQL 拆成 baseline migration + 后续 incremental migrations。
3. CLI 提供 privileged `pulsara db migrate`，实现 advisory lock、checksum 和 baseline/adopt-existing-DB。
4. production host startup 只做 schema version/checksum verification；不提供隐式 auto-migrate。
5. production adapter 只检查 supported schema version，不执行 DDL。
6. `MemoryWriteUnitOfWork` 删除所有 schema SQL。
7. local-dev/test fixture 在 suite/session setup 显式迁移一次。
8. 对 hard-cut event payload，可选择增加 JSONB CHECK；复杂 policy 等价仍由 Pydantic/contract test 验证。

### 8.6 验收条件

- 以无 DDL 权限的 runtime DB role 可运行完整 host。
- 未迁移 DB 在 host startup 明确失败，不在第一个 memory write 才失败。
- governance transaction trace 中不出现 schema DDL。
- governance event repository 与 memory UOW 的 database/schema binding 不一致时，wiring 构造 fail closed。
- 两个 migration process 并发启动时只有一个持有 advisory lock，另一个等待或安全退出。
- migration checksum drift 与未经验证的 adopt-existing-DB 都 fail closed。

## 9. Tool-result renderer 的领域耦合债务

### 9.1 旧设计来源

`context_engine/tool_results.py` 从“把 ToolResultBlock 变成 LLM message”演进为约 2200 行的子系统，现承担：

- transcript segmentation；
- two-phase budget allocator；
- stable render cache；
- universal observation timing；
- artifact primary/read-more policy；
- terminal essential envelope；
- terminal_process inventory sorting；
- body/envelope/total cap 与 borrow accounting；
- compact/ref-only/omitted 策略。

### 9.2 当前兼容层

context layer 直接知道：

- `terminal` / `terminal_process` 名称；
- terminal session id、backend type、process action 等 identity keys；
- MCP model tool-name normalization 常量；
- 哪些 terminal 状态字段是 essential；
- process list 中 running/actionable/finished 的排序。

这使 generic context allocator 同时成为 terminal adapter 和 MCP naming adapter。

此外，计划中的 typed render profile 目前没有 durable replay source：

- `ToolResultEndEvent` 只 durable 地保存 state、artifact refs 和 universal observation timing；
- execution-time `model_tool_name` 没有写入 event；
- terminal operational kind / essential fields 仍由当前 payload和 renderer heuristic 推导；
- render policy/schema version 没有随 tool result 持久化；
- replay reducer 只恢复 timing metadata，不恢复领域 render facts。

因此现在直接删除 payload heuristic，会出现 live compile 有 essential envelope，而 resume/compaction
replay 后只剩 generic body/artifacts 的漂移。

### 9.3 当前成本

1. 新 built-in structured tool 想要 essential envelope，只能继续向这个文件增加 heuristic。
2. 外部/custom payload 若撞到字段名，容易被误识别为内建语义；此前 generic timing 从 payload 猜测已经出现过同类问题。
3. tool domain schema 改动需要修改 context compiler 测试。
4. render cache fidelity 判断与 terminal-domain policy 强耦合。
5. 文件拆分困难，因为 allocator 输入还不是完全 normalized 的 render unit。
6. render profile 随当前代码重新推导，历史 tool result 的模型可见语义会在升级后改变。

### 9.4 能不能 hard cut

能。不要删除 generic allocator，而应让它只消费 tool/capability 层产生的 typed facts：

```python
ToolResultRenderProfile(
    model_tool_name=...,
    body_candidate=...,
    artifacts=...,
    observation_timing=...,
    essential_envelope=...,
    operational_kind=...,
)
```

terminal adapter 在 tool execution/persistence boundary 生成 `TerminalEssentialEnvelope`；MCP/custom 默认没有领域 envelope。context layer 不从 arbitrary JSON 猜 tool kind。

同时定义 event-safe 的 `ToolResultRenderProfileFact`，至少包含：

```text
schema_version
profile_fingerprint
descriptor_id / tool_origin
model_tool_name
operational_kind
typed essential_envelope facts
```

它必须在 tool execution/persistence boundary 生成，并写入 `ToolResultEndEvent` 的 typed field 或
required durable metadata。`ExternalExecutionResultEvent` 作为 supported ingress，则需要按 call id
提供同形状的 render-profile map。replay 将该 fact 恢复到 normalized `ToolResultRenderUnit`；renderer
只做 budget-dependent full/minimal/omitted 选择，不重新猜领域身份。

不要持久化最终 rendered string；应持久化 execution-time facts和 schema version。这样预算变化可
重新分配，而 tool identity/operational meaning 不会随当前代码漂移。

### 9.5 推荐 cut 顺序

1. 定义 `ToolResultRenderProfileFact` schema/version/fingerprint，并加入 ToolResultEnd / external-result ingress contract。
2. tool execution boundary 从 capability descriptor 冻结 execution-time `model_tool_name` 和 operational kind。
3. terminal/terminal_process 生成 typed essential facts；replay 恢复同一 fact。
4. 抽出纯 `ToolResultBudgetAllocator`，输入 normalized units，输出 decisions。
5. 抽出 generic envelope renderer（header、timing、artifact refs、body）。
6. model tool-name normalization 由 capability descriptor 提供，context 只读取 durable already-exposed name。
7. 增加 live/replay/compacted-history render-unit equality 后，删除 `_is_terminal_like_payload()` 等 payload heuristic。
8. render cache key 包含 render-profile schema/fingerprint；value 只保存 canonical decision/fidelity，不保存重新推断副产物。

## 10. Capability declaration、subagent profile 与 skill discovery 债务

### 10.1 旧设计来源

工具最初由 registry/tool class 声明；unified capability surface 又需要独立 descriptor。为了尽快 hard cut 到 descriptor-first exposure，built-in provider 复制了工具描述和 JSON schema。

subagent 随后又引入 profile-specific tool subset 和 permission constraints。

### 10.2 当前兼容层

#### descriptor 与 tool class 双声明

tool class / registry 与 `BuiltinToolCapabilityProvider` 都维护 description、schema、read-only/concurrency 等信息。模型 exposure 以 descriptor 为准，而 `ToolRegistry.tool_specs()` 主要由测试使用。

#### stale resolve 参数

`CapabilityRuntime.resolve_for_turn(permission_policy, plan_active)` 接收参数后直接丢弃。visible-but-gated 设计已经决定 exposure 不随 permission mode 隐藏，但旧 API 仍暗示会变化。

#### subagent profiles 多处列举

`research_worker / verification_worker / review_worker` 及其 tool subsets 同时存在于 runtime set、tool schema enum、descriptor 和文档/测试。

#### skill roots 与 ignored frontmatter

当前仍扫描四类 roots：

- workspace `.pulsara/skills`
- workspace `.agents/skills`
- user `~/.pulsara/skills`
- user `~/.agents/skills`

部分 frontmatter（如 allowed/blocked scopes）会被解析后忽略或只诊断；这会让 skill author 误以为其约束已生效。

### 10.3 当前成本

- tool schema 修一次可能只更新 model exposure 或只更新 executor validation；
- profile 新增/改名需要多文件同步；
- dead resolver args 让调用方传入无效 facts；
- skill root precedence 和 ignored field 是隐性兼容策略。

### 10.4 能不能 hard cut

能，但 descriptor-first 方向应保留。

### 10.5 推荐 cut 顺序

1. 定义 built-in `ToolDefinition`，descriptor 与 binding adapter 从它派生；tool implementation 只提供 execute。
2. 删除 production `ToolRegistry.tool_specs()` 或让它直接读取 capability descriptor，不维护第二 schema。
3. `CapabilityRuntime.resolve_for_turn()` 删除无效 permission/plan 参数。
4. 建立 `SubagentProfileRegistry`，tool schema enum、runtime profile、inspect 都从 registry 生成。
5. 冻结 canonical skill roots；若继续兼容 `.agents/skills`，必须给出明确退出版本。
6. ignored frontmatter 要么实现，要么 schema-level reject，不再 warning 后继续。

## 11. Runtime hook 的 critical path 与非 durable failure 债务

### 11.1 旧设计来源

`RuntimeEventPublisher` 通过 ordered subscribers 让 timeline、artifact、memory outbox 等投影跟随 canonical events 更新。同步顺序最初有利于一致性和测试确定性。

### 11.2 当前兼容层

- `RuntimeSession.emit/emit_many` 是 async API，却直接执行同步 `EventLog.append/extend`；
- `PostgresEventLog.extend()` 每批新建同步 psycopg connection，并在 event loop thread 上完成 lock/insert/run projection；
- publisher await 每个 subscriber；
- hook manager 顺序执行 hooks；
- timeline persistence、graph/outbox reconcile 等 hook 内含同步 DB / archive 工作；
- exception 被捕获到内存 `hook_manager.errors`；
- process 重启后 failure 事实丢失。

### 11.3 当前成本

1. canonical event append 本身就在 model/tool event hot path 阻塞 event loop，并重复支付 connection setup。
2. 非关键投影延迟直接增加 model/tool event path latency。
3. event 已 durable，但 hook 失败没有 durable retry schedule / health fact。
4. sync I/O 在 async publisher 上继续阻塞 event loop。
5. 调用方难以区分“event commit 失败”和“某个 observer 失败”。

### 11.4 能不能 hard cut

能，但需要区分两类 hook：

- critical transactional invariant：失败应使 command fail/repair；
- best-effort projection/observer：event commit 后异步处理，可 durable retry。

### 11.5 推荐 cut 顺序

1. 先按第 5 节把 canonical event writer 改为 async connection pool；禁止 async emit 直接调用同步 PostgresEventLog。
2. 给 hook 声明 `criticality`、delivery mode、idempotency key。
3. canonical event commit 后，把 persistent projection job 写入 outbox。
4. background worker 处理 graph/index/archive projection，并记录 retry/dead-letter。
5. publisher 只同步运行轻量 critical hooks。
6. 新增 durable `RuntimeObserverFailedEvent` 或 health table；不只保存在 Python list。
7. 禁止 sync DB I/O 直接运行在 async publisher loop。

## 12. Compaction core 与 memory candidate producer 的依赖方向

### 12.1 旧设计来源

为了复用 compact LLM 已经读取过的长上下文，compaction prompt 增加 optional memory-candidate block。这个产品行为合理，也避免第二次 LLM 调用。

### 12.2 当前兼容层

`runtime/compaction` 现在直接 import / 持有：

- memory candidate pool DTO / sink；
- memory domain / scope；
- ontology kind；
- candidate fingerprint / duplicate policy；
- governance visibility metadata。

`ContextCompactionPolicy` 也嵌入 memory candidate policy。`ContextCompactionService` 同时负责：

- compaction eligibility；
- prompt；
- LLM call；
- summary parse/artifact；
- compaction events；
- candidate parse、scope force、redaction、append；
- zero-proposal audit event。

### 12.3 当前成本

1. context maintenance 层依赖 memory product semantics。
2. 没有 memory domain 时，compaction wiring分支决定 candidate behavior。
3. candidate schema/ontology变化会修改 compaction service。
4. service 过大，summary correctness 与 candidate best-effort 很难独立测试。

### 12.4 能不能 hard cut

能，但不能简单改成“CompletedEvent 后再调用第二个 LLM”，否则失去复用同一 compact output 的价值。

建议引入 typed compaction extension：

```python
class CompactionOutputExtension(Protocol):
    prompt_fragment: str
    def parse(raw_output, completed_summary) -> ExtensionParseResult: ...
```

compaction core 只知道 extension 的 prompt fragment、bounded raw block、diagnostics 和 audit append callback；memory-owned extension 负责 candidate DTO、scope、pool、ontology。

更彻底的方案是：core 只 durable 地写 summary artifact + extension artifact/event，memory producer订阅后 append pool。无论采用哪种，都不要让 compaction core import candidate pool concrete model。

### 12.5 推荐 cut 顺序

1. 把 candidates parser/sink 移到 memory-owned package。
2. 定义 compaction extension contract 与 output block registry。
3. summary parse/failure 保持 core hard contract；extension parse 始终 best-effort。
4. event writer 统一后，memory extension 使用同一 stored event attribution。
5. `ContextCompactionPolicy` 只保留 context policy；candidate extraction policy归 memory extension。

## 13. 暂定 hard cut 优先级

### P1：先解决事实源漂移和生产安全边界

1. **Subagent graph facts + 纯 reducer + bootstrap/projector 收口（阶段 A）**
   - 已有可复现 drift；无 writer 前置依赖；应最先做。
2. **最小 committed-reducer seam + Subagent command-path hard cut（阶段 B）**
   - conditional atomic commit后，reducer先catch up再apply current，publisher独立catch up；随后删除 live `_tasks/_runs` 直接 mutation。
3. **ResolvedModelCall/Fact 成为 compiler/transport/durable events 的单次调用真源**
   - 删除 256k/8k 常量；effective options、token estimator、limits 只解析一次。
4. **async live event writer + governance event transaction 原子性**
   - 删除 compaction append-then-rescan-publish；memory mutation/events 同 UOW commit。
5. **版本化数据库 migration**
   - 从所有 runtime/UOW 热路径移除 DDL。
6. **建立低层 contracts / ports，开始解除依赖环**
   - 先 permission preset、event-visible primitives 和多个小 tool capability ports。

### P2：收口半迁移架构与生产兼容面

7. immutable compile snapshot + normalized transcript/tool-result units + `ContextSource`，最后删除 `ContextCompileInputs`。
8. AgentRuntime typed working set + interaction/context/tool coordinators。
9. stable CustomEvent typed 化；分类 external ingress，并只删除 truly orphaned event vocabulary。
10. 删除 legacy MCP managers、in-memory product mode、raw permission CLI flags。
11. 删除 `LLMMessage.TOOL_CALL` 与 optional descriptor production path。
12. 先持久化 typed tool render-profile fact，再让 renderer 消费并移除 domain payload heuristic。
13. hook critical/best-effort 分层 + durable retry/failure。

### P3：减少声明漂移与长期维护税

14. ToolDefinition / descriptor / binding 单一声明。
15. SubagentProfileRegistry。
16. skill roots 与 ignored frontmatter hard cut。
17. compaction extension boundary。

## 14. 依赖表

| 工作项 | 前置依赖 | 为什么 |
|---|---|---|
| Subagent graph facts / reducer / bootstrap / projector（阶段 A） | 无 | 独立、已有 drift、收益立即可测 |
| Conditional atomic EventLog batch + Minimal committed-reducer seam | 无 | CAS阻止stale plan入账；区分durable commit、deterministic state apply与observer errors |
| Subagent command-path hard cut（阶段 B） | 阶段 A、committed-reducer seam | event 已 commit 时必须保证 live graph state已 apply |
| SubagentGraphHydrator | Subagent graph facts、EventLogLocator、archive reader | 正文与 child native facts 不是纯 parent-event state |
| ChildExecutionRegistry | Subagent command-path hard cut | process handles 与 durable graph facts 分离 |
| ResolvedModelCall | model limits 配置可解析 | compiler/transport 必须共享单次调用事实 |
| ResolvedModelCallFact/event identity | ResolvedModelCall | compiled/start events需要稳定 join，retry/fallback可审计 |
| Immutable ContextFactSnapshot | ResolvedModelCall | compile input 不再暴露 mutable LoopState |
| Normalized transcript/tool-result units | ContextFactSnapshot | pairing/order 不能被预渲染字符串抹平 |
| ContextSource hard cut | ContextFactSnapshot、normalized units | sources 只产结构化 facts/candidates，不重新包装旧字符串 facade |
| Durable tool-result render-profile fact | capability descriptor、ToolResultEnd/external ingress schema | live/replay operational facts必须一致 |
| Tool-result render profile | durable render-profile fact、normalized tool-result units | allocator 输入先稳定，避免先拆后改形状 |
| Async LiveRuntimeEventWriter | committed-reducer seam、async Postgres event adapter/pool | typed event、hook、compaction都依赖它 |
| Governance events 同 UOW | transaction-aware event repository、LiveRuntimeEventWriter | memory mutation与 runtime facts 原子提交，commit 后连续发布 |
| CustomEvent typed 化 | LiveRuntimeEventWriter | 避免新 typed event继续走两段式发布 |
| Hook/outbox 重构 | LiveRuntimeEventWriter、migration runner | 需要 durable job/failure schema |
| Runtime dependency-cycle cleanup | contracts/ports package | 没有低层归属只能移动循环 |
| AgentRuntime coordinator 拆分 | typed working set、ports初步建立 | 否则只是把循环 import 分散到更多文件 |
| 删除 legacy MCP / in-memory product mode | test support factory | 保留 unit test 能力但移除产品分支 |
| Schema hot-path hard cut | privileged migration command、verify-only startup | 先有替代部署路径 |
| Compaction-memory extension | LiveRuntimeEventWriter | extension audit/append需要明确 commit boundary |

建议依赖图：

```text
Model limits configuration
    └─ ResolvedModelCall
         ├─ ResolvedModelCallFact / event identity
         └─ Immutable ContextFactSnapshot

Capability descriptor + ToolResultEnd/external ingress schema
    └─ Durable tool render-profile facts

Immutable ContextFactSnapshot + durable tool render-profile facts
    └─ Normalized transcript/tool-result units
         ├─ ContextSource hard cut
         └─ Tool-result render profiles

Minimal committed-reducer seam
    ├─ Subagent command-path hard cut
    └─ Async LiveRuntimeEventWriter

Async LiveRuntimeEventWriter
    ├─ typed stable events
    ├─ governance events same-UOW commit
    ├─ hook/outbox split ── Schema migrations
    └─ compaction memory extension

contracts / ports
    ├─ dependency-cycle cleanup
    ├─ production compatibility cuts
    └─ AgentRuntime coordinator split

Subagent graph facts/reducer/bootstrap/projector 阶段 A（可独立立即推进）
Schema migrations      （可独立立即推进）
```

## 15. 不建议采用的重构方式

### 15.1 不做一次性 runtime rewrite

不做一次性、无行为护栏的全 runtime 重写。由于系统尚未上线，可以实施激进的 subsystem hard cut：不保留旧 schema、旧 API、双写事件或兼容分支；但必须按事实源逐块替换，并在每一步保持 durable replay、compiled payload 和 inspector projection 可验证。

### 15.2 不为了减少表/后端而合并 canonical store 与 projections

多 store 不是本轮核心问题；先解决 migration 和 outbox ownership。

### 15.3 不为后向兼容双写新旧 event

当前项目已经多次选择 hard cut。typed event migration 应修改 writer、reader、projection、tests 同 PR 完成；不要长期同时写 typed event + CustomEvent。

### 15.4 不保留“deprecated 但永远报错”的公开 API

CLI raw permission axes、production in-memory flag、legacy MCP export 等应直接删除。测试需要的能力放到 test support，不应伪装成产品兼容。

### 15.5 不把 source-of-truth 问题误修成更多 cache consistency code

subagent drift、permission preset duplication、tool taxonomy duplication都应删除副本，而不是新增同步逻辑。

## 16. 每个 debt PR 的统一验收模板

每次 hard cut 至少回答：

1. **唯一真源是什么？**
2. **旧真源是否已删除，而不是标 deprecated？**
3. **durable event、live runtime、inspector 是否从同一 reducer/contract 得到相同结果？**
4. **replay / resume 是否有独立测试？**
5. **pressure / failure 是否 fail closed，且不会静默扩大权限或丢事实？**
6. **production path 是否还存在 test-only branch？**
7. **是否增加 import/dependency rule，防止旧边界重新长回来？**
8. **是否有 real-LLM 或 durable dogfood 能证明用户可见行为没有倒退？**
9. **event write 是否保持连续 sequence；offline repair 是否只在 quiescent session 执行？**
10. **跨表 canonical mutation 与对应 runtime events 是否同事务，或有明确 outbox 语义？**
11. **commit、committed reducer apply、observer publication 是否分阶段；observer error 会不会伪装成 commit failure？**
12. **模型 compile 与 transport 是否消费同一个 `ResolvedModelCall`，durable events 是否保存同一个 fact identity？**
13. **tool execution-time render-profile facts 是否 durable，live/replay units 是否一致？**
14. **graph equality 比较的是纯 facts，archive/child-log hydration 与 process handles 是否明确分层？**
15. **migration 是否只有一个 ledger；same-UOW repositories 是否属于同一 database/schema transaction domain？**
16. **event 是 internal emit、external ingress、reserved 还是 truly orphaned，ownership 是否已声明？**

## 17. 这份文档的后续更新方式

这份审计应作为 active debt ledger，而不是完成记录的堆积地。

规则：

- 某项完成 hard cut 后，在对应章节加入 commit、删除的旧路径和验收结果；
- 完成并稳定一段时间后，将该章节移入 `archived_docs/` 的完成审计，不长期留在 active 文档；
- 新功能若引入第二真源、compatibility facade 或 caller-specific publish/filter，应立即补入；
- 只因文件变长，不足以新增债务；必须指出 ownership、truth、dependency 或 lifecycle 的具体问题；
- 文档中的 line number 只作审计快照，后续以 symbol 名称和 git history 为准。

本轮审计的核心判断可以压缩成一句话：

> Pulsara 的 durable architecture 主线已经正确；下一阶段不应继续增加并行兼容路径，而应让 model limits、subagent graph、event writes、schema version 和 tool/runtime contracts 各自只剩一个生产真源。
