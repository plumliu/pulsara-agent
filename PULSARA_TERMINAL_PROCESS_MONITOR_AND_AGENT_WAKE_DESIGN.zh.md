# Pulsara Terminal Monitor、Host Typed Ingress 与 Autonomous Continuation 实施规格

> 状态：TM0-TM5 已完成代码落地、阶段 gate、串行全量回归与 Definition of Done 复核
> 日期：2026-07-21
> 范围：managed terminal process 的持续、受限 monitor，Host 统一 ingress 与受限自动续跑
> 性质：这不是一个孤立的 terminal tool 增强，而是一次 Host typed ingress / autonomous run-entry hard cut

## 1. 问题与代码真值

Pulsara 当前支持：

1. `terminal` 在 `yield_time_ms` 到达后保留进程并返回 `process_id`；
2. `terminal_process list/log/poll/wait` 主动查看或等待进程；
3. yielded process 自然结束后写入 `TerminalProcessCompletedEvent`；
4. 后续上下文通过 transcript completion note 获知进程结束。

模型若启动长时间训练、benchmark 或后端服务，目前只能反复执行短 `wait`，或者结束当前 turn 并等待用户再次发言。它不能表达“持续观察这个进程；每当出现一批有意义的新输出、周期性检查点或最终完成时再通知我”。

本轮代码核对确认，不能在现有 terminal tool 上直接加一个 timer callback：

* `HostSession.run_turn()` 直接检查 active/pending 状态后创建 boundary task，不存在统一 ingress queue；
* Host run boundary 强制 `CurrentUserMessageFact.source_kind="host_user_input"`，没有合法的 autonomous Host run-entry；
* `RuntimeRequestKind` 没有 terminal observation request，且 runtime request carrier 本身不能替代 RunStart provenance；
* `TerminalProcessCompletedEvent` 当前一经 transcript reducer fold 就立即追加模型可见 completion note；
* `OutputAccumulator` 保存无界 `_text_parts`，snapshot 每次 join 全文，未换行输出不会发布；
* yielded 后 initial `output_callback` 被清除，无法成为长期观察 authority；
* `poll/log/wait` 被分类为 observe action，但 monitor 会持续产生未来 notification，并可能安排付费模型调用，不能沿用只读权限。

因此，本规格将原来的 terminal-specific wake scheduler 调整为：

```text
SanitizedOutputJournal
        |
        v
TerminalMonitorCoordinator
 PREPARED_DORMANT -> ACTIVE_READY <-> FIRING -> durable observations -> TERMINATED
        |
        v
HostIngressCoordinator
 human / interaction resume / runtime notification 统一仲裁
        |
        v
typed Host run boundary
        |
        v
ProviderInput
```

### 1.1 为什么删除 `terminal_process.watch`

旧草案把 `watch` 定义为“注册后立即返回，条件满足时最多唤醒一次”。它与现有 `wait` 的差异只剩执行权归属：`wait` 阻塞当前 tool call，`watch` 把同一次等待移到 Host 后台。这个差异真实存在，但不足以支撑一个独立的长期产品边界，也迫使模型在每次唤醒后重新注册，增加 tool round、durable lifecycle 和提示词成本。

本规格物理删除公开 `terminal_process.watch`、全部旧 `TerminalProcessWatch*` DTO/event/action 和 one-shot re-arm 语义，持续订阅最终由独立的 `terminal_monitor.register` 拥有：

| 能力 | `wait` | `monitor` |
|---|---|---|
| 控制权 | 当前 tool call 保持等待 | 注册后立即返回，HostSession 持续拥有 |
| 结果位置 | 当前 run 的一次 ToolResult | 多次 typed notification，可进入 safe point、future autonomous run 或 human merge |
| 生命周期 | 一次调用/timeout 后结束 | completion、cancel、expiry 或 session close 才结束 |
| 输出游标 | 本次 observation cursor | 每次 committed observation 推进 monitor cursor |
| 恢复 | tool call 取消即结束 | 跨detach持续；restart按durable state精确恢复已确认结果或typed终结，不伪造OS adoption |
| 权限 | ordinary terminal observe | autonomy/scheduling permission + bounded delivery budget |

`terminal_process.wait`继续服务“我愿意在当前 run 内等最多几十秒”；`terminal_monitor.register`服务“我先释放当前执行权，之后持续把有意义的状态变化告诉我”。两者共享journal、cursor、condition evaluator和typed receipt，但不共享run ownership或public tool owner。

## 2. 目标

V1 提供 bounded、persistent 的 terminal monitor：

```text
start/yield process
    -> prepare dormant monitor registration
    -> registration + ToolResult terminal atomic FULL
    -> activate and immediately recheck
    -> heartbeat/output/completion condition wins
    -> durable bounded observation N
    -> HostIngressCoordinator selects human or runtime ingress
    -> progress observation disposition FULL
    -> advance consumed cursor; re-arm or enter completion-only
    -> repeat within rate/budget bounds
    -> completion/cancel/expiry/session-close terminalizes monitor
```

必须满足：

* 不 busy-poll；
* 不为每个 stdout chunk 调用模型或写一条 durable event；
* 只返回 baseline cursor 之后的新 sanitized output；
* registration、每次 observation、delivery、cursor advancement 和 explicit observation 均有唯一 durable linearization；
* user input、interaction resume 与 runtime notification 不能通过不同入口竞争；
* 一个模型可见 terminal observation 只有一个 projection owner；
* 不允许 monitor 造成无限自动调用、并发 run、无界通知积压或权限绕过；
* HostSession、workspace、process 与 runtime ledger ownership 可验证；
* FULL/NONE/UNKNOWN/PARTIAL、caller cancellation、close 和 restart 都有唯一结果。

## 3. 非目标

V1 不做：

* Host 进程崩溃后的 OS process adoption；
* durable PID registry；
* 任意正则表达式或模型生成的日志匹配器；
* 无界周期订阅；
* 任意 shell/regex/filter script 作为 monitor 判定器；
* child/subagent 注册 autonomous monitor；
* 多个 Agent 并发消费同一个 monitor；
* 将完整训练日志直接注入模型上下文；
* 在已经 dispatch 的 model call 中途注入 notification；
* 通用 cron、recurring scheduler 或 `/loop`；
* 用 monitor 替代 stop/close/terminal permission。

`PRE_MODEL_STEP` safe-point delivery 纳入最终产品方向，但在本规格中作为 TM4 的独立 gate：它只能发生在前一轮 tool/control 已 FULL、下一次 context compile 尚未开始时，绝不能修改已经冻结或 dispatch 的 provider input。若 TM4 尚未完成，monitor notification 必须退化为 current run 结束后的 Host ingress，不能临场注入已冻结 provider payload。

## 4. 中央不变量

### 4.1 Host ingress 是唯一入口

所有会改变 Host execution state 的输入都必须进入同一个 `HostIngressCoordinator`：

* human new-run input；
* pending interaction resume；
* confirmed runtime notification；
* stop/close control gate。

`HostSession.run_turn()` 不再直接创建 boundary task，只提交 human ingress 并等待结果。任何 terminal coordinator、completion callback 或 recovery owner 均不得直接调用 `run_turn()`。

ACTIVE run的PRE_MODEL_STEP也必须向同一个coordinator申请notification lease；agent/model loop不得直接扫描notification projection并拼message。该lease不创建新Host ingress/RunStart，但与new-run admission共享human priority、permission、chain-state和notification-head CAS。

### 4.2 Monitor registration 与 ToolResult terminal 原子闭合

模型只有在 durable ToolResult 声明注册成功时，monitor 才能存在。唯一顺序为：

```text
prepare dormant registration
    -> atomic commit(
           TerminalProcessMonitorRegisteredEvent,
           monitor-lifecycle reservation acquire,
           notification projection transition,
           ToolResult terminal projection,
           ToolResultEnd,
           physical settlement
       )
    -> FULL: activate
    -> immediate recheck against frozen baseline
```

禁止：

* 先 activate monitor，再尝试写 ToolResult；
* ToolResult FULL 后用第二个事务补 registration；
* tool implementation 自己绕过 `_commit_tool_terminal()` 调用 EventLog；
* 在 durable lifecycle 尚未落地时向 production capability 暴露 `monitor`。

### 4.3 Observation FULL 前不得进入 Host ingress，progress FULL 后不得终结 monitor

monitor 从 `ACTIVE_READY` 进入 `FIRING` 后，stable observation candidate、physical reservation 和 retry owner 必须完整保留。只有 observation FULL 后才能发布 runtime notification。

progress/heartbeat observation FULL 只推进 `last_committed_observation_ordinal` 和 `last_observation_cursor`；`last_consumed_cursor` 只有在模型可见delivery或dominant receipt FULL后才推进。delivery/receipt FULL 后，未达到progress cap时回到 `ACTIVE_READY`，达到cap时进入`ACTIVE_COMPLETION_ONLY`。若completion/expiry在pending progress交付前到达，terminal delta从`last_consumed_cursor`开始，并与旧progress的superseded disposition同批FULL，不能从较新的observation cursor开始而丢掉尚未交付的正文。progress不是terminal outcome；只有process completion、explicit cancel、monitor expiry、session close或unrecoverable authority failure才终结monitor。

### 4.4 Completion 只是 lifecycle authority

`TerminalProcessCompletedEvent` 不再直接产生 transcript message。它只证明 process terminal lifecycle、最终状态和 output cursor。

模型可见内容只能由两种 owner 产生：

1. `poll/log/wait` 的 committed ToolResult terminal projection；
2. committed `HostRunIngress` 中的 typed runtime request。

因此 explicit observation 与 autonomous/human-merged delivery 不会在 append-only transcript 中重复。

### 4.5 Output authority 与 UI streaming 分离

`SanitizedOutputJournal` 是 cursor、monitor observation 和 receipt 的唯一 output authority。UI subscriber、initial terminal streaming callback 和 artifact renderer 都只是 reader；reader detach 不影响 journal。

### 4.6 User priority 只在统一 linearization 点定义

在同一次 ingress selection 临界区中：

```text
close/stop gate
    > valid pending-interaction resume
    > queued human new-run ingress
    > confirmed runtime notification
```

一旦某个 ingress 已取得 `PREPARING` admission ownership，之后到达的 human input不能回到过去抢占它；它进入下一次 selection。所谓“用户优先”是对同一 selection snapshot 的确定性优先级，不是对已经 linearized 的 RunStart 进行撤销。

### 4.7 V1 terminal monitor authority 只能来自一个 runtime ledger

Host owner相同不代表ledger相同。V1要求monitor caller、process origin、completion writer、observation writer、notification projection与Host RunStart全部绑定Host main RuntimeSession。main agent也不能monitor child-origin process；跨ledger支持留待独立hard cut。

### 4.8 Capacity 必须先reserve、后发布、最终release

background process ID与monitor registration对模型可见前，必须分别拥有completion-head和monitor-lifecycle reservation。monitor reservation贯穿整个持续订阅，只在monitor terminal FULL后释放；completion/delivery完成后再按typed transition释放process head。capacity不能只增不减，也不能用queue eviction代替release。

### 4.9 Monitor observation 不等于一次模型调用

持续monitor有三层独立线性化：

```text
journal progress
    -> durable monitor observation
    -> delivery disposition
    -> optional model sampling
```

observation FULL只证明“有一份可交付事实”，不会直接调用模型。它可以被：

* 当前run的PRE_MODEL_STEP safe point消费；
* idle Host的autonomous RunStart消费；
* 下一human run merge；
* explicit `poll/log/wait` receipt消费；
* permission/budget gate保留为pending。

因此monitor可以持续、多次观察，而模型调用仍受one-pending、debounce、确定性progress sliding window、human priority和wake-chain budget约束。UI journal stream完全不进入该模型delivery链。

## 5. Sanitized Output Journal

### 5.1 替换 OutputAccumulator

TM0 物理删除 `OutputAccumulator` 作为 production authority，新增 `SanitizedOutputJournal`：

```python
class SanitizedOutputJournal:
    # one incremental UTF-8 decoder
    # one stateful ANSI/control normalizer
    # one stateful secret sanitizer
    # bounded immutable retained segments
    # cumulative sanitized char/UTF-8-byte cursors
    # incremental prefix digest
    # condition/revision notification
    # mandatory bounded canonical spool for managed/background processes
    ...
```

Journal 必须满足：

* 内存受 `max_retained_segments/max_retained_chars/max_retained_utf8_bytes` 约束；
* append 和 snapshot 不 join 全部历史；
* cursor 是 process output 生命周期内的绝对位置；
* retention eviction 只影响可直接返回的 delta，不倒退 cursor；
* output callback 被移除时 journal 仍继续推进；
* reader 通过 condition/revision 等待，不为每个 monitor 建 thread；
* managed/background process存活期间必须写入process-owned bounded sanitized spool；
* spool有固定quota、retained-head cursor与overflow gap，不得退回无界内存；
* artifact export是spool之上的可选持久化层，不能替代live spool authority。

### 5.2 Canonicalization contract

```python
class TerminalOutputSanitizationContractFact(FrozenFactBase):
    schema_version: Literal["terminal_output_sanitization_contract.v1"]
    contract_id: str
    contract_version: int
    utf8_error_policy: Literal["replace"]
    ansi_normalization_contract_fingerprint: str
    control_character_policy: Literal["preserve_newline_normalize_cr"]
    secret_redaction_contract_fingerprint: str
    maximum_sanitizer_carry_utf8_bytes: int
    oversized_sensitive_token_policy: Literal["redact_entire_token"]
    partial_line_policy_fingerprint: str
    contract_fingerprint: str
```

sanitization 必须是 stateful 的。secret token、ANSI sequence 或 UTF-8 code point 跨 provider/process chunk 时，结果必须与一次性输入完整字节流相同。任何尚未越过 sanitizer safe frontier 的字符不得发布给 cursor reader。

若敏感 token 超过 bounded carry，journal 输出固定 `[REDACTED_OVERSIZE_TOKEN]` 并推进；不得为了等待闭合 token 无界保留 raw bytes。

### 5.3 Partial-line sealing

V1 支持无换行 progress output。immutable journal segment 可由以下原因 seal：

```text
line_boundary
carriage_return_boundary
partial_line_quiet
explicit_observation_boundary
segment_size_boundary
process_terminal
```

process-local segment形状固定为：

```python
@dataclass(frozen=True, slots=True)
class TerminalOutputJournalSegment:
    segment_index: int
    start_cursor: TerminalOutputCursorFact
    end_cursor: TerminalOutputCursorFact
    sanitized_text: str
    seal_reason: str
    content_sha256: str
```

segment不是AgentEvent；只有bounded monitor observation/receipt/completion引用cursor或preview。`content_sha256`必须由exact UTF-8 text重算；字符数与UTF-8字节数分别由end/start cursor offset之差唯一派生，并与text精确闭合，不再另存计数字段。

规则：

* newline/CR 到达时立即 seal sanitizer 已确认的 safe prefix；
* 未闭合行经过 `partial_line_quiet_seconds` 后可 seal safe prefix；
* explicit `poll/log/wait` 可在 sanitizer safe frontier 强制 seal；
* 单 segment 达到 UTF-8 byte bound 时强制 seal；
* process terminal 时 flush decoder/sanitizer 并 seal；
* quiet/heartbeat/expiry 使用 monotonic clock；UTC 只进入 durable attribution；
* segment layout 是物理 attribution，不进入 output text semantic fingerprint。

### 5.4 Cursor 与 delta

```python
class TerminalOutputStreamIdentityFact(FrozenFactBase):
    schema_version: Literal["terminal_output_stream_identity.v1"]
    process_id: str
    journal_instance_id: str
    stream_identity_fingerprint: str


class TerminalOutputCursorFact(FrozenFactBase):
    schema_version: Literal["terminal_output_cursor.v1"]
    stream_identity: TerminalOutputStreamIdentityFact
    sanitized_char_offset: int
    sanitized_utf8_byte_offset: int
    canonical_prefix_sha256: str
    sanitizer_contract_fingerprint: str
    cursor_fingerprint: str


class TerminalOutputDeltaSemanticFact(FrozenFactBase):
    schema_version: Literal["terminal_output_delta_semantic.v1"]
    availability: Literal["available"]
    requested_start_cursor: TerminalOutputCursorFact
    available_start_cursor: TerminalOutputCursorFact
    end_cursor: TerminalOutputCursorFact
    output_preview: str
    delta_content_sha256: str
    truncated: bool
    delta_semantic_fingerprint: str


class UnavailableRecoveredTerminalOutputDeltaFact(FrozenFactBase):
    schema_version: Literal[
        "unavailable_recovered_terminal_output_delta.v1"
    ]
    availability: Literal["unavailable_recovered"]
    requested_start_cursor: TerminalOutputCursorFact
    terminal_cursor: TerminalOutputCursorFact
    recovery_reason: Literal[
        "spool_range_evicted",
        "spool_write_failed",
        "spool_writer_queue_overflow",
        "spool_fsync_timeout",
        "spool_terminal_drain_timeout",
        "artifact_gc_confirmed",
    ]
    delta_semantic_fingerprint: str


class TerminalOutputDeltaAttributionFact(FrozenFactBase):
    schema_version: Literal["terminal_output_delta_attribution.v1"]
    delta_semantic_fingerprint: str
    full_output_artifact_ref: ContextArtifactReferenceFact | None
    retained_segment_first_index: int | None
    retained_segment_last_index: int | None
    attribution_fingerprint: str


TerminalMonitorObservationOutputFact = Annotated[
    TerminalOutputDeltaSemanticFact
    | UnavailableRecoveredTerminalOutputDeltaFact,
    Field(discriminator="availability"),
]
```

`canonical_prefix_sha256` 是从 journal 起点到该 cursor 的完整 sanitized UTF-8 stream digest，不依赖 `append()` chunk 或 journal segment boundary。它是 terminal supervisor 生成的 receipt commitment，不宣称仅凭 digest 可在没有前缀内容时继续计算。

所有同一delta中的cursor必须嵌入完全相同的`TerminalOutputStreamIdentityFact`。以下值只允许作为process-local property或wire lowering的派生值，禁止重新写入authority DTO：

```text
available_delta_chars = end.char_offset - available_start.char_offset
available_delta_utf8_bytes = end.byte_offset - available_start.byte_offset
omitted_delta_chars = available_start.char_offset - requested_start.char_offset
omitted_delta_utf8_bytes = available_start.byte_offset - requested_start.byte_offset
```

factory还必须验证offset非负单调、prefix digest continuity以及preview/hash与retained canonical bytes一致。`TerminalOutputStreamIdentityFact`是process/journal identity的唯一共享carrier；同一DTO不得再复制`process_id`与`journal_instance_id`。

artifact locator、retained segment indexes和page layout只进入attribution；monitor observation semantic只嵌上述available/unavailable output union，不嵌物理attribution。

若 baseline 已被内存 retention 淘汰：

* `available_start_cursor > requested_start_cursor`；
* omitted counts 精确非零；
* `truncated=True`；
* 不能把 retained head 伪装成 baseline；
* full output 只通过 artifact/log permission path hydrate。

### 5.5 Bounded spool 与 recovery authority

```python
class TerminalOutputSpoolPolicyFact(FrozenFactBase):
    schema_version: Literal["terminal_output_spool_policy.v1"]
    maximum_spool_utf8_bytes: int
    page_utf8_bytes: int
    maximum_pending_spool_utf8_bytes: int
    page_fsync_timeout_ms: int
    terminal_drain_timeout_ms: int
    overflow_policy: Literal["evict_oldest_complete_page_with_gap"]
    file_permission_mode: Literal["0600"]
    page_commit_contract_fingerprint: str
    retention_horizon_policy_fingerprint: str
    policy_fingerprint: str


class TerminalOutputSpoolGapFact(FrozenFactBase):
    schema_version: Literal["terminal_output_spool_gap.v1"]
    start_cursor: TerminalOutputCursorFact
    end_cursor: TerminalOutputCursorFact
    reason: Literal[
        "quota_evicted",
        "write_enospc",
        "write_permission_denied",
        "write_io_error",
        "writer_queue_overflow",
        "fsync_timeout",
        "terminal_drain_timeout",
    ]
    gap_fingerprint: str


class TerminalOutputSpoolWriterStateFact(FrozenFactBase):
    schema_version: Literal["terminal_output_spool_writer_state.v1"]
    stream_identity: TerminalOutputStreamIdentityFact
    journal_end_cursor: TerminalOutputCursorFact
    successfully_spooled_cursor: TerminalOutputCursorFact
    retained_start_cursor: TerminalOutputCursorFact
    writer_state: Literal[
        "active",
        "degraded",
        "closed",
        "authority_untrusted",
    ]
    latest_gap: TerminalOutputSpoolGapFact | None
    spool_policy_fingerprint: str
    state_fingerprint: str


class TerminalOutputRecoveryReferenceFact(FrozenFactBase):
    schema_version: Literal["terminal_output_recovery_reference.v1"]
    spool_locator_id: str
    spool_writer_state: TerminalOutputSpoolWriterStateFact
    spool_manifest_reference: ContextArtifactReferenceFact | None
    spool_policy_fingerprint: str
    recovery_reference_fingerprint: str
```

规则：

* yielded/background ownership对模型可见前必须已建立bounded spool；
* `spool_locator_id`由terminal spool store解析，durable fact不得内嵌任意host path；
* spool只保存sanitized canonical bytes，不保存raw environment/output；
* journal reader只负责sanitization、memory append和向bounded spool queue转交immutable page candidate；它不得await文件写入或`fsync`；
* `journal_end_cursor`在sanitized bytes进入journal后推进；`successfully_spooled_cursor`只在对应page完成temp write、长度/checksum校验、bounded `fsync`、atomic rename与manifest CAS后推进；两者不得由同一callback顺带推进；
* spool writer使用独立session-owned bounded owner。queue满、ENOSPC、permission failure、I/O error或`fsync`超时均冻结typed gap并进入`degraded`，不能阻塞child stdout reader或改变被监控进程行为；
* degraded后live monitor仍可读取bounded memory；该range被memory eviction后，只能生成typed unavailable delta，不得假称已经spooled；
* quota overflow明确推进retained-head并记录gap；不能为了active monitor无界pin全部输出；
* completion outer attribution保存`TerminalOutputRecoveryReferenceFact`；
* monitor terminal + configured recovery horizon到达前，不主动GC仍被reference覆盖的retained pages；
* spool GC还要求notification account中已无该process/monitor reservation且无FIRING/reconciliation owner；
* quota eviction、已durable分类的spool write/queue/fsync gap或confirmed artifact GC可使旧baseline range不可恢复，此时只能生成`UnavailableRecoveredTerminalOutputDeltaFact`；
* unavailable branch只证明completion/status/cursors和缺失原因，不保存伪造preview、delta hash或available-start关系；
* unavailable wire payload固定`output_preview=""`、preview bytes `0`、SHA-256(empty)、`truncated=True`；delta char/byte count只由terminal-baseline cursor差计算，全部计入omitted；
* 若exact retained range与historical sanitizer binding均存在，recovery可重建普通available delta；
* 已进入FIRING但结果UNKNOWN/PARTIAL的candidate不得根据spool重新生成，必须走原owner reconciliation。

page commit唯一算法是：写入同目录临时文件，校验exact byte length与content hash，执行bounded `fsync`，atomic rename，再CAS manifest；manifest只能引用全部完成该算法的immutable page。启动时未被manifest引用的partial temp page直接清理，不得推进successful cursor。已commit page/hash与manifest冲突时进入`authority_untrusted`并阻止exact recovery；它不阻止process lifecycle completion或typed `authority_untrusted` monitor termination/account release，但不得伪造可交付terminal observation。

process terminal时，spool owner最多等待`terminal_drain_timeout_ms`。deadline内仍未确认的tail冻结`terminal_drain_timeout` gap，随后completion使用live memory中的available branch或stable unavailable branch继续提交；spool failure永远不能阻塞completion terminalization。

recovery reason由gap branch唯一映射：`quota_evicted -> spool_range_evicted`；三种write failure -> `spool_write_failed`；queue/fsync/terminal-drain分别映射同名unavailable reason。caller不得自由选择较温和的reason。

`artifact_gc_confirmed`必须有durable GC/tombstone authority。artifact无tombstone丢失、已commit page hash冲突、codec/sanitizer historical binding冲突均属于authority-untrusted并fail closed，不能降级成output unavailable。operational metrics至少记录journal/spooled cursor lag、pending spool bytes、degraded reason、gap bytes、fsync latency与terminal drain timeout。

### 5.6 UI-only stream contract

持续 UI tail 与模型monitor共享journal authority，但不是同一delivery owner。TM5公开结构化channel：

```text
x.pulsara/terminal_monitor_event
    journal_delta
    monitor_observation_committed
    monitor_delivery_disposition
    process_completed
    monitor_terminated
```

subscriber使用`stream_identity + terminal cursor + notification projection revision`作为reconnect cursor。服务端只从retained journal/spool和durable projection replay；range已经淘汰时发送typed gap，不伪造连续流。每subscriber拥有独立bounded queue，overflow时丢弃该subscriber的中间UI delta并发送最新cursor/gap；不得反压journal writer、monitor FIRING owner或Host ingress。UI attach/detach既不创建模型notification，也不改变monitor cursor、automatic delivery budget或durable disposition。

## 6. Typed Observation Receipt

### 6.1 Receipt schema

`terminal_process poll/log/wait` 的最终 ToolResult 必须携带：

```python
class RunningTerminalProcessStateFact(FrozenFactBase):
    schema_version: Literal["running_terminal_process_state.v1"]
    status: Literal["running"]
    state_fingerprint: str


class TerminalProcessLifecycleOutcomeFact(FrozenFactBase):
    schema_version: Literal["terminal_process_lifecycle_outcome.v1"]
    status: Literal["success", "error", "timeout", "killed"]
    exit_code: int
    kill_reason: Literal[
        "user_tool_kill", "teardown", "lifetime_watchdog"
    ] | None
    outcome_fingerprint: str


TerminalProcessObservedStateFact = Annotated[
    RunningTerminalProcessStateFact | TerminalProcessLifecycleOutcomeFact,
    Field(discriminator="status"),
]


class InlineTerminalObservationCoverageFact(FrozenFactBase):
    schema_version: Literal["inline_terminal_observation_coverage.v1"]
    coverage_kind: Literal["inline"]
    covered_start_cursor: TerminalOutputCursorFact
    covered_end_cursor: TerminalOutputCursorFact
    visible_content_sha256: str
    coverage_fingerprint: str


class ArtifactTerminalObservationCoverageFact(FrozenFactBase):
    schema_version: Literal["artifact_terminal_observation_coverage.v1"]
    coverage_kind: Literal["artifact"]
    covered_start_cursor: TerminalOutputCursorFact
    covered_end_cursor: TerminalOutputCursorFact
    artifact_reference: ContextArtifactReferenceFact
    covered_range_content_sha256: str
    artifact_codec_contract_fingerprint: str
    coverage_fingerprint: str


class UnavailableTerminalObservationCoverageFact(FrozenFactBase):
    schema_version: Literal["unavailable_terminal_observation_coverage.v1"]
    coverage_kind: Literal["unavailable_recovered"]
    unavailable_delta_semantic_fingerprint: str
    coverage_fingerprint: str


TerminalProcessObservationCoverageFact = Annotated[
    InlineTerminalObservationCoverageFact
    | ArtifactTerminalObservationCoverageFact
    | UnavailableTerminalObservationCoverageFact,
    Field(discriminator="coverage_kind"),
]


class TerminalProcessObservationSemanticFact(FrozenFactBase):
    schema_version: Literal["terminal_process_observation_semantic.v1"]
    requested_start_cursor: TerminalOutputCursorFact | None
    observed_start_cursor: TerminalOutputCursorFact
    observed_end_cursor: TerminalOutputCursorFact
    output_coverage: TerminalProcessObservationCoverageFact
    observed_state: TerminalProcessObservedStateFact
    observation_semantic_fingerprint: str


class TerminalProcessObservationReceiptFact(FrozenFactBase):
    schema_version: Literal["terminal_process_observation_receipt.v1"]
    observation_semantic: TerminalProcessObservationSemanticFact
    action_kind: Literal["poll", "log", "wait"]
    origin_tool_call_id: str
    completion_event_reference: ContextEventReferenceFact | None
    receipt_fingerprint: str
```

`timed_out`不是durable字段；它唯一等价于`outcome.status == "timeout"`。中央factory冻结terminal outcome矩阵：success要求exit 0；error要求正exit且不等于124；timeout要求exit 124且reason null；killed要求负exit且reason required。running branch没有exit或kill字段。

该 fact 嵌入 hydrated tool terminal projection，并由 `ToolResultEndEvent` required reference join。无需再建立一个可与 ToolResult 漂移的第二正文事件。

### 6.2 Dominance

receipt 只可消费满足全部条件的 pending terminal observation：

* receipt、pending monitor observation与completion cursor的stream identity完全相同；
* available pending observation要求`receipt.observed_start_cursor <= pending.available_start_cursor`；
* available pending observation要求`receipt.observed_end_cursor >= pending.end_cursor`；
* inline coverage的covered range必须精确等于receipt start/end，且ToolResult terminal document中的可见正文hash与coverage一致；
* artifact coverage的covered range必须精确等于receipt start/end；artifact FULL、codec/hydration binding和covered range hash必须在ToolResult commit前验证；
* tail-only `90..110`不能消费pending `0..100`，即使end cursor更大；
* observation cursor 处的 prefix digest 与 journal authority一致；
* running receipt的completion reference必须为null，且不能消费completion observation；
* terminal receipt的completion reference required；tool需先确认同ledger completion FULL，不能把物理exit冒充durable terminal authority；
* completion observation 要求 receipt 的nested lifecycle outcome与completion semantic逐字段一致，引用同一个confirmed `TerminalProcessCompletedEvent`，并独立满足output coverage；
* unavailable-recovered completion只能由复制exact unavailable delta semantic fingerprint的unavailable coverage消费，不能用空inline正文冒充覆盖；
* broad `list` 不生成 receipt，也不消费 notification。

ToolResult FULL 后，session-owned observation owner提交 `explicitly_observed` disposition。NONE 保留稳定 candidate重试；UNKNOWN/PARTIAL latch，不能在 durable结果不确定时自动启动新 run。

ToolResult terminal batch还必须写入唯一的cursor transition carrier；不能只在process-local monitor store中推进baseline：

```python
class TerminalProcessMonitorReceiptAppliedEvent(EventBase):
    schema_version: Literal[
        "terminal_process_monitor_receipt_applied.v1"
    ]
    registration_event_reference: ContextEventReferenceFact
    tool_result_end_event_identity: StableEventIdentityFact
    receipt_fingerprint: str
    observed_end_cursor: TerminalOutputCursorFact
    pending_observation_event_reference: ContextEventReferenceFact | None
    monitor_state_transition: TerminalProcessMonitorStateTransitionFact
```

该event与`ToolResultEndEvent`、eligible `explicitly_observed` disposition、account/head transition处于同一个accounted terminal batch。stable ID由`monitor_id + ToolResultEnd.id`确定性派生；event不得增加observation ordinal，且registration、ToolResult、pending observation必须属于同一runtime ledger。Reducer先从hydrated ToolResult terminal document重验receipt，再验证`observed_end_cursor`和state transition；event自身不能用裸cursor自证已经覆盖provider实际看到的内容。

对active monitor，dominant receipt还通过同一个`TerminalProcessMonitorStateTransitionFact`单调推进`last_consumed_cursor`与`last_observation_cursor`到`max(previous, observed_end_cursor)`，但不增加monitor observation ordinal：

* 有pending progress时，同时消费该observation；若progress count低于cap回到`ACTIVE_READY`，否则进入`ACTIVE_COMPLETION_ONLY`；
* 无pending progress时，只推进baseline，防止monitor稍后把模型刚通过`poll/log/wait`看到的输出再次通知；原state若为`ACTIVE_COMPLETION_ONLY`则不得借receipt重新开启progress；
* terminal receipt按completion矩阵终结matching monitor pending delivery；
* state transition与ToolResultEnd/explicit disposition/account projection同一transaction/CAS，不能在ToolResult FULL后process-local改cursor。

active run 内出现 receipt 时，Host ingress gate 会在该 run terminal 前保持关闭，因此 disposition有机会先于下一 autonomous dispatch FULL；这不是依赖概率，而是统一 ingress state machine 的 invariant。

## 7. Host Typed Ingress Hard Cut

### 7.1 Run ingress union

```python
class HostRunIngressSemanticFact(FrozenFactBase):
    schema_version: Literal["host_run_ingress_semantic.v1"]
    ordered_current_input_semantic_fingerprints: tuple[str, ...]
    ingress_semantic_fingerprint: str


class HostIngressItemPlacementFact(FrozenFactBase):
    schema_version: Literal["host_ingress_item_placement.v1"]
    item_kind: Literal["human_input", "runtime_notification", "runtime_request"]
    item_semantic_fingerprint: str
    accepted_ingress_ordinal: int
    placement_fingerprint: str


class HostRunIngressAttributionFact(FrozenFactBase):
    schema_version: Literal["host_run_ingress_attribution.v1"]
    ingress_id: str
    host_session_id: str
    conversation_id: str | None
    observed_at_utc: str
    ingress_semantic_fingerprint: str
    ordered_item_placements: tuple[HostIngressItemPlacementFact, ...]
    attribution_fingerprint: str


class HumanRunIngressFact(FrozenFactBase):
    schema_version: Literal["human_run_ingress.v1"]
    ingress_kind: Literal["human"]
    semantic_identity: HostRunIngressSemanticFact
    attribution: HostRunIngressAttributionFact
    human_message: HumanInputWireSemanticFact
    attached_runtime_notifications: tuple[
        HostRuntimeNotificationAttachmentFact, ...
    ]
    fact_fingerprint: str


class RuntimeRequestRunIngressFact(FrozenFactBase):
    schema_version: Literal["runtime_request_run_ingress.v1"]
    ingress_kind: Literal["runtime_request"]
    semantic_identity: HostRunIngressSemanticFact
    attribution: HostRunIngressAttributionFact
    runtime_request: RuntimeRequestWireSemanticFact
    source_notifications: tuple[HostRuntimeNotificationAttachmentFact, ...]
    autonomy_delivery: TerminalAutonomousDeliveryFact
    fact_fingerprint: str


HostRunIngressFact = Annotated[
    HumanRunIngressFact | RuntimeRequestRunIngressFact,
    Field(discriminator="ingress_kind"),
]
```

每层只有一个自身 fingerprint：`HostRunIngressSemanticFact` 的 semantic fingerprint不含Host/session/event attribution；outer ingress fact的`fact_fingerprint`覆盖nested semantic与attribution。outer validator必须要求：

* attribution中复制的semantic fingerprint等于nested semantic identity；
* ordered placements按`accepted_ingress_ordinal, stable item ID`排序且unique；
* placements中的ordered semantic fingerprints精确等于semantic identity中的ordered tuple；
* outer union discriminator与RunStart boundary kind精确相等；
* placement只证明item kind、semantic identity和因果ordinal，不再拥有source evidence。

notification attachment使用现有runtime-observation semantic，但补齐Host ingress attribution：

```python
class HostRuntimeNotificationAttachmentFact(FrozenFactBase):
    schema_version: Literal["host_runtime_notification_attachment.v1"]
    observation_wire_semantic: RuntimeObservationWireSemanticFact
    source_event_references: tuple[ContextEventReferenceFact, ...]
    wake_chain_id: str | None
    attachment_fingerprint: str


class HostAutonomousRuntimeRequestOwnerFact(FrozenFactBase):
    schema_version: Literal["host_autonomous_runtime_request_owner.v1"]
    owner_kind: Literal["host_autonomous_run_entry"]
    host_session_id: str
    conversation_id: str | None
    wake_chain_id: str
    ordered_attachment_fingerprints: tuple[str, ...]
    owner_fingerprint: str


class ActiveRunMonitorSafePointCommitGuardFact(FrozenFactBase):
    schema_version: Literal["active_run_monitor_safe_point_commit_guard.v1"]
    runtime_session_id: str
    run_start_event_reference: ContextEventReferenceFact
    active_segment_id: str
    active_segment_generation: int
    expected_host_state_generation: int
    expected_next_model_call_index: int
    expected_llm_lifecycle_generation: int
    expected_termination_intent_revision: int
    expected_stop_intent_revision: int
    expected_close_intent_revision: int
    expected_permission_policy_revision: int
    expected_permission_policy_fingerprint: str
    prior_model_control_disposition_reference: ContextEventReferenceFact
    previous_model_call_end_event_reference: ContextEventReferenceFact
    expected_provider_input_generation_id: str
    expected_provider_input_generation_revision: int
    expected_provider_input_committed_state_fingerprint: str
    expected_pending_interaction_frontier_fingerprint: str
    expected_open_tool_pair_frontier_fingerprint: str
    expected_notification_state_fingerprint: str
    expected_selected_notification_head_fingerprints: tuple[str, ...]
    expected_autonomy_chain_state_fingerprint: str
    prepared_provider_input_append_fingerprint: str
    guard_fingerprint: str


class HostActiveRunMonitorDeliveryFact(FrozenFactBase):
    schema_version: Literal["host_active_run_monitor_delivery.v1"]
    owner_kind: Literal["host_active_run_pre_model_step"]
    commit_guard: ActiveRunMonitorSafePointCommitGuardFact
    ordered_attachment_fingerprints: tuple[str, ...]
    autonomy_delivery: TerminalAutonomousDeliveryFact
    delivery_fingerprint: str


class TerminalProcessNotificationWirePayloadFact(FrozenFactBase):
    schema_version: Literal["terminal_process_notification_wire_payload.v1"]
    monitor_id: str
    observation_ordinal: int
    process_id: str
    observation_kind: Literal[
        "heartbeat", "output_progress", "process_completed", "monitor_expired"
    ]
    process_state: TerminalProcessObservedStateFact
    output_availability: Literal["available", "unavailable_recovered"]
    observed_end_cursor_fingerprint: str
    output_preview: str
    output_preview_utf8_bytes: int
    output_preview_sha256: str
    output_delta_chars: int
    omitted_delta_chars: int
    truncated: bool
    full_log_available: bool
    payload_semantic_fingerprint: str


class TerminalProcessObservationRuntimeRequestPayloadFact(FrozenFactBase):
    schema_version: Literal[
        "terminal_process_observation_runtime_request_payload.v1"
    ]
    ordered_notifications: tuple[TerminalProcessNotificationWirePayloadFact, ...]
    task_policy: Literal["inspect_progress_under_root_policy"]
    payload_semantic_fingerprint: str
```

`HostRuntimeNotificationAttachmentFact`是notification source evidence的唯一durable owner。placement只排序；`HostAutonomousRuntimeRequestOwnerFact`只保存Host/chain ownership与ordered attachment fingerprints。三者validator要求owner fingerprints精确覆盖runtime ingress中ordered attachments，而exact event hydration只从attachment执行，禁止在placement或owner中复制第三份source refs。

`HostActiveRunMonitorDeliveryFact`是PRE_MODEL_STEP的prepared companion，不是第二个ModelStart writer。generation/provider-input coordinator只准备stable append candidate；`LLMRuntime`继续是ModelCallStart唯一owner。

guard必须由session-owned factory在同一无await authority snapshot中构造。`prior_model_control_disposition_reference`必须是当前completed model/tool step的latest FULL disposition；pending interaction与open tool-pair frontier fingerprint必须分别等于canonical empty/closed frontier；selected notification-head tuple必须与outer ordered attachments逐项对应，并且全部属于expected notification state。任何字段都不能由agent caller自报。

`ModelStreamStartCommitPort`/lifecycle bundle新增typed safe-point companion。commit必须在现有LLM writer lock与同一PostgreSQL transaction中验证guard并提交：

```text
ProviderInput append + generation-state CAS
+ TerminalProcessObservationDeliveryDispositionEvent(active_run_safe_point)
+ TerminalAutonomyChainState transition
+ ModelCallStartEvent
+ existing lifecycle/account facts and settlement
```

pre-commit任一revision/reference/frontier变化均返回typed `ACTIVE_RUN_MONITOR_SAFE_POINT_REPLAN_REQUIRED`，不写部分batch、不消费observation或automatic delivery ordinal，并abandon prepared ProviderInput handle。NONE只有在guard仍匹配时重试同一candidate；UNKNOWN/PARTIAL保留append、delivery、chain和ModelStart owner并latch。stop/close在FULL前推进revision会使guard stale；FULL之后到达则按现有active-call stop contract作用于已经合法开始的model call。

wire payload中的`output_delta_chars`与`omitted_delta_chars`不是新的authority：adapter lowering必须从monitor observation output cursors按§5.4公式计算并写入，pre-send validator再从同一typed observation重算逐字段比较。preview UTF-8 bytes/hash同理由preview正文重算。

wire lowering还必须从observation cursor的stream identity派生`process_id`，并执行branch matrix：heartbeat/output-progress/monitor-expired只接受running state与available output；process-completed只接受terminal lifecycle outcome，unavailable-recovered也只允许该分支。`monitor_id/observation_ordinal`必须等于durable source observation；wire DTO不能被caller脱离typed observation独立构造。

`RuntimeRequestOwnerFact` union、`RuntimeRequestKindContractFact.allowed_owner_kinds`与`RuntimeRequestWireSemanticFact.lifecycle_class`必须分别新增`host_autonomous_run_entry`/`host_run_entry`。仅新增`terminal_process_observation` request kind而不增加owner branch是非法半迁移。

同一typed notification payload分别进入：

* human run companion的`RuntimeObservationWireSemanticFact(observation_kind="terminal_process_observation")`；
* autonomous run的`RuntimeRequestWireSemanticFact(request_kind="terminal_process_observation")`。

两者共享payload semantic，但wire envelope、instruction policy和outer attribution不同。对应payload/kind/owner必须注册到中央runtime user-carrier protocol；unknown schema/kind在adapter前fail closed。

`RuntimeRequestKind` 新增 `terminal_process_observation`，但这只是 wire/request semantic。合法 autonomous Host run 还必须拥有上述 Host run ingress、RunStart carrier 和 durable notification disposition。

### 7.2 RunStart 与 current input

Host `RunStartEvent` 必须 required：

* `host_run_ingress`；
* ingress kind 与 boundary kind；
* exact permission basis；
* exact current input messages；
* notification delivery references；
* autonomy chain attribution（仅 runtime branch）。

旧的 human-only `CurrentUserMessageFact` 必须 hard-cut 为 typed current-run input union：

```text
HumanCurrentInputMessageFact
RuntimeRequestCurrentInputMessageFact
RuntimeNotificationCompanionMessageFact
```

约束：

* human text 继续使用 `pulsara_human_input` user carrier；
* terminal autonomous request 使用 `pulsara_runtime_request` user carrier；
* attached notification 不能拼进 human text；
* capability resolve basis 使用 ingress semantic fingerprint，不能把 runtime notification伪装成 human intent；
* ProviderInput、Context manifest、transcript reducer 和 Inspector 必须保存同一 ingress identity；
* subagent RunEntry 不受本 union替代，但 V1不能产生 terminal autonomous Host ingress。

### 7.3 Permission snapshot

autonomous run 的 permission basis 同时绑定：

1. monitor registration 时的 run permission snapshot；
2. dispatch 时 HostSession 当前 effective permission policy；
3. 独立 autonomy/scheduling policy。

resulting permission 只能更严格，不能通过历史 monitor 恢复已经撤销的权限。registration 已获准也不等于未来 dispatch 必然获准；dispatch gate关闭时，observation 保持 pending-for-human。

### 7.4 Inspector

Inspector 必须显示：

* ingress kind；
* human/runtime semantic identity；
* attached notification refs；
* monitor observation/receipt/disposition join；
* delivery chain与ordinal；
* permission basis；
* exact RunStart/ProviderInput reference。

历史 Inspector 只能根据 durable ingress/events 展示，不得从 process-local queue伪造 ingress。

## 8. HostIngressCoordinator

### 8.1 Owner 与状态

`HostSession` 拥有唯一 `HostIngressCoordinator`。它在同一无-await lock/actor中维护：

```text
OPEN_IDLE
PREPARING
ACTIVE
WAITING_USER
STOPPING
CLOSING
CLOSED
LATCHED
```

并统一持有：

* bounded human ingress queue；
* pending interaction resume；
* confirmed runtime notification refs；
* current admission attempt；
* close/stop gate；
* notification projection/recovery cursor。

现有 `_run_lock` 继续串行执行 run，但不再承担 ingress admission/priority。

每个被接纳的ingress拥有独立、稳定的process owner：

```text
QUEUED
    -> PREPARING(admission generation)
    -> WITHDRAWN

PREPARING
    -> COMMITTED
    -> REPLAN_REQUIRED
    -> RECONCILIATION_REQUIRED

COMMITTED
    -> ACTIVE
    -> FINISHED
```

```python
@dataclass(slots=True)
class HostIngressAttemptOwner:
    attempt_id: str
    ingress: HostRunIngressFact
    accepted_ingress_ordinal: int
    state: Literal[
        "queued",
        "preparing",
        "committed",
        "active",
        "finished",
        "withdrawn",
        "replan_required",
        "reconciliation_required",
    ]
    caller_waiter_attached: bool
    selection_queue_revision: int | None
    admission_proof: HostIngressAdmissionProofFact | None
    prepared_handles: tuple[object, ...]
    completion: asyncio.Future[AgentRunResult]
```

owner在第一次await前接管ingress、waiter状态与所有prepared handles；boundary task栈帧不能成为唯一owner。`prepared_handles`在实现中必须换成现有typed Context/ProviderInput/manifest abandonment handles，不能以裸`object`进入production API。

```python
class HostIngressAdmissionProofFact(FrozenFactBase):
    schema_version: Literal["host_ingress_admission_proof.v1"]
    admission_id: str
    admission_generation: int
    ingress_fact_fingerprint: str
    selected_ingress_item_ids: tuple[str, ...]
    selected_notification_head_fingerprints: tuple[str, ...]
    expected_host_state_generation: int
    expected_permission_policy_revision: int
    expected_permission_policy_fingerprint: str
    expected_close_intent_revision: int
    expected_autonomy_chain_state_fingerprint: str | None
    proposed_automatic_delivery_ordinal: int | None
    admission_proof_fingerprint: str


class HostIngressCoordinatorStateFact(FrozenFactBase):
    schema_version: Literal["host_ingress_coordinator_state.v1"]
    host_session_id: str
    state_generation: int
    lifecycle_state: Literal[
        "open_idle",
        "preparing",
        "active",
        "waiting_user",
        "stopping",
        "closing",
        "closed",
        "latched",
    ]
    active_admission_id: str | None
    active_admission_generation: int | None
    active_run_start_event_id: str | None
    permission_policy_revision: int
    permission_policy_fingerprint: str
    close_intent_revision: int
    state_fingerprint: str
```

`HostIngressAdmissionProofFact`是durable admission authority的唯一DTO，直接进入RunStart lifecycle bundle。外层RunStart、notification disposition和Host state transition只复制其`admission_proof_fingerprint`做equality join，不再从reservation复制第二份guard。`selection_queue_revision`、caller waiter和prepared handles只属于process-local `HostIngressAttemptOwner`，不进入durable proof。PostgreSQL/InMemory均提供同语义的Host ingress projection row；不存在row时只允许HostSession genesis创建，已有ledger事实但row缺失必须reset/migrate，不能现场猜测generation。

#### 8.1.1 Caller cancellation 与 capacity

* human waiter在`QUEUED`时取消：原子`QUEUED -> WITHDRAWN`，移出queue，不分配RunStart ID；
* 已分配的`accepted_ingress_ordinal`即使withdrawn也不复用；ordinal允许有gap但必须单调；
* caller在`PREPARING`后取消：只detach waiter，owner继续完成相同attempt；不能把caller cancellation解释成user stop；
* caller需要停止已接纳attempt时，必须提交统一coordinator control intent；
* human queue满时，在分配`accepted_ingress_ordinal`前稳定拒绝`host_human_ingress_capacity_exhausted`；
* rejected item不得悄悄进入后续selection，也不得生成durablehuman attribution；
* runtime notification不因human queue满被丢弃，它仍由durablenotification projection持有。

### 8.2 Selection

唯一 selection 算法：

1. 在 coordinator lock 内冻结 queue revision 和 Host state；
2. 若 closing/stopping/latching，禁止新 dispatch；
3. `WAITING_USER` 时只允许匹配 pending interaction 的 resume；
4. 否则优先选择已排队 human ingress；
5. 无 human 时选择最早 confirmed、未消费且 autonomy gate允许的 runtime notification；
6. 在attempt owner中安装process-local selection ownership，构造唯一`HostIngressAdmissionProofFact`并进入`PREPARING`；
7. lock 外准备 boundary；
8. 回到coordinator执行pre-commit CAS；
9. atomic commit RunStart + notification disposition + durable Host state CAS；
10. FULL 进入 ACTIVE；NONE 保留/重试；UNKNOWN/PARTIAL latch。

任何 caller 都不能跳过 step 1-6 直接创建 boundary task。

monitor notification的调度class由typed observation唯一派生，不另存caller自报priority：

```text
completion error/timeout/lifetime_watchdog -> urgent
completion success/output_progress         -> progress
heartbeat/monitor_expired                  -> informational
```

human ingress始终高于三类notification。active goal/scheduled long task可按versioned scheduling policy抑制`progress/informational` autonomous dispatch，但不能删除observation；它们保持pending供safe point或human merge。`urgent`仍需通过当前permission和wake-chain budget，不能绕过用户关闭autonomy的决定。

每个被接纳的ingress item在同一lock内获得单调`accepted_ingress_ordinal`。当human run附带notification时，最终current-input顺序按该ordinal排列，再以stable item ID打破同ordinal tie；不能因为“human拥有dispatch优先级”就篡改已经发生的因果顺序。`HostRunIngressSemanticFact.ordered_current_input_semantic_fingerprints`必须精确覆盖这一顺序。

#### 8.2.1 Pre-commit CAS 与 replan

preparation期间以下变化会使admission proof stale：

* selected notification被newer terminal observation supersede或被receipt消费；
* selected process-head fingerprint变化；
* permission policy revision/fingerprint变化；
* selected autonomy chain state/ordinal变化；
* Host state generation或close intent revision变化；
* admission被explicit control intent撤销。

stale处理只有一种：

```text
PREPARING -> REPLAN_REQUIRED
    -> release/abandon all prepared Context/ProviderInput/artifact handles
    -> do not commit RunStart
    -> do not consume proposed automatic delivery ordinal
    -> return selected facts to canonical projection
    -> re-enter selection with a new admission generation
```

单纯有新human/runtime item入队只增加queue revision，不使已经selected的attempt过期；否则持续输入会造成starvation。`selection_queue_revision`只用于审计。

RunStart writer必须在同一transaction中CAS唯一admission proof：

* exact admission ID/generation/fingerprint；
* Host state generation；
* selected notification-head fingerprints；
* permission revision/fingerprint；
* expected autonomy chain state fingerprint与proposed delivery ordinal；
* close intent revision；
* expected materialization account state。

CAS stale返回typed`HOST_INGRESS_REPLAN_REQUIRED`，不是UNKNOWN，也不能写任何部分batch。commit开始后到达的close/permission intent先排队；若CAS已经FULL，它们作用于新ACTIVE run，若尚未linearize则使admission proof stale。

NONE只可在admission proof仍匹配时重试同一stable candidate；UNKNOWN/PARTIAL保留全部prepared handles与admission proof并进入`RECONCILIATION_REQUIRED`。new-run路径的automatic delivery ordinal只在包含RunStart的FULL transaction中递增；safe-point路径则只在包含ModelCallStart的FULL transaction中递增。两者CAS同一chain state。

### 8.3 Human merge

如果 human ingress在 selection 时已排队，它可附带 pending terminal notifications：

* human message 与 runtime notification保持独立 typed message；
* human message使用`pulsara_human_input`，companion使用`pulsara_runtime_observation`；
* notification不额外消耗 automatic delivery budget；
* delivery disposition与该 human RunStart同批 FULL；
* 同一 observation已被 receipt消费时不得附带；
* 不同 wake chain可被 human run同时附带，因为本次模型调用由用户触发。

### 8.4 Runtime dispatch

V1 autonomous run 每次只选择一个 wake chain；同一 chain 的多个已确认 notification可有序合并。其他 chain继续 pending，避免一轮 autonomous run 模糊消耗多个独立 chain budget。

autonomous run的current task使用`pulsara_runtime_request(request_kind=terminal_process_observation)`；committed monitor observation payload作为typed source observation嵌入request，不伪装成human input。

atomic batch至少包含：

```text
TerminalProcessObservationDeliveryDispositionEvent(autonomous_dispatched)
+ TerminalAutonomyChainState transition
+ RunStartEvent(host_run_ingress=RuntimeRequestRunIngressFact)
+ existing RunStart boundary/account facts
```

`RuntimeRequestRunIngressFact.source_notifications`、nested `TerminalAutonomousDeliveryFact.ordered_source_attachment_fingerprints`和Host owner的ordered tuple必须逐项相等。RunStart失败时不能先消费 observation；disposition/chain CAS失败时也不能启动模型或消耗ordinal。

### 8.5 Durable notification projection

`HostIngressNotificationProjectionStore` 是 session-owned incremental reducer，而不是 terminal-specific ad hoc queue。它消费：

* terminal completion lifecycle；
* monitor observation；
* ToolResult observation receipt；
* delivery/deferred/terminal disposition；
* RunStart/RunEnd；
* session close/recovery facts。

其state至少冻结：

```python
class TerminalMonitorNotificationHeadFact(FrozenFactBase):
    schema_version: Literal["terminal_monitor_notification_head.v1"]
    monitor_id: str
    registration_event_reference: ContextEventReferenceFact
    monitor_core_state_fingerprint: str
    last_committed_observation_ordinal: int
    last_observation_cursor_fingerprint: str
    last_consumed_cursor_fingerprint: str
    pending_observation_event_reference: ContextEventReferenceFact | None
    latest_delivery_event_reference: ContextEventReferenceFact | None
    head_fingerprint: str


class TerminalNotificationProcessHeadFact(FrozenFactBase):
    schema_version: Literal["terminal_notification_process_head.v1"]
    stream_identity: TerminalOutputStreamIdentityFact
    latest_completion_event_reference: ContextEventReferenceFact | None
    monitor_heads: tuple[TerminalMonitorNotificationHeadFact, ...]
    latest_dominant_receipt_reference: ContextEventReferenceFact | None
    pending_completion_without_monitor_reference: ContextEventReferenceFact | None
    head_fingerprint: str


class HostIngressNotificationProjectionStateFact(FrozenFactBase):
    schema_version: Literal["host_ingress_notification_projection_state.v1"]
    ledger_runtime_session_id: str
    source_through_sequence: int
    process_heads: tuple[TerminalNotificationProcessHeadFact, ...]
    reservation_account_revision: int
    reservation_account_state_fingerprint: str
    reducer_contract_fingerprint: str
    state_fingerprint: str
```

`process_heads`按stream identity排序且unique；V1每个process最多一个nested monitor head，避免同一completion被多个monitor重复投影。每个monitor head只允许一个pending progress observation。pending count由monitor heads和unmonitored completion的pending refs唯一重算，不进入state fingerprint输入。`autonomous_delivery_eligible`也不持久化；selection gate每次根据confirmed observation、当前permission/scheduling policy和chain budget计算，避免head保存过期policy结论。

它的bound不是“满了就丢”：managed process在进入yielded/background状态前必须预留一个completion notification slot，monitor registration再预留一个贯穿monitor lifetime的monitor slot；没有slot则不能产生新的background owner。已获reservation的process即使同时完成也必然有位置。same-monitor output/heartbeat只保留一个pending progress；completion/expiry可在同一head内用consumed cursor构造覆盖性terminal observation并supersede它。durable source events仍全部保留，projection coalescing不删除audit identity。

process-local queue只借用下一批bounded refs；`max_pending_runtime_notifications`约束resident/dispatch batch，不授权丢弃durable completion。reopen从durable projection row + bounded delta恢复，不能扫描全文并重新猜测output。

#### 8.5.1 Ledger-scoped notification reservation account

独立计数器不足以证明acquire/release。V1冻结唯一account：

```python
class TerminalNotificationReservationFact(FrozenFactBase):
    schema_version: Literal["terminal_notification_reservation.v1"]
    reservation_id: str
    reservation_kind: Literal["completion_process_head", "monitor_lifecycle"]
    stream_identity: TerminalOutputStreamIdentityFact
    monitor_id: str | None
    created_by_event_id: str
    reservation_fingerprint: str


class TerminalNotificationReservationAccountStateFact(FrozenFactBase):
    schema_version: Literal[
        "terminal_notification_reservation_account_state.v1"
    ]
    ledger_runtime_session_id: str
    account_revision: int
    maximum_completion_process_heads: int
    maximum_active_monitor_slots: int
    active_completion_reservations: tuple[TerminalNotificationReservationFact, ...]
    active_monitor_reservations: tuple[
        TerminalNotificationReservationFact, ...
    ]
    latest_transition_event_id: str | None
    state_fingerprint: str


class TerminalNotificationAccountTransitionFact(FrozenFactBase):
    schema_version: Literal["terminal_notification_account_transition.v1"]
    source_revision: int
    result_revision: int
    before_state_fingerprint: str
    after_state_fingerprint: str
    reservation: TerminalNotificationReservationFact
    cause_event_identities: tuple[StableEventIdentityFact, ...]
    resulting_projection_state_fingerprint: str
    transition_fingerprint: str
```

`TerminalNotificationReservationCreatedEvent`与`TerminalNotificationReservationReleasedEvent`分别嵌上述transition，outer event type唯一决定reserve/release方向，transition不再复制`transition_kind`。account tuple均按reservation ID排序且受maximum约束。transition使用source/result revision、before/after fingerprint与stable cause identities；created validator要求reservation只出现在after，released validator要求它只出现在before。PostgreSQL/InMemory共享相同CAS语义。cause identities不得引用同一个transition-bearing bookkeeping event，避免payload identity环；post-commit validator再按exact stored event ID/schema/payload确认same-batch identity。

canonical genesis：

```text
ledger_runtime_session_id = current runtime ledger
account_revision = 0
active_completion_reservations = ()
active_monitor_reservations = ()
latest_transition_event_id = None
```

account row与Host runtime ledger genesis/bootstrap同事务创建。已有ledger business event但缺row时必须reset/migrate；不能在首次monitor时现场创建一个“看起来为空”的account。

acquire/release矩阵：

| Reservation | Acquire | Release |
|---|---|---|
| completion process head | background/yielded process对模型可见的ToolResult terminal同批 | process已terminal，且completion已delivered、explicitly observed或session_closed，并且无pending/reconciliation owner |
| monitor lifecycle | monitor registration + ToolResult terminal同批 | completion/expiry observation或`TerminalProcessMonitorTerminatedEvent` FULL同批 |

process可先物理launch，但在completion reservation FULL前只能由terminal execution owner持有，不能把`process_id`发布给模型或外部caller。capacity拒绝时终止该物理process并返回typed capacity result；NONE保留同一tool terminal owner；UNKNOWN/PARTIAL latch，不能留下一个已公开但无head容量的background process。

monitor observation不创建新的capacity reservation；它复用registration持有的monitor-lifecycle slot和process completion head。progress delivery后slot继续保留，monitor自动回到ACTIVE_READY；completion/expiry observation FULL或explicit terminal outcome FULL才释放monitor slot。一个process无论有多少bounded monitor，至多占一个completion head，但每个monitor都占一个独立lifecycle slot。

head retirement唯一条件：

```text
process is terminal
and no active completion reservation after this transition
and no active monitor lifecycle reservation
and no pending observation
and no FIRING/reconciliation owner
```

release、head retirement、notification projection revision/horizon与account revision必须通过session-owned`TerminalNotificationAccountCommitPort`在一个transaction/CAS中推进。不能先释放counter再异步删除head，也不能先retire head再补release event。

具体same-batch owner：

* autonomous/human delivery：RunStart + delivery disposition + eligible completion-slot release/head retirement；
* explicit observation：receipt已FULL后，由observation owner提交explicit disposition + eligible release/head retirement；
* session close：session_closed dispositions + eligible releases/head retirements；
* process尚running或仍有pending/reconciliation owner时，delivery只能清pending observation，不能提前释放completion reservation。

restart从durable reservation transitions、completion/monitor terminal events、dispositions与projection row重建完全相同的余额。projection/account row缺失或fingerprint冲突属于reset/migration或authority-untrusted，不能把全部slot现场归零。

## 9. Monitor Registration

### 9.1 Public Tool API

`terminal_monitor`是唯一持续订阅工具；不在`terminal_process`保留monitor action，也不保留`watch`或`wake_when`别名：

```json
{
  "action": "register",
  "process_id": "...",
  "conditions": {
    "output": {
      "min_new_output_chars": 200,
      "quiet_period_ms": 500
    },
    "heartbeat_interval_seconds": 60
  },
  "delivery": {
    "max_output_chars": 4000,
    "minimum_progress_observation_interval_seconds": 5
  },
  "lifetime": {
    "maximum_duration_seconds": 36000
  }
}
```

conditions 是 OR：

* 自`last_observation_cursor`起，sanitized 新输出达到 threshold并经过 quiet period；
* heartbeat interval 到达；
* process confirmed terminal；completion condition始终启用。

output与heartbeat都可为空，此时monitor只等待completion。completion不是可关闭的bool：任何process terminal都必须终结monitor；status矩阵只决定是否产生模型可见completion observation。heartbeat 是重复检查点，不是一次 absolute deadline。V1 不支持 regex、任意 shell filter 或“每行一个模型事件”。日志正文始终是不可信数据，不能改变 monitor policy。

monitor 返回 committed registration 和 `monitor_id` 后立即释放当前 tool call。后续 progress observation 不要求模型再次注册；相同 monitor 自动推进 cursor 并继续观察。公开 API 另提供 `cancel_monitor`/`list_monitors` typed action，cancel 只终结 monitor，不终止 process。

### 9.2 Semantic、lifetime 与 attribution 分层

```python
class TerminalProcessMonitorOutputConditionFact(FrozenFactBase):
    schema_version: Literal["terminal_process_monitor_output_condition.v1"]
    min_new_output_chars: int
    quiet_period_ms: int
    condition_fingerprint: str


class TerminalProcessMonitorConditionsFact(FrozenFactBase):
    schema_version: Literal["terminal_process_monitor_conditions.v1"]
    output: TerminalProcessMonitorOutputConditionFact | None
    heartbeat_interval_seconds: int | None
    conditions_fingerprint: str


class TerminalProcessMonitorDeliveryPolicyFact(FrozenFactBase):
    schema_version: Literal["terminal_process_monitor_delivery_policy.v1"]
    max_output_chars: int
    minimum_progress_observation_interval_seconds: int
    maximum_pending_progress_observations: Literal[1]
    maximum_committed_progress_observations: int
    progress_observation_rate_window_seconds: int
    maximum_progress_observations_per_rate_window: int
    delivery_policy_fingerprint: str


class TerminalProcessMonitorProgressLimiterStateFact(FrozenFactBase):
    schema_version: Literal["terminal_process_monitor_progress_limiter_state.v1"]
    retained_progress_observed_at_utc: tuple[str, ...]
    last_committed_progress_observed_at_utc: str | None
    delivery_policy_fingerprint: str
    limiter_state_fingerprint: str


class TerminalProcessMonitorLifetimeFact(FrozenFactBase):
    schema_version: Literal["terminal_process_monitor_lifetime.v1"]
    kind: Literal["bounded", "process_lifetime"]
    maximum_duration_seconds: int
    lifetime_fingerprint: str


class TerminalProcessMonitorPolicyFact(FrozenFactBase):
    schema_version: Literal["terminal_process_monitor_policy.v1"]
    conditions: TerminalProcessMonitorConditionsFact
    delivery: TerminalProcessMonitorDeliveryPolicyFact
    lifetime: TerminalProcessMonitorLifetimeFact
    policy_fingerprint: str


class TerminalProcessMonitorRegistrationSemanticFact(FrozenFactBase):
    schema_version: Literal["terminal_process_monitor_registration_semantic.v1"]
    monitor_id: str
    initial_baseline_cursor: TerminalOutputCursorFact
    policy: TerminalProcessMonitorPolicyFact
    registration_semantic_fingerprint: str


class TerminalProcessMonitorRegistrationAttributionFact(FrozenFactBase):
    schema_version: Literal["terminal_process_monitor_registration_attribution.v1"]
    owner_host_session_id: str
    owner_conversation_id: str | None
    origin_runtime_session_id: str
    process_origin_runtime_session_id: str
    process_origin_run_entry_kind: Literal["host_main_run"]
    origin_run_event_reference: ContextEventReferenceFact
    origin_tool_call_id: str
    registered_at_utc: str
    expires_at_utc: str
    permission_authority: TerminalAutonomyPermissionAuthorityFact
    wake_chain: TerminalAutonomousDeliveryChainAttributionFact
    attribution_fingerprint: str
```

invariants：

* `conditions.output is not None` 唯一表达 output progress 已启用；不复制 bool；
* completion condition由monitor contract固定启用，不存bool；`process_lifetime` 仍必须有 implementation hard cap，不等于无界 session subscription；
* V1 `maximum_duration_seconds <= 36_000`，到期写 typed terminal outcome；
* `maximum_pending_progress_observations == 1`：前一条 progress 尚未 disposition 时不再提交第二条 progress；journal 继续推进，completion/expiry可用consumed cursor构造覆盖pending内容的terminal observation；
* `maximum_committed_progress_observations >= 1`；physical contract额外保留一条terminal observation，因此总observation上限为`maximum_committed_progress_observations + 1`；
* registration 的 process/journal identity 只从 `initial_baseline_cursor.stream_identity` 取得；
* `monitor_id` 由 origin tool-call stable identity 确定性派生，同一 retry 必须复用 exact registration；
* physical heartbeat/output deadline 使用 monotonic clock；UTC 只用于 durable attribution与restart expiry判断。

progress limiter只使用一种确定性sliding-window算法，不同时实现token bucket/fixed window/burst：

1. registration的limiter tuple为空且last committed time为null。arbiter每次尝试progress时冻结一个`observed_at_utc=T`；若wall clock回退，`T=max(sampled_utc, last_committed_progress_observed_at_utc)`，该normalized值进入stable candidate，retry不得重新采样。
2. 从tuple删除所有`t <= T - progress_observation_rate_window_seconds`的元素，窗口是`(T-window, T]`。
3. 只有tuple长度小于`maximum_progress_observations_per_rate_window`，并且last committed time为null或`T-last_committed >= minimum_progress_observation_interval_seconds`时，progress才eligible。
4. FULL时把`T` append到tuple并令last committed time为`T`；若tuple非空，其最后一项必须等于last committed time。NONE复用相同candidate，UNKNOWN/PARTIAL保留owner。未eligible时只安排process-local monotonic wake，不写durable deferred event。
5. 下一eligible UTC唯一派生为`max(last_committed + minimum_interval, oldest + window)`；对应项不存在或未达到窗口容量时忽略该项。process-local monotonic timer到达后必须重新冻结authority snapshot，不能仅凭旧timer提交。
6. restart从durable committed progress events重建同一bounded tuple，不创建“满bucket”或重置quota。

tuple中的时间必须是canonical UTC、单调非递减且数量不超过`maximum_progress_observations_per_rate_window`；非空时最后一项必须等于last committed time。factory每次从previous state与candidate `T`完整重算resulting state，caller不能直接提交一个自报tuple。

quiet/debounce只决定何时形成output candidate；上述limiter只限制durable progress/heartbeat；`ResolvedTerminalAutonomyChainPolicyFact.minimum_automatic_delivery_interval_seconds`只限制付费model sampling。completion与expiry绕过quiet、progress interval、sliding window和progress count，但仍走唯一arbiter与accounted writer。

#### 9.2.1 V1 single-ledger restriction

`owner_host_session_id`只能证明Host隔离，不能证明EventLog authority。process launch时必须额外冻结：

```text
origin_runtime_session_id
origin_run_event_reference
origin_run_entry_kind
origin_tool_call_id
```

V1 registration validator同时要求：

```text
caller is committed Host main run
process.origin_run_entry_kind == host_main_run
process.origin_runtime_session_id == caller.runtime_session_id
process.origin_runtime_session_id == HostSession.runtime_session_id
```

任一不满足均稳定拒绝：

```text
terminal_monitor_cross_ledger_process_unsupported
```

因此registration、completion、monitor observation、receipt disposition与Host RunStart均由同一runtime ledger证明。仅拥有相同`owner_host_session_id`不足以通过。

未来若支持parent monitor child-origin process，必须先引入per-ledger authority horizons、`EventLogLocator`、cross-ledger stored-event reference与原子/可恢复reparent contract；V1不得临场跨ledger named-read。

### 9.3 Dormant owner 与 same-batch join

tool executor返回一个 `PreparedTerminalProcessMonitorRegistration`，它是 dormant且无timer/condition subscription。`PreparedToolTerminalResult`/terminal commit port增加 typed companion candidate，而不是开放任意 side-effect callback。

ToolResult terminal projection与`TerminalProcessMonitorRegisteredEvent`共享 exact registration semantic fingerprint和稳定event IDs。为避免递归payload identity：

* 两者可以保存对方的deterministic event ID；
* 不在彼此payload中嵌套对方的payload fingerprint；
* same-batch validator核对 tool call ID、monitor ID、registration fingerprint和event IDs；
* hydrated ToolResult文档必须声明相同的registration semantic；
* monitor-lifecycle reservation 与 registration/ToolResult terminal 同批 acquire。

```python
class TerminalProcessMonitorRegisteredEvent(EventBase):
    schema_version: Literal["terminal_process_monitor_registered.v1"]
    registration_semantic: TerminalProcessMonitorRegistrationSemanticFact
    registration_attribution: TerminalProcessMonitorRegistrationAttributionFact
    resulting_monitor_core_state_fingerprint: str
    tool_result_end_event_id: str
    notification_account_transition_fingerprint: str
```

registered event只保存resulting core fingerprint，不嵌latest event reference；monitor core state的initial revision为0，`last_observation_cursor == last_consumed_cursor == initial_baseline_cursor`，observation ordinal与progress count均为0，progress limiter tuple为空/last committed time为null，pending observation为null。

confirmation矩阵：

| write outcome | dormant/active owner | 行为 |
|---|---|---|
| FULL | ACTIVE_READY | fold committed registration，activate，立即recheck |
| NONE | DORMANT | 保留stable candidate，由tool terminal owner重试 |
| UNKNOWN/PARTIAL | RECONCILIATION_REQUIRED | 保留owner，latch，禁止activate/notify |
| rejected_before_commit | TERMINATED | ToolResult不得声称注册成功 |
| caller cancelled after FULL | ACTIVE_READY | 先adopt FULL并activate，再传播caller cancellation |

### 9.4 Registration race recheck

FULL 后必须立即读取：

* exact frozen initial baseline cursor；
* current journal end cursor；
* confirmed process terminal reference；
* monotonic heartbeat/output timers和durable expiry。

若进程在prepare/commit窗口内已经结束或输出达到threshold，recheck生成ordinal 1的普通稳定observation，不得漏掉completion。若多个条件同时成立，使用§10.2固定winner；不能为同一snapshot提交多条初始observation。

### 9.5 `cancel_monitor` 原子终结契约

cancel与registration对称，由tool terminal owner一次性提交，不能由tool implementation先改live monitor：

```python
class TerminalProcessMonitorCancelIntentFact(FrozenFactBase):
    schema_version: Literal["terminal_process_monitor_cancel_intent.v1"]
    monitor_id: str
    origin_cancel_tool_call_id: str
    monitor_termination_event_id: str
    tool_result_end_event_id: str
    intent_fingerprint: str


class TerminalProcessMonitorCancellationSemanticFact(FrozenFactBase):
    schema_version: Literal["terminal_process_monitor_cancellation_semantic.v1"]
    cancel_intent: TerminalProcessMonitorCancelIntentFact
    expected_monitor_state_revision: int
    expected_monitor_core_state_fingerprint: str
    cancellation_semantic_fingerprint: str


@dataclass(frozen=True, slots=True)
class PreparedTerminalProcessMonitorCancellation:
    cancel_intent: TerminalProcessMonitorCancelIntentFact
    cancellation_semantic: TerminalProcessMonitorCancellationSemanticFact
    stable_monitor_termination_candidate: AgentEvent
    pending_progress_disposition_candidate: AgentEvent | None
    notification_account_release_candidate: AgentEvent
    prepared_tool_terminal_result: PreparedToolTerminalResult
```

唯一成功batch：

```text
TerminalProcessObservationDeliveryDispositionEvent(monitor_cancelled)  # iff pending progress
+ TerminalProcessMonitorTerminatedEvent(explicit_cancel)
+ monitor-lifecycle reservation release
+ notification projection/state transition
+ cancel ToolResult terminal projection
+ ToolResultEnd
+ physical operation settlement
```

ACTIVE_READY/ACTIVE_PENDING_DELIVERY下，全部candidate与prepared ToolResult必须在第一次await前转交同一个session-owned cancellation owner。ToolResult文档required join monitor ID、source state revision、termination event ID、release transition fingerprint和`cancelled` outcome。

confirmation矩阵：

* FULL：先fold termination/release/disposition，再返回cancel ToolResult；
* NONE：保留整批stable candidates并重试；
* UNKNOWN/PARTIAL：保留owner并latch，不返回“cancelled”；
* monitor已`TERMINAL_PENDING_DELIVERY/TERMINATED`：返回typed `already_terminal` ToolResult，不消费pending completion且不写第二份termination；
* monitor处于`FIRING`：在任何await前只向现有FIRING owner安装stable `TerminalProcessMonitorCancelIntentFact`，等待observation confirmation；progress FULL后由唯一factory以该intent和resulting state派生`CancellationSemanticFact`及上述cancel batch，completion/expiry FULL则返回`already_terminal`；
* caller cancel-after-FULL：先adopt完整FULL batch再传播caller cancellation。

`FIRING` cancel intent不能替换、撤销或重新生成正在确认的observation candidate，也不能提前自报未知的resulting state。close也必须复用相同terminalization owner，不得另开一条“best effort cancel”旁路。

## 10. TerminalMonitorCoordinator

### 10.1 状态机

```text
PREPARED_DORMANT
    -> ACTIVE_READY               registration FULL
    -> TERMINATED                 registration rejected

ACTIVE_READY
    -> FIRING                     arbiter freezes winner/candidate/operation owner
    -> TERMINATED                 explicit cancel/session close/invalid owner

FIRING
    -> ACTIVE_PENDING_DELIVERY    progress/heartbeat observation FULL
    -> TERMINAL_PENDING_DELIVERY  completion/expiry observation FULL
    -> FIRING                     NONE, retry exact candidate
    -> RECONCILIATION_REQUIRED    UNKNOWN/PARTIAL

ACTIVE_PENDING_DELIVERY
    -> ACTIVE_READY               delivery/receipt FULL and progress count below cap
    -> ACTIVE_COMPLETION_ONLY     delivery/receipt FULL and progress count reached cap
    -> TERMINAL_PENDING_DELIVERY  completion/expiry atomically supersedes pending progress

ACTIVE_COMPLETION_ONLY
    -> TERMINAL_PENDING_DELIVERY  completion/expiry observation FULL
    -> TERMINATED                 explicit cancel/session close/invalid owner

TERMINAL_PENDING_DELIVERY
    -> TERMINATED                 delivery/explicit receipt/session-close disposition FULL
```

`FIRING` 必须保留：

* stable observation candidate与observation ordinal；
* source journal snapshot；
* completion ref（如适用）；
* physical reservation/settlement owner；
* retry/confirmation state；
* owner HostSession/ledger identity。

只有 FULL callback 才能在所有 writer/account locks 外把 notification发布给 Host ingress store。eviction/cancel/close不能删除 FIRING owner。

持续语义的关键不变量：

* progress/heartbeat FULL 后 `last_observation_cursor = observation.end_cursor`，下一次threshold从该cursor计算；
* `last_consumed_cursor`只在safe-point/autonomous/human delivery或dominant receipt FULL后推进；
* 永远满足`initial_baseline <= last_consumed_cursor <= last_observation_cursor <= journal_end_cursor`；
* `last_committed_observation_ordinal`严格加一，stable event ID由`monitor_id + ordinal`派生；
* progress pending期间journal继续写入，但不再提交第二条progress；
* pending progress被delivery/receipt消费后，`last_consumed_cursor`推进到实际可见end并立即recheck；
* progress count达到policy上限后，最后一条pending progress仍须正常delivery/disposition；随后进入`ACTIVE_COMPLETION_ONLY`，不再形成output/heartbeat observation，但继续监听process completion与monitor expiry；
* confirmed completion/expiry supersede pending progress时，terminal delta必须从`last_consumed_cursor`而非`last_observation_cursor`构造；
* supersede transaction同时提交pending progress的typed disposition、terminal observation和resulting state，模型最终可见区间不能出现gap；
* terminal observation FULL将`last_observation_cursor`推进到terminal end，但在它真正delivery/receipt前不推进`last_consumed_cursor`；
* completion/expiry observation FULL即停止物理monitor并释放monitor-lifecycle reservation，pending delivery仍由completion process head持有。

durable reducer state冻结为：

```python
class TerminalProcessMonitorCoreStateFact(FrozenFactBase):
    schema_version: Literal["terminal_process_monitor_core_state.v1"]
    monitor_id: str
    state_revision: int
    lifecycle_state: Literal[
        "active_ready",
        "active_pending_delivery",
        "active_completion_only",
        "terminal_pending_delivery",
        "terminated",
        "reconciliation_required",
    ]
    last_observation_cursor: TerminalOutputCursorFact
    last_consumed_cursor: TerminalOutputCursorFact
    last_committed_observation_ordinal: int
    committed_progress_observation_count: int
    progress_limiter_state: TerminalProcessMonitorProgressLimiterStateFact
    pending_observation_semantic_fingerprint: str | None
    terminal_reason: Literal[
        "process_completed",
        "monitor_expired",
        "explicit_cancel",
        "session_closed",
        "interrupted_by_host_restart",
        "explicit_process_kill",
        "process_completion_not_delivery_eligible",
        "authority_untrusted",
    ] | None
    core_state_fingerprint: str


class TerminalProcessMonitorStateTransitionFact(FrozenFactBase):
    schema_version: Literal["terminal_process_monitor_state_transition.v1"]
    source_revision: int
    result_revision: int
    before_core_state_fingerprint: str
    after_core_state_fingerprint: str
    observation_ordinal: int | None
    transition_fingerprint: str
```

core state不保存latest event ref，避免event payload与resulting state形成identity环；event references只进入projection attribution row。transition由outer observation/termination/disposition event决定方向，不能用自由字符串重新声明。

### 10.2 Arbiter

同一 authority snapshot中的优先级：

```text
confirmed process_completed > monitor_expiry > output_threshold > heartbeat
```

规则：

* completion必须有confirmed `TerminalProcessCompletedEvent` reference；
* completion winner还必须通过§11.1 status eligibility；`user_tool_kill`与`teardown`不产生completion observation，而是typed monitor terminal outcome；
* 物理进程已经退出但completion write尚未确定时，不得伪装成heartbeat/output observation；
* completion write UNKNOWN时对应owner latch；
* output threshold先按char/byte bound判断，再由quiet timer合并；
* heartbeat不会被持续output quiet timer无限推迟；
* output threshold与heartbeat同时eligible时只提交output winner；该observation同时满足本次heartbeat checkpoint，并从它的FULL时间重置下一heartbeat deadline，不能紧接着再补一条heartbeat；
* expiry是hard lifetime bound，不能被output或heartbeat无限推迟；
* `ACTIVE_COMPLETION_ONLY`只评估confirmed completion、expiry、cancel与close；output/heartbeat即使满足也不得形成candidate；
* completion/expiry不读取progress limiter eligibility，并且拥有physical reservation中的terminal reserve；
* arbiter在一个临界区冻结monitor revision、status和end cursor；
* observation candidate冻结后，后续output/completion不能改写该candidate；
* `ACTIVE_PENDING_DELIVERY`只允许completion/expiry terminal observation supersede，不允许普通output/heartbeat追加第二个pending progress。

### 10.3 Observation facts

```python
class TerminalProcessMonitorProgressObservationSemanticFact(FrozenFactBase):
    schema_version: Literal["terminal_process_monitor_progress_observation_semantic.v1"]
    monitor_id: str
    observation_kind: Literal["heartbeat", "output_progress"]
    observation_ordinal: int
    process_state: RunningTerminalProcessStateFact
    output_authority: TerminalOutputDeltaSemanticFact
    observation_semantic_fingerprint: str


class TerminalProcessMonitorCompletionObservationSemanticFact(FrozenFactBase):
    schema_version: Literal["terminal_process_monitor_completion_observation_semantic.v1"]
    monitor_id: str
    observation_kind: Literal["process_completed"]
    observation_ordinal: int
    completion_semantic: TerminalProcessCompletionSemanticFact
    output_authority: TerminalMonitorObservationOutputFact
    observation_semantic_fingerprint: str


class TerminalProcessMonitorExpiryObservationSemanticFact(FrozenFactBase):
    schema_version: Literal["terminal_process_monitor_expiry_observation_semantic.v1"]
    monitor_id: str
    observation_kind: Literal["monitor_expired"]
    observation_ordinal: int
    process_state: RunningTerminalProcessStateFact
    output_authority: TerminalOutputDeltaSemanticFact
    observation_semantic_fingerprint: str


TerminalProcessMonitorObservationSemanticFact = Annotated[
    TerminalProcessMonitorProgressObservationSemanticFact
    | TerminalProcessMonitorCompletionObservationSemanticFact
    | TerminalProcessMonitorExpiryObservationSemanticFact,
    Field(discriminator="observation_kind"),
]


class TerminalProcessMonitorObservationCommittedEvent(EventBase):
    schema_version: Literal["terminal_process_monitor_observation_committed.v1"]
    registration_event_reference: ContextEventReferenceFact
    observation: TerminalProcessMonitorObservationSemanticFact
    monitor_state_transition: TerminalProcessMonitorStateTransitionFact
    output_delta_attribution: TerminalOutputDeltaAttributionFact | None
    completion_event_reference: ContextEventReferenceFact | None
    owner_host_session_id: str
    wake_chain_id: str
    observed_at_utc: str
    physical_reservation_id: str
    physical_reservation_fingerprint: str


class TerminalProcessMonitorTerminationSemanticFact(FrozenFactBase):
    schema_version: Literal["terminal_process_monitor_termination_semantic.v1"]
    monitor_id: str
    terminal_reason: Literal[
        "explicit_cancel",
        "session_closed",
        "interrupted_by_host_restart",
        "explicit_process_kill",
        "process_completion_not_delivery_eligible",
        "authority_untrusted",
    ]
    terminal_cursor: TerminalOutputCursorFact
    last_committed_observation_ordinal: int
    termination_semantic_fingerprint: str


class TerminalProcessMonitorTerminatedEvent(EventBase):
    schema_version: Literal["terminal_process_monitor_terminated.v1"]
    registration_event_reference: ContextEventReferenceFact
    termination_semantic: TerminalProcessMonitorTerminationSemanticFact
    monitor_state_transition: TerminalProcessMonitorStateTransitionFact
    notification_account_transition_fingerprint: str
    cause_event_references: tuple[ContextEventReferenceFact, ...]
    terminated_at_utc: str
```

observation event是durable notification authority，但不是transcript semantic message。event validator要求nested `observation_ordinal`等于transition ordinal；ordinal、before/after core state、resulting observation/consumed cursors与registration必须CAS闭合。

eligible process completion和monitor expiry已经由terminal observation分支表达，不再额外写`TerminalProcessMonitorTerminatedEvent`；它们在同一个observation/account transaction中把core state推进到`terminal_pending_delivery`并释放lifecycle slot。status矩阵判定ineligible的process completion写`process_completion_not_delivery_eligible` termination。达到progress上限不会终结monitor：最后一条progress disposition FULL后进入`active_completion_only`，为completion/expiry保留一条terminal observation。`TerminatedEvent`覆盖所有没有模型可见terminal observation的cancel/close/restart/kill/ineligible-completion/authority failure，避免两份terminal outcome。

output authority矩阵：

* `availability=available`：`output_delta_attribution` required，复制的semantic fingerprint必须等于nested delta；
* `availability=unavailable_recovered`：attribution必须为null，只能出现在completion observation；
* live output/heartbeat/expiry observation禁止使用unavailable branch；
* progress/expiry observation只能嵌`RunningTerminalProcessStateFact`，completion reference必须为null；
* completion observation必须嵌完整`TerminalProcessCompletionSemanticFact`，completion event reference required且hydrated semantic逐字段相等；
* progress/heartbeat available delta的requested start必须等于source state的`last_consumed_cursor`，且在ACTIVE_READY时它也等于`last_observation_cursor`；
* completion/expiry terminal delta的requested start必须等于source state的`last_consumed_cursor`；若存在pending progress，其end必须被terminal delta覆盖；
* 所有observation的output cursor stream identity都必须与registration initial baseline和completion terminal cursor（如有）相同；
* unavailable branch只允许restart/reopen recovery owner构造，并使用独立stable recovery contract fingerprint。

### 10.4 Physical writer

每次background observation必须使用独立的accounted operation kind，例如`TERMINAL_PROCESS_MONITOR_OBSERVATION`：

* operation reservation在ACTIVE_READY→FIRING linearization时安装；它不同于贯穿monitor lifetime的capacity reservation；
* candidate canonical bytes与fixed wrapper charge受PhysicalBurstContract约束；
* terminal settlement同批或按现有background operation contract结算；
* direct `EventLog.extend()` 是architecture violation；
* close必须drain FULL/NONE/UNKNOWN owner。

## 11. Completion Projection 与去重 Hard Cut

### 11.1 Completion event schema

`TerminalProcessCompletedEvent` 改为semantic/attribution分层：

```python
class TerminalProcessCompletionSemanticFact(FrozenFactBase):
    schema_version: Literal["terminal_process_completion_semantic.v1"]
    terminal_output_cursor: TerminalOutputCursorFact
    outcome: TerminalProcessLifecycleOutcomeFact
    completion_semantic_fingerprint: str
```

event outer attribution再保存：

* exact terminal process owner（HostSession、conversation、workspace/process generation）；
* exact `origin_runtime_session_id`与origin RunStart/tool-call references；
* required `TerminalOutputRecoveryReferenceFact`，以及可选canonical output artifact/ref或bounded preview semantic；
* duration、producer clock与物理terminalization attribution。

status/exit/kill reason只能来自nested `TerminalProcessLifecycleOutcomeFact`；outer event不得复制第二份。completion的process/journal identity只来自`terminal_output_cursor.stream_identity`，receipt与monitor observation通过同一stream identity完成join。

canonical status/suppression矩阵：

| Lifecycle status | exit/kill_reason invariant | Completion event | monitor completion behavior | Human attachment / restart repair |
|---|---|---|---|---|
| `success` | `exit_code == 0`、reason null | required | eligible | eligible |
| `error` | `exit_code > 0`且不等于124、reason null | required | eligible | eligible |
| `timeout` | `exit_code == 124`、reason null | required | eligible | eligible |
| `killed/user_tool_kill` | `exit_code < 0`、matching reason | required | not eligible；monitor写`explicit_process_kill` terminal outcome并释放slot | kill ToolResult是可见owner，不再追加notification |
| `killed/lifetime_watchdog` | `exit_code < 0`、matching reason | required | eligible | eligible |
| `killed/teardown` | `exit_code < 0`、matching reason | required | not eligible | 只进入`session_closed` disposition与account release |

`running`不是completion status；`blocked`表示process根本未合法启动，不得构造completion。V1删除yielded process的隐式`completion_suppressed/record_event=None`分支：所有已向模型公开`process_id`的background process都必须有confirmed lifecycle completion。模型可见性只由上表与delivery disposition决定。

user kill owner必须在kill ToolResult terminal与monitor terminal outcome之间做exact process/tool-call join；不能仅看到`status=killed`就猜测是explicit observation。

physical process exit由唯一`TerminalProcessTerminalizationOwner` linearize：

```text
RUNNING
    -> COMPLETION_FIRING(journal finish + stable completion candidate)
    -> COMPLETION_CONFIRMED          FULL
    -> COMPLETION_FIRING             NONE/retry
    -> RECONCILIATION_REQUIRED       UNKNOWN/PARTIAL
```

只有`COMPLETION_CONFIRMED` reference可进入monitor arbiter与notification projection。journal final cursor和completion candidate在第一次await前一并转交owner；reader task栈帧不得独占candidate。该background writer同样走accounted physical operation与close drain。

其 transcript domain改为 deterministic no-op/lifecycle authority。数据库需要reset或独立historical migration；不能用同一registry fingerprint把旧semantic classification解释成新规则。

### 11.2 Pending monitor/completion notification

`HostIngressNotificationProjectionStore` 从confirmed monitor observation与completion派生human-visible pending notification：

* 无matching monitor：只允许下一次human run attachment，不自动唤醒；
* matching monitor completion observation FULL：notification获得autonomous eligibility并terminalize monitor；
* matching monitor progress observation FULL：notification获得autonomous eligibility；delivery/receipt后同一monitor re-arm；
* matching receipt FULL且dominates：标记explicitly observed；
* later completion supersede同monitor尚未dispatch的heartbeat/output preview；
* durable source identities全部保留，不合并其audit identity。

派生projection必须由单一versioned reducer生成，不能由Host prompt builder临场扫描ledger。

### 11.3 唯一模型可见 owner

```text
explicit poll/log/wait
    -> ToolResult terminal projection
    -> no runtime notification message

human/autonomous delivery
    -> HostRunIngress typed runtime request/attachment
    -> RunStart transcript projection

active-run safe-point delivery
    -> typed runtime-observation ProviderInput append
    -> next ModelCallStart lifecycle commit
```

safe-point observation属于runtime-owned auxiliary ProviderInput source，不伪造human或canonical transcript message；其durable source、delivery disposition和committed ProviderInput reference必须exact join。后续rollover/rewrite遵守现有runtime-observation lifecycle contract。

删除：

* transcript reducer的`_append_terminal_completion_note()`；
* legacy transcript completion-note reconstruction；
* completion event和monitor observation各写一条可见消息的路径。

### 11.4 Delivery/disposition events

```python
class TerminalProcessObservationDeliveryDispositionEvent(EventBase):
    schema_version: Literal[
        "terminal_process_observation_delivery_disposition.v1"
    ]
    observation_source_references: tuple[ContextEventReferenceFact, ...]
    outcome: Literal[
        "autonomous_dispatched",
        "merged_into_human_run",
        "active_run_safe_point",
        "explicitly_observed",
        "superseded_by_terminal_observation",
        "monitor_cancelled",
        "session_closed",
    ]
    run_start_event_reference: ContextEventReferenceFact | None
    model_call_start_event_reference: ContextEventReferenceFact | None
    tool_result_end_event_reference: ContextEventReferenceFact | None
    resulting_notification_state_fingerprint: str


class TerminalProcessObservationDeliveryDeferredEvent(EventBase):
    schema_version: Literal[
        "terminal_process_observation_delivery_deferred.v1"
    ]
    observation_source_references: tuple[ContextEventReferenceFact, ...]
    reason: Literal[
        "wake_budget_exhausted",
        "autonomy_permission_disabled",
        "host_waiting_user",
    ]
```

nullability矩阵：

* autonomous/merged必须与同批RunStart exact join，ModelCallStart ref必须为null；
* active_run_safe_point必须与同批ModelCallStart及其committed ProviderInput reference exact join，RunStart ref必须为null；
* explicitly_observed必须引用dominant ToolResultEnd；
* superseded_by_terminal_observation必须引用newer completion/expiry observation source，并与terminal observation、old pending progress disposition、resulting monitor state同批提交；
* monitor_cancelled只能清理pending progress，不得消费confirmed completion；
* deferred event类型本身即表示仍pending-for-human；它不是physical monitor cancellation，也不消费human delivery；
* session_closed终结delivery，但保留monitor observation/completion audit。

## 12. Autonomy Chain、权限与 Child 边界

### 12.1 不可绕过的 chain identity

```python
class ResolvedTerminalAutonomyChainPolicyFact(FrozenFactBase):
    schema_version: Literal["resolved_terminal_autonomy_chain_policy.v1"]
    policy_id: str
    policy_version: int
    maximum_automatic_deliveries: int
    minimum_automatic_delivery_interval_seconds: int
    maximum_notifications_per_autonomous_ingress: int
    policy_fingerprint: str


class TerminalAutonomousDeliveryChainAttributionFact(FrozenFactBase):
    schema_version: Literal["terminal_autonomous_delivery_chain_attribution.v1"]
    wake_chain_id: str
    root_human_run_event_reference: ContextEventReferenceFact
    parent_monitor_id: str | None
    parent_automatic_delivery_ordinal: int | None
    resolved_policy: ResolvedTerminalAutonomyChainPolicyFact
    attribution_fingerprint: str


class TerminalAutonomyChainStateFact(FrozenFactBase):
    schema_version: Literal["terminal_autonomy_chain_state.v1"]
    wake_chain_id: str
    state_revision: int
    last_automatic_delivery_ordinal: int
    last_automatic_delivery_at_utc: str | None
    chain_policy_fingerprint: str
    state_fingerprint: str


class TerminalAutonomousDeliveryFact(FrozenFactBase):
    schema_version: Literal["terminal_autonomous_delivery.v1"]
    wake_chain_id: str
    ordered_source_attachment_fingerprints: tuple[str, ...]
    delivery_kind: Literal["active_run_safe_point", "autonomous_run_start"]
    automatic_delivery_ordinal: int
    chain_policy_fingerprint: str
    delivery_fingerprint: str
```

规则：

* human-origin Host run为本轮注册的monitor创建chain root；
* autonomous continuation中新注册的monitor必须继承同一`wake_chain_id`并记录`parent_monitor_id`；
* 一次automatic delivery可合并一到`maximum_notifications_per_autonomous_ingress`个attachments，但全部attachment的non-null `wake_chain_id`必须等于delivery chain；跨chain通知必须留待后续selection，不能共用ordinal；
* `ordered_source_attachment_fingerprints`必须与`RuntimeRequestRunIngressFact.source_notifications`或`HostActiveRunMonitorDeliveryFact.ordered_attachment_fingerprints`逐项、同序相等，且每项unique；budget fact不得只挑一个monitor代表整批；
* ordinal在`active_run_safe_point` ModelStart FULL或`autonomous_run_start` RunStart FULL时消耗，且不得超过chain attribution中resolved policy的maximum；不论本次包含几个attachments，一次model sampling只消耗一个ordinal；
* human merge和explicit receipt不消耗automatic delivery ordinal；
* 注册多个monitor不能预先消耗或重置budget；
* 重新注册新monitor不能创建新chain绕过上限；
* human发起的新run可以创建新chain；
* `wake_budget_exhausted` 只禁止自动dispatch，observation仍pending供human/Inspector查看；由于每monitor最多一个pending progress，monitor不会继续堆积progress事件，confirmed completion仍可supersede。

delivery复制的`chain_policy_fingerprint`必须等于chain attribution中nested resolved policy；它是bounded equality join，不是第二份policy。历史最大automatic delivery数、间隔和每次notification上限只由`ResolvedTerminalAutonomyChainPolicyFact`拥有。safe-point与new-run两条路径必须CAS同一个`TerminalAutonomyChainStateFact`，校验revision、last ordinal、minimum interval和整组ordered attachment fingerprints，不能各自维护counter。

### 12.2 Bounds

V1 policy至少冻结：

```text
max_active_monitors_per_host_session = 8
max_active_monitors_per_process = 1
min_heartbeat_interval_seconds = 5
max_heartbeat_interval_seconds = 1800
max_monitor_lifetime_seconds = 36000
max_committed_progress_observations_per_monitor = 119
reserved_terminal_observations_per_monitor = 1
progress_observation_rate_window_seconds = 600
max_progress_observations_per_rate_window = 60
min_progress_observation_interval_seconds = 5
max_automatic_terminal_deliveries_per_chain = 12
min_automatic_delivery_interval_seconds = 5
max_pending_runtime_notifications = 16
max_notifications_per_autonomous_ingress = 8
```

具体常量由versioned policy fact拥有，doctor证明registration、最多119条progress和预留的1条terminal observation、Host ingress的physical bounds可容纳最大合法payload。progress limiter严格使用§9.2的sliding-window recurrence并由durable tuple恢复；不存在额外burst bucket。达到progress cap后进入`ACTIVE_COMPLETION_ONLY`，completion/expiry仍可提交预留terminal observation。automatic delivery interval只来自chain policy，不与observation limiter混用。

同一process已有ACTIVE/FIRING/pending-delivery monitor时，新registration稳定拒绝`terminal_monitor_already_active_for_process`；V1不做隐式policy update或replacement。调用者必须先cancel旧monitor并确认terminal FULL，再注册新monitor。

### 12.3 Permission gate

`monitor` 不属于 `TERMINAL_PROCESS_READ_ONLY_ACTIONS`。新增独立分类：

```text
effective_permission_category = terminal_process_schedule
```

```python
class TerminalAutonomyPermissionAuthorityFact(FrozenFactBase):
    schema_version: Literal["terminal_autonomy_permission_authority.v1"]
    registration_permission_snapshot_id: str
    registration_permission_mode: str
    registration_permission_policy_fingerprint: str
    scheduling_policy_id: str
    scheduling_policy_version: int
    scheduling_policy_fingerprint: str
    caller_owner_kind: Literal["host_main_run"]
    authority_fingerprint: str
```

并冻结：

* registration-time autonomy/scheduling gate；
* dispatch-time current Host autonomy gate；
* permission snapshot与registration/RunStart join；
* ASK结果通过现有pending interaction owner，不由monitor私建UI prompt；
* policy收紧后旧monitor只能defer给human，不能继续付费调用。

`TerminalAutonomyPermissionAuthorityFact`只存在于获准的registered event，因此不复制`registration_allowed=True`。拒绝必须返回独立typed tool result/reason，且不得构造permission authority、registration event或notification reservation。

### 12.4 Child/subagent

V1只允许committed Host main run注册autonomous monitor。child/subagent调用`monitor`稳定拒绝：

```text
terminal_monitor_child_owner_unsupported
```

child仍可使用`poll/log/wait`。未来若支持child，必须先增加typed wake target、parent/child ledger refs、reparent authority和跨ledger terminal join；不得默认“唤醒parent”或恢复child。

同样禁止main agent注册一个child-origin process ID：即使ProcessRegistry的`owner_host_session_id`相同，只要`process.origin_runtime_session_id`不是Host main RuntimeSession，仍返回`terminal_monitor_cross_ledger_process_unsupported`。这条restriction必须由process metadata + registration validator执行，不能依赖模型不传该ID。

### 12.5 Output trust boundary

terminal output始终是不可信数据：

* wire只通过中央`pulsara_runtime_observation`/`pulsara_runtime_request` canonical JSON codec生成；
* output preview只能位于typed payload string字段，必须JSON escape；
* root protocol明确其为process data，不允许覆盖root policy或伪造human attribution；
* adapter pre-send guard从typed carrier重编码并逐字节比较，不能只验证wire JSON形状；
* arbitrary log text不能产生monitor condition、permission change或tool instruction；
* raw environment、command secrets和未sanitized spool不得进入notification payload。

## 13. Recovery、Cancel 与 Close

### 13.1 Live detach

UI/transport detach不取消monitor，不影响journal。HostSession仍是logical owner。

### 13.2 HostCore restart

V1不重新adopt OS process：

* durable registration无terminal outcome且无confirmed completion时，写`interrupted_by_host_restart`；
* confirmed completion晚于registration时，可精确恢复status/completion/cursors并补最终completion observation；
* spool recovery reference覆盖monitor `last_consumed_cursor`到terminal cursor的range时，补`availability=available`的exact terminal delta；
* typed quota/spool-write/queue/fsync gap覆盖range，或artifact有confirmed GC tombstone时，补`availability=unavailable_recovered`的稳定completion observation；
* manifest/page无对应typed gap或GC authority却缺失、hash冲突、codec/sanitizer historical binding冲突时标记recovery authority untrusted；不得把corruption降级成普通unavailable；
* unavailable recovery不得复用completion preview冒充baseline delta，也不得自报原delta hash；
* already FULL observation可由Host notification projection恢复并交付；progress disposition后只在live process仍由同一HostCore拥有时重新`ACTIVE_READY`；
* FIRING UNKNOWN/PARTIAL保持reconciliation required；
* notification reservation account与projection row必须先通过revision/fingerprint join，才能恢复pending余额；
* 首次notification/monitor projection checkpoint的validation base必须是各自唯一的sequence-zero canonical genesis；notification genesis固定runtime identity、8/8容量、reducer contract与空heads，monitor genesis固定runtime identity、reducer contract与空records；
* 后续checkpoint validation base必须与PostgreSQL前一row完整相等，不能从caller自报的另一套base开始重放；
* HostCore在runtime wiring前冻结唯一absolute reopen deadline；RuntimeSession的materialization/transcript/provider-input/tool/long-horizon bootstrap、notification restore、monitor restore、projection cross-join、HostSession plan bootstrap与physical monitor-owner recovery均复用该timestamp，任何子阶段不得重新执行`monotonic() + timeout`；
* 不得根据PID碰巧存在而重新arm。

### 13.3 Cancel

* `cancel_monitor`必须使用§9.5的session-owned cancellation owner；不存在“先改live state，再best-effort写ToolResult”的路径；
* ACTIVE_READY/ACTIVE_PENDING_DELIVERY的唯一成功结果，是pending progress disposition（如有）、typed termination、reservation release、projection transition、cancel ToolResult terminal projection、ToolResultEnd与settlement同批FULL；
* FIRING不能丢弃candidate。cancel intent加入原FIRING owner；progress FULL后再提交原子cancel batch，completion/expiry FULL则返回typed `already_terminal`；
* TERMINAL_PENDING_DELIVERY的physical monitor已经结束，后续只能改变delivery disposition；cancel不得消费pending completion，也不得写第二份termination；
* NONE保留exact batch，UNKNOWN/PARTIAL latch；caller cancel-after-FULL先adopt结果；
* cancel只终结monitor，不终止process，也不伪造process completion。

### 13.4 Host close

唯一close顺序：

```text
close ingress + tool + process admission
    -> stop/finish active run per existing contract
    -> terminate ACTIVE_READY/ACTIVE_PENDING_DELIVERY/ACTIVE_COMPLETION_ONLY monitors
    -> close/kill owned processes and finalize journals
    -> drain completion + monitor observation/terminalization owners
    -> disposition all remaining notifications as session_closed
    -> final notification projection/account/writer drain
    -> release terminal lease, journal handles and HostSession resources
```

`FIRING`/reconciliation monitor不能被active-monitor termination sweep删除；它们在terminalization drain中确认。teardown process仍写`killed/teardown` lifecycle completion，但不产生automatic/human delivery。

任一completion、monitor、projection或account owner无法FULL/reconcile时，close attempt失败并保留indexed HostSession/terminal lease供相同close重试；不得继续释放workspace资源。不得长时间持有Host run lock完成I/O；每个owner使用stable candidate + confirmation state。

## 14. Durable Event 与 State Matrix

V1新增或hard-cut：

| Durable carrier | 唯一 owner | Transcript semantic | 关键 join |
|---|---|---|---|
| `TerminalProcessMonitorRegisteredEvent` | tool terminal owner | no-op | ToolResultEnd + registration semantic |
| `TerminalProcessMonitorObservationCommittedEvent` | observation coordinator | no-op | registration + stream cursor + branch-required completion |
| `TerminalProcessMonitorTerminatedEvent` | observation/cancellation/recovery owner | no-op | registration + prior monitor state + reason-specific batch |
| `TerminalProcessCompletedEvent` | process terminalization owner | no-op | stream identity + terminal cursor + lifecycle outcome |
| notification reservation created/released | notification account commit port | no-op | account before/after + process/monitor cause |
| ToolResultEnd receipt | tool terminal owner | existing tool result | receipt + terminal document |
| cancel ToolResultEnd | cancellation owner | existing tool result | termination + reservation release + pending disposition |
| delivery disposition | Host ingress/receipt owner | no-op | RunStart or ToolResultEnd |
| deferred delivery | Host ingress owner | no-op | monitor observation + chain/policy |
| Host RunStart ingress | Host ingress coordinator | yes | typed ingress + admission proof + delivery + permission |

中央validator至少冻结：

* every durable DTO `frozen=True/extra="forbid"/schema_version`；
* own semantic fingerprint与outer fact/attribution fingerprint分层；
* Host ingress attribution复制的semantic fingerprint等于nested semantic identity；
* exact notification source refs只由`HostRuntimeNotificationAttachmentFact`拥有且sorted/unique；
* start/end cursor共享同一个stream identity且offset单调，所有char/byte counts由cursor差重算；
* available output必须有matching delta attribution；unavailable recovery禁止delta attribution/伪造body；
* output delta attribution复制的semantic fingerprint等于monitor observation nested delta semantic；
* terminal lifecycle outcome的status/exit/kill reason矩阵，`timed_out`只作为`status == timeout`派生property；
* completion observation/receipt中的nested completion semantic等于hydrated completion reference；
* registration/process/completion/observation/RunStart refs全部属于state声明的single runtime ledger且sequence不超过`source_through_sequence`；
* completion condition固定启用；output/heartbeat optional，不能以bool关闭terminalization；
* registration/observation/disposition/termination的monitor revision连续，progress FULL后仍可回到ACTIVE_READY；
* `last_consumed_cursor <= last_observation_cursor`，threshold只从observation cursor计算，而completion/expiry从consumed cursor构造；
* 每monitor最多一个pending progress；completion/expiry supersede必须同批处置旧progress并覆盖其未消费区间；
* FULL前ACTIVE_READY/FIRING状态矩阵；
* receipt dominance必须证明可见区间start不晚于pending available start且end不早于pending end；artifact branch还要证明exact range/hash，completion branch分别证明lifecycle与output；
* disposition outcome/reference矩阵；
* Host ingress admission proof与durable Host state、selected heads及permission revision精确CAS；
* notification account source/result revision、before/after fingerprint、capacity与release公式；
* notification account transition方向由outer Created/Released event唯一决定；
* automatic delivery的ordered attachment fingerprints与runtime ingress/safe-point companion逐项相等、同chain且bounded；一次sampling只消耗一个连续ordinal；
* progress limiter严格按sliding-window recurrence重算；progress cap后的唯一非terminal状态是`active_completion_only`，completion/expiry绕过progress limiter；
* cancel ToolResult成功只允许来自§9.5原子batch，FIRING cancel intent不得取代原candidate；
* journal end与successfully spooled cursor分别推进；spool gap/degraded/authority-untrusted矩阵及terminal bounded drain闭合；
* Host runtime ingress不能携带human attribution；
* child owner不能注册monitor；
* preview chars/UTF-8 bytes从正文重算，artifact ref bounds受统一policy约束；
* all same-batch IDs无fingerprint递归环。

stable event ID由唯一factory确定性派生：registration使用`monitor_id`，observation使用`monitor_id + observation_ordinal`，terminal outcome使用`monitor_id + terminal reason`，completion使用process generation，delivery使用ordered observation source IDs + cause RunStart/ToolResult ID。retry必须重用同一candidate ID与payload；不得用当前UTC或随机UUID重新生成。

## 15. 分阶段实施

每个阶段必须独立全绿；production `monitor` capability直到TM5才公开。旧TW命名只对应已删除的one-shot设计，不再使用。

### TM0：Output journal 与 typed receipt

实现：

* `SanitizedOutputJournal`替换`OutputAccumulator`；
* bounded immutable segments、mandatory bounded spool、cursor、delta、condition；
* partial-line与stateful sanitizer；
* `poll/log/wait` receipt进入ToolResult terminal document；
* process additive冻结`origin_runtime_session_id/run-entry kind`；
* available/unavailable recovered delta union；
* terminal completion事件先additive携带terminal cursor/recovery ref与唯一typed lifecycle outcome，旧transcript行为暂不切换；
* notification reservation account DTO/commit port先以shadow state落地，不切production capacity。

Gate：

* chunk/ANSI/secret跨边界结果与one-shot canonicalization一致；
* 无换行`progress=73%`可在quiet bound内观察；
* retained eviction返回精确gap；
* restart有range时重建exact delta，无range时只能生成typed unavailable recovery；
* 10万segments内存保持policy bound；
* spool达到quota后推进gap且disk保持bound；
* ENOSPC、permission failure、queue overflow与fsync timeout分别产生typed gap；journal cursor继续推进而successfully-spooled cursor不虚增；
* spool writer被阻塞时child stdout reader仍持续drain，进程行为不受磁盘I/O反压；
* partial temp page不进入manifest；committed page hash conflict使recovery authority untrusted，但completion仍能在terminal drain deadline内收口；
* callback detach后journal继续推进；
* running/terminal observed-state union与cursor invariant全覆盖；
* lifecycle outcome的status/exit/kill reason 6行矩阵全覆盖，并证明无独立`timed_out`字段。

### TM1：Atomic registration 与 persistent monitor shadow

实现：

* dormant registration owner；
* registration + ToolResult terminal + settlement atomic commit；
* `ACTIVE_READY/FIRING/ACTIVE_PENDING_DELIVERY/ACTIVE_COMPLETION_ONLY/TERMINAL_PENDING_DELIVERY`状态机；
* background accounted observation writer；
* post-FULL immediate recheck；
* registration same-ledger validator；
* notification account的monitor-lifecycle reservation shadow transition；
* repeated observation ordinal、observation/consumed双cursor和one-pending-progress reducer；
* Inspector shadow projection。

Gate：

* registration窗口内completion不丢；
* main caller对child-origin/cross-ledger process稳定拒绝；
* ToolResult NONE不activate；
* cancel-after-FULL先adopt再传播；
* observation NONE保持FIRING stable candidate；
* UNKNOWN/PARTIAL latch且不发布notification；
* progress FULL后monitor回到pending-delivery，disposition FULL后自动re-arm；
* pending progress期间不生成第二条progress，completion仍可supersede；
* delivered cursor 0、pending progress 0..100、completion tail 100..150时，最终terminal observation覆盖0..150且与旧progress disposition同批；
* `cancel_monitor` NONE/UNKNOWN/cancel-after-FULL与FIRING intent矩阵不丢candidate，成功ToolResult与termination/release/disposition同批；
* close drain不丢owner；
* production tool schema仍不暴露`monitor`。

DTO gate还必须证明registration只从initial baseline cursor取得stream identity，progress/completion/expiry observation union不能构造条件性null组合，observation ordinal与state transition严格连续。

### TM2：Generic HostIngressCoordinator 与 typed run-entry

实现：

* human/resume/runtime统一coordinator；
* per-ingress QUEUED/PREPARING/REPLAN/RECONCILIATION owner；
* `HostRunIngressFact`与RunStart hard cut；
* 唯一`HostIngressAdmissionProofFact`、durable Host state row与RunStart commit CAS；
* Context/ProviderInput/permission/Inspector join；
* synthetic runtime notification用于测试；
* `run_turn()`改为submit + await；
* 禁止所有direct boundary creation旁路。

Gate：

* human与runtime同时排队时human稳定获胜；
* barrier测试真正让两个producer同时进入selection，不依赖task调度运气；
* PREPARING后到达human进入下一selection，不破坏已linearized run；
* QUEUED caller cancellation撤回，PREPARING caller cancellation只detach；
* queue capacity拒绝发生在ordinal分配前；
* notification supersede、permission revision与close intent使admission proof replan；
* stale replan释放prepared handles且不消耗delivery ordinal；
* WAITING_USER只接收matching resume；
* RunStart NONE/UNKNOWN不消费notification；
* runtime ingress在wire上是`pulsara_runtime_request`，不是human；
* attachment是exact source refs唯一owner，placement/runtime owner不能复制refs；
* 原有Host human run行为等价。

### TM3：Completion projection hard cut 与 explicit consumption

实现：

* completion退出transcript semantic domain；
* 删除legacy completion note；
* notification projection reducer；
* notification reservation account切production acquire/release；
* receipt dominance与explicitly-observed disposition；
* human merge/autonomous disposition与RunStart atomic join；
* restart/Inspector exact projection；
* event-domain/decoder/checkpoint schema hard cut及DB reset/migration。

Gate：

* completion + matching monitor observation只产生一个模型可见input；
* poll/log/wait可见区间完整覆盖pending observation后不再为该observation auto wake，running monitor随后继续`ACTIVE_READY`；
* pending 0..100而tail receipt仅覆盖90..110时不能消费；inline 0..110或exact artifact range才可dominant；
* completion receipt必须同时覆盖exact lifecycle outcome和pending output range；只看到status或只看到tail都不能消费；
* running receipt不能消费completion；
* receipt/observation race在active run terminal前收口；
* no-monitor completion只在下一human run出现，不自动调用模型；
* progress delivery不释放monitor slot；completion/expiry/termination FULL释放monitor slot；最终delivery/receipt/close释放completion slot并retire head；
* 顺序完成超过account capacity数量的process，在每次消费后仍可继续yield新process；
* teardown/watchdog都写lifecycle completion，但只有status matrix允许的分支进入delivery；
* close按process finalization后再drain completion/account的顺序收口；
* exact replay重建相同pending/consumed状态。

projection gate还必须证明`pending_count`由heads重算、autonomous eligibility由当前selection gate重算，Created/Released outer event唯一决定account transition方向。

### TM4：Repeated monitor delivery 与 PRE_MODEL_STEP safe point

实现：

* output/heartbeat/completion/expiry arbiter；
* one-pending-progress coalescing、唯一sliding-window limiter、completion-only progress cap与monitor lifetime hard cap；
* autonomy permission与child rejection；
* delivery chain/ordinal/budget；
* bounded human/runtime notification merge；
* `PRE_MODEL_STEP` safe-point delivery owner；
* `ActiveRunMonitorSafePointCommitGuardFact`与ModelStart lifecycle companion；
* `ModelStreamStartCommitPort`原子接纳ProviderInput append、delivery disposition和chain transition；
* close/restart operational metrics；
* production tool schema仍不暴露`monitor`。

safe-point唯一算法：当前run已完成前一tool/control FULL、没有queued human/resume ingress，且下一`ContextCompiled`/ProviderInput尚未冻结时，Host从durable notification projection借用confirmed monitor observations；它在同一authority snapshot冻结active segment、stop/close/termination revisions、permission、latest control disposition、ProviderInput generation、interaction/tool frontiers、notification和chain state，随后准备typed runtime-observation companion。`LLMRuntime`在下一ModelStart writer lock内重验专用guard，并在同一transaction提交ProviderInput append、delivery disposition、chain-state CAS与ModelCallStart。若该run不会再发起model call，借用撤销且observation保持pending，之后走普通Host ingress。已经dispatch的provider call永不修改。lease取得后到达的human input进入下一selection，不回到过去撤销已linearized safe point。

Gate：

* active run在下一次model sampling前收到confirmed monitor observation，不额外创建RunStart；
* observation在Context/ProviderInput冻结后到达时不注入当前call；
* ModelStart NONE/UNKNOWN不消费safe-point observation；
* preparation后、commit前的stop/close/permission/control-disposition/model-step ABA全部返回typed replan且无部分batch；
* pending interaction或open tool pair存在时safe-point fail closed，不开启新model call；
* safe-point与human ingress、receipt、completion supersede遵守同一notification-head CAS；
* 同一observation只能由safe-point、autonomous RunStart、human merge或explicit receipt之一消费；
* rate limiter/restart不因进程重启而重置；
* progress持续输出不会制造无界durable queue。
* sliding-window在边界`T-window`、clock rollback、NONE retry和restart restore下产生相同eligibility与stable candidate；仓库不存在第二套burst/token-bucket/fixed-window算法；
* 第119条progress消费后进入`ACTIVE_COMPLETION_ONLY`，不再写第120条progress；随后completion或expiry仍提交预留terminal observation；
* 一次safe-point/autonomous sampling合并多个同chain attachments时，budget fact逐项覆盖整组且只消耗一个ordinal；跨chain attachment不合并；

### TM5：Public monitor、UI stream 与 dogfood

实现：

* 公开`terminal_monitor.register`、`terminal_monitor.cancel`、`terminal_monitor.list`；`terminal_process`只保留进程操作；
* 删除public/internal `watch` action、旧DTO/event decoder与compat alias；
* structured UI `terminal_monitor_event` channel，提供stream cursor、bounded replay、subscriber backpressure与detach语义；
* dogfood与Inspector最终投影。

Gate/dogfood：

1. 训练脚本每30秒输出epoch；同一个monitor产生至少两条progress和一条completion，不重新注册；
2. dev server启动后，monitor捕获ready输出，再执行health check；
3. 无输出进程由重复heartbeat报告“仍运行、无新输出”，达到lifetime后typed expiry；
4. output与completion同时到达只产生一次delivery；
5. active run中observation在合法PRE_MODEL_STEP注入；无safe point时run结束后再续跑；
6. human input与runtime notification竞态遵守统一priority；
7. explicit poll/log/wait消费不早于observation的通知，monitor随后继续观察；
8. session close终结monitor且不伪造completion；
9. slow/detached UI subscriber不影响journal/monitor owner；
10. 同一monitor反复通知不能重置chain budget；autonomous continuation中新建monitor也必须继承chain；
11. budget exhausted不调用模型，但下一human run仍看到pending observation；
12. child注册monitor稳定拒绝。
13. main agent拿到child-origin process ID后注册monitor同样稳定拒绝；
14. recovery range缺失时模型只看到typed output-unavailable，不看到伪造delta；
15. sequential process/head reuse证明slot不会泄漏；
16. 10小时hard-cap fixture以虚拟时钟验证，无真实长等待；
17. architecture guard证明仓库不存在`terminal_process.watch`或`TerminalProcessWatch*`生产引用。
18. progress达到上限后进程继续运行并最终完成，模型仍收到completion，而不是静默失去monitor；
19. 两个同chain monitor同时ready时可在一个model sampling中交付，并由一个ordinal覆盖两个exact attachment fingerprints；
20. ENOSPC与spool fsync timeout dogfood使用fault injection证明terminal completion不被磁盘故障阻塞。

## 16. 本地实现调研

调研日期：2026-07-21。阅读对象：

* `/Users/plumliu/Desktop/python_workspace/claude-code`；
* `/Users/plumliu/Desktop/python_workspace/grok-build`。

### 16.1 Claude Code

Claude Code的background Bash/task notification形成了真实自动续跑链：

1. background shell task将输出保存到task output file；
2. terminal后原子设置`notified`并把`task-notification`放入module-level priority queue；
3. active query可在后续safe point消费notification；
4. query idle时，`useQueueProcessor`订阅统一队列并自动启动新query；
5. 同priority/mode items可以批量dispatch。

关键代码：

* `src/tasks/LocalShellTask/LocalShellTask.tsx`；
* `src/utils/messageQueueManager.ts`；
* `src/utils/queueProcessor.ts`；
* `src/hooks/useQueueProcessor.ts`；
* `src/query.ts`。

其`now > next > later`统一priority queue与query guard直接支持本规格的结论：terminal producer不应自己拥有Host dispatch。

Claude Code还包含one-shot stall detector：定期检查output file；长时间无增长且尾行像交互提示时发送一次notification。Pulsara V1只吸收“fixed registry / Host-owned condition”思想，不开放任意regex。

### 16.2 Grok Build

Grok Build更接近Host notification owner：

* MonitorTool后台tail session-owned output file；
* monitor本身可运行独立shell/filter script，stdout每行都可形成事件；
* 默认最长约10小时，并支持session-lifetime persistent模式；
* backend使用`Weak`，monitor不延长session/process生命周期；
* notification携带`owner_session_id`；
* completion由`TaskCompleted`唯一负责，避免额外terminal MonitorEvent；
* active/idle状态在同一session-state lock中判断；
* `NotificationDrain`只在无active turn、无queued user prompt时运行；
* pending notifications可合并为`PromptOrigin::NotificationDrain` synthetic turn；
* model显式读取terminal result时清除matching notification；
* goal/autonomy owner可抑制synthetic wake。

关键代码：

* `crates/codegen/xai-grok-tools/src/implementations/grok_build/monitor/tool.rs`；
* `crates/codegen/xai-grok-tools/src/implementations/grok_build/monitor/types.rs`；
* `crates/codegen/xai-grok-shell/src/session/acp_session_impl/notification_drain.rs`；
* `crates/codegen/xai-grok-shell/src/session/acp_session_tests/auto_wake_suppression_tests.rs`。

其200ms debounce、bounded line/batch、token bucket与queue cap证明高流量保护必须由harness实现。Grok还会在下一次model sampling前注入active-turn notification，而不是总要创建新turn。但Pulsara不复制“每个stdout line最终成为模型消息”的高成本路径，也不在V1开放任意shell filter作为authority。

### 16.3 吸收与不吸收

| 维度 | Claude Code / Grok | Pulsara V1 |
|---|---|---|
| 调度owner | unified queue/session lock | `HostIngressCoordinator` |
| output authority | task/session output file | sanitized journal + cursor + artifact |
| completion去重 | notified latch / TaskCompleted | lifecycle event no-op + ingress/ToolResult唯一projection |
| explicit consumption | active query/result sweep | durable receipt + dominance + disposition |
| active run | safe-point或buffer | TM4 `PRE_MODEL_STEP` typed safe-point；否则buffer到Host ingress |
| 持续通知 | Grok可逐行、多次event | 同一monitor多次bounded observation；one-pending progress coalescing |
| 生命周期 | 最长约10小时/session persistent | V1最长10小时或process lifetime，Host close必终结 |
| filter | 任意shell/filter script | V1固定output/heartbeat/completion registry |
| child/reparent | session ownership/reparent | V1 same-ledger Host main only，跨ledger留待独立hard cut |
| 调度class | Next/Later、goal suppression | typed urgent/progress/informational + current policy gate |
| durable确认 | 主要process/session state | registration/observation/RunStart typed FULL joins |
| autonomy limit | gate/token bucket | permission + monitor rate limit + chain identity + ordinal budget |
| UI channel | structured monitor events | TM5 cursor/replay/backpressure contract |

明确不复制：

* process-global、跨HostSession的notification queue；
* 每行stdout触发消息/模型调用；
* 任意模型生成的shell/regex filter直接成为durable condition authority；
* XML或自由字符串承担runtime authority；
* monitor owner强引用延长terminal/session生命周期；
* 用提示词要求模型“记得稍后poll”替代scheduler。

### 16.4 与旧 one-shot 方案相比的产品结论

讨论中最初的问题是：“如果旧API只唤醒一次，它与`wait`究竟差在哪里？”答案是旧API仅把等待从当前tool ownership搬到Host ownership，虽有detach/restart价值，却仍要求模型反复注册。这个产品形状既没有Grok monitor的持续体验，又引入完整durable registration成本。

因此本规格作出明确hard cut：

* `wait`保持短时、同步、一次ToolResult；
* `monitor`是持续、异步、多次observation；
* progress delivery后由harness自动re-arm，不再要求模型调用另一个工具；
* output burst通过debounce、one-pending、唯一sliding-window limiter和chain budget吸收；
* completion仍由`TerminalProcessCompletedEvent`唯一证明，monitor只提供delivery authority；
* active-run低延迟由PRE_MODEL_STEP safe point解决，idle时才创建autonomous run；
* UI持续tail与模型wake共享journal，但各自拥有独立backpressure和成本边界。

## 17. 修改面与长期契约

预计主要修改：

* `runtime/terminal/output.py`或新`output_journal.py`；
* `runtime/terminal/process.py`；
* `tools/base.py`、`tools/builtins/terminal_process.py`；
* `runtime/agent.py`的tool terminal commit；
* `host/session.py`、`host/run_boundary.py`与新`host/ingress.py`；
* `llm/commit.py`、`llm/lifecycle.py`、`llm/runtime.py`的ModelStart lifecycle bundle与safe-point guard；
* `primitives/run_entry.py`、`primitives/runtime_observation.py`；
* 新`primitives/terminal_observation.py`、`primitives/host_ingress.py`；
* `event/events.py`与event-domain registry/historical decoder；
* `runtime/authority_materialization/transcript_reducer.py`；
* context input、ProviderInput、permission、Inspector、recovery与accounting paths。

需要同步：

* Agent loop / Host run-entry contract；
* LLM transport/user carrier contract；
* Message transcript projection contract；
* EventLog storage/accounting contract；
* Recovery contract；
* Permission policy contract；
* Inspector projection contract；
* terminal process tool/result contract。

Architecture guards至少禁止：

* Host boundary绕过`HostIngressCoordinator`；
* monitor tool直接写EventLog；
* production暴露`terminal_process.watch`/`wake_when` alias或定义`TerminalProcessWatch*`类型；
* `TerminalProcessCompletedEvent`进入transcript semantic projection；
* production继续构造`OutputAccumulator`；
* monitor归类为read-only observe；
* child runtime产生monitor registration；
* main runtime monitor cross-ledger/child-origin process；
* yielded process通过`completion_suppressed`跳过lifecycle completion；
* notification reservation release与process-head retirement分事务；
* monitor observation FULL前调用Host autonomous dispatch；
* production定义或引用旧`HostIngressDispatchReservationFact`/`HostIngressCommitGuardFact`；
* completion/receipt/monitor-observation/wire payload绕过`TerminalProcessLifecycleOutcomeFact`复制status/exit/kill reason；
* output authority DTO保存可由cursor差重算的char/byte counts，或重复保存process/journal identity；
* ingress placement/runtime owner复制attachment已经拥有的notification source refs。

## 18. Definition of Done

只有同时满足以下条件，terminal monitor V1才完成：

* output memory与delta materialization有可证明bound；
* managed process有mandatory bounded spool与typed unavailable recovery；
* registration与ToolResult terminal原子FULL；
* registration、completion、monitor observation与Host ingress在V1属于同一runtime ledger；
* observation candidate在await/取消/UNKNOWN期间不失去owner；
* 同一monitor可提交多个严格连续observation，progress disposition后自动继续，不需要模型重新注册；
* 每monitor最多一个pending progress，observation/consumed双cursor保证terminal supersede不丢未交付区间；
* progress cap进入completion-only并保留terminal observation capacity；completion/expiry不会被debounce、limiter或cap丢失；
* human、resume、runtime notification只有一个Host ingress仲裁点；
* 每个ingress attempt有cancellation/replan/reconciliation owner，RunStart只消费唯一admission proof并完成commit CAS；
* notification reservation account acquire/release/retirement可exact restart；
* autonomous RunStart拥有合法typed ingress和permission provenance；
* completion不再直接写模型可见note；
* explicit ToolResult与runtime ingress不会重复同一observation；
* wake chain不能通过重新注册重置；
* 一次automatic sampling的budget fact精确覆盖全部ordered attachments，不以单个monitor代替整批；
* child/autonomy permission边界fail closed；
* close/restart/Inspector可解释每个registration、observation、cursor advancement和delivery结果；
* cancel成功结果与pending disposition、termination、reservation release、ToolResult terminal和settlement原子闭合；
* spool I/O不反压process reader；journal cursor、spooled cursor、typed gap与recovery authority可独立审计；
* lifecycle outcome、output stream identity和notification source evidence均只有一个durable owner；
* 所有可由union branch、outer event、cursor或当前policy唯一推导的bool/count不进入durable DTO；
* TM0-TM5 gate与dogfood全部通过。

完成这些边界后，产品形状是：模型注册一次monitor，Host可在进程生命周期中多次交付有意义的bounded observation；是否发生付费模型调用仍由safe point、human priority、permission、rate limit和wake-chain budget共同决定。持续性属于monitor lifecycle，不等于无限自动调用。

### 18.1 实施验收记录（2026-07-21）

本规格已经按TM0-TM5的垂直顺序落地；production capability直到TM5才公开，未保留旧`watch`兼容入口：

| 阶段 | 主要代码落点 | 验收证据 |
|---|---|---|
| TM0 | `runtime/terminal/output.py`、`primitives/terminal_observation.py` | stateful sanitizer、bounded memory/spool、cursor/gap/recovery、typed lifecycle矩阵；`test_terminal_monitor_tm0.py`覆盖边界切分、partial line、10万segment、quota/ENOSPC/permission/fsync/queue/hash-conflict故障 |
| TM1 | `runtime/terminal/monitor.py`、terminal tool execution/settlement路径 | dormant registration、same-batch ToolResult、FIRING owner、双cursor、重复observation、completion supersede和原子cancel；纯reducer gate与Host实际注册/完成路径均有覆盖 |
| TM2 | `host/ingress.py`、`primitives/host_ingress.py`、`host/session.py`、RunStart DTO | human/resume/runtime唯一仲裁、per-ingress owner、admission proof/CAS、stop/close cancellation ownership；priority barrier、detach/withdraw、capacity/replan、WAITING_USER和permission revision gate均通过 |
| TM3 | `runtime/terminal/notification.py`、transcript reducer、event/domain/recovery/Inspector | completion lifecycle-only hard cut、receipt dominance、notification account acquire/release/retire、exact replay；tail不完全覆盖、artifact exact range和sequential slot reuse均有确定性测试 |
| TM4 | Host safe-point、LLM lifecycle companion、monitor arbiter与chain reducer | output/heartbeat/completion/expiry、sliding window、completion-only reserve、10小时虚拟时钟、PRE_MODEL_STEP原子提交、同chain多attachment单ordinal；真实Host路径证明不额外创建RunStart |
| TM5 | `tools/builtins/terminal_monitor.py`、`runtime/terminal/ui_stream.py`、Inspector | 公开`terminal_monitor.register/list/cancel`，bounded UI replay/backpressure，删除旧API；本地真实进程dogfood覆盖重复progress、dev server ready后HTTP health check、safe point、同chain批量与cancel/list |

最终验证口径：

* 串行全量：`uv run pytest -q`，`2354 passed, 2 skipped`，耗时`1413.06s`；
* TM0-TM5纯状态机、故障注入与architecture gate：`71 passed`；
* 真实本地Host/process dogfood：`6 passed`，覆盖重复progress、ready后HTTP health check、PRE_MODEL_STEP、同chain批量与cancel/list；
* PostgreSQL/EventLog夹具共享进程级资源，因此并行`xdist`结果不作为本阶段正确性判据；
* architecture guard扫描整个production source tree，禁止`terminal_process.watch`、`wake_when`、`TerminalProcessWatch*`和`OutputAccumulator`，并禁止completion重新进入transcript semantic projection；
* TM5使用确定性的本地Host/process dogfood，不依赖网络API或真实LLM随机性；本阶段没有把“全量real-LLM”伪装成monitor correctness gate。

当前保留的产品边界是有意约束，而非未完成项：V1拒绝child/cross-ledger monitor，不执行任意shell filter，不允许UI subscriber拥有模型delivery，也不把已经dispatch的provider call临场改写。

## 19. Reviewer Findings 收口索引

| Finding | 冻结位置 | 结论 |
|---|---|---|
| Host无合法autonomous run-entry | §7、§8 | 新增`HostRunIngress`并贯穿RunStart/ProviderInput/Inspector |
| terminal queue不能保证user优先 | §8 | 所有human/resume/runtime notification进入唯一coordinator |
| registration与ToolResult存在crash window | §4.2、§9.3 | dormant prepare，same-batch FULL后activate/recheck |
| observation过早丢owner | §10.1、§10.4 | `FIRING`保存candidate/reservation，FULL才publish |
| completion与monitor observation无法从append-only transcript去重 | §11 | completion改为lifecycle no-op，ToolResult或RunStart为唯一projection owner |
| explicit observation无durable cursor receipt | §6 | receipt进入ToolResult terminal并按dominance提交disposition |
| OutputAccumulator不支持bounded cursor | §5 | 正式替换为stateful sanitized journal |
| automatic delivery budget可由重复notification/新monitor绕过 | §12.1 | durable chain ID、parent monitor与continuous ordinal |
| child/permission未冻结 | §12.3-§12.4 | 独立scheduling gate，V1拒绝child registration |
| main可monitor child-origin process | §9.2.1、§12.4 | V1要求process与Host main RuntimeSession同ledger |
| restart无法重建exact delta | §5.5、§13.2 | mandatory bounded spool + available/unavailable recovered union |
| ingress缺少per-item owner/CAS | §8.1-§8.2 | QUEUED/PREPARING owner、withdraw/detach/replan与RunStart admission proof CAS |
| notification slot不会释放 | §8.5.1 | ledger-scoped account、原子release与deterministic head retirement |
| terminal status/suppression/close冲突 | §11.1、§13.4 | canonical status矩阵、所有yielded completion durable、process finalization后最终drain |
| Host ingress reservation/guard dual truth | §8.1-§8.2 | 删除两份DTO，RunStart只嵌唯一`HostIngressAdmissionProofFact` |
| lifecycle outcome四处重复 | §6、§10.3、§11.1 | 唯一terminal outcome + running state；observation改为progress/completion/expiry discriminated union |
| output cursor计数与stream identity重复 | §5.3-§5.5 | 计数由cursor差派生，process/journal只由`TerminalOutputStreamIdentityFact`拥有 |
| ingress source refs三份owner | §7.1 | attachment唯一持有exact refs；placement只排序，runtime owner只持attachment fingerprints |
| 派生bool/count进入durable state | §8.5、§8.5.1、§9.2、§11.4、§12 | 删除pending/eligibility/direction/allowed等字段，由union、outer event或当前policy重算 |
| one-shot API与`wait`产品语义过近 | §1.1、§2、§9 | 删除旧API；monitor registration持续到completion/cancel/expiry/close |
| 持续monitor可能制造无界通知 | §4.9、§8.5、§9.2、§10 | one-pending progress、双cursor、deterministic sliding window与completion-only cap |
| active run通知必须另起RunStart | §4.1、§7.1、§11.4、TM4 | PRE_MODEL_STEP companion与ModelStart原子join |
| safe-point可绕过autonomy budget | §8.2、§12.1 | safe-point/new-run CAS同一automatic delivery chain state |
| Grok持续/UI能力缺口 | §5.6、§16、TM5 | structured UI channel、10小时hard cap；明确不采纳任意shell filter |
| 旧watch命名形成compat双路径 | §1.1、§17、TM5 | 禁止alias、旧DTO/event decoder和production引用 |
| completion supersede丢pending progress正文 | §4.3、§10.1、§10.3 | 拆分observation/consumed cursor；terminal从consumed起并同批处置旧progress |
| PRE_MODEL_STEP存在stop/permission/model-step ABA | §7.1、§8.2、TM4 | 专用safe-point guard；ProviderInput/disposition/chain/ModelStart同writer transaction |
| tail receipt错误消费更早pending区间 | §6.2、TM3 | start/end可见区间dominance；artifact exact range；completion lifecycle/output双证明 |
| cancel ToolResult与monitor termination分裂 | §9.5、§13.3、TM1 | session-owned cancellation owner与唯一原子terminal batch |
| 多notification delivery只能归因一个monitor | §12.1、TM4 | ordered attachment fingerprints逐项join；一次sampling一个ordinal |
| observation cap静默丢completion | §9.2、§10.1、§12.2 | progress cap后进入completion-only并预留terminal observation |
| rate-limit算法不唯一 | §9.2、§12.2、TM4 | 单一sliding-window recurrence；去除burst/token-bucket/fixed-window双路径 |
| mandatory spool故障/阻塞语义缺失 | §5.5、§13.2、TM0 | 分离journal/spooled cursor，bounded async writer、typed gap与terminal drain deadline |

reviewer建议的实施顺序已映射为TM0-TM5，见§15；不再保留旧的“先建one-shot API，再补durable lifecycle和Host queue”的拓扑。

仍保留的重复值都是有明确validator的bounded join：每层own fingerprint、attribution复制nested semantic fingerprint、preview正文对应的UTF-8 bytes/hash、contract ID/version/fingerprint、account source/result revision、registration runtime与process-origin runtime identity，以及observation中的Host/wake-chain routing attribution。它们分别证明跨层identity、内容完整性、历史binding、CAS迁移或V1 same-ledger约束，不属于可删除的第二真源。
