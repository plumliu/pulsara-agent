# Pulsara Host Run-Boundary Safe Point Hard Cut 实施规格

> 状态：RB0–RB5 已完成并归档；实现、fault matrix、全量测试与必要 real-LLM dogfood 已验收。
> 基线：ResolvedModelCall、run-bound permission、Subagent graph reducer、MCP Startup Latency Hard Cut 已完成。
> 上游总路线：PULSARA_NEXT_FIVE_HARD_CUT_STAGES_PLAN.zh.md。
> 下游阶段：Context Compiler Input Hard Cut。
> Hard-cut 原则：项目尚未上线；不保留旧 constructor、旧 event schema、旧 scratchpad fallback 或双 pipeline。

## 0. 文档目标

本章只统一 Host 拥有的两类 run boundary：

    PRE_RUN
    PRE_INTERACTION_RESUME

它回答六个长期问题：

1. 哪些维护和校验必须发生在 RunStart durable commit 之前；
2. 哪些事实是 run-frozen，哪些只能在 continuation boundary 再派生；
3. 哪一步失败时不得创建或恢复 run；
4. 哪些 audit 必须与 RunStart 或 continuation boundary 原子提交；
5. permission、model target、MCP installation、preflight compaction、transcript 与 capability exposure 的唯一顺序；
6. Inspector 如何从 durable facts 解释“本次 run/continuation 之前发生了什么”。

本章不创建一个可插拔、覆盖所有 runtime safe point 的万能框架。最终代码形态是两条显式 typed pipeline：

    prepare_new_run_boundary(...)
      -> PreparedNewRunBoundary
      -> prepare AgentRunDraft
      -> commit_new_run_boundary(...)
      -> CommittedNewRunBoundary
      -> activate and continue

    prepare_interaction_resume_boundary(...)
      -> PreparedInteractionResumeBoundary
      -> commit_interaction_resume_boundary(...)
      -> CommittedInteractionResumeBoundary
      -> activate and route by interaction kind

两条Host pipeline最终都接入一个更低层的run-entry contract：

    CommittedRunEntry = CommittedHostRunEntry | CommittedSubagentRunEntry

PRE_INTERACTION_RESUME不是第三种run entry；它引用原CommittedRunEntry并产生optional continuation boundary。
Subagent child不经过Host safe point，但必须通过独立SubagentRunEntryDriver合法创建child RunStart，再与Host entry
共同进入AgentRuntime committed execution API。

## 1. 最终产品语义

### 1.1 新 run

Host 收到 user input 后立即记录 observation time，并为本次尝试分配 boundary identity。取得 HostSession run ownership 后：

- 已知 reconciliation/session fault 先 fail closed；
- model target 与 permission contract先做纯解析；
- MCP required generation 在自己的 deadline 内 ready 或阻断；
- optional MCP 失败可保持旧 surface / degraded projection；
- preflight compaction基于明确的 transcript snapshot；
- final transcript带 source high-water；
- exact current user message以required typed RunStart fact持久化；
- RunStart 与 pending MCP installation audit 一次 atomic batch commit；
- commit acknowledgement 前 run 处于 PREPARING，不是 ACTIVE；
- commit 后才执行 plan-entry audit、memory on_turn_start 和 capability exposure resolution；
- context compile 只消费 committed boundary facts与本 model step 的动态事实。

### 1.2 同进程 pending interaction continuation

PRE_INTERACTION_RESUME 只表示同一 live HostSession 中的：

- approval resolution；
- plan question / plan exit resolution；
- MCP input-required resolution。

它不表示 HostCore.resume_session()。Durable session reopen 无法恢复旧 coroutine、pending MCP lease 或旧 LoopState，必须继续作为 SESSION_REOPEN / OPEN_RECOVERY 独立协议。

PRE_INTERACTION_RESUME：

- 从原 RunStart 重建并验证 model target 与 permission contract；
- 保留 exact pending interaction / MCP lease identity；
- required MCP failure按 retryable/terminal原因区分；
- safe point安装的新 MCP surface先 durable audit，再继续旧 run；
- capability exposure不允许 widen；
- approval、plan、MCP三类 continuation 使用不同 gate policy；
- audit commit失败时原 pending state与lease保持可重试。

### 1.3 Capability 的 run/continuation 语义

run start 后产生一个初始 CapabilityExposureSnapshot。

同一 run suspend/resume 时：

- 完整CapabilityExposureSemanticFact（authorization、catalog与active-skill projection）未变：复用原 exposure；
- capability/binding 被撤销：生成 continuation exposure，单调收窄；
- 新安装的工具：不加入旧 run exposure，下一新 run 才可见；
- catalog/active projection只保留初始exposure中exact rendered fragments，删除可以、重新展开不可以；
- active skill、原 user intent、workspace、plan 与 permission basis 不得丢失；
- original MCP pending call继续校验 exact binding identity；
- 不允许使用 user_input=""、prior_messages=()、active_skill_names=() 全量重算。

这条语义同时满足：

- active run不会被后台 discovery 静默扩权；
- safety revoke能 fail closed；
- pending MCP binding变化可 terminal deny；
- ContextFactSnapshot可稳定记录 continuation exposure generation。

Capability exposure的owner不是永远等于host boundary：Host run使用`host_boundary`，child run使用
`subagent_run_start`。两者都必须产出同形状typed exposure fact；只有owner attribution不同。

## 2. 当前代码真相

### 2.1 PRE_RUN 当前顺序

当前 HostSession.run_turn() / stream_turn() 顺序是：

1. 在 run lock 外检查 lifecycle、stopping、pending、active；
2. 获取 run lock；
3. HostSession._apply_mcp_safe_point()；
4. AgentRuntime.resolve_run_model_target()；
5. rebuild prior messages；
6. preflight compact；
7. compact成功后再次 rebuild；
8. append plan runtime messages；
9. HostSession._begin_active_state() 立即设置 active_run_id；
10. AgentRuntime必要时 repair dangling subagent children；
11. capture RunPermissionSnapshot；
12. 此时才采集 user_observed_at_utc；
13. copy prior messages并append current user；
14. atomic emit_many(RunStart, pending MCP audits...)；
15. pending plan-entry audit；
16. memory on_turn_start；
17. resolve capability exposure并写 scratchpad；
18. CustomEvent(name="capability_exposure_resolved")；
19. model/context loop。

主要落点：

- src/pulsara_agent/host/session.py：run_turn、stream_turn、_prepare_prior_messages_for_turn；
- src/pulsara_agent/runtime/agent.py：_stream_task；
- src/pulsara_agent/runtime/session.py：emit_many/write_events。

### 2.2 PRE_INTERACTION_RESUME 当前顺序

approval、plan、MCP resume 当前共同前缀是：

1. lock外校验pending identity / active state；
2. 获取run lock；
3. apply MCP safe point；
4. require suspended state；
5. standalone commit pending MCP audits；
6. 从唯一 RunStart rebind model target；
7. 把 suspended state恢复为active；
8. Agent内再次校验pending payload；
9. interaction-specific continuation。

当前 permission snapshot只继续信任process-local LoopState，没有从RunStart重建或比对。

MCP installation变化时，HostSession会删除 scratchpad capability exposure；Agent随后以空user input、空prior messages、空active skills重算 exposure。

此外，当前 CapabilityRuntime.resolve_for_turn() 每次都会新建 CapabilityRegistry；registry generation
只表示本次临时注册顺序，不是跨 safe point 的 surface identity。MCP installation 不变也不能证明
built-in、local skill、custom/extra descriptor 或 execution binding 没有变化。

### 2.3 当前已经正确的事实

- RunStart 与本run首次引用的 MCP installation audits 已经使用 emit_many() 原子提交；
- resume audit 在state恢复与model continuation前提交；
- MCP input-required持有exact pending lease，不按当前tool name重新acquire；
- required MCP有自己的绝对deadline；
- worker completion只产candidate，不修改active surface；
- post-linearization MCP architecture fault会latch session并要求close/reopen；
- model target在resume时从原RunStart rebind。

本章必须保留这些正确性成果。

### 2.4 当前真实缺口

#### 缺口 A：已知 reconciliation 发现太晚

RuntimeSession只在最终event write时检查 reconciliation_required。Host可能已经：

- 安装新的process-local MCP surface；
- 执行preflight compaction；
- 写compaction artifact/event；
- 设置process-local active_run_id；

随后RunStart才被EventReconciliationRequired拒绝。

#### 缺口 B：admission只在锁外检查

两个并发resume/new-run caller可同时通过lock外检查。后进入lock的caller不会完整重验，可能在最终失败前先执行MCP mutation。

#### 缺口 C：PREPARING 与 ACTIVE 混合

active_run_id在RunStart commit前设置。Status/close无法区分：

- 正在等待required MCP；
- 正在preflight compact；
- RunStart正在commit；
- durable run已经active。

#### 缺口 D：boundary preparation没有完整close owner

MCP wait和preflight compaction发生在_run_owned()创建owned task之前。并发close若只drain active task，可能看不到仍在运行的boundary preparation。

#### 缺口 E：user observation time失真

user_observed_at_utc在MCP/preflight之后采集。慢MCP或compaction会把用户输入到达时间记成runtime准备完成时间。

#### 缺口 F：capability resume重算丢basis

空user input / active skill重算会：

- 丢explicit active skill；
- 丢user intent相关resolver输入；
- 允许新surface工具进入旧run；
- Inspector只看到初始exposure event，看不到resume-effective exposure。

#### 缺口 G：RunStart post-commit publication failure没有清晰owner

RunStart batch若durable commit成功、publisher observer失败，代码acknowledge MCP audit后重抛。Host随后清理process-local active bookkeeping，ledger可能留下dangling RunStart。

#### 缺口 H：preflight维护事实无法join即将开始的run

preflight compaction event仍归属旧latest event context。Inspector无法直接判断哪个new run触发了该compaction。

#### 缺口 I：commit await cancellation没有事实确认

RuntimeSession.write_events() 先在同步临界区提交ledger、归约并enqueue publisher，随后才await observer
delivery。CancelledError或其他BaseException若发生在publication await期间，caller不能把“await没有返回”解释成
“没有commit”。若boundary owner直接丢弃draft，会留下无人拥有的RunStart或已commit continuation boundary。

#### 缺口 J：plan host force-exit可绕过RunStart boundary

exit_plan_workflow()在没有pending plan interaction时会创建临时LoopState并直接设置active_run_id，随后写
PlanModeExitedEvent；该路径没有RunStart，是active-before-commit的独立后门。

#### 缺口 K：compaction cancellation可留下Started无terminal

ContextCompactionService.compact()只捕获Exception。CancelledError会越过现有failed event路径；同时service
直接append EventLog，尚无统一commit acknowledgement/result DTO，无法判断terminal fact是未提交、已提交但
publication失败，还是commit outcome unknown。

#### 缺口 L：child、user message与exposure projection没有通用entry truth

- child仍调用同一个AgentRuntime.run_task()自行创建RunStart与exposure，却不具备Host boundary id；
- current user text仍从RunStartEvent.metadata["user_input"]恢复，不能被降格成process-local-only input；
- LocalSkillCapabilityProvider只产catalog/active injection/prompt，不产descriptor，因此descriptor/binding fingerprint
  无法识别skill projection撤销；
- CapabilityExposurePlan还包含完整tool specs、catalog entries、active injections与两类prompt，单纯name sets不足以
  replay model-visible exposure。

#### 缺口 M：post-commit execution与stream observer缺统一owner

RunStart full commit后，capability surface freeze、exposure resolve/commit、subagent parent snapshot refresh或model loop
任一步异常都可能越过RunEnd。当前bounded lossless stream queue还会在consumer不读时阻塞producer；stop/close若
不先detach observer，RunEnd delivery也可能再次阻塞同一个queue。

### 2.5 实施前代码锚点

| 事实 | 当前落点 |
|---|---|
| new run/stream重复编排 | src/pulsara_agent/host/session.py:677-738 |
| MCP prepare/required/install | src/pulsara_agent/host/session.py:420-638 |
| approval/plan/MCP resume重复前缀 | src/pulsara_agent/host/session.py:800-889、952-977 |
| active-before-commit | src/pulsara_agent/host/session.py:1236-1242 |
| target-only resume rebind | src/pulsara_agent/host/session.py:1244-1261 |
| preflight transcript/compact/rebuild | src/pulsara_agent/host/session.py:1386-1432 |
| permission/user/RunStart/exposure顺序 | src/pulsara_agent/runtime/agent.py:1050-1155 |
| empty-basis exposure fallback | src/pulsara_agent/runtime/agent.py:1180-1193 |
| RuntimeSession reconciliation write gate | src/pulsara_agent/runtime/session.py:540-544 |
| compaction direct EventLog append | src/pulsara_agent/runtime/compaction/service.py:383、528、605 |
| write_events commit后await publication | src/pulsara_agent/runtime/session.py:467-494 |
| host plan force-exit伪造active state | src/pulsara_agent/host/session.py:891-950 |
| compaction仅捕获Exception | src/pulsara_agent/runtime/compaction/service.py:245-605 |
| child复用AgentRuntime host-style RunStart | src/pulsara_agent/runtime/agent.py:853-896 |
| current user从metadata replay | src/pulsara_agent/runtime/transcript.py:83-93 |
| local skill仅产prompt projection | src/pulsara_agent/capability/resolver.py:38-73 |
| bounded observer queue可阻塞producer | src/pulsara_agent/host/session.py:304-325、1323-1375 |
| mutable ContextCompileRequest.state | src/pulsara_agent/runtime/context_engine/types.py:214-233 |

## 3. 范围与非目标

### 3.1 本章范围

- HostSession new run / stream run入口；
- approval / plan / MCP live interaction resume入口；
- run-boundary admission与reconciliation guard；
- PREPARING ownership、cancel、drain；
- RunStart + MCP audit commit；
- continuation audit commit；
- transcript snapshot/source watermark；
- preflight compaction correlation；
- run permission/model target rebind；
- capability resolve basis与continuation narrowing；
- typed boundary diagnostics与Inspector projection；
- ContextFactSnapshot上游输入contract。
- Host/subagent共同的CommittedRunEntry与exposure owner contract（不把child塞进Host pipeline）。

### 3.2 非目标

- 不统一MID_TURN_COMPACTION、POST_TOOL、POST_RUN、CLOSE；
- 不改HostCore durable SESSION_REOPEN的产品语义；
- 不抽象任意participant注册机制；
- 不让一个coordinator任意修改LoopState/MCP/memory/subagent；
- 不在本章完成ContextFactSnapshot/TranscriptCompileInput hard cut；
- 不在本章完成ContextSource registry；
- 不让HostRunBoundaryDriver创建child run；child ownership仍属于SubagentRunEntryDriver；
- 不在本章解决全局Async RuntimeEventWriter/governance UOW；
- 但production compaction event不能继续绕过现有RuntimeSession writer；本章只做这条窄接线，不扩展为全局writer重构；
- 不把preflight compaction与RunStart强行放进同一数据库事务；
- 不为旧event/runtime兼容。

## 4. 冻结设计决策

### 4.1 名称

只新增：

    HostRunBoundaryKind.PRE_RUN
    HostRunBoundaryKind.PRE_INTERACTION_RESUME

禁止使用泛化的SafePointKind覆盖其他runtime safe point。

### 4.2 两条固定pipeline

PRE_RUN与PRE_INTERACTION_RESUME共享vocabulary和commit helper，但不是同一participant集合。

禁止：

- list[SafePointParticipant]动态注册；
- participant自行改变phase顺序；
- participant持有整个HostSession或LoopState service locator；
- participant自行写任意CustomEvent。

### 4.3 三段式

每条pipeline都分为：

    PREPARE
      -> DURABLE_COMMIT
      -> ACTIVATE

prepare可执行bounded async work，但不能把run标成ACTIVE。

durable commit是canonical existence boundary。

activate只能发生在commit acknowledgement之后。

### 4.4 RunBoundary不是ContextFactSnapshot

Run boundary冻结：

- run identity；
- user observation；
- model target；
- permission；
- MCP installation；
- capability resolve basis；
- transcript source boundary；
- preflight compaction refs。

ContextFactSnapshot每次model call还要加入：

- ResolvedModelCall；
- current memory projection；
- current subagent projection；
- normalized tool-result units；
- continuation exposure；
- compile timing。

### 4.5 Failure不是单一bool

统一使用：

    PROCEED
    PROCEED_DEGRADED
    RETRYABLE_BLOCK
    TERMINAL_BLOCK
    SESSION_LATCHED
    COMMIT_OUTCOME_UNKNOWN
    COMMITTED_BUT_PUBLICATION_FAILED
    COMMITTED_EXECUTION_FAILED

`COMMIT_OUTCOME_UNKNOWN`不是可重试的普通错误。它表示稳定candidate IDs尚未能被EventLog确认成
none/full/partial之一；在确认完成前session禁止新的run/resume/mutation，只允许inspect、bounded repair与close。

## 5. 依赖方向与模块落点

建议新增：

    src/pulsara_agent/primitives/permission.py
        PermissionMode / preset policy facts / single preset expansion mapping

    src/pulsara_agent/primitives/run_entry.py
        HostRunBoundaryKind / CurrentUserMessageFact / host-child run-entry facts / exposure owner

    src/pulsara_agent/primitives/run_boundary.py
        Host boundary-specific event-safe facts/enums only

    src/pulsara_agent/primitives/capability.py
        descriptor/binding/projection/exposure semantic facts

    src/pulsara_agent/host/run_boundary.py
        process-local prepared/committed DTO
        fixed pipeline coordinator
        no provider/adapter imports

    src/pulsara_agent/runtime/run_draft.py
        AgentRunDraft builder
        LoopState remains Agent-owned

    src/pulsara_agent/runtime/subagent/run_entry.py
        child-only prepare/commit driver

依赖方向：

    primitives.permission --------------------┐
    primitives.run_entry -> primitives.capability
              └-------------------------------┼-> primitives.run_boundary
    primitives.capability --------------------┘
        -> event schema
        -> host.run_boundary / subagent.run_entry
        -> AgentRuntime committed-entry/continuation APIs
        -> HostSession entry facades
        -> ContextFactSnapshot builder

禁止：

- primitives import HostSession/LoopState/MCP manager；
- event schema import host coordinator；
- compiler import HostSession；
- MCP supervisor import AgentRuntime；
- Inspector读取live coordinator重算历史boundary。

依赖图表示单向允许边：run_entry不import capability/run_boundary；capability可import exposure owner；run_boundary可
import capability与subagent entry。HostRunBoundaryKind虽用于Host pipeline，但放在run_entry低层，避免
CapabilityExposureOwnerFact反向import host/run_boundary形成环。

RB0必须把PermissionMode、preset policy payload与`preset_to_policy(mode).to_dict()`的唯一mapping下沉到
`primitives.permission`。runtime permission gate反向消费该primitive；不得在run_boundary复制一份字符串Literal或
preset mapping，也不保留`runtime.permission.PermissionMode`兼容import path。`EffectivePermissionPolicy`可以继续作为runtime/component对象，但生产event fact只使用低层
`PresetPermissionPolicyFact`。

## 6. 低层event-safe contracts

本节所有Pydantic fact统一`extra="forbid"`，生产constructor必须显式提供required字段；进入event前做
recursive JSON-safe copy/validation，禁止把mutable dict/list、manager/callable或live object引用留在fact中。
示例省略公共model config，不表示这些规则可选。

### 6.1 Enums

    class HostRunBoundaryKind(StrEnum):
        PRE_RUN = "pre_run"
        PRE_INTERACTION_RESUME = "pre_interaction_resume"

    class RunEntryKind(StrEnum):
        HOST = "host"
        SUBAGENT_CHILD = "subagent_child"

    class CapabilityExposureOwnerKind(StrEnum):
        HOST_BOUNDARY = "host_boundary"
        SUBAGENT_RUN_START = "subagent_run_start"

    class DurableRunExistence(StrEnum):
        NONE = "none"
        FULL = "full"
        UNKNOWN = "unknown"
        PARTIAL_UNTRUSTED = "partial_untrusted"

Permission primitive同时在RB0定义为：

    class PermissionMode(StrEnum):
        READ_ONLY = "read-only"
        ASK_PERMISSIONS = "ask-permissions"
        ACCEPT_EDITS = "accept-edits"
        BYPASS_PERMISSIONS = "bypass-permissions"

    class PresetPermissionPolicyFact(BaseModel):
        mode: PermissionMode
        expanded_policy: dict[str, JsonValue]

    preset_permission_policy_fact(mode: PermissionMode) -> PresetPermissionPolicyFact

唯一preset mapping与validator位于`primitives.permission`。expanded_policy必须逐字段等于该mapping，不能接受
custom policy；runtime `preset_to_policy()`只是把同一primitive fact转换为EffectivePermissionPolicy，不拥有第二份
常量表。`DEFAULT_PERMISSION_MODE`也移到该primitive；Host/runtime不得各自定义default。

    class HostRunBoundaryPhase(StrEnum):
        INGRESS = "ingress"
        ADMISSION = "admission"
        CONTRACT_RESOLUTION = "contract_resolution"
        RECOVERY_MAINTENANCE = "recovery_maintenance"
        MCP_REQUIRED_WAIT = "mcp_required_wait"
        MCP_INSTALLATION = "mcp_installation"
        TRANSCRIPT_SNAPSHOT = "transcript_snapshot"
        PREFLIGHT_COMPACTION = "preflight_compaction"
        FINAL_FREEZE = "final_freeze"
        DURABLE_COMMIT = "durable_commit"
        ACTIVATION = "activation"
        POST_COMMIT_INITIALIZATION = "post_commit_initialization"

    class HostRunBoundaryDisposition(StrEnum):
        PROCEED = "proceed"
        PROCEED_DEGRADED = "proceed_degraded"
        RETRYABLE_BLOCK = "retryable_block"
        TERMINAL_BLOCK = "terminal_block"
        SESSION_LATCHED = "session_latched"
        COMMIT_OUTCOME_UNKNOWN = "commit_outcome_unknown"
        COMMITTED_BUT_PUBLICATION_FAILED = "committed_but_publication_failed"
        COMMITTED_EXECUTION_FAILED = "committed_execution_failed"

### 6.2 Boundary identity

    class HostRunBoundaryIdentityFact(BaseModel):
        boundary_id: str
        kind: HostRunBoundaryKind
        runtime_session_id: str
        run_id: str
        turn_id: str
        reply_id: str
        attempt_number: int
        observed_at_utc: str

规则：

- boundary_id每次host action attempt唯一；
- retryable resume重试继续使用原interaction_id，但生成新boundary_id；
- run_id在PRE_RUN ingress分配，但在RunStart commit前不代表durable run；
- observed_at_utc为Host API观察到用户输入/interaction resolution的UTC时间；
- timestamp必须timezone-aware、canonical UTC；
- attempt_number必须正数。

### 6.3 Run entry、current user与exposure owner

    class CurrentUserMessageFact(BaseModel):
        message_id: str
        source_kind: Literal[
            "host_user_input", "subagent_task", "subagent_primitive_objective"
        ]
        text: str
        observed_at_utc: str
        content_sha256: str
        source_artifact_id: str | None

这是current user text的durable truth，不是process-local diagnostic。Host user input要求source_artifact_id=None；
task-backed child要求source_kind=subagent_task；primitive child要求source_kind=subagent_primitive_objective；两者的
source_artifact_id都指向durable task/objective artifact。`content_sha256`按UTF-8原文计算，`observed_at_utc`
必须canonical UTC。Host message observed_at必须等于boundary identity observed_at；child message observed_at必须等于
SubagentRunEntryFact.task_observed_at_utc。RunStart.created_at在final freeze时生成，必须canonical UTC且
`RunStart.created_at >= current_user_message.observed_at_utc`，不得为了cross-field equality回填成较早的ingress/task
观察时间。RunStartEvent.user_input_chars必须等于`len(text)`。

    class CapabilityExposureOwnerFact(BaseModel):
        owner_kind: CapabilityExposureOwnerKind
        owner_id: str
        host_boundary_kind: HostRunBoundaryKind | None
        runtime_session_id: str
        run_id: str

`host_boundary`的owner_id是boundary_id；`subagent_run_start`的owner_id是child RunStartEvent.id（orchestration
subagent_run_id仍在SubagentRunEntryFact中）。owner runtime/run必须
与carrier RunStart或continuation event一致。host owner要求host_boundary_kind非空；child owner要求该字段None。

    class ChildResultRenderPolicyFact(BaseModel):
        renderer_version: str
        max_summary_chars: int
        max_artifact_refs: int
        policy_fingerprint: str

renderer_version必须非空，两个cap必须>=0。policy_fingerprint按canonical
`[renderer_version, max_summary_chars, max_artifact_refs]`重算；不得包含当前default、object identity或wall clock。
SubagentBudget/SubagentBudgetSnapshotEvent在RB2新增required
`max_result_artifact_refs_per_child`，且entry policy的max_summary_chars/max_artifact_refs必须分别等于owning
SubagentRunStartedEvent.budget_snapshot中的`max_result_summary_chars_per_child`与
`max_result_artifact_refs_per_child`。V1 SubagentBudget构造默认值冻结为32，但event/entry字段始终required；不允许entry
driver在replay/repair时另取当前config default。

    class SubagentRunEntryFact(BaseModel):
        subagent_run_id: str
        subagent_task_id: str | None
        parent_runtime_session_id: str
        parent_run_id: str
        spawn_edge_id: str
        capability_profile_fingerprint: str
        task_artifact_id: str
        task_observed_at_utc: str
        child_result_render_policy: ChildResultRenderPolicyFact
        permission_snapshot_id: str
        model_target_fingerprint: str
        mcp_installation_id: str
        mcp_installation_owner_runtime_session_id: str

SubagentRunEntryFact只保存child ledger可以验证的event-safe来源。child执行handle、parent runtime instance、slot
reservation或MCP manager均不得进入该fact。driver必须校验CurrentUserMessageFact text/hash/observed_at与
task_artifact及其source event hydrate结果完全相等，不能只信任process-local task string。

child branch invariant：

- task-backed child的`subagent_task_id`必须非空，并精确匹配parent graph中owning task，current user source kind必须为
  subagent_task；
- primitive/run-only child的`subagent_task_id`必须为None，parent graph不得伪造task ownership，current user source
  kind必须为subagent_primitive_objective；
- 两者都必须有task/objective artifact，因为child current-user text仍需durable replay；primitive child通过spawn edge而
  不是task id验证artifact归属；
- Inspector的task_id允许null，并明确显示`entry_mode=primitive_run`或`task_backed`；
- child_result_render_policy在RunStart前冻结，normal path与SESSION_REOPEN repair必须消费同一完整policy fact；
- V1 child MCP installation owner必须等于parent_runtime_session_id，并与RunStart顶层owner字段一致。

RunStartEvent最终required字段：

    run_entry_kind: RunEntryKind
    current_user_message: CurrentUserMessageFact
    terminal_run_end_event_id: str
    new_run_boundary: NewRunBoundaryFact | None
    subagent_run_entry: SubagentRunEntryFact | None

branch invariant：

- host：new_run_boundary required，subagent_run_entry=None；
- subagent_child：subagent_run_entry required，new_run_boundary=None；
- current user、permission、model、MCP identity必须与对应entry fact一致；
- terminal_run_end_event_id在RunStart commit前生成，整个run只允许一个匹配ID的RunEnd；
- `metadata["user_input"]`禁止作为生产truth，RB2删除writer与replay fallback。

RB0将当前runtime `StopReason` TypeAlias hard cut到`primitives.run_lifecycle`：

    class RunStopReason(StrEnum):
        FINAL = "final"
        MAX_TURNS = "max_turns"
        MODEL_ERROR = "model_error"
        TOOL_ERROR_BUDGET = "tool_error_budget"
        PLAN_INTERACTION_BUDGET = "plan_interaction_budget"
        MEMORY_HOOK_ERROR = "memory_hook_error"
        WAITING_USER = "waiting_user"
        ABORTED = "aborted"
        POST_COMMIT_INITIALIZATION_ERROR = "post_commit_initialization_error"
        RUNTIME_PUBLICATION_FAILURE = "runtime_publication_failure"
        INTERACTION_ROUTER_ERROR = "interaction_router_error"
        SUBAGENT_PENDING_UNSUPPORTED = "subagent_pending_unsupported"
        RUNTIME_EXECUTION_ERROR = "runtime_execution_error"

LoopState.stop_reason与AgentRunResult.stop_reason使用该enum；WAITING_USER只允许segment result，不允许进入RunEnd。新增
稳定原因必须扩展enum、validator、CLI/Inspector mapping与contract test，禁止自由字符串reason registry。

RunEndEvent新增required：

    terminalization_kind: Literal[
        "normal",
        "user_stop",
        "host_teardown",
        "execution_failure",
        "recovered_interrupted",
    ]

其event.id必须等于对应RunStart.terminal_run_end_event_id，`stop_reason: RunStopReason`为required。validator冻结矩阵：

| terminalization_kind | status | stop_reason | abort_kind | error_message |
| --- | --- | --- | --- | --- |
| normal | finished | final | None | None |
| user_stop | aborted | aborted | user_stop | None |
| host_teardown | aborted | aborted | host_teardown | None |
| execution_failure | failed | 除final/waiting_user/aborted外的稳定failure enum | None | required bounded/redacted |
| recovered_interrupted | aborted | aborted | host_teardown | None；recovery attribution由typed metadata/fact保存 |

不允许把host teardown伪装成user stop，也不允许用自由字符串组合表达新增终态。reducer拒绝同run第二个terminal、
错误ID或不符合矩阵的payload。

parent subagent ledger引用child native terminal使用`primitives.subagent`中的低层fact（只依赖
`primitives.run_lifecycle.RunStopReason`，不得import event/runtime）：

    class ChildNativeTerminalReferenceFact(BaseModel):
        child_runtime_session_id: str
        child_run_id: str
        terminal_event_id: str
        terminal_sequence: int
        terminal_status: Literal["finished", "failed", "aborted"]
        terminalization_kind: Literal[
            "normal", "user_stop", "host_teardown",
            "execution_failure", "recovered_interrupted",
        ]
        stop_reason: RunStopReason

    class ChildExplicitResultEvidenceFact(BaseModel):
        source_result_submitted_event_id: str
        source_result_submitted_event_sequence: int
        child_runtime_session_id: str
        child_run_id: str
        source_tool_call_id: str
        tool_call_start_event_id: str
        tool_call_start_sequence: int
        tool_result_end_event_id: str
        tool_result_end_sequence: int

    class ChildResultHandoffFact(BaseModel):
        handoff_kind: Literal["explicit", "inferred"]
        renderer_version: str
        render_policy_fingerprint: str
        child_terminal_reference: ChildNativeTerminalReferenceFact
        explicit_evidence: ChildExplicitResultEvidenceFact | None
        result_id: str
        summary: str
        result_artifact_id: str
        artifact_ids: tuple[str, ...]
        rendered_payload_sha256: str
        token_usage: ModelTokenUsageFact | None
        usage_status: Literal["complete", "partial", "missing"]
        tool_call_count: int

SubagentRunCompletedEvent新增required `result_handoff: ChildResultHandoffFact`；其现有result_id/summary/
result_artifact_id/artifact_ids/token_usage/tool_call_count字段必须和handoff逐项相等。SubagentRunCompletedEvent、SubagentRunFailedEvent与
SubagentRunCancelledEvent同时新增
`child_terminal_reference: ChildNativeTerminalReferenceFact | None`。branch invariant：

- child RunStart确认NONE时，parent start-failure允许reference=None，且不得声称child_run_id；
- child RunStart一旦FULL，parent completed/failed/cancelled terminal fact必须有reference；
- reference runtime/run必须匹配SubagentRunFact，event ID/sequence/status/kind/reason必须和child EventLog中唯一RunEnd
  完全相等；
- SubagentRunCompletedEvent的child_run_id在本hard cut中改为required，且必须等于reference.child_run_id；该冗余字段只作
  现有graph projection索引，reference是跨ledger attribution truth；
- completed event的result_handoff.child_terminal_reference必须等于event.child_terminal_reference；
- SubagentRunFailedEvent/SubagentRunCancelledEvent只有`child_run_start_not_committed`的NONE branch允许reference=None；
  其他terminal branch必须非空；
- UNKNOWN/PARTIAL child不得构造terminal reference，也不得提交普通parent terminal event。

handoff invariant：explicit要求explicit_evidence非空，所有result/summary/artifact字段逐字复用该唯一
SubagentResultSubmittedEvent；inferred要求explicit_evidence=None。两者的renderer_version与
render_policy_fingerprint必须分别等于SubagentRunEntryFact.child_result_render_policy.renderer_version与
policy_fingerprint；该policy同时冻结explicit passthrough、
inferred rendering、artifact-ref cap与accounting聚合算法。primary result artifact必须位于sorted/unique
artifact_ids，payload hash必须等于artifact bytes hash。tool_call_count从child ledger中
`sequence <= terminal_sequence`的unique ToolCallStart facts重算；token usage从ModelCallEnd facts按renderer version聚合：
全部有usage为complete，部分缺失为partial且保存reported sum，全部缺失为missing且token_usage=None。parent completion
的token_usage/tool_call_count必须和handoff相等，不能由live AgentRunResult另算第二份。

summary chars不得超过policy.max_summary_chars；除primary result artifact外的artifact refs数量不得超过
policy.max_artifact_refs，选择规则由renderer_version冻结（V1按child event sequence、artifact id稳定排序后取前N，禁止
依当前archive枚举顺序）。cap=0是合法分支：summary为空串、supplemental refs为空，但primary result artifact仍required。

validator还要求：ChildNativeTerminalReferenceFact.terminal_sequence>=1；usage_status=missing iff token_usage is None；
complete/partial iff token_usage非空；tool_call_count>=0。ModelTokenUsageFact继续负责所有token字段非负、cached/reasoning
不超过parent count以及total=input+output；handoff不得用裸dict绕过该validator。

RB2将production SubagentResultSubmittedEvent的`source_tool_call_id`改为required，并新增required
`source_child_runtime_session_id`与`source_child_run_id`；component test若要直接覆盖result reducer，应构造完整synthetic
child call/result evidence，不能写一个production event却留空source。

构造explicit handoff前，EventLogLocator必须验证：submission归属matching parent SubagentRunFact；child EventLog存在同ID
ToolCallStart/ToolResultEnd pairing；tool name精确为`report_agent_result`；result为terminal success；call start/result sequence
均>=1且严格小于child terminal sequence；child runtime/run和handoff terminal reference完全相等。验证结果写入
ChildExplicitResultEvidenceFact。任一缺失、迟到、tool name错误、跨run或pairing冲突时，该submission不是“退回inferred”
的普通缺失，而是explicit_result_evidence_invalid contract error并阻止parent completion，避免同一parent event ID在normal/
repair间选择不同payload。

`report_agent_result` production writer在提交SubagentResultSubmittedEvent前就必须读取run-entry render policy：summary按同一
versioned规则clip，supplemental artifact refs超过max_artifact_refs则tool result fail closed且不写submission event；handoff
阶段不得再次截断一个已经durable的explicit result。

### 6.4 Transcript source boundary

    class BoundaryTranscriptSnapshotFact(BaseModel):
        source_through_sequence: int
        source_event_count: int
        compacted_window_id: str | None
        preflight_compaction_id: str | None
        preflight_compaction_terminal_event_id: str | None
        preflight_compaction_terminal_sequence: int | None

Invariant：

- source_through_sequence >= 0；
- final transcript只允许读取sequence <= source_through_sequence的event；
- preflight attempt id存在时terminal event id/sequence必须同时存在；Completed才允许
  compacted_window_id非空，Failed terminal时该字段为None；
- compaction未attempt时四个compaction字段均为None；
- field nullable来自真实semantic branch，不是legacy兼容。

### 6.5 Capability resolve basis

execution surface与model-visible projection必须分层建模：

    class CapabilityDescriptorBindingIdentityFact(BaseModel):
        capability_name: str
        provider_id: str
        descriptor_id: str
        descriptor_fingerprint: str
        descriptor_artifact_id: str
        binding_fingerprint: str | None
        binding_contract_id: str | None
        binding_contract_version: str | None

    class CapabilityExecutionSurfaceIdentityFact(BaseModel):
        surface_contract_version: str
        entries: tuple[CapabilityDescriptorBindingIdentityFact, ...]
        descriptor_set_fingerprint: str
        execution_binding_set_fingerprint: str
        execution_surface_fingerprint: str
        mcp_installation_id: str

每个descriptor entry保存完整canonical descriptor的artifact ref，使replay可以恢复ToolSpec的name、description与
input schema；fingerprint覆盖output/artifact declarations、permission categories、availability、advertise、
deferred/hidden、parallelism与suspension flags。binding fingerprint覆盖origin、implementation contract id/version、
MCP binding identity与execution boundary。非model-callable descriptor允许binding fields为None；callable descriptor
的binding_fingerprint、binding_contract_id与binding_contract_version必须全部非空，且能在current live
ToolRegistry中按三者精确rebind。三个binding字段必须同时为空或同时非空，禁止只有contract id没有version。

entries按capability name排序且name/descriptor id唯一；descriptor artifact canonical payload的hash必须等于
descriptor_fingerprint。descriptor_set、binding_set与execution_surface fingerprints都从完整entries重算验证，
不接受caller提供但无法复算的opaque hash。

local skill等provider可能不产生descriptor，必须单独保存model-visible projection：

    class CapabilityProjectionEntryFact(BaseModel):
        projection_entry_id: str
        projection_kind: Literal[
            "catalog_entry", "active_skill_injection", "provider_prompt_fragment"
        ]
        stable_name: str
        provider_id: str
        source_kind: Literal["builtin", "mcp", "workspace", "user", "bundled", "custom"]
        content_fingerprint: str
        content_artifact_id: str

    class CapabilityRenderedProjectionFragmentFact(BaseModel):
        fragment_id: str
        container_id: str
        fragment_role: Literal["prefix", "entry", "suffix", "static"]
        static_scope: Literal["container_wrapper", "projection_wrapper"] | None
        source_entry_id: str | None
        source_content_fingerprint: str | None
        fragment_fingerprint: str
        fragment_artifact_id: str
        order_index: int

    class CapabilityProjectionFact(BaseModel):
        visible_source_entries: tuple[CapabilityProjectionEntryFact, ...]
        rendered_fragments: tuple[CapabilityRenderedProjectionFragmentFact, ...]
        source_entry_count: int
        rendered_entry_count: int
        omitted_entry_count: int
        projection_semantic_fingerprint: str
        rendered_prompt_fingerprint: str | None
        rendered_prompt_artifact_id: str | None
        rendered_prompt_chars: int

    class CapabilityExposureSemanticFact(BaseModel):
        execution_surface: CapabilityExecutionSurfaceIdentityFact
        catalog_projection: CapabilityProjectionFact
        active_skill_projection: CapabilityProjectionFact
        authorization_fingerprint: str
        exposure_semantic_fingerprint: str

exposure_semantic_fingerprint按execution_surface_fingerprint、两类projection semantic/rendered prompt fingerprint与
authorization_fingerprint的canonical payload重算；MCP installation attribution、owner和diagnostics不参与。

empty projection要求visible_source_entries/rendered_fragments为空、rendered prompt字段None/chars=0。
`projection_entry_id`由canonical JSON
`[projection_kind, provider_id, source_kind, stable_name]`的SHA-256生成；provider A与provider B的同名同内容entry因此
仍是不同身份。该ID是跨exposure稳定的semantic identity，不是随机instance ID。visible_source_entries必须恰好等于
至少有一个entry fragment真正进入rendered prompt的model-visible items，counts满足
source=rendered+omitted且rendered_entry_count等于visible source entry数；renderer预算导致的omission必须显式
计数/diagnostic，不能把未渲染item当作exposed。非空projection必须写artifact；event payload不
重复大型skill body/prompt，但artifact内容、fingerprint与entry refs足以重建exact model-visible catalog/active prompt。
catalog与active entry分别保存；初次renderer还必须把exact model-visible输出分解成有序fragment。catalog的compact
index fragment、detail fragment和static wrapper必须分别建模；active skill body和provider prompt也必须是独立entry
fragment。continuation只能复用原始fragment bytes并删除fragment，绝不允许从current source entry重新渲染或把原来
index-only的skill升级成detail。MCP lifecycle prompt或其他不对应skill catalog item的provider文本必须建模为
provider_prompt_fragment，不能只藏在aggregate rendered prompt里。

validator要求rendered_entry_count == len(visible_source_entries)，projection_entry_id在catalog+active两类projection合并
后唯一，所有counts非负；fragment
order_index必须从0连续递增，fragment_id在整个exposure内唯一。entry fragment的source_entry_id必须引用一个visible
source entry且source_content_fingerprint与其content_fingerprint完全相等；prefix/suffix/static的source_entry_id与
source_content_fingerprint必须为None。fragment_role=static时static_scope required；其他role必须为None。prefix/
suffix只能包围同container的existing entry fragments，continuation删除最后一个entry时必须连同该container wrapper一起
删除；static_scope=container_wrapper同样随container最后一个entry删除，projection_wrapper只在projection仍有至少一个
entry fragment时保留。具有独立业务语义的静态文本不得使用static role，必须建模为
provider_prompt_fragment source entry并以source_entry_id关联。rendered prompt必须由remaining fragments按order精确拼接；其fingerprint/artifact必须成对存在。artifact读取后
的内容hash必须等于对应fragment及aggregate rendered_prompt_fingerprint。

fragment_id按canonical
`[projection_type, container_id, fragment_role, static_scope, source_entry_id, fragment_fingerprint,
original_order_index]`生成；同一exact
fragment跨continuation保留相同ID，语义不同的fragment不得复用ID。所谓“全局唯一”指同一
CapabilityExposureSnapshotFact的catalog+active fragment namespace中无重复；跨revision保留同一semantic fragment ID是
continuation审计所需，不应强制随机化。

projection artifacts在CapabilityExposureResolvedEvent commit前写入，并以exposure_id/owner metadata归属；artifact
写失败则不提交exposure event。event commit失败可留下可GC orphan artifact，但绝不允许event引用不存在的artifact。
initial exposure发生在RunStart后，失败由11.1 RunEnd terminalizer收口；continuation exposure artifact失败发生在
resume boundary commit前，保留原pending/lease可重试。

fingerprint禁止包含Python object id、callable repr、manager地址、secret、token或随机registry generation。
composition root生成CapabilityExecutionSurfaceIdentityFact；projection resolver基于original basis生成projection与最终
semantic fact。`mcp_installation_id`只作surface attribution，不参与semantic equality。production binding若没有stable
implementation contract id/version则surface freeze直接contract error；不得回退到class name、module path、
`repr(callable)`或当前注册序号。component tests可显式提供`test:<name>:v1`。

producer API在RB0定义、RB2一次性hard cut：

    class CapabilityExecutionSurfaceProvider(Protocol):
        provider_id: str
        def snapshot_descriptors(
            self,
            context: CapabilityExecutionSurfaceSnapshotContext,
        ) -> CapabilityDescriptorSnapshotOutput: ...

    class CapabilityProjectionProvider(Protocol):
        provider_id: str
        def resolve_projection(
            self,
            context: CapabilityProjectionResolveContext,
            *,
            execution_surface: CapabilityExecutionSurfaceIdentityFact,
        ) -> CapabilityProjectionOutput: ...

    CapabilityRuntime.freeze_execution_surface(...)
        -> FrozenCapabilityExecutionSurface

    CapabilityRuntime.resolve_exposure_projection(...)
        -> CapabilityExposurePlan

`snapshot_descriptors()`发生在RunStart前，只能读取composition-root/static built-in declaration、frozen MCP installation、
explicit custom-tool registration与同一时刻的binding registry snapshot；不得读取raw user input、prior transcript、active
skill、permission mode或plan context。Builtin/MCP/custom executable provider实现execution-surface protocol；local skill是
projection-only provider。availability若依赖当前turn context，必须建模为projection/authorization结果，不能修改descriptor
identity。

`resolve_projection()`发生在RunStart FULL commit后，只负责catalog entries、active skill injections、provider prompt与
基于frozen execution surface的direct/deferred/hidden/callable authorization；它不得新增descriptor/binding。现有
`CapabilityProvider.resolve()`同时返回descriptor与projection的production API在RB2删除，不保留compatibility overload；
任何context-dependent descriptor provider均为configuration contract error。

canonical descriptor artifacts在Host/child RunStart final freeze前按deterministic artifact id写入；写失败则不创建
RunStart。RunStart commit失败可能留下以boundary_id/subagent_run_id标记的orphan descriptor artifact，可由GC删除；
RunStart一旦FULL commit，其引用的descriptor artifact必须已存在。这样CapabilityResolveBasisFact不会引用一个要等
post-RunStart exposure阶段才创建的artifact。

event-safe fact：

    class CapabilityResolveBasisFact(BaseModel):
        basis_id: str
        basis_kind: Literal["initial", "continuation"]
        source_basis_id: str | None
        source_basis_fingerprint: str | None
        owner: CapabilityExposureOwnerFact
        workspace_identity_fingerprint: str
        memory_domain_id: str
        permission_snapshot_id: str
        plan_active: bool
        active_skill_names: tuple[str, ...]
        user_intent_fingerprint: str
        prior_transcript_fingerprint: str
        mcp_installation_id: str
        execution_surface_identity: CapabilityExecutionSurfaceIdentityFact
        basis_fingerprint: str

initial basis要求source fields均为None；continuation basis要求source fields均非空并指向原initial basis。process-local basis
另持有resolver所需raw user input、prior messages与workspace path；event-safe fact只保存bounded identity/fingerprint，不重复写secret或大文本。
`mcp_installation_id`必须等于`execution_surface_identity.mcp_installation_id`；basis_fingerprint覆盖其他全部字段的
canonical event-safe payload。

### 6.6 New-run boundary fact

    class NewRunBoundaryFact(BaseModel):
        identity: HostRunBoundaryIdentityFact
        transcript: BoundaryTranscriptSnapshotFact
        model_target_fingerprint: str
        permission_snapshot_id: str
        mcp_installation_id: str
        capability_basis: CapabilityResolveBasisFact
        degraded_reason_codes: tuple[str, ...]

    RunEntryFact = NewRunBoundaryFact | SubagentRunEntryFact

最终schema中RunStartEvent新增required的new_run_boundary字段用于Host parent/user run；该schema切换在RB2与
全部production/test constructor一次性完成，RB0不提前改RunStartEvent。

完整host/child branch见6.3。这里额外要求host boundary中的run/turn/reply、current user、model、permission与
MCP identity都和RunStart carrier一致；child则由SubagentRunEntryFact执行同等级cross-field validation。

### 6.7 Continuation boundary fact/event

    class InteractionResumeBoundaryFact(BaseModel):
        identity: HostRunBoundaryIdentityFact
        original_run_start_event_id: str
        original_run_start_sequence: int
        interaction_id: str
        interaction_kind: Literal["approval", "plan", "mcp_input_required"]
        suspended_state_token_fingerprint: str
        permission_snapshot_id: str
        model_target_fingerprint: str
        mcp_installation_id: str
        source_exposure_id: str
        source_exposure_semantic_fingerprint: str
        source_exposure_fact_fingerprint: str
        effective_exposure_id: str
        effective_exposure_semantic_fingerprint: str
        effective_exposure_fact_fingerprint: str
        exposure_transition: Literal["reused", "narrowed"]
        committed_mcp_audit_event_ids: tuple[str, ...]

新增typed RunInteractionResumeBoundaryEvent承载该fact。

事件只在boundary durable commit时写入；retryable pre-commit失败不伪造committed event。

### 6.8 Typed capability exposure event

将CustomEvent(name="capability_exposure_resolved") hard cut为：

    class CapabilityExposureResolvedEvent(EventBase):
        exposure: CapabilityExposureSnapshotFact
        exposure_revision: int

至少保存：

- exposure id/fingerprint与monotonic exposure_revision（不是CapabilityRegistry generation）；
- owner kind/id（host boundary或subagent run start）；
- resolution kind：initial / continuation_reused / continuation_narrowed；
- capability basis id/fingerprint；
- MCP installation id；
- source exposure id（continuation时required）；
- complete per-capability authorization entries与name sets；
- execution surface、catalog projection、active-skill projection及artifact/content refs；
- exposure_semantic_fingerprint与exposure_fact_fingerprint；
- diagnostics；
- narrowing reason codes。

旧CustomEvent生产路径删除，不保留双写。
initial exposure_revision=1；每个committed continuation exposure严格+1；reused也写新revision/event用于审计，但
复用同一semantic fingerprint并生成新的fact fingerprint/owner attribution。

### 6.9 Central event-safe DTOs

以下DTO在本章是required contract，不允许实现时自行用dict或scratchpad代替。

McpBindingIdentityFact与McpInstallationReferenceFact落在既有`primitives.mcp`，下文只冻结consumer shape；
run_boundary不得复制MCP fingerprint算法。

    class McpBindingIdentityFact(BaseModel):
        server_id: str
        slot_id: str
        snapshot_id: str
        discovery_generation: int

字段与process-local McpBindingIdentity一一对应，generation非负；不含manager/supervisor/lease。

    class McpInstallationReferenceFact(BaseModel):
        installation_id: str
        owner_runtime_session_id: str
        config_epoch: int
        event_safe_config_set_fingerprint: str
        server_snapshot_semantic_fingerprints: tuple[tuple[str, str], ...]
        binding_identities: tuple[McpBindingIdentityFact, ...]

这是Prepared/run-entry使用的MCP fact；它只含event-safe identity，不含descriptor object、McpCapabilityTool、
supervisor、manager或lease。其字段必须与McpCapabilitySnapshotInstalledEvent及RunStart顶层MCP字段一致。
server tuples与bindings必须sorted/unique，config_epoch/generation非负；各semantic fingerprint由既有MCP
primitive validator复算，不在run_boundary另建第二套MCP identity算法。

    class HostRunBoundaryDiagnostic(BaseModel):
        code: str
        severity: Literal["info", "warning", "error"]
        phase: HostRunBoundaryPhase
        disposition: HostRunBoundaryDisposition | None
        error_type: str | None
        message: str
        metadata: dict[str, JsonValue]

规则：code稳定；message与metadata经过secret redaction和长度上限；不得保存raw user input、MCP payload、
exception repr或workspace绝对路径。

    class PlanWorkflowStateFact(BaseModel):
        workflow_id: str | None
        active: bool
        revision: int
        entered_event_id: str | None
        entered_event_sequence: int | None
        entry_run_id: str | None
        entry_turn_id: str | None
        entry_reply_id: str | None
        stored_default_permission: PresetPermissionPolicyFact
        accepted_plan_artifact_id: str | None

PresetPermissionPolicyFact由`primitives.permission`唯一mapping生成并验证。`active=true`时workflow/entered/entry字段全部
required；`active=false`时全部为None。entry run/turn/reply只是workflow event的durable attribution；无pending
host exit复用它们时不得据此创建或发布active run。revision必须非负且只由typed plan reducer递增。

    class CapabilityAuthorizationEntryFact(BaseModel):
        capability_name: str
        descriptor_fingerprint: str
        binding_fingerprint: str | None
        disposition: Literal["direct", "deferred", "hidden"]
        callable: bool

    class CapabilityExposureDiagnosticFact(BaseModel):
        code: str
        severity: Literal["info", "warning", "error"]
        stage: Literal["resolve", "projection", "rebind", "narrow"]
        message: str

    class CapabilityExposureSnapshotFact(BaseModel):
        exposure_id: str
        owner: CapabilityExposureOwnerFact
        resolution_kind: Literal[
            "initial", "continuation_reused", "continuation_narrowed"
        ]
        resolve_basis: CapabilityResolveBasisFact
        semantic: CapabilityExposureSemanticFact
        authorization_entries: tuple[CapabilityAuthorizationEntryFact, ...]
        source_exposure_id: str | None
        direct_names: tuple[str, ...]
        deferred_names: tuple[str, ...]
        hidden_names: tuple[str, ...]
        callable_names: tuple[str, ...]
        exposure_semantic_fingerprint: str
        exposure_fact_fingerprint: str
        diagnostics: tuple[CapabilityExposureDiagnosticFact, ...]

authorization entries和四组name tuples必须完整、sorted/unique，并互相精确派生。V1设
`MAX_CAPABILITY_AUTHORIZATION_ENTRIES=512`作为安全hard cap；超限直接CapabilityExposureTooLarge并由committed
run terminalizer fail closed，禁止truncate后继续。continuation时source_exposure_id required；narrowed的
authorization/callable entries必须是source中相同descriptor+binding fingerprint entry的子集。
semantic.authorization_fingerprint必须从完整authorization_entries重算并相等。

`exposure_semantic_fingerprint`只覆盖execution surface、catalog/active projections、authorization entries与
model-visible prompts，不含owner、boundary、basis或event id；`exposure_fact_fingerprint`再覆盖owner attribution、
resolve basis、resolution kind、source exposure与semantic fingerprint。两者不得混用：前者决定reuse/narrow，
后者用于durable event equality/join。

CapabilityExposureSnapshotFact足以重建provider-visible semantic exposure；live execution仍必须把每个binding
fingerprint精确rebind到当前process-local ToolRegistry/MCP slot。event/artifact不能重建Python manager或lease，也
不得尝试这样做。CapabilityExposureDiagnosticFact同样bounded/redacted，且不依赖HostRunBoundaryPhase，因此
Host与child可共享event schema。

明确拆成两个API：

    rebuild_capability_exposure_semantic(
        fact: CapabilityExposureSnapshotFact,
        archive: ArtifactReader,
    ) -> ReplayedCapabilityExposurePlan

    rebind_capability_exposure_for_execution(
        replayed: ReplayedCapabilityExposurePlan,
        current_surface: CapabilityExecutionSurface,
    ) -> CapabilityExposurePlan

第一个恢复exact ToolSpec/catalog/active prompt与authorization，不需要live runtime；第二个只在descriptor/binding
fingerprints精确匹配时附加executable bindings。Inspector使用前者，Agent使用两者，任一artifact缺失或binding
不匹配都fail closed。

    class ResumeGatePolicy(BaseModel):
        interaction_kind: Literal["approval", "plan", "mcp_input_required"]
        recheck_capability: bool
        recheck_binding: bool
        recheck_permission: bool
        permission_wait_behavior: Literal[
            "not_applicable", "already_confirmed", "allow_wait", "fail_closed_deny"
        ]

冻结映射：approval=`capability+binding / already_confirmed`；plan=`workflow capability / permission
not_applicable`；MCP=`capability+binding+original permission / fail_closed_deny`。constructor不允许任意组合。

### 6.10 Durable commit confirmation

    class BoundaryBatchCommitStatus(StrEnum):
        NONE = "none"
        FULL = "full"
        PARTIAL = "partial"
        CONFLICT = "conflict"
        UNKNOWN = "unknown"

    class BoundaryBatchConfirmation(BaseModel):
        status: BoundaryBatchCommitStatus
        candidate_event_ids: tuple[str, ...]
        committed_event_ids: tuple[str, ...]
        committed_sequences: tuple[int, ...]
        actual_last_sequence: int | None

`FULL`要求所有candidate按相同ID、相同canonical payload、连续sequence存在；`NONE`要求一个candidate都
不存在；存在真子集或不连续为`PARTIAL`；同ID不同payload为`CONFLICT`；storage暂时不可确认才是
`UNKNOWN`。PARTIAL/CONFLICT立即latch ledger structural reconciliation；UNKNOWN latch boundary mutation，
但不得伪装成pre-commit failure。

唯一映射为：NONE→DurableRunExistence.NONE；FULL→FULL；UNKNOWN→UNKNOWN；PARTIAL/CONFLICT→
PARTIAL_UNTRUSTED。任何API、status或Inspector都不得把UNKNOWN/PARTIAL压成bool。
该enum描述PRE_RUN draft的RunStart存在性。PRE_INTERACTION_RESUME的原run已经FULL；resume boundary自身是否
committed由BoundaryBatchConfirmation表达，不能把原run降成UNKNOWN。HostBoundaryStopUncertain只用于PRE_RUN
commit uncertainty；suspended/active run stop沿原run terminalizer处理。

## 7. Process-local DTO

### 7.1 Immutable ingress

    @dataclass(frozen=True, slots=True)
    class NewRunBoundaryInput:
        identity: HostRunBoundaryIdentityFact
        user_input: str
        active_skill_names: frozenset[str]
        host_session_id: str
        conversation_id: str

    @dataclass(frozen=True, slots=True)
    class InteractionResumeBoundaryInput:
        identity: HostRunBoundaryIdentityFact
        interaction_id: str
        interaction_kind: Literal["approval", "plan", "mcp_input_required"]
        resolution: object
        suspended_state_token: str

Raw user input在prepare期间只存在process-local input；final freeze时必须原样复制到
RunStartEvent.current_user_message，随后该typed field成为replay/compaction/recovery truth。Raw interaction
resolution只在process-local input中存在，直到interaction-specific typed resolution event durable commit；两者都
不得复制进diagnostic全文。

`stream_turn()`以及streaming approval/plan resolution facades必须在普通同步函数体内创建对应BoundaryInput；
详见20.1。这样调用返回iterator前已经capture observed_at并注册Host-owned driver，不会把first
`__anext__()`时间误记为用户输入/interaction resolution时间。

### 7.2 Capability basis与run working set

    @dataclass(frozen=True, slots=True)
    class CapabilityResolveBasis:
        fact: CapabilityResolveBasisFact
        user_input: str
        prior_messages: tuple[Msg, ...]
        active_skill_names: frozenset[str]
        workspace_root: Path
        memory_domain_id: str

该process-local DTO可包含resolver真正需要的raw inputs。V1中的Msg尚未递归immutable，因此这里只承诺
`model_copy(deep=True)`后的single-owner deep copy，不宣称recursive freeze；不得持有HostSession、LoopState、
live manager或mutable registry。C阶段以TranscriptCompileInput替换它。

    @dataclass(slots=True)
    class RunWorkingSet:
        run_start_event_id: str
        run_start_sequence: int
        run_model_target: ResolvedModelTarget
        permission_snapshot: RunPermissionSnapshot
        plan_snapshot: PlanWorkflowStateFact
        capability_resolve_basis: CapabilityResolveBasis
        original_exposure_plan: CapabilityExposurePlan | None
        original_exposure_fact: CapabilityExposureSnapshotFact | None
        effective_exposure_plan: CapabilityExposurePlan | None
        effective_exposure_fact: CapabilityExposureSnapshotFact | None
        latest_committed_resume_boundary: InteractionResumeBoundaryFact | None

RunWorkingSet是Agent-owned process-local工作集，不是durable truth。所有`*Fact`字段必须能从RunStart、typed
exposure event与latest committed resume boundary重建；`*Plan`只能通过event-safe semantic fact在当前匹配的
live binding surface上精确rebind，不能从event重造manager/lease。删除scratchpad后不得从旧字符串或mutable
session default推断。

### 7.3 Prepared new run与blocked carrier

    @dataclass(frozen=True, slots=True)
    class PreparedNewRunBoundary:
        identity: HostRunBoundaryIdentityFact
        run_model_target: ResolvedModelTarget
        permission_snapshot: RunPermissionSnapshot
        plan_snapshot: PlanWorkflowStateFact
        mcp_installation_fact: McpInstallationReferenceFact
        owned_transcript_messages: tuple[Msg, ...]
        transcript_fact: BoundaryTranscriptSnapshotFact
        capability_basis: CapabilityResolveBasis
        pending_mcp_audits: tuple[AgentEvent, ...]
        diagnostics: tuple[HostRunBoundaryDiagnostic, ...]

`owned_transcript_messages`必须deep copy且只由boundary attempt拥有，但在C阶段前不宣称递归immutable。
Prepared DTO不得持有live manager、McpCapabilityTool、HostSession或mutable scratchpad。

live execution surface独立由coordinator attempt持有：

    @dataclass(slots=True)
    class BoundaryExecutionHandles:
        handle_id: str
        handle_generation: int
        owner_id: str
        state: Literal["attempt_owned", "run_owned", "retiring", "closed"]
        mcp_installation: McpInstalledCapabilitySnapshot
        capability_runtime: CapabilityRuntime
        tool_registry: ToolRegistry
        borrow_tracker: CapabilityExecutionBorrowTracker

该DTO明确是process-local live handle，不进入Prepared fact、event、fingerprint或compiler；commit/rollback/close按
handle_id/generation/owner_id负责install、retire或drain。borrow authority携带同一handle ID/generation，tracker只计数
`active_parent_tool_call_borrows`与`active_child_tool_call_borrows`；两者均为0即可退休execution handle，retiring后禁止
新borrow。MCP pending interaction lease完全归`McpServerSupervisor`，不得在tracker内镜像计数；execution handle退休不
查询或等待pending lease，MCP slot/manager退休也不依赖execution-handle tracker，只依赖Supervisor自己的active borrower
与pending reservation状态。detached child使用`ChildExecutionRegistry`持有的child-owned
execution handles；child整个lifetime不持有binding borrow/lease，只有真实MCP/tool call acquire后计入child handle的
`active_child_tool_call_borrows`，call terminal后释放。相关server slot变化仍按MCP contract先阻止新borrow、cancel/drain
affected child，再等待在途borrow归零。state迁移必须单向，retiring不得恢复run_owned。

PRE_INTERACTION_RESUME若current execution surface/binding identity与run owner current handle精确相等，可让attempt的
execution_handles=None并复用current handle；只要安装、binding或execution surface identity变化，就必须预建独立incoming
handles，不能原地修改current handle。

`PreparedNewRunBoundary`只表示`PROCEED`或`PROCEED_DEGRADED`，所以不携带一个可伪装为blocked的
disposition。target/permission/required MCP等早期失败使用独立carrier：

    @dataclass(frozen=True, slots=True)
    class HostRunBoundaryBlocked:
        identity: HostRunBoundaryIdentityFact
        phase: HostRunBoundaryPhase
        disposition: HostRunBoundaryDisposition
        diagnostics: tuple[HostRunBoundaryDiagnostic, ...]
        retry_after_utc: str | None

    PrepareNewRunBoundaryResult = PreparedNewRunBoundary | HostRunBoundaryBlocked

validator要求blocked disposition只能是RETRYABLE_BLOCK、TERMINAL_BLOCK、SESSION_LATCHED或
COMMIT_OUTCOME_UNKNOWN；PROCEED/DEGRADED必须返回Prepared，committed状态必须返回Committed DTO。

### 7.4 Agent run draft

    @dataclass(slots=True)
    class AgentRunDraft:
        state: LoopState
        run_start_event: RunStartEvent
        current_user_message: CurrentUserMessageFact
        terminal_run_end_event_id: str
        capability_basis: CapabilityResolveBasis

AgentRunDraft由AgentRuntime创建并拥有。它不是durable truth，也不进入compiler。

### 7.5 Committed new run

    @dataclass(frozen=True, slots=True)
    class CommittedNewRunBoundary:
        prepared: PreparedNewRunBoundary
        run_start_event_id: str
        run_start_sequence: int
        committed_audit_event_ids: tuple[str, ...]
        committed_through_sequence: int
        publication_status: Literal["completed", "failed_after_commit", "unavailable"]

    @dataclass(frozen=True, slots=True)
    class CommittedHostRunEntry:
        boundary: CommittedNewRunBoundary
        run_start_event: RunStartEvent
        run_start_sequence: int

    @dataclass(frozen=True, slots=True)
    class PreparedSubagentRunEntry:
        entry_fact: SubagentRunEntryFact
        current_user_message: CurrentUserMessageFact
        run_model_target: ResolvedModelTarget
        permission_snapshot: RunPermissionSnapshot
        mcp_installation_fact: McpInstallationReferenceFact
        capability_basis: CapabilityResolveBasis
        terminal_run_end_event_id: str

    @dataclass(frozen=True, slots=True)
    class CommittedSubagentRunEntry:
        prepared: PreparedSubagentRunEntry
        run_start_event: RunStartEvent
        run_start_sequence: int
        committed_through_sequence: int
        publication_status: Literal["completed", "failed_after_commit", "unavailable"]

    CommittedRunEntry = CommittedHostRunEntry | CommittedSubagentRunEntry

HostRunBoundaryDriver只产CommittedHostRunEntry；SubagentRunEntryDriver只产CommittedSubagentRunEntry。两者都在
RunStart commit前生成CurrentUserMessageFact与terminal_run_end_event_id，并将同一个CommittedRunEntry交给
AgentRuntime。禁止AgentRuntime根据caller类型猜entry kind或自行补RunStart。

### 7.6 Prepared/committed resume

    @dataclass(frozen=True, slots=True)
    class PreparedInteractionResumeBoundary:
        identity: HostRunBoundaryIdentityFact
        interaction_id: str
        interaction_kind: Literal["approval", "plan", "mcp_input_required"]
        suspended_state_token: str
        original_run_start_event: RunStartEvent
        rebound_model_target: ResolvedModelTarget
        permission_snapshot: RunPermissionSnapshot
        mcp_installation_fact: McpInstallationReferenceFact
        owned_continuation_exposure_plan: CapabilityExposurePlan
        continuation_exposure_fact: CapabilityExposureSnapshotFact
        pending_mcp_audits: tuple[AgentEvent, ...]
        gate_policy: ResumeGatePolicy
        diagnostics: tuple[HostRunBoundaryDiagnostic, ...]

    @dataclass(frozen=True, slots=True)
    class CommittedInteractionResumeBoundary:
        prepared: PreparedInteractionResumeBoundary
        exposure_event_id: str
        exposure_event_sequence: int
        boundary_event_id: str
        boundary_event_sequence: int
        committed_audit_event_ids: tuple[str, ...]
        committed_through_sequence: int
        publication_status: Literal["completed", "failed_after_commit", "unavailable"]

    PrepareInteractionResumeBoundaryResult = (
        PreparedInteractionResumeBoundary | HostRunBoundaryBlocked
    )

owned_continuation_exposure_plan与owned_transcript_messages同样是single-owner process data，不是event-safe fact；
durable/replay authority是continuation_exposure_fact。

### 7.7 Boundary attempt owner

HostSession新增process-local：

    @dataclass(slots=True)
    class HostRunBoundaryAttempt:
        boundary_id: str
        kind: HostRunBoundaryKind
        phase: HostRunBoundaryPhase
        owner_task: asyncio.Task[object]
        draft_run_id: str
        execution_handles: BoundaryExecutionHandles | None
        candidate_event_ids: tuple[str, ...]
        commit_state: Literal[
            "not_started",
            "commit_in_flight",
            "committed",
            "publication_failed",
            "commit_outcome_unknown",
            "ledger_latched",
        ]
        completion: asyncio.Future[HostRunBoundaryAttemptOutcome]

    @dataclass(frozen=True, slots=True)
    class HostRunBoundaryAttemptOutcome:
        boundary_id: str
        disposition: HostRunBoundaryDisposition
        commit_confirmation: BoundaryBatchConfirmation | None
        durable_run_existence: DurableRunExistence
        terminal_event_id: str | None
        diagnostics: tuple[HostRunBoundaryDiagnostic, ...]

    @dataclass(frozen=True, slots=True)
    class HostBoundaryStoppedBeforeCommit:
        status: Literal["cancelled_before_run_start"]
        boundary_id: str
        draft_run_id: str
        durable_run_existence: Literal[DurableRunExistence.NONE]
        diagnostics: tuple[HostRunBoundaryDiagnostic, ...]

    @dataclass(frozen=True, slots=True)
    class HostBoundaryStopUncertain:
        status: Literal["commit_outcome_unknown", "ledger_latched"]
        boundary_id: str
        draft_run_id: str
        durable_run_existence: Literal[
            DurableRunExistence.UNKNOWN,
            DurableRunExistence.PARTIAL_UNTRUSTED,
        ]
        commit_confirmation: BoundaryBatchConfirmation
        diagnostics: tuple[HostRunBoundaryDiagnostic, ...]

    HostBoundaryStopResult = HostBoundaryStoppedBeforeCommit | HostBoundaryStopUncertain

HostSession同时最多一个boundary attempt。

close/stop/status必须认识PREPARING attempt，但不得把它报告为ACTIVE run。
attempt owner用`try/finally`覆盖CancelledError在内的BaseException，确保shared completion最终解析为outcome或同一
异常；registry只在boundary_id/attempt object identity仍匹配时compare-and-clear。旧owner不得清除已经安装的
retry attempt，所有waiter观察同一个最终结果。

### 7.8 Attempt number与suspended state token

- PRE_RUN的attempt_number固定为1；用户重试new turn是新的boundary/run identity，不复用旧draft run id；
- live resume的counter由PendingInteractionRuntimeState在`_run_lock`内拥有；每次进入prepare原子`+1`；
- retryable failure保留interaction_id与suspended_state_token，但下一次使用新boundary_id与新attempt_number；
- suspended_state_token在run第一次进入WAITING_USER时随机生成并和pending interaction一起保存；
- 同一pending的重试不轮换token；新一轮suspension/新interaction必须轮换；
- terminal resolution、stop或close finalize后立即作废token；
- token是process-local anti-ABA identity，不写raw token到durable event，只写其event-safe fingerprint；
- resume boundary fact保存attempt_number和suspended_state_token_fingerprint，Inspector不得显示raw token。

### 7.9 Durable run owner与active segment owner

    @dataclass(frozen=True, slots=True)
    class RunTerminationIntent:
        intent_id: str
        kind: Literal["user_stop", "host_teardown"]
        requested_at_utc: str
        requester_id: str
        target_segment_id: str | None
        target_segment_generation: int | None

target segment fields必须同时为空或同时非空；ACTIVE stop/close必须冻结当前matching pair，SUSPENDED run允许两者为空并
直接由run terminalizer结束。requested_at必须canonical UTC，intent_id在首次CAS前生成且join者不得替换。

    @dataclass(slots=True)
    class StreamObserverHandle:
        observer_id: str
        queue: asyncio.Queue[AgentEvent | StreamSentinel]
        state: Literal["attached", "backpressured", "detached"]
        detached_reason: str | None
        detached: asyncio.Future[None]

    @dataclass(slots=True)
    class RunExecutionSegmentResult:
        segment_id: str
        segment_generation: int
        disposition: Literal["waiting_user", "run_terminal"]
        run_result: AgentRunResult

    @dataclass(frozen=True, slots=True)
    class RunSegmentInstallBlocked:
        reason: Literal[
            "termination_intent_present",
            "terminalization_started",
            "stale_activation_owner",
        ]
        current_terminal_state: str
        termination_intent_id: str | None

    @dataclass(slots=True)
    class RunExecutionSegmentOwner:
        segment_id: str
        segment_generation: int
        segment_state: Literal["reserved", "active", "completed"]
        activation_kind: Literal["initial", "interaction_resume"]
        activation_owner_kind: Literal[
            "host_run_boundary",
            "host_resume_boundary",
            "subagent_run_start",
        ]
        activation_owner_id: str
        driver_task: asyncio.Task[object] | None
        completion: asyncio.Future[RunExecutionSegmentResult]
        observer: StreamObserverHandle | None

    @dataclass(slots=True)
    class CommittedRunExecutionOwner:
        entry: CommittedRunEntry
        execution_handles: BoundaryExecutionHandles
        retiring_execution_handles: dict[str, BoundaryExecutionHandles]
        terminal_event_id: str
        terminal_candidate: RunEndEvent | None
        terminal_state: Literal[
            "open",
            "candidate_frozen",
            "committing",
            "confirmed",
            "commit_outcome_unknown",
            "ledger_latched",
        ]
        terminalization_task: asyncio.Task[BoundaryBatchConfirmation] | None
        termination_intent: RunTerminationIntent | None
        run_completion: asyncio.Future[AgentRunResult]
        next_segment_generation: int
        active_segment: RunExecutionSegmentOwner | None

HostSession或Subagent child registry按run_id只允许一个稳定CommittedRunExecutionOwner。它代表durable RunStart到匹配
RunEnd的完整lifetime；ACTIVE→WAITING_USER→ACTIVE的每一次执行只由一个可替换segment owner表示。normal completion、
stop、close与BaseException不能各自创建RunEnd；它们都join稳定run owner。terminal candidate一旦冻结，retry/
confirmation不得改payload或ID。

segment规则：

- install segment必须在run owner锁/CAS下取得`segment_generation=next+1`；同run同一时刻最多一个active segment；
- segment完成/取消/observer detach只能在`segment_id + generation`仍匹配时compare-and-clear，旧callback不得清除后装的
  resume segment，避免ABA；
- Host正常进入WAITING_USER时，segment completion返回WAITING_USER、detach observer并清除active_segment；稳定run owner
  与run_completion保持open，绝不写RunEnd；
- PRE_INTERACTION_RESUME成功commit后安装新的interaction_resume segment，不复用第一次API调用的driver task、observer
  或segment Future；
- `run_turn()`/`stream_turn()`只等待其segment completion；`run_completion`只能在matching RunEnd FULL commit并fold后
  完成；close/stop和recovery等待或驱动run completion；
- child V1不支持WAITING_USER，按11.1的child terminalization branch处理，因此不会留下可resume child segment。

activation owner cross-field invariant：Host initial=`initial + host_run_boundary + boundary_id`；Host resume=
`interaction_resume + host_resume_boundary + resume boundary_id`；child initial=
`initial + subagent_run_start + child RunStartEvent.id`。child不得伪造Host boundary，V1也不存在child resume segment。

registry API必须显式携带ABA identity：

    install_segment(
        run_id: str,
        *,
        activation_kind: Literal["initial", "interaction_resume"],
        activation_owner_kind: Literal[
            "host_run_boundary", "host_resume_boundary", "subagent_run_start"
        ],
        activation_owner_id: str,
        driver_factory: Callable[[], Coroutine[object, object, object]],
        observer: StreamObserverHandle | None,
    ) -> RunExecutionSegmentOwner | RunSegmentInstallBlocked

    complete_segment(
        run_id: str,
        *,
        segment_id: str,
        segment_generation: int,
        result: RunExecutionSegmentResult,
    ) -> Literal["completed", "stale_segment"]

`RunSegmentInstallBlocked.reason`只能是`termination_intent_present`、`terminalization_started`或
`stale_activation_owner`。`install_segment`必须在同一run-owner CAS临界区同时验证：active_segment=None、
terminal_state="open"、termination_intent is None、activation owner仍是latest committed entry/continuation；全部成立才
原子递增generation并先安装`segment_state=reserved, driver_task=None` owner；随后仍在同一个无await registry调用中才允许
调用driver_factory并`asyncio.create_task()`，再把task写回owner并切active。即便loop使用eager task factory，task开始时owner
已经可查。factory/create_task失败必须按segment identity compare-and-remove reserved owner，并由committed terminalizer收口。
若factory已返回coroutine而create_task失败，registry必须显式close coroutine，不能留下unawaited warning。blocked path绝不
调用factory，也不创建coroutine。`complete_segment`不得使用“当前run id相同”作为充分条件。resume retry在continuation
durable commit前不得安装segment。

suspended stop/close与post-commit resume竞态冻结为：

- intent先赢：resume即使continuation已FULL，也不得swap或安装segment；attempt-owned incoming handles转retiring并在borrow
  归零后关闭，run owner直接按intent terminalize；
- handle swap先赢、intent后到：intent可CAS成功，但随后install_segment必须看到intent并返回blocked；不启动driver，
  current incoming handles与old retiring handles都由run terminalizer/close收口；
- segment install先赢：intent记录matching segment identity后取消该segment，走普通committed stop；
- 任一blocked branch都不能把continuation durable fact回滚成未提交，也不能恢复旧exposure/surface；
- swap API与install API都必须检查termination_intent/terminal_state；前者在intent已存在时返回
  `swap_skipped_terminating`并把incoming ownership交还attempt retirement，后者执行最终线性化recheck。

resume execution-handle swap使用独立同步API：

    @dataclass(frozen=True, slots=True)
    class ExecutionHandleSwapResult:
        status: Literal["swapped", "swap_skipped_terminating"]
        current_handle_id: str
        retiring_handle_id: str | None
        termination_intent_id: str | None

    swap_execution_handles_after_continuation_commit(
        run_id: str,
        *,
        expected_current_handle_id: str,
        incoming: BoundaryExecutionHandles,
        committed_continuation_event_id: str,
    ) -> ExecutionHandleSwapResult

规则：

- incoming在continuation FULL commit前始终由resume attempt以attempt_owned持有；NONE rollback关闭incoming，UNKNOWN/
  PARTIAL由attempt保留并阻止破坏性close；
- FULL/fold后，在run owner同步CAS临界区验证run identity、expected current handle、incoming generation、
  terminal_state=open且termination_intent=None；无await地将old标为retiring、incoming改为run_owned、交换current pointer并
  把old放入retiring map；已有intent/terminalization时返回swap_skipped_terminating，不修改current pointer；
- swap是continuation execution surface的线性化点，必须发生在新segment install之前；
- old handle即使不再是current，也要等active parent/child tool-call borrow全部结束后才异步retire/close；不得因pointer
  swap提前结束真实在途tool call。原pending MCP lease继续由Supervisor独立保护exact old slot/manager，不阻塞或挂靠在
  execution handle retirement上；
- swap后segment安装或router初始化失败，不能回滚到旧exposure/surface；新handle由run terminalizer/close负责drain，old仍按
  retiring barrier收口；
- swap同步block若异常，latch run/session、保留old+incoming ownership，只允许terminalization/inspect/close，不在线选择
  “恢复旧面”或“完成新面”；
- retirement callback以handle_id+generation compare-and-remove，旧callback不能删除同ID复用或新generation handle。

stop/close取消规则：

1. stop_current_turn/explicit user stop先以run owner CAS安装kind=user_stop的RunTerminationIntent；
2. Host close/shutdown先以同一CAS安装kind=host_teardown；
3. 只有intent安装或join完成后才取消matching active segment；
4. first successful CAS是线性化事实，后到的stop/close只join，不覆盖已经冻结的intent；
5. driver捕获CancelledError时必须从run owner读取intent；无intent的裸取消是
   `execution_failure + stop_reason=runtime_execution_error`，不得猜成user_stop或host_teardown；
6. terminal candidate已冻结后新intent返回already_terminalizing，不修改RunEnd payload；
7. intent target segment identity若不再匹配，不能取消新装的resume segment，caller重新读取owner后决定join或发起新intent。

RunStart FULL confirmation后，entry driver原子把execution_handles ownership转移给该owner；转移完成前不得清理
boundary/child attempt，owner终结并drain后才retire/release handles。

## 8. 生命周期状态机

### 8.1 New run

    IDLE
      -> PREPARING
      -> COMMITTING
      -> ACTIVE
      -> SUSPENDED | TERMINAL

失败分支：

    PREPARING
      -> RETRYABLE_BLOCK -> IDLE
      -> TERMINAL_BLOCK  -> IDLE
      -> SESSION_LATCHED -> CLOSING_ONLY

    COMMITTING
      -> pre-commit failure -> IDLE
      -> committed/publication failed -> COMMITTED_PENDING_REPAIR

### 8.2 Resume

    SUSPENDED
      -> PREPARING_RESUME
      -> COMMITTING_RESUME
      -> ACTIVE

retryable failure回到同一SUSPENDED identity；不得创建新pending interaction或释放MCP pending lease。

### 8.3 Active publication rule

只有durable commit acknowledgement后：

- HostSession.active_run_id可设置；
- _active_state可发布；
- state status可从draft/suspended切RUNNING；
- model/tool continuation可开始。

PREPARING期间使用独立_boundary_attempt与draft state owner。
状态投影同时保存DurableRunExistence：PREPARING初始NONE；commit confirmation FULL后才为FULL；UNKNOWN与
PARTIAL_UNTRUSTED进入对应latch状态，不能继续使用ACTIVE bool表达。

## 9. Lock、线性化与source watermark

### 9.1 Host run lock

- Host API入口可在lock前做cheap friendly check；
- 获取_run_lock后必须重新做authoritative check；
- boundary prepare/commit/activate全程属于同一个Host-owned operation；
- 不允许另一个new run/resume插入。

`_boundary_attempt`的注册、取消请求与attempt identity compare-and-clear使用独立的event-loop同步临界区
（无await setter或小型threading.RLock），不依赖等待`_run_lock`。PREPARING owner本身持有`_run_lock`时，
stop/close必须仍能发出cancel并await shared completion；否则会形成“为了取消boundary先等boundary释放lock”的
死锁。attempt owner只有在identity匹配时才能clear，旧owner不得删除新retry attempt。

### 9.2 Runtime write lock

- 不得跨MCP await、compaction LLM或archive I/O持有SessionWriteCoordinator lock；
- durable atomicity由RuntimeSession.emit_many/write_events提供；
- boundary开始的reconciliation check用于避免已知无效工作；
- commit时writer reconciliation check仍是最终authority。

ContextCompactionService新增小型async commit port与明确result DTO：

    @dataclass(frozen=True, slots=True)
    class CompactionEventCommitResult:
        candidate_event_id: str
        committed_event: AgentEvent
        committed_through_sequence: int
        publication_status: Literal["completed", "failed_after_commit", "unavailable"]
        publication_errors: tuple[EventPublicationError, ...]

    class CompactionEventCommitPort(Protocol):
        async def commit_event(
            self,
            event: AgentEvent,
            *,
            state: LoopState | None,
        ) -> CompactionEventCommitResult: ...

        async def confirm_event(
            self,
            candidate: AgentEvent,
        ) -> BoundaryBatchConfirmation: ...

production adapter绑定现有`RuntimeSession.write_event()`，并将EventWriteResult或
EventPublicationAfterCommitError.result规范化为CompactionEventCommitResult；service不得假定
`write_event()`返回AgentEvent。component test可使用实现相同confirm语义的fake port。

冻结规则：

- Started/Completed/Failed/MemoryCandidatesProposed等compaction AgentEvent都通过该port；
- service可继续通过EventLog只读planning snapshot；
- production不得直接event_log.append/extend compaction facts；
- HostSession删除“service直接写log后再扫描并publish”的补偿路径；
- archive与event仍不是一个数据库事务，原有typed failure/recovery语义保留；
- 这不等于提前实现全局LiveRuntimeEventWriter。

Compaction terminal contract同时hard cut为：

- ContextCompactionStartedEvent新增required `terminal_event_id`；
- plan完成时预生成Started id与唯一terminal event id；Started保存自己的id与terminal_event_id；
  Completed/Failed二选一的`event.id`必须等于该terminal_event_id，并保存`started_event_id`；
- 继续使用ContextCompactionFailedEvent，不新增AbortedEvent；新增
  `termination_kind: Literal["failed", "cancelled", "recovered_interrupted"]`；
- `failure_stage`保留实际中断phase，并新增`recovery_terminalization`；
- Started commit前的cancel不需要terminal event；Started full commit后任何BaseException都必须尝试提交
  stable-id Failed terminal fact；
- publication failure不等于terminal commit failure；full confirmation后terminalization ownership已完成；
- none可用同candidate重试；partial/conflict latch ledger；unknown保留
  PendingCompactionTerminalization并阻止破坏性session close；
- recovery扫描按`terminal_event_id`查找Started无Completed/Failed，使用Started内的bounded facts构造
  `termination_kind="recovered_interrupted"`，同stable terminal id幂等补写；
- close在deadline内drain pending terminalization；失败时HostCore保留session、lease与retry入口，不释放
  EventLog/archive ownership。

pre-start planning/resolution failure仍可写`termination_kind="failed"`的ContextCompactionFailedEvent，但
`started_event_id=None`，它不冒充Started的terminal pair。`cancelled`与`recovered_interrupted`必须有
started_event_id且event.id必须匹配Started.terminal_event_id。reducer/Inspector按该pairing invariant拒绝同一
Started的双terminal或错误terminal id。

### 9.3 EventLog snapshot

新增只读helper：

    read_event_snapshot_through_current()
      -> EventLogReadSnapshot(
           through_sequence,
           events,
         )

实现顺序：

1. 读取当前committed high-water；
2. 读取并过滤sequence <= high-water的events；
3. rebuild transcript只消费该tuple；
4. 将high-water写入BoundaryTranscriptSnapshotFact。

boundary不要求RunStart紧邻source high-water；并发terminal completion等后续event可插入，但不能偷偷进入已冻结transcript。

### 9.4 MCP commit block

沿用MCP hard cut：

- required await在installation前；
- slot transition与surface swap是无await同步commit block；
- architecture fault后session latch且只允许inspect/close；
- boundary coordinator不重新实现MCP内部状态机。

## 10. PRE_RUN 精确算法

### 10.1 Ingress

HostSession.run_turn/stream_turn收到输入时立即：

1. 生成boundary_id、run_id、turn_id、reply_id；
2. capture user_observed_at_utc；
3. 创建immutable NewRunBoundaryInput；
4. 注册BoundaryAttempt owner；
5. 进入shared run execution handle。

run_turn与stream_turn必须调用同一个pipeline；差别只在结果观察方式。

### 10.2 锁内admission

获取_run_lock后按顺序：

1. lifecycle必须OPEN；
2. _mcp_installation_faulted必须false；
3. 无stopping run；
4. 无active run；
5. 无pending interaction；
6. 无其他boundary attempt；
7. RuntimeSession.reconciliation_required必须false；
8. ledger structural latch不允许在线清除；
9. HostCore registry/session ownership仍匹配。

失败不得调用MCP prepare、compaction或任何durable mutation。

### 10.3 Pure contract resolution

在远端wait前先做：

1. snapshot plan state/version；
2. resolve run model target；
3. validate preset session/plan permission mode；
4. resolve RunPermissionSnapshot；
5. validateworkspace/memory-domain/run identity。

目标是让明显的model/permission配置错误在等待required MCP前fail fast。

### 10.4 Recovery maintenance

当前首次run前的repair_dangling_children必须：

- 作为明确RECOVERY_MAINTENANCE phase；
- 在RunStart前完成；
- 失败视为fatal，不继续；
- durable repair events由正常RuntimeSession writer提交；
- 长期迁到Host session reopen/recovery后从PRE_RUN删除。

### 10.5 MCP prepare / required / install

固定顺序：

    supervisor.prepare(configs)
      -> join/start required attempts
      -> await_required(ticket)
      -> drain installable candidates
      -> validate complete installation
      -> synchronous commit_slot_transition/surface swap
      -> freeze installed snapshot
      -> materialize pending audit ownership

optional failure：

- 保持旧valid surface或安装DEGRADED/STARTING projection；
- disposition可为PROCEED_DEGRADED；
- diagnostics必须bounded/redacted。

required failure：

- RETRYABLE_BLOCK；
- 不创建RunStart；
- 不标ACTIVE；
- background attempt ownership仍由supervisor管理。

### 10.5.1 Freeze execution surface

MCP installation commit后、任何descriptor artifact或RunStart构造前，composition root调用：

    CapabilityRuntime.freeze_execution_surface(
        builtin_snapshot=...,
        mcp_installation=...,
        custom_binding_snapshot=...,
    ) -> FrozenCapabilityExecutionSurface

该调用通过`CapabilityExecutionSurfaceProvider.snapshot_descriptors()`收集descriptor，并从同一ToolRegistry/MCP slot
snapshot收集binding identity；随后执行descriptor/binding完整性校验、生成canonical descriptor artifacts并冻结
CapabilityExecutionSurfaceIdentityFact。它是pure/process-local snapshot + artifact materialization，不解析catalog、
active skill或user-context projection。

descriptor artifact写失败为pre-RunStart fatal，不能创建run。freeze完成后直到RunStart commit不得再注册/替换tool
binding；晚到MCP candidate留给下一safe point。RunStart后的initial exposure只能消费该frozen surface，不能再次从live
registry snapshot descriptors。

### 10.6 Transcript / compaction

固定顺序：

1. read pre-compaction EventLog snapshot；
2. rebuild pre-compaction transcript；
3. estimate using resolved target；
4. below threshold：skip；
5. above threshold：run preflight compaction；
6. publish/observe typed compaction terminal facts；
7. read new final EventLog snapshot；
8. rebuild canonical prior transcript，RunStart user只读current_user_message typed field；
9. append plan runtime messages astyped draft inputs；
10. freeze BoundaryTranscriptSnapshotFact。

Auto compaction失败：

- ContextCompactionFailedEvent仍是durable maintenance truth；
- boundary继续，交给ContextCompiler判断最终是否容纳；
- disposition PROCEED_DEGRADED；
- 若final transcript rebuild本身失败则fatal。

所有compaction event commit必须经过CompactionEventCommitPort。因此若reconciliation在boundary
prepare期间被并发latch，下一次compaction write会fail closed，不能继续绕过RuntimeSession写入ledger。

Manual/mid-turn compaction不进入此pipeline。

#### 10.6.1 Preflight compaction cancel/commit ownership

ContextCompactionService必须维护`started_commit_state`与`terminal_candidate`，并围绕整个
Started→summarize→artifact→terminal过程捕获`BaseException`：

1. Started未commit：取消owned provider/archive work，不写terminal；
2. Started full commit：构造stable-id Failed(`termination_kind="cancelled"`或`"failed"`)；
3. 在Host-owned、shielded且bounded的terminalization task中调用CompactionEventCommitPort；
4. commit调用自身被cancel/抛错时用`confirm_event()`分类none/full/partial/conflict/unknown；
5. full：fold committed event并允许原CancelledError继续传播；
6. none：在deadline内重试同candidate；
7. partial/conflict：latch ledger structural reconciliation；
8. unknown：注册PendingCompactionTerminalization，boundary不得清掉其owner，close必须drain；
9. terminal fact确认后才允许boundary把该compaction引用写入final transcript fact。

若port因已有RuntimeSession/EventLog reconciliation latch在commit前拒绝，confirmation为NONE但本次不得盲目
重试；stable terminal candidate进入PendingCompactionTerminalization并继承该latch，等待显式ledger/reducer
repair或close/reopen。若EventPublicationAfterCommitError携带committed result，则直接按FULL处理，不把它塞回
pending queue。

不得用`except Exception`承担取消收口；cleanup捕获BaseException只用于恢复ownership，完成后仍重抛原
CancelledError/KeyboardInterrupt/SystemExit。

### 10.7 Final freeze

基于最终facts创建：

- reference already-frozen canonical descriptor artifacts；
- CapabilityResolveBasis；
- CurrentUserMessageFact；
- PreparedNewRunBoundary；
- AgentRunDraft；
- RunStartEvent.new_run_boundary；
- pending MCP audit events。

`RunStart.created_at`在这里生成；它必须不早于CurrentUserMessageFact.observed_at_utc，但不与观察时间相等。

RunStart.current_user_message保存exact user text/observation；不得只保存chars/hash。RunStart与每个pending audit
ID在进入boundary DURABLE_COMMIT前生成并冻结。initial exposure要等post-commit
memory hook后才能resolve，因此只要求在它自己的event commit前生成stable ID。所有已知repair/terminal candidate
也必须在对应Started/commit attempt前冻结ID。retry或commit confirmation不得重新构造不同ID或不同payload。

final freeze后不允许：

- 重新读取session default permission；
- 重新resolve model target；
- 安装另一个MCP candidate；
- 把晚到EventLog event加入transcript；
- 修改active skill names。

### 10.8 Durable commit

唯一batch：

    RuntimeSession.emit_many(
        RunStartEvent,
        *pending_mcp_installation_audits,
        state=draft.state,
    )

Invariant：

- RunStart是batch第一条；
- audits紧随其后；
- batch所有event id在commit前稳定生成；
- 全batch连续sequence；
- commit失败不留partial；
- committed slice确认后才ack audit ownership；
- same event ids重试必须使用EventLog confirm_batch幂等确认；
- post-commit publication failure不能误当pre-commit failure。

`await emit_many()`正常返回不是唯一commit acknowledgement。boundary coordinator必须围绕整个commit await
捕获`BaseException`，包括CancelledError。异常后启动Host-owned confirmation task，并用`asyncio.shield()`
保证外层取消不会连带取消confirmation；若caller已经处于cancelling状态，confirmation/repair task仍登记在
HostSession attempt上供close drain，不能成为unowned task。

确认算法：

    try:
        result = await runtime_session.write_events(stable_batch)
    except BaseException as original:
        confirmation = await shield_owned(confirm_batch(stable_batch))
        match confirmation.status:
            case NONE:
                mark_not_committed(); rollback_draft(); raise original
            case FULL:
                mark_committed(); fold_and_repair_publication(); terminalize_if_cancelled()
                raise original
            case PARTIAL | CONFLICT:
                latch_ledger(); retain_attempt(); raise EventReconciliationRequired from original
            case UNKNOWN:
                latch_boundary_mutation(); retain_attempt(); raise CommitOutcomeUnknown from original

`FULL` repair必须catch up reducer/publisher至少到confirmation返回的actual high-water，acknowledge batch中已
commit的MCP audits，并在重新抛出取消/observer错误前把attempt切到committed。new run若无法继续model loop，
随后用stable RunEnd candidate terminalize。`PARTIAL`不能通过普通reducer rebuild解除；只能专门ledger
verify/repair或close/reopen。

### 10.9 Activate

commit acknowledgement后：

1. 构造CommittedHostRunEntry；
2. HostSession从PREPARING切ACTIVE；
3. publish active_run_id/_active_state；
4. boundary attempt commit_state=committed；
5. Agent进入stream_committed_entry()；
6. close/stop改为drain active owner。

## 11. Post-RunStart 初始化

### 11.1 Committed execution terminalizer

Host或child RunStart一旦FULL commit，AgentRuntime只能通过统一最外层执行入口运行：

    stream_committed_entry(
        entry: CommittedRunEntry,
        working_set: RunWorkingSet,
    ) -> AsyncIterator[AgentEvent]

该入口外层捕获`BaseException`，覆盖：plan-entry audit、memory hook、CapabilityExecutionSurface revalidation/rebind、provider
resolver、projection artifact write、typed exposure commit、subagent parent snapshot refresh、context compile/model/tool
loop以及interaction-specific router。规则：

1. RunStart.terminal_run_end_event_id在entry commit前已冻结；
2. run terminalizer按run_id单写者/CAS取得ownership，首次owner冻结RunEnd payload；
3. state已由匹配ID RunEnd full commit时只fold/join，不重复terminalize；
4. CancelledError只在run owner已有RunTerminationIntent时映射对应user_stop/host_teardown；无intent则映射
   `execution_failure + runtime_execution_error`。普通loop failure保留LoopState中的详细RunStopReason；其他
   BaseException按stage映射post_commit_initialization_error/runtime_publication_failure/interaction_router_error/
   runtime_execution_error之一，不使用异常message生成reason；
5. RunEnd commit也执行stable candidate + none/full/partial/conflict/unknown confirmation；
6. FULL后归约/ack并重新传播原异常；PARTIAL/CONFLICT latch ledger；UNKNOWN保留PendingRunTerminalization与
   destructive-close blocker；
7. outer terminalizer cleanup失败不得覆盖原异常因果链，但结构化terminalization error必须可inspect；
8. Host正常suspend不是异常，不写RunEnd；正常finished/aborted/failed也必须使用同一terminal event ID；
9. child返回WAITING_USER不是合法suspend：child terminalizer冻结
   `RunEnd(status=failed, terminalization_kind=execution_failure,
   stop_reason=subagent_pending_unsupported)`并先在child ledger确认FULL；只有child RunEnd FULL/fold后，parent才提交
   SubagentRunFailedEvent/TaskFailed cascade并释放handle/slot；
10. child RunEnd为UNKNOWN/PARTIAL/CONFLICT时不得把parent graph写成普通failed，必须按19.2 child commit/terminal
    reconciliation矩阵保留owner和capacity。

SESSION_REOPEN recovery扫描RunStart无RunEnd时，必须使用RunStart.terminal_run_end_event_id构造
`recovered_interrupted` RunEnd candidate并执行同一confirm算法；不能生成新ID。若相同terminal ID已有不同payload，
视为EventIdConflict/ledger latch。

该规则同样适用于child RuntimeSession。只有同进程live reconcile且matching child owner确实仍存活时才允许暂时保持
running；SESSION_REOPEN/owner missing不得套用live例外，必须先修复child RunEnd，再按19.2生成parent terminal reference。

同一规则包住`stream_committed_interaction_resume(entry, continuation, ...)`。continuation boundary full commit后，
interaction router任意异常都由原run的terminalizer收口；不能只为memory hook保留特例。HostSession/transport
observer无权直接构造RunEnd。

因此“正常suspend不写RunEnd”只适用于可由Host pending router继续的host run。V1 child pending不可路由，必须在
child ledger留下稳定terminal事实，不能只结束parent graph而让child RunStart悬空。

### 11.2 初始化顺序

固定顺序保持现有长期契约：

1. pending PlanModeEnteredEvent；
2. memory on_turn_start hook；
3. 从RunStart前冻结的execution surface调用`resolve_exposure_projection()`，产生initial CapabilityExposurePlan；
4. refresh subagent parent capability snapshot；
5. emit CapabilityExposureResolvedEvent；
6. 进入model loop；
7. 每个model step resolve ResolvedModelCall；
8. build ContextFactSnapshot；
9. compile/send。

Capability exposure不得提前到memory hook之前，也不得晚于provider tools/prompt构造。

Memory hook失败：

- RunStart已经存在；
- 写typed hook error；
- 正常finalize RunEnd；
- 不能回滚RunStart或把run说成“未创建”。

Capability rebind/resolve/exposure commit/subagent refresh等其他异常与memory hook完全同级，统一进入11.1；不允许
在各步骤散落只捕获Exception的局部terminalizer。

## 12. PRE_INTERACTION_RESUME 精确算法

### 12.1 Ingress与锁内identity

lock外friendly check后，lock内必须重验：

- session OPEN且未faulted；
- no active/stopping/preparing attempt；
- pending interaction仍存在；
- interaction id/kind精确匹配；
- suspended run id/state token匹配；
- MCP pending lease identity匹配（适用时）；
- RuntimeSession无reconciliation latch。

两个并发resolver都通过lock外检查时，只有第一个能通过锁内identity。第二个不得执行MCP mutation。

### 12.2 Rebuild original run contract

在MCP mutation前：

1. 从EventLog读取该run唯一RunStart；
2. 校验run/turn/reply；
3. rebind ResolvedModelTarget；
4. 从RunStart permission fields重建RunPermissionSnapshot；
5. 与suspended state中的snapshot做full equality；
6. 不一致时contract error/session latch；
7. 原RunStart缺字段直接hard-cut schema error。

不得只信任mutable LoopState。

### 12.3 MCP required/install

与PRE_RUN共用MCP helper，但failure policy按pending语义：

- pending自身binding disable/remove/reconfigure：TERMINAL_BLOCK；
- unrelated required server unavailable：RETRYABLE_BLOCK，保留pending/lease；
- same config临时retry失败且leased binding仍有效：RETRYABLE_BLOCK；
- optional unrelated update：可安装，但不能widen旧run exposure。

### 12.4 Continuation exposure

输入：

- original CapabilityResolveBasis；
- original exposure fact/plan；
- original CapabilityExposureSemanticFact；
- current CapabilityExecutionSurfaceIdentityFact与installed execution surface；
- exact pending binding identity；
- original permission snapshot。

先调用唯一派生函数：

    derive_continuation_basis(
        original: CapabilityResolveBasis,
        *,
        continuation_owner: CapabilityExposureOwnerFact,
        current_mcp: McpInstallationReferenceFact,
        current_execution_surface: FrozenCapabilityExecutionSurface,
    ) -> CapabilityResolveBasis

派生规则：

- 保留original workspace identity、memory domain、permission snapshot、plan_active、active_skill_names、user intent、
  prior transcript fingerprint以及process-local raw user/prior/workspace inputs；
- 生成新的basis_id，设置basis_kind=continuation与source_basis_id/fingerprint；
- 替换owner、当前MCP installation与current execution surface identity；
- 从完整event-safe payload重新计算basis_fingerprint，不修改original basis；
- 禁止从resume空输入、当前LoopState scratchpad或新的用户意图猜测basis。

算法：

1. 无论MCP installation是否变化，都用derived continuation basis在current execution surface和current local
   projection sources上解析current candidate；projection resolve必须是无网络、无durable mutation的pure read；
2. current candidate的`exposure_semantic_fingerprint`与original相同：reuse；
3. 仅MCP installation、descriptor names或registry generation相同都不足以reuse；
4. semantic变化时执行`monotonic_narrow(original_exposure, current_candidate)`；
5. authorization entry只有name + descriptor_fingerprint + binding_fingerprint全部相等且current disposition不更宽时
   才保留；否则hidden/deny并记录reason；
6. catalog/active-skill visible source entry只有projection_entry_id + content_fingerprint完全相等才保留；provider、
   source kind、stable name任一归属变化，或内容变化/撤销，都删除；
7. current新增descriptor、binding、catalog item或active injection全部忽略；
8. 对保留entry只复制source_entry_id指向它的original exact rendered fragments；删除entry时删除其全部
   index/detail/body fragments；
   container无entry fragment后同时删除原prefix/suffix/container_wrapper static；整个projection无entry后也删除
   projection_wrapper static。不得调用catalog/active renderer，不得重新截断、重排、扩展detail
   或生成original prompt中不存在的文本；remaining fragments按original order拼接并写新的aggregate artifact；
9. 验证authorization entries、direct/deferred/callable sets、catalog entries与active entries都是original子集；
10. 计算new exposure_semantic_fingerprint与包含continuation owner/basis的exposure_fact_fingerprint；
11. 生成continuation exposure fact/event并在current live surface精确rebind可调用项。

若subset invariant失败，SESSION_LATCHED。

`CapabilityRegistry.snapshot().generation`只允许作为单次resolve的diagnostic，不进入reuse decision或durable
identity。即使execution surface未变，local skill catalog/active prompt变化也会改变semantic fingerprint并触发
narrowing；反之process generation变化但完整authorization与projection semantic相等仍可reuse。

这里的subset同时是内容可见性subset：相同source entry在original只出现name/index时，continuation不得因为其他skill被
撤销而获得description/detail。当前`render_catalog_prompt()`的hybrid/compact/truncated模式只用于initial exposure；
continuation走fragment intersection renderer。

### 12.5 Durable continuation commit

一个batch提交：

    RuntimeSession.emit_many(
        *pending_mcp_installation_audits,
        CapabilityExposureResolvedEvent,
        RunInteractionResumeBoundaryEvent,
        state=suspended_state,
    )

顺序冻结为audits在前、typed continuation exposure居中、boundary event在后，使boundary event引用的
installation audit与effective exposure都已在同batch更早位置。

commit acknowledgement后：

- ack pending audits；
- fold committed continuation exposure/boundary facts，但此时尚未切active；
- 若resume attempt有incoming handles，调用`swap_execution_handles_after_continuation_commit()`，incoming转给原
  CommittedRunExecutionOwner、old进入retiring；若无incoming则重新验证current handle identity并原样复用；
- install verified target/permission/exposure到typed RunWorkingSet；
- 调用install_segment执行termination intent/terminal state最终recheck；安装新generation RunExecutionSegmentOwner后，
  suspended state才切active；
- route interaction-specific continuation。

swap或segment install后的任何异常都走原run stable terminalizer；不得恢复旧working set/exposure/handles。incoming
handles由run owner拥有，old handles由retiring map拥有；原pending MCP lease由Supervisor单独拥有，因此异常路径不得
把它转移到handle tracker，也不得在terminal fact commit确认前释放。
swap_skipped_terminating或RunSegmentInstallBlocked不是可重试resume：continuation fact已FULL，必须放弃启动segment、retire
attempt-owned incoming（若尚未swap）并join既有termination intent直到RunEnd；不得把pending恢复成“尚未resume”。
swap_skipped_terminating时跳过RunWorkingSet replacement与segment install；无incoming/reuse-current branch也必须在working-set
replacement前后各检查一次intent/terminal state。swap成功后intent才到达时，working set可以反映已committed continuation，
但segment install仍必须blocked，且没有model/tool执行能观察到该process-local过渡态。

commit前失败：

- pending/lease保持；
- state不active；
- retry产生新boundary attempt id。

“commit前失败”必须经过10.8的stable-batch confirmation后才能成立。若resume commit await被取消或抛任意
BaseException：

- NONE：保持原pending/token/lease，可用新boundary id重试；
- FULL：fold exposure与boundary facts，ack audits，按committed语义处理pending；不得回到pre-commit状态；
- PARTIAL/CONFLICT：ledger latch，pending和lease保留给close/recovery；
- UNKNOWN：boundary mutation latch并保留attempt owner；
- cancellation在上述ownership动作完成后继续向caller传播。

### 12.6 Approval resume

ApprovalResolution完成原permission WAIT，不得再次调用同一个permission gate造成循环。

必须：

- validate confirmed call ids；
- capability exposure access recheck；
- exact binding/descriptor recheck；
- user confirmation rules写入已有typed event；
- execute confirmed calls；
- denied calls写gate/result；
- tool results后进入mid-turn safe point。

### 12.7 Plan resume

Plan resolution使用workflow-specific policy：

- interaction kind/id；
- plan revision/exit state；
- run read-only permission contract；
- capability exposure access for workflow tool；
- approve/cancel等run-ending语义；
- 不套用普通tool permission re-approval。

### 12.8 MCP input-required resume

必须：

1. exact pending lease/binding generation；
2. continuation capability exposure gate；
3. original run permission gate；
4. DENY写typed gate + terminal tool result；
5. WAIT_FOR_USER V1转fail-closed deny；
6. ALLOW才borrow pending lease并resume；
7. terminal result durable commit确认后complete lease；
8. 再进入mid-turn compaction/model continuation。

### 12.9 Host/user plan cancel与force-exit

`HostSession.exit_plan_workflow()`必须纳入API hard cut，并拆成两个互斥分支。

#### A. 存在suspended plan interaction

- 走完整PRE_INTERACTION_RESUME，interaction kind仍为plan；
- force-exit可terminalizepending question；普通cancel只接受允许cancel的pending exit；
- 从原RunStart重建target，当前run继续使用原preset read-only permission snapshot；
- commit continuation exposure/boundary后写PlanExitResolvedEvent（适用时）、PlanModeExitedEvent与RunEnd；
- agent exit_plan路径可写tool result；host/user路径没有tool call时不得伪造tool result；
- durable terminal facts确认后释放pending state/lease；
- 不进行follow-up model call。

#### B. plan active但不存在suspended run

这是`HOST_PLAN_WORKFLOW_MUTATION`，不是PRE_RUN或PRE_INTERACTION_RESUME：

- 不创建LoopState、AgentRunDraft、RunStart、RunEnd或active_run_id；
- 不执行MCP safe point、model target resolution或capability exposure；
- 使用PlanWorkflowStateFact中原entry event context作为storage attribution，并生成独立
  `workflow_operation_id`；该attribution不表示新run；
- PlanModeExitedEvent schema增加
  `transition_owner: Literal["agent_run", "host_workflow"]`与条件required的
  `host_workflow_operation_id`；
- event通过RuntimeSession writer提交并遵守stable ID + BaseException confirm算法；
- commit确认后才把HostSession plan reducer/state切inactive；
- restored_permission_mode/policy必须是non-null preset expansion，只恢复future run stored default；
- plan active期间已经存在的任何run contract永远不被放宽。

Event validator要求`agent_run`时host_workflow_operation_id=None且carrier run必须有唯一RunStart；
`host_workflow`时operation id required，carrier context必须等于PlanWorkflowStateFact entry attribution，且该
operation id不得出现在RunStart/RunEnd。Plan reducer按workflow id/revision归约，不按carrier run lifecycle猜测。

旧的`new_state() -> active_run_id=state.run_id -> emit PlanModeExited -> _finish_active_run()`路径删除并加入
grep/architecture guard。

## 13. Gate policy matrix

| Interaction | Target | Permission | Capability | Binding | Result |
|---|---|---|---|---|---|
| new run | fresh resolve | capture preset snapshot | initial resolve after memory hook | current installation | model loop |
| approval | original RunStart | 不重新WAIT | 必须recheck | 必须recheck | confirmed/denied tool result |
| plan | original RunStart | 原read-only contract | workflow capability recheck | descriptor if applicable | workflow continuation |
| MCP input | original RunStart | 必须recheck | 必须recheck | exact pending identity | resume或terminal deny |
| host plan exit（无suspended run） | 无target/run | 仅恢复future stored default | 不解析 | 不解析 | workflow event only |

禁止用一个common gate participant覆盖各行差异；host workflow mutation甚至不是run gate。

## 14. Failure disposition 表

| Phase / failure | Disposition | Durable run exists | Pending保留 | Session可继续 |
|---|---|---:|---:|---:|
| lifecycle/admission reject | RETRYABLE_BLOCK或TERMINAL_BLOCK | 否 | 是（resume） | 视原因 |
| reducer reconciliation可repair | RETRYABLE_BLOCK | 否 | 是 | repair后 |
| ledger structural latch | SESSION_LATCHED | 否 | 是 | 仅close/reopen |
| model target/permission invalid | TERMINAL_BLOCK | 否 | 不适用 | 配置修复后 |
| optional MCP failure | PROCEED_DEGRADED | 否/原run | 是 | 是 |
| required MCP unavailable | RETRYABLE_BLOCK | 否/原run | 是 | 可重试 |
| MCP post-linearization fault | SESSION_LATCHED | 否/原run | 是 | 仅close/reopen |
| auto preflight compaction failed | PROCEED_DEGRADED | 否 | 不适用 | 是 |
| transcript final rebuild failed | TERMINAL_BLOCK | 否 | 不适用 | 是 |
| RunStart pre-commit failure | RETRYABLE_BLOCK | NONE | 不适用 | 是 |
| RunStart commit成功、publication失败 | COMMITTED_BUT_PUBLICATION_FAILED | FULL | 不适用 | 需terminalize/repair |
| RunStart后capability/exposure/model/router异常 | COMMITTED_EXECUTION_FAILED | FULL | 依interaction | stable RunEnd后可继续/close |
| RunStart/resume commit outcome无法确认 | COMMIT_OUTCOME_UNKNOWN | UNKNOWN/原run FULL | 保留 | 禁止mutation，仅inspect/repair/close |
| RunStart/resume partial/conflicting batch | SESSION_LATCHED | PARTIAL_UNTRUSTED/原run FULL | 保留 | ledger repair或close/reopen |
| resume audit pre-commit failure | RETRYABLE_BLOCK | 原run | 是 | 是 |
| resume audit commit成功、publication失败 | COMMITTED_BUT_PUBLICATION_FAILED | 原run | 依terminal fact | 需fold committed slice |
| pending自身binding变化 | TERMINAL_BLOCK | 原run | 直到terminal result commit | 是 |
| compaction Started后cancel，terminal full commit | PROCEED_DEGRADED或cancel | 否/原run | 不适用 | 是 |
| compaction terminal outcome unknown | COMMIT_OUTCOME_UNKNOWN | 否/原run | 不适用 | 禁止破坏性close，需drain/confirm |

## 15. Publication-after-commit

### 15.0 BaseException与commit outcome authority

本节不仅处理EventPublicationAfterCommitError。任何在`write_events()`调用边界逃出的BaseException都必须先
经过stable candidate batch confirmation，因为当前writer在等待publisher future前已经完成同步ledger commit，
未来async writer也可能在commit acknowledgement丢失后被cancel。

唯一authority是EventLog.confirm_batch返回的none/full/partial/conflict与锁内actual high-water，不是Python
exception type、task cancelled flag或`HostRunBoundaryAttempt.commit_state`的旧值。attempt state只在确认结果后
更新。confirmation itself失败时进入COMMIT_OUTCOME_UNKNOWN并保留owned repair task；不得清理draft、pending、
lease或MCP audit queue。

### 15.1 New run

若EventPublicationAfterCommitError包含已commit RunStart batch：

1. 从committed slice确认RunStart与audits；
2. acknowledge committed MCP audits；
3. boundary attempt标记committed；
4. 不得清掉draft state/owner；
5. 尝试publisher/reducer catch-up；
6. 若不能继续model loop，写RunEnd(aborted/failed, reason=runtime_publication_failure)；
7. RunEnd也无法commit时latch session，保留close/recovery ownership；
8. 向caller传播结构化post-commit错误。

不得留下无人拥有的dangling RunStart。

相同步骤也适用于`cancel_during_run_start_publication_wait`。full confirmation后即使原异常是CancelledError，
也必须先ack audits、fold committed RunStart并写/安排stable RunEnd terminal candidate，随后才传播取消。

### 15.2 Resume

若boundary/audit已commit但publication失败：

- fold committed events；
- acknowledge audit；
- 根据是否已有terminal result决定pending lease；
- 不重复写相同boundary event；
- pending state不得回到commit前假象；
- 向caller传播post-commit错误。

`cancel_during_resume_boundary_publication_wait`同样先confirm；full后continuation boundary已经是事实，pending
折叠与MCP lease完成规则必须按committed slice执行，不能因为caller被cancel而保留一份“尚未resume”的假象。

MCP suspension event和resume terminal tool-result batch还各自拥有一层stable candidate confirmation。任意
`BaseException`后统一裁决：`NONE`才允许abort suspension reservation或恢复原pending state；`FULL`必须confirm/fold并推进
唯一Supervisor lease owner；`PARTIAL/UNKNOWN`必须latch、保留reservation/lease与可定位pending carrier。不得在写入前clear
pending后只针对`EventPublicationAfterCommitError`做恢复，也不得把`CancelledError`直接解释为pre-commit失败。

### 15.3 Unknown confirmation retry

V1只提供一个窄恢复入口：

    retry_boundary_commit_confirmation(boundary_id) -> BoundaryBatchConfirmation

它只能读取attempt中冻结的candidate batch，不能重新prepare、改变payload或提交新ID。后续结果：NONE回滚draft；
FULL执行committed repair；PARTIAL/CONFLICT升级ledger latch；仍UNKNOWN继续保留owner。只有上述转换能解除
boundary mutation latch。close若在deadline内仍无法确认，HostCore不得把session从registry移除或释放event/MCP/
terminal/workspace ownership；caller可在storage恢复后重试close/confirmation。外层deadline不能杀死已进入同步
PostgreSQL driver的worker；超时只停止caller等待，worker仍由attempt拥有并在结束时compare-and-complete。

## 16. Cancellation、close与drain

### 16.1 Boundary task owner

run entry从INGRESS开始就必须有Host-owned task/attempt。不能等到RunStart后才创建_run_owned task。

HostSession close顺序：

1. lifecycle -> CLOSING；
2. 若已有committed run，CAS安装或join host_teardown RunTerminationIntent；
3. detach/drain stream observer与run_turn waiter observation，不触碰execution owner；
4. cancel boundary attempt（若有）；
5. bounded await attempt completion；
6. 若boundary commit_state=committed，按active/repair路径terminalize；
7. drain PendingRun/CompactionTerminalization与其他boundary repair owner；
8. cancel matching active segment并drain run owner；
9. finalize suspended run；
10. drain subagents/MCP/terminal；
11. release shared resources。

### 16.2 Cancel before commit

- discardAgentRunDraft；
- clearPREPARING；
- noRunStart；
- already durable compaction/MCP events不回滚；
- pending MCP audit ownership保留给下一boundary；
- compaction Started已full commit时必须按10.6.1补
  ContextCompactionFailedEvent(termination_kind="cancelled")；unknown terminal commit会保留close blocker。

### 16.3 Cancel after commit

- run已存在；
- activate minimal owner或进入committed repair；
- caller先CAS安装明确user_stop/host_teardown intent；
- 只取消intent中matching segment identity；
- emitRunEnd；
- 不把它降格为pre-commit cancellation。

### 16.4 stop_current_turn() during PREPARING

`stop_current_turn()`必须认识boundary attempt，而不是只查看`_active_task/_active_state`：

- PRE_RUN PREPARING且commit confirmation为NONE：cancel/drain attempt，返回
  `HostBoundaryStoppedBeforeCommit(... durable_run_existence=NONE)`；
- PRE_RUN COMMITTING时先执行15.0 confirmation；NONE同上，FULL按已存在run terminalize；
- PREPARING_RESUME的原run始终FULL：resume boundary NONE时先取消pending resume attempt，再由原
  CommittedRunExecutionOwner CAS安装user_stop intent后abort/RunEnd；resume boundary FULL时先fold continuation、
  handle swap（若有）后安装intent再abort；UNKNOWN/PARTIAL时
  latch并保留pending/owner，不报告stop成功；
- FULL/已ACTIVE：先CAS安装user_stop intent，再沿用AgentRunResult + RunEnd路径；
- PARTIAL/CONFLICT：返回HostBoundaryStopUncertain(existence=PARTIAL_UNTRUSTED)并latch；
- UNKNOWN：返回HostBoundaryStopUncertain(existence=UNKNOWN)并保留confirmation owner；
- pre-commit stop不写RunEnd，因为不存在RunStart；reason只保存在bounded live diagnostic；
- stop caller取消不转移boundary repair ownership。
- active stream存在时先detach/drain observer，再cancel driver，使abort/RunEnd不会阻塞在满queue。

API返回hard cut为：

    StopCurrentTurnResult = AgentRunResult | HostBoundaryStopResult | None

两种HostBoundaryStopResult都不是AgentRunResult，也不得伪造run status或把UNKNOWN/PARTIAL写成false。

### 16.5 Deadline

Host boundary不创建一个覆盖所有participant的模糊总deadline：

- required MCP使用每server absolute deadline；
- compaction使用自己的model/tool timeout；
- close drain使用Host close deadline；
- event commit使用storage deadline；
- diagnostics记录各阶段duration。

## 17. Durable audit与Inspector

### 17.1 不新增万能SafePointEvent

durable truth分工：

- Host PRE_RUN commit marker：RunStartEvent.new_run_boundary；
- child entry commit marker：RunStartEvent.subagent_run_entry；
- current user durable truth：RunStartEvent.current_user_message；
- MCP installation：McpCapabilitySnapshotInstalledEvent；
- preflight compact：ContextCompaction*Event + boundary id；
- initial/continuation exposure：CapabilityExposureResolvedEvent；
- PRE_INTERACTION_RESUME：RunInteractionResumeBoundaryEvent；
- gate：CapabilityGateDecisionEvent；
- terminal outcome：ToolResult/RunEnd。

### 17.2 Compaction correlation

ContextCompactionStarted/Completed/FailedEvent新增semantic字段：

    host_boundary_id: str | None
    host_boundary_kind: Literal["pre_run"] | None

Invariant：

- phase=preflight => host_boundary_id/kind required；
- manual/mid_turn => both None；
- field存在不是legacy fallback。

compaction event仍可归属旧latest run context；Inspector依boundary id把它投影到即将开始的新run preparation。

### 17.3 Inspector normalized shape

    run_boundary = {
      "boundary_id": ...,
      "kind": "pre_run",
      "run_entry_kind": "host",
      "status": "committed",
      "durable_run_existence": "full",
      "observed_at_utc": ...,
      "current_user_message_id": ...,
      "current_user_content_sha256": ...,
      "source_through_sequence": ...,
      "preflight_compaction": {...} | None,
      "permission_snapshot_id": ...,
      "target_fingerprint": ...,
      "mcp_installation_id": ...,
      "capability_basis_fingerprint": ...,
      "execution_surface_fingerprint": ...,
      "descriptor_set_fingerprint": ...,
      "execution_binding_set_fingerprint": ...,
      "catalog_projection_fingerprint": ...,
      "active_skill_projection_fingerprint": ...,
      "exposure_semantic_fingerprint": ...,
      "exposure_fact_fingerprint": ...,
      "run_start_event_id": ...,
      "run_start_sequence": ...,
      "diagnostics": [...],
    }

    continuation_boundaries = [
      {
        "boundary_id": ...,
        "interaction_id": ...,
        "interaction_kind": ...,
        "original_run_start_event_id": ...,
        "mcp_installation_id": ...,
        "source_exposure_semantic_fingerprint": ...,
        "effective_exposure_semantic_fingerprint": ...,
        "exposure_transition": "reused" | "narrowed",
        "source_exposure_fact_fingerprint": ...,
        "effective_exposure_fact_fingerprint": ...,
        "committed_audit_event_ids": [...],
        "sequence": ...,
      }
    ]

    child_run_entry = {
      "run_entry_kind": "subagent_child",
      "subagent_run_id": ...,
      "subagent_task_id": ... | null,
      "entry_mode": "task_backed" | "primitive_run",
      "parent_runtime_session_id": ...,
      "parent_run_id": ...,
      "task_artifact_id": ...,
      "child_result_render_policy": {
        "renderer_version": ...,
        "max_summary_chars": ...,
        "max_artifact_refs": ...,
        "policy_fingerprint": ...,
      },
      "current_user_message_id": ...,
      "current_user_content_sha256": ...,
      "exposure_owner_kind": "subagent_run_start",
      "child_terminal_reference": {
        "child_runtime_session_id": ...,
        "child_run_id": ...,
        "terminal_event_id": ...,
        "terminal_sequence": ...,
        "terminal_status": ...,
      } | null,
      "child_result_handoff": {
        "handoff_kind": "explicit" | "inferred",
        "renderer_version": ...,
        "render_policy_fingerprint": ...,
        "max_summary_chars": ...,
        "max_artifact_refs": ...,
        "explicit_source_tool_call_id": ... | null,
        "result_id": ...,
        "result_artifact_id": ...,
        "rendered_payload_sha256": ...,
        "usage_status": ...,
        "tool_call_count": ...,
      } | null,
      "run_start_event_id": ...,
      "run_start_sequence": ...,
    }

Inspector：

- 只读durable facts；
- 不查询当前HostSession重算；
- 不按event sequence猜compaction与下一run关系；
- schema缺失/identity冲突显示contract_error；
- old event在hard-cut DB中不支持；
- host plan workflow mutation单独投影workflow_operation_id与entry attribution，不显示成run；
- compaction Started无terminal显示pending_terminalization或contract_error，不静默忽略；
- commit outcome unknown显示live/session latch；historical ledger只在confirm/repair后显示committed或structural error。
- subagent child显示同形状run_entry/exposure projection，但owner_kind=subagent_run_start，并跨parent runtime join
  spawn/profile facts；不得伪造host boundary。
- child terminal reference通过EventLogLocator跨child runtime校验；引用缺失、sequence/status不符或parent terminal早于child
  terminal均显示contract_error，不按当前graph status猜测。
- authorization与projection counts来自完整facts；若超过schema hard cap则显示contract_error，不展示被静默截断的
  “看似完整”集合。
- current_user_message.text虽是durable truth，Inspector默认只显示id/chars/hash/timing；显式 transcript detail权限
  才读取正文，diagnostic永不复制正文。

### 17.4 Live status

HostSession summary新增：

    boundary = {
      "state": "idle" | "preparing" | "committing" | "committed_repair" | "commit_outcome_unknown",
      "boundary_id": ...,
      "kind": ...,
      "phase": ...,
      "draft_run_id": ...,
      "started_at_utc": ...,
      "candidate_event_ids": [...],
      "durable_run_existence": "none" | "full" | "unknown" | "partial_untrusted",
      "pending_compaction_terminalization_count": ...,
      "observer_state": "attached" | "backpressured" | "detached",
      "active_segment_id": ... | null,
      "active_segment_generation": ... | null,
      "active_segment_owner_kind": ... | null,
      "active_segment_owner_id": ... | null,
      "current_execution_handle_id": ... | null,
      "retiring_execution_handle_count": ...,
    }

Subagent live projection另外显示`child_entry_commit_state=none|full|unknown|partial_untrusted`与
`reconciliation_required`；UNKNOWN/PARTIAL child即使parent durable graph仍是nonterminal，也必须在physical capacity
计数与close blockers中可见。ownerless child RunStart recovery另外显示
`child_terminal_repair_state=not_needed|pending|commit_outcome_unknown|ledger_latched|completed`，不得把unknown/partial显示成
普通running。

Live status是process observation，不冒充durable event。

## 18. Context Compiler Input Hard Cut衔接

### 18.1 Snapshot builder输入

后续ContextFactSnapshot builder required接收：

- CommittedRunEntry（Host或subagent child）；
- latest CommittedInteractionResumeBoundary | None（continuation不是新entry）；
- typed RunWorkingSet；
- current ResolvedModelCall；
- typed memory/subagent/tool-result projections；
- compile timing。

禁止：

- 从HostSession default重新推断permission；
- 从liveMCP supervisor重新推断installation；
- 从scratchpad找capability exposure；
- 从EventLog“最新event”猜transcript high-water；
- 从当前时间重造user observation。
- 从RunStart.metadata["user_input"]恢复current user；只读required current_user_message fact。

### 18.2 每model call重新构造

boundary facts是上游稳定slice，不代表整个ContextFactSnapshot。

同一run的后续model call：

- model target不变；
- permission不变；
- initial exposure或narrowed continuation exposure明确；
- resolved call id变化；
- transcript/tool results/memory/subagent/timing变化；
- ContextFactSnapshot id/fingerprint相应变化。

### 18.3 ContextSource

ContextSource阶段只读取ContextFactSnapshot授权slice；不得重新调用Host boundary或MCP supervisor。

## 19. SESSION_REOPEN 与 subagent边界

### 19.1 Durable session reopen

HostCore.resume_session()继续独立：

- manifest/runtime identity reservation；
- dangling run repair；
- newHostSession/newMCP supervisor；
- required startup；
- manifest/publish。

它不恢复pending interaction/LoopState，因此不使用PRE_INTERACTION_RESUME pipeline。

可复用的只有低层：

- RuntimeSession mutation admission；
- RunStart contract validators；
- diagnostics DTO。

### 19.2 Subagent child

child run不经过Host safe point：

- parent spawn command与child capability profile已有独立atomic/durable contract；
- SubagentRunEntryDriver从hydrated task artifact、parent spawn/run facts与child profile构造PreparedSubagentRunEntry；
- child model target/permission来自child profile，permission必须是preset-only primitive fact；
- task-backed text写入RunStart.current_user_message(source_kind="subagent_task")；primitive objective使用
  source_kind="subagent_primitive_objective"；两者都引用对应durable artifact；
- child RunStart使用run_entry_kind=subagent_child与required SubagentRunEntryFact；
- child driver预生成terminal_run_end_event_id，通过child RuntimeSession提交并执行同样的BaseException confirm；
- FULL后产出CommittedSubagentRunEntry，再调用AgentRuntime.stream_committed_entry()；
- child initial CapabilityExposureResolvedEvent owner为subagent_run_start，不要求host boundary id；
- child ContextFactSnapshot使用同一个CommittedRunEntry union；
- host boundary coordinator不得成为child service locator；
- parentHost continuation exposure narrowing影响child的规则仍由Subagent/MCP binding safety contract控制。

task-backed与primitive child都走同一个driver；区别只在SubagentRunEntryFact.subagent_task_id branch。primitive child的
subagent_task_id为None，仍从spawn message/objective artifact hydrate current-user fact。

child RunStart candidate使用stable IDs并执行与Host相同的confirmation，但parent graph、execution handle与capacity按
以下矩阵处理：

| child confirmation | child ledger语义 | parent graph/handle动作 | capacity |
| --- | --- | --- | --- |
| NONE | 已证明无child RunStart | 可以提交稳定`SubagentRunFailedEvent(reason=child_run_start_not_committed)`；parent commit确认后release handle | release |
| FULL | child RunStart存在 | 安装CommittedSubagentRunEntry/run owner并继续；原操作已取消/失败则先terminalize child，再写parent failure | 持有至child terminal+drain |
| UNKNOWN | 存在性暂不可确认 | 不写普通failed；ChildExecutionRegistry保存candidate IDs、confirmation owner与`reconciliation_required` live projection | 保留 |
| PARTIAL/CONFLICT | child ledger结构不可信 | latch child RuntimeSession ledger；parent不得声称clean failure，Inspector显示`partial_untrusted` | 保留 |

UNKNOWN/PARTIAL/CONFLICT时parent durable SubagentRun保持非terminal；process-local parent graph projection必须join
ChildExecutionRegistry并显示`child_entry_commit_state`与`reconciliation_required=true`。V1不新增一个假terminal event来
掩盖child ledger不确定性。后续confirm为NONE后才允许parent fail；confirm为FULL后才能安装run owner并继续或稳定
terminalize。close必须drain confirmation/terminalization owner；未解决前不得释放slot reservation、child runtime、MCP
lease或并发容量，也不得允许新的child绕过physical cap。

child WAITING_USER的RunEnd也使用相同矩阵：FULL后parent写`subagent_pending_unsupported` failure；UNKNOWN/PARTIAL时
继续保留owner和capacity，不把parent提前terminalize。

#### 19.2.1 Child terminal → parent graph crash repair

child RunEnd FULL commit后，driver先构造ChildNativeTerminalReferenceFact；normal completion还必须构造
ChildResultHandoffFact，随后才构造parent terminal candidate。parent event ID必须由低层helper按
`[parent_runtime_session_id, subagent_run_id, child_terminal_event_id, parent_terminal_event_type]`确定性生成；正常路径与
restart repair使用同一ID/payload builder。

child无RunEnd时先区分owner语义：

- live reconcile且ChildExecutionRegistry中matching owner/driver确实存活：保持running，不能由旁路recovery抢先终结；
- SESSION_REOPEN、Host reopen或matching owner已不存在：先使用child RunStart.terminal_run_end_event_id构造
  `recovered_interrupted` child RunEnd并走stable confirmation；FULL后再构造terminal reference与parent failed/cancelled
  candidate；
- child RunEnd candidate confirmation UNKNOWN：显示commit_outcome_unknown blocker；PARTIAL/CONFLICT：latch child ledger；
  两者都不得显示普通running、不得提前terminalize parent或释放capacity。

映射固定为：child recovered_interrupted → parent SubagentRunFailedEvent(reason_code=child_recovered_interrupted)；已durable
user_stop/host_teardown child RunEnd分别映射SubagentRunCancelledEvent(cancelled_by=user/host_shutdown)；execution_failure映射parent failed并保留
typed child stop reason。不得由repair caller自由选择failed/cancelled。

parent repair流程：

1. parent graph中run非terminal时，通过EventLogLocator读取child RuntimeSession；
2. 精确定位child RunStart及其required terminal_run_end_event_id；
3. 按上面的live/reopen分支取得FULL、contract-valid child RunEnd；
4. 构造ChildNativeTerminalReferenceFact；child terminal为normal时按19.2.2构造exact result handoff；
5. 构造stable parent candidate；
6. parent append使用Subagent command planner/CAS；same ID+same payload按幂等成功，different payload latch graph/ledger；
7. parent terminal FULL/fold后才releasehandle、slot与child runtime；
8. task-backed child随后使用同一parent terminal event id驱动Task terminal/dependency cascade；primitive child不伪造task
   terminal。

这样child terminal已提交、parent terminal尚未提交的窗口可重放修复，并能由Inspector跨runtime证明“child terminal在先”。

#### 19.2.2 Deterministic child result handoff

child terminalization_kind=normal时选择唯一结果来源：

1. parent ledger已有唯一SubagentResultSubmittedEvent：必须按6.3通过EventLogLocator构造完整
   ChildExplicitResultEvidenceFact；验证通过才选择explicit并逐字段复用，不重新summary或写新result ID；存在submission但
   evidence无效时fail closed，不能静默改选inferred；
2. 否则选择inferred：用SubagentRunEntryFact.child_result_render_policy，通过child transcript reducer读取
   `sequence <= child terminal sequence`的最终assistant text、bounded artifact refs与frozen summary char cap；
3. inferred result ID按
   `subagent_result:sha256(subagent_run_id, child_terminal_event_id, policy_fingerprint)`生成；primary artifact ID按
   `subagent_result_artifact:sha256(subagent_run_id, child_terminal_event_id, policy_fingerprint)`生成；
4. summary、output payload、artifact refs排序/去重、empty-final fallback与clipping全部属于versioned renderer contract；不得
   读取当前prompt、当前budget default或wall clock；
5. archive put使用deterministic ID和canonical metadata。ID不存在则写入；同ID同bytes+同semantic metadata视为幂等成功；
   同ID不同bytes/metadata是ArtifactContentConflict并fail closed；
6. 由上述fact构造SubagentRunCompletedEvent及task completion，normal path与restart repair共享同一pure builder。

显式结果的随机ID可以保留，因为它已经由SubagentResultSubmittedEvent durable冻结；只有尚未durable的inferred结果必须
确定性派生。验收比较parent event ID、result ID、artifact ID、artifact bytes/hash、summary与完整canonical event payload，
不能只比较event ID。

inferred artifact semantic metadata固定为artifact_kind、subagent_run_id、result_id、child_runtime_session_id、
child_terminal_event_id、renderer_version、render_policy_fingerprint、max caps与result_source；observed/write timestamp不进入semantic equality或ID。archive API
必须提供put-if-absent-or-confirm-identical语义，不能用普通overwrite掩盖冲突。

RB2在`memory.foundation.protocols.ArtifactStore`新增专用contract：

    class ArtifactContentConflict(RuntimeError): ...

    class ArtifactPutConfirmation(BaseModel):
        status: Literal["inserted", "confirmed_identical"]
        artifact: ArtifactWriteResult

    def put_text_if_absent_or_confirm_identical(
        blob_id: str,
        content: str,
        *,
        session_id: str,
        run_id: str,
        media_type: str,
        semantic_metadata: Mapping[str, JsonValue],
    ) -> ArtifactPutConfirmation: ...

identity比较必须在一个store-owned临界区/数据库事务内覆盖blob ID、exact UTF-8 bytes/digest、media type、session/run
ownership与canonical semantic_metadata。metadata key order不影响相等；缺key、额外key或value变化都是
ArtifactContentConflict。数据库created_at/stored_at等storage metadata不参与。InMemory实现使用thread-safe lock包住
check+insert；PostgreSQL实现使用现有artifact advisory/row lock，在同一事务中insert-on-conflict后读取并比较JSONB，两个
并发writer必须得到`inserted + confirmed_identical`或一个明确conflict，不能last-write-wins。

普通`put_text()`可以保留原generic语义，但deterministic child handoff production path只能调用新API。metadata-only
conflict、bytes-only conflict、同payload retry与两个并发PostgreSQL writer均是RB2 required tests。

RB2只能删除AgentRuntime对**Host** RunStart的ownership；同一PR必须把当前`child_agent.run_task()`隐式
RunStart迁到SubagentRunEntryDriver，不能先删除generic path再让child没有入口。最终形态是AgentRuntime完全不
创建RunStart，Host与Subagent两个entry driver分别拥有commit。

## 20. API hard cut

### 20.1 HostSession

删除入口内联编排，变为：

    async def run_turn(...):
        return await self._run_boundary_driver.run(...)

    def stream_turn(...) -> AsyncIterator[AgentEvent]:
        ingress = self._run_boundary_driver.capture_ingress(...)
        owned_driver = self._run_boundary_driver.start_owned_stream(ingress)
        return owned_driver.observe()

    async def resolve_approval(...):
        return await self._resume_boundary_driver.resolve_approval(...)

    async def resolve_plan_interaction(...):
        return await self._resume_boundary_driver.resolve_plan(...)

    async def resolve_mcp_input_required(...):
        return await self._resume_boundary_driver.resolve_mcp(...)

    def stream_approval_resolution(...) -> AsyncIterator[AgentEvent]:
        return self._resume_boundary_driver.start_owned_approval_stream(...).observe()

    def stream_plan_interaction_resolution(...) -> AsyncIterator[AgentEvent]:
        return self._resume_boundary_driver.start_owned_plan_stream(...).observe()

    async def exit_plan_workflow(...):
        return await self._plan_workflow_driver.exit_or_resume(...)

run/stream共享同一execution owner和pipeline。

所有streaming Host ingress刻意是普通`def`。async-generator函数体直到第一次`__anext__()`才执行，无法满足“调用入口即
记录observed_at并注册owner”。新的ordinary factory在返回iterator前同步capture ingress并在当前event loop
创建Host-owned driver task；iterator只观察该task。caller迟读、暂不读或关闭observer都不改变execution owner；
session close/stop仍能定位并drain它。

V1明确选择“lossless bounded queue允许backpressure暂停run”，不实现durable spool：

- consumer暂不pull且queue满时，producer/model loop可以暂停；这不是run取消或terminal状态；
- stream iterator `aclose()`或consumer task cancellation只detach observer，不隐式stop run；
- stop_current_turn()/HostSession close在cancel active driver或写RunEnd前，必须同步mark observer detached并drain
  queue，使已阻塞put至多完成一次后所有后续emit no-op；
- detach同时resolve独立`observer.detached` future；observe loop等待queue item或detached/driver completion，不能依赖
  向已满queue再put一个DONE sentinel才能退出；
- RunEnd的durable commit完全独立于observer queue；detached consumer可以看不到末尾events，但Inspector/replay可见；
- observer detach与active driver identity compare-and-clear使用同一event-loop线性化边界；
- consumer既不pull也不close时，run保持ACTIVE/paused，直到consumer恢复、explicit stop或session close。

`run_turn()` waiter也只是observer：内部用`await asyncio.shield(host_owned_task)`。caller取消await表示detach waiter，
不得把CancelledError传播进run task或隐式映射user stop；只有stop_current_turn/session close有取消authority。
API/CLI若希望“disconnect即stop”，必须显式调用stop，不得依赖普通task cancellation传播。
REPL Ctrl-D沿用detach语义，`:stop`显式停止run，`:close`走session close；transport adapters必须把自己的disconnect
policy显式映射到detach或stop。

该API要求在running event loop线程调用；若`asyncio.get_running_loop()`失败则同步抛HostSessionUsageError，不创建
半个ingress或unowned task。

`run_turn()`可以继续是async API，其observation时刻定义为coroutine实际开始执行并注册boundary owner的时刻，
不是Python创建coroutine object的时刻。

### 20.2 AgentRuntime

新增：

    PreparedRunEntry = PreparedNewRunBoundary | PreparedSubagentRunEntry

    prepare_run_draft(prepared: PreparedRunEntry) -> AgentRunDraft
    stream_committed_entry(
        draft: AgentRunDraft,
        committed: CommittedRunEntry,
    ) -> AsyncIterator[AgentEvent]

    stream_committed_interaction_resume(
        state: LoopState,
        entry: CommittedRunEntry,
        committed: CommittedInteractionResumeBoundary,
        resolution: object,
    ) -> AsyncIterator[AgentEvent]

删除AgentRuntime内部：

- freshpermission snapshot推断；
- Host RunStart+MCP audit batch ownership（迁到Host driver）；
- child RunStart ownership（迁到SubagentRunEntryDriver）；
- Host active bookkeeping；
- empty-input capability resume fallback。

AgentRuntime仍拥有：

- LoopState/RunWorkingSet；
- memory hooks；
- exposure resolution implementation；
- model/tool loop；
- interaction-specific execution；
- unified committed execution terminalizer与run finalization。

### 20.3 SubagentRunEntryDriver

    prepare_child_run_entry(
        hydrated_run: HydratedSubagentRunView,
        child_runtime_session: RuntimeSession,
    ) -> PreparedSubagentRunEntry

    commit_child_run_entry(
        prepared: PreparedSubagentRunEntry,
    ) -> CommittedSubagentRunEntry

driver由SubagentRuntime composition root拥有；它验证parent graph facts、child profile、task artifact、MCP owner与
capability basis，使用child RuntimeSession提交RunStart并执行same stable confirmation。不得调用HostSession或
HostRunBoundaryDriver。

### 20.4 RuntimeSession

新增/公开：

    require_mutation_allowed()
    read_event_snapshot_through_current()
    async confirm_candidate_batch(candidates) -> BoundaryBatchConfirmation
    commit_run_start_boundary(...)
    commit_interaction_resume_boundary(...)

commit helpers最终仍委托通用write_events，不创建第二套writer。
`confirm_candidate_batch()`是EventLog.confirm_batch的RuntimeSession async authority：底层storage调用在
bounded worker/thread中执行并持有同一thread-safe write
coordinator，校验canonical payload/连续sequence，返回锁内actual high-water，并在partial/conflict时设置不可被
普通reducer rebuild清除的ledger structural latch。

## 21. 代码落脚点

### 21.1 新文件

- src/pulsara_agent/primitives/run_boundary.py
- src/pulsara_agent/primitives/run_entry.py
- src/pulsara_agent/primitives/run_lifecycle.py
- src/pulsara_agent/primitives/subagent.py
- src/pulsara_agent/primitives/capability.py
- src/pulsara_agent/primitives/permission.py
- src/pulsara_agent/primitives/__init__.py
- src/pulsara_agent/host/run_boundary.py
- src/pulsara_agent/runtime/run_draft.py
- src/pulsara_agent/runtime/subagent/run_entry.py
- tests/test_run_boundary_contract.py
- tests/test_run_boundary_host_lifecycle.py
- tests/test_run_boundary_resume.py
- tests/test_run_boundary_architecture.py

### 21.2 修改文件

- src/pulsara_agent/host/session.py
- src/pulsara_agent/host/core.py
- src/pulsara_agent/host/registry.py
- src/pulsara_agent/cli.py
- src/pulsara_agent/runtime/agent.py
- src/pulsara_agent/runtime/session.py
- src/pulsara_agent/runtime/state.py
- src/pulsara_agent/runtime/permission_snapshot.py
- src/pulsara_agent/runtime/permission.py
- src/pulsara_agent/runtime/transcript.py
- src/pulsara_agent/runtime/compaction/service.py
- src/pulsara_agent/memory/foundation/protocols.py
- src/pulsara_agent/memory/artifacts/archive.py
- src/pulsara_agent/memory/artifacts/postgres_archive.py
- src/pulsara_agent/runtime/mcp/supervisor.py
- src/pulsara_agent/primitives/mcp.py
- src/pulsara_agent/runtime/subagent/runtime.py
- src/pulsara_agent/runtime/subagent/facts.py
- src/pulsara_agent/runtime/subagent/commands.py
- src/pulsara_agent/runtime/subagent/reducer.py
- src/pulsara_agent/runtime/subagent/hydration.py
- src/pulsara_agent/runtime/subagent/projection.py
- src/pulsara_agent/capability/runtime.py
- src/pulsara_agent/capability/exposure.py
- src/pulsara_agent/capability/resolver.py
- src/pulsara_agent/capability/provider.py
- src/pulsara_agent/capability/render.py
- src/pulsara_agent/capability/types.py
- src/pulsara_agent/tools/registry.py
- src/pulsara_agent/event/events.py
- src/pulsara_agent/event/__init__.py
- src/pulsara_agent/event_log/serialization.py
- src/pulsara_agent/inspector/service.py
- tests/conftest.py
- tests/test_artifact_store_contract.py
- run start/resume/compaction/MCP/subagent/real-LLM tests。

### 21.3 长期contract

按代码落地PR同步更新：

- contracts/AGENT_RUNTIME_LOOP_CONTRACT.zh.md
- contracts/PERMISSION_POLICY_CONTRACT.zh.md
- contracts/MESSAGE_TRANSCRIPT_CONTEXT_CONTRACT.zh.md
- contracts/RUNTIME_SEMANTIC_GRAPH_CONTRACT.zh.md
- contracts/RECOVERY_CONTRACT.zh.md
- contracts/HOST_RESUME_CONTRACT.zh.md
- contracts/EVENT_LOG_STORAGE_CONTRACT.zh.md
- contracts/CAPABILITY_SURFACE_CONTRACT.zh.md
- contracts/MCP_CAPABILITY_CONTRACT.zh.md
- contracts/CONTEXT_COMPACTION_CONTINUITY_CONTRACT.zh.md
- contracts/INSPECTOR_PROJECTION_CONTRACT.zh.md
- contracts/APP_SETTINGS_CLI_ENTRY_CONTRACT.zh.md

## 22. PR实施顺序

每个PR必须独立全绿，不允许“后续PR补ownership”。

### RB0：Characterization与低层contract hard cut（event behavior不变）

内容：

- characterization tests锁定当前new-run/resume顺序；
- PermissionMode/preset mapping从runtime迁到primitives.permission，迁移全部imports并删除旧定义；
- HostRunBoundary enums/facts；
- diagnostics/disposition；
- boundary identity/timestamp validator；
- low-level PermissionMode/PresetPermissionPolicyFact与唯一preset expansion mapping；
- CurrentUserMessageFact、SubagentRunEntryFact与CommittedRunEntry union；
- descriptor/binding identity、provider-aware projection entry/fragment identity、catalog/active projection与
  CapabilityExposureSemantic facts；
- CapabilityExecutionSurfaceProvider/CapabilityProjectionProvider split protocol、fragment-level projection facts与
  binding contract id/version invariant；
- low-level RunStopReason、ChildResultRenderPolicyFact、ChildNativeTerminalReferenceFact与ChildResultHandoffFact；
- CapabilityResolveBasis/Exposure facts；
- PlanWorkflowStateFact、ResumeGatePolicy、blocked result union；
- RunWorkingSet process-local DTO contract；
- NewRunBoundaryFact与RunStart branch validator可作为低层model单测，但**不修改RunStartEvent production schema**；
- RunInteractionResumeBoundaryEvent；
- CapabilityExposureResolvedEvent；
- serialization/AgentEvent union；
- Inspector DTO占位但不投影live fallback。

验收：

- event round-trip；
- invalid identity/fingerprint/timestamp拒绝；
- standalone host/subagent RunStart branch model invariant；
- old production event/trajectory仍全绿；
- no runtime behavior change。

RB0不得给现有RunStartEvent添加nullable/default placeholder，也不得要求旧production writer生成假的boundary fact。
RunStart schema与所有constructor的原子hard cut只在RB2发生。

### RB1：Boundary attempt owner与admission kernel

内容：

- HostRunBoundaryAttempt；
- PREPARING lifecycle/status；
- stable CommittedRunExecutionOwner与可轮换RunExecutionSegmentOwner；
- activation owner kind/id、segment generation CAS与run completion/segment completion分离；
- stop/close termination intent first-writer-wins contract；
- process-local RunTerminationIntent DTO与suspended stop/resume install竞态；
- segment registry接收driver factory，先安装owner再创建Task，兼容eager task factory；
- ordinary-def stream ingress ownership；
- lossless bounded queue、observer detach-before-stop/close与shielded run_turn waiter；
- lossless bounded backpressure与observer/waiter detach contract；
- close/stop cancel/drain，包含PREPARING stop result；
- lock内authoritative recheck；
- RuntimeSession.require_mutation_allowed；
- known reconciliation在MCP/compaction前阻断；
- no participant registry architecture guard。

验收：

- concurrent run callers；
- concurrent resume callers；
- close during required wait；
- close during preflight；
- stop before commit / stop during commit；
- reconciliation prevents MCP/compaction；
- PREPARING不显示active run。

### RB2：PRE_RUN纵向hard cut

内容：

- ingress user observation/boundary identity；
- pure target/permission resolve；
- explicit recovery maintenance；
- MCP helper；
- EventLog snapshot/source watermark；
- preflight compaction correlation；
- CompactionEventCommitPort接入现有RuntimeSession writer，删除production direct append/post-publish补偿；
- AgentRunDraft；
- RunStartEvent一次性hard cut为required current_user_message、terminal_run_end_event_id与host/child entry branch，
  并迁移所有production/test constructors；
- RunEndEvent一次性hard cut为matching stable id + terminalization_kind；
- LoopState/AgentRunResult/Host API/CLI/Inspector统一消费low-level RunStopReason enum，删除runtime Literal alias与自由
  string assignments；
- Host RunStart+audit commit迁到boundary coordinator；child RunStart迁到SubagentRunEntryDriver；
- 两路都产出CommittedRunEntry并调用AgentRuntime.stream_committed_entry()；
- transcript/recovery删除metadata["user_input"] fallback；
- Host与child initial CapabilityExposureResolvedEvent production写路径、CapabilityResolveBasis与RunWorkingSet在本PR落地；
- 删除capability_exposure_resolved CustomEvent production写路径，不双写；
- CapabilityProvider.resolve生产接口hard cut：execution surface在RunStart前由snapshot_descriptors/composition root
  生成，catalog/active projections在RunStart后由resolve_projection生成并写fragment/artifact refs；
- context-dependent descriptor provider禁止进入production；
- commit后activation；
- unified committed execution stable RunEnd terminalizer；
- child WAITING_USER先稳定terminalize child RunEnd，再写parent graph failure；
- child RunStart NONE/FULL/UNKNOWN/PARTIAL confirmation与parent graph/handle/capacity矩阵；
- child native terminal reference、deterministic parent terminal id与restart repair；
- ChildResultRenderPolicyFact与SubagentBudget summary/artifact caps cross-validation；
- explicit/inferred ChildResultHandoffFact、versioned deterministic renderer与idempotent artifact write；
- ArtifactStore put_text_if_absent_or_confirm_identical API、metadata identity与PostgreSQL concurrent writer contract；
- live child owner reconcile和SESSION_REOPEN ownerless recovery分支；
- child borrow tracker遵守MCP per-call lease contract，不引入lifetime borrow；
- BaseException/cancellation commit confirmation与post-commit repair owner；
- compaction stable terminal id、Failed cancellation/recovery语义与commit port；
- exit_plan_workflow无pending分支改成host workflow mutation，不伪造active run；
- run/streamproduction只走新pipeline；
- 删除旧inline new-run path。

长期contract：

- AGENT_RUNTIME_LOOP；
- EVENT_LOG_STORAGE；
- PERMISSION_POLICY；
- MESSAGE_TRANSCRIPT_CONTEXT；
- RUNTIME_SEMANTIC_GRAPH；
- CAPABILITY_SURFACE（initial exposure/run-entry owner）；
- CONTEXT_COMPACTION_CONTINUITY；
- APP_SETTINGS_CLI_ENTRY。

验收：

- new run完整failure matrix；
- RunStart boundary fact equality；
- Host/subagent CommittedRunEntry与typed current user replay equality；
- initial typed exposure/basis/working-set equality；
- compaction refs/source high-water；
- cancel during publication full/none/partial confirmation；
- host plan force-exit no RunStart/no active state；
- active-after-commit；
- run/streamtrajectory equality。

### RB3：PRE_INTERACTION_RESUME纵向hard cut

内容：

- suspended state token；
- RunStart target+permission rebuild/equality；
- current MCP install +pending audit；
- continuation exposure reused/narrowed；
- derive_continuation_basis保留原输入事实并替换owner/current MCP/execution surface；
- continuation每次用derived basis解析current semantic candidate；
- authorization/catalog/active projection完整相等才reuse，built-in/skill/custom revoke可narrow；
- continuation projection只做original rendered-fragment intersection，禁止重新渲染造成detail promotion；
- empty container/projection wrapper与static fragment删除规则；
- continuation FULL后execution handles同步CAS swap，old handles只按真实parent/child tool-call borrow barrier退休；MCP
  pending lease由Supervisor独立保护exact slot/manager；
- approval/plan/MCP gate policy；
- typed resume event；
- post-commit lease/state recovery；
- 在existing CommittedRunExecutionOwner上安装新generation RunExecutionSegmentOwner，并接入router terminalizer；
- 所有resume入口production只走新pipeline；
- exit_plan_workflow有suspended分支只走PRE_INTERACTION_RESUME；
- 删除empty-input exposure fallback。

长期contract：

- HOST_RESUME；
- CAPABILITY_SURFACE；
- MCP_CAPABILITY；
- AGENT_RUNTIME_LOOP。

验收：

- pending identity race；
- required/unrelated/self-bindingfailure；
- no exposure widening；
- active skill preservation；
- approval不重复permission WAIT；
- MCP capability+permission recheck；
- plan workflow semantics；
- audit failure retry。
- cancel during resume publication confirmation。

### RB4：Inspector/replay与projection

内容：

- Inspector run boundary/continuation projections；
- Host/subagent run-entry、complete capability semantic exposure与RunWorkingSet replay projection；
- boundary-compaction join；
- live status PREPARING；
- commit outcome unknown/ledger latch/pending compaction terminalization projection；
- durable run owner、active segment generation与child entry reconciliation live join；
- host workflow plan exit projection；
- replay contract；
- PostgreSQL event payload CHECK（若项目采用）；
- contract error diagnostics；
- docs migration。

验收：

- live/replay projection equality；
- Inspector不读live runtime；
- orphan/mismatch diagnostics；
- boundary timeline可解释pre-run与resume；
- old schema hard fail。

RB4不再承担production typed exposure迁移；RB2已经提供initial source exposure，RB3才能在独立全绿的前提下
实现continuation reuse/narrowing。RB4只消费这些durable事实。

### RB5：Deletion gates、fault injection与dogfood

内容：

- 删除旧HostSession内联MCP/target/preflight/resume顺序；
- 删除AgentRuntime内全部RunStart ownership（Host与child driver已接管）；
- 删除scratchpad capability exposure fallback；
- grep/import guards；
- cancellation/commit uncertainty/fault tests；
- full pytest/ruff；
- real LLM/MCP/plan/subagent/compaction dogfood。

验收：

- architecture guards全绿；
- no compatibility overload；
- no dangling boundary/RunStart；
- no leakedMCP lease/task；
- Context Compiler Input阶段只接收新boundary contract。

## 23. 测试矩阵

### 23.1 DTO/schema

- test_host_run_boundary_identity_requires_utc_and_positive_attempt
- test_new_run_boundary_fact_matches_run_start_contract
- test_host_run_start_requires_boundary_fact
- test_subagent_run_start_rejects_host_boundary_fact
- test_subagent_run_start_requires_subagent_entry_and_current_user_task_fact
- test_subagent_task_backed_entry_requires_matching_task_id
- test_primitive_subagent_entry_requires_null_task_id_and_spawn_owned_artifact
- test_subagent_entry_requires_frozen_child_result_render_policy
- test_child_result_policy_matches_parent_budget_snapshot_caps
- test_nondefault_summary_cap_normal_and_repair_handoff_payloads_are_identical
- test_inspector_accepts_null_subagent_task_id
- test_committed_run_entry_accepts_host_and_subagent_without_fake_boundary
- test_exposure_owner_accepts_host_boundary_or_subagent_run_start
- test_current_user_message_is_required_and_matches_user_input_chars
- test_run_start_metadata_user_input_is_not_a_supported_truth
- test_run_end_id_and_terminalization_kind_match_run_start_contract
- test_run_end_terminalization_matrix_distinguishes_user_stop_and_host_teardown
- test_run_end_preserves_detailed_typed_stop_reason
- test_run_end_rejects_waiting_user_and_unknown_free_string_reason
- test_child_native_terminal_reference_matches_child_run_end_exactly
- test_child_handoff_usage_status_and_terminal_sequence_invariants
- test_run_start_created_at_is_not_backfilled_to_user_observed_at
- test_resume_boundary_requires_original_run_start_reference
- test_resume_exposure_transition_rejects_widening
- test_boundary_diagnostics_are_bounded_and_redacted
- test_execution_surface_identity_covers_per_descriptor_and_binding_entries
- test_callable_binding_identity_requires_contract_id_and_version
- test_execution_surface_snapshot_provider_cannot_read_turn_context
- test_projection_provider_cannot_add_descriptor_or_binding
- test_exposure_semantic_identity_covers_catalog_and_active_skill_projections
- test_exposure_semantic_and_fact_fingerprints_have_distinct_attribution_rules
- test_descriptor_artifact_exists_before_run_start_commit
- test_exposure_replay_fails_closed_on_missing_projection_artifact
- test_projection_fragment_fact_rebuilds_exact_model_visible_prompt
- test_projection_entry_identity_distinguishes_same_name_content_from_different_providers
- test_projection_fragment_references_source_entry_id_not_stable_name
- test_projection_entry_and_fragment_ids_are_unique_across_exposure
- test_capability_identity_ignores_registry_generation_and_object_identity
- test_capability_authorization_cap_fails_closed_without_truncation
- test_primitives_permission_owns_single_preset_expansion_mapping
- test_prepare_result_uses_blocked_carrier_before_target_resolution
- test_suspended_state_token_reuses_only_for_same_pending_interaction
- test_run_owner_survives_waiting_user_while_initial_segment_completes
- test_resume_installs_new_segment_generation_without_aba_clear
- test_run_completion_waits_for_matching_run_end_not_segment_suspend
- test_child_initial_segment_uses_subagent_run_start_activation_owner
- test_stop_and_close_race_preserves_first_cas_termination_intent
- test_cancelled_driver_without_intent_is_not_reported_as_user_or_host_stop
- test_suspended_stop_intent_blocks_post_commit_resume_segment_install
- test_resume_swap_winner_still_blocks_segment_when_stop_intent_arrives_before_install
- test_segment_owner_is_installed_before_driver_factory_runs_under_eager_task_factory
- test_blocked_segment_install_never_invokes_driver_factory_or_agent_runtime
- test_segment_task_creation_failure_closes_coroutine_and_terminalizes_committed_run
- test_prepared_boundary_contains_mcp_reference_fact_not_live_tools_or_supervisor
- test_prepared_transcript_is_owned_deep_copy_without_claiming_recursive_immutability

### 23.2 Admission/concurrency

- test_pre_run_rechecks_lifecycle_inside_run_lock
- test_pre_run_rejects_known_reconciliation_before_mcp
- test_pre_run_rejects_known_reconciliation_before_compaction
- test_second_concurrent_run_does_not_apply_safe_point_mutation
- test_second_concurrent_resume_does_not_apply_safe_point_mutation
- test_preparing_boundary_is_not_reported_as_active_run
- test_close_cancels_and_drains_preparing_boundary
- test_stop_preparing_before_commit_returns_no_durable_run_result
- test_stop_during_commit_confirms_before_choosing_run_semantics
- test_stop_during_preparing_resume_preserves_full_original_run_existence_and_terminalizes
- test_stream_turn_captures_observation_and_registers_owner_before_first_pull
- test_stream_resume_captures_resolution_and_registers_owner_before_first_pull
- test_unconsumed_stream_iterator_does_not_orphan_execution_owner
- test_stream_backpressure_pauses_run_until_consumer_or_detach
- test_stop_detaches_full_observer_queue_before_run_end
- test_detached_observer_exits_without_putting_done_into_full_queue
- test_run_turn_waiter_cancellation_detaches_without_stopping_run
- test_cli_disconnect_calls_explicit_stop_when_stop_policy_is_requested

### 23.3 PRE_RUN

- test_user_observed_at_is_captured_before_mcp_wait
- test_model_target_failure_does_not_wait_for_required_mcp
- test_required_mcp_failure_does_not_create_run_start
- test_optional_mcp_failure_proceeds_degraded
- test_recovery_maintenance_precedes_run_start
- test_preflight_reads_explicit_event_snapshot
- test_preflight_compaction_rebuilds_final_transcript
- test_preflight_compaction_failure_proceeds_to_compiler
- test_preflight_compaction_event_commit_honors_runtime_reconciliation
- test_compaction_events_publish_once_through_runtime_writer
- test_final_transcript_excludes_events_after_source_high_water
- test_run_start_and_mcp_audits_commit_atomically
- test_active_state_published_only_after_commit_ack
- test_run_start_post_commit_publication_failure_retains_owner
- test_cancel_during_run_start_publication_wait_confirms_full_batch_and_terminalizes
- test_cancel_during_run_start_publication_wait_with_none_discards_draft
- test_partial_run_start_confirmation_latches_ledger
- test_run_turn_and_stream_turn_share_identical_boundary_pipeline
- test_run_start_typed_current_user_replays_without_metadata_fallback
- test_preflight_compaction_rebuilds_typed_current_user_message
- test_subagent_run_entry_commits_before_child_agent_execution
- test_child_run_start_none_records_parent_start_failure_and_releases_capacity
- test_child_run_start_full_installs_committed_owner
- test_child_run_start_unknown_retains_handle_slot_and_reports_reconciliation
- test_child_run_start_partial_latches_child_ledger_without_clean_parent_failure
- test_subagent_exposure_event_uses_subagent_owner
- test_pre_run_descriptor_artifact_failure_does_not_create_run_start
- test_post_commit_capability_rebind_failure_terminalizes_stable_run_end
- test_post_commit_projection_artifact_failure_terminalizes_stable_run_end
- test_post_commit_exposure_commit_failure_terminalizes_stable_run_end
- test_post_commit_subagent_snapshot_refresh_failure_terminalizes_stable_run_end
- test_child_post_commit_exposure_failure_terminalizes_child_run_end
- test_child_waiting_user_terminalizes_child_before_parent_graph_failure
- test_child_waiting_user_unknown_terminal_commit_retains_parent_handle_and_capacity
- test_parent_subagent_terminal_fact_references_committed_child_terminal
- test_restart_repairs_child_terminal_parent_graph_crash_window_with_same_event_id
- test_explicit_child_handoff_reuses_submitted_result_payload_exactly
- test_production_result_submission_requires_child_tool_call_and_run_attribution
- test_explicit_handoff_rejects_wrong_tool_cross_run_or_post_terminal_evidence
- test_report_agent_result_applies_frozen_summary_cap_and_rejects_artifact_ref_overflow_before_submit
- test_inferred_child_handoff_rebuilds_event_result_artifact_summary_and_payload_exactly
- test_child_handoff_accounting_rebuilds_usage_status_and_tool_count_from_child_ledger
- test_inferred_child_handoff_artifact_same_id_different_content_fails_closed
- test_deterministic_artifact_metadata_only_conflict_fails_closed
- test_two_postgres_writers_confirm_identical_deterministic_artifact
- test_two_postgres_writers_with_same_id_different_metadata_conflict
- test_live_child_without_run_end_stays_running_while_matching_owner_exists
- test_session_reopen_repairs_ownerless_child_run_end_before_parent_failure
- test_child_run_end_unknown_or_partial_is_reconciliation_blocker_not_running
- test_run_end_commit_unknown_retains_terminalization_owner_and_close_blocker
- test_plan_force_exit_without_pending_does_not_create_run_or_active_state
- test_plan_force_exit_without_pending_restores_future_default_only
- test_compaction_cancel_after_started_commits_stable_failed_terminal_fact
- test_compaction_terminal_commit_unknown_blocks_destructive_close

### 23.4 Resume common

- test_resume_rechecks_pending_identity_under_lock
- test_resume_requires_exactly_one_original_run_start
- test_resume_rebuilds_target_from_run_start
- test_resume_rebuilds_permission_from_run_start
- test_resume_rejects_mutated_loopstate_permission_snapshot
- test_resume_audits_commit_before_activation
- test_resume_audit_failure_preserves_pending_state_and_lease
- test_resume_post_commit_publication_failure_folds_committed_slice
- test_cancel_during_resume_boundary_publication_wait_confirms_and_folds_full_batch
- test_resume_commit_unknown_preserves_pending_token_and_lease
- test_committed_resume_router_exception_uses_original_run_terminalizer

### 23.5 Capability continuation

- test_resume_reuses_exposure_when_full_exposure_semantic_identity_unchanged
- test_resume_derives_continuation_basis_with_new_owner_mcp_and_surface
- test_resume_continuation_basis_preserves_original_user_and_transcript_identity
- test_resume_does_not_reuse_when_builtin_descriptor_revoked_with_same_mcp_installation
- test_resume_narrows_when_local_skill_catalog_entry_revoked_with_same_mcp_installation
- test_resume_narrows_when_active_skill_content_fingerprint_changes
- test_resume_ignores_new_catalog_and_active_skill_entries
- test_resume_catalog_narrowing_reuses_original_fragments_without_detail_promotion
- test_resume_drops_empty_projection_container_wrappers
- test_resume_drops_orphan_static_fragments_after_last_container_entry_removed
- test_semantic_provider_prompt_cannot_use_unowned_static_fragment
- test_resume_does_not_keep_fragment_when_same_entry_moves_to_different_provider
- test_resume_handle_swap_occurs_only_after_full_continuation_commit
- test_resume_handle_swap_does_not_transfer_pending_lease_into_handle_tracker
- test_supervisor_pending_lease_keeps_exact_old_slot_until_terminal_fact_commit
- test_resume_segment_install_failure_does_not_rollback_old_surface
- test_resume_post_swap_failure_drains_incoming_and_retiring_handles
- test_child_lifetime_does_not_hold_mcp_binding_borrow
- test_retiring_handle_waits_only_for_child_in_flight_tool_call_borrow
- test_resume_does_not_reuse_when_custom_binding_changes_with_same_descriptor_names
- test_resume_reuses_semantically_equal_exposure_across_registry_generation_change
- test_resume_narrows_exposure_when_binding_revoked
- test_resume_never_adds_newly_discovered_tools_to_old_run
- test_resume_preserves_original_active_skill_basis
- test_resume_preserves_user_intent_basis
- test_resume_exposure_event_references_source_exposure
- test_resume_exposure_subset_property

### 23.6 Interaction-specific

- test_approval_resume_rechecks_capability_without_second_permission_wait
- test_approval_resume_denies_revoked_confirmed_tool
- test_plan_resume_uses_workflow_policy
- test_plan_force_exit_with_suspended_interaction_uses_resume_boundary_and_read_only_contract
- test_host_plan_exit_has_workflow_event_but_no_tool_result
- test_mcp_resume_requires_exact_pending_binding
- test_mcp_resume_rechecks_original_permission
- test_mcp_resume_wait_for_user_is_fail_closed
- test_unrelated_required_failure_preserves_mcp_pending_lease
- test_self_binding_reconfiguration_terminalizes_and_releases_after_result_commit

### 23.7 Cancellation/close

- test_close_during_mcp_wait_drains_boundary_attempt
- test_close_during_preflight_compaction_drains_boundary_attempt
- test_cancel_before_run_start_discards_draft
- test_cancel_after_run_start_terminalizes_durable_run
- test_commit_confirmation_maps_none_full_unknown_partial_without_bool_collapse
- test_uncertain_stop_result_never_claims_durable_run_false
- test_compaction_started_then_boundary_cancel_gets_terminal_fact
- test_compaction_recovery_repairs_started_without_terminal_by_stable_id
- test_compaction_publication_failure_counts_as_committed_terminal
- test_session_resources_not_released_while_boundary_task_alive

### 23.8 Event/Inspector

- test_run_boundary_events_round_trip
- test_inspector_joins_preflight_compaction_by_boundary_id
- test_inspector_projects_committed_new_run_boundary
- test_inspector_projects_all_resume_boundaries
- test_inspector_reports_boundary_identity_mismatch
- test_inspector_does_not_recompute_historical_exposure
- test_live_status_reports_preparing_phase
- test_inspector_projects_commit_outcome_unknown_and_pending_terminalization

### 23.9 Real dogfood

- test_real_new_run_slow_optional_mcp_boundary
- test_real_preflight_compaction_boundary_join
- test_real_approval_resume_preserves_active_skill
- test_real_mcp_resume_binding_reconfiguration
- test_real_plan_resume_read_only_contract
- existing full real-LLM/dogfood matrix。

## 24. Architecture与grep gates

必须增加：

    rg "_apply_mcp_safe_point" src/pulsara_agent/host/session.py

只允许boundary coordinator/composition seam，不允许各入口重复调用。

    rg "_begin_active_state" src/pulsara_agent/host/session.py

旧active-before-commit helper必须删除或只接受CommittedHostRunEntry。

    rg 'metadata\s*=\s*\{[^}]*"user_input"|metadata\.get\("user_input"\)' src/pulsara_agent

RunStart writer与transcript replay不得再使用metadata user_input fallback。

    rg 'RunStartEvent\(' src/pulsara_agent/runtime/agent.py

AgentRuntime不得创建Host或child RunStart；只允许HostRunBoundaryDriver与SubagentRunEntryDriver生产。

    rg 'child_agent\.run_task|AgentRuntime\.run_task' src/pulsara_agent

production child/host entry不得调用隐式RunStart API；最终只允许stream_committed_entry与
stream_committed_interaction_resume。

    rg 'user_input=""|prior_messages=\[\]|active_skill_names=frozenset\(\)' src/pulsara_agent/runtime/agent.py

resume exposure不得使用空basis重算。

    rg 'capability_exposure_resolved' src

只允许typed event name/serialization/Inspector，不允许CustomEvent生产。

    rg 'snapshot\(\)\.generation|tool_registry_generation' src/pulsara_agent/host src/pulsara_agent/runtime src/pulsara_agent/capability

不得用临时registry generation决定continuation exposure reuse；只允许diagnostic/test落点。

    rg 'class CapabilityProvider|def resolve\(' src/pulsara_agent/capability

RB2后不允许descriptor+projection混合production provider。descriptor producer只能实现snapshot_descriptors；projection
producer只能实现resolve_projection。通用`resolve()`匹配必须由AST architecture test逐项证明不是旧capability provider
compatibility API。

    rg 'class PermissionMode|_PRESET_POLICIES' src/pulsara_agent/runtime

定义与唯一mapping必须位于primitives.permission；runtime只能内部consume，不能复制或兼容re-export truth。

    rg 'from pulsara_agent\.runtime\.permission import .*PermissionMode' src tests

旧import path必须清零；不通过re-export维持compatibility。

    rg "stop_reason\s*=\s*['\"]|stop_reason:\s*Literal" src/pulsara_agent

RB2后production不得赋自由字符串stop reason或重新声明Literal真源；必须消费
`primitives.run_lifecycle.RunStopReason`。AST test允许序列化比较`.value`，不允许构造未注册reason。

    rg 'mcp_installation:\s*McpInstalledCapabilitySnapshot' src/pulsara_agent/host/run_boundary.py

匹配项只允许BoundaryExecutionHandles的字段；AST architecture test必须验证所有Prepared/Committed fact字段图中
不存在McpInstalledCapabilitySnapshot、McpCapabilityTool、supervisor、manager或lease。

    rg "event_log\.(append|extend)" src/pulsara_agent/runtime/compaction

production compaction service不得绕过CompactionEventCommitPort；只读iter/snapshot允许。

    rg 'async def stream_(turn|approval_resolution|plan_interaction_resolution)' src/pulsara_agent/host

Host streaming ingress必须是同步capture + owned iterator factory。

    rg 'new_state\(\).*exit_plan|active_run_id.*exit_plan|_prepare_state_for_plan' src/pulsara_agent/host/session.py

exit_plan_workflow无pending分支不得伪造active run；匹配结果必须由architecture test逐处allowlist。

    rg "ContextCompileRequest.*state|state: LoopState" src/pulsara_agent/runtime/context_engine

本阶段可暂存；Context Compiler Input Hard Cut完成时必须清零。

Architecture tests：

- HostSession entry facade不得直接import MCP supervisor internals；
- run boundary coordinator不得import provider adapters；
- primitives不得import runtime；
- compiler不得importhost boundary coordinator；
- no generic SafePointParticipant protocol；
- only HostRunBoundaryDriver commits host RunStart；only SubagentRunEntryDriver commits child RunStart；
- capability authorization cap overflow必须raise，禁止bounded/truncated name set继续执行；
- stop/close必须先detach observer，再cancel committed execution owner；
- run_turn caller cancellation必须由shield隔离，不得成为implicit stop。
- segment callback必须以run_id + segment_id + generation compare-and-clear，禁止只按run_id清owner；
- segment activation必须使用owner kind/id；child路径不得构造fake Host boundary id；
- stop/close取消segment前必须已在run owner安装matching RunTerminationIntent；
- install_segment与handle swap必须在同一run-owner state读取中拒绝existing termination intent/terminalization；
- committed run segment task只能由owner registry从driver_factory创建；Host/Agent/Subagent caller不得预建Task；
- child entry UNKNOWN/PARTIAL时capacity accounting必须继续计入该handle，禁止parent clean-failure shortcut释放slot；
- continuation projection path不得调用initial catalog/active renderer，只允许fragment intersection renderer。
- projection intersection必须按projection_entry_id + content_fingerprint，不得按stable_name单独匹配；
- continuation handle swap只能消费FULL committed boundary，old handle必须进入retiring tracker而非立即close。
- child result normal path与repair必须调用同一ChildResultHandoff builder；inferred path不得调用uuid4生成result/artifact ID；
- child不得持有lifetime MCP lease；borrow tracker只记录真实in-flight child tool call；
- semantic provider prompt必须有projection source entry，禁止藏在source-less static fragment。

## 25. 数据、migration与hard cut

- RunStart schema变更后旧event不进入supported runtime；
- 开发数据库reset或一次性migration；
- migration不能伪造boundary source high-water/exposure basis；
- migration不能把legacy metadata user_input静默提升为typed current_user_message；开发数据reset或显式一次性
  migration必须校验exact text/hash/timestamp；
- child RunStart缺SubagentRunEntryFact或exposure owner的旧ledger不进入supported runtime；
- 无法从旧event证明的fact不得猜测；
- old suspended run不支持live continuation，本就不能跨进程恢复；
- PostgreSQL JSONB无需独立列，facts保存在event payload；
- 若采用RUN_START JSONB CHECK，必须同时要求run_entry_kind/current_user_message/terminal_run_end_event_id，并按
  host/child branch检查new_run_boundary/subagent_run_entry存在性；深层hash/equality仍由Pydantic/contract tests负责；
- runs.metadata可denormalize boundary id用于列表，仍非truth；
- Inspector遇到旧RunStart显示contract error，不fallback。

## 26. 可观测性指标

至少记录：

- boundary prepare/commit/activate duration；
- phase duration；
- disposition counts；
- required MCP wait duration；
- preflight compaction attempted/completed/failed；
- transcript source event count/high-water；
- RunStart commit latency；
- committed-but-publication-failed count；
- commit confirmation none/full/partial/conflict/unknown counts；
- resume exposure reused/narrowed count；
- surface descriptor/binding identity change count；
- catalog/active projection semantic change count；
- Host/subagent run-entry counts；
- post-commit terminalizer outcome/latency/unknown counts；
- removed capability count；
- interaction resume retry count；
- close during preparing count；
- boundary cancellation/drain timeout count；
- pending compaction terminalization count/age；
- preparing stop before/full/unknown commit counts；
- observer backpressure duration/detach reason；
- run_turn waiter detach count。

指标不得包含raw user input、MCP credentials、tool arguments或secret-like diagnostics。

## 27. 完成后的代码形态

    HostSession
      -> HostRunBoundaryDriver
           -> NewRunBoundaryPipeline
           -> InteractionResumeBoundaryPipeline
           -> RuntimeSession commit helpers
           -> McpServerSupervisor public safe-point API
    SubagentRuntime
      -> SubagentRunEntryDriver
           -> child RuntimeSession RunStart commit
    HostRunBoundaryDriver / SubagentRunEntryDriver
      -> CommittedRunEntry
    CommittedRunEntry
      -> AgentRuntime
           -> AgentRunDraft builder
           -> committed execution terminalizer
           -> post-commit memory/exposure/model loop
           -> interaction-specific resume router
      -> ContextFactSnapshot builder（下一阶段）

HostSession保留：

- user-facing API；
- run lock与boundary/active task ownership；
- lifecycle/close；
- pending routing facade。

AgentRuntime保留：

- LoopState/RunWorkingSet；
- model/tool/workflow state machine；
- memory hooks；
- exposure implementation；
- finalization。

## 28. 完成定义

本章完成必须同时满足：

1. new run/live interaction resume只有两条显式typed pipeline；
2. known reconciliation在任何MCP/preflight mutation前阻断；
3. admission在run lock内authoritative重验；
4. PREPARING有独立owner，可被close bounded drain；
5. user observation在Host ingress采集；
6. model target/permission/MCP/transcript boundary只有一个freeze点；
7. RunStart+MCP audits atomic commit；
8. durable commit前不发布ACTIVE run；
9. 任意BaseException/cancellation后的none/full/partial/unknown commit outcome都有唯一owner，RunStart不会dangling；
10. resume从原RunStart重建target与permission；
11. Host/subagent分别合法commit RunStart并统一产出CommittedRunEntry；
12. current user由required typed RunStart fact持久化，metadata fallback删除；
13. capability continuation比较完整authorization/catalog/active projection semantic，只收窄、永不widen；
14. approval/plan/MCP gate policy分离；
15. host/user plan exit无pending时不伪造run，有pending时走resume boundary；
16. RunStart full commit后任意BaseException由stable RunEnd terminalizer收口；
17. compaction Started在取消/恢复后有stable terminal fact或明确close blocker；
18. Prepared facts不含MCP supervisor/tool/lease，transcript只声明owned deep copy；
19. lossless backpressure、observer detach与waiter cancellation语义固定；
20. PermissionMode/preset mapping只有低层primitive一个真源；
21. preflight compaction可通过boundary id join下一run；
22. typed capability exposure替代CustomEvent；
23. Inspector只从durable facts解释boundary；
24. ContextFactSnapshot builder消费CommittedRunEntry + optional continuation；
25. 旧inline编排、empty-input exposure fallback和active-before-commit删除；
26. Ruff、全量pytest、fault injection、real LLM/MCP dogfood全绿。

完成后，本文件移入archived_docs/；总路线指针移动到Context Compiler Input Hard Cut。

## 29. 归档后 durable-ownership 复审收口

归档后的故障探针发现“全绿”仍掩盖了若干 commit acknowledgement 与 process owner 间隙。本节记录最终实现，
并视为第28节完成定义的一部分。

### 29.1 RunStart full commit 后立即取得 owner，失败则先写 RunEnd

- `_commit_new_run_entry()` 不再把“commit 成功”和“owner 安装”分给两个 caller step；full commit 后在同一个
  commit owner 内调用 `_adopt_committed_host_run()`。
- owner 安装异常时，不允许返回无主 `CommittedHostRunEntry`；先以 RunStart 中冻结的
  `terminal_run_end_event_id` 写稳定 `RunEnd(runtime_execution_error)`，确认 durable 后才传播 architecture error。
- resume batch full commit 后的 fold/swap/working-set 异常使用相同 terminalizer；原 run 不得留下可继续但无法解释的
  continuation。
- initial/continuation 的 post-commit publication failure 分别标记
  `publication_status="failed_after_commit"`，terminal reason 使用
  `runtime_publication_failure`，不得伪装为 completed/runtime_execution_error。

### 29.2 RunEnd confirmation 是唯一 terminal truth

- `LoopStatus` 只表示执行状态，不证明 `RunEnd` 已 durable；Host 仅在 `state.finalized=True` 时设置
  `owner.terminal_state="confirmed"`、解析 `run_completion` 并清除 active run。
- RunEnd 多次写入失败时保留 owner、stable terminal candidate、LoopState 与 retry authority；session继续 fail closed。
- `stop_current_turn()` 与 `HostSession.aclose()` 可重试 frozen candidate；close deadline/写入失败必须阻止 HostCore
  释放 session、terminal lease 与 workspace。
- terminal confirmation 后同步 retirement execution handles、断开borrow callback环并从registry移除owner；长session
  不得按run数保留历史LoopState/transcript。

### 29.3 Boundary confirmation 比较完整稳定 payload

`HostRunBoundaryAttempt`保存：

- `candidate_events`；
- `candidate_event_ids`；
- `candidate_payload_fingerprints`。

confirmation 使用EventLog immutable payload equality，而非只按ID存在性判断。结果覆盖
`none/full/partial/conflict/unknown`：partial/conflict/confirmation exception均latch session。无论confirmation自身如何失败，
`_finish_boundary_attempt_safely()`都必须解析attempt completion，stop/close不得无界等待无人能完成的Future。V1不提供
online ledger repair authority；unknown/partial/conflict只能close/reopen或显式offline repair。

### 29.4 Compaction Started→terminal 由service-owned bounded owner负责

- Started durable 后立即注册 `CompactionTerminalizationOwner`；Completed/Failed stable candidate在commit前冻结。
- cancellation不再无界`await` writer task。RuntimeSession port用stable candidate执行`confirm_event_batch()`；后台publication
  task只被有界持有并消费结果。
- terminal pre-commit失败保留candidate；下一次compaction与Host close必须先bounded drain。
- service启动时扫描orphan Started，以Started冻结的terminal ID构造
  `recovery_terminalization/recovered_interrupted` Failed fact；不得从当前模型/config重新规划。
- pending owner drain失败是close blocker，不能只在Inspector中计数后继续teardown。

### 29.5 Prepared carrier、child evidence与capability identity

- `prepare_agent_run_draft()`只接收显式 current user、RunStart/RunEnd IDs、permission、target、capability basis、host/child
  entry facts；不得读取scratchpad，也不得在transcript freeze之后repair dangling children。
- Host repair dangling child发生在transcript/watermark freeze前；production child明确构造并消费
  `PreparedSubagentRunEntry`。
- synthetic native terminal/tool evidence仅允许`complete_fake()`测试seam；production explicit completion缺真实child
  RunStart/RunEnd或pre-terminal `report_agent_result` tool evidence时fail closed。
- capability projection semantic fingerprint排除exposure-scoped entry/fragment artifact IDs；fact fingerprint仍保留artifact
  attribution。相同模型可见文本必须命中continuation reuse。
- execution handle borrow tracker接入真实tool-call acquire/release；swap只保留有live borrow的old handle，borrow归零自动close。

### 29.6 迟到commit、frozen execution truth与最终retirement

- `retire_confirmed()`首次因borrow非零返回时，borrow tracker callback不仅关闭最后一个retiring handle，还必须以
  `(run_id, owner identity)`做ABA-safe registry删除。handle已closed但owner仍留在registry属于生命周期泄漏。
- Compaction Started在caller cancellation时若confirmation暂为missing、底层write task仍在运行，commit port必须把
  `PendingCompactionEventCommit`转交service；不得只挂done callback后忘记该write。service在Started commit前先注册
  provisional owner，close/safe point bounded resolve task：确认full则用原terminal ID补
  `recovery_terminalization`，确认none则删除provisional owner，confirmation exception/unknown/timeout继续作为
  pending owner与close blocker；“无法确认”绝不等于“没有提交”。
- `FrozenCapabilityExecutionSurface`必须进入`AgentRunDraft`、`RunWorkingSet`和`BoundaryExecutionHandles`。Agent exposure
  只能消费draft中的surface，禁止从scratchpad回读；initial与resume execution handles均在durable commit前由boundary
  attempt捕获，commit fold不得重新读取当前live wiring。
- `CurrentUserMessageFact`是draft builder唯一user source：`UserMsg.content/id/created_at`与`RunStart.user_input_chars`
  全部从该fact派生，删除并行`user_input`参数。
- borrow authority携带handle ID/generation并与handle state原子绑定；`retiring/closed`后authority与底层tracker都拒绝新borrow。
  detached child不引用parent handle：immediate与dependency-scheduled child都在统一child runner创建child-owned frozen
  execution handles，由`ChildExecutionRegistry`持有到coroutine fully drained；child tool loop只在真实在途调用期间执行
  `borrow_child_tool_call/release_child_tool_call`，不引入child-lifetime borrow。

### 29.7 Deferred child release与同步线程工具owner

- `ChildExecutionRegistry.attach_execution_handles()`必须给tracker安装以`subagent_run_id + exact handles identity/generation`
  为条件的callback。只有child已经`release_requested/closing`且coroutine不存在或done时，最后一个borrow归零才可重试
  `_finalize_release()`；普通active borrow变化不得提前关闭child。成功收口同时释放child session、capacity reservation、
  MCP reverse index并清callback。
- sync tool不能用awaiting coroutine的`finally`代表真实执行结束。生产路径把`asyncio.to_thread()`包装为独立shielded
  thread task；borrow由该task真实completion callback释放。外层取消后tool-batch driver继续等待thread task，Host
  stop/close只可在bounded deadline内等待，超时保留run/handle/session owner并禁止RunEnd/teardown越过仍在运行的副作用。
- `RunExecutionOwnerRegistry.wait_until_retired()`提供bounded close barrier；confirmed RunEnd不等于process resources已释放。
