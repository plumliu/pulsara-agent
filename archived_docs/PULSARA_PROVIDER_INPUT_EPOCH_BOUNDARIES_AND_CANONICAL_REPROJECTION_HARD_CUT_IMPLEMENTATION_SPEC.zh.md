# Pulsara Provider-Input Epoch 边界与 Canonical 重投影 Hard-Cut 实施规格

> 状态：**FROZEN — 已吸收 5.6-sol xhigh 第八轮 critic 的唯一 Major 与全部 Minor；按用户指示完成最后一轮修订，不再复审**
>
> 修订日期：2026-09-18
>
> 最高约束：仓库根目录 `AGENTS.md`。本文只具体化其“只有 new cold epoch 与 explicitly adopted compaction successor 可以重建 provider-input root”的规则，不扩张第三种 root rebuild 边界。
>
> Hard-cut 授权：本项目仍处于主动开发期。本轮允许直接修改 clean-v0 PostgreSQL baseline、删除旧 schema、重置经核验的数据库、修改内部类型与公开测试；**不保留旧路径兼容，不做双读、双写、旧枚举 alias、fallback、在线迁移链、feature flag 或过渡 repair**。
>
> 权威覆盖：本文覆盖下列早期规格中“compatibility 字段变化本身即可授权 reset/reprojection”的条款：
>
> - `ROUND_3_1_PROVIDER_INPUT_PREFIX_CONTINUITY_IMPLEMENTATION_SPEC.zh.md` 的通用 `ProviderInputEpochResetReason` reset 语义；
> - `ROUND_5B_LONG_HORIZON_CONTEXT_COMPACTION_IMPLEMENTATION_SPEC.zh.md` 中把 `CONTEXT_BINDING_REWRITE` 本身当作 reset authority 的条款；
> - `ROUND_6_MCP_PRODUCTION_CAPABILITY_IMPLEMENTATION_SPEC.zh.md` 中 tool surface 变化自动建立新 epoch 的条款；
> - `ROUND_9_UNIFIED_CAPABILITY_SEMANTICS_IMPLEMENTATION_SPEC.zh.md` 中 `MODEL_TARGET_CHANGED | PROVIDER_LOWERING_CHANGED` 机械授权 wire reprojection 的条款；
> - `PULSARA_ROUTE_WIRE_API_MODEL_UNIVERSE_AND_ADAPTER_HARD_CUT_IMPLEMENTATION_SPEC.zh.md` 中 compatibility mismatch 自动建立 cold successor 的条款；
> - `PULSARA_MODEL_SWITCH_HANDOVER_COMPACTION_AND_RECENT_WINDOW_HARD_CUT_IMPLEMENTATION_SPEC.zh.md` 中以 A/B same-epoch incompatibility 本身解释 cold reset 的条款；
> - 其他规格中依赖 `BASE_SYSTEM_CHANGED`、`TOOL_SURFACE_CHANGED`、`MODEL_TARGET_CHANGED`、`PROVIDER_LOWERING_CHANGED` 或 `CONTEXT_BINDING_REWRITE` 自动重建已安装 root 的内容。
>
> 这些历史文档中与本文不冲突的 canonical、replay、compaction、adapter、tool、memory、Hook、Skill、MCP、model-switch 三档与 resource-boundary 语义继续有效。实现本 hard cut 时，还必须同步修订 `PULSARA_MEMORY_GOVERNANCE_TERMINAL_CLAIM_SOURCE_SEMANTICS_AND_PROMPT_HARD_CUT_IMPLEMENTATION_SPEC.zh.md` 中仍把旧 provider columns 当作 canonical facts 的内容。
>
> 非覆盖声明：本文保留 Round 5A.2 / Round 5B 已激活的 bounded replay manifest 与 metadata-first、final-selected-compatible-only body hydration 合同；本轮只改变 canonical row/replay relation 与 epoch authority，不授权全量读取 opaque replay bodies。

---

## 0. 执行结论

本轮进行一次完整 hard cut：**provider-input root 的重建权从“兼容性差异枚举”迁移到两个且仅两个架构边界：new cold epoch 与 explicitly adopted compaction successor。**

模型切换不是第三个架构边界，而是 new cold epoch 的显式产品子路径。实现层采用四个封闭 transition arm，其中 `InstalledEpochAppend` 不创建新 epoch，另外三个 arm 表达两类合法 root rebuild：

最终只有以下路径可以产生新的 provider-input epoch nonce：

```text
1. continuity slot 持有 owner 签发的 bootstrap lease，当前 read 已生成一次性 authority
   -> Empty-scope cold bootstrap

2. 下一条 NEW_TURN 已冻结一个与 installed source 不同的显式 destination target
   -> Explicit model-switch cold epoch

3. Canonical compaction snapshot/binding 已原子采用，且同一 Host 仍持有 exact predecessor
   -> Adopted compaction successor
```

若第 1 条的Empty cold在首次install前就采用compaction，7.6 节先执行不产生nonce的FULL reseal，再重新走第 1 条；它不是第四个new-epoch arm。

`current_view(scope) is None`、字典中不存在 slot、普通错误后的 cleanup、任意 `discard_scope()` 或 compatibility mismatch **都不是** cold authority。Host 重启、首次创建 ROOT、新建 SUBAGENT_TASK、FULL adoption 后 no-continuation 与用户显式 runtime reopen，必须先由 lifecycle owner 签发 lease，再由 safe-point owner 对exact current handle/read/capability/target签发sealed basis，最后由continuity owner消费basis签发一次性 authority；terminal child永远不能重新打开。

其余所有路径只能：

```text
installed SYSTEM                   byte-identical
installed provider tools          byte-identical
installed semantic messages       append-only by suffix
installed provider wire root      exact prefix
installed epoch nonce             unchanged
```

以下事实永远不能自行授权 root rebuild：

- base SYSTEM observation 变化；
- tool、MCP、Skill、Hook、permission、memory、workspace 或 runtime discovery 变化；
- physical reconnect、credential refresh、registry refresh 或 UI refresh；
- target、estimator、message lowering、native-tool lowering 或 replay contract 的偶然漂移；
- context-base identity 不同，但无法证明它来自已采用的 compaction；
- 任意 fingerprint、digest、版本字符串或测试专用 flag 不同。

### 0.1 最终控制流

```text
canonical relational truth
  + current canonical context binding
  + one approved epoch boundary
  + boundary-frozen SYSTEM / tools / target / adapter
        |
        v
provider-neutral typed input
        |
        +--> validate/select provider-native replay attachments
        |        absent / well-formed incompatible -> public semantic projection
        |        manifest corrupt or selected-compatible body corrupt
        |          -> typed fail, provider open = 0
        v
destination adapter-owned materialization
        |
        v
exact final-wire quote and admission
        |
        v
continuity CAS validates the typed transition
        |
        v
install epoch/append -> provider open
```

模型 A 已安装的 Chat/Responses payload 不作为模型 B 的转换输入。A -> B 的 source of truth 是 canonical effective context；B 使用自己的 adapter 从该 truth 重新投影。

### 0.2 非目标

本轮不：

- 引入 durable epoch journal、checkpoint、receipt、generation table 或 replay reducer；
- 把 process-local provider-input epoch 持久化；
- 增加任意总 turn、model-call、tool-call、retry 或 worker lifetime cap；
- 复制 provider SDK 已有的 transport/parser/state machine；
- 把 provider-native replay 升格为 canonical semantic truth；
- 为旧数据库、旧枚举或旧测试保留兼容路径；
- 通过 hash 证明 owner authority、adoption authenticity 或 process-local object identity。

---

## 1. Canonical truth 与 provider projection 的最终边界

### 1.1 Canonical semantic truth 与模型无关

Canonical transcript 与 context snapshot 表达：

- 用户提交的 typed content；
- assistant 的公开文本、数据与 tool request；
- tool call ID、name 与 canonical arguments；
- tool result、late outcome 与 terminal observation；
- plan continuation、inter-agent message；
- 已采用 compaction snapshot 与其 continuation 语义。

它们不表达：

- OpenAI、Anthropic、Google 或其他 provider 品牌；
- Chat Completions、Responses 或未来 wire API；
- HTTP request shape；
- provider message/tool JSON key；
- 当前 API key、connection secret 或 physical client；
- 某个 provider cache 的命中状态。

`FrozenProviderInputItem` 是 canonical read 的 provider-neutral typed projection，不是 provider payload。

### 1.2 Effective canonical context

新 epoch 不总是读取会话创建以来的全部 transcript rows。它读取当前 context binding 指定的有效 canonical context：

```text
FULL_HISTORY
  -> canonical history through the frozen cut

SNAPSHOT
  -> adopted context snapshot base
  -> canonical suffix after snapshot source-through sequence
```

一旦 snapshot 被采用，未来 cold/model-switch/compaction successor 不得绕过 binding 复活已被 snapshot 覆盖的旧 provider context。

### 1.3 Provider-native replay 是可选 durable attachment

Provider-native replay fragment 可以保存：

- Chat closed reasoning fields；
- Responses exact output items；
- 未来 adapter 明确声明且可验证的 closed native assistant carrier。

它是 assistant canonical entry 的 durable 附件，不是 canonical row 的组成部分，也不是公开语义的唯一来源。

Replay metadata selection 必须同时满足：

- destination wire API；
- destination codec kind；
- replay contract；
- replay target compatibility；
- canonical public projection exact match；
- message placement exact match。

读取顺序固定为 metadata-first、selected-compatible-only hydration：先用 bounded manifest metadata 选择 final placement；只有 final selected 且 destination-compatible 的 attachment 才读取/验证 private payload body。Metadata-incompatible 或不存在时，使用 canonical assistant blocks 的 public semantic projection。未选中或 incompatible body 不读取、不审计，也不阻断本次 semantic continuation。不得把 A 的 provider-native payload 翻译成 B 的 native payload，不得猜测隐藏 reasoning。

### 1.4 Canonical assistant commit 与 replay attachment

当 adapter contract 要求 native replay 时，assistant semantic blocks 与 replay attachment 仍在一个 PostgreSQL transaction 中提交。两者职责不同：

- assistant entry/blocks：canonical semantic truth；
- replay fragment：可选的 target-compatible replay optimization/continuation carrier。

如果当前 adapter 要求 replay 而无法产生合法 fragment，assistant settlement 在 transaction 前失败；不得提交一条声称完成但缺少其 adapter 必需 replay 的 assistant occurrence。

---

## 2. 当前实现问题

当前实现把以下两件事混成一个机制：

1. 检测 installed compatibility 与新候选有哪些字段不同；
2. 判断调用方是否获准重建 provider root。

`_compatibility_reset_reason()` 当前可能返回：

```text
COLD_HOST_BOOTSTRAP
BASE_SYSTEM_CHANGED
TOOL_SURFACE_CHANGED
MODEL_TARGET_CHANGED
PROVIDER_LOWERING_CHANGED
CONTEXT_BINDING_REWRITE
```

之后 production path 会：

- 将 old canonical item count 归零；
- 重新 materialize SYSTEM、tools 与 messages；
- 创建新 epoch nonce；
- continuity owner 仅凭 `reset_reason is not None` 接受 incompatible successor。

这使任意 compatibility drift 都可能冒充 approved boundary，并导致：

- catalog refresh 热替换 tools；
- source observation 变化热替换 SYSTEM；
- adapter/lowering 漂移重写旧 messages；
- 未经 adoption 证明的 context-base mismatch 重建 root；
- model switch、compaction 与普通 drift 共用同一枚举，无法证明真实产品路径。

当前 `provider_assistant_replay_contract_fingerprint` 还没有进入 `_compatibility_reset_reason()` 的完整分类，进一步说明“差异枚举 = authority”不是稳定设计。

本轮不补齐这一枚举；本轮删除其 reset authority。

---

## 3. 封闭 epoch transition union

### 3.1 最终类型形状

生产 candidate 必须携带一个封闭 transition，而不是 `reset_reason`：

```python
@dataclass(frozen=True, slots=True)
class FrozenProviderPhysicalCallTarget:
    purpose: ModelCallPurpose
    model_call_binding: ModelCallBinding
    target_bundle: FrozenEpochModelTargetBundle
    input_budget: FrozenModelInputBudget


@dataclass(frozen=True, slots=True)
class FrozenEpochModelCallTarget:
    session_id: str
    turn_id: str
    model_call_index: int
    physical_call_target: FrozenProviderPhysicalCallTarget


@dataclass(frozen=True, slots=True)
class FrozenDirectSwitchAdmission:
    destination: FrozenEpochModelCallTarget
    semantic_projection: FrozenModelInputSemanticProjection
    wire_materialization: FrozenProviderWireMaterialization
    quote: FrozenProviderWireInputQuote


@dataclass(frozen=True, slots=True)
class InstalledEpochAppend:
    predecessor: InstalledEpochRuntimeCohort


@dataclass(frozen=True, slots=True)
class EmptyScopeColdStart:
    bootstrap_authority: PreparedEmptyScopeBootstrapAuthority
    seed: CanonicalColdContinuationSeed | SubagentInitialSeed


@dataclass(frozen=True, slots=True)
class ExplicitModelSwitchColdStart:
    predecessor: InstalledEpochRuntimeCohort
    admission: FrozenDirectSwitchAdmission
    _authority: object


@dataclass(frozen=True, slots=True)
class AdoptedCompactionSuccessor:
    predecessor: InstalledEpochRuntimeCohort
    destination: FrozenEpochModelCallTarget
    seed: AdoptedCompactionContinuationSeed


ProviderInputEpochTransition = (
    InstalledEpochAppend
    | EmptyScopeColdStart
    | ExplicitModelSwitchColdStart
    | AdoptedCompactionSuccessor
)
```

真实 symbol 可以根据当前模块边界调整，但 union 必须封闭、exhaustive，并保留上述四种且仅上述四种语义。

`FrozenProviderPhysicalCallTarget`是所有physical model call共用的唯一transport-free target contract；它完整冻结purpose、binding（含reasoning selection）、target bundle与call budget，但不含session/turn等产品坐标。`FrozenEpochModelCallTarget`只在其上增加epoch agent-loop的session/turn/model-call-index；其`purpose/model_call_binding/target_bundle/input_budget`只能作为委托给`physical_call_target`的只读property，不能重复存字段。Compaction summary、memory auxiliary与connection probe的permit各自直接持有一个physical target，再在permit中绑定自己的产品坐标。

这些target只能由对应boundary/product resolution owner的private factory从exact`PreparedKernelModelTarget`/resolution中抽取；返回值**不得保留**`PreparedKernelModelTarget`、`ResolvedModelTarget`、`ResolvedModelCall`、`NormalizedLLMTransport`、client/registry/credential handle或provider-open callable。Binding connection、bundle connection/target、budget与purpose必须在physical-target factory/`__post_init__`中exact-join；epoch wrapper再exact-joinsession/turn/index及`AGENT_MODEL_LOOP`purpose。`input_budget`收纳maximum/effective input/output/resource bounds，不为示意新增重复fingerprint。Native-tool wire contract不是任何call-target source field；它只能由`target_bundle.projection_strategy`派生为只读property，禁止保存第二份truth。

`FrozenDirectSwitchAdmission` 只能由model-switch owner在消费exact admitted `HandleFreeProviderWireObservation`后私有构造；它只保留execution-independent destination/materialization/quote，不保留observation内部旧measurement candidate、Prepared/Resolved call/target或transport。`ExplicitModelSwitchColdStart.destination` 是`admission.destination`的property，不另存字段；authority、transition、new-epoch candidate必须exact-bind同一admission object/value。

`InstalledEpochRuntimeCohort` 包含其 `FrozenProviderInputEpochView`，因此 transition 不再同时携带一份可漂移的 view 与另一份 target/profile。`ExplicitModelSwitchColdStart` 的 source target 只能从 exact predecessor cohort 读取；不得携带 call-specific `ResolvedModelCall` 作为第二份 source truth。Destination connection ID 从 transition destination 的`model_call_binding.connection_id` 派生，不在transition重复存储。

`AdoptedCompactionSuccessor.predecessor` 必须非空。它只表达 adoption FULL 后同一 Host、同一 scope、仍保有 exact predecessor 的立即安装或重试；destination target/binding 必须同时由 FULL confirmation exact-join。Host restart、FULL后no-continuation或slot已丢失等无 predecessor 场景，只能在 lifecycle owner 已签发bootstrap lease、当前prepare又签发exact authority时走 `EmptyScopeColdStart`，不得让adopted arm与empty arm重叠。对“尚未安装predecessor就先采用compaction”的真实路径，使用 7.6 节的non-nonce Empty reseal，不得把predecessor放宽为nullable。

### 3.2 不新增公开 transition enum

本文不要求增加 `COMPACTION_SUCCESSOR` 或其他公开枚举。不同 dataclass arm 本身已经表达类型和完整值。

如果 UI/trace 需要诊断 label，它只能从 transition 派生：

```text
EMPTY_SCOPE_COLD_START
EXPLICIT_MODEL_SWITCH_COLD_START
ADOPTED_COMPACTION_SUCCESSOR
INSTALLED_EPOCH_APPEND
```

诊断字符串不能作为 compiler、continuity 或 install 的输入，也不能反向恢复 authority。

### 3.3 Authority 使用完整值与 owner identity

Transition 验证使用：

- exact predecessor cohort object identity；
- existing epoch nonce/revision；
- exact `ModelConnectionId` / execution-independent `FrozenEpochModelCallTarget`；
- canonical turn binding；
- canonical context snapshot/binding revision；
- current owner slot 与现有 CAS；
- PostgreSQL constraints/transaction outcome。

不得新增：

- transition fingerprint；
- adoption proof hash；
- model-switch proof digest；
- `fingerprint -> transition` registry；
- durable epoch authority row；
- compatibility fallback token。

### 3.4 Authorized-empty lease，而不是裸 `None`

Continuity owner 内部 slot 至少形成以下封闭状态：

```text
AuthorizedEmpty(bootstrap_lease)
PreparingEmpty(bootstrap_lease, preparation_reservation)
BoundEmptyPreparation(bootstrap_lease, sealed_basis, preparation_authority)
Prepared(previous_state, candidate)
EmptyAdoptionPending(previous_empty_state, attempt/cut/destination)
EmptyAdoptionFullSettling(pending, full_confirmation, revocation_progress)
EmptyAdoptionConflictSettling(pending, conflict_confirmation, revocation_progress)
EmptyAdoptionResourceReleaseQuarantine(pending, revoked_resources, failure)
Installed(runtime_cohort)
InstalledNoContinuationSettling(predecessor, closure, revocation_progress)
```

`revocation_progress`是private closed union `RevocationNotStarted(exact_resources) | RevokedProviderPreparationResources`，不是nullable字段。进入settling时只能是`RevocationNotStarted`；safe-point owner完成不可失败logical revoke后，在同一owner编排下替换为`RevokedProviderPreparationResources`，physical release和最终lease publication只接受后者。所有settling arm在两种progress下都同样non-authorizing并拒绝prepare/open；quarantine只能携带已经logical revoked的resources及其未完成cleanup责任。

这里必须区分以下线性对象：

- `AuthorizedEmptyBootstrapLease`：长期驻留 slot 的 lifecycle entitlement。它只冻结 scope、Host owner/generation、sealed issuer value 与稳定 base/adoption provenance；**不冻结**尚不存在的 turn、dispatch cut 或 `source_through_sequence`。
- `EmptyPreparationReservation`：continuity owner在同步lock内占用lease后返回的private one-shot reservation；它不含canonical read，也不执行I/O。
- `OwnerIssuedCanonicalDispatchObservation`：canonical dispatch reader的private factory在自己发起并完成的exact read operation上签发的process-local one-shot carrier。它绑定read operation identity、exact active handle identity/generation/cut、scope/Host/turn与`FrozenCanonicalProviderDispatchRead`完整值；公开可构造、hash自洽的read DTO本身没有authority，不能换取该carrier。
- `OwnerIssuedCapabilityDispatchObservation`：capability/tool-surface owner的private factory签发的process-local one-shot carrier。它绑定exact handle/cut、`FrozenCapabilityDispatchCut`、`PreparedKernelToolSurface`与真实`ProcessLocalToolSurfaceBorrow` object identity。可见字段相同的capability DTO、伪造borrow或另一attempt的borrow都不能换取该carrier。
- `SealedProviderInputPreparationBasis`：只能由`ProviderSafePointCoordinator`在其lock内、对exact active `PreparedProviderInputHandle` 调用`_require_current()`成功后，**消费上述两个owner-issued carrier**并与`FrozenEpochModelCallTarget`、scope/turn/Host及本边界subject exact-join后私有签发；它禁止接收裸read/capability DTO。Empty的subject是exact continuity reservation，direct/adopted的subject是exact installed predecessor与该turn/adoption事实。Basis线性持有handle和tool-surface borrow，直到abort/close或最终provider permit转移；cohort永不持有physical borrow。
- `PreparedEmptyScopeBootstrapAuthority`：caller完成pure admission并选择direct cold后，safe-point owner再次在lock内验证exact handle/basis仍current，并调用continuity owner的私有`bind_current_basis(reservation, sealed_basis)`得到的one-shot authority。它绑定本次turn、context binding、canonical cut/source-through、capability cut、target、owner-issued observations与真实safe-point handle。

Lease 只能来自以下 lifecycle issuer：

1. 新 session ROOT scope 的首次注册；
2. fresh Host 完成 canonical takeover 后对可恢复 ROOT scope 的首次注册；
3. 新 `SUBAGENT_TASK` scope 的创建 transaction 已成功，且 child 仍为可运行状态；
4. FULL adoption 后确定不继续当前 active branch，continuity owner 将 exact installed slot 原子替换为携带 adopted-base provenance 的 `AuthorizedEmpty`；
5. 用户显式执行 session/runtime reopen：旧 Host 完整关闭，新 Host 完成 takeover 后按第 2 项注册。

实现中 lease source 使用 private sealed dataclass union，而不是 caller-provided 字符串或公开 enum：`NewSessionRootLeaseSource | FreshHostRootLeaseSource | NewSubagentLeaseSource | AdoptedBaseLeaseSource`。各 arm 分别携带现有 session genesis、writer generation、durable runnable task fact或adopted snapshot/binding完整值；runtime reopen不增加新arm，它得到的仍是 `FreshHostRootLeaseSource`。

普通代码不得公开构造lease/reservation/authority。`current_view() -> None`只是一种observation，不能作为compiler输入或install证明；不存在slot同样不能自动admission。唯一流程为：

```text
continuity.begin_empty_preparation()
  -> under RLock: AuthorizedEmpty -> PreparingEmpty; return reservation
caller obtains/holds existing provider-input safe-point handle
safe-point/canonical-reader owner begins and completes an exact handle-bound read
  -> private OwnerIssuedCanonicalDispatchObservation
capability owner borrows the exact prepared tool surface for that handle/cut
  -> private OwnerIssuedCapabilityDispatchObservation owning the live borrow
safe_point.seal_empty_preparation_basis(handle, reservation,
                                        owner_issued_read,
                                        owner_issued_capability,
                                        frozen_call_target)
  -> under safe-point RLock: consume both carriers, require exact current handle;
     return sealed basis owning handle + tool borrow
caller performs pure final-wire measurement/admission
if direct cold:
  safe_point.bind_current_empty_preparation(handle, reservation, sealed_basis)
    -> under safe-point RLock: re-require exact current handle/basis
    -> while that lock remains held, continuity.bind_current_basis(...)
    -> under continuity RLock: PreparingEmpty -> BoundEmptyPreparation; return authority
if compaction before first install:
  compaction owner consumes summary permit(s), builds snapshot and obtains dry ADMITTED
  safe_point.begin_empty_adoption(handle, reservation, sealed_basis,
                                  attempt/summary/dry/...)
    -> under the same lock ordering enter EmptyAdoptionPending; see 7.6
caller compiles and registers candidate
  -> under RLock: BoundEmptyPreparation -> Prepared
safe_point.install_bound_candidate(handle, sealed_basis, candidate)
  -> under safe-point RLock: require same current handle/basis/candidate
  -> synchronously continuity CAS: Prepared -> Installed
  -> mark exact handle model-active; linearly transfer handle + tool borrow into
     one-shot EpochAgentLoopProviderOpenPermit / ProviderDispatchExecutionAuthority
```

Continuity owner仍是纯process-local同步owner，**不读写PostgreSQL、不await I/O**。两owner锁顺序只能是`safe-point -> continuity`；continuity不得回调safe-point owner。Owner-issued observations/basis创建、continuity bind/empty-adoption begin与final install/model-active各自都由safe-point owner在持有exact handle lock并重新`_require_current()`时编排，因此handle不能在某次验证和对应state transition之间rotate/close；若它在这些阶段之间被关闭/轮换，下一次验证必须fail closed。Epoch agent-loop transport只能在`EpochAgentLoopProviderOpenPermit`产生后borrow；cohort不保留handle、tool borrow、basis、authority或permit。Basis在任何失败/abort路径必须同时关闭handle与tool borrow；成功路径只允许把两者线性转移给既有`ProviderDispatchExecutionAuthority`或其hard-cut等价物。`EmptyAdoptionPending` 只用于 7.6 节的source-less compaction adoption in-flight，不产生nonce或epoch agent-loop provider-open能力；compaction summary使用7.2节单独的purpose permit。

所有`*Settling`与`*ResourceReleaseQuarantine`状态都是private、non-authorizing current states：`begin_empty_preparation()`、adopted retry、candidate register/install、provider permit与任何lease issuer必须exhaustive拒绝，不能把它们当作`None`或Empty。它们只在safe-point owner已安装逻辑revocation fence后短暂存在，或在physical resource release失败时保留到Host close/进程重启；不得设置timeout后自动恢复，不新增durable receipt/repair job。

Reservation、owner-issued observations、sealed basis/authority、candidate各阶段都必须有exact one-shot `abort()/close()`：在caller `finally` 中，若Host仍open且exact object仍占有slot，就只恢复原lease；旧reservation/carrier/basis/authority/candidate永久失效，basis已取得的handle与tool borrow一并关闭。Read failure、capability/target freeze failure、compile failure、cancellation与candidate尚未构造时都必须可恢复。Handle rotated/closed、未由owner签发但可见字段相同的read/capability、cross-attempt borrow/target/basis均fail closed且provider open=0。Host close则使所有outstanding handles/borrows失效并释放slot，不恢复lease到已关闭owner。下一次prepare必须从原lease重新begin并读取canonical truth，不能复用旧read/carrier/basis/authority。

新 session/fresh Host lease 可以允许未来首次 turn 的 canonical cut；idle/FULL-adoption lease 必须绑定 exact adopted snapshot/context-base provenance，但允许该 base 之后合法追加的 canonical suffix。它不得要求未来 `source_through_sequence` 等于 lease 签发时的值。Prepared authority 则 exact-bind 本次读取到的最终 through sequence。

---

## 4. Installed epoch append

### 4.1 Compiler contract

Ordinary `compile_append()` 只允许：

```text
new messages = installed messages || suffix
```

它必须要求：

- planning predecessor 是 exact installed runtime cohort；
- candidate 的 SYSTEM、tools、target、adapter/materializer、estimator 与 replay policy 全部来自该 cohort，不重新 resolve；
- canonical frontier 保持 prefix；
- SYSTEM 与 installed 值完全相等；
- provider tools 与 installed 值完全相等；
- native tool projection set 与 installed 值完全相等；
- semantic message prefix 完全相等；
- provider wire root/tools/ordered items 保持 exact prefix；
- epoch nonce 不变；
- revision 只按现有成功 install 规则递增。

`compile_append()` 不得调用 full `compile()` 来重建 installed prefix，不得因为任何 compatibility mismatch 把 `old_count` 归零。

### 4.2 Root-stable producer 行为

Ordinary append 不应每次读取最新 SYSTEM/tools 后再通过 mismatch 决定 reset。它应直接复用 installed root：

- BASE_SYSTEM 使用 installed exact body；
- direct provider tools 使用 installed exact specs；
- native projection 使用 installed exact projection objects；
- installed adapter/target/materializer/estimator 使用 exact cohort value；
- root-bound Skill/MCP/tool descriptions 不因 reload 改变。

允许按既有 lifecycle 产生 suffix 的 source 仍可追加 suffix，但它们不能回写或重排已安装 root。

### 4.3 Runtime capability change

新增、删除、替换或重连 Tool/MCP/Skill 时：

- 当前 epoch 的 provider tools 保持不变；
- late capability 不提升到当前 direct cohort；
- cohort 只冻结 immutable semantic specs/native projection，不长期持有 MCP session、tool process、provider client、credential handle 或其他 physical resource；
- 每次执行由 dependency owner 临时借出 physical runtime，并 exact-match installed semantic spec；
- 若相同 semantic spec 的 physical execution 已真实不可用，返回 typed unavailable/failure；
- 新 capability 只在下一 approved cold/compaction boundary 冻结。

不得以“避免当前调用失败”为由偷偷重建 provider root。

为了让长期低上下文会话能够主动采用新 capability，产品必须实现 5.6 节唯一的 safe runtime reopen API。它不追加 durable epoch event、不增加第三种架构边界，也不得由 catalog watcher 自动触发。

---

## 5. Empty-scope cold bootstrap

### 5.1 唯一授权事实

Cold bootstrap 的必要且充分 process-local authority 是：

```text
continuity slot == AuthorizedEmpty(exact unconsumed bootstrap lease)
continuity owner synchronously reserves that lease; it performs no I/O
caller holds the existing safe-point handle and freezes the current canonical dispatch read
safe-point owner proves the exact handle is current and seals handle/read/capability/target
continuity owner consumes that sealed basis against the reservation and issues authority
candidate carries the newly issued exact PreparedEmptyScopeBootstrapAuthority
candidate/install exact-bind the same handle/basis and lease scope/Host/issuer/base provenance
```

slot 裸空、`current_view(scope) is None`、`dict.get(scope) is None` 或 caller 声称“这是 cold”均不充分。适用 issuer 仅为 3.4 节列出的 scope birth、fresh Host takeover、new subagent、FULL-adoption no-continuation arm 与显式 runtime reopen。

### 5.2 输入来源

Cold bootstrap 从当前 canonical binding 读取 effective context，并冻结当时的：

- current base SYSTEM；
- current tool/Skill/MCP capability cut；
- current target 与 adapter profile；
- current replay target；
- current source observations；
- current exact final-wire admission。

它不恢复旧 process-local epoch，也不需要 durable reset record。

### 5.3 类型约束

`CanonicalColdContinuationSeed` 只允许 `predecessor_view is None`。当前“只要 `context_base_changed` 就自动构造 Canonical cold seed”的路径必须删除。

`SubagentInitialSeed` 继续只允许新 SUBAGENT_TASK scope；现有 private authority/object-identity pattern 保留。

Cold candidate 必须同时 exact-join prepared authority 内冻结的 scope、Host owner identity/generation、exact active `PreparedProviderInputHandle` identity/generation/cut、当前 canonical binding/source-through frontier、capability cut、`FrozenEpochModelCallTarget` 与 lifecycle kind。对于 adopted-base lease，当前 read 必须以 exact adopted snapshot/context-base 为 base，并可包含其后的 canonical suffix。Terminal/COMPLETED/INTERRUPTED child 不得签发或恢复 `SubagentInitialSeed`；ROOT scope 的 authority 不得用于 child，反之亦然。

### 5.4 删除 generic scope reset

现有 `discard_scope()` 必须删除，并按 owner 语义拆成互不替代的操作：

- `close_host_scope()`：仅 Host teardown 使用；关闭整个 continuity owner并释放所有 slot；同一 owner不能再 bootstrap；
- `retire_terminal_subagent_scope()`：仅在 durable task 已 exact-confirm existing closed `SubagentTaskStatus.terminal is True` 后，从 bounded continuity slots 移除 child并释放并发容量；不保留 process-local tombstone map；
- `retire_full_adoption_without_continuation_and_arm_empty()`：只能由safe-point owner消费exact FULL confirmation、exact predecessor、current no-continuation admission fence与private one-shot closure；先进入non-authorizing settling并撤销全部successor capabilities，最后才发布`AuthorizedEmpty(adopted-base lease)`；
- ordinary candidate `discard()`：销毁本次 prepared authority/candidate，只退回 prepare 前的 exact lease 或 installed cohort，不关闭 scope、不创建新 entitlement；
- explicit runtime reopen：关闭整个旧 Host/session runtime，由新 Host takeover；不调用同一 owner 的清空 API。

`NoContinuationClosure` 是以下 private sealed union，不接受 caller boolean/string/assertion：

```text
IdleTargetNoContinuationClosure
DurableTurnNotRunningClosure
PostCompactBlockedClosure
SessionStartCompactBlockedClosure
SuccessorFinalAbandonmentClosure
```

前四种product evidence分别只能由idle target owner、durable turn lifecycle read、exact`PostCompact` outcome或exact`SessionStartCompact` outcome的private factory签发；final-abandonment evidence只能由compaction/turn settlement owner签发。**这些product evidence本身不是最终closure。** Compaction coordinator必须先安装private one-shot`NoContinuationAdmissionFence`。Fence以触发它的FULL confirmation、attempt/cut、turn与destination证明真实性，但其current admission占位键是`scope + exact predecessor object identity`：一旦安装，所有仍试图从该predecessor创建adopted seed/transition/basis/candidate/permit的入口都立即拒绝，包括使用不同新attempt ID、cut或destination的retry/preparation；它不是只挡触发attempt。Fence安装只改变process-local current admission state，不增加durable event/job/relation；其锁不得在取得safe-point lock时继续持有，锁序固定为“先安装fence并释放compaction lock，再`safe-point -> continuity`”。Fence只在lease成功发布后随predecessor退休，或随Host关闭一起失效；release/CAS失败时保持current。

Safe-point owner随后在自己的lock内重新验证fence仍current、exact handle generation与product evidence，才把对应arm私签为最终`NoContinuationClosure`并立即消费。检查集合必须穷尽：`PreparedProviderInputHandle`、`SealedProviderInputPreparationBasis`、`ProcessLocalToolSurfaceBorrow`、`AdoptedCompactionContinuationSeed`、`AdoptedCompactionSuccessor` transition、prepared candidate、continuation handle、empty reservation/authority、所有summary/agent-loop provider-open permit/borrow与provider stream。任何已open permit/stream仍在settlement中时不得签closure；fence保持阻止新retry，待其physical settlement FULL后重新检查。Final-abandonment arm不能用“已关闭preparation”文字代替上述exact集合。

`retire_full_adoption_without_continuation_and_arm_empty()`只能由safe-point owner在同一critical section编排：先让continuity exact consume`Installed(predecessor)`与closure进入non-authorizing`InstalledNoContinuationSettling`，然后对所有尚未open的seed/transition/candidate/basis/handle/tool borrow执行与7.6相同的不可失败logical revoke，再执行physical release。只有release FULL且再次exact-joinFULL confirmation、scope/turn、attempt/cut、adopted snapshot/binding后，continuity才允许`InstalledNoContinuationSettling -> AuthorizedEmpty(AdoptedBaseLeaseSource)`。Release/CAS失败时continuity仍停在无lease的`InstalledNoContinuationSettling(..., RevokedProviderPreparationResources)`；所谓quarantine仅是Host/controller层关闭结果，**不得增加第二个continuity quarantine arm**。`begin_empty_preparation()`与adopted retry都拒绝。由此新retry既不能在closure检查与CAS之间插入，也不能与新Empty entitlement并存。

该流程覆盖且仅覆盖：原idle compaction、adoption FULL后turn已不再RUNNING、`PostCompact`/`SessionStartCompact`阻止continuation，以及post-FULL successor preparation/install已最终放弃。它不是第三种root boundary；canonical adoption已发生，该操作只关闭旧process-local predecessor的继续资格，并为未来cold read保存adopted-base provenance。若当前branch仍要立即继续，则不得安装fence/签closure，必须保留predecessor并走`AdoptedCompactionSuccessor`。

本操作和这五种closure只适用于**已存在`Installed(predecessor)`**的compaction。若首次cold install之前没有predecessor，不得伪造closure或nullable predecessor；必须走 7.6 节的FULL Empty reseal。

Terminal child 防重开不依赖 tombstone。后续 registration 必须重新 exact-read durable task，并且只有一个全新的 task creation transaction、状态仍为 runnable 时才能签发 lease；同一 terminal task ID 永远不能重新注册。并发上限只统计当前 active/prepared/installed child slots，绝不能演变成总生命周期上限。

Assistant settlement NONE、ACK-unknown、replay reservation cleanup、provider error、adapter unavailable、compile error 或普通 cancellation 都不得关闭 installed scope。若 assistant canonical winner 已存在但 replay attachment 缺失或不匹配，按 replay corruption/ACK-unknown 规则 fail closed；不得通过 scope reset 将损坏隐藏成 cold start。

### 5.5 Host takeover 是 cold bootstrap 的前置条件

Fresh Host 恢复已有 session 时，必须先通过现有 writer takeover transaction 将旧 generation 的 `RUNNING` turn 与 `PENDING_START | WAITING_DEPENDENCY | ACTIVE` subagent 标为 `HOST_TAKEOVER` interruption，再允许 continuity owner 为 ROOT 签发 bootstrap lease。顺序固定为：

```text
acquire new writer generation
  -> atomically interrupt prior running authority
  -> read current canonical binding
  -> register AuthorizedEmpty(lease) for recoverable ROOT
  -> on a later provider preparation, freeze current read and issue authority
```

不得尝试恢复旧 provider stream、旧 prepared candidate、旧 tool batch、旧 epoch view 或旧 process-local permit。若 takeover transaction 未确认 FULL，bootstrap lease/authority均不得签发，provider open=0。

### 5.6 唯一 safe runtime reopen 产品路径

本 hard cut 新增唯一用户可达、process-local 产品命令：

```text
POST /api/sessions/{session_id}/runtime/reopen
  -> LocalHttpServer._reopen_session_runtime(...)
  -> operation: PreparedRuntimeReopenOperation | PreparedRuntimeResumeOperation
       = LocalSessionController.prepare_runtime_reopen(session_id)
  -> detach_outcome = LocalBrowserBridge.detach_session_for_runtime_reopen(operation)
  -> match detach_outcome:
       BridgeDetachNotStarted(reason)
         -> LocalSessionController.abort_runtime_reopen(operation, reason)
       BridgeDetachFailed(token, error)
         -> quarantine(operation, token, error)
       BridgeDetachFull(token)
         -> host_outcome = match operation:
              PreparedRuntimeReopenOperation
                -> LocalSessionController.prepare_runtime_reopen_close(operation)
              PreparedRuntimeResumeOperation
                -> NoOldHostReadyToResume(operation)
         -> bridge_settlement = LocalBrowserBridge.settle_runtime_reopen_detach(
              token, host_outcome)
         -> no_live_observation = LocalSessionController.finalize_runtime_reopen(
              operation, host_outcome, bridge_settlement)
         -> LocalSessionController.resume_session(
              session_id, no_live_observation=no_live_observation)
```

请求 body 为 closed empty shape；它不是 `session_commands` canonical command，不写 durable event/job/relation。Raw `close_session`、cancel/interrupt 与 safe reopen 是不同产品操作，不得互相 alias。

`LocalSessionController` 是 Host publication/gate 的唯一线性化 owner；`LocalHttpServer` 是同时持有 `sessions` 与 `bridge` 的 HTTP coordinator，因此由它排序 bridge detach，不向 `LocalWebApplication` 新增穿透端口。Controller必须维护 per-session current fence，使 `resume_session`、raw close、第二次 reopen 与新 dispatch不能越过正在进行的操作。通用`disconnect_session()`不能用于任何会unpublish/close Host的路径，因为它没有与`connect()`共用per-session lock，也不绑定controller operation。本hard cut让raw close先取得sealed`PreparedRawCloseOperation`并安装`RawCloseInFlight`，再通过同一底层lock/gate/outcome machinery的`detach_session_for_raw_close(operation)`清bridge，最后才unpublish/physical close；它仍是关闭canonical conversation的独立产品操作，不能alias为runtime reopen。

唯一顺序为：

1. HTTP owner调用`prepare_runtime_reopen(session_id)`；controller在admission lock下确认该session没有其他current operation。若存在live Host，它选定exact published`HostSessionHandle`、安装reopen fence，再调用旧Host的`prepare_safe_runtime_reopen()`原子阻止新admission并确认quiescence，返回private sealed one-shot`PreparedRuntimeReopenOperation`，旧Host此时仍published。若不存在live Host，它必须在同一lock内完成下文定义的完整no-live检查并直接安装`RuntimeReopenInFlight`，返回携带该次one-shot证明的`PreparedRuntimeResumeOperation`；两种arm是closed union，caller不能用nullable old handle或bool分支伪造；
2. 若任一owner busy，或controller current state是`ResumeInFlight | RawCloseInFlight | RuntimeReopenInFlight | Quarantined`，不创建operation handle；旧Host若存在则继续保持原状态，返回typed`RUNTIME_REOPEN_BUSY`或quarantine结果；
3. HTTP owner在controller fence仍持有时执行`bridge.detach_session_for_runtime_reopen(operation)`。该API取得与`connect()`完全相同的per-session bridge lock，在锁内向controller exact-confirm operation/fence，先安装bridge gate并创建private one-shot token，再枚举、移除并尽力关闭全部连接。它不得在安装gate或发生连接副作用后裸抛；closed结果只能是`BridgeDetachNotStarted(reason)`（无gate/无副作用）、`BridgeDetachFull(token)`或`BridgeDetachFailed(token, typed_error)`。后两者都携带exact token；即使某个`aclose()`抛错，也继续settle其余连接后返回FAILED，gate保持current；
4. 同session lock保证：pre-fence connect若已持锁，会先完成publication再被detach纳入；尚未调用`resume_session()`的connect在controller fence失败；detach gate之后的新connect在bridge gate失败。因而不存在“取得old Host handle但尚未发布，detach却漏过”的窗口；
5. 对`NotStarted`，controller可直接exact abort并清自己的fence。对`DetachFailed(token)`，物理browser connection是否关闭已不可证明，**不得恢复old Host为可用状态**；HTTP owner把controller operation与bridge token一起转为唯一current`CrossOwnerRuntimeReopenQuarantine`，bridge gate保持closed，所有resume/reopen/dispatch/raw close/connect拒绝，只能完整重启进程；
6. 对`DetachFull(token)`，若在unpublish前取消，controller先`prepare_abort_runtime_reopen(operation)`但继续保留Host gate/current fence，bridge再消费token执行`settle_runtime_reopen_detach(..., ABORTED)`。只有bridge settlement FULL后，controller才`finalize_abort_runtime_reopen()`恢复old Host并清fence；settlement失败转`CrossOwnerRuntimeReopenQuarantine`，不能先恢复Host；
7. 继续commit时，old-Host arm由controller消费operation执行`prepare_runtime_reopen_close()`：在lock下重新验证handle/gate/identity，unpublish旧Host，在lock外完整执行`core.close_session(old_host_id, close_conversation=False)`，但保留per-session current fence，且**暂不创建/发布新Host**。其closed结果是`OldHostCloseFull`或`OldHostCloseQuarantined`，后者保留old host/close-attempt identity。No-old-Host arm不得调用close API或伪造`OldHostCloseFull`；controller只接受原`PreparedRuntimeResumeOperation`并产出独立closed outcome `NoOldHostReadyToResume(operation)`，current fence继续保留；
8. `OldHostCloseQuarantined`要求bridge把同一token settle为quarantined-closed gate；两边都保持current quarantine。`OldHostCloseFull | NoOldHostReadyToResume`要求bridge消费token释放gate；只有bridge settlement FULL后，controller才在admission lock内调用统一`finalize_runtime_reopen(...)`，重新证明无published Host、无残余连接且operation仍current，并返回exact one-shot`StableNoLiveHostObservation`。任一bridge settlement失败都转`CrossOwnerRuntimeReopenQuarantine`；controller不得提前清fence或凭`NoOldHostReadyToResume`直接resume；
9. HTTP owner必须把该exact observation显式传给`resume_session(session_id, *, no_live_observation=...)`。该方法在同一controller admission lock下重新验证并消费observation，原子地把`RuntimeReopenInFlight`替换为`ResumeInFlight`，之后才开始异步Host创建；它不得把observation丢弃后另做一次裸live-map检查。并发connect只会与同一个normal resume owner合流；new Host完成writer takeover/interruption后publish。Resume失败返回typed`RUNTIME_CLOSED_REOPEN_DEFERRED`并清理为可重新签发no-live observation的无current-op状态，不复活old handle；
10. 新Host的ROOT continuity owner签发fresh-host bootstrap lease，下一provider preparation从canonical truth cold bootstrap。

Quiescence 必须至少证明以下 owner 全为空闲，不能只检查 active turn：

- 无 accepted/running turn、pending/queued/steer delivery；
- 无 provider stream、assistant settlement/replay reservation；
- 无 tool execution、未决 side effect/ACK-unknown closure；
- 无 compaction planning/adoption/successor install；
- 无 nonterminal subagent/delegation/message settlement；
- 无 terminal process或terminal monitor工作；
- 无 Hook interaction/dispatch、MCP/extension call、memory write或其他已接纳 physical work。

“当前没有live Host”不能从`_by_session`缺项推导。Controller在同一admission lock下同时确认：无published handle、无`ResumeInFlight | RawCloseInFlight | RuntimeReopenInFlight | Quarantined` sparse current operation、无尚未登记完的resume/close task，且canonical session仍可resume，才私签one-shot`StableNoLiveHostObservation`。它不能成为长期map entry。Runtime-reopen路径由`prepare_runtime_reopen()`在该lock内立即消费初始observation并安装`RuntimeReopenInFlight`；双owner settlement后，`finalize_runtime_reopen(...)`返回一个新的exact observation，且只能由`resume_session(session_id, *, no_live_observation=...)`在重新校验后消费并原子替换为`ResumeInFlight`。普通非reopen的`resume_session(session_id)`允许在同一lock内直接执行相同no-live检查并原子安装`ResumeInFlight`，但不得接受后忽略一个已传入的observation，也不得在异步Host创建开始后才补登记current operation。`ResumeInFlight`到publish FULL或失败后移除；raw`close_session()`必须在unpublish前安装`RawCloseInFlight`，physical close FULL后才移除，失败则替换为current quarantine。Runtime reopen遇到任一in-flight/quarantine或canonical terminal一律不签发operation。因此raw close的“已unpublish、physical close尚未完成”窗口和既有resume的“Host尚未publish”窗口都不能被误判成可创建第二Host，也不会为了每个历史session永久保存idle状态。

若请求开始时没有old Host，`prepare_runtime_reopen()`只能按上述规则消费初始no-live observation、安装fence并返回不含old handle的sealed`PreparedRuntimeResumeOperation`。它仍走同一bridge closed-outcome/gate协议以验证无残余连接，但host-side closed outcome必须是独立的`NoOldHostReadyToResume`，不能借用`OldHostCloseFull`。Bridge release FULL与controller finalize都完成并产出新的one-shot observation后，才可调用带显式参数的resume。Controller operation与bridge detach token都是one-shot current state，不能积累history map。HTTP cancellation只取消等待；一旦任一owner发生副作用，shielded owner task必须继续到双owner FULL settlement或current quarantine。

Bridge settlement本身也必须在same per-session lock内返回closed`BridgeSettlementFull | BridgeSettlementFailed(token, typed_error)`，不得在修改gate后裸抛或丢token。只有FULL消费token并删除gate；FAILED必须保证gate仍为或重新成为同token绑定的closed/quarantined gate，controller fence此时仍current。Controller finalize API只接受FULL value，绝不能接受caller bool/exception absence作为成功证明。

`QuarantinedRuntimeReopen | CrossOwnerRuntimeReopenQuarantine | RawCloseQuarantine`是同一controller current quarantine union的封闭arms，不是历史日志，最多每session一个current value。因为现有`KernelHostCore`的`CLOSE_FAILED_QUARANTINED`及bridge partial failure都没有可证明physical owners全部关闭的retry/settle API，本hard cut**不伪造同进程恢复**：该状态下`resume_session`、新reopen、dispatch、raw close与bridge reconnect全部typed拒绝，不得创建第二Host。唯一恢复是完整退出当前应用进程；进程退出终止所有旧physical owners，新进程不继承process-local quarantine，然后按fresh Host takeover语义恢复canonical session。UI/API必须明确告知“需重启Pulsara”，不得将quarantine误报成普通deferred。

Bridge的per-session lock/gate entry也是current资源而非无界历史：entry记录active connection数、connect/detach waiter引用数与可选current gate；只有三者全为零且没有controller current operation时，才在global bridge lock下identity-check后删除。大量session反复connect/reopen/close不得留下永久`_session_locks`或gate tombstone，也不得靠总session cap掩盖泄漏。

该命令只是用户主动结束旧 process-local epoch并创建 fresh Host；真正的新 root authority仍是 fresh Host 的 `EmptyScopeColdStart`，不是“capability changed”本身。

---

## 6. 显式模型切换 cold epoch

### 6.1 模型切换是 cold epoch 的产品子路径

模型切换不是 compatibility drift，也不是第四种 root rebuild 边界。它是：

```text
installed A
  + 下一条已接纳 NEW_TURN 冻结 destination B
  + B != A
  -> explicit model-switch cold epoch
```

当前 `turns.model_call_binding` 继续作为 durable turn selection truth，不新增 model-switch attempt/stage/event/table。

### 6.2 允许切换的时刻

只有以下条件全部满足时才可构造 `ExplicitModelSwitchColdStart`：

- 当前调用是该 NEW_TURN 的第一个 foreground model call；
- 尚未产生 semantic assistant output；
- 尚未完成或开始当前 turn 的 tool batch；
- destination 来自该 turn 已冻结的 `model_call_binding`；
- source 是 continuity owner 中 exact installed cohort，不另行 resolve；
- source connection/target 与 destination 确实不同；
- current canonical cut 与该 turn exact join；
- direct precheck 对 B 使用 B 自己的 adapter、estimator 与 hard limits。

Mid-turn 设置变化、provider retry、tool follow-up、reconnect、gateway telemetry 或 current registry drift 不得构造该 transition。

### 6.3 Tier 1：direct cold switch

若 B 的 exact final-wire candidate：

- 低于 B 的 automatic trigger；
- 满足 B token budget；
- 满足 physical byte bound；
- 满足 B modalities、tool projection 与 replay requirements；

则不进行 compaction，直接从 canonical effective context 构建 B cold epoch。

Tier 1 明确复用现有 `HandleFreeProviderWireObservation`，顺序只能是：

```text
authority-free exact HandleFreeProviderWireObservation
  -> exact final-wire fit admitted
  -> consume observation into FrozenDirectSwitchAdmission
  -> canonical/capability owners issue exact current handle-bound observations
  -> safe-point owner consumes observations into SealedProviderInputPreparationBasis
  -> model-switch owner private factory signs one-shot direct authority while
     safe-point owner revalidates the exact current handle/basis under lock
  -> transition and new-epoch candidate exact-bind that frozen admission
  -> safe-point owner revalidates handle/basis and synchronously makes
     continuity install consume candidate/authority
  -> handle + tool borrow transfer into EpochAgentLoopProviderOpenPermit
```

`HandleFreeProviderWireObservation`必须保持既有定义：它不含safe-point handle、continuity reservation、tool borrow、permit或install authority；其现有内部measurement candidate也不得进入transition/candidate。它只证明pure measurement被一次性消费，**不能**证明canonical read/capability来自owner。`ModelSwitchDirectPrecompileDecision`必须一次性消费observation并抽取`FrozenDirectSwitchAdmission`；随后由3.4节owner-issued observations形成exact direct basis。该admission、basis与后续transition authority一直线性传递到final continuity install。Admission/transition/candidate必须保留同一canonical cut、execution-independent destination binding/target、native projection/replay selection、materialization、quote与admission，不得在precheck后退化成普通append candidate或重新测量后沿用旧authority。

Direct transition authority只能在B exact final-wire observation已成功admitted、owner-issued read/capability carriers已被basis消费、safe-point handle仍current时由private factory签发。签发和final install都必须在safe-point lock下重新`_require_current()`并exact-check同一basis/borrow；两次之间发生handle rotation/close时authority作废、provider open=0。Precheck尚未判定时不存在provisional authority；一旦结果进入Tier 2/3，任何direct observation/decision/basis/authority必须销毁或从未创建，compaction dry/adopted arm不能复用它。单次调用中`ExplicitModelSwitchColdStart`与compaction arms必须互斥。

### 6.4 Tier 2/3：model-switch compaction

若 B 不能 direct fit，继续使用现有 model-switch handover 三档语义：

- Tier 2：A 生成唯一 handover summary candidate，B exact-admit successor；
- Tier 3：B 从 canonical-derived projection source 生成 summary，B exact-admit successor。

Summary physical target与successor destination必须是两个具名字段，禁止用含糊的单一“destination target”代替。封闭矩阵为：

| 路径 | `summary_call_target` | `successor_destination` |
|---|---|---|
| ordinary same-target compaction | A | A |
| model-switch Tier 2 handover | A | B |
| model-switch Tier 3 destination projection | B | B |
| pending Empty/source-less compaction | B（本次cold target） | B |

表中A/B都表示exact `FrozenProviderPhysicalCallTarget`/`FrozenEpochModelCallTarget`内的完整bundle/binding/budget，不是connection字符串。Summary initial与unique repair复用同一`summary_call_target`值但各有独立phase permit；dry projection/adoption/successor只读取`successor_destination`。任何A/B交叉、把Tier 2 summary误开到B、或让summary permit隐式选择transport都fail closed。

一旦 snapshot/binding adoption FULL，最终安装通过 `AdoptedCompactionSuccessor(destination=B)` 完成。它不再同时依赖 `MODEL_TARGET_CHANGED` 与 `CONTEXT_BINDING_REWRITE`。

### 6.5 Reasoning 与 credential

- reasoning effort/toggle/budget 仍是 per-call output control，不改变 context-bearing prefix；
- 同 target 的 reasoning-only 变化继续普通 append；
- secret value refresh、OAuth token refresh 或 physical client reconnect 不改变 root；
- 下一 NEW_TURN 的 durable binding 显式选择不同 `ModelConnectionId` 时构成 model switch，即使 endpoint/model 相同；
- native replay 是否复用仍由 replay-target compatibility 决定，不由 connection ID 单独决定。

### 6.6 Connection 配置编辑

本 hard cut 冻结一个单一路径，不保留产品选项：

- `ModelConnectionId` 对所有 context-bearing target 配置不可变；
- provider family、model、endpoint identity、wire API、transport kind、adapter/lowering profile、modality/tool capability等任一context-bearing编辑，以及authentication mode/credential lookup contract的改变，都创建新的`ModelConnectionId`；
- 用户选择新 ID 后，由下一条 NEW_TURN 的 durable `model_call_binding` 冻结并触发 explicit model-switch cold path；
- 在authentication mode/locator不变时，credential secret/token的**值**刷新与连接池重建可以留在同一ID，因为它们只属于physical execution，不改变semantic target；
- 删除同 ID context-bearing 原地修改、旧 ID 自动跟随新 route、target fingerprint drift 自动 reset 等所有生产路径。

设置层必须在写入时拒绝对既有 ID 的 context-bearing overwrite；不得等到 dispatch 时靠 fingerprint mismatch 猜测用户意图，也不得新增 compatibility registry、旧值 alias 或自动复制 session binding。

---

## 7. Adopted compaction successor

### 7.1 Adoption 才是授权边界

Compaction planning、summary terminal、dry successor measurement、Hook probe 都不授权 root rebuild。

只有以下 canonical transaction 成功后才允许构造 successor：

- snapshot 内容已提交；
- context binding revision 已提交；
- current turn/session binding 已切到该 revision；
- adoption confirmation 与 exact attempt/cut join。

### 7.2 Pre-adoption dry projection 没有 install capability

Compaction在adoption前需要真实summary provider call以及hypothetical successor测量。二者是不同authority：summary执行复用现有compaction summary execution owner，但hard-cut掉其中直接持有`ResolvedModelCall.target.transport`的路径，改为transport-free、one-shot`CompactionSummaryProviderOpenPermit`。Permit source fields明确为`summary_call_target: FrozenProviderPhysicalCallTarget`与`successor_destination: FrozenEpochModelCallTarget`，并必须满足6.4节封闭A/B矩阵。Permit只能由summary owner在exact prepared semantic input/final wire已通过现有validation，且source仍current后私有签发：installed source exact-bind predecessor cohort；pending-Empty source exact-bind3.4节sealed basis的owner-issued read/handle。它还绑定attempt、summary phase（`INITIAL | UNIQUE_REPAIR`）、semantic input、native tool projection、wire plan/quote和“无tool execution authority”contract。Initial与唯一repair各需独立permit并各消费一次；一个phase的permit不能重放、不能换取continuity install/nonce，也不能用于正常agent-loop open。

Summary call可以且必须发生在canonical adoption之前。`CompactionSummaryProviderOpenPermit`只授权该次summary physical open，不授权root rebuild；失败只按现有compaction attempt语义收束。它不使用agent-loop的tool borrow，也不允许执行tool或产生side effect；为保持既有final-wire/prefix语义，provider仍可看见exact frozen tool projection并返回tool-call blocks，但这些blocks只作为触发唯一denial repair的data，永远不送入tool runtime。Pending-Empty source在每次permit签发前由safe-point owner重新验证exact sealed basis/handle仍current；installed source由compaction owner重新验证exact predecessor仍installed。Summary返回后才构造candidate snapshot并进入下面的pure dry projection。

Dry projection保留一个独立的只读输入类型：

```python
@dataclass(frozen=True, slots=True)
class InstalledCompactionDrySource:
    predecessor: InstalledEpochRuntimeCohort


@dataclass(frozen=True, slots=True)
class PendingEmptyCompactionDrySource:
    canonical_read: FrozenCanonicalProviderDispatchRead
    capability_cut: FrozenCapabilityDispatchCut
    empty_scope_provenance: FrozenEmptyScopeProvenance
    _sealed_basis_identity: object


CompactionDrySource = (
    InstalledCompactionDrySource | PendingEmptyCompactionDrySource
)


@dataclass(frozen=True, slots=True)
class CompactionDryProjectionBasis:
    source: CompactionDrySource
    proposed_snapshot: FrozenContextSnapshotCandidate
    destination: FrozenEpochModelCallTarget


@dataclass(frozen=True, slots=True)
class CompactionDryProjectionResult:
    semantic_projection: FrozenModelInputSemanticProjection
    wire_materialization: FrozenProviderWireMaterialization
    quote: FrozenProviderWireInputQuote
    admission: CompactionDryAdmitted | CompactionDryRejected


@dataclass(frozen=True, slots=True)
class CompactionDryAdmitted:
    pass


@dataclass(frozen=True, slots=True)
class CompactionDryRejected:
    reason: ExistingTypedCompactionAdmissionReason
```

本路径直接复用3.1节的`FrozenEpochModelCallTarget`，不再创建第二个dry-only target DTO。`CompactionDrySource`也是closed union，不使用nullable predecessor：已安装epoch只能走`InstalledCompactionDrySource`；fresh Host/fork/import/首次cold over-budget等尚无installed predecessor的路径只能走`PendingEmptyCompactionDrySource`。后者只能由compaction coordinator的private factory从exact live sealed basis抽取；`FrozenEmptyScopeProvenance`与`_sealed_basis_identity`只用于coordinator后续把dry result与它仍持有的basis exact-join，不携带reservation/authority/handle/tool borrow或安装能力，公开值不能反向换取cold authority。任意caller拼装的同值dry source均不会被owner API接受。

一旦冻结，dry materializer只消费source与destination的pure values，不得再 resolve/borrow transport。`FrozenModelInputBudget` 必须是仓库现有完整 typed budget value 或其 hard-cut 后的唯一等价物，包含该 call 的 input/output/resource bounds；不为配合示意新增重复字段或 fingerprint。

上述 admission union 是 closed pure value，只表达 admitted/rejected 与既有 typed reason，不携带 provider handle、continuity inputs或可消费 permit。Basis只允许调用pure semantic projection、`destination.target_bundle` 的pure materializer/estimator/replay contract与exact quote/admission，并且只能返回上述result。它不能：

- 实现 `ProviderInputEpochTransition`；
- 生成 epoch nonce、continuity candidate、permit 或 provider-open authority；
- 注册、安装或替换 continuity slot；
- 被 cast/包装成 adopted seed；
- 在 adoption transaction 之后继续作为 authority 使用。
- 触达 `ModelRuntime.borrow_transport()` 或任何 live provider-open 入口。

“dry不得borrow transport”只约束本dry basis/result；它不禁止前序summary owner消费`CompactionSummaryProviderOpenPermit`。Source-less固定顺序是：sealed Empty basis → initial summary及至多一次unique repair → candidate snapshot → dry admitted → safe-point再次验证exact basis/handle → `begin_empty_adoption()` → canonical adoption。Summary/repair/dry任一步在进入pending前失败，必须关闭其permit/physical borrow并通过exact basis abort恢复原Empty state；进入pending后由7.6节settlement规则接管。

这不是旧/新兼容双轨，而是 compaction 协议中原本就不同的 plan/measure phase。现有返回 `ColdEpochInputAssemblyResult`/`ColdEpochContinuityCandidateInputs` 的 `bind_prepared_wire()` 不得再被 dry path 调用。必须新增明确的 `project_and_quote_compaction_dry(...) -> CompactionDryProjectionResult`；共享只能下沉到 internal pure lowering/materialization/measurement functions。静态类型与runtime validation都必须使dry result根本无法传给continuity register/install。

本节明确覆盖 Round 5B 中要求 pre-adoption dry path 复用 install-capable cold assembly carrier 的旧条款；post-adoption normal successor仍使用new-epoch compile/install API，不保留旧 carrier fallback。

### 7.3 Adopted seed 只能由 FULL confirmation 签发

当前宽泛的 `CompactionContinuationSeed` hard-cut 为 adoption 后专用类型，例如：

```python
@dataclass(frozen=True, slots=True)
class AdoptedCompactionContinuationSeed:
    preparation_basis: SealedProviderInputPreparationBasis
    dispatch_read: FrozenCanonicalProviderDispatchRead
    adopted_snapshot: FrozenContextSnapshotFact
    adopted_binding: FrozenContextBindingFact
    predecessor_epoch_nonce: str
    predecessor_epoch_revision: int
    _authority: object
```

实际字段应复用当前完整typed values；不得为了匹配示意增加冗余DTO fingerprint。predecessor nonce/revision非空，并exact-join`AdoptedCompactionSuccessor.predecessor`；adopted attempt/cut/turn binding还必须exact-join transition的destination connection/target。FULL之后，canonical reader与capability owner必须针对仍current的safe-point handle重新签发owner-issued observations，safe-point owner消费它们形成一个绑定adopted read/FULL confirmation/predecessor/destination的**新**`SealedProviderInputPreparationBasis`；pre-adoption summary/dry basis不能沿用为install basis。`_authority`只允许由compaction coordinator的private factory在adoption confirmation FULL、new basis current且所有exact join完成后签发。Seed线性拥有该basis，签发和final install都由safe-point owner在lock内重新验证handle/basis；最终把handle与tool borrow转入epoch agent-loop permit。Dry basis/result、裸read/capability DTO或FULL confirmation单独都不能换取该authority。

### 7.4 Post-adoption install failure

若 adoption FULL 后、successor install/open 前失败或断电：

- canonical snapshot/binding 保持新真值；
- 旧 provider request 不重播；
- 未 open 的 successor 不伪装成已调用；
- 新 Host 没有 predecessor，按 Empty-scope cold bootstrap 读取 adopted base；
- 同 Host 且当前 branch 仍获准继续时，必须保留 installed cohort，并通过 canonical adopted snapshot/binding exact read 重新签发 adopted seed、恢复 `AdoptedCompactionSuccessor`；
- 同 Host 但 turn terminal、Hook阻止 continuation或 successor 已最终放弃时，compaction coordinator必须先安装5.4节的`NoContinuationAdmissionFence`，以`scope + exact predecessor`阻止全部同/跨attempt的新retry/preparation；随后safe-point owner在穷尽检查全部successor capability并按`safe-point -> continuity`锁序确认无physical work后，才私签并立即消费对应exact `NoContinuationClosure`。统一retire先进入non-authorizing`InstalledNoContinuationSettling`，logical revoke并physical release全部旧能力，release FULL后才与FULL/predecessor exact-join并发布adopted-base lease；未来turn再从adopted base cold bootstrap；
- 单纯 `context_base_semantic_identity` mismatch 不能授权重建。

这里复用现有 canonical snapshot/binding rows 与 repository reader；不新增 receipt、checkpoint、repair graph 或 durable epoch row。

普通 transient failure cleanup 不得丢弃 predecessor。只有 exact FULL confirmation 加 5.4 节的 private one-shot `NoContinuationClosure` 才能把它退休为 adopted-base lease；caller 口头断言“当前 branch不再继续”不构成 authority。Host teardown则直接使process-local predecessor消失，新 Host随后通过 takeover-authorized Empty cold路径恢复。

### 7.5 Active 与 idle compaction

- Active compaction且继续当前请求：adoption 后在同一 Host 以 non-null exact predecessor立即安装successor；
- 任何FULL后no-continuation：原idle compaction、turn不再RUNNING、`PostCompact`/`SessionStartCompact`阻止continuation或最终放弃，都先由compaction coordinator安装`NoContinuationAdmissionFence`，再由safe-point owner在锁内穷尽验证并私签对应`NoContinuationClosure` arm。统一retire把slot从`Installed`变为无lease的`InstalledNoContinuationSettling`，先撤销并释放全部successor capability；只有release FULL才发布携带adopted-base lease的`AuthorizedEmpty`。未来用户消息才可在prepare时生成新的one-shot authority，从canonical adopted base走`EmptyScopeColdStart`；
- 两者共享同一 canonical snapshot truth，不增加两套 reducer 或 replay protocol。

### 7.6 无 installed predecessor 的 FULL adoption：Empty reseal

Fresh Host、fork/import 目标session、首次ROOT cold或其他authorized Empty在首次install前就可能因destination over-budget/模态要求触发session-start compaction。这是必须支持的真实产品路径，不能伪造nullable adopted predecessor。

该路径使用一个private、process-local、**不产生epoch nonce** 的continuity操作：

```text
PreparingEmpty | BoundEmptyPreparation | Prepared(EmptyScopeColdStart)
  -> keep exact sealed Empty basis current
  -> initial summary; if required, one unique repair
       (each consumes its own CompactionSummaryProviderOpenPermit)
  -> candidate snapshot + CompactionDryProjectionResult(ADMITTED)
  -> safe-point revalidates exact handle/basis under lock
  -> continuity.begin_empty_adoption(exact empty handle/state,
                                     attempt/cut/destination/dry admission)
  -> EmptyAdoptionPending(previous_empty_state, attempt/cut/destination)
  -> canonical adoption
       network ACK unknown -> stay pending; shielded exact-confirm owner continues
       NONE                -> exact abort/restore previous empty state or lease
       FULL                -> EmptyAdoptionFullSettling (no lease)
                              -> logical revoke old capabilities
                              -> physical release FULL
                              -> AuthorizedEmpty(AdoptedBaseLeaseSource)
       CONFLICT            -> EmptyAdoptionConflictSettling (no lease)
                              -> revoke/release old capabilities
                              -> close current Host
  -> if immediate continuation: acquire a new safe-point handle and redo
       begin reservation -> read/freeze -> sealed basis -> EmptyScopeColdStart
     otherwise: leave the adopted-base lease for a future turn
```

`begin_empty_adoption()`只能在summary/unique repair完成且dry result为ADMITTED后，由safe-point owner在其lock内重新确认exact handle/sealed basis current后调用；continuity然后在自己的lock内exact-consume当前Empty reservation/authority/candidate中实际存在的那一个状态，连同该basis绑定scope/Host/turn、old canonical base/cut、destination`FrozenEpochModelCallTarget`、summary result、dry admission与compaction attempt。Adoption I/O由coordinator在lock外执行；一旦进入pending，caller cancellation只取消等待，shielded owner task必须持续exact-confirm，直到repository closed outcome`NONE | FULL | CONFLICT`或Host close。Network ACK unknown不是`CompactionConfirmationKind`、不是terminal outcome，绝不能被记录或处理成`confirmed-unknown`，普通`finally`不得恢复旧lease。

Repository outcome返回后，coordinator**不得直接调用continuity settlement**。`NONE | FULL | CONFLICT`全部只能传给`ProviderSafePointCoordinator.settle_empty_adoption(handle, sealed_basis, pending, confirmation)`：它先取得safe-point lock并`_require_current(handle/basis)`，再按唯一`safe-point -> continuity`锁序调用对应continuity CAS，最后仍在safe-point lock内决定handle/basis/tool borrow的保留或关闭。Continuity owner永远不回调safe-point，也不得声称自己能失效handle。

- `NONE`表示canonical transaction确定无effect。仅当old Host仍open且exact handle/basis current时，safe-point owner调用continuity exact-restore previous Empty state，并保留恢复后仍合法的handle/basis；本attempt的summary/dry/permits全部失效。若current/restore条件不成立，safe-point owner关闭basis/borrow并请求Host fail-closed close，不猜测恢复；
- `FULL`时，safe-point owner先在持锁状态调用continuity把pending CAS为non-authorizing`EmptyAdoptionFullSettling`，**此时绝不发布lease**。随后它执行不可失败的process-local logical revocation：basis/authority/candidate/seed标为consumed，tool-surface owner先使exact borrow的所有validation/execution入口永久拒绝，safe-point owner再从`_active_handle`移除并标记handle closed；这些内存状态变化产生一个只含cleanup责任、无任何authority的`RevokedProviderPreparationResources`。接着在仍受safe-point lock/fence保护时执行process-local physical release。只有release FULL后，continuity才允许`FullSettling -> AuthorizedEmpty(AdoptedBaseLeaseSource)` CAS；
- `CONFLICT`表示canonical side已有不同winner、partial/mismatched rows或predecessor不匹配。Safe-point owner同样先把pending CAS为non-authorizing`EmptyAdoptionConflictSettling`，逻辑撤销并release全部old capabilities，但永远不发布lease，随后请求关闭current Host；绝不能恢复old lease、也不能用本candidate reseal。Fresh Host完成takeover后从repository实际canonical winner/binding重新cold bootstrap。

Logical revoke必须是owner内部、无I/O、不可失败且先于physical release；`ProcessLocalToolSurfaceBorrow.close()`若当前实现把logical invalidation与可能失败的release混在一起，hard cut必须拆成“owner revoke（先标closed/移出current）+ cleanup release”。Physical release或最终continuity CAS任一步失败，slot保持/转为`EmptyAdoptionResourceReleaseQuarantine`，没有lease、handle、可执行borrow或重试authority，并触发Host close/quarantine；不得从settling状态恢复old Empty。`begin_empty_preparation()`和所有prepare/register/install API必须拒绝三个settling/quarantine状态。因此不存在“new lease先发布，old capability后撤销”的窗口。

若Host close/handle rotation先取得safe-point lock，settlement随后发现exact handle不再current，则不得执行NONE restore或发布FULL lease；只确认本basis/borrow已经或即将随Host关闭并结束本地owner。若settlement先取得锁，则Host close只能在上述NONE或FULL/CONFLICT settling/revoke/release/CAS编排完成后继续。暂停在FULL confirmation后、logical revoke前或physical release中时，continuity slot均不是`AuthorizedEmpty`，任何并发begin都typed拒绝。

FULL reseal拆成continuity owner的两个private同步子操作：`begin_empty_full_settlement(...)`只把exact pending变为`EmptyAdoptionFullSettling`；`publish_empty_after_full_release(settling, revoked_resources, release_full)`只在safe-point owner已证明logical revoke与physical release FULL后签发唯一`AdoptedBaseLeaseSource`。二者都只能由上述safe-point settlement持锁调用，并exact-joinattempt/cut、scope/turn、destination target/binding、adopted snapshot/context binding与同一settling object。Read DTO/fingerprint、dry basis/result、Hook outcome、caller assertion或compaction coordinator直调都不能签发lease。

Immediate continuation、`SessionStartCompact`/`PostCompact` block与final abandonment都先执行同一FULL reseal：区别只在reseal之后是否立即重新prepare。此路径没有installed predecessor，因此不使用`AdoptedCompactionSuccessor`或5.4节的`NoContinuationClosure`/retire。FULL后reseal前Host崩溃由fresh Host takeover从adopted canonical base恢复；FULL后同same-Host exact reseal失败则fail closed并关闭该Host runtime，不得把旧Empty state当作仍有效。ACK unknown等待中、NONE/CONFLICT/FULL settlement前后任一点Host close都使所有process-local pending/handles失效；新Host只读canonical当前真值，不恢复process-local attempt。不新增durable receipt、repair job或第四个new-epoch arm。

---

## 8. Destination reprojection pipeline

### 8.1 唯一完整流程

任何 cold/model-switch/compaction successor 都必须经过同一条 destination projection pipeline：

```text
1. freeze canonical provider-input cut
2. resolve current context binding
3. read FULL_HISTORY or SNAPSHOT + suffix
4. build FrozenProviderInputItem sequence
5. lower to provider-neutral LLMMessage sequence
6. freeze boundary-legal SYSTEM and tool surface
7. validate bounded replay manifest relation/metadata and select final placements
8. hydrate/validate body only for final selected + destination-compatible replay
9. retain public semantic messages for absent/metadata-incompatible replay
10. destination adapter materializes exact root/tools/input
11. destination estimator traverses that exact materialization
12. enforce token/byte/modality/resource admission
13. continuity owner validates typed transition and CAS-installs
14. EpochAgentLoopProviderOpenPermit may borrow/open provider transport
```

本流程描述新epoch的normal agent-loop call；pre-adoption summary、memory auxiliary与connection probe不是epoch projection/install，分别受9.6节自己的purpose permit约束。

### 8.2 禁止 wire-to-wire 转换

不得实现：

```text
A Chat payload -> converter -> B Responses payload
A Responses output items -> heuristic -> B provider-native reasoning
old adapter JSON -> patch keys -> new adapter JSON
```

只允许：

```text
canonical truth -> provider-neutral semantic input -> B adapter materialization
```

### 8.3 Provider replay

Replay fragment 只在 destination target exact compatible 时替换对应 public assistant placement。若 A 与 B 只更换 connection secret、但 replay target 的 wire/model/endpoint/transport/codec 完全相同，可继续使用 native replay；若 wire API、model、endpoint、transport 或 codec 不兼容，则只使用 public semantic projection。

Replay 不得决定 epoch boundary，也不得因为 replay 不兼容而触发额外 reset。

Replay selection 必须保持 metadata-first、selected-compatible-only hydration，并区分以下结果：

| Replay 状态 | 行为 |
|---|---|
| attachment 不存在 | 使用 canonical public semantic projection |
| bounded manifest cut 中 relation/metadata 本身损坏或矛盾 | 立即 typed corruption，provider open=0 |
| placement 被选中，但 manifest metadata 与 destination 不兼容 | 不 hydrate body，使用 canonical public semantic projection |
| final selected 且 destination-compatible，body/payload/digest/codec/public projection/placement 全部合法 | hydrate native replay |
| final selected 且 destination-compatible，但 body 缺失、损坏、overbound 或与 projection/placement 不符 | typed corruption，provider open=0，不得 fallback |
| unselected 或 metadata-incompatible attachment 的 private body | 本次不 hydrate、不审计；潜在 body corruption 不阻断调用 |

只有本次必须消费的 selected-compatible body 损坏时，“存在但损坏”才不能伪装成“不存在”；不得为了全量审计读取本次不消费的 opaque private bodies，working set继续服从既有 bounded manifest 与 selected hydration 合同。同一 installed epoch 已采用且已验证的 native replay placement 是 strict-prefix 的一部分：后续 append 必须复用 exact installed placement，不能因为 reader/registry/codec observation 变化在 native 与 public 之间切换。只有新 cold/adopted boundary 才重新执行 destination metadata selection。

---

## 9. Adapter 与 lowering ownership

### 9.1 Dependency/adapter owner

每个 provider adapter 负责：

- provider-specific message/tool materialization；
- request body shape；
- native tool schema；
- provider stream parsing；
- terminal validation；
- provider-native replay codec；
- transport lifecycle 与 provider-specific error normalization。

Pulsara 负责：

- canonical truth；
- approved epoch boundary；
- destination target/adapter freeze；
- prefix continuity；
- public semantic lowering；
- replay compatibility selection；
- exact final-wire measurement/admission；
- tool side-effect、permission、cancellation 与 settlement 产品语义。

不得把 provider SDK/parser/transport state machine 复制进 continuity/compiler。

### 9.2 Same-epoch adapter stability

Ordinary append 必须从一个 installed runtime cohort 复用 exact：

- execution-independent frozen target bundle；
- route/wire profile；
- native tool projection set；
- message/native-tool lowering contract；
- assistant replay contract；
- estimator；
- adapter-owned context materializer。

如果这些值在同一 Host/epoch 内无法继续取得，返回 typed internal/adapter unavailable failure，provider open=0。不能通过 `PROVIDER_LOWERING_CHANGED` 重建 root。

### 9.3 Code deployment 与动态 adapter

- 普通代码升级通过 Host restart 生效，旧 process-local epoch 自然消失；
- 新 Host 从 canonical truth cold bootstrap，无需 lowering reset；
- 动态安装第三方 adapter 只进入未来 approved cold/compaction boundary；
- 已安装 epoch 不因为 plugin reload、registry refresh 或 adapter discovery 改变；
- 切换到使用另一 adapter 的显式模型，走 model-switch cold/compaction 路径。

### 9.4 单一 `InstalledEpochRuntimeCohort` owner

Continuity slot 必须原子拥有一个 `InstalledEpochRuntimeCohort`；不得继续把 view 放在 continuity owner、target 放在 `_installed_targets`、adapter/estimator 从 registry 临时重解。其概念字段为：

```text
InstalledEpochRuntimeCohort
  view: FrozenProviderInputEpochView
  target_bundle: FrozenEpochModelTargetBundle

FrozenEpochModelTargetBundle
  connection: FrozenModelConnectionExecutionContract
  target_fact: ResolvedModelTargetFact v7
  reasoning_contract: ReasoningControlContract
  projection_strategy: FrozenProviderProjectionStrategy
  estimator: exact pure estimator/value

FrozenModelConnectionExecutionContract
  connection_id: ModelConnectionId
  authentication_mode: ModelConnectionAuthentication
```

`FrozenModelConnectionExecutionContract`是connection的唯一execution-independent owner。当前closed authentication union是`BEARER_API_KEY | NONE`；credential locator不另存字段：`BEARER_API_KEY`按现有settings contract由`connection_id`定位`LocalModelApiKey`，`NONE`派生为no credential。未来若真实产品增加OAuth等独立locator，必须先把该closed typed contract扩展为唯一source field，不能在borrow处猜测。它不保存secret/token/client，也不重复target/base URL/limits。

`ResolvedModelTargetFact` clean hard-cut到`resolved-model-target:v7`，在现有route/wire/model/canonical endpoint/transport/modalities/limits/budget/estimator完整值之外新增唯一的`tool_call_capability: bool`，并**同时删除`target_fingerprint`与`endpoint_fingerprint`**。不读取或兼容v6，不保留alias/optional default。它由resolution owner从exact model catalog或`UserDeclaredModelTarget.tool_call`冻结；provider context shape、tool exposure与native-tool lowering只读取这一份capability truth。既然完整frozen fact/call target已经携带到consumer，request validation直接使用typed object identity/equality与现有owner authority，不再用可重算的DTO自摘要证明join。

同一hard cut必须完成以下穷尽删除，不允许只删epoch aggregate而留下旁路自摘要：

- 删除`ResolvedModelTargetFact.target_fingerprint`以及helper/export `resolved_model_target_fingerprint`；
- 无条件删除`ResolvedModelTargetFact.endpoint_fingerprint`及resolution阶段对它的populate/check；完整`canonical_endpoint_base_url`已经在同一DTO中，是唯一endpoint truth；
- 删除`LLMContext.target_fingerprint`；
- 删除`ContextCompileBudgetReport.target_fingerprint`及其budget-report identity/serialization输入；
- 删除`FrozenProviderWireInputPlan.resolved_target_semantic_fingerprint`；
- 删除`ProviderWireMeasurement`构造参数与私有字段`_resolved_target_semantic_fingerprint`；
- 删除`ModelInputCompileOperationalProjection.target_fingerprint`及public diagnostic payload字段；
- 删除direct dispatch、provider dispatch、compaction、auxiliary、connection probe、resolution与validation中针对上述字段的所有compare/populate/pass-through；
- 删除`ProviderInputEpochCompatibility.model_target_fingerprint`，且不得以新名称移入cohort、bundle、permit、transition或diagnostic DTO。

这些join全部改用caller已经携带的完整`ResolvedModelTargetFact`、`FrozenEpochModelTargetBundle`或`FrozenProviderPhysicalCallTarget`的typed equality/object identity与private owner factory。Provider replay继续保留独立的`replay_target_fingerprint`，因为它跨durable/restart compatibility边界；其唯一builder从endpoint/model/transport/codec等最小facts局部计算，不读取或回存完整target DTO digest。

模型目标派生hash的生产allowlist封闭为以下两种，不存在“诊断需要所以临时保留target/endpoint digest”的第三种：

1. Durable replay边界：`replay_target_fingerprint`以及`ProviderReplayTargetCompatibilityFact.endpoint_identity_fingerprint`可以保留。`build_provider_replay_target_compatibility(*, target_fact, ...)`必须接收完整`ResolvedModelTargetFact v7`，只在这个唯一真实replay-boundary builder内从其`canonical_endpoint_base_url`局部重算历史`pulsara.model-endpoint:v2`字节；不提供endpoint-string overload或legacy fingerprint参数，该endpoint digest不得回流到`ResolvedModelTargetFact`或其他resolved/bundle/context DTO；
2. 已存在且必须保持observable bytes的canonical/immutable boundary identity builder，例如provider-wire plan durable-replay identity或compaction immutable source-lineage proof：builder可把完整target fact作为输入，并在builder局部重算历史target component以维持同一稳定ID；该component不得作为字段返回、缓存、导出或传给其他owner。每个此类builder必须在architecture allowlist中逐名登记，未登记的target-derived hash调用即失败。

因此`provider_wire_input_plan_identity_fingerprint`等真实boundary builder要改为显式接收/验证完整target fact（或包含它的唯一typed owner），而不是从plan读取被删除字段；`_summary_source_identity_value`等aggregate proof builder也只能消费完整target fact并在自己的最终aggregate内部编码/局部计算。普通diagnostic projection不跨上述边界，故直接报告非敏感typed target标识或省略该维度，禁止重新生成完整target digest。

`FrozenProviderProjectionStrategy` 是一个封闭的execution-independent pure strategy/value，只拥有精确route/wire profile、pure reasoning lowerer implementation、message/context materializer、native-tool materializer与assistant replay codec contract。Reasoning 支持契约只由bundle的`reasoning_contract`持有，strategy只需exact-validate其lowerer可服务该contract；native-tool contract只由strategy持有并作为bundle/call-target的派生property暴露，同时strategy必须exact-validate它对`target_fact.tool_call_capability`的支持关系。Strategy不拥有limits/budget/endpoint/model/connection/authentication/tool capability的第二份truth，也不得捕获registry或live transport。

Bundle 只保留上述source fields。以下均是property/纯派生，**不得再存一份字段**：

- `connection_id`与credential lookup semantics由`connection`派生；
- `target_key` 由 `target_fact.route_id/wire_api/model_id` 构造；
- route/model/wire/endpoint identity、transport binding、limits 与 context budget 从 `target_fact` 派生；
- route-wire/materializer/native-tool/replay codec contract 从 `projection_strategy` 派生；
- `replay_target` 由 `target_fact` 的 wire/endpoint/model/transport facts 与 strategy 的 codec contract 纯计算，不持久第二份 target；
- estimator identity 从 `estimator.fact` 派生并与 `target_fact.token_estimator` exact equality。

`FrozenEpochModelTargetBundle` 只能由 model boundary resolution owner 的 private `freeze_epoch_model_target_bundle(...)` factory 构造。Factory可以消费当前 `ModelConnectionConfig`/`ResolvedModelTarget`，但返回值只提取connection execution contract和execution-independent facts/strategy，不得保留完整config、resolved object或transport。Factory 与 bundle `__post_init__` 必须 fail closed 地 exact-validate：

- boundary `ModelCallBinding.connection_id == connection.connection_id`，且factory input config ID、authentication mode与resolution source/connection contract相同；
- factory input config/contract/profile 的 target key、canonical endpoint、wire/model/route 与 `target_fact` 完整相同；
- resolution source 的 limits/context budget、input modalities与tool-call capability与`target_fact`完整相同，且fact内部budget invariant通过；
- `estimator.fact == target_fact.token_estimator`；
- strategy route-wire profile 的 route/wire/model-identity policy 与 fact/resolution source 相同；
- strategy replay codec/contract 纯派生的 replay target 与 fact 的 wire/endpoint/model/transport binding 完全相容；
- strategy transport contract identity 与 `target_fact.transport_binding_id/transport_contract_version` 相同，native-tool materializer与`tool_call_capability`一致；
- reasoning contract 与 exact resolution source/strategy 的 supported lowerer 相同。

上述 source-dependent equality 在private factory内完成；返回后 `__post_init__` 对 bundle 内仍并存的connection contract/fact/strategy/estimator 重做可在返回值内验证的 exact checks。任一 mismatch 都不得产生 bundle。不允许为了“让 `__post_init__` 有对照值”再加 `ModelConnectionConfig`、`target_contract`、`model_profile`、`context_budget`、`replay_target`、native-tool contract或transport-binding副本。

`FrozenEpochModelTargetBundle` 是 execution-independent hard-cut type；它**不得**包含 `ResolvedModelTarget`、`ResolvedModelCall`、`PreparedKernelModelTarget`、`NormalizedLLMTransport`、provider client、transport registry、credential/secret handle或任何能直接 open live stream 的对象。

`view` 是 SYSTEM、tools、messages、wire/tool plans、replay placements 与 canonical frontier 的唯一 owner；`target_bundle` 是 connection execution contract、target fact（含tool-call capability）、reasoning/projection strategy 与 estimator 的唯一 owner，其他 target/profile/budget/replay/native-tool/transport identity 仅为派生值。Cohort 顶层不得重复这些字段。Register/install 在同一 lock/CAS 中替换整个 cohort；append transition、provider dispatch、replay reservation 与 final open 都携带/验证同一 object identity。删除 `_installed_targets` 及其他与 view 平行、可能独立更新的 epoch state。

Cohort只拥有immutable semantic/configuration values和pure adapter strategy；不拥有provider transport、HTTP client、MCP session、tool subprocess、credential、OAuth token、lease或其他physical liveness。`ModelRuntime.borrow_transport(purpose_permit)`是唯一生产短借API：它**只消费permit内部唯一的`FrozenProviderPhysicalCallTarget`**，不接受第二个target/bundle/binding参数，因而caller不能让permit与实际borrow目标分叉。Runtime以该target的`connection.connection_id`向settings/secret owner读取**当前**immutable connection config与credential（probe例外见9.6），并在physical open前exact-check config ID/authentication mode/target key/canonical endpoint、wire/model/route、user-declared limits/modalities/tool-call/reasoning、target fact与transport binding contract能服务frozen bundle，然后构造/借出physical transport。ID不存在、authentication/tool-call/config被非法原地更改或任一exact check失败都typed unavailable，不使用cached config/fallback、不改变epoch、不重新resolve semantic target。Borrow只活到该次operation settlement；“continuity install/model-active后”仅适用于epoch agent-loop permit，不能误用于pre-adoption summary、auxiliary或probe。

### 9.5 `ProviderInputEpochCompatibility` 字段逐项删除

`ProviderInputEpochCompatibility` aggregate 在本 hard cut 中删除，不保留 rename、wrapper、alias 或兼容 DTO。原字段逐项归属固定如下：

| 旧字段 | 最终决定与唯一 owner |
|---|---|
| `compiler_contract_version` | 从 continuity 删除；只可保留在 `ContextCompileBudgetReport` 的诊断/编译报告契约中，代码部署通过 Host restart 生效，不能参与 epoch equality/reset |
| `base_system_semantic_fingerprint` | 从 epoch compatibility 删除；完整 `system_prompt` 只由 `cohort.view` 持有并 exact equality；source observation 自身若已有独立 identity 可保留，但不得复制回来 |
| `tool_surface_fingerprint` | 从 epoch compatibility 删除；完整 tool specs 与 native projection set 只由 `cohort.view` 持有；tool surface 自身的稳定 canonical ID 可在其唯一 builder/diagnostic consumer 保留 |
| `model_connection_id` | 移入 `target_bundle.connection.connection_id`，作为完整 typed ID；authentication mode由同一connection execution contract持有；完整`ModelConnectionConfig`不进bundle，每次borrow按ID从唯一settings owner重读并exact-check；不同 ID 只通过 explicit model-switch arm 生效 |
| `model_target_fingerprint` | 从epoch compatibility及`ResolvedModelTargetFact v7`/`LLMContext`全部删除；完整execution-independent target fact由target bundle持有，request validation用完整typed values/owner identity；durable replay只保留独立`replay_target_fingerprint`并由其唯一builder局部计算，不能恢复DTO自摘要 |
| `estimator_fingerprint` | 从 epoch compatibility 删除；exact pure estimator/value 由 target bundle 持有；final-wire quote 自身的 estimator identity 保留用于 quote exact join |
| `provider_message_lowering_contract` | 从 epoch compatibility 删除；完整 route-wire/materializer/native-tool profile 仅由 `target_bundle.projection_strategy` 持有并按 exact object/value 复用 |
| `context_base_semantic_identity` | 只由 `cohort.view.canonical_frontier`/canonical context binding 持有；不得在 target bundle 另存第二份 compatibility truth |
| `provider_assistant_replay_contract_fingerprint` | 从 epoch compatibility 删除；replay codec contract 由 projection strategy 持有，replay target 由 bundle 纯派生而不作为副本存储；durable attachment只保留跨重启compatibility/integrity所需digest |

`semantic_prefix_fingerprint`、compiled wire/replay digests 等仍按 fingerprint subtraction 规格保留在真实 prefix、immutable payload 与跨重启 replay 校验边界。它们只能验证已有值，不能签发 transition authority。

### 9.6 Physical provider open 的封闭 purpose authority

生产代码中每一次physical`open_stream()`都必须经`ModelRuntime.borrow_transport(purpose_permit)`消费下列closed process-local one-shot union之一。每个permit都必须通过只读`.call_target`property暴露且只暴露一个`FrozenProviderPhysicalCallTarget`作为实际borrow target；该property委托给permit已有的唯一source field，不重复存对象。其他target只能是供product exact-join的不同语义字段，不能被runtime选择：

```text
EpochAgentLoopProviderOpenPermit
CompactionSummaryProviderOpenPermit
AuxiliaryModelProviderOpenPermit
ConnectionProbeProviderOpenPermit
```

`ModelCallPurpose`在本hard cut后的physical-open closed set只保留`AGENT_MODEL_LOOP | CONTEXT_COMPACTION_SUMMARY | MEMORY_GOVERNANCE | CONNECTION_PROBE`。删除未被生产owner消费的`CONTEXT_WINDOW_COMPACTION_SUMMARY`与`COMPACTION_MEMORY_EXTRACTION`枚举值及其exports/tests，不保留alias、deprecated member、parse fallback或“暂时纯路径”分支；pure compaction projection不需要model-call purpose。

- `EpochAgentLoopProviderOpenPermit`只存`epoch_target`，其`.call_target`返回`epoch_target.physical_call_target`；它只由safe-point/continuity final install或same-epoch append的model-active路径签发，线性拥有3.4节转移来的current handle与`ProcessLocalToolSurfaceBorrow`，exact-bindcohort/candidate/call/turn/cut。只有这一arm要求continuity installed/model-active；现有`ProviderDispatchExecutionAuthority`可作为其实现，不复制第二套tool authority。
- `CompactionSummaryProviderOpenPermit`只存`summary_call_target`作为physical source，其`.call_target`返回同一object；另存具名`successor_destination`仅供6.4矩阵、dry/adoption exact-join，runtime绝不能borrow它。Permit按7.2节由既有summary execution owner为`INITIAL | UNIQUE_REPAIR`分别签发，exact-bindattempt/source/semantic/native-tool projection/final-wire且无tool execution authority；它可以在adoption前使用，provider tool-call只触发既有唯一denial repair，不能打开agent loop或tool runtime。
- `AuxiliaryModelProviderOpenPermit.call_target`的purpose必须是`MEMORY_GOVERNANCE`。Permit由`DirectKernelAuxiliaryJsonModel`与调用它的产品owner共同签发；memory governance repository owner的hard-cut confirmation API返回private one-shot`ConfirmedMemoryGovernanceTerminalFence` carrier（不是caller可伪造的bool），governor必须消费该carrier，才能把exact prepared auxiliary call线性换成permit。Permit绑定fence/candidate/origin binding/semantic packet/final wire/finite deadlines，不创建epoch、continuity authority或tool access。`DirectKernelAuxiliaryJsonModel`的allowed-purpose set同步删除`CONTEXT_COMPACTION_SUMMARY`，不留旧入口；若未来另一产品复用auxiliary port，必须显式扩充closed purpose，而不能接受裸`PreparedAuxiliaryJsonModelCall`直接open。Compaction summary走专用permit，不与该arm重叠。
- `ConnectionProbeProviderOpenPermit.call_target`使用本hard cut新增的`ModelCallPurpose.CONNECTION_PROBE`，只由connection-probe owner为一次未发布draft签发。Permit线性持有ephemeral draft`ModelConnectionConfig`、与call target一致的typed authentication mode及临时credential owner，允许runtime直接读取该temporary settings owner而不是按ID查询saved settings。它绑定exact probe input、finite timeout、retry disabled与tool-free tiny request，settlement后清空credential，不进入cohort/canonical/continuity，也不能换取普通provider permit。旧的probe-as-`AGENT_MODEL_LOOP`标签同步删除，不保留alias。

Permit本身不含live transport；`borrow_transport`消费permit后才创建/借出physical transport，并在operation close/physical completion时释放。四个arm不得cast、互换或fallback，runtime对union exhaustive match并验证call-target purpose。当前仓库四个真实open-site`direct_model.py`、`compaction/model_call.py`、`auxiliary_model.py`、`connection_probe.py`必须各落到且仅落到对应arm；architecture oracle禁止其他直接`target.transport.open_stream()`。Memory governance复用auxiliary arm及其durable fence，不新增第五种permit或另一套transport owner。

---

## 10. PostgreSQL clean-v0 schema hard cut

### 10.1 Canonical row 去 provider 化

为落实“canonical semantic row 与 provider/wire API 无关”，clean-v0 baseline 从 `transcript_entries` 删除：

```text
provider_wire_api
provider_replay_disposition
provider_replay_fragment_id
```

同时删除：

- 依赖这三个字段的 unique constraint；
- 旧的 assistant/non-assistant provider-column CHECK；
- transcript entry 指回 replay fragment 的复合 foreign key；
- repository DTO、reader、terminal projection 与测试中的相应字段。

保留：

- `context_binding_revision_id`；
- `provider_input_through_sequence`；

因为它们描述 assistant occurrence 消费的 canonical cut，不描述 provider 品牌或 wire shape。

删除旧 CHECK 时必须保留并重写 canonical cut invariant，不能连同 provider columns 一起丢掉。clean-v0 至少包含等价于以下约束：

```sql
UNIQUE (session_id, id, entry_kind),
CHECK (
    (
        entry_kind IN ('ASSISTANT_MESSAGE', 'ASSISTANT_TOOL_REQUEST')
        AND provider_input_through_sequence IS NOT NULL
        AND provider_input_through_sequence >= 0
        AND provider_input_through_sequence < entry_sequence
        AND (
            (entry_owner_kind = 'EXECUTED_TURN'
                AND context_binding_revision_id IS NOT NULL)
            OR
            (entry_owner_kind = 'IMPORTED_HISTORY'
                AND context_binding_revision_id IS NULL)
        )
    )
    OR
    (
        entry_kind NOT IN ('ASSISTANT_MESSAGE', 'ASSISTANT_TOOL_REQUEST')
        AND context_binding_revision_id IS NULL
        AND provider_input_through_sequence IS NULL
    )
)
```

列名 `provider_input_through_sequence` 虽带历史命名，但语义是“该 assistant occurrence 消费的 canonical input cut”，不是 provider provenance；本轮保留它，不为名称洁癖做第二次 schema 改名。

### 10.2 Replay attachment table

`provider_assistant_replay_fragments` 继续作为独立 durable attachment，至少包含：

- `assistant_entry_id`；
- `assistant_entry_kind`，closed 为 `ASSISTANT_MESSAGE | ASSISTANT_TOOL_REQUEST`；
- `wire_api`；
- `codec_kind`；
- replay contract；
- replay target compatibility；
- public projection identity；
- payload bytes/digest/size/item count。

约束：

- `UNIQUE(session_id, assistant_entry_id)`；
- `transcript_entries` 提供 `UNIQUE(session_id, id, entry_kind)`；
- replay table 以 `(session_id, assistant_entry_id, assistant_entry_kind)` 单向 composite FK 引用 `(session_id, id, entry_kind)`，从数据库层证明目标确为 assistant entry；
- codec/wire closed union；
- payload bounds/integrity；
- fragment 与 semantic blocks 同 transaction 插入。

删除 transcript -> replay 的反向 FK 与 pointer，避免循环引用。Replay attachment 只有上述单向 relation；删除/复制/fork 语义由该 relation 与现有 transaction owner 明确实现，不新增 disposition shadow column。

不存在 replay row 即 public-semantic-only。无需在 canonical entry 再保存一份 disposition。

Assistant commit 的 ACK-unknown confirmation 必须 exact-read 并匹配：entry identity/owner/kind、canonical cut、parent content、完整有序 blocks、accepted occurrence，以及 replay 的**存在性和值**。候选无 replay 时数据库也必须无 replay row；候选有 replay 时必须恰有一行且所有 contract/target/public-projection/payload/digest/size/item-count/fragment identity 完全相等。NONE、部分 row 或不同 replay 都不能被确认成原 winner，也不能靠 cold reset 绕过。

如果产品未来需要独立展示“这条回答由哪个模型生成”，应单独定义真正的 model provenance product contract；不得继续借 replay 字段充当偶然 provenance。

### 10.3 无迁移兼容

本轮直接修改：

```text
src/pulsara_agent/storage/migrations/sql/0000_conversation_kernel_baseline.sql
```

并同步修改 repository、reader、fork/import、terminal/frontend projection、fixtures 与 tests。不得增加：

- `ALTER TABLE ... IF EXISTS` 兼容旧库；
- old/new nullable dual columns；
- backfill job；
- legacy replay pointer fallback；
- schema-version negotiation；
- migration-time dual read/write。

实施和 dogfood 前解析并核验目标 PostgreSQL，直接重置 clean-v0。不得对未核验或远程未知数据库执行破坏性操作。

### 10.4 Fork/import 只复制 semantic truth

Fork/import 只按本节 canonical/replay relation 复制或重绑 durable semantic truth。新 fork session 是新 session lifecycle：它只能在creation transaction成功后取得自己的 `NewSessionRootLeaseSource`，或在未立即打开而后续resume时由fresh Host takeover取得`FreshHostRootLeaseSource`。它不得继承source session的`InstalledEpochRuntimeCohort`、epoch nonce/revision、bootstrap lease/reservation/authority、prepared candidate、provider-open permit、no-continuation closure或runtime reopen operation。

Imported history 同样不提供epoch authority或provider placeholder。不论fork/import复制了多少canonical rows，目标session的第一次provider open都必须独立消费目标session的Empty lease/reservation/authority并从其current canonical read重投影。

---

## 11. Compiler 与 continuity API hard cut

### 11.1 删除 reset reason

删除：

- `ProviderInputEpochResetReason`；
- `ProviderInputEpochCompatibility`；
- `_compatibility_reset_reason()`；
- `reset_reason` 在 compile result、semantic projection、prepared candidate 中的所有字段；
- `reset_reason is not None` 创建 epoch nonce 的逻辑；
- continuity owner 的 generic incompatible-successor branch；
- `EXPLICIT_TEST_RESET`；
- 对应 export、fixture 与断言。

### 11.2 Compiler 分工

必须形成三个角色互斥的明确入口：

```text
compile_installed_append(..., predecessor=InstalledEpochRuntimeCohort,
                         transition=InstalledEpochAppend)
    -> only suffix allocation

compile_new_epoch(..., transition=EmptyScopeColdStart
                                  | ExplicitModelSwitchColdStart
                                  | AdoptedCompactionSuccessor)
    -> full canonical reprojection

project_and_quote_compaction_dry(..., basis=CompactionDryProjectionBasis)
    -> CompactionDryProjectionResult, never installable
```

可以复用内部 pure lowering/budget functions，但公开入口不能再根据 compatibility mismatch 自行选择 append/rebuild，也不能让dry API返回任何new-epoch carrier。

### 11.3 Epoch nonce

Nonce 生成规则固定为：

```text
InstalledEpochAppend
    -> exact predecessor nonce

EmptyScopeColdStart
ExplicitModelSwitchColdStart
AdoptedCompactionSuccessor
    -> fresh nonce
```

任何 fingerprint、contract version 或 field mismatch 都不得直接进入 nonce 分支。

### 11.4 Continuity register/install

Continuity owner 对 transition exhaustive match：

#### InstalledEpochAppend

- predecessor exact installed cohort object；
- SYSTEM/tools/target/materializer/estimator/replay policy 全部取自该 cohort；
- same nonce；
- canonical, semantic and final-wire prefix exact；
- installed replay reservations/fragments按现有 same-epoch规则保留。

#### EmptyScopeColdStart

- `begin_empty_preparation()`在同步lock内将`AuthorizedEmpty(exact lease)`线性转为`PreparingEmpty(exact reservation)`，不读库不await；
- canonical reader与capability owner分别私签绑定exact handle/read operation及真实tool-surface borrow的one-shot observation；safe-point owner禁止接收裸DTO，只能在其lock内`_require_current(handle)`并消费两种carrier私签`SealedProviderInputPreparationBasis`；pure admission后选择direct cold时，safe-point owner必须再次验证handle/basis current，并在handle仍锁定时调用`bind_current_basis()`将exact reservation转为`BoundEmptyPreparation(exact basis/authority)`；
- candidate只能从该Bound state注册为`Prepared`，并继续携带exact basis；不得跳过Preparing/Bound中间态；final install/model-active也必须由safe-point owner在同一handle仍current时同步编排；
- lease issuer/stable base provenance/scope/Host owner exact join；basis/authority/candidate与本次handle identity/generation/cut、owner-issued canonical read、owner-issued capability cut、真实tool borrow及`FrozenEpochModelCallTarget` exact join；
- slot 缺失或 `current_view() is None` 本身不能通过；
- expected revision 0；
- fresh nonce；
- seed/canonical cut/scope exact join；
- reservation/carrier/basis/authority/candidate任一阶段失败或取消，exact`abort()/close()`只能在Host仍open且它仍占有slot时归还原lease，并关闭未转移的handle/tool borrow；install后lease/basis永久消费，handle/borrow只可线性进入epoch agent-loop permit；Host close使全部outstanding handle/borrow失效；terminal child移除slot且durable terminal status阻止reopen。

#### ExplicitModelSwitchColdStart

- predecessor exact installed cohort object；
- source target/connection 只从 predecessor cohort 读取；
- `FrozenDirectSwitchAdmission` 的execution-independent destination exact join turn binding/target bundle/call budget、由strategy派生的native-tool contract及materialization/quote；原measurement observation不进transition/candidate；
- canonical/capability owners先为exact current safe-point handle签carrier，safe-point owner消费为direct preparation basis；transition由model-switch precheck owner在safe-point lock下重新验证该basis后签发；
- fresh nonce；
- candidate完整使用destination frozen call target/adapter，且不引用`PreparedKernelModelTarget`/Resolved/transport；
- 不要求与 A root prefix join；final install前safe-point owner再次验证同一handle/basis，rotation/close使authority失效，成功后把handle/tool borrow转入epoch agent-loop permit。

#### AdoptedCompactionSuccessor

- canonical adopted binding/snapshot exact join；
- predecessor 必须是 same-Host exact installed cohort，不可为空；
- seed predecessor nonce/revision 与该 cohort exact join；
- execution-independent destination connection/target/adopted turn binding/call budget 与 adoption confirmation exact join；
- FULL后必须取得新的owner-issued read/capability observations与绑定adopted truth/predecessor/destination的current safe-point basis；private adoption authority必须同时来自FULL confirmation与该basis，pre-adoption summary/dry basis/result不可用；
- fresh nonce；
- candidate使用 adopted effective context；
- 不要求与 predecessor root prefix join；签发与final install分别在safe-point lock下重新验证同一handle/basis，成功后线性转移handle/tool borrow。

Restart、FULL-adoption no-continuation 或任何 predecessor 已丢失的情况不进入 adopted arm：只有 lifecycle owner 已签发 `AuthorizedEmpty` lease、prepare又生成exact authority时才能走 Empty cold。Active adoption 后安装失败且branch仍继续时保留predecessor并重试adopted arm；branch不再继续时，compaction coordinator必须先安装`NoContinuationAdmissionFence`阻止所有新retry/preparation，safe-point owner再穷尽验证5.4节列出的successor capability集合并私签closure。统一retire只能`Installed -> InstalledNoContinuationSettling -> logical revoke -> physical release FULL -> AuthorizedEmpty`；普通cleanup/caller assertion、仅有product evidence或未释放的permit/stream不得把它转换成empty。

#### EmptyAdoptionPending（non-nonce state operation）

- 仅允许从exact Preparing/Bound/Prepared Empty state进入，进入前尚无installed predecessor，且initial/unique-repair summary已settled、dry result已ADMITTED、safe-point已重新验证exact basis；
- network ACK unknown不是outcome：保持pending且provider open=0，由shielded owner继续exact-confirm；
- `NONE | FULL | CONFLICT`全部只能由safe-point owner的`settle_empty_adoption()`在持有exact current handle/basis lock时、按`safe-point -> continuity`调用私有CAS；coordinator不得直接settle continuity；
- NONE exact恢复previous state并保留仍合法handle/basis；FULL先进入non-authorizing`EmptyAdoptionFullSettling`，safe-point owner不可失败地logical revoke basis/authority/candidate/borrow/handle并取得`RevokedProviderPreparationResources`，physical release FULL后才发布lease；CONFLICT先进入`EmptyAdoptionConflictSettling`，执行同样revoke/release但永不发布lease并关闭current Host；任一current/join/release/final CAS失败均保持无lease的`EmptyAdoptionResourceReleaseQuarantine`并关闭Host；
- `begin_empty_preparation()`及其他prepare/register/install入口必须拒绝`EmptyAdoptionFullSettling | EmptyAdoptionConflictSettling | EmptyAdoptionResourceReleaseQuarantine`；immediate continuation只能在FULL release与lease publication之后从新safe-point handle重新执行整个Empty prepare，block/abandon才留下该lease；
- 不生成nonce、不进入`AdoptedCompactionSuccessor`、不调用installed-predecessor retire。

任何 arm 验证失败：candidate 失效、provider open=0、installed state不被半更新。

---

## 12. 失败与重试语义

| 场景 | 结果 |
|---|---|
| slot缺失/`current_view() is None`，但无bootstrap lease或本次prepared authority | fail closed，provider open=0；不得自行创建slot或nonce |
| Empty prepare已取得reservation，但canonical read/capability freeze/compile在candidate前失败或取消 | exact close reservation/carrier/basis/authority并归还原lease；关闭未转移的handle/tool borrow；旧read/handle不能重放 |
| Empty/direct/adopted bind或install使用可见字段相同但非owner-issued的read/capability、伪造borrow、rotated/closed handle、cross-attempt target/basis | private carrier验证、safe-point `_require_current()`或exact join失败；销毁本次handles/borrows，provider open=0 |
| terminal child尝试重新打开 | durable lifecycle conflict；不重建slot、不签发lease/authority |
| ordinary append 观察到 SYSTEM/tools/current registry 新值 | 继续使用 installed root；新值延后 |
| ordinary append 无法为frozen target/tool spec短借匹配physical runtime | typed fail，provider open=0，不 reset |
| explicit model switch precheck/compile失败 | A epoch保持；B provider open=0；不自动fallback A回答当前新请求 |
| Tier 1 B不fit | 进入既有Tier 2/3，或在compaction关闭时typed拒绝 |
| compaction summary/unique repair/dry在进入adoption pending前失败或断电 | 关闭exact summary permit/borrow并恢复原Empty state；无canonical变化，下次fresh规划 |
| 无installed predecessor的Empty compaction adoption NONE | 仅由safe-point settlement在handle/basis current时调用continuity恢复exact previous Empty state/lease；否则关闭Host；不生成nonce |
| 无installed predecessor的Empty compaction adoption network ACK unknown | 非终态，保持pending，provider open=0；shielded owner持续exact confirm到NONE/FULL/CONFLICT或Host close，禁止普通cleanup恢复旧lease |
| 无installed predecessor的Empty compaction adoption FULL | safe-point settlement先CAS到无lease的`EmptyAdoptionFullSettling`，再logical revoke全部old capability；physical release FULL后才发布adopted-base lease。暂停于确认后/revoke前/release中时begin均拒绝；immediate continuation只能取得新handle并完整prepare |
| 无installed predecessor的Empty compaction adoption FULL后资源release或final CAS失败 | 保持/转`EmptyAdoptionResourceReleaseQuarantine`，无lease、handle、可执行borrow或retry authority，并关闭/quarantine current Host；不得恢复旧Empty |
| 无installed predecessor的Empty compaction adoption CONFLICT | safe-point settlement先进入无lease的`EmptyAdoptionConflictSettling`，logical revoke并release old capability后关闭current Host；不得恢复/reseal candidate；fresh Host读取actual canonical winner |
| NONE/FULL/CONFLICT settlement与handle rotation/Host close竞态 | 同一safe-point lock线性化；rotation/close先行则禁止continuity mutation，settlement先行则完整restore或invalidate/close后Host close继续；不得出现新lease与旧current handle并存 |
| 无installed predecessor的Empty compaction FULL后same-Host reseal mismatch/failure | fail closed并关闭Host runtime；不得恢复旧Empty；restart从adopted canonical base恢复 |
| adoption FULL后 successor install失败 | adopted base保持；continuation仍有效、no-continuation与restart分别严格走下三条，不存在第四种恢复 |
| same-Host active adoption后install失败且branch仍继续 | 保留exact predecessor，用新读到的FULL confirmation重签adopted seed；不得generic discard |
| FULL后turn terminal/Hook block/最终放弃 | compaction owner先安装`NoContinuationAdmissionFence`拒绝新retry；safe-point owner穷尽检查全部successor capability且无open physical work后私签closure，CAS到`InstalledNoContinuationSettling`，logical revoke并physical release；只有release FULL及FULL/predecessor/binding再次exact-join后才arm adopted-base lease |
| installed no-continuation closure检查与continuity CAS之间发生同attempt或不同attempt/cut/destination并发retry | `scope + exact predecessor` fence已current，所有retry/preparation typed拒绝；safe-point owner再次验证同一fence后才进入settling，不存在新retry与lease并存 |
| installed no-continuation的successor capability release或最终CAS失败 | continuity保持无lease的`InstalledNoContinuationSettling(..., RevokedProviderPreparationResources)`，Host/controller进入关闭或quarantine结果；fence继续拒绝retry，不得增加continuity quarantine arm、恢复predecessor可运行状态或发布Empty lease |
| caller只声称no-continuation，或closure的scope/turn/attempt/cut/destination不匹配 | fail closed，保留predecessor，不arm Empty |
| active adoption后Host崩溃/重启 | takeover interrupt完成后注册Empty lease，下一prepare从adopted canonical base签发authority并重建 |
| idle adoption后未来重新打开 | 占用adopted-base lease，对当前suffix read新签authority，走Empty cold；不得伪造nullable adopted predecessor |
| Host takeover未能原子interrupt旧RUNNING authority | 不签发bootstrap lease/authority，provider open=0 |
| safe runtime reopen发现任一owner busy | 撤销gate/fence，旧Host保持published且可用 |
| bridge detach在安装gate前返回NotStarted | 无bridge副作用；controller exact abort，旧Host保持published且可用 |
| bridge detach安装gate后任一connection close失败 | closed outcome必须携带exact token；转`CrossOwnerRuntimeReopenQuarantine`，不得恢复old Host或丢token |
| detach FULL后、old Host unpublish前取消 | controller先prepare-abort并保留fence；bridge exact release FULL后controller才finalize-abort恢复old Host；bridge settlement失败转current quarantine |
| connect已取得old Host handle、暂停在bridge mapping发布前时开始reopen | reopen detach在同一per-session bridge lock等待；connect先发布后立即被纳入detach，或在controller fence处失败；detach FULL后不存在old connection |
| old Host已unpublish且physical close未FULL/抛错 | 进入唯一current quarantine；所有resume/reopen/dispatch/raw close/reconnect拒绝，无第二Host；用户必须完整重启Pulsara |
| old Host close FULL后bridge release失败 | controller fence仍current并转cross-owner quarantine；不得创建/publish new Host |
| old Host与bridge settlement均FULL但new Host resume失败 | 稳定no-live、canonical conversation仍OPEN，返回deferred且可普通resume；不得复活旧handle或双Host |
| runtime reopen开始时本来就没有old Host | `PreparedRuntimeResumeOperation`走bridge gate；host-side只能返回`NoOldHostReadyToResume`，不得伪造`OldHostCloseFull`；bridge FULL后finalize产出新one-shot observation并显式传给resume |
| runtime reopen finalize产出no-live observation但resume前发生并发状态变化 | `resume_session(..., no_live_observation=...)`在同一admission lock下重新验证失败，provider/Host open=0；不得忽略observation后退回live-map检查 |
| raw close暂停于unpublish后physical close前，或normal resume尚未publish | controller sparse current分别为`RawCloseInFlight`或`ResumeInFlight`；runtime reopen typed busy，不能只凭live map缺项签`StableNoLiveHostObservation` |
| HTTP request在old Host已unpublish后取消 | shielded owner task继续settleold close与bridge token，请求只停止等待；仅双owner FULL才finalize并把returned no-live observation显式传给resume，任一失败转current quarantine |
| unknown context-base mismatch | fail closed；不解释成compaction |
| adapter/lowering drift | ordinary append fail closed；只可在未来approved boundary采用新contract |
| provider reconnect/credential refresh | 不改变epoch/root |
| replay与destination不兼容 | 使用public semantic projection；不触发额外epoch |
| replay manifest relation/metadata损坏 | typed corruption，provider open=0 |
| final selected + destination-compatible replay body损坏/overbound/与projection或placement矛盾 | typed corruption，provider open=0；不得当作absence fallback |
| unselected或metadata-incompatible replay private body潜在损坏 | 本次不hydrate、不审计、不阻断semantic continuation |
| assistant ACK-unknown确认的entry/blocks/replay存在性或值不完全一致 | conflict/unknown保持；不得reset scope或确认另一winner |
| external tool side effect结果未知 | 沿用现有closure/settlement语义；不得因新epoch重试工具 |

不得在 switch 失败后透明改回 A 并处理用户明确选择 B 的请求。不得在 provider 已可能接收请求后以 cold rebuild 自动重发。

---

## 13. 生产代码范围

实施时至少审计并 hard-cut 以下 owner；以真实引用搜索为准，不为匹配文档创建空 wrapper：

```text
src/pulsara_agent/model_input/continuity.py
src/pulsara_agent/model_input/compiler.py
src/pulsara_agent/model_input/contracts.py
src/pulsara_agent/model_input/diagnostics.py
src/pulsara_agent/model_input/provider_replay.py
src/pulsara_agent/conversation_kernel/cold_epoch.py
src/pulsara_agent/conversation_kernel/input_continuity.py
src/pulsara_agent/conversation_kernel/host.py
src/pulsara_agent/conversation_kernel/provider_dispatch.py
src/pulsara_agent/conversation_kernel/safe_point.py
src/pulsara_agent/conversation_kernel/direct_model.py
src/pulsara_agent/conversation_kernel/auxiliary_model.py
src/pulsara_agent/conversation_kernel/runner.py
src/pulsara_agent/conversation_kernel/assistant_settlement.py
src/pulsara_agent/conversation_kernel/tool_runtime.py
src/pulsara_agent/conversation_kernel/reader.py
src/pulsara_agent/conversation_kernel/fork_history.py
src/pulsara_agent/conversation_kernel/compaction/coordinator.py
src/pulsara_agent/conversation_kernel/compaction/contracts.py
src/pulsara_agent/conversation_kernel/compaction/model_call.py
src/pulsara_agent/conversation_kernel/memory/governor.py
src/pulsara_agent/conversation_kernel/_repository/conversation.py
src/pulsara_agent/conversation_kernel/_repository/fork.py
src/pulsara_agent/conversation_kernel/_repository/authority.py
src/pulsara_agent/conversation_kernel/_repository/kernel.py
src/pulsara_agent/llm/model_connections.py
src/pulsara_agent/llm/model_target.py
src/pulsara_agent/llm/resolution.py
src/pulsara_agent/llm/runtime.py
src/pulsara_agent/llm/connection_probe.py
src/pulsara_agent/llm/validation.py
src/pulsara_agent/primitives/model_call.py
src/pulsara_agent/settings.py
src/pulsara_agent/llm/provider_replay.py
src/pulsara_agent/llm/request.py
src/pulsara_agent/llm/adapters/**
src/pulsara_agent/storage/migrations/sql/0000_conversation_kernel_baseline.sql
src/pulsara_agent/terminal_protocol/canonical_v3.py
src/pulsara_agent/web_app/session_controller.py
src/pulsara_agent/web_app/http_server.py
src/pulsara_agent/web_app/browser_bridge.py
```

前端除同步实际暴露被删除的 replay 字段外，还必须为 safe runtime reopen API 提供一个明确的用户动作、busy/deferred状态、close-quarantined需完整重启Pulsara的提示与重新连接行为；不得借本轮重构修改其他无关 UI。

Active specification 同步范围至少包括：

```text
ROUND_3_1_PROVIDER_INPUT_PREFIX_CONTINUITY_IMPLEMENTATION_SPEC.zh.md
ROUND_5B_LONG_HORIZON_CONTEXT_COMPACTION_IMPLEMENTATION_SPEC.zh.md
ROUND_6_MCP_PRODUCTION_CAPABILITY_IMPLEMENTATION_SPEC.zh.md
ROUND_9_UNIFIED_CAPABILITY_SEMANTICS_IMPLEMENTATION_SPEC.zh.md
PULSARA_ROUTE_WIRE_API_MODEL_UNIVERSE_AND_ADAPTER_HARD_CUT_IMPLEMENTATION_SPEC.zh.md
PULSARA_MODEL_SWITCH_HANDOVER_COMPACTION_AND_RECENT_WINDOW_HARD_CUT_IMPLEMENTATION_SPEC.zh.md
PULSARA_COMPACTION_FINAL_WIRE_ESTIMATION_HARD_CUT_IMPLEMENTATION_SPEC.zh.md
PULSARA_MEMORY_GOVERNANCE_TERMINAL_CLAIM_SOURCE_SEMANTICS_AND_PROMPT_HARD_CUT_IMPLEMENTATION_SPEC.zh.md
```

这些文档要么更新为本文的新真值，要么在冲突章节明确标成 historical/superseded；不得让 active 文档继续声称旧 reset enum、旧 provider columns 或 compatibility mismatch 是 authority。

---

## 14. 实施顺序

### Phase A：冻结新规格与红灯

1. 本文成为唯一 active epoch-boundary authority；
2. 给 generic compatibility reset 写失败测试；
3. 给两类架构边界、三个合法 new-epoch arm 与一个 append arm 写 transition authority 测试；
4. 给 canonical row provider-column subtraction 写 schema/repository 红灯；
5. 给 A Chat -> B Responses 与 A Responses -> B Chat 的 canonical reprojection 写红灯。

### Phase B：schema 与 canonical/replay 分离

1. 修改 clean-v0 baseline；
2. 删除 transcript provider replay columns；
3. 保留 provider-free assistant canonical-cut CHECK，增加 entry-kind composite FK；
4. replay attachment 改为仅由 replay table 单向拥有；
5. settlement、ACK-unknown confirmation、reader、fork/import 与 terminal projection hard cut；
6. 重置经核验的开发/测试数据库。

### Phase C：transition union 与 compiler split

1. 引入封闭 typed transition union；
2. 删除 reset enum/reason；
3. 删除 `ProviderInputEpochCompatibility` aggregate；
4. 建立原子 `InstalledEpochRuntimeCohort`，删除平行 `_installed_targets`；
5. 分离 installed append 与 new-epoch compile；
6. ordinary append 改为复用 installed cohort；
7. continuity owner exhaustive validate transition。

### Phase D：cold 与 model switch

1. 引入长期`AuthorizedEmptyBootstrapLease`，以`PreparingEmpty` reservation -> canonical/capability owner-issued handle-bound observations -> safe-point owner消费为sealed basis（持有handle/tool borrow）-> `BoundEmptyPreparation` authority两阶段线性绑定current事实；
2. 删除 generic `discard_scope()`，拆分 Host close、terminal child retire、FULL-adoption no-continuation arm 与 candidate discard；
3. Host takeover interrupt先于fresh authority签发；
4. 收紧 CanonicalColdContinuationSeed；
5. Tier 1 direct precheck 的 authority 线性传到 install；
6. connection context-bearing配置改为immutable ID，新配置新ID；
7. 将controller per-session current operation hard-cut为覆盖resume/raw close/runtime reopen/quarantine的closed union；实现old-Host与no-old-Host两个sealed operation arm、独立`NoOldHostReadyToResume` closed outcome、返回并显式传入`resume_session(..., no_live_observation=...)`的one-shot observation、bridge closed detach outcome/token/settlement、与connect共用per-session lock的gate、cross-owner quarantine及lock-entry安全退休；
8. terminal child retirement释放bounded并发slot且不建tombstone map；
9. reasoning-only、connection switch、target switch、mid-turn drift测试闭合；
10. 清除 `MODEL_TARGET_CHANGED` reset分支。

### Phase E：compaction successor

1. 以不可安装的`CompactionDryProjectionBasis/Result`和closed installed/pending-Empty dry source替代pre-adoption宽泛seed与install-capable cold assembly carrier；
2. 将summary execution hard-cut为transport-free、phase-bound one-shot `CompactionSummaryProviderOpenPermit`；source-less严格执行sealed basis -> summary/unique repair -> dry admitted -> safe-point revalidate -> begin adoption；
3. adoption outcome封闭为`NONE | FULL | CONFLICT`，network ACK unknown只保持pending并由shielded ownerexact-confirm；
4. adoption FULL后重新取得owner-issued read/capability basis，才私有签发non-null predecessor的adopted seed；
5. installed predecessor的active continuation、五类private`NoContinuationClosure`、post-adoption failure/restart各走唯一arm；no-continuation必须先安装admission fence，再由safe-point owner穷尽检查、进入无lease settling、revoke/release FULL后发布lease；无predecessor的FULL adoption同样先进入无lease settling，revoke/release FULL后才完成non-nonce Empty reseal；
6. model-switch Tier 2/3只通过adopted transition安装B；
7. 删除`CONTEXT_BINDING_REWRITE` reset分支。

### Phase F：adapter/lowering 收口

1. same-epoch exact adapter/profile/native projection reuse；
2. 引入只含connection execution contract（ID + authentication）、v7 target fact（含tool-call capability且删除target/endpoint fingerprint）、reasoning/projection strategy/estimator的execution-independent target bundle，以及统一`FrozenProviderPhysicalCallTarget`与epoch wrapper；穷尽删除9.4列出的target fingerprint字段/helper/check/diagnostic pass-through，以完整typed values/private authority替代，只有durable replay builder局部计算endpoint compatibility digest，native-tool contract仅为strategy派生property；
3. 通过唯一`ModelRuntime.borrow_transport(purpose_permit)`与四臂closed purpose permit union短借physical transport；runtime只读permit内唯一call target，summary显式区分call target与successor destination并执行A/A、A/B、B/B矩阵；删除四个open-site直接持有transport的路径，并删除无生产owner的`CONTEXT_WINDOW_COMPACTION_SUMMARY | COMPACTION_MEMORY_EXTRACTION` purpose，不留alias；
4. drift fail-closed；
5. cold/compaction destination重新materialize；
6. replay保持metadata-first、selected-compatible-only hydration并区分可见metadata corruption与被消费body corruption；
7. 按9.5逐字段删除compatibility重复真值；
8. 删除 `PROVIDER_LOWERING_CHANGED`、`BASE_SYSTEM_CHANGED`、`TOOL_SURFACE_CHANGED` 等剩余分支。

### Phase G：删除旧真值并验证

1. 删除旧测试、fixture、enum export、文档权威声明；
2. 搜索确认没有 compatibility fallback、generic scope reset或未授权nonce factory；
3. focused tests；
4. PostgreSQL clean-v0全量；
5. Python全量与architecture checks；
6. 使用用户保存配置执行真实 Chat/Responses/model-switch/compaction dogfood；
7. isolated wheel/launcher 按现有 activation 要求验证；
8. 同步第13节所有active规格，确认不再有冲突权威。

任何 phase 不保留临时 old/new 双轨；每个提交点都应保持唯一生产路径。

---

## 15. 测试矩阵

### 15.1 Ordinary append strict prefix

- SYSTEM producer当前值改变，installed SYSTEM保持byte-identical；
- tool catalog新增/删除/修改，installed tools保持byte-identical；
- Skill/MCP/Hook/memory/permission/reconnect变化不重建root；
- messages只能追加suffix；
- final Chat与Responses materialization分别保持prefix；
- registry/target/lowering drift不能产生fresh nonce；
- generic mismatch provider open=0。

### 15.2 Cold bootstrap

- exact`AuthorizedEmptyBootstrapLease`只能在lock内进入`PreparingEmpty(reservation)`；canonical reader/capability owner私签exact handle-bound carrier，safe-point owner对exact active handle调用`_require_current()`并消费carrier形成持有真实tool borrow的basis后，exact bind进入`BoundEmptyPreparation(basis, authority)` -> fresh nonce/revision 1；
- slot absent/裸None/伪造lease或authority均拒绝；
- lease签发早于未来turn时允许合法suffix推进；prepared authority必须绑定最终read through；
- canonical read failure、capability/target freeze failure、compile failure、candidate构造前cancellation以及candidate discard分别在reservation/basis-authority/candidate阶段exact close，并归还同一个lease；下一次prepare签发新reservation/basis/authority，旧read不能重放；
- 可见字段与hash全部相同但非canonical reader签发的read carrier、非capability owner签发的capability carrier、伪造/foreign tool borrow、rotated/closed safe-point handle、cross-attempt target/basis、install前handle rotation全部拒绝且provider open=0；
- Host close与abort竞态时outstanding reservation/basis/authority/candidate失效且不在closed owner中恢复lease；成功install/model-active后lease/handles都不能重放；
- restart从FULL_HISTORY重建；
- restart从adopted SNAPSHOT + suffix重建；
- Host takeover先interrupt旧RUNNING turn/subagent，再签发lease；当前provider prepare之后才签authority；
- takeover ACK未知/失败时不签发lease/authority；
- terminal child从slot移除并释放并发容量，durable terminal status阻止同task reopen；连续完成任意多批child不触发总生命周期cap；
- assistant settlement/provider/replay错误不清空installed slot；
- current SYSTEM/tools/adapter只在该boundary冻结；
- cold失败不留下半安装slot或permit；
- safe runtime reopen逐一覆盖turn/queue、provider/assistant/tool/compaction、subagent、terminal/monitor、Hook/MCP/extension/memory busy；busy时旧Host保持published且无operation残留；
- 验证bridge detach closed outcome：gate前NotStarted无副作用可abort；gate后partial failure始终返回token并进入cross-owner quarantine；detach FULL后的取消按controller prepare-abort -> bridge release FULL -> controller finalize-abort恢复old Host，settlement失败不得先清controller fence；
- 确定性暂停connect于`resume_session()`取得old handle之后、bridge mapping publication之前，再发起reopen：detach必须等待同一session lock并删除随后发布的connection，或connect在controller fence处typed失败；post-fence connect不得发布old-host mapping；
- 分别暂停raw close于unpublish后/physical close前、normal resume于Host publication前，runtime reopen都必须因`RawCloseInFlight | ResumeInFlight`拒绝；只有admission lock下同时确认所有live/in-flight/task状态后私签的`StableNoLiveHostObservation`可开始no-host reopen，且不为历史idle session留map entry；验证runtime-reopen finalize产出的observation确实作为显式参数传给`resume_session`，该方法在同一lock中消费它并把`RuntimeReopenInFlight`原子替换为`ResumeInFlight`，stale/foreign/replayed observation全部拒绝；
- reopen成功严格bridge detach FULL -> old Host unpublish/full close -> bridge release FULL -> controller finalize产生no-live observation -> 显式observation resume/publish，无双Host窗口；no-old-Host arm只能返回`NoOldHostReadyToResume`，不能调用close或伪造`OldHostCloseFull`，且同样等待bridge FULL与finalize observation；close/bridge未FULL进入current quarantine；双FULL后resume失败返回deferred且以后可普通resume；HTTP cancel不中断双owner settlement；正常终态不累积operation/fence；新Host cold bootstrap采用新capability；
- 跨大量session反复connect/reopen/close后，per-session bridge lock/gate entry在无connection、gate、waiter、controller operation时identity-safe退休，不形成无界current-state泄漏。

### 15.3 Tier 1 model switch

- A Chat -> B Responses direct fit；
- A Responses -> B Chat direct fit；
- same wire API不同model/endpoint；
- same target不同connection；
- context-bearing connection edit创建新ID，旧ID不可变；credential-only refresh保留同ID且不换epoch；
- reasoning-only不换epoch；
- mid-turn/tool-followup不切target；
- B over token/byte/modalities bound不进入Tier 1；
- transition predecessor cohort/destination/turn binding任一漂移时provider open=0；
- authority-free`HandleFreeProviderWireObservation` -> exact-fit admitted -> consume为纯`FrozenDirectSwitchAdmission` -> owner-issued read/capability carriers -> current safe-point basis -> private direct authority -> transition/candidate exact-bind admission+basis；原measurement observation/Resolved candidate不进入transition，任一value漂移均销毁observation/admission/basis/authority并provider open=0；
- direct authority签发后或final install前旋转/关闭handle、替换tool borrow均fail closed，B provider open=0；
- direct authority只在exact-fit admitted后签发；Tier 2/3路径不存在可复用provisional direct authority；
- Tier 1无compaction divider/event/snapshot。

### 15.4 Tier 2/3 model switch

- Tier 2严格`summary_call_target=A`、`successor_destination=B`，handover后adopted B successor；交换成B/A或误用B summary均provider open=0；
- Tier 3严格`summary_call_target=B`、`successor_destination=B`，B-side projection后adopted B successor；
- 每次成功仅一次canonical adoption与一次B normal open；
- adoption前crash无canonical effect；
- adoption后crash恢复same snapshot base；
- FULL后adopted seed必须使用新owner-issued canonical/capability basis；pre-adoption basis或final install前handle rotation均拒绝；
- 不依赖MODEL/LOWERING/CONTEXT reset reason；
- A native replay不兼容B时使用public semantic projection。

### 15.5 Ordinary compaction

- `CompactionDryProjectionBasis`只能pure project/quote；其closed source union覆盖installed cohort与pending Empty，且共用的`FrozenEpochModelCallTarget`不包含`PreparedKernelModelTarget`/`ResolvedModelTarget`/`ResolvedModelCall`/transport/client/registry/credential，类型上不能产生candidate/permit/install或borrow transport；
- `CompactionDryProjectionResult`只含semantic projection/materialization/quote/pure admission，不能传入register/install；dry path不返回`ColdEpochInputAssemblyResult`；
- adoption FULL后才能构造successor；
- adopted predecessor必须non-null且exact installed cohort；
- pending Empty在无predecessor时严格执行sealed basis -> initial summary/至多一次unique repair（每次独立one-shot permit）-> dry admitted -> safe-point revalidate -> begin adoption；pending前失败恢复原Empty state；
- closed outcome覆盖NONE/FULL/CONFLICT：NONE仅在exact current时恢复，FULL先进入无leasesettling、revoke/release FULL后才reseal，CONFLICT进入无leasesettling并revoke/release后关闭Host；network ACK unknown保持非终态pending并由shielded owner确认；immediate continuation重做完整Empty prepare，SessionStart/PostCompact block或final abandonment留下adopted-base lease；
- NONE/FULL/CONFLICT分别与handle close、generation rotation及Host close做确定性竞态：所有settlement都必须经safe-point owner持锁调用continuity，证明不会同时留下新/旧lease与current handle/tool borrow；
- 分别从Preparing reservation+sealed basis、Bound authority、Prepared Empty candidate进入`EmptyAdoptionPending`，验证safe-point handle current、attempt/cut/destination exact join及caller cancellation由owner task继续settle；
- 对pending Empty在repository FULL确认后、logical revoke前确定性暂停，证明slot为`EmptyAdoptionFullSettling`、新lease尚未发布且所有prepare/open拒绝；再分别注入tool-borrow/handle physical release failure与final CAS failure，证明保持无leasequarantine并关闭Host；成功时证明旧handle/basis/authority/candidate/borrow先失效并release FULL、随后才发布lease；不增加第四个nonce arm；
- pending Empty在adoption前/network ACK unknown/NONE/FULL/CONFLICT各点Host close或断电，均不凭process-local attempt复活旧lease；新Host只从repository当前canonical winner/base走fresh Empty；
- same-target successor可采用当前boundary的SYSTEM/tools/adapter；
- same-Host post-adoption install failure保留predecessor并重签adopted seed；
- idle、durable turn-not-running、`PostCompact` blocked、`SessionStartCompact` blocked、final abandonment各自只能提供对应product evidence；compaction owner必须先安装`NoContinuationAdmissionFence`，其`scope + exact predecessor`占位阻止所有新adopted retry/preparation，包括使用不同attempt ID/cut/destination的请求；safe-point owner重新验证fence/current handle后才私签最终`NoContinuationClosure`，伪造/重放/交叉scope-turn-attempt-cut-destination均拒绝；
- 对closure检查完成而尚未进入continuity CAS的窗口确定性暂停，分别并发同attempt与不同新attempt ID/cut/destination的retry，证明同一predecessor fence全部拒绝；逐项保留`PreparedProviderInputHandle`、`SealedProviderInputPreparationBasis`、`ProcessLocalToolSurfaceBorrow`、`AdoptedCompactionContinuationSeed`、transition、candidate、continuation handle、empty reservation/authority、summary/agent-loop permit/borrow或provider stream，证明任一current/open physical work都阻止签closure或进入settling；
- 有效closure + FULL + predecessor先统一进入`InstalledNoContinuationSettling`，再logical revoke并physical release全部successor capability；release FULL且binding再次exact-join后才arm adopted-base lease。注入任一release/final CAS failure时continuity保持无lease的同一settling arm、fence保持、Host/controller关闭或quarantine，不恢复predecessor且不新增continuity quarantine arm；
- restart后只走authorized Empty cold，不走nullable adopted arm；
- FULL后no-continuation只能按fence -> safe-point closure -> non-authorizing settling -> logical revoke -> physical release FULL -> lease publication的顺序消费exact closure/FULL/predecessor；未来prepare仅在最终lease发布后允许suffix推进、签发一次性authority；
- unknown context rewrite拒绝。

### 15.6 Adapter/replay

- same epoch复用exact adapter/materializer/projection set；
- bundle private factory与`__post_init__`对connection ID/authentication mode、target key/endpoint/wire/limits-budget/modalities/tool-call capability、estimator fact/route profile/native-tool support/replay target/transport binding/reasoning contract的每一种mismatch组合都fail closed；
- bundle只保存connection execution contract、v7 target fact、reasoning/strategy/estimator，不保存完整`ModelConnectionConfig`，也不重复`target_contract`/`model_profile`/`context_budget`/`replay_target`/native-tool/transport binding fields；native-tool contract仅从strategy派生，borrow按ID重读当前config并与authentication/tool-call/fact exact-check；
- v7穷尽删除`ResolvedModelTargetFact.target_fingerprint`、`ResolvedModelTargetFact.endpoint_fingerprint`、`resolved_model_target_fingerprint`、`LLMContext.target_fingerprint`、`ContextCompileBudgetReport.target_fingerprint`、`FrozenProviderWireInputPlan.resolved_target_semantic_fingerprint`、`ProviderWireMeasurement._resolved_target_semantic_fingerprint`与`ModelInputCompileOperationalProjection.target_fingerprint`及全部compare/populate/pass-through；request validation使用完整typed value/owner identity；独立replay target/endpoint compatibility digest跨重启保留且只由replay builder从完整endpoint局部计算；
- `build_provider_replay_target_compatibility(*, target_fact, ...)`只接受完整target fact并在builder内部从`canonical_endpoint_base_url`局部重算历史endpoint identity；不存在endpoint-string overload或legacy fingerprint参数。测试证明durable `ProviderReplayTargetCompatibilityFact.endpoint_identity_fingerprint`字节稳定，而resolved DTO、bundle、call target及diagnostic均不携带该digest；
- `FrozenProviderPhysicalCallTarget`是四类permit唯一borrow target，epoch wrapper不重复purpose/binding/bundle/budget；runtime API不接受第二target参数；
- 四个真实open-site各只能消费对应的closed purpose permit；`ModelCallPurpose` physical-open set只含`AGENT_MODEL_LOOP | CONTEXT_COMPACTION_SUMMARY | MEMORY_GOVERNANCE | CONNECTION_PROBE`，旧`CONTEXT_WINDOW_COMPACTION_SUMMARY | COMPACTION_MEMORY_EXTRACTION`值、exports、parse alias与tests均不存在；memory governance在owner-issued durable terminal fence carrier后使用auxiliary permit，unsaved connection probe以`CONNECTION_PROBE` purpose使用ephemeral draft permit且不查询saved settings；summary initial/repair均可在adoption前open但不能换取epoch authority；
- ordinary summary A/A、Tier 2 A/B、Tier 3 B/B、pending Empty B/B逐项通过；summary permit的successor destination不能被runtime借用；
- lowering contract drift fail closed；
- Host restart应用新lowering；
- adopted compaction应用新lowering；
- compatible replay native replacement；
- incompatible replay public semantic fallback；
- replay absence不改变canonical entry内容；
- manifest relation/metadata损坏时typed corruption；
- final selected + compatible body缺失/损坏/overbound/placement不符时typed corruption且不fallback；
- unselected或metadata-incompatible private body不hydrate、不审计、不阻断；
- same epoch已安装native replay不能切回public placement；
- assistant blocks/replay required attachment仍原子提交。

### 15.7 Schema

- `transcript_entries` 不含provider/wire/replay pointer字段；
- provider-free CHECK仍严格约束assistant/non-assistant canonical cut；
- replay row通过entry-kind composite FK唯一引用assistant entry；
- 不存在transcript -> replay反向FK或循环pointer；
- ACK-unknown exact确认entry、ordered blocks及replay存在性/完整值；
- imported semantic history不需要provider placeholder；
- fork/import目标session只能从自身new-session/fresh-Host Empty lease启动，不得继承source cohort/nonce/lease/reservation/authority/candidate/permit/closure；
- fork复制semantic truth，并只在现有replay fork契约允许时复制/重绑定attachment；
- clean-v0创建、约束、删除与reset通过；
- 不存在legacy schema fallback。

### 15.8 Architecture oracle

静态检查至少断言生产代码不存在：

```text
ProviderInputEpochResetReason
_compatibility_reset_reason
EXPLICIT_TEST_RESET
reset_reason is not None
BASE_SYSTEM_CHANGED
TOOL_SURFACE_CHANGED
MODEL_TARGET_CHANGED
PROVIDER_LOWERING_CHANGED
CONTEXT_BINDING_REWRITE
ProviderInputEpochCompatibility
```

若某个字符串仅存在于明确标记为历史的文档，可允许；生产代码、active spec 与测试不得继续依赖。

Architecture oracle 还必须枚举并 allowlist：

- 所有 fresh epoch nonce factory/call site；
- 所有`AuthorizedEmptyBootstrapLease`、`EmptyPreparationReservation`、canonical reader/capability owner-issued carrier、safe-point owner`SealedProviderInputPreparationBasis`与`PreparedEmptyScopeBootstrapAuthority` private constructor/issuer；
- 所有`AuthorizedEmpty -> PreparingEmpty -> BoundEmptyPreparation -> Prepared/Installed` transition，以及handle/tool borrow/basis/lease/reservation/authority/candidate占用、current验证、归还、消费、线性转移与Host-close invalidation；
- 所有`EmptyAdoptionPending` begin、network-unknown confirm，以及唯一safe-point-owned`settle_empty_adoption()`对NONE restore、FULL/CONFLICT non-authorizing settling、logical revoke、physical release与最终lease/no-lease结果的call site；continuity子操作不得被coordinator直调，`AuthorizedEmpty` publication在AST/control-flow上必须后支配revoke与release FULL，并证明该路径不生成nonce或epoch agent-loop permit；
- 所有 full canonical reprojection entry point；
- 所有 continuity scope close/remove/pop call site；
- 所有adopted seed factory、以`scope + exact predecessor`占位并拒绝跨attempt retry的`NoContinuationAdmissionFence`安装/校验/消费、五类product evidence与最终`NoContinuationClosure` private issuer、`InstalledNoContinuationSettling` revoke/release/FULL-no-continuation retire call site，以及四臂provider-open purpose permit factory/consumer；continuity union不得新增`InstalledNoContinuationQuarantine`或等价第二arm，失败只能停在携带revoked resources的同一settling state并由Host/controller承载quarantine；
- 所有`FrozenEpochModelCallTarget` private factory、closed dry source/result constructor及其禁止borrow transport/register/install的type boundary；
- 所有controller per-session`ResumeInFlight | RawCloseInFlight | RuntimeReopenInFlight | Quarantined` sparse current transition、`StableNoLiveHostObservation` private issuer/consumer、old/no-old sealed operation union、`NoOldHostReadyToResume`唯一factory、finalize observation返回值及`resume_session(..., no_live_observation=...)`显式consumer、bridge closed detach outcome/token、同session-lock gate/settle、cross-owner quarantine与lock/gate entry retirement call site。

除明确 owner 之外，生产代码不得直接 `discard_scope()`、从裸 `None` 构造 cold seed、在错误 cleanup 中 pop slot、手工生成 fresh nonce，或让 dry compaction result进入install API。Oracle 校验的是 allowlisted ownership，不只是旧 enum 字符串消失。

类型/AST oracle还必须断言所有transition/new-epoch candidate、`InstalledEpochRuntimeCohort/FrozenEpochModelTargetBundle/FrozenProviderPhysicalCallTarget/FrozenEpochModelCallTarget/FrozenDirectSwitchAdmission/CompactionDryProjectionBasis/Result`都不引用`ResolvedModelTarget`、`ResolvedModelCall`、`PreparedKernelModelTarget`、`NormalizedLLMTransport`、client/registry/credential handle，且cohort顶层不重复view/target bundle已拥有的SYSTEM、tools、frontier、profile、estimator或replay fields；bundle不存完整`ModelConnectionConfig`，也不存`target_contract/model_profile/context_budget/replay_target/native_tool_wire_contract/transport_binding/target_fingerprint`副本字段。`ResolvedModelTargetFact.target_fingerprint`、`ResolvedModelTargetFact.endpoint_fingerprint`、`resolved_model_target_fingerprint`、`LLMContext.target_fingerprint`、`ContextCompileBudgetReport.target_fingerprint`、`FrozenProviderWireInputPlan.resolved_target_semantic_fingerprint`、`ProviderWireMeasurement._resolved_target_semantic_fingerprint`、`ModelInputCompileOperationalProjection.target_fingerprint`及其constructor/serialization/check全部必须不存在。Architecture tests必须为private freeze factory、bundle内部校验及borrow-time当前config校验分别构造connection ID、authentication mode、target key、endpoint、wire、limits/budget/modalities/tool-call capability、estimator fact、route profile/native-tool support/replay target/transport binding/reasoning mismatch cases。所有physical provider open只能从allowlisted`ModelRuntime.borrow_transport(purpose_permit)`结果进入且runtime无第二target参数：epoch agent-loop arm要求continuity install/model-active，compaction summary、auxiliary memory与connection probe各要求9.6节自己的exact permit；dry path和transition/candidate均不在physical capability allowlist。`ModelCallPurpose`枚举必须exact等于9.6节四值，旧summary/memory枚举值及alias不存在。

模型目标派生hash的AST/call-site allowlist必须逐名封闭为：`llm.provider_replay._replay_target_fingerprint`及其durable replay读写/校验consumer；`build_provider_replay_target_compatibility(target_fact=...)`内部从`canonical_endpoint_base_url`局部计算并仅写入durable `ProviderReplayTargetCompatibilityFact.endpoint_identity_fingerprint`的replay-boundary路径；`provider_wire_input_plan_identity_fingerprint(plan, *, target_fact)`；以及compaction的`_summary_source_identity_value(..., target_fact=...)`最终aggregate proof路径。后两种aggregate builder只能在内部从完整fact局部形成历史稳定component，不得返回该component或把它写入DTO。除此之外，任何model-target-derived fingerprint field/helper/call site都使oracle失败；`llm.resolution`不得再构造endpoint digest，PostgreSQL的`database_target_fingerprint`属于独立database endpoint/schema边界，不在本规则内。

Transport-capable`PreparedKernelModelTarget`/`ResolvedModelCall`在hard cut后只能出现于boundary resolution、bundle/call-target private freeze factory及各purpose的pure preparation内部，不能直接带transport穿过permit边界；所有provider-open/dispatch API必须在类型与AST allowlist上拒绝裸resolved/prepared对象。Summary permit的`call_target`必须identity-equal`summary_call_target`，`successor_destination`不得进入runtime borrow，并静态/动态覆盖A/A、Tier-2 A/B、Tier-3 B/B、pending-Empty B/B矩阵。`HandleFreeProviderWireObservation`必须在transition前被消费并抽取为`FrozenDirectSwitchAdmission`，不得使其内部legacy measurement candidate进入transition/new-epoch candidate。Oracle还必须枚举当前四个`.open_stream()`site并在hard cut后确认它们都改为consume对应permit的borrowed transport，不允许新增第五个直接site。

---

## 16. Real-provider dogfood

使用 `LocalSettingsStore` 与 `require_pulsara_home()` 只读加载用户保存配置；不得导出、打印或复制真实 credential。使用经核验、可重置的 PostgreSQL target。

至少执行：

1. Chat cold bootstrap，多轮append，证明SYSTEM/tools/messages prefix；
2. Responses cold bootstrap，多轮append，证明actual wire prefix；
3. Chat A -> Responses B Tier 1，证明canonical semantic history被B重新materialize；
4. Responses A -> Chat B Tier 1；
5. A -> smaller B触发Tier 2或Tier 3，证明adoption后才首次B normal open；
6. provider-native replay compatible时保留、incompatible时semantic fallback；
7. 对final selected + destination-compatible replay注入body corruption，证明typed corruption且不会public fallback；另证明unselected/incompatible opaque body不被hydrate；
8. capability/catalog变化在old epoch不热替换；通过唯一HTTP runtime reopen命令验证busy不动旧Host、成功无双Host窗口、close-FULL后resume-deferred可恢复；注入physical close failure后同进程所有入口拒绝且进程重启后fresh Host恢复；
9. post-adoption分别模拟same-Host install失败、FULL后no-continuation与Host重启，证明三者分别走non-null adopted重试、retire-to-lease、fresh-Host Empty；
10. 对fresh Host/fork首次open制造source-less over-budget compaction，证明FULL后non-nonce Empty reseal并重新取得safe-point handle，最终仍只走`EmptyScopeColdStart`；
11. 模拟takeover前存在RUNNING turn/subagent，证明先interrupt再cold open；
12. credential值始终redact，其余真实prompt/response/wire诊断按AGENTS.md保留可观察性。

Dogfood失败不得通过修改模型输出、添加provider名称分支、跳过assertion或恢复旧reset路径制造通过。

---

## 17. 完成定义

只有全部满足以下条件，才能标记本hard cut完成：

1. 每个fresh epoch nonce都能被一个且仅一个typed transition解释；
2. Empty cold必须经过`AuthorizedEmpty -> PreparingEmpty(reservation) -> canonical/capability owner-issued carriers -> safe-point sealed current basis(handle + tool borrow) -> BoundEmptyPreparation(authority)`线性绑定；裸None、可见值/hash自洽但非owner-issued的read/capability、foreign borrow、stale/cross-attempt handle/basis、普通错误或terminal child无法伪造cold，且pre-candidate failure可exact归还lease并关闭physical borrow；
3. 不存在“compatibility/fingerprint不同所以reset”的生产分支；
4. ordinary epoch SYSTEM/tools byte-identical、messages与actual wire append-only；
5. continuity slot原子拥有单一runtime cohort；view与只含connection execution contract（ID/authentication）、v7 target fact（含tool-call capability且无target/endpoint fingerprint）、reasoning/strategy/estimator的target bundle各自只有一份truth，9.4节穷举的target fingerprint字段/helper/check/diagnostic pass-through全部删除且只有该节显式hash allowlist存在，durable replay endpoint compatibility digest只在replay builder内从完整canonical endpoint局部计算，native-tool contract只由strategy派生，完整connection config只在freeze/borrow边界临时读取校验，cohort不含transport/client/credential或平行target state；
6. A -> B direct switch完全从canonical effective context重投影，不转换A wire payload，并依次消费authority-free measurement、owner-issued read/capability basis、admission、private authority与exact-bound candidate；authority签发及install均重验safe-point current；
7. compaction dry basis/result无安装/运行能力、不含Prepared/Resolved target/call/transport且不返回cold assembly carrier；summary initial/unique repair使用各自transport-free one-shot permit且发生在adoption前，A/A、Tier-2 A/B、Tier-3 B/B、pending-Empty B/B target矩阵封闭；adopted successor只能由FULL adoption、FULL后fresh owner-issued basis与non-null exact predecessor授权；installed predecessor的FULL no-continuation必须先以`scope + exact predecessor` admission fence阻止所有同/跨attempt新retry，再由safe-point owner穷尽检查全部successor capability，进入无lease settling并在logical revoke/physical release FULL后才发布adopted-base lease；失败时continuity保持同一settling arm，Host/controller才进入关闭或quarantine；无predecessor的FULL也必须先进入无lease settling，revoke/release FULL后才完成non-nonce Empty reseal并重新prepare；任一release/final CAS失败均无lease；source-less outcome完整覆盖NONE/FULL/CONFLICT并全部经safe-point settlement，network ACK unknown永不冒充terminal；
8. 所有transition/new-epoch candidate只携带`FrozenEpochModelCallTarget`，adapter/lowering drift不能热重建root；physical transport只由`ModelRuntime.borrow_transport(purpose_permit)`消费permit内唯一`FrozenProviderPhysicalCallTarget`后按operation短借，closed permits分别覆盖epoch agent-loop、compaction summary、auxiliary memory与unsaved connection probe，且只有agent-loop arm要求continuity install/model-active；`ModelCallPurpose`只剩四个对应值，两个无生产owner的旧compaction purpose已删除且无alias；
9. provider-native replay与canonical semantic row物理分离；metadata-first/selected-compatible-only hydration不变，被消费的corrupt body不fallback；
10. clean-v0 schema保留canonical cut invariant与assistant-kind FK，无旧列、循环pointer、双读、双写或迁移fallback；
11. `ProviderInputEpochCompatibility`、generic `discard_scope()` 与未allowlist的fresh nonce factory全部删除；terminal child释放bounded slot且不建无界tombstone；
12. Host takeover在cold authority之前中断旧RUNNING authority；controller sparse current union覆盖resume/raw close/runtime reopen/quarantine，no-live必须由同锁完整检查私签`StableNoLiveHostObservation`；safe runtime reopen对old/no-old Host使用sealed closed union，no-old只返回`NoOldHostReadyToResume`，controller finalize产出的exact observation必须显式传给并由`resume_session(..., no_live_observation=...)`在同一lock消费后原子安装`ResumeInFlight`；bridge closed outcome/token/settlement共同线性化，partial/settlement failure保留current quarantine并要求完整重启，不存在old-handle publication或双Host窗口；per-session bridge entries可安全退休且idle session不留历史map entry；
13. 未增加durable event/relation/job/checkpoint/receipt；
14. focused、PostgreSQL、全量、architecture、真实provider与isolated验证全部通过；
15. fork/import 新session只能用自身new-session/fresh-Host Empty authority，不继承source process-local cohort/handle/permit；
16. 所有冲突旧规格被明确标记为历史或由本文覆盖，active文档不再宣称旧reset语义。

最终必须先能对任意physical provider open回答“它消费了9.6节哪个exact product-purpose permit”。对任意epoch agent-loop provider open还必须进一步回答：

```text
它是在同一installed epoch中追加了什么suffix？

或者：

它由哪个 owner-issued cold subtype 或哪个 FULL-adopted compaction boundary 获准建立新root？
```

Epoch agent-loop若无法给出append或approved new-root boundary之一即不合法；summary、auxiliary memory与probe若没有各自purpose permit，或试图借其permit取得epoch/continuity/tool-execution authority，同样不合法。
