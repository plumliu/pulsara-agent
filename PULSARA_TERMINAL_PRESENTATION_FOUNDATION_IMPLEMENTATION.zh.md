# Pulsara Terminal Presentation Foundation Hard-Cut 实施规格

> 状态：IMPLEMENTED（2026-08-01；INFRA-0 至 INFRA-5 renderer-neutral Python infrastructure hard cut）
> Requirement namespace：`TUI-FND-*`
> 唯一owner：Python renderer-neutral presentation foundation、durable prompt queue与Terminal application services
> 上位产品基线：`PULSARA_TERMINAL_UI_UX_RESEARCH_AND_DESIGN.zh.md`
> 相邻契约：`PULSARA_TERMINAL_CLIENT_PROTOCOL_CONTRACT.zh.md`

## 0. 目的与裁决

本规格建立Bubble Tea、Legacy REPL及未来其他客户端共同依赖的Python基础设施。它不实现terminal layout，不拥有TTY，也不定义Protobuf wire representation。

本规格是`TUI-FND-*`的唯一normative implementation owner。上位产品文档保留调研、UX裁决与设计历史；凡涉及Python DTO字段、state transition、fingerprint、transaction、migration、gate或file cut，均以本文完整定义为准。本文不得用“见上位定义”替代中央carrier的逐字段契约；若上位摘要与本文冲突，必须在同一文档PR同步修正摘要。

Foundation必须做到：

1. RuntimeSession/EventLog继续是唯一durable truth与commit/reducer/publication gateway。
2. UI observation不进入`RuntimeEventPublisher`等待链，不反压durable writer。
3. `TranscriptProjectionStateStore`继续是canonical transcript acceptance、suppression、tool pairing与terminal-document join的唯一语义owner；presentation不得建立第二套transcript reducer。
4. Python presentation kernel只拥有canonical transcript leaf到cell的显示投影、registered durable-audit purpose classification与operational owner projection。
5. client只接收renderer-neutral projection，不接收`AgentEvent`、canonical transcript内部state或raw storage authority。
6. accepted prompt queue intent拥有EventLog transition authority、bounded checkpoint和exact CAS projection。
7. prompt submission、stop、interaction resolution与queue mutation通过closed application services进入Runtime。
8. Foundation不import `prompt_toolkit`、Textual、Bubble Tea、Go generated code或concrete client transport。

## 1. 范围与非目标

### 1.1 本规格拥有

- `RawStoredEventEnvelope`最终低层owner迁移；
- complete physical stored-batch receipt；
- confirmation candidate evidence与FULL classifier；
- `UiCommittedEventTap`、ring、bootstrap、catch-up和detach；
- durable/operational feed分层；
- canonical transcript committed-fold delta与presentation source join；
- renderer-neutral semantic cells、activity、interaction view和status values；
- bounded transcript viewport与paged history port；
- O(1) session snapshot；
- Terminal application command services；
- durable prompt queue transition authority、CAS rows、artifact holds、charge和checkpoint；
- queue consumption到RunStart/provider-input的原子边界；
- interaction event-safe view与Python secret hydration service boundary；
- close、reopen、repair和headless tests。

### 1.2 本规格不拥有

- Protobuf message、frame、transport和version negotiation；
- attachment/controller wire state；
- Go `Model/Update/View`；
- terminal宽度、line wrap、颜色、key binding、selection或mouse；
- Legacy REPL capability policy；
- desktop/web协议；
- arbitrary plugin renderer。

## 2. Hard cut 前代码真值（迁移基线）

本节记录实施前的migration observation，不再描述当前代码。当前最终owner、路径与验证证据以第14至17节及仓库内`tests/test_terminal_infrastructure_architecture.py`为准。

### 2.1 Event storage

- `EventWriteResult`当前位于`src/pulsara_agent/ports/event_write.py`。
- `EventBatchConfirmation`与`RawStoredEventEnvelope`当前位于`src/pulsara_agent/event_log/protocol.py`。
- PostgreSQL writer在`src/pulsara_agent/event_log/postgres.py`分配canonical sequence并构造transcript-prefix accounting。
- RuntimeSession在`src/pulsara_agent/runtime/session.py`把完整stored batch投影成caller-facing business与accounting events。

该缺口已由INFRA-1关闭：raw envelope现在贯穿encoder-built pair、physical commit receipt、exact confirmation与restored range proof；normal writer不再从decoded `AgentEvent`重新编码。

### 2.2 Runtime observation

- `HostSession.stream_turn()`只覆盖一个activation，不是session级reconnect bus。
- `RuntimeEventPublisher`逐subscriber等待，不可接入UI。
- run observer已有slow-consumer detach语义，可借鉴ownership但不可暴露内部owner。

### 2.3 Queue与checkpoint

- `HostIngressCoordinator`是live boundary仲裁器，不是durable user-intent queue。
- `runtime_projection_checkpoints`是validated mutable checkpoint carrier。
- `PhysicalOperationKind.CHECKPOINT_COMMIT`与`PostgresConnectionLane.CHECKPOINT_MAINTENANCE`已存在。
- ArtifactStore支持confirm-identical write和identity-bound delete，但没有prepared reference lease。

## 3. Package与依赖边界

目标package：

```text
src/pulsara_agent/primitives/stored_event.py
src/pulsara_agent/primitives/terminal_presentation.py
src/pulsara_agent/primitives/prompt_queue.py
src/pulsara_agent/ports/stored_event.py
src/pulsara_agent/ports/terminal_presentation.py
src/pulsara_agent/ports/terminal_commands.py
src/pulsara_agent/runtime/terminal_presentation/
    observation.py
    projection.py
    viewport.py
    snapshot.py
    application.py
    prompt_queue.py
    prompt_queue_checkpoint.py
src/pulsara_agent/event_log/postgres_prompt_queue.py
src/pulsara_agent/event_log/historical_decoder.py
```

依赖DAG：

```text
event_log stored receipt ------------------------\
authority_materialization canonical transcript ---+-> primitives.terminal_presentation
registered durable-audit purpose policy ----------/          -> ports.terminal_presentation
operational owner inventory --------------------------------> runtime.terminal_presentation
                                                              -> host composition

runtime.terminal_presentation -X-> client protocol adapter
runtime.terminal_presentation -X-> prompt_toolkit/Textual/Go
```

`RawStoredEventEnvelope`从`event_log.protocol`移动到`primitives.stored_event`。`event_log.protocol.RawStoredEventEnvelope`必须物理删除，不得re-export、alias或继续作为public import path；所有production/tests一次性切换最终owner。

该hard cut同时拆开DTO、构造与decode ownership：

```text
primitives.stored_event
  RawStoredEventEnvelope
  # pure frozen carrier；只做scalar、canonical bytes、wrapper和fingerprint验证

event_log.serialization
  build_raw_stored_event_envelope(...)
  hydrate_raw_stored_event_envelope_from_row(...)
  # 唯一current-write / stored-row factory

event_log.historical_decoder
  decode_raw_stored_event_envelope(...)
  # 唯一schema-registry-bound historical decode
```

`RawStoredEventEnvelope`不得再拥有`from_stored_event()`或`decode_owned()`方法。低层DTO不得import `AgentEvent`、`EventSchemaDomainRegistry`或historical decoder。正常write path复用首次构造的同一对象；exact candidate confirmation与generic restore均从canonical stored row hydrate值相等、canonical bytes与fingerprint完全相同的新对象，但只有前者在FULL时可构造physical batch receipt。任何路径都禁止从decoded `AgentEvent`重新编码raw envelope。

`StoredEventBatchCommitReceipt`与`JoinedRawStoredEventRangeProof`的最终owner是`ports.stored_event`。二者都是process-local storage carrier，不是durable/event-safe fact，因为`owned_stored_events`仍包含runtime-owned `AgentEvent`对象；禁止序列化、写入EventLog或跨Terminal protocol发送。`event_log.protocol`只拥有candidate match/evidence与transaction protocol，通过qualified imports引用raw envelope、receipt和range proof，不能反向定义或re-export它们。

## 4. Stored event canonical carrier

### TUI-FND-EVT-001 Raw envelope

`RawStoredEventEnvelope`是递归不可变、event-schema-bound的stored row carrier，至少冻结：

```text
stored_envelope_version
runtime_session_id
event_id
run_id
turn_id
reply_id
sequence
created_at_utc
event_type
event_schema_version
event_schema_fingerprint
event_domain_contract_fingerprint
canonical_payload_bytes
payload_fingerprint
envelope_fingerprint
```

字段名固定为`sequence`与`envelope_fingerprint`，不得再引入`canonical_sequence`或`stored_envelope_fingerprint`别名。唯一current-write构造点在EventLog分配sequence并按exact schema binding编码之后。PostgreSQL persistence、transcript-prefix accounting与normal commit receipt必须消费同一个对象和同一份`canonical_payload_bytes`，不得重新编码。

Exact candidate confirmation与generic restore无法保证Python object identity，也不要求“同一实例”。它们必须从PostgreSQL canonical stored row直接hydrate新envelope，并证明：

- 所有scalar wrapper字段相等；
- `canonical_payload_bytes`逐byte相等；
- payload与envelope fingerprints相等；
- exact historical schema/domain binding相等；
- 未经过decoded `AgentEvent -> encode`往返。

### TUI-FND-EVT-002 Complete physical batch receipt

```text
StoredEventBatchCommitReceipt
  owned_stored_events: tuple[AgentEvent, ...]
  raw_stored_envelopes: tuple[RawStoredEventEnvelope, ...]
  ordered_join_fingerprint: Fingerprint

StoredEventPairProof =
    EncoderBuiltStoredEventPair
  | DecoderHydratedStoredEventPair

EncoderBuiltStoredEventPair
  owned_stored_event
  raw_stored_envelope
  encoder_contract_fingerprint
  pair_fingerprint

DecoderHydratedStoredEventPair
  owned_stored_event
  raw_stored_envelope
  historical_decoder_contract_fingerprint
  pair_fingerprint
```

`StoredEventPairProof`是module-private、sealed、process-local carrier，不进入receipt、EventLog或wire。两个branch只能由各自唯一factory构造；普通caller不能直接实例化或伪造proof。Receipt factory只接受ordered pair proofs，并逐项验证：

- 数量相同且非空；
- runtime session、event ID、sequence、event type、schema binding一致；
- `canonical_json_bytes(owned_stored_event.model_dump(mode="json"))`逐byte等于raw envelope已经拥有的`canonical_payload_bytes`；只比较wrapper identity而payload不同必须拒绝；
- canonical sequence严格连续；
- pair proof与当前owned/raw对象identity及fingerprint一致；
- ordered join fingerprint由完整physical order中央重算。

`pair_fingerprint = H("stored-event-pair-proof:v1", proof branch, runtime_session_id, event_id, sequence, event_type/schema/domain identity, payload_fingerprint, envelope_fingerprint, encoder_or_decoder_contract_fingerprint)`。它是本次process内的proof identity，不取代raw envelope或receipt fingerprint。

只有normal write与仍持有exact prepared candidate batch identity的FULL confirmation可以收敛为相同的`StoredEventBatchCommitReceipt`形状，证明算法不同：

```text
normal write
  sequence allocation
    -> event_log encoder一次生成owned stored event + raw envelope
    -> sealed EncoderBuiltStoredEventPair
    -> persistence/accounting
    -> receipt scalar/fingerprint validation
    -> 不调用historical decoder

exact candidate FULL confirmation
  canonical stored row
    -> hydrate raw envelope
    -> historical decoder恢复owned stored event
    -> sealed DecoderHydratedStoredEventPair
    -> receipt scalar/fingerprint validation
```

Normal write的pair factory必须位于完成canonical encode的同一EventLog implementation boundary，并复用已验证的schema binding与canonical bytes。Exact candidate confirmation只能调用`event_log.historical_decoder.decode_raw_stored_event_envelope()`完成owned/raw join，并且必须用prepared candidate的ordered event IDs/payload fingerprints证明这些连续rows就是该candidate batch。`RawStoredEventEnvelope`自身没有decode capability；normal writer不得为了构造receipt再次JSON decode。

`agent_events`不新增batch ID、batch ordinal或batch size。Generic reopen、doctor、repair与bounded catch-up没有原始transaction batch identity，绝不能把单行、SQL page或任意连续chunk包装成`StoredEventBatchCommitReceipt`。这些路径只能使用`JoinedRawStoredEventRangeProof`。

### TUI-FND-EVT-003 Caller projection

```text
EventWriteResult
  committed_events                 # business subset
  accounting_events                # materialization subset
  stored_batch_receipt             # complete physical batch
  business_accounting_partition_fingerprint
  existing reducer/publication/confirmation fields
```

business与accounting projection必须是`owned_stored_events`的disjoint、order-preserving、exhaustive partition。UI tap entry消费complete receipt的全部raw envelopes，因此accounting sequence不会形成假gap；registered presentation policy可以把accounting event的durable-audit purpose设为`noop`，但canonical transcript projection只消费transcript reducer的fold result。

### TUI-FND-EVT-004 Candidate confirmation evidence

Partial confirmation不能冒充physical batch receipt：

```text
StoredEventCandidateMatch
  candidate_index
  candidate_event_id
  candidate_payload_fingerprint
  owned_stored_event
  raw_stored_envelope
  join_fingerprint

EventBatchConfirmationEvidence
  exact_ordered_candidate_batch_fingerprint
  matched_candidates
  missing_event_ids
  actual_last_sequence
  evidence_fingerprint

EventBatchConfirmationDisposition =
    FULL | NONE | PARTIAL | CONFLICT | UNAVAILABLE

ConfirmedFullStoredBatch
  receipt: StoredEventBatchCommitReceipt
  confirmation_evidence_fingerprint
  classifier_contract_fingerprint
```

`StoredEventCandidateMatch`只能由`DecoderHydratedStoredEventPair`与exact prepared candidate共同构造；FULL classifier从ordered match内的同一pair proofs形成receipt，不接受caller重新拼接owned/raw对象。Stored payload比较算法固定为：验证assigned sequence/order后，把stored event的sequence规范化回`None`，使用exact historical schema重新编码，再与pre-commit candidate fingerprint比较。

唯一classifier矩阵：

- 所有candidate exact match、顺序一致且sequence连续：`FULL`；
- 所有candidate missing：`NONE`；
- exact非空subset加missing subset：`PARTIAL`；
- 同ID不同payload、schema drift、duplicate index、顺序或连续性错误：`CONFLICT`；
- physical read未完成：`UNAVAILABLE`。

只有FULL可构造receipt并进入tap。NONE允许same stable candidate retry；PARTIAL/CONFLICT进入ledger reconciliation；UNAVAILABLE保留owner并重试read。

### TUI-FND-EVT-005 Restored contiguous range proof

```text
JoinedRawStoredEventRangeProof
  runtime_session_id
  source_kind: reopen_restore | runtime_catch_up | doctor | repair
  from_sequence_exclusive
  through_sequence
  owned_stored_events: tuple[AgentEvent, ...]
  raw_stored_envelopes: tuple[RawStoredEventEnvelope, ...]
  historical_decoder_id
  historical_decoder_version
  historical_decoder_contract_fingerprint
  ordered_range_envelope_accumulator
  range_proof_fingerprint
```

该proof是process-local、非durable carrier，由EventLog-owned bounded raw-row reader与historical decoder共同构造。Validator必须证明：

- tuple非空、owned/raw数量相同，且每项runtime session、event ID、sequence、schema/domain identity逐项join；
- sequence严格连续并精确覆盖`(from_sequence_exclusive, through_sequence]`；
- 每个owned event来自对应raw row的exact historical decode，不经过owned-event re-encode；
- decoder ID/version/fingerprint可从historical registry exact resolve；
- `ordered_range_envelope_accumulator = H("joined-raw-stored-event-range:v1", runtime_session_id, from/through sequence, ordered tuple[(sequence, event_id, envelope_fingerprint)])`；
- `range_proof_fingerprint`覆盖source kind、range identity、decoder binding与ordered accumulator，但不声称或推导physical transaction boundary。

SQL page、checkpoint delta与doctor page可以形成任意大小的连续range proof；分页边界不得进入canonical transcript semantic state。Empty range使用typed no-op disposition，不构造空proof。Range proof不能进入`UiCommittedEventTap`、不能用于构造`ConfirmedFullStoredBatch`，也不能作为EventLog transaction receipt。

## 5. Non-blocking committed tap

### TUI-FND-OBS-001 安装点

```text
EventLog FULL
  -> RuntimeSession installs complete receipt
  -> TranscriptProjectionStateStore.apply_live_committed(receipt) returns live fold result
  -> other committed reducers fold owned events
  -> build CommittedPresentationTapEntry(receipt, live fold result)
  -> UiCommittedEventTap.offer_nowait(entry)
  -> existing publisher enqueue/delivery
```

`UiCommittedEventTap`只是non-blocking transfer owner，不是transcript reducer。它只保存一个原子复合entry：

```text
CommittedPresentationTapEntry
  schema_version
  runtime_session_id
  source_first_sequence
  source_last_sequence
  raw_stored_envelopes: tuple[RawStoredEventEnvelope, ...]
  stored_batch_ordered_join_fingerprint
  canonical_fold_result: LiveCommittedFoldResult
  tap_entry_fingerprint
```

Central factory必须验证raw envelope tuple与complete receipt逐项相同，并验证fold result的source batch identity、first/last sequence与envelope accumulator完全一致。`tap_entry_fingerprint = H("committed-presentation-tap-entry:v1", runtime_session_id, first/last sequence, stored batch ordered join fingerprint, source envelope accumulator, fold_result_fingerprint)`，不得覆盖subscriber、ring generation或delivery timing。某个physical batch可以产生empty transcript leaf delta，但仍必须有覆盖同一receipt的fold result。Ring eviction、subscriber delivery和bootstrap copy都以完整entry为最小单位；raw evidence与canonical fold result不得分开存放或独立淘汰。

Foundation background consumer只能在HostSession成功publish并成为live owner后激活；RuntimeSession构造或尚未publish的Host open attempt不得启动async worker，否则failed-open同步rollback无法证明physical drain。Publish前已经FULL的receipt仍由tap ring/monotonic observed high-water保留，worker激活后走同一bounded bootstrap/catch-up，不建立第二条replay路径。Gateway attach可以幂等确保worker已启动，但不能成为唯一projection owner。

`offer_nowait()`不得：

- await；
- 执行I/O；
- serialize/decode event；
- 调用renderer或client callback；
- 获取client-owned lock；
- 抛出影响commit结果的异常。

### TUI-FND-OBS-002 Ring与ingestion

Tap维护session-scoped bounded ring，按sequence覆盖范围保存完整`CommittedPresentationTapEntry`并计算aggregate bytes。Entry ingestion矩阵：

- exact next entry：append；
- 整个entry的sequence范围和`tap_entry_fingerprint`已存在：no-op；
- 任意partial overlap，即使重叠envelope完全相同：不得拆分entry，detach generation并要求ledger catch-up/rebootstrap；
- gap、同sequence不同envelope fingerprint或same batch identity不同fold result：detach generation，要求ledger catch-up/rebootstrap；
- capacity overflow：推进ring floor并通知observer gap，不阻塞writer。

Tap必须在ring之外维护单调的`latest_valid_observed_sequence`。该值只在完整receipt/tap-entry重新验证成功后推进，但不因ring eviction、generation rollover或subscriber detach回退。Subscriber进入GAP时，Foundation保留已经FULL的checkpoint/root installation，先退休该次exact delivery owner，再以当前projection high-water和`latest_valid_observed_sequence`重新bootstrap；缺失suffix只能由canonical raw range proof恢复。Background worker不能因为旧subscriber被detach而继续空转，也不能把`_worker is not None`当作“已经重新订阅”的证明。每次正常wake应冻结并fold当前全部pending whole entries，再形成一个checkpoint candidate；不得对每条tap entry串行执行一次artifact/SQL checkpoint后才ack，否则正常burst会把bounded subscriber人为推入GAP。

### TUI-FND-OBS-003 Bootstrap线性化

```text
1. restore bounded durable checkpoint/snapshot exactly through H
2. under tap lock install subscriber in CATCHING_UP state and freeze current ring head R
3. concurrent new commits enter a whole-entry catch-up buffer
4. choose the earliest retained whole live entry E whose source_first_sequence > H
5. if an entry overlaps H, never split it; read canonical raw rows from H+1 through E.first-1
6. hydrate bounded contiguous pages as JoinedRawStoredEventRangeProof and call fold_restored_range()
7. process durable-audit purpose for the same restored ranges and advance high-water only after both axes succeed
8. once state through_sequence == E.first-1, ingest E and later whole live entries by exact next-entry join
9. if no whole E is retained, range-fold through R and wait for the next future entry beginning at R+1
10. re-acquire tap lock, drain whole buffered entries, validate continuity, then switch LIVE
11. publish bootstrap result or typed gap/rebuild outcome
```

durable snapshot与operational owner inventory不是同一原子snapshot。前者使用`authority_high_water`，后者使用独立generation/cursor/fingerprint。

Durable bootstrap中的canonical transcript部分必须来自现有transcript checkpoint/root manifest加bounded transcript-domain delta，并由`TranscriptProjectionStateStore.fold_restored_range()`恢复；presentation不得直接用raw catch-up events重建messages。Raw catch-up同时为physical continuity和registered durable-audit extractor供给证据。Restored range与首个whole live entry只按`through_sequence + 1 == source_first_sequence`、before/after state fingerprint和reducer/registry contract join；不得要求、推测或合成source batch fingerprint。

已被range fold覆盖的ring/buffer entry必须按sequence/envelope fingerprint证明完全相同后丢弃，不能再次应用；与range边界发生partial overlap的entry永远不进入tap consumer。只有从下一条完整live entry开始，bootstrap才恢复使用`CommittedPresentationTapEntry`及真实batch fingerprint。Ring覆盖不足时可以继续bounded EventLog page read或重建更新snapshot，但任何page都不得伪装为physical batch receipt。

## 6. Canonical transcript、durable audit与operational feed

### TUI-FND-OBS-004 三路输入

Foundation只接受以下三类carrier；carrier ownership互斥，但同一个durable事件是否参与canonical transcript与durable audit是两个独立projection purpose，绝不按事件类型强制互斥：

```text
canonical transcript projection input
  <- TranscriptProjectionStateStore live/restored fold result

durable audit input
  <- registered presentation purpose policy + typed audit extractor + raw stored envelopes

operational input
  <- OperationalUiFrame
```

三类carrier不得互相回退：

- raw envelope的durable-audit extractor不得自行重建message content、acceptance或tool pairing，即使同一个事件也由canonical transcript reducer消费；
- canonical transcript delta不得夹带live owner inventory；
- operational frame不得生成durable transcript leaf、推进durable authority high-water或在reopen时冒充历史事实。

### TUI-FND-OBS-005 Canonical transcript live/restore fold

`TranscriptProjectionStateStore`必须提供两个不可混用的入口，并在同一reducer lock内调用一个grouping-independent pure fold core：

```text
apply_live_committed(
  receipt: StoredEventBatchCommitReceipt,
) -> LiveCommittedFoldResult

fold_restored_range(
  range_proof: JoinedRawStoredEventRangeProof,
) -> RestoredRangeFoldResult

CanonicalTranscriptFoldDeltaFact
  schema_version
  runtime_session_id
  from_sequence_exclusive
  through_sequence
  reducer_contract_fingerprint
  event_registry_contract_fingerprint
  before_live_assembly_fingerprint
  after_live_assembly_fingerprint
  before_stable_state_fingerprint
  after_stable_state_fingerprint
  before_canonical_spine_fingerprint
  after_canonical_spine_fingerprint
  ordered_leaf_changes: tuple[CanonicalTranscriptLeafChangeFact, ...]
  ordered_placement_transition_proofs: tuple[CanonicalTranscriptPlacementTransitionProofFact, ...]
  ordered_audit_dispositions: tuple[TranscriptAuditDispositionFact, ...]
  resulting_canonical_state_fingerprint
  fold_delta_fingerprint

LiveCommittedFoldResult
  source_stored_batch_ordered_join_fingerprint
  source_first_sequence
  source_last_sequence
  source_envelope_accumulator
  fold_delta: CanonicalTranscriptFoldDeltaFact
  live_result_fingerprint

RestoredRangeFoldResult
  source_range_proof_fingerprint
  source_first_sequence
  source_last_sequence
  source_envelope_accumulator
  fold_delta: CanonicalTranscriptFoldDeltaFact
  restored_result_fingerprint

CanonicalTranscriptLeafChangeFact =
    TranscriptLeafAppendedFact
  | TranscriptLeafReplacedFact
  | TranscriptLeafRetiredFact

CanonicalTranscriptPlacementAnchorReferenceFact
  schema_version
  placement_key_contract_id / version / fingerprint
  transcript_anchor_id
  stable_anchor_slot_key
  stable_first_spine_coordinate / stable_last_spine_coordinate
  anchor_fingerprint
  anchor_reference_fingerprint

CanonicalTranscriptPlacementAnchorTombstoneFact
  schema_version
  placement_key_contract_id / version / fingerprint
  retired_anchor_reference: CanonicalTranscriptPlacementAnchorReferenceFact
  stable_anchor_slot_key
  stable_first_spine_coordinate / stable_last_spine_coordinate
  retired_by_source_reference
  replacement_anchor_reference: CanonicalTranscriptPlacementAnchorReferenceFact | None
  tombstone_fingerprint

CanonicalTranscriptPlacementTransitionProofFact
  schema_version
  placement_key_contract_id / version / fingerprint
  transition_kind: append | single_replace | interval_replace | retire_to_tombstone
  before_canonical_spine_fingerprint
  after_canonical_spine_fingerprint
  ordered_predecessor_anchor_references: tuple[CanonicalTranscriptPlacementAnchorReferenceFact, ...]
  resulting_anchor_reference: CanonicalTranscriptPlacementAnchorReferenceFact | None
  resulting_anchor_tombstones: tuple[CanonicalTranscriptPlacementAnchorTombstoneFact, ...]
  reducer_id / reducer_version / reducer_contract_fingerprint
  transition_proof_fingerprint

TranscriptAuditDispositionFact =
    SuppressedModelOutputAuditFact
  | RecoveredTranscriptAuditFact
  | RejectedTranscriptCandidateAuditFact
```

两个入口先验证`source_first_sequence == current through_sequence + 1`，再把ordered `(owned stored event, raw envelope)` pairs交给同一个private fold core。Pure core不得读取receipt fingerprint、range proof fingerprint、source kind、SQL page size或physical batch length；它只消费canonical order、event/schema/domain identity与payload。Live wrapper从receipt中央重算source fields并要求source batch fingerprint等于receipt ordered join fingerprint；restore wrapper从range proof重算相同sequence/envelope fields，但只引用range proof fingerprint。

`source_envelope_accumulator = H("canonical-transcript-fold-source:v1", runtime_session_id, ordered tuple[(sequence, event_id, envelope_fingerprint)])`，不得使用caller accumulator。相同checkpoint/base state与相同ordered event range，无论按原始live batches、单个range、逐事件range或任意bounded contiguous pages输入，都必须得到相同resulting canonical state、stable/live assembly fingerprints、leaf set、semantic/continuity accumulators、canonical-spine fingerprints与placement transition proofs；只有live/range wrapper attribution与每页delta fingerprint可以不同。每个leaf change必须携带完整`TranscriptProjectionLeafEntryFact`或其exact previous/resulting references、transcript-owned ordinal、source-event accumulator和fact fingerprint。每个audit disposition必须由transcript reducer已经决定的accepted/suppressed/recovery状态构造；presentation不能从control event重新推导。

`CanonicalTranscriptPlacementTransitionProofFact`只能由transcript reducer与leaf change在同一次fold中生成。Validator冻结以下矩阵：append没有predecessor且产生一个新anchor/slot；single replacement有且仅有一个predecessor并继承其stable slot与spine coordinate；interval replacement的predecessor按spine顺序连续、无重叠，resulting anchor继承完整covered interval并按registered slot-key derivation从ordered predecessor slots生成唯一slot；retirement不把坐标或slot删除，而是产生不可渲染的stable tombstone。Tombstone仍是before/after audit anchor的合法placement coordinate，但永远不生成canonical transcript cell。Proof中的placement-key contract三元组、before/after spine fingerprints必须与fold delta/current binding逐项相等，reducer三元组必须与本次fold binding exact join。缺失proof、coordinate/slot重分配、predecessor不连续、contract drift或retirement未保留tombstone一律`RECONCILIATION_REQUIRED`。

旧`apply_committed(tuple[AgentEvent, ...])`与`rebuild(tuple[AgentEvent, ...])`必须物理删除。任何live path只能调用`apply_live_committed(receipt)`；任何reopen/catch-up/doctor/repair只能从checkpoint/base恢复后调用一个或多个`fold_restored_range(range_proof)`。不得在restore代码中为任意page构造fake receipt。

RuntimeSession generic committed-reducer registration必须在同一INFRA-2 hard cut升级为双入口port：

```text
CommittedReducerIngressPort
  apply_live_committed(receipt: StoredEventBatchCommitReceipt)
  fold_restored_range(range_proof: JoinedRawStoredEventRangeProof)

register_committed_reducer(
  reducer_id,
  through_sequence,
  ingress: CommittedReducerIngressPort,
  rebuild_owner: CommittedReducerRebuildPort | None,
)

CommittedReducerRebuildPort
  rebuild_to(
    target_through_sequence,
    absolute_deadline,
  ) -> CommittedReducerRebuildReceipt

CommittedReducerRebuildReceipt
  reducer_id
  reducer_contract_fingerprint
  restored_base_identity
  base_through_sequence
  target_through_sequence
  ordered_range_proof_accumulator
  resulting_state_fingerprint
  rebuild_receipt_fingerprint
```

RuntimeSession普通commit与exact candidate FULL confirmation调用`apply_live_committed()`；registration落后、initial catch-up、reconcile与repair调用`fold_restored_range()`。若当前live receipt前存在missing interval，必须先分页构造range proofs并fold到`receipt.first_sequence - 1`，再应用live receipt。`_CommittedReducerRegistration`不得继续保存一个同时接收live/catch-up tuple的ambiguous callback。

现有只消费owned events的其他domain reducer可以通过唯一`GroupingIndependentOwnedEventReducerAdapter`接入：adapter在两个方法中分别提取receipt/range proof的`owned_stored_events`，但只有通过“不同contiguous partition得到相同state fingerprint”的contract test后才能注册。Transcript reducer直接实现双入口，不经过该adapter。旧`rebuild_committed(tuple)`改为domain-owned `CommittedReducerRebuildPort`：其内部从可信checkpoint/genesis初始化，再消费bounded range-proof pages；无checkpoint的privileged repair也只能reset后分页fold，不能把完整ledger装入一个tuple。RuntimeSession只在receipt的reducer identity/contract、target through sequence与当前registered owner exact join后更新high-water；rebuild owner不得返回event tuple作为证明。

`UserPromptCell`、`AssistantMessageCell`与`ToolTerminalCell`只能从该fold result中的canonical leaf change派生：

- user/assistant cell只消费`TranscriptMessageLeafEntryFact`；
- tool terminal cell只消费`TranscriptToolResultLeafEntryFact`及其exact terminal projection reference；
- tool grouping只消费`TranscriptToolPairLeafEntryFact`，不得按tool-call ID在UI侧重新配对；
- terminal document hydration通过transcript-owned bounded document view/port完成，不允许presentation扫描EventLog或猜测latest projection；
- suppressed output若产品选择展示，只能由`TranscriptAuditDispositionFact`投影为显式`AuditCell`，永不成为assistant transcript cell。

`TranscriptProjectionStateStore`仍唯一决定：completed projection是否有`ACCEPTED` disposition、suppressed output是否排除、tool call/result pairing、terminal document exact join、compaction/long-horizon后canonical leaf set。Presentation只决定这些已冻结事实如何显示。

### TUI-FND-OBS-006 Durable audit feed

Raw stored envelopes只进入registered presentation event policy。Registry按exact event type/schema/domain identity冻结两个互不排斥的purpose axis：

```text
PresentationEventPolicyFact
  schema_version
  event_type
  event_schema_version
  event_schema_fingerprint
  event_domain_contract_fingerprint
  canonical_transcript_handling: reducer_owned | irrelevant
  durable_audit_handling: noop | typed_extractor_binding
  durable_audit_extractor_id: str | None
  durable_audit_extractor_version: str | None
  durable_audit_extractor_contract_fingerprint: Fingerprint | None
  allowed_audit_placement_request_kinds: tuple[before_leaf | after_leaf | ledger_sequence, ...]
  audit_anchor_resolution_contract_fingerprint: Fingerprint | None
  allowed_audit_field_path_accumulator
  allowed_output_cell_kinds
  policy_fingerprint
```

两个axis的矩阵固定为：

- `RunStartEvent`与`RunEndEvent`是`reducer_owned + typed_extractor_binding`：transcript reducer产生user/recovery canonical leaves，受限extractor只读取run lifecycle/status字段；
- model/tool transcript语义可为`reducer_owned + noop`，其accepted/suppressed/pairing结果只从canonical fold result进入UI；
- approval、plan、MCP interaction、compaction boundary和physical failure通常为`irrelevant + typed_extractor_binding`；
- accounting/physical bookkeeping通常为`irrelevant + noop`；
- missing policy、schema/domain drift、unknown extractor或不合法axis组合均`unsupported_fail_closed`，detach/rebuild，不猜测。

当`durable_audit_handling == noop`时，extractor ID/version/fingerprint与anchor-resolution fingerprint必须全部为`None`，allowed placement request kinds/field paths/output kinds为空。当它等于`typed_extractor_binding`时，三项extractor identity、non-empty closed request-kind set与anchor-resolution contract全部required。`policy_fingerprint`必须覆盖extractor ID、version、contract fingerprint、allowed placement request kinds、anchor-resolution contract、allowed field accumulator与allowed output kinds，不能只覆盖opaque hash。

Process-local `PresentationAuditExtractorRegistry`以`(extractor_id, extractor_version)`为唯一key，并提供：

```text
resolve_exact(
  extractor_id,
  extractor_version,
  extractor_contract_fingerprint,
) -> PresentationAuditExtractor
```

同一ID/version注册不同contract fingerprint是composition configuration conflict，Host/Gateway不得READY。Current registry必须保留仍可被retained EventLog schema、presentation checkpoint/root/tree引用的historical extractor binding；restore按policy中的完整三元组exact resolve，缺失历史binding时返回`REBASE_REQUIRED`或`RECONCILIATION_REQUIRED`，不能用current version代替。Audit extractor registry contract fingerprint覆盖sorted完整binding set及每个binding允许的event schema/domain keys；presentation policy registry则覆盖sorted policy matrix、allowed placement request kinds与anchor-resolution contracts，两个fingerprint不得合并。

Typed audit extractor是closed registry binding，只能读取policy allowlist中的非正文field path，并且其output union不得包含`UserPromptCell`、`AssistantMessageCell`或`ToolTerminalCell`。它可以生成`InteractionCell`、`CompactionBoundaryCell`、`RecoveryCell`、`ErrorCell`、`AuditCell`或`SystemNoticeCell`，但不能改变canonical transcript acceptance、pairing、order或content。Run lifecycle的唯一合法输出是`AuditCell(audit_kind="run_lifecycle")`，`RunLifecycleCell`不存在。Policy registry与extractor registry的两个独立fingerprint都进入`PresentationHistoryProjectionRootFact`；snapshot携带exact root identity，history cursor只通过root fingerprint间接绑定它们。

一个tap entry的`authority_high_water`只有在以下条件全部满足后才能推进到`source_last_sequence`：canonical fold result已验证完整source receipt；entry中每个raw envelope都命中exact policy；每个`typed_extractor_binding`均成功产生合法bounded output或合法empty disposition；每个`noop`均被显式消费。任一axis失败都返回typed rebuild/reconciliation，不允许只提交transcript change而丢失同一RunStart/RunEnd的lifecycle change。

### TUI-FND-OBS-007 Operational feed

Operational frame用于model token preview、elapsed time、terminal output tail、subagent progress等尚未durable terminalize的信息。每帧必须携带：

```text
runtime_session_id
operational_generation
operational_cursor
owner_kind / owner_id / owner_generation
coalesce_key
bounded payload
expires_or_replaced semantics
```

它不使用EventLog sequence，不推进`authority_high_water`，可被coalesce/drop。durable terminal projection到达后必须退休对应live frame。

## 7. Presentation semantics

### TUI-FND-PROJ-001 Presentation-owned semantics

Python presentation kernel在不越过canonical transcript reducer的前提下唯一决定：

- cell kind、stable cell ID和source references；
- canonical tool leaf的display subtype，以及durable-audit error/approval/plan/MCP/subagent/compaction语义分类；
- semantic group identity和group membership；
- severity与不可隐藏标志；
- event-safe public content；
- security redaction、byte/token cap；
- status segment logical value；
- interaction request view；
- notification priority和dedupe identity。

Source ownership矩阵固定为：

| Cell branch | 唯一事实来源 | Presentation可决定 | Presentation禁止决定 |
|---|---|---|---|
| `UserPromptCell` | canonical transcript message leaf | label、group、bounded display blocks | user occurrence、顺序、content authority |
| `AssistantMessageCell` | canonical accepted assistant leaf | display grouping、severity | accepted/suppressed、latest winner |
| `ToolTerminalCell` | canonical tool-result leaf + pair leaf | visual group与summary policy | call/result pairing、terminal projection winner |
| `AuditCell` | registered audit event或transcript audit disposition | audit kind、severity、visibility | 把audit升级为canonical transcript |
| operational activity cells | operational frame | coalescing与expiry display | durable completion、history placement key或root-local display rank |

它不得决定：

- terminal line wrap；
- 颜色、border、padding；
- client width/height layout；
- selection、scroll或follow-tail preference；
- collapse preference；
- key binding、mouse或clipboard。

### TUI-FND-PROJ-002 Durable/operational closed unions

```text
DurableHistoryCell =
    UserPromptCell
  | AssistantMessageCell
  | ToolTerminalCell
  | ErrorCell
  | InteractionCell
  | CompactionBoundaryCell
  | RecoveryCell
  | AuditCell
  | SystemNoticeCell

OperationalActivityCell =
    ModelActivityCell
  | ToolActivityCell
  | TerminalProcessActivityCell
  | SubagentActivityCell
```

两个union必须是不相交的Python类型与wire oneof：

- `DurableHistoryCell`每个branch都包含stable ID、semantic revision、ordered durable source refs、source accumulator、visibility policy、bounded content blocks和optional group identity，可进入unified history root、checkpoint与page；
- `OperationalActivityCell`必须携带owner identity/generation、operational cursor、coalesce key与expiry/replacement semantics，不得携带history ordinal、history cursor或projection-root reference；
- 旧`TerminalSemanticCell`扁平union必须物理删除，不得通过optional durable fields同时表达两类lifecycle；
- `RunLifecycleCell`不是类型或wire branch。RunStart/RunEnd lifecycle必须投影为`AuditCell(audit_kind="run_lifecycle")`；
- Raw reasoning/private chain-of-thought永不进入任何public content。

### TUI-FND-PROJ-003 Projection revision

`projection_revision`是client-visible projection变更版本，不等于EventLog sequence：

- 一个durable event可产生零个、一个或多个projection changes；
- durable history、interaction、queue或其他client-visible durable/application projection change可以不与EventLog sequence 1:1；
- reducer每次发布non-empty durable/application ordered delta时CAS推进revision；
- duplicate input不推进revision；
- snapshot携带`authority_high_water`与`projection_revision`。

Operational activity只推进自己的`operational_generation/cursor`，不推进`projection_revision`，不生成durable history delta。

### TUI-FND-PROJ-004 Unified history placement root

Canonical transcript reducer和durable-audit extractor分别保持语义ownership；唯一`PresentationHistoryProjectionOwner`只拥有它们输出在终端durable history中的全局placement/order。它不得改写transcript leaf、audit cell、acceptance、pairing、severity或content。

```text
CanonicalTranscriptPlacementAnchorFact
  schema_version
  placement_key_contract_id / version / fingerprint
  transcript_anchor_id
  stable_anchor_slot_key
  anchor_kind: appended | single_replacement | interval_replacement
  stable_first_spine_coordinate / stable_last_spine_coordinate
  first_ordering_boundary_sequence / last_ordering_boundary_sequence
  inherited_anchor_fingerprint_accumulator
  anchor_fingerprint

AuditHistoryPlacementRequestFact =
    BeforeTranscriptLeafAuditPlacementRequestFact
  | AfterTranscriptLeafAuditPlacementRequestFact
  | LedgerSequenceAuditPlacementRequestFact

BeforeTranscriptLeafAuditPlacementRequestFact
  request_kind = before_leaf
  target_transcript_leaf_reference
  audit_local_ordinal
  request_fingerprint

AfterTranscriptLeafAuditPlacementRequestFact
  request_kind = after_leaf
  target_transcript_leaf_reference
  audit_local_ordinal
  request_fingerprint

LedgerSequenceAuditPlacementRequestFact
  request_kind = ledger_sequence
  source_event_reference
  audit_local_ordinal
  request_fingerprint

AuditHistoryPlacementAnchorFact =
    BeforeTranscriptLeafAuditAnchorFact
  | AfterTranscriptLeafAuditAnchorFact
  | LedgerSequenceAuditAnchorFact

BeforeTranscriptLeafAuditAnchorFact
  anchor_kind = before_leaf
  target_transcript_anchor_id / target_anchor_fingerprint
  audit_local_ordinal
  anchor_fingerprint

AfterTranscriptLeafAuditAnchorFact
  anchor_kind = after_leaf
  target_transcript_anchor_id / target_anchor_fingerprint
  audit_local_ordinal
  anchor_fingerprint

LedgerSequenceAuditAnchorFact
  anchor_kind = ledger_sequence
  source_event_reference
  resolved_left_transcript_anchor: optional
  resolved_right_transcript_anchor: optional
  transcript_gap_proof_fingerprint
  audit_local_ordinal
  anchor_fingerprint

PresentationHistoryEntrySourceFact =
    CanonicalTranscriptHistorySourceFact
  | DurableAuditHistorySourceFact

CanonicalTranscriptHistorySourceFact
  transcript_leaf_reference
  transcript_leaf_fingerprint
  transcript_placement_anchor: CanonicalTranscriptPlacementAnchorFact
  transcript_reducer_id / version / contract_fingerprint
  source_fold_delta_fingerprint
  source_leaf_change_ordinal

DurableAuditHistorySourceFact
  audit_cell_id / audit_cell_semantic_revision / audit_cell_fingerprint
  ordered_source_event_references
  presentation_policy_fingerprint
  extractor_id / version / contract_fingerprint
  extractor_output_ordinal
  audit_placement_anchor: AuditHistoryPlacementAnchorFact

PresentationHistoryPlacementKeyContractFact
  schema_version
  placement_key_contract_id / placement_key_contract_version
  framing_id = presentation-history-placement-key-fixed:v1
  framing_magic = ASCII("PHK1")
  framing_version_uint16 = 1
  encoded_byte_count = 75
  spine_coordinate_type = uint64
  spine_coordinate_width_bytes = 8
  integer_byte_order = big_endian
  spine_coordinate_genesis = 1
  spine_coordinate_left_none_sentinel = 0
  spine_coordinate_right_none_sentinel = 18446744073709551615
  spine_coordinate_max_append_value = 18446744073709551614
  spine_coordinate_append_rule = previous_max + 1
  relative_position_kind_order = (
    before_first=0,
    before_leaf=1,
    canonical_leaf=2,
    after_leaf=3,
    ledger_gap=4,
    after_last=5,
  )
  relative_position_kind_width_bytes = 1
  source_sequence_type = uint64 / width_bytes = 8
  local_ordinal_type = uint32 / width_bytes = 4
  stable_tiebreaker_contract_id = sha256-event-safe-stable-id:v1
  stable_tiebreaker_input_normalization = canonical_utf8_stable_id
  stable_tiebreaker_byte_count = 32
  canonical_layout = magic[4] || version[2] || primary_coordinate[8] || kind_rank[1] || source_sequence_or_zero[8] || local_ordinal[4] || left_coordinate_or_sentinel[8] || right_coordinate_or_sentinel[8] || stable_tiebreaker[32]
  placement_key_contract_fingerprint

PresentationHistoryPlacementKeyFact
  schema_version
  placement_key_contract_id / version / fingerprint
  canonical_spine_left_coordinate: optional
  canonical_spine_right_coordinate: optional
  relative_position_kind: before_first | before_leaf | canonical_leaf | after_leaf | ledger_gap | after_last
  source_ledger_sequence_or_zero
  relative_local_ordinal
  stable_source_tiebreaker
  canonical_comparable_key_bytes
  placement_key_fingerprint

PresentationHistoryEntryFact
  schema_version
  runtime_session_id
  history_entry_id
  placement_key
  source: PresentationHistoryEntrySourceFact
  cell: DurableHistoryCell
  entry_fingerprint

PresentationHistoryRankBasisFact =
    ConfirmedRootRankBasisFact
  | ActiveHeadRankBasisFact

ConfirmedRootRankBasisFact
  schema_version
  history_root_identity_fingerprint
  rank_basis_fingerprint

ActiveHeadRankBasisFact
  schema_version
  history_active_head_fingerprint
  through_authority_sequence
  rank_basis_fingerprint

PresentationHistoryRankedEntryView
  schema_version
  history_entry: PresentationHistoryEntryFact
  root_local_display_rank
  rank_basis: PresentationHistoryRankBasisFact
  ranked_view_fingerprint
```

`PresentationHistoryPlacementKeyContractFact`是编码与比较的唯一authority。所有整数都使用fixed-width unsigned big-endian；`uint64`合法范围为`0..UINT64_MAX`，`uint32`合法范围为`0..UINT32_MAX`。Spine genesis签发coordinate 1，append只能取`previous_max + 1`；0与`UINT64_MAX`分别只作为left-`None`和right-`None` sentinel，绝不能成为真实anchor。Append到`spine_coordinate_max_append_value`之后返回typed `SPINE_COORDINATE_EXHAUSTED`并进入session rotation/reconciliation，绝不wrap。

六个kind的typed-field矩阵与primary coordinate只能是：

| kind | left coordinate | right coordinate | primary coordinate | source sequence | local ordinal |
|---|---:|---:|---:|---:|---:|
| `before_first` | `None` | first spine coordinate，empty spine时`None` | `0` | exact audit source，`1..UINT64_MAX` | `0..UINT32_MAX` |
| `before_leaf` | predecessor last coordinate或`None` | target first coordinate | target first coordinate | exact audit source，`1..UINT64_MAX` | `0..UINT32_MAX` |
| `canonical_leaf` | target first coordinate | target last coordinate | target first coordinate | `0` | `0` |
| `after_leaf` | target last coordinate | successor first coordinate或`None` | target last coordinate | exact audit source，`1..UINT64_MAX` | `0..UINT32_MAX` |
| `ledger_gap` | resolved left last coordinate或`None` | resolved right first coordinate或`None` | left coordinate，`None`时`0` | exact audit source，`1..UINT64_MAX` | `0..UINT32_MAX` |
| `after_last` | last spine coordinate，empty spine时`None` | `None` | left coordinate，`None`时`0` | exact audit source，`1..UINT64_MAX` | `0..UINT32_MAX` |

相邻non-`None` left/right必须来自同一canonical spine proof，满足`left < right`；canonical interval要求`left <= right`。`ledger_gap`要求left/right都存在；open-ended gap只能规范化为`before_first`或`after_last`，两者只有在empty spine时才允许left/right同时为`None`。Before/after target、ledger gap resolution与canonical leaf anchor必须逐字段exact join其typed source proof，caller不能只提交coordinates。Relative kind rank固定为表中contract声明的`0..5`，不能按enum name、Python declaration order或Go constant临时排序。Stable tiebreaker固定为32 raw bytes：对event-safe stable ID的canonical UTF-8使用registered domain-separated SHA-256；禁止可变长度、Unicode locale排序、hex文本或secret-derived input。

`canonical_comparable_key_bytes`必须逐字节等于`PHK1[4] || version_uint16[2] || primary_coordinate_uint64[8] || kind_rank_uint8[1] || source_sequence_uint64[8] || local_ordinal_uint32[4] || left_or_zero_uint64[8] || right_or_UINT64_MAX_uint64[8] || stable_tiebreaker[32]`，总长恰好75 bytes；不得添加length prefix、padding或implementation-specific framing。比较只允许unsigned lexicographic byte order。`placement_key_contract_fingerprint`覆盖除自身外的上述全部contract fields，包括kind order、width、sentinel、normalization与layout。Factory从typed fields重建bytes并验证kind/coordinate/sentinel矩阵，caller不能提交opaque bytes作为authority。Process-local `PresentationHistoryPlacementKeyContractRegistry.resolve_exact(contract_id, version, fingerprint)`是唯一encoder/decoder resolver；same ID/version不同fingerprint是composition conflict。Transcript transition proof、tree contract、root、cursor factory、wire mapper和historical root decoder必须exact join同一binding。Retained root所需旧binding在root退役前不得卸载；missing historical binding返回`REBASE_REQUIRED | RECONCILIATION_REQUIRED`，不能用current encoder重写旧key。

Identity链必须无环：canonical entry ID只覆盖`runtime_session_id + "canonical_transcript" + stable_anchor_slot_key`；audit entry ID只覆盖`runtime_session_id + "durable_audit" + audit_cell_id + extractor_output_ordinal`。两者都不覆盖placement key、cell payload、root、rank或entry fingerprint。Canonical placement key的final tiebreaker使用stable anchor slot key；audit placement key使用预先形成的stable audit entry ID。最后`entry_fingerprint`才覆盖entry ID、完整placement key、source与cell。禁止从entry fingerprint反推ID，或让placement key/entry ID互相递归。

Canonical transcript是不可重排的spine。`CanonicalTranscriptPlacementAnchorFact`只能由`TranscriptProjectionStateStore`随`CanonicalTranscriptLeafChangeFact`产生，Presentation不得按source sequence、arrival time、latest replacement sequence或cell ID重建它：

- appended leaf只在spine尾部取得新的stable coordinate、stable anchor slot key与首次ordering boundary；coordinate/slot一经签发永不重编号；
- one-for-one replacement消费`single_replace` proof并继承被替换leaf的exact stable anchor slot、first/last stable coordinates、ordering boundary与anchor lineage；
- interval replacement消费`interval_replace` proof并继承被覆盖区间的first/last stable coordinates与boundary；resulting slot key由ordered predecessor slot-key accumulator唯一派生，predecessor anchor fingerprints按canonical order进入accumulator；
- retirement消费`retire_to_tombstone` proof并保留同一stable coordinate与slot key的不可渲染anchor tombstone；已有audit anchor继续绑定该coordinate，不得被迁移到任意邻居；
- current spine中的anchor interval必须严格有序且不重叠；replacement/retirement所需的old-anchor to resulting-anchor rebind必须由transcript reducer输出typed proof，Presentation不得自行选择新位置；
- replacement发生的ledger sequence只进入occurrence attribution，永不进入canonical ordering key。

Audit extractor必须按registered policy产生closed `AuditHistoryPlacementRequestFact`，不能caller-supply resolved anchor。Central placement factory验证request与extractor/source exact join后才生成`AuditHistoryPlacementAnchorFact`：before/after request exact join一个current transcript anchor；anchor被replacement覆盖时，只能消费transcript reducer提供的typed rebind proof。`ledger_sequence`不是全局排序键：central resolver使用canonical fold提供的ordered spine与每个anchor继承的ordering boundaries，把该request exact-resolve到一个left/right transcript gap，并冻结`transcript_gap_proof_fingerprint`。缺失target、ambiguous gap、non-monotonic boundary或unproved rebind全部返回`RECONCILIATION_REQUIRED`。

唯一merge算法为：保持canonical anchor stable-coordinate顺序不变；每个kind/primary-coordinate组内的audit都按`(source sequence, audit_local_ordinal, stable tiebreaker)`排列；canonical cell始终位于自身`before_leaf`与`after_leaf`两组之间，proved gap audit位于left anchor全部after内容之后、right anchor全部before内容之前。Central factory将该结果编码成唯一、可按unsigned lexicographic bytes比较的`canonical_comparable_key_bytes`；canonical branch的`stable_source_tiebreaker`覆盖stable anchor slot key，audit branch覆盖预先形成的stable audit history entry ID，使一个root内placement key本身唯一。Duplicate placement key是authority conflict，不允许再用外部字段临时打破平局。该stable placement key是persistent tree的唯一durable key；插入audit、retirement或interval replacement不得重写未受影响suffix的key。

`root_local_display_rank`只能在读取某个confirmed root或active head时，依据tree subtree counts与bounded resident suffix即时派生。它从0连续编号，但只属于`PresentationHistoryRankedEntryView`，不得进入entry、placement key、tree node、cursor、candidate、root semantic identity或artifact payload。相同entry在不同root generation中可以有不同display rank而保持byte-identical durable identity；Go只能把rank当该response/snapshot内的显示位置，不能把它当stable cursor。

Resident vector fingerprint的唯一覆盖为ordered `(history_entry_id, entry_fingerprint, placement_key_fingerprint, root_local_display_rank)`，明确排除rank-basis/root/active-head attribution。因此checkpoint把同一resident placement从tail移入root时，可以在basis换代后合法证明`unchanged`；client在安装root-advanced frame时把整条resident vector原子rebind到resulting active-head basis，不需要逐entry upsert。`ranked_view_fingerprint`仍覆盖rank basis，用于单个snapshot/page carrier exact join，但不能直接充当resident vector accumulator。

Long-Horizon rewrite、leaf replacement/retirement或extractor contract变更可生成新projection generation和placement-key set，但不得就地更改旧root。已经发布的旧cursor在其immutable root仍被retention owner保留时继续读取旧snapshot；只有caller要求跨到latest root、旧root合法退役，或historical codec/contract已不可恢复时，才返回typed `CURSOR_STALE | REBASE_REQUIRED`，绝不按cell ID静默跨root映射。

```text
PresentationHistoryProjectionRootFact
  schema_version
  runtime_session_id
  root_codec_id / version / contract_fingerprint
  history_projection_id / version / contract_fingerprint
  materialization_policy_fingerprint
  tree_contract_fingerprint
  placement_key_contract_id / version / fingerprint
  canonical_transcript_reducer_contract_fingerprint
  event_domain_registry_contract_fingerprint
  presentation_policy_registry_contract_fingerprint
  audit_extractor_registry_contract_fingerprint
  projection_generation
  through_authority_sequence
  presentation_source_segment_count
  presentation_source_prefix_accumulator
  source_prefix_transition_proof: PresentationHistorySourcePrefixTransitionProofFact | None
  previous_projection_root_reference: PresentationHistoryProjectionRootReferenceFact | None
  root_kind: empty | non_empty
  tree_root_node_reference: optional
  tree_height
  entry_count
  first_placement_key / last_placement_key
  canonical_transcript_spine_fingerprint
  ordered_history_entry_accumulator
  projection_root_fingerprint
```

`projection_root_fingerprint`覆盖history projection/materialization/tree/placement-key contracts、canonical transcript reducer、event-domain registry、presentation-policy registry、audit-extractor registry、generation/high-water、presentation-source segment count/prefix accumulator及transition proof、previous root reference、root node reference、tree height、entry count、placement-key bounds、canonical spine fingerprint与entry accumulator。Empty root禁止node reference且height/count为0；non-empty root要求node range/count/accumulator与root逐项exact join。`event_domain_registry_contract_fingerprint`仍只表达EventLog domain/schema binding；它绝不得代替presentation policy或audit extractor registry fingerprint。

### TUI-FND-PROJ-005 Persistent tree, checkpoint and restore owner

Unified root复用现有transcript projection的persistent-tree模式：bounded immutable leaf/internal nodes、stable placement-key ranges、subtree counts、content-addressed root、path-copy mutation与现有mutable validated `runtime_projection_checkpoints`。禁止flat all-pages manifest、持久化连续history ordinal或可覆盖shadow history table。

```text
PresentationHistoryTreeContractFact
  schema_version
  tree_contract_id / version
  placement_key_contract: PresentationHistoryPlacementKeyContractFact
  max_inline_entry_bytes
  max_leaf_entries / max_leaf_node_bytes
  max_internal_fanout / max_internal_node_bytes
  max_tree_height / maximum_representable_entries
  node_canonicalization_contract_fingerprint
  ordering_contract_fingerprint
  tree_contract_fingerprint

PresentationHistoryGrowthAdmissionKind =
    prompt_submission
  | run_activation
  | queue_steer_delivery
  | queue_follow_up_delivery
  | interaction_continuation

PresentationHistoryGrowthQuoteKindBoundFact
  schema_version
  admission_kind: PresentationHistoryGrowthAdmissionKind
  maximum_new_history_entries
  derivation_input_contract_fingerprint
  kind_bound_fingerprint

PresentationHistoryGrowthQuotePolicyFact
  schema_version
  quote_policy_id / quote_policy_version
  ordered_kind_bounds: tuple[PresentationHistoryGrowthQuoteKindBoundFact, ...]
  maximum_active_committed_runs_per_session = 1
  maximum_nonterminal_growth_reservations_per_session
  quote_derivation_contract_fingerprint
  quote_policy_fingerprint

PresentationHistoryMaterializationPolicyFact
  schema_version
  policy_id / version
  tree_contract: PresentationHistoryTreeContractFact
  growth_quote_policy: PresentationHistoryGrowthQuotePolicyFact
  max_root_fact_bytes
  checkpoint_max_new_nodes / checkpoint_max_new_node_bytes
  checkpoint_max_confirmation_lineage_reads
  tail_soft_max_events / tail_soft_max_entries / tail_soft_max_bytes
  tail_hard_max_events / tail_hard_max_entries / tail_hard_max_bytes
  capacity_soft_rotation_threshold_entries
  terminalization_maintenance_reserve_entries
  minimum_ordinary_growth_quote_entries
  capacity_growth_and_reserve_contract_fingerprint
  max_retained_root_generations / root_retention_ttl_seconds
  read_max_entries / read_max_page_canonical_bytes / read_max_page_rendered_bytes
  read_max_node_reads
  read_max_tree_height
  retention_contract_fingerprint
  read_contract_fingerprint
  policy_fingerprint

PresentationHistoryTreeNodeReferenceFact
  schema_version
  node_kind: leaf | internal
  node_artifact_id / node_sha256 / node_byte_count
  first_placement_key / last_placement_key
  subtree_entry_count
  subtree_entry_accumulator
  node_reference_fingerprint

PresentationHistoryLeafNodeFact
  schema_version
  first_placement_key / last_placement_key
  ordered_entries: tuple[PresentationHistoryEntryFact, ...]
  subtree_entry_accumulator
  node_fingerprint

PresentationHistoryInternalNodeFact
  schema_version
  tree_level
  ordered_child_references: tuple[PresentationHistoryTreeNodeReferenceFact, ...]
  subtree_entry_accumulator
  node_fingerprint

PresentationHistoryTailMutationFact =
    UpsertPresentationHistoryEntryMutationFact
  | RemovePresentationHistoryEntryMutationFact

UpsertPresentationHistoryEntryMutationFact
  schema_version
  mutation_kind = upsert
  mutation_id
  source_from_sequence_exclusive / source_through_sequence
  history_entry_id
  placement_key
  expected_previous_entry_fingerprint: Fingerprint | None
  resulting_entry: PresentationHistoryEntryFact
  mutation_fingerprint

RemovePresentationHistoryEntryMutationFact
  schema_version
  mutation_kind = remove
  mutation_id
  source_from_sequence_exclusive / source_through_sequence
  history_entry_id
  placement_key
  expected_previous_entry_fingerprint
  resulting_anchor_tombstone_reference: CanonicalTranscriptPlacementAnchorTombstoneFact | None
  mutation_fingerprint

PresentationHistoryTailMutationReferenceFact
  schema_version
  mutation_id
  mutation_kind: upsert | remove
  mutation_fingerprint
  mutation_reference_fingerprint

PresentationHistoryTailFoldSegmentFact
  schema_version
  runtime_session_id
  from_sequence_exclusive / through_sequence
  source_range_fingerprint
  source_range_accumulator
  ordered_mutation_references: tuple[PresentationHistoryTailMutationReferenceFact, ...]
  mutation_count
  mutation_accumulator
  segment_fingerprint

PresentationHistorySourcePrefixTransitionProofFact
  schema_version
  runtime_session_id
  previous_projection_root_reference: PresentationHistoryProjectionRootReferenceFact | None
  predecessor_through_authority_sequence
  predecessor_source_segment_count
  predecessor_source_prefix_accumulator
  ordered_appended_segments: tuple[PresentationHistoryTailFoldSegmentFact, ...]
  appended_segment_count
  appended_segment_accumulator
  resulting_through_authority_sequence
  resulting_source_segment_count
  resulting_source_prefix_accumulator
  transition_proof_fingerprint

PresentationHistoryProjectionRootReferenceFact
  schema_version
  root_kind: empty | non_empty
  root_artifact_id / root_sha256 / root_byte_count
  projection_root_fingerprint
  materialization_policy_fingerprint
  tree_contract_fingerprint
  root_reference_fingerprint

PresentationHistoryProjectionCheckpointFact
  schema_version
  runtime_session_id
  checkpoint_kind = terminal_presentation_history
  checkpoint_generation
  previous_checkpoint_fingerprint
  through_authority_sequence
  presentation_source_segment_count
  presentation_source_prefix_accumulator
  projection_revision
  projection_root_reference: PresentationHistoryProjectionRootReferenceFact
  projection_root_fingerprint
  checkpoint_fingerprint

PresentationHistoryCheckpointCandidateCutFact
  schema_version
  cut_kind: append_prefix | rewrite_snapshot
  source_active_head_fingerprint
  source_confirmed_root_fingerprint
  frozen_tail_prefix_from_sequence_exclusive
  frozen_tail_prefix_through_sequence
  frozen_tail_prefix_source_range_accumulator
  ordered_frozen_tail_prefix_segments: tuple[PresentationHistoryTailFoldSegmentFact, ...]
  frozen_tail_prefix_segment_count
  frozen_tail_prefix_segment_accumulator
  frozen_tail_prefix_mutation_count
  frozen_tail_prefix_mutation_accumulator
  frozen_tail_prefix_resulting_resident_accumulator
  resulting_presentation_source_prefix_accumulator
  cut_fingerprint

PresentationHistoryCheckpointCandidateFact
  schema_version
  checkpoint_candidate_id
  expected_previous_checkpoint_generation
  expected_previous_checkpoint_fingerprint
  expected_previous_projection_root_fingerprint
  candidate_cut: PresentationHistoryCheckpointCandidateCutFact
  resulting_checkpoint: PresentationHistoryProjectionCheckpointFact
  resulting_root_identity
  candidate_fingerprint

PresentationHistoryCheckpointCommitGuard
  checkpoint_candidate_id / candidate_fingerprint
  physical_attempt_generation
  absolute_deadline_monotonic
  dependency_lease_generation

PresentationHistoryCheckpointCommitDisposition =
    FULL
  | NONE
  | UNKNOWN
  | CONFLICT

PresentationHistoryCompatibleSuccessorLineageProofFact
  checkpoint_candidate_id / candidate_fingerprint
  candidate_projection_root_reference
  installed_projection_root_reference
  ordered_previous_root_references: tuple[PresentationHistoryProjectionRootReferenceFact, ...]
  ordered_source_prefix_transition_proofs: tuple[PresentationHistorySourcePrefixTransitionProofFact, ...]
  lineage_depth
  covered_tail_prefix_through_sequence
  covered_tail_prefix_segment_count
  covered_tail_prefix_segment_accumulator
  covered_presentation_source_prefix_accumulator
  covered_tail_prefix_mutation_count
  covered_tail_prefix_mutation_accumulator
  covered_tail_prefix_source_range_accumulator
  shared_contract_fingerprint_accumulator
  lineage_proof_fingerprint

PresentationHistoryCheckpointFullReceipt
  disposition = FULL
  checkpoint_candidate_id / candidate_fingerprint
  installed_checkpoint
  installed_root_identity
  winner_kind: exact_candidate | identical_concurrent_winner | compatible_successor
  compatible_successor_lineage_proof: PresentationHistoryCompatibleSuccessorLineageProofFact | None
  confirmation_fingerprint

PresentationHistoryCheckpointNoneReceipt
  disposition = NONE
  checkpoint_candidate_id / candidate_fingerprint
  exact_observed_predecessor_checkpoint: PresentationHistoryProjectionCheckpointFact | None
  confirmation_fingerprint

PresentationHistoryCheckpointUnknownReceipt
  disposition = UNKNOWN
  checkpoint_candidate_id / candidate_fingerprint
  last_physical_attempt_generation
  unresolved_operation_identity
  reconciliation_identity

PresentationHistoryCheckpointConflictReceipt
  disposition = CONFLICT
  checkpoint_candidate_id / candidate_fingerprint
  exact_observed_checkpoint
  conflict_reason
  conflict_fingerprint

PresentationHistoryCheckpointCommitOutcome =
    PresentationHistoryCheckpointFullReceipt
  | PresentationHistoryCheckpointNoneReceipt
  | PresentationHistoryCheckpointUnknownReceipt
  | PresentationHistoryCheckpointConflictReceipt

PresentationHistoryRootIdentityFact
  schema_version
  runtime_session_id
  history_projection_id / version / contract_fingerprint
  materialization_policy_fingerprint
  tree_contract_fingerprint
  placement_key_contract_id / version / fingerprint
  checkpoint_generation
  checkpoint_fingerprint
  projection_root_reference: PresentationHistoryProjectionRootReferenceFact
  projection_generation
  projection_root_fingerprint
  through_authority_sequence
  presentation_source_segment_count
  presentation_source_prefix_accumulator
  presentation_policy_registry_contract_fingerprint
  audit_extractor_registry_contract_fingerprint
  root_identity_fingerprint

PresentationHistoryCapacityStateFact =
    AvailableHistoryCapacityFact
  | HistorySessionRotationRequiredFact
  | HistoryTreeCapacityExhaustedFact
  | HistoryCapacityReconciliationRequiredFact

PresentationHistoryGrowthQuoteFact
  schema_version
  growth_quote_id
  runtime_session_id
  admission_kind: PresentationHistoryGrowthAdmissionKind
  source_authority_fingerprint
  quote_policy_id / quote_policy_version / quote_policy_fingerprint
  maximum_new_history_entries
  quote_fingerprint

PresentationHistoryGrowthReservationState =
    reserved
  | settled
  | released
  | reconciliation_required

PresentationHistoryGrowthReservationFact
  schema_version
  growth_reservation_id
  quote: PresentationHistoryGrowthQuoteFact
  owner_kind / owner_id / owner_generation
  reservation_revision
  previous_reservation_fingerprint: Fingerprint | None
  settled_materialized_entry_count
  remaining_unmaterialized_entry_count
  reservation_state: PresentationHistoryGrowthReservationState
  reservation_fingerprint

PresentationHistoryCapacityAdmissionDisposition =
    available
  | session_rotation_required

PresentationHistoryCapacityAdmissionDecisionFact
  schema_version
  runtime_session_id
  source_active_head_fingerprint
  requested_growth_quote_fingerprint
  confirmed_entry_count
  current_tail_worst_case_entry_count
  active_growth_reservation_remaining_entry_count
  requested_admission_growth_quote_entry_count
  projected_ordinary_entries
  soft_rotation_threshold_entries
  terminalization_maintenance_reserve_entries
  maximum_representable_entries
  disposition: PresentationHistoryCapacityAdmissionDisposition
  decision_fingerprint

AvailableHistoryCapacityFact
  schema_version
  capacity_state = available
  confirmed_entry_count
  current_tail_worst_case_entry_count
  active_growth_reservation_remaining_entry_count
  projected_ordinary_entries_before_request
  soft_rotation_threshold_entries
  terminalization_maintenance_reserve_entries
  remaining_ordinary_admission_entries
  capacity_state_fingerprint

HistorySessionRotationRequiredFact
  schema_version
  capacity_state = session_rotation_required
  confirmed_entry_count
  current_tail_worst_case_entry_count
  active_growth_reservation_remaining_entry_count
  projected_ordinary_entries_before_request
  soft_rotation_threshold_entries
  terminalization_maintenance_reserve_entries
  stable_reason = SESSION_HISTORY_ROTATION_REQUIRED
  capacity_state_fingerprint

HistoryTreeCapacityExhaustedFact
  schema_version
  capacity_state = exhausted
  observed_entry_count
  maximum_representable_entries
  stable_reason = HISTORY_TREE_CAPACITY_EXHAUSTED
  reconciliation_identity
  capacity_state_fingerprint

HistoryCapacityReconciliationRequiredFact
  schema_version
  capacity_state = reconciliation_required
  source_active_head_fingerprint
  offending_growth_quote_fingerprint
  offending_growth_reservation_fingerprint
  observed_positive_growth_entries
  remaining_unmaterialized_entry_count
  stable_reason: HISTORY_GROWTH_QUOTE_EXCEEDED | CAPACITY_POLICY_DRIFT | RESERVATION_AUTHORITY_CONFLICT
  reconciliation_identity
  capacity_state_fingerprint
```

Tail segmentizer必须把每个canonical EventLog sequence规范化为恰好一个`PresentationHistoryTailFoldSegmentFact`，不沿用physical commit batch或SQL restore page边界：`from_sequence_exclusive = sequence - 1`、`through_sequence = sequence`。该sequence产生的全部presentation mutations按stable placement key排序进入references；合法noop segment的tuple为空、count为0且mutation accumulator为canonical empty。`source_range_fingerprint/source_range_accumulator`从该single raw stored envelope及其exact transcript/audit purpose dispositions重算。这样live batch、single restored range和任意bounded restored pages都会生成byte-identical segment stream。Segment factory验证所有mutation的source range恰好等于该segment，禁止一个mutation跨segment或一个sequence被拆成多个segment。

`mutation_reference_fingerprint`覆盖mutation ID/kind/fingerprint；segment mutation accumulator覆盖ordered mutation references；`segment_fingerprint`覆盖schema/session、exact one-sequence range、source range fingerprint/accumulator、ordered mutation references/count/accumulator。Active-tail segment accumulator固定为`H("presentation-history-tail-segments:v1", ordered tuple[(through_sequence, segment_fingerprint)])`；空tail使用registered canonical empty。上述fingerprint都禁止覆盖physical live-batch ID、SQL page、checkpoint generation或process owner identity。

`PresentationHistoryActiveHeadOwner`必须同时保留完整bounded segment tuple、segment引用的完整mutation facts和derived resident map；aggregate hash不是可切分authority。Tail source/segment/mutation accumulators全部由exact tuple重算。Checkpoint cut只能落在segment边界；prefix/suffix只能通过tuple slice形成，绝不尝试反演aggregate hash。Noop-only concurrent suffix因此仍有独立segment carrier与可重算source accumulator。

Durable source lineage使用固定recurrence：`P0 = H("presentation-history-source-prefix:v1", runtime_session_id, canonical_empty)`；`Pn = H("presentation-history-source-prefix:v1", P(n-1), segment.through_sequence, segment.segment_fingerprint)`。Generation-0的segment count为0且prefix accumulator为`P0`；non-genesis root/checkpoint要求`presentation_source_segment_count == through_authority_sequence`并保存`Pn`。每个non-genesis root还必须嵌入一个bounded `PresentationHistorySourcePrefixTransitionProofFact`：其predecessor fields与exact previous root逐项相等，ordered appended segments连续覆盖`(predecessor H, resulting H]`，逐项递推后得到root的resulting count/sequence/`Pn`；genesis的proof与previous root必须同时为`None`。Candidate resulting prefix只能从expected predecessor `Pk`依次fold `ordered_frozen_tail_prefix_segments`得到，且resulting root transition proof必须携带byte-identical segment tuple。Compatible successor必须沿retained root lineage逐跳验证这些transition proofs，证明candidate `Pn`是installed `Pm`的exact prefix recurrence，不能只比较最终tree、entry accumulator、through sequence或两个opaque aggregate hash。

Transition-proof factory必须重算`appended_segment_count == len(ordered_appended_segments)`、`resulting_source_segment_count = predecessor_source_segment_count + appended_segment_count`、`resulting_through_authority_sequence = predecessor_through_authority_sequence + appended_segment_count`，并验证每个segment的from/through连续。`appended_segment_accumulator`覆盖ordered `(through_sequence, segment_fingerprint)`；proof fingerprint覆盖predecessor identity、完整bounded segment tuple、所有derived counts/accumulators与resulting identity。Tuple events/bytes必须同时不超过tail hard bounds与`max_root_fact_bytes`，否则不能形成candidate并返回typed checkpoint rebuild/capacity outcome。

这里的`CONFLICT`是checkpoint exact-confirmation/domain disposition，不是第四种数据库physical commit result；底层physical outcome仍只能被证明为committed、not committed或unresolved，domain classifier结合exact row/root evidence后才产生上述四分支。

`maximum_representable_entries`由tree height、leaf capacity与internal fanout按唯一checked-in formula计算并由factory重算，不接受caller arbitrary value；整数overflow或声明值不一致是configuration conflict。Policy validators必须证明soft bounds逐项小于hard bounds、leaf/internal/root bytes不超过artifact hard limit、growth quote policy对closed admission enum恰有一个positive bound且`maximum_active_committed_runs_per_session == 1`、`capacity_soft_rotation_threshold_entries + terminalization_maintenance_reserve_entries <= maximum_representable_entries`、terminalization maintenance reserve不小于一次已准入run的worst-case terminal projection entry budget、`minimum_ordinary_growth_quote_entries > 0`、read tree height不大于materialized tree height上限、confirmation lineage reads不大于retained root generation bound，并冻结所有数字进入`policy_fingerprint`。Production不得在constructor或adapter中另存hidden bounds。

`checkpoint_candidate_id = H("presentation-history-checkpoint-candidate:v2", runtime_session_id, checkpoint_kind, expected_previous_checkpoint_fingerprint, candidate_cut.cut_fingerprint, resulting_checkpoint.checkpoint_fingerprint)`；`candidate_fingerprint`覆盖完整expected predecessor guard、candidate cut、resulting checkpoint与resulting root identity，但不覆盖physical attempt generation/deadline。`PresentationHistoryCheckpointCommitGuard`是process-local frozen dataclass，不进入candidate/event/artifact semantic identity，只有checkpoint owner可以签发或换代。

Candidate cut factory必须证明：source confirmed root与expected predecessor root相同；cut只能落在owner-held segment边界；prefix from-sequence等于该root through sequence；prefix through sequence等于resulting checkpoint/root through sequence；`ordered_frozen_tail_prefix_segments`是source active-head segment tuple的exact prefix，其segment/source-range/mutation count与accumulators全部从该tuple重算；resulting source-prefix accumulator按固定`P` recurrence从predecessor root推进；resulting tree等于在expected predecessor tree上按segment顺序应用references所绑定的exact mutations。`append_prefix`只允许append/noop mutation；`rewrite_snapshot`必须覆盖冻结active head的完整segment/mutation/state identity，不能只声明一个affected range。任何caller-supplied count、through sequence或accumulator均需从owner-held exact tuple重算。

Full-receipt validator要求`exact_candidate | identical_concurrent_winner`的lineage proof为`None`；`compatible_successor`则必须携带non-empty proof，其ordered chain从installed root逐跳exact-read `previous_projection_root_reference`，最终到达candidate resulting root，depth不超过materialization policy bound，且每跳session、projection、tree、placement-key、policy与registry contracts相同。`ordered_source_prefix_transition_proofs`必须与每个root hop一一对应并逐项重放`P` recurrence。Successor proof还必须证明它覆盖candidate的exact frozen segment prefix或同一source head的更长exact segment prefix；covered segment count/accumulator、through sequence、mutation accumulator与source-range accumulator均不得小于或偏离candidate cut。NONE的observed predecessor为`None`只对expected genesis合法；其他generation必须逐字段等于candidate expected predecessor。

Leaf node placement keys必须按`canonical_comparable_key_bytes`严格递增且全局唯一；internal child placement-key ranges严格递增、不重叠且fanout有界；每个reference的range/count/accumulator必须与exact-read node一致。Append或rewrite只path-copy受影响leaf及其ancestor path：append为`O(tree height)`新node，range rewrite为`O(affected leaves + affected ancestor paths)`，绝不读取/序列化全树或重写未受影响suffix。Page read按placement-key anchor遍历tree，并通过沿途subtree counts派生root-local display rank；复杂度和I/O上界为`O(tree height + returned entries)`且必须同时服从node-read、entry、page canonical/rendered bytes与absolute-deadline limits。

`PresentationHistoryRootIdentityFact`只能由validated checkpoint + exact-read immutable root artifact + exact root-node proof的central factory构造，所有字段必须exact join；viewport、cursor和protocol mapper不得拆开或caller-supply registry fingerprints。Stable node/root artifact ID分别为`H("presentation-history-tree-node-artifact:v1", node_fingerprint)`与`H("presentation-history-root-artifact:v1", projection_root_fingerprint)`；fingerprint不覆盖自身ID或storage attribution。New path-copy nodes与root artifact必须先write/confirm identical，才可冻结checkpoint candidate；未引用artifact成为可GC orphan。Checkpoint是derived projection，不得参与Runtime EventLog commit或让writer等待I/O。

Root/node decoding只能通过`PresentationHistoryArtifactCodecRegistry.resolve_exact(codec_id, codec_version, codec_contract_fingerprint)`。Same ID/version + different fingerprint是composition conflict；任何retained root引用的historical root/node codec binding必须保留到该root退役。缺失binding返回`RECONCILIATION_REQUIRED | REBASE_REQUIRED`，不得用current codec猜测解码。

Stable checkpoint candidate不包含physical attempt deadline、connection、task或mutable row handle。每次NONE retry只新建`PresentationHistoryCheckpointCommitGuard`并复用byte-identical candidate。数据库writer在同一transaction内锁定exact checkpoint row，验证expected predecessor generation/fingerprint/root，再CAS安装`resulting_checkpoint`；caller cancellation只能detach waiter，physical operation仍由checkpoint owner持有并必须先真实退出。所有BaseException、timeout和caller cancellation之后都必须等待physical exit、exact-read row，再按唯一矩阵分类：

- row逐字段等于candidate resulting checkpoint：`FULL/exact_candidate`；
- concurrent writer安装byte-identical stable candidate：`FULL/identical_concurrent_winner`；
- observed newer checkpoint的immutable previous-root chain在policy-bounded depth内exact包含candidate resulting root，全部projection/materialization contracts一致且placement-key/root accumulator及candidate-prefix coverage lineage合法：`FULL/compatible_successor`并采用observed latest root；
- row仍逐字段等于expected predecessor，且physical transaction已证明未提交：`NONE`；
- row不可读、physical outcome与row状态不能同时证明，或confirmation自身超时：`UNKNOWN`；
- same/later generation但root lineage、payload、contract或predecessor不兼容：`CONFLICT`。

只有`FULL`或proved compatible successor才能在projection lock内把process-local active head换到installed root；`NONE`进入bounded retry wait；`UNKNOWN`安装projection-checkpoint reconciliation owner并保留stable candidate/root artifacts，Host close必须drain或明确blocked；`CONFLICT`不得任选一方继续，必须以exact observed row重建或进入offline repair。数据库已安装但进程在swap前崩溃时，reopen通过相同exact confirmation恢复installed root，不允许继续使用旧active head。底层`write_runtime_projection_checkpoint(...)->None`必须升级为typed prepare/CAS/confirm port，adapter exception不能充当commit outcome。

Root artifact是immutable快照，旧cursor在自身root的retention horizon内继续读取旧root/tree，不因latest checkpoint推进而立即失效。`PresentationHistoryRootRetentionOwner`无条件保留current checkpoint root与所有active cursor lease引用的root；除此之外只保留同时位于latest `max_retained_root_generations`且age不超过`root_retention_ttl_seconds`的unleased roots。Foundation/Gateway在向attachment发布snapshot/page cursor时签发process-local `PresentationHistoryCursorRootLease(attachment_id, root_fingerprint, lease_generation, expires_at)`；issue不进入wire cursor identity，detach/expiry释放，process允许的attachments/leases本身有closed hard cap。

`previous_projection_root_reference`是weak lineage attribution，不是tree/content retention edge：hydrating current root只需current root node reference，不递归hydrate全部previous roots；GC不得因为current root携带weak predecessor reference而永久保留旧root。Root无checkpoint reference、无active cursor lease，并且已超出generation window或TTL任一上限时即可retire；node只有在不再被任何retained root的tree root node强可达时才可删除。Compatible-successor confirmation只能沿仍retained的weak lineage targets证明；target已GC时不得伪造proof，只能把winner作为unconfirmed conflict并从exact current checkpoint重新build。Cursor root已合法retire才返回`CURSOR_STALE`；不得因新root出现而强制rebase。

Canonical generation-0冻结为：`checkpoint_generation=0`、`projection_generation=0`、`through_authority_sequence=0`、`presentation_source_segment_count=0`、`presentation_source_prefix_accumulator=P0`、`source_prefix_transition_proof=None`、`root_kind=empty`、`tree_height=0`、`entry_count=0`、first/last placement key为`None`、canonical empty spine/entry accumulators、无node reference、canonical empty root artifact，并绑定activation时的全部current contract/policy fingerprints。`previous_checkpoint_fingerprint=None`与`previous_projection_root_reference=None`只对generation-0合法；任何non-genesis root的previous reference必须exact-read并与predecessor checkpoint root join。新session在首次Terminal attachment准入前exact-confirm genesis；existing session在INFRA-3 activation preparation或background rebuild中安装非genesis checkpoint，未安装前attachment返回typed `REBASE_REQUIRED`，不从session genesis同步全量扫描。

Checkpoint maintenance拥有service-owned bounded physical operation、absolute deadline、artifact/DB dependency leases与close drain责任。Stable candidate一旦形成，service必须持有exact candidate、raw checkpoint candidate、artifact tuple、attempt generation/guard和最后一次typed confirmation receipt，直至FULL并完成root installation与tap delivery。`NONE | UNKNOWN | CONFLICT`都不能由普通exception丢弃该owner：live owner使用bounded backoff重试相同candidate的write/exact-confirm；waiter cancellation只detach；Host close在共享absolute deadline内继续drain，期限耗尽时明确返回close blocked。任何rebuild/new candidate只能在已有attempt由typed supersession/reconciliation协议退休之后开始。Live presentation在confirmed checkpoint之后只能保留以下process-local、受events/bytes硬上限的tail owner：

```text
PresentationHistoryActiveHeadFact
  schema_version
  runtime_session_id
  confirmed_root_identity: PresentationHistoryRootIdentityFact
  tail_from_sequence_exclusive
  through_authority_sequence
  tail_source_range_accumulator
  tail_segment_count
  ordered_tail_segment_accumulator
  tail_mutation_count
  ordered_tail_mutation_accumulator
  resulting_resident_entry_count
  resulting_resident_entry_accumulator
  capacity_state: PresentationHistoryCapacityStateFact
  active_head_fingerprint
```

`tail_from_sequence_exclusive`必须等于confirmed root through sequence。`PresentationHistoryActiveHeadOwner`在同一lock下保存完整bounded `PresentationHistoryTailFoldSegmentFact` tuple、该tuple引用的完整`PresentationHistoryTailMutationFact` map，以及由confirmed root window应用这些mutation后得到的resident ordered map；Fact本身只保存derived count/accumulators，不复制segment、mutation或entry payload。Segment tuple必须从`confirmed_root.through_authority_sequence + 1`逐sequence连续到head high-water；tail source/segment/mutation accumulators全部从exact tuple重算。Noop authority sequence仍追加一个空mutation segment并推进segment/source lineage；因此任何prefix/suffix切分都通过tuple slice重算，绝不反演aggregate hash。Upsert/remove均以stable placement key定位，remove必须exact join previous entry fingerprint；任何mutation都不得给未受影响entry重分配key。

Tail只能消费与nested confirmed root完全相同的history projection、transcript reducer、event-domain、presentation policy与audit extractor registry contracts；任一binding变化都必须先rebuild/install新root，不得在一个active head中混用两代extractor。该head不是durable root，不写artifact/checkpoint、不生成page cursor、不进入root retention。Canonical empty tail要求`tail_from_sequence_exclusive == through_authority_sequence == confirmed_root.through_authority_sequence`、`tail_segment_count == tail_mutation_count == 0`、source/segment/mutation accumulators均为registered canonical empty，resident vector等于从confirmed root hydrate出的bounded window。Tail mutation顺序由source range与central placement factory唯一决定，不存在连续tail ordinal或next ordinal。

Tail达到policy soft watermark立即启动checkpoint attempt。Candidate必须在projection lock内冻结`PresentationHistoryCheckpointCandidateCutFact`：source active-head/root fingerprint、只落在segment边界的exact prefix、完整ordered segment tuple、through sequence、source/segment/mutation accumulators、resulting durable source-prefix accumulator与resulting resident accumulator；随后artifact/tree I/O在锁外进行，Runtime writer可继续向active tail追加。

Typed FULL安装只能在同一projection lock内执行以下prefix split：

1. 对`append_prefix`，验证current active head仍以candidate frozen segment tuple逐segment、逐mutation、逐source range exact开头；installed exact/identical root只消费该prefix，`compatible_successor`只能在durable source-prefix transition proof覆盖同一prefix或current owner中已经存在的更长exact segment prefix时消费proved covered prefix，绝不能把另一process尚未进入本owner的future range当作local suffix predecessor；
2. 按segment tuple切出`(installed_root.through_authority_sequence, current_head.through_authority_sequence]`中尚未被root覆盖的exact suffix，原样保留其中包括noop的每个segment，并以installed root重新计算tail source/segment/mutation及resident accumulators；resulting active head因此可以拥有non-empty tail；
3. post-cut suffix只在全部为append/noop、placement contracts相同且不触碰candidate已读取key range时可rebase；出现replacement、retirement、remove、same-key upsert或contract change时candidate标记`SUPERSEDED_REBUILD_REQUIRED`，不得把它当append suffix；
4. `rewrite_snapshot`要求安装时current active head仍逐字段等于source active head；任何post-cut change都废弃candidate并重建，不能局部拼接；
5. NONE保留原candidate与完整current tail后换physical guard重试；UNKNOWN进入reconciliation；CONFLICT或prefix mismatch从exact installed/current head重建。

Checkpoint FULL不得无条件清空tail，也不得让已经在candidate cut后进入live head的event消失。Root-advance candidate必须引用resulting active head的exact segment suffix identity；frame冻结前再次验证installed root、retained segment suffix和resident transition三方一致。超过tail hard bound时detach相关UI generation并要求rebase，不阻塞Runtime writer或把resident tail伪造成confirmed root。

History capacity不是silent truncation开关。普通growth admission只有一个计算口径：

```text
projected_ordinary_entries =
    confirmed_entry_count
  + current_tail_worst_case_entry_count
  + active_growth_reservation_remaining_entry_count
  + requested_admission_growth_quote_entry_count

available iff
  projected_ordinary_entries <= capacity_soft_rotation_threshold_entries

hard policy invariant
  capacity_soft_rotation_threshold_entries
  + terminalization_maintenance_reserve_entries
  <= maximum_representable_entries
```

`terminalization_maintenance_reserve_entries`绝不进入`projected_ordinary_entries`，因此不会被重复计算；它只占用soft threshold与hard maximum之间的隔离区。`confirmed_entry_count`来自exact confirmed root；`current_tail_worst_case_entry_count`只覆盖当前tail已经物化但尚未checkpoint的net entries；`active_growth_reservation_remaining_entry_count`只汇总所有已准入reservation尚未物化的remaining amount，已经进入tail的部分必须在同一owner lock下从remaining扣除，禁止与tail重复计数；requested quote在decision中单独加一次。

每类ordinary admission只能通过registered `PresentationHistoryGrowthQuotePolicyFact`从closed admission kind、exact source authority与bounded operation contract派生`PresentationHistoryGrowthQuoteFact`，caller不能自报数字。`PresentationHistoryGrowthQuotePolicyRegistry.resolve_exact(id, version, fingerprint)`是唯一resolver；same ID/version不同fingerprint是composition conflict，active reservation所需旧binding在其terminal settlement前不得卸载。Runtime session任一时刻最多存在一个active committed run；queue steer/follow-up、interaction continuation与新run admission在同一capacity-owner lock下串行reserve，任何额外process-local attempt都不能绕过reservation registry。Quote为0、超过对应kind bound或无法exact join source authority均fail closed。

`growth_quote_id = H("presentation-history-growth-quote:v1", runtime_session_id, admission_kind, source_authority_fingerprint, quote_policy_id, quote_policy_version, quote_policy_fingerprint)`；same ID不同maximum count是authority conflict。`growth_reservation_id = H("presentation-history-growth-reservation:v1", growth_quote_id, owner_kind, owner_id)`，不覆盖可换代的process generation。Quote fingerprint覆盖完整quote但不覆盖自身ID；reservation fingerprint覆盖quote、owner/generation、revision、previous fingerprint、settled/remaining/state但不覆盖自身ID。Reservation state update要求`revision = previous + 1`并使用exact previous fingerprint CAS；reopen可在同一stable reservation ID上安装新owner generation。Same source authority只能存在一个nonterminal reservation winner。

`PresentationHistoryGrowthReservationFact`是capacity owner持有的typed derived projection，不是第二份EventLog semantic authority；其source authority、candidate或UNKNOWN reconciliation identity必须可从既有durable run/queue/interaction事实exact重建。Admission在capacity-owner lock内、产生任何可增长durable candidate前安装reservation；若进程在candidate形成前崩溃，因不存在durable growth而无需恢复该空reservation。若candidate可能FULL/UNKNOWN，reopen必须先exact confirm并重建reservation，普通admission在此之前fail closed。

每次committed history fold按实际positive entry growth结算：相同数量从`remaining_unmaterialized_entry_count`扣除并进入tail count；remove/retirement不能倒增remaining，也不能把已消耗quote退回ordinary pool。Operation terminal FULL后释放unused remainder并进入`settled`；明确NONE/cancel-before-commit只在确认没有candidate winner后进入`released`；UNKNOWN、partial confirmation或owner crash保留remaining reservation并进入`reconciliation_required`，reopen必须从exact durable authority重建或确认，不能乐观释放。

Quote policy必须对其bounded operation contract给出保守上限，因此正常committed fold永远不应超过remaining quote。若代码/schema drift导致actual positive growth超出remaining，该durable EventLog commit已经是truth，Presentation绝不能回滚、阻塞或让writer等待UI；它必须fold能够证明的event、安装typed `HISTORY_GROWTH_QUOTE_EXCEEDED` capacity reconciliation/hard fence、拒绝后续ordinary admission，并保留terminalization maintenance reserve给当前run收口。只有offline repair/new projection generation可以解除该fault。该异常矩阵与“Runtime writer never awaits UI”共同进入architecture test。

`AvailableHistoryCapacityFact | HistorySessionRotationRequiredFact`描述不含新request的active-head baseline；`PresentationHistoryCapacityAdmissionDecisionFact`才绑定本次requested quote并重算上述公式。Baseline projected count不超过soft threshold且remaining至少容纳`minimum_ordinary_growth_quote_entries`时为available；否则为rotation required。一个较大request越过threshold时，该request得到typed rotation decision，即使更小request理论上仍可容纳，也不得由adapter自行缩小quote或截断content。

已经准入的run、pending interaction resolution、RunEnd/closure/recovery terminalization只消费隔离的terminalization maintenance reserve，不得被ordinary work借用。若因corruption、policy drift或错误估算实际达到tree hard maximum，安装`HistoryTreeCapacityExhaustedFact`并拒绝所有非terminal repair growth；UI返回typed reconciliation/“start new session”，不得roll over、evict或重写旧history。V1不实现history epoch/super-root；未来若需要无限单session history，必须另立schema/retention hard cut。

`PresentationHistoryProjectionCheckpointOwner`拥有两条共用同一placement core的输入：

```text
apply_live_entry(
  tap_entry: CommittedPresentationTapEntry,
  exact_audit_extractor_outputs,
)

fold_restored_range(
  transcript_result: RestoredRangeFoldResult,
  exact_audit_extractor_outputs,
  source_range_proof_fingerprint,
)
```

两条路径必须对同一sequence range生成相同的ordered history entries和resulting root accumulator，不依赖live batch或SQL page分组。`authority_high_water`只在canonical transcript purpose与audit purpose都对该range完成后推进，即使该range产生零个history entry。

Production attach/reopen只能：

1. exact-read并验证checkpoint/root/tree的全部contract bindings与node reachability；
2. 从checkpoint `through_authority_sequence + 1`开始消费bounded restored ranges；
3. 在event/bytes/deadline上限内得到current root并发布viewport/cursor；
4. 超过bounded tail时返回`REBASE_REQUIRED | RECONCILIATION_REQUIRED`并调度session-owned checkpoint maintenance，不扫描完整EventLog，不阻断Runtime继续提交。

Privileged offline doctor可从canonical transcript genesis/checkpoint与EventLog raw ranges分页重建新projection generation，但该路径不进入Host/UI bootstrap。History page port只读cursor root identity指向的confirmed immutable root/tree artifacts，禁止临时扫描EventLog、分别分页transcript/audit后在client merge，或从resident cells伪造durable root。

## 8. Bounded viewport与history

### TUI-FND-VIEW-001 Snapshot

```text
PresentationHistoryViewportSnapshot
  runtime_session_id
  projection_revision
  active_head: PresentationHistoryActiveHeadFact
  ordered_resident_entries: tuple[PresentationHistoryRankedEntryView, ...]
  latest_root_cursor_pair: PresentationHistoryLatestRootCursorPairFact
  resident_cell_count / resident_bytes
  oldest/newest history entry ID / placement key
```

Snapshot只嵌入central factory产生的exact active head，其nested confirmed root是唯一page/cursor authority；不接受caller另行提供的registry/root/tail字段。`latest_root_cursor_pair`必须绑定该nested confirmed root，不能继续回传上一个checkpoint root的cursor；before/after两项不能作为独立snapshot字段被部分更新。`ordered_resident_entries`是confirmed root window应用bounded tail mutations后的单一placement-key ordered view，可同时包含transcript与audit cells；每项display rank只对该active-head basis有效。`follow_tail`、unseen count、selection和expanded IDs属于Go client，不进入Python snapshot。Operational activity使用独立snapshot/generation/cursor，不混入该tuple。

### TUI-FND-VIEW-002 Page port

Unified durable history cursor的唯一identity为：

```text
PresentationHistoryPageCursorFact
  schema_version
  history_root_identity: PresentationHistoryRootIdentityFact
  anchor_history_entry_id
  anchor_placement_key: PresentationHistoryPlacementKeyFact
  cursor_fingerprint

PresentationHistoryPageDirection = before | after

PresentationHistoryPageReadDisposition =
    PAGE
  | CURSOR_STALE
  | REBASE_REQUIRED
  | RECONCILIATION_REQUIRED

PresentationHistoryLatestRootCursorPairFact
  schema_version
  root_identity: PresentationHistoryRootIdentityFact
  before_cursor: PresentationHistoryPageCursorFact | None
  after_cursor: PresentationHistoryPageCursorFact | None
  cursor_pair_fingerprint

PresentationHistoryRootCursorRelationFact
  schema_version
  previous_root_identity
  resulting_root_identity
  relation_kind: strict_prefix_extended | rewritten_generation
  previous_cursor_disposition = retained_pinned
  shared_prefix_entry_count
  shared_prefix_accumulator
  relation_fingerprint

PresentationHistoryRootResidentTransitionFact =
    ResidentEntriesUnchangedFact
  | BoundedOrderedResidentChangesFact
  | ResidentHistoryRebaseRequiredFact

ResidentEntriesUnchangedFact
  schema_version
  transition_kind = unchanged
  before_resident_vector_fingerprint
  after_resident_vector_fingerprint
  exact_equivalence_proof_fingerprint
  transition_fingerprint

PresentationHistoryResidentChangeFact =
    PresentationHistoryResidentUpsertFact
  | PresentationHistoryResidentRemoveFact

PresentationHistoryResidentUpsertFact
  schema_version
  change_kind = upsert
  history_entry_id
  placement_key
  expected_previous_entry_fingerprint: Fingerprint | None
  resulting_ranked_entry: PresentationHistoryRankedEntryView
  change_fingerprint

PresentationHistoryResidentRemoveFact
  schema_version
  change_kind = remove
  history_entry_id
  placement_key
  expected_previous_entry_fingerprint
  change_fingerprint

BoundedOrderedResidentChangesFact
  schema_version
  transition_kind = bounded_ordered_changes
  before_resident_vector_fingerprint
  after_resident_vector_fingerprint
  ordered_changes: tuple[PresentationHistoryResidentChangeFact, ...]
  change_count
  encoded_change_bytes
  transition_limits_policy_fingerprint
  ordered_change_accumulator
  transition_fingerprint

ResidentHistoryRebaseReason =
    RESIDENT_CHANGE_COUNT_EXCEEDED
  | RESIDENT_CHANGE_BYTES_EXCEEDED
  | REWRITE_REQUIRES_SNAPSHOT
  | PINNED_WINDOW_NOT_PROVABLE
  | SESSION_HISTORY_ROTATION_REQUIRED
  | HISTORY_TREE_CAPACITY_EXHAUSTED

ResidentHistoryRebaseRequiredFact
  schema_version
  transition_kind = rebase_required
  before_resident_vector_fingerprint
  target_root_identity: PresentationHistoryRootIdentityFact
  target_active_head_fingerprint
  stable_reason: ResidentHistoryRebaseReason
  bounded_rebase_or_snapshot_token
  token_generation / expires_at_utc
  transition_fingerprint

PresentationHistoryRootAdvancedFact
  schema_version
  base_projection_revision
  resulting_projection_revision
  previous_active_head_fingerprint
  resulting_active_head: PresentationHistoryActiveHeadFact
  latest_root_cursor_pair: PresentationHistoryLatestRootCursorPairFact
  previous_root_relation: PresentationHistoryRootCursorRelationFact
  resident_transition: PresentationHistoryRootResidentTransitionFact
  consumed_checkpoint_candidate_cut_fingerprint
  consumed_tail_prefix_through_sequence
  consumed_tail_prefix_source_range_accumulator
  consumed_tail_prefix_segment_count / consumed_tail_prefix_segment_accumulator
  consumed_tail_prefix_mutation_count / consumed_tail_prefix_mutation_accumulator
  retained_tail_suffix_from_sequence_exclusive / retained_tail_suffix_through_sequence
  retained_tail_suffix_source_range_accumulator
  retained_tail_suffix_segment_count / retained_tail_suffix_segment_accumulator
  retained_tail_suffix_mutation_count / retained_tail_suffix_mutation_accumulator
  checkpoint_full_confirmation_fingerprint
  root_advanced_fingerprint
```

Operational frame没有durable placement key/root，不得进入`PresentationHistoryPageCursorFact`。Cursor通过nested exact root identity间接绑定canonical transcript reducer、event-domain registry、presentation policy registry与audit extractor registry的完整contract identity，不复制任何单个registry fingerprint。Long-Horizon rewrite、latest checkpoint/root推进或registry/reducer contract变化会生成新root，但不改写已发布的immutable root；旧cursor在root仍retained且page codec/protocol仍可解码时继续精确读取旧snapshot。只有root超出retention、artifact/codec不可用或request显式要求latest incompatible projection contract时才返回`CURSOR_STALE | REBASE_REQUIRED`及最新root hint；不得仅按`anchor_history_entry_id`猜位置或悄悄跨root继续分页。

`PresentationHistoryPageCursorFact`只绑定unified root/generation与anchor；不存在feed kind，`cursor_fingerprint`明确不覆盖direction。Direction的唯一domain owner是本次page request。Python port只有一个方法：

```text
PresentationHistoryPagePort.read_page(
  cursor: PresentationHistoryPageCursorFact,
  direction: PresentationHistoryPageDirection,
  limits: PresentationHistoryPageReadLimits,
  absolute_deadline,
) -> PresentationHistoryPageReadOutcome
```

`read_page()`必须：

- 使用stable semantic cursor；
- 走session-owned bounded I/O；
- 接收absolute deadline；
- 限制tree-node reads、entries、page canonical/rendered bytes和artifact reads；
- 返回page high-water、continuity proof和next cursors；
- 不从session genesis全量fold；
- checkpoint不足时typed fail closed或要求offline repair。

Per-request limits只能进一步收紧server materialization policy；effective node reads、entries、canonical bytes、rendered bytes与tree height逐项取request/policy较小值，caller不能放宽durable root冻结的上限。

Page response必须回显validated input cursor fingerprint、validated request direction、page root/generation、ordered history-entry accumulator、page high-water和next cursors；返回cursor仍然不含direction。Page只从cursor绑定的confirmed `PresentationHistoryProjectionRootFact`/tree artifacts按placement-key range有界读取，已经包含全局排序的transcript与audit entries；每个返回项的root-local display rank由subtree counts派生并绑定该root fingerprint。Server与client都不再做cross-feed merge。禁止保留`read_before/read_after`方法、在cursor中复制direction，或在request中添加feed kind；因此不存在cursor、method、request与feed四方不一致的容错矩阵。

Checkpoint FULL安装新root不是ordinary authority-only advance。Foundation必须在同一projection lock内冻结`PresentationHistoryRootAdvancedFact`：resulting active head已经指向new root，并且只保留installed root未覆盖的exact post-cut segment suffix；latest cursor pair绑定new root，old-root relation明确保留旧cursor为pinned snapshot。Consumed cut、proved durable source-prefix coverage、consumed/retained segment tuple identity、mutation identity与resulting active head必须逐字段exact join；retained suffix包含noop segment时，其segment count/source accumulator必须保留，即使mutation count为0。该frame是client-visible paging-authority change，必须CAS推进`projection_revision`并进入有gap detection的projection stream；不能降级成不推进revision的`AuthorityAdvanceFrame`。若frame delivery丢失，client必须由projection revision gap触发snapshot rebuild。

`strict_prefix_extended`只对old tree placement-key ordered entry vector是new tree exact prefix合法。只有应用consumed segment prefix/root swap并保留concurrent segment suffix后，before/after resident vectors逐项byte-identical，才允许`ResidentEntriesUnchangedFact`；其before/after fingerprints必须相等，equivalence proof覆盖root relation、candidate cut与retained suffix。`BoundedOrderedResidentChangesFact`必须在policy count/bytes内，ordered apply后精确得到resulting active head的resident vector与rank basis；duplicate change、same key conflicting upsert、missing expected previous或accumulator mismatch均fail closed。超界或rewrite无法给出bounded changes时使用`ResidentHistoryRebaseRequiredFact`，其target root/head必须与本frame resulting authority相同，token attachment-bound、single-purpose且有界过期。Rewrite使用`rewritten_generation`且不得伪造prefix。两种root relation都允许已发old cursor在retention horizon内继续浏览old root，但它们不再是latest cursor。Follow-tail、jump-to-end、近期页被evict后的rehydration和任何“从当前root继续”请求必须使用new latest cursor pair；old pinned cursor只服务已经明确绑定old root的历史浏览。Root advanced freeze或delivery前crash时，reopen从typed FULL checkpoint confirmation与segment cut/suffix proof重建同一个active head/cursor pair/relation/resident-transition candidate。

### TUI-FND-VIEW-003 O(1) session snapshot

`TerminalUiSessionSnapshot`只能聚合已resident state：

```text
session identity / lifecycle
authority_high_water
projection_revision
viewport snapshot
operational activity snapshot identity / generation / cursor
pending interaction public view
queue head projection
status values
notifications
```

读取snapshot不得执行SQL、artifact、graph、MCP或manager调用。

Foundation在每次confirmed root installation后原子更新resident viewport cache；Gateway snapshot只能读取该cache以及queue/interaction/operational resident projections。History page仍可读取immutable tree artifacts，但必须经session-owned bounded async I/O service执行：admission有硬上限，caller timeout/cancellation只结束waiter，physical executor operation继续由session owner追踪；Host close在同一个absolute deadline下等待其真实退出。Gateway event loop不得直接调用同步`ArtifactStore`/PostgreSQL page、root hydration或checkpoint API。

## 9. Terminal application services

### TUI-FND-CMD-001 Closed service split

不得提供generic `invoke(name, dict)`：

```text
TerminalSessionQueryService
TerminalPromptSubmissionService
TerminalRunControlService
TerminalInteractionResolutionService
TerminalPromptQueueMutationService
TerminalSessionLifecycleService
```

Gateway与Legacy adapter只能持有所需子集。每个mutation request接受由protocol/adapter映射的closed internal DTO，并返回typed operational outcome和可供exact confirmation的authority references。

`TerminalSessionLifecycleService.start_successor_session()`是history rotation的唯一ordinary出口：request必须携带source runtime session、expected `HistorySessionRotationRequiredFact.capacity_state_fingerprint`与stable command identity；FULL只创建并返回一个exact successor session，不迁移旧session的queue、pending interaction、secret continuation或controller lease。Hard exhaustion只能调用该service或privileged repair，任何prompt/queue/run service都必须拒绝继续向旧session增长history。Legacy REPL不获得绕过capacity fence的direct Host入口。

### TUI-FND-CMD-002 Stable command binding

所有mutation service接受：

```text
client_instance_id
attachment_id
command_id
expected_target_id
expected_generation
controller_lease_identity
```

service必须在构造stable event candidate前验证controller/target generation。相同command ID只能确认同一semantic request；不同payload为conflict。

## 10. Durable prompt queue

### TUI-FND-QUEUE-001 Authority

EventLog queue transition chain是唯一semantic/audit authority。PostgreSQL queue item/head/account/reference rows只是支持并发CAS、claim和bounded reopen的projection，每行保存exact head event ID、fingerprint、revision与transition accumulator。

Projection row最小形状冻结为：

```text
PromptQueueItemRow
  runtime_session_id
  queue_item_id
  accepted_ordinal
  delivery_state
  content_retention_state
  row_revision
  head_transition_event_id
  head_transition_sequence
  head_transition_candidate_payload_fingerprint
  requested_delivery_mode
  resolved_delivery_mode
  reservation_identity / generation / ordered_set_fingerprint
  prepared_content_reference / fingerprints
  cancellation / rejection / reconciliation disposition
  reducer_contract_fingerprint
  event_registry_fingerprint
  row_fingerprint

PromptQueueAccountRow
  runtime_session_id
  next_accepted_ordinal
  queue_chain_head_event_id / sequence / payload_fingerprint
  account_revision
  checkpoint_generation / through_sequence / fingerprint
  transition_count / transition_accumulator
  bounded_tail_first_sequence / count / bytes / accumulator
  pending_item_count / reserved_item_count / artifact_bytes
  pending_item_head_set_accumulator
  row_set_accumulator
  reducer_contract_fingerprint
  event_registry_fingerprint
  account_fingerprint
```

Queue item/account row是从event chain可重建的CAS projection，不是semantic fallback。任一row/event mismatch必须fail closed并进入typed repair；adapter不得选择“更新的一边”继续。

### TUI-FND-QUEUE-002 Transition vocabulary

```text
PromptQueueAcceptedEvent
PromptQueueReservationInstalledEvent
PromptQueueReservationReleasedEvent
PromptQueueDeliveryRejectedEvent
PromptQueueCommittedToRunEvent
PromptQueueCommittedToProviderInputEvent
PromptQueueCancelledEvent
PromptQueueReconciliationRequiredEvent

PromptQueueContentRetiredEvent             # orthogonal retention transition
```

所有branch共享且必须由central factory构造：

```text
PromptQueueTransitionHeadFact
  schema_version
  runtime_session_id
  queue_item_id
  accepted_ordinal
  transition_ordinal
  predecessor_event_reference: ContextEventReferenceFact | None
  predecessor_candidate_payload_fingerprint: Fingerprint | None
  previous_delivery_state: PromptQueueDeliveryState | None
  resulting_delivery_state: PromptQueueDeliveryState
  previous_content_retention_state: PromptQueueContentRetentionState
  resulting_content_retention_state: PromptQueueContentRetentionState
  expected_item_revision
  resulting_item_revision
  expected_account_revision
  resulting_account_revision
  transition_semantic_fingerprint
  transition_attribution_fingerprint
  transition_fact_fingerprint
```

Accepted branch必须携带`PreparedPromptQueueContent` exact reference、requested/resolved delivery mode、source client/submission identity与content semantic fingerprint；reservation branch必须携带reservation kind/ID/generation、exact ordered item-set fingerprint、run/safe-point target和absolute deadline；release/commit/cancel/reject/reconciliation branch分别携带closed reason及exact source/reservation/run/provider-input/repair references。`PromptQueueContentRetiredEvent`携带preparation/hold identity、artifact identity fingerprint、retention policy fingerprint与retirement reason，不复制large content。

除accepted event的predecessor与previous delivery state、generation-0 retention genesis外，所有optional join必须non-null。Event validator、queue reducer和PostgreSQL companion三层重算相同transition fingerprint与state matrix；不能只信event内自报的resulting state/revision。

Stable identity算法冻结为：

```text
queue_item_id = H(
  "prompt-queue-item:v1",
  runtime_session_id,
  client_instance_id,
  client_submission_id,
  content_semantic_fingerprint,
)

transition_event_id = H(
  "prompt-queue-transition-event:v1",
  queue_item_id,
  command_id,
  transition_kind,
  predecessor_event_id,
  expected_item_revision,
  reservation_generation_or_zero,
)
```

Same ID + byte-identical pre-commit payload是exact retry；same ID + different payload是authority conflict。

`PromptQueueReservationInstalledEvent`必须携带closed `reservation_kind = steer | follow_up`，并分别投影到不同state；`PromptQueueCommittedToProviderInputEvent`只对应steer，`PromptQueueCommittedToRunEvent`只对应follow-up。Delivery lifecycle完整冻结为：

```text
PromptQueueDeliveryState =
    accepted_pending
  | steer_reserved
  | follow_up_reserved
  | committed_to_active_run
  | committed_to_new_run
  | cancelled
  | delivery_rejected
  | reconciliation_required

PromptQueueContentRetentionState = active | retired

accepted_pending
  -> steer_reserved
      -> committed_to_active_run
      -> released_to_pending -> accepted_pending
      -> delivery_rejected
      -> reconciliation_required
  -> follow_up_reserved
      -> committed_to_new_run
      -> released_to_pending -> accepted_pending
      -> delivery_rejected
      -> reconciliation_required
  -> cancelled
  -> delivery_rejected
  -> reconciliation_required
```

`committed_to_active_run`、`committed_to_new_run`、`cancelled`与`delivery_rejected`是delivery吸收态。`reconciliation_required`关闭普通reservation admission，只能由typed repair owner处理。Explicit steer错过exact safe point必须reject，不得静默改成follow-up。Auto intent可以先通过typed release回到pending，再由新的follow-up reservation generation接管；不得在原reservation内改写mode。

V1物理删除`PromptQueueEditedEvent`及任何in-place content update port。编辑严格等于：

```text
cancel old item FULL
  -> submit replacement as a new stable queue item candidate
```

旧item与replacement拥有不同client submission identity、queue item ID、content semantic identity和event chain。Replacement失败不会复活旧item。

Content retention与delivery lifecycle正交：

```text
content_retention_state = active | retired
```

`PromptQueueContentRetiredEvent`只推进retention state、content reference与artifact hold，不把delivery state改成generic `retired`，也不改变已经冻结的delivery disposition。

### TUI-FND-QUEUE-003 Content

```text
PreparedPromptQueueContent =
    InlineQueueContent
  | ConfirmedArtifactQueueContent

PromptQueueArtifactWriteReceiptIdentityFact
  schema_version
  artifact_storage_contract_fingerprint
  confirmation_status: inserted | confirmed_identical
  artifact_id
  artifact_digest
  artifact_size_bytes
  media_type
  semantic_metadata_fingerprint
  stored_location_identity
  receipt_identity_fingerprint

InlineQueueContent
  schema_version
  content_kind = inline
  canonical_utf8_text
  canonical_payload_sha256
  canonical_byte_count
  media_type = text/plain; charset=utf-8
  codec = utf-8
  content_semantic_reference
  content_semantic_fingerprint
  content_attribution_fingerprint
  content_fact_fingerprint

ConfirmedArtifactQueueContent
  schema_version
  content_kind = confirmed_artifact
  preparation_id
  preparation_fingerprint
  preparation_hold_revision
  stable_content_addressed_artifact_id
  artifact_identity_fingerprint
  canonical_payload_sha256
  canonical_byte_count
  media_type
  codec
  artifact_semantic_reference
  confirmed_write_receipt_identity
  confirmed_write_receipt_fingerprint
  content_semantic_fingerprint
  content_attribution_fingerprint
  content_fact_fingerprint
```

三个fingerprint的覆盖范围固定为：

```text
content_semantic_fingerprint = H(
  "prompt-queue-content-semantic:v1",
  canonical_payload_sha256,
  canonical_byte_count,
  normalized_media_type,
  codec,
  content_semantic_reference,
)

content_attribution_fingerprint = H(
  "prompt-queue-content-attribution:v1",
  content_kind,
  preparation_id_or_inline_admission_identity,
  preparation_fingerprint_or_none,
  hold_revision_or_zero,
  stable_artifact_id_or_none,
  artifact_identity_fingerprint_or_none,
  confirmed_write_receipt_identity_or_none,
  confirmed_write_receipt_fingerprint_or_none,
  storage_location_attribution_or_none,
)

content_fact_fingerprint = H(
  "prompt-queue-content-fact:v1",
  content_semantic_fingerprint,
  content_attribution_fingerprint,
)
```

Inline branch的`content_semantic_reference`固定为registered canonical UTF-8 text contract；artifact branch使用`artifact_semantic_reference`作为同一公式中的semantic reference。Preparation、hold、artifact ID/location、write receipt、storage generation与时间戳不得直接或间接进入`content_semantic_fingerprint`。相同canonical content即使重新prepare、confirmed-identical或迁移storage location，也保持相同content semantic identity；physical occurrence由attribution/fact fingerprint区分。`queue_item_id`只覆盖`content_semantic_fingerprint`，不得覆盖attribution或fact fingerprint。

inline受严格UTF-8 bytes/token cap。Artifact branch必须由central factory exact join artifact row、confirmed write receipt与PREPARED hold；caller不能只提供ID/fingerprint自行证明confirmed。Factory冻结attribution后不得在queue acceptance retry期间换用另一次preparation/write receipt。

### TUI-FND-QUEUE-004 Durable artifact hold

```text
PromptQueueArtifactPreparationHoldFact
  schema_version
  preparation_id
  runtime_session_id
  owner_client_submission_identity
  artifact_id
  artifact_identity_fingerprint
  content_fingerprint
  state: PREPARED | CONSUMED | RELEASED
  consuming_queue_item_id: str | None
  hold_revision
  created_at_utc
  expires_at_utc
  confirmed_write_receipt_identity
  confirmed_write_receipt_fingerprint
  preparation_fingerprint
  hold_row_fingerprint
```

该fact使用registered `FrozenStorageFactBase`，不进入EventLog。Validator矩阵：

- `PREPARED`要求`consuming_queue_item_id is None`；
- `CONSUMED`要求non-empty consuming item并exact joinaccepted event/content reference；
- `RELEASED`保留最后一次consuming identity用于GC/repair audit，不允许回到PREPARED；
- `hold_revision`每次合法CAS恰好加一；
- confirmed receipt identity/fingerprint、artifact identity和content fingerprint必须与锁定的artifact row逐项相等；
- `PromptQueueArtifactWriteReceiptIdentityFact`由现有`ArtifactPutConfirmation`与锁定后的`ArtifactRecord`共同构造；status、ID、digest、bytes、media type、semantic metadata与stored location逐项exact join，不直接持有mutable record/result对象；
- `preparation_fingerprint`覆盖除mutable state/revision之外的稳定preparation identity，`hold_row_fingerprint`覆盖完整state、consuming item与revision；same preparation ID + different stable fact为conflict。

协议：

```text
artifact put/confirm + PREPARED hold
  -> queue acceptance transaction validates/locks hold
  -> event/account/head/reference + PREPARED -> CONSUMED
  -> queue retention retirement transaction
  -> delete/retire content reference + CONSUMED -> RELEASED
  -> later GC may delete RELEASED hold/artifact
```

只对`artifacts.id`建立`ON DELETE RESTRICT`外键；identity在artifact row lock内exact验证。GC准入必须证明没有PREPARED hold和queue-content reference。

Hold状态机与retention transaction固定为：

```text
PREPARED
  -> CONSUMED       # queue acceptance FULL
  -> RELEASED       # pre-accept abandonment/expiry

CONSUMED
  -> RELEASED       # terminal queue-item retention retirement FULL

RELEASED
  -> physical row deletion by GC
```

Acceptance UNKNOWN期间expiry sweeper不得抢先release；必须先在相同lock order下exact-confirmevent、queue row/reference与hold。Waiter cancellation只detach，physical preparation owner继续持有hold直至typed outcome。

### TUI-FND-QUEUE-005 Companion与charge

每个queue transaction使用candidate-bound `PostgresPromptQueueTransactionCompanion`。Closed kind与允许的row/table operation matrix为：

```text
PromptQueueCompanionKind =
    ACCEPT
  | RESERVE
  | RELEASE_RESERVATION
  | COMMIT_TO_ACTIVE_RUN
  | COMMIT_TO_NEW_RUN
  | CANCEL
  | DELIVERY_REJECT
  | RECONCILIATION_LATCH
  | CONTENT_RETIRE
```

Companion identity绑定runtime session、exact ordered event candidate IDs、candidate payload fingerprints、exact batch fingerprint、companion kind、storage mutation plan fingerprint与generation。Handle只能在完整ordered candidate batch和normalized auxiliary plan都冻结后签发。

```text
PromptQueueCompanionChargeFact
  schema_version
  companion_kind
  runtime_session_id
  exact_ordered_event_batch_fingerprint
  item_row_mutation_count
  account_row_mutation_count
  content_reference_mutation_count
  artifact_hold_mutation_count
  total_auxiliary_row_mutations
  normalized_auxiliary_payload_base_bytes
  sequence_wrapper_max_bytes
  revision_wrapper_max_bytes
  conservative_charged_payload_bytes
  charge_contract_fingerprint
  storage_mutation_plan_fingerprint
  charge_fingerprint
```

`PromptQueueCompanionChargeFact`是registered、schema-versioned `FrozenFactBase`；central factory覆盖全部字段并拒绝caller自报fingerprint。Free-form `max_rows_by_relation`或dict table plan被禁止；`companion_kind -> exact allowed table/op set + per-relation max rows + per-row byte contract`由closed storage registry唯一提供，并纳入`charge_contract_fingerprint`。

sequence分配前按normalized mutation plan和storage registry物理上限冻结固定保守charge：

```text
conservative_charged_payload_bytes
  = normalized_auxiliary_payload_base_bytes
  + sequence_wrapper_max_bytes
  + revision_wrapper_max_bytes
```

Stored rebind后、任何auxiliary SQL前验证actual row counts、table/op set、stored event batch fingerprint、plan fingerprint和`actual_bytes <= conservative_charged_payload_bytes`；超界或affected-row drift整批rollback。Materialization account按保守值结算，不在commit后回填actual、退款或追加terminal charge。Large artifact bytes由ArtifactStore budget结算，不在queue companion重复收费。

### TUI-FND-QUEUE-006 Checkpoint

```text
PromptQueueReducerContractFact
  schema_version
  reducer_id
  reducer_version
  reducer_contract_fingerprint

PromptQueueEventDomainRegistryBinding
  schema_version
  registry_id
  registry_version
  ordered_event_type_schema_accumulator
  registry_fingerprint

PromptQueueDomainCheckpointFact
  schema_version
  runtime_session_id
  reducer_id
  reducer_version
  reducer_contract_fingerprint
  event_registry_id
  event_registry_version
  event_registry_fingerprint
  checkpoint_generation
  through_sequence
  transition_count
  transition_accumulator
  account_revision
  next_accepted_ordinal
  pending_item_head_set_accumulator
  queue_row_set_accumulator
  checkpoint_fingerprint

PromptQueueHeadReceipt
  schema_version
  reducer_contract_fingerprint
  event_registry_fingerprint
  checkpoint_generation
  checkpoint_fingerprint
  bounded_tail_first_sequence
  bounded_tail_last_sequence
  bounded_tail_count
  bounded_tail_accumulator
  resulting_queue_head_event_id: str | None
  resulting_queue_head_payload_fingerprint: Fingerprint | None
  resulting_account_revision
  resulting_row_set_accumulator
  receipt_fingerprint

PromptQueueCheckpointCommitGuard                 # process-local
  runtime_session_id
  expected_previous_through_sequence
  expected_previous_payload_fingerprint
  expected_account_revision
  expected_queue_head_event_id
  expected_queue_head_payload_fingerprint
  expected_row_set_accumulator
  expected_pending_item_head_set_accumulator
  guard_generation
```

前四个durable/proof carrier均使用registered、schema-versioned `FrozenFactBase`与domain-separated central factory；commit guard为不可序列化、generation-scoped process-local carrier。Reducer fingerprint覆盖完整delivery/retention transition matrix、row/account lowering、ordering、capacity、reservation与retirement；registry binding覆盖本规格所有queue EventType的schema/domain identity。

V1复用`runtime_projection_checkpoints` mutable validated row：

```text
projection_kind = prompt_queue.v1
through_sequence = PromptQueueDomainCheckpointFact.through_sequence
projection_schema_version = registered queue checkpoint schema version
ledger_prefix = exact committed prefix at through_sequence
validation_base_through_sequence = previous trusted checkpoint through_sequence
validation_base_state_payload = previous trusted checkpoint state payload
state_payload = canonical PromptQueueDomainCheckpointFact
payload_fingerprint = central raw-checkpoint fingerprint
```

不新增immutable checkpoint table、artifact carrier、content-addressed checkpoint ID或第二套retention协议。`PromptQueueDomainCheckpointFact`只是该mutable validated row的typed state payload；queue account中的checkpoint generation/through/fingerprint是唯一pointer projection。

Canonical generation-0 empty checkpoint必须由session bootstrap transaction确定构造：

```text
checkpoint_generation = 0
through_sequence = 0
transition_count = 0
transition_accumulator = H(
  "prompt-queue-transition-genesis:v1",
  runtime_session_id,
  reducer_contract_fingerprint,
  event_registry_fingerprint,
)
account_revision = 0
next_accepted_ordinal = 1
pending_item_head_set_accumulator = canonical_empty_accumulator
queue_row_set_accumulator = canonical_empty_accumulator
resulting_queue_head_event_id = None
resulting_queue_head_payload_fingerprint = None
```

Head fields只允许在generation-0 empty genesis同时为`None`。Existing sessions由offline migration在证明无queue transition后安装genesis；production admission遇到missing genesis必须fail closed，不得临时自建。

commit choreography：

1. writer lock内读取checkpoint/account/head，计算soft/hard watermark与本次maximum burst，安装或取得shared checkpoint attempt；
2. release writer lock；
3. lock外await/wake attempt；
4. owner使用`CHECKPOINT_COMMIT`与`CHECKPOINT_MAINTENANCE` capacity；
5. transaction锁session/checkpoint/account/head/row set，验证完整guard，覆盖validated checkpoint并CAS account pointer/bounded-tail base；
6. 同一snapshot重读checkpoint/account/head，构造`PromptQueueHeadReceipt`；
7. admission重新进入writer并从authority、capacity和safe point从头验证。

Registry冻结以下六个上限：

```text
SOFT_TAIL_MAX_TRANSITIONS = 192
HARD_REOPEN_MAX_TRANSITIONS = 256
SOFT_TAIL_MAX_BYTES = 4 MiB
HARD_REOPEN_MAX_BYTES = 8 MiB
MAX_ADMITTED_TRANSITION_BURST = 1
MAX_ADMITTED_TRANSITION_BURST_BYTES = 64 KiB
```

并强制：

```text
SOFT_TAIL_MAX_TRANSITIONS + MAX_ADMITTED_TRANSITION_BURST
  <= HARD_REOPEN_MAX_TRANSITIONS
SOFT_TAIL_MAX_BYTES + MAX_ADMITTED_TRANSITION_BURST_BYTES
  <= HARD_REOPEN_MAX_BYTES
```

达到soft watermark必须wake session-owned owner；计划burst会越过hard bound时普通admission不得提交。普通admission不得持writer lock等待checkpoint owner。Waiter cancellation只detach；Host close在释放EventLog/DB前bounded drain owner。

V1每个RuntimeSession transaction最多包含一个queue-domain transition event；matching RunStart/provider-input/accounting events不计入transition count，但仍受既有EventLog/physical-account batch bound。Queue event canonical payload超过64 KiB在stable candidate形成前typed reject；large content必须走artifact reference，不能扩大该上限。

Checkpoint FULL要求generation恰好`previous + 1`、through sequence等于observed queue head、transition/head/row-set accumulator recurrence连续。NONE复用same candidate。UNKNOWN必须在同一database snapshot读取checkpoint row和account pointer，结果仅允许：`FULL | NONE | SUPERSEDED_BY_COMPATIBLE_WINNER | RECONCILIATION_REQUIRED`；不能只凭generation增加接纳winner。

### TUI-FND-QUEUE-007 Reopen与repair

Production reopen只能在一个database snapshot中读取latest trusted checkpoint、account/head receipt与current pending/reserved row set，再通过queue-event-type indexed port分页读取`(checkpoint.through_sequence, head]`的bounded typed delta。Reducer必须重算transition count/accumulator、head identity、account revision、pending head-set与row-set accumulator；全部exact match后才安装projection。Mismatch、deadline耗尽或events/bytes tail超界返回typed `queue_projection_reconciliation_required`，不得扫描session genesis。

Privileged offline doctor只能在exclusive maintenance barrier下分页fold完整queue transition chain，重建checkpoint/row/account并写typed repair receipt；它不能扫描/解码无关session events，也不能进入Host open、UI attach或普通reconnect热路径。

## 11. Queue dispatch原子边界

### TUI-FND-QUEUE-008 Follow-up

Follow-up queue item consumption与matching RunStart/ingress authority必须通过RuntimeSession writer和transaction companion同事务提交。FULL后item才离开queue；NONE保留same reservation candidate；UNKNOWN进入exact confirmation。

### TUI-FND-QUEUE-009 Steer

V1唯一safe point：`after_tool_results_before_followup_model_input_freeze`。Consumption与provider-input generation必须同事务exact join。Provider input candidate一旦冻结，steer不可再进入该generation。

### TUI-FND-QUEUE-010 Reservation failure

preflight、target、RunStart/provider-input preparation或capacity在stable candidate前失败时：

- 可重试：写`ReservationReleased`回pending；
- deterministic非法：写`DeliveryRejected`；
- commit/authority未知：写或安装`ReconciliationRequired` owner。

## 12. Interaction与secret service boundary

### TUI-FND-INT-001 Event-safe view

Foundation从exact durable pending authority投影：

```text
TerminalInteractionRequestView =
    ApprovalRequestView
  | PlanQuestionView
  | PlanExitView
  | McpFormRequestView
  | McpPrivateUrlRequestViewRedacted
```

View不包含private URL、form response、continuation plaintext或secret digest。

### TUI-FND-INT-002 Python secret owner

MCP encrypted continuation store是storage-only durable authority；Host MCP interaction service是唯一decrypt/hydration owner。Foundation只向Protocol adapter提供attachment-bound secret lease issuer，不暴露plaintext DTO到普通projection。

Reconnect必须从exact pending continuation签发新lease，不能恢复旧client object。Resolution/cancel/expiry/closure才能删除底层continuation；UI detach只revoke attachment lease。

Secret transport、peer validation和frame规则由`TUI-PROTO-*`拥有。

## 13. Close与failure ownership

### TUI-FND-LIFE-001 Client independence

Client detach、buffer overflow、decode failure或renderer crash不得：

- cancel active run；
- close RuntimeSession；
- release queue/job physical owner；
- block Host close；
- 改变durable publication outcome。

### TUI-FND-LIFE-002 Foundation close

RuntimeSession close顺序：

1. stop new external UI attachments/commands，并停止新的Foundation I/O admission；
2. 完成既有RuntimeSession/run/queue terminalization，使其最后一批durable events进入tap；
3. revoke external controller、secret与client root leases；external observation subscriber可立即detach；
4. 保留Foundation-owned internal tap subscriber，drain stable checkpoint/catch-up/root-install delivery owner以及全部executor中的artifact/DB physical operations；
5. internal owner全部terminal后才detach internal tap subscriber，并关闭Foundation service；
6. releaseRuntime reducer、DB与artifact dependencies。

External UI subscribers本身不参与close blocker；Foundation-owned internal tap subscription只是收口工具，不是client dependency。Stable checkpoint attempt、confirmed-but-undelivered root以及已经physical-start的I/O必须参与close blocker。取消async worker不能代表底层executor operation已取消；共享deadline到期而任一physical operation或checkpoint owner仍未terminal时，Host close必须明确blocked，不能继续关闭reducer、connection pool或artifact dependency。

## 14. 实施切片归属

当前renderer-neutral hard cut按INFRA-0至INFRA-5完成；它们是同一Python authority边界的可独立验证切片，不依赖真实renderer：

| Slice | Foundation交付 | 当前状态 |
|---|---|---|
| INFRA-0 | 最终DTO/port/registry owner、fingerprint与AST import rule | IMPLEMENTED |
| INFRA-1 | stored envelope、built pair、physical receipt、confirmation与restored range proof | IMPLEMENTED |
| INFRA-2 | canonical live/restored fold、committed tap、bootstrap/catch-up/GAP、operational store | IMPLEMENTED |
| INFRA-3 | unified history projection、persistent tree、checkpoint、capacity、viewport/page | IMPLEMENTED |
| INFRA-4 | application services、durable queue、artifact hold、secret lease与bounded reopen | IMPLEMENTED |
| INFRA-5 | versioned Python protocol server与test-only headless conformance consumer | IMPLEMENTED |

Bubble Tea S0 feasibility、全部`TUI-BT-*`、Go packaging、PTY renderer与默认TTY activation继续为`DEFERRED`；它们不参与Foundation完成判定。

## 15. 文件修改面

### 15.1 Event vocabulary与PostgreSQL hard cut

Hard cut前代码真值为`AGENT_EVENT_SCHEMA_VERSION = 8`、PostgreSQL migration head `10`。INFRA-1的raw-envelope owner迁移不新增durable event；INFRA-4启用queue时必须在同一个不可拆分cutover中：

```text
AGENT_EVENT_SCHEMA_VERSION: 8 -> 9
PostgreSQL migration: 0011_terminal_presentation_queue.sql
expected catalog: expected_catalog_v11.json
protected relation registry: 0011_runtime_write_protected_relations_v1.json
```

Event schema generation 9必须同时修改并验证：

- `EventType`新增全部`PROMPT_QUEUE_*` closed values；
- `AgentEvent` union加入本规格8类delivery/repair events与1类content-retirement event；
- 每个event class拥有closed typed payload、schema version、schema fingerprint与domain contract；
- `DEFAULT_EVENT_SCHEMA_REGISTRY`注册current binding，historical decoder可以按exact generation-8/9 row identity读取；
- `_event_domain()`/event-domain registry将queue events归入registered `prompt_queue` non-transcript audit domain；
- transcript semantic/acceleration registries显式把queue events分类为non-transcript，不能进入canonical transcript reducer；
- event schema catalog、golden vectors、serialization round-trip、historical decoder与architecture expected count一起更新。

Migration 0011至少创建并纳入schema manifest：

```text
prompt_queue_items
prompt_queue_accounts
prompt_queue_content_references
prompt_queue_artifact_preparation_holds
```

它还必须：

- 为queue item/account state、revision、ordinal、head event reference和fingerprints安装closed CHECK constraints与unique indexes；
- 对runtime session和`artifacts.id`建立正确FK，artifact删除使用`ON DELETE RESTRICT`；
- 为session/state/accepted ordinal、head event、hold expiry、content reference建立bounded reopen/maintenance indexes；
- 更新`POSTGRES_SCHEMA_MANIFESTS`到`range(12)`、migration registry name/checksum/postcondition、runtime grant policy、reserved/protected relation registry；
- 更新schema verifier、migration expected catalog、grant golden、manifest fingerprint和reset fixtures；
- 在database maintenance epoch/barrier内为所有existing sessions安装queue account与canonical generation-0 checkpoint，并证明旧schema不可能含queue transitions；
- binary activation与migration 0011不可拆分：head 10 binary不得生产queue event，head 11 binary遇到缺失genesis/account必须fail closed；不保留dual-read、lazy table creation或旧queue fallback。

### 15.2 新增

- `src/pulsara_agent/primitives/stored_event.py`
- `src/pulsara_agent/primitives/terminal_presentation.py`
- `src/pulsara_agent/primitives/prompt_queue.py`
- `src/pulsara_agent/ports/stored_event.py`
- `src/pulsara_agent/ports/terminal_presentation.py`
- `src/pulsara_agent/ports/terminal_commands.py`
- `src/pulsara_agent/runtime/terminal_presentation/observation.py`
- `src/pulsara_agent/runtime/terminal_presentation/projection.py`
- `src/pulsara_agent/runtime/terminal_presentation/history_projection.py`
- `src/pulsara_agent/runtime/terminal_presentation/history_checkpoint.py`
- `src/pulsara_agent/runtime/terminal_presentation/history_tree_store.py`
- `src/pulsara_agent/runtime/terminal_presentation/history_retention.py`
- `src/pulsara_agent/runtime/terminal_presentation/history_capacity.py`
- `src/pulsara_agent/runtime/terminal_presentation/viewport.py`
- `src/pulsara_agent/runtime/terminal_presentation/snapshot.py`
- `src/pulsara_agent/runtime/terminal_presentation/application.py`
- `src/pulsara_agent/runtime/terminal_presentation/prompt_queue.py`
- `src/pulsara_agent/runtime/terminal_presentation/prompt_queue_checkpoint.py`
- `src/pulsara_agent/event_log/postgres_prompt_queue.py`
- `src/pulsara_agent/event_log/historical_decoder.py`
- `src/pulsara_agent/storage/migrations/sql/0011_terminal_presentation_queue.sql`
- `src/pulsara_agent/storage/migrations/expected_catalog_v11.json`
- `src/pulsara_agent/storage/migrations/resources/0011_runtime_write_protected_relations_v1.json`

### 15.3 修改

- `src/pulsara_agent/event_log/protocol.py`
- `src/pulsara_agent/event_log/serialization.py`
- `src/pulsara_agent/event/events.py`
- `src/pulsara_agent/event/__init__.py`
- `src/pulsara_agent/event_log/__init__.py`
- `src/pulsara_agent/event_log/postgres.py`
- `src/pulsara_agent/event_log/in_memory.py`
- `src/pulsara_agent/ports/event_write.py`
- `src/pulsara_agent/runtime/session.py`
- `src/pulsara_agent/runtime/authority_materialization/transcript_reducer.py`
- `src/pulsara_agent/runtime/authority_materialization/transcript_restore.py`
- `src/pulsara_agent/runtime/authority_materialization/doctor.py`
- `src/pulsara_agent/runtime/authority_materialization/checkpoint_service.py`
- `src/pulsara_agent/runtime/authority_materialization/contracts.py`
- `src/pulsara_agent/runtime/context_input/replay.py`
- `src/pulsara_agent/runtime/subagent/runtime.py`
- `src/pulsara_agent/primitives/transcript_projection.py`
- `src/pulsara_agent/host/session.py`
- `src/pulsara_agent/host/core.py`
- `src/pulsara_agent/host/production_composition.py`
- `src/pulsara_agent/storage/migrations/manifest.py`
- `src/pulsara_agent/storage/migrations/registry.py`
- `src/pulsara_agent/storage/migrations/grants.py`
- `src/pulsara_agent/storage/migrations/runner.py`
- `src/pulsara_agent/storage/migrations/verifier.py`
- event/PostgreSQL schema golden vectors与reset fixtures
- `tests/test_runtime_committed_writer.py`
- `tests/test_authority_materialization_contract.py`
- `tests/test_context_transcript_projection.py`
- `tests/test_transcript_projection_contract.py`
- `tests/test_terminal_presentation_history_projection.py`
- `tests/test_terminal_presentation_history_postgres.py`
- `tests/test_terminal_presentation_history_capacity.py`

### 15.4 Raw envelope无shim迁移集合

INFRA-1必须迁移baseline AST inventory中的全部25个production consumer；该集合是hard-cut baseline，不是抽样文件清单：

```text
src/pulsara_agent/event_log/__init__.py
src/pulsara_agent/event_log/in_memory.py
src/pulsara_agent/event_log/postgres.py
src/pulsara_agent/event_log/protocol.py
src/pulsara_agent/llm/commit.py
src/pulsara_agent/memory/compaction/evidence.py
src/pulsara_agent/memory/governance/batch_input.py
src/pulsara_agent/memory/governance/evidence.py
src/pulsara_agent/runtime/authority_materialization/checkpoint_service.py
src/pulsara_agent/runtime/authority_materialization/contracts.py
src/pulsara_agent/runtime/authority_materialization/evidence_cursor.py
src/pulsara_agent/runtime/authority_materialization/transcript_reducer.py
src/pulsara_agent/runtime/authority_materialization/transcript_restore.py
src/pulsara_agent/runtime/context_input/event_slice.py
src/pulsara_agent/runtime/context_input/live.py
src/pulsara_agent/runtime/long_horizon/checkpoint.py
src/pulsara_agent/runtime/long_horizon/reducer_contract.py
src/pulsara_agent/runtime/projection_jobs/compaction_memory_driver.py
src/pulsara_agent/runtime/projection_jobs/compaction_memory_settlement.py
src/pulsara_agent/runtime/projection_jobs/postgres_repository.py
src/pulsara_agent/runtime/projection_jobs/source.py
src/pulsara_agent/runtime/run_execution/authority.py
src/pulsara_agent/runtime/run_execution/recovery.py
src/pulsara_agent/runtime/run_execution/registry.py
src/pulsara_agent/runtime/terminal/process.py
```

现有test consumers`tests/test_context_evidence_cursor.py`、`tests/test_host_lifecycle_contract.py`、`tests/test_run_boundary_contract.py`同PR切换。Gate不只比较“25”：它保存canonical AST import observation `(source module, imported module, symbol, import kind, line-independent observation fingerprint)`；任何新增old-owner import、dynamic literal import、class/factory duplicate definition都失败。Inventory因用户并行代码变化增长时，实施者必须迁移新observation，不能更新baseline以放行旧path。

### 15.5 删除/禁止

- `event_log.protocol.RawStoredEventEnvelope`旧public definition/import path；
- UI通过`RuntimeEventPublisher`订阅；
- renderer从`AgentEvent`自行fold；
- UI/runtime shared mutable dict snapshot；
- generic string command dispatcher；
- process-local-only prompt queue；
- queue row作为semantic authority；
- production reopen完整session scan。

## 16. Gate与测试

### TUI-FND-GATE-001 Architecture

- projection/foundation不importclient protocol adapter、prompt_toolkit、Textual或Go；
- raw envelope只有一个AST definition owner；
- old raw-envelope owner/import/factory/decode observation为零，25-file baseline已全部迁移；
- 所有production append返回complete storage receipt；
- normal append receipt只消费sealed encoder-built pairs，writer critical path的historical decoder调用数恒为零；exact candidate confirmation消费decoder-hydrated pairs；generic restore消费range proofs；
- no UI subscriber in publisher registry；
- tap/ring element只能是不可拆分的`CommittedPresentationTapEntry`，禁止独立raw/fold lane或对partial overlap做局部追加；
- `TranscriptProjectionStateStore`只暴露`apply_live_committed(receipt)`与`fold_restored_range(range_proof)`；旧tuple/rebuild API和raw-envelope re-encode为零；
- `register_committed_reducer`只接收双入口`CommittedReducerIngressPort`，registration不保存ambiguous tuple callback；
- generic reopen、doctor、repair与catch-up无法构造`StoredEventBatchCommitReceipt`，只有live/exact-candidate FULL路径可以；
- `UserPromptCell`、`AssistantMessageCell`、`ToolTerminalCell` factory只接受canonical transcript leaf/delta，禁止接受raw `AgentEvent`；
- presentation policy按canonical-transcript与durable-audit purpose双轴注册，禁止event-type互斥classifier；
- durable-audit extractor只能读取policy field allowlist，且其closed output union物理排除`UserPromptCell`、`AssistantMessageCell`与`ToolTerminalCell`；
- audit extractor policy包含ID/version/fingerprint，process registry按三元组exact resolve并拒绝same ID/version fingerprint conflict；
- `PresentationHistoryProjectionOwner`是transcript/audit全局placement的唯一owner；不存在transcript-only/audit-only history root、page port或client merge；
- `PresentationHistoryProjectionRootFact`同时覆盖history projection、transcript reducer、event-domain registry、presentation policy registry与audit extractor registry contract fingerprints；
- persistent tree entry/node/root/cursor的durable identity只使用stable placement key；`history_ordinal`、`anchor_ordinal`、`next_history_ordinal`及等价连续rank字段的definitions/serialization observations为零；
- placement-key contract只有一个registered ID/version/fingerprint与fixed 75-byte encoder/decoder；tree/root/cursor/protocol mapper不得另建kind order、sentinel或framing常量；
- `CanonicalTranscriptPlacementTransitionProofFact`与anchor tombstone只能由transcript reducer定义/构造；presentation不得签发、重编号或迁移stable spine coordinate；
- history entry ID、anchor slot、placement key与entry fingerprint遵守无环derivation；fingerprint dependency graph gate拒绝任何placement-key/entry-ID自引用；
- active-tail owner必须保存逐sequence segment tuple；checkpoint candidate必须携带segment-boundary typed cut与source-prefix transition proof，root swap必须保留未覆盖segment suffix；任何aggregate-only split、无prefix proof的`tail.clear()`、rewrite-as-append或FULL-implies-empty-tail路径被AST/contract gate禁止；
- resident root transition是三个closed DTO branch，Protocol mapper不得只传enum tag、dict payload或省略before/after vector proof；
- `DurableHistoryCell`与`OperationalActivityCell`的Python/wire branch不相交；旧`TerminalSemanticCell`与`RunLifecycleCell`定义、alias、wire branch均为零；
- operational activity不得出现在history root/page/cursor、durable projection delta或`projection_revision` reducer中；
- `JoinedRawStoredEventRangeProof.__module__`必须是`pulsara_agent.ports.stored_event`，EventLog/RuntimeSession/doctor/repair直接import最终owner，不设置event_log旧路径alias或第二份overlap DTO；
- acceptance/suppression、tool pairing和terminal-document winner只在`TranscriptProjectionStateStore`实现；
- prompt queue relation mutation只有唯一companion/repository owner；
- gateway只能通过application services mutation。
- growth quote/reservation与capacity decision只有Foundation owner；Gateway、Legacy与Go不得重算ordinary projected count或借用terminalization maintenance reserve。

### TUI-FND-GATE-002 Deterministic tests

- raw envelope/owned event exact join；
- business/accounting exhaustive partition；
- normal FULL与confirmation FULL receipt同形；
- normal FULL receipt construction不调用historical decoder；
- partial/non-contiguous confirmation不能构造receipt；
- arbitrary stored-row page只能构造range proof，不能构造receipt或tap entry；
- live batch、single restored range、逐事件range与不同bounded page partition最终canonical state完全相同；
- checkpoint H经range fold后只在下一条完整live entry边界切换，跨H entry不会重复或遗漏；
- tap next/whole-duplicate/partial-overlap/gap matrix，partial overlap必须detach而非append suffix；
- bootstrap concurrent commit不丢失；
- canonical fold result exact join source stored-batch fingerprint、first/last sequence与envelope accumulator；
- `RunStartEvent`/`RunEndEvent`同时覆盖transcript reducer与lifecycle extractor而不产生重复正文cell；
- audit extractor的same ID/version + different fingerprint在composition时fail closed；retained historical ID/version/fingerprint可exact rebind，缺失binding返回typed rebase/reconciliation；
- canonical transcript fold delta与raw audit feed exact join，suppressed output只生成audit cell；
- RunStart/RunEnd lifecycle只生成`AuditCell(audit_kind="run_lifecycle")`，没有unknown cell branch；
- 同一live/restored sequence range产生相同canonical-spine anchors、audit anchor gap proofs、`PresentationHistoryPlacementKeyFact`、ordered entries与root accumulator；
- canonical leaf在later sequence被single/interval replacement时继承原anchor，不移动到history尾部；两个canonical anchors永不因audit sequence或replacement sequence交换顺序；
- append/single replace/interval replace/retire四类placement transition proof逐项验证before/after spine、predecessor、resulting anchor与tombstone；retired anchor上的before/after audit保持stable placement coordinate且不渲染canonical cell；
- single replacement保持stable anchor slot、canonical entry ID与placement key；interval replacement slot按ordered predecessor slots唯一派生，identity graph无递归；
- 同一RunStart同时产生lifecycle audit与user transcript leaf时，typed `before_leaf` anchor稳定得到唯一全局顺序；`ledger_sequence` anchor只能经proved transcript gap插入；
- placement-key golden逐kind验证typed coordinate matrix、0/`UINT64_MAX` sentinel、uint bounds、32-byte tiebreaker、exact 75-byte `PHK1` framing与unsigned lexicographic order；same ID/version fingerprint conflict和missing historical binding均fail closed；
- checkpoint/root/tree exact restore可重建相同viewport和cursor；registry fingerprint变更生成新root，但retained旧root cursor仍可读旧snapshot；
- latest checkpoint/root推进时旧cursor仍可读自身immutable root；只有retention lease/TTL规则允许后的GC才使其stale；
- retained historical root/node codec exact resolve；same ID/version different fingerprint conflict与missing codec fail-closed；
- persistent-tree append/rewrite只path-copy受影响paths，checkpoint work不随total session history线性增长；page read node count满足`O(tree height + page size)` hard bound；
- 在旧placement key之间插入audit、删除entry或interval replacement时，未受影响suffix的entry/node key byte-identical，只path-copy affected leaves/ancestor paths；root-local display rank可变化但不进入durable fingerprint；
- materialization policy所有node/tree/tail/capacity-reserve/retention/read数字逐项进入fingerprint，soft<hard与tree capacity validator全绿；
- checkpoint normal/cancel/timeout分别覆盖FULL/NONE/UNKNOWN/CONFLICT，byte-identical winner与proved compatible successor adoption，UNKNOWN reopen reconciliation；
- checkpoint NONE/UNKNOWN/CONFLICT后重复使用同一个attempt/candidate fingerprint，live retry最终FULL；close deadline前未收口时typed blocked且owner不丢失；
- checkpoint candidate冻结后并发append/noop增长tail：每个noop sequence仍形成空mutation segment；FULL只消费proved segment prefix、保留exact segment suffix，resulting head允许non-empty tail；compatible successor逐跳重放durable source-prefix transition proof后消费proved longer prefix；post-cut rewrite/retirement废弃candidate或typed rebuild；
- checkpoint FULL后root-advance delivery原子携带new active head、new latest cursor pair、old pinned-root relation、consumed segment prefix、retained segment suffix与closed resident transition；frame丢失由projection revision gap重建；
- resident unchanged/changes/rebase validators分别覆盖equal-vector proof、ordered upsert/remove count+bytes+accumulator、exact target root/head token；malformed branch全部fail closed；
- capacity逐项验证`confirmed + current tail + active remaining reservations + requested quote`；tail materialization与reservation remaining在同一lock结算而不重复计数；terminalization maintenance reserve只参与`soft + reserve <= hard maximum`。NONE释放、FULL结算unused、UNKNOWN保留reservation；soft threshold返回typed rotation且已准入terminalization仍可完成；hard maximum不silent truncate/evict；
- cursor与request均不含feed kind，page直接返回unified globally ordered entries；
- operational activity不进入durable history page，也不因coalesce/drop改变history root或projection revision；
- tap subscriber在checkpoint physical I/O期间overflow进入GAP后，以durable observed high-water重新bootstrap并最终追到latest sequence；不会留下仍alive但无subscriber的worker；
- presentation projection idempotence与revision；
- viewport hard bounds、paged history、cursor stale/rebase矩阵；
- cached snapshot不执行I/O；重启后的首次history page通过session-owned bounded executor执行，阻塞artifact/SQL read不冻结event loop；waiter cancellation后Host close仍等待physical read真实退出；
- history page、anchor lookup、viewport materialization与checkpoint path-copy使用调用方冻结的同一个absolute deadline；root/tree每次`ArtifactStore.get_text()`都必须原样传递该deadline，PostgreSQL connection checkout与statement timeout不得续期或回退为`None`；真实PostgreSQL表锁测试必须在blocker仍持有时观察目标read自行timeout并物理退出；
- Long-Horizon rewrite后旧cursor可以继续读取其仍retained的旧immutable root，但不得按cell ID静默映射到新root；跨root只接受proved replacement cursor或typed rebase；
- cursor不含direction，`read_page()`只消费request-owned direction；
- queue V1 cancel+replacement，任何`PromptQueueEditedEvent`/in-place content mutation均不存在；
- identical content跨不同artifact preparation/write receipt保持相同semantic fingerprint与queue identity，attribution/fact fingerprint不同；
- queue transition、artifact hold、charge、checkpoint与safe-point CAS；
- close/drain与client detach isolation；
- stored-event pair同wrapper identity、不同canonical payload bytes必须拒绝；receipt进入transcript fold/tap前再次验证owned payload与raw bytes join；
- MCP form response错误request key、stale batch/round owner、controller/attachment generation、owner epoch或TTL均拒绝并best-effort zero mutable buffer。

### TUI-FND-GATE-003 PostgreSQL

- queue acceptance/hold/account/head原子性；
- UNKNOWN exact confirmation；
- checkpoint/account pointer CAS；
- presentation history path-copy node/root confirm-identical、checkpoint FULL/NONE/UNKNOWN/CONFLICT、compatible-winner adoption、orphan/reachability GC与bounded restored-tail；
- GC delete restriction；
- conservative charge rollback；
- production bounded reopen和offline doctor repair；
- migration 0011、event generation 9、runtime grants/protected registry与expected catalog exact verification；
- head-10/head-11 activation、existing-session genesis与reset/cutover tests。

## 17. Definition of Done

1. `TUI-FND-*`全部有requirement-to-code-to-test traceability。
2. Normal write的raw stored envelope只编码一次并贯穿persistence、accounting与receipt；exact confirmation与generic restore只从canonical row hydrate，绝不从decoded event重编码。
3. Runtime writer从不等待UI observer或client。
4. `TranscriptProjectionStateStore`仍是canonical transcript acceptance、suppression、pairing与terminal-document join唯一owner；presentation只拥有显示投影和registered durable-audit purpose classification。
5. Renderer读取O(1) snapshot，history走bounded page port。
6. `authority_high_water`与`projection_revision`物理分离。
7. Durable prompt queue以EventLog transition为唯一semantic authority。
8. Large paste不存在FULL queue reference指向未确认或可被GC删除的artifact。
9. Queue checkpoint有genesis、contract binding、soft/hard watermark和bounded reopen。
10. Follow-up/steer consumption与RunStart/provider-input authority同事务。
11. Secret不进入普通projection，Host是唯一hydration owner。
12. Client crash/detach不取消run或阻塞Host close。
13. Foundation不存在任何prompt_toolkit/Textual/Bubble Tea import。
14. Legacy REPL与Gateway都不能绕过closed application services创建新mutation semantics。
15. V1 queue不存在`PromptQueueEditedEvent`或in-place content update；编辑只由cancel FULL加new acceptance表达。
16. Raw envelope DTO、builder、historical decoder分别只有一个final owner，old import path及全部AST observations为零。
17. Event schema generation 9与PostgreSQL migration 0011在同一INFRA-4 hard cut激活，catalog/grants/protected registry/reset审计全绿。
18. Durable history只有一种`PresentationHistoryProjectionRootFact`和一个page port；每个root只有一对directionless cursor，每个attachment恰有一个latest pair及policy-bounded pinned old-root cursors。Root绑定transcript reducer、event-domain registry、presentation policy registry与audit extractor registry完整contract；rewrite不改写旧root，跨root或旧root退役时只返回proved replacement cursor或typed stale/rebase。
19. Normal writer通过encoder-built pair形成receipt且不重复decode；只有exact candidate FULL confirmation可通过historical decoder形成同形receipt；generic restore只形成range proof。
20. Transcript reducer用同一pure core处理live receipt与restored range，最终canonical state与physical batch/page grouping无关；tap只保存live raw+fold复合entry。
21. Event policy以transcript/audit双purpose建模；同一事件可参与两种投影，但audit extractor不能生成canonical transcript cell。
22. Queue content semantic identity不覆盖preparation、hold、artifact/write receipt或storage attribution；queue item ID只依赖semantic fingerprint。
23. Audit extractor以ID/version/fingerprint exact rebind，retained historical binding缺失时fail closed。
24. History direction只由page request拥有；cursor和port method不复制direction。
25. RuntimeSession与subagent的全部committed-reducer registrations使用双入口port；initial catch-up、reconcile、doctor、restore和repair没有tuple/fake-receipt旁路。
26. Unified history root只拥有placement/order；canonical transcript leaf与durable audit cell的语义owner保持不变，server/client都不存在cross-feed merge。
27. Presentation history checkpoint拥有canonical generation-0、bounded persistent tree、path-copy node/root、完整materialization policy、typed CAS confirmation、bounded production restore与offline doctor rebuild。
28. `RunLifecycleCell`物理不存在；run lifecycle使用closed `AuditCell(run_lifecycle)`贯穿Foundation与Protocol；未来Go adapter必须消费同一closed branch。
29. `DurableHistoryCell`与`OperationalActivityCell`是两个不相交closed union；operational activity只使用独立generation/cursor，不进入history checkpoint/page。
30. `durable_feed_kind`从Foundation cursor与Protocol request物理删除；unified root是history ordering与paging的唯一authority。
31. Canonical transcript placement只消费reducer-owned stable spine coordinate、transition proof与anchor tombstone；replacement/retirement继承原位置，audit只能以proved `before_leaf | after_leaf | ledger_sequence` anchor合并。Placement key由唯一registered fixed-width contract编码，并在tree/root/cursor/wire/historical decoder间exact rebind。
32. Checkpoint FULL通过`PresentationHistoryRootAdvancedFact`向client安装new latest cursor pair；old-root cursors仅作为retained pinned history继续有效。
33. Checkpoint candidate跨retry byte-identical，physical guard可换代；FULL/NONE/UNKNOWN/CONFLICT、compatible winner与reopen reconciliation均有唯一状态出口。
34. History persistent tree只按stable placement key寻址；continuous history ordinal被物理删除，display rank只在root/active-head ranked view中派生。
35. Active tail以逐EventLog sequence segment tuple为可切分authority；checkpoint cut、durable source-prefix recurrence与segment suffix retention形成单一swap协议。Noop-only concurrent tail仍有独立carrier，checkpoint I/O期间新增tail既不丢失也不重复，rewrite/retirement不能伪装append suffix。
36. Root resident transition三个branch均为完整closed DTO并与Python Protocol逐字段映射；不存在标签-only或自由payload实现。Go映射属于deferred `TUI-BT-*`验收。
37. Tree soft rotation threshold通过central growth quote/reservation为ordinary growth建立typed session fence；ordinary projected count只计算confirmed、tail、active remaining reservations与requested quote，不重复计算terminalization maintenance reserve。Hard exhaustion只能新建session或privileged repair，不截断历史。
38. Stored-event pair/receipt逐项证明owned event canonical payload与raw envelope bytes完全相同；mutable owned event在fold/tap前被修改会fail closed，不能与raw presentation feed分叉。
39. Foundation checkpoint owner在NONE/UNKNOWN/CONFLICT及waiter cancellation后保留stable candidate并live retry；Host close使用共享deadline等待logical owner和executor physical operation，超时明确blocked。
40. Tap GAP/overflow通过monotonic durable observed high-water和canonical raw range重新bootstrap；正常burst按frozen pending batch checkpoint，不存在alive worker永久失去subscriber的状态。
41. Gateway snapshot是resident O(1)读取；history/root artifact读取只经session-owned bounded async I/O，caller cancellation不遗失physical operation owner。
42. MCP FORM_RESPONSE sealed handle exact绑定request key、interaction batch owner/generation/round/request-set、controller/attachment generation、owner epoch与TTL；owner变化或expiry使未来consume fail closed并释放plaintext buffer。
43. History root/tree所有durable artifact读取都消费同一调用级absolute deadline；page与checkpoint path-copy均有deadline传播回归，PostgreSQL阻塞read不依赖测试手工释放即可由statement timeout真实退出。
