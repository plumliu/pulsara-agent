# Pulsara Governance Source Evidence Hard Cut 实施规格

> 状态：GSE0-GSE5 已完成
>
> 日期：2026-07-18
>
> 前置：
> `PULSARA_AUTHORITY_MATERIALIZATION_AND_LOSSLESS_TRANSCRIPT_PROJECTION_DESIGN.zh.md`
>
> 前置：
> `PULSARA_CONTEXT_EVIDENCE_CURSOR_PERFORMANCE_OPTIMIZATION_IMPLEMENTATION.zh.md`
>
> 前置：
> `PULSARA_MODEL_STREAM_DELTA_SEGMENT_COALESCING_HARD_CUT_IMPLEMENTATION.zh.md`
>
> 长期契约：
> `contracts/GOVERNANCE_WRITE_OUTBOX_CONTRACT.zh.md`
>
> 长期契约：
> `contracts/MEMORY_SURFACES_CONTRACT.zh.md`

---

## 实施结果（2026-07-18）

GSE0-GSE5 已按本文档完成一次性 production hard cut，生产治理输入不再读取或猜测
raw model-stream events。主要落点如下：

- 新增 frozen governance evidence、batch input、claim、producer outbox 与 recovery DTO；
- main-agent memory tool 与 replay evidence 共用唯一 versioned candidate builder；
- governance evidence 从 transcript reducer 的同锁 frozen snapshot 派生，只接纳
  `ACCEPTED` terminal projection、完成 pairing 的 tool result 与已验证 canonical quote；
- reflection 与 compaction producer 使用 event/account/outbox 原子 bundle，candidate row
  仅由 session-owned dispatcher 幂等投影；`NONE` 重试同一 candidate，`UNKNOWN` 精确
  confirm，caller cancellation 只 detach，Host close drain producer owner；
- governance candidate 使用 durable all-or-none claim；Prepared artifact 冻结 exact
  `ResolvedModelCall`、system prompt、ordered messages、context ID 与 canonical
  `LLMContext`，recovery 不读取 current model configuration 重新解释输入；
- related canonical memory 冻结 `node_revision`；executor 在 UOW 内锁定 canonical row
  并校验 revision，漂移时不执行旧 supersede/contradiction mutation；
- Inspector、EventLog schema、PostgreSQL projection、长期 contracts 与 architecture
  guards 已同步；`_source_event_summaries()`、raw `source_events` facade 和 governance
  production full-run scan 已删除。

最终验证：

- `uv run pytest -q`：`2246 passed, 77 skipped`；
- governance architecture guards：`31 passed`；
- full real-LLM marker suite：`53 passed, 21 skipped`；其中skip均由独立昂贵
  dogfood开关控制；
- real embedding/reranker relatedness fixture：`recall@k = 1.0`；
- `uv run ruff check src tests evals`：通过；
- `git diff --check`：通过。

本文后续章节继续保留冻结的设计、迁移顺序与验收矩阵，作为实现与长期审计的
规范来源；其中“完成定义”均按上述 production hard cut 落地，不再表示待办。

---

## 0. 最终结论

当前 `MemoryGovernanceEngine._source_event_summaries()` 不是 durable authority，也不是稳定的领域投影。它执行以下弱逻辑：

```text
candidate.source_run_id
    -> EventLog.iter(run_id=...)
    -> 取前 80 个 event
    -> 猜测 tool_call_id / delta / message / status 等属性
    -> list[dict[str, Any]]
    -> governance prompt
```

该路径同时存在四类问题：

1. 对每个候选扫描完整 run ledger；
2. 前 80 条截断与候选真实来源没有稳定关系；
3. model-stream segment hard cut 后已没有旧 `delta` 真源；
4. completed 但被 control disposition suppress 的模型输出仍可能进入 governance prompt。

本阶段执行一次性 hard cut：

```text
ImmutableGovernanceCandidateSnapshotFact
        |
        +-> GovernanceSourceEvidenceSemanticFact
        |      `- 只覆盖候选相关的业务语义
        |
        +-> GovernanceSourceEvidenceAttributionFact
        |      `- durable event/artifact/high-water/producer refs
        |
        `-> GovernanceEvidencePromptProjectionFact
               `- 本次 Flash model 实际可见的有界投影

GovernanceBatchInputSnapshotFact
        +-> durable candidate claims
        +-> ordered candidate snapshots
        +-> ordered source evidence
        +-> relatedness snapshots
        +-> allowed scopes
        +-> source high-water
        +-> prompt projection policy
        +-> exact ResolvedModelCall + canonical LLMContext
        `-> exact batch input fingerprint
```

Reflection与compaction candidate均由producer event/account transition/outbox同transaction投影；
governance batch在准备artifact前对candidate执行durable all-or-none claim。Prepared
在exact target/call、system prompt、ordered messages与context ID冻结后才可FULL，
因此recovery不需要也不允许从current configuration重新resolve。

必须删除：

- `_source_event_summaries()`；
- `_candidate_snapshot()` 中的 raw `source_events`；
- governance 对 `event_log.iter(run_id=...)` 的生产调用；
- governance 对 `delta`、segment content 或任意 event attribute 的反射式猜测；
- prompt 中旧 `source_events` 自由 JSON 口径；
- `CandidateOrigin.GOVERNANCE` 再次进入 governance LLM 的任何路径。

本阶段不改变以下 authority 边界：

- EventLog 仍是 runtime truth；
- transcript projection/checkpoint 仍是 EventLog reducer 的可重建 memoization；
- candidate pool 仍是 proposal inbox，不是 governed memory；
- canonical memory 仍只能由 `MemoryGovernanceExecutor` + UOW 写入；
- runtime semantic graph 不因本阶段获得 governed-memory 写权限；
- model-stream segment 仍属于 `non_transcript`，不进入 governance evidence semantic identity。

---

## 1. 为什么不是“terminal projection + disposition”两个对象直接拼起来

### 1.1 它们是必要条件，但不是完整证据

对 `MAIN_AGENT_TOOL` 候选，合法来源链必须是：

```text
canonical current-user message
    -> completed ModelCallTerminalProjectionCommittedEvent
    -> ModelCallControlDispositionResolvedEvent(ACCEPTED)
    -> exact memory tool-call semantic
    -> paired ToolResultTerminalProjectionCommittedEvent
    -> candidate-pool proposal
```

`terminal projection + disposition` 能证明模型输出完成且被控制面接纳，但单独使用它们仍不能证明：

- `candidate.user_quote` 确实来自 canonical user message；
- memory tool call 已经执行并得到 terminal result；
- tool call 与 tool result 的 pairing 完整；
- 候选来自哪一个 accepted tool call，而不是同一 reply 中另一个调用；
- model-visible evidence 使用了什么有界 projection policy。

### 1.2 唯一正确的复用点是 transcript reducer

现有 `TranscriptProjectionStateStore` 已经实现：

- non-completed model projection 只作 audit；
- completed projection 等待 disposition；
- 只有 `ACCEPTED` 才进入 stable transcript；
- accepted tool-call 必须等待 matching terminal tool result；
- call/result identity、tool name、semantic fingerprint 与 pairing order 强校验；
- suppressed projection 不进入 stable transcript。

因此 governance 不得重新实现一套 projection/disposition/pairing reducer。它必须从同一个 reducer 的 frozen evidence snapshot 选择 candidate-scoped entries，并额外保留 disposition durable ref。

### 1.3 segment identity 只能进入 attribution

完整 `TerminalProjectionReferenceFact` 会绑定：

- terminal document fact fingerprint；
- `ModelCallSemanticSourceFact`；
- segment policy；
- durable semantic event count；
- settlement measurement；
- artifact placement。

这些字段会随 segment layout、checkpoint schedule 或物理写入策略变化。它们不能进入 governance evidence semantic fingerprint。

冻结规则：

```text
same accepted candidate payload
+ same selected model tool-call semantic
+ same canonical quoted evidence
+ same tool-result semantic/timing
=> same GovernanceSourceEvidenceSemanticFact.semantic_fingerprint

event IDs / sequence / artifact IDs / terminal document refs / source layout changed
=> attribution fact may change
=> semantic fingerprint must not change
```

---

## 2. Authority 分层

### 2.1 Semantic identity

Semantic identity 回答：

> governance 在业务语义上看到了什么证据？

它只允许覆盖：

- candidate payload semantic fingerprint；
- candidate origin；
- accepted marker；
- selected memory tool-call semantic identity；
- canonical tool-result semantic identity；
- tool observation timing；
- verified quoted-evidence semantic identity；
- compaction extractor semantic contract与解析结果；
- reflection-reported evidence semantic identity。

它禁止覆盖：

- event ID / sequence；
- ledger high-water；
- terminal document/ref fact fingerprint；
- artifact ID；
- segment count / source-item count；
- segment policy / settlement measurement；
- checkpoint ID / tree layout；
- process-local object identity；
- prompt truncation layout。

### 2.2 Durable attribution

Attribution 回答：

> 这份 semantic evidence 由哪一组 durable facts 证明？

它必须覆盖：

- runtime session/run/turn/reply identity；
- reducer authority high-water；
- candidate row identity与origin；
- terminal projection committed event ref；
- disposition event ref；
- tool-result projection committed event ref；
- terminal document reference；
- canonical user message entry ref与character span；
- reflection/compaction producer carrier；
- artifact ref、content hash与producer contract；
- fact fingerprint。

### 2.3 Model-visible prompt projection

Prompt projection 回答：

> Flash governance model 本次实际看到了 semantic evidence 的哪一部分？

它是 invocation-scoped、content-addressed、bounded 的 projection，不是完整 authority。

它必须：

- 引用完整 evidence semantic fingerprint；
- 冻结 projection policy ID/version/fingerprint；
- 保存实际 included/omitted fields与reason；
- 保存实际 model-visible candidate JSON；
- 计算独立 projection fingerprint；
- 不改变完整 evidence semantic identity。

### 2.4 Batch input durable truth

Governance batch 必须拥有唯一 durable input snapshot。不得仅依赖模型调用临时 `LLMContext.messages`。

```text
GovernanceBatchInputSnapshotFact
    -> content-addressed artifact
    -> MemoryGovernanceBatchPreparedEvent
    -> ModelCallStartEvent governance binding
    -> MemoryGovernanceDecisionRecord
    -> governance write/outbox events
```

所有箭头必须精确 join 同一个 `batch_input_fingerprint`。

---

## 3. 公共 frozen DTO 规则

### 3.1 基座

所有 durable facts：

```python
class GovernanceEvidenceFrozenFact(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
```

统一规则：

- 每层 DTO 只有一个自身 fingerprint；
- nested semantic fingerprint 在外层只作 equality join，不是外层自身 fingerprint；
- canonical JSON 使用 `ensure_ascii=False`、sorted keys、compact separators、`allow_nan=False`；
- 所有 tuple 有 schema-level 数量上限；
- 所有 string 有 characters 或 UTF-8 bytes 上限；
- 所有governance event ref统一使用
  `GovernanceStoredEventReferenceFact`，不接受bare event ID、
  `StableEventIdentityFact`或信息不完整的`ContextEventReferenceFact`；
- 所有 artifact ref 同时校验 artifact ID、content hash、byte count、media type与contract fingerprint；
- 所有 union 使用 discriminator；
- 所有 fingerprint 只能由唯一 factory 构造并由 validator重算。

### 3.2 Fingerprint domain separators

冻结以下 domain separators：

```text
governance-candidate-payload-semantic:v1
governance-quoted-evidence-semantic:v1
governance-quoted-evidence-attribution:v1
governance-main-tool-source-semantic:v1
governance-reflection-source-semantic:v1
governance-compaction-source-semantic:v1
governance-source-attribution:v1
governance-evidence-prompt-projection:v1
governance-immutable-candidate-snapshot:v1
governance-relatedness-snapshot:v1
governance-batch-input-snapshot:v1
governance-batch-input-reference:v1
governance-stored-event-reference:v1
governance-evidence-artifact-reference:v1
transcript-projection-leaf-entry-reference:v1
governance-batch-input-artifact-contract:v1
governance-model-input:v1
main-agent-memory-candidate-builder-contract:v1
```

不得让调用方自由传入 domain separator。

### 3.3 中央 reference、artifact 与 reason DTO

`ContextEventReferenceFact`当前虽有`sequence`，但没有
`event_schema_version/event_schema_fingerprint/stored_envelope_fingerprint`，不足以独立验证
historical envelope。Governance 不允许再使用只有 event ID 或
`StableEventIdentityFact`的 reference，统一使用：

```python
class GovernanceStoredEventReferenceFact(GovernanceEvidenceFrozenFact):
    schema_version: Literal["governance_stored_event_reference.v1"]
    stable_identity: StableEventIdentityFact
    sequence: int = Field(ge=1)
    stored_envelope_fingerprint: str
    reference_fingerprint: str
```

`reference_fingerprint`由唯一 factory 覆盖 complete stable identity、sequence 与
stored-envelope fingerprint。Hydrator必须按 exact event ID 读取，重算
stored envelope并验证`sequence <= authority_ledger_through_sequence`。该类是
`ContextEventReferenceFact`的 schema-complete hard cut；本阶段不修改旧类以免影响
Stage 3/4 其他 consumer。

```python
class GovernanceEvidenceArtifactReferenceFact(GovernanceEvidenceFrozenFact):
    schema_version: Literal["governance_evidence_artifact_reference.v1"]
    artifact_kind: Literal[
        "governance_batch_input",
        "terminal_projection",
        "compaction_summary",
        "quoted_evidence",
        "tool_result",
        "related_memory_content",
    ]
    artifact_id: str = Field(min_length=1, max_length=256)
    media_type: str = Field(min_length=1, max_length=128)
    content_sha256: str
    content_bytes: int = Field(ge=0, le=16 * 1024 * 1024)
    artifact_contract_id: str = Field(min_length=1, max_length=128)
    artifact_contract_version: str = Field(min_length=1, max_length=64)
    artifact_contract_fingerprint: str
    reference_fingerprint: str


class TranscriptProjectionLeafEntryReferenceFact(
    GovernanceEvidenceFrozenFact
):
    schema_version: Literal["transcript_projection_leaf_entry_reference.v1"]
    runtime_session_id: str
    entry_kind: Literal["message", "tool_pair", "tool_result"]
    ordinal: int = Field(ge=0)
    entry_semantic_fingerprint: str
    entry_fact_fingerprint: str
    source_event_references: tuple[GovernanceStoredEventReferenceFact, ...]
    reference_fingerprint: str
```

Leaf reference的`source_event_references`必须 ordered、non-empty、同 session且每项小于
snapshot high-water。它是 frozen transcript tree 中的 locator，不是 caller 自报的
message ID。

```python
class GovernanceBatchInputArtifactContractFact(
    GovernanceEvidenceFrozenFact
):
    schema_version: Literal["governance_batch_input_artifact_contract.v1"]
    contract_id: str
    contract_version: str
    document_schema_fingerprint: str
    canonicalization_contract_fingerprint: str
    media_type: Literal["application/vnd.pulsara.governance-batch-input+json"]
    max_artifact_utf8_bytes: int = Field(ge=1, le=2 * 1024 * 1024)
    contract_fingerprint: str
```

`GovernanceBatchInputReferenceFact.artifact_contract_fingerprint`必须精确等于该
declarative contract的 fingerprint；artifact writer、reader、Prepared validator、recovery
与 Inspector必须从 composition-root registry 按 ID/version/fingerprint 精确 rebind。
V1 production contract将`max_artifact_utf8_bytes`冻结为`1 MiB`；该上限由第 13.1
节的maximal-input doctor与candidate/relatedness tuple bounds交叉证明可行。

```python
class GovernanceEvidenceBuildReason(StrEnum):
    FULL_MAIN_TOOL_JOIN = "full_main_tool_join"
    FULL_REFLECTION_JOIN = "full_reflection_join"
    FULL_COMPACTION_JOIN = "full_compaction_join"
    WAIT_REDUCER_BEHIND = "wait_reducer_behind"
    WAIT_PROJECTION_OUTBOX = "wait_projection_outbox"
    WAIT_ARTIFACT_CONFIRMATION = "wait_artifact_confirmation"
    INVALID_SOURCE_CALL_MISSING = "invalid_source_call_missing"
    INVALID_TERMINAL_RUN_WITHOUT_PAIR = "invalid_terminal_run_without_pair"
    INVALID_CANDIDATE_PAYLOAD_MISMATCH = "invalid_candidate_payload_mismatch"
    INVALID_PRODUCER_OMITS_CANDIDATE = "invalid_producer_omits_candidate"
    INVALID_RAW_CANDIDATE_INDEX = "invalid_raw_candidate_index"
    INVALID_ORIGIN_FIELDS = "invalid_origin_fields"
    UNTRUSTED_ARTIFACT_HASH = "untrusted_artifact_hash"
    UNTRUSTED_REDUCER_EVENT_MISMATCH = "untrusted_reducer_event_mismatch"
    UNTRUSTED_DECODER_BINDING = "untrusted_decoder_binding"
    UNTRUSTED_ID_PAYLOAD_CONFLICT = "untrusted_id_payload_conflict"
    NOT_APPLICABLE_AUDIT_ORIGIN = "not_applicable_audit_origin"


class CandidateEvidenceRejectionReason(StrEnum):
    SOURCE_CALL_MISSING = "source_call_missing"
    TERMINAL_RUN_WITHOUT_PAIR = "terminal_run_without_pair"
    CANDIDATE_PAYLOAD_MISMATCH = "candidate_payload_mismatch"
    PRODUCER_OMITS_CANDIDATE = "producer_omits_candidate"
    RAW_CANDIDATE_INDEX_MISSING = "raw_candidate_index_missing"
    ORIGIN_FIELDS_INVALID = "origin_fields_invalid"
```

`CandidateEvidenceRejectionReason`只允许 candidate-local invalid 分支，不包含任何
authority-corruption code。两个 enum 由 schema validator 执行 status/reason matrix，不接受
自由字符串或运行时注册未知 code。

---

## 4. Candidate semantic snapshot

```python
class GovernanceCandidatePayloadSemanticFact(GovernanceEvidenceFrozenFact):
    schema_version: Literal["governance_candidate_payload_semantic.v1"]
    candidate_origin: Literal[
        "main_agent_tool",
        "reflection",
        "compaction",
    ]
    payload_kind: str
    canonical_candidate_payload: CandidatePayload
    canonical_payload_utf8_bytes: int = Field(ge=1, le=16 * 1024)
    intent_fingerprint: str | None
    payload_semantic_fingerprint: str


class GovernanceCandidateAttributionFact(GovernanceEvidenceFrozenFact):
    schema_version: Literal["governance_candidate_attribution.v1"]
    entry_id: str
    runtime_session_id: str
    source_run_id: str
    source_turn_id: str
    source_reply_id: str
    source_tool_call_id: str | None
    source_event_reference: GovernanceStoredEventReferenceFact | None
    source_artifact_reference: GovernanceEvidenceArtifactReferenceFact | None
    quoted_evidence_locator: CandidateQuotedEvidenceLocatorFact | None
    created_at_utc: str
    attribution_fingerprint: str
```

```python
class CandidateQuotedEvidenceLocatorFact(GovernanceEvidenceFrozenFact):
    schema_version: Literal["candidate_quoted_evidence_locator.v1"]
    locator_kind: Literal[
        "canonical_user_message_span",
        "reflection_quote_index",
        "compaction_summary_span",
    ]
    source_message_id: str | None
    source_event_reference: GovernanceStoredEventReferenceFact | None
    source_artifact_reference: GovernanceEvidenceArtifactReferenceFact | None
    source_quote_index: int | None
    start_char: int | None
    end_char: int | None
    quoted_text_sha256: str
    locator_fingerprint: str
```

Invariant：

- `candidate_origin="main_agent_tool"` 必须有 `source_tool_call_id`；
- `main_agent_tool`若携带replacement/supersede quote，locator必须是`canonical_user_message_span`；
- `candidate_origin="reflection"` 必须有 reflection carrier identity；
- `candidate_origin="compaction"` 必须有 completed event与summary artifact；
- `CandidateOrigin.GOVERNANCE` 不属于该 union；
- candidate row的payload canonicalization必须与semantic fact逐字段相等；
- `canonical_payload_utf8_bytes`必须从typed payload canonical bytes重算，超限candidate
  在producer schema边界被拒绝，不能留给governance prompt临时截断；
- `intent_fingerprint`的nullable规则沿用origin producer contract，不允许builder临时推断。

```python
class ImmutableGovernanceCandidateSnapshotFact(GovernanceEvidenceFrozenFact):
    schema_version: Literal["immutable_governance_candidate_snapshot.v1"]
    payload_semantic: GovernanceCandidatePayloadSemanticFact
    candidate_attribution: GovernanceCandidateAttributionFact
    source_evidence_semantic: GovernanceSourceEvidenceSemanticFact
    source_evidence_attribution: GovernanceSourceEvidenceAttributionFact
    prompt_projection: GovernanceEvidencePromptProjectionFact
    candidate_snapshot_fingerprint: str
```

`candidate_snapshot_fingerprint`覆盖五个nested fact fingerprints，但不替代其中任何一层的独立validator。

---

## 5. Quoted evidence

### 5.1 Semantic fact

```python
class GovernanceQuotedEvidenceSemanticFact(GovernanceEvidenceFrozenFact):
    schema_version: Literal["governance_quoted_evidence_semantic.v1"]
    quote_kind: Literal[
        "canonical_user_span",
        "reflection_reported",
        "compaction_summary_span",
    ]
    text: str = Field(max_length=16_384)
    text_utf8_bytes: int = Field(ge=0, le=64 * 1024)
    text_sha256: str
    verification_status: Literal["canonical_match", "origin_reported"]
    semantic_fingerprint: str
```

### 5.2 Attribution fact

```python
class GovernanceQuotedEvidenceAttributionFact(GovernanceEvidenceFrozenFact):
    schema_version: Literal["governance_quoted_evidence_attribution.v1"]
    quote_semantic_fingerprint: str
    source_entry_ref: TranscriptProjectionLeafEntryReferenceFact | None
    source_artifact_ref: GovernanceEvidenceArtifactReferenceFact | None
    start_char: int | None
    end_char: int | None
    producer_event_reference: GovernanceStoredEventReferenceFact | None
    attribution_fingerprint: str
```

### 5.3 Canonical matching

`MAIN_AGENT_TOOL` 的用户 quote 必须按以下唯一算法验证：

1. 从 frozen transcript reducer snapshot选择same run/turn的canonical user entry；
2. hydrate完整 normalized user text；
3. 按Unicode code point offsets切出`[start_char:end_char]`；
4. 该substring必须byte-for-byte等于quote text；
5. content SHA、UTF-8 bytes与semantic fingerprint全部重算；
6. 同一text出现多次时，producer必须保存明确span，不允许builder“找第一个”；
7. 未保存span的旧候选不兼容，数据库重置。

Producer hard cut：

- memory proposal sink不再只复制`user_quote: str`；
- 它必须从canonical current-user fact冻结message ID、原文中的exact span与text SHA；
- 若只保留最后2,000 characters，`start_char`必须等于原文长度减2,000，而不是把截断文本当成新全文；
- candidate pool row持久化typed locator；
- governance builder只验证locator，不执行全文模糊搜索；
- `user_quote`可以暂时保留为数据库显示projection，但不再具有authority。

`MAIN_AGENT_TOOL` 的 supersede authority 只接受：

```text
quote_kind = canonical_user_span
verification_status = canonical_match
```

`reflection_reported` 不能单独授权 destructive supersede。若reflection quote能精确匹配canonical user entry，builder可以构造新的`canonical_user_span` semantic/attribution pair；否则保持`origin_reported`。

Compaction summary quote只能是`compaction_summary_span + origin_reported`，不得冒充用户原话。

---

## 6. Source evidence semantic DTO

### 6.1 Main-agent tool

```python
class MainAgentToolGovernanceSourceSemanticFact(GovernanceEvidenceFrozenFact):
    schema_version: Literal["main_agent_tool_governance_source_semantic.v1"]
    evidence_kind: Literal["main_agent_tool"]
    candidate_payload_semantic_fingerprint: str
    model_control_acceptance: Literal["accepted"]
    selected_tool_call_semantic: ModelToolCallBlockSemanticFact
    tool_result_semantic: ToolTerminalProjectionSemanticFact
    quoted_evidence_semantic: GovernanceQuotedEvidenceSemanticFact | None
    semantic_fingerprint: str
```

Invariant：

- selected tool call ID等于candidate `source_tool_call_id`；
- tool call必须`completion_status="completed"`；
- memory tool name必须来自run-frozen capability descriptor；
- tool result semantic的tool call ID/name与call完全相等；
- result timing与terminal projection semantic完全相等；
- candidate payload fingerprint必须与tool-call arguments经过versioned candidate builder后相等；
- semantic fingerprint不覆盖完整terminal document reference或source layout。

### 6.2 Reflection

```python
class ReflectionGovernanceSourceSemanticFact(GovernanceEvidenceFrozenFact):
    schema_version: Literal["reflection_governance_source_semantic.v1"]
    evidence_kind: Literal["reflection"]
    candidate_payload_semantic_fingerprint: str
    reflection_policy_id: str
    reflection_policy_version: str
    reflection_policy_contract_fingerprint: str
    reflection_model_result_semantic_fingerprint: str
    candidate_index: int
    ordered_quoted_evidence_semantics: tuple[
        GovernanceQuotedEvidenceSemanticFact, ...
    ]
    semantic_fingerprint: str
```

Reflection semantic只绑定模型实际reported的quote与候选payload，不把event ID、model call ID、usage或sequence放进semantic fingerprint。

### 6.3 Compaction

```python
class CompactionMemoryCandidateExtractorContractFact(GovernanceEvidenceFrozenFact):
    schema_version: Literal["compaction_memory_candidate_extractor_contract.v1"]
    extractor_id: str
    extractor_version: str
    accepted_input_schema_fingerprint: str
    output_candidate_schema_fingerprint: str
    parsing_rules_fingerprint: str
    normalization_rules_fingerprint: str
    contract_fingerprint: str


class CompactionGovernanceSourceSemanticFact(GovernanceEvidenceFrozenFact):
    schema_version: Literal["compaction_governance_source_semantic.v1"]
    evidence_kind: Literal["compaction"]
    candidate_payload_semantic_fingerprint: str
    summary_content_sha256: str
    summary_content_semantic_fingerprint: str
    extractor_contract: CompactionMemoryCandidateExtractorContractFact
    raw_candidate_index: int
    canonical_parsed_candidate_payload_fingerprint: str
    intent_fingerprint: str
    quoted_evidence_semantic: GovernanceQuotedEvidenceSemanticFact | None
    semantic_fingerprint: str
```

Invariant：

- summary artifact bytes必须重算SHA与semantic fingerprint；
- raw candidate index定位唯一parser output；
- parsed payload fingerprint等于candidate row payload semantic fingerprint；
- intent fingerprint等于producer保存值；
- extractor ID/version/fingerprint三者精确匹配composition-root binding；
- unsupported historical extractor contract fail closed，不使用当前parser猜测。

### 6.4 Union

```python
GovernanceSourceEvidenceSemanticFact = Annotated[
    MainAgentToolGovernanceSourceSemanticFact
    | ReflectionGovernanceSourceSemanticFact
    | CompactionGovernanceSourceSemanticFact,
    Field(discriminator="evidence_kind"),
]
```

不存在`governance`分支。`CandidateOrigin.GOVERNANCE`继续是executor/UOW创建的audit/provenance row，并由candidate pool排除在pending列表之外。

---

## 7. Durable attribution DTO

```python
class GovernanceSourceEvidenceAttributionFact(GovernanceEvidenceFrozenFact):
    schema_version: Literal["governance_source_evidence_attribution.v1"]
    evidence_kind: Literal["main_agent_tool", "reflection", "compaction"]
    evidence_semantic_fingerprint: str
    runtime_session_id: str
    authority_ledger_through_sequence: int
    candidate_entry_id: str
    producer_event_references: tuple[GovernanceStoredEventReferenceFact, ...]
    model_terminal_projection_reference: TerminalProjectionReferenceFact | None
    model_disposition_event_reference: GovernanceStoredEventReferenceFact | None
    tool_terminal_projection_reference: TerminalProjectionReferenceFact | None
    quoted_evidence_attributions: tuple[
        GovernanceQuotedEvidenceAttributionFact, ...
    ]
    source_artifact_references: tuple[
        GovernanceEvidenceArtifactReferenceFact, ...
    ]
    producer_contract_fingerprints: tuple[str, ...]
    fact_fingerprint: str
```

Nullability matrix：

| evidence kind | model projection | disposition | tool projection | producer carrier | artifact |
|---|---|---|---|---|---|
| main_agent_tool | required | required ACCEPTED | required | projection/disposition/tool-result events | optional tool artifacts |
| reflection | forbidden | forbidden | forbidden | reflection completed required | optional quoted-evidence artifact |
| compaction | forbidden | forbidden | forbidden | compaction completed/proposed required | summary artifact required |

`producer_event_references` 的exact type/count matrix：

- `main_agent_tool`：exactly one model terminal-projection committed ref、one ACCEPTED
  disposition ref、one tool terminal-projection committed/end carrier ref；
- `reflection`：exactly one `MemoryReflectionCompletedEvent` ref；
- `compaction`：exactly one compaction completed ref与one
  `ContextCompactionMemoryCandidatesProposedEvent` ref；
- 禁止传入该candidate未选中的same-run sibling events。

Terminal projection artifact references必须与上述stored carrier refs通过document/reference/
committed-event identity强join，因此artifact自身可在无外部猜测的情况下证明所有
carrier sequences均不超过H。

Attribution validator必须验证：

- 所有`producer_event_references` 与 disposition reference 可从artifact自身验证
  `sequence <= authority_ledger_through_sequence`；
- 每个reference的stable identity、schema fingerprint、payload fingerprint、sequence与
  stored-envelope fingerprint逐项重算一致；
- refs全部属于同一runtime session；
- main tool的projection/disposition/tool result属于same run/turn/reply；
- disposition resolved call ID与projection reference identity一致；
- reflection candidate attribution中的entry ID/payload fingerprint/index精确匹配；
- compaction completed event、summary artifact、extractor contract与candidate row精确匹配；
- refs只影响fact fingerprint，不进入nested semantic fingerprint。

---

## 8. Transcript reducer provenance hard cut

### 8.1 Assistant entry必须保存disposition ref

当前accepted assistant stable entry只保存terminal projection committed event ref。实施后：

```text
TranscriptMessageLeafEntryFact.source_event_refs = (
    ModelCallTerminalProjectionCommittedEvent ref,
    ModelCallControlDispositionResolvedEvent ref,
)
```

有tool calls时，assistant entry、tool pair与tool result仍保持现有semantic identity；disposition ref只进入materialization/fact identity。

### 8.2 Schema变化

- `TranscriptMessageLeafEntryFact`升级schema version；
- checkpoint leaf/page/root materialization fact随之升级；
- `TranscriptMessageLeafSemanticFact.semantic_fingerprint`不变；
- `TranscriptProjectionStableSemanticStateFact.normalized_transcript_fingerprint`不变；
- checkpoint artifact ID、fact fingerprint与tree pages允许变化；
- PostgreSQL与checkpoint artifacts直接重置，不提供旧schema decoder migration。

### 8.3 原子authority snapshot API

新增：

```python
@dataclass(frozen=True, slots=True)
class GovernanceTranscriptAuthoritySnapshot:
    reducer_evidence_snapshot: TranscriptProjectionReducerEvidenceSnapshot
    document_view: VerifiedTranscriptProjectionDocumentView
    ledger_through_sequence: int
    ledger_continuity_accumulator: str
    transcript_semantic_event_count: int
    transcript_semantic_accumulator: str
    snapshot_fingerprint: str


class TranscriptProjectionStateStore:
    def capture_governance_authority_snapshot(
        self,
    ) -> GovernanceTranscriptAuthoritySnapshot: ...
```

该方法必须在同一个synchronous reducer `RLock`内：

1. 冻结live state；
2. 冻结stable entries；
3. 冻结required projection refs；
4. 冻结已验证document view；
5. 读取reducer自己的`ledger_through_sequence=H`；
6. 构造snapshot fingerprint；
7. 返回后才释放锁。

禁止算法：

```text
EventLog.next_sequence() -> H
await / call evidence_snapshot()
把H贴到snapshot
```

所有后续exact/sparse EventLog reads必须显式`through_sequence <= H`。发现required carrier sequence大于H时返回`not_ready`，不得把future fact加入旧snapshot。

---

## 9. Reflection producer hard cut

### 9.1 当前问题

当前reflection顺序为：

```text
parse model output
-> append candidate pool rows
-> construct MemoryReflectionCompletedEvent
-> caller later emits completed event
```

候选row可能在completed event未durable时可见；completion也没有candidate entry IDs或payload fingerprints。

### 9.2 新candidate attribution

```python
class ReflectionCandidateAttributionFact(GovernanceEvidenceFrozenFact):
    schema_version: Literal["reflection_candidate_attribution.v1"]
    candidate_entry_id: str
    candidate_index: int
    candidate_payload: CandidatePayload
    candidate_payload_fingerprint: str
    intent_fingerprint: str | None
    ordered_quoted_evidence_indices: tuple[int, ...]
    attribution_fingerprint: str
```

`MemoryReflectionCompletedEvent`新增required：

```python
reflection_model_call_end_event_identity: StableEventIdentityFact
reflection_model_result_semantic_fingerprint: str
reflection_policy_contract_fingerprint: str
ordered_candidate_attributions: tuple[ReflectionCandidateAttributionFact, ...]
```

Invariant：

- `proposed_count == len(ordered_candidate_attributions)`；
- indices从0开始连续且唯一；
- candidate kinds与attributions逐项一致；
- quoted evidence indices合法；
- payload/fingerprint可由event独立重建；
- completed event ID由reflection ID确定性派生。

### 9.3 Event-first + 通用 transactional outbox

Reflection engine不得直接写candidate pool。新顺序：

```text
parse model output
-> freeze candidate entry IDs and payloads
-> build stable MemoryReflectionCompletedEvent candidate
-> MemoryCandidateProjectionCommitPort.commit_producer_bundle()
     atomically:
       append AgentEvent
       advance materialization account
       insert memory_candidate_projection_outbox rows
-> publication
-> service-owned dispatcher idempotently projects candidate rows
```

Outbox唯一键：

```text
(runtime_session_id, producer_kind, producer_event_id, candidate_entry_id)
```

新增低层提交端口：

```python
class CandidateProjectionProducerKind(StrEnum):
    REFLECTION = "reflection"
    COMPACTION = "compaction"


class CandidateProjectionOutboxItemFact(GovernanceEvidenceFrozenFact):
    schema_version: Literal["candidate_projection_outbox_item.v1"]
    producer_kind: CandidateProjectionProducerKind
    producer_event_identity: StableEventIdentityFact
    candidate_entry_id: str
    candidate_index: int = Field(ge=0, le=255)
    candidate_payload: CandidatePayload
    candidate_payload_fingerprint: str
    candidate_attribution_fingerprint: str
    item_fingerprint: str


class MemoryCandidateProjectionCommitPort(Protocol):
    def commit_producer_bundle(
        self,
        *,
        producer_event: AgentEvent,
        ordered_outbox_items: tuple[CandidateProjectionOutboxItemFact, ...],
        expected_materialization_account_state_fingerprint: str,
        absolute_deadline_monotonic: float,
    ) -> RuntimeEventWriteOperationResult: ...
```

`producer_event`只允许`MemoryReflectionCompletedEvent`或
`ContextCompactionMemoryCandidatesProposedEvent`，并必须与
`producer_kind`符合。Port先构造stable event candidate、stable outbox items与account
transition candidate，再交给同一physical operation owner。

PostgreSQL implementation必须在同一pooled connection、同一transaction中：

1. CAS materialization account row；
2. append producer event envelope；
3. insert ordered outbox rows；
4. commit；
5. 按stable IDs执行confirmation。

`NONE`必须rollback三者；`FULL`必须三者均可exact-ID确认；
`PARTIAL/UNKNOWN`保留session-owned reconciliation owner并禁止dispatcher猜测。
InMemory implementation使用同一个临时state copy执行三项mutation，任一
validator失败时整体丢弃，不允许事件与outbox拆成两次in-memory commit。

状态：

```text
pending -> applying -> applied
                   `-> failed(retryable)
```

规则：

- `NONE`：重试同一completed event与outbox payload；
- `FULL`：candidate只能从通用outbox发布；
- `UNKNOWN/PARTIAL`：保留service-owned owner并confirm，不直接append candidate；
- dispatcher crash：restart从outbox恢复；
- candidate insert幂等且payload conflict fail closed；
- candidate row永远不能早于durable completed event可见；
- Host close bounded drain event writer与reflection projection dispatcher；
- outbox永久失败时candidate不进入governance pending set。

这项hard cut同时解决reflection candidate/completion的原子归因，不保留旧“先candidate后event”路径。

---

## 10. Compaction producer contract

### 10.1 Extractor binding

Composition root注册：

```python
class CompactionMemoryCandidateExtractorBinding:
    contract: CompactionMemoryCandidateExtractorContractFact
    implementation_build_fingerprint: str | None
    parse: CompactionMemoryCandidateExtractor
```

准入比较只使用ID/version/contract fingerprint；build fingerprint只用于Inspector。

任何相同summary bytes产生不同canonical candidate payload的修改：

```text
extractor_version必须升级
and
contract_fingerprint必须改变
```

### 10.2 Producer carrier

`ContextCompactionMemoryCandidatesProposedEvent`必须保存：

- summary artifact ID/content SHA/bytes；
- extractor contract fact；
- ordered raw candidate attributions；
- parsed payload fingerprints；
- intent fingerprints；
- skipped/duplicate facts；
- completed compaction event identity。

Candidate row不再依赖自由`metadata`推断上述字段。允许保留metadata作operational diagnostics，但builder不得读取它决定semantic evidence。

Compaction与Reflection共用第 9.3 节的
`MemoryCandidateProjectionCommitPort`。新顺序是：

```text
freeze completed compaction + confirmed summary artifact
-> bind extractor contract and build ordered candidate attributions
-> freeze ContextCompactionMemoryCandidatesProposedEvent
-> commit_producer_bundle(proposed event, compaction outbox items)
-> FULL
-> dispatcher projects memory_candidates rows
```

禁止继续使用当前`candidate_pool.append -> emit proposed event`的candidate-first
顺序，也禁止吞掉proposed event write failure。`NONE`重试同一event/outbox
bundle；`UNKNOWN/PARTIAL`交给同一reconciliation owner。

### 10.3 Replay

Restart builder必须：

1. hydrate exact summary artifact；
2.校验artifact hash；
3. rebind historical extractor contract；
4. 使用同一parser重放指定raw index；
5. 比较candidate payload与intent fingerprints；
6. mismatch进入`authority_untrusted`，不得用当前extractor结果覆盖历史事实。

---

## 11. Main-agent tool evidence builder

### 11.1 Declarative builder contract

当前memory tool在adapter外使用`uuid4()`注入`candidate_id`，使同一durable
tool-call arguments无法exact replay。V1 hard cut删除该随机入口，冻结：

```python
class MainAgentMemoryCandidateBuilderContractFact(
    GovernanceEvidenceFrozenFact
):
    schema_version: Literal[
        "main_agent_memory_candidate_builder_contract.v1"
    ]
    builder_id: str
    builder_version: str
    input_schema_fingerprint: str
    output_schema_fingerprint: str
    candidate_id_policy: Literal["source_identity_sha256_v1"]
    normalization_contract_fingerprint: str
    contract_fingerprint: str


class MainAgentMemoryCandidateBuilderBinding:
    contract: MainAgentMemoryCandidateBuilderContractFact
    implementation_build_fingerprint: str | None
    build: MainAgentMemoryCandidateBuilder
```

Composition root必须按builder ID/version/contract fingerprint精确解析binding。
`implementation_build_fingerprint`只用于诊断，不进入durable identity。任何使同一
input产生不同candidate semantics的修改，必须同时升级version并改变contract
fingerprint。

Builder的canonical input是：

```text
runtime_session_id
+ source run/turn/reply IDs
+ exact source_tool_call_id
+ exact tool name
+ exact arguments parse state and canonical/raw argument form
+ builder ID/version/contract fingerprint
```

Candidate ID唯一算法：

```text
candidate:main_tool:
sha256(
  "main-agent-memory-candidate-id:v1\0"
  + runtime_session_id + "\0"
  + source_run_id + "\0"
  + source_tool_call_id + "\0"
  + builder_contract_fingerprint
)
```

`candidate_id`继续作为candidate payload字段，但必须由上述factory生成并在
candidate row、tool result projection、builder replay三方精确比较。不允许调用方
传入UUID，也不允许replay时重新随机生成。

### 11.2 唯一输入

Builder只接受：

```python
build_main_agent_tool_evidence(
    *,
    candidate: PooledMemoryCandidate,
    authority: GovernanceTranscriptAuthoritySnapshot,
    event_reader: GovernanceBoundedEventReader,
) -> GovernanceEvidenceBuildResult
```

禁止接受：

- mutable `LoopState`；
- `state.messages`；
- raw segment list；
- `event_log.iter()`；
- pre-rendered source summary string；
- caller自报accepted bool；
- current capability registry作为历史tool identity真源。

### 11.3 定位算法

在frozen stable entries中：

1. 选择same run/turn的canonical user entry；
2. 找到唯一assistant terminal projection entry，其hydrated document包含matching `source_tool_call_id`；
3. assistant source refs必须包含matching projection committed与ACCEPTED disposition；
4. 找到唯一tool pair entry；
5. 找到唯一tool result projection entry；
6. validate terminal projection reference/document full join；
7. validate tool call ID/name、arguments semantics、tool result semantics/timing；
8. 按durable builder ID/version/contract fingerprint解析historical binding；
9. validate candidate payload与candidate ID由exact tool arguments与versioned builder产生；
10. validate candidate quote的canonical user span；
11. 构造semantic、attribution与prompt projection。

不得选择：

- thinking block作为governance quoted evidence；
- provider error diagnostics；
- suppressed model projection；
- same run中其他memory tool call；
- incomplete/suspended tool pair；
- current tool descriptor重新解释历史call。

### 11.4 多tool call

同一accepted model projection可以包含多个memory calls。每个candidate必须按exact `source_tool_call_id`获得独立evidence；assistant projection semantic可以共享，但tool call/result semantic必须唯一。

一个candidate不能同时引用多个source tool calls。Merge只发生在governance decision层，不发生在source evidence builder。

---

## 12. Evidence build result与失败分类

```python
class GovernanceEvidenceBuildStatus(StrEnum):
    FULL = "full"
    NOT_READY = "not_ready"
    CANDIDATE_SOURCE_INVALID = "candidate_source_invalid"
    AUTHORITY_UNTRUSTED = "authority_untrusted"
    NOT_APPLICABLE = "not_applicable"
```

```python
class GovernanceEvidenceBuildResult(GovernanceEvidenceFrozenFact):
    schema_version: Literal["governance_evidence_build_result.v1"]
    status: GovernanceEvidenceBuildStatus
    candidate_entry_id: str
    source_high_water: int
    evidence_semantic: GovernanceSourceEvidenceSemanticFact | None
    evidence_attribution: GovernanceSourceEvidenceAttributionFact | None
    stable_reason_code: GovernanceEvidenceBuildReason
    retry_after_seconds: float | None
    result_fingerprint: str
```

Nullability：

- `FULL`：semantic/attribution required，retry absent；
- `NOT_READY`：facts absent，retry required；
- `CANDIDATE_SOURCE_INVALID`：facts absent，retry absent；
- `AUTHORITY_UNTRUSTED`：facts absent，retry absent；
- `NOT_APPLICABLE`：仅用于明确不进入governance input的audit origin。

Reason prefix matrix是closed：

| status | allowed reason family |
|---|---|
| FULL | `FULL_MAIN_TOOL_JOIN | FULL_REFLECTION_JOIN | FULL_COMPACTION_JOIN` |
| NOT_READY | `WAIT_*` |
| CANDIDATE_SOURCE_INVALID | `INVALID_*` |
| AUTHORITY_UNTRUSTED | `UNTRUSTED_*` |
| NOT_APPLICABLE | `NOT_APPLICABLE_AUDIT_ORIGIN` |

`CANDIDATE_SOURCE_INVALID`与`MemoryCandidateEvidenceRejectedRecord`的reason使用一张
显式one-to-one mapping table，不能通过删前缀或拼接字符串派生：

| build reason | rejection reason |
|---|---|
| INVALID_SOURCE_CALL_MISSING | SOURCE_CALL_MISSING |
| INVALID_TERMINAL_RUN_WITHOUT_PAIR | TERMINAL_RUN_WITHOUT_PAIR |
| INVALID_CANDIDATE_PAYLOAD_MISMATCH | CANDIDATE_PAYLOAD_MISMATCH |
| INVALID_PRODUCER_OMITS_CANDIDATE | PRODUCER_OMITS_CANDIDATE |
| INVALID_RAW_CANDIDATE_INDEX | RAW_CANDIDATE_INDEX_MISSING |
| INVALID_ORIGIN_FIELDS | ORIGIN_FIELDS_INVALID |

### 12.1 Candidate source invalid

以下属于候选provenance无效：

- source tool call ID不存在；
- source run已terminal但没有matching accepted pair；
- candidate payload与exact tool-call arguments不一致；
- reflection carrier明确未包含该candidate entry；
- compaction raw index不存在；
- candidate origin与required source fields矛盾。

它不能伪装成LLM `SkipDecision`。新增system-owned terminal record：

```python
class MemoryCandidateEvidenceRejectedRecord(GovernanceEvidenceFrozenFact):
    schema_version: Literal["memory_candidate_evidence_rejected.v1"]
    candidate_entry_id: str
    source_high_water: int
    stable_reason_code: CandidateEvidenceRejectionReason
    observed_source_fingerprints: tuple[str, ...]
    rejection_fingerprint: str
```

Candidate pool pending查询必须排除已有该record的entry。对应runtime event由UOW/outbox发布，但record不是`MemoryGovernanceDecisionRecord`，不占用LLM decision vocabulary。

### 12.2 Authority untrusted

以下属于canonical authority不可信：

- terminal artifact hash/fact conflict；
- reducer snapshot与EventLog exact event不一致；
- stable entry source refs无法hydrate；
- historical event decoder/contract无法rebind；
- same ID出现payload conflict；
- compaction artifact与durable producer fingerprint冲突；
- reflection completion/outbox payload conflict。

处理：

- 阻止整个governance batch；
- 安装session-owned governance authority latch；
- 不terminalize candidates；
- 不调用Flash；
- 不写普通skip；
- Inspector展示bounded mismatch code与refs；
- 只有doctor/repair或session reopen重建成功后才能解除。

若EventLog本身已untrusted，不得尝试向同一ledger追加“错误说明”来掩盖问题；只记录operational diagnostic并latch。

### 12.3 Not ready closure

`NOT_READY`不是无限重试状态。冻结origin-specific closure：

| origin | 可等待 | 转invalid | 转authority_untrusted |
|---|---|---|---|
| main_agent_tool | source run active且reducer H尚未覆盖tool terminal | RunEnd FULL后仍无matching pair | existing refs/hash/reducer conflict |
| reflection | durable completion FULL但projection outbox仍pending | completion明确不含candidate（正常不应发布row） | completion/outbox payload conflict |
| compaction | artifact physical write/confirmation仍pending | completed producer明确不含raw index | confirmed artifact hash/contract conflict |

Coordinator拥有per-session retry timer：

```text
initial = 0.5s
multiplier = 2
max = 30s
jitter = none
```

同一candidate的next due time由stable retry generation管理；新safe point只wake，不重置backoff。source high-water推进后可立即重试。close必须取消timer并drain正在运行的builder。

---

## 13. Prompt projection policy

### 13.1 Contract

```python
class GovernanceEvidencePromptProjectionContractFact(
    GovernanceEvidenceFrozenFact
):
    schema_version: Literal["governance_evidence_prompt_projection_contract.v1"]
    policy_id: str
    policy_version: str
    max_quote_characters_per_candidate: int = Field(ge=0, le=8_192)
    max_assistant_text_characters_per_candidate: int = Field(ge=0, le=8_192)
    max_tool_result_characters_per_candidate: int = Field(ge=0, le=8_192)
    max_artifact_refs_per_candidate: int = Field(ge=0, le=16)
    max_candidates_per_batch: int = Field(ge=1, le=32)
    max_related_memories_per_candidate: int = Field(ge=0, le=16)
    max_candidate_projection_utf8_bytes: int = Field(ge=1, le=64 * 1024)
    max_batch_projection_utf8_bytes: int = Field(ge=1, le=512 * 1024)
    truncation_policy: Literal["typed_head_tail_v1"]
    essential_envelope_contract_fingerprint: str
    contract_fingerprint: str
```

V1 production defaults：

```text
quote chars / candidate          = 2,000
assistant text chars / candidate = 2,000
tool result chars / candidate    = 2,000
artifact refs / candidate        = 8
candidates / batch               = 20
related memories / candidate     = 8
candidate projection bytes       = 16 KiB
batch projection bytes           = 128 KiB
```

这些是governance prompt的physical serialization bounds，不是runtime ledger、terminal
document或model context第二真源，也不能用bytes与tokens直接比较。

Configuration doctor必须对每个production Flash target/options pair：

1. 使用生产允许的最大batch candidate count；
2. 构造maximal typed candidate projections、relatedness projections、artifact refs与metadata；
3. 使用真实`GovernanceSystemPromptContractFact`组装exact canonical `LLMContext`；
4. 通过生产provider serializer与同一token estimator计算input tokens；
5. 对比`ResolvedModelContextBudgetFact.input_budget_tokens`，其中已包含output
   reservation与input safety margin；
6. 同时验证canonical artifact bytes与所有per-item/per-batch physical bounds。

任一target/options pair不可行时startup/doctor fail closed并列出target、resolved
input budget、estimated maximal input、safety margin与超限component。不允许以
`128 KiB < input token budget`之类量纲错误的比较声称可行。

### 13.2 Projection fact

```python
class GovernancePromptEvidenceTextFact(GovernanceEvidenceFrozenFact):
    schema_version: Literal["governance_prompt_evidence_text.v1"]
    field_code: Literal[
        "verified_user_quote",
        "accepted_assistant_text",
        "selected_tool_arguments",
        "tool_result_essential",
        "reflection_report",
        "compaction_summary",
    ]
    text: str = Field(max_length=2_000)
    source_semantic_fingerprint: str
    verification_status: Literal["canonical_match", "origin_reported"]
    text_fingerprint: str


class GovernanceCandidatePromptPayloadFact(GovernanceEvidenceFrozenFact):
    schema_version: Literal["governance_candidate_prompt_payload.v1"]
    candidate_entry_id: str
    candidate_payload_semantic_fingerprint: str
    canonical_candidate_payload: CandidatePayload
    evidence_kind: Literal["main_agent_tool", "reflection", "compaction"]
    accepted: bool
    ordered_evidence_texts: tuple[GovernancePromptEvidenceTextFact, ...]
    tool_name: str | None
    tool_result_state: str | None
    observation_timing_fingerprint: str | None
    artifact_references: tuple[GovernanceEvidenceArtifactReferenceFact, ...]
    payload_utf8_bytes: int = Field(ge=1, le=16 * 1024)
    payload_fingerprint: str


class GovernanceEvidencePromptProjectionFact(GovernanceEvidenceFrozenFact):
    schema_version: Literal["governance_evidence_prompt_projection.v1"]
    source_evidence_semantic_fingerprint: str
    projection_contract_id: str
    projection_contract_version: str
    projection_contract_fingerprint: str
    model_visible_payload: GovernanceCandidatePromptPayloadFact
    included_field_codes: tuple[str, ...]
    omitted_field_codes: tuple[str, ...]
    truncation_reason_codes: tuple[str, ...]
    projected_utf8_bytes: int
    projection_fingerprint: str
```

Projection只允许：

- candidate typed payload；
- verified quote及verification status；
- accepted marker；
- selected tool call name/arguments semantics；
- tool result state、timing与有界essential result；
- compaction/reflection origin facts；
- bounded artifact refs；
- prior governance outcomes与relatedness view。

`GovernanceCandidatePromptPayloadFact`的validator必须对typed candidate、ordered evidence
texts与artifact refs执行canonical serialization，重算UTF-8 bytes与payload fingerprint。
`model_visible_payload`不允许`dict[str, Any]`、自由JSON或未绑定的pre-rendered
prompt字符串。

### 13.3 Exact decision view

最终`GovernanceModelInputFact`中的每个candidate item必须同时保存三层、但不得混淆
它们的authority：

```text
candidate
    = GovernanceCandidatePromptPayloadFact
    = bounded typed source-evidence projection

decision_candidate
    = ValidCandidatePayload.candidate的exact领域DTO
    = 仅供需要candidate字段的decision逐字段复制

lifecycle
    = actions_allowed
    + allowed_memory_ids
    + allowed_replacement_evidence_refs
```

`decision_candidate`禁止携带`ValidCandidatePayload` wrapper；模型也不得把外层
`candidate` evidence object复制进decision的`candidate`字段。无有效candidate时该字段为
`null`，只有完整证据足以构造所有required typed fields时才允许
`correct_and_submit/merge_and_submit`。

`lifecycle.allowed_memory_ids`只来自本次冻结的FULL relatedness snapshot；
`allowed_replacement_evidence_refs`只有在quoted evidence已exact匹配canonical transcript
span时才包含`candidate_user_quote`。Producer event ID、quote semantic fingerprint与artifact
reference不能冒充replacement authority。Prompt中的allowlist只帮助模型生成合法请求；
executor仍必须独立从同一batch snapshot重建allowlist并逐项复核。

`GovernanceSystemPromptContract` v4冻结以下决策顺序：

1. 先判durability；today/this-time/one-off状态即使出现“remember”也应skip；
2. 再判exact/semantic duplicate；重复memory应skip；
3. 最后判lifecycle；只有canonical quote明确表达change/replace/stop Y use Z才可
   supersede，普通durable冲突且没有replacement intent时使用non-destructive
   contradiction，可兼容的相关事实继续走普通coexist路径。

该顺序属于exact frozen model input contract，并进入system prompt SHA、assembly contract
fingerprint与最终provider-neutral context fingerprint。修改任一规则必须升级contract version；
recovery继续读取artifact中的旧exact prompt，不使用当前进程的新版本重新解释。

禁止暴露：

- thinking正文；
- raw provider diagnostics；
- model segment content/layout；
- secret-bearing tool output；
-未选择的同run事件；
- mutable metadata dict；
- unbounded terminal document JSON。

Head/tail规则必须对UTF-8与Unicode code points确定性，插入固定typed omission marker；不得使用Python `repr()`。

---

## 14. Relatedness snapshot

Relatedness仍是advisory side path，但本次模型看到的结果必须进入batch input identity。

```python
class GovernanceRelatedMemorySemanticFact(GovernanceEvidenceFrozenFact):
    schema_version: Literal["governance_related_memory_semantic.v1"]
    memory_id: str
    memory_type: str
    canonical_statement_sha256: str
    canonical_statement_utf8_bytes: int = Field(ge=1, le=64 * 1024)
    scope: str
    status: str
    verification_status: str | None
    source_authority: str | None
    applies_when: str | None
    do_not_apply_when: str | None
    semantic_fingerprint: str


class GovernanceRelatedMemoryPromptProjectionFact(
    GovernanceEvidenceFrozenFact
):
    schema_version: Literal["governance_related_memory_prompt_projection.v1"]
    memory_semantic_fingerprint: str
    projected_statement: str = Field(max_length=2_000)
    relationship_codes: tuple[str, ...]
    exact_duplicate: bool
    projection_contract_fingerprint: str
    projected_utf8_bytes: int = Field(ge=1, le=4 * 1024)
    projection_fingerprint: str


class GovernanceRelatednessCandidateFact(GovernanceEvidenceFrozenFact):
    schema_version: Literal["governance_relatedness_candidate.v1"]
    graph_id: str
    memory_node_revision: int = Field(ge=1)
    canonical_memory: GovernanceRelatedMemorySemanticFact
    canonical_statement_inline: str | None = Field(default=None, max_length=4_096)
    canonical_content_reference: GovernanceEvidenceArtifactReferenceFact | None
    prompt_projection: GovernanceRelatedMemoryPromptProjectionFact
    source_projection_fingerprint: str
    fact_fingerprint: str


class GovernanceRelatednessSnapshotFact(GovernanceEvidenceFrozenFact):
    schema_version: Literal["governance_relatedness_snapshot.v1"]
    candidate_entry_id: str
    availability: Literal["full", "partial", "unavailable"]
    ordered_candidates: tuple[GovernanceRelatednessCandidateFact, ...]
    provider_contract_fingerprint: str
    snapshot_fingerprint: str
```

`ordered_candidates`按`(memory_id, node_revision)`确定性排序、唯一，数量不超过
`max_related_memories_per_candidate`。`FULL`才允许destructive lifecycle decision；
`PARTIAL/UNAVAILABLE`可以作model-visible缺失事实，但不能伪造空的FULL snapshot。

Executor destructive lifecycle gate必须比较decision target与该batch frozen relatedness snapshot，不能重新查询current relatedness后替换历史准入。

`canonical_statement_inline`与`canonical_content_reference`必须exactly one present。Inline
段有独立UTF-8 bytes bound；超限时必须使用content-addressed artifact。两个
分支都必须重算为与`canonical_memory.canonical_statement_sha256/
canonical_statement_utf8_bytes` 一致的typed statement。`source_projection_fingerprint`
只作relatedness producer attribution，不能替代statement、graph ID、node revision或
model-visible projection。

本阶段给canonical memory node增加monotonic `node_revision`；所有canonical UOW
mutation与revision同transaction推进。Relatedness snapshot factory在一个consistent read中
冻结graph ID、node revision、canonical content与prompt projection，不依赖稍后重查
current node来填补历史输入。

Canonical target状态仍在UOW transaction内重读；drift按现有规则downgrade/regovernance。

### 14.1 Durable candidate claim

候选选择与batch归属不得只依赖process-local lock。新增：

```python
class GovernanceCandidateClaimStatus(StrEnum):
    PREPARING = "preparing"
    PREPARED = "prepared"
    TERMINAL = "terminal"
    RELEASED = "released"


class MemoryGovernanceCandidateClaimFact(GovernanceEvidenceFrozenFact):
    schema_version: Literal["memory_governance_candidate_claim.v1"]
    candidate_entry_id: str
    candidate_row_fingerprint: str
    governance_batch_id: str
    claim_generation: int = Field(ge=1)
    status: GovernanceCandidateClaimStatus
    prepared_event_id: str | None
    terminal_record_id: str | None
    previous_claim_fingerprint: str | None
    claim_fingerprint: str
```

PostgreSQL 表`memory_governance_candidate_claims`以`candidate_entry_id`
为唯一键。`MemoryGovernanceCandidateClaimService.claim_pending_batch()`必须在同一
transaction中：

1. 按stable candidate ordering选择pending rows；
2. 验证row fingerprint不变；
3. 对整个batch执行all-or-none claim insert/CAS；
4. 返回immutable claimed candidate snapshot与claim generation。

Live coordinator、manual governance、reopen recovery与stale-worker takeover只能通过该service。
Recovery必须接管原`governance_batch_id`与claims，不得重新选择同一candidate
构造新batch。接管只在RuntimeSession reopen mutation gate内，以generation CAS
证明旧owner已不可达；禁止只根据wall-clock expiry抢占。

Status matrix：

| current | legal next | rule |
|---|---|---|
| absent | preparing | candidate selection与claim同transaction |
| preparing | prepared | batch artifact confirmed + Prepared FULL，CAS同批claims |
| preparing | released | 仅Prepared之前确定放弃，必须durable release CAS |
| preparing | terminal | candidate-source-invalid的system-owned rejection |
| prepared | terminal | batch Completed/Failed/Blocked或decision terminal join |
| prepared | released | forbidden |
| terminal/released | any | forbidden，exact idempotent replay除外 |

`NOT_READY`默认保留`PREPARING` claim并由同batch retry owner重试，避免另一
worker重复调用模型。Prepared FULL 时 claims与 Prepared event 通过exact batch ID/
candidate IDs/fingerprints强join。候选pending查询必须排除所有non-RELEASED
claims。

Prepared linearization使用唯一低层端口：

```python
class MemoryGovernanceBatchPreparationCommitPort(Protocol):
    def commit_prepared_bundle(
        self,
        *,
        prepared_event: MemoryGovernanceBatchPreparedEvent,
        expected_claims: tuple[MemoryGovernanceCandidateClaimFact, ...],
        expected_materialization_account_state_fingerprint: str,
        absolute_deadline_monotonic: float,
    ) -> RuntimeEventWriteOperationResult: ...
```

PostgreSQL在同一transaction内CAS所有claims `PREPARING -> PREPARED`、append
Prepared event并推进materialization account。任一claim generation/status/fingerprint不符
时整批rollback。InMemory实现使用同一copy-on-write snapshot。`NONE`不得留下
部分PREPARED claim；`PARTIAL/UNKNOWN`必须交给session-owned confirmation owner，
不能另起新batch。

---

## 15. Governance batch input durable carrier

### 15.1 Exact governance model input

Prepared recovery的权威输入不是一个总fingerprint，而是已冻结的完整
provider-neutral `LLMContext`。新增：

```python
class GovernanceSystemPromptContractFact(GovernanceEvidenceFrozenFact):
    schema_version: Literal["governance_system_prompt_contract.v1"]
    contract_id: str
    contract_version: str
    template_content_sha256: str
    assembly_contract_fingerprint: str
    contract_fingerprint: str


class GovernanceFrozenLLMToolCallFact(GovernanceEvidenceFrozenFact):
    schema_version: Literal["governance_frozen_llm_tool_call.v1"]
    tool_call_id: str = Field(min_length=1, max_length=256)
    name: str = Field(min_length=1, max_length=128)
    arguments: str = Field(max_length=64 * 1024)
    semantic_fingerprint: str


class GovernanceFrozenLLMMessageFact(GovernanceEvidenceFrozenFact):
    schema_version: Literal["governance_frozen_llm_message.v1"]
    role: Literal[
        "system", "user", "assistant", "tool_call", "tool_result",
        "runtime_observation",
    ]
    content: tuple[str, ...] = Field(max_length=16)
    thinking: tuple[str, ...] = Field(max_length=16)
    tool_calls: tuple[GovernanceFrozenLLMToolCallFact, ...] = Field(max_length=32)
    tool_call_id: str | None
    name: str | None
    arguments: str | None
    message_fingerprint: str


class GovernanceModelInputFact(GovernanceEvidenceFrozenFact):
    schema_version: Literal["governance_model_input.v1"]
    governance_batch_id: str
    resolved_call: ResolvedModelCallFact
    target_fingerprint: str
    context_id: str
    model_call_index: Literal[None] = None
    system_prompt_contract: GovernanceSystemPromptContractFact
    exact_system_prompt: str = Field(max_length=64 * 1024)
    ordered_messages: tuple[GovernanceFrozenLLMMessageFact, ...] = Field(
        min_length=1,
        max_length=128,
    )
    tool_spec_count: Literal[0] = 0
    compiler_estimated_input_tokens: int = Field(ge=1)
    estimator_contract_fingerprint: str
    model_input_fingerprint: str
```

`GovernanceModelInputFact` validator必须构造一个exact `LLMContext`并验证：

- `resolved_call.purpose == MEMORY_GOVERNANCE`；
- `resolved_call.context_mode == DIRECT` 且`model_call_index is None`；
- `target_fingerprint == resolved_call.target.target_fingerprint`；
- `context_id == "memory_governance:" + governance_batch_id`；
- `system_prompt` 只由frozen contract与exact text确定；
- ordered messages的role/content/thinking/tool fields逐项与artifact一致；
- `tools == ()`；
- estimate由同一production serializer/estimator重算；
- canonical `LLMContext` fingerprint等于`model_input_fingerprint`。

正确顺序冻结为：

```text
capture/claim evidence
-> resolve exact Flash target and ResolvedModelCallFact
-> assemble exact system prompt + ordered messages + context ID
-> build GovernanceModelInputFact
-> persist GovernanceBatchInputSnapshotFact artifact
-> commit Prepared FULL
-> commit ModelStart with the exact frozen ResolvedModelCallFact
```

Recovery禁止读取current model slots、current prompt template或current relatedness重新resolve。
当historical target/provider binding不可exact rebind时，batch进入typed blocked/latch，不能
替换model或构造新call ID。

### 15.2 Artifact fact

```python
class GovernanceBatchInputSnapshotFact(GovernanceEvidenceFrozenFact):
    schema_version: Literal["governance_batch_input_snapshot.v1"]
    artifact_contract: GovernanceBatchInputArtifactContractFact
    runtime_session_id: str
    governance_batch_id: str
    source_ledger_through_sequence: int
    transcript_authority_snapshot_fingerprint: str
    ordered_preparing_claims: tuple[MemoryGovernanceCandidateClaimFact, ...]
    ordered_candidate_snapshots: tuple[
        ImmutableGovernanceCandidateSnapshotFact, ...
    ]
    ordered_relatedness_snapshots: tuple[
        GovernanceRelatednessSnapshotFact, ...
    ]
    allowed_scopes: tuple[str, ...]
    prompt_projection_contract_fingerprint: str
    model_input: GovernanceModelInputFact
    final_model_visible_input_fingerprint: str
    batch_input_fingerprint: str
```

Invariant：

- candidate IDs ordered、unique且不含`CandidateOrigin.GOVERNANCE`；
- preparing claims与candidate snapshots按entry ID一一对应，status全部为
  `PREPARING`且batch ID/generation/row fingerprint精确一致；
- relatedness snapshots与candidate IDs一一对应；
- allowed scopes sorted/unique；
- 每个source attribution high-water等于batch source high-water；
- final model-visible input由`model_input` 的exact system prompt、ordered messages与
  provider-neutral serialization重算；
- `final_model_visible_input_fingerprint == model_input.model_input_fingerprint`；
- model input中的candidate IDs、projection fingerprints、allowed scopes与artifact其他字段精确join；
- batch input fingerprint覆盖所有nested fact fingerprints；
- artifact bytes由single factory生成并验证size bound。

### 15.3 Artifact reference

```python
class GovernanceBatchInputReferenceFact(GovernanceEvidenceFrozenFact):
    schema_version: Literal["governance_batch_input_reference.v1"]
    governance_batch_id: str
    artifact_id: str
    artifact_content_sha256: str
    artifact_utf8_bytes: int
    artifact_contract_id: str
    artifact_contract_version: str
    artifact_contract_fingerprint: str
    batch_input_fingerprint: str
    reference_fingerprint: str
```

Artifact ID：

```text
governance-batch-input:{batch_input_fingerprint_without_prefix}
```

### 15.4 Prepared event

```python
class MemoryGovernanceBatchPreparedEvent(EventBase):
    type: Literal[EventType.MEMORY_GOVERNANCE_BATCH_PREPARED]
    governance_batch_id: str
    source_ledger_through_sequence: int
    candidate_entry_ids: tuple[str, ...]
    preparing_claims_fingerprint: str
    batch_input_reference: GovernanceBatchInputReferenceFact
    resolved_model_call_id: str
    target_fingerprint: str
    model_input_fingerprint: str
    ordered_prompt_projections_fingerprint: str
    event_fingerprint: str
```

Event ID：

```text
memory_governance_batch:{governance_batch_id}:prepared
```

Prepared event只有在artifact put与read-confirm FULL后才能提交。Artifact写入/确认使用RuntimeSession-owned bounded I/O service，支持stable candidate、cancellation ownership、close drain与UNKNOWN latch。

Prepared validator必须hydrate artifact并确认上述call/target/input三个identity与
`GovernanceModelInputFact`逐项一致。`ordered_prompt_projections_fingerprint`由
ordered candidate prompt projection fingerprints与ordered relatedness prompt projection fingerprints
在一次canonical traversal中重算，不允许caller自报总hash。

### 15.5 ModelCallStart join

新增：

```python
class GovernanceModelInputAttributionFact(GovernanceEvidenceFrozenFact):
    schema_version: Literal["governance_model_input_attribution.v1"]
    governance_batch_prepared_event_reference: GovernanceStoredEventReferenceFact
    batch_input_reference: GovernanceBatchInputReferenceFact
    resolved_model_call_id: str
    target_fingerprint: str
    final_model_visible_input_fingerprint: str
    attribution_fingerprint: str
```

`ModelCallStartEvent`新增nullable `governance_input_attribution`，nullability matrix：

```text
resolved_call.purpose == MEMORY_GOVERNANCE
    => required
otherwise
    => forbidden
```

`MemoryGovernanceBatchPreparedEvent`必须先独立取得`FULL`。随后
`prepare_model_lifecycle_start_bundle()`构造ModelStart accounted batch，并通过
`GovernanceModelInputAttributionFact`精确引用已经FULL的prepared event。二者不得同批，
也不得在Prepared outcome仍为`NONE/UNKNOWN/PARTIAL`时启动provider。Start bundle必须验证：

- context ID由batch ID稳定派生；
- prepared event可按exact ID读取，且其sequence不大于ModelStart的pre-commit high-water；
- ModelStart的`resolved_call`与artifact内冻结的`ResolvedModelCallFact`逐字段相等；
- 实际`LLMContext`与artifact内`GovernanceModelInputFact`逐字段相等；
- 实际canonical input fingerprint等于`final_model_visible_input_fingerprint`；
- batch input reference与prepared event逐字段相等；
- resolved call purpose为`MEMORY_GOVERNANCE`。

### 15.6 Decision join

`MemoryGovernanceDecisionRecord`新增required：

```python
batch_input_fingerprint: str
batch_input_reference_fingerprint: str
governance_model_call_id: str
decision_index: int
decision_payload_fingerprint: str
```

Decision ID确定性派生：

```text
memory_governance_decision:
    sha256(batch_input_fingerprint, decision_index, decision_payload_fingerprint)
```

UOW必须验证：

- target candidate存在于matching batch snapshot；
- decision payload中的candidate内容与snapshot/allowed scopes相容；
- replacement evidence refs只能指向matching candidate snapshot的verified quote/evidence；
- destructive lifecycle target来自matching relatedness snapshot；
- model call Start/End与batch input identity一致；
-同decision ID重试payload byte-identical。

---

## 16. Batch preparation与执行算法

### 16.1 Preparation

```text
1. flush pending governance event outbox
2. transactionally select + claim pending candidate rows (excluding GOVERNANCE origin)
3. capture_governance_authority_snapshot() -> H
4. for each claimed candidate:
     build origin-aware evidence through H
5. classify build results
6. authority_untrusted -> latch/block whole batch
7. candidate_source_invalid -> atomically write system terminal record + claim TERMINAL
8. any not_ready among remaining claims -> retain all remaining claims as PREPARING,
   schedule same-batch retry and stop this preparation attempt
9. all remaining claims full -> collect relatedness snapshot
10. build bounded prompt projection
11. resolve exact Flash target and ResolvedModelCallFact
12. assemble exact system prompt, ordered messages and context ID
13. build GovernanceModelInputFact + GovernanceBatchInputSnapshotFact
14. put/read-confirm content-addressed artifact
15. atomically commit MemoryGovernanceBatchPreparedEvent + CAS all claims
    PREPARING -> PREPARED，并取得FULL
16. commit ModelStart with exact frozen resolved call/input attribution
17. consume committed model result
18. parse output against frozen candidate IDs
19. apply deterministic decision IDs through UOW
20. commit batch terminal event and CAS claims -> TERMINAL
```

空full set时不启动model call。Invalid records将对应claims终结为
`TERMINAL`；任一not-ready存在时不允许把同batch的FULL subset单独Prepared，
而是保留所有未终结`PREPARING` claims与retry owner。这保证一个batch ID
只对应一个确定的candidate set，不会在同batch下出现“部分Prepared、部分等待”。
Prepared event与claims的状态推进必须使用同一accounted PostgreSQL transaction；
如果存储层不能提供该bundle，不允许在应用层用两次commit仿写原子性。

### 16.2 Batch terminal events

新增：

```python
MemoryGovernanceBatchCompletedEvent
MemoryGovernanceBatchFailedEvent
MemoryGovernanceBatchBlockedEvent
```

三者共享：

- batch ID；
- prepared event ID；
- batch input fingerprint；
- governance model call ID（若已启动）；
- decision IDs；
- terminal reason enum；
- bounded diagnostics；
- terminal event fingerprint。

Exactly one terminal event per prepared batch。`Blocked`只用于artifact/authority在prepared后变得untrusted；pre-prepared authority latch不强行写event。

### 16.3 Cancellation

- caller cancellation不取消已开始的artifact write、ModelStart/terminal commit或decision UOW physical operation；
- UI subscriber detach不退休batch owner；
- cancellation前尚未Prepared FULL：保留stable artifact/event candidate；
- ModelStart FULL后取消：LLMRuntime完成terminal recovery；
- model terminal FULL但decision未完成：batch owner从durable terminal projection恢复output并继续apply；
- unknown commit保留owner与latch，不构造新batch ID覆盖旧winner。

### 16.4 Recovery

`MemoryGovernanceBatchRecoveryService`在RuntimeSession reopen时读取指定event types与exact refs，不全量scan ledger。

状态矩阵：

| durable state | recovery |
|---|---|
| artifact存在、matching claims preparing、Prepared缺失 | reopen接管原batch claim generation，hydrate exact artifact并重建byte-identical Prepared candidate，调用preparation commit port |
| artifact存在、无matching claim/owner | 验证不可达后才进入orphan artifact GC；不得仅因Prepared缺失立即删除 |
| claims preparing、artifact缺失 | reopen接管原batch claim generation，重试same artifact/input candidate或按confirmed absence执行durable claim release |
| Prepared存在、ModelStart缺失 | hydrate artifact内的exact `GovernanceModelInputFact`，rebind frozen target/provider并启动same call ID |
| ModelStart存在、ModelEnd缺失 | 交给ModelStreamRecoveryService |
| completed ModelEnd、batch terminal缺失 | 从terminal projection materialize输出、重跑parser/UOW |
|部分decision records存在 | 验证deterministic IDs，幂等补剩余decision |
|batch Completed存在 | no-op，验证decision集合 |
|payload conflict / unknown schema | authority latch |

Recovery不得调用当前clock重新生成prompt，不得从current model configuration
重新resolve call，不得重新跑relatedness，不得重新选择candidate batch。
Artifact内的resolved call/system prompt/messages/context ID是唯一recovery authority。

### 16.5 Close

Host/RuntimeSession close顺序：

```text
stop new governance batch admission
-> cancel retry timers
-> drain active evidence builders
-> drain batch-input artifact writers
-> drain batch execution/recovery owners
-> drain governance decision UOW/event outbox
-> close shared I/O services
```

任一步超时且仍有physical mutation时fail closed，不允许Host teardown越过。

---

## 17. Candidate lifecycle与GOVERNANCE audit rows

### 17.1 Pending定义

Candidate pending必须改为：

```text
origin != GOVERNANCE
and no MemoryGovernanceDecisionRecord terminal target
and no MemoryCandidateEvidenceRejectedRecord
and no active PREPARING/PREPARED/TERMINAL durable claim
```

`RELEASED` claim是历史audit row，不阻止candidate被后续batch重新claim；新claim必须
以旧claim fingerprint为`previous_claim_fingerprint`并递增generation。

### 17.2 GOVERNANCE origin

`CandidateOrigin.GOVERNANCE`保持现有语义：

- executor/UOW为correct/merge后的canonical write attribution创建；
- 不是新的proposal inbox项；
- 不进入`MemoryGovernanceInput`；
- 不触发recursive governance；
- 不需要provenance DAG、深度或循环策略。

它可以保存：

```python
class GovernanceDerivedWriteAttributionFact(...):
    parent_candidate_entry_ids: tuple[str, ...]
    governance_batch_id: str
    batch_input_fingerprint: str
    decision_id: str
    decision_payload_fingerprint: str
    attribution_fingerprint: str
```

该fact只供canonical write、Inspector与审计，不属于source evidence semantic union。

---

## 18. Inspector

Inspector新增bounded projection：

```text
memory_governance_batches[]
    batch_id
    status
    source_high_water
    candidate_count
    batch_input_fingerprint
    artifact status/ref
    model call id/outcome
    decision ids
    terminal reason

memory_governance_candidate_evidence[]
    candidate entry id/origin
    build status/reason
    evidence semantic fingerprint
    attribution fact fingerprint
    prompt projection fingerprint
    quote verification status
    accepted disposition ref
    tool-result ref

memory_governance_authority_latch
    status
    mismatch code
    bounded refs
    observed high-water
```

规则：

- historical Inspector只从durable batch artifact/events/records投影；
- process-local not-ready timer与owner状态只能由optional live diagnostics provider显示；
- 不从raw segment重建governance evidence；
- artifact缺失显示`artifact_missing`，不伪造snapshot；
- mismatch与candidate rejection明确区分。

---

## 19. Event与schema vocabulary

新增EventType：

```text
MEMORY_GOVERNANCE_BATCH_PREPARED
MEMORY_GOVERNANCE_BATCH_COMPLETED
MEMORY_GOVERNANCE_BATCH_FAILED
MEMORY_GOVERNANCE_BATCH_BLOCKED
MEMORY_CANDIDATE_EVIDENCE_REJECTED
```

更新：

- `MemoryReflectionCompletedEvent`；
- `ContextCompactionMemoryCandidatesProposedEvent`；
- `ModelCallStartEvent`；
- `AgentEvent` union；
- serialization/historical decoder/domain registry；
- PostgreSQL generated/expression indexes（batch ID、source tool-call ID、reflection ID）；
- Inspector event projection；
- EventLog storage contract。

这些governance batch events属于`non_transcript`。它们不得改变canonical transcript semantic accumulator。

---

## 20. PostgreSQL schema

### 20.1 新表

```sql
CREATE TABLE memory_candidate_evidence_rejections (...);
CREATE TABLE memory_candidate_projection_outbox (...);
CREATE TABLE memory_governance_candidate_claims (...);
CREATE TABLE memory_governance_batch_inputs (...);
```

`memory_candidate_projection_outbox`的producer kind只允许`reflection|compaction`，
并以`(runtime_session_id, producer_kind, producer_event_id, candidate_entry_id)`
唯一。每行保存stable producer identity、ordered candidate index、typed payload bytes/
fingerprint、projection status与last stable failure code。它与producer event、materialization
account transition只能通过`MemoryCandidateProjectionCommitPort`同transaction写入。

`memory_governance_candidate_claims`以candidate entry ID为主键，保存batch ID、claim
generation、candidate row fingerprint、status、Prepared/terminal refs与state fingerprint。
Claim selection与insert/CAS必须同transaction all-or-none；不允许存在另一个只在
Python内存的权威claim map。

`memory_governance_batch_inputs`保存bounded projection row：

- runtime session ID；
- batch ID unique；
- prepared event ID；
- artifact ID/hash/bytes；
- batch input fingerprint unique；
- source high-water；
- status；
- model call ID；
- terminal event ID；
- created/updated timestamps。

完整candidate/evidence payload只在content-addressed artifact中保存，不在row重复一份大JSON。

### 20.2 现有表hard cut

`memory_governance_decisions`新增：

- `batch_input_fingerprint NOT NULL`；
- `batch_input_reference_fingerprint NOT NULL`；
- `governance_model_call_id NOT NULL`；
- `decision_index NOT NULL`；
- `decision_payload_fingerprint NOT NULL`；
- unique(batch_input_fingerprint, decision_index)。

`memory_candidates`保留现有source columns作为row projection，但生产evidence不再读取自由metadata决定authority。Reflection/compaction producer必须同步保存typed carrier identity可索引字段。

`memory_candidates`新增typed `quoted_evidence_locator JSONB`；validator与数据库CHECK必须强制origin-specific nullability。旧`user_quote`列若保留，只是由locator hydrate得到的bounded display projection，不参与source authority或destructive lifecycle准入。

### 20.3 Reset

Pulsara尚未上线，本阶段不提供兼容migration：

- reset PostgreSQL；
- reset Oxigraph派生面；
- 删除旧candidate/governance rows；
- 删除旧checkpoint/artifact兼容reader；
- 不迁移旧`source_events` prompt snapshots。

---

## 21. 代码落点

新增模块建议：

```text
src/pulsara_agent/primitives/governance_evidence.py
src/pulsara_agent/memory/governance/evidence.py
src/pulsara_agent/memory/governance/batch_input.py
src/pulsara_agent/memory/governance/claims.py
src/pulsara_agent/memory/governance/recovery.py
src/pulsara_agent/memory/candidates/projection_outbox.py
```

主要修改：

```text
src/pulsara_agent/memory/governance/engine.py
src/pulsara_agent/memory/governance/coordinator.py
src/pulsara_agent/memory/governance/executor.py
src/pulsara_agent/memory/candidates/pool.py
src/pulsara_agent/memory/reflection/engine.py
src/pulsara_agent/runtime/compaction/candidates.py
src/pulsara_agent/runtime/compaction/service.py
src/pulsara_agent/tools/builtins/memory.py
src/pulsara_agent/runtime/authority_materialization/transcript_reducer.py
src/pulsara_agent/runtime/session.py
src/pulsara_agent/llm/lifecycle.py
src/pulsara_agent/event/events.py
src/pulsara_agent/inspector/service.py
```

长期合同同步：

```text
contracts/GOVERNANCE_WRITE_OUTBOX_CONTRACT.zh.md
contracts/MEMORY_SURFACES_CONTRACT.zh.md
contracts/RUNTIME_SEMANTIC_GRAPH_CONTRACT.zh.md
contracts/EVENT_LOG_STORAGE_CONTRACT.zh.md
contracts/LLM_TRANSPORT_CONTRACT.zh.md
```

---

## 22. GSE0-GSE5 实施顺序

### GSE0：DTO与contract additive

落地：

- semantic/attribution/prompt projection三层DTO；
- quoted evidence DTO；
- extractor contract；
- immutable candidate snapshot；
- batch input artifact/ref DTO；
- exact governance model input/system-prompt contract DTO；
- stored-event/artifact/leaf-entry reference DTO；
- main-agent candidate builder contract；
- durable claim与generic producer-outbox DTO；
- build status/reason enums；
- fingerprint factories与invariant matrix；
- architecture tests只验证新DTO，不切production。

完成定义：

- 每层只有一个own fingerprint；
- segment layout mutation只改变attribution测试通过；
-所有union/nullability/size bounds测试通过；
- no production behavior change。

### GSE1：Transcript provenance与原子snapshot

落地：

- accepted assistant entry保存disposition ref；
- schema/materialization hard cut；
- `capture_governance_authority_snapshot()`；
- document view与high-water单锁冻结；
- bounded exact reader through-H contract。

完成定义：

- snapshot竞态测试证明不会混入H+1；
- same semantics/different segment layout的stable semantic fingerprint相同；
- suppressed projection不存在于governance authority；
- full tests independently green。

### GSE2：Producer provenance hard cut

落地：

- reflection completed event candidate attribution；
- reflection/compaction event-first generic transactional outbox、commit port与dispatcher；
- compaction extractor binding与proposed event schema；
- deterministic main-agent candidate ID/builder binding；
- reset PostgreSQL/Oxigraph。

完成定义：

- reflection candidate不可早于completed FULL可见；
- crash/restart可从outbox幂等恢复；
- compaction candidate不可早于proposed event FULL可见；
- compaction replay drift fail closed；
- GOVERNANCE origin继续不pending；
- full tests independently green。

### GSE3：Origin-aware builder与prompt projection

落地：

- main/reflection/compaction builders；
- canonical quote span；
-五态build result；
- versioned prompt projection policy；
- relatedness frozen snapshot。

该PR只允许完全只读的shadow mode，新builder结果不改变任何生产状态。

Shadow可以capture snapshot、运行builder、构造prompt projection并记录bounded
operational metrics，但不得：

- acquire durable candidate claim；
- 写candidate rejection或任何runtime event/artifact；
- 安装authority latch；
- 启动not-ready timer/requeue；
- 影响candidate pending/status、model call或Host close。

Shadow mismatch/build failure只进入process-local bounded diagnostic。Authority latch、
rejection、retry timer的真实生产所有权统一在GSE4生效。

完成定义：

- candidate-scoped exact joins全部通过；
- raw segments/thinking不进入projection；
- builder不调用`EventLog.iter()`；
- failure classification matrix全覆盖；
- full tests independently green。

### GSE4：Durable batch input与execution owner

落地：

- durable candidate claim service、multi-process/reopen takeover；
- typed origin-aware evidence作为production input hard cut；
- candidate evidence rejection record与pending-query hard cut；
- authority latch、system-owned rejection、not-ready timer/backoff；
- exact target/call resolution与`GovernanceModelInputFact`；
- batch input artifact writer；
- Prepared/Completed/Failed/Blocked events；
- `MEMORY_GOVERNANCE` ModelStart governance attribution与required schema hard cut；
- decision record/UOW batch join；
- recovery owner与Host close drain；
- Inspector durable projection；
- 按required ModelStart/claim/batch schema再次reset PostgreSQL/Oxigraph并从空库切换。

GSE4是唯一production switch point。在该PR中同时切换batch selection/claim、
evidence preparation、model execution、decision apply、recovery与ModelStart schema
producer/consumer。不允许出现“新schema required但旧engine仍生产”的中间版本。

完成定义：

- model call与decision都能反查exact batch artifact；
- Prepared之前resolved call/system prompt/messages已完整冻结；
- 并发live/reopen/manual workers不能claim同一candidate；
- cancel-after-FULL、UNKNOWN、restart恢复测试通过；
- partial decision UOW幂等恢复；
- no model call before Prepared FULL；
- full tests independently green。

### GSE5：旧代码删除与合同收尾

落地：

-删除`_source_event_summaries()`；
-删除`_compaction_source_events()`的governance用途；
-删除raw `source_events` prompt contract；
-删除full-run scan与event attribute猜测；
-删除shadow/compat path；
-同步全部长期合同、Inspector与Real LLM tests。

GSE5不再引入production owner、schema required transition或新执行路径；GSE4已经完成
functional hard cut。本PR的职责是物理删除旧facade、shadow与grep allowlist，以及
完成文档/测试迁移。

完成定义：

-旧symbol/file/JSON key grep为零；
- architecture guards通过；
-全量pytest、Ruff、git diff --check通过；
- governance relatedness eval通过；
- Real LLM memory governance smoke通过；
-数据库从新schema启动。

---

## 23. 详细测试矩阵

### 23.1 Fingerprint

- same evidence semantics + different segment count -> same semantic fingerprint；
- different event IDs/sequences -> attribution fingerprint changes only；
- different prompt truncation -> projection fingerprint changes only；
- candidate payload change -> semantic/batch fingerprints change；
- artifact placement change -> attribution/batch fact change，semantic不变；
-每个fingerprint validator拒绝caller自报值。

### 23.2 Main tool

- memory candidate ID对same source identity/builder contract确定性一致；
- current `uuid4()` candidate ID生产路径为zero-match；
- builder ID/version/contract fingerprint缺失或不匹配fail closed；
- completed + ACCEPTED + matching tool result -> FULL；
- completed + SUPPRESSED_BY_TERMINATION -> candidate_source_invalid；
- provider_error/cancelled/runtime_error -> no accepted evidence；
- disposition缺失且run active -> NOT_READY；
- disposition缺失且RunEnd FULL -> candidate_source_invalid；
- tool result suspended -> NOT_READY；
- wrong tool name/call ID -> candidate_source_invalid；
- terminal artifact hash conflict -> authority_untrusted；
- multiplememory calls同reply精确分离；
- thinking与non-selected text不泄漏；
- canonical quote span exact match；
- duplicated text但wrong span被拒绝；
- mutable candidate user_quote不能覆盖canonical quote。

### 23.3 Reflection

- completion FULL前candidate不可见；
- event/account/outbox同transaction NONE时三者均不可见；
- event UNKNOWN保留owner；
- FULL后dispatcher crash/restart幂等补candidate；
- candidate payload conflict latch；
- ordered candidate indices/payload fingerprints join；
- origin-reported quote不授权supersede；
-可匹配canonical user span时升级canonical quote。

### 23.4 Compaction

- proposed event FULL前candidate不可见；
- proposed event NONE/UNKNOWN时不可直接append candidate；
- compaction outbox dispatcher crash/restart幂等；
- completed event + artifact + extractor binding full join；
- summary artifact missing/pending -> NOT_READY；
- confirmed hash conflict -> authority_untrusted；
- raw candidate index missing -> candidate_source_invalid；
- extractor implementation build变化但contract不变 -> compatible；
- semantic parser变化而version未升级 -> contract guard failure；
- replay candidate payload drift -> authority_untrusted。

### 23.5 Authority snapshot

- governance stored-event ref的schema identity/envelope fingerprint/sequence可独立重算；
- producer ref sequence > H时artifact validator直接拒绝；
- H读取与snapshot capture并发时只返回reducer H；
- reducer在capture后推进H+1不污染snapshot；
- committed但未fold event不被future-read加入；
- exact read sequence > H -> NOT_READY；
- document registry与stable entry under one lock；
- checkpoint/restart前后snapshot semantic equality；
- Cursor eviction不影响exact builder correctness。

### 23.6 Failure/lifecycle

- 两个live coordinators、manual worker与reopen worker并发claim同一candidate，只有一个batch winner；
- multi-candidate claim部分冲突时all-or-none；
- Preparing claim recovery接管原batch/generation，不选新batch；
- Prepared claim不得release；
- candidate invalid产生system-owned rejection，不产生LLM SkipDecision；
- rejection后candidate不再pending；
- authority untrusted阻止整个batch且不terminalize candidates；
- not-ready timer按0.5/1/2/.../30秒推进；
-新safe point不重置backoff；
-source high-water推进立即wake；
-close取消timer并drainbuilder。

### 23.7 Batch durability

- artifact未confirm不得Prepared；
- target/call resolution与exact model input artifact必须早于Prepared；
- Prepared FULL前不得ModelStart；
- Prepared存在、ModelStart缺失时，recovery不读current config且复用same call ID/target/input；
- historical target binding缺失时blocked，不替换model；
- ModelStart input attribution与artifact exact join；
- actual LLMContext messages fingerprint与snapshot相等；
- decision record target必须存在于batch snapshot；
- decision batch fingerprint漂移被UOW拒绝；
- cancellation after artifact FULL继续确认Prepared；
- cancellation after ModelEnd FULL恢复decision apply；
- decision 1 committed/decision 2 missing restart补齐；
- observer/publication failure不撤销durable winner；
- Host close不越过activeUOW或artifact writer。

### 23.8 Prompt bounds

- per-candidate character/byte bounds；
- aggregate 128 KiB bound；
- Unicode/head-tail deterministic；
- artifact refs最多8；
- unboundedtool output只进入typed essential envelope；
- thinking、raw provider diagnostics、segments均禁止；
- relatedness prompt必须包含canonical statement与node revision；
- relatedness只有memory ID/fingerprint时validator拒绝；
- doctor使用maximal canonical `LLMContext`、生产serializer/estimator验证所有
  production Flash target静态可行；
- bytes/token直接比较不存在于production doctor。

### 23.9 Architecture guards

禁止production governance模块出现：

```text
_source_event_summaries
"source_events"
event_log.iter(
.delta
TextBlockSegmentEvent
ThinkingBlockSegmentEvent
DataBlockSegmentEvent
ToolCallArgumentsSegmentEvent
hasattr(event,
getattr(event,
```

允许segment symbol只出现在LLM transport/reducer、diagnostic doctor与明确测试模块，不允许governance evidence builder导入。

禁止：

- `CandidateOrigin.GOVERNANCE`进入`list_pending()`；
- reflection engine直接`candidate_pool.append_candidate()`；
- compaction service直接`candidate_pool.append_candidate()`；
- main-agent memory candidate path使用`uuid4()`；
- governance pending selection绕过durable claim service；
- governance model call缺batch input attribution；
- decision record缺batch input fingerprint；
- prompt projection读取自由candidate metadata决定authority。

---

## 24. Definition of Done

本阶段只有同时满足以下条件才完成：

1. Governance source evidence拥有semantic、attribution、prompt projection三层独立identity；
2. segment/checkpoint/event sequence变化不污染evidence semantic fingerprint；
3. main-agent candidate只消费canonical accepted/pairing-complete transcript evidence；
4. canonical user quote有exact entry/span proof；
5. reflection candidate由durable completion/outbox投影，不存在candidate-first窗口；
6. compaction candidate同样由durable proposed event/outbox投影，并绑定summary hash与extractor ID/version/contract fingerprint；
7. reducer snapshot与high-water在同一锁域冻结；
8. invalid candidate与untrusted authority使用不同terminal/latch语义；
9. not-ready具有确定closure与coordinator-owned retry timer；
10. candidate selection与durable claim同transaction，live/recovery/manual owner不会重复治理；
11. full batch input以content-addressed artifact durable保存；
12. artifact在Prepared前冻结exact ResolvedModelCall、system prompt、ordered messages与context ID；
13. Prepared event、ModelStart、decision UOW与batch input fingerprint完整join；
14. `CandidateOrigin.GOVERNANCE`仍只作audit/provenance row；
15. prompt projection有per-item/per-batch bounds，relatedness含canonical content/revision，doctor按tokens验证；
16. `_source_event_summaries()`、raw source events、full-run governance scan物理删除；
17. runtime semantic graph与governed memory authority边界不变；
18. PostgreSQL/Oxigraph按新schema reset；
19. 全量测试、Ruff、`git diff --check`、architecture gates、governance eval与Real LLM smoke全部通过。

---

## 25. 最终架构

```text
EventLog
  |
  +-> TranscriptProjectionStateStore
  |      completed projection
  |      + ACCEPTED disposition
  |      + pairing-complete tool result
  |      -> atomic GovernanceTranscriptAuthoritySnapshot(H)
  |
  +-> ReflectionCompleted + generic transactional candidate outbox
  |
  +-> CompactionProposed + summary artifact + extractor contract
  |      `-> same generic transactional candidate outbox
  |
  `-> origin-aware GovernanceSourceEvidenceBuilder
          |
          +-> SemanticFact
          +-> AttributionFact
          `-> bounded PromptProjectionFact
                  |
                  `-> durable all-or-none candidate claims
                          |
                          `-> resolve exact target/call
                                  |
                                  `-> GovernanceModelInputFact
                                          |
                                          `-> GovernanceBatchInputSnapshot artifact
                          |
                          `-> Prepared event + claim CAS
                                  |
                                  `-> ModelCallStart exact binding
                                          |
                                          `-> Decision UOW exact binding
                                                  |
                                                  `-> governed canonical memory
```

这一结构的核心不是“给旧summary换一个typed名字”，而是彻底冻结：

```text
谁证明候选来源
什么属于业务语义
什么只是物理归因
模型实际看到了什么
决策最终绑定了哪份输入
```

只有这五个问题分别拥有唯一owner，governance source evidence才不会重新变成另一个字符串facade或第二真源。
