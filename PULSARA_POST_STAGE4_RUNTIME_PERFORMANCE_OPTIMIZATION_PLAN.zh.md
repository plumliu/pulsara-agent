# Pulsara Stage 4.5 Durable Runtime 性能稳定化计划

> 状态：基于 real-LLM dogfood 实测形成的下一步优化计划；尚未实施
>
> 日期：2026-07-16
>
> 前置：
> `PULSARA_LONG_HORIZON_CONTEXT_WINDOWS_HARD_CUT_IMPLEMENTATION.zh.md`
>
> 前置：
> `PULSARA_AUTHORITY_MATERIALIZATION_AND_LOSSLESS_TRANSCRIPT_PROJECTION_DESIGN.zh.md`
>
> 建议位置：Stage 4 完成后的性能稳定化插曲，优先于继续扩大 Stage 5
> `ContextSource Ownership Hard Cut` 的改动面

---

## 0. 结论

Stage 4 与 Authority Materialization hard cut 已经解决了以下正确性问题：

- resolved model budget 是模型上下文预算的唯一真源；
- durable EventLog 仍是最终 authority；
- terminal projection、transcript checkpoint 与 materialization account 已有明确 owner；
- model stream、tool、checkpoint、child 与 governance 的 durable commit 不再绕开统一 account；
- compiler 不再依赖 sequence 1 的无界 full fold；
- checkpoint 是可丢弃、可重建的 acceleration，不是第二真源；
- writer cancellation、UNKNOWN/PARTIAL、stable confirmation 与 close drain 已有 fail-closed 语义。

但 real-LLM dogfood 表明，当前物理实现的本地 durable 成本已经和远程 provider 等待处于同一量级。下一步不应删除 durable
facts、弱化 account、不记录 semantic delta，或把 correctness 换成吞吐；应新增一个范围严格受控的 **Stage 4.5 Durable Runtime
性能稳定化**。

冻结顺序为：

1. 先补齐 model control disposition 在持续 `NONE` 后的 session-owned live retry/drain owner；
2. 建立 deterministic runtime / PostgreSQL profiling，而不是用三次 real-LLM 估算稳定 p95；
3. 先优化 semantic batching 与 structural grouping，减少 transaction amplification；
4. 再压缩 PostgreSQL 单 batch 固定成本；
5. 重新测量后，只有剩余同步 durable wait 仍显著位于 model-stream 关键路径时，才实现 bounded one-inflight write-behind；
6. 将 transcript projection evidence 从“每次从 base 重读”改成 verified projection cursor；
7. 再做 verified artifact hydration 与 Stage-4-owned prepared context reuse；
8. 只有上述路径仍不足时，才讨论 durable semantic delta coalescing。

所有 retry owner、queue、cache、batching 与 connection reuse 都不得改变 event payload、stable ID、replay、accounting 或
provider-visible semantic identity。

两个已经闭环的事实不得在 Stage 4.5 中重新误判：

- memory governance mutation 与 stable governance runtime event candidate 已使用同事务 transactional outbox；ledger dispatch
  失败只形成可重试 pending dispatch，不再构成永久 split-brain；
- model control disposition 的 cancel-after-`FULL` 已消费 typed write outcome、执行 fold、安装 permit 并 adopt durable winner；当前只剩
  持续 `NONE` 后缺少独立 live retry/drain service 的 liveness 空洞。

本计划不是新的产品能力，也不改变 Stage 4 的长程预算策略。它只优化：

```text
相同 durable facts
相同 reducer output
相同 provider payload
相同 exact replay result
```

所需的物理 I/O、transaction、CPU 与 allocation 成本。

---

## 1. 实测基线

### 1.1 测量方法

本轮使用临时 pytest profiling plugin 对三条 real-LLM dogfood 轨迹进行单次采样。

Provider 时间覆盖：

```text
SanitizingProviderTransportExecution.read_next()
    -> await anext(raw_stream)
```

因此包含：

- 首个 provider stream item 的网络等待；
- 后续流式 item 的网络等待；
- provider stream terminal / usage 的等待；
- sanitizing transport 的少量本地处理。

Durable writer 时间覆盖：

```text
RuntimeEventWriteService.execute()
RuntimeEventWriteService.execute_blocking()
```

包括：

- session FIFO queue wait；
- PostgreSQL transaction；
- materialization account CAS；
- physical reservation charge；
- committed reducer fold；
- ordered publication enqueue，以及少数显式要求`await_delivery`的delivery wait。

它不包含任意observer callback的墙钟时间；普通semantic batch通常只计到ordered publication enqueue。

Context 时间覆盖：

- `prepare_live_context_snapshot()`；
- `ContextInputIoService.execute()`；
- context manifest persist；
- transcript checkpoint check；
- subagent graph checkpoint restore。

`provider exclusive` 与 `durable exclusive` 已扣除两者并发重叠时间，适合比较它们分别增加的墙钟关键路径。

### 1.2 三条 dogfood 结果

| 用例 | 墙钟 | 模型调用 | Provider 独占 | Durable 独占 | Context prepare |
|---|---:|---:|---:|---:|---:|
| Long Plan | 296.1s | 19 | 49.3s / 16.6% | 68.0s / 23.0% | 32.8s / 11.1% |
| Long PR4 compaction | 36.7s | 1 | 17.1s / 46.5% | 11.1s / 30.2% | 不走标准 compile |
| Subagent system | 41.8s | 13 | 15.4s / 36.8% | 10.3s / 24.6% | 5.2s / 12.4% |

说明：

- Long Plan 与 Subagent system 通过；
- Long PR4 compaction 已完成真实 compaction，但生成摘要未包含测试要求的字面量 `pr4`，因此语义断言失败；完整调用链的性能
  数据仍然有效；
- 单次采样不能作为稳定 benchmark；PERF0必须使用20–50次确定性fixture计算median/p95，real-LLM只保留三次median/range；
- 三条轨迹的远程 provider 速度、输出长度和工具行为不同，不能只比较总墙钟。

### 1.3 关键明细

#### Long Plan

```text
model calls                         19
provider read items              8,019
durable writer waits            1,125
durable writer union             75.2s
durable writer exclusive         68.0s
context prepare                  32.8s
context I/O                      28.7s
transcript projection evidence   19.0s
manifest I/O                      4.0s
subagent checkpoint restore       2.4s
transcript checkpoint check       0.1s
```

Long Plan 中，durable writer 独占时间约为 provider 独占时间的 `1.38x`。这不是远程模型慢可以掩盖的小额固定成本。

#### Long PR4 compaction

```text
model calls                          1
provider read items              3,584
durable writer waits              635
provider exclusive              17.1s
durable exclusive               11.1s
```

一个长 model stream 即产生数百次 writer completion。当前 semantic batching 降低了 transaction 数量，但仍存在明显的 transaction /
account charge amplification。

#### Subagent system

```text
model calls                         13
durable writer waits               436
provider exclusive               15.4s
durable exclusive                10.3s
context prepare                   5.2s
context I/O                       3.2s
subagent checkpoint restore       1.2s
```

并发 child 会让 provider、tool 和 durable I/O 部分重叠，但 durable 独占时间仍约为 provider 独占时间的 `67%`。

### 1.4 当前判断

实测不支持以下假设：

```text
database I/O + durable actions
    << remote model API time
```

更准确的现状是：

```text
database I/O + durable actions
    ~= remote model API time 的同一量级
```

并且 Long Plan 已出现：

```text
durable exclusive > provider exclusive
```

Stage 4 后 real-LLM 全量测试变慢，不只是 provider 波动或新增测试；durable accounting、semantic batch commit 和 context evidence
materialization 都贡献了真实墙钟成本。

### 1.5 Context preparation 为什么达到 32.8 秒

Long Plan 的`context prepare=32.8s`不是主要消耗在prompt字符串拼接、token estimator或最终lowering。实测中最大的单项是：

```text
transcript-projection-evidence-read   19.0s
```

占context preparation约`58%`。

当前`prepare_projection_evidence(requested_through_sequence=H)`会从active run seed或checkpoint base开始，读取到本次compile冻结的
high-water：

```text
compile 1: base -> H1
compile 2: base -> H2
compile 3: base -> H3
...
```

而不是：

```text
compile 1: base -> H1
compile 2: H1 -> H2
compile 3: H2 -> H3
```

每次`read_transcript_domain_delta()`还会执行：

1. 冻结ledger high-water；
2. 读取before/after transcript prefix facts；
3. 通过server-side cursor读取整个区间的transcript semantic events；
4. 为每个row重建canonical raw envelope并统计payload bytes；
5. 再逐event decode；
6. 从before accumulator重新计算完整semantic accumulator；
7. 验证最终count与accumulator等于after prefix fact。

所以这不是一次廉价的high-water check，而是一次完整的bounded proof reconstruction。

Long Plan有`8,019`个provider stream items和19次model call。Semantic batching减少transaction，但不减少durable semantic event数量；
若adopted projection base在连续model steps之间不推进，后续每次compile都会重新读取越来越长的semantic prefix，形成近似：

```text
O(prefix_1 + prefix_2 + ... + prefix_model_step_count)
```

除了evidence read，context preparation还串行执行：

```text
bounded authority bundle
subagent graph checkpoint restore
selected child result exact reads
static instruction artifact lookup/write
compaction summary/source artifact hydration
terminal projection content hydration
stable transcript projection
tool-result render preparation
candidate collection
named-fact artifact hydration
```

当前主要观测值为：

```text
transcript projection evidence   19.0s
subagent checkpoint restore       2.4s
context live authority read       2.2s
named-fact artifact read          1.0s
```

其中`model-control-attribution-read=3.9s`发生在model terminal之后的control resolution，不属于
`prepare_live_context_snapshot()`，不得误计为context compiler成本。

Transcript checkpoint调度本身只有约`0.1s`，不是当前瓶颈。真正的问题是：

```text
checkpoint/base之后的完整evidence累积重读
    +
stable entries/document/artifact的重复hydration与projection
```

---

## 2. 不可破坏的语义边界

任何性能优化必须保留以下 invariant。

### 2.1 EventLog authority

- canonical EventLog 仍是最终 authority；
- terminal projection、checkpoint、materialization account 都是由 EventLog/reducer 可验证的 durable facts；
- 不允许以 process-local cache、PostgreSQL projection row 或 prepared object 替代 canonical event；
- privileged doctor 仍能从 supported raw ledger 重建 projection/checkpoint/account state。

### 2.2 Stable candidate 与 commit outcome

- 同一个 logical attempt 的 event ID 与 payload 必须稳定；
- batching 不得在 retry 时改变 semantic event payload或顺序；
- FULL/NONE/UNKNOWN/PARTIAL 语义不变；
- cancellation 后仍由 physical owner完成 stable confirmation；
- cache miss、worker重启或connection重连不得改变 durable result。

### 2.3 Model stream

- transport source item 的 sanitization、sequence index与terminal projection保持 lossless；
- completed/provider_error/cancelled/runtime_error 的 terminal outcome不变；
- batching buffer或条件性write-behind queue只保存stable frozen candidates，不保存可被后续修改的draft对象；
- confirmed semantic cursor只能由单一persistence owner推进；
- 同步batching阶段provider reader不得越过当前commit boundary；
- 只有条件性write-behind启用后，provider reader才可在hard bound内领先confirmed cursor；
- provider reader任何时候都不得领先physical reservation与burst hard bound；
- terminal projection必须等待semantic queue seal并FULL drain；
- control disposition仍只能消费confirmed terminal projection；
- provider stream observer仍只是 UI/live observation，不得成为 runtime control owner。

### 2.4 Materialization account

- reservation、charge、settlement与consumer horizon继续使用同一 ledger materialization generation；
- 不允许为了减少 event 数而跳过 physical headroom accounting；
- account row与durable bookkeeping events必须原子一致；
- checkpoint barrier、producer admission与close drain语义不变。

### 2.5 Context exactness

- projection cursor与context cache只能保存canonical reducer的可验证加速结果；
- cursor/cache identity必须绑定base identity、through sequence、contract fingerprint与semantic accumulator；
- cursor必须保存构造当前durable authority所需的完整delta identities，不能只保存prefix accumulator；
- cursor/cache-local mismatch先丢弃加速状态并执行现有exact restore；只有canonical restore/reducer仍不一致才fail closed；
- schedule、hit/miss不得进入provider semantic fingerprint；
- exact replay不得依赖process-local cursor/cache存在。

---

## 3. 优化优先级

### 3.1 Correctness prelude：Disposition `NONE` live retry/drain

`SessionModelCallControlDispositionOwner` 已经能持有 stable candidate、adopt durable winner，并在存在 pending candidate 时阻止
`RunEnd` 与 `RuntimeSession.close()`。但持续 `NONE` 超过当前 bounded inline retry 后，尚无独立 service-owned worker继续：

```text
retry same stable candidate
    -> FULL: fold + install permit/suppression + adopt winner
    -> NONE: retain candidate + bounded backoff
    -> UNKNOWN/PARTIAL: latch reconciliation
```

因此 Stage 4.5 的第一个 PR 必须新增 session-owned retry/drain owner：

- stable candidate仍只有一份；
- waiter cancellation只detach，不取消owner；
- safe point可以触发或join同一attempt，但不创建第二candidate；
- close执行bounded drain；仍为`NONE`时保持fail-closed，不清除candidate；
- `UNKNOWN/PARTIAL`立即latch，不降级成`NONE`；
- restart recovery与live owner使用同一candidate identity和winner invariant。

建议收敛为：

```python
class SessionModelCallControlDispositionRetryService:
    def adopt_pending(
        self,
        candidate: ModelCallControlDispositionResolvedEvent,
    ) -> DispositionRetryHandle: ...

    async def join_or_retry(
        self,
        resolved_model_call_id: str,
        *,
        deadline_monotonic: float,
    ) -> ModelCallControlResolutionResult: ...

    async def drain_pending(
        self,
        *,
        deadline_monotonic: float,
    ) -> None: ...
```

每个call只允许一个entry：

```text
PENDING_NONE
    -> RETRYING(generation)
    -> WINNER_FULL

PENDING_NONE / RETRYING
    -> RECONCILIATION_REQUIRED
```

worker由service拥有，waiter使用`asyncio.shield()`或等价detach机制。Retry使用bounded exponential backoff与绝对deadline；generation/CAS
防止迟到attempt覆盖新winner。`FULL`后必须完成fold、permit/suppression安装与winner adoption，才允许清除pending entry。

这是 correctness/liveness mini-PR，不依赖PERF0，也不授权提前实现通用write-behind subsystem。

### 3.2 P0：Model semantic stream batching amplification

这是当前最高优先级。

当前模型流按以下边界flush：

```python
_SEMANTIC_BATCH_MAX_EVENTS = 16
_SEMANTIC_BATCH_MAX_CHARS = 4_096
_SEMANTIC_BATCH_MAX_AGE_SECONDS = 0.025
```

每个batch仍需要：

```text
event writer FIFO
    -> materialization reservation charge
    -> business semantic events
    -> PhysicalOperationChargeAppliedEvent
    -> account row CAS
    -> PostgreSQL commit
    -> committed reducer fold
    -> ordered publication
```

25ms latency bound可在持续输出中产生接近每秒40次flush机会。即使pool已消除多数物理connection建立，transaction、WAL、account CAS、
bookkeeping event与Python DTO/fingerprint成本仍按batch重复。

第一轮优化目标不是合并durable semantic events，也不是先改变reader/writer等待拓扑，而是：

```text
减少 batch 次数
减少每 batch 固定物理成本
保持 source item 与 durable semantic event 的 lossless 一一对应
```

当前样本给出强信号：

```text
Long Plan:       8019 provider reads / 1125 writer waits ~= 7.1
Long compaction: 3584 provider reads /  635 writer waits ~= 5.6
```

这不是严格的`semantic events / transaction`，因为分子、分母都含少量其他操作；但足以说明实际batch远未稳定接近16-event hard bound。
Long compaction只有少量block Start/End，主要放大器更可能是25ms age flush。

PERF0必须先记录每次flush的：

```text
reason
event count
semantic UTF-8 bytes
canonical stored-envelope bytes
oldest event age
```

再选择target，不得直接将`25ms`武断改成`100ms`。

### 3.3 P0：PostgreSQL fixed-cost reduction

Batching会改变transaction workload，因此应先于SQL tuning落地。随后基于同一确定性fixture检查：

```text
queue wait
connection lease wait
transaction begin
session advisory lock
parent identity ensure
candidate serialization
materialization account read/CAS
event multi-row insert
run projection update
commit/WAL
reducer fold
publication enqueue
```

优先优化有分段证据的fixed cost；不允许用更大pool、更大线程池或降低durability掩盖transaction amplification。

### 3.4 P0 conditional：One-inflight write-behind

Write-behind保留在路线中，但降级为batching与SQL优化后的条件性步骤。

只有重新测量证明以下任一成立时才实施：

- transaction数量已经明显下降，但单次commit latency仍串行占据model stream关键路径；
- durable synchronous wait仍占显著墙钟，且存在可观测provider/persistence overlap headroom；
- PostgreSQL RTT/WAL抖动在远程数据库或快速provider下重新成为主要阻塞；
- first-visible latency、terminal drain与最大uncommitted tail仍能受hard bound约束。

V1只允许：

```text
one in-flight physical semantic commit
    +
one bounded accumulation buffer
```

不得建立无界logical batch queue。释放provider read的边界是：

```text
durable FULL
    +
committed reducer fold complete
```

不得等待observer delivery；observer failure只能形成operational diagnostic。

### 3.5 P0：Transcript projection evidence 重复读取

Long Plan 中：

```text
context prepare                  32.8s
context I/O                      28.7s
transcript-projection-evidence   19.0s
```

`prepare_projection_evidence()` 当前从adopted seed/checkpoint base读取到requested high-water的transcript-domain delta，用于证明
stable state的semantic count/accumulator与canonical ledger一致。

这个证明是正确性所需，但同一个active base在连续model steps中会反复读取高度重叠的prefix。下一步应将其改为：

```text
verified projection cursor at H
        +
canonical delta (H, new_H]
        ->
verified projection cursor at new_H
```

而不是：

```text
base
    ->
read entire delta through new_H
```

### 3.6 P1：Context artifact hydration 与 Stage-4-owned prepared reuse

以下内容可按immutable identity复用：

- terminal projection content artifact；
- named-fact artifact；
- window compaction source document；
- prepared transcript provider projection；
- manifest canonical bytes/fingerprint；
- prepared observation rollup。

但它们必须是bounded verified-content cache，key至少包含：

```text
artifact_id
artifact_sha256
artifact_size
media_type
codec/contract fingerprint
semantic owner fingerprint
```

任何identity变化自然miss；cache read error与miss不得改变durable semantic identity。

PERF3只允许复用Stage 4已经冻结的transcript、rollup、manifest canonical representation等identity。不得缓存Stage 5尚未hard-cut的
ContextSource collector、source registry或旧source ownership中间结果。

### 3.7 P2：Durable semantic delta coalescing

只有在完成：

1. deterministic profiling；
2. semantic batching与structural grouping；
3. PostgreSQL fixed-cost优化；
4. 条件性write-behind decision gate；
5. verified projection cursor；

之后，durable writer仍显著占用墙钟，才考虑将相邻source delta合并为durable chunk event。

这是schema与historical decoder变更，不是普通性能开关。它必须另立hard-cut规格并回答：

- source item provenance如何保留；
- transport sequence range如何编码；
- text/thinking/data/tool argument的block boundary如何保持；
- provider error/cancellation时open block如何恢复；
- terminal projection reducer如何lossless消费chunk；
- historical raw event如何与新chunk event共存；
- event-domain registry、burst contract与physical charge如何升级；
- Inspector如何展示chunk内部source item；
- exact replay与doctor如何验证chunk content。

Stage 4.5默认不实施这一步。

---

## 4. PERF0：内建性能观测与稳定基线

### 4.1 目标

将本轮临时pytest monkeypatch转成runtime-owned、默认低开销、可在dogfood显式开启的typed metrics。

### 4.2 Writer分段

每个write attempt至少记录：

```text
queue_wait_seconds
physical_operation_seconds
connection_lease_wait_seconds
transaction_seconds
session_lock_seconds
candidate_serialization_seconds
account_prepare_seconds
account_cas_seconds
event_insert_seconds
transaction_commit_seconds
stable_confirmation_seconds
reducer_fold_seconds
publication_enqueue_seconds
publication_delivery_wait_seconds

business_event_count
bookkeeping_event_count
candidate_payload_bytes
charged_payload_bytes
postgres_round_trip_count
```

Session-owned retry/outbox liveness还必须记录：

```text
disposition_pending_candidate_count
disposition_oldest_pending_age_seconds
disposition_retry_attempt_count
disposition_retry_outcome
governance_outbox_pending_count
governance_outbox_oldest_pending_age_seconds
governance_outbox_dispatch_attempt_count
governance_outbox_dispatch_outcome
```

Model semantic batching必须记录：

```text
semantic_flush_reason
semantic_flush_event_count
semantic_flush_source_item_count
semantic_flush_utf8_bytes
semantic_flush_canonical_envelope_bytes
semantic_flush_oldest_age_seconds
semantic_first_visible_commit_seconds
logical_semantic_batches_per_1k_source_items
semantic_events_per_logical_batch
```

若re-measure gate最终启用条件性write-behind，还必须追加：

```text
semantic_pipeline_pending_events
semantic_pipeline_pending_bytes
semantic_pipeline_oldest_unconfirmed_age_seconds
semantic_pipeline_provider_ahead_seconds
semantic_pipeline_backpressure_seconds
semantic_pipeline_commit_inflight_seconds
provider_persistence_overlap_seconds
semantic_pipeline_terminal_drain_seconds
```

这些metrics是operational observation：

- 不进入EventLog semantic facts；
- 不进入context manifest/provider payload；
- 不影响stable event ID；
- 可进入structured test report、Inspector operational diagnostics或独立metrics sink。

### 4.3 Context分段

每次compile至少记录：

```text
authority_bundle_read_seconds
authority_bundle_events/bytes
projection_evidence_read_seconds
projection_evidence_delta_events/bytes
projection_cursor_outcome
projection_cursor_previous_through_sequence
projection_cursor_requested_through_sequence
projection_cursor_new_delta_events/bytes
projection_authority_assembly_entries/bytes
terminal_artifact_hydration_seconds
named_fact_artifact_hydration_seconds
subagent_checkpoint_restore_seconds
transcript_checkpoint_seconds
provider_projection_build_seconds
manifest_build_seconds
manifest_persist_seconds
```

### 4.4 Provider分段

每个model call记录：

```text
transport_open_seconds
first_item_wait_seconds
stream_read_wait_seconds
source_item_count
semantic_batch_count
events_per_semantic_batch
semantic_utf8_bytes_per_batch
canonical_envelope_bytes_per_batch
oldest_event_age_per_batch
semantic_batch_flush_reason
terminal_commit_seconds
```

`semantic_batch_flush_reason`至少区分：

```text
block_end
tool_call_end
hard_max_events
hard_max_bytes
target_age
terminal
provider_error
cancellation
```

### 4.5 Benchmark协议

主要性能gate使用确定性fixture：

1. 固定sanitized provider stream、固定source item与interarrival timing的model stream replay；
2. 真实PostgreSQL上的固定candidate batch与固定account state；
3. 冻结长ledger上的连续compile与projection cursor推进；
4. batching、SQL与cursor fixture每项运行20–50次；
5. 分别记录冷cache与稳定warm-cache结果。

若PERF0分段证明时间仍主要消耗在PostgreSQL内部，后续深挖可以包含：

- `pg_stat_statements`；
- WAL bytes；
- `EXPLAIN (ANALYZE, BUFFERS, WAL)`；
- pool lease、advisory lock、transaction与commit/WAL分段。

这些不是首轮batching baseline的前置条件。首轮不声称精确归因PostgreSQL实际commit transaction数量，也不声称精确归因单个Pulsara writer独占产生的WAL bytes。当前commit-port instrumentation只提供caller-observed wall time和logical batch count；cluster LSN差值只能作为`postgres_cluster_wal_lsn_delta_bytes`诊断趋势，不进入acceptance。

Real-LLM只保留最终dogfood：

1. Long Plan；
2. Long manual compaction；
3. Subagent system。

同一版本每条运行3次，报告median、range和最慢样本，不用3个样本计算稳定p95。记录模型slot、provider binding、输出
tokens/source items与工具调用数量；provider总时间只作背景，不作为唯一pass/fail阈值。

所有fixture与dogfood都使用绝对值与归一化值：

```text
durable seconds / 1,000 stored events
logical semantic batches / 1,000 semantic source items
events and canonical bytes / logical semantic batch
bookkeeping events / business events
context evidence logical bytes <= fixed prefix overhead + new semantic stored-envelope bytes
context prepare seconds / model step
```

Empty semantic delta单独要求semantic envelope rows/bytes为0且只承担bounded prefix-query overhead；不得计算bytes/0比例。

#### 4.5.1 Durable Runtime Dataset V1 接线状态

离线数据集入口冻结为：

```text
benchmarks/durable-runtime/datasets/v1/
├── writer-scenarios/
└── context-scenarios/
```

`validate`、`plan`、`smoke`只验证typed dataset contract、case展开和worker隔离，不产生性能结论。首个真实production adapter为：

```text
model-semantic-batch-matrix
```

执行命令：

```bash
uv run python benchmarks/durable-runtime/runners/run_dataset.py \
  benchmark-writer \
  --postgres-dsn "$PULSARA_POSTGRES_DSN" \
  --postgres-admin-dsn "$PULSARA_BENCHMARK_POSTGRES_ADMIN_DSN" \
  --template-database pulsara \
  --output .benchmarks/model-semantic-batch.jsonl
```

冻结规则：

- application DSN使用普通Pulsara角色；
- admin DSN只负责本地`CREATE DATABASE ... TEMPLATE ...`与`DROP DATABASE`；
- 每个sample在计时前克隆clean template，计时后关闭RuntimeSession/pool并删除clone；
- 外层baseline串行执行，场景内部并发由scenario自己控制；
- batch 16是唯一production-valid baseline与semantic reference；
- batch 4/8是显式`sensitivity_analysis`矩阵，只用于解释batching曲线，不得进入production acceptance；
- batch 1/32/64只属于dataset counterfactual analysis，production writer adapter拒绝执行；
- manifest中的5次warmup、30次measured才是production baseline；
- scenario必须显式冻结`production_baseline_case_id=batch-16`，且必须与`semantic_reference_case_id`相同；
- production acceptance要求`git.dirty=false`；dirty worktree即使grader和iteration完整也只能生成diagnostic结果；
- `--case-id`或`--diagnostic-*-iterations`只用于接线诊断，结果必须写`measurement_contract_adhered=false`，不得进入acceptance；
- `--case-kind sensitivity_analysis`可正式执行batch 4/8的5次warmup与30次measured，但所有sample必须保持`production_acceptance_eligible=false`；
- 每个measured sample先通过ordered semantic content、terminal projection、physical settlement balance和accounted writer path四项grader，再接纳性能数据；
- 输出为sample JSONL及同名`.summary.json`，必须保存case/manifest/build/PostgreSQL/runtime-capacity identity、raw vector hash，以及每case的median、nearest-rank p95、min/max；
- `semantic_commit_port_wall_seconds`是caller等待完整production commit port的墙钟时间，不冒充纯PostgreSQL时间；
- `logical_semantic_batch_count`与`logical_model_commit_count`是commit-port调用数，不冒充物理transaction数；
- `postgres_cluster_wal_lsn_delta_bytes`只作diagnostic，不进入acceptance。

长时间context baseline必须使用trajectory级operational progress：

- 每条trajectory只在计时区间外写一条`START`与一条`PASS`或`FAIL`；
- progress只进入stderr和可选外部JSONL，不进入EventLog、manifest、provider payload或semantic fingerprint；
- 日志保存scenario、mode、phase、iteration、累计进度、trajectory wall与ETA；
- failure只保存稳定异常类型与bounded reason code，不写raw exception正文。

每个measured context sample通过grader后必须立即fsync到
`<output-stem>.inprogress.jsonl`，并原子更新progress manifest。只有expected row count、连续sample ordinal和raw-vector hash全部通过后，
才能将journal原子rename为最终JSONL并发布summary。失败journal只供检查，不授权正式baseline自动resume。

六个context scenario使用串行`benchmark-context-suite`统一编排：

- 默认覆盖14个mode/case、340条trajectory；各scenario保留自己的warmup/measured contract，不得用统一`14 × 23`替代真实展开；
- suite output directory必须位于Git worktree之外；
- 正式运行在创建输出前检查`git.dirty=false`；
- 每个scenario拥有独立结果journal，suite拥有独立progress log与suite journal；
- 全部scenario完成后才发布`context-suite.summary.json`；
- 完成后再将整个suite目录移动到仓库baseline目录并单独提交。

2026-07-16的单次diagnostic wiring结果不是正式baseline，但证明adapter已走通真实：

```text
LLMRuntime
→ RuntimeSessionModelStreamEventCommitPort
→ RuntimeEventWriteService
→ PostgreSQL EventLog/materialization account
→ terminal projection/rollout settlement
```

同一8192-delta workload的观测为：

| case | logical semantic batches | model stream wall | semantic commit-port wall | ledger events | cluster WAL LSN delta |
|---|---:|---:|---:|---:|---:|
| batch-4 | 2050 | 32.124s | 29.347s | 10253 | 44,182,704 |
| batch-8 | 1026 | 27.195s | 24.474s | 9229 | 37,470,424 |
| batch-16 | 514 | 26.616s | 23.020s | 8717 | 33,748,896 |

三组结果的ordered semantic content与terminal projection semantic identity完全一致；每组physical settlement分别通过自身余额闭合验证，且materialization account high-water与ledger high-water一致。physical charged/bookkeeping成本允许随batch schedule变化，并作为解释性metric输出。该结果只用于证明fixture/grader有辨识力，并支持“先优化batching/固定commit成本”的优先级；正式决策仍以完整20–50次baseline为准。

#### 4.5.2 正式 Writer Baseline（2026-07-16）

正式production-valid writer baseline位于：

```text
benchmarks/durable-runtime/baselines/v1/
├── model-semantic-batch-16-a2ae6726.jsonl
└── model-semantic-batch-16-a2ae6726.jsonl.summary.json
```

环境与验收身份：

```text
Git commit                  a2ae672691818ea7fe8f164f90e83df842154e73
Git dirty                   false
PostgreSQL                  17.9 (Homebrew)
Python                      CPython 3.12.12 / arm64
warmup / measured           5 / 30
measurement contract        adhered
production acceptance       passed
```

同一`8192`个sanitized semantic source items、唯一production-valid `batch-16`的正式结果为：

| metric | median | p95 nearest-rank | min | max |
|---|---:|---:|---:|---:|
| average semantic batch size | 15.942 events | 15.942 | 15.880 | 15.942 |
| logical semantic batch count | 514 | 515 | 514 | 516 |
| logical model commit count | 516 | 517 | 516 | 518 |
| ledger event delta | 8,717 | 8,718 | 8,717 | 8,719 |
| ledger events / 1,000 source items | 1,063.827 | 1,063.949 | 1,063.827 | 1,064.071 |
| model stream wall | 24.248s | 69.482s | 19.084s | 99.337s |
| semantic commit-port wall | 20.907s | 65.010s | 16.497s | 93.175s |
| writer seconds / 1,000 source items | 2.552s | 7.934s | 2.013s | 11.371s |

必须从该baseline冻结以下判断：

1. **当前deterministic workload已经把16-event production hard bound基本填满。**
   `15.942 / 16`约为`99.6%`利用率。对这类连续stream，仅调整`25ms`age或让producer“更耐心等待”不会显著减少logical semantic batch count；`514`已经接近当前event-count contract下的理论下界。
2. **writer仍位于同步model-stream critical path。**
   median semantic commit-port wall约占median model-stream wall的`86%`。该比例只说明caller等待完整commit port的墙钟成本，不能拆成纯PostgreSQL、account CAS、reducer或publication比例。
3. **尾部抖动是真实且很大，但现有baseline不能归因。**
   commit-port p95约为median的`3.1×`，最大值约为`4.5×`。在PERF0分段指标完成前，禁止把该尾部简单归因于WAL、连接、锁、clone database或Python调度中的任一项。
4. **该结果不授权提高production hard bound或coalesce durable facts。**
   若要让logical batch数下降至少`40%`，必须明确改变source event fragmentation、production hard limit或durable event schema；这些都不是“低风险参数调优”。writer-side多个logical batches合并为一次physical transaction可以降低数据库固定成本，但不会改变本表中的logical count，且需要独立physical-operation指标证明。
5. **PERF1A的主要真实轨迹收益仍需由provider interarrival profile决定。**
   本fixture证明full-batch路径的固定成本和正确性，不代表真实provider stream总能形成full batch。只有真实stream显示大量underfilled batch时，adaptive age/structural grouping才可能显著减少logical commits。

因此，该正式baseline支持的优先级不是“先盲目增大batch”，而是：

```text
PERF0 writer内部固定成本分段
    ->
PERF1A 只优化真实underfilled/structural flush
    ->
PERF1B PostgreSQL/account固定成本
    ->
重新测量后再决定是否需要writer-side micro-coalescing或PERF1C
```

#### 4.5.3 正式 Context Suite Baseline（2026-07-17）

完整正式context baseline位于：

```text
benchmarks/durable-runtime/baselines/v1/context-suite-7e9a484d/
```

它绑定clean Git commit `7e9a484d8f2e52545e0c9ae76d5083b24d5c723c`，串行执行：

```text
14 mode/case
340 total trajectories
300 measured samples
6 / 6 scenario production acceptance passed
0 failed trajectory
total suite wall ≈ 12,612s（约3小时30分钟）
```

各场景`context_prepare_total_wall_seconds`是**一条完整trajectory内多个compile point的累计值**，不得误读为单次compile latency：

| scenario | process-cold median / p95 | steady median / p95 | steady improvement |
|---|---:|---:|---:|
| artifact-heavy-tools | 13.846s / 14.477s | 12.474s / 12.637s | 9.9% |
| incremental-active-window | 35.212s / 35.455s | 24.698s / 24.913s | 29.9% |
| long-plan-prefix-growth | 21.311s / 21.522s | 14.551s / 14.743s | 31.7% |
| single-long-compaction | 2.642s / 2.699s | 1.979s / 2.065s | 25.1% |
| subagent-two-children | 11.190s / 11.269s | 8.070s / 8.195s | 27.9% |

额外的checkpoint与artifact模式结果为：

| case | total median | p95 | 解释 |
|---|---:|---:|---|
| checkpoint preferred hit | 4.117s | 4.211s | 正常checkpoint恢复路径 |
| checkpoint process-cold/no-cache | 4.108s | 4.233s | 与preferred hit近似，差异落在噪声内 |
| checkpoint preferred missing/rebase | 7.079s | 7.187s | 比preferred hit高约71.9%，rebase是明确固定成本 |
| artifact verified-cache warm | 12.254s | 12.663s | 仅比普通steady-state再快约1.8% |

必须从该baseline冻结以下判断：

1. **pure compiler不是Stage 4.5的主要瓶颈。**
   各case的`pure_context_compile_total_wall_seconds` median仅为`0.015s–0.215s`，约占累计context preparation的`0.2%–1.7%`。不得用AST lowering、DTO构造或pure allocation微优化替代authority/evidence I/O治理。
2. **session-owned增量状态已经有效，但仍未消除重复preparation成本。**
   steady-state在长前缀、active window、compaction和subagent场景中比process-cold快约`25%–32%`；这证明现有cache/store有价值，同时也说明steady-state仍保留可观的数据库/evidence成本。
3. **PERF2A应先于PERF2B。**
   verified artifact cache相对普通steady-state仅额外改善约`1.8%`；而`incremental-active-window`与`long-plan-prefix-growth`在steady-state仍累计消耗`24.698s`与`14.551s`。首要目标仍是verified projection cursor和new-delta evidence read，artifact hydration cache是后续增益。
4. **rebase应保持bounded且可观测，但不是普通热路径优化的替代目标。**
   missing/rebase比preferred hit高约`71.9%`，但p95仅比median高约`1.5%`，说明算法稳定而成本明确。除非production profile证明rebase频繁发生，否则不应优先牺牲checkpoint correctness换取该冷路径加速。
5. **这份context baseline具有良好的重复性。**
   14个case的p95通常只比median高`0.7%–4.6%`。它适合作为优化前后end-to-end acceptance基线，和writer baseline的高尾部形成鲜明对照。
6. **当前suite不能单独归因evidence、artifact、PostgreSQL与CPU份额。**
   它记录trajectory aggregate、per-point mean/max和pure compile total，但不记录每个substage分段。`transcript-projection-evidence-read`下降比例仍必须由PERF0 typed metrics证明，不能仅从end-to-end改善反推。

与PERF2目标的直接关系：

- `long-plan-prefix-growth` steady-state `context_prepare_mean_wall_seconds` median为约`0.766s / model step`；
- `incremental-active-window` steady-state对应约`0.772s / model step`；
- 两者都略高于本规格冻结的`< 0.75s`目标，因此目标仍有辨识力，但优化不得以放宽authority、replay或manifest invariant换取；
- 优化后必须复用相同scenario/case fingerprint纪律重新跑完整suite，逐case比较raw sample vector、median、p95与semantic grader。

### 4.6 PERF0完成条件

- 不再依赖临时pytest plugin获取上述分段；
- metrics关闭时不改变EventLog、manifest或provider payload；
- metrics开启时同一test的durable fingerprints完全一致；
- deterministic fixtures可以重复生成稳定median/p95、range与归一化writer指标；
- 三条dogfood均输出machine-readable profile artifact；
- dogfood只报告三次median/range，不伪装成稳定p95；
- profile可区分queue、PostgreSQL、account/reducer与publication成本。

---

## 5. PERF1：Semantic Batching、PostgreSQL 与条件性 Write-Behind

### 5.1 当前等待拓扑

当前`flush_semantic_events()`直接：

```text
await commit_port.commit_semantic(...)
```

commit完成后才：

- 将confirmed events交给terminal projection reducer；
- 推进`semantic_item_count`；
- 推进`last_semantic_event_id`；
- 清空pending batch；
- 继续provider loop。

虽然`RuntimeEventWriteService`已经使用critical ledger executor，model worker仍同步等待其Future，因此“DB在线程池”并不等于“model stream
不等待DB”。

现有timer路径可以产生有限重叠：

```text
next read_task pending
    +
25ms flush timer fires
    ->
commit while read_task remains alive
```

但size/char/structural flush会在下一次read task创建前等待commit。这解释了Long Plan中只有`7.3s` provider/durable overlap。

### 5.2 Structural grouping

Semantic block boundary与durable transaction boundary不是同一概念。V1冻结：

- Text/Thinking/Data/ToolCall `Start` 可以等待后续delta，不单独flush；
- `End` 与该block前面尚未提交的delta同批，然后flush；
- `ToolCallEnd`只表示参数流闭合，不是tool execution permit，不要求独立transaction；
- 无delta的`Start -> End`允许同批提交；
- Provider error、cancel和provider terminal必须形成drain/commit barrier；
- `ModelCallStart`、terminal projection、`ModelCallEnd`、control disposition继续保持原有同步边界；
- tool execution仍只能发生在terminal FULL、materialized result FULL与accepted disposition FULL之后。

该分组只改变transaction边界，不改变：

```text
event type
stable event ID
event payload
canonical event order
terminal projection
replay result
```

### 5.3 Batching hard bounds

当前physical burst contract继续是绝对上界。Batching controller只能在上界内选择target：

```text
target_batch_events <= max_batch_events
target_batch_utf8_bytes <= max_batch_payload_bytes
target_batch_age <= max_batch_age
```

Batch candidate必须在进入writer前冻结：

```text
resolved_model_call_id
model_call_start_event_id
first/last transport sequence index
expected previous semantic event ID
stable event candidates
source item count
semantic UTF-8 bytes
canonical envelope byte estimate
batch fingerprint
flush reason
```

canonical pre-commit charge validation与事务内真实stored-envelope校验保持不变。

### 5.4 Flush matrix

#### Block Start

- 不单独flush；
- 等待delta、对应End、target age或hard bound。

#### Block End / ToolCallEnd

- 与前面的pending delta同批；
- 形成一次flush；
- 不产生tool execution permit。

#### Hard max events/bytes

- 立即冻结当前batch；
- 同步模式等待FULL后继续provider read；
- 不改变下一batch expected previous semantic event identity。

#### Target age

- 由单一timer主动唤醒；
- 不因每个25ms tick创建多个commit owner；
- age从batch内最早尚未FULL的semantic event计算。

#### Provider error / cancellation / terminal

- 先flush并确认此前semantic prefix；
- provider error与cancelled/runtime_error结果仅供审计和UI，不进入成功tool/reply控制路径；
- terminal lifecycle不得越过`NONE/UNKNOWN/PARTIAL`。

### 5.5 不直接写死一个更大的25ms

简单把：

```text
25ms -> 100ms
```

可能降低transaction数量，但会增加：

- UI流式可见延迟；
- cancellation前尚未durable的semantic tail；
- worker crash后的recovery delta；
- terminal等待；
- 单batch physical burst。

因此需要基于contract的adaptive batching，而不是只调一个常量。

### 5.6 自适应batching算法

每个`ModelStreamExecutionHandle`维护process-local batching controller：

```text
target_batch_events
target_batch_utf8_bytes
target_batch_age
recent_commit_latency_ewma
recent_provider_interarrival_ewma
pending_source_items
pending_utf8_bytes
```

Hard bounds仍来自versioned physical burst contract：

```text
events <= max_batch_events
UTF-8 bytes <= max_batch_payload_bytes
age <= max_batch_age
```

Controller只在hard bounds以内选择更合适的flush点。

建议规则：

1. Start不单独flush，End/ToolCallEnd与前面的delta同批后flush；
2. provider error、terminal与cancellation前必须flush；
3. pending batch达到hard event/byte bound立即flush；
4. provider item密集且最近commit latency高时，在hard bound内增大target events/bytes；
5. provider item稀疏或UI latency接近上界时，按age flush；
6. writer queue已有积压时，不为每个25ms tick继续创建独立commit owner；
7. 同步batching阶段同一个call始终只有一个pending semantic commit；
8. controller状态不durable，不进入fingerprint；reopen仍只从confirmed semantic cursor恢复。

### 5.7 PERF1B可选：Writer侧micro-coalescing

RuntimeEventWriteService可以在物理开始前识别：

```text
same runtime session
same model call reservation
contiguous semantic cursor
compatible deadline
no structural/terminal boundary
```

的相邻queued semantic operations，并合并为一次physical transaction。

但每个logical operation仍必须获得自己的typed result，且：

- 任一candidate conflict不得污染其他logical operation；
- stable ID/payload不变；
- account transition必须能一次覆盖全部business events；
- reducer按canonical sequence一次fold；
- publication保持event sequence order；
- caller cancellation不取消shared physical owner。

若实现复杂度超过LLMRuntime侧adaptive batching收益，则V1不做writer-side coalescing。

### 5.8 PERF1B：PostgreSQL fixed-cost优化

在PERF0确认瓶颈后，按顺序检查：

1. pool lease是否等待；
2. session advisory lock是否等待；
3. materialization account row CAS是否占主导；
4. parent identity cache是否真实命中；
5. batch insert是否仍有per-event SQL；
6. run projection update是否可batch；
7. account/business events是否在一个round trip中写入；
8. transaction commit/WAL fsync是否占主导。

不允许：

- 关闭fsync或使用不durable transaction作为生产默认；
- 绕过account row CAS；
- 将publication success误当作commit success；
- 将多个session塞入同一无deadline巨型transaction。

### 5.9 PERF1A/PERF1B目标

在不改变source item数量、durable event数量和semantic fingerprint的前提下：

- terminal/control/tool execution仍只消费FULL committed semantic prefix；
- production-valid saturated `batch-16` fixture保持average batch utilization约`99%`，logical semantic batch median不得高于正式baseline `514`，p95不得高于`515`；
- PERF0必须另行冻结带真实interarrival/structural boundary的underfilled deterministic stream fixture；只有该fixture的baseline确认存在可合并空隙时，其`logical semantic batches / 1,000 source items`才要求至少下降40%；
- Long Plan `durable writer waits / model call`至少下降40%；
- Long compaction只有在PERF0证明semantic batch显著underfilled时，才要求`logical semantic batches / 1,000 source items`至少下降40%；否则改用physical writer operations、commit-port wall与durable exclusive作为验收指标；
- deterministic PostgreSQL fixture的writer p95 queue wait与commit p95不恶化；
- saturated `batch-16`的`semantic_commit_port_wall_seconds` median/p95必须低于正式baseline `20.907s / 65.010s`；PERF1R在看到分段数据后冻结最小改善幅度，禁止用provider stream随机波动代替writer改善；
- first-item与UI semantic latency不超过contract上界；
- cancellation/recovery/UNKNOWN测试保持全绿；
- dogfood durable exclusive三次median应显著下降，但不以provider随机变快作为通过依据。

Long Plan当前provider与durable联合关键路径近似为：

```text
56.5s provider union
+ 75.2s durable union
-  7.3s existing overlap
=124.4s
```

理想完全重叠的理论下界为`max(56.5s, 75.2s)=75.2s`，即最多可隐藏约`49s`。这只是后续write-behind的理论headroom，
不是PERF1A/PERF1B的目标。

这些是初始工程目标，不是永久产品常量；PERF0基线可在实施前调整具体阈值，但不得删除量化验收。

### 5.10 Re-measure gate

PERF1A batching与PERF1B PostgreSQL fixed-cost优化完成后，必须先重新运行确定性fixture与三条real-LLM dogfood。

只有在以下判断成立时才进入PERF1C：

```text
logical batch amplification已显著下降
    &&
单次durable commit latency仍串行进入model stream关键路径
    &&
provider read与persistence存在可观测重叠空间
    &&
hard-bound queue不会突破physical reservation、UI latency或terminal drain contract
```

至少比较：

```text
durable critical-path share
logical semantic batches / 1,000 source items
commit p50/p95
provider interarrival p50/p95
first-visible semantic latency
estimated overlap headroom
```

若剩余成本主要仍是transaction数量或单batch SQL放大，则继续优化batching/SQL，不得用write-behind隐藏它。

Gate必须输出machine-readable、非durable、secret-safe report：

```text
baseline_profile_id
candidate_profile_id
fixture_contract_fingerprint
logical_semantic_batches_per_1k_source_items
commit_latency_p50/p95
provider_interarrival_p50/p95
durable_critical_path_seconds
estimated_overlap_headroom_seconds
first_visible_latency_p95
decision = implement_write_behind | skip_write_behind
bounded_reason_codes
```

该report不进入EventLog、manifest或provider fingerprint，但作为PERF1C PR是否存在的审计依据。

### 5.11 PERF1C：`ModelSemanticPersistencePipeline`

只有通过re-measure gate才新增process-local、service-owned handle：

```python
class ModelSemanticPersistencePipeline:
    async def enqueue(
        self,
        batch: FrozenModelSemanticBatch,
    ) -> None: ...

    async def seal_and_drain(self) -> ConfirmedSemanticPrefix: ...

    async def request_cancel(self, *, reason: str) -> None: ...

    async def wait_physical_completion(self) -> SemanticPersistenceOutcome: ...
```

Pipeline由`ModelStreamExecutionHandle/Registry`拥有，不由provider subscriber或Agent caller拥有。状态机：

```text
ACCEPTING
    -> SEALED
    -> DRAINING
    -> DRAINED_FULL

ACCEPTING/SEALED/DRAINING
    -> RECONCILIATION_REQUIRED

ACCEPTING
    -> CANCEL_REQUESTED
    -> DRAINING
```

物理拓扑严格限制为：

```text
one active RuntimeEventWriteService operation
    +
one bounded accumulation buffer
```

不得将大量logical batches预先塞入session writer FIFO。

### 5.12 Write-behind hard bounds

至少冻结：

```text
max_pending_events
max_pending_payload_bytes
max_oldest_unconfirmed_age
max_provider_ahead_seconds
max_terminal_drain_seconds
```

并要求：

```text
uncommitted usage
    <= active model physical reservation remaining capacity
    <= model physical burst contract
```

provider可在一个commit in-flight期间继续填充唯一bounded accumulation buffer。达到任一pending上限后暂停provider
`read_next()`；解除backpressure只依赖前一batch durable `FULL`与reducer fold，不等待publisher/observer。

Commit矩阵：

- `FULL`：fold reducer、推进confirmed cursor、释放buffer；
- `NONE`：保留同一candidate、bounded retry，不允许后续batch越过；
- 持续`NONE`：终止provider read，将candidate提升给session-owned persistence retry owner；
- `UNKNOWN/PARTIAL`：latch、保留physical owner，禁止terminal/control；
- provider terminal：保存terminal draft，seal并FULL drain后才提交terminal batch；
- explicit cancellation：provider physical operation与persistence owner分别drain；
- process crash：未commit buffer不是authority，按Start-without-End recovery处理。

### 5.13 PERF1C目标

- provider/persistence overlap在确定性fixture中可重复测得；
- Long Plan provider/durable overlap三次median至少增加30s，或由PERF0重新冻结等价归一化目标；
- durable exclusive三次median至少下降25%，且不是provider随机差异；
- bounded backlog、terminal drain、NONE/UNKNOWN/PARTIAL与close测试全绿；
- 关闭PERF1C后，ordered durable events、terminal projection、disposition和provider payload完全相同。

---

## 6. PERF2：Verified Transcript Projection Cursor

> 详细实施规格：
> `PULSARA_CONTEXT_EVIDENCE_CURSOR_PERFORMANCE_OPTIMIZATION_IMPLEMENTATION.zh.md`
>
> 本节保留阶段目标与性能口径；Cursor DTO、owner、same-high-water/delta-extension算法、anchor CAS、fallback、测试与
> Definition of Done 以详细实施规格为唯一权威。

### 6.1 当前重复工作

当前`prepare_projection_evidence(requested_through_sequence=H)`会：

1. 读取active seed/checkpoint base；
2. 从base sequence读取transcript-domain delta到`H`；
3. 校验prefix semantic event count/accumulator；
4. 与process-local stable state join；
5. 必要时执行exact restore；
6. hydrate stable entries与terminal documents。

连续model steps常形成：

```text
base -> H1
base -> H2
base -> H3
...
```

即使`H2`只比`H1`多很小的delta。

当前incremental `transcript_projection_state_store`虽然已经跟随committed events推进，但compiler不能只相信process-local store自报状态。
`prepare_projection_evidence()`还需要证明：

```text
canonical ledger prefix
    ->
supported transcript-domain semantic events
    ->
same count/accumulator
    ->
same stable state
```

问题不在“为什么要证明”，而在“为什么每轮从base重新证明全部prefix”。

### 6.2 当前读取复杂度

一次`read_transcript_domain_delta(base, H)`包含：

```text
high-water query
before prefix query
after prefix query
transcript-domain range query
row -> canonical envelope
payload byte accounting
event decode
semantic accumulator replay
```

连续model steps的总成本近似：

```text
sum(size(base..Hi), i=1..model_step_count)
```

而目标成本应为：

```text
size(base..H1)
    +
sum(size(H(i-1)..Hi), i=2..model_step_count)
```

即：

```text
O(total new transcript-domain events)
```

而不是：

```text
O(repeated historical prefixes)
```

`context_authority_slice_cache`已经让primary authority bundle读取具备增量行为，因此Long Plan中
`context-live-authority-read`只有约`2.2s`。同样的verified incremental原则尚未应用到projection evidence，才造成`19.0s`
的单项成本。

### 6.3 `VerifiedTranscriptProjectionCursor`

新增session-owned：

```python
class VerifiedTranscriptProjectionCursor:
    ...
```

Cursor至少包含：

```text
runtime_session_id
stable projection anchor identity
anchor carrier stable identity + available-from sequence
current projection base fact
event_domain_registry_contract_fingerprint
reducer_contract_fingerprint
verified_through_sequence
prefix_semantic_event_count
prefix_semantic_accumulator
prefix_ledger_continuity_accumulator
semantic_envelope_chunk_vector
stable_state_fingerprint
cursor_fingerprint
```

`semantic_envelope_chunk_vector`保存完整、按canonical顺序排列的semantic delta envelopes；它是process-local、immutable、
structurally-shared persistent data structure，不是新的durable artifact或authority。

每个identity至少保存：

```text
runtime_session_id
sequence
event_id
event_type
event_schema_version
event_schema_fingerprint
payload_fingerprint
envelope_fingerprint
```

它必须足以原样重建当前`transcript_domain_delta_refs`与prepared semantic-envelope fingerprint序列，不能只保存event ID或单一prefix
accumulator。

Cursor不得成为以下对象的第二owner：

- stable transcript entries；
- terminal projection documents；
- artifact content registry；
- complete normalized transcript；
- manifest payload。

这些仍由既有`TranscriptProjectionStateStore`与document/artifact registry拥有。Cursor只证明durable prefix并保存构造完整authority
所必需的canonical envelope chunks；V1保留完整`RawStoredEventEnvelope`，以保持现有authority/proof/manifest schema逐字段不变。

Cursor key不得只使用`run_id`或`window_id`。

Run seed与checkpoint anchor必须绑定完整committed carrier identity和`anchor_available_from_sequence`；checkpoint还绑定candidate fingerprint。
`base_sequence <= requested H`不代表anchor已durable可用，future carrier不得回答过去high-water。

Cursor绑定完整event-domain registry fingerprint。Stage 5纯ContextSource/candidate改造不要求Cursor schema迁移。若Stage 5新增event或改变registry，
Cursor本身仍无需迁移，但旧durable seed/checkpoint与current registry不兼容，不能靠discard + exact restore跨越。本项目开发期明确关闭旧session并reset
PostgreSQL；未来如需保留ledger，另立registry/schema migration hard cut。Cursor guard不得反向禁止合理event演进。

### 6.4 唯一 reducer 与原子 evidence snapshot

`TranscriptProjectionStateStore`继续是唯一 committed transcript reducer。Cursor不得跟随每个 committed batch私自fold第二份stable/live
state；它只在context evidence准备时，以已经由EventLog证明的delta与live reducer的exact high-water snapshot做join。

State store新增单锁`evidence_snapshot()`，一次冻结：

```text
live assembly state
stable entries
stable entries required terminal projection refs
```

不允许先读Cursor、再从不同high-water分别读取`state_store.snapshot()`与`stable_entries()`。Cursor advance使用anchor generation CAS；
checkpoint/run-seed adoption可以使在途candidate失效，但不等待Cursor I/O。

Anchor linearization使用独立同步`threading.RLock`，原子覆盖generation、carrier-bound anchor、Cursor、reachable artifacts、latest checkpoint与
active context；现有async lock只保护checkpoint owner lifecycle。

Factory首次构造执行完整vector/proof深验；production same-H与delta extension只执行private construction guard、outer root fingerprint、active
anchor/contract/reducer high-water的`O(1)` fast validation。完整event-ID fingerprint与authority refs合并为一次必要的prefix traversal，不能为cache
validation额外再遍历old chunks。

### 6.5 复用现有 bounded sparse read

不新增same-high-water PostgreSQL round trip。Cursor首次由startup/exact restore证明；ledger append-only且live reducer仍位于同一H时，same-H可直接
复用。

增量路径继续调用现有：

```python
read_transcript_domain_delta(
    after_sequence=cursor.verified_through_sequence,
    through_sequence=requested_through_sequence,
    ...,
)
```

新增proof composition helper只接受factory签发的`ValidatedCursorUseToken`，不得平行接收base/proof/vector。它只historical-decode新delta；为保持
现有durable proof schema，完整event-ID fingerprint与最终authority refs仍是`O(active prefix)`，但必须共享同一次traversal。

### 6.6 增量扩展

请求`Hnew`时：

#### Cursor exact hit

```text
cursor.verified_through_sequence == Hnew
```

若live reducer的单锁evidence snapshot也精确位于`Hnew`，不执行PostgreSQL read；直接比较Cursor prefix、semantic source与reducer snapshot，
再使用Cursor中的完整canonical envelope chunks组装authority。

#### Cursor incremental hit

```text
cursor.verified_through_sequence < Hnew
```

只读取：

```text
(cursor.verified_through_sequence, Hnew]
```

并验证：

- delta.before与cursor prefix count/accumulator完全一致；
- delta ledger continuity before与cursor continuity一致；
- delta.after count/accumulator/continuity与RuntimeSession committed reducer snapshot一致；
- 新delta envelopes追加到chunk vector，不重建旧chunk；
- 只深验new chunks，从authenticated persistent vector root组合next root；
- named terminal document refs可按exact IDs补充hydrate。

#### Cursor落后、倒退与base mismatch

以下任一发生时discard cursor并走existing exact restore：

- run seed改变；
- checkpoint/rebase改变；
- event-domain/reducer contract改变；
- semantic accumulator不连续；
- cursor payload损坏；
- requested high-water倒退。

CAS失败后不能只比较new base sequence。新anchor的committed carrier sequence必须`<= requested H`；否则必须使用historical one-shot restore，
防止未来RunStart/checkpoint carrier回答过去high-water。

上述均为cursor-local mismatch：先discard并执行exact restore，不直接latch。只有canonical restore本身无法证明连续性，或canonical reducer仍不一致，
才fail closed。

若mismatch来自durable event/schema registry变化，old seed/checkpoint exact restore也必须拒绝；Stage 5按开发期hard cut执行DB reset，不能把它降级成
普通Cursor miss。

### 6.7 Boundedness与复杂度口径

Cursor owner必须：

- 只保存单个active bounded projection base；
- chunk具有固定最大identity count与byte bound；
- 通过immutable chunk结构共享旧prefix；
- run-seed/checkpoint base或registry/reducer contract变化时明确retire旧cursor；
- close时可直接丢弃；
- cursor eviction不影响replay或checkpointability。

此外新增全进程唯一`CursorResidentBudgetManager`，默认限制：

```text
max resident charge bytes = 512 MiB
max resident chunks       = 4,096
max resident cursors      = 64
```

发布Cursor前必须resident admission。超限时按zero-borrow LRU淘汰，无法admit则继续返回本次canonical exact evidence但不缓存；不得fail closed、
latch ledger或触发compaction。RuntimeSession/child/detached session不得各自创建budget manager。Charge覆盖payload、所有envelope identity UTF-8 bytes与
conservative object reserves；详细算法以Cursor实施规格为准。

Composition-root doctor必须证明default process budget至少可admit一个single-ledger maximal legal Cursor；physical limit或支持的Python runtime变化时
重新校准resident charge fixture。

V1只承诺：

```text
database range read + event decode = O(new transcript-domain delta)
```

V1不承诺：

```text
entire compile = O(new delta)
```

因为最终authority DTO、完整refs tuple、fingerprint与manifest serialization仍是`O(active prefix)`。端到端增量化需要Merkle/range-proof
durable schema hard cut，不属于Stage 4.5。

### 6.8 Verified artifact hydration cache

新增或复用bounded verified-content cache：

```text
(artifact_id, sha256, size, media_type, codec contract)
    -> immutable decoded content/document
```

适用：

- terminal projection content；
- named model-visible facts；
- compaction summary/source document；
- checkpoint tree nodes/pages；
- context manifest read-confirm。

禁止只按artifact ID缓存未验证正文。

### 6.9 可并行的context准备

在authority high-water、active window与projection base冻结后，以下操作可按依赖图并行，而不是全部串行await：

```text
projection cursor new-delta read
subagent graph checkpoint restore
compaction summary/source artifact read
已知exact named artifact hydration
```

但并行化只能发生在：

- 它们消费同一frozen high-water；
- 彼此不依赖对方输出；
- 任一失败会取消尚未开始的辅助I/O；
- 已开始的physical I/O仍由ContextInputIoService负责bounded drain；
- 最终snapshot builder在单点执行全部cross-fact join。

以下仍具有依赖：

```text
subagent graph restore
    -> source selection
    -> selected result exact reads

projection cursor / stable state snapshot
    -> required terminal content refs
    -> terminal artifact hydration
    -> normalized transcript

normalized transcript
    -> tool result render input
    -> candidate collection
```

不得为了并行化而在多个任务中复制snapshot truth。

### 6.10 PERF2目标

- 连续compile只读取new transcript-domain delta；
- Long Plan的
  `transcript-projection-evidence-read` median从约19s下降至少70%；
- Long Plan `context prepare / model step` median低于0.75s；
- 同一frozen base的cursor/exact restore生成严格相同proof/delta identities、materialization-equivalent stable content与相同Context Input Manifest fingerprint；
- cursor与stable state必须在同一high-water原子快照；
- 删除cursor后exact replay结果不变；
- cursor-local corruption、contract drift、high-water rollback均discard并exact restore；canonical restore失败才fail closed；
- process resident budget始终有界，admission reject/eviction不改变canonical结果；
- normal hit不深验old chunks，full-ID fingerprint与authority refs只有一次prefix traversal；
- context evidence read bytes接近`O(new delta + newly referenced artifacts)`，不再接近`O(base..H)`。
- 不把最终authority DTO构建或manifest serialization误报为`O(new delta)`。

---

## 7. PERF3：Stage-4-Owned Prepared Context 复用

PERF2完成后，再优化以下CPU与artifact路径。

本阶段只允许复用Stage 4已经冻结、且不会被Stage 5 ContextSource Ownership Hard Cut重新定义的输入。禁止缓存：

- 旧ContextSource collector输出；
- 尚未hard-cut的source registry决定；
- 旧source ownership facade；
- current `AgentRuntime` context拼接顺序；
- 任何以mutable producer状态为key的prepared context。

### 7.1 Provider projection

Invocation timing是每次compile动态事实，因此不能缓存跨invocation的最终timing header；但可以复用：

- durable stable transcript semantic；
- transcript message/block placement basis；
- transcript lowering lane；
- timing source attribution；
- normalized content hydration；
- token estimate中不依赖`compiled_at`的静态部分。

最终：

```text
stable prepared basis
    + invocation compiled_at/timing overlay
    -> PreparedTranscriptProviderProjectionFact
```

### 7.2 Manifest canonical representation

同一compile attempt应只生成一次：

```text
canonical manifest payload
manifest fingerprint
artifact bytes
payload byte count
```

builder、Pydantic validator和artifact writer不得各自完整canonicalize大型manifest。

Trusted live factory可复用已验证canonical representation；replay/untrusted ingress仍独立完整验证。

### 7.3 Rollup

Prepared rollup cache key不得包含完整transcript fingerprint。应绑定：

```text
durable rollup fingerprint
member unit fingerprints
placement anchor/basis fingerprint
render policy
estimator fingerprint
carrier contract fingerprint
```

不相关的transcript append不应让active rollup重新renderer/materialize。

### 7.4 PERF3目标

- provider projection静态部分跨model step有稳定cache hit；
- dynamic timing仍按本次compiled_at正确重算；
- manifest大型fixture每次compile只做一次完整canonical serialization；
- active rollup不因unrelated append失效；
- Stage 5删除旧ContextSource ownership时不需要迁移或兼容PERF3 cache schema；
- exact provider payload与当前实现byte-for-byte一致。

---

## 8. 明确不做

Stage 4.5不做：

- 将ModelStart、terminal、control disposition、tool gate/result或RunEnd改成fire-and-forget；
- 让provider terminal越过尚未FULL确认的semantic backlog；
- 用无界process-local queue隐藏数据库吞吐不足；
- 删除EventLog raw semantic events；
- 将checkpoint或cache升级为authority；
- 取消PhysicalOperationChargeAppliedEvent但不提供等价durable accounting；
- 降低stable confirmation或UNKNOWN fail-closed强度；
- 用最终settlement替代stream期间physical headroom accounting；
- 关闭PostgreSQL durability；
- 用更大的线程池掩盖transaction amplification；
- 让context compiler读取mutable LoopState；
- 把Long-Horizon token compaction用于处理event transaction pressure；
- 提前实现Stage 5 ContextSource registry；
- 在PERF1A/PERF1B重新测量前默认实施write-behind；
- 将governance transactional outbox误判为永久split-brain并另造第二事实通道；
- 将projection cursor称为durable authority或声称整个compile已是`O(new delta)`；
- 以单次最快dogfood作为性能完成证据。

---

## 9. 建议PR顺序

### DISP0：Disposition live retry/drain owner

- session-owned stable candidate retry service；
- shared attempt/future与waiter cancellation isolation；
- `FULL/NONE/UNKNOWN/PARTIAL`矩阵；
- safe point join、RunEnd blocker与Host close bounded drain；
- restart recovery identity join；
- 不引入semantic write-behind。

### PERF0：Typed runtime profiling

- writer/context/provider分段metrics；
- deterministic provider stream、PostgreSQL batch与long-ledger compile fixtures；
- 每个fixture运行20–50次并报告median/p95；
- machine-readable dogfood profile与三次median/range；
- `pg_stat_statements`、WAL bytes与`EXPLAIN (ANALYZE, BUFFERS, WAL)`；
- metrics关闭/开启semantic equality测试。

### PERF1A：Semantic batching与structural grouping

- flush reason；
- Start等待delta，End与delta同批后flush；
- ToolCallEnd不形成独立transaction或execution permit；
- adaptive target；
- hard burst bounds不变；
- provider error/cancellation/terminal形成barrier；
- first-visible latency与oldest pending age上限；
- 不改durable event schema。

### PERF1B：Writer/PostgreSQL fixed-cost reduction

- 根据PERF0数据减少round trip；
- account CAS、event insert、parent projection批处理；
- connection/pool/lock telemetry；
- 可选writer-side compatible queue coalescing。

### PERF1R：Re-measure decision gate

- 重跑deterministic fixture与三条dogfood；
- 判断剩余成本来自transaction amplification还是同步commit latency；
- 冻结是否进入PERF1C的量化结论；
- 若write-behind收益不足，直接跳过PERF1C。

### PERF1C：Conditional one-inflight write-behind

- `ModelSemanticPersistencePipeline`与registry ownership；
- one active commit + one bounded accumulation buffer；
- max events/bytes/age/provider-ahead/terminal-drain hard bounds；
- provider reader/persistence并行；
- terminal seal/drain barrier；
- persistent `NONE`提升到session-owned persistence owner；
- `FULL/NONE/UNKNOWN/PARTIAL/cancellation`矩阵；
- Host close与restart recovery；
- 不改durable event schema。

### PERF2A：Verified transcript projection cursor

- cursor DTO与session owner；
- run-seed/checkpoint carrier identity、available-from sequence与checkpoint candidate fingerprint；
- 唯一validated factory：首次完整深验，normal use执行O(1) fast validation；
- proof composition只接受factory签发的validated token；
- structurally-shared chunked semantic envelope vector；
- process-owned resident bytes/chunks/cursors budget、lease与LRU eviction；
- cursor与stable state同high-water原子join/快照；
- same-high-water零数据库读取与现有bounded sparse delta复用；
- new-delta extension；
- run-seed/checkpoint/rebase/contract invalidation；
- Stage 5 registry变化使用DB reset或独立migration，不由Cursor跨越；
- frozen-base restore与inline/artifact materialization-equivalence；
- exact restore fallback与negative tests。

### PERF2B：Verified artifact hydration cache

- digest/size/media type/codec/owner完整key；
- terminal/named fact/compaction/checkpoint hydration；
- bounded LRU与oversize pre-check；
- cache hit/miss semantic equality。

### PERF3：Stage-4-owned prepared context reuse

- static provider projection basis；
- single canonical manifest representation；
- prepared rollup cache修正；
- token estimate静态部分复用。

### PERF4：Final re-measure and acceptance

- 重跑deterministic fixtures；
- 重跑三条dogfood各3次；
- 全量non-real；
- 全量real-LLM + dogfood；
- PostgreSQL/Inspector/close/recovery故障矩阵；
- 决定是否需要另立durable semantic delta coalescing hard-cut规格；
- 若已达到目标，直接回到Stage 5。

---

## 10. 测试矩阵

名称以`conditional_model_semantic_pipeline`开头的测试只在PERF1R选择实施PERF1C时成为required acceptance；若decision为
`skip_write_behind`，则必须改为验证pipeline生产类型、配置入口与owner没有进入production wiring。

### 10.1 Performance

- `test_disposition_none_live_retry_adopts_same_candidate_winner`
- `test_deterministic_provider_stream_profile_is_reproducible`
- `test_postgres_semantic_batch_benchmark_reports_cluster_wal_diagnostic`
- `test_long_ledger_compile_benchmark_reads_only_new_delta`
- `test_real_long_plan_emits_runtime_performance_profile`
- `test_real_long_compaction_emits_runtime_performance_profile`
- `test_real_subagent_system_emits_runtime_performance_profile`
- `test_semantic_start_waits_and_end_flushes_with_prior_delta`
- `test_tool_call_end_does_not_force_standalone_transaction`
- `test_semantic_batching_reduces_logical_batches_without_changing_events`
- `test_semantic_batching_records_flush_reason_bytes_and_oldest_age`
- `test_conditional_model_semantic_pipeline_overlaps_provider_and_commit`
- `test_conditional_model_semantic_pipeline_applies_bounded_backpressure`
- `test_incremental_evidence_reads_only_new_delta`
- `test_projection_cursor_avoids_repeated_prefix_decode`
- `test_projection_cursor_and_stable_state_freeze_same_high_water`
- `test_manifest_factory_serializes_once_per_compile_attempt`
- `test_prepared_rollup_cache_survives_unrelated_transcript_append`

### 10.2 Semantic equality

- disposition live retry与restart recovery采用同一stable candidate/winner；
- batching on/off产生相同ordered semantic events；
- Start/End grouping改变不影响terminal projection；
- synchronous/conditional-pipelined persistence产生相同ordered semantic events；
- conditional pipeline backlog形状不影响terminal projection；
- provider terminal必须等待confirmed semantic prefix；
- batch size改变不影响terminal projection；
- batch schedule改变不影响control disposition；
- 同一frozen base的cursor/exact restore产生严格相同proof/delta、materialization-equivalent stable content及相同manifest/provider payload；
- artifact cache hit/miss产生相同normalized content；
- provider projection static cache不复用旧invocation timing；
- manifest canonical representation复用不跳过untrusted replay validation。

### 10.3 Failure

- disposition live retry `NONE`保留原stable candidate；
- disposition live retry `UNKNOWN/PARTIAL` latch；
- disposition waiter cancellation只detach owner；
- Host close不得越过pending disposition candidate；
- batching timer只创建一个flush owner；
- semantic batch commit NONE重试原stable candidate；
- semantic batch UNKNOWN保留owner并latch；
- adaptive controller crash不改变recovery；
- conditional pipeline queued NONE保留原stable candidate；
- conditional pipeline UNKNOWN/PARTIAL保留owner并latch；
- conditional pipeline backlog达到hard bound时停止provider read；
- conditional pipeline provider terminal先到达时必须seal/drain；
- conditional pipeline caller detach不取消worker；
- Host close不得越过conditional pipeline或provider physical owner；
- cursor corrupted自动discard，canonical restore成功；
- cursor accumulator mismatch先discard并exact restore；canonical accumulator mismatch才fail closed；
- cursor/stable-state high-water mismatch先discard并exact restore；canonical reducer mismatch才fail closed；
- cache artifact digest mismatch先evict并read-confirm；canonical artifact digest mismatch才fail closed；
- connection断线重连不重复event；
- queue deadline、caller cancellation与Host close保持原语义；
- benchmark metrics sink失败不影响runtime。

### 10.4 Architecture guards

禁止：

- metrics进入event-safe fingerprint；
- cache对象进入ContextFactSnapshot durable truth；
- compile/replay依赖cursor或cache存在；
- new direct PostgreSQL writer绕过RuntimeEventWriteService；
- provider transport自行提交semantic events；
- Agent/subscriber直接拥有conditional semantic persistence task；
- conditional pipeline向session FIFO无界enqueuelogical batches；
- terminal/control读取unconfirmed semantic backlog；
- adaptive batching越过physical burst hard bounds；
- projection cursor按run ID弱定址；
- projection cursor与stable state分别读取不同high-water；
- PERF3缓存旧ContextSource collector/source registry中间结果；
- PERF1R决定`skip_write_behind`后仍把pipeline生产类型或配置入口接入runtime；
- production raw semantic coalescing在无独立schema规格时出现。

---

## 11. Definition of Done

Stage 4.5只有同时满足以下条件才完成。

### 11.1 Correctness

- 全部现有durable/replay/account/checkpoint invariant保持；
- stable IDs、payload fingerprints和provider-visible payload不因优化变化；
- disposition持续`NONE`由session-owned live retry/drain owner收口；
- semantic structural grouping只改变batch边界，不改变event order或terminal semantics；
- 若PERF1C启用，model semantic pipeline由service-owned handle持有，subscriber/caller detach不影响durable收口；
- terminal projection、ModelCallEnd和control disposition只消费FULL confirmed semantic prefix；
- 若PERF1C启用，pending semantic backlog具有events/bytes/age/provider-ahead/terminal-drain hard bound；
- 所有failure/cancellation/recovery/close测试通过；
- cursor/cache可完全删除并从canonical authority恢复；
- cursor与stable transcript state只能以同一high-water原子快照消费。

### 11.2 Performance

- deterministic provider/PostgreSQL/long-ledger fixture各运行20–50次；
- 三条dogfood各3次并报告median/range；
- durable writer fixed-cost有分段证据；
- saturated `batch-16`保持logical count不回归，并降低commit-port median/p95；
- 只有PERF0确认underfilled的deterministic/real-stream fixture，才要求logical semantic batches / 1,000 source items至少下降40%；
- transcript evidence read median至少下降70%；
- Long Plan context prepare / model step median低于0.75s；
- provider first-item与UI streaming latency不越过冻结contract；
- 若PERF1C启用，provider read与semantic persistence具有可重复测得的并行重叠；
- 若PERF1C启用，Long Plan overlap与durable exclusive达到PERF1R冻结的量化目标；
- 没有通过增大无界queue、线程、cache或payload上限伪造吞吐提升。

### 11.3 Operability

- Inspector/metrics能区分provider、writer、account、context与tool时间；
- profile输出bounded、secret-safe；
- production默认metrics开销有明确上限；
- Host close仍能bounded drain全部physical owners；
- PostgreSQL pool、writer queue、cursor与cache均有容量/eviction/timeout观测；
- governance outbox pending count/oldest age作为operational liveness观测，不再误报为split-brain correctness。

---

## 12. 最终建议

下一步建议暂停扩大Stage 5改动面，先实施：

```text
DISP0 disposition persistent-NONE live retry/drain
    ->
PERF0 deterministic typed profiling
    ->
PERF1A semantic batching + structural grouping
    ->
PERF1B writer/PostgreSQL fixed-cost reduction
    ->
PERF1R re-measure gate
    + justified: PERF1C one-inflight write-behind
    + not justified: skip PERF1C
    ->
PERF2A verified transcript projection cursor
    ->
PERF2B verified artifact hydration
    ->
PERF3 Stage-4-owned prepared reuse
```

`PERF1C`是条件分支，不是默认必做步骤。若PERF1A/PERF1B已经把durable critical path压低到目标范围，直接跳到PERF2A。

最终如果：

```text
durable exclusive << provider exclusive
context prepare / model step < 0.75s
saturated batch-16 logical count不回归
underfilled stream logical batches / 1,000 source items达到PERF1R冻结目标
cursor database read/decode接近O(new delta)
```

则停止性能重构，回到Stage 5。

只有当：

```text
batching与SQL fixed-cost已经优化
条件性write-behind已经被明确实施或明确判定无收益
transaction固定成本已经压缩
projection cursor读取已经增量化
durable writer仍是主要墙钟瓶颈
```

才值得另立durable semantic delta coalescing hard cut。

这条顺序避免两种错误：

1. 因为本地durable成本高，就削弱刚完成的authority/accounting正确性；
2. 在没有分段证据前，提前引入新的event schema与historical decoder复杂度。

Stage 4.5的目标不是让Pulsara少记录事实，也不是默认用异步队列隐藏数据库延迟，而是先减少不必要的transaction，再以受控并发隐藏
仍然存在的不可消除latency，最终以更低的物理成本记录同一组正确事实。
