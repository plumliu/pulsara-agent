# Pulsara Post-Compaction Memory Extraction Hard-Cut 实施规格

状态：**已实施并通过 CME0-CME5 gates，D5 CLOSED**

对应债务：`PULSARA_RUNTIME_ARCHITECTURE_DEBT_REBASE.zh.md` 的 D5

实施代号：`CME0 -> CME5`

最后修订：2026-07-28

---

## 0. 文档地位

本文冻结 Pulsara D5 **Compaction-memory extension** 的最终生产拓扑、durable authority、模型调用边界、候选治理边界、迁移顺序、逐文件修改面、阶段 gate 与 Definition of Done。

关键词：

- **MUST / 必须**：实现不得偏离；
- **MUST NOT / 禁止**：hard cut 后不得存在；
- **SHOULD / 应**：只有经显式 reviewer 裁决才可偏离；
- **MAY / 可以**：不影响本阶段核心不变量。

本文 supersede 债务文档 13.3 节中以下旧建议：

> 保留“一次 compact 模型调用同时返回 summary 与 optional extension”的产品优化。

D5 最终裁决改为两次职责严格分离的模型调用：

```text
Call A: Context compaction summary
  - 同步、关键路径
  - 只负责恢复上下文连续性
  - 输出只能是 summary

Call B: Post-compaction memory extraction
  - 异步、可选、durable job 驱动
  - 只读取 exact canonical evidence
  - 只产生待治理 memory candidates
  - 永远不能直接写 durable memory
```

这不是“在 summary 后再随手启动一个 task”。Call B 必须拥有 EventLog admission、durable job、lease/retry、exact model lifecycle、候选 outbox 与 recovery authority。

### 0.1 核心裁决摘要

| 问题 | 冻结结论 |
|---|---|
| Summary 与 memory extraction 是否共用一次模型调用 | 否，必须拆成两个调用 |
| Call B 是否阻塞 compaction 成功 | 否 |
| Call B 是否阻塞下一次用户 run | 否；低优先级 admission，foreground arrival 可立即 preempt |
| Call B 是否可直接写 durable memory | 否，只能产生 candidate |
| Call B 的 canonical source | lossless transcript projection 派生的 typed human-evidence manifest |
| Previous summary 是否可作为 memory evidence | 否 |
| Tool output、assistant text、runtime observation 是否可作为 V1 evidence | 否 |
| Call B 的 durable trigger | 与 `ContextCompactionCompletedEvent` 同批提交的 typed Requested event |
| Completed FULL 后 crash 是否会丢 extraction | 否，D3 seeder 可恢复 job |
| 无活 RuntimeSession 时是否算失败 | 否，job 保持 `PENDING` |
| V1 candidate kinds | 仅 `Preference` |
| Call B 成本上限 | target token/byte budget + durable per-session call/input/output-token/milliunit account |
| Call B raw output authority | confirmed ModelCall terminal projection；不另存raw output artifact |
| Candidate 是否自动进入 canonical memory | 否，继续经过 candidate pool 与 governance |
| 历史数据迁移 | 不兼容 hard cut，reset-only |

---

## 1. 为什么必须是两次调用

### 1.1 两个输出承担不同 correctness 责任

Compaction summary 是 context continuity 的一部分。它必须：

- 在上下文压力下及时完成；
- 忠实承接 previous summary 与新 compacted transcript；
- 失败时按 compaction lifecycle 终结；
- 成功后立即允许主流程继续。

Memory extraction 则是 optional derived work。它必须：

- 只从可验证 evidence 中提出候选；
- 容忍 provider 暂时不可用、输出不合法或候选为零；
- 允许异步 retry；
- 永远不能改变已完成 summary 的合法性；
- 永远不能把模型推断直接升级为 durable memory。

将两者塞进一个输出会制造四种错误耦合：

1. memory JSON 格式错误可能拖垮合法 summary；
2. summary prompt 为兼顾候选提取而变长、变复杂；
3. memory evidence 会退化为 summary prose，而非 original canonical source；
4. memory schema、ontology 或 governance 变化会继续修改 compaction core。

### 1.2 第二次调用的额外成本是可控且可审计的

Call B 不重放完整 compaction input。它只读取 bounded、typed、sanitized direct human evidence，因此输入通常显著小于 Call A。

Call B 还具备以下退出路径：

- extension 未配置：不建 job；
- human-evidence manifest 中没有 eligible evidence：不调用 provider，提交 typed empty result；
- session 在执行前关闭：typed supersede；
- provider 或 parser 暂时失败：durable retry；
- 达到 retry cap：dead-letter，由 Inspector/CLI 暴露。

此外，Call B 在 dispatch前必须同时取得 target-aware input budget与session lifetime
background-derived-work cost reservation；任一不足都形成typed no-call terminal result。

因此“两次调用”不等于“每次 compaction 必然多付一次完整上下文调用”。

### 1.3 与 memory reflection 的关系

当前 `memory/reflection` 已经证明 memory-owned 独立模型调用是可行的。D5 复用其 model lifecycle 方向，但不复制其全部候选语义。

两者边界如下：

| 维度 | Memory reflection | Post-compaction extraction |
|---|---|---|
| Trigger | run/safe-point policy | completed compaction request event |
| Source | reflection input snapshot | exact newly compacted transcript human-leaf manifest |
| V1 candidate kinds | 现有 closed union | 仅 Preference |
| Durable work owner | 现有 reflection owner | D3 durable projection job |
| Evidence | reflection-reported quote | exact canonical human input reference |
| Retry | 当前 reflection contract | D3 lease/retry/dead-letter |

D5 不删除 reflection。两个 producer 必须共享 candidate semantic identity 与 governance dedupe，而不能各自发明候选去重规则。
也禁止直接把 Request交给当前 reflection engine：它仍以run/safe-point为owner，不能证明
本规格的manifest、D3 lease、background budget与same-transaction result receipt。

---

## 2. 当前代码真值与债务

### 2.1 Compaction core 仍拥有 memory 产品语义

当前 `src/pulsara_agent/runtime/compaction/candidates.py` 直接依赖：

- `memory.candidates.pool`；
- memory scope/domain/ontology；
- `PreferenceCandidate` 与 pooled candidate；
- candidate ID、intent fingerprint、quoted evidence locator；
- `<memory_candidates_json>` parser 与 normalization。

`ContextCompactionPolicy` 仍嵌入 `ContextCompactionMemoryCandidatePolicy`，`ContextCompactionService` 仍持有：

- `candidate_sink`；
- `candidate_projection_commit_port`；
- process-local `CompactionCandidateProjectionReceipt`；
- candidate producer event 与 outbox row factory。

这使 `runtime/compaction -> memory` concrete dependency 继续存在，违反 D4 后的 target DAG 方向。

### 2.2 当前 one-call prompt 混合两个协议

`runtime/compaction/prompts/context_compaction_prompt.md` 同时要求：

- `<summary>`；
- optional `<memory_candidates_json>`。

`production_compaction_prompt(memory_candidates_enabled=...)` 通过字符串替换动态删改 prompt。该做法有三个问题：

1. prompt 文件不是单一稳定 contract；
2. summary parser 必须知道 memory tag；
3. model-visible summary task 与 memory task 无法分别 fingerprint、version 与回归。

### 2.3 当前 crash gap 尚未闭合

当前顺序是：

```text
ContextCompactionCompletedEvent FULL
    -> install process-local candidate projection owner
    -> parse summary memory block
    -> prepare ContextCompactionMemoryCandidatesProposedEvent
    -> commit producer event + candidate projection outbox
```

如果进程在第一步与第三步之间退出：

- summary 已 durable；
- candidate request 尚未 durable；
- reopen 禁止 post-scan Completed 猜测是否应提取；
- memory extraction 永久丢失。

D2 正确地没有用 post-scan fallback 掩盖该窗口。D5 必须通过同批 request admission 正式闭合。

### 2.4 当前 governance evidence 绑定错误 source

当前 compaction candidate governance 使用 summary artifact 作为 source evidence：

```text
candidate
  -> ContextCompactionMemoryCandidatesProposedEvent
  -> ContextCompactionCompletedEvent
  -> summary artifact text
```

summary 是 derived continuity artifact，不是用户原话。它可能：

- 合并多个说话者；
- 省略否定或时间条件；
- 将 tool output 改写成陈述；
- 将旧 recalled memory 再次概括。

因此 summary 只能作为 correlation artifact，不能继续作为 V1 durable-memory evidence authority。

---

## 3. 目标与非目标

### 3.1 必须完成

D5 必须完成：

1. summary-only Call A hard cut；
2. generic post-completion extension port；
3. transcript-derived exact human-evidence manifest；
4. `ContextCompactionCompletedEvent + MemoryExtractionRequestedEvent` 原子 admission；
5. D3 新 projection kind、seeder、job、lease、retry 与 dead-letter；
6. session-scoped background model driver registry；
7. Call B 独立 model purpose、runtime request、provider-input lane 与 lifecycle attribution；
8. strict Preference-only output codec；
9. exact human evidence nodes、input artifact 与 model-output recovery；
10. target-aware input budget与session durable background cost account；
11. 唯一 RuntimeSession writer下的event/receipt/head/outbox/job原子settlement；
12. governance 从 summary evidence 切到 canonical sanitized human evidence；
13. semantic identity与physical occurrence attribution彻底分层；
14. 删除 compaction core 中全部 memory concrete ownership；
15. reset-only migration、Inspector、CLI、tests、dogfood 与 architecture guard；
16. D5 最终从 debt document 标记为 `CLOSED`。

### 3.2 明确不做

D5 不做：

- 不让 compaction summary 直接写 memory；
- 不让 extraction candidate 绕过 governance；
- 不从 previous summary、recalled memory 或 working context 反推新 memory；
- 不从 assistant reply 或 tool result 自动提取 Claim/Observation；
- 不在 V1 产生 `Claim`、`Observation`、`ActionBoundary` 或 `Decision`；
- 不合并或删除 memory reflection；
- 不把所有 background model calls 泛化为用户可扩展 job framework；
- 不为了 D5 拆分 `AgentRuntime` 或 `HostSession`，那属于 D6；
- 不迁移旧 compaction proposed event、旧 candidate pool row 或旧 governance evidence；
- 不恢复被 reset 的 PostgreSQL/Oxigraph world；
- 不要求重跑全部历史 real-LLM pytest。

---

## 4. 最终架构

### 4.1 总拓扑

```text
bounded compaction source snapshot -> Call A: summary only
verified lossless transcript       -> human-evidence manifest artifact preparation
                                      (physical I/O runs in parallel with Call A)
                         \           /
                          \         /
atomic RuntimeSession batch
  ContextCompactionCompletedEvent
  ContextCompactionMemoryExtractionRequestedEvent
        |
        v
D3 durable seeder
        |
        v
COMPACTION_MEMORY_EXTRACTION job
        |
        v
session background model-call admission
        |
        v
Call B: exact human evidence -> strict Preference proposals
        |
        v
atomic RuntimeSession settlement
  ContextCompactionMemoryExtractionCompletedEvent
  memory_candidate_projection_outbox rows
  durable projection result receipt / target head / job success
        |
        v
candidate pool
        |
        v
memory governance
        |
        v
canonical memory, or no write
```

### 4.2 唯一真源

| 事实 | 唯一 owner |
|---|---|
| summary text | summary artifact referenced by `ContextCompactionCompletedEvent` |
| eligible human source set | `CompactionHumanEvidenceManifestReferenceFact` |
| 是否请求 memory extraction | same-batch Requested event |
| extraction work lifecycle | D3 durable projection job |
| physical model lifecycle | ModelCall events + terminal projection |
| exact extraction input | content-addressed input artifact |
| exact raw extraction output | confirmed terminal projection document |
| stable parsed result waiting for write | immutable `CompactionMemoryExtractionResultCandidateFact` |
| parsed candidate occurrence | extraction Completed event attribution |
| candidate delivery to pool | memory candidate projection outbox |
| durable memory write | governance UOW |

禁止以下第二真源：

- summary 中的 hidden JSON block；
- reopen 后扫描 Completed 并猜测 extraction；
- process-local candidate owner；
- worker 内临时 list/dict 作为唯一 candidate receipt；
- summary artifact 作为用户 evidence；
- model 自报 scope、authority、verification 或 candidate ID。

### 4.3 Ownership 与依赖方向

```text
primitives / event / ports
          ^
          |
runtime/compaction core
          ^
          |
memory/compaction extension implementation
          |
          v
runtime/projection_jobs + RuntimeSession writer
```

最终 import gate：

```text
runtime/compaction  -X-> memory.candidates
runtime/compaction  -X-> memory.governance
runtime/compaction  -X-> ontology.memory
runtime/compaction  -X-> projection_jobs concrete repository
llm/commit          -X-> memory.compaction concrete types

memory/compaction    -> ports.compaction_extensions
memory/compaction    -> ports.model_lifecycle
memory/compaction    -> primitives/event/projection contracts
host composition     -> runtime core + memory extension implementation
```

---

## 5. Transcript-derived human evidence manifest

### 5.1 不建立第二套 EventLog range reducer

D5 不要求 `_build_plan()` 分页扫描任意长物理 EventLog，也不删除当前
`through_sequence` / `keep_after_sequence`。Call A 继续使用现有异步 bounded source-read：

```text
maximum events: 16,384
maximum canonical payload bytes: 16 MiB
I/O owner: ContextInputIoService / blocking executor
_build_plan(): synchronous pure calculation over the owned bounded snapshot
```

超过该 physical hard cap 的 compaction attempt 继续按现有 typed source-bound failure处理。
D5 不声称 Call A 可以一次消费任意长 resident transcript，也不得把 PostgreSQL I/O
重新放进 `_build_plan()` 或 event loop。

Call B 的 source authority 从已经存在的 lossless transcript projection/checkpoint 派生。
它只选择 typed human leaves，不读取、hash 或 fold同一区间内所有 model segment、tool
projection、monitor、projection job、runtime observation 等非目标物理事件。

### 5.2 Selection window physical attribution

新增：

```python
class CompactionHumanEvidenceSelectionWindowAttributionFact(FrozenFactBase):
    schema_version: Literal[
        "compaction_human_evidence_selection_window_attribution.v1"
    ]

    previous_keep_after_sequence: int
    current_keep_after_sequence: int
    current_through_sequence: int
    predecessor_completed_event_id: str | None

    transcript_projection_base_semantic_fingerprint: Fingerprint
    transcript_semantic_source_fingerprint: Fingerprint
    transcript_stable_state_semantic_fingerprint: Fingerprint

    selection_contract_fingerprint: Fingerprint
    window_attribution_fingerprint: Fingerprint
```

V1 必须保持当前 compaction invariant：

```text
previous_keep_after_sequence >= 0
current_keep_after_sequence > previous_keep_after_sequence
current_keep_after_sequence <= current_through_sequence
```

该 fact 完全属于 occurrence/physical attribution。它不进入 manifest、evidence set、input或
governance semantic fingerprint；相同 human evidence仅因 compaction 调度边界/sequence不同
时，semantic identity必须保持不变。

### 5.3 Typed human leaf semantic 与 attribution

```python
class CompactionHumanEvidenceLeafSemanticFact(FrozenFactBase):
    schema_version: Literal[
        "compaction_human_evidence_leaf_semantic.v1"
    ]
    source_kind: Literal["direct_human_input"]
    message_provider_semantic_fingerprint: Fingerprint
    text_semantic_fingerprint: Fingerprint
    text_utf8_sha256: str
    text_utf8_bytes: int
    semantic_fingerprint: Fingerprint


class CompactionHumanEvidenceLeafAttributionFact(FrozenFactBase):
    schema_version: Literal[
        "compaction_human_evidence_leaf_attribution.v1"
    ]
    leaf_reference: TranscriptProjectionLeafEntryReferenceFact
    exact_run_start_event_reference: ContextEventReferenceFact
    message_id: str
    run_id: str
    turn_id: str
    reply_id: str
    source_sequence: int
    leaf_semantic_fingerprint: Fingerprint
    attribution_fingerprint: Fingerprint


class CompactionHumanEvidenceInlineSelectionProjectionFact(FrozenFactBase):
    schema_version: Literal[
        "compaction_human_evidence_inline_selection_projection.v1"
    ]
    projection_kind: Literal["inline_full"]
    source_leaf_semantic_fingerprint: Fingerprint
    sanitizer_contract_fingerprint: Fingerprint
    sanitized_full_text: str
    sanitized_full_text_sha256: Fingerprint
    sanitized_full_text_utf8_bytes: int
    hard_size_disposition: Literal["selectable"]
    selection_projection_fingerprint: Fingerprint


class CompactionHumanEvidenceArtifactSelectionProjectionFact(FrozenFactBase):
    schema_version: Literal[
        "compaction_human_evidence_artifact_selection_projection.v1"
    ]
    projection_kind: Literal["artifact_full"]
    source_leaf_semantic_fingerprint: Fingerprint
    sanitizer_contract_fingerprint: Fingerprint
    sanitized_full_text_reference: ContentAddressedArtifactReferenceFact
    sanitized_full_text_sha256: Fingerprint
    sanitized_full_text_utf8_bytes: int
    hard_size_disposition: Literal["permanently_oversize"]
    selection_projection_fingerprint: Fingerprint


CompactionHumanEvidenceSelectionProjectionFact = Annotated[
    CompactionHumanEvidenceInlineSelectionProjectionFact
    | CompactionHumanEvidenceArtifactSelectionProjectionFact,
    Field(discriminator="projection_kind"),
]
```

Eligibility 必须由 transcript leaf 本身证明：

```text
entry_kind == message
provider role == user
provider name == user
source event type == RUN_START
RunStart.current_user_message.source_kind == host_user_input
source sequence in (previous_keep_after, current_keep_after]
leaf text digest == RunStart.current_user_message.text digest
```

Monitor attachment是独立 `runtime_observation` leaf；runtime/subagent request 是
`runtime_request` leaf，因此不会因为共享一个 RunStart envelope而被误归为 human input。

Selection projection由manifest builder对exact leaf text执行当前closed sanitizer生成。它属于
selection/physical projection，不进入leaf或manifest source semantic identity：两个不同原文若
得到相同sanitized全文，可以共享sanitized semantic；source leaf attribution仍保持不同。

V1 inline hard bound固定为8 KiB。完整sanitized正文不超过该bound时必须使用`inline_full`；超过
时必须使用`artifact_full`并立即标记`permanently_oversize`。Artifact branch仍保存完整sanitized
正文的content-addressed reference/hash/bytes，但worker不得为了选择而hydrate它，因为该leaf在
V1必然被永久省略。禁止只保存head/tail、token估算或caller自报的“sanitized”标志。

### 5.4 Manifest semantic 与 physical attribution

```python
class CompactionHumanEvidenceManifestSemanticFact(FrozenFactBase):
    schema_version: Literal[
        "compaction_human_evidence_manifest_semantic.v1"
    ]
    eligible_leaf_count: int
    ordered_semantic_accumulator: Fingerprint
    transitive_leaf_coverage_fingerprint: Fingerprint
    selection_contract_fingerprint: Fingerprint
    manifest_semantic_fingerprint: Fingerprint


class CompactionHumanEvidenceManifestAttributionFact(FrozenFactBase):
    schema_version: Literal[
        "compaction_human_evidence_manifest_attribution.v1"
    ]
    manifest_semantic_fingerprint: Fingerprint
    runtime_session_id: str
    selection_window_attribution: (
        CompactionHumanEvidenceSelectionWindowAttributionFact
    )
    transcript_cursor_fingerprint: Fingerprint
    transcript_cursor_generation: int
    verified_through_sequence: int
    ledger_continuity_accumulator: Fingerprint
    domain_completeness_proof_fingerprint: Fingerprint
    ordered_leaf_attribution_accumulator: Fingerprint
    ordered_selection_projection_accumulator: Fingerprint
    selection_projection_contract_fingerprint: Fingerprint
    paged_manifest_root_reference: ContentAddressedArtifactReferenceFact
    attribution_fingerprint: Fingerprint


class CompactionHumanEvidenceManifestReferenceFact(FrozenFactBase):
    schema_version: Literal[
        "compaction_human_evidence_manifest_reference.v1"
    ]
    manifest_semantic_fingerprint: Fingerprint
    manifest_attribution_fingerprint: Fingerprint
    paged_manifest_root_reference: ContentAddressedArtifactReferenceFact
    reference_fingerprint: Fingerprint
```

Semantic manifest 不含 compaction ID、request ID、job ID、artifact locator、cursor generation、
event ID或ledger sequence。Attribution 保存 exact physical proof。Reference 只做二者 join。
Manifest semantic中的`selection_contract_fingerprint`只表示source eligibility/classification
contract，不覆盖sanitizer或selection projection；后两者由attribution中的独立projection
contract/accumulator证明，防止secret形态改变source semantic identity。

### 5.5 Paged artifact 与 completeness proof

Manifest page 是 content-addressed、最多 256 leaves / 1 MiB。Root 只保存：

```text
page count
eligible leaf count
ordered semantic accumulator
ordered attribution accumulator
ordered selection projection accumulator
first/last source sequence
transcript cursor/completeness proof references
```

Manifest builder 必须从一个 `VerifiedTranscriptProjectionCursorSnapshot` 与同 generation
的 reducer evidence snapshot中构造。它不得从 process-local `Msg`、provider payload或
compaction summary反推 human leaves。

每个manifest page按causal order保存leaf semantic、leaf attribution与selection projection三元组；
page bound同时满足最多256 leaves与最多1 MiB canonical payload。Worker按page从新到旧流式读取
selection projection，不先读取RunStart正文：

1. `artifact_full/permanently_oversize`直接记录永久省略并继续扫描更老leaf；
2. `inline_full`使用完整sanitized正文执行target-specific token estimate；
3. 当前leaf无法装入剩余token/byte budget时记录永久省略并继续扫描更老leaf，禁止提前停止；
4. 达到256条真正入选leaf、source耗尽，或剩余budget为零时停止；
5. 达到256条后，未扫描suffix的omission count/semantic/attribution accumulator必须由page/root
   range proof确定性派生，不能丢失审计；
6. 最后仅对真正入选的最多256个exact RunStart references点读，重跑同一sanitizer，并逐字节
   rebind sanitized text/hash/bytes与selection projection。

因此worker不得从run/session开头重放EventLog，也不得读取trigger sequence之后的事件；它对
source leaves的page scan可以是O(window)，但resident memory保持page-bounded，RunStart正文
exact-read保持最多256条。Target-specific token estimate不得进入manifest/source semantic。
所有 PostgreSQL/artifact hydrate通过现有 bounded blocking I/O owner执行；同步 exact-read
禁止直接运行在async event loop。

Completeness validator证明：

1. cursor verified through >= current keep-after；
2. selection window内每个 eligible `user/user` message leaf恰好出现一次；
3. 非 eligible leaf均由 closed classifier排除；
4. page roots按 transcript ordinal有序且无重叠；
5. semantic/attribution accumulators与root相等；
6. selection projection accumulator与root相等且每项绑定同一leaf/sanitizer contract；
7. each selected RunStart ref exact-rebinds同一 leaf text与sanitized projection semantic。

### 5.6 Manifest admission 与 failure isolation

Memory extension启用时，manifest semantic、attribution、page plan与stable artifact identity在
Call A dispatch前从当前 verified transcript cursor process-local冻结；artifact owner随后启动
session-owned、bounded preparation task执行`put-if-absent-or-confirm-identical`。该physical
artifact I/O与Call A并行，不得位于Call A provider dispatch的前置等待链。

新增process-local identity、snapshot与handle：

```python
class CompactionHumanEvidenceManifestPreparationIdentity(
    FrozenRuntimeStateBase
):
    preparation_id: str
    generation: int
    stable_manifest_reference_fingerprint: Fingerprint
    operation_deadline_monotonic: float
    identity_fingerprint: Fingerprint


class CompactionHumanEvidenceManifestPreparationFailureSnapshot(
    FrozenRuntimeStateBase
):
    failure_stage: Literal[
        "page_content_write",
        "page_write",
        "root_write",
        "artifact_confirmation",
        "physical_cancel",
    ]
    diagnostic: BoundedRuntimeFailureDiagnosticFact
    failure_fingerprint: Fingerprint


class CompactionHumanEvidenceManifestPreparationSnapshot(
    FrozenRuntimeStateBase
):
    preparation_identity_fingerprint: Fingerprint
    logical_state: Literal["preparing", "full", "failed", "abandoned"]
    physical_state: Literal["queued", "running", "exited"]
    completion_consumed: bool
    failure: (
        CompactionHumanEvidenceManifestPreparationFailureSnapshot | None
    )
    snapshot_fingerprint: Fingerprint


class CompactionHumanEvidenceManifestConsumedFull(FrozenRuntimeStateBase):
    outcome_kind: Literal["full"]
    manifest_reference: CompactionHumanEvidenceManifestReferenceFact
    pin_transfer_identity_fingerprint: Fingerprint
    outcome_fingerprint: Fingerprint


class CompactionHumanEvidenceManifestConsumedAbandoned(
    FrozenRuntimeStateBase
):
    outcome_kind: Literal["abandoned"]
    failure_stage: Literal[
        "manifest_not_ready_at_completion",
        "manifest_prepare",
        "manifest_abandoned",
    ]
    diagnostic: BoundedRuntimeFailureDiagnosticFact
    outcome_fingerprint: Fingerprint


CompactionHumanEvidenceManifestConsumptionOutcome = Annotated[
    CompactionHumanEvidenceManifestConsumedFull
    | CompactionHumanEvidenceManifestConsumedAbandoned,
    Field(discriminator="outcome_kind"),
]


class CompactionHumanEvidenceManifestPreparationHandle(Protocol):
    @property
    def identity(
        self,
    ) -> CompactionHumanEvidenceManifestPreparationIdentity: ...
    def snapshot_nowait(
        self,
    ) -> CompactionHumanEvidenceManifestPreparationSnapshot: ...
    def consume_full_or_abandon(
        self,
    ) -> CompactionHumanEvidenceManifestConsumptionOutcome: ...
    def request_physical_cancel(self) -> None: ...
    async def wait_physical_exit(self, *, deadline_monotonic: float) -> bool: ...
```

Logical与physical状态彼此独立：

```text
logical:
PREPARING
  -> FULL
  -> FAILED
  -> ABANDONED

physical:
QUEUED -> RUNNING -> EXITED
```

Call A terminal result准备完成时，compaction owner只允许原子调用
`consume_full_or_abandon()`一次，不允许先inspect再单独abandon：

```text
FULL
  -> 使用exact stable manifest reference准备Requested

PREPARING
  -> admission_failed(manifest_not_ready_at_completion)
  -> logical ABANDONED；安装physical cancel intent；不得等待task

FAILED
  -> admission_failed(manifest_prepare)

ABANDONED
  -> admission_failed(manifest_abandoned)
```

`PREPARING` task在收到abandon intent后即使晚到FULL，也只能释放pin并结束；它不得补写
Requested、不得改写已冻结Completed disposition，也不得触发post-scan recovery。由此 optional
manifest artifact I/O对Call A completion增加的等待时间严格为零。

`consume_full_or_abandon()`与task terminal transition使用同一owner lock/CAS，因此不会出现
“观察到PREPARING后task变FULL、随后又错误abandon”的ABA。Failure snapshot只能由closed
sanitizer factory生成；禁止保存raw exception、artifact path、DSN或credential。

Logical `ABANDONED`不代表physical `EXITED`。Preparation task是唯一physical owner，持有自己的
artifact/DB provider borrower leases直到task退出；caller cancellation只detach。RuntimeSession
维护所有preparation operations的bounded registry，session close必须：停止新preparation、对
未消费owner调用`consume_full_or_abandon()`、请求physical cancel、在共享close deadline内等待
全部`EXITED`，然后才允许释放artifact/DB dependencies。任一operation未退出时extension drain抛出
`TimeoutError`，outer Host close保持blocked并保留dependency owner，不得伪装成功。

若 cursor、document hydration、artifact confirmation或manifest preparation失败：

- extension intent记录 typed `admission_failed`；
- Call A 仍可执行并提交 Completed；
- 不写 Requested，不建 job；
- 不退化到 EventLog full scan；
- 不使用 summary作为 evidence fallback。

Prepared manifest artifact graph（sanitized content refs、pages、root）由extension private handle
持有完整pin set。只有Completed+Requested batch FULL后才把retention整体转交durable
Request/job；Call A失败、extension admission_failed或session close时必须设置abandon/cancel
intent；pin set由physical task finalizer在`EXITED`前释放，不能由logical caller抢先释放。
Orphan artifact可由现有content-addressed GC回收，绝不能据此补建Request。

### 5.7 Previous summary 与 omission边界

Call A 可以继续携带 previous summary。Human evidence manifest只覆盖：

```text
(previous_keep_after_sequence, current_keep_after_sequence]
```

previous/current summary、prior extraction result与candidate均不进入 manifest。

Manifest 本身完整列出该 window 的全部 eligible human leaves。后续 target-aware evidence
selection可能因 token/byte budget省略部分 leaves；该 omission 是 V1 明确的永久自动提取
取舍，不会跨过新 compaction boundary后再自动补提。被省略事实仍保留在 canonical
EventLog/transcript中，可由显式 memory tool、reflection或未来 shard contract处理。结果
receipt必须显示 `permanent_automatic_omission`，禁止把它描述为“稍后会自动处理”。

---

## 6. Generic post-completion extension port

### 6.1 低层 contract

新增 `src/pulsara_agent/ports/compaction_extensions.py`：

```python
class CompactionPostCompletionExtensionContractFact(FrozenFactBase):
    schema_version: Literal["compaction_post_completion_extension_contract.v1"]
    extension_id: str
    extension_version: str
    request_event_type: str
    request_event_schema_fingerprint: Fingerprint
    source_manifest_contract_fingerprint: Fingerprint
    admission_policy_fingerprint: Fingerprint
    contract_fingerprint: Fingerprint


class CompactionPostCompletionExtensionPrivateHandleIdentity(
    FrozenRuntimeStateBase
):
    extension_id: str
    handle_id: str
    generation: int
    manifest_preparation_identity_fingerprint: Fingerprint
    identity_fingerprint: Fingerprint


class CompactionPostCompletionExtensionPrivateHandle(Protocol):
    @property
    def identity(
        self,
    ) -> CompactionPostCompletionExtensionPrivateHandleIdentity: ...
    @property
    def active(self) -> bool: ...
    def confirm_request_batch_full(
        self,
        *,
        prepared_batch_fingerprint: Fingerprint,
        stored_request_reference: ContextEventReferenceFact,
    ) -> None: ...
    def retain_request_batch_none(
        self,
        *,
        prepared_batch_fingerprint: Fingerprint,
    ) -> None: ...
    def mark_request_batch_reconciliation_required(
        self,
        *,
        prepared_batch_fingerprint: Fingerprint,
    ) -> None: ...
    def abandon_before_write(self, *, reason: str) -> None: ...


class PreparedCompactionPostCompletionExtensionIntentIdentity(
    FrozenRuntimeStateBase
):
    extension_contract_fingerprint: Fingerprint
    completed_event_id: str
    request_event_id: str
    extension_link_id: str
    business_occurrence_fingerprint: Fingerprint
    private_handle_identity_fingerprint: Fingerprint
    intent_fingerprint: Fingerprint


@dataclass(frozen=True, slots=True)
class PreparedCompactionPostCompletionExtensionIntent:
    identity: PreparedCompactionPostCompletionExtensionIntentIdentity
    extension_contract: CompactionPostCompletionExtensionContractFact
    private_handle: CompactionPostCompletionExtensionPrivateHandle = field(
        compare=False,
        repr=False,
    )


class PreparedCompactionPostCompletionExtensionAdmissionFailure(
    FrozenRuntimeStateBase
):
    extension_contract_fingerprint: Fingerprint
    failure_stage: Literal[
        "intent_factory",
        "target_resolution",
        "manifest_prepare",
        "manifest_not_ready_at_completion",
        "manifest_abandoned",
        "request_factory",
    ]
    diagnostic: BoundedRuntimeFailureDiagnosticFact
    preparation_fingerprint: Fingerprint


class CompactionPostCompletionExtensionLinkFact(FrozenFactBase):
    schema_version: Literal["compaction_post_completion_extension_link.v1"]
    compaction_id: str
    completed_event_id: str
    request_event_id: str
    extension_contract_fingerprint: Fingerprint
    extension_link_id: str


class PreparedCompactionPostCompletionExtensionBatchIdentity(
    FrozenRuntimeStateBase
):
    extension_contract_fingerprint: Fingerprint
    extension_link_id: str
    request_event_id: str
    request_event_type: str
    request_event_schema_fingerprint: Fingerprint
    request_event_payload_fingerprint: Fingerprint
    prepared_batch_fingerprint: Fingerprint


@dataclass(frozen=True, slots=True)
class PreparedCompactionPostCompletionExtensionBatch:
    identity: PreparedCompactionPostCompletionExtensionBatchIdentity
    extension_link: CompactionPostCompletionExtensionLinkFact
    request_event_candidate: FrozenEventWriteCandidate


class CompactionPostCompletionExtensionPort(Protocol):
    def prepare_intent(
        self,
        *,
        runtime_session_id: str,
        event_context: EventContext,
        compaction_id: str,
        completed_event_id: str,
        trigger: Literal["manual", "auto"],
        phase: str | None,
        previous_keep_after_sequence: int,
        current_keep_after_sequence: int,
        current_through_sequence: int,
        predecessor_completed_event_id: str | None,
        transcript_authority_snapshot: object,
        event_lookup: Callable[[str], AgentEvent | None],
    ) -> (
        PreparedCompactionPostCompletionExtensionIntent
        | PreparedCompactionPostCompletionExtensionAdmissionFailure
        | None
    ): ...

    def prepare_completion_disposition(
        self,
        *,
        preparation: (
            PreparedCompactionPostCompletionExtensionIntent
            | PreparedCompactionPostCompletionExtensionAdmissionFailure
        ),
        completed_event: ContextCompactionCompletedEvent,
    ) -> (
        PreparedCompactionPostCompletionExtensionBatch
        | CompactionPostCompletionExtensionAdmissionFailedFact
    ): ...
```

`prepare_intent()`由extension在内部构造并持有
`CompactionHumanEvidenceManifestPreparationHandle`；compaction core只交付已经冻结的transcript
authority snapshot、window边界与exact event lookup，不得自行构造memory-owned manifest handle。
这样generic port不会把live handle暴露给caller，也不会让caller替换sanitizer/manifest owner。

`PreparedCompactionPostCompletionExtensionIntent` 与 batch 是普通
`@dataclass(frozen=True, slots=True)`，不是Pydantic model；`private_handle`使用
`field(compare=False, repr=False)`，且`__post_init__`必须验证live handle object、identity、
generation与intent identity exact join。稳定fingerprint只覆盖
`PreparedCompactionPostCompletionExtensionIntentIdentity`，绝不遍历live object。

Request factory必须立即调用现有EventLog schema registry生成`FrozenEventWriteCandidate`。
Prepared batch保存canonical bytes/payload fingerprint，不保存裸`AgentEvent`；write admission只能
从candidate按exact schema binding thaw，并逐字节确认。由此event metadata或nested payload在
factory返回后无法漂移。

Compaction write owner必须把typed confirmation反馈给同一private handle：FULL才转移manifest
pin set并revoke handle；NONE保留同一candidate/pins供exact retry；UNKNOWN/PARTIAL保留owner并
进入reconciliation。Admission failure或尚未进入write时的close才允许
`abandon_before_write()`。这些方法都必须exact验证prepared batch fingerprint，禁止caller仅凭
request ID结算错误handle。

禁止通过全局或局部开启Pydantic `arbitrary_types_allowed`容纳capability。Live handle不得被core
序列化、复制、pickle、持久化或pattern-match；handle revoke后所有操作fail closed。具体memory
payload由extension implementation按handle identity保存，不能塞进mutable `dict`/`object`再交给
core。

### 6.2 Core 的合法认知范围

Compaction core 只允许知道：

- extension ID/version/fingerprint；
- 是否配置；
- request event ID；
- prepared batch fingerprint；
- typed admission failure diagnostic。

Core 禁止知道：

- candidate kind；
- memory scope/domain/ontology；
- extraction prompt；
- parser；
- candidate pool；
- governance；
- projection outbox row。

### 6.3 Completion disposition

`ContextCompactionCompletedEvent` 新增：

```python
class CompactionPostCompletionExtensionRequestedFact(FrozenFactBase):
    schema_version: Literal[
        "compaction_post_completion_extension_requested.v1"
    ]
    disposition_kind: Literal["requested"]
    extension_link: CompactionPostCompletionExtensionLinkFact
    disposition_fingerprint: Fingerprint


class CompactionPostCompletionExtensionAdmissionFailedFact(FrozenFactBase):
    schema_version: Literal[
        "compaction_post_completion_extension_admission_failed.v1"
    ]
    disposition_kind: Literal["admission_failed"]
    extension_contract_fingerprint: Fingerprint
    failure_stage: Literal[
        "intent_factory",
        "target_resolution",
        "manifest_prepare",
        "manifest_not_ready_at_completion",
        "manifest_abandoned",
        "request_factory",
    ]
    diagnostic: BoundedRuntimeFailureDiagnosticFact
    disposition_fingerprint: Fingerprint


CompactionPostCompletionExtensionDispositionFact = Annotated[
    CompactionPostCompletionExtensionRequestedFact
    | CompactionPostCompletionExtensionAdmissionFailedFact,
    Field(discriminator="disposition_kind"),
]

ContextCompactionCompletedEvent.post_completion_extension_dispositions:
    tuple[CompactionPostCompletionExtensionDispositionFact, ...]
```

Completed 保存 ordered tuple，最多 4 个 extension。只有composition root明确没有配置适用的
extension时tuple才为空；配置后的target解析、intent、manifest或request preparation失败必须形成
typed `admission_failed`，不得再退化成空tuple。

### 6.4 原子 batch validator

当 disposition 为 `requested` 时，RuntimeSession precommit 必须证明：

```text
batch contains exactly one matching request event
Completed precedes Request
Request is immediately after Completed among compaction-extension events
same runtime_session/run/turn/reply
request source_compaction_id == completed.compaction_id
Completed disposition.extension_link == Request.extension_link
extension_link.completed_event_id == completed.id
extension_link.request_event_id == request.id
extension_link.compaction_id == completed.compaction_id
extension_link_id recomputes from stable IDs and contract fingerprint
```

禁止：

- Requested 独立提交；
- Completed requested disposition 没有 companion event；
- 一个 request 指向多个 Completed；
- post-commit callback 再生成 request；
- reopen 扫描 Completed 补 request。

`extension_link` 只覆盖预先可知的 stable event IDs 与 extension contract。它禁止覆盖
Completed/Request payload fingerprint，因此 event payload之间不存在递归 hash。

### 6.5 Extension failure 不得使 summary 失效

如果 extension intent 或 request factory 失败：

1. 使用 closed sanitizer 生成 bounded diagnostic；
2. Completed 写 `admission_failed` disposition；
3. 不写 Requested；
4. summary 仍可 FULL；
5. 不调用 Call B；
6. Inspector 显示 extension admission failure。

该分支是 optional derived-work failure，不得改写 `ContextCompactionCompletedEvent` 为 Failed。
`admission_failed` 只能在Completed/Requested terminal batch的EventLog write admission之前冻结。
Call A自身既有ModelCall lifecycle不受此规则影响。Requested batch一旦进入
WRITING，`NONE` 必须重试同一 Completed/Request candidates，`UNKNOWN/PARTIAL` 必须
reconcile；禁止沿用同一 completed event ID改写成 admission_failed payload。

---

## 7. Memory extraction request authority

### 7.1 Extraction contract

新增 memory-owned durable contract：

```python
class CompactionMemoryExtractionContractFact(FrozenFactBase):
    schema_version: Literal["compaction_memory_extraction_contract.v1"]

    extractor_id: Literal["pulsara.compaction-memory-extraction"]
    extractor_version: Literal["1"]

    accepted_source_kind: Literal["direct_human_input_only"]
    output_candidate_kinds: tuple[Literal["Preference"], ...]

    input_document_schema_fingerprint: Fingerprint
    output_document_schema_fingerprint: Fingerprint
    evidence_selection_contract_fingerprint: Fingerprint
    sanitizer_contract_fingerprint: Fingerprint
    parser_contract_fingerprint: Fingerprint
    normalization_contract_fingerprint: Fingerprint
    candidate_identity_contract_fingerprint: Fingerprint

    maximum_evidence_nodes: int
    maximum_input_utf8_bytes: int
    maximum_output_utf8_bytes: int
    maximum_candidates: int
    maximum_evidence_refs_per_candidate: int
    maximum_statement_utf8_bytes: int

    contract_fingerprint: Fingerprint
```

所有 bounds 来自一个 closed central factory。caller 不得自报。

### 7.2 Policy 与 model target

```python
class CompactionMemoryExtractionPolicyFact(FrozenFactBase):
    schema_version: Literal["compaction_memory_extraction_policy.v1"]

    enabled: bool
    allowed_triggers: tuple[Literal["manual", "auto"], ...]
    allowed_phases: tuple[
        Literal["pre_run", "mid_turn", "manual", "window_maintenance"], ...
    ]
    model_target: ResolvedModelTargetFact
    maximum_attempts: int
    provider_timeout_seconds: int
    lease_duration_seconds: int
    retry_policy_fingerprint: Fingerprint
    input_budget_policy_fingerprint: Fingerprint
    background_work_budget_policy_fingerprint: Fingerprint
    policy_fingerprint: Fingerprint
```

Call B target 独立于 Call A target。默认可以使用低成本/低延迟模型，但必须通过 `ModelCallPurpose.COMPACTION_MEMORY_EXTRACTION` 单独解析并冻结，不能继承 summarizer target 的 process-local object。

Requested event 中的 extraction policy 是该 job 唯一执行真源，不只是审计副本。V1 closed
factory冻结：`maximum_attempts=3`、provider timeout、lease duration、retry contract、input-budget
selection contract及background account policy。D3 seed candidate必须从exact stored Request派生
`DurableProjectionDeliveryPolicyFact`；active seed contract只允许与该closed factory逐字段相等的
policy。claim耗尽判断、retry delay、physical attempt deadline都消费job内这份派生policy，禁止
回退到通用projection默认的12次尝试或进程启动时常量。

ModelCallStart transaction companion必须在同一PostgreSQL transaction中exact-read Request，
验证Request source/event/schema/payload、job delivery policy、resolved model target与当前
background account policy。任一binding不一致都拒绝Start；延迟执行、restart和binary重载不得
重新解释Request。

`prepare_intent()`返回`None`只表示policy明确disabled、trigger/phase不适用或composition root根本
没有配置该extension。extension已经配置且适用时，target解析、manifest preparation或factory
失败必须返回typed admission failure；不得用`None`隐藏配置故障，也不得影响Call A。

### 7.3 Requested event

新增：

```python
class ContextCompactionMemoryExtractionRequestedEvent(EventBase):
    type: Literal[
        EventType.CONTEXT_COMPACTION_MEMORY_EXTRACTION_REQUESTED
    ]

    extension_link: CompactionPostCompletionExtensionLinkFact
    human_evidence_manifest_reference: (
        CompactionHumanEvidenceManifestReferenceFact
    )
    memory_domain_id: str
    resolved_scope: str
    extraction_contract: CompactionMemoryExtractionContractFact
    extraction_policy: CompactionMemoryExtractionPolicyFact

    business_occurrence_fingerprint: Fingerprint
    event_semantic_fingerprint: Fingerprint
```

Request 不复制 manifest正文，只保存 exact content-addressed reference。Seeder/worker必须
exact-read source Completed event、验证同一个 `extension_link`，再 hydrate并验证 manifest
semantic/attribution/root。Request 不包含 `job_id`、Completed payload fingerprint、summary
text、candidate、raw credentials 或 process owner。

### 7.4 Stable IDs

```text
request_event_id =
  H("context-compaction-memory-extraction-request-id:v1",
    runtime_session_id,
    compaction_id,
    completed_event_id,
    extension_contract_fingerprint)

extension_link_id =
  H("compaction-post-completion-extension-link:v1",
    compaction_id,
    completed_event_id,
    request_event_id,
    extension_contract_fingerprint)

target_key =
  H("compaction-memory-extraction-target:v1",
    runtime_session_id,
    request_event_id)

job_id =
  durable_projection_job_id(
    projection_kind=COMPACTION_MEMORY_EXTRACTION,
    source_event_reference=stored_request_reference,
    target_key=target_key,
    handler_contract_fingerprint=handler_contract_fingerprint)
```

`completed_event_id` 在 compaction terminal candidate freeze 时预分配；
`request_event_id` 与 `extension_link_id` 随后一次性确定。Completed 与 Request 都只嵌相同
`extension_link`，禁止复制或覆盖对方 payload fingerprint。`job_id` 只能在 Request FULL 并被
D3 seeder exact-read 后生成，复用现有 `build_job_candidate()` 的 source-reference 模式。
这些 event ID 是 occurrence identity，不得由其最终 payload fingerprint反推；任何把
Completed payload作为 completed ID输入的通用 factory都不适用于该 batch。

Target update policy 为 `SINGLE_ASSIGNMENT`：

- 无 head：允许 exact first result；
- 已有 exact same request/result：confirm；
- 同 request 不同 result：authority conflict；
- 更高 sequence 不会覆盖旧 result。

### 7.5 Seeder source horizon

D3 job 的 source event 是 Requested event：

```text
job.source_event_reference.sequence == requested.sequence
job.source_horizon.through_sequence == requested.sequence
```

Seeder page high-water 只属于 seed checkpoint，不进入 job semantic。

Seeder 必须 exact-read：

1. Requested；
2. immediate preceding Completed；
3. 两者相同的 extension link 与 Completed disposition；
4. human evidence manifest reference/root/pages；
5. transcript completeness proof；
6. event/artifact schema bindings。

任一 join 不成立时写 per-authority seed failure，不得创建 job。

---

## 8. D3 job 扩展

D3 只拥有 durable admission、target/lease/retry/dead-letter authority。它不实现第二套
Host-aware LLM runtime，不直接构造 provider input，也不持有 RuntimeSession/LLM adapter。
Memory-owned semantic service由session-bound
`CompactionMemoryExtractionSessionDriver`实现，并通过process-owned driver registry借用现有
one-shot model lifecycle与唯一RuntimeSession writer；D3 service只通过driver port调度，不持有该
concrete adapter。

### 8.1 新 projection kind 与 execution class

```python
class DurableProjectionKind(StrEnum):
    RUN_TIMELINE = "run_timeline.v1"
    TOOL_RESULT_EXECUTION_EVIDENCE = "tool_result_execution_evidence.v1"
    COMPACTION_MEMORY_EXTRACTION = "compaction_memory_extraction.v1"


class DurableProjectionExecutionClass(StrEnum):
    DATABASE_PROJECTION = "database_projection"
    SESSION_MODEL_PROJECTION = "session_model_projection"
```

`DurableProjectionHandlerContractFact` 新增 `execution_class`。

现有 timeline/evidence 为 `DATABASE_PROJECTION`；新 kind 为 `SESSION_MODEL_PROJECTION`。

`build_projection_executable_registry()` 只要求 database kinds 有 synchronous executable。新的 central completeness gate 必须验证：

```text
trigger registry kinds
  == handler contract kinds
  == activation kinds
  == database executable kinds U session driver kinds
```

禁止为满足旧 registry shape 而伪造一个同步 executable，再从中抓取 `RuntimeSession`。

### 8.2 Job absence of live driver

无 live session driver 时：

- job 保持 `PENDING`；
- 不 claim；
- 不增加 attempt count；
- 不进入 retry_wait；
- 不写 failure diagnostic；
- 不消耗 provider budget。

`runtime_binding_unavailable` 是 selection condition，不是 job failure。

### 8.3 Driver registry

新增 process-owned registry：

```python
class CompactionMemoryExtractionSessionDriverHandle(Protocol):
    @property
    def runtime_session_id(self) -> str: ...
    @property
    def driver_generation(self) -> int: ...
    @property
    def binding_fingerprint(self) -> Fingerprint: ...

    async def acquire_model_safe_point(
        self, *, deadline_monotonic: float
    ) -> BackgroundModelCallAdmissionLease | None: ...

    async def execute_leased_job(
        self,
        job: LeasedDurableProjectionJob,
        *,
        deadline_monotonic: float,
    ) -> None: ...

    async def close(self, *, deadline_monotonic: float) -> None: ...


class CompactionMemoryExtractionDriverRegistry(Protocol):
    def register(
        self,
        driver: CompactionMemoryExtractionSessionDriverHandle,
    ) -> DriverRegistrationLease: ...

    def available_runtime_session_ids(
        self, *, now_monotonic: float
    ) -> tuple[str, ...]: ...
    def next_eligible_at_monotonic(
        self, runtime_session_id: str
    ) -> float | None: ...
    def mark_dirty(self, runtime_session_id: str) -> None: ...
    def borrow(self, runtime_session_id: str) -> DriverBorrow | None: ...
```

每个 registration/borrow 都是 borrower-scoped、generation-aware：

- unregister 后旧 borrow 禁止新操作；
- in-flight physical task 保留自己的 dependency leases；
- Host close 必须 drain 或明确进入 close-blocked；
- driver 不得被 job row、event 或 Inspector 序列化。

#### 8.3.1 Process-local carrier 的 closed contract

以下不是只有名字的 placeholder：

```python
class DriverRegistrationLeaseIdentity(FrozenRuntimeStateBase):
    registry_id: str
    runtime_session_id: str
    driver_generation: int
    binding_fingerprint: Fingerprint
    registration_id: str
    identity_fingerprint: Fingerprint


class DriverRegistrationLease(Protocol):
    @property
    def identity(self) -> DriverRegistrationLeaseIdentity: ...
    @property
    def active(self) -> bool: ...
    def revoke(self) -> None: ...


class DriverBorrowIdentity(FrozenRuntimeStateBase):
    registration_identity_fingerprint: Fingerprint
    borrow_id: str
    borrow_generation: int
    identity_fingerprint: Fingerprint


class DriverBorrow(Protocol):
    @property
    def identity(self) -> DriverBorrowIdentity: ...
    @property
    def active(self) -> bool: ...
    @property
    def driver(self) -> CompactionMemoryExtractionSessionDriverHandle: ...
    def release(self) -> None: ...


class BackgroundModelCallAdmissionLeaseIdentity(FrozenRuntimeStateBase):
    lease_id: str
    lease_generation: int
    runtime_session_id: str
    operation_id: str
    admission_proof_fingerprint: Fingerprint
    identity_fingerprint: Fingerprint


class BackgroundModelCallAdmissionLease(Protocol):
    @property
    def identity(self) -> BackgroundModelCallAdmissionLeaseIdentity: ...
    @property
    def state(self) -> Literal[
        "issued", "in_flight", "consumed", "released", "reconciliation_required"
    ]: ...
    def release(self) -> None: ...
```

Registry/Host coordinator是唯一 issuer。Handle object identity、owner generation与identity fact
必须同时匹配；只拿到复制的 identity fact不能执行操作。Registration revoke先禁止新
borrow；已签发 borrow持有自己的依赖 lease直到 operation退出。所有 caller cancellation
只 detach waiter，不可隐式 revoke共享 owner。

### 8.4 Background model-call admission

新增 narrow port：

```python
class BackgroundModelCallAdmissionPort(Protocol):
    async def acquire(
        self,
        *,
        runtime_session_id: str,
        operation_kind: Literal["compaction_memory_extraction"],
        operation_id: str,
        deadline_monotonic: float,
    ) -> BackgroundModelCallAdmissionLease | None: ...
```

Admission lease 的 durable/process-local proof 冻结：

```python
class BackgroundModelCallAdmissionProof(FrozenRuntimeStateBase):
    lease_id: str
    lease_generation: int
    runtime_session_id: str
    operation_id: str
    host_state_generation: int
    active_run_frontier_fingerprint: Fingerprint
    permission_policy_revision: int
    permission_policy_fingerprint: Fingerprint
    stop_intent_revision: int
    close_intent_revision: int
    expected_provider_input_generation_revision: int
    expires_at_monotonic: float
    proof_fingerprint: Fingerprint
```

#### 8.4.1 Purpose-neutral model lifecycle transaction companion

新增低层 contract（最终 owner：`ports/model_lifecycle.py`，不能位于 memory package）：

```python
class ModelLifecycleTransactionCompanionIdentityFact(FrozenRuntimeStateBase):
    companion_kind: Literal["durable_derived_model_job"]
    phase: Literal["start", "terminal"]
    purpose: ModelCallPurpose
    resolved_model_call_id: str
    stable_primary_event_id: str
    external_owner_reference_fingerprint: Fingerprint
    stable_candidate_fingerprint: Fingerprint
    companion_fingerprint: Fingerprint


class ModelLifecycleTransactionCompanion(
    EventLogTransactionCompanion,
    Protocol,
):
    @property
    def identity(
        self,
    ) -> ModelLifecycleTransactionCompanionIdentityFact: ...
```

`llm/commit.py` 的 start/terminal commit API、对应 commit guard，以及 RuntimeSession
`reserve_physical_operation_from_thread()` / `settle_physical_operation_from_thread()` 必须新增
该可空 companion seam。LLM commit port只验证：

```text
phase matches commit method
purpose / resolved_model_call_id matches ModelCall event
stable_primary_event_id matches Start or End
companion fingerprint matches frozen guard
companion is applied by EventLog on the same transaction cursor
```

它禁止 import、downcast或解释 memory/job/account concrete DTO。Companion implementation由
D3/background-derived-work service提供，并通过现有 `EventLogTransactionCompanion` 在同一
PostgreSQL transaction执行 closed SQL mutation；不得另开 connection。

Start transaction：

```text
BEGIN
  validate exact D3 job lease + target execution lease + lease_generation
  validate current dispatch_attempt_count and intended next ordinal
  validate exact background account revision + ModelCallReservationQuoteFact
  advance dispatch_attempt_count to intended ordinal
  insert open background budget reservation and update account open balances
  append provider-input generation/append events, physical reservation,
         and ModelCallStart
COMMIT
```

Terminal transaction：

```text
BEGIN
  validate exact open background reservation + quote + ModelCallStart
  derive settlement from ModelCallEnd/terminal projection/usage
  move quote from open balances to settled charges
  append terminal projection, ModelCallEnd, physical settlement,
         and any existing rollout settlement events
COMMIT
```

Repository hard cut仅作用于新`SESSION_MODEL_PROJECTION` execution class：它的D3 `claim()`只能推进
`lease_generation/state_revision`，禁止增加任何attempt计数。Migration v9为该execution class
新增独立`dispatch_attempt_count`；只允许start companion在ModelCallStart transaction内加一，
maximum attempts也只比较该字段。现有非模型projection jobs继续使用原`attempt_count`语义，
不得因D5被重命名或改变retry行为；`SESSION_MODEL_PROJECTION`的legacy `attempt_count`必须固定为0/
not-applicable并由validator拒绝漂移。Safe-point stale、admission deny或Start `NONE`都不消耗
ordinal；Start `FULL`即使provider尚未真正发出也已消耗一个ordinal，并由terminal
`not_started_zero`关闭。

ModelCallStart admission 必须在同一个 Host/session coordinator lock 下执行 final CAS：

- exact lease identity/generation仍 active；
- exact D3 job/target lease仍与 start companion相同；
- Host state generation未变化；
- 没有新 human/pending-interaction ingress；
- permission、stop、close revision未变化；
- provider-input generation/revision仍是 expected value；
- 没有新 active run/model call。

CAS stale 时必须释放 safe-point lease并将 projection job lease安全归还 `PENDING`；不得
dispatch provider、不得消耗 dispatch attempt ordinal。一次 ModelCallStart FULL 后 safe-point
lease才进入 `CONSUMED`。caller cancellation只detach waiter，不撤销已进入 IN_FLIGHT 的
physical owner。

Admission lease状态机：

```text
ISSUED
  -> IN_FLIGHT
       -> CONSUMED                 # ModelCallStart FULL
       -> ISSUED(next generation)  # writer NONE, same stable Start candidate
       -> RECONCILIATION_REQUIRED  # UNKNOWN/PARTIAL
  -> RELEASED                     # pre-dispatch stale/close
```

只有 coordinator可推进状态；`release()` 对 `IN_FLIGHT` 只登记 intent，不能在物理 writer
owner尚未退出时抢先释放 dependency。

Driver registry必须额外暴露 O(1) dirty-session wake 与
`next_eligible_at_monotonic(runtime_session_id)`。Seeder/service只 claim 当前可借用且已到
eligible time 的 session。若 selection 后发生 race、safe-point CAS miss：

```python
class CompactionMemoryExtractionJobDeferralFact(FrozenFactBase):
    schema_version: Literal[
        "compaction_memory_extraction_job_deferral.v1"
    ]
    job_id: str
    reason: Literal["driver_busy", "safe_point_stale"]
    deferral_ordinal: int
    not_before_utc: datetime
    deferral_policy_fingerprint: Fingerprint
    deferral_fingerprint: Fingerprint
```

```text
job lease -> PENDING
dispatch attempt ordinal unchanged
provider budget unchanged
durable not_before = now + deterministic bounded backoff (1s..5s)
```

同一 busy session不得在 worker polling loop中反复 claim/release。满页 dirty queue立即 yield
后继续；只有完成一轮且无 dirty authority时才进入普通 idle wait。

准入优先级必须低于：

1. human input；
2. pending interaction resume；
3. active Host run/model/tool；
4. compaction Call A；
5. terminal monitor delivery；
6. session close/stop。

V1 只在以下 safe point claim：

```text
HostSession IDLE
and no active/preparing run
and no WAITING_USER interaction
and no publication reconciliation latch
and no close/stop intent
and no active model call for this RuntimeSession
```

Call B 不创建 Host RunStart，不进入 Host ingress，不向用户伪装成 autonomous task。

若 ModelCallStart CAS 已 FULL 后 human ingress 到达，human ingress仍立即获得前台 admission，
不等待后台 provider timeout。Host向 background owner提交 typed
`preempted_by_foreground` cancel intent；后台 call使用独立 one-shot lane完成 stop/End/usage
settlement。已经 dispatch 的 call计入成本，已有合法 completed output可继续 settlement；被
取消的 attempt按 retry policy进入 `RETRY_WAIT`。禁止把 background call登记成 Host active
run，也禁止用同一 session mutex阻塞 human RunStart。

### 8.5 Concurrency 与资源

默认 closed policy：

```text
global active extraction calls: 2
per RuntimeSession: 1
max attempts per job: 3
provider timeout: <= 120s
job lease: >= provider timeout + 60s settlement reserve
lease heartbeat interval: <= lease / 3
```

executor 仍为 process-owned。D5 不新建 per-HostCore thread pool。

Provider operation 是 async owner，不占用 D3 PostgreSQL maintenance worker。Claim、source
hydrate与普通 D3 state transition使用 `PROJECTION_MAINTENANCE` lane；result event settlement
必须进入唯一 RuntimeSession critical EventLog writer，见 12.4。

### 8.6 Durable background-derived-work budget

每个 RuntimeSession 使用 versioned、durable budget account。单次调用金额与token上界必须
复用现有 `ModelCallReservationQuoteFact`；禁止另造第二套 token/milliunit估算公式。

```python
class BackgroundDerivedWorkBudgetPolicyFact(FrozenFactBase):
    schema_version: Literal["background_derived_work_budget_policy.v1"]
    policy_id: str
    maximum_dispatched_calls_per_session: int
    maximum_physical_input_tokens_per_session: int
    maximum_output_tokens_per_session: int
    maximum_milliunits_per_session: int
    pricing_contract_fingerprint: Fingerprint
    policy_fingerprint: Fingerprint


class BackgroundDerivedWorkBudgetAccountFact(FrozenFactBase):
    schema_version: Literal["background_derived_work_budget_account.v1"]
    runtime_session_id: str
    policy_fingerprint: Fingerprint
    account_revision: int
    account_status: Literal["active", "reconciliation_required"]
    dispatched_call_count: int
    settled_call_count: int
    open_reservation_count: int
    open_reserved_input_tokens: int
    open_reserved_output_tokens: int
    open_reserved_milliunits: int
    settled_charged_input_tokens: int
    settled_charged_output_tokens: int
    settled_charged_milliunits: int
    account_fingerprint: Fingerprint


class BackgroundDerivedWorkBudgetReservationFact(FrozenFactBase):
    schema_version: Literal["background_derived_work_budget_reservation.v1"]
    reservation_id: str
    runtime_session_id: str
    extraction_job_id: str
    operation_id: str
    dispatch_attempt_ordinal: int
    model_call_reservation_quote: ModelCallReservationQuoteFact
    source_account_revision: int
    reservation_fingerprint: Fingerprint


class BackgroundDerivedWorkBudgetSettlementFact(FrozenFactBase):
    schema_version: Literal["background_derived_work_budget_settlement.v1"]
    reservation_fingerprint: Fingerprint
    model_call_end_event_id: str
    accounting_basis: Literal[
        "provider_reported_usage",
        "not_started_zero",
        "reserved_missing_usage",
        "cancelled_reserved",
    ]
    charged_input_tokens: int
    charged_output_tokens: int
    charged_milliunits: int
    usage_charge_fingerprint: Fingerprint
    source_account_revision: int
    resulting_account_revision: int
    settlement_fingerprint: Fingerprint


class BackgroundDerivedWorkBudgetAccountReferenceFact(FrozenFactBase):
    schema_version: Literal["background_derived_work_budget_account_ref.v1"]
    runtime_session_id: str
    account_revision: int
    account_fingerprint: Fingerprint
    reference_fingerprint: Fingerprint


class BackgroundDerivedWorkBudgetAdmissionFailureFact(FrozenFactBase):
    schema_version: Literal[
        "background_derived_work_budget_admission_failure.v1"
    ]
    failure_kind: Literal[
        "call_cap_exhausted",
        "input_token_cap_exhausted",
        "output_token_cap_exhausted",
        "milliunit_cap_exhausted",
        "account_reconciliation_required",
    ]
    source_account_reference: BackgroundDerivedWorkBudgetAccountReferenceFact
    rejected_quote_fact_fingerprint: Fingerprint
    failure_fingerprint: Fingerprint
```

Account factory必须逐次验证：

```text
dispatched_call_count == settled_call_count + open_reservation_count

settled_charged_input_tokens + open_reserved_input_tokens
  <= policy.maximum_physical_input_tokens_per_session

settled_charged_output_tokens + open_reserved_output_tokens
  <= policy.maximum_output_tokens_per_session

settled_charged_milliunits + open_reserved_milliunits
  <= policy.maximum_milliunits_per_session

dispatched_call_count <= policy.maximum_dispatched_calls_per_session
```

`open_reserved_*` 只表示当前未settle reservations，不是累计值；`settled_charged_*` 只表示
已关闭reservation的累计收费。Reservation中的三项上界唯一来自 nested quote：

```text
input  = quote.physical_input_token_upper_bound
output = quote.output_token_upper_bound
cost   = quote.reserved_milliunits
```

Policy factory必须给所有生产 target解析出有限、非零上限。每次 provider dispatch都必须：

1. 在 ModelCallStart 的 RuntimeSession writer transaction companion中 CAS reserve；
2. reserve失败时不写 ModelCallStart、不调用 provider；
3. Start FULL后 `dispatched_call_count/open_reservation_count` 同时加一；
4. ModelCallEnd terminal companion使用现有 model-call accounting factory结算；
5. retry的每次实际 dispatch分别计费；
6. crash/reopen exact-confirm open reservation与 ModelCall lifecycle后再 settle。

Terminal matrix：

| 物理结果 | input/output/milliunit charge | reservation |
|---|---|---|
| Start FULL，provider未dispatch | `not_started_zero`，全部 0 | close |
| reported usage且input/output及计算后milliunit均不超过quote | 按现有cached/non-cached/output权重计算 | close |
| usage missing | quote input/output/milliunit上界 | close |
| cancelled/preempted且无完整usage | quote input/output/milliunit上界 | close |
| reported input/output或计算后milliunit超过quote | 不伪造合法charge；保留open reserve，account转`reconciliation_required` | retain |

Reported-over-quote仍必须与 terminal projection/ModelCallEnd同事务记录 typed account
reconciliation；它只禁止新的 background-derived calls，不阻止foreground Host run。Start或End
transaction `UNKNOWN/PARTIAL` 则属于 RuntimeSession ledger reconciliation，因为 agent event
是否提交也未知。

Ownership唯一性：ModelCallStart companion是唯一reservation writer，ModelCall terminal
companion是唯一settlement writer。Extraction result companion只exact-read二者并验证join；
`NO_ELIGIBLE_EVIDENCE`、`INPUT_BUDGET_UNSATISFIABLE`与
`BACKGROUND_BUDGET_EXHAUSTED`三个no-call branch均不得修改background account。任何result
companion对budget relation的`INSERT/UPDATE/DELETE`都属于architecture gate failure。

达到 session call/token/milliunit上限后，job通过唯一 result settlement写
`BACKGROUND_BUDGET_EXHAUSTED` terminal outcome；不再 retry，也不调用 provider。该预算与
D3 `maximum_attempts` 正交，防止长 session通过反复 compaction绕过总成本上限。

---

## 9. Exact evidence input

### 9.1 V1 source eligibility matrix

| Canonical source | V1 eligibility | 原因 |
|---|---:|---|
| Host human `RunStart.current_user_message` | eligible | direct user evidence |
| human input merged with monitor notification | 只取 human branch | notification 不是 user preference |
| runtime request | excluded | runtime-authored task |
| subagent task/current request | excluded | parent/runtime authored |
| assistant text | excluded | model output |
| tool call arguments | excluded | model-generated action |
| tool result | excluded | observation，不是 user preference |
| runtime observation | excluded | clock/memory/skill/lifecycle |
| recalled memory | excluded | 防止 memory 自复制 |
| recent working context | excluded | derived projection |
| previous compaction summary | excluded | derived continuity artifact |
| current compaction summary | excluded | derived continuity artifact |
| governance/reflection/summarizer request | excluded | subsystem request |

V1 不允许根据 assistant 的“用户似乎喜欢 X”创建 Preference。

### 9.2 Evidence semantic、attribution 与 node

```python
class CompactionMemoryEvidenceRedactionAuditFact(FrozenFactBase):
    schema_version: Literal["compaction_memory_evidence_redaction_audit.v1"]
    redaction_ordinal: int
    sanitizer_rule_id: str
    sanitizer_rule_version: str
    replacement_text: str
    sanitized_start_char: int
    sanitized_end_char: int
    audit_fingerprint: Fingerprint


class CompactionMemoryEvidenceSemanticFact(FrozenFactBase):
    schema_version: Literal["compaction_memory_evidence_semantic.v1"]
    source_kind: Literal["direct_human_input"]
    sanitized_full_message_text: str
    sanitized_full_message_sha256: str
    sanitized_full_message_utf8_bytes: int
    sanitizer_contract_fingerprint: Fingerprint
    evidence_semantic_fingerprint: Fingerprint


class CompactionMemoryEvidenceInputProjectionFact(FrozenFactBase):
    schema_version: Literal[
        "compaction_memory_evidence_input_projection.v1"
    ]
    projection_kind: Literal["full"]
    evidence_semantic_fingerprint: Fingerprint
    projected_text: str
    projected_text_sha256: str
    projected_text_utf8_bytes: int
    projection_contract_fingerprint: Fingerprint
    projection_fingerprint: Fingerprint


class CompactionMemoryEvidenceAttributionFact(FrozenFactBase):
    schema_version: Literal["compaction_memory_evidence_attribution.v1"]
    evidence_semantic_fingerprint: Fingerprint
    source_event_reference: GovernanceStoredEventReferenceFact
    source_run_id: str
    source_turn_id: str
    source_reply_id: str
    source_message_id: str
    original_text_sha256: str
    original_text_utf8_bytes: int
    source_wire_semantic_fingerprint: Fingerprint
    ordered_redaction_audits: tuple[
        CompactionMemoryEvidenceRedactionAuditFact, ...
    ]
    attribution_fingerprint: Fingerprint


class CompactionMemoryEvidenceNodeFact(FrozenFactBase):
    schema_version: Literal["compaction_memory_evidence_node.v1"]
    evidence_node_id: str
    semantic: CompactionMemoryEvidenceSemanticFact
    input_projection: CompactionMemoryEvidenceInputProjectionFact
    attribution: CompactionMemoryEvidenceAttributionFact
    node_fingerprint: Fingerprint


class CompactionMemoryEvidenceSetSemanticFact(FrozenFactBase):
    schema_version: Literal[
        "compaction_memory_evidence_set_semantic.v1"
    ]
    ordered_evidence_semantics: tuple[
        CompactionMemoryEvidenceSemanticFact, ...
    ]
    ordered_input_projection_fingerprints: tuple[Fingerprint, ...]
    evidence_count: int
    ordered_evidence_semantic_accumulator: Fingerprint
    selection_contract_fingerprint: Fingerprint
    sanitizer_contract_fingerprint: Fingerprint
    input_projection_contract_fingerprint: Fingerprint
    evidence_set_semantic_fingerprint: Fingerprint
```

`evidence_node_id` 使用既有 JSON-LD registry 的 canonical `evidence:` compact IRI 前缀，
是 occurrence identity，由 exact source event identity、human message identity、semantic
fingerprint 与 sanitizer contract deterministic 生成。禁止引入未注册的 graph ID 前缀；物理 event reference
不进入 `evidence_semantic_fingerprint`；node validator 必须 exact join semantic/attribution。

中央factory必须冻结唯一canonical-empty evidence set：

```text
ordered_evidence_semantics = ()
ordered_input_projection_fingerprints = ()
evidence_count = 0
ordered_evidence_semantic_accumulator = canonical empty accumulator
selection/sanitizer/input-projection contract fingerprints = current static contract versions
```

`selection_contract_fingerprint`只标识deterministic选择算法版本，不覆盖resolved target、token
limit、byte limit或本次omission；这些只属于input attribution。任何no-call branch不得自行构造
另一种empty fingerprint。

Input projection validator必须证明 `projection_kind="full"` 且 projected text/digest/bytes与
完整 sanitized semantic逐项相等。V1 registry禁止 `head_tail` 或任何截断 branch。
Redaction audit只属于 attribution，不保存单个原 secret 的digest或正文；char offset只定位
sanitized text中的replacement token。Candidate与governance始终引用整条evidence semantic，
不接受start/end offset。

### 9.3 Sanitizer

Evidence sanitizer 必须是 closed registry，不允许 `str(error)` 或 caller 自报“已脱敏”。至少覆盖：

- bearer/API tokens；
- PEM/private-key blocks；
- common credential assignments；
- DSN password；
- cloud secret/access key patterns；
- overlong opaque high-entropy runs。

规则：

- original text 不复制进 extraction artifact；
- artifact只在attribution保存whole-message original digest/bytes，在semantic保存完整sanitized
  message；禁止保存per-secret match digest；
- governance只能显示同一sanitizer重新生成并验证的完整sanitized message；
- candidate statement 再次经过 output sanitizer；
- sanitizer contract 变化必须改变 extraction contract fingerprint；
- unknown sanitizer version fail closed before provider dispatch。

`GovernanceQuotedEvidenceSemanticFact.quote_kind` 增加并在本路径固定为
`canonical_sanitized_user_message`；`verification_status` 固定为
`canonical_sanitized_match`。该quote表示exact完整sanitized user message，不表示original
message中的连续span。Governance evidence builder必须exact-read原RunStart，重新执行同一
sanitizer并逐字节验证整条message。

### 9.4 Target-aware input budget 与 deterministic selection

新增：

```python
class ResolvedExtractionInputBudgetAttributionFact(FrozenFactBase):
    schema_version: Literal["resolved_extraction_input_budget_attribution.v1"]
    resolved_model_target_fingerprint: Fingerprint
    target_input_limit_tokens: int
    static_prompt_tokens: int
    carrier_and_framing_reserve_tokens: int
    output_reserve_tokens: int
    safety_margin_tokens: int
    usable_evidence_tokens: int
    maximum_physical_input_utf8_bytes: int
    token_estimator_contract_fingerprint: Fingerprint
    budget_selection_contract_fingerprint: Fingerprint
    attribution_fingerprint: Fingerprint


class CompactionMemoryInputBudgetFailureFact(FrozenFactBase):
    schema_version: Literal["compaction_memory_input_budget_failure.v1"]
    failure_kind: Literal[
        "prompt_and_reserves_exceed_target",
        "no_complete_evidence_message_fits",
    ]
    resolved_budget_attribution_fingerprint: Fingerprint
    failure_fingerprint: Fingerprint
```

中央 factory 必须验证：

```text
usable_evidence_tokens =
  target_input_limit_tokens
  - static_prompt_tokens
  - carrier_and_framing_reserve_tokens
  - output_reserve_tokens
  - safety_margin_tokens

usable_evidence_tokens > 0
maximum_physical_input_utf8_bytes <= extraction contract hard bound
```

Target context limit、prompt/framing reserve与 estimator 必须来自 resolved model/transport
contract；caller 不得自报。Evidence selection必须同时满足 estimated token budget 与 UTF-8
byte budget。若 prompt/reserve本身已无法容纳，job 进入 typed、non-retryable
`input_budget_unsatisfiable`，不写 ModelCallStart，也不消耗 provider budget。

V1 默认：

```text
max evidence nodes: 256
max model-visible UTF-8 bytes per node: 8 KiB
max extraction input artifact: 512 KiB
manifest page leaves: 256
manifest page payload: 1 MiB
```

当 eligible evidence 超过 model input bound：

1. 以 sequence 从新到旧stream manifest selection projections；
2. sanitizer已由manifest builder执行；worker验证其contract，不从raw bytes猜测sanitized size；
3. `permanently_oversize`、超过8 KiB或自身无法装入usable token budget的message整条永久省略，
   并继续扫描更老leaf；
4. 禁止生成`head_tail`、摘要或中间截断projection；
5. 对`inline_full`同时估算target token与canonical UTF-8 bytes；当前message无法装入剩余
   aggregate budget时省略该message并继续扫描，不得把它当成停止条件；
6. 仅在真正入选256条、source耗尽，或剩余token/byte budget为零时停止；
7. 对入选项exact-read RunStart、重跑sanitizer、逐字节rebind后恢复causal ascending order；
8. 记录全部永久省略的count、reason、semantic accumulator与attribution accumulator；
9. 不把oversize合法历史dead-letter，也不承诺跨下一次compaction自动补提。

最多 256 nodes 是产品取舍，不是 source completeness 声明。Manifest仍证明 window内全部
eligible leaves；Call B receipt明确把未选择 leaves 标记为
`permanent_automatic_omission`。

### 9.5 Input semantic、occurrence attribution 与 artifact

```python
class CompactionMemoryExtractionInputSemanticFact(FrozenFactBase):
    schema_version: Literal[
        "compaction_memory_extraction_input_semantic.v1"
    ]
    evidence_set: CompactionMemoryEvidenceSetSemanticFact
    prompt_contract_fingerprint: Fingerprint
    input_codec_contract_fingerprint: Fingerprint
    extraction_contract_fingerprint: Fingerprint
    input_semantic_fingerprint: Fingerprint


class CompactionMemoryExtractionInputAttributionFact(FrozenFactBase):
    schema_version: Literal[
        "compaction_memory_extraction_input_attribution.v1"
    ]
    compaction_id: str
    extension_link: CompactionPostCompletionExtensionLinkFact
    request_event_reference: GovernanceStoredEventReferenceFact
    durable_job_id: str
    durable_job_source_reference_fingerprint: Fingerprint
    human_evidence_manifest_reference: (
        CompactionHumanEvidenceManifestReferenceFact
    )
    ordered_evidence_attributions: tuple[
        CompactionMemoryEvidenceAttributionFact, ...
    ]
    resolved_input_budget_attribution: (
        ResolvedExtractionInputBudgetAttributionFact
    )
    permanent_omission_count: int
    permanent_omission_semantic_accumulator: Fingerprint
    permanent_omission_attribution_accumulator: Fingerprint
    attribution_fingerprint: Fingerprint


class CompactionMemoryExtractionInputDocumentFact(FrozenFactBase):
    schema_version: Literal[
        "compaction_memory_extraction_input_document.v1"
    ]
    semantic: CompactionMemoryExtractionInputSemanticFact
    attribution: CompactionMemoryExtractionInputAttributionFact
    document_fingerprint: Fingerprint
```

`input_semantic_fingerprint` 禁止覆盖 compaction/request/job/event/artifact/model/target
identity、resolved context limit、ledger sequence、cursor generation或attributed node。相同
完整sanitized evidence semantics、相同selection/sanitizer/projection/extraction/prompt/codec
contract必须得到相同semantic fingerprint，不受实际target budget影响。
Occurrence attribution只证明本次为何、从哪里执行。
Input attribution已经保存同一个Requested的完整`request_event_reference`；D3 source carrier在该层只
保存`durable_job_source_reference_fingerprint`做exact join，避免`primitives.compaction`反向依赖
`projection_jobs.contracts`并复制第二份物理reference。最终Completed occurrence仍保存完整
`DurableProjectionSourceEventReferenceFact`，transaction companion必须验证两者指向同一Request。

Artifact contract：

```text
media_type:
  application/vnd.pulsara.compaction-memory-extraction-input+json

artifact_id:
  content-addressed from complete document_fingerprint

provider-visible projection:
  semantic.evidence_set only, encoded by the frozen prompt/input codec

write rule:
  put-if-absent-or-confirm-identical
```

Input artifact 必须 FULL 后才允许 ModelCallStart。Artifact locator只进入 model-call/input
attribution，不回流 `input_semantic_fingerprint`。

Input artifact `put-if-absent-or-confirm-identical`必须接收本次job attempt的同一absolute
deadline；PostgreSQL store必须据此安装statement timeout。该blocking write由driver-owned
auxiliary physical operation持有，waiter cancellation只detach逻辑等待，driver仍等待physical
operation真正退出后才释放owner或传播cancellation。禁止让无deadline artifact I/O无限阻塞
driver与Host close。

---

## 10. Call B 模型协议

### 10.1 Model lifecycle vocabulary

新增：

```python
ModelCallPurpose.COMPACTION_MEMORY_EXTRACTION

RuntimeRequestKind:
  "compaction_memory_extraction_request"

RuntimeOperationRequestPayloadFact.operation_kind:
  "compaction_memory_extraction"

OneShotGenerationScopeFact.operation_kind:
  "compaction_memory_extraction_model_call"

ProviderInputGenerationFact.call_lane:
  "compaction_memory_extraction_one_shot"
```

Call B 使用 direct one-shot provider input，不继承 main conversation prefix，也不参与 Host provider-prefix continuity。

### 10.2 Start attribution

`ModelCallStartEvent` 新增可空 branch：

```python
class CompactionMemoryExtractionModelInputAttributionFact(FrozenFactBase):
    schema_version: Literal[
        "compaction_memory_extraction_model_input_attribution.v1"
    ]

    extraction_job_id: str
    dispatch_attempt_ordinal: int
    request_event_reference: GovernanceStoredEventReferenceFact
    input_artifact_reference: GovernanceEvidenceArtifactReferenceFact
    input_semantic_fingerprint: Fingerprint
    input_document_fingerprint: Fingerprint
    resolved_input_budget_attribution_fingerprint: Fingerprint
    background_budget_reservation: (
        BackgroundDerivedWorkBudgetReservationFact
    )
    extraction_contract_fingerprint: Fingerprint
    attribution_fingerprint: Fingerprint
```

Validator matrix：

```text
purpose == COMPACTION_MEMORY_EXTRACTION
  <=> extraction attribution is present

resolved target == Requested.extraction_policy.model_target
operation_id == stable hash(extraction_job_id, dispatch_attempt_ordinal)
context_mode == direct
model_call_index is None
```

### 10.3 Prompt contract

新增静态 prompt：

`src/pulsara_agent/memory/compaction/prompts/memory_extraction_prompt.md`

Root prompt 必须明确：

- 输入节点均为 Pulsara 选择的 canonical human evidence；
- 只提取长期有复用价值、稳定且明确的 user Preference；
- 不把临时任务要求、单次命令参数、路径、token、secret、tool output 写成 preference；
- 不推断用户未明确表达的偏好；
- 后出现的 evidence 可以修正早期 evidence；
- 输出 JSON only；
- evidence_node_ids 必须来自输入；
- 不得输出 candidate ID、scope、authority、confidence 或 verification。

Prompt 是单一静态 owner，不允许运行时字符串替换。

### 10.4 Strict output schema

```python
class CompactionMemoryPreferenceProposalFact(FrozenRuntimeStateBase):
    kind: Literal["Preference"]
    statement: str
    evidence_node_ids: tuple[str, ...]


class CompactionMemoryExtractionOutputFact(FrozenRuntimeStateBase):
    schema_version: Literal[
        "compaction_memory_extraction_output.v1"
    ]
    candidates: tuple[CompactionMemoryPreferenceProposalFact, ...]
```

Wire JSON：

```json
{
  "schema_version": "compaction_memory_extraction_output.v1",
  "candidates": [
    {
      "kind": "Preference",
      "statement": "The user prefers ...",
      "evidence_node_ids": ["evidence:..."]
    }
  ]
}
```

### 10.5 Parser 与 validator

唯一 parser source 是 `ModelCallEndEvent.terminal_projection` exact引用的现有
`TerminalProjectionDocumentFact`。Driver必须通过 `TerminalProjectionReferenceFact` hydrate
content-addressed document，验证：

```text
projection_kind == model_call
terminal_outcome == completed
source_fact resolves to the exact ModelCallStart/End
document/reference/hash/byte-count/contract all match
ordered model items satisfy the terminal projection reducer contract
```

Extraction-specific raw output artifact被禁止。Parser按静态 extraction-output codec从ordered
model projection items重建唯一JSON bytes；result attribution保存terminal projection reference
与parsed semantic fingerprint，但不复制raw正文。Crash后可以重新hydrate并确定性parse同一
terminal document；不得再次调用provider。若未来需要持久化parse cache，必须另立
content-addressed `ParsedExtractionOutputDocumentFact`且只引用terminal projection，不能复制
raw model text；该cache不属于V1。

Parser 必须：

- 只接受一个 JSON object；
- duplicate key reject；
- `NaN`/`Infinity` reject；
- `extra="forbid"`；
- candidates 最多 3；
- 每个 candidate evidence refs 为 1..8；
- evidence refs ordered unique；
- 每个 ref 必须存在于 exact input；
- statement 非空且不超过 1,000 UTF-8 bytes；
- statement 经过 NFC、line-ending、outer-whitespace normalization；
- 不 lowercase，不改写语义；
- secret-like output reject；
- 重复 semantic candidate 在同一 output 内 deterministic collapse：保留首个 ordinal，
  按 input evidence causal order合并 evidence refs；合并后超过上限则整项 reject，不静默截断；
- valid empty 是成功，不是 parser failure。

禁止 permissive Markdown fence search 后“尽量找一个 JSON”。仅允许 closed codec 明确剥离一个完整外层 code fence；任何额外 prose 均 reject。

### 10.6 Runtime-owned normalization

模型无权决定以下字段：

```text
scope              <- configured workspace/project scope
source_authority   <- CONVERSATION_EVIDENCE
verification       <- INFERRED
candidate_id       <- deterministic factory
entry_id           <- deterministic occurrence factory
intent fingerprint <- central semantic factory
```

V1 normalized candidate：

```python
PreferenceCandidate(
    candidate_id=...,
    statement=normalized_statement,
    scope=resolved_scope,
    evidence_ids=ordered_evidence_node_ids,
    source_authority=CONVERSATION_EVIDENCE,
    verification_status=INFERRED,
)
```

### 10.7 Semantic identity 与 occurrence identity

```text
candidate_semantic_fingerprint =
  H("memory-candidate-semantic:v2",
    kind,
    resolved_scope,
    normalized_statement)

candidate_occurrence_fingerprint =
  H("compaction-memory-candidate-occurrence:v1",
    extraction_job_id,
    request_event_id,
    output_candidate_ordinal,
    candidate_semantic_fingerprint,
    ordered_evidence_node_ids,
    extraction_contract_fingerprint)

candidate_id  = "candidate:" + occurrence hash
pool_entry_id = "pool:" + occurrence hash
```

`normalized_statement`的唯一V2规则是Unicode NFC、CRLF/CR统一为LF、删除outer whitespace；
大小写与内部连续空白均保留。Canonical memory view、candidate pool、relatedness的
`is_exact_duplicate`与executor `already_exists()`必须调用同一个中央factory并比较该fingerprint。
Casefold/whitespace collapse只允许用于lexical/alias discovery，不能产生duplicate authority。

Compaction ID、model call ID、attempt ordinal 不进入 candidate semantic fingerprint。这样 reflection、main-agent tool 与 compaction extraction 可以在 governance 中识别语义重复。

---

## 11. Physical model attempt 与 recovery

### 11.1 Stable attempt identity

```text
operation_id =
  H("compaction-memory-extraction-model-operation:v1",
    extraction_job_id,
    dispatch_attempt_ordinal)

resolved_model_call_id =
  resolved-call factory(operation_id, purpose, target, dispatch_attempt_ordinal)
```

Intended ordinal始终为durable job `dispatch_attempt_count + 1`。同一Start candidate的NONE
retry必须复用同一operation/model call identity；只有Start transaction FULL才推进durable
count。Claim/lease generation不得进入operation identity。

### 11.2 执行前 exact-read

每次准备 provider dispatch 前，driver 必须按 operation ID 检查：

1. 是否已有 FULL ModelCallStart；
2. 是否已有 FULL ModelCallEnd；
3. terminal projection 是否 FULL；
4. 是否已有 durable RESULT_READY candidate；
5. 是否已有 final extraction result receipt。

矩阵：

| Durable state | 行为 |
|---|---|
| 无 Start | 可以准备并 dispatch |
| Start，无 End | 先走 model/control recovery，不得直接新调用 |
| End completed | hydrate exact terminal projection并deterministic parse，禁止再次调用provider |
| End provider_error/runtime_error | 当前 attempt terminal，按 retry policy 新 attempt |
| Final result receipt FULL | confirm job success，不再 parse/call |
| conflict/partial | reconciliation required |

### 11.3 At-least-once physical call 边界

Provider 已返回但 ModelCallEnd 尚未 durable 时，进程 crash 可能导致 provider 被再次调用。这是现有 model lifecycle 的 at-least-once physical边界。

但以下不变量必须成立：

- 一旦 ModelCallEnd completed FULL，绝不再次调用 provider；
- 一旦 terminal projection + ModelCallEnd FULL，后续只hydrate/parse；
- 一旦 producer settlement FULL，候选 event/outbox/job result exactly-once；
- UNKNOWN 先 exact-confirm，再决定是否 retry。

### 11.4 Caller cancellation

Provider operation 由 driver-owned task 持有。外层 waiter cancellation 只 detach，不销毁 physical owner。

如果 session close 请求取消：

1. 安装 close intent；
2. 不再 claim 新 job；
3. 请求 active stream stop；
4. 等待 ModelCallEnd/terminal projection；
5. 若已有 completed output则优先 settlement；
6. 否则按 terminal outcome 结算 retry/supersede；
7. physical owner退出后才释放 RuntimeSession/LLM leases。

---

## 12. Result event、candidate outbox 与 job 的原子 settlement

### 12.1 Completed event

新增：

```python
class CompactionMemoryNoEligibleEvidenceResultSemanticFact(FrozenFactBase):
    schema_version: Literal[
        "compaction_memory_no_eligible_result_semantic.v1"
    ]
    outcome_kind: Literal["no_eligible_evidence"]
    evidence_set_semantic_fingerprint: Fingerprint
    extraction_semantic_contract_fingerprint: Fingerprint
    result_semantic_fingerprint: Fingerprint


class CompactionMemoryInputBudgetUnsatisfiableResultSemanticFact(FrozenFactBase):
    schema_version: Literal[
        "compaction_memory_input_budget_unsatisfiable_result_semantic.v1"
    ]
    outcome_kind: Literal["input_budget_unsatisfiable"]
    failure_kind: Literal[
        "prompt_and_reserves_exceed_target",
        "no_complete_evidence_message_fits",
    ]
    evidence_set_semantic_fingerprint: Fingerprint
    budget_selection_contract_fingerprint: Fingerprint
    extraction_semantic_contract_fingerprint: Fingerprint
    result_semantic_fingerprint: Fingerprint


class CompactionMemoryBackgroundBudgetExhaustedResultSemanticFact(FrozenFactBase):
    schema_version: Literal[
        "compaction_memory_background_budget_exhausted_result_semantic.v1"
    ]
    outcome_kind: Literal["background_budget_exhausted"]
    input_semantic_fingerprint: Fingerprint
    evidence_set_semantic_fingerprint: Fingerprint
    exhaustion_kind: Literal[
        "call_cap_exhausted",
        "input_token_cap_exhausted",
        "output_token_cap_exhausted",
        "milliunit_cap_exhausted",
        "account_reconciliation_required",
    ]
    extraction_semantic_contract_fingerprint: Fingerprint
    result_semantic_fingerprint: Fingerprint


class CompactionMemoryValidEmptyResultSemanticFact(FrozenFactBase):
    schema_version: Literal[
        "compaction_memory_valid_empty_result_semantic.v1"
    ]
    outcome_kind: Literal["valid_empty"]
    input_semantic_fingerprint: Fingerprint
    evidence_set_semantic_fingerprint: Fingerprint
    terminal_projection_semantic_fingerprint: Fingerprint
    parser_contract_fingerprint: Fingerprint
    extraction_semantic_contract_fingerprint: Fingerprint
    result_semantic_fingerprint: Fingerprint


class CompactionMemoryValidCandidatesResultSemanticFact(FrozenFactBase):
    schema_version: Literal[
        "compaction_memory_valid_candidates_result_semantic.v1"
    ]
    outcome_kind: Literal["valid_candidates"]
    input_semantic_fingerprint: Fingerprint
    evidence_set_semantic_fingerprint: Fingerprint
    terminal_projection_semantic_fingerprint: Fingerprint
    parser_contract_fingerprint: Fingerprint
    extraction_semantic_contract_fingerprint: Fingerprint
    ordered_candidate_semantic_fingerprints: tuple[Fingerprint, ...]
    result_semantic_fingerprint: Fingerprint


CompactionMemoryExtractionResultSemanticFact = Annotated[
    CompactionMemoryNoEligibleEvidenceResultSemanticFact
    | CompactionMemoryInputBudgetUnsatisfiableResultSemanticFact
    | CompactionMemoryBackgroundBudgetExhaustedResultSemanticFact
    | CompactionMemoryValidEmptyResultSemanticFact
    | CompactionMemoryValidCandidatesResultSemanticFact,
    Field(discriminator="outcome_kind"),
]


class CompactionMemoryNoEligibleEvidenceAttributionFact(FrozenFactBase):
    schema_version: Literal[
        "compaction_memory_no_eligible_attribution.v1"
    ]
    outcome_kind: Literal["no_eligible_evidence"]
    attribution_fingerprint: Fingerprint


class CompactionMemoryInputBudgetUnsatisfiableAttributionFact(FrozenFactBase):
    schema_version: Literal[
        "compaction_memory_input_budget_unsatisfiable_attribution.v1"
    ]
    outcome_kind: Literal["input_budget_unsatisfiable"]
    resolved_input_budget: ResolvedExtractionInputBudgetAttributionFact
    budget_failure: CompactionMemoryInputBudgetFailureFact
    attribution_fingerprint: Fingerprint


class CompactionMemoryBackgroundBudgetExhaustedAttributionFact(FrozenFactBase):
    schema_version: Literal[
        "compaction_memory_background_budget_exhausted_attribution.v1"
    ]
    outcome_kind: Literal["background_budget_exhausted"]
    input_artifact_reference: GovernanceEvidenceArtifactReferenceFact
    input_semantic_fingerprint: Fingerprint
    resolved_input_budget: ResolvedExtractionInputBudgetAttributionFact
    rejected_reservation_quote: ModelCallReservationQuoteFact
    budget_admission_failure: (
        BackgroundDerivedWorkBudgetAdmissionFailureFact
    )
    attribution_fingerprint: Fingerprint


class CompactionMemoryModelResultAttributionFact(FrozenFactBase):
    schema_version: Literal["compaction_memory_model_result_attribution.v1"]
    outcome_kind: Literal["valid_empty", "valid_candidates"]
    input_artifact_reference: GovernanceEvidenceArtifactReferenceFact
    input_semantic_fingerprint: Fingerprint
    resolved_input_budget: ResolvedExtractionInputBudgetAttributionFact
    model_call_start_event_reference: GovernanceStoredEventReferenceFact
    model_call_end_event_reference: GovernanceStoredEventReferenceFact
    model_terminal_projection_reference: TerminalProjectionReferenceFact
    parsed_output_semantic_fingerprint: Fingerprint
    dispatch_attempt_ordinal: int
    background_budget_reservation: BackgroundDerivedWorkBudgetReservationFact
    background_budget_settlement: BackgroundDerivedWorkBudgetSettlementFact
    attribution_fingerprint: Fingerprint


CompactionMemoryExtractionOutcomeAttributionFact = Annotated[
    CompactionMemoryNoEligibleEvidenceAttributionFact
    | CompactionMemoryInputBudgetUnsatisfiableAttributionFact
    | CompactionMemoryBackgroundBudgetExhaustedAttributionFact
    | CompactionMemoryModelResultAttributionFact,
    Field(discriminator="outcome_kind"),
]


class CompactionMemoryExtractionOccurrenceAttributionFact(FrozenFactBase):
    schema_version: Literal[
        "compaction_memory_extraction_occurrence_attribution.v1"
    ]
    compaction_id: str
    extension_link: CompactionPostCompletionExtensionLinkFact
    request_event_reference: GovernanceStoredEventReferenceFact
    durable_job_id: str
    durable_job_source_reference: DurableProjectionSourceEventReferenceFact
    human_evidence_manifest_reference: (
        CompactionHumanEvidenceManifestReferenceFact
    )
    outcome_attribution: CompactionMemoryExtractionOutcomeAttributionFact
    occurrence_attribution_fingerprint: Fingerprint


class CompactionMemoryPreferenceCandidatePayloadFact(FrozenFactBase):
    schema_version: Literal[
        "compaction_memory_preference_candidate_payload.v1"
    ]
    kind: Literal["Preference"]
    candidate_id: str
    statement: str
    scope: str
    evidence_ids: tuple[str, ...]
    source_authority: Literal["conversation_evidence"]
    verification_status: Literal["inferred"]
    candidate_semantic_fingerprint: Fingerprint
    payload_fingerprint: Fingerprint


class CompactionMemoryExtractionCandidateAttributionFact(FrozenFactBase):
    schema_version: Literal[
        "compaction_memory_extraction_candidate_attribution.v1"
    ]

    candidate_entry_id: str
    candidate_ordinal: int
    candidate_payload: CompactionMemoryPreferenceCandidatePayloadFact
    candidate_occurrence_fingerprint: Fingerprint
    candidate_created_at_utc: str
    ordered_evidence_node_ids: tuple[str, ...]
    ordered_evidence_semantic_fingerprints: tuple[Fingerprint, ...]
    attribution_fingerprint: Fingerprint


class ContextCompactionMemoryExtractionCompletedEvent(EventBase):
    type: Literal[
        EventType.CONTEXT_COMPACTION_MEMORY_EXTRACTION_COMPLETED
    ]

    result_semantic: CompactionMemoryExtractionResultSemanticFact
    occurrence_attribution: (
        CompactionMemoryExtractionOccurrenceAttributionFact
    )
    ordered_candidate_attributions: tuple[
        CompactionMemoryExtractionCandidateAttributionFact, ...
    ]
```

矩阵：

- semantic与outcome attribution discriminator必须相等；
- `NO_ELIGIBLE_EVIDENCE`：required canonical-empty evidence fingerprint；无input/model fields；
- `INPUT_BUDGET_UNSATISFIABLE`：required canonical-empty evidence fingerprint、resolved budget +
  typed failure；无input/model fields；
- `BACKGROUND_BUDGET_EXHAUSTED`：required input artifact/input semantic、resolved budget、完整
  rejected `ModelCallReservationQuoteFact`、account reference与typed admission failure；无
  ModelCallStart/End/terminal projection；
- `VALID_EMPTY`：required input、Start/End/terminal projection、background reservation/
  settlement、parsed output semantic；candidate tuple为空；
- `VALID_CANDIDATES`：与VALID_EMPTY相同，candidate tuple为1..3；
- valid-candidates semantic tuple与candidate attribution tuple长度/semantic集合必须相等；
- event source request 必须与 job source exact join。

`CompactionMemoryPreferenceCandidatePayloadFact`是V1唯一event-safe candidate payload；禁止在
event/result candidate中嵌入现有mutable `CandidatePayload`、`MemoryCandidate`、
`PooledMemoryCandidate`或自由metadata dict。Payload factory必须验证evidence IDs与outer
attribution逐项相等，candidate semantic只计算kind/scope/normalized statement。

`candidate_created_at_utc`不得使用pool/runtime default factory：valid model result固定等于exact
ModelCallEnd的`created_at`；no-call branch没有candidate。Completed event自身的`created_at`同样
由result authority冻结：model branch使用ModelCallEnd `created_at`，no-call branch使用source
Request `created_at`。因此crash后重建同一result candidate仍得到byte-identical event payload。

`NO_ELIGIBLE_EVIDENCE`与`INPUT_BUDGET_UNSATISFIABLE`的
`evidence_set_semantic_fingerprint`必须精确等于中央factory产生的required canonical-empty
fingerprint，禁止使用`None`、空字符串或caller自报占位值。`BACKGROUND_BUDGET_EXHAUSTED`
使用已形成input中的exact evidence-set semantic fingerprint；quote、target、account revision与
admission failure只属于outcome attribution，不得进入result semantic。

所有semantic branch禁止包含compaction/request/job/event/artifact/model-call occurrence、target
identity、dispatch ordinal或candidate ordinal。Terminal projection的semantic fingerprint是
模型输出内容semantic，不是physical call identity。Valid-candidates的ordered candidate
fingerprints按fingerprint canonical ascending排序；模型顺序只保存在candidate occurrence
attribution，因此仅输出排序不同不会改变result semantic identity。

Result event不复制 resolved call或usage；它们由：

```text
request -> source Completed
model call End -> Start/resolved call/usage/terminal projection
```

exact-read 获得。RuntimeSession precommit/reducer必须从 occurrence attribution exact-rebind
Request、source Completed、manifest、job与model lifecycle，不能信任 caller 自报 join。

### 12.2 Stable result identity 与 durable RESULT_READY candidate

```text
completed_event_id =
  H("compaction-memory-extraction-completed-event-id:v1",
    runtime_session_id,
    request_event_id,
    job_id,
    target_key)

result_owner = existing ProjectionJobResultOwnerFact(
  job_id,
  job_semantic_fingerprint,
  job_candidate_fingerprint,
  source_event_reference_fingerprint)

result_candidate_id =
  H("compaction-memory-extraction-result-candidate-id:v1",
    result_owner.owner_fingerprint,
    completed_event_id,
    result_semantic_fingerprint)

receipt_id =
  H("compaction-memory-extraction-result-receipt-id:v1",
    result_owner.owner_fingerprint,
    completed_event_id,
    target_key,
    result_semantic_fingerprint)
```

`ProjectionJobResultOwnerFact` 是D3已有closed owner，不得再用未定义的“result owner”文本
代替。新增bounded durable candidate：

```python
class DurableProjectionEventWriteCandidateFact(FrozenFactBase):
    schema_version: Literal["durable_projection_event_write_candidate.v1"]
    event_id: str
    event_type: str
    event_schema_version: str
    event_schema_fingerprint: Fingerprint
    event_domain_contract_fingerprint: Fingerprint
    canonical_unsequenced_payload_utf8: str
    canonical_payload_sha256: Fingerprint
    canonical_payload_utf8_bytes: int
    candidate_fingerprint: Fingerprint


class CandidateOutboxPlanItemFact(FrozenFactBase):
    schema_version: Literal["candidate_outbox_plan_item.v1"]
    candidate_ordinal: int
    candidate_entry_id: str
    candidate_attribution_fingerprint: Fingerprint
    expected_projection_item_fingerprint: Fingerprint
    expected_physical_row_fingerprint: Fingerprint
    item_fingerprint: Fingerprint


class CandidateOutboxPlanFact(FrozenFactBase):
    schema_version: Literal["candidate_outbox_plan.v1"]
    producer_event_id: str
    ordered_items: tuple[CandidateOutboxPlanItemFact, ...]
    item_count: int
    ordered_item_accumulator: Fingerprint
    lowering_contract_fingerprint: Fingerprint
    plan_fingerprint: Fingerprint


class CompactionMemoryExtractionResultCandidateFact(FrozenFactBase):
    schema_version: Literal[
        "compaction_memory_extraction_result_candidate.v1"
    ]
    result_candidate_id: str
    result_owner: ProjectionJobResultOwnerFact
    job_id: str
    target_key: str
    completed_event_id: str
    producer_event_candidate: DurableProjectionEventWriteCandidateFact
    result_semantic_fingerprint: Fingerprint
    receipt_id: str
    intended_target_head_revision: int
    expected_target_head_fingerprint: Fingerprint | None
    candidate_outbox_plan: CandidateOutboxPlanFact
    result_candidate_fingerprint: Fingerprint


class ResultCandidateInstallationGuard(FrozenRuntimeStateBase):
    result_candidate_id: str
    result_candidate_fingerprint: Fingerprint
    job_id: str
    source_job_state_revision: int
    source_job_lease_generation: int
    source_job_lease_fingerprint: Fingerprint
    target_lease_fingerprint: Fingerprint
    guard_fingerprint: Fingerprint
```

Candidate payload最多包含3个candidate及8个evidence refs/candidate，必须受event/outbox byte
hard bound约束。安装算法使用`PROJECTION_MAINTENANCE` transaction：exact-read job/source/
terminal authority，以process-local`ResultCandidateInstallationGuard` CAS exact current job/target
lease，insert immutable result candidate，transition
`LEASED -> RESULT_READY`。No-call branch可直接从LEASED安装。Crash发生在parse后、安装前时，
允许从同一terminal projection deterministic重建；安装FULL后只能hydrate该candidate。

Installation guard在FULL后即销毁，不进入durable RESULT_READY。Restart/reclaim后读取RESULT_READY
不得与新的lease fingerprint比较。D3 lease/state transition已经提供安装审计，V1不在candidate中
复制`source_installation_lease_fingerprint`。

`candidate_outbox_plan`只保存event-safe attribution references、expected lowering fingerprints、
count与accumulator，不保存physical rows。Plan factory必须从同一个
`DurableProjectionEventWriteCandidateFact` thaw exact Completed event并生成；event candidate是
candidate payload的唯一真源。

No-call与`VALID_EMPTY` branch必须使用中央canonical-empty outbox plan；
`VALID_CANDIDATES` plan item count必须等于Completed event candidate attribution count并保持同一
ordinal。`producer_event_id`必须等于`completed_event_id`，每个plan item只能引用event内同ordinal
attribution fingerprint。Plan/current lease/physical row之间不存在任何反向引用。

Receipt中的stored completed reference含最终ledger sequence，无法在event append前冻结；
transaction companion必须从EventLog传入的exact `stored_events`与stable candidate唯一构造。
Receipt ID、result semantic、target revision与outbox plan已经冻结，最终sequence只属于receipt
attribution，不改变result candidate identity。

### 12.3 Durable result receipt

新增 branch：

```python
class CompactionMemoryExtractionProjectionResultReceiptFact(FrozenFactBase):
    schema_version: Literal[
        "compaction_memory_extraction_projection_result_receipt.v1"
    ]

    receipt_id: str
    projection_kind: Literal[
        DurableProjectionKind.COMPACTION_MEMORY_EXTRACTION
    ]
    job_id: str
    target_key: str
    source_request_event_reference: DurableProjectionSourceEventReferenceFact
    completed_event_reference: GovernanceStoredEventReferenceFact
    completed_result_semantic_fingerprint: Fingerprint
    target_head_revision: int
    outbox_item_count: int
    outbox_item_accumulator: Fingerprint
    permanent_automatic_omission_count: int
    permanent_automatic_omission_semantic_accumulator: Fingerprint
    permanent_automatic_omission_attribution_accumulator: Fingerprint
    receipt_fingerprint: Fingerprint
```

`receipt_id` 由`ProjectionJobResultOwnerFact`、source request reference、target key与result semantic
deterministic生成，不覆盖 target head。Settlement先按 target CAS确定
`target_head_revision`，构造 receipt，再构造
`DurableProjectionTargetHeadFact(applied_result_receipt_reference=...)`。Target head单向引用
receipt ID/fingerprint；receipt禁止保存 head fingerprint。由此不存在 receipt/head自引用。

### 12.4 Transaction companion

新增 memory-owned settlement port：

```python
class CompactionMemoryExtractionSettlementWriteAttemptIdentity(
    FrozenRuntimeStateBase
):
    result_candidate_id: str
    result_candidate_fingerprint: Fingerprint
    settlement_generation: int
    opened_at_utc: str
    deadline_monotonic: float
    identity_fingerprint: Fingerprint


@dataclass(slots=True)
class CompactionMemoryExtractionSettlementWriteAttempt:
    identity: CompactionMemoryExtractionSettlementWriteAttemptIdentity
    active: bool = True

    def consume(self) -> None: ...


class CompactionMemoryExtractionSettlementPort(Protocol):
    async def commit_result(
        self,
        *,
        result_candidate: CompactionMemoryExtractionResultCandidateFact,
        write_attempt: CompactionMemoryExtractionSettlementWriteAttempt,
    ) -> CompactionMemoryExtractionSettlementOutcome: ...
```

该 port 是 session-bound memory service；其唯一 event-write dependency 是
`RuntimeSession.write_events_with_deadline()`。D3 repository先以独立CAS执行：

```text
RESULT_READY | SETTLEMENT_RETRY_WAIT
  -> SETTLEMENT_WRITING(settlement_generation + 1)
```

然后issuer创建process-local attempt；deadline不进入result candidate fingerprint。Port hydrate
stable event candidate并构造 `EventLogTransactionCompanion`：

```text
RuntimeSession.write_events_with_deadline(
  (ContextCompactionMemoryExtractionCompletedEvent,),
  deadline_monotonic=write_attempt.identity.deadline_monotonic,
  state=WRITING,
  transaction_companion=prepared_memory_extraction_companion)
```

RuntimeSession writer唯一拥有：

- `agent_events` append；
- materialization/account charge；
- event schema与precommit reducer；
- FULL/NONE/UNKNOWN confirmation；
- committed publication。

Transaction companion只能在 writer提供的同一 PostgreSQL cursor/transaction内写 receipt、
head、candidate outbox与job state；它禁止修改background budget account/reservation/settlement，
禁止直接`INSERT agent_events`、另开connection或使用`PROJECTION_MAINTENANCE` lane写result
event。

Background budget只有两个mutation owner：ModelCallStart companion reserve，ModelCall terminal
companion settle。Result companion对model-result branch必须exact-read并验证既有reservation/
settlement与Start/End/terminal projection join；no-call branches必须证明没有background
reservation/settlement，且绝不修改account。Architecture SQL allowlist必须让result companion对
所有background budget relations保持read-only。

唯一合法 PostgreSQL transaction：

```text
BEGIN
  validate runtime write epoch / normal admission
  validate exact RESULT_READY candidate + SETTLEMENT_WRITING generation
  validate target SINGLE_ASSIGNMENT head
  model-result branch: exact-read existing budget reservation/settlement
  no-call branch: prove absence of model lifecycle and budget mutation
  RuntimeSession writer appends ContextCompactionMemoryExtractionCompletedEvent
  derive exact stored completed reference from stored_events
  thaw stored Completed event candidate
  lower candidate attribution -> physical outbox rows with every field explicit
  verify CandidateOutboxPlanFact count/ordered accumulator/row fingerprints
  insert immutable projection result receipt
  install exact target head
  insert memory candidate projection outbox rows
  transition job -> SUCCEEDED
COMMIT
```

`producer event + outbox + receipt + head + job success` 必须同事务。禁止：

- 先标记 job success 再写 event；
- 先写 candidates 再写 producer event；
- MemoryCandidateProjectionCommitPort 与 D3 repository 分别 commit；
- completed event 后 callback 再 settlement。

Outbox lowering必须显式设置：entry/payload/origin、source session/run/turn/reply、source event、
evidence locator、intent fingerprint、closed metadata与`candidate_created_at_utc`。禁止调用
`PooledMemoryCandidate`的`entry_id`、`metadata`或`created_at` defaults。生成的暂时性
`CandidateProjectionOutboxRow`只存在于该transaction companion调用栈中；不得回存RESULT_READY
或成为第二份result authority。

`CompactionMemoryExtractionSettlementOutcome` 是 closed process-local carrier：

```python
class CompactionMemoryExtractionSettlementOutcome(FrozenRuntimeStateBase):
    confirmation: Literal["full", "none", "conflict", "unresolved"]
    result_candidate_id: str
    result_candidate_fingerprint: Fingerprint
    settlement_generation: int
    producer_event_identity: StableEventIdentityFact
    result_receipt_reference: (
        DurableProjectionResultReceiptReferenceFact | None
    )
    target_head_revision: int | None
    publication_status: Literal[
        "not_applicable", "completed", "enqueued", "unavailable", "failed_after_commit"
    ]
    runtime_session_ledger_reconciliation_required: bool
    outcome_fingerprint: Fingerprint
```

Factory从 `RuntimeSession` typed write result构造，caller不能自报。Stable result candidate、
event ID、receipt ID、target revision与outbox plan跨所有physical attempts保持不变；只有
`settlement_generation` 与该generation的absolute deadline变化。

### 12.5 Confirmation matrix

```text
FULL
  event、receipt、head、outbox、job status 全部 exact

NONE
  全部不存在；当前deadline内可bounded retry；deadline耗尽后CAS到
  SETTLEMENT_RETRY_WAIT，保留同一result candidate

CONFLICT
  任一 identity 已存在但 payload不同，fail closed

UNRESOLVED
  无法证明 FULL/NONE，retain owner + reconciliation latch
```

UNKNOWN 后 exact confirmation 必须在新 connection、同 database target、同 runtime write epoch 下执行。

`SETTLEMENT_RETRY_WAIT` 到期后可以签发新generation与新absolute deadline；这不是deadline
续期，因为前一physical owner已明确终结。禁止重新hydrate provider、增加dispatch attempt、
改变event payload或另造receipt。Crash/reopen若发现`SETTLEMENT_WRITING`过期，先按stable
candidate exact-confirm：FULL则SUCCEEDED，NONE则RETRY_WAIT，其余进入ledger reconciliation。

### 12.6 Outbox projection 与 governance

Job 在 outbox durable 后即为 `SUCCEEDED`。candidate pool 的实际 projection 继续由 `MemoryCandidateProjectionCommitPort`/dispatcher owning outbox state 完成。

因此：

- candidate pool 暂时不可用不会重跑 Call B；
- outbox retry 不修改 job result；
- outbox dead-letter 与 extraction job dead-letter 是不同状态；
- governance 只读取已投影 candidate 与 exact producer evidence。

---

## 13. Failure、retry 与 close 状态机

### 13.1 Job 状态机

```text
PENDING
  -> LEASED
  -> SUPERSEDED                     # graceful close before execution

LEASED
       -> PENDING                    # no Start FULL; safe deferral/lease loss
       -> MODEL_RETRY_WAIT           # one dispatched attempt terminally failed
       -> SUPERSEDED                 # graceful close and no Start FULL
       -> DEAD_LETTER
       -> RECONCILIATION_REQUIRED latch
       -> RESULT_READY               # no-call or deterministic parsed result

MODEL_RETRY_WAIT
  -> LEASED                          # not_before reached + due-claim CAS
  -> SUPERSEDED                      # graceful close; no live model/result owner

RESULT_READY
  -> SETTLEMENT_WRITING(g)
       -> SUCCEEDED
       -> SETTLEMENT_RETRY_WAIT       # exact NONE / physical deadline exhausted
       -> RECONCILIATION_REQUIRED     # event transaction UNKNOWN/PARTIAL

SETTLEMENT_RETRY_WAIT -- due retry CAS --> SETTLEMENT_WRITING(g+1)
SETTLEMENT_RETRY_WAIT -- close maintenance CAS --> SETTLEMENT_WRITING(g+1)
```

`dispatch_attempt_count`只在`SESSION_MODEL_PROJECTION`的ModelCallStart companion中推进；其他D3
execution class继续使用既有`attempt_count`。`settlement_generation`只在进入
SETTLEMENT_WRITING时推进。现有enum如不包含这些状态，migration v9必须显式扩展operational
state/row，不能用diagnostic字符串模拟。`RECONCILIATION_REQUIRED`可由durable owner/latch表
承载，但不得压成`DEAD_LETTER`。

`MODEL_RETRY_WAIT -> LEASED`必须由repository在`not_before <= now`时执行单一due-claim CAS；
CAS同时验证`dispatch_attempt_count < maximum_attempts`并只增加lease generation/state
revision，不增加dispatch count；达到上限必须进入DEAD_LETTER，不得由worker先改状态再另行
claim。Graceful close可以将PENDING、无Start FULL的LEASED及MODEL_RETRY_WAIT转为
SUPERSEDED。RESULT_READY与SETTLEMENT_RETRY_WAIT已经拥有待提交事实，禁止supersede。

Provider terminal failure与output-contract failure提交`MODEL_RETRY_WAIT`后，driver只接受
`FULL`。若返回`CONFLICT`，repository必须在同一transaction exact-read已有winner；只有相同
dispatch ordinal、failure、state revision、released job/target lease及相同retry state才可归类为
兼容`FULL`，其他结果进入reconciliation/error，禁止被driver当成成功处理。

### 13.2 Failure taxonomy

新增或冻结：

| Failure | 分类 | 默认动作 |
|---|---|---|
| provider unavailable | transient | retry |
| model timeout | transient | retry |
| provider error terminal | transient/contract policy | retry |
| output JSON malformed | model output contract | bounded retry |
| candidate secret detected | model output contract | bounded retry |
| source event temporarily unreadable | transient storage | retry |
| manifest root/completeness mismatch | source authority conflict | dead-letter + job/target latch |
| request/completed join mismatch | source authority conflict | dead-letter + job/target latch |
| handler/extractor fingerprint mismatch | target contract mismatch | dead-letter + target latch |
| target input budget unsatisfiable | terminal policy | completed no-call result |
| session background budget exhausted | terminal policy | completed no-call result |
| background account/quote recurrence mismatch | account authority conflict | background account latch |
| foreground arrives after ModelCallStart | typed preemption | stop/settle then retry policy |
| result settlement NONE | stable result-candidate owner | retry same candidate under a new physical write generation when needed |
| result event transaction UNKNOWN/PARTIAL | ledger reconciliation | RuntimeSession ledger latch；no new provider call |
| model Start/End transaction UNKNOWN/PARTIAL | ledger reconciliation | RuntimeSession ledger latch；no new provider call |
| no live session driver | not a failure | stay pending |
| graceful session close while PENDING/eligible LEASED/MODEL_RETRY_WAIT | typed supersede | no provider call |

所有 durable diagnostic 使用 closed sanitizer。禁止持久化 raw provider exception、prompt、credential 或 full response。

Latch必须按fault domain隔离：

```text
JOB/TARGET
  manifest/source/request/handler authority mismatch
  -> block only the exact projection job or target
  -> foreground run and unrelated extraction targets remain available

BACKGROUND_ACCOUNT
  budget recurrence, quote or settlement mismatch
  -> block new background-derived model admission for that account
  -> foreground model calls remain available

RUNTIME_SESSION_LEDGER
  ModelCallStart、ModelCallEnd或result event transaction UNKNOWN/PARTIAL
  -> block ordinary RuntimeSession mutation until exact reconciliation

NONE
  provider failure、strict parser rejection、ordinary retry exhaustion
  -> retry/dead-letter according to job policy；do not install another latch
```

可选Call B不得仅因manifest、handler、provider、parser或job dead-letter反向阻断前台run。
只有共享RuntimeSession EventLog事务本身进入UNKNOWN/PARTIAL时，才允许安装ledger级latch。

### 13.3 Backoff

Provider/model retry以`dispatch_attempt_count`为ordinal：

```text
dispatch attempt 1 -> 1s
dispatch attempt 2 -> 2s
dispatch attempt 3 -> 4s
```

Settlement writer的同generation `NONE` retry使用10..250ms bounded exponential backoff；该
generation deadline耗尽后进入`SETTLEMENT_RETRY_WAIT`。下一次durable settlement generation使用
固定1s durable defer后签发新physical deadline，并必须复用同一RESULT_READY candidate。V1 model retry
与settlement defer均不使用jitter；schedule完全由stored Request派生的base/max policy与closed recurrence
决定，禁止读取process-local随机源或zero-delay retry loop。

### 13.4 Session close

Close 顺序：

```text
close new extraction admission
close new manifest preparation admission
stop claiming this RuntimeSession
supersede unclaimed pending jobs for graceful explicit session close
stop/drain active provider operations
settle completed physical results if available
consume/abandon all manifest logical owners and request physical cancel
bounded drain all manifest preparation operations to physical EXITED
drain RESULT_READY / SETTLEMENT_RETRY_WAIT owners without superseding them;
  close maintenance may bypass settlement not_before
drain extraction transaction owners
unregister session driver
release RuntimeSession / LLM / artifact / DB leases
continue ordinary Host close
```

Manifest preparation、provider或settlement owner的所有drain共享同一个Host close absolute
deadline，不得为各阶段续期。Close maintenance绕过`SETTLEMENT_RETRY_WAIT.not_before`只改变
调度资格，不放宽deadline或改变result candidate。任一active physical owner未在deadline内退出，
Host close传播bounded drain failure并保持blocked，不得伪装成功或释放dependency。

Detach 与 close 不同：detach 不 supersede job，只要 RuntimeSession 仍由 Host process 持有，driver 可以继续工作。

### 13.5 Process restart

Restart 后：

1. D3 repository 恢复 pending/retry/expired lease；
2. Host/RuntimeSession reopen 注册新 driver generation；
3. worker exact-read Request/Completed/manifest；
4. recovery 检查 deterministic model operation lifecycle；
5. 已有 ModelCallEnd completed 时只hydrate terminal projection并deterministic parse，不重新调用；
6. 已有RESULT_READY/SETTLEMENT_RETRY_WAIT时只签发新settlement generation，不重新调用；
7. final settlement FULL 时 confirm success；
8. dangling model Start 先走现有 model/control recovery。

禁止：

- 扫描 Completed 创建缺失 Requested；
- 根据 summary tag 恢复 candidate；
- 重新解释旧 one-call output；
- 在没有 exact request event 时补 job。

---

## 14. Governance evidence hard cut

### 14.1 新 semantic source

删除 `CompactionGovernanceSourceSemanticFact` 中以下 summary-based fields：

```text
summary_content_sha256
summary_content_semantic_fingerprint
raw_candidate_index
quoted_evidence_semantic: optional single summary span
```

替换为：

```python
class CompactionExtractionGovernanceSourceSemanticFact(
    GovernanceEvidenceFrozenFact
):
    schema_version: Literal[
        "compaction_extraction_governance_source_semantic.v1"
    ]
    evidence_kind: Literal["compaction"]

    candidate_payload_semantic_fingerprint: Fingerprint
    evidence_set_semantic_fingerprint: Fingerprint
    extraction_semantic_contract_fingerprint: Fingerprint

    ordered_evidence_semantics: tuple[
        GovernanceQuotedEvidenceSemanticFact, ...
    ]
    semantic_fingerprint: Fingerprint
```

每个 quoted evidence 必须：

- `quote_kind="canonical_sanitized_user_message"`；
- 引用完整 verified evidence node，不携带 start/end char；
- `verification_status="canonical_sanitized_match"`；
- exact-read original RunStart human message；
- 与 evidence node original digest、selection、sanitizer contract exact join；
- governance prompt只能使用重新计算并逐字节验证的 sanitized projection。

`source range`、compaction/request/job identity、model result、artifact locator、attempt与
candidate ordinal都禁止进入 governance semantic fingerprint。相同 candidate semantic与
相同 ordered sanitized evidence semantics，在相同 extraction semantic contract下必须得到
相同 governance semantic identity。

### 14.2 Attribution

`GovernanceSourceEvidenceAttributionFact` 的 compaction branch 必须引用：

1. extraction Requested event；
2. extraction Completed event；
3. source compaction Completed event；
4. model call Start/End/terminal projection，若 provider 被调用；
5. input artifact与作为唯一模型输出authority的terminal projection；
6. exact human RunStart event refs；
7. candidate attribution fingerprint与candidate ordinal；
8. permanent omission receipt；
9. extraction occurrence attribution fingerprint。

Summary artifact 可以作为 correlation reference，但不得进入 evidence semantic fingerprint，也不得作为 prompt evidence text。

### 14.3 Governance prompt projection

Compaction candidate 的 model-visible governance evidence 改为：

```text
candidate statement
ordered canonical human evidence snippets
```

不再投影 compaction summary。

Candidate 仍为：

```text
accepted = false
source authority = conversation evidence
verification = inferred
```

“evidence canonical”不等于“candidate conclusion已验证”。

### 14.4 Cross-producer dedupe

Governance relatedness/dedupe 必须使用 shared `candidate_semantic_fingerprint`：

- same scope + kind + normalized statement；
- normalization仅执行NFC、line-ending canonicalization与outer trim，保留大小写和内部空白；
- origin、compaction ID、reflection ID、tool call ID 不进入 semantic identity；
- occurrence/provenance 仍永久保留；
- exact duplicate 可以 skip/merge，但不得删除 audit record。

---

## 15. Schema migration 与 hard cut

### 15.1 Migration 0009

新增：

```text
src/pulsara_agent/storage/migrations/sql/
  0009_compaction_memory_extraction_projection_activation.sql

src/pulsara_agent/storage/migrations/resources/
  0009_compaction_memory_extraction_activation_v1.json
  0009_runtime_write_protected_relations_v1.json

src/pulsara_agent/storage/migrations/
  expected_catalog_v9.json
```

Migration 必须：

- 注册新 projection kind activation；
- 安装 handler/seed/retry/result contract fingerprint；
- 为`SESSION_MODEL_PROJECTION`增加独立`dispatch_attempt_count`及其claim/Start CAS constraints；
- 增加`RESULT_READY`、`SETTLEMENT_WRITING`、`SETTLEMENT_RETRY_WAIT`状态、immutable result
  candidate relation与settlement-generation constraints；
- result candidate payload只允许durable event candidate、immutable outbox plan与stable target
  metadata；catalog/validator禁止physical row或lease fingerprint字段；
- 创建 background-derived-work budget account/reservation relation及CAS constraints；
- 创建 extraction receipt/head/outbox/job settlement所需 indexes；
- 将 candidate outbox、projection result/head/job settlement 涉及 relation 纳入 protected relation registry；
- 为 fresh session bootstrap 安装新 active cutover；
- 更新 grants/manifest/catalog；
- 使用 D3 maintenance epoch/barrier；
- 不在 constructor 或 startup hot path 执行 DDL。

### 15.1.1 历史 migration identity 不得变化

增加 `DurableProjectionKind.COMPACTION_MEMORY_EXTRACTION` 后，禁止修改 v5-v8 的 packaged
manifest、pre-activation resource、activation resource、migration contract 或 registry
prefix。特别是当前 v6 loader 不得继续使用 `tuple(DurableProjectionKind)` 解释历史
resource；它必须改为显式历史 closed set：

```python
DURABLE_PROJECTION_V6_PRE_ACTIVATION_KINDS = (
    DurableProjectionKind.RUN_TIMELINE,
    DurableProjectionKind.TOOL_RESULT_EXECUTION_EVIDENCE,
)
```

V9 当前 registry completeness 由“v6 historical set + v9 activation”推导。Golden gate 必须
证明追加 v9 后 migration 0..8 的：

```text
migration_contract_fingerprint
registry_prefix_fingerprint
object_manifest_fingerprint
expected_catalog_fingerprint
```

逐项保持不变。全局 reserved object-name union 只能是 deep-audit contract，不能回流历史
migration identity。

### 15.2 Reset-only

D5 修改：

- compaction event schema；
- memory candidate producer event；
- governance evidence identity；
- projection kind activation；
- candidate occurrence identity。

因此 production cutover 为 reset-only：

```text
stop Host admission
drain model/tool/projection owners
enter maintenance epoch
verify no in-flight owner
reset PostgreSQL event/memory/projection world
reset Oxigraph projection world
apply migrations 0000..0009
verify catalog/grants/registry prefix
install new runtime epoch
start new binary
```

对非空 v8 database，`db migrate` 必须返回 typed `RESET_REQUIRED_FOR_COMPACTION_MEMORY_EXTRACTION_V1`，不得：

- decode old summary memory block；
- backfill Requested；
- migrate old proposed candidates；
- 将旧 summary evidence 归因为新 exact human evidence。

### 15.3 Session bootstrap

`RuntimeSessionOwnerBootstrapPort` 必须在 session row 同一 transaction 中写新 projection
cutover与revision 0 background-derived-work budget account。

Bootstrap exact confirmation 比较完整 DTO/fingerprint，不只比较 kind set。

---

## 16. 删除清单

Hard cut 后物理删除：

- `src/pulsara_agent/runtime/compaction/candidates.py`；
- `ContextCompactionMemoryCandidatePolicy`；
- `ContextCompactionPolicy.memory_candidates`；
- `ContextCompactionService.candidate_sink`；
- `ContextCompactionService.candidate_projection_commit_port`；
- compaction service 内所有 candidate owner/receipt dict/task/drain；
- `parse_compaction_memory_candidates()`；
- `<memory_candidates_json>` prompt 与 parser；
- `production_compaction_prompt(memory_candidates_enabled=...)` 参数；
- `_without_memory_candidate_instructions()`；
- `ContextCompactionMemoryCandidatesProposedEvent`；
- `CompactionCandidateProjectionRequestIdentity`；
- `PreparedCompactionCandidateProjectionInput`；
- `CompactionCandidateProjectionReceipt`；
- old summary-based `CompactionMemoryCandidateExtractorContractFact`；
- old summary-based `CompactionCandidateAttributionFact`；
- old summary evidence governance branch；
- old tests/fixtures/golden schema literals。

旧 symbol 不保留 re-export、deprecated alias、historical decoder allowlist 或 fallback。

---

## 17. 逐文件修改面

### 17.1 新增文件

| 文件 | 内容 |
|---|---|
| `src/pulsara_agent/ports/compaction_extensions.py` | generic extension protocol；identity facts + frozen dataclass live carriers + `FrozenEventWriteCandidate` |
| `src/pulsara_agent/ports/model_lifecycle.py` | purpose-neutral model lifecycle transaction companion contract |
| `src/pulsara_agent/primitives/compaction.py` | human-evidence manifest、extension disposition、memory extraction durable facts |
| `src/pulsara_agent/memory/compaction/__init__.py` | memory-owned public composition surface |
| `src/pulsara_agent/memory/compaction/contracts.py` | extraction contract/policy/output、event-safe Preference payload、outbox plan与candidate attribution |
| `src/pulsara_agent/memory/compaction/extension.py` | extension intent/request factory |
| `src/pulsara_agent/memory/compaction/manifest.py` | transcript cursor/leaf、sanitizer-bound selection projection、paged manifest、physical preparation owner与completeness proof |
| `src/pulsara_agent/memory/compaction/evidence.py` | target-aware selection、eligibility、evidence node builder |
| `src/pulsara_agent/memory/compaction/sanitizer.py` | closed sanitizer registry |
| `src/pulsara_agent/runtime/projection_jobs/compaction_budget.py` | input budget与background-derived-work account deterministic transitions；由D3 PostgreSQL adapter消费 |
| `src/pulsara_agent/projection_jobs/compaction_memory_policy.py` | Request-derived D3 delivery policy与closed V1 execution defaults |
| `src/pulsara_agent/memory/compaction/parser.py` | strict output codec/normalizer |
| `src/pulsara_agent/memory/compaction/driver_support.py` | memory-owned evidence/parser/result/settlement contract facade |
| `src/pulsara_agent/runtime/projection_jobs/compaction_memory_driver.py` | RuntimeSession-bound session driver、model lifecycle与recovery adapter |
| `src/pulsara_agent/runtime/projection_jobs/model_lifecycle.py` | Request/job/account exact join与atomic Start/End companions |
| `src/pulsara_agent/memory/compaction/result_candidate.py` | immutable RESULT_READY candidate/outbox plan factory与hydration |
| `src/pulsara_agent/projection_jobs/compaction_memory.py` | durable RESULT_READY fact与process-local installation guard |
| `src/pulsara_agent/memory/compaction/settlement_support.py` | memory-owned outbox/result lowering facade |
| `src/pulsara_agent/runtime/projection_jobs/compaction_memory_settlement.py` | producer/outbox/job atomic RuntimeSession transaction companion |
| `src/pulsara_agent/memory/compaction/prompts/memory_extraction_prompt.md` | Call B static prompt |
| `src/pulsara_agent/storage/migrations/sql/0009_compaction_memory_extraction_projection_activation.sql` | activation schema/data |
| `src/pulsara_agent/storage/migrations/resources/0009_compaction_memory_extraction_activation_v1.json` | packaged activation contract |
| `src/pulsara_agent/storage/migrations/expected_catalog_v9.json` | deep catalog golden |
| `tests/test_compaction_memory_extraction_contracts.py` | DTO/fingerprint/parser tests |
| `tests/test_compaction_memory_extraction_runtime.py` | model driver/state-machine tests |
| `tests/test_compaction_memory_extraction_postgres.py` | durable job/settlement/recovery integration |
| `tests/test_compaction_memory_governance_evidence.py` | exact evidence/governance tests |

### 17.2 Compaction core

| 文件 | 修改 |
|---|---|
| `runtime/compaction/service.py` | summary-only；并行manifest owner；generic extension batch；保留现有bounded async source read；删除 memory ownership |
| `runtime/compaction/planner.py` | summary parser 不再识别 memory tag；不引入PostgreSQL paging |
| `runtime/compaction/inline.py` | 不触发 candidate owner；保持现有compaction boundary语义 |
| `runtime/compaction/commit.py` | completion batch 支持 extension companion exact retry/confirmation |
| `runtime/compaction/prompts/context_compaction_prompt.md` | 仅 summary protocol |
| `runtime/compaction/__init__.py` | 删除 candidate exports，导出 core-only contract |

### 17.3 Event/primitives/model lifecycle

| 文件 | 修改 |
|---|---|
| `event/events.py` | 新 Requested/Completed events；修改 compaction lifecycle schemas；删除旧 Proposed event |
| `event/__init__.py` | event export hard cut |
| `event_log/serialization.py` | schema registry/golden 更新；Request使用现有 `FrozenEventWriteCandidate`；不加旧 decoder |
| `primitives/model_call.py` | 新 ModelCallPurpose |
| `primitives/runtime_observation.py` | 新 runtime request kind/operation kind |
| `primitives/provider_input.py` | 新 one-shot operation/lane |
| `primitives/long_horizon.py` | 复用并验证现有 `ModelCallReservationQuoteFact`，不新增第二套quote |
| `llm/commit.py` | start/end commit guard 接入purpose-neutral transaction companion |
| `llm/lifecycle.py` | extraction Start attribution matrix |
| `llm/runtime.py` | direct call owner/recovery join |
| `llm/recovery.py` | dangling extraction call recovery |
| `runtime/provider_input/planner.py` | one-shot lane mapping |
| `runtime/provider_input/coordinator.py` | extraction one-shot generation scope |

### 17.4 D3 durable projection

| 文件 | 修改 |
|---|---|
| `projection_jobs/contracts.py` | 新 kind、execution class、dispatch count、RESULT_READY/retry states、result receipt branch、failure taxonomy |
| `projection_jobs/compaction_memory_policy.py` | Request extraction policy与source-derived D3 delivery policy的唯一factory |
| `ports/projection_jobs.py` | session-model driver/settlement port contracts |
| `runtime/projection_jobs/registry.py` | trigger/handler/driver completeness |
| `runtime/projection_jobs/seeder.py` | Requested -> stable single-assignment job |
| `runtime/projection_jobs/source.py` | exact Requested/Completed/manifest join |
| `runtime/projection_jobs/postgres_repository.py` | model-job claim不增attempt；retry due-claim/close CAS；Start companion CAS；immutable result candidate与settlement-generation transitions |
| `runtime/projection_jobs/model_lifecycle.py` | ModelStart exact-read Request并原子join job policy、target与background account policy |
| `runtime/projection_jobs/repository.py` | closed repository protocol 扩展 |
| `runtime/projection_jobs/service.py` | driver registry、low-priority claim、wake/close |
| `runtime/projection_jobs/result.py` | extraction receipt hydration/validation |
| `runtime/projection_jobs/inspection.py` | pending/no-driver/attempt/dead-letter projection |
| `runtime/projection_jobs/pre_activation.py` | v9 activation/readiness/reset-required |
| `runtime/projection_jobs/migration_transform.py` | v9 fresh-world activation only |

### 17.5 Memory candidate/governance

| 文件 | 修改 |
|---|---|
| `memory/candidates/pool.py` | shared candidate semantic fingerprint；new attribution metadata only |
| `memory/candidates/projection_outbox.py` | 从stored Completed event确定性lower physical rows；全部字段显式；plan exact confirmation |
| `memory/canonical/postgres_uow_scope.py` | same-transaction candidate insert持久化同一个shared semantic fingerprint |
| `memory/governance/evidence.py` | canonical human evidence builder，删除 summary evidence |
| `memory/governance/dedupe.py` | cross-producer semantic dedupe |
| `memory/governance/batch_input.py` | canonical evidence prompt projection |
| `primitives/governance_evidence.py` | new semantic/attribution/input artifact contracts与immutable outbox plan shared facts |
| `primitives/memory_candidate.py` | shared candidate semantic factory；现有mutable candidate DTO不得进入event/RESULT_READY |
| `memory/reflection/engine.py` | 只切 shared semantic identity，不改变 reflection trigger/lifecycle |

### 17.6 Composition、Host 与 close

| 文件 | 修改 |
|---|---|
| `runtime/wiring.py` | compose memory extension、driver、settlement；compaction core只收 generic port |
| `host/composition_contract.py` | process driver registry与session registration contract |
| `host/production_composition.py` | process-owned registry/service；不创建新 thread pool |
| `host/core.py` | RuntimeSession open/close register/unregister driver |
| `host/session.py` | background admission、foreground preemption、manifest physical owner与close/supersede/drain |
| `runtime/session.py` | same-batch validator、composite transaction companion、budget read-only guard、recovery joins |

### 17.7 Storage、CLI、Inspector 与文档

| 文件 | 修改 |
|---|---|
| `storage/migrations/registry.py` | immutable v9 definition/prefix |
| `storage/migrations/manifest.py` | v9 relation/index/grant shape |
| `storage/migrations/runner.py` | reset-required prerequisite |
| `storage/session_bootstrap.py` | new active kind cutover exact confirmation |
| `inspector/service.py` | extraction request/job/model/candidate chain |
| `inspector/store.py` | bounded exact reads |
| `cli.py` | `projection jobs`, retry/dead-letter/diagnostic display；不提供直接 approve memory |
| `PULSARA_RUNTIME_ARCHITECTURE_DEBT_REBASE.zh.md` | CME5 后 D5 CLOSED，更新 one-call 旧结论 |

### 17.8 长期 contracts

必须同步：

- `contracts/CONTEXT_COMPACTION_CONTINUITY_CONTRACT.zh.md`；
- `contracts/MESSAGE_TRANSCRIPT_CONTEXT_CONTRACT.zh.md`；
- `contracts/LLM_TRANSPORT_CONTRACT.zh.md`；
- `contracts/AGENT_RUNTIME_LOOP_CONTRACT.zh.md`；
- `contracts/EVENT_LOG_STORAGE_CONTRACT.zh.md`；
- `contracts/ARTIFACT_STORE_CONTRACT.zh.md`；
- `contracts/MEMORY_SURFACES_CONTRACT.zh.md`；
- `contracts/GOVERNANCE_WRITE_OUTBOX_CONTRACT.zh.md`；
- `contracts/RUNTIME_EVENT_PUBLISHING_HOOKS_CONTRACT.zh.md`；
- `contracts/POSTGRES_SCHEMA_MIGRATION_CONTRACT.zh.md`；
- `contracts/RECOVERY_CONTRACT.zh.md`；
- `contracts/INSPECTOR_PROJECTION_CONTRACT.zh.md`；
- `contracts/PACKAGE_DEPENDENCY_AND_PORTS_CONTRACT.zh.md`；
- `contracts/EVAL_DOGFOOD_GATE_CONTRACT.zh.md`；
- D3 durable projection contract/documentation。

---

## 18. 分阶段实施

### CME0：Additive contracts 与 type ownership

实施：

- 新增最终 owner 下的 extension link、manifest/evidence semantic/projection/attribution、input
  budget、background budget、driver handle、purpose-neutral model-lifecycle companion、strict
  output、event-safe Preference payload、no-call union、immutable outbox plan、RESULT_READY
  candidate与settlement companion ports；
- 新增identity fact + frozen dataclass live prepared carriers；Request只使用
  `FrozenEventWriteCandidate`，但不新增或注册 durable EventType；
- 不修改 D3 production kind/trigger/activation registry；
- 不修改 migration、EventLog schema registry、compaction prompt或governance binding；
- 旧 production path完全不切换；旧模块只能 exact import最终 shared owner，不复制 class identity。

Gate：

```text
all non-event FrozenFact schemas registered
fingerprint golden vectors pass
TypeAlias ownership gate pass
live capability carrier constructs without arbitrary_types_allowed
prepared Request candidate remains byte-identical after source AgentEvent mutation
durable facts recursively reject mutable CandidatePayload/PooledMemoryCandidate/outbox rows
no EventType/schema-registry/migration fingerprint drift
no production composition/import behavior drift
acyclic extension/request/job/receipt/head identity probes pass
semantic fingerprints exclude source secret digest, target, quote and occurrence identity
all no-call union branches and canonical-empty evidence fingerprint validate
```

### CME1：Process-local shadow human-evidence manifest

实施：

- 从 verified transcript cursor/evidence snapshot构造process-local shadow manifest及
  sanitizer-bound full selection projections；
- 对 human/runtime-request/monitor/subagent leaf执行 closed classifier与completeness验证；
- 记录bounded live diagnostic/metrics，不写artifact、event、job、outbox或candidate；
- `_build_plan()` 与 Call A仍使用现有 16,384 events / 16 MiB bounded async source read；
- 旧 one-call candidate path仍是唯一 production candidate producer；
- 不修改任何 durable event schema或长期 replay authority。

Gate：

```text
transcript cursor/completeness/leaf exact-rebind tests
human vs runtime-request/monitor/subagent classifier tests
paged shadow manifest resident-memory bounds
page-stream selector skips oversized/non-fitting leaves and backfills older fitting leaves
selected RunStart exact reads <= 256 while inspected source pages may exceed 256 leaves
manifest semantic is independent from cursor/artifact/compaction identities
selection projection/sanitizer identity does not enter source manifest semantic
shadow failure leaves Call A and old candidate path unchanged
compaction summary behavior remains golden-equivalent
no EventType/schema-registry/migration fingerprint drift
```

### CME2：Dormant worker、settlement 与 governance builder

实施：

- 完成 memory-owned target-aware evidence selector、Call B driver、strict parser、budget
  account state machine、immutable RESULT_READY owner、result/transaction companion builder与
  exact governance builder；
- 为`llm/commit.py`安装purpose-neutral optional companion seam，并用isolated D3 model-job
  fixture验证Start/End原子CAS；production caller仍全部传`None`；
- 在isolated fixture实现manifest preparation handle与并行artifact owner；不接入production
  compaction composition；
- 以event-safe Completed payload + immutable `CandidateOutboxPlanFact`验证deterministic physical
  outbox lowering；RESULT_READY不保存physical row或current lease；
- 使用 isolated test registry/fixture驱动 dormant request，不注册 production EventType、D3
  trigger/activation或Host driver；
- RuntimeSession writer integration以test-only final candidate验证，但 production无法产生
  Request、claim job或dispatch Call B；
- 完成 process-local recovery/close/foreground-preemption状态机测试；
- 旧 one-call candidate path仍是唯一 production producer，故不存在 dual truth。

Gate：

```text
no-driver leaves PENDING without attempt churn
target token + byte budget boundary tests
session call/token/milliunit reserve/settle/reopen tests
model-job claim advances lease generation but not dispatch attempt
ModelCallStart FULL atomically advances dispatch ordinal and reserves exact quote
ModelCallEnd FULL atomically settles the same quote and terminal projection
result companion exact-reads budget settlement and has no budget-table mutation capability
Start/End NONE/UNKNOWN preserve exact owner and fault-domain latch rules
busy deferral/not-before and foreground preemption tests
provider call lifecycle/recovery tests
strict parser/security tests
atomic event+outbox+job settlement tests
outbox rows rebuild byte-identically without runtime defaults
manifest logical abandon + physical operation drain/close-blocked tests
RESULT_READY retries across physical deadlines without provider re-dispatch
RESULT_READY remains valid after job lease generation changes
MODEL_RETRY_WAIT due-claim/graceful-close transitions pass
governance exact evidence tests
close/restart/crash matrix tests
isolated PostgreSQL integration tests
production registries/composition remain byte-for-byte unchanged
null model-lifecycle companion preserves existing LLM commit golden behavior
```

### CME3：原子 production hard cut

CME3 是不可拆分、单一部署单元的 production switch：

1. 注册 Requested/ExtractionCompleted event schemas与model lifecycle vocabulary；
2. 安装 RuntimeSession Completed+Request无环link validator，以及Start/End purpose-neutral
   companion guards；
3. reset world并apply migration v9、projection activation、model-job dispatch count、RESULT_READY
   candidate/settlement owner与background budget tables；
4. 将 shadow manifest切为FULL content-addressed manifest authority；
5. summary prompt/parser切为summary-only；
6. 激活 Requested admission、D3 seeder/model-job claim、Start/End companion、driver与唯一
   RuntimeSession writer settlement；
7. governance切到exact sanitized human evidence；
8. 物理删除 old one-call candidate producer/event/parser/fixtures；
9. production composition启动时验证新旧 producer不可能同时存在。

Gate：

```text
fresh DB migrations 0000..0009 pass
nonempty v8 world returns typed reset-required
PostgreSQL + Oxigraph reset/bootstrap pass
Completed+Request FULL/NONE/UNKNOWN and no-cycle identity tests
manifest artifact preparation adds zero wait to Call A completion
manifest physical operation retains artifact/DB leases until EXITED and is drained on close
summary call output cannot contain memory block contract
one successful extraction uses two distinct model purposes
candidate never bypasses governance
result event uses RuntimeSession writer; companion cannot insert agent_events
result companion SQL cannot mutate background budget tables
stored Completed event is the sole source for deterministic outbox lowering
model-job safe-point miss does not advance dispatch attempt
terminal projection is the sole durable model-output authority
background budget exhaustion never dispatches provider
RESULT_READY settlement retry uses a new physical deadline and the same event candidate
close maintenance bypasses settlement not_before without extending close deadline
no old event/action/schema literal in production
```

### CME4：Architecture cleanup 与 operational surface

实施：

- import/AST guards；
- Inspector/CLI final projection；
- remove stale fixtures/helpers/docs；
- performance/bounds tests；
- dependency baseline downward update。

Gate：

```text
runtime/compaction -> memory concrete imports == 0
old compaction candidate symbols == 0
old memory tag literals == 0
no post-scan Completed fallback
no process-local compaction candidate owner
no Pydantic model field contains live capability Protocol
no prepared extension batch stores mutable AgentEvent
no durable event/RESULT_READY embeds CandidatePayload, PooledMemoryCandidate or physical outbox row
result settlement SQL writes to background budget relations == 0
bounded manifest paging/claim/close tests
busy-session claim churn == 0 before not-before
ordinary offline pytest full green
```

### CME5：Dogfood、contract sync 与 debt closure

实施：

- 更新全部长期 contracts；
- 更新 debt rebase 中 D5；
- 运行 frozen long compaction dogfood；
- 记录模型调用、latency、candidate/governance audit；
- 最终 DoD audit。

Gate：

```text
frozen long dogfood passes
Call A completion precedes Call B terminal result
main task can continue before Call B finishes
Call B input contains only eligible canonical human nodes
candidate producer/outbox/job receipt exact join
zero-candidate output accepted
Inspector reconstructs full chain
DoD checklist has machine-readable evidence
```

---

## 19. 测试矩阵

### 19.1 Unit contracts

- every new fact rejects caller-reported wrong fingerprint；
- manifest page/root/completeness/leaf accumulator matrix；
- extension batch ordering；
- live handle resides only in frozen dataclass and cannot enter Pydantic serialization；
- prepared intent identity exact-joins live handle object/generation；
- prepared Request uses `FrozenEventWriteCandidate`; mutating the source AgentEvent metadata/nested
  payload after freeze cannot change candidate bytes/fingerprint；
- stable request/job/target/candidate IDs；
- changing either event payload does not recurse into extension link identity；
- receipt/head one-way reference golden vector；
- Completed event candidate -> outbox plan is one-way；event payload never references plan；
- requested/admission-failed dispositions require schema_version；
- five result semantic union branches reject cross-branch nullable fields；
- no-evidence and input-unsatisfiable require the canonical-empty evidence fingerprint；
- strict JSON duplicate key、NaN、extra field、prose、oversize；
- evidence refs missing/duplicate/out-of-order；
- sanitizer redaction/rejection；
- valid empty；
- event-safe Preference payload rejects mutable/free metadata and runtime-created timestamps；
- RESULT_READY rejects `CandidatePayload`、`PooledMemoryCandidate` and physical outbox rows；
- immutable outbox plan count/accumulator/lowering golden vectors；
- candidate semantic identity cross-origin equality。

### 19.2 Compaction tests

- summary prompt has no memory instructions/tags；
- summary parser never searches memory JSON；
- previous summary feeds Call A but not Call B；
- manifest selection window is exactly the newly compacted transcript interval；
- extension disabled still compacts；
- extension factory failure still commits Completed；
- configured target/manifest/request failure always produces typed admission_failed；
- disabled/not-applicable is the only path allowed to produce no disposition；
- manifest artifact PREPARING at Call A completion is inspected without await and yields
  `manifest_not_ready_at_completion`；
- `consume_full_or_abandon()` closes the FULL-vs-ABANDONED race atomically；
- late manifest FULL after abandon releases its pin and never backfills Requested；
- logical ABANDONED retains physical dependency leases until operation EXITED；
- Completed+Request batch NONE retries exact candidates；
- publication hint failure does not lose durable job。

### 19.3 Source evidence tests

- direct human input included；
- runtime request excluded；
- subagent task excluded；
- assistant/tool/runtime observation excluded；
- mixed human+monitor ingress only includes human branch；
- secret redaction does not leak original text to artifact；
- distinct original secrets that sanitize to the same complete message produce the same evidence
  semantic fingerprint and different attribution fingerprints；
- long manifest pages without whole-window resident load；
- selection projection stores full sanitized inline text or exact content reference and never enters
  source semantic identity；
- target estimator runs from sanitizer-bound projection before RunStart hydration；
- oversized/non-fitting newer leaves are skipped while older fitting leaves can backfill；
- page scan may inspect the full source window but exact RunStart reads never exceed 256；
- over-bound history yields deterministic selected suffix + permanent omission receipt；
- one overlong human message is omitted as a whole and never lowered as `head_tail`；
- governance quote references a whole sanitized node and never claims an original span；
- changing model target or resolved context limits does not change input semantic when the selected
  full evidence messages are identical；
- physical compaction/request/job/model identities do not change evidence/governance semantic fingerprints。

### 19.4 Model lifecycle tests

- no driver means no claim；
- model-job claim advances lease generation only；dispatch attempt remains unchanged；
- active Host run blocks background admission；
- busy driver defers with bounded not-before and no claim churn；
- human ingress after ModelCallStart proceeds immediately and preempts background；
- caller cancellation detaches；
- completed ModelCallEnd prevents second provider call；
- dangling Start uses recovery；
- provider error creates retry attempt；
- parser failure does not rerun Call A；
- ModelCallStart NONE does not change call identity or dispatch ordinal；
- ModelCallStart FULL atomically advances one ordinal and reserves the exact
  `ModelCallReservationQuoteFact`；
- ModelCallEnd FULL atomically settles that reservation with terminal projection/usage；
- Start/End UNKNOWN installs only RuntimeSession ledger reconciliation；
- terminal projection is the sole durable raw model-output authority；
- completed terminal projection reparses deterministically without another provider call；
- RESULT_READY survives writer deadline exhaustion and retries under a new settlement generation
  with byte-identical event candidate；
- lease heartbeat prevents concurrent claim；
- target token budget and byte cap both gate dispatch；
- session call/input-token/output-token/milliunit exhaustion produces the typed no-call result；
- open reserve + settled charge recurrence holds at every account revision；
- cancelled/preempted/missing-usage/over-quote settlement matrix is exact；
- budget reservation is atomically joined with ModelCallStart and settled with ModelCallEnd；
- no-call/result settlement performs zero background-account writes；
- result companion rejects missing or mismatched pre-existing model budget settlement；
- MODEL_RETRY_WAIT due claim is one CAS and graceful close supersedes it；
- settlement close maintenance bypasses not_before under the unchanged close deadline。

### 19.5 Settlement crash matrix

至少覆盖：

```text
crash before Request batch
crash while manifest artifact is PREPARING and Call A completes
crash after manifest artifact FULL, before Call A terminal batch
crash after Completed+Request FULL, before seed
crash after job insert, before claim
crash after input artifact FULL, before ModelCallStart
crash during ModelCallStart + background budget reservation: NONE/UNKNOWN
crash after ModelCallStart FULL, before dispatch
crash after provider return, before ModelCallEnd
crash after ModelCallEnd + terminal projection FULL, before deterministic parse
crash during ModelCallEnd + budget settlement: NONE/UNKNOWN
crash after deterministic parse, before RESULT_READY candidate install
crash after RESULT_READY install, then reclaim with a different job lease generation
crash after RESULT_READY, before first settlement write generation
settlement physical deadline expires, then reopen retries same RESULT_READY with new generation
crash during result transaction: NONE
crash during result transaction: FULL unknown to caller
result conflict
outbox pending after job success
candidate pool projection retry
governance retry
```

### 19.6 Close/reopen

- close before claim -> typed supersede；
- close while waiting safe point；
- close during provider call；
- close while manifest physical write is blocked；
- manifest physical exit is required before artifact/DB lease release；
- close deadline exhausted -> close blocked；
- process restart reclaims expired lease；
- session reopen registers new driver generation；
- old driver facade fails closed；
- unresolved settlement prevents new provider call。
- RESULT_READY/SETTLEMENT_RETRY_WAIT reopen never re-dispatches provider；
- RESULT_READY never compares against the current job lease fingerprint；
- manifest/source/handler latch does not block foreground RuntimeSession work；
- only agent-event transaction UNKNOWN/PARTIAL installs RuntimeSession ledger latch。

### 19.7 Architecture tests

AST/rg gate 至少禁止：

```text
runtime/compaction importing memory.candidates
runtime/compaction importing memory.governance
runtime/compaction importing ontology.memory
llm/commit importing memory.compaction
live Protocol capability field inside a Pydantic BaseModel
PreparedCompactionPostCompletionExtensionBatch.request_event: AgentEvent
CompactionMemoryExtractionResultCandidateFact.ordered_candidate_outbox_rows
CompactionMemoryExtractionResultCandidateFact.expected_job_lease_fingerprint
CompactionMemoryExtractionCandidateAttributionFact.candidate_payload: CandidatePayload
result settlement companion INSERT/UPDATE/DELETE background budget relations
<memory_candidates_json>
ContextCompactionMemoryCandidatesProposedEvent
CompactionCandidateProjectionReceipt
parse_compaction_memory_candidates
post-scan of ContextCompactionCompletedEvent for candidate recovery
```

---

## 20. Frozen real-LLM dogfood

### 20.1 用例

保留一个 long compaction dogfood，不恢复大量旧 real-LLM pytest：

1. 用户在早期明确表达 2 个长期 preference；
2. 中间包含 assistant 推断、tool output、runtime observation 与 recalled memory 干扰项；
3. 轨迹增长到触发真实 compaction；
4. Call A 生成 continuity summary；
5. 主任务继续并完成下一步；
6. Call B 在 safe point 异步执行；
7. candidate 进入 pool；
8. governance 对 exact human evidence 作出决定。

### 20.2 必须记录

- Call A/Call B resolved model purpose/target/call ID；
- Requested/Completed/job/lease/result receipt IDs；
- Call A input/output token；
- Call B input/output token；
- resolved input token budget与physical byte cap；
- background budget reservation/charge/account revision；
- Call A wall latency；
- Call B queue delay与wall latency；
- evidence node count/bytes/omission；
- manifest root/completeness proof与permanent omission accumulator；
- candidate count/semantic fingerprints；
- governance outcome；
- provider reported cache fields，仅作为观察，不作为 pass gate。

### 20.3 Pass criteria

Dogfood 不以“必须产生固定数量候选”作为唯一 gate，因为真实模型可合法返回 zero。它必须证明：

- 两个调用职责与 input 隔离；
- Call A 不等待 Call B；
- Call B 不读取 summary/recalled memory/tool output；
- 输出符合 strict schema；
- 显式 preference 场景至少有一个 run 能产生并治理 candidate；
- 所有 durable refs 可由 Inspector exact join；
- 无旧 producer/fallback。

---

## 21. Inspector 与 CLI

Inspector 必须展示：

```text
compaction
  human-evidence manifest / completeness proof
  summary call / artifact
  extension disposition
  extraction request
  seed checkpoint / job
  driver availability
  lease / attempt / retry / dead-letter
  model call Start/End/terminal projection
  input artifact / terminal projection / parsed result semantic
  extraction completed result
  candidate outbox
  candidate pool projection
  governance decision / memory write outcome
```

状态必须区分：

- `not_configured`；
- `admission_failed`；
- `pending_no_runtime_binding`；
- `pending_safe_point`；
- `deferred_busy_not_before`；
- `leased_model_call`；
- `preempted_by_foreground`；
- `input_budget_unsatisfiable`；
- `background_budget_exhausted`；
- `retry_wait`；
- `result_ready`；
- `settlement_writing`；
- `settlement_retry_wait`；
- `dead_letter`；
- `result_full_outbox_pending`；
- `candidate_projected`；
- `governance_pending`；
- `governed_no_write`；
- `governed_write`；
- `job_or_target_reconciliation_required`；
- `background_account_reconciliation_required`；
- `runtime_session_ledger_reconciliation_required`。

CLI 可以提供：

- list/show；
- retry dead-letter extraction job；
- abandon/supersede pending job；
- inspect exact evidence chain。

CLI 禁止直接把 extraction result 标记为 durable memory。

---

## 22. Definition of Done

D5 只有在以下全部满足时才能标记 `CLOSED`。

机器可读证据：`benchmarks/suites/core/v1/cme5_dod_evidence.json`。该记录绑定
frozen suite/scenario/runner contract fingerprint，并记录本轮全量测试、失败节点定向闭环、
PostgreSQL/Oxigraph 集成与 long real-LLM dogfood 结果。依照本轮测试约束，全量 pytest 只运行
一次；其中 5 个失败在修复后逐节点复跑为 5/5 green，后续 broad affected matrix 的唯一失败也
通过 exact-node 复跑闭环，没有第二次重跑整套 pytest。

### 22.1 调用边界

- [x] Call A prompt/output contract 只包含 summary；
- [x] Call B 使用独立 purpose/request/lane/target；
- [x] Call B 不阻塞 Call A completion；
- [x] manifest artifact preparation与Call A并行，completion point不等待pending artifact I/O；
- [x] Call B 不创建 Host run ingress；
- [x] foreground human ingress不等待active Call B provider timeout；
- [x] disabled/no-evidence 路径不产生不必要 provider call。
- [x] 五类result outcome均由closed discriminated union无歧义编码。

### 22.2 Durable authority

- [x] Completed 与 Requested 同批 FULL；
- [x] live prepared carriers为frozen dataclass，fingerprinted identity与capability object彻底分离；
- [x] Request在prepared batch中仅以`FrozenEventWriteCandidate`存在；
- [x] Completed/Request link、Request/job与receipt/head identity均无递归 fingerprint；
- [x] Requested 是唯一 job trigger；
- [x] source horizon 等于 Requested sequence；
- [x] no post-scan recovery；
- [x] job lease/retry/dead-letter/reconciliation完整；
- [x] Request-derived model retry固定maximum attempts 3、1s/2s/4s、无jitter，live/recovery共用factory；
- [x] no-driver 不消耗 attempt；
- [x] busy/safe-point miss使用bounded not-before且不形成claim churn；
- [x] model-job claim只推进lease generation；
- [x] ModelCallStart同事务推进dispatch ordinal并reserve exact existing quote；
- [x] ModelCallEnd同事务settle exact reservation与terminal projection；
- [x] provider completed result不会重复调用；
- [x] terminal projection是唯一durable raw model-output authority；
- [x] RESULT_READY保存stable event candidate并可跨physical deadline/generation重试；
- [x] RESULT_READY只保存event-safe candidate + immutable outbox plan，不保存physical rows/current lease；
- [x] result event仅由RuntimeSession writer追加；
- [x] transaction companion禁止自行写agent_events；
- [x] result event/outbox/receipt/head/job success 同事务；
- [x] receipt保存head revision，head单向引用receipt；
- [x] session call/input-token/output-token/milliunit budget公式、reserve/settle/recovery完整；
- [x] Start/terminal companion分别是唯一budget reserve/settle owner；result companion对budget表只读；
- [x] source/target、background-account与RuntimeSession-ledger latch fault domain严格隔离。

### 22.3 Evidence 与治理

- [x] Call B 只消费 exact direct human evidence；
- [x] manifest completeness证明window内全部eligible human leaves；
- [x] manifest selection projection绑定sanitizer contract且不进入source semantic；
- [x] selector可stream全部page并跳过不合适leaf，selected RunStart exact-read最多256条；
- [x] target-aware token与byte budget共同约束Call B input；
- [x] 超过256 nodes的永久automatic omission在receipt/Inspector可见；
- [x] 单条超长message整条省略，V1不存在`head_tail` projection；
- [x] previous/current summary 均不是 evidence；
- [x] recalled memory/working context/tool output均排除；
- [x] sanitizer contract与测试全绿；
- [x] V1 仅产生 Preference；
- [x] model 无权设置 scope/authority/verification/ID；
- [x] candidate只进入 pending pool；
- [x] governance使用 canonical human evidence；
- [x] governance只引用完整sanitized message node，不声称original span；
- [x] redaction match secret/digest不进入semantic，等价sanitized全文跨origin同identity；
- [x] resolved target/budget只进入attribution，不改变input semantic；
- [x] semantic fingerprint不包含compaction/request/job/model/artifact/ordinal attribution；
- [x] shared semantic fingerprint 支持 cross-producer dedupe。
- [x] governance exact duplicate仅比较shared semantic；casefold/whitespace collapse只用于discovery。

### 22.4 Ownership 与依赖

- [x] `runtime/compaction` 对 memory concrete import 为零；
- [x] compaction policy/service不再拥有 candidate sink/outbox/parser；
- [x] process-local compaction candidate owner为零；
- [x] durable event/RESULT_READY中不存在mutable candidate DTO、metadata dict或runtime timestamp default；
- [x] outbox rows仅从stored Completed event按immutable plan确定性lower；
- [x] memory extension 通过 low-level port组合；
- [x] LLM commit只消费purpose-neutral companion，不import/downcast memory/job/account DTO；
- [x] driver borrow/release/close fail-closed；
- [x] manifest logical abandon与physical exit分离，close在释放artifact/DB前drain全部operation；
- [x] D4 dependency gate无增长并向下收缩。

### 22.5 Migration 与恢复

- [x] migration 0009 immutable registry/catalog/grants 全绿；
- [x] nonempty v8 world typed reset-required；
- [x] fresh PostgreSQL/Oxigraph world bootstrap全绿；
- [x] session bootstrap写入完整新 kind cutover；
- [x] session bootstrap原子创建background-derived-work budget account；
- [x] migration 0009为model-job dispatch count、RESULT_READY与settlement-generation提供完整shape；
- [x] crash matrix全绿；
- [x] close/reopen matrix全绿；
- [x] MODEL_RETRY_WAIT due-claim/close与SETTLEMENT_RETRY_WAIT close-maintenance transitions全绿；
- [x] Inspector exact reconstruction全绿。

### 22.6 清理与验证

- [x] old event/DTO/parser/prompt tag physical deletion；
- [x] no deprecated alias/historical fallback；
- [x] ordinary full pytest按本轮约束完成一次全量与失败节点定向闭环；
- [x] targeted PostgreSQL/Oxigraph integration green；
- [x] frozen long real-LLM dogfood green；
- [x] 长期 contracts同步；
- [x] debt document D5只在以上全部完成后改为 `CLOSED`。

---

## 23. 最终不变量

实施完成后，以下陈述必须同时为真：

```text
Compaction summary is continuity, not memory evidence.

Memory extraction is optional derived work, not compaction correctness.

No optional manifest artifact I/O delays Call A completion.

No manifest physical operation outlives the artifact or database leases it borrowed.

Every extraction has one durable Requested authority.

Every candidate cites exact canonical human evidence.

No physical occurrence identity changes evidence or governance semantic identity.

No background extraction can exceed its target input or session cost budget.

Every dispatched attempt atomically advances its ordinal and reserves its exact quote.

Only the model terminal companion settles a background budget reservation.

No background extraction can hold foreground human ingress hostage.

No model output becomes durable memory without governance.

No crash after compaction completion can silently erase an admitted extraction.

No extraction retry can rerun an already durably completed model call.

The terminal projection is the sole durable raw extraction-output authority.

No settlement retry can change its RESULT_READY candidate or call the provider again.

No durable RESULT_READY authority embeds a live handle, mutable candidate, physical row, or current lease.

Every candidate outbox row is deterministically lowered from the stored Completed event.

No memory schema change requires runtime/compaction to understand memory DTOs.
```

这就是 D5 的完成边界。
