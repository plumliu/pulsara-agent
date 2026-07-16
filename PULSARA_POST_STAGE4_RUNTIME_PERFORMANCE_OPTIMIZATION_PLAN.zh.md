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
性能稳定化**：

1. 先把 writer 的 queue、PostgreSQL commit、account CAS、reducer fold 与 publication 分段测量；
2. 将 model semantic stream 从“reader逐batch等待commit”改成“bounded write-behind persistence pipeline”；
3. 在pipeline基础上减少 transaction / charge batch amplification；
4. 将 transcript projection evidence 从“每次从 base 重读”改成“verified incremental memoization”；
5. 只在上述三项不足时，才讨论 durable semantic delta coalescing；
6. 所有 queue、cache、batching 与 connection reuse 都不得改变 event payload、stable ID、replay、accounting 或 provider-visible semantic
   identity。

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
- 单次采样不能作为稳定 benchmark，后续 PERF0 必须使用同一轨迹至少三次的 median / p95；
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
- write-behind queue只保存stable frozen candidates，不保存可被后续修改的draft对象；
- confirmed semantic cursor只能由单一persistence owner推进；
- provider reader可以领先confirmed cursor，但不得领先physical reservation与burst hard bound；
- terminal projection必须等待semantic queue seal并FULL drain；
- control disposition仍只能消费confirmed terminal projection；
- provider stream observer仍只是 UI/live observation，不得成为 runtime control owner。

### 2.4 Materialization account

- reservation、charge、settlement与consumer horizon继续使用同一 ledger materialization generation；
- 不允许为了减少 event 数而跳过 physical headroom accounting；
- account row与durable bookkeeping events必须原子一致；
- checkpoint barrier、producer admission与close drain语义不变。

### 2.5 Context exactness

- context cache只能缓存canonical reducer的纯函数结果；
- cache identity必须绑定base identity、through sequence、contract fingerprint与semantic accumulator；
- mismatch时丢弃cache并fail closed或执行现有exact restore；
- cache schedule、hit/miss不得进入provider semantic fingerprint；
- exact replay不得依赖process-local cache存在。

---

## 3. 优化优先级

### 3.1 P0：Model semantic stream write-behind persistence

当前PostgreSQL操作已经由`RuntimeEventWriteService`在线程池中执行，event loop不会直接运行blocking transaction。但model stream仍在每次
flush时：

```text
await commit_semantic(batch)
    ->
commit FULL
    ->
才继续读取下一批provider items
```

年龄定时器触发时，已经启动的next `read_next()`可以与commit部分重叠；但structural、max-events、max-chars触发的flush仍会在开始下一次
provider read前等待durable commit。Long Plan中provider与durable writer只有约`7.3s`重叠，说明现有并行度远未覆盖大部分durable成本。

第一优先应新增service-owned：

```text
ModelSemanticPersistencePipeline
```

让provider reader与durable persistence成为两个受同一`ModelStreamExecutionHandle`拥有的并行owner：

```text
commit ModelStart
        |
        v
provider reader
        |
        +--> frozen stable semantic batches
                  |
                  v
        bounded process-local queue
                  |
                  v
        sequential persistence worker
                  |
                  v
        commit + fold + publication enqueue

provider terminal draft
        |
        v
seal queue -> drain all semantic batches FULL
        |
        v
commit Terminal Projection + ModelCallEnd
        |
        v
resolve Control Disposition
```

这不是fire-and-forget：

- semantic candidates在enqueue前必须具有stable event ID/payload；
- persistence worker严格按transport sequence顺序commit；
- terminal、control、tool execution与final reply仍等待drain barrier；
- queue达到events/bytes/batches hard bound时停止provider read并施加backpressure；
- NONE重试原stable batch；
- FULL推进confirmed cursor和terminal projection reducer；
- UNKNOWN/PARTIAL保留owner、latch ledger并阻止terminal/control；
- caller detach不取消pipeline；
- Host close必须drain provider physical operation与semantic persistence owner。

这项优化改变的是等待拓扑，不改变EventLog schema、semantic event数量或accounting。

### 3.2 P0：Model semantic stream durable amplification

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

Write-behind pipeline完成后的batching阶段目标不是合并durable semantic events，而是：

```text
减少 batch 次数
减少每 batch 固定物理成本
保持 source item 与 durable semantic event 的 lossless 一一对应
```

### 3.3 P0：Transcript projection evidence 重复读取

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
verified evidence cache at H
        +
canonical delta (H, new_H]
        ->
verified evidence cache at new_H
```

而不是：

```text
base
    ->
read entire delta through new_H
```

### 3.4 P1：Context artifact hydration 与 prepared projection复用

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

### 3.5 P1：PostgreSQL transaction 与writer物理路径

当前已有：

- critical ledger executor与auxiliary I/O executor分离；
- process-owned PostgreSQL connection pool；
- session-owned FIFO event writer；
- write attempt absolute deadline；
- parent session/run/turn identity cache；
- multi-row event insert。

下一步不是再引入另一套writer，而是测清并压缩现有一次batch的固定成本：

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
commit
reducer fold
publisher enqueue
```

在没有分段数据前，不应凭直觉调整pool size、worker count或PostgreSQL timeout。

### 3.6 P2：Durable semantic delta coalescing

只有在完成：

1. model semantic write-behind pipeline；
2. writer固定成本优化；
3. adaptive batching；
4. verified evidence cache；

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

Model semantic pipeline还必须记录：

```text
semantic_pipeline_backlog_batches
semantic_pipeline_backlog_events
semantic_pipeline_backlog_bytes
semantic_pipeline_backpressure_seconds
semantic_pipeline_commit_inflight_seconds
provider_persistence_overlap_seconds
semantic_pipeline_drain_seconds
semantic_pipeline_terminal_tail_seconds
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
projection_evidence_cache_outcome
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
chars_per_semantic_batch
semantic_batch_flush_reason
terminal_commit_seconds
```

`semantic_batch_flush_reason`至少区分：

```text
structural
max_events
max_chars
max_age
terminal
cancellation
```

### 4.5 Benchmark协议

固定三条代表轨迹：

1. Long Plan；
2. Long manual compaction；
3. Subagent system。

每次性能验收：

- 同一代码版本每条至少运行3次；
- 报告median、p95与最慢样本；
- 记录模型slot、provider binding、输出tokens/source items与工具调用数量；
- provider总时间只作背景，不作为唯一pass/fail阈值；
- 本地metrics使用绝对值与归一化值：

```text
durable seconds / 1,000 stored events
transactions / 1,000 semantic source items
bookkeeping events / business events
context evidence bytes read / new transcript-domain bytes
context prepare seconds / model step
```

### 4.6 PERF0完成条件

- 不再依赖临时pytest plugin获取上述分段；
- metrics关闭时不改变EventLog、manifest或provider payload；
- metrics开启时同一test的durable fingerprints完全一致；
- 三条dogfood均输出machine-readable profile artifact；
- profile可区分queue、PostgreSQL、account/reducer与publication成本。

---

## 5. PERF1：Model Semantic Write-Behind 与自适应批处理

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

### 5.2 `ModelSemanticPersistencePipeline`

新增process-local、service-owned handle：

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

推荐状态机：

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

Pipeline由`ModelStreamExecutionHandle/Registry`拥有，而不是provider subscriber或Agent caller拥有。

每个queued batch保存：

```text
resolved_model_call_id
model_call_start_event_id
first_transport_sequence_index
semantic_item_count
expected_previous_semantic_event_id
stable event candidates
candidate events/bytes
batch fingerprint
enqueue order
```

### 5.3 单一物理writer与本地bounded backlog

Pipeline不得一次把大量logical batches塞入session event writer FIFO。正确形态是：

```text
最多一个 active RuntimeEventWriteService operation
    +
pipeline-owned bounded pending batches
```

这样：

- semantic backlog不会长期占满全session writer queue；
- RunEnd、checkpoint repair或其他control facts仍能获得writer admission；
- pipeline可以在前一个commit期间继续接收provider items；
- commit worker完成后从本地queue取下一batch；
- terminal drain具有唯一owner。

建议冻结三重上限：

```text
max_pending_semantic_batches
max_pending_semantic_events
max_pending_semantic_payload_bytes
```

并要求：

```text
queued uncommitted usage
    <= active model physical reservation remaining capacity
    <= model physical burst contract
```

达到任一上限后，provider reader停止调用`read_next()`，直到backlog下降到low-water。

### 5.4 Commit、terminal 与 cancellation矩阵

#### Semantic FULL

- fold committed reducer；
- apply committed terminal projection reducer；
- 推进confirmed semantic cursor；
- 完成对应batch waiter；
- 启动下一batch。

#### Semantic NONE

- 保留同一stable candidate；
- 按bounded retry policy重试；
- 不允许后续batch越过；
- deadline耗尽后终止provider read；
- 将candidate转移给session-owned pending persistence owner；
- 阻止terminal/control/close越过；
- 后续safe point或close继续bounded retry；进程退出后的Start-without-End才由reopen recovery收口。

#### Semantic UNKNOWN/PARTIAL

- latch ledger reconciliation；
- 停止新provider read；
- 保留pipeline与physical reservation owner；
- 不得生成terminal projection或control disposition；
- Host close fail closed。

#### Provider terminal先到达

- 保存terminal draft；
- seal pipeline；
- 等待全部semantic batch FULL；
- 验证terminal semantic item count等于confirmed cursor；
- 然后才生成并commit terminal batch。

#### Explicit cancellation

- 同时request cancel provider physical operation；
- seal semantic pipeline；
- physical provider与persistence owner分别drain；
- 已接收且已enqueue的semantic facts仍按stable candidate收口；
- 两个owner均可信完成后才能commitcancelled terminal。

#### Process crash

- 未commit的process-localbacklog不是durable authority；
- reopen仍按Start-without-End recovery处理；
- 因此backlog hard bound同时也是最大uncommitted semantic tail contract；
- 不得通过无界queue换取吞吐。

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
target_batch_chars
target_batch_age
recent_commit_latency_ewma
recent_provider_interarrival_ewma
pending_source_items
pending_chars
```

Hard bounds仍来自versioned physical burst contract：

```text
events <= max_batch_events
chars <= max_batch_chars
age <= max_batch_age
```

Controller只在hard bounds以内选择更合适的flush点。

建议规则：

1. structural event仍立即flush；
2. terminal/cancellation前必须flush；
3. pending batch达到hard event/char bound立即flush；
4. provider item密集且最近commit latency高时，增大target events/chars，减少transaction；
5. provider item稀疏或UI latency接近上界时，按age flush；
6. writer queue已有积压时，不为每个25ms tick继续创建独立commit owner；
7. 同一个call始终只有一个pending semantic commit；
8. controller状态不durable，不进入fingerprint；reopen仍只从confirmed semantic cursor恢复。

### 5.7 Writer侧可选micro-coalescing

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

### 5.8 PostgreSQL优化

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

### 5.9 PERF1目标

在不改变source item数量、durable event数量和semantic fingerprint的前提下：

- provider reader与semantic persistence在backlog未达hard bound时保持并行；
- Long Plan provider/durable overlap的三次median至少增加30s；
- terminal/control/tool execution仍只消费FULL committed semantic prefix；
- Long Plan `durable writer waits / model call`至少下降40%；
- Long compaction `transactions / 1,000 source items`至少下降40%；
- writer p95 queue wait不恶化；
- first-item与UI semantic latency不超过contract上界；
- cancellation/recovery/UNKNOWN测试保持全绿；
- durable exclusive time的三次median至少下降25%。

Long Plan当前provider与durable联合关键路径近似为：

```text
56.5s provider union
+ 75.2s durable union
-  7.3s existing overlap
=124.4s
```

理想完全重叠的理论下界为`max(56.5s, 75.2s)=75.2s`，即最多可隐藏约`49s`。实际仍有Start、terminal、
disposition、queue backpressure与跨model-step barrier，因此PERF1不承诺达到理论上界，但应证明节省不是仅来自provider随机变快。

这些是初始工程目标，不是永久产品常量；PERF0基线可在实施前调整具体阈值，但不得删除量化验收。

---

## 6. PERF2：Verified Incremental Transcript Evidence

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

### 6.3 新cache

新增session-owned：

```python
class VerifiedTranscriptProjectionEvidenceCache:
    ...
```

cache entry至少包含：

```text
runtime_session_id
projection_base_identity
projection_base_fingerprint
event_domain_registry_contract_fingerprint
reducer_contract_fingerprint
through_sequence
semantic_event_count
semantic_accumulator
ledger_continuity_accumulator
stable_state_fingerprint
stable_entries
terminal_document_refs
verified_artifact_content identities
entry_fact_fingerprint
```

cache key不得只使用`run_id`或`window_id`。

### 6.4 增量扩展

请求`Hnew`时：

#### Cache exact hit

```text
cached.through_sequence == Hnew
```

直接复用prepared evidence。

#### Cache incremental hit

```text
cached.through_sequence < Hnew
```

只读取：

```text
(cached.through_sequence, Hnew]
```

并验证：

- delta.before与cached prefix count/accumulator完全一致；
- delta ledger continuity before与cached accumulator一致；
- reducer从cached stable/live state增量apply后得到delta.after；
- final state与RuntimeSession committed reducer snapshot一致；
- named terminal document refs可按exact IDs补充hydrate。

#### Base/key mismatch

以下任一发生时cache miss：

- run seed改变；
- checkpoint/rebase改变；
- active window generation改变；
- event-domain/reducer contract改变；
- semantic accumulator不连续；
- cache payload损坏；
- requested high-water倒退。

Miss走现有canonical restore，不猜测、不修补。

### 6.5 Boundedness

cache必须：

- 只保存active/recent bounded projection bases；
- 具有entry count与payload bytes双上限；
- immutable entry可结构共享；
- oversized entry在mutation前跳过，不清空已有cache；
- close时可直接丢弃；
- cache eviction不影响replay或checkpointability。

### 6.6 Artifact hydration cache

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

### 6.7 可并行的context准备

在authority high-water、active window与projection base冻结后，以下操作可按依赖图并行，而不是全部串行await：

```text
projection evidence delta read
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

projection evidence
    -> required terminal content refs
    -> terminal artifact hydration
    -> normalized transcript

normalized transcript
    -> tool result render input
    -> candidate collection
```

不得为了并行化而在多个任务中复制snapshot truth。

### 6.8 PERF2目标

- 连续compile只读取new transcript-domain delta；
- Long Plan的
  `transcript-projection-evidence-read` median从约19s下降至少70%；
- Long Plan `context prepare / model step` median低于0.75s；
- cache hit/miss生成相同Context Input Manifest fingerprint；
- 删除cache后exact replay结果不变；
- corruption、contract drift、high-water rollback均fail closed；
- context evidence read bytes接近`O(new delta + newly referenced artifacts)`，不再接近`O(base..H)`。

---

## 7. PERF3：Prepared Context 复用

PERF2完成后，再优化以下CPU与artifact路径。

### 7.1 Provider projection

Invocation timing是每次compile动态事实，因此不能缓存跨invocation的最终timing header；但可以复用：

- durable stable transcript semantic；
- section membership basis；
- lowering lane；
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
- 以单次最快dogfood作为性能完成证据。

---

## 9. 建议PR顺序

### PERF0：Typed runtime profiling

- writer/context/provider分段metrics；
- machine-readable dogfood profile；
- 三次median基线；
- metrics关闭/开启semantic equality测试。

### PERF1A：Model semantic write-behind pipeline

- `ModelSemanticPersistencePipeline`与registry ownership；
- 单active writer + bounded local backlog；
- provider reader/persistence并行；
- terminal seal/drain barrier；
- NONE/FULL/UNKNOWN/PARTIAL/cancellation矩阵；
- Host close与restart recovery；
- 不改durable event schema。

### PERF1B：Semantic batching controller

- flush reason；
- adaptive target；
- hard burst bounds不变；
- cancellation/terminal/structural立即flush；
- 不改durable event schema。

### PERF1C：Writer/PostgreSQL fixed-cost reduction

- 根据PERF0数据减少round trip；
- account CAS、event insert、parent projection批处理；
- connection/pool/lock telemetry；
- 可选writer-side compatible queue coalescing。

### PERF2：Verified incremental evidence cache

- evidence cache DTO与owner；
- delta extension；
- bounded artifact hydration cache；
- checkpoint/rebase/window invalidation；
- exact restore fallback与negative tests。

### PERF3：Prepared context reuse

- static provider projection basis；
- single canonical manifest representation；
- prepared rollup cache修正；
- token estimate静态部分复用。

### PERF4：Re-measure and decision gate

- 重跑三条dogfood各至少3次；
- 全量non-real；
- 全量real-LLM + dogfood；
- PostgreSQL/Inspector/close/recovery故障矩阵；
- 决定是否需要另立durable semantic delta coalescing hard-cut规格；
- 若PERF1/PERF2已达到目标，直接回到Stage 5。

---

## 10. 测试矩阵

### 10.1 Performance

- `test_real_long_plan_emits_runtime_performance_profile`
- `test_real_long_compaction_emits_runtime_performance_profile`
- `test_real_subagent_system_emits_runtime_performance_profile`
- `test_model_semantic_pipeline_overlaps_provider_read_and_durable_commit`
- `test_model_semantic_pipeline_applies_bounded_backpressure`
- `test_semantic_batching_reduces_transactions_without_changing_events`
- `test_incremental_evidence_reads_only_new_delta`
- `test_projection_evidence_cache_avoids_repeated_prefix_decode`
- `test_manifest_factory_serializes_once_per_compile_attempt`
- `test_prepared_rollup_cache_survives_unrelated_transcript_append`

### 10.2 Semantic equality

- synchronous/pipelined persistence产生相同ordered semantic events；
- pipeline backlog形状不影响terminal projection；
- provider terminal必须等待confirmed semantic prefix；
- adaptive batching on/off产生相同ordered semantic events；
- batch size改变不影响terminal projection；
- batch schedule改变不影响control disposition；
- evidence cache hit/miss产生相同snapshot/manifest/provider payload；
- artifact cache hit/miss产生相同normalized content；
- provider projection static cache不复用旧invocation timing；
- manifest canonical representation复用不跳过untrusted replay validation。

### 10.3 Failure

- pipeline queued NONE保留原stable candidate；
- pipeline UNKNOWN/PARTIAL保留owner并latch；
- pipeline backlog达到hard bound时停止provider read；
- provider terminal先到达时必须seal/drain；
- caller detach不取消pipeline；
- Host close不得越过pending pipeline或provider physical owner；
- semantic batch commit NONE重试原stable candidate；
- semantic batch UNKNOWN保留owner并latch；
- adaptive controller crash不改变recovery；
- cache corrupted自动discard，canonical restore成功；
- cache accumulator mismatch fail closed；
- cache artifact digest mismatch fail closed；
- connection断线重连不重复event；
- queue deadline、caller cancellation与Host close保持原语义；
- benchmark metrics sink失败不影响runtime。

### 10.4 Architecture guards

禁止：

- metrics进入event-safe fingerprint；
- cache对象进入ContextFactSnapshot durable truth；
- compile/replay依赖cache存在；
- new direct PostgreSQL writer绕过RuntimeEventWriteService；
- provider transport自行提交semantic events；
- Agent/subscriber直接拥有semantic persistence task；
- pipeline向session FIFO无界enqueuelogical batches；
- terminal/control读取unconfirmed semantic backlog；
- adaptive batching越过physical burst hard bounds；
- context evidence cache按run ID弱定址；
- production raw semantic coalescing在无独立schema规格时出现。

---

## 11. Definition of Done

Stage 4.5只有同时满足以下条件才完成。

### 11.1 Correctness

- 全部现有durable/replay/account/checkpoint invariant保持；
- stable IDs、payload fingerprints和provider-visible payload不因优化变化；
- model semantic pipeline由service-owned handle持有，subscriber/caller detach不影响durable收口；
- terminal projection、ModelCallEnd和control disposition只消费FULL confirmed semantic prefix；
- pending semantic backlog具有events/bytes/batches hard bound；
- 所有failure/cancellation/recovery/close测试通过；
- cache可完全删除并从canonical authority恢复。

### 11.2 Performance

- 三条dogfood各至少3次；
- durable writer fixed-cost有分段证据；
- provider read与semantic persistence具有可观测并行重叠；
- Long Plan provider/durable overlap median至少增加30s；
- Long Plan durable exclusive median至少下降25%；
- semantic transactions / 1,000 source items至少下降40%；
- transcript evidence read median至少下降70%；
- Long Plan context prepare / model step median低于0.75s；
- provider first-item与UI streaming latency不越过冻结contract；
- 没有通过增大无界queue、线程、cache或payload上限伪造吞吐提升。

### 11.3 Operability

- Inspector/metrics能区分provider、writer、account、context与tool时间；
- profile输出bounded、secret-safe；
- production默认metrics开销有明确上限；
- Host close仍能bounded drain全部physical owners；
- PostgreSQL pool、writer queue与cache均有容量/eviction/timeout观测。

---

## 12. 最终建议

下一步建议暂停扩大Stage 5改动面，先实施：

```text
PERF0 typed profiling
    ->
PERF1A model semantic write-behind pipeline
    ->
PERF1B adaptive batching
    ->
PERF1C writer/PostgreSQL fixed-cost reduction
    ->
PERF2 incremental transcript evidence
```

完成后重新测量。

如果届时：

```text
durable exclusive << provider exclusive
context prepare / model step < 0.75s
```

则停止性能重构，回到Stage 5。

只有当：

```text
provider/durable pipeline已经完成
transaction固定成本已经压缩
evidence读取已经增量化
durable writer仍是主要墙钟瓶颈
```

才值得另立durable semantic delta coalescing hard cut。

这条顺序避免两种错误：

1. 因为本地durable成本高，就削弱刚完成的authority/accounting正确性；
2. 在没有分段证据前，提前引入新的event schema与historical decoder复杂度。

Stage 4.5的目标不是让Pulsara少记录事实，而是让它以更低的物理成本记录同一组正确事实。
