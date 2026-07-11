# Pulsara 下一阶段五步 Hard-Cut 总路线

> 状态：冻结的阶段顺序与跨阶段契约；每一阶段仍需自己的实施规格。  
> 基线：ResolvedModelTarget / ResolvedModelCall hard cut 已完成；Subagent graph reducer hard cut 已完成。  
> 进度：阶段一 MCP Startup Latency Hard Cut 已完成；下一实施章固定为阶段二 Context Compiler Input Hard Cut。  
> 原则：项目尚未上线，不为旧 event、旧数据库、旧 constructor 或旧 runtime facade 保留生产兼容路径。

## 0. 结论

下一阶段固定为五个相互依赖、但必须独立全绿的 hard-cut 章节：

1. **MCP Startup Latency Hard Cut（已完成）**
2. **Context Compiler Input Hard Cut**
3. **Long-Horizon Context Windows**
4. **ContextSource Ownership Hard Cut**
5. **Prompt Cache**

依赖关系如下：

```text
ResolvedModelCall（已完成）
        │
        ├── MCP Startup Latency Hard Cut
        │      独立解决 session open / safe-point 的远端阻塞
        │
        └── Context Compiler Input Hard Cut
                 │
                 ├── Long-Horizon Context Windows
                 │       │
                 │       └── context-window / rollup / compaction identity
                 │
                 └── ContextSource Ownership Hard Cut
                              │
                              └── Canonical ProviderInputPlan
                                           │
                                           └── Prompt Cache
```

Prompt Cache 不能越过第 2、3、4 步直接实施。否则 cache identity 会把 mutable
`LoopState`、旧字符串包装 facade、未稳定的 tool-result rollup 或临时 section ownership
误当作长期输入契约。

## 1. 为什么是五步而不是四步

Reviewer 提醒的遗漏是正确的：`ContextFactSnapshot` 只解决“compiler 读什么”，并不自动解决
“谁拥有 section、谁能把字符串包装成 context”。

因此必须明确区分：

- **Context Compiler Input Hard Cut**：稳定事实输入、transcript pairing 和 tool-result render unit；
- **ContextSource Ownership Hard Cut**：稳定非 transcript source 的所有权、候选 shape、registry 和 lowering 边界。

二者可以连续实施，但不能只做前者便开始 Prompt Cache。

## 2. 全局 Hard-Cut 规则

以下规则适用于五个阶段的每个 PR。

### 2.1 Schema 与事实

- 新 schema 字段 required；不以 nullable 表示“旧事件没有”。
- 不从 scratchpad、当前 session default、当前时间或旧字段推断新事实。
- 不保留旧 constructor、alias、compatibility overload 或 fallback reader。
- event payload、manifest、Inspector projection 与 replay reducer 消费同一 typed DTO。
- 同一个事实只有一个 durable truth；projection denormalization 必须写 invariant。
- event-safe fingerprint 不包含 secret、URL query、userinfo 或明文 header/token。

### 2.2 数据与迁移

- 新 contract version 下的旧 event / manifest 非法，不进入 supported runtime path。
- 开发阶段允许 reset PostgreSQL / Oxigraph。
- 如果需要保留 dogfood 数据，使用一次性显式 migration，不做 runtime fallback。
- runtime DB role 默认 verify-only；DDL 由独立 migration 路径执行。

### 2.3 PR 边界

- 每个 PR 删除一个旧真源，并增加 grep gate。
- 每个 PR 在合并前独立通过 Ruff、全量 pytest 和适用的 real-LLM dogfood。
- 不允许用“后续 PR 会补”解释当前 PR 无法运行的 ownership 缺口。
- background task、thread、manager、lease 都必须有明确 owner、cancel、drain 和 retry 语义。
- 所有 safe-point mutation 必须有单一线性化边界。

### 2.4 Architecture guards

至少维护以下静态 guard：

- production compiler 不 import / 接收 `LoopState`；
- production 不构造 `ContextCompileInputs`；
- production MCP open/turn/resume 不调用同步远端 `sync_servers()`；
- ContextSource 不返回预渲染 provider message 或任意字符串 facade；
- provider adapter 不重新解释 context budget、cache identity 或 compaction generation；
- `tool_result_context_chars=36_000` 不再是 run-ending 独立真源；
- Prompt Cache 不读取 live manager、scratchpad 或当前 wall clock 重算 identity。

## 3. 阶段一：MCP Startup Latency Hard Cut（已完成）

### 3.1 目标

- optional MCP 的连接与发现不阻塞 `HostCore.open_session()` 和 REPL 横幅；
- required MCP 具有明确的 blocking deadline 与失败语义；
- worker 只产生候选 snapshot / binding，不直接改 HostSession wiring；
- HostSession 只在 safe point 原子安装 descriptor 与 execution binding；
- config epoch 阻止 disable、reconfigure 或 close 之后到达的 stale completion；
- close/shutdown cancel 并 bounded drain 所有 session-owned MCP work；
- 删除 session open 与每 turn/resume 的旧同步远端 sync 路径。

### 3.2 不在本阶段做

- 不改 MCP tool call / elicitation / input-required 的产品协议；
- 不做跨 HostSession 的持久 snapshot cache；
- 不做 Prompt Cache；
- 不让 background worker mid-run 修改 exposure；
- 不把 MCP manager 提升为 HostCore 全局共享 singleton。

### 3.3 完成定义

- 只有 optional slow MCP 时，session open latency 不包含 connect/discovery wall-clock；
- required server 未 READY 时，open 在 deadline 内成功或以 typed error 失败；
- config 未变且 snapshot 未过期时，new turn 不触发远端 discovery；
- descriptor 与 executable binding 带相同 installation/snapshot identity；
- stale worker 无法回写，并关闭自己持有的 manager；
- session close 后没有 MCP task、SDK owner task、HTTP client 或 stdio process 遗留；
- Inspector 可看到 STARTING / READY / DEGRADED / FAILED、epoch、generation 和分阶段 timing。

详细规格见：

`PULSARA_MCP_STARTUP_LATENCY_HARD_CUT_IMPLEMENTATION.zh.md`

实施闭环：M0–M4 已删除同步 startup/turn sync、旧 manager+bundle 双真源和 legacy timeout schema；真实
public MCP、required failure deadline、REPL optional-background trajectory、全量 pytest 与 architecture gates 已通过。
路线当前指针移至第 4 节。

## 4. 阶段二：Context Compiler Input Hard Cut

### 4.1 目标

把 context compile 的输入冻结为不可变事实，而不是把整个 agent working state 交给 compiler。

核心 DTO：

```python
class ContextFactSnapshot(BaseModel):
    runtime_session_id: str
    run_id: str
    turn_id: str
    reply_id: str
    permission_snapshot: RunPermissionSnapshotFact
    resolved_model_call: ResolvedModelCallFact
    capability_snapshot: CapabilityExposureSnapshotFact
    plan_snapshot: PlanContextFact
    memory_projection: MemoryProjectionFact | None
    subagent_projection: SubagentContextFact | None
    timing: ContextCompileTimingFact

class TranscriptCompileInput(BaseModel):
    messages: tuple[TranscriptMessageFact, ...]
    current_user_anchor: str
    compacted_windows: tuple[CompactedWindowFact, ...]

class ToolResultRenderUnit(BaseModel):
    tool_call_id: str
    tool_name: str
    call_position: int
    result_position: int
    result_state: str
    content: ToolResultContentFact
    artifacts: tuple[ToolResultArtifactRef, ...]
    observation_timing: ToolObservationTiming
    render_profile: ToolResultRenderProfileFact
```

字段名可在实施文档中微调，但 ownership 不再开放讨论：

- snapshot builder 读取 live runtime；
- compiler 只读 snapshot / transcript / render units；
- tool-result pairing 在进入 compiler 前已结构化；
- compiler 不通过 scratchpad 找 cache、span 或 fallback message。

### 4.2 PR 顺序

#### C0：低层 DTO 与 schema contract

- 新增 immutable `ContextFactSnapshot`；
- 新增 `TranscriptCompileInput`；
- 新增 normalized `ToolResultRenderUnit`；
- 新增 fingerprint/version；
- event-visible DTO 放低层 primitives/contracts，避免 event/runtime 反向依赖。

#### C1：Snapshot builder

- 在 AgentRuntime / HostSession safe point 构造 snapshot；
- 所有 mutable dict/list 递归 freeze；
- snapshot 记录 source event ids/sequences；
- current user、tool timing、permission、resolved call 不做二次推断。

#### C2：Transcript 与 tool-result normalization

- assembler/reducer 产出 provider-neutral typed transcript；
- call/result pairing、provider-native assistant tool call、external result 全部规范化；
- tool-result renderer 接收 resolved estimator 与 render units；
- 删除 compiler 前的第二套 chars/4 估算真源。

#### C3：Compiler API hard cut

生产 API 只接受：

```python
compile_context(
    *,
    facts: ContextFactSnapshot,
    transcript: TranscriptCompileInput,
    tool_results: tuple[ToolResultRenderUnit, ...],
    section_candidates: tuple[ContextSectionCandidate, ...],
) -> CompiledContext
```

`ContextSectionCandidate` 的最小 typed shape 在 C0 一并定义，以保证 C3 没有临时 API 空洞。
C 阶段允许现有 AgentRuntime collector 把各子系统事实转换为 candidate，但禁止再传裸 component
string；S 阶段再把这些 producer 的 ownership 迁入正式 source registry 并删除 collector facade。

- 删除 `ContextCompileRequest.state`；
- 删除 production `ContextCompileInputs`；
- 删除 legacy current-user fallback；
- 删除 scratchpad render-cache fallback。

#### C4：Replay / recovery / Inspector

- replay 从 durable event 重建同形状 snapshot；
- live/replay compile fact equality；
- Inspector 展示 snapshot id、source sequences、normalized unit counts；
- schema 缺失直接 contract error。

#### C5：删除旧 facade

- 删除 `build_llm_context()` / `msg_to_llm_messages()` production path；
- 删除所有旧 constructor；
- grep gate；
- 全量 real-LLM、plan、MCP resume、subagent、compaction 回归。

### 4.3 完成定义

- production compiler 无法访问 `LoopState`；
- compile 输入可稳定序列化、fingerprint 和 replay；
- normalized transcript 保留 pairing/order；
- tool-result renderer 与 final estimator 使用同一个 resolved call；
- `ContextCompiledEvent` 可 join 到 snapshot 与 source sequences；
- 同一 snapshot + 同一 compiler version 必须得到同一 provider-neutral compiled payload。

## 5. 阶段三：Long-Horizon Context Windows

### 5.1 目标

一个 user run 可以跨多个 bounded model-visible context window 持续推进；durable EventLog 与 artifact
不被删除，只有 compiled projection 被 rollup、micro-compact 或 LLM compact。

研究输入：

`PULSARA_LONG_HORIZON_BUDGET_PRIOR_ART_RESEARCH.zh.md`

进入本阶段前必须将研究文档升级为 ResolvedModelCall 同等精度的实施规格。

### 5.2 必须冻结的身份

```python
class ContextWindowFact(BaseModel):
    window_id: str
    run_id: str
    generation: int
    previous_window_id: str | None
    opened_at_sequence: int
    closed_at_sequence: int | None
    resolved_model_target_fingerprint: str
    input_budget_tokens: int

class ToolObservationProjectionFact(BaseModel):
    tool_call_id: str
    source_result_event_id: str
    representation: Literal[
        "full", "preview", "artifact_locator", "essential", "rollup", "cleared"
    ]
    projection_generation: int
    reason_code: str

class RolloutBudgetStateFact(BaseModel):
    phase: Literal[
        "exploration", "warning", "restricted", "finalization_only", "exhausted"
    ]
    consumed_input_tokens: int
    consumed_output_tokens: int
    consumed_tool_units: int
    finalization_reserve: FinalizationReserveFact
```

### 5.3 PR 顺序

#### L0：预算、window、rollout DTO 与 typed events

- active context、observation projection、rollout、step、progress 分开；
- window identity、projection generation、rewrite reason；
- typed window opened/closed、projection rewritten、budget phase changed events；
- Inspector join contract。

#### L1：36K hard cap → dynamic soft projection target

- 删除固定 `tool_result_context_chars` 作为 run-ending truth；
- soft target 从 `ResolvedModelContextBudgetFact.input_budget_tokens` 派生；
- required non-tool tokens 先计量；
- hard available 是 final input budget 的真实剩余；
- 超 soft target继续 degrade，不直接 fail；
- final resolved input budget仍是 provider 前 hard cap。

#### L2：跨 tool-result rollup 与 artifact-aware thinning

- old completed observations可合并成 bounded rollup；
- latest/currently actionable result优先保留；
- artifact locator、timing、result state、pairing不可丢；
- raw event/artifact不变；
- render decision durable，可 replay，不因下一 compile 随机漂移。

#### L3：current-run deterministic micro-compaction

- 只处理已 completed、非 pending、非 latest 的旧 tool body；
- 不跨未闭合 pairing；
- 不动 current user、pending interaction、latest error evidence；
- 写 projection rewrite event；
- 不调用 LLM。

#### L4：pairing-safe current-run LLM compaction

- 同一 run 内打开下一 context window；
- summarizer 使用独立 ResolvedModelCall；
- summary 覆盖明确 sequence/window；
- protected current tail 与 pending state完整保留；
- compaction 失败恢复到旧 window或进入 finalization，不能半写 projection；
- durable raw history不删除。

#### L5：rollout budget、finalization reserve、阶段状态机

正常状态机：

```text
exploration
  -> warning
  -> restricted_low_value_exploration
  -> finalization_only
  -> exhausted
  -> emergency_hard_stop
```

- `max_turns/max_tool_calls` 提高并降级为 emergency circuit breaker；
- 正常预算耗尽前至少保留一次完整 synthesis model call；
- finalization-only 禁止新搜索/抓取，可读取已有 artifact/evidence；
- 无论成功或预算耗尽都必须产出可读结论。

#### L6：provenance-aware evidence progress guard

- search query、URL、artifact、result/evidence fingerprint；
- repeated action 与 repeated evidence分开；
- 无新增 evidence 时进入 warning/restricted；
- 允许一次带明确理由的 retry；
- progress guard 不替代 provider/tool error retry。

### 5.4 仍保留的安全 hard caps

- resolved model input budget；
- per-observation / essential envelope cap；
- artifact persistence、terminal raw collection、MCP payload尺寸保护；
- pagination/item cap；
- emergency max turns/tool calls；
- secret/redaction与schema/pairing contract。

### 5.5 完成定义

- 36,083 chars 的历史 envelope 不再单独终止 run；
- current run 可至少跨两个 context windows；
- raw events/artifacts 与 compacted projection均可 inspect；
- 120 万累计 input 不会被误判为单次 context overflow；
- budget 收窄时先限制探索并保留 final answer；
- 重复搜索真实 dogfood 能因 evidence progress 而收口。

## 6. 阶段四：ContextSource Ownership Hard Cut

### 6.1 目标

将非 transcript context 的所有权从 AgentRuntime 中散落的字符串拼接，迁移为结构化 source
candidate。Prompt Cache 只能建立在这个最终 ownership 上。

核心 DTO：

```python
class ContextSourceId(StrEnum):
    SYSTEM = "system"
    RUNTIME = "runtime"
    MEMORY = "memory"
    CAPABILITY = "capability"
    PLAN = "plan"
    RECOVERY = "recovery"
    SUBAGENT = "subagent"

class ContextSectionCandidate(BaseModel):
    candidate_id: str
    source_id: ContextSourceId
    source_fact_ids: tuple[str, ...]
    priority: int
    required: bool
    lifecycle_policy: ContextLifecyclePolicyFact
    lowering_kind: str
    payload: ContextSectionPayload
```

### 6.2 所有权规则

- source 只读取 `ContextFactSnapshot` 的授权 slice；
- source 只产 typed facts/candidates；
- source 不产 provider-native message；
- source 不估算最终 payload；
- source 不读取其他 source 的输出；
- compiler 统一 lifecycle、allocation、timing overlay 与 lowering；
- transcript/tool-result 不伪装成 prose source。

### 6.3 PR 顺序

#### S0：ContextSectionCandidate 与 registry contract

- 固定 source ids、candidate ids、payload union、priority与required语义；
- registry registration/duplicate id hard fail；
- event/Inspector DTO。

#### S1：稳定 source 迁移

- base system；
- runtime context/timing；
- plan/recovery；
- capability catalog。

#### S2：动态 source 迁移

- memory projection；
- subagent handoff/results；
- MCP installed snapshot diagnostics；
- workspace skill active context。

#### S3：统一 allocation/lowering

- lifecycle cache只缓存 source output；
- compile-time timing overlay不污染cache；
- source tokens由同一 estimator计算；
- required source超预算按typed pressure失败。

#### S4：删除旧 ownership

- 删除 component prompt strings；
- 删除 AgentRuntime 中各 source 的直接拼接；
- 删除 legacy section wrappers；
- grep/import architecture guards。

### 6.4 完成定义

- 每个 model-visible non-transcript byte都能归属到唯一 source/candidate；
- Inspector能从 compiled section追到 source fact；
- source registry输出顺序确定；
- 新 source不能绕过 lifecycle/budget/lowering；
- production不存在旧字符串重包装路径。

## 7. 阶段五：Prompt Cache

### 7.1 前置条件

只有以下条件全部满足才开始实现：

- ResolvedModelCall 已完成；
- ContextFactSnapshot 已完成；
- normalized transcript/tool units 已完成；
- Long-Horizon 的 window/projection/compaction identity 已完成；
- ContextSource ownership 已完成；
- provider remote continuation仍 fail-closed 禁用。

### 7.2 实施顺序

#### P0：观测基线

- normalized cached input usage；
- requested target 与 reported model identity分开；
- 不改变 provider payload。

#### P1：Canonical ProviderInputPlan

- 在 `ModelCallStartEvent` 前构造；
- durable carrier固定为 `ModelCallStartEvent.prompt_cache_input`；
- exact provider-visible messages/tools/instructions/options；
- provider input fingerprint覆盖最终 wire semantics。

#### P2：Cache identity 与 break reasons

identity必须包含：

- requested target fingerprint；
- provider request-shape fingerprint；
- system/source ownership versions；
- installed MCP/capability snapshot identity；
- context window id/generation；
- rollup/micro-compaction/LLM compaction projection generation；
- tool schema/render-profile facts；
- permission/exposure中实际影响payload的facts。

reported model identity只做 post-call observation grouping，不进入 pre-send identity。

#### P3：Stable prefix lanes

- stable instructions；
- stable tool catalog；
- append-only durable transcript；
- volatile turn/source tail；
- timing/current user永远在volatile lane。

#### P4：Provider-specific cache controls

- typed allowlist；
- 不允许 remote continuation；
- hint失败降级为普通请求；
- cache miss不触发 runtime mutation。

#### P5：Metrics、Inspector、dogfood

- cache identity/break reason；
- reported cached tokens；
- prefix stability；
- hit/miss不影响结果正确性；
- MCP config change、compaction、permission change回归。

### 7.3 完成定义

- 同一 cache identity 必然对应同一 provider-visible prefix；
- 任意影响 prefix 的 durable fact变化都有稳定 break reason；
- cache hit/miss 不改变事件、工具权限或最终正确性；
- provider未报告usage时显示 missing，不伪造0；
- Inspector不通过当前 runtime重新推断历史 identity。

## 8. 跨阶段关键不变量

### 8.1 Descriptor / binding / provider input

```text
MCP installed snapshot
  -> capability descriptor set
  -> execution binding set
  -> ContextFactSnapshot capability fact
  -> ContextSource candidate
  -> ProviderInputPlan tool schema
  -> Prompt Cache identity
```

任一步的 generation/identity 不一致都 fail closed，不允许“descriptor新、binding旧”或“tool schema旧、cache identity新”。

### 8.2 Durable raw truth 与 projection

- EventLog / artifact 是 raw durable truth；
- ContextWindow、rollup、compaction、section 是 model-visible projection facts；
- Prompt Cache 是 provider optimization observation；
- 三层不能互相覆盖或删除。

### 8.3 时间与 identity

- wall clock 是 observation，不参与可复现事实的随机生成；
- compile timing放volatile lane；
- background MCP完成时间不直接修改 active run；
- cache identity不读取“现在”；
- Inspector显示记录时间，不重新计算历史 age。

### 8.4 Suspend / resume

- suspended run继续使用原 run permission/model call contract；
- MCP pending interaction保留原 binding generation；
- context compaction不跨未完成tool pairing；
- resume前safe point若发现binding被撤销，写typed denial/failure，不静默换server generation。

## 9. 总体验收矩阵

### 9.1 静态

- Ruff；
- import/layering tests；
- grep gates；
- event serialization map completeness；
- Pydantic required-field negative tests；
- no-secret fingerprint tests。

### 9.2 单元与属性测试

- immutable snapshot mutation probes；
- pairing/order property tests；
- MCP epoch race / stale completion；
- context allocation determinism；
- projection rewrite idempotency；
- cache identity canonicalization。

### 9.3 故障注入

- optional MCP connect/discovery hang；
- required deadline；
- config disable while worker completes；
- close during connect/discovery；
- compaction commit acknowledgement lost；
- rollup/summary artifact write failure；
- provider cache hint rejection；
- old schema replay hard failure。

### 9.4 Real LLM / REPL dogfood

- slow optional MCP不阻塞REPL banner；
- MCP ready后下一safe point可用且同snapshot执行；
- 40+ tool observations不死于36K固定cap；
- same-run跨window继续任务；
- budget收窄后产出final answer；
- Prompt Cache hit/miss trajectory语义一致。

## 10. 文档生命周期

- `PULSARA_MCP_STARTUP_LATENCY_NOTE.zh.md`：保留为问题与实测记录；
- `PULSARA_MCP_STARTUP_LATENCY_HARD_CUT_IMPLEMENTATION.zh.md`：阶段一唯一实施规格；
- `ARCHITECTURE_DEBT_AUDIT.zh.md`：记录阶段二、四的债务来源；
- `PULSARA_LONG_HORIZON_BUDGET_PRIOR_ART_RESEARCH.zh.md`：阶段三研究输入，实施前另写hard-cut规格；
- `PULSARA_PROMPT_CACHE_CONTRACT.zh.md`：阶段五产品契约，实施前按阶段二至四最终DTO校准。

每个实施文档在对应阶段完成后移入 `archived_docs/`；总路线在五阶段完成前保持根目录可见。
