# Pulsara Context Evidence Cursor 性能优化实施规格

> 状态：EC0–EC3 已完成；EC4 benchmark、live diagnostics 与最终性能验收待实施
>
> 日期：2026-07-17
>
> 前置：`PULSARA_AUTHORITY_MATERIALIZATION_AND_LOSSLESS_TRANSCRIPT_PROJECTION_DESIGN.zh.md`
>
> 性能计划：`PULSARA_POST_STAGE4_RUNTIME_PERFORMANCE_OPTIMIZATION_PLAN.zh.md`
>
> 基线：`benchmarks/durable-runtime/baselines/v1/context-suite-7e9a484d/`
>
> 后续：Stage 5 `ContextSource Ownership Hard Cut`

---

## 实施进度（2026-07-17）

本轮已完成 EC0–EC3 的 production hard cut：

- 新增 process-local `VerifiedTranscriptProjectionCursorSnapshot`、唯一 factory/use token、persistent immutable envelope chunks；
- Run seed 与 checkpoint anchor 均绑定完整 durable carrier identity、availability sequence 与 generation；
- RuntimeSession startup exact restore 会尝试构造并 resident-admit Cursor；
- same-high-water 直接消费 Cursor 与同一时刻冻结的 reducer/document view，不执行 EventLog evidence read；
- forward growth 只读取 `(cursor_H, requested_H]`，只 historical-decode 新 semantic delta；
- empty semantic suffix 只推进 continuity/high-water，不伪造 transcript facts；
- 一次 prepare 复用同一个 absolute deadline，取消与 deadline 不发布 partial candidate；
- process-wide resident manager 按 Python chunk 对象身份证明真实结构共享并计费；内容相同但物理独立的 chunk 必须分别收费；
- resident admission 使用 `PREPARED -> CALLBACKS -> COMMITTED` phase/CAS，所有 eviction callback 均在 manager locks 外执行，anchor lock 内禁止调用 manager API；
- exact-handle lease 覆盖 token validation、delta read、candidate composition、installation/fallback 决议的完整逻辑使用期；
- frozen-base restore 可携带 exact run/checkpoint anchor identity，按 durable carrier event ID 重建并校验 stable identity；
- materialization equivalence 对完整 projection base/proof/source 执行 strict equality，仅允许 stable entry/document carrier 采用 versioned materialization equivalence；
- frozen terminal document view 保存 canonical sorted lookup index，resolver 使用二分查找；
- run-seed/checkpoint adoption、close 与 LRU eviction 均只删除 process-local memoization，不改变 reducer 或 durable authority；
- Cursor 类型由 architecture guard 禁止进入 event、primitives、serialization 与 PostgreSQL schema。

本轮没有新增或修改 durable event/DTO、EventLog schema与数据库 migration。EC1 的 shadow-only 双路径在 EC2 hard cut 后已删除，production 只保留 Cursor fast path与 canonical exact fallback。

验证证据：

```text
Cursor 定向：10 passed
Authority / Context contracts：141 passed
Agent / Host lifecycle：149 passed
PostgreSQL schema / driver：8 passed
全量非 real-LLM：2206 passed, 77 skipped
```

EC4 仍负责正式 context benchmark、三条 real long-horizon dogfood、Host/Runtime bounded live diagnostics、完整 metrics 接线与最终 architecture/performance gate；上述项目不计入本轮完成范围。

---

## 0. 结论

本规格实现 Stage 4.5 的 `PERF2A Verified Transcript Projection Cursor`。

当前每次 context compile 都会从 active run seed 或 transcript checkpoint base 重新读取到本次 ledger high-water 的全部
transcript-domain semantic events。`TranscriptProjectionStateStore` 已经跟随 committed event 增量推进，但 compiler 为证明 process-local
state 与 canonical EventLog 一致，仍重复执行：

```text
base -> H1
base -> H2
base -> H3
...
```

Context Evidence Cursor 将这条路径改为：

```text
exact restore(base -> H1)
        |
        v
verified cursor at H1
        + read(H1, H2] -> verified cursor at H2
        + read(H2, H3] -> verified cursor at H3
```

冻结结论：

1. EventLog 仍是唯一 durable authority；
2. `TranscriptProjectionStateStore` 仍是唯一 live transcript reducer；
3. Cursor 只保存已经由 EventLog sparse proof 与 live reducer 同 high-water 验证过的 evidence；
4. Cursor 是 process-local、可丢弃、不可恢复的性能 memoization；
5. Cursor 不新增 durable event、artifact、数据库表、migration 或 fingerprint schema；
6. Cursor 命中与同一 frozen base 的 exact restore必须生成严格相同的proof/delta，以及materialization-equivalent的stable content；
7. V1 只承诺数据库 range read 与 historical event decode 接近 `O(new delta)`；
8. V1 不声称最终 authority DTO、完整 refs fingerprint 或 manifest serialization 已经是 `O(new delta)`；
9. Cursor 位于ContextSource candidate ingress之下；纯source/candidate改造不改变其设计，event registry变化则要求DB reset或独立migration，
   不能由Cursor exact restore跨越；
10. 任意 cursor mismatch 都只能丢弃加速状态并进入现有 exact restore，不能在线修补 canonical facts。
11. 全进程共享有界resident budget；admission rejection或LRU eviction只移除memoization，不影响canonical执行。

本规格不是 transcript schema hard cut，也不是新的 cache authority。

---

## 1. 基线与问题

### 1.1 正式 Context baseline

当前正式离线 context suite 共执行：

```text
6 scenarios
14 modes/cases
3 warmup + 20 measured per production case
340 total trajectories
300 measured trajectories
0 correctness failures
```

主要结果：

| 场景 | cold/no-cache | steady-state | 改善 |
|---|---:|---:|---:|
| artifact-heavy tool history | 13.846s | 12.474s | 9.9% |
| incremental active window | 35.212s | 24.698s | 29.9% |
| long plan prefix growth | 21.311s | 14.551s | 31.7% |
| compaction source | 2.642s | 1.979s | 25.1% |
| subagent parent authority | 11.190s | 8.070s | 27.9% |

`checkpoint-preferred-hit` 与 `checkpoint-cold/no-cache` 接近：

```text
preferred hit     4.117s
cold/no-cache     4.108s
missing/rebase    7.079s
```

这说明当前成本不主要来自“是否找到 checkpoint”，而来自 checkpoint 之后每次 compile 仍重新读取和证明相同 historical prefix。

### 1.2 Real Long Plan profile

真实 Long Plan dogfood 中：

```text
model calls                              19
prepare_live_context_snapshot total   32.78s
context I/O union                     28.70s
transcript-projection-evidence-read   18.99s
pure compiler                          < 0.3s
```

`transcript-projection-evidence-read` 是 context preparation 的最大单项。它不是 provider API 等待，也不是 LLM compaction，而是本地
PostgreSQL sparse read、canonical envelope 构造、historical decode 与 accumulator 复核。

### 1.3 当前代码真值

当前生产入口：

```text
prepare_live_context_snapshot()
    -> TranscriptProjectionCheckpointService.prepare_projection_evidence(H)
        -> EventLog.read_transcript_domain_delta(base, H]
        -> materialize_transcript_sparse_read_proof(...)
        -> TranscriptProjectionStateStore.snapshot()
        -> TranscriptProjectionStateStore.stable_entries()
        -> build PreparedTranscriptProjectionEvidence
```

主要落点：

- `src/pulsara_agent/runtime/authority_materialization/checkpoint_service.py`
- `src/pulsara_agent/runtime/authority_materialization/transcript_reducer.py`
- `src/pulsara_agent/runtime/authority_materialization/transcript_restore.py`
- `src/pulsara_agent/runtime/authority_materialization/contracts.py`
- `src/pulsara_agent/event_log/protocol.py`
- `src/pulsara_agent/event_log/postgres.py`
- `src/pulsara_agent/runtime/context_input/transcript_authority.py`

PostgreSQL 已经维护：

```text
transcript semantic prefix count
transcript semantic accumulator
ledger continuity accumulator
per-event schema identity
canonical payload fingerprint
canonical envelope fingerprint
```

因此不需要新增 durable proof。缺少的是一个 process-local owner，用于记住“这个 base 到这个 high-water 已经被证明过”。

---

## 2. 范围

### 2.1 本阶段必须完成

本阶段只做：

1. 新增 process-local `VerifiedTranscriptProjectionCursor`；
2. 新增 immutable chunked semantic envelope vector；
3. 新增 reducer 的单锁 evidence snapshot seam；
4. 从 RuntimeSession startup exact restore 初始化 Cursor；
5. 实现 same-high-water fast path；
6. 实现 `(cursor_H, requested_H]` 增量 sparse read；
7. 实现 anchor generation CAS、失效与 exact restore fallback；
8. 将 referenced terminal projection documents 冻结为 exact immutable read view；
9. 增加 process-local、secret-safe timing/counter metrics；
10. 将 context benchmark 扩展为可验收 Cursor hit、delta read bytes 与 fallback reason；
11. 删除 normal compile 中从 active base 重读完整 transcript semantic prefix 的生产路径；
12. 新增全进程共享的resident bytes/chunks/cursors budget、lease与LRU eviction；
13. 冻结首次deep validation、normal O(1) fast validation和单次必要prefix traversal；
14. 保留 repair/doctor/restart 的 bounded exact restore。

### 2.2 明确不做

本阶段不做：

- 不改变 `ContextTranscriptAuthorityFact` schema；
- 不改变 `TranscriptDomainSparseReadProofFact` schema；
- 不改变 Context Input Manifest schema或 fingerprint；
- 不新增 durable cursor event、cursor artifact 或 cursor database row；
- 不让 Cursor 成为 committed reducer；
- 不复制一份独立 `TranscriptProjectionStateStore`；
- 不改变 terminal projection document 或 checkpoint tree schema；
- 不缓存 ContextSource collector、candidate set、source registry 或 provider payload；
- 不实现 Stage 5 ContextSource ownership；
- 不实现 prepared full context reuse；
- 不实现 Merkle range proof 或 composable manifest fingerprint；
- 不删除 exact restore；
- 不为旧数据库保留兼容路径；
- 不以放宽 physical bound 换取性能。

### 2.3 与 PERF2B/PERF3 的分界

本规格只优化 transcript evidence prefix。

后续能力归属：

```text
verified terminal/artifact hydration cache     -> PERF2B
prepared context/compiler result reuse         -> PERF3
candidate/source registry ownership            -> Stage 5
durable Merkle/range proof schema               -> future hard cut
```

---

## 3. 唯一 authority 与 owner

### 3.1 Authority hierarchy

冻结唯一层级：

```text
Canonical EventLog
    |
    +--> PostgreSQL transcript prefix facts
    |
    +--> exact sparse transcript-domain delta
            |
            v
TranscriptProjectionStateStore
            |
            v
PreparedTranscriptProjectionEvidence
            |
            v
ContextFactSnapshot / Manifest / ProviderInputPlan
```

Cursor 只能位于：

```text
exact sparse delta + verified reducer snapshot
            |
            v
process-local memoization
```

不能位于 EventLog 之前，也不能跳过 reducer join。

### 3.2 唯一 reducer

`TranscriptProjectionStateStore` 继续拥有：

- stable transcript entries；
- pending model projection；
- pending control disposition；
- pending tool pair/result；
- MCP suspension attribution；
- external requirement；
- semantic event count/accumulator；
- ledger through sequence/continuity；
- checkpointable state。

Cursor 不得：

- fold `AgentEvent`；
-维护 pending assembly；
-自行生成 normalized transcript；
-判断 control disposition；
-判断 tool pairing；
-注册为 RuntimeSession committed reducer。

Cursor 只保存 live reducer 已经验证并导出的 immutable evidence。

### 3.3 Owner

每个ledger的anchor/Cursor publication owner是：

```text
TranscriptProjectionCheckpointService
```

理由：

- 该 service 已拥有 active run seed/checkpoint anchor；
- 它已拥有 exact restore fallback；
- 它已参与 checkpoint adoption 与 rebase；
- 它已通过 `ContextInputIoService` 执行 bounded PostgreSQL read；
- RuntimeSession close 已经 drain 相关 context physical I/O。

不得在：

- compiler；
- HostSession；
- AgentRuntime；
- ContextSource；
- benchmark adapter

各自创建 cursor。

全进程resident admission/lease/eviction owner则唯一是：

```text
CursorResidentBudgetManager process singleton
```

Checkpoint service只能保存manager签发的exact handle，不能自行决定常驻内存上限；resident manager也不能读取或修改transcript reducer/durable
facts。两者通过prepare admission、anchor CAS、commit/abort与exact-handle eviction callback协作。

---

## 4. 核心不变量

### 4.1 Authority invariant

```text
cursor != durable authority
cursor != replay input
cursor != reducer state
```

删除 Cursor 后，同一 ledger 必须能够通过现有 exact restore 生成相同 evidence。

### 4.2 Base identity invariant

Cursor 只能绑定一个 exact projection base：

```text
runtime_session_id
anchor kind
anchor carrier stable event identity
anchor_available_from_sequence
run-seed semantic/reference identity
anchor stable state semantic fingerprint
base_ledger_through_sequence
base_ledger_continuity_accumulator
checkpoint candidate/materialization identity when checkpoint-based
event_domain_registry_contract_fingerprint
transcript_projection_reducer_contract_fingerprint
```

任一字段变化都必须 invalidate Cursor。

`CheckpointProjectionBaseFact.fact_fingerprint` 不能直接作为 stable base identity，因为其 acceleration fact包含本次
`delta_through_sequence`、delta event/byte count与ledger through sequence，会随每次compile的requested high-water变化。Cursor必须从
`_RunSeedProjectionAnchor | _CheckpointProjectionAnchor` 的不变字段派生anchor identity；本次完整`projection_base`作为Cursor evidence payload
单独保存。

Base覆盖的ledger prefix与anchor何时成为durable可用事实是两个不同序列：

```text
base_ledger_through_sequence       # seed/checkpoint materializes through here
anchor_available_from_sequence     # carrier event commits here
```

任意Cursor path必须满足：

```text
anchor_available_from_sequence <= requested_through_sequence
```

不得仅因`base_ledger_through_sequence <= requested H`就使用该anchor。

不得只按：

```text
run_id
window_id
checkpoint_id
```

定址。

### 4.3 Prefix invariant

对 Cursor at `H`：

```text
cursor.delta.before == projection base prefix
cursor.delta.after.through_sequence == H
cursor.delta.after.semantic_event_count == reducer_snapshot.transcript_semantic_event_count
cursor.delta.after.semantic_accumulator == reducer_snapshot.transcript_semantic_accumulator
cursor.delta.after.ledger_continuity_accumulator == reducer_snapshot.ledger_continuity_accumulator
cursor.semantic_source.resulting_state_fingerprint
    == reducer_snapshot.stable_semantic_state.state_semantic_fingerprint
```

### 4.4 Evidence equivalence invariant

对同一 frozen anchor/base，Cursor path与`restore_transcript_projection_from_base()`必须严格相等：

```text
projection_base
semantic_source
domain_completeness_proof
semantic_delta_events
```

以下物理carrier允许不同，但必须通过versioned materialization-equivalence contract：

```text
stable_entries
hydrated_message_contents
document view membership beyond required stable refs
inline normalized message vs artifact-backed normalized message reference
```

Materialization equivalence必须hydrate当前stable entries真正引用的内容，再比较：

```text
ordered leaf semantic identities
ordered provider-visible normalized blocks
tool pair identity/order
tool terminal projection semantic identity
message attribution/source refs required by canonical authority
final normalized transcript fingerprint
```

并进一步要求：

```text
ContextTranscriptAuthorityFact equal
Context Input Manifest fingerprint equal
CompiledContext semantic fingerprint equal
ProviderInputPlan equal
```

### 4.5 Append-only invariant

```text
new_delta.before == cursor.delta.after
new semantic event sequence > previous semantic event sequence
new semantic event order is canonical ledger order
no duplicate event ID/sequence/envelope fingerprint
```

非 transcript semantic events只推进 prefix through/continuity，不追加到 semantic envelope chunks。

### 4.6 High-water invariant

Fast path只能消费 exact high-water：

```text
reducer_evidence_snapshot.ledger_through_sequence == requested_through_sequence
```

若 live store 已经领先 requested high-water，不允许用较新的 stable entries 回答较旧请求。必须使用 one-shot exact restore。

### 4.7 Failure invariant

Cursor mismatch：

```text
invalidate cursor
record operational diagnostic
run existing exact restore
```

只有 exact restore 也无法证明 canonical ledger 时，才传播现有 contract mismatch、ledger untrusted 或 reconciliation required。

Cursor 自身损坏不新增 durable latch。

Durable event/schema registry mismatch不是Cursor-local failure。它必须继续由canonical restore拒绝；开发期Stage 5 hard cut通过关闭旧session并reset
PostgreSQL建立新registry ledger。

### 4.8 Resident budget invariant

```text
sum(physically distinct admitted chunk object charges + admitted cursor metadata charges)
    <= process max_resident_charge_bytes
admitted physically distinct chunk object count <= process max_resident_chunks
admitted cursor count <= process max_resident_cursors
```

只有同一个 immutable chunk Python 对象被 persistent append 路径真实复用时才允许去重。`chunk_fingerprint`只证明值相同，不证明物理共享；exact
rebuild 产生的值相同、对象不同的 chunks 必须分别收费。Pending admission reservation和borrowed retired handle都计入budget，直到abort或最后一个lease释放。Eviction/admission rejection不改变任何durable、
reducer或anchor semantic identity。

### 4.9 Validation cost invariant

```text
factory build / explicit deep audit    -> O(active semantic prefix)
normal same-H use validation           -> O(1)
delta extension validation             -> O(new chunks)
required V1 ID fingerprint + refs      -> one O(active prefix) traversal
```

不得以“防cache corruption”为由在每次命中重复深验全部old chunks；private construction guard与immutable persistent roots承担normal-path trust boundary。

---

## 5. Process-local DTO

本节 DTO 均为 process-local Python dataclass，不是 Pydantic durable fact，不进入 event serialization registry。

### 5.1 `TranscriptProjectionCursorBaseIdentity`

```python
@dataclass(frozen=True, slots=True)
class CursorAnchorCarrierIdentity:
    stable_event_identity: StableEventIdentityFact
    committed_sequence: int
    carrier_kind: Literal["run_start", "transcript_checkpoint_committed"]


@dataclass(frozen=True, slots=True)
class RunSeedCursorBaseIdentity:
    runtime_session_id: str
    base_kind: Literal["run_seed"]
    anchor_carrier: CursorAnchorCarrierIdentity
    anchor_available_from_sequence: int
    run_seed_semantic_fingerprint: str
    run_seed_reference_fingerprint: str
    stable_state_semantic_fingerprint: str
    base_ledger_through_sequence: int
    base_ledger_continuity_accumulator: str
    canonical_base_prefix_fingerprint: str
    event_domain_registry_contract_fingerprint: str
    reducer_contract_fingerprint: str
    identity_fingerprint: str


@dataclass(frozen=True, slots=True)
class CheckpointCursorBaseIdentity:
    runtime_session_id: str
    base_kind: Literal["checkpoint"]
    anchor_carrier: CursorAnchorCarrierIdentity
    anchor_available_from_sequence: int
    run_seed_semantic_fingerprint: str
    run_seed_reference_fingerprint: str
    stable_state_semantic_fingerprint: str
    checkpoint_id: str
    checkpoint_committed_event_id: str
    checkpoint_committed_event_sequence: int
    checkpoint_candidate_fingerprint: str
    checkpoint_candidate_ledger_through_sequence: int
    checkpoint_candidate_ledger_continuity_accumulator: str
    checkpoint_materialization_fingerprint: str
    previous_checkpoint_id: str | None
    ledger_materialization_generation: int
    consumer_horizon_revision: int
    checkpoint_build_contract_fingerprint: str
    base_ledger_through_sequence: int
    base_ledger_continuity_accumulator: str
    canonical_base_prefix_fingerprint: str
    event_domain_registry_contract_fingerprint: str
    reducer_contract_fingerprint: str
    identity_fingerprint: str


TranscriptProjectionCursorBaseIdentity = (
    RunSeedCursorBaseIdentity | CheckpointCursorBaseIdentity
)
```

规则：

- production Cursor只允许 `run_seed | checkpoint`；
- `anchor_available_from_sequence == anchor_carrier.committed_sequence`；
- run seed carrier必须是完整committed `RunStartEvent` stable identity；
- checkpoint carrier必须是完整committed `TranscriptProjectionCheckpointCommittedEvent` stable identity；
- checkpoint carrier event ID/sequence必须等于分支内重复的committed ID/sequence；
- checkpoint candidate fingerprint必须等于committed event与checkpoint candidate双方保存的fingerprint；
- `base_ledger_through_sequence` 对run seed等于`source_ledger_through_sequence`；
- `base_ledger_through_sequence` 对checkpoint等于`checkpoint_candidate_ledger_through_sequence`；
- checkpoint分支的base continuity必须等于candidate continuity；
- `canonical_base_prefix_fingerprint`只能从startup/frozen-base exact EventLog read的`delta.before`重算；
- base prefix的sequence/continuity/semantic count/accumulator必须与seed/checkpoint stable semantic source逐字段join；
- `checkpoint_materialization_fingerprint`来自root/materialization fact，不包含本次delta字段；
- `identity_fingerprint` 使用 domain-separated `context_fingerprint`；
- 不包含 ContextSource、candidate、provider、model target 或 invocation timing identity。

`CursorAnchorCarrierIdentity`不保存第二个`carrier_fingerprint`。它的完整身份就是：

```text
stable_event_identity
+ committed_sequence
+ carrier_kind
```

外层run-seed/checkpoint base分别使用：

```text
context_fingerprint("run-seed-cursor-base-identity:v1", payload_without_identity_fingerprint)
context_fingerprint("checkpoint-cursor-base-identity:v1", payload_without_identity_fingerprint)
```

重算自己的唯一`identity_fingerprint`。不得为nested carrier再引入一个可漂移的自报hash。

`StableEventIdentityFact`本身不包含sequence，因此`CursorAnchorCarrierIdentity.committed_sequence`是required字段，不能从event ID或payload
推断。

现有process-local anchor DTO同步hard cut：

```python
@dataclass(frozen=True, slots=True)
class _RunSeedProjectionAnchor:
    anchor_kind: Literal["run_seed"]
    carrier: CursorAnchorCarrierIdentity
    seed_semantic: RunTranscriptSeedSemanticFact
    seed_reference: RunTranscriptSeedReferenceFact


@dataclass(frozen=True, slots=True)
class _CheckpointProjectionAnchor:
    anchor_kind: Literal["checkpoint"]
    carrier: CursorAnchorCarrierIdentity
    checkpoint_candidate_fingerprint: str
    # existing seed/state/scope/materialization/generation fields remain required
    ...
```

删除只接收`ProjectionBaseFact`的`_anchor_from_projection_base(base)`。替换为：

```python
_anchor_from_restored_base(
    base,
    *,
    carrier_event,
    checkpoint_candidate_fingerprint,
)
```

缺carrier时不得构造production anchor。

### 5.2 `VerifiedTranscriptSemanticEnvelopeChunk`

```python
@dataclass(frozen=True, slots=True)
class VerifiedTranscriptSemanticEnvelopeChunk:
    first_sequence: int
    last_sequence: int
    event_count: int
    canonical_payload_bytes: int
    envelopes: tuple[RawStoredEventEnvelope, ...]
    chunk_fingerprint: str
```

V1 保存完整 `RawStoredEventEnvelope`，而不是只保存 event ID。原因：

1. 当前 `PreparedTranscriptProjectionEvidence` 以 canonical envelope 为输入；
2. `ContextTranscriptAuthorityFact` 需要完整 event reference identity；
3. current proof/fingerprint 覆盖 envelope identity；
4. 保留 envelope 可避免为性能优化引入 durable schema hard cut；
5. active base 到 high-water 的 envelope 数和 payload bytes 已受 Stage 4 physical bound 约束。

Chunk 约束：

```text
cursor_max_chunk_events
    = min(256, AuthorityMaterializationLimits.max_unreclaimable_ledger_events)
cursor_max_chunk_payload_bytes
    = AuthorityMaterializationLimits.max_unreclaimable_charged_payload_bytes

1 <= event_count <= cursor_max_chunk_events
canonical_payload_bytes <= cursor_max_chunk_payload_bytes
sequences strictly increasing within chunk
chunk ranges strictly increasing across vector
```

分割算法按canonical event order贪心填充：加入下一 envelope将超过event或byte cap时开启新chunk；单个envelope自身超过byte cap时复用现有
physical-bound错误，不创建oversized chunk。空 delta 使用空 chunk vector，不创建伪 chunk。

整个vector继续受active base的`max_unreclaimable_*`上限约束，因此chunk count也被semantic event count物理有界；不新增独立于Stage 4
materialization contract的第二容量窗口。

### 5.3 `PersistentTranscriptSemanticEnvelopeVector`

```python
@dataclass(frozen=True, slots=True)
class PersistentTranscriptSemanticEnvelopeVector:
    chunks: tuple[VerifiedTranscriptSemanticEnvelopeChunk, ...]
    event_count: int
    canonical_payload_bytes: int
    first_sequence: int | None
    last_sequence: int | None
    vector_fingerprint: str

    def _append_validated_delta(
        self,
        *,
        previous: ValidatedCursorUseToken,
        delta: RawTranscriptDomainDeltaSnapshot,
    ) -> PersistentTranscriptSemanticEnvelopeVector: ...

    def materialize(self) -> tuple[RawStoredEventEnvelope, ...]: ...
```

`_append_validated_delta()`只创建：

- 新 delta 的 chunks；
- 新 outer tuple；
- 新 vector metadata。

它不得复制旧 envelope payload。

Production调用权只属于`ValidatedCursorSnapshotFactory.build_from_validated_previous()`；helper必须验证`previous.cursor.semantic_envelopes is self`
与token guard，不能让caller对任意vector直接append自报delta。

`materialize()`在构造当前兼容 DTO 时允许 `O(active prefix)` flatten。V1 不隐藏这项成本。

Fingerprint算法冻结为persistent root：

```text
chunk_fingerprint
    = context_fingerprint(
          "verified-transcript-semantic-envelope-chunk:v1",
          first/last/count/bytes + ordered envelope_fingerprints,
      )

empty_vector_fingerprint
    = context_fingerprint("persistent-transcript-semantic-envelope-vector-empty:v1", {})

next_vector_fingerprint
    = context_fingerprint(
          "persistent-transcript-semantic-envelope-vector-append:v1",
          previous_vector_fingerprint
          + ordered new chunk_fingerprints
          + next aggregate count/bytes/first/last,
      )
```

Initial exact restore也必须从empty root按相同chunk order fold，不能使用另一套“hash完整tuple”算法。Factory首次deep validation重算整条root chain；
normal append只验证new chunks并从authenticated previous root推进。

### 5.4 `TranscriptProjectionReducerEvidenceSnapshot`

在 `TranscriptProjectionStateStore` 新增单锁导出：

```python
@dataclass(frozen=True, slots=True)
class TranscriptProjectionReducerEvidenceSnapshot:
    live_state: TranscriptProjectionLiveAssemblyState
    stable_entries: tuple[TranscriptProjectionLeafEntryFact, ...]
    required_projection_references: tuple[TerminalProjectionReferenceFact, ...]
    snapshot_fingerprint: str
```

以及：

```python
def evidence_snapshot(self) -> TranscriptProjectionReducerEvidenceSnapshot:
    ...
```

该方法必须在同一个 `TranscriptProjectionStateStore._lock` 内冻结：

- live state；
- stable entries；
- stable entries消费的 required terminal projection refs。

Pending assembly继续由 live registry为后续 committed fold持有；它不属于本次 provider-visible stable evidence，也不得为了 Cursor snapshot被
复制进 document view。

不得继续由 caller 分别调用：

```text
snapshot()
stable_entries()
projection_references(...)
```

并假设它们属于同一 high-water。

`snapshot_fingerprint`的唯一算法为：

```python
context_fingerprint(
    "transcript-projection-reducer-evidence-snapshot:v1",
    {
        "live_assembly_fingerprint": live_state.assembly_fingerprint,
        "ordered_stable_entry_fact_fingerprints": tuple(
            entry.fact_fingerprint for entry in stable_entries
        ),
        "ordered_required_projection_reference_fingerprints": tuple(
            ref.reference_fingerprint for ref in required_projection_references
        ),
    },
)
```

`required_projection_references`按stable-entry traversal第一次出现的顺序去重；不得按caller map迭代顺序生成。该DTO只能由
`TranscriptProjectionStateStore.evidence_snapshot()`构造，caller不能自报fingerprint。

State store在导出stable entries/required refs的同一次既有traversal中收集上述fingerprints并计算snapshot；不得为fast Cursor validation再遍历一遍
stable entries。后续`validate_for_use()`只比较这个owner-produced snapshot fingerprint。

### 5.5 `VerifiedTranscriptProjectionDocumentView`

```python
@dataclass(frozen=True, slots=True)
class VerifiedTranscriptProjectionDocumentViewEntry:
    reference: TerminalProjectionReferenceFact
    document: TerminalProjectionDocumentFact


@dataclass(frozen=True, slots=True)
class VerifiedTranscriptProjectionDocumentView:
    entries: tuple[VerifiedTranscriptProjectionDocumentViewEntry, ...]
    view_fingerprint: str

    def resolve(
        self,
        reference: TerminalProjectionReferenceFact,
    ) -> TerminalProjectionDocumentFact: ...


class TranscriptProjectionDocumentResolver(Protocol):
    def resolve(
        self,
        reference: TerminalProjectionReferenceFact,
    ) -> TerminalProjectionDocumentFact: ...
```

在 `TranscriptProjectionDocumentRegistry` 新增：

```python
def freeze_references(
    self,
    references: tuple[TerminalProjectionReferenceFact, ...],
) -> VerifiedTranscriptProjectionDocumentView: ...
```

它必须：

- 在 registry 单锁内解析所有 refs；
- 对每个 ref 重新执行现有 document/reference join；
- defensive-copy为按`reference_fingerprint`排序且唯一的tuple；
- 从每个entry内容重算`view_fingerprint`；
- 返回递归immutable、exact-subset view，不接受caller提供的普通mutable dict；
- 拒绝 missing、conflict、重复但不同 payload；
- 不把 registry 中未被当前 evidence 引用的未来 document 带入 snapshot。

`PreparedTranscriptProjectionEvidence.document_registry`与`stable_transcript.project_stable_context_transcript(documents=...)` hard cut为
`TranscriptProjectionDocumentResolver`；live registry与frozen view都可实现，但production compile必须传frozen view。

只冻结stable entries真正引用的terminal projection documents。Pending assembly继续由live mutable registry拥有，不进入provider-visible evidence
view。

Entry不保存冗余`entry_fingerprint`。View的唯一fingerprint算法为：

```python
context_fingerprint(
    "verified-transcript-projection-document-view:v1",
    {
        "ordered_entries": tuple(
            (
                entry.reference.reference_fingerprint,
                entry.document.fact_fingerprint,
            )
            for entry in entries
        ),
    },
)
```

`entries`仍按`reference_fingerprint`排序且唯一；reference/document完整join先于view fingerprint计算。

### 5.6 `VerifiedTranscriptProjectionCursorSnapshot`

```python
@dataclass(frozen=True, slots=True)
class VerifiedTranscriptProjectionCursorSnapshot:
    generation: int
    base_identity: TranscriptProjectionCursorBaseIdentity
    # 本次H的完整projection base；checkpoint acceleration字段可随H变化。
    projection_base: TranscriptProjectionBaseFact
    verified_through_sequence: int
    delta_before: RawTranscriptDomainPrefixFact
    delta_after: RawTranscriptDomainPrefixFact
    semantic_envelopes: PersistentTranscriptSemanticEnvelopeVector
    semantic_source: TranscriptProjectionSemanticSourceFact
    domain_completeness_proof: TranscriptDomainSparseReadProofFact
    reducer_snapshot_fingerprint: str
    cursor_fingerprint: str
```

该class不开放普通constructor。唯一入口：

```python
class ValidatedCursorSnapshotFactory:
    @classmethod
    def build(
        cls,
        *,
        generation: int,
        anchor: ProjectionAnchor,
        anchor_carrier_event: RunStartEvent
            | TranscriptProjectionCheckpointCommittedEvent,
        projection_base: TranscriptProjectionBaseFact,
        base_prefix: RawTranscriptDomainPrefixFact,
        through_prefix: RawTranscriptDomainPrefixFact,
        semantic_envelopes: PersistentTranscriptSemanticEnvelopeVector,
        semantic_source: TranscriptProjectionSemanticSourceFact,
        domain_completeness_proof: TranscriptDomainSparseReadProofFact,
        reducer_snapshot: TranscriptProjectionReducerEvidenceSnapshot,
        event_domain_binding: TranscriptEventDomainRegistryBinding,
    ) -> VerifiedTranscriptProjectionCursorSnapshot: ...

    @classmethod
    def build_from_validated_previous(
        cls,
        *,
        previous: ValidatedCursorUseToken,
        new_delta: RawTranscriptDomainDeltaSnapshot,
        next_semantic_envelopes: PersistentTranscriptSemanticEnvelopeVector,
        next_semantic_source: TranscriptProjectionSemanticSourceFact,
        next_domain_completeness_proof: TranscriptDomainSparseReadProofFact,
        reducer_snapshot: TranscriptProjectionReducerEvidenceSnapshot,
    ) -> VerifiedTranscriptProjectionCursorSnapshot: ...

    @classmethod
    def validate_for_use(
        cls,
        cursor: VerifiedTranscriptProjectionCursorSnapshot,
        *,
        active_anchor: ProjectionAnchor,
        event_domain_binding: TranscriptEventDomainRegistryBinding,
        reducer_snapshot: TranscriptProjectionReducerEvidenceSnapshot,
    ) -> ValidatedCursorUseToken: ...

    @classmethod
    def deep_validate(
        cls,
        cursor: VerifiedTranscriptProjectionCursorSnapshot,
        *,
        active_anchor: ProjectionAnchor,
        event_domain_binding: TranscriptEventDomainRegistryBinding,
        reducer_snapshot: TranscriptProjectionReducerEvidenceSnapshot,
    ) -> None: ...


@dataclass(frozen=True, slots=True, init=False)
class ValidatedCursorUseToken:
    cursor: VerifiedTranscriptProjectionCursorSnapshot
    anchor_generation: int
    anchor_base_identity_fingerprint: str
    reducer_snapshot_fingerprint: str
    event_domain_registry_contract_fingerprint: str
    _factory_guard: object
```

所有字段使用private construction token或`init=False`，production code禁止直接构造、`dataclasses.replace()`或绕过factory。

`build()`与`deep_validate()`必须执行完整不可自证join：

1. 从carrier committed event重算`StableEventIdentityFact`并验证sequence；
2. 从anchor派生base sequence、continuity、semantic count/accumulator；
3. `base_prefix`的上述字段必须等于anchor派生值；
4. `base_prefix.ledger_payload_bytes`只能来自同一次canonical EventLog restore/read，不接受独立caller数值；
5. 重算base prefix fingerprint并要求等于base identity的`canonical_base_prefix_fingerprint`；
6. 重算每个chunk fingerprint、count、bytes、sequence order；
7. 重算vector fingerprint、count、bytes、first/last sequence；
8. vector中每个event必须位于`(base_prefix.through_sequence, through_prefix.through_sequence]`；
9. vector event IDs fingerprint必须等于proof `selected_event_ids_fingerprint`；
10. vector count必须等于proof `selected_transcript_semantic_event_count`；
11. proof `prefix_before == base_prefix`、`prefix_through == through_prefix`；
12. through prefix必须等于semantic source的count/accumulator；
13. semantic source resulting state必须等于reducer stable state；
14. reducer ledger through/continuity/count/accumulator必须等于through prefix；
15. current registry/reducer contract必须等于base与source；
16. `anchor_available_from_sequence <= verified_through_sequence`；
17. 最后重算cursor fingerprint。

`build()`成功时在Cursor内安装module-private construction guard。`validate_for_use()`是normal-path的`O(1)`校验，只允许检查：

1. private construction guard来自当前module factory；
2. constant-size outer `cursor_fingerprint`可由nested root fingerprints重算；
3. active anchor identity/generation与Cursor精确一致；
4. registry/reducer contract仍与current binding一致；
5. reducer snapshot high-water、continuity、semantic count/accumulator与Cursor `delta_after`一致；
6. Cursor保存的reducer snapshot fingerprint等于本次state-store-owned `evidence_snapshot()`返回值；normal caller不再次遍历entries/refs重算；
7. `anchor_available_from_sequence <= verified_through_sequence`。

它成功后返回module-private、不可直接构造的`ValidatedCursorUseToken`。Same-H与delta composition只能接受该token，不接受裸Cursor。

Token只在一次`prepare_projection_evidence()`调用内有效，不得放入service state；resident `CursorResidentLease`必须覆盖token validation、bounded
delta-read await、candidate composition、installation/fallback决议的全部逻辑使用期，发布前仍需anchor generation CAS。Factory在消费token时必须
重算token fingerprint，并验证token冻结的generation、anchor identity、reducer snapshot与registry binding；不得只检查private guard。

若caller取消后底层context I/O继续物理运行，worker closure不得继续捕获Cursor/token；它只允许持有已经冻结的scalar range/deadline。这样逻辑
Cursor使用期结束后可以释放lease，而底层I/O仍由session-owned physical operation owner负责drain，不形成未计费Cursor强引用。

`deep_validate()`重新遍历全部chunks/vector/proof，只允许用于：

- factory首次构造；
- startup/reopen seed；
- EC1 shadow comparison；
- explicit debug/doctor；
- corruption fault-injection tests。

Production same-H和delta extension不得每次重算旧chunk fingerprint。Delta extension只深验new chunks，并使用factory认证的persistent vector root组合
new root。V1必须保留的full event-ID fingerprint与authority refs在同一次`O(active prefix)` traversal中生成，禁止先为Cursor深验遍历一次、再为
authority/manifest遍历第二次。

`cursor_fingerprint`使用：

```python
context_fingerprint(
    "verified-transcript-projection-cursor:v1",
    {
        "generation": generation,
        "base_identity_fingerprint": base_identity.identity_fingerprint,
        "projection_base_fact_fingerprint": projection_base.fact_fingerprint,
        "verified_through_sequence": verified_through_sequence,
        "delta_before_prefix_fingerprint": raw_prefix_fingerprint(delta_before),
        "delta_after_prefix_fingerprint": raw_prefix_fingerprint(delta_after),
        "semantic_envelope_vector_fingerprint": semantic_envelopes.vector_fingerprint,
        "semantic_source_fingerprint": semantic_source.semantic_source_fingerprint,
        "domain_completeness_proof_fingerprint": (
            domain_completeness_proof.completeness_fingerprint
        ),
        "reducer_snapshot_fingerprint": reducer_snapshot_fingerprint,
    },
)
```

不得把Python object ID、resident handle、LRU时间或Cursor budget状态带入fingerprint。

`raw_prefix_fingerprint()`的唯一算法为：

```python
context_fingerprint(
    "raw-transcript-domain-prefix:v1",
    {
        "through_sequence": prefix.through_sequence,
        "ledger_payload_bytes": prefix.ledger_payload_bytes,
        "semantic_event_count": prefix.semantic_event_count,
        "semantic_accumulator": prefix.semantic_accumulator,
        "ledger_continuity_accumulator": (
            prefix.ledger_continuity_accumulator
        ),
    },
)
```

该helper只存在于`evidence_cursor.py`，所有base/cursor validation复用它。

Cursor 不保存：

- mutable document registry；
- ContextSource candidate；
- final normalized transcript；
- invocation timing projection；
- compiled sections；
- manifest bytes；
- provider payload。

### 5.7 `ProjectionEvidenceCursorOutcome`

```python
class ProjectionEvidenceCursorOutcome(StrEnum):
    SAME_HIGH_WATER_HIT = "same_high_water_hit"
    DELTA_EXTENSION = "delta_extension"
    SEEDED_FROM_STARTUP_RESTORE = "seeded_from_startup_restore"
    EXACT_RESTORE_CURSOR_ABSENT = "exact_restore_cursor_absent"
    EXACT_RESTORE_REQUESTED_BEHIND = "exact_restore_requested_behind"
    EXACT_RESTORE_LIVE_STORE_AHEAD = "exact_restore_live_store_ahead"
    EXACT_RESTORE_ANCHOR_CHANGED = "exact_restore_anchor_changed"
    EXACT_RESTORE_ANCHOR_NOT_YET_AVAILABLE = (
        "exact_restore_anchor_not_yet_available"
    )
    EXACT_RESTORE_ANCHOR_CARRIER_MISMATCH = (
        "exact_restore_anchor_carrier_mismatch"
    )
    EXACT_RESTORE_CHECKPOINT_CANDIDATE_MISMATCH = (
        "exact_restore_checkpoint_candidate_mismatch"
    )
    EXACT_RESTORE_PREFIX_MISMATCH = "exact_restore_prefix_mismatch"
    EXACT_RESTORE_MATERIALIZATION_MISMATCH = (
        "exact_restore_materialization_mismatch"
    )
    EXACT_RESTORE_CONTRACT_CHANGED = "exact_restore_contract_changed"
    EXACT_PATH_RESIDENT_ADMISSION_REJECTED = (
        "exact_path_resident_admission_rejected"
    )
```

该 enum 只进入 operational metrics，不进入 EventLog。

### 5.8 Process-owned resident budget DTO

Cursor保存完整canonical payload，因此单ledger的`65,536 events / 256 MiB` physical bound不能替代进程RAM治理；N个open/detached
RuntimeSession不能各自常驻到该上限。新增纯operational、process-owned：

```python
@dataclass(frozen=True, slots=True)
class CursorResidentBudgetLimits:
    max_resident_charge_bytes: int = 512 * 1024 * 1024
    max_resident_chunks: int = 4_096
    max_resident_cursors: int = 64


@dataclass(frozen=True, slots=True)
class CursorResidentCharge:
    payload_bytes: int
    identity_utf8_bytes: int
    envelope_object_reserve_bytes: int
    chunk_object_reserve_bytes: int
    cursor_object_reserve_bytes: int
    total_charge_bytes: int
    chunk_count: int


@dataclass(frozen=True, slots=True)
class CursorResidentHandle:
    resident_entry_id: str
    owner_runtime_session_id: str
    anchor_generation: int
    cursor: VerifiedTranscriptProjectionCursorSnapshot
    charge: CursorResidentCharge


class CursorResidentLease:
    handle: CursorResidentHandle

    def __enter__(self) -> VerifiedTranscriptProjectionCursorSnapshot: ...
    def __exit__(self, exc_type, exc, traceback) -> None: ...


@dataclass(frozen=True, slots=True)
class CursorResidentBudgetDiagnostic:
    max_resident_charge_bytes: int
    max_resident_chunks: int
    max_resident_cursors: int
    resident_charge_bytes: int
    resident_chunk_count: int
    resident_cursor_count: int
    pending_admission_count: int
    active_borrow_count: int
    admission_count: int
    admission_rejected_count: int
    eviction_count: int
    evicted_charge_bytes: int
```

V1 limits为进程固定默认值，不进入durable config、Cursor fingerprint或provider identity。测试可以依赖注入更小limits；production不得按session
各建一份manager。

Limits三个字段必须为positive integer。Charge各分项必须non-negative，`total_charge_bytes`必须严格等于五个byte分项之和，`chunk_count`必须等于
candidate vector chunk count；所有DTO由budget module factory构造，不接受caller自报charge。

`CursorResidentCharge`由唯一`estimate_cursor_resident_charge()`计算：

```text
payload_bytes
    = sum(len(envelope.canonical_payload_bytes)) over physically distinct resident chunk objects

identity_utf8_bytes
    = every RawStoredEventEnvelope string field's exact UTF-8 byte length

envelope_object_reserve_bytes
    = event_count * 1_024

chunk_object_reserve_bytes
    = chunk_count * 1_024

cursor_object_reserve_bytes
    = 64 * 1_024

total_charge_bytes
    = payload + identity + all fixed reserves
```

`sequence`整数与tuple/slot引用由fixed reserves覆盖。Composition-root doctor必须用当前支持的CPython版本和maximal legal envelope fixture验证
fixed reserves不低估实际owned object size；Python runtime或`RawStoredEventEnvelope`形状变化时必须先更新该fixture/contract。Charge只用于保守RAM
admission，不是可重放事实。

Composition-root doctor还必须证明：当前single-ledger maximal legal Cursor的estimated charge/chunk count分别不超过default process byte/chunk
limits。若默认值与physical contract变更后不再可行，启动配置检查失败；不得等到长任务中途才发现所有maximal Cursor均无法缓存。

Manager按chunk对象身份维护process-local unique chunk refcount。只有old/new Cursor的persistent vector确实引用同一chunk对象时，短暂重叠才不重复
收费。`chunk_fingerprint`不得作为物理共享身份：exact rebuild即使产生相同fingerprint，只要chunk对象不同就必须分别计费。V1不引入中央interner。

---

## 6. Cursor owner 与状态机

### 6.1 Owner state

`TranscriptProjectionCheckpointService` 新增：

```python
self._anchor_state_lock: threading.RLock
self._projection_anchor_generation: int
self._cursor_advance_lock: asyncio.Lock
self._verified_evidence_cursor_handle: CursorResidentHandle | None
self._cursor_resident_budget: CursorResidentBudgetManager
```

现有`asyncio.Lock`必须重命名为`_checkpoint_owner_lock`，只保护checkpoint owner/task lifecycle；它不再被称为anchor lock。

状态机：

```text
ABSENT
  -> VERIFIED(H)

VERIFIED(H)
  -> VERIFIED(H)                  same-high-water hit
  -> ADVANCING(H -> Hnew)
  -> INVALIDATED                  anchor/contract mismatch
  -> ABSENT                       resident LRU eviction

ADVANCING
  -> VERIFIED(Hnew)               CAS success
  -> INVALIDATED                  anchor changed
  -> VERIFIED(H)                  cancellation/deadline/read failure
  -> VERIFIED(H) or ABSENT        resident admission rejected

INVALIDATED
  -> ABSENT
  -> VERIFIED(H)                  exact restore success
```

### 6.2 不是 durable lifecycle

不新增：

```text
CursorStartedEvent
CursorAdvancedEvent
CursorFailedEvent
CursorRecoveredEvent
```

Cursor 丢失、进程退出或 cache eviction 不需要 recovery terminalization。

### 6.3 Process-owned resident budget owner

新增`runtime/authority_materialization/cursor_resident_budget.py`。它仿照process-owned blocking executor，只创建一个
`CursorResidentBudgetManager`，由所有open、detached和child RuntimeSession共享。

Manager在单一`threading.RLock`内维护：

```text
resident handles by exact entry ID
owner runtime-session attribution on each handle
physically shared chunk-object charge/refcount
active borrow count
monotonic LRU access counter
pending admission reservations
aggregate resident bytes/chunks/cursors
```

唯一API：

```python
@dataclass(frozen=True, slots=True)
class CursorResidentAdmissionReservation:
    reservation_id: str
    owner_runtime_session_id: str
    anchor_generation: int
    candidate: VerifiedTranscriptProjectionCursorSnapshot
    candidate_charge: CursorResidentCharge
    replaces_resident_entry_id: str | None
    provisional_handle: CursorResidentHandle


class CursorResidentBudgetManager:
    def prepare_admission(
        self,
        *,
        owner_runtime_session_id: str,
        anchor_generation: int,
        candidate: VerifiedTranscriptProjectionCursorSnapshot,
        replaces: CursorResidentHandle | None,
        eviction_callback: Callable[[str], bool],
    ) -> CursorResidentAdmissionReservation | None: ...

    def commit_admission(
        self,
        reservation: CursorResidentAdmissionReservation,
    ) -> None: ...

    def abort_admission(
        self,
        reservation: CursorResidentAdmissionReservation,
    ) -> None: ...

    def borrow(self, handle: CursorResidentHandle) -> CursorResidentLease | None: ...
    def retire(self, handle: CursorResidentHandle) -> None: ...
    def diagnostics(self) -> CursorResidentBudgetDiagnostic: ...


def process_cursor_resident_budget_manager() -> CursorResidentBudgetManager: ...
```

`prepare_admission()`返回`None`就是typed admission rejection；不抛资源不足异常。Reservation计入pending totals，防止并发admission共同越过limit。
`commit_admission()`和`abort_admission()`均按exact reservation ID与phase CAS处理，旧attempt不能删除新reservation。Candidate、charge、
replacement identity在prepare后不可变。`abort_admission()`只允许取消`PREPARED`；进入`CALLBACKS`后由commit owner唯一收口，防止callback期间错误
abort。

Successful admission与successful `borrow()`各自推进同一个monotonic access counter；failed borrow、diagnostics read和Inspector展示不得刷新LRU。

`process_cursor_resident_budget_manager()`在module lock内lazy-create唯一production singleton，形状与`blocking_executor.py`一致。仅tests可通过
private fixture reset；production composition root不得接受任意manager override。`RuntimeSession` constructor可以为unit test显式注入manager，但
默认与Host/child wiring必须取得同一process singleton。

Manager保存eviction callback时必须使用`weakref.WeakMethod`或等价weak owner token，不能因process singleton反向强引用已关闭RuntimeSession。Owner已
消失时，entry视为callback已成功detach；borrow归零后可直接释放resident charge。

Anchor CAS与`commit_admission()`组成同步、无await block；一旦CAS安装provisional handle，commit对同一valid reservation必须为no-fail内存转换。
CAS未安装时只能abort。`BaseException`不得留下“anchor持有provisional handle、manager却未计费”的状态；fault injection下若no-fail invariant自身
破坏，exact CAS清除provisional handle并拒绝Cursor，canonical evidence照常返回。

发布candidate的唯一顺序为：

```text
build + deep validate candidate
    -> estimate resident charge
    -> manager.prepare_admission(candidate, replaces_exact_handle)
    -> anchor generation/identity CAS installs reservation.provisional_handle
    -> manager.commit_admission(reservation)
    -> release/retire replaced exact handle
```

Anchor CAS失败必须`abort_admission()`；不得留下unreachable resident entry。替换旧handle时：

- old borrow count为0：admission projection可扣除old handle将释放的exclusive charge；
- old仍被borrow：old charge保留到最后一个lease释放，new candidate必须在剩余budget内独立admit；
- old/new persistent vectors引用同一immutable chunk对象时按object-identity refcount收费；值相同但对象不同不得去重；
- close/anchor replacement使用exact handle ID退休，不能误删新generation。

超限前先做deterministic eviction planning：

```text
eligible = borrow_count == 0 and not pending admission
order = (last_access_counter ascending, resident_charge descending, entry_id)
```

先虚拟计算候选victims是否足够；若不足，不执行部分淘汰，直接拒绝candidate。若足够，manager在自身锁内标记victims并将reservation CAS到
`CALLBACKS`，释放全部manager locks后调用各owner的同步exact-handle eviction callback，再回锁完成resident转换。Manager lock与anchor lock绝不
嵌套；checkpoint service也不得在anchor lock内调用`abort_admission()`、`retire()`、`borrow()`或其他manager API。

Eviction callback只允许在`_anchor_state_lock`内执行：

```text
if current cursor handle ID == victim ID:
    current cursor handle = None
```

它不得改变anchor、generation、reducer、reachable artifacts、latest checkpoint或active run attribution。

无法resident admission时：

- 当前请求继续返回已由canonical exact/delta path构造并验证的evidence；
- 不发布candidate Cursor；
- 旧Cursor若仍与active anchor兼容可以保留；
- outcome为`EXACT_PATH_RESIDENT_ADMISSION_REJECTED`；
- 不fail closed、不latch ledger、不触发checkpoint/LLM compaction。

这是一层可丢弃memoization RAM保护，不是模型上下文窗口，也不是新的durable physical admission。

### 6.4 Lock ownership

冻结四个锁域：

1. `TranscriptProjectionStateStore._lock`：同步、短持有、冻结 reducer evidence；
2. `_anchor_state_lock: threading.RLock`：同步、无await、线性化全部anchor state；
3. process-owned Cursor budget manager `RLock`：resident admission/lease/LRU；
4. `_checkpoint_owner_lock: asyncio.Lock`：checkpoint task/terminal owner lifecycle；
5. `_cursor_advance_lock: asyncio.Lock`：每个 RuntimeSession 最多一个 evidence advance/fallback。

`_anchor_state_lock`必须原子覆盖：

```text
projection_anchor_generation
projection_anchor including carrier identity
verified_evidence_cursor_handle
reachable_artifact_ids
latest_checkpoint_id
active_run_context
prepared_run_seed_artifacts ownership transfer
```

新增同步快照：

```python
@dataclass(frozen=True, slots=True)
class ProjectionAnchorStateSnapshot:
    generation: int
    anchor: ProjectionAnchor | None
    cursor_handle: CursorResidentHandle | None
    reachable_artifact_ids: frozenset[str]
    latest_checkpoint_id: str | None
    active_run_context: EventContext | None


def _snapshot_anchor_state(self) -> ProjectionAnchorStateSnapshot: ...
```

规则：

- 不在 state-store `RLock` 内 await；
- 不在 RuntimeSession event write lock 内执行 PostgreSQL evidence read；
- 不在 `_anchor_state_lock` 内执行 PostgreSQL/artifact I/O或获取async lock；
- Cursor advance 可以持有 `_cursor_advance_lock` 跨 await；
- committed reducer 与 event writer 从不获取 `_cursor_advance_lock`；
- checkpoint adoption 不等待当前 Cursor I/O 完成，只在`_anchor_state_lock`内推进generation并使其CAS失败；
- `_cursor_advance_lock`不能兼任anchor linearization；
- lock order固定为：任何代码不得在持有state-store lock或checkpoint-owner async lock时获取anchor state lock；budget manager lock与anchor
  lock绝不嵌套，跨owner动作使用prepare/CAS/commit或锁外exact-handle callback。

### 6.5 Anchor generation

以下操作必须在同一 anchor mutation block 内：

```text
increment projection_anchor_generation
install new run-seed/checkpoint anchor
set verified cursor handle = None and exact-retire old handle
update reachable artifact ownership
update latest checkpoint and active run attribution
```

适用：

- `adopt_committed_run_seed()`；
- `_adopt_committed_checkpoint()`；
- checkpoint rebase；
- active projection contract replacement；
- session reopen initialization。

`adopt_committed_run_seed()`必须比较carrier identity。即使seed semantic/reference逐字段相同，只要新`RunStartEvent`的event ID、payload identity或
committed sequence不同，也必须：

```text
generation += 1
install new carrier-bound run-seed anchor
invalidate cursor
atomically transfer prepared artifacts and active context
```

`_adopt_committed_checkpoint()`必须先在锁外准备新artifact set，然后在`_anchor_state_lock`内一次性交换reachable artifacts、latest checkpoint、
carrier-bound anchor、generation与Cursor。禁止继续在async lock外单独修改reachable/latest字段。

Cursor advance 发布前必须比较：

```text
captured_anchor_generation == current_anchor_generation
captured_base_identity == current_base_identity
```

失败时不得发布 stale Cursor。

---

## 7. Startup seed

### 7.1 RuntimeSession startup

RuntimeSession 当前先执行：

```text
restore_transcript_projection(... ledger high-water ...)
    -> RestoredTranscriptProjection
```

随后创建 `TranscriptProjectionCheckpointService`。

Checkpoint service constructor 必须尝试从 `RestoredTranscriptProjection` 构造 Cursor：

```text
restore.projection_base
restore.semantic_source
restore.domain_completeness_proof
restore.semantic_delta_events
restore.state_store.evidence_snapshot()
restore.anchor_carrier_event
```

`RestoredTranscriptProjection`新增required process-local `anchor_carrier_event`：

- run-seed base返回拥有该seed的committed `RunStartEvent`；
- checkpoint base返回对应`TranscriptProjectionCheckpointCommittedEvent`；
- empty/test genesis为`None`且不得seed Cursor。

Restore必须按exact ID/schema/payload读取并验证carrier，不得只从projection base中的event ID/sequence拼接stable identity。

只有全部join成立，且process resident manager admission与anchor CAS都成功，才发布：

```text
SEEDED_FROM_STARTUP_RESTORE
```

Resident admission被拒绝时startup仍成功、reducer/anchor保持可用，只是不安装Cursor；下一次prepare走exact path。

### 7.2 Startup seed invariant

必须验证：

```text
restore base == active service anchor
proof prefix through == restored store through
proof selected count == len(semantic_delta_events)
semantic source count/accumulator == reducer stable state
semantic envelope identities == proof selected IDs fingerprint input
all required terminal projection refs resolve
```

任一不成立：

- 不阻止 RuntimeSession open；
- Cursor 保持 `ABSENT`；
- 记录 bounded operational diagnostic；
- 首次 compile 使用 existing exact restore；
- exact restore 失败时沿用现有 fail-closed 语义。

### 7.3 Empty/test genesis

Production RunStart 必须已有 durable run seed。Cursor 不新增 seedless production path。

`empty | test_genesis` restore不发布Cursor；它们继续使用exact evidence test path。InMemory/test genesis只允许显式test composition
root构造，architecture guard禁止生产模块引用test factory。

---

## 8. Evidence 准备算法

### 8.1 总入口

保持现有 public API：

```python
async def prepare_projection_evidence(
    *,
    requested_through_sequence: int,
) -> PreparedTranscriptProjectionEvidence:
    ...
```

调用者不感知 Cursor。

总算法：

```text
serialize per-session cursor advance
capture active anchor + generation
inspect cursor
freeze exact reducer evidence snapshot
choose same-H / delta-extension / exact-restore
freeze exact document view
build current PreparedTranscriptProjectionEvidence
CAS publish cursor when eligible
return
```

### 8.2 Same-high-water fast path

前置：

```text
cursor exists
cursor base == active base
cursor.verified_through_sequence == requested H
cursor.base_identity.anchor_available_from_sequence <= requested H
reducer_snapshot.live_state.ledger_through_sequence == H
```

不执行 PostgreSQL read。

进入fast path前必须：

1. 从process budget manager取得exact-handle`CursorResidentLease`；
2. 调用`ValidatedCursorSnapshotFactory.validate_for_use()`取得`ValidatedCursorUseToken`；
3. 只从该token访问Cursor proof/vector。

Borrow或fast validation失败按cursor-local miss处理，不能继续使用原handle中的proof/vector。Normal same-H不得调用`deep_validate()`。

验证：

```text
cursor.delta_after semantic count/accumulator
    == reducer live state semantic count/accumulator
cursor.delta_after continuity
    == reducer live state continuity
cursor.semantic_source resulting state
    == reducer stable state
cursor proof through prefix
    == cursor.delta_after
```

随后：

1. 从 reducer evidence snapshot 取得 stable entries 与 exact refs；
2. 从 document registry 冻结 exact document view；
3. 复用 Cursor proof 与 semantic envelope vector；
4. materialize envelope tuple；
5. 构造 `PreparedTranscriptProjectionEvidence`；
6. 记录 `same_high_water_hit`。

因为 Cursor 只可能由 successful exact restore 或 successful delta extension 发布，且 ledger 是 append-only，所以 exact same-H 不需要每次重新查询
PostgreSQL prefix row。

### 8.3 Incremental delta extension

前置：

```text
cursor Hprev < requested Hnew
cursor base == active base
cursor.base_identity.anchor_available_from_sequence <= requested Hnew
reducer snapshot exactly at Hnew
```

读取new delta前先borrow exact resident handle并执行`validate_for_use()`；lease必须持续到delta read、proof/candidate composition、resident
installation与fallback决议全部结束。不得用一个没有factory construction guard、active-anchor join或exact reducer high-water join的Cursor作为
`delta.before` authority。Normal path不重新深验old chunks。

只读取：

```python
read_transcript_domain_delta(
    after_sequence=Hprev,
    through_sequence=Hnew,
    ...same bounded limits/deadline...
)
```

验证矩阵：

| 输入 | 必须等于 |
|---|---|
| `delta.before.through_sequence` | `Hprev` |
| `delta.before.semantic_event_count` | `cursor.delta_after.semantic_event_count` |
| `delta.before.semantic_accumulator` | `cursor.delta_after.semantic_accumulator` |
| `delta.before.ledger_continuity_accumulator` | `cursor.delta_after.ledger_continuity_accumulator` |
| `delta.after.through_sequence` | `Hnew` |
| `delta.after.semantic_event_count` | reducer snapshot count |
| `delta.after.semantic_accumulator` | reducer snapshot accumulator |
| `delta.after.ledger_continuity_accumulator` | reducer snapshot continuity |
| registry contract | cursor/base/current binding |

然后：

1. 将 `delta.semantic_events` 追加为新 immutable chunks；
2. 以 active base prefix + full chunk vector重建现有 `RawTranscriptDomainDeltaSnapshot` 兼容视图；
3. 使用新增的 verified proof composition helper生成逐字段相同 sparse proof；
4. 从 reducer stable state构造 semantic source；
5. 冻结 exact document view；
6. 构造 next Cursor candidate；
7. 向process resident manager申请replacement admission；
8. 重新取得 anchor generation并CAS安装`reservation.provisional_handle`；
9. commit/abort resident admission并exact-retire旧handle；
10. 返回 evidence。

Proof composition不得把完整旧 envelope tuple重新传入当前 `materialize_transcript_sparse_read_proof()`，因为该 helper会 decode全部输入事件。
新增：

```python
def compose_verified_transcript_sparse_read_proof(
    *,
    previous: ValidatedCursorUseToken,
    new_delta: RawTranscriptDomainDeltaSnapshot,
) -> TranscriptDomainSparseReadProofFact: ...
```

该 helper 必须：

1. 只接受factory签发、绑定exact active anchor/reducer snapshot/registry contract的`ValidatedCursorUseToken`；
2. 从token内取previous Cursor的base prefix、proof与persistent vector，禁止caller平行传入这些字段；
3. 精确比较previous proof through prefix与`new_delta.before`；
4. 只深验并historical-decode`new_delta.semantic_events`；
5. 从`new_delta.after`取得最终count/accumulator/continuity；
6. append只验证new chunks，并从previous authenticated vector root组合next vector root；
7. 使用完整ordered event ID view重算当前V1 `selected_event_ids_fingerprint`；
8. 该full-ID traversal同时生成最终authority refs，不允许另起一次old-prefix validation traversal；
9. 生成与“从base一次读取到Hnew”逐字段相同的proof；
10. 在无new semantic event时不decode旧事件；
11. 不接受caller自报的final accumulator；
12. 最终next Cursor仍只能由`ValidatedCursorSnapshotFactory.build_from_validated_previous()`构造。

`build_from_validated_previous()`验证new delta/chunks/proof/reducer joins，并复用token已经认证的old vector root；它不得退回对old chunks的完整深验。
Debug/shadow可以在next Cursor发布后额外调用`deep_validate()`，但该成本不进入production normal path。

因此：

```text
historical decode       O(new semantic delta)
event-ID fingerprint    O(active semantic prefix) in V1
```

这两个复杂度必须分别测量，不得合并宣称为端到端`O(new delta)`。

### 8.4 为什么不让 Cursor 自行 fold new delta

禁止算法：

```text
cursor stable state
    + decode new delta
    + private reducer
    -> next stable state
```

这会复制：

- pending assembly；
- terminal document registry；
- control disposition；
- tool pairing；
- RunEnd cleanup。

正确算法只将 new delta 的 durable prefix 与 canonical committed reducer 的 exact high-water snapshot做 join。Reducer 结果仍只有一个 owner。

### 8.5 Requested high-water behind live store

若：

```text
requested H < reducer store Hlive
```

V1 不实现 cursor rewind，也不保留任意历史 reducer snapshots。

处理：

```text
run one-shot restore_transcript_projection(requested H)
return exact evidence
do not replace a newer active cursor
```

这保持现有回归：

```text
test_projection_evidence_restores_requested_high_water_when_live_store_is_ahead
```

### 8.6 Cursor absent

若 Cursor absent 且 requested H 等于 live store H：

1. 使用现有 `restore_transcript_projection(requested H)`；
2. 验证 restore 与 active anchor兼容；
3. 重新冻结active reducer evidence snapshot；
4. 验证active reducer snapshot与restored stable state、count、accumulator、continuity完全一致；
5. 构造 evidence；
6. 若anchor generation未变且active reducer仍位于requested H，尝试resident admission并CAS发布Cursor；
7. 若CAS失败，只返回one-shot exact evidence并abort admission；
8. 若resident admission被拒绝，返回evidence与`exact_path_resident_admission_rejected`，不发布Cursor；
9. 其他成功路径返回`exact_restore_cursor_absent`。

不得从 live store 自报 state直接创建首个 Cursor。首个 Cursor 必须有 canonical EventLog proof。

### 8.7 Anchor changed during I/O

若 I/O 期间 run seed/checkpoint/rebase被采用：

```text
candidate base generation != current generation
```

则：

- candidate 不得发布；
- 已读取结果可直接丢弃；
- 重新读取current anchor carrier identity与`anchor_available_from_sequence`；
- 只有`new_anchor.anchor_available_from_sequence <= requested H`时才允许按new anchor重试一次；
- 若new carrier sequence晚于requested H，使用historical one-shot exact restore；
- 不允许用未来 checkpoint回答过去 high-water。

禁止使用`new_anchor.base_ledger_through_sequence <= requested H`替代carrier availability判断。Run seed可能materialize through H，但直到
`RunStart(H+1)`才成为durable；checkpoint candidate同样可能早于committed carrier多个sequence。

### 8.8 Empty semantic delta

`(Hprev, Hnew]` 可能只有 non-transcript events。

此时：

```text
delta.semantic_events == ()
semantic count/accumulator unchanged
ledger continuity and through advance
chunk vector unchanged
proof through prefix advances
```

仍可发布 Cursor at `Hnew`。

### 8.9 Physical bounds

每次增量 read 继续受：

```text
AuthorityMaterializationLimits.max_unreclaimable_ledger_events
AuthorityMaterializationLimits.max_unreclaimable_charged_payload_bytes
operation absolute deadline
ContextInputIoService pending operation cap
```

约束。

Cursor 全量 chunks 也必须满足 active base 的同一物理 contract。若 accumulated delta 超限，正常系统应先通过 transcript checkpoint/rebase推进
base；若仍超限，沿用 existing physical admission/checkpoint blocker，不允许 Cursor 自行丢事件。

---

## 9. Proof 与 fingerprint 保持

### 9.1 不改变现有 durable proof

仍使用：

```text
TranscriptDomainPrefixFact
TranscriptDomainSparseReadProofFact
TranscriptProjectionSemanticSourceFact
ContextEventReferenceFact
ContextTranscriptAuthorityFact
```

Cursor 不进入上述任何 DTO。

### 9.2 Full refs 保留

`ContextTranscriptAuthorityFact.transcript_domain_delta_refs` 当前保存完整 ordered refs。因此 V1 Cursor 必须保留 active base之后全部 transcript semantic
envelope identities。

不得把 authority改成：

```text
cursor fingerprint only
prefix accumulator only
last event ID only
```

### 9.3 复杂度声明

Cursor 后：

```text
PostgreSQL semantic range bytes read       O(new semantic delta)
historical payload decode                  O(new semantic delta)
chunk append                               O(new semantic delta)
```

但仍有：

```text
full refs tuple materialization            O(active semantic prefix)
selected event IDs fingerprint             O(active semantic prefix)
authority DTO canonicalization             O(active semantic prefix)
manifest serialization                     O(active input manifest)
stable entries tuple export                O(active normalized transcript)
```

这些是后续 composable range proof/Merkle identity hard cut 的范围，不得在 PERF2A benchmark 中误报为已消除。

### 9.4 Cursor fingerprint

`cursor_fingerprint` 只用于 process-local corruption detection，输入至少包括：

```text
base identity fingerprint
verified through sequence
delta before/after prefix values
semantic envelope vector fingerprint
semantic source fingerprint
sparse proof completeness fingerprint
reducer evidence snapshot fingerprint
cursor implementation contract fingerprint
```

它不进入 manifest 或 provider identity。

### 9.5 Materialization-equivalence contract

新增process-local、versioned binding：

```python
@dataclass(frozen=True, slots=True)
class TranscriptProjectionMaterializationEquivalenceContract:
    contract_id: Literal[
        "pulsara.transcript-projection-materialization-equivalence"
    ]
    contract_version: Literal["1"]
    inline_message_contract_fingerprint: str
    message_artifact_contract_fingerprint: str
    terminal_document_contract_fingerprint: str
    stable_entry_union_contract_fingerprint: str
    contract_fingerprint: str


class TranscriptProjectionMaterializationEquivalenceBinding:
    def compare(
        self,
        *,
        left: PreparedTranscriptProjectionEvidence,
        right: PreparedTranscriptProjectionEvidence,
    ) -> TranscriptProjectionMaterializationEquivalenceResult: ...


class TranscriptProjectionMaterializationMismatchCode(StrEnum):
    STABLE_ENTRY_COUNT = "stable_entry_count"
    STABLE_ENTRY_SEMANTIC = "stable_entry_semantic"
    NORMALIZED_MESSAGE_CONTENT = "normalized_message_content"
    TERMINAL_DOCUMENT = "terminal_document"
    PAIRING = "pairing"
    ATTRIBUTION = "attribution"
    SOURCE_REFERENCE = "source_reference"
    NORMALIZED_TRANSCRIPT = "normalized_transcript"


@dataclass(frozen=True, slots=True)
class TranscriptProjectionMaterializationEquivalenceResult:
    equivalent: bool
    mismatch_code: TranscriptProjectionMaterializationMismatchCode | None
    left_normalized_transcript_fingerprint: str
    right_normalized_transcript_fingerprint: str
    compared_stable_entry_count: int
    compared_terminal_document_count: int
    contract_fingerprint: str
```

Result是process-local comparison DTO，不保存自己的fingerprint。Invariant：

- `equivalent=True`时`mismatch_code is None`，双方normalized transcript fingerprint相等，所有逐项比较已完成；
- `equivalent=False`时`mismatch_code`必填，并在第一个canonical traversal mismatch处停止；
- count均为non-negative bounded integer；
- `contract_fingerprint`必须等于当前binding contract，不能由caller传入另一个版本。

比较算法：

1. 要求两侧projection base semantic identity、semantic source、proof和semantic delta严格相等；
2. 按ordinal遍历stable entries；
3. inline message直接读取typed blocks；
4. artifact-backed message通过reference从hydrated contents读取并验证document/hash/contract；
5. terminal projection ref从各自frozen document view解析；
6. 将每个entry降为同一typed provider-semantic materialization；
7. 比较ordered semantic identities、provider blocks、pairing、attribution与source refs；
8. 忽略inline/artifact carrier kind、artifact ID、page/tree layout与未被stable entries引用的extra hydrated documents；
9. 输出双方normalized materialization fingerprint与bounded mismatch code。

禁止只比较`stable_entries == stable_entries`，也禁止只比较caller自报的normalized transcript fingerprint。

### 9.6 Frozen-base restore

新增：

```python
def restore_transcript_projection_from_base(
    *,
    event_log: EventLog,
    archive: ArtifactStore,
    runtime_session_id: str,
    frozen_anchor_identity: TranscriptProjectionCursorBaseIdentity | None,
    requested_through_sequence: int,
    ...,
) -> RestoredTranscriptProjection: ...
```

该helper必须：

- 按anchor carrier exact ID读取并重算stable identity；
- 验证carrier sequence不晚于requested H；
- run seed分支只使用该RunStart seed；
- checkpoint分支只使用该committed checkpoint/candidate/materialization；
- 不扫描并选择“更新”的checkpoint；
- 从frozen base读取bounded delta到requested H；
- 返回严格相同的projection base/proof/semantic delta；
- hydrate该base物理tree所需message documents及stable/pending terminal documents。

Cursor same-base correctness路径必须传入`frozen_anchor_identity`。历史manifest replay可传`None`，此时helper仍严格恢复manifest-owned projection base，
但不得把缺少durable carrier identity的结果adopt为live Cursor anchor。Run-seed分支传入identity时必须exact读取RunStart并返回
`anchor_carrier_event`；checkpoint分支必须同时校验committed event identity与candidate fingerprint。

EC1 shadow comparison、Cursor mismatch确认和同base correctness测试必须使用该helper。Generic `restore_transcript_projection()`继续负责startup、requested-behind与
historical best-compatible-base选择；不能拿generic restore的另一种合法materialization与live Cursor做raw DTO equality。

Generic restore的candidate eligibility也必须改为：

```text
anchor carrier committed sequence <= requested_through_sequence
```

不能只按seed source/checkpoint candidate sequence过滤。

---

## 10. Cancellation、deadline 与 close

### 10.1 Caller cancellation

`ContextInputIoService` 已经拥有 physical I/O。Cursor 不新增 thread pool。

若 caller 在 delta read期间取消：

- caller收到 cancellation；
- ContextInputIoService继续持有/收口已启动 physical operation；
- Cursor 保持旧 verified snapshot；
- 不发布 partial candidate；
- 后续 compile可以重新读取同一 delta；
- Host close继续通过现有 context I/O drain等待 physical operation。

V1 不要求 cancellation 后后台自动发布 Cursor。这避免新增第二个 service-owned recovery owner。

### 10.2 Deadline

`prepare_projection_evidence()`进入时冻结一个 absolute deadline；本次 primary read、proof compose与exact restore fallback必须复用该
deadline。不得在 fallback时重新获得完整 30 秒。Public API可以继续只接收requested high-water，由service入口统一冻结deadline。

Deadline 到达：

- Cursor不变；
- waiter按现有 typed context I/O timeout失败；
- physical operation按现有 service规则drain；
- 不写 durable cursor failure event。

### 10.3 Session close

Close 顺序不新增独立阶段：

```text
stop new context preparation
drain ContextInputIoService physical operations
drain checkpoint owner
exact-retire Cursor resident handle and drop references
continue existing RuntimeSession/Host teardown
```

Cursor retirement永不构成close blocker；存在borrow时manager将handle标记retired，并在最后一个lease释放后扣除resident charge。正在执行的底层I/O仍是
blocker。

### 10.4 Process crash

进程退出时process resident manager与全部Cursor一起丢失。Reopen继续执行现有 transcript projection exact restore，并尝试重新seed Cursor；resident
admission被拒绝不影响reopen正确性。

不需要 durable cursor recovery。

---

## 11. Mismatch 与 fallback 矩阵

| 条件 | Cursor动作 | Canonical动作 | durable latch |
|---|---|---|---|
| Cursor absent | 不使用 | exact restore | 否 |
| same-H exact join | 复用 | 无数据库读取 | 否 |
| valid delta extension | CAS推进 | bounded sparse read | 否 |
| requested behind live | 不回退 Cursor | one-shot exact restore | 否 |
| anchor generation changed | discard candidate | retry或exact restore | 否 |
| new anchor carrier sequence > requested H | 不使用future anchor | historical one-shot exact restore | 否 |
| carrier stable identity/candidate fingerprint mismatch | invalidate | frozen-base或generic exact restore | 仅canonical失败时 |
| base identity mismatch | invalidate | exact restore | 否 |
| process-local Cursor contract changed、durable registry相同 | invalidate | exact restore/rebind | 否 |
| durable event/schema registry changed | drop with old session | DB reset或独立migration；旧ledger restore继续拒绝 | 现有contract mismatch |
| delta.before mismatch | invalidate | exact restore | 仅 exact restore失败时 |
| live reducer high-water ahead | 不使用 fast path | one-shot exact restore | 否 |
| live reducer high-water behind | 不使用 | existing committed-reducer blocker | 现有语义 |
| document ref missing | invalidate | exact hydrate/restore | 仅 canonical缺失时 |
| Cursor fingerprint corrupt | invalidate | exact restore | 否 |
| process resident admission rejected | 不发布candidate，保留兼容旧handle | 返回本次exact evidence | 否 |
| process resident LRU eviction | exact-handle清除 | 下一次prepare按absent exact path | 否 |
| exact restore ledger untrusted | 无 | fail closed | 现有 latch |
| cancellation/deadline | 保留旧 Cursor | operation drain | 否 |

原则：cache failure不能被升级为 durable ledger failure；canonical proof failure也不能被降级成 cache miss。

---

## 12. Observability

### 12.1 Process-local metrics

新增以下 secret-safe metrics：

```text
projection_cursor_outcome
projection_cursor_previous_through_sequence
projection_cursor_requested_through_sequence
projection_cursor_new_ledger_events
projection_cursor_new_semantic_events
projection_cursor_new_semantic_payload_bytes
projection_cursor_new_semantic_stored_envelope_bytes
projection_cursor_prefix_rows_read
projection_cursor_prefix_logical_bytes_read
projection_cursor_total_logical_bytes_read
projection_cursor_full_semantic_events
projection_cursor_full_semantic_payload_bytes
projection_cursor_database_read_wall_seconds
projection_cursor_proof_compose_wall_seconds
projection_cursor_document_freeze_wall_seconds
projection_cursor_exact_restore_wall_seconds
projection_cursor_hit_count
projection_cursor_delta_extension_count
projection_cursor_exact_restore_count
projection_cursor_invalidation_count
projection_cursor_anchor_generation
projection_cursor_resident_charge_bytes
projection_cursor_process_resident_charge_bytes
projection_cursor_process_resident_chunk_count
projection_cursor_process_resident_cursor_count
projection_cursor_resident_admission_count
projection_cursor_resident_admission_rejected_count
projection_cursor_resident_eviction_count
projection_cursor_resident_evicted_charge_bytes
projection_cursor_fast_validation_wall_seconds
projection_cursor_deep_validation_wall_seconds
```

不得记录：

- event正文；
- tool arguments/result正文；
- artifact正文；
- prompt/provider payload；
- secrets；
- unbounded exception message。

### 12.2 Operational diagnostics

Diagnostic 使用 bounded enum code：

```text
cursor_seed_join_failed
cursor_base_identity_mismatch
cursor_prefix_mismatch
cursor_reducer_snapshot_mismatch
cursor_document_view_failed
cursor_anchor_cas_lost
cursor_fingerprint_invalid
cursor_resident_admission_rejected
cursor_resident_evicted
cursor_resident_chunk_identity_conflict
```

可记录 exception type，不记录 raw exception `str()`。

### 12.3 Host/Runtime live diagnostics

Historical `InspectorService`不能从PostgreSQL重建Cursor，本阶段不修改它，也不在历史投影中伪造`present=False`。

Cursor只通过live owner暴露：

```python
class RuntimeSession:
    def transcript_projection_cursor_diagnostics(
        self,
    ) -> LiveTranscriptProjectionCursorDiagnostic: ...


class HostSession:
    def snapshot(self) -> HostSessionSnapshot:
        # 嵌入当前live runtime cursor bounded diagnostics
        ...
```

Live diagnostics只展示 bounded process-local snapshot：

```text
present
base_kind
verified_through_sequence
semantic_event_count
semantic_payload_bytes
chunk_count
resident_charge_bytes
process_resident_charge_bytes
process_resident_chunk_count
process_resident_cursor_count
resident_budget_max_bytes
resident_admission_rejected_count
resident_eviction_count
last_outcome
hit/delta/fallback counters
invalidation_count
```

输出必须标注：

```text
authority_kind = process_local_verified_memoization
```

不得显示成 durable fact或 replay source。

输出规则：

- live RuntimeSession存在：Host snapshot返回`live_runtime.transcript_projection_cursor`；
- session已detach但RuntimeSession仍open：仍可读取live snapshot；
- session未open/已物理关闭：没有可调用live owner，不生成Cursor snapshot；
- historical Inspector继续只展示durable seed/checkpoint/proof，不混入Cursor；
- exact replay与standalone inspector CLI不得调用live diagnostics。

---

## 13. 与 Stage 5 ContextSource Hard Cut 的边界

### 13.1 Stage 5 可以改变什么

Stage 5 可以改变：

- ContextSource registry；
- candidate producer ownership；
- source collection policy；
- source/candidate ingress DTO；
- named model-visible facts如何注册；
- candidate选择与 omission policy。

### 13.2 Stage 5 兼容边界

纯 ContextSource/candidate ingress hard cut不得重新解释：

- transcript projection reducer；
- run seed/checkpoint base identity；
- terminal projection documents；
- transcript sparse completeness proof；
- Cursor base/prefix identity；
- `PreparedTranscriptProjectionEvidence` 的 canonical含义。

但 Stage 5可以合理新增AgentEvent或调整event schema/domain registry。Cursor base绑定完整
`event_domain_registry_contract_fingerprint`，而durable run seed/checkpoint也冻结旧registry fingerprint。必须区分两种情况：

```text
仅process-local Cursor损坏/丢失，durable registry未变
    -> discard Cursor
    -> exact restore同一durable registry
    -> seed new Cursor

Stage 5改变event schema/domain registry
    -> close/drain所有旧RuntimeSession
    -> reset PostgreSQL与相关durable artifact test state
    -> 用新registry bootstrap新ledger/run seed/checkpoint
    -> seed new Cursor
```

仅discard Cursor不能修复durable registry mismatch：old seed/checkpoint fingerprint与current registry不同，exact restore必须继续fail closed。本项目尚未
上线，Stage 5若改变registry，V1明确使用数据库reset，不实现旧ledger migration或兼容decoder facade。未来若需要保留历史ledger，必须单独定义
registry/schema migration hard cut，不能塞进Cursor fallback。

当前代码中registry fingerprint覆盖完整`DEFAULT_EVENT_SCHEMA_REGISTRY`的supported event schema集合，包括non-transcript event；restore又要求
seed/checkpoint fingerprint与current binding精确相等。因此即使只新增non-transcript event，也属于上述durable registry hard cut，而不是Cursor-local
cache invalidation。

Cursor自身仍不需要schema migration，因为它没有durable schema；它也不增加Stage 5已有的reset成本。Architecture guard不得反向禁止Stage 5新增
合法non-transcript event，但相应变更必须选择“DB reset”或未来独立migration，不能声称仅exact restore即可跨越。

### 13.3 Cursor key 禁止项

Cursor key不得包含：

```text
ContextSource ID
candidate ID
candidate set fingerprint
source registry version
selected source IDs
provider model/target
active context window ID/generation
tool-result rendering profile
invocation timing
compiled context ID
manifest ID
```

因此 Stage 5 只删除旧source ownership、且不改变event/schema registry时，不需要迁移Cursor或重置ledger。若同时改变registry，Cursor instance会随
session close自然丢弃，durable state按上述规则reset；Cursor不提供跨registry恢复能力。

### 13.4 实施顺序

冻结建议：

```text
PERF0/low-risk writer measurement
    -> Context Evidence Cursor (本规格)
    -> re-measure context suite
    -> Stage 5 ContextSource Ownership Hard Cut
    -> source-aware prepared cache（若仍需要）
```

Cursor 不应推迟到 Stage 5之后，因为当前 19 秒 evidence read 与 source ownership无关；提前完成可以缩短 Stage 5 每次真实回归的反馈时间。
这里承诺的是Cursor设计不因纯source ownership改造返工，不承诺Stage 5 registry hard cut可以复用旧durable ledger。Stage 5按计划reset数据库后，
Cursor从新seed/checkpoint重新建立。

---

## 14. 文件落点

### 14.1 新增

```text
src/pulsara_agent/runtime/authority_materialization/evidence_cursor.py
src/pulsara_agent/runtime/authority_materialization/cursor_resident_budget.py
tests/test_context_evidence_cursor.py
tests/test_cursor_resident_budget.py
```

`evidence_cursor.py` 只包含：

- process-local DTO；
- chunk vector；
- identity/fingerprint helper；
- cursor compose/validate helper；
- operational outcome enum。

`cursor_resident_budget.py`只包含process singleton、resident charge estimator、admission reservation、handle/lease、LRU eviction与bounded
diagnostics；不得import compiler、EventLog writer或durable schemas以外的业务owner。

它不得 import：

- AgentRuntime；
- HostSession；
- ContextSource；
- compiler；
- provider transport。

### 14.2 修改

```text
src/pulsara_agent/runtime/authority_materialization/checkpoint_service.py
src/pulsara_agent/runtime/authority_materialization/transcript_reducer.py
src/pulsara_agent/runtime/authority_materialization/transcript_restore.py
src/pulsara_agent/runtime/authority_materialization/contracts.py
src/pulsara_agent/runtime/wiring.py
src/pulsara_agent/runtime/session.py
src/pulsara_agent/runtime/context_input/transcript_authority.py
src/pulsara_agent/runtime/context_input/live.py
src/pulsara_agent/runtime/context_input/stable_transcript.py
src/pulsara_agent/host/session.py
tests/test_authority_materialization_contract.py
tests/test_context_input_architecture.py
tests/test_runtime_event_architecture.py
tests/test_host_lifecycle_contract.py
tests/test_inspector.py
benchmarks/durable-runtime/generators/context_*.py
benchmarks/durable-runtime/runners/run_dataset.py
```

`live.py` public call形状应保持不变；修改主要是 metrics attribution与只读 document view typing。

### 14.3 不应修改

除非实现发现当前 contract自相矛盾，否则本阶段不应修改：

```text
src/pulsara_agent/event.py
src/pulsara_agent/event_log/serialization.py
src/pulsara_agent/primitives/context.py durable schemas
src/pulsara_agent/primitives/transcript_projection.py durable schemas
database migrations
```

若实现需要修改上述文件，应暂停并回到规格审查，不能在 PR 内临场扩范围。

---

## 15. 实施顺序

本阶段拆为 EC0–EC4。每个 PR 必须独立全绿，不保留双生产路径。

### EC0：不可变 seam 与观测

目标：不改变 production evidence选择，仅建立可验证的 Cursor输入。

实施：

1. 新增 `evidence_cursor.py` process-local DTO；
2. 新增唯一`ValidatedCursorSnapshotFactory`、`ValidatedCursorUseToken`与immutable chunk vector；
3. 冻结full-build/deep-validate与normal fast-validate两种成本边界；
4. 新增process-owned`CursorResidentBudgetManager`、fixed limits、charge estimator与dependency wiring；
5. 新增同步`_anchor_state_lock`及carrier-bound anchor DTO，不启用fast path；
6. 新增 `TranscriptProjectionStateStore.evidence_snapshot()`；
7. 新增 `TranscriptProjectionDocumentRegistry.freeze_references()`；
8. 新增只接受validated token的`compose_verified_transcript_sparse_read_proof()`；
9. 新增materialization-equivalence result/binding与frozen-base restore seam；
10. `PreparedTranscriptProjectionEvidence` 接受只读 document resolver protocol；
11. 增加Cursor与resident budget metrics schema，但production outcome保持disabled；
12. architecture guard禁止Cursor进入durable schema registry。

验收：

- existing evidence结果完全不变；
- `snapshot()+stable_entries()` 的 production组合读取归零；
- document view只包含stable-entry exact refs；
- direct Cursor construction与self-certified proof被拒绝；
- resident manager为全进程唯一，超限只拒绝memoization；
- no event/schema/database migration。

### EC1：Startup seed 与 shadow comparison

目标：从已完成 exact restore构造 Cursor，但不用于production返回。

实施：

1. RuntimeSession startup restore后从exact carrier构造shadow Cursor并执行resident admission；
2. 每次现有 evidence完成后构造 shadow next Cursor；
3. 使用`restore_transcript_projection_from_base()`冻结相同base；
4. 严格比较projection base、proof、delta envelopes、semantic source；
5. 使用versioned materialization-equivalence比较stable entries/documents；
6. mismatch只记录 diagnostic并使测试失败；
7. 增加 carrier-aware anchor generation/invalidation；
8. benchmark记录 hypothetical hit/delta bytes。

验收：

- context suite所有case strict-proof与materialization-equivalence通过；
- real plan/subagent/compaction smoke无 mismatch；
- startup/restart/checkpoint rebase全部生成可验证 Cursor；
- Cursor删除后exact restore不变。
- resident rejection/eviction不改变shadow correctness结果。

### EC2：Same-high-water hard cut

目标：compile retry或无新ledger事件时零数据库 evidence read。

实施：

1. 启用 same-H path；
2. borrow resident handle，执行O(1) `validate_for_use()`并与exact reducer evidence snapshot join；
3. 冻结 exact document view；
4. normal same-H不调用 `read_transcript_domain_delta()`；
5. behind/ahead/mismatch仍exact restore；
6. 删除 shadow dual return path。

验收：

- same-H连续prepare只发生首次数据库 evidence read；
- proof/delta严格相同，materialization equivalent，manifest/provider payload逐字段相同；
- live store ahead回归保持exact restore；
- Cursor corruption自动失效。
- same-H normal path不深验old chunks。

### EC3：Incremental delta hard cut

目标：normal prefix growth只读 `(cursor_H, Hnew]`。

实施：

1. 启用 delta extension；
2. 完整 before/after prefix join；
3. validated-token proof composition与persistent chunk append；
4. process resident replacement admission；
5. active anchor generation CAS与admission commit/abort；
6. empty semantic delta推进；
7. cancellation/deadline保持旧 Cursor；
8. checkpoint/run-seed adoption invalidate；
9. production normal compile从 active base重读完整 semantic range的调用归零。

验收：

- 连续 N 次compile数据库读取总量接近全部 new delta之和；
- exact restore与Cursor path fingerprint相同；
- checkpoint adoption race不发布 stale Cursor；
- requested behind不回退 active Cursor；
- no second reducer。
- resident budget下LRU eviction与admission reject均退回exact path。

### EC4：Benchmark、guards 与收口

目标：用正式 baseline证明收益并关闭回流入口。

实施：

1. context suite新增 Cursor metrics；
2. 重跑6个context scenarios；
3. 跑3条real long-horizon dogfood；
4. RuntimeSession/HostSession snapshot增加bounded live Cursor view；
5. 增加 module-level architecture guards；
6. 更新 Stage 4.5性能文档实测结果；
7. 删除 EC1 shadow-only代码、临时diagnostics和测试开关。

验收：

- 本规格第17节全部 gate通过；
- dirty/clean baseline identity符合 benchmark contract；
- 没有 legacy evidence full-prefix normal path；
- 文档、代码与benchmark使用同一指标名称。

---

## 16. 测试矩阵

### 16.1 DTO 与纯算法

```text
test_cursor_base_identity_covers_projection_and_contracts
test_run_seed_anchor_requires_committed_run_start_carrier
test_checkpoint_anchor_requires_committed_carrier_and_candidate_fingerprint
test_anchor_availability_is_carrier_sequence_not_base_sequence
test_cursor_base_identity_excludes_context_source_and_invocation
test_envelope_chunk_rejects_unordered_or_duplicate_sequences
test_envelope_vector_append_structurally_shares_old_chunks
test_envelope_vector_empty_delta_preserves_chunks
test_cursor_fingerprint_rejects_tampered_prefix
test_cursor_fingerprint_rejects_tampered_envelope
test_cursor_anchor_identity_has_no_redundant_fingerprint
test_reducer_evidence_snapshot_fingerprint_covers_live_entries_and_refs
test_document_view_fingerprint_covers_reference_and_document
test_validated_cursor_factory_rejects_vector_proof_id_mismatch
test_validated_cursor_factory_rejects_vector_proof_count_mismatch
test_validated_cursor_factory_rejects_vector_outside_proof_range
test_validated_cursor_factory_rejects_base_prefix_not_derived_from_anchor
test_compose_requires_factory_validated_use_token
test_fast_validation_checks_outer_anchor_contract_and_reducer_only
test_fast_validation_does_not_walk_old_chunks
test_deep_validation_detects_tampered_old_chunk_in_debug_mode
test_composed_sparse_proof_equals_full_range_proof
test_composed_sparse_proof_decodes_only_new_delta
test_composed_sparse_proof_recomputes_full_event_id_fingerprint
test_event_id_fingerprint_and_authority_refs_share_one_prefix_traversal
```

### 16.2 Atomic reducer snapshot

```text
test_reducer_evidence_snapshot_freezes_state_and_entries_under_one_lock
test_reducer_evidence_snapshot_refs_cover_only_stable_entries
test_pending_assembly_documents_remain_owned_by_live_registry
test_document_view_contains_only_requested_immutable_documents
test_document_view_rejects_missing_or_mismatched_document
test_document_view_is_not_changed_by_future_registry_append
test_document_view_defensively_copies_sorted_entries
test_document_view_fingerprint_is_recomputed_from_contents
test_materialization_equivalence_result_has_strict_boolean_matrix
```

### 16.3 Startup/restart

```text
test_startup_exact_restore_seeds_verified_cursor
test_startup_restore_rebinds_exact_anchor_carrier
test_startup_cursor_seed_mismatch_falls_back_without_latching_ledger
test_reopen_rebuilds_cursor_without_durable_cursor_fact
test_checkpoint_restore_cursor_matches_exact_evidence
test_run_seed_restore_cursor_matches_exact_evidence
```

### 16.4 Same-H

```text
test_same_high_water_cursor_hit_performs_zero_event_log_reads
test_same_high_water_cursor_hit_preserves_sparse_proof
test_same_high_water_cursor_hit_preserves_manifest_fingerprint
test_same_high_water_cursor_hit_preserves_provider_payload
test_same_high_water_requires_exact_reducer_high_water
```

### 16.5 Delta extension

```text
test_cursor_reads_only_new_transcript_domain_delta
test_cursor_extends_across_non_transcript_only_suffix
test_empty_semantic_delta_reports_fixed_prefix_overhead_without_ratio
test_cursor_delta_before_must_equal_previous_prefix
test_cursor_delta_after_must_equal_reducer_snapshot
test_cursor_preserves_full_ordered_delta_refs
test_cursor_extension_does_not_decode_old_envelopes
test_cursor_extension_respects_event_and_byte_bounds
```

### 16.6 Race 与 fallback

```text
test_checkpoint_adoption_invalidates_inflight_cursor_candidate
test_run_seed_adoption_invalidates_previous_cursor
test_same_seed_with_new_run_start_carrier_rotates_generation
test_future_run_start_anchor_cannot_answer_past_high_water
test_future_checkpoint_carrier_cannot_answer_candidate_high_water
test_historical_restore_filters_anchor_by_carrier_sequence
test_anchor_state_lock_atomically_swaps_cursor_artifacts_and_context
test_requested_behind_live_store_uses_one_shot_exact_restore
test_live_store_ahead_never_returns_future_stable_entries
test_cursor_contract_change_forces_exact_restore
test_cursor_corruption_forces_exact_restore
test_durable_registry_change_requires_reset_or_migration_not_cursor_restore
test_exact_restore_failure_preserves_existing_ledger_error_classification
test_cancelled_cursor_read_does_not_publish_partial_cursor
test_deadline_does_not_replace_previous_verified_cursor
test_close_drains_context_io_not_cursor_cache
```

### 16.7 Checkpoint/rebase

```text
test_checkpoint_commit_advances_anchor_generation
test_checkpoint_anchor_joins_committed_carrier_and_candidate_fingerprint
test_checkpoint_rebase_retires_old_cursor_chunks
test_cursor_never_uses_checkpoint_newer_than_requested_high_water
test_checkpoint_failure_does_not_change_active_cursor_base
test_recovered_interrupted_checkpoint_keeps_cursor_fail_closed
```

### 16.8 Semantic/materialization equivalence

```text
test_same_base_cursor_and_restore_have_equal_proof_and_delta
test_inline_and_artifact_backed_message_are_materialization_equivalent
test_oversized_message_is_equivalent_before_and_after_checkpoint
test_oversized_message_is_equivalent_before_and_after_restart
test_extra_pending_documents_do_not_change_stable_materialization_equivalence
test_cursor_and_exact_restore_build_equal_context_authority
test_cursor_and_exact_restore_build_equal_manifest
test_cursor_and_exact_restore_build_equal_provider_projection
test_cursor_does_not_change_timing_projection
test_cursor_does_not_change_candidate_selection
```

### 16.9 Architecture guards

```text
test_cursor_types_are_not_registered_as_durable_schemas
test_cursor_is_owned_only_by_checkpoint_service
test_cursor_does_not_import_context_source_or_compiler
test_cursor_is_not_registered_as_committed_reducer
test_normal_projection_evidence_does_not_read_from_active_base
test_exact_restore_remains_available_only_for_restore_repair_and_fallback
test_stage5_source_identity_is_absent_from_cursor_key
test_pure_stage5_source_change_reuses_cursor_contract
test_stage5_registry_change_does_not_claim_cross_registry_exact_restore
test_historical_inspector_never_fabricates_cursor_snapshot
test_live_host_snapshot_exposes_bounded_cursor_diagnostics
```

### 16.10 Process resident budget

```text
test_cursor_resident_budget_is_process_wide_across_runtime_sessions
test_resident_charge_counts_payload_identity_and_fixed_object_reserves
test_maximal_legal_envelope_fixture_is_not_undercharged
test_default_process_budget_can_admit_one_maximal_legal_cursor
test_resident_admission_evicts_zero_borrow_lru_first
test_resident_admission_uses_larger_charge_as_lru_tie_breaker
test_diagnostics_and_failed_borrow_do_not_refresh_lru
test_resident_admission_rejection_performs_no_partial_eviction
test_resident_admission_rejection_returns_exact_evidence_without_latch
test_eviction_clears_only_matching_cursor_handle
test_eviction_does_not_change_anchor_reducer_or_durable_facts
test_borrowed_cursor_is_retired_only_after_last_lease_release
test_replacement_counts_shared_chunks_once
test_anchor_cas_failure_aborts_pending_resident_admission
test_close_retires_handle_without_becoming_close_blocker
test_process_manager_does_not_strongly_retain_closed_runtime_session
test_detached_sessions_share_the_same_process_budget
test_child_runtime_uses_the_same_process_budget
```

---

## 17. 性能验收

### 17.1 Correctness first

任何性能样本只有同时通过以下 grader 才可接纳：

```text
projection base/proof/semantic delta strict equality
stable content materialization equivalence
context authority equality
manifest fingerprint equality
provider payload equality
terminal/tool pairing equality
exact replay equality
```

### 17.2 离线 context suite gate

基于已提交 baseline，重跑相同：

```text
dataset version
scenario fingerprints
PostgreSQL template/schema fingerprint
production source identity
warmup/measured iterations
cache/reset policy
```

目标：

1. `transcript_projection_evidence_read_wall_seconds` median下降至少70%；
2. logical read满足：

   ```text
   projection_cursor_total_logical_bytes_read
       <= fixed_prefix_query_overhead_bytes
          + 1 * projection_cursor_new_semantic_stored_envelope_bytes
   ```

   `fixed_prefix_query_overhead_bytes`由同一registry/schema下high-water scalar与before/after prefix rows的canonical byte count冻结；不包含
   PostgreSQL wire framing。`new_semantic_stored_envelope_bytes`包含event wrapper与canonical payload，因此`K=1`；
3. `long-plan-prefix-growth` steady total从14.551s下降至少25%；
4. `incremental-active-window` steady total从24.698s下降至少25%；
5. long-plan `context_prepare_mean_wall_seconds < 0.75s`；
6. same-H fixture第二次及以后 evidence PostgreSQL read count为0；
7. checkpoint missing/rebase correctness不退化；
8. artifact read count不增加；
9. p95不得因 lock serialization显著恶化；
10. same-H normal path `projection_cursor_deep_validation_wall_seconds == 0`；
11. delta extension对old prefix最多执行一次event-ID/authority materialization traversal；
12. process resident charge始终不超过configured bytes/chunks/cursors limits；
13. resident admission rejection fixture仍通过全部correctness grader；
14. 0 correctness grader failures。

Empty semantic delta必须单独满足：

```text
projection_cursor_new_semantic_events == 0
projection_cursor_new_semantic_stored_envelope_bytes == 0
projection_cursor_prefix_rows_read <= 2
projection_cursor_total_logical_bytes_read <= fixed_prefix_query_overhead_bytes
```

不得计算`read_bytes / new_semantic_bytes`，避免零分母。PostgreSQL协议、TLS与pool framing若需测量，归PERF0物理I/O指标，不进入本逻辑
correctness gate。

### 17.3 Real dogfood gate

至少运行：

- long plan/job queue；
- subagent system；
- long compaction/current-run window。

比较：

```text
context_prepare union
transcript evidence read union
context prepare per model call
cursor hit/delta/fallback counts
provider payload fingerprint
final task outcome
```

Real dogfood只用于最终轨迹验证，不用3次样本声称稳定 p95。

### 17.4 预期收益口径

根据 baseline，合理中心估计：

```text
transcript evidence substage    -70% to -90%
context prepare total           -25% to -50%
long-plan context total         center estimate about -40%
```

这是实施目标，不是先验保证。若 evidence I/O下降而 total context未下降，PERF0必须继续分解：

- stable entries export；
- full refs fingerprint；
- manifest canonicalization；
- artifact hydration；
- candidate/source collection。

不得通过修改 benchmark fixture 掩盖瓶颈转移。

---

## 18. Architecture 与 grep gates

### 18.1 Normal path 禁止

完成 EC3 后，production normal context prepare禁止：

```text
read_transcript_domain_delta(active_base, requested_H)
```

每次无条件从 active base读取。

允许调用点必须属于：

- startup/reopen exact restore；
- explicit repair/doctor；
- Cursor absent/mismatch fallback；
- requested high-water behind live store；
- checkpoint/run-seed restore；
- tests。

Guard应检查 module-level call graph与 allowlist，不能只检查一个函数体中的直接 `.iter()` 或 helper名称。

### 18.2 禁止第二 reducer

Guard禁止 `evidence_cursor.py`：

- import transcript reducer private apply函数；
- decode `AgentEvent` 并修改 stable/live state；
-注册 committed reducer；
-维护 pending model/tool/suspension maps。

### 18.3 禁止 durable 化

Guard禁止 Cursor DTO：

- 继承 `FrozenFactBase`；
- 出现在 AgentEvent union；
- 注册到 event schema registry；
- 写入 artifact store；
- 写入 PostgreSQL projection table；
- 出现在 Context manifest。

### 18.4 Stage 5 boundary guard

Guard禁止 Cursor模块 import：

```text
context candidate collector
ContextSource registry
provider input plan
model transport
tool-result renderer
```

### 18.5 Resident budget guard

Guard必须保证：

- production只通过process singleton取得`CursorResidentBudgetManager`；
- RuntimeSession、HostSession、child runtime不得各自构造manager或覆盖production limits；
- `_verified_evidence_cursor_handle`只能由resident manager admission返回；
- normal compile必须borrow exact handle，不能直接长期持有裸Cursor；
- eviction callback只清除matching handle，不修改anchor/reducer/durable state；
- resident charge、LRU、borrow count不得进入Cursor/durable/provider fingerprint。

---

## 19. Failure codes

本阶段复用现有 canonical错误分类。只新增 operational enum：

```text
cursor_absent
cursor_requested_behind
cursor_live_store_ahead
cursor_anchor_changed
cursor_anchor_not_yet_available
cursor_anchor_carrier_mismatch
cursor_checkpoint_candidate_mismatch
cursor_base_mismatch
cursor_prefix_mismatch
cursor_reducer_mismatch
cursor_document_missing
cursor_materialization_mismatch
cursor_contract_changed
cursor_corrupt
cursor_resident_admission_rejected
cursor_resident_evicted
cursor_resident_chunk_identity_conflict
cursor_cancelled
cursor_deadline_exceeded
```

映射：

- 所有cursor-local identity/proof/materialization code均先discard并尝试canonical fallback；
- resident admission rejected/evicted只影响memoization，返回exact evidence或下次exact restore；
- resident chunk identity conflict拒绝cache candidate并记录operational diagnostic，不改变canonical ledger；
- cancellation/deadline传播现有 caller outcome；
- fallback成功不写 durable failure event；
- fallback失败使用现有 `contract_mismatch | ledger_untrusted | artifact_missing` 等分类；
- 不新增 `cursor_error` 作为 durable context compile reason。

---

## 20. Definition of Done

以下条件全部成立，PERF2A 才完成：

1. Cursor 是 process-local、session-owned、可丢弃；
2. EventLog仍是唯一 durable authority；
3. `TranscriptProjectionStateStore`仍是唯一 live reducer；
4. Cursor未注册为 committed reducer；
5. Cursor未进入任何 durable schema、event、artifact或数据库表；
6. Cursor base identity覆盖projection base、stable state、registry和reducer contract；
7. Cursor key不包含ContextSource/candidate/provider invocation identity；
8. startup exact restore可以seed Cursor；
9. same-high-water path不读PostgreSQL evidence range；
10. prefix growth只读 `(cursor_H, requested_H]`；
11. new delta before/after与Cursor和reducer exact join；
12. reducer state、stable entries与required refs在同一锁内冻结；
13. production evidence使用exact immutable document view；
14. live store ahead时不返回未来state；
15. requested behind时使用one-shot exact restore且不回退active Cursor；
16. run seed/checkpoint/rebase使用generation CAS使stale candidate失效；
17. cancellation/deadline不发布partial Cursor；
18. Host close只drain physical I/O，不等待cache retirement；
19. mismatch先丢弃cache，再走canonical exact restore；
20. cache mismatch不误latch ledger；
21. canonical proof failure不降级成cache miss；
22. 同base Cursor/exact restore的projection base、proof与delta严格相同，stable content满足versioned materialization equivalence；
23. Context authority/manifest/provider payload fingerprint相同；
24. exact replay不依赖Cursor存在；
25. full refs仍被保留，不用accumulator代替；
26. chunk append不复制旧envelope payload；
27. physical event/byte/deadline bound未放宽；
28. metrics不记录正文或secret；
29. context benchmark包含cursor hit/delta/fallback和read bytes；
30. 正式context suite 0 correctness failures；
31. evidence read median至少下降70%；
32. long-plan与active-window steady total至少下降25%；
33. long-plan context prepare mean低于0.75s，或有经审计的新瓶颈报告；
34. real long-plan/subagent/compaction轨迹通过；
35. normal production evidence full-prefix reread architecture gate为零；
36. Cursor durable化、第二reducer和Stage 5 identity污染 guards为零；
37. 全量非real pytest、Ruff、compileall、git diff --check通过；
38. 文档与性能计划同步最终实测结果；
39. 未新增旧数据库兼容、migration或fallback facade；Stage 5 registry hard cut使用开发期DB reset；
40. Stage 5可以在不迁移Cursor schema的情况下开始，但event/schema registry变化不得复用旧durable ledger；
41. run seed/checkpoint anchor均绑定完整committed carrier identity、sequence与availability；
42. checkpoint anchor精确绑定candidate fingerprint；
43. future carrier绝不回答过去high-water；
44. anchor、Cursor、reachable artifacts、latest checkpoint与active context由同一同步RLock原子交换；
45. 所有Cursor只能由validated factory构造；首次/deep模式验证完整vector/proof，normal use只执行O(1) fast validation；
46. oversized inline/artifact message在checkpoint、restart和Cursor路径满足materialization equivalence；
47. historical Inspector不伪造process-local Cursor，live diagnostics只由RuntimeSession/HostSession提供；
48. 纯Stage 5 source改造不使Cursor失效；event registry变化要求关闭旧session并DB reset或独立migration；
49. empty semantic delta使用固定prefix overhead gate，不计算零分母比例；
50. 全进程只有一个Cursor resident budget manager，固定限制bytes/chunks/cursors；
51. 发布Cursor前必须resident admission，replacement与anchor CAS使用prepare/commit/abort；
52. LRU eviction只清除exact Cursor handle，不改变anchor、reducer或durable facts；
53. resident admission rejected继续返回canonical exact evidence，不fail closed；
54. borrowed/retired handle在最后一个lease释放后才扣除resident charge；
55. carrier、reducer snapshot、document view与Cursor fingerprint均有唯一factory/formula，冗余entry/carrier fingerprint已删除；
56. proof composition只接受`ValidatedCursorUseToken`，不接收可拼出矛盾组合的平行字段；
57. same-H与delta normal path不深验旧chunks，full event-ID fingerprint与authority refs共享一次prefix traversal。
58. delta extension lease覆盖I/O、composition与install/fallback完整逻辑使用期；
59. caller取消后仍运行的physical I/O不持有Cursor/token强引用，只持有冻结scalar range；
60. resident charge只按真实对象共享去重，相同fingerprint的物理独立chunks分别收费；
61. eviction callback在全部manager locks之外执行，manager lock与anchor lock无反向获取路径；
62. admission reservation以phase/CAS收口，callback phase不得被并发abort；
63. use token冻结的generation、anchor、reducer与registry字段均由token fingerprint重验；
64. frozen run-seed/checkpoint restore按exact durable carrier重建stable identity；
65. materialization equivalence要求完整projection base严格相等；
66. frozen document resolver使用canonical index，不得按entry数量线性扫描每次lookup。

---

## 21. 最终拓扑

实施完成后的 production topology：

```text
Canonical EventLog
    |
    +--> transcript prefix facts
    |
    +--> exact bounded delta read --------------------+
                                                       |
Committed Event Batch                                  |
    |                                                  |
    v                                                  v
TranscriptProjectionStateStore              Verified Evidence Cursor
    |                                          (process-local memoization)
    +---------------- exact high-water join -----------+
                                                       ^
                                                       |
                              Process Cursor Resident Budget Manager
                                admission / lease / LRU eviction
                                                       |
                                                       v
                                  PreparedTranscriptProjectionEvidence
                                                       |
                                                       v
                                         ContextFactSnapshot / Manifest
                                                       |
                                                       v
                                             Stage 5 candidate ingress
```

关键口径：

```text
Cursor makes proof incremental.
Cursor does not become proof authority.
Cursor makes database work proportional to new delta.
Cursor does not yet make the entire compile proportional to new delta.
Cursor resident rejection removes memoization, never canonical truth.
```

这就是本阶段唯一允许的性能优化边界。
