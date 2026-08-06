# Pulsara Context Input Manifest 引用化、分页化与非阻塞审计 Hard Cut 计划

状态：`MP0-MP3 CODE COMPLETE / TARGETED VERIFIED`

实施回执日期：`2026-08-06`

范围：只修复 Context Input Manifest 独立于 provider token budget 的第二上下文窗口，并完成与它直接相连的 `ContextCompiled -> ProviderInput -> ModelStart -> recovery/replay/GC` ownership hard cut；不重写 Context compiler、Long-Horizon 语义、provider vector、通用 ArtifactStore 或 transcript reducer。

实施口径：reset-only hard cut；不保留 `context-input-manifest:v8` 的生产兼容路径，不新增 compatibility shim、durable audit job、manifest repair owner 或数据库表。

---

## 1. 事故事实

2026-08-05 的真实 TUI dogfood 中，一次 terminal 工具调用已经成功提交，Pulsara 准备后续模型调用时在 provider dispatch 之前失败：

```text
context input manifest exceeds max_input_manifest_chars:
1065543 > 1048576
```

对应 durable 轨迹：

```text
ModelCallEnd(completed)
-> accepted disposition
-> ToolResult terminal projection FULL
-> Context projection rewrite
-> ContextCompiled(status="failed", failure_stage="candidate_materialization")
-> RunError(code="context_input_candidate_invalid")
-> RunEnd(status="failed", stop_reason="model_error")
```

这不是 provider context overflow：

| 指标 | 现场值 |
|---|---:|
| provider input budget | 239,616 tokens |
| 上一次成功 final payload estimate | 56,844 tokens |
| 上一次成功 manifest artifact | 1,041,836 bytes |
| 下一次 manifest candidate | 1,065,543 bytes |
| manifest hard cap | 1,048,576 bytes |

最近一次成功 manifest 的主要物理组成：

| 顶层字段 | canonical JSON 体积 |
|---|---:|
| `ordered_transcript_projection` | 约 536 KiB |
| `snapshot` | 约 292 KiB |
| `transcript_authority` | 约 45 KiB |
| `prepared_candidate_set` | 约 38 KiB |
| `projection_state` | 约 36 KiB |
| 其他 policy、refs、attribution | 约 70 KiB |

`ContextCompileInputManifestFact` 同时内嵌 snapshot、ordered projection、transcript authority、provider plan、projection state 和多组 attribution。它们大量引用相同语义内容。实际 provider payload 远未达到 token budget，审计载体却先达到 1 MiB，形成独立的第二上下文窗口。

示例程序重复输出历史加速了增长，但不是根因。任何足够长且仍处于合法 provider token budget 内的 session 都能稳定复现。

---

## 2. 当前代码真值与必须切断的耦合

### 2.1 当前 production 顺序不是一个三事件事务

生产主路径位于：

- `src/pulsara_agent/runtime/agent.py`
- `src/pulsara_agent/runtime/context_input/manifest.py`
- `src/pulsara_agent/runtime/provider_input/coordinator.py`
- `src/pulsara_agent/runtime/provider_input/planner.py`
- `src/pulsara_agent/runtime/provider_input/store.py`
- `src/pulsara_agent/llm/lifecycle.py`

当前真实顺序是：

```text
build full ContextCompileInputManifestFact
-> serialize and enforce max_input_manifest_chars
-> synchronously require manifest artifact FULL
-> finalize and register process-local ProviderInput preparation
-> commit ContextCompiledEvent
     # reducer installs durable preparation attribution
-> prepare Model lifecycle Start bundle
-> atomically commit ProviderInput companion events + ModelStart
-> dispatch provider
```

`ContextCompiledEvent` 与 `ProviderInputAppendCommittedEvent + ModelStartEvent` 不是同一个 transaction。前者是 durable preparation installation boundary，后者是 exact consumption boundary。Hard cut 必须保留这两个线性化点，不能把它们在文档中伪装成一个三事件 batch。

### 2.2 Artifact availability 错误控制 model admission

`ContextInputManifestProjectionReferenceFact` 当前同时包含：

- ordered provider projection identity；
- manifest artifact ID；
- manifest content fingerprint；
- manifest fact fingerprint。

Provider Input preparation、rollover、append、committed reference、ModelStart recovery 和 `ContextCompiledEvent` 都 exact join 该 fact。结果是：

```text
audit artifact availability
  -> ProviderInput preparation admission
  -> ContextCompiled FULL
  -> ProviderInput append + ModelStart
  -> provider dispatch
```

这是本次事故的直接 ownership 错误。

### 2.3 ContextCompiled 本身仍是第二个大型 carrier

当前 `ContextCompiledEvent` 还直接保存：

- `sections`；
- `tool_specs`；
- `diagnostics`；
- `lifecycle_decisions`；
- 两套 tool-result render decision；
- `tool_result_budget_report`；
- 完整 `PreparedProviderInputAppendCandidateFact`。

这些字段主要由 Inspector 和旧 replay 使用。Provider store/recovery 真正需要的只是 preparation ownership、commit guard、candidate/plan fingerprint 和 exact `ContextCompiled` event reference。只替换 manifest artifact reference 而保留上述载体，无法兑现“小型 semantic commit”。

### 2.4 Provider preparation 与 recovery 仍依赖 manifest vocabulary

以下 durable/process-local join 都使用 `compiled_manifest` 或 `manifest_projection_reference_fingerprint`：

- `ProviderInputPreparationOwnershipAttributionFact`；
- `ProviderInputRolloverRequestFact`；
- `PreparedProviderInputAppendCandidateFact`；
- `ProviderInputAppendCommittedEvent`；
- `CommittedProviderInputReferenceFact`；
- `runtime/provider_input/store.py` 的 committed fold；
- `runtime/provider_input/recovery.py` 的 crash-before-ModelStart abandonment；
- `runtime/model_stream_recovery.py` 的 ModelStart/append exact join。

这些调用面必须在同一次 hard cut 中切到 compact context commit；否则 artifact reference 仍会通过 recovery seam 反向成为 dispatch authority。

### 2.5 旧 replay 混合了两种不同承诺

`runtime/context_input/replay.py` 当前既尝试：

1. 从 full manifest 重建整个 compiler invocation；
2. 证明 provider 最终实际看到了什么。

第二项已有更强的 durable authority：

```text
ModelStart.provider_input_reference
-> exact ProviderInputAppendCommittedEvent
-> committed provider vector/root + replay bindings
-> canonical provider payload
```

Audit artifact 缺失不能让上述 provider payload authority 失效。Hard cut 后必须把 provider payload replay 与 compiler audit resolution 物理拆成两个 API。

### 2.6 现有 transcript GC 不能回收任意 audit pages

`runtime/authority_materialization/transcript_gc.py` 当前只会：

- 读取 full v8 manifest；
- 找到 manifest 保护的 transcript checkpoint root；
- 从 checkpoint materialization reachability 产生删除候选。

通用 `ArtifactStore` 没有按 artifact kind 列举对象的 API。若直接采用“写 pages，最后写 root”，root 写失败后 pages 没有可发现的 durable index，所谓“现有 artifact GC 自动回收 orphan pages”并不成立。

### 2.7 共享 ContextInputIoService 不能让 audit 抢占 critical 配额

`ContextInputIoService` 当前默认只有 8 个 pending slots，并被 Context Input、provider recovery、checkpoint、terminal projection、prompt queue、memory governance 等多条 required path 共用。若 best-effort audit 直接调用现有 `start_owned()`，它可以占满 slots，使下一次 required context read 以 `PendingContextInputIoError` 失败，重新制造 audit 对 live admission 的反向控制。

---

## 3. 冻结目标

### 3.1 小型 dispatch commit 取代 artifact-dependent authority

- `ContextCompiledEvent` 只保存 model dispatch、durable preparation installation、预算和恢复真正需要的 bounded facts。
- 不再把完整 transcript、snapshot、projection、render detail 或 provider candidate复制进该事件。
- ProviderInput 全链只 join compact commit、ordered projection identity 和 preparation installation。

### 3.2 Audit materialization 完全退出 model semantic admission

- Provider semantic validation完成后即可提交 `ContextCompiledEvent`。
- page/plan/root 写入失败、超时、缺失或内容冲突不产生 `RunError`，不设置 Runtime ledger reconciliation latch，也不改变 ProviderInput/ModelStart 的 commit classification。
- `ContextCompiledEvent` 只记录 deterministic audit expectation，不声明 artifact 已经存在。

### 3.3 Provider token budget 是唯一 provider 内容预算 authority

“唯一”只针对 provider-visible token 内容窗口，不删除 provider-input 或 Long-Horizon 已有的结构/物理安全上限。

必须继续保留当前闭合约束，例如：

- ordered projection 最多 16,384 units；
- ordered projection canonical bytes 最多 16 MiB；
- append 最多 512 units；
- append candidate canonical bytes 最多 4 MiB；
- vector tree、changed-node、artifact codec 和 EventLog payload bounds。

这些约束失败属于 typed provider physical-policy/configuration failure，不能伪装成 token pressure，也不能由 audit paging 绕过。

当前 Agent 还把 `LongHorizonContextAllocationPolicyFact.max_projection_units_per_window` 作为独立的结构性 window-maintenance trigger。该规则属于 Long-Horizon 现有语义，本次必须保留：

```text
provider token/target pressure
or existing Long-Horizon projection-unit maintenance trigger
  -> may request window compaction

audit bytes/page count/write state
  -> never requests compaction
```

本次不借机把 unit-count trigger改写成token判断，也不扩大它的适用范围。

### 3.4 Audit resolution 是 optional closed outcome

```text
exact_artifact
reconstructed
unavailable
integrity_or_contract_mismatch
```

后两项只影响 Inspector、doctor 和显式 audit 命令。它们不能反向否定已确认的 ModelStart、tool execution、assistant reply 或 RunEnd。

### 3.5 Root-last 必须同时具备可发现的 incomplete plan

- expectation 只冻结 stable semantic key、plan ID 和 root ID；不预计算 page layout。
- worker 先持久化完整 materialization plan，再写 plan-scoped immutable pages，最后写 completion root。
- root 存在表示该 worker曾确认 plan及全部 pages；读取时仍必须重新验证每个引用。
- root 缺失时，GC 可通过 plan exact 枚举并清理半成品 pages。

---

## 4. 明确非目标

本次不做：

- 不增加新的通用 projection framework；
- 不增加 Context Input audit durable job 表；
- 不增加 manifest retry/reconciliation event；
- 不改变 canonical transcript acceptance、suppression、pairing；
- 不改变 provider token estimator；
- 不改变 Long-Horizon summary 语义；
- 不改变 tool-result 截断/rollup 策略；
- 不承诺 audit artifact exactly-once；
- 不保留 v8 full-manifest historical decoder；
- 不通过单纯提高 1 MiB 上限解决问题；
- 不让 globally deduplicated page 在没有 durable reference index 的情况下被 GC；
- 不把 storage-only audit facts 放入 AgentEvent。

---

## 5. 最终 ownership 与 DTO

本节所有 `FrozenFactBase` DTO必须注册唯一schema version、domain separator和own fingerprint field；所有`FrozenStorageFactBase` DTO使用独立storage registry。Assignment union只允许final owner定义，其他模块不得复制class或保留legacy re-export。

Final type owners：

```text
primitives/context_input_commit.py
  ContextCompile projection-base references
  ContextCompileSourceReferenceSetFact
  ContextCompileInputCommitFact
  ContextInputAuditExpectationFact

primitives/provider_input.py
  ProviderInputPreparationInstallFact
  provider preparation/append/reference carriers

primitives/context_input_audit_storage.py
  storage-only plan/page/root vocabulary
```

`provider_input.py`只保存semantic commit fingerprint，不反向import完整commit class；`event/events.py`负责组合两边。禁止通过forward-ref重建新的`context <-> provider_input` import SCC。

### 5.1 Source authority reference set

新增 compact `ContextCompileSourceReferenceSetFact`。它只保存 bounded durable references与reconstruction attribution：

```python
class ContextCompileSourceReferenceSetFact(FrozenFactBase):
    schema_version: Literal["context_compile_authority_reference_set.v1"]
    run_start_event_reference: ContextEventReferenceFact
    continuation_event_reference: ContextEventReferenceFact | None
    primary_ledger_horizon: LedgerAuthorityHorizonFact
    authority_horizon_set_reference: LedgerAuthorityHorizonSetReferenceFact
    transcript_projection_base_reference: (
        RunSeedProjectionBaseReferenceFact
        | CheckpointProjectionBaseReferenceFact
    )
    reference_set_fingerprint: Fingerprint
```

`transcript_projection_base_reference` 是新增的 compact discriminated union：

- run-seed branch exact引用现有 `RunTranscriptSeedReferenceFact`、stable semantic state fingerprint 和 source base fingerprint；
- checkpoint branch exact引用现有 checkpoint root reference、checkpoint through sequence、stable semantic state fingerprint 和 source base fingerprint；
- 不复制 `ProjectionBaseCommonFact.stable_semantic_state`、provider projection正文或 transcript delta正文。

该 reference set 是bounded audit reconstruction的精确attribution，不是checkpoint artifact的retention owner。否则每个历史compile都会永久pin住其旧checkpoint，使reachability GC失效。Checkpoint物理保留权继续只属于active/fallback checkpoint policy、run seed或其他现有canonical owner；被GC回收后，该历史audit可以降级为reconstructed/unavailable。

### 5.2 Context compile dispatch commit

新增：

```python
class ContextCompileInputCommitFact(FrozenFactBase):
    schema_version: Literal["context_compile_input_commit.v1"]
    runtime_session_id: str
    run_id: str
    context_id: str
    resolved_model_call_id: str
    resolved_model_target_fingerprint: Fingerprint
    model_call_index: int
    compile_attempt_index: int
    context_retry_index: int
    source_through_sequence: int

    source_references: (
        ContextCompileSourceReferenceSetFact
    )
    snapshot_semantic_fingerprint: Fingerprint
    ordered_projection_identity: (
        ProviderOrderedTranscriptProjectionIdentityFact
    )
    prepared_provider_input_plan_fingerprint: Fingerprint
    canonical_provider_input_plan_fingerprint: Fingerprint
    provider_neutral_payload_fingerprint: Fingerprint
    input_aggregate_fingerprint: Fingerprint
    canonical_render_decisions_fingerprint: Fingerprint

    token_estimator_fingerprint: Fingerprint
    input_budget_tokens: int
    final_payload_estimated_tokens: int
    budget_decision_fingerprint: Fingerprint
    commit_fingerprint: Fingerprint
```

规则：

- `commit_fingerprint` 覆盖所有上述字段。
- 它不覆盖 audit artifact ID、page layout、write outcome、operation ID、deadline 或 diagnostic。
- `source_through_sequence` 必须与 primary ledger horizon、authority set 和 snapshot semantic source exact join；sequence 单独不能作为 ledger identity。
- budget fingerprint 使用中央 factory，覆盖 target、estimator、input budget 和 final estimate。
- commit canonical JSON 最大 64 KiB；超过即实现/contract错误，不得降级为 audit paging。

### 5.3 Durable preparation installation

新增 compact `ProviderInputPreparationInstallFact`，替代 `ContextCompiledEvent.prepared_provider_input`：

```python
class ProviderInputPreparationInstallFact(FrozenFactBase):
    schema_version: Literal["provider_input_preparation_install.v2"]
    semantic_commit_fingerprint: Fingerprint
    preparation_ownership: ProviderInputPreparationOwnershipFact
    prepared_candidate_fingerprint: Fingerprint
    prepared_plan_fingerprint: Fingerprint
    canonical_provider_input_plan_fingerprint: Fingerprint
    ordered_projection_identity_fingerprint: Fingerprint
    generation_commit_guard: ProviderInputGenerationCommitGuardFact
    rollover_request_fingerprint: Fingerprint | None
    install_fingerprint: Fingerprint
```

唯一 owner 规则：

- process-local `PreparedProviderInputAppendCandidateFact` 继续拥有完整 physical candidate，直到 Start batch FULL/NONE/UNKNOWN settlement；
- `ContextCompiledEvent` 只持有 installation fact；
- live committed reducer 将 installation 与 staged candidate exact join；
- restore/recovery 只从 installation 恢复 durable preparation attribution和 abandonment 所需 guard，不重建完整 candidate；
- `ProviderInputPreparationOwnershipAttributionFact` 保存 `semantic_commit_fingerprint` 与 ordered projection identity，不再保存 manifest artifact reference；
- installation与attribution都保存canonical provider plan fingerprint，并与staged candidate、semantic commit、最终`CommittedProviderInputReferenceFact` exact join；
- `PreparedProviderInputAppendCandidateFact`和`ProviderInputRolloverRequestFact`保存semantic commit fingerprint及ordered projection identity，不保存audit expectation/root ID；
- `ProviderInputAppendCommittedEvent`和`CommittedProviderInputReferenceFact`保存同一semantic commit fingerprint；
- `candidate_kind/reference_kind/append_kind` 的 production vocabulary 从 `compiled_manifest` 原子改为 `compiled_context`。

### 5.4 ContextCompiledEvent 的 bounded compiled branch

新的 compiled branch只允许：

```text
resolved_call
budget
semantic_commit
provider_input_preparation_install
audit_expectation
必要的 compact Long-Horizon decision/shadow
```

以下旧字段从 durable compiled branch物理删除，转入 optional audit materialization：

```text
sections
tool_specs
diagnostics
lifecycle_decisions
tool_result_render_decisions
tool_result_budget_report
tool_result_render_decision_facts
tool_result_render_operational_facts
prepared_provider_input
manifest_projection_reference
```

`ContextCompiledEvent`所有branch的candidate canonical payload最大 256 KiB。Failure/pressure branch保留自身 typed failure authority，但其component fingerprint集合也必须bounded，并删除所有 manifest candidate/write outcome字段和 `INPUT_MANIFEST_WRITE` failure stage。

该上限由唯一ContextCompiled candidate factory预检，并由`freeze_event_write_candidate()`形成的actual canonical payload bytes二次验证；不得只按Python字段长度估算。

Exact join矩阵：

```text
ContextCompiled.outer identity/resolved_call/budget
  == semantic_commit identity/target/budget

ContextCompiled.preparation_install.semantic_commit_fingerprint
  == semantic_commit.commit_fingerprint

ProviderInputAppend.semantic_commit_fingerprint
  == consumed preparation attribution semantic_commit_fingerprint
  == prepared candidate semantic_commit_fingerprint

CommittedProviderInputReference.semantic_commit_fingerprint
  == ProviderInputAppend.semantic_commit_fingerprint

ModelStart.provider_input_reference
  -> exact append event identity + semantic commit fingerprint
```

任一不等只能由现有event commit/recovery owner判为conflict/reconciliation；不得回读audit来“修正”winner。

Preparation identity同样无环：`semantic commit -> prepared candidate -> preparation install -> ContextCompiled event`。Prepared candidate不得包含install fingerprint或ContextCompiled payload fingerprint。

### 5.5 Audit expectation 不预计算 page layout

新增：

```python
class ContextInputAuditExpectationFact(FrozenFactBase):
    schema_version: Literal["context_input_audit_expectation.v1"]
    semantic_commit_fingerprint: Fingerprint
    audit_contract_id: str
    audit_contract_version: str
    audit_contract_fingerprint: Fingerprint
    materialization_key: Fingerprint
    expected_plan_artifact_id: str
    expected_root_artifact_id: str
    expected_root_semantic_fingerprint: Fingerprint
    expectation_fingerprint: Fingerprint
```

唯一推导：

```text
materialization_key = H(
  semantic_commit_fingerprint,
  audit_contract_id/version/fingerprint
)

plan_artifact_id = "context-input-audit-plan:" + H(materialization_key)
root_artifact_id = "context-input-audit-root:" + H(materialization_key)
root_semantic_fingerprint = H(semantic_commit_fingerprint, audit_contract_fingerprint)
```

Expectation factory禁止读取 ArtifactStore、序列化 full audit source、划分页或计算 page digest。否则会把第二上下文窗口的 CPU/内存工作重新放回 critical path。

`audit_contract_fingerprint`必须覆盖closed component-kind registry、每个extractor ID/version/fingerprint、canonical JSON codec、排序规则、page partition算法、media types以及5.6/6.5的物理上限。任何一项变化都必须推进contract version，从而得到新的materialization key；不得在同一key下生成不同plan。

### 5.6 Storage-only plan、page 与 root

以下 DTO 全部继承并注册 `FrozenStorageFactBase`，位于独立的 storage vocabulary；EventLog writer在类型层不得接受它们：

```python
class ContextInputAuditPageFact(FrozenStorageFactBase): ...
class ContextInputAuditMaterializationPlanFact(FrozenStorageFactBase): ...
class ContextInputAuditRootFact(FrozenStorageFactBase): ...
```

Page 必须保存：

- source runtime-session ID；
- materialization key；
- zero-based page ordinal；
- component kind与entry range；
- closed typed payload；
- canonical payload SHA-256与bytes；
- page storage fingerprint。

256 KiB上限覆盖最终stored `ContextInputAuditPageFact`的完整canonical JSON，而不只是nested payload。Partition contract必须预留schema、identity、fingerprint和JSON framing bytes；最终codec超界时整次materialization停止且不写plan。

Page artifact ID使用：

```text
H(materialization_key, page_ordinal, payload_sha256, audit_contract_fingerprint)
```

因此 page 是 immutable、content-verified、plan-scoped 的单一 owner，不在不同 expectation 之间共享。没有 durable reference-count table时，不使用全局 content-addressed page ID。

Plan 必须保存：

- source runtime-session/run/context/resolved-call identity；
- expectation/semantic commit/materialization key；
- expected root ID与root semantic fingerprint；
- ordered canonical component references；
- ordered page references；
- page/component counts、bytes和accumulators；
- plan fingerprint。

Root 必须保存：

- source runtime-session/run/context/resolved-call identity；
- semantic commit/materialization key；
- exact plan artifact reference；
- component/page counts与accumulators；
- materialization contract fingerprint；
- root semantic fingerprint；
- root materialization fingerprint。

Root不重复完整 plan 或 page正文。

Typed repository必须把artifact namespace的`session_id/run_id`与storage fact中的source identity exact join，禁止跨session用同一ID读取或写入。

Fingerprint DAG必须保持无环：

```text
semantic commit + audit contract
  -> materialization key
  -> stable plan/root IDs

page payloads
  -> page references
  -> plan content fingerprint
  -> plan artifact reference
  -> root materialization fingerprint
```

Plan只保存stable root ID/semantic fingerprint，不保存root materialization fingerprint；root单向引用plan artifact reference。

### 5.7 Typed audit artifact repository

在通用 `ArtifactStore` 上增加窄 adapter `ContextInputAuditArtifactRepository`：

- 只接受注册过的 plan/page/root storage fact；
- 统一 canonical JSON codec、media type、semantic metadata和identity verification；
- 返回 exact artifact reference，包含 ID、content SHA-256、bytes、media type、storage fact fingerprint 和 semantic metadata fingerprint；
- same ID + identical canonical bytes = compatible；
- same ID + different bytes/metadata = typed conflict，仅记录 audit diagnostic；
- codec沿用secret sink guard，拒绝MCP continuation/request secret、sealed response或其他storage-only secret carrier进入audit payload；
- 禁止 caller通过普通 dict自行构造 audit artifact。

Privileged GC使用单独的`ContextInputAuditMaintenanceRepository` protocol，在上述typed read/codec之上额外要求现有identity-based `delete_if_identity`；live Agent不取得delete capability。

本次不扩张通用 ArtifactStore schema或关系表。

### 5.8 Maintenance policy

新增process-local frozen `ResolvedContextInputAuditMaintenancePolicy`，不进入semantic commit：

```text
incomplete_plan_retention_seconds = 86,400  # from ArtifactRecord.stored_at
catalog_page_max_events = 32
catalog_page_max_payload_bytes = 8 MiB
maximum_delete_candidates_per_invocation = 4,096
completed_root_retention = retained
```

一次GC达到删除上限时返回typed continuation high-water，由下一次显式maintenance调用继续；不得为了完成全库扫描无界持有event或page reference集合。本次hard cut不回收valid completed roots，其长期retention另立规格。

32-event page与compiled `ContextCompiledEvent <= 256 KiB`共同给出8 MiB canonical payload上限；每页读取后仍重算实际payload bytes。若单event或page超界，GC fail closed，不能截断后继续删除。

---

## 6. 写入与提交算法

### 6.1 Model critical path

```text
build snapshot/projection/provider planning bundle in memory
-> validate provider semantic and physical joins
-> compute final provider token estimate
-> enforce provider token/target budget
-> build ContextCompileInputCommitFact
-> build ContextInputAuditExpectationFact      # O(1), no page layout
-> finalize process-local ProviderInput candidate using semantic commit
-> build ProviderInputPreparationInstallFact
-> commit compact ContextCompiledEvent
     FULL    -> durable preparation installed
     NONE    -> retry same event candidate
     UNKNOWN -> existing Runtime event reconciliation owner
-> prepare lifecycle Start bundle
-> atomically commit ProviderInput companion batch + ModelStart
-> dispatch provider only after Start batch FULL
-> on the exact committed ModelStart receipt, offer audit source to
   BEST_EFFORT_AUDIT lane; never await physical result
```

AuditStore不能出现在 ContextCompiled、ProviderInput append 或 ModelStart 的 required result中。Provider-input vector/root artifacts仍是 model replay authority，继续按现有 contract在 Start前required confirmation；本规格只移除 context-audit artifact依赖。

Audit不得在Start batch FULL前引用provider vector/root为committed authority。把offer绑定到exact committed ModelStart receipt还能避免为最终被abandon的preparation物化审计。Provider dispatch可以已经开始；offer不是send barrier。

### 6.2 Process-local lazy audit capture

Model critical path只冻结`PreparedContextInputAuditSourceCapture`：semantic commit、expectation与一个process-local immutable-input capture callable。它不得构造完整audit object graph、canonical encode source、计算count-based quote或执行page layout。Start FULL后，唯一factory将capture与exact stored Start/append references绑定；`ContextInputIoService`必须先原子取得固定最坏情况32 MiB process permit和session permit，worker才可调用capture。

worker在permit内构造`PreparedContextInputAuditSourceBasis`并执行唯一bounded canonical encoder。Encoder在分配完整escaped payload前检查单个scalar，并在streaming encode过程中累计最多16 MiB；oversize、source extraction异常、codec异常、capacity不足或deadline到期全部只形成process-local typed skip。它们不得进入`ContextInputPreparationError`、failed `ContextCompiledEvent`、`RunErrorEvent`、RunEnd或ledger reconciliation。

最终materialization carrier只持有：

- semantic commit与expectation；
- matching committed ModelStart和ProviderInput append references；
- existing authority reference facts；
- bounded invocation-only typed audit detail；
- materialization policy/contract identity。

它不得保存 mutable `AgentEvent`、raw archive connection、RuntimeSession或完整v8 manifest。Production不得用item count、浅层`getsizeof()`或并不存在的per-item 8 KiB上限估算resident charge。单个operation固定charge 32 MiB，对应最多16 MiB canonical audit source；低于该上限的eager quote helper仅供deterministic test/offline caller使用，也必须接受worker的最终bounded encode判定。

Provider开始stream后，worker只能读取该递归不可变carrier和typed repository，不能回读正在变化的LoopState、RunOwner或process-local caches。

### 6.3 ContextInputIoService 的低优先级 lane

扩展现有 owner，而不是新建 audit writer service：

```text
ContextInputIoLane.CRITICAL
ContextInputIoLane.BEST_EFFORT_AUDIT
```

冻结规则：

- 新增owner-loop-only的`offer_best_effort_nowait(...) -> AuditOfferDisposition`；它不等待async lock、executor slot或ArtifactStore；
- `BEST_EFFORT_AUDIT` 每个 RuntimeSession最多1个 queued/running operation；
- 使用process-owned `best_effort_audit_executor`，固定2个workers、8个全局queued/running permits和64 MiB全局resident quote上限；
- submission必须先通过non-blocking process count+bytes permit CAS，再通过session-local 1-operation CAS；任一失败立即skip；
- audit executor不复用当前12-worker `auxiliary_io_executor`，因此不占用8个session critical slots或process auxiliary workers；
- lane满、executor拒绝或source超界时，`offer_best_effort_nowait`立即返回typed skipped disposition；
- process permit与session permit必须先于source capture/object-graph构造取得；capture callable不得在caller/event-loop线程提前求值；
- operation由`ContextInputIoService`持有，caller cancellation只detach；
- physical completion callback在移除session operation的同一owner-loop settlement中释放process permit；submit失败也必须同步释放；
- completion observer只接收secret-safe reason code和计数，不接收正文或exception message；
- Host close沿用同一个`context_input_io_service.drain_pending()`，不增加close phase；
- close deadline到期可报告physical close-blocked，但不能修改RunEnd或ledger reconciliation状态。

### 6.4 Audit materialization physical order

一个service-owned worker完成整个操作：

```text
deterministically classify references vs page-owned detail
-> build bounded pages and page references in memory
-> build materialization plan
-> put/confirm plan FIRST
-> for each ordered page:
     put/confirm exact immutable page
-> exact-read/verify all referenced pages
-> build root from confirmed plan reference and accumulators
-> put/confirm root LAST
-> publish process-local redacted terminal diagnostic
```

规则：

- plan写入失败时禁止开始任何page写入；
- 每个ArtifactStore调用接收同一absolute deadline；deadline后禁止启动新调用；
- root仅在全部page exact confirmation后形成；
- root写入失败保留plan/pages，交给offline GC；
- 不做live retry，不创建stable writer状态机，不做NONE/UNKNOWN durable classification；
- process crash后不会自动重放该operation；显式doctor可以从canonical authority重新构造同一deterministic plan；
- root/page conflict不设置Runtime ledger latch。

### 6.5 固定物理上限

```text
maximum component references        256
maximum audit expectation bytes        8 KiB
maximum inline-small-fact bytes       8 KiB/item
maximum total inline bytes            64 KiB
maximum pages                         64
maximum page canonical bytes         256 KiB
maximum total page canonical bytes    16 MiB
maximum single source resident charge 32 MiB
maximum plan canonical bytes         128 KiB
maximum root canonical bytes          64 KiB
maximum audit operation deadline      30 s
maximum pending audit operations       1/session
maximum process audit workers           2
maximum process audit operations        8
maximum process audit resident quote   64 MiB
```

达到任何audit上限只产生`skipped_physical_bound`或`unavailable`，不得触发compaction或model failure。

### 6.6 为什么不需要 completion event

读取方通过 expectation定位：

```text
root + plan + pages exact validate
  -> ExactAuditArtifact

root absent/invalid
  -> bounded canonical reconstruction
       -> ReconstructedAudit
       -> AuditUnavailable / AuditIntegrityFailure
```

Plan存在而root缺失只表示incomplete materialization，不是新的业务状态。因此不新增：

- AuditPreparedEvent；
- AuditCompletedEvent；
- AuditFailedEvent；
- AuditRecoveryService；
- AuditReceiptProjection。

---

## 7. Replay、Inspector 与 GC

### 7.1 Provider payload replay 是 required authority

新增或重命名 required API：

```python
load_committed_provider_payload_for_model_start(
    model_start_reference,
    event_log,
    provider_input_store,
    artifact_store,
) -> ExactCommittedProviderPayload
```

它只能沿：

```text
ModelStart
-> exact ProviderInputAppendCommittedEvent
-> CommittedProviderInputReferenceFact
-> provider vector/root + replay binding set
```

重建provider-visible payload。它不读取 audit expectation、plan、page或root。Model-stream recovery、resume和doctor的required provider proof都消费该API。

### 7.2 Compiler audit resolution 是 optional authority

```python
ContextInputAuditLoadOutcome = (
    ExactAuditArtifact
    | ReconstructedAudit
    | AuditUnavailable
    | AuditIntegrityFailure
)
```

- `ExactAuditArtifact`：root、plan、pages、semantic commit以及每个canonical component reference全部exact join；只验证reference identity，不让audit覆盖component owner的内容。
- `ReconstructedAudit`：从EventLog、transcript seed/checkpoint、provider vector与既有artifacts bounded重建；必须携带 reconstructed component kinds、omitted component kinds和proof fingerprint。
- `AuditUnavailable`：缺失或bounded read上限不足。
- `AuditIntegrityFailure`：root/plan/page content、schema或contract不匹配；若canonical reconstruction成功，返回`ReconstructedAudit`并附redacted artifact diagnostic。

`ReconstructedAudit`只承诺其声明的reconstructable semantic view与exact artifact等价，不承诺恢复invocation-only timing/diagnostic或原v8对象的byte identity。

若`ContextCompiledEvent`已FULL但matching Start batch从未FULL，loader返回`AuditUnavailable(reason="model_start_not_committed")`；这属于预期的abandoned preparation，不应显示为artifact writer failure。

只有显式`--require-exact-audit`命令要求`ExactAuditArtifact`。任何live run/recovery path不得调用该flag。

### 7.3 Canonical authority优先级

```text
1. EventLog中的ContextCompiled semantic commit/preparation install
2. ModelStart + ProviderInput append + persistent vector/root
3. transcript seed/checkpoint + bounded semantic delta
4. tool-result/context-source canonical artifacts
5. optional audit root/plan/pages
```

Audit artifact不得覆盖前四项；发现冲突时只把audit标记为untrusted。

### 7.4 GC ownership

拆成两个明确职责：

1. transcript checkpoint GC：
   - 不再加载full manifest；
   - 删除`_manifest_protected_checkpoint_roots()`及“ContextCompiled自动pin checkpoint”的旧规则；
   - 只按run seed、active/retained fallback checkpoint和其他现有canonical retention owner计算保护集；
   - `ContextCompileSourceReferenceSetFact`只用于audit reconstruction，不进入deletion protection set；
   - audit root缺失或历史base已被合法GC时，audit resolution降级，不能阻止maintenance。

2. context audit incomplete GC：
   - 只在durable session closed、run owner drained并取得现有checkpoint-maintenance exclusive authority后运行；
   - 按`ContextCompiledEvent.audit_expectation`调用现有`read_raw_events_by_type(limit=32, through_sequence=...)`做bounded backward paging，不使用固定10,000 events作为完整性假设；
   - plan缺失：无page可删除；
   - root存在且valid：保留plan/pages；
   - root缺失且plan超过24小时retention：按plan中的exact plan-scoped page references执行`delete_if_identity`，再删除plan；
   - root/page identity不匹配：fail closed并报告doctor diagnostic；
   - audit GC永远不得删除component references指向的provider/transcript/tool-result canonical artifacts。

因为page是plan-scoped，删除incomplete plan不需要全局reference-count table。禁止宣称现有transcript GC会自动发现任意artifact page；该调用面必须显式实现。

Valid completed root、plan和page在本规格中持续保留；GC不得自行采用“保留最近N个”之类未冻结策略。

---

## 8. Compaction 与预算规则

删除：

- `ContextCandidateCollectionPolicyFact.max_input_manifest_chars`；
- `build_context_input_manifest_candidate()`的flat canonical bytes hard reject；
- manifest `candidate_materialization/input_manifest_write` failure；
- manifest/page count触发Long-Horizon compaction的任何分支。

允许请求window compaction的现有closed判断：

```text
LongHorizonContextBudgetDecisionFact.decision == "window_compaction_required"
  # derived from provider-visible token estimate and the frozen target/window policy
or
LongHorizonContextBudgetDecisionFact.unit_count_limit_exceeded == true
```

第一项允许沿用现有低于hard budget的proactive token trigger ratio；本规格不把它收窄为“只有实际超预算才compact”。第二项是结构维护，不是第二套provider token budget。其他Provider-input structural bounds继续fail closed，并分类为`provider_input_physical_policy_unsatisfied`，不得自动请求compaction。Audit storage的结果只能是：

```text
materialized
skipped_capacity
skipped_physical_bound
failed_operationally
unavailable_on_read
```

它不能成为：

- model_error；
- context_budget pressure；
- Runtime ledger reconciliation；
- RunEnd failure原因。

Long-Horizon自己的compaction input manifest不在本次删除范围内；architecture guard必须限定为Context Compile manifest，不能误删另一领域carrier。

`RuntimeSession.latch_context_input_reconciliation_required()`也不能整项删除：`runtime/long_horizon/checkpoint_store.py`仍用它保护required checkpoint consistency。本次只删除Context Compile audit writer对该latch的调用，并用AST/callsite guard证明audit模块无法取得该capability。

---

## 9. Schema hard cut

### 9.1 删除

- `ContextCompileInputManifestFact` v8 flat carrier；
- `ContextCompileInputAuditFact` artifact-FULL carrier；
- `ContextInputManifestProjectionReferenceFact`；
- `ContextInputManifestWriteService`及attempt/physical state DTO；
- manifest write FULL/ABSENT/CONFLICT/UNKNOWN对model admission的分支；
- `max_input_manifest_chars` production policy；
- `context_input_manifest_write_failed` RunError路径；
- `ContextInputFailureReasonCode.MANIFEST_*`、`ContextCompileFailureStage.INPUT_MANIFEST_WRITE`和仅由flat manifest candidate使用的`CANDIDATE_MATERIALIZATION`；
- compiled `ContextCompiledEvent`中的unbounded audit/display字段；
- ProviderInput production vocabulary `compiled_manifest`；
- `max_projection_units_per_manifest`、`max_projection_canonical_bytes_per_manifest`和`context_manifest_physical_policy_fingerprint`这些已不再准确的provider physical-policy字段名。

### 9.2 新增或替换

- `ContextCompileSourceReferenceSetFact`；
- `ContextCompileInputCommitFact`；
- `ProviderInputPreparationInstallFact`；
- `ContextInputAuditExpectationFact`；
- storage-only plan/page/root facts与typed repository；
- provider committed reference/append/recovery的`semantic_commit_fingerprint` join；
- `max_ordered_projection_units`、`max_ordered_projection_canonical_bytes`和`ordered_projection_physical_policy_fingerprint`；
- required provider-payload replay API；
- optional audit load outcome；
- `ContextInputIoLane.BEST_EFFORT_AUDIT`。

### 9.3 Reset与版本

当前代码真值：

- `AGENT_EVENT_SCHEMA_VERSION = 9`；
- PostgreSQL migration head为`0012_terminal_active_queue_projection.sql`。

原子cut时：

- event schema generation `9 -> 10`；
- ContextCompiled、ProviderInputAppend及其nested durable facts使用新schema/golden；
- PostgreSQL/Oxigraph reset；
- migration head仍为0012，本次不新增relation；
- 不迁移v8 manifest artifacts；
- 不保留dual decoder、dual writer或旧reference re-export。

Post-review authority收紧又改变了`ContextCompiledEvent`的nested durable schema：

- `ProviderInputPreparationInstallFact v1 -> v2`；
- `ProviderInputPreparationOwnershipAttributionFact v3 -> v4`；
- 新增canonical provider plan exact join；
- event schema generation在同一未合入hard-cut world中继续单调推进`10 -> 11`。

因此最终合入/reset world的代码真值是generation 11；generation 10只表示最初MP2落地、
不再是可部署目标。PostgreSQL/Oxigraph仍必须在最终generation 11代码下重新reset，不能复用
先前按generation 10建立的event world。

若MP2开始前event schema generation已被其他工作推进，实施者必须从当时latest generation单调+1，并同步本节与machine evidence；不得硬编码冲突版本。

---

## 10. 实施阶段

### MP0：Authority inventory与additive final types

目标：不改生产行为，冻结最终owner和删除地图。

工作：

- 冻结本次1,065,543-byte事故的sanitized deterministic fixture；
- 将v8每个顶层字段分类为：required durable authority、existing authority reference、optional audit detail、process-only operational；
- 记录`ContextCompiledEvent`旧unbounded字段的consumer及最终替代；
- 证明provider vector/root可以独立恢复provider-visible payload；
- 冻结commit/install/expectation和storage DTO golden；
- 新types只additive存在，不接production binding。

Gate：

- fixture provider estimate仍约56k tokens且旧manifest超过1 MiB；
- inventory不存在unknown owner；
- provider payload replay proof不读取manifest；
- production行为和event schema generation不变。

### MP1：Dormant audit materialization与I/O lane

目标：先实现不会影响live path的完整optional plane，但不激活新event schema。

工作：

- 实现typed audit repository、plan/page/root factory与hydrator；
- 实现plan-first/page/root-last算法；
- 为`ContextInputIoService`增加隔离的BEST_EFFORT_AUDIT lane；
- 实现optional audit closed outcome和incomplete GC dry-run/fixture；
- 实现provider payload required replay API；
- 不从Agent生产路径调用audit materializer；
- 不写v8/new audit双份production artifact。

Gate：

- critical lane饱和不影响audit disposition，audit lane饱和不影响critical admission；
- root/plan/page tamper与incomplete cleanup通过；
- no production binding、no event schema change、no database reset。

### MP2：不可拆分的production hard cut

目标：一次切换semantic/event/provider/recovery/GC owner。

同一PR完成：

- `ContextCompiledEvent` compact schema；
- semantic commit与preparation installation；
- Agent删除full manifest build/write等待；
- Provider planner/coordinator/store/recovery/model-stream recovery切到`compiled_context`；
- ProviderInput append + ModelStart exact join semantic commit；
- 激活best-effort audit offer；
- transcript GC删除historical ContextCompiled retention dependency；
- 激活incomplete audit GC；
- 删除production `ContextInputManifestWriteService`、v8 DTO和旧failure path；
- event schema generation递增并执行PostgreSQL/Oxigraph reset。

Gate：

- 每个event commit仍按FULL/NONE/UNKNOWN走现有Runtime owner；
- audit拒绝所有写入时text-only/tool-loop均完成；
- crash-after-ContextCompiled-before-ModelStart能从compact installation安全abandon；
- ModelStart recovery不读取audit；
- 无old/new dual write或compat shim。

### MP3：Inspector、doctor、contracts与dogfood

目标：完成可观察性和最终清理，不改变MP2 durable topology。

工作：

- Inspector展示exact/reconstructed/unavailable/integrity状态；
- 显式doctor支持`--require-exact-audit`与incomplete cleanup；
- 删除剩余v8 test/support factory和旧Inspector assumptions；
- 更新长期contracts、architecture guards、事故回执；
- 运行全量non-real与real TUI dogfood。

Gate：

- exact artifact与reconstructed outcome在其声明的reconstructable view上等价；
- omitted invocation-only detail明确可见，不伪装exact；
- root/pages全缺失不影响live run、resume或provider replay；
- 旧symbol/string production命中为零。

---

## 11. 文件修改面

### 11.1 Durable/event/provider surface

- `src/pulsara_agent/primitives/context.py`
- `src/pulsara_agent/primitives/context_input_commit.py`（新增）
- `src/pulsara_agent/primitives/provider_input.py`
- `src/pulsara_agent/primitives/context_input_audit_storage.py`（新增）
- `src/pulsara_agent/primitives/__init__.py`
- `src/pulsara_agent/event/events.py`
- `src/pulsara_agent/event/__init__.py`
- `src/pulsara_agent/event_log/serialization.py`

### 11.2 Runtime production surface

- `src/pulsara_agent/runtime/agent.py`
- `src/pulsara_agent/runtime/context_input/manifest.py`（删除/拆分）
- `src/pulsara_agent/runtime/context_input/commit.py`（新增）
- `src/pulsara_agent/runtime/context_input/audit_storage.py`（新增）
- `src/pulsara_agent/runtime/context_input/audit_materializer.py`（新增）
- `src/pulsara_agent/runtime/context_input/replay.py`
- `src/pulsara_agent/runtime/context_input/snapshot.py`
- `src/pulsara_agent/runtime/context_input/policy.py`
- `src/pulsara_agent/runtime/context_input/io_service.py`
- `src/pulsara_agent/blocking_executor.py`
- `src/pulsara_agent/runtime/context_input/__init__.py`
- `src/pulsara_agent/runtime/provider_input/planner.py`
- `src/pulsara_agent/runtime/provider_input/causal.py`
- `src/pulsara_agent/runtime/provider_input/coordinator.py`
- `src/pulsara_agent/runtime/provider_input/store.py`
- `src/pulsara_agent/runtime/provider_input/recovery.py`
- `src/pulsara_agent/runtime/model_stream_recovery.py`
- `src/pulsara_agent/runtime/session.py`
- `src/pulsara_agent/runtime/session_run_capabilities.py`
- `src/pulsara_agent/runtime/authority_materialization/transcript_gc.py`
- `src/pulsara_agent/host/session.py`
- `src/pulsara_agent/inspector/service.py`

### 11.3 Tests/support

- `tests/test_context_input_manifest.py`（重命名为commit/audit contract tests）
- `tests/test_agent_runtime_loop.py`
- `tests/test_provider_input_hard_cut.py`
- `tests/test_context_snapshot_builder.py`
- `tests/test_inspector.py`
- `tests/test_subagent_runtime.py`
- `tests/test_runtime_event_architecture.py`
- `tests/test_durable_projection_architecture.py`
- `tests/test_authority_materialization_contract.py`
- `tests/support/model_call.py`
- 新增manifest subtraction、storage-only sink和I/O lane architecture guards。

### 11.4 Long-term contract

- `contracts/INSPECTOR_PROJECTION_CONTRACT.zh.md`

该contract必须把“exact provider payload replay”与“optional compiler audit projection”拆成两个独立状态，不再把manifest artifact presence解释成历史ModelStart可信度。

---

## 12. 测试矩阵

### 12.1 Model happy path

- text-only compile -> compact ContextCompiled FULL -> Start batch FULL -> final reply；
- tool result后follow-up compile -> Start -> final reply；
- initial generation、existing append、rollover三类preparation；
- crash-after-ContextCompiled-before-Start -> compact recovery abandonment；
- Start batch UNKNOWN -> existing model/event reconciliation，不读取audit；
- resume从ModelStart/provider vector重建exact payload。

### 12.2 Incident与budget

- 1.1 MiB、4 MiB、16 MiB optional audit detail，provider input低于token budget；
- provider input真实超过token budget，仍正确进入Long-Horizon compaction；
- 现有低于hard budget的proactive token trigger ratio行为保持不变；
- 现有Long-Horizon projection-unit trigger仍可独立进入window maintenance；
- ordered projection 16 MiB、append 512 units、candidate 4 MiB等physical bound仍fail closed；
- audit page/root大小不能改变provider semantic fingerprint或compaction decision；
- 事故fixture完成follow-up ModelStart、accepted reply和RunEnd(FINAL)。

### 12.3 Audit materialization

- plan/page/root全部成功；
- plan write failure且page write count为0；
- first/middle/final page failure；
- root failure后plan/pages可被offline GC发现；
- same plan/root/page ID + different payload conflict；
- root exists但plan/page缺失；
- page/root tamper；
- sealed MCP/interaction secret被typed repository拒绝；
- source大于16 MiB直接skipped；
- 20 MiB unbounded diagnostic/metadata在固定permit之后被bounded encoder typed skip，正常model Start与provider dispatch不受影响；
- source extractor、freeze或codec异常只形成`skipped_source_capture`，不产生context failure或RunError；
- no unowned asyncio task。

### 12.4 I/O ownership与close

- 8个critical slots占满时audit typed skip，不影响critical owner；
- audit lane running时required context read仍可准入；
- 9个并发session只能安装8个process audit operations，第9个typed skip；
- process resident quotes累计超过64 MiB时，即使operation count未满也typed skip；
- submit failure、normal exit、exception和close drain均精确释放process permit；
- audit waiter cancellation只detach；
- Host close等待真实audit physical exit；
- close deadline到期为typed physical blocker，但ledger latch保持false；
- diagnostic不包含audit正文、artifact payload或raw exception text。

### 12.5 Replay/Inspector/GC

- exact provider payload replay在所有audit artifacts缺失时通过；
- exact audit hydrate；
- bounded reconstructed audit及typed omissions；
- reconstruction越界 -> unavailable；
- invalid audit + successful canonical reconstruction；
- transcript GC不再因历史ContextCompiled永久pin旧checkpoint；active/fallback canonical roots仍被保护；
- incomplete audit GC按paged event catalog发现旧plan；
- audit GC不删除任何canonical component artifact。

### 12.6 Physical bounds

- semantic commit <=64 KiB；
- audit expectation <=8 KiB；
- every ContextCompiled event branch <=256 KiB；
- plan <=128 KiB；
- root <=64 KiB；
- page <=256 KiB；
- total pages <=64且总bytes <=16 MiB；
- provider dispatch critical path的required context-audit ArtifactStore I/O次数为0。

---

## 13. Architecture guards

机器守卫必须禁止：

- `max_input_manifest_chars`重新进入Context Compile model admission；
- Agent在dispatch前await audit plan/page/root物理结果；
- audit operation占用`ContextInputIoLane.CRITICAL`配额；
- ProviderInput DTO引用artifact write outcome或audit root identity；
- `manifest_projection_reference_fingerprint`和`compiled_manifest` production残留；
- provider physical policy继续使用`*_per_manifest`或`context_manifest_*`旧命名；
- ModelStart recovery、provider payload replay或resume读取audit artifact；
- `ContextCompiledEvent`重新拥有旧unbounded audit/display字段；
- `FrozenStorageFactBase`进入AgentEvent/EventLog payload；
- sealed MCP/interaction secret carrier进入audit plan/page/root；
- expectation factory序列化full source或计算page layout；
- plan FULL前写page，或page未确认时写root；
- globally shared audit page在没有reference index时被GC；
- audit page count触发Long-Horizon compaction；
- audit failure生成RunError或设置Runtime reconciliation latch；
- 新增context-audit durable job/retry/recovery service；
- RuntimeSession增加专用audit close phase；
- v8 full-manifest production loader、writer或dual-write残留；
- audit GC加载full manifest来寻找checkpoint protection；
- transcript GC把historical ContextCompiled projection-base reference当作永久retention root；
- architecture grep误伤Long-Horizon自身的compaction input manifest。

---

## 14. 验收指标

| 指标 | 目标 |
|---|---:|
| provider dispatch前required context-audit writes | 0 |
| audit operation占用critical I/O slots | 0 |
| semantic commit最大canonical bytes | 64 KiB |
| 任一ContextCompiled branch最大canonical bytes | 256 KiB |
| audit root最大canonical bytes | 64 KiB |
| audit page最大canonical bytes | 256 KiB |
| manifest size触发RunError路径 | 0 |
| manifest size触发compaction路径 | 0 |
| manifest专用repair/reconciliation owner | 0 |
| flat full manifest artifact | 0 |
| v8 production symbol/string命中 | 0 |
| 事故fixture结果 | final reply + RunEnd(FINAL) |

数据库侧验证：长session不再为每次model call写入接近1 MiB的flat重复manifest；page只承载没有现有durable owner的bounded detail，incomplete pages可由plan精确清理。

---

## 15. Definition of Done

- [x] `ContextCompileInputCommitFact`是compiled context dispatch的唯一compact commit。
- [x] `ProviderInputPreparationInstallFact`是ContextCompiled与staged candidate之间唯一durable installation carrier。
- [x] ContextCompiled和Start batch保持两个明确线性化点。
- [x] ProviderInput append、ModelStart、recovery不依赖audit availability。
- [x] ModelStart exact payload可只从ProviderInput durable authority恢复。
- [x] full v8 manifest被references + plan-scoped pages + root物理替代。
- [x] expectation不预计算page layout，plan在page之前FULL，root最后写入。
- [x] old ContextCompiled unbounded audit/display fields已删除。
- [x] `max_input_manifest_chars`不再存在于production admission。
- [x] provider token budget是唯一provider-visible内容预算；既有Long-Horizon unit-count maintenance保持不变。
- [x] provider structural/physical安全上限仍完整保留。
- [x] audit写失败不会产生RunError、failed RunEnd或Runtime reconciliation latch。
- [x] audit lane不会耗尽required ContextInput I/O容量。
- [x] exact/reconstructed/unavailable/integrity outcomes均有typed测试。
- [x] transcript GC不再加载full manifest或把历史compile当retention owner；incomplete audit GC可安全删除plan-owned pages。
- [x] 不新增audit durable job、repair service、数据库表或close phase。
- [ ] event schema generation单调推进，reset发生在MP2原子cut。
- [x] architecture guards阻止旧耦合回归。
- [ ] PostgreSQL/Oxigraph reset后全量non-real测试通过。
- [ ] real TUI dogfood在原`little_snake`等价轨迹下通过。

---

## 15.1 MP0-MP3实施回执

### MP0：最终authority与typed vocabulary

- 新增`ContextCompileInputCommitFact`、`ProviderInputPreparationInstallFact`和`ContextInputAuditExpectationFact`，分别冻结semantic commit、preparation install与optional audit expectation。
- 新增storage-only plan/page/root vocabulary；这些DTO注册在独立storage registry，不能进入AgentEvent union或EventLog writer。
- 冻结中央、穷尽、不可变的component extractor registry；每项拥有exact kind、ownership、ID、version和fingerprint。
- 冻结并覆盖1,065,543-byte sanitized事故fixture；compact ContextCompiled仍小于256 KiB，provider estimate保持56k量级。

### MP1：optional audit lane与分页载体

- 实现plan FIRST、bounded pages、root LAST的materializer，以及immutable same-ID compatible success/conflict校验。
- existing canonical authority只允许小型inline reference；snapshot、ordered projection、provider plan、projection state、rollup和rollout等完整对象不会复制进page。
- 新增独立2-worker best-effort executor、process-wide 8 operations/64 MiB和session-wide 1 operation限制；不占用critical ContextInput I/O slots。
- 实现exact/reconstructed/unavailable/integrity closed load outcome、read-only doctor与24小时incomplete plan/page GC。

### MP2：production hard cut

- 删除`context_input/manifest.py`、flat v8 DTO、manifest write owner、`compiled_manifest` provider vocabulary和manifest-specific failure/admission路径。
- `ContextCompiledEvent`仅保存compact carriers与既有bounded Long-Horizon attribution；MP2先从9推进到10，post-review canonical-plan hard cut最终推进到11。
- Provider planner/coordinator/store/recovery与model-stream recovery全部改为exact join `semantic_commit_fingerprint`；required provider payload replay只读取ModelStart、append、vector/root、horizon与binding authority。
- optional audit只在ProviderInput append + ModelStart FULL后nowait offer；skip、capacity、deadline、artifact failure均不能改变provider dispatch、RunEnd或runtime reconciliation。
- transcript checkpoint GC不再读取historical full manifest或把compile attribution当永久retention root。

### MP3：Inspector、doctor与长期门控

- Inspector并列展示required provider replay与optional audit状态，不用后者覆盖前者。
- CLI新增`checkpoint doctor --domain context_input_audit [--require-exact-audit]`与`checkpoint gc --domain context_input_audit [--apply]`；PostgreSQL exclusive maintenance authority继续验证durable session close与无running run。
- 长期EventLog、Inspector和LLM transport contracts已同步；architecture guards阻止storage DTO进入EventLog、audit触发runtime latch/RunError以及canonical authority重新复制进pages。

### 本轮验证证据

- Context commit/audit contract：`21 passed`。
- ProviderInput与subagent主链：`115 passed`。
- Context snapshot/compaction、EventLog、Inspector、architecture：`235 passed`。
- production provider replay/audit failure路径：`4 passed`。
- CLI checkpoint/doctor/GC：`5 passed`。
- 最终关键回归与formatter后复核：`7 passed`、`21 passed`、`7 passed`。
- `ruff check src tests`、`ruff format --check src tests`、`compileall`、`git diff --check`均通过。

本轮没有执行破坏性的PostgreSQL/Oxigraph reset，没有运行全量pytest，也没有运行real TUI dogfood；因此上方三个相关DoD保持未勾选。event schema代码真值已推进到11，但只有在generation 11代码下完成reset与全量验证后，才能把组合项“event schema generation单调推进，reset发生在MP2原子cut”勾选为完成。

## 15.2 Post-review finding收口

- optional audit source改为只冻结process-local lazy capture；取得固定最坏情况permit后才执行bounded canonical encode。oversize、source extractor/freeze/codec异常和capacity rejection都只形成typed process-local skip，不再进入context preparation failure、`RunErrorEvent`或failed `RunEnd`。
- `ProviderInputPreparationInstallFact.v2`与`ProviderInputPreparationOwnershipAttributionFact.v4`新增canonical provider-plan fingerprint，并与staged candidate、semantic commit、`ModelCallStart.committed_provider_input_reference`逐项exact join。
- child RuntimeSession teardown改由admission registry原子安装唯一`active -> closing(task/generation) -> closed` owner；normal、timeout、cancel、batch repair和Host close只shield-await同一physical task。
- incomplete audit GC的catalog/read/delete复用同一absolute deadline；PostgreSQL adapter在artifact advisory lock前安装connection deadline与statement timeout。
- exact audit load与GC共用plan-reference validator：由deterministic expected plan artifact ID和实际plan正文重建完整expected reference，再与root reference exact比较。

Post-review定向门控为`276 passed in 66.20s`，覆盖audit、EventLog schema、ProviderInput、subagent teardown、CLI/Inspector及production Agent路径；新增PostgreSQL物理deadline顺序探针也包含在该矩阵中。`ruff format src tests`、`ruff check src tests`、`compileall`与`git diff --check`均通过。本轮仍按约定未运行全量pytest、数据库reset或real TUI dogfood。

## 15.3 Second post-review finding收口

- production optional audit不再接受返回完整source tuple的任意callable。Agent只冻结引用既有authority的borrowed component carrier；固定32 MiB permit FULL后，唯一closed collector才以4 KiB canonical JSON chunk写入临时文件spool，按UTF-8边界记录page range，并且每次只重建一个page fact。collector不会同时持有frozen source、canonical copy、JSON chunks与全部page正文。
- streaming collector在encode期间执行16 MiB canonical-byte、64 page、单page及mapping fanout hard bound；oversize、unsupported source、secret/storage/runtime carrier和codec failure均只返回process-local typed skip。12 MiB合法输入的新增`tracemalloc`峰值为`14,123,469` bytes并产生56 pages；20 MiB输入在约`28,961` bytes新增峰值时返回`skipped_physical_bound`。因此两个各32 MiB permit的operation不会再突破64 MiB process audit resident account。
- child RuntimeSession teardown现在由registry持有`active -> closing(task,generation) -> retry_wait | closed | reconciliation_required`完整状态。task退出后必定清除physical task owner；typed retryable failure允许调用者携带新absolute deadline安装下一generation，terminal failure则latch typed reconciliation，不能重复执行同一失败task。
- 原Host-specific临时teardown入口已物理删除。新增purpose-neutral `NonHostRuntimeSessionTeardownPort`，只允许`resume_recovery | child_terminal`两种closed purpose；两者执行完全相同的provider-input quiesce、writer/reducer barrier、checkpoint maintenance、remaining I/O drain与最终sync close。AST gate验证exact两个production caller及其purpose，不能靠新增字符串allowlist扩张。
- PostgreSQL exact artifact delete为整个transaction安装physical cancellation owner，并在advisory lock、row read、delete与显式commit前逐次重算同一absolute deadline。fake physical-cancel探针与真实PostgreSQL advisory-lock integration均证明超时会取消/关闭exact connection；deadline后不会继续等待后续statement或无界commit。
- canonical provider-plan与deterministic plan-reference两条上一轮主线保持不变，并继续由原exact-join回归覆盖。

本轮最终定向矩阵为`57 passed in 14.81s`，包括context audit全矩阵、production Agent optional-audit三路径、child teardown三分支、resume PostgreSQL crash-repair、incident architecture gate及真实PostgreSQL locked-delete测试。按约定没有运行全量pytest或real TUI dogfood。当前数据库状态为migration head 12、registry prefix exact match、`up_to_date`；本轮没有新增durable schema变更，因此不需要再次reset。

## 15.4 Final post-review finding收口

- `NonHostRuntimeSessionTeardownRetryableError`不再继承`TimeoutError`。Child运行预算由独立`ChildExecutionBudgetExpired` owner产生；teardown waiter/physical disposition不能再进入`subagent_timeout`分支，也不能改写已经由child `RunEnd`确定的semantic outcome。
- child admission registry现在安装一个跨全部physical attempts存活的teardown lineage task。retryable physical exit在同一owner内执行bounded backoff并自动推进generation；成功进入`closed`，三次重试耗尽或不可恢复失败进入typed `reconciliation_required`。任何terminal状态都会清除task，`retry_wait`只允许持有仍存活的lineage owner，不再留下无人扫描的failed task。
- `ChildRuntimeCompositionLease`单独持有purpose-bound `NonHostRuntimeSessionTeardownCapability`。registry只能调用该narrow capability，不能借完整`RuntimeSession`执行其他close surface；resume recovery也通过同一binder取得`RESUME_RECOVERY` capability。AST gate冻结exact两个purpose binding及唯一底层port invocation。
- audit page splitter不再按未转义fragment bytes猜测上限。它使用最终`ContextInputAuditPageFact` storage wrapper、固定宽度fingerprint及最坏合法ordinal计算canonical byte upper bound，并按UTF-8边界动态缩小range；最终page再次验证256 KiB hard bound。160,000个反斜杠的回归现在成功分页，任一wrapper本身不可容纳时唯一返回`SKIPPED_PHYSICAL_BOUND`。
- PostgreSQL artifact delete把physical operation `arm()`纳入统一异常归一化区域。调用时deadline已经过期的竞态现在稳定返回`TimeoutError`，不会泄漏`PostgresSchemaError`，且不会checkout connection或进入transaction。

本轮按约定只运行定向门控：context audit、subagent、incident architecture三文件共`138 passed`；真实PostgreSQL dangling-run resume回归`1 passed`。未运行全量pytest或real TUI dogfood。

---

## 16. 最终判断

本问题不能通过提高1 MiB上限修复。正确边界是：

```text
compact ContextCompiled dispatch commit
-> durable ProviderInput preparation installation
-> ProviderInput append + ModelStart authority
-> provider dispatch

optional audit expectation
-> low-priority plan-first/page/root-last materialization
-> exact | reconstructed | unavailable | integrity failure
```

Provider实际看到的输入由ProviderInput durable vector/root证明；compiler audit只提供额外解释材料。这样既保留crash recovery、checkpoint protection、Inspector和offline审计，又不会让审计载体成为第二上下文窗口，也不会用best-effort I/O反向耗尽required runtime资源。
