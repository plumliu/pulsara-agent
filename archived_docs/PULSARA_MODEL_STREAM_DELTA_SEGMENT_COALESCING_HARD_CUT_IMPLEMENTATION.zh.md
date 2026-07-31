# Pulsara Model Stream Delta Segment Coalescing Hard Cut 实施规格

> 状态：SEG0A、SEG0B、SEG1、SEG2、SEG3 已完成；write-behind 判定为跳过
>
> 日期：2026-07-18
>
> 前置：
> `PULSARA_LONG_HORIZON_CONTEXT_WINDOWS_HARD_CUT_IMPLEMENTATION.zh.md`
>
> 前置：
> `PULSARA_AUTHORITY_MATERIALIZATION_AND_LOSSLESS_TRANSCRIPT_PROJECTION_DESIGN.zh.md`
>
> 前置：
> `PULSARA_CONTEXT_EVIDENCE_CURSOR_PERFORMANCE_OPTIMIZATION_IMPLEMENTATION.zh.md`
>
> 性能基线与总路线：
> `PULSARA_POST_STAGE4_RUNTIME_PERFORMANCE_OPTIMIZATION_PLAN.zh.md`

---

## 0. 结论

本 hard cut 将下列四类 provider model-stream delta 从“一个 sanitized source item 对应一个 durable event”改为“连续、同 kind、同 block 的多个 source item 对应一个 durable segment event”：

| 当前 durable event | 新 durable event | 本章处理 |
|---|---|---|
| `TextBlockDeltaEvent` | `TextBlockSegmentEvent` | 是 |
| `ThinkingBlockDeltaEvent` | `ThinkingBlockSegmentEvent` | 是 |
| `DataBlockDeltaEvent` | `DataBlockSegmentEvent` | 是 |
| `ToolCallDeltaEvent` | `ToolCallArgumentsSegmentEvent` | 是 |
| `ToolResultTextDeltaEvent` | 不变 | 否 |
| `ToolResultDataDeltaEvent` | 不变 | 否 |

本章的目标不是少记录模型产生的语义内容，而是停止把 provider/SDK/network 的任意 fragmentation 误当成 Pulsara 的 durable semantic event 粒度。

```text
RawProviderStreamItem
    -> SanitizingLLMTransport
    -> SanitizedProviderSemanticEnvelope
    -> ModelStreamCoalescingCoordinator
         |- ModelStreamInputArbiter
         |- ModelStreamSegmentAccumulator
         |- ModelStreamDurableBatchAccumulator
         `- ModelStreamForegroundCommitOwner
    -> non-transcript durable segment events
    -> terminal projection
    -> transcript-semantic accepted result
```

必须同时成立：

- 拼接后的 text、thinking、data、tool arguments byte-for-byte 不变；
- block start/end、全局 projection order 与 tool-call pairing 不变；
- provider source item count、transport index coverage 与 source accumulator 可审计；
- terminal projection、control disposition、canonical transcript 与 provider payload语义不变；
- `NONE` 重试同一 stable segment candidate；
- `UNKNOWN/PARTIAL` 保留 owner 并 latch；
- terminal 不得越过尚未 `FULL` 的 source prefix；
- EventLog 仍是 durable authority；process-local segment buffer 不是第二真源；
- 本章不引入 write-behind，不允许 provider reader 领先正在提交的 sealed candidate；
- PostgreSQL 直接重置，不读取、不迁移旧 raw-delta ledger。

正式 writer baseline 已证明当前 `batch-16` 接近满载：`8192` 个、每个 `16` characters 的 text delta 形成 `514` 个 semantic commit、`8717` 条 ledger events，median semantic commit-port wall 为 `20.907s`。继续把 16 条 event 放进同一 transaction 只能减少 transaction 固定成本，不能消除 8192 条 durable rows、wrapper charge、reducer fold 和后续 evidence decode。Segment hard cut 直接改变这个根因。

### 0.1 实施结果

2026-07-18 已按本规格完成一次性 hard cut：

- provider adapters、mock 与 benchmark fixture 只产生 adapter-private `RawProviderStreamItem`；
- sanitizer 使用单一 outstanding envelope 的 prepare/adopt/discard 协议；
- `ModelStreamCoalescingCoordinator` 成为 read/timer/cancel、open segment、sealed batch 与 foreground commit 的唯一 owner；
- 四类旧 durable DeltaEvent、EventType、decoder 与 production reducer 分支已物理删除；
- text、thinking、data 与 tool-call arguments 全部落为 bounded segment，Start/End/error 保持 typed singleton；
- live cursor、commit guard、terminal projection、recovery、assembler、timeline、physical quote/accounting 与 transcript event-domain registry 已同步迁移；
- segment 继续属于 `non_transcript`，Context Evidence Cursor 只消费 terminal projection/control 等 transcript-semantic facts；
- PostgreSQL 使用新 schema 重新开始，不保留旧 ledger 兼容 reader/writer。

最终复审继续收紧了以下实现边界：read stamp使用sanitizer冻结的`accepted_at_monotonic_ns`，cancel stamp在`request_cancel()`线性化点同步安装；singleton candidate必须在seal旧segment前完整构造并验证，任何构造失败不得推进source/durable cursor；一次adopt transition产生的全部candidate必须在sanitizer acknowledgement与任何await之前同步移交Coordinator；普通writer异常经stable confirmation得到`NONE`时仍重试原candidate bytes；terminal document的source count、accumulator、durable count、actual stream measurement以及segment/domain/reducer contract必须与live cursor、physical charge/settlement和当前composition-root binding精确join。Projection artifact的caller cancellation与owned physical task cancellation被明确区分；Responses缺失tool-call identity时fail closed；provider retry只在最终durable provider-error中保存bounded、脱敏summary，不再产生per-attempt durable event或raw provider-data trace。

正式 deterministic writer 结果保存于：

```text
benchmarks/durable-runtime/baselines/v1/model-stream-segment-v1-df869e93.jsonl
benchmarks/durable-runtime/baselines/v1/model-stream-segment-v1-df869e93.jsonl.summary.json
```

该结果来自 clean validation worktree、空 template database、5 次 warmup 与 30 次 measured，`measurement_contract_adhered=true`、`production_acceptance_passed=true`：

| 指标 | batch-16 baseline | segment-v1 | 变化 |
|---|---:|---:|---:|
| durable text events | 8192 raw delta | 5 segments | age bound 额外 seal 1 次；pure content boundary 为 4 |
| logical semantic commits median | 514 | 2 | -99.61% |
| ledger events median | 8717 | 18 | -99.79% |
| semantic commit-port wall median | 20.907s | 0.049s | -99.77% |
| semantic commit-port wall p95 | 65.010s | 0.056s | -99.91% |
| model stream wall median | 24.248s | 1.947s | -91.97% |

完整 Context suite 保存于：

```text
benchmarks/durable-runtime/baselines/v1/context-suite-df869e93/
```

六个场景、340 条 trajectory 全部通过，suite 与每个 scenario 均为 `production_acceptance_passed=true`，总墙钟 `4460.5s`。相对 `context-suite-7e9a484d`：

- 各 mode 的 context prepare total median 下降 `44.87%–93.00%`；
- `artifact-heavy-tools` authority events 从 `1959` 降到 `118`；
- `incremental-active-window` 从 `5003` 降到 `621`，transcript semantic delta count 保持 `82`；
- `long-plan-prefix-growth` 从 `4964` 降到 `563`，transcript semantic delta count 保持 `80`；
- `subagent-two-children` 从 `2987` 降到 `319`，transcript semantic delta count 保持 `38`；
- checkpoint hit/rebase、compaction、Cursor/exact path 与 provider semantic graders 全部通过。

全量 Real LLM + dogfood 为 `74 passed`。三条代表轨迹的三次样本为：

| 轨迹 | median | range |
|---|---:|---:|
| Long Plan | 95.41s | 89.40–112.65s |
| Long Compaction | 24.51s | 23.3–36.63s |
| Subagent System | 约29.7s | 29.65–31.23s |

最终复审后的 non-real 全量为 `2236 passed, 77 skipped, 160 warnings`；SEG3 全量 Real LLM + 全部 dogfood 开关为 `74 passed`。最终源码又定向重跑 Long Plan、Subagent spawn/wait 与 Context compaction，结果为 `3 passed`（`115.61s`）。全量和定向结果都来自对应修复之后的实际执行，不沿用修复前结果。

正式 writer 中 foreground semantic commit wait median 只占 model stream wall 约 `2.52%`，单 call 累计约 `0.049s`，p95约 `56ms`，均低于第18.3节的三个 write-behind gate，且未观察到critical writer queue持续积压。因此冻结：

```text
write_behind_decision = skip_after_segment_coalescing
```

后续若新的provider interarrival、并发session或远程PostgreSQL证据重新越过量化gate，必须另立bounded one-inflight write-behind规格；不得在本实现中留下休眠兼容路径。

---

## 1. 范围与非目标

### 1.1 本章负责

1. 四类 model delta 的 process-local 连续聚合；
2. adapter-private `RawProviderStreamItem` discriminated union，并删除 `RawLLMTransport -> AgentEvent` 输入协议；
3. 四类 versioned durable segment event；
4. source range、transport sequence、source receipt accumulator 与 segment content identity；
5. `ModelStreamCoalescingCoordinator` 对 read/timer/cancel、segment、batch与foreground commit的唯一 ownership；
6. `ModelStreamLiveSemanticCursor` 从 event-count cursor 改为 source-item + durable-event 双 cursor；
7. terminal projection reducer、diagnostic materializer 与 Start-without-End recovery 消费 segment；
8. model physical burst contract、charge、settlement 与 doctor 静态检查；
9. Event domain registry、serialization、Inspector、Cursor/checkpoint regression与 architecture gates；
10. deterministic writer benchmark 与 real dogfood re-measure；
11. 删除四类 raw delta 的 durable producer/consumer 路径。

### 1.2 本章明确不负责

- 不聚合 `ToolResultTextDeltaEvent` 或 `ToolResultDataDeltaEvent`；
- 不改变 terminal projection document 的 model-visible content schema；
- 不改变 `ResolvedModelCall`、model context window 或 rollout budget；
- 不实现 one-inflight write-behind；
- 不让 UI、Agent、direct caller 或 window summarizer消费未确认 segment；
- 不把 segment buffer、preview 或 Cursor 变成 durable authority；
- 不用最终 usage 反推流中 segment boundary；
- 不把 data block 当自然语言 token 流；
- 不保留旧 raw-delta ledger 的 historical decoder 或兼容 reader；
- 除 adapter-private raw item union 外，不重构 provider SDK wire parsing；
- 不提前吸收 Stage 5 ContextSource ownership。

### 1.3 Segment 不是 transaction batch

必须物理区分：

```text
source item aggregation
    多个 delta draft -> 一个 durable segment event

transaction batching
    多个 durable events -> 一次 commit_semantic() transaction
```

当前 `batch-16` 只完成第二层。新设计先完成第一层，再由 bounded commit batcher组合 Start、segment、End、provider error 等 durable events。

### 1.4 Segment 不是 write-behind

V1 保持：

```text
seal stable candidates
    -> await commit_semantic(FULL)
    -> advance confirmed source cursor
    -> continue durable worker state
```

提交期间可以继续 drain/cancel 底层 physical provider operation，但不得把新的 durable candidate 无界排入 session FIFO，也不得让 provider terminal越过未确认 prefix。只有 segment hard cut 后重测仍证明 foreground semantic commit wait显著，才另立 write-behind 规格。

---

## 2. 为什么 delta event 不等于 token

Provider delta 是 SDK/SSE 暴露的任意增量字符串，不是跨 provider 稳定的 tokenizer unit。一个 delta 可能包含半个解码片段、一个 token、多个 token、tool arguments 的任意 JSON fragment 或 base64 data。结构事件甚至没有模型文本 token。

本章同时使用三种不同单位，不得互换：

| 单位 | 用途 |
|---|---|
| sanitized source item count | 防 provider fragmentation/CPU abuse；证明 transport index coverage |
| estimated token / Unicode code point | text/thinking/tool-call 的 soft segmentation target |
| UTF-8/canonical payload bytes | 内存、event row、queue、artifact 与 physical admission hard bound |

现有 `PulsaraHeuristicTokenEstimatorV1` 对普通 text 使用 `ceil(codepoints / 4)`。因此 V1 的 `8192` estimated-token soft target等价冻结为 `32768` Unicode code points。它只是 deterministic soft boundary，不是物理 hard cap。真正 hard cap 必须是 bytes 与 source-item count。

---

## 3. 旗舰模型最大输出调研摘要

本节只用于确认 segment target不能等于整个 model output cap，不成为模型配置真源。真实可调用上限仍只来自 `ResolvedModelTargetFact.context_budget.effective_output_tokens`。

截至 2026-07-18，代表性官方资料为：

| 模型 | 官方最大输出 | 参考 |
|---|---:|---|
| GPT-5.4 / GPT-5.5 / GPT-5.6 Sol | 128K | [GPT-5.4](https://developers.openai.com/api/docs/models/gpt-5.4)、[GPT-5.5](https://developers.openai.com/api/docs/models/gpt-5.5)、[GPT-5.6 Sol](https://developers.openai.com/api/docs/models/gpt-5.6-sol) |
| Claude Opus 4.8 / Sonnet 5 synchronous Messages API | 128K | [Anthropic models overview](https://platform.claude.com/docs/en/about-claude/models/overview) |
| DeepSeek V4 Pro / V4 Flash | 384K | [DeepSeek models and pricing](https://api-docs.deepseek.com/quick_start/pricing) |
| Qwen3.7 Plus | 64K | [Alibaba Cloud model table](https://help.aliyun.com/en/model-studio/vision-model/) |
| MiniMax M2 | 128K（含 CoT） | [MiniMax models introduction](https://platform.minimax.io/docs/guides/models-intro) |

主流 synchronous flagship output cap 集中在 `64K-128K`，`384K` 是需要支持的离群上界。以 `8192` estimated tokens 为 content-driven soft target，名义 segment 数为：

```text
64K   -> 8
128K  -> 16
384K  -> 48
```

实际 segment 数还会受到 block boundary、source-item cap、byte cap 和 maximum unconfirmed age影响。Data block不参与这组 token 计算。

---

## 4. 核心不变量

### 4.1 语义不变量

对任意合法 sanitized provider stream `D`：

```text
assemble_raw_drafts(D)
    ==
assemble_durable_segments(segment(D))
```

等式至少覆盖：

- ordered block kinds；
- block IDs 与 tool call IDs；
- text content；
- thinking content；
- data media type 与 base64/string content；
- raw tool arguments JSON；
- tool arguments parse status/error；
- completion/interrupted status；
- provider error placement；
- terminal outcome；
- terminal projection semantic fingerprint；
- completed call 的 `CommittedModelCallResult`；
- control disposition 与 tool execution eligibility。

Segment schedule、event IDs、source attribution与fact fingerprint允许因物理 grouping而变化；provider-visible semantic fingerprint不得因此变化。

### 4.2 连续性不变量

每一个 committed model semantic event都覆盖一个非空、连续 source span：

```text
source_item_count = last_transport_index - first_transport_index + 1
```

同一 call 的 durable events按 ledger sequence排序后必须满足：

```text
event[0].first_transport_index = 0
event[n].first_transport_index = event[n-1].last_transport_index + 1
terminal.semantic_item_count = final.last_transport_index + 1
```

不得出现 gap、overlap、duplicate、reverse range或跨 call/start attribution。

### 4.3 Source receipt accumulator 不变量

Sanitizing transport之后、Coordinator adopt之前准备唯一 source accumulator transition：

```text
A0 = sha256(domain="model-stream-sanitized-source:v2", payload="empty")

Ai+1 = sha256(
    domain="model-stream-sanitized-source:v2",
    payload={
        previous: Ai,
        transport_sequence_index: i,
        draft_kind: draft.kind,
        draft_schema_version: draft.schema_version,
        draft_fingerprint: draft.draft_fingerprint,
    },
)
```

`read_next()`只能准备`Ai -> Ai+1` transition；只有Coordinator调用`acknowledge_adopted()`后，`Ai+1`才成为live adopted accumulator。`discard_unadopted()`必须让accumulator继续保持`Ai`。

每个 singleton/segment event保存自己的 `source_accumulator_before` 与 `source_accumulator_after`。Replay不能重建已丢弃的 network fragmentation正文，也不能独立重算某个segment内部的raw draft identities；它只能精确验证transport range continuity、相邻before/after chain与terminal final accumulator。Terminal draft保存 final source accumulator，terminal commit要求它等于 live cursor。

这个 accumulator是 sanitizer producer生成的source receipt commitment，不是自足的durable replay proof，也不是provider-visible semantic fingerprint。本章明确不保存每个draft fingerprint tuple或Merkle leaves，避免重新制造per-delta payload放大。

### 4.4 Block 不变量

一个 segment只能包含连续、同 kind、同 logical block identity的 delta drafts：

```text
Text      key = (text, block_id)
Thinking  key = (thinking, block_id)
Data      key = (data, block_id, media_type)
ToolCall  key = (tool_call_arguments, tool_call_id)
```

任何 key 变化、Start、End、provider error或terminal都必须先 seal 当前 open segment。禁止跨 block、跨 kind、跨 media type 或跨 tool call拼接。

### 4.5 Terminal 不变量

`ProviderTransportTerminalDraft.semantic_item_count` 与新增 `semantic_source_accumulator` 描述 sanitized source truth。`ModelCallEndEvent`、terminal projection与settlement只有在以下条件成立后才能提交：

```text
live_cursor.confirmed_source_item_count == terminal.semantic_item_count
live_cursor.confirmed_source_accumulator == terminal.semantic_source_accumulator
no open or sealed-unconfirmed segment candidate
provider physical operation is COMPLETED
```

### 4.6 Control 不变量

Segment不改变：

```text
completed + ACCEPTED
    -> may deliver final reply / execute tool / enter canonical transcript

provider_error | cancelled | runtime_error | suppressed
    -> audit/UI only
```

尤其不得因为 `ToolCallArgumentsSegmentEvent` 已经闭合出合法 JSON，就在 `ToolCallEnd` 或 terminal disposition前 speculative execution。

---

## 5. V1 Segment Policy

### 5.1 冻结常量

```python
MODEL_STREAM_TEXT_SEGMENT_TARGET_ESTIMATED_TOKENS = 8_192
MODEL_STREAM_TEXT_SEGMENT_TARGET_CODEPOINTS = 32_768

MODEL_STREAM_STRING_SEGMENT_TARGET_UTF8_BYTES = 64 * 1024
MODEL_STREAM_DATA_SEGMENT_TARGET_UTF8_BYTES = 64 * 1024

MODEL_STREAM_SEGMENT_MAX_CONTENT_UTF8_BYTES = 128 * 1024
MODEL_STREAM_SEGMENT_MAX_CANONICAL_EVENT_BYTES = 256 * 1024
MODEL_STREAM_SEGMENT_MAX_SOURCE_ITEMS = 4_096

MODEL_STREAM_COMMIT_MAX_DURABLE_EVENTS = 16
MODEL_STREAM_COMMIT_MAX_CANDIDATE_BYTES = 1 * 1024 * 1024

MODEL_STREAM_MAX_UNCONFIRMED_AGE_SECONDS = 1.0
MODEL_STREAM_MAX_SINGLE_SOURCE_ITEM_CANONICAL_BYTES = 128 * 1024
```

`TARGET_CODEPOINTS` 必须与 V1 estimator fingerprint一起进入 segment policy contract。未来 token estimator语义变化时，必须升级 segment policy version与contract fingerprint，不能继续声称 `32768 codepoints == 8192 estimate`。

### 5.2 Soft seal 条件

Text、thinking、tool arguments在追加下一个完整 source item前，若任一条件成立：

```text
pending非空
且 (
    pending_codepoints + next_codepoints > 32768
    或 pending_utf8_bytes + next_utf8_bytes > 64 KiB
)
```

则先 seal pending，再用 next source item开始新 segment。

Data按：

```text
pending非空
且 pending_utf8_bytes + next_utf8_bytes > 64 KiB
```

执行相同逻辑。

这条64KiB string soft target用于在大量引号、反斜杠、控制字符或多字节Unicode下提前触发prospective sizing；它不替代下面的exact canonical candidate measurement。Soft target允许单个 source item越过 target，但不得越过 single-source/hard-content/canonical-event hard bound。

### 5.3 Prospective canonical candidate sizing

每次append前必须由唯一candidate factory对“pending + next source item”构造sequence=None的prospective stable event payload，并调用生产canonical event serializer获得exact candidate bytes。不得只用 `content_utf8_bytes + constant`猜测JSON escaping。

```text
prospective = build_segment_candidate(
    pending_content + next_content,
    prospective_source_span,
    prospective_attribution,
    sequence=None,
)

if prospective.canonical_event_bytes > 256 KiB:
    if pending is non-empty:
        seal pending(reason=canonical_event_byte_boundary)
        retry next item as the first item of a new segment
    else:
        fail closed: segment_single_source_item_unrepresentable
```

Composition-root doctor与sanitizer的single-source cap必须静态证明合法单个source item可以构造成一个不超过256KiB的segment candidate。因此 `segment_single_source_item_unrepresentable` 表示contract/implementation drift，必须latch，不得改写、截断或丢弃已经accepted的source item。

这里的256KiB约束作用于sequence未分配时的stable event candidate canonical bytes。PostgreSQL transaction内追加的sequence/schema stored-envelope wrapper不回写candidate，也不改变stable ID；它由10.2节的fixed conservative wrapper charge覆盖，并继续接受writer的pre-commit stored-envelope上界验证。

### 5.4 Hard seal/commit 条件

任一条件成立必须 seal：

- content达到 hard UTF-8 byte cap；
- source item count达到 `4096`；
- contiguous key变化；
- 遇到 matching或其他 block的 Start/End；
- provider error、cancel、runtime failure或terminal；
- oldest unconfirmed source age达到 `1.0s`。

任一条件成立必须提交当前 prepared durable event batch：

- durable event count达到 `16`；
- candidate bytes达到/即将越过 `1MiB`；
- provider error/cancel/runtime failure/terminal barrier；
- oldest unconfirmed source age达到 `1.0s`。

Start/End、kind/block变化只负责seal，不天然成为transaction barrier。若sealed batch尚未达到event/byte/age/error/cancel/terminal条件，可以继续和后续durable events同批。Transaction batch boundary也不得反向seal当前open segment。

一个event只能保存一个seal reason。Signal-driven reason先由7.3节的arbiter全序确定：deadline获胜使用`maximum_unconfirmed_age`，cancel获胜使用`cancellation_boundary`，provider terminal或provider error使用`terminal_boundary`。已由arbiter选中的较早signal不能被同一轮稍晚signal改写。

普通source item驱动的多个boundary同时成立时，使用下列唯一优先级：

```text
structural_boundary               # Start/End
contiguous_key_changed
canonical_event_byte_boundary
hard_content_byte_limit
source_item_limit
soft_data_byte_target | soft_string_byte_target
soft_text_token_target
```

前两项在处理next item前seal当前pending；后五项按prospective candidate判断，若需要先seal pending再重试next item。Append后恰好达到hard content或source-count上限时立即seal，并继续使用上述优先级。Transaction batch boundary永远不参与这张表。

### 5.5 单 source item 超限

Sanitizing transport必须在接受 source item、推进 index前验证：

```text
canonical draft bytes <= 128 KiB
```

超限产生 stable sanitized provider error：

```text
provider_source_item_payload_limit_exceeded
```

不得在 segment accumulator中临时切开一个 source item，因为这会让一个 transport index跨多个 durable ranges，并重新引入 ambiguous attribution。

### 5.6 时间边界的身份语义

`1.0s` age可以让相同 semantic content在不同调度下形成不同 segment boundaries。该变化只允许进入 durable fact/acceleration identity，不得进入 terminal projection semantic identity或 provider-visible semantic fingerprint。

一旦一个 segment被 seal，其 source range、content、event ID、candidate bytes与fingerprint永久冻结。`NONE`、caller cancellation或confirmation retry不得依据新时钟重新分段。

---

## 6. DTO 与事件 schema

以下为字段级冻结示意；实现必须使用项目统一 `FrozenFactBase`、`extra="forbid"`、fingerprint factory与event schema registry。

### 6.1 Adapter-private raw union

SEG0A必须先删除 `RawLLMTransport -> AsyncIterator[AgentEvent | TransportUsageReport]` 这个输入协议。新增不继承 `EventBase`、不携带run/turn/reply/sequence、不能进入serialization registry的process-local union：

```python
RawProviderStreamItem: TypeAlias = Annotated[
    RawProviderBlockStart
    | RawProviderTextDelta
    | RawProviderThinkingDelta
    | RawProviderDataDelta
    | RawProviderToolCallDelta
    | RawProviderBlockEnd
    | RawProviderFailure,
    Field(discriminator="raw_kind"),
]
```

字段矩阵：

| raw kind | required payload |
|---|---|
| `block_start` | block kind/id；data media type或tool name按kind必填 |
| `text_delta` | block id、non-empty delta |
| `thinking_delta` | block id、non-empty delta |
| `data_delta` | block id、media type、non-empty data |
| `tool_call_delta` | tool call id、non-empty raw arguments fragment |
| `block_end` | block kind/id |
| `failure` | process-local exception/error carrier；不得durable serialization |

```python
class RawLLMTransport(Protocol):
    def stream(...) -> AsyncIterator[RawProviderStreamItem | TransportUsageReport]: ...
```

所有OpenAI adapter、mock adapter和real-LLM fixtures必须先迁移到这个union。`SanitizingLLMTransport`是唯一把raw item转成versioned `ProviderTransportSemanticDraft`的owner。SEG0A完成前不得删除旧DeltaEvent；SEG0A完成后旧DeltaEvent不再拥有任何adapter-input用途，SEG1才能物理删除它们。

为保证SEG0A独立全绿，SEG0A期间只允许`LLMRuntime`内部存在一条`ProviderTransportSemanticDraft -> 旧durable DeltaEvent`临时生产桥；adapter与`RawLLMTransport`均不得看见该桥。该桥必须列入SEG1 deletion gate并在SEG1同PR物理删除，不能成为兼容facade。

### 6.2 Source span

```python
class ModelStreamSourceSpanFact(FrozenFactBase):
    schema_version: Literal["model_stream_source_span.v1"]
    resolved_model_call_id: str
    model_call_start_event_id: str

    first_transport_sequence_index: int
    last_transport_sequence_index: int
    source_item_count: int

    first_draft_kind: ProviderSemanticDraftKind
    last_draft_kind: ProviderSemanticDraftKind
    source_accumulator_before: Fingerprint
    source_accumulator_after: Fingerprint

    source_span_fingerprint: Fingerprint
```

Invariant：

- count等于闭区间长度；
- singleton span的 first/last kind相等；
- before/after使用全 call accumulator；
- replay只验证range与相邻before/after commitment chain，不声称重算span内部raw drafts；
- source span fingerprint覆盖以上全部字段，排除自身。

### 6.3 Durable attribution

```python
class ModelStreamSemanticAttributionFact(FrozenFactBase):
    schema_version: Literal["model_stream_semantic_attribution.v2"]
    resolved_model_call_id: str
    model_call_start_event_id: str
    durable_semantic_event_index: int
    durable_kind: ModelStreamDurableSemanticKind
    source_span: ModelStreamSourceSpanFact
    segment_seal_reason: ModelStreamSegmentSealReason | None
    segment_policy_contract_fingerprint: Fingerprint
    attribution_fingerprint: Fingerprint
```

`ModelStreamSegmentSealReason` 是封闭枚举：

```text
soft_text_token_target
soft_string_byte_target
soft_data_byte_target
hard_content_byte_limit
canonical_event_byte_boundary
source_item_limit
contiguous_key_changed
structural_boundary
maximum_unconfirmed_age
terminal_boundary
cancellation_boundary
```

四类segment必须携带reason；Start、End、provider error等singleton必须为`None`。Transaction batch boundary不属于segment reason，不能改变segment layout。Start、End、provider error等 singleton durable events也使用同一attribution，source span count固定为1。这样 commit/replay使用一套 cursor算法，不保留 v1 per-item特判。

### 6.4 四类 segment event

```python
class TextBlockSegmentEvent(EventBase):
    type: Literal[EventType.TEXT_BLOCK_SEGMENT]
    block_id: str
    text: str
    content_utf8_bytes: int
    content_sha256: Fingerprint
    estimated_tokens_v1: int
    model_stream_attribution: ModelStreamSemanticAttributionFact


class ThinkingBlockSegmentEvent(EventBase):
    type: Literal[EventType.THINKING_BLOCK_SEGMENT]
    block_id: str
    thinking: str
    content_utf8_bytes: int
    content_sha256: Fingerprint
    estimated_tokens_v1: int
    model_stream_attribution: ModelStreamSemanticAttributionFact


class DataBlockSegmentEvent(EventBase):
    type: Literal[EventType.DATA_BLOCK_SEGMENT]
    block_id: str
    media_type: str
    data: str
    content_utf8_bytes: int
    content_sha256: Fingerprint
    model_stream_attribution: ModelStreamSemanticAttributionFact


class ToolCallArgumentsSegmentEvent(EventBase):
    type: Literal[EventType.TOOL_CALL_ARGUMENTS_SEGMENT]
    tool_call_id: str
    arguments_json_fragment: str
    content_utf8_bytes: int
    content_sha256: Fingerprint
    estimated_tokens_v1: int
    model_stream_attribution: ModelStreamSemanticAttributionFact
```

Validator必须从实际 content重算 UTF-8 bytes、SHA 与V1 estimate。Data media type必须与 matching Start一致；segment本身不能修改 media type。

### 6.5 Terminal draft

```python
class ProviderTransportTerminalDraft(BaseModel):
    schema_version: Literal["provider_transport_terminal_draft.v2"]
    outcome: Literal["completed", "provider_error"]
    usage: ModelTokenUsageFact | None
    usage_status: Literal["reported", "missing"]
    reported_model_id: str | None
    semantic_item_count: int
    semantic_source_accumulator: Fingerprint
    terminal_fingerprint: Fingerprint
```

Terminal accumulator由 `SanitizingLLMTransport` 输出，不由 LLMRuntime根据 durable events猜测。

### 6.6 Live cursor

```python
class ModelStreamLiveSemanticCursor:
    resolved_model_call_id: str
    model_call_start_event_id: str
    start_sequence: int

    confirmed_source_item_count: int
    confirmed_source_accumulator: Fingerprint
    confirmed_durable_event_count: int
    last_semantic_event_id: str | None
    terminal_event_id: str | None
```

`advance_semantic()` 不再执行 `+= len(events)` 作为 source count，而是逐 event验证 source span continuity，并：

```text
confirmed_source_item_count += event.source_span.source_item_count
confirmed_source_accumulator = event.source_span.source_accumulator_after
confirmed_durable_event_count += 1
```

### 6.7 Commit guard

```python
class ModelStreamSemanticCommitGuard:
    resolved_model_call_id: str
    model_call_start_event_id: str

    first_transport_sequence_index: int
    source_item_count: int
    source_accumulator_before: Fingerprint
    source_accumulator_after: Fingerprint

    first_durable_semantic_event_index: int
    durable_event_count: int
    expected_previous_semantic_event_id: str | None
```

Guard验证 source coverage与 durable candidates，不再要求 candidate count等于 provider source item count。

---

## 7. ModelStreamCoalescingCoordinator

### 7.1 唯一 owner

新增：

```text
src/pulsara_agent/llm/coalescing.py
src/pulsara_agent/llm/segment.py
```

唯一生产owner：

```text
ModelStreamCoalescingCoordinator
    |- ModelStreamInputArbiter
    |- ModelStreamSegmentAccumulator
    |- ModelStreamDurableBatchAccumulator
    |- ModelStreamForegroundCommitOwner
    `- ModelStreamLiveSemanticCursor
```

Coordinator恰好拥有：

- 一个transport read；
- 至多一个completed-but-not-adopted sanitized envelope；
- 一个open segment；
- 一个bounded sealed durable batch；
- 一个foreground commit attempt；
- 一个confirmed source cursor；
- 一个confirmed durable-event cursor。

`LLMRuntime`只驱动Coordinator，不再分别隐式维护timer、pending chars、semantic batch、source count与commit candidate。Adapter、recovery、Inspector或测试不得各自实现字符串拼接或竞态winner算法。

### 7.2 Sanitized envelope的prepare/adopt协议

```python
class SanitizedProviderSemanticEnvelope:
    envelope_id: str
    draft: ProviderTransportSemanticDraft
    proposed_transport_sequence_index: int
    source_accumulator_before: Fingerprint
    source_accumulator_after: Fingerprint
    accepted_at_monotonic_ns: int
    read_completion_stamp: ArbiterSignalStamp
```

Sanitizer每次只能存在一个outstanding envelope。`read_next()`完成validation、single-item admission与prospective source receipt计算，但不得在返回前不可逆地推进adopted source cursor。Coordinator调用同步、无await的：

```text
acknowledge_adopted(envelope_id)
```

后，sanitizer才推进adopted item count/bytes/index/accumulator，并允许下一次read。Cancellation先赢时调用：

```text
discard_unadopted(envelope_id)
```

然后关闭transport；terminal source count/accumulator只使用adopted prefix。这样不存在“sanitizer已经推进、durable/live cursor却永远看不到该item”的状态。

`acknowledge_adopted()`之前，Coordinator必须同步接管该transition生成的完整candidate tuple。结构边界允许一次transition同时产生“旧segment + singleton”或“旧segment + 新segment”；即使第一项恰好填满当前16-event batch，第二项也必须已经进入Coordinator的bounded pending queue。若随后commit发生caller cancellation、`UNKNOWN/PARTIAL`或reconciliation latch，execution handle必须同时保留attempted batch与全部pending candidates；禁止任何candidate只存在于worker局部变量或栈帧。

read signal的monotonic stamp必须复用该envelope在sanitizer完成validation时冻结的`accepted_at_monotonic_ns`，不能在`read_next()`返回到worker后重新取时钟。cancel signal则必须在`ModelStreamExecutionHandle.request_cancel()`内、设置cancel event之前同步签发；waiter只能读取该既有stamp，不能在被调度唤醒后补签。

### 7.3 Input Arbiter与唯一winner

`ModelStreamInputArbiter`使用同步linearization lock与单调递增ordinal为read completion、cancel intent安装唯一：

```python
class ArbiterSignalStamp:
    monotonic_ns: int
    linearization_ordinal: int
```

Oldest age deadline是固定absolute monotonic timestamp，不因循环或新read重置。每轮同时观察read、cancel和deadline，按以下唯一算法：

1. 若已有completed-but-not-adopted envelope，禁止启动第二个read；
2. 为所有ready signal构造全序键：deadline为`(deadline_monotonic_ns, 0, 0)`，read/cancel为`(stamp.monotonic_ns, 1, stamp.linearization_ordinal)`；最小键唯一获胜；
3. deadline获胜：只seal/commit当时已经adopted的open segment，然后从ready set移除该deadline；不得adopt、discard或重启read；
4. read获胜：adopt唯一envelope；若cancel随后获胜，以`cancellation_boundary` seal刚刚adopt的内容；
5. cancel获胜：discard唯一unadopted envelope（若有），关闭transport，terminal source只使用adopted/confirmed prefix；
6. 重复选择，直到本轮ready signals处理完或cancel进入terminal flow；
7. 因deadline在相同monotonic timestamp下priority为0，`age_deadline <= envelope.accepted_at_monotonic_ns`时必先flush旧prefix；read与cancel同timestamp时由在同一linearization lock下签发的唯一ordinal决定；
8. deadline到达不取消、不重启唯一transport read，持续快速read也不能饿死timer。

`linearization_ordinal`消除平台时钟分辨率导致的read/cancel相同timestamp歧义；固定deadline priority消除deadline与其他signal同timestamp的歧义。不存在实现自选的tie-break。

Cancel linearization后迟到完成的transport read只能进入同一个discard路径；它不得生成第二个envelope、不得推进transport index/receipt accumulator，也不得重新打开terminal flow。

### 7.4 Segment accumulator

```text
adopt sanitized envelope E

if E is one of four delta kinds:
    derive contiguous key K
    if open segment exists and key differs:
        seal open segment
    run soft codepoint/UTF-8 limits
    run exact prospective canonical candidate sizing
    if pending + E crosses a boundary:
        seal pending with exact reason
        retry E as first item
    append E content and before/after receipt commitment
    if hard content/source bound reached:
        seal open segment
else:
    build and fully validate singleton durable candidate for E
    seal open segment(reason=structural_boundary)
    install the already-frozen singleton candidate
```

Content使用list/rope-like parts累积，seal时只 `join()` 一次；禁止每个delta执行 `current + delta` 形成O(n²) allocation。`ModelStreamSegmentAccumulator`是pure state machine；I/O、timer与commit只能由Coordinator调用。

singleton必须先按预测的next durable index完成Event DTO、canonical bytes、hard-cap与fingerprint验证，再允许seal已有open segment。singleton构造失败、segment最终hard-cap验证失败或candidate freeze失败时，open segment、source cursor与durable event index必须保持原值。

### 7.5 Durable batch accumulator

Sealed segment与singleton structural event进入bounded durable batch。Batch只由以下条件提交：

- durable event count；
- exact candidate bytes；
- oldest unconfirmed age；
- provider error/cancel/runtime failure；
- provider terminal。

Start/End、kind/block变化只seal；block End不强制transaction。Batch event/byte boundary也不得seal open segment。Coordinator可以提交现有sealed batch，同时保留仍未到segment边界的open segment。

### 7.6 Foreground commit owner

V1同时最多一个foreground commit。Seal后的event/candidate/source range永久冻结：

```text
NONE            -> retry exact candidate bytes
FULL            -> fold, advance both cursors, publish
UNKNOWN/PARTIAL -> retain owner and latch
cancel-after-FULL -> adopt typed FULL result before propagating cancellation
```

`NONE`不只包括writer主动返回的typed pre-commit outcome。任意普通数据库/writer异常都必须先对同一stable event-ID batch执行confirmation；确认`NONE`后转换成同一个retry outcome，确认`FULL`则adopt该winner，只有`UNKNOWN/PARTIAL`才latch。外层不得因异常类型不同而重建segment或改写terminal outcome。

`RuntimeEventWriteCancelled`必须在commit port内消费其writer-owned physical result：`FULL`返回confirmed batch，queued/pre-commit `NONE`转换为`ModelStreamCommitNotCommitted`，其他physical error保留原错误，缺失physical result则latch。它不得以裸`CancelledError`穿出并被runtime误判为transport worker cancellation。

Provider terminal draft一旦确定，terminal projection document、artifact ID、terminal events与outcome也成为同一份stable candidate。Projection artifact的caller cancellation必须等待原physical operation；瞬时pre-commit failure只允许用相同document bytes重试。Content conflict、confirmation drift或无法证明的physical outcome必须latch，禁止把已经确定的`completed`/`provider_error`改写成`runtime_error`。

Projection artifact waiter收到`CancelledError`时必须检查owned physical task本身是否被取消，不能用`physically_complete`替代。Caller cancellation与physical success同时发生时，waiter先`uncancel()`并消费原operation的成功值或原始异常；只有owned task的`cancelled()`为真时才属于physical cancellation。

Foreground commit未terminal前，Coordinator不得产生第二个commit owner或无界读取provider。这仍不是write-behind。

### 7.7 Commit candidate算法

每个seal产生一次immutable event与stable candidate。Event ID：

```text
model_segment:{resolved_call_id}:{durable_semantic_event_index}:
    {durable_kind}:{source_span_fingerprint_prefix}
```

Singleton structural event继续使用同一`durable_semantic_event_index` namespace。Event ID与candidate不得包含wall-clock deadline；实际source span已经唯一确定sealed candidate。

### 7.8 Tool arguments

`ToolCallArgumentsSegmentEvent.arguments_json_fragment`始终保存 raw concatenated JSON fragment。只有 matching `ToolCallEndEvent` 后 terminal projection reducer才执行一次 JSON parse，继续区分：

```text
valid_object
invalid_json
non_object_json
```

Segment边界不能改变 parse status、raw JSON或parse error code。

### 7.9 Data block

Data block按 UTF-8 bytes聚合，不做 token estimate。V1继续把 `data` 当 provider传入的 string/base64 carrier，不在本章新增解码、重编码或压缩。

以下全部 fail closed：

- 同 block media type漂移；
- source item超过128KiB canonical bytes；
- segment content超过128KiB UTF-8 bytes；
- segment canonical event超过256KiB；
- End时出现未知 data block；
- terminal completed时仍有违反 transport contract的 open block。

---

## 8. Lifecycle 与提交顺序

### 8.1 正常 completed

```text
commit ModelStart FULL
    -> receive Start + deltas
    -> Coordinator seal/commit zero or more bounded batches FULL
    -> receive End, seal preceding segment, keep End in bounded batch
    -> receive ProviderTerminal(completed)
    -> seal/commit remaining batch FULL
    -> verify source count + accumulator + physical completion
    -> prepare terminal projection from committed segments
    -> commit projection + ModelEnd + ReplyEnd + settlement FULL
    -> EventLog materialize CommittedModelCallResult
    -> control disposition
```

### 8.2 Provider error/open block

```text
pending segment
    -> seal
    -> append ProviderModelStreamErrorEvent singleton
    -> commit FULL
    -> physical drain
    -> terminal projection marks open block interrupted
    -> ModelEnd(provider_error)
```

已观察、已确认的 segment只用于 audit/UI；不得执行其中 tool call或交付成功 reply。

Adapter必须尊重provider显式block终止信号。OpenAI Responses的`response.output_text.done`、`response.reasoning_summary_text.done`与`response.reasoning_text.done`分别生成对应typed End；随后`response.completed`只关闭仍然active的block。`done`携带的完整text/reasoning/tool-arguments也是lossless reconciliation authority：此前没有delta且final payload非空时，adapter必须补发唯一typed delta；此前已有delta时，累计内容必须与final payload逐字节相等，否则以稳定protocol error fail closed。禁止忽略final payload、重复附加完整内容或静默接受drift。`response.failed/error/incomplete`以及SDK/network异常不得调用`close_active_blocks()`或合成正常End，terminal projection必须把当时仍open的block标记为`interrupted`。Tool arguments在具有非空tool name的Start之前到达时直接以稳定protocol error fail closed，禁止用空name补造Start。Provider item ID、tool call ID与tool name在首个named Start时冻结；Responses的function-call item同时缺少`call_id`与`item.id`时以`transport_tool_call_identity_missing` fail closed，禁止生成Pulsara-owned随机UUID。后续delta/done或Chat chunk发生identity/name drift必须fail closed。并行active tool call使用provider start order关闭，禁止依赖hash/set iteration产生不确定的End顺序。

Provider retry永久属于adapter-private operational loop。每次attempt可以写脱敏日志，但不得产生`CustomEvent(name="llm.retry")`，也不得保存raw exception message/repr、URL、response body或`provider_data`。最终失败的唯一durable provenance是`ProviderSanitizedErrorFact.retry_summary: ProviderRetrySummaryFact`：保存最多32次attempt的稳定reason/status/delay/retry-after、final reason、exhausted与semantic-output skipped状态，并由声明式contract fingerprint校验。已经有semantic output时必须写`skipped_reason="semantic_output_started"`。成功retry不产生durable retry history；本章不恢复per-attempt event。

### 8.3 Cancel

`request_cancel()` 后：

1. 取消/关闭 provider transport；
2. 等待 physical completion；
3. seal已经被Coordinator adopt的 pending source；cancel先赢的completed-but-not-adopted envelope必须discard；
4. commit stable segment batch；
5. commit cancelled terminal batch；
6. control面不得接纳 partial tool call。

若 physical completion为 `BLOCKED_UNTRUSTED`，保留 execution owner、segment buffer/candidate与physical operation，禁止 terminal commit和Host teardown。

### 8.4 `NONE/FULL/UNKNOWN/PARTIAL`

| outcome | 行为 |
|---|---|
| `NONE` | 保留同一 sealed candidates；在原 attempt deadline/owner下重试，不重新分段 |
| `FULL` | fold committed events；推进双 cursor；发布 committed segment notifications |
| `UNKNOWN/PARTIAL` | latch ledger；保留 owner与candidate；禁止继续 provider semantic progression和terminal |
| caller cancelled after `FULL` | 从 typed operation result adopt committed batch、推进 cursor，然后传播取消 |

### 8.5 Worker crash/restart

Process-local、尚未 seal/commit 的 source tail不是 durable truth。重启看到 `ModelStart` without `ModelEnd`时：

- 只从 committed singleton/segment events重建 interrupted projection；
- 验证 source range与accumulator chain；
- 写 stable recovery terminal；
- 不伪造丢失的 provider tail；
- 不将 interrupted tool call变为 executable；
- 不把 UI可能看到的未确认 preview反写入 EventLog。

最大未确认 tail由 `1s + 128KiB + 4096 source items` 三个上界共同限制，不是无限 buffer。

---

## 9. Terminal Projection 与 Materialization

### 9.1 Reducer contract v2

`ModelTerminalProjectionReducer`支持：

```text
TextBlockSegmentEvent             -> append text
ThinkingBlockSegmentEvent         -> append thinking
DataBlockSegmentEvent             -> append data after media-type join
ToolCallArgumentsSegmentEvent     -> append raw arguments fragment
```

四类旧 DeltaEvent不得进入 reducer。

Reducer每个 durable event执行：

1. 验证 attribution call/start；
2. 验证 durable event index；
3. 验证 source span first index等于当前 source count；
4. 验证 source accumulator before等于当前 accumulator；
5. fold content；
6. 推进 source count/accumulator与 durable event count。

### 9.2 Source fact v2

`ModelCallSemanticSourceFact`升级，至少保存：

```text
source_semantic_item_count        # provider sanitized source items
source_first_transport_index
source_last_transport_index
source_semantic_accumulator       # raw source receipt chain
durable_semantic_event_count      # singleton + segment rows
durable_event_accumulator         # canonical durable semantic event chain
segment_policy_contract_fingerprint
model_stream_semantic_domain_contract_fingerprint
reducer_contract_fingerprint
```

terminal commit必须hydrate同一个content-addressed document，并在写入前验证：source call/Start identity、source item count、final source accumulator、durable event count分别等于live commit guard；`segment_policy_contract_fingerprint`、`model_stream_semantic_domain_contract_fingerprint`与`reducer_contract_fingerprint`分别等于当前run-frozen binding。任一不一致均为contract failure，不能只依赖document自报的outer fingerprint。

Provider semantic identity只由 ordered normalized terminal items决定；source/durable grouping进入 outer fact fingerprint与audit，不污染 semantic fingerprint。

### 9.3 Canonical result

Completed model call继续从 confirmed terminal projection document构造 `CommittedModelCallResult`。禁止恢复旧 raw-delta materializer作为控制面第二真源。

Diagnostic/doctor可以比较：

```text
process-local raw-draft reference assembler
vs
durable segment reducer
```

但 diagnostic结果不能参与control disposition。

---

## 10. Physical Admission 与 Accounting

### 10.1 Burst contract hard cut

现有：

```text
TransportFragmentedBurstContractFact
fragmentation_mode = one_event_per_sanitized_source_item
```

替换为：

```python
class TransportSegmentedBurstContractFact(PhysicalBurstContractBase):
    schema_version: Literal["transport_segmented_burst_contract.v1"]
    burst_shape: Literal["transport_segmented"]
    operation_kind: Literal[PhysicalOperationKind.MODEL_CALL]
    segmentation_mode: Literal["contiguous_model_delta_segment_v1"]

    max_source_items: int
    max_source_payload_bytes: int
    max_single_source_item_canonical_bytes: int

    max_segment_source_items: int
    max_segment_content_utf8_bytes: int
    max_segment_canonical_event_bytes: int
    max_durable_event_wrapper_overhead_bytes: int
    max_unconfirmed_age_millis: int

    max_durable_events_per_source_item: int
    max_synthetic_semantic_tail_events: int
    max_synthetic_semantic_tail_payload_bytes: int
    max_start_commit_batches: int
    max_terminal_commit_batches: int
    max_recovery_commit_batches: int
    max_bookkeeping_events_per_commit: int
    max_bookkeeping_base_payload_bytes_per_commit: int
    max_bookkeeping_payload_bytes_per_business_event: int
    segment_policy_contract_fingerprint: Fingerprint
    sanitization_contract_fingerprint: Fingerprint
```

### 10.2 Worst case仍保守

若 provider在每个 source item之间切换 kind/block，segment无法减少 durable event count。因此 upfront reservation与doctor worst case不能直接按“正常每8K一个segment”缩小。

Provider/adapter显式产出的`RawProviderFailure`属于普通`max_source_items`并接受相同source-item admission。只有sanitizer因exception、protocol violation或circuit breaker合成的`ProviderErrorDraft`属于独立synthetic semantic tail；V1最多一个。它仍获得下一个transport index并进入before/after receipt chain，但其bounded payload由synthetic/terminal tail覆盖，不重复计入provider source payload。

计数语义冻结为：

```text
adapter_source_item_count <= max_source_items
synthetic_semantic_tail_count <= max_synthetic_semantic_tail_events
terminal.semantic_item_count =
    adapter_source_item_count + synthetic_semantic_tail_count
```

所有adapter-derived与synthetic semantic envelope共享同一连续`transport_sequence_index`和source-span chain；但actual settlement必须分别报告两种count，不能把synthetic error伪装成adapter source item。

唯一reservation公式冻结为：

```text
max_durable_semantic_events =
    max_source_items * max_durable_events_per_source_item
    + max_synthetic_semantic_tail_events

# age/error/cancel可以让每个semantic event成为独立commit
max_semantic_commit_batches = max_durable_semantic_events

max_commit_batches =
    max_start_commit_batches
    + max_semantic_commit_batches
    + max_terminal_commit_batches
    + max_recovery_commit_batches

max_bookkeeping_events =
    max_commit_batches * max_bookkeeping_events_per_commit

max_bookkeeping_payload_bytes =
    max_commit_batches
        * max_bookkeeping_base_payload_bytes_per_commit
    + max_durable_semantic_events
        * max_bookkeeping_payload_bytes_per_business_event

max_total_reserved_events =
    max_durable_semantic_events
    + max_bookkeeping_events
    + max_structural_tail_events
    + max_terminal_recovery_events

max_total_reserved_payload_bytes =
    max_sanitized_source_payload_bytes
    + max_synthetic_semantic_tail_payload_bytes
    + max_durable_semantic_events
        * max_durable_event_wrapper_overhead_bytes
    + max_bookkeeping_payload_bytes
    + max_structural_tail_payload_bytes
    + max_terminal_recovery_payload_bytes
```

`max_sanitized_source_payload_bytes`只表示所有已准入source item对durable semantic content的canonical payload贡献，已经覆盖正文及其JSON escaping，但不包含不会durable化的adapter-private DTO wrapper。`max_synthetic_semantic_tail_payload_bytes`单独覆盖sanitizer合成error的bounded正文。Durable wrapper overhead只覆盖segment/singleton event相对这些payload新增的schema、ID、attribution、sequence上界，不得再次按`max_segment_canonical_event_bytes`重复计算正文。Candidate factory必须验证每个实际event满足：

```text
canonical_candidate_bytes
    <= sum(canonical_source_payload_bytes_in_span)
       + max_durable_event_wrapper_overhead_bytes
```

Sequence位数、ID长度、fingerprint、seal reason与所有bookkeeping字段都必须有schema max length和maximal fixture。Start、normal terminal、recovery terminal分别由三个显式batch上界覆盖；因为1秒age可能让最坏情况下每个durable semantic event独立commit，`max_commit_batches`必须由上式导出，禁止沿用经验值。性能收益体现在actual settlement，而不是通过低估reservation获得。

`PhysicalOperationChargeAppliedEvent`不能使用与batch大小无关的固定byte charge。该event会内嵌本批全部business candidate的charge identity；maximal fixture实测从单business event约`6.6 KiB`增长到16 events约`30.2 KiB`。SEG1因此同步hard cut `PhysicalChargeContractFact`：

```text
charge_applied_payload_quote =
    charge_applied_bookkeeping_base_charge_bytes
    + business_event_count
        * charge_applied_bookkeeping_per_business_event_charge_bytes
```

V1冻结为保守的`7,680 + 2,048 * business_event_count` bytes，并保留`256 KiB`作为该schema本身的absolute stored-envelope cap。Writer必须在同一transaction内分配sequence并构造stored envelope后、commit前验证actual bytes不超过本次dynamic quote；低估必须rollback，禁止post-commit observation后再latch。Replay/doctor从durable charge event中的同一quote结算，不从当前process policy重算。Start/terminal/recovery等非semantic business events的per-event charge由对应structural/recovery tail覆盖，不能重复计入semantic source payload。

V1 contract还必须强制：

```text
max_durable_events_per_source_item = 1
max_synthetic_semantic_tail_events = 1
max_start_commit_batches = 1
max_terminal_commit_batches = 1
```

`max_recovery_commit_batches`由Start-without-End recovery矩阵的最大合法batch数声明式导出，不允许composition root任意填写。

### 10.3 Actual charge

每个 segment event只按一次 candidate event、一次 wrapper与所属 commit bookkeeping收费。不得按内部 source item count虚构 N 个 durable wrapper charge。

Settlement同时保存：

- sanitized source item count/bytes；
- synthetic semantic tail count/bytes；
- durable singleton event count；
- durable segment event count；
- segment content/canonical bytes；
- actual commit batch count；
- charged candidate/wrapper/bookkeeping totals；
- released reservation。

具体durable carrier为`ModelStreamSettlementMeasurementFact`，由terminal projection的`ModelCallSemanticSourceFact`持有；每个semantic batch使用`ModelStreamSemanticCommitMeasurementFact`保存ordered semantic stable identities、writer-prepared actual candidate bytes与对应`PhysicalOperationChargeAppliedEvent` identity/fingerprint。这里的actual bytes必须直接取自同批charge fact的`business_candidate_charge_payload_bytes`，它覆盖RuntimeSession default metadata等write-boundary overlay；不得使用Coordinator在overlay前计算、仅用于segment/commit admission的prospective candidate bytes冒充actual measurement。Terminal commit guard精确join measurement fingerprint，`PhysicalOperationSettlementFact.model_stream_measurement_fingerprint`再次join同一值。Inspector只投影bounded aggregate counts/bytes/batch count与measurement fingerprint，不展开全部batch identity列表。

### 10.4 Doctor

配置/doctor至少验证：

- target/estimator与segment policy binding存在且fingerprint匹配；
- single source item cap不大于segment canonical hard cap可承载范围；
- segment candidate可被一次 writer operation接受；
- maximum structural/error/terminal batch仍可完成；
- max output cap不会被误用为physical row cap；
- Data path只使用bytes合同；
- worst-case source-item alternation仍被reservation覆盖。
- synthetic provider error的来源、index、receipt chain与tail charge矩阵闭合；
- Start、semantic、terminal、recovery四类最坏commit batch均进入quote；
- maximal source/event/bookkeeping fixture不越过wrapper与commit quote。

---

## 11. Context Evidence Cursor、Checkpoint 与 Replay

### 11.1 Cursor仍保留，但不消费segment

Segment解决producer fragmentation；Cursor解决transcript-semantic repeated prefix read。当前event-domain真值把model stream Start/segment/End全部分类为`non_transcript`；真正进入Cursor semantic vector的是terminal projection、control disposition及其他明确的transcript-semantic events。

本 hard cut后：

- 四类segment与model singleton只推进ledger continuity/active authority high-water，不进入Cursor selected semantic envelopes或semantic accumulator；
- Cursor继续只保存terminal projection/control等transcript-semantic envelopes；
- full event-domain registry fingerprint因schema hard cut升级，旧process-local Cursor一次性失效；
- process-local旧Cursor全部discard；
- PostgreSQL reset后从新seed/checkpoint exact restore；
- same-H、new-delta extension与resident eviction语义不变；
- Cursor不存在、淘汰或拒绝admission仍走bounded exact path。

### 11.2 Context semantic不依赖segment layout

Transcript projection只消费terminal projection/control disposition后的稳定语义。Segment source ranges、flush reason、event IDs、tree/checkpoint placement不得进入 provider semantic fingerprint。

### 11.3 Regression boundary

Segment完成后只需重跑现有Cursor/context suite证明domain classification与ledger continuity没有回归。本章不声称segment减少Cursor resident vector，也不重新打开Cursor on/off性能决策。Segment event schedule不得进入transcript semantic prefix、checkpoint semantic identity或provider semantic fingerprint。

---

## 12. Subscription、Inspector 与 UI

### 12.1 Public subscription

现有 public model notification subscription继续只发布 committed events。它将看到 segment级更新，不再看到每个 raw delta event。

V1最大 committed streaming cadence由 `MODEL_STREAM_MAX_UNCONFIRMED_AGE_SECONDS = 1.0`限定。UI必须把一个 segment content作为增量追加，不能把它误显示成完整 block。

如果未来需要低于1秒的非durable preview，必须另立明确的 process-local `ModelStreamPreviewSubscription`：

- 只供UI；
- 不使用 `AgentEvent`；
- 不进入EventLog、transcript、tool gate、usage或control；
- crash/restart可丢失；
- 不能作为本章性能验收前置。

### 12.2 Inspector

Inspector对每个 segment bounded展示：

```text
durable event ID/type/sequence
call/start ID
block/tool-call identity
source first/last/count
content bytes
estimated tokens（非Data）
flush reason
source accumulator before/after
segment policy fingerprint
```

默认不展开完整content；只显示bounded preview、SHA与artifact/reference信息。Inspector必须区分 source item count与durable event count。

---

## 13. Database Reset 与删除清单

这是 hard cut，不做 backward compatibility。生产切换PR合入前必须重置开发/test PostgreSQL schema/data。

删除或禁止 durable 使用：

- `EventType.TEXT_BLOCK_DELTA`
- `EventType.THINKING_BLOCK_DELTA`
- `EventType.DATA_BLOCK_DELTA`
- `EventType.TOOL_CALL_DELTA`
- 四类 event的serialization/historical decoder注册；
- terminal reducer/materializer/recovery中的四类delta分支；
- `one_event_per_sanitized_source_item` model burst contract；
- `commit_semantic()` 中 candidate count等于source item count的假设；
- live cursor `semantic_item_count += len(events)`；
- benchmark只比较batch-4/8/16但不改变durable row数量的production acceptance口径。

删除存在严格前置：所有provider adapters、mock、fixtures与`RawLLMTransport`必须已迁移到adapter-private `RawProviderStreamItem` union。禁止以“只从durable union移除、adapter继续构造同名EventBase”为过渡方案。SEG0A合入后，生产adapter输入协议中不再出现任何`AgentEvent`；SEG1随后物理删除旧DeltaEvent/EventType/decoder。

---

## 14. 代码落点

| 文件/模块 | 修改 |
|---|---|
| `src/pulsara_agent/llm/raw_provider.py` | 新增adapter-private raw stream discriminated union；不依赖EventBase |
| `src/pulsara_agent/llm/coalescing.py` | 新增唯一Coordinator、InputArbiter、batch与foreground commit owner |
| `src/pulsara_agent/llm/segment.py` | 新增pure segment accumulator、policy、prospective candidate sizing与factory |
| `src/pulsara_agent/llm/adapters/*` | 全部改产RawProviderStreamItem，不再产AgentEvent |
| `src/pulsara_agent/llm/drafts.py` | terminal draft v2；source accumulator envelope |
| `src/pulsara_agent/llm/sanitizing_transport.py` | raw union输入；single outstanding prepare/adopt/discard；per-source cap与receipt accumulator |
| `src/pulsara_agent/llm/runtime.py` | 驱动Coordinator；删除独立read/timer/batch winner与raw delta durable producer |
| `src/pulsara_agent/llm/commit.py` | source-item/durable-event双guard；stable segment retry |
| `src/pulsara_agent/llm/execution.py` | live cursor v2、committed segment notification、close/drain |
| `src/pulsara_agent/llm/terminal_projection.py` | reducer contract v2、segment消费、source fact v2 |
| `src/pulsara_agent/llm/materialize.py` | diagnostic segment reader；无production raw fallback |
| `src/pulsara_agent/llm/recovery.py` | Start-without-End按segment恢复interrupted projection |
| `src/pulsara_agent/event/events.py` | 四类segment events；删除四类durable delta events |
| `src/pulsara_agent/event/types.py`或等价枚举模块 | 新EventType、删旧EventType |
| `src/pulsara_agent/event_log/serialization.py` | segment schema/decoder；删除旧delta decoder |
| `src/pulsara_agent/primitives/model_call.py` | attribution v2、source fact v2、typed enums |
| `src/pulsara_agent/primitives/authority_materialization.py` | segmented burst contract DTO/constants |
| `src/pulsara_agent/runtime/authority_materialization/contracts.py` | binding、doctor、reservation formulas |
| `src/pulsara_agent/runtime/context_input/*` | segment分类为non_transcript；registry/reset回归；semantic identity不含segment layout |
| `src/pulsara_agent/runtime/timeline.py`与Inspector | bounded segment projection |
| `benchmarks/durable-runtime/*` | segment production case、metrics与grader |
| `contracts/LLM_TRANSPORT_CONTRACT.zh.md` | source vs durable segment ownership |
| `contracts/EVENT_LOG_STORAGE_CONTRACT.zh.md` | segment authority与reset边界 |
| `contracts/MESSAGE_TRANSCRIPT_CONTEXT_CONTRACT.zh.md` | segment不进入provider semantic layout |

实际实现前必须用 `rg`重新生成完整producer/consumer清单，不能只依赖本表。

---

## 15. 实施顺序

### SEG0A：Adapter-private raw DTO hard cut

- 新增`RawProviderStreamItem` sealed union；
- `RawLLMTransport`返回raw union/usage，不再返回AgentEvent；
- OpenAI adapters、mock、real-LLM fixtures一次性迁移；
- sanitizer输入、failure与block-state validation迁移；
- SEG0A临时runtime bridge是旧DeltaEvent唯一允许的producer，并由SEG1 deletion gate精确定位；
- architecture guard禁止adapter import/construct durable model block events；
- 暂不改变EventLog writer与durable schema；
- 全量测试保持绿。

### SEG0B：Pure coalescing contracts与reference assembler

- 新segment DTO、policy contract、source span与source accumulator；
- pure segment accumulator、prospective canonical sizer与candidate factory；
- `ModelStreamInputArbiter` deterministic winner模型；
- `ModelStreamCoalescingCoordinator` process-local state machine；
- raw-draft reference assembler与segment reducer equality grader；
- 四类delta、Unicode、base64、tool JSON与boundary单测；
- 不接生产writer，不改变EventLog schema；
- 全量测试保持绿。

### SEG1：Production vertical hard cut

同一PR内完成：

- Runtime producer切到segment；
- commit guard/live cursor双计数；
- terminal draft/source fact v2；
- terminal projection/materializer/recovery切到segment；
- Event union/serialization/domain registry切换；
- physical burst/account/doctor切换；
- public subscription/Inspector切换；
- segment注册为non_transcript；Cursor/checkpoint/replay做domain regression；
- 删除四类raw delta durable路径；
- 重置PostgreSQL；
- 全量non-real、architecture gates与failure matrix全绿。

四种delta必须一次hard cut，不允许先让text走segment、thinking/tool/data继续raw而形成双生产语义。

### SEG2：Deterministic benchmark与性能验收

- 新增production-valid `segment-v1` writer scenario；
- 同一 `8192 * 16 chars` source workload；
- grader比较raw-draft reference assembler与durable segment terminal projection；
- 输出source/segment/event/commit/bytes/wall metrics；
- 5 warmup + 30 measured；
- clean Git、empty template DB、独立clone DB；
- 保存正式baseline/optimized result。

### SEG3：Real dogfood与路线决策

- Long Plan；
- Long compaction；
- Subagent system；
- 全量real-LLM + dogfood开关；
- 对比provider/durable/context独占时间；
- 冻结 `skip_write_behind` 或另立write-behind规格；
- 重跑现有context suite确认Cursor/checkpoint无回归；
- 同步长期contracts与Stage 4.5总计划完成状态。

---

## 16. 测试矩阵

### 16.1 Segment算法

- OpenAI adapter、mock与real fixture的每个输出都是合法`RawProviderStreamItem`；
- raw union不继承`EventBase`、不能serialization、没有run/turn/reply/sequence；
- `RawProviderFailure`只进入sanitizer，不携带raw exception进入durable candidate；
- sanitizer任意时刻最多一个outstanding envelope，未adopt不得推进source cursor；
- 同kind、同block连续delta聚合；
- kind变化seal；
- block ID变化seal；
- data media type变化fail closed；
- soft codepoint target在source-item边界seal；
- 所有string的64KiB UTF-8 soft target；
- data byte target在source-item边界seal；
- hard content/canonical/source count cap；
- 同一append同时越过多个bound时seal reason遵循唯一优先级；
- quotes/backslashes/control characters在append前触发exact canonical boundary；
- single-source maximal fixture必然可表示；
- one oversized source item被sanitizer拒绝；
- timer在没有新provider item时主动flush；
- timer不取消唯一transport read；
- block End只seal、不天然commit；
- transaction batch boundary不改变segment layout；
- list parts只在seal时join，large fixture非O(n²)；
- empty Start/End不产生空segment。
- singleton准备失败不seal已有segment、不推进source/durable cursor；segment最终验证失败也不推进durable cursor。

### 16.2 四种内容

- ASCII、CJK、emoji与combining Unicode text；
- thinking/text交替保持global order；
- base64 data跨source item拼接完全一致；
- tool arguments在任意JSON字符边界切分；
- valid object、invalid JSON、non-object JSON保持原status；
- 同一tool call多个segment不重复parse；
- 多tool calls不交叉拼接。

### 16.3 Source receipt commitment

- first/last/count矩阵；
- gap、overlap、duplicate、reverse range拒绝；
- accumulator before/after drift拒绝；
- replay不声称独立重算segment内部raw draft identities；
- terminal count mismatch拒绝；
- terminal accumulator mismatch拒绝；
- durable event index drift拒绝；
- segment policy fingerprint mismatch拒绝；
- terminal source/live cursor与segment/domain/reducer contract任一漂移均在commit前拒绝；
- retry candidate bytes稳定。

### 16.4 Physical quote与settlement

- worst-case每个source item独立形成一个durable event；
- `RawProviderFailure`计入`max_source_items`，synthetic `ProviderErrorDraft`只计入独立tail；
- maximal content escaping不会同时按source payload与segment canonical bytes重复收费；
- Start、每个semantic event、normal terminal与recovery terminal的最坏batch排列全部进入`max_commit_batches`；
- candidate wrapper、bookkeeping、structural与recovery maximal fixture均不超过quote；
- actual settlement按真实segment/event/commit数闭合并释放未用余额；
- source adapter/synthetic counts、singleton/segment counts、candidate bytes与semantic batch identities逐项闭合；terminal source、charge events与physical settlement join同一measurement fingerprint；
- quote不足在dispatch前拒绝，不能等durable commit后才发现hard-cap breach。

### 16.5 Lifecycle/failure

- read+timer同时ready按age-before-envelope处理；
- read+cancel分别覆盖read先赢与cancel先赢；
- read+timer+cancel三者同时ready使用stable signal stamp；
- 任何时刻最多一个completed-but-not-adopted envelope；
- 同一次adopt transition产生两个candidate且第一项触发flush时，两项均已由Coordinator/handle持有；
- cancel先赢时discard unadopted且terminal只使用adopted prefix；
- cancel linearization后迟到read completion不生成envelope、不推进index/accumulator；
- commit `NONE`重试同一segment；
- `FULL`后caller cancel仍adopt/fold/cursor advance；
- `UNKNOWN/PARTIAL`保留owner并latch；
- projection artifact caller cancel-after-physical-FULL消费原成功结果，owned physical task真正cancel才报physical cancellation；
- provider error在open text/thinking/data/tool block后生成interrupted projection；
- Responses显式text/thinking done各生成唯一End，completed不重复End；done-only完整内容会生成唯一delta，已有delta与final payload不一致时fail closed；failure路径不伪造End；
- tool arguments先于具有非空name的Start时fail closed；
- Responses tool-call同时缺少call/item identity时fail closed且不生成随机ID；
- cancel physical drain后提交已接受tail；
- worker crash丢失未确认tail后recovered-interrupted；
- terminal不得越过sealed-unconfirmed segment；
- subscriber detach不停止segment worker；
- slow subscriber只detach；
- Host close drain active commit和physical operation；
- BLOCKED_UNTRUSTED禁止terminal/teardown。
- final provider error的bounded retry summary可历史恢复，raw trace/provider data与per-attempt durable event保持为零。

### 16.6 Semantic equality

- raw-draft assembler与segment reducer逐字段语义相等；
- terminal projection semantic fingerprint相等；
- segment schedule变化不改变provider semantic fingerprint；
- context manifest/provider payload相等；
- control disposition相等；
- completed tool execution eligibility相等；
- provider_error/cancel/runtime_error不得执行closed tool call；
- exact replay/restart结果相等。

### 16.7 Cursor/checkpoint

- segment/singleton明确分类为non_transcript；
- segment推进ledger continuity但不推进Cursor transcript semantic count/accumulator；
- terminal projection/control继续作为Cursor transcript-semantic envelopes；
- 旧registry fingerprint使process-local Cursor discard；
- same-H零read；
- terminal projection/control new-delta extension；
- Cursor/exact restore transcript/provider semantic相等；
- Cursor eviction不影响exact path；
- segment grouping不污染checkpoint semantic fingerprint。

### 16.8 Data-specific

- `image/png`、`audio/*`与generic application media type；
- data按UTF-8 bytes而非token seal；
- base64内容不被decode/re-encode；
- content SHA/byte count重算；
- 128KiB hard content与256KiB canonical event边界；
- 当前无production provider data output时，mock/deterministic adapter仍完整覆盖。

---

## 17. Architecture 与 grep gates

SEG1完成后production必须零匹配，不保留adapter-input allowlist：

```text
TextBlockDeltaEvent(
ThinkingBlockDeltaEvent(
DataBlockDeltaEvent(
ToolCallDeltaEvent(
EventType.TEXT_BLOCK_DELTA
EventType.THINKING_BLOCK_DELTA
EventType.DATA_BLOCK_DELTA
EventType.TOOL_CALL_DELTA
one_event_per_sanitized_source_item
semantic_item_count += len(events)
AsyncIterator[AgentEvent | TransportUsageReport]
```

另外禁止：

- Agent/Host/direct/window直接构造segment event；
- provider adapter import/construct任意durable model block EventBase；
- RawLLMTransport返回AgentEvent；
- provider adapter直接写EventLog；
- Coordinator/segment accumulator之外拼接durable model delta；
- LLMRuntime平行维护read/timer/cancel winner；
- 同时存在两个completed-but-not-adopted envelopes；
- sanitizer在Coordinator adopt前不可逆推进adopted source cursor；
- segment content超过hard byte cap；
- append后才发现canonical candidate超限而无prospective sizing；
- segment内部保存全部draft payload或fingerprint tuple造成第二payload放大；
- terminal/control读取process-local pending segment；
- source accumulator进入provider semantic fingerprint；
- segment flush reason进入terminal semantic identity；
- Data调用token estimator决定hard bound；
- tool-result delta误用model segment accumulator；
- segment被分类成transcript_semantic或进入Cursor semantic vector；
- Cursor成为segment恢复必需authority；
- 本章偷偷引入write-behind queue；
- old/new durable delta schema双写；
- 无DB reset读取旧ledger。

---

## 18. 性能验收

### 18.1 Deterministic writer

对现有 `8192 deltas * 16 chars = 131072 chars ~= 32768 V1 estimated tokens` fixture，关闭wall-clock age pressure的pure/reference assembler必须由V1 content target精确产生4个text segments。完整production runtime还同时执行`1s` oldest-unconfirmed-age hard bound；当source-item sanitize/coalescing本身跨过该deadline时，允许在相同content boundary之外多产生bounded age segment。该差异只改变non-transcript物理layout，不得改变terminal projection或provider semantic identity。

完整production benchmark必须满足：

```text
pure content-boundary text segment count = 4
production durable text segment count = 4..8
semantic commit count <= 8
ledger event delta <= 64
ordered semantic content grade = pass
terminal projection grade = pass
physical settlement = valid
account high-water = ledger high-water
```

相对正式batch-16 baseline：

- ledger event count至少下降 `99%`；
- logical semantic commit count至少下降 `98%`；
- median semantic commit-port wall至少下降 `80%`；
- model stream wall median不得回归；
- p95必须报告，但在PERF0分段归因成熟前不以单个cluster WAL异常判失败。

### 18.2 Real dogfood

三条轨迹各运行至少3次并报告median/range：

- durable writer waits/model call至少下降 `70%`；
- durable exclusive不得高于旧基线；
- provider first confirmed content latency不超过 `1.25s + provider first-item latency`；
- terminal drain p95不恶化；
- model/tool行为与原轨迹semantic grader一致；
- 不以更大的无界buffer、thread pool或关闭durability伪造提升。

### 18.3 Write-behind decision gate

只有segment完成后仍满足任一条件，才允许另立write-behind：

- foreground semantic commit wait仍占model stream wall `>=10%`；
- 每call中间segment commit累计阻塞 `>=1s`；
- p95单segment commit `>=250ms`；
- 并发session下critical writer queue持续积压。

否则正式记录：

```text
write_behind_decision = skip_after_segment_coalescing
```

---

## 19. Definition of Done

本 hard cut只有同时满足以下条件才完成：

1. 所有provider adapter、mock与real fixture只产`RawProviderStreamItem`，`RawLLMTransport`不再暴露`AgentEvent`；
2. sanitizer使用唯一single-outstanding prepare/adopt/discard协议，不会在Coordinator adopt前推进source truth；
3. 四类model delta全部由唯一`ModelStreamCoalescingCoordinator`与pure segment accumulator聚合；
4. read/timer/cancel三方winner、completed-but-not-adopted上限与foreground commit owner全部符合冻结算法；
5. prospective canonical sizing在append前执行，所有合法single-source fixture都可表示；
6. EventLog不再保存四类raw delta event，旧EventType、decoder与production reducer分支物理删除；
7. Start/End/error仍保持typed lifecycle与全局顺序；block End只seal、不天然commit；
8. source ranges完整覆盖terminal semantic item count，receipt accumulator before/after chain贯穿terminal/recovery；
9. terminal projection byte-for-byte/lossless重建四类内容，tool arguments parse/control行为不变；
10. provider error/cancel/runtime error仍为audit-only；
11. `NONE/FULL/UNKNOWN/PARTIAL/cancellation`矩阵全部通过；
12. physical reservation覆盖worst-case alternation、synthetic error、Start/terminal/recovery batch与bookkeeping；actual settlement反映segment降本；
13. segment/start/end保持`non_transcript`，Cursor只消费terminal projection/control等transcript-semantic events；
14. Cursor/checkpoint/replay的ledger continuity、semantic accumulator与provider semantic identity回归全部通过；
15. PostgreSQL已reset，无兼容reader/writer；
16. deterministic writer验收达标；
17. 全量non-real pytest、Ruff、`git diff --check`通过；
18. 全量real-LLM与全部dogfood开关通过，或每个外部服务skip/failure有独立证据；
19. 长期contracts、Inspector与性能总计划同步；
20. 已基于新数据明确决定是否需要write-behind。

---

## 20. 最终实施判断

本章优先级高于 write-behind。当前主要成本不是“上一批写入时没有继续读provider”，而是Pulsara把provider fragmentation durable化成了数千条events。正确顺序为：

```text
SEG0A adapter-private raw DTO hard cut
    -> SEG0B pure Coordinator/segment/arbiter contracts
    -> SEG1 durable segment vertical hard cut + DB reset
    -> deterministic writer baseline
    -> real dogfood re-measure
    -> Cursor/checkpoint domain regression
    -> only if still justified: one-inflight write-behind
    -> Stage 5 ContextSource Ownership
```

Segment hard cut不会削弱EventLog authority。它重新定义更合理的durable事实：模型在某个logical block中产生了一段连续内容；provider恰好用多少个SSE/SDK delta发送这段内容，只保留为bounded source range与accumulator attribution，不再一比一占据durable ledger rows。
