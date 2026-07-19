# Pulsara 下一阶段六步 Hard-Cut 总路线

> 状态：历史总路线与跨阶段背景；ContextSource之后的实施顺序已由新的两阶段规格取代。
> 基线：ResolvedModelTarget / ResolvedModelCall hard cut 已完成；Subagent graph reducer hard cut 已完成。
> 进度：阶段一 MCP Startup Latency、阶段二 Host Run-Boundary Safe Point、阶段三 Context Compiler Input Hard Cut、阶段四 Long-Horizon Context Windows 已完成；下一实施章改为 `PULSARA_CONTEXT_SOURCE_AND_INCREMENTAL_PROVIDER_INPUT_HARD_CUT_IMPLEMENTATION.zh.md` 中的两阶段 hard cut。
> 阶段二已通过归档后 durable-ownership 复审：RunStart/RunEnd confirmation、boundary payload conflict、迟到 compaction commit owner、frozen execution truth、Prepared carrier、child native evidence与deferred-borrow handle retirement均已补齐。
> 原则：项目尚未上线，不为旧 event、旧数据库、旧 constructor 或旧 runtime facade 保留生产兼容路径。
> 文件名说明：为保持既有链接稳定，文件仍保留 NEXT_FIVE；正文路线已正式扩展为六阶段。

## 0. 结论

原路线将工作拆成六个相互依赖的章节：

1. **MCP Startup Latency Hard Cut（已完成）**
2. **Host Run-Boundary Safe Point Hard Cut（已完成）**
3. **Context Compiler Input Hard Cut（已完成）**
4. **Long-Horizon Context Windows（已完成）**
5. **ContextSource Ownership Hard Cut（实施顺序已取代）**
6. **Prompt Cache（实施顺序已取代）**

当前权威实施顺序为：

```text
Append-aware ContextSource Ownership Hard Cut
-> Incremental ProviderInput Generation Hard Cut
-> provider-specific cache observation/hints（后置）
```

依赖关系如下：

```text
ResolvedModelCall（已完成）
        │
        └── MCP Startup Latency Hard Cut
               │
               │  background discovery + installable candidate
               │
               └── Host Run-Boundary Safe Point Hard Cut
                      │  PRE_RUN / PRE_INTERACTION_RESUME
                      │  durable commit / activation / failure ownership
                      │
                      ├── CommittedHostRunEntry
Subagent RunEntry ────┴── CommittedRunEntry
                             │
                             └── Context Compiler Input Hard Cut
                                    │
                                    └── Long-Horizon Context Windows
                                           │  context-window / rollup / compaction identity
                                           │
                                           └── Append-aware ContextSource Ownership Hard Cut
                                                  │
                                                  └── Incremental ProviderInput Generation Hard Cut
                                                         │
                                                         └── provider cache observation/hints
```

Incremental ProviderInput不能越过第 2、3、4 步与append-aware ContextSource contract直接实施。否则 input identity 会把 mutable
`LoopState`、旧字符串包装 facade、未稳定的 tool-result rollup 或临时 section ownership
误当作长期输入契约。

## 1. 为什么从五步扩展为六步

MCP hard cut完成后，代码真值暴露出一个不能继续隐含在HostSession大函数中的前置层：
`ContextFactSnapshot`可以冻结“compiler读什么”，但它不能自行决定一个run何时已经合法创建、
resume何时已经合法继续，也不能解释MCP installation、preflight compaction、permission/model target
与RunStart commit之间的线性化关系。

因此新增独立阶段：

- **Host Run-Boundary Safe Point Hard Cut**：稳定PRE_RUN/PRE_INTERACTION_RESUME的admission、
  prepare、durable commit、activate、failure ownership与Inspector join；
- **Context Compiler Input Hard Cut**：消费已经committed的boundary facts，稳定compiler输入；
- **ContextSource Ownership Hard Cut**：稳定非transcript section的producer ownership。

三者不能合并：

- boundary contract解决“run/continuation何时存在、哪些上游事实已冻结”；
- compiler input解决“单次model call看到哪些immutable facts/units”；
- source ownership解决“谁能生产哪些non-transcript candidates”。

它们可以连续实施，但不能跳过任一层便开始Long-Horizon或Prompt Cache。

## 2. 全局 Hard-Cut 规则

以下规则适用于六个阶段的每个 PR。

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
- PREPARING / COMMITTED / ACTIVE 必须是不同状态；durable commit前不得发布active run。

### 2.4 Architecture guards

至少维护以下静态 guard：

- production compiler 不 import / 接收 `LoopState`；
- production 不构造 `ContextCompileInputs`；
- production MCP open/turn/resume 不调用同步远端 `sync_servers()`；
- Host new-run/live-resume入口只能调用typed run-boundary pipeline，不得各自内联MCP/target/compaction/audit顺序；
- AgentRuntime不得创建RunStart；Host/Subagent entry driver分别拥有各自branch；
- RunStart current user不得使用metadata fallback；
- primitives不得import runtime permission；preset mapping只有primitives.permission一个真源；
- production resume不得以空user input、空active skills重新解析capability exposure；
- ContextSource 不返回预渲染 provider message 或任意字符串 facade；
- ContextSource只接收source-specific discriminated input，不得import完整 `ContextFactSnapshot`；
- provider tool definitions由独立 `CapabilityToolCatalogRootFact`拥有，不得混入capability prose source；
- source canonical revision不读取ProviderInput generation head；generation supersession只归pure append planner/reducer；
- ProviderInput跨parent/child authority必须使用per-ledger horizons，禁止裸single high-water；
- committed ProviderInput core不包含ModelStart/ContextCompiled/close event refs；event refs只进入独立attribution envelope；
- prepared append ownership与committed core物理分离，ModelStart guard同时CAS两者；
- ModelStart只保存bounded authority-horizon/replay-binding set roots，不重复完整cross-ledger tuple；
- LLMRuntime继续是ModelStart与完整model lifecycle唯一writer；generation coordinator只准备stable companions与guard；
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

## 4. 阶段二：Host Run-Boundary Safe Point Hard Cut（已完成）

### 4.1 目标

只统一Host拥有的两个run boundary：

    PRE_RUN
    PRE_INTERACTION_RESUME

最终语义：

- Host API ingress立即记录user observation与boundary identity；
- lock内authoritative admission/reconciliation检查早于MCP/compaction mutation；
- PREPARING、DURABLE_COMMITTED、ACTIVE是不同状态；
- required MCP先await、后install；
- preflight使用explicit transcript source high-water；
- model target、permission、MCP installation与capability resolve basis只有一个freeze点；
- RunStart与pending MCP audits原子commit；
- commit acknowledgement后才activate；
- resume从原RunStart重建target与permission；
- continuation exposure只允许reuse或monotonic narrowing，不允许widen；
- exposure reuse基于完整CapabilityExposureSemanticFact（descriptor/binding + catalog + active-skill projections），MCP installation id只作attribution；
- Host与subagent分别拥有RunStart commit，但统一产出CommittedRunEntry；
- RunStart.current_user_message是typed durable truth，不再使用metadata fallback；
- approval/plan/MCP使用interaction-specific gate policy；
- durable commit await被cancel或抛BaseException时，必须用stable IDs + confirm_batch判定none/full/partial/unknown；
- host/user plan force-exit无pending时是workflow mutation，不伪造active run；
- Started compaction在cancel/recovery后有stable terminal fact或明确close blocker；
- RunStart full commit后所有execution异常由统一stable RunEnd terminalizer收口；
- streaming使用lossless bounded backpressure，stop/close先detach observer；waiter cancellation不等于stop；
- Inspector从typed boundary facts解释pre-run/resume顺序。

本阶段不创建通用SafePointCoordinator，不纳入POST_TOOL、MID_TURN_COMPACTION、POST_RUN或CLOSE。
Durable SESSION_REOPEN也保持独立协议。
Subagent child不执行Host safe point；RB2只因RunStart schema与下游ContextFactSnapshot统一而同步落
SubagentRunEntryDriver/CommittedSubagentRunEntry。

### 4.2 关键DTO与状态

    HostRunBoundaryKind
    HostRunBoundaryPhase
    HostRunBoundaryDisposition
    NewRunBoundaryInput
    PreparedNewRunBoundary
    CommittedNewRunBoundary
    PreparedInteractionResumeBoundary
    CommittedInteractionResumeBoundary
    CommittedHostRunEntry
    CommittedSubagentRunEntry
    CommittedRunEntry
    CommittedRunExecutionOwner
    CurrentUserMessageFact
    RunEntryFact
    CapabilityResolveBasisFact
    CapabilityExecutionSurfaceIdentityFact
    CapabilityExposureSemanticFact
    CapabilityExposureSnapshotFact
    PlanWorkflowStateFact
    RunWorkingSet
    HostRunBoundaryBlocked
    BoundaryBatchConfirmation
    DurableRunExistence
    CapabilityExposureResolvedEvent
    RunInteractionResumeBoundaryEvent

固定pipeline：

    ingress
      -> admission
      -> prepare
      -> durable commit
      -> activate
      -> post-commit initialization / interaction router

Failure disposition至少区分：

    PROCEED
    PROCEED_DEGRADED
    RETRYABLE_BLOCK
    TERMINAL_BLOCK
    SESSION_LATCHED
    COMMIT_OUTCOME_UNKNOWN
    COMMITTED_BUT_PUBLICATION_FAILED
    COMMITTED_EXECUTION_FAILED

### 4.3 PR顺序

#### RB0：Characterization与低层contract hard cut

- boundary enums/facts/events；
- low-level permission primitive与唯一preset mapping；
- CurrentUserMessageFact、Host/Subagent run-entry facts与CommittedRunEntry union；
- standalone RunStart host/subagent branch fact/validator；
- typed descriptor/binding/projection/exposure semantic facts与blocked result DTO；projection entry/fragment具有provider-aware
  stable identity；
- CapabilityExecutionSurfaceProvider.snapshot_descriptors与CapabilityProjectionProvider.resolve_projection split contract；
- low-level RunStopReason、ChildResultRenderPolicyFact、ChildNativeTerminalReferenceFact与ChildResultHandoffFact；
- serialization/negative schema tests；
- permission primitive/import layering一次性hard cut；不改变RunStart production schema或production trajectory。

#### RB1：Boundary attempt owner与admission kernel

- PREPARING lifecycle；
- boundary task cancel/drain；
- stable durable-run owner与generation/CAS active-segment owner；suspend完成segment但不完成run；
- process-local RunTerminationIntent与segment activation owner kind/id；
- stop/close先CAS安装user_stop/host_teardown termination intent，再取消matching segment；
- install_segment/swap同时检查terminal state与intent，suspended stop可阻断post-commit resume activation；
- segment registry接收driver factory，先安装owner再创建Task；
- ordinary-def stream ingress owner与PREPARING stop；
- lossless backpressure、observer detach与run_turn waiter-cancel contract；
- lock内revalidation；
- reconciliation precheck；
- close during MCP wait/compaction；
- 禁止participant registry。

#### RB2：PRE_RUN纵向hard cut

- ingress timing；
- target/permission pure resolve；
- recovery maintenance；
- MCP prepare/required/install；
- transcript snapshot/preflight correlation；
- compaction events通过现有RuntimeSession writer，不再direct append后补publish；
- AgentRunDraft；
- RunStart schema与所有constructors在本PR一次性hard cut；
- RunEnd stable terminal id/terminalization kind同步hard cut；
- 保留详细typed RunStopReason，不压成泛化error；
- required typed current_user_message与metadata replay fallback删除；
- HostRunEntryDriver和SubagentRunEntryDriver同时迁移，产出CommittedRunEntry；
- child task_id nullable branch、RunStart NONE/FULL/UNKNOWN/PARTIAL parent graph/handle/capacity矩阵；
- child WAITING_USER先稳定提交child RunEnd，再以ChildNativeTerminalReferenceFact提交parent failure；
- child terminal→parent graph deterministic-id crash repair；
- explicit result复用durable submission；inferred result使用versioned deterministic ChildResultHandoffFact和幂等artifact；
- ChildResultRenderPolicyFact冻结renderer version、summary cap、artifact-ref cap并与parent budget snapshot一致；
- deterministic artifact使用metadata-aware put-if-absent-or-confirm-identical CAS；
- explicit submission必须有可跨child ledger验证的report_agent_result call/result evidence；
- live child owner reconcile与SESSION_REOPEN ownerless recovered-interrupted repair分流；
- child仅在真实在途tool/MCP call持borrow，不引入child-lifetime lease；
- RunStart+audit commit；
- pre-RunStart execution-surface snapshot与post-RunStart projection provider hard cut；context-dependent descriptor provider
  禁止；
- initial typed exposure/basis/RunWorkingSet与fragmentized catalog/active projection production path并删除旧CustomEvent；
-统一committed execution RunEnd terminalizer；
- active-after-commit；
- BaseException/cancellation commit confirmation与post-commit recovery；
- compaction terminalization ownership；
- 无pending plan force-exit workflow mutation。

#### RB3：PRE_INTERACTION_RESUME纵向hard cut

- pending/suspended identity；
- target+permission RunStart rebuild；
- continuation MCP/audit；
- exposure reuse/narrowing；
- full authorization/catalog/active-skill semantic comparison与built-in/skill/custom revoke narrowing；
- derive_continuation_basis替换owner/current MCP/surface但保留original user/transcript/skill basis；
- continuation只对provider-aware entry identity和original rendered fragments求交，禁止重渲染产生detail widening；
- empty container/projection wrapper随最后entry删除，独立provider语义必须拥有source entry；
- approval/plan/MCP gate matrix；
- Supervisor-owned pending lease failure semantics；
- continuation FULL commit后同步CAS swap execution handles；old handles只按真实parent/child tool-call borrow barrier退休；
  MCP pending lease独立由Supervisor保护exact slot/manager；
- 在existing committed run owner上安装新generation segment并接入router terminalizer；
- suspended plan force-exit走resume boundary。

#### RB4：Inspector/replay与projection

- boundary/continuation timeline；
- Host/subagent run-entry与exposure owner projection；
- compaction boundary join；
- live PREPARING status；
- commit outcome unknown/compaction terminalization/host workflow mutation projection；
- contract/replay migration。

#### RB5：Deletion gates、fault injection与dogfood

- 删除HostSession/AgentRuntime旧内联编排；
- architecture/grep guards；
- full pytest/Ruff；
- real LLM、MCP、plan、subagent、compaction dogfood。

### 4.4 完成定义

- new run/live interaction resume只有两条typed pipeline；
- known reconciliation不会在MCP/preflight后才被发现；
- boundary preparation有close可drain的owner；
- durable commit前不出现active run；
- RunStart post-commit publication failure不会成为无人拥有的dangling run；
- cancellation/任意BaseException后的commit outcome由stable batch confirmation裁决；
- resume不会信任mutable LoopState permission；
- old run exposure不会因MCP、built-in、skill或custom surface变化而扩权；
- projection同名同内容但provider/source identity改变时仍视为撤销+新增，不错误保留旧fragment；
- authorization、catalog与active-skill projection共同决定exposure semantic；
- Host/subagent都拥有合法run-entry driver，AgentRuntime不再隐式创建RunStart；
- current user text由RunStart typed fact持久化并可replay；
- UNKNOWN/PARTIAL run existence不再压成bool；
- post-commit任意异常由stable RunEnd terminalizer收口；
- Prepared facts不持有live MCP manager/tool/lease；
- observer backpressure与waiter cancellation语义固定；
- PermissionMode/preset mapping已下沉到低层primitive；
- host plan force-exit不会绕过RunStart contract伪造active run；
- compaction cancellation不会留下无owner的Started；
- preflight compaction可join到触发它的新run boundary；
- ContextFactSnapshot builder可直接消费committed boundary facts；
- Host/subagent都通过CommittedRunEntry进入AgentRuntime与ContextFactSnapshot；continuation作为optional附加boundary。
- durable run lifetime与一次ACTIVE segment lifetime已分离，resume不会复用旧driver/Future/observer；
- suspended stop/close intent可线性化阻断post-commit resume segment，不会在terminating run上启动新driver；
- child native terminal与parent graph terminal可跨ledger验证并在崩溃后幂等补齐；
- inferred child completion的result/artifact/summary/payload可由frozen render policy确定性重建；
- child result caps同renderer version一起成为fingerprinted run-entry policy；artifact retry同时校验semantic metadata；
- live child reconcile与ownerless SESSION_REOPEN recovery不会互相覆盖；
- child lifetime本身不阻止MCP slot退休；execution handle只等待真实in-flight parent/child borrow，MCP slot则只等待
  Supervisor账本中的active borrower与pending lease；
- resume execution-handle swap不触碰Supervisor-owned旧MCP pending lease，也不在post-swap failure后回滚到旧surface。
- initial/resume execution handles在boundary commit前冻结，Agent exposure、working set与tool binding消费同一surface；
- Compaction Started取消后迟到commit仍有service-owned pending commit与bounded terminalization authority；
- confirmed owner因deferred borrow暂缓退休时，在最后borrow归零后自动从registry删除；
- CurrentUserMessageFact是UserMsg与RunStart user chars的唯一输入真源；child-owned generation-aware authority只记录
  真实在途tool call，dependency scheduler与immediate child共享同一child runner安装路径。
- child closing handle在deferred borrow归零后由exact-generation callback完成release；sync tool的borrow与run/close barrier
  持续到真实worker thread结束，而不是awaiting coroutine被取消的时刻。

详细实施规格：

`archived_docs/PULSARA_HOST_RUN_BOUNDARY_SAFE_POINT_HARD_CUT_IMPLEMENTATION.zh.md`

实施闭环：RB0–RB5 已完成 typed PRE_RUN/PRE_INTERACTION_RESUME、Host/subagent CommittedRunEntry、
stable run/segment ownership、fragment-only capability narrowing、deterministic child handoff、Inspector/replay、
fault injection、全量 pytest/Ruff 与必要 real-LLM approval/plan/subagent/compaction dogfood。路线当前指针移至第5节。

## 5. 阶段三：Context Compiler Input Hard Cut（已完成）

详细实施规格：

- [PULSARA_CONTEXT_COMPILER_INPUT_HARD_CUT_IMPLEMENTATION.zh.md](/Users/plumliu/Desktop/python_workspace/pulsara_agent/PULSARA_CONTEXT_COMPILER_INPUT_HARD_CUT_IMPLEMENTATION.zh.md)

### 5.1 目标

把 context compile 的输入冻结为不可变事实，而不是把整个 agent working state 交给 compiler。

核心 DTO：

```python
class ContextFactSnapshot(BaseModel):
    runtime_session_id: str
    run_id: str
    turn_id: str
    reply_id: str
    run_entry: RunEntryFact
    continuation_boundary: InteractionResumeBoundaryFact | None
    current_user_message: CurrentUserMessageFact
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
    essential: ToolResultEssentialFact | None
```

上面只表示ownership轮廓；字段与validator的唯一实施真源是详细规格。详细规格已经进一步冻结：canonical
`FrozenStoredEvent` slice、`ContextSnapshotBuildInput` pure builder、raw malformed tool arguments、durable terminal
essential、完整resolved render/candidate policy、`PreparedToolResultRenderInput`与`PreparedContextCandidateSet`。

ownership 不再开放讨论：

- live snapshot collector required接收`CommittedRunEntry`（CommittedHostRunEntry或CommittedSubagentRunEntry）与
  optional latest `CommittedInteractionResumeBoundary`，再读取本model step的typed working facts；
- continuation不是新run entry；child不需要伪造Host boundary；
- compiler只读snapshot / transcript / prepared render input / prepared candidate set；
- tool-result pairing 在进入 compiler 前已结构化；
- renderer只返回per-unit fragments；最终`LLMMessage`、assistant tool-call/result sequencing与message order只由compiler lowering；
- compiler 不通过 scratchpad 找 cache、span 或 fallback message。
- boundary result不是ContextFactSnapshot本身；resolved call、memory、subagent、tool-result与compile
  timing仍按每次model call加入。

### 5.2 PR 顺序

#### C0：低层 DTO 与 schema contract

- 新增 immutable `ContextFactSnapshot`；
- 新增 `TranscriptCompileInput`；
- 新增 normalized `ToolResultRenderUnit`；
- 新增完整resolved render/candidate policy与prepared ingress DTO；
- 新增 fingerprint/version；
- event-visible DTO 放低层 primitives/contracts，避免 event/runtime 反向依赖。

#### C1：Snapshot builder

- live collector从committed boundary/working set构造`ContextSnapshotBuildInput`；replay collector只从durable
  facts/manifest构造同形状input；pure builder不接收`RunWorkingSet`；
- canonical authority slice保存同一high-water下的immutable stored-event bytes，不保存mutable`AgentEvent`引用；
- transcript projection window单独选择summary/retained history/protected current run，compaction不再决定authority起点；
- stored-event wrapper identity与canonical bytes由唯一factory双向验证；
- 在每次model call前组合resolved call与current typed working facts；
- 所有 mutable dict/list 递归 freeze；
- snapshot 记录 source event ids/sequences；
- current user、tool timing、permission、resolved call 不做二次推断。
- current user只从RunStartEvent.current_user_message读取，不从metadata或process-local input重造。
- TranscriptCompileInput.current_user_anchor必须等于该typed message id，文本/hash/timing必须完全一致。
- 不允许重新读取session default、live MCP supervisor或scratchpad capability exposure。
- snapshot冻结完整`candidate_authorities`：正文hash/chars、event/artifact refs、priority/required/stability、channel/lowering与
  dependency fingerprint；candidate prepare/compiler均做强join，合法artifact ID不能掩护伪造inline正文。
- authority同时持有唯一model-visible正文、source timing与selection facts；collector只消费authority，不接受并行字符串。
  memory/subagent从canonical projection/graph events重建正文并使用真实event created-at/sequence。

#### C2：Transcript 与 tool-result normalization

- assembler/reducer 产出 provider-neutral typed transcript；
- call/result pairing、provider-native assistant tool call、external result 全部规范化；
- malformed/non-object tool arguments保留raw provider string并与error result配对；
- render profile、terminal command/process/inventory essential required durable carrier；
- descriptor声明allowed render variants与versioned declarative semantics builder contract，terminal_process的
  inventory/observation/error形态由本次event保存的actual profile表达；
- render variant同时冻结allowed result states、execution phase与terminal-payload timing requirement；
- composition root通过immutable `ToolResultSemanticsBuilderRegistry`绑定builder ID/version/declarative contract与callable；
- descriptor、registry binding与External requirement必须精确匹配builder ID/version/contract fingerprint；process-local
  implementation build fingerprint只用于当前进程诊断，不进入任何durable或semantic identity；
- pre-execution permission/exposure/policy deny必须消费原descriptor的typed denial semantics；
- normal/deny/workflow/MCP/external全部实际调用同一个resolved semantics builder；unknown只允许descriptor缺失；
- terminal command的executed essential与pre-execution command-error essential分离，deny不伪造cwd/session/backend；
- actual profile保存render-contract/variant fingerprints与original exposure/descriptor durable attribution；
- external execution requirement冻结descriptor contract与essential capture policy，result通过typed ingress builder回指
  original requirement；delayed completion只消费原requirement policy，不读取当前system policy；
- external caller只提交result/timing/variant/domain payload，descriptor/exposure/fingerprints由builder注入；
- tool-result renderer 接收 resolved estimator 与 render units，只返回fragments/canonical decisions，不返回pre-lowered messages；
- result ref/pair/unit/fragment四方identity与message/block/global position必须完全一致，跨call pairing fail closed；
- observation/terminal timing inclusion由renderer显式flags传递，禁止扫描工具正文推断timing是否已渲染；
- 删除 compiler 前的第二套 chars/4 估算真源。

#### C3：Compiler API hard cut

生产 API 只接受：

```python
compile_context(
    *,
    facts: ContextFactSnapshot,
    transcript: TranscriptCompileInput,
    tool_results: PreparedToolResultRenderInput,
    section_candidates: PreparedContextCandidateSet,
) -> CompiledContext
```

`ContextSectionCandidate` 的最小 typed shape 在 C0 一并定义，以保证 C3 没有临时 API 空洞。
C 阶段允许现有 AgentRuntime collector 把各子系统事实转换为 candidate，但禁止再传裸 component
string；S 阶段再把这些 producer 的 ownership 迁入正式 source registry 并删除 collector facade。

- 删除 `ContextCompileRequest.state`；
- 删除 production `ContextCompileInputs`；
- 删除 legacy current-user fallback；
- 删除 scratchpad render-cache fallback。
- candidate required/priority成为唯一allocation顺序真源；system candidate不再隐式must-keep；
- 固定source/channel/lowering矩阵由schema与compiler共同消费；lifecycle后、budget前运行pure timing overlay，所有section保存
  structured timing，memory/subagent使用各自freshness；
- session-owned render cache采用prepare-read、durable compiled FULL后commit-write的两阶段协议；
- render cache只接纳full-visible/full-envelope/within-budget/payload-preserved结果；cache failure仅是operational diagnostic；
- 多artifact compact/minimal envelope与decision共享同一text-like primary；binary不冒充primary，非primary无read_more；
- candidate lifecycle cache改为entry count与aggregate chars双上界LRU，并暴露bounded eviction/oversized-skip metrics；cache read
  exception与普通miss生成同一durable candidate-set/manifest fingerprint；
- pre-manifest所有preparation stage失败都写typed input-failure audit（ledger untrusted除外）；
- static-instruction artifact写入与PostgreSQL event-slice读取通过RuntimeSession-owned bounded I/O owner执行；
- manifest未确认时failed event只使用candidate-aware input failure，full audit只能引用confirmed artifact；
- manifest write cancellation/unknown由session-owned generation/CAS state machine、shielded waiter与bounded drain收口；
- logical generation与physical DB operation分离，old blocking writer必须被保留到真实EXIT，之后再final confirm。
- logical availability、physical drain与post-terminal verification是三个正交状态，迟到conflict进consistency latch
  而不反向改写已完成Future。

#### C4：Replay / recovery / Inspector

- replay 从 durable event 重建同形状 snapshot；
- live/replay compile fact equality；
- Inspector 展示 snapshot id、source sequences、normalized unit counts；
- Inspector分开展示historical durable builder semantic contract与当前进程implementation build diagnostic，后者不参与
  replay verdict；
- schema 缺失直接 contract error。

#### C5：删除旧 facade

- 删除 `build_llm_context()` / `msg_to_llm_messages()` production path；
- 删除所有旧 constructor；
- grep gate；
- 全量 real-LLM、plan、MCP resume、subagent、compaction 回归。

### 5.3 完成定义

- production compiler 无法访问 `LoopState`；
- compiler只接收已durable-committed的run/continuation boundary；
- compile 输入可稳定序列化、fingerprint 和 replay；
- normalized transcript 保留 pairing/order；
- tool-result renderer 与 final estimator 使用同一个 resolved call；
- `ContextCompiledEvent` 可 join 到 snapshot 与 source sequences；
- 同一 snapshot + 同一 compiler version 必须得到同一 provider-neutral compiled payload。

### 5.4 明确移交阶段四的成本：subagent graph 从 sequence 1 全量 fold

阶段三为先保证 correctness 与 exact replay，冻结了一个有意保守的 V1 合约：每次 live compile 与 replay 都从同一份
parent canonical event slice 派生 subagent candidate selection；在尚无 durable graph checkpoint 时，该 source range 必须是
`sequence 1..source_through_sequence`。Pure reducer由这份冻结 slice 一次性产生 ordered eligible IDs，再按同一 frozen policy
切分 selected/omitted。Selection 保存真实 `source_from_sequence/source_through_sequence`，不得先读 process-local graph、再绑定
较晚 EventLog high-water，也不得为了缩短查询范围漏掉旧的 pending result。

这个选择解决的是事实一致性，不是最终性能方案。它意味着：

- 每次 model compile 可能读取、解码并 fold 整个 parent session ledger；
- exact replay / Inspector 需要读取同一全量 graph source range；
- session 越长，context preparation 的 I/O、CPU、内存与 replay latency 越接近 O(session event count)；
- authority range可能被旧的 pending/omitted subagent facts拉回 sequence 1，即使model-visible transcript已经compacted。

因此这是一项**已知且被阶段三验收允许的过渡成本**，不是可以长期保留的 Long-Horizon 行为，也不能被误报成阶段三
compiler purity缺陷。阶段四必须将它作为正式 hard-cut 工作项，而不只是顺手优化：

1. 引入durable、versioned、fingerprinted subagent graph reducer memoization，并冻结
   `graph_reducer_id + graph_reducer_version + graph_reducer_contract_fingerprint`；
2. 将authority拆成`SubagentGraphSemanticSourceFact`：只覆盖runtime、按ledger order折叠的graph-domain event count/semantic accumulator、
   reducer contract与最终graph state fingerprint；graph-event semantic payload使用剥离storage sequence/wrapper的versioned canonical projection，
   最终semantic state也不含物理sequence/high-water；selection/candidate/snapshot/input semantic identity只消费该fact；
3. 将运行加速拆成`SubagentGraphAccelerationFact`：checkpoint ID/high-water、全ledger continuity accumulator、delta range/count只进入
   manifest operational audit、Inspector与metrics，不进入模型输入semantic fingerprint；
4. live与replay必须消费同一checkpoint + bounded contiguous delta restore；不同checkpoint schedule、artifact GC或compatible rebase只要
   最终semantic source与selection相同，就仍是同一semantic identity/exact replay；
5. production hot path只允许checkpoint + bounded delta；仅从未有checkpoint的新session可在bootstrap cap内从sequence 1构建并FULL
   confirm首个checkpoint，不能先full fold继续compile；
6. privileged offline doctor可从canonical EventLog无界full fold、验证/重建checkpoint，但compiler、live replay、resume与Inspector不得
   隐式调用；
7. ledger continuity accumulator覆盖全部canonical events；graph semantic accumulator只覆盖当前contract支持的graph-domain events。
   已声明non-graph/checkpoint event只进入continuity；未来graph-domain但当前contract不支持的event fail closed；
8. checkpoint artifact是可丢弃cache，不由旧manifest永久pin；exact replay优先原checkpoint，但原candidate缺失、冲突或delta超bound时，
   可使用任意不晚于目标high-water、contract-compatible且bounded的checkpoint rebase；无bounded路径时交给offline repair；
9. V1不做online physical GC；doctor/GC只允许closed/quiescent session并持有PostgreSQL advisory maintenance lock；
10. `cap=0`、no eligible、全部omitted、跨window pending result与result consumed/delivered边界必须继续保持当前selection语义。

冻结结论：Checkpoint是可持久化、可丢弃、可重新计算的reducer memoization；EventLog与版本化reducer始终是唯一authority。

阶段四实施规格必须给出checkpoint创建/推进/retention/rebase/recovery owner、PostgreSQL读取边界、reducer contract、production hot
path与offline repair隔离、Inspector projection及负向测试；只把`minimum_sequence`改大、只复用live reducer state或增加无durable
identity的内存cache均不算完成。

## 6. 阶段四：Long-Horizon Context Windows

### 6.1 目标

一个 user run 可以跨多个 bounded model-visible context window 持续推进；durable EventLog 与 artifact
不被删除，只有 compiled projection 被 rollup、micro-compact 或 LLM compact。

研究输入：

`PULSARA_LONG_HORIZON_BUDGET_PRIOR_ART_RESEARCH.zh.md`

详细实施规格：

`PULSARA_LONG_HORIZON_CONTEXT_WINDOWS_HARD_CUT_IMPLEMENTATION.zh.md`

本节只保留阶段边界与路线摘要；DTO、identity、owner、commit/recovery、PR与测试口径以详细实施规格为准。研究文档继续作为
prior-art输入，不再承担生产契约。

### 6.2 必须冻结的身份

```python
class SubagentGraphSemanticSourceFact(BaseModel):
    runtime_session_id: str
    graph_event_count: int
    graph_semantic_accumulator: str
    graph_reducer_id: str
    graph_reducer_version: str
    graph_reducer_contract_fingerprint: str
    graph_state_semantic_fingerprint: str

class SubagentGraphAccelerationFact(BaseModel):
    checkpoint_id: str
    checkpoint_through_sequence: int
    checkpoint_ledger_continuity_accumulator: str
    delta_from_sequence: int
    delta_through_sequence: int
    delta_count: int
    ledger_through_sequence: int
    ledger_continuity_accumulator: str

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
        "full", "preview", "essential", "artifact_locator", "rollup_member", "pair_stub"
    ]
    projection_generation: int
    reason_code: str

class RolloutBudgetStateFact(BaseModel):
    phase: Literal[
        "exploration", "warning", "restricted", "finalization_only", "exhausted",
        "emergency_hard_stop"
    ]
    charged_milliunits: int
    reserved_milliunits: int
    finalization_reserve_milliunits: int
```

第一类进入selection/candidate/snapshot/input semantic identity；第二类只进入manifest operational audit与Inspector，不参与provider-visible
payload identity或exact replay semantic equality。

### 6.3 PR 顺序

#### L0A/L0B：checkpoint；预算、window、rollout DTO 与 typed events

- active context、observation projection与rollout分开；L0B接入最终action classification/cost、account/window reducer与
  `model_visible=False` exact-recurrence Inspector shadow，但不执行phase gate、不创建provider candidate；L5只激活model-visible hint与gate，
  不另写第二classifier/recurrence算法；
- window identity、projection generation、rewrite reason；
- generation>1 window使用durable normalized retained-transcript baseline，并把context authority拆为post-compaction primary delta与
  RunStart/capability/plan/memory/rollout exact/sparse named ranges；provider context缩短后compiler CPU、内存与physical read horizon必须同步缩短；
- 阶段四保证compile成本受active-window与named-fact caps约束，不再随完整session增长；V1不把该保证误写成incremental transcript
  reducer。未来的`O(new delta)`优化只能是canonical input之上的可丢弃memoization，不能成为第二事实源；
- 所有production async event commit经RuntimeSession-owned FIFO writer；Host/process只共享bounded blocking executor与PostgreSQL connection pool，
  session仍独立拥有serialization、physical-operation tracking、reconciliation与close drain；
- event write attempt使用单一absolute deadline覆盖queue、commit与stable confirmation；queued expiry主动CAS返回NONE，physical-start之后由critical
  owner返回typed FULL/NONE/UNKNOWN。Host/resume/RunEnd/compaction/MCP等async外层不得同步调用confirmation或重置deadline；
- prepared rollup cache由durable rollup/member/placement basis与policy/estimator/carrier contract定址，不用完整transcript fingerprint；
- durable subagent graph reducer contract与checkpoint memoization；reducer ID/version/contract fingerprint精确join；event domain按历史
  type/schema version不可变绑定，无关non-graph event新增不漂移旧run；
- EventLog row使用per-event schema version/fingerprint/domain identity；production graph/replay通过pre-union raw envelope snapshot读取，不能先用
  当前AgentEvent union解析旧payload；
- semantic source identity与checkpoint/delta acceleration attribution彻底分离；
- selection从sequence-1 full fold迁移为checkpoint + bounded contiguous delta；checkpoint生产、推进、retention、rebase与
  Inspector attribution必须有单一owner；
- production只允许bounded bootstrap/hot path；无界full fold只存在于privileged offline doctor；
- V1不做online checkpoint physical GC；privileged maintenance只在closed/quiescent session与PostgreSQL advisory lock下运行；
- model/tool/child使用最终reservation/settlement算法与session-scoped commit ports；model reservation由pre-margin input physical bound与output
  cap生成唯一quote；LLMRuntime通过split start/semantic/terminal stream port持久化完整stream，并返回service-owned execution handle；独立worker
  驱动可取消的typed transport execution，committed semantic event保存call/start/index/draft attribution；public subscription只供UI观察，observer
  detach/lag不取消worker，Agent/direct/window控制侧在terminal FULL后从EventLog materialize完整call result；awaitable cancel signal负责打断blocked read，
  physical operation未退出时close fail closed；所有provider binding由central sanitizing transport包装，raw SDK/HTTP/network exception只在wrapper task内
  转成typed error+terminal drafts，禁止exception旁路；只有整个terminal outcome为completed时closed tool call/text才可进入后续gate，其他outcome全部
  audit-only；completed main call还必须在tool/reply前FULL commit durable control disposition，stop intent与accepted disposition共用run-control lock；
  锁内只做ledger confirm、deterministic fold与permit CAS，ordered publisher/observer notification严格在锁外；observer failure不撤销durable winner或permit；
  ModelStart recovery plan先冻结由durable activation owner + segment generation组成的event-safe run activation，disposition/reopen复用该fact，随机
  process-local segment ID只用于live ABA guard且不得进入durable identity；
  SESSION_REOPEN使用独立recovery guard，以Start-frozen activation + canonical high-water CAS验证disposition/downstream仍为空；永不伪造live segment
  ID或生成permit，late durable winner与downstream race通过重扫矩阵收口；ModelStart同时冻结versioned downstream predicate contract，五类RunEnd及
  `CapabilityGateDecisionEvent`、tool reservation、`ToolExecutionSuspendedEvent`、`ToolResultEndEvent`都必须验证在先disposition；V1 registry
  就是这些已有schema、writer和recovery规则事件变体的封闭集合，不预留尚未定义的downstream event类别或抽象predicate code；
  provider error按declarative contract脱敏并使用canonical EventType；reopen通过Start-frozen recovery plan修复main/direct/window三类
  Start-without-End，semantic prefix末尾已有唯一provider error时保持provider-error outcome，无error才恢复runtime-error；phase固定exploration，L0B不据此deny；
- typed window opened/closed、projection rewritten、budget phase changed events；
- Inspector join contract。

#### L1：36K hard cap → dynamic soft projection target

- 删除固定 `tool_result_context_chars` 作为 run-ending truth；
- soft target 从 `ResolvedModelContextBudgetFact.input_budget_tokens` 派生；
- fixed non-result tokens（包含assistant tool-call framing）先计量；
- hard available 是 final input budget 的真实剩余；
- 超 soft target继续 degrade，不直接 fail；
- `max_projection_units_per_window`在L1只记录shadow diagnostic；L2/L3只降低token表示，L4才有关闭旧window的hard出口；
- final resolved input budget仍是 provider 前 hard cap。

#### L2：跨 tool-result rollup 与 artifact-aware thinning

- old completed observations可合并成 bounded rollup；
- latest/currently actionable result优先保留；
- artifact locator、timing、result state、pairing不可丢；
- raw event/artifact不变；
- rollup使用typed `PreparedObservationRollupUnit`，在pure compiler前完成artifact materialization，并在完整pair group后以独立
  runtime-owned observation carrier插入；ordering anchor不产生tool-call/result归因，禁止追加到最后member的tool-result body；
- runtime-observation carrier完整fact由`ResolvedModelTargetFact`持有并进入target fingerprint；registry精确rebind，prepared unit只引用同一contract；
- anchor group中的非member sibling绝不自动扩展进rollup；group不完整则放弃该rollup；
- render decision durable，可 replay，不因下一 compile 随机漂移。
- L2/L3尚无window compaction时，minimum projection若仍在hard input内则继续并记录typed unreachable diagnostic；若超过hard input则以
  `context_window_compaction_unavailable`收口且不调用provider。L4才把同一结果改为真正的window compaction。

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
- compaction前先resolve actual summarizer call并计算physical-bound reservation；exploration放不下时先切`finalization_only`并重启safe
  point，再从独立compaction reserve申请；该reserve仍放不下才typed exhausted；
- 从L4开始强制active unit cap；通过关闭旧window和summary减少active units，protected tail仍超限则fail closed；
- durable raw history不删除。

#### L5：rollout budget、finalization reserve、阶段状态机

正常状态机：

```text
exploration
  -> warning
  -> restricted
  -> finalization_only
  -> exhausted
  -> emergency_hard_stop
```

- `max_turns/max_tool_calls` 提高并降级为 emergency circuit breaker；
- 激活L0B已接线的model/tool/child reservation authority：tool gate使用allow+reservation、terminal+settlement两个atomic batch，
  child使用versioned resolved额度；`SubagentBudgetSnapshotEvent`只冻结policy，dependency-waiting child只在实际start时提交resolved budget与
  reservation；不新增第二套owner或公式；
- 正常预算耗尽前至少保留一次完整 synthesis model call；
- exploration剩余无法容纳下一次真实ResolvedModelCall且无可回收reservation时，确定性进入`finalization_only`，不能等待settled ratio永远
  达不到100%；
- finalization-only 禁止新搜索/抓取，可读取已有 artifact/evidence；
- 对每个production primary target/summarizer pair在配置加载或`pulsara config-check`阶段计算total rollout budget、各finalization reserve与
  exploration allowance；任一启用组合不可行时，Host不得接受session/run；
- model-step可注入中性rollout status hint，只陈述phase、已结算model/tool calls、剩余allowance与bounded exact recurrence；
- hint不得要求继续、停止、finalize或选择下一步，也不得直接改变phase、permission、capability gate或reservation；
- raw call count本身不触发exploration阶段的模型可见hint；Inspector/CLI仍可始终展示计数；
- provider usage missing时按pre-margin input physical bound与完整effective output cap的reservation quote全额结算；reported input高于estimate但
  仍在physical bound内正常结算，stream字符数不成为第二token算法；
- 每个child按child primary target独立解析window policy，并让child primary/summarizer组合进入配置期可行性矩阵；
- child close/handoff以charged milliunits和各settlement basis counts为真源；reported token totals只统计reported子集，不保留单一usage status；nested
  aggregate、close event与handoff必须精确join同一subaccount fingerprint；
- 无论成功或预算耗尽都必须保留可读结论所需的finalization资源。

阶段四到L5结束。明确不实现通用provenance-aware evidence progress guard、novelty ontology、3/5/7 progress阈值或基于
“无新增证据”的自动deny。未来若建设AutoResearch/Web Research专用证据质量策略，必须另立产品层规格。

### 6.4 仍保留的安全 hard caps

- resolved model input budget；
- per-observation / essential envelope cap；
- artifact persistence、terminal raw collection、MCP payload尺寸保护；
- pagination/item cap；
- emergency max turns/tool calls；
- secret/redaction与schema/pairing contract。

### 6.5 完成定义

- 36,083 chars 的历史 envelope 不再单独终止 run；
- current run 可至少跨两个 context windows；
- raw events/artifacts 与 compacted projection均可 inspect；
- 120 万累计 input 不会被误判为单次 context overflow；
- budget 收窄时先限制探索并保留 final answer；
- 每个启用的primary/summarizer组合在运行前通过同一确定性公式的静态可行性校验，`config-check`可解释不可行组合；
- 完整model stream由LLMRuntime的service-owned handle持久化；committed-notification subscription仅供UI/live observation，Agent/direct/window
  通过EventLog-backed canonical result作控制决策且没有semantic/lifecycle第二writer；consumer停止pull或mailbox lag不会截断结果或阻断terminal
  settlement；blocked transport read由awaitable cancel signal与exact physical-operation drain收口；central sanitizing transport保证raw provider/network
  exception不跨入LLMRuntime、event、日志或exception chain；provider-error/cancel/runtime-error中的tool call即使已闭合也不执行，text不作为成功回复或
  canonical transcript；completed main result只有与ModelStart-frozen run activation精确join的durable ACCEPTED disposition才可执行/交付/replay，
  reopen不会把已durable provider error覆盖成
  runtime error；completed但无disposition且无downstream effect时reopen写suppressed-by-recovery，已有effect却缺accepted则latch；
- process restart可用ModelStart冻结的terminal IDs/quote确定性修复main/direct/window三类未闭合stream，且不会由generic reservation repair双重结算；
- model/compaction/child reservation复用pre-margin input physical bound + output cap的唯一quote算法；
- rollup作为独立runtime observation进入payload，不借任一tool result role承载跨调用内容；
- child按自己的primary target解析window policy，parent/child target不同不会复用错误threshold；
- 重复搜索真实dogfood由累计rollout phase与finalization reserve收口，不依赖runtime对任务价值的主观判断；
- 中性status hint只报告发生了什么，不告诉模型下一步要做什么；
- steady-state live compile与exact replay不再从parent ledger sequence 1全量fold subagent graph；
- live SubagentRuntime初始化、child rollout accounting、model stream subscription/materialization/control、window compaction
  planning/recovery与Host PRE_RUN transcript projection均已迁移到checkpoint/incremental store或indexed bounded reads；
- transcript projection以durable compaction summary为checkpoint，之后只读取bounded typed control delta与reply-ID range；首次window
  没有checkpoint时也受events/bytes cap，不能退回session-wide full scan；
- RunStart冻结的transcript checkpoint basis与“本次preflight是否产生terminal”分离；本次未compact时继续复用最近durable checkpoint，
  ContextFactSnapshot从该basis加bounded authority delta恢复；legacy preflight compaction source/lifecycle同样使用bounded/indexed reads；
- prior transcript与compaction summary共享accepted-disposition reducer；completed但被termination/recovery suppress的model output只供audit/UI，
  不得再次进入canonical transcript或summary；
- checkpoint + delta selection与offline sequence-1 reference fold在cap=0、no eligible、policy omitted、consumed/delivered及跨window
  pending result矩阵上完全一致；
- checkpoint schedule/ID/delta长度不进入candidate/snapshot/input semantic identity；
- checkpoint event、物理sequence/high-water与ledger continuity不进入graph semantic identity；未来unsupported graph-domain event fail closed；
- 原checkpoint artifact缺失或delta超bound时，任意不晚于目标high-water且contract-compatible的bounded rebase可保持exact replay；EventLog +
  matching reducer可由offline doctor重建cache；
- architecture/grep gate禁止正常production compile/replay重新引入sequence-1 subagent graph full fold或process-local truth fallback。

## 7. 阶段五：ContextSource Ownership Hard Cut

> **实施顺序已被取代。** ContextSource ownership 与增量 ProviderInput 现在按
> `PULSARA_CONTEXT_SOURCE_AND_INCREMENTAL_PROVIDER_INPUT_HARD_CUT_IMPLEMENTATION.zh.md`
> 的两阶段路线实施。以下内容只保留历史范围说明，不再作为开工顺序或验收真值。

### 7.1 目标

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

这里的正式source-owned candidate必须复用阶段三实施规格中已经hard-cut的candidate ingress；本节字段表示S阶段新增/
收紧的ownership contract，不代表再创建第二个同名DTO。需要新增required字段时升级统一schema并原子迁移。

### 7.2 所有权规则

- source 只读取 `ContextFactSnapshot` 的授权 slice；
- source 只产 typed facts/candidates；
- source 不产 provider-native message；
- source 不估算最终 payload；
- source 不读取其他 source 的输出；
- compiler 统一 lifecycle、allocation、timing overlay 与 lowering；
- transcript/tool-result 不伪装成 prose source。

### 7.3 PR 顺序

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

### 7.4 完成定义

- 每个 model-visible non-transcript byte都能归属到唯一 source/candidate；
- Inspector能从 compiled section追到 source fact；
- source registry输出顺序确定；
- 新 source不能绕过 lifecycle/budget/lowering；
- production不存在旧字符串重包装路径。

## 8. 阶段六：Prompt Cache

> **实施顺序已被取代。** Canonical ProviderInputPlan、stable prefix与timing placement已经并入
> `PULSARA_CONTEXT_SOURCE_AND_INCREMENTAL_PROVIDER_INPUT_HARD_CUT_IMPLEMENTATION.zh.md`
> 的第二阶段。以下内容只保留provider cache背景；provider-specific hint与usage观察后置。

### 8.1 前置条件

只有以下条件全部满足才开始实现：

- ResolvedModelCall 已完成；
- Host Run-Boundary Safe Point Hard Cut 已完成；
- ContextFactSnapshot 已完成；
- normalized transcript/tool units 已完成；
- Long-Horizon 的 window/projection/compaction identity 已完成；
- ContextSource ownership 已完成；
- provider remote continuation仍 fail-closed 禁用。

### 8.2 实施顺序

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
- permission/exposure中实际影响payload的facts；capability使用exposure semantic fingerprint，owner/basis等fact
  attribution只在确实改变wire payload时进入identity。

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

### 8.3 完成定义

- 同一 cache identity 必然对应同一 provider-visible prefix；
- 任意影响 prefix 的 durable fact变化都有稳定 break reason；
- cache hit/miss 不改变事件、工具权限或最终正确性；
- provider未报告usage时显示 missing，不伪造0；
- Inspector不通过当前 runtime重新推断历史 identity。

## 9. 跨阶段关键不变量

### 9.1 Descriptor / binding / provider input

```text
MCP installed snapshot
  -> capability descriptor set
  -> execution binding set
  -> catalog / active-skill model-visible projection
  -> CapabilityExposureSemanticFact
  -> committed Host/Subagent run entry + optional continuation boundary
  -> ContextFactSnapshot capability fact
  -> ContextSource candidate
  -> ProviderInputPlan tool schema
  -> Prompt Cache identity
```

任一步的 generation/identity 不一致都 fail closed，不允许“descriptor新、binding旧”或“tool schema旧、cache identity新”。

### 9.2 Durable raw truth 与 projection

- EventLog / artifact 是 raw durable truth；
- ContextWindow、rollup、compaction、section 是 model-visible projection facts；
- Prompt Cache 是 provider optimization observation；
- 三层不能互相覆盖或删除。

### 9.3 时间与 identity

- wall clock 是 observation，不参与可复现事实的随机生成；
- Host user observation在Host ingress记录，child task observation在SubagentRunEntryDriver记录；两者都由typed
  RunStart.current_user_message传给ContextFactSnapshot；
- compile timing放volatile lane；
- background MCP完成时间不直接修改 active run；
- cache identity不读取“现在”；
- Inspector显示记录时间，不重新计算历史 age。

### 9.4 Suspend / resume

- suspended run从原RunStart重建并验证permission/model target contract；
- MCP pending interaction保留原 binding generation；
- continuation每次按original basis解析current candidate；只有authorization、catalog与active-skill完整semantic identity相等才复用，否则单调收窄；
- approval/plan/MCP使用interaction-specific gate policy；
- context compaction不跨未完成tool pairing；
- resume前safe point若发现binding被撤销，写typed denial/failure，不静默换server generation。

## 10. 总体验收矩阵

### 10.1 静态

- Ruff；
- import/layering tests；
- grep gates；
- event serialization map completeness；
- Pydantic required-field negative tests；
- no-secret fingerprint tests。

### 10.2 单元与属性测试

- run-boundary admission/phase/disposition tests；
- PREPARING cancel/drain与active-after-commit tests；
- cancel during RunStart/resume publication wait confirmation tests；
- stop-before-commit与stream-before-first-pull ownership tests；
- continuation exposure subset property tests；
- same-MCP-installation下built-in/skill/custom revoke tests；
- catalog/active-skill projection revoke/change/new-entry narrowing tests；
- Host/subagent CommittedRunEntry与typed current-user replay tests；
- durable run existence four-state tests；
- post-commit capability/exposure/router failure RunEnd terminalization tests；
- observer full-queue detach与run_turn waiter cancellation tests；
- primitives permission layering/preset mapping drift tests；
- immutable snapshot mutation probes；
- pairing/order property tests；
- MCP epoch race / stale completion；
- context allocation determinism；
- projection rewrite idempotency；
- cache identity canonicalization。

### 10.3 故障注入

- known reconciliation before MCP/preflight；
- RunStart commit成功但publication失败；
- RunStart/resume commit await cancellation的none/full/partial/unknown confirmation；
- close during required MCP wait/preflight compaction；
- concurrent new-run/resume identity race；
- optional MCP connect/discovery hang；
- required deadline；
- config disable while worker completes；
- close during connect/discovery；
- compaction commit acknowledgement lost；
- compaction Started后cancel/terminal commit unknown/recovery repair；
- rollup/summary artifact write failure；
- provider cache hint rejection；
- old schema replay hard failure。

### 10.4 Real LLM / REPL dogfood

- slow optional MCP不阻塞REPL banner；
- MCP ready后下一new-run boundary可用且同snapshot执行；
- approval/plan/MCP live resume保持原permission/target并正确narrow exposure；
- preflight compaction可从Inspector join到触发它的新run；
- 40+ tool observations不死于36K固定cap；
- same-run跨window继续任务；
- budget收窄后产出final answer；
- Prompt Cache hit/miss trajectory语义一致。

## 11. 文档生命周期

- `PULSARA_MCP_STARTUP_LATENCY_NOTE.zh.md`：保留为问题与实测记录；
- `PULSARA_MCP_STARTUP_LATENCY_HARD_CUT_IMPLEMENTATION.zh.md`：阶段一唯一实施规格；
- `archived_docs/PULSARA_HOST_RUN_BOUNDARY_SAFE_POINT_HARD_CUT_IMPLEMENTATION.zh.md`：已完成的阶段二实施规格；
- `ARCHITECTURE_DEBT_AUDIT.zh.md`：记录阶段二、三、五的债务来源；
- `PULSARA_LONG_HORIZON_BUDGET_PRIOR_ART_RESEARCH.zh.md`：阶段四研究输入，实施前另写hard-cut规格；
- `PULSARA_PROMPT_CACHE_CONTRACT.zh.md`：阶段六产品契约，实施前按阶段二至五最终DTO校准。

每个实施文档在对应阶段完成后移入 `archived_docs/`；总路线在六阶段完成前保持根目录可见。
