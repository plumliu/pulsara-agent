# Pulsara Durable Hook / Projection Jobs Hard-Cut 实施规格

状态：**实施完成，D3 CLOSED，normative**

对应债务：`PULSARA_RUNTIME_ARCHITECTURE_DEBT_REBASE.zh.md` 的 D3

实施代号：`DPJ0 -> DPJ5`

最后修订：2026-07-25

---

## 0. 文档地位

本文冻结 Pulsara D3 **Durable hook/projection jobs** 的生产实现。

关键词：

- **MUST / 必须**：实现不得偏离；
- **MUST NOT / 禁止**：实施后不得存在；
- **SHOULD / 应**：只有经显式 reviewer 裁决才能偏离；
- **MAY / 可以**：不影响本 hard cut 的实现选择。

本文不是“给现有 hook 加一个后台队列”。它完成以下 ownership hard cut：

```text
canonical AgentEvent / canonical graph mutation
                    |
                    v
       durable projection admission
                    |
                    v
       durable lease/retry/dead-letter
                    |
                    v
          idempotent projection handler
                    |
                    v
       durable result / target-head receipt
```

实施完成后：

1. EventLog canonical commit 不等待 timeline、artifact、graph、Oxigraph、search index 或 vector index；
2. publisher/hook failure 不再是 durable projection failure 的唯一记录；
3. process crash 后 pending projection 可以恢复；
4. timeline、tool-result execution evidence 与 canonical mutation surfaces 各有唯一 durable owner；
5. UI/CLI observer 仍是轻量 best-effort，不冒充 durable completion。

本文 supersede 债务文档中“可由 hook 自己 retry”的任何宽松解释。

---

## 1. 为什么现在实施 D3

### 1.1 前置债务已经闭环

D3 的两个真实 blocker 已经消失：

- Schema Hot-Path hard cut 已提供 immutable migration registry、verified PostgreSQL access 与 verify-only startup；
- RuntimeSession writer 已提供 typed event、account/reducer 同批提交和稳定 publication outcome。

因此新增 job、lease、dead-letter schema 不再需要 constructor DDL，也不需要重新设计 EventLog writer。

### 1.2 当前代码真值

当前 production 路径仍有三类 derived work 由 process-local callback 或不一致的 outbox consumer 持有。

#### A. Run timeline

`RunTimelinePersistenceHook` 在 publisher callback 内：

1. 重新读取 run EventLog；
2. 组装 timeline；
3. 写 artifact；
4. 写 graph；
5. 追加 canonical mutation outbox。

问题：

- callback 失败只进入 `RuntimeHookManager.errors`；
- `event_store.iter(run_id=...)` 没有冻结 source high-water；
- artifact、graph、outbox 是多个 physical commit；
- retry 会重新读取“现在”的 ledger，而不是原 trigger 时刻的 ledger；
- publisher 延迟直接增加 RuntimeSession publication latency。

#### B. Tool-result execution evidence

`ExecutionEvidencePersistenceHook` 并不注册在 `RuntimeHookManager`，而是由 `AgentRuntime`
在 tool result 后直接调用。

失败时写入 typed `ToolResultEvidenceProjectionFailedEvent`，但该事件只是 audit：

- 它没有 durable retry owner；
- 它不证明 projection 后续成功；
- 当前 evidence ID 使用随机 UUID，timestamp 使用 wall clock，无法 exact replay；
- graph write 与 mutation outbox write 仍可能分裂。

因此 D2 的 typed failure audit **没有**关闭 D3。

#### C. Canonical mutation replay

`CanonicalMutationOutboxReplayHook` 在 publisher callback 内构造同步 reconciler。

现有 `memory_write_outbox` 同时混合：

- immutable mutation body；
- mutable per-surface status；
- generic status/attempt；
- vector-only claim token；
- search index、vector、Oxigraph 三套不同 claim/transaction 算法。

部分 consumer 持有 PostgreSQL row lock 时执行外部 I/O；另一些 consumer 使用独立 lease。
这既不是统一 job ownership，也不能给出一致的 restart/crash 语义。

### 1.3 当前 failure accounting 不可靠

`RuntimeHookManager` 和 `RuntimeEventPublisher` 都只保存 process-local error list。
并且 `RuntimeHookManager` 会吞掉 hook exception，所以 publisher 可能把 callback 视为成功。

结果是：

```text
AgentEvent durable FULL
    -> callback invoked
    -> graph write failed
    -> error only in memory
    -> process exits
    -> no durable pending work
```

这是 D3 要消除的核心窗口。

---

## 2. 目标与非目标

### 2.1 目标

本 hard cut 必须完成：

1. durable projection job schema；
2. EventLog-to-job durable seeder；
3. stable job identity、closed handler registry；
4. lease、retry schedule、dead-letter 与 exact settlement；
5. run timeline vertical hard cut；
6. tool-result execution evidence vertical hard cut；
7. canonical mutation outbox V2 与统一 surface delivery worker；
8. publisher heavy callback 删除；
9. restart、Host close、UNKNOWN settlement 与 Inspector；
10. additive schema、canonical mutation 与per-kind admission migrations `0005 -> 0008`。

### 2.2 非目标

本阶段明确不做：

- 不把 derived projection 加进 AgentEvent canonical commit transaction；
- 不为 job failure 再写 AgentEvent；
- 不实现 generic user-extensible background job framework；
- 不允许 handler dynamic import；
- 不自动 backfill 所有 pre-cutover timeline/evidence；
- 不关闭 D5 compaction-memory extension；
- 不把 `memory_candidate_projection_outbox` 合并进本 job schema；
- 不保证 UI subscriber exactly-once；
- 不以 async PostgreSQL driver 重写现有 storage；
- 不删除 historical `ToolResultEvidenceProjectionFailedEvent` decoder。

`memory_candidate_projection_outbox` 的 producer/event-first correctness 和 compaction extension
继续由 D5 拥有。

---

## 3. 中央架构裁决

### 3.1 EventLog 是 projection admission 的 canonical source

Timeline 和 execution-evidence job **不**作为 RuntimeSession transaction companion 写入。

理由：

- 它们是 derived projection，不是 canonical run invariant；
- projection repository 暂时故障不能阻止 AgentEvent commit；
- job 可由 immutable EventLog 确定性重建；
- companion 会把 projection schema availability 重新带回 EventLog hot path。

正确拓扑：

```text
RuntimeSession atomic AgentEvent commit
                  |
                  v
            canonical EventLog
                  |
       +----------+----------+
       |                     |
       v                     v
best-effort wake      periodic durable scan
       |                     |
       +----------+----------+
                  v
 DurableProjectionEventSeeder
  - exact ledger prefix
  - stable job candidates
  - jobs + seed checkpoint atomic
                  |
                  v
   DurableProjectionJobService
```

Publisher wake 丢失不影响 correctness；periodic scan 必须最终发现 behind session。

### 3.2 Seeder checkpoint 不得先于 job admission

Seeder 的唯一 commit 必须在一个 PostgreSQL transaction 中：

1. lock exact seed checkpoint；
2. 读取并验证 source ledger prefix；
3. 读取 bounded delta；
4. 构造全部 stable job candidates；
5. insert 或 exact-confirm 全部 job rows；
6. advance checkpoint；
7. commit。

V1要求EventLog、runtime projection checkpoints与projection job tables位于同一个
`VerifiedPostgresSchemaBinding`/physical target。Separate job database不受支持；否则“jobs +
checkpoint atomic”声明不成立。

禁止：

```text
advance checkpoint
-> later insert jobs
```

允许的 crash 窗口：

```text
insert jobs
-> crash before checkpoint commit
```

由于同一 transaction，该窗口最终为 NONE；若未来 repository 分拆，也只能得到
“job FULL、checkpoint old”，下次 exact-confirm 后继续，不得漏 job。

### 3.3 Canonical mutation 已经是 durable source，不再包第二层 outbox

`memory_write_outbox` 本身就是 canonical mutation delivery source。

它要升级为：

```text
immutable canonical mutation
        |
        +-- search-index surface job
        +-- vector-index surface job
        +-- oxigraph surface job
```

禁止创建：

```text
memory_write_outbox
    -> generic durable_projection_jobs
        -> surface worker
```

这种双层 outbox 会制造两套 lease、retry 与 completion 真源。

### 3.4 Publisher 只允许 O(1) wake

生产 publisher callback 可以：

- `asyncio.Event.set()`；
- 写 bounded process-local diagnostic；
- 投递已有内存 queue 的 bounded notification。

生产 publisher callback 禁止：

- EventLog range read；
- PostgreSQL artifact/graph write；
- Oxigraph I/O；
- embedding/vector I/O；
- search-index replay；
- durable job claim；
- retry loop；
- unbounded serialization。

---

## 4. 三类 identity 必须分离

### 4.1 Job semantic identity

表示“哪一个 canonical source 要执行哪一种 projection”。

它只覆盖：

- projection kind；
- exact source event reference；
- target semantic key；
- handler contract；
- source interpretation contract。

它不得覆盖：

- current attempt；
- lease owner；
- retry time；
- process ID；
- observed failure；
- artifact physical locator；
- worker version attribution。

### 4.2 Projection result semantic identity

表示 source 经 handler contract 得到的 canonical output。

它覆盖：

- ordered output document semantics；
- deterministic artifact/document IDs；
- exact source horizon；
- lowering/projection contract。

它不覆盖：

- attempt；
- lease；
- PostgreSQL row revision；
- Oxigraph endpoint；
- publication wake。

### 4.3 Physical delivery attribution

表示：

- 哪个 attempt/lease 执行；
- 何时 claim/settle；
- 哪个 concrete artifact row；
- 哪个 external surface；
- retry/dead-letter diagnostic。

物理 attribution 不得反向改变 job/result semantic fingerprint。

---

## 5. Central vocabulary

所有 FrozenFact 必须：

- `extra="forbid"`；
- 有显式 `schema_version`；
- 由中央 factory 重算 fingerprint；
- 禁止 caller 自报一个未重算 fingerprint；
- 使用 domain-separated canonical JSON SHA-256。

统一 fingerprint：

```text
H(domain, value...)
    = "sha256:" + SHA256(
        UTF8(canonical_json({
            "domain": domain,
            "values": [...]
        }))
      ).hexdigest()
```

Canonical JSON 必须：

- UTF-8；
- object key 升序；
- 无 insignificant whitespace；
- tuple 编码为 JSON array；
- `None` 编码为 JSON null；
- datetime 编码为 UTC RFC3339 microsecond；
- 禁止 NaN/Infinity；
- 字符串不做 Unicode compatibility normalization。

### 5.1 Projection kind

```python
from enum import StrEnum


class DurableProjectionKind(StrEnum):
    RUN_TIMELINE = "run_timeline.v1"
    TOOL_RESULT_EXECUTION_EVIDENCE = "tool_result_execution_evidence.v1"
```

V1 只允许这两个 generic job kind。

Canonical mutation surfaces 使用独立 closed registry，不加入该 enum。

### 5.2 Exact source event reference

```python
class DurableProjectionSourceEventReferenceFact(FrozenFactBase):
    schema_version: Literal[
        "durable_projection_source_event_reference.v1"
    ] = "durable_projection_source_event_reference.v1"
    runtime_session_id: str
    run_id: str
    turn_id: str
    reply_id: str
    event_id: str
    sequence: int
    event_type: str
    event_schema_version: str
    event_schema_fingerprint: str
    event_domain_contract_fingerprint: str
    payload_fingerprint: str
    stored_envelope_fingerprint: str
    reference_fingerprint: str
```

Invariants：

- `sequence >= 1`；
- ref 必须 exact-read 回同一 stored envelope；
- `payload_fingerprint` 从 decoded typed event 重算；
- `stored_envelope_fingerprint` 从 storage row canonical fields 重算；
- 每个job的
  `trigger_horizon.through_sequence == source_event_reference.sequence`；
- trigger horizon的continuity/payload/transcript prefix字段必须直接来自该stored event row；
- seeder分页/扫描high-water不得进入job semantic或candidate fingerprint。

### 5.3 Source horizon

```python
class DurableProjectionLedgerHorizonFact(FrozenFactBase):
    schema_version: Literal[
        "durable_projection_ledger_horizon.v1"
    ] = "durable_projection_ledger_horizon.v1"
    runtime_session_id: str
    through_sequence: int
    ledger_continuity_accumulator: str
    ledger_payload_prefix_bytes: int
    transcript_semantic_prefix_count: int
    transcript_semantic_prefix_accumulator: str
    horizon_fingerprint: str
```

Horizon 必须直接来自 EventLog canonical prefix，不得由 selected event slice 自行 hash。

同一个类型承担两种明确命名的用途：

- `trigger_horizon`：job-local，严格等于trigger sequence；
- `scan_horizon`：seed commit-local，表示本批checkpoint准备推进到的扫描high-water。

两者不得互换。Seeder可以在一次`scan_horizon=100`的batch中发现sequence 75的trigger，但该job
只能保存EventLog row 75携带的canonical prefix。相同trigger无论落在哪个分页batch，都必须产生
byte-identical job semantic/candidate。

### 5.4 Handler contract

```python
class DurableProjectionTargetUpdatePolicy(StrEnum):
    FULL_REPLACEMENT = "full_replacement"
    SINGLE_ASSIGNMENT = "single_assignment"


class DurableProjectionHandlerContractFact(FrozenFactBase):
    schema_version: Literal[
        "durable_projection_handler_contract.v1"
    ] = "durable_projection_handler_contract.v1"
    projection_kind: DurableProjectionKind
    handler_id: str
    handler_version: str
    accepted_source_event_types: tuple[str, ...]
    accepted_source_schema_bindings_fingerprint: str
    target_update_policy: DurableProjectionTargetUpdatePolicy
    result_schema_fingerprint: str
    idempotency_contract_fingerprint: str
    contract_fingerprint: str
```

Composition root 必须验证：

- admission registry中每个当前 `DurableProjectionKind` 恰好一个 contract；
- execution registry按exact contract fingerprint选择handler；
- execution registry可以保留有non-terminal job引用的historical handler；
- 没有数据库字符串驱动的额外handler；
- source event type set 非空且无重复；
- accepted schema binding fingerprint只覆盖该handler实际读取的event type/version/domain contracts；
  无关historical decoder增加不得改变现有handler identity；
- handler 不通过 module path/dynamic import 解析。

V1 closed mapping：

```text
run_timeline.v1
    -> full_replacement

tool_result_execution_evidence.v1
    -> single_assignment
```

Policy是target authority，不是worker调度提示：

- `full_replacement`允许同一target由更高source sequence的完整projection推进；
- `single_assignment`在第一次assignment后永久冻结source event/result；任何不同source event都
  是authority conflict，不能以“更新”或“supersede”吸收。

Handler semantic contract一旦写入job就不可改。若实现变更会改变result semantics：

1. 新增新的versioned projection kind/handler contract；
2. 由后续独立规格定义old-kind deactivation、checkpoint freeze、new-kind cutover与target handoff；
3. 已存在的non-terminal job继续由旧contract handler处理；
4. 只有old activation已durably deactivated、全部old checkpoints冻结且没有non-terminal owner，
   才允许删除historical executable；
5. D3 V1不实现该upgrade，禁止让同一个 `run_timeline.v1` 在部署后静默表示另一种projection。

Physical policy：

```python
class DurableProjectionPhysicalPolicyFact(FrozenFactBase):
    schema_version: Literal[
        "durable_projection_physical_policy.v1"
    ] = "durable_projection_physical_policy.v1"
    database_operation_timeout_seconds: int
    source_hydration_timeout_seconds: int
    handler_compute_timeout_seconds: int
    result_commit_timeout_seconds: int
    external_surface_attempt_timeout_seconds: int
    maximum_physical_attempt_seconds: int
    policy_fingerprint: str
```

V1 defaults：

```text
database operation = 10s
source hydration = 20s
handler compute = 30s
result commit = 20s
external surface attempt = 60s
maximum physical attempt = 120s
```

每个lease FULL后冻结一个process-monotonic absolute attempt deadline。Nested operation只能取
`min(phase bound, remaining attempt)`，不得续期。一次retry是新的physical attempt，可以获得新预算，
但job semantic/retry policy不变。

Ordinary compute/I/O在attempt deadline前预留30秒confirmation/settlement tail。Tail复用同一absolute
deadline；不得在UNKNOWN后创建新30秒。Deadline耗尽仍无法确认时返回`UNRESOLVED`并保留lease，
等待expiry/recovery。

### 5.5 Retry policy

```python
class DurableProjectionRetryPolicyFact(FrozenFactBase):
    schema_version: Literal[
        "durable_projection_retry_policy.v1"
    ] = "durable_projection_retry_policy.v1"
    maximum_attempts: int
    base_delay_milliseconds: int
    maximum_delay_milliseconds: int
    lease_duration_seconds: int
    claim_batch_size: int
    policy_fingerprint: str
```

V1 frozen policy：

```text
maximum_attempts = 12
base_delay_milliseconds = 1000
maximum_delay_milliseconds = 300000
lease_duration_seconds = 180
claim_batch_size = 32
```

Retry delay：

```text
delay(attempt)
    = min(maximum_delay, base_delay * 2 ** max(0, attempt - 1))
```

不使用随机 jitter。多 worker 竞争由 DB lease 和 `SKIP LOCKED` 解决，deterministic schedule
有利于 replay/Inspector。

V1不续租。单次physical attempt必须在lease expiry前至少预留30秒settlement tail；
超时或worker失联后允许idempotent reclaim。

```python
class DurableProjectionDeliveryPolicyFact(FrozenFactBase):
    schema_version: Literal[
        "durable_projection_delivery_policy.v1"
    ] = "durable_projection_delivery_policy.v1"
    retry_policy: DurableProjectionRetryPolicyFact
    physical_policy: DurableProjectionPhysicalPolicyFact
    delivery_policy_fingerprint: str
```

Delivery policy在job admission时冻结，但属于physical ownership，不进入projection result semantic。

Delivery policy升级规则：

- existing job/surface row永远继续使用其durable policy；
- V1中一个projection kind/seed contract只能绑定一份delivery policy；
- generic projection policy改变不能原地修改`run_timeline.v1`或
  `tool_result_execution_evidence.v1`；它需要后续独立规格新增versioned kind以及old-kind
  deactivation/cutover authority，D3 V1不声称已支持live upgrade；
- surface policy/handler改变必须创建新的versioned surface kind；仅composition中新增或删除
  既有compatible surface时，才允许新mutation采用不同surface plan；
- crash留下“job已FULL、checkpoint未推进”时，recovery必须exact-read并采用existing durable
  candidate/policy推进accumulator，不能用当前default覆盖；
- 禁止对non-terminal row做bulk policy rewrite。

### 5.6 Job semantic fact

```python
class DurableProjectionJobSemanticFact(FrozenFactBase):
    schema_version: Literal[
        "durable_projection_job_semantic.v1"
    ] = "durable_projection_job_semantic.v1"
    job_id: str
    projection_kind: DurableProjectionKind
    target_key: str
    source_event_reference: DurableProjectionSourceEventReferenceFact
    trigger_horizon: DurableProjectionLedgerHorizonFact
    handler_contract: DurableProjectionHandlerContractFact
    job_semantic_fingerprint: str
```

```python
class DurableProjectionJobCandidateFact(FrozenFactBase):
    schema_version: Literal[
        "durable_projection_job_candidate.v1"
    ] = "durable_projection_job_candidate.v1"
    job_semantic: DurableProjectionJobSemanticFact
    activation_fingerprint: str
    seed_contract_fingerprint: str
    delivery_policy: DurableProjectionDeliveryPolicyFact
    canonical_mutation_surface_plan: "CanonicalMutationSurfacePlanFact"
    candidate_fingerprint: str
```

Candidate fingerprint覆盖semantic + delivery policy，用于durable admission exact-confirm；
job/result semantic fingerprint不覆盖delivery policy。

Job ID：

```text
job_id = "projection-job:" + H(
    "pulsara:durable-projection-job-id:v1",
    projection_kind,
    source_event_reference.runtime_session_id,
    source_event_reference.event_id,
    target_key,
    handler_contract.contract_fingerprint,
)
```

同一 event/kind/target 必须得到 byte-identical candidate。

`target_key` 只能由中央factory派生，格式冻结为：

```text
run_timeline.v1
    = "run:" + H(runtime_session_id, run_id)

tool_result_execution_evidence.v1
    = "tool-result:" + H(
          runtime_session_id,
          run_id,
          tool_call_id,
      )
```

Evidence target刻意不覆盖`result_semantic_fingerprint`：一个session/run/tool call只有一个terminal
evidence assignment。冲突的第二个`ToolResultEndEvent`必须竞争同一target并fail closed，而不是
获得另一个target。禁止caller传入自由target key。

### 5.7 Operational state

```python
class DurableProjectionJobStatus(StrEnum):
    PENDING = "pending"
    LEASED = "leased"
    RETRY_WAIT = "retry_wait"
    SUCCEEDED = "succeeded"
    SUPERSEDED = "superseded"
    DEAD_LETTER = "dead_letter"


class DurableProjectionJobOperationalStateFact(FrozenFactBase):
    schema_version: Literal[
        "durable_projection_job_operational_state.v1"
    ] = "durable_projection_job_operational_state.v1"
    status: DurableProjectionJobStatus
    state_revision: int
    repair_generation: int
    attempt_count: int
    lease_generation: int
    lease_owner_id: str | None
    lease_expires_at: datetime | None
    next_attempt_at: datetime | None
    last_failure: BoundedRuntimeFailureDiagnosticFact | None
    result_receipt_reference: (
        "DurableProjectionResultReceiptReferenceFact | None"
    )
    state_fingerprint: str
```

Conditional invariants：

- `state_revision >= 0`、`repair_generation >= 0`、`attempt_count >= 0`；
- `repair_generation > 0` required exact contiguous repair-action lineage；
- `LEASED` required lease owner/expiry；
- non-`LEASED` 禁止 live lease；
- `RETRY_WAIT` required next attempt/failure；
- `SUCCEEDED` required result；
- `SUPERSEDED` required result，且exact-read handler contract的
  `target_update_policy == full_replacement`；`single_assignment`出现该状态是repository
  authority conflict；
- `DEAD_LETTER` required failure；
- terminal state 不得重新 claim，只有 admin repair 创建新 repair generation。

### 5.8 Result semantic 与 immutable receipt

```python
class DurableContentAddressedArtifactReferenceFact(FrozenFactBase):
    schema_version: Literal[
        "durable_content_addressed_artifact_reference.v1"
    ] = "durable_content_addressed_artifact_reference.v1"
    artifact_semantic_id: str
    content_sha256: str
    content_utf8_bytes: int
    artifact_store_contract_fingerprint: str
    artifact_semantic_fingerprint: str
    reference_fingerprint: str


class DurableProjectionArtifactResultDocumentReferenceFact(FrozenFactBase):
    schema_version: Literal[
        "durable_projection_artifact_result_document_reference.v1"
    ] = "durable_projection_artifact_result_document_reference.v1"
    document_kind: Literal["artifact"]
    semantic_document_id: str
    document_semantic_fingerprint: str
    media_type: str
    content_codec_contract_fingerprint: str
    metadata_contract_fingerprint: str
    artifact_reference: DurableContentAddressedArtifactReferenceFact
    reference_fingerprint: str


class DurableProjectionGraphResultDocumentReferenceFact(FrozenFactBase):
    schema_version: Literal[
        "durable_projection_graph_result_document_reference.v1"
    ] = "durable_projection_graph_result_document_reference.v1"
    document_kind: Literal["graph_document"]
    graph_id: str
    semantic_document_id: str
    graph_document_type: str
    document_semantic_fingerprint: str
    canonical_json_sha256: str
    canonical_json_utf8_bytes: int
    jsonld_codec_contract_fingerprint: str
    reference_fingerprint: str


class DurableProjectionGraphRelationReferenceFact(FrozenFactBase):
    schema_version: Literal[
        "durable_projection_graph_relation_reference.v1"
    ] = "durable_projection_graph_relation_reference.v1"
    document_kind: Literal["graph_relation"]
    relation_id: str
    graph_id: str
    source_document_id: str
    predicate_iri: str
    target_document_id: str
    relation_semantic_fingerprint: str
    lowering_contract_fingerprint: str
    reference_fingerprint: str


DurableProjectionResultDocumentReferenceFact = (
    DurableProjectionArtifactResultDocumentReferenceFact
    | DurableProjectionGraphResultDocumentReferenceFact
    | DurableProjectionGraphRelationReferenceFact
)


class DurableProjectionCanonicalMutationReferenceFact(FrozenFactBase):
    schema_version: Literal[
        "durable_projection_canonical_mutation_reference.v1"
    ] = "durable_projection_canonical_mutation_reference.v1"
    mutation_id: str
    mutation_semantic_fingerprint: str
    ordered_surface_delivery_identity_fingerprints: tuple[str, ...]
    reference_fingerprint: str


class DurableProjectionResultSemanticFact(FrozenFactBase):
    schema_version: Literal[
        "durable_projection_result_semantic.v1"
    ] = "durable_projection_result_semantic.v1"
    projection_kind: DurableProjectionKind
    source_projection_fingerprint: str
    ordered_document_semantic_fingerprints: tuple[str, ...]
    ordered_canonical_mutation_semantic_fingerprints: tuple[str, ...]
    result_semantic_fingerprint: str


class ProjectionJobResultOwnerFact(FrozenFactBase):
    schema_version: Literal[
        "projection_job_result_owner.v1"
    ] = "projection_job_result_owner.v1"
    owner_kind: Literal["durable_projection_job"]
    job_id: str
    job_semantic_fingerprint: str
    job_candidate_fingerprint: str
    source_event_reference_fingerprint: str
    owner_fingerprint: str


class PreActivationHookResultOwnerFact(FrozenFactBase):
    schema_version: Literal[
        "pre_activation_hook_result_owner.v1"
    ] = "pre_activation_hook_result_owner.v1"
    owner_kind: Literal["pre_activation_hook"]
    projection_kind: DurableProjectionKind
    source_event_reference: DurableProjectionSourceEventReferenceFact
    hook_contract_fingerprint: str
    owner_fingerprint: str


DurableProjectionResultOwner = (
    ProjectionJobResultOwnerFact
    | PreActivationHookResultOwnerFact
)


class DurableProjectionAppliedResultReceiptFact(FrozenFactBase):
    schema_version: Literal[
        "durable_projection_applied_result_receipt.v1"
    ] = "durable_projection_applied_result_receipt.v1"
    receipt_kind: Literal["applied"]
    receipt_id: str
    result_owner: DurableProjectionResultOwner
    result_semantic: DurableProjectionResultSemanticFact
    target_key: str
    source_event_reference_fingerprint: str
    source_sequence: int
    target_head_revision: int
    result_document_references: tuple[
        DurableProjectionResultDocumentReferenceFact, ...
    ]
    canonical_mutation_references: tuple[
        DurableProjectionCanonicalMutationReferenceFact, ...
    ]
    receipt_fingerprint: str


class DurableProjectionResultReceiptReferenceFact(FrozenFactBase):
    schema_version: Literal[
        "durable_projection_result_receipt_reference.v1"
    ] = "durable_projection_result_receipt_reference.v1"
    receipt_id: str
    receipt_fingerprint: str
    reference_fingerprint: str


class DurableProjectionSupersededResultReceiptFact(FrozenFactBase):
    schema_version: Literal[
        "durable_projection_superseded_result_receipt.v1"
    ] = "durable_projection_superseded_result_receipt.v1"
    receipt_kind: Literal["superseded"]
    receipt_id: str
    candidate_result_owner: DurableProjectionResultOwner
    projection_kind: DurableProjectionKind
    target_key: str
    candidate_source_event_reference_fingerprint: str
    candidate_source_sequence: int
    effective_applied_result_receipt_reference: (
        DurableProjectionResultReceiptReferenceFact
    )
    target_head_revision: int
    receipt_fingerprint: str


DurableProjectionResultReceiptFact = (
    DurableProjectionAppliedResultReceiptFact
    | DurableProjectionSupersededResultReceiptFact
)
```

Document reference是严格discriminated union，不存在nullable carrier或
`projection_receipt`伪branch：

- artifact branch required IANA-style media type、versioned content codec、metadata contract与
  content-addressed typed artifact reference；
- graph-document branch required graph ID、JSON-LD codec与canonical JSON digest；
- graph-relation branch requireddirect edge endpoints/predicate与versioned lowering contract。

Reference不允许raw DSN/path。Artifact physical locator只存在nested content-addressed attribution，
不能保存credential-bearing locator。Media type、codec与metadata fingerprint全部进入artifact
document semantic；改变任一项不得复用旧document identity。

`durable_projection_result_receipts`是完整result receipt的唯一durable carrier。Applied receipt
持有documents/mutations；superseded receipt只引用exact applied receipt，不复制其完整内容。
Target head、job state、pre-activation outcome和Inspector只保存
`DurableProjectionResultReceiptReferenceFact`，必须通过receipt table exact-read后使用。

`result_semantic_fingerprint`只覆盖provider-independent projection content semantic：

- source projection fingerprint；
- ordered document semantic fingerprints；
- ordered canonical mutation semantic fingerprints。

它不得覆盖mutation ID、source owner、ordering、surface delivery、artifact locator、target head或
physical receipt。Receipt fingerprint才覆盖这些durable attribution joins。

Applied receipt要求candidate/effective source完全相等；superseded receipt要求effective applied
receipt source sequence严格更大。Superseded receipt只证明旧candidate被合法吸收，不声称其
prepared output曾写入target。Superseded receipt中央factory还必须exact-read handler contract并
证明`target_update_policy == full_replacement`；`single_assignment`不能构造该branch，即使caller
提供了结构上合法的source sequence与applied receipt。

Applied receipt owner必须等于本次job或pre-activation attempt；superseded receipt保存candidate
owner，并通过`effective_applied_result_receipt_reference`取得真正effective owner/content。

Deterministic IDs：

```text
applied receipt ID
    = "projection-result-receipt:" + H(
          projection kind,
          target key,
          source event reference fingerprint,
          result semantic fingerprint,
      )

superseded receipt ID
    = "projection-result-receipt:" + H(
          projection kind,
          target key,
          candidate owner fingerprint,
          candidate source event reference fingerprint,
          effective applied receipt fingerprint,
      )
```

Receipt insert只允许insert-or-exact-confirm；同ID不同内容是result authority conflict。

`DurableProjectionResultReceiptReferenceFact`由完整receipt中央factory唯一派生，只保存receipt
ID/fingerprint；projection kind、target、candidate/effective source、result semantic和branch都不在
引用中复制第二份。Consumer必须exact-read immutable receipt后再验证：

- applied receipt的candidate/effective source完全相等；
- superseded receipt的candidate fields来自被终结owner，effective fields与nested applied receipt
  完全相等；
- effective `result_semantic_fingerprint`来自applied receipt；
- superseded candidate尚未执行handler时，不伪造candidate result semantic；
- target head只能引用applied receipt；job/pre-activation outcome可以引用两种branch。

### 5.9 Lease、prepared result 与 settlement

Worker从repository取得的borrower-scoped lease：

```python
class LeasedDurableProjectionJob(FrozenFactBase):
    schema_version: Literal[
        "leased_durable_projection_job.v1"
    ] = "leased_durable_projection_job.v1"
    job: DurableProjectionJobSemanticFact
    job_candidate_fingerprint: str
    activation_fingerprint: str
    seed_contract_fingerprint: str
    delivery_policy: DurableProjectionDeliveryPolicyFact
    canonical_mutation_surface_plan: "CanonicalMutationSurfacePlanFact"
    expected_state_revision: int
    repair_generation: int
    attempt_count: int
    lease_generation: int
    lease_owner_id: str
    lease_expires_at: datetime
    lease_fingerprint: str
```

它是process-local handle，但字段全部来自durable row。Repository settlement必须同时验证：

- `job_id`；
- job semantic fingerprint；
- job candidate fingerprint；
- activation/seed contract fingerprints；
- expected state revision；
- lease generation；
- owner ID；
- lease fingerprint。

统一prepared output：

```python
class PreparedDurableProjectionArtifactDocumentFact(FrozenFactBase):
    schema_version: Literal[
        "prepared_durable_projection_artifact_document.v1"
    ] = "prepared_durable_projection_artifact_document.v1"
    document_kind: Literal["artifact"]
    semantic_document_id: str
    document_semantic_fingerprint: str
    media_type: str
    content_codec_contract_fingerprint: str
    metadata_contract_fingerprint: str
    content_sha256: str
    content_utf8_bytes: int
    canonical_content_utf8: str
    artifact_reference: DurableContentAddressedArtifactReferenceFact
    document_fingerprint: str


class PreparedDurableProjectionGraphDocumentFact(FrozenFactBase):
    schema_version: Literal[
        "prepared_durable_projection_graph_document.v1"
    ] = "prepared_durable_projection_graph_document.v1"
    document_kind: Literal["graph_document"]
    graph_id: str
    semantic_document_id: str
    graph_document_type: str
    document_semantic_fingerprint: str
    canonical_json_sha256: str
    canonical_json_utf8_bytes: int
    canonical_json_utf8: str
    jsonld_codec_contract_fingerprint: str
    document_fingerprint: str


class PreparedDurableProjectionGraphRelationFact(FrozenFactBase):
    schema_version: Literal[
        "prepared_durable_projection_graph_relation.v1"
    ] = "prepared_durable_projection_graph_relation.v1"
    document_kind: Literal["graph_relation"]
    relation_reference: DurableProjectionGraphRelationReferenceFact
    source_authority_fingerprint: str
    relation_fingerprint: str


PreparedDurableProjectionDocumentFact = (
    PreparedDurableProjectionArtifactDocumentFact
    | PreparedDurableProjectionGraphDocumentFact
    | PreparedDurableProjectionGraphRelationFact
)


class PreparedDurableProjectionResultFact(FrozenFactBase):
    schema_version: Literal[
        "prepared_durable_projection_result.v1"
    ] = "prepared_durable_projection_result.v1"
    result_owner: DurableProjectionResultOwner
    result_semantic: DurableProjectionResultSemanticFact
    ordered_documents: tuple[
        PreparedDurableProjectionDocumentFact, ...
    ]
    canonical_mutation_candidates: tuple[
        "CanonicalMutationCandidateFact", ...
    ]
    prepared_result_fingerprint: str
```

Prepared result在第一次await前必须是owned deep immutable carrier。`canonical_content_utf8`受各handler
output hard bound；不得引用稍后仍可修改的dict/model实例。

构造顺序唯一冻结为：

```text
prepared documents + pure CanonicalMutationSemanticFact
-> DurableProjectionResultSemanticFact
-> ProjectionResultCanonicalMutationOwnerFact(result semantic fingerprint)
-> CanonicalMutationCandidateFact
-> PreparedDurableProjectionResultFact
```

Factory必须重算并验证：

- job owner exact join leased job ID/semantic/candidate/source；
- pre-activation owner只允许DPJ2 transitional hook contract，且该kind尚无durable activation；
- every prepared document恰好匹配artifact/graph-document/graph-relation一个branch，unknown kind或
  nullable cross-branch field不可编码；
- artifact prepared/reference media type、codec、metadata与content-addressed digest exact join；
- graph relation prepared/reference与closed lowering contract exact join；
- documents的ordered semantic fingerprints与result semantic完全相等；
- candidates中的ordered `mutation_semantic_fingerprint`与result semantic完全相等；
- 每个candidate owner引用该result semantic fingerprint；
- `prepared_result_fingerprint`覆盖完整result semantic、documents和candidates；
- mutation candidate/receipt attribution不得反向进入result semantic fingerprint。

Commit outcome：

```python
class DurableProjectionCommitConfirmation(StrEnum):
    FULL = "full"
    NONE = "none"
    CONFLICT = "conflict"
    UNRESOLVED = "unresolved"


class DurableProjectionSettlementOutcome(FrozenFactBase):
    schema_version: Literal[
        "durable_projection_settlement_outcome.v1"
    ] = "durable_projection_settlement_outcome.v1"
    confirmation: DurableProjectionCommitConfirmation
    job_id: str
    attempted_lease_fingerprint: str
    resulting_status: DurableProjectionJobStatus | None
    resulting_state_revision: int | None
    resulting_repair_generation: int | None
    result_receipt_reference: (
        DurableProjectionResultReceiptReferenceFact | None
    )
    failure: BoundedRuntimeFailureDiagnosticFact | None
    outcome_fingerprint: str
```

Invariants：

- `FULL` required resulting status/state revision/repair generation；
- `FULL + SUCCEEDED|SUPERSEDED` required result receipt reference；
- `NONE`禁止resulting state；
- `CONFLICT` required authority diagnostic，不自动retry；
- `UNRESOLVED`表示deadline内无法证明，不得改写为conflict或success。

---

## 6. Stable cutover 与 seed checkpoint

### 6.1 为什么不自动 backfill 所有历史 projection

Pre-D3 execution evidence 使用：

- random UUID；
- projection-time wall clock；
- process-local hook ordering。

若 migration 后对所有历史 `ToolResultEndEvent` 自动重放，将产生第二套不同 identity 的 evidence，
无法证明是 repair 还是 duplicate。

因此 V1 对每个 projection kind使用独立 explicit cutover：

- 每个kind只admit其自身post-cutover source events；
- timeline/evidence可在不同deployment独立hard cut；
- existing pending canonical mutation outbox 是既有 durable obligation，必须迁移并继续；
- Inspector 对 pre-cutover projection 显示 `not_durably_observable`，不得猜测。

### 6.2 Seed contract 与 kind activation authority

```python
class DurableProjectionTriggerBindingFact(FrozenFactBase):
    schema_version: Literal[
        "durable_projection_trigger_binding.v1"
    ] = "durable_projection_trigger_binding.v1"
    projection_kind: DurableProjectionKind
    trigger_event_type: str
    accepted_event_schema_fingerprints: tuple[str, ...]
    target_resolver_id: str
    target_resolver_version: str
    target_resolver_contract_fingerprint: str
    binding_fingerprint: str


class DurableProjectionSeedContractFact(FrozenFactBase):
    schema_version: Literal[
        "durable_projection_seed_contract.v1"
    ] = "durable_projection_seed_contract.v1"
    projection_kind: DurableProjectionKind
    handler_contract: DurableProjectionHandlerContractFact
    delivery_policy: DurableProjectionDeliveryPolicyFact
    canonical_mutation_surface_plan: "CanonicalMutationSurfacePlanFact"
    ordered_trigger_bindings: tuple[
        DurableProjectionTriggerBindingFact, ...
    ]
    source_query_contract_fingerprint: str
    candidate_factory_contract_fingerprint: str
    seed_contract_fingerprint: str


class DurableProjectionKindActivationSemanticFact(FrozenFactBase):
    schema_version: Literal[
        "durable_projection_kind_activation_semantic.v1"
    ] = "durable_projection_kind_activation_semantic.v1"
    activation_id: str
    projection_kind: DurableProjectionKind
    seed_contract: DurableProjectionSeedContractFact
    activation_policy: Literal["post_cutover_events_only"]
    activation_semantic_fingerprint: str


class DurableProjectionKindActivationFact(FrozenFactBase):
    schema_version: Literal[
        "durable_projection_kind_activation.v1"
    ] = "durable_projection_kind_activation.v1"
    activation_semantic: DurableProjectionKindActivationSemanticFact
    activation_migration_version: int
    resulting_migration_registry_prefix_fingerprint: str
    activation_fingerprint: str
```

`durable_projection_kind_activations` 是active kind的唯一durable registry：

- v7只插入`run_timeline.v1`；
- v8只插入`tool_result_execution_evidence.v1`；
- row immutable，runtime role无INSERT/UPDATE/DELETE；
- composition root必须让每个durable-active kind恰好匹配一个executable；DPJ1允许显式
  `shadow_only` executable，但它没有seed/claim admission且不得写production target；
- unknown active kind、缺少historical handler或contract fingerprint不同都禁止Host admission；
- V1中seed contract完整冻结surface plan；composition改变不能悄悄改变已激活kind的job candidate。

V1两个generic projection kind的canonical mutation surface plan都固定为
`(oxigraph.v1,)`，与当前runtime-semantic mutation行为一致。PostgreSQL graph/artifact是projection
commit transaction的一部分，不是async surface；search/vector只由明确的memory mutation producer
请求。Durable composition本来就要求Oxigraph，不能因某次worker启动时endpoint缺失把plan改为空。

V1 active kind在database lifetime内不可原地改变或删除。改变trigger、handler semantic、delivery
policy或surface plan需要后续独立hard cut：新增versioned kind，并同时冻结old-kind
deactivation、pending-job drain与target handoff。本文只为v1保留identity空间，不定义该升级算法。
仅修改physical worker并发数、poll interval等不进入candidate的service policy不需要新kind。

Activation semantic与migration attribution严格分离：

- `activation_id = "projection-activation:" + H(projection kind, seed contract fingerprint,
  activation policy)`；
- packaged migration resource只保存
  `DurableProjectionKindActivationSemanticFact`；
- migration definition/contract覆盖该semantic resource fingerprint；
- registry计算出本migration resulting prefix后，runner才构造外层activation fact；
- resulting registry prefix不得反向进入activation semantic或migration definition fingerprint；
- outer activation fingerprint可以覆盖semantic + migration version + resulting prefix。

### 6.3 Cutover row

对应activation migration为每个existing runtime session、projection kind写一条immutable row：

```python
class DurableProjectionSessionCutoverFact(FrozenFactBase):
    schema_version: Literal[
        "durable_projection_session_cutover.v1"
    ] = "durable_projection_session_cutover.v1"
    runtime_session_id: str
    projection_kind: DurableProjectionKind
    cutover_through_sequence: int
    cutover_ledger_continuity_accumulator: str
    cutover_ledger_payload_prefix_bytes: int
    cutover_transcript_semantic_prefix_count: int
    cutover_transcript_semantic_prefix_accumulator: str
    migration_version: int
    migration_registry_prefix_fingerprint: str
    activation_fingerprint: str
    seed_contract_fingerprint: str
    cutover_policy_id: Literal["post_cutover_events_only"]
    cutover_fingerprint: str
```

规则：

- existing session：kind activation migration以当时exact ledger head为cutover；
- new session：session row与所有active-kind sequence-0 cutover rows同transaction创建；
- cutover row immutable；
- runtime role 不得 UPDATE/DELETE；
- active kind missing cutover是authority failure；尚未activation的kind允许没有row。
- activation/seed contract必须exact-read
  `durable_projection_kind_activations`，不得由当前代码自报。

Physical cutover row只保存上述authority fields，不接受caller-supplied
`cutover_fingerprint`。Repository read时由中央factory重算fact/fingerprint。New-session bootstrap
使用固定 `INSERT ... SELECT` 从immutable kind activation rows写canonical sequence-0 constants，
API不接收kind、contract、high-water或accumulator参数；architecture guard禁止其他production
cutover insert。Physical PK为
`(runtime_session_id, projection_kind)`。

### 6.3.1 Database-scoped runtime admission epoch

Offline/restart部署不是authority。V5必须安装database-scoped write barrier，V6及以后migration
必须使用它证明old/new producer不并发。

```python
class RuntimeWriteAdmissionMode(StrEnum):
    NORMAL = "normal"
    MAINTENANCE = "maintenance"


class RuntimeWriteAdmissionEpochFact(FrozenFactBase):
    schema_version: Literal[
        "runtime_write_admission_epoch.v1"
    ] = "runtime_write_admission_epoch.v1"
    database_target_fingerprint: str
    epoch_number: int
    mode: RuntimeWriteAdmissionMode
    authorized_runtime_role: str
    active_migration_registry_prefix_fingerprint: str
    protected_relation_registry_fingerprint: str
    maintenance_operation_id: str | None
    target_migration_version: int | None
    state_revision: int
    epoch_fingerprint: str


class RuntimeWriteMaintenanceAuthorityFact(FrozenFactBase):
    schema_version: Literal[
        "runtime_write_maintenance_authority.v1"
    ] = "runtime_write_maintenance_authority.v1"
    maintenance_operation_id: str
    database_target_fingerprint: str
    expected_normal_epoch_fingerprint: str
    maintenance_epoch_fingerprint: str
    target_migration_version: int
    authority_fingerprint: str


class RuntimeWriteProtectedRelationFact(FrozenFactBase):
    schema_version: Literal[
        "runtime_write_protected_relation.v1"
    ] = "runtime_write_protected_relation.v1"
    schema_name: str
    relation_name: str
    allowed_normal_operations: tuple[
        Literal["insert", "update", "delete"], ...
    ]
    allowed_maintenance_operations: tuple[
        Literal["insert", "update", "delete"], ...
    ]
    owning_write_domains: tuple[str, ...]
    guard_trigger_name: str
    guard_trigger_contract_fingerprint: str
    relation_fingerprint: str


class RuntimeWriteProtectedRelationRegistryFact(FrozenFactBase):
    schema_version: Literal[
        "runtime_write_protected_relation_registry.v1"
    ] = "runtime_write_protected_relation_registry.v1"
    registry_version: str
    ordered_relations: tuple[RuntimeWriteProtectedRelationFact, ...]
    relation_count: int
    relation_identity_accumulator: str
    production_dml_inventory_fingerprint: str
    registry_fingerprint: str


class RuntimeWriteAdmissionGuard(Protocol):
    @property
    def admission_epoch(self) -> RuntimeWriteAdmissionEpochFact:
        ...

    @property
    def transaction_owner_id(self) -> str:
        ...

    @property
    def guard_lock_identity_fingerprint(self) -> str:
        ...

    @property
    def maintenance_authority_fingerprint(self) -> str | None:
        ...
```

`RuntimeWriteAdmissionGuard`只是process-local mirror；PostgreSQL trigger才是最终authority。
V5冻结以下可执行协议。

Database-local physical authorities：

```text
runtime_write_admission_epochs
runtime_write_guard_secrets
runtime_write_protected_relations
```

`runtime_write_guard_secrets`保存migration runner生成的256-bit nonce及admin/runtime role binding。
Runtime role没有`SELECT/INSERT/UPDATE/DELETE`；只有固定`SECURITY DEFINER` guard functions可读取。
Nonce、physical advisory-lock key和backend PID不进入schema/job semantic fingerprint。

Lock keys由SQL guard owner使用HMAC-SHA-256(secret, canonical fields)截取signed int64生成：

```text
B = lock_key("runtime-write-barrier", database OID)

T = lock_key(
      "runtime-write-token",
      database OID,
      epoch number,
      mode,
      registry prefix,
      protected relation registry fingerprint,
      maintenance operation ID or null,
    )
```

Closed SQL functions：

```text
pulsara_acquire_normal_runtime_write_guard(
    expected_epoch_fingerprint,
    expected_registry_prefix
)

pulsara_acquire_maintenance_runtime_write_guard(
    maintenance_operation_id,
    expected_epoch_fingerprint
)

pulsara_enter_runtime_write_maintenance(...)
pulsara_install_runtime_write_normal_epoch(...)
```

Normal transaction：

1. function required `current_user == authorized_runtime_role`；
2. `pg_advisory_xact_lock_shared(B)`；
3. `SELECT epoch ... FOR SHARE`；
4. exact validate `mode=normal`、epoch fingerprint与verified registry prefix；
5. `pg_advisory_xact_lock_shared(T)`；
6. 返回epoch/guard-lock identity，provider据此构造borrower/transaction-scoped opaque guard；
7. EventLog/session/artifact/graph/memory/index/canonical mutation/pre-activation/job/seed/surface writer
   全部required消费该guard。

Maintenance transition：

1. admin-only function取得`pg_advisory_xact_lock(B)`，等待所有normal/maintenance shared holders；
2. lock exact normal epoch，CAS成绑定target migration与operation ID的maintenance epoch；
3. commit后ordinary runtime role即使持有旧process guard也无法取得new `T`；
4. drain/migration write transaction只能由admin role调用maintenance guard function；
5. function取得shared `B`，exact validatecurrent maintenance epoch/op，再取得maintenance `T`；
6. final activation/migration使用exclusive `B`等待所有maintenance writers，安装new normal epoch；
7. migration失败保持maintenance；
8. `abort-maintenance`只在ledger/catalog/target migration均证明NONE时安装新normal epoch number。

每个protected relation的`BEFORE INSERT OR UPDATE OR DELETE` trigger调用固定
`pulsara_assert_runtime_write_guard()`。该security-definer function：

- exact-readcurrent epoch/role；
- 从secret重算`B/T`；
- 查询`pg_locks`，要求`pid = pg_backend_pid()`且两把transaction advisory locks均granted；
- 按relation registry与operation验证normal/maintenance permission matrix；
- maintenance mode required exact operation ID；
- 不读取custom GUC、application name、caller token、SQL comment或process-local字符串。

Session-level伪造旧normal lock不能越过epoch，因为每个epoch的`T`不同；runtime role既不能读取secret，
也不能调用maintenance function。直接调用PostgreSQL advisory-lock builtin即使造成DoS，也无法推导
maintenance `T`获得写authority。

Protected relation不是“至少这些表”的手工清单。V5 packaged
`RuntimeWriteProtectedRelationRegistryFact`必须与以下集合exact set-equality：

```text
all relations with runtime-role INSERT/UPDATE/DELETE grant
UNION every relation reached by a production DML owner
MINUS migration-ledger/admin-only relations
```

AST + SQL-resource inventory、grant manifest、expected catalog与durable registry四方重算
`production_dml_inventory_fingerprint`。包括session/EventLog/account/checkpoint、artifact/tool artifact、
graph documents、immutable relation facts、memory nodes/relations/search/vector、candidate/governance、
provider-input/terminal-monitor、canonical mutation及projection job全部relations。后续migration新增
runtime-writable relation时，必须在同一migration先追加registry entry + trigger再开放grant；遗漏、
重复或多余entry都使schema verification fail closed。

Registry按schema head immutable/versioned，不原位改写。V6删除legacy outbox时安装新的v6 registry；
new normal epoch引用v6 fingerprint。V7/V8若relation/permission set未变，可以继续引用v6 registry，
但必须exact验证。Trigger只接受current epoch绑定的registry；historical registry只供maintenance
verification/replay。

Architecture guard仍要求production writer通过typed guard port；database trigger负责阻止旧binary、
漏接线writer与stale transaction真正提交。

成功取得maintenance row的exclusive transition本身证明此前持有shared epoch lock的database write
transaction已经归零。尚在process-local compute但未admit write的旧callback不再是authority：
它之后无法提交，offline drain从canonical source补齐其结果。

### 6.3.2 Unique RuntimeSession owner bootstrap

所有session创建必须经过唯一transaction-aware port：

```python
class RuntimeSessionOwnerSemanticFact(FrozenFactBase):
    schema_version: Literal[
        "runtime_session_owner_semantic.v1"
    ] = "runtime_session_owner_semantic.v1"
    runtime_session_id: str
    workspace_root: str | None
    owner_semantic_fingerprint: str


class RuntimeSessionOwnerBootstrapCandidateFact(FrozenFactBase):
    schema_version: Literal[
        "runtime_session_owner_bootstrap_candidate.v1"
    ] = "runtime_session_owner_bootstrap_candidate.v1"
    session_owner: RuntimeSessionOwnerSemanticFact
    expected_admission_epoch_fingerprint: str
    candidate_fingerprint: str


class RuntimeSessionBootstrapStateFact(FrozenFactBase):
    schema_version: Literal[
        "runtime_session_bootstrap_state.v1"
    ] = "runtime_session_bootstrap_state.v1"
    session_owner: RuntimeSessionOwnerSemanticFact
    ordered_active_cutover_fingerprints: tuple[str, ...]
    ordered_pre_activation_cutover_fingerprints: tuple[str, ...]
    cutover_set_accumulator: str
    admission_epoch_fingerprint: str
    state_fingerprint: str


class RuntimeSessionBootstrapCommitOutcomeFact(FrozenFactBase):
    schema_version: Literal[
        "runtime_session_bootstrap_commit_outcome.v1"
    ] = "runtime_session_bootstrap_commit_outcome.v1"
    confirmation: DurableProjectionCommitConfirmation
    attempted_candidate_fingerprint: str
    resulting_state: RuntimeSessionBootstrapStateFact | None
    physical_disposition: Literal["inserted", "exact_confirmed"] | None
    failure: BoundedRuntimeFailureDiagnosticFact | None
    outcome_fingerprint: str


class RuntimeSessionOwnerBootstrapPort(Protocol):
    def bootstrap(
        self,
        *,
        candidate: RuntimeSessionOwnerBootstrapCandidateFact,
        deadline_monotonic: float,
    ) -> RuntimeSessionBootstrapCommitOutcomeFact:
        ...
```

Port是唯一允许`INSERT INTO sessions`的production module：

- new row、全部active-kind durable cutover及全部not-yet-active pre-activation cutover同transaction；
- bootstrap只拥有immutable session identity/workspace binding与cutovers，不写mutable manifest
  metadata；manifest owner必须在bootstrap FULL后的独立existing-row UPDATE中写metadata；
- existing row必须验证required cutover set完整后才允许manifest metadata update或parent-row ensure；
- active/pre-activation同kind双authority、missing cutover、workspace identity conflict均fail closed；
- `RuntimeSessionBootstrapStateFact`只描述最终session + cutover bundle；首次insert和后来
  exact-confirm必须得到相同state fingerprint；
- `RuntimeSessionOwnerSemanticFact`是session row immutable identity的完整carrier；
  `owner_semantic_fingerprint = H(runtime_session_id, canonical workspace_root)`，禁止只保存一个
  无法重建字段的裸hash；
- `physical_disposition`是operational outcome，不进入stable state；
- port独占transaction/commit confirmation；外部caller不得把session insert嵌进自己的独立
  transaction；
- `host/session_manifest.py`、`event_log/postgres.py`与
  `memory/canonical/unit_of_work.py`必须在各自domain transaction前调用同一port，FULL后只更新/
  ensure existing session，不再拥有insert fallback；
- AST/SQL architecture guard禁止其他production Python出现`INSERT INTO sessions`；
- migration SQL和该唯一bootstrap repository是精确allowlist。

Commit/confirmation algorithm：

1. 第一次await前冻结candidate及expected final state identity；
2. verified provider开启normal-guard transaction；
3. lock session ID，insert-or-exact-confirm session row；
4. 从durable active/pre-activation registries执行固定`INSERT ... SELECT`；
5. exact-read完整cutover set并构造stable state；
6. commit；
7. commit明确成功才返回`FULL`；
8. commit exception、connection loss、caller cancellation或unknown outcome后，physical bootstrap
   owner保留candidate，并在同一absolute deadline内使用新verified connection exact-confirm：
   - session row +完整cutover set + epoch等于candidate预期：`FULL`；
   - session/cutover均不存在且epoch仍是expected normal：`NONE`；
   - partial row、额外/缺失cutover、workspace/epoch冲突：`CONFLICT`；
   - deadline内无法读取完整authority：`UNRESOLVED`。

`BaseException`不得绕过confirmation。Waiter cancellation只detach；process-owned operation继续到
FULL/NONE/CONFLICT/UNRESOLVED。`NONE`重试必须复用同一candidate；`UNRESOLVED`禁止三个caller继续
各自创建session。并发bootstrap中，一个attempt可报告`inserted`，其余报告`exact_confirmed`，但
它们的resulting stable state必须byte-identical。

### 6.4 Projection checkpoint

每个 session/kind 使用独立checkpoint：

```text
run_timeline.v1
    -> "durable_projection_event_seed.run_timeline.v1"

tool_result_execution_evidence.v1
    -> "durable_projection_event_seed.tool_result_execution_evidence.v1"
```

checkpoint state：

```python
class DurableProjectionSeedStateFact(FrozenFactBase):
    schema_version: Literal[
        "durable_projection_seed_state.v1"
    ] = "durable_projection_seed_state.v1"
    runtime_session_id: str
    projection_kind: DurableProjectionKind
    cutover_fingerprint: str
    through_sequence: int
    ledger_continuity_accumulator: str
    ledger_payload_prefix_bytes: int
    transcript_semantic_prefix_count: int
    transcript_semantic_prefix_accumulator: str
    admitted_job_candidate_count: int
    admitted_job_candidate_accumulator: str
    seed_contract_fingerprint: str
    state_fingerprint: str
```

Genesis 是该kind cutover，不一定是 sequence 0。不同kind不得共享checkpoint、candidate
accumulator或failure row。

禁止 caller-provided arbitrary base。Repository 必须：

- exact-read immutable cutover；
- checkpoint absent 时仅接受 canonical cutover state；
- checkpoint present 时复用 `runtime_projection_checkpoints` 的 base/current/prefix validation；
- state high-water 与 EventLog prefix exact join。

### 6.5 Trigger registry

V1：

| Seed lane / job kind | Trigger event type | Target | Activation |
|---|---|---|---|
| `run_timeline.v1` | `ReplyEndEvent` | runtime session + run | migration v7 |
| `run_timeline.v1` | `RunErrorEvent` | runtime session + run | migration v7 |
| `run_timeline.v1` | `RunEndEvent` | runtime session + run | migration v7 |
| `tool_result_execution_evidence.v1` | `ToolResultEndEvent` | runtime session + run + tool call | migration v8 |

表中每一项必须来自activation row内的
`DurableProjectionTriggerBindingFact`，不能另建process-local trigger真源。

Unknown trigger binding、historical decoder mismatch 或同一source event产生重复job identity：

- 不推进 checkpoint；
- 安装 seeder authority failure；
- 不静默跳过。

同一target出现不同source event不属于seed duplicate。Seeder必须为每个exact trigger admission
deterministic job；`full_replacement`由target policy排序，`single_assignment`则让第二个source在
target commit中形成durable authority conflict。禁止在seed阶段按target key去重，从而隐藏冲突的
第二个`ToolResultEndEvent`。

Seeder failure carrier：

```python
class DurableProjectionSeedFailureFact(FrozenFactBase):
    schema_version: Literal[
        "durable_projection_seed_failure.v1"
    ] = "durable_projection_seed_failure.v1"
    failure_id: str
    runtime_session_id: str
    projection_kind: DurableProjectionKind
    activation_fingerprint: str
    expected_seed_state_fingerprint: str | None
    blocked_from_sequence: int
    blocked_through_sequence: int
    observed_scan_horizon: DurableProjectionLedgerHorizonFact | None
    failure_kind: Literal[
        "active_cutover_missing",
        "ledger_account_missing",
        "ledger_account_prefix_conflict",
        "source_authority_conflict",
        "historical_decoder_unavailable",
        "trigger_contract_mismatch",
        "job_identity_conflict",
    ]
    conflicting_source_event_reference_fingerprint: str | None
    diagnostic: BoundedRuntimeFailureDiagnosticFact
    seed_contract_fingerprint: str
    failure_fingerprint: str
```

它写入 `durable_projection_seed_failures`，以
deterministic `failure_id` 为PK。ID覆盖session、kind、activation、expected state或null、
blocked range与failure kind。该row存在时：

- 本session该kind后续sequence不得越过；
- 其他session仍可seed；
- 同session其他kind仍可seed；
- automatic retry只允许再次exact-confirm同一failure；
- repair必须typed CAS绑定failure fingerprint；
- transient database/network unavailable不伪造成durable authority failure。

```python
class DurableProjectionSeedRepairActionFact(FrozenFactBase):
    schema_version: Literal[
        "durable_projection_seed_repair_action.v1"
    ] = "durable_projection_seed_repair_action.v1"
    repair_action_id: str
    runtime_session_id: str
    projection_kind: DurableProjectionKind
    expected_seed_failure_fingerprint: str
    expected_seed_state_fingerprint: str | None
    action: Literal[
        "retry_after_authority_repair",
        "reverify_after_schema_repair",
    ]
    authority_references: tuple[
        "DurableRepairAuthorityReferenceFact", ...
    ]
    repair_generation: int
    predecessor_repair_action_fingerprint: str | None
    action_fingerprint: str


class DurableProjectionSeedFailureResolutionFact(FrozenFactBase):
    schema_version: Literal[
        "durable_projection_seed_failure_resolution.v1"
    ] = "durable_projection_seed_failure_resolution.v1"
    seed_failure_fingerprint: str
    repair_action_fingerprint: str
    resulting_seed_state_fingerprint: str
    resolved_through_sequence: int
    resolution_fingerprint: str
```

Seed repair只能重新处理同一blocked range；V1禁止`skip_sequence`或手工推进checkpoint。
`active_cutover_missing`只能由privileged schema/activation repair收口，runtime repair port不得创建
replacement cutover。`observed_scan_horizon=None`只允许在完整catalog/ledger读取已经证明prefix
authority损坏时；transient read failure仍不得写durable failure。

Repair generation从1开始且严格连续；generation 1 predecessor为空，后续required exact predecessor。
同一failure/generation最多一个action，concurrent operator command使用CAS，不能创建平行repair
branches。

Failure/resolution都是immutable：

- active failure = 没有exact resolution的failure row；
- repair action只授权一次重新处理，不直接隐藏failure；
- repaired seed commit candidate必须携带exact repair action fingerprint；
- jobs + checkpoint + failure resolution同一transaction FULL；
- resolution的resulting state必须覆盖原blocked range；
- row永久保留供Inspector审计，不使用DELETE或mutable `resolved=true`。

### 6.6 Seeder batch bounds

V1：

```text
maximum source events per batch = 512
maximum source payload bytes per batch = 8 MiB
maximum jobs per batch = 512
```

若单个 source event 超出 storage hard bound，属于 EventLog corruption，不是 projection dead-letter。
若一次最多512条的读取结果累计超过8 MiB，Seeder必须选择同时满足event/byte上限的**最长非空
前缀**并提交；剩余合法events留给下一页。不得因为多条各自合法的events累计超过page bound就写
`source_authority_conflict`。

Seeder 每批 commit 后让出 event loop；不得在 publisher callback 内执行。

### 6.7 Seed commit candidate

```python
class DurableProjectionSeedCommitCandidateFact(FrozenFactBase):
    schema_version: Literal[
        "durable_projection_seed_commit_candidate.v1"
    ] = "durable_projection_seed_commit_candidate.v1"
    runtime_session_id: str
    projection_kind: DurableProjectionKind
    expected_seed_state: DurableProjectionSeedStateFact
    resulting_seed_state: DurableProjectionSeedStateFact
    scan_horizon: DurableProjectionLedgerHorizonFact
    repaired_seed_failure_fingerprint: str | None
    seed_repair_action_fingerprint: str | None
    ordered_job_candidates: tuple[DurableProjectionJobCandidateFact, ...]
    source_event_count: int
    source_payload_bytes: int
    candidate_fingerprint: str


class DurableProjectionSeedFailureCommitCandidateFact(FrozenFactBase):
    schema_version: Literal[
        "durable_projection_seed_failure_commit_candidate.v1"
    ] = "durable_projection_seed_failure_commit_candidate.v1"
    runtime_session_id: str
    projection_kind: DurableProjectionKind
    activation_fingerprint: str
    expected_seed_state_fingerprint: str | None
    failure: DurableProjectionSeedFailureFact
    candidate_fingerprint: str


DurableProjectionSeedWriteCandidate = (
    DurableProjectionSeedCommitCandidateFact
    | DurableProjectionSeedFailureCommitCandidateFact
)


class DurableProjectionSeedCommitOutcome(FrozenFactBase):
    schema_version: Literal[
        "durable_projection_seed_commit_outcome.v1"
    ] = "durable_projection_seed_commit_outcome.v1"
    confirmation: DurableProjectionCommitConfirmation
    attempted_candidate_fingerprint: str
    committed_seed_state_fingerprint: str | None
    committed_seed_failure_fingerprint: str | None
    committed_seed_failure_resolution_fingerprint: str | None
    committed_job_ids: tuple[str, ...]
    failure: BoundedRuntimeFailureDiagnosticFact | None
    outcome_fingerprint: str


class DurableProjectionSeedCommitPort(Protocol):
    def commit(
        self,
        *,
        candidate: DurableProjectionSeedWriteCandidate,
        admission_guard: RuntimeWriteAdmissionGuard,
        deadline_monotonic: float,
    ) -> DurableProjectionSeedCommitOutcome:
        ...
```

Factory必须证明：

- expected state等于repository current checkpoint或canonical cutover genesis；
- candidate/state/cutover projection kind完全相等；
- each job candidate activation/seed contract与cutover、durable kind activation完全相等；
- resulting state high-water等于scan horizon；
- ordered job candidates恰好等于该kind在本delta的trigger输出；
- 每个job的trigger horizon严格等于其source event sequence，并从该event row重建；
- admitted candidate count/accumulator从expected state与ordered candidates增量重算；
- zero-job delta仍推进ledger high-water，但candidate accumulator保持不变；
- candidate不嵌raw event payload。
- repair fields必须同时为空或同时存在；存在时exact join active failure/action，且commit构造
  `DurableProjectionSeedFailureResolutionFact`。

Failure candidate必须CAS同一activation与expected checkpoint/cutover状态。Ordinary `FULL`
outcome中committed seed state与failure fingerprint恰有一个非空；repaired `FULL`允许state +
resolution fingerprint，禁止同时写新failure。Failure commit禁止同时插job或推进checkpoint。
`NONE`三者都为空。`CONFLICT|UNRESOLVED`不能伪造成durable authority failure。

PostgreSQL commit port接收完整candidate，不接受“jobs tuple + independently supplied checkpoint”
两个平行参数。它必须在一个verified connection/transaction内读写EventLog prefix、
`runtime_projection_checkpoints`、jobs与seed failures；禁止调用会自行checkout/commit的现有
checkpoint convenience API。

### 6.8 Session enumeration

Worker 必须按active projection kind分别paged枚举：

```text
fast lane:
sessions
LEFT JOIN durable_projection_session_cutovers
LEFT JOIN runtime_projection_checkpoints
LEFT JOIN ledger_materialization_accounts
WHERE cutover.projection_kind = :active_kind
  AND (
      checkpoint missing
      OR ledger account missing
      OR ledger_materialization_accounts.ledger_through_sequence > seed head
  )
ORDER BY cutover.projection_kind, session id
LIMIT bounded_page

mandatory integrity lane:
durable_projection_kind_activations CROSS JOIN sessions
LEFT JOIN cutovers/accounts/checkpoints
paged by stable (projection_kind, session_id) cursor
-> exact-read EventLog head/prefix regardless of account value
-> compare account/checkpoint/cutover
```

禁止 `tuple(event_log.iter())` 或全量 session materialization。

Account head只用于behind-session acceleration，不是source proof。每批seeder仍必须exact-read
`agent_events` horizon/prefix。Account head、EventLog head或checkpoint不一致时写seed authority
failure，不得取三者最大值继续。

Fast lane不得成为唯一discovery owner。Integrity lane必须round-robin最终覆盖所有active
session/kind；process restart可从cursor genesis重新开始，但不能永久只扫描有backlog的热点session。
Missing cutover、missing account或EventLog/account prefix conflict都必须进入typed authority
failure/Host health，不得因SQL inner join而消失。

Production seeder保存process-local稳定keyset cursor
`(runtime_session_id, projection_kind)`，每页最多256个authority；到达尾页后从genesis
wrap-around。Cursor只影响扫描调度，不进入job/seed semantic。单个validly-addressed
session/kind在contract resolution、source preparation或authority validation中发生确定性失败时，
service必须冻结并提交该authority自己的`DurableProjectionSeedFailureCommitCandidateFact`，然后继续
本页后续authority；只有failure candidate本身无法FULL确认时才允许整轮fail closed。禁止每轮回到
固定首页，也禁止一个坏authority永久阻塞其后的session。

Lost wake 时 polling 最迟在 configured poll interval 内发现 behind session。

---

## 7. Job repository 与 state machine

### 7.1 SQL record shape

`durable_projection_jobs` 至少包含：

```text
job_id text primary key
projection_kind text not null
target_key text not null
runtime_session_id text not null
run_id text not null
source_event_id text not null
source_sequence bigint not null
source_event_type text not null
source_reference jsonb not null
trigger_horizon jsonb not null
handler_contract jsonb not null
handler_contract_fingerprint text not null
activation_fingerprint text not null
seed_contract_fingerprint text not null
delivery_policy jsonb not null
delivery_policy_fingerprint text not null
canonical_mutation_surface_plan jsonb not null
canonical_mutation_surface_plan_fingerprint text not null
job_semantic_fingerprint text not null
job_candidate_fingerprint text not null

status text not null
state_revision bigint not null
repair_generation bigint not null
attempt_count integer not null
lease_generation bigint not null
lease_owner_id text null
lease_expires_at timestamptz null
next_attempt_at timestamptz null
last_failure jsonb null
result_receipt_reference jsonb null
state_fingerprint text not null

created_at timestamptz not null
updated_at timestamptz not null
```

Required unique/index：

- PK `job_id`；
- unique `(projection_kind, source_event_id, target_key)`；
- claim index `(status, next_attempt_at, created_at, job_id)`；
- source index `(runtime_session_id, source_sequence)`；
- target index `(projection_kind, target_key, source_sequence)`；
- lease expiry index for `LEASED`。

`durable_projection_result_receipts` 至少包含：

```text
receipt_id text primary key
receipt_kind text not null
projection_kind text not null
target_key text not null
candidate_source_sequence bigint not null
effective_source_sequence bigint not null
result_semantic_fingerprint text not null
receipt_payload jsonb not null
receipt_fingerprint text not null
created_at timestamptz not null
```

Required unique/index：

- unique applied `(projection_kind, target_key, effective_source_sequence)`；
- target/source index `(projection_kind, target_key, candidate_source_sequence)`；
- immutable row：runtime role无UPDATE/DELETE；
- scalar/payload/fingerprint由central receipt factory重算。

`durable_projection_target_authority_conflicts`至少包含：

```text
conflict_id text primary key
projection_kind text not null
target_key text not null
candidate_source_sequence bigint not null
existing_target_head_fingerprint text not null
conflict_payload jsonb not null
conflict_fingerprint text not null
created_at timestamptz not null
```

Required index `(projection_kind, target_key, created_at, conflict_id)`。该表immutable，runtime role无
UPDATE/DELETE；target claim、read facade、coverage与Inspector都必须检查该target是否存在conflict
row，不能仅查看job dead-letter。

`created_at/updated_at` 是 operational attribution，不进入 semantic fingerprint。

Indexed scalar columns是nested source/job facts的materialized copies，不是第二真源。Repository与
schema CHECK必须验证：

- source event ID/sequence/type等于`source_reference`对应字段；
- trigger horizon sequence等于source sequence，且prefix字段等于source stored row；
- runtime session/run等于source reference；
- projection kind/target/handler contract/fingerprint等于job semantic factory结果；
- activation/seed fingerprints exact join session cutover与durable kind activation；
- delivery policy fingerprint等于stored complete delivery policy；
- surface plan fingerprint等于stored complete surface plan；
- terminal state的result receipt reference可exact-read immutable receipt；
- operational state scalars与state fingerprint重算一致。

任一不一致分类为repository authority conflict，不得只相信更方便查询的scalar。

### 7.2 State transitions

```text
PENDING
  -> LEASED(g)

RETRY_WAIT
  -> LEASED(g + 1), when database-time >= next_attempt_at

LEASED(g)
  -> SUCCEEDED
  -> SUPERSEDED
  -> RETRY_WAIT
  -> DEAD_LETTER

LEASED(g), lease expired
  -> LEASED(g + 1)

DEAD_LETTER
  -> RETRY_WAIT, only with typed retry repair action
  -> SUPERSEDED, only with exact newer target-head repair proof
```

禁止：

- terminal -> pending 直接 UPDATE；
- lease settlement 不检查 generation/token；
- process monotonic time决定跨进程 lease；
- claim transaction 持有期间执行 handler I/O。

Typed repair transition是唯一terminal exception：

- CAS exact job semantic/state revision/repair generation；
- 同transaction插入repair action；
- increment `repair_generation`；
- `retry_same_contract`重置本generation `attempt_count = 0`并进入`RETRY_WAIT`；
- old failure保留在current state与repair action lineage，直到新attempt产生新outcome；
- manual supersession required exact newer target head/result receipt reference且handler policy为
  `full_replacement`；`single_assignment`不能以repair改成另一个source；
- `SUCCEEDED`不得repair或重开。

### 7.3 Claim algorithm

Target-scoped execution lease：

```python
class DurableProjectionTargetExecutionLeaseFact(FrozenFactBase):
    schema_version: Literal[
        "durable_projection_target_execution_lease.v1"
    ] = "durable_projection_target_execution_lease.v1"
    projection_kind: DurableProjectionKind
    target_key: str
    owner_job_id: str
    owner_source_sequence: int
    lease_generation: int
    lease_owner_id: str
    lease_expires_at: datetime
    state_revision: int
    lease_fingerprint: str
```

它是operational scheduling authority，不进入job/result semantic。Claim transaction先按
`(projection_kind, target_key)`锁execution lease row：

- active unexpired lease存在时不claim该target第二个job；
- 无active lease时按handler target policy选择：
  - `full_replacement`只允许当前eligible source sequence最大的job取得lease；
  - `single_assignment`在无head时只允许source sequence最小的首次assignment候选取得lease；已有
    head时只有exact same source candidate可进入exact-confirm，其他candidate直接进入target
    authority conflict settlement，不执行handler；
- job lease与target lease使用同一owner ID/generation/expiry并同transaction安装；
- settle/relinquish同transaction释放target lease；
- worker crash后两者一起按database time expiry/reclaim；
- 只有`full_replacement`可以把older jobs逐个写superseded receipt；
- `single_assignment`禁止supersede：第二个distinct source必须dead-letter并把target health标记
  `authority_untrusted`。

一个 claim transaction 必须先在SQL层构造eligible target集合，再应用global limit：

```sql
WITH due_jobs AS (
    SELECT ...,
           row_number() OVER (
               PARTITION BY projection_kind, target_key
               ORDER BY next_attempt_at NULLS FIRST, created_at, job_id
           ) AS target_row_ordinal
    FROM durable_projection_jobs
    WHERE <due-or-expired>
),
eligible_targets AS (
    SELECT projection_kind, target_key, min(<due-order>) AS first_due
    FROM due_jobs
    WHERE NOT EXISTS (<active target lease>)
      AND NOT EXISTS (<target authority conflict>)
    GROUP BY projection_kind, target_key
    ORDER BY first_due, projection_kind, target_key
    LIMIT :bounded_target_window
)
SELECT due_jobs.*
FROM due_jobs
JOIN eligible_targets USING (projection_kind, target_key)
WHERE target_row_ordinal <= :bounded_per_target_window
FOR UPDATE SKIP LOCKED;
```

随后：

- exact-confirm/allocate target execution lease；
- increment `attempt_count`；
- increment `lease_generation`；
- install stable worker attempt ID；
- set expiry using database clock；
- recompute operational state fingerprint；
- commit；
- transaction 外执行 handler。

若eligible row的`attempt_count >= maximum_attempts`，claim transaction不再启动handler，而是
写 `DEAD_LETTER(attempts_exhausted)`。`next_attempt_at`使用database
`clock_timestamp()`计算，不使用worker wall clock。

### 7.4 Cancellation

Worker task cancellation：

- waiter cancellation 不取消 shared physical attempt；
- Host close 停止新 claim；
- in-flight handler 获得 bounded drain deadline；
- 可安全停止时显式 relinquish 为 `RETRY_WAIT`；
- 无法确认 settlement 时保持 lease，等待 expiry/recovery；
- 禁止把 cancellation 当作 success。

### 7.5 Failure classification

Closed reasons：

```python
class DurableProjectionFailureKind(StrEnum):
    TRANSIENT_STORAGE_UNAVAILABLE = "transient_storage_unavailable"
    TRANSIENT_EXTERNAL_SURFACE_UNAVAILABLE = "transient_external_surface_unavailable"
    DEADLINE_EXCEEDED = "deadline_exceeded"
    SOURCE_NOT_READY = "source_not_ready"
    SOURCE_AUTHORITY_CONFLICT = "source_authority_conflict"
    TARGET_AUTHORITY_CONFLICT = "target_authority_conflict"
    REPOSITORY_AUTHORITY_CONFLICT = "repository_authority_conflict"
    HISTORICAL_DECODER_UNAVAILABLE = "historical_decoder_unavailable"
    HANDLER_CONTRACT_MISMATCH = "handler_contract_mismatch"
    PROJECTION_INPUT_OVERSIZE = "projection_input_oversize"
    PROJECTION_OUTPUT_OVERSIZE = "projection_output_oversize"
    ATTEMPTS_EXHAUSTED = "attempts_exhausted"
    RESULT_IDENTITY_CONFLICT = "result_identity_conflict"
    EXTERNAL_SURFACE_CONTRACT_MISMATCH = "external_surface_contract_mismatch"
```

Retryable：

- transient storage/external unavailable；
- deadline exceeded；
- source not ready only when exact source已经引用另一个durable、non-terminal physical owner，
  且该owner仍可合法完成。

固定source horizon内缺失event、非法tool pairing、artifact/content digest冲突，或被引用owner已经
terminal而结果仍不存在，都不是`SOURCE_NOT_READY`；它们必须分类为source authority conflict。

Immediate dead-letter：

- source authority conflict；
- target/repository authority conflict；
- decoder unavailable；
- handler mismatch；
- input/output oversize；
- attempts exhausted；
- result identity conflict；
- surface contract mismatch。

Dead-letter 不终结 Host run，不修改 EventLog；它使 projection health degraded。

### 7.6 Diagnostic sanitization

Job row只保存 `BoundedRuntimeFailureDiagnosticFact`。

必须使用 closed sanitizer registry：

```text
projection-storage-error.v1
projection-external-surface-error.v1
projection-contract-error.v1
```

禁止：

- fallback `str(error)`；
- DSN；
- authorization header；
- raw tool result；
- raw user text；
- unbounded traceback。

最大 message 2048 UTF-8 bytes；完整 operator diagnostic 仅进受控 local log。

---

## 8. Target head 与 out-of-order completion

同一 run 可以快速产生多个 timeline jobs。旧 job 可能晚于新 job完成。

`durable_projection_target_heads`：

```python
class DurableProjectionTargetHeadFact(FrozenFactBase):
    schema_version: Literal[
        "durable_projection_target_head.v1"
    ] = "durable_projection_target_head.v1"
    projection_kind: DurableProjectionKind
    target_key: str
    applied_source_sequence: int
    applied_source_event_reference_fingerprint: str
    applied_result_receipt_reference: (
        DurableProjectionResultReceiptReferenceFact
    )
    head_revision: int
    head_fingerprint: str


class DurableProjectionTargetAuthorityConflictFact(FrozenFactBase):
    schema_version: Literal[
        "durable_projection_target_authority_conflict.v1"
    ] = "durable_projection_target_authority_conflict.v1"
    conflict_id: str
    projection_kind: DurableProjectionKind
    target_key: str
    target_update_policy: DurableProjectionTargetUpdatePolicy
    conflict_kind: Literal[
        "distinct_source_for_single_assignment",
        "same_source_different_result",
        "target_receipt_rebind_conflict",
    ]
    candidate_source_event_reference_fingerprint: str
    candidate_source_sequence: int
    candidate_result_semantic_fingerprint: str | None
    existing_target_head_fingerprint: str
    existing_applied_result_receipt_reference: (
        DurableProjectionResultReceiptReferenceFact
    )
    handler_contract_fingerprint: str
    conflict_fingerprint: str
```

Commit algorithm：

1. validate owner：lock job exact lease，或exact rebind active pre-activation contract/epoch guard；
2. lock target head；
3. exact-read handler target update policy；
4. compare source sequence/result；
5. apply policy-specific matrix。

`full_replacement` matrix：

| Condition | Outcome |
|---|---|
| no head | commit applied receipt + head revision 1 + `SUCCEEDED` |
| candidate source > head | commit applied receipt + advance head + `SUCCEEDED` |
| candidate source < head | commit superseded receipt; no target mutation |
| same source and same result | exact-confirm applied receipt + `SUCCEEDED` |
| same source and different result | `RESULT_IDENTITY_CONFLICT` dead-letter |

`SUPERSEDED` 是成功 terminal disposition，不消耗 retry。

`single_assignment` matrix：

| Condition | Outcome |
|---|---|
| no head | commit applied receipt + head revision 1 + `SUCCEEDED` |
| same source event and same result | exact-confirm applied receipt + `SUCCEEDED` |
| same source event and different result | `RESULT_IDENTITY_CONFLICT` dead-letter |
| any different source event, regardless of sequence/result | `TARGET_AUTHORITY_CONFLICT` dead-letter + target `authority_untrusted` |

Evidence不得产生superseded receipt。即使第二个`ToolResultEndEvent`拥有更高sequence并生成相同result
semantic，也不是同一terminal fact的retry，必须fail closed。

`authority_untrusted`不是process-local bool。Conflict settlement必须与job dead-letter在同一
transaction insert-or-exact-confirm immutable
`durable_projection_target_authority_conflicts` row：

```text
conflict_id = "projection-target-conflict:" + H(
    projection kind,
    target key,
    conflict kind,
    candidate source event reference fingerprint,
    candidate result semantic fingerprint or null,
    existing target head fingerprint,
    existing applied result receipt fingerprint,
    handler contract fingerprint,
)
```

`distinct_source_for_single_assignment`要求policy为`single_assignment`，candidate result必须为空：
handler根本不得执行。`same_source_different_result`要求candidate result非空。
`target_receipt_rebind_conflict`只在完整receipt/head exact-read后证明矛盾时生成。任一active conflict
row都会使该target及其projection health成为`authority_untrusted`并阻止后续handler execution；
V1没有自动resolution或“选择一个结果”路径，只允许reset或后续独立offline authority-repair规格。

“same result”不能只比较一个caller-provided hash：commit port必须通过target head的receipt
reference exact-read immutable applied receipt、documents、canonical mutation references与content
digests，并由中央factory重算。Fingerprint相同但receipt/row/artifact缺失或不一致是target
authority conflict，不得exact-confirm。

Applied/superseded result receipt、PostgreSQL result documents、canonical mutation rows、target head
与job settlement必须在同一transaction。Pre-activation commit同样必须写receipt；process-local
outcome不是durable carrier。

### 8.1 DPJ2 transitional pre-activation owner

V6已经删除legacy outbox，但timeline/evidence durable admission分别到v7/v8才切换。因此DPJ2必须
先把两个旧trigger迁到deterministic V2 output UOW，不能保留split write。

```python
class PreActivationProjectionHookContractSemanticFact(FrozenFactBase):
    schema_version: Literal[
        "pre_activation_projection_hook_contract_semantic.v1"
    ] = "pre_activation_projection_hook_contract_semantic.v1"
    projection_kind: DurableProjectionKind
    hook_contract_id: str
    hook_contract_version: str
    handler_contract: DurableProjectionHandlerContractFact
    delivery_policy: DurableProjectionDeliveryPolicyFact
    canonical_mutation_surface_plan: "CanonicalMutationSurfacePlanFact"
    ordered_trigger_bindings: tuple[
        DurableProjectionTriggerBindingFact, ...
    ]
    source_query_contract_fingerprint: str
    prepared_result_factory_contract_fingerprint: str
    contract_semantic_fingerprint: str


class PreActivationProjectionHookContractFact(FrozenFactBase):
    schema_version: Literal[
        "pre_activation_projection_hook_contract.v1"
    ] = "pre_activation_projection_hook_contract.v1"
    contract_semantic: PreActivationProjectionHookContractSemanticFact
    installation_migration_version: int
    resulting_migration_registry_prefix_fingerprint: str
    contract_fingerprint: str


class PreActivationProjectionSessionCutoverFact(FrozenFactBase):
    schema_version: Literal[
        "pre_activation_projection_session_cutover.v1"
    ] = "pre_activation_projection_session_cutover.v1"
    runtime_session_id: str
    projection_kind: DurableProjectionKind
    pre_activation_contract_fingerprint: str
    cutover_through_sequence: int
    cutover_ledger_continuity_accumulator: str
    cutover_ledger_payload_prefix_bytes: int
    cutover_transcript_semantic_prefix_count: int
    cutover_transcript_semantic_prefix_accumulator: str
    migration_version: int
    migration_registry_prefix_fingerprint: str
    cutover_fingerprint: str


class PreActivationProjectionTargetCoverageItemFact(FrozenFactBase):
    schema_version: Literal[
        "pre_activation_projection_target_coverage_item.v1"
    ] = "pre_activation_projection_target_coverage_item.v1"
    projection_kind: DurableProjectionKind
    target_key: str
    latest_trigger_event_reference: DurableProjectionSourceEventReferenceFact
    applied_result_receipt_reference: (
        DurableProjectionResultReceiptReferenceFact
    )
    item_fingerprint: str


class PreActivationProjectionCoveragePageFact(FrozenFactBase):
    schema_version: Literal[
        "pre_activation_projection_coverage_page.v1"
    ] = "pre_activation_projection_coverage_page.v1"
    runtime_session_id: str
    projection_kind: DurableProjectionKind
    page_index: int
    previous_page_fingerprint: str | None
    ordered_items: tuple[
        PreActivationProjectionTargetCoverageItemFact, ...
    ]
    item_count: int
    item_accumulator: str
    canonical_utf8_bytes: int
    page_fingerprint: str


class PreActivationProjectionCoverageSetReferenceFact(FrozenFactBase):
    schema_version: Literal[
        "pre_activation_projection_coverage_set_reference.v1"
    ] = "pre_activation_projection_coverage_set_reference.v1"
    page_count: int
    target_count: int
    ordered_page_fingerprint_accumulator: str
    ordered_target_item_accumulator: str
    last_page_fingerprint: str | None
    reference_fingerprint: str


class PreActivationProjectionCoverageReceiptFact(FrozenFactBase):
    schema_version: Literal[
        "pre_activation_projection_coverage_receipt.v1"
    ] = "pre_activation_projection_coverage_receipt.v1"
    coverage_receipt_id: str
    runtime_session_id: str
    projection_kind: DurableProjectionKind
    pre_activation_contract_fingerprint: str
    start_cutover_fingerprint: str
    frozen_horizon: DurableProjectionLedgerHorizonFact
    scanned_trigger_event_count: int
    scanned_trigger_event_accumulator: str
    target_coverage_set: PreActivationProjectionCoverageSetReferenceFact
    maintenance_operation_id: str
    maintenance_authority_fingerprint: str
    receipt_fingerprint: str


class PreActivationProjectionCommitOutcomeFact(FrozenFactBase):
    schema_version: Literal[
        "pre_activation_projection_commit_outcome.v1"
    ] = "pre_activation_projection_commit_outcome.v1"
    confirmation: DurableProjectionCommitConfirmation
    attempted_result_owner_fingerprint: str
    result_receipt_reference: (
        DurableProjectionResultReceiptReferenceFact | None
    )
    resulting_target_head_fingerprint: str | None
    failure: BoundedRuntimeFailureDiagnosticFact | None
    outcome_fingerprint: str


class PreActivationProjectionCommitPort(Protocol):
    def commit(
        self,
        *,
        prepared_result: PreparedDurableProjectionResultFact,
        admission_guard: RuntimeWriteAdmissionGuard,
        deadline_monotonic: float,
    ) -> PreActivationProjectionCommitOutcomeFact:
        ...
```

Port只接受`PreActivationHookResultOwnerFact`，并在一个transaction内完成：

```text
exact source rebind
-> target-head CAS
-> artifact/graph documents
-> V2 canonical mutation base + surface deliveries
-> immutable result receipt
-> target head
```

它不写job或假造job settlement。`NONE/UNRESOLVED`仍由旧hook的process-local diagnostic承担，
所以D3尚未关闭；这只是保证v6之后没有split output或legacy mutation writer。

Composition guard：

- `durable_projection_pre_activation_contracts`是唯一contract authority；v6 migration用packaged
  semantic resource构造timeline/evidence两条immutable outer rows；
- v6为每个existing session/kind写
  `durable_projection_pre_activation_session_cutovers`；v6之后、对应activation之前创建的新session
  与仍active的pre-activation kind sequence-0 cutover同transaction写入；
- 新session的sequence-0 row仍引用安装该contract的v6 migration version/prefix；它证明的是
  pre-activation authority来源，不伪装成session creation时的新migration；
- executable hook/handler/port必须exact match该row；
- kind没有durable activation、source sequence严格大于其session pre-activation cutover时，
  closed pre-activation hook contract可使用；
- v7 timeline activation FULL后，timeline pre-activation owner构造立即fail closed；
- v8 evidence activation FULL后，evidence pre-activation owner构造立即fail closed；
- target head保留historical owner fact；后续timeline job可按full-replacement policy推进，evidence
  job只允许same-source exact-confirm；
- pre-activation callback丢失process-local outcome时，offline drain从deterministic receipt ID +
  target head exact-confirm，不重新猜测结果；
- production中不存在同时合法的pre-activation与job owner。

V7/V8 activation migration分别验证：

- activation handler、delivery policy、trigger binding、source query与surface semantics和对应
  pre-activation contract完全相等；durable seed candidate factory是activation新增的admission
  owner，不与旧hook伪造等价identity；
- old hook admission已经关闭且没有in-flight callback/UOW；
- privileged pre-activation drain已从v6/session cutover扫描到exact current ledger head；
- timeline target head覆盖该range内latest trigger；evidence target在range内至多一个trigger且head
  exact覆盖它；range内没有trigger时不要求head；
- activation cutover head等于drain frozen head，二者之间禁止event admission；
- activation FULL后该kind pre-activation contract仅作historical attribution，不再授权execution。

Pre-activation drain使用相同exact source readers和commit port，paged扫描、stable target去重，并在
offline deadline耗尽时停止而不推进activation。它不是普通Host hook，也不允许跳过failed target；
下次命令从target heads重新exact-confirm。只有全部session/kind coverage postcondition成立，
activation migration才可commit。

Timeline pre-activation owner从DPJ2起就必须使用第9节同一persistent reducer与target-head delta
reader。它不得在v6->v7过渡期继续每个ReplyEnd从run genesis重建；否则activation前的合法长run仍会
保留O(n²)路径，且coverage receipt无法证明与v7 handler相同的result contract。

Drain对每个session/kind完成后写一条immutable、content-addressed coverage receipt：

- range严格为`(pre-activation cutover, frozen_horizon]`；
- trigger accumulator覆盖range内全部该kind trigger；
- 每个target item保存policy-resolved effective trigger + exact applied result receipt reference；
- timeline只在handler contract声明full replacement时按target折叠到latest trigger；
- evidence每个tool-result target独立出现，且range内同一target必须恰好一个source event；发现第二个
  source event时写authority conflict并禁止coverage receipt；
- page最多256 targets / 8 MiB canonical JSON，receipt只保存page-root/count/accumulator；
- page/receipt写入前在同一maintenance epoch内re-read EventLog horizon与target receipts；
- 任一missing/conflicting target、superseded-only receipt或unresolved pre-activation attempt都禁止
  coverage receipt；
- receipt不是caller的“done”声明，activation validator可从EventLog trigger accumulator、
  immutable result receipts和content-addressed pages重算。

`durable_projection_pre_activation_coverage_pages`与
`durable_projection_pre_activation_coverage_receipts`只允许admin maintenance owner写入。
相同receipt ID/page fingerprint只允许exact-confirm。

Stable identities：

```text
coverage page key
    = page.page_fingerprint

coverage receipt ID
    = "pre-activation-coverage:" + H(
          runtime session ID,
          projection kind,
          pre-activation contract fingerprint,
          start cutover fingerprint,
          frozen horizon fingerprint,
          scanned trigger count,
          scanned trigger accumulator,
          target coverage set reference fingerprint,
          maintenance operation ID,
          maintenance authority fingerprint,
      )
```

Page partition按`target_key`升序、固定256-item split冻结；retry不得按当前fetch page重新分片。
Receipt中的maintenance operation ID必须与maintenance authority exact join，不能由caller传入自由
字符串。相同maintenance operation/range/root在drain crash后必须得到byte-identical page keys、
receipt ID与fingerprint；新maintenance operation即使coverage内容相同也创建不同attribution receipt，
旧receipt不得跨operation授权activation。

唯一公开运维入口冻结为：

```text
pulsara db projections drain-pre-activation \
    --kind run_timeline.v1|tool_result_execution_evidence.v1 \
    --deadline-seconds <bounded>
```

命令要求Host/event admission已经停止，取得verified schema/admin authority，并拒绝未知kind、
未安装contract、已激活kind或仍有in-flight hook owner。它不写caller自报的“drain complete”行；
后续activation migration必须从EventLog frozen heads和target heads独立重算coverage。

---

## 9. Run timeline vertical hard cut

### 9.1 Target-head driven incremental source

Timeline不得为每个trigger从run genesis重读全历史。新增paged EventLog port与persistent reducer state：

```python
class RunTimelineReducerBaseFact(FrozenFactBase):
    schema_version: Literal[
        "run_timeline_reducer_base.v1"
    ] = "run_timeline_reducer_base.v1"
    base_kind: Literal["genesis", "applied_result"]
    runtime_session_id: str
    run_id: str
    base_through_sequence: int
    base_applied_result_receipt_reference: (
        DurableProjectionResultReceiptReferenceFact | None
    )
    base_state_semantic_fingerprint: str
    base_fingerprint: str


class RawRunProjectionSourcePage(FrozenFactBase):
    schema_version: Literal[
        "raw_run_projection_source_page.v1"
    ] = "raw_run_projection_source_page.v1"
    runtime_session_id: str
    run_id: str
    after_sequence_exclusive: int
    through_sequence_inclusive: int
    page_index: int
    previous_page_fingerprint: str | None
    ordered_stored_events: tuple[
        "DurableProjectionStoredEventFact", ...
    ]
    selected_event_count: int
    selected_payload_bytes: int
    selected_event_accumulator: str
    has_more: bool
    next_after_sequence: int | None
    page_fingerprint: str


class RunTimelinePersistentStateSemanticFact(FrozenFactBase):
    schema_version: Literal[
        "run_timeline_persistent_state_semantic.v1"
    ] = "run_timeline_persistent_state_semantic.v1"
    runtime_session_id: str
    run_id: str
    through_sequence: int
    status: str
    start_sequence: int
    end_sequence: int | None
    item_count: int
    ordered_item_semantic_accumulator: str
    persistent_item_vector_root_semantic_fingerprint: str
    open_item_state_semantic_fingerprint: str
    state_semantic_fingerprint: str


class PreparedRunTimelineProjectionFact(FrozenFactBase):
    schema_version: Literal[
        "prepared_run_timeline_projection.v1"
    ] = "prepared_run_timeline_projection.v1"
    reducer_base: RunTimelineReducerBaseFact
    trigger_event_reference: DurableProjectionSourceEventReferenceFact
    trigger_horizon: DurableProjectionLedgerHorizonFact
    resulting_state: RunTimelinePersistentStateSemanticFact
    ordered_source_page_fingerprint_accumulator: str
    ordered_new_vector_node_semantic_fingerprints: tuple[str, ...]
    manifest_document_semantic_fingerprint: str
    graph_head_document_semantic_fingerprint: str
    preparation_fingerprint: str
```

Stored event carrier：

```python
class DurableProjectionStoredEventFact(FrozenFactBase):
    schema_version: Literal[
        "durable_projection_stored_event.v1"
    ] = "durable_projection_stored_event.v1"
    event_reference: DurableProjectionSourceEventReferenceFact
    canonical_payload_json_utf8: str
    canonical_payload_utf8_bytes: int
    canonical_payload_sha256: str
    stored_event_fingerprint: str
```

它使用immutable UTF-8 string，不嵌mutable `dict`。Handler decode后必须：

- 使用event schema registry typed decode；
- 重算payload fingerprint；
- exact join event reference；
- 不把decoded mutable object跨第一次await保存为authority。

Port：

```python
class RunProjectionSourceReader(Protocol):
    def read_run_projection_source_page(
        self,
        *,
        runtime_session_id: str,
        run_id: str,
        after_sequence_exclusive: int,
        through_sequence_inclusive: int,
        page_index: int,
        previous_page_fingerprint: str | None,
        deadline_monotonic: float,
    ) -> RawRunProjectionSourcePage:
        ...
```

要求：

- SQL固定
  `run_id = ? AND sequence > after_sequence_exclusive AND sequence <= through_sequence_inclusive`；
- `through_sequence_inclusive`严格等于job trigger sequence；
- final page必须包含trigger ref；
- 不得调用无 high-water 的 `event_log.iter(run_id=...)`；
- handler 不得读取 trigger 后 event。

Base选择：

- target head不存在：canonical genesis，`base_through_sequence=0`；
- target head source `< trigger`：exact-read applied result receipt及其persistent reducer state；
- target head source `== trigger`：走same-source receipt exact-confirm，不执行handler；
- target head source `> trigger`：直接写superseded receipt，不执行handler；
- base receipt/state/artifact缺失或digest conflict是target authority conflict。

Timeline applied receipt必须恰好引用一个versioned timeline manifest document；manifest内嵌
`RunTimelinePersistentStateSemanticFact`并引用persistent-vector root/open-state carrier。
Reducer base只能从该committed manifest恢复，不能从current EventLog重新推测旧base，也不能从
target head里复制的fingerprint伪造state。

Persistent item vector使用绝对timeline item ordinal、固定leaf size和path-copy append。相同canonical
event prefix无论从genesis还是任意合法base恢复，都必须得到同一state/root fingerprint；leaf
partition不得受physical page、attempt或base选择影响。Open-item state是bounded typed document，
只保存尚未closed的reply/model/tool/plan lifecycle；其数量受现有runtime parallelism hard caps约束。

Timeline `source_projection_fingerprint`只覆盖job trigger event/reference、exact trigger horizon与
versioned source interpretation contract，不覆盖reducer base或source page partition。Base/page
chain只进入`PreparedRunTimelineProjectionFact.preparation_fingerprint`和result receipt attribution。

### 9.2 Per-page 与 persistent-state bounds

V1：

```text
source events per page = 512
source payload bytes per page = 8 MiB
timeline items per persistent-vector leaf = 128
timeline leaf canonical bytes = 1 MiB
new vector nodes per append = 64
open-item state canonical bytes = 1 MiB
timeline manifest canonical bytes = 256 KiB
timeline graph-head document bytes = 256 KiB
```

这些是单页/单node/resident-state bounds，不是整个合法run的第二窗口。只要每个canonical event符合
EventLog hard bound，run history增长不得触发`projection_input_oversize`。Handler逐页fold并释放
raw page；process resident data只包括one source page、open state、right spine和prepared new nodes。

超过单event、single item、single open-state或single leaf hard bound才是typed projection
oversize/contract failure。Logical timeline可以包含任意数量pages/leaves；读取API必须paged。

### 9.3 Deterministic assembly

Timeline handler 必须：

- 使用 trigger source sequence 作为 projection revision；
- 从applied target head state只fold
  `(base_through_sequence, trigger_sequence]`；
- manifest/root ID从
  `(runtime_session_id, run_id, source_sequence, contract)`确定性派生；
- graph ID 保持 run-stable；
- `created_at` 来自 canonical `RunStartEvent.created_at`；
- `updated_at` 来自 trigger event `created_at`；
- V1 scope从typed source固定为`"ctx:" + RunStartEvent.turn_id`，不得读取
  `LoopState.current_scope`；
- item ordering 只来自 event sequence；
- completed item进入persistent vector后不可被后续attempt原位改写；
- path-copy node identity由absolute ordinal range + child semantics派生；
- summary truncation/codec进入 handler contract；
- 禁止 `utc_now()` 和 UUID。

Timeline artifact hard cut为versioned persistent manifest + content-addressed item leaves，不再每次写
完整pretty JSON。`memory/foundation/run_timeline_query.py`、working-context与Inspector必须：

- exact-read manifest/root；
- bounded/paged hydrate leaves；
- summary只消费state/head和需要的bounded tail；
- working-context若在RunEnd hook时尚未看到异步timeline receipt，不得从EventLog重建；后续
  model compile执行bounded latest-RunEnd sparse read，并在receipt可见时lazy refresh；
- refresh由RuntimeSession-owned blocking-I/O owner执行；同一planned model-call index最多启动
  一次，baseline不得同步读取，phase restart复用同一attempt receipt；
- 只有显式export API可materialize full timeline，并要求caller提供output bound；
- 不得为普通recall/Inspector重新join全历史。

Canonical codec、leaf split、root accumulator、open-state lowering及export codec全部进入handler contract。
未来非默认scope必须先成为typed canonical event fact。

### 9.4 Atomic commit port

```python
class RunTimelineProjectionCommitPort(Protocol):
    def commit(
        self,
        *,
        leased_job: "LeasedDurableProjectionJob",
        timeline_preparation: PreparedRunTimelineProjectionFact,
        prepared_result: PreparedDurableProjectionResultFact,
        admission_guard: RuntimeWriteAdmissionGuard,
        deadline_monotonic: float,
    ) -> "DurableProjectionSettlementOutcome":
        ...
```

同一 PostgreSQL connection/transaction：

1. exact-confirm lease；
2. target-head CAS；
3. upsert artifact；
4. upsert PostgreSQL graph document；
5. append immutable canonical mutation；
6. create requested surface delivery rows；
7. insert/exact-confirm immutable result receipt；
8. update target head；
9. settle job。

`PostgresArtifactStore` 和 `PostgresGraphStore` 必须提供 transaction-aware port；
禁止 commit port 内重新 `psycopg.connect()`。

V1 production要求artifact、graph、job、target-head和canonical mutation base全部位于同一个
verified PostgreSQL physical target。若未来启用external artifact store，必须先为artifact
增加独立durable delivery owner；不能继续声称跨store atomic commit。

Job的 `SUCCEEDED` 只表示PostgreSQL canonical derived projection已经FULL。Search/vector/Oxigraph
是否已经materialize由surface delivery rows独立表示；两者不得混成同一个status。

Commit port还必须验证prepared result内每个mutation candidate：

- timeline preparation trigger/horizon严格等于leased job source/trigger horizon；
- persistent reducer base applied receipt可exact hydrate，或为canonical genesis；
- resulting state through sequence严格等于trigger sequence；
- source page chain连续覆盖`(base, trigger]`且不含trigger后event；
- source owner是当前projection job；
- surface plan fingerprint等于leased job冻结plan；
- mutation ordinal连续；
- mutation semantic与prepared documents一致；
- commit port分配ordering后才构造durable mutation document/reference。

Generic worker对`run_timeline.v1`使用target-scoped scheduling：

- 同一target最多一个active physical handler；
- claim时只选择该target当前eligible jobs中source sequence最大的job；
- latest job成功后，较旧jobs不执行handler，逐个写superseded receipt并terminalize；
- 新trigger在handler运行期间到达可以等待，不取消已admitted attempt；
- target scheduling不能持PostgreSQL row transaction跨source hydrate/compute。

这使正常长run的timeline fold总量近似随新增event线性增长，而不是每个ReplyEnd重复fold全部历史。

### 9.5 Production cut

完成本 vertical cut 后：

- 删除 `RunTimelinePersistenceHook` production registration；
- publisher 不再构造 timeline；
-旧 hook class可以先留在 historical/test module，一个阶段后物理删除；
- tests 改为 source event -> seeder -> worker -> result；
- publisher callback latency test证明没有 timeline I/O。

---

## 10. Tool-result execution evidence vertical hard cut

### 10.1 Source authority

一个 evidence job 必须 exact join：

- `ToolResultEndEvent`；
- tool call start/block identity；
- terminal projection；
- tool name；
- tool call ID；
- result semantic fingerprint；
- source run/session；
- relevant artifact references。

禁止按 call ID + “最近 event”模糊匹配。

```python
class ToolCallArgumentsEvidenceProjectionFact(FrozenFactBase):
    schema_version: Literal[
        "tool_call_arguments_evidence_projection.v1"
    ] = "tool_call_arguments_evidence_projection.v1"
    tool_call_start_reference: DurableProjectionSourceEventReferenceFact
    tool_call_end_reference: DurableProjectionSourceEventReferenceFact
    arguments_segment_count: int
    arguments_segment_reference_accumulator: str
    raw_arguments_json: str
    raw_arguments_json_sha256: str
    raw_arguments_json_utf8_bytes: int
    parse_disposition: Literal[
        "valid_object",
        "invalid_json",
        "non_object_json",
    ]
    parsed_arguments_object: dict[str, Any] | None
    parse_error_code: Literal[
        "json_decode_error",
        "top_level_non_object",
    ] | None
    canonical_arguments_json_sha256: str | None
    canonical_arguments_json_utf8_bytes: int | None
    bounded_input_summary: str
    bounded_input_summary_sha256: str
    summary_contract_fingerprint: str
    projection_fingerprint: str


class ToolResultEvidenceOutputProjectionFact(FrozenFactBase):
    schema_version: Literal[
        "tool_result_evidence_output_projection.v1"
    ] = "tool_result_evidence_output_projection.v1"
    result_state: "ToolResultState"
    result_semantic_fingerprint: str
    bounded_output_summary: str
    bounded_output_summary_sha256: str
    output_was_truncated: bool
    ordered_artifact_reference_fingerprints: tuple[str, ...]
    projection_contract_fingerprint: str
    projection_fingerprint: str


class ToolResultExecutionEvidenceSourceFact(FrozenFactBase):
    schema_version: Literal[
        "tool_result_execution_evidence_source.v1"
    ] = "tool_result_execution_evidence_source.v1"
    tool_result_start_reference: DurableProjectionSourceEventReferenceFact
    tool_result_end_reference: DurableProjectionSourceEventReferenceFact
    terminal_projection: "ToolResultTerminalProjectionEndReferenceFact"
    tool_call_arguments: ToolCallArgumentsEvidenceProjectionFact
    output_projection: ToolResultEvidenceOutputProjectionFact
    tool_call_id: str
    tool_name: str
    evidence_scope: str
    source_fingerprint: str
```

Factory 只能从 exact EventLog source snapshot 构造。

Factory必须：

- exact join `ToolCallStart -> arguments segments -> ToolCallEnd`；
- exact join `ToolResultStart -> ToolResultEnd`，并使用End内嵌的typed
  `ToolResultTerminalProjectionEndReferenceFact`；
- 所有refs的session/run/turn/reply/tool-call identity一致；
- terminal projection semantic join中的call ID、result state与result fingerprint一致；
- output projection的artifact tuple恰好等于
  `ToolResultEndEvent`/terminal projection的typed refs；
- arguments总是保留exact raw JSON及其digest；合法object才拥有parsed object与canonical JSON
  identity，malformed JSON和top-level non-object分别产生稳定parse disposition/error code，
  不能被归类为source authority corruption；
- input summary从canonical object JSON或invalid/non-object raw diagnostic按versioned bounded
  projection生成，不再读取`LoopState.pending_tool_calls`或scratchpad；
- output summary只从accepted terminal projection生成，不重新fold raw result deltas；
- V1 `evidence_scope = "ctx:" + turn_id`；未来非默认scope必须先成为typed canonical event fact，
  不能从`LoopState.current_scope`补写。

```python
class ToolResultExecutionEvidenceSourceReader(Protocol):
    def read_source(
        self,
        *,
        job: DurableProjectionJobSemanticFact,
        maximum_exact_event_reads: int,
        maximum_artifact_references: int,
        deadline_monotonic: float,
    ) -> ToolResultExecutionEvidenceSourceFact:
        ...
```

V1 bounds：

```text
maximum exact event reads = 64
maximum artifact references = 64
maximum canonical tool arguments = 128 KiB UTF-8
maximum bounded input summary = 2 KiB UTF-8
maximum bounded output summary = 500 Unicode codepoints / 2 KiB UTF-8
```

Reader必须使用stored refs/named sparse query并固定
`<= job.trigger_horizon.through_sequence == ToolResultEndEvent.sequence`。不得为了寻找
`ToolResultTerminalProjectionCommittedEvent`越过trigger；End内嵌reference就是V1 terminal
projection authority。
禁止对整轮EventLog做线性扫描。

V1 input summary是canonical arguments JSON本身。若其UTF-8大于2 KiB，保留能够让
`prefix + "..."`不超过2 KiB的最长完整Unicode codepoint prefix；不做pretty-print、key重排
以外的第二次自然语言摘要。Canonical JSON codec与boundary算法进入summary contract。

V1 output projection保持现有可见语义，但由中央pure factory唯一实现：

```text
accepted terminal text/data descriptors
-> concatenate in canonical block order
-> strip leading/trailing whitespace
-> <= 500 codepoints: keep
-> > 500 codepoints: first 497 codepoints + "..."
```

Data block只生成typed media/url/base64-size descriptor，不hydrate或持久化raw base64。
UTF-8仍超过2 KiB时使用同一factory按codepoint继续缩短到hard bound；实际boundary与
projection contract fingerprint一起冻结。

### 10.2 Deterministic identity

替换当前 UUID/wall-clock：

```text
evidence_document_id = H(
    "pulsara:tool-result-execution-evidence-document:v1",
    runtime_session_id,
    run_id,
    tool_call_id,
    result_semantic_fingerprint,
)
```

所有 node/edge IDs从上述 document identity + typed local key 派生。

Timestamp：

- tool start time来自 canonical call event；
- result time来自 `ToolResultEndEvent.created_at`；
- projection time只能进入 physical attribution，不进入 graph semantics。

Shared Turn/Artifact documents不得作为relation accumulator。V1新增immutable per-edge facts：

```python
class TurnProducedToolResultRelationFact(FrozenFactBase):
    schema_version: Literal[
        "turn_produced_tool_result_relation.v1"
    ] = "turn_produced_tool_result_relation.v1"
    relation_document_id: str
    graph_id: str
    turn_id: str
    predicate_iri: Literal["https://pulsara.dev/runtime#produced"]
    tool_result_document_id: str
    source_tool_result_end_reference_fingerprint: str
    relation_semantic_fingerprint: str


class ToolResultArtifactRelationFact(FrozenFactBase):
    schema_version: Literal[
        "tool_result_artifact_relation.v1"
    ] = "tool_result_artifact_relation.v1"
    relation_document_id: str
    graph_id: str
    tool_result_document_id: str
    predicate_iri: Literal["https://pulsara.dev/runtime#provides"]
    artifact_document_id: str
    artifact_semantic_reference_fingerprint: str
    artifact_role: str
    artifact_ordinal: int
    relation_semantic_fingerprint: str


class CanonicalGraphRelationLoweringContractFact(FrozenFactBase):
    schema_version: Literal[
        "canonical_graph_relation_lowering_contract.v1"
    ] = "canonical_graph_relation_lowering_contract.v1"
    contract_id: Literal["canonical-graph-relation-lowering.v1"]
    accepted_relation_schema_fingerprints: tuple[str, ...]
    postgres_relation_schema_fingerprint: str
    rdf_named_graph_codec_fingerprint: str
    jsonld_read_merge_contract_fingerprint: str
    owned_predicate_iris: tuple[str, ...]
    contract_fingerprint: str


class CanonicalGraphRelationRowFact(FrozenFactBase):
    schema_version: Literal[
        "canonical_graph_relation_row.v1"
    ] = "canonical_graph_relation_row.v1"
    relation_id: str
    graph_id: str
    relation_kind: Literal[
        "turn_produced_tool_result",
        "tool_result_provides_artifact",
    ]
    source_document_id: str
    predicate_iri: str
    target_document_id: str
    relation_semantic_fingerprint: str
    source_authority_fingerprint: str
    lowering_contract_fingerprint: str
    row_fingerprint: str


class CanonicalGraphRelationReadPageFact(FrozenFactBase):
    schema_version: Literal[
        "canonical_graph_relation_read_page.v1"
    ] = "canonical_graph_relation_read_page.v1"
    graph_id: str
    source_document_id: str
    predicate_iri: str | None
    after_relation_id: str | None
    ordered_relations: tuple[CanonicalGraphRelationRowFact, ...]
    relation_count: int
    relation_accumulator: str
    has_more: bool
    next_after_relation_id: str | None
    page_fingerprint: str


class CanonicalGraphNodeReadViewFact(FrozenFactBase):
    schema_version: Literal[
        "canonical_graph_node_read_view.v1"
    ] = "canonical_graph_node_read_view.v1"
    graph_id: str
    node_id: str
    base_document_semantic_fingerprint: str
    ordered_relation_semantic_accumulator: str
    merged_relation_count: int
    merged_canonical_json_utf8: str
    merged_canonical_json_sha256: str
    jsonld_read_merge_contract_fingerprint: str
    view_fingerprint: str
```

IDs从relation kind + graph + endpoint semantic identities + role/ordinal确定性派生。
`CanonicalGraphRelationLoweringContractFact`进入evidence handler/result semantic contract，V1
owned predicates恰好是`rt:produced`与`rt:provides`。

PostgreSQL lowering：

- evidence settlement必须先为Turn及每个referenced Artifact执行deterministic
  put-if-absent-or-confirm-identical base-document写入；fixture不得预建这些node来掩盖producer
  缺失；
- 同一evidence commit transaction将每个fact降为一条immutable `graph_relation_facts` row；
- PK `(graph_id, relation_id)`，unique
  `(graph_id, source_document_id, predicate_iri, target_document_id, relation_kind)`；
- insert-or-exact-confirm；runtime role无UPDATE/DELETE；
- `graph_documents`只保存node/base document，不再因新增edge覆盖Turn/ToolResult/Artifact；
- `storage/postgres_memory_projection.py`不得在document refresh时删除
  `graph_relation_facts`；legacy `memory_relations`与新relation table物理分离；
- canonical relation query对legacy embedded/document-projected edges与immutable rows做ordered
  union/dedupe，immutable row的semantic identity优先，冲突fail closed。

Oxigraph lowering：

```text
TurnProducedToolResult
    -> GRAPH <graph> { <turn> rt:produced <tool-result> }

ToolResultArtifact
    -> GRAPH <graph> { <tool-result> rt:provides <artifact> }
```

Surface handler只执行idempotent exact quad insert；禁止`DELETE WHERE { <source> ?p ?o }`，因为这会
删除其他job拥有的edge。Settlement receipt绑定graph/source/predicate/target与lowering contract。

Read contract hard cut：

- 新增`GraphStore.get_jsonld_read_view(node)`，返回relation-aware typed read view：base
  document + legacy embedded edge + `graph_relation_facts`/Oxigraph direct quads按
  predicate/target排序合并去重；
- canonical owner是`CanonicalGraphNodeReadViewFact`；既有`get_jsonld()`是兼容facade，只返回其
  `merged_canonical_json_utf8`解码后的owned deep copy；
- `GraphStore.get_base_jsonld_for_update()`是transaction-internal port，只返回base document；
- ordinary `put_jsonld()`若payload包含registry-owned `rt:produced`或`rt:provides`则fail closed；
  只有v8 migration/historical decoder可写legacy embedded form；
- relationship traversal使用paged `CanonicalGraphRelationReadPageFact`，不得靠hydrate整个Turn；
- convenience node view最多合并1024条edge；超过时返回typed
  `relation_view_requires_paged_hydration`，不得静默截断；
- relation-aware read view是deep immutable/read-only authority；它materialize出的dict再传给
  `put_jsonld()`会因owned predicate guard被拒绝；
- PostgreSQL与Oxigraph对同一source/predicate必须产生相同ordered target identity accumulator。

这样`get_jsonld(turn_id)`仍可看到`produced`，但该字段是只读合成结果，不再由Turn document拥有。
Duplicate edge exact-confirm；相同relation ID不同endpoint/content是result identity conflict。

V1 result document集合必须保持现有production语义：

1. 一个deterministic ToolResult document；
2. 每个exact artifact ref至多一个deterministic
   `ToolResultArtifactRelationFact`，不重写shared Artifact node；
3. exact `TurnProducedToolResultRelationFact`；
4. 对应runtime-semantic canonical mutation candidates。

既有ToolResult base document中的`rt:storedAs`只允许继续指向按现有contract选出的单个primary
artifact，并进入ToolResult document semantic；它不是全部artifact membership的累加器。全部
ordered artifact refs由immutable `rt:provides` relation facts拥有。没有primary artifact时禁止
伪造`rt:storedAs`；存在多个artifact时不得通过覆盖base document追加第二个`storedAs`。JSON-LD
codec必须继续将`rt:produced`与`rt:provides`按list语义物化，关系顺序由relation read contract
决定，而不是document insertion order。

Prepared/result receipt中每个relation必须使用
`PreparedDurableProjectionGraphRelationFact` /
`DurableProjectionGraphRelationReferenceFact`，并与typed source fact、PostgreSQL row及Oxigraph
quad exact join；不能把relation伪装成普通graph document。

本hard cut不自动创建claim/preference/evidence judgement；那些仍由其typed memory/governance
producer拥有。相同document ID出现不同semantic content是result identity conflict，不得last-write-wins。

### 10.3 Commit

同一 transaction：

1. validate exact lease；
2. target-head CAS；
3. write evidence documents及immutable relation facts；
4. exact-confirm referenced artifact identity，不覆盖shared Artifact document；
5. append canonical mutations；
6. create surface deliveries；
7. insert immutable result receipt；
8. settle job。

```python
class ToolResultExecutionEvidenceProjectionCommitPort(Protocol):
    def commit(
        self,
        *,
        leased_job: LeasedDurableProjectionJob,
        source: ToolResultExecutionEvidenceSourceFact,
        prepared_result: PreparedDurableProjectionResultFact,
        admission_guard: RuntimeWriteAdmissionGuard,
        deadline_monotonic: float,
    ) -> DurableProjectionSettlementOutcome:
        ...
```

Port必须验证
`source.source_fingerprint == prepared_result.result_semantic.source_projection_fingerprint`，
并在transaction内重新exact-read job source identity；process-local prepared result不能自证authority。
其mutation candidates同样必须绑定leased job source owner与frozen surface plan。

同一turn多个tool-result jobs必须可并行提交而不竞争一个mutable Turn JSON document。Concurrency
gate至少用两个barrier-synchronized jobs证明最终relation set完整，且任意commit顺序产生相同
relation semantic accumulator。

### 10.4 Production cut

切换后：

- `AgentRuntime` 删除 `tool_result_persistence_hook` field/call；
- `runtime/wiring.py` 不再构造 `ExecutionEvidencePersistenceHook`；
- production 不再写 `ToolResultEvidenceProjectionFailedEvent`；
- historical decoder保留；
- failure只进入 durable job state；
- run/model control不等待 evidence projection。

这一步关闭 D3 的 tool-result evidence ownership，但不关闭 D5 compaction candidate projection。

---

## 11. Canonical mutation outbox V2

### 11.1 Immutable mutation 与 mutable surface delivery分表

现有 payload 内 `surface_apply_status` 必须删除。

V2：

```python
class CanonicalMutationSemanticFact(FrozenFactBase):
    schema_version: Literal[
        "canonical_mutation_semantic.v2"
    ] = "canonical_mutation_semantic.v2"
    mutation_kind: "CanonicalMutationKind"
    graph_id: str
    graph_document_semantic_fingerprint: str
    mutation_payload: "CanonicalMutationPayloadCarrier"
    mutation_contract_fingerprint: str
    mutation_semantic_fingerprint: str


class CanonicalMutationCandidateFact(FrozenFactBase):
    schema_version: Literal[
        "canonical_mutation_candidate.v2"
    ] = "canonical_mutation_candidate.v2"
    mutation_id: str
    mutation_ordinal: int
    mutation_semantic: CanonicalMutationSemanticFact
    source_owner_fingerprint: str
    source_authority_fingerprints: tuple[str, ...]
    requested_surfaces: tuple["CanonicalMutationSurface", ...]
    surface_plan_fingerprint: str
    candidate_fingerprint: str


class CanonicalMutationDocumentFact(FrozenFactBase):
    schema_version: Literal[
        "canonical_mutation_document.v2"
    ] = "canonical_mutation_document.v2"
    candidate: CanonicalMutationCandidateFact
    ordering: "CanonicalMutationOrderingFact"
    mutation_fact_fingerprint: str
```

```python
class CanonicalMutationOrderingFact(FrozenFactBase):
    schema_version: Literal[
        "canonical_mutation_ordering.v1"
    ] = "canonical_mutation_ordering.v1"
    sequence_key: str
    sequence_number: int
    predecessor_mutation_id: str | None
    predecessor_ordering_fingerprint: str | None
    ordering_contract_fingerprint: str
    ordering_fingerprint: str


class CanonicalMutationSequenceHeadFact(FrozenFactBase):
    schema_version: Literal[
        "canonical_mutation_sequence_head.v1"
    ] = "canonical_mutation_sequence_head.v1"
    sequence_key: str
    last_mutation_sequence_number: int
    last_mutation_id: str
    last_ordering_fingerprint: str
    head_revision: int
    head_fingerprint: str
```

V1 `sequence_key = "graph:" + H(graph_id)`。同一graph内所有mutation严格排序；不同graph可以并行。

Allocation在producer outer transaction中：

1. deterministic mutation ID已存在时，exact-confirmcandidate并复用原ordering；
2. 否则lock `canonical_mutation_sequence_heads`；
3. allocate `sequence_number = previous + 1`；
4. bind exact predecessor；
5. insert mutation + advance sequence head；
6. outer transaction FULL才公开。

`sequence_number >= 1`；first mutation predecessor必须为空；后续必须exact引用前一head。
Ordering属于durable delivery causality，不进入candidate/`mutation_semantic_fingerprint`，但进入mutation fact与
surface delivery identity fingerprint。

`canonical_mutation_sequence_heads`完整保存
`CanonicalMutationSequenceHeadFact`，不能只保存一个裸counter。Allocation transaction必须验证
last mutation ID/ordering fingerprint/sequence与实际row exact join后才能分配下一号。

```python
class CanonicalMutationKind(StrEnum):
    GOVERNED_MEMORY = "governed_memory.v2"
    RUNTIME_SEMANTIC = "runtime_semantic.v2"
    GRAPH_RESET = "graph_reset.v2"
    GRAPH_DELETE = "graph_delete.v2"
```

New-production mutation ID：

```text
mutation_id = "canonical-mutation:" + H(
    "pulsara:canonical-mutation-id:v2",
    mutation_semantic.mutation_kind,
    source_owner_fingerprint,
    mutation_ordinal,
    mutation_semantic.graph_id,
    mutation_semantic.mutation_semantic_fingerprint,
)
```

`mutation_ordinal >= 0`，并在同一个source owner内唯一。Retry/restart必须生成同一ID；
禁止新production writer使用UUID outbox ID。
`LegacyCanonicalMutationOwnerFact`是唯一例外：migration保留原`outbox:*` ID，并由legacy payload
digest/owner fact证明；production factory拒绝创建该分支。

Payload carrier：

```python
class CanonicalInlineJsonDocumentFact(FrozenFactBase):
    schema_version: Literal[
        "canonical_inline_json_document.v1"
    ] = "canonical_inline_json_document.v1"
    canonical_json_utf8: str
    canonical_utf8_bytes: int
    canonical_sha256: str
    document_semantic_fingerprint: str


class CanonicalArtifactDocumentReferenceFact(FrozenFactBase):
    schema_version: Literal[
        "canonical_artifact_document_reference.v1"
    ] = "canonical_artifact_document_reference.v1"
    document_semantic_fingerprint: str
    artifact_reference: DurableContentAddressedArtifactReferenceFact
    reference_fingerprint: str


CanonicalMutationPayloadCarrier = (
    CanonicalInlineJsonDocumentFact
    | CanonicalArtifactDocumentReferenceFact
)
```

Bounds：

- inline canonical JSON最多256 KiB；
- 更大payload必须先在同一producer UOW写content-addressed artifact，再引用；
- artifact locator不进入document semantic identity；
- surface worker hydrate后重算content digest与document semantic fingerprint。

```python
class CanonicalMutationSurface(StrEnum):
    SEARCH_INDEX = "search_index.v1"
    VECTOR_INDEX = "vector_index.v1"
    OXIGRAPH = "oxigraph.v1"
```

`requested_surfaces` 在 producer UOW 内根据 resolved composition冻结。
未配置 Oxigraph 时不得先请求、后标记 skipped。

Tuple必须无重复，并按closed registry顺序：

```text
search_index.v1 -> vector_index.v1 -> oxigraph.v1
```

`source_authority_fingerprints`同样必须由source owner factory按contract顺序生成，不接受caller
任意重排。

### 11.2 Surface delivery row

```python
class CanonicalMutationSurfaceHandlerContractFact(FrozenFactBase):
    schema_version: Literal[
        "canonical_mutation_surface_handler_contract.v1"
    ] = "canonical_mutation_surface_handler_contract.v1"
    surface: CanonicalMutationSurface
    handler_id: str
    handler_version: str
    accepted_mutation_kinds: tuple[CanonicalMutationKind, ...]
    payload_codec_fingerprint: str
    target_compatibility_fingerprint: str
    idempotency_contract_fingerprint: str
    contract_fingerprint: str


class CanonicalMutationPlannedSurfaceFact(FrozenFactBase):
    schema_version: Literal[
        "canonical_mutation_planned_surface.v1"
    ] = "canonical_mutation_planned_surface.v1"
    handler_contract: CanonicalMutationSurfaceHandlerContractFact
    delivery_policy: DurableProjectionDeliveryPolicyFact
    planned_surface_fingerprint: str


class CanonicalMutationSurfacePlanFact(FrozenFactBase):
    schema_version: Literal[
        "canonical_mutation_surface_plan.v1"
    ] = "canonical_mutation_surface_plan.v1"
    ordered_surfaces: tuple[CanonicalMutationPlannedSurfaceFact, ...]
    composition_fingerprint: str
    plan_fingerprint: str


class CanonicalMutationSurfaceDeliveryIdentityFact(FrozenFactBase):
    schema_version: Literal[
        "canonical_mutation_surface_delivery_identity.v1"
    ] = "canonical_mutation_surface_delivery_identity.v1"
    mutation_id: str
    surface: CanonicalMutationSurface
    mutation_semantic_fingerprint: str
    mutation_fact_fingerprint: str
    mutation_ordering_fingerprint: str
    surface_sequence_number: int
    predecessor_surface_delivery_identity_fingerprint: str | None
    predecessor_surface_sequence_number: int | None
    handler_contract: CanonicalMutationSurfaceHandlerContractFact
    delivery_identity_fingerprint: str


class CanonicalMutationSurfaceSequenceHeadFact(FrozenFactBase):
    schema_version: Literal[
        "canonical_mutation_surface_sequence_head.v1"
    ] = "canonical_mutation_surface_sequence_head.v1"
    surface: CanonicalMutationSurface
    sequence_key: str
    last_surface_sequence_number: int
    last_mutation_sequence_number: int
    last_mutation_id: str
    last_delivery_identity_fingerprint: str
    head_revision: int
    head_fingerprint: str


class CanonicalMutationSurfaceDeliveryStateFact(FrozenFactBase):
    schema_version: Literal[
        "canonical_mutation_surface_delivery_state.v1"
    ] = "canonical_mutation_surface_delivery_state.v1"
    delivery_identity: CanonicalMutationSurfaceDeliveryIdentityFact
    delivery_policy: DurableProjectionDeliveryPolicyFact
    status: Literal[
        "pending",
        "leased",
        "retry_wait",
        "applied",
        "decommissioned",
        "dead_letter",
    ]
    state_revision: int
    repair_generation: int
    attempt_count: int
    lease_generation: int
    lease_owner_id: str | None
    lease_expires_at: datetime | None
    next_attempt_at: datetime | None
    terminal_receipt: "CanonicalMutationSurfaceTerminalReceipt | None"
    last_failure: BoundedRuntimeFailureDiagnosticFact | None
    state_fingerprint: str
```

Surface state使用与generic job相同的lease/retry/repair invariants：

- revisions/generations/counts非负；
- `repair_generation > 0` exact join contiguous surface repair actions；
- `leased` required live owner/expiry；
- `retry_wait` required next attempt和failure；
- `applied|decommissioned` required matching terminal receipt；
- `dead_letter` required failure且不得ordinary claim。

PK：

```text
(mutation_id, surface)
```

每个 surface 可独立 claim。Base mutation completion由全部 required surface state派生，不保存第二个
可漂移的 top-level status。

Job result和mutation append receipt只引用该immutable identity。Claim、attempt、retry和applied
state fingerprint不得进入上游result semantic identity。

每个surface row还必须冻结：

- complete retry/physical delivery policy，独立于delivery semantic identity；
- complete surface handler contract；
- provider-visible/index target compatibility；
- vector surface的embedding model/tokenization/vector-dimension contract；
- Oxigraph surface的JSON-LD/RDF lowering contract。

Composition变更不能把已经请求的surface默认为applied。若provider被移除：

- pending delivery保持pending/retry/dead-letter；
- operator必须恢复compatible provider，或提交typed decommission repair；
- 不允许因“当前未配置”静默放弃既有durable obligation。

会改变target schema/tokenization/vector dimension/RDF lowering的升级必须创建新的versioned surface
kind并执行typed rebuild/cutover；不得让同一个`vector_index.v1`或`oxigraph.v1`静默换contract。
旧surface存在non-terminal delivery时，deployment必须保留historical handler。

Surface claim handle：

```python
class LeasedCanonicalMutationSurfaceDeliveryFact(FrozenFactBase):
    schema_version: Literal[
        "leased_canonical_mutation_surface_delivery.v1"
    ] = "leased_canonical_mutation_surface_delivery.v1"
    delivery_identity: CanonicalMutationSurfaceDeliveryIdentityFact
    delivery_policy: DurableProjectionDeliveryPolicyFact
    expected_state_revision: int
    repair_generation: int
    attempt_count: int
    lease_generation: int
    lease_owner_id: str
    lease_expires_at: datetime
    lease_fingerprint: str


class ConfirmedCanonicalMutationSurfaceAppliedReceiptFact(FrozenFactBase):
    schema_version: Literal[
        "confirmed_canonical_mutation_surface_applied_receipt.v1"
    ] = "confirmed_canonical_mutation_surface_applied_receipt.v1"
    mutation_id: str
    surface: CanonicalMutationSurface
    mutation_semantic_fingerprint: str
    delivery_identity_fingerprint: str
    target_semantic_identity: str
    applied_document_semantic_fingerprint: str
    surface_handler_contract_fingerprint: str
    receipt_fingerprint: str


class LegacyRecordedSurfaceAppliedReceiptFact(FrozenFactBase):
    schema_version: Literal[
        "legacy_recorded_surface_applied_receipt.v1"
    ] = "legacy_recorded_surface_applied_receipt.v1"
    mutation_id: str
    surface: CanonicalMutationSurface
    legacy_outbox_id: str
    legacy_payload_sha256: str
    legacy_recorded_status: Literal["applied"]
    migration_version: int
    receipt_fingerprint: str


class CanonicalMutationSurfaceDecommissionedReceiptFact(FrozenFactBase):
    schema_version: Literal[
        "canonical_mutation_surface_decommissioned_receipt.v1"
    ] = "canonical_mutation_surface_decommissioned_receipt.v1"
    mutation_id: str
    surface: CanonicalMutationSurface
    delivery_identity_fingerprint: str
    decommission_reason: Literal[
        "operator_decommission",
        "superseded_by_rebuild",
    ]
    repair_action_fingerprint: str
    replacement_surface_identity_fingerprint: str | None
    receipt_fingerprint: str


CanonicalMutationSurfaceTerminalReceipt = (
    ConfirmedCanonicalMutationSurfaceAppliedReceiptFact
    | LegacyRecordedSurfaceAppliedReceiptFact
    | CanonicalMutationSurfaceDecommissionedReceiptFact
)
```

Settlement exact join lease fingerprint/generation/state revision。
`applied|decommissioned` required receipt；
`retry_wait/dead_letter` required sanitized failure。
`decommissioned` required typed repair authority，不得伪造effective newer mutation。Rebuild reason还必须
引用replacement surface identity。

Per-surface target head：

```python
class CanonicalMutationSurfaceTargetHeadFact(FrozenFactBase):
    schema_version: Literal[
        "canonical_mutation_surface_target_head.v1"
    ] = "canonical_mutation_surface_target_head.v1"
    surface: CanonicalMutationSurface
    sequence_key: str
    terminal_surface_sequence_number: int
    terminal_mutation_sequence_number: int
    terminal_mutation_id: str
    terminal_mutation_semantic_fingerprint: str
    terminal_disposition: Literal["applied", "decommissioned"]
    terminal_receipt_fingerprint: str
    head_revision: int
    head_fingerprint: str
```

Claim/settlement matrix：

1. surface row只有在其exact surface predecessor已经`applied|decommissioned`时可claim；
2. `surface_sequence_number == 1`要求无target head或head属于显式migration genesis；
3. non-first要求target head的terminal surface ordinal、mutation ordinal、mutation ID与
   candidate exact predecessor完全相等；
4. settlement transaction lock exact surface target head；
5. expected predecessor/head一致时apply receipt + advance head；
6. head已严格越过candidate surface ordinal而row仍非terminal时是authority conflict，不得
   自动supersede；
7. predecessor `retry_wait|leased`时保持pending；
8. predecessor `dead_letter`时显示`blocked_by_predecessor`，不得越过。

同一个 `(surface, sequence_key)` 任意时刻最多一个active lease。External I/O期间不持head row lock；
claim时冻结expected head，settlement时CAS。

Typed decommission repair不执行external I/O。它必须从当前target head开始按surface predecessor顺序
terminalize bounded rows，每个row写`decommissioned` receipt并推进terminal head；不得跳过中间
pending/dead-letter row。若要停用整个surface，repair coordinator重复有界批次直到该surface
没有non-terminal obligation。重新启用不复活这些rows；会改变target contract的恢复必须使用新的
versioned surface kind/rebuild authority。

单row repair/decommission必须由repository在一个transaction中完成exact dead-letter CAS、
immutable repair action、resulting state与必要的target-head推进。`retry_same_contract`重置
attempt state为pending；`decommission_with_authority|decommission_after_rebuild`产生terminal
receipt并解除后继surface sequence阻塞。CLI分别暴露`surface-retry`和
`surface-decommission`，不得通过直接UPDATE修复。

Surface predecessor在mutation commit transaction中由
`canonical_mutation_surface_sequence_heads`分配：

- 只链接此前真正请求同一surface的delivery；
- `surface_sequence_number = previous + 1`，first为1；
- mutation-global sequence允许出现gap，但必须严格递增；
- first surface delivery predecessor fields都为空；
- non-first两个predecessor fields都required；
- surface sequence head保存完整`CanonicalMutationSurfaceSequenceHeadFact`，并与delivery rows
  同transaction更新；
- claim不得在运行时按当前table排序重新猜predecessor。

### 11.3 Atomic mutation port

Source owner是discriminated union：

```python
class ProjectionResultCanonicalMutationOwnerFact(FrozenFactBase):
    schema_version: Literal[
        "projection_result_canonical_mutation_owner.v1"
    ] = "projection_result_canonical_mutation_owner.v1"
    owner_kind: Literal["projection_result"]
    result_owner: DurableProjectionResultOwner
    projection_kind: DurableProjectionKind
    source_event_reference: DurableProjectionSourceEventReferenceFact
    projection_result_semantic_fingerprint: str
    owner_fingerprint: str


class GovernanceCanonicalMutationOwnerFact(FrozenFactBase):
    schema_version: Literal[
        "governance_canonical_mutation_owner.v1"
    ] = "governance_canonical_mutation_owner.v1"
    owner_kind: Literal["memory_governance"]
    governance_batch_id: str
    governance_batch_input_fingerprint: str
    decision_id: str
    decision_semantic_fingerprint: str
    ordered_source_event_reference_fingerprints: tuple[str, ...]
    owner_fingerprint: str


class CanonicalMemoryWriteMutationOwnerFact(FrozenFactBase):
    schema_version: Literal[
        "canonical_memory_write_mutation_owner.v1"
    ] = "canonical_memory_write_mutation_owner.v1"
    owner_kind: Literal["canonical_memory_write"]
    operation_id: str
    operation_kind: "CanonicalMemoryMutationOperationKind"
    ordered_authority_fingerprints: tuple[str, ...]
    owner_fingerprint: str


class GraphMaintenanceMutationOwnerFact(FrozenFactBase):
    schema_version: Literal[
        "graph_maintenance_mutation_owner.v1"
    ] = "graph_maintenance_mutation_owner.v1"
    owner_kind: Literal["graph_maintenance"]
    maintenance_operation_id: str
    maintenance_kind: Literal["graph_reset", "graph_delete"]
    graph_id: str
    ordered_authority_fingerprints: tuple[str, ...]
    owner_fingerprint: str


class LegacyCanonicalMutationOwnerFact(FrozenFactBase):
    schema_version: Literal[
        "legacy_canonical_mutation_owner.v1"
    ] = "legacy_canonical_mutation_owner.v1"
    owner_kind: Literal["legacy_migration"]
    legacy_outbox_id: str
    legacy_payload_sha256: str
    migration_version: int
    owner_fingerprint: str


CanonicalMutationSourceOwner = (
    ProjectionResultCanonicalMutationOwnerFact
    | GovernanceCanonicalMutationOwnerFact
    | CanonicalMemoryWriteMutationOwnerFact
    | GraphMaintenanceMutationOwnerFact
    | LegacyCanonicalMutationOwnerFact
)
```

`ProjectionResultCanonicalMutationOwnerFact.projection_result_semantic_fingerprint`必须引用
`DurableProjectionResultSemanticFact.result_semantic_fingerprint`。它不引用
`PreparedDurableProjectionResultFact.prepared_result_fingerprint`或
`DurableProjectionResultReceiptReferenceFact.reference_fingerprint`，从而避免
`result -> mutation candidate -> owner -> result`递归身份环。
其中nested result owner可以是durable job或尚未activation的transitional hook；factory必须执行
第8.1节activation exclusivity guard。

```python
class CanonicalMemoryMutationOperationKind(StrEnum):
    CLAIM = "claim"
    PREFERENCE = "preference"
    ACTION_BOUNDARY = "action_boundary"
    OBSERVATION = "observation"
    DECISION = "decision"
    TURN_RELATION = "turn_relation"
    WORKING_CONTEXT = "working_context"
    RUNTIME_SEMANTIC_DOCUMENT = "runtime_semantic_document"
```

Composition root必须穷尽所有production mutation producer；不能让未知producer回退到generic owner。
`LegacyCanonicalMutationOwnerFact` 只能由migration/binding lane构造，production commit port拒绝它。
所有new-production `operation_id` 必须从canonical source authority确定性派生；UUID只可作为
physical attempt ID，不得进入mutation source semantic identity。

```python
class CanonicalMutationAppendReceipt(FrozenFactBase):
    schema_version: Literal[
        "canonical_mutation_append_receipt.v1"
    ] = "canonical_mutation_append_receipt.v1"
    mutation_id: str
    mutation_semantic_fingerprint: str
    append_disposition: Literal["inserted", "exact_confirmed"]
    mutation_fact_fingerprint: str
    ordering_fingerprint: str
    ordered_surface_delivery_identity_fingerprints: tuple[str, ...]
    receipt_fingerprint: str


class PreparedCanonicalMutationBundleFact(FrozenFactBase):
    schema_version: Literal[
        "prepared_canonical_mutation_bundle.v1"
    ] = "prepared_canonical_mutation_bundle.v1"
    source_owner: CanonicalMutationSourceOwner
    surface_plan: CanonicalMutationSurfacePlanFact
    ordered_mutation_candidates: tuple[
        CanonicalMutationCandidateFact, ...
    ]
    bundle_fingerprint: str


class CanonicalMutationBundleAppendReceiptFact(FrozenFactBase):
    schema_version: Literal[
        "canonical_mutation_bundle_append_receipt.v1"
    ] = "canonical_mutation_bundle_append_receipt.v1"
    attempted_bundle_fingerprint: str
    ordered_mutation_receipts: tuple[
        CanonicalMutationAppendReceipt, ...
    ]
    receipt_fingerprint: str


class VerifiedPostgresTransactionHandle(Protocol):
    @property
    def schema_binding_fingerprint(self) -> str:
        ...

    @property
    def transaction_owner_id(self) -> str:
        ...

    @property
    def transaction_generation(self) -> int:
        ...

    @property
    def connection_provider_borrower_id(self) -> str:
        ...
```

所有 producer 必须使用：

```python
class CanonicalMutationCommitPort(Protocol):
    def append_bundle_in_transaction(
        self,
        *,
        connection: VerifiedPostgresTransactionHandle,
        admission_guard: RuntimeWriteAdmissionGuard,
        bundle: PreparedCanonicalMutationBundleFact,
    ) -> CanonicalMutationBundleAppendReceiptFact:
        ...
```

`VerifiedPostgresTransactionHandle`是process-local opaque capability，不是仅凭上述字符串即可自证的
DTO。它只能由active verified connection borrower/UOW issuer创建：

- commit port验证handle对象由同一issuer registry签发；
- transaction generation仍active且未commit/rollback；
- physical connection identity等于schema binding；
- handle不可pickle/asdict/event-serialize；
- production API不同时接受raw `psycopg.Connection`；
- tests使用显式`UnverifiedTestPostgresTransactionHandle`，不得进入production composition。

`RuntimeWriteAdmissionGuard`必须由同一physical transaction/issuer创建并仍持有epoch shared lock；
connection handle A + admission guard B的cross-pair立即拒绝。Maintenance-mode append只允许admin
guard且maintenance operation与调用中的migration/drain owner精确相等。

Bundle factory验证：

- candidate ordinals恰好为`0..N-1`；
- 每个candidate source owner fingerprint等于bundle owner；
- 每个candidate surface plan fingerprint等于bundle plan；
- requested surfaces与plan ordered surfaces完全相等；
- mutation IDs/semantics/candidates无重复。

Commit port先按canonical UTF-8 `sequence_key`升序锁所有mutation sequence heads，再按
`(surface registry ordinal, sequence_key)`锁surface sequence heads，最后按candidate ordinal
分配ordering/delivery predecessor，禁止由caller决定lock order。

Production UOW全局lock order：

```text
projection job row/lease
-> projection target head
-> canonical memory/graph domain locks
-> canonical mutation sequence heads
-> canonical mutation surface sequence heads
-> mutation/surface inserts
-> projection job settlement
```

没有某层owner时直接跳过该层，禁止反向获取。Surface apply/repair使用独立顺序：

```text
surface delivery row
-> surface target head
-> settlement/repair row
```

Receipt只表示mutation rows已加入当前outer transaction，不表示transaction已FULL。
只有outer commit owner确认FULL后，调用方才能公开durable append完成。

必须审计并迁移：

- governance UOW：保留其现有 same-UOW 行为，改为 V2 port；
- run timeline projection commit；
- tool-result execution evidence projection commit；
- graph reset/delete；
- execution evidence ledger 的其他 production writer；
- canonical memory writes。

禁止：

```text
write graph
commit
append mutation outbox
```

以及：

```text
append mutation
commit
directly mutate Oxigraph
```

### 11.4 Unified surface handlers

`DurableProjectionJobService` 同时运行 closed surface handler registry：

```text
search_index.v1
vector_index.v1
oxigraph.v1
```

替换：

- `CanonicalMutationOutboxReplayHook`；
- independent search-index outbox replay owner；
- vector-only claim worker；
- Oxigraph materializer row-lock owner；
- graph facade direct Oxigraph best-effort path。

External I/O 必须在 claim transaction 外执行。

Handler 必须 idempotent：

- search index以 mutation/document identity upsert；
- vector index以 graph/document revision upsert；
- Oxigraph以 graph/document semantic identity replace/upsert；
- duplicate execution不得产生 duplicate semantic object。

### 11.5 External side effect后 settlement丢失

轨迹：

```text
surface side effect succeeds
-> connection lost before delivery row settlement
-> lease expires
-> retry same mutation/surface
```

要求：

- handler exact-read external target或执行 idempotent upsert；
- second attempt得到同一 applied receipt semantic；
- settlement只接受当前 lease generation；
- 不因可能重复而跳过未确认 work。

---

## 12. Migrations 0005 -> 0008

新增：

```text
src/pulsara_agent/storage/migrations/sql/
    0005_durable_projection_jobs.sql
    0006_canonical_mutation_surface_jobs.sql
    0007_run_timeline_projection_activation.sql
    0008_tool_result_evidence_projection_activation.sql

src/pulsara_agent/storage/migrations/resources/
    0005_runtime_write_protected_relations_v1.json
    0006_runtime_write_protected_relations_v2.json
    0006_pre_activation_projection_contracts_v1.json
    0006_legacy_surface_binding_plan_contract_v1.json
    0007_run_timeline_activation_v1.json
    0008_tool_result_evidence_activation_v1.json
```

### 12.0 Prerequisite-aware migration state machine

当前“连续apply全部pending definitions”的runner必须hard cut。本文不引入`--through`；唯一owner是
prerequisite-aware migrator。

```python
class PostgresMigrationPreparationKind(StrEnum):
    LEGACY_SURFACE_BINDING_PLAN = "legacy_surface_binding_plan.v1"
    RUN_TIMELINE_PRE_ACTIVATION_COVERAGE = (
        "run_timeline_pre_activation_coverage.v1"
    )
    TOOL_RESULT_EVIDENCE_PRE_ACTIVATION_COVERAGE = (
        "tool_result_evidence_pre_activation_coverage.v1"
    )


class PostgresMigrationPreparationRequirementFact(FrozenFactBase):
    schema_version: Literal[
        "postgres_migration_preparation_requirement.v1"
    ] = "postgres_migration_preparation_requirement.v1"
    current_head_version: int
    next_migration_version: int
    preparation_kind: PostgresMigrationPreparationKind
    expected_registry_prefix_fingerprint: str
    expected_database_target_fingerprint: str
    required_maintenance_operation_kind: str
    preparation_contract_fingerprint: str
    requirement_fingerprint: str


class PostgresMigrationProgressOutcomeFact(FrozenFactBase):
    schema_version: Literal[
        "postgres_migration_progress_outcome.v1"
    ] = "postgres_migration_progress_outcome.v1"
    status: Literal[
        "up_to_date",
        "advanced",
        "preparation_required",
    ]
    initial_head_version: int | None
    resulting_head_version: int | None
    applied_versions: tuple[int, ...]
    preparation_requirement: (
        PostgresMigrationPreparationRequirementFact | None
    )
    resulting_registry_prefix_fingerprint: str
    outcome_fingerprint: str
```

Closed prerequisite registry：

| Next migration | Required durable preparation |
|---|---|
| v5 | none |
| v6 | exact `LegacySurfaceMigrationBindingPlanFact` under current v6 maintenance operation |
| v7 | complete run-timeline coverage receipt set under current v7 maintenance operation |
| v8 | complete tool-result-evidence coverage receipt set under current v8 maintenance operation |

Runner algorithm：

1. 完整验证current ledger history与current-head expected catalog；
2. 查看next migration的closed prerequisite；
3. prerequisite缺失时，不开始next migration、不自动进入maintenance、不继续apply later definitions；
4. 返回`preparation_required` + typed requirement；这是成功的bounded progress outcome，不是migration
   failure，也不允许runner循环跳过；
5. prerequisite FULL时exact验证maintenance epoch、plan/coverage roots与current head，再apply该version；
6. apply后验证该version ledger/catalog并继续检查下一version；
7. 遇到下一个缺失prerequisite立即返回，已FULL migrations保持committed；
8. 只有current head = latest才返回`up_to_date`。

Historical-head maintenance：

- final v8 binary必须保留v5/v6/v7 expected catalogs、decoder/factory和preparation contracts；
- maintenance verifier可以为exact current historical head签发
  `VerifiedHistoricalHeadMaintenanceBinding`，只授权closed plan/drain/migrate/status命令；
- production Host/worker binding仍required latest，不能用historical maintenance binding启动Host；
- plan/drain command必须exact匹配typed requirement，进入对应next-version maintenance operation；
- migration完成后historical binding立即失效。

因此final v8 binary处理v4 database的确定路径是：

```text
db migrate
    -> apply v5
    -> PREPARATION_REQUIRED(v6 binding plan)

plan-legacy-surface-bindings
db migrate
    -> apply v6
    -> PREPARATION_REQUIRED(v7 timeline coverage)

drain-pre-activation --kind run_timeline.v1
db migrate
    -> apply v7
    -> PREPARATION_REQUIRED(v8 evidence coverage)

drain-pre-activation --kind tool_result_execution_evidence.v1
db migrate
    -> apply v8 / UP_TO_DATE
```

Crash/retry必须从durable ledger、maintenance epoch和preparation receipts恢复同一boundary。CLI不得把
`PREPARATION_REQUIRED`打印成catalog corruption，也不得在一个invocation里偷偷生成plan/drain。

### 12.1 Migration 0005：additive infrastructure

新增relations：

```text
runtime_write_admission_epochs
runtime_write_guard_secrets
runtime_write_protected_relations
durable_projection_kind_activations
durable_projection_pre_activation_contracts
durable_projection_pre_activation_session_cutovers
durable_projection_pre_activation_coverage_pages
durable_projection_pre_activation_coverage_receipts
durable_projection_session_cutovers
durable_projection_seed_failures
durable_projection_seed_failure_resolutions
durable_projection_jobs
durable_projection_result_receipts
durable_projection_target_heads
durable_projection_target_authority_conflicts
durable_projection_target_execution_leases
graph_relation_facts
canonical_mutations_v2
canonical_mutation_sequence_heads
canonical_mutation_surface_deliveries
canonical_mutation_surface_sequence_heads
canonical_mutation_surface_target_heads
canonical_mutation_v2_migration_binding_plan_pages
canonical_mutation_v2_migration_binding_plans
canonical_mutation_v2_migration_binding_receipts
durable_projection_repair_actions
```

`runtime_projection_checkpoints` 继续承载 seeder checkpoint，不复制 checkpoint table。

`0005` 不修改或删除legacy `memory_write_outbox`。DPJ1之后：

- old production producers/consumers继续使用legacy table；
- V2 base/surface tables保持production-empty；
- V2 surface worker只对in-memory/test或rollback-only candidate做shadow验证，不向production
  V2 tables/external target写入；
- 只有DPJ2/`0006`一次性迁移全部legacy rows/producers并启用V2；
- timeline/evidence hard cut必须晚于该ordering cutover。

V5使用Schema Hot-Path合同规定的offline migration/restart deployment，但不创建任何production
projection-kind cutover，不启动authoritative seeder。它安装runtime admission epoch genesis、
guard function/triggers、唯一session bootstrap repository contract。DPJ1 production binary切换后，
所有受保护write path必须先取得v5 normal epoch guard；这是v6 maintenance hard cut的前置gate。

V5 postcondition还必须证明：

- exact一条32-byte guard secret row；value永不进入report/log；
- exact一条normal epoch genesis且runtime role binding正确；
- durable protected-relation registry与v5 production DML/grant/catalog inventory set-equal；
- 每个registry relation安装且只安装一个expected guard trigger；
- normal function仅runtime/admin可execute，maintenance/transition functions仅admin可execute，
  `PUBLIC`无权限；
- session bootstrap stable-state factory/confirmation port已经取代三个insert owner。

### 12.2 Migration 0006：existing mutation hard cut

DPJ2 restart cutover期间，`memory_write_outbox` hard cut到 immutable V2 base。
同一v6 migration还安装timeline/evidence的immutable pre-activation hook contracts；这两条row只为
v6->v7/v8 staged owner handoff服务，不是durable job activation。

Packaged v6 resource只保存
`PreActivationProjectionHookContractSemanticFact`。Registry先计算v6 resulting prefix，runner再
构造outer contract fact，避免migration definition与resulting prefix形成递归identity。

V6在同一migration transaction内还必须：

1. 插入timeline/evidence两条immutable pre-activation contract；
2. bounded遍历migration开始时存在的全部runtime sessions；
3. 通过EventLog canonical prefix reader取得每个session的exact head；
4. 为每个session × 两个kind写pre-activation session cutover；
5. 验证session count、每kind cutover count、ordered session-ID accumulator及所有prefix proof；
6. 确认没有kind activation、seed checkpoint或durable projection job。

V6后创建session的唯一bootstrap transaction必须：

- 对已经durable-active的kind写sequence-0 `DurableProjectionSessionCutoverFact`；
- 对已有pre-activation contract但尚未active的kind写sequence-0
  `PreActivationProjectionSessionCutoverFact`；
- 与session row原子提交；
- 对同一kind出现active + pre-activation双authority时fail closed。

Cutover是database-fenced offline/restart-only：

```text
v5 normal epoch
-> CAS enter v6 maintenance epoch
-> database waits existing shared write guards
-> ordinary runtime writes fail closed
-> run v6 migration under exclusive schema lock/admin maintenance guard
-> verify no legacy binding remains
-> commit v6 + install new normal epoch
-> start only v6-aware binary
```

不支持old/new binary混跑。V6 database必须让old registry startup fail closed。
V6 precondition要求production V2 base/surface/sequence/head tables仍为空；发现未知preexisting
V2 row时分类为migration conflict，不尝试与legacy ordering猜测合并。

推荐 migration shape：

1. stop old-version Host/process admission；
2. rename old table为 migration-local legacy source；
3. reuse已经存在的V2 base/surface tables；
4. copy exact rows；
5. preserve old outbox/mutation ID；
6. classify each legacy row；
7. install v6 protected-relation registry/trigger set并验证所有old producers已从composition移除；
8. drop legacy source only after postcondition，new normal epoch引用v6 registry fingerprint。

Legacy states：

```text
already fully applied
    -> immutable mutation
    -> all requested surfaces applied with legacy-recorded receipt

pending/retryable with parseable V1 payload
    -> immutable mutation + pending/applied surface rows

ambiguous/corrupt payload
    -> migration conflict + full v6 rollback
    -> legacy table remains unchanged for inspection/reset
```

不得重新投递 V1 中已经 confirmed applied 的 surface。

Migration还必须：

- 按legacy `(sequence_key, created_at, outbox_id)`冻结每个graph的V2 ordering；
- 构造exact predecessor chain与sequence head；
- 对每个surface只链接实际请求该surface的legacy rows并建立surface sequence head；
- 为每个surface建立latest contiguous terminal target head；migration genesis只允许由
  legacy-recorded applied receipts推进，不得凭pending/failed row推进；
- 对每个 `(surface, sequence_key)` 验证legacy状态必须是contiguous applied prefix后接
  pending/failed suffix；
- 若出现non-applied gap之后的legacy applied row，v6以
  `legacy_surface_ordering_conflict`回滚；不得用较新row自动supersede较旧mutation；
- operator必须在旧binary仍关闭且v6尚未commit时，通过现有privileged
  reconcile/rebuild command把legacy surface恢复成contiguous applied prefix，再重跑migration；
  若无法证明恢复，只能进入explicit reset/inspection流程；
- preserve bounded attempt count；
- migration通过maintenance epoch证明所有legacy database write owner已quiesced；残留
  process-local compute无提交authority，vector claim token/lease只作为
  stale physical attribution迁移为retryable state，不继承old owner/token；
- 若旧external side effect可能FULL但settlement未记录，V2只能依靠同一mutation identity做
  idempotent retry/exact target confirmation，不得猜测applied；
- 不复制raw `last_error`，而是经closed legacy diagnostic sanitizer；
- 将legacy failed surface转为deterministic `retry_wait`；
- 使用database migration time设置第一次eligible time，只作为operational attribution；
- 对new-V2 row ID collision执行semantic exact-confirm；同ID不同semantic使v6 rollback；
- Inspector明确区分`confirmed_applied`与`legacy_recorded_applied`。

Legacy row没有完整surface handler contract，禁止由current composition静默补齐。V6 required显式
typed binding plan。三种处置不是`enum + nullable fields`，而是互斥authority branch：

```python
class LegacySurfaceHistoricalBindingProofFact(FrozenFactBase):
    schema_version: Literal[
        "legacy_surface_historical_binding_proof.v1"
    ] = "legacy_surface_historical_binding_proof.v1"
    binding_kind: Literal["historical_confirmed"]
    surface: CanonicalMutationSurface
    historical_handler_contract: (
        CanonicalMutationSurfaceHandlerContractFact
    )
    observed_target_semantic_identity: str
    observed_target_contract_fingerprint: str
    ordered_target_authority_fingerprints: tuple[str, ...]
    proof_fingerprint: str


class LegacySurfaceMigrationRebindAuthorityFact(FrozenFactBase):
    schema_version: Literal[
        "legacy_surface_migration_rebind_authority.v1"
    ] = "legacy_surface_migration_rebind_authority.v1"
    binding_kind: Literal["migration_rebound"]
    authority_id: str
    database_target_fingerprint: str
    maintenance_authority_fingerprint: str
    legacy_outbox_id: str
    surface: CanonicalMutationSurface
    expected_legacy_status: Literal["pending", "failed"]
    no_full_side_effect_proof_fingerprint: str
    resulting_planned_surface: CanonicalMutationPlannedSurfaceFact
    authority_fingerprint: str


class LegacySurfaceDecommissionAndRebuildAuthorityFact(FrozenFactBase):
    schema_version: Literal[
        "legacy_surface_decommission_and_rebuild_authority.v1"
    ] = "legacy_surface_decommission_and_rebuild_authority.v1"
    binding_kind: Literal["decommission_and_rebuild"]
    authority_id: str
    database_target_fingerprint: str
    maintenance_authority_fingerprint: str
    surface: CanonicalMutationSurface
    expected_legacy_surface_head_fingerprint: str
    rebuild_receipt_fingerprint: str
    resulting_handler_contract_fingerprint: str
    resulting_target_compatibility_fingerprint: str
    authority_fingerprint: str


LegacySurfaceMigrationBindingAuthority = (
    LegacySurfaceHistoricalBindingProofFact
    | LegacySurfaceMigrationRebindAuthorityFact
    | LegacySurfaceDecommissionAndRebuildAuthorityFact
)


class LegacySurfaceMigrationBindingEntryFact(FrozenFactBase):
    schema_version: Literal[
        "legacy_surface_migration_binding_entry.v1"
    ] = "legacy_surface_migration_binding_entry.v1"
    legacy_outbox_id: str
    legacy_payload_sha256: str
    surface: CanonicalMutationSurface
    legacy_surface_status: str
    binding_authority: LegacySurfaceMigrationBindingAuthority
    entry_fingerprint: str


class LegacySurfaceMigrationBindingPageFact(FrozenFactBase):
    schema_version: Literal[
        "legacy_surface_migration_binding_page.v1"
    ] = "legacy_surface_migration_binding_page.v1"
    page_index: int
    previous_page_fingerprint: str | None
    ordered_entries: tuple[
        LegacySurfaceMigrationBindingEntryFact, ...
    ]
    entry_count: int
    entry_accumulator: str
    canonical_utf8_bytes: int
    page_fingerprint: str


class LegacySurfaceMigrationBindingPlanFact(FrozenFactBase):
    schema_version: Literal[
        "legacy_surface_migration_binding_plan.v1"
    ] = "legacy_surface_migration_binding_plan.v1"
    plan_id: str
    database_target_fingerprint: str
    expected_v5_registry_prefix_fingerprint: str
    maintenance_authority_fingerprint: str
    legacy_row_count: int
    legacy_row_accumulator: str
    binding_page_count: int
    ordered_binding_page_fingerprint_accumulator: str
    binding_entry_count: int
    binding_entry_accumulator: str
    ordered_privileged_authority_fingerprints: tuple[str, ...]
    plan_fingerprint: str


class LegacySurfaceMigrationRebaseFact(FrozenFactBase):
    schema_version: Literal[
        "legacy_surface_migration_rebase.v1"
    ] = "legacy_surface_migration_rebase.v1"
    surface: CanonicalMutationSurface
    sequence_key: str
    covered_through_legacy_outbox_id: str
    covered_through_surface_sequence_number: int
    rebuild_receipt_fingerprint: str
    resulting_handler_contract_fingerprint: str
    resulting_target_compatibility_fingerprint: str
    maintenance_authority_fingerprint: str
    rebase_fingerprint: str


class LegacySurfaceMigrationBindingAppliedReceiptFact(FrozenFactBase):
    schema_version: Literal[
        "legacy_surface_migration_binding_applied_receipt.v1"
    ] = "legacy_surface_migration_binding_applied_receipt.v1"
    plan_fingerprint: str
    resulting_v6_registry_prefix_fingerprint: str
    historical_confirmed_count: int
    migration_rebound_count: int
    decommissioned_and_rebuilt_count: int
    ordered_surface_rebase_fingerprints: tuple[str, ...]
    resulting_mutation_accumulator: str
    resulting_surface_delivery_accumulator: str
    receipt_fingerprint: str
```

唯一preflight命令先CAS进入目标v6的exclusive maintenance epoch，再从该frozen v5 database
snapshot生成content-addressed pages/plan，并写入v5已经存在的immutable
`canonical_mutation_v2_migration_binding_plan_pages/plans`。CLI输出文件只是secret-safe export，
不是migration authority。Database保持maintenance直到：

- 同一maintenance operation运行v6 migration；或
- typed `abort-maintenance`证明v6 migration NONE后安装新normal epoch。

`db migrate`必须exact-readdurable plan，验证其maintenance authority仍是current epoch，并re-read
database target、legacy row count/accumulator及每个target authority后才接受。Plan是environment-
specific migration input，其fingerprint进入database-local applied receipt，不进入跨database
immutable migration definition/prefix。

Branch invariants：

- `historical_confirmed`只允许`LegacySurfaceHistoricalBindingProofFact`，且observed target/build
  identity必须能重算historical handler的target compatibility；
- `migration_rebound`只允许typed privileged rebind authority，且
  `no_full_side_effect_proof_fingerprint`必须从legacy state、target absence与claim/receipt state
  的同一maintenance snapshot重算；
- `decommission_and_rebuild`只允许typed rebuild authority，rebuild receipt必须覆盖exact legacy
  surface head；
- entry的surface/outbox/status必须与nested authority exact join；
- plan的privileged authority tuple恰好等于所有rebind/rebuild branch authority fingerprints，
  historical proof不得混入；
- caller不能只传fingerprint；factory必须exact-read对应maintenance operation、target/build receipt
  与legacy row后构造完整branch。

Closed matrix：

| Legacy case | Required disposition |
|---|---|
| applied vector，全部受影响row拥有一致`embedding_fingerprint + builder_version + dimensions`且与historical target-compatibility contract精确相符 | `historical_confirmed` |
| pending/failed vector，尚无可能FULL的side effect | `migration_rebound`，required typed privileged authority + current planned contract |
| applied delete、缺失vector row、mixed build identity或无法证明historical contract | `decommission_and_rebuild`或reset |
| applied search/Oxigraph | target receipt/compatibility可exact证明才允许`historical_confirmed`，否则decommission+rebuild/reset |
| pending search/Oxigraph | 只有确认无side effect FULL窗口时允许`migration_rebound` |

Vector `target_compatibility_fingerprint`必须覆盖provider family、model ID、dimensions、embedded-text
builder contract与vector codec；只比较`surface="vector_index"`不构成binding proof。

`decommission_and_rebuild`要求Host/barrier已进入maintenance，并在v6前由typed privileged full-surface
rebuild生成exact receipt。V6：

- 将无法证明的legacy per-row delivery标记为typed decommissioned，不伪造applied handler；
- 以rebuild receipt安装immutable `LegacySurfaceMigrationRebaseFact`和database-local target
  compatibility；
- 只有rebuild覆盖exact legacy graph/surface head才允许推进surface target head；
- rebuild/receipt缺失、过期或head变化使migration rollback；
- operator不接受rebuild时只能显式reset。

Canonical mutation semantic/binding不能在pure SQL计算时，可以在同一v6 transaction使用上述pages
经closed central V1 decoder/factory bind；不得commit中间`binding_required`状态。Production worker
永远不读取plan或临时猜测legacy contract。

Migration runner为此新增closed typed data-transform contract：

```python
class PostgresMigrationDataTransformContractFact(FrozenFactBase):
    schema_version: Literal[
        "postgres_migration_data_transform_contract.v1"
    ] = "postgres_migration_data_transform_contract.v1"
    transform_id: Literal[
        "bind_canonical_mutation_v1_to_v2",
        "bind_legacy_surface_contracts_v1_to_v2",
        "activate_run_timeline_projection_v1",
        "activate_tool_result_execution_evidence_projection_v1",
    ]
    transform_version: str
    input_schema_fingerprint: str
    output_schema_fingerprint: str
    canonical_codec_fingerprint: str
    maximum_rows_per_fetch: int
    maximum_payload_bytes_per_fetch: int
    transform_contract_fingerprint: str
```

规则：

- transform contract fingerprint进入其所属v6/v7/v8 migration contract/prefix；
- runner使用同一admin connection与同一migration transaction；
- server cursor每次最多256 rows / 16 MiB canonical payload，禁止全表materialization；
- migration preflight先用`pg_column_size`拒绝single legacy payload超过16 MiB的database；
  分类为`legacy_payload_oversize` migration conflict并回滚，不得尝试materialize；
- output只由对应中央legacy decoder/V2 mutation factory或activation/cutover factory生成；
- transform registry closed，不接受module path/caller callback；
- cancellation/failure rollback整个所属migration；
- v6 postcondition要求0 `binding_required`，并恰好一条
  `LegacySurfaceMigrationBindingAppliedReceiptFact` exact join输入plan、resulting v6 prefix、
  branch counts、surface rebases、mutation accumulator与delivery accumulator；
- applied receipt缺失、plan/legacy accumulator变化、任一authority过期或存在未分类entry时整个v6
  rollback；
- v7/v8使用第12.3节activation postcondition；
- commit UNKNOWN继续使用schema migrator既有FULL/NONE/CONFLICT/UNRESOLVED确认。

### 12.3 Migrations 0007 / 0008：per-kind admission activation

```text
0007_run_timeline_projection_activation.sql
0008_tool_result_evidence_projection_activation.sql
```

每个activation是“maintenance barrier + durable coverage + bounded migration”的不可拆分procedure，
但有两个durable command owner：

`drain-pre-activation`拥有步骤1-6：

1. CAS normal epoch进入该migration的exclusive maintenance epoch；
2. database transition等待所有已admit shared write transaction收口；普通Host、session creation、
   EventLog、mutation与old hook UOW从此fail closed；
3. process owner停止新callback并等待bounded local tasks；仍在compute但没有write guard的callback
   可以放弃，因为它不能再提交；
4. 运行privileged pre-activation drain，从每个session的pre-activation cutover exclusive扫描到
   exact frozen EventLog head；
5. 使用相同exact source reader、deterministic assembler和
   `PreActivationProjectionCommitPort`补齐/确认所有target；
6. 为每个session写immutable content-addressed coverage pages/receipt；

随后命令返回typed preparation FULL，database保持同一maintenance epoch。下一次`db migrate`拥有
步骤7-15：

7. 取得schema advisory lock并开始activation migration transaction；
8. 验证maintenance operation/epoch仍与coverage receipts完全相等；
9. 用bounded aggregate query证明current session set与coverage receipt set的
   count/ordered-ID accumulator相等，并证明EventLog heads没有越过各receipt frozen horizon；
10. 验证该kind尚无activation/checkpoint/job，且pre-activation contract与packaged activation
    的handler、policy、trigger、source-query和surface semantics兼容；
11. 写完整immutable `DurableProjectionKindActivationFact`；
12. 从coverage receipts通过单个set-based `INSERT ... SELECT`写
    `DurableProjectionSessionCutoverFact`；cutover fields直接复制receipt frozen canonical horizon；
13. 验证activation、cutover set与coverage set postcondition；
14. commit migration并原子安装新registry-prefix normal epoch；
15. 只启动对应new-owner binary，再开放event admission。

Runner在coverage缺失时只返回`PREPARATION_REQUIRED`，不得自己执行步骤1-6。Drain FULL后若进程
crash，下一次runner exact reuse receipts；若epoch/op不匹配，旧coverage不能授权migration。

Timeline handler contract必须声明“同一run的较新trigger产生完整replacement projection”，所以
target head覆盖range内latest timeline trigger即可证明更早trigger已被吸收。Evidence target
按session/run/tool-call single-assignment，必须逐target覆盖；同target第二个source event是
activation-blocking authority conflict，不得用较新event替代或为不同result fingerprint另建target。

`0007`/`0008`各自使用closed migration data-transform contract。Activation semantic canonical
JSON与expected semantic fingerprint必须是migration definition的packaged immutable resource，
不能从当前composition临时组装。Runner用已经计算出的resulting registry prefix构造外层
activation attribution。Expensive EventLog/target scan只发生在maintenance drain并形成durable
coverage receipt；activation transaction不得重新扫描全部events/targets。它只执行indexed
receipt/session/head aggregate validation和bulk cutover insert。

Migration postcondition：

- exact一个kind activation row；
- migration开始时存在的每个session exact一个该kind cutover；
- cutover contract全部引用packaged activation；
- activation cutover与immutable coverage receipt frozen head逐session精确相等；
- coverage receipt set的count/session accumulator等于session set；
- receipt trigger/target/page roots均已由central validator重算；
- 无该kindcheckpoint/job/seed failure；
- session count、cutover count、ordered session-ID accumulator完全相等。

`0007` 与同一release中的代码切换：

- 删除timeline production hook；
- activate `run_timeline.v1` seeder/handler。

`0008` 与同一release中的代码切换：

- 删除AgentRuntime execution-evidence hook/failure-audit producer；
- activate `tool_result_execution_evidence.v1` seeder/handler。

Activation migration之后创建的新session，session row与所有active-kind sequence-0 cutovers，以及
仍未激活的pre-activation kind sequence-0 cutovers，在同一transaction通过两个immutable registry
固定`INSERT ... SELECT`写入。V7阶段要求timeline durable cutover + evidence pre-activation
cutover；V8及以后要求timeline + evidence durable cutovers且不再写pre-activation cutover。

Owner ranges唯一冻结为：

```text
sequence <= v6/session pre-activation cutover
    -> pre-D3；Inspector显示not_durably_observable

pre-activation cutover < sequence <= activation frozen head
    -> pre-activation output UOW + mandatory offline drain覆盖

sequence > activation frozen head
    -> durable seeder/job owner
```

New normal epoch FULL后才允许Host admission，因此不存在owner gap。Activation不得仅凭“旧hook
应该已经运行”跳过offline drain、coverage receipt或result-receipt exact validation。

### 12.4 Registry/manifest

必须更新：

- immutable migration definitions v5/v6/v7/v8；
- per-version manifests v5/v6/v7/v8；
- grant policy；
- expected fast catalog；
- expected deep catalog；
- registry-prefix golden vectors。
- pre-activation/activation resource canonical SHA-256/schema fingerprint，且resource bytes进入
  所属migration contract fingerprint。
- v5 protected-relation registry resource、guard function definitions/ACL/trigger set进入v5
  migration contract与deep catalog；database-local nonce value不进入semantic fingerprint。
- v6 protected-relation registry resource删除legacy outbox并exact覆盖V2 owner；v7/v8若复用v6
  registry，其expected catalog/epoch仍必须引用相同fingerprint。
- legacy binding plan contract resource进入v6 migration identity；database-specific plan/receipt
  fingerprint不进入global registry prefix。
- v5/v6/v7 expected catalogs必须可被final binary historical-head maintenance verifier读取；latest
  Host verifier仍只接受v8。

每次append都必须保持所有旧definition/prefix golden不变：

```text
v5 preserves v0-v4
v6 preserves v0-v5
v7 preserves v0-v6
v8 preserves v0-v7
```

### 12.5 Runtime grants

Runtime role：

- projection tables SELECT/INSERT/UPDATE；
- 无 DELETE；
- runtime role只能`EXECUTE pulsara_acquire_normal_runtime_write_guard`与assertion所需closed wrapper；
  无secret-table SELECT、maintenance function EXECUTE或epoch direct UPDATE；
- admission epoch row只允许guard function取得shared lock/read；
- maintenance transition、coverage pages/receipts与legacy migration binding receipt只允许admin；
- repair actions只有受控 command port可写；
- kind activation与pre-activation contract rows只允许SELECT；
- durable/pre-activation cutover existing rows无 UPDATE/DELETE，INSERT只允许session bootstrap
  repository固定语句；
- surface/job claim只能经 repository allowlisted SQL。
- runtime worker无legacy raw payload读取路径。

Migration/admin role拥有 DDL、legacy binding及maintenance functions；normal/maintenance role/function/
relation-operation permission matrix进入grant manifest与deep verifier。`PUBLIC`对全部guard functions
无EXECUTE。

### 12.6 Reset policy

四次migration都不得要求 PostgreSQL/Oxigraph reset。

只有：

- migration history conflict；
- legacy payload authority conflict且 operator选择放弃历史；
- legacy surface ordering conflict无法经privileged pre-migration reconcile证明收口；
- expected catalog conflict

才进入现有 explicit reset/inspection流程。

---

## 13. DurableProjectionJobService ownership

### 13.1 Owner

Production owner：`HostCore`。

```text
HostCore
  -> VerifiedPostgresAccessLease
  -> DurableProjectionJobService
       -> event seeder
       -> generic projection workers
       -> canonical mutation surface workers
```

Service：

- 不持有 `HostSession`；
- 不持有 `RuntimeSession`；
- 不依赖 active run；
- 多 HostCore/进程并发时依靠 DB lease；
- session close后仍可完成 derived projection。

`InMemoryDurableProjectionRepository`只用于unit tests和现有明确non-durable composition，health必须
标记`non_durable_test_mode`，不得声称restart recovery。把整个in-memory product branch移出
production仍由D4负责；D3不得借此保留PostgreSQL adapter的`dsn | in_memory`双入口。

### 13.2 Startup

顺序：

```text
verify schema
-> acquire verified PostgreSQL borrower
-> verify runtime admission epoch is normal and registry-prefix exact
-> construct unique session bootstrap/admission guard ports
-> construct projection repositories/handlers
-> recover expired leases
-> start seed scan
-> start workers
-> expose Host session admission
```

Schema 未 verified不得启动 worker。

Executable registry/contract completeness是Host admission gate；external target当前可达性不是。
Handler constructor不得通过网络probe Oxigraph/embedding/search。Provider暂时不可达时service以
`worker_unavailable|retrying`运行并保留durable backlog，不能把derived target outage重新变成
canonical Host startup blocker。

### 13.3 Close

顺序：

```text
close new Host/session admission
-> close RuntimeSessions
-> stop new seed/claim admission
-> bounded drain active projection handlers
-> settle/relinquish what can be proven
-> leave unresolved work leased for expiry
-> close projection service
-> close retrieval/vector/Oxigraph resources
-> release PostgreSQL borrower
```

Vector handler可能借用 embedding provider，因此 projection service必须先于 retrieval resources关闭。

### 13.4 Wake subscriber

唯一 production publisher integration：

```python
class DurableProjectionWakeSubscriber:
    async def on_published_event(
        self,
        published: "RuntimePublishedEvent",
    ) -> None:
        self._wake_event.set()
```

它：

- 不解析完整 event；
- 不 claim；
- 不查询 DB；
- 不承诺对应 event已 admission；
- close后 no-op；
- wake coalescing合法。

### 13.5 Polling

Wake只是latency hint。

V1：

```text
active backlog poll <= 1 second
idle seed sweep <= 5 seconds
session enumeration page <= 256 rows
dead-letter不自动重试
```

Poll使用paged query和单批预算，不全量加载。每个idle tick至少推进一个integrity-lane page；
fast-lane backlog不得永久饿死integrity cursor。

### 13.6 Physical operation ownership

Sync PostgreSQL/artifact/Oxigraph/index code只能经process-owned bounded physical operation service
执行。`DurableProjectionJobService`拥有task/future与operation lifecycle，但不创建ThreadPoolExecutor。

新增：

```text
runtime.blocking_executor.projection_maintenance_executor()
    process singleton
    max workers = 9
    thread prefix = pulsara-projection-maintenance

PostgresConnectionLane.PROJECTION_MAINTENANCE
    min size = 0
    max size = 8
    max waiting = 32
```

V1 process-wide concurrency：

```text
seed/source operations = 1
generic projection handlers = 4
surface handlers = 4 total, max 1 per (surface, sequence_key)
```

要求：

- 多个HostCore只borrow同一database-target-partitioned physical service/executor/semaphores，不得各建
  九线程；
- seed/job/result/surface所有PostgreSQL phase使用`PROJECTION_MAINTENANCE` lane；
- projection source reader直接在该lane执行bounded EventLog table reads，不借用
  critical EventLog writer executor/reserve；
- critical ledger executor和EventLog critical-write capacity始终独立保留；
- 禁止untracked `asyncio.to_thread()`；
- task/thread future由service owner保存直到terminal；
- waiter cancellation只detach，不丢physical owner；
- PostgreSQL phase设置statement timeout并传absolute deadline；
- external SDK必须有bounded timeout；
- 无法主动取消的thread不被标记NONE/success，lease保持到settle或expiry；
- HostCore close按第13.3节drain自身operations并release borrower；process executor只在最后一个
  process service borrower释放且全部tracked operation terminal后关闭；
- blocking executor capacity、projection DB lane capacity与generic/surface semaphore必须进入
  startup doctor/Inspector，但不进入job semantic。

---

## 14. Publication/hook hard cut

### 14.1 删除 production heavy hooks

必须删除 production composition中的：

- `RunTimelinePersistenceHook` registration；
- `CanonicalMutationOutboxReplayHook` registration；
- `ExecutionEvidencePersistenceHook` AgentRuntime wiring/call。

### 14.2 RuntimeHookManager

`RuntimeHookManager` 可以保留给 UI/process-local observer，但：

- `errors` 改为 `deque(maxlen=256)`；
- error明确标记 `best_effort_observer_failure`；
- 每项message最多2048 UTF-8 bytes并使用closed sanitizer；
- 不作为 Inspector durable truth；
- hook contract明确禁止 storage mutation；
- architecture test检查 production registrations。

### 14.3 RuntimeEventPublisher

Publisher：

- subscriber diagnostics使用`deque(maxlen=256)`，单项最多2048 UTF-8 bytes；
- subscriber latency可观测；
- derived projection completion不进入 publication result；
- projection service不可用不把已经 committed AgentEvent重新分类为 publication failure；
- critical publication latch不用于 derived job dead-letter。

### 14.4 禁止递归 event

以下都不得写 AgentEvent：

- job retry；
- job dead-letter；
- lease expiry；
- external surface delivery failure；
- worker shutdown；
- repair/requeue。

原因：

```text
projection failure event
-> publisher
-> projection admission
-> projection failure event
```

会产生递归 truth domain。

Job tables、repair action table与 Inspector是唯一 operational authority。

---

## 15. Repair 与 dead-letter

### 15.1 禁止 raw UPDATE

Operator不得执行：

```sql
UPDATE durable_projection_jobs SET status = 'pending';
```

CLI/Inspector repair必须写：

```python
class DurableRepairAuthorityReferenceFact(FrozenFactBase):
    schema_version: Literal[
        "durable_repair_authority_reference.v1"
    ] = "durable_repair_authority_reference.v1"
    authority_kind: Literal[
        "operator_command",
        "deployment_configuration",
        "projection_rebuild",
        "source_authority_repair",
    ]
    authority_id: str
    authority_semantic_fingerprint: str
    reference_fingerprint: str


class DurableProjectionRepairActionFact(FrozenFactBase):
    schema_version: Literal[
        "durable_projection_repair_action.v1"
    ] = "durable_projection_repair_action.v1"
    repair_action_id: str
    job_id: str
    expected_state_revision: int
    expected_job_semantic_fingerprint: str
    expected_repair_generation: int
    action: Literal["retry_same_contract", "supersede_after_manual_repair"]
    operator_reason_code: "DurableProjectionRepairReason"
    authority_references: tuple[DurableRepairAuthorityReferenceFact, ...]
    requested_at: datetime
    resulting_repair_generation: int
    action_fingerprint: str
```

```python
class DurableProjectionRepairReason(StrEnum):
    TRANSIENT_DEPENDENCY_RESTORED = "transient_dependency_restored"
    SOURCE_AUTHORITY_REPAIRED = "source_authority_repaired"
    TARGET_AUTHORITY_REPAIRED = "target_authority_repaired"
    SURFACE_PROVIDER_REBOUND = "surface_provider_rebound"
    OPERATOR_SUPERSEDED = "operator_superseded"
```

规则：

- retry只允许相同 handler contract；
- contract升级不得伪装 same retry，必须新 projection kind/version；
- CAS expected revision；
- resulting repair generation必须等于expected + 1；
- repair action durable；
- successful admission increment repair generation；
- bounded diagnostic不得含自由 secret。
- `requested_at`由repository使用database clock分配；CLI/caller不得自报timestamp。

### 15.2 Surface repair/decommission

```python
class CanonicalMutationSurfaceRepairActionFact(FrozenFactBase):
    schema_version: Literal[
        "canonical_mutation_surface_repair_action.v1"
    ] = "canonical_mutation_surface_repair_action.v1"
    repair_action_id: str
    delivery_identity_fingerprint: str
    expected_state_revision: int
    expected_surface_head_fingerprint: str | None
    expected_repair_generation: int
    action: Literal[
        "retry_same_contract",
        "decommission_with_authority",
        "decommission_after_rebuild",
    ]
    authority_references: tuple[DurableRepairAuthorityReferenceFact, ...]
    rebuild_result_receipt_reference: (
        DurableProjectionResultReceiptReferenceFact | None
    )
    resulting_repair_generation: int
    requested_at: datetime
    action_fingerprint: str
```

规则：

- retry不能换handler/target compatibility；
- resulting repair generation必须等于expected + 1；
- decommission required composition-removal authority，并写
  `CanonicalMutationSurfaceDecommissionedReceiptFact`；
- rebuild required exact replacement surface identity/rebuild authority，不得假造old-surface
  effective head；
- `decommission_after_rebuild`只能在exact rebuild receipt已经durable FULL后提交，并把该receipt
  放入authority references和`rebuild_result_receipt_reference`；
- public API只接受receipt ID，不接受caller自报replacement fingerprint。Repository必须在同一
  transaction读取`durable_projection_result_receipts`，验证applied receipt、canonical mutation
  reference、surface delivery identity、handler compatibility与FULL terminal surface receipt，
  再从receipt派生replacement target identity；
- action与state/head transition同transaction；
- dead-letter predecessor未被合法terminal前，successors继续blocked。
- `requested_at`同样由repository database clock分配。

### 15.3 Authority conflict

`SOURCE_AUTHORITY_CONFLICT` / `RESULT_IDENTITY_CONFLICT`：

- 不允许自动 retry；
- target health标记 `authority_untrusted`；
- affected projection read path必须公开 degraded状态；
- 不能把旧/新任一版本静默选为 canonical。

---

## 16. Recovery/crash matrix

### 16.1 Event commit

| Window | Required result |
|---|---|
| AgentEvent NONE | seeder看不到，不建job |
| AgentEvent FULL，wake丢失 | periodic scan建job |
| AgentEvent UNKNOWN后最终FULL | ledger authority确认后建一次 |
| AgentEvent UNKNOWN后NONE | 不建job |

### 16.1.1 Session bootstrap

| Window | Required result |
|---|---|
| candidate冻结前crash | 无owner、无session row |
| session/cutovers transaction NONE | exact confirmation `NONE`，retry same candidate |
| commit FULL、response丢失/cancel | exact-read stable state并返回`FULL/exact_confirmed` |
| partial/mismatched session/cutover set | `CONFLICT`，禁止domain caller继续 |
| confirmation deadline耗尽 | `UNRESOLVED`，保留physical owner/diagnostic |

### 16.2 Seeder

| Window | Required result |
|---|---|
| candidate build前crash | checkpoint不动，重读 |
| jobs insert前crash | checkpoint不动 |
| transaction commit UNKNOWN | 新连接exact-read jobs + checkpoint；分类FULL/NONE/CONFLICT |
| duplicate seed | exact job semantic相同则no-op |
| same job ID payload不同 | authority conflict，checkpoint不推进 |

Seeder UNKNOWN confirmation必须是：

```text
FULL
NONE
CONFLICT
UNRESOLVED
```

`UNRESOLVED` 不得误报 corruption。

### 16.3 Worker lease

| Window | Required result |
|---|---|
| claim commit NONE | row仍可claim |
| claim FULL后worker crash | lease expiry后reclaim |
| handler cancellation before side effect | retry_wait或lease expiry |
| side effect FULL，settlement丢失 | idempotent retry/exact confirm |
| settlement UNKNOWN | exact-read lease/job/result receipt/head |
| applied receipt FULL、outcome丢失 | deterministic receipt/head exact-confirm |
| superseded receipt FULL、outcome丢失 | job state/receipt exact-confirm |
| stale older timeline完成 | superseded |
| same source不同result | dead-letter conflict |

### 16.4 Host close

Host close不得：

- 删除 pending jobs；
- mark in-flight success；
-等待所有 backlog清空；
-关闭 embedding/Oxigraph provider后继续执行 handler。
-释放process projection executor borrower时仍有tracked operation。

如果runner或tracked physical operation在close deadline内未terminal，`aclose()`必须返回typed
blocked failure；HostCore保持`CLOSING`（`CLOSE_BLOCKED`），拒绝新session、schema borrower和
projection wake admission，同时保留projection service、PostgreSQL access lease、
embedding/Oxigraph与retrieval resources。禁止记录diagnostic后恢复`OPEN`或继续释放dependency；
physical owner结束后重试close才可释放。

### 16.5 Cutover

| Data | Behavior |
|---|---|
| pre-cutover timeline/evidence | no automatic backfill |
| post-cutover trigger | normal durable job |
| existing pending mutation | migrate and continue |
| existing applied mutation | preserve applied |
| corrupt/ambiguous legacy mutation | v6 rollback; legacy table remains; inspection/reset required |
| v5 normal writer active when maintenance requested | migration waits its shared epoch lock；之后writer被fence |
| process-local old callback finishes after maintenance | runtime write rejected；drain owns recovery |
| drain target commits、coverage receipt前crash | rerun exact-confirm receipts/heads，尚无activation |
| coverage receipt FULL、activation前crash | reuse immutable receipt under same maintenance epoch |
| EventLog/session head differs from coverage receipt | activation rollback，regenerate receipt only after合法authority |
| activation commit UNKNOWN | exact-read migration ledger、normal/maintenance epoch、activation、cutovers与receipt set |
| v6 legacy binding plan head变化 | v6 rollback；regenerate plan |
| unprovable legacy applied/delete | typed rebuild/decommission或reset，不迁移为伪造applied |
| v5 FULL、v6 plan缺失 | runner返回`PREPARATION_REQUIRED`，head保持v5 |
| binding plan FULL、runner前crash | same maintenance operation exact reuse plan |
| v6 FULL、v7 coverage缺失 | runner返回timeline coverage requirement，head保持v6 |
| v7 FULL、v8 coverage缺失 | runner返回evidence coverage requirement，head保持v7 |
| final binary从v4启动migrate | 逐boundary推进；不得apply-all越过缺失preparation |

Maintenance失败后不得让Host自行清除barrier。只有migration FULL安装new normal epoch，或
`abort-maintenance`证明target migration NONE，才能恢复ordinary writes。

---

## 17. Inspector 与 CLI

### 17.1 Inspector projection

Inspector必须展示：

- job kind/id/target；
- target update policy；
- exact source event/sequence/horizon；
- status/revision；
- attempts；
- current lease generation/expiry；
- next retry；
- bounded failure；
- result receipt reference + exact applied/superseded receipt；
- target head；
- source cutover；
- runtime admission epoch/maintenance operation；
- pre-activation coverage receipt/page-root；
- v6 legacy binding applied receipt；
- current migration preparation requirement/historical-head maintenance binding；
- immutable graph relation references/lowering contract；
- blocked seed frontier/failure；
- pre-cutover `not_durably_observable`；
- surface delivery state；
- repair action lineage。

Inspector不得：

- 从 hook errors猜历史 failure；
- 将没有job解释为success；
- 把 pre-cutover absence解释为projection missing；
- 读取全表后内存筛选。

### 17.2 CLI

建议命令：

```text
pulsara db projections status
pulsara db projections status --session <id>
pulsara db projections dead-letters
pulsara db projections retry --job <id> --reason-code <closed-code>
pulsara db projections surfaces
pulsara db projections surface-retry --mutation <id> --surface <closed-surface> --authority-id <id>
pulsara db projections surface-decommission --mutation <id> --surface <closed-surface> --authority-id <id>
pulsara db projections drain-pre-activation --kind <closed-kind>
pulsara db projections plan-legacy-surface-bindings [--export <path>]
pulsara db maintenance status
pulsara db maintenance abort --operation <id>
```

`pulsara db migrate`输出必须投影`PostgresMigrationProgressOutcomeFact`。当status为
`preparation_required`时，CLI使用稳定非零“需要运维步骤”exit code，打印且只打印closed next
command与requirement fingerprint；不得继续apply、自动执行昂贵drain或建议reset。

所有 list：

- stable sort；
- bounded page；
- cursor pagination；
- 输出 registry/handler fingerprints；
- secret-safe。

### 17.3 Health

至少区分：

```text
healthy
backlogged
retrying
degraded_dead_letter
authority_untrusted
worker_unavailable
```

UI stream仍然best-effort。UI看到“event published”不得显示“timeline projected”。

---

## 18. Physical bounds 与性能门槛

### 18.1 Repository bounds

V1 hard bounds：

| Carrier | Bound |
|---|---:|
| identifier / kind / contract ID | 512 UTF-8 bytes each |
| durable job candidate canonical JSON | 512 KiB |
| durable result receipt reference canonical JSON | 16 KiB |
| durable applied result receipt canonical JSON | 512 KiB |
| durable superseded result receipt canonical JSON | 64 KiB |
| durable target authority conflict canonical JSON | 64 KiB |
| content-addressed artifact reference | 16 KiB |
| graph relation row/reference | 32 KiB |
| graph relation read page | 256 edges / 2 MiB |
| relation-aware node convenience view | 1024 edges / 4 MiB |
| prepared documents per result | 128 |
| prepared result aggregate canonical content | 8 MiB |
| timeline source page | 512 events / 8 MiB |
| timeline persistent-vector leaf | 128 items / 1 MiB |
| pre-activation coverage page | 256 targets / 8 MiB |
| pre-activation coverage receipt | 256 KiB |
| legacy surface binding page | 256 entries / 8 MiB |
| protected-relation registry entry | 16 KiB |
| canonical mutation candidates/references per result | 256 |
| source authority fingerprints per mutation | 128 |
| mutations per producer bundle | 256 |
| producer bundle aggregate carrier | 8 MiB |
| planned surfaces per mutation | 3 |
| repair authority refs | 16 |
| bounded diagnostic | 2 KiB UTF-8 |
| one repository fetch page | 256 rows / 16 MiB canonical JSON |

Tuple count、UTF-8 size与canonical JSON size都必须在central factory和repository admission两层
验证。Bounds及codec fingerprint进入对应handler/seed/migration contract；实现不得只依赖
PostgreSQL toast或driver memory。超过handler source/output bound进入typed dead-letter；
超过repository carrier bound是candidate factory error，checkpoint/UOW不得推进。

Timeline bounds只约束单page/node/resident state，不约束logical run总events/bytes。

禁止：

- `tuple(event_log.iter())`；
- `SELECT *` without limit；
- unbounded hook error list；
- unbounded dead-letter rendering；
- handler中全session扫描。

### 18.2 Publisher gate

Deterministic benchmark必须证明：

- 添加projection wake subscriber后，publisher callback不随timeline/artifact size增长；
- callback不建立PostgreSQL/Oxigraph/embedding连接；
- callback保留bounded `(runtime_session_id, projection_kind)` dirty hint；hint溢出只影响latency，
  periodic keyset scan仍保证eventual admission；
- authority scan取满一页时只`yield`并立即继续下一页，完整wrap后才进入idle poll；
- 1000 events wake coalescing不会创建1000 worker tasks；
- canonical run latency不等待projection backlog。

Gate以structural counters为主，不用易漂移的绝对毫秒：

```text
publisher storage/external calls = 0
1000 consecutive wakes -> newly created worker tasks <= 1
callback serialized source payload bytes = 0
blocked projection handler -> canonical event commit仍可完成
```

Small/maximum-size event callback wall time只作为诊断；若最大size显著高于small，architecture test应
定位unexpected payload parsing，不通过“放宽毫秒阈值”掩盖。

### 18.3 Worker gate

记录：

- seed events/sec；
- jobs claimed/sec；
- handler duration；
- retry/dead-letter rate；
- backlog age；
- lease expiry count；
- surface apply latency。

这些是operational metrics，不进入job semantic identity。

Deterministic load fixture至少包含100,000-event long run、10,000 mixed source events、
zero/one/many-trigger pages、expired leases和out-of-order timeline completion，并断言：

- seed SQL/read次数随page count线性，不随已处理历史重新增长；
- timeline fold source events总数不超过initial genesis fold + committed trigger deltas + bounded
  retry overhead，禁止每个trigger重扫run genesis；
- process常驻carrier不超过active claim batches + one seed batch；
- no handler持row transaction跨external I/O；
- backlog清空后integrity sweep仍继续前进；
- projection executor active threads `<= 9` process-wide，与HostCore数量无关；
- projection PostgreSQL checkout只使用`PROJECTION_MAINTENANCE` lane；
- saturated projection lane时critical EventLog commit/confirmation reserve仍可用。

### 18.4 Retention

V1不由runtime role删除job、dead-letter、repair或surface-delivery rows。

- all reads必须indexed/paged，因此durable history增长不能变成startup全表materialization；
- active/retry/dead-letter rows永不自动GC；
- succeeded/superseded rows也先保留，作为D3审计证据；
- immutable `graph_relation_facts`与引用它们的evidence result receipt同寿命；V1不GC；
- immutable applied/superseded result receipts必须至少与引用它们的job、target head、timeline
  manifest和coverage receipt同寿命；V1不GC；
- pre-activation coverage pages/receipts由activation/cutover永久引用，V1不GC；
- durable legacy surface binding plan pages/plan、applied receipt、rebase与privileged authority lineage至少与
  migrated V2 mutation/surface rows同寿命，V1不GC；
- future archive/retention若需要实施，必须新增privileged typed maintenance contract和
  content-addressed terminal tombstone，不得直接加入worker的普通DELETE路径。

这是“durable table线性增长”的明确V1选择，不等于允许无界process memory或无界单次query。

---

## 19. Security 与 trust boundary

1. Job payload只保存 exact source refs，不复制raw user/tool output。
2. Handler在执行时通过verified source reader hydrate。
3. Artifact locator只存在physical attribution。
4. Failure diagnostic必须脱敏。
5. Worker只使用 `VerifiedPostgresConnectionProvider` borrower。
6. External surface credentials只由provider owner持有，不进job row。
7. Handler registry closed；数据库字段不能决定任意Python import。
8. Unknown schema/version fail closed。
9. Runtime role无DDL；对D3 job/cutover/repair/mutation-delivery tables无generic DELETE。
10. Repair命令使用typed CAS，不接受raw SQL片段。
11. Protected production DML必须由database admission epoch guard授权；process-local“Host已停止”
    不是migration proof。
12. Legacy binding plan/coverage pages只保存secret-safe identities与digests，不保存embedding
    credential、DSN或raw tool/user payload。
13. Runtime guard nonce只存在admin-readable physical table；不进入report、event、job、Inspector或
    semantic fingerprint。
14. Relation lowering只接受closed predicate registry；数据库中的predicate字符串不能驱动任意
    SPARQL/SQL。

---

## 20. 实施阶段与独立 gate

每个阶段必须独立全绿。不得先删production owner，再在下一阶段补durable替代。

### DPJ0：Additive contracts、registries 与 architecture fixtures

新增：

- central DTOs/factories；
- per-trigger horizon与immutable result receipt contracts；
- full-replacement/single-assignment target policy与strict result-document union；
- executable runtime admission lock protocol、maintenance authority与protected-relation registry；
- unique RuntimeSession bootstrap port contract；
- pre-activation coverage receipt与legacy surface binding plan；
- prerequisite-aware migration registry/outcome；
- immutable graph relation lowering/read contract；
- projection kind/trigger/handler/surface registries；
- deterministic ID/fingerprint；
- source snapshot ports；
- retry/failure taxonomy；
- in-memory job repository state machine；
- architecture tests。

Production不切换。

Gate：

- DTO schema/fingerprint golden；
- prepared v5 migration definition不改变v0-v4 golden；
- registry set equality；
- source ref exact rebind；
- 相同trigger位于不同seed page时job semantic/candidate byte-identical；
- applied/superseded receipt factory和target-head exact rebind；
- conflicting second ToolResultEnd竞争同一evidence target并fail closed；
- result document union rejects unknown/cross-branch fields；
- coverage receipt/page stable-ID golden；
- migration prerequisite transition exhaustive tests；
- state transition exhaustive tests；
- secret redaction probes；
- old production行为仍绿。

### DPJ1：Migration 0005、PostgreSQL repositories 与 worker shadow

新增：

- migration v5；
- runtime admission epoch secret、advisory-lock SQL functions、exhaustive protected-relation
  registry/triggers；
- unique session bootstrap repository并迁移全部三条session producer；
- immutable `graph_relation_facts` repository + PostgreSQL/RDF lowering shadow；
- job/cutover/result-receipt/head/target-conflict/target-lease/repair repositories；
- canonical mutation V2 tables；
- seed commit port；
- claim/settle/recovery；
- HostCore-owned logical worker + process-owned projection executor/DB lane；
- generic timeline/evidence handler保持shadow；
- V2 surface handlers只做synthetic/shadow验证，legacy surface owners保持production。

Shadow seeder只运行pure candidate comparison，不写cutover、authoritative jobs或checkpoint。

Gate：

- fresh/behind/up-to-date migration，以及v4->v5后typed
  `PREPARATION_REQUIRED(v6 binding plan)`；
- schema drift/grant tests；
- normal/maintenance advisory-lock acquisition、trigger `pg_locks`验证、role matrix、stale writer
  rejection与abort-maintenance NONE；
- runtime DML/grant/catalog/protected-relation registry exact set-equality；
- EventLog/manifest/memory-UOW session creation全部原子产生required cutovers；
- bootstrap inserted/exact-confirm state一致，以及commit loss/cancel FULL/NONE/CONFLICT/UNRESOLVED；
- AST guard除bootstrap repository/migration外无`INSERT INTO sessions`；
- legacy `memory_write_outbox` remains byte-compatible and operational；
- synthetic V2 surface apply、settlement loss与idempotency；
- synthetic per-kind cutover/checkpoint fixtures；
- seed transaction crash matrix；
- multi-worker lease race；
- expired lease reclaim；
- shutdown；
- multi-HostCore共享executor/lane capacity，不创建N × 9 threads；
- no constructor DDL。

### DPJ2：Canonical mutation surface hard cut

同一阶段完成：

- migration v6 + restart cutover；
- v5 normal epoch -> v6 maintenance -> v6 normal epoch machine-enforced transition；
- require exact durable `LegacySurfaceMigrationBindingPlanFact` + applied receipt；plan command进入并保留
  同一v6 maintenance operation，export文件不作为authority；
- install immutable pre-activation timeline/evidence contracts；
- all existing mutation producers使用V2 bundle commit port；
- DPJ1 shadow exact source readers/assemblers成为pre-activation hook唯一prepared-result factory；
- timeline/evidence old triggers改用deterministic prepared result +
  `PreActivationProjectionCommitPort`，删除split output；
- migrate legacy ordering/surface rows；
- enable unified V2 surface workers；
- remove replay hook；
- remove vector-specific worker/claims；
- remove direct Oxigraph mutation；
- external side-effect settlement recovery。

Gate：

- v5 historical-head verifier、missing-plan preparation outcome、v6 resume与legacy row matrix；
- applied/pending/delete vector binding matrix及build-identity conflict；
- unprovable legacy surface必须decommission+rebuild或reset；
- binding plan pages/plan与v6 applied receipt immutable；plan maintenance authority、legacy
  accumulator或target proof变化使v6 rollback；
- maintenance barrier阻止已运行v5 Host在v6 migration期间提交；
- v0-v5 migration golden不变；
- v6 packaged pre-activation semantic与outer registry-prefix attribution无identity recursion；
- existing session × kind cutover count/prefix proof，以及new-session sequence-0 bootstrap；
- governance same-UOW preserved；
- graph reset/delete same-UOW；
- multi-graph/multi-surface lock-order与deadlock stress；
- per-surface ordering/predecessor gates；
- no row lock during external I/O；
- duplicate external apply idempotent；
- partial surface failure；
- dead-letter/repair；
- no production V1 payload mutation；
- pre-activation commit crash/target-head CAS matrix；
- pre-activation applied receipt在callback outcome丢失后仍可exact rebind；
- pre-activation owner在无activation时合法、伪造activation overlap被拒绝。

### DPJ3：Run timeline vertical hard cut

同一阶段完成：

- migration v7 + offline timeline activation cutover；
- 将paged incremental run reader/persistent reducer绑定到durable job owner；
- reuse DPJ2 transaction-aware output UOW，增加leased-job/target settlement；
- timeline handler production enable；
- activate timeline-only seed lane/checkpoint；
- delete timeline production hook registration。

Evidence仍由old production hook拥有，且没有evidence cutover/checkpoint。Timeline seeder不得
interpret或推进evidence lane。

Gate：

- maintenance epoch + immutable per-session coverage receipt覆盖完整
  start-exclusive/end-inclusive range；
- v6 head缺coverage时`db migrate`返回typed preparation requirement且不apply v7/v8；
- missing/failed receipt target、superseded-only receipt、epoch/head变化都会阻止v7 commit；
- drain完成后、v7 commit前crash可从result receipts/coverage pages幂等重跑；
- job trigger horizon严格等于trigger sequence，seed scan horizon不进入job identity；
- v7 cutover exact high-water与v0-v6 golden stability；
- trigger后event不进入结果；
- 100k+ event合法run按page fold，不因总history超过16 MiB dead-letter；
- successive triggers只foldtarget-head delta，load fixture证明无O(n²) full-history refold；
- persistent-vector root不受source page/base选择影响；
- duplicate/restart exact idempotency；
- out-of-order supersession；
- artifact+graph+mutation+head+settlement atomic；
- per-page/per-item oversize typed failure，不存在whole-run input cap；
- publisher callback无timeline storage I/O；
- existing timeline behavior fixture迁移。

### DPJ4：Tool-result execution evidence vertical hard cut

同一阶段完成：

- migration v8 + offline evidence activation cutover；
- 将existing exact tool-result join/deterministic assembler绑定到durable job owner；
- reuse DPJ2 evidence output UOW，增加leased-job/target settlement；
- activate evidence-only seed lane/checkpoint；
- delete AgentRuntime persistence hook；
- stop production failure-audit event。

Gate：

- maintenance epoch + immutable evidence coverage receipt逐tool-result target完整覆盖；
- evidence target key不含result fingerprint；同call第二个distinct ToolResultEnd无法生成第二个
  successful target；
- single-assignment target从不产生superseded receipt；
- missing/failed result receipt、epoch/head变化都会阻止v8 commit；
- drain完成后、v8 commit前crash可从result receipts/coverage pages幂等重跑；
- call/result/projection cross-pair rejection；
- v8 cutover exact high-water与v0-v7 golden stability；
- exact same ToolResultEnd replay只exact-confirm，不产生duplicate evidence；distinct second
  ToolResultEnd进入target authority conflict；
- crash/restart；
- graph+mutation+job atomic；
- same-turn parallel tool results使用immutable edge facts，任意commit顺序不丢relation；
- PostgreSQL relation row、relation-aware JSON-LD read与Oxigraph direct quad accumulator一致；
- ordinary `put_jsonld`拒绝relation-registry-owned predicates；
- projection failure不改变run outcome；
- architecture grep无production hook call。

### DPJ5：Recovery、Inspector、cleanup、contracts、dogfood

完成：

- Inspector/CLI；
- historical/pre-cutover projection；
- final class/module deletion；
- bounded diagnostics；
- Host close；
- long-term contracts；
- debt rebase；
- deterministic performance benchmark；
- process executor/PostgreSQL projection lane saturation benchmark；
- final binary从v4依次经历`v5 -> binding plan -> v6 -> timeline coverage -> v7 ->
  evidence coverage -> v8`的migration dogfood；
- one core durable dogfood。

本阶段不需要全量 real-LLM suite。Projection correctness应由deterministic tests证明。
Dogfood只验证：

-真实Host run不等待timeline/evidence；
- run结束后projection最终成功；
- restart后pending job继续；
- Inspector显示exact source/result。

Gate：

- 第22节Definition of Done逐项审计；
- full offline pytest；
- relevant PostgreSQL integration tests；
- frozen core dogfood；
- architecture grep/AST gate。

---

## 21. 修改面

实际文件可按现有package边界微调，但ownership不得改变。

### 21.1 New package

建议：

```text
src/pulsara_agent/runtime/projection_jobs/
    __init__.py
    contracts.py
    registry.py
    source.py
    seeder.py
    repository.py
    worker.py
    timeline.py
    execution_evidence.py
    pre_activation.py
    coverage.py
    canonical_mutation.py
    diagnostics.py
    service.py
```

低层storage实现：

```text
src/pulsara_agent/storage/projection_jobs/
    postgres.py
    in_memory.py

src/pulsara_agent/storage/
    runtime_admission.py
    session_bootstrap.py
```

### 21.2 Existing files

至少审计/修改：

```text
src/pulsara_agent/runtime/hooks.py
src/pulsara_agent/runtime/publisher.py
src/pulsara_agent/runtime/session.py
src/pulsara_agent/runtime/wiring.py
src/pulsara_agent/runtime/blocking_executor.py
src/pulsara_agent/host/core.py
src/pulsara_agent/host/session_manifest.py
src/pulsara_agent/runtime/agent.py
src/pulsara_agent/runtime/timeline.py
src/pulsara_agent/memory/foundation/run_timeline_query.py
src/pulsara_agent/memory/working_context.py

src/pulsara_agent/memory/hooks/run_timeline_persistence.py
src/pulsara_agent/memory/hooks/runtime_persistence.py
src/pulsara_agent/memory/canonical/outbox_replay_hook.py
src/pulsara_agent/memory/canonical/mutation_outbox.py
src/pulsara_agent/memory/canonical/unit_of_work.py
src/pulsara_agent/memory/canonical/ledger.py
src/pulsara_agent/memory/canonical/lifecycle.py
src/pulsara_agent/memory/canonical/write_service.py
src/pulsara_agent/memory/canonical/index_sync.py
src/pulsara_agent/memory/canonical/vector_index_sync.py
src/pulsara_agent/memory/canonical/oxigraph_materializer.py
src/pulsara_agent/memory/canonical/reconcile.py
src/pulsara_agent/memory/__init__.py

src/pulsara_agent/graph/durable_facade.py
src/pulsara_agent/graph/store.py
src/pulsara_agent/graph/postgres.py
src/pulsara_agent/graph/oxigraph.py
src/pulsara_agent/graph/jsonld_codec.py
src/pulsara_agent/storage/postgres_memory_projection.py
src/pulsara_agent/memory/canonical/query.py
src/pulsara_agent/memory/artifacts/postgres_archive.py
src/pulsara_agent/event_log/protocol.py
src/pulsara_agent/event_log/postgres.py
src/pulsara_agent/event_log/in_memory.py
src/pulsara_agent/storage/postgres_connection_provider.py
src/pulsara_agent/inspector/store.py
src/pulsara_agent/inspector/service.py
src/pulsara_agent/cli.py

src/pulsara_agent/storage/migrations/registry.py
src/pulsara_agent/storage/migrations/manifest.py
src/pulsara_agent/storage/migrations/grants.py
src/pulsara_agent/storage/migrations/runner.py
src/pulsara_agent/storage/migrations/verifier.py
src/pulsara_agent/storage/migrations/sql/0005_durable_projection_jobs.sql
src/pulsara_agent/storage/migrations/sql/0006_canonical_mutation_surface_jobs.sql
src/pulsara_agent/storage/migrations/sql/0007_run_timeline_projection_activation.sql
src/pulsara_agent/storage/migrations/sql/0008_tool_result_evidence_projection_activation.sql
src/pulsara_agent/storage/migrations/resources/0005_runtime_write_protected_relations_v1.json
src/pulsara_agent/storage/migrations/resources/0006_runtime_write_protected_relations_v2.json
src/pulsara_agent/storage/migrations/resources/0006_pre_activation_projection_contracts_v1.json
src/pulsara_agent/storage/migrations/resources/0006_legacy_surface_binding_plan_contract_v1.json
src/pulsara_agent/storage/migrations/resources/0007_run_timeline_activation_v1.json
src/pulsara_agent/storage/migrations/resources/0008_tool_result_evidence_activation_v1.json
src/pulsara_agent/storage/migrations/expected_catalog_v5.json
src/pulsara_agent/storage/migrations/expected_catalog_v6.json
src/pulsara_agent/storage/migrations/expected_catalog_v7.json
src/pulsara_agent/storage/migrations/expected_catalog_v8.json
```

`src/pulsara_agent/cli.py`只通过`projection_jobs/pre_activation.py`暴露typed offline drain命令；
CLI不得自行扫描EventLog、构造target coverage或写activation/cutover rows。
Legacy binding plan命令同样只调用closed migration plan factory；CLI不得接受raw handler
fingerprint、surface receipt或任意SQL。

必须通过 `rg` 审计所有：

```text
MutationOutboxWriter
memory_write_outbox
ExecutionEvidenceLedger
RunTimelinePersistenceHook
ExecutionEvidencePersistenceHook
CanonicalMutationOutboxReplayHook
record_tool_result_block
_record_turn_produced
rt:produced / rt:provides direct document mutation
direct Oxigraph write
INSERT INTO sessions
untracked asyncio.to_thread
```

不能只修改已知wiring call site。

### 21.3 Tests 与 benchmark

至少新增/迁移：

```text
tests/test_durable_projection_seed.py
tests/test_durable_projection_jobs.py
tests/test_durable_projection_recovery.py
tests/test_runtime_write_admission_epoch.py
tests/test_runtime_session_owner_bootstrap.py
tests/test_postgres_migration_preparation_state_machine.py
tests/test_pre_activation_projection_coverage.py
tests/test_graph_relation_lowering.py
tests/test_canonical_mutation_surface_delivery_v2.py
tests/test_legacy_surface_migration_binding.py
tests/test_projection_job_inspector.py
tests/test_runtime_timeline.py
tests/test_execution_evidence_ledger.py
tests/test_memory_index_sync.py
tests/test_memory_vector_index_sync.py
tests/test_oxigraph_materializer.py
tests/test_schema_migrations.py
tests/test_schema_hot_path_architecture.py
tests/test_runtime_event_architecture.py

benchmarks/suites/
    durable_projection_pipeline.py
```

现有legacy tests不得仅改import后继续直接操作`memory_write_outbox`。DPJ2后：

- behavior tests通过V2 commit port/worker构造fixture；
- migration tests可以在restricted v5 database中直接插入legacy rows；
- unit tests可用explicit in-memory repository；
- production-composition tests必须使用verified PostgreSQL lease。

---

## 22. Definition of Done

### 22.1 Durable admission

- [x] Active kind/seed contract来自immutable migration-owned activation row，不能由current code自证。
- [x] Pre-activation contract semantic来自packaged v6 resource，outer migration attribution不反向进入
      semantic fingerprint。
- [x] Timeline/evidence trigger来自exact committed EventLog。
- [x] 每个job trigger horizon严格等于trigger event sequence；seed scan page/high-water不进入job
      semantic或candidate。
- [x] Seeder不按target key去重distinct source events；同一tool call的第二个ToolResultEnd进入
      single-assignment target conflict路径。
- [x] EventLog FULL后即使publisher wake丢失，periodic seeder仍会admit。
- [x] Jobs与seed checkpoint同transaction。
- [x] Seed checkpoint绑定canonical ledger prefix和immutable cutover。
- [x] Seed failure、repair action与resolution形成immutable exact chain。
- [x] Duplicate seed只exact-confirm，不产生新identity。
- [x] Seeder使用stable keyset cursor覆盖超过256个authority、尾页wrap-around；单authority
      failure durable化后继续本页。

### 22.2 Job ownership

- [x] Stable job key/semantic fingerprint。
- [x] Closed handler registry。
- [x] Lease generation/token/expiry。
- [x] 同target durable execution lease确保至多一个active handler；full-replacement选择latest
      source，single-assignment选择首次source并拒绝后续distinct source。
- [x] Bounded deterministic retry。
- [x] Dead-letter与typed diagnostic。
- [x] Cancellation/Host close不丢owner。
- [x] Settlement UNKNOWN有FULL/NONE/CONFLICT/UNRESOLVED确认。
- [x] Applied/superseded result均有immutable durable receipt；job/target/pre-activation只保存exact
      receipt reference。
- [x] Prepared/final result documents使用artifact/graph-document/graph-relation strict union；
      artifact required media/codec/metadata/content-addressed reference，无`projection_receipt`分支。
- [x] Target head可以仅凭receipt table重绑documents/mutations/effective owner，不依赖process-local
      outcome或job row存在。
- [x] Timeline target policy是`full_replacement`；evidence是`single_assignment`。
- [x] 同一tool call的第二个distinct terminal event竞争同一target并fail closed，不能双成功或
      supersede。
- [x] Target authority conflict与job dead-letter同transaction写immutable conflict row；claim、
      coverage、read facade与Inspector均从该row派生`authority_untrusted`。
- [x] Claim SQL在global limit前排除active-leased/conflicted target；hot target不能饿死其他
      eligible target。
- [x] Host close若tracked physical operation未terminal则保持`CLOSING/CLOSE_BLOCKED`并保留
      dependency leases、拒绝新session；physical owner收口后由重试close完成释放。

### 22.3 Timeline

- [x] Target-head base + exact trigger horizon之间paged fold。
- [x] 无trigger后event。
- [x] Whole-run history不受16,384 events / 16 MiB第二窗口限制。
- [x] Persistent item vector使用absolute ordinal/fixed split，root不受page/base影响。
- [x] Normal successive trigger成本随新增delta增长，不重复fold全部历史。
- [x] Ordinary query/working-context/Inspector使用paged manifest，不隐式materialize full timeline。
- [x] Working-context在timeline receipt迟到后通过bounded sparse read eventually lazy refresh，
      不从EventLog重建timeline。
- [x] IDs/timestamps deterministic。
- [x] Artifact/graph/mutation/result receipt/head/job同UOW。
- [x] Out-of-order job不会回退target。
- [x] Old production timeline hook已删除。

### 22.4 Execution evidence

- [x] Tool call/result/terminal projection exact join。
- [x] Malformed/non-object tool arguments保留raw identity与typed parse disposition，不误报
      source authority corruption。
- [x] 不再使用UUID/wall-clock semantic identity。
- [x] Evidence graph/mutation/result receipt/head/job同UOW。
- [x] Turn-produced 与 ToolResult-artifact 使用immutable per-edge facts，不read-modify-write shared
      Turn/Artifact documents。
- [x] Evidence owner在relation前put-if-absent-or-confirm-identical Turn/Artifact base documents，
      测试不依赖预建fixture。
- [x] Relation fact同UOW写入immutable PostgreSQL row，并由Oxigraph handler降为exact direct quad。
- [x] `get_jsonld_read_view` typed relation view、legacy `get_jsonld` compatibility materialization、
      paged relation query与ordinary `put_jsonld` owned-predicate rejection已按同一lowering
      contract切换。
- [x] Same-turn parallel jobs任意commit顺序都保留全部relations。
- [x] AgentRuntime不再同步调用persistence hook。
- [x] Production不再以failure audit代替retry owner。

### 22.5 Canonical mutation

- [x] Immutable mutation与mutablesurface state分离。
- [x] 所有producer使用same-UOW commit port。
- [x] Search/vector/Oxigraph使用统一lease contract。
- [x] Surface-local ordinal/predecessor严格连续，较新mutation不能自动supersede旧delta。
- [x] 外部I/O不持PostgreSQL row transaction。
- [x] External side effect + lost settlement可恢复。
- [x] Replay hook、vector special claim、direct Oxigraph production path已删除。
- [x] Surface dead-letter拥有transactional retry/decommission CAS、terminal receipt、head推进与
      `surface-retry`/`surface-decommission` CLI。

### 22.6 Publisher

- [x] Publisher callback无DB/archive/Oxigraph/embedding I/O。
- [x] Wake subscriber O(1)、可coalesce。
- [x] Hook/publisher diagnostics bounded。
- [x] Job failure不写recursive AgentEvent。
- [x] Canonical commit不等待projection backlog。

### 22.7 Migration/recovery

- [x] Migrations v5/v6/v7/v8 immutable并通过fresh/behind/drift测试。
- [x] Migrator是prerequisite-aware state machine；缺plan/coverage返回typed
      `PREPARATION_REQUIRED`并停在current head，不连续误跑later migrations。
- [x] Final v8 binary可使用historical-head maintenance binding将v4依次推进到v8；historical binding
      不能启动production Host。
- [x] v5追加后v0-v4不变，v6追加后v0-v5不变，v7追加后v0-v6不变，
      v8追加后v0-v7不变。
- [x] Existing pending mutation不丢。
- [x] Legacy surface contract全部来自typed binding plan；vector applied/pending/delete matrix无
      current-composition猜测。
- [x] Binding plan由v5 durable plan tables拥有，在同一v6 maintenance operation中生成/消费；
      CLI export不能成为migration authority。
- [x] Unprovable historical applied/delete只允许decommission+exact rebuild receipt或reset。
- [x] Timeline/evidence各自拥有独立cutover、checkpoint与seed lane。
- [x] Preactivation output owner与durable job owner按kind互斥，handoff无split write/owner gap。
- [x] V6为全部existing session × kind写canonical pre-activation cutover；v6后session bootstrap
      原子写入active/pre-activation互斥的sequence-0 cutovers。
- [x] `RuntimeSessionOwnerBootstrapPort`是唯一session INSERT owner；manifest/EventLog/memory UOW均
      使用它，AST guard无旁路。
- [x] Bootstrap stable state不含`created_new_session`；commit/cancel/connection-loss统一得到
      FULL/NONE/CONFLICT/UNRESOLVED exact confirmation。
- [x] Bootstrap exact confirmation比较完整ordered cutover DTO/fingerprint及数量，不只比较kind set。
- [x] V5 normal write epoch覆盖所有source/mutation/session/projection writers。
- [x] Guard SQL function取得secret-derived transaction advisory locks；relation trigger通过
      `pg_locks`、epoch、role与operation matrix验证。
- [x] Protected-relation registry与production DML inventory、runtime grants、expected catalog完全
      set-equal；不存在“至少保护这些表”的开放清单。
- [x] V6/V7/V8通过database maintenance epoch阻止已运行Host继续提交，不依赖人工quiesce。
- [x] V7/V8 activation前offline drain写immutable per-session coverage pages/receipts，完整覆盖
      `(pre-activation cutover, activation frozen head]`。
- [x] Coverage page key与receipt ID按第8.1节公式稳定；drain crash在同operation下exact reuse。
- [x] Activation transaction只验证maintenance epoch、receipt/session/head aggregates并bulk
      install cutovers，不重扫全部events/targets。
- [x] Drain后EventLog head变化、maintenance epoch变化或任一missing/failed receipt都会阻止
      activation commit。
- [x] Pre-cutover timeline/evidence不被伪造backfill，两个kind的activation顺序不造成
      duplicate projection或owner gap。
- [x] Restart恢复pending/retry/expired lease。
- [x] Runtime role无DDL，且对D3 operational tables无DELETE；既有domain-table privilege仍由
      schema manifest精确管理。

### 22.8 Inspection

- [x] Inspector显示source horizon、job、target head、surface状态。
- [x] Inspector可以exact显示applied/superseded result receipt及pre-activation coverage receipt。
- [x] Pre-cutover显示`not_durably_observable`。
- [x] Dead-letter repair为typed CAS action。
- [x] CLI list bounded/paged/secret-safe。

### 22.9 Architecture guards

- [x] `on_published_event` production path无heavy storage。
- [x] 无production `RunTimelinePersistenceHook`。
- [x] 无production `CanonicalMutationOutboxReplayHook`。
- [x] 无production `ExecutionEvidencePersistenceHook`。
- [x] 无handler dynamic import。
- [x] 无constructor DDL。
- [x] 无raw EventLog full scan。
- [x] 无job failure AgentEvent。
- [x] 除唯一bootstrap repository/migration外无production `INSERT INTO sessions`。
- [x] Protected DML必须经过runtime admission SQL guard。
- [x] Production relation writes不再read-modify-write Turn/Artifact JSON；owned predicates只能经
      graph relation port。
- [x] Projection code不创建私有ThreadPoolExecutor，不使用untracked `asyncio.to_thread()`。
- [x] Projection DB phase不借用critical EventLog writer lane。

### 22.10 Debt accounting

完成后只关闭 D3：

- durable hook/projection job ownership；
- timeline离开publisher；
- tool-result evidence获得durable retry owner；
- canonical mutation surface delivery统一；
- lease/retry/dead-letter；
- lightweight UI/CLI observation。

不得误报关闭：

- D4 dependency-cycle/test-support；
- D5 compaction-memory extension；
- D6 AgentRuntime/HostSession coordinator split；
- generic async PostgreSQL性能支线。

---

## 23. 长期合同同步

按阶段同步，不得等DPJ5一次性补文档：

### DPJ0/DPJ1

- `POSTGRES_SCHEMA_MIGRATION_CONTRACT`；
- `EVENT_LOG_STORAGE_CONTRACT`；
- `RECOVERY_CONTRACT`；
- verified PostgreSQL connection/lane contract；
- RuntimeSession owner bootstrap、database runtime-admission epoch与maintenance barrier contract；
- prerequisite-aware migration/historical-head maintenance contract。

### DPJ2

- canonical memory mutation/outbox contract；
- search/vector/Oxigraph materialization contract；
- governance same-UOW contract；
- `GRAPH_JSONLD_STORAGE_CONTRACT`；
- legacy surface binding/rebind/rebuild contract。

### DPJ3

- `RUNTIME_EVENT_PUBLISHING_HOOKS_CONTRACT`；
- `ARTIFACT_STORE_CONTRACT`；
- `GRAPH_JSONLD_STORAGE_CONTRACT`；
- timeline/Inspector contract；
- persistent timeline manifest/vector、paged query与pre-activation coverage contract。

### DPJ4

- `RUNTIME_SEMANTIC_GRAPH_CONTRACT`；
- tool execution/result projection contract；
- Agent loop contract；
- immutable Turn-produced/ToolResult-artifact relation contract；
- GraphStore JSON-LD read/write、PostgreSQL relation-row与Oxigraph quad lowering contract。

### DPJ5

- `INSPECTOR_PROJECTION_CONTRACT`；
- architecture debt rebase；
- operational CLI contract。

若仓库中实际合同文件名不同，使用现有合同文件；不得另建平行真源。

---

## 24. 最终裁决

D3 的正确中心不是“让 hook 更可靠”，而是：

```text
Canonical truth
    -> durable, replayable projection admission
    -> durable physical owner
    -> idempotent target mutation
    -> exact terminal receipt
```

在该模型中：

- hook只是可选wake；
- EventLog是timeline/evidence source authority；
- canonical mutation outbox是surface delivery source authority；
- job row是retry/lease/dead-letter authority；
- target head是derived projection ordering authority；
- Inspector读取durable state，不读取process-local error猜测历史。

第22节已经全部满足，因此`PULSARA_RUNTIME_ARCHITECTURE_DEBT_REBASE.zh.md` 的
**D3 Durable hook/projection jobs** 已从 `OPEN` 改为 `CLOSED`。
